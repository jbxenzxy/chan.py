# -*- coding: utf-8 -*-
"""A/B 对照：用真实回测引擎（Runner + 状态机）跑各出场方案，比较组合级指标。

每个方案都是完整重跑（含"出场变早 → 持仓期拒收信号变化"的二阶效应），
不是离线重放。方案开关见 exp_policy.ExpExitPolicy 的类属性。
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
def _find_repo(_p):
    """向上找到仓库根（含 Chan.py + DataAPI 的那一级）—— 从任意深度都成立。"""
    for _ in range(7):
        if (os.path.isfile(os.path.join(_p, "Chan.py"))
                and os.path.isdir(os.path.join(_p, "DataAPI"))):
            return _p
        _p = os.path.dirname(_p)
    return ""


EXP = HERE if os.path.basename(HERE) == "Exp" else os.path.dirname(HERE)
# 优先级：显式 CHAN_REPO > 向上找仓库根 > 沙盒布局（<work>/wt_latest）
REPO = (os.environ.get("CHAN_REPO") or _find_repo(HERE)
        or os.path.join(os.path.dirname(os.path.dirname(EXP)), "wt_latest"))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(EXP, "common"))     # 共享层（tdx_source / exp_policy …）
sys.path.insert(0, HERE)
os.chdir(REPO)

import exp_policy                                  # noqa: E402
from tdx_source import records as _page_records, stock_list   # noqa: E402
from Backtest.Runner import run                    # noqa: E402
from Backtest.ExitParams import (STOCK_EXIT_PARAMS, round_trip_cost,  # noqa: E402
                                 shares_for)

# ── 方案表 ────────────────────────────────────────────────────────────
PLANS = {
    "A0 基线（现状 1R→保本+0.5R）": {},
    "A1 降门槛 0.5R":               {"OVR_BE_TRIGGER_R": 0.5},
    "A2 +百分比保本 10%（用户方案）": {"PCT_BE_TRIGGER": 0.10, "PCT_BE_BUFFER": 0.0},
    "A3 +百分比保本 10% 落点+2%":    {"PCT_BE_TRIGGER": 0.10, "PCT_BE_BUFFER": 0.02},
    "A4 +百分比保本 8%":             {"PCT_BE_TRIGGER": 0.08, "PCT_BE_BUFFER": 0.0},
    "A5 +百分比保本 15%":            {"PCT_BE_TRIGGER": 0.15, "PCT_BE_BUFFER": 0.0},
    "A6 双条件 min(0.5R,10%)":      {"OVR_BE_TRIGGER_R": 0.5, "PCT_BE_TRIGGER": 0.10,
                                     "PCT_BE_BUFFER": 0.0},
    "A7 百分比跟踪 20%/回撤10%":     {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10},
    "A8 A2 + A7":                   {"PCT_BE_TRIGGER": 0.10, "PCT_BE_BUFFER": 0.0,
                                     "PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10},
    "A9 A2 + 时间兜底 12 根":        {"PCT_BE_TRIGGER": 0.10, "PCT_BE_BUFFER": 0.0,
                                     "TIME_STOP_BARS": 12},
}


def recs_of(code, freq, n=None):
    """**页面同源** records（通达信 vipdoc + 自实现前复权）。

    `n` 仅为兼容旧签名保留 —— 实际根数由 vipdoc 决定，**不做截断**
    （页面也不会多给一根）；截断会改变笔的起点，等于换了一条序列。
    """
    return _page_records(code, freq)


_CACHE = {}


def recs_cached(code, freq, n):
    key = (code, freq, n)
    if key not in _CACHE:
        _CACHE[key] = recs_of(code, freq, n)
    return _CACHE[key]


def one(code, freq, n):
    """返回该标的在该方案下的逐笔净收益序列。"""
    try:
        res = run(code[:2], code[2:], freq, None, None,
                  records=recs_cached(code, freq, n))
    except Exception:                                # noqa: BLE001
        return []
    out = []
    for t in res.trades:
        if t.open_ or t.net_return is None:
            continue
        out.append({
            "code": code, "freq": freq, "type": t.bsp_type, "side": t.side,
            "entry_date": t.entry_date[:10],
            "exit_date": (t.exit_date or "")[:10],
            "entry": t.entry_price, "R": t.r_distance,
            "R_pct": (t.r_distance / t.entry_price * 100) if t.entry_price else 0,
            "net": t.net_return * 100, "gross": (t.gross_return or 0) * 100,
            "r_mult": t.r_multiple, "reason": t.exit_reason,
            "bars": t.bars_held,
        })
    return out


# ── 进程池：CPU 型回测的**唯一**加速方式 ──────────────────────────────
# `one()` 内部是 `Runner.run`（纯 Python CPU 活），CPython 的 GIL 让
# ThreadPoolExecutor 在这类活上退化成串行 —— 实测 `bench_pool.py`：
#   线程 1 个 = 20.6s、线程 12 个 = 22.1s（**12 个不比 1 个快**）、
#   进程 8 个 = 6.8s（×3.05）、进程 16 个 = 7.9s（核多≠更快，本机 20 逻辑核甜点在 8~10）。
# 逐笔结果与线程路径**完全一致**（`_selfcheck_once` 每轮第一次调用时实地验一遍）。
_POOL = None
_CHK = {"done": False}


def _proc_init():
    """子进程内装 monkey-patch。

    ⚠ 必须：`install()` 改的是父进程内存里的模块属性，spawn 出来的子进程**不继承**
      ⇒ 少了这一步，子进程跑的是**原始策略**。症状是"结果静默变成基线值，而且看起来更快"，
      没有任何报错 —— 这正是要用护栏而不是靠自觉的原因。
    """
    import exp_policy
    exp_policy.install()


def _proc_one(args):
    """子进程内跑一只标的。cfg 变了才重配（一个方案内 cfg 恒定 ⇒ 每方案只重配一次）。"""
    import exp_policy
    import ab_run
    code, freq, n, cfg = args
    if getattr(_proc_one, "_cfg", None) != cfg:
        exp_policy.reset()
        exp_policy.configure(**cfg)
        _proc_one._cfg = cfg
    return ab_run.one(code, freq, n)


def get_pool(procs: int):
    """取（或建）常驻进程池。`procs <= 0` ⇒ None（走线程路径）。"""
    global _POOL
    if procs and procs > 0:
        if _POOL is None:
            _POOL = ProcessPoolExecutor(max_workers=procs, initializer=_proc_init)
        return _POOL
    return None


def close_pool():
    """跑完全部方案后收池（脚本 main 末尾调用）。"""
    global _POOL
    if _POOL is not None:
        _POOL.shutdown()
        _POOL = None


def _key(row):
    return (row["code"], row["entry_date"], row["exit_date"], row["reason"],
            round(float(row["net"]), 9), round(float(row["R"] or 0), 9))


def _selfcheck_once(codes, freq, n, cfg, workers, procs):
    """首次走进程路径时，用前 3 只标的对拍线程版 —— 把"配置没传进子进程"钉成红灯。"""
    sub = codes[:3]
    if len(sub) < 2:
        return True
    thr = sorted(_key(r) for r in run_plan(sub, freq, n, cfg, workers, procs=0))
    prc = sorted(_key(r) for r in run_plan(sub, freq, n, cfg, workers, procs=procs))
    ok = (thr == prc)
    print("  [自检] 进程池 vs 线程池逐笔一致 {}（{} 笔 / {} 笔）".format(
        "✔" if ok else "✘ 不一致，请勿采信本次结果", len(thr), len(prc)), flush=True)
    return ok


def run_plan(codes, freq, n, cfg, workers=10, procs=None):
    """跑一个方案 ⇒ 逐笔净收益列表。

    `procs > 0`  走**进程池**（真多核，×3.0~3.2，逐笔与线程版一致）
    `procs = 0`  走原**线程池**（GIL 串行化，等价单核 —— 保留只为对照）
    `procs=None` 读环境变量 `EXP_PROCS`（未设 = 0）

    注意：进程路径**不在父进程 install** —— 每个子进程自己装（`_proc_init`）。
    """
    if procs is None:
        procs = int(os.environ.get("EXP_PROCS", "0") or 0)
    pool = get_pool(procs)
    if pool is not None:
        if not _CHK["done"]:
            _CHK["done"] = True
            _selfcheck_once(codes, freq, n, cfg, workers, procs)
        res = list(pool.map(_proc_one, [(c, freq, n, cfg) for c in codes],
                            chunksize=4))
        return [r for sub in res for r in sub]
    exp_policy.install()
    exp_policy.reset()
    exp_policy.configure(**cfg)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            res = list(ex.map(lambda c: one(c, freq, n), codes))
    finally:
        exp_policy.uninstall()
        exp_policy.reset()
    rows = []
    for r in res:
        rows.extend(r)
    return rows


def metrics(rows):
    if not rows:
        return {}
    nets = [r["net"] for r in rows]
    wins = [x for x in nets if x > 0]
    loss = [x for x in nets if x <= 0]
    rms = [r["r_mult"] for r in rows if r["r_mult"] is not None]
    return {
        "n": len(rows),
        "avg_net": sum(nets) / len(nets),
        "win_rate": len(wins) / len(rows) * 100,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(loss) / len(loss) if loss else 0.0,
        "pf": (sum(wins) / abs(sum(loss))) if loss and sum(loss) else float("inf"),
        "avg_r": sum(rms) / len(rms) if rms else 0.0,
        "avg_bars": sum(r["bars"] or 0 for r in rows) / len(rows),
        "sl_pct": sum(1 for r in rows if r["reason"] == "sl") / len(rows) * 100,
        "be_pct": sum(1 for r in rows if r["reason"] == "breakeven") / len(rows) * 100,
        "tr_pct": sum(1 for r in rows if r["reason"] == "trailing") / len(rows) * 100,
        "worst": min(nets),
        "sum_net": sum(nets),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--workers", type=int, default=10,
                    help="线程数（procs=0 时生效；GIL 下≈单核）")
    ap.add_argument("--procs", type=int, default=0,
                    help="进程数（0=线程路径；建议 8~10，实测 ×3.0，见 bench_pool.py）")
    ap.add_argument("--out", default="ab_out.json")
    a = ap.parse_args()
    if a.procs <= 0:
        print("提示：加 --procs 8 可用多核跑方案（实测 ×3.0，逐笔结果一致）", flush=True)

    codes = stock_list(a.limit)
    print("标的 {} 只 / {} / {} 根".format(len(codes), a.freq, a.n), flush=True)
    # 预热缓存
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(lambda c: recs_cached(c, a.freq, a.n), codes))
    print("K 线缓存完成 {:.1f}s".format(time.time() - t0), flush=True)

    all_out = {}
    print("\n{:<32} {:>5} {:>9} {:>8} {:>9} {:>9} {:>7} {:>7} {:>7} {:>8}".format(
        "方案", "笔数", "平均净收益%", "胜率%", "平均盈利%", "平均亏损%",
        "盈亏比", "均R倍数", "持仓根", "止损占比%"))
    print("-" * 118)
    for name, cfg in PLANS.items():
        t1 = time.time()
        rows = run_plan(codes, a.freq, a.n, cfg, a.workers, a.procs)
        m = metrics(rows)
        all_out[name] = {"cfg": cfg, "metrics": m, "rows": rows}
        print("{:<32} {:>5} {:>9.2f} {:>8.1f} {:>9.2f} {:>9.2f} {:>7.2f} {:>7.2f} {:>7.1f} {:>8.1f}".format(
            name, m["n"], m["avg_net"], m["win_rate"], m["avg_win"], m["avg_loss"],
            m["pf"], m["avg_r"], m["avg_bars"], m["sl_pct"]),
            flush=True)
        print("     [{:.1f}s]  保本离场 {:.1f}% / 跟踪离场 {:.1f}% / 最差单笔 {:.1f}%".format(
            time.time() - t1, m["be_pct"], m["tr_pct"], m["worst"]), flush=True)

    close_pool()
    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump(all_out, f, ensure_ascii=False, indent=1)
    print("\n→ " + a.out)


if __name__ == "__main__":
    main()

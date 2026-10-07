# -*- coding: utf-8 -*-
"""出场结局分解：结构止损到底有几种？（修订 `signal_quality.py` 口径一的错误论断）

被修正的论断
------------
`signal_quality.py` 口径一原写：
    「`FAIL（原笔延伸）` ⟺ 结构止损被触碰 ⇒ 原笔延伸率不是独立指标」
**这句是错的**。两件事在代码里并不等价：

  1. **保护价 ≠ 分型极值。**
     `Trading/Strategy/Exit.py:353`：`raw_stop = base - R - buffer×tick`；
     `Exit.py:321`：`R = max(A, B)`，`A` = 入场价到分型极值的距离（`Exit.py:289`），
     `B = atr_sl_multiple × ATR`（`atr_sl_multiple=1.0`，`Backtest/ExitParams.py:70`）。
     ⇒ 保护价 ≤ 分型极值（B > A 时严格更低）。
     所以"价格穿过分型极值"只是"保护价被跌破"的**必要不充分**条件，反向不成立。
  2. **保护价可以在反向笔早就走出来之后才被跌破。**
     `Backtest/Runner.py:423` 每根闭合 K 线都调 `pol.check()`，只要持仓未平。
     常山北明那一单：浮盈 20%+、向上笔已走出，但浮盈始终没到 `1.0R`（≈31.6%）
     ⇒ 保本层从未启动 ⇒ 最终回到**初始保护价**，亏 31.6%。

⇒ 结构止损至少两族（用户 2026-10-07 的表述）：
   ② **原笔延伸型**：反向笔没走出来就被打掉 —— 入场信号的前提当场破裂；
   ① **走出反向笔型**：反向笔走出来了，浮盈没够到保本触发，最终回到初始保护价
      —— **这是出场问题，不是入场问题**。

本脚本做什么
------------
对每一笔**真实可交易**的信号（复刻 `Backtest/Runner.run` 的开平仓时序：一帧最多开
一笔、持仓期拒收新信号），用**真实** `LayeredExitPolicy` + `STOCK_EXIT_PARAMS`
逐根回放，记录：

  · `prim`      = 首个结构事件（`ext` 原笔延伸 / `succ` 走出反向笔 / `none` 都没发生）
  · `exit_reason` = 离场层（`sl` 初始保护价 / `breakeven` 保本层 / `trailing` 跟踪层）
  · `mfe_R`     = 离场时已见过的最大浮盈（R 倍数）—— 直接暴露"常山北明型"
  · `r_multiple` / `net_return` / `bars_held`

输出「首个结构事件 × 离场层」交叉表 —— 这张表就是"两类结构止损"的量化版本。

参数口径与仓库同步（不必在此抄数字）
------------------------------------
`STOCK_EXIT_PARAMS` 从 `Backtest/ExitParams.py` **import**，不复制；
`LayeredExitPolicy` / `Position` / `Signal` / `Instrument` / `bar_from_klu` /
`bsp_to_dict` 同样直接 import —— 本脚本不重写任何出场规则。

用法::

    python exit_fate.py --freq w --n 800 --limit 800 --procs 8 --out fate_w.json
    python exit_fate.py --freq d --n 800 --limit 800 --procs 8 --out fate_d.json
    python exit_fate.py --freq 30m --n 5000 --limit 300 --procs 6 --out fate_30m.json
    python exit_fate.py --freq w --limit 60 --verify 60     # 与仓库 run() 对拍
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

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

from ab_run import recs_cached                                     # noqa: E402
from tdx_source import stock_list                                  # noqa: E402
from Backtest.ExitParams import (STOCK_EXIT_PARAMS, TARGET_AMOUNT,  # noqa: E402
                                 round_trip_cost, shares_for)
from Backtest.Runner import (bar_from_klu, bsp_to_dict,             # noqa: E402
                             date_fmt_of, default_chan_config, kl_type_of)
from Backtest.State import State, next_state                       # noqa: E402
from Trading.Infra.Instrument import Instrument                    # noqa: E402
from Trading.Infra.Records import Position, Signal                 # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy                # noqa: E402


def recs_src(code: str, freq: str, n: int = None, src: str = "tdx") -> list:
    """**页面同源** K 线（与 `signal_quality.recs_src` 同一个取数函数）。

    `src` / `n` 仅为兼容旧签名保留；`src="ext"` 才是旧的腾讯/新浪链路。
    """
    if src in ("tencent", "sina", "ext"):
        from datetime import datetime
        if src == "sina":
            from fetch_min import fetch as fmin
            return [dict(r, dt=datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S"))
                    for r in fmin(code, freq, n or 2000)]
        from fetch_kline import fetch as fk
        return [dict(r, dt=datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S"))
                for r in fk(code, freq, n or 800)]
    return recs_cached(code, freq, n)


def _bi_anchor(chan):
    """(最新向上笔起点 idx, 最新向下笔起点 idx)。同一帧只扫一次笔表。"""
    newest_up, newest_dn = -1, -1
    for bi in chan[0].bi_list:
        b = bi.get_begin_klu().idx
        if bi.is_up():
            if b > newest_up:
                newest_up = b
        elif b > newest_dn:
            newest_dn = b
    return newest_up, newest_dn


def scan(code: str, freq: str, n: int, src: str = "tencent", max_bars=None):
    """复刻 `Backtest/Runner.run` 的时序跑一遍，返回 (每笔记录, 统计)。"""
    import Chan
    from DataAPI import TdxAPI
    from Common.CEnum import AUTYPE

    recs = recs_src(code, freq, n, src)
    if not recs:
        return [], {}
    date_fmt = date_fmt_of(freq)
    market, num = str(code)[:2], str(code)[2:]
    full = "{}{}".format(market.lower(), num)

    pol = LayeredExitPolicy(dict(STOCK_EXIT_PARAMS))   # 全程唯一实例（ATR 连续）
    inst = Instrument(product=None)
    rows: list = []
    state = State.FLAT
    pos = None
    cur: dict = {}
    frozen: dict = {}
    frame = 0
    stat = {"signals": 0, "rejected": 0, "type_appended": 0}

    with TdxAPI.tdx_data_context(recs):
        chan = Chan.CChan(
            code=full, begin_time=None, end_time=None,
            data_src="custom:TdxAPI.CTdxAPI", lv_list=[kl_type_of(freq)],
            config=default_chan_config(), autype=AUTYPE.NONE, market_type="stock")
        for _ in chan.step_load():
            frame += 1
            if max_bars is not None and frame > max_bars:
                break
            cur_klu = chan[0][-1][-1]
            bar = bar_from_klu(cur_klu, date_fmt)
            pol.on_bar(bar, inst)

            # ── 1) 先结算已有持仓（与 Runner.py:422-436 同序） ──────────────
            if state is State.IN_TRADE and pos is not None:
                c = cur
                c["bars"] += 1
                up_idx, dn_idx = _bi_anchor(chan)
                if c["side"] > 0:
                    if not c["ext_frame"] and bar.low < c["ext"]:
                        c["ext_frame"] = frame
                    if not c["succ_frame"] and up_idx >= c["anchor"]:
                        c["succ_frame"] = frame
                    c["best"] = max(c["best"], bar.high)
                else:
                    if not c["ext_frame"] and bar.high > c["ext"]:
                        c["ext_frame"] = frame
                    if not c["succ_frame"] and dn_idx >= c["anchor"]:
                        c["succ_frame"] = frame
                    c["best"] = min(c["best"], bar.low)

                chk = pol.check(pos, bar, inst)
                if chk is not None:
                    if chk.plan is not None:
                        pos.exit_plan = chk.plan
                    if not chk.only_update:
                        fill = float(chk.fill_price if chk.fill_price else chk.price)
                        sign = int(pos.side.sign)
                        R = float(c["R"] or 0.0)
                        gross = (fill - pos.entry_price) * sign / pos.entry_price
                        cost = round_trip_cost(pos.entry_price, fill, sign, c["shares"])
                        notional = float(c["shares"]) * pos.entry_price
                        c.update({
                            "exit_date": bar.date, "exit_price": fill,
                            "exit_reason": str(chk.reason),
                            "bars_held": frame - c["entry_frame"],
                            "r_multiple": ((fill - pos.entry_price) * sign / R)
                            if R > 0 else None,
                            "gross": gross, "net": gross - cost / notional,
                            "mfe_R": (c["best"] - pos.entry_price) * sign / R
                            if R > 0 else None,
                        })
                        s, e = c["succ_frame"], c["ext_frame"]
                        if e and (not s or e <= s):
                            c["prim"] = "ext"
                        elif s:
                            c["prim"] = "succ"
                        else:
                            c["prim"] = "none"
                        rows.append(c)
                        pos, cur = None, {}
                        state = next_state(state, exit_=True)

            # ── 2) 再看本帧新信号（与 Runner.py:438-471 同序） ──────────────
            for bsp in chan[0].bs_point_lst.bsp_iter():
                key = (str(bsp.klu.time), bool(bsp.is_buy))
                if key in frozen:
                    if frozen[key] != bsp.type2str():
                        stat["type_appended"] += 1
                        frozen[key] = bsp.type2str()
                    continue
                if bsp.klu.idx != cur_klu.idx:
                    continue
                frozen[key] = bsp.type2str()
                stat["signals"] += 1
                if state is State.IN_TRADE:
                    stat["rejected"] += 1      # 持仓期拒收（Runner §4.7.3 Q13）
                    continue
                bdict = bsp_to_dict(bsp, date_fmt)
                sig = Signal.from_bsp(bdict, symbol=full, freq=freq)
                entry = float(bdict["price"])
                plan = pol.plan(sig, entry, inst)     # ★ 冻结就在这一行
                f_klu = bsp.bi.get_end_klu()
                is_buy = bool(bsp.is_buy)
                ext = float(f_klu.low) if is_buy else float(f_klu.high)
                R = float(plan.params.get("R") or 0.0)
                pos = Position(
                    symbol=full, side=sig.side, volume=1, entry_price=entry,
                    entry_at="", entry_bar_ts=int(bdict["timestamp"]),
                    signal_key=sig.key, open_order_id="", exit_plan=plan)
                cur = {
                    "code": code, "freq": freq,
                    "side": 1 if is_buy else -1, "type": str(bdict["type"]),
                    "entry_date": bdict["date"], "entry": entry,
                    "ext": ext, "A": abs(entry - ext), "R": R,
                    "anchor": int(f_klu.idx), "entry_frame": frame,
                    "shares": shares_for(entry, full, TARGET_AMOUNT, is_index=False),
                    "ext_frame": 0, "succ_frame": 0, "bars": 0, "best": entry,
                }
                state = next_state(state, entry=True)
                break                          # 一帧最多开一笔

    # 收尾：跑到末根仍未平仓 ⇒ 计为「未离场」（不计入离场层统计，但保留样本）
    if cur:
        c = cur
        s, e = c["succ_frame"], c["ext_frame"]
        c["prim"] = ("ext" if (e and (not s or e <= s)) else
                     "succ" if s else "none")
        R = float(c["R"] or 0.0)
        c.update({"exit_date": None, "exit_price": None, "exit_reason": "open",
                  "bars_held": None, "r_multiple": None, "gross": None, "net": None,
                  "mfe_R": (c["best"] - c["entry"]) * c["side"] / R if R > 0 else None})
        rows.append(c)

    stat["trades"] = len(rows)
    return rows, stat


# ════════════════════════════════════════════════════════════════════
# 汇总 / 打印
# ════════════════════════════════════════════════════════════════════
def _avg(v):
    v = [x for x in v if x is not None]
    return (sum(v) / len(v)) if v else 0.0


def _pct(a, b):
    return ("{:.1f}%".format(a / b * 100)) if b else "—"


def paired_boot(d, iters=10000, seed=7):
    """配对 bootstrap：均值是否显著≠0（双侧）。"""
    d = [x for x in d if x is not None]
    if not d:
        return 0.0, 1.0, 0
    obs = sum(d) / len(d)
    rnd = random.Random(seed)
    n = len(d)
    cnt = sum(1 for _ in range(iters)
              if sum(d[rnd.randrange(n)] for _ in range(n)) / n <= 0)
    return obs, min(2 * min(cnt / iters, 1 - cnt / iters), 1.0), n


def report(rows, stat, title):
    closed = [r for r in rows if r["exit_reason"] != "open"]
    n = len(closed)
    print("══ {} ══".format(title))
    print("信号 {} 条（持仓期拒收 {} 条）⇒ 成交 {} 笔，末根未平 {} 笔".format(
        stat["signals"], stat["rejected"], n, len(rows) - n), flush=True)
    if not n:
        return {}
    sl = [r for r in closed if r["exit_reason"] == "sl"]
    be = [r for r in closed if r["exit_reason"] == "breakeven"]
    tr = [r for r in closed if r["exit_reason"] == "trailing"]
    print("\n── 离场层分布（这是「结构止损率」的正面回答）──")
    print("{:<26} {:>8} {:>9} {:>10}".format("离场层", "笔数", "占比", "均净收益"))
    for lab, sub in (("sl  初始保护价（止损）", sl),
                     ("breakeven 保本层", be),
                     ("trailing 跟踪层", tr)):
        print("{:<26} {:>8} {:>9} {:>9.2f}%".format(
            lab, len(sub), _pct(len(sub), n), _avg([r["net"] for r in sub]) * 100))

    print("\n── 交叉表：首个结构事件 × 离场层（「两类结构止损」的量化版）──")
    cells = {}
    for r in closed:
        cells.setdefault(r["prim"], {}).setdefault(r["exit_reason"], []).append(r)
    print("{:<22} {:>16} {:>16} {:>16}".format("首个结构事件", "sl（止损）",
                                               "breakeven", "trailing"))
    for prim, lab in (("ext", "原笔延伸（反向笔没走出）"),
                      ("succ", "走出反向笔"),
                      ("none", "都没发生")):
        c = cells.get(prim, {})
        print("{:<22} {:>16} {:>16} {:>16}".format(
            lab,
            "{} ({})".format(len(c.get("sl", [])),
                             _pct(len(c.get("sl", [])), n)),
            "{} ({})".format(len(c.get("breakeven", [])),
                             _pct(len(c.get("breakeven", [])), n)),
            "{} ({})".format(len(c.get("trailing", [])),
                             _pct(len(c.get("trailing", [])), n))))

    ext_n = len([r for r in closed if r["prim"] == "ext"])
    succ_n = len([r for r in closed if r["prim"] == "succ"])
    print("\n  · 原笔延伸（=口径一的 FAIL）      {} 笔 = {}".format(ext_n, _pct(ext_n, n)))
    print("  · 结构止损离场（exit_reason=sl）   {} 笔 = {}  ← 比上一行**多** {}".format(
        len(sl), _pct(len(sl), n),
        _pct(len(sl) - ext_n, n)))
    print("  · 其中「走出反向笔却仍止损」（常山北明型）= {} 笔 = {}".format(
        len([r for r in sl if r["prim"] == "succ"]),
        _pct(len([r for r in sl if r["prim"] == "succ"]), n)))

    print("\n── 三类结局的收益画像 ──")
    groups = [
        ("② 原笔延伸 → 止损", [r for r in sl if r["prim"] == "ext"]),
        ("① 走出反向笔 → 止损", [r for r in sl if r["prim"] == "succ"]),
        ("③ 走出反向笔 → 保本/跟踪离场",
         [r for r in closed if r["prim"] == "succ"
          and r["exit_reason"] in ("breakeven", "trailing")]),
    ]
    print("{:<30} {:>7} {:>9} {:>11} {:>10} {:>10} {:>9}".format(
        "分组", "笔数", "均净收益", "胜率(net>0)", "均R倍数", "均浮盈R", "均持有"))
    for lab, sub in groups:
        if not sub:
            continue
        print("{:<30} {:>7} {:>8.2f}% {:>10} {:>10.2f} {:>10.2f} {:>9.1f}".format(
            lab, len(sub), _avg([r["net"] for r in sub]) * 100,
            _pct(sum(1 for r in sub if (r["net"] or 0) > 0), len(sub)),
            _avg([r["r_multiple"] for r in sub]),
            _avg([r["mfe_R"] for r in sub]),
            _avg([r["bars_held"] for r in sub])))

    print("\n── 显著性的正确问法：三类结局的净收益是否显著为负（配对 bootstrap，按标的）──")
    by_code = {}
    for r in closed:
        by_code.setdefault((r["code"], r["prim"]), []).append(r["net"])
    for lab, prim in (("② 原笔延伸", "ext"), ("① 走出反向笔", "succ"),
                      ("③ 保本/跟踪离场(反向笔)", "succ-ok")):
        keys = [k for k in by_code if k[1] == ("succ" if prim == "succ-ok" else prim)]
        if prim == "succ-ok":
            keep = {(r["code"], r["prim"]) for r in closed
                    if r["prim"] == "succ"
                    and r["exit_reason"] in ("breakeven", "trailing")}
            keys = [k for k in keys if k in keep]
        elif prim == "succ":
            keep = {(r["code"], r["prim"]) for r in closed
                    if r["prim"] == "succ" and r["exit_reason"] == "sl"}
            keys = [k for k in keys if k in keep]
        per_code = [_avg(by_code[k]) for k in keys]
        if not per_code:
            continue
        # 单侧：检验均值是否 < 0
        rnd = random.Random(7)
        nc = len(per_code)
        cnt = sum(1 for _ in range(10000)
                  if sum(per_code[rnd.randrange(nc)] for _ in range(nc)) / nc >= 0)
        obs = sum(per_code) / nc
        print("  {:<24} 标的 {:>4}  均净 {:>+7.2f}%  P(均值≥0)={:.4f}{}".format(
            lab, nc, obs * 100, cnt / 10000,
            " ***" if cnt / 10000 < 0.001 else
            " **" if cnt / 10000 < 0.01 else
            " *" if cnt / 10000 < 0.05 else " n.s."))
    return {"n_trades": n, "sl": len(sl), "be": len(be), "tr": len(tr),
            "prim_ext": ext_n, "prim_succ": succ_n,
            "succ_sl": len([r for r in sl if r["prim"] == "succ"]),
            "net": {k: _avg([r["net"] for r in v]) * 100
                    for k, v in (("sl", sl), ("be", be), ("tr", tr))}}


# ── 进程池用的顶层函数 ──────────────────────────────────────────────
def _warm(args):
    return bool(recs_src(*args))


def _scan(args):
    return scan(*args)


def verify(codes, freq, src, n, limit):
    """与仓库 `Backtest.Runner.run` 对拍：同一批标的，比笔数与离场层分布。"""
    from Backtest.Runner import run
    print("\n══════ 自检：与仓库 Backtest.Runner.run 对拍（{} 只） ══════".format(limit))
    mine = {"sl": 0, "breakeven": 0, "trailing": 0, "n": 0}
    theirs = {"sl": 0, "breakeven": 0, "trailing": 0, "n": 0}
    diff_examples = []
    for c in codes[:limit]:
        recs = recs_src(c, freq, n, src)
        if not recs:
            continue
        rows, _ = scan(c, freq, n, src)
        for r in rows:
            if r["exit_reason"] != "open":
                mine[r["exit_reason"]] = mine.get(r["exit_reason"], 0) + 1
                mine["n"] += 1
        res = run(str(c)[:2], str(c)[2:], freq, records=recs,
                  exit_params=dict(STOCK_EXIT_PARAMS))
        for t in res.trades:
            if t.exit_reason:
                theirs[t.exit_reason] = theirs.get(t.exit_reason, 0) + 1
                theirs["n"] += 1
        if len([t for t in res.trades if t.exit_reason]) != mine["n"]:
            # 单标的笔数不一致 → 记录前 3 例供排查
            for t in res.trades:
                if t.exit_reason and len(diff_examples) < 3:
                    diff_examples.append((c, t.entry_date, t.exit_reason))
    print("{:<14} {:>10} {:>10} {:>10} {:>10}".format("来源", "笔数", "sl",
                                                      "breakeven", "trailing"))
    for lab, d in (("本脚本回放", mine), ("仓库 run()", theirs)):
        print("{:<14} {:>10} {:>10} {:>10} {:>10}".format(
            lab, d["n"], d["sl"], d["breakeven"], d["trailing"]))
    ok = (mine["n"] == theirs["n"] and mine["sl"] == theirs["sl"]
          and mine["breakeven"] == theirs["breakeven"]
          and mine["trailing"] == theirs["trailing"])
    print("→ {}".format("完全一致 ✔" if ok else "不一致 ✘（见上表；差异样本 {}）".format(
        diff_examples)))
    return ok


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=None,
                    help="已废弃：根数由 vipdoc 决定（保留仅为兼容旧命令行）")
    ap.add_argument("--src", default="tdx", help="tdx=页面同源（默认）；ext=旧外部链路")
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--procs", type=int, default=8)
    ap.add_argument("--verify", type=int, default=0)
    ap.add_argument("--out", default="fate.json")
    a = ap.parse_args()

    codes = stock_list(a.limit)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(_warm, [(c, a.freq, a.n, a.src) for c in codes]))
    codes = [c for c in codes if recs_src(c, a.freq, a.n, a.src)]
    print("周期={} 源={} 有效标的 {} 只，预热 {:.0f}s".format(
        a.freq, a.src, len(codes), time.time() - t0), flush=True)

    if a.verify:
        verify(codes, a.freq, a.src, a.n, a.verify)

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=a.procs) as ex:
        res = list(ex.map(_scan, [(c, a.freq, a.n, a.src)
                                  for c in codes], chunksize=4))
    rows = [r for sub, _ in res for r in sub]
    stat = {"signals": sum(s["signals"] for _, s in res),
            "rejected": sum(s["rejected"] for _, s in res),
            "type_appended": sum(s["type_appended"] for _, s in res)}
    print("回放 {:.0f}s（{} 进程）\n".format(time.time() - t0, a.procs), flush=True)

    summary = report(rows, stat, "{} 周期 出场结局分解".format(a.freq))
    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump({"meta": vars(a), "params": dict(STOCK_EXIT_PARAMS),
                   "stat": stat, "summary": summary, "rows": rows},
                  f, ensure_ascii=False)
    print("\n→ " + a.out)


if __name__ == "__main__":
    main()

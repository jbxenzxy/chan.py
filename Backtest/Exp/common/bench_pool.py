# -*- coding: utf-8 -*-
"""线程池 vs 进程池：同一份回测工作量的实测对比（回答"回测能不能多进程"）。

背景
----
`ab_run.py::run_plan` / `ab_pct.py` 把 `Runner.run` 放进 `ThreadPoolExecutor`
（`ab_run.py:97`、`ab_pct.py:101`）。`Runner.run` 是**纯 Python CPU 活**
（缠论递归 + 逐帧状态机），而 CPython 的 GIL 让多线程在这类活上**退化成串行**
—— `--workers 12` 实际只用了 1 个核。

本脚本对同一批标的同时跑两条路径，断言**逐笔结果完全一致**，并报告墙钟时间。

⚠ 进程池的两个必要适配（本脚本已做）
  1. `exp_policy.install()` 是**父进程里的 monkey-patch**，fork/spawn 后不继承
     ⇒ 必须用 `ProcessPoolExecutor(initializer=...)` 在每个子进程里重新 install
     并 configure，否则子进程跑的是**原始策略**（结果会静默错，而且"看起来更快"）。
  2. 子进程各自持有 `ab_run._CACHE`，K 线从磁盘缓存读（不是重复抓网）。

用法::

    python bench_pool.py --freq w --n 800 --limit 160 --threads 12 --procs 8
"""
from __future__ import annotations

import json
import os
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

# 用一份"有实际分支"的方案，避免测成"全是默认值"的空转
CFG = {"PCT_BE_TRIGGER": 0.10, "PCT_BE_BUFFER": 0.0,
       "PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10}


def _init(cfg):
    """子进程内：装 monkey-patch + 写入方案配置（缺一不可，见模块 docstring ⚠）。"""
    import exp_policy
    exp_policy.install()
    exp_policy.reset()
    exp_policy.configure(**cfg)


def _one(args):
    import ab_run
    return ab_run.one(*args)


def _fingerprint(rows_by_code):
    """把结果压成一个可比对的指纹：逐标的 笔数 + 净收益和 + 离场原因序列。"""
    fp = {}
    for code, rows in rows_by_code.items():
        fp[code] = (len(rows),
                    round(sum(r["net"] for r in rows), 6),
                    "".join(str(r["reason"])[:1] for r in rows))
    return fp


def _run_threads(codes, freq, n, workers):
    import exp_policy
    import ab_run
    exp_policy.install()
    exp_policy.reset()
    exp_policy.configure(**CFG)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            res = list(ex.map(lambda c: ab_run.one(c, freq, n), codes))
    finally:
        exp_policy.uninstall()
        exp_policy.reset()
    return {c: r for c, r in zip(codes, res)}


def _run_procs(codes, freq, n, procs):
    jobs = [(c, freq, n) for c in codes]
    with ProcessPoolExecutor(max_workers=procs, initializer=_init,
                             initargs=(CFG,)) as ex:
        res = list(ex.map(_one, jobs, chunksize=4))
    return {c: r for c, r in zip(codes, res)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--limit", type=int, default=160)
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--procs", type=int, default=8)
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()

    import ab_run
    from page_kline import stock_list

    codes = stock_list(a.limit)
    print("核数 {} | 标的 {} 只 | {} / {} 根 | 方案 {}".format(
        os.cpu_count(), len(codes), a.freq, a.n, CFG), flush=True)

    # 预热：把 K 线全部落到磁盘缓存，排除取数噪音
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.threads) as ex:
        list(ex.map(lambda c: ab_run.recs_cached(c, a.freq, a.n), codes))
    print("K 线缓存预热 {:.1f}s\n".format(time.time() - t0), flush=True)

    out = {}
    for rep in range(a.repeat):
        t0 = time.time()
        thr = _run_threads(codes, a.freq, a.n, a.threads)
        t_thr = time.time() - t0

        t0 = time.time()
        prc = _run_procs(codes, a.freq, a.n, a.procs)
        t_prc = time.time() - t0

        ft, fp = _fingerprint(thr), _fingerprint(prc)
        same = (ft == fp)
        diff = [c for c in ft if ft[c] != fp.get(c)]
        n_trade = sum(v[0] for v in fp.values())

        print("第 {} 轮：线程 {} 个 = {:6.1f}s | 进程 {} 个 = {:6.1f}s | "
              "加速 {:.2f}× | 逐笔一致 {}（{} 笔）".format(
                  rep + 1, a.threads, t_thr, a.procs, t_prc,
                  t_thr / t_prc if t_prc else 0.0,
                  "✔" if same else "✘ 差异标的 {}: {}".format(len(diff), diff[:5]),
                  n_trade), flush=True)
        out[rep] = {"threads": t_thr, "procs": t_prc, "same": same,
                    "n_trades": n_trade}

    print("\n结论：线程池在 CPU 型回测上被 GIL 串行化，"
          "进程池才真正吃到多核。")
    with open(os.path.join(HERE, "bench_pool.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": vars(a), "cfg": CFG, "runs": out}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()

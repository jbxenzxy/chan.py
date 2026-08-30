# -*- coding: utf-8 -*-
"""端到端验证：CChan 并发建链结果与串行基线完全一致

这是 _tdx_data per-request 改造的核心断言。用合成日线走生产同款构造路径
（data_src="custom:TdxAPI.CTdxAPI" + tdx_data_context 注入 + step_load 消费）。

判据不是「条数等于 120」（缠论会做 K 线包含合并，120 根原始 ≠ 120 根合并
K 线），而是**并发结果与串行基线逐项一致**：
    · 单线程先跑一遍，记录每个标的的 (K线数, 末根收盘, 笔数, 中枢数) 作基线
    · 8 线程 × 3 轮并发重跑，任一结果与基线不符即判串数据

改造前这条路径必然挂：CTdxAPI._tdx_data 是类变量，8 个线程互相覆盖，
只能靠 _stock_analysis_lock 串行兜住。
"""
import os
import sys
import threading
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))            # .../src/Test
_REPO_ROOT = os.path.dirname(_HERE)                            # .../src
sys.path.insert(0, _REPO_ROOT)

# 压低 GIL 切换间隔：在极端调度下仍能保持零串数据
sys.setswitchinterval(1e-4)

# 无第三方依赖的环境（CI 沙箱）下，用最小 stub 顶替 pandas / numpy /
# chinese_calendar，使本用例在没有安装生产依赖时也能跑结构性验证。
_STUBS = os.path.join(os.path.dirname(_REPO_ROOT), "stubs")
if os.path.isdir(_STUBS):
    try:
        import pandas  # noqa: F401
    except ImportError:
        sys.path.insert(0, _STUBS)

from Common.CEnum import AUTYPE, KL_TYPE  # noqa: E402
from Chan import CChan  # noqa: E402
from ChanConfig import CChanConfig  # noqa: E402
from DataAPI.TdxAPI import tdx_data_context  # noqa: E402

N_THREADS = 8
ROUNDS = 3
BARS = 120


def make_records(thread_idx, bars=BARS):
    """每个线程一份形态不同的走势，末根收盘价 = 100 + thread_idx"""
    recs = []
    base = datetime(2025, 1, 1)
    for i in range(bars):
        amp = 1.0 + thread_idx * 0.37
        o = 100.0 + amp * (i % 7)
        c = 100.0 + amp * ((i + 1) % 7)
        recs.append({
            "dt": base + timedelta(days=i),
            "open": o, "close": c,
            "high": max(o, c) + 0.5, "low": min(o, c) - 0.5,
            "vol": 1000 + i, "amount": (o + c) * 1000,
        })
    last = recs[-1]
    last["close"] = 100.0 + thread_idx
    last["high"] = max(last["open"], last["close"]) + 0.5
    last["low"] = min(last["open"], last["close"]) - 0.5
    return recs


def build_chan(records, code):
    """与 AppEngine._analyze_stock_internal 单窗口分支同构"""
    config = CChanConfig()
    config.kl_data_check = False
    with tdx_data_context(records):
        chan = CChan(
            code=code, begin_time=None, end_time=None,
            data_src="custom:TdxAPI.CTdxAPI",
            lv_list=[KL_TYPE.K_DAY], config=config,
            autype=AUTYPE.NONE, market_type="stock",
        )
        for _snapshot in chan.step_load():
            pass
    return chan


def fingerprint(chan):
    """结构指纹：足以检出任何数据串扰"""
    kl_list = chan[KL_TYPE.K_DAY]
    return (
        len(kl_list.lst),                                   # 合并后 K 线数
        round(kl_list.lst[-1].lst[-1].close, 6),            # 末根收盘
        len(kl_list.bi_list),                               # 笔数
        len(kl_list.zs_list),                               # 中枢数
        round(kl_list.lst[-1].high, 6),                     # 末根合并K线高点
    )


failures = []
_fail_lock = threading.Lock()


def worker(idx, barrier, baseline):
    recs = make_records(idx)
    expect = baseline[idx]
    for _ in range(ROUNDS):
        try:
            barrier.wait()
            got = fingerprint(build_chan(recs, f"sh60000{idx}"))
            if got != expect:
                with _fail_lock:
                    failures.append(f"thread{idx}: 期望 {expect}，实到 {got}")
                return
        except Exception as e:  # noqa: BLE001
            import traceback
            with _fail_lock:
                failures.append(f"thread{idx} 异常 {type(e).__name__}: {e}\n"
                                + traceback.format_exc())
            return


def main():
    print(f"① 串行基线：{N_THREADS} 个标的各建链一次")
    baseline = {}
    for i in range(N_THREADS):
        baseline[i] = fingerprint(build_chan(make_records(i), f"sh60000{i}"))
    print("   基线:", baseline[0], "…", baseline[N_THREADS - 1])
    if len(set(baseline.values())) != N_THREADS:
        print("  [FAIL] 各标的基线指纹重复，合成数据不足以区分线程")
        return 1
    print(f"  [PASS] {N_THREADS} 个标的指纹互不相同（合成数据有效）")

    print(f"\n② 并发复现：{N_THREADS} 线程 × {ROUNDS} 轮")
    barrier = threading.Barrier(N_THREADS)
    threads = [threading.Thread(target=worker, args=(i, barrier, baseline))
               for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(180)
        if t.is_alive():
            failures.append("线程超时（可能死锁）")

    total = N_THREADS * ROUNDS
    if failures:
        print(f"  [FAIL] {len(failures)} 项")
        for f in failures[:5]:
            print("   ", f)
        return 1
    print(f"  [PASS] {total} 次并发建链全部与串行基线一致，零串数据")
    return 0


if __name__ == "__main__":
    sys.exit(main())

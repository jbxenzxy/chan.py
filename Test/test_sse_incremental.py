# -*- coding: utf-8 -*-
"""
P2-4 增量快照 —— 行为回归守护用例
=====================================================================
背景：P2-4 将 SSE 每根 K 线完成时的全量 O(n) 快照重建改为增量
（复用缓存 klines 仅追加新确认K线 + EMA 状态续算 MACD）。本守护用例
锁定增量路径与全量路径输出一致，防止「增量优化引入 MACD/klines 漂移」：

  ① 增量 klines：新确认K线追加、预览bar剥离、合并场景尾部 OHLC 修正
  ② 增量 MACD ≡ 全量 MACD：对同一序列，逐根增量续算与全量重算
     逐位一致（EMA 状态续算正确性）
  ③ 快照同构：_extract_realtime_snapshot 增量路径（prev_klines/
     prev_ema_state）与全量路径 klines/meta.kline_count 一致
  ④ 状态缺失回退：prev_ema_state=None 时增量路径回退全量重算，仍正确

运行：python Test/test_sse_incremental.py            # 校验（run_all 组件）
      python Test/test_sse_incremental.py --update   # 兼容 run_all --update
"""
import argparse
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    try:
        import typing_extensions
        typing.Self = typing_extensions.Self
    except ImportError:
        pass

from App.AppSSE import (
    _incremental_klines,
    _apply_macd_full,
    _apply_macd_incremental,
    _extract_realtime_snapshot,
)


# ── 轻量 Mock：仅承载 _incremental_klines 需要的 kl_list 结构 ────────
class MockTime:
    def __init__(self, ts):
        self.ts = ts

    def toFmtStr(self, fmt):
        from datetime import datetime
        return datetime.fromtimestamp(self.ts).strftime(fmt)


class MockMetric:
    def __init__(self, vol=0, turnover=0.0):
        self.metric = {"volume": vol, "turnover": turnover}


class MockKlu:
    def __init__(self, ts, o=100.0, h=101.0, l=99.0, c=100.5, vol=10):
        self.time = MockTime(ts)
        self.open = o; self.high = h; self.low = l; self.close = c
        self.trade_info = MockMetric(vol=vol)


class MockKlc:
    def __init__(self, klus):
        self.lst = klus
        self.fx = None


class MockKlList:
    def __init__(self, klus):
        self.lst = [MockKlc(klus)]
        self.bi_list = []
        self.zs_list = []
        self.seg_list = []


def _klu(ts, c=100.5, vol=10):
    return MockKlu(ts, c=c, vol=vol)


def _preview_bar(dt_str):
    return {"date": dt_str, "timestamp": 0, "open": 0, "high": 0,
            "low": 0, "close": 0, "vol": 0, "amount": 0, "dif": 0, "dea": 0, "macd": 0}


def _fmt_ts(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M:%S")


def test_incremental_klines(failures):
    """① 增量 klines：新确认K线追加 + 预览bar剥离 + 合并修正"""
    base = 1_700_000_000
    fmt = "%Y/%m/%d %H:%M:%S"
    # 缓存快照：2 根确认K线 + 1 根预览bar
    prev = [
        {"date": _fmt_ts(base), "open": 1, "high": 2, "low": 0, "close": 1.5, "vol": 10, "amount": 0,
         "dif": 0, "dea": 0, "macd": 0},
        {"date": _fmt_ts(base + 15), "open": 2, "high": 3, "low": 1, "close": 2.5, "vol": 10, "amount": 0,
         "dif": 0, "dea": 0, "macd": 0},
        _preview_bar(_fmt_ts(base + 30)),  # 预览bar（下一根形成K线）
    ]
    # chan 推进：新增第 3 根确认K线
    kl_list = MockKlList([_klu(base), _klu(base + 15), _klu(base + 30, c=3.5)])
    out, changed_idx = _incremental_klines(prev, kl_list, fmt)
    if len(out) != 3:
        failures.append(f"① 增量后数量 {len(out)} != 3")
        print(f"[FAIL] ① 增量 klines: 数量 {len(out)}")
        return
    if out[-1]["date"] != _fmt_ts(base + 30) or out[-1]["close"] != 3.5:
        failures.append(f"① 新确认K线未正确追加: {out[-1]}")
        print(f"[FAIL] ① 增量 klines: 尾部 {out[-1]}")
        return
    if changed_idx != 2:
        failures.append(f"① changed_idx {changed_idx} != 2")
        print(f"[FAIL] ① 增量 klines: changed_idx {changed_idx}")
        return
    print("[PASS] ① 增量 klines: 新确认K线追加 + 预览bar剥离")

    # 合并场景：chan 尾部K线 OHLC 修正（日期不变）
    kl_list2 = MockKlList([_klu(base), _klu(base + 15, c=9.9)])
    out2, changed_idx2 = _incremental_klines(prev, kl_list2, fmt)
    if len(out2) != 2:
        failures.append(f"① 合并场景数量 {len(out2)} != 2")
        print(f"[FAIL] ① 合并场景: 数量 {len(out2)}")
        return
    if out2[-1]["close"] != 9.9:
        failures.append(f"① 合并场景尾部 OHLC 未修正: {out2[-1]}")
        print(f"[FAIL] ① 合并场景: 尾部 {out2[-1]}")
        return
    if changed_idx2 != 1:
        failures.append(f"① 合并场景 changed_idx {changed_idx2} != 1")
        print(f"[FAIL] ① 合并场景: changed_idx {changed_idx2}")
        return
    print("[PASS] ① 增量 klines: 合并场景尾部 OHLC 修正")


def test_macd_incremental_equals_full(failures):
    """② 增量 MACD ≡ 全量 MACD：逐根续算与全量重算逐位一致"""
    import random
    random.seed(42)
    closes = [100.0 + i * 0.5 + random.uniform(-1, 1) for i in range(120)]
    klines = [{"close": c, "dif": 0, "dea": 0, "macd": 0} for c in closes]

    # 全量：一次性重算
    full = [dict(k) for k in klines]
    _apply_macd_full(full)

    # 增量：先算前 60 根，再逐根续算到 120
    inc = [dict(k) for k in klines[:60]]
    state = _apply_macd_full(inc)
    for i in range(60, len(klines)):
        inc.append(dict(klines[i]))
        state = _apply_macd_incremental(inc, len(inc) - 1, state)

    for i in range(len(full)):
        for key in ("dif", "dea", "macd"):
            if abs(full[i][key] - inc[i][key]) > 1e-9:
                failures.append(f"② 第{i}根 {key}: 全量={full[i][key]} 增量={inc[i][key]}")
                print(f"[FAIL] ② MACD 漂移: 第{i}根 {key} 全量={full[i][key]} 增量={inc[i][key]}")
                return
    print("[PASS] ② 增量 MACD ≡ 全量 MACD（120 根逐位一致）")


def test_macd_state_missing_fallback(failures):
    """④ 状态缺失回退：prev_ema_state=None 时增量路径回退全量重算"""
    closes = [100.0 + i for i in range(40)]
    klines = [{"close": c, "dif": 0, "dea": 0, "macd": 0} for c in closes]
    full = [dict(k) for k in klines]
    _apply_macd_full(full)

    inc = [dict(k) for k in klines]
    state = _apply_macd_incremental(inc, 0, None)  # 状态缺失 → 回退全量
    for i in range(len(full)):
        for key in ("dif", "dea", "macd"):
            if abs(full[i][key] - inc[i][key]) > 1e-9:
                failures.append(f"④ 第{i}根 {key} 回退不一致")
                print(f"[FAIL] ④ 状态缺失回退: 第{i}根 {key}")
                return
    if state is None:
        failures.append("④ 回退后状态仍为 None")
        print("[FAIL] ④ 状态缺失回退: 返回状态 None")
        return
    print("[PASS] ④ 状态缺失回退: 回退全量重算且返回 EMA 状态")


def test_snapshot_incremental_consistency(failures):
    """③ 快照同构：增量路径与全量路径 klines/meta.kline_count 一致"""
    from datetime import datetime
    base = datetime(2025, 6, 2, 9, 30, 0).timestamp()
    fmt = "%Y/%m/%d %H:%M:%S"

    # 构造 40 根K线的 chan
    klus = []
    for i in range(40):
        ts = base + 15 * i
        klus.append(_klu(ts, c=100.0 + i * 0.3, vol=10))
    kl_list = MockKlList(klus)

    class MockChan(dict):
        def __init__(self):
            super().__init__()
            self._kl = kl_list
        def __getitem__(self, key):
            return self._kl

    chan = MockChan()
    kl_type = ("kl",)

    # 全量路径
    full = _extract_realtime_snapshot(chan, kl_type, "SYM", "名称", "15s")
    # 增量路径：先取前 39 根，再增量到 40
    prev_klus = klus[:39]
    prev_kl_list = MockKlList(prev_klus)

    class MockChanPrev(dict):
        def __init__(self):
            super().__init__()
            self._kl = prev_kl_list
        def __getitem__(self, key):
            return self._kl

    chan_prev = MockChanPrev()
    prev_snap = _extract_realtime_snapshot(chan_prev, kl_type, "SYM", "名称", "15s")
    # 模拟调用方追加预览bar
    _ex = prev_snap["klines"]
    _ex.append(_preview_bar(_fmt_ts(base + 15 * 39)))

    inc = _extract_realtime_snapshot(
        chan, kl_type, "SYM", "名称", "15s",
        prev_klines=prev_snap["klines"],
        prev_ema_state=prev_snap["meta"].get("_ema_state"))

    if len(full["klines"]) != len(inc["klines"]):
        failures.append(f"③ klines 数量: 全量={len(full['klines'])} 增量={len(inc['klines'])}")
        print(f"[FAIL] ③ 快照同构: klines 数量 {len(full['klines'])} != {len(inc['klines'])}")
        return
    for i in range(len(full["klines"])):
        for key in ("date", "open", "high", "low", "close", "vol", "amount", "dif", "dea", "macd"):
            if full["klines"][i].get(key) != inc["klines"][i].get(key):
                failures.append(f"③ 第{i}根 {key}: 全量={full['klines'][i].get(key)} 增量={inc['klines'][i].get(key)}")
                print(f"[FAIL] ③ 快照同构: 第{i}根 {key} 不一致")
                return
    if full["meta"]["kline_count"] != inc["meta"]["kline_count"]:
        failures.append(f"③ kline_count: {full['meta']['kline_count']} != {inc['meta']['kline_count']}")
        print("[FAIL] ③ 快照同构: kline_count 不一致")
        return
    print("[PASS] ③ 快照同构: 增量路径与全量路径 klines/MACD/kline_count 逐位一致")


def main():
    ap = argparse.ArgumentParser(description="P2-4 增量快照守护用例")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    args = ap.parse_args()

    failures = []
    test_incremental_klines(failures)
    test_macd_incremental_equals_full(failures)
    test_macd_state_missing_fallback(failures)
    test_snapshot_incremental_consistency(failures)

    print()
    if failures:
        print(f"===== P2-4 增量快照: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== P2-4 增量快照: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

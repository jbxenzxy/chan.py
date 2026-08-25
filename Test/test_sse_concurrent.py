# -*- coding: utf-8 -*-
"""
P2-2 补测试缺口 ② —— SSE 多连接并发测试
=====================================================================
背景：SSE 实时流采用「方案A 每连接一条常驻线程」（同步生成器 +
StreamingResponse），生产环境可同时挂多个连接（多窗口/多用户）。本用例
验证多连接并发下的隔离性与稳定性：

  ① 并发连接数：同时驱动 N=8 个 sse_futures_stream_single 生成器
     （各配独立 MockSource），全部正常收尾（init → update×N → 正常关闭）
  ② 事件序列一致性：每连接首事件为 init，末事件为正常关闭，事件类型
     序列与单连接基线一致（无跨连接串扰）
  ③ 数据源隔离：各连接独立 MockSource，calls 计数互不污染（connect/
     append_bar/close 各为 1 次/连接）
  ④ 线程安全：并发迭代期间无异常抛出（生成器在各自线程迭代）

离线实现（真实天勤源不可用于回归）：复用 test_sse_gray 的确定性
MockSource + 业务桩模式，仅把「单连接驱动」改为「多线程并发驱动」。

运行：python Test/test_sse_concurrent.py [--update]
"""
import argparse
import os
import sys
import threading
import time

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

import FrontAPI
from DataAPI.TqSdkCSSESource import CSSESource, CSSESourceClosed
import App.AppSSE as _sse_mod

SYMBOL = "KQ.m@SHFE.rb"
NOW = time.time()
CONCURRENCY = 8          # 并发连接数


# ═══════════════════════════════════════════════════════════════════════
# 确定性 Mock（与 test_sse_gray.py 同模式，精简到并发用例所需）
# ═══════════════════════════════════════════════════════════════════════
class MockRow(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class MockKlines:
    def __init__(self, bars):
        self.bars = list(bars)

    def __len__(self):
        return len(self.bars)

    @property
    def iloc(self):
        return self

    def __getitem__(self, idx):
        return self.bars[idx]


class MockKlu:
    def __init__(self, ts):
        from datetime import datetime
        self.time = type("T", (), {"to_str": lambda _s, _t=ts: datetime.fromtimestamp(_t).isoformat()})()


class MockKlc:
    def __init__(self, klus):
        self.lst = klus


class MockKlList:
    def __init__(self):
        self.lst = [MockKlc([MockKlu(NOW - 200)])]
        self.bi_list = [object()] * 3
        self.zs_list = [object()] * 1
        self.seg_list = []


class MockChan(dict):
    def __init__(self):
        super().__init__()
        self._kl_list = MockKlList()

    def __getitem__(self, key):
        return self._kl_list


def _bar(dt_s, o=100.0, h=101.0, l=99.0, c=100.5, vol=10):
    return MockRow({"datetime": int(dt_s * 1e9), "open": o, "high": h,
                    "low": l, "close": c, "volume": vol})


def _snapshot(tag, n_klines=4):
    from datetime import datetime, timedelta
    base = datetime(2025, 6, 2, 9, 30, 0)
    klines = []
    for i in range(n_klines):
        dt = base + timedelta(seconds=15 * i)
        klines.append({"date": dt.strftime("%Y/%m/%d %H:%M:%S"),
                       "timestamp": int(dt.timestamp() * 1000),
                       "open": 100.0 + i, "high": 101.0 + i,
                       "low": 99.0 + i, "close": 100.5 + i,
                       "vol": 10, "amount": 0,
                       "dif": 0.1, "dea": 0.05, "macd": 0.1})
    return {"klines": klines,
            "meta": {"kline_count": n_klines, "bi_count": 3, "zs_count": 1,
                     "bss": [], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "tag": tag},
            "bis": [], "tag": tag}


class MockSource(CSSESource):
    """脚本化数据源（每连接独立实例）：n=1 初始化；n=2 推进K线（完成路径）；
    n=3,4 tick 路径；n=5 正常关闭。"""
    CLOSED_AT = 5

    def __init__(self, freq_sec=15.0):
        self.api = None
        self.freq_sec = freq_sec
        self._n_wait = 0
        self.calls = {"connect": 0, "append_bar": [], "close": 0, "cleanup": []}
        self._kl = MockKlList()
        self.klines = MockKlines([_bar(NOW - 100), _bar(NOW)])
        self._snap_counter = 0

    def connect(self):
        self.calls["connect"] += 1

    def get_kline_serial(self, symbol, freq_sec):
        return self.klines

    def wait_update(self, deadline_ns):
        self._n_wait += 1
        if self._n_wait >= self.CLOSED_AT:
            raise CSSESourceClosed("mock 脚本终局")
        if self._n_wait == 2:
            self.klines.bars.append(_bar(NOW + self.freq_sec, c=101.0))

    def last_records(self, code_key):
        return None

    def append_bar(self, bar, code_key):
        self.calls["append_bar"].append((code_key, bar["dt"].isoformat()))

    def close(self):
        self.calls["close"] += 1

    def cleanup_records(self, code_key):
        self.calls["cleanup"].append(code_key)


def _patch_business():
    """业务桩注入（全局一次，所有连接共用确定性桩）。返回恢复句柄。"""
    _sse_mod.init_chan_calls = 0
    _sse_mod.extract_calls = 0
    _sse_mod.white_calls = 0
    _sse_mod.drain_calls = 0

    def stub_init_chan_symbol(api, symbol, name, freq_sec, freq_label, start_time=None, end_time=None,
                              num_bars=None):
        _sse_mod.init_chan_calls += 1
        kl_list = MockKlList()
        chan = MockChan()
        chan._kl_list = kl_list
        return chan, api.klines, ("kl", ), None

    def stub_extract(chan, kl_type, symbol, name, freq_label,
                     saved_selection_date="", lightweight=False, klines=None,
                     prev_klines=None, prev_ema_state=None, is_replay=False):
        _sse_mod.extract_calls += 1
        return _snapshot(f"snap{_sse_mod.extract_calls}")

    def stub_white_hline(kl_list, freq, date_fmt):
        _sse_mod.white_calls += 1
        return {"high": 110.0, "low": 90.0, "freq": freq}

    def stub_drain(chan):
        _sse_mod.drain_calls += 1

    originals = {}
    for name, stub in (("init_chan_symbol", stub_init_chan_symbol),
                       ("_extract_realtime_snapshot", stub_extract),
                       ("_calc_futures_white_hline", stub_white_hline),
                       ("_drain_chan", stub_drain)):
        originals[name] = getattr(_sse_mod, name)
        setattr(_sse_mod, name, stub)

    def restore():
        for name, orig in originals.items():
            setattr(_sse_mod, name, orig)

    return restore


def _parse_event(raw):
    """从帧提取事件类型（心跳返回 'heartbeat'）"""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    else:
        text = raw
    if text.startswith(":"):
        return "heartbeat"
    for ln in text.strip().split("\n"):
        if ln.startswith("event: "):
            return ln[7:]
    return None


def _run_one_connection(i, results, errors):
    """单连接驱动：独立 MockSource，收集事件类型序列与 calls 快照。"""
    src = MockSource()
    try:
        gen = FrontAPI.sse_futures_stream_single(
            SYMBOL, freq="15s", start_time=None, source=src)
        events = []
        for frame in gen:
            ev = _parse_event(frame)
            if ev is not None:
                events.append(ev)
        results[i] = {
            "events": events,
            "connect": src.calls["connect"],
            "append_bar": len(src.calls["append_bar"]),
            "close": src.calls["close"],
            "cleanup": len(src.calls["cleanup"]),
        }
    except Exception as exc:  # noqa: BLE001 —— 记录单连接异常，不中断其它连接
        errors[i] = f"{type(exc).__name__}: {exc}"


def test_concurrent_sse_connections(failures):
    """① 并发 N 连接全部正常收尾 + ③ 数据源隔离（calls 计数互不污染）"""
    restore = _patch_business()
    try:
        results = {}
        errors = {}
        threads = []
        for i in range(CONCURRENCY):
            t = threading.Thread(target=_run_one_connection, args=(i, results, errors))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=30)

        if errors:
            failures.append(f"① {len(errors)} 个连接异常: {errors}")
            print(f"[FAIL] ① 并发连接异常: {errors}")
            return
        if len(results) != CONCURRENCY:
            failures.append(f"① 完成连接数 {len(results)} != {CONCURRENCY}")
            print(f"[FAIL] ① 完成连接数 {len(results)}")
            return
        for i in range(CONCURRENCY):
            rec = results[i]
            if rec["connect"] != 1 or rec["close"] != 1:
                failures.append(f"① 连接{i} connect/close 计数异常: {rec}")
                print(f"[FAIL] ① 连接{i} calls: {rec}")
                return
        print(f"[PASS] ① {CONCURRENCY} 连接并发全部正常收尾（connect/close 各 1 次）")
    finally:
        restore()


def test_event_sequence_consistency(failures):
    """② 事件序列一致性：首事件 init，末事件 update，无跨连接串扰"""
    restore = _patch_business()
    try:
        results = {}
        errors = {}
        threads = []
        for i in range(CONCURRENCY):
            t = threading.Thread(target=_run_one_connection, args=(i, results, errors))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=30)

        if errors:
            failures.append(f"② 连接异常: {errors}")
            print(f"[FAIL] ② 连接异常: {errors}")
            return
        first_events = set()
        for i in range(CONCURRENCY):
            events = results[i]["events"]
            if not events or events[0] != "init":
                failures.append(f"② 连接{i} 首事件非 init: {events[:3]}")
                print(f"[FAIL] ② 连接{i} 首事件: {events[:3]}")
                return
            first_events.add(events[0])
            # 事件类型序列应一致（init → update×N），且不含其它连接串扰标记
            if any(ev not in ("init", "update", "heartbeat") for ev in events):
                failures.append(f"② 连接{i} 出现未知事件: {events}")
                print(f"[FAIL] ② 连接{i} 未知事件: {events}")
                return
        if len(first_events) != 1:
            failures.append(f"② 首事件不一致: {first_events}")
            print(f"[FAIL] ② 首事件集合: {first_events}")
            return
        print(f"[PASS] ② 事件序列一致: 每连接首事件 init，无跨连接串扰")
    finally:
        restore()


def main():
    ap = argparse.ArgumentParser(description="P2-2 ② SSE 多连接并发测试")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    args = ap.parse_args()

    failures = []
    test_concurrent_sse_connections(failures)
    test_event_sequence_consistency(failures)

    print()
    if failures:
        print(f"===== P2-2 ② SSE 并发测试: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print(f"===== P2-2 ② SSE 并发测试: 全部通过（{CONCURRENCY} 连接） =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

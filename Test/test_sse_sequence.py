# -*- coding: utf-8 -*-
"""
阶段 2.5：SSE 事件序列快照用例（3b-2 后改为 native 生成器回归）
=====================================================================
设计文档 8.4：「为两个 SSE 端点增加事件序列快照用例（首事件类型 /
事件间隔 / 正常关闭行为），为阶段 3b 的 SSE 接口形态重写提供回归基线」。

口径遵循 V10 灰度比对修正：事件类型序列 + 事件总数（统计性核对），
不做逐事件内容比对（内容比对属阶段 3b 灰度验证，输入固定历史区间）。

3b-2 拆除 legacy 桥接后，本用例被测对象改为 native 原生异步生成器
（sse_futures_stream_single/dual + CSSESource 数据源抽象）：
  - 首事件类型：init（含失败载荷）→ update（tick/K线完成路径）→ 心跳
  - 正常关闭：数据源抛 CSSESourceClosed → 生成器正常耗尽
  - 异常路径：init 初始化失败（init_chan 返回 None）→ init 事件带 error
    载荷并正常关闭（wait_update 运行时异常按设计重试，不产出 error 帧）

真实实时数据源（天勤）不可离线复现，故用确定性 MockSource 驱动。

运行：python Test/test_sse_sequence.py [--update]
"""
import argparse
import io
import json
import os
import sys
import time
import traceback

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

SNAPSHOT = os.path.join(TEST_DIR, "snapshots", "sse_event_sequences.json")

SYMBOL = "KQ.m@SHFE.rb"
NOW = time.time()


# ── 确定性 Mock 对象（与 test_sse_gray.py 同模式）────────────────────
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
    return {
        "klines": klines,
        "meta": {"kline_count": n_klines, "bi_count": 3, "zs_count": 1,
                 "bss": [], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "tag": tag},
        "bis": [],
        "tag": tag,
    }


class MockSource(CSSESource):
    """确定性脚本驱动：wait_update 第 n 次 → 推进K线（完成路径）→ 正常关闭。

    彻底解耦业务后仅承载数据源面（connect / get_kline_serial / wait_update /
    last_records / append_bar / close / cleanup_records）；业务确定性桩见
    _patch_business（后台 monkeypatch AppSSE 模块级业务函数）。
    """
    CLOSED_AT = 4

    def __init__(self, fail_init=False):
        self.api = None
        self.freq_sec = 15.0
        self._n_wait = 0
        self.fail_init = fail_init
        self.calls = {"connect": 0, "close": 0, "cleanup": []}
        self.klines = MockKlines([_bar(NOW - 100), _bar(NOW)])

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
        pass

    def close(self):
        self.calls["close"] += 1

    def cleanup_records(self, code_key):
        self.calls["cleanup"].append(code_key)


def _patch_business(src, fail_init=False):
    """把驱动器依赖的服务层业务函数替换为确定性桩（彻底解耦业务后 MockSource
    不再承载 init_chan/extract/white_hline/step_load；业务在 AppSSE，测试以
    monkeypatch 注入确定性行为并统计调用次数）。返回恢复句柄。"""
    _sse_mod.init_chan_calls = 0
    _sse_mod.extract_calls = 0
    _sse_mod.white_calls = 0
    _sse_mod.drain_calls = 0

    def stub_init_chan_symbol(api, symbol, name, freq_sec, freq_label, start_time=None, end_time=None,
                              num_bars=None):
        _sse_mod.init_chan_calls += 1
        if fail_init:
            return None
        chan = MockChan()
        return chan, src.klines, ("kl",), None

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


# ── 采集与断言 ───────────────────────────────────────────────────────
def _collect(gen):
    frames = []
    for frame in gen:
        frames.append(frame)
    return frames


def parse_frame(raw):
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    if text.startswith(":"):
        return "heartbeat", None
    lines = text.strip().split("\n")
    event = data = None
    for ln in lines:
        if ln.startswith("event: "):
            event = ln[7:]
        elif ln.startswith("data: "):
            data = ln[6:]
    return event, json.loads(data) if data is not None else None


def _sequence_summary(events):
    """V10 口径：事件类型序列 + 事件总数（剥离 data 内容）"""
    typed = [e for e, _ in events if e != "heartbeat"]
    return {
        "first_event": typed[0] if typed else None,
        "event_sequence": typed,
        "event_count": len(typed),
        "heartbeat_count": sum(1 for e, _ in events if e == "heartbeat"),
    }


def run_case(kind, fail_init=False):
    src = MockSource(fail_init=fail_init)
    restore = _patch_business(src, fail_init=fail_init)
    try:
        if kind == "single":
            gen = FrontAPI.sse_futures_stream_single(
                SYMBOL, freq="15s", start_time=None, source=src)
        else:
            gen = FrontAPI.sse_futures_stream_dual(
                SYMBOL, "1m", "15s", start_time=None, source=src)
        frames = _collect(gen)
    finally:
        restore()
    return frames, src


def main():
    ap = argparse.ArgumentParser(description="SSE 事件序列快照（native 生成器）")
    ap.add_argument("--update", action="store_true",
                    help="重冻事件序列基线（协议变更经确认后使用）")
    args = ap.parse_args()

    failures = []
    summary = {}

    cases = [
        ("single", dict(kind="single")),
        ("dual", dict(kind="dual")),
        ("error", dict(kind="single", fail_init=True)),
    ]
    for name, kw in cases:
        try:
            frames, src = run_case(**kw)
        except Exception:
            print(f"[FAIL] {name} 驱动异常:")
            traceback.print_exc()
            failures.append(f"{name}: 生成器驱动抛异常")
            continue

        events = []
        try:
            for raw in frames:
                events.append(parse_frame(raw))
        except Exception as e:
            failures.append(f"{name}: 帧解析失败 {e}")
            print(f"[FAIL] {name} 帧解析: {e}")
            continue

        s = _sequence_summary(events)

        # 首事件类型
        expected_first = {"single": "init", "dual": "init", "error": "init"}[name]
        if s["first_event"] != expected_first:
            failures.append(f"{name}: 首事件类型 {s['first_event']} != {expected_first}")
            print(f"[FAIL] {name} 首事件: {s['first_event']}")
        else:
            print(f"[PASS] {name} 首事件类型: {s['first_event']}")

        # 正常关闭行为（for 正常耗尽 = 正常关闭）
        print(f"[PASS] {name} 正常关闭: 生成器正常耗尽（{len(frames)} 帧, "
              f"心跳 {s['heartbeat_count']} 帧）")

        # 异常路径专属：init 失败载荷（error 键）+ 无后续事件
        if name == "error":
            ev, payload = events[0] if events else (None, None)
            if ev != "init" or not isinstance(payload, dict) or "error" not in payload:
                failures.append(f"error 用例 init 载荷缺 error 键: {payload}")
                print(f"[FAIL] error 路径: init 载荷 {payload}")
            else:
                print("[PASS] error 路径: init 事件带 error 载荷并正常关闭")

        # 收尾清理（SELF_CONTAINED 协议）
        if src.calls["close"] != 1:
            failures.append(f"{name}: close() 调用 {src.calls['close']} 次（应 1）")
        n_clean = len(src.calls["cleanup"])
        expect_clean = 2 if name == "dual" else 1
        if n_clean != expect_clean:
            failures.append(f"{name}: cleanup_records {n_clean} 次（应 {expect_clean}）")
        if src.calls["close"] == 1 and n_clean == expect_clean:
            print(f"[PASS] {name} 收尾: close×1 + cleanup×{n_clean}")

        summary[name] = s

    # ── 冻结 / 比对事件序列快照 ──
    from Test import comparator
    if args.update or not os.path.exists(SNAPSHOT):
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with io.open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"\n[{'UPDATED' if args.update else 'FROZEN'}] SSE 事件序列基线: "
              f"{json.dumps({k: v['event_sequence'] for k, v in summary.items()}, ensure_ascii=False)}")
    else:
        with io.open(SNAPSHOT, encoding="utf-8") as f:
            expected = json.load(f)
        ok, diff = comparator.compare(expected, summary, path="$.sse")
        if ok:
            print("[PASS] SSE 事件序列快照: 类型序列/首事件/事件总数 一致")
        else:
            failures.append(f"SSE 序列漂移: {diff}")
            print(f"[FAIL] SSE 事件序列快照:\n{diff}")

    print()
    if failures:
        print(f"===== SSE 事件序列: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== SSE 事件序列: 全部通过（native 生成器） =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

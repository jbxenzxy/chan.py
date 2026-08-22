# -*- coding: utf-8 -*-
"""
阶段 3b-1：SSE 灰度比对用例（native 原生生成器 vs 冻结基线 + legacy 形态等价）
=====================================================================
设计文档 8.6-3b 灰度比对口径（V10）：
  ① 事件类型序列逐个比对 type 字段顺序；
  ② 数据内容剥离时间戳 / 实时价字段后比对结构与笔/段/中枢计数；
  ③ 间隔仅做统计性核对（事件总数一致即可，不做逐事件比对）。

离线实现（真实天勤源不可用于回归）：
  - MockSource（CSSESource 子类）注入 FrontAPI.sse_futures_stream_single/dual，
    以确定性脚本驱动无限循环协议：连接 → init → tick×N → K线完成×1 →
    tick×N → CSSESourceClosed 正常收尾；
  - 壁钟确定性：过期K线（dt = T-100s）触发完成路径，新鲜K线（dt = T）
    触发 tick 路径，迭代内零 sleep，脚本步进完全确定；
  - legacy 形态等价：native 每帧字节与 FrontAPI._sse_frame 重编码逐字节
    一致（= 遗留 ChartHandler 的 `event: X\\ndata: {json}\\n\\n` 格式）；
  - 收尾断言：close()/cleanup_records() 逐窗调用，双窗口弹出期货缓存。

基线冻结：snapshots/sse_gray_native.json（--update 重冻）。
运行：python Test/test_sse_gray.py [--update]
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
from FrontAPI import _sse_frame
from DataAPI.TqSdkCSSESource import CSSESource, CSSESourceClosed
import App.AppSSE as _sse_mod

SNAPSHOT = os.path.join(TEST_DIR, "snapshots", "sse_gray_native.json")

SYMBOL = "KQ.m@SHFE.rb"
NOW = time.time()           # mock 时间基准（构建时确定）


# ═══════════════════════════════════════════════════════════════════════
# 确定性 Mock 对象（klines / chan / 快照）
# ═══════════════════════════════════════════════════════════════════════
class MockRow(dict):
    """模拟 pandas 行：.get() 语义与天勤 klines 一致"""
    def get(self, key, default=None):
        return dict.get(self, key, default)


class MockKlines:
    """可切换当前K线的 klines 替身：iloc[-1]/iloc[-2] + len()"""
    def __init__(self, bars):
        self.bars = list(bars)      # [MockRow, ...]

    def __len__(self):
        return len(self.bars)

    @property
    def iloc(self):
        return self

    def __getitem__(self, idx):
        if idx < 0:
            return self.bars[idx]
        return self.bars[idx]


class MockKlu:
    """chan 末根 K 线（DIAG 用）：.time.to_str()"""
    def __init__(self, ts):
        from datetime import datetime
        self.time = type("T", (), {"to_str": lambda _s, _t=ts: datetime.fromtimestamp(_t).isoformat()})()


class MockKlc:
    def __init__(self, klus):
        self.lst = klus


class MockKlList:
    def __init__(self):
        self.lst = [MockKlc([MockKlu(NOW - 200)])]
        self.bi_list = [object()] * 3      # 3 笔（结构计数用）
        self.zs_list = [object()] * 1      # 1 中枢


class MockChan(dict):
    """chan 替身：chan[kl_type] → kl_list"""
    def __init__(self):
        super().__init__({("kl", ): MockKlList()})

    def __getitem__(self, key):
        return self._kl_list

    def __init_subclass__(cls):
        pass


def _bar(dt_s, o=100.0, h=101.0, l=99.0, c=100.5, vol=10):
    return MockRow({"datetime": int(dt_s * 1e9), "open": o, "high": h,
                    "low": l, "close": c, "volume": vol})


def _snapshot(tag, n_klines=4):
    """确定性快照：date/timestamp 含壁钟成分（口径②冻结时剥离）"""
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


# ═══════════════════════════════════════════════════════════════════════
# MockSource：脚本化驱动 SSE 协议
# ═══════════════════════════════════════════════════════════════════════
class MockSource(CSSESource):
    """灰度注入数据源（锁分类同 SELF_CONTAINED：不触共享分析缓存）

    彻底解耦业务后仅承载数据源面（connect / get_kline_serial / wait_update /
    last_records / append_bar / close / cleanup_records）；init_chan/extract/
    white_hline/step_load 已下沉服务层 AppSSE，业务确定性桩见 _patch_business。

    脚本（wait_update 第 n 次调用；klines 原地变更——真实天勤的
    get_kline_serial 返回同一 DataFrame 引用，生成器持有的引用随行情更新）：
      n=1 → 返回（循环初始化 last_bar，无事件）
      n=2 → klines 原地推进一根（klines 推进 → K线完成路径：
            append_bar + _drain_chan + 全量快照 update）
      n=3,4 → 返回（当前K线新鲜 → tick 路径 ×2）
      n=5 → 抛 CSSESourceClosed → 正常收尾
    """
    CLOSED_AT = 5                 # 第 5 次抛正常关闭

    def __init__(self, freq_sec=15.0):
        self.api = None
        self.freq_sec = freq_sec
        self._n_wait = 0
        self.calls = {"connect": 0, "append_bar": [], "close": 0,
                      "cleanup": []}
        self._kl = MockKlList()
        # 初始：一根过期K线（iloc[-2]，仅占位）+ 当前形成中K线（新鲜）
        self.klines = MockKlines([_bar(NOW - 100), _bar(NOW)])
        self._snap_counter = 0

    # ── CSSESource 数据源面协议 ──
    def connect(self):
        self.calls["connect"] += 1

    def get_kline_serial(self, symbol, freq_sec):
        return self.klines

    def wait_update(self, deadline_ns):
        self._n_wait += 1
        if self._n_wait >= self.CLOSED_AT:
            raise CSSESourceClosed("mock 脚本终局")
        if self._n_wait == 2:
            # 原地推进一根（模拟天勤新K线开bar）：iloc[-1] 切到未来时间戳，
            # iloc[-2]（原当前K线）冻结 → 触发 K线完成路径
            self.klines.bars.append(_bar(NOW + self.freq_sec, c=101.0))

    def last_records(self, code_key):
        return None

    def append_bar(self, bar, code_key):
        self.calls["append_bar"].append((code_key, bar["dt"].isoformat()))

    def close(self):
        self.calls["close"] += 1

    def cleanup_records(self, code_key):
        self.calls["cleanup"].append(code_key)


def _patch_business(src):
    """把驱动器依赖的服务层业务函数替换为确定性桩（彻底解耦业务后 MockSource
    不再承载 init_chan/extract/white_hline/step_load；业务在 AppSSE，测试以
    monkeypatch 注入确定性行为并统计调用次数）。返回恢复句柄。"""
    _sse_mod.init_chan_calls = 0
    _sse_mod.extract_calls = 0
    _sse_mod.white_calls = 0
    _sse_mod.drain_calls = 0

    def stub_init_chan_symbol(api, symbol, name, freq_sec, freq_label, start_time=None):
        _sse_mod.init_chan_calls += 1
        kl_list = MockKlList()
        chan = MockChan()
        chan._kl_list = kl_list
        # 返回 (chan, klines, kl_type, records)：单窗口用 klines，双窗口忽略
        return chan, src.klines, ("kl", ), None

    def stub_extract(chan, kl_type, symbol, name, freq_label,
                     saved_selection_date="", lightweight=False, klines=None,
                     prev_klines=None, prev_ema_state=None):
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


# ═══════════════════════════════════════════════════════════════════════
# 采集与断言
# ═══════════════════════════════════════════════════════════════════════
def _collect(gen):
    frames = []
    try:
        for frame in gen:
            frames.append(frame)
    finally:
        pass
    return frames


def parse_frame(raw):
    """解析一帧 → (event|None(心跳), payload|None)；校验字节形态与 legacy 一致"""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    else:
        text = raw
    if text.startswith(":"):
        assert text == ": heartbeat\n\n", f"心跳帧格式异常: {text!r}"
        return "heartbeat", None
    lines = text.strip().split("\n")
    event = data = None
    for ln in lines:
        if ln.startswith("event: "):
            event = ln[7:]
        elif ln.startswith("data: "):
            data = ln[6:]
    assert event and data is not None and text.endswith("\n\n"), \
        f"帧格式与 legacy 不符: {text!r}"
    payload = json.loads(data)
    # ② 形态等价：重编码逐字节一致（_sse_frame 与遗留 write() 同格式）
    assert _sse_frame(event, payload) == raw.encode("utf-8") if isinstance(raw, str) else _sse_frame(event, payload) == raw, \
        "帧重编码与 legacy _sse_frame 不一致"
    return event, payload


def _strip_volatile(obj):
    """口径②：剥离时间戳/实时价等随壁钟漂移字段"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("date", "timestamp", "generated_at", "time"):
                continue
            if k in ("open", "high", "low", "close") and isinstance(v, (int, float)):
                continue      # 实时价字段（tick 更新对象波动）
            out[k] = _strip_volatile(v)
        return out
    if isinstance(obj, list):
        return [_strip_volatile(x) for x in obj]
    return obj


def run_case(kind):
    """驱动同步生成器，返回 (frames, source.calls)；业务桩经 _patch_business 注入"""
    src = MockSource()
    restore = _patch_business(src)
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
    ap = argparse.ArgumentParser(description="阶段 3b-1 SSE 灰度比对")
    ap.add_argument("--update", action="store_true",
                    help="重冻 native 基线（native 协议变更经确认后使用）")
    args = ap.parse_args()

    failures = []
    summary = {}

    for kind in ("single", "dual"):
        try:
            frames, src = run_case(kind)
        except Exception:
            print(f"[FAIL] {kind} 驱动异常:")
            traceback.print_exc()
            failures.append(f"{kind}: 生成器驱动抛异常")
            continue
        calls = src.calls

        # 解析 + 形态断言
        events = []
        try:
            for raw in frames:
                events.append(parse_frame(raw))
        except AssertionError as e:
            failures.append(f"{kind}: {e}")
            print(f"[FAIL] {kind} 帧形态: {e}")
            continue

        seq = [e for e, _ in events]
        typed = [e for e in seq if e != "heartbeat"]

        # ① 首事件类型 + 心跳存在
        if not typed or typed[0] != "init":
            failures.append(f"{kind}: 首事件应为 init，实际 {typed[:1]}")
            print(f"[FAIL] {kind} 首事件: {typed[:1]}")
        else:
            print(f"[PASS] {kind} 首事件: init（共 {len(frames)} 帧，"
                  f"心跳 {seq.count('heartbeat')} 帧）")
        if seq.count("heartbeat") == 0:
            failures.append(f"{kind}: 无心跳注释帧（保活协议缺失）")
            print(f"[FAIL] {kind} 心跳: 缺失")

        # init 载荷结构（口径②：剥离时间戳/实时价后冻结结构）
        init_payload = dict(events[0][1]) if events and events[0][0] == "init" else None
        if init_payload is None:
            failures.append(f"{kind}: init 载荷缺失")
        elif kind == "dual" and ("main" not in init_payload or "sub" not in init_payload):
            failures.append(f"{kind}: init 载荷缺 main/sub 双窗口键")
            print(f"[FAIL] {kind} init 结构: {sorted(init_payload)}")
        else:
            print(f"[PASS] {kind} init 结构: "
                  f"{'main+sub 双窗口' if kind == 'dual' else '单窗口'}")

        # 收尾清理断言（SELF_CONTAINED 收尾协议）
        if calls["close"] != 1:
            failures.append(f"{kind}: close() 调用 {calls['close']} 次（应 1）")
        n_clean = len(calls["cleanup"])
        expect_clean = 2 if kind == "dual" else 1
        if n_clean != expect_clean:
            failures.append(f"{kind}: cleanup_records {n_clean} 次（应 {expect_clean}）")
        if calls["close"] == 1 and n_clean == expect_clean:
            print(f"[PASS] {kind} 收尾: close×1 + cleanup×{n_clean}"
                  f"（{[c for c in calls['cleanup']]}）")

        # K线完成路径确实发生（append_bar + _drain_chan 至少一次）
        if not calls["append_bar"] or _sse_mod.drain_calls < 1:
            failures.append(f"{kind}: K线完成路径未触发"
                            f"（append={len(calls['append_bar'])}, "
                            f"drain={_sse_mod.drain_calls}）")
            print(f"[FAIL] {kind} 完成路径: append={len(calls['append_bar'])} "
                  f"drain={_sse_mod.drain_calls}")
        else:
            print(f"[PASS] {kind} 协议路径: K线完成×{len(calls['append_bar'])} "
                  f"+ tick×{seq.count('update')} 全触发")

        summary[kind] = {
            "typed_sequence": typed,                       # ① 类型序列
            "typed_count": len(typed),                     # ③ 事件总数
            "heartbeat_count": seq.count("heartbeat"),     # ③ 统计性核对
            "init_structure": _strip_volatile(init_payload) if init_payload else None,
        }

    # ── 冻结 / 比对基线 ──
    if args.update or not os.path.exists(SNAPSHOT):
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with io.open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"\n[{'UPDATED' if args.update else 'FROZEN'}] native 基线 → snapshots/sse_gray_native.json")
        print(f"  序列: {json.dumps({k: v['typed_sequence'] for k, v in summary.items()}, ensure_ascii=False)}")
    else:
        with io.open(SNAPSHOT, encoding="utf-8") as f:
            expected = json.load(f)
        for kind, exp in expected.items():
            cur = summary.get(kind)
            if cur is None:
                failures.append(f"{kind}: 本次未产出（驱动失败）")
                continue
            if cur["typed_sequence"] != exp["typed_sequence"]:
                failures.append(f"{kind}: 类型序列漂移 {exp['typed_sequence']} → {cur['typed_sequence']}")
                print(f"[FAIL] {kind} ① 序列: {exp['typed_sequence']} → {cur['typed_sequence']}")
            elif cur["typed_count"] != exp["typed_count"] or cur["heartbeat_count"] != exp["heartbeat_count"]:
                failures.append(f"{kind}: ③ 事件总数漂移 typed={cur['typed_count']}/{exp['typed_count']} "
                                f"hb={cur['heartbeat_count']}/{exp['heartbeat_count']}")
                print(f"[FAIL] {kind} ③ 总数: typed {cur['typed_count']}/{exp['typed_count']}, "
                      f"hb {cur['heartbeat_count']}/{exp['heartbeat_count']}")
            else:
                print(f"[PASS] {kind} ①③ 基线: 序列/总数一致（{cur['typed_count']} 事件 + "
                      f"{cur['heartbeat_count']} 心跳）")
            if json.dumps(cur["init_structure"], sort_keys=True, ensure_ascii=False) != \
               json.dumps(exp["init_structure"], sort_keys=True, ensure_ascii=False):
                failures.append(f"{kind}: ② init 结构漂移（剥离时间戳后）")
                print(f"[FAIL] {kind} ② 结构: init 载荷剥离时间戳后不一致")
            else:
                print(f"[PASS] {kind} ② 结构: init 载荷剥离时间戳/实时价后一致")

    print()
    if failures:
        print(f"===== SSE 灰度比对: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== SSE 灰度比对: 全部通过（native 协议与基线一致 + legacy 帧形态等价）=====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

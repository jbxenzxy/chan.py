# -*- coding: utf-8 -*-
"""
Test/test_futures_replay_window.py —— 期货单窗复盘窗口 [L, R]（二期）
=====================================================================
被测（App/AppSSE.py）：
  1. `_futures_window_fetch_bars` 四种窗口分支（纯函数，无需 mock）：
     A 默认 / B 选点 / C 复盘 / 组合（复盘继承选点，start+end 同传）；
  2. `_sse_single_gen` 的 start_time 继承逻辑（stub init_chan_symbol 捕获参数，
     MockSource 驱动生成器，参考 test_sse_concurrent 的桩模式）：
     - end_time + start_time 缺失 → 从 CSV 恢复（F5 后前端内存丢失，CSV 是 SSOT）；
     - end_time + start_time 显式 → 原样传递（一期旧语义「忽略选点」已删除）；
     - end_time + CSV 空 → start_time=None（默认窗口 [end-N, end]）；
     - 无 end_time + CSV 有 → start_time=CSV（B 操作/冷启动恢复，现状回归保护）；
     - start ≥ end（倒挂）→ 丢弃 start 回退默认窗口（SSE init 事件无弹窗通道，
       回退比报错平滑，前端弹窗已拦用户输入路径）；
  3. meta.saved_selection_date 恒回显 CSV 真值：A 复盘（start=A左 显式传入、
     CSV 空）→ 快照收到空串，start 不冒充选点。

隔离：snapshot_runner.isolate_side_effects 重定向选点 CSV（不碰生产 App/）；
真实天勤数据源用 MockSource（CSSESource 子类）替代，全程离线。

运行：python Test/test_futures_replay_window.py
"""
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

from Test.snapshot_runner import isolate_side_effects

import FrontAPI
from DataAPI.TqSdkCSSESource import CSSESource, CSSESourceClosed
import App.AppSSE as _sse_mod

SYMBOL = "KQ.m@SHFE.rb"
FREQ = "15s"
CSV_POINT = "2025/06/01 09:30:00"   # CSV 选点（_get_saved_point 读取口径）
EXPLICIT_START = "2025/05/01 09:30:00"  # 显式 start_time（前端窗口左边界）
END_TIME = "2025/06/10 15:00:00"


# ═══════════════════════ _futures_window_fetch_bars ═══════════════════

def test_fetch_bars_branches():
    """四种窗口分支的取数根数与 base_bars_out（纯函数）。"""
    f = _sse_mod._futures_window_fetch_bars
    n = 100
    # A 默认：fetch = base = n
    assert f(15, base_bars=n) == (n, n)
    # C 复盘（end 无 start）：fetch = n + 估算(end→now)，base = n
    fetch, base = f(15, end_time=END_TIME, base_bars=n)
    assert base == n and fetch > n, f"C 分支异常: fetch={fetch}, base={base}"
    # 组合（start+end，start<end）：fetch = 估算(start→now)，base=None（跳过末N根截断）
    fetch, base = f(15, start_time=EXPLICIT_START, end_time=END_TIME, base_bars=n)
    assert base is None, f"组合分支 base_bars_out 应为 None，实为 {base}"
    assert fetch > 0, f"组合分支 fetch 异常: {fetch}"
    # 组合倒挂（start≥end）：防御性回退 C 分支
    fetch, base = f(15, start_time=END_TIME, end_time=EXPLICIT_START, base_bars=n)
    assert base == n and fetch > n, f"倒挂回退异常: fetch={fetch}, base={base}"
    print(f"[PASS] fetch_bars 四分支: C fetch={fetch}（倒挂回退）、组合 base=None")


# ═══════════════════════ _sse_single_gen 继承逻辑 ═════════════════════

class MockKlines:
    def __init__(self, bars):
        self.bars = bars

    def __len__(self):
        return len(self.bars)

    @property
    def iloc(self):
        return self

    def __getitem__(self, idx):
        return self.bars[idx]


class MockKlList:
    def __init__(self):
        import time as _t
        self.lst = []
        self.bi_list = []
        self.zs_list = []
        self.seg_list = []
        self._ts = _t.time()


class MockChan(dict):
    def __init__(self):
        super().__init__()
        self._kl_list = MockKlList()

    def __getitem__(self, key):
        return self._kl_list


class MockSource(CSSESource):
    """精简脚本化数据源：init 后第一次 wait_update 即正常关闭。"""

    def __init__(self, freq_sec=15.0):
        import time as _t
        self.api = None
        self.freq_sec = freq_sec
        self._n_wait = 0
        self._ts = _t.time()
        self.klines = MockKlines([
            {"datetime": int(self._ts * 1e9), "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.5, "volume": 10},
        ])

    def connect(self):
        pass

    def get_kline_serial(self, symbol, freq_sec):
        return self.klines

    def wait_update(self, deadline_ns):
        self._n_wait += 1
        if self._n_wait >= 2:
            raise CSSESourceClosed("mock 脚本终局")

    def last_records(self, code_key):
        return None

    def append_bar(self, bar, code_key):
        pass

    def set_data(self, records, symbol=None):
        pass

    def get_data(self, symbol=None, **kwargs):
        return None

    def get_last_n(self, n=1, symbol=None):
        return None

    def clear_all_cache(self):
        pass

    def close(self):
        pass

    def close_api(self):
        pass

    def cleanup_records(self, code_key):
        pass

    def get_kl_data(self):
        return None


def _run_gen(start_time, end_time, csv_point, captured):
    """跑一次 _sse_single_gen（stub 业务桩），捕获 init_chan_symbol 参数与快照 meta。"""
    from datetime import datetime, timedelta
    restore_iso = isolate_side_effects()
    try:
        if csv_point:
            _sse_mod.app_data.save_point_time(SYMBOL, "测试品种", FREQ, csv_point)

        def stub_init(api, symbol, name, freq_sec, freq_label, start_time=None,
                      end_time=None, num_bars=None):
            captured["init"].append({"start_time": start_time, "end_time": end_time})
            chan = MockChan()
            return chan, api.klines, ("kl",), None

        def stub_extract(chan, kl_type, symbol, name, freq_label,
                         saved_selection_date="", lightweight=False, klines=None,
                         prev_klines=None, prev_ema_state=None, is_replay=False):
            captured["extract"].append({"saved": saved_selection_date, "replay": is_replay})
            base = datetime(2025, 6, 2, 9, 30, 0)
            kl = [{"date": (base + timedelta(seconds=15 * i)).strftime("%Y/%m/%d %H:%M:%S"),
                   "timestamp": int(base.timestamp() * 1000), "open": 100.0, "high": 101.0,
                   "low": 99.0, "close": 100.5, "vol": 10, "amount": 0,
                   "dif": 0.1, "dea": 0.05, "macd": 0.1} for i in range(2)]
            return {"klines": kl, "meta": {"kline_count": len(kl), "bi_count": 0,
                                           "zs_count": 0, "bss": []},
                    "bis": []}

        def stub_white(kl_list, freq, date_fmt):
            return None

        def stub_drain(chan):
            pass

        originals = {}
        for name, stub in (("init_chan_symbol", stub_init),
                           ("_extract_realtime_snapshot", stub_extract),
                           ("_calc_futures_white_hline", stub_white),
                           ("_drain_chan", stub_drain)):
            originals[name] = getattr(_sse_mod, name)
            setattr(_sse_mod, name, stub)
        try:
            src = MockSource()
            gen = FrontAPI.sse_futures_stream_single(
                SYMBOL, freq=FREQ, start_time=start_time, end_time=end_time, source=src)
            for _frame in gen:
                pass  # 耗尽（MockSource 两次 wait_update 后正常关闭）
        finally:
            for name, orig in originals.items():
                setattr(_sse_mod, name, orig)
    finally:
        restore_iso()


def test_replay_inherits_csv_point():
    """end_time + start_time 缺失 → 从 CSV 恢复（继承生效）。"""
    captured = {"init": [], "extract": []}
    _run_gen(None, END_TIME, CSV_POINT, captured)
    assert captured["init"], "生成器未到达 init"
    got = captured["init"][0]
    assert got["start_time"] == CSV_POINT, \
        f"复盘应从 CSV 恢复选点 {CSV_POINT!r}，init 收到 start_time={got['start_time']!r}"
    assert got["end_time"] == END_TIME
    print(f"[PASS] 复盘继承 CSV 选点: start={got['start_time']}")


def test_replay_explicit_start_kept():
    """end_time + 显式 start_time → 原样传递（旧「忽略选点」语义已删除）。"""
    captured = {"init": [], "extract": []}
    _run_gen(EXPLICIT_START, END_TIME, None, captured)
    got = captured["init"][0]
    assert got["start_time"] == EXPLICIT_START, \
        f"显式 start_time 被丢弃: {got['start_time']!r}"
    assert got["end_time"] == END_TIME
    # meta 恒回显 CSV 真值：CSV 空 → 快照 saved 为空串（A 复盘 start 不冒充选点）
    assert captured["extract"][0]["saved"] == "", \
        f"CSV 空时 meta.saved_selection_date 应为空，实为 {captured['extract'][0]['saved']!r}"
    assert captured["extract"][0]["replay"] is True
    print(f"[PASS] 显式 start 原样传递 + meta 不冒充选点（CSV 空 → 空）")


def test_replay_no_csv_default_window():
    """end_time + CSV 空 → start_time=None（默认窗口 [end-N, end]）。"""
    captured = {"init": [], "extract": []}
    _run_gen(None, END_TIME, None, captured)
    got = captured["init"][0]
    assert got["start_time"] is None, f"CSV 空时 start_time 应为 None，实为 {got['start_time']!r}"
    print("[PASS] 复盘无选点默认窗口: start=None")


def test_live_csv_restore_regression():
    """无 end_time + CSV 有 → start_time=CSV（B 操作/冷启动恢复，现状回归保护）。"""
    captured = {"init": [], "extract": []}
    _run_gen(None, None, CSV_POINT, captured)
    got = captured["init"][0]
    assert got["start_time"] == CSV_POINT, \
        f"实时路径 CSV 恢复失效: {got['start_time']!r}"
    assert got["end_time"] is None
    # meta 回显 CSV 真值 == start_time（选点态）
    assert captured["extract"][0]["saved"] == CSV_POINT
    print(f"[PASS] 实时路径 CSV 恢复: start={got['start_time']}")


def test_replay_inverted_start_falls_back():
    """start ≥ end（倒挂）→ 丢弃 start 回退默认窗口。"""
    captured = {"init": [], "extract": []}
    _run_gen(END_TIME, EXPLICIT_START, None, captured)  # start=END > end=EXPLICIT
    got = captured["init"][0]
    assert got["start_time"] is None, \
        f"倒挂 start 应回退为 None，实为 {got['start_time']!r}"
    assert got["end_time"] == EXPLICIT_START
    print("[PASS] 倒挂回退默认窗口: start=None")


def test_replay_meta_shows_csv_point():
    """CSV 有选点的复盘：meta.saved_selection_date = CSV 真值（菜单点亮依据）。"""
    captured = {"init": [], "extract": []}
    _run_gen(None, END_TIME, CSV_POINT, captured)
    assert captured["extract"][0]["saved"] == CSV_POINT, \
        f"meta 应回显 CSV 选点，实为 {captured['extract'][0]['saved']!r}"
    print(f"[PASS] 复盘 meta 回显 CSV 选点: {captured['extract'][0]['saved']}")


def main():
    test_fetch_bars_branches()
    test_replay_inherits_csv_point()
    test_replay_explicit_start_kept()
    test_replay_no_csv_default_window()
    test_live_csv_restore_regression()
    test_replay_inverted_start_falls_back()
    test_replay_meta_shows_csv_point()
    print("ALL 期货单窗复盘窗口 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

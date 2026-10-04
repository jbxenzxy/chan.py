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
     - start ≥ end（倒挂）→ **报错中止**本次连接（init 错误帧 → 前端 showAlert），
       不再静默回退；与股票 `_analyze_stock_internal` 同判据 `>=`、同文案，见
       Docs/选点&复盘方案v1.15.md §3.5；
  3. meta.saved_selection_date 恒回显 CSV 真值：A 复盘（start=A左 显式传入、
     CSV 空）→ 快照收到空串，start 不冒充选点；
  4. 期货选点 end_date 透传链（P0 回归）：漏斗层 → RAW 薄壳 → AppSSE 三段
     贯通（薄壳漏收 end_date 会让每次选点 TypeError → REST 500）。

隔离：snapshot_runner.isolate_side_effects 重定向选点 CSV（不碰生产 App/）；
真实天勤数据源用 MockSource（CSSESource 子类）替代，全程离线。

运行：python Test/test_futures_replay_window.py

四期追加（dual gen 窗口解耦）：`_sse_dual_gen` 的 start/sub_start_time 独立——
两窗各自继承各自周期列的选点（CSV 恢复）、显式 sub_start_time 优先、
无选点方式A 交由 init_chan_symbol 按 FUTURES_LOOKBACK_CONFIG[sub_freq] 自算
（折算机制废除）；meta 双字段（main/sub 快照各自回显各自列）。
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
    # 组合倒挂（start≥end）：纯函数无处报错，防御性走 C 分支。gen 层已在更早
    # 处报错中止（见 test_replay_inverted_start_errors），故此分支正常流程不可达。
    fetch, base = f(15, start_time=END_TIME, end_time=EXPLICIT_START, base_bars=n)
    assert base == n and fetch > n, f"倒挂回退异常: fetch={fetch}, base={base}"
    print(f"[PASS] fetch_bars 四分支: C fetch={fetch}（纯函数倒挂防御）、组合 base=None")


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
                captured.setdefault("frames", []).append(_frame)  # 耗尽；倒挂时提前 return
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


def test_replay_inverted_start_errors():
    """start ≥ end（倒挂）→ 报错中止，不静默回退（需求⑼ 兜底归一）。

    断言三件：① 首帧 init 载荷带 error（前端据此 showAlert 告知用户）；
    ② 文案与股票 `_analyze_stock_internal` 同形（「复盘起始时间 … 不早于
    复盘截止时间 …」）；③ `init_chan_symbol` **一次都没被调用** —— 证明
    没有偷偷按默认窗口加载（旧「回退默认窗口」行为已删除）。
    """
    for _label, _start in (("start>end", END_TIME), ("start==end", EXPLICIT_START)):
        captured = {"init": [], "extract": []}
        _run_gen(_start, EXPLICIT_START, None, captured)
        frames = captured.get("frames") or []
        assert frames, f"[{_label}] 生成器未产出任何帧"
        assert b'"error"' in frames[0], f"[{_label}] 倒挂应报 init 错误帧，实为 {frames[0]!r}"
        text = frames[0].decode("utf-8")
        assert "不早于" in text and "复盘起始时间" in text, \
            f"[{_label}] 文案与股票不归一: {text!r}"
        assert not captured["init"], \
            f"[{_label}] 倒挂已报错却仍按默认窗口加载: {captured['init']!r}"
    print("[PASS] 倒挂报错中止: init 错误帧 + 未加载默认窗口（start>end 与 start==end）")


def test_replay_meta_shows_csv_point():
    """CSV 有选点的复盘：meta.saved_selection_date = CSV 真值（菜单点亮依据）。"""
    captured = {"init": [], "extract": []}
    _run_gen(None, END_TIME, CSV_POINT, captured)
    assert captured["extract"][0]["saved"] == CSV_POINT, \
        f"meta 应回显 CSV 选点，实为 {captured['extract'][0]['saved']!r}"
    print(f"[PASS] 复盘 meta 回显 CSV 选点: {captured['extract'][0]['saved']}")


def test_dual_gen_independent_starts():
    """dual gen 双 start 解耦（四期）：上窗/下窗各自继承各自周期列的选点。"""
    captured = {"init": [], "extract": []}

    def _run_dual(csv_main, csv_sub, kw_start=None, kw_sub_start=None):
        restore_iso = isolate_side_effects()
        try:
            if csv_main:
                _sse_mod.app_data.save_point_time(SYMBOL, "测试品种", "1m", csv_main)
            if csv_sub:
                _sse_mod.app_data.save_point_time(SYMBOL, "测试品种", "15s", csv_sub)

            def stub_init(api, symbol, name, freq_sec, freq_label, start_time=None,
                          end_time=None, num_bars=None):
                captured["init"].append({"freq": freq_label, "start": start_time,
                                         "end": end_time, "num_bars": num_bars})
                chan = MockChan()
                return chan, api.klines, ("kl",), None

            def stub_extract(chan, kl_type, symbol, name, freq_label,
                             saved_selection_date="", lightweight=False, klines=None,
                             prev_klines=None, prev_ema_state=None, is_replay=False):
                captured["extract"].append({"freq": freq_label, "saved": saved_selection_date})
                return {"klines": [], "meta": {"kline_count": 0, "bi_count": 0,
                                               "zs_count": 0, "bss": []}, "bis": []}

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
                gen = FrontAPI.sse_futures_stream_dual(
                    SYMBOL, "1m", "15s", start_time=kw_start,
                    sub_start_time=kw_sub_start, end_time=None, source=src)
                for _frame in gen:
                    pass
            finally:
                for name, orig in originals.items():
                    setattr(_sse_mod, name, orig)
        finally:
            restore_iso()

    # 场景 1：两列均有选点、入口无显式 start → 各自继承
    captured = {"init": [], "extract": []}
    _run_dual("2025/05/01 09:30:00", "2025/06/01 09:30:00")
    by_freq = {c["freq"]: c for c in captured["init"]}
    assert by_freq["1m"]["start"] == "2025/05/01 09:30:00", \
        f"上窗应继承 1m 列选点，实为 {by_freq['1m']['start']!r}"
    assert by_freq["15s"]["start"] == "2025/06/01 09:30:00", \
        f"下窗应继承 15s 列选点，实为 {by_freq['15s']['start']!r}"
    assert by_freq["1m"]["num_bars"] is None and by_freq["15s"]["num_bars"] is None, \
        "选点路径 num_bars 应为 None（init 内按墙钟估算）"
    # meta 双字段：两窗快照各自回显各自列
    by_ext = {c["freq"]: c for c in captured["extract"]}
    assert by_ext["1m"]["saved"] == "2025/05/01 09:30:00"
    assert by_ext["15s"]["saved"] == "2025/06/01 09:30:00"
    print("[PASS] dual 双 start 解耦: 两窗各自继承各自周期列选点 + meta 双字段")

    # 场景 2：显式 sub_start_time 优先于 CSV
    captured = {"init": [], "extract": []}
    _run_dual("2025/05/01 09:30:00", "2025/06/01 09:30:00",
              kw_sub_start="2025/07/01 09:30:00")
    by_freq = {c["freq"]: c for c in captured["init"]}
    assert by_freq["15s"]["start"] == "2025/07/01 09:30:00", \
        f"显式 sub_start_time 应优先于 CSV，实为 {by_freq['15s']['start']!r}"
    print("[PASS] dual 显式 sub_start 优先")

    # 场景 3：两列均空 → 双 start 均 None（方式A，各自配置根数由 init 自算）
    captured = {"init": [], "extract": []}
    _run_dual(None, None)
    by_freq = {c["freq"]: c for c in captured["init"]}
    assert by_freq["1m"]["start"] is None and by_freq["15s"]["start"] is None, \
        "无选点时双 start 应为 None（方式A）"
    assert by_freq["1m"]["num_bars"] is None and by_freq["15s"]["num_bars"] is None, \
        "方式A num_bars 应为 None（init 内按 FUTURES_LOOKBACK_CONFIG[sub_freq] 自算）"
    print("[PASS] dual 无选点方式A: 双 start=None，根数交由 init 自算")


def test_select_point_end_date_plumbing():
    """期货选点 end_date 透传链（P0 回归）：漏斗层 → RAW 薄壳 → AppSSE。

    缺陷原型：AppChart.call_futures_manual_select_point 恒以关键字传 end_date，
    而同模块的 RAW 薄壳 futures_manual_select_point 形参只有
    (symbol, freq, bi_idx) ⇒ 每次选点 TypeError → REST 500「手选失败」。
    只断言「签名里有 end_date」不够（漏透传同样 500），故两处都钉。
    """
    import inspect
    from App import AppChart as chart
    from App import AppSSE as sse

    params = inspect.signature(chart.futures_manual_select_point).parameters
    assert "end_date" in params, \
        f"RAW 薄壳 futures_manual_select_point 缺 end_date 形参（漏斗层恒以关键字传入 → TypeError）: {list(params)}"

    orig = sse.futures_manual_select_point
    seen = {}

    def _stub(symbol, freq="15s", bi_idx="0", end_date=None):
        seen.update(symbol=symbol, freq=freq, bi_idx=bi_idx, end_date=end_date)
        return {"ok": True}

    sse.futures_manual_select_point = _stub
    try:
        chart.call_futures_manual_select_point("KQ.m@SHFE.rb", freq="15s",
                                               bi_idx="0", end_date="2026/09/01")
    finally:
        sse.futures_manual_select_point = orig
    assert seen.get("end_date") == "2026/09/01", \
        f"end_date 未透传到 AppSSE（复盘态选点窗口左边界失效）: {seen!r}"
    print("[PASS] 期货选点 end_date 透传: 漏斗层 → RAW 薄壳 → AppSSE 全链贯通")


def test_dual_gen_inverted_start_errors():
    """dual gen 复盘倒挂归一（评审 #5）：**任一窗** start ≥ end 即报错中止。

    对齐双窗越界判定口径「任一窗越界即拦/回」——两窗在同一次连接里落地，
    无法只对一窗报错，故不再「只回退倒挂那一窗」，而是整体中止并给出带
    「上窗 / 下窗」定位的同一文案（前端 showAlert）。断言：init 错误帧 +
    文案定位 + 两窗 `init_chan_symbol` 均未被调用（没有偷偷按默认窗口加载）。
    """
    def _run(end_time, csv_main, csv_sub):
        captured = []
        frames = []
        restore_iso = isolate_side_effects()
        try:
            if csv_main:
                _sse_mod.app_data.save_point_time(SYMBOL, "测试品种", "1m", csv_main)
            if csv_sub:
                _sse_mod.app_data.save_point_time(SYMBOL, "测试品种", "15s", csv_sub)

            def stub_init(api, symbol, name, freq_sec, freq_label, start_time=None,
                          end_time=None, num_bars=None):
                captured.append({"freq": freq_label, "start": start_time})
                return MockChan(), api.klines, ("kl",), None

            def stub_extract(chan, kl_type, symbol, name, freq_label,
                             saved_selection_date="", lightweight=False, klines=None,
                             prev_klines=None, prev_ema_state=None, is_replay=False):
                return {"klines": [], "meta": {"kline_count": 0, "bi_count": 0,
                                               "zs_count": 0, "bss": []}, "bis": []}

            def stub_white(kl_list, freq, date_fmt):
                return None

            def stub_drain(chan):
                pass

            originals = {}
            for _n, _s in (("init_chan_symbol", stub_init),
                           ("_extract_realtime_snapshot", stub_extract),
                           ("_calc_futures_white_hline", stub_white),
                           ("_drain_chan", stub_drain)):
                originals[_n] = getattr(_sse_mod, _n)
                setattr(_sse_mod, _n, _s)
            try:
                src = MockSource()
                gen = FrontAPI.sse_futures_stream_dual(
                    SYMBOL, "1m", "15s", start_time=None,
                    sub_start_time=None, end_time=end_time, source=src)
                for _frame in gen:
                    frames.append(_frame)
            finally:
                for _n, _o in originals.items():
                    setattr(_sse_mod, _n, _o)
        finally:
            restore_iso()
        return {c["freq"]: c for c in captured}, frames

    # 场景 1：上窗倒挂（选点晚于复盘点）→ 整体中止，两窗都不加载
    by_freq, frames = _run("2025/07/01 09:30:00", "2025/08/01 09:30:00", "2025/06/01 09:30:00")
    assert frames and b'"error"' in frames[0], f"上窗倒挂应报 init 错误帧，实为 {frames[:1]!r}"
    assert "上窗复盘起始时间" in frames[0].decode("utf-8"), \
        f"文案未定位到上窗: {frames[0]!r}"
    assert not by_freq, f"倒挂已报错却仍加载了窗: {by_freq!r}"
    print("[PASS] dual 复盘倒挂: 上窗倒挂 → 整体中止（两窗均未加载）")

    # 场景 2：只下窗倒挂 → 同样整体中止，文案定位「下窗」
    by_freq, frames = _run("2025/07/01 09:30:00", "2025/06/01 09:30:00", "2025/08/01 09:30:00")
    assert frames and "下窗复盘起始时间" in frames[0].decode("utf-8"), \
        f"文案未定位到下窗: {frames[:1]!r}"
    assert not by_freq, f"下窗倒挂已报错却仍加载了窗: {by_freq!r}"
    print("[PASS] dual 复盘倒挂: 下窗倒挂 → 整体中止（两窗均未加载）")


def test_select_point_rebuild_passes_num_bars():
    """选点重建取数必须显式算根数（评审 #9，防回潮）。

    `fetch_kline` 不传 num_bars 会退回默认配置根数，并在内部被截成末 N 根——
    与「选点后 [T, 最新] 全量不截断」语义相反。该路径在 P0（end_date 形参缺失）
    修复前根本走不到，修好后才成为真实缺陷。
    """
    import ast as _ast
    with open(os.path.join(REPO_ROOT, "App", "AppSSE.py"), encoding="utf-8") as _f:
        _tree = _ast.parse(_f.read())
    _target = None
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.FunctionDef) and _node.name == "futures_manual_select_point":
            _target = _node
            break
    assert _target is not None, "未找到 futures_manual_select_point"
    _calls = []
    for _node in _ast.walk(_target):
        if isinstance(_node, _ast.Call) and isinstance(_node.func, _ast.Attribute) \
                and _node.func.attr == "fetch_kline":
            _calls.append([_kw.arg for _kw in _node.keywords])
    assert _calls, "futures_manual_select_point 内未找到 fetch_kline 调用"
    for _kwargs in _calls:
        assert "num_bars" in _kwargs, \
            f"选点重建 fetch_kline 未传 num_bars（会被截成末 N 根）: kwargs={_kwargs}"
    print("[PASS] 期货选点重建 fetch_kline 显式传 num_bars（不截断）")


def main():
    test_fetch_bars_branches()
    test_replay_inherits_csv_point()
    test_replay_explicit_start_kept()
    test_replay_no_csv_default_window()
    test_live_csv_restore_regression()
    test_replay_inverted_start_errors()
    test_replay_meta_shows_csv_point()
    test_dual_gen_independent_starts()
    test_dual_gen_inverted_start_errors()
    test_select_point_end_date_plumbing()
    test_select_point_rebuild_passes_num_bars()
    print("ALL 期货单窗复盘窗口 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

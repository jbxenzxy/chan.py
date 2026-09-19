# -*- coding: utf-8 -*-
"""
Test/test_lookback_truncation.py —— 股票「K线回看窗口截断」行为回归
=====================================================================
被测：App/AppEngine.py `_analyze_stock_internal` 的**两条同型截断分支**
  A. 复盘路径（end_date 有值 且 未传 start_time）—— 紧随 end_date 过滤之后
  B. 冷启动路径（end_date 为空 且 无 CSV 保存选点）—— 紧随选点判断之后
两条的实现逐字相同：`if not FULL_DATA_MODE and freq in STOCKS_LOOKBACK_CONFIG
→ trunc_bars > 0 and len(records) > trunc_bars → records = records[-trunc_bars:]`。

为什么补这条用例（覆盖缺口的来历）：
  这两条分支此前在门禁内**零执行、零断言**——
    · `Test/snapshot_runner.py` 与 `Test/test_trigger_step_replay.py` 为把
      「K线回看窗口是 AppConfig 运行时配置」这个可变基础设施从被测对象里
      摘出去，采集时把 STOCKS_LOOKBACK_CONFIG **置空**（= 不截断）；
    · `Test/test_15m_period.py` 只断言回看配置的 keys ⊆ 各自市场支持集，
      从不看条数。
  置空本身是对的（旧态绑定宿主机默认值，窗口一变快照就整体漂移，算法变更与
  配置漂移混在一起无法区分），但代价是截断分支被让位成零覆盖：改坏它门禁全绿。
  本用例把这段覆盖补回来，且**不依赖 AppConfig 任何默认值**——窗口由用例自己
  写死，断言「末 N 根」的精确条数与左右边界。

口径（每条都是行为断言，不看实现文本）：
  1. 冷启动 + 显式窗口 → 条数 == 窗口值，末根 == 夹具最新一根，起点 == 倒数第 N 根；
  2. 冷启动 + 空窗口 → 全量条数（不截断）；
  3. 冷启动 + `bars <= 0` → 全量条数（「不限制」旁路）；
  4. 冷启动 + FULL_DATA_MODE=True → 全量条数（全量模式旁路）；
  5. 复盘 + 显式窗口 → 条数 == 窗口值，末根 == 锚点，起点 == 锚点前第 N-1 根；
  6. 复盘 + 空窗口 → 锚点（含）之前的全部条数；
  7. 复盘 + 传 start_time（选点）→ **不做根数截断**（条数 > 窗口值），
     即截断只发生在「无选点」那一支。
其中 1 与 5 同时钉死两条分支都被真正执行（条数恰为窗口值即执行证据）；
2/3/4/6 钉死「截断的不生效条件」；7 钉死截断与选点路径的互斥。

只测 freq="d"：30m/5m 与 d 共用同一实现（同一 if 条件），改实现即红。

依赖：复用 Test/snapshot_runner 的注入设施（打桩数据源 / 副作用隔离 /
确定性参考表），全程离线、不联网（股东减持取数入口被 _seed_reference 打掉）。

运行：python Test/test_lookback_truncation.py
"""
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

import typing
if not hasattr(typing, "Self"):
    import typing_extensions
    typing.Self = typing_extensions.Self

from Test.snapshot_runner import (
    install_data_source, isolate_side_effects, _seed_reference,
)

FIXTURE = "stock_day.json"
CODE = "600519"
TRUNC_BARS = 120           # 显式窗口值：严格小于夹具根数（500），且不等于任何对照条数
REPLAY_ANCHOR_OFFSET = 30  # 复盘锚点：夹具倒数第 30 根
SELECT_OFFSET = 200        # 选点：夹具倒数第 200 根（选点区间条数 > TRUNC_BARS，可区分）


def collect(lookback, full_data_mode=False, **kwargs):
    """在隔离环境中跑一次 `_analyze_stock_internal`，返回 (result, fixture_rows)。

    lookback 直接写进模块级 STOCKS_LOOKBACK_CONFIG（AppEngine 的三处判断都读
    模块全局名），跑完还原——不污染同进程内的后续组件。
    """
    from App import AppEngine as m

    restore_iso = isolate_side_effects()        # 清分析缓存 + 窗口置空 + 选点隔离
    restore_src, rows = install_data_source(FIXTURE)
    restore_ref = _seed_reference()             # 展示性 meta 打桩（含减持，杜绝联网）
    saved_lookback = m.STOCKS_LOOKBACK_CONFIG
    saved_full_mode = m.FULL_DATA_MODE
    m.STOCKS_LOOKBACK_CONFIG = lookback
    m.FULL_DATA_MODE = full_data_mode
    try:
        result = m._analyze_stock_internal(CODE, freq="d", cache_chan=False, **kwargs)
        return result, rows
    finally:
        m.FULL_DATA_MODE = saved_full_mode
        m.STOCKS_LOOKBACK_CONFIG = saved_lookback
        restore_ref()
        restore_src()
        restore_iso()


def kline_dates(result):
    """取输出 K 线日期序列，并交叉校验 meta.kline_count 与明细条数一致。"""
    assert "error" not in result, f"分析返回 error: {result.get('error')}"
    klines = result.get("klines") or []
    meta_count = (result.get("meta") or {}).get("kline_count")
    assert meta_count == len(klines), \
        f"meta.kline_count({meta_count}) 与 klines 明细条数({len(klines)}) 不一致"
    return [k.get("date") for k in klines]


def _fixture_rows():
    from Test.gen_fixtures import load_records
    return load_records(os.path.join(TEST_DIR, "fixtures", FIXTURE))


def _ymd(dt):
    return dt.strftime("%Y/%m/%d")


def test_cold_start_applies_window():
    """冷启动 + 显式窗口：末 N 根，左边界精确到位。"""
    result, rows = collect({"d": (TRUNC_BARS, "用例窗口")})
    dates = kline_dates(result)
    assert len(dates) == TRUNC_BARS, \
        f"冷启动窗口截断失效：{len(dates)} 条（期望 {TRUNC_BARS}）"
    assert dates[-1] == _ymd(rows[-1]["dt"]), "截断改动了末根（应恒为最新一根）"
    assert dates[0] == _ymd(rows[-TRUNC_BARS]["dt"]), \
        f"截断起点错位：{dates[0]}（期望 {_ymd(rows[-TRUNC_BARS]['dt'])}）"
    print(f"[PASS] 冷启动截断: {len(rows)} 条 -> {len(dates)} 条，"
          f"边界 {dates[0]} ~ {dates[-1]}")


def test_cold_start_empty_window_keeps_all():
    """冷启动 + 空窗口（快照/回放入口的口径）：不截断。"""
    result, rows = collect({})
    dates = kline_dates(result)
    assert len(dates) == len(rows), f"空窗口应不截断：{len(dates)} != 夹具 {len(rows)}"
    print(f"[PASS] 冷启动空窗口: 全量 {len(dates)} 条不截断")


def test_cold_start_bars_zero_means_unlimited():
    """冷启动 + bars<=0：语义为「不限制」，跳过截断。"""
    result, rows = collect({"d": (0, "不限制")})
    dates = kline_dates(result)
    assert len(dates) == len(rows), f"bars=0 应视为不限制：{len(dates)} != 夹具 {len(rows)}"
    print(f"[PASS] 冷启动 bars=0: 全量 {len(dates)} 条不截断")


def test_full_data_mode_skips_window():
    """冷启动 + FULL_DATA_MODE=True：窗口旁路。"""
    result, rows = collect({"d": (TRUNC_BARS, "用例窗口")}, full_data_mode=True)
    dates = kline_dates(result)
    assert len(dates) == len(rows), f"全量模式仍截断：{len(dates)} != 夹具 {len(rows)}"
    print(f"[PASS] 全量模式旁路: 全量 {len(dates)} 条不截断")


def test_replay_applies_window():
    """复盘 + 显式窗口：条数 == 窗口值，末根 == 锚点，起点 == 锚点前第 N-1 根。"""
    rows = _fixture_rows()
    anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
    result, _ = collect({"d": (TRUNC_BARS, "用例窗口")}, end_date=anchor)
    dates = kline_dates(result)
    assert len(dates) == TRUNC_BARS, \
        f"复盘窗口截断失效：{len(dates)} 条（期望 {TRUNC_BARS}）"
    assert dates[-1] == anchor, f"复盘末根应 == 锚点 {anchor}，实为 {dates[-1]}"
    idx_anchor = len(rows) - 1 - REPLAY_ANCHOR_OFFSET
    assert dates[0] == _ymd(rows[idx_anchor - (TRUNC_BARS - 1)]["dt"]), \
        f"复盘截断起点错位：{dates[0]}"
    print(f"[PASS] 复盘截断: 锚点 {anchor}，{TRUNC_BARS} 条，边界 {dates[0]} ~ {dates[-1]}")


def test_replay_empty_window_keeps_all_up_to_anchor():
    """复盘 + 空窗口：锚点（含）之前的全部条数。"""
    rows = _fixture_rows()
    anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
    result, _ = collect({}, end_date=anchor)
    dates = kline_dates(result)
    expected = len(rows) - REPLAY_ANCHOR_OFFSET
    assert len(dates) == expected, f"复盘空窗口：{len(dates)} 条（期望 {expected}）"
    print(f"[PASS] 复盘空窗口: 锚点前全量 {len(dates)} 条不截断")


def test_replay_with_start_time_ignores_window():
    """复盘 + 传 start_time（选点）：不做根数截断 —— 截断与选点路径互斥。"""
    rows = _fixture_rows()
    anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
    start = _ymd(rows[-1 - SELECT_OFFSET]["dt"])
    result, _ = collect({"d": (TRUNC_BARS, "用例窗口")}, end_date=anchor, start_time=start)
    dates = kline_dates(result)
    expected = (len(rows) - REPLAY_ANCHOR_OFFSET) - (len(rows) - 1 - SELECT_OFFSET)
    assert len(dates) == expected, \
        f"选点路径被误截断：{len(dates)} 条（期望 {expected}，窗口值 {TRUNC_BARS}）"
    assert len(dates) > TRUNC_BARS, \
        f"选点区间({len(dates)})未超过窗口值({TRUNC_BARS})，本用例失去区分力"
    assert dates[0] == start, f"选点起点错位：{dates[0]} != {start}"
    print(f"[PASS] 复盘选点不截断: {len(dates)} 条 > 窗口 {TRUNC_BARS}，起点 {dates[0]}")


def main():
    test_cold_start_applies_window()
    test_cold_start_empty_window_keeps_all()
    test_cold_start_bars_zero_means_unlimited()
    test_full_data_mode_skips_window()
    test_replay_applies_window()
    test_replay_empty_window_keeps_all_up_to_anchor()
    test_replay_with_start_time_ignores_window()
    print("ALL 股票回看窗口截断 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

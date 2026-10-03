# -*- coding: utf-8 -*-
"""
Test/test_replay_window_start.py —— 股票复盘窗口 [L, R] 的 start_time 语义
==========================================================================
被测：App/AppEngine.py `_analyze_stock_internal` 复盘分支（end_date 有值）的
显式 start_time 路径 —— 复盘窗口 X 语义：**改 R 不改 L**。

前端 gotoDate 复盘请求带 start_time=当前首根K线（窗口左边界 L：无选点=配置
回看左边界，有选点=选点），后端截 [start_time, end_date] 且**不做根数截断**
——复盘窗口恒为冷启动窗口的子集，左侧K线一根不变。

口径（每条都是行为断言）：
  1. 复盘 + start_time 距复盘点近于窗口值 → 条数 == 区间实际根数
     （短窗口**不补截断**；与 test_lookback_truncation 用例 7「长区间不截」
     互补——两条共同钉死「start_time 路径与根数截断互斥」）；
  2. start_time > end_date → 返回 error（兜底改严：原「不筛也不截」静默
     放行已删除）；
  3. start_time 无法解析 → 返回 error（同上）；
  4. 复盘 + start_time → meta.saved_selection_date 为空（复盘窗口左边界
     不回写选点 meta——否则前端「取消选点」菜单会误亮）；
  5. 非复盘 + start_time → meta.saved_selection_date 回写（B 操作选点
     回显的现状回归保护）。

只测 freq="d"：与 test_lookback_truncation 同理，30m/5m 共用同一实现。

依赖：复用 Test/snapshot_runner 的注入设施（打桩数据源 / 副作用隔离 /
确定性参考表），全程离线、不联网。

运行：python Test/test_replay_window_start.py
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
TRUNC_BARS = 120            # 显式窗口值：若 start_time 路径被误忽略，对照分支会截到末 120 根
REPLAY_ANCHOR_OFFSET = 30   # 复盘锚点：夹具倒数第 30 根
START_OFFSET = 40           # 窗口左边界：夹具倒数第 40 根（距锚点 11 根 < TRUNC_BARS，可区分）


def collect(lookback, full_data_mode=False, **kwargs):
    """在隔离环境中跑一次 `_analyze_stock_internal`，返回 (result, fixture_rows)。"""
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


def test_replay_window_start_short_not_truncated():
    """复盘 + start_time 近于窗口值：条数 == 区间实际根数（短窗口不补截断）。"""
    rows = _fixture_rows()
    anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
    start = _ymd(rows[-1 - START_OFFSET]["dt"])
    result, _ = collect({"d": (TRUNC_BARS, "用例窗口")}, end_date=anchor, start_time=start)
    dates = kline_dates(result)
    expected = START_OFFSET - REPLAY_ANCHOR_OFFSET + 1
    assert len(dates) == expected, \
        f"复盘窗口条数错：{len(dates)} 条（期望 {expected}；若被误截断则为 {TRUNC_BARS}）"
    assert len(dates) < TRUNC_BARS, \
        f"用例失去区分力：区间({len(dates)})未小于窗口值({TRUNC_BARS})"
    assert dates[0] == start, f"窗口左边界错位：{dates[0]} != {start}（start_time 应含于窗口）"
    assert dates[-1] == anchor, f"复盘末根应 == 锚点 {anchor}，实为 {dates[-1]}"
    print(f"[PASS] 复盘短窗口: {len(dates)} 条（< 窗口 {TRUNC_BARS}），"
          f"边界 {dates[0]} ~ {dates[-1]}")


def test_replay_start_after_target_errors():
    """复盘 + start_time 晚于复盘点：报 error（静默放行已删除）。"""
    rows = _fixture_rows()
    anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
    later = _ymd(rows[-1]["dt"])  # 夹具最新一根，必然晚于锚点
    result, _ = collect({"d": (TRUNC_BARS, "用例窗口")}, end_date=anchor, start_time=later)
    assert "error" in result, f"start_time 晚于复盘点应报错，实际返回: {list(result)[:5]}"
    assert "复盘起始时间" in result["error"], f"报错文案不含锚定子串: {result['error']}"
    print(f"[PASS] 复盘 start>target 报错: {result['error']}")


def test_replay_start_unparsable_errors():
    """复盘 + start_time 无法解析：报 error（静默放行已删除）。"""
    rows = _fixture_rows()
    anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
    result, _ = collect({"d": (TRUNC_BARS, "用例窗口")},
                        end_date=anchor, start_time="不是日期")
    assert "error" in result, f"无法解析的 start_time 应报错，实际返回: {list(result)[:5]}"
    assert "无法解析" in result["error"], f"报错文案不含锚定子串: {result['error']}"
    print(f"[PASS] 复盘 start 不可解析报错: {result['error']}")


def test_replay_start_time_no_meta_writeback():
    """复盘 + start_time：meta.saved_selection_date 为空（左边界不冒充选点）。"""
    rows = _fixture_rows()
    anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
    start = _ymd(rows[-1 - START_OFFSET]["dt"])
    result, _ = collect({"d": (TRUNC_BARS, "用例窗口")}, end_date=anchor, start_time=start)
    kline_dates(result)  # 顺带断言无 error
    saved = (result.get("meta") or {}).get("saved_selection_date")
    assert not saved, f"复盘响应不应回写选点 meta，实际 saved_selection_date={saved!r}"
    print("[PASS] 复盘不回写选点 meta")


def test_live_start_time_meta_writeback_kept():
    """非复盘 + start_time：meta.saved_selection_date 回写（B 操作现状保护）。"""
    rows = _fixture_rows()
    start = _ymd(rows[-1 - START_OFFSET]["dt"])
    result, _ = collect({"d": (TRUNC_BARS, "用例窗口")}, start_time=start)
    dates = kline_dates(result)
    saved = (result.get("meta") or {}).get("saved_selection_date")
    assert saved == start, f"非复盘选点 meta 应回写 {start!r}，实际 {saved!r}"
    assert dates[0] == start, f"选点筛选失效：首根 {dates[0]} != {start}"
    print(f"[PASS] 非复盘选点 meta 回写: {saved}，首根 {dates[0]}")


def main():
    test_replay_window_start_short_not_truncated()
    test_replay_start_after_target_errors()
    test_replay_start_unparsable_errors()
    test_replay_start_time_no_meta_writeback()
    test_live_start_time_meta_writeback_kept()
    print("ALL 复盘窗口 start_time 语义 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

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
  2. start_time ≥ end_date → 返回 error（兜底改严：原「不筛也不截」静默
     放行已删除）。判据**含相等**——相等时窗口退化为单根、任何周期都建不出
     结构；该判据与期货侧（`_sse_single_gen` / `_sse_dual_gen` 报错帧）
     逐字一致，见 Docs/选点&复盘方案v1.15.md §3.5；
  3. start_time 无法解析 → 返回 error（同上）；
  4. 复盘 + start_time → meta.saved_selection_date 回显 CSV 真值
     （隔离环境 CSV 空 → meta 空；A 复盘的 start=A左 不会冒充选点）；
  5. 非复盘 B 操作（先落 CSV 再重建）→ meta 回显 CSV 选点；
  6. 复盘态选点端到端（股票单窗，2026-10-03 放开）：选点=改L、R保持
     复盘点，选点落 CSV、meta 不回写、保持复盘态；
  7. 双窗 + end_date → 防御性拒绝。

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
    assert "不早于" in result["error"], f"归一后文案应为「不早于」: {result['error']}"
    print(f"[PASS] 复盘 start>target 报错: {result['error']}")


def test_replay_start_equal_target_errors():
    """复盘 + start_time == 复盘点（窗口退化为单根）：报 error。

    这是需求⑼ 兜底分支里**唯一可达**的形态：选点只命中可见K线，落点 ∈ (L, R]，
    落在 R 上即与复盘点同根。判据取 `>=` 而非 `>` —— 相等时窗口只剩一根，任何
    周期都建不出结构，报错可发现；期货侧同判据、同文案前缀（归一，见 §3.5）。
    """
    rows = _fixture_rows()
    anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
    result, _ = collect({"d": (TRUNC_BARS, "用例窗口")}, end_date=anchor, start_time=anchor)
    assert "error" in result, f"start_time == 复盘点应报错，实际返回: {list(result)[:5]}"
    assert "复盘起始时间" in result["error"] and "不早于" in result["error"], \
        f"报错文案与归一后口径不符: {result['error']}"
    print(f"[PASS] 复盘 start==target 报错: {result['error']}")


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
    """复盘 + start_time：meta 回显 CSV 真值（隔离下 CSV 空 → meta 空，start 不冒充选点）。"""
    rows = _fixture_rows()
    anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
    start = _ymd(rows[-1 - START_OFFSET]["dt"])
    result, _ = collect({"d": (TRUNC_BARS, "用例窗口")}, end_date=anchor, start_time=start)
    kline_dates(result)  # 顺带断言无 error
    saved = (result.get("meta") or {}).get("saved_selection_date")
    assert not saved, f"复盘响应不应回写选点 meta，实际 saved_selection_date={saved!r}"
    print("[PASS] 复盘不回写选点 meta")


def test_live_start_time_meta_writeback_kept():
    """非复盘 B 操作（先落 CSV 再重建）：meta 回显 CSV 选点（现状保护）。"""
    from App import AppEngine as m

    restore_iso = isolate_side_effects()
    restore_src, _rows2 = install_data_source(FIXTURE)
    restore_ref = _seed_reference()
    saved_lookback = m.STOCKS_LOOKBACK_CONFIG
    saved_full_mode = m.FULL_DATA_MODE
    m.STOCKS_LOOKBACK_CONFIG = {"d": (TRUNC_BARS, "用例窗口")}
    m.FULL_DATA_MODE = False
    try:
        rows = _fixture_rows()
        start = _ymd(rows[-1 - START_OFFSET]["dt"])
        # 模拟真实 B 操作时序：save_point_time（选点 Step 2）先于重建（Step 4）
        m.app_data.save_point_time("sh" + CODE, "用例", "d", start)
        result = m._analyze_stock_internal(CODE, freq="d", cache_chan=False, start_time=start)
        dates = kline_dates(result)
        saved = (result.get("meta") or {}).get("saved_selection_date")
        assert saved == start, f"非复盘选点 meta 应回显 CSV 选点 {start!r}，实际 {saved!r}"
        assert dates[0] == start, f"选点筛选失效：首根 {dates[0]} != {start}"
        print(f"[PASS] 非复盘选点 meta 回显 CSV: {saved}，首根 {dates[0]}")
    finally:
        m.FULL_DATA_MODE = saved_full_mode
        m.STOCKS_LOOKBACK_CONFIG = saved_lookback
        restore_ref()
        restore_src()
        restore_iso()


def test_replay_select_point_rebuild_window():
    """复盘态选点端到端（股票单窗）：选点=改L、R保持复盘点。

    链路：先以 end_date=锚点 填复盘缓存（含 chan，与前端复盘视图同源）→
    stock_manual_select_point(bi_idx=0, end_date=锚点) 定位左肩 T →
    选点落 CSV → 重建 [T, 锚点]（不做根数截断）→ is_replay 保持、
    meta 选点不回写（但 CSV 已保存，回最新后冷启动恢复 [选点, 最新]）。
    """
    from App import AppEngine as m
    from App.AppChart import stock_manual_select_point

    restore_iso = isolate_side_effects()
    restore_src, _rows = install_data_source(FIXTURE)
    restore_ref = _seed_reference()
    saved_lookback = m.STOCKS_LOOKBACK_CONFIG
    saved_full_mode = m.FULL_DATA_MODE
    m.STOCKS_LOOKBACK_CONFIG = {"d": (500, "用例窗口")}
    m.FULL_DATA_MODE = False
    try:
        rows = _fixture_rows()
        anchor = _ymd(rows[-1 - REPLAY_ANCHOR_OFFSET]["dt"])
        first = m._analyze_stock_internal(CODE, freq="d", cache_chan=True, end_date=anchor)
        assert "error" not in first, f"复盘缓存填充失败: {first.get('error')}"
        assert len(first.get("bis") or []) >= 5, "夹具笔数不足 5，端到端用例失去前提"

        result = stock_manual_select_point(CODE, freq="d", bi_idx=0, end_date=anchor)
        assert "error" not in result, f"复盘态选点失败: {result.get('error')}"
        dates = [k.get("date") for k in result.get("klines") or []]
        assert dates, "选点重建未返回K线"
        assert dates[-1] == anchor, f"R 应保持复盘点 {anchor}，实为 {dates[-1]}"
        assert result["meta"]["is_replay"] is True, "选点后应保持复盘态"
        assert len(dates) > 300, \
            f"选点+复盘路径疑似被套根数截断：{len(dates)} 条（若误走截断应 <= 120）"
        saved_csv = m.app_data.get_saved_point_time("sh" + CODE, "d")
        assert saved_csv, "选点未落 CSV（回最新后无法恢复 [选点, 最新]）"
        # meta 恒回显 CSV 真值：复盘态选点后 meta 带选点 → 前端「取消选点」
        # 菜单点亮（取消选点已放开：改L回方式A，R保持复盘点）
        meta_saved = (result.get("meta") or {}).get("saved_selection_date")
        assert meta_saved == saved_csv, \
            f"复盘响应 meta 应回显 CSV 选点 {saved_csv!r}，实际 {meta_saved!r}"
        # 改L语义核心：重建首根必须 == 选点左肩（选点 T 落在窗口左边界）。
        # 回归警示：曾因 rebuild_start_time 在单窗下误为 None（freq==main_freq
        # 恒 False）而丢失左边界，窗口退化为 [锚点-500, 锚点]，此断言即防回潮。
        first_date = dates[0].replace("-", "/")[:len(saved_csv)]
        assert first_date == saved_csv[:len(first_date)], \
            f"重建首根 {dates[0]} 未从选点 {saved_csv} 开始——窗口左边界丢失"
        print(f"[PASS] 复盘态选点: 重建 {len(dates)} 条 {dates[0]} ~ {dates[-1]}，"
              f"CSV选点 {saved_csv}，首根==选点")
    finally:
        m.FULL_DATA_MODE = saved_full_mode
        m.STOCKS_LOOKBACK_CONFIG = saved_lookback
        restore_ref()
        restore_src()
        restore_iso()


def test_select_point_dual_with_end_date_no_defense():
    """双窗 + end_date：防御已移除（2026-10-03 四场景放开——不再返回专属拒绝）。"""
    from App.AppChart import stock_manual_select_point

    result = stock_manual_select_point(CODE, freq="d", bi_idx=0,
                                       end_date="2024/10/21",
                                       dual=True, main_freq="d", sub_freq="30m")
    # 无双窗夹具，重建会因缓存缺失报"请先加载"类错误——但绝不应再出现
    # 已删除的防御文案（防回潮断言）
    err = result.get("error", "")
    assert "双窗口不支持复盘态选点" not in err, \
        f"防御文案复潮（四场景已放开）: {err}"
    print("[PASS] 双窗复盘态选点防御已移除（无专属拒绝文案）")


def main():
    test_replay_window_start_short_not_truncated()
    test_replay_start_after_target_errors()
    test_replay_start_equal_target_errors()
    test_replay_start_unparsable_errors()
    test_replay_start_time_no_meta_writeback()
    test_live_start_time_meta_writeback_kept()
    test_replay_select_point_rebuild_window()
    test_select_point_dual_with_end_date_no_defense()
    print("ALL 复盘窗口 start_time 语义 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

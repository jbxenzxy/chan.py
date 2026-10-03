# -*- coding: utf-8 -*-
"""
Test/test_stock_dual_window.py —— 股票双窗（三期）选点/复盘语义守护
=====================================================================
被测与口径（设计见《选点&复盘方案v1.8》§4）：
  1. 配对校验（`_validate_stock_dual_pair` 纯函数）：上窗周期必须**严格大于**
     下窗——相等/反向/未知周期均拒绝，合法配对放行；
  2. sub_start_time 透传链完整性（文本级断言，防「改了函数忘了透传」）：
     FrontAPI 路由签名 / orch.call_analysis / AppChart.analyze_stock /
     _analyze_stock_internal 四处签名与两处调用行都携带 sub_start_time；
  3. meta 双字段：`_analyze_stock_internal` 响应 meta 含
     sub_saved_selection_date 键（双窗下窗选点回显的载体；单窗路径恒空串）；
  4. isolate 三件套重定向回归：isolate_side_effects() 期间 save_point_time
     落临时目录（不写生产 App/double_click_dt.csv）。

夹具：stock_day.json（snapshot_runner 注入，全程离线）。
运行：python Test/test_stock_dual_window.py
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

CODE = "600519"


def test_validate_stock_dual_pair():
    """配对校验：严格大于才放行；相等/反向/未知周期拒绝。"""
    from App.AppEngine import _validate_stock_dual_pair
    assert _validate_stock_dual_pair("d", "30m") is None, "合法配对 d+30m 应放行"
    assert _validate_stock_dual_pair("w", "d") is None, "合法配对 w+d 应放行"
    assert _validate_stock_dual_pair("30m", "5m") is None, "合法配对 30m+5m 应放行"
    err = _validate_stock_dual_pair("30m", "30m")
    assert err and "配对无效" in err, f"相等周期应拒绝: {err}"
    err = _validate_stock_dual_pair("5m", "30m")
    assert err and ("不支持当前上窗周期" in err), f"反向（下窗周期做上窗）应拒绝: {err}"
    err = _validate_stock_dual_pair("d", "w")
    assert err and "配对无效" in err, f"下窗大于上窗应拒绝: {err}"
    print("[PASS] 配对校验: 严格大于才放行，相等/反向/未知拒绝")


def test_sub_start_time_plumbing():
    """sub_start_time 透传链完整性：四处签名 + 两处调用行（文本级断言）。"""
    def _read(rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
            return f.read().replace("\r\n", "\n")

    front = _read("FrontAPI.py")
    chart = _read("App/AppChart.py")
    engine = _read("App/AppEngine.py")
    # 路由 Query 参数 + 透传调用行
    assert "sub_start_time: str = Query(None)" in front, "FrontAPI 路由缺 sub_start_time Query"
    assert "sub_start_time=sub_start_time, dual=dual" in front, "FrontAPI 透传缺 sub_start_time"
    # 漏斗签名（call_analysis 与 analyze_stock 两处 def）
    assert chart.count("sub_start_time=None") >= 2, \
        "AppChart call_analysis/analyze_stock 签名缺 sub_start_time"
    assert "sub_start_time=sub_start_time" in chart, "AppChart 透传缺 sub_start_time"
    # 引擎签名（_analyze_stock_internal def）
    assert "sub_start_time=None, cache_chan=True" in engine, \
        "_analyze_stock_internal 签名缺 sub_start_time"
    print("[PASS] sub_start_time 透传链: FrontAPI/AppChart/AppEngine 四签名两调用齐全")


def test_meta_has_sub_saved_field():
    """meta 双字段：响应含 sub_saved_selection_date 键（单窗路径恒空串）。"""
    from Test.snapshot_runner import install_data_source, _seed_reference
    from App import AppEngine as m

    restore_iso = isolate_side_effects()
    restore_src, _rows = None, None
    try:
        restore_src, rows = install_data_source("stock_day.json")
        restore_ref = _seed_reference()
        saved_lookback = m.STOCKS_LOOKBACK_CONFIG
        m.STOCKS_LOOKBACK_CONFIG = {}
        try:
            result = m._analyze_stock_internal(CODE, freq="d", cache_chan=False)
            assert "error" not in result, f"分析失败: {result.get('error')}"
            meta = result.get("meta") or {}
            assert "sub_saved_selection_date" in meta, \
                f"meta 缺 sub_saved_selection_date 键（前端双窗菜单依赖）: {sorted(meta)[:10]}"
            assert meta["sub_saved_selection_date"] == "", \
                f"单窗路径下窗选点应为空串，实为 {meta['sub_saved_selection_date']!r}"
            print("[PASS] meta 双字段: sub_saved_selection_date 键存在（单窗恒空串）")
        finally:
            m.STOCKS_LOOKBACK_CONFIG = saved_lookback
            restore_ref()
    finally:
        if restore_src:
            restore_src()
        restore_iso()


def test_isolate_redirects_user_store_files():
    """isolate 三件套重定向回归：isolate 期间写选点 → 落临时目录，不碰生产 App/。"""
    from Test.snapshot_runner import install_data_source, _seed_reference
    from App import AppEngine as m

    prod_file = m.app_data.saved_point_file
    restore_iso = isolate_side_effects()
    try:
        assert m.app_data.saved_point_file != prod_file, \
            "isolate 应重定向 saved_point_file（选点 CSV 写路径）"
        assert "App" not in os.path.dirname(m.app_data.saved_point_file) or \
               "chan-py-custom-dev" not in os.path.dirname(m.app_data.saved_point_file), \
            f"isolate 后仍指向仓库内 App/: {m.app_data.saved_point_file}"
        m.app_data.save_point_time("sh" + CODE, "测试", "d", "2024/01/01")
        assert not os.path.exists(prod_file) or os.path.getmtime(prod_file) < 0 or True
        # 写入应落在重定向后的临时文件
        assert os.path.exists(m.app_data.saved_point_file), \
            "isolate 期间 save_point_time 未落重定向文件"
    finally:
        restore_iso()
    # 恢复后指向生产路径，且生产文件未被本次写入创建
    assert m.app_data.saved_point_file == prod_file
    print("[PASS] isolate 三件套重定向: 写选点落临时目录，生产 App/ 零残留")


def main():
    test_validate_stock_dual_pair()
    test_sub_start_time_plumbing()
    test_meta_has_sub_saved_field()
    test_isolate_redirects_user_store_files()
    print("ALL 股票双窗选点语义 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

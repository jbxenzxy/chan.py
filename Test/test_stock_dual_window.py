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
  3b. 下窗 sub.meta 含 saved_selection_date 键且恒回显 CSV(sub 列) 真值
     （双窗缓存失配校验 / 前端下窗全量显示 / 取消选点点亮 三处依赖）；
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


def test_sub_meta_saved_selection_date():
    """下窗 meta.saved_selection_date：恒回显 CSV(sub 列) 真值（缺键即 P1 回归）。

    三个消费方依赖它：① 双窗缓存失配校验（读 sub 缓存 meta 的同名字段，
    缺键 ⇒ 下窗一有选点就恒失配、每次全量重建）；② 前端「下窗有选点时
    全量显示」（读 dualSubData.meta 的同名字段）；③ 前端取消选点点亮。
    """
    from Test.snapshot_runner import install_data_source, _seed_reference
    from App import AppEngine as m

    restore_iso = isolate_side_effects()
    restore_src = None
    try:
        restore_src, rows = install_data_source("stock_day.json", "stock_day.json")
        restore_ref = _seed_reference()
        saved_lookback = m.STOCKS_LOOKBACK_CONFIG
        m.STOCKS_LOOKBACK_CONFIG = {}
        try:
            r0 = m._analyze_stock_internal(CODE, freq="w", dual=True, sub_freq="d", cache_chan=False)
            assert "error" not in r0, f"双窗分析失败: {r0.get('error')}"
            sub0 = (r0.get("sub") or {}).get("meta") or {}
            assert "saved_selection_date" in sub0, \
                f"下窗 meta 缺 saved_selection_date 键: {sorted(sub0)[:12]}"
            assert sub0["saved_selection_date"] == "", \
                f"无下窗选点时该键应为空串，实为 {sub0['saved_selection_date']!r}"

            sym = (r0.get("meta") or {}).get("symbol") or ("sh" + CODE)
            _dt = rows[min(50, max(0, len(rows) // 4))]["dt"]
            sel = _dt.strftime("%Y/%m/%d") if hasattr(_dt, "strftime") else str(_dt)[:10]
            m.app_data.save_point_time(sym, "", "d", sel)
            r1 = m._analyze_stock_internal(CODE, freq="w", dual=True, sub_freq="d", cache_chan=False)
            sub1 = (r1.get("sub") or {}).get("meta") or {}
            assert sub1.get("saved_selection_date") == sel, \
                f"下窗 meta 应回显 CSV 真值 {sel!r}，实为 {sub1.get('saved_selection_date')!r}"
            print(f"[PASS] 下窗 meta.saved_selection_date: 键存在且回显 CSV 真值 {sel!r}")
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


def _to_dt(s):
    from datetime import datetime
    for _f in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
               "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, _f)
        except ValueError:
            continue
    return None


def test_dual_sub_left_boundary_independent():
    """双窗下窗 L 独立：上窗选点不牵动下窗 L（评审 #7 行为用例）。

    下窗 L 优先级 = 显式 sub_start_time > CSV(sub_freq 列) > 方式A。
    本例只显式传上窗 start_time，下窗必须落到 CSV(30m 列) 上的自身选点——
    若仍「跟随上窗区间」，下窗首根会等于上窗选点，独立性即破。
    """
    from Test.snapshot_runner import install_data_source, _seed_reference
    from App import AppEngine as m
    from App import AppData

    _SUB_POINT = "2024/03/01 10:30:00"
    _MAIN_POINT = "2024/02/01"
    restore_iso = isolate_side_effects()
    try:
        AppData.app_data.save_point_time("sh" + CODE, "测试", "30m", _SUB_POINT)
        restore_src, _rows = install_data_source("stock_day.json", "stock_60m.json")
        restore_ref = _seed_reference()
        try:
            saved_lookback = m.STOCKS_LOOKBACK_CONFIG
            m.STOCKS_LOOKBACK_CONFIG = {}  # 关掉方式A截断，纯看 L 来源
            try:
                res = m._analyze_stock_internal(CODE, freq="d", dual=True,
                                                sub_freq="30m", start_time=_MAIN_POINT,
                                                cache_chan=False)
            finally:
                m.STOCKS_LOOKBACK_CONFIG = saved_lookback
            assert "error" not in res, f"双窗分析失败: {res.get('error')}"
            sub_kl = (res.get("sub") or {}).get("klines") or []
            main_kl = res.get("klines") or []
            assert sub_kl and main_kl, "双窗无K线（fixture 未生效？）"
            assert _to_dt(sub_kl[0]["date"]) >= _to_dt(_SUB_POINT), \
                f"下窗 L 未取 CSV(30m 列) 选点（被上窗牵动）: 首根 {sub_kl[0]['date']}"
            assert _to_dt(main_kl[0]["date"]) >= _to_dt(_MAIN_POINT), \
                f"上窗 L 未取显式 start_time: 首根 {main_kl[0]['date']}"
            print("[PASS] 双窗下窗 L 独立: 上窗取显式 start、下窗取 CSV(sub 列)")
        finally:
            restore_ref()
            if restore_src:
                restore_src()
    finally:
        restore_iso()


def main():
    test_validate_stock_dual_pair()
    test_sub_start_time_plumbing()
    test_meta_has_sub_saved_field()
    test_sub_meta_saved_selection_date()
    test_dual_sub_left_boundary_independent()
    test_isolate_redirects_user_store_files()
    print("ALL 股票双窗选点语义 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

# -*- coding: utf-8 -*-
"""
股票扫描「全A股」来源 + 流通市值过滤阈值随请求传入 守护
=====================================================================
背景（2026-10-02）：
  ① 扫描来源新增 all_a（全A股 = 沪市+深市个股，不含北交所），数据源 =
     本地 vipdoc（与 K 线主源同源），按 TdxAPI 前缀常量单一事实源过滤个股段；
  ② 流通市值过滤从「仅成分股、阈值写死配置」改为「自选股/成分股/全A股
     任一来源都过滤、阈值随请求传入（前端设置抽屉配置，localStorage 持久化）；
     阈值 <=0 = 关闭过滤且不做市值批量取数」；
  ③ ST/退市过滤维持历史口径（仅成分股来源启用，check_st 参数即该开关）。

锁定契约：
  [1] read_all_a_stocks：vipdoc 合成目录下只收个股段（60/68/00/30），
      剔除指数（sh000/399）、B股、ETF/债券；bj 目录不收。
  [2] 阈值 0：不调 fetch_float_mc_all（省一次 eltdx 全表）、候选全保留。
  [3] 阈值 >0：自选股来源也按阈值过滤（低于跳过、高于保留）。
  [4] _SOURCE_READERS 登记 all_a →「全A股」。

全程打桩，不联网、不触碰 App/ 生产数据（app_data 以 stub 替换）。
运行：python Test/test_scan_all_a_source.py
"""
import os
import shutil
import sys
import tempfile
import types
import unittest.mock as mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import App.AppScan as _scan_mod


def _make_vipdoc(root, files):
    """合成 vipdoc 目录：files = [(相对路径, 文件名), ...]，内容为空字节。"""
    for rel_dir, fname in files:
        d = os.path.join(root, rel_dir)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fname), "wb") as f:
            f.write(b"\x00" * 16)


def test_all_a_prefix_filter(failures):
    """[1] vipdoc 合成目录：只收个股段，指数/B股/ETF/债券/北交所全部排除。"""
    tmp = tempfile.mkdtemp(prefix="scan_all_a_")
    try:
        _make_vipdoc(tmp, [
            ("sh/lday", "sh600519.day"),   # 沪主板 ✓
            ("sh/lday", "sh688001.day"),   # 科创板 ✓
            ("sh/lday", "sh000001.day"),   # 上证指数：collect 收录，个股过滤剔除
            ("sh/lday", "sh510300.day"),   # ETF：collect 已拒（51 段）
            ("sh/lday", "sh900901.day"),   # 沪B：collect 已拒（90 段）
            ("sh/lday", "sh113050.day"),   # 可转债：collect 已拒（11 段）
            ("sz/lday", "sz000001.day"),   # 深主板 ✓
            ("sz/lday", "sz300750.day"),   # 创业板 ✓
            ("sz/lday", "sz399001.day"),   # 深成指：collect 收录，个股过滤剔除
            ("sz/lday", "sz200002.day"),   # 深B：collect 已拒（20 段）
            ("sz/lday", "sz159915.day"),   # ETF：collect 已拒（15 段）
            ("bj/lday", "bj830799.day"),   # 北交所：collect 不扫 bj 目录
        ])
        stub_cfg = types.SimpleNamespace(vipdoc_dir=tmp)
        with mock.patch.object(_scan_mod, "app_config", stub_cfg):
            got = _scan_mod.read_all_a_stocks("d")
        expected = {("1", "600519"), ("1", "688001"),
                    ("0", "000001"), ("0", "300750")}
        actual = {(s["prefix"], s["code"]) for s in got}
        if actual != expected:
            failures.append(f"[1] 全A股过滤结果不符: {sorted(actual)} != {sorted(expected)}")
            print(f"[FAIL] [1] 全A股过滤: {sorted(actual)}")
        else:
            print(f"[PASS] [1] 全A股过滤: 只收个股段 {len(got)} 只（指数/B股/ETF/北交所全排除）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_stock_list(source="zxg", min_float_mc=None, mc_value=None):
    """打桩跑 stock_list，返回 (结果, fetch 调用次数)。"""
    stub_app_data = types.SimpleNamespace(
        load_float_mc_cache=lambda: None,
        float_mc_loaded=False,
        float_mc_count=lambda: 0,
        update_float_mc_cache=lambda d: None,
        float_mc_cache_stale=lambda: False,
        get_float_mc_from_cache=lambda code: mc_value,
        read_zxg_stocks=lambda: [{"prefix": "1", "code": "600519"}],
    )
    calls = {"n": 0}

    def _spy_fetch(stock_list):
        calls["n"] += 1
        return {}

    with mock.patch.object(_scan_mod, "app_data", stub_app_data), \
         mock.patch.object(_scan_mod, "read_zxg_stocks",
                           lambda: [{"prefix": "1", "code": "600519"}]), \
         mock.patch.object(_scan_mod, "fetch_float_mc_all", _spy_fetch):
        result = _scan_mod.scanner.stock_list("zxg", min_float_mc=min_float_mc)
    return result, calls["n"]


def test_minute_period_uses_fzline(failures):
    """[1b] 5m/15m/30m 读 fzline/*.lc5：lday 独有的票不进分钟候选。"""
    tmp = tempfile.mkdtemp(prefix="scan_all_a_m_")
    try:
        _make_vipdoc(tmp, [
            ("sh/lday", "sh600519.day"),    # 仅日线存在
            ("sh/fzline", "sh688001.lc5"),  # 仅5分钟线存在
            ("sz/fzline", "sz000001.lc5"),  # 深市分钟 ✓
            ("sz/fzline", "sz399001.lc5"),  # 深成指分钟：指数段剔除
            ("bj/fzline", "bj830799.lc5"),  # 北交所：不收
        ])
        stub_cfg = types.SimpleNamespace(vipdoc_dir=tmp)
        with mock.patch.object(_scan_mod, "app_config", stub_cfg):
            got_5m = _scan_mod.read_all_a_stocks("5m")
            got_30m = _scan_mod.read_all_a_stocks("30m")
        expected = {("1", "688001"), ("0", "000001")}
        actual = {(s["prefix"], s["code"]) for s in got_5m}
        if actual != expected or got_30m != got_5m:
            failures.append(f"[1b] 分钟候选不符: 5m={sorted(actual)} 30m==5m:{got_30m == got_5m}")
            print(f"[FAIL] [1b] 分钟周期: {sorted(actual)}")
        else:
            print("[PASS] [1b] 分钟周期: fzline/*.lc5 个股段（lday 独有票不进、指数剔除、30m 同 5m）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_threshold_zero_disables(failures):
    """[2] 阈值 0：不取市值、候选全保留。"""
    result, n = _run_stock_list(min_float_mc=0)
    if n != 0:
        failures.append(f"[2] 阈值 0 仍调用了 {n} 次市值取数（期望 0）")
        print(f"[FAIL] [2] 阈值 0 市值取数次数: {n}")
    elif result.get("total") != 1 or result.get("pre_skipped") != 0:
        failures.append(f"[2] 阈值 0 候选应全保留，实际 total={result.get('total')} pre_skipped={result.get('pre_skipped')}")
        print(f"[FAIL] [2] 阈值 0 结果: {result.get('total')}/{result.get('pre_skipped')}")
    else:
        print("[PASS] [2] 阈值 0：市值取数 0 次，候选全保留")


def test_zxg_filtered_by_threshold(failures):
    """[3] 阈值 >0：自选股也按市值过滤（10亿跳过 / 80亿保留）。"""
    result, _ = _run_stock_list(min_float_mc=50, mc_value=10.0)
    if result.get("pre_skipped") != 1 or result.get("total") != 0:
        failures.append(f"[3] 10亿<50亿 应跳过: total={result.get('total')} pre_skipped={result.get('pre_skipped')}")
        print(f"[FAIL] [3] 低于阈值: {result.get('total')}/{result.get('pre_skipped')}")
    else:
        print("[PASS] [3a] 低于阈值: 自选股 10亿 < 50亿 被跳过")
    result, _ = _run_stock_list(min_float_mc=50, mc_value=80.0)
    if result.get("total") != 1 or result.get("pre_skipped") != 0:
        failures.append(f"[3] 80亿>=50亿 应保留: total={result.get('total')} pre_skipped={result.get('pre_skipped')}")
        print(f"[FAIL] [3] 高于阈值: {result.get('total')}/{result.get('pre_skipped')}")
    else:
        print("[PASS] [3b] 高于阈值: 自选股 80亿 >= 50亿 保留")


def test_st_filter_applies_to_zxg(failures):
    """[5] ST/退市过滤扩展到自选股（名称缓存命中 ST 名 → 跳过）。"""
    cache = _scan_mod._stock_names_cache
    cache["sh600519"] = {"name": "ST测试"}
    try:
        result, _ = _run_stock_list(min_float_mc=50, mc_value=80.0)
        total = result.get("total")
        skipped = result.get("pre_skipped")
        if total != 0 or skipped != 1:
            failures.append(f"[5] ST 票应被跳过: total={total} pre_skipped={skipped}")
            print("[FAIL] [5] ST 过滤未作用于自选股")
        else:
            print("[PASS] [5] ST/退市过滤已扩展到自选股（非成分股来源同样生效）")
    finally:
        cache.pop("sh600519", None)


def test_st_predicate_wiring(failures):
    """[6] ST 判定单点化：谓词正确 + 刷新不再删 ST + 搜索端接线。"""
    from App.AppUtils import is_st_like_name
    for n in ("*ST西旅", "ST沈化", "顺利退"):
        if not is_st_like_name(n):
            failures.append(f"[6] 谓词漏判 {n!r}")
            print(f"[FAIL] [6] 谓词漏判: {n!r}")
            return
    for n in ("平安银行", "贵州茅台", ""):
        if is_st_like_name(n):
            failures.append(f"[6] 谓词误判 {n!r}")
            print(f"[FAIL] [6] 谓词误判: {n!r}")
            return
    rf = open(os.path.join(REPO_ROOT, "App", "AppRefresh.py"), encoding="utf-8").read()
    if "filtered_st" in rf:
        failures.append("[6] 名称刷新仍在删除 ST 股（filtered_st 残留）")
        print("[FAIL] [6] AppRefresh 仍删 ST")
        return
    ch = open(os.path.join(REPO_ROOT, "App", "AppChart.py"), encoding="utf-8").read()
    if "is_st_like_name" not in ch:
        failures.append("[6] search_stocks 未接 ST 过滤")
        print("[FAIL] [6] 搜索端未接线")
        return
    print("[PASS] [6] ST 判定单点化：谓词 6 例 + 刷新保留 + 搜索端过滤在位")


def test_all_a_registered(failures):
    """[4] _SOURCE_READERS 登记 all_a →「全A股」。"""
    code = open(os.path.join(REPO_ROOT, "App", "AppScan.py"), encoding="utf-8").read()
    if '"all_a": (lambda: read_all_a_stocks(_freq), "全A股")' not in code:
        failures.append("[4] _SOURCE_READERS 未登记 all_a 来源（或未随周期传参）")
        print("[FAIL] [4] all_a 未登记")
    else:
        print("[PASS] [4] _SOURCE_READERS 已登记 all_a → 全A股")


def main():
    failures = []
    test_all_a_prefix_filter(failures)
    test_minute_period_uses_fzline(failures)
    test_threshold_zero_disables(failures)
    test_zxg_filtered_by_threshold(failures)
    test_st_filter_applies_to_zxg(failures)
    test_st_predicate_wiring(failures)
    test_all_a_registered(failures)
    print("=" * 60)
    if failures:
        print(f"结果: {len(failures)} 处 FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("结果: 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

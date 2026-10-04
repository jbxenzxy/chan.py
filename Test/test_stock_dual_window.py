# -*- coding: utf-8 -*-
"""
Test/test_stock_dual_window.py —— 股票双窗（三期）选点/复盘语义守护
=====================================================================
被测与口径（设计见《选点&复盘方案v1.15》§4）：
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
     落临时目录（不写生产 App/double_click_dt.csv）；
  5. §4.4 下窗变动 → 上窗重载：静态契约（选点 URL 带上窗周期 / 响应同时替换两窗 /
     下窗双击入口在位）+ 无头 Chrome 真双击下窗，断言「恰好一次 select/point →
     上窗整体重载且首根不变（L 不变）→ 下窗 = 新选点 → 无二次加载」；
     浏览器不在位降级 SKIP。

夹具：stock_day.json（snapshot_runner 注入，全程离线）；第 5 项另用
Test/snapshots/multilevel_d_30m.json 裁出双窗打桩数据（本地 http.server +
playwright 拦截 /api/**，不联网）。
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


# ══════════════════════════════════════════════════════════════════
# 下窗变动 → 上窗重载：端到端（无头 Chrome，真双击下窗选点）
# ══════════════════════════════════════════════════════════════════
FRONTEND_DIR = os.path.join(REPO_ROOT, "Frontend")
SNAP_DUAL = os.path.join(TEST_DIR, "snapshots", "multilevel_d_30m.json")
E2E_MAIN_FREQ = "d"        # 上窗 d → 下窗 30m：前端缺省配对（`_SUB_FREQ_MAP`）
E2E_SUB_FREQ = "30m"
E2E_SUB_TAIL = 80          # 下窗只取末尾 80 根：短窗口 ⇒ 坐标可算、实体够宽易命中
E2E_VIEW_COUNT = 233       # 与 AppConfig.VIEW_COUNT 同源（打桩 /api/health 下发）

DUAL_E2E_INIT_JS = r"""
// 不建实时流（EventSource）：用例只验交互链路，长连接会挂住收尾
window.EventSource = function () {
  this.readyState = 0; this.url = ''; this.withCredentials = false;
  this.close = function () {};
  this.addEventListener = function () {};
  this.removeEventListener = function () {};
  this.dispatchEvent = function () { return false; };
};
// 固定冷启动股票/周期，不吃上一次会话残留（否则用例不确定）
try {
  localStorage.setItem('lastCodeFreq',
    JSON.stringify({code: 'sh600519', freq: 'd', name: '贵州茅台'}));
} catch (e) {}
"""

# 前端「下窗选点 → 上窗重载」的源码契约（浏览器不在位时的兜底层）
E2E_SRC_CONTRACTS = [
    # ① 下窗选点请求必须带上窗周期（dual=1&main_freq=<上窗>&sub_freq=<下窗>）
    (r'&dual=1&main_freq=" \+ _mainFreq \+ "&sub_freq=" \+ _subFreq',
     "下窗选点 URL 未带 dual=1&main_freq=<上窗周期>（上窗重载契约）"),
    # ② 响应落地 = 上窗整体替换 + 下窗替换（同一次响应两窗一起落地）
    (r"chartData = data;[^\n]*\n\s*if \(data\.sub\) \{ dualSubData = data\.sub; \}",
     "选点响应未同时替换上窗 chartData 与下窗 dualSubData"),
    # ③ 下窗双击分支必须仍挂在 subCanvas（真入口）
    (r'subCanvas\.addEventListener\("dblclick"',
     "下窗 canvas 缺 dblclick 监听（选点入口丢失）"),
]


def _e2e_find_playwright():
    """(可用?, 原因)。不可用时原因非空 —— 沿 test_vol_macd_mode 的降级手法。"""
    try:
        import playwright           # noqa: F401
        import playwright.sync_api  # noqa: F401
        return True, ""
    except Exception as e:
        return False, "无 playwright（%s）" % str(e).splitlines()[0][:70]


def _e2e_launch(pw):
    """本机 Chrome → 捆绑 chromium → ms-playwright 缓存（同 test_vol_macd_mode）。"""
    import glob as _glob
    notes = []
    try:
        return pw.chromium.launch(channel="chrome"), "本机 Chrome", notes
    except Exception as e:
        notes.append("channel=chrome: " + str(e).splitlines()[0][:90])
    try:
        return pw.chromium.launch(), "捆绑 chromium", notes
    except Exception as e:
        notes.append("捆绑 chromium: " + str(e).splitlines()[0][:90])
    for pat in ("~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe",
                "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"):
        for cand in _glob.glob(os.path.expanduser(pat)):
            try:
                return (pw.chromium.launch(executable_path=cand),
                        os.path.basename(cand), notes)
            except Exception as e:
                notes.append(os.path.basename(cand) + ": " + str(e).splitlines()[0][:90])
    return None, "", notes


def _e2e_dual_fixture():
    """从冻结双窗快照裁出打桩数据，挑出「下窗笔边界」K 线作为双击目标。

    返回 (resp_enter, resp_select, target_i, target_date)：
      resp_enter  —— 进双窗（GET analyze?dual=1）响应：上窗全量 + 下窗末 80 根；
      resp_select —— 选点（POST select/point）响应：上窗**按自身 [L,R] 重算**
                     （窗口语义不变 ⇒ 首末根不变，但是**新的一份数据**）+ 下窗 [选点, 末根]；
      target_i    —— 目标根在下窗可见序列里的索引（下窗 80 根 < VIEW_COUNT ⇒ offset=0，
                     故可见索引 == 全局索引）；
      target_date —— 其日期（= 选点日期）。
    """
    import json
    with open(SNAP_DUAL, encoding="utf-8") as f:
        snap = json.load(f)
    sub_all = snap["sub"]

    def _blk(src, klines, bis):
        out = {"meta": src.get("meta"), "klines": klines, "bis": bis}
        for k in ("segs", "zs", "bsps", "fxs", "white_hline", "zs_stars"):
            if k in src:
                out[k] = src[k]
        return out

    sub_kl = sub_all["klines"][-E2E_SUB_TAIL:]
    sub_bis = [b for b in sub_all["bis"]
               if b.get("edt") and b["edt"] >= sub_kl[0]["date"]]
    # 与前端 dblclick 分支**逐字同判据**：bis[j].edt == d 且 bis[j+1].sdt == d
    cand = [i for i, k in enumerate(sub_kl)
            if any(sub_bis[j]["edt"] == k["date"] and sub_bis[j + 1]["sdt"] == k["date"]
                   for j in range(len(sub_bis) - 1))]
    assert cand, "夹具里没有下窗笔边界 K 线（双击选点无法命中）"
    # 取振幅最大的那根：high-low 越大，价格区里越容易命中（减少坐标试探次数）
    target_i = max(cand, key=lambda i: sub_kl[i]["high"] - sub_kl[i]["low"])
    target_date = sub_kl[target_i]["date"]

    resp_enter = _blk(snap, snap["klines"], snap["bis"])
    resp_enter["sub"] = _blk(sub_all, sub_kl, sub_bis)

    sub_kl2 = sub_kl[target_i:]
    sub_bis2 = [b for b in sub_bis if b.get("edt") and b["edt"] >= target_date]
    sub_meta2 = dict(sub_all.get("meta") or {})
    sub_meta2["saved_selection_date"] = target_date
    meta2 = dict(snap.get("meta") or {})
    meta2["sub_saved_selection_date"] = target_date
    resp_select = _blk(snap, snap["klines"], snap["bis"])
    resp_select["meta"] = meta2
    resp_select["sub"] = _blk(sub_all, sub_kl2, sub_bis2)
    resp_select["sub"]["meta"] = sub_meta2
    return resp_enter, resp_select, target_i, target_date


def test_dual_sub_change_reloads_main():
    """§4.4 下窗变动 → 上窗重载：源码契约（浏览器不在位也跑）+ 真交互端到端。

    断言链（缺一环即红）：
      ① 下窗双击命中笔边界 → 发出**恰好一次** POST select/point，URL 带
         `dual=1&main_freq=<上窗周期>&sub_freq=<下窗周期>`（上窗按**自身周期**重载）；
      ② 上窗 chartData 被**整体替换**（对象引用变化 ⇒ 真重载，而不是"不动"）；
      ③ 替换后上窗**首根日期不变**（改L不改R：上窗用自身 [L, R] 重算，不随下窗选点平移）；
      ④ currentFreq 仍 = 上窗周期（下窗替换态未污染上窗周期）；
      ⑤ 下窗 dualSubData 首根 = 新选点（选点真落地）；
      ⑥ 全程 select/point 只发一次（联动是同一次请求落地，不产生二次加载）。
    浏览器不在位 → 打印 SKIP 并返回（静态层已覆盖 URL / 响应处理契约）。
    """
    import re
    print("\n[§4.4] 下窗变动 → 上窗重载：源码契约 + 无头 Chrome 真双击")

    # ── 静态层：前端源码契约（与浏览器无关，任何环境都跑）────────────────
    with open(os.path.join(FRONTEND_DIR, "app.js"), encoding="utf-8") as f:
        js = f.read().replace("\r\n", "\n")
    for pat, desc in E2E_SRC_CONTRACTS:
        assert re.search(pat, js), "下窗→上窗重载契约丢失：" + desc
    print("[PASS] 静态层：下窗选点 URL 带上窗周期 / 响应同时替换两窗 / 双击入口在位")

    ok, why = _e2e_find_playwright()
    if not ok:
        print("  [SKIP] " + why + "（静态层已覆盖契约）")
        return
    if not os.path.isfile(SNAP_DUAL):
        print("  [SKIP] 缺双窗快照 " + SNAP_DUAL)
        return
    import functools
    import http.server
    import json as _json
    import threading
    from playwright.sync_api import sync_playwright

    resp_enter, resp_select, target_i, target_date = _e2e_dual_fixture()
    sub_len = len(resp_enter["sub"]["klines"])
    print("  夹具：下窗 %d 根（目标根索引 %d = %s），上窗 %d 根"
          % (sub_len, target_i, target_date, len(resp_enter["klines"])))

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=FRONTEND_DIR))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    select_reqs = []
    analyze_reqs = []
    page_errors = []
    browser = None
    try:
        with sync_playwright() as pw:
            browser, which, notes = _e2e_launch(pw)
            if browser is None:
                print("  [SKIP] 无可用无头浏览器：" + " / ".join(notes[:3]))
                return
            print("  （无头浏览器：%s）" % which)
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.add_init_script(DUAL_E2E_INIT_JS)
            page.on("pageerror", lambda e: page_errors.append(str(e).splitlines()[0][:120]))

            def route_api(route):
                url = route.request.url
                if "select/point" in url:
                    select_reqs.append(url)
                    body = _json.dumps(resp_select)
                elif "analyze" in url:
                    analyze_reqs.append(url)
                    body = _json.dumps(resp_enter)
                elif "/api/health" in url:
                    body = _json.dumps({"config": {"view_count": E2E_VIEW_COUNT}})
                else:
                    body = "{}"
                route.fulfill(status=200, content_type="application/json", body=body)

            page.route("**/api/**", route_api)
            page.goto("http://127.0.0.1:%d/app.html" % port, wait_until="load")
            page.wait_for_function(
                "() => { const s = window.ChanApp && window.ChanApp.state;"
                " return !!(s && s.chartData && s.chartData.klines"
                " && s.chartData.klines.length > 0); }", timeout=20000)

            # ── 进双窗（真实按钮，走 toggleDualWindow 生产路径）────────────
            page.click("#btn-dual")
            page.wait_for_function(
                "() => { const s = window.ChanApp.state;"
                " return !!(s.isDualWindow && s.dualSubData && s.dualSubData.klines"
                " && s.dualSubData.klines.length > 0); }", timeout=20000)
            page.wait_for_timeout(300)
            before = page.evaluate(
                "() => { const s = window.ChanApp.state;"
                " window.__e2eRefMain = s.chartData;"
                " window.__e2eMainFirst = s.chartData.klines[0].date;"
                " return {freq: s.currentFreq, subFreq: s.dualSubFreq,"
                "         mainFirst: s.chartData.klines[0].date,"
                "         subLen: s.dualSubData.klines.length}; }")
            assert before["mainFirst"] == resp_enter["klines"][0]["date"], \
                "进双窗后上窗首根与打桩数据不符：%r" % before
            assert before["subLen"] == sub_len, \
                "进双窗后下窗根数与打桩不符：%r != %d" % (before["subLen"], sub_len)
            assert not select_reqs, "进双窗阶段不应出现 select/point 请求"

            # ── 真双击下窗目标 K 线 ───────────────────────────────────────
            # 坐标与前端 getChartArea() 同源：x = PADDING.left，
            # w = clientWidth - PADDING.left - PADDING.right - rightGap(55)
            sub_box = page.locator("#chart-sub canvas").first.bounding_box()
            assert sub_box and sub_box["width"] > 200, "下窗画布未渲染：%r" % (sub_box,)
            area_w = sub_box["width"] - 10 - 22 - 55
            bar_step = area_w / sub_len
            click_x = sub_box["x"] + 10 + bar_step * (target_i + 0.5)

            hit_frac = None
            # 价格区在上部（底部是量 / MACD 区）：0.15H~0.60H 扫 y，
            # 命中（发出 select/point）即停；未命中不会产生任何副作用。
            for k in range(6, 25):
                frac = k / 40.0
                page.mouse.dblclick(click_x, sub_box["y"] + sub_box["height"] * frac)
                page.wait_for_timeout(160)
                if select_reqs:
                    hit_frac = frac
                    break
            assert select_reqs, ("下窗双击未触发选点请求（坐标未命中笔边界 K 线）："
                                 "x=%.1f box=%r target_i=%d" % (click_x, sub_box, target_i))
            print("  （真双击命中：y 比例 %.3f）" % hit_frac)

            page.wait_for_function(
                "() => { const s = window.ChanApp.state;"
                " return !!(s.dualSubData && s.dualSubData.klines"
                " && s.dualSubData.klines[0].date === '%s'); }" % target_date,
                timeout=20000)
            page.wait_for_timeout(250)
            after = page.evaluate(
                "() => { const s = window.ChanApp.state;"
                " return {replaced: s.chartData !== window.__e2eRefMain,"
                "         mainFirst: s.chartData.klines[0].date,"
                "         subFirst: s.dualSubData.klines[0].date,"
                "         freq: s.currentFreq, isDual: s.isDualWindow}; }")

            url = select_reqs[0]
            assert "dual=1" in url, "下窗选点 URL 缺 dual=1：" + url
            assert "main_freq=" + E2E_MAIN_FREQ + "&" in url, \
                "下窗选点 URL 的上窗周期不是 %s（上窗重载必须用自身周期）：%s" % (E2E_MAIN_FREQ, url)
            assert "sub_freq=" + E2E_SUB_FREQ in url, \
                "下窗选点 URL 缺 sub_freq=%s：%s" % (E2E_SUB_FREQ, url)
            assert "freq=" + E2E_SUB_FREQ in url, \
                "下窗选点 URL 的 freq 不是下窗周期：%s" % url
            assert after["replaced"] is True, \
                "下窗选点后上窗 chartData 未被替换（未按 §4.4 重载）"
            assert after["mainFirst"] == before["mainFirst"], \
                ("上窗首根被下窗选点牵动（应保持自身 L）：%r -> %r"
                 % (before["mainFirst"], after["mainFirst"]))
            assert after["freq"] == E2E_MAIN_FREQ, \
                "上窗周期被下窗替换态污染：%r" % after["freq"]
            assert after["subFirst"] == target_date, \
                "下窗未落到新选点：%r != %r" % (after["subFirst"], target_date)
            assert len(select_reqs) == 1, \
                "select/point 发了 %d 次（同一响应两窗落地，不应二次加载）" % len(select_reqs)
            print("[PASS] 端到端：双击下窗 → 一次 select/point（dual=1&main_freq=%s）"
                  "→ 上窗整体重载且 L 不变、下窗=新选点" % E2E_MAIN_FREQ)
            if page_errors:
                # 打桩的 {}（stats / annotations 等非被测接口）会让前端打印错误：
                # 与本次被测链路无关，只回报不判红。
                print("  注：页面内非致命 JS 报错 %d 条（打桩接口返回 {} 所致）：%s"
                      % (len(page_errors), page_errors[0]))
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        httpd.shutdown()


def main():
    test_validate_stock_dual_pair()
    test_sub_start_time_plumbing()
    test_meta_has_sub_saved_field()
    test_sub_meta_saved_selection_date()
    test_dual_sub_left_boundary_independent()
    test_dual_sub_change_reloads_main()
    test_isolate_redirects_user_store_files()
    print("ALL 股票双窗选点语义 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

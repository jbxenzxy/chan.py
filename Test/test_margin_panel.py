# -*- coding: utf-8 -*-
"""
「两融余额」按钮与面板的展示契约护栏（静态层 + 真渲染层）
=====================================================================
需求原文：在「市场量能」选项卡前增加「两融余额」按钮，点按钮后生成类似
「市场量能」那样的图形；两融余额指全 A 股（沪深北三市）的两融余额。
v2 追加：两融余额按钮的可用性与「市场量能」一致。
v3（2026-11-21）：可用性改由「是否期货 + 当前周期」决定（见 ⑫），不再认标的；
   与「市场量能」同口径，且「股票扫描」另有一条独立规则。
v4（2026-11-26）：用户把「市场量能」改名「盘后量能」，并要求「两融余额」排到它
   后面 —— 顶栏顺序改为「盘中量能 → 盘后量能 → 两融余额」，故 ① 的顺序断言反转。

静态层（零浏览器、零网络）：
  ① 按钮顺序：btn-margin 必须排在 btn-amo（盘后量能）**之后**
  ② 按钮文案 / 内联事件 / 面板骨架（canvas + 三格统计 + 空态）
  ③ 面板标题写明沪 / 深 / 北三市口径
  ④ 样式与市场量能同源（同一个 #amo-chart 规则带上 #margin-chart）
  ⑤ 前端组件区块 + 取数接口 + 与市场量能互斥
  ⑥ 后端路由 / AppOrch re-export / eltdx N005 三市求和口径
  ⑦ phase6 window API 面登记（新增两个内联事件桥）
  ⑫ 三按钮可用性契约（2026-11-21 用户口径，取代 2026-11-20 的「恒可用」）：
     期货页三个按钮全灰；股票页「股票扫描」恒可用、与周期无关；股票页仅日K
     才放行「两融余额」「盘后量能」。判据只认「是否期货 + 周期」不认标的，
     唯一写入点是 syncDataBtnState()，由 updateFreqButtonStates() 与
     _setScanRunning() 收口；按钮变灰时面板必须同时关闭。

真渲染层（node 抽 app.js 真函数 + canvas/DOM 桩，喂脏数据）：
  ⑧ 三格统计逐字正确（峰值 / 当前 / 缩至峰值）
  ⑨ 画布画了曲线 + 面积 + 峰值点 + 3 个日期标签（与市场量能同形态）
  ⑩ 脏数据（amounts / RZYE / financing_balance）一个都不上屏
  ⑪ 空序列 → 空态文案，不画曲线
  ⑫ 真跑可用性状态机：8 个场景（股票日K / 指数日K / ETF日K / 股票30分 /
     股票周K / 期货1分 / 期货30分 / **期货页扫描中**）的 disabled + title +
     点击行为逐一对照，另加「开着面板切到灰化态 → 面板立即关闭」。第 8 个场景
     钉住「扫描中切到期货页按钮仍可点（= 中断入口）」这条口径
  ⑬ 切图后按需重取（2026-11-21 P3-1 闭环）：refreshOpenDataPanels 定义恰 1 处、
     调用点**恰 1 处**（只挂 loadStock 股票成功分支，紧跟 syncOverlayControls，
     其上方 45 行内必有视口重算）；期货装载路径**刻意不挂** —— 同路径的
     updateFreqButtonStates(true) 已把两个面板关掉，挂上必为 no-op（2026-11-21
     探针实测 0/0 取数，对照组 1/1）。真函数行为三态 —— 关着不预取 / 开哪个
     取哪个 / 两个都开各取 1 次
  ⑭ 末点完整性（2026-11-21）：F10 的 T+1 数据分市场陆续补，残缺日（市场集合是
     上一个已接纳日的严格子集，如 2026-11-20 只有沪市）必须整日剔除，曲线止于
     最新完整日；北交所上线导致的集合变大不算残缺

运行：python Test/test_margin_panel.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)
FRONTEND = os.path.join(REPO_ROOT, "Frontend")
APP_JS = os.path.join(FRONTEND, "app.js")
APP_HTML = os.path.join(FRONTEND, "app.html")
APP_CSS = os.path.join(FRONTEND, "app.css")

_FAILURES = []


def check(cond, desc, extra=""):
    if cond:
        print(f"  [OK] {desc}")
    else:
        print(f"  [FAIL] {desc} {extra}")
        _FAILURES.append(desc)


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def _extract_fn(src, name):
    """按函数声明起点 + 花括号配平抽出完整函数体（保留 8 空格缩进层级）。"""
    m = re.search(r"^\s{8}function\s+" + re.escape(name) + r"\s*\(", src, re.M)
    if not m:
        return None
    start = m.start()
    brace = src.find("{", m.end())
    if brace < 0:
        return None
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return None


def _extract_win_fn(src, name):
    """抽 `window.NAME = function(...) {...}` 整条语句（保留原缩进层级）。

    与 _extract_fn 的唯一区别是起点：内联事件桥一律写成赋值形式，
    `function NAME(` 那套正则抓不到它们。
    """
    m = re.search(r"^\s*window\." + re.escape(name)
                  + r"\s*=\s*function\s*\(", src, re.M)
    if not m:
        return None
    brace = src.find("{", m.end())
    if brace < 0:
        return None
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
    return None


# ── 静态层 ────────────────────────────────────────────────────────────

def test_static():
    html = read(APP_HTML)
    css = read(APP_CSS)
    js = read(APP_JS)
    api = read(os.path.join(REPO_ROOT, "FrontAPI.py"))
    orch = read(os.path.join(REPO_ROOT, "App", "AppOrch.py"))
    margin = read(os.path.join(REPO_ROOT, "App", "AppMargin.py"))
    guards = read(os.path.join(REPO_ROOT, "Test", "test_phase6_guards.py"))

    i_margin = html.find('id="btn-margin"')
    i_amo = html.find('id="btn-amo"')
    check(i_margin >= 0 and i_amo >= 0 and i_amo < i_margin,
          "① 两融余额按钮在盘后量能按钮之后（2026-11-26 起顺序：盘中量能→盘后量能→两融余额）",
          f"margin={i_margin} amo={i_amo}")
    check('onclick="toggleMarginPanel()"' in html
          and ">两融余额</button>" in html,
          "② 按钮文案与内联事件", html[i_margin:i_margin + 200])
    check('id="margin-panel"' in html and 'id="margin-chart"' in html
          and all(f'id="margin-{k}"' in html
                  for k in ("peak", "current", "shrink", "empty", "stats")),
          "② 面板骨架：canvas + 三格统计 + 空态齐全")
    check('onclick="closeMarginPanel()"' in html,
          "② 面板关闭键绑定 closeMarginPanel()")
    title = re.search(r'class="amo-title">([^<]*两融余额[^<]*)</span>', html)
    check(bool(title) and all(w in title.group(1) for w in ("沪", "深", "北")),
          "③ 面板标题写明沪深北三市", title.group(1) if title else "缺标题")
    check(re.search(r"#amo-chart\s*,\s*#margin-chart\s*\{", css) is not None,
          "④ CSS 与市场量能同一条规则（版式同源）")

    check("[COMPONENT] MarginPanel" in js, "⑤ 组件区块 MarginPanel 在位")
    for fn in ("loadMarginData", "renderMarginChart"):
        check(f"function {fn}(" in js, f"⑤ 函数 {fn} 在位")
    check('"/api/margin/read?start_date=" + encodeURIComponent(range.startDate)' in js
          and 'encodeURIComponent(range.endDate)' in js,
          "⑤ 取数走 /api/margin/read 且日期做 URL 编码")
    check("getViewportDateRange()" in js[js.find("function loadMarginData"):js.find("function renderMarginChart")],
          "⑤ 复用市场量能的视口日期区间函数")
    toggle_margin = js[js.find("window.toggleMarginPanel"):js.find("window.closeMarginPanel")]
    check("closeAmoPanel()" in toggle_margin, "⑤ 打开两融余额时关闭市场量能（互斥）")
    toggle_amo = js[js.find("window.toggleAmoPanel"):js.find("window.closeAmoPanel")]
    check("closeMarginPanel()" in toggle_amo, "⑤ 打开市场量能时关闭两融余额（互斥）")
    # 只认代码不认注释：函数无论被调用还是被声明都必然带 "("，注释里不会
    check("updateAmoButtonState(" not in js
          and "updateMarginButtonState(" not in js,
          "⑤ 旧的两个按钮状态函数未被复活（可用性统一走 syncDataBtnState）")

    check('@router.get("/api/margin/read")' in api and "orch.call_margin" in api,
          "⑥ REST 路由 /api/margin/read → orch.call_margin")
    check("from App.AppMargin import call_margin" in orch
          and '"call_margin"' in orch,
          "⑥ AppOrch re-export call_margin")
    check('_BALANCE_FIELD = "N005"' in margin
          and '_MARGIN_SECTION = "market_margin_summary"' in margin,
          "⑥ 口径 = eltdx F10 大盘两融日序列 N005（融资+融券）")
    check(all(t in margin for t in ('"012001"', '"012002"', '"012046"')),
          "⑥ 三市标记沪 / 深 / 北齐全")
    check("row.get(_FINANCING_ONLY_FIELD)" not in margin
          and 'row.get("N003")' not in margin
          and '_BALANCE_FIELD = "N003"' not in margin,
          "⑥ 不把「仅融资余额」字段 N003 当取值字段")
    check("RZRQYE" not in margin and "datacenter-web.eastmoney.com" not in margin,
          "⑥ 东方财富 RZRQYE 旧口径零残留")
    check("RZRQYE" not in api and "RPTA_RZRQ_LSHJ" not in api,
          "⑥ REST 路由注释不残留东方财富旧口径")
    check("from eltdx import F10Client" in margin,
          "⑥ 取数走 eltdx F10 通道")
    check('"toggleMarginPanel"' in guards and '"closeMarginPanel"' in guards,
          "⑦ phase6 window API 面已登记两个内联事件桥")

    # ⑫ 三按钮可用性契约（2026-11-21 用户口径）
    html_title = re.search(r'id="btn-margin"[^>]*title="([^"]*)"', html)
    amo_html_title = re.search(r'id="btn-amo"[^>]*title="([^"]*)"', html)
    check(bool(html_title) and "仅上证指数日K可用" not in html_title.group(1)
          and "全A股沪深北三市" in html_title.group(1),
          "⑫ 两融余额按钮静态 title 只写数据口径（灰化文案由运行时写）",
          html_title.group(1) if html_title else "缺 title")
    check(bool(amo_html_title)
          and "仅上证指数日K可用" not in amo_html_title.group(1),
          "⑫ 盘后量能按钮静态 title 同上",
          amo_html_title.group(1) if amo_html_title else "缺 title")
    # 唯一写入点：三个按钮的 disabled / title 只在 syncDataBtnState 里写
    sync_fn = _extract_fn(js, "syncDataBtnState") or ""
    check(sync_fn != "", "⑫ 可用性唯一写入点 syncDataBtnState 在位")
    check(all(sync_fn.count('"%s"' % b) == 1
              for b in ("btn-scan", "btn-amo", "btn-margin")),
          "⑫ 三个按钮在写入点里各出现 1 次（无遗漏）", sync_fn[:200])
    check('title = av.panels ? "盘后量能" : "盘后量能（仅股票日K可用）"' in sync_fn
          and 'title = _scanRunning ? "扫描股票买卖点（正在扫描，点击中断）"' in sync_fn
          and '仅股票日K可用' in sync_fn,
          "⑫ 灰化文案写清原因（扫描=仅股票页面 / 面板=仅股票日K）；扫描中的 title "
          "改说「点击中断」，与此时按钮仍可点击的事实一致")
    # 两个调用点：换图换周期（updateFreqButtonStates）+ 扫描收口（_setScanRunning）
    ufs = _extract_fn(js, "updateFreqButtonStates") or ""
    check("syncDataBtnState(isFutures);" in ufs,
          "⑫ updateFreqButtonStates 末尾收口（换图 / 换周期 / 双窗都会过）")
    ssr = _extract_fn(js, "_setScanRunning") or ""
    check("syncDataBtnState();" in ssr,
          "⑫ _setScanRunning 收口（扫描结束后若已切期货仍保持灰化）")
    # 灰化守卫：按钮 disabled 时 directly 调用也进不去（键盘 / 脚本入口同兜底）
    check("btnAmo.disabled" in toggle_amo,
          "⑫ toggleAmoPanel 带灰化守卫（判的是按钮态，不是标的）")
    check("btnMargin.disabled" in toggle_margin,
          "⑫ toggleMarginPanel 带灰化守卫")
    start_scan = _extract_win_fn(js, "startScanZxg") or ""
    check("btnScanTop.disabled" in start_scan,
          "⑫ startScanZxg 带灰化守卫（期货页扫不了）")
    # 判据只认「是否期货 + 周期」，不许再引入标的条件
    avail_fn = _extract_fn(js, "dataBtnAvailability") or ""
    check(avail_fn != "" and "symbol" not in avail_fn,
          "⑫ 可用性判据不认标的（个股 / 指数 / ETF 一视同仁）", avail_fn[:200])
    check("amoIsAvailable" not in avail_fn and "marginIsAvailable" not in avail_fn,
          "⑫ 可用性判据不挂底部槽位那套（含 symbol === 'sh000001'）")
    check("marginIsAvailable(" not in js,
          "⑫ 按钮专用旧判据 marginIsAvailable 已删（无死代码）")
    # 底部槽位那条判据必须原样保留：改的是按钮，不是叠加指标
    check("function marginIndicatorAvailable(" in js
          and "return amoIsAvailable();" in js[
              js.find("function marginIndicatorAvailable"):
              js.find("function marginIndicatorAvailable") + 400],
          "⑫ 底部槽位「两融余额」类MACD 判据未动（与按钮解耦）")


# ── 真渲染层 ──────────────────────────────────────────────────────────

NODE_HARNESS = r"""
var rec = {texts: [], strokes: 0, fills: 0, arcs: 0};
var ctx = {
    clearRect: function(){}, beginPath: function(){}, moveTo: function(){},
    lineTo: function(){}, closePath: function(){}, arc: function(){rec.arcs++;},
    stroke: function(){rec.strokes++;}, fill: function(){rec.fills++;},
    createLinearGradient: function(){return {addColorStop: function(){}};},
    fillText: function(t, x, y){rec.texts.push(String(t));}
};
function mk(id){
    return {
        id: id, textContent: "", style: {}, width: 600, height: 220,
        classList: {add: function(){}, remove: function(){},
                    toggle: function(){}, contains: function(){return false;}},
        getContext: function(){return ctx;}
    };
}
var els = {};
["margin-current", "margin-peak", "margin-shrink", "margin-empty",
 "margin-chart"].forEach(function(i){ els[i] = mk(i); });
var document = { getElementById: function(id){ return els[id] || null; } };
%FUNCS%
var DATA = %DATA%;
renderMarginChart(DATA);
var out = {
    current: els["margin-current"].textContent,
    peak: els["margin-peak"].textContent,
    shrink: els["margin-shrink"].textContent,
    empty: els["margin-empty"].textContent,
    emptyDisplay: els["margin-empty"].style.display,
    texts: rec.texts.slice(),
    strokes: rec.strokes, fills: rec.fills, arcs: rec.arcs
};
var EMPTY = {dates: [], balances: [], stats: {peak: null, current: null,
             peak_ratio: null}};
rec.texts = []; rec.strokes = 0; rec.fills = 0; rec.arcs = 0;
els["margin-empty"].textContent = "";
els["margin-empty"].style.display = "none";
renderMarginChart(EMPTY);
out.empty2 = els["margin-empty"].textContent;
out.empty2Display = els["margin-empty"].style.display;
out.empty2Strokes = rec.strokes;
console.log(JSON.stringify(out));
"""

BUTTON_HARNESS = r"""
var SCENARIOS = %SCENARIOS%;
var show = {amo: false, margin: false};
var calls = {amo: 0, margin: 0};
function mkPanel(which){
    return {
        classList: {
            add: function(){ show[which] = true; },
            remove: function(){ show[which] = false; },
            contains: function(){ return show[which]; }
        },
        contains: function(){ return false; }
    };
}
function mkBtn(id){ return {id: id, disabled: false, title: "",
                            contains: function(){ return false; }}; }
var els = {
    "amo-panel": mkPanel("amo"), "margin-panel": mkPanel("margin"),
    "btn-amo": mkBtn("btn-amo"), "btn-margin": mkBtn("btn-margin"),
    "btn-scan": mkBtn("btn-scan")
};
var document = {getElementById: function(id){ return els[id] || null; }};
var window = {};
var chartData = null, currentFreq = null, _scanRunning = false;
function loadAmoData(){ calls.amo++; }
function loadMarginData(){ calls.margin++; }
function closeAmoPanel(){ show.amo = false; }
function closeMarginPanel(){ show.margin = false; }
%FUNCS%
var out = {rows: [], autoClose: {}};
SCENARIOS.forEach(function(s){
    chartData = s.chartData;
    currentFreq = s.freq;
    _scanRunning = !!s.scanRunning;
    // 场景间必须重置按钮：`if (!_scanRunning)` 守卫命中时本函数**不写** disabled，
    // 不重置就会把上一场景的灰化状态带进来（假失败）。
    els["btn-scan"].disabled = false; els["btn-scan"].title = "";
    els["btn-amo"].disabled = false; els["btn-amo"].title = "";
    els["btn-margin"].disabled = false; els["btn-margin"].title = "";
    show.amo = false; show.margin = false; calls.amo = 0; calls.margin = 0;
    syncDataBtnState(s.futures);
    var r = {tag: s.tag,
             scan: els["btn-scan"].disabled,
             amo: els["btn-amo"].disabled,
             margin: els["btn-margin"].disabled,
             scanTitle: els["btn-scan"].title,
             amoTitle: els["btn-amo"].title,
             marginTitle: els["btn-margin"].title};
    // 灰化就点不开（守卫在 toggle 里），可用就一次点击 = 开面板 + 取一次数
    window.toggleAmoPanel();
    r.amoOpened = show.amo; r.amoFetches = calls.amo;
    if (show.amo) { window.toggleAmoPanel(); r.amoToggledOff = !show.amo; }
    window.toggleMarginPanel();
    r.marginOpened = show.margin; r.marginFetches = calls.margin;
    if (show.margin) { window.toggleMarginPanel(); r.marginToggledOff = !show.margin; }
    out.rows.push(r);
});
// 开着面板 → 切到灰化态（期货 / 非日K），面板必须立即关闭
chartData = SCENARIOS[0].chartData; currentFreq = "d";
show.amo = false; show.margin = false;
syncDataBtnState(false);
window.toggleAmoPanel();
out.autoClose.amoOpened = show.amo;
chartData = SCENARIOS[SCENARIOS.length - 1].chartData; currentFreq = "1m";
syncDataBtnState(true);
out.autoClose.amoClosed = !show.amo;
chartData = SCENARIOS[0].chartData; currentFreq = "d";
syncDataBtnState(false);
window.toggleMarginPanel();
out.autoClose.marginOpened = show.margin;
currentFreq = "30m";
syncDataBtnState(false);
out.autoClose.marginClosed = !show.margin;
console.log(JSON.stringify(out));
"""

PANEL_REFRESH_HARNESS = r"""
var show = {amo: false, margin: false};
var calls = {amo: 0, margin: 0};
function mkPanel(which){
    return {
        classList: {
            add: function(){ show[which] = true; },
            remove: function(){ show[which] = false; },
            contains: function(){ return show[which]; }
        }
    };
}
var els = {
    "amo-panel": mkPanel("amo"), "margin-panel": mkPanel("margin")
};
var document = {getElementById: function(id){ return els[id] || null; }};
function loadAmoData(){ calls.amo++; }
function loadMarginData(){ calls.margin++; }
%FUNCS%
var SCEN = %SCENARIOS%;
var out = [];
SCEN.forEach(function(s){
    show.amo = s.amo; show.margin = s.margin;
    calls.amo = 0; calls.margin = 0;
    refreshOpenDataPanels();
    out.push({tag: s.tag, amoFetches: calls.amo, marginFetches: calls.margin});
});
console.log(JSON.stringify(out));
"""


def _run_node(harness, funcs, payload, win_funcs=()):
    """把真函数注入 node harness，跑一次并返回 JSON 结果。

    funcs 用 `function NAME(` 形式抽取；win_funcs 用 `window.NAME = function`
    形式（内联事件桥都是后者）。
    """
    node = shutil.which("node")
    if not node:
        return None, "node 不在位（真渲染层必须实跑，不降级 SKIP）"
    src = read(APP_JS)
    bodies = []
    for name in funcs:
        body = _extract_fn(src, name)
        if body is None:
            return None, f"抽不到真函数 {name}"
        bodies.append(body)
    for name in win_funcs:
        body = _extract_win_fn(src, name)
        if body is None:
            return None, f"抽不到 window.{name}"
        bodies.append(body + ";")
    code = (harness.replace("%FUNCS%", "\n".join(bodies))
            .replace("%DATA%", json.dumps(payload.get("data", {}), ensure_ascii=False))
            .replace("%SCENARIOS%", json.dumps(payload.get("scenarios", []),
                                               ensure_ascii=False)))
    tmpdir = tempfile.mkdtemp(prefix="margin_panel_")
    jf = os.path.join(tmpdir, "harness.js")
    try:
        with open(jf, "w", encoding="utf-8") as f:
            f.write(code)
        proc = subprocess.run([node, jf], capture_output=True, text=True,
                              timeout=60)
        if proc.returncode != 0:
            return None, (proc.stderr or proc.stdout)[:300]
        return json.loads(proc.stdout.strip().splitlines()[-1]), None
    except Exception as exc:
        return None, str(exc)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_real_render():
    """⑧⑨⑪ canvas 真渲染（喂脏数据）+ 空态"""
    dirty = {
        "dates": ["2024/01/02", "2024/01/03", "2024/01/04",
                  "2024/01/05", "2024/01/08"],
        "balances": [16500, 16800, 16000, 17200, 15500],
        "stats": {"peak": 17200, "current": 15500, "peak_ratio": 90.12,
                  "peak_date": "2024/01/05", "current_date": "2024/01/08"},
        "amounts": [999999, 888888],
        "RZYE": 777777,
        "financing_balance": 666666,
    }
    out, err = _run_node(NODE_HARNESS,
                         ("fmtAmt", "fmtAxisDate", "renderMarginChart"),
                         {"data": dirty})
    if err:
        check(False, "⑧ node 真渲染 harness 执行失败", err)
        return

    check(out["peak"] == "1.72 万亿" and out["current"] == "1.55 万亿",
          "⑧ 峰值/当前余额按亿元→万亿口径显示",
          f"peak={out['peak']} current={out['current']}")
    check(out["shrink"] == "90.12%", "⑧ 缩至峰值占比",
          out["shrink"])
    check(out["strokes"] >= 1 and out["fills"] >= 1 and out["arcs"] >= 1,
          "⑨ 画布画出曲线 + 面积 + 峰值/当前点",
          f"strokes={out['strokes']} fills={out['fills']} arcs={out['arcs']}")
    axis = ["24/01/02", "24/01/04", "24/01/08"]
    check(all(a in out["texts"] for a in axis)
          and sum(1 for t in out["texts"] if re.match(r"^\d{2}/\d{2}/\d{2}$", t)) == 3,
          "⑨ 日期轴首/中/尾 3 个标签，格式与市场量能同源",
          str(out["texts"]))
    check("峰值 1.72 万亿" in out["texts"],
          "⑨ 峰值点标注数值与 stats.peak 同源（防渲染读错序列字段）",
          str(out["texts"]))
    joined = " ".join([out["peak"], out["current"], out["shrink"],
                       out["empty"]] + out["texts"])
    check(all(w not in joined for w in ("999999", "888888", "777777",
                                        "666666", "amounts", "RZYE",
                                        "financing_balance")),
          "⑩ 脏数据（另一个面板字段/仅融资余额/第三方字段）一个都不上屏",
          joined)
    check("无两融余额数据" in out["empty2"]
          and out["empty2Display"] == "block"
          and out["empty2Strokes"] == 0,
          "⑪ 空序列显示空态且不画曲线", str(out))


def test_button_availability():
    """⑫ 三按钮可用性状态机（2026-11-21 用户口径）

    ⑴ 期货页三个按钮全灰；⑵ 股票页仅日K 放行「两融余额」「市场量能」；
    ⑶ 股票页「股票扫描」恒可用、与周期无关；⑷ 扫描**进行中**切到期货页时扫描
    按钮不灰（守卫 `if (!_scanRunning)` 不抢），保留中断入口。判据不认标的
    （个股 / 指数 / ETF 同）。真函数 + DOM 桩实跑：disabled / title / 点击行为 /
    切灰化关面板。
    """
    def cd(sym, market=None, dates=("2026-11-18", "2026-11-19", "2026-11-20")):
        meta = {"symbol": sym}
        if market:
            meta["market"] = market
        return {"meta": meta, "klines": [{"date": d} for d in dates]}

    fut_dates = ("2026-11-20 09:01", "2026-11-20 09:02", "2026-11-20 14:59")
    scenarios = [
        {"tag": "stock_d", "freq": "d", "futures": False, "chartData": cd("sh600519")},
        {"tag": "index_d", "freq": "d", "futures": False, "chartData": cd("sh000001")},
        {"tag": "etf_d", "freq": "d", "futures": False, "chartData": cd("sh510300")},
        {"tag": "stock_30m", "freq": "30m", "futures": False, "chartData": cd("sh600519")},
        {"tag": "stock_w", "freq": "w", "futures": False, "chartData": cd("sh600519")},
        {"tag": "fut_1m", "freq": "1m", "futures": True,
         "chartData": cd("IF2312", "futures", fut_dates)},
        {"tag": "fut_30m", "freq": "30m", "futures": True,
         "chartData": cd("IF2312", "futures", fut_dates)},
        # 扫描中切到期货页（2026-11-21 用户问：启动扫描后能不能切页）：
        # syncDataBtnState 的 `if (!_scanRunning)` 守卫此时**不抢**按钮，
        # 按钮保持可点 = 保留中断入口。
        {"tag": "fut_1m_scanning", "freq": "1m", "futures": True,
         "scanRunning": True, "chartData": cd("IF2312", "futures", fut_dates)},
    ]
    # True = 灰化不可用
    EXPECT = {
        "stock_d":   {"scan": False, "amo": False, "margin": False},
        "index_d":   {"scan": False, "amo": False, "margin": False},
        "etf_d":     {"scan": False, "amo": False, "margin": False},
        "stock_30m": {"scan": False, "amo": True,  "margin": True},
        "stock_w":   {"scan": False, "amo": True,  "margin": True},
        "fut_1m":    {"scan": True,  "amo": True,  "margin": True},
        "fut_30m":   {"scan": True,  "amo": True,  "margin": True},
        "fut_1m_scanning": {"scan": False, "amo": True, "margin": True},
    }
    out, err = _run_node(BUTTON_HARNESS, ("dataBtnAvailability", "syncDataBtnState"),
                         {"scenarios": scenarios},
                         win_funcs=("toggleAmoPanel", "toggleMarginPanel"))
    if err:
        check(False, "⑫ node 可用性状态机 harness 执行失败", err)
        return
    rows = {r["tag"]: r for r in out["rows"]}
    check(len(rows) == len(scenarios), "⑫ %d 个场景都跑到" % len(scenarios), str(list(rows)))

    for s in scenarios:
        tag, cn = s["tag"], s["tag"]
        r, want = rows[tag], EXPECT[tag]
        got = {"scan": r["scan"], "amo": r["amo"], "margin": r["margin"]}
        check(got == want, f"⑫ {cn}：disabled 三元组 = {want}", str(got))
        # title：灰化的必须写明原因，可用的回到静态口径（无「仅」字样）
        ok_title = True
        for k, base in (("scan", "扫描股票买卖点"), ("amo", "盘后量能"),
                        ("margin", "两融余额")):
            t = r[k + "Title"]
            if want[k]:
                ok_title = ok_title and ("仅" in t and base in t)
            else:
                ok_title = ok_title and t.startswith(base) and "仅" not in t
        check(ok_title, f"⑫ {cn}：title 随可用性切换（灰化带原因）",
              f"scan={r['scanTitle']!r} amo={r['amoTitle']!r} margin={r['marginTitle']!r}")
        # 点击行为：灰化 → 开不了且不取数；可用 → 开 + 取 1 次 + 再点关掉
        if want["amo"]:
            amo_ok = r["amoOpened"] is False and r["amoFetches"] == 0
        else:
            amo_ok = (r["amoOpened"] is True and r["amoFetches"] == 1
                      and r["amoToggledOff"] is True)
        if want["margin"]:
            margin_ok = r["marginOpened"] is False and r["marginFetches"] == 0
        else:
            margin_ok = (r["marginOpened"] is True and r["marginFetches"] == 1
                         and r["marginToggledOff"] is True)
        check(amo_ok and margin_ok, f"⑫ {cn}：点击行为与可用性一致（灰化点不开）",
              str(r))

    # 扫描中切到期货页（2026-11-21 用户问「启动扫描后能不能切到期货页」）：
    # 能切 —— 扫描是独立面板 + 后端任务，不依赖当前图；此时 syncDataBtnState 的
    # `if (!_scanRunning)` 守卫不抢按钮，按钮保持可点（= 保留中断入口），
    # title 也从「仅股票页面可用」改为「点击中断」，避免与亮着的按钮自相矛盾。
    # 扫描一结束 _setScanRunning(false) → syncDataBtnState() 会把按钮按期货页灰化。
    sc = rows["fut_1m_scanning"]
    check(sc["scan"] is False and "点击中断" in sc["scanTitle"],
          "⑫ 扫描中切到期货页：扫描按钮仍可点（= 中断入口），title 说明点击语义",
          str(sc))
    check(rows["fut_1m"]["scan"] is True,
          "⑫ 对照：同一期货页、非扫描态 → 扫描按钮灰化", str(rows["fut_1m"]))

    ac = out["autoClose"]
    check(ac["amoOpened"] is True and ac["amoClosed"] is True,
          "⑫ 开着「市场量能」切到灰化态（期货）→ 面板立即关闭", str(ac))
    check(ac["marginOpened"] is True and ac["marginClosed"] is True,
          "⑫ 开着「两融余额」切到灰化态（非日K）→ 面板立即关闭", str(ac))
def test_p31_refresh_hook():
    """⑬ 切图后按需重取打开着的数据面板（2026-11-21 P3-1 闭环）

    旧自动关闭逻辑寄生在被删的 updateAmoButtonState/updateMarginButtonState 里
    （由 updateFreqButtonStates 调用），取消灰化时一并消失 —— 键盘 Enter 切票后
    面板保持打开且显示上一视口区间的陈旧序列。修复 = 图表装载成功分支调用
    refreshOpenDataPanels：面板开着就按新视口重取，关着不预取。

    **只挂 1 处**（loadStock 股票成功分支）：期货装载路径（单窗 / 双窗 init）的
    updateFreqButtonStates(true) → syncDataBtnState → av.panels=false 已经把两个
    面板关掉，那两处调用恒为 no-op（2026-11-21 探针 0/0 vs 对照 1/1）。原护栏
    写「调用点恰 3 处」，等于把死代码钉成契约，已一并收窄为 1 处。
    """
    src = read(APP_JS)
    lines = src.split("\n")
    n_def = sum(1 for ln in lines if "function refreshOpenDataPanels(" in ln)
    check(n_def == 1, "⑬ refreshOpenDataPanels 定义恰 1 处", str(n_def))
    marks = [i for i, ln in enumerate(lines)
             if "refreshOpenDataPanels(); // [P3-1]" in ln]
    check(len(marks) == 1, "⑬ 调用点恰 1 处（标记注释 [P3-1]）", str(marks))

    # 唯一那一处必须紧跟自己的装载收尾语句（防被挪到装载完成之前）
    def _adjacent(prev_pat):
        return [i for i in range(1, len(lines))
                if prev_pat in lines[i - 1]
                and "refreshOpenDataPanels(); // [P3-1]" in lines[i]]

    check(len(_adjacent("syncOverlayControls();")) == 1,
          "⑬ 股票成功分支：紧跟 syncOverlayControls（视口已重算）之后")
    # 不另设「期货路径不得挂」的反向断言：`updateFreqButtonStates(true)` 在换品种 /
    # 换周期 / 双窗切换里共 6 处，逐段扫会误伤将来合法的挂点；「恰 1 处」+ 上面
    # 「只此一处紧跟 syncOverlayControls」已经等价于「期货两处没有挂」。

    # 每个调用点上方 45 行内必须有视口重算：证明它挂在「数据与视口都已就位」
    # 之后，而不是装载流程中段
    for i in marks:
        above = "\n".join(lines[max(0, i - 45):i])
        check("viewOffset = Math.max(0," in above,
              "⑬ 第 %d 行调用点上方 45 行内有视口重算" % (i + 1))

    scenarios = [
        {"tag": "both_closed", "amo": False, "margin": False},
        {"tag": "amo_open", "amo": True, "margin": False},
        {"tag": "both_open", "amo": True, "margin": True},
    ]
    out, err = _run_node(PANEL_REFRESH_HARNESS, ("refreshOpenDataPanels",),
                         {"scenarios": scenarios})
    if err:
        check(False, "⑬ node 重取 harness 执行失败", err)
        return
    by = {r["tag"]: r for r in out}
    check(len(by) == 3, "⑬ 3 个开合场景都跑到", str(out))
    check(by["both_closed"]["amoFetches"] == 0
          and by["both_closed"]["marginFetches"] == 0,
          "⑬ 面板都关着：重取钩子不预取（0 次取数）", str(by["both_closed"]))
    check(by["amo_open"]["amoFetches"] == 1
          and by["amo_open"]["marginFetches"] == 0,
          "⑬ 只开市场量能：只重取市场量能（1 次）", str(by["amo_open"]))
    check(by["both_open"]["amoFetches"] == 1
          and by["both_open"]["marginFetches"] == 1,
          "⑬ 两个都开着：各重取 1 次", str(by["both_open"]))


def test_margin_completeness():
    """⑭ 末点完整性（2026-11-21）：残缺日整日剔除，曲线止于最新完整日

    实测形态：2026-11-20 的 F10 只回了沪市一行（012001 = 11,394.48 亿），
    按日求和得到约一半的假余额 —— 画出来就是末点垂直下扎。判据是「当日市场集合
    不得是上一个已接纳日的严格子集」，与「哪一天」「哪个市场」都无关。
    """
    from App import AppMargin

    def row(d, tag, yuan):
        return {"N001": d, "N010": tag, "N005": yuan}

    H, S, B = "012001", "012002", "012046"

    def full(d, base):
        return [row(d, H, base), row(d, S, base * 0.9), row(d, B, base * 0.01)]

    # 11-19 完整 → 11-20 只剩沪市（实测形态）
    rows = (full("2026-11-18", 1.20e12) + full("2026-11-19", 1.19e12)
            + [row("2026-11-20", H, 1.18e12)])
    got = AppMargin._parse_rows(rows)
    check([d for d, _ in got] == ["2026-11-18", "2026-11-19"],
          "⑭ 只有沪市的残缺末点整日剔除（曲线止于 11-19）",
          str([d for d, _ in got]))
    check(abs(got[-1][1] - 1.19e12 * 1.91) < 1e6,
          "⑭ 留下来的仍是三市合计（不是只剩沪市那一半）", str(got[-1][1]))

    # 中段残缺日同样剔除，其后的完整日不受影响
    rows2 = (full("2026-11-17", 1.20e12) + [row("2026-11-18", H, 1.10e12)]
             + full("2026-11-19", 1.10e12))
    got2 = AppMargin._parse_rows(rows2)
    check([d for d, _ in got2] == ["2026-11-17", "2026-11-19"],
          "⑭ 中段残缺日剔除且不影响后续完整日", str([d for d, _ in got2]))

    # 北交所 2023-02-13 才有数据：集合变大不算残缺
    rows3 = ([row("2023-02-10", H, 1e12), row("2023-02-10", S, 9e11)]
             + [row("2023-02-13", H, 1e12), row("2023-02-13", S, 9e11),
                row("2023-02-13", B, 1e10)])
    got3 = AppMargin._parse_rows(rows3)
    check([d for d, _ in got3] == ["2023-02-10", "2023-02-13"],
          "⑭ 市场集合变大（北交所上线）不算残缺", str([d for d, _ in got3]))

    # 脏值 / 未知市场标记的老规矩不变（负数不覆盖同日好值、未知标记不计入）
    rows4 = (full("2026-11-19", 1.20e12)
             + [row("2026-11-19", H, -5), row("2026-11-19", "999999", 1e12)])
    got4 = AppMargin._parse_rows(rows4)
    check(len(got4) == 1 and abs(got4[0][1] - 1.20e12 * 1.91) < 1e6,
          "⑭ 负数 / 未知市场标记照旧丢弃，不重复累加", str(got4))

def main():
    print("===== 两融余额面板展示契约护栏 =====")
    test_static()
    test_real_render()
    test_button_availability()
    test_p31_refresh_hook()
    test_margin_completeness()
    print(f"===== 合计 {len(_FAILURES)} 项失败 =====")
    if _FAILURES:
        for f in _FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("===== 全部通过 =====")
    sys.exit(0)


if __name__ == "__main__":
    main()

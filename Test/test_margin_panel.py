# -*- coding: utf-8 -*-
"""
「两融余额」按钮与面板的展示契约护栏（静态层 + 真渲染层）
=====================================================================
需求原文：在「市场量能」选项卡前增加「两融余额」按钮，点按钮后生成类似
「市场量能」那样的图形；两融余额指全 A 股（沪深北三市）的两融余额。
v2 追加：两融余额按钮的可用性与「市场量能」一致（仅上证指数日K可用）。

静态层（零浏览器、零网络）：
  ① 按钮顺序：btn-margin 必须排在 btn-amo 之前
  ② 按钮文案 / 内联事件 / 面板骨架（canvas + 三格统计 + 空态）
  ③ 面板标题写明沪 / 深 / 北三市口径
  ④ 样式与市场量能同源（同一个 #amo-chart 规则带上 #margin-chart）
  ⑤ 前端组件区块 + 取数接口 + 与市场量能互斥
  ⑥ 后端路由 / AppOrch re-export / eltdx N005 三市求和口径
  ⑦ phase6 window API 面登记（新增两个内联事件桥）
  ⑫ 按钮恒可用契约（2026-11-20 取消「仅上证指数日K可用」灰化）：toggle 里没有
     前置可用性判据、按钮上除「点面板外关闭」外无第二处 getElementById（即无
     disabled / title 运行时写入点）、title 只写数据口径、底部槽位判据未被动到

真渲染层（node 抽 app.js 真函数 + canvas/DOM 桩，喂脏数据）：
  ⑧ 三格统计逐字正确（峰值 / 当前 / 缩至峰值）
  ⑨ 画布画了曲线 + 面积 + 峰值点 + 3 个日期标签（与市场量能同形态）
  ⑩ 脏数据（amounts / RZYE / financing_balance）一个都不上屏
  ⑪ 空序列 → 空态文案，不画曲线
  ⑫ 真跑两个按钮的点击路径：上证指数 / 个股 / ETF / 期指四场景都要能开面板
     并各取一次数，且全程不改按钮 disabled / title
  ⑬ 切图后按需重取（2026-11-21 P3-1 闭环）：refreshOpenDataPanels 定义恰 1 处、
     调用点恰 3 处且各紧跟装载收尾语句（其上方 45 行内必有视口重算）；真函数
     行为三态 —— 关着不预取 / 开哪个取哪个 / 两个都开各取 1 次

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
    check(i_margin >= 0 and i_amo >= 0 and i_margin < i_amo,
          "① 两融余额按钮在市场量能按钮之前", f"margin={i_margin} amo={i_amo}")
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
          "⑤ 两个按钮状态函数已删除（切标的 / 切周期不再改按钮 disabled/title）")

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

    # ⑫ 可用性契约：两个按钮恒可用（2026-11-20 取消「仅上证指数日K可用」灰化）
    html_title = re.search(r'id="btn-margin"[^>]*title="([^"]*)"', html)
    amo_html_title = re.search(r'id="btn-amo"[^>]*title="([^"]*)"', html)
    check(bool(html_title) and "仅上证指数日K可用" not in html_title.group(1)
          and "全A股沪深北三市" in html_title.group(1),
          "⑫ 两融余额按钮 title 只写数据口径，无「仅上证指数日K可用」",
          html_title.group(1) if html_title else "缺 title")
    check(bool(amo_html_title)
          and "仅上证指数日K可用" not in amo_html_title.group(1),
          "⑫ 市场量能按钮 title 同上",
          amo_html_title.group(1) if amo_html_title else "缺 title")
    # 打开面板不许有前置可用性判据：否则按钮不灰化、看着可点，点了却没反应
    check("amoIsAvailable" not in toggle_amo,
          "⑫ toggleAmoPanel 无「仅指数日K」前置判据")
    check("marginIsAvailable" not in toggle_margin,
          "⑫ toggleMarginPanel 无「仅指数日K」前置判据")
    # 按钮上不许有第二处写入点（disabled / title 运行时覆写都算）
    check(js.count('"btn-amo"') == 1 and js.count('"btn-margin"') == 1,
          "⑫ 两个按钮各只有 1 处 getElementById（点击面板外关闭时用），"
          "无第二个改写点",
          f'btn-amo x{js.count(chr(34) + "btn-amo" + chr(34))} '
          f'btn-margin x{js.count(chr(34) + "btn-margin" + chr(34))}')
    check("marginIsAvailable(" not in js,
          "⑫ 按钮专用判据 marginIsAvailable 已删（无死代码）")
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
function mkBtn(id){
    // disabled / title 故意初始化成「从未被写过」的样子：
    // 任何运行时改写都会在结果里露出来（旧实现会写 !available 和提示文案）
    return {id: id, disabled: false, title: "",
            contains: function(){ return false; }};
}
var els = {
    "amo-panel": mkPanel("amo"), "margin-panel": mkPanel("margin"),
    "btn-amo": mkBtn("btn-amo"), "btn-margin": mkBtn("btn-margin")
};
var document = {getElementById: function(id){ return els[id] || null; }};
var window = {};
var chartData = null, currentFreq = null;
function loadAmoData(){ calls.amo++; }
function loadMarginData(){ calls.margin++; }
function closeAmoPanel(){ show.amo = false; }
function closeMarginPanel(){ show.margin = false; }
// 可用性判据恒为 false（模拟「非上证指数日K」）：谁把判据加回按钮路径，
// 面板就打不开，断言会直接指到那一行，而不是变成 ReferenceError。
function amoIsAvailable(){ return false; }
function marginIsAvailable(){ return false; }
%FUNCS%
var out = [];
SCENARIOS.forEach(function(s){
    chartData = s.chartData;
    currentFreq = s.freq;
    show.amo = false; show.margin = false;
    calls.amo = 0; calls.margin = 0;

    // 市场量能：一次点击 = 开面板 + 取一次数；再点一次 = 关
    window.toggleAmoPanel();
    var r = {tag: s.tag, amoOpened: show.amo, amoFetches: calls.amo,
             amoDisabled: els["btn-amo"].disabled,
             amoTitle: els["btn-amo"].title};
    window.toggleAmoPanel();
    r.amoClosedAgain = (show.amo === false);

    // 两融余额：同上
    window.toggleMarginPanel();
    r.marginOpened = show.margin;
    r.marginFetches = calls.margin;
    r.marginDisabled = els["btn-margin"].disabled;
    r.marginTitle = els["btn-margin"].title;
    window.toggleMarginPanel();
    r.marginClosedAgain = (show.margin === false);
    out.push(r);
});
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


def test_button_always_enabled():
    """⑫ 真跑两个按钮的点击路径：任何标的/周期都要能开面板取数、且不改按钮状态

    这里的可用性判据桩（amoIsAvailable / marginIsAvailable）**恒返回 false**，
    等价于「当前打开的是个股 / ETF / 期指」。它们还留在按钮路径上，面板就打不开。
    """
    def cd(sym, market=None, dates=("2026-11-18", "2026-11-19", "2026-11-20")):
        meta = {"symbol": sym}
        if market:
            meta["market"] = market
        return {"meta": meta, "klines": [{"date": d} for d in dates]}

    scenarios = [
        {"tag": "index_daily", "freq": "d", "chartData": cd("sh000001")},
        {"tag": "stock_daily", "freq": "d", "chartData": cd("sh600519")},
        {"tag": "etf_daily", "freq": "d", "chartData": cd("sh510300")},
        # 期指 1 分钟：k.date 带时分，getViewportDateRange 只 slice(0,10)
        {"tag": "index_future_1m", "freq": "1m",
         "chartData": cd("IF2312", "futures",
                         ("2026-11-20 09:01", "2026-11-20 09:02",
                          "2026-11-20 14:59"))},
    ]
    out, err = _run_node(BUTTON_HARNESS, (), {"scenarios": scenarios},
                         win_funcs=("toggleAmoPanel", "toggleMarginPanel"))
    if err:
        check(False, "⑫ node 点击路径 harness 执行失败", err)
        return
    by = {r["tag"]: r for r in out}
    check(len(by) == 4, "⑫ 4 种标的/周期场景都跑到", str(out))

    LABEL = {"index_daily": "上证指数日K", "stock_daily": "个股日K",
             "etf_daily": "ETF 日K", "index_future_1m": "期指 1 分钟"}
    for tag, cn in LABEL.items():
        r = by[tag]
        check(r["amoOpened"] is True and r["amoFetches"] == 1,
              f"⑫ {cn}：点「市场量能」即开面板并取一次数", str(r))
        check(r["marginOpened"] is True and r["marginFetches"] == 1,
              f"⑫ {cn}：点「两融余额」即开面板并取一次数", str(r))
        check(r["amoClosedAgain"] is True and r["marginClosedAgain"] is True,
              f"⑫ {cn}：再点一次正常关闭（toggle 语义未被改坏）", str(r))
        check(r["amoDisabled"] is False and r["marginDisabled"] is False,
              f"⑫ {cn}：两个按钮的 disabled 全程没被写过（不灰化）", str(r))
        check(r["amoTitle"] == "" and r["marginTitle"] == "",
              f"⑫ {cn}：两个按钮的 title 全程没被运行时覆写（静态值唯一来源）",
              str(r))


def test_p31_refresh_hook():
    """⑬ 切图后按需重取打开着的数据面板（2026-11-21 P3-1 闭环）

    旧自动关闭逻辑寄生在被删的 updateAmoButtonState/updateMarginButtonState 里
    （由 updateFreqButtonStates 调用），取消灰化时一并消失 —— 键盘 Enter 切票后
    面板保持打开且显示上一视口区间的陈旧序列。修复 = 图表装载成功分支
    （loadStock 股票 / 期货单窗 init / 期货双窗 init，共 3 处）调用
    refreshOpenDataPanels：面板开着就按新视口重取，关着不预取。
    """
    src = read(APP_JS)
    lines = src.split("\n")
    n_def = sum(1 for ln in lines if "function refreshOpenDataPanels(" in ln)
    check(n_def == 1, "⑬ refreshOpenDataPanels 定义恰 1 处", str(n_def))
    marks = [i for i, ln in enumerate(lines)
             if "refreshOpenDataPanels(); // [P3-1]" in ln]
    check(len(marks) == 3, "⑬ 调用点恰 3 处（标记注释 [P3-1]）", str(marks))

    # 每个调用点的上一行必须是各自装载路径的收尾语句（防被挪到装载完成之前）
    def _adjacent(prev_pat):
        return [i for i in range(1, len(lines))
                if prev_pat in lines[i - 1]
                and "refreshOpenDataPanels(); // [P3-1]" in lines[i]]

    check(len(_adjacent("syncOverlayControls();")) == 1,
          "⑬ 股票成功分支：紧跟 syncOverlayControls（视口已重算）之后")
    check(len(_adjacent("generateStats();")) == 1,
          "⑬ 期货单窗 init：紧跟 generateStats（装载收尾）之后")
    check(len(_adjacent("render();")) == 1,
          "⑬ 期货双窗 init：紧跟 render（装载收尾）之后")

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


def main():
    print("===== 两融余额面板展示契约护栏 =====")
    test_static()
    test_real_render()
    test_button_always_enabled()
    test_p31_refresh_hook()
    print(f"===== 合计 {len(_FAILURES)} 项失败 =====")
    if _FAILURES:
        for f in _FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("===== 全部通过 =====")
    sys.exit(0)


if __name__ == "__main__":
    main()

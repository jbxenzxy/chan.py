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
  ⑫ 按钮可用性契约：marginIsAvailable 复用 amoIsAvailable，
     HTML 静态 title 与 JS 运行时覆写逐字相同，旧「有K线即可用」零残留

真渲染层（node 抽 app.js 真函数 + canvas/DOM 桩，喂脏数据）：
  ⑧ 三格统计逐字正确（峰值 / 当前 / 缩至峰值）
  ⑨ 画布画了曲线 + 面积 + 峰值点 + 3 个日期标签（与市场量能同形态）
  ⑩ 脏数据（amounts / RZYE / financing_balance）一个都不上屏
  ⑪ 空序列 → 空态文案，不画曲线
  ⑫ 真跑两个按钮的状态函数：4 种视口场景逐项核 disabled/title/关闭面板

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
    for fn in ("marginIsAvailable", "updateMarginButtonState",
               "loadMarginData", "renderMarginChart"):
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
    check("            updateMarginButtonState();" in js,
          "⑤ 按钮可用性与周期切换同步")

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

    # ⑫ 可用性契约（静态层）
    margin_avail = js[js.find("function marginIsAvailable"):
                      js.find("function updateMarginButtonState")]
    check("return amoIsAvailable();" in margin_avail,
          "⑫ marginIsAvailable 直接复用 amoIsAvailable（可用性单一 SSOT）")
    check("chartData.klines" not in margin_avail and "请先加载K线" not in js,
          "⑫ 旧「只要有K线就可用」口径零残留")
    margin_state = js[js.find("function updateMarginButtonState"):
                      js.find("window.toggleMarginPanel")]
    js_unavailable = re.search(
        r'btn\.title = available \? "[^"]*" : "([^"]*)";', margin_state)
    html_title = re.search(r'id="btn-margin"[^>]*title="([^"]*)"', html)
    amo_html_title = re.search(r'id="btn-amo"[^>]*title="([^"]*)"', html)
    check(bool(js_unavailable) and bool(html_title)
          and js_unavailable.group(1) == html_title.group(1),
          "⑫ HTML 静态 title 与 JS 不可用覆写逐字相同（两写入口同口径）",
          f"html={html_title.group(1) if html_title else None} "
          f"js={js_unavailable.group(1) if js_unavailable else None}")
    check(bool(html_title) and bool(amo_html_title)
          and "仅上证指数日K可用" in html_title.group(1)
          and "仅上证指数日K可用" in amo_html_title.group(1),
          "⑫ 两个按钮的首屏不可用提示与「市场量能」同口径")


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
var results = [];
var closeAmo = 0, closeMargin = 0, panelShow = false;
function closeAmoPanel(){ closeAmo++; }
function closeMarginPanel(){ closeMargin++; }
function makeBtn(id){
    return {id: id, disabled: null, title: "",
            classList: {add: function(){}, remove: function(){},
                        contains: function(){ return panelShow; }}};
}
var els = {};
["btn-amo", "btn-margin", "amo-panel", "margin-panel"].forEach(
    function(i){ els[i] = makeBtn(i); });
var document = {getElementById: function(id){ return els[id] || null; }};
var chartData = null, currentFreq = null;
var window = {};
%FUNCS%
SCENARIOS.forEach(function(s){
    chartData = s.chartData;
    currentFreq = s.freq;
    panelShow = !!s.panelOpen;
    closeAmo = 0; closeMargin = 0;
    updateAmoButtonState();
    updateMarginButtonState();
    results.push({tag: s.tag,
        amoDisabled: els["btn-amo"].disabled,
        marginDisabled: els["btn-margin"].disabled,
        amoTitle: els["btn-amo"].title,
        marginTitle: els["btn-margin"].title,
        closeAmo: closeAmo, closeMargin: closeMargin});
});
console.log(JSON.stringify(results));
"""


def _run_node(harness, funcs, payload):
    """把真函数注入 node harness，跑一次并返回 JSON 结果。"""
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
    """⑫ 真跑两个按钮状态函数：4 种视口场景逐项核 disabled/title/关面板"""
    scenarios = [
        {"tag": "sh000001_d", "panelOpen": False, "freq": "d",
         "chartData": {"meta": {"symbol": "sh000001"}, "klines": [1, 2, 3]}},
        {"tag": "other_symbol", "panelOpen": True, "freq": "d",
         "chartData": {"meta": {"symbol": "sz399001"}, "klines": [1, 2, 3]}},
        {"tag": "non_daily", "panelOpen": True, "freq": "60m",
         "chartData": {"meta": {"symbol": "sh000001"}, "klines": [1, 2, 3]}},
        # 关键反向场景：有 K 线但不是上证指数日K，旧实现会误判为可用
        {"tag": "klines_only", "panelOpen": True, "freq": "d",
         "chartData": {"klines": [1, 2, 3]}},
    ]
    out, err = _run_node(BUTTON_HARNESS,
                         ("amoIsAvailable", "marginIsAvailable",
                          "updateAmoButtonState", "updateMarginButtonState"),
                         {"scenarios": scenarios})
    if err:
        check(False, "⑫ node 按钮状态 harness 执行失败", err)
        return
    by = {r["tag"]: r for r in out}
    check(len(by) == 4, "⑫ 4 种视口场景都跑到", str(out))

    ok = by["sh000001_d"]
    check(ok["amoDisabled"] is False and ok["marginDisabled"] is False,
          "⑫ 上证指数日K：两个按钮都可用", str(ok))
    check("全A股沪深北三市" in ok["marginTitle"],
          "⑫ 可用时提示写明全A股沪深北三市口径", str(ok["marginTitle"]))

    for tag in ("other_symbol", "non_daily", "klines_only"):
        r = by[tag]
        check(r["amoDisabled"] is True and r["marginDisabled"] is True,
              f"⑫ {tag}：两个按钮一起灰化（可用性同口径）", str(r))
        check("仅上证指数日K可用" in r["amoTitle"]
              and "仅上证指数日K可用" in r["marginTitle"],
              f"⑫ {tag}：两个按钮的不可用提示同口径", str(r))
        check(r["closeAmo"] >= 1 and r["closeMargin"] >= 1,
              f"⑫ {tag}：面板打开时变不可用 → 两个面板都关闭释放数据", str(r))

    html = read(APP_HTML)
    html_title = re.search(r'id="btn-margin"[^>]*title="([^"]*)"', html)
    amo_html_title = re.search(r'id="btn-amo"[^>]*title="([^"]*)"', html)
    check(bool(html_title) and html_title.group(1) == by["other_symbol"]["marginTitle"],
          "⑫ 首屏静态 title == JS 运行时不可用覆写（两个写入口不漂移）",
          f"html={html_title.group(1) if html_title else None}")
    check(bool(amo_html_title)
          and amo_html_title.group(1) == by["other_symbol"]["amoTitle"],
          "⑫ 「市场量能」两个写入口同样逐字相同",
          f"html={amo_html_title.group(1) if amo_html_title else None}")


def main():
    print("===== 两融余额面板展示契约护栏 =====")
    test_static()
    test_real_render()
    test_button_availability()
    print(f"===== 合计 {len(_FAILURES)} 项失败 =====")
    if _FAILURES:
        for f in _FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("===== 全部通过 =====")
    sys.exit(0)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
「盘中量能」按钮与面板的契约护栏（静态层 + 真函数层 + 后端行为层）
=====================================================================
需求原文：在「市场量能」按钮**前面**增加一个「预测量能」按钮；该按钮在
任何页面（股票 / 期货）、任何周期都可点；点击后在页面**左下角**弹出面板，
面板版式照「开盘红」App 的「沪深京预测量能」截图 1:1 复刻（蓝色标签条 /
绿色缩量 / 0 轴蓝线 + 「昨日|总量能」图例 / 绿色 y 轴刻度 / 底部注释）。
v2（2026-11-26）：按钮改名「盘中量能」，「市场量能」改名「盘后量能」，
   顶栏顺序改为「盘中量能 → 盘后量能 → 两融余额」。**面板内文字不动**：
   标题「市场量能」、「今日｜预测量能 NNNN亿」、底部注释仍是截图原文。

静态层（零浏览器、零网络）：
  ① 按钮顺序：btn-amo-pred（盘中量能）在 btn-amo（盘后量能）之前，
     btn-margin（两融余额）在 btn-amo 之后；文案「盘中量能」
  ② 恒可用：按钮标签内无 disabled；syncDataBtnState() 函数体里不出现
     btn-amo-pred（它不进「仅股票日K」那条灰化判据）
  ③ 面板骨架：en-panel / energy-date / energy-actual / energy-pred /
     energy-chart / energy-note 齐全，注释文案逐字一致
  ④ 左下角定位：.en-panel 同时写 left 与 bottom
  ⑤ 浅色版式：面板底色为白、缩量色 #0aa53c、增量色 #e33b3b
  ⑥ 接口链路：FrontAPI /api/energy/read + AppOrch.call_energy +
     AppEnergy 不在模块级 import DataAPI（分层方向不破）

真函数层（node 抽 app.js 原函数 + canvas/DOM 桩，喂真数值）：
  ⑦ 标题三行文案逐字：11-26 / 沪深京｜实际量能 8018亿（整数亿） /
     今日｜预测量能 13140亿(-5.65%,缩量787亿)
  ⑧ 放量态转红：+4.10% / 增量572亿，且面板加 .en-up
  ⑧b 末端指示点：位置钉在曲线末点（与 yOf/xOf 同算）、首屏加 .flash，
     放量态 y 轴镜像后红点仍在绘图区内
  ⑧c 红点只跟数据走：同数据重复渲染不闪（连 .flash 都清掉）、数据变了才闪，
     且位置随末点移动
  ⑨ 轮询节奏：trading→3000ms / break→30000ms / pre、closed→不轮询

后端行为层（合成数据，无网络）：
  ⑩ r(t) 表：等速放量下 r[k] = (k+1)/240，单调递增且末根为 1
  ⑪ 预测反推：P = C / r，第 k 根与第 k+1 根的比值符合 r 的比值
  ⑫ 缓存过期：收盘后「样本末日 < 今天」判 stale，盘中不判 stale；
     周末/假日 15:00 后当天只重建一次（built_at 兜底，防每请求重拉 3×2400 根）
  ⑬ 会话状态：非当日一律 closed
  ⑭ 收盘后基准不塌：r 表样本末日=当日 ⇒ 用 prev_total，不用今天自己
  ⑮ 非交易日基准：周日/法定假日打开，「昨日」= 展示日的前一交易日（不是周六、
     不是假日当天），且假日盘前/盘后同基准同 pct

运行：python Test/test_energy_panel.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

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


def _extract_fn(src, name, indent=8):
    """按函数声明起点 + 花括号配平抽出完整函数体（含 var 前缀外的函数整体）。"""
    m = re.search(r"^\s{%d}function\s+%s\s*\(" % (indent, re.escape(name)), src, re.M)
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
                return src[m.start(): i + 1]
    return None


# ═══════════════════════════════════════════════════════════════════════
# ① ~ ⑥ 静态层
# ═══════════════════════════════════════════════════════════════════════
def test_static(failures_ignored=None):
    html = read(APP_HTML)
    js = read(APP_JS)
    css = read(APP_CSS)

    print("[①] 按钮顺序与文案")
    i_pred = html.find('id="btn-amo-pred"')
    i_amo = html.find('id="btn-amo"')
    i_margin = html.find('id="btn-margin"')
    check(i_pred > 0, "app.html 存在 #btn-amo-pred")
    check(0 < i_pred < i_amo, "「盘中量能」排在「盘后量能」之前",
          f"(pred@{i_pred} amo@{i_amo})")
    check(0 < i_amo < i_margin, "「两融余额」排在「盘后量能」之后",
          f"(amo@{i_amo} margin@{i_margin})")
    m = re.search(r'<button[^>]*id="btn-amo-pred"[^>]*>(.*?)</button>', html, re.S)
    check(bool(m) and m.group(1).strip() == "盘中量能", "按钮文案为「盘中量能」",
          "" if not m else f"实测 {m.group(1)!r}")
    check(bool(m) and 'title="盘中量能（沪深京，盘中实时）"' in m.group(0),
          "title 写清口径「盘中量能（沪深京，盘中实时）」",
          "" if not m else m.group(0)[:120])
    check(bool(m) and "onclick=\"toggleEnergyPanel()\"" in m.group(0),
          "按钮内联事件为 toggleEnergyPanel()")

    print("[②] 恒可用（不进灰化名单）")
    tag = m.group(0) if m else ""
    check("disabled" not in tag, "按钮标签内没有 disabled 属性")
    body = _extract_fn(js, "syncDataBtnState") or ""
    check(body != "", "抽出 syncDataBtnState 函数体")
    check("btn-amo-pred" not in body,
          "syncDataBtnState 不碰 #btn-amo-pred（期货页/任意周期都不灰化）")
    body_av = _extract_fn(js, "dataBtnAvailability") or ""
    check(body_av != "" and "btn-amo-pred" not in body_av,
          "dataBtnAvailability 不参与本按钮判据")

    print("[③] 面板骨架与注释文案")
    for dom in ('id="energy-panel"', 'id="energy-date"', 'id="energy-actual"',
                'id="energy-pred"', 'id="energy-chart"', 'class="en-note"',
                'class="en-chart-wrap"', 'id="energy-dot"'):
        check(dom in html, f"面板 DOM 含 {dom}")
    note = "注：红色曲线代表当日预测量能增量，绿色代表缩量"
    check(note in html, "底部注释逐字一致", "" if note in html else "缺失/不一致")
    check("沪深京｜实际量能" in html, "实际量能行文案「沪深京｜实际量能」")

    print("[④] 左下角定位 + ⑤ 浅色版式")
    m_panel = re.search(r"\.en-panel\s*\{(.*?)\}", css, re.S)
    block = m_panel.group(1) if m_panel else ""
    check("position: fixed" in block, ".en-panel 为 fixed 浮层")
    check("left:" in block and "bottom:" in block, ".en-panel 同时写 left/bottom（左下角）")
    check("right:" not in block, ".en-panel 不写 right（避免与市场量能重叠在图右下）")
    check("background: #ffffff" in block, ".en-panel 底色为白（照截图，不跟站内深色主题）")
    check("#0aa53c" in css, "缩量色 #0aa53c 已定义")
    check("#e33b3b" in css and ".en-panel.en-up" in css, "放量态 .en-up 用红色 #e33b3b")

    print("[⑤b] 末端刷新指示点：平时不可见，只在 .flash 时闪一次")
    m_dot = re.search(r"\.en-dot\s*\{(.*?)\}", css, re.S)
    dot_block = m_dot.group(1) if m_dot else ""
    check("position: absolute" in dot_block, ".en-dot 绝对定位在画布覆盖层上",
          "" if m_dot else "未找到 .en-dot 规则")
    check("pointer-events: none" in dot_block, ".en-dot 不吃鼠标事件（面板外点关闭照常）")
    check("background" not in dot_block,
          ".en-dot 容器自身不画底色（旧的常驻柔光已去掉，静止时彻底看不见）", dot_block)
    check(".en-dot.show" in css, ".en-dot 默认不显示、加 .show 才显示（未出图时不留孤儿点）")
    # 关键：两个伪元素基础态必须 opacity 0 —— 不加 .flash 时红点在视觉上不存在
    m_base = re.search(r"\.en-dot::before, \.en-dot::after\s*\{(.*?)\}", css, re.S)
    check(m_base is not None and "opacity: 0" in m_base.group(1),
          "::before/::after 基础 opacity 为 0（不闪 = 看不见）",
          "" if m_base else "未找到 .en-dot::before, .en-dot::after 规则")
    check(".en-dot.pulse" not in css,
          "旧的「常驻点 + 光圈脉冲」机制已删（现在整点只在闪的瞬间存在）")
    check(".en-dot.flash::before" in css and "animation: en-dot-core" in css,
          "加 .flash 才播实心点动画")
    check(".en-dot.flash::after" in css and "animation: en-dot-pulse" in css,
          "加 .flash 才播外圈扩散动画")
    check("@keyframes en-dot-core" in css and "@keyframes en-dot-pulse" in css,
          "两层关键帧都定义了")
    m_core = re.search(r"@keyframes en-dot-core\s*\{(.*?)\}\s*\}", css, re.S)
    core = (m_core.group(1) if m_core else "").replace("\r\n", " ").replace("\n", " ")
    check("100% { opacity: 0" in core,
          "实心点末帧 opacity 0（900ms 后回到不可见，不留常驻点）", core.strip())
    check("scale(2.2)" in css, "外圈由 1 倍扩到 2.2 倍")
    check("#f5222d" in css, "红点用 #f5222d（与趋势色 #e33b3b 分开：它只表示「刚刷新」）")
    check("circle closest-side" in css, "渐变半径取 closest-side（否则默认 farthest-corner "
          "会把外圈 taper 甩到元素框外，被 clip 切出一道硬边）")
    check("width: 16px; height: 16px" in css and "width: 5px; height: 5px" in css,
          "外圈 16px / 实心点 5px（截图 1080px=360dp@3x 量出后按 plotW 比例放大）")
    check(".en-panel.en-up .en-dot::before" in css and "box-shadow: 0 0 0 1.5px #fff" in css,
          "放量态实心点补白描边（曲线本身是红的，不描边会糊在一起）")

    print("[⑥] 接口链路 / 分层方向")
    api = read(os.path.join(REPO_ROOT, "FrontAPI.py"))
    check('@router.get("/api/energy/read")' in api, "FrontAPI 注册 /api/energy/read")
    check("orch.call_energy" in api, "路由经 orch.call_energy（不直连功能域）")
    orch = read(os.path.join(REPO_ROOT, "App", "AppOrch.py"))
    check("from App.AppEnergy import call_energy" in orch, "AppOrch 引入 call_energy")
    check('"call_energy"' in orch, "AppOrch __all__ 登记 call_energy")
    energy = read(os.path.join(REPO_ROOT, "App", "AppEnergy.py"))
    check("import DataAPI" not in energy and "from DataAPI" not in energy,
          "AppEnergy 不 import DataAPI（分层方向不破）")
    check(re.search(r"^from eltdx import", energy, re.M) is None,
          "eltdx 为函数内惰性 import（缺包不影响 App 启动）")


# ═══════════════════════════════════════════════════════════════════════
# ⑦ ~ ⑨ 真函数层（node）
# ═══════════════════════════════════════════════════════════════════════
_HARNESS = r"""
var _energyTimer = null;
var _energySig = null;        // 与 app.js 同名：renderEnergyPanel 读写它判「有没有新数据」
var EN_SHRINK = "#0aa53c", EN_UP = "#e33b3b";
__FUNCS__

// ── DOM / canvas 桩：只记录被写入的文案与 class，不渲染像素 ──
var _els = {};
var _ctxCalls = [];
function _ctx() {
  var noop = function () {};
  return {
    setTransform: noop, clearRect: noop, beginPath: noop, moveTo: noop,
    lineTo: noop, stroke: noop, fill: noop, fillRect: noop, save: noop,
    restore: noop, setLineDash: noop, closePath: noop,
    measureText: function () { return { width: 96 }; },
    fillText: function (t) { _ctxCalls.push(String(t)); },
    set font(v) {}, set fillStyle(v) {}, set strokeStyle(v) {},
    set lineWidth(v) {}, set textAlign(v) {}, set textBaseline(v) {},
    set lineJoin(v) {}
  };
}
function _el(id) {
  if (!_els[id]) {
    var cls = [];
    _els[id] = {
      id: id, textContent: "", style: {},
      clientWidth: 428,
      classList: {
        add: function (c) { if (cls.indexOf(c) < 0) cls.push(c); },
        remove: function (c) { var i = cls.indexOf(c); if (i >= 0) cls.splice(i, 1); },
        toggle: function (c, on) { on ? this.add(c) : this.remove(c); },
        contains: function (c) { return cls.indexOf(c) >= 0; },
        _list: cls
      },
      getContext: function () { return _ctx(); }
    };
  }
  return _els[id];
}
var document = { getElementById: function (id) { return _el(id); } };
var window = { devicePixelRatio: 1 };

var _timerDelays = [];
var setTimeout = function (fn, ms) { _timerDelays.push(ms); return _timerDelays.length; };
var clearTimeout = function () {};

function runCase(data) {
  _ctxCalls = [];          // 逐例重置，否则上一例的刻度会串到下一例
  renderEnergyPanel(data);
  var dot = _el("energy-dot");
  return {
    date: _el("energy-date").textContent,
    actual: _el("energy-actual").textContent,
    pred: _el("energy-pred").textContent,
    up: _el("energy-panel").classList.contains("en-up"),
    ctxText: _ctxCalls.slice(),
    dotLeft: dot.style.left,
    dotTop: dot.style.top,
    dotShown: dot.classList.contains("show"),
    dotFlash: dot.classList.contains("flash")
  };
}

var base = {
  date: "11-26", status: "trading",
  actual: 8017.8, predict: 13140, yesterday: 13928.3,
  pct: -5.65, delta: -787, shrink: true, bars_done: 120,
  points: [[1, -7.73], [2, -9.85], [120, -5.65]],
  marks: [{ k: 0, label: "09:30" }, { k: 120, label: "11:30" }, { k: 240, label: "15:00" }]
};
var shrinkCase = runCase(base);
var upCase = runCase(Object.assign({}, base, {
  predict: 14500, pct: 4.1, delta: 572, shrink: false,
  points: [[1, 1.5], [120, 4.1]]
}));
// 用户口径：实际量能只留整数亿（13261.46 → 13261），不显示两位小数
var intCase = runCase(Object.assign({}, base, { actual: 13261.46, points: [[1, -7.49], [240, -4.79]] }));

scheduleEnergyPoll("trading"); scheduleEnergyPoll("break");
scheduleEnergyPoll("pre"); scheduleEnergyPoll("closed");

// ── 指纹语义：同一份数据重复渲染不闪，数据变了才闪 ──
function _snapDot() {
  var d = _el("energy-dot");
  return { flash: d.classList.contains("flash"), left: d.style.left, top: d.style.top };
}
function _copy(d) { return Object.assign({}, d); }
var moved = Object.assign({}, base, {
  actual: 8100.25, pct: -5.42, delta: -760,
  points: [[1, -7.73], [2, -9.85], [122, -5.42]]
});

_energySig = null;                       // 复位成「还没画过」
renderEnergyPanel(_copy(base));
var g1 = _snapDot().flash;               // 首屏 → 闪
var g1x = _snapDot().left;
renderEnergyPanel(_copy(base));
var g2 = _snapDot().flash;               // 同数据 → 不闪，且残影被清掉
renderEnergyPanel(moved);
var g3 = _snapDot().flash;               // 换数据 → 又闪
var g3x = _snapDot().left;
renderEnergyPanel(_copy(moved));
var g4 = _snapDot().flash;               // 再同数据 → 不闪

var out = {
  shrinkCase: shrinkCase, upCase: upCase, intActual: intCase.actual,
  sig: [g1, g2, g3, g4],
  sigLeft: [g1x, g3x],
  delays: _timerDelays,
  pctFmt: [fmtEnergyPct(-5.65), fmtEnergyPct(3.2), fmtEnergyPct(0)],
  hasEnterPoll: typeof scheduleEnergyPoll === "function"
};
console.log("__RESULT__" + JSON.stringify(out));
"""


def test_real_render():
    print("[⑦] 真渲染层（node 抽 app.js 原函数）")
    node = shutil.which("node")
    if not node:
        check(False, "node 不可用，跳过真渲染层")
        return
    src = read(APP_JS)
    funcs = []
    for name in ("fmtEnergyPct", "stopEnergyPolling", "energySig", "renderEnergyPanel",
                 "renderEnergyChart", "flashEnergyDot", "scheduleEnergyPoll"):
        fn = _extract_fn(src, name)
        if fn is None:
            check(False, f"未能从 app.js 抽出 {name}()")
            return
        funcs.append(fn)
    harness = _HARNESS.replace("__FUNCS__", "\n".join(funcs))

    tmp = tempfile.mkdtemp(prefix="energy_panel_")
    path = os.path.join(tmp, "harness.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write(harness)
    proc = subprocess.run([node, path], capture_output=True, text=True,
                          encoding="utf-8", errors="ignore", timeout=60)
    stdout = proc.stdout or ""
    if "__RESULT__" not in stdout:
        check(False, "node 执行渲染桩失败", (proc.stderr or stdout)[-400:])
        return
    import json as _json
    out = _json.loads(stdout.split("__RESULT__", 1)[1].strip())

    sc = out["shrinkCase"]
    check(sc["date"] == "11-26", "标题日期 = 11-26", f"实测 {sc['date']!r}")
    check(sc["actual"] == "8018", "实际量能取整 = 8018（8017.80 → 整数亿，不带小数）",
          f"实测 {sc['actual']!r}")
    check(out["intActual"] == "13261", "实际量能只留整数部分：13261.46 → 13261",
          f"实测 {out['intActual']!r}")
    want = "今日｜预测量能 13140亿(-5.65%,缩量787亿)"
    check(sc["pred"] == want, "缩量文案逐字一致", f"实测 {sc['pred']!r}")
    check(sc["up"] is False, "缩量态不加 .en-up")

    up = out["upCase"]
    want_up = "今日｜预测量能 14500亿(+4.10%,增量572亿)"
    check(up["pred"] == want_up, "放量文案逐字一致", f"实测 {up['pred']!r}")
    check(up["up"] is True, "放量态加 .en-up（色块与文字转红）")

    print("[⑧] 画布绘制内容")
    texts = " ".join(sc["ctxText"])
    check("昨日|总量能 13928亿" in texts, "画出「昨日|总量能」图例")
    check("09:30" in texts and "11:30" in texts and "15:00" in texts,
          "画出横轴三个刻度")
    check(any(t.endswith("%") for t in sc["ctxText"]), "画出 y 轴百分比刻度")

    print("[⑧b] 末端指示点：位置钉在曲线最后一点，新数据才加 .flash")
    def _px(s):
        try:
            return float(str(s).replace("px", ""))
        except ValueError:
            return None
    lx, ly = _px(sc["dotLeft"]), _px(sc["dotTop"])
    check(sc["dotShown"] is True, "渲染后给 .en-dot 加 .show")
    check(sc["dotFlash"] is True, "首屏渲染加 .flash（触发一次亮起→扩散→淡出）")
    # k=120 → padL(68) + 120/240 * plotW(350) = 243（与曲线同 xOf，不是随便估的）
    check(lx is not None and abs(lx - 243) < 0.01, "红点 x = 68 + 120/240×350 = 243px",
          f"实测 {sc['dotLeft']!r}")
    # cssH = round(428*0.54) = 231；绘图区 14 ~ 14+187，末点 -5.65 / minV -9.85 → 约 121px
    check(ly is not None and 14 < ly < 201, "红点 y 落在绘图区内（不在 0 轴也不在底部）",
          f"实测 {sc['dotTop']!r}")
    check(abs(ly - (14 + (-5.65 / -9.85) * 187)) < 0.5, "红点 y 与末点同用 yOf(v)",
          f"实测 {ly}，应为 {14 + (-5.65 / -9.85) * 187:.2f}")
    # 放量态也钉在同一点（红点不随趋势色改变位置）
    up_lx = _px(out["upCase"]["dotLeft"])
    up_ly = _px(out["upCase"]["dotTop"])
    check(up_lx is not None and abs(up_lx - (68 + 120 / 240 * 350)) < 0.01,
          "放量态红点同样钉在末点 x", f"实测 {out['upCase']['dotLeft']!r}")
    # 放量态 y 轴要镜像（0 贴底、向上到峰值）；旧映射只按最小值算，
    # 正的百分比会映射到 y<0 直接被画出画布外 —— 用红点 y 兜住这条回归
    check(up_ly is not None and 14 <= up_ly <= 201,
          "放量态红点 y 落在绘图区内（y 轴已镜像，不再画到画布外）",
          f"实测 {out['upCase']['dotTop']!r}")
    up_ticks = [t for t in out["upCase"]["ctxText"] if t.endswith("%")]
    check(bool(up_ticks) and all(not t.startswith("-") for t in up_ticks),
          "放量态 y 轴刻度为正（镜像后刻度符号跟着翻）", f"实测 {up_ticks}")

    print("[⑧c] 红点只跟「数据有没有变」走")
    sig = out["sig"]
    check(sig == [True, False, True, False],
          "首屏闪 → 同数据不闪 → 换数据再闪 → 再同数据不闪",
          f"实测 {sig}")
    check(out["sigLeft"][0] != out["sigLeft"][1],
          "数据变了红点跟着末点移动（不是钉死在旧位置）",
          f"实测 {out['sigLeft']}")

    print("[⑨] 轮询节奏")
    check(out["pctFmt"] == ["-5.65", "+3.20", "+0.00"], "涨跌幅格式带符号两位小数",
          f"实测 {out['pctFmt']}")
    delays = out["delays"]
    check(delays[:2] == [3000, 30000], "trading→3000ms / break→30000ms",
          f"实测 {delays[:2]}")
    check(len(delays) == 2, "pre / closed 不排轮询（不产生定时器）", f"实测 {delays}")


# ═══════════════════════════════════════════════════════════════════════
# ⑩ ~ ⑬ 后端行为层（合成数据，无网络）
# ═══════════════════════════════════════════════════════════════════════
def test_energy_behavior():
    from App import AppEnergy as E

    print("[⑩] r(t) 表：等速放量下的解析解")
    flat = {f"2026-01-{d:02d}": [1.0] * 240 for d in range(2, 9)}
    orig = E._fetch_minute_days
    E._fetch_minute_days = lambda client, code: flat
    try:
        table = E._build_ratio(None)
    finally:
        E._fetch_minute_days = orig

    bars = table["bars"]
    check(len(bars) == 240, "r 表 240 格", f"实测 {len(bars)}")
    check(abs(bars[0] - 1 / 240) < 2e-8, "r[0] = 1/240（首分钟占比）",
          f"实测 {bars[0]:.6f}")
    check(abs(bars[119] - 120 / 240) < 2e-8, "r[119] = 120/240（11:30 累计一半）",
          f"实测 {bars[119]:.6f}")
    check(abs(bars[239] - 1.0) < 2e-8, "r[239] = 1（收盘）")
    check(all(bars[i] <= bars[i + 1] + 1e-12 for i in range(239)), "r 逐格单调不减")
    check(table["samples"] == 7, "样本天数 = 合成 7 天", f"实测 {table['samples']}")

    print("[⑪] 预测反推 P = C / r")
    table2 = {"bars": [0.1, 0.2, 0.4, 1.0], "yesterday_total": 100.0,
              "prev_total": 90.0, "samples": 4}
    p1 = E._predict(table2["bars"], 10.0, 1)
    p2 = E._predict(table2["bars"], 20.0, 2)
    check(abs(p1 - 100.0) < 1e-9 and abs(p2 - 100.0) < 1e-9,
          "累计额翻倍 + r 翻倍 → 预测不变", f"实测 {p1}, {p2}")
    check(E._predict(table2["bars"], 10.0, 0) is None, "k=0 返回 None（未开盘）")
    check(E._predict(table2["bars"], 10.0, 99) is None, "k 越界返回 None")

    print("[⑫] 缓存过期判据")
    cache = {"trade_date": "2026-01-05", "bars": [0.1] * 240}
    orig_close = E._now_after_close
    try:
        E._now_after_close = lambda: True
        stale = E._is_stale(cache)
        check(isinstance(stale, bool), "_is_stale 返回布尔")
        # 收盘后：样本末日早于今天 → 该重建
        old = {"trade_date": "2000-01-01", "bars": [0.1] * 240}
        check(E._is_stale(old) is True, "收盘后样本末日落后 → 判过期")
        today = datetime.now().strftime("%Y-%m-%d")
        fresh = {"trade_date": today, "bars": [0.1] * 240}
        check(E._is_stale(fresh) is False, "收盘后样本末日=今天 → 不过期")
        # 周末 / 法定假日：样本末日推不动，重建也白重建 ⇒ 今天收盘后建过一次就别再拉
        tried = {"trade_date": "2000-01-01", "bars": [0.1] * 240,
                 "built_at": today + " 16:00:00"}
        check(E._is_stale(tried) is False,
              "周末/假日 15:00 后：今天已建过表 → 不再判过期（否则每个请求都重拉 3×2400 根）")
        premade = {"trade_date": "2000-01-01", "bars": [0.1] * 240,
                   "built_at": today + " 09:00:00"}
        check(E._is_stale(premade) is True,
              "盘前冷启动建过表：收盘后仍要重建一次（把今天收进样本）")
        E._now_after_close = lambda: False
        mid = {"trade_date": "2000-01-01", "bars": [0.1] * 240}
        check(E._is_stale(mid) is False, "盘中不因交易日推进而重建（当日未收盘）")
    finally:
        E._now_after_close = orig_close

    print("[⑬] 会话状态")
    check(E._session_status("2000-01-01") == "closed", "非当日 → closed")
    check(E._session_status("2000-01-01") in ("closed", "pre", "trading", "break"),
          "状态取值在四个枚举内")

    print("[⑭] 收盘后基准不塌：r 表样本末日=当日 ⇒ 用 prev_total，不用 today 自己")
    n = E._BARS_PER_DAY
    today = E.datetime.now().strftime("%Y-%m-%d")
    bars = [min(1.0, (k + 1) / n) for k in range(n)]
    tot_today, tot_prev = 13261.46e8, 13928.30e8
    cum = [tot_today * (k + 1) / n for k in range(n)]

    def _ratio_of(trade_date, ytot, ptot):
        return {"version": E._CACHE_VERSION, "trade_date": trade_date,
                "yesterday_total": ytot, "prev_total": ptot, "bars": bars,
                "samples": 20, "built_at": ""}

    orig_ratio, orig_live = E._get_ratio, E._live

    def _run(ratio, live):
        E._get_ratio = lambda: ratio
        E._live = lambda: live
        try:
            return E.call_energy()
        finally:
            E._get_ratio, E._live = orig_ratio, orig_live

    # 盘中：今天未收盘、不进 full，样本末日仍是昨天 ⇒ 基准 = yesterday_total
    mid = _run(_ratio_of("2026-01-05", tot_prev, 13000e8),
               {"day": today, "cum": cum[:n // 2], "snap": 0.0})
    # 收盘后：r 表重建把今天收进样本，样本末日=今天 ⇒ 基准必须回退到 prev_total
    closed = _run(_ratio_of(today, tot_today, tot_prev),
                  {"day": today, "cum": cum, "snap": 0.0})

    check(abs(mid["yesterday"] - 13928.3) < 0.01,
          "盘中「昨日」= 13928.30 亿", f"实测 {mid['yesterday']}")
    check(abs(closed["yesterday"] - 13928.3) < 0.01,
          "收盘后「昨日」仍是 13928.30 亿（不是今天自己的 13261.46）",
          f"实测 {closed['yesterday']}")
    check(closed["shrink"] is True, "收盘后仍判缩量（不会误判成放量、整条曲线转红）",
          f"实测 shrink={closed['shrink']} pct={closed['pct']}")
    check(closed["pct"] == mid["pct"],
          "收盘前后 pct 同基准（曲线不会在收盘瞬间整体抬到 0% 附近）",
          f"盘中 {mid['pct']} / 收盘 {closed['pct']}")
    check(closed["points"][-1][1] == closed["pct"],
          "曲线末点 = 面板 pct（两者用同一条基准）",
          f"末点 {closed['points'][-1]} / pct {closed['pct']}")

    print("[⑮] 非交易日：「昨日」= 展示日的前一交易日，不是周六/假日当天")

    class _FakeDT:
        """假时钟：只替 datetime.now()，其余行为照旧"""
        now_value = None

        @classmethod
        def now(cls):
            return cls.now_value

    orig_dt = E.datetime

    def _run_at(when, ratio, day, cum_):
        _FakeDT.now_value = when
        E.datetime = _FakeDT
        return _run(ratio, {"day": day, "cum": cum_, "snap": 0.0})

    tot_tue, tot_mon = 13000e8, 11000e8
    cum_tue = [tot_tue * (k + 1) / n for k in range(n)]
    try:
        # 周日 11-29 打开：最近交易日是周五 11-27，昨日必须是周四 11-26
        sun = _run_at(datetime(2026, 11, 29, 11, 0),
                      _ratio_of("2026-11-27", tot_today, tot_prev), "2026-11-27", cum)
        # 同一天，但 r 表还是周五收盘前建的（样本末日=周四）
        sun2 = _run_at(datetime(2026, 11, 29, 11, 0),
                       _ratio_of("2026-11-26", tot_prev, 12400e8), "2026-11-27", cum)
        # 周中法定假日（周三 11-25 休市，最近交易日是周二 11-24）
        hol_am = _run_at(datetime(2026, 11, 25, 10, 0),
                         _ratio_of("2026-11-24", tot_tue, tot_mon), "2026-11-24", cum_tue)
        hol_pm = _run_at(datetime(2026, 11, 25, 16, 0),
                         _ratio_of("2026-11-24", tot_tue, tot_mon), "2026-11-24", cum_tue)
    finally:
        E.datetime = orig_dt

    check(abs(sun["yesterday"] - 13928.3) < 0.01,
          "周日打开「昨日」= 周四 13928.30 亿（周五的前一交易日，绝不是周六）",
          f"实测 {sun['yesterday']}")
    check(sun["trade_date"] == "2026-11-27",
          "周日面板日期 = 最近交易日 11-27（不是周日自己）", f"实测 {sun['trade_date']}")
    check(sun["yesterday"] != sun["actual"],
          "「昨日」不等于展示日自己（否则 pct 塌成 0、整条曲线贴 0 轴）",
          f"昨日 {sun['yesterday']} / 实际 {sun['actual']}")
    check(abs(sun2["yesterday"] - 13928.3) < 0.01,
          "r 表还停在周四时，周日仍取 13928.30（改由 yesterday_total 提供）",
          f"实测 {sun2['yesterday']}")
    check(abs(hol_am["yesterday"] - 11000.0) < 0.01,
          "法定假日打开「昨日」= 周一 11000 亿（不是假日当天）",
          f"实测 {hol_am['yesterday']}")
    check(hol_am["yesterday"] == hol_pm["yesterday"] and hol_am["pct"] == hol_pm["pct"],
          "假日 10:00 与 16:00 同基准同 pct（不会在 15:00 那一刻跳变）",
          f"{hol_am['yesterday']}/{hol_am['pct']} vs {hol_pm['yesterday']}/{hol_pm['pct']}")
    check(sun["status"] == "closed" and hol_am["status"] == "closed",
          "非交易日 status=closed（前端据此停止轮询）")


# ═══════════════════════════════════════════════════════════════════════
def main():
    print("===== 盘中量能面板契约护栏 =====")
    test_static()
    test_real_render()
    test_energy_behavior()
    print(f"===== 合计 {len(_FAILURES)} 项失败 =====")
    if _FAILURES:
        for f in _FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("===== 全部通过 =====")
    sys.exit(0)


if __name__ == "__main__":
    main()

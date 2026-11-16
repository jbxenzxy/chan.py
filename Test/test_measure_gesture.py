# -*- coding: utf-8 -*-
"""K线主图「区间测量」手势护栏（2026-11-16，复审裁决 P3-1 唯一必做项）。

背景：custom-dev `6768b05`（vs tag 1116）新增 Ctrl+左键拖框测量（价差/幅度%/耗时，
天狼50 同款）。本护栏钉住两类回归面：

  ① 手势状态机（node 抽真实源码执行，桩只覆盖 DOM 壳）：
     · Ctrl+按下待定 → 4px 升级拖框 → 跟手 → 松手定格（虚线转实线语义由
       dragging 标志承担）；
     · 日期锚点落定（a0/a1/t0/t1 + f0/f1 中心偏移）：实时尾部追加 + 视口右移
       后框仍锚同一批 K 线、读数不变（价差/耗时是数据事实）；
     · 换标的/周期/市场按 _measureDataKey 作废（判身份不判对象引用）；
     · 误触过滤（升级阈值 4px + 松手**任一维** <4px 即丢弃，`||` 语义）；
     · 阈值常量从 app.js 抽取注入驱动（B0a/B0b 对角行为对 + K7 钉值，防父本
       阈值漂移对护栏不可见）；K6 钉 render→事件侧几何接线（防 _measureGeom
       登记被删后功能整体静默失效）；
     · 手势不互抢：Ctrl+点击落回原区间选择 / 双窗 Ctrl+拖动归红框 / 出界照常平移；
     · mouseleave 分态：拖拽中取消、落定保留；
     · 越界渲染：整体出界不画 / 三边夹取（含非零原点绘图区防 area.x/area.y 轴混淆
       与写死 0）/ 双窗下窗不画。
  ② click 守卫（P3-2 改一次性 flag 后的行为）：
     · 拖框 mouseup 武装 `_measureClickGuardArmed` → 浏览器补发的那次 click 被
       chip 监听器吞掉一次且**消费即复位**；
     · 任何真实点击自带 mousedown 先解除武装 → 下一次 chip 点击正常切换；
     · 纯函数边界：耗时格式化各档（无「0分0秒」尾零）、日期解析（垃圾输入返
       null 不编造）、锚点查找（hint 命中/扩散/缺失 -1）。

抽取手法：注释感知的花括号配平抽真实函数（含真实 priceToY/yToPrice 坐标映射），
与 test_crosshair_readout.py 同型；零网络、零浏览器、秒级。
跑法：python Test/test_measure_gesture.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)

APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")

_passed = 0
_failed = []


def rec(no, title, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
        print("[PASS] %s %s" % (no, title))
        if detail:
            print("        " + detail)
    else:
        _failed.append("%s %s | %s" % (no, title, detail))
        print("[FAIL] %s %s" % (no, title) + (" | " + detail if detail else ""))


def read(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


def _to_matching_brace(src, b):
    """从 '{'（下标 b）走到配对 '}' 的下标；跳过字符串/模板串/行注释/块注释。"""
    i = b + 1
    n = len(src)
    depth = 1
    while i < n:
        c = src[i]
        if c == '"' or c == "'" or c == "`":
            q = c
            i += 1
            while i < n and src[i] != q:
                if src[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError("花括号不配对")


def fn_src(src, name):
    m = re.search(r"^[ \t]*function\s+%s\s*\(" % re.escape(name), src, re.M)
    if not m:
        raise AssertionError("未找到 function %s" % name)
    b = src.index("{", m.end())
    return src[m.start():_to_matching_brace(src, b)]


def click_listener_src(src):
    """抽 `canvas.addEventListener("click", function(e) { ... })` 的匿名函数体，
    包成可执行的 `const clickListener = function (e) { ... };`（首个命中即
    单窗 chip 切换监听器，其内含守卫消费点）。"""
    m = re.search(r'canvas\.addEventListener\("click",\s*function\s*\(e\)\s*\{', src)
    if not m:
        raise AssertionError("未找到 chip click 监听器")
    b = m.end() - 1
    body = src[b:_to_matching_brace(src, b)]
    return "const clickListener = function (e) " + body + ";"


def find_node():
    # 复审3 必修4：NODE_EXE + shutil.which 已足够（本机 which 命中 WorkBuddy 自带 node）；
    # 硬编码本机路径是环境泄漏。仓库其余 5 处同款硬编码本轮一并同步清理（消除分叉）。
    cand = [os.environ.get("NODE_EXE"), shutil.which("node")]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def run_node(src, tag):
    node = find_node()
    if not node:
        rec(tag, "node 可用（离线执行真实代码段）", False, "未找到 node")
        return None
    tmp = tempfile.mkdtemp(prefix="measure_gesture_")
    p = os.path.join(tmp, "drv.js")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        r = subprocess.run([node, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode != 0:
        rec(tag, "node 执行成功", False, (r.stderr or "").strip()[:400])
        return None
    return json.loads(r.stdout)


# ══════════════════════════════════════════════════════════════════
# node 驱动：闭包状态前置声明 + 抽取的真实函数 + 场景跑批（输出 JSON）
# ══════════════════════════════════════════════════════════════════
DRIVER_PREAMBLE = r"""
// ── 闭包状态前置声明（与 app.js 内同名变量同义）──
let _mouseDownX = 0, _mouseDownY = 0;
let _measureRect = null, _measurePending = null, _measureGeom = null;
let _measureClickGuardArmed = false;
const MEASURE_MIN_DRAG = /*MEASURE_MIN_DRAG_INJECT*/4;   // 值由 build_driver 从 app.js 抽取注入
let mouseX = -1, mouseY = -1, isDragging = false, dragStartX = 0, dragStartOffset = 0;
let viewOffset = 0, viewCount = 10;
let isDualWindow = false, activeDualWindow = 'main', _ctrlPressed = false;
let dualRedRange = null, dualShowNewZs = false, dualNewZsData = null, dualOffscreenState = false, dualHighlightRange = null;
let chartData = null;
let _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
let _currentGlobalIdx = 3, _currentClipText = null, currentFreq = '5m';
let _logScale = false, _isMirrorMode = false;
const COLORS = { bi: '#4fc3f7' };
const window = globalThis; window._isRenderingBottom = false;
const calls = [];
function render() { calls.push('render'); }
function renderTop() { calls.push('renderTop'); }
function getChartArea() { return { x: 0, y: 0, w: 800, h: 400 }; }
function updateActiveWindowClass() {}
function updateSlider() {}
function updateFreqButtonStates() {}
function showToast() {}
let chipCalls = 0;
function cycleBottomSlot() { chipCalls++; }
let chipHit = 0;
function hitBottomSlotChip() { return chipHit; }
const canvas = { style: { cursor: '' }, getBoundingClientRect() { return { left: 0, top: 0 }; } };
const navigator = { clipboard: { writeText() {} } };
const ctx = {
  _rects: [], _texts: [],
  save() {}, restore() {}, setLineDash() {},
  strokeRect(x, y, w, h) { this._rects.push({ x, y, w, h }); },
  fillRect(x, y, w, h) { this._rects.push({ x, y, w, h }); },
  fillText(t) { this._texts.push({ t }); },
  measureText(s) { return { width: String(s).length * 7 }; },
  font: '', textAlign: '', textBaseline: '', strokeStyle: '', fillStyle: '',
};
const res = [];
function check(no, actual, expected) {
  // undefined 会被 JSON.stringify 丢掉键 ⇒ Python 侧 r["got"] 直接 KeyError，
  // 红项退化成 traceback 且该条之后的所有断言静默跳过（复审5 §2.2）。
  // 只在**上报字段**上折成 null；ok 必须照旧用原值比较，否则
  // check(..., undefined, null) 会被判成相等（E1/E2b/E2c/G1/A12/A13 都是 null 期望）。
  res.push({ no: no, ok: JSON.stringify(actual) === JSON.stringify(expected),
             got: actual === undefined ? null : actual,
             want: expected === undefined ? null : expected });
}
function mkChartData(n, symbol) {
  const kl = [];
  const t0 = new Date(2026, 10, 13, 9, 30).getTime();
  for (let i = 0; i < n; i++) {
    const d = new Date(t0 + i * 5 * 60000);
    const p = (x) => String(x).padStart(2, "0");
    kl.push({
      date: d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
            p(d.getHours()) + ":" + p(d.getMinutes()),
      open: 3500, high: 3510, low: 3490, close: 3500,
    });
  }
  return { meta: { symbol: symbol || "IF2606", freq: "5m", market: "futures" }, klines: kl };
}
function resetGesture() {
  _measureRect = null; _measurePending = null;
  _measureGeom = { area: AREA, priceRange: PR, barStep: 80, subPixelOffset: 0 };
  _measureClickGuardArmed = false;
  isDragging = false; viewOffset = 0; isDualWindow = false;
  _currentGlobalIdx = 3;
  _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
  chartData = mkChartData(20);
  calls.length = 0; ctx._rects.length = 0; ctx._texts.length = 0;
  chipCalls = 0; canvas.style.cursor = '';
}
const AREA = { x: 0, y: 0, w: 800, h: 400 };
const PR = { min: 3000, max: 4000 };
const ev = (x, y, ctrl, btn) => ({ clientX: x, clientY: y, ctrlKey: !!ctrl, button: btn || 0, preventDefault() {} });
"""

DRIVER_SCENARIOS = r"""
// ══ A. 纯函数边界 ══
const S = _measureSpanText;
check("A1 span 59s", S(59000), "59秒");
check("A2 span 60s", S(60000), "1分");
check("A3 span 83s", S(83000), "1分23秒");
check("A4 span 3600s", S(3600000), "1小时");
check("A5 span 3660s", S(3660000), "1小时1分");
check("A6 span 86400s", S(86400000), "1天");
check("A7 span 25h", S(90000000), "1天1小时");
check("A8 span 0", S(0), "0秒");
const DF = _measureDateMs;
check("A9 date with min", DF("2026-11-16 10:07"), new Date(2026, 10, 16, 10, 7).getTime());
check("A10 date with sec", DF("2026-11-16 10:07:30"), new Date(2026, 10, 16, 10, 7, 30).getTime());
check("A11 slash date", DF("2026/11/16"), new Date(2026, 10, 16).getTime());
check("A12 garbage", DF("abc"), null);
check("A13 empty", DF(null), null);
const FND = _measureFindAbs;
const arr5 = [0, 1, 2, 3, 4].map(i => ({ date: "d" + i }));
check("A14 find at hint", FND(arr5, "d1", 1), 1);
check("A15 find expand", FND(arr5, "d3", 1), 3);
check("A16 missing", FND(arr5, "d99", 1), -1);
check("A17 empty arr", FND([], "d1", 0), -1);

// ══ B0. 阈值行为对（对角位移：两维同值，绕开 || 的单维携带；值=app.js 注入）══
function finalizeBox() {
  resetGesture();
  onMouseDown(ev(100, 100, true));
  onMouseMove(ev(240, 200, true));
  onMouseUp(ev(240, 200, true));
  return _measureRect;
}
resetGesture();
onMouseDown(ev(100, 100, true));
onMouseMove(ev(102, 102, true));      // 两维均 2px < 4 ⇒ 不升级
check("B0a 对角2px不升级(阈值来自app.js)", !!_measureRect, false);
resetGesture();
onMouseDown(ev(100, 100, true));
onMouseMove(ev(105, 105, true));      // 两维均 5px ≥ 4 ⇒ 升级（父本阈值改大则此处红）
check("B0b 对角5px升级(阈值来自app.js)", !!(_measureRect && _measureRect.dragging), true);

// ══ B. 完整拖框手势（单窗）══
resetGesture();
onMouseDown(ev(100, 100, true));
check("B1 Ctrl按下→待定不建框不平移", { pending: !!_measurePending, dragging: isDragging, cursor: canvas.style.cursor },
    { pending: true, dragging: false, cursor: "" });
onMouseMove(ev(160, 160, true));
check("B2 位移过4px升级为拖框", { dragging: _measureRect && _measureRect.dragging, rendered: calls.length > 0 },
    { dragging: true, rendered: true });
check("B3 起点几何(p0)", [_measureRect.i0, _measureRect.p0], [0.75, 3750]);
onMouseMove(ev(240, 200, true));
check("B4 终点跟手(p1)", [_measureRect.i1, _measureRect.p1], [2.5, 3500]);
onMouseUp(ev(240, 200, true));
const m = _measureRect;
check("B5 松手定格+守卫武装(一次性flag)", { dragging: m && m.dragging, armed: _measureClickGuardArmed === true },
    { dragging: false, armed: true });
check("B6 日期锚点", { a0: m.a0, t0: chartData.klines[m.a0].date, a1: m.a1, t1: chartData.klines[m.a1].date },
    { a0: 1, t0: chartData.klines[1].date, a1: 3, t1: chartData.klines[3].date });
check("B7 中心偏移保留", [m.f0, m.f1], [-0.25, -0.5]);

// 落定后 drawMeasureRect：读数与框位（0.5 = Math.round 像素居中）
drawMeasureRect(AREA, PR, 80, 0);
const r0 = ctx._rects.find(r => r.w > 50);
check("B8 框位(未动视口)", r0, { x: 100.5, y: 100.5, w: 140, h: 100 });
check("B9 价差读数(上减下恒正)", ctx._texts[0] && ctx._texts[0].t, "250.00点");
check("B10 幅度%基数=下方点", ctx._texts[1] && ctx._texts[1].t, (250 / 3500 * 100).toFixed(2) + "%");
check("B11 耗时读数(3根5min=10分)", ctx._texts[2] && ctx._texts[2].t, "10分");

// ══ C. 实时刷新（尾部追加 + 视图右移）后框仍锚同一批 K 线 ══
chartData.klines.push({ date: "2026-11-13 12:05", open: 3500, high: 3510, low: 3490, close: 3500 });
viewOffset = 1;
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA, PR, 80, 0);
const r1 = ctx._rects.find(r => r.w > 50);
check("C1 框随视图右移1格", r1, { x: 20.5, y: 100.5, w: 140, h: 100 });
check("C2 读数不变(数据事实)", ctx._texts.map(t => t.t), ["250.00点", (250 / 3500 * 100).toFixed(2) + "%", "10分"]);

// ══ D. 换标的作废 ══
chartData = mkChartData(20, "RB2606");
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA, PR, 80, 0);
check("D1 数据身份变了不画", ctx._rects.length, 0);

// ══ D1b/D1c. 换周期 / 换市场同样作废（数据身份三要素缺一不可）══
finalizeBox();
ctx._rects.length = 0; ctx._texts.length = 0;
chartData.meta.freq = "30m";
drawMeasureRect(AREA, PR, 80, 0);
check("D1b 换周期不画", ctx._rects.length, 0);
chartData.meta.freq = "5m";
chartData.meta.market = "stock";
drawMeasureRect(AREA, PR, 80, 0);
check("D1c 换市场不画", ctx._rects.length, 0);

// ══ E. Ctrl+点击（未过阈值）落回原区间选择 ══
resetGesture();
onMouseDown(ev(100, 100, true));
onMouseMove(ev(102, 102, true));
onMouseUp(ev(102, 102, true));
check("E1 未升级→框不存在", _measureRect, null);
check("E2 Ctrl+点击保留区间选择", _rangeSelect.mode, "SELECTED_A");
check("E3 区间起点=点击处", _rangeSelect.startIdx, 3);

// ══ E2. 升级但纵向位移<4px → 误触丢弃 ══
resetGesture();
onMouseDown(ev(100, 100, true));
onMouseMove(ev(170, 102, true));
check("E2a 已升级为拖框", _measureRect && _measureRect.dragging, true);
onMouseUp(ev(170, 102, true));
check("E2b 纵向<4px按误触丢弃", _measureRect, null);

// ══ E2c. 横向<4px 也丢弃（误触 = 任一维 <4px 即丢，|| 语义；&& 变异在此判红）══
resetGesture();
onMouseDown(ev(100, 100, true));
onMouseMove(ev(101, 200, true));      // 横向1px 升级、纵向100px
onMouseUp(ev(101, 200, true));
check("E2c 横向<4px按误触丢弃", _measureRect, null);

// ══ F. 普通拖拽平移不受影响 ══
resetGesture();
onMouseDown(ev(500, 100, false));
check("F1 非Ctrl按下→平移模式", { dragging: isDragging, cursor: canvas.style.cursor, pending: !!_measurePending },
    { dragging: true, cursor: "grabbing", pending: false });
onMouseMove(ev(300, 100, false));
check("F2 平移量正确", viewOffset, 2.5);
onMouseUp(ev(300, 100, false));
check("F3 松手结束平移", isDragging, false);

// ══ G. mouseleave：拖拽中取消 / 落定保留 ══
resetGesture();
onMouseDown(ev(100, 100, true));
onMouseMove(ev(200, 200, true));
onMouseLeave();
check("G1 拖拽中移出→取消", _measureRect, null);
resetGesture();
onMouseDown(ev(100, 100, true));
onMouseMove(ev(200, 200, true));
onMouseUp(ev(200, 200, true));
onMouseLeave();
check("G2 落定后移出→保留", _measureRect && _measureRect.dragging, false);
check("G3 落定框仍有日期锚点", [_measureRect.t0 != null, _measureRect.t1 != null], [true, true]);

// ══ H. 越界不漏画：整体出图裁剪 / 下窗不画 ══
finalizeBox();
viewOffset = 10;                       // 框整体平移出绘图区左界
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA, PR, 80, 0);
check("H1 框整体出界→完全不画（三边裁剪由 H3 覆盖）", ctx._rects.length, 0);
finalizeBox();
window._isRenderingBottom = true;      // 双窗副图渲染：测量框属主图，不画
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA, PR, 80, 0);
check("H2 下窗渲染不画测量框", ctx._rects.length, 0);
window._isRenderingBottom = false;

// ══ H3. 部分出界：三边各被夹到绘图区（vR 右界不可达：_measurePointAt 夹住 mx
//        且 viewOffset 只增 ⇒ 框只会从左出界，复审3 §6 明确不要求）══
const boxOf = () => ctx._rects.filter(r => r.w > 50 && r.h > 50)[0] || null;

finalizeBox();                          // 框锚定 viewOffset=0：x=[100,240] y=[100,200]
viewOffset = 2.5;                       // 日期重定位 gs=floor(2.5)=2 → 框左移 160px，x0=-60 探出 area.x=0
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA, PR, 80, 0);
const bL = boxOf();
check("H3a 左界夹到 area.x", bL && bL.x, 0.5);

finalizeBox();                          // resetGesture 已复位 viewOffset=0
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA, { min: 3400, max: 3600 }, 80, 0);   // y0=(3600-3750)*2=-300 探出上界
const bT = boxOf();
check("H3b 上界夹到 area.y", bT && bT.y, 0.5);

finalizeBox();
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA, { min: 3600, max: 3800 }, 80, 0);   // y1=(3800-3500)*2=600 探出下界
const bB = boxOf();
check("H3d 下界夹到 area.y+area.h", bB && bB.y + bB.h, 400.5);

// ══ H3e/H3f. 非零原点绘图区（复审4 §2）：AREA 的 x=y=0 让 area.x 与 area.y
//    数值不可区分——「拿另一轴当夹取原点」「把原点写死 0」两类改动在
//    H3a~H3d 下恒绿（swap_left_origin / swap_top_origin / clip_left_noop）。
//    真实 getChartArea() 给 x=PADDING.left=10 / y=PADDING.top=20（单窗），
//    二者不等 ⇒ 可达的 10px 级可见错位。══
const AREA2 = { x: 10, y: 20, w: 800, h: 400 };

finalizeBox();
viewOffset = 2.5;                       // 日期重定位 gs=2 → x0=10-160+... 夹到 AREA2.x=10
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA2, PR, 80, 0);
const bL2 = boxOf();
check("H3e 左界夹到 area.x(=10)，未与 area.y 混淆、未写死 0", bL2 && bL2.x, 10.5);

finalizeBox();                          // resetGesture 已复位 viewOffset=0
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA2, { min: 3400, max: 3600 }, 80, 0);   // y0=20+(3600-3750)*2=-280 → 夹到 AREA2.y=20
const bT2 = boxOf();
check("H3f 上界夹到 area.y(=20)，未与 area.x 混淆", bT2 && bT2.y, 20.5);

finalizeBox();                          // resetGesture 已复位 viewOffset=0
ctx._rects.length = 0; ctx._texts.length = 0;
drawMeasureRect(AREA2, { min: 3600, max: 3800 }, 80, 0);   // y1=20+(3800-3500)*2=620 探出下界
const bB2 = boxOf();
check("H3g 下界夹到 area.y+area.h(=420)，远端不与 area.x+area.h 混淆",
      bB2 && bB2.y + bB2.h, 420.5);

// ══ I. 双窗/出界不抢手势 ══
resetGesture();
isDualWindow = true;
onMouseDown(ev(100, 100, true));
check("I1 双窗下Ctrl拖动仍归红框手势(不平移)", { pending: !!_measurePending, dragging: isDragging },
    { pending: false, dragging: true });
isDualWindow = false; isDragging = false;
onMouseDown(ev(100, 450, true));
check("I2 出界Ctrl按下→照常平移", { pending: !!_measurePending, dragging: isDragging },
    { pending: false, dragging: true });

// ══ J. click 守卫（P3-2：一次性 flag，真实执行 chip 监听器）══
resetGesture();
onMouseDown(ev(100, 100, true));
onMouseMove(ev(200, 200, true));
onMouseUp(ev(200, 200, true));            // 拖框收尾：armed=true
chipHit = 1;
clickListener({ detail: 1, clientX: 100, clientY: 100 });   // 拖框尾巴补发的那次 click
check("J1 尾巴click被吞(chip不切)+消费即复位", { chip: chipCalls, armed: _measureClickGuardArmed },
    { chip: 0, armed: false });
clickListener({ detail: 1, clientX: 100, clientY: 100 });   // 紧随的真实点击（无新 mousedown 也放行）
check("J2 复位后chip点击放行", chipCalls, 1);
resetGesture();
_measureClickGuardArmed = true;           // 模拟 armed 残留（上一手势刚拖完框）
onMouseDown(ev(500, 100, false));         // 任何真实点击必带 mousedown ⇒ 先解除
check("J3 真实点击的mousedown解除武装", _measureClickGuardArmed, false);
onMouseUp(ev(500, 100, false));
chipCalls = 0; chipHit = 1;
clickListener({ detail: 1, clientX: 100, clientY: 100 });
check("J4 解除后的chip点击不受影响", chipCalls, 1);
"""

DRIVER = DRIVER_PREAMBLE + "\n%s\n" + "\n" + DRIVER_SCENARIOS + "\nconsole.log(JSON.stringify(res));"


def build_driver(js):
    # 阈值抽取放宽到任意表达式（[^;]+）：4.5 / 2*2 等变异注入后行为对不受干扰，
    # 由 K7 钉值判红——报错形态干净（复审3 §6 顺带项）。
    m = re.search(r"const\s+MEASURE_MIN_DRAG\s*=\s*([^;]+?)\s*;", js)
    if not m:
        raise AssertionError("未找到 const MEASURE_MIN_DRAG（app.js）")
    names = ["_measureDataKey", "_measureFindAbs", "_measureAnchor", "_measureDateMs",
             "_measureSpanText", "_measurePointAt", "_armMeasure", "_startMeasure",
             "_dragMeasure", "drawMeasureRect", "onMouseDown", "onMouseMove",
             "onMouseUp", "onMouseLeave", "priceToY", "yToPrice"]
    srcs = [fn_src(js, n) for n in names] + [click_listener_src(js)]
    # 阈值注入：抽取函数引用的必须是 app.js 自己的常量（否则父本改阈值护栏不可见）
    driver = DRIVER.replace("%s", "\n\n".join(srcs))
    return driver.replace("/*MEASURE_MIN_DRAG_INJECT*/4",
                          "/*MEASURE_MIN_DRAG_INJECT*/" + m.group(1))


def test_source_contracts(js):
    """flag 判据的接线契约（行为已由 J 段真执行覆盖，这里钉防回潮）。"""
    rec("K1", "声明：一次性 flag（非时间窗）",
        "let _measureClickGuardArmed = false;" in js)
    rec("K2", "旧时间窗符号零残留",
        "_measureClickGuardUntil" not in js)
    up = fn_src(js, "onMouseUp")
    down = fn_src(js, "onMouseDown")
    rec("K3", "mouseup 测量分支武装守卫",
        "_measureClickGuardArmed = true;" in up)
    rec("K4", "mousedown 无条件解除守卫",
        "_measureClickGuardArmed = false;" in down)
    rec("K5", "chip 监听器消费即复位（吞一次不影响下一个真实点击）",
        "if (_measureClickGuardArmed) { _measureClickGuardArmed = false; return; }" in js)
    # K6：render→事件侧的几何接线（上轮变异 no_geom_registration 的假绿缺口）。
    #   render() 是薄分发器，登记块在主图渲染真身 _renderChart 内。
    #   结构断言而非双子串：「赋值挪出守卫块」变异下两个子串都还在，弱断言恒绿
    #   （复审3 §3.1 geom_outside_if）；正则钉死「赋值位于 _isRenderingBottom 块内」。
    rnd = fn_src(js, "_renderChart")
    rec("K6", "主图 _renderChart 在下窗守卫块内登记 _measureGeom",
        re.search(r"if \(!window\._isRenderingBottom\) \{\s*(?://[^\n]*\n\s*)*_measureGeom = \{", rnd) is not None)
    # K7：阈值数值钉死（配合 B0a/B0b 行为对与驱动注入——三件套才咬得住阈值漂移）
    rec("K7", "阈值常量钉死为 4px",
        "const MEASURE_MIN_DRAG = 4;" in js)


_cached = None


def main():
    global _cached
    js = read(APP_JS)
    _cached = run_node(build_driver(js), "①")
    if _cached is not None:
        rec("①", "node 抽真实源码执行（%d 项场景）" % len(_cached), True)
        for r in _cached:
            code = r["no"].split(" ", 1)[0]
            rec(code, r["no"], r["ok"],
                "got=%s want=%s" % (json.dumps(r["got"], ensure_ascii=False),
                                    json.dumps(r["want"], ensure_ascii=False)))
    test_source_contracts(js)

    total = _passed + len(_failed)
    print("\n%s 合计 %d 项：%d 通过 / %d 失败" %
          ("[FAIL]" if _failed else "[PASS]", total, _passed, len(_failed)))
    for f in _failed:
        print("  - " + f)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()

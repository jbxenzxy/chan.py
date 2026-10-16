# -*- coding: utf-8 -*-
"""底部指标区「光标横线读数」的护栏（2026-10-16）。

需求（用户原文）：
  ⑴ K 线绘图区十字线横虚线，右侧坐标轴有白框显示横线对应的数值；
  ⑵ 指标区（成交额/量、MACD、RSI）的横虚线没有 → 本次补齐。

实现：鼠标悬停在底部槽位**绘图窗**内时，横线 y 反算为该槽指标数值，
经与纵轴刻度同源的格式化后，写入与 K 线区价格读数同一个右侧白框通道
（_overlayData.rightPrice / rightY / rightW → _drawOverlayIfNeeded）。

  ① cursorText 四类读数（node 抽真实源码执行）
     · vol 柱状：与 drawVolume 的 y(v) 互逆（柱子恒从底部长起，翻转视图不翻）
     · vol 类MACD / MACD / RSI：与 macdToY / rsiToY 互逆（翻转视图镜像）
     · 格式化与纵轴刻度同源：formatVolume（万/亿/手）/ formatVolMacdVal / toFixed(2)
     · 端点 / 中点 / 越界夹回
  ② _overlayLabelW：50 是下限；长文本按实测宽度加宽（不出框）
  ③ _drawOverlayIfNeeded：不传 rightW ⇒ 恒 50（K 线区行为不变）；传了用 rightW
  ④ drawCrosshair 采集（node 抽 drawCrosshair 真函数 + 桩执行）
     · 命中槽窗 → rightPrice/rightY/rightW 全设置
     · K 线区 → 价格读数（既有行为不回归，不设 rightW）
     · 标签行 / mouseX 出区 → 不设 rightPrice
     · 双窗（_subBottomSlots）槽集合切换后读数跟着换
  ⑤ 源码契约
     · 三槽均定义 cursorText；采集块在 drawCrosshair 的 else 分支内
     · _renderChart 调用点显式传 slotAreas/slotRanges；
       slotRanges 计算先于调用点（渲染序：值域先算、后画）

零网络、零浏览器、秒级。跑法：python Test/test_crosshair_readout.py
"""
import glob
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
    with io.open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


# ══════════════════════════════════════════════════════════════════
# JS 源码抽取（花括号配对；跳过字符串 / 模板串 / 注释）
# ══════════════════════════════════════════════════════════════════
def _to_matching_brace(src, i):
    """i 指向 '{'，返回配对 '}' 之后的下标。"""
    depth, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            q, i = c, i + 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
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


def object_src(src, name):
    """抽取 `const NAME = {...};` 对象字面量（`const NAME = ` 前缀到配对 `}`）。"""
    m = re.search(r"^[ \t]*const\s+%s\s*=\s*\{" % re.escape(name), src, re.M)
    if not m:
        raise AssertionError("未找到 const %s = {" % name)
    b = m.end() - 1   # '{' 位置
    return src[m.start():_to_matching_brace(src, b)]


def find_node():
    cand = [os.environ.get("NODE_EXE"), shutil.which("node")]
    cand += glob.glob(os.path.expanduser(
        "~/.workbuddy/binaries/node/versions/*/node.exe"))
    cand += [r"C:\Program Files\nodejs\node.exe"]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def run_node(src, tag):
    node = find_node()
    if not node:
        rec(tag, "node 可用（离线执行真实代码段）", False, "未找到 node")
        return None
    tmp = tempfile.mkdtemp(prefix="crosshair_readout_")
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
# ①②③ cursorText / _overlayLabelW / _drawOverlayIfNeeded
# ══════════════════════════════════════════════════════════════════
CURSOR_TAIL = r"""
let ctx = null;
const A = { x: 10, y: 100, w: 800, h: 100 };
const R1000 = { min: 0, max: 1000 };
const R10 = { min: -10, max: 10 };
const RSI = { min: 20, max: 80 };
function t(id, y, range, opts) {
    _isMirrorMode = !!(opts && opts.mirror);
    _volDisplayMode = (opts && opts.volMode) || 'bar';
    return BOTTOM_INDICATORS[id].cursorText(y, A, range);
}
function fmt(v, futures) { isFutures = !!futures; return formatVolume(v); }
const out = {
    volBar: {
        bottom: t('vol', 200, R1000), top: t('vol', 100, R1000),
        mid: t('vol', 150, R1000), midMirror: t('vol', 130, R1000, { mirror: true })
    },
    volMacd: {
        top: t('vol', 100, R10, { volMode: 'macd' }),
        bottom: t('vol', 200, R10, { volMode: 'macd' }),
        mid: t('vol', 150, R10, { volMode: 'macd' }),
        topMirror: t('vol', 100, R10, { volMode: 'macd', mirror: true }),
        bottomMirror: t('vol', 200, R10, { volMode: 'macd', mirror: true })
    },
    macd: {
        top: t('macd', 100, R10), bottom: t('macd', 200, R10), mid: t('macd', 150, R10),
        topMirror: t('macd', 100, R10, { mirror: true }),
        bottomMirror: t('macd', 200, R10, { mirror: true })
    },
    rsi: {
        top: t('rsi', 100, RSI), bottom: t('rsi', 200, RSI), mid: t('rsi', 150, RSI),
        topMirror: t('rsi', 100, RSI, { mirror: true })
    },
    clamp: { high: t('macd', 90, R10), low: t('macd', 210, R10) },
    fmtVol: { s1: fmt(500), s2: fmt(50000), s3: fmt(256000000),
              f1: fmt(9999, true), f2: fmt(50000, true) }
};
// ② 白框宽度：measureText 用固定字宽 stub（真函数逻辑不变）
ctx = { save: function() {}, restore: function() {}, font: '',
        measureText: function(s) { return { width: String(s).length * 6.6 }; } };
out.label = { short: _overlayLabelW('123'), long: _overlayLabelW('1234567890') };
// ③ 白框绘制：记账 stub，看 fillRect 宽度与位置
const ops = [];
ctx = { fillRect: function(x, y, w, h) { ops.push(['fillRect', x, y, w, h]); },
        fillText: function(t, x, y) { ops.push(['fillText', t, x, y]); } };
const KAREA = { x: 10, y: 20, w: 800, h: 300 };
_drawOverlayIfNeeded({ rightPrice: '11.47', rightY: 150 }, KAREA);
_drawOverlayIfNeeded({ rightPrice: '5600.00万', rightY: 480, rightW: 61 }, KAREA);
out.overlay = { ops: ops };
process.stdout.write(JSON.stringify(out));
"""


def _cursor_driver(js):
    return "\n".join([
        "let _isMirrorMode = false;",
        "let _volDisplayMode = 'bar';",
        "let isFutures = false;",
        "function isFuturesMode() { return isFutures; }",
        fn_src(js, "formatVolume"),
        fn_src(js, "formatVolMacdVal"),
        fn_src(js, "_bottomValueAtY"),
        fn_src(js, "_overlayLabelW"),
        fn_src(js, "_drawOverlayIfNeeded"),
        object_src(js, "BOTTOM_INDICATORS"),
        CURSOR_TAIL,
    ])


def test_cursor_text():
    print("\n① cursorText 四类读数（node 抽真实源码执行）")
    d = _cached_cursor
    if d is None:
        return
    vb, vm, mc, rs = d["volBar"], d["volMacd"], d["macd"], d["rsi"]

    # ── vol 柱状：恒从底部长起（翻转不翻方向），端点/中点 ──
    rec("①", "vol 柱状：窗底=0、窗顶=range.max、中点=一半（formatVolume 整数量级）",
        vb["bottom"] == "0" and vb["top"] == "1000" and vb["mid"] == "500",
        "bottom=%r top=%r mid=%r" % (vb["bottom"], vb["top"], vb["mid"]))
    rec("①", "vol 柱状：翻转视图不翻柱方向 ⇒ 读数不变（非中点 700，而非翻转式的 300）",
        vb["midMirror"] == "700", "mirror=%r" % vb["midMirror"])

    # ── vol 类MACD / MACD：翻转敏感（镜像互逆） ──
    rec("①", "vol 类MACD：不翻转时窗顶=+max、窗底=−|min|（formatVolMacdVal 带符号）",
        vm["top"] == "10" and vm["bottom"] == "-10" and vm["mid"] == "0",
        "top=%r bottom=%r mid=%r" % (vm["top"], vm["bottom"], vm["mid"]))
    rec("①", "vol 类MACD：翻转视图镜像（顶↔底互换）",
        vm["topMirror"] == "-10" and vm["bottomMirror"] == "10",
        "topMirror=%r bottomMirror=%r" % (vm["topMirror"], vm["bottomMirror"]))
    rec("①", "MACD：与 macdToY 互逆（端点 + 中点 0.00）",
        mc["top"] == "10.00" and mc["bottom"] == "-10.00" and mc["mid"] == "0.00",
        "top=%r bottom=%r mid=%r" % (mc["top"], mc["bottom"], mc["mid"]))
    rec("①", "MACD：翻转视图镜像（toFixed(2) 格式不变）",
        mc["topMirror"] == "-10.00" and mc["bottomMirror"] == "10.00",
        "topMirror=%r bottomMirror=%r" % (mc["topMirror"], mc["bottomMirror"]))
    rec("①", "RSI：与 rsiToY 互逆（[20,80] 值域：顶=80.00 / 底=20.00 / 中=50.00）",
        rs["top"] == "80.00" and rs["bottom"] == "20.00" and rs["mid"] == "50.00",
        "top=%r bottom=%r mid=%r" % (rs["top"], rs["bottom"], rs["mid"]))
    rec("①", "RSI：翻转视图镜像",
        rs["topMirror"] == "20.00", "topMirror=%r" % rs["topMirror"])

    # ── 越界夹回（窗口命中判定已保证 y 在窗内，此处防浮点越界） ──
    rec("①", "y 越出窗上下沿时夹回端点值（不产生超界数值）",
        d["clamp"]["high"] == "10.00" and d["clamp"]["low"] == "-10.00",
        "high=%r low=%r" % (d["clamp"]["high"], d["clamp"]["low"]))

    # ── 格式化与纵轴刻度同源 ──
    fv = d["fmtVol"]
    rec("①", "formatVolume 单位口径：500→500 / 5.00万 / 2.56亿；期货 9999→9999、5.00万",
        fv["s1"] == "500" and fv["s2"] == "5.00万" and fv["s3"] == "2.56亿"
        and fv["f1"] == "9999" and fv["f2"] == "5.00万",
        str(fv))


def test_label_width():
    print("\n② _overlayLabelW 白框宽度（50 下限 + 长文本加宽）")
    d = _cached_cursor
    if d is None:
        return
    lb = d["label"]
    rec("②", "短文本宽度取下限 50（= K 线区现行固定宽）",
        lb["short"] == 50, "short=%r" % lb["short"])
    rec("②", "长文本按实测宽度加宽（10 字符 → 66 + 8 = 74，不出框）",
        lb["long"] == 74, "long=%r" % lb["long"])


def test_overlay_draw():
    print("\n③ _drawOverlayIfNeeded 白框绘制（宽度通道）")
    d = _cached_cursor
    if d is None:
        return
    ops = d["overlay"]["ops"]
    rec("③", "不传 rightW ⇒ 恒 50（K 线区价格读数行为不变）",
        len(ops) == 4 and ops[0] == ["fillRect", 812, 140, 50, 20]
        and ops[1] == ["fillText", "11.47", 816, 154],
        str(ops[:2]))
    rec("③", "传 rightW ⇒ 用给定宽度（指标区长文本不出框）",
        len(ops) == 4 and ops[2] == ["fillRect", 812, 470, 61, 20]
        and ops[3] == ["fillText", "5600.00万", 816, 484],
        str(ops[2:]))


# ══════════════════════════════════════════════════════════════════
# ④ drawCrosshair 采集（真函数 + 桩执行）
# ══════════════════════════════════════════════════════════════════
CROSSHAIR_TAIL = r"""
let _isMirrorMode = false;
let _logScale = false;
let _volDisplayMode = 'bar';
let isFutures = false;
function isFuturesMode() { return isFutures; }
let _overlayData = null;
let mouseX = -1, mouseY = -1;
let viewOffset = 0, viewCount = 5;
let currentFreq = 'd';
let maPeriods = {};
const MA_COLORS = { 5: '#FFFFFF' };
const COLORS = { crosshair: 'rgba(255,255,255,0.3)' };
let _currentGlobalIdx = -1, _currentClipText = '';
let _subCurrentGlobalIdx = -1, _subClipText = '';
const window = { _isRenderingBottom: false };
const canvas = { getBoundingClientRect: function() { return { left: 0, top: 0 }; } };
function getKlineEndTime(d) { return d; }
function mkEl() {
    return { innerHTML: '', style: {}, offsetWidth: 0,
             getBoundingClientRect: function() { return { left: 0, top: 0, width: 0, height: 0 }; } };
}
const document = { getElementById: function() { return mkEl(); } };
const ctx = {
    font: '', strokeStyle: '', fillStyle: '', lineWidth: 1, textAlign: '', textBaseline: '',
    setLineDash: function() {}, beginPath: function() {}, moveTo: function() {},
    lineTo: function() {}, stroke: function() {}, save: function() {}, restore: function() {},
    measureText: function(s) { return { width: String(s).length * 6.6 }; }
};
function mkK(i) {
    return { date: '2026/01/0' + (i + 1) + ' 10:00',
             open: 10 + i, high: 11 + i, low: 9 + i, close: 10.5 + i };
}
const klines5 = [mkK(0), mkK(1), mkK(2), mkK(3), mkK(4)];
const chartData = { klines: klines5, meta: { symbol: 'sh600000', forward_adjust: false,
    pe_ttm: null, index_belong: null, shareholder_reduction: null } };
let dualSubData = null;
let _bottomSlots = ['vol', 'macd'];
let _subBottomSlots = ['rsi'];
const areaK = { x: 10, y: 20, w: 800, h: 300 };
const sa0 = { x: 10, y: 334, w: 800, h: 100 };
const sa1 = { x: 10, y: 448, w: 800, h: 100 };
const bottomY = sa1.y + sa1.h;
const pr = { min: 10, max: 12 };
const R0 = { min: 0, max: 1000 };
const R1 = { min: -10, max: 10 };
const RSI = { min: 20, max: 80 };
function snap() {
    if (!_overlayData) return { rp: null, ry: null, rw: null };
    return {
        rp: _overlayData.rightPrice === undefined ? null : _overlayData.rightPrice,
        ry: _overlayData.rightY === undefined ? null : _overlayData.rightY,
        rw: _overlayData.rightW === undefined ? null : _overlayData.rightW
    };
}
function run(mx, my, areas, ranges, mirror) {
    mouseX = mx; mouseY = my;
    _isMirrorMode = !!mirror;
    _overlayData = null;
    drawCrosshair(klines5, areaK, pr, bottomY, 160, 0, areas, ranges);
    return snap();
}
const out = {};
out.klineHover = run(100, 100, [sa0, sa1], [R0, R1]);
out.slot0Hover = run(100, 384, [sa0, sa1], [R0, R1]);
out.slot0Amount = run(100, 384, [sa0, sa1], [{ min: 0, max: 112000000 }, R1]);
out.slot1Hover = run(100, 498, [sa0, sa1], [R0, R1]);
out.labelRow = run(100, 327, [sa0, sa1], [R0, R1]);
out.outsideX = run(900, 384, [sa0, sa1], [R0, R1]);
out.slot1MidNormal = run(100, 468, [sa0, sa1], [R0, R1]);
out.slot1MidMirror = run(100, 468, [sa0, sa1], [R0, R1], true);
out.slot0VolMirror = run(100, 364, [sa0, sa1], [R0, R1], true);
dualSubData = chartData;
out.dualSubSlot = run(100, 384, [sa0], [RSI]);
process.stdout.write(JSON.stringify(out));
"""


def _crosshair_driver(js):
    return "\n".join([
        fn_src(js, "yToPrice"),
        fn_src(js, "_fmtPrice"),
        fn_src(js, "formatVolume"),
        fn_src(js, "formatVolMacdVal"),
        fn_src(js, "_bottomValueAtY"),
        fn_src(js, "_overlayLabelW"),
        object_src(js, "BOTTOM_INDICATORS"),
        fn_src(js, "_bottomSlotList"),
        fn_src(js, "_slotAt"),
        fn_src(js, "drawCrosshair"),
        CROSSHAIR_TAIL,
    ])


def test_crosshair_collect():
    print("\n④ drawCrosshair 采集（真函数 + 桩执行）")
    js = read(APP_JS)
    d = run_node(_crosshair_driver(js), "④")
    if d is None:
        return
    k, s0, s0a, s1 = d["klineHover"], d["slot0Hover"], d["slot0Amount"], d["slot1Hover"]
    rec("④", "K 线区悬停：价格读数（既有行为不回归）+ 不设 rightW（恒 50 通道）",
        k["rp"] == "11.47" and k["ry"] == 100 and k["rw"] is None,
        str(k))
    rec("④", "槽 0 绘图窗（vol 柱状）：读数 = 反算值，rightY = 鼠标 y，rightW = 50",
        s0["rp"] == "500" and s0["ry"] == 384 and s0["rw"] == 50,
        str(s0))
    rec("④", "槽 0 长文本（成交额 5600.00万）：rightW 按实测宽加宽到 61",
        s0a["rp"] == "5600.00万" and s0a["rw"] == 61, str(s0a))
    rec("④", "槽 1 绘图窗（MACD）：读数 = 0.00（中点），rightY = 鼠标 y",
        s1["rp"] == "0.00" and s1["ry"] == 498 and s1["rw"] == 50,
        str(s1))
    rec("④", "槽 0 标签行（非绘图窗）：不设读数（横线仍在，仅无值域可读）",
        d["labelRow"]["rp"] is None, str(d["labelRow"]))
    rec("④", "mouseX 出区：不设读数（与 K 线区行为一致）",
        d["outsideX"]["rp"] is None, str(d["outsideX"]))
    rec("④", "MACD 非中点：正常 6.00 / 翻转 −6.00（镜像互逆）",
        d["slot1MidNormal"]["rp"] == "6.00" and d["slot1MidMirror"]["rp"] == "-6.00",
        "normal=%r mirror=%r" % (d["slot1MidNormal"]["rp"], d["slot1MidMirror"]["rp"]))
    rec("④", "翻转视图下 vol 柱状读数不变（柱子不翻方向）",
        d["slot0VolMirror"]["rp"] == "700", str(d["slot0VolMirror"]))
    rec("④", "双窗：槽集合切换到 _subBottomSlots（rsi）后读数跟着换",
        d["dualSubSlot"]["rp"] == "50.00", str(d["dualSubSlot"]))


# ══════════════════════════════════════════════════════════════════
# ⑤ 源码契约（静态）
# ══════════════════════════════════════════════════════════════════
def test_source_contracts():
    print("\n⑤ 源码契约（静态）")
    js = read(APP_JS)
    bobj = object_src(js, "BOTTOM_INDICATORS")
    i_vol = bobj.find("id: 'vol'")
    i_macd = bobj.find("id: 'macd'")
    i_rsi = bobj.find("id: 'rsi'")
    seg = {"vol": bobj[i_vol:i_macd], "macd": bobj[i_macd:i_rsi], "rsi": bobj[i_rsi:]}
    miss = [k for k, v in seg.items() if "cursorText:" not in v]
    rec("⑤", "三个槽位各定义 cursorText 钩子（注册表五钩子契约）",
        i_vol != -1 and i_macd != -1 and i_rsi != -1 and not miss, "missing=%s" % miss)

    rec("⑤", "采集块用同源引用：BOTTOM_INDICATORS[_slotAt(si)] + slotRanges[si]",
        "BOTTOM_INDICATORS[_slotAt(si)].cursorText(mouseY, sa, slotRanges[si])" in js
        and "const sa = slotAreas[si];" in js, "")

    m = re.search(r"drawCrosshair\(klinesToDraw, area, priceRange, bottomBottomY, "
                  r"barStep, subPixelOffset,\s*\n\s*slotAreas, slotRanges\);", js)
    rec("⑤", "_renderChart 调用点显式传 slotAreas/slotRanges（几何/值域不重算）",
        bool(m), "" if m else "调用点未传参")

    i_slots = js.find("const slotAreas = [], slotRanges = [];")
    i_call = js.find("drawCrosshair(klinesToDraw, area, priceRange, bottomBottomY")
    rec("⑤", "渲染序：slotRanges 值域计算先于 drawCrosshair 调用点",
        i_slots != -1 and i_call != -1 and i_slots < i_call,
        "slots@%s call@%s" % (i_slots, i_call))

    rec("⑤", "读数写进与 K 线区同一通道（rightPrice/rightY/rightW）",
        "_overlayData.rightPrice = text;" in js
        and "_overlayData.rightY = mouseY;" in js
        and "_overlayData.rightW = _overlayLabelW(text);" in js, "")

    rec("⑤", "_drawOverlayIfNeeded 的宽度通道：rightW || 50（K 线区恒 50 不变）",
        "const labelW = overlayData.rightW || 50;" in js, "")


_cached_cursor = None


def main():
    global _cached_cursor
    js = read(APP_JS)
    _cached_cursor = run_node(_cursor_driver(js), "①②③")

    test_cursor_text()
    test_label_width()
    test_overlay_draw()
    test_crosshair_collect()
    test_source_contracts()

    total = _passed + len(_failed)
    print("\n%s 合计 %d 项：%d 通过 / %d 失败" %
          ("[FAIL]" if _failed else "[PASS]", total, _passed, len(_failed)))
    for f in _failed:
        print("  - " + f)
    if _failed:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()

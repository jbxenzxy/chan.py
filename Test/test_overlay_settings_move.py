# -*- coding: utf-8 -*-
"""图层开关入抽屉护栏（2026-11-20）。

背景：K 线页顶栏原挂着「分型 / 笔 / 线段 / 中枢 / 买卖点」5 颗常驻按钮，
`.btn` 是 padding 4px 12px + 11px 字号，合计 ≈240px、含 gap 后挤占 ≈260–310px；
顶栏在窄屏靠 `overflow-x:auto` 横向滚动兜底（app.css 的 .header 注释），
这 5 颗正好把右侧「设置 / 刷新」挤出 13.9 吋屏视野。改造 = 收进既有的
「显示设置」抽屉（#bsp-filter-dialog）顶部一节「图层显示」。
同日第二批：抽屉原「其他 → 显示笔索引编号」并入该节为「笔编号」（value=biidx），
并把两处节标题的冗余后缀去掉（买卖点类型 / 均线周期（斐波那契数列））。

这条护栏钉四件事：
  ① 搬迁到位（静态）：顶栏 5 个 id 不再存在且无处回潮；抽屉里 6 个复选框的
     value 恰好按 {fx,bi,biidx,seg,zs,bsp} 的顺序排列（笔编号紧跟笔；fx 的文案是
     「分型编号」——那颗画的本来就是分型序号，biidx 的文案是「笔编号」），每个都绑
     onchange="onOverlayChange(this)"；「其他」节与 show-bi-idx 已彻底删除。
  ② 状态仍是单源（静态）：两个 window 入口在位；saveOverlaySettings 的
     localStorage 键 chan_overlay_settings 与 6 个键名未变（改了 = 用户已有
     设置全部丢失，且不会有任何东西报红）；只服务已删控件的 onShowBiIdxChange
     不再有 window 绑定（否则就是同一状态的第二个写入口）。
  ③ 文案（静态）：两处节标题按要求收敛，抽屉里不再出现「可多选」。
  ④ 行为（node 抽真实源码执行，桩只覆盖 DOM / localStorage / render）：
     · onOverlayChange 真能改状态 + 落盘 + 重绘（三条缺一即红）；
     · 笔编号那条 value=biidx 必须映射到 showBiIdx 并写成 showBiIdx 键
       （value 与持久化键不同名，写错就静默失效）；
     · syncOverlayControls 按状态回填，且**故意预置错误 checkbox 状态**证明它
       真写而不是恒等；
     · toggleOverlay 仍可翻转并同步复选框；
     · 两个负向：未知 type 不改状态、未知 value 不会把复选框点亮。

为什么必须有 ④：删掉 DOM 节点后，`value` 写错或不写 checked 都不会报错，
表现是"点复选框没反应"——②的静态断言挡不住这种失效。
静态断言一律在**剥掉注释**的代码上做：本轮注释里留着「原『其他 → 显示笔索引编号』」
「window.onShowBiIdxChange」这类留档文字，注释不算证据（只认代码）。
跑法：python Test/test_overlay_settings_move.py
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
APP_HTML = os.path.join(REPO_ROOT, "Frontend", "app.html")

# 顺序即文档顺序（笔编号必须紧跟笔）：value 集合与顺序都由这条常量断言
LAYER_ORDER = ["fx", "bi", "biidx", "seg", "zs", "bsp"]
# value → saveOverlaySettings 里的键名（唯一映射表，两边对不上就静默丢设置）
PERSIST_KEYS = {"fx": "Fx", "bi": "Bi", "biidx": "BiIdx",
                "seg": "Seg", "zs": "Zs", "bsp": "Bsp"}
# 这 5 个 id 属于"已删除的顶栏按钮"，任何一处回潮都是把省下来的空间又还回去
DEAD_IDS = ["btn-fx", "btn-bi", "btn-seg", "btn-zs", "btn-bsp"]

_passed = 0
_failed = []


def rec(no, title, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
        print("[PASS] %s %s" % (no, title))
    else:
        _failed.append("%s %s | %s" % (no, title, detail))
        print("[FAIL] %s %s" % (no, title) + (" | " + detail if detail else ""))


def read(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


def strip_js_comments(src):
    """剥掉 // 与 /* */ 注释（跳过字符串里的斜杠）：注释不算证据。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for ln in src.split("\n"):
        cut, q, i = -1, None, 0
        while i < len(ln):
            c = ln[i]
            if q:
                if c == "\\":
                    i += 2
                    continue
                if c == q:
                    q = None
            elif c in "\"'`":
                q = c
            elif c == "/" and i + 1 < len(ln) and ln[i + 1] == "/":
                cut = i
                break
            i += 1
        out.append(ln if cut < 0 else ln[:cut])
    return "\n".join(out)


def strip_html_comments(src):
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def _to_matching_brace(src, b):
    i, n, depth = b + 1, len(src), 1
    while i < n:
        c = src[i]
        if c in "\"'`":
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
    """抽 `function name(...) {...}` 或 `window.name = function(...) {...}` 原文。"""
    m = re.search(r"^[ \t]*function\s+%s\s*\(" % re.escape(name), src, re.M)
    pat = r"^[ \t]*function\s+%s\s*\(" % re.escape(name)
    if not m:
        pat = r"^[ \t]*window\.%s\s*=\s*function\s*\(" % re.escape(name)
        m = re.search(pat, src, re.M)
    if not m:
        raise AssertionError("未找到 %s" % name)
    b = src.index("{", m.end())
    return src[m.start():_to_matching_brace(src, b)]


def find_node():
    cand = [os.environ.get("NODE_EXE"), shutil.which("node")]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def run_node(src, tag):
    node = find_node()
    if not node:
        rec(tag, "node 可用", False, "未找到 node")
        return None
    tmp = tempfile.mkdtemp(prefix="overlay_move_")
    p = os.path.join(tmp, "drv.js")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        r = subprocess.run([node, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode != 0:
        rec(tag, "node 执行成功", False, (r.stderr or "").strip()[:600])
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        rec(tag, "node 输出 JSON", False, r.stdout[:600])
        return None


js = read(APP_JS)
html = read(APP_HTML)
js_code = strip_js_comments(js)
html_code = strip_html_comments(html)

# 抽屉片段（后面几处断言都限定在抽屉内，避免误伤页面别处的同名文字）
_dlg = strip_html_comments(html[html.index('id="bsp-filter-dialog"'):])

# ══════════════════ ① 搬迁到位（静态） ══════════════════
for _i, _id in enumerate(DEAD_IDS):
    rec("S1-%d" % (_i + 1), "顶栏已无 id=%s" % _id, ('id="%s"' % _id) not in html_code,
        "app.html 仍存在该按钮 id")

rec("S1-6", "app.js 不再引用已删按钮 id",
    not any(('"%s"' % d) in js_code for d in DEAD_IDS),
    "app.js 残留 " + str([d for d in DEAD_IDS if ('"%s"' % d) in js_code]))

_cb_vals = re.findall(
    r'<input[^>]*name="overlay-layer"[^>]*value="([a-z]+)"[^>]*>', _dlg)
rec("S2-1", "抽屉里恰有 6 个图层复选框", len(_cb_vals) == 6, "实测 %d 个" % len(_cb_vals))
rec("S2-2", "value 集合恰为 {fx,bi,biidx,seg,zs,bsp}",
    sorted(_cb_vals) == sorted(LAYER_ORDER), "实测 " + str(_cb_vals))
rec("S2-3", "value 顺序：笔编号紧跟笔（fx,bi,biidx,seg,zs,bsp）",
    _cb_vals == LAYER_ORDER, "实测顺序 " + str(_cb_vals))
rec("S2-4", "6 个复选框全部绑 onchange=onOverlayChange(this)",
    html_code.count('name="overlay-layer"') == 6 and
    len(re.findall(r'name="overlay-layer"[^>]*onchange="onOverlayChange\(this\)"', _dlg)) == 6,
    "绑定处数 = %d" % len(re.findall(r'onchange="onOverlayChange\(this\)"', _dlg)))
rec("S2-5", "笔编号那颗复选框的显示文案就是「笔编号」",
    bool(re.search(r'name="overlay-layer"[^>]*value="biidx"[^>]*>笔编号</label>', _dlg)),
    "未找到 value=biidx 且文案为「笔编号」的 label")

rec("S2-6", "分型那颗复选框的显示文案就是「分型编号」",
    bool(re.search(r'name="overlay-layer"[^>]*value="fx"[^>]*>分型编号</label>', _dlg)),
    "未找到 value=fx 且文案为「分型编号」的 label")

# 图层复选框必须落在 #bsp-filter-dialog 的 body 内（不能漂到别处变成第二套入口）
rec("S2-7", "图层复选框位于「显示设置」抽屉内",
    'name="overlay-layer"' in _dlg, "复选框不在 #bsp-filter-dialog 片段内")

# 「其他 → 显示笔索引编号」必须整体消失：DOM、处理器、内联引用三处都不许留
rec("S2-8", "抽屉里已无 show-bi-idx 复选框",
    'name="show-bi-idx"' not in html_code, "app.html 仍有 name=show-bi-idx")
rec("S2-9", "「显示笔索引编号」文案已删（代码里不许再出现）",
    "显示笔索引编号" not in html_code, "app.html 仍有「显示笔索引编号」")
rec("S2-10", "app.html 不再引用 onShowBiIdxChange",
    "onShowBiIdxChange" not in html_code, "内联事件仍指向已删函数")

# ══════════════════ ② 状态单源（静态） ══════════════════
rec("S3-1", "window.toggleOverlay 仍暴露（控制台 / 快捷键挂点）",
    "window.toggleOverlay = function" in js_code)
rec("S3-2", "window.onOverlayChange 已暴露（HTML 内联桥）",
    "window.onOverlayChange = function" in js_code)
rec("S3-3", "持久化键 chan_overlay_settings 未变",
    "chan_overlay_settings" in js_code)
for _k in LAYER_ORDER:
    _var = "show" + PERSIST_KEYS[_k]
    rec("S3-4%s" % _k, "saveOverlaySettings 仍写 %s" % _var,
        ("%s:" % _var) in js_code,
        "键名丢失 = 用户已有设置静默清空")
rec("S3-5", "旧函数名 applyOverlayButtonStates 在代码里已不存在（只留注释留档）",
    "applyOverlayButtonStates" not in js_code,
    "代码里仍有 %d 处实名引用" % js_code.count("applyOverlayButtonStates"))
rec("S3-6", "onShowBiIdxChange 已无 window 绑定（同一状态不留第二个写入口）",
    "onShowBiIdxChange" not in js_code,
    "代码里仍有 %d 处实名引用" % js_code.count("onShowBiIdxChange"))

_decl = re.search(r"^[ \t]*let showBi = true,.*$", js, re.M)
rec("S3-7", "6 个图层变量的声明行仍在（默认值单一来源）",
    _decl is not None, "未找到 'let showBi = true, ...' 声明行")

# ══════════════════ ③ 节标题文案 ══════════════════
rec("S4-1", "标题已改为「买卖点类型」（去掉（可多选））",
    '>买卖点类型</div>' in _dlg, "抽屉内未找到「>买卖点类型</div>」")
rec("S4-2", "标题已改为「均线周期（斐波那契数列）」",
    '>均线周期（斐波那契数列）</div>' in _dlg,
    "抽屉内未找到「>均线周期（斐波那契数列）</div>」")
rec("S4-3", "抽屉内不再出现「可多选」字样",
    "可多选" not in _dlg, "仍有 %d 处" % _dlg.count("可多选"))

# ══════════════════ ④ 行为（node 抽真实源码） ══════════════════
DRV = u"""var window = globalThis;
var _store = {};
var localStorage = {
    setItem: function (k, v) { _store[k] = v; },
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null; }
};
var _renderCalls = 0;
function render() { _renderCalls += 1; }
/*__DECL__*/
var _volDisplayMode = "vol";
var _bottomSlots = [];
var _subBottomSlots = [];
var bspFilter = { "0": true };
var maPeriods = { "5": true };
var _logScale = false;
function _stored() {
    var raw = _store["chan_overlay_settings"];
    return (typeof raw === "string") ? JSON.parse(raw) : null;
}
/*__FN_GET__*/
/*__FN_SET__*/
/*__FN_SYNC__*/
/*__FN_SAVE__*/
/*__FN_TOGGLE__*/
/*__FN_ONCHANGE__*/
var KEYS = /*__KEYS__*/;
var _cbs = [];
function mkCbs() {
    _cbs = [];
    for (var i = 0; i < KEYS.length; i++) {
        _cbs.push({ value: KEYS[i], checked: null, marker: "stale" });
    }
}
function idxOf(v) { return KEYS.indexOf(v); }
var document = {
    querySelectorAll: function (sel) {
        return sel === '#bsp-filter-dialog input[name="overlay-layer"]' ? _cbs : [];
    },
    getElementById: function () { return null; }
};
var out = {};

mkCbs();
window.onOverlayChange({ value: "fx", checked: true });
out.afterFx = { showFx: showFx, stored: (_stored() || {}).showFx,
                hasKey: _stored() !== null, render: _renderCalls };

mkCbs();
window.onOverlayChange({ value: "zs", checked: false });
out.afterZsOff = { showZs: showZs, checked: _cbs[idxOf("zs")].checked,
                   stored: (_stored() || {}).showZs };

mkCbs();
window.onOverlayChange({ value: "biidx", checked: true });
var _stBi = _stored() || {};
out.afterBiIdx = {
    now: showBiIdx, checked: _cbs[idxOf("biidx")].checked,
    stored: _stBi.showBiIdx,
    hasOwn: Object.prototype.hasOwnProperty.call(_stBi, "showBiIdx"),
    storedShowBi: _stBi.showBi,
    render: _renderCalls
};

mkCbs();
syncOverlayControls();
out.syncAll = [];
for (var j = 0; j < _cbs.length; j++) {
    out.syncAll.push({ value: _cbs[j].value, checked: _cbs[j].checked, wasStale: _cbs[j].marker });
}
out.stateNow = { fx: showFx, bi: showBi, biidx: showBiIdx,
                 seg: showSeg, zs: showZs, bsp: showBsp };

var _si = idxOf("seg"), _segBefore = showSeg, _r0 = _renderCalls;
mkCbs();
window.toggleOverlay("seg");
out.afterToggleSeg = { seg: showSeg, before: _segBefore, checked: _cbs[_si].checked,
                       renderDelta: _renderCalls - _r0 };

var _snap = JSON.stringify([showFx, showBi, showBiIdx, showSeg, showZs, showBsp]);
var _storeBefore = _store["chan_overlay_settings"];
var _r1 = _renderCalls;
mkCbs();
window.toggleOverlay("nope");
out.unknownType = {
    changed: _snap !== JSON.stringify([showFx, showBi, showBiIdx, showSeg, showZs, showBsp]),
    storeTouched: _store["chan_overlay_settings"] !== _storeBefore,
    renderDelta: _renderCalls - _r1
};

_cbs.push({ value: "ghost", checked: null, marker: "stale" });
syncOverlayControls();
out.ghost = { checked: _cbs[_cbs.length - 1].checked };

console.log(JSON.stringify(out));
"""


def behavior_checks():
    try:
        parts = {}
        for n in ("_overlayGet", "_overlaySet", "syncOverlayControls",
                  "saveOverlaySettings", "toggleOverlay", "onOverlayChange"):
            parts[n] = fn_src(js, n)
    except AssertionError as e:
        rec("B0", "抽到全部 6 个真实函数", False, str(e))
        return

    drv = (DRV
           .replace("/*__DECL__*/", _decl.group(0).strip())
           .replace("/*__FN_GET__*/", parts["_overlayGet"])
           .replace("/*__FN_SET__*/", parts["_overlaySet"])
           .replace("/*__FN_SYNC__*/", parts["syncOverlayControls"])
           .replace("/*__FN_SAVE__*/", parts["saveOverlaySettings"])
           .replace("/*__FN_TOGGLE__*/", parts["toggleOverlay"])
           .replace("/*__FN_ONCHANGE__*/", parts["onOverlayChange"])
           .replace("/*__KEYS__*/", json.dumps(LAYER_ORDER)))

    r = run_node(drv, "B0")
    if r is None:
        return

    a = r["afterFx"]
    rec("B1-1", "onOverlayChange 真的改了状态", a["showFx"] is True, str(a))
    rec("B1-2", "onOverlayChange 真的落了盘",
        a["hasKey"] is True and a["stored"] is True, str(a))
    rec("B1-3", "onOverlayChange 真的重绘（render 调用 1 次）", a["render"] == 1, str(a))

    z = r["afterZsOff"]
    rec("B2-1", "关掉中枢后状态为 false", z["showZs"] is False, str(z))
    rec("B2-2", "关掉中枢后落盘同步为 false", z["stored"] is False, str(z))

    # 笔编号：value=biidx 与持久化键 showBiIdx 不同名，映射错了就静默失效
    b = r["afterBiIdx"]
    rec("B3-1", "value=biidx 真的改到 showBiIdx", b["now"] is True, str(b))
    rec("B3-2", "biidx 落盘用的是 showBiIdx 键（不是 biidx 键）",
        b["hasOwn"] is True and b["stored"] is True, str(b))
    rec("B3-3", "写 biidx 没把别的图层键覆盖掉（showBi 仍在）",
        b["storedShowBi"] is True, str(b))
    rec("B3-4", "biidx 变化也触发重绘", b["render"] >= 1, str(b))

    st = r["stateNow"]
    by_val = dict((x["value"], x["checked"]) for x in r["syncAll"])
    for _i, _k in enumerate(LAYER_ORDER):
        rec("B4-%d" % (_i + 1), "syncOverlayControls 回填 %s 与状态一致" % _k,
            by_val.get(_k) == st[_k], "%s: cb=%r state=%r" % (_k, by_val.get(_k), st[_k]))
    # 预置值全是 null（"stale"），回填后必须全部变成布尔 True/False ⇒ 证明它真写、不是恒等
    rec("B4-7", "回填真的写入了复选框（预置 null 全部被覆盖）",
        all(isinstance(x["checked"], bool) for x in r["syncAll"]), str(r["syncAll"]))
    rec("B4-8", "回填覆盖了抽屉里全部 6 个复选框（不多不少）",
        len(r["syncAll"]) == 6, "实测 %d 个" % len(r["syncAll"]))

    t = r["afterToggleSeg"]
    rec("B5-1", "toggleOverlay 仍能翻转状态", t["seg"] is not t["before"], str(t))
    rec("B5-2", "toggleOverlay 同步了对应复选框", t["checked"] == t["seg"], str(t))
    rec("B5-3", "toggleOverlay 触发一次重绘", t["renderDelta"] == 1, str(t))

    u = r["unknownType"]
    rec("B6-1", "未知 type 不改状态（负向）", u["changed"] is False, str(u))
    rec("B6-2", "未知 type 不落盘（负向）", u["storeTouched"] is False, str(u))
    rec("B6-3", "未知 type 不重绘（负向）", u["renderDelta"] == 0, str(u))
    rec("B6-4", "未知 value 不会被点亮（负向）", r["ghost"]["checked"] is False, str(r["ghost"]))


behavior_checks()

print("\n" + "=" * 60)
print("合计 %d 项，通过 %d，失败 %d" % (_passed + len(_failed), _passed, len(_failed)))
if _failed:
    for f in _failed:
        print("  ✗ " + f)
    sys.exit(1)

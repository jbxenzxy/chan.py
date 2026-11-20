# -*- coding: utf-8 -*-
"""图层开关入抽屉护栏（2026-11-20）。

背景：K 线页顶栏原挂着「分型 / 笔 / 线段 / 中枢 / 买卖点」5 颗常驻按钮，
`.btn` 是 padding 4px 12px + 11px 字号，合计 ≈240px、含 gap 后挤占 ≈260–310px；
顶栏在窄屏靠 `overflow-x:auto` 横向滚动兜底（app.css 的 .header 注释），
这 5 颗正好把右侧「设置 / 刷新」挤出 13.9 吋屏视野。改造 = 收进既有的
「显示设置」抽屉（#bsp-filter-dialog）顶部一节「图层显示」。

这条护栏钉三件事：
  ① 搬迁到位（静态）：顶栏 5 个 id 不再存在且无处回潮；抽屉里 5 个复选框的
     value 集合恰为 {fx,bi,seg,zs,bsp}（与 _overlayGet / _overlaySet 认识的键
     同一套），每个都绑 onchange="onOverlayChange(this)"。
  ② 状态仍是单源（静态）：两个 window 入口在位；saveOverlaySettings 的
     localStorage 键 chan_overlay_settings 与 5 个键名未变（改了 = 用户已有
     设置全部丢失，且不会有任何东西报红）。
  ③ 行为（node 抽真实源码执行，桩只覆盖 DOM / localStorage / render）：
     · onOverlayChange 真能改状态 + 落盘 + 重绘（三条缺一即红）；
     · syncOverlayControls 按状态回填，且**故意预置错误 checkbox 状态**证明它
       真写而不是恒等；
     · toggleOverlay 仍可翻转并同步复选框；
     · 两个负向：未知 type 不改状态、未知 value 不会把复选框点亮。

为什么必须有 ③：删掉 DOM 节点后，`value` 写错或不写 checked 都不会报错，
表现是"点复选框没反应"——②的静态断言挡不住这种失效。
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

LAYER_KEYS = ["fx", "bi", "seg", "zs", "bsp"]
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

# ══════════════════ ① 搬迁到位（静态） ══════════════════
for _i, _id in enumerate(DEAD_IDS):
    rec("S1-%d" % (_i + 1), "顶栏已无 id=%s" % _id, ('id="%s"' % _id) not in html,
        "app.html 仍存在该按钮 id")

rec("S1-6", "app.js 不再引用已删按钮 id",
    not any(('"%s"' % d) in js for d in DEAD_IDS),
    "app.js 残留 " + str([d for d in DEAD_IDS if ('"%s"' % d) in js]))

_cb_vals = re.findall(
    r'<input[^>]*name="overlay-layer"[^>]*value="([a-z]+)"[^>]*>', html)
rec("S2-1", "抽屉里恰有 5 个图层复选框", len(_cb_vals) == 5, "实测 %d 个" % len(_cb_vals))
rec("S2-2", "value 集合恰为 {fx,bi,seg,zs,bsp}",
    sorted(_cb_vals) == sorted(LAYER_KEYS), "实测 " + str(_cb_vals))
rec("S2-3", "5 个复选框全部绑 onchange=onOverlayChange(this)",
    html.count('name="overlay-layer"') == 5 and
    len(re.findall(r'name="overlay-layer"[^>]*onchange="onOverlayChange\(this\)"', html)) == 5,
    "绑定处数 = %d" % len(re.findall(r'onchange="onOverlayChange\(this\)"', html)))

# 图层复选框必须落在 #bsp-filter-dialog 的 body 内（不能漂到别处变成第二套入口）
_dlg = html[html.index('id="bsp-filter-dialog"'):]
rec("S2-4", "图层复选框位于「显示设置」抽屉内",
    'name="overlay-layer"' in _dlg, "复选框不在 #bsp-filter-dialog 片段内")

# ══════════════════ ② 状态单源（静态） ══════════════════
rec("S3-1", "window.toggleOverlay 仍暴露（控制台 / 快捷键挂点）",
    "window.toggleOverlay = function" in js)
rec("S3-2", "window.onOverlayChange 已暴露（HTML 内联桥）",
    "window.onOverlayChange = function" in js)
rec("S3-3", "持久化键 chan_overlay_settings 未变",
    "chan_overlay_settings" in js)
for _k in LAYER_KEYS:
    rec("S3-4%s" % _k, "saveOverlaySettings 仍写 show%s" % _k.capitalize(),
        ("show%s:" % _k.capitalize()) in js,
        "键名丢失 = 用户已有设置静默清空")
rec("S3-5", "旧函数名 applyOverlayButtonStates 只存在于解释性注释",
    js.count("applyOverlayButtonStates") == 1 and
    all(("applyOverlayButtonStates" not in ln) or ln.lstrip().startswith("//")
        for ln in js.split("\n")),
    "实名引用残留 %d 处" % js.count("applyOverlayButtonStates"))

_decl = re.search(r"^[ \t]*let showBi = true,.*$", js, re.M)
rec("S3-6", "5 个图层变量的声明行仍在（默认值单一来源）",
    _decl is not None, "未找到 'let showBi = true, ...' 声明行")

# ══════════════════ ③ 行为（node 抽真实源码） ══════════════════
def behavior_checks():
    try:
        parts = [fn_src(js, n) for n in
                 ("_overlayGet", "_overlaySet", "syncOverlayControls",
                  "saveOverlaySettings", "toggleOverlay", "onOverlayChange")]
    except AssertionError as e:
        rec("B0", "抽到全部 6 个真实函数", False, str(e))
        return

    drv = u"""
var window = globalThis;
var _store = {};
var localStorage = {
    setItem: function (k, v) { _store[k] = v; },
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null; }
};
var _renderCalls = 0;
function render() { _renderCalls += 1; }
%s
var _volDisplayMode = "vol";
var _bottomSlots = [];
var _subBottomSlots = [];
var bspFilter = { "0": true };
var maPeriods = { "5": true };
var _logScale = false;
var _cbs = [];
function _stored() {
    var raw = _store["chan_overlay_settings"];
    return (typeof raw === "string") ? JSON.parse(raw) : null;
}
%s
function mkCbs() {
    _cbs = [];
    for (var i = 0; i < %s.length; i++) {
        _cbs.push({ value: %s[i], checked: null, marker: "stale" });
    }
}
var document = {
    querySelectorAll: function (sel) {
        return sel === '#bsp-filter-dialog input[name="overlay-layer"]' ? _cbs : [];
    },
    getElementById: function () { return null; }
};
var out = {};
%s
%s
%s
%s
%s
mkCbs();
window.onOverlayChange({ value: "fx", checked: true });
out.afterFx = { showFx: showFx, stored: (_stored() || {}).showFx,
                hasKey: _stored() !== null, render: _renderCalls };

mkCbs();
window.onOverlayChange({ value: "zs", checked: false });
out.afterZsOff = { showZs: showZs, checked: _cbs[3].checked,
                   stored: (_stored() || {}).showZs };

mkCbs();
syncOverlayControls();
out.syncAll = [];
for (var j = 0; j < _cbs.length; j++) {
    out.syncAll.push({ value: _cbs[j].value, checked: _cbs[j].checked, wasStale: _cbs[j].marker });
}
out.stateNow = { fx: showFx, bi: showBi, seg: showSeg, zs: showZs, bsp: showBsp };

var _before = { seg: showSeg, r: _renderCalls };
mkCbs();
window.toggleOverlay("seg");
out.afterToggleSeg = { seg: showSeg, before: _before.seg, checked: _cbs[2].checked,
                       renderDelta: _renderCalls - _before.r };

var _snap = { fx: showFx, bi: showBi, seg: showSeg, zs: showZs, bsp: showBsp };
var _storeBefore = _store["chan_overlay_settings"];
var _rBefore = _renderCalls;
mkCbs();
window.toggleOverlay("nope");
out.unknownType = {
    changed: JSON.stringify(_snap) !== JSON.stringify(
        { fx: showFx, bi: showBi, seg: showSeg, zs: showZs, bsp: showBsp }),
    storeTouched: _store["chan_overlay_settings"] !== _storeBefore,
    renderDelta: _renderCalls - _rBefore
};

_cbs.push({ value: "ghost", checked: null, marker: "stale" });
syncOverlayControls();
out.ghost = { checked: _cbs[_cbs.length - 1].checked };
console.log(JSON.stringify(out));
""" % (_decl.group(0), parts[0], json.dumps(LAYER_KEYS), json.dumps(LAYER_KEYS),
       parts[1], parts[2], parts[3], parts[4], parts[5])

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

    st = r["stateNow"]
    rec("B3-1", "syncOverlayControls 回填 fx 与状态一致",
        r["syncAll"][0]["checked"] == st["fx"], str(r["syncAll"][0]))
    rec("B3-2", "syncOverlayControls 回填 bi 与状态一致",
        r["syncAll"][1]["checked"] == st["bi"], str(r["syncAll"][1]))
    rec("B3-3", "syncOverlayControls 回填 seg 与状态一致",
        r["syncAll"][2]["checked"] == st["seg"], str(r["syncAll"][2]))
    rec("B3-4", "syncOverlayControls 回填 zs 与状态一致",
        r["syncAll"][3]["checked"] == st["zs"], str(r["syncAll"][3]))
    rec("B3-5", "syncOverlayControls 回填 bsp 与状态一致",
        r["syncAll"][4]["checked"] == st["bsp"], str(r["syncAll"][4]))
    # 预置值全是 null（"stale"），回填后必须全部变成布尔 True/False ⇒ 证明它真写、不是恒等
    rec("B3-6", "回填真的写入了复选框（预置 null 全部被覆盖）",
        all(isinstance(x["checked"], bool) for x in r["syncAll"]), str(r["syncAll"]))

    t = r["afterToggleSeg"]
    rec("B4-1", "toggleOverlay 仍能翻转状态", t["seg"] is not t["before"], str(t))
    rec("B4-2", "toggleOverlay 同步了对应复选框", t["checked"] == t["seg"], str(t))
    rec("B4-3", "toggleOverlay 触发一次重绘", t["renderDelta"] == 1, str(t))

    u = r["unknownType"]
    rec("B5-1", "未知 type 不改状态（负向）", u["changed"] is False, str(u))
    rec("B5-2", "未知 type 不落盘（负向）", u["storeTouched"] is False, str(u))
    rec("B5-3", "未知 type 不重绘（负向）", u["renderDelta"] == 0, str(u))
    rec("B5-4", "未知 value 不会被点亮（负向）", r["ghost"]["checked"] is False, str(r["ghost"]))


behavior_checks()

print("\n" + "=" * 60)
print("合计 %d 项，通过 %d，失败 %d" % (_passed + len(_failed), _passed, len(_failed)))
if _failed:
    for f in _failed:
        print("  ✗ " + f)
    sys.exit(1)

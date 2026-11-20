# -*- coding: utf-8 -*-
"""顶栏左端改造护栏（2026-11-20 第三批）。

三件事一起改的，且互相耦合，所以合到一条护栏里钉：
  ⑴ 删「查询」按钮 —— 输入框回车即 loadStock（onInputKeydown 的 Enter 分支），
     按钮只是重复入口。**删它的前提是回车路径在**，否则页面上就没有任何鼠标/
     键盘入口能发起了（历史下拉点击除外）。
  ⑵ 「刷新」键从顶栏右端搬到最左端「代码」之前。它刷的是股票名 / 板块文件 /
     指数归属三份数据缓存，不是"重新加载本图"——位置变了、语义没变，靠 title 说明。
  ⑶ 删「代码:」的冒号。
  ⑷ 删刷新过程的页内文字进度（#refresh-status）。删因不是"不好看"，而是它**参与
     布局**：那段 step 文案是 .header-right 里的 flex 项，无头 Chrome 实测
     （1920/1600/1440/1366/1280 五档）文字一出现，右半段控件整体横移 72–78px；
     长文案在 ≤1440px 还会把顶栏撑出横向滚动条（1440 溢出 112px、1280 溢出 163px）。
     后端 log 已逐步骤打印（[股名刷新] 步骤N/5 …），页面上不再重复一遍。

钉住的契约：
  ① 位置（静态）：#btn-refresh 只出现一次，且是 .stock-input 的**第一个元素子节点**
     （在 <label> 与输入框之前）；.header-right 片段里没有它；#refresh-status 在
     html / js 里都 0 处。
  ② 删除面（静态）：html 里没有 onclick="loadStock()" 的按钮；<label>代码</label>
     不带冒号；输入框仍绑 onkeydown="onInputKeydown(event)"（删按钮的前提）。
  ③ 无样式倒挂（静态）：app.css 里没有 `.stock-input button` 这条选择器 ——
     权重 0,1,1 会压过 .btn-icon 的 padding:0/width:28px，把方形图标键撑成扁条。
  ④ 行为（node 抽真实函数执行，桩只覆盖 fetch / DOM / showAlert / setTimeout）：
     · refreshStockNames 仍发 POST /api/stocks/refresh，并把按钮置灰 + 转圈；
     · 全程**不抛异常** —— 这是"删了 DOM 却留着引用"的唯一暴露方式：
       getElementById 返回 null 后 .style/.textContent 就是 TypeError，而静态
       grep 挡不住（引用可能藏在某条分支里）；
     · 轮询 running=true → 500ms 后再轮询；running=false → 按钮复位；
     · 失败走 showAlert，且按钮照样复位（否则键永久置灰，只能刷新页面）；
     · 置灰期间重复点击不再发请求（防重入）；
     · 回车 → loadStock 恰好一次（删查询按钮的前提，⑥/⑦ 的行为面）。
跑法：python Test/test_header_topbar_layout.py
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
APP_CSS = os.path.join(REPO_ROOT, "Frontend", "app.css")

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


def strip_html_comments(src):
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def strip_css_comments(src):
    """CSS 注释同样不算证据：本轮在 app.css 里留了一句「原有一条 .stock-input button { … }」
    的留档注释，不剥掉就会被下面的选择器断言当成回潮。"""
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


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
    if not m:
        m = re.search(r"^[ \t]*window\.%s\s*=\s*function\s*\(" % re.escape(name), src, re.M)
    if not m:
        raise AssertionError("未找到 %s" % name)
    b = src.index("{", m.end())
    return src[m.start():_to_matching_brace(src, b)]


def find_node():
    for c in (os.environ.get("NODE_EXE"), shutil.which("node")):
        if c and os.path.isfile(c):
            return c
    return None


def run_node(src, tag):
    node = find_node()
    if not node:
        rec(tag, "node 可用", False, "未找到 node")
        return None
    tmp = tempfile.mkdtemp(prefix="hdr_topbar_")
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


html = read(APP_HTML)
js = read(APP_JS)
css = read(APP_CSS)
html_code = strip_html_comments(html)
js_code = strip_js_comments(js)
css_code = strip_css_comments(css)

# ══════════════════ ① 位置（静态） ══════════════════
rec("S1-1", "#btn-refresh 在 app.html 里只出现一次",
    html_code.count('id="btn-refresh"') == 1,
    "出现 %d 次（重复 = 两颗键抢同一个 id）" % html_code.count('id="btn-refresh"'))

_si = html_code[html_code.index('<div class="stock-input">'):]
_si = _si[:_si.index('<div class="stock-history"')]
_first_tag = re.search(r"<(button|label|input|div)\b", _si[1:])
rec("S1-2", ".stock-input 的第一个元素子节点就是刷新键",
    bool(re.search(r'^\s*<button[^>]*id="btn-refresh"', _si[_si.index(">") + 1:])),
    "首个子节点 = " + (_first_tag.group(1) if _first_tag else "?"))
rec("S1-3", "刷新键落在「代码」标签之前（位置：最左端）",
    _si.index('id="btn-refresh"') < _si.index("<label>"),
    "btn-refresh 在下标 %d，label 在下标 %d"
    % (_si.index('id="btn-refresh"'), _si.index("<label>")))
rec("S1-4", "「代码」标签仍在输入框之前（顺序未被搬乱）",
    _si.index("<label>") < _si.index('id="stock-code-input"'), "")

_hr = html_code[html_code.index('<div class="header-right">'):]
_hr = _hr[:_hr.index('<div id="chart-container"')]
rec("S1-5", ".header-right 片段里已无刷新键（真的搬走了，不是复制一份）",
    'id="btn-refresh"' not in _hr, ".header-right 里仍有 btn-refresh")

rec("S1-6", "#refresh-status 在 app.html 里已 0 处",
    "refresh-status" not in html_code,
    "app.html 仍有 %d 处" % html_code.count("refresh-status"))
rec("S1-7", "app.js 里不再引用 #refresh-status（代码面；注释留档不算）",
    "refresh-status" not in js_code,
    "app.js 仍有 %d 处实名引用" % js_code.count("refresh-status"))

# ══════════════════ ② 删除面（静态） ══════════════════
rec("S2-1", "顶栏已无「查询」按钮（无 onclick=\"loadStock()\"）",
    'onclick="loadStock()"' not in html_code, "app.html 仍有该按钮")
rec("S2-2", "输入框仍绑 onkeydown=\"onInputKeydown(event)\"（删按钮的前提）",
    'onkeydown="onInputKeydown(event)"' in html_code,
    "回车入口不见了 —— 此时页面上再没有查询入口")
rec("S2-3", "app.js 的 onInputKeydown 仍存在",
    "window.onInputKeydown = function" in js_code, "未找到该函数")
rec("S2-4", "标签文案是「代码」，不带冒号",
    "<label>代码</label>" in html_code and "代码:" not in html_code,
    "实测 label=%r，冒号残留 %d 处"
    % (re.findall(r"<label>([^<]*)</label>", html_code)[:3], html_code.count("代码:")))

# ══════════════════ ③ 无样式倒挂（静态） ══════════════════
rec("S3-1", "app.css 里没有 `.stock-input button` 选择器（会压过 .btn-icon 的 padding:0）",
    not re.search(r"\.stock-input\s+button\s*\{", css_code),
    "该选择器仍在：权重 0,1,1 > .btn-icon 的 0,1,0，方形图标键会被撑成扁条")
rec("S3-2", ".btn-icon 仍是 28×28 + padding:0（尺寸单一来源）",
    bool(re.search(r"\.btn-icon\s*\{[^}]*width:\s*28px[^}]*padding:\s*0", css_code, re.S)),
    ".btn-icon 基础尺寸定义被改")

# ══════════════════ ④ 行为（node 抽真实源码） ══════════════════
DRV = u"""var window = globalThis;
var _fetches = [];
var _alerts = [];
var _timers = [];
var _threw = [];
function showAlert(m) { _alerts.push(String(m)); }
function setTimeout(fn, ms) { _timers.push({ fn: fn, ms: ms }); return _timers.length; }
function loadStock() { _loadCalls += 1; }
var _loadCalls = 0;

/* 极简 thenable：语义与 Promise 足够对齐（then 抛错要能落进 catch） */
function _state(v, ok, err) { return { v: v, ok: ok, err: err }; }
function _P(st) {
    var api = {
        then: function (fn) {
            if (!st.ok) return _P(st);
            try {
                var r = fn(st.v);
                if (r && typeof r.then === "function") return r;
                return _P(_state(r, true, null));
            } catch (e) { return _P(_state(undefined, false, e)); }
        },
        "catch": function (fn) {
            if (!st.ok) { try { fn(st.err); } catch (e) { _threw.push(String(e)); } }
            return api;
        }
    };
    return api;
}
var _routes = {};
function _useRoute(map) { _routes = map; }
/* 真 fetch 的 Response 有 json()；桩必须同样提供，否则代码里第一段
   .then(resp => resp.json()) 会 TypeError，整条链直接落进 catch。 */
function _wrap(v) {
    if (v && typeof v === "object" && !v.json) {
        v.json = function () { return _P(_state(v, true, null)); };
    }
    return v;
}
function fetch(url, opts) {
    _fetches.push({ url: url, method: (opts && opts.method) || "GET" });
    var h = _routes[url];
    if (!h) return _P(_state({}, true, null));
    if (h === "__reject__") return _P(_state(undefined, false, new Error("network down")));
    return _P(_state(_wrap(h), true, null));
}

var _btn = {
    disabled: false,
    _cls: {},
    classList: {
        add: function (c) { _btn._cls[c] = true; },
        remove: function (c) { delete _btn._cls[c]; },
        contains: function (c) { return !!_btn._cls[c]; }
    },
    _anim: "",
    querySelector: function (sel) {
        if (sel !== "svg") return null;
        return { style: { set animation(v) { _btn._anim = v; },
                          get animation() { return _btn._anim; } } };
    }
};
var _historyShown = false;
var _navItems = [];
var _historyEl = {
    classList: { contains: function (c) { return c === "show" && _historyShown; },
                 remove: function () { _historyShown = false; } },
    querySelectorAll: function () { return _navItems; }
};
var document = {
    getElementById: function (id) {
        if (id === "btn-refresh") return _btn;
        if (id === "stock-history") return _historyEl;
        return null;          // #refresh-status 之类的已删节点 → 引用残留即 TypeError
    }
};

/*__FN_RESET__*/
/*__FN_REFRESH__*/
/*__FN_POLL__*/
/*__FN_KEYDOWN__*/

var out = {};

/* —— 1) 启动：POST + 置灰 + 转圈，running=true 时应挂起等待下一轮 —— */
_useRoute({ "/api/stocks/refresh": { status: "started" },
            "/api/stocks/refresh/read/status": { running: true, step: "刷新股票名..." } });
try { window.refreshStockNames(); }
catch (e) { _threw.push("launch:" + String(e)); }
out.launch = { fetches: _fetches.slice(), disabled: _btn.disabled,
               active: _btn.classList.contains("active"), anim: _btn._anim,
               timers: _timers.map(function (t) { return t.ms; }) };

/* —— 2) 防重入：置灰期间再点，不应再发请求 —— */
var _n0 = _fetches.length;
try { window.refreshStockNames(); } catch (e) { _threw.push("reentry:" + String(e)); }
out.reentry = { delta: _fetches.length - _n0, disabled: _btn.disabled };

/* —— 3) 收尾（无错误）：按钮复位，不弹窗 —— */
_fetches = []; _timers = [];
_useRoute({ "/api/stocks/refresh/read/status": { running: false, step: "" } });
try { _timers.length = 0; pollRefreshStatus(_btn); }
catch (e) { _threw.push("done:" + String(e)); }
out.done = { disabled: _btn.disabled, active: _btn.classList.contains("active"),
             anim: _btn._anim, alerts: _alerts.slice(), timers: _timers.length };

/* —— 4) 收尾（有错误）：弹窗 + 照样复位 —— */
_alerts = [];
_useRoute({ "/api/stocks/refresh/read/status": { running: false, error: "补全失败" } });
try { pollRefreshStatus(_btn); }
catch (e) { _threw.push("err:" + String(e)); }
out.fail = { disabled: _btn.disabled, active: _btn.classList.contains("active"),
             anim: _btn._anim, alerts: _alerts.slice() };

/* —— 5) 启动请求被拒：弹窗 + 复位（不许永久置灰） —— */
_alerts = []; _btn.disabled = false; _btn._cls = {}; _btn._anim = "";
_useRoute({ "/api/stocks/refresh": "__reject__" });
try { window.refreshStockNames(); }
catch (e) { _threw.push("reject:" + String(e)); }
out.reject = { disabled: _btn.disabled, anim: _btn._anim, alerts: _alerts.slice() };

/* —— 6) 回车 → loadStock 恰好一次（删查询按钮的前提） —— */
_historyShown = false; _navItems = []; _loadCalls = 0;
try {
    window.onInputKeydown({ key: "Enter", preventDefault: function () {} });
} catch (e) { _threw.push("enter:" + String(e)); }
out.enter = { loadCalls: _loadCalls };

/* —— 7) 下拉未展开时按方向键：不该触发加载（只有 Enter 才查） —— */
_loadCalls = 0;
try {
    window.onInputKeydown({ key: "ArrowDown", preventDefault: function () {} });
} catch (e) { _threw.push("arrow:" + String(e)); }
out.arrowIdle = { loadCalls: _loadCalls };

out.threw = _threw;
console.log(JSON.stringify(out));
"""


def behavior_checks():
    parts = {}
    for n in ("_resetRefreshBtn", "refreshStockNames", "pollRefreshStatus", "onInputKeydown"):
        try:
            parts[n] = fn_src(js, n)
        except AssertionError as e:
            rec("B0", "抽到全部 4 个真实函数", False, str(e))
            return

    drv = (DRV
           .replace("/*__FN_RESET__*/", parts["_resetRefreshBtn"])
           .replace("/*__FN_REFRESH__*/", parts["refreshStockNames"])
           .replace("/*__FN_POLL__*/", parts["pollRefreshStatus"])
           .replace("/*__FN_KEYDOWN__*/", parts["onInputKeydown"]))

    r = run_node(drv, "B0")
    if r is None:
        return

    # 先看"有没有炸"：删了 DOM 却留引用，唯一暴露方式就是运行期 TypeError
    rec("B1-1", "三个函数全程没有抛异常（无残留 DOM 引用）",
        r["threw"] == [], "抛了：" + repr(r["threw"]))

    L = r["launch"]
    rec("B2-1", "点击后发出 POST /api/stocks/refresh",
        len(L["fetches"]) >= 1 and L["fetches"][0]["url"] == "/api/stocks/refresh"
        and L["fetches"][0]["method"] == "POST", str(L["fetches"][:2]))
    rec("B2-2", "接着轮询 read/status（GET）",
        len(L["fetches"]) == 2
        and L["fetches"][1]["url"] == "/api/stocks/refresh/read/status"
        and L["fetches"][1]["method"] == "GET", str(L["fetches"]))
    rec("B2-3", "按钮置灰", L["disabled"] is True, str(L))
    rec("B2-4", "按钮高亮（.active）", L["active"] is True, str(L))
    rec("B2-5", "图标转圈（animation 含 spin）", "spin" in (L["anim"] or ""), repr(L["anim"]))
    rec("B2-6", "running=true 时挂 500ms 后再轮询",
        L["timers"] == [500], str(L["timers"]))

    R = r["reentry"]
    rec("B3-1", "置灰期间重复点击不再发请求（防重入）", R["delta"] == 0, str(R))

    D = r["done"]
    rec("B4-1", "running=false 后解除置灰", D["disabled"] is False, str(D))
    rec("B4-2", "跑完摘掉 .active", D["active"] is False, str(D))
    rec("B4-3", "跑完停转（animation 清空）", (D["anim"] or "") == "", repr(D["anim"]))
    rec("B4-4", "正常完成不弹窗", D["alerts"] == [], str(D["alerts"]))
    rec("B4-5", "正常完成后不再挂定时器", D["timers"] == 0, str(D["timers"]))

    F = r["fail"]
    rec("B5-1", "后端报错时弹窗一次", len(F["alerts"]) == 1 and "补全失败" in F["alerts"][0],
        str(F["alerts"]))
    rec("B5-2", "报错后按钮照样复位（否则永久置灰）",
        F["disabled"] is False and (F["anim"] or "") == "", str(F))

    J = r["reject"]
    rec("B6-1", "启动请求被拒时弹窗", len(J["alerts"]) == 1 and "启动刷新失败" in J["alerts"][0],
        str(J["alerts"]))
    rec("B6-2", "启动失败也复位（不留死键盘）", J["disabled"] is False, str(J))

    rec("B7-1", "回车 → loadStock 恰好一次", r["enter"]["loadCalls"] == 1,
        str(r["enter"]))
    rec("B7-2", "下拉未展开时方向键不触发加载（负向）", r["arrowIdle"]["loadCalls"] == 0,
        str(r["arrowIdle"]))


behavior_checks()

print("\n" + "=" * 60)
print("合计 %d 项，通过 %d，失败 %d" % (_passed + len(_failed), _passed, len(_failed)))
if _failed:
    print("\n失败明细：")
    for f in _failed:
        print("  - " + f)
sys.exit(1 if _failed else 0)

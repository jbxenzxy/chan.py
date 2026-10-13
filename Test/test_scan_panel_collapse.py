# -*- coding: utf-8 -*-
"""「扫描结果面板」收起交互契约（2026-10-13 需求）
====================================================================
需求（用户指定）：
  ⑴ 面板**展开**时，鼠标点在 **K 线绘图区 / 指标绘图区** ≡ 点「-」最小化面板；
     范围经用户拍板：只认绘图区，#chart-container 之外的顶部工具栏按钮等区域不响应。
  ⑵ 面板**最小化**后只剩「+」恢复键 —— × 只在展开态提供（关闭会清后端扫描缓存，
     属不可逆动作，收起后误点代价高）。
  附加（同轮确认）：Esc ≡ 最小化（逐层收起的最末层）；扫描中 × 灰化 + title 说明原因。

实现（Frontend/app.js / app.html / app.css）：
  · `_applyScanMinimized(min)` = 最小化/恢复的**唯一 DOM 写入点**，按钮 / 点绘图区 /
    Esc 三处都走它（按钮图标与 title 恒跟状态一致）；
  · `#chart-container` 上的 document mousedown 监听：未展开 / 已最小化一律 return（幂等，
    不能复用 toggle —— 否则最小化态点绘图区会把面板又弹开）；**只观察不拦截**
    （不 preventDefault / stopPropagation）；用 mousedown 而非 click（面板内按下、
    面板外松开时 click 的 target 是公共祖先，会被误判）；
  · `_setScanRunning(v)` = 扫描运行态的**唯一写入点**（原 8 处裸赋值收敛），同步
    × 的禁用态（扫描中 closeScanPanel 直接 return，灰化消除「点了没反应」）；
  · Esc 逐层收起：设置抽屉 → 扫描模式对话框 → 区间选择 → 面板最小化（一次一层）。

三层守护
--------------------------------------------------------------------
 [静态]  app.html / app.css / app.js 的结构与计数契约（唯一写入点、幂等 guard、
         不拦截、判定锚点唯一、window 面零新增 —— 防 test_phase6_guards 白名单红）；
 [真渲染] 无头 Chrome 打开真实页面：真点绘图区 / 面板内 / 顶部按钮 / Esc，
         断言 minimized 状态迁移 + 最小化态 × 的 computed display === 'none'
         + 禁用态样式生效 + 事件未被吞（容器 mousedown 计数）。
         浏览器不在位时降级 SKIP（静态层已覆盖）。

跑法：python Test/test_scan_panel_collapse.py（退出码 0/1 即判决）
"""
import functools
import glob
import http.server
import io
import json
import os
import re
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
APPJS = os.path.join(ROOT, "Frontend", "app.js")
APPHTML = os.path.join(ROOT, "Frontend", "app.html")
APPCSS = os.path.join(ROOT, "Frontend", "app.css")
FRONTEND_DIR = os.path.join(ROOT, "Frontend")

_OK = []


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  —— " + str(detail)[:260]))
    return cond


def _read(p):
    """行尾归一化：镜像（git archive，LF）与工作树（CRLF）都能跑。"""
    return io.open(p, encoding="utf-8").read().replace("\r\n", "\n")


def _slice(text, start_marker, end_marker="});"):
    """取 [start_marker, 其后首个 end_marker] 的片段（取不到返回空串）。"""
    i = text.find(start_marker)
    if i < 0:
        return ""
    j = text.find(end_marker, i)
    return text[i:j] if j > 0 else ""


# ══════════════════════════════════════════════════════════════════════
# ① 静态契约（DOM / CSS / app.js）
# ══════════════════════════════════════════════════════════════════════
def part_static():
    print("══ ① 静态契约（html 结构 / css 形态 / js 唯一写入点与监听语义）══")
    html = _read(APPHTML)
    css = _read(APPCSS)
    js = _read(APPJS)

    # ── DOM：两颗键的接线与 title ────────────────────────────────────
    check("① 展开态「-」= toggleScanMinimize()，带 title",
          '<span class="scan-minimize" onclick="toggleScanMinimize()" '
          'title="最小化面板">-</span>' in html)
    check("①「×」= closeScanPanel()，带 title（禁用态 title 由 _setScanRunning 覆写）",
          '<span class="scan-close" onclick="closeScanPanel()" '
          'title="关闭面板">×</span>' in html)
    _hdr_right = _slice(html, '<span class="scan-header-right">', "</div>")
    check("① 两颗键同处 .scan-header-right（防被挪出 header 后 CSS 隐藏失效）",
          "scan-minimize" in _hdr_right and "scan-close" in _hdr_right)

    # ── CSS：最小化态无 × / 扫描中 × 灰化 ────────────────────────────
    check("① 最小化态隐藏 ×（只留 + 恢复键）",
          ".scan-panel.minimized .scan-close { display: none; }" in css)
    check("① 最小化态**仍然**隐藏 body（原有契约未被挤掉）",
          ".scan-panel.minimized .scan-body { display: none; }" in css)
    _dis = _slice(css, ".scan-close.disabled {", "}")
    check("① 禁用态：灰化 + not-allowed",
          "opacity" in _dis and "not-allowed" in _dis, _dis)
    check("① 禁用态 hover **不**转红（否则看起来仍可点）",
          ".scan-close.disabled:hover" in css)

    # ── js：最小化统一入口 ──────────────────────────────────────────
    check("② `_applyScanMinimized` 定义恰 1 处",
          js.count("function _applyScanMinimized(min) {") == 1)
    check("② `classList.toggle(\"minimized\"` 全文件恰 1 处（唯一 DOM 写入点，"
          "防在别处再写一份状态）",
          js.count('classList.toggle("minimized"') == 1,
          js.count('classList.toggle("minimized"'))
    check("② toggleScanMinimize 走统一入口（图标/title 恒同步）",
          "_applyScanMinimized(!panel.classList.contains(\"minimized\"))" in js)
    check("②「点绘图区」与「Esc」也走统一入口（共 1 定义 + 3 调用）",
          js.count("_applyScanMinimized(") == 4, js.count("_applyScanMinimized("))

    # ── js：绘图区监听（判定锚点 / 幂等 / 不拦截 / mousedown）─────────
    # 注意：app.js 里有多个 mousedown 监听（stats / amo 面板各一），必须从**本段注释**
    #   起切，否则会切到别人的监听块上（曾出现「在错误的片段里断言 → 假通过」）。
    _mdown = _slice(js, "// 面板**展开**时，在 K 线绘图区")
    check("② 绘图区监听存在且判定锚点 = #chart-container（唯一）",
          'closest("#chart-container")' in _mdown
          and js.count('closest("#chart-container")') == 1,
          js.count('closest("#chart-container")'))
    check("② 幂等 guard：未展开 return + 已最小化 return（不能复用 toggle）",
          'contains("show")' in _mdown and 'contains("minimized")' in _mdown, _mdown[:200])
    # 只扫**代码行**（注释里本来就写着「不 preventDefault / stopPropagation」，
    #   连注释一起扫会把这条契约本身判红）。
    _mdown_code = "\n".join(l for l in _mdown.split("\n")
                            if not l.strip().startswith("//"))
    check("② 只观察不拦截：监听块（代码行）内无 preventDefault / stopPropagation",
          "preventDefault" not in _mdown_code
          and "stopPropagation" not in _mdown_code, _mdown_code[:200])
    check("② 用 mousedown 而非 click（面板内按下→面板外松开时 click 会误判为绘图区）",
          'closest("#chart-container")' not in
          _slice(js, 'document.addEventListener("click", function(e) {'))

    # ── js：扫描运行态唯一写入点 + × 禁用同步 ────────────────────────
    check("② `_setScanRunning` 定义恰 1 处、调用 8 处（原 8 个裸赋值点全收敛）",
          js.count("function _setScanRunning(v) {") == 1
          and js.count("_setScanRunning(true);") == 1
          and js.count("_setScanRunning(false);") == 7,
          (js.count("_setScanRunning(true);"), js.count("_setScanRunning(false);")))
    _bare = re.findall(r"(?<!let )\b_scanRunning = (?:true|false);", js)
    check("② 模块内再无 `_scanRunning = true/false` 裸赋值（含声明行豁免）",
          not _bare, _bare)
    _setter = _slice(js, "function _setScanRunning(v) {", "\n        }")
    check("② 禁用态在 `_setScanRunning` 内同步（disabled class + title 双向）",
          'classList.toggle("disabled", !!v)' in _setter
          and "扫描中不可关闭" in _setter and "关闭面板" in _setter, _setter[:220])

    # ── js：Esc 逐层收起（顺序 + 每层 return）────────────────────────
    _esc = _slice(js, "document.addEventListener('keydown', function(e) {")
    check("② Esc 分支含扫描模式对话框层（scanModeDialogCancel）",
          "scanModeDialogCancel();" in _esc)
    check("② Esc 分支末层 = 面板最小化（不是关闭 —— 关闭会清缓存）",
          "_applyScanMinimized(true);" in _esc
          and "closeScanPanel();" not in _esc, _esc[-260:])
    _order = [_esc.find("closeBspSettings();"),
              _esc.find("scanModeDialogCancel();"),
              _esc.find("_rangeSelect.mode === 'SELECTED_A'"),
              _esc.find("_applyScanMinimized(true);")]
    check("② 层序 = 抽屉 → 对话框 → 区间选择 → 面板（一次 Esc 只收一层，各层 return）",
          _order == sorted(_order) and -1 not in _order, _order)
    _sel = _esc[_order[2]:_order[3]] if -1 not in _order else ""
    check("② 区间选择层带 return（否则同一次 Esc 会连收两层）",
          re.search(r"render\(\);\s*\n\s*return;", _sel) is not None, _sel[-160:])

    # ── window 面零新增（防 test_phase6_guards 白名单假红）───────────
    check("② 内部函数不挂 window（window 白名单不动）",
          "window._applyScanMinimized" not in js
          and "window._setScanRunning" not in js)


# ══════════════════════════════════════════════════════════════════════
# ② 真渲染层（无头 Chrome；浏览器不在位降级 SKIP）
# ══════════════════════════════════════════════════════════════════════
SET_STATE = r"""
(args) => {
  const p = document.getElementById('scan-panel');
  // 用 != null 而不是 !== undefined：Python 侧 None 会被序列化成 JSON null，
  //   null !== undefined 为真 ⇒ 会把「不传的项」误当成 false 执行 toggle
  //   （曾实测：set_state(min=False) 顺手把 show 也 toggle 掉了，面板直接隐藏）。
  if (args.show != null) { p.classList.toggle('show', !!args.show); }
  if (args.min != null) { p.classList.toggle('minimized', !!args.min); }
  return { show: p.classList.contains('show'), min: p.classList.contains('minimized') };
}
"""

READ_STATE = r"""
() => {
  const p = document.getElementById('scan-panel');
  const btn = p.querySelector('.scan-minimize');
  return { show: p.classList.contains('show'),
           min: p.classList.contains('minimized'),
           icon: btn ? btn.textContent.trim() : '',
           title: btn ? btn.title : '',
           closeDisplay: getComputedStyle(p.querySelector('.scan-close')).display };
}
"""


def find_playwright():
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        return sync_playwright, ""
    except Exception as e:  # noqa: BLE001
        return None, str(e).splitlines()[0][:120]


def launch_browser(pw):
    """本机 Chrome 优先（与 test_scan_dialog_layout.py 同一处置）。"""
    notes = []
    try:
        return pw.chromium.launch(channel="chrome"), "本机 Chrome", notes
    except Exception as e:  # noqa: BLE001
        notes.append("channel=chrome: " + str(e).splitlines()[0][:90])
    try:
        return pw.chromium.launch(), "捆绑 chromium", notes
    except Exception as e:  # noqa: BLE001
        notes.append("捆绑 chromium: " + str(e).splitlines()[0][:90])
    for pat in ("~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe",
                "~/AppData/Local/ms-playwright/chromium-*/chrome-win/chrome.exe"):
        for cand in glob.glob(os.path.expanduser(pat)):
            try:
                return (pw.chromium.launch(executable_path=cand),
                        os.path.basename(cand), notes)
            except Exception as e:  # noqa: BLE001
                notes.append(os.path.basename(cand) + ": " + str(e).splitlines()[0][:90])
    return None, "", notes


def part_render():
    print("══ ② 真渲染（无头 Chrome 真点：绘图区 / 面板内 / 顶部按钮 / Esc）══")
    sync_playwright, why = find_playwright()
    if sync_playwright is None:
        print("  [SKIP] " + why + "（静态层已覆盖契约）")
        return

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=FRONTEND_DIR))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    with sync_playwright() as pw:
        browser, which, notes = launch_browser(pw)
        if browser is None:
            print("  [SKIP] 无可用无头浏览器：" + " / ".join(notes[:3]))
            httpd.shutdown()
            return
        print("  （无头浏览器：%s）" % which)
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:160]))

        def route_api(route):
            body = (json.dumps({"config": {"view_count": 233}})
                    if "/api/health" in route.request.url else "{}")
            route.fulfill(status=200, content_type="application/json", body=body)

        page.route("**/api/**", route_api)
        page.goto("http://127.0.0.1:%d/app.html" % port, wait_until="load")
        page.wait_for_timeout(800)

        # 首屏 API 打桩数据不足，页面会弹出 #error.error-overlay **全屏盖住绘图区**
        #   （本用例只测面板收起交互，与首屏数据无关）⇒ 显式隐藏；并把「探针点最上层
        #   元素属于容器」钉成前提 —— 否则会退化成「点了个寂寞」的假红（曾实测：
        #   点击全被 error-overlay 吃掉，容器 mousedown 计数恒 0）。
        page.evaluate("""() => {
             const e = document.getElementById('error');
             if (e) { e.style.display = 'none'; }
           }""")
        page.wait_for_timeout(120)

        chart = page.locator("#chart-container").bounding_box()
        check("② 前提：#chart-container 已在位（绘图区判定锚点）",
              bool(chart) and chart["width"] > 200 and chart["height"] > 200, chart)
        probe = [chart["x"] + chart["width"] * 0.75,
                 chart["y"] + chart["height"] * 0.35]
        top_el = page.evaluate(
            """([x, y]) => {
                 const el = document.elementFromPoint(x, y);
                 return el ? (el.tagName + (el.id ? '#' + el.id : '')
                              + (el.className ? '.' + String(el.className).split(' ')[0] : ''))
                           : 'NONE';
               }""", probe)
        check("② 前提：探针点最上层元素属于 #chart-container（无遮挡）",
              page.evaluate(
                  """([x, y]) => {
                       const el = document.elementFromPoint(x, y);
                       return !!(el && el.closest && el.closest('#chart-container'));
                     }""", probe), top_el)

        def set_state(show=None, min=None):
            return page.evaluate(SET_STATE, {"show": show, "min": min})

        def state():
            return page.evaluate(READ_STATE)

        # 事件未被吞：在容器上挂 mousedown 计数（本测试自身注入，非生产代码）
        page.evaluate(
            """() => {
                 window.__mDown = 0;
                 document.getElementById('chart-container')
                     .addEventListener('mousedown', () => { window.__mDown += 1; });
               }""")

        # ── A. 点绘图区 ⇒ 最小化（且事件正常派发到容器）──────────────
        set_state(show=True, min=False)
        page.mouse.click(probe[0], probe[1])
        page.wait_for_timeout(120)
        s = state()
        check("A 点绘图区 → 面板最小化", s["min"] is True and s["show"] is True, s)
        check("A 按钮图标 / title 同步成「+」/ 恢复面板（走统一入口，不是裸 classList）",
              s["icon"] == "+" and s["title"] == "恢复面板", s)
        check("A 事件未被吞：容器自身 mousedown 计数 == 1",
              page.evaluate("() => window.__mDown") == 1)

        # ── B. 已最小化时再点绘图区 ⇒ 幂等（不回弹）──────────────────
        page.mouse.click(probe[0], probe[1])
        page.wait_for_timeout(120)
        s = state()
        check("B 已最小化再点绘图区 → 仍最小化（不能是 toggle）", s["min"] is True, s)

        # ── C. 点面板内部 ⇒ 不最小化 ─────────────────────────────────
        set_state(min=False)
        header = page.locator("#scan-panel .scan-header").bounding_box()
        check("C 前提：面板 header 可见（量测前提）", bool(header),
              (header, state()))
        if header:
            page.mouse.click(header["x"] + header["width"] * 0.35,
                             header["y"] + header["height"] * 0.5)
            page.wait_for_timeout(120)
            s = state()
            check("C 点面板内部 → 不最小化", s["min"] is False, s)

        # ── D. 点顶部工具栏按钮 ⇒ 不最小化 ───────────────────────────
        btn = page.locator("#btn-scan").bounding_box()
        check("D 前提：#btn-scan（顶部按钮）在位", bool(btn), btn)
        page.mouse.click(btn["x"] + btn["width"] * 0.5, btn["y"] + btn["height"] * 0.5)
        page.wait_for_timeout(150)
        s = state()
        check("D 点顶部按钮 → 不最小化（范围只认绘图区）", s["min"] is False, s)
        page.evaluate("() => { try { scanModeDialogCancel(); } catch (e) {} }")
        set_state(min=False)

        # ── E. 最小化态「×」被隐藏（computed），展开态在 ────────────
        set_state(min=True)
        s = state()
        check("E 最小化态 × 的 computed display === 'none'（真渲染，非仅 CSS 字面）",
              s["closeDisplay"] == "none", s)
        set_state(min=False)
        s = state()
        check("E 展开态 × 恢复可见", s["closeDisplay"] != "none", s)

        # ── F. 扫描中 × 禁用态样式真生效 ─────────────────────────────
        dis = page.evaluate(
            """() => {
                 const el = document.querySelector('#scan-panel .scan-close');
                 el.classList.add('disabled');
                 const cs = getComputedStyle(el);
                 const out = { opacity: cs.opacity, cursor: cs.cursor, color: cs.color };
                 el.classList.remove('disabled');
                 return out;
               }""")
        check("F 禁用态：opacity < 1 且 cursor = not-allowed",
              float(dis["opacity"]) < 1 and dis["cursor"] == "not-allowed", dis)

        # ── G. Esc ⇒ 最小化（展开态）────────────────────────────────
        set_state(min=False)
        page.keyboard.press("Escape")
        page.wait_for_timeout(120)
        s = state()
        check("G Esc → 面板最小化（且不是关闭：面板仍 show）",
              s["min"] is True and s["show"] is True, s)

        # ── H. Esc 在对话框打开时只收对话框（一次一层）──────────────
        set_state(min=False)
        page.evaluate(
            "() => document.getElementById('scan-mode-dialog').classList.add('show')")
        page.keyboard.press("Escape")
        page.wait_for_timeout(120)
        dlg = page.evaluate(
            "() => document.getElementById('scan-mode-dialog')"
            ".classList.contains('show')")
        s = state()
        check("H 对话框开着时 Esc 只收对话框，面板保持展开（一次只收一层）",
              dlg is False and s["min"] is False, (dlg, s))

        page.unroute_all(behavior="ignore")
        browser.close()
    httpd.shutdown()

    if errs:
        print("  （页面非致命 JS 错误 %d 条，不判红：%s）" % (len(errs), errs[:2]))


def main():
    print("=" * 68)
    print("扫描结果面板收起交互契约（点绘图区最小化 / 最小化态无 × / Esc / 扫描中灰化）")
    print("=" * 68)
    part_static()
    part_render()
    print("-" * 68)
    total, passed = len(_OK), sum(_OK)
    print("合计 %d 项，通过 %d，失败 %d" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

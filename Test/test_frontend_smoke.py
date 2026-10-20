# -*- coding: utf-8 -*-
"""
补测试缺口 ④ —— 前端 JS 冒烟测试
=====================================================================
背景：前端为「零构建」原生 JS（Frontend/app.js），无打包器/无类型检查，
重构（阶段 6 组件化）后缺少「前端可加载、关键入口存在」的冒烟守护。
本用例做静态冒烟（不依赖真实浏览器/后端）：

  ① HTML 骨架：app.html 引用 app.js / app.css；关键 DOM 元素存在
     （stock-code-input / chart-container / freq-selector / scan-panel）
  ② JS 语法：node --check app.js 通过（零构建下语法错误会直接白屏）
  ③ 全局入口：window 级函数存在（switchFreq / toggleDualWindow /
     loadStock / startScanZxg / toggleStats 等）
  ⑤ 事件引用：onclick 内联引用与 window 函数一一对应（防死引用）
  ⑦ 代码输入框聚焦不清空：执行 app.html 里**真实的 onfocus 表达式**（node 桩 DOM），
     断言聚焦后 input.value 原样保留、历史下拉被展开、× 清除按钮亮出

注：已删除 ChanApp.components 组件注册表（纯写入死结构，仅被本用例
用正则"确认字符串存在"，实际无任何消费方）及其守护项，④ 不再存在。

运行：python Test/test_frontend_smoke.py [--update]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
FRONTEND_DIR = os.path.join(REPO_ROOT, "Frontend")
ENTRY_HTML = os.path.join(FRONTEND_DIR, "app.html")
APP_JS = os.path.join(FRONTEND_DIR, "app.js")
APP_CSS = os.path.join(FRONTEND_DIR, "app.css")

# 关键 DOM 元素（阶段 6 组件化后仍须存在）
REQUIRED_DOM_IDS = [
    "stock-code-input", "chart-container", "freq-selector",
    "scan-panel", "scan-body", "stats-panel",
    "bsp-filter-dialog", "annotation-menu", "annotation-dialog",
]

# 全局 window 函数入口（HTML onclick / onchange 直接引用）
REQUIRED_WINDOW_FUNCS = [
    "switchFreq", "toggleDualWindow", "loadStock", "startScanZxg",
    "toggleStats", "toggleOverlay",
    "refreshStockNames", "openBspSettings", "closeBspSettings",
    "saveScanToZxg", "closeScanPanel", "toggleScanMinimize",
    "scanModeDialogConfirm", "scanModeDialogCancel",
    "annotationAdd", "annotationDialogConfirm", "annotationDialogCancel",
    "cancelSelectedPoint", "toggleMirrorMode", "annotationReplayToHere",
    "handleDateChange", "handleDateKeydown", "handleDateInput",
    "onInputKeydown", "onInputChange", "clearInput", "showHistory",
]

# KLineChart 组件契约方法（阶段 6 组件化对外接口；注册表删除后
# 由 window 绑定面 + ② 渲染管线守护接管，此处仅保留文档）

def test_html_skeleton(failures):
    """① HTML 骨架：引用 app.js/app.css + 关键 DOM 元素"""
    if not os.path.exists(ENTRY_HTML):
        failures.append("① app.html 不存在")
        print("[FAIL] ① app.html 不存在")
        return
    with open(ENTRY_HTML, encoding="utf-8") as f:
        html = f.read()
    if 'src="app.js' not in html:
        failures.append("① app.html 未引用 app.js")
        print("[FAIL] ① 未引用 app.js")
        return
    if 'href="app.css"' not in html:
        failures.append("① app.html 未引用 app.css")
        print("[FAIL] ① 未引用 app.css")
        return
    missing = [i for i in REQUIRED_DOM_IDS if f'id="{i}"' not in html]
    if missing:
        failures.append(f"① 缺失 DOM 元素: {missing}")
        print(f"[FAIL] ① 缺失 DOM: {missing}")
        return
    print(f"[PASS] ① HTML 骨架: app.js/app.css 引用 + {len(REQUIRED_DOM_IDS)} 个关键 DOM")


def test_js_syntax(failures):
    """② JS 语法：node --check app.js 通过（零构建白屏守护）"""
    if not os.path.exists(APP_JS):
        failures.append("② app.js 不存在")
        print("[FAIL] ② app.js 不存在")
        return
    try:
        proc = subprocess.run(
            ["node", "--check", APP_JS],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=60)
    except FileNotFoundError:
        failures.append("② node 不可用，无法做语法检查")
        print("[FAIL] ② node 不可用")
        return
    except subprocess.TimeoutExpired:
        failures.append("② node --check 超时")
        print("[FAIL] ② node --check 超时")
        return
    if proc.returncode != 0:
        failures.append(f"② app.js 语法错误:\n{proc.stdout[:500]}")
        print(f"[FAIL] ② app.js 语法错误: {proc.stdout[:300]}")
        return
    print("[PASS] ② JS 语法: node --check app.js 通过")


def test_window_api_surface(failures):
    """③ 全局入口：window 级函数存在（HTML 内联引用不悬空）"""
    if not os.path.exists(APP_JS):
        failures.append("③ app.js 不存在")
        print("[FAIL] ③ app.js 不存在")
        return
    with open(APP_JS, encoding="utf-8") as f:
        js = f.read()
    missing = []
    for fn in REQUIRED_WINDOW_FUNCS:
        # 匹配 window.fn = function 或 function fn( 定义
        if not re.search(rf"window\.{re.escape(fn)}\s*=", js) and \
           not re.search(rf"function\s+{re.escape(fn)}\s*\(", js):
            missing.append(fn)
    if missing:
        failures.append(f"③ 缺失 window 函数: {missing}")
        print(f"[FAIL] ③ 缺失 window 函数: {missing}")
        return
    print(f"[PASS] ③ 全局入口: {len(REQUIRED_WINDOW_FUNCS)} 个 window 函数均在")


def test_inline_event_refs(failures):
    """⑤ 事件引用：HTML onclick/onchange 内联引用与 window 函数一一对应"""
    if not os.path.exists(ENTRY_HTML) or not os.path.exists(APP_JS):
        failures.append("⑤ app.html/app.js 缺失")
        print("[FAIL] ⑤ app.html/app.js 缺失")
        return
    with open(ENTRY_HTML, encoding="utf-8") as f:
        html = f.read()
    with open(APP_JS, encoding="utf-8") as f:
        js = f.read()
    # 收集 HTML 内联事件处理器中调用的函数名
    refs = set()
    for m in re.finditer(r"(?:onclick|onchange|onkeydown|oninput|onfocus|onblur)=\"([^\"]+)\"", html):
        for fn in re.findall(r"([A-Za-z_$][\w$]*)\s*\(", m.group(1)):
            refs.add(fn)
    # 过滤 JS 内建/字面量（DOM API 等）
    builtin = {"if", "event", "document", "getElementById", "this", "target",
               "parseInt", "parseFloat", "String", "Number", "Math", "Date",
               "JSON", "Object", "Array", "console", "window", "setTimeout",
               "setInterval", "clearTimeout", "clearInterval", "requestAnimationFrame",
               "focus", "blur", "preventDefault", "stopPropagation", "length"}
    dangling = sorted(fn for fn in refs
                      if fn not in builtin
                      and not re.search(rf"window\.{re.escape(fn)}\s*=", js)
                      and not re.search(rf"function\s+{re.escape(fn)}\s*\(", js))
    if dangling:
        failures.append(f"⑤ 悬空事件引用: {dangling}")
        print(f"[FAIL] ⑤ 悬空事件引用: {dangling}")
        return
    print(f"[PASS] ⑤ 事件引用: {len(refs)} 个内联引用全部有对应定义")


def test_chart_action_guards(failures):
    """⑥ N1 防回潮：每个「chartData 全文替换点」都必须受并发守卫保护

    背景：遗留缺口 N1 —— 前端并发请求后到响应覆盖先到（图表
    「闪回」）。修复按数据源分三类守卫，本用例分别静态断言：

      A. HTTP fetch 响应链 —— 请求序号守卫三件套：
         _bumpChartActionSeq()（请求前捕获）+ _isChartActionStale(_seq)
         （响应与 catch 中丢弃过期）。全文替换点所在函数向上必须有序号
         捕获、就近（前 15 / 后 80 行）必须有守卫丢弃。
      B. 期货 SSE 生命周期（connectRealtimeInit / connectRealtimeDual /
         handleRealtimeDataSingle / handleRealtimeDataDual）—— 守卫机制是
         「建新流前必先 disconnectRealtime() 断旧流」，旧连接事件不可能
         与新连接交错。断言 connect* 函数体内先断流再 new EventSource。
      C. 同步渲染入口 _renderChart —— 参数直传、调用内同步换引用，无
         异步窗口；守卫责任在各异步调用源（A/B 类），此处显式白名单。

    任何新增全文替换点落入 A 类而漏挂守卫、或改动图表数据管线（如给
    _renderChart 新增异步调用源、重命名 SSE 函数），此处即失败——此时应
    同步更新本守护的白名单与类别归属。
    """
    if not os.path.exists(APP_JS):
        failures.append("⑥ app.js 不存在")
        print("[FAIL] ⑥ app.js 不存在")
        return
    with open(APP_JS, encoding="utf-8") as f:
        lines = f.read().splitlines()

    if not any("_chartActionSeq = 0" in ln for ln in lines):
        failures.append("⑥ 缺少守卫序号声明 _chartActionSeq = 0（N1 修复被整体移除？）")
        print("[FAIL] ⑥ 缺少 _chartActionSeq 声明")
        return

    FUNC_RE = re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\(")

    def enclosing_func(idx):
        """向上找最近的函数定义名（零构建前端的轻量近似）"""
        for j in range(idx, -1, -1):
            m = FUNC_RE.search(lines[j])
            if m:
                return m.group(1)
        return ""

    # B 类：SSE 生命周期守卫 —— connect* 必须先断流再建新流
    SSE_CONNECT = ("connectRealtimeInit", "connectRealtimeDual")
    for fn in SSE_CONNECT:
        body = "\n".join(lines)
        m = re.search(rf"function\s+{fn}\s*\(", body)
        ok = False
        if m:
            # 取函数体近似区段（到下一个顶级 function 为止），断言先断流后建流
            seg = body[m.start():m.start() + 6000]
            disc = seg.find("disconnectRealtime()")
            esrc = seg.find("new EventSource")
            ok = disc != -1 and esrc != -1 and disc < esrc
        if not ok:
            failures.append(f"⑥ {fn} 未满足「先 disconnectRealtime() 再 new EventSource」")
            print(f"[FAIL] ⑥ {fn} SSE 建流前未断旧流")
            return
    print(f"[PASS] ⑥-B SSE 生命周期守卫: {len(SSE_CONNECT)} 个 connect* 均先断流再建流")

    # A/C 类：逐个 chartData 全文替换点判定
    SSE_HANDLED = {"connectRealtimeInit", "connectRealtimeDual",
                   "handleRealtimeDataSingle", "handleRealtimeDataDual"}
    SYNC_ENTRY = {"_renderChart"}      # C 类：同步入口，守卫在异步调用源
    sites = [i for i, ln in enumerate(lines)
             if re.search(r"chartData\s*=\s*data\b", ln)
             and "let chartData" not in ln]        # 排除声明行
    unguarded = []
    for i in sites:
        fn = enclosing_func(i)
        if fn in SSE_HANDLED:
            continue                                # B 类：连接生命周期守卫
        if fn in SYNC_ENTRY:
            continue                                # C 类：同步换引用
        back = lines[max(0, i - 120):i]
        # 找到本站点的序号捕获行（相对 back 的下标）
        cap_rel = next((j for j in range(len(back) - 1, -1, -1)
                        if re.search(r"=\s*_bumpChartActionSeq\(\)", back[j])), None)
        if cap_rel is None:
            unguarded.append(f"L{i + 1}({fn}):无序号捕获")
            continue
        cap_abs = i - len(back) + cap_rel
        # 守卫丢弃必须严格落在「捕获行与赋值行之间」（then 处理器首语句
        # 的统一插桩形态）。这个位置不变量使「删 then 守卫」必被抓到——
        # catch 侧守卫是纵深防御，位置在赋值之后，不参与本判定。
        guarded = any("_isChartActionStale(_seq)" in ln
                      for ln in lines[cap_abs + 1:i])
        if not guarded:
            unguarded.append(f"L{i + 1}({fn}):捕获与赋值之间无守卫丢弃")
    if unguarded:
        failures.append(
            f"⑥ {len(unguarded)} 处 chartData 全文替换点缺 N1 守卫：{unguarded}")
        print(f"[FAIL] ⑥ chartData 替换点缺守卫: {unguarded}")
        return
    print(f"[PASS] ⑥-A N1 防回潮: {len(sites)} 处 chartData 替换点全部受守卫保护"
          "（HTTP 序号守卫 / SSE 生命周期 / 同步入口白名单）")


def test_code_input_focus_keeps_value(failures):
    """⑦ 聚焦不清空：执行 app.html 真实的 onfocus 表达式，输入框文本必须原样保留

    需求背景（2026-10-20 用户反馈）：期货页输入 CFFEX.IF2409 回车后，从图表点回
    输入框想只把末两位 09 改成 10 —— 旧的绑定「聚焦即先 clearInput() 再展开历史」
    会在聚焦瞬间把代码抹掉，导致只能整串重打。

    做法（不是「整段文本包含关键字」式静态断言）：从 app.html 抽出 #stock-code-input
    的 onfocus 属性原文，连同 app.js 里**真实的** clearInput / toggleInputClear /
    onCodeInputFocus 函数体一起丢进 node 跑；桩 DOM 的 input.value 预置为
    "CFFEX.IF2409"，然后断言：
      · 执行 onfocus 后 value 仍是 "CFFEX.IF2409"（被清空即红）；
      · showHistory 被调用恰好 1 次（下拉没展开也红）；
      · × 清除按钮由 display:none 变为可见（整串重输的出口没被砍掉）。
    判别力：把 app.html 的绑定改回「先 clearInput() 再 showHistory()」必红
    （交付报告有变异记录）。
    """
    if not (os.path.exists(ENTRY_HTML) and os.path.exists(APP_JS)):
        failures.append("⑦ app.html/app.js 缺失")
        print("[FAIL] ⑦ app.html/app.js 缺失")
        return
    with open(ENTRY_HTML, encoding="utf-8") as f:
        html = f.read()
    with open(APP_JS, encoding="utf-8") as f:
        js = f.read()

    m = re.search(r'<input[^>]*id="stock-code-input"[^>]*onfocus="([^"]*)"', html)
    if not m:
        failures.append("⑦ 找不到 #stock-code-input 的 onfocus 绑定")
        print("[FAIL] ⑦ 找不到 #stock-code-input 的 onfocus 绑定")
        return
    onfocus_attr = m.group(1)
    # 根因直判：失败信息比行为断言更直给
    if "clearInput" in onfocus_attr:
        failures.append("⑦ onfocus 仍调用 clearInput（聚焦即清空，改末两位会丢上下文）: "
                        + onfocus_attr)
        print("[FAIL] ⑦ onfocus 仍含 clearInput: " + onfocus_attr)
        return

    node = shutil.which("node")
    if not node:
        print("[SKIP] ⑦ node 不在位，聚焦行为层跳过（② 已单独判 node 缺失）")
        return

    def grab(fn_name):
        """抽 `window.<fn_name> = function ... };` 整段（按花括号配平）。"""
        start = js.find("window.%s = function" % fn_name)
        if start < 0:
            return None
        i = js.find("{", start)
        depth, j = 0, i
        while j < len(js):
            if js[j] == "{":
                depth += 1
            elif js[j] == "}":
                depth -= 1
                if depth == 0:
                    return js[start:j + 1] + ";"
            j += 1
        return None

    parts = []
    for fn_name in ("clearInput", "toggleInputClear", "onCodeInputFocus"):
        seg = grab(fn_name)
        if not seg:
            failures.append("⑦ app.js 缺失 window.%s（聚焦链路上的函数）" % fn_name)
            print("[FAIL] ⑦ app.js 缺失 window.%s" % fn_name)
            return
        parts.append(seg)

    harness = "\n".join([
        "// 桩 DOM：只建聚焦链路用到的元素（缺失即 null，保持真实语义）",
        "var input = { value: 'CFFEX.IF2409', style: {} };",
        "var clearBtn = { style: { display: 'none' } };",
        "var histCalls = 0;",
        "function showHistory() { histCalls++; }",
        "var document = { getElementById: function (id) {",
        "    if (id === 'stock-code-input') { return input; }",
        "    if (id === 'input-clear') { return clearBtn; }",
        "    return null;",
        "} };",
        "var window = globalThis;   // 浏览器里 window.fn = ... 即全局函数，node 用 globalThis 等价复现",
    ] + parts + [
        "// 执行 app.html 里真实的 onfocus 表达式",
        "try { " + onfocus_attr + "; } catch (e) {",
        "    console.log(JSON.stringify({ error: String(e) }));",
        "    process.exit(0);",
        "}",
        "console.log(JSON.stringify({",
        "    value: input.value,",
        "    clearShown: clearBtn.style.display,",
        "    histCalls: histCalls",
        "}));",
    ]) + "\n"

    fd, jf = tempfile.mkstemp(suffix=".js", prefix="focus_guard_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(harness)
        proc = subprocess.run([node, jf], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        if proc.returncode != 0:
            failures.append("⑦ node 执行聚焦 harness 失败: "
                            + (proc.stderr or proc.stdout)[:300])
            print("[FAIL] ⑦ node 执行失败")
            return
        out = (proc.stdout or "").strip().splitlines()
        res = json.loads(out[-1]) if out else {}
        if res.get("error"):
            failures.append("⑦ onfocus 表达式执行抛错: " + res["error"])
            print("[FAIL] ⑦ onfocus 抛错: " + res["error"])
            return
        if res.get("value") != "CFFEX.IF2409":
            failures.append("⑦ 聚焦后输入框被改写（期望 CFFEX.IF2409，实际 %r）"
                            % res.get("value"))
            print("[FAIL] ⑦ 聚焦后 value=%r" % res.get("value"))
            return
        if res.get("histCalls") != 1:
            failures.append("⑦ 聚焦未展开历史下拉（showHistory 调用 %r 次）"
                            % res.get("histCalls"))
            print("[FAIL] ⑦ showHistory 调用 %r 次" % res.get("histCalls"))
            return
        if res.get("clearShown") != "":
            failures.append("⑦ 有内容时 × 清除按钮未亮出（display=%r）"
                            % res.get("clearShown"))
            print("[FAIL] ⑦ × 未亮出 display=%r" % res.get("clearShown"))
            return
    finally:
        try:
            os.unlink(jf)
        except OSError:
            pass

    print("[PASS] ⑦ 聚焦不清空: onfocus=%s 保留 value + 展开历史 + 亮出 ×" % onfocus_attr)


def test_history_dropdown_keyboard(failures):
    """⑧ 历史下拉键盘导航：↑/↓ + Enter（只有按过方向键才用高亮项）

    需求背景（2026-10-20 用户拍板）：历史下拉此前只能鼠标点。根因是
    `onInputKeydown` 用 `searchResults.length` 当门禁，而 `showHistory()`
    会把 searchResults 清空（历史视图本就不经搜索）—— 于是展开历史后
    方向键直接落进「不可导航」早退分支。

    语义（用户明确选择）：下拉展开时按 Enter，
      · 没按过方向键 → 加载**输入框里的文字**（不会莫名选中历史第一条）；
      · 按过方向键 → 加载**高亮项**。
    实现载具是 `selectedIndex`：搜索结果由 doSearch 预设 0（保持"敲关键词
    回车选第一个"的老手感），历史视图由 showHistory 预设 -1，只有 ↑/↓ 能
    把它变成非负。

    做法：⑧-A 静态断言「三处渲染都带 data-code」+「门禁不再依赖 searchResults」；
    ⑧-B 把 app.js 里**真实的** onInputKeydown / updateSearchSelection 丢进
    node，配桩 DOM（querySelectorAll 只认 .stock-history-item[data-code]）
    跑七条键盘序列，逐条比对。
    判别力：把门禁改回 searchResults、或把 Enter 分支改回「恒用高亮项」，
    对应断言必红（交付报告有变异记录）。
    """
    if not (os.path.exists(ENTRY_HTML) and os.path.exists(APP_JS)):
        failures.append("⑧ app.html/app.js 缺失")
        print("[FAIL] ⑧ app.html/app.js 缺失")
        return
    with open(APP_JS, encoding="utf-8") as f:
        js = f.read()

    # ── ⑧-A 静态：data-code 是键鼠同源的唯一契约载体 ──────────
    for label, needle in (
        ("showHistory 固定入口项", 'data-code="${escAttr(item.code)}"'),
        ("showHistory 用户历史项", 'data-code="${escAttr(c.code)}"'),
        ("doSearch 搜索结果项", 'data-code="${escAttr(rawFullCode)}"'),
    ):
        if needle not in js:
            failures.append("⑧ %s 缺 data-code（键盘回车拿不到要加载的代码）" % label)
            print("[FAIL] ⑧ %s 缺 %s" % (label, needle))
            return

    kd_start = js.find("window.onInputKeydown = function")
    kd_end = js.find("window.updateSearchSelection = function")
    if kd_start < 0 or kd_end < 0 or kd_end <= kd_start:
        failures.append("⑧ 找不到 onInputKeydown / updateSearchSelection")
        print("[FAIL] ⑧ 找不到键盘处理函数")
        return
    kd_src = js[kd_start:kd_end]
    if "!searchResults.length" in kd_src:
        failures.append("⑧ onInputKeydown 仍以 searchResults 为空当门禁"
                        "（历史视图下它恒空 ⇒ 方向键全部失效）")
        print("[FAIL] ⑧ 门禁仍依赖 searchResults")
        return

    node = shutil.which("node")
    if not node:
        print("[SKIP] ⑧ node 不在位，键盘行为层跳过（② 已单独判 node 缺失）")
        return

    def grab(fn_name):
        start = js.find("window.%s = function" % fn_name)
        if start < 0:
            return None
        i = js.find("{", start)
        depth, j = 0, i
        while j < len(js):
            if js[j] == "{":
                depth += 1
            elif js[j] == "}":
                depth -= 1
                if depth == 0:
                    return js[start:j + 1] + ";"
            j += 1
        return None

    parts = []
    for fn_name in ("onInputKeydown", "updateSearchSelection"):
        seg = grab(fn_name)
        if not seg:
            failures.append("⑧ app.js 缺失 window.%s" % fn_name)
            print("[FAIL] ⑧ app.js 缺失 window.%s" % fn_name)
            return
        parts.append(seg)

    harness = "\n".join([
        "var selectedIndex = -1;",
        "// 历史视图下 searchResults 的真实值就是空数组（showHistory 会清空它）；",
        "// 显式声明是为了让「门禁误用 searchResults」的变异落到语义断言上，",
        "// 而不是先抛 ReferenceError（后者也算红，但失败信息不直给）。",
        "var searchResults = [];",
        "var log = [];",
        "function loadStock() { log.push(['loadStock']); }",
        "function selectHistory(c) { log.push(['selectHistory', c]); }",
        "function mkItem(code) {",
        "    return { dataset: { code: code }, style: {}, scrolled: 0,",
        "             scrollIntoView: function () { this.scrolled++; } };",
        "}",
        "var items = [mkItem('sh600519'), mkItem('sh000001'), mkItem('KQ.m@CFFEX.IM')];",
        "var shown = true, removed = 0;",
        "var el = {",
        "    classList: {",
        "        contains: function (c) { return c === 'show' && shown; },",
        "        remove: function (c) { if (c === 'show') { shown = false; removed++; } }",
        "    },",
        "    // 只认带 data-code 的条目 —— 与真实选择器同义",
        "    querySelectorAll: function (sel) {",
        "        return (sel === '.stock-history-item[data-code]' && shown) ? items : [];",
        "    }",
        "};",
        "var document = { getElementById: function (id) {",
        "    return id === 'stock-history' ? el : null;",
        "} };",
        "var window = globalThis;",
    ] + parts + [
        "function key(k) {",
        "    var ev = { key: k, defaultPrevented: false,",
        "               preventDefault: function () { this.defaultPrevented = true; } };",
        "    onInputKeydown(ev);",
        "    return ev;",
        "}",
        "var out = {};",
        "// T1 历史视图未导航 → 回车 = 加载输入框文字（新语义核心）",
        "log = []; key('Enter');",
        "out.t1 = { log: log.slice(), idx: selectedIndex };",
        "// T2 ↓ 一次 → 高亮 0；回车 → 选中它",
        "log = []; key('ArrowDown'); out.t2idx = selectedIndex;",
        "key('Enter'); out.t2 = log.slice(); out.t2scroll = items[0].scrolled;",
        "// T3 从 0 向上 → 末项（wrap）",
        "selectedIndex = 0; key('ArrowUp'); out.t3idx = selectedIndex;",
        "// T4 从 -1 向上 → 应是末项，不是 len-2",
        "selectedIndex = -1; key('ArrowUp'); out.t4idx = selectedIndex;",
        "// T5 搜索结果视图（doSearch 预设 0）→ 回车仍选第一个",
        "log = []; selectedIndex = 0; key('Enter'); out.t5 = log.slice();",
        "// T6 Esc 关闭",
        "selectedIndex = -1; key('Escape'); out.t6removed = removed;",
        "// T7 下拉未展开时回车 → 直接加载",
        "log = []; key('Enter'); out.t7 = log.slice();",
        "// T8 ↓ 必须阻止默认行为（否则光标在输入框里乱跳）",
        "shown = true; selectedIndex = -1; out.t8prevented = key('ArrowDown').defaultPrevented;",
        "out.navCount = items.length;",
        "console.log(JSON.stringify(out));",
    ]) + "\n"

    fd, jf = tempfile.mkstemp(suffix=".js", prefix="hist_nav_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(harness)
        proc = subprocess.run([node, jf], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        if proc.returncode != 0:
            failures.append("⑧ node 执行键盘 harness 失败: "
                            + (proc.stderr or proc.stdout)[:300])
            print("[FAIL] ⑧ node 执行失败")
            return
        out = (proc.stdout or "").strip().splitlines()
        res = json.loads(out[-1]) if out else {}
    finally:
        try:
            os.unlink(jf)
        except OSError:
            pass

    def bad(msg):
        failures.append(msg)
        print("[FAIL] " + msg)

    if res.get("t1", {}).get("log") != [["loadStock"]]:
        bad("⑧ 历史视图未导航直接回车应加载输入框文字，实际 %r"
            % res.get("t1", {}).get("log"))
        return
    if res.get("t1", {}).get("idx") != -1:
        bad("⑧ 未导航时 selectedIndex 被改写为 %r（回车会误选历史第一条）"
            % res["t1"]["idx"])
        return
    if res.get("t2idx") != 0:
        bad("⑧ ↓ 后高亮下标应为 0，实际 %r" % res.get("t2idx"))
        return
    if res.get("t2") != [["selectHistory", "sh600519"]]:
        bad("⑧ ↓ 后回车应选中高亮项 sh600519，实际 %r" % res.get("t2"))
        return
    if res.get("t3idx") != res.get("navCount", 0) - 1:
        bad("⑧ 从 0 上移应回绕到末项，实际 %r" % res.get("t3idx"))
        return
    if res.get("t4idx") != res.get("navCount", 0) - 1:
        bad("⑧ 从 -1 上移应落到末项（off-by-one），实际 %r" % res.get("t4idx"))
        return
    if res.get("t5") != [["selectHistory", "sh600519"]]:
        bad("⑧ 搜索结果视图回车应选预设的第 0 项，实际 %r" % res.get("t5"))
        return
    if res.get("t6removed") != 1:
        bad("⑧ Esc 未关闭下拉（classList.remove('show') 调用 %r 次）"
            % res.get("t6removed"))
        return
    if res.get("t7") != [["loadStock"]]:
        bad("⑧ 下拉未展开时回车应直接加载，实际 %r" % res.get("t7"))
        return
    if not res.get("t8prevented"):
        bad("⑧ 方向键未 preventDefault（光标会在输入框内乱跳）")
        return
    if not res.get("t2scroll"):
        bad("⑧ 高亮项未 scrollIntoView（长历史列表高亮到屏外等于没反应）")
        return

    print("[PASS] ⑧ 历史下拉键盘导航: 未导航回车=加载输入框 / ↑↓后回车=选中高亮项"
          " / 回绕与滚动跟随均正确")


def main():
    ap = argparse.ArgumentParser(description="P2-2 ④ 前端 JS 冒烟测试")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    args = ap.parse_args()

    failures = []
    test_html_skeleton(failures)
    test_js_syntax(failures)
    test_window_api_surface(failures)
    test_inline_event_refs(failures)
    test_code_input_focus_keeps_value(failures)
    test_history_dropdown_keyboard(failures)
    test_chart_action_guards(failures)

    print()
    if failures:
        print(f"===== P2-2 ④ 前端 JS 冒烟测试: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== P2-2 ④ 前端 JS 冒烟测试: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

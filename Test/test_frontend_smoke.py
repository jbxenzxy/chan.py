# -*- coding: utf-8 -*-
"""
P2-2 补测试缺口 ④ —— 前端 JS 冒烟测试
=====================================================================
背景：前端为「零构建」原生 JS（Frontend/app.js），无打包器/无类型检查，
重构（阶段 6 组件化）后缺少「前端可加载、关键入口存在」的冒烟守护。
本用例做静态冒烟（不依赖真实浏览器/后端）：

  ① HTML 骨架：index.html 引用 app.js / app.css；关键 DOM 元素存在
     （stock-code-input / chart-container / freq-selector / scan-panel）
  ② JS 语法：node --check app.js 通过（零构建下语法错误会直接白屏）
  ③ 全局入口：window 级函数存在（switchFreq / toggleDualWindow /
     loadStock / startScanZxg / toggleStats 等）
  ⑤ 事件引用：onclick 内联引用与 window 函数一一对应（防死引用）

注：P1-6 已删除 ChanApp.components 组件注册表（纯写入死结构，仅被本用例
用正则"确认字符串存在"，实际无任何消费方）及其守护项，④ 不再存在。

运行：python Test/test_frontend_smoke.py [--update]
"""
import argparse
import os
import re
import subprocess
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
FRONTEND_DIR = os.path.join(REPO_ROOT, "Frontend")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")
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

# KLineChart 组件契约方法（阶段 6 组件化对外接口；P1-6 注册表删除后
# 由 window 绑定面 + ② 渲染管线守护接管，此处仅保留文档）

def test_html_skeleton(failures):
    """① HTML 骨架：引用 app.js/app.css + 关键 DOM 元素"""
    if not os.path.exists(INDEX_HTML):
        failures.append("① index.html 不存在")
        print("[FAIL] ① index.html 不存在")
        return
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    if 'src="app.js' not in html:
        failures.append("① index.html 未引用 app.js")
        print("[FAIL] ① 未引用 app.js")
        return
    if 'href="app.css"' not in html:
        failures.append("① index.html 未引用 app.css")
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
    if not os.path.exists(INDEX_HTML) or not os.path.exists(APP_JS):
        failures.append("⑤ index.html/app.js 缺失")
        print("[FAIL] ⑤ index.html/app.js 缺失")
        return
    with open(INDEX_HTML, encoding="utf-8") as f:
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

    背景：审计 v1.3 遗留缺口 N1 —— 前端并发请求后到响应覆盖先到（图表
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


def main():
    ap = argparse.ArgumentParser(description="P2-2 ④ 前端 JS 冒烟测试")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    args = ap.parse_args()

    failures = []
    test_html_skeleton(failures)
    test_js_syntax(failures)
    test_window_api_surface(failures)
    test_inline_event_refs(failures)
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

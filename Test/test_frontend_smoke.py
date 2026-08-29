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


def main():
    ap = argparse.ArgumentParser(description="P2-2 ④ 前端 JS 冒烟测试")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    args = ap.parse_args()

    failures = []
    test_html_skeleton(failures)
    test_js_syntax(failures)
    test_window_api_surface(failures)
    test_inline_event_refs(failures)

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

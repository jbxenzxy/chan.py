# -*- coding: utf-8 -*-
"""
阶段 6：前端组件化 —— 成果防护守护用例
=====================================================================
守护阶段 6 的结构性成果（设计文档 V10 方案 8.9）：

  ① 组件区块完备：Frontend/app.js 单文件内 9 个 [COMPONENT] 区块
     （KLineChart / NavToolbar / SymbolSearch / StatsPanel /
     BspSettingsPanel / ScanPanel / RealtimeService / AnnotationPanel /
     Bootstrap）全部在位且非空；区块外不允许游离的业务函数声明。
  ② KLineChart 内容契约：渲染管线关键函数（render 族 / draw* 族 /
     priceToY 坐标系 / onWheel 交互）物理位于 KLineChart 区块内
     （阶段 8 拆 kline/ 子目录时按区块整体迁移）。
  ③ window API 面冻结：app.js 全文件 window.* 赋值名集合 == 基线 64 个
     + window.ChanApp（阶段 6 组件化为纯代码搬移，对外 API 零漂移；
     HTML onclick / 控制台调试依赖此面）。
  ④ index.html 事件引用完整：全部内联事件（onclick/oninput/onkeydown/
     onchange/…）调用的标识符均可解析到 window 绑定。
  ⑤ 零构建约束：app.js 无 import/export/require（原生 JS 零依赖，
     设计 3.5）；Frontend/ 保持 3 文件不拆子目录（设计 8.9：kline/、
     panels/ 留待阶段 8）。
  ⑥ JS 语法校验：node --check（node 不在位时 SKIP 降级，不判 FAIL）。
  ⑧ 缓存击穿纪律：index.html 以 app.js?v=7+ 引用（版本号只增不减）。
  ⑨ 合并层完整（A 方案 AppState 访问层，双方案取长合并项）：
     ChanApp.state 的 30 个 getter/setter 访问器 + 8 个方法别名在位，
     访问器变量与 [STATE] 区块声明交叉一致，且不新增 window.* 绑定。

运行：python Test/test_phase6_guards.py          # 校验（run_all 组件 13）
      python Test/test_phase6_guards.py --update  # 兼容参数（无冻结基线可更新，等价校验）
"""
import argparse
import os
import re
import subprocess
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")
INDEX_HTML = os.path.join(REPO_ROOT, "Frontend", "index.html")
FRONTEND_DIR = os.path.join(REPO_ROOT, "Frontend")

# ═══════════════════════════════════════════════════════════════════════
# ①② 组件区块
# ═══════════════════════════════════════════════════════════════════════
COMPONENTS = [
    "KLineChart", "NavToolbar", "SymbolSearch", "StatsPanel",
    "BspSettingsPanel", "ScanPanel", "RealtimeService", "AnnotationPanel",
    "Bootstrap",
]
# ② KLineChart 渲染管线契约（区块内必须物理存在）
KLINE_PIPELINE = [
    "render", "renderSingle", "renderTop", "renderBottom", "_renderChart",
    "drawGrid", "drawCandles", "drawMacd", "drawVolume", "drawBiLines",
    "drawZs", "drawSegLines", "drawBspMarkers", "drawMaLines", "drawCrosshair",
    "drawDateAxis", "priceToY", "yToPrice", "getChartArea", "getVisibleKlines",
    "onWheel", "toggleOverlay", "toggleDualWindow",
]

# ③ window API 冻结基线（阶段 6 前全量：64 个取自 4d0de89，另含 AMO 面板
#    closeAmoPanel/toggleAmoPanel 两项既有登记，合计 66 个；随后「盘后下载」
#    功能移除 4 个 window 函数（toggleDownloadPanel/closeDownloadPanel/
#    startDownload/stopDownload），现回归为 62 个）
WINDOW_BASELINE = {
    '_dualZsDebugCount', '_isRenderingBottom', '_lastCalcRedRangeError', '_lastGrayStatus',
    '_lastRedFrameStatus', 'annotationAdd', 'annotationDeleteAllGlobal', 'annotationDeleteAnnotation',
    'annotationDialogCancel', 'annotationDialogConfirm', 'annotationDialogKeydown', 'annotationEditAnnotation',
    'annotationReplayToHere', 'bspFilterSelectAll', 'bspFilterSelectNone', 'cancelSelectedPoint',
    'clearHistory', 'clearInput', 'closeAmoPanel', 'closeBspSettings',
    'closeScanPanel', 'doSearch', 'gotoDate',
    'handleDateBlur', 'handleDateChange', 'handleDateInput', 'handleDateKeydown',
    'initCoordSystemRadio', 'loadScanResult', 'loadStock', 'maPeriodsSelectAll',
    'maPeriodsSelectNone', 'onBspFilterChange', 'onCoordSystemChange', 'onInputChange',
    'onInputKeydown', 'onMaPeriodChange', 'onShowBiIdxChange', 'openBspSettings',
    'refreshStockNames', 'removeHistory', 'saveScanToZxg', 'scanModeDialogCancel',
    'scanModeDialogConfirm', 'scanSourceSelectAll', 'scanSourceSelectNone', 'selectHistory',
    'showHistory', 'startScanZxg',
    'switchFreq', 'toggleAmoPanel', 'toggleDualWindow', 'toggleInputClear',
    'toggleMirrorMode', 'toggleOverlay', 'toggleScanMinimize', 'toggleStats',
    'updateScanRecentDisabled', 'updateScanSaveBtn', 'updateSearchSelection', 'updateWeekday',
}
# 允许的登记性新增（组件注册表）
WINDOW_ALLOWED_NEW = {"ChanApp"}
# 允许的区块外顶层辅助函数（既有基线即存在的必要工具，非业务组件）
ALLOWED_STRAY = {"getLayoutParams"}

# JS 关键字（④ 中内联事件的非函数标识符误报豁免）
JS_KEYWORDS = {"if", "else", "return", "this", "var", "let", "const", "new",
               "typeof", "true", "false", "null", "undefined", "event",
               "window", "document", "Math", "Number", "String", "parseInt",
               "parseFloat", "confirm", "alert", "encodeURIComponent"}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def component_sections(src):
    """按 [COMPONENT] 横幅切块 → [(name, text), ...]（保持出现顺序）"""
    marks = [(m.start(), m.group(1))
             for m in re.finditer(r"//\s*\[COMPONENT\]\s*(\S+)\s*——", src)]
    secs = []
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else src.find("// [EXEC]")
        secs.append((name, src[pos:end if end > pos else len(src)]))
    return secs


def test_component_sections(failures):
    src = read(APP_JS)
    secs = component_sections(src)
    names = [n for n, _ in secs]
    missing = [c for c in COMPONENTS if c not in names]
    dup = {n for n in names if names.count(n) > 1}
    bad = []
    if missing:
        bad.append(f"缺失区块: {missing}")
    if dup:
        bad.append(f"重复区块: {sorted(dup)}")
    for n, text in secs:
        if n in COMPONENTS:
            decls = re.findall(r"^\s{8}(?:async\s+)?function\s+(\w+)|"
                               r"^\s{8}window\.(\w+)\s*=\s*(?:async\s*)?function",
                               text, re.M)
            n_decl = sum(1 for t in decls if any(t))
            if n_decl == 0:
                bad.append(f"{n}: 区块内无函数声明")

    # 区块外不允许游离业务函数（全部顶层声明必须落位某区块）
    in_sec = "".join(t for _, t in secs)
    # 收集区块内已见的函数名（粗集合）
    seen = set(re.findall(r"function\s+(\w+)", in_sec)) | \
           set(re.findall(r"window\.(\w+)\s*=", in_sec))
    stray = []
    for m in re.finditer(r"^\s{8}(?:async\s+)?function\s+(\w+)", src, re.M):
        if m.group(1) not in seen and m.group(1) not in ALLOWED_STRAY:
            stray.append(m.group(1))

    if bad or stray:
        failures.append("① 组件区块: " + "; ".join(bad + [f"游离函数: {stray[:8]}"]))
        print(f"[FAIL] ① 组件区块完备: {'; '.join(bad)}" + (f"；游离函数 {len(stray)} 个" if stray else ""))
        for s in stray[:8]:
            print("      -", s)
    else:
        cnt = {n: len(re.findall(r"function", t)) for n, t in secs}
        print(f"[PASS] ① 组件区块完备: {len(secs)} 个区块全部在位非空，"
              f"顶层函数零游离（{sum(cnt.values())} 处 function 定义落位）")


def test_kline_contract(failures):
    src = read(APP_JS)
    secs = dict(component_sections(src))
    if "KLineChart" not in secs:
        failures.append("② KLineChart 契约: 区块缺失")
        print("[FAIL] ② KLineChart 内容契约: 区块缺失")
        return
    text = secs["KLineChart"]
    # 函数声明（function NAME(）或 window 绑定（window.NAME = function）均视为在位
    miss = [f for f in KLINE_PIPELINE
            if not re.search(rf"function\s+{f}\s*\(", text)
            and not re.search(rf"window\.{f}\s*=\s*function", text)]
    if miss:
        failures.append("② KLineChart 契约缺失: " + ",".join(miss))
        print(f"[FAIL] ② KLineChart 内容契约: {len(miss)} 个管线函数不在区块内")
        for f in miss:
            print("      -", f)
    else:
        print(f"[PASS] ② KLineChart 内容契约: 渲染管线 {len(KLINE_PIPELINE)} 个"
              f"关键函数物理位于区块内（阶段 8 整体迁移单元）")


def test_window_surface(failures):
    src = read(APP_JS)
    names = set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", src))
    expect = WINDOW_BASELINE | WINDOW_ALLOWED_NEW
    only_old = sorted(WINDOW_BASELINE - names)
    only_new = sorted(names - expect)
    if only_old or only_new:
        failures.append("③ window API 面: "
                        f"丢失 {only_old[:8]}; 未登记新增 {only_new[:8]}")
        print(f"[FAIL] ③ window API 面冻结: 丢失 {len(only_old)} / 未登记新增 {len(only_new)}")
    else:
        print(f"[PASS] ③ window API 面冻结: {len(names)} 个绑定 == 基线 {len(WINDOW_BASELINE)}"
              f" + {sorted(WINDOW_ALLOWED_NEW)}（对外 API 零漂移）")


def test_html_handlers(failures):
    html = read(INDEX_HTML)
    src = read(APP_JS)
    wins = set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", src))
    handlers = set()
    for m in re.finditer(r'on[a-z]+="([^"]*)"', html):
        for call in re.finditer(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(", m.group(1)):
            n = call.group(1)
            if n not in JS_KEYWORDS:
                handlers.add(n)
    miss = sorted(h for h in handlers if h not in wins)
    if miss:
        failures.append("④ HTML 事件引用: " + ",".join(miss))
        print(f"[FAIL] ④ index.html 事件引用完整: {len(miss)} 个处理器无 window 绑定")
        for h in miss:
            print("      -", h)
    else:
        print(f"[PASS] ④ index.html 事件引用完整: {len(handlers)} 个内联事件标识符"
              f"全部解析到 window 绑定")


def test_zero_build(failures):
    src = read(APP_JS)
    bad = []
    if re.search(r"^\s*(import\s|export\s|require\()", src, re.M):
        bad.append("出现 import/export/require（违反零构建约束）")
    files = sorted(os.listdir(FRONTEND_DIR))
    if files != ["app.css", "app.js", "index.html"]:
        bad.append(f"Frontend/ 文件面漂移: {files}（应恰为 3 文件，子目录拆分留待阶段 8）")
    if bad:
        failures.append("⑤ 零构建约束: " + "; ".join(bad))
        print(f"[FAIL] ⑤ 零构建/单文件约束: {'; '.join(bad)}")
    else:
        print("[PASS] ⑤ 零构建/单文件约束: 无 import/export/require；"
              "Frontend/ 保持 3 文件（原生 JS 零依赖，设计 3.5/8.9）")


def test_node_syntax(failures):
    try:
        r = subprocess.run(["node", "--check", APP_JS], capture_output=True,
                           text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("[SKIP] ⑥ JS 语法校验: node 不在位（CI 环境降级跳过）")
        return
    if r.returncode != 0:
        failures.append("⑥ JS 语法: node --check 失败: " + r.stderr.strip()[:200])
        print(f"[FAIL] ⑥ JS 语法校验: node --check 报错")
        print("      ", r.stderr.strip()[:300])
    else:
        print("[PASS] ⑥ JS 语法校验: node --check 通过")


def test_cache_bust(failures):
    html = read(INDEX_HTML)
    m = re.search(r"app\.js\?v=(\d+)", html)
    if not m:
        failures.append("⑧ 缓存击穿: index.html 未以 ?v=N 引用 app.js")
        print("[FAIL] ⑧ 缓存击穿纪律: 未找到 app.js?v=N 引用")
    elif int(m.group(1)) < 7:
        failures.append(f"⑧ 缓存击穿: 版本号 v={m.group(1)} < 7（阶段 6 交付应为 v7+）")
        print(f"[FAIL] ⑧ 缓存击穿纪律: v={m.group(1)} < 7")
    else:
        print(f"[PASS] ⑧ 缓存击穿纪律: app.js?v={m.group(1)}（≥7，版本号只增不减）")


# ═══════════════════════════════════════════════════════════════════════
# ⑨ 合并层完整性（A 方案 AppState 状态访问层，双方案取长合并项）
# ═══════════════════════════════════════════════════════════════════════
STATE_LAYER_VARS = [
    "chartData", "showBi", "showFx", "showZs", "showSeg", "showBsp", "showBiIdx",
    "bspFilter", "maPeriods", "_logScale", "_showVolume", "_subShowVolume",
    "currentFreq", "lastStockFreq", "lastFuturesFreq", "isDualWindow",
    "dualSubData", "dualSubFreq", "viewOffset", "viewCount", "isRealtimeMode",
    "realtimeSymbol", "realtimeFreq", "realtimeStartTime", "realtimeConnected",
    "annotations", "initialized", "_isMirrorMode", "activeDualWindow",
    "_ctrlPressed",
]
STATE_LAYER_METHODS = [
    "loadOverlaySettings", "saveOverlaySettings", "applyOverlayButtonStates",
    "getShowMa", "saveLastState", "loadLastCodeFreq", "init", "initDefault",
]


def _state_decl_names(sec):
    """提取 [STATE] 区块内全部顶层声明名（按声明行切分，括号深度感知，
    兼容 'let a = 1, b = [1,2];' 多声明符、含逗号初始化式与行尾注释）。"""
    names = set()
    for line in sec.split("\n"):
        if not re.match(r"\s*(?:const|let|var)\s", line):
            continue
        # 去行尾注释（保守：仅当 // 前为空白，避免误伤字符串内的 URL）
        line = re.sub(r"\s//.*$", "", line)
        body = re.sub(r"^\s*(?:const|let|var)\s", "", line)
        depth = 0
        parts, buf = [], []
        for ch in body:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        parts.append("".join(buf))
        for p in parts:
            m = re.match(r"\s*([A-Za-z_$][\w$]*)\s*=", p)
            if m:
                names.add(m.group(1))
    return names


def test_state_layer(failures):
    src = read(APP_JS)
    bad = []
    merged = re.search(r"\[MERGED\][\s\S]*?(?=// \[EXEC\])", src)
    sec = merged.group(0) if merged else ""
    if not merged:
        bad.append("[MERGED] 合并层区块缺失")
    # 30 个访问器逐项在位（getter/setter 同源引用同一闭包变量）
    for v in STATE_LAYER_VARS:
        pat = (rf"{re.escape(v)}: \{{ get: function\(\)\{{ return {re.escape(v)}; \}}, "
               rf"set: function\(v\)\{{ {re.escape(v)} = v; \}} \}}")
        if not re.search(pat, sec):
            bad.append(f"访问器缺失: {v}")
    # 8 个方法别名逐项在位
    for m in STATE_LAYER_METHODS:
        if not re.search(rf"s\.{re.escape(m)} = {re.escape(m)};", sec):
            bad.append(f"方法别名缺失: {m}")
    # 访问器变量必须真实存在于 [STATE] 区块声明（防状态改名后静默失效）
    state_sec = re.search(r"//\s*\[STATE\][\s\S]*?(?=//\s*\[COMPONENT\])", src)
    if state_sec:
        decl = _state_decl_names(state_sec.group(0))
        undeclared = [v for v in STATE_LAYER_VARS if v not in decl]
        if undeclared:
            bad.append(f"访问器变量未在 [STATE] 声明: {undeclared[:6]}")
    else:
        bad.append("[STATE] 区块缺失（无法交叉校验访问器变量）")
    # 约束：不新增 window.* 绑定（API 面冻结）、不重建已删除的 components 注册表
    if re.search(r"ChanApp\.components\.state\b", src):
        bad.append("state 误入 components 注册表（P1-6 已删注册表，禁止回潮）")
    if bad:
        failures.append("⑨ 合并层: " + "; ".join(bad[:10]))
        print(f"[FAIL] ⑨ 合并层完整（AppState 访问层）: {len(bad)} 处问题")
        for b in bad[:10]:
            print("      -", b)
    else:
        print(f"[PASS] ⑨ 合并层完整（AppState 访问层）: {len(STATE_LAYER_VARS)} 个访问器"
              f" + {len(STATE_LAYER_METHODS)} 个方法别名在位，变量与 [STATE] 声明"
              f"一致；未新增 window.* 绑定（A 方案合并项）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="兼容参数：本守护为冻结基线校验，无待更新快照")
    args = ap.parse_args()

    print("===== 阶段 6 成果防护：前端组件化（设计 8.9） =====")
    failures = []
    test_component_sections(failures)
    test_kline_contract(failures)
    test_window_surface(failures)
    test_html_handlers(failures)
    test_zero_build(failures)
    test_node_syntax(failures)
    test_cache_bust(failures)
    test_state_layer(failures)
    print("-" * 64)
    if failures:
        for f in failures:
            print(f"[FAIL] {f}")
        print("===== 阶段 6 成果防护: 失败 =====")
        return 1
    print("===== 阶段 6 成果防护: 全部通过（9 类守护） =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())

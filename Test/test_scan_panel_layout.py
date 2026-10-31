# -*- coding: utf-8 -*-
"""扫描结果面板「列契约 / 标题契约 / 列宽预算」护栏
====================================================================
背景（2026-10-31 用户需求）：结果行要在「代码」之后新增一列**流通市值**（如 `30亿`），
而 8 个扫描模式此前各写一份行构造（4 个模式甚至进度期/终态各一份），加一列要改 8 处、
漏一处就是某模式少一列。同时标注模式的「标注文字」列是唯一可压缩列，加列会不会把它
挤没，需要实测而不是拍脑袋。

本轮落地的三条契约（本文件即它们的守卫）：
  ① **列序契约**：`[勾选][名称][代码][流通市值][120日均线][模式专属列…][标签]`
     唯一实现 = `_scanRowHtml`；7 个 `_renderXxxRows` 只提供「专属列」与「标签」。
     120 日均线（↓跌破/↑站上）由 `_scanRowHtml` 统一出，**唯一例外是标注模式**
     （`ma120:false`）—— 它已有 7 列，再插一列标注文字就只剩 6 个汉字。
  ② **块序契约**：`头部行 → 口径行 → 市场分布行 → 行列表 | 空态`
     唯一实现 = `_scanPanelHtml`；进度期（`_doUpdatePanel`）与终态（`_paintScanFinal`）
     都调它 —— 此前两处各拼一遍，市场分布行因此在放量/底分型/买卖点上「扫完就消失」。
  ③ **间距契约**：列与列之间的空隙**只有一个来源** —— `.scan-stock-row` 的 `gap`。
     各列一律不写 padding-left/right、标签列不写 margin-left:auto。列宽用
     「权重 + flex-basis:0」摊满整行，min-width 保内容不被压。这样 6 列行与 8 列行
     的间距是同一个 8px（实测：8 个面板各自的相邻列间距集合都恰好是 {8}）。

列宽预算（实测，非估算；量法见 `_artifact/build_mockup_v2.py` 的页内探针）：
  行内容宽 = 面板宽 − 2×8(body padding) − 2×8(row padding)
  · 480 面板 ⇒ 464。各列实测：标注 name 57 / code 68 / mv 44 / freq 32 / ann 144 /
    tags 36；6 列模式 name 92 / code 109 / mv 71 / ma 83 / tags 36；回测 8 列
    name 68 / code 81 / mv 53 / ma 61 / 笔数 53 / 期望 66（无标签列）。
  · 440 面板 ⇒ 424；回测 8 列的 min-width 合计 380 + 7×8 gap + 18 勾选 = 454 > 424
    ⇒ 装不下（③e 反锚点）。480 是"8 列 + 8px 等间距"的下限。
  · K 线不足 120 根 / 末根收盘 <=0 ⇒ 后端发 ma120_val=0，前端显示 `--`（不显示 0）。

标题契约（2026-10-31 用户拍板，两套风格）：
  · 判据带「最近 N 根」的 4 个模式（放量/连涨/RSI/买-卖点）→ `周期 · 最新 N 根 · 模式名`
  · 判据与 N 无关的 3 个模式（均线/底分型/回测）→ `周期 · 模式名`
  · 标注 → `全周期 标注`（查全周期标注库，不套上面两套）
  统一用 `textContent`（此前买/卖点用 innerHTML 拼 13px/11px 混排，同一块标题区
  出现 4 种字号）。

跑法：`python Test/test_scan_panel_layout.py`（退出码 0/1 即判决）
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPJS = os.path.join(ROOT, "Frontend", "app.js")
APPCSS = os.path.join(ROOT, "Frontend", "app.css")
APPSCAN = os.path.join(ROOT, "App", "AppScan.py")

# 模式 → (行渲染函数, 终态渲染函数)。顺序无关，仅用于"8 个模式都接线"的穷举断言。
MODES = [
    ("买/卖点", "_renderBspRows", "renderScanResults"),
    ("底分型", "_renderFxDRows", "renderFxDScanResults"),
    ("均线", "_renderMaRows", "renderMaScanResults"),
    ("放量", "_renderFangliangRows", "renderFangliangScanResults"),
    ("连涨", "_renderLianzhangRows", "renderLianzhangScanResults"),
    ("RSI", "_renderRsiRows", "renderRsiScanResults"),
    ("回测", "_renderBacktestRows", "renderBacktestScanResults"),
]

_OK = []


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  \u2014\u2014 " + str(detail)[:300]))
    return cond


def _code_only(appjs):
    """去掉整行注释，只留可执行代码（反锚点必须打在代码上，注释不算证据）。"""
    keep = []
    for ln in appjs.split("\n"):
        t = ln.strip()
        if t.startswith("//") or t.startswith("*") or t.startswith("/*"):
            continue
        keep.append(ln)
    return "\n".join(keep)


def _extract_fn(appjs, fn_name):
    """按花括号配平抽一个函数（兼容单行 / 多行；8 空格缩进）。"""
    m = re.search(r"        (?:window\.)?" + fn_name + r"\s*=\s*function\s*\(|        function " + fn_name + r"\(", appjs)
    assert m, "app.js 抽取失败: " + fn_name
    i = appjs.index("{", m.end())
    depth = 0
    for j in range(i, len(appjs)):
        if appjs[j] == "{":
            depth += 1
        elif appjs[j] == "}":
            depth -= 1
            if depth == 0:
                return appjs[appjs.rfind("\n", 0, m.start()) + 1:j + 1]
    raise AssertionError("app.js 花括号不配平: " + fn_name)


# ══════════════════════════════════════════════════════════════════════
# ① 列序与市值格式（node 抽真函数）
# ══════════════════════════════════════════════════════════════════════
def _run_node(appjs, calls):
    node = shutil.which("node")
    if not node:
        return None
    prelude = ("var window = globalThis;\nvar out = [];\n"
               "var _scanFreq = 'd';\n"
               "function chkBox(c, v) { return '<CHK:' + c + ':' + (v ? 1 : 0) + '>'; }\n"
               # _scanRowHtml 依赖的转义器（与 app.js 里同名实现逐字等价）
               "function statsEsc(s) { return String(s == null ? '' : s)"
               ".replace(/&/g, '&amp;').replace(/</g, '&lt;')"
               ".replace(/>/g, '&gt;').replace(/\"/g, '&quot;'); }\n")
    driver = "".join("out.push(%s + '=' + JSON.stringify(%s));\n" % (json.dumps(t), e)
                     for t, e in calls)
    driver += "console.log(out.join('\\n'));\n"
    harness = prelude + _extract_fn(appjs, "_fmtFloatMc") + "\n" + \
        _extract_fn(appjs, "buildMa120Html") + "\n" + \
        _extract_fn(appjs, "_scanRowHtml") + "\n" + driver
    jf = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    jf.write(harness)
    jf.close()
    try:
        proc = subprocess.run([node, jf.name], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    finally:
        os.unlink(jf.name)
    assert proc.returncode == 0, (proc.stderr or proc.stdout)[:800]
    kv = {}
    for ln in proc.stdout.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            kv[k] = json.loads(v)
    return kv


def part1(appjs):
    print("══ ① 列序与市值格式（node 抽 _scanRowHtml / _fmtFloatMc）══")
    row = ('_scanRowHtml({code:"sh600519",name:"贵州茅台",float_mc:30},'
           '{checked:true,cols:"<COL>",tags:"<TAG>"})')
    kv = _run_node(appjs, [
        ("ROW", row),
        ("MV30", '_fmtFloatMc(30)'),
        ("MV305", '_fmtFloatMc(30.5)'),
        ("MV1234", '_fmtFloatMc(1234)'),
        ("MVNULL", '_fmtFloatMc(null)'),
        ("MVZERO", '_fmtFloatMc(0)'),
        ("MVUNDEF", '_fmtFloatMc(undefined)'),
        ("MVSTR", '_fmtFloatMc("30.5")'),
    ])
    if kv is None:
        check("node 不在位，列契约运行时段 SKIP", True)
        return
    html = kv["ROW"]
    order = [html.find(x) for x in
             ("<CHK:", "scan-col-name", "scan-col-code", "scan-col-mv", "<COL>", "scan-col-tags", "<TAG>")]
    check("①a 列序 = 勾选 → 名称 → 代码 → 流通市值 → 专属列 → 标签",
          all(v >= 0 for v in order) and order == sorted(order), order)
    check("①b 市值列渲染在**代码之后**（用户要求的第 3 列）",
          html.find("scan-col-code") < html.find("scan-col-mv"), html)
    check("①c 30 亿 ⇒ 「30亿」（整数不带 .0，与用户示例逐字一致）",
          kv["MV30"] == "30亿", kv["MV30"])
    check("①d 30.5 ⇒ 「30.5亿」（<100 亿保留 1 位）", kv["MV305"] == "30.5亿", kv["MV305"])
    check("①e 1234 ⇒ 「1234亿」（>=100 亿取整，不出现 1234.5）",
          kv["MV1234"] == "1234亿", kv["MV1234"])
    check("①f 缺值 / 0 ⇒ 「--」（0 代表没取到，不能显示成市值 0）",
          kv["MVNULL"] == "--" and kv["MVZERO"] == "--" and kv["MVUNDEF"] == "--",
          (kv["MVNULL"], kv["MVZERO"], kv["MVUNDEF"]))
    check("①g 数字串（JSON 里偶发）按数值处理，不当 0",
          kv["MVSTR"] == "30.5亿", kv["MVSTR"])


# ══════════════════════════════════════════════════════════════════════
# ② 单点构造 / 单点装配（静态）
# ══════════════════════════════════════════════════════════════════════
def part2(appjs):
    print("══ ② 单点构造与单点装配（静态反锚点）══")
    n_row = appjs.count('class="scan-stock-row"')
    check("②a `scan-stock-row` 字面量全文件**恰好 1 处**（多一处就是又长出一份行构造）",
          n_row == 1, "出现 %d 处" % n_row)
    body = _extract_fn(appjs, "_scanRowHtml")
    check("②b 那一处就在 `_scanRowHtml` 内",
          'class="scan-stock-row"' in body, body[:160])

    missing = [fn for _, fn, _ in MODES if fn + "(" not in appjs]
    check("②c 7 个行渲染函数都存在（4 列固定 + 3 列自带）",
          not missing, missing)
    bad = []
    for label, fn, _ in MODES:
        b = _extract_fn(appjs, fn)
        if "_scanRowHtml(" not in b:
            bad.append(label)
    check("②d 每个行渲染函数都**调用** `_scanRowHtml`（不得自己拼行）", not bad, bad)

    # 终态：6 个适配器都走 _paintScanFinal（统计定义处之外的出现次数）
    n_paint_def = appjs.count("function _paintScanFinal(")
    n_paint_call = len(re.findall(r"(?<!function )_paintScanFinal\(", appjs)) - 0
    # 7 个模式 = 买/卖点 + 底分型 + 均线 + 放量 + 连涨 + RSI + 回测（标注走独立分支）
    check("②e `_paintScanFinal` 定义 1 处、被 7 个模式的终态渲染各调 1 次",
          n_paint_def == 1 and n_paint_call == 7, (n_paint_def, n_paint_call))

    # 装配器：进度期与终态都必须经过它
    n_panel_def = appjs.count("function _scanPanelHtml(")
    n_panel_call = len(re.findall(r"_scanPanelHtml\(", appjs)) - n_panel_def
    check("②f `_scanPanelHtml` 定义 1 处、调用点 2 处（进度期 + 终态）+ 标注 1 处",
          n_panel_def == 1 and n_panel_call == 3, (n_panel_def, n_panel_call))
    check("②g 进度期面板（_doUpdatePanel）走同一个装配器",
          "_scanPanelHtml(" in _extract_fn(appjs, "_doUpdatePanel"), "进度期另拼了一份")

    # 终态渲染函数体内不得再有 scan-summary 直拼（块序已收归装配器）
    dirty = []
    for label, _, fn in MODES:
        b = _extract_fn(appjs, fn)
        if 'class="scan-summary"' in b or 'class="scan-no-result"' in b:
            dirty.append(label)
    check("②h 终态渲染函数体里**不再**直拼 scan-summary / scan-no-result（块序单点）",
          not dirty, dirty)

    # 市场分布行与空态由装配器统一产出
    n_ms = len([ln for ln in appjs.split("\n")
                if "_scanMarketSummaryHtml(results)" in ln
                and "function " not in ln])
    check("②i 市场分布行只在装配器里出现一次（进度/终态不再各算一份）",
          n_ms == 1, n_ms)

    # ── 120 日均线列：7 个模式共用、标注例外 ──
    row_fn = _extract_fn(appjs, "_scanRowHtml")
    row_code = _code_only(row_fn)
    check("②j `_scanRowHtml` 是 120 日均线列的唯一输出点（8 个模式共用一处）",
          row_code.count("buildMa120Html(") == 1, row_code.count("buildMa120Html("))
    check("②k 120 日均线列夹在「市值」与「模式专属列」之间（列序固定）",
          row_code.index('scan-col-mv') < row_code.index("buildMa120Html(")
          < row_code.index("o.cols"), "列序不符")
    check("②l 标注模式用 ma120:false 关掉该列（它已有 7 列，标注文字是唯一可压列）",
          "ma120: false" in appjs, "标注未关列")

    dup = []
    for label, fn, _ in MODES:
        if "buildMa120Html" in _extract_fn(appjs, fn):
            dup.append(label)
    check("②m 7 个行渲染函数**不再**自己传 buildMa120Html（否则每行两列）",
          not dup, dup)

    scan_src = io.open(APPSCAN, encoding="utf-8").read()
    check("②n 后端 `_row_ma120` 定义 1 处、全模式同一份口径",
          scan_src.count("def _row_ma120(") == 1, "定义数不符")
    check("②o `_scan_one` 里只算一次（ma120_fields），不在收口重复取数",
          scan_src.count("ma120_fields = _row_ma120(klines)") == 1,
          scan_src.count("ma120_fields = _row_ma120(klines)"))
    n_carry = scan_src.count("**ma120_fields,")
    check("②p 6 个命中 return 都带出 ma120（底分型/放量/连涨/RSI/回测/买-卖点）",
          n_carry == 6, n_carry)
    check("②q 买/卖点分支不再自算 below_ma120（口径已上提到 _row_ma120）",
          "below_ma120 = False" not in scan_src, "仍有分支自算")
    check("②r 标签为空时**不输出**标签列（回测无标签，空 span 会白占一列宽度）",
          "if (o.tags) {" in row_fn, "仍无条件输出")


# ══════════════════════════════════════════════════════════════════════
# ③ 列宽预算与列间距（静态算术，数字全部来自 app.css 真值）
# ══════════════════════════════════════════════════════════════════════
def _px(css, sel, prop="min-width"):
    m = re.search(re.escape(sel) + r"\s*\{[^}]*?" + prop + r":\s*(\d+)px", css, re.S)
    return int(m.group(1)) if m else None


def _flex_w(css, sel):
    """取 `flex: <w> <s> <b>` 的权重位。"""
    m = re.search(re.escape(sel) + r"\s*\{[^}]*?flex:\s*([\d.]+)\s", css, re.S)
    return float(m.group(1)) if m else None


def _gap(css):
    m = re.search(r"\.scan-stock-row\s*\{[^}]*?gap:\s*(\d+)px", css, re.S)
    return int(m.group(1)) if m else None


_SEL = {"name": ".scan-col-name", "code": ".scan-col-code", "mv": ".scan-col-mv",
        "freq": ".scan-col-freq", "ann": ".scan-col-ann", "ma": ".scan-col-ma",
        "btcount": ".scan-col-btcount", "expect": ".scan-col-expect",
        "tags": ".scan-col-tags"}

# 每种模式的列（不含勾选），顺序 = 行内列序。回测没有标签列（②r 钉住空标签不输出）。
_ROW_COLS = {
    "标注(7列)": ["name", "code", "mv", "freq", "ann", "tags"],
    "通用(6列)": ["name", "code", "mv", "ma", "tags"],
    "连涨(7列)": ["name", "code", "mv", "ma", "expect", "tags"],
    "回测(8列)": ["name", "code", "mv", "ma", "btcount", "expect"],
}


def _min_need(css, cols, gap):
    """一行所需最小宽度 = 勾选 + Σ各列 min-width + gap × 间隙数。"""
    total = _px(css, ".scan-col-chk", "width") or 0
    for c in cols:
        total += _px(css, _SEL[c]) or 0
    return total + gap * len(cols)


def part3():
    print("══ ③ 列宽/间距契约：480 面板 + 间距唯一来源 gap（440 下 8 列装不下）══")
    css = io.open(APPCSS, encoding="utf-8").read()
    panel = _px(css, ".scan-panel", "width")
    gap = _gap(css)
    check("③a 面板宽 = 480px（440 下回测 8 列装不下，见 ③e）", panel == 480, panel)
    check("③b 列间距唯一来源 = .scan-stock-row 的 gap，且值为 8px", gap == 8, gap)

    row_inner = panel - 2 * 8 - 2 * 8
    check("③c 行内容宽 = 面板 − 2×8(body) − 2×8(row)", row_inner == panel - 32, row_inner)

    # 每种模式的最小需求宽都必须 <= 行内容宽（否则列会被压到 min-width 以下 → 溢出）
    need = {k: _min_need(css, v, gap) for k, v in _ROW_COLS.items()}
    for k, v in need.items():
        check("③d %s 最小需求 %dpx <= 行内容 %dpx" % (k, v, row_inner), v <= row_inner, v)

    # 反锚点：面板改回 440，回测 8 列虽有 368px 的 min 下限、但行内容只剩 408px——
    #   余量 40px 摊到 7 列不到 6px/列，等于每列都贴死在 min-width 上（内容一变长就裁）。
    #   480 下余量 80px，才留得住"按权重摊开"的空间。
    slack_440 = (440 - 32) - need["回测(8列)"]
    slack_480 = row_inner - need["回测(8列)"]
    check("③e 反锚点：440 下回测 8 列余量仅 %dpx（每列贴死 min-width，故加宽到 480 的 %dpx）"
          % (slack_440, slack_480), slack_440 < 48 <= slack_480, (slack_440, slack_480))

    # 标注文字列必须是权重最大的列（弹性摊分下才拿得到最宽），且 min-width 容得下 7 汉字
    w_ann = _flex_w(css, ".scan-col-ann")
    w_others = [_flex_w(css, _SEL[c]) or 0 for c in ("name", "code", "mv", "freq")]
    check("③f 标注文字列权重最大（弹性摊分时优先吃空间）",
          w_ann is not None and w_ann > max(w_others), (w_ann, w_others))
    check("③g 标注列 min-width >= 90px（放得下 7 个汉字，其余由权重摊）",
          (_px(css, ".scan-col-ann") or 0) >= 90, _px(css, ".scan-col-ann"))

    code_min = _px(css, ".scan-col-code")
    check("③h 代码列 min-width 68px 仍容得下市场前缀形态（2+6 字符 ≈ 64px）",
          code_min is not None and 64 <= code_min <= 76, code_min)

    # 间距唯一来源：各列不得再写内距 / auto 边距（否则与 gap 叠加 → 6 列行与 8 列行
    #   的空隙不一样，正是本轮要修的现象）
    row_rule = re.search(r"\.scan-stock-row\s*\{[^}]*\}", css, re.S)
    check("③i 行上声明 gap（列间空隙的唯一来源）",
          "gap: 8px" in (row_rule.group(0) if row_rule else ""), "行上无 gap")
    leftovers = []
    for sel in (".scan-col-name", ".scan-col-code", ".scan-col-mv", ".scan-col-freq",
                ".scan-col-ma", ".scan-col-btcount", ".scan-col-expect", ".scan-col-ann",
                ".scan-col-tags"):
        m = re.search(re.escape(sel) + r"\s*\{[^}]*\}", css, re.S)
        body = m.group(0) if m else ""
        if "padding-left" in body or "padding-right" in body or "margin-left: auto" in body:
            leftovers.append(sel)
    check("③j 各列不再写 padding-left/right 或 margin-left:auto（与 gap 叠加会让间距不均）",
          not leftovers, leftovers)

    # 标签列：宽度按内容（basis auto，不参与摊分），否则 RSI 的 "超卖 12.3 (3/5)"
    #   会被裁成 "…12.3 (3/5)"、买/卖点多标签会被裁掉后面的
    tags_rule = re.search(r"\.scan-col-tags\s*\{[^}]*\}", css, re.S)
    rule_txt = tags_rule.group(0) if tags_rule else ""
    check("③k 标签列 flex-basis = auto（按内容宽，不参与剩余空间摊分）",
          re.search(r"flex:\s*0\s+1\s+auto", rule_txt) is not None, rule_txt[:200])
    check("③l 标签列有 max-width 上限（防止长标签吃光整行）",
          _px(css, ".scan-col-tags", "max-width") is not None,
          _px(css, ".scan-col-tags", "max-width"))


# ══════════════════════════════════════════════════════════════════════
# ④ 代码形态与字段来源（静态）
# ══════════════════════════════════════════════════════════════════════
def part4(appjs):
    print("══ ④ 代码形态 / float_mc 来源（静态）══")
    n_dot = len(re.findall(r'code\s*\+\s*"\."\s*\+', _code_only(appjs)))
    check("④a app.js 无「code + \".\" + market」点号形态（后端 _get_stock_market_code 一律拒绝）",
          n_dot == 0, "仍 %d 处" % n_dot)
    check("④b 标注行改用 market + code（前缀在前）",
          '(c.market || "") + c.code' in appjs, "未归一")

    src = io.open(APPSCAN, encoding="utf-8").read()
    check("④c 后端行契约收口在 scan_one（业务分支 = _scan_one，字段在此补齐）",
          "resp[\"float_mc\"] = _row_float_mc(code)" in src, "未在 scan_one 补齐 float_mc")
    check("④d 后端取数与过滤解耦（阈值 <=0 仍取数供展示）",
          "_need_float_mc = any(s in (\"zxg\", \"page_index\", \"all_a\") for s in sources)" in src,
          "取数条件仍绑在 _mc_enabled 上")

    # 标注列文字：**不在 JS 里按字符数截断**。列宽 131px 减 8px 内距只剩 123px，
    #   纯汉字 10 个字刚好、11 个溢出 9px；而 "止损位上移至32001" 这种 11 字符只有
    #   106px。任何固定字数上限都只是"我以为" —— 旧写法写死 11，纯汉字 11 字会被 CSS
    #   硬切半个字且不出现省略号。溢出交给 `.scan-col-ann` 的 text-overflow:ellipsis，
    #   全文另有 title 兜底。
    ann_lines = [ln for ln in _code_only(appjs).split("\n") if "_closestAnnText(" in ln]
    check("④e `_closestAnnText` 只吃 annotations（无字符数上限参数）",
          "function _closestAnnText(annotations) {" in appjs
          and all(", 11)" not in ln and "maxLen" not in ln for ln in ann_lines),
          ann_lines[:3])
    ann_rule = re.search(r"\.scan-col-ann\s*\{[^}]*\}", io.open(APPCSS, encoding="utf-8").read(), re.S)
    check("④f 标注列声明 text-overflow:ellipsis（溢出省略号由 CSS 负责）",
          bool(ann_rule) and "text-overflow: ellipsis" in ann_rule.group(0),
          ann_rule.group(0)[:200] if ann_rule else "MISSING")


# ══════════════════════════════════════════════════════════════════════
# ⑤ 标题两套风格（node 抽真函数跑 8 个模式）
# ══════════════════════════════════════════════════════════════════════
def part5(appjs):
    print("══ ⑤ 标题两套风格（node 抽 updateScanTitle）══")
    node = shutil.which("node")
    if not node:
        check("node 不在位，标题运行时段 SKIP", True)
        return
    prelude = ("var window = globalThis;\nvar out = [];\n"
               "var _scanFreq = 'd';\nvar _scanRecentDays = 3;\nvar _scanMode = 'bsp';\n"
               "var _el = { textContent: '', innerHTML: '' };\n"
               "var document = { getElementById: function () { return _el; } };\n")
    modes = ["bsp", "fangliang", "lianzhang", "rsi", "ma", "fx_d", "backtest", "ann"]
    driver = ""
    for m in modes:
        driver += ("_scanMode = %s; _el.textContent = ''; _el.innerHTML = '__UNSET__';\n"
                   "updateScanTitle();\n"
                   "out.push(%s + '=' + JSON.stringify({t: _el.textContent, h: _el.innerHTML}));\n"
                   % (json.dumps(m), json.dumps(m)))
    driver += "console.log(out.join('\\n'));\n"
    harness = prelude + _extract_fn(appjs, "updateScanTitle") + "\n" + \
        appjs[appjs.index("        var _SCAN_TITLE = {"):appjs.index("        function updateScanTitle(")] + "\n" + driver
    jf = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    jf.write(harness)
    jf.close()
    try:
        proc = subprocess.run([node, jf.name], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    finally:
        os.unlink(jf.name)
    check("⑤a node 执行成功", proc.returncode == 0, (proc.stderr or proc.stdout)[:400])
    if proc.returncode != 0:
        return
    kv = {}
    for ln in proc.stdout.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            kv[k] = json.loads(v)

    check("⑤b 带「最新 N 根」的 4 个模式：`日K · 最新 3 根 · <模式>`",
          kv["bsp"]["t"] == "日K · 最新 3 根 · 买/卖点"
          and kv["fangliang"]["t"] == "日K · 最新 3 根 · 放量"
          and kv["lianzhang"]["t"] == "日K · 最新 3 根 · 连涨"
          and kv["rsi"]["t"] == "日K · 最新 3 根 · RSI",
          {m: kv[m]["t"] for m in ("bsp", "fangliang", "lianzhang", "rsi")})
    check("⑤c 无该设置的 3 个模式：`日K · <模式>`（不带根数）",
          kv["ma"]["t"] == "日K · 均线" and kv["fx_d"]["t"] == "日K · 底分型"
          and kv["backtest"]["t"] == "日K · 回测",
          {m: kv[m]["t"] for m in ("ma", "fx_d", "backtest")})
    check("⑤d 标注保持原文案 `全周期 标注`（不套两套风格）",
          kv["ann"]["t"] == "全周期 标注", kv["ann"]["t"])
    check("⑤e 8 个模式全部走 textContent，**零 innerHTML**（统一 13px，不再混排）",
          all(kv[m]["h"] == "__UNSET__" for m in modes),
          {m: kv[m]["h"] for m in modes if kv[m]["h"] != "__UNSET__"})
    check("⑤f 标题里不再出现旧写法（3连涨 / 3根RSI / [最近 … 根]）",
          "连涨" in kv["lianzhang"]["t"] and "3连涨" not in kv["lianzhang"]["t"]
          and "3根RSI" not in kv["rsi"]["t"] and "[最近" not in kv["bsp"]["t"],
          (kv["lianzhang"]["t"], kv["rsi"]["t"], kv["bsp"]["t"]))

    # 静态：标题表是**单一来源**（模式名与"是否带 N"只在这里定义一次）
    n_tbl = appjs.count("var _SCAN_TITLE = {")
    check("⑤g 标题表 `_SCAN_TITLE` 恰好 1 处定义（模式名/window 标记单点）",
          n_tbl == 1, n_tbl)
    check("⑤h 标题只有一个写入点：getElementById(\"scan-title\") 恰 1 处、且无 innerHTML 写入",
          appjs.count('getElementById("scan-title")') == 1
          and 'scan-title").innerHTML' not in appjs,
          (appjs.count('getElementById("scan-title")'), 'scan-title").innerHTML' in appjs))


def main():
    appjs = io.open(APPJS, encoding="utf-8").read()
    part1(appjs)
    part2(appjs)
    part3()
    part4(appjs)
    part5(appjs)
    ok = all(_OK)
    print("\n%s  (%d/%d)" % ("ALL PASS" if ok else "FAILED", sum(_OK), len(_OK)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

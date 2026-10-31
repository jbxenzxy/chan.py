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
  ④ **行字段契约**：后端 `scan_one` 的响应**原样**进结果数组，前端不得再抄一份
     「字段白名单」。均线模式曾有一份 `normalize`（只有它这么做），加市值列时漏了
     `float_mc`、加 120 日均线列时漏了 `below_ma120/ma120_val` ⇒ 该面板这两列恒为空。
     同一轮后端 ma 分支也漏了 `**ma120_fields`（它不是「命中才 return」形态，被
     「6 个命中 return」的统计漏掉）。两端都已修，本文件 ①j/②p2/②p3 守着。
  ⑤ **市值格式**：列宽只够 4~5 字符、字号 11px ⇒ 一律四舍五入到整数，**不出现
     小数点**（`83.4亿` 的点在小字号下看不见，会被读成 `834亿`，差 10 倍）。
     0 < 市值 < 1 亿显示 `<1亿`（取整成 `0亿` 会被误读为「没取到」），缺值仍 `--`。

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
    harness = prelude + \
        _extract_fn(appjs, "_fmtFloatMc") + "\n" + \
        _extract_fn(appjs, "buildFloatMcHtml") + "\n" + \
        _extract_fn(appjs, "buildMa120Html") + "\n" + \
        _extract_fn(appjs, "_scanRowHtml") + "\n" + \
        _extract_fn(appjs, "_renderMaRows") + "\n" + driver
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
        ("MV834", '_fmtFloatMc(83.4)'),
        ("MV1234", '_fmtFloatMc(1234)'),
        ("MVNULL", '_fmtFloatMc(null)'),
        ("MVZERO", '_fmtFloatMc(0)'),
        ("MVUNDEF", '_fmtFloatMc(undefined)'),
        ("MVSTR", '_fmtFloatMc("30.5")'),
        ("MVSUB1", '_fmtFloatMc(0.4)'),
        # 小盘（< 50 亿）＝ 面板下行青 + ↓ 前缀，与第四列 120 日均线同一套记号
        ("MVH_LOW", 'buildFloatMcHtml(30)'),
        ("MVH_EDGE", 'buildFloatMcHtml(49.4)'),
        ("MVH_50", 'buildFloatMcHtml(50)'),
        ("MVH_BIG", 'buildFloatMcHtml(1234)'),
        ("MVH_NULL", 'buildFloatMcHtml(null)'),
        ("MVH_SUB1", 'buildFloatMcHtml(0.4)'),
        # 均线模式整行：第四列（120 日均线）必须真有内容 —— 这一列曾在均线面板
        #   恒为 `--`（前端 normalize 白名单 + 后端 ma 分支同时漏字段）。
        ("MAROW_DOWN", '_renderMaRows([{code:"sh600000",name:"浦发银行",float_mc:1234.6,'
                       'ma_category:2,below_ma120:true,ma120_val:8.88}])'),
        ("MAROW_UP", '_renderMaRows([{code:"sz000001",name:"平安银行",float_mc:2009.2,'
                     'ma_category:5,below_ma120:false,ma120_val:11.11}])'),
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
    check("①d 30.5 ⇒ 「31亿」（四舍五入，不再保留 1 位小数）",
          kv["MV305"] == "31亿", kv["MV305"])
    check("①d2 83.4 ⇒ 「83亿」（用户举的反例：带点时会被看成 834 亿）",
          kv["MV834"] == "83亿", kv["MV834"])
    check("①e 1234 ⇒ 「1234亿」（>=100 亿同样取整）",
          kv["MV1234"] == "1234亿", kv["MV1234"])
    check("①f 缺值 / 0 ⇒ 「--」（0 代表没取到，不能显示成市值 0）",
          kv["MVNULL"] == "--" and kv["MVZERO"] == "--" and kv["MVUNDEF"] == "--",
          (kv["MVNULL"], kv["MVZERO"], kv["MVUNDEF"]))
    check("①g 数字串（JSON 里偶发）按数值处理，不当 0",
          kv["MVSTR"] == "31亿", kv["MVSTR"])
    check("①h 0 < 市值 < 1 亿 ⇒ 「<1亿」（取整会变 0亿，与缺值混淆）",
          kv["MVSUB1"] == "<1亿", kv["MVSUB1"])
    _mv_all = ["MV30", "MV305", "MV834", "MV1234", "MVSTR", "MVSUB1"]
    check("①i 市值列**任何取值都不含小数点**（本列字号 11px，点看不清 ⇒ 量级误读）",
          not any("." in kv[k] for k in _mv_all),
          [(k, kv[k]) for k in _mv_all if "." in kv[k]])

    md, mu = kv["MAROW_DOWN"], kv["MAROW_UP"]
    check("①j 均线模式行**第四列有内容**：跌破 ⇒ ↓8.88，站上 ⇒ ↑11.11（曾恒为 --）",
          "↓8.88" in md and "↑11.11" in mu, (md, mu))
    check("①k 均线行里 120 日线列夹在市值与标签之间（与其余 6 个模式同列序）",
          md.find("scan-col-mv") < md.find("scan-col-ma") < md.find("scan-col-tags"), md)
    check("①l 均线行市值同样取整（1234.6 ⇒ 1235亿；2009.2 ⇒ 2009亿）",
          "1235亿" in md and "2009亿" in mu, (md[:120], mu[:120]))

    # 小盘（< 50 亿）＝ 下行青 + ↓ 前缀，与第四列 120 日均线同一套记号
    check("①m 30 亿 ⇒ `<span class=\"scan-col-mv low\">↓30亿`（箭头 + low 类，与 ↓120线同构）",
          kv["MVH_LOW"] == '<span class="scan-col-mv low">↓30亿</span>', kv["MVH_LOW"])
    check("①n 边界：49.4 亿（显示 49）算小盘；50 亿整不算；49.6 显示 50亿也不算（避免「↓50亿」自相矛盾）",
          kv["MVH_EDGE"] == '<span class="scan-col-mv low">↓49亿</span>'
          and kv["MVH_50"] == '<span class="scan-col-mv">50亿</span>'
          and kv["MVH_BIG"] == '<span class="scan-col-mv">1234亿</span>',
          (kv["MVH_EDGE"], kv["MVH_50"], kv["MVH_BIG"]))
    check("①o 缺值不加箭头不着色（`--` 没有数，谈不上「低」）；<1 亿仍带箭头 ⇒ `↓<1亿`",
          kv["MVH_NULL"] == '<span class="scan-col-mv">--</span>'
          and kv["MVH_SUB1"] == '<span class="scan-col-mv low">↓<1亿</span>',
          (kv["MVH_NULL"], kv["MVH_SUB1"]))


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
          row_code.index("buildFloatMcHtml(") < row_code.index("buildMa120Html(")
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
    check("②p 7 个会上屏的 return 都带出 ma120（底分型/均线/放量/连涨/RSI/回测/买-卖点）",
          n_carry == 7, n_carry)
    # 均线分支不是「命中才 return」形态，靠计数器容易被漏 → 直接锚住那段代码
    _ma_seg = scan_src[scan_src.index("# ── 均线分类扫描模式 ──"):
                       scan_src.index("# ── 放量扫描模式 ──")]
    check("②p2 均线分类分支的响应体带 ma120 字段（曾漏 ⇒ 面板第四列恒为 --）",
          "**ma120_fields," in _ma_seg, "均线分支未带字段")
    check("②p3 前端不再有 normalize 字段白名单（8 个模式里只有均线加过，连漏两列）",
          appjs.count("normalize:") == 0 and "spec.normalize" not in appjs,
          appjs.count("normalize:"))
    check("②q 买/卖点分支不再自算 below_ma120（口径已上提到 _row_ma120）",
          "below_ma120 = False" not in scan_src, "仍有分支自算")
    check("②r 标签为空时**不输出**标签列（回测无标签，空 span 会白占一列宽度）",
          "if (o.tags) {" in row_fn, "仍无条件输出")
    _bfn = _extract_fn(appjs, "buildFloatMcHtml")
    check("②s 市值列经 `buildFloatMcHtml` 单点构造（类名/箭头不散在行里）",
          "buildFloatMcHtml(r.float_mc)" in row_code
          and row_code.count("_fmtFloatMc(") == 0,
          row_code.count("_fmtFloatMc("))
    check("②t 小盘分界在 `buildFloatMcHtml` 内声明 1 处（无外部全局依赖，抽这个函数即可测）",
          len(re.findall(r"var LOW_YI\s*=\s*50\s*;", _bfn)) == 1 and "LOW_YI" not in appjs.replace(_bfn, ""),
          _bfn[:120])
    check("②u 该常量**不读**后端过滤阈值（显示口径不跟着业务阈值走：用户把阈值调大，"
          "整列不该一起变色）",
          "scan_min_float_mc" not in row_fn and "scan_min_float_mc" not in _bfn,
          "显示路径引用了业务阈值")


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


def _css_code_only(css):
    """去掉 CSS 注释块，只留声明体。

    字重/颜色断言必须打在**真声明**上：本轮写的注释里就带 `.scan-col-ma.down`、
    `#00F0F0` 这些字样，不去注释会把说明文字当规则匹配（铁律：注释不算证据）。
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _norm_color(v):
    """颜色值归一：去所有空白 + 小写。

    CSS 写字面量时按惯例留空格（`rgba(0, 240, 240, 0.6)`），JS 常量不写
    （`rgba(0,240,240,0.6)`）—— 逐字比对前必须先吃掉这个差异，否则「同源」断言
    会因为一个空格假红。
    """
    return re.sub(r"\s+", "", v).lower() if v else v


def _color(css, sel):
    """取**包含该选择器**的规则块里的 color 值（块内可能先有 background，取最后一处）。

    值可能是 hex，也可能是 rgba() —— 面板的下行青与副图 MACD 跌柱同源，就是
    半透明 rgba 形态（不透明版在深底上比红亮 2.7 倍，红青失衡）。
    `(?<![-\\w])` 是为了不把 `background-color:` 抓成 `color:`。

    选择器允许是**逗号列表**：市值小盘色与 120 日均线跌破色共用一条规则
    （`.scan-col-ma.down, .scan-col-mv.low { color: … }`），只认 `sel {` 紧邻
    形态会把这种"单一色源"写法判成没颜色。按逗号切分后**逐项精确比对**，不用
    子串（否则查 `.scan-col-ma` 会命中 `.scan-col-ma.down`，把中性灰判成青）。
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sels = [s.strip() for s in m.group(1).split(",")]
        if sel not in sels:
            continue
        hits = re.findall(r"(?<![-\w])color:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))",
                          m.group(2))
        if hits:
            return _norm_color(hits[-1])
    return None


def _hex_rgb(v):
    """`#rrggbb` → [r,g,b]；非 hex 返回 None（用来跟 COLORS 里的 rgba 比 RGB 位）。"""
    m = re.match(r"#([0-9a-f]{6})$", v or "")
    return [int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)] if m else None


def _rgba_rgb(v):
    """`rgba(r,g,b,a)` → [r,g,b]；非 rgb 形态返回 None。"""
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", v or "")
    return [int(m.group(i)) for i in (1, 2, 3)] if m else None


def _scan_css(css):
    """切出扫描面板那一段样式（`.scan-panel {` … `.amo-panel {`），去注释。

    「面板内不得出现某某色」这类反锚点必须限定在这个区间 —— 全文件 grep 会把
    别的面板（统计 / 量能 / 设置抽屉）算进来，那不是在守本面板的契约。
    """
    c = _css_code_only(css)
    return c[c.index(".scan-panel {"):c.index(".amo-panel {")]


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
    appjs = io.open(APPJS, encoding="utf-8").read()
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

    # 120 日均线列的方向色：跌破（↓）必须与面板里其余"下行"元素同一个青。
    #   此前的 #e94560 是面板**强调红**，同时被周期列（日K）、面板标题、计数标签复用
    #   ⇒ 同一行里 `日K`（中性）与 `↓68.42`（看空）同色，颜色就丧失了区分力。
    #   2026-10-31 再进一步：青的**唯一事实源**是副图 MACD 跌柱 COLORS.macdDown
    #   （`rgba(0,240,240,0.6)`）。不透明的 #00F0F0 在面板底 #16213e 上感知亮度
    #   0.69，是同屏红 #FF3C3C（0.25）的 2.7 倍 —— 看空信号比看多信号亮一倍，红青失衡。
    c_down = _color(css, ".scan-col-ma.down")
    c_sell = _color(css, ".scan-bsp-tag.scan-tag-down")
    c_sell_base = _color(css, ".scan-bsp-tag.sell")
    c_cross = _color(css, ".crosshair-info .down")
    c_freq = _color(css, ".scan-col-freq")
    c_neutral = _color(css, ".scan-col-name")
    m_macd = re.search(r'macdDown:\s*"([^"]+)"', appjs)
    macd_down = _norm_color(m_macd.group(1)) if m_macd else None
    check("③m 跌破均线（↓）用面板内的下行青，与卖点标签 scan-tag-down / sell 同色",
          c_down is not None and c_down == c_sell == c_sell_base,
          (c_down, c_sell, c_sell_base))
    check("③m2 该青与副图 MACD 跌柱 COLORS.macdDown 逐字同源（不是另调一个近似青）",
          macd_down is not None and c_down == macd_down, (c_down, macd_down))
    check("③m3 反锚点：面板下行青不再是**不透明** #00f0f0（它比红亮 2.7 倍）",
          c_down is not None and c_down != "#00f0f0", c_down)
    check("③m4 光标条的跌幅数字同用这一个青（同屏三处，不能漂成两种）",
          c_cross == c_down, (c_cross, c_down))
    # 市值小盘色（< 50 亿）：与 ↓120线用的是**同一条 CSS 规则**（逗号选择器），
    # 而不是各写一遍色值 —— 后者改一处漏一处，就成了两个青。
    c_mvlow = _color(css, ".scan-col-mv.low")
    check("③m5 市值小盘（< 50 亿）与 ↓120线同一个青（两条选择器共用一条 color 规则）",
          c_mvlow is not None and c_mvlow == c_down, (c_mvlow, c_down))
    n_mvlow_rule = len(re.findall(r"\.scan-col-mv\.low\s*[,{]", _css_code_only(css)))
    check("③m6 小盘色只出现 1 条规则（不留第二处色值副本）", n_mvlow_rule == 1, n_mvlow_rule)
    check("③n 反锚点：跌破均线不再用强调红（周期列同色 ⇒ 中性信息与看空信号不分）",
          c_down is not None and c_freq is not None and c_down != c_freq, (c_down, c_freq))
    check("③o 站上均线（↑）仍是中性灰，强调只给 ↓ 一侧",
          c_neutral is not None and _color(css, ".scan-col-ma") == c_neutral,
          (_color(css, ".scan-col-ma"), c_neutral))

    # ── 字重：面板内一律常规（2026-10-31 用户拍板「面板显示中的文字不用粗体，数字也一样」）──
    #   小字号下粗体把笔画糊成一块：9px 标签、11px 汇总数字、13px 标题原先全是 600/700，
    #   面板里唯一"不粗"的只有正文列。层级改由**颜色 + 字号**表达。
    bad_weight = []
    for m in re.finditer(r"([^{}\n]*)\{([^}]*)\}", _css_code_only(css)):
        sel, body = m.group(1).strip(), m.group(2)
        if ".scan-" not in sel:
            continue
        for w in re.findall(r"font-weight:\s*(\d+|bold)", body):
            if w == "bold" or int(w) >= 600:
                bad_weight.append(sel + " → " + w)
    check("③p 扫描面板内无 font-weight >= 600 / bold（含标签、标题、数值列）",
          not bad_weight, bad_weight[:6])

    n_b = len(re.findall(r"<b>", _code_only(appjs)))
    check("③q 汇总行数字改用 <span class=\"scan-num\">，不再用 <b>（<b> 默认 700）",
          n_b == 0 and '"scan-num"' in _code_only(appjs),
          "app.js 仍有 %d 处 <b>" % n_b)
    check("③r .scan-summary 的数字强调只变色不加粗（.scan-num 无 font-weight 声明）",
          _color(css, ".scan-summary .scan-num") is not None
          and re.search(r"\.scan-summary\s+\.scan-num\s*\{[^}]*font-weight",
                        _css_code_only(css)) is None,
          _color(css, ".scan-summary .scan-num"))
    check("③r2 反锚点：`.scan-summary b` 选择器已不存在（<b> 没人用了，留着是死规则）",
          re.search(r"\.scan-summary\s+b\s*[,{]", _css_code_only(css)) is None, "仍存在")

    # ── 面板的红：全部收敛到副图 MACD 涨柱的 RGB（2026-10-31 用户：「面板中的红色
    #   也改为跟 MACD 红柱同样的红色」）──
    #   此前面板里有 4 种红：#e94560（标题/周期/计数，偏粉）、#ff3c3c（买点/涨）、
    #   #ff4444（底分型最强）、#F44336（均线类0）。同一个"红"四种值，改一处必漂。
    #   红**不照抄 macdUp 的 0.6 alpha**：同底上 rgba(255,60,60,.6) 合成对比只有
    #   2.3:1（AA 4.5），13px 标题会糊；取同 RGB 的不透明形态。青能真用 0.6 是因为
    #   #00F0F0 亮度高、合成后仍有 4.84:1 —— 各按自己亮度取 alpha，视觉才拉得平。
    m_up = re.search(r'macdUp:\s*"([^"]+)"', appjs)
    macd_up_rgb = _rgba_rgb(_norm_color(m_up.group(1))) if m_up else None
    red_sels = (".scan-title", ".scan-col-freq", ".scan-summary .scan-num",
                ".scan-bsp-tag.buy", ".scan-bsp-tag.scan-tag-up",
                ".scan-bsp-tag.fx-strongest", ".scan-bsp-tag.ma-cat0")
    red_bad = {}
    for sel in red_sels:
        got = _hex_rgb(_color(css, sel))
        if got != macd_up_rgb:
            red_bad[sel] = _color(css, sel)
    check("③s 面板内的红 = 副图 MACD 涨柱 COLORS.macdUp 的 RGB(255,60,60)",
          macd_up_rgb is not None and not red_bad, (red_bad, macd_up_rgb))
    check("③s2 反锚点：扫描面板内不再出现旧的粉红 #e94560（4 种红 → 1 种）",
          "#e94560" not in _scan_css(css).lower(), "仍有 #e94560")
    check("③s3 反锚点：也不再有 #ff4444 / #f44336 这两种近似红",
          "#ff4444" not in _scan_css(css).lower()
          and "#f44336" not in _scan_css(css).lower(), "仍有旧红")


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

    # 均线列的跌破类名必须与 CSS 选择器同名：JS 写 `warn`、CSS 写 `down` 时颜色会静默失效
    #   （类名不匹配不报错，只是不生效 —— 只能靠这条断言挡住）。`warn` 是旧名，已弃用。
    code_js = _code_only(appjs)
    check("④b2 跌破均线用 `scan-col-ma down`（与 CSS `.scan-col-ma.down` 同名）",
          code_js.count('class="scan-col-ma down"') == 1
          and 'class="scan-col-ma warn"' not in code_js,
          code_js.count('class="scan-col-ma down"'))

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


# ══════════════════════════════════════════════════════════════════════
# ⑥ 后端 ma 分支**运行时**：响应字段 → 前端真函数整行（端到端）
# ══════════════════════════════════════════════════════════════════════
def _synth_klines(n=240, rising=True):
    """合成日线：单调升/降。240 根同时满足均线分类（>=233）与 120 日均线（>=120）。"""
    out = []
    for i in range(n):
        c = round(10.0 + i * 0.05 if rising else 60.0 - i * 0.05, 2)
        out.append({"date": "2025/%02d/%02d" % (1 + i // 28 % 12, 1 + i % 28),
                    "open": round(c - 0.02, 2), "high": round(c + 0.05, 2),
                    "low": round(c - 0.05, 2), "close": c, "vol": 1000 + i,
                    "amount": 100000 + i})
    return out


def part6(appjs):
    print("══ ⑥ 后端 ma 分支运行时（桩喂 K 线跑真 scan_one，再交前端真函数渲染）══")
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from App import AppScan as _sc
    except Exception as e:                                # pragma: no cover
        check("⑥a 运行时段前置：能导入 AppScan", False, repr(e)[:200])
        return

    def _run(klines):
        def _stub(qualified_code, **kw):
            return {"klines": klines, "meta": {"name": "测试股"}, "bsps": [], "bis": []}

        orig = _sc._m.analyze_stock
        _sc._m.analyze_stock = _stub
        try:
            return _sc.scanner.scan_one("300750", freq="d", prefix="0", recent="1",
                                        source="zxg", mode="ma", bsp_types=None)
        finally:
            _sc._m.analyze_stock = orig

    up, down = _synth_klines(rising=True), _synth_klines(rising=False)
    r_up, r_down = _run(up), _run(down)

    check("⑥a 均线模式响应带全 120 日均线字段（曾缺 ⇒ 面板第四列恒为 `--`）",
          isinstance(r_up, dict) and all(k in r_up for k in ("below_ma120", "ma120_val")),
          sorted(r_up) if isinstance(r_up, dict) else r_up)
    check("⑥b 与 `_row_ma120` 逐位相等（后端只此一份口径，不另算）",
          (r_up.get("below_ma120"), r_up.get("ma120_val")) ==
          (_sc._row_ma120(up)["below_ma120"], _sc._row_ma120(up)["ma120_val"])
          and (r_down.get("below_ma120"), r_down.get("ma120_val")) ==
          (_sc._row_ma120(down)["below_ma120"], _sc._row_ma120(down)["ma120_val"]),
          (r_up.get("ma120_val"), _sc._row_ma120(up)["ma120_val"]))
    check("⑥c 方向正确：单调升 ⇒ 站上（False）；单调降 ⇒ 跌破（True）",
          r_up.get("below_ma120") is False and r_down.get("below_ma120") is True
          and r_up.get("ma120_val", 0) > 0 and r_down.get("ma120_val", 0) > 0,
          (r_up.get("below_ma120"), r_down.get("below_ma120")))
    check("⑥d 均线分类同轮有效（升 ⇒ 类0，降 ⇒ 类8）",
          r_up.get("ma_category") == 0 and r_down.get("ma_category") == 8,
          (r_up.get("ma_category"), r_down.get("ma_category")))

    # 后端响应 → 前端真 `_renderMaRows`（不经任何手工补字段）
    kv = _run_node(appjs, [
        ("UP", '_renderMaRows([%s])' % json.dumps(r_up, ensure_ascii=False)),
        ("DOWN", '_renderMaRows([%s])' % json.dumps(r_down, ensure_ascii=False)),
    ])
    if kv is None:
        check("node 不在位，端到端渲染段 SKIP", True)
        return
    m_up = re.search(r'scan-col-ma">↑([\d.]+)<', kv["UP"])
    m_dn = re.search(r'scan-col-ma down">↓([\d.]+)<', kv["DOWN"])
    check("⑥e 整行渲染出 4 列真值：站上 ⇒ `↑<值>`，跌破 ⇒ `↓<值>`（不再出现 `--` 空列）",
          m_up is not None and m_dn is not None, (kv["UP"], kv["DOWN"]))
    check("⑥f 渲染出的均线价位与后端一致（小数点后逐位相同）",
          m_up is not None and m_dn is not None
          and abs(float(m_up.group(1)) - float(r_up["ma120_val"])) < 1e-9
          and abs(float(m_dn.group(1)) - float(r_down["ma120_val"])) < 1e-9,
          (m_up.group(1) if m_up else None, r_up.get("ma120_val")))
    check("⑥g 该行同时带市值与类标签（列不因某模式缺字段而消失）",
          "scan-col-mv" in kv["UP"] and "ma-cat0" in kv["UP"],
          kv["UP"][:200])


def main():
    appjs = io.open(APPJS, encoding="utf-8").read()
    part1(appjs)
    part2(appjs)
    part3()
    part4(appjs)
    part5(appjs)
    part6(appjs)
    ok = all(_OK)
    print("\n%s  (%d/%d)" % ("ALL PASS" if ok else "FAILED", sum(_OK), len(_OK)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

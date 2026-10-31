# -*- coding: utf-8 -*-
"""扫描结果面板「市场分布摘要」护栏（`_scanMarketSummaryHtml`）
====================================================================
背景（2026-10-13 修复的真缺陷，非本轮 RSI 需求引入）：
  该函数 2026-09-29（提交 19115a1）引入时按 `results[i].code.split(".")` 取市场元素
  （假设形态是 `600519.SH`），而全链路 code 契约自始至终是 **market(小写) + code(数字)、
  market 在前、无任何连接符**（后端 `App/AppUtils.py::_get_stock_market_code` 明文规定
  「点号写法一律拒绝」；扫描链路的 market 由 `AppScan.scan_one` 的 `_PREFIX_MAP` 产出，
  取值只有 sh / sz / bj / hk）——⇒ `split(".")` 恒得单元素数组、`parts.length > 1` 恒假、
  4 个计数恒 0、摘要恒为空串。缺陷存活 14 天未被发现，原因是三个既有模式测试**全部打桩**
  `function _scanMarketSummaryHtml(rs) { return ''; }`，真函数行为永不暴露。

修复（本文件 ② 段钉住）：
  · 市场前缀改取 code **前两字符**并 `.toUpperCase()` 后比较；
  · 无匹配市场（ds 指数 / 期货代码 / 无前缀裸数字）时返回**空串**，不留 `margin-top:8px`
    的空 div（空串形态已被三个既有测试的桩跑了很久，是已验证安全的形态）。

四段守护 ——
  ① 真函数行为（node 抽 app.js 真代码）：四市场计数与输出顺序 / 混合计数 /
     港股指数 / 大写输入 / 无匹配前缀（ds / futures / 裸数字）返回空串 / 空数组；
  ② 源码反锚点（**先于任何运行时驱动**，2026-10-12 教训）：函数体内零 `split`、
     必须有 `slice(0, 2)` + `toUpperCase()` + 空态早退；app.js 全文件点号分段零处；
  ③ 跨语言契约对齐（本缺陷的根治护栏）：**真解析**后端 `AppScan.scan_one` 的
     `_PREFIX_MAP`，断言它的每一个取值前端都统计得到；表外前缀（ds）不被统计；
  ④ 六模式接线：fx_d / fangliang / bsp / lianzhang / rsi / backtest 全部调用。

跑法：`python Test/test_scan_market_summary.py`（退出码 0/1 即判决）
"""
import ast
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
APPSCAN = os.path.join(ROOT, "App", "AppScan.py")

_OK = []


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  \u2014\u2014 " + str(detail)[:260]))
    return cond


def _extract_fn(appjs, fn_name):
    """按**花括号配平**抽一个函数（兼容单行 / 多行两种写法）。"""
    m = re.search(r"        function " + fn_name + r"\(", appjs)
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


def _backend_markets():
    """真解析 `AppScan.scan_one` 的 `_PREFIX_MAP`，返回其 values 去重排序集。"""
    src = io.open(APPSCAN, encoding="utf-8").read()
    m = re.search(r"_PREFIX_MAP\s*=\s*(\{[^}]*\})", src)
    assert m, "未找到 AppScan 的 _PREFIX_MAP"
    d = ast.literal_eval(m.group(1))
    assert isinstance(d, dict) and d, "_PREFIX_MAP 解析异常: " + repr(d)
    return sorted(set(d.values())), d


def _run_node(appjs, calls):
    """把真函数抽进 node 跑；calls = [(标签, JS 表达式)]，返回 {标签: 解析后值}。"""
    node = shutil.which("node")
    if not node:
        return None
    prelude = (
        "var window = globalThis;\n"
        "var out = [];\n"
        "function _j(x) { return JSON.stringify(x); }\n"
    )
    driver = "".join(
        "out.push(%s + '=' + _j(%s));\n" % (json.dumps(tag), expr)
        for tag, expr in calls)
    driver += "console.log(out.join('\\n'));\n"
    harness = prelude + _extract_fn(appjs, "_scanMarketSummaryHtml") + "\n" + driver
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


def _call(*codes):
    return "_scanMarketSummaryHtml([" + ",".join('{"code":%s}' % json.dumps(c) for c in codes) + "])"


# ══════════════════════════════════════════════════════════════════════
# ② 源码反锚点（**先于任何运行时驱动** —— 上一轮教训：静态项若排在 node 之后，
#    node 一失败整段就被跳过，反锚点形同虚设）
# ══════════════════════════════════════════════════════════════════════
def part2(appjs):
    print("══ ② 源码反锚点：市场取前两字符，不得按点号分段 ══")
    body = _extract_fn(appjs, "_scanMarketSummaryHtml")

    check("②a 函数体内**零** `split`（点号分段是历史缺陷写法，回潮即红）",
          "split" not in body, "函数体内出现 split")
    check("②b 函数体内取 `slice(0, 2)` 作市场前缀",
          "slice(0, 2)" in body, "未按前两字符取前缀")
    check("②c 函数体内 `.toUpperCase()`（后端 market 是小写，比较串是大写）",
          ".toUpperCase()" in body, "未大写化")
    check("②d 函数体内有空态早退 `marketParts.length === 0`（无匹配市场不留空 div）",
          "marketParts.length === 0" in body, "无空态早退")
    check("②e 四个市场计数变量均在（sh/sz/bj/hk）",
          all(("%sCount" % m) in body for m in ("sh", "sz", "bj", "hk")), body[:200])

    # 全文件：不再有点号分段取市场（防别的函数重蹈覆辙）
    n_dot = len(re.findall(r'code\.split\(["\']\.["\']\)', appjs))
    check("②f app.js 全文件 `code.split(\".\")` **零处**",
          n_dot == 0, "仍有 %d 处点号分段" % n_dot)

    # 定义唯一
    n_def = appjs.count("function _scanMarketSummaryHtml(")
    check("②g app.js 内该函数**恰好 1 处定义**", n_def == 1, "定义 %d 处" % n_def)


# ══════════════════════════════════════════════════════════════════════
# ① 真函数行为（node 抽真代码）
# ══════════════════════════════════════════════════════════════════════
def part1(appjs):
    print("══ ① 真函数行为（node 抽段；node 不在位则 SKIP）══")
    kv = _run_node(appjs, [
        ("FOUR", _call("sh600519", "sz000001", "bj430047", "hk00700")),
        ("MIX", _call("sh600519", "sh601318", "sz300750")),
        ("ONLY_SZ", _call("sz000002")),
        ("HKIDX", _call("hkHSTECH")),
        ("UPPER", _call("SH600519", "SH601318", "SZ000001")),
        ("NOMATCH", _call("ds932000", "futuresRB2510", "600519")),
        ("EMPTY", "_scanMarketSummaryHtml([])"),
    ])
    if kv is None:
        check("node 不在位，前端契约层 SKIP", True)
        return

    # 汇总行数字的形态：`<span class="scan-num">N</span>`（只变色、不加粗）。
    #   2026-10-31 起 `<b>` 全下线 —— 它默认 700，面板里一律常规字重。
    SN = '<span class="scan-num">%d</span>'
    four = kv["FOUR"]
    check("①a 四市场各 1 只 ⇒ 四条摘要齐全",
          all(("上海 " + SN % 1 + " 只" in four, "深圳 " + SN % 1 + " 只" in four,
               "北京 " + SN % 1 + " 只" in four, "香港 " + SN % 1 + " 只" in four)), four)
    # 用 find（而非 index）+ 显式 -1 检查：摘要为空时必须**报红**而不是抛 ValueError
    #   —— 抛异常会让后续断言全被跳过（变异自证时实测到过这个坑）。
    o = [four.find(x) for x in ("上海", "深圳", "北京", "香港")]
    check("①b 输出顺序 = 上海 → 深圳 → 北京 → 香港",
          all(v >= 0 for v in o) and o == sorted(o), four)
    check("①c 外层是 `scan-summary` 且保持既有 margin-top:8px 样式",
          'class="scan-summary"' in four and "margin-top:8px" in four, four)

    mix = kv["MIX"]
    check("①d 混合计数：sh×2 + sz×1 ⇒ 「上海 2 只，深圳 1 只」，且**不含**北京/香港",
          "上海 " + SN % 2 + " 只" in mix and "深圳 " + SN % 1 + " 只" in mix
          and "北京" not in mix and "香港" not in mix, mix)
    check("①e 市场之间用全角逗号「，」连接（与仓库中文文案一致）",
          "，" in mix and "," not in mix, mix)

    check("①f 只深市 ⇒ 仅一条（不输出 0 只的市场）",
          kv["ONLY_SZ"].count('class="scan-num"') == 1
          and "深圳 " + SN % 1 + " 只" in kv["ONLY_SZ"],
          kv["ONLY_SZ"])
    check("①g 港股指数 `hkHSTECH`（market 前缀港 hk）计入香港",
          "香港 " + SN % 1 + " 只" in kv["HKIDX"], kv["HKIDX"])
    check("①h 大写输入 `SH600519` 同样计入（toUpperCase 容错）",
          "上海 " + SN % 2 + " 只" in kv["UPPER"] and "深圳 " + SN % 1 + " 只" in kv["UPPER"],
          kv["UPPER"])

    check("①i 无匹配前缀（ds 指数 / 期货 / 裸数字）⇒ 返回**空串**（不留空 div）",
          kv["NOMATCH"] == "", repr(kv["NOMATCH"]))
    check("①j 空结果数组 ⇒ 返回**空串**", kv["EMPTY"] == "", repr(kv["EMPTY"]))


# ══════════════════════════════════════════════════════════════════════
# ③ 跨语言契约对齐（本缺陷的根治护栏）
# ══════════════════════════════════════════════════════════════════════
def part3(appjs):
    print("══ ③ 跨语言契约：后端 _PREFIX_MAP 的每个取值前端都统计得到 ══")
    markets, raw = _backend_markets()
    print("     后端 _PREFIX_MAP = %s ⇒ market 取值 %s" % (raw, markets))
    check("③a 后端 market 取值集合 = {sh, sz, bj, hk} 四值（前端只认这四个）",
          markets == ["bj", "hk", "sh", "sz"], markets)

    # 用后端**真实取值**构造 code（market + 数字，无连接符）喂真函数
    codes = [m + "000001" for m in markets]
    kv = _run_node(appjs, [("REAL", _call(*codes))])
    if kv is None:
        check("node 不在位，跨语言契约层 SKIP", True)
        return
    html = kv["REAL"]
    SN = '<span class="scan-num">%d</span>'
    check("③b 后端每个 market 取值构造的 code 都能被前端统计（4 个市场各 1 只）",
          all(("上海 " + SN % 1 + " 只" in html, "深圳 " + SN % 1 + " 只" in html,
               "北京 " + SN % 1 + " 只" in html, "香港 " + SN % 1 + " 只" in html)), html)

    # 反向：不在 _PREFIX_MAP 里的前缀不得被误统计
    kv2 = _run_node(appjs, [("EXTRA", _call("ds932000", "fu000001", "zz000001"))])
    check("③c 表外前缀（ds / fu / zz）**不被**任何市场统计 ⇒ 空串",
          kv2["EXTRA"] == "", repr(kv2["EXTRA"]))

    # 后端 code 拼接方式：market 在前（拼串而非点号）
    src = io.open(APPSCAN, encoding="utf-8").read()
    m = re.search(r"qualified_code\s*=\s*\(?\s*market_prefix\s*\+\s*code", src)
    check("③d 后端 code = `market_prefix + code`（market 在前、无连接符）",
          bool(m), "未找到 market_prefix + code 形式")
    check("③e 后端 market 经 `.lower()` 归一（与前端 toUpperCase 对偶）",
          "market_prefix.lower()" in src, "未找到 lower() 归一")


# ══════════════════════════════════════════════════════════════════════
# ④ 六模式接线
# ══════════════════════════════════════════════════════════════════════
def part4(appjs):
    print("══ ④ 接线：8 个模式都拿到市场摘要（装配器单点）══")
    # 2026-10-31 重构后：市场分布行由**装配器** `_scanPanelHtml` 统一产出，
    # 各模式不再各调一次 —— 原先 6 处调用点（外加 3 个内联 renderRows 首行）
    # 正是"进度期/终态各拼一份"的温床，放量/底分型/买卖点的市场分布行因此
    # 扫完就消失。现在只要保证：①摘要只在装配器里出现一次；②所有模式都经过装配器。
    n_call = appjs.count("_scanMarketSummaryHtml(") - appjs.count(
        "function _scanMarketSummaryHtml(")
    check("④a 调用点恰好 1 处（唯一实现在装配器 _scanPanelHtml 内）",
          n_call == 1, "实测 %d 处" % n_call)

    panel = _extract_fn(appjs, "_scanPanelHtml")
    check("④b 装配器内调用一次市场摘要，且夹在口径行与行列表之间（块序单点）",
          panel.count("_scanMarketSummaryHtml(results)") == 1
          and panel.index("_scanMarketSummaryHtml(results)") < panel.index('class="scan-no-result"'),
          panel[:220])

    # 各模式的行函数**不再**自己挂摘要（否则又回到两份副本）
    for fn in ("_renderLianzhangRows", "_renderRsiRows", "_renderBacktestRows",
               "_renderBspRows", "_renderFxDRows", "_renderMaRows", "_renderFangliangRows"):
        body = _extract_fn(appjs, fn)
        check("④c `%s` 内不得再挂市场摘要（块序归装配器）" % fn,
              "_scanMarketSummaryHtml" not in body, "仍自行挂摘要")

    # 所有模式的终态都经 _paintScanFinal → _scanPanelHtml；进度期经 _doUpdatePanel → _scanPanelHtml
    n_paint = len([ln for ln in appjs.split("\n")
                   if "_paintScanFinal(" in ln and "function " not in ln])
    check("④d 7 个模式的终态渲染都经 `_paintScanFinal`", n_paint == 7, n_paint)
    upd = _extract_fn(appjs, "_doUpdatePanel")
    check("④e 进度期面板经同一个装配器（`_doUpdatePanel` 内调 `_scanPanelHtml`）",
          "_scanPanelHtml(" in upd, "进度期另拼了一份")
    check("④f 标注模式也经装配器（三处调用点：装配器自身 + 进度期 + 终态 + 标注）",
          appjs.count("_scanPanelHtml(") - appjs.count("function _scanPanelHtml(") == 3,
          appjs.count("_scanPanelHtml("))


def main():
    print("=" * 68)
    print("扫描结果面板「市场分布摘要」护栏（_scanMarketSummaryHtml）")
    print("=" * 68)
    appjs = io.open(APPJS, encoding="utf-8").read()
    part2(appjs)      # 静态段**必须**先跑（不依赖 node，且要能独立判死）
    part1(appjs)
    part3(appjs)
    part4(appjs)
    print("-" * 68)
    total, passed = len(_OK), sum(_OK)
    print("合计 %d 项，通过 %d，失败 %d" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

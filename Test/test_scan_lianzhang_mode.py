# -*- coding: utf-8 -*-
"""「连涨」扫描模式（scan mode = "lianzhang"）护栏
====================================================================
「股票扫描」新增第 7 个模式：连涨 —— 在选定来源内，找出**最近 N 根 K 线逐根收红**
的标的（N 取弹窗里的「最近N根」，默认 3）。

  ① 后端判据（`AppScan.Scanner.scan_one(mode="lianzhang")`）：收红的口径必须与
     **K 线图上的红色**同源 —— `close > open`（**严格**不等；`close == open`
     画白色十字线，既非红也非绿 ⇒ 不算）。见 `Frontend/app.js` 的 `drawCandles()`。
     ⚠️ 它**不是**通达信 `UPNDAY`（连涨 = 逐根高于**前一根收盘**）—— 两者会选出
     不同的票（跳空低开仍可能收红），这条是本次需求澄清的结论，写成断言防回潮。
     同处钉住：只看**最后** N 根 / K 线不足 N 根不算。
  ①+ 涨幅口径 = K 线图底部**十字白框**（悬停窗口首根时的读数「N 根 +涨跌(涨幅%)」）：
     基期 = 窗口首根的**前一根收盘**（无前根时回落该根开盘，同白框的 `centerIdx > 0 ?
     prev.close : k.open`），终点 = 末根收盘 ⇒ `(末根收盘 − 首根前收) / 首根前收`，
     **不是**首根开盘；代数上 = N 根单根涨幅（同为「收盘/前收」）的复利连乘。
     **可为负**：三根连红但整体跳空下跌是可能的。
  ② 真实冻结切片：具体窗口的日期与涨幅期望 + 120 组截断点×N 的一致性不变量
     （证明模块判定 == 同口径朴素复算，且该批次非空转）。
  ③ 前端真函数层（node 抽 app.js 真代码）：置灰契约（连涨用「最近N根」⇒ 可编辑，
     来源与周期也可用）、切到连涨时 N<2 自动填 3、按区间涨幅降序、「N连涨」标签、
     涨红跌绿、终态摘要与口径披露、空态提示。
  ④ 静态结构契约：HTML 单选项位置（**紧跟在「放量」之后**）+ onchange + 选项总数、
     资源版本号、localStorage 白名单、提交 spec、后端分支与判据源码。

样本：合成 K 线（手算期望，覆盖平盘 / 阴线 / 不足 / 跳空 / 零价）+ 真实冻结切片
`Test/fixtures_real/sz002190_d.json`（1393 根日线，与 bt01 / 回测扫描同一批）。

跑法：`python Test/test_scan_lianzhang_mode.py`（退出码 0/1 即判决）
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
sys.path.insert(0, ROOT)

from App import AppScan as _sc                       # noqa: E402

FIXTURES = os.path.join(ROOT, "Test", "fixtures_real")
APPJS = os.path.join(ROOT, "Frontend", "app.js")
APPHTML = os.path.join(ROOT, "Frontend", "app.html")

_OK = []


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  \u2014\u2014 " + str(detail)[:260]))
    return cond


# ── 桩驱动：把 analyze_stock 换成本地 K 线（不改被测逻辑）────────────
#   与 test_scan_backtest_mode.py 同一手法：scan_one 消费的就是
#   result["klines"] / result["meta"]，桩喂的数据与真路径逐字段同形。
def _run(klines, mode="lianzhang", recent="3", name="\u6d4b\u8bd5\u80a1",
         code="002190", prefix="0", freq="d"):
    def _stub(qualified_code, **kw):
        return {"klines": klines, "meta": {"name": name}, "bsps": [], "bis": []}

    orig = _sc._m.analyze_stock
    _sc._m.analyze_stock = _stub
    try:
        return _sc.scanner.scan_one(code, freq=freq, prefix=prefix,
                                    recent=str(recent), source="zxg", mode=mode)
    finally:
        _sc._m.analyze_stock = orig


def _series(rows):
    """rows = [(date, open, close), ...] -> klines（high/low 取开收极值，够用）"""
    return [{"date": d, "open": o, "close": c, "high": max(o, c),
             "low": min(o, c), "vol": 1, "amount": 1e6} for (d, o, c) in rows]


# ══════════════════════════════════════════════════════════════════════
# ① 后端判据（合成 K 线，手算期望）
# ══════════════════════════════════════════════════════════════════════
def part1():
    print("══ ① 后端判据：最近 N 根逐根收红（口径 = K 线红色 close > open）══")

    # A 三根全红 ⇒ 命中，字段齐全
    #   前置一根阴线（前收 9.50），使两种涨幅基期给出**不同**的数：
    #     白框口径 (11.0-9.50)/9.50 = 15.79%  vs  首根开盘口径 (11.0-10.0)/10.0 = 10.00%
    row = _run(_series([("2026/08/31", 9.60, 9.50),
                        ("2026/09/01", 10.0, 10.2),
                        ("2026/09/02", 10.1, 10.5),
                        ("2026/09/03", 10.4, 11.0)]), recent="3")
    check("① 三根全红 ⇒ 命中", row.get("is_lianzhang") is True, row)
    check("① 命中行补市场前缀 + 名称/周期透传 + 无 error",
          row.get("code") == "sz002190" and row.get("name") == "测试股"
          and row.get("freq") == "d" and "error" not in row, row)
    check("① recent_days 回显为 3（前端据此渲染「N连涨」标签）",
          row.get("recent_days") == 3, row.get("recent_days"))
    check("① 涨幅 = 首根**前收** → 末根收盘（(11.0-9.50)/9.50 = 15.79%）"
          "—— 与 K 线图十字白框同口径",
          row.get("gain_pct") == 15.79, row.get("gain_pct"))
    check("① 判别力：涨幅**不是**旧口径「首根开盘 → 末根收盘」的 10.0%",
          row.get("gain_pct") != 10.0, row.get("gain_pct"))
    check("① 涨幅恒等于三根单根涨幅（各以前收为基）的复利连乘 "
          "1.0737*1.0294*1.0476 - 1 = 15.79%",
          row.get("gain_pct") == round(
              ((10.2 / 9.50) * (10.5 / 10.2) * (11.0 / 10.5) - 1) * 100, 2),
          row.get("gain_pct"))
    check("① 区间日期透传（首根 / 末根的 date）",
          row.get("date_from") == "2026/09/01"
          and row.get("date_to") == "2026/09/03",
          (row.get("date_from"), row.get("date_to")))

    # B 中间一根平盘：close == open（图上画白色十字线）⇒ 不算红
    r_b = _run(_series([("d1", 10.0, 10.2), ("d2", 10.5, 10.5),
                        ("d3", 10.5, 11.0)]), recent="3")
    check("① 中间一根平盘（close == open ⇒ 白色十字线）不算红 ⇒ 未命中",
          r_b.get("is_lianzhang") is False and "error" not in r_b, r_b)
    check("① 未命中行的 code 是**裸代码**（不补前缀，与其它模式同形）",
          r_b.get("code") == "002190", r_b.get("code"))

    # C 阴线
    r_c = _run(_series([("d1", 10.0, 10.2), ("d2", 10.5, 10.1),
                        ("d3", 10.1, 11.0)]), recent="3")
    check("① 任一根收阴 ⇒ 未命中", r_c.get("is_lianzhang") is False, r_c)

    # D 数据不足
    r_d = _run(_series([("d1", 10.0, 10.2), ("d2", 10.1, 10.5)]), recent="3")
    check("① K 线不足 N 根 ⇒ 未命中且不抛异常",
          r_d.get("is_lianzhang") is False and "error" not in r_d, r_d)

    # E N=1 边界
    check("① N=1 单根收红 ⇒ 命中",
          _run(_series([("d1", 10.0, 10.2)]), recent="1").get("is_lianzhang") is True)
    check("① N=1 单根收阴 ⇒ 未命中",
          _run(_series([("d1", 10.2, 10.0)]), recent="1").get("is_lianzhang") is False)
    check("① N=1 且**无前根** ⇒ 基期回落该根开盘（白框同款兜底），涨幅 = 2.0%",
          _run(_series([("d1", 10.0, 10.2)]), recent="1").get("gain_pct") == 2.0)
    r_e3 = _run(_series([("d0", 9.0, 10.0), ("d1", 10.5, 11.0)]), recent="1")
    check("① N=1 且**有前根** ⇒ 基期 = 前收（(11.0-10.0)/10.0 = 10.0%）；"
          "对照：首根开盘口径只会给 4.76%，两者不同 ⇒ 该断言有判别力",
          r_e3.get("gain_pct") == 10.0, r_e3.get("gain_pct"))

    # F 只看最后 N 根
    r_f = _run(_series([("d0", 20.0, 19.0), ("d1", 10.0, 10.2),
                        ("d2", 10.1, 10.5), ("d3", 10.4, 11.0)]), recent="3")
    check("① 只看**最后** N 根：更早的阴线不影响（4 根取后 3 根）",
          r_f.get("is_lianzhang") is True, r_f)

    # G 判别力：三根连红但整体跳空下跌 ⇒ 仍命中，且涨幅为负
    #   前置阳线（前收 10.50）⇒ 白框口径 -11.43%（若误用首根开盘只会得 -7.0%）
    r_g = _run(_series([("d0", 10.20, 10.50),
                        ("d1", 10.00, 10.10), ("d2", 9.50, 9.60),
                        ("d3", 9.20, 9.30)]), recent="3")
    check("① 三根连红但整体跳空下跌 ⇒ **仍命中**，涨幅为负"
          "（连涨 ≠ 区间上涨；证明判据不是「涨幅 > 0」的子集）",
          r_g.get("is_lianzhang") is True and r_g.get("gain_pct") == -11.43, r_g)

    # H 零价保护：基期（前收）= 0 ⇒ 涨幅回落 0.0，不抛 ZeroDivisionError
    #   （与白框的 `startPrice !== 0 ? ... : "0.00"` 同款保护）
    r_h = _run(_series([("d0", 1.0, 0.0), ("d1", 10.0, 10.2),
                        ("d2", 10.1, 10.5), ("d3", 10.4, 11.0)]), recent="3")
    check("① 基期前收 = 0（脏数据）⇒ 收红判定照常，涨幅回落 0.0、不抛异常",
          r_h.get("is_lianzhang") is True and r_h.get("gain_pct") == 0.0
          and "error" not in r_h, r_h)


# ══════════════════════════════════════════════════════════════════════
# ② 真实冻结切片
# ══════════════════════════════════════════════════════════════════════
def _page_klines(fixture_name):
    """冻结切片（`dt` 带时分秒）→ 页面 `chartData.klines` 格式（斜杠日期）。"""
    recs = json.load(io.open(os.path.join(FIXTURES, fixture_name), encoding="utf-8"))
    out = []
    for r in recs:
        dt = r["dt"]
        out.append({"date": "%s/%s/%s" % (dt[:4], dt[5:7], dt[8:10]),
                    "open": r["open"], "high": r["high"], "low": r["low"],
                    "close": r["close"], "vol": r["vol"], "amount": r["amount"]})
    return out


def part2():
    print("══ ② 真实冻结切片：具体窗口期望 + 一致性不变量 ══")
    kl = _page_klines("sz002190_d.json")
    check("② 切片规模 1393 根（与 bt01 / 回测扫描同一批）", len(kl) == 1393, len(kl))

    row = _run(kl[:1379], recent="3")
    check("② L=1379 / N=3 ⇒ 命中（区间 2026/09/07 ~ 2026/09/09）",
          row.get("is_lianzhang") is True
          and row.get("date_from") == "2026/09/07"
          and row.get("date_to") == "2026/09/09", row)
    check("② 该窗口涨幅 = 2.68%（首根前收 25.77 = 2026/09/04 收盘 → 末根收盘 26.46）；"
          "对照：首根开盘口径给 2.16%，两者不同 ⇒ 判别力自证",
          row.get("gain_pct") == 2.68, row.get("gain_pct"))

    row5 = _run(kl[:1347], recent="5")
    check("② L=1347 / N=5 ⇒ 命中，涨幅 10.08%（首根前收 21.13 = 2026/07/20 收盘 "
          "→ 末根收盘 23.26）；对照：首根开盘口径给 11.19%",
          row5.get("is_lianzhang") is True and row5.get("gain_pct") == 10.08, row5)

    r_miss = _run(kl, recent="3")
    check("② L=1393 / N=3（末 3 根非全红）⇒ 未命中",
          r_miss.get("is_lianzhang") is False, r_miss)

    # 不变量：任意截断点 × 任意 N，模块判定 == 独立复算（同口径朴素实现）
    bad = []
    bad_gain = []
    hits = 0
    probes = 0
    for L in range(1370, 1394):
        for N in range(1, 6):
            probes += 1
            row_l = _run(kl[:L], recent=str(N))
            got = row_l.get("is_lianzhang")
            seg = kl[L - N:L]
            want = len(seg) == N and all(k["close"] > k["open"] for k in seg)
            if got is not want:
                bad.append((L, N, got, want))
            if got:
                hits += 1
                # 涨幅：按「白框口径」独立复算（基期 = 窗口首根的前收；无前根时回落
                #   首根开盘）逐组比对 —— 只对命中项有意义。
                i0 = L - N
                base = kl[i0 - 1]["close"] if i0 >= 1 else kl[i0]["open"]
                exp = round((seg[-1]["close"] - base) / base * 100, 2) if base else 0.0
                if row_l.get("gain_pct") != exp:
                    bad_gain.append((L, N, row_l.get("gain_pct"), exp))
    check("② %d 组（24 截断点 × N=1..5）判定与独立复算**逐组一致**"
          % probes, not bad, bad[:5])
    check("② 同批 %d 个命中项的**涨幅**也与白框口径独立复算逐组一致" % hits,
          not bad_gain, bad_gain[:5])
    check("② 该批次非空转（命中 > 0，判别力自证）", hits > 0, hits)


# ══════════════════════════════════════════════════════════════════════
# ③ 前端真函数（node 抽段）
# ══════════════════════════════════════════════════════════════════════
def _extract_any_fn(appjs, fn_name):
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


_ROWS = [
    # 故意打乱顺序 + 混入 0，验证「按区间涨幅降序」真的发生（不是顺序透传）
    {"code": "sz000003", "name": "C\u80a1", "recent_days": 3, "gain_pct": -1.5},
    {"code": "sz000001", "name": "A\u80a1", "recent_days": 3, "gain_pct": 6.25},
    {"code": "sz000004", "name": "D\u80a1", "recent_days": 3, "gain_pct": 0.0},
    {"code": "sz000002", "name": "B\u80a1", "recent_days": 3, "gain_pct": 2.5},
]


def part3():
    print("══ ③ 前端真函数（node 抽段；node 不在位则 SKIP）══")
    node = shutil.which("node")
    if not node:
        check("node 不在位，前端契约层 SKIP", True)
        return

    appjs = io.open(APPJS, encoding="utf-8").read()

    prelude = (
        "var window = globalThis;\n"
        "var _scanSources = ['zxg'];\n"
        "var _scanFreq = 'd';\n"
        "var _scanRecentDays = 3;\n"
        "function chkBox(code, checked) { return '<chk:' + code + ':' + (checked ? '1' : '0') + '>'; }\n"
        "function _scanMarketSummaryHtml(rs) { return ''; }\n"
        "function updateScanSaveBtn() {}\n"
        "var DOM = { 'scan-body': { innerHTML: '' } };\n"
        "var _els = {\n"
        "  'scan-recent-row': { style: {} },\n"
        "  'scan-recent-days': { disabled: null, value: '1' },\n"
        "  'scan-freq-row': { style: {} },\n"
        "  'scan-source-section': { style: {} } };\n"
        "var _modeChecked = null;\n"
        "var document = {\n"
        "  getElementById: function (id) { return _els[id] || DOM[id] || null; },\n"
        "  querySelector: function () { return _modeChecked; }\n"
        "};\n"
        "var out = [];\n"
    )

    driver = (
        # ── ③a 置灰契约 + 自动填 3 ──
        "function _probeMode(v) {\n"
        "  _modeChecked = { value: v };\n"
        "  _els['scan-recent-row'].style = {};\n"
        "  _els['scan-recent-days'].disabled = null;\n"
        "  _els['scan-recent-days'].value = '1';\n"
        "  _els['scan-freq-row'].style = {};\n"
        "  _els['scan-source-section'].style = {};\n"
        "  updateScanRecentDisabled();\n"
        "  return JSON.stringify({\n"
        "    recent: _els['scan-recent-row'].style.opacity,\n"
        "    recentDis: _els['scan-recent-days'].disabled,\n"
        "    val: String(_els['scan-recent-days'].value),\n"
        "    freq: _els['scan-freq-row'].style.opacity,\n"
        "    src: _els['scan-source-section'].style.opacity });\n"
        "}\n"
        "out.push('M_lz=' + _probeMode('lianzhang'));\n"
        "out.push('M_bsp=' + _probeMode('bsp'));\n"
        "out.push('M_ann=' + _probeMode('ann'));\n"
        "out.push('M_bt=' + _probeMode('backtest'));\n"
        # ── ③b 排序 / 标签 / 配色 / 勾选 ──
        "var rows = " + json.dumps(_ROWS, ensure_ascii=False) + ";\n"
        "var html = _renderLianzhangRows(rows);\n"
        "function _idx(s) { return html.indexOf(s); }\n"
        "out.push('ORDER=' + JSON.stringify({\n"
        "  ok: _idx('A\u80a1') < _idx('B\u80a1') && _idx('B\u80a1') < _idx('D\u80a1')\n"
        "      && _idx('D\u80a1') < _idx('C\u80a1') }));\n"
        "function _rowHtml(code) { var i = html.indexOf('loadScanResult(\\'' + code);\n"
        "  return html.slice(i, html.indexOf('</div>', i)); }\n"
        "out.push('TAG=' + JSON.stringify({\n"
        "  lz: _rowHtml('sz000001').indexOf('3\u8fde\u6da8') >= 0,\n"
        "  cls: _rowHtml('sz000001').indexOf('fl-rise') >= 0 }));\n"
        "out.push('COLOR=' + JSON.stringify({\n"
        "  pos: _rowHtml('sz000001').indexOf('#FF3C3C') >= 0,\n"
        "  neg: _rowHtml('sz000003').indexOf('#00F0F0') >= 0,\n"
        "  zero: _rowHtml('sz000004').indexOf('#FF3C3C') >= 0,\n"
        "  posTxt: _rowHtml('sz000001').indexOf('6.25%') >= 0,\n"
        "  negTxt: _rowHtml('sz000003').indexOf('-1.50%') >= 0,\n"
        "  zeroTxt: _rowHtml('sz000004').indexOf('0.00%') >= 0 }));\n"
        "out.push('CHK=' + JSON.stringify({\n"
        "  all: _idx('<chk:sz000001:1>') >= 0 && _idx('<chk:sz000003:1>') >= 0 }));\n"
        # ── ③c 终态渲染：摘要 / 口径披露 / 空态 ──
        "_scanRecentDays = 3;\n"
        "document.getElementById('scan-body').innerHTML = '';\n"
        "renderLianzhangScanResults(rows.slice(), 100, 7, false);\n"
        "var F = document.getElementById('scan-body').innerHTML;\n"
        "out.push('FIN=' + JSON.stringify({\n"
        "  cnt: F.indexOf('\u8fde\u6da8 <b>4</b> \u53ea') >= 0,\n"
        "  skip: F.indexOf('\u8df3\u8fc7 <b>7</b> \u53ea') >= 0,\n"
        "  cal: F.indexOf('\u9010\u6839\u6536\u7ea2\uff08\u6536\u76d8 > \u5f00\u76d8\uff0c\u5e73\u76d8\u4e0d\u7b97\uff09') >= 0,\n"
        "  gain: F.indexOf('\u6da8\u5e45 = \u9996\u6839\u7684\u524d\u4e00\u6839\u6536\u76d8"
        " \u2192 \u672b\u6839\u6536\u76d8\uff08\u540c K \u7ebf\u56fe\u5e95\u90e8\u767d\u6846"
        "\u8bfb\u6570\uff0c\u975e\u9996\u6839\u5f00\u76d8\uff09') >= 0,\n"
        "  gainOld: F.indexOf('\u9996\u6839\u5f00\u76d8 \u2192 \u672b\u6839\u6536\u76d8') < 0\n"
        "           && F.indexOf('\u6da8\u5e45 = \u9996\u6839\u524d\u6536 ') < 0,\n"
        "  rev: F.indexOf('\u6700\u8fd1 3 \u6839') >= 0 }));\n"
        "document.getElementById('scan-body').innerHTML = '';\n"
        "renderLianzhangScanResults([], 100, 100, false);\n"
        "out.push('EMPTY=' + JSON.stringify({\n"
        "  msg: document.getElementById('scan-body').innerHTML.indexOf('\u672a\u53d1\u73b0\u8fde\u6da8\u6807\u7684') >= 0 }));\n"
        "console.log(out.join('\\n'));\n"
    )

    harness = (prelude
               + _extract_any_fn(appjs, "updateScanRecentDisabled") + "\n"
               + _extract_any_fn(appjs, "_scanSourceLabel") + "\n"
               + _extract_any_fn(appjs, "_btCol") + "\n"
               + _extract_any_fn(appjs, "_btPct") + "\n"
               + _extract_any_fn(appjs, "buildLianzhangTagHtml") + "\n"
               + _extract_any_fn(appjs, "_renderLianzhangRows") + "\n"
               + _extract_any_fn(appjs, "renderLianzhangScanResults") + "\n"
               + driver)

    jf = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    jf.write(harness)
    jf.close()
    try:
        proc = subprocess.run([node, jf.name], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        lines = [x for x in proc.stdout.splitlines() if x.strip()]
    finally:
        os.unlink(jf.name)

    if proc.returncode != 0:
        check("node 执行成功", False, (proc.stderr or proc.stdout)[:500])
        return
    kv = dict(x.split("=", 1) for x in lines)

    m_lz = json.loads(kv["M_lz"])
    m_bsp = json.loads(kv["M_bsp"])
    m_ann = json.loads(kv["M_ann"])
    m_bt = json.loads(kv["M_bt"])
    check("③a 连涨模式：「最近N根」**可用**（opacity 1 + input 未 disabled）",
          m_lz["recent"] == "1" and m_lz["recentDis"] is False, m_lz)
    check("③a 连涨模式：「扫描周期」「扫描来源」都可用（需求核心）",
          m_lz["freq"] == "1" and m_lz["src"] == "1", m_lz)
    check("③a 切到连涨时 N<2 自动填 3（当前值 '1' ⇒ '3'）",
          m_lz["val"] == "3", m_lz)
    check("③a 对照·买卖点模式：不自动改值（'1' 保持 '1'）—— 证明上一条不是恒真",
          m_bsp["val"] == "1" and m_bsp["recent"] == "1", m_bsp)
    check("③a 对照·标注/回测模式：「最近N根」置灰且不改值",
          m_ann["recent"] == "0.35" and m_ann["val"] == "1"
          and m_bt["recent"] == "0.35" and m_bt["val"] == "1", (m_ann, m_bt))

    order = json.loads(kv["ORDER"])
    check("③b 按区间涨幅降序：6.25 → 2.5 → 0.0 → -1.5", order["ok"], order)
    tag = json.loads(kv["TAG"])
    check("③b 标签为「N连涨」且用红色样式类 fl-rise（与 K 线红柱同色）",
          tag["lz"] and tag["cls"], tag)
    color = json.loads(kv["COLOR"])
    check("③b 涨红跌绿（正 #FF3C3C / 负 #00F0F0 / 0 归非负侧）+ 数值格式",
          color["pos"] and color["neg"] and color["zero"]
          and color["posTxt"] and color["negTxt"] and color["zeroTxt"], color)
    chk = json.loads(kv["CHK"])
    check("③b 命中即默认勾选（本模式每行都满足判据）", chk["all"], chk)
    fin = json.loads(kv["FIN"])
    check("③c 终态摘要含 连涨 N 只 / 跳过 N 只", fin["cnt"] and fin["skip"], fin)
    check("③c 口径披露：判据与涨幅基期都写在脸上 —— 基期写明「首根的前一根收盘」"
          "并显式否定「首根开盘」（防「图上明明是红的却没扫到」或「涨幅跟白框对不上」）",
          fin["cal"] and fin["gain"] and fin["gainOld"] and fin["rev"], fin)
    empty = json.loads(kv["EMPTY"])
    check("③c 空结果态给出可读提示（而非空白面板）", empty["msg"], empty)


# ══════════════════════════════════════════════════════════════════════
# ④ 静态结构契约
# ══════════════════════════════════════════════════════════════════════
def part4():
    print("══ ④ 静态结构契约（HTML 位置 / 版本号 / 白名单 / 后端分支）══")
    html = io.open(APPHTML, encoding="utf-8").read()
    check("④ 单选项存在（name=scan-mode / value=lianzhang）",
          bool(re.search(r'name="scan-mode"\s+value="lianzhang"', html)), "未找到")
    check("④ 位置正确：紧跟在「放量」之后、在「底分型」之前（需求指定）",
          html.index('value="fangliang"') < html.index('value="lianzhang"')
          < html.index('value="fx_d"'), "顺序不符")
    check("④ 该单选项带 onchange 联动置灰逻辑",
          bool(re.search(r'value="lianzhang"[^>]*onchange="updateScanRecentDisabled\(\)"',
                         html)), "缺 onchange")
    n_opts = len(re.findall(r'name="scan-mode"\s+value="', html))
    check("④ 扫描模式选项总数为 7（标注/均线/放量/连涨/底分型/买-卖点/回测）",
          n_opts == 7, n_opts)
    check("④ 资源版本号已抬到 v=71（防浏览器吃旧缓存），且 v=70 / v=69 / v=68 零残留",
          'app.js?v=71' in html and 'app.js?v=70' not in html
          and 'app.js?v=69' not in html and 'app.js?v=68' not in html, "版本号未同步")

    appjs = io.open(APPJS, encoding="utf-8").read()
    check("④ localStorage 白名单收 lianzhang（否则重开弹窗回落到标注模式）",
          bool(re.search(r'savedMode === "lianzhang"', appjs)), "白名单未含 lianzhang")
    check("④ 结果面板标题分支存在",
          '} else if (_scanMode === "lianzhang") {' in appjs, "缺标题分支")
    check("④ 提交 spec 用 mode: \"lianzhang\" 且分类读 is_lianzhang",
          bool(re.search(r'mode:\s*"lianzhang"', appjs))
          and bool(re.search(r'classify: function\(data\) \{ return !!data\.is_lianzhang; \}',
                             appjs)), "spec / classify 不符")

    src = io.open(os.path.join(ROOT, "App", "AppScan.py"), encoding="utf-8").read()
    check("④ 后端分支存在（if mode == \"lianzhang\"）",
          'if mode == "lianzhang":' in src, "未找到分支")
    check("④ 后端判据写死为**严格** close > open（与 K 线红色同源，不许写成 >=）",
          '(k.get("close", 0) or 0) > (k.get("open", 0) or 0) for k in seg' in src,
          "判据未找到或已被改宽")
    check("④ 后端与前端同源锚点：两处注释都写明「口径 = K 线图红色」",
          "口径 = K 线图红色" in src or "**红色 K 线**同源" in src, "后端缺同源说明")

    # 涨幅基期的新口径同样要钉死 —— 指回 K 线图白框，防再被改回「首根开盘」
    check("④ 后端涨幅基期写死为窗口首根的**前收**（与白框同源）",
          'prev_close = (klines[i_first - 1].get("close", 0) or 0) if i_first >= 1' in src,
          "涨幅基期未找到")
    check("④ 后端零 `first_open` 残留（旧口径必须清干净，不许留双轨）",
          "first_open" not in src, "first_open 仍有残留")
    check("④ 涨幅零价保护与白框同款（`prev_close != 0`，不是 `> 0`）",
          "if prev_close != 0 else 0.0" in src, "零价保护口径与白框不一致")
    check("④ 同源锚点：K 线图白框的基期表达式仍在（改了它必须回来重核本模式）",
          bool(re.search(r"prevKLine \? prevKLine\.close : ", appjs))
          and appjs.count("rightVisibleK.close - startPrice") >= 1, "白框基期锚点丢失")

    # 披露行本身也是契约：面板上把基期写错，比数字算错更难自查
    #   （用户正是照这句话去对白框的）。措辞要求：写明基期 + 显式否定旧口径。
    check("④ 面板口径披露行写明基期 = 首根的前一根收盘，且显式否定「首根开盘」",
          '涨幅 = 首根的前一根收盘 → 末根收盘' in appjs
          and '非首根开盘' in appjs
          and '涨幅 = 首根开盘' not in appjs, "披露行措辞未同步")


def main():
    print("=" * 68)
    print("「连涨」扫描模式（scan mode = lianzhang）护栏")
    print("=" * 68)
    part1()
    part2()
    part3()
    part4()
    print("-" * 68)
    total, passed = len(_OK), sum(_OK)
    print("合计 %d 项，通过 %d，失败 %d" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

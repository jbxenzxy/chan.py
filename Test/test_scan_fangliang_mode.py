# -*- coding: utf-8 -*-
"""「放量」扫描模式（scan mode = "fangliang"）护栏
====================================================================
「股票扫描」的放量模式 —— 在选定来源内，找出**最近 N 根内出现天量**的标的。

判据（`AppScan.Scanner.scan_one(mode="fangliang")`，后端为唯一事实源）：

  ① 取最近 N 根（弹窗「最近N根」，记为 recent_days）中**成交额最大**的一根，
     记为 A；
  ② 从 N 根之前再往前取 W 根作**比较窗口**（W = `app_config.
     SCAN_FANGLIANG_WINDOW_BARS`，默认 120，可配）；
  ③ A 的成交额 **严格大于**窗口内最高成交额 ⇒ 命中（= 成交额创 W 根新高）。

  ⚠️ 它**不是**「最近 N 根**天天**放量」—— N 根里只有最猛的那一根参与比较，
     另外 N-1 根多大都不影响判定。这条是最容易误解的点，写成断言防回潮。

边界（一律**静默**返回未命中，且不抛异常）：
  - K 线不足 `recent_days + W` 根 ⇒ 不参评（新股 / 停牌票）
  - A 的成交额为 0（脏数据）⇒ 不参评（防 `0 > 0` 的伪命中）
  - `A == 窗口峰值` ⇒ **不命中**（判据是严格 `>`，平量不算放量）

输出字段：`is_fangliang` / `amount_a`（A 的成交额）/ `peak_prev`（窗口峰值）/
`a_is_rise`（A 那根收阳否 —— 前端据此给标签上红/绿，与 K 线图成交额柱同色）。

四段守护 ——
  ① 后端判据（合成 K 线，手算期望）：命中 / 平量不命中 / 数据不足 / A为0 /
     「最猛那根」语义（N 根中非最大者多大都不算）/ 严格 `>` 判别力 / 只看最近 N 根；
  ② 真实冻结切片：具体窗口的 A / 峰值 / 命中期望 + 截断点扫描与独立复算逐组一致
     （证明模块判定 == 同口径朴素复算，且该批次非空转）；
  ③ 前端真函数（node 抽 app.js 真代码）：口径披露行的**存在性、措辞、窗口根数
     来源**（SSOT 配置下发 / 未拉到时的中性回落），以及进度期与终态两处**同源**
     （共用 `_fangliangCaliberHtml`，防两处漂移）；
  ④ 静态结构契约：披露函数存在 / 后端判据源码锚点 / 配置项 SSOT / 资源版本号。

样本：合成 K 线（手算期望）+ 真实冻结切片 `Test/fixtures_real/sz002190_d.json`
（1393 根日线，与 bt01 / 回测 / 连涨同一批）。

跑法：`python Test/test_scan_fangliang_mode.py`（退出码 0/1 即判决）
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
from App.AppConfig import app_config                  # noqa: E402

FIXTURES = os.path.join(ROOT, "Test", "fixtures_real")
APPJS = os.path.join(ROOT, "Frontend", "app.js")
APPHTML = os.path.join(ROOT, "Frontend", "app.html")

_OK = []


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  \u2014\u2014 " + str(detail)[:260]))
    return cond


# W：本测试全程用**运行时真值**，不写死 120 —— 它可被配置改，
#   写死会让测试在别人调过配置的机器上假红。
W = int(app_config.scan_fangliang_window_bars or 120)


# ── 桩驱动：把 analyze_stock 换成本地 K 线（不改被测逻辑）────────────
#   与 test_scan_lianzhang_mode.py 同一手法：scan_one 消费的就是
#   result["klines"] / result["meta"]，桩喂的数据与真路径逐字段同形。
def _run(klines, mode="fangliang", recent="3", name="\u6d4b\u8bd5\u80a1",
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
    """rows = [(date, open, close, amount), ...] -> klines

    high/low 取开收极值（放量判据只看 amount，够用）。
    """
    return [{"date": d, "open": o, "close": c, "high": max(o, c),
             "low": min(o, c), "vol": 1, "amount": amt}
            for (d, o, c, amt) in rows]


def _flat(n, amount=1000.0, start_day=1):
    """n 根平铺K线（成交额恒定），供拼窗口用。"""
    return [("d%04d" % (start_day + i), 10.0, 10.1, amount) for i in range(n)]


# ══════════════════════════════════════════════════════════════════════
# ① 后端判据（合成 K 线，手算期望）
# ══════════════════════════════════════════════════════════════════════
def part1():
    print("══ ① 后端判据：最近 N 根内最大成交额 A 严格大于其前 W 根峰值 ══")

    # W 必须够小才跑得动合成样本；这里直接用配置真值确认窗口语义，
    # 若配置被改到极大，合成样本会退化成「数据不足」—— 用断言显式暴露。
    check("① 比较窗口 W 取自配置（app_config.SCAN_FANGLIANG_WINDOW_BARS），"
          "运行时可读且 >= 1", isinstance(W, int) and W >= 1, W)

    # A 命中：前窗 W 根恒 1000，最近 3 根中最大 5000 > 1000
    kl_hit = _series(_flat(W, 1000.0) + [("a1", 10.0, 10.2, 1200.0),
                                         ("a2", 10.2, 10.3, 1500.0),
                                         ("a3", 10.3, 10.5, 5000.0)])
    row = _run(kl_hit, recent="3")
    check("① 最近 N 根内出现天量（5000 > 前窗峰值 1000）⇒ 命中",
          row.get("is_fangliang") is True, row)
    check("① 命中行补市场前缀 + 名称/周期透传 + 无 error",
          row.get("code") == "sz002190" and row.get("name") == "测试股"
          and row.get("freq") == "d" and "error" not in row, row)
    check("① amount_a 回显 A 的成交额（= 最近 N 根内的最大值 5000，不是最后一根）",
          row.get("amount_a") == 5000.0, row.get("amount_a"))
    check("① peak_prev 回显比较窗口峰值（1000）",
          row.get("peak_prev") == 1000.0, row.get("peak_prev"))
    check("① a_is_rise = A 那根收阳（a3: close 10.5 > open 10.3）",
          row.get("a_is_rise") is True, row.get("a_is_rise"))

    # 「最猛那根」语义：N 根中**非最大**的根无论多大都不参与比较 ——
    #   把天量放在 N 根的最前面（A = 最前那根），最后一根很小，仍应命中。
    kl_first = _series(_flat(W, 1000.0) + [("a1", 10.0, 10.2, 5000.0),
                                           ("a2", 10.2, 10.3, 900.0),
                                           ("a3", 10.3, 10.5, 800.0)])
    r_first = _run(kl_first, recent="3")
    check("① A = 最近 N 根内的**最大**者（不必是最后一根）：天量在最前仍命中，"
          "amount_a 取 5000",
          r_first.get("is_fangliang") is True
          and r_first.get("amount_a") == 5000.0, r_first)

    # 判别力：天量在**窗口内**（不在最近 N 根）⇒ 不得命中。
    #   这条同时证否「放量 = 序列里出现过巨量」这种误读。
    kl_old = _series([("p1", 10.0, 10.1, 9000.0)] + _flat(W - 1, 1000.0, 2)
                     + _flat(3, 500.0, W + 5))
    r_old = _run(kl_old, recent="3")
    check("① 判别力：巨量发生在**比较窗口内**（不在最近 N 根）⇒ **不命中**"
          "（放量 ≠ 历史上出现过巨量）",
          r_old.get("is_fangliang") is False, r_old)

    # 平量：A == 窗口峰值 ⇒ 严格 `>` 不满足 ⇒ 不命中
    kl_eq = _series(_flat(W, 1000.0) + [("a1", 10.0, 10.2, 800.0),
                                        ("a2", 10.2, 10.3, 900.0),
                                        ("a3", 10.3, 10.5, 1000.0)])
    r_eq = _run(kl_eq, recent="3")
    check("① 平量不算放量：A(1000) == 窗口峰值(1000) ⇒ **不命中**"
          "（判据是严格 `>`，写成 `>=` 即回潮）",
          r_eq.get("is_fangliang") is False, r_eq)

    # 差一点点：A = 1000.01 > 1000 ⇒ 命中（证明上一条不是恒不命中）
    kl_eps = _series(_flat(W, 1000.0) + [("a1", 10.0, 10.2, 800.0),
                                         ("a2", 10.2, 10.3, 900.0),
                                         ("a3", 10.3, 10.5, 1000.01)])
    check("① 判别力自证：A 仅超出窗口峰值一丝（1000.01 > 1000）即命中 "
          "—— 证明「平量不命中」不是恒不命中的假绿",
          _run(kl_eps, recent="3").get("is_fangliang") is True)

    # 数据不足：不足 recent_days + W 根 ⇒ 静默未命中
    r_short = _run(_series(_flat(W - 1, 1000.0) + _flat(3, 5000.0, W + 5)),
                   recent="3")
    check("① K 线不足 N + W 根 ⇒ 静默未命中且不抛异常（新股/停牌票）",
          r_short.get("is_fangliang") is False and "error" not in r_short, r_short)

    # A = 0（脏数据）⇒ 不参评，防 0 > 0 伪命中
    r_zero = _run(_series(_flat(W, 0.0) + _flat(3, 0.0, W + 5)), recent="3")
    check("① A 的成交额为 0（脏数据）⇒ 不参评、不命中、不抛异常",
          r_zero.get("is_fangliang") is False and "error" not in r_zero, r_zero)

    # 未命中行必须是裸代码（与其它模式同形）
    check("① 未命中行的 code 是**裸代码**（不补前缀，与其它模式同形）",
          r_eq.get("code") == "002190", r_eq.get("code"))

    # 收阴的 A ⇒ a_is_rise False（前端据此上绿标签）
    kl_fall = _series(_flat(W, 1000.0) + [("a1", 10.0, 10.2, 1200.0),
                                          ("a2", 10.2, 10.3, 1500.0),
                                          ("a3", 10.5, 10.3, 5000.0)])
    r_fall = _run(kl_fall, recent="3")
    check("① A 那根收阴（close 10.3 < open 10.5）⇒ a_is_rise False"
          "（前端标签走绿色，与 K 线成交额柱同色）",
          r_fall.get("is_fangliang") is True
          and r_fall.get("a_is_rise") is False, r_fall)

    # N=1 边界
    r_n1 = _run(_series(_flat(W, 1000.0) + [("a1", 10.0, 10.5, 5000.0)]),
                recent="1")
    check("① N=1：最近 1 根即 A，> 窗口峰值 ⇒ 命中",
          r_n1.get("is_fangliang") is True and r_n1.get("amount_a") == 5000.0,
          r_n1)


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
    print("══ ② 真实冻结切片：具体窗口期望 + 截断点扫描一致性 ══")
    kl = _page_klines("sz002190_d.json")
    check("② 切片规模 1393 根（与 bt01 / 回测 / 连涨同一批）", len(kl) == 1393, len(kl))

    # 截断点扫描：任意 L × N，模块判定与独立复算逐组一致。
    #   起点 = N + W（数据刚够），终点 = 全长；步长 3 兼顾覆盖与耗时。
    #   ⚠️ 必须覆盖[数据刚够, 全长]整段，不能只扫尾部 —— 命中窗口可能落在
    #   前段（本切片唯一命中在 L=1055），只扫尾部会让「非空转」自证变成假绿。
    bad = []
    bad_amt = []
    hits = 0
    probes = 0
    for L in range(3 + W, len(kl) + 1, 3):
        for N in (1, 3, 5):
            need = N + W
            if L < need:
                continue
            probes += 1
            row_l = _run(kl[:L], recent=str(N))
            got = row_l.get("is_fangliang")
            seg = kl[L - N:L]
            amt_a = max((k["amount"] for k in seg), default=0)
            prev = kl[L - need:L - N]
            peak = max((k["amount"] for k in prev), default=0)
            want = amt_a > 0 and amt_a > peak
            if got is not want:
                bad.append((L, N, got, want, amt_a, peak))
            if got:
                hits += 1
                if row_l.get("amount_a") != amt_a or row_l.get("peak_prev") != peak:
                    bad_amt.append((L, N, row_l.get("amount_a"), amt_a,
                                    row_l.get("peak_prev"), peak))
    check("② %d 组（截断点 × N=1/3/5）判定与独立复算**逐组一致**" % probes,
          not bad, bad[:5])
    check("② 同批 %d 个命中项的 amount_a / peak_prev 也与独立复算逐组一致" % hits,
          not bad_amt, bad_amt[:5])
    check("② 该批次非空转（命中 > 0，判别力自证）", hits > 0, hits)

    # 具体窗口期望（可读的锚点：供人肉复核用）
    L0 = 1393
    row = _run(kl[:L0], recent="3")
    seg = kl[L0 - 3:L0]
    amt_a = max(k["amount"] for k in seg)
    peak = max(k["amount"] for k in kl[L0 - 3 - W:L0 - 3])
    check("② L=1393 / N=3 ⇒ 判定与手算一致（A=%.0f vs 前 %d 根峰值=%.0f ⇒ %s）"
          % (amt_a, W, peak, "命中" if amt_a > peak else "未命中"),
          row.get("is_fangliang") is (amt_a > peak), row)

    # 样本内**必须**存在至少一个命中窗口：否则 ② 整体只证明了「都在返回 False」。
    win_hit = None
    for L in range(1000, 1394):
        if L < 3 + W:
            continue
        segx = kl[L - 3:L]
        a = max(k["amount"] for k in segx)
        p = max(k["amount"] for k in kl[L - 3 - W:L - 3])
        if a > p:
            win_hit = (L, a, p)
            break
    check("② 样本内确实存在命中窗口（找到 L=%s，A=%.0f > 峰值=%.0f）"
          % (win_hit[0] if win_hit else "-",
             win_hit[1] if win_hit else 0, win_hit[2] if win_hit else 0),
          win_hit is not None, win_hit)
    if win_hit:
        rw = _run(kl[:win_hit[0]], recent="3")
        check("② 该命中窗口经模块判定同样命中（非空转）",
              rw.get("is_fangliang") is True, rw)


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
        "var _scanFangliangWindowBars = null;\n"
        "function chkBox(code, checked) { return '<chk:' + code + ':' + (checked ? '1' : '0') + '>'; }\n"
        "function _scanMarketSummaryHtml(rs) { return ''; }\n"
        "function updateScanSaveBtn() {}\n"
        "function _scanSourceLabel() { return '\u81ea\u9009\u80a1'; }\n"
        "var DOM = { 'scan-body': { innerHTML: '' } };\n"
        "var document = {\n"
        "  getElementById: function (id) { return DOM[id] || null; },\n"
        "  querySelector: function () { return null; }\n"
        "};\n"
        "var out = [];\n"
    )

    # 打桩一个「A 收阳」的命中行，验证行本身仍照常渲染（披露行不挤掉数据行）
    rows = [{"code": "sz000001", "name": "A\u80a1", "a_is_rise": True,
             "amount_a": 5e8, "peak_prev": 1e8}]

    driver = (
        # ── ③a 窗口根数已拉到：文案里出现具体数字，且与变量同值 ──
        "_scanFangliangWindowBars = 120;\n"
        "_scanRecentDays = 3;\n"
        "out.push('W120=' + JSON.stringify({ h: _fangliangCaliberHtml() }));\n"
        # ── ③b 未拉到（null）：回落中性措辞，**不得**出现 \"120\" 这类硬编码 ──
        "_scanFangliangWindowBars = null;\n"
        "out.push('WNUL=' + JSON.stringify({ h: _fangliangCaliberHtml() }));\n"
        # ── ③c 非 120 的配置值必须原样带出（证明不是写死 120） ──
        "_scanFangliangWindowBars = 250;\n"
        "out.push('W250=' + JSON.stringify({ h: _fangliangCaliberHtml() }));\n"
        # ── ③d 终态渲染：披露行 + 数据行共存 ──
        "_scanFangliangWindowBars = 120;\n"
        "document.getElementById('scan-body').innerHTML = '';\n"
        "renderFangliangScanResults(" + json.dumps(rows, ensure_ascii=False)
        + ".slice(), 100, 7, false);\n"
        "var F = document.getElementById('scan-body').innerHTML;\n"
        "out.push('FIN=' + JSON.stringify({ html: F }));\n"
        # ── ③e 空结果态：披露行仍在（口径不该只在有票时才出现） ──
        "document.getElementById('scan-body').innerHTML = '';\n"
        "renderFangliangScanResults([], 100, 100, false);\n"
        "out.push('EMPTY=' + JSON.stringify({\n"
        "  html: document.getElementById('scan-body').innerHTML }));\n"
        "console.log(out.join('\\n'));\n"
    )

    harness = (prelude
               + _extract_any_fn(appjs, "_fangliangCaliberHtml") + "\n"
               + _extract_any_fn(appjs, "renderFangliangScanResults") + "\n"
               + _extract_any_fn(appjs, "buildFangliangTagHtml") + "\n"
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

    w120 = json.loads(kv["W120"])["h"]
    wnul = json.loads(kv["WNUL"])["h"]
    w250 = json.loads(kv["W250"])["h"]
    fin = json.loads(kv["FIN"])["html"]
    empty = json.loads(kv["EMPTY"])["html"]

    check("③a 已拉到窗口根数时，披露行带出**配置的真值**（120）"
          "且说明判据（成交额最大的一根 A）",
          "120" in w120 and ("A" in w120) and ("\u6210\u4ea4\u989d" in w120), w120)
    check("③a 披露行写明「最近 N 根」（N 取自 _scanRecentDays = 3）",
          "\u6700\u8fd1 3 \u6839" in w120, w120)
    check("③b 未拉到配置时**回落中性措辞**，且**不出现硬编码 120**"
          "（在前端复制一份口径 = 迟早与后端漂移）",
          ("\u6bd4\u8f83\u7a97\u53e3" in wnul) and ("120" not in wnul), wnul)
    check("③c 非默认窗口值（250）原样带出 ⇒ 证明窗口根数是**取值**而非字面量",
          "250" in w250 and "250" not in w120, (w250, w120))
    check("③c 披露行样式与连涨口径行同构（class=scan-summary + 同字号/颜色）",
          'class="scan-summary"' in w120 and "font-size:10px" in w120
          and "#7a8399" in w120, w120)
    check("③d 终态面板同时含**披露行**与**数据行**（披露不挤掉结果）",
          ("\u6210\u4ea4\u989d" in fin) and ("A\u80a1" in fin)
          and ("\u653e\u91cf" in fin), fin[:300])
    check("③e 空结果态**同样**给出披露行（口径不该只在有票时才显示）",
          "\u6210\u4ea4\u989d" in empty and "\u672a\u53d1\u73b0\u653e\u91cf\u6807\u7684" in empty,
          empty[:300])
    check("③e 空结果态数据行数为 0（无残留行）",
          "scan-stock-row" not in empty, empty[:200])


# ══════════════════════════════════════════════════════════════════════
# ④ 静态结构契约
# ══════════════════════════════════════════════════════════════════════
def part4():
    print("══ ④ 静态结构契约（披露函数 / 后端判据 / 配置 SSOT / 版本号）══")
    appjs = io.open(APPJS, encoding="utf-8").read()
    html = io.open(APPHTML, encoding="utf-8").read()
    src = io.open(os.path.join(ROOT, "App", "AppScan.py"), encoding="utf-8").read()

    check("④ 披露构造函数存在且是**单一来源**（_fangliangCaliberHtml）",
          "function _fangliangCaliberHtml()" in appjs, "未找到披露函数")
    # 单一来源的实质：进度期与终态都调它，而不是各写一份字面量
    n_call = len(re.findall(r"_fangliangCaliberHtml\(\)", appjs))
    check("④ 进度期与终态**共用**同一披露函数（调用点 >= 3：定义 + renderRows + "
          "renderFangliangScanResults）", n_call >= 3, n_call)
    check("④ 披露文案**不在别处重复门面**（防两处漂移）：全仓搜不到第二处"
          "「成交额最大的一根」字面量",
          appjs.count("\u6210\u4ea4\u989d\u6700\u5927\u7684\u4e00\u6839") == 1,
          appjs.count("\u6210\u4ea4\u989d\u6700\u5927\u7684\u4e00\u6839"))

    # 配置 SSOT：前端只**接收**、不写默认数字
    check("④ 前端接收 /api/health 下发的 scan_fangliang_window_bars 到 "
          "_scanFangliangWindowBars",
          re.search(r"data\.config\.scan_fangliang_window_bars", appjs) is not None,
          "未接收配置")
    check("④ 变量声明存在且初值 null（未拉到 ⇒ 中性回落，不预设 120）",
          re.search(r"let _scanFangliangWindowBars = null;", appjs) is not None,
          "变量声明不符")

    # 后端判据锚点：严格 `>` + 窗口来自配置 + 两个输出字段
    check("④ 后端判据写死为**严格** amount_a > peak_prev（不许写成 >=）",
          "is_fangliang = amount_a > peak_prev" in src, "判据未找到或已被改宽")
    check("④ 后端窗口根数取自配置（SSOT = SCAN_FANGLIANG_WINDOW_BARS）",
          "app_config.scan_fangliang_window_bars" in src, "窗口未走配置")
    check("④ 后端输出 amount_a / peak_prev / a_is_rise 三字段（前端披露与标签依赖）",
          '"amount_a": amount_a' in src and '"peak_prev": peak_prev' in src
          and '"a_is_rise": a_is_rise' in src, "字段缺失")
    check("④ 后端零价保护：A 为 0 时不参评（防 `0 > 0` 伪命中）",
          "if amount_a > 0:" in src, "零价保护缺失")
    check("④ 后端数据不足静默跳过（need_bars = recent_days + window_bars）",
          "need_bars = recent_days + window_bars" in src, "边界判据缺失")

    check("④ 配置项 SCAN_FANGLIANG_WINDOW_BARS 在 App/AppConfig.py 有定义与字段",
          "SCAN_FANGLIANG_WINDOW_BARS" in
          io.open(os.path.join(ROOT, "App", "AppConfig.py"), encoding="utf-8").read(),
          "配置项未定义")

    check("④ 资源版本号已抬到 v=73（防浏览器吃旧缓存），且 v=72 及更早零残留",
          'app.js?v=73' in html and 'app.js?v=72' not in html,
          "版本号未同步")
    check("④ 端点仍走 /api/health 的 config（披露取值的唯一来路）",
          "api_health" in io.open(os.path.join(ROOT, "FrontAPI.py"),
                                  encoding="utf-8").read(), "健康检查端点缺失")


def main():
    print("=" * 68)
    print("「放量」扫描模式（scan mode = fangliang）护栏")
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

# -*- coding: utf-8 -*-
"""底部指标区「单窗双槽位 + RSI」改造的护栏（2026-10-09）。

对应设计 `Docs/底部指标区改造设计_单窗双槽位.md` §4.3 的四条建议护栏：

  ① 槽位几何不变式（node 抽**真实源码**执行）
     · n = 1（双窗）时 getBottomSlotLabelArea(0) / getBottomSlotPlotArea(0) 与改造前
       getMacdTextArea() / getVolArea() 的公式**逐像素相同** ⇒ 双窗零回归
     · n = 2（单窗）时 label0 ∪ plot0 ∪ label1 ∪ plot1 恰好铺满底部区，无重叠无空隙
     · 每槽绘图窗高度 slotH(n) = netH×VOL_RATIO − MACD_TEXT_HEIGHT，**与 n 无关**
     · 底部区下沿恒 ≡ L.top + netH（与改造前 volArea 下沿同值）
  ② RSI 数值对齐（两份**后端**实现逐点比对）
     · App/AppUtils.calculate_rsi(period) ≡ Math/RSI.RSI(period)，含「前 period−1 根
       用简单平均」这个非标准播种口径
     · 边界：首根恒 50；down == 0 → 100/0；全平 → 末根 0；样本 < period
     · 预览 bar 继承：_inherit_metrics_for_preview_bar 把 rsi 一并继承
     · SSE 侧 _apply_rsi_full 定义 1 处、两条快照路径都调用
  ③ chip 命中与双击拦截（源码契约）
     · getBottomSlotChipRect(i) / hitBottomSlotChip(x, y) 是纯函数且被 click 消费
     · 双击指标区**只做命中拦截**（两块 dblclick 都在底带分支 return，不落到底下的
       「双击空白 → 恢复全视图」）
     · 双击切换已废除（无 _showVolume = !_showVolume）；chip 按 BOTTOM_ORDER 循环
     · 单击处理器用 click.detail > 1 忽略双击的第二次点击（不得连切两位）
  ④ 翻转视图一致性（node 抽 rsiToY 真执行 + 源码反锚点）
     · rsiToY(v) 翻转前后之和 ≡ 2×area.y + area.h；50 是不动点；70 / 30 互换
     · drawRsi / drawRsiAxis 的所有 y 都走 rsiToY(v)，**禁**硬编码相对比例
       （area.h * 0.3 / * 0.7 / * 0.5 之类）
     · drawRsiAxis 中间档恒 "50"（不是 MACD 的零线 "0"），且不沿用 zeroY 命名
     · RSI 分支不做颜色对调（只翻 Y 位置、不改颜色）

零网络、零浏览器、秒级。跑法：python Test/test_bottom_slots.py
"""
import glob
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")
APPSSE_PY = os.path.join(REPO_ROOT, "App", "AppSSE.py")

VOL_RATIO = 0.2
MACD_TEXT_HEIGHT = 14

_passed = 0
_failed = []


def rec(no, title, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
        print("[PASS] %s %s" % (no, title))
        if detail:
            print("        " + detail)
    else:
        _failed.append("%s %s | %s" % (no, title, detail))
        print("[FAIL] %s %s" % (no, title) + (" | " + detail if detail else ""))


def read(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


# ══════════════════════════════════════════════════════════════════
# JS 源码抽取（花括号配对；跳过字符串 / 模板串 / 注释）
# ══════════════════════════════════════════════════════════════════
def _to_matching_brace(src, i):
    """i 指向 '{'，返回配对 '}' 之后的下标。"""
    depth, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            q, i = c, i + 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError("花括号不配对")


def fn_src(src, name):
    m = re.search(r"^[ \t]*function\s+%s\s*\(" % re.escape(name), src, re.M)
    if not m:
        raise AssertionError("未找到 function %s" % name)
    b = src.index("{", m.end())
    return src[m.start():_to_matching_brace(src, b)]


def const_line(src, pat):
    m = re.search(pat, src, re.M)
    if not m:
        raise AssertionError("未匹配常量: %s" % pat)
    return m.group(0)


def strip_comments(src):
    """剥掉 JS 的 // 与 /* */ 注释（保留字符串字面量内容）。"""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            q = c
            out.append(c)
            i += 1
            while i < n:
                out.append(src[i])
                if src[i] == "\\":
                    if i + 1 < n:
                        out.append(src[i + 1])
                        i += 2
                        continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def find_node():
    cand = [os.environ.get("NODE_EXE"), shutil.which("node")]
    cand += glob.glob(os.path.expanduser(
        "~/.workbuddy/binaries/node/versions/*/node.exe"))
    cand += [r"C:\Program Files\nodejs\node.exe"]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def run_node(src, tag):
    node = find_node()
    if not node:
        rec(tag, "node 可用（离线执行真实代码段）", False, "未找到 node")
        return None
    tmp = tempfile.mkdtemp(prefix="bottom_slots_")
    p = os.path.join(tmp, "drv.js")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        r = subprocess.run([node, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode != 0:
        rec(tag, "node 执行成功", False, (r.stderr or "").strip()[:400])
        return None
    return json.loads(r.stdout)


# ══════════════════════════════════════════════════════════════════
# ① 槽位几何不变式
# ══════════════════════════════════════════════════════════════════
GEO_TAIL = r"""
function measure(dual) {
  isDualWindow = dual;
  const n = SLOT_COUNT();
  const L = getLayoutParams();
  const netH = canvas.clientHeight - L.top - L.bottom - L.gap;
  const chartH = netH * (1 - L.volRatio);
  const labels = [], plots = [];
  for (let i = 0; i < n; i++) {
    labels.push(getBottomSlotLabelArea(i));
    plots.push(getBottomSlotPlotArea(i));
  }
  return { n: n, L: L, netH: netH, chartH: chartH,
           total: netH * L.volRatio, labels: labels, plots: plots,
           bottomBottomY: getBottomAreaBottomY() };
}
// 单窗几何下的 netH（与 measure(false) 一致）及按 volRatioFor 推出的每槽高。
// 注意双窗走 DUAL_LAYOUT（volRatio 0.30），**不**经 volRatioFor ⇒ 「与槽数无关」
// 只在同一窗口几何（单窗 PADDING/GAP）下成立，双窗另由零回归两条断言钉住。
function slotHFor(n, netH) {
  return (netH * volRatioFor(n) - n * MACD_TEXT_HEIGHT - (n - 1) * SLOT_GAP) / n;
}
const netH_single = canvas.clientHeight - PADDING.top - PADDING.bottom - GAP;
const out = { single: measure(false), dual: measure(true),
              volRatioFor1: volRatioFor(1), volRatioFor2: volRatioFor(2),
              netHSingle: netH_single,
              slotH1: slotHFor(1, netH_single), slotH2: slotHFor(2, netH_single) };
process.stdout.write(JSON.stringify(out));
"""


def _geo_driver(js, W, H):
    return "\n".join([
        const_line(js, r"const PADDING = \{[^}]*\};"),
        const_line(js, r"const VOL_RATIO = [^\n]*"),
        const_line(js, r"const MACD_TEXT_HEIGHT = \d+;"),
        const_line(js, r"const SLOT_GAP = \d+;"),
        const_line(js, r"const DUAL_LAYOUT = \{[^}]*\};"),
        const_line(js, r"const SLOT_COUNT = \(\) => \(isDualWindow \? 1 : 2\);"),
        "let isDualWindow = false;",
        "let canvas = { clientWidth: %d, clientHeight: %d };" % (W, H),
        fn_src(js, "volRatioFor"),
        fn_src(js, "getLayoutParams"),
        fn_src(js, "_bottomSlotMetrics"),
        fn_src(js, "getBottomSlotLabelArea"),
        fn_src(js, "getBottomSlotPlotArea"),
        fn_src(js, "getBottomAreaBottomY"),
        GEO_TAIL,
    ])


def test_geometry():
    print("\n① 槽位几何不变式（node 抽真实源码执行）")
    js = read(APP_JS)
    d = run_node(_geo_driver(js, 1600, 820), "①")
    if d is None:
        return
    s, du = d["single"], d["dual"]

    rec("①", "槽数：单窗 2 槽 / 双窗 1 槽", s["n"] == 2 and du["n"] == 1,
        "单窗 n=%s 双窗 n=%s" % (s["n"], du["n"]))
    rec("①", "volRatioFor(n) = VOL_RATIO × n（n=1 时恒等于 VOL_RATIO）",
        abs(d["volRatioFor1"] - VOL_RATIO) < 1e-12
        and abs(d["volRatioFor2"] - VOL_RATIO * 2) < 1e-12,
        "for(1)=%s for(2)=%s" % (d["volRatioFor1"], d["volRatioFor2"]))
    rec("①", "双窗 volRatio 仍走 DUAL_LAYOUT（0.30，未被 volRatioFor 顶替）",
        abs(du["L"]["volRatio"] - 0.30) < 1e-12, str(du["L"]["volRatio"]))
    rec("①", "单窗 volRatio = volRatioFor(SLOT_COUNT()) = 0.40",
        abs(s["L"]["volRatio"] - VOL_RATIO * 2) < 1e-12, str(s["L"]["volRatio"]))

    # n = 1 与改造前公式逐像素等价（改造前：getMacdTextArea / getVolArea）
    old_label = {"y": du["L"]["top"] + du["chartH"], "h": MACD_TEXT_HEIGHT}
    old_plot = {"y": old_label["y"] + MACD_TEXT_HEIGHT,
                "h": du["netH"] * du["L"]["volRatio"] - MACD_TEXT_HEIGHT}
    rec("①", "n=1 标签行 ≡ 改造前 getMacdTextArea()（双窗零回归）",
        abs(du["labels"][0]["y"] - old_label["y"]) < 1e-9
        and abs(du["labels"][0]["h"] - old_label["h"]) < 1e-9,
        "got y=%.4f h=%.4f / old y=%.4f h=%.4f" % (
            du["labels"][0]["y"], du["labels"][0]["h"], old_label["y"], old_label["h"]))
    rec("①", "n=1 绘图窗 ≡ 改造前 getVolArea()（双窗零回归）",
        abs(du["plots"][0]["y"] - old_plot["y"]) < 1e-9
        and abs(du["plots"][0]["h"] - old_plot["h"]) < 1e-9,
        "got y=%.4f h=%.4f / old y=%.4f h=%.4f" % (
            du["plots"][0]["y"], du["plots"][0]["h"], old_plot["y"], old_plot["h"]))

    # slotH 与槽数无关（设计不变式；只在**同一窗口几何**下成立 —— 双窗走
    # DUAL_LAYOUT 的 0.30，不经 volRatioFor，已由上面两条零回归断言另行钉住）
    expect_h = s["netH"] * VOL_RATIO - MACD_TEXT_HEIGHT
    rec("①", "单窗每槽绘图窗 ≡ netH×VOL_RATIO − MACD_TEXT_HEIGHT（实测量，非公式）",
        abs(s["plots"][0]["h"] - expect_h) < 1e-9,
        "实测=%.4f 期望=%.4f" % (s["plots"][0]["h"], expect_h))
    rec("①", "同一窗口几何下 slotH(1) == slotH(2)（volRatioFor = n × VOL_RATIO 的唯一解）",
        abs(d["slotH1"] - d["slotH2"]) < 1e-9
        and abs(d["slotH2"] - s["plots"][0]["h"]) < 1e-9,
        "slotH(1)=%.4f slotH(2)=%.4f 实测n=2=%.4f" % (
            d["slotH1"], d["slotH2"], s["plots"][0]["h"]))

    # 四段恰好铺满
    segs = [(s["labels"][0]["y"], s["labels"][0]["h"]),
            (s["plots"][0]["y"], s["plots"][0]["h"]),
            (s["labels"][1]["y"], s["labels"][1]["h"]),
            (s["plots"][1]["y"], s["plots"][1]["h"])]
    ok = abs(segs[0][0] - (s["L"]["top"] + s["chartH"])) < 1e-9
    for k in range(3):
        ok = ok and abs((segs[k][0] + segs[k][1]) - segs[k + 1][0]) < 1e-9
    ok = ok and abs((segs[3][0] + segs[3][1]) - s["bottomBottomY"]) < 1e-9
    rec("①", "n=2 四段（label0/plot0/label1/plot1）恰好铺满底部区：无重叠、无空隙", ok,
        str([(round(a, 2), round(b, 2)) for a, b in segs]))

    rec("①", "底部区下沿 getBottomAreaBottomY() ≡ L.top + netH（与改造前 volArea 下沿同值）",
        abs(s["bottomBottomY"] - (s["L"]["top"] + s["netH"])) < 1e-9
        and abs(du["bottomBottomY"] - (du["L"]["top"] + du["netH"])) < 1e-9,
        "单窗=%.4f 双窗=%.4f" % (s["bottomBottomY"], du["bottomBottomY"]))


# ══════════════════════════════════════════════════════════════════
# ② RSI 数值对齐（两份后端实现）
# ══════════════════════════════════════════════════════════════════
def _cases():
    rnd = random.Random(20261009)
    px, walk = 100.0, []
    for _ in range(300):
        px = max(1.0, px + rnd.uniform(-2, 2))
        walk.append(px)
    return [
        ("空", []),
        ("单根", [10.0]),
        ("两根", [10.0, 11.0]),
        ("样本 < period", [10.0, 11.0, 12.0, 11.0, 12.0, 13.0, 12.0, 13.0]),
        ("全平", [5.0] * 40),
        ("全跌", [100.0 - i for i in range(60)]),
        ("单调上涨", [float(i) for i in range(1, 80)]),
        ("随机游走 300 根", walk),
    ]


def test_rsi_numeric():
    print("\n② RSI 数值对齐：App/AppUtils.calculate_rsi ≡ Math/RSI.RSI（逐点）")
    from App.AppUtils import calculate_rsi, _inherit_metrics_for_preview_bar
    from Math.RSI import RSI

    def ref(seq, period):
        r = RSI(period=period)
        return [r.add(c) for c in seq]

    cases = _cases()
    worst, bad = 0.0, []
    for name, seq in cases:
        for period in (12, 14, 6):
            got = calculate_rsi(list(seq), period)
            want = ref(list(seq), period)
            if len(got) != len(want):
                bad.append("%s/p%d 长度 %d vs %d" % (name, period, len(got), len(want)))
                continue
            for a, b in zip(got, want):
                if abs(a - b) > worst:
                    worst = abs(a - b)
                    if worst > 1e-12:
                        bad.append("%s/p%d 最大偏差 %.3e" % (name, period, worst))
    rec("②", "%d 组序列 × 3 个 period（12/14/6）逐点 ≡ 核心 Math/RSI.RSI" % len(cases),
        not bad and worst < 1e-12, "最大偏差 %.3e %s" % (worst, bad[:3]))

    flat = calculate_rsi([5.0] * 40)
    rec("②", "全平序列：首根 50.0、末根 0.0（down == 0 且 up == 0）",
        flat[0] == 50.0 and flat[-1] == 0.0, "首=%s 末=%s" % (flat[0], flat[-1]))
    up = calculate_rsi([float(i) for i in range(1, 40)])
    rec("②", "单调上涨：首根 50.0、末根 100.0（down == 0 且 up > 0）",
        up[0] == 50.0 and up[-1] == 100.0, "首=%s 末=%s" % (up[0], up[-1]))
    dn = calculate_rsi([100.0 - i for i in range(40)])
    rec("②", "单调下跌：首根 50.0、末根 0.0", dn[0] == 50.0 and dn[-1] == 0.0,
        "首=%s 末=%s" % (dn[0], dn[-1]))
    short = calculate_rsi([10.0, 11.0, 12.0])
    rec("②", "样本 < period：仍逐根有值、首根恒 50.0（非标准播种口径）",
        len(short) == 3 and short[0] == 50.0, str(short))
    rec("②", "默认 period = 12（设计定稿；不是核心 RSI 的 14）",
        calculate_rsi.__defaults__ == (12,), str(calculate_rsi.__defaults__))

    # klines_list[-1] 就是预览 bar（假数据），继承源是 [-2]
    kl = [{"dif": 2.0, "dea": 1.0, "macd": 2.0, "rsi": 42.25},
          {"dif": 9.9, "dea": 9.9, "macd": 9.9, "rsi": 9.9}]
    _inherit_metrics_for_preview_bar(kl)
    rec("②", "预览 bar 继承：末根 k.rsi ≡ 前一根的 rsi（与 dif/dea/macd 同口径）",
        kl[-1] == {"dif": 2.0, "dea": 1.0, "macd": 2.0, "rsi": 42.25}, str(kl[-1]))
    kl1 = [{"rsi": 7.0}]
    _inherit_metrics_for_preview_bar(kl1)
    rec("②", "预览 bar 继承：len < 2 时直接返回（不抛、不改）",
        kl1 == [{"rsi": 7.0}], str(kl1))

    sse = read(APPSSE_PY)
    rec("②", "AppSSE：_apply_rsi_full 只定义 1 处，且两条快照路径都调用",
        sse.count("def _apply_rsi_full(") == 1
        and sse.count("_apply_rsi_full(klines_out)") == 2,
        "def=%d call=%d" % (sse.count("def _apply_rsi_full("),
                            sse.count("_apply_rsi_full(klines_out)")))
    rec("②", "AppSSE：5 处预览 bar 字典都补了 'rsi': 0",
        sse.count("'rsi': 0") == 5, str(sse.count("'rsi': 0")))


# ══════════════════════════════════════════════════════════════════
# ③ chip 命中与双击拦截（源码契约）
# ══════════════════════════════════════════════════════════════════
def test_chip_and_dblclick():
    print("\n③ chip 命中与双击拦截（源码契约）")
    js = read(APP_JS)

    rec("③", "chip 命中测试抽成了纯函数（便于护栏与真渲染按坐标点）",
        "function getBottomSlotChipRect(i) {" in js
        and "function hitBottomSlotChip(x, y) {" in js,
        "chipRect=%d hit=%d" % (js.count("function getBottomSlotChipRect"),
                                js.count("function hitBottomSlotChip")))
    chip = fn_src(js, "getBottomSlotChipRect")
    rec("③", "getBottomSlotChipRect 只依赖几何与文本宽度（不读鼠标 / 不读点击状态）",
        "mouse" not in chip.lower() and "click" not in chip.lower() and "_slotAt(i)" in chip,
        chip.splitlines()[0].strip())

    cyc = fn_src(js, "cycleBottomSlot")
    rec("③", "cycleBottomSlot 按 BOTTOM_ORDER 循环（macd → rsi → vol → macd）",
        "BOTTOM_ORDER.indexOf(_slotAt(i)) + 1" in cyc
        and "BOTTOM_ORDER.length" in cyc and "saveOverlaySettings()" in cyc, "")
    rec("③", "BOTTOM_ORDER 恰为 ['vol','macd','rsi']",
        "const BOTTOM_ORDER = ['vol', 'macd', 'rsi'];" in js, "")

    rec("③", "双击切换已废除：无 _showVolume / _subShowVolume 的翻转赋值",
        "_showVolume = !_showVolume" not in js
        and "_subShowVolume = !_subShowVolume" not in js
        and "_showVolume" not in js and "_subShowVolume" not in js, "")
    rec("③", "双击指标区只做命中拦截（单窗 + 双窗下窗各一处，各含 return）",
        js.count("const bottomTop = getBottomSlotLabelArea(0).y;") == 2
        and js.count("const bottomBottom = getBottomAreaBottomY();") == 2
        and js.count("                    clickY >= bottomTop && clickY <= bottomBottom) {\n"
                     "                    return;") == 1
        and js.count("                        clickY >= bottomTop && clickY <= bottomBottom) {\n"
                     "                        return;") == 1,
        "bottomTop=%d bottomBottom=%d" % (
            js.count("const bottomTop = getBottomSlotLabelArea(0).y;"),
            js.count("const bottomBottom = getBottomAreaBottomY();")))

    # dblclick 处理器里不得再出现切换调用
    bad_dbl = []
    for m in re.finditer(r'\.addEventListener\("dblclick", function\(e\) \{', js):
        b = js.index("{", m.end() - 1)
        body = strip_comments(js[m.start():_to_matching_brace(js, b)])
        if "cycleBottomSlot" in body or "_setSlotAt" in body or "_bottomSlots" in body:
            bad_dbl.append(js.count("\n", 0, m.start()) + 1)
    rec("③", "两块 dblclick 处理器都不再切换槽位", not bad_dbl, str(bad_dbl))

    rec("③", "chip 单击处理器用 click.detail > 1 忽略双击的第二次点击（不连切两位）",
        js.count("if (e.detail > 1) return;") == 2, str(js.count("if (e.detail > 1) return;")))
    rec("③", "chip 单击只经 canvas / subCanvas 的 click 订阅（各 1 处）",
        js.count('canvas.addEventListener("click", function(e) {') == 1
        and js.count('subCanvas.addEventListener("click", function(e) {') == 1, "")

    # 旧几何函数名不得回潮（定义级）
    rec("③", "旧几何函数 getVolArea / getMacdTextArea 的定义已移除",
        "function getVolArea(" not in js and "function getMacdTextArea(" not in js, "")


# ══════════════════════════════════════════════════════════════════
# ④ 翻转视图一致性
# ══════════════════════════════════════════════════════════════════
RSI_Y_DRIVER = r"""
let _isMirrorMode = false;
%s
const area = { x: 10, y: 100, w: 500, h: 200 };
const range = getRsiRange();
const vals = [0, 30, 50, 70, 100];
const out = { normal: {}, mirror: {}, range: range,
              midPoint: area.y + area.h / 2 };
_isMirrorMode = false;
vals.forEach(v => { out.normal[v] = rsiToY(v, area, range); });
_isMirrorMode = true;
vals.forEach(v => { out.mirror[v] = rsiToY(v, area, range); });
out.symSumExpect = 2 * area.y + area.h;
process.stdout.write(JSON.stringify(out));
"""


def test_mirror():
    print("\n④ 翻转视图一致性（node 抽 rsiToY 真执行 + 源码反锚点）")
    js = read(APP_JS)
    drv = RSI_Y_DRIVER % "\n".join([fn_src(js, "getRsiRange"), fn_src(js, "rsiToY")])
    d = run_node(drv, "④")
    if d is None:
        return

    rec("④", "RSI 值域固定 {min:0, max:100}（不从数据推；这是注册表带 range() 钩子的原因）",
        d["range"] == {"min": 0, "max": 100}, str(d["range"]))
    sym = all(abs(d["normal"][str(v)] + d["mirror"][str(v)] - d["symSumExpect"]) < 1e-9
              for v in (0, 30, 50, 70, 100))
    rec("④", "镜像不变式：rsiToY(v) 翻转前后之和 ≡ 2×area.y + area.h（对任意 v 成立）", sym,
        "expect=%.1f got=%s" % (
            d["symSumExpect"],
            {v: round(d["normal"][str(v)] + d["mirror"][str(v)], 3)
             for v in (0, 30, 50, 70, 100)}))
    rec("④", "50 是翻转不动点（前后同位、且恰在绘图窗正中）",
        abs(d["normal"]["50"] - d["mirror"]["50"]) < 1e-9
        and abs(d["normal"]["50"] - d["midPoint"]) < 1e-9,
        "normal=%.4f mirror=%.4f mid=%.4f" % (
            d["normal"]["50"], d["mirror"]["50"], d["midPoint"]))
    rec("④", "70 / 30 在翻转下互换（rsiToY(70) 翻后 == rsiToY(30) 翻前，反之亦然）",
        abs(d["mirror"]["70"] - d["normal"]["30"]) < 1e-9
        and abs(d["mirror"]["30"] - d["normal"]["70"]) < 1e-9,
        "mirror70=%.3f normal30=%.3f | mirror30=%.3f normal70=%.3f" % (
            d["mirror"]["70"], d["normal"]["30"],
            d["mirror"]["30"], d["normal"]["70"]))
    rec("④", "0 / 100 是值域两端（翻转后互换到另一侧，不与中位混淆）",
        abs(d["normal"]["100"] - (d["midPoint"] - 100)) < 1e-9
        and abs(d["normal"]["0"] - (d["midPoint"] + 100)) < 1e-9,
        "rsiToY(100)=%.1f rsiToY(0)=%.1f" % (d["normal"]["100"], d["normal"]["0"]))

    # ── 源码反锚点：三条水平线必须同源、不得硬编码比例、不得沿用 zeroY ──
    draw = fn_src(js, "drawRsi")
    axis = fn_src(js, "drawRsiAxis")
    ratio_hits = re.findall(r"area\.h\s*\*\s*0?\.\d+", draw + axis)
    rec("④", "drawRsi / drawRsiAxis 里无 area.h * 0.3 / * 0.7 / * 0.5 之类硬编码比例",
        not ratio_hits, str(ratio_hits))
    rec("④", "drawRsi 的三条水平线全部走 rsiToY(v)（30/70 与 50 各 1 次以上）",
        draw.count("rsiToY(") >= 3 and "[70, 30].forEach" in draw,
        "rsiToY 调用 %d 次" % draw.count("rsiToY("))
    rec("④", "drawRsiAxis 三档的 y 全部走 rsiToY（不是手写 topVal / botVal 位置）",
        axis.count("rsiToY(") == 3, "rsiToY 调用 %d 次" % axis.count("rsiToY("))
    rec("④", "drawRsiAxis 中间档恒为字符串 \"50\"，且不沿用 MACD 的 zeroY 命名",
        '"50"' in axis and "zeroY" not in axis and "50" in axis,
        axis.replace("\n", " ")[:150])
    rec("④", "drawRsiAxis 的上下档取 range 两端（翻转时换位，不是常量 100/0 写死）",
        "const topVal = _isMirrorMode ? range.min : range.max;" in axis
        and "const botVal = _isMirrorMode ? range.max : range.min;" in axis, "")
    rec("④", "RSI 分支不做颜色对调（只翻 Y 位置、不改颜色）：无涨跌色 / COLORS.up|down",
        all(k not in draw + axis for k in ("#FF3C3C", "#00F0F0", "COLORS.up", "COLORS.down")), "")
    rec("④", "RSI 折线为单色 COLORS.rsi（与红绿涨跌体系不冲突）",
        "COLORS.rsi" in draw and "setLineDash" in draw, "")


# ══════════════════════════════════════════════════════════════════
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 64)
    print("底部指标区「单窗双槽位 + RSI」改造护栏")
    print("=" * 64)
    test_geometry()
    test_rsi_numeric()
    test_chip_and_dblclick()
    test_mirror()
    print("-" * 64)
    if _failed:
        for f in _failed:
            print("[FAIL] " + f)
        print("===== 失败 =====（%d 项）" % len(_failed))
        return 1
    print("===== 全部通过（%d 项）=====" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

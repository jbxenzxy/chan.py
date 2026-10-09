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
     · rsiToY(v) 翻转前后之和 ≡ 2×area.y + area.h —— 对**任意**值域成立（不再只对 [0,100]）
     · 值域**中点**是翻转不动点且恰在绘图窗正中；值域关于 50 对称时 80 / 20 互换
     · drawRsi / drawRsiAxis 的所有 y 都走 rsiToY(v)，**禁**硬编码相对比例
       （area.h * 0.2 / * 0.8 / * 0.5 之类）
     · drawRsiAxis 中间档恒 "50"（不是 MACD 的零线 "0"），且不沿用 zeroY 命名
     · RSI 分支不做颜色对调（只翻 Y 位置、不改颜色）
  ⑤ RSI 观感契约（颜色 / 参考线 / chip / 标签行分割线 / 点虚线）
     · 曲线色 ≡ MACD 白线色（COLORS.rsi == COLORS.dif）；标签数值用 COLORS.dif
     · 三条参考线 = [50, 80, 20]，统一「细点虚线」[1, 3]（不再各自 setLineDash）
     · chip 短名 'RSI'（标签行仍带参数 RSI(12):，与 MACD 槽「chip=MACD / 标签带参数」同构）
     · 槽 1..n-1 的标签行上沿补分割线（槽 0 那条已由主图末条网格线给出，不叠画两遍）
  ⑥ RSI 值域自适应（node 真执行 getRsiRange + 源码锚点）
     · 值域 = 可见窗口 rsi 的 min/max ± 5%（随放大/滚动而变，不再固定 [0,100]）
     · 空窗 / 无 rsi 字段 / 全缺值 → 回落 [0,100]；整窗恒等 → 不塌陷；缺值点被跳过
     · 量化证明：窗口收窄到波动极小的区间，占屏比从 span/100 升到 span/(span×1.1)

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
// 三组值域：两组关于 50 对称（0~100 / 20~80）、一组窄值域且不对称（52~56）。
// 镜像不变式必须对**任意**值域成立——值域已改为随可见窗口自适应，不再是固定 [0,100]。
const cases = [
  { key: '0|100',  min: 0,  max: 100, vals: [0, 20, 50, 80, 100] },
  { key: '20|80',  min: 20, max: 80,  vals: [20, 35, 50, 65, 80] },
  { key: '52|56',  min: 52, max: 56,  vals: [52, 53, 54, 55, 56] },
];
const out = { normal: {}, mirror: {}, mid: {},
              midPoint: area.y + area.h / 2,
              symSumExpect: 2 * area.y + area.h };
cases.forEach(rc => {
  const range = { min: rc.min, max: rc.max };
  out.mid[rc.key] = (rc.min + rc.max) / 2;
  _isMirrorMode = false;
  rc.vals.forEach(v => { out.normal[rc.key + '|' + v] = rsiToY(v, area, range); });
  _isMirrorMode = true;
  rc.vals.forEach(v => { out.mirror[rc.key + '|' + v] = rsiToY(v, area, range); });
});
process.stdout.write(JSON.stringify(out));
"""



def test_mirror():
    print("\n④ 翻转视图一致性（node 抽 rsiToY 真执行 + 源码反锚点）")
    js = read(APP_JS)
    drv = RSI_Y_DRIVER % "\n".join([fn_src(js, "rsiToY")])
    d = run_node(drv, "④")
    if d is None:
        return

    keys = sorted(d["normal"].keys())
    sym = all(abs(d["normal"][k] + d["mirror"][k] - d["symSumExpect"]) < 1e-9
              for k in keys)
    rec("④", "镜像不变式：rsiToY(v) 翻转前后之和 ≡ 2×area.y + area.h"
        "（对 3 组值域共 %d 个组合成立，不再只对固定 [0,100]）" % len(keys), sym,
        "expect=%.1f worst=%.3e" % (d["symSumExpect"],
        max(abs(d["normal"][k] + d["mirror"][k] - d["symSumExpect"]) for k in keys)))

    midok, middetail = True, []
    for key, mid in sorted(d["mid"].items()):
        n = d["normal"][key + "|" + str(mid)]
        m = d["mirror"][key + "|" + str(mid)]
        middetail.append("%s->%.3f" % (key, n))
        if abs(n - d["midPoint"]) > 1e-9 or abs(m - d["midPoint"]) > 1e-9:
            midok = False
    rec("④", "值域中点是翻转不动点，且恰在绘图窗正中（对任意值域成立，不只是对称值域）",
        midok, "mid=%.1f got=%s" % (d["midPoint"], middetail))

    swap = all(abs(d["mirror"]["20|80|" + str(a)] - d["normal"]["20|80|" + str(b)]) < 1e-9
               for a, b in ((80, 20), (65, 35)))
    rec("④", "值域关于 50 对称时，80 / 20（及 65 / 35）在翻转下互换", swap,
        "m80=%.3f n20=%.3f | m65=%.3f n35=%.3f" % (d["mirror"]["20|80|80"],
        d["normal"]["20|80|20"], d["mirror"]["20|80|65"], d["normal"]["20|80|35"]))

    narrow = d["normal"]["52|56|54"]
    rec("④", "窄值域（52~56，跨度为固定 [0,100] 的 1/25）下镜像与不动点同样成立",
        abs(narrow - d["midPoint"]) < 1e-9, "rsiToY(54)=%.3f mid=%.3f" % (narrow, d["midPoint"]))

    # ── 源码反锚点：三条水平线必须同源、不得硬编码比例、不得沿用 zeroY ──
    draw = fn_src(js, "drawRsi")
    axis = fn_src(js, "drawRsiAxis")
    ratio_hits = re.findall(r"area\.h\s*\*\s*0?\.\d+", draw + axis)
    rec("④", "drawRsi / drawRsiAxis 里无 area.h * 0.2 / * 0.8 / * 0.5 之类硬编码比例",
        not ratio_hits, str(ratio_hits))

    loop = re.search(r"RSI_REF_VALUES\.forEach\(v => \{(.*?)\n            \}\);",
                     draw, re.S)
    rec("④", "三条参考线画在同一个 forEach(RSI_REF_VALUES) 里，且每条的 y 都走 rsiToY(v)",
        bool(loop) and "rsiToY(v, area, range)" in loop.group(1)
        and "moveTo(area.x, y)" in loop.group(1)
        and "[70, 30]" not in draw,
        "loop=%s rsiToY=%d" % (bool(loop), draw.count("rsiToY(")))
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
# ⑤ 观感契约 + ⑥ 值域自适应
# ══════════════════════════════════════════════════════════════════
RSI_RANGE_DRIVER = r"""
%s
%s
function _win(n, amp) {
  return Array.from({ length: n }, (_, i) => ({ rsi: 50 + amp * Math.sin(i / 15) }));
}
const full = _win(300, 30);
const wA = full.slice(0, 40);      // 正弦上升段
const wB = full.slice(40, 80);     // 正弦下降段
const peak = full.slice(22, 28);   // 正弦峰顶附近 6 根：真实波动不到 1 点（旧口径下就是直线）
const out = {
  empty:    getRsiRange([]),
  noField:  getRsiRange([{ close: 1 }, { close: 2 }]),
  allNull:  getRsiRange([{ rsi: null }, { rsi: undefined }]),
  flat:     getRsiRange([50, 50, 50, 50].map(v => ({ rsi: v }))),
  trend:    getRsiRange([40, 45, 50, 55, 60].map(v => ({ rsi: v }))),
  withNull: getRsiRange([{ rsi: null }, { rsi: 30 }, { rsi: 70 }]),
  winA:     getRsiRange(wA),
  winB:     getRsiRange(wB),
  peak:     getRsiRange(peak),
  // ⑷ 边界窗口：RSI 的自然定义域是 [0,100]，留白不得把刻度推到域外
  userCase: getRsiRange([{ rsi: 0 }, { rsi: 95.229 }]),   // 上证指数日K 实测那对（0 与 95.229）
  topEdge:  getRsiRange([{ rsi: 60 }, { rsi: 100 }]),
  botEdge:  getRsiRange([{ rsi: 40 }, { rsi: 0 }]),
  bothEdge: getRsiRange([{ rsi: 0 }, { rsi: 100 }]),
};
// 夹回之前的旧口径（同一窗口），用来钉住用户报的那两个数字从哪来
const _span = 95.229 - 0;
out.userCaseOld = { min: 0 - _span * 0.05, max: 95.229 + _span * 0.05 };
out.peakSpan = Math.max.apply(null, peak.map(k => k.rsi))
             - Math.min.apply(null, peak.map(k => k.rsi));
out.peakFillOld = out.peakSpan / 100;                        // 旧口径：固定 [0,100] 的占屏比
out.peakFillNew = out.peakSpan / (out.peak.max - out.peak.min);   // 新口径：自适应值域
process.stdout.write(JSON.stringify(out));
"""


def test_rsi_style_and_range():
    print("\n⑤ RSI 观感契约（颜色 / 参考线 / chip / 分割线 / 点虚线）")
    js = read(APP_JS)

    # ① 曲线色 = MACD 白线色；标签数值同色
    dif = re.search(r"\bdif:\s*\"([^\"]+)\"", js)
    rsi = re.search(r"\brsi:\s*\"([^\"]+)\"", js)
    rec("①", "RSI 曲线色 ≡ MACD 白线色（COLORS.rsi 与 COLORS.dif 取同一色值）",
        bool(dif and rsi and dif.group(1).upper() == rsi.group(1).upper()),
        "dif=%s rsi=%s" % (dif.group(1) if dif else None, rsi.group(1) if rsi else None))
    lbl = fn_src(js, "drawRsiSlotLabel")
    rec("①", "标签数值用 MACD DIF 的白（COLORS.dif），不再用 COLORS.rsi",
        "ctx.fillStyle = COLORS.dif;" in lbl and "COLORS.rsi" not in lbl, "")

    # ② 参考线 50 / 80 / 20
    m = re.search(r"const RSI_REF_VALUES = \[([^\]]*)\];", js)
    got = [int(x) for x in m.group(1).split(",")] if m else []
    rec("②", "参考线取值 = [50, 80, 20]（中轴 + 超买 80 + 超卖 20，与通达信常见 RSI 副图一致）",
        got == [50, 80, 20], str(got))
    draw = strip_comments(fn_src(js, "drawRsi"))
    rec("②", "旧的 30 / 70 两线硬编码已清零，三条线统一从 RSI_REF_VALUES 取",
        "[70, 30]" not in draw and "[30, 70]" not in draw and "RSI_REF_VALUES" in draw, "")

    # ⑤ 三条线统一「细点虚线」
    m = re.search(r"const RSI_REF_DASH = \[([^\]]*)\];", js)
    dv = [int(x) for x in m.group(1).split(",")] if m else []
    rec("⑤", "三条线统一「细点虚线」：1px 点 + 3px 空隙（RSI_REF_DASH = [1, 3]）",
        dv == [1, 3], str(dv))
    rec("⑤", "参考线与中轴共用同一条线型（不再各自 setLineDash；旧 [4, 4] 已清零）",
        draw.count("setLineDash(RSI_REF_DASH)") == 1 and "[4, 4]" not in draw
        and "const midY" not in draw,
        "dash=%d midY=%s" % (draw.count("setLineDash(RSI_REF_DASH)"),
                             "const midY" in draw))
    rec("⑤", "50 中轴也走虚线（三条线全部点虚线，只靠明度区分：中轴 0.2 / 超买超卖 0.15）",
        "rgba(255,255,255,0.2)" in draw and "rgba(255,255,255,0.15)" in draw, "")
    rec("⑥", "参考线越界不画（值域自适应后 20 / 80 可能整体落在窗外）",
        "if (v < range.min || v > range.max) return;" in draw, "")

    # ③ chip 短名
    rec("③", "chip 短名为 RSI（去掉参数），标签行仍带参数 RSI(12):（与 MACD「chip=MACD /"
        " 标签=MACD(12,26,9)」同构）",
        "tabLabel: () => 'RSI'," in js and "tabLabel: () => 'RSI(12)'" not in js
        and 'ctx.fillText("RSI(12):"' in lbl, "")

    # ④ 槽标签行上沿分割线
    div = fn_src(js, "drawBottomSlotDivider")
    rec("④", "新增 drawBottomSlotDivider(i)：画在 getBottomSlotLabelArea(i).y，颜色同网格线",
        "getBottomSlotLabelArea(i)" in div and "COLORS.grid" in div
        and ".y" in div, div.replace("\n", " ")[:120])
    mloop = re.search(r"for \(let si = 1; si < slotCount; si\+\+\) \{[^}]*"
                      r"drawBottomSlotDivider\(si\);", js)
    rec("④", "只为 i ≥ 1 的槽补分割线（槽 0 那条已由主图末条网格线给出，避免叠画变亮）",
        bool(mloop) and js.count("drawBottomSlotDivider(si)") == 1,
        "calls=%d" % js.count("drawBottomSlotDivider(si)"))
    rec("④", "分割线在 _renderChart 的槽位绘制段内（底部区单独重绘时也走同一条路径）",
        bool(re.search(r"drawBottomSlotDivider\(si\);.*?drawBottomSlotLabel\(si, bottomCtx\);",
                       js, re.S)), "")

    print("\n⑦ 标签行排版（chip 靠右 / 无边框 / 与正文同字号同字重同基线）")
    chip_fn = strip_comments(fn_src(js, "getBottomSlotChipRect"))
    rec("⑦", "chip 靠标签行**右端**：x 由 label.x + label.w 反推（不再钉在最左）",
        bool(re.search(r"x:\s*label\.x \+ label\.w - \d+(\.\d+)? - \(tw \+ CHIP_PAD_X \* 2\)",
                       chip_fn))
        and "x: label.x + 2" not in chip_fn,
        chip_fn.splitlines()[-1].strip())
    chip_draw = strip_comments(fn_src(js, "drawBottomSlotChip"))
    rec("⑦", "chip 形态 = 文字 + 底色，**无边框**（fillRect 在，strokeRect / strokeStyle 不在）",
        "fillRect(chip.x" in chip_draw and "strokeRect" not in chip_draw
        and "strokeStyle" not in chip_draw, "")
    row = strip_comments(fn_src(js, "drawBottomSlotLabel"))
    _tx = re.search(r"c\.textX = (\w+)\.x \+ (\d+);", row)
    # ⚠ 只断言「字符串里有这句话」是不够的：P27 曾写成 c.textX = label.x + 4，
    #   而该函数的局部名是 textArea ⇒ 静态断言全绿、真渲染 ReferenceError 整图崩掉。
    #   故这里连「引用的标识符在函数内声明过」一起钉住。
    rec("⑦", "标签行正文自**左端**起画，且引用的局部几何变量确实在函数内声明过",
        bool(_tx) and ("const %s = " % _tx.group(1)) in row
        and "chip.x + chip.w" not in js,
        (_tx.group(0) if _tx else "未匹配") + " | 声明=%s" % (
            ("const %s = " % _tx.group(1)) in row if _tx else None))
    fns = {n: strip_comments(fn_src(js, n)) for n in
           ("drawBottomSlotChip", "drawVolSlotLabel", "drawMacdSlotLabel", "drawRsiSlotLabel")}
    fonts = set()
    for _src in fns.values():
        fonts.update(re.findall(r'ctx\.font = "([^"]+)"', _src))
    rec("⑦", "chip 与三个指标的正文共用同一字体串（11px monospace，且都不含 bold）",
        fonts == {"11px monospace"} and all("bold" not in v for v in fns.values()),
        str(sorted(fonts)))
    _mth = int(re.search(r"const MACD_TEXT_HEIGHT = (\d+);", js).group(1))
    _chiph = re.search(r"const CHIP_H = (.+?);", js).group(1).strip()
    _cy = int(re.search(r"y: label\.y \+ (\d+),", chip_fn).group(1))
    _ch = int(re.search(r"h: CHIP_H - (\d+),", chip_fn).group(1))
    _cb = int(re.search(r"chip\.y \+ chip\.h - (\d+)\)", fns["drawBottomSlotChip"]).group(1))
    _the = [re.search(r"const lineY = textArea\.y \+ (\d+);", fns[n]).group(1)
            for n in ("drawVolSlotLabel", "drawMacdSlotLabel", "drawRsiSlotLabel")]
    _base = _cy + (_mth - _ch) - _cb
    rec("⑦", "chip 文字与标签行正文**基线同值**（%d ≡ %s）：不是靠调视力凑的"
        % (_base, _the[0]),
        _chiph == "MACD_TEXT_HEIGHT" and len(set(_the)) == 1 and _base == int(_the[0]),
        "chip=%d 正文=%s（三个指标必须同值）" % (_base, _the))
    vol_fn = strip_comments(fn_src(js, "drawVolSlotLabel"))
    rec("⑦", "类MACD 不再在右上角重复画「成交额 / 成交量」（chip 已标识品种口径）",
        "textAlign = \"right\"" not in vol_fn
        and "textArea.x + textArea.w - 4" not in js
        and "isFuturesMode() ? '成交量' : '成交额'" in js, "")

    print("\n⑥ RSI 值域自适应（node 真执行 getRsiRange）")
    drv = RSI_RANGE_DRIVER % (fn_src(js, "rsiOf"), fn_src(js, "getRsiRange"))   # 两个 %s ⇒ 必须给元组
    d = run_node(drv, "⑥")
    if d is None:
        return

    rec("⑥", "空窗口 / 无 rsi 字段 / 全为缺值 → 回落 {min:0,max:100}（不塌陷、不 NaN）",
        d["empty"] == {"min": 0, "max": 100}
        and d["noField"] == {"min": 0, "max": 100}
        and d["allNull"] == {"min": 0, "max": 100},
        "empty=%s noField=%s allNull=%s" % (d["empty"], d["noField"], d["allNull"]))
    rec("⑥", "值域 = 可见窗口 rsi 的 min / max 各留 5% 余量（与 getPriceRange 同口径）",
        abs(d["trend"]["min"] - 39.0) < 1e-9 and abs(d["trend"]["max"] - 61.0) < 1e-9,
        str(d["trend"]))
    rec("⑥", "缺值点被跳过，不参与 min / max",
        abs(d["withNull"]["min"] - 28.0) < 1e-9 and abs(d["withNull"]["max"] - 72.0) < 1e-9,
        str(d["withNull"]))
    rec("⑥", "整窗恒等（含恒 50 的平线）→ 窗口不塌陷（span > 0 且含该值）",
        d["flat"]["max"] - d["flat"]["min"] > 0
        and d["flat"]["min"] <= 50 <= d["flat"]["max"], str(d["flat"]))
    rec("⑥", "值域随可见窗口 **变化**（缩放 / 滚动即变；固定 [0,100] 时两者恒等）",
        d["winA"] != d["winB"]
        and (d["winA"]["max"] - d["winA"]["min"]) < 100
        and (d["winB"]["max"] - d["winB"]["min"]) < 100,
        "A=%s B=%s" % (d["winA"], d["winB"]))
    rec("⑥", "放大到波动极小的区间：RSI 占屏比由 %.1f%% 升到 %.1f%%（不再被压成直线）"
        % (d["peakFillOld"] * 100, d["peakFillNew"] * 100),
        d["peakFillNew"] > 0.85 and d["peakFillNew"] > 50 * d["peakFillOld"],
        "该窗真实波动 %.3f 点：旧占屏 %.4f / 新占屏 %.4f（%.0f×）"
        % (d["peakSpan"], d["peakFillOld"], d["peakFillNew"],
           d["peakFillNew"] / max(d["peakFillOld"], 1e-12)))
    # ★ ⑷ 夹回 [0,100]：用户实测（上证指数日K）纵轴印出 99.99 / −4.76 —— 负值来自
    #   留白（min − 0.05Δ），不是 RSI 本身。把同一窗口的旧/新口径都钉住。
    _old_lo = "%.2f" % d["userCaseOld"]["min"]
    _old_hi = "%.2f" % d["userCaseOld"]["max"]
    rec("④", "用户报的两个数确系留白所致：同一窗口旧口径印 %s / %s，夹回后下沿为 0.00"
        % (_old_hi, _old_lo),
        _old_lo == "-4.76" and _old_hi == "99.99"
        and abs(d["userCase"]["min"]) < 1e-12
        and abs(d["userCase"]["max"] - 99.99045) < 1e-6,
        "旧=[%s, %s] 新=%s" % (_old_lo, _old_hi, d["userCase"]))
    rec("④", "值域夹回指标定义域 [0,100]：下沿不出现负值、上沿不超过 100",
        d["topEdge"]["max"] == 100 and d["botEdge"]["min"] == 0
        and d["bothEdge"] == {"min": 0, "max": 100},
        "topEdge=%s botEdge=%s bothEdge=%s"
        % (d["topEdge"], d["botEdge"], d["bothEdge"]))
    _wins = ["empty", "noField", "allNull", "flat", "trend", "withNull",
             "winA", "winB", "peak", "userCase", "topEdge", "botEdge", "bothEdge"]
    rec("④", "任意窗口都不变量：0 ≤ min < max ≤ 100（有界量 + 不塌陷）",
        all(0 <= d[w]["min"] < d[w]["max"] <= 100 for w in _wins),
        str({w: (d[w]["min"], d[w]["max"]) for w in _wins}))


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
    test_rsi_style_and_range()
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

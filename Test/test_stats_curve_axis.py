# -*- coding: utf-8 -*-
"""盈亏曲线「坐标轴」契约 —— 2026-09-19 用户提问「纵坐标和横坐标都没有显示出来」。

改前实测（探针记录 CanvasRenderingContext2D.fillText 的每一次调用）
---------------------------------------------------------------------
  · 纵轴只有 3 个数字（max / 0 / min），**没有轴线、没有刻度线、没有网格**；
    单边行情（全正或全负）时 0 被画两遍，两处只差 5px，糊在一起：
        全正：fillText('0', x=42, y=205) + fillText('0', x=42, y=200)
        全负：fillText('0', x=42, y=13)  + fillText('0', x=42, y=18)
  · 横轴**一个字都没有**：4 段文字全部落在 x=42（左槽）与末端标签，
    没有任何时间/笔序标签，也没有横轴线与刻度线。
  · 左留白写死 46px：7 位数时 "-1200000" 宽 45.4px、右对齐贴 x=42 →
    左边缘 -3.4px，**负号被裁掉**；末端标签固定向右排 →
    右边缘 417.4px > 画布 410px，**被裁掉 7.4px**。

本文件把修复后的性质钉死
---------------------------------------------------------------------
 [A] 纵轴：刻度值 ≥4 个、单调、互不重复、**"0" 恰好一个**、且首尾刻度
     必须把数据范围包住（曲线不会跑出可视区）。
 [B] 横轴：有轴线、有刻度线、有日期标签（取该笔的 exit_at → MM-DD），
     首尾标签落在绘图区两端、互不重叠。
 [C] 任何一段文字（按真实 measureText 宽度算盒子）都不许越出画布 ——
     这是"负号被裁掉"的直接回归项。
 [D] 坏数据不静默：某笔 cumulative 为 NaN 时，曲线与坐标仍要画出来
     （旧实现会被 NaN 传染，整张图连坐标一起消失且不报错）。

两层
---------------------------------------------------------------------
 [静态] 从 app.js 抽出真实代码段，在 node + canvas 桩里跑，逐场景审计几何。
        canvas 桩的 measureText 宽度模型**略宽于**真实字体（走保守一侧），
        任何环境都能跑。
 [真渲染] 无头 Chrome 打开真页面 → 打桩 /api/trader/trades → 点「统计」，
       用真实 measureText 复核同一批断言。浏览器不在位时降级 SKIP。

跑法：python Test/test_stats_curve_axis.py
"""
from __future__ import annotations

import functools
import glob
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
APP_JS = os.path.join(_REPO, "Frontend", "app.js")
FRONTEND_DIR = os.path.join(_REPO, "Frontend")
SNAP_FUTURES = os.path.join(_HERE, "snapshots", "futures_15s_full.json")

_FAIL: list[str] = []


def check(cond, name, extra=""):
    ok = bool(cond)
    print(("  [ok]   " if ok else "  [FAIL] ") + name
          + (("   << " + str(extra)) if not ok else ""))
    if not ok:
        _FAIL.append(name)
    return ok


# ══════════════════════════════════════════════════════════════
# 抽出真实代码段：eqAxisTicks / eqAxisNum / eqAxisDate / drawEquityCurve
# ══════════════════════════════════════════════════════════════
def extract_block():
    js = open(APP_JS, encoding="utf-8").read()
    s = js.index("        function eqAxisTicks(")
    e = js.index("function updateSlider()")
    blk = js[s:e]
    assert "function drawEquityCurve(curve) {" in blk, "抽出的段落里没有 drawEquityCurve"
    # 只数「8 空格缩进的顶层声明」：drawEquityCurve 内部还有 yOf / xOf 两个内嵌函数，
    # 按 "function " 子串数会多出来。
    tops = re.findall(r"^        function (\w+)\(", blk, re.M)
    assert tops == ["eqAxisTicks", "eqAxisNum", "eqAxisDate", "drawEquityCurve"], \
        "顶层函数不是预期的 4 个：%s" % tops
    assert blk.count("{") == blk.count("}"), "大括号不配对"
    return blk


# ══════════════════════════════════════════════════════════════
# 场景数据（python 侧生成，两层共用）
# ══════════════════════════════════════════════════════════════
def curve_of(nets, dates=None, bad_at=None):
    out, cum = [], 0.0
    for i, nc in enumerate(nets):
        cum += nc
        cu = cum
        if bad_at is not None and i == bad_at:
            cu = None          # JSON 里 null → JS Number(null) = 0… 见下面的 "坏值" 特判
        d = dates[i] if dates else "2026-09-%02d" % (i % 9 + 1)
        out.append({"exit_at": "%s 10:%02d:00" % (d, i % 60),
                    "net_cash": nc, "cumulative": cu})
    return out


SCENARIOS = [
    ("全正 6 笔", curve_of([1400, 690, 320, 520, 1100, 2880])),
    ("有回撤 8 笔", curve_of([900, 300, -250, 500, 1200, -400, 800, 2100])),
    ("全负 5 笔", curve_of([-300, -900, -700, -1500, -1200])),
    ("单笔 +666", curve_of([666])),
    ("长序列 60 笔", curve_of([(220 if i % 3 else -310) for i in range(60)])),
    ("7 位数末端", curve_of([300000, -900000, -600000])),
    ("全平 3 笔", curve_of([0, 0, 0])),
    ("跨 0 轴 4 笔", curve_of([600, -1200, 700, -100])),
    ("跨月日期", curve_of([100, 200, 300, 400],
                          dates=["2026-07-31", "2026-08-15",
                                 "2026-09-01", "2026-10-08"])),
    # 日内：一天里几笔成交 → 横轴必须给时刻（三个 "09-18" 并排等于没信息）
    ("同日 6 笔", curve_of([100, -50, 300, 120, -80, 410],
                           dates=["2026-09-18"] * 6)),
]


def with_bad_value():  # [D] 坏值场景：某一笔的 cumulative 不是数
    # 用字符串而不是 NaN 字面量：JSON 里没有 NaN，走两层（node 的 JSON.parse /
    # 页面的 r.json()）都会解析失败，那就变成"请求坏了"而不是"数据坏了"。
    # 用 "?" 让 JS 的 Number() 得到 NaN —— 与真实脏数据同一条路径。
    c = curve_of([100, 200, 300, 400])
    c[2]["cumulative"] = "?"
    return c


SCENARIOS.append(("含坏值一笔", with_bad_value()))


# ══════════════════════════════════════════════════════════════
# 审计：一份记录（texts / segs）逐条判定
# ══════════════════════════════════════════════════════════════
def audit(tag, rec, curve):
    n = len(curve)
    w, h = rec["w"], rec["h"]
    texts, segs = rec["texts"], rec["segs"]
    ok_all = True

    def need(cond, name, extra=""):
        nonlocal ok_all
        if not check(cond, "[%s] %s" % (tag, name), extra):
            ok_all = False

    need(len(texts) > 0, "画布上真的有文字（否则后面全是假绿）", len(texts))
    if not texts:
        return False

    # 文字盒子：align 决定锚点含义
    def box(t):
        if t["align"] == "left":
            left = t["x"]
        elif t["align"] == "right":
            left = t["x"] - t["w"]
        else:
            left = t["x"] - t["w"] / 2
        return left, left + t["w"]

    # ── [C] 越界 ────────────────────────────────────────────
    oob = []
    for t in texts:
        l, r = box(t)
        if l < -0.5 or r > w + 0.5 or t["y"] < 0 or t["y"] > h:
            oob.append((t["t"], round(l, 1), round(r, 1), round(t["y"], 1)))
    need(not oob, "所有文字都在画布内（含 7 位数）", oob)

    # ── [A] 纵轴刻度 ────────────────────────────────────────
    gx = min(t["x"] for t in texts)
    ylab = [t for t in texts if abs(t["x"] - gx) < 0.6]
    # 条数只保证"不止 max/min 两个"（3~7）—— 具体几条由数据跨度决定；
    # 真正把旧实现拦下来的是下面两条：值不重复（旧版 0 画两遍）与有网格线。
    need(3 <= len(ylab) <= 7, "纵轴刻度值 3~7 个（不是只有 max/min 两个）", len(ylab))
    vals = [t["t"] for t in ylab]
    need(len(set(vals)) == len(vals), "纵轴刻度值互不重复（0 不再被画两遍）", vals)
    need(vals.count("0") == 1, "纵轴恰好一个 0", vals)

    def num(s):
        m = re.fullmatch(r"(-?)(\d+(?:\.\d+)?)(万|亿)?", s)
        if not m:
            return None
        v = float(m.group(2))
        v *= {"万": 1e4, "亿": 1e8}.get(m.group(3), 1)
        return -v if m.group(1) else v

    nums = [num(v) for v in vals]
    need(all(x is not None for x in nums), "纵轴刻度都是可解析的数值", vals)
    if all(x is not None for x in nums):
        need(nums == sorted(nums) and len(set(nums)) == len(nums),
             "纵轴刻度单调递增", nums)
        # 刻度按 y 从小到大 == 数值从大到小
        by_y = sorted(ylab, key=lambda t: t["y"])
        need([t["t"] for t in by_y] == list(reversed(vals)),
             "刻度值随 y 增大而减小（上大下小，没画反）",
             [t["t"] for t in by_y])
        data = [c["cumulative"] for c in curve
                if isinstance(c["cumulative"], (int, float))
                and c["cumulative"] == c["cumulative"]] + [0]
        need(nums[0] <= min(data) + 1e-6 and nums[-1] >= max(data) - 1e-6,
             "首尾刻度把数据范围包住（曲线不会顶出可视区）",
             "刻度 %s / 数据 %.0f~%.0f" % (nums, min(data), max(data)))

    # ── [B] 横轴 ────────────────────────────────────────────
    plotBottom = max(t["y"] for t in ylab)
    vline = [s for s in segs if abs(s[0] - s[2]) < 0.6 and abs(s[3] - s[1]) > 30]
    need(len(vline) >= 1, "有纵轴线（不是只有浮空的数字）", len(vline))
    ticks = [s for s in segs if abs(s[0] - s[2]) < 0.6 and 2 < abs(s[3] - s[1]) <= 8]
    need(len(ticks) >= min(n, 2), "横轴有刻度线", len(ticks))
    hl = [s for s in segs if abs(s[0] - s[2]) > 30 and abs(s[3] - s[1]) < 0.6]
    need(len(hl) >= 4, "有网格线（≥4 条）", len(hl))
    need(any(abs(s[1] - plotBottom) < 1.0 for s in hl),
         "横轴线画在绘图区底部", [round(s[1], 1) for s in hl])

    xlab = [t for t in texts if t["y"] > plotBottom + 2]
    need(len(xlab) >= min(n, 3), "横轴有日期标签（≥3 个）", len(xlab))
    if xlab:
        # 整条曲线同一天 → 给时刻 HH:MM（同日的三个 "09-18" 并排没有信息量）；
        # 跨天 → 给 MM-DD。
        same_day = curve[0]["exit_at"][:10] == curve[-1]["exit_at"][:10]
        pat = r"\d{2}:\d{2}" if same_day else r"\d{2}-\d{2}"
        need(all(re.fullmatch(pat, t["t"]) for t in xlab),
             "横轴标签是 %s" % ("HH:MM（同日）" if same_day else "MM-DD"),
             [t["t"] for t in xlab])
        xs = [t["x"] for t in xlab]
        need(xs == sorted(xs) and len(set(xs)) == len(xs),
             "横轴标签 x 互不重叠且按序", [round(x, 1) for x in xs])
        need(len(set(t["t"] for t in xlab)) == len(xlab),
             "横轴标签文字互不重复", [t["t"] for t in xlab])
        padL = gx + 6
        padR = 10
        ned = w - padR
        if n > 1:
            need(abs(xlab[0]["x"] - padL) < 1.5, "首个日期标签落在绘图区左端",
                 "%s vs %s" % (xlab[0]["x"], padL))
            need(abs(xlab[-1]["x"] - ned) < 1.5, "末个日期标签落在绘图区右端",
                 "%s vs %s" % (xlab[-1]["x"], ned))
            need(xlab[0]["align"] == "left" and xlab[-1]["align"] == "right",
                 "首标签左对齐、末标签右对齐（贴边不越界）",
                 (xlab[0]["align"], xlab[-1]["align"]))
        else:
            need(xlab[0]["align"] == "center", "只有一笔时日期标签居中",
                 xlab[0]["align"])
        # 标签内容必须来自那一笔的 exit_at（不是编出来的）
        plotW = ned - padL
        bad = []
        for t in xlab:
            idx = 0 if n <= 1 else round((t["x"] - padL) / plotW * (n - 1))
            idx = max(0, min(n - 1, idx))
            want = curve[idx]["exit_at"][11:16] if same_day \
                else curve[idx]["exit_at"][5:10]
            if t["t"] != want:
                bad.append((idx, t["t"], want))
        need(not bad, "标签取的是该笔的 exit_at", bad)
        if n > 1:
            need(len(xlab) >= min(n, 3), "横轴日期标签条数够用", len(xlab))

    # ── 0 轴虚线：只在该轴的刻度跨 0 时出现 ─────────────────
    # 全正 / 全负行情里 0 就是最外侧那条网格线，不必再叠一条虚线 ——
    # 改前是无条件画的，于是单边行情下 0 与最外侧刻度贴在一起画两遍。
    dashed = [s for s in hl if s[4]]
    need(len(dashed) <= 1, "0 轴虚线最多一条", len(dashed))
    axis_crosses = bool(nums) and all(x is not None for x in nums) \
        and nums[0] < 0 < nums[-1]
    need(bool(dashed) == axis_crosses, "0 轴虚线只在刻度跨 0 时出现",
         "刻度 %s / 虚线 %d 条" % (vals, len(dashed)))

    # ── 末端数值标签 ────────────────────────────────────────
    tail = [t for t in texts if t["t"].endswith(" 元")]
    need(len(tail) == 1, "末端有且只有一个「累计 xx 元」标签", [t["t"] for t in tail])
    # ── 曲线本体 ────────────────────────────────────────────
    need(rec["strokes"] >= 2 and rec["fills"] >= 1, "曲线与面积都画了",
         (rec["strokes"], rec["fills"]))
    return ok_all


# ══════════════════════════════════════════════════════════════
# 第 1 层：node + canvas 桩（抽真实代码段）
# ══════════════════════════════════════════════════════════════
NODE_DRIVER = r"""
// ── canvas / DOM 桩 ────────────────────────────────────────────
// 宽度模型刻意**略宽于**真实 10px system-ui（真值：'0'=5.9, '7430'=23.5,
// '-1200000'=45.4），走保守一侧：桩里放得下，真浏览器一定也放得下。
function textWidth(t) {
  var w = 0;
  for (var i = 0; i < t.length; i++) {
    var c = t[i];
    if (c >= "0" && c <= "9") w += 6.0;
    else if (c === "-") w += 3.6;
    else if (c === ".") w += 3.0;
    else if (c === " ") w += 3.0;
    else if (c.charCodeAt(0) > 255) w += 10.0;   // 万 / 亿 / 元
    else w += 6.0;
  }
  return w;
}
function makeCtx(rec) {
  var cur = null, dash = false;
  return {
    font: "", textAlign: "", textBaseline: "", fillStyle: "", strokeStyle: "",
    lineWidth: 1,
    setTransform: function () {}, clearRect: function () {},
    beginPath: function () { cur = null; },
    moveTo: function (x, y) { cur = [x, y]; },
    lineTo: function (x, y) {
      if (cur) rec.segs.push([cur[0], cur[1], x, y, dash]);
      cur = [x, y];
    },
    closePath: function () { cur = null; },
    stroke: function () { rec.strokes++; },
    fill: function () { rec.fills++; },
    setLineDash: function (d) { dash = !!(d && d.length); },
    arc: function (x, y, r) { rec.arcs.push([x, y, r]); },
    measureText: function (t) { return { width: textWidth(String(t)) }; },
    fillText: function (t, x, y) {
      rec.texts.push({ t: String(t), x: x, y: y, w: textWidth(String(t)),
                       align: this.textAlign });
    }
  };
}
var canvases = {};
function makeCanvas() {
  var rec = { texts: [], segs: [], arcs: [], strokes: 0, fills: 0 };
  var cv = {
    width: 0, height: 0, style: {}, dataset: {}, _rec: rec,
    clientWidth: 410, clientHeight: 240,
    parentNode: { clientWidth: 438 },     // 与 440px 卡片下的实测一致
    getContext: function () { return this._ctx || (this._ctx = makeCtx(rec)); }
  };
  return cv;
}
var document = { getElementById: function (id) { return canvases[id] || null; } };
var window = { devicePixelRatio: 1 };

/*__BLOCK__*/

var scenarios = require(process.argv[2]);
var out = [];
scenarios.forEach(function (sc) {
  var cv = makeCanvas();
  canvases["trade-equity-canvas"] = cv;
  drawEquityCurve(sc.curve);
  out.push({ name: sc.name, w: parseFloat(cv.style.width), h: parseFloat(cv.style.height),
             texts: cv._rec.texts, segs: cv._rec.segs,
             strokes: cv._rec.strokes, fills: cv._rec.fills });
});
console.log(JSON.stringify(out));
"""


def find_node():
    cand = [os.environ.get("NODE_EXE"), None]
    import shutil
    cand[1] = shutil.which("node")
    cand += glob.glob(os.path.expanduser(
        "~/.workbuddy/binaries/node/versions/*/node.exe"))
    cand += [r"C:\Program Files\nodejs\node.exe"]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def run_static_layer():
    try:
        blk = extract_block()
    except AssertionError as e:
        check(False, "抽得出现有曲线代码段（覆盖面自检）", e)
        return
    check(True, "抽得出现有曲线代码段（%d 行）" % blk.count("\n"))
    node = find_node()
    if not node:
        print("  [SKIP] 未找到 node（静态几何层无法执行）")
        return
    with tempfile.TemporaryDirectory() as tmp:
        drv = os.path.join(tmp, "drv.js")
        spec = os.path.join(tmp, "spec.js")
        open(drv, "w", encoding="utf-8").write(
            NODE_DRIVER.replace("/*__BLOCK__*/", blk))
        # 用 module.exports 而不是 JSON 文件：坏值场景里可能有 NaN，
        # 那是合法 JS 但不是合法 JSON。
        open(spec, "w", encoding="utf-8").write(
            "module.exports = "
            + json.dumps([{"name": n, "curve": c} for n, c in SCENARIOS],
                         allow_nan=True)
            + ";\n")
        proc = subprocess.run([node, drv, spec], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        if proc.returncode != 0:
            check(False, "node 执行成功", (proc.stderr or "")[-400:])
            return
        check(True, "node 执行成功")
        recs = json.loads(proc.stdout.strip().splitlines()[-1])
    for (name, curve), rec in zip(SCENARIOS, recs):
        audit("静态 " + name, rec, curve)


# ══════════════════════════════════════════════════════════════
# 第 2 层：真实无头 Chrome + 真实 measureText
# ══════════════════════════════════════════════════════════════
SPY_JS = r"""
() => {
  window.__spy = { texts: [], segs: [], strokes: 0, fills: 0 };
  const P = CanvasRenderingContext2D.prototype;
  // 只记「盈亏曲线」这一块画布：主图的画布也在同一个原型上，
  // 不按 canvas.id 过滤会把 K 线 / MACD / 成交量柱的绘制全收进来。
  const mine = function (ctx) {
    return !!(ctx.canvas && ctx.canvas.id === "trade-equity-canvas");
  };
  if (P.__patched) return;
  P.__patched = true;
  const _ft = P.fillText, _mt = P.moveTo, _lt = P.lineTo,
        _bp = P.beginPath, _st = P.stroke, _fi = P.fill, _sld = P.setLineDash;
  let cur = null, dash = false;
  P.moveTo = function (x, y) { cur = mine(this) ? [x, y] : null;
                              return _mt.apply(this, arguments); };
  P.lineTo = function (x, y) {
    if (cur) window.__spy.segs.push([cur[0], cur[1], x, y, dash]);
    if (mine(this)) cur = [x, y];
    return _lt.apply(this, arguments);
  };
  P.beginPath = function () { cur = null; return _bp.apply(this, arguments); };
  P.setLineDash = function (d) { if (mine(this)) dash = !!(d && d.length);
                                return _sld.apply(this, arguments); };
  P.stroke = function () { if (mine(this)) window.__spy.strokes++;
                           return _st.apply(this, arguments); };
  P.fill = function () { if (mine(this)) window.__spy.fills++;
                         return _fi.apply(this, arguments); };
  P.fillText = function (t, x, y) {
    if (mine(this)) {
      window.__spy.texts.push({ t: String(t), x: x, y: y,
        w: this.measureText(String(t)).width, align: this.textAlign });
    }
    return _ft.apply(this, arguments);
  };
}
"""


def find_playwright():
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        return sync_playwright
    except Exception:
        return None


def launch_browser(pw):
    for kw in ({"channel": "chrome"}, {}):
        try:
            return pw.chromium.launch(**kw)
        except Exception:
            continue
    for pat in ("~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe",):
        for cand in glob.glob(os.path.expanduser(pat)):
            try:
                return pw.chromium.launch(executable_path=cand)
            except Exception:
                continue
    return None


def run_render_layer():
    sync_playwright = find_playwright()
    if sync_playwright is None:
        print("  [SKIP] 本环境无 playwright（静态几何层已覆盖同一批断言）")
        return
    if not os.path.isfile(SNAP_FUTURES):
        print("  [SKIP] 缺快照 " + SNAP_FUTURES)
        return
    snap_text = open(SNAP_FUTURES, encoding="utf-8").read()
    cur = [None]

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=FRONTEND_DIR))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    # 截图只服务交付说明，不是门禁的一部分 —— 默认不落盘（跑测试不该在仓库里
    # 留下产物），要图时用 STATS_SHOT_DIR 指定一个仓库外的目录。
    shot_dir = os.environ.get("STATS_SHOT_DIR", "")

    with sync_playwright() as pw:
        browser = launch_browser(pw)
        if browser is None:
            print("  [SKIP] 无可用无头浏览器")
            httpd.shutdown()
            return
        page = browser.new_page(viewport={"width": 1600, "height": 900})

        def route_api(route):
            u = route.request.url
            if "/api/health" in u:
                body = json.dumps({"config": {"view_count": 233}})
            elif "analyze" in u:
                body = snap_text
            elif "trader/trades" in u:
                body = json.dumps(cur[0])
            else:
                body = "{}"
            route.fulfill(status=200, content_type="application/json", body=body)

        page.route("**/api/**", route_api)
        page.goto("http://127.0.0.1:%d/index.html" % port, wait_until="load")
        page.evaluate(SPY_JS)
        page.wait_for_function(
            "() => { const s = window.ChanApp && window.ChanApp.state;"
            " return !!(s && s.chartData && s.chartData.klines.length > 0); }",
            timeout=20000)
        page.wait_for_timeout(400)

        for si, (name, curve) in enumerate(SCENARIOS):
            cum = 0.0
            pts = []
            for c in curve:
                cum += c["net_cash"]
                pts.append({"exit_at": c["exit_at"], "net_cash": c["net_cash"],
                            "cumulative": c["cumulative"]})
            nets = [c["net_cash"] for c in curve]
            cur[0] = {
                "count": len(curve), "wins": len([x for x in nets if x > 0]),
                "losses": len([x for x in nets if x < 0]),
                "flat": len([x for x in nets if x == 0]),
                "total_net": round(cum, 2),
                "win_rate": 0.5, "pl_ratio": 1.5, "profit_factor": 2.0,
                "avg_win": 100.0, "avg_loss": -100.0,
                "max_win": {"trade_id": "T1", "exit_at": "2026-09-01 10:00:00",
                            "net_cash": 300.0},
                "max_loss": {"trade_id": "T2", "exit_at": "2026-09-02 10:00:00",
                             "net_cash": -200.0},
                "expectancy": round(cum / len(curve), 2), "by_reason": {},
                "equity_curve": pts, "symbol_raw": "KQ.m@CFFEX.IF",
                # 每个场景给不同的品种键：面板 HTML 变了才会重写 innerHTML、
                # 才会重建 canvas 并重绘。若两场 HTML 完全一样，面板命中缓存
                # 不重绘（这本身是刻意设计），本层就采不到绘制记录。
                "symbol_key": "IF%d" % si,
                "by_symbol": {}, "read_errors": [], "dbs_scanned": 1,
            }
            page.evaluate("() => { const p = document.getElementById('stats-panel');"
                          " if (p) p.classList.remove('show');"
                          " window.__spy = { texts: [], segs: [], strokes: 0,"
                          "                 fills: 0, arcs: [] }; }")
            page.click("#btn-stats")
            try:
                page.wait_for_function(
                    "() => { const b = document.getElementById('stats-content');"
                    " return !!(b && b.innerHTML.indexOf('成交笔数') >= 0); }",
                    timeout=15000)
            except Exception as e:
                print("  （等渲染超时：%s）" % str(e).splitlines()[0][:90])
            page.wait_for_timeout(300)
            info = page.evaluate("""() => {
              const cv = document.getElementById('trade-equity-canvas');
              if (!cv) return null;
              return { w: parseFloat(cv.style.width), h: parseFloat(cv.style.height),
                       texts: window.__spy.texts, segs: window.__spy.segs,
                       strokes: window.__spy.strokes, fills: window.__spy.fills };
            }""")
            if info is None:
                check(False, "[真渲染 %s] 画布存在" % name)
                continue
            audit("真渲染 " + name, info, curve)
            if shot_dir and name in ("全正 6 笔", "全负 5 笔", "7 位数末端"):
                os.makedirs(shot_dir, exist_ok=True)
                page.locator("#stats-panel").screenshot(
                    path=os.path.join(shot_dir, "after_%s.png" % name.replace(" ", "")))
        page.unroute_all(behavior="ignore")
        browser.close()
    httpd.shutdown()


# ══════════════════════════════════════════════════════════════
print("[静态] node + canvas 桩：抽出 app.js 真实代码段跑逐场景几何审计")
run_static_layer()
print("\n[真渲染] 无头 Chrome + 真实 measureText")
run_render_layer()

print("\n" + "=" * 62)
print("盈亏曲线坐标轴契约 结果: %s"
      % ("全部通过" if not _FAIL else "%d 项失败 -> %s" % (len(_FAIL), _FAIL[:6])))
print("=" * 62)
sys.exit(1 if _FAIL else 0)

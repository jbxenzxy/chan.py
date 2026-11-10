# -*- coding: utf-8 -*-
"""
指标区「两融余额」类MACD —— 双层护栏
=====================================================================
被守护的功能（2026-11-10 新增）：

  底部指标区原本三个候选（成交额/量、MACD、RSI），现新增第四个「两融余额」。
  它不是把每日余额连成曲线，而是把**全 A 股沪深北三市每日两融余额**代入
  MACD(12,26,9)，画成与「成交额/量 类MACD」同形态的红绿柱 + 白橙线。
  口径由用户拍板：仅上证指数日K可用、一次取全历史做 EMA 预热、支持翻转视图。

四层守护：

  ① 静态契约：注册表 / 循环顺序 / 五个钩子 / 单一 MACD 核心 / 单一可用性 SSOT /
     翻转视图分支 / REST 复用 / 门禁登记。
  ② 数值对齐（真跑）：把 app.js 的 VOL_MACD_CORE 段与 buildMarginMacdByDate
     抽到 node 执行，逐点比对后端 AppUtils.calculate_macd —— 不是「看起来像 MACD」。
     顺带钉住：斜杠日期归一、同日重复去重、坏值只丢该行、样本不足 26 根整窗置 0。
  ③ 日期对齐（真跑）：calcMarginMacdMap 把交易日序列映射到每根K线，
     数据起始前给 0、当日未披露继承前一交易日、斜杠日期同样归一。
  ④ canvas 真渲染（node 桩，无浏览器依赖）：抽真实 drawMacd / drawMacdCrossArrows /
     drawMarginMacdAxis / drawMarginSlotLabel，**正常与翻转两个视角各跑一遍**，
     验证柱的位置与颜色对调、白橙线与零线仍在、纵轴 top/bot 文本互换、
     标签行 BAR 颜色对调；再喂脏数据，证明坏值不上屏、不抛异常。
  ⑤ 真浏览器绘制覆盖（无头 Chrome，浏览器不在位降级 SKIP）：起本地静态服务 +
     桩 /api/health / analyze / /api/margin/read → 点 chip 循环到「两融余额」→
     断言请求发出、标签行 DIF/DEA/BAR 带亿元、底部区红/青 MACD 柱真上屏。
  另：① 里「失败只取一次」的旧契约已改为「失败计数 + 指数退避重试（上限 3 次）」。

运行：python Test/test_margin_macd_indicator.py       （run_all 组件）
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")
FRONT_API = os.path.join(REPO_ROOT, "FrontAPI.py")
RUN_ALL = os.path.join(TEST_DIR, "run_all.py")
RUN_ALL_TESTS = os.path.join(REPO_ROOT, "run_all_tests.py")

from App.AppUtils import calculate_macd, MACD_MIN_BARS

_FAILURES = []
CORE_BEGIN = "// >>> VOL_MACD_CORE"
CORE_END = "// <<< VOL_MACD_CORE"


def check(cond, desc, extra=""):
    if cond:
        print(f"  [OK] {desc}")
    else:
        print(f"  [FAIL] {desc} {extra}")
        _FAILURES.append(desc)
    return bool(cond)


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def _extract_fn(src, name):
    """按函数声明起点 + 花括号配平抽出完整函数体（保留 8 空格缩进层级）。"""
    m = re.search(r"^\s{8}function\s+" + re.escape(name) + r"\s*\(", src, re.M)
    if not m:
        return None
    brace = src.find("{", m.end())
    if brace < 0:
        return None
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
    return None


def _extract_core(src):
    a = src.find(CORE_BEGIN)
    b = src.find(CORE_END)
    if a < 0 or b <= a:
        return None
    return src[a:b + len(CORE_END)]


# ═══════════════════════════════════════════════════════════════════════
# ① 静态契约
# ═══════════════════════════════════════════════════════════════════════
def test_static():
    print("\n① 静态契约（注册表 / 单一核心 / 翻转 / 可用性 / 门禁）")
    js = read(APP_JS)
    api = read(FRONT_API)

    order = re.search(r"const BOTTOM_ORDER = \[([^\]]*)\];", js)
    check(bool(order) and order.group(1) == "'vol', 'macd', 'rsi', 'margin'",
          "① BOTTOM_ORDER 恰为 4 项且两融余额在末位（chip 循环第 4 个）",
          order.group(1) if order else "抽不到 BOTTOM_ORDER")

    margin = js[js.find("            margin: {"):js.find("        };", js.find("            margin: {"))]
    check(len(margin) > 200, "① margin 记录区块长度 > 200（防正则只捞半截）", str(len(margin)))
    for hook in ("tabLabel", "range:", "draw:", "axis:", "label:", "cursorText:"):
        check(hook in margin, f"① margin 五钩子齐全：{hook}")
    check("tabLabel: () => '两融余额'" in margin,
          "① chip 短名就是「两融余额」（正文不重复前缀）")

    check(js.count("function calcMacdVals(") == 1
          and js.count("function _volMacdEma(") == 1
          and js.count("2.0 / (period + 1)") == 1,
          "① 全仓只一份 EMA / MACD 核心（成交额类MACD 与两融类MACD 共用）",
          f"calcMacdVals={js.count('function calcMacdVals(')} "
          f"_volMacdEma={js.count('function _volMacdEma(')} "
          f"EMA系数={js.count('2.0 / (period + 1)')}")
    check("const macdVals = calcMacdVals(vals);" in js
          and js.count("calcMacdVals(vals)") == 2,
          "① 两处消费者都调 calcMacdVals（calcVolMacdMap + buildMarginMacdByDate）",
          str(js.count("calcMacdVals(vals)")))

    dmm = _extract_fn(js, "drawMarginMacd") or ""
    check("drawMacd(klines, marginArea, macdRange, barStep, barWidth, subPixelOffset, marginMacdOf);" in dmm,
          "① 画法完全复用 drawMacd，只换取值来源（不写第二套红绿柱/白橙线）")
    check(js.count("function drawMacd(") == 1, "① drawMacd 仍只有一份实现（翻转视图口径单一）")

    axis = _extract_fn(js, "drawMarginMacdAxis") or ""
    label = _extract_fn(js, "drawMarginSlotLabel") or ""
    check("_isMirrorMode ? macdRange.min" in axis
          and "_isMirrorMode ? (v.macd < 0)" in label,
          "① 翻转视图：纵轴 top/bot 对调 + 标签行 BAR 颜色对调（与标准MACD同口径）")
    check("_bottomValueAtY(y, area, range)" in margin
          and "formatMarginMacdVal(_bottomValueAtY" in margin,
          "① 光标读数走 _bottomValueAtY（其本身已处理翻转），单位亿元")

    avail = _extract_fn(js, "marginIndicatorAvailable") or ""
    check("return amoIsAvailable();" in avail,
          "① 可用性单一 SSOT：仅上证指数日K，与两融面板按钮同判据")
    slot = _extract_fn(js, "_slotAt") or ""
    cyc = _extract_fn(js, "cycleBottomSlot") or ""
    check("_bottomIndicatorEnabled(id)" in slot and "_bottomIndicatorEnabled(next)" in cyc
          and "return id !== 'margin' || marginIndicatorAvailable();" in js,
          "① 槽位回落 + chip 循环跳过不可用指标（3 项循环的真行为由"
          " test_bottom_slots 的 node 探针验收）")

    ensure = _extract_fn(js, "_ensureMarginSeries") or ""
    check("/api/margin/read?start_date=" in ensure
          and "encodeURIComponent(_MARGIN_FULL_START)" in ensure
          and "encodeURIComponent(_MARGIN_FULL_END)" in ensure
          and api.count('@router.get("/api/margin/read")') == 1,
          "① 复用既有 /api/margin/read 取全历史，未新增 REST 路由、未改后端")
    check("_marginFetching = true;" in ensure
          and "if (_marginMacdByDate || _marginFetching || _marginRetryTimer) return;" in ensure,
          "① 全历史只取一次（防每帧重连）；失败不再用空对象冒充已取到")
    check("_marginMacdByDate = null;" in ensure
          and "_marginMacdByDate = {}" not in ensure
          and "_MARGIN_MAX_RETRIES" in ensure
          and "_MARGIN_RETRY_BASE_MS * Math.pow(2, _marginRetryCount - 1)" in ensure
          and "_marginRetryCount >= _MARGIN_MAX_RETRIES" in ensure,
          "① 失败计数 + 指数退避重试（上限 3 次，间隔 2/4/8s），耗尽后不每帧重连")
    check("_marginRetryCount = 0;" in ensure and "buildMarginMacdByDate(data);" in ensure,
          "① 成功后重置重试计数（下一次失败重新享有完整退避窗口）")
    check("_marginRetryCount = 0;" in (_extract_fn(js, "cycleBottomSlot") or ""),
          "① 重新选到两融余额槽会重置计数（手动重试入口可达）")
    check("calcMarginMacdMap(data.klines, _marginMacdByDate)" in js,
          "① 渲染层按全序列做日期对齐（绘制 / 标签 / 纵轴 / 读数同源）")
    check("window.margin" not in js and "window.toggleMarginMacd" not in js,
          "① 未新增 window 绑定（phase6 window API 面不漂移）")
    check('"亿"' in (_extract_fn(js, "formatMarginMacdVal") or "")
          and "toFixed(2)" in (_extract_fn(js, "formatMarginMacdVal") or ""),
          "① 数值单位随两融余额（亿元），非成交额/量的万/亿/手")

    check("margin_macd_indicator" in read(RUN_ALL)
          and "191 个组件" in read(RUN_ALL_TESTS),
          "① 门禁注册 + 发现式 docstring 组件数同步")


# ═══════════════════════════════════════════════════════════════════════
# ②③④ node 真跑（数值对齐 / 日期对齐 / canvas 真渲染 + 翻转 + 脏数据）
# ═══════════════════════════════════════════════════════════════════════
NODE_HARNESS = r"""
var rec = {rects: [], strokes: [], fills: [], texts: []};
var ctx = {
    fillStyle: "", strokeStyle: "", lineWidth: 1, font: "",
    textAlign: "", textBaseline: "",
    fillRect: function(x,y,w,h){ rec.rects.push({x:x,y:y,w:w,h:h,c:String(this.fillStyle)}); },
    beginPath: function(){}, moveTo: function(){}, lineTo: function(){},
    closePath: function(){}, arc: function(){},
    fill: function(){ rec.fills.push(String(this.fillStyle)); },
    stroke: function(){ rec.strokes.push(String(this.strokeStyle)); },
    fillText: function(t,x,y){ rec.texts.push({t:String(t),x:x,y:y,c:String(this.fillStyle)}); },
    save: function(){}, restore: function(){}, setLineDash: function(){},
    measureText: function(t){ return {width: String(t).length * 6}; },
    clearRect: function(){}
};
var COLORS = {macdUp:"UP", macdDown:"DOWN", dif:"DIF", dea:"DEA",
              text:"TEXT", textLight:"LIGHT", bg:"BG"};
var MACD_CROSS_H = 10, MACD_CROSS_HW = 4;
var _isMirrorMode = false;
var _marginMacdMap = null, _marginMacdByDate = null, _marginLoadError = "";
%FUNCS%
var DATA = %DATA%;
var KLINES = %KLINES%;
var VALS = %VALS%;

function reset(){ rec.rects=[]; rec.strokes=[]; rec.fills=[]; rec.texts=[]; }

// ② 纯数值序列：JS 的共用 MACD 核心（供 Python 与 calculate_macd 逐点比对）
var coreOut = calcMacdVals(VALS);
var shortOut = calcMacdVals(VALS.slice(0, %SHORT%));
var byDate = buildMarginMacdByDate(DATA);
var byDateKeys = Object.keys(byDate).sort();
var byDateVals = byDateKeys.map(function(d){ return byDate[d]; });

// ③ 交易日 → 每根K线
_marginMacdMap = calcMarginMacdMap(KLINES, byDate);
var mapped = KLINES.map(function(k){ return marginMacdOf(k); });
var range = getMarginMacdRange(KLINES);

// 标签行取数状态：数据已就绪（否则会走「加载中」分支，画不出 DIF/DEA/BAR）
_marginMacdByDate = byDate;
_marginLoadError = "";

// ④ 真渲染：正常 / 翻转 两个视角
var area = {x:0, y:0, w:400, h:100};
var barStep = area.w / KLINES.length;
function renderOne(mirror){
    _isMirrorMode = mirror; reset();
    drawMarginMacd(KLINES, area, range, barStep, 4, 0);
    var draw = {rects: rec.rects.slice(), strokes: rec.strokes.slice(), fills: rec.fills.slice()};
    reset();
    drawMarginMacdAxis(area, range);
    var axis = rec.texts.slice();
    reset();
    drawMarginSlotLabel({x:0,y:0,w:400,h:14}, {targetK: KLINES[KLINES.length-1], textX: 4});
    var label = rec.texts.slice();
    return {draw: draw, axis: axis, label: label};
}
var normal = renderOne(false);
var mirror = renderOne(true);

// 脏数据：坏值 + 多余字段 + 空序列，不得抛异常、不得把 NaN/undefined 画上屏
var DIRTY = {dates:[], balances:[], stats:{peak:null,current:null,peak_ratio:null}};
DIRTY.dates = ["2024/01/01", "2024/01/01", "bad-date", "2024/01/02", "2024/01/03"];
DIRTY.balances = [15000.5, 15100.5, 99999, "?", null];
DIRTY.extra = {RZRQYE: 999, note: "脏字段"};
var dirtyByDate = buildMarginMacdByDate(DIRTY);
var dirtyKeys = Object.keys(dirtyByDate);
var dirtyRender = null;
try {
    _marginMacdByDate = dirtyByDate;
    _marginMacdMap = calcMarginMacdMap(KLINES, dirtyByDate);
    _isMirrorMode = false; reset();
    drawMarginMacd(KLINES, area, getMarginMacdRange(KLINES), barStep, 4, 0);
    drawMarginSlotLabel({x:0,y:0,w:400,h:14}, {targetK: KLINES[KLINES.length-1], textX: 4});
    dirtyRender = {texts: rec.texts.map(function(t){return t.t;}),
                   rects: rec.rects.length};
} catch(e) { dirtyRender = {error: String(e)}; }

console.log(JSON.stringify({
    coreOut: coreOut, shortOut: shortOut,
    byDateKeys: byDateKeys, byDateVals: byDateVals,
    mapped: mapped, range: range,
    normal: normal, mirror: mirror,
    dirtyKeys: dirtyKeys, dirtyRender: dirtyRender
}));
"""


def _run_node(src, payload):
    node = shutil.which("node")
    if not node:
        return None, "node 不在位（真渲染层必须实跑，不降级 SKIP）"
    core = _extract_core(src)
    if core is None:
        return None, "抽不到 VOL_MACD_CORE 段"
    names = ["buildMarginMacdByDate", "calcMarginMacdMap", "marginMacdOf",
             "getMarginMacdRange", "formatMarginMacdVal", "drawMacd",
             "drawMacdCrossArrows", "drawMarginMacd", "drawMarginMacdAxis",
             "drawMarginSlotLabel"]
    bodies = [core]
    for n in names:
        body = _extract_fn(src, n)
        if body is None:
            return None, f"抽不到真函数 {n}"
        bodies.append(body)
    code = (NODE_HARNESS.replace("%FUNCS%", "\n".join(bodies))
            .replace("%DATA%", json.dumps(payload["data"], ensure_ascii=False))
            .replace("%KLINES%", json.dumps(payload["klines"], ensure_ascii=False))
            .replace("%VALS%", json.dumps(payload["vals"], ensure_ascii=False))
            .replace("%SHORT%", str(payload["short"])))
    tmp = tempfile.mkdtemp(prefix="margin_macd_")
    jf = os.path.join(tmp, "harness.js")
    try:
        with open(jf, "w", encoding="utf-8") as f:
            f.write(code)
        proc = subprocess.run([node, jf], capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            return None, f"node rc={proc.returncode}: {proc.stderr[-600:]}"
        return json.loads(proc.stdout.strip().splitlines()[-1]), ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fixture():
    # 波形余额（有涨有跌）→ MACD 柱必然正负都有，才能验证红/青两色
    vals = [15000.0 + 600.0 * math.sin(i / 5.0) + i * 20.0 for i in range(60)]
    dates = ["2024/%02d/%02d" % (1 + i // 28, 1 + i % 28) for i in range(60)]
    # 第 5 天重复一次（后值覆盖前值）；并混入坏值与多余字段
    d = list(dates)
    v = list(vals)
    d.insert(5, dates[4])
    v.insert(5, vals[4] + 777.0)
    d.append("bad-date")
    v.append(-1.0)
    data = {"dates": d, "balances": v,
            "stats": {"peak": None, "current": None, "peak_ratio": None},
            "RZRQYE": 123456}   # 旧口径字段：必须被忽略
    klines = [{"date": "2023-12-25"}]               # 数据开始前 → 0
    for i in range(20):
        klines.append({"date": "2024/%02d/%02d" % (1 + i // 28, 1 + i % 28)})
    klines[2]["date"] = "2024/01/02 15:00:00"      # 带时间戳 → slice(0,10) 命中
    klines.append({"date": "2024-03-20"})           # 晚于最后交易日 → 继承
    # 与 buildMarginMacdByDate 去重后的值序列对齐（去掉重复日 + 坏行）
    uniq = []
    for dd, vv in zip(d, v):
        if len(dd) == 10 and isinstance(vv, (int, float)) and vv > 0:
            key = dd.replace("/", "-")
            if uniq and uniq[-1][0] == key:
                uniq[-1] = (key, vv)
            else:
                uniq.append((key, vv))
    return {"data": data, "klines": klines, "vals": vals,
            "short": MACD_MIN_BARS - 1, "uniq": uniq}


def test_node_real_run():
    print("\n②③④ node 真跑：数值对齐 / 日期对齐 / canvas 真渲染（含翻转）")
    fx = _fixture()
    out, err = _run_node(read(APP_JS), fx)
    if not check(out is not None, "②③④ node harness 真跑成功", err):
        for d in ("数值对齐", "日期对齐", "真渲染"):
            _FAILURES.append(f"{d}（harness 未跑）")
        return

    # ② 数值对齐
    vals = fx["vals"]
    py_macd = calculate_macd(vals)
    js_macd = out["coreOut"]
    ok = len(js_macd) == len(py_macd)
    maxdiff = 0.0
    if ok:
        for a, b in zip(js_macd, py_macd):
            maxdiff = max(maxdiff, abs(a["dif"] - b["dif"]),
                          abs(a["dea"] - b["dea"]), abs(a["macd"] - b["macd"]))
        ok = maxdiff < 1e-8
    check(ok, f"② calcMacdVals 与 AppUtils.calculate_macd 逐点相同（{len(vals)} 点，最大差 {maxdiff:.2e}）")
    check(all(x["dif"] == 0 and x["dea"] == 0 and x["macd"] == 0 for x in out["shortOut"]),
          f"② 样本不足 {MACD_MIN_BARS} 根时整窗置 0（后端同门槛）")

    uniq = fx["uniq"]
    keys = out["byDateKeys"]
    check(keys == [k for k, _ in uniq],
          "② 斜杠日期归一为连字符、同日重复只保留最后一条、坏值只丢该行",
          f"got={keys[:6]}... expect={[k for k,_ in uniq][:6]}...")
    py_uniq = calculate_macd([v for _k, v in uniq])
    diff = 0.0
    if len(out["byDateVals"]) == len(py_uniq):
        for a, b in zip(out["byDateVals"], py_uniq):
            diff = max(diff, abs(a["macd"] - b["macd"]), abs(a["dif"] - b["dif"]))
    check(diff < 1e-8,
          f"② 两融余额序列确实代入 MACD 算法（不是余额原值连成曲线，最大差 {diff:.2e}）")
    check(out["dirtyKeys"] == ["2024-01-01"],
          "② 脏数据：坏日期 / 字符串 / null / 负数只丢该行，多余字段不入口径",
          str(out["dirtyKeys"]))

    # ③ 日期对齐
    mapped = out["mapped"]
    zero = {"dif": 0, "dea": 0, "macd": 0}
    check(mapped[0] == zero, "③ 数据开始前的K线给零值（不放大纵轴）")
    check(mapped[1] == out["byDateVals"][0],
          "③ 斜杠日期归一后精确命中首个交易日")
    check(mapped[2] != zero, "③ 带时间戳的K线按 slice(0,10) 命中交易日")
    check(mapped[-1] == out["byDateVals"][-1],
          "③ 当日两融 T+1 未披露 → 继承前一交易日（与预览bar 同口径）")
    check(len({(x["dif"], x["dea"], x["macd"]) for x in mapped}) > 3,
          "③ 区间内多根K线各自命中不同交易日（不是整段同一值）")
    check(out["range"]["max"] > out["range"]["min"],
          "③ 值域 dif/dea/macd 三者同域且非退化")

    # ④ 真渲染 + 翻转
    n, m = out["normal"], out["mirror"]
    nr, mr = n["draw"]["rects"], m["draw"]["rects"]
    check(len(nr) == len(fx["klines"]) and len(mr) == len(nr) and len(nr) > 0,
          f"④ 两视角柱数相同且等于K线数（{len(nr)}）")
    check(len({r["c"] for r in nr}) >= 2,
          "④ 正常视角同时有红柱与青柱（不是空白图）")
    flipped_colors = sum(1 for a, b in zip(nr, mr)
                         if (a["c"] == "UP" and b["c"] == "DOWN")
                         or (a["c"] == "DOWN" and b["c"] == "UP"))
    check(flipped_colors == len(nr),
          f"④ 翻转视图：每一根柱的颜色都对调（{flipped_colors}/{len(nr)}）")
    moved = sum(1 for a, b in zip(nr, mr) if abs(a["y"] - b["y"]) > 0.01)
    check(moved > 0, f"④ 翻转视图：柱绕零线翻转（{moved} 根 y 改变）")
    check(len(n["draw"]["strokes"]) == 3 and len(m["draw"]["strokes"]) == 3,
          "④ 两视角都画出白线 DIF / 橙线 DEA / 零线（各 3 次 stroke）")
    check(len(n["draw"]["fills"]) == len(m["draw"]["fills"]),
          "④ 金叉/死叉箭头在两视角都按真实 drawMacdCrossArrows 绘制")

    na = [t["t"] for t in n["axis"]]
    ma = [t["t"] for t in m["axis"]]
    check(len(na) == 3 and na[1] == "0" and na[0] != ma[0] and na[0] == ma[2],
          "④ 纵轴翻转：top/bot 文本互换、零线仍在中", f"normal={na} mirror={ma}")

    def bar_color(view):
        hits = [t["c"] for t in view["label"] if t["t"].startswith("BAR:")]
        return hits[0] if hits else None

    nb, mb = bar_color(n), bar_color(m)
    check(nb in ("#FF3C3C", "#00F0F0") and mb in ("#FF3C3C", "#00F0F0") and nb != mb,
          "④ 标签行 BAR 颜色随翻转对调（与标准MACD 同口径）", f"{nb} -> {mb}")
    check(any(t["t"].endswith("亿") or t["t"].endswith("万亿")
              for t in n["label"] if t["t"].startswith(("DIF:", "DEA:", "BAR:"))),
          "④ 标签行数值单位为亿元（与两融余额口径一致）")

    dirty = out["dirtyRender"]
    check(isinstance(dirty, dict) and "error" not in dirty,
          "④ 脏数据不抛异常", str(dirty))
    if isinstance(dirty, dict) and "error" not in dirty:
        bad = [t for t in dirty["texts"] if "NaN" in t or "undefined" in t or "Infinity" in t]
        check(not bad, "④ 脏数据不上屏（无 NaN / undefined / Infinity）", str(bad[:4]))


# ═══════════════════════════════════════════════════════════════════════
# ⑤ 真浏览器绘制覆盖（无头 Chrome：chip 循环到两融余额 → 柱子上屏）
# ═══════════════════════════════════════════════════════════════════════
MARGIN_RECT_RECORDER_JS = r"""
window.__marginTexts = [];
window.__marginRects = [];
window.EventSource = function () {
  this.readyState = 0; this.url = ''; this.close = function () {};
  this.addEventListener = function () {}; this.removeEventListener = function () {};
  this.dispatchEvent = function () { return false; };
};
(function () {
  var orig = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type) {
    var ctx = orig.apply(this, arguments);
    if (type === '2d' && ctx && !ctx.__marginWrapped) {
      ctx.__marginWrapped = true;
      var of = ctx.fillText, or = ctx.fillRect;
      ctx.fillText = function (t, x, y) {
        try { window.__marginTexts.push(String(t)); } catch (e) {}
        return of.apply(this, arguments);
      };
      ctx.fillRect = function (x, y, w, h) {
        try { window.__marginRects.push([x, y, w, h, String(this.fillStyle)]); } catch (e) {}
        return or.apply(this, arguments);
      };
    }
    return ctx;
  };
})();
"""


def test_real_browser_margin_slot():
    print("\n⑤ 真浏览器绘制覆盖（chip 循环到两融余额 → 柱子上屏）")
    if TEST_DIR not in sys.path:
        sys.path.insert(0, TEST_DIR)
    try:
        from test_vol_macd_mode import find_playwright, _launch_browser
    except Exception as e:
        print("  [SKIP] 无法复用浏览器启动器：%s" % e)
        return
    pl, why = find_playwright()
    if pl is None:
        print("  [SKIP] %s（静态 / node / canvas 层已覆盖）" % why)
        return
    from playwright.sync_api import sync_playwright

    snap_path = os.path.join(TEST_DIR, "snapshots", "stock_d_full.json")
    if not check(os.path.isfile(snap_path), "⑤ 股票快照存在", snap_path):
        return
    served = json.loads(open(snap_path, encoding="utf-8").read())
    # 只改喂给页面的副本：两融指标与面板共用「上证指数日K」判据
    served["meta"]["symbol"] = "sh000001"
    served["meta"]["freq"] = "日线"
    served["meta"]["is_index"] = True
    dates = [k["date"] for k in served["klines"]]
    balances = [15000.0 + 800.0 * math.sin(i / 6.0) + i * 3.0
                for i in range(len(dates))]
    margin_payload = json.dumps({
        "dates": dates, "balances": balances,
        "stats": {"peak": None, "current": None, "peak_ratio": None}})
    served_text = json.dumps(served)
    margin_hits = []

    import functools
    import http.server
    import threading

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(_Quiet, directory=os.path.join(REPO_ROOT, "Frontend")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    js = read(APP_JS)
    m_pad = re.search(r"const PADDING = \{ top: (\d+), right: (\d+), bottom: (\d+), left: (\d+) \};", js)
    m_vol = re.search(r"const VOL_RATIO = ([\d.]+), GAP = (\d+);", js)
    m_gap = re.search(r"const rightGap = (\d+);", js)
    up = re.search(r'macdUp:\s*"([^"]+)"', js)
    down = re.search(r'macdDown:\s*"([^"]+)"', js)
    if not all([m_pad, m_vol, m_gap, up, down]):
        check(False, "⑤ 布局常量与柱色可从源码解析（测试与实现同源）", "")
        httpd.shutdown()
        return
    top, right, bottom, left = [int(x) for x in m_pad.groups()]
    vol_base, gap = float(m_vol.group(1)), int(m_vol.group(2))
    right_gap = int(m_gap.group(1))
    colors = (up.group(1), down.group(1))

    with sync_playwright() as pw:
        browser, which, notes = _launch_browser(pw)
        if browser is None:
            print("  [SKIP] 无可用无头浏览器：" + " / ".join(notes[:3]))
            httpd.shutdown()
            return
        print("  （无头浏览器：%s）" % which)
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        page.add_init_script(MARGIN_RECT_RECORDER_JS)

        def route_api(route):
            url = route.request.url
            if "/api/margin/read" in url:
                margin_hits.append(url)
                body = margin_payload
            elif "/api/health" in url:
                body = json.dumps({"config": {"view_count": 233}})
            elif "analyze" in url:
                body = served_text
            else:
                body = "{}"
            route.fulfill(status=200, content_type="application/json", body=body)

        page.route("**/api/**", route_api)
        page.goto("http://127.0.0.1:%d/app.html" % port, wait_until="load")
        page.wait_for_function(
            "() => { const s = window.ChanApp && window.ChanApp.state;"
            " return !!(s && s.chartData && s.chartData.klines && s.chartData.klines.length > 0); }",
            timeout=20000)
        page.wait_for_timeout(300)
        page.evaluate("""() => {
            const s = window.ChanApp.state;
            s.currentFreq = 'd';
            s.chartData.meta.symbol = 'sh000001';
            s._bottomSlots = ['macd', 'rsi'];
        }""")

        box = page.locator("#chart-container canvas").first.bounding_box()
        if not check(box is not None and box["width"] > 200,
                     "⑤ 首屏 canvas 已渲染", str(box)):
            page.unroute_all(behavior="ignore")
            browser.close()
            httpd.shutdown()
            return
        W, H = box["width"], box["height"]
        netH = H - top - bottom - gap
        b_top = top + netH * (1 - vol_base * 2)
        area_w = W - left - right - right_gap
        chip_x = box["x"] + left + area_w - 6
        chip_y = box["y"] + b_top + 7

        # chip 两次：macd → rsi → margin（可用图上不再跳过 margin）
        page.mouse.click(chip_x, chip_y)
        page.wait_for_timeout(200)
        page.mouse.click(chip_x, chip_y)
        page.wait_for_timeout(500)
        slots = page.evaluate("() => window.ChanApp.state._bottomSlots")
        check(list(slots)[0] == "margin",
              "⑤ chip 循环到两融余额（macd → rsi → margin）", str(slots))
        check(len(margin_hits) >= 1,
              "⑤ /api/margin/read 被真请求（不是只改了状态没取数）", str(len(margin_hits)))
        store = page.evaluate(
            "() => JSON.parse(localStorage.getItem('chan_overlay_settings') || '{}')")
        check(list(store.get("bottomSlots") or [])[:1] == ["margin"],
              "⑤ 槽位配置正常落盘 bottomSlots", str(store.get("bottomSlots")))

        # 清记录 → 鼠标移出画布触发一次干净重绘（避免 hover 标签与重复文字）
        page.evaluate("() => { window.__marginTexts = []; window.__marginRects = []; }")
        page.mouse.move(box["x"] + W / 2, box["y"] + H + 60)
        page.wait_for_timeout(350)
        texts = page.evaluate("() => window.__marginTexts.slice()")
        rects = page.evaluate("() => window.__marginRects.slice()")
        # Canvas 的 fillStyle getter 会把颜色串归一化（rgba(255,60,60,0.6) →
        # rgba(255, 60, 60, 0.6)），与源码字面逐字比会假红 ⇒ 比对前去掉空白。
        norm = lambda c: c.replace(" ", "")
        palette = tuple(norm(c) for c in colors)
        bars = [r for r in rects if r[1] > b_top and norm(r[4]) in palette]

        check("两融余额" in texts,
              "⑤ 标签行 chip 写出「两融余额」", repr(texts[-20:]))
        dif = [t for t in texts if t.startswith("DIF:")]
        dea = [t for t in texts if t.startswith("DEA:")]
        bar = [t for t in texts if t.startswith("BAR:")]
        check(bool(dif) and bool(dea) and bool(bar)
              and all(("亿" in t) for t in (dif[0], dea[0], bar[0])),
              "⑤ 标签行 DIF / DEA / BAR 真取到两融 MACD 值（单位亿元）",
              repr([dif[:1], dea[:1], bar[:1]]))
        check(len(bars) > 5,
              "⑤ 底部区真有两融 MACD 柱上屏（COLORS.macdUp / macdDown 的 fillRect）",
              "bars=%d rects=%d b_top=%.1f palette=%s observed=%s"
              % (len(bars), len(rects), b_top, palette,
                 sorted({r[4] for r in rects})[:14]))
        check(len({r[4] for r in bars}) >= 2,
              "⑤ 波形余额产生正负两色柱（不是全零值空图）",
              str({r[4] for r in bars}))

        page.unroute_all(behavior="ignore")
        browser.close()
    httpd.shutdown()

def main():
    print("=" * 72)
    print("两融余额类MACD 指标：静态契约 + node 数值对齐 + canvas 真渲染（含翻转）")
    print("=" * 72)
    test_static()
    test_node_real_run()
    test_real_browser_margin_slot()
    print("\n" + "=" * 72)
    if _FAILURES:
        print(f"合计 {len(_FAILURES)} 项失败：")
        for f in _FAILURES:
            print("  -", f)
        sys.exit(1)
    print("合计 0 项失败")
    sys.exit(0)


if __name__ == "__main__":
    main()

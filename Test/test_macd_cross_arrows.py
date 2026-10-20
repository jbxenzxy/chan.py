# -*- coding: utf-8 -*-
"""
MACD 金叉 / 死叉箭头 —— 判定口径 + 几何落点 + 真渲染三层守护
=====================================================================
被守护的功能（2026-10-20 新增）：

  MACD 槽在 DIF/DEA 交叉处画箭头：
    ① 金叉（本根 DIF > DEA，等于不算）→ 红色向上箭头，颜色取 COLORS.macdUp
       —— 与 MACD 红柱**同一个常量**，不另写字面量；
    ② 死叉（本根 DIF < DEA，等于不算）→ 绿色向下箭头，颜色取 COLORS.macdDown；
    ③ 翻转视图：落点、朝向、颜色**一起翻**（金叉→绿箭头朝下、死叉→红箭头朝上），
       不变式「红恒朝上 / 绿恒朝下」在两态下都成立，变的只是「交叉 → 颜色」的绑定；
    ④ 成交额/量 的「类MACD」走同一个 drawMacd（valOf = volMacdOf），也画自己那一槽的
       交叉箭头 —— 判定与落点跟着取值函数走，不串到价格 MACD 槽。

三层：

  ① 源码契约（静态）：调用点无条件（两槽都画）、颜色只走 COLORS 且随翻转对调、
     判定式就是「本根严格 + 前一根非严格」、几何参数集中成常量、无颜色字面量。
  ② 几何真跑（node + canvas 桩）：把 app.js 里的 `drawMacd` 与
     `drawMacdCrossArrows` **原样抽出来**执行，用桩 ctx 记录每次 `fill()` 的
     fillStyle 与路径点，再对「交叉点集合 / 方向 / 落点 / 越界」做几何断言。
     —— 不是"源码里看起来有"，是"真跑一遍：画了几支、画在哪、是不是真在交叉根上"。
     ②b 单开一跑量能侧：价格字段全程等值时，价格槽必须 0 箭头、量能槽必须画出
     volMacdOf 的交叉 —— 两槽各判各的，互不串槽。
  ③ 真渲染（无头 Chrome，浏览器不在位降级 SKIP）：起本地静态服务 + 路由打桩喂
     真实快照，在同一帧里记录主画布的 `fill()` 序列，按 MACD 槽绘图窗筛出箭头，
     再反查"这根K线是否真是交叉根"——证明箭头真的接进了渲染管线，而不是只存在于
     一个没人调用的函数里。

运行：python Test/test_macd_cross_arrows.py     # 校验（run_all 组件）
"""
import glob
import json
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
FRONTEND_DIR = os.path.join(REPO_ROOT, "Frontend")
SNAP_STOCK = os.path.join(TEST_DIR, "snapshots", "stock_d_full.json")


# ═══════════════════════════════════════════════════════════════════
# 公共工具
# ═══════════════════════════════════════════════════════════════════
def read(path):
    """读文本并归一换行（本仓是 CRLF 检出，正则/切片一律按 \\n 处理）。"""
    with open(path, encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


def check(failures, cond, desc, detail=""):
    if cond:
        print(f"  [ok] {desc}")
    else:
        failures.append(desc + (f" | {detail}" if detail else ""))
        print(f"  [FAIL] {desc}" + (f" | {detail}" if detail else ""))
    return bool(cond)


def fn_src(js, name):
    """按大括号配对抽 `function <name>(...) { ... }` 整段。

    不用缩进/空行切分：函数体内既有空行，也有与函数同级缩进的块注释。
    """
    m = re.search(r"\n[ \t]*function\s+" + re.escape(name) + r"\s*\(", js)
    if not m:
        return None
    i = js.find("{", m.end())
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(js)):
        ch = js[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return js[m.start():j + 1]
    return None


def const_decl(js, name):
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*(\d+)\s*;", js)
    return (m.group(1), int(m.group(1))) if m else (None, None)


def colors_of(js):
    """从 COLORS 字面量取 macdUp / macdDown —— 用例不另抄一份颜色。"""
    m = re.search(r"macdUp:\s*\"([^\"]+)\",\s*macdDown:\s*\"([^\"]+)\"", js)
    return (m.group(1), m.group(2)) if m else (None, None)


def norm_color(s):
    return re.sub(r"\s+", "", str(s)).lower()


def find_node():
    cand = [os.environ.get("NODE_EXE"), shutil.which("node")]
    cand += glob.glob(os.path.expanduser(
        "~/.workbuddy/binaries/node/versions/*/node.exe"))
    cand += [r"C:\Program Files\nodejs\node.exe"]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def find_playwright():
    try:
        import playwright  # noqa: F401
        return playwright, ""
    except Exception as e:  # pragma: no cover
        return None, f"playwright 未安装: {type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════════
# ① 源码契约
# ═══════════════════════════════════════════════════════════════════
def test_source_contract(failures):
    print("\n① 源码契约（调用点 / 颜色来源 / 判定式 / 参数集中）")
    js = read(APP_JS)
    macd = fn_src(js, "drawMacd")
    arrows = fn_src(js, "drawMacdCrossArrows")
    if not check(failures, macd is not None, "app.js 内存在 drawMacd"):
        return
    if not check(failures, arrows is not None,
                 "app.js 内存在 drawMacdCrossArrows（箭头绘制体）",
                 "缺函数时 ②③ 都无从谈起"):
        return

    up, down = colors_of(js)
    check(failures, up is not None and down is not None,
          "COLORS 里能取到 macdUp / macdDown", f"got {up!r} / {down!r}")

    calls = re.findall(r"^\s*drawMacdCrossArrows\(klines, macdArea, macdToY, barStep, subPixelOffset, getVals\);$", macd, re.M)
    check(failures, len(calls) == 1 and "if (!valOf)" not in macd,
          "drawMacd 内**无条件**调用一次箭头绘制（价格MACD 与 类MACD 各画自己一槽）",
          f"命中 {len(calls)}")
    check(failures,
          "drawMacd(klines, volArea, macdRange, barStep, barWidth, subPixelOffset, volMacdOf);" in js,
          "drawVolumeMacd 仍把 volMacdOf 当 valOf 传入（类MACD 的箭头按成交额/量的值判，"
          "不偷价格 dif/dea）")

    check(failures, "ctx.fillStyle = (golden !== flip) ? COLORS.macdUp : COLORS.macdDown;" in arrows
          and "const flip = _isMirrorMode;" in arrows,
          "箭头取色 = COLORS.macdUp / macdDown，且翻转视图下**对调**（(golden !== flip)）")
    check(failures, "const dir = (golden ? -1 : 1) * (flip ? -1 : 1);" in arrows,
          "朝向与取色同源翻转：翻转视图下金叉朝下、死叉朝上（不是只翻落点、不翻方向）")
    lits = re.findall(r"#[0-9A-Fa-f]{3,8}\b|rgba?\([^)]*\)", arrows)
    check(failures, not lits,
          "箭头函数体内无颜色字面量（防「另写一个红」与柱色漂移）", f"发现 {lits}")

    check(failures, "const golden = cur.dif > cur.dea && prev.dif <= prev.dea;" in arrows,
          "金叉 = 本根 dif>dea（严格，等于不算）且前一根 dif<=dea")
    check(failures, "const death = cur.dif < cur.dea && prev.dif >= prev.dea;" in arrows,
          "死叉 = 本根 dif<dea（严格，等于不算）且前一根 dif>=dea")
    check(failures, "for (let i = 1; i < n; i++)" in arrows,
          "从第 2 根起判（首根无前值，不能凭空造交叉）")

    h_lit, h = const_decl(js, "MACD_CROSS_H")
    hw_lit, hw = const_decl(js, "MACD_CROSS_HW")
    check(failures, h is not None and hw is not None and 4 <= h <= 16 and 2 <= hw <= 8,
          "箭头尺寸常量 MACD_CROSS_H / MACD_CROSS_HW 存在且在合理区间",
          f"H={h} HW={hw}")
    check(failures, arrows.count("MACD_CROSS_H") >= 5 and arrows.count("MACD_CROSS_HW") >= 2,
          "箭头路径全部走常量（不散落裸像素值）",
          f"H x{arrows.count('MACD_CROSS_H')} HW x{arrows.count('MACD_CROSS_HW')}")

    check(failures, "if (dir < 0 && yTip < lo)" in arrows
          and "if (dir > 0 && yTip > hi)" in arrows,
          "尖端越出绘图窗时整体夹回（不许画到标签行 / 相邻槽）")
    check(failures, "typeof dif === 'number'" in arrows and "typeof dea === 'number'" in arrows,
          "只认有限数值：缺字段 / null / NaN 一律不判",
          "null 若走 Number() 会被当成 0 这个合法值参与比较")


# ═══════════════════════════════════════════════════════════════════
# ② 几何真跑（node + canvas 桩）
# ═══════════════════════════════════════════════════════════════════
NODE_DRIVER = r"""
const fs = require('fs');
const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

// —— app.js 的模块级依赖，按真实值注入 ——
const COLORS = spec.colors;
const ZERO_MACD_VALS = { dif: 0, dea: 0, macd: 0 };
// 类MACD 的取值 Map：与 app.js 的 _volMacdMap 同构（key 为 K 线对象本身）
const _volMacdMap = new Map((spec.klines || []).filter(k => k.vm).map(k => [k, k.vm]));
let _isMirrorMode = !!spec.mirror;

// —— 被测代码（上面由 Python 原样拼好：两个 const + 两个函数）——

// —— canvas 桩：记录每次 fill() 的 fillStyle 与路径点 ——
function makeCtx() {
  let cur = null;
  const rec = [];
  return {
    _rec: rec,
    fillStyle: '', strokeStyle: '', lineWidth: 1,
    beginPath() { cur = []; },
    moveTo(x, y) { if (cur) cur.push([x, y]); },
    lineTo(x, y) { if (cur) cur.push([x, y]); },
    closePath() {},
    fill() { if (cur) rec.push({ style: String(this.fillStyle), pts: cur }); cur = null; },
    stroke() { cur = null; },
    fillRect() {}, strokeRect() {}, arc() {}, rect() {},
    measureText() { return { width: 0 }; }, fillText() {},
  };
}

const area = spec.area;
const range = spec.range;
const span = range.max - range.min;
const macdToY = _isMirrorMode
  ? (v) => area.y + (v - range.min) / span * area.h
  : (v) => area.y + area.h - (v - range.min) / span * area.h;

const out = {};

// A) 只跑箭头函数（几何主战场）
let ctx = makeCtx();
drawMacdCrossArrows(spec.klines, area, macdToY, spec.barStep, spec.subPixelOffset, (k) => k);
out.arrows = ctx._rec;

// B) 跑整段 drawMacd：不传 valOf ⇒ 有箭头（正例）
ctx = makeCtx();
drawMacd(spec.klines, area, range, spec.barStep, spec.barWidth, spec.subPixelOffset);
out.macdNoVal = ctx._rec.length;

// C) 跑整段 drawMacd：传 valOf（类MACD 的实际调用形态）⇒ 按**取值函数**画箭头
//    取值用「dif / dea 互换」而不是常量：互换后交叉依然成串（金叉↔死叉互换），
//    这里断言的是「箭头跟着 valOf 走」，常量写法会让断言退化成恒真。
ctx = makeCtx();
drawMacd(spec.klines, area, range, spec.barStep, spec.barWidth, spec.subPixelOffset,
         (k) => ({ dif: k.dea, dea: k.dif, macd: 0 }));
out.valArrows = ctx._rec;
// C2) valOf 返回常量（永不交叉）⇒ 0 箭头：证明没有偷偷回落到 K 线自身的 dif/dea
ctx = makeCtx();
drawMacd(spec.klines, area, range, spec.barStep, spec.barWidth, spec.subPixelOffset,
         () => ({ dif: 1, dea: 0, macd: 1 }));
out.valFlat = ctx._rec.length;

// D) 空 / 单根 / 绘图窗太矮 ⇒ 零箭头、且不抛
ctx = makeCtx();
drawMacdCrossArrows([], area, macdToY, spec.barStep, 0, (k) => k);
drawMacdCrossArrows(spec.klines.slice(0, 1), area, macdToY, spec.barStep, 0, (k) => k);
drawMacdCrossArrows(spec.klines, { x: area.x, y: area.y, w: area.w, h: 6 },
                    macdToY, spec.barStep, 0, (k) => k);
out.degenerate = ctx._rec.length;

// E) 类MACD：drawVolumeMacd 内部 = drawMacd + volMacdOf ⇒ 画的是**量能**的交叉
//    同一批 K 线再单独跑一次价格 paths，用来证明两槽各判各的（互不串槽）。
ctx = makeCtx();
drawVolumeMacd(spec.klines, area, range, spec.barStep, spec.barWidth, spec.subPixelOffset);
out.volArrows = ctx._rec;
ctx = makeCtx();
drawMacd(spec.klines, area, range, spec.barStep, spec.barWidth, spec.subPixelOffset);
out.priceArrows = ctx._rec;

process.stdout.write(JSON.stringify(out));
"""


def _num(x):
    """只认有限数值：None / 字符串（含 "NaN"）/ NaN 一律视为缺值。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    if isinstance(x, float) and x != x:
        return None
    return x


def expected_crosses(klines):
    """测试侧独立重算交叉（不复用被测代码的表达式，避免「两边同错」）。"""
    gold, death = [], []
    for i in range(1, len(klines)):
        a, b = klines[i - 1], klines[i]
        d0, e0 = _num(a.get("dif")), _num(a.get("dea"))
        d1, e1 = _num(b.get("dif")), _num(b.get("dea"))
        if None in (d0, e0, d1, e1):
            continue
        if d1 > e1 and d0 <= e0:
            gold.append(i)
        elif d1 < e1 and d0 >= e0:
            death.append(i)
    return gold, death


def _area():
    return {"x": 60.0, "y": 300.0, "w": 800.0, "h": 80.0}


def _range_of(klines, pad=None):
    lo = min(min(_num(k.get("dif")) or 0, _num(k.get("dea")) or 0) for k in klines)
    hi = max(max(_num(k.get("dif")) or 0, _num(k.get("dea")) or 0) for k in klines)
    m = pad if pad is not None else (max(abs(hi), abs(lo)) * 0.1 or 0.1)
    return {"min": lo - m, "max": hi + m}


def _spec(klines, area, bar_step=8.0, sub_px=0.0, mirror=False, rng=None, colors=None):
    return {
        "klines": klines,
        "area": area,
        "range": rng or _range_of(klines),
        "barStep": bar_step,
        "barWidth": bar_step * 0.5,
        "subPixelOffset": sub_px,
        "mirror": mirror,
        "colors": colors,
    }


def _run_node(node, drv, spec, tmp):
    path = os.path.join(tmp, "spec.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec, f)
    proc = subprocess.run([node, drv, path], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return None, (proc.stderr or "").strip().splitlines()[-1][:200]
    return json.loads(proc.stdout), ""


def _check_one(failures, name, spec, out, h):
    gold, death = expected_crosses(spec["klines"])
    arr = out["arrows"]
    area = spec["area"]
    ok = check(failures, len(arr) == len(gold) + len(death),
               f"{name}：箭头数 == 交叉数（{len(gold)} 金叉 + {len(death)} 死叉）",
               f"实画 {len(arr)}")
    if not ok:
        return

    idx = sorted(gold + death)
    bad_dir, bad_style, bad_pts, bad_h, bad_x, bad_box = [], [], [], [], [], []
    flip = bool(spec.get("mirror"))
    for a, i in zip(arr, idx):
        pts, ys = a["pts"], [p[1] for p in a["pts"]]
        is_gold = i in gold
        # 金叉默认朝上；翻转视图下金叉朝下。取色与朝向同侧：红 = 朝上、绿 = 朝下。
        want_up = is_gold != flip
        if len(pts) != 7:
            bad_pts.append(i)
            continue
        if want_up and not (ys[0] == min(ys) and ys.count(min(ys)) == 1):
            bad_dir.append((i, ("金叉" if is_gold else "死叉") + "未朝上", "flip" if flip else "normal"))
        if (not want_up) and not (ys[0] == max(ys) and ys.count(max(ys)) == 1):
            bad_dir.append((i, ("金叉" if is_gold else "死叉") + "未朝下", "flip" if flip else "normal"))
        want = spec["colors"]["macdUp"] if want_up else spec["colors"]["macdDown"]
        if norm_color(a["style"]) != norm_color(want):
            bad_style.append((i, a["style"], want))
        if abs((max(ys) - min(ys)) - h) > 1e-6:
            bad_h.append((i, round(max(ys) - min(ys), 3)))
        want_x = area["x"] + spec["barStep"] * i + spec["barStep"] / 2 - spec["subPixelOffset"]
        if abs(pts[0][0] - want_x) > 1e-6:
            bad_x.append((i, round(pts[0][0], 3), round(want_x, 3)))
        if min(ys) < area["y"] - 1e-6 or max(ys) > area["y"] + area["h"] + 1e-6:
            bad_box.append((i, round(min(ys), 2), round(max(ys), 2)))

    check(failures, not bad_dir, f"{name}：箭头方向与颜色同侧（红恒朝上、绿恒朝下；翻转视图对调）",
          f"{bad_dir[:3]}")
    check(failures, not bad_style, f"{name}：颜色逐支对上红/青，且与本视角的交叉类型匹配",
          f"{bad_style[:3]}")
    check(failures, not bad_pts, f"{name}：每支都是 7 点闭合路径", f"{bad_pts[:3]}")
    check(failures, not bad_h, f"{name}：箭头总高恒为 MACD_CROSS_H（夹回不压扁形状）",
          f"{bad_h[:3]}")
    check(failures, not bad_x, f"{name}：横向落点 = 该根K线柱心", f"{bad_x[:3]}")
    check(failures, not bad_box, f"{name}：整体落在绘图窗内（不越界到标签行/相邻槽）",
          f"{bad_box[:3]}")


def test_geometry(failures):
    print("\n② 几何真跑（node 执行 app.js 里的真实函数，桩 ctx 记路径）")
    js = read(APP_JS)
    macd = fn_src(js, "drawMacd")
    arrows = fn_src(js, "drawMacdCrossArrows")
    if not (macd and arrows):
        check(failures, False, "② 前置：两个函数都能抽出", "见 ①")
        return
    up, down = colors_of(js)
    h_lit, h = const_decl(js, "MACD_CROSS_H")
    hw_lit, hw = const_decl(js, "MACD_CROSS_HW")
    colors = {"macdUp": up, "macdDown": down}

    node = find_node()
    if not node:
        print("  [SKIP] 未找到 node（几何层无法执行；① 已覆盖契约）")
        return

    # 样本 1：手排 3 金叉 / 2 死叉，中间夹一段 dif==dea 等值
    seq = [(-1.0, -0.5), (-0.5, -0.6), (0.0, -0.3), (0.2, 0.1), (0.1, 0.3),
           (0.0, 0.0), (0.0, 0.0), (0.3, 0.0), (0.5, 0.2), (0.4, 0.6),
           (0.2, 0.4), (0.6, 0.3)]
    k1 = [{"dif": d, "dea": e, "macd": (d - e) * 2} for d, e in seq]

    # 样本 2：缺字段 / 显式 NaN 字符串（JSON 不能带 NaN，走 Number("NaN") 同一条路径）
    k2 = [dict(k) for k in k1]
    del k2[3]["dif"]
    k2[8]["dea"] = "NaN"

    # 样本 3：交叉点贴上下沿（夹回逻辑的正例）
    k3 = [{"dif": -9.0, "dea": -1.0, "macd": 0},
          {"dif": 1.0, "dea": 0.9, "macd": 0.2},
          {"dif": -1.0, "dea": -0.9, "macd": -0.2},
          {"dif": -9.0, "dea": -1.0, "macd": 0}]

    area = _area()
    cases = [
        ("手排序列（3金叉/2死叉/2根等值）", _spec(k1, area, colors=colors)),
        ("含缺字段与 NaN", _spec(k2, area, colors=colors)),
        ("交叉点贴上下沿（夹回正例）", _spec(k3, area, rng={"min": -10.0, "max": 2.0}, colors=colors)),
        ("subPixelOffset=0.4（滚动中）", _spec(k1, area, sub_px=0.4, colors=colors)),
        ("barStep=2（密集）", _spec(k1, area, bar_step=2.0, colors=colors)),
        ("翻转视图", _spec(k1, area, mirror=True, colors=colors)),
    ]

    tmp = tempfile.mkdtemp(prefix="macd_arrows_")
    drv = os.path.join(tmp, "driver.js")
    with open(drv, "w", encoding="utf-8") as f:
        f.write(f"const MACD_CROSS_H = {h};\n")
        f.write(f"const MACD_CROSS_HW = {hw};\n")
        f.write(fn_src(js, "volMacdOf") + "\n")
        f.write(fn_src(js, "drawVolumeMacd") + "\n")
        f.write(arrows + "\n" + macd + "\n" + NODE_DRIVER)

    results = {}
    for name, spec in cases:
        out, err = _run_node(node, drv, spec, tmp)
        if out is None:
            check(failures, False, f"{name}：node 执行成功", err)
            continue
        results[name] = (spec, out)
        _check_one(failures, name, spec, out, h)

    # 翻转视图：**锚点（箭尾）**关于中线镜像；朝向与颜色**一起翻**（用户 2026-10-20 定）。
    #   不变式：两态下都成立「红恒朝上、绿恒朝下」；变的只是「交叉 → 颜色」的绑定
    #   （正常：金叉红朝上 / 死叉绿朝下；翻转：金叉绿朝下 / 死叉红朝上）。
    #   唯一的合法偏差是「夹回」：镜像后尖端会越出窗沿时，整支被推回窗内，
    #   此时尖端必须**恰好压在窗沿上**（否则就是算错了位置）。
    base_name = "手排序列（3金叉/2死叉/2根等值）"
    if base_name in results and "翻转视图" in results:
        (sp_n, out_n), (sp_m, out_m) = results[base_name], results["翻转视图"]
        mid = sp_n["area"]["y"] + sp_n["area"]["h"] / 2
        lo = sp_n["area"]["y"] + 1
        hi = sp_n["area"]["y"] + sp_n["area"]["h"] - 1
        bad, clamped = [], 0
        for a_n, a_m in zip(out_n["arrows"], out_m["arrows"]):
            want_tail = 2 * mid - a_n["pts"][3][1]
            tail_m, tip_m = a_m["pts"][3][1], a_m["pts"][0][1]
            if abs(tail_m - want_tail) <= 1e-6:
                continue
            if min(abs(tip_m - lo), abs(tip_m - hi)) <= 1e-6:
                clamped += 1
            else:
                bad.append((round(tail_m, 2), round(want_tail, 2), round(tip_m, 2)))
        check(failures, not bad,
              "翻转视图：锚点（箭尾=交叉点）关于中线镜像"
              f"（{clamped} 支因越界夹回，尖端已压窗沿）", f"{bad[:3]}")
        same_n = len(out_n["arrows"]) == len(out_m["arrows"])
        up_ok = all(a["pts"][0][1] < a["pts"][3][1] for a in out_m["arrows"]
                    if norm_color(a["style"]) == norm_color(up))
        dn_ok = all(a["pts"][0][1] > a["pts"][3][1] for a in out_m["arrows"]
                    if norm_color(a["style"]) == norm_color(down))
        check(failures, same_n and up_ok and dn_ok,
              "翻转视图：朝向与颜色一起翻（金叉绿朝下、死叉红朝上），仍是「红恒朝上/绿恒朝下」")
        # 逐支对位比较：同一条交叉根上，两态的取色必须**互换**（不是只翻位置）
        not_swapped = []
        for i, (a_n, a_m) in enumerate(zip(out_n["arrows"], out_m["arrows"])):
            c_n, c_m = norm_color(a_n["style"]), norm_color(a_m["style"])
            if not ((c_n == norm_color(up) and c_m == norm_color(down))
                    or (c_n == norm_color(down) and c_m == norm_color(up))):
                not_swapped.append((i, a_n["style"], a_m["style"]))
        check(failures, not not_swapped,
              "翻转视图：同一条交叉根的取色与正常视图互换（金叉↔死叉，不是同色照搬）",
              f"{not_swapped[:3]}")

    # 整段 drawMacd 的两态（与 ① 的静态闸互为印证）
    if base_name in results:
        spec, out = results[base_name]
        n_arrow = len(out["arrows"])
        check(failures, out["macdNoVal"] == n_arrow,
              "drawMacd 不传 valOf ⇒ 箭头数与单独调用一致（正例）",
              f"{out['macdNoVal']} vs {n_arrow}")
        swapped = [{"dif": k.get("dea"), "dea": k.get("dif"),
                    "macd": k.get("macd")} for k in spec["klines"]]
        _check_one(failures, "drawMacd + valOf（类MACD 形态）",
                   dict(spec, klines=swapped), {"arrows": out["valArrows"]}, h)
        check(failures, out["valFlat"] == 0,
              "valOf 返回常量（永不交叉）⇒ 0 箭头：没有回落到 K 线自身的 dif/dea",
              f"实画 {out['valFlat']}")
        # 同一批 K 线（无 vm 字段）走类MACD：量能侧 0 交叉 ⇒ 0 箭头；
        # 价格侧照常画 —— 两槽各判各的，成交额/量的分支不会把价格交叉复制一份。
        check(failures, len(out["priceArrows"]) == n_arrow and len(out["volArrows"]) == 0,
              "类MACD 与价格MACD 互不串槽（无 vm ⇒ 量能 0 箭头；价格箭头数不变）",
              f"price={len(out['priceArrows'])} vol={len(out['volArrows'])}")
        check(failures, out["degenerate"] == 0,
              "空数组 / 单根 / 绘图窗过矮 ⇒ 零箭头且不抛异常",
              f"实画 {out['degenerate']}")


# ═══════════════════════════════════════════════════════════════════
# ②b 类MACD（成交额/量）也画箭头 —— 真执行 drawVolumeMacd
# ═══════════════════════════════════════════════════════════════════
def test_volume_macd_arrows(failures):
    print("\n②b 类MACD 箭头（drawVolumeMacd 真执行：判据跟着 volMacdOf 走）")
    js = read(APP_JS)
    macd = fn_src(js, "drawMacd")
    arrows = fn_src(js, "drawMacdCrossArrows")
    volmacd = fn_src(js, "drawVolumeMacd")
    volof = fn_src(js, "volMacdOf")
    if not (macd and arrows and volmacd and volof):
        check(failures, False, "②b 前置：drawMacd / 箭头 / drawVolumeMacd / volMacdOf 都能抽出",
              f"{bool(macd)}/{bool(arrows)}/{bool(volmacd)}/{bool(volof)}")
        return
    up, down = colors_of(js)
    _h_lit, h = const_decl(js, "MACD_CROSS_H")
    _hw_lit, hw = const_decl(js, "MACD_CROSS_HW")
    colors = {"macdUp": up, "macdDown": down}

    node = find_node()
    if not node:
        print("  [SKIP] 未找到 node（② 已覆盖几何）")
        return

    # 价格侧**全程等值**（dif == dea，永不交叉）；量能侧 3 金叉 / 2 死叉。
    #   若箭头来自价格字段 ⇒ 一支都画不出；若来自 volMacdOf ⇒ 恰好 5 支。
    vm = [(-1.0, -0.5), (-0.5, -0.6), (0.0, -0.3), (0.2, 0.1), (0.1, 0.3),
          (0.0, 0.0), (0.0, 0.0), (0.3, 0.0), (0.5, 0.2), (0.4, 0.6),
          (0.2, 0.4), (0.6, 0.3)]
    klines = [{"dif": 0.5, "dea": 0.5, "macd": 0.0,
               "vm": {"dif": d, "dea": e, "macd": (d - e) * 2}} for d, e in vm]
    area = _area()
    rng = {"min": -1.2, "max": 0.8}
    spec = _spec(klines, area, rng=rng, colors=colors)
    tmp = tempfile.mkdtemp(prefix="macd_vol_arrows_")
    drv = os.path.join(tmp, "driver.js")
    with open(drv, "w", encoding="utf-8") as f:
        f.write(f"const MACD_CROSS_H = {h};\n")
        f.write(f"const MACD_CROSS_HW = {hw};\n")
        f.write(volof + "\n")
        f.write(volmacd + "\n")
        f.write(arrows + "\n" + macd + "\n" + NODE_DRIVER)
    out, err = _run_node(node, drv, spec, tmp)
    if out is None:
        check(failures, False, "②b node 执行成功", err)
        return

    # 量能箭头 = 量能值算出的交叉（用同一套几何断言，spec 换成 vm 序列）
    vm_series = [{"dif": d, "dea": e} for d, e in vm]
    _check_one(failures, "类MACD（量能）",
               dict(spec, klines=vm_series), {"arrows": out["volArrows"]}, h)
    check(failures, len(out["priceArrows"]) == 0,
          "价格侧 dif == dea（永不交叉）⇒ 价格槽 0 箭头：箭头确实来自 volMacdOf，"
          "不是把价格交叉复制了一份", f"实画 {len(out['priceArrows'])}")

    # 翻转视图：量能槽同样「朝向 + 颜色一起翻」
    sp_m = dict(spec, mirror=True)
    out_m, err = _run_node(node, drv, sp_m, tmp)
    if out_m is None:
        check(failures, False, "②b 翻转视图 node 执行成功", err)
        return
    _check_one(failures, "类MACD（量能·翻转）",
               dict(sp_m, klines=vm_series), {"arrows": out_m["volArrows"]}, h)
    check(failures, len(out_m["volArrows"]) == len(out["volArrows"]),
          "类MACD 翻转视图箭头数不变（只翻朝向/颜色，不增不减）",
          f"{len(out_m['volArrows'])} vs {len(out['volArrows'])}")

# ═══════════════════════════════════════════════════════════════════
# ③ 真渲染（无头 Chrome）
# ═══════════════════════════════════════════════════════════════════
FILL_RECORDER_JS = r"""
window.__fills = [];
// 不建实时流：本用例只验渲染，长连接会让打桩的路由一直挂着
window.EventSource = function () {
  this.readyState = 0; this.url = ''; this.withCredentials = false;
  this.close = function () {};
  this.addEventListener = function () {};
  this.removeEventListener = function () {};
  this.dispatchEvent = function () { return false; };
};
(function () {
  var orig = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type) {
    var ctx = orig.apply(this, arguments);
    if (type === '2d' && ctx && !ctx.__fillWrapped) {
      ctx.__fillWrapped = true;
      var pts = null;
      var mv = ctx.moveTo, lt = ctx.lineTo, bp = ctx.beginPath, fl = ctx.fill;
      ctx.beginPath = function () { pts = []; return bp.apply(this, arguments); };
      ctx.moveTo = function (x, y) {
        if (pts) pts.push([x, y]);
        return mv.apply(this, arguments);
      };
      ctx.lineTo = function (x, y) {
        if (pts) pts.push([x, y]);
        return lt.apply(this, arguments);
      };
      ctx.fill = function () {
        try {
          var par = this.canvas && this.canvas.parentElement;
          if (pts && par && par.id === 'chart-container') {
            window.__fills.push({ style: String(this.fillStyle), pts: pts.slice() });
          }
        } catch (e) {}
        pts = null;
        return fl.apply(this, arguments);
      };
    }
    return ctx;
  };
})();
"""


def _launch_browser(pw):
    notes = []
    try:
        return pw.chromium.launch(channel="chrome"), "本机 Chrome", notes
    except Exception as e:
        notes.append("channel=chrome: " + str(e).splitlines()[0][:90])
    try:
        return pw.chromium.launch(), "捆绑 chromium", notes
    except Exception as e:
        notes.append("捆绑 chromium: " + str(e).splitlines()[0][:90])
    pats = ["~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe",
            "~/AppData/Local/ms-playwright/chromium-*/chrome-win/chrome.exe",
            "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"]
    for pat in pats:
        for cand in glob.glob(os.path.expanduser(pat)):
            try:
                return (pw.chromium.launch(executable_path=cand),
                        os.path.basename(os.path.dirname(cand)), notes)
            except Exception as e:
                notes.append(os.path.basename(cand) + ": " + str(e).splitlines()[0][:90])
    return None, "", notes


def test_real_render(failures):
    print("\n③ 真渲染（无头 Chrome：同一帧记 fill() 序列，反查交叉根）")
    pl, why = find_playwright()
    if pl is None:
        print(f"  [SKIP] {why}（①② 已覆盖契约与几何）")
        return
    from playwright.sync_api import sync_playwright

    if not check(failures, os.path.isfile(SNAP_STOCK), "股票快照存在", SNAP_STOCK):
        return
    snap_text = open(SNAP_STOCK, encoding="utf-8").read()
    klines = json.loads(snap_text)["klines"]

    js = read(APP_JS)
    up, down = colors_of(js)
    h_lit, h = const_decl(js, "MACD_CROSS_H")
    m_pad = re.search(r"const PADDING = \{ top: (\d+), right: (\d+), bottom: (\d+), left: (\d+) \};", js)
    m_vol = re.search(r"const VOL_RATIO = ([\d.]+), GAP = (\d+);", js)
    m_txt = re.search(r"const MACD_TEXT_HEIGHT = (\d+);", js)
    m_gap = re.search(r"const rightGap = (\d+);", js)
    m_slot = re.search(r"const SLOT_GAP = (\d+);", js)
    m_vc = re.search(r"let VIEW_COUNT = (\d+);", js)
    if not check(failures, all((m_pad, m_vol, m_txt, m_gap, m_slot)),
                 "布局常量可从源码解析（测试与实现同源，不各写各的魔法数字）"):
        return
    top, right, bottom, left = (int(m_pad.group(i)) for i in (1, 2, 3, 4))
    vol_ratio, gap = float(m_vol.group(1)), int(m_vol.group(2))
    text_h, right_gap, slot_gap = int(m_txt.group(1)), int(m_gap.group(1)), int(m_slot.group(1))
    view_count = int(m_vc.group(1)) if m_vc else 233

    import functools
    import http.server
    import threading

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=FRONTEND_DIR))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    page_errors = []
    with sync_playwright() as pw:
        browser, which, notes = _launch_browser(pw)
        if browser is None:
            print("  [SKIP] 无可用无头浏览器：" + " / ".join(notes[:3]))
            httpd.shutdown()
            return
        print(f"  （无头浏览器：{which}）")
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        page.add_init_script(FILL_RECORDER_JS)
        page.on("pageerror", lambda e: page_errors.append(str(e)[:160]))

        def route_api(route):
            url = route.request.url
            if "/api/health" in url:
                body = json.dumps({"config": {"view_count": view_count}})
            elif "analyze" in url:
                body = snap_text
            else:
                body = "{}"
            route.fulfill(status=200, content_type="application/json", body=body)

        page.route("**/api/**", route_api)
        page.goto(f"http://127.0.0.1:{port}/app.html", wait_until="load")
        page.wait_for_function(
            "() => { const s = window.ChanApp && window.ChanApp.state;"
            " return !!(s && s.chartData && s.chartData.klines && s.chartData.klines.length > 0); }",
            timeout=20000)
        page.wait_for_timeout(500)

        box = page.locator("#chart-container canvas").first.bounding_box()
        ok_box = check(failures, box is not None and box["width"] > 200,
                       "页面加载真实快照并完成首屏渲染", f"box={box}")
        fills = page.evaluate("() => window.__fills") if ok_box else []
        page.unroute_all(behavior="ignore")
        browser.close()
        httpd.shutdown()

    if page_errors:
        print(f"  （非致命 JS 错误 {len(page_errors)} 条，仅回报不判红）")
        for e in page_errors[:2]:
            print("    " + e)
    if not ok_box:
        return

    W, H = box["width"], box["height"]
    net_h = H - top - bottom - gap
    s0_top = top + net_h * (1 - vol_ratio * 2) + text_h
    s0_bot = s0_top + (net_h * vol_ratio * 2 - 2 * text_h - slot_gap) / 2
    area_x = left
    area_w = W - left - right - right_gap
    eff = min(len(klines), view_count)
    bar_step = area_w / eff

    def _in_slot(f):
        return all(s0_top - 1 <= p[1] <= s0_bot + 1 for p in f["pts"])

    ups = [f for f in fills if norm_color(f["style"]) == norm_color(up) and _in_slot(f)]
    dns = [f for f in fills if norm_color(f["style"]) == norm_color(down) and _in_slot(f)]
    check(failures, len(ups) >= 3 and len(dns) >= 3,
          "MACD 槽内已画出金叉红箭头与死叉绿箭头（各 ≥3）",
          f"红 {len(ups)} / 绿 {len(dns)}")

    def _is_arrow(f, want_up):
        if len(f["pts"]) != 7:
            return False
        ys = [p[1] for p in f["pts"]]
        if abs((max(ys) - min(ys)) - h) > 0.6:
            return False
        if want_up:
            return ys[0] == min(ys) and ys.count(min(ys)) == 1
        return ys[0] == max(ys) and ys.count(max(ys)) == 1

    check(failures, bool(ups) and all(_is_arrow(f, True) for f in ups),
          "红箭头：7 点、尖端朝上、总高 == MACD_CROSS_H")
    check(failures, bool(dns) and all(_is_arrow(f, False) for f in dns),
          "绿箭头：7 点、尖端朝下、总高 == MACD_CROSS_H")

    arrows = [(f["pts"][0][0], "gold") for f in ups] + [(f["pts"][0][0], "death") for f in dns]
    local = [((x - area_x - bar_step / 2) / bar_step, kind) for x, kind in arrows]
    check(failures, all(abs(li - round(li)) < 0.02 for li, _ in local),
          "箭头横向落点对齐到 K 线柱心（不偏半根）",
          f"{[round(li, 2) for li, _ in local][:6]}")

    best = None
    for start in range(0, max(1, len(klines) - eff + 1)):
        ok = True
        for li, kind in local:
            g = start + int(round(li))
            if g < 1 or g >= len(klines):
                ok = False
                break
            d1, e1 = _num(klines[g].get("dif")), _num(klines[g].get("dea"))
            d0, e0 = _num(klines[g - 1].get("dif")), _num(klines[g - 1].get("dea"))
            if None in (d0, e0, d1, e1):
                ok = False
                break
            if kind == "gold" and not (d1 > e1 and d0 <= e0):
                ok = False
                break
            if kind == "death" and not (d1 < e1 and d0 >= e0):
                ok = False
                break
        if ok:
            best = start
            break
    check(failures, best is not None,
          "每支箭头都落在真实交叉根上（按快照 dif/dea 逐支反查）",
          f"{len(arrows)} 支无法同时对齐")
    check(failures, best == max(0, len(klines) - eff),
          "箭头只画在当前可视窗口内（对齐起点 == len(klines) − view_count）",
          f"对齐起点 {best} / 期望 {max(0, len(klines) - eff)}")


# ═══════════════════════════════════════════════════════════════════
def main():
    print("=" * 64)
    print("MACD 金叉 / 死叉箭头 —— 判定口径 + 几何落点 + 真渲染")
    print("=" * 64)
    failures = []
    test_source_contract(failures)
    test_geometry(failures)
    test_volume_macd_arrows(failures)
    test_real_render(failures)
    print("=" * 64)
    print(f"合计失败 {len(failures)} 项")
    for f in failures:
        print("  - " + f)
    print("=" * 64)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

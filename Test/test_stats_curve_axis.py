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
 [E] 纵轴**每根刻度都带单位**（元 / 万元 / 百万元），且**整条轴只用一档**
     （2026-09-19 用户：「纵坐标需要添加单位，也不过万→元、过万→万元、
     过百万→百万元」）。逐条各自判档会得到"0 元 / 50.00 万元 / 1.00 百万元"
     这种同轴混单位 —— 刻度之间反而不能直接比大小，所以按**整条轴的量级**
     选一档。档位规则与统计面板「总净盈亏」共用同一个 moneyScale()：本文件
     把轴标签的数值反解回元，逐场景核对它落在哪一档、并与面板字面比对。
     小数位数跟步长走，且必须**能把这个步长写准**：步长 0.25 百万元时只给
     1 位小数会把 0.25/0.5/0.75 写成 0.3/0.5/0.8（等于刻度值全是错的）。
     末端标签与面板同源（money()）—— 曲线末端就是总净盈亏那个数。
 [B] 横轴：有轴线、有刻度线、有日期标签（取该笔的 exit_at）。
     **标签条数恒为 3（首 / 中 / 尾），与笔数无关** —— 2026-09-19 用户提问
     「以后有 1000 笔，横轴咋显示？是不是借鉴市场量能的横轴设计？」：
     旧实现按可用宽度等分、上限 5 条，笔数一变落点就换一套、条数也跟着变；
     现改为与「市场量能」面板同一条口径，1000 笔也只给 index 0 / 499 / 999
     三个，永远不会挤。跨天给 YY-MM-DD（直接复用市场量能的 fmtAxisDate，
     本文件会交叉比对两者输出是否一致），整条曲线落在同一天时降级为 HH:MM。
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

import datetime
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
# 抽出真实代码段：moneyScale / money / eqAxisTicks / eqAxisNum /
#                eqAxisDate / drawEquityCurve
# money / moneyScale 一起抽：纵轴单位与面板金额共用这一份实现，
# 抽进来才能证明「轴上的"3.40 百万元"与面板的"3.40 百万元"是同一个函数算的」。
# ══════════════════════════════════════════════════════════════
def extract_fmt_date():
    """抽出「市场量能」面板的 fmtAxisDate。

    eqAxisDate 跨天时直接调它 —— 两个面板的日期写法因此只有一个来源。
    抽出来一起喂给 node 桩，否则桩里会 ReferenceError。
    """
    js = open(APP_JS, encoding="utf-8").read()
    m = re.search(r"^        function fmtAxisDate\(d\) \{.*?\n        \}",
                  js, re.S | re.M)
    assert m, "抽不到 fmtAxisDate（市场量能的日期格式函数）"
    return m.group(0)


def extract_block():
    js = open(APP_JS, encoding="utf-8").read()
    s = js.index("        function moneyScale(")
    e = js.index("function updateSlider()")
    blk = js[s:e]
    assert "function drawEquityCurve(curve) {" in blk, "抽出的段落里没有 drawEquityCurve"
    # 横轴日期必须继续走市场量能那一套，不许自己再写一份格式
    assert "return fmtAxisDate(" in blk, \
        "eqAxisDate 跨天分支没有引用 fmtAxisDate（与市场量能同源已断）"
    # 只数「8 空格缩进的顶层声明」：drawEquityCurve 内部还有 yOf / xOf 两个内嵌函数，
    # 按 "function " 子串数会多出来。
    tops = re.findall(r"^        function (\w+)\(", blk, re.M)
    assert tops == ["moneyScale", "money", "eqAxisTicks", "eqAxisNum",
                    "eqAxisDate", "drawEquityCurve"], \
        "顶层函数不是预期的 6 个：%s" % tops
    assert blk.count("{") == blk.count("}"), "大括号不配对"
    return blk


# ══════════════════════════════════════════════════════════════
# [E] 缩位规则的「唯一实现」静态检查
# 纵轴单位与面板总净盈亏必须是**同一个函数**算出来的：只要有人在别处再抄
# 一份 if (a >= 1e4) "万元" 的分支，下面第一条就会红。
# ══════════════════════════════════════════════════════════════
def static_source_checks():
    src = open(APP_JS, encoding="utf-8").read()
    blk = extract_block()
    check(src.count('"万元"') == 1 and src.count('"百万元"') == 1,
          "[E] 档位字面全仓只出现一次（缩位规则只有 moneyScale 一份）",
          (src.count('"万元"'), src.count('"百万元"')))
    check(src.count("function moneyScale(") == 1 and src.count("function money(") == 1,
          "[E] moneyScale / money 各只有一处定义",
          (src.count("function moneyScale("), src.count("function money(")))
    check("var sc = moneyScale(" in blk,
          "[E] 纵轴单位取自 moneyScale（与面板同源，不是轴自己判档）")
    check("var tag = money(lastV);" in blk,
          "[E] 末端标签调 money()（与面板总净盈亏同一格式）")
    m = re.search(r"^        function eqAxisNum\(v, step, sc\) \{\n(.*?)\n        \}\n",
                  blk, re.S | re.M)
    check(m is not None, "[E] eqAxisNum 接收单位参数 sc")
    body = m.group(1) if m else ""
    check("1e4" not in body and "1e6" not in body and "1e8" not in body,
          "[E] eqAxisNum 内没有自己写死的档位阈值（档位只在 moneyScale）")
    check('+ " " + unit' in body, "[E] eqAxisNum 输出的每根刻度都带单位")


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
    # [E] 过万 → 轴切「万元」档（不过万的那几场景用「元」档；同一个函数、
    # 同一个面板，档位跟着轴的量级走）
    ("过万 6 笔", curve_of([4000, 3000, -1500, 5000, 2000, 6500])),
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

# 1000 笔 —— 用户直接问到的场景（「以后有 1000 笔，横轴咋显示？」）。
# 日期按天铺开近 3 年，三条标签必然互不相同，能真正验出"只给首/中/尾"。
_D0 = datetime.date(2026, 1, 1)
SCENARIOS.append((
    "千笔长序列",
    curve_of([(220 if i % 3 else -310) for i in range(1000)],
             dates=[(_D0 + datetime.timedelta(days=i)).isoformat()
                    for i in range(1000)]),
))


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
    # 三家单位与 app.js moneyScale 的三档一一对应（元 / 万元 / 百万元）
    UNIT = {"元": 1.0, "万元": 1e4, "百万元": 1e6}

    def num(s):
        # [E] 刻度一律是「<数值> <单位>」；单位只允许这三档，别的一律解析不出
        m = re.fullmatch(r"(-?)(\d+(?:\.\d+)?) (元|万元|百万元)", s)
        if not m:
            return None
        v = float(m.group(2)) * UNIT[m.group(3)]
        return -v if m.group(1) else v

    def unit_of(s):
        m = re.fullmatch(r"-?\d+(?:\.\d+)? (元|万元|百万元)", s)
        return m.group(1) if m else None

    gx = min(t["x"] for t in texts)
    ylab = [t for t in texts if abs(t["x"] - gx) < 0.6]
    # 条数只保证"不止 max/min 两个"（3~7）—— 具体几条由数据跨度决定；
    # 真正把旧实现拦下来的是下面几条：值不重复（旧版 0 画两遍）、有网格线、
    # 每根刻度都带单位。
    need(3 <= len(ylab) <= 7, "纵轴刻度值 3~7 个（不是只有 max/min 两个）", len(ylab))
    vals = [t["t"] for t in ylab]
    need(len(set(vals)) == len(vals), "纵轴刻度值互不重复（0 不再被画两遍）", vals)

    nums = [num(v) for v in vals]
    need(all(x is not None for x in nums), "纵轴刻度都是可解析的数值", vals)
    if all(x is not None for x in nums):
        # ── [E] 纵轴单位（2026-09-19）─────────────────────────
        units = [unit_of(v) for v in vals]
        need(all(u is not None for u in units),
             "每根纵轴刻度都带单位（元 / 万元 / 百万元）", vals)
        need(len(set(units)) == 1, "整条轴只用一档单位（不混档）", units)
        need(sum(1 for x in nums if x == 0) == 1, "纵轴恰好一个 0", vals)
        # 档位由**整条轴的量级**决定，规则与 moneyScale 逐字一致
        big = max(abs(x) for x in nums)
        want_unit = "百万元" if big >= 1e6 else ("万元" if big >= 1e4 else "元")
        need(units[0] == want_unit,
             "单位档位 = 轴量级（不过万→元 / 过万→万元 / 过百万→百万元）",
             "轴最大 %.0f / 用了 %s / 应为 %s" % (big, units[0], want_unit))
        need(nums == sorted(nums) and len(set(nums)) == len(nums),
             "纵轴刻度单调递增", nums)
        # 小数位够不够：真实刻度是等距的，位数不足被四舍五入（0.25→0.3）时
        # 解析回来的间距就会不齐 —— 这条专抓"刻度值被写错"。
        gaps = [round(nums[i + 1] - nums[i], 6) for i in range(len(nums) - 1)]
        tol = max(1e-3, abs(nums[-1]) * 1e-9)
        need(bool(gaps) and (max(gaps) - min(gaps)) <= tol,
             "相邻刻度等距（小数位数足够写准该步长）", gaps)
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
    # plotBottom == 绘图区底边：轴范围就是刻度边界，最低那条刻度必然落在底边上。
    plotBottom = max(t["y"] for t in ylab)
    vline = [s for s in segs if abs(s[0] - s[2]) < 0.6 and abs(s[3] - s[1]) > 30]
    need(len(vline) >= 1, "有纵轴线（不是只有浮空的数字）", len(vline))
    hl = [s for s in segs if abs(s[0] - s[2]) > 30 and abs(s[3] - s[1]) < 0.6]
    need(len(hl) >= 4, "有网格线（≥4 条）", len(hl))
    need(any(abs(s[1] - plotBottom) < 1.0 for s in hl),
         "横轴线画在绘图区底部", [round(s[1], 1) for s in hl])

    # 标签条数**与笔数无关**：恒为 3（首 / 中 / 尾），n<3 时有几笔给几笔。
    # 刻度线只认「从绘图区底边往下伸 4px」那一段 —— 1000 笔时曲线每一步只有
    # 零点几像素宽，拿「竖且短」当特征会把曲线段误判成刻度线。
    want_n = min(3, n)
    ticks = [s for s in segs if abs(s[0] - s[2]) < 0.6
             and abs(s[1] - plotBottom) < 1.0 and abs(s[3] - (plotBottom + 4)) < 1.0]
    need(len(ticks) == want_n,
         "横轴刻度线恒为 %d 根（与笔数 %d 无关）" % (want_n, n), len(ticks))

    xlab = [t for t in texts if t["y"] > plotBottom + 2]
    need(len(xlab) == want_n,
         "横轴日期标签恒为 %d 个（与笔数 %d 无关）" % (want_n, n), len(xlab))
    if xlab:
        # 整条曲线同一天 → 给时刻 HH:MM（同日的三个 "26-09-18" 并排没有信息量）；
        # 跨天 → 给 YY-MM-DD（与市场量能的 fmtAxisDate 同源，见文件头 [B]）。
        same_day = curve[0]["exit_at"][:10] == curve[-1]["exit_at"][:10]
        pat = r"\d{2}:\d{2}" if same_day else r"\d{2}-\d{2}-\d{2}"
        need(all(re.fullmatch(pat, t["t"]) for t in xlab),
             "横轴标签是 %s" % ("HH:MM（同日）" if same_day else "YY-MM-DD"),
             [t["t"] for t in xlab])
        xs = [t["x"] for t in xlab]
        need(xs == sorted(xs) and len(set(xs)) == len(xs),
             "横轴标签 x 互不重叠且按序", [round(x, 1) for x in xs])
        need(len(set(t["t"] for t in xlab)) == len(xlab),
             "横轴标签文字互不重复", [t["t"] for t in xlab])
        padL = gx + 6
        padR = 10
        ned = w - padR
        plotW = ned - padL
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
        # 落点必须是 首 / 中 / 尾 三个 index，标签内容取自那一笔的 exit_at（不是编的）
        exp_idx = sorted(set([0, (n - 1) // 2, n - 1])) if n > 1 else [0]
        bad, got_idx = [], []
        for t in xlab:
            idx = 0 if n <= 1 else round((t["x"] - padL) / plotW * (n - 1))
            idx = max(0, min(n - 1, idx))
            got_idx.append(idx)
            want = curve[idx]["exit_at"][11:16] if same_day \
                else curve[idx]["exit_at"][2:10]
            if t["t"] != want:
                bad.append((idx, t["t"], want))
        need(got_idx == exp_idx, "落点是 首/中/尾 三个 index", (got_idx, exp_idx))
        need(not bad, "标签取的是该笔的 exit_at", bad)

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
    # 特征不能再取"以 元 结尾"：纵轴刻度现在也带单位、也以这些单位结尾。
    # 末端标签既不在纵轴槽（x == gx）里、也不在横轴下方，取补集即可。
    tail = [t for t in texts if t not in ylab and t not in xlab]
    need(len(tail) == 1, "末端有且只有一个「累计 …」标签", [t["t"] for t in tail])
    if len(tail) == 1:
        finite = [c["cumulative"] for c in curve
                  if isinstance(c["cumulative"], (int, float))
                  and c["cumulative"] == c["cumulative"]]
        lastv = float(finite[-1]) if finite else 0.0
        # 末端值就是总净盈亏（TradeStats.py:197 / :213-222），面板与曲线该给同一串；
        # 这里把 money() 的规则在 Python 侧重写一遍再逐字比对，防两处各写各的。
        a = abs(lastv)
        div, unit = ((1e6, "百万元") if a >= 1e6
                     else (1e4, "万元") if a >= 1e4 else (1.0, "元"))
        mt = re.fullmatch(r"-?\d+\.\d{2} (元|万元|百万元)", tail[0]["t"])
        need(mt is not None,
             "末端标签 = 两位小数金额 + 单位（与面板 money() 同构）", tail[0]["t"])
        if mt:
            need(mt.group(1) == unit, "末端标签单位档位 = 自身量级",
                 "%s vs %s（末端累计 %.2f）" % (mt.group(1), unit, lastv))
            tv = num(tail[0]["t"])
            need(tv is not None
                 and abs(tv - round(lastv / div, 2) * div) <= div * 0.005 + 1e-6,
                 "末端标签数值 = 末端累计值按该档缩位到 2 位小数",
                 "%s vs %.2f" % (tail[0]["t"], lastv))
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

/*__DEPS__*/

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
// 跨面板交叉校验：曲线横轴的日期串必须与「市场量能」的 fmtAxisDate 逐字一致
// （同一个日期，两个面板不许给出两种写法）。
var cross = [];
["2026-09-18", "2026-01-01", "2027-05-15", "2028-09-26"].forEach(function (d) {
  cross.push([d, fmtAxisDate(d), eqAxisDate(d + " 10:00:00", false)]);
});
// 金额缩位交叉校验：纵轴刻度与面板「总净盈亏」共用 moneyScale / money ——
// 把两者对同一批量级的结果都带出来，跟 Python 侧重写的规则逐字比对。
// （同一份实现 + 两侧独立重算 = "改了一处忘另一处"会被立刻发现。）
var money_cases = [];
[0, 1, 9999, 10000, 999999, 1000000, 3400, 34000, 3400000, -34000,
 -3400000, 1234567.89, -1234567.89, 99999999].forEach(function (v) {
  var sc = moneyScale(v);
  money_cases.push([v, money(v), sc.div, sc.unit]);
});
console.log(JSON.stringify({ scenarios: out, cross: cross, money: money_cases }));
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
        dep = extract_fmt_date()
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
            NODE_DRIVER.replace("/*__DEPS__*/", dep).replace("/*__BLOCK__*/", blk))
        # 用 module.exports 而不是 JSON 文件：坏值场景里可能有 NaN，
        # 那是合法 JS 但不是合法 JSON。
        open(spec, "w", encoding="utf-8").write(
            "module.exports = "
            + json.dumps([{"name": n, "curve": c} for n, c in SCENARIOS],
                         allow_nan=True)
            + ";\n")
        proc = subprocess.run([node, drv, spec], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
        if proc.returncode != 0:
            check(False, "node 执行成功", (proc.stderr or "")[-400:])
            return
        check(True, "node 执行成功")
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    for d, want, got in payload["cross"]:
        check(want == got, "跨面板一致：%s → 市场量能 '%s' / 曲线 '%s'"
              % (d, want, got))
    # 金额缩位：JS 的 money()/moneyScale() 与 Python 侧重写的规则逐条比对。
    # 这是"纵轴单位与面板「总净盈亏」同一档"的实证 —— 两边同时对才可能全绿。
    for v, text, div, unit in payload["money"]:
        a = abs(float(v))
        w_div, w_unit = ((1e6, "百万元") if a >= 1e6
                         else (1e4, "万元") if a >= 1e4 else (1.0, "元"))
        check(w_div == div and w_unit == unit,
              "缩位档位一致：%g → %s" % (v, unit), (unit, w_unit))
        want = "%.2f %s" % (float(v) / w_div, w_unit)
        check(text == want, "缩位字面一致：%g → JS '%s' / 规则 '%s'"
              % (v, text, want))
    for (name, curve), rec in zip(SCENARIOS, payload["scenarios"]):
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
        page.goto("http://127.0.0.1:%d/app.html" % port, wait_until="load")
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
            if shot_dir and name in ("全正 6 笔", "全负 5 笔", "7 位数末端",
                                     "千笔长序列", "过万 6 笔"):
                os.makedirs(shot_dir, exist_ok=True)
                page.locator("#stats-panel").screenshot(
                    path=os.path.join(shot_dir, "after_%s.png" % name.replace(" ", "")))
        page.unroute_all(behavior="ignore")
        browser.close()
    httpd.shutdown()


# ══════════════════════════════════════════════════════════════
print("[静态源码] 金额缩位（元 / 万元 / 百万元）只有一处实现")
static_source_checks()
print("\n[静态] node + canvas 桩：抽出 app.js 真实代码段跑逐场景几何审计")
run_static_layer()
print("\n[真渲染] 无头 Chrome + 真实 measureText")
run_render_layer()

print("\n" + "=" * 62)
print("盈亏曲线坐标轴契约 结果: %s"
      % ("全部通过" if not _FAIL else "%d 项失败 -> %s" % (len(_FAIL), _FAIL[:6])))
print("=" * 62)
sys.exit(1 if _FAIL else 0)

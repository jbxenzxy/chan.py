# -*- coding: utf-8 -*-
"""「RSI」扫描模式（scan mode = "rsi"）护栏
====================================================================
需求（2026-10-12 第二轮，用户指定）：
  扫描模式第一行「连涨」之后新增「RSI」；勾选后判「最近 N 根 K 线的 RSI 值
  是否越过超买 / 超卖阈值」，结果面板中「小于 20 的排在大于 80 的前面」。

判据（`AppScan.Scanner.scan_one(mode="rsi")`，后端为唯一事实源）：
  ① 先在全量收盘序列上算出整条 RSI（周期 = RSI_CYCLE，与 K 线图副图同一条线），
     再取最近 N 根窗口 win = rsi[-N:] —— 只对窗口内 N 根单独算会得到与图上
     不同的数（前 period-1 根走的是简单平均播种）；
  ② 命中 ⟺ win 内**任一根** v >= 超买阈值 或 v <= 超卖阈值（**含边界**）；
  ③ 归类（决定结果面板分组）：
       ① 末根 <= 超卖 → 超卖组 / 末根 >= 超买 → 超买组（末根直接决定）；
       ② 末根落回开区间（中性）→ 窗口内出现过超卖即归超卖组，否则超买组
          （此时必然出现过超买）—— 即中性时**超卖优先**；
  ④ 静默未命中（不抛异常、不进结果列表、由页面计入「跳过」）的两种情况：
       · K 线数 < N + RSI_CYCLE ⇒ 窗口会落进 RSI 播种区（窗口起点 L-N 必须 ≥ RSI_CYCLE）；
       · 全序列一个价都没变（停牌 / 一字板 / 无成交）⇒ RSI 走公式边界分支得
         退化值 0，那是退化而不是超卖（副图同样画出贴 0 平线，但不该报命中）。

输出字段：is_rsi / rsi_group / rsi_last / rsi_min / rsi_max /
          rsi_hit_under / rsi_hit_over / recent_days / freq / last_close。

阈值同源（值级，不是注释级）——
  Python 侧 `AppUtils.RSI_OVERBOUGHT / RSI_OVERSOLD` = max / min(`RSI_REF_VALUES[1:]`)；
  前端 `app.js` `RSI_SEG_OVER / RSI_SEG_UNDER` = max / min(`RSI_REF_VALUES.slice(1)`)
  —— 两侧**都只取三元组里的阈值对**（[中轴 50, 超买, 超卖] 的后两位），派生式逐字同形：
  把中轴算进来会在「超买 < 50 / 超卖 > 50」时把取色边界顶在中轴，与后端判据分叉。
  （2026-10-14 订正：此前 Python 侧写的是 `max(RSI_REF_VALUES)` —— 对整个三元组取 ——
   默认 (50,80,20) 下与 `[1:]` 取值相同故从未翻红，但两种派生式并存本身就是分叉隐患，
   现统一为 `[1:]`；本段的三条断言随之改成「必须是 `[1:]` 形态」的可判别形式。）
  两侧同值 ⇒ 「图上画成超买红」与「扫描报超买」永远同判据。② 段用
  **跨语言解析**把这条钉死。

四段守护 ——
  ① 后端判据（合成 K 线，手算期望，经真实 scan_one 打桩驱动）：命中 / 未命中 /
     含边界（窗口内**恰好 20.0 与 80.0** 各一例，判据写宽写窄都会改变命中根数）/
     归类三分支（①末根 / ②中性兜底，含「两个都命中时超卖优先」）/ 门槛两条 /
     N=1 与 N 超长 / 返回字段与裸码约定；
  ② 阈值同源与判据方向（源码锚点 + 真导入 + 跨语言同值 + rsi 分支内无 80/20 副本）；
  ③ 前端真函数（node 抽 app.js 真代码）：排序（组间超卖全前 + 组内按极值）/
     标签文本与配色类 / 披露行（阈值随变量走、只留判据句）/ 空结果态 / 缺字段兜底
     （与后端同源分层：行内阈值 → 末根 → 命中方向）；
  ④ 接线完整性：模式枚举 / 持久化白名单 / 自动补 3 / 标题 / runScan 分支指向 /
     app.html 选项与 DOM 顺序。

样本：合成 K 线（手算期望；每个用例都先在 `Test/fixtures` 外经真实 `scan_one`
打桩复核过一轮再固化）。跑法：`python Test/test_scan_rsi_mode.py`（退出码 0/1 即判决）
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

from App import AppScan as _sc                              # noqa: E402
from App.AppUtils import (                                  # noqa: E402
    RSI_CYCLE,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_REF_VALUES,
    calculate_rsi,
)

APPJS = os.path.join(ROOT, "Frontend", "app.js")
APPHTML = os.path.join(ROOT, "Frontend", "app.html")
APPUTILS = os.path.join(ROOT, "App", "AppUtils.py")
APPSCAN = os.path.join(ROOT, "App", "AppScan.py")

_OK = []


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  \u2014\u2014 " + str(detail)[:260]))
    return cond


# ── 桩驱动：把 analyze_stock 换成本地 K 线（不改被测逻辑）────────────
#   与 test_scan_fangliang_mode.py / test_scan_lianzhang_mode.py 同一手法：
#   scan_one 消费的就是 result["klines"] / result["meta"]，桩喂的数据与真路径同形。
def _run(klines, recent="3", name="\u6d4b\u8bd5\u80a1", code="002190",
         prefix="0", freq="d", mode="rsi"):
    def _stub(qualified_code, **kw):
        return {"klines": klines, "meta": {"name": name}, "bsps": [], "bis": []}

    orig = _sc._m.analyze_stock
    _sc._m.analyze_stock = _stub
    try:
        return _sc.scanner.scan_one(code, freq=freq, prefix=prefix,
                                    recent=str(recent), source="zxg", mode=mode)
    finally:
        _sc._m.analyze_stock = orig


def _mk(closes, start_day=1):
    """收盘价序列 → klines（RSI 判据只看 close；OHLC 取同值够用）。"""
    return [{"date": "d%04d" % (start_day + i), "open": c, "close": c,
             "high": c, "low": c, "vol": 1, "amount": 1.0}
            for i, c in enumerate(closes)]


def _seg(start, n, step):
    return [round(start + step * i, 4) for i in range(n)]


# ══════════════════════════════════════════════════════════════════════
# ① 后端判据（合成 K 线，手算期望）
# ══════════════════════════════════════════════════════════════════════
def part1():
    print("══ ① 后端判据：窗口内任一根越过阈值即命中（含边界）══")

    # ── 纯超卖 / 纯超买（单调序列；15 根 = N+RSI_CYCLE 恰在门槛边界上，含）──
    r = _run(_mk([100 - 3 * i for i in range(15)]))
    check("① 单调跌 15 根 / N=3：窗口 [0,0,0] ⇒ 命中，归超卖组（末根 <= 超卖）",
          r.get("is_rsi") is True and r.get("rsi_group") == "under"
          and r.get("rsi_hit_under") == 3 and r.get("rsi_hit_over") == 0
          and r.get("rsi_min") == 0.0 and r.get("rsi_last") == 0.0, r)

    r = _run(_mk([100 + 3 * i for i in range(15)]))
    check("① 单调涨 15 根 / N=3：窗口 [100,100,100] ⇒ 命中，归超买组",
          r.get("is_rsi") is True and r.get("rsi_group") == "over"
          and r.get("rsi_hit_over") == 3 and r.get("rsi_max") == 100.0, r)

    check("① 命中行补市场前缀 + 名称/周期透传 + 无 error",
          r.get("code") == "sz002190" and r.get("name") == "测试股"
          and r.get("freq") == "d" and "error" not in r, r)
    check("① 命中行带 recent_days 回显（前端标签的「/N」取自它）",
          r.get("recent_days") == 3, r.get("recent_days"))

    # ── 含边界（判据写宽写窄都会改变命中根数）──
    #   窗口 = [20.0, 16.42, 13.73]：首根**恰好 20.0**。
    #   判据 `<= RSI_OVERSOLD` ⇒ 计 3 根；改成严格 `<` ⇒ 只剩 2 根 ⇒ 下面断言立刻红。
    #   前两根平盘把样本补到 15 根（= N+RSI_CYCLE 边界）后重推：平盘 diff=0 不改变
    #   其后各 diff 的正负累加，故窗口首根仍**精确**落在 20.0 上。
    cl20 = [100, 100, 101, 102, 103, 104, 105, 106, 107, 100, 93, 86, 79, 72, 65]
    r = _run(_mk(cl20))
    check("① 含边界（超卖侧）：窗口内**恰好 20.0** 的一根被计入 ⇒ hit_under == 3"
          "（写成严格 `<` 会掉到 2，本断言即红）",
          r.get("is_rsi") is True and r.get("rsi_hit_under") == 3
          and r.get("rsi_group") == "under" and r.get("rsi_last") == 13.73, r)

    #   窗口 = [80.0, 83.58, 86.27]：首根**恰好 80.0**。
    #   判据 `>= RSI_OVERBOUGHT` ⇒ 计 3 根；改成严格 `>` ⇒ 只剩 2 根 ⇒ 下面断言立刻红。
    cl80 = [100, 100, 99, 98, 97, 96, 95, 94, 93, 100, 107, 114, 121, 128, 135]
    r = _run(_mk(cl80))
    check("① 含边界（超买侧）：窗口内**恰好 80.0** 的一根被计入 ⇒ hit_over == 3"
          "（写成严格 `>` 会掉到 2，本断言即红）",
          r.get("is_rsi") is True and r.get("rsi_hit_over") == 3
          and r.get("rsi_group") == "over" and r.get("rsi_last") == 86.27, r)

    # ── 归类分支 ──
    # ① 末根决定：末根 80.07（>= 超买）⇒ 即使窗口里也有别的越界，直接归超买组
    r = _run(_mk(_seg(100, 12, 5) + _seg(155, 3, -6)))
    check("① 归类①（末根直接决定）：末根 80.07 >= 超买 ⇒ 归超买组",
          r.get("is_rsi") is True and r.get("rsi_group") == "over"
          and r.get("rsi_last") == 80.07 and r.get("rsi_hit_over") == 3, r)

    # 隔离「末根超买优先」：末根 89.67 >= 超买，窗口内却有 2 根 <= 超卖 ——
    #   末根分支给 over；删掉该分支、走中性兜底会给 under（本断言即红）。
    #   这是能区分二者的唯一形状：末根 <= 超卖侧与兜底**恒等**（末根本身
    #   必然计入 hit_under）—— 三分支写法里第一支在行为上冗余，仅作「末根
    #   优先」的显式表达（界面上不得当作未覆盖分支来算）。
    #   样本 = 11 根平盘 + 原 13 根（补到 L=24 = N+RSI_CYCLE 边界后重推：平盘
    #   diff=0 不改其后各 diff 的正负累加顺序，末 12 根 K 线序列不变）。
    cl_over_hu = [100] * 11 + [100, 96, 92, 96, 100, 104, 108, 112, 116, 120, 124, 128, 132]
    r = _run(_mk(cl_over_hu), recent="12")
    check("① 归类①（末根超买优先）：末根 89.67 >= 超买、窗口内 2 根 <= 超卖"
          " ⇒ 仍归**超买组**（末根优先于兜底）",
          r.get("is_rsi") is True and r.get("rsi_group") == "over"
          and r.get("rsi_hit_under") == 2 and r.get("rsi_hit_over") == 5
          and r.get("rsi_last") == 89.67, r)

    # ② 末根中性兜底：末根 77.08 落回开区间、窗口内无超卖 ⇒ 归超买组
    r = _run(_mk(_seg(100, 14, 2) + _seg(126, 6, -1)))
    check("① 归类②（末根中性兜底）：末根 77.08 中性、窗口内 2 根 >= 超买 ⇒ 归超买组",
          r.get("is_rsi") is True and r.get("rsi_group") == "over"
          and r.get("rsi_last") == 77.08 and r.get("rsi_hit_over") == 2
          and r.get("rsi_max") == 86.01, r)

    # 极值取值域自证：本序列全序列 max（早期涨段 RSI 到顶）明显大于窗口 max，
    #   若实现把 min/max 写成全序列（排序键与标签都会跟着错源），本断言立刻不同。
    _ser = calculate_rsi(_seg(100, 14, 2) + _seg(126, 6, -1))
    check("① rsi_min / rsi_max 取**窗口**极值（本例全序列 max=%.2f ≠ 窗口 max=86.01，写成全序列即变）" % max(_ser),
          r.get("rsi_min") == 77.08 and r.get("rsi_max") == 86.01
          and round(max(_ser), 2) != 86.01,
          (r.get("rsi_min"), r.get("rsi_max"), round(max(_ser), 2)))

    r = _run(_mk(_seg(100, 12, -4) + _seg(64, 3, 3)))
    check("① 归类②（超卖侧）：末根 25.22 中性、窗口内 1 根 <= 超卖 ⇒ 归超卖组",
          r.get("is_rsi") is True and r.get("rsi_group") == "under"
          and r.get("rsi_last") == 25.22 and r.get("rsi_hit_under") == 1
          and r.get("rsi_min") == 15.38, r)

    # ③ 两个方向都命中 + 末根中性 ⇒ **超卖优先**（与「小于20的排在大于80的前面」同向）
    #   样本 = 11 根平盘 + 原 13 根（补到 L=24 = N+RSI_CYCLE 边界）：平盘 diff=0，末 12 根
    #   K 线与原样本的末 12 根完全一致；但播种段（索引 1..11）全为 0 ⇒ 递推起点不同，
    #   窗口 RSI 须按新序列重推（故期望值与旧样本不同）。
    both = [100] * 11 + [100, 94, 88, 94, 100, 106, 112, 118, 124, 130, 136, 130, 124]
    r = _run(_mk(both), recent="12")
    check("① 归类②（两者皆命中 + 末根 65.03 中性）⇒ **超卖优先**："
          "hit_under=2 / hit_over=3 / group=under",
          r.get("is_rsi") is True and r.get("rsi_group") == "under"
          and r.get("rsi_hit_under") == 2 and r.get("rsi_hit_over") == 3
          and r.get("rsi_last") == 65.03, r)
    check("① N=12 大窗口：rsi_max=86.3（该根既进 hit_over、又不改归类 ⇒ 仍 under）",
          r.get("rsi_max") == 86.3, r.get("rsi_max"))

    # ── 未命中与裸码约定 ──
    #   16 根（>= N+RSI_CYCLE=15）：浅幅交替涨跌，窗口 [54.17, 49.65, 53.85] 全中性
    r = _run(_mk([100.0, 101.0] * 8))
    check("① 浅幅震荡（窗口 [54.17,49.65,53.85] 全中性）⇒ 未命中",
          r.get("is_rsi") is False and "error" not in r, r)
    check("① 未命中行的 code 是**裸代码**（不补前缀，与其它模式同形）",
          r.get("code") == "002190", r.get("code"))

    # ── 门槛（2026-10-14 由 max(N, RSI_CYCLE+1) 收紧为 N + RSI_CYCLE）──
    #   门槛 = N + RSI_CYCLE = 15：窗口 [L-N, L-1] 必须**整段**脱离 RSI 播种区
    #   （索引 0 = 常量 50、1..11 = 简单平均播种，12 起才是递推）。
    #   下面用 13 / 14 / 15 三个长度卡在**旧式门槛 13** 两侧做判别对：
    #     13 根：旧式实现恰好参评（全涨 ⇒ 命中），新式必须静默跳过 —— 本组判别力核心；
    #     14 根：比 15 少 1，窗口 [11..13] 首根仍落在播种区 ⇒ 跳过；
    #     15 根：恰好 L - N == RSI_CYCLE，窗口 [12..14] 全为递推值 ⇒ 参评并命中。
    r = _run(_mk([100 + i for i in range(13)]))
    check("① 门槛：13 根 == 旧式门槛 max(N, RSI_CYCLE+1)，但 < N+RSI_CYCLE=15"
          " ⇒ 静默未命中（改回旧式即红）",
          r.get("is_rsi") is False and "error" not in r, r)

    r = _run(_mk([100 + i for i in range(14)]))
    check("① 门槛：14 根（窗口 [11..13] 首根仍在播种区）⇒ 静默未命中",
          r.get("is_rsi") is False and "error" not in r, r)

    r = _run(_mk([100 + i for i in range(15)]))
    check("① 门槛自证：恰好 15 根（== N + RSI_CYCLE，窗口 [12..14] 全递推）⇒ 参评并命中",
          r.get("is_rsi") is True, r)

    #   16 根（>= 门槛，能走到「无波动」这条判据）：全序列一个价都没变
    r = _run(_mk([50.0] * 16))
    check("① 全序列一个价都没变（停牌/一字板）⇒ 静默未命中"
          "（RSI 走边界分支得退化值 0，是退化不是超卖）",
          r.get("is_rsi") is False and "error" not in r, r)

    r = _run(_mk(_seg(100, 15, 1)), recent="30")
    check("① N 超出序列长度（N=30 ⇒ 门槛 42 > 15 根）⇒ 静默未命中",
          r.get("is_rsi") is False and "error" not in r, r)

    # ── N=1：只看最后一根（13 根 = N + RSI_CYCLE 边界；窗口 = 索引 12，即递推首根）──
    r = _run(_mk([100 + 3 * i for i in range(13)]), recent="1")
    check("① N=1：窗口 = 末根一根（索引 12），>= 超买 ⇒ 命中且 hit_over == 1",
          r.get("is_rsi") is True and r.get("rsi_hit_over") == 1
          and r.get("recent_days") == 1, r)

    # ── 返回字段完整性（前端标签 / 排序依赖）──
    #   15 根 = N + RSI_CYCLE 边界（不足门槛会走未命中分支，字段自然不全 ⇒ 必须给够）
    need = ("is_rsi", "rsi_group", "rsi_last", "rsi_min", "rsi_max",
            "rsi_hit_under", "rsi_hit_over", "recent_days", "freq", "last_close")
    r = _run(_mk([100 - 3 * i for i in range(15)]))
    miss = [k for k in need if k not in r]
    check("① 命中行输出字段齐全（前端标签 / 排序 / 分组计数依赖）", not miss, miss)

    # ── 非 rsi 模式不受影响（同一票同一数据，mode="lianzhang" 走自己的判据）──
    #   此处刻意仍用 13 根：同一序列在 rsi 模式下已被门槛拦下（见上组判别对），
    #   lianzhang 模式照常产出自己的字段 ⇒ 「门槛只约束 rsi 分支」的直接证据。
    r = _run(_mk([100 - 3 * i for i in range(13)]), mode="lianzhang")
    check("① 模式隔离：同一序列走 lianzhang 不产生 is_rsi 字段"
          "（且不因 rsi 门槛而跳过）",
          "is_rsi" not in r and "is_lianzhang" in r, r)


# ══════════════════════════════════════════════════════════════════════
# ② 阈值同源与判据方向
# ══════════════════════════════════════════════════════════════════════
def part2():
    print("══ ② 阈值同源（值级）与判据方向 ══")
    src_u = io.open(APPUTILS, encoding="utf-8").read()
    src_s = io.open(APPSCAN, encoding="utf-8").read()
    appjs = io.open(APPJS, encoding="utf-8").read()

    check("② Python 侧：超买/超卖由 RSI_REF_VALUES 的 max/min **派生**（不写字面量）",
          "RSI_OVERBOUGHT = max(RSI_REF_VALUES[1:])" in src_u
          and "RSI_OVERSOLD = min(RSI_REF_VALUES[1:])" in src_u,
          "\u5b9a\u4e49\u5f62\u6001\u5df2\u53d8")
    # 负向判别（2026-10-14 增补）：**不许**对整个三元组取 max/min —— 那会把常量中轴 50
    #   算进来，与前端 `slice(1)` 不同形。默认 (50,80,20) 下两种写法取值**相同**，
    #   故上面的取值断言看不出差别，**只有这条形态断言能判别**（改回 `max(RSI_REF_VALUES)`
    #   即红 —— 这就是本次改动的变异自证）。
    check("② Python 侧负向：max/min **只许**作用在阈值对 `[1:]` 上，不得把中轴算进来",
          not re.search(r"RSI_OVERBOUGHT\s*=\s*max\(RSI_REF_VALUES\)", src_u)
          and not re.search(r"RSI_OVERSOLD\s*=\s*min\(RSI_REF_VALUES\)", src_u),
          "\u4e2d\u8f74\u88ab\u7b97\u8fdb\u6d3e\u751f\u5f0f")
    check("② 真导入：派生值 == max/min(RSI_REF_VALUES[1:])（与前端 slice(1) 同形）",
          RSI_OVERBOUGHT == max(RSI_REF_VALUES[1:])
          and RSI_OVERSOLD == min(RSI_REF_VALUES[1:]),
          (RSI_OVERBOUGHT, RSI_OVERSOLD, RSI_REF_VALUES))

    check("② 前端：副图分段着色阈值同样由 RSI_REF_VALUES 的**阈值对**派生"
          "（RSI_SEG_OVER/UNDER = max/min(RSI_REF_VALUES.slice(1))；"
          "可配置化后由 _applyRsiThresholds 重算，派生式不变）",
          "RSI_SEG_OVER = Math.max.apply(null, RSI_REF_VALUES.slice(1));" in appjs
          and "RSI_SEG_UNDER = Math.min.apply(null, RSI_REF_VALUES.slice(1));" in appjs,
          "\u524d\u7aef\u6d3e\u751f\u884c\u672a\u627e\u5230")

    # 阈值可配置化后前端不再写死运行数组：默认兜底常量（RSI_DEFAULT_*）与 Python
    #   默认同值；运行值 [中轴, 超买, 超卖] 由 _applyRsiThresholds() 按用户配置重算。
    _mo = re.search(r"const RSI_DEFAULT_OVER = (\d+);", appjs)
    _mu = re.search(r"const RSI_DEFAULT_UNDER = (\d+);", appjs)
    _mr = re.search(r"let RSI_REF_VALUES = \[(\d+), RSI_DEFAULT_OVER, RSI_DEFAULT_UNDER\];", appjs)
    front_vals = ([int(_mr.group(1)), int(_mo.group(1)), int(_mu.group(1))]
                  if (_mo and _mu and _mr) else None)
    check("② 跨语言同源：前端默认兜底 [中轴, 超买, 超卖] == Python RSI_REF_VALUES",
          front_vals == list(RSI_REF_VALUES),
          (front_vals, list(RSI_REF_VALUES)))
    check("② 两端 max/min 同值（同样只取阈值对，与 Python `[1:]` 同形）"
          " ⇒ 「图上画成超买」与「扫描报超买」永远同判据",
          front_vals is not None
          and max(front_vals[1:]) == RSI_OVERBOUGHT
          and min(front_vals[1:]) == RSI_OVERSOLD,
          (front_vals, RSI_OVERBOUGHT, RSI_OVERSOLD))

    # ── rsi 分支切片（锚点漂移即抛异常，不会静默空转）──
    i = src_s.index('if mode == "rsi":')
    j = src_s.index("# \u2500\u2500 \u56de\u6d4b\u626b\u63cf\u6a21\u5f0f \u2500\u2500", i)
    rsi_slice = src_s[i:j]
    check("② 切片区段非空（防锚点漂移后护栏空转）", len(rsi_slice) > 2000, len(rsi_slice))

    check("② 判据方向：`v >= 超买` / `v <= 超卖`（**含边界**；改成严格不等即红）",
          "if v >= _rsi_over" in rsi_slice
          and "if v <= _rsi_under" in rsi_slice, "\u5224\u636e\u65b9\u5411\u4e0d\u7b26")

    check("② 阈值取值链：请求参数优先、缺省回退 RSI_OVERBOUGHT / RSI_OVERSOLD（默认 SSOT）",
          "_rsi_over = RSI_OVERBOUGHT if rsi_over is None else float(rsi_over)" in rsi_slice
          and "_rsi_under = RSI_OVERSOLD if rsi_under is None else float(rsi_under)" in rsi_slice,
          "\u9608\u503c\u53d6\u503c\u94fe\u7f3a\u5931")
    check("② 防御校验：非法组合（不满足 0 < 超卖 < 超买 < 100）整对回退默认 + 告警",
          "if not (0 < _rsi_under < _rsi_over < 100):" in rsi_slice
          and rsi_slice.count("_rsi_over, _rsi_under = RSI_OVERBOUGHT, RSI_OVERSOLD") == 2,
          "\u9632\u5fa1\u6821\u9a8c\u7f3a\u5931")

    check("② rsi 分支内**不写 80 / 20 数值副本**（阈值只在 RSI_REF_VALUES 一处）",
          re.search(r"(?<!\d)(80|20)(?!\d)", rsi_slice) is None,
          re.findall(r".{0,40}(?<!\d)(?:80|20)(?!\d).{0,40}", rsi_slice)[:2])

    check("② 归类三分支齐备（末根 <= 超卖 / 末根 >= 超买 / 中性按极值兜底）",
          "if rsi_last <= _rsi_under:" in rsi_slice
          and "elif rsi_last >= _rsi_over:" in rsi_slice
          and 'rsi_group = "under" if hit_under else "over"' in rsi_slice,
          "\u5f52\u7c7b\u5206\u652f\u7f3a\u5931")

    check("② 门槛：K 线不足 **或** 全序列无波动 ⇒ 静默未命中（同一行判据）",
          "if len(closes) < need_bars or min(closes) == max(closes):" in rsi_slice,
          "\u95e8\u69db\u5224\u636e\u7f3a\u5931")
    check("② 门槛根数 = N + RSI_CYCLE（窗口整段脱离播种区；不是裸 N，"
          "也不是旧式 max(N, RSI_CYCLE+1)）",
          "need_bars = recent_days + RSI_CYCLE" in rsi_slice,
          "\u95e8\u69db\u6839\u6570\u4e0d\u7b26")
    #   负向（2026-10-14 增补）：旧式 max(N, RSI_CYCLE+1) 只保**末根**脱离播种区，
    #   N >= 13 时退化为裸 N ⇒ 窗口起点回到索引 0（整段含播种值）—— 不许残留。
    check("② 门槛负向：不得残留 max(N, RSI_CYCLE+1) 形态的根数式（改回旧式即红）",
          not re.search(r"need_bars\s*=\s*max\(", rsi_slice),
          re.findall(r"need_bars\s*=\s*[^\n]*", rsi_slice))
    check("② RSI 在**全量**收盘序列上算完再切片（只喂窗口会得到与图上不同的数）",
          "rsi_series = calculate_rsi(closes)" in rsi_slice
          and "win = rsi_series[-recent_days:]" in rsi_slice, "\u53d6\u6570\u987a\u5e8f\u4e0d\u7b26")

    check("② 阈值 / 计算函数经 import 引入（单一来源，非本模块复制常量）",
          "from App.AppUtils import RSI_CYCLE, RSI_OVERBOUGHT, RSI_OVERSOLD, calculate_rsi"
          in src_s, "\u5bfc\u5165\u884c\u4e0d\u7b26")


# ══════════════════════════════════════════════════════════════════════
# ③ 前端真函数（node 抽 app.js 真代码）
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


# 混合样本：3 超卖 + 2 超买 + 1 条缺 rsi_group（兜底：15.0 <= 当前生效 20 ⇒ under，
#   与该行 rsi_hit_under=1 同向）。
#   期望顺序（组间 + 组内极值）：C(0.0) → A(8.1) → F(15.0) → E(15.4) → D(92.5) → B(86.0)


def _title_row(appjs, mode, name):
    """从 `_SCAN_TITLE` 表取某模式的 window 标记（"true"/"false"/""）。

    标题已收归单一表（模式名 + 是否带「最新 N 根」都在那一行），故断言打在表上，
    而不是像以前那样去搜每个模式各自的字面量拼接（那种写法本身就说明没归一）。
    """
    for ln in appjs.split("\n"):
        if ln.strip().startswith(mode + ":") and ('name: "' + name + '"') in ln:
            return "true" if "window: true" in ln else "false"
    return ""

_ROWS = [
    {"code": "sz000001", "name": "A", "is_rsi": True, "recent_days": 3,
     "rsi_group": "under", "rsi_last": 10.0, "rsi_min": 8.1, "rsi_max": 25.0,
     "rsi_hit_under": 2, "rsi_hit_over": 0},
    {"code": "sh600000", "name": "B", "is_rsi": True, "recent_days": 3,
     "rsi_group": "over", "rsi_last": 88.0, "rsi_min": 70.0, "rsi_max": 86.0,
     "rsi_hit_under": 0, "rsi_hit_over": 1},
    {"code": "sz000002", "name": "C", "is_rsi": True, "recent_days": 3,
     "rsi_group": "under", "rsi_last": 5.0, "rsi_min": 0.0, "rsi_max": 20.0,
     "rsi_hit_under": 3, "rsi_hit_over": 0},
    {"code": "sh600001", "name": "D", "is_rsi": True, "recent_days": 3,
     "rsi_group": "over", "rsi_last": 95.0, "rsi_min": 60.0, "rsi_max": 92.5,
     "rsi_hit_under": 0, "rsi_hit_over": 3},
    {"code": "sz000003", "name": "E", "is_rsi": True, "recent_days": 3,
     "rsi_group": "under", "rsi_last": 18.0, "rsi_min": 15.4, "rsi_max": 30.0,
     "rsi_hit_under": 1, "rsi_hit_over": 0},
    {"code": "sz000004", "name": "F", "is_rsi": True, "recent_days": 3,
     "rsi_last": 15.0, "rsi_min": 15.0, "rsi_max": 40.0,
     "rsi_hit_under": 1, "rsi_hit_over": 0},
]

# 披露行的「判据句」判别词 —— 面板只留这一句（不含阈值数字，随变量插值）。
#   用它而非「含边界」探测披露行存在性：后者已按要求从面板移除。
_CAL_MARK = "判据：其中任一根"


def part3():
    print("══ ③ 前端真函数（node 抽段；node 不在位则 SKIP）══")
    node = shutil.which("node")
    if not node:
        check("node 不在位，前端契约层 SKIP", True)
        return

    appjs = io.open(APPJS, encoding="utf-8").read()
    consts = []
    # 阈值可配置化后：取 默认兜底常量 + 用户变量 + let 派生三件（初值 = 默认，
    #   与改造前语义等价；运行值重算由 test_scan_rsi_thresholds 另钉）。
    for pat in (r"        const RSI_DEFAULT_OVER = .*",
                r"        const RSI_DEFAULT_UNDER = .*",
                r"        let _rsiOverUser = .*",
                r"        let _rsiUnderUser = .*",
                r"        let RSI_REF_VALUES = .*",
                r"        let RSI_SEG_OVER = .*",
                r"        let RSI_SEG_UNDER = .*"):
        m = re.search(pat, appjs)
        assert m, "app.js 常量行抽取失败: " + pat
        consts.append(m.group(0).strip())

    prelude = "\n".join(consts) + "\n" + (
        "var window = globalThis;\n"
        "var _scanFreq = '30m';\n"
        "var _scanRecentDays = 3;\n"
        "var _scanSources = ['zxg'];\n"
        "function chkBox(code, checked) { return '<chk:' + code + '>'; }\n"
        "function statsEsc(x) { return String(x == null ? '' : x); }\n"
        "function _scanMarketSummaryHtml(rs) { return '<mkt:' + rs.length + '>'; }\n"
        "function updateScanSaveBtn() {}\n"
        "function _scanSourceLabel() { return '\u81ea\u9009\u80a1'; }\n"
        "var DOM = { 'scan-body': { innerHTML: '' } };\n"
        "var document = { getElementById: function (id) { return DOM[id] || null; },"
        " querySelector: function () { return null; } };\n"
        "var out = [];\n"
    )

    rows_json = json.dumps(_ROWS, ensure_ascii=False)
    driver = (
        "var arr = " + rows_json + ".slice();\n"
        "var rowsHtml = _renderRsiRows(arr);\n"
        "out.push('ORDER=' + JSON.stringify(arr.map(function (r) { return r.code; })));\n"
        "out.push('TAGU=' + JSON.stringify(buildRsiTagHtml(\n"
        "  {rsi_group:'under', rsi_min:15.4, rsi_hit_under:2, recent_days:3})));\n"
        "out.push('TAGO=' + JSON.stringify(buildRsiTagHtml(\n"
        "  {rsi_group:'over', rsi_max:92.5, rsi_hit_over:3, recent_days:3})));\n"
        "out.push('GRP=' + JSON.stringify([\n"
        "  _rsiGroupOf({rsi_group:'under'}), _rsiGroupOf({rsi_group:'over'}),\n"
        "  _rsiGroupOf({rsi_last:15.0}), _rsiGroupOf({rsi_last:85.0})]));\n"
        "out.push('GRP2=' + JSON.stringify([\n"
        "  _rsiGroupOf({rsi_last:50.0, rsi_hit_under:2, rsi_hit_over:0}),\n"
        "  _rsiGroupOf({rsi_last:25.0, rsi_under:30, rsi_over:70}),\n"
        "  _rsiGroupOf({rsi_last:85.0, rsi_hit_under:3, rsi_hit_over:1}),\n"
        "  _rsiGroupOf({rsi_hit_over:2}),\n"
        "  _rsiGroupOf({rsi_hit_under:1, rsi_hit_over:2}),\n"
        "  _rsiGroupOf({})]));\n"
        "out.push('CAL=' + JSON.stringify(_rsiCaliberHtml()));\n"
        "out.push('CALRT=' + JSON.stringify(_rsiCaliberHtml(\n"
        "  [{rsi_over:70, rsi_under:30}])));\n"
        "out.push('CALVALS=' + JSON.stringify([RSI_SEG_OVER, RSI_SEG_UNDER]));\n"
        "out.push('ROWS=' + JSON.stringify(rowsHtml));\n"
        "DOM['scan-body'].innerHTML = '';\n"
        "renderRsiScanResults(" + rows_json + ", 100, 7, false);\n"
        "out.push('FIN=' + JSON.stringify(DOM['scan-body'].innerHTML));\n"
        "DOM['scan-body'].innerHTML = '';\n"
        "renderRsiScanResults([], 100, 100, false);\n"
        "out.push('EMPTY=' + JSON.stringify(DOM['scan-body'].innerHTML));\n"
        "console.log(out.join('\\n'));\n"
    )

    harness = (prelude
               + "\n".join(_extract_any_fn(appjs, n) for n in
                           ("_rsiGroupOf", "buildRsiTagHtml", "_rsiCaliberHtml",
                            "_renderRsiRows", "renderRsiScanResults",
                            "_fmtFloatMc", "buildFloatMcHtml", "_scanRowHtml", "_scanSummaryLine",
                            "buildMa120Html", "_scanPanelHtml", "_paintScanFinal")) + "\n"
               + driver)

    jf = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    jf.write(harness)
    jf.close()
    try:
        proc = subprocess.run([node, jf.name], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    finally:
        os.unlink(jf.name)

    if proc.returncode != 0:
        check("node 执行成功", False, (proc.stderr or proc.stdout)[:500])
        return
    kv = dict(x.split("=", 1) for x in proc.stdout.splitlines() if "=" in x)

    order = json.loads(kv["ORDER"])
    check("③ 排序：**超卖组整体**排在超买组之前（用户拍板「小于20的排在大于80的前面」）",
          set(order[:4]) == {"sz000002", "sz000001", "sz000004", "sz000003"}
          and set(order[4:]) == {"sh600001", "sh600000"}, order)
    check("③ 排序：超卖组内按窗口**最小** RSI 升序（0.0 → 8.1 → 15.0 → 15.4）",
          order[:4] == ["sz000002", "sz000001", "sz000004", "sz000003"], order)
    check("③ 排序：超买组内按窗口**最大** RSI 降序（92.5 → 86.0）",
          order[4:] == ["sh600001", "sh600000"], order)
    check("③ 排序键 == 标签显示的那个极值（标签与排序不许各算一份）",
          "(Number(a.rsi_min) || 0) - (Number(b.rsi_min) || 0)" in appjs
          and "(Number(b.rsi_max) || 0) - (Number(a.rsi_max) || 0)" in appjs
          and "v = Number(isUnder ? r.rsi_min : r.rsi_max)" in appjs,
          "\u6392\u5e8f\u952e\u4e0e\u6807\u7b7e\u53d6\u503c\u4e0d\u540c\u6e90")

    grp = json.loads(kv["GRP"])
    check("③ _rsiGroupOf：显式字段优先；缺字段时按阈值（行内 rsi_under/over 优先，"
          "缺省退当前生效值）比较 rsi_last（15→under / 85→over）",
          grp == ["under", "over", "under", "over"], grp)

    grp2 = json.loads(kv["GRP2"])
    check("③ _rsiGroupOf 兜底与后端同源分层（旧实现只按「rsi_last <= 当前 20」判，"
          "第 1/2/5 条即红）："
          "① 末根中性 50 且 hit_under>0 ⇒ under；"
          "② 阈值取该行自带 rsi_under=30 ⇒ 25 归 under（旧按当前 20 误判 over）；"
          "③ 末根 85 越超买 ⇒ over（末根优先于 hit_under=3，顺序写反即红）；"
          "④ 无 rsi_last ⇒ 按命中方向，仅超买 ⇒ over；"
          "⑤ 两侧都命中 ⇒ 超卖优先 under；"
          "⑥ 空行 ⇒ 确定性 over（不引入第三态）",
          grp2 == ["under", "under", "over", "over", "under", "over"], grp2)

    tag_u = json.loads(kv["TAGU"])
    tag_o = json.loads(kv["TAGO"])
    check("③ 标签（超卖）：青色系 scan-tag-down（= 副图 macdDown 同源类）+「超卖 15.4 (2/3)」",
          "scan-bsp-tag" in tag_u and "scan-tag-down" in tag_u
          and "\u8d85\u5356 15.4" in tag_u and "(2/3)" in tag_u, tag_u)
    check("③ 标签（超买）：红色系 scan-tag-up（= 副图 macdUp 同源类）+「超买 92.5 (3/3)」",
          "scan-bsp-tag" in tag_o and "scan-tag-up" in tag_o
          and "\u8d85\u4e70 92.5" in tag_o and "(3/3)" in tag_o, tag_o)

    cal = json.loads(kv["CAL"])
    calvals = json.loads(kv["CALVALS"])
    fn_cal = _extract_any_fn(appjs, "_rsiCaliberHtml")
    check("③ 披露行的阈值是**变量插值**（函数体内不写 80/20 字面量；写死则改配置就漂移）",
          "RSI_SEG_OVER" in fn_cal and "RSI_SEG_UNDER" in fn_cal
          and re.search(r"(?<!\d)(80|20)(?!\d)", fn_cal) is None, fn_cal[:200])
    check("③ 披露行：阈值来自变量 RSI_SEG_OVER/UNDER（值 %s）" % (calvals,),
          calvals == [RSI_OVERBOUGHT, RSI_OVERSOLD]
          and (">= " in cal.replace("≥", ">=")) and ("<= " in cal.replace("≤", "<=")),
          (calvals, cal))
    calrt = json.loads(kv["CALRT"])
    check("③ 披露行：**回传值优先** —— 结果行带 rsi_over/rsi_under=70/30 时覆盖当前"
          "默认 80/20（扫描后改配置，文案不跟着变）",
          "≥ 70" in calrt and "≤ 30" in calrt
          and "≥ 80" not in calrt and "≤ 20" not in calrt, calrt)
    check("③ 披露行：**只留判据一句话** —— 不含「含边界 / 副图同源 / 排序」等实现侧话术",
          all(w not in cal for w in ("\u542b\u8fb9\u754c", "\u526f\u56fe",
                                     "\u8d85\u5356\u7ec4\u6392\u8d85\u4e70\u7ec4\u524d",
                                     "\u4e00\u5bf9")), cal)
    check("③ 披露行：以「最近 N 根（取自 _scanRecentDays = 3）+ 判据句」收尾，句后无追加",
          "\u6700\u8fd1 3 \u6839" in cal
          and cal.rstrip().endswith("\uff08\u8d85\u5356\uff09</div>"), cal)
    check("③ 披露行样式与连涨/放量口径行同构（class=scan-summary + 同字号/颜色）",
          'class="scan-summary"' in cal and "font-size:10px" in cal
          and "#7a8399" in cal, cal)

    rows_html = json.loads(kv["ROWS"])
    # 本轮起「行函数只出行」：市场分布行与口径行由装配器 `_scanPanelHtml` 统一挂，
    #   行函数里再出现 scan-summary 就是两处又要漂移了（放量/底分型曾经的病）。
    check("③ renderRows 只产出数据行（6 行），**不含**市场分布 / 口径行",
          rows_html.count("scan-stock-row") == 6
          and "scan-summary" not in rows_html and _CAL_MARK not in rows_html,
          rows_html[:200])

    fin = json.loads(kv["FIN"])
    check("③ 终态：摘要按**组**计数「超卖 4 只，超买 2 只」（数字走 scan-num，不加粗）",
          "\u8d85\u5356 <span class=\"scan-num\">4</span> \u53ea" in fin
          and "\u8d85\u4e70 <span class=\"scan-num\">2</span> \u53ea" in fin,
          fin[:400])
    check("③ 终态：含披露行 + 6 行数据行",
          _CAL_MARK in fin and fin.count("scan-stock-row") == 6, fin[:300])
    check("③ 终态：勾选框按 code 生成（复用 chkBox，命中即勾）",
          fin.count("<chk:") == 6, fin.count("<chk:"))

    empty = json.loads(kv["EMPTY"])
    check("③ 空结果态：披露行仍在（口径不该只在有票时才出现）+ 零数据行",
          _CAL_MARK in empty and "scan-stock-row" not in empty
          and "\u672a\u53d1\u73b0 RSI" in empty, empty[:300])

    # 本轮起"共用"不再靠两处各写一行调用，而是**同一个函数引用**：
    #   spec.renderRows 与 renderRsiScanResults 传给 _paintScanFinal 的 rowsFn 是它。
    check("③ 进度期与终态共用同一份行渲染（两个入口传的是同一个函数引用）",
          "renderRows: _renderRsiRows," in appjs
          and "results, _renderRsiRows," in appjs,
          "\u4e24\u5904\u672a\u5171\u7528")


# ══════════════════════════════════════════════════════════════════════
# ④ 接线完整性（静态）
# ══════════════════════════════════════════════════════════════════════
def part4():
    print("══ ④ 接线完整性（模式枚举 / 白名单 / 自动补 3 / 标题 / 分支指向）══")
    appjs = io.open(APPJS, encoding="utf-8").read()
    html = io.open(APPHTML, encoding="utf-8").read()

    check("④ _scanMode 取值注释含 \"rsi\"",
          '"rsi" = RSI \u8d85\u4e70\u8d85\u5356\u626b\u63cf' in appjs, "\u6ce8\u91ca\u672a\u8865")
    # 白名单的契约是「**包含** rsi」（各模式之间是 || 求或，顺序无功能意义）。原断言
    #   把**相邻顺序**也写死：本次新增 "rsi" 时恰好插在 lianzhang 与 backtest 之间才没红，
    #   下次在别处插模式（或白名单重排）即假红。这里改为解析整条 if 条件取集合、断言
    #   包含关系（与 Test/test_scan_backtest_mode.py ⑥ 同款写法，那边 2026-10-12
    #   已因同型写死假红过一轮）。
    _wl_m = re.search(
        r'if \(savedMode === "\w+"(?: \|\| savedMode === "\w+")*\)', appjs)
    _whitelist = re.findall(r'savedMode === "(\w+)"', _wl_m.group(0)) if _wl_m else []
    check("④ 模式持久化白名单含 rsi（否则刷新后回落默认模式）",
          "rsi" in _whitelist,
          "\u767d\u540d\u5355\u672a\u542b rsi\uff0c\u5b9e\u9645=" + str(_whitelist))
    check("④ 「最近N根」对 rsi 可用，且当前值 <2 时自动补 3（与连涨同款）",
          "var isRsi = selected && selected.value === \"rsi\";" in appjs
          and "(isLianzhang || isRsi) && (parseInt(input.value, 10) || 0) < 2" in appjs,
          "\u81ea\u52a8\u8865 3 \u672a\u63a5")
    check("④ 「最近N根」灰化名单**不含** rsi（ann/ma/fx_d/backtest 才灰）",
          "if (isAnn || isMa || isFxD || isBacktest) {" in appjs, "\u7070\u5316\u5224\u636e\u5df2\u53d8")
    check("④ 标题表收录 rsi 且带 window 标记（统一成「最新 N 根」风格）",
          _title_row(appjs, "rsi", "RSI") == "true",
          "标题表未含 rsi / window 标记")

    check("④ runScan 分支存在且 mode 传 \"rsi\"",
          'if (_scanMode === "rsi") {' in appjs and 'mode: "rsi",' in appjs,
          "\u5206\u652f\u672a\u63a5")
    check("④ 结果分类谓词 = !!data.is_rsi（与后端字段同名）",
          "classify: function(data) { return !!data.is_rsi; }" in appjs,
          "\u8c13\u8bcd\u4e0d\u7b26")
    check("④ 分支位置在 lianzhang 之后、默认 bsp 之前（与 UI/后端同序）",
          appjs.index('if (_scanMode === "rsi") {')
          > appjs.index('if (_scanMode === "lianzhang") {')
          and appjs.index('if (_scanMode === "rsi") {')
          < appjs.index("// \u4e70\u5356\u70b9\u626b\u63cf\u6a21\u5f0f\uff08\u9ed8\u8ba4\uff09"),
          "\u5206\u652f\u4f4d\u7f6e\u4e0d\u7b26")

    check("④ 渲染五件套齐备（_rsiGroupOf / buildRsiTagHtml / _rsiCaliberHtml / "
          "_renderRsiRows / renderRsiScanResults）",
          all(("function " + f) in appjs for f in
              ("_rsiGroupOf", "buildRsiTagHtml", "_rsiCaliberHtml",
               "_renderRsiRows", "renderRsiScanResults")), "\u51fd\u6570\u7f3a\u5931")

    # ── app.html 契约 ──
    check("④ app.html：RSI 选项存在（radio name=scan-mode value=rsi）",
          'name="scan-mode" value="rsi"' in html, "\u9009\u9879\u672a\u627e\u5230")
    i_chk = html.index('name="scan-mode" value="lianzhang"')
    i_rsi = html.index('name="scan-mode" value="rsi"')
    i_fx = html.index('name="scan-mode" value="fx_d"')
    check("④ app.html：RSI 落在「连涨」之后、「底分型」之前（DOM 顺序即落位顺序）",
          i_chk < i_rsi < i_fx, (i_chk, i_rsi, i_fx))
    check("④ app.html：模式容器为 5 列网格（4 列会让 RSI 掉到第 2 行）",
          "grid-template-columns:1fr 1fr 1fr 1fr 1fr;" in html
          and "grid-template-columns:1fr 1fr 1fr 1fr;" not in html,
          "\u7f51\u683c\u5217\u6570\u4e0d\u7b26")
    check("④ app.html 模式区注释**不复述阈值数值**（数值只在 RSI_REF_VALUES 一处）",
          "\u226580" not in html and "\u226420" not in html, "\u4ecd\u5b58\u5728\u6570\u503c\u526f\u672c")


def main():
    print("=" * 68)
    print("「RSI」扫描模式（scan mode = rsi）护栏")
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

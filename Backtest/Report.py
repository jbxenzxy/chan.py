# -*- coding: utf-8 -*-
"""
报告输出（Backtest/Report.py）
===============================
P0 产出：**一只票的完整交易清单**（CSV）+ 控制台摘要。

★ CSV 字段范围（v1.15 用户裁定）
---------------------------------------------------------------------
P0 的 CSV 字段 = **P0 实际所需**，**不按 §9.2 预留 7 列**
（`S_plus` / `S_minus` / `sum_net` / `sum_r` / `sum_hold` …）。
理由：用户已明确「§9 全 A 批跑」是**待定功能、目前不实现** ⇒ 不为未定功能
冻结 CSV 契约。将来真做 §9，按 §9.8 走"重冻 CSV 契约 + 重录快照"。

口径行（摘要里必须打出，设计文档 §5.4 末）：
    区间 `[L, R]` / 周期 / 出场参数 / 费率 / T+0 偏离 / 前复权 ——
    否则"换个周期数字就变了"无人察觉。

出场原因显示文案（v1.18，2026-10-05 用户裁定为三选一）
---------------------------------------------------------------------
引擎侧只产出三个 `reason`（`sl` / `breakeven` / `trailing`），但**原文是英文标识符**，
不适合直接印给人看 ⇒ 面向用户的文案由 `exit_reason_labels()` / `exit_reason_legend()`
生成（本模块 = 报告层 SSOT）：控制台 `summary_text` / `trades_table` 与本模块的
App 层调用方（`App/AppBacktest.py` → 前端面板）**共用同一份**，
别在渲染层各抄一遍（抄一份 = 改一处漏一处）。

    ⚠ CSV 的 `exit_reason` 列**保持英文原值** —— 那是数据契约（列名与取值都可能被
      下游脚本消费），本地化只发生在"给人看"的两处。两者不同源不是 bug，是分工。
"""
from __future__ import annotations

import csv
import io
from typing import Any, Dict, List, Optional, Tuple

from .ExitParams import (COMMISSION_RATE, MIN_COMMISSION_CASH, NOMINAL_COST_RATE,
                         STAMP_DUTY_RATE, TARGET_AMOUNT, TRANSFER_FEE_RATE)
from .Metrics import Metrics, compute
from .Runner import RunResult

# 逐笔交易清单的列（**不预留 §9.2 字段**）
CSV_COLUMNS = [
    "code", "freq", "side", "bsp_type",
    "entry_date", "entry_price", "R",
    "exit_date", "exit_price", "exit_reason",
    "bars_held", "r_multiple",
    "gross_return_pct", "cost_cash", "net_return_pct",
    "shares", "status",
]


def _row(code: str, freq: str, t) -> List[str]:
    def _f(x, nd=6):
        return "" if x is None else ("%.{}f".format(nd) % x)
    return [
        code, freq, t.side, t.bsp_type,
        t.entry_date, _f(t.entry_price, 3), _f(t.r_distance, 4),
        t.exit_date or "", _f(t.exit_price, 3), t.exit_reason or "",
        ("" if t.bars_held is None else str(t.bars_held)),
        _f(t.r_multiple, 4),
        _f(None if t.gross_return is None else t.gross_return * 100, 4),
        _f(t.cost_cash, 4),
        _f(None if t.net_return is None else t.net_return * 100, 4),
        str(t.shares), ("open" if t.open_ else "closed"),
    ]


def trades_csv(result: RunResult) -> str:
    """逐笔交易清单 → CSV 文本（LF 行尾，utf-8）。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for t in result.trades:
        w.writerow(_row(result.code, result.freq, t))
    return buf.getvalue()


def write_csv(result: RunResult, path: str) -> str:
    """写 CSV（utf-8-sig：Excel 直开不乱码）。返回写入路径。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(trades_csv(result))
    return path


# ════════════════════════════════════════════════════════════════════
# 出场原因 → 面向用户的显示文案（报告层 SSOT）
# ════════════════════════════════════════════════════════════════════
# 引擎侧 `ExitCheck.reason` 的取值**只有三种规则身份**
# （`Trading/Strategy/Exit.py::LayeredExitPolicy._phase_reason`，2026-10-05 用户裁定为三选一）：
#
#   "sl"         初始止损保护线 —— L1 结构距离 / L2 (2×ATR) 取大算出的保护价，**从未被抬过**
#   "breakeven"  保本保护线 —— 浮盈 **>** `breakeven_trigger_r`×R 后抬价
#   "trailing"   跟踪保护线 —— 浮盈 **>** `win_loss_ratio`×R 后抬价
#
# ⚠ 三件事必须分清，别混：
#   ① `reason` **只表达规则身份、不表达盈亏**（盈亏口径在 `Metrics`：净收益率三分）。
#      「跟踪止盈」也可能只小赚甚至因滑点净亏，那是成交结果，不是这个字段的事。
#   ② `breakeven` 文案里的那个 `1R` 是**触发该层的浮盈阈值**（`breakeven_trigger_r`），
#      **不是**保护线的位置 —— 保护线落在入场价 **上方 0.5R**（`breakeven_buffer_r`）。
#      用户口径原文：「保本(1R)」= 浮盈到 1R 就保本（2026-10-05）。
#   ③ `trailing` 那一格**不写 R 数**：该笔实际锁住多少 R 就是逐笔明细里的 `r_multiple`，
#      写两遍只会让人以为是两个不同的数。
#
# 为什么文案由**函数**生成而不是模块级常量字典：`1R` 里的 1 必须跟着
# `breakeven_trigger_r` 走 —— 常量字典会在有人把阈值改成 1.5 之后继续印「保本(1R)」，
# 正是"配置改了口径文案没改"的经典漂移（改一处必须只改一处）。
EXIT_REASON_ORDER: Tuple[str, ...] = ("sl", "breakeven", "trailing")


def _fmt_r(v: float) -> str:
    """R 倍数 → 文案（1.0 → "1"，0.5 → "0.5"，2.5 → "2.5"）。"""
    return "%g" % float(v)


def resolved_exit_params(exit_params: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """出场参数 → 四项 R 口径（缺省 / 空表 = `STOCK_EXIT_PARAMS` 经 `ExitPolicyParams` 校验）。

    `None` 与 `{}` 都按"未指定"处理（空表不携带任何覆盖，与 `Runner.run` 的
    `dict(exit_params or STOCK_EXIT_PARAMS)` 同义）。

    与 `Runner.run` 同源：那边把同一份 dict 交给 `LayeredExitPolicy`，未列出的键走模型默认
    ⇒ 这里必须走**同一个模型**取默认值，不能自己抄一份 `1.0 / 0.5 / 2.0`
    （抄一份 = 多一个会漂移的事实源）。

    惰性 import `Trading.Strategy.Exit`：`Trading/Config.py` 在导入期构造整个 DEFAULT_CONFIG，
    `.env` 笔误会让 import 失败 ⇒ 不能放在本模块顶层（层表允许 `Trading.Strategy`，见 §5.1）。
    """
    from .ExitParams import STOCK_EXIT_PARAMS
    from Trading.Strategy.Exit import ExitPolicyParams

    raw = dict(exit_params) if exit_params else dict(STOCK_EXIT_PARAMS)
    p = ExitPolicyParams(**raw)
    return {
        "win_loss_ratio": float(p.win_loss_ratio),
        "trailing_trigger_r": float(p.trailing_trigger_r),
        "breakeven_trigger_r": float(p.breakeven_trigger_r),
        "breakeven_buffer_r": float(p.breakeven_buffer_r),
    }


def exit_reason_labels(exit_params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """出场原因 → 显示文案（三选一；控制台 / 前端 / 图例**共用同一份**）。

        止损        触发**止损**保护线离场
        保本(1R)    触发**保本**保护线离场（括号内 = 触发该层的浮盈阈值，随配置变）
        跟踪止盈    触发**跟踪**保护线离场（锁住多少 R 见逐笔 `r_multiple`）

    字典的键序 = `EXIT_REASON_ORDER` ⇒ 调用方（含 JS 的 `Object.keys`）拿到的是
    **稳定顺序**，不必再排一次。
    """
    p = resolved_exit_params(exit_params)
    return {
        "sl": "止损",
        "breakeven": "保本({}R)".format(_fmt_r(p["breakeven_trigger_r"])),
        "trailing": "跟踪止盈",
    }


def exit_reason_legend(exit_params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """出场原因三选一的**口径说明**（悬浮提示 / 图例用，一句话讲清怎么触发的）。

    与 `exit_reason_labels` 同源同参：两个函数各写一份数字的后果是
    "标签写 1R、图例写 0.5R"这种只坏一半的漂移。
    """
    p = resolved_exit_params(exit_params)
    return {
        "sl": "止损：收盘价跌破止损保护线离场。保护线 = 入场价 − R"
              "（R = max(结构距离, 2×ATR)），从未被抬过。",
        "breakeven": "保本：浮盈 > {tr}R 后把保护线抬到入场价{sign}{buf}R 处，"
                     "收盘价跌破该线离场。".format(
                         tr=_fmt_r(p["breakeven_trigger_r"]),
                         sign=("+" if p["breakeven_buffer_r"] >= 0 else "−"),
                         buf=_fmt_r(abs(p["breakeven_buffer_r"]))),
        "trailing": "跟踪止盈：浮盈 > {wl}R 后启动跟踪，保护线 = 至今最有利价 − {td}R，"
                    "收盘价跌破该线离场（该笔锁住多少 R 见行内 R 倍数）。".format(
                        wl=_fmt_r(p["win_loss_ratio"]),
                        td=_fmt_r(p["trailing_trigger_r"])),
    }


def sort_exit_reasons(keys) -> List[str]:
    """原因键 → `EXIT_REASON_ORDER` 稳定序（未知键按字典序排在其后）。

    未知键**不丢**：直接丢弃会让"引擎哪天多出一种 reason"在报告里凭空消失
    （宁可显示一个没见过的英文词，也不要少一行）。
    """
    known = {k: i for i, k in enumerate(EXIT_REASON_ORDER)}
    return sorted(keys, key=lambda k: (known.get(str(k), len(known)), str(k)))


def caliber_lines(result: RunResult) -> List[str]:
    """口径行（区间 / 周期 / 出场参数 / 费率 / 三条偏离）。

    指数标的（`result.is_index`）额外追加一条**不适用**声明：指数不可交易
    ⇒ 费率 / `target_amount` / 股数 / 成本那一整套「元口径」全是虚构的数字，
    必须在口径行里点名，否则摘要上会印出一串看着像真的成本与倍率
    （实测上证指数 `max_notional` 达 `target_amount` 的 7.9 倍 —— 那是因为
    指数点位高、`target_amount/price` 落到了 `min_lot` 上，与"买不买得起"无关）。
    纯**价格**口径（区间 / 周期 / 出场参数 / 三条偏离）对指数依然成立，故保留。
    """
    lo = result.start_dt or "(不限)"
    hi = result.target_dt or "(不限)"
    # 口径行必须印**这次实际用的**出场参数（`result.exit_params`），不是回测侧那份常量 ——
    # 调用方经 `run(exit_params=...)` 换过参数时，拿常量印就等于报告与结果不符
    # （「换个配置数字就变了」却无人察觉，正是口径行的存在意义）。
    # 文案格式保持不变（`{}`.format(float) → "3.0"）：这行是对外口径文本，改动要有理由。
    _ep = resolved_exit_params(result.exit_params)
    lines = [
        "标的 {}  周期 {}  区间 [{}, {}]".format(
            "{}{}".format(result.market, result.code), result.freq, lo, hi),
        "出场参数 win_loss_ratio={} / trailing_trigger_r={}（毛 R 不扣成本，§5.4c）".format(
            _ep["win_loss_ratio"], _ep["trailing_trigger_r"]),
        "费率 佣金 k={:g}（含规费与过户费）/ 最低佣金 m={:g} 元 / 印花税 s={:g}（仅卖出）/ "
        "过户费 t={:g}；名义 c=2k+s={:.4%}；target_amount=m/k={:.0f} 元".format(
            COMMISSION_RATE, MIN_COMMISSION_CASH, STAMP_DUTY_RATE, TRANSFER_FEE_RATE,
            NOMINAL_COST_RATE, TARGET_AMOUNT),
        "偏离披露：① 不套 T+1（按 T+0）；② 不建模涨跌停 / 停牌；③ 前复权价（非真实成交价）",
        # §8.10 后半：前端护栏之外，**报告口径行同样要写明**单窗态 / 未启用区间套。
        # 前端只挡住"面板打开着却切成双窗"这一种情形，API 可被直接调用 ⇒ 报告侧必须自陈。
        "窗口口径 单窗态：仅当前周期序列，未启用区间套"
        "（双窗 / 副级别联立的买卖点不在本次口径内）",
    ]
    if result.is_index:
        lines.append(
            "⚠ 指数标的（不可交易）：上方「费率 / target_amount」与股数、成本、净收益率"
            "均不适用（该套数字在指数上没有对应标的物）；"
            "本次结果中仅区间 / 周期 / 出场参数 / 三类偏离以及价格侧指标"
            "（胜率、毛 R、盈亏比、持仓根数）有效。")
    return lines


def summary_text(result: RunResult, metrics: Metrics | None = None) -> str:
    """控制台摘要（口径先行 + 指标 + 明细）。"""
    m = metrics if metrics is not None else compute(result)
    _lab = exit_reason_labels(result.exit_params)
    L = list(caliber_lines(result))
    L.append("")
    L.append("K 线 {} 根   首见信号 {} 个（放行开仓 {} / 类型过滤 {} / 持仓期拒收 {}）".format(
        result.bars_total, result.signals_seen,
        len(result.trades), m.filtered, m.rej))
    L.append("交易笔数 {}（已平 {} / 未平 {}）".format(
        len(result.trades), m.n, m.u))
    if m.n:
        L.append("胜/亏/平 = {}/{}/{}   胜率 {}".format(
            m.w, m.l, m.e,
            "-" if m.win_rate is None else "%.1f%%" % (m.win_rate * 100)))
        # 指数不可交易 ⇒ 净收益率（成本口径）标「不适用」，不印一个虚构数字。
        # 期望 R 是**毛**口径（纯价格），指数与个股同样成立，照常印。
        L.append("期望 R（毛）= {}   平均净收益率 = {}".format(
            "-" if m.expectancy_r is None else "%.4f" % m.expectancy_r,
            "(不适用·指数)" if result.is_index else
            ("-" if m.avg_net_return is None else "%.4f%%" % (m.avg_net_return * 100))))
        L.append("盈亏比 = {}   盈利因子 = {}   平均持仓根数 = {}".format(
            "-" if m.profit_loss_ratio is None else "%.4f" % m.profit_loss_ratio,
            "-" if m.profit_factor is None else "%.4f" % m.profit_factor,
            "-" if m.avg_bars_held is None else "%.1f" % m.avg_bars_held))
        L.append("分类型 " + ", ".join(
            "{}:n={} avgR={}".format(
                k, v["n"],
                "-" if v.get("expectancy_r") is None else "%.3f" % v["expectancy_r"])
            for k, v in sorted(m.by_bsp_type.items())))
        L.append("分原因 " + ", ".join(
            "{}:n={}".format(_lab.get(k, k), v["n"])
            for k, v in ((k, m.by_reason[k]) for k in sort_exit_reasons(m.by_reason))))
    if result.type_appended_after_freeze:
        L.append("⚠ 冻结后类型追加计数 = {}（预期 0 ⇒ 须回来复核 §2.6 定案）"
                 .format(result.type_appended_after_freeze))
    return "\n".join(L)


def trades_table(result: RunResult) -> str:
    """逐笔明细表（控制台用，等宽）。

    原因列印 `exit_reason_labels` 的中文（`sl`/`breakeven`/`trailing` → 止损 / 保本(1R) /
    跟踪止盈）；未平仓笔的原因列留空 —— 它还没有出场，填「持仓中」到原因列是把
    状态冒充成原因。

    中文列宽按 2 个显示列算（`%-16s` 对应 8 个汉字）—— 等宽表在 CJK 下只能近似对齐，
    这不是 bug，控制台本来就不是主界面（页面面板才是）。
    """
    _lab = exit_reason_labels(result.exit_params)
    head = "%-4s %-12s %-5s %-5s %10s %8s  %-12s %5s %9s %-16s %-14s" % (
        "id", "入场日", "方向", "类", "入场价", "R", "出场日", "持仓", "R倍数", "原因", "未平仓浮动")
    rows = [head]
    for t in result.trades:
        if t.open_:
            reason = ""
            unreal = "-" if t.unrealized_r is None else "%.4f" % t.unrealized_r
        else:
            reason = _lab.get(str(t.exit_reason), str(t.exit_reason or "-"))
            unreal = ""
        rows.append("%-4d %-12s %-5s %-5s %10.3f %8.3f  %-12s %5s %9s %-16s %-14s" % (
            t.trade_id, t.entry_date, t.side, t.bsp_type, t.entry_price, t.r_distance,
            t.exit_date or "(未平仓)", t.bars_held if t.bars_held is not None else "-",
            "-" if t.r_multiple is None else "%.4f" % t.r_multiple,
            reason, unreal))
    return "\n".join(rows)

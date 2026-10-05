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
"""
from __future__ import annotations

import csv
import io
from typing import Any, Dict, List

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


def caliber_lines(result: RunResult) -> List[str]:
    """口径行（区间 / 周期 / 出场参数 / 费率 / 三条偏离）。"""
    from .ExitParams import STOCK_EXIT_PARAMS
    lo = result.start_dt or "(不限)"
    hi = result.target_dt or "(不限)"
    return [
        "标的 {}  周期 {}  区间 [{}, {}]".format(
            "{}{}".format(result.market, result.code), result.freq, lo, hi),
        "出场参数 win_loss_ratio={} / trailing_trigger_r={}（毛 R 不扣成本，§5.4c）".format(
            STOCK_EXIT_PARAMS["win_loss_ratio"], STOCK_EXIT_PARAMS["trailing_trigger_r"]),
        "费率 佣金 k={:g}（含规费与过户费）/ 最低佣金 m={:g} 元 / 印花税 s={:g}（仅卖出）/ "
        "过户费 t={:g}；名义 c=2k+s={:.4%}；target_amount=m/k={:.0f} 元".format(
            COMMISSION_RATE, MIN_COMMISSION_CASH, STAMP_DUTY_RATE, TRANSFER_FEE_RATE,
            NOMINAL_COST_RATE, TARGET_AMOUNT),
        "偏离披露：① 不套 T+1（按 T+0）；② 不建模涨跌停 / 停牌；③ 前复权价（非真实成交价）",
    ]


def summary_text(result: RunResult, metrics: Metrics | None = None) -> str:
    """控制台摘要（口径先行 + 指标 + 明细）。"""
    m = metrics if metrics is not None else compute(result)
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
        L.append("期望 R（毛）= {}   平均净收益率 = {}".format(
            "-" if m.expectancy_r is None else "%.4f" % m.expectancy_r,
            "-" if m.avg_net_return is None else "%.4f%%" % (m.avg_net_return * 100)))
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
            "{}:n={}".format(k, v["n"]) for k, v in sorted(m.by_reason.items())))
    if result.type_appended_after_freeze:
        L.append("⚠ 冻结后类型追加计数 = {}（预期 0 ⇒ 须回来复核 §2.6 定案）"
                 .format(result.type_appended_after_freeze))
    return "\n".join(L)


def trades_table(result: RunResult) -> str:
    """逐笔明细表（控制台用，等宽）。"""
    head = "%-4s %-12s %-5s %-5s %10s %8s  %-12s %5s %9s %-10s" % (
        "id", "入场日", "方向", "类", "入场价", "R", "出场日", "持仓", "R倍数", "原因")
    rows = [head]
    for t in result.trades:
        rows.append("%-4d %-12s %-5s %-5s %10.3f %8.3f  %-12s %5s %9s %-10s" % (
            t.trade_id, t.entry_date, t.side, t.bsp_type, t.entry_price, t.r_distance,
            t.exit_date or "(未平仓)", t.bars_held if t.bars_held is not None else "-",
            "-" if t.r_multiple is None else "%.4f" % t.r_multiple,
            t.exit_reason or "-"))
    return "\n".join(rows)

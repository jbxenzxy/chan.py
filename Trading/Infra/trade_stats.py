# -*- coding: utf-8 -*-
"""成交统计（账户无关的已兑现往返汇总）。

供 K 线页「成交统计」面板：历史盈亏曲线 / 实际胜率 / 平均盈亏比 /
最大单笔盈亏损 / 总净盈亏 / 期望值。

设计要点：
  · 纯计算 + 只读取，零副作用。DB 读取用 sqlite3 只读模式（uri=mode=ro），
    绝不触发 Store 的 schema 迁移写操作（Store.__init__ 会写库）。
  · 账户无关：trades 表无 broker/账户列，simnow/实盘天然合并；调用方
    传入多个 db 路径即 union。
  · symbol 匹配用**双向后缀 GLOB**：前端 chartData.meta.symbol 可能是
    "KQ.m@CFFEX.IF2609"，而库内 pos.symbol 可能是 "IF2609"（或反过来），
    任一方是另一方后缀即可命中，兼容各代码约定。
  · **读库结果逐库上报**（2026-09-16 补）：见 `load_trades_report`。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional

# 读库状态码（`load_trades_report` 的 sources[].status）
SRC_OK = "ok"                    # 读取成功（rows=0 也可能是真的没有成交）
SRC_MISSING = "missing"          # 文件不存在 —— 尚未产生过自动下单记录，**非故障**
SRC_OPEN_FAILED = "open_failed"  # sqlite 打不开（权限 / 路径非法 / 磁盘故障）
SRC_QUERY_FAILED = "query_failed"  # 打开了但 SELECT 失败（库损坏 / 旧 schema）
# 判定"真故障"的口径：不是 ok 也不是 missing。missing 是正常的"没跑过"。
_FAILED_STATUSES = (SRC_OPEN_FAILED, SRC_QUERY_FAILED)


def load_trades_report(db_paths: List[str],
                       symbol: Optional[str] = None) -> Dict[str, Any]:
    """从若干 state.db（只读）取 trades 行，**并把每个库的读取结果一并报出**。

    symbol 为空 → 不按品种过滤（跨库取全部）。
    匹配规则：exact OR 双向后缀 GLOB（见模块 docstring）。

    返回::

        {
          "rows":       List[Dict]   合并后的成交行（按 exit_at 升序）
          "sources":    List[Dict]   每库一条 {path, status, rows, error}
          "dbs_total":  int          实际扫描的库数
          "dbs_ok":     int          读取成功的库数
          "dbs_failed": int          真故障的库数（不含 missing）
        }

    `status` 取值为模块级 `SRC_*` 常量之一。其中 **`query_failed` 最危险** ——
    它长得和"这个品种没有成交"一模一样（旧 schema 里 `trades` 表被改名为
    `trades_legacy_points`、或库文件损坏，都会走到这里）。

    **为什么不再静默跳过（2026-09-16 修）**：旧实现对任一库的失败一律 `continue`
    / `pass`，最终只返回一个空列表 —— 调用方无法区分"真的没有成交"与"读不出来"，
    于是面板会理直气壮地显示「该品种暂无历史成交」，哪怕库里躺着几百笔。
    失败必须可见，这个函数就是为了让调用方拿到这个区分。
    """
    rows: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []

    for db in db_paths:
        rec: Dict[str, Any] = {"path": db, "status": SRC_OK,
                               "rows": 0, "error": None}
        if not os.path.isfile(db):
            rec["status"] = SRC_MISSING
            rec["error"] = "文件不存在"
            sources.append(rec)
            continue
        try:
            con = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
        except sqlite3.Error as e:
            rec["status"] = SRC_OPEN_FAILED
            rec["error"] = "{}: {}".format(type(e).__name__, e)
            sources.append(rec)
            continue
        try:
            con.row_factory = sqlite3.Row
            if symbol:
                cur = con.execute(
                    "SELECT * FROM trades "
                    "WHERE symbol=? OR ? GLOB ('*'||symbol) "
                    "OR symbol GLOB ('*'||?) "
                    "ORDER BY exit_at",
                    (symbol, symbol, symbol))
            else:
                cur = con.execute("SELECT * FROM trades ORDER BY exit_at")
            got = [dict(r) for r in cur.fetchall()]
            rows.extend(got)
            rec["rows"] = len(got)
        except sqlite3.Error as e:
            rec["status"] = SRC_QUERY_FAILED
            rec["error"] = "{}: {}".format(type(e).__name__, e)
        finally:
            con.close()
        sources.append(rec)

    rows.sort(key=lambda r: r.get("exit_at") or "")
    return {
        "rows": rows,
        "sources": sources,
        "dbs_total": len(sources),
        "dbs_ok": sum(1 for s in sources if s["status"] == SRC_OK),
        "dbs_failed": sum(1 for s in sources
                          if s["status"] in _FAILED_STATUSES),
    }


def compute_trade_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一组已合并、按 exit_at 升序的成交记录，算统计摘要。

    字段（来自 trades 表 schema）：symbol, side, volume, entry_price, exit_price,
    entry_at, exit_at, reason, gross_points, cost_cash, net_cash, bars_held,
    exit_plan_name, exit_plan_params。

    返回（count==0 时见 _empty_stats）：
      count / wins / losses / flat   总笔数 / 净>0 / 净<0 / 净==0
      win_rate          实际胜率 = wins / count（平手计入分母、不计胜）
      avg_win / avg_loss   盈利笔均值 / 亏损笔均值（元）
      profit_factor     总盈 / |总亏|；无亏损→None（无定义，非 0）
      avg_pl_ratio      平均盈亏比 = avg_win / |avg_loss|；无亏损→None
      total_net         总净盈亏（元）
      max_win / max_loss    最大单笔盈/亏（含 trade_id、exit_at、net_cash）
      expectancy        期望收益 = win_rate*avg_win + loss_rate*avg_loss（元/笔）
      equity_curve      累计净值序列：[{exit_at, net_cash, cumulative}]
      by_reason         按出场原因（tp/sl/trailing/time…）拆分的净盈亏合计
    """
    if not trades:
        return _empty_stats()

    def _net(t: Dict[str, Any]) -> float:
        v = t.get("net_cash")
        return float(v) if isinstance(v, (int, float)) else 0.0

    wins = [t for t in trades if _net(t) > 0]
    losses = [t for t in trades if _net(t) < 0]
    flat = [t for t in trades if _net(t) == 0]
    count = len(trades)

    win_rate = (len(wins) / count) if count else 0.0
    loss_rate = (len(losses) / count) if count else 0.0

    gross_win = sum(_net(t) for t in wins)
    gross_loss = sum(_net(t) for t in losses)      # 负值
    abs_gross_loss = -gross_loss if gross_loss else 0.0

    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    abs_avg_loss = -avg_loss if avg_loss else 0.0

    profit_factor = (gross_win / abs_gross_loss) if abs_gross_loss > 0 else None
    avg_pl_ratio = (avg_win / abs_avg_loss) if abs_avg_loss > 0 else None

    total_net = gross_win + gross_loss

    best = max(trades, key=_net)
    worst = min(trades, key=_net)
    max_win = {"trade_id": best.get("trade_id"),
               "exit_at": best.get("exit_at"),
               "net_cash": round(_net(best), 2)}
    max_loss = {"trade_id": worst.get("trade_id"),
                "exit_at": worst.get("exit_at"),
                "net_cash": round(_net(worst), 2)}

    expectancy = win_rate * avg_win + loss_rate * avg_loss

    equity_curve: List[Dict[str, Any]] = []
    cum = 0.0
    for t in trades:
        nc = _net(t)
        cum += nc
        equity_curve.append({
            "exit_at": t.get("exit_at"),
            "net_cash": round(nc, 2),
            "cumulative": round(cum, 2),
        })

    by_reason: Dict[str, float] = {}
    for t in trades:
        r = t.get("reason") or "unknown"
        by_reason[r] = by_reason.get(r, 0.0) + _net(t)

    return {
        "symbol": trades[0].get("symbol"),
        "count": count,
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(flat),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": (round(profit_factor, 4)
                          if profit_factor is not None else None),
        "avg_pl_ratio": (round(avg_pl_ratio, 4)
                         if avg_pl_ratio is not None else None),
        "total_net": round(total_net, 2),
        "max_win": max_win,
        "max_loss": max_loss,
        "expectancy": round(expectancy, 2),
        "equity_curve": equity_curve,
        "by_reason": {k: round(v, 2) for k, v in by_reason.items()},
    }


def _empty_stats() -> Dict[str, Any]:
    return {
        "symbol": None,
        "count": 0, "wins": 0, "losses": 0, "flat": 0,
        "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "profit_factor": None, "avg_pl_ratio": None,
        "total_net": 0.0,
        "max_win": {"trade_id": None, "exit_at": None, "net_cash": 0.0},
        "max_loss": {"trade_id": None, "exit_at": None, "net_cash": 0.0},
        "expectancy": 0.0,
        "equity_curve": [],
        "by_reason": {},
    }

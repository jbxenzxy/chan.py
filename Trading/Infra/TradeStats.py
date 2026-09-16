# -*- coding: utf-8 -*-
"""成交统计（账户无关的已兑现往返汇总）。

供 K 线页「成交统计」面板：历史盈亏曲线 / 实际胜率 / 平均盈亏比 /
最大单笔盈亏损 / 总净盈亏 / 期望值。

设计要点：
  · 纯计算 + 只读取，零副作用。DB 读取用 sqlite3 只读模式（uri=mode=ro），
    绝不触发 Store 的 schema 迁移写操作（Store.__init__ 会写库）。
  · 账户无关：trades 表无 broker/账户列，simnow/实盘天然合并；调用方
    传入多个 db 路径即 union。
  · **按品种键合并（2026-09-16 修 P0-1）**：匹配口径 = `product_key_of()`
    的品种键相等 —— 盘前在主连上跑（`KQ.m@CFFEX.IF`）、盘后在月份合约上记账
    (`CFFEX.IF2609`)，两边归一后同键 → 合并统计（用户 2026-09-16 拍板）。
    旧实现用「双向后缀 GLOB」，对**真实取值**恒不命中（详见 `product_key_of`）。
  · **读库结果逐库上报**（2026-09-16 补）：见 `load_trades_report`。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional

from .Product import parse_product_key

# 读库状态码（`load_trades_report` 的 sources[].status）
SRC_OK = "ok"                    # 读取成功（rows=0 也可能是真的没有成交）
SRC_MISSING = "missing"          # 文件不存在 —— 尚未产生过自动下单记录，**非故障**
SRC_OPEN_FAILED = "open_failed"  # sqlite 打不开（权限 / 路径非法 / 磁盘故障）
SRC_QUERY_FAILED = "query_failed"  # 打开了但 SELECT 失败（库损坏 / 旧 schema）
# 判定"真故障"的口径：不是 ok 也不是 missing。missing 是正常的"没跑过"。
_FAILED_STATUSES = (SRC_OPEN_FAILED, SRC_QUERY_FAILED)


def product_key_of(symbol: Optional[str]) -> str:
    """把「任意写法的合约符号」归一到**品种键** —— 统计合并的唯一口径。

    例::

        product_key_of("KQ.m@CFFEX.IF")  -> "IF"    # 主连（前端 chartData.meta.symbol）
        product_key_of("CFFEX.IF2609")   -> "IF"    # 月份合约（库内 trades.symbol）
        product_key_of("SHFE.au2512")    -> "AU"
        product_key_of("IF2609")         -> "IF"    # 裸代码（无交易所前缀）
        product_key_of("")               -> ""

    为什么必须换掉「双向后缀 GLOB」（旧实现，本文件 2026-09-16 前的口径）：
      库内 `trades.symbol` = 月份合约 `CFFEX.IF2609`，前端传的是主连
      `KQ.m@CFFEX.IF` —— **两者互不为后缀**（`KQ.m@CFFEX.IF` 里根本没有
      "IF2609" 这段），于是查询**恒返回 0 笔**，面板永远显示「该品种暂无历史成交」。
      旧验证之所以没发现：它刻意挑了**互为后缀**的一对
      （`IF2609` ↔ `CFFEX.IF2609`），那条用例**在设计上就必然通过**。

    归一规则**只有一份**：转发 `Product.parse_product_key`（末段 + 剥月份 +
    转大写），不在这里重写第二份正则 —— 否则两处规则漂移时，"哪个键算同一品种"
    会出现两个答案。裸代码（无 "."）补一个前导点即可复用同一套规则。
    """
    s = str(symbol or "").strip()
    if not s:
        return ""
    return parse_product_key(s if "." in s else "." + s)


def load_trades_report(db_paths: List[str],
                       symbol: Optional[str] = None) -> Dict[str, Any]:
    """从若干 state.db（只读）取 trades 行，**并把每个库的读取结果一并报出**。

    symbol 为空 → 不按品种过滤（跨库取全部）。
    symbol 非空 → 按**品种键相等**过滤（同品种的多个合约月份合并，见
    `product_key_of`）。

    返回::

        {
          "rows":       List[Dict]   合并后的成交行（按 exit_at 升序）
          "sources":    List[Dict]   每库一条 {path, status, rows, error}
          "dbs_total":  int          实际扫描的库数
          "dbs_ok":     int          读取成功的库数
          "dbs_failed": int          真故障的库数（不含 missing）
          "symbol_raw": str          调用方传入的原始符号（"" = 未过滤）
          "symbol_key": str          归一后的品种键；"" = 传了符号但解析不出品种
          "by_symbol":  Dict[str,int] 命中行的**合约拆分**（symbol → 笔数）
        }

    `status` 取值为模块级 `SRC_*` 常量之一。其中 **`query_failed` 最危险** ——
    它长得和"这个品种没有成交"一模一样（旧 schema 里 `trades` 表被改名为
    `trades_legacy_points`、或库文件损坏，都会走到这里）。

    **为什么过滤放在 Python 侧**：品种键是「剥掉合约月份」后的派生值，SQL 里
    没有这个函数；若改写成 `LIKE '%IF%'` 之类，等于把归一规则抄成第二份，
    两处迟早漂移。trades 行数是百量级，全表读回再过滤的代价可以忽略。

    ⚠️ **「传了符号」与「解析不出品种键」必须分开**（本文件第一版踩过）：若判据写成
    `if key:`，当 `symbol="CFFEX."`（末段为空 → key=""）时会走 else 分支 —— **不过滤**，
    于是把**全库所有品种**的成交当成这次查询的结果返回。比返回 0 笔危险得多：
    面板会显示一个"有数据但串了品种"的假象。故判据 = `want_filter`（传没传符号），
    `want_filter and not key` → 一行都不匹配。

    **为什么不再静默跳过（2026-09-16 修）**：旧实现对任一库的失败一律 `continue`
    / `pass`，最终只返回一个空列表 —— 调用方无法区分"真的没有成交"与"读不出来"，
    于是面板会理直气壮地显示「该品种暂无历史成交」，哪怕库里躺着几百笔。
    失败必须可见，这个函数就是为了让调用方拿到这个区分。
    """
    raw = str(symbol or "").strip()
    want_filter = bool(raw)
    key = product_key_of(raw) if want_filter else ""
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
            got = [dict(r) for r in
                   con.execute("SELECT * FROM trades ORDER BY exit_at")]
            if want_filter:
                # key 为空 = 解析不出品种键 → 一行都不匹配（**绝不退回"不过滤"**）。
                got = ([r for r in got
                        if product_key_of(r.get("symbol")) == key]
                       if key else [])
            rows.extend(got)
            rec["rows"] = len(got)
        except sqlite3.Error as e:
            rec["status"] = SRC_QUERY_FAILED
            rec["error"] = "{}: {}".format(type(e).__name__, e)
        finally:
            con.close()
        sources.append(rec)

    rows.sort(key=lambda r: r.get("exit_at") or "")
    by_symbol: Dict[str, int] = {}
    for r in rows:
        s = str(r.get("symbol") or "")
        by_symbol[s] = by_symbol.get(s, 0) + 1

    return {
        "rows": rows,
        "sources": sources,
        "dbs_total": len(sources),
        "dbs_ok": sum(1 for s in sources if s["status"] == SRC_OK),
        "dbs_failed": sum(1 for s in sources
                          if s["status"] in _FAILED_STATUSES),
        "symbol_raw": str(symbol or ""),
        "symbol_key": key,
        "by_symbol": by_symbol,
    }


def compute_trade_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一组已合并、按 exit_at 升序的成交记录，算统计摘要。

    字段（来自 trades 表 schema）：symbol, side, volume, entry_price, exit_price,
    entry_at, exit_at, reason, gross_points, cost_cash, net_cash, bars_held,
    exit_plan_name, exit_plan_params。

    ⚠️ 返回值里**没有 `symbol` 字段**（2026-09-16）：一行成交一个合约，而统计
    口径是**整个品种**（多合约月份合并），取任意一行的 symbol 都是误导。
    "统计的是哪个品种"由调用方用 `load_trades_report` 的 `symbol_key` 给出。

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

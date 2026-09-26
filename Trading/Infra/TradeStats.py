# -*- coding: utf-8 -*-
"""成交统计（账户无关的已兑现往返汇总）。

供 K 线页「成交统计」面板：历史盈亏曲线 / 实际胜率 / 盈亏比 /
最大单笔盈亏损 / 总净盈亏 / 期望值。

设计要点：
  · 纯计算 + 只读取，零副作用。DB 读取用 sqlite3 只读模式（uri=mode=ro），
    绝不触发 Store 的 schema 迁移写操作（Store.__init__ 会写库）。
  · 账户无关：trades 表无 broker/账户列，simnow/实盘天然合并；调用方
    传入多个 db 路径即 union。
  · **按品种键合并**：盘前在主连上跑（`KQ.m@CFFEX.IF`）、盘后在月份合约上记账
    (`CFFEX.IF2609`) —— 两边归一后同键 → 合并统计（用户拍板）。
    旧实现用「双向后缀 GLOB」，对**真实取值**恒不命中（详见
    `Product.product_key_of`）。
  · **归一已搬到写入侧** 品种键由 `Store.save_trade`
    落进 `trades.product_key` 列，本模块的过滤判据因此是**列相等**
    （`WHERE product_key = ?`），**不再解读 `symbol`**。这样"同一批成交算不算
    同一品种"只有一个答案来源（写入那一刻算好的键），而不是"写库一份 symbol +
    查库时现算一次"两处口径。
  · **读库结果逐库上报**（补）：见 `load_trades_report`。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional

from .Product import product_key_of

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
    symbol 非空 → 按**品种键相等**过滤（同品种的多个合约月份合并）：先把传入
    的符号归一成品种键（`Product.product_key_of`），再拿它去比**列**
    `trades.product_key`（写入侧落好的，见 `StateDB.save_trade`）。

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

    **为什么判据是「列相等」而不是「查库时现算」**
    旧实现读回全表后逐行 `product_key_of(row["symbol"])` —— 于是同一件事有了
    两个口径来源：写库那一刻的 symbol 与查库时现算的键。归一搬到写入侧后，
    "这笔成交属于哪个品种"在**落库时**就定好了，查询只做 `WHERE product_key = ?`。
    这也顺手去掉了"读回全表再过滤"的隐含前提（SQL 侧不能算品种键）。

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
            if want_filter and not key:
                # 传了符号但解析不出品种键 → 一行都不匹配
                # （**绝不退回"不过滤"**：那会把全库成交当成这次查询的结果，
                # 面板会显示一个"有数据但串了品种"的假象）。
                got = []
            elif key:
                got = [dict(r) for r in con.execute(
                    "SELECT * FROM trades WHERE product_key = ?"
                    " ORDER BY exit_at", (key,))]
            else:
                got = [dict(r) for r in
                       con.execute("SELECT * FROM trades ORDER BY exit_at")]
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


def filter_trades_by_bsp_type(trades: List[Dict[str, Any]],
                             allowed_types: Optional[Any]) -> List[Dict[str, Any]]:
    """按允许的买卖点类型过滤成交行（统计侧对「买卖点类型过滤」SSOT 的落地）。

    与自动下单共用同一份勾选（`state.db` 的 `bsp_type_filter`）：用户取消勾选的
    某类，其历史成交不计入统计（总净盈亏 / 胜率 / 盈亏比 … 一并重算）。

    allowed_types 为 None → 不过滤（全部放行，等价于「用户从未设置过滤」）。
    allowed_types 为可迭代的 str → 只保留「signal_key 中段（逗号拆段后）至少有一段
    落在允许集合内」的成交。

    ⚠️ 放行口径刻意与 `Trading/Engine/Engine._bsp_type_allowed` **完全一致**：
    signal_key 格式 `date|bsp_type|is_buy`（`Records.make_key`），中段可能是逗号串
    （同一右肩 K 合并出的多类型，如 "1,11"）—— 任一段被允许即保留，避免把
    「引擎当初放行过」的成交在统计侧误排除，保证「显示 / 下单 / 统计」三处口径一致。
    signal_key 中段完全不含 0/1/2/3（人工单 / 回放 RUN 等）的成交：allowed_types
    为 None 时保留（无过滤）；已设过滤时因没有任何 0-3 段命中允许集 → 排除
    （与引擎对「非勾选类型一律忽略」同一保守方向）。
    """
    if allowed_types is None:
        return list(trades)
    allowed = set(str(t) for t in allowed_types)
    out: List[Dict[str, Any]] = []
    for t in trades:
        sk = t.get("signal_key") or ""
        parts = sk.split("|")
        bt = parts[1] if len(parts) >= 2 else ""
        segs = [s.strip() for s in bt.split(",") if s.strip()]
        if any(s in allowed for s in segs):
            out.append(t)
    return out


def compute_trade_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一组已合并、按 exit_at 升序的成交记录，算统计摘要。

    字段（来自 trades 表 schema）：symbol, side, volume, entry_price, exit_price,
    entry_at, exit_at, reason, gross_points, cost_cash, net_cash, bars_held,
    exit_plan_name, exit_plan_params。

    ⚠️ **金额口径 = `net_cash`（净额，已扣双边手续费）** —— 本函数所有金额
    （avg_win / avg_loss / total_net / max_win / max_loss / expectancy /
    equity_curve / by_reason）以及"盈利笔 / 亏损笔"的**分类**，读的都是
    `net_cash`；`gross_points` 与 `cost_cash` 只作原始记录，**不参与统计**。
    所以"毛利为正、被手续费倒扣成净亏"的那一笔，在这里算**亏损笔**
    👉 这是刻意的：口径与账户真实到账一致。
    净额的形成在别处（本函数不重算）：`Engine._book_close` 里
    `net_cash = gross×乘数×手数 − cost`，`cost = (开仓档 + 离场档) × 手数`
    （见 `Instrument.cost_cash`；离场档按平今 / 平昨两档取）。
    回归钉在 `Trading/Test/test_trade_stats_formulas.py [H]`（造 gross 与 net
    符号相反的样本，防止哪天改读 `gross_points` 而全部断言仍绿）。

    ⚠️ 返回值里**没有 `symbol` 字段** 一行成交一个合约，而统计
    口径是**整个品种**（多合约月份合并），取任意一行的 symbol 都是误导。
    "统计的是哪个品种"由调用方用 `load_trades_report` 的 `symbol_key` 给出。

    返回（count==0 时见 _empty_stats）：
      count / wins / losses / flat   总笔数 / 净>0 / 净<0 / 净==0
      win_rate          实际胜率 = wins / count（平手计入分母、不计胜）
      avg_win / avg_loss   盈利笔均值 / 亏损笔均值（元，净额口径）
      pl_ratio          盈亏比 = avg_win / |avg_loss|；无亏损→None
      profit_factor     盈利因子 = 总盈 / |总亏|；无亏损→None（无定义，非 0）
      total_net         总净盈亏（元）= Σnet_cash
      max_win / max_loss    最大单笔盈/亏（含 trade_id、exit_at、net_cash）；
                            只在**同侧**成交里取：该侧一笔都没有时
                            net_cash=0.0、trade_id/exit_at=None
      expectancy        期望收益 = win_rate*avg_win + loss_rate*avg_loss（元/笔）
      equity_curve      累计净值序列：[{exit_at, net_cash, cumulative}]
      by_reason         按出场**规则身份**（reason）分组：{key: {n, wins, losses,
                        flat, net}}。**刻意不按「止盈 / 止损」分**：reason 只回答
                        "哪条规则触发的离场"（breakeven 保本层保护价 / trailing
                        跟踪层保护价 / sl 初始止损线 / auto_order_off* /
                        reconcile_* …），而"这笔赚没赚"是**成交结果** —— 保本离场
                        名义上是止盈、被滑点打成净亏的也有，跟踪离场同理；把 reason
                        映射成止盈/止损必然要在"愿望"和"实际"之间二选一，两边都不对。
                        要看盈亏就读组内的 wins / losses / net（与本函数顶层同一套
                        net_cash 三分口径），"保本这一组到底赚没赚"因此可直接读出。
      by_bsp_type       按买卖点类型（0/1/2/3 类）分组：{type: {n, wins, losses}}。
                       类型取自每条成交的 `signal_key` 中段 —— `Records.make_key`
                       的格式是 `date|bsp_type|is_buy`（见 Records.py:186），自动下单
                       成交的 signal_key 即该格式（引擎 `_run_signal_key = sig.key`，
                       sig.key 对 BSP 信号就是 make_key）。非 0-3 的类型（人工单 /
                       回放 RUN 等）不入桶；组内只计胜/亏，平手（net_cash==0）既不入
                       胜也不入亏，与面板「类胜负（胜/亏）」展示口径一致。用途：面板在
                       "成交笔数"之后加一行"类型胜负"，一眼看出每类买卖点的胜/亏笔数。
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
    pl_ratio = (avg_win / abs_avg_loss) if abs_avg_loss > 0 else None

    total_net = gross_win + gross_loss

    # 最大单笔只在**同一侧**的成交里取。取全样本极值时，全亏的品种会把
    # "亏得最少的那一笔"当成最大盈利报出去（负的"盈利"自相矛盾）。某一侧
    # 一笔都没有 → 这个数不存在，报 0（用户拍板 2026-09-19）。
    best = max(wins, key=_net) if wins else None
    worst = min(losses, key=_net) if losses else None
    max_win = {"trade_id": best.get("trade_id") if best else None,
               "exit_at": best.get("exit_at") if best else None,
               "net_cash": round(_net(best), 2) if best else 0.0}
    max_loss = {"trade_id": worst.get("trade_id") if worst else None,
                "exit_at": worst.get("exit_at") if worst else None,
                "net_cash": round(_net(worst), 2) if worst else 0.0}

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

    # by_reason：按出场**规则身份**（reason）分组，每组给笔数 + 该组内的胜 / 负 / 平笔数
    #   + 净额合计。分组键是规则，不是盈亏 —— 理由见 summarize 的 docstring「为什么
    #   by_reason 不按止盈/止损分」，组内 wins / losses 用的就是本函数顶层的同一套
    #   net_cash 三分口径（> 0 / < 0 / == 0），刻意不另立第二套标准。
    by_reason: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        r = t.get("reason") or "unknown"
        b = by_reason.setdefault(r, {"n": 0, "wins": 0, "losses": 0,
                                     "flat": 0, "net": 0.0})
        nc = _net(t)
        b["n"] += 1
        b["net"] = round(b["net"] + nc, 2)
        b["wins" if nc > 0 else "losses" if nc < 0 else "flat"] += 1

    # by_bsp_type：按买卖点类型（0/1/2/3 类）分组，每组给胜/亏笔数。
    #   类型取自 signal_key 中段（Records.make_key 的 `date|bsp_type|is_buy`），
    #   自动下单成交的 signal_key 即该格式；非 0-3 的类型（人工/回放 RUN 等）
    #   不入桶。组内只计胜/亏，平手既不入胜也不入亏（与「类胜负（胜/亏）」一致）。
    #   命中口径刻意窄：只答"每类买卖点的胜/亏数量"，不掺金额/净额（那两件事
    #   顶层 count/wins/losses 与 by_reason 已经覆盖）。
    by_bsp_type: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        sk = t.get("signal_key") or ""
        parts = sk.split("|")
        bt = parts[1] if len(parts) >= 2 else ""
        if bt not in ("0", "1", "2", "3"):
            continue
        g = by_bsp_type.setdefault(bt, {"n": 0, "wins": 0, "losses": 0})
        nc = _net(t)
        g["n"] += 1
        if nc > 0:
            g["wins"] += 1
        elif nc < 0:
            g["losses"] += 1

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
        "pl_ratio": (round(pl_ratio, 4)
                     if pl_ratio is not None else None),
        "total_net": round(total_net, 2),
        "max_win": max_win,
        "max_loss": max_loss,
        "expectancy": round(expectancy, 2),
        "equity_curve": equity_curve,
        "by_reason": by_reason,
        "by_bsp_type": by_bsp_type,
    }


def _empty_stats() -> Dict[str, Any]:
    return {
        "count": 0, "wins": 0, "losses": 0, "flat": 0,
        "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "profit_factor": None, "pl_ratio": None,
        "total_net": 0.0,
        "max_win": {"trade_id": None, "exit_at": None, "net_cash": 0.0},
        "max_loss": {"trade_id": None, "exit_at": None, "net_cash": 0.0},
        "expectancy": 0.0,
        "equity_curve": [],
        "by_reason": {},
        "by_bsp_type": {},
    }

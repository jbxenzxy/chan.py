# -*- coding: utf-8 -*-
"""
引擎状态机
==========
事件驱动，只处理两类事件：bar（K 线闭合）与 signal（缠论买卖点）。

每根 K 线的处理顺序（顺序错了结果就错了）
    ① 结算已有持仓 —— 用**刚闭合**的 K 线的 high/low 判止盈止损
    ② 才处理落在这根 K 线上的信号 —— 决定开仓
    反过来会变成"同一根 K 线内既开仓又平仓"，是回测里最常见的作弊来源。

两处时序防护（踩过才知道）
    ① 入场那根 K 线不能参与出场判定：信号在 K 线 T 闭合时产生、按 T 的收盘价开仓，
       若结算也用 T 的高低点，等于开仓瞬间就可能"被止损"。
       因此结算时跳过 bar.timestamp <= position.entry_bar_ts 的 K 线。
    ② 重复/回退的 bar 直接丢弃：SSE 可能重发，或断线重连后补发历史帧。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .brokers.base import Broker
from .config import GatewayConfig
from .events import EventLog
from .risk import RiskGate
from .store import Store
from .strategy.base import EntryPolicy, ExitCheck, ExitPolicy
from .symbols import InstrumentSpec
from .types import (
    Bar, DecisionType, ExitPlan, Position, Signal, Trade, now_cn,
)


class GatewayEngine:
    def __init__(self, cfg: GatewayConfig, broker: Broker,
                 entry_policy: EntryPolicy, exit_policy: ExitPolicy,
                 store: Store, ev: EventLog):
        self.cfg = cfg
        self.spec: InstrumentSpec = cfg.instrument
        self.broker = broker
        self.entry_policy = entry_policy
        self.exit_policy = exit_policy
        self.store = store
        self.ev = ev
        self.risk = RiskGate(cfg.risk, self.spec)

        self.position: Optional[Position] = None
        self.last_bar: Optional[Bar] = None
        self.bars_seen: int = 0
        self._trade_seq = 0
        # P3 配套：close 失败 cooldown 状态。
        # 用途：上一笔 close 被拒后，连续 _close_retry_bars 根 K 线内不再尝试平仓，
        # 避免"每根 bar 都触发一次平仓"导致的死循环；到 _close_max_streak 后
        # 认定持仓为幻影（broker 端不存在），强制从引擎清掉。
        self._last_close_failed_bar_ts: int = 0
        self._close_fail_streak: int = 0
        self._close_retry_bars: int = 5      # 失败后冷却多少根 bar 再试
        self._close_max_streak: int = 20     # 连续失败这么多根后清掉幻影持仓
        self._restore()

    # ---------------- 状态恢复 ----------------
    def _restore(self) -> None:
        pd = self.store.get_json("position")
        if isinstance(pd, dict) and pd.get("symbol"):
            try:
                self.position = Position.from_dict(pd)
                self.ev.write("start", restored_position=self.position.to_dict())
            except Exception as e:
                self.ev.write("error", where="restore_position", err=str(e))
        self.risk.restore(self.store.get_json("day_stats"))
        self.bars_seen = int(self.store.get_json("bars_seen", 0) or 0)

    def _persist(self) -> None:
        if self.position:
            self.store.set_json("position", self.position.to_dict())
        else:
            self.store.delete_key("position")
        self.store.set_json("day_stats", self.risk.snapshot())
        self.store.set_json("bars_seen", self.bars_seen)

    # ---------------- bar 事件 ----------------
    def on_bar(self, bar: Bar) -> None:
        if self.last_bar is not None and bar.timestamp <= self.last_bar.timestamp:
            return                      # 重复或回退的 K 线，丢弃
        self.bars_seen += 1
        self.last_bar = bar

        day = (bar.date or "")[:10]
        if day and self.risk.roll_day(day):
            self.ev.write("day_roll", day=day, stats=dict(self.risk.day_stats))
            self._persist()

        self.ev.write("bar", date=bar.date, close=bar.close,
                      high=bar.high, low=bar.low, seq=self.bars_seen)

        # 心跳：真实 CTP 通道需要定期收发数据，否则被判"用户不活跃"断连。
        # dry_run 通道的 pulse() 是空实现，零开销。
        try:
            self.broker.pulse()
        except Exception:
            pass

        if self.position:
            self._settle_position(bar)

    def _settle_position(self, bar: Bar) -> None:
        pos = self.position
        if pos is None:
            return
        if bar.timestamp <= pos.entry_bar_ts:
            return                      # 入场那根 K 线不参与出场判定

        bars_held = max(0, self.bars_seen - pos.entry_bar_seq)
        check: Optional[ExitCheck] = self.exit_policy.check(
            pos, bar, self.spec, bars_held)

        if check is None:
            if self.cfg.risk.close_before_session_end and self._after_close(bar):
                check = ExitCheck("eod", bar.close)
        if check is None:
            return

        if check.plan is not None:
            pos.exit_plan = check.plan
            self._persist()

        if check.only_update:
            self.ev.write("exit_plan_update", reason=check.reason,
                          stop=pos.exit_plan.stop_price, tp=pos.exit_plan.tp_price,
                          symbol=pos.symbol)
            return

        self._close_position(check.reason, check.price, bar,
                             signal_key=pos.signal_key)

    def _after_close(self, bar: Bar) -> bool:
        """是否已过当日收盘（用于收盘前强平）。中午休市不算。"""
        parts = (bar.date or "").split()
        if len(parts) < 2 or not self.spec.sessions:
            return False
        hm = parts[1][:5]
        last_end = self.spec.sessions[-1].split("-")[-1]
        return hm >= last_end

    # ---------------- signal 事件 ----------------
    def on_signal(self, sig: Signal) -> None:
        if not self.store.try_mark_signal(sig.key, "processing"):
            self.ev.write("signal_dup", key=sig.key, date=sig.date,
                          type=sig.bsp_type, is_buy=sig.is_buy,
                          prev_action=self.store.signal_action(sig.key))
            return

        self.ev.write("signal", key=sig.key, date=sig.date, type=sig.bsp_type,
                      is_buy=sig.is_buy, price=sig.price, high=sig.high, low=sig.low)

        decision = self.entry_policy.decide(sig, self.position, self.spec)
        if not decision:
            self.store.update_signal_action(sig.key, "skip", decision.reason)
            self.ev.write("signal_skip", key=sig.key, reason=decision.reason)
            return

        bar_date = self.last_bar.date if self.last_bar else sig.date

        if decision.type in (DecisionType.CLOSE_AND_HOLD, DecisionType.CLOSE_AND_REVERSE):
            if self.position is not None:
                price = self.last_bar.close if self.last_bar else sig.price
                self._close_position("signal_reverse", price, self.last_bar,
                                     signal_key=sig.key)
            if decision.type is DecisionType.CLOSE_AND_HOLD:
                self.store.update_signal_action(sig.key, "close_only", decision.reason)
                return

        volume = int(self.cfg.risk.max_volume)
        ok, why = self.risk.check_open(decision.side or sig.side, volume, bar_date)
        if not ok:
            self.store.update_signal_action(sig.key, "risk_block", why)
            self.ev.write("risk_block", key=sig.key, side=str(decision.side or sig.side),
                          reason=why, bar_date=bar_date)
            return

        self._open_position(sig, decision.side or sig.side, volume)

    # ---------------- 开 / 平 ----------------
    def _open_position(self, sig: Signal, side, volume: int) -> None:
        o = self.broker.submit("open", side, volume, sig.price, sig.key,
                               note="缠论{}点信号开仓".format("买" if sig.is_buy else "卖"))
        self.store.save_order(o)
        self.ev.write("order", order_id=o.order_id, action=o.action,
                      side=str(o.side), volume=o.volume, price=o.price,
                      req_price=o.req_price, status=o.status, broker=o.broker)

        # 真实下单可能被拒/超时：不开仓，落盘后结束，不产生幽灵持仓
        if o.status != "filled" or o.filled_price is None:
            why = o.meta.get("reject_reason") or o.status
            self.store.update_signal_action(sig.key, "rejected", why)
            self.ev.write("order_rejected", key=sig.key, order_id=o.order_id,
                          reason=why)
            return

        entry_price = o.filled_price
        plan: ExitPlan = self.exit_policy.plan(sig, entry_price, self.spec)

        self.position = Position(
            symbol=self.spec.trade_symbol, side=side, volume=o.volume,
            entry_price=entry_price, entry_at=now_cn(),
            entry_bar_ts=self.last_bar.timestamp if self.last_bar else 0,
            entry_bar_seq=self.bars_seen,
            signal_key=sig.key, open_order_id=o.order_id, exit_plan=plan)
        self._persist()

        self.store.update_signal_action(sig.key, "opened", o.order_id)
        self.ev.write("open", symbol=self.position.symbol, side=str(side),
                      volume=o.volume, entry_price=entry_price,
                      stop=plan.stop_price, tp=plan.tp_price,
                      exit_policy=plan.name, exit_params=plan.params,
                      signal_key=sig.key)

    def _close_position(self, reason: str, trigger_price: float,
                        bar: Optional[Bar], signal_key: str = "") -> None:
        pos = self.position
        if pos is None:
            return

        # P3 配套：连续 close 失败就停手，避免反向信号/SL/TP 在每根 bar 上死循环重试。
        # 触发条件：上一笔 close 被拒（broker 没拿到成交），且 retry cooldown 未过期。
        now_ts = self.last_bar.timestamp if self.last_bar else 0
        if (self._last_close_failed_bar_ts
                and now_ts - self._last_close_failed_bar_ts <= self._close_retry_bars):
            return

        o = self.broker.submit("close", pos.side, pos.volume, trigger_price,
                               signal_key or pos.signal_key, note=reason)
        self.store.save_order(o)
        self.ev.write("order", order_id=o.order_id, action=o.action,
                      side=str(o.side), volume=o.volume, price=o.price,
                      req_price=o.req_price, status=o.status, broker=o.broker,
                      reason=reason)

        # 平仓被拒/超时：保留持仓，等下一根 K 线再试，不凭空平掉
        if o.status != "filled" or o.filled_price is None:
            why = o.meta.get("reject_reason") or o.status
            self.ev.write("order_rejected", key=pos.signal_key, order_id=o.order_id,
                          action="close", reason=reason, reject=why)
            # 记 cooldown：在最近 _close_retry_bars 根 K 线内不再尝试 close，
            # 给 tqsdk/上游同步留时间窗，避免每根 bar 都触发平仓
            self._last_close_failed_bar_ts = now_ts
            # 如果 retry cooldown 内一直失败且持仓实际不在 CTP（phantom），
            # 到上限后强制清掉，避免引擎永久卡死。
            self._close_fail_streak += 1
            if self._close_fail_streak >= self._close_max_streak:
                self.ev.write("position_drop", reason="close_repeatedly_rejected",
                              streak=self._close_fail_streak,
                              pos_signal_key=pos.signal_key,
                              pos_entry=pos.entry_price)
                self.position = None
                self._persist()
            return

        # 成功平仓：清掉失败计数
        self._last_close_failed_bar_ts = 0
        self._close_fail_streak = 0

        exit_price = o.filled_price
        gross = pos.pnl_points(exit_price)
        cost = self.spec.cost_points(pos.entry_price, exit_price,
                                     close_today=self.spec.close_today_first)
        net = gross - cost
        cash = self.spec.points_to_cash(net, pos.volume)
        bars_held = max(0, self.bars_seen - pos.entry_bar_seq)

        self._trade_seq += 1
        t = Trade(
            trade_id="T{:05d}".format(self._trade_seq), symbol=pos.symbol,
            side=pos.side, volume=pos.volume, entry_price=pos.entry_price,
            exit_price=exit_price, entry_at=pos.entry_at, exit_at=now_cn(),
            reason=reason, gross_points=round(gross, 4),
            cost_points=round(cost, 4), net_points=round(net, 4),
            net_cash=round(cash, 2), bars_held=bars_held,
            signal_key=pos.signal_key, exit_plan_name=pos.exit_plan.name,
            exit_plan_params=pos.exit_plan.params)
        self.store.save_trade(t)
        self.risk.on_trade_closed(net, pos.volume)

        self.position = None
        self._persist()

        self.ev.write("close", symbol=t.symbol, side=str(t.side), reason=reason,
                      entry=t.entry_price, exit=t.exit_price,
                      gross=t.gross_points, cost=t.cost_points,
                      net=t.net_points, cash=t.net_cash, bars_held=bars_held,
                      trade_id=t.trade_id, exit_policy=t.exit_plan_name)

    # ---------------- 统计 ----------------
    def summary(self) -> Dict[str, Any]:
        trades = self.store.trades()
        n = len(trades)
        wins = [t for t in trades if t["net_points"] > 0]
        losses = [t for t in trades if t["net_points"] <= 0]
        tot = sum(t["net_points"] for t in trades)
        cash = sum(t["net_cash"] for t in trades)
        by_reason: Dict[str, Any] = {}
        for t in trades:
            r = t["reason"]
            by_reason.setdefault(r, {"n": 0, "net": 0.0})
            by_reason[r]["n"] += 1
            by_reason[r]["net"] = round(by_reason[r]["net"] + t["net_points"], 4)
        return {
            "trades": n,
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / n, 4) if n else 0.0,
            "avg_win": round(sum(t["net_points"] for t in wins) / len(wins), 4) if wins else 0.0,
            "avg_loss": round(sum(t["net_points"] for t in losses) / len(losses), 4) if losses else 0.0,
            "net_points": round(tot, 4),
            "net_cash": round(cash, 2),
            "expectancy_points": round(tot / n, 4) if n else 0.0,
            "by_reason": by_reason,
            "open_position": self.position.to_dict() if self.position else None,
            "exit_policy": self.exit_policy.describe(),
            "entry_policy": self.entry_policy.describe(),
            "spec": {"signal_symbol": self.spec.signal_symbol,
                     "trade_symbol": self.spec.trade_symbol,
                     "price_tick": self.spec.price_tick,
                     "multiplier": self.spec.multiplier},
        }

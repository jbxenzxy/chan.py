# -*- coding: utf-8 -*-
"""
对账与卡单监控（ReconcileMixin）
================================
从 Engine.py 拆出的运维职能，以 Mixin 形式挂回 TradingEngine（方法仍通过 self 调用）：

    _reconcile_position / _reconcile_positions / _reconcile_side
        持仓对账（增强 B）：账本 vs 真实持仓逐边比对，发现漂移时落事件并修正。
    _check_unlock_stuck
        Phase F1：UNLOCK 卡单监控。on_bar 每根 K 线调用一次，
        超窗口期未确认成交则按 broker 回报重建/清理 _unlock_in_flight。

设计约束：本文件只依赖 Infra 数据结构与 self 注入的引擎上下文
（positions/broker/store/ev/cfg 等），不反向 import 引擎主体，维持单向依赖。
"""
from __future__ import annotations

from typing import List, Optional

from ..Infra.Types import Bar, EngineState, Order, OrderIntent, Position, Side, Trade, now_cn

class ReconcileMixin:
    # ---------------- 持仓对账（增强 B） ----------------
    def _reconcile_position(self) -> None:
        """【E3.3 兼容壳】单仓对账。E3.3 起实际逻辑在 _reconcile_positions。

        on_bar 已直接调 _reconcile_positions；保留本函数以防外部旧测试直接调用。
        """
        self._reconcile_positions()

    def _reconcile_positions(self, source: str = "on_bar") -> None:
        """【E3.3 + Phase F2】多仓对账——每侧（LONG/SHORT）独立与券商真实持仓比对。

        触发场景：
          · on_bar（默认）：用户在快期3等外部终端手工平仓 / 幽灵持仓 / 账户被改
          · restore（Phase F2 新增）：引擎启动 _restore 后立刻拉一次真实持仓，
            防止"本地 store 有持仓但真实账户已平"造成重启后第一根 bar 误判
          · unlock_stuck（Phase F1 新增）：UNLOCK 卡单 5 bars 后复核走这里

        对账策略（每侧独立）：
          · real_vol < 0 或 None → skip（broker 不支持对账，如 dry_run）
          · real_vol == 0 → 清空同侧全部仓位（每笔一事件 position_externally_closed）
          · real_vol < engine_vol → FIFO 部分平最早仓位（按 entry_bar_seq ASC），
            生成 trade.reason='reconcile_external_partial'，但不实际下单（broker 不动）
          · real_vol > engine_vol → 仅告警 position_mismatch（不自动接管未知持仓，
            避免误判用户手动加仓为引擎应跟踪仓位）
          · 全部清空后 → state IDLE；否则保持 EXITING

        入场当根 K 线跳过（仅 on_bar 路径生效，restore 路径不跳）：
          开仓后 tqsdk 持仓同步需要时间，避免误判刚开仓为「已平」。

        source 参数：写入事件 source 字段，便于审计区分触发源。
        """
        fn = getattr(self.broker, "real_position", None)
        if fn is None:
            return

        # 收集每侧仓位（FIFO 排序，便于部分平）
        longs = sorted(self.positions.same_side_positions(Side.LONG),
                       key=lambda p: p.entry_bar_seq)
        shorts = sorted(self.positions.same_side_positions(Side.SHORT),
                       key=lambda p: p.entry_bar_seq)

        all_cleared = True
        for side, side_positions in ((Side.LONG, longs), (Side.SHORT, shorts)):
            if not side_positions:
                continue
            engine_vol = sum(p.volume for p in side_positions)

            # 入场当根 K 线跳过（仅 on_bar 路径生效）：
            # 同侧最早仓位若 bars_held < 1 → 整侧 skip，避免误判刚开仓为「已平」
            # restore 路径：bars_seen 已被 _restore 末尾置 0（首次启动时），
            # 不应拦截首拉对账
            if source == "on_bar":
                min_bars_held = min(self.bars_seen - p.entry_bar_seq
                                    for p in side_positions)
                if min_bars_held < 1:
                    all_cleared = False
                    continue

            try:
                real_vol = fn(side)
            except Exception as e:
                # Phase F2：source="restore" 时 broker.real_position 异常 → 写告警事件
                # 让 _restore 的外层 try/except 也能感知（便于审计/告警）
                if source == "restore":
                    self.ev.write("restore_reconcile_failed",
                                  reason="{}: {}".format(type(e).__name__, e),
                                  side=str(side),
                                  note="broker.real_position 抛异常，按本地 store 启动")
                all_cleared = False
                continue
            if real_vol is None:
                continue

            side_all_cleared = self._reconcile_side(
                side, side_positions, engine_vol, real_vol, source)
            if not side_all_cleared:
                all_cleared = False

        # state：所有仓位都清完 → IDLE
        if not self.positions.is_empty():
            all_cleared = False
        if all_cleared:
            self._last_close_failed_bar_ts = 0
            # Step 1：cooldown 改按根数（序号差）判定，这里同步清序号
            self._last_close_failed_bar_seq = 0
            self._persist()
            self._state = EngineState.IDLE

    def _reconcile_side(self, side: Side, side_positions: List[Position],
                        engine_vol: int, real_vol: int, source: str) -> bool:
        """【Phase F2】单侧对账（与 _reconcile_positions 解耦）。

        返回 True 表示该侧已全部清空（real_vol == 0）。
        返回 False 表示：一致 / 部分平后仍有残留 / 告警不接管。
        调用方汇总两侧返回值决定是否 state→IDLE。
        """
        if real_vol > engine_vol:
            # 真实持仓 > 引擎：告警不接管（用户可能在外部手动加仓）
            self.ev.write("position_mismatch", side=str(side),
                          engine_vol=engine_vol, real_vol=real_vol,
                          n_engine_positions=len(side_positions),
                          reason="real_gt_engine", source=source)
            return False

        if real_vol == engine_vol:
            # 一致：无需操作
            return False

        # real_vol < engine_vol：部分平或全平（FIFO 顺序）
        n_to_close = engine_vol - real_vol  # 手数差
        close_list: List[Position] = []
        closed_vol = 0
        for pos in side_positions:
            if closed_vol >= n_to_close:
                break
            if pos.volume <= (n_to_close - closed_vol):
                # 整笔平
                close_list.append(pos)
                closed_vol += pos.volume
            else:
                # 部分平（仅取差额手数）：当前 E3.3 不支持仓位内部分平，
                # 保守策略：整笔平（生成 trade.reason='reconcile_external_partial_overflow'）
                # 后续 E3.4 可考虑把单 Position 拆分为多笔（如 entry split）
                # 这里直接整笔平，溢出的 closed_vol 写 warning
                close_list.append(pos)
                closed_vol += pos.volume
                self.ev.write("reconcile_partial_overflow",
                              side=str(side),
                              pos_signal_key=pos.signal_key,
                              pos_volume=pos.volume,
                              closed_so_far=closed_vol,
                              needed=n_to_close,
                              source=source,
                              note="E3.3 不支持仓位内拆分，整笔平代替")

        # 生成 trade + 从 book remove（不实际下单）
        for idx, pos in enumerate(close_list):
            # 用最新 bar.close 作为参考 exit_price（无真实成交，仅供 trade 记账）
            ref_price = (self.last_bar.close if self.last_bar else pos.entry_price)
            gross = pos.pnl_points(ref_price)
            # 2026-09-10：成本口径与 Engine 的 hard-exit 路径对齐（规则 ⑸）。
            #   原写法直接传全局开关 self.spec.close_today_first（默认 True）→ 恒按
            #   "平今"费率（0.0345%）计，对**跨日单**高估 15 倍；而 Engine.py:758 那边
            #   是按 entry_date 动态判定 —— 两处成本口径不一致。现改为与 Engine 同源。
            _today = (self.last_bar.date[:10]
                      if (self.last_bar is not None and self.last_bar.date)
                      else now_cn()[:10])
            _is_today_pos = bool(pos.entry_date) and pos.entry_date[:10] >= _today
            cost = self.spec.cost_points(
                pos.entry_price, ref_price,
                close_today=bool(_is_today_pos and self.spec.close_today_first))
            net = gross - cost
            cash = self.spec.points_to_cash(net, pos.volume)
            bars_held = max(0, self.bars_seen - pos.entry_bar_seq)

            self._trade_seq += 1
            t = Trade(
                trade_id="T{:05d}".format(self._trade_seq), symbol=pos.symbol,
                side=pos.side, volume=pos.volume, entry_price=pos.entry_price,
                exit_price=ref_price, entry_at=pos.entry_at, exit_at=now_cn(),
                reason="reconcile_external_partial", gross_points=round(gross, 4),
                cost_points=round(cost, 4), net_points=round(net, 4),
                net_cash=round(cash, 2), bars_held=bars_held,
                signal_key=pos.signal_key, exit_plan_name=pos.exit_plan.name,
                exit_plan_params=pos.exit_plan.params)
            self.store.save_trade(t)
            # （2026-09-08：原 RiskGate.on_trade_closed 当日统计已随五道硬闸门删除。）

            self.positions.remove(pos)
            self.ev.write("position_externally_closed",
                          reason="reconcile_external_partial",
                          side=str(pos.side), symbol=pos.symbol,
                          signal_key=pos.signal_key,
                          exit_price=ref_price,
                          gross_points=t.gross_points,
                          net_points=t.net_points,
                          fifo_index=idx, pos_count=len(close_list),
                          source=source)

        # 全部清空（real_vol == 0）：写一笔总结事件
        if real_vol == 0:
            self.ev.write("position_externally_closed_summary",
                          reason="reconcile_real_zero",
                          side=str(side),
                          engine_vol=engine_vol,
                          n_positions=len(side_positions),
                          source=source)
        elif n_to_close > 0:
            self.ev.write("position_externally_closed_summary",
                          reason="reconcile_partial",
                          side=str(side),
                          engine_vol=engine_vol, real_vol=real_vol,
                          closed_vol=closed_vol,
                          n_positions_closed=len(close_list),
                          source=source)

        # 全部清空判定：real_vol==0 ⇒ 该侧 0 持仓 ⇒ True（让 state 走 IDLE）
        return real_vol == 0

    # ════════════════════════════════════════════════════════════════
    # Phase F1（2026-09-05）：UNLOCK 卡单监控
    #   on_bar 入口每根 bar 调一次 _check_unlock_stuck(bar)
    #   · _unlock_in_flight 为空 → skip（无卡单监控中）
    #   · bars_elapsed < _unlock_stuck_bars → skip（窗口期内不打扰）
    #   · 已达窗口 → 调 broker.trade_confirmed(UNLOCK, sig.key)：
    #       True  → 真成交（CTP 已收到回报）→ 清 in-flight
    #       False → 查 broker.real_position(target.side)：
    #           · > 0  → UNLOCK 卡单确认 → 把 target 重建回 portfolio（真实账户仍在）
    #             → state EXITING，让下一信号走 UNLOCK 重试
    #           · == 0 → UNLOCK 卡单恢复（CTP 已平但 engine 端已删 target）→ 清 in-flight
    #           · None → broker 不支持对账 → 默认按"恢复"清 in-flight
    #
    #   设计要点：
    #     · _unlock_position 报单前快照 target → _unlock_in_flight["target_snapshot"]
    #       卡单时用快照重建 Position（真实账户还在，引擎必须重新跟踪）
    #     · 报单成功仍走 P13 旧路径（立即 remove + save_trade）—— 保持现有测试零变化
    #     · 快照重建时**生成一条修正 trade**（reason='unlock_stuck_restored'），
    #       避免后续 reconcile_external_partial 误把 target 视为外部平仓再平一次
    #     · dry_run broker.trade_confirmed=True → 不触发 reconcile，行为零变化
    # ════════════════════════════════════════════════════════════════
    def _check_unlock_stuck(self, bar: Bar) -> None:
        if self._unlock_in_flight is None:
            return
        rec = self._unlock_in_flight
        bars_elapsed = self.bars_seen - rec["submit_bar_seq"]
        if bars_elapsed < self._unlock_stuck_bars:
            return  # 窗口期内：先信 submit 返回，不打扰

        # 窗口期已过：调 broker.trade_confirmed 复核
        fn_tc = getattr(self.broker, "trade_confirmed", None)
        confirmed = True
        if callable(fn_tc):
            try:
                confirmed = bool(fn_tc(OrderIntent.UNLOCK, rec["signal_key"]))
            except Exception:
                # broker 查询异常 → 保守按未确认走 reconcile
                confirmed = False

        if confirmed:
            self.ev.write("unlock_confirmed",
                          signal_key=rec["signal_key"],
                          target_signal_key=rec.get("target_signal_key", ""),
                          bars_elapsed=bars_elapsed)
            self._unlock_in_flight = None
            return

        # 未确认：Phase G2 —— 先撤掉该 signal_key 的在途 UNLOCK 委托。
        # 若不撤，重建 portfolio 后挂单仍可能成交 → 双重平仓。
        # base/dry_run 的 cancel_pending 返回 0（无在途单），零行为影响。
        fn_cp = getattr(self.broker, "cancel_pending", None)
        if callable(fn_cp):
            try:
                n_cancelled = int(fn_cp(rec["signal_key"]))
                if n_cancelled > 0:
                    self.ev.write("unlock_pending_cancelled",
                                  signal_key=rec["signal_key"],
                                  target_signal_key=rec.get("target_signal_key", ""),
                                  cancelled=n_cancelled,
                                  bars_elapsed=bars_elapsed)
            except Exception:
                pass  # 撤单异常不阻断后续 real_position 对账

        # 查 broker.real_position(target.side) 判定卡单 vs 恢复
        target_side_str = rec.get("target_side", "")
        fn_rp = getattr(self.broker, "real_position", None)
        real_vol: Optional[int] = None
        if callable(fn_rp):
            try:
                target_side = (Side.LONG if target_side_str == "LONG"
                               else Side.SHORT if target_side_str == "SHORT"
                               else None)
                if target_side is not None:
                    real_vol = fn_rp(target_side)
            except Exception:
                real_vol = None

        if real_vol is not None and real_vol > 0:
            # 卡单确认：真实账户仍有反向持仓 → 把 target 重建回 portfolio
            snap = rec.get("target_snapshot")
            if snap is not None:
                restored_pos = Position.from_dict(snap)
                # 若 portfolio 已空（已被 P13 remove），直接 add 回去
                # 若 portfolio 非空（极少：UNLOCK 后又开新仓）→ 防御性 add_max 检查
                try:
                    self.positions.add(restored_pos)
                except PositionBookError:
                    # portfolio 已满（cfg.max_open_positions 缩到当前数以下）→ 告警
                    self.ev.write("unlock_stuck_restore_failed",
                                  signal_key=rec["signal_key"],
                                  reason="portfolio_full_cannot_restore_target")
                    self._unlock_in_flight = None
                    return
                self.ev.write("unlock_stuck_confirmed",
                              signal_key=rec["signal_key"],
                              target_signal_key=rec.get("target_signal_key", ""),
                              reason="real_position_still_held_after_stuck_window",
                              target_side=target_side_str,
                              real_vol=real_vol,
                              bars_elapsed=bars_elapsed)
                # 重建后保持 EXITING，让下一信号走 UNLOCK 重试
                # （_reconcile_positions 的 IDLE 转移会被 portfolio 非空挡住）
            else:
                # 没有快照（理论上 _unlock_position 必须存了）→ 告警
                self.ev.write("unlock_stuck_confirmed",
                              signal_key=rec["signal_key"],
                              target_signal_key=rec.get("target_signal_key", ""),
                              reason="real_position_still_held_no_snapshot",
                              target_side=target_side_str,
                              real_vol=real_vol,
                              bars_elapsed=bars_elapsed)
            self._unlock_in_flight = None
            return

        # 卡单恢复（real_vol == 0 / None）：portfolio 已空（已被 P13 remove），
        # 清 in-flight，写恢复事件
        self.ev.write("unlock_stuck_recovered",
                      signal_key=rec["signal_key"],
                      target_signal_key=rec.get("target_signal_key", ""),
                      reason=("real_position_zero_after_stuck_window"
                              if real_vol is not None
                              else "real_position_unknown_保守按恢复处理"),
                      target_side=target_side_str,
                      real_vol=real_vol,
                      bars_elapsed=bars_elapsed)
        self._unlock_in_flight = None

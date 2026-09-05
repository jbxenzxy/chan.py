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

from typing import Any, Dict, Optional, Tuple

from .brokers.base import Broker
from .config import GatewayConfig
from .events import EventLog
from .position_book import PositionBook, PositionBookError
from .risk import RiskGate
from .sizing import PositionSizer
from .store import Store
from .strategy.base import EntryPolicy, ExitCheck, ExitPolicy
from .symbols import InstrumentSpec
from .types import (
    Bar, DecisionType, EntryMode, EngineState, ExitMode, ExitPlan, OrderIntent, Position,
    Side, Signal, Trade, now_cn,
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
        # 仓位管理（手数定档）。默认 enabled=False —— 直接返回固定手数，
        # 与加这个模块之前的行为完全一致，不查账户、不联网。
        self.sizer = PositionSizer(cfg.sizing, self.spec, cfg.risk.max_volume)

        # Phase E1（2026-09-05）：引入 PositionBook 容器，为 E2 (UNLOCK_FIRST) / E3 (N≥1)
        # 多仓场景预留扩展点。E1 阶段 max=1，语义与单一 self.position 完全等价。
        # 旧代码继续走 self.position（property 兼容层转发到 book.legacy_single()）。
        # Phase E3.1（2026-09-05）：max 改为 cfg.risk.max_open_positions 配置化，默认仍=1
        # —— 所有现存测试（P5..P13）零行为变化。
        self.positions: PositionBook = PositionBook(
            max_positions=cfg.risk.max_open_positions)
        # Phase A：4 态引擎状态机
        #   IDLE     无持仓，等待入场信号
        #   OPENING  正在开仓（瞬态：下单到成交之间）
        #   IN_TRADE 已持仓，等待离场条件
        #   EXITING  正在离场（瞬态：下单到成交之间）
        # 状态转移由 _open_position / _close_position / _reconcile_position 主导。
        # Phase B 信号门按此状态决定是否接收新信号、是否触发离场。
        self._state: EngineState = EngineState.IDLE
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

    # ---------------- 兼容层：engine.position property ----------------
    # Phase E1：旧版代码（含 P5..P11 测试）读写 engine.position 都是按"单 Position 或 None"
    # 设计的。通过这两个 property，把读写都转发到 self.positions 这个容器，
    # 让现有调用点不需要改任何一行 —— E2/E3 接入多仓语义时，再逐步把
    # `engine.position.x` 替换成 `engine.positions.legacy_single()?.x` 或 .positions[*].x。
    @property
    def position(self) -> Optional[Position]:
        return self.positions.legacy_single()

    @position.setter
    def position(self, value: Optional[Position]) -> None:
        self.positions.set_legacy(value)

    # ---------------- 状态恢复 ----------------
    def _restore(self) -> None:
        # Phase E1：优先读新版 "positions" list（多仓），回退到老版 "position" 单字段。
        # 老数据库无 "positions" 键时也能恢复，且不破坏现有迁移路径。
        # Phase E3.1：用 cfg.risk.max_open_positions 作为恢复时的容量上限（保持 E1 兼容，
        # 默认=1 行为完全不变）。
        # 设计：绕过 setter（setter 只接 Position 而非整簿），用 replace_with 内部交换；
        # replace_with 在 persisted 数据超出 cfg max 时做"取前 max + 截断丢弃"语义
        # —— cfg 上限变化或历史数据来自更宽容版本时仍能启动，仅丢多余的仓并打 warning。
        restore_max = self.cfg.risk.max_open_positions
        pd_list = self.store.get_json("positions")
        if isinstance(pd_list, list):
            new_book = PositionBook.from_dict(pd_list, max_positions=restore_max)
            self.positions.replace_with(new_book)
        else:
            pd = self.store.get_json("position")
            if isinstance(pd, dict) and pd.get("symbol"):
                new_book = PositionBook(max_positions=restore_max)
                new_book.set_legacy(Position.from_dict(pd))
                self.positions.replace_with(new_book)

        # E3.1：截断 warning —— persisted 多仓数据超出 cfg max 时丢了一些仓。
        truncated = self.positions.truncated_on_restore
        if truncated:
            self.ev.write(
                "positions_truncated_on_restore",
                reason="cfg_max_smaller_than_persisted",
                cfg_max=restore_max,
                persisted_n=(len(truncated) + restore_max),
                kept_n=restore_max,
                dropped_keys=[p.signal_key for p in truncated],
            )
        # E1：写"start"事件只记簿大小/首个位置摘要，不调 legacy_single（多仓会抛）。
        # 这是观察日志，不是引擎逻辑，规避守护报错即可。
        if not self.positions.is_empty():
            p0 = self.positions.positions[0]
            self.ev.write("start", restored_position=p0.to_dict(),
                          positions_n=len(self.positions))
        # Phase A：根据持仓推断初始 state。持仓在 → IN_TRADE；无持仓 → IDLE。
        self._state = EngineState.IN_TRADE if not self.positions.is_empty() else EngineState.IDLE
        self.risk.restore(self.store.get_json("day_stats"))
        self.bars_seen = int(self.store.get_json("bars_seen", 0) or 0)

    def _persist(self) -> None:
        # Phase E1：双写兼容 —— 新键 "positions"（list）保留扩展空间，
        # 旧键 "position"（单字段）继续写以做审计 / 旧流程回归。
        # 多仓时旧键取 positions[0] —— 是为了保留"看一眼持仓是哪个合约"的旧 API，
        # 不是引擎主入口（主入口走 self.positions）。legacy_single() 在多仓会抛错
        # 是有意的早期守护 E3，这里规避它。
        if not self.positions.is_empty():
            self.store.set_json("positions", self.positions.to_dict())
            p0 = self.positions.positions[0]
            self.store.set_json("position", p0.to_dict())
        else:
            self.store.delete_key("positions")
            self.store.delete_key("position")
        self.store.set_json("day_stats", self.risk.snapshot())
        self.store.set_json("bars_seen", self.bars_seen)

    # ---------------- bar 事件 ----------------
    def on_bar(self, bar: Bar) -> None:
        if self.last_bar is not None and bar.timestamp <= self.last_bar.timestamp:
            return                      # 重复或回退的 K 线，丢弃
        self.bars_seen += 1
        self.last_bar = bar

        # 每根 K 线（无论是否持仓）都喂给出场策略，供其维护 ATR 等历史缓冲。
        # LayeredExitPolicy 等需要历史的策略借此在开仓瞬间就有足够样本。
        # 默认 no-op，不影响其它策略。
        try:
            self.exit_policy.on_bar(bar, self.spec)
        except Exception:
            pass

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

        # 持仓对账（增强 B）：与券商真实持仓比对。若发现持仓已被外部平掉
        # （如用户在快期3手工平仓）或属幽灵持仓，立即修正引擎账目，
        # 避免继续傻等平仓 / 误判新信号。dry_run 等无真实账户的通道返回 None，跳过。
        if self.position is not None:
            self._reconcile_position()

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

    # ---------------- 持仓对账（增强 B） ----------------
    def _reconcile_position(self) -> None:
        """与券商真实持仓对账，修正引擎账目。

        触发场景：用户在快期3等外部终端手工平仓 / 幽灵持仓 / 账户被别的程序改动。
        若真实持仓已为 0（而引擎仍记着持仓），立即清掉引擎账目并落盘，避免：
          · 反复尝试平仓刷「等待持仓更新超时」；
          · 同向新买点被当成「已持仓」而漏单 / 误判。
        若真实手数与引擎不一致（且非 0），仅告警，不自动接管未知持仓。
        """
        pos = self.position
        if pos is None:
            return
        # 入场当根 K 线跳过：开仓后 tqsdk 持仓同步需要时间，避免误判刚开仓为「已平」
        if (self.bars_seen - pos.entry_bar_seq) < 1:
            return
        fn = getattr(self.broker, "real_position", None)
        if fn is None:
            return
        try:
            real_vol = fn(pos.side)
        except Exception:
            return
        if real_vol is None:
            return
        if real_vol <= 0:
            self.ev.write("position_externally_closed",
                          reason="reconcile_real_zero",
                          side=str(pos.side), symbol=pos.symbol,
                          signal_key=pos.signal_key)
            self.position = None
            self._last_close_failed_bar_ts = 0
            self._close_fail_streak = 0
            self._persist()
            # Phase A：外部已平，回 IDLE
            self._state = EngineState.IDLE
        elif real_vol != pos.volume:
            self.ev.write("position_mismatch", engine_vol=pos.volume,
                          real_vol=real_vol, side=str(pos.side))

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

        # ════════════════════════════════════════════════════════════════
        # Phase B：信号门按引擎状态重组
        #   OPENING/EXITING → 正在下单中（瞬态），本次信号忽略但保留幂等键
        #   IN_TRADE        → 持仓中：
        #                       反向信号 → 仅触发离场（不再反向开新仓）
        #                       同向信号 → 忽略
        #   IDLE            → 正常开仓流程
        # 设计动机：移除"反向信号立刻反手开仓"的能力——一次入场 N 单
        # （N=1 时等同当前）全部离场后才允许下次交易，避免在持仓中并发开仓
        # 导致 N 单循环放大。
        # ════════════════════════════════════════════════════════════════
        if self._state in (EngineState.OPENING, EngineState.EXITING):
            self.store.update_signal_action(
                sig.key, "in_flight", "state={}".format(self._state.value))
            self.ev.write("signal_in_flight", key=sig.key,
                          state=self._state.value,
                          reason="engine_busy_opening_or_exiting")
            return

        if self._state == EngineState.IN_TRADE and self.position is not None:
            pos_side_long = (self.position.side.value > 0)
            is_opposite = (sig.is_buy != pos_side_long)
            if is_opposite:
                # 反向信号：只触发离场（按 entry_mode 决定的 exit_mode 由 Phase D 接入），
                # 离场后回到 IDLE，下次信号才会被正常开仓处理。
                # 先快照 position.signal_key，再调 _close_position——后者会把
                # self.position 置 None，导致事件里读不到原仓 key。
                pos_sig_key = self.position.signal_key
                price = self.last_bar.close if self.last_bar else sig.price
                self._close_position("signal_reverse", price, self.last_bar,
                                     signal_key=sig.key)
                self.store.update_signal_action(
                    sig.key, "close_only", "reverse_signal_in_trade")
                self.ev.write("signal_exit_only", key=sig.key,
                              reason="reverse_signal_in_trade",
                              position_signal_key=pos_sig_key)
                return
            else:
                self.store.update_signal_action(
                    sig.key, "skip", "in_trade_same_direction")
                self.ev.write("signal_skip", key=sig.key,
                              reason="in_trade_same_direction")
                return

        # ── state == IDLE：进入正常开仓决策 ──

        # ════════════════════════════════════════════════════════════════
        # Phase E2：UNLOCK_FIRST 入场路径
        #   触发条件 —— IDLE 时 portfolio 非空 + has_opposite(sig.side)：
        #     · IDLE 表示当前没有今仓
        #     · portfolio 非空意味着昨日 LOCK 后留下的反向对冲仓（只可能是 1 笔，max=1 限制）
        #     · 信号方向与 portfolio 那笔相反 → 这是一次"解锁昨仓"信号，不是开新仓
        #   行为 —— broker.submit(UNLOCK, target.side, vol, sig.price, ...)：
        #     · 报 CloseYesterdayOffset（注意：side 是 target.side = portfolio 那笔的方向，
        #       不是 sig.side —— 因为平的是昨仓，方向就是昨仓的方向）
        #     · 成功后 portfolio 清这笔记 Trade（reason=unlock_against_signal），
        #       不开新今仓（"解锁"动作严格只清昨；今仓由后续信号决定）
        #     · 失败（拒单/超时）→ signal_rejected，portfolio 不动
        #   状态机 —— UNLOCK 走 EXITING（语义是清空旧持仓，与"平仓"对位）
        #   不影响 IN_TRADE 分支：那个分支处理的是"今仓反手"，与"昨仓解锁"语义正交
        # ════════════════════════════════════════════════════════════════
        if (self._state == EngineState.IDLE
                and not self.positions.is_empty()
                and self.positions.has_opposite(sig.side)):
            # signal_action 由 _unlock_position 内部根据成交/拒单写
            # （这里不写"unlock"，否则 UNLOCK 拒单时它的"rejected"覆盖会被 on_signal
            # 的"unlock"反过来覆盖，丢失失败原因）
            self._unlock_position(sig, sig.side)
            self.ev.write("signal_unlock", key=sig.key, reason="reverse_yesterday_position")
            return

        decision = self.entry_policy.decide(sig, self.position, self.spec)
        if not decision:
            self.store.update_signal_action(sig.key, "skip", decision.reason)
            self.ev.write("signal_skip", key=sig.key, reason=decision.reason)
            return

        bar_date = self.last_bar.date if self.last_bar else sig.date

        volume, why_vol = self._size_position(sig)
        if volume <= 0:
            self.store.update_signal_action(sig.key, "risk_block", why_vol)
            self.ev.write("risk_block", key=sig.key, side=str(decision.side or sig.side),
                          reason=why_vol, bar_date=bar_date)
            return

        # 手数上限以 sizer 的有效上限为准（sizing 关闭时它 == risk.max_volume，行为不变）。
        # 否则会出现「sizing 算 4 手、风控按 risk.max_volume=1 拦」的死角。
        ok, why = self.risk.check_open(decision.side or sig.side, volume, bar_date,
                                       max_volume=self.sizer.max_volume)
        if not ok:
            self.store.update_signal_action(sig.key, "risk_block", why)
            self.ev.write("risk_block", key=sig.key, side=str(decision.side or sig.side),
                          reason=why, bar_date=bar_date)
            return

        self._open_position(sig, decision.side or sig.side, volume)

    # ---------------- 手数定档 ----------------
    def _size_position(self, sig: "Signal") -> "Tuple[int, str]":
        """问仓位管理"这笔开几手"。默认关闭仓位管理时就是固定手数。

        喂给 PositionSizer 的三个输入都做了降级：
          · 权益   —— broker.equity()；离线/未登录返回 None → sizer 回退 fallback_volume
          · 止损距 —— 信号 K 线极值距离（与 LayeredExitPolicy 的 R 同源）
          · ATR    —— exit_policy.current_atr()；策略不支持或样本不足 → None
        返回 (手数, 原因)；手数 ≤ 0 表示不开仓。
        """
        equity = None
        fn = getattr(self.broker, "equity", None)
        if callable(fn):
            try:
                equity = fn(self.sizer.equity_source)
            except Exception:
                equity = None

        price = float(sig.price or 0.0)
        if price <= 0 and self.last_bar is not None:
            price = float(self.last_bar.close or 0.0)

        # 止损距离：多单看信号 K 线最低价，空单看最高价（与 LayeredExitPolicy 的 R 同源）
        stop_dist = None
        try:
            stop_dist = abs(float(sig.price) - float(sig.low)) if sig.is_buy \
                else abs(float(sig.high) - float(sig.price))
        except (TypeError, ValueError):
            stop_dist = None

        atr = None
        atr_fn = getattr(self.exit_policy, "current_atr", None)
        if callable(atr_fn):
            try:
                atr = atr_fn()
            except Exception:
                atr = None

        return self.sizer.size(equity=equity, price=price,
                               stop_distance_points=stop_dist, atr_points=atr)

    # ---------------- 开 / 平 ----------------
    def _open_position(self, sig: Signal, side, volume: int) -> None:
        # Phase A：进入瞬态 OPENING（v1 同步 broker：下单完成时已能立刻知道成交与否，
        # 但状态机严格遵守 IDLE→OPENING→IN_TRADE/IDLE 的语义，便于将来接异步 CTP）。
        self._state = EngineState.OPENING

        o = self.broker.submit(OrderIntent.OPEN, side, volume, sig.price, sig.key,
                               note="缠论{}点信号开仓".format("买" if sig.is_buy else "卖"))
        self.store.save_order(o)
        self.ev.write("order", order_id=o.order_id, action=o.action,
                      intent=o.meta.get("intent", o.action),   # Phase C：把 intent 写入事件流
                      side=str(o.side), volume=o.volume, price=o.price,
                      req_price=o.req_price, status=o.status, broker=o.broker)

        # 真实下单可能被拒/超时：不开仓，落盘后结束，不产生幽灵持仓
        if o.status != "filled" or o.filled_price is None:
            why = o.meta.get("reject_reason") or o.status
            self.store.update_signal_action(sig.key, "rejected", why)
            self.ev.write("order_rejected", key=sig.key, order_id=o.order_id,
                          reason=why)
            # 开仓被拒：回 IDLE 等下一次信号
            self._state = EngineState.IDLE
            return

        entry_price = o.filled_price
        plan: ExitPlan = self.exit_policy.plan(sig, entry_price, self.spec)

        # Phase A：Position.entry_mode 显式标记 OPEN_FIRST（默认但写明便于将来 UNLOCK_FIRST 接入）
        self.position = Position(
            symbol=self.spec.trade_symbol, side=side, volume=o.volume,
            entry_price=entry_price, entry_at=now_cn(),
            entry_bar_ts=self.last_bar.timestamp if self.last_bar else 0,
            entry_bar_seq=self.bars_seen,
            signal_key=sig.key, open_order_id=o.order_id, exit_plan=plan,
            entry_mode=EntryMode.OPEN_FIRST)
        self._persist()

        self.store.update_signal_action(sig.key, "opened", o.order_id)
        self.ev.write("open", symbol=self.position.symbol, side=str(side),
                      volume=o.volume, entry_price=entry_price,
                      stop=plan.stop_price, tp=plan.tp_price,
                      exit_policy=plan.name, exit_params=plan.params,
                      signal_key=sig.key,
                      entry_mode=self.position.entry_mode.value)
        # 开仓成交：进入 IN_TRADE 等待离场
        self._state = EngineState.IN_TRADE

    def _close_position(self, reason: str, trigger_price: float,
                        bar: Optional[Bar], signal_key: str = "") -> None:
        pos = self.position
        if pos is None:
            return

        # Phase A：进入瞬态 EXITING。
        # Phase D（2026-09-05）：按 pos.entry_mode 联动决定离场方式（硬规则，不留开关）。
        #   OPEN_FIRST   → LOCK_SOFT  → OrderIntent.LOCK  + 反向 side（开反向同手数）
        #   UNLOCK_FIRST → CLOSE_HARD → OrderIntent.CLOSE + pos.side（平昨无费率问题）
        # 没有 entry_mode 字段的旧持仓（默认 OPEN_FIRST）走 LOCK_SOFT，
        # 与"今天开仓 → 离场用锁仓"的硬规则一致。
        intent, side = self._exit_intent(pos)
        self._state = EngineState.EXITING

        # P3 配套：连续 close 失败就停手，避免反向信号/SL/TP 在每根 bar 上死循环重试。
        # 触发条件：上一笔 close 被拒（broker 没拿到成交），且 retry cooldown 未过期。
        now_ts = self.last_bar.timestamp if self.last_bar else 0
        if (self._last_close_failed_bar_ts
                and now_ts - self._last_close_failed_bar_ts <= self._close_retry_bars):
            # cooldown 中：保持 EXITING 状态，下一根 bar 再试
            return

        # Phase C+D：传 OrderIntent（CTP OpenCloseType 由 broker 端按表映射）
        o = self.broker.submit(intent, side, pos.volume, trigger_price,
                               signal_key or pos.signal_key, note=reason)
        self.store.save_order(o)
        self.ev.write("order", order_id=o.order_id, action=o.action,
                      intent=o.meta.get("intent", intent.value),   # Phase C：把 intent 写入事件流
                      side=str(o.side), volume=o.volume, price=o.price,
                      req_price=o.req_price, status=o.status, broker=o.broker,
                      reason=reason, exit_mode=intent.value)

        # 平仓被拒/超时：保留持仓，等下一根 K 线再试，不凭空平掉
        if o.status != "filled" or o.filled_price is None:
            why = o.meta.get("reject_reason") or o.status
            self.ev.write("order_rejected", key=pos.signal_key, order_id=o.order_id,
                          action=intent.value, reason=reason, reject=why)
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
                # phantom 清掉：回 IDLE
                self._state = EngineState.IDLE
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
                      trade_id=t.trade_id, exit_policy=t.exit_plan_name,
                      entry_mode=pos.entry_mode.value,
                      exit_mode=intent.value)            # Phase D：离场方式（open→lock, unlock→close）
        # 平仓成交：回 IDLE 等待下一信号
        self._state = EngineState.IDLE

    # ---------------- 解锁入场（Phase E2 UNLOCK_FIRST 路径） ----------------
    def _unlock_position(self, sig: Signal, side: Side) -> None:
        """Phase E2：触发 UNLOCK_FIRST 入场。

        语义边界
          · "解锁" = CloseYesterdayOffset —— 把 portfolio 里那笔反向昨仓平掉
          · 不开新今仓。今仓由"今晚"或"明早"下一个信号决定
          · 与 LOCK（开反向同手数）形成对偶：LOCK 是今仓反开，UNLOCK 是昨仓关清

        报单要点
          · side 用 portfolio 里那笔的方向（target.side），不是 sig.side
            —— CloseYesterdayOffset 的语义就是"按方向平昨仓"
          · 成交价采用 sig.price（信号 K 线收盘价的对称使用，与 v1 LOCK 一致）

        状态机
          · 进入 EXITING（与平仓同位："清空旧持仓"）
          · broker 拒单 / 超时 → signal_rejected，回 IDLE
          · 成交 → portfolio 删这笔记 Trade，state 回 IDLE

        Trade 记帐
          · reason="unlock_against_signal" —— 不同于 tp/sl/eod/manual 的离场口径
          · cost 用 close_today=False（CloseYesterday 费率，zce/gfex 等无平今惩罚的交易所口径）
        """
        opp_list = self.positions.opposite_positions(side)
        if not opp_list:
            # 理论不可能走到这里（on_signal 已 has_opposite 判定）。防御性记录。
            self.ev.write("unlock_skipped", key=sig.key,
                          reason="no_opposite_in_portfolio")
            return
        # E2 阶段 max=1，opposite_positions 必然 ≤ 1。取首。
        target = opp_list[0]

        # 进入 EXITING（清空旧持仓，对位 _close_position 的状态语义）
        self._state = EngineState.EXITING

        o = self.broker.submit(OrderIntent.UNLOCK, target.side, target.volume,
                               sig.price, sig.key,
                               note="信号解锁昨仓（UNLOCK_FIRST）")
        self.store.save_order(o)
        self.ev.write("order", order_id=o.order_id, action=o.action,
                      intent=o.meta.get("intent", o.action),
                      side=str(o.side), volume=o.volume, price=o.price,
                      req_price=o.req_price, status=o.status, broker=o.broker,
                      reason="unlock_yesterday")

        if o.status != "filled" or o.filled_price is None:
            why = o.meta.get("reject_reason") or o.status
            self.store.update_signal_action(sig.key, "rejected", why)
            self.ev.write("order_rejected", key=sig.key, order_id=o.order_id,
                          action="unlock", reason=why)
            self._state = EngineState.IDLE
            return

        # 成交：算 Trade + 从 portfolio 删除
        gross = target.pnl_points(o.filled_price)
        cost = self.spec.cost_points(target.entry_price, o.filled_price,
                                     close_today=False)  # CloseYesterday 费率
        net = gross - cost
        cash = self.spec.points_to_cash(net, target.volume)
        bars_held = max(0, self.bars_seen - target.entry_bar_seq)

        self._trade_seq += 1
        t = Trade(
            trade_id="T{:05d}".format(self._trade_seq),
            symbol=target.symbol, side=target.side, volume=target.volume,
            entry_price=target.entry_price, exit_price=o.filled_price,
            entry_at=target.entry_at, exit_at=now_cn(),
            reason="unlock_against_signal",
            gross_points=round(gross, 4),
            cost_points=round(cost, 4),
            net_points=round(net, 4),
            net_cash=round(cash, 2),
            bars_held=bars_held,
            signal_key=target.signal_key,
            exit_plan_name=target.exit_plan.name,
            exit_plan_params=target.exit_plan.params,
        )
        self.store.save_trade(t)
        self.risk.on_trade_closed(net, target.volume)

        self.positions.remove(target)
        # E2 阶段 portfolio 退化为单仓，删后必然空；E3 多仓后这里要不要保留 state
        # 由 settle/close loop 决定 —— 当前防御性更稳是 IDLE
        self._state = EngineState.IDLE
        self._persist()

        # signal_action 由 _unlock_position 自己统一管理：
        #   成功 → "unlock"   失败（前面）→ "rejected"
        # 这样 on_signal 入口再写"unlock"也不会矛盾
        self.store.update_signal_action(
            sig.key, "unlock",
            "successfully_unlocked_yesterday_position")

        self.ev.write("unlock", symbol=t.symbol, side=str(t.side),
                      entry=t.entry_price, unlock=o.filled_price,
                      gross=t.gross_points, cost=t.cost_points,
                      net=t.net_points, cash=t.net_cash, bars_held=bars_held,
                      trade_id=t.trade_id, unlock_signal_key=sig.key,
                      unlock_order_id=o.order_id,
                      entry_mode=target.entry_mode.value)

    # ---------------- 离场方式（Phase D 硬规则） ----------------
    @staticmethod
    def _exit_intent(pos: Position) -> "Tuple[OrderIntent, Side]":
        """按 pos.entry_mode 联动决定离场方式（不留配置开关）。

        OPEN_FIRST   → LOCK_SOFT  → OrderIntent.LOCK  + 反向 side
                                          （开反向同手数；CTP OpenCloseType=Open）
        UNLOCK_FIRST → CLOSE_HARD → OrderIntent.CLOSE + pos.side
                                          （平昨无费率问题；CTP OpenCloseType=CloseToday / CloseAny）

        LOCK 在引擎视角是"软离场"——调用 broker.submit(OPEN, opposite, ...) 让 broker 真的
        去开反向仓；引擎把当前 Position 视为已了结（pos=None），Trade 仍按 trigger_price
        结算（cost 用 close_today_first 路径 = LOCK 替代平今的成本等价）。broker 端真实多出来的
        反向仓由第二天通过 UNLOCK 平掉（在 v2 PositionBook 接入前由用户在快期3手工处理）。
        """
        if pos.entry_mode is EntryMode.UNLOCK_FIRST:
            return OrderIntent.CLOSE, pos.side
        # OPEN_FIRST 或 默认 → LOCK_SOFT
        opposite = Side.SHORT if pos.side is Side.LONG else Side.LONG
        return OrderIntent.LOCK, opposite

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

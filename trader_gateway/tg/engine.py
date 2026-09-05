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
        # ════════════════════════════════════════════════════════════════
        # Phase F1（2026-09-05）：UNLOCK 卡单检测
        #   问题：UNLOCK 报单后 broker 返回 filled，但 CTP 通道异常时真实未成交；
        #         引擎若直接信 filled 删 portfolio，次日同向信号进来时
        #         has_opposite=False 走正常开仓路径 → 真实账户持仓仍在 → 错配。
        #   方案：报单成功后记 _unlock_in_flight，5 bars 后调
        #         broker.trade_confirmed(UNLOCK, sig.key) 二次确认：
        #           · True  → 真成交，清 in-flight
        #           · False → 调 _reconcile_positions 兜底（按真实持仓修正）
        #   dry_run broker.trade_confirmed 默认 True → 不触发 reconcile，行为零变化
        # ════════════════════════════════════════════════════════════════
        self._unlock_in_flight: Optional[Dict[str, Any]] = None
        # dict = {"signal_key": str, "target_signal_key": str,
        #         "submit_bar_ts": int, "submit_bar_seq": int}
        self._unlock_stuck_bars: int = 5     # 报单后多少 bar 触发复核（与 _close_retry_bars 对齐）
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
        # Phase F：恢复 _unlock_in_flight —— 接续上次崩前的卡单标记，
        #   让 _check_unlock_stuck 在余下 bar 进度下继续推进到 5-bar 复核。
        #   不恢复的副作用：引擎崩溃一次即丢卡单检测能力，F1 形同虚设。
        fl = self.store.get_json("_unlock_in_flight")
        if isinstance(fl, dict):
            self._unlock_in_flight = fl

        # ════════════════════════════════════════════════════════════════
        # Phase F2（2026-09-05）：_restore 末尾首拉真实持仓
        #   场景：上轮 SSE 实时成交留下持仓 → 进程重启 → _restore 从 store 读出持仓
        #         但真实账户可能已被用户在快期3手工平仓 / 隔夜强减 / 其它程序操作。
        #         若不在 _restore 末尾立刻对账，第一根 on_bar 之前引擎会误把
        #         "幽灵持仓" 当真，继续傻等平仓 / 误判新信号。
        #   行为：
        #     · broker 有 real_position → 调 _reconcile_positions(source="restore")
        #     · broker 无 real_position（dry_run） → skip
        #     · 若 broker.real_position 抛异常 → 写 warning，不阻断启动
        #   与 on_bar 路径差异：
        #     · source="restore"：跳过"入场当根 K 线"判定（bars_seen 可能为 0）
        #     · source="on_bar"：保留"入场当根 K 线"判定（避免误判刚开仓为已平）
        # ════════════════════════════════════════════════════════════════
        if not self.positions.is_empty():
            fn = getattr(self.broker, "real_position", None)
            if callable(fn):
                try:
                    self._reconcile_positions(source="restore")
                except Exception as e:
                    self.ev.write("restore_reconcile_failed",
                                  reason="{}: {}".format(type(e).__name__, e),
                                  note="首拉真实持仓失败，引擎按本地 store 启动")

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
        # Phase F：持久化 _unlock_in_flight —— 引擎崩 / 重启后 _restore 才能
        #   恢复卡单标记，让 _check_unlock_stuck 继续在 bars_seen>=submit+5 时
        #   触发 broker.trade_confirmed 复核。无此持久化时重启会让 in_flight
        #   永驻为 None，F1 检测整段失效（幽灵持仓残留无法兜底）。
        if self._unlock_in_flight is not None:
            self.store.set_json("_unlock_in_flight", self._unlock_in_flight)
        else:
            self.store.delete_key("_unlock_in_flight")

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

        # ════════════════════════════════════════════════════════════════
        # Phase F1：UNLOCK 卡单监控
        #   UNLOCK 报单 5 bars 后未确认 → 调 _reconcile_positions 兜底
        #   必须在 _reconcile_position 之前调用，否则 reconcile 清掉残留持仓后
        #   无法识别"UNLOCK 卡单"与"普通外部平仓"的差异
        # ════════════════════════════════════════════════════════════════
        self._check_unlock_stuck(bar)

        # 持仓对账（增强 B）：与券商真实持仓比对。若发现持仓已被外部平掉
        # （如用户在快期3手工平仓）或属幽灵持仓，立即修正引擎账目，
        # 避免继续傻等平仓 / 误判新信号。dry_run 等无真实账户的通道返回 None，跳过。
        # E3.3：多仓版判空用 not positions.is_empty()，避免 self.position property 在多仓时报错
        if not self.positions.is_empty():
            self._reconcile_position()

        if not self.positions.is_empty():
            self._settle_position(bar)

    def _settle_position(self, bar: Bar) -> None:
        """【E3.3 兼容壳】单仓 settle。E3.3 起实际逻辑在 _settle_positions。
        on_bar 已直接调 _settle_positions；保留本函数以防外部旧测试直接调用。
        """
        self._settle_positions(bar)

    def _settle_positions(self, bar: Bar) -> None:
        """【E3.3】多仓 settle——遍历 positions 逐笔判 exit，触发的仓位一次性 FIFO 平仓。

        与 E3.1 _settle_position 区别：
          · E3.1 单仓：pos=self.position → 单笔判 exit → 单笔 _close_position
          · E3.3 多仓：for-each positions → 收集本 bar 触发 exit 的所有仓 → 一次性 FIFO 平仓
            （一次性提交：避免多笔平仓单分散在不同 bar 上，trade 时间戳错乱）

        FIFO 出场顺序由 _close_positions 内部保证（按 entry_bar_seq ASC）。
        """
        # FIFO 排序：先判最早的仓位（防御性，虽然调用方通常已按顺序遍历）
        ordered = sorted(self.positions.positions, key=lambda p: p.entry_bar_seq)

        to_close: List[Position] = []
        # 第一笔触发的 trigger_price 作为整批平仓的统一触发价（避免多 trade 价格不一致）
        batch_trigger_price: Optional[float] = None
        batch_reason: Optional[str] = None

        for pos in ordered:
            # 入场那根 K 线不参与出场判定（沿用 E3.1 语义）
            if bar.timestamp <= pos.entry_bar_ts:
                continue

            bars_held = max(0, self.bars_seen - pos.entry_bar_seq)
            check: Optional[ExitCheck] = self.exit_policy.check(
                pos, bar, self.spec, bars_held)

            if check is None:
                if self.cfg.risk.close_before_session_end and self._after_close(bar):
                    check = ExitCheck("eod", bar.close)
            if check is None:
                continue

            # 更新 exit_plan（如有）
            if check.plan is not None:
                pos.exit_plan = check.plan

            # only_update：不触发平仓，只更新 plan
            if check.only_update:
                self.ev.write("exit_plan_update", reason=check.reason,
                              stop=pos.exit_plan.stop_price, tp=pos.exit_plan.tp_price,
                              symbol=pos.symbol, position_signal_key=pos.signal_key)
                continue

            # 触发平仓：加入批次
            to_close.append(pos)
            # 第一笔触发的原因 + 价格作为整批的 reason / trigger_price
            # （后续笔的 check.reason / check.price 仅记事件，不影响批次执行）
            if batch_trigger_price is None:
                batch_trigger_price = check.price
                batch_reason = check.reason

        # 一次性 FIFO 平仓（避免每笔独立 submit 时序错乱）
        if to_close:
            self._persist()  # 先持久化 exit_plan 更新
            self._close_positions(to_close, batch_reason or "settle_exit",
                                  batch_trigger_price or bar.close,
                                  bar, signal_key=to_close[0].signal_key)

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
            cost = self.spec.cost_points(pos.entry_price, ref_price,
                                         close_today=self.spec.close_today_first)
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
            self.risk.on_trade_closed(net, pos.volume)

            self.positions.remove(pos)
            self.ev.write("position_externally_closed",
                          reason="reconcile_external_partial",
                          side=str(pos.side), symbol=pos.symbol,
                          signal_key=pos.signal_key,
                          exit_price=ref_price,
                          gross_points=t.gross_points,
                          net_points=t.net_points,
                          fifo_index=idx, batch_size=len(close_list),
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

        # 未确认：查 broker.real_position(target.side) 判定卡单 vs 恢复
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

        if self._state == EngineState.IN_TRADE and len(self.positions) > 0:
            # ════════════════════════════════════════════════════════════════
            # Phase E3.2：多仓守卫 —— max_open_positions ≥ 2 时 on_signal 必须区分
            # 单仓（E3.1 行为） vs 多仓（E3.2 限制） 两套语义。
            #   · 单仓 (len==1) —— 保留原逻辑：反向 close_only、同向 skip
            #   · 多仓 (len>=2) —— E3.2 限制：
            #       全同向多仓 + 同向信号 → skip "in_trade_multi_same_side"
            #       含反向仓 + 反向信号 → skip "reverse_deferred_to_e33"（E3.3 才接入 FIFO 反手）
            #       含反向仓 + 同向信号 → skip "in_trade_mixed_same_signal"
            #   不做"在多仓里追加同向"：E3.3 才实现 portfolio 级别 FIFO。
            #
            # 注意：判断条件用 `len(self.positions) > 0` 而不是 `self.position is not None`，
            #       因为 self.position property → book.legacy_single() 在多仓时抛
            #       PositionBookError（E3.1 守护），E3.2 多仓守卫必须在不调 legacy_single
            #       的前提下区分单/多仓。
            # ════════════════════════════════════════════════════════════════
            if len(self.positions) >= 2:
                same_side_n = len(self.positions.same_side_positions(sig.side))
                pos_side_long = (self.positions.positions[0].side.value > 0)
                is_opposite = (sig.is_buy != pos_side_long)

                if same_side_n == len(self.positions):
                    # 全同向多仓 + 同向新信号
                    self.store.update_signal_action(
                        sig.key, "skip", "in_trade_multi_same_side")
                    self.ev.write("signal_skip", key=sig.key,
                                  reason="in_trade_multi_same_side",
                                  n=len(self.positions),
                                  same_side_n=same_side_n)
                    return
                if is_opposite:
                    # 含反向仓 + 反向信号：E3.3 才接 FIFO 反手
                    self.store.update_signal_action(
                        sig.key, "skip", "reverse_deferred_to_e33")
                    self.ev.write("signal_skip", key=sig.key,
                                  reason="reverse_deferred_to_e33",
                                  n=len(self.positions),
                                  same_side_n=same_side_n)
                    return
                # 含反向仓 + 同向信号
                self.store.update_signal_action(
                    sig.key, "skip", "in_trade_mixed_same_signal")
                self.ev.write("signal_skip", key=sig.key,
                              reason="in_trade_mixed_same_signal",
                              n=len(self.positions),
                              same_side_n=same_side_n)
                return

            # 单仓（E3.1 默认 max=1）：保留原逻辑。
            # 此时 self.position (property → legacy_single) 安全可用。
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
            # Phase E3.2：多反向仓防御 —— E2 假设 max=1（反向仓最多 1 笔）。
            # max_open_positions ≥ 2 + IDLE + 多反向仓 是异常配置组合，
            # 暂记 warning，由用户/运维修正 cfg（E3.3 才接多笔解锁）。
            if len(self.positions.opposite_positions(sig.side)) > 1:
                self.ev.write(
                    "unlock_partial_warning",
                    key=sig.key,
                    reason="multi_opposite_in_portfolio_e33_scope",
                    opp_n=len(self.positions.opposite_positions(sig.side)))
            # signal_action 由 _unlock_position 内部根据成交/拒单写
            # （这里不写"unlock"，否则 UNLOCK 拒单时它的"rejected"覆盖会被 on_signal
            # 的"unlock"反过来覆盖，丢失失败原因）
            self._unlock_position(sig, sig.side)
            self.ev.write("signal_unlock", key=sig.key, reason="reverse_yesterday_position")
            return

        # Phase E3.2：IDLE + 多同向仓守卫。
        #   理论：IDLE 表示当前无在持今仓，portfolio 非空只可能是反向昨仓
        #         （走上面 has_opposite 分支），不会留多同向。
        #   但 max≥2 配置下，若用户在外部终端反复平反向仓留下同向残留，
        #   IDLE + 同向残留 + 新同向信号 → 显式 skip "idle_with_same_side"，
        #   避免引擎对"沉睡仓位"重复叠加。
        if (self._state == EngineState.IDLE
                and not self.positions.is_empty()
                and not self.positions.has_opposite(sig.side)):
            same_n = len(self.positions.same_side_positions(sig.side))
            if same_n > 0:
                self.store.update_signal_action(
                    sig.key, "skip", "idle_with_same_side")
                self.ev.write("signal_skip", key=sig.key,
                              reason="idle_with_same_side",
                              n=len(self.positions), same_side_n=same_n)
                return

        decision = self.entry_policy.decide(sig, self.position, self.spec)
        if not decision:
            self.store.update_signal_action(sig.key, "skip", decision.reason)
            self.ev.write("signal_skip", key=sig.key, reason=decision.reason)
            return

        bar_date = self.last_bar.date if self.last_bar else sig.date

        # Phase E3.2：调 _size_batch 拿 (per_batch, batch_count, reason)，
        # 默认 batch_count=1 ⇒ 与 _size_position 完全等价（零行为变化）。
        per_batch, batch_count, why_vol = self._size_batch(sig)
        if per_batch <= 0 or batch_count <= 0:
            self.store.update_signal_action(sig.key, "risk_block", why_vol)
            self.ev.write("risk_block", key=sig.key, side=str(decision.side or sig.side),
                          reason=why_vol, bar_date=bar_date)
            return

        # 手数上限以 sizer 的有效上限为准（sizing 关闭时它 == risk.max_volume，行为不变）。
        # 否则会出现「sizing 算 4 手、风控按 risk.max_volume=1 拦」的死角。
        # Phase E3.2：传 batch_count + existing_same_side 给 risk，risk 仅做诊断不拦截。
        existing_same = len(self.positions.same_side_positions(decision.side or sig.side))
        ok, why = self.risk.check_open(decision.side or sig.side, per_batch, bar_date,
                                       max_volume=self.sizer.max_volume,
                                       batch_count=batch_count,
                                       existing_same_side=existing_same)
        if not ok:
            self.store.update_signal_action(sig.key, "risk_block", why)
            self.ev.write("risk_block", key=sig.key, side=str(decision.side or sig.side),
                          reason=why, bar_date=bar_date)
            return

        self._open_positions(sig, decision.side or sig.side,
                             per_batch=per_batch, batch_count=batch_count)

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

    # ---------------- Phase E3.2：批次版手数定档 ----------------
    def _size_batch(self, sig: "Signal") -> "Tuple[int, int, str]":
        """问仓位管理"这笔每笔几手 + 开几笔"。默认 batch_count=1 ⇒ 与 _size_position 完全等价。

        与 _size_position 共享同样的输入降级链路（权益/止损/ATR），仅把
        sizer.size() 换成 sizer.size_batch()。原因里带 "+batch=N" 后缀便于审计。
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

        return self.sizer.size_batch(equity=equity, price=price,
                                     stop_distance_points=stop_dist, atr_points=atr)

    # ---------------- 开 / 平 ----------------
    def _open_position(self, sig: Signal, side, volume: int) -> None:
        # Phase E3.2：兼容壳。E3.1 旧调用方（含 P5..P12 测试）走的就是单笔；
        # 转发到 _open_positions 并固定 batch_count=1，行为与原实现完全一致。
        self._open_positions(sig, side, per_batch=volume, batch_count=1)

    def _open_positions(self, sig: Signal, side, *, per_batch: int = 1,
                        batch_count: int = 1) -> None:
        """Phase E3.2：批次开仓 —— 把"开 N 手"拆成"开 N 个独立 Position 各 per_batch 手"。

        设计要点
          · effective_batch = max(0, min(batch_count, cfg_max - same_side_n))
            —— 同向仓已满 cfg.max_open_positions 时静默不开（不报错）
            —— 超出 cfg.max 时静默截断 + max_open_cap 告警（满足"静默填到 max"决策）
          · batch_count=1（默认/E3.1 兼容路径）⇒ effective_batch=1，行为零变化
          · 逐笔 broker.submit 独立 sub_key（sig.key#idx），独立 Position.add
          · 首笔拒单整批停（保持 E3.1 行为：拒单不留幽灵）
          · state 转移：任一笔成交 → IN_TRADE；全部拒单 → IDLE
          · signal_action 总结：opened（全部）/ partial（部分）/ rejected（首笔拒单）/
            split_silenced（同向仓已满 cfg_max）
        """
        if per_batch <= 0 or batch_count <= 0:
            self.store.update_signal_action(
                sig.key, "rejected", "zero_volume_or_batch")
            self.ev.write("order_rejected", key=sig.key,
                          reason="zero_volume_or_batch",
                          per_batch=per_batch, batch_count=batch_count)
            return

        cfg_max = self.cfg.risk.max_open_positions
        same_side_n = len(self.positions.same_side_positions(side))

        # 静默截断：现存同向仓已满 cfg.max → 不开、不报错（"静默填到 max"）
        if cfg_max > 0 and same_side_n >= cfg_max:
            self.ev.write(
                "split_silenced", key=sig.key,
                reason="same_side_already_max", cfg_max=cfg_max,
                same_side_n=same_side_n, batch_count=batch_count,
                note="引擎静默填到 max_open_positions；本批 0 笔提交")
            self.store.update_signal_action(
                sig.key, "split_silenced",
                "same_side_full_n={}".format(same_side_n))
            return

        effective_batch = (
            min(batch_count, cfg_max - same_side_n) if cfg_max > 0 else batch_count)

        # 截断告警（如 effective_batch < batch_count，说明 cfg.max 不够装）
        if effective_batch < batch_count:
            self.ev.write(
                "max_open_cap", key=sig.key,
                cfg_max=cfg_max, requested=batch_count,
                effective=effective_batch, same_side_n=same_side_n,
                note="cfg.max_open_positions 不足以容纳本批，截断静默")

        # 进入 OPENING（瞬态）。成交后才转 IN_TRADE。
        self._state = EngineState.OPENING

        opened_positions: list = []
        for idx in range(effective_batch):
            # Phase E3.2 兼容：effective_batch==1（默认/E3.1 路径）⇒ sub_key 就是 sig.key
            # —— 旧 Position.signal_key 字段期望无后缀，加 #0 会破坏 P10 等旧测试。
            sub_key = sig.key if effective_batch == 1 else "{}#{}".format(sig.key, idx)
            o = self.broker.submit(
                OrderIntent.OPEN, side, per_batch, sig.price, sub_key,
                note="缠论{}点信号开仓 #{}/{}".format(
                    "买" if sig.is_buy else "卖", idx + 1, effective_batch))
            self.store.save_order(o)
            self.ev.write("order", order_id=o.order_id, action=o.action,
                          intent=o.meta.get("intent", o.action),
                          side=str(o.side), volume=o.volume, price=o.price,
                          req_price=o.req_price, status=o.status, broker=o.broker,
                          batch_idx=idx, batch_size=effective_batch)

            # 单笔拒单：整批停（与 E3.1 _open_position 拒单语义一致）
            if o.status != "filled" or o.filled_price is None:
                why = o.meta.get("reject_reason") or o.status
                self.store.update_signal_action(
                    sig.key, "rejected", why)
                self.ev.write("order_rejected", key=sig.key, order_id=o.order_id,
                              reason=why, batch_idx=idx, batch_size=effective_batch)
                self._state = EngineState.IDLE
                return

            # 成交：建独立 Position（独立 sub_key + exit_plan）
            entry_price = o.filled_price
            plan: ExitPlan = self.exit_policy.plan(sig, entry_price, self.spec)
            pos = Position(
                symbol=self.spec.trade_symbol, side=side, volume=o.volume,
                entry_price=entry_price, entry_at=now_cn(),
                entry_bar_ts=self.last_bar.timestamp if self.last_bar else 0,
                entry_bar_seq=self.bars_seen,
                signal_key=sub_key, open_order_id=o.order_id, exit_plan=plan,
                entry_mode=EntryMode.OPEN_FIRST)
            self.positions.add(pos)
            opened_positions.append(pos)

            self.ev.write("open", symbol=pos.symbol, side=str(side),
                          volume=o.volume, entry_price=entry_price,
                          stop=plan.stop_price, tp=plan.tp_price,
                          exit_policy=plan.name, exit_params=plan.params,
                          signal_key=sub_key, parent_sig=sig.key,
                          entry_mode=pos.entry_mode.value,
                          batch_idx=idx, batch_size=effective_batch)

        # 持久化一次（多仓时一次性写盘，避免分笔抖动）
        self._persist()

        # signal_action 总结
        n_opened = len(opened_positions)
        if n_opened == effective_batch:
            self.store.update_signal_action(
                sig.key, "opened",
                "batch={}/{}".format(n_opened, batch_count))
        elif n_opened == 0:
            self.store.update_signal_action(
                sig.key, "rejected", "all_batch_rejected")
        else:
            self.store.update_signal_action(
                sig.key, "partial",
                "opened={}/{}".format(n_opened, batch_count))

        # state：任一成交 → IN_TRADE
        self._state = EngineState.IN_TRADE if n_opened > 0 else EngineState.IDLE

    def _close_position(self, reason: str, trigger_price: float,
                        bar: Optional[Bar], signal_key: str = "") -> None:
        """【E3.3 兼容壳】单仓平仓。

        E3.3 起实际平仓逻辑迁到 _close_positions；本函数保留以兼容：
          · on_signal 反向信号路径（E3.2 多仓守卫已 skip 多仓场景，到这里一定单仓）
          · tests/test_p10_state_machine.py 等旧测试直接调 _close_position

        多仓场景请直接调 _close_positions(positions, ...)。
        """
        pos = self.position  # 走 property → legacy_single() → 多仓抛 PositionBookError
        if pos is None:
            return
        self._close_positions([pos], reason, trigger_price, bar, signal_key)

    def _close_positions(self, positions: List[Position], reason: str,
                         trigger_price: float, bar: Optional[Bar],
                         signal_key: str = "") -> None:
        """【E3.3】多仓平仓——按 FIFO（entry_bar_seq ASC）逐笔平仓。

        语义约定（与 E3.1 单仓 _close_position 等价 + 多仓扩展）：
          · 顺序：FIFO（最早建仓先平）—— 同根 bar 触发的多仓按建仓时间顺序平
          · 拒单策略：第一笔拒单 → 整批停 + retry cooldown（沿用 _close_retry_bars）
            后续笔拒单 → 保留剩余仓位继续（不阻塞已部分成交仓位）
          · phantom 兜底：retry streak 超限 → 清掉所有目标仓位（与 E3.1 一致）
          · 部分成交：剩余仓位保留在 book + state EXITING
          · 全部成交：state IDLE

        调用方传入的 positions 列表会自动按 entry_bar_seq 排序（防御性）。
        """
        if not positions:
            return

        # FIFO 排序——按建仓时间升序（防御性：即使调用方传乱序也保证 FIFO）
        ordered = sorted(positions, key=lambda p: p.entry_bar_seq)

        # Phase A：进入瞬态 EXITING（任一平仓动作触发）
        self._state = EngineState.EXITING

        # P3 配套：连续 close 失败 cooldown（沿用 _close_position 旧 cooldown 字段）
        now_ts = self.last_bar.timestamp if self.last_bar else 0
        if (self._last_close_failed_bar_ts
                and now_ts - self._last_close_failed_bar_ts <= self._close_retry_bars):
            return  # cooldown 中：保持 EXITING，下一根 bar 再试

        # 清掉 E3.1 单仓版的 streak 字段（_close_position 旧逻辑），改用 FIFO 批次内失败计数
        # 注：保留 _close_fail_streak 用于 phantom 兜底判定，但不再每笔递增（仅首笔拒单递增）
        n_closed = 0
        n_rejected = 0
        first_rejected = False

        for idx, pos in enumerate(ordered):
            # Phase D：按 pos.entry_mode 决定离场方式（硬规则，不留开关）
            intent, side = self._exit_intent(pos)

            o = self.broker.submit(intent, side, pos.volume, trigger_price,
                                   signal_key or pos.signal_key,
                                   note=reason)
            self.store.save_order(o)
            self.ev.write("order", order_id=o.order_id, action=o.action,
                          intent=o.meta.get("intent", intent.value),
                          side=str(o.side), volume=o.volume, price=o.price,
                          req_price=o.req_price, status=o.status, broker=o.broker,
                          reason=reason, exit_mode=intent.value,
                          position_signal_key=pos.signal_key,
                          fifo_index=idx, batch_size=len(ordered))

            # 平仓被拒/超时：保留持仓，等下一根 K 线再试
            if o.status != "filled" or o.filled_price is None:
                why = o.meta.get("reject_reason") or o.status
                self.ev.write("order_rejected", key=pos.signal_key, order_id=o.order_id,
                              action=intent.value, reason=reason, reject=why,
                              fifo_index=idx, batch_size=len(ordered))
                self._last_close_failed_bar_ts = now_ts
                n_rejected += 1

                if not first_rejected and n_closed == 0:
                    # 第一笔拒单 → 整批停 + cooldown（避免 broker 故障时反复 submit）
                    first_rejected = True
                    self._close_fail_streak += 1
                    if self._close_fail_streak >= self._close_max_streak:
                        # phantom 清掉（兜底）：所有目标仓位都视为外部已平
                        self.ev.write("position_drop",
                                      reason="close_repeatedly_rejected",
                                      streak=self._close_fail_streak,
                                      batch_size=len(ordered))
                        for p in ordered:
                            self.positions.remove(p)
                        self._persist()
                        self._state = EngineState.IDLE
                    return  # 整批停
                # 后续笔拒单：保留剩余仓位继续
                continue

            # 成功平仓：清掉失败计数
            self._last_close_failed_bar_ts = 0
            if first_rejected is False:
                # 仅在全部成交时重置 streak（部分成交场景保留 streak 给后续 bar 处理）
                pass
            else:
                # 已部分成交 + 当前笔成功：重置 streak
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

            # E3.3 关键：从 book 移除（多仓版必须 remove 单仓版无需）
            self.positions.remove(pos)
            n_closed += 1

            self.ev.write("close", symbol=t.symbol, side=str(t.side), reason=reason,
                          entry=t.entry_price, exit=t.exit_price,
                          gross=t.gross_points, cost=t.cost_points,
                          net=t.net_points, cash=t.net_cash, bars_held=bars_held,
                          trade_id=t.trade_id, exit_policy=t.exit_plan_name,
                          entry_mode=pos.entry_mode.value,
                          exit_mode=intent.value,
                          position_signal_key=pos.signal_key,
                          fifo_index=idx, batch_size=len(ordered))

        # 全部处理完毕（全部成交 / 部分成交 + 后续拒单 / 全部拒单后整批停早 return）
        self._persist()
        if self.positions.is_empty():
            # 所有仓位都平了 → IDLE
            self._state = EngineState.IDLE
        # else: 仍有仓位（部分成交或 cooldown 中）→ 保持 EXITING

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
        # ════════════════════════════════════════════════════════════════
        # Phase F1：UNLOCK 卡单监控 —— 报单成功后设 in-flight
        #   5 bars 后 _check_unlock_stuck 会调 broker.trade_confirmed 复核。
        #   拒单/异常分支不设 in-flight（目标未平 → on_signal 再次走 UNLOCK 会重新记）
        #   trade_records 不足时 → False → 查 broker.real_position(target.side)：
        #     · > 0 → 卡单确认 → 用 target_snapshot 重建 portfolio
        #     · == 0 → 卡单恢复 → 清 in-flight
        #
        #   target_snapshot 必须存：卡单时需要重建 Position 对象（target 已被 remove）
        #   target_side 必须存：real_position() 需要 side 参数
        # ════════════════════════════════════════════════════════════════
        self._unlock_in_flight = {
            "signal_key": sig.key,
            "target_signal_key": target.signal_key,
            "target_side": target.side.name,       # "LONG" / "SHORT"
            "target_snapshot": target.to_dict(),   # 卡单重建用
            "submit_bar_ts": (self.last_bar.timestamp if self.last_bar else 0),
            "submit_bar_seq": self.bars_seen,
        }
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

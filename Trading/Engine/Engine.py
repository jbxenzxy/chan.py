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

import time
from typing import Any, Dict, List, Optional, Tuple

from ..Broker.Base import Broker
from ..Config import TradingConfig
from ..Infra.EventLog import EventLog
from ..Infra.PeriodProfile import (
    bar_secs_for,
)
from .PositionBook import PositionBook, PositionBookError
from .Reconcile import ReconcileMixin
from ..Infra.Store import Store
from ..Strategy.Base import EntryPolicy, ExitCheck, ExitPolicy
from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.Types import (
    Bar, DecisionType, EntryMode, EngineState, ExitMode, ExitPlan, Order, OrderIntent,
    Position, Side, Signal, Trade, now_cn,
)


class TradingEngine(ReconcileMixin):
    def __init__(self, cfg: TradingConfig, broker: Broker,
                 entry_policy: EntryPolicy, exit_policy: ExitPolicy,
                 store: Store, ev: EventLog):
        self.cfg = cfg
        self.spec: InstrumentSpec = cfg.instrument
        self.broker = broker
        self.entry_policy = entry_policy
        self.exit_policy = exit_policy
        self.store = store
        self.ev = ev
        # 开仓手数（@2026-09-08 二次精简）：每个买卖点只开一笔，一笔挂 N 手，
        # N = 风控层 `risk.max_volume`。原"仓位管理 PositionSizer/SizingConfig"
        # 整条通道已删除，开仓手数不再经任何计算，直接取风险层配置。
        self.lots_per_signal: int = int(cfg.risk.max_volume)
        # 解锁昨仓后是否补开今仓缺额（原生于 SizingConfig，迁至 Wind RiskConfig）
        self.unlock_no_new_open: bool = bool(cfg.risk.unlock_no_new_open)

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
        # Step 1 修复（2026-09-08）：cooldown 改用**根数**口径。
        #   旧代码拿"毫秒时间戳差值"去和"根数 5"比 → 实际是 5 毫秒，
        #   冷却从来没生效过（namespace 级的单位混用 bug）。
        #   真正的根数 = bars_seen 序号差，与周期、与时间戳单位都无关。
        self._last_close_failed_bar_seq: int = 0
        self._close_fail_streak: int = 0
        # Step 2.2（2026-09-08）：三项时序常量归一到 cfg.engine（原硬编码 5/20/5 收口），
        #   属性名不变，下游消费点（cooldown 判定 / 幻影清除 / Reconcile 卡单复核）零改动。
        self._close_retry_bars: int = cfg.engine.close_retry_bars    # 失败后冷却多少根 bar 再试
        self._close_max_streak: int = cfg.engine.close_max_streak    # 连续失败这么多根后清掉幻影持仓
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
        self._unlock_stuck_bars: int = cfg.engine.unlock_stuck_bars   # 报单后多少 bar 触发复核（与 close_retry_bars 对齐）
        # ════════════════════════════════════════════════════════════════
        # 解锁复核 in-flight（单笔）：UNLOCK 报单成功后挂起，5 bars 后
        # _check_unlock_stuck 调 broker.trade_confirmed 复核真实持仓。
        # ════════════════════════════════════════════════════════════════
        # ════════════════════════════════════════════════════════════════
        # Phase I1（2026-09-06）：自动下单开关
        #   True = 正常接收买卖点信号并交易（默认）
        #   False = 关闭语义（用户拍板）：
        #       ① 不再接收买卖点信号（on_signal 顶部门，见 auto_order_off skip）
        #       ② 把簿内所有「未锁定」持仓锁仓（_lock_remaining_positions；
        #          LOCK 软离场落簿反向 LOCKED 仓，次日对向信号解锁）
        #   由前端开关经后端进程托管触发（App/AppTrader.py → main.py 的
        #   shutdown_and_lock_all），_restore/_persist 持久化，重启不漂移。
        # ════════════════════════════════════════════════════════════════
        # ════════════════════════════════════════════════════════════════
        # Step 1（2026-09-08）：周期 bar 秒数 —— 引擎按 source.freq 推导（用于追价窗口检查）。
        #   （2026-09-08 精简：原 L4 时间/收盘兜底的 set_bar_secs 注入已随功能删除。）
        self.bar_secs: Optional[int] = bar_secs_for(cfg.source.freq, default=None)
        # 离场追价窗口（close_max_chase × chase_interval）若长于一根 bar，
        # 15s 下会出现"上一轮还没追完、下一根 bar 又发起新一轮"的叠加。
        # 不阻断（引擎本来就跨 bar 重试），但必须可见——历史上这类问题
        # 全靠"静默降级"被藏起来。
        _bp = cfg.broker_params
        chase_window = float(_bp.close_max_chase) * float(_bp.chase_interval)
        if self.bar_secs and chase_window > self.bar_secs:
            self.ev.write(
                "chase_window_exceeds_bar", freq=cfg.source.freq,
                bar_secs=self.bar_secs, chase_window_sec=round(chase_window, 2),
                note="离场追价窗口长于一根 bar；短周期（15s）请把 "
                     "close_max_chase × chase_interval 调到 bar_secs 以内")

        self.auto_order_enabled: bool = True
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
        # Phase A：根据持仓推断初始 state。在持今仓 → IN_TRADE；无持仓 → IDLE。
        # Phase H1：簿内只剩 LOCKED 锁仓（昨日 LOCK 遗留、等对向信号解锁）→ IDLE，
        #   让下个信号走 on_signal 的 E2 UNLOCK 门（unlock_against_signal 记帐 +
        #   CloseYesterday 费率），而不是 IN_TRADE 的 signal_reverse 平仓路径。
        if (self.positions.is_empty()
                or all(p.entry_mode is EntryMode.LOCKED
                       for p in self.positions.positions)):
            self._state = EngineState.IDLE
        else:
            self._state = EngineState.IN_TRADE
        self.bars_seen = int(self.store.get_json("bars_seen", 0) or 0)
        # Phase F：恢复 _unlock_in_flight —— 接续上次崩前的卡单标记，
        #   让 _check_unlock_stuck 在余下 bar 进度下继续推进到 5-bar 复核。
        #   不恢复的副作用：引擎崩溃一次即丢卡单检测能力，F1 形同虚设。
        fl = self.store.get_json("_unlock_in_flight")
        if isinstance(fl, dict):
            self._unlock_in_flight = fl
        # 解锁复核（unlock reconcile）：恢复卡单标记后无需额外处理 ——
        # state 按持仓推断（上方），复核窗口由 bars_seen 推进。

        # Phase I1：恢复自动下单开关。关闭语义要跨重启保持
        # （前端关闭 → 子进程退出 → 再启动服务/引擎必须仍是关闭态，
        # 不能悄悄重新开始接收信号）。AppTrader.start 显式置 True 再拉起。
        self.auto_order_enabled = bool(self.store.get_json("auto_order_enabled", True))

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
        self.store.set_json("bars_seen", self.bars_seen)
        # Phase F：持久化 _unlock_in_flight —— 引擎崩 / 重启后 _restore 才能
        #   恢复卡单标记，让 _check_unlock_stuck 继续在 bars_seen>=submit+5 时
        #   触发 broker.trade_confirmed 复核。无此持久化时重启会让 in_flight
        #   永驻为 None，F1 检测整段失效（幽灵持仓残留无法兜底）。
        if self._unlock_in_flight is not None:
            self.store.set_json("_unlock_in_flight", self._unlock_in_flight)
        else:
            self.store.delete_key("_unlock_in_flight")
        # Phase I1：持久化自动下单开关（跨重启保持关闭语义）
        self.store.set_json("auto_order_enabled", self.auto_order_enabled)

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

        # （2026-09-08：原 on_bar 里的 RiskGate.roll_day 换日统计随风控五道硬闸门整体删除。）

        self.ev.write("bar", date=bar.date, close=bar.close,
                      high=bar.high, low=bar.low, seq=self.bars_seen)

        # 心跳：真实 CTP 通道需要定期收发数据，否则被判"用户不活跃"断连。
        # dry_run 通道的 pulse() 是空实现，零开销。
        try:
            self.broker.pulse()
        except Exception:
            pass

        # ════════════════════════════════════════════════════════════════
        # 解锁复核（unlock reconcile）
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

        # Phase I1：自动下单关闭态 —— 不再判止盈止损/时间离场（引擎已决定
        # "全部锁仓"），残留的未锁定持仓（如上次锁仓被拒）继续补锁，直到
        # 簿内只剩 LOCKED。开启态维持原行为（settle 止盈止损/收盘强平）。
        if not self.positions.is_empty():
            if self.auto_order_enabled:
                self._settle_position(bar)
            else:
                self._lock_remaining_positions(bar,
                                               reason="auto_order_off_retry")

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
        exit_trigger_price: Optional[float] = None
        exit_reason: Optional[str] = None

        for pos in ordered:
            # Phase H1：锁仓（LOCK 软离场落簿的反向仓）不参与 TP/SL/EOD 出场判定。
            # 它的唯一合法离场 = 次日对向信号触发 UNLOCK（on_signal has_opposite 门）。
            # 若在此放行，EOD/止盈会把锁仓当日平掉（CloseYesterday 被 CTP 拒），
            # 或次日被 EOD 以平仓路径误杀（费率劣化），故显式跳过。
            if pos.entry_mode is EntryMode.LOCKED:
                continue
            # 入场那根 K 线不参与出场判定（沿用 E3.1 语义）
            if bar.timestamp <= pos.entry_bar_ts:
                continue

            bars_held = max(0, self.bars_seen - pos.entry_bar_seq)
            check: Optional[ExitCheck] = self.exit_policy.check_with(
                pos, bar, self.spec, bars_held=bars_held)

            # （2026-09-08：原引擎侧收盘前强平 _after_close / close_before_session_end
            #   随 L4 时间兜底一并删除，出场只剩策略层的 L1-L3。）

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
            if exit_trigger_price is None:
                exit_trigger_price = check.price
                exit_reason = check.reason

        # 一次性 FIFO 平仓（避免每笔独立 submit 时序错乱）
        if to_close:
            self._persist()  # 先持久化 exit_plan 更新
            self._close_positions(to_close, exit_reason or "settle_exit",
                                  exit_trigger_price or bar.close,
                                  bar, signal_key=to_close[0].signal_key)

    # （2026-09-08：原引擎侧 `_after_close`（收盘前强平）与 `_held_secs`（持仓秒数）
    #   随 L4 时间/收盘兜底一并删除，出场判定只依赖策略层 L1-L3。）

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
        # Phase I1：自动下单关闭门（用户拍板关闭语义 ①）
        #   关闭后不再接收任何买卖点信号：信号幂等键照常消费（防重放），
        #   但 action 记为 skip/auto_order_off，不进入任何交易决策。
        # ════════════════════════════════════════════════════════════════
        if not self.auto_order_enabled:
            self.store.update_signal_action(sig.key, "skip", "auto_order_off")
            self.ev.write("signal_skip", key=sig.key, reason="auto_order_off")
            return

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

        # ════════════════════════════════════════════════════════════════
        # 解锁入场路径（unlock-then-enter）
        #   触发条件 —— IDLE 时持仓簿非空 + has_opposite(sig.side)：
        #     · IDLE 表示当前没有在持今仓；簿非空意味着昨日 LOCK 后落簿的
        #       反向锁仓（entry_mode=LOCKED，可能不止一笔）
        #     · 信号方向与锁仓相反 → 这是一次"解锁昨仓 + 入场"信号
        #   行为：对最老的一笔锁仓持仓发一笔 UNLOCK（整笔全量，FOK），
        #   缺额补开与否由 unlock_no_new_open 决定（见 _unlock_position）。
        # ════════════════════════════════════════════════════════════════
        if (self._state == EngineState.IDLE
                and not self.positions.is_empty()
                and self.positions.has_opposite(sig.side)):
            self._unlock_position(sig, sig.side)
            self.ev.write("signal_unlock", key=sig.key, reason="reverse_yesterday_position")
            return

        # ── state == IDLE：进入正常开仓决策 ──

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

        # 开仓手数 @2026-09-08：每个买卖点只开一笔，一笔挂 self.lots_per_signal 手
        # （= 风控层 risk.max_volume，默认 2）。原 PositionSizer/SizingConfig 手数定档
        #   通道已删除；不再有"手数 ≤ 0 被风控拦下"的分支。
        lots = self.lots_per_signal

        # （2026-09-08：资金闸门 capital_gate 与风控五道硬闸门 risk.check_open
        #   以及仓位管理 PositionSizer 均整体删除 —— 开仓手数只由风控层
        #   risk.max_volume 决定，另受 max_open_positions 笔数上限约束。）

        self._open_position(sig, decision.side or sig.side, lots)

    # （2026-09-08：原 `_size_position`（问仓位管理"这笔开几手"）、`_capital_gate`
    #   资金闸门薄委托均随"仓位管理 PositionSizing/SizingConfig 整条删除 + 资金闸门
    #   删除"一并移除；开仓手数直接取 self.lots_per_signal。）

    # ---------------- 开 / 平 ----------------
    def _open_position(self, sig: Signal, side, volume: int) -> None:
        """一笔开仓：一笔报单挂 volume 手（FOK），成交后簿面记一笔持仓。

        设计要点
          · 全 FOK：全成或全撤由交易所保证，成交归属永远无歧义——
            这笔报单要么整笔成交（簿面 1 笔 volume 手），要么整笔作废。
          · 中金所限价单每次最大下单 20 手：约定配置不超过它（见 RiskConfig.max_volume
            注释"中金所限价单单笔上限 20 手，配置不应超过"），故不再另设交易所 20 手拦截
            （2026-09-08 二次精简：原 over_exchange_limit 拒单随 PositionSizing 一并删除）。
          · 同向持仓笔数已达 cfg.risk.max_open_positions → 静默跳过（open_silenced，
            沿用"静默填到 max，不报错"决策）。
          · 拒单 → signal_action=rejected + 回 IDLE，等下一信号；不追价。
        """
        if volume <= 0:
            self.store.update_signal_action(
                sig.key, "rejected", "zero_volume")
            self.ev.write("order_rejected", key=sig.key,
                          reason="zero_volume", volume=volume)
            return

        cfg_max = self.cfg.risk.max_open_positions
        same_side_n = len(self.positions.same_side_positions(side))
        # 静默跳过：现存同向持仓已满 cfg.max → 不开、不报错（"静默填到 max"）
        if cfg_max > 0 and same_side_n >= cfg_max:
            self.ev.write(
                "open_silenced", key=sig.key,
                reason="same_side_already_max", cfg_max=cfg_max,
                same_side_n=same_side_n,
                note="引擎静默填到 max_open_positions；本信号不开仓")
            self.store.update_signal_action(
                sig.key, "open_silenced",
                "same_side_full_n={}".format(same_side_n))
            return

        # 进入 OPENING（瞬态）。成交后才转 IN_TRADE。
        self._state = EngineState.OPENING

        o = self.broker.submit(
            OrderIntent.OPEN, side, volume, sig.price, sig.key,
            note="缠论{}点信号开仓 {}手（FOK）".format(
                "买" if sig.is_buy else "卖", volume))
        self.store.save_order(o)
        self.ev.write("order", order_id=o.order_id, action=o.action,
                      intent=o.meta.get("intent", o.action),
                      side=str(o.side), volume=o.volume, price=o.price,
                      req_price=o.req_price, status=o.status, broker=o.broker)

        if o.status != "filled" or o.filled_price is None:
            why = o.meta.get("reject_reason") or o.status
            self.store.update_signal_action(sig.key, "rejected", why)
            self.ev.write("order_rejected", key=sig.key, order_id=o.order_id,
                          reason=why, volume=volume)
            self._state = EngineState.IDLE
            return

        # 成交：簿面记一笔持仓（volume 手，独立 exit_plan）
        entry_price = o.filled_price
        plan: ExitPlan = self.exit_policy.plan(sig, entry_price, self.spec)
        pos = Position(
            symbol=self.spec.trade_symbol, side=side, volume=volume,
            entry_price=entry_price, entry_at=now_cn(),
            entry_bar_ts=self.last_bar.timestamp if self.last_bar else 0,
            entry_bar_seq=self.bars_seen,
            signal_key=sig.key, open_order_id=o.order_id, exit_plan=plan,
            entry_mode=EntryMode.OPEN_FIRST)
        self.positions.add(pos)

        self.ev.write("open", symbol=pos.symbol, side=str(side),
                      volume=pos.volume, entry_price=entry_price,
                      stop=plan.stop_price, tp=plan.tp_price,
                      exit_policy=plan.name, exit_params=plan.params,
                      signal_key=sig.key, entry_mode=pos.entry_mode.value)

        self._persist()
        self.store.update_signal_action(sig.key, "opened", "lots={}".format(volume))
        self._state = EngineState.IN_TRADE

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
                         signal_key: str = "", force_lock: bool = False) -> None:
        """【E3.3】多仓平仓——按 FIFO（entry_bar_seq ASC）逐笔平仓。

        语义约定（与 E3.1 单仓 _close_position 等价 + 多仓扩展）：
          · 顺序：FIFO（最早建仓先平）—— 同根 bar 触发的多仓按建仓时间顺序平
          · 拒单策略：第一笔拒单 → 整批停 + retry cooldown（沿用 _close_retry_bars）
            后续笔拒单 → 保留剩余仓位继续（不阻塞已部分成交仓位）
          · phantom 兜底：retry streak 超限 → 清掉所有目标仓位（与 E3.1 一致）
          · 部分成交：剩余仓位保留在 book + state EXITING
          · 全部成交：state IDLE

        force_lock（Phase I1）：True 时无视 entry_mode，目标持仓**全部 LOCK**
        （开反向同手数锁仓）。用于自动下单关闭语义 ② —— 无论 OPEN_FIRST
        还是 UNLOCK_FIRST 入场，关闭时一律锁仓（用户拍板：不区分入场方式）。

        调用方传入的 positions 列表会自动按 entry_bar_seq 排序（防御性）。
        """
        if not positions:
            return

        # FIFO 排序——按建仓时间升序（防御性：即使调用方传乱序也保证 FIFO）
        ordered = sorted(positions, key=lambda p: p.entry_bar_seq)

        # Phase A：进入瞬态 EXITING（任一平仓动作触发）
        self._state = EngineState.EXITING

        # P3 配套：连续 close 失败 cooldown（沿用 _close_position 旧 cooldown 字段）
        # P2-2：last_bar 为 None（引擎启动后从未收到 K 线）时按当前时间兜底——
        # 否则 0 - 上次失败时间戳 为负数 ≤ _close_retry_bars，cooldown 误把
        # 锁仓一笔不锁（尤其 shutdown 收尾路径），且无任何错误上报。
        # cooldown 判定用**根数**口径（bars_seen 序号差），与周期、时间戳单位
        # 都无关。旧代码拿毫秒时间戳差值去和"5 根"比 → 等价 5 毫秒，冷却恒不生效。
        now_ts = self.last_bar.timestamp if self.last_bar else time.time()
        if (self._last_close_failed_bar_seq
                and (self.bars_seen - self._last_close_failed_bar_seq)
                < self._close_retry_bars):
            return  # cooldown 中：保持 EXITING，下一根 bar 再试

        # 清掉 E3.1 单仓版的 streak 字段（_close_position 旧逻辑），改用 FIFO 批次内失败计数
        # 注：保留 _close_fail_streak 用于 phantom 兜底判定，但不再每笔递增（仅首笔拒单递增）
        n_closed = 0
        n_rejected = 0
        first_rejected = False

        for idx, pos in enumerate(ordered):
            # Phase D：按 pos.entry_mode 决定离场方式（硬规则，不留开关）。
            # force_lock（Phase I1）→ 全部 LOCK（自动下单关闭语义 ②）。
            if force_lock and pos.entry_mode is not EntryMode.LOCKED:
                opposite = Side.SHORT if pos.side is Side.LONG else Side.LONG
                intent, side = OrderIntent.LOCK, opposite
            else:
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
                          fifo_index=idx, pos_count=len(ordered))

            # 平仓被拒/超时：保留持仓，等下一根 K 线再试
            if o.status != "filled" or o.filled_price is None:
                why = o.meta.get("reject_reason") or o.status
                self.ev.write("order_rejected", key=pos.signal_key, order_id=o.order_id,
                              action=intent.value, reason=reason, reject=why,
                              fifo_index=idx, pos_count=len(ordered))
                self._last_close_failed_bar_ts = now_ts
                self._last_close_failed_bar_seq = self.bars_seen
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
                                      pos_count=len(ordered))
                        for p in ordered:
                            self.positions.remove(p)
                        self._persist()
                        self._state = EngineState.IDLE
                    return  # 整批停
                # 后续笔拒单：保留剩余仓位继续
                continue

            # 成功平仓：清掉失败计数
            self._last_close_failed_bar_ts = 0
            self._last_close_failed_bar_seq = 0
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
            # （2026-09-08：原 RiskGate.on_trade_closed 当日统计已随五道硬闸门删除。）

            # E3.3 关键：从 book 移除（多仓版必须 remove 单仓版无需）
            self.positions.remove(pos)
            n_closed += 1

            # ═══ Phase H1：LOCK 软离场 → 反向锁仓落簿 ═══
            # LOCK 成交后，broker 端真实存在一笔反向仓（offset=OPEN 开出）。
            # 此前它只在 broker 侧、引擎簿不可见 → 次日 has_opposite 恒 False，
            # "解锁入场"在纯自动流程中不可达（评审议题 S1）。
            # 现在把这笔反向仓作为 Position 落簿（entry_mode=LOCKED）：
            #   · 持久化 / 重启恢复 / 对账接管 全部走既有管线（零新增机制）
            #   · settle（TP/SL/EOD）跳过 LOCKED —— 锁仓唯一离场 = 次日对向信号 UNLOCK
            #   · 容量天然自洽：remove 1 + add 1，簿内总数不变
            if intent is OrderIntent.LOCK:
                lock_pos = Position(
                    symbol=pos.symbol, side=side, volume=pos.volume,
                    entry_price=o.filled_price, entry_at=now_cn(),
                    entry_bar_ts=(self.last_bar.timestamp if self.last_bar else 0),
                    signal_key=pos.signal_key + "#lock",
                    open_order_id=o.order_id,
                    exit_plan=ExitPlan(name="locked_await_unlock",
                                       stop_price=0.0),
                    entry_bar_seq=self.bars_seen,
                    entry_mode=EntryMode.LOCKED,
                )
                try:
                    self.positions.add(lock_pos)
                    self.ev.write(
                        "lock_booked", symbol=lock_pos.symbol,
                        side=str(lock_pos.side), volume=lock_pos.volume,
                        entry=lock_pos.entry_price, order_id=o.order_id,
                        lock_of=pos.signal_key,
                        position_signal_key=lock_pos.signal_key,
                        fifo_index=idx, pos_count=len(ordered))
                except PositionBookError as e:
                    # 防御：remove+add 1:1 下不该发生（cfg max 被外部改小等）。
                    # 落簿失败 → 反向仓留在 broker 端由对账/人工处理，不阻塞批次。
                    self.ev.write(
                        "lock_book_failed", symbol=pos.symbol, side=str(side),
                        volume=pos.volume, error=str(e),
                        order_id=o.order_id, lock_of=pos.signal_key)

            self.ev.write("close", symbol=t.symbol, side=str(t.side), reason=reason,
                          entry=t.entry_price, exit=t.exit_price,
                          gross=t.gross_points, cost=t.cost_points,
                          net=t.net_points, cash=t.net_cash, bars_held=bars_held,
                          trade_id=t.trade_id, exit_policy=t.exit_plan_name,
                          entry_mode=pos.entry_mode.value,
                          exit_mode=intent.value,
                          position_signal_key=pos.signal_key,
                          fifo_index=idx, pos_count=len(ordered))

        # 全部处理完毕（全部成交 / 部分成交 + 后续拒单 / 全部拒单后整批停早 return）
        self._persist()
        # Phase H1：簿空 或 簿内只剩 LOCKED 锁仓（等待次日对向信号解锁）→ IDLE。
        # 锁仓不属于"平仓未完成"，不应让引擎卡在 EXITING。
        remaining = self.positions.positions
        if (self.positions.is_empty()
                or all(p.entry_mode is EntryMode.LOCKED for p in remaining)):
            self._state = EngineState.IDLE
        # else: 仍有在持今仓（部分成交或 cooldown 中）→ 保持 EXITING

    # ---------------- 解锁入场（Phase E2 UNLOCK_FIRST 路径） ----------------
    def _unlock_position(self, sig: Signal, side: Side) -> None:
        """解锁入场：一笔 FOK 报单整笔解锁最老的一笔锁仓持仓，缺口补开默认关闭。

        语义（2026-09-06 全 FOK 重构后）
          · 一次反向信号只解一笔：取最老的一笔反向锁仓持仓（entry_bar_seq 升序），
            对它的全部手数发一笔 FOK 解锁报单。全成或全撤，无部分成交。
          · 不成交 → 整笔作废，回 IDLE，等下一个信号（入场不追价）。
          · 成交 → 记 Trade + 从簿删除 + 设解锁复核 in-flight。
          · 缺口补开（默认关闭，见 unlock_no_new_open）：
                new_lots = 今日信号想开手数 N - 已解锁手数 V
            unlock_no_new_open=True（默认）→ 缺口一律不补开（规避平今高手续费），
                只解锁昨仓、回 IDLE，今仓方向留给下一个信号；
            unlock_no_new_open=False → N > V 时补开 N-V 手、N <= V 纯解锁不补开。

        为什么"解锁 V 手后只补开 N-V 手"
            锁仓 = 反向开同手数把昨仓锁住。解锁平掉昨仓后，反向那 V 手变成
            实际净敞口（方向 = 今日信号方向），所以达到目标 N 手只需再开 N-V 手。
        """
        opp_list = sorted(self.positions.opposite_positions(side),
                          key=lambda p: p.entry_bar_seq)
        if not opp_list:
            # 理论不可能走到这里（on_signal 已 has_opposite 判定）。防御性记录。
            self.ev.write("unlock_skipped", key=sig.key,
                          reason="no_opposite_in_portfolio")
            return
        target = opp_list[0]          # 最老的一笔锁仓持仓

        # 进入 EXITING（清空旧持仓，对位 _close_position 的状态语义）
        self._state = EngineState.EXITING

        o = self.broker.submit(OrderIntent.UNLOCK, target.side, target.volume,
                               sig.price, sig.key,
                               note="信号解锁昨仓（FOK {}手）".format(target.volume))
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
                          action="unlock", reason=why, volume=target.volume)
            self._state = EngineState.IDLE
            return

        # 成交：记 Trade + 从簿删除（unlock 事件由 _book_unlock_trade 写出）
        self._book_unlock_trade(sig, target, o)

        # 解锁复核 in-flight：报单成功后设，若干 bars 后 _check_unlock_stuck 复核。
        # broker.trade_confirmed 为假时查真实持仓 —— >0 卡单确认（快照重建回簿），
        # ==0 卡单恢复（清 in-flight）。防的是"回报丢失"，不是"撮合不确定"。
        self._unlock_in_flight = {
            "signal_key": sig.key,
            "target_signal_key": target.signal_key,
            "target_side": target.side.name,       # "LONG" / "SHORT"
            "target_snapshot": target.to_dict(),   # 卡单重建用
            "submit_bar_ts": (self.last_bar.timestamp if self.last_bar else 0),
            "submit_bar_seq": self.bars_seen,
        }

        # ── 缺口补开：今日信号想开 N 手，已解锁 V 手 → 补开 N-V 手 ──
        v = int(target.volume)
        want = self.lots_per_signal     # 每个买卖点想开 N 手（= 风控层 max_volume）

        new_lots = max(0, int(want) - v)
        if new_lots > 0 and self.unlock_no_new_open:
            # 开关：解锁后绝不新开今仓（金融期货平今高手续费规避）
            self.ev.write("unlock_no_new_open",
                          key=sig.key, unlocked=v, want=want, skipped=new_lots,
                          note="unlock_no_new_open=true：跳过缺口补开，仅解锁")
            new_lots = 0

        # 容量守卫：补开是"再开一笔持仓"，簿容量不足 1 笔则整笔不补（不能开半笔）
        headroom = max(0, self.positions.max_positions - len(self.positions))
        if new_lots > 0 and headroom < 1:
            self.ev.write("max_open_cap", key=sig.key,
                          requested=new_lots, effective=0,
                          book_n=len(self.positions),
                          book_max=self.positions.max_positions,
                          note="解锁后补开被簿容量截断（簿已满，无法再开一笔）")
            new_lots = 0

        if new_lots > 0:
            # （2026-09-08：原 RiskGate.check_open 补开风控检查已随五道硬闸门删除；
            #   原 PositionSizing 仓位计算已随固定手数精简删除；补开手数 = N - 已解锁 V，
            #   由"手数直接取 max_volume"决定，仅余簿容量守卫约束。）
            pass

        self.ev.write("unlock_result", key=sig.key, unlocked=v, want=want,
                      new_open=new_lots,
                      action=("with_new_open" if new_lots > 0 else "pure_unlock"))

        if new_lots > 0:
            # 补开：state 由 _open_position 推进（成交→IN_TRADE / 拒单→IDLE）
            self._open_position(sig, side, new_lots)
        else:
            self._state = EngineState.IDLE

        # signal_action 统一出口（覆盖 _open_position 写的 opened）
        self.store.update_signal_action(
            sig.key, "unlock",
            "unlocked={}/want={}/new_open={}".format(v, want, new_lots))
        self._persist()

    def _book_unlock_trade(self, sig: Signal, target: Position, o: Order) -> Trade:
        """UNLOCK 成交记帐（Trade 落盘 + risk 登记 + 从簿删除 + unlock 事件）。

        乐观语义：submit 返回 filled 即记帐 —— 真实未成交（回报丢失）由解锁复核
        （_check_unlock_stuck）兜底：快照重建回簿；Trade 记录保留作审计。
        """
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
        # （2026-09-08：原 RiskGate.on_trade_closed 当日统计已随五道硬闸门删除。）
        self.positions.remove(target)

        self.ev.write("unlock", symbol=t.symbol, side=str(t.side),
                      entry=t.entry_price, unlock=o.filled_price,
                      gross=t.gross_points, cost=t.cost_points,
                      net=t.net_points, cash=t.net_cash, bars_held=bars_held,
                      trade_id=t.trade_id, unlock_signal_key=sig.key,
                      unlock_order_id=o.order_id,
                      entry_mode=target.entry_mode.value)
        return t

    # ---------------- 离场方式（Phase D 硬规则） ----------------
    @staticmethod
    def _exit_intent(pos: Position) -> "Tuple[OrderIntent, Side]":
        """按 pos.entry_mode 联动决定离场方式（不留配置开关）。

        OPEN_FIRST   → LOCK_SOFT  → OrderIntent.LOCK  + 反向 side
                                          （开反向同手数；CTP OpenCloseType=Open）
        UNLOCK_FIRST → CLOSE_HARD → OrderIntent.CLOSE + pos.side
                                          （平昨无费率问题；CTP OpenCloseType=CloseToday / CloseAny）
        LOCKED（Phase H1） → OrderIntent.UNLOCK + pos.side
                                          （锁仓的唯一合法离场 = 解锁，CloseYesterday。
                                           settle 已跳过 LOCKED，本分支纯防御；
                                           若同日被触发，CTP 会拒"平昨"——预期行为，
                                           防止锁仓当日走平今逃费路径。）

        LOCK 在引擎视角是"软离场"——调用 broker.submit(OPEN, opposite, ...) 让 broker 真的
        去开反向仓；引擎把当前 Position 视为已了结（pos=None），Trade 仍按 trigger_price
        结算（cost 用 close_today_first 路径 = LOCK 替代平今的成本等价）。
        Phase H1 起，LOCK 成交后 broker 端真实存在的反向仓由引擎落簿
        （entry_mode=LOCKED，见 _close_positions 内 lock_booked 事件），
        次日对向信号经 on_signal 的 has_opposite 门自动触发 UNLOCK。
        """
        if pos.entry_mode is EntryMode.LOCKED:
            # Phase H1：锁仓只能解锁（CloseYesterday）。防御分支，正常路径不可达。
            return OrderIntent.UNLOCK, pos.side
        if pos.entry_mode is EntryMode.UNLOCK_FIRST:
            return OrderIntent.CLOSE, pos.side
        # OPEN_FIRST 或 默认 → LOCK_SOFT
        opposite = Side.SHORT if pos.side is Side.LONG else Side.LONG
        return OrderIntent.LOCK, opposite

    # ════════════════════════════════════════════════════════════════
    # Phase I1（2026-09-06）：自动下单关闭（前端开关 → 进程托管触发）
    #   关闭语义（用户拍板）：
    #     ① auto_order_enabled=False → on_signal 顶部拒收所有买卖点信号
    #     ② _lock_remaining_positions → 簿内所有「未锁定」持仓全部 LOCK
    #        （无论 OPEN_FIRST 还是 UNLOCK_FIRST 入场，关闭一律锁仓；
    #         LOCK 成交后由 _close_positions 落簿反向 LOCKED 仓，次日对向
    #         信号经 has_opposite 门自动 UNLOCK —— 与正常 LOCK 完全同管线）
    #   幂等性：重复关闭只对仍未锁定的持仓补锁；簿内只剩 LOCKED 时无操作。
    #   状态：auto_order_enabled 持久化（_persist），重启保持关闭语义。
    # ════════════════════════════════════════════════════════════════
    def _lock_remaining_positions(self, bar: Optional[Bar] = None,
                                  reason: str = "auto_order_off") -> None:
        """把簿内所有未锁定持仓锁仓（关闭语义 ② 的执行体）。

        on_bar 关闭态下每根 K 线调用一次：上次锁仓被拒（cooldown 期）的
        残留持仓会在 cooldown 结束后自动补锁，直到簿内只剩 LOCKED。
        """
        remaining = [p for p in self.positions.positions
                     if p.entry_mode is not EntryMode.LOCKED]
        if not remaining:
            return
        ref_price = 0.0
        if bar is not None and bar.close:
            ref_price = bar.close
        elif self.last_bar is not None and self.last_bar.close:
            ref_price = self.last_bar.close
        self._close_positions(remaining, reason, ref_price, bar,
                              signal_key="auto_order_off", force_lock=True)

    def shutdown_and_lock_all(self, reason: str = "auto_order_off") -> None:
        """自动下单关闭入口（main.py 收到退出信号时调用）。

        ① 停信号门（后续 SSE 推来的买卖点信号一律 skip）→
        ② 锁全部未锁定持仓 → ③ 持久化。幂等：重复调用安全。
        """
        self.auto_order_enabled = False
        self._lock_remaining_positions(reason=reason)
        self._persist()
        self.ev.write("auto_order_off",
                      reason=reason,
                      locked_n=sum(1 for p in self.positions.positions
                                   if p.entry_mode is EntryMode.LOCKED),
                      remaining_unlocked=sum(
                          1 for p in self.positions.positions
                          if p.entry_mode is not EntryMode.LOCKED),
                      note="停止接收信号 + 未锁定持仓已锁仓")

    def auto_order_status(self) -> Dict[str, Any]:
        """自动下单状态快照（供后端进程托管 / API / 前端轮询）。"""
        return {
            "enabled": self.auto_order_enabled,
            "state": self._state.value,
            "positions": [p.to_dict() for p in self.positions.positions],
        }

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
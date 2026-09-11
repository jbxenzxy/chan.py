# -*- coding: utf-8 -*-
"""
引擎状态机
==========
事件驱动，只处理两类事件：bar（K 线闭合）与 signal（缠论买卖点）。

账户三态（需求 ⑴）—— **唯一判据是净敞口**
    空仓态 FLAT     簿内无仓单
    运行态 RUNNING  净敞口 ≠ 0（Σ side.sign × volume），盈亏实时变动
    锁仓态 LOCKED   净敞口 = 0 但簿内有仓单（多空互锁），盈亏锁定

    判定收口在 `account_state()`（架构约束 A1）：除它之外，任何地方都不得再写
    第二处三态判定。`EngineState`（IDLE / IN_TRADE）只是它的派生镜像，供外部
    （API / 前端 / 旧测试）读取。

两种操作（需求 ⑵）—— 没有第三种
    OPEN  开仓：买信号 → 买开，卖信号 → 卖开
    CLOSE 平仓：买信号 → 买平，卖信号 → 卖平
    「锁仓 / 解锁」不是操作：它们与 OPEN / CLOSE 在 CTP 报文层完全等价，
    只是引擎的记账标签（2026-09-11 删除）。

转移表（需求 ⑷⑸⑹⑺）—— 全部集中在 `_decide_action` / `_decide_exit`
    ① 空仓 + 信号           → OPEN（信号方向）  → 成交后净敞口 ≠ 0 → 运行态
    ② 锁仓 + 信号 + 今仓    → OPEN（信号方向）  → 成交后净敞口 ≠ 0 → 运行态
    ③ 锁仓 + 信号 + 跨日仓  → CLOSE（信号方向） → 成交后净敞口 ≠ 0 → 运行态
    ④ 运行 + 出场 + 今仓    → OPEN（净敞口反方向）→ 成交后净敞口 = 0 → 锁仓态
    ⑤ 运行 + 出场 + 跨日仓  → CLOSE（净敞口方向） → 成交后 → 锁仓态 或 空仓态

    「今仓 / 跨日」判据 = `positions.latest().entry_date` 是否等于当前交易日。
    只看最近一笔就够 —— 运行态 / 锁仓态下簿内仓单要么全是今仓、要么全是跨日仓，
    不可能混合（附录 A.3 归纳证明）。

一段运行（run）—— 风控锚的载体
    净敞口从 0 变非 0 的那一刻开启一段 run，用**该次成交价**作风控锚（D1）。
    L1-L3 只判这段 run，不逐笔判仓单 —— 这让「空仓做多」与「锁仓做多」的
    风控表现天然一致（场景 X / Y 等价，见分析文档 §4.7）。
    净敞口回到 0（转移 ④⑤）时 run 结束。

每根 K 线的处理顺序（顺序错了结果就错了）
    ① 结算已有持仓 —— 用**刚闭合**的 K 线的 high/low 判止盈止损 L1-L3
    ② 才处理落在这根 K 线上的信号 —— 决定开仓
    反过来会变成"同一根 K 线内既开仓又平仓"，是回测里最常见的作弊来源。

两处时序防护（踩过才知道）
    ① 入场那根 K 线不能参与出场判定：信号在 K 线 T 闭合时产生、按 T 的收盘价开仓，
       若结算也用 T 的高低点，等于开仓瞬间就可能"被止损"。
       因此结算时跳过 bar.timestamp <= run.entry_bar_ts 的 K 线。
    ② 重复/回退的 bar 直接丢弃：SSE 可能重发，或断线重连后补发历史帧。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
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
    PLAUSIBLE_DATE_MIN,
    AccountState,
    Bar, DecisionType, EngineState, ExitPlan, Order, OrderIntent,
    Position, Side, Signal, Trade, now_cn, now_ms, trading_day_from_clock,
    trading_day_of_ms,
)


def _opposite(side: Side) -> Side:
    """反方向。"""
    return Side.SHORT if side is Side.LONG else Side.LONG


def _oldest(cands: List[Position]) -> Optional[Position]:
    """建仓最早的一笔（FIFO）。空列表 → None。"""
    return min(cands, key=lambda p: p.entry_bar_seq) if cands else None


@dataclass
class _Action:
    """一次报单动作（转移 ①~⑤ 的产物）。纯数据，便于单测与审计。

    side 的语义随 intent 变化：
      OPEN  → 开仓方向（多 = 买开，空 = 卖开）
      CLOSE → **被平仓单的方向**（多 = 卖平，空 = 买平）
    """

    intent: OrderIntent
    side: Side
    volume: int
    target: Optional[Position] = None   # CLOSE 的对冲目标（反向最早的一笔）
    is_exit: bool = False               # 是否离场动作（决定 broker 追不追价）
    transition: int = 0                 # 转移编号 1..5，仅用于审计


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

        # 持仓簿：按建仓时间先后排列的仓单序列，**没有配对概念**。
        # 不设笔数上限（D2）—— 资金是唯一闸门（钱不够自然开不成功，由柜台拒单兜底）。
        self.positions: PositionBook = PositionBook()
        # Phase A：4 态引擎状态机
        #   IDLE     无持仓，等待入场信号
        #   OPENING  正在开仓（瞬态：下单到成交之间）
        #   IN_TRADE 已持仓，等待离场条件
        #   EXITING  正在离场（瞬态：下单到成交之间）
        # 状态转移由 _execute（唯一报单出口）→ _book_open / _book_close 落账，
        # 再由 _sync_state 按净敞口统一刷新；_reconcile_positions 负责对账纠偏。
        # 2026-09-11 重构后 _state 是 account_state() 的派生镜像，不再有独立口径。
        # Phase B 信号门按此状态决定是否接收新信号、是否触发离场。
        self._state: EngineState = EngineState.IDLE
        self.last_bar: Optional[Bar] = None
        self.bars_seen: int = 0
        self._trade_seq = 0
        # ════════════════════════════════════════════════════════════════
        # 运行态（run）风控状态 —— 需求 ⑴「运行态」在本引擎里的载体
        #   净敞口从 0 变非 0 的那一刻开启一段 run，用**该次成交价**作风控锚
        #   （D1 拍板）。止盈止损 L1-L3 只判这段 run，不逐笔判仓单 —— 这样
        #   「空仓做多」与「锁仓做多」的风控表现天然一致（场景 X / Y 等价）。
        #   净敞口回到 0（转移 ④⑤）时 run 结束。
        #
        #   ⚠️ run_anchor 是**风控锚**，不是会计锚：Trade 的盈亏仍按被平仓单
        #      自己的 entry_price 计（锁仓期间盈亏挂原仓不兑现，两者本就不等）。
        # ════════════════════════════════════════════════════════════════
        self._run_side: Optional[Side] = None
        self._run_anchor: float = 0.0
        self._run_volume: int = 0
        self._run_bar_ts: int = 0
        self._run_bar_seq: int = 0
        self._run_signal_key: str = ""
        self._run_plan: Optional[ExitPlan] = None
        # 上一次报单被拒的原因（供调用方写 signal_action）。每次 _execute 开头清空。
        self._last_reject: str = ""
        # ════════════════════════════════════════════════════════════════
        # CLOSE 冷却与「连续被拒」兜底
        #   ⚠️ 这两项在 Phase 4 重写引擎核心时**漏迁**（配置项留着、判定没了），
        #      Phase 6 按新分层重新落地：判定挂在 `_execute` 这一唯一报单出口上。
        #
        #   为什么必须冷却：broker 内部一笔 CLOSE 已按 `close_max_chase` 轮追价
        #     （最坏 = 20 组报单/撤单）。引擎若每根 bar 再补一笔，15s 周期下就是
        #     每分钟 80 组报撤单 —— 直接顶中金所「频繁报撤单」监管计数（风险 R13）。
        #   冷却口径是**根数**（bars_seen 序号差），与周期、与时间戳单位都无关
        #     （旧代码曾拿毫秒时间戳差值去比"5 根" → 实际 5 毫秒，冷却从未生效）。
        #   豁免：关闭自动下单（shutdown）路径不走冷却 —— 那是用户当面点下的收尾
        #         动作，不值得为省报撤单额度让它等。
        #
        #   兜底：连续被拒到 `close_max_streak` 次 → 认定该仓在柜台不存在（幻影），
        #     从簿里清掉，并升级为**严重告警**（D11）叫人核对实盘。
        #   ⚠️ 冷却只作用于 CLOSE；转移 ④ 的反向 OPEN 不追价、也无冷却。
        # ════════════════════════════════════════════════════════════════
        self._last_close_failed_bar_seq: int = 0
        self._close_fail_streak: int = 0
        self._close_retry_bars: int = cfg.engine.close_retry_bars    # 失败后冷却多少根 bar 再试
        self._close_max_streak: int = cfg.engine.close_max_streak    # 连续被拒多少次后清幻影仓
        # ════════════════════════════════════════════════════════════════
        # 运行期 run 风控锚自检（2026-09-12 补，G2 的运行期对等护栏）
        #   G2 只在**启动期**拒「有净敞口但无 run」；运行期对账 / 卡单复核 /
        #   连拒清仓三处改簿都可能让净敞口 0→非 0 而绕过 `_run_start`（run 的
        #   唯一开启点）→ 这里补一道自检，否则 L1-L3 **静默**失效。
        #   `_run_ready`：恢复流程走完才允许自检（`_restore` 中途的 `_sync_state`
        #   会看到"有敞口、run 还没恢复"的瞬时假象）。
        #   `_run_missing_notified`：同一次异常只报一次（D11 队列本就按 code 合并，
        #   这里是挡**事件日志**刷屏 —— 每根 bar 一条会把真正的离场信号淹没）。
        # ════════════════════════════════════════════════════════════════
        self._run_ready: bool = False
        self._run_missing_notified: bool = False
        # ════════════════════════════════════════════════════════════════
        # CLOSE 卡单检测（原 UNLOCK 卡单检测，2026-09-11 随 UNLOCK 概念一并改名）
        #   问题：CLOSE 报单后 broker 返回 filled，但 CTP 通道异常时真实未成交；
        #         引擎若直接信 filled 删掉仓单，就变成"簿面已平、实盘仍有仓"。
        #   方案：成交落账后记 `_close_in_flight`，若干 bars 后调
        #         broker.trade_confirmed(CLOSE, key) 二次确认：
        #           · True  → 真成交，清 in-flight
        #           · False → 按真实持仓兜底（见 Reconcile._check_close_stuck）
        #   dry_run 的 trade_confirmed 恒 True → 不触发，行为零变化。
        # ════════════════════════════════════════════════════════════════
        self._close_in_flight: Optional[Dict[str, Any]] = None
        # dict = {"signal_key": str, "target_signal_key": str, "target_side": str,
        #         "target_snapshot": dict, "submit_bar_ts": int, "submit_bar_seq": int}
        self._close_stuck_bars: int = cfg.engine.close_stuck_bars
        # ════════════════════════════════════════════════════════════════
        # D11 告警队列（Phase 6）
        #   引擎是告警的**生产者**，但用户看到它走的是前端 5s 轮询 → 打的是 API
        #   进程；而 API 进程与引擎是**两个进程**（App/AppTrader 托管子进程），
        #   不共享内存。所以队列落 state.db 的 kv：引擎写 `alerts`，API 层读它、
        #   并把用户确认的水位回写 `alerts_ack_ts`；引擎在下一次落盘时读到水位后
        #   把已确认条目清出。两侧都以 state.db 为唯一事实源，不需要额外 IPC。
        #   ⚠️ 必须落盘的另一个理由：资金不足 / 非交易时段这类故障，用户往往随后
        #      就重启进程去处理；只留在内存里的告警等于没告过。
        # ════════════════════════════════════════════════════════════════
        self._alerts: List[Dict[str, Any]] = []
        self._alerts_ack_ts: float = 0.0
        # ════════════════════════════════════════════════════════════════
        # 自动下单开关
        #   True  = 正常接收买卖点信号并交易（默认）
        #   False = 关闭语义（用户拍板，2026-09-11 确认保留）：
        #       ① 不再接收买卖点信号（on_signal 顶部门，见 auto_order_off skip）
        #       ② 运行态（净敞口 ≠ 0）持仓按规则 ⑹ 离场一次：
        #          今仓 → 反向 OPEN（变锁仓），跨日仓 → CLOSE
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

    # ════════════════════════════════════════════════════════════════
    # 账户三态（需求 ⑴）—— 架构约束 A1 的**唯一**实现点
    #   判据只有净敞口，与"这笔仓是怎么来的"完全无关：
    #     net != 0          → RUNNING（运行态）
    #     net == 0 且簿空   → FLAT（空仓态）
    #     net == 0 且簿非空 → LOCKED（锁仓态）
    #
    #   历史口径（已删）：曾按"持仓来源标记"判定，需要维护 origin / lock_pair_id
    #   两套记账才能回答"现在是不是锁着" —— 那正是把问题绕复杂的根源。
    #   净敞口是交易所与柜台的唯一事实，本地不需要第二套说法。
    #
    #   ⚠️ 除本方法外，任何地方都不得再写第二处三态判定（含 `if net == 0`）。
    # ════════════════════════════════════════════════════════════════
    def account_state(self) -> AccountState:
        """账户三态判定（唯一实现点）。详见 `AccountState` 文档。"""
        if self.positions.net_volume() != 0:
            return AccountState.RUNNING
        if self.positions.is_empty():
            return AccountState.FLAT
        return AccountState.LOCKED

    # ════════════════════════════════════════════════════════════════
    # 当前交易日（SSOT，2026-09-10 立）
    #   规则 ⑷⑸⑹⑺ 的 today 口径**唯一实现点**。此前 2 处各自现算：
    #     on_signal : sig.date[:10] if sig.date else ""
    #     离场      : bar.date[:10] if bar.date else now_cn()[:10]
    #   前者用**信号自然日**、后者用**bar 自然日**，都建立在"格式化字符串"上 ——
    #   既与建仓端 entry_date 的口径可能漂移，又无法处理夜盘（夜盘成交属于次一交易日）。
    #   收口到 _day_of_anchor() + _current_trading_day()。
    # ════════════════════════════════════════════════════════════════
    @staticmethod
    def _day_of_anchor(anchor) -> str:
        """单个时间锚（Bar / Signal）→ 所属【交易日】'YYYY-MM-DD'。取不到 → ''。

        一个锚上有两个**表达同一事实**的时间副本（都源自 chan.py 的 `klu.time`）：
          · timestamp：毫秒时间戳 —— 权威，且是**唯一**能正确处理夜盘的来源
            （夜盘 21:00 的成交属于次一交易日；用自然日会差一天 → 误判今仓为昨仓
             → 发 CLOSE 平今 → CTP 拒单）。
          · date：格式化字符串 —— timestamp 的展示副本。

        可信度递减：timestamp 能派生出可信日期（>= PLAUSIBLE_DATE_MIN）就用它；
        否则用 date 字符串。**两者都取不到才返回 ''**（由调用方 fail-fast）。
        降级分支服务于"timestamp 不是真实毫秒"的数据（历史补录 / 测试夹具用序号），
        生产路径（App 侧 int(ts*1000)）永远走 timestamp 分支。
        """
        if anchor is None:
            return ""
        d = trading_day_of_ms(int(getattr(anchor, "timestamp", 0) or 0))
        if d and d >= PLAUSIBLE_DATE_MIN:
            return d
        return trading_day_from_clock(str(getattr(anchor, "date", "") or ""))

    def _current_trading_day(self, bar: Optional[Bar] = None) -> str:
        """当前所属【交易日】。恒返回非空 'YYYY-MM-DD'。

        来源优先级：调用方 bar → self.last_bar → 墙钟 now_ms()。
        墙钟兜底保证返回值非空，故下游**不再需要**"today 为空"的防御分支
        （旧实现里 on_signal 的 `if today and ...` 在 today 为空时会走错分支，
        而离场路径早有 now_cn() 兜底 —— 两处口径本就不一致）。
        """
        for anchor in (bar, self.last_bar):
            d = self._day_of_anchor(anchor)
            if d:
                return d
        return trading_day_of_ms(now_ms())

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

    # ────────────────────────────────────────────────────────────────
    # 启动闸门（Phase 5 收口）—— 共同口径：**宁可启动不了，也不带半新半旧的状态跑**。
    #   旧状态一旦被新口径解释，错误会一路跑到报单且不可逆（典型是 phantom 清仓：
    #   簿面清空但实盘仍有仓）。故三道闸门一律 fail-fast：
    #
    #     G1 旧 schema 闸门（本段）    持仓记录带已删除的来源键 / kv 残留旧键 → 拒
    #     G2 run 闸门（_restore_run）  净敞口≠0 却无 run、run 与净敞口方向矛盾 → 拒
    #     G3 时间锚闸门（_restore）    entry_date 三源全空 → 拒
    #
    #   ⚠️ 用户拍板（2026-09-11）：旧库一律**严格拒绝**，不做"自动剥离 + 继续跑"的
    #      兼容迁移。理由：旧记录的持仓语义（锁仓仓算不算敞口）与当前"只看净敞口"
    #      的口径不同，自动迁移只是把账实不符从启动期推迟到报单期。
    # ────────────────────────────────────────────────────────────────
    # 持仓记录曾用 `entry_mode` 键标记来源（后改名 `origin`），2026-09-11 随
    # "来源"概念连同 `lock_pair_id` / `exit_mode` 一并删除。带这些键的库由旧版本
    # 写入 —— 直接恢复会让引擎对已对冲的仓发平仓单（账实不符）。
    _LEGACY_POSITION_KEYS = ("entry_mode", "origin", "lock_pair_id", "exit_mode")
    # kv 表同样可能残留旧键（旧版本把配对序号 / 解锁在途标记写在 kv 上）。
    # 它们已经没有读取方了，留着只说明"这份库是旧版本写的"。
    _LEGACY_KV_KEYS = ("lock_pair_seq", "unlock_in_flight")

    @staticmethod
    def _legacy_position_records(records) -> list:
        """挑出仍含旧键的持仓记录（纯函数，便于单测）。"""
        return [d for d in records if isinstance(d, dict)
                and any(k in d for k in TradingEngine._LEGACY_POSITION_KEYS)]

    def _reject_legacy_state(self, records) -> None:
        """G1：检出旧 schema（持仓记录 / kv 残留键）→ 抛错拒绝启动。"""
        legacy = self._legacy_position_records(records)
        if legacy:
            keys = "/".join(self._LEGACY_POSITION_KEYS)
            syms = sorted({str(d.get("symbol") or "?") for d in legacy})
            self.ev.write("state_schema_incompatible",
                          legacy_key=keys, legacy_n=len(legacy), symbols=syms,
                          note="旧 schema 持仓记录，拒绝恢复，避免锁仓持仓被误判为敞口持仓")
            raise RuntimeError(
                "state.db 的持仓记录仍是旧 schema（含 {} 键），与当前代码不兼容：\n"
                "  受影响合约：{}（共 {} 条）\n"
                "  旧记录用「来源标记」区分锁仓仓与敞口仓，而当前口径只看净敞口，\n"
                "  直接恢复会让引擎对已对冲的仓发平仓单（账实不符）。\n"
                "  处理：确认账户无未了结持仓后，删除 Trading/State/state.db 再启动。"
                .format(keys, ", ".join(syms), len(legacy)))
        stale = [k for k in self._LEGACY_KV_KEYS
                 if self.store.get_json(k) is not None]
        if stale:
            self.ev.write("state_schema_incompatible",
                          legacy_key="/".join(stale), legacy_n=len(stale),
                          note="kv 表残留旧 schema 键")
            raise RuntimeError(
                "state.db 的 kv 表残留旧 schema 键（{}），与当前代码不兼容：\n"
                "  这些键随 2026-09-11「来源 / 配对 / 解锁」重构一并删除，\n"
                "  库里还有它们说明这份库由旧版本写入，其余状态同样不可信。\n"
                "  处理：确认账户无未了结持仓后，删除 Trading/State/state.db 再启动。"
                .format("/".join(stale)))


    def _restore(self) -> None:
        # Phase E1：优先读新版 "positions" list（多仓），回退到老版 "position" 单字段。
        # 老数据库无 "positions" 键时也能恢复，且不破坏现有迁移路径。
        # v1.3（Q5 拍板）：restore 不再用 cfg 容量截断 —— 不限容量，恢复永不丢失持仓（解 D3）。
        restore_max = None
        # v1.4（切合约隔离）：只恢复当前 trade_symbol 的持仓。切换合约（如 IF→IM）后
        # 旧合约持仓留在 state.db 不加载进簿，避免 _restore 末尾的 _reconcile_positions
        # 把旧合约持仓当成「外部平仓」误清（PnL 还会按新合约 spec 算，全错）。
        # 旧合约持仓由 _persist 的分片合并继续保留在库里，切回原合约时可恢复。
        my_symbol = self.spec.trade_symbol
        pd_list = self.store.get_json("positions")
        # 旧 schema 闸门：改名前写入的记录用 entry_mode 键，必须显式处理（见上方注释）
        self._reject_legacy_state(
            pd_list if isinstance(pd_list, list) else [self.store.get_json("position")])
        if isinstance(pd_list, list):
            own = [d for d in pd_list
                   if isinstance(d, dict) and d.get("symbol") == my_symbol]
            new_book = PositionBook.from_dict(own, max_positions=restore_max)
            self.positions.replace_with(new_book)
        else:
            pd = self.store.get_json("position")
            if isinstance(pd, dict) and pd.get("symbol") == my_symbol:
                new_book = PositionBook(max_positions=restore_max)
                new_book.set_legacy(Position.from_dict(pd))
                self.positions.replace_with(new_book)

        # ════════════════════════════════════════════════════════════════
        # F2（2026-09-10）：恢复后仍无 entry_date 的持仓 → 拒绝启动。
        #   entry_date 已在 Position.__post_init__（F4）里尽力重建，顺序为
        #     entry_bar_ts（建仓 K 线的毫秒时间戳 —— 权威来源，旧库必定有它，
        #                   因为 entry_bar_ts 比 entry_date 更早引入）
        #     entry_at    （墙钟字符串，兜底）
        #   三者全空 = 这条记录**真的不含任何时间信息**，"今仓/昨仓"无从判定 →
        #   规则 ⑸ 必然判错 → 对今仓发 CLOSE 平今 → CTP 拒单 → 连锁 phantom 清仓
        #   （簿面清空但实盘仍有仓，不可逆）。此处宁可拒绝启动，也不猜方向。
        #   注意：这是"真无解"的脏数据，与旧实现把 "" 静默当"昨仓"是两回事 ——
        #   后者让错误一路跑到报单，前者在启动期就把它挡住。
        # ════════════════════════════════════════════════════════════════
        unresolved = [p for p in self.positions.positions if not p.entry_date]
        if unresolved:
            self.ev.write(
                "position_entry_date_unresolved",
                n=len(unresolved),
                signal_keys=[p.signal_key for p in unresolved],
                entry_bar_ts=[p.entry_bar_ts for p in unresolved],
                entry_at=[p.entry_at for p in unresolved],
                note="持仓无任何可用时间锚（entry_date / entry_bar_ts / entry_at 全空），"
                     "无法判定今仓/昨仓，拒绝启动")
            raise RuntimeError(
                "state.db 有 {} 笔持仓记录里 entry_date / entry_bar_ts / entry_at "
                "三个时间源全空，无法判定「今仓 / 昨仓」，拒绝启动。\n"
                "  原因：规则 ⑸ 用 entry_date 决定离场走 LOCK（今仓：反向开仓锁仓）\n"
                "        还是 CLOSE（昨仓：平仓）。判错会对今仓发平今 CLOSE → 中金所\n"
                "        拒单 → 连续拒单触发 phantom 清仓（簿面清空但实盘仍有仓）。\n"
                "  处理：核对实盘持仓后，删除 Trading/State/state.db 再启动。\n"
                "  受影响持仓：{}".format(
                    len(unresolved), [p.signal_key for p in unresolved]))

        # E3.1：截断 warning —— persisted 多仓数据超出 cfg max 时丢了一些仓。
        # v1.3：不限容量下 truncated 恒为空；保留本段仅为"若将来恢复有限容量"时
        # 不再静默丢失持仓（写 error 级事件），且 avoid None 参与算术。
        truncated = self.positions.truncated_on_restore
        if truncated:
            self.ev.write(
                "positions_truncated_on_restore",
                reason="cfg_max_smaller_than_persisted",
                cfg_max=restore_max,
                persisted_n=len(truncated) + (restore_max or len(self.positions)),
                kept_n=restore_max,
                dropped_keys=[p.signal_key for p in truncated],
            )
        # E1：写"start"事件只记簿大小/首个位置摘要，不调 legacy_single（多仓会抛）。
        # 这是观察日志，不是引擎逻辑，规避守护报错即可。
        if not self.positions.is_empty():
            p0 = self.positions.positions[0]
            self.ev.write("start", restored_position=p0.to_dict(),
                          positions_n=len(self.positions))
        # 初始 state 由持仓派生：运行态（净敞口 ≠ 0）→ IN_TRADE，否则 → IDLE。
        # 空仓与锁仓都归 IDLE —— 两者都在"等交易信号"，区别由 account_state 表达。
        self._sync_state()
        self.bars_seen = int(self.store.get_json("bars_seen", 0) or 0)
        # ════════════════════════════════════════════════════════════════
        # R1（2026-09-10）：三个持久 ID 的序号跨重启恢复。
        #   症状（实测）：`_trade_seq` 是**进程内计数器**，
        #     `_persist` / `_restore` 都不碰它们 → 重启归零 → `trade_id` 从
        #     T00001 重来 → 与库内既有记录相撞 → 旧 `INSERT OR REPLACE` 把上一
        #     进程的成交流水**静默覆盖**（实测：2 笔独立成交落库只剩 1 条）。
        #   根因不是"计数器这个方案不可靠"，而是**漏接持久化** —— 同性质的
        #     `bars_seen` 早就走 set_json/get_json 了（实测跨重启 7 → 7）。
        #   修法（与 bars_seen 同构，不新增表、不改 schema）：
        #     ① `_persist` 把序号写 kv；
        #     ② 恢复取 max(kv 值, 库内数据推导值) —— 双保险：kv 丢了（换库 /
        #        被外部清理）也能从真实数据反推出已用过的最大号；
        #     ③ broker 的报单序号同理，从 `orders` 表抬升到 max 之上。
        #   为什么不改成"时间戳 ID"：实测同一毫秒内批量离场会生成相同毫秒，
        #     墙钟回拨后也会
        #     与历史号重复 —— 症状和计数器一模一样，只是触发条件换了。单调
        #     计数器不吃墙钟，且可读性/可续号性都更好。
        #   同时不动 `entry_bar_seq`：它已持久化（bars_seen）且语义是"根数"
        #     （`bars_held = bars_seen - entry_bar_seq`），换算成时间戳要除以
        #     周期 → 引入周期依赖，与 `Engine.py` 注释里刻意保持的"与周期无关"相悖。
        # ════════════════════════════════════════════════════════════════
        self._trade_seq = max(int(self.store.get_json("trade_seq", 0) or 0),
                              self.store.max_trade_seq())
        fn_seed = getattr(self.broker, "seed_order_seq", None)
        if callable(fn_seed):
            fn_seed(max(int(self.store.get_json("order_seq", 0) or 0),
                        self.store.max_order_seq(self.broker.name)))
        # 恢复 CLOSE 卡单标记 —— 接续上次崩前的卡单检测，
        # 让 _check_close_stuck 在余下 bar 进度下继续推进复核。
        fl = self.store.get_json("_close_in_flight")
        if isinstance(fl, dict):
            self._close_in_flight = fl
        # Phase 5 G5：恢复的卡单标记可能与当前 bar 进度不自洽（自愈规则见方法注释）
        self._validate_close_in_flight()
        # D11：恢复未确认告警（severe 落库的目的就是"重启后还在"）
        self._load_alerts()
        # 运行态风控状态（run）：净敞口 ≠ 0 时必须有 run，否则 L1-L3 无从判定。
        self._restore_run()

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
        # 恢复流程到此结束 —— 之后才允许运行期 run 自检（见 `__init__` 的
        # `_run_ready` 注释：`_restore` 中途的 `_sync_state` 会看到瞬时假象）。
        self._run_ready = True

    def _check_run_anchor(self, source: str) -> None:
        """运行期 run 风控锚自检 —— G2 的运行期对等护栏（2026-09-12 补）。

        缺口：`_run_start` 全仓唯一开启点是 `_execute` 的成交落账分支，而净敞口
        有**三条路径不经过 `_execute`** 直接改簿：
          · 对账删仓（`Reconcile._reconcile_positions` 的 `positions.remove`）
          · CLOSE 卡单复核重建（同文件的 `positions.add`）
          · 连拒达上限清幻影仓（`_note_close_rejected` 的 `positions.remove`）
        三处都只补了「净敞口 → 0」的收口边，**0 → 非 0 那条边全缺** → 净敞口
        非 0 却没有 run → `_settle_positions` 在 `_run_view() is None` 时直接
        return，**L1-L3 静默失效且不写事件不发告警**（实测复现）。

        本方法**只告警、不建锚**（保守方案）：`_run_plan` 必须靠
        `exit_policy.plan(sig, ...)` 生成，而这三条路径共同缺的就是信号形态数据，
        建锚只能靠猜 —— 猜出来的止损价可能比裸奔更危险。故先把它**显性化**：
        写事件 + 发 severe 告警，下一次重启会被 G2 直接拒绝启动。
        """
        if not self._run_ready:
            return
        if self.account_state() is not AccountState.RUNNING:
            # 空仓 / 锁仓没有净敞口，本就不需要 run —— 顺手复位通知锁，避免
            # 「告警过一次 → 平仓 → 再开仓」之后不再提示。
            self._run_missing_notified = False
            return
        if self._run_side is not None and self._run_plan is not None:
            self._run_missing_notified = False
            return
        if self._run_missing_notified:
            return                      # 同一次异常只报一次，避免事件日志刷屏
        self._run_missing_notified = True
        net = self.positions.net_volume()
        self.ev.write("run_state_missing_runtime",
                      source=source, net_volume=net,
                      positions_n=len(self.positions.positions),
                      run_side=str(self._run_side),
                      note="净敞口 ≠ 0 但没有风控锚（run），L1-L3 已失效："
                           "成因是「{}」改簿后净敞口 0→非 0，而 run 只在 "
                           "`_execute` 成交时开启".format(source))
        self.alert(
            self.ALERT_SEVERE, "run_missing_anchor",
            "检测到净敞口 {:+} 手但没有风控锚（run），L1-L3 止损止盈已失效。"
            "成因：{} 使净敞口从 0 变成非 0，而 run 只在报单成交时开启。"
            "当前敞口处于**无止损**状态，请人工核对柜台持仓并考虑手动平仓；"
            "重启引擎会被启动闸门（G2）拒绝，直到该问题解决。".format(net, source),
            net_volume=net)

    def _persist(self) -> None:
        # Phase E1：双写兼容 —— 新键 "positions"（list）保留扩展空间，
        # 旧键 "position"（单字段）继续写以做审计 / 旧流程回归。
        # 多仓时旧键取 positions[0] —— 是为了保留"看一眼持仓是哪个合约"的旧 API，
        # 不是引擎主入口（主入口走 self.positions）。legacy_single() 在多仓会抛错
        # 是有意的早期守护 E3，这里规避它。
        # v1.4（切合约隔离）：positions 按 trade_symbol 分片写回 —— 只覆盖当前合约的
        # 持仓，保留库里其它合约的持仓，切走再切回时能恢复管理。旧键 "position" 优先
        # 写当前合约首仓，否则退回其它合约首仓（仅供审计「看一眼是哪个合约」）。
        my_symbol = self.spec.trade_symbol
        existing = self.store.get_json("positions")
        existing_list = existing if isinstance(existing, list) else []
        others = [d for d in existing_list
                  if isinstance(d, dict) and d.get("symbol")
                  and d.get("symbol") != my_symbol]
        mine = self.positions.to_dict() if not self.positions.is_empty() else []
        merged = others + mine
        if merged:
            self.store.set_json("positions", merged)
            self.store.set_json("position", mine[0] if mine else others[0])
        else:
            self.store.delete_key("positions")
            self.store.delete_key("position")
        self.store.set_json("bars_seen", self.bars_seen)
        # R1：三个持久 ID 的序号落 kv（跨重启不复用）。见 _restore 的 R1 段。
        #   order_seq 是 broker 端计数器的当前值 —— 不落的话，只要中途没有
        #   新报单，重启后 broker 计数器会回到旧值，下一条委托号就可能撞上
        #   库内既有号（R2 之后会直接抛 IdCollisionError）。
        self.store.set_json("trade_seq", int(self._trade_seq))
        fn_seq = getattr(self.broker, "order_seq", None)
        if callable(fn_seq):
            self.store.set_json("order_seq", int(fn_seq()))
        # 持久化 CLOSE 卡单标记 —— 引擎崩 / 重启后 _restore 才能接续复核。
        if self._close_in_flight is not None:
            self.store.set_json("_close_in_flight", self._close_in_flight)
        else:
            self.store.delete_key("_close_in_flight")
        # 运行态风控状态（run）：跨重启保持 L1-L3 的风控锚与出场计划。
        self._persist_run()
        # P6-F 补全（2026-09-12）：CLOSE 冷却落 kv。子进程架构下 API 侧读的是
        # kv 而不是 `auto_order_status()`（后者只被测试调用），不落 kv 则前端
        # tooltip 的"平仓冷却剩余"永远读不到 —— 前端只能看到"点了没动静"。
        self.store.set_json("close_cooldown", {
            "active": self._in_close_cooldown(),
            "bars_left": self._close_cooldown_bars_left(),
            "streak": self._close_fail_streak,
        })
        # Phase I1：持久化自动下单开关（跨重启保持关闭语义）
        self.store.set_json("auto_order_enabled", self.auto_order_enabled)
        # D11：告警队列 + ack 水位（顺带吃掉 API 层已确认的条目）
        self._persist_alerts()

    # ════════════════════════════════════════════════════════════════
    # 运行态（run）持久化
    #   run 是"净敞口 ≠ 0 时由谁接风控"的唯一答案，必须与持仓簿一起跨重启。
    #   丢失它 = L1-L3 不知道该拿什么价当锚 —— 要么止损远在天边，要么开仓即触发。
    # ════════════════════════════════════════════════════════════════
    _RUN_KV = "run"

    def _persist_run(self) -> None:
        if self._run_plan is None or self._run_side is None:
            self.store.delete_key(self._RUN_KV)
            return
        self.store.set_json(self._RUN_KV, {
            "side": self._run_side.name,
            "anchor": self._run_anchor,
            "volume": self._run_volume,
            "bar_ts": self._run_bar_ts,
            "bar_seq": self._run_bar_seq,
            "signal_key": self._run_signal_key,
            "plan": self._run_plan.to_dict(),
        })

    def _restore_run(self) -> None:
        """G2：恢复 run，并校验它与净敞口自洽。三种不自洽，处理各不相同：

        · **有敞口、无 run** → 拒绝启动。run 是 L1-L3 的风控锚，缺它等于让敞口
          在没有止损的状态下运行（与 entry_date 三源全空那道闸门同口径）。
        · **有 run、无敞口** → 清掉残留（不是错误）。典型成因：进程在 `_run_end`
          之前崩了，或对账把仓清了。这段 run 对当前状态没有任何约束力，
          留着只会让 `_persist` 把它写回，脏数据永远清不掉。
        · **run 方向与净敞口符号矛盾** → 拒绝启动。这说明库被外部改过或版本
          不兼容，继续跑会让 L1-L3 把止损判在错误的方向上（越亏越不止损）。
        """
        d = self.store.get_json(self._RUN_KV)
        st = self.account_state()
        if isinstance(d, dict) and d.get("side") in ("LONG", "SHORT"):
            if st is not AccountState.RUNNING:
                self.ev.write("run_state_stale_cleared",
                              run_side=str(d.get("side")),
                              net_volume=self.positions.net_volume(),
                              positions_n=len(self.positions),
                              note="净敞口为 0 但 state.db 仍有运行态记录（run），"
                                   "判定为上一段的残留，本次启动丢弃")
                self._run_reset()
                # 同步删库：否则这段孤儿 run 会一直躺在 kv 里，每次启动重复清一遍
                self.store.delete_key(self._RUN_KV)
                return
            side = Side[d["side"]]
            net = self.positions.net_volume()
            if (side is Side.LONG and net < 0) or (side is Side.SHORT and net > 0):
                self.ev.write("run_state_contradiction",
                              run_side=str(side), net_volume=net,
                              positions_n=len(self.positions),
                              note="运行态方向与净敞口方向矛盾，拒绝启动")
                raise RuntimeError(
                    "state.db 的运行态（run）方向与净敞口方向矛盾，拒绝启动。\n"
                    "  run 记录的是 {} 方向，而簿内净敞口为 {:+} 手（{}）。\n"
                    "  继续跑会让 L1-L3 把止损判在错误的方向上（越亏越不止损）。\n"
                    "  出现本错误说明：state.db 由旧版写入，或被手工/第三方工具改过。\n"
                    "  处理：确认账户无未了结持仓后，删除 Trading/State/state.db 再启动。"
                    .format(side.name, net, "净多" if net > 0 else "净空"))
            self._run_side = side
            self._run_anchor = float(d.get("anchor") or 0.0)
            self._run_volume = int(d.get("volume") or 0)
            self._run_bar_ts = int(d.get("bar_ts") or 0)
            self._run_bar_seq = int(d.get("bar_seq") or 0)
            self._run_signal_key = str(d.get("signal_key") or "")
            self._run_plan = ExitPlan.from_dict(d.get("plan") or {})
            return
        if st is not AccountState.RUNNING:
            return          # 空仓态 / 锁仓态不需要 run
        self.ev.write("run_state_missing",
                      net_volume=self.positions.net_volume(),
                      positions_n=len(self.positions),
                      note="净敞口 ≠ 0 但 state.db 没有运行态记录（run），"
                           "L1-L3 无风控锚可用，拒绝启动")
        raise RuntimeError(
            "state.db 缺少运行态（run）记录，但有未平净敞口，拒绝启动。\n"
            "  run 记录 L1-L3 的风控锚与出场计划；缺它等于让敞口在没有止损的状态下运行。\n"
            "  出现本错误说明：state.db 由旧版写入，或被手工/第三方工具改过。\n"
            "  处理：确认账户无未了结持仓后，删除 Trading/State/state.db 再启动。")

    def _validate_close_in_flight(self) -> None:
        """G5：恢复的 CLOSE 卡单标记若与当前 bar 进度不自洽 → 丢弃。

        标记里的 `submit_bar_seq` 是落账那一刻的 `bars_seen`。若它比当前的
        `bars_seen` 还大（库被清过 / bars_seen 被重置 / 换过库文件），
        `_check_close_stuck` 算出的 `bars_elapsed` 恒为负 → 永远小于
        `close_stuck_bars` → **二次确认复核永不触发**，标记永久悬挂。
        宁可丢掉（最坏是少做一次复核），也不要留一个永不生效的死标记。
        """
        rec = self._close_in_flight
        if not isinstance(rec, dict):
            self._close_in_flight = None
            return
        seq = int(rec.get("submit_bar_seq") or 0)
        if seq > self.bars_seen:
            self.ev.write("close_in_flight_dropped_stale",
                          signal_key=str(rec.get("signal_key") or ""),
                          submit_bar_seq=seq, bars_seen=self.bars_seen,
                          note="卡单标记的 bar 序号超前于当前进度，"
                               "丢弃以避免二次确认复核永不触发")
            self._close_in_flight = None
            # 同步删库：否则死标记会一直躺在 kv 里，每次启动重复判一遍
            self.store.delete_key("_close_in_flight")

    def _sync_state(self) -> None:
        """`_state` 是账户三态的**派生镜像**（IN_TRADE / IDLE），供外部读取。

        它不是独立状态机：真值恒等于 `account_state()`，此处只做一次投影，
        避免外部（API / 前端 / 旧测试）各自去判三态。

        2026-09-12：这里顺带挂一次 run 风控锚自检 —— `_sync_state` 是所有改簿
        路径（成交落账 / 拒单记账 / 对账 / 卡单复核）的共同收口点，挂在这里比在
        三处各写一遍更不容易漏。见 `_check_run_anchor` 的文档串。
        """
        self._state = (EngineState.IN_TRADE
                       if self.account_state() is AccountState.RUNNING
                       else EngineState.IDLE)
        self._check_run_anchor("runtime")

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
        # CLOSE 卡单复核
        #   CLOSE 报单 N bars 后未确认 → 按真实持仓兜底（重建或清理）
        #   必须在 _reconcile_position 之前调用，否则 reconcile 清掉残留持仓后
        #   无法识别"CLOSE 卡单"与"普通外部平仓"的差异
        # ════════════════════════════════════════════════════════════════
        self._check_close_stuck(bar)

        # 持仓对账（增强 B）：与券商真实持仓比对。若发现持仓已被外部平掉
        # （如用户在快期3手工平仓）或属幽灵持仓，立即修正引擎账目，
        # 避免继续傻等平仓 / 误判新信号。dry_run 等无真实账户的通道返回 None，跳过。
        # 判空用 not positions.is_empty()，不用 self.position（多仓下 legacy_single 会抛）
        if not self.positions.is_empty():
            self._reconcile_position()

        # 只有运行态（净敞口 ≠ 0）才谈得上"离场"：
        #   空仓态无仓可平；锁仓态净敞口为 0、盈亏已锁定，等交易信号（规则 ⑶）。
        if self.account_state() is AccountState.RUNNING:
            if self.auto_order_enabled:
                self._settle_positions(bar)
            else:
                self._force_exit(bar, reason="auto_order_off_retry")

    def _settle_positions(self, bar: Bar) -> None:
        """出场判定（L1-L3）—— 运行态唯一的"正常离场"入口（规则 ⑹）。

        判的是**这一段 run**（净敞口），不是逐笔仓单：
          · 风控锚 = 净敞口从 0 变非 0 那次的成交价（`_run_anchor`）；
          · 出场计划挂在引擎上（`_run_plan`），不挂在某笔仓单上。
        这样"空仓做多"与"锁仓平空后转多"用的是同一份计划 —— 场景 X / Y 等价。

        触发后交给 `_force_exit`（转移 ④ 或 ⑤，按最近一笔仓单的建仓交易日决定）。
        """
        if self.account_state() is not AccountState.RUNNING:
            return
        run = self._run_view()
        if run is None:
            return
        # 入场那根 K 线不参与出场判定：信号在 K 线 T 闭合时产生、按 T 的收盘价成交，
        # 若结算也用 T 的高低点，等于刚建仓就可能"被止损"。
        if bar.timestamp <= run.entry_bar_ts:
            return

        bars_held = max(0, self.bars_seen - run.entry_bar_seq)
        check: Optional[ExitCheck] = self.exit_policy.check_with(
            run, bar, self.spec, bars_held=bars_held)
        if check is None:
            return

        if check.plan is not None:
            self._run_plan = check.plan
        if check.only_update:
            # 只更新计划（保本 / 跟踪位移），不触发离场
            self._persist()
            self.ev.write("exit_plan_update", reason=check.reason,
                          stop=self._run_plan.stop_price,
                          tp=self._run_plan.tp_price,
                          symbol=run.symbol,
                          position_signal_key=run.signal_key)
            return

        self._force_exit(bar, reason=check.reason, trigger_price=check.price)


    # ---------------- signal 事件 ----------------
    def on_signal(self, sig: Signal) -> None:
        """交易信号入口。规则 ⑶：运行态不响应信号，出场只由 L1-L3 负责。"""
        if not self.store.try_mark_signal(sig.key, "processing"):
            self.ev.write("signal_dup", key=sig.key, date=sig.date,
                          type=sig.bsp_type, is_buy=sig.is_buy,
                          prev_action=self.store.signal_action(sig.key))
            return

        self.ev.write("signal", key=sig.key, date=sig.date, type=sig.bsp_type,
                      is_buy=sig.is_buy, price=sig.price, high=sig.high, low=sig.low)

        # 自动下单关闭门：幂等键照常消费（防重放），但不进入任何交易决策。
        if not self.auto_order_enabled:
            self.store.update_signal_action(sig.key, "skip", "auto_order_off")
            self.ev.write("signal_skip", key=sig.key, reason="auto_order_off")
            return

        # 下单瞬态（同步 broker 下不可达，留给二期异步 broker）
        if self._state in (EngineState.OPENING, EngineState.EXITING):
            self.store.update_signal_action(
                sig.key, "in_flight", "state={}".format(self._state.value))
            self.ev.write("signal_in_flight", key=sig.key,
                          state=self._state.value,
                          reason="engine_busy_opening_or_exiting")
            return

        today = self._day_of_anchor(sig) or self._current_trading_day()
        act = self._decide_action(sig, today)
        if act is None:
            self.store.update_signal_action(
                sig.key, "skip", "running_ignore_signal")
            self.ev.write("signal_skip", key=sig.key,
                          reason="running_ignore_signal")
            return

        if act.transition == 1:
            # 空仓开新仓：先过入场策略的三道过滤
            decision = self.entry_policy.decide(sig, None, self.spec)
            if not decision:
                self.store.update_signal_action(sig.key, "skip", decision.reason)
                self.ev.write("signal_skip", key=sig.key, reason=decision.reason)
                return

        o = self._execute(act, ref_price=sig.price, bar=self.last_bar, sig=sig,
                          reason="signal")
        if o is None:
            self.store.update_signal_action(
                sig.key, "rejected", self._last_reject or "rejected")
            self._sync_state()
            return
        self.store.update_signal_action(
            sig.key, "opened" if act.intent is OrderIntent.OPEN else "closed",
            "transition={}/lots={}".format(act.transition, act.volume))

    # ════════════════════════════════════════════════════════════════
    # 决策 / 执行层（架构约束 A1 + A3）
    #   A1：三态与转移动作只在 `account_state` / `_decide_action` /
    #       `_decide_exit` 三个方法里判定，别处不许再写 `if net == 0` 之类
    #       的内联判定 —— 二期加品种护栏时不用回头找"还有哪里判了三态"。
    #   A3：`_execute` 是全引擎**唯一**的报单出口，前面挂 `_pre_trade_check`
    #       校验链（二期 Q1 全品种 / Q2 平今开关 / Q3 交割月的护栏都往这条链上加，
    #       不碰转移表结构）。
    # ════════════════════════════════════════════════════════════════
    def _decide_action(self, sig: Signal, today: str) -> Optional["_Action"]:
        """交易信号到达时的动作决策（转移 ①②③）。**纯函数：不改任何状态。**"""
        st = self.account_state()
        if st is AccountState.RUNNING:
            return None                                    # 规则 ⑶
        if st is AccountState.FLAT:
            # 转移 ①：空仓 → OPEN（信号方向），一笔挂 lots_per_signal 手
            return _Action(OrderIntent.OPEN, sig.side, self.lots_per_signal,
                           None, is_exit=False, transition=1)

        # 锁仓态：看最近一笔仓单的建仓交易日（D_last）
        latest = self.positions.latest()
        if latest is None or latest.entry_date >= today:
            # 转移 ②：当日锁 → OPEN（信号方向），不动锁仓仓单
            return _Action(OrderIntent.OPEN, sig.side, self.lots_per_signal,
                           None, is_exit=False, transition=2)

        # 转移 ③：跨日锁 → CLOSE（信号方向）
        #   买信号 = 买平 = 平掉空头仓；卖信号 = 卖平 = 平掉多头仓。
        #   对冲目标 = 持仓序列中**反向最早**的一笔（附录：仓单无配对，纯 FIFO）。
        side = _opposite(sig.side)
        target = self.positions.oldest_opposite(sig.side)
        if target is None:
            # 簿非空却找不对冲目标 —— 锁仓态必是双向，走到这里说明簿被外力改过。
            # 不猜、不开新仓，写事件后放弃本信号。
            self.ev.write("signal_no_close_target", key=sig.key, today=today,
                          note="锁仓态下找不到可对冲的反向仓单，放弃本信号")
            return None
        return _Action(OrderIntent.CLOSE, side,
                       min(self.lots_per_signal, target.volume),
                       target, is_exit=False, transition=3)

    def _decide_exit(self, bar: Optional[Bar] = None) -> Optional["_Action"]:
        """离场动作决策（转移 ④⑤）—— 运行态唯一出场口径。**纯函数。**"""
        net = self.positions.net_volume()
        if net == 0:
            return None
        latest = self.positions.latest()
        if latest is None:
            return None
        net_side = Side.LONG if net > 0 else Side.SHORT
        if latest.entry_date >= self._current_trading_day(bar):
            # 转移 ④：今日仓 → 反向 OPEN（净敞口归零，进入锁仓态）
            return _Action(OrderIntent.OPEN, _opposite(net_side), abs(net),
                           None, is_exit=True, transition=4)
        # 转移 ⑤：跨日仓 → CLOSE（净敞口方向），对冲目标 = 同向最早一笔
        target = _oldest([p for p in self.positions.positions
                          if p.side is net_side])
        if target is None:
            return None
        return _Action(OrderIntent.CLOSE, net_side,
                       min(abs(net), target.volume), target,
                       is_exit=True, transition=5)

    def _pre_trade_check(self, act: "_Action", today: str,
                         sig: Optional[Signal] = None) -> Optional[str]:
        """报单前校验链（A3）。返回 None = 通过；返回字符串 = 拒绝原因。

        ⚠️ 二期（Q1 全品种 / Q2 平今开关 / Q3 交割月护栏）一律往这条链上加，
        不要去改 `_decide_action` / `_decide_exit` 的转移表结构。
        """
        if act.volume <= 0:
            return "zero_volume"
        if not today:
            return "no_trading_day"
        if act.intent is OrderIntent.OPEN:
            # 建仓必须能确定"建仓所属交易日"：entry_date 是规则 ⑷⑸ 判
            # 「今仓 → 反向开仓 / 跨日 → 平仓」的唯一依据，空着就是非法状态。
            if not self._open_time_anchor(sig)[1]:
                return "no_time_anchor"
            return None
        # ── CLOSE ──
        if act.target is None:
            return "close_without_target"
        if not act.target.entry_date:
            return "close_target_no_entry_date"
        # ★ 全交易所安全性硬约束（契约测试 p35）：CLOSE 的目标必须是跨日仓。
        #   中金所没有平今指令，对今仓发 CLOSE 会被当平昨处理并按平今收费
        #   （0.0345%，是平昨的 15 倍）；上期所 / 能源中心则需要 CLOSETODAY。
        #   规则 ⑷⑸⑹⑺ 保证正常路径不会产生这种报单，这里是最后一道闸。
        if act.target.entry_date >= today:
            return "close_target_is_today"
        if act.volume > act.target.volume:
            return "close_volume_exceeds_target"
        # 2026-09-12 补（对称）：`act.volume < target.volume` 同样是账实不符 ——
        # broker 只平掉 `act.volume` 手，而 `_book_close` 是按 `pos.volume`
        # **整笔**记 Trade 并整笔 `positions.remove` 的（它拿不到"实际平了多少"）。
        # 触发场景：跨会话调小 `risk.max_volume`（如 4→2）后带旧仓重启，转移 ③⑤
        # 的 `min(lots_per_signal, target.volume)` 就会算出"只平一部分"。
        # 不猜、不做部分平仓（PositionBook 无减仓 API），直接拒绝并叫人处理。
        if act.volume < act.target.volume:
            return "close_volume_below_target"
        return None

    def _open_time_anchor(self, sig: Optional[Signal]) -> "Tuple[int, str]":
        """OPEN 的建仓时间锚：（K 线毫秒时间戳，建仓交易日）。

        优先级：last_bar（成交所在 K 线）→ sig（信号 K 线，与建仓 K 线同源）。
        两者都取不到 → 返回 (0, "")，由 `_pre_trade_check` 拒绝建仓。
        """
        ts = int(self.last_bar.timestamp) if self.last_bar else 0
        if ts <= 0 and sig is not None:
            ts = int(sig.timestamp or 0)
        d = (self._day_of_anchor(self.last_bar)
             or (self._day_of_anchor(sig) if sig is not None else ""))
        return ts, d

    def _execute(self, act: "_Action", ref_price: float,
                 bar: Optional[Bar] = None, sig: Optional[Signal] = None,
                 reason: str = "", force: bool = False) -> Optional[Order]:
        """【A3 唯一报单出口】提交一笔委托并落账。

        force: 绕过 CLOSE 冷却（只有"关闭自动下单"的收尾路径用 —— 用户当面点下的
               动作不值得为省报撤单额度等冷却）。

        一次信号 / 一次离场 = 一笔报单（需求 ⑷）。成交后按 intent 分两条落账路径：
          OPEN  → 簿内新增一笔仓单
          CLOSE → 记一笔 Trade + 移除被对冲的那笔仓单
        落账后按**净敞口变化**开启 / 结束 run（风控锚 = 本次成交价，D1）。
        """
        self._last_reject = ""
        today = self._current_trading_day(bar)
        why = self._pre_trade_check(act, today, sig)
        if why is not None:
            self._last_reject = why
            self.ev.write("order_rejected",
                          key=(sig.key if sig is not None else ""),
                          reason=why, transition=act.transition,
                          intent=act.intent.value, volume=act.volume,
                          note="报单前校验未通过，未向柜台发出任何委托")
            return None

        # CLOSE 冷却（Phase 6）：同一笔平仓刚被拒过就先别再砸单 —— 免得每根
        # bar 都往柜台发一笔（broker 内部每笔已经追过 close_max_chase 轮）。
        if (act.intent is OrderIntent.CLOSE and not force
                and self._in_close_cooldown()):
            self._last_reject = "close_cooldown"
            # 2026-09-12 补：冷却拦截原本**直接 return、不写任何事件**，与上方
            # 前置校验失败会写 `order_rejected` 不对称。后果是"这根 bar 为什么没
            # 补单"在事件日志里完全不可见（前端 tooltip 又因为 kv 里没有
            # `close_cooldown` 而读不到）→ 运维侧彻底无感知。
            # 注意：这里写的是"跳过"而不是"拒单" —— 冷却期内**没有**向柜台发出
            # 任何委托，不能混进 `order_rejected`（那会让拒单统计与 R13 计数失真）。
            self.ev.write("close_retry_skipped",
                          key=(sig.key if sig is not None
                               else (act.target.signal_key
                                     if act.target is not None else "")),
                          transition=act.transition,
                          bars_left=self._close_cooldown_bars_left(),
                          streak=self._close_fail_streak,
                          note="CLOSE 冷却期内未报单（未向柜台发出委托），"
                               "冷却结束后自动重试")
            return None

        # 报单的审计键：优先用信号键。离场动作没有信号时从被平仓单 / 本段 run
        # 派生 —— 空键会让多笔仓单在 state.db 与事件日志里无法区分。
        if sig is not None:
            okey = sig.key
        elif act.target is not None:
            okey = act.target.signal_key + "#exit"
        else:
            okey = (self._run_signal_key or "manual") + "#lock"
        o = self.broker.submit(
            act.intent, act.side, act.volume, ref_price, okey,
            note=reason or "transition_{}".format(act.transition),
            entry_date=(act.target.entry_date if act.target is not None else ""),
            is_exit=act.is_exit)
        # ── 审计补全（2026-09-11 Phase 7）──────────────────────────────
        # OrderIntent 由 4 值收敛为 2 值（OPEN / CLOSE）后，`orders` 表里
        # **① 开新仓**与**④ 反向开仓锁仓**都记成 intent="open"，单看这一行
        # 无法区分"主动建仓"与"离场触发的锁仓"。旧版靠 intent="lock" 区分，
        # 那个值没了 → 审计信息净损失。
        # 这里在报单出口统一补回：把 is_exit 与 transition 一并写进 meta。
        # 放在引擎侧而不是各 broker 内部，是为了**所有通道口径一致**
        # （dry_run / simnow / live 三处不必各写一遍，也不会漏一个）。
        o.meta["is_exit"] = bool(act.is_exit)
        o.meta["transition"] = int(act.transition)
        self.store.save_order(o)
        self.ev.write("order", order_id=o.order_id, action=o.action,
                      intent=o.meta.get("intent", act.intent.value),
                      side=str(o.side), volume=o.volume, price=o.price,
                      req_price=o.req_price, status=o.status, broker=o.broker,
                      reason=reason, transition=act.transition,
                      is_exit=act.is_exit,
                      target_signal_key=(act.target.signal_key
                                         if act.target is not None else ""))

        if o.status != "filled" or o.filled_price is None:
            why = o.meta.get("reject_reason") or o.status
            self._last_reject = why
            self.ev.write("order_rejected",
                          key=(sig.key if sig is not None else ""),
                          order_id=o.order_id, action=act.intent.value,
                          reason=why, transition=act.transition,
                          volume=act.volume, is_exit=act.is_exit,
                          reject_class=o.meta.get("reject_class", ""))
            # D11：把拒单升级成用户可见的告警（D10 已判出"追不追得动"）
            self._alert_on_reject(act, o, why)
            if act.intent is OrderIntent.CLOSE:
                self._note_close_rejected(act, o)
            self._sync_state()
            return None

        if act.intent is OrderIntent.CLOSE:
            # 2026-09-12 补：配置语义是"**连续**被拒 N 次清幻影仓"，而此前只有
            # "达上限"时才清零 —— 成功 CLOSE 不清零 → 变成"**累计**被拒 N 次"：
            # 一次拒单 + 中间若干笔正常成交 + 再一次拒单会跨 run 累积到阈值，
            # 把引擎自己刚开出来的**真仓**当幻影清掉（实测：见 test_p42 [1]）。
            self._close_fail_streak = 0
        # ── 成交落账：净敞口的变化决定 run 的开启 / 结束 ──
        net_before = self.positions.net_volume()
        if act.intent is OrderIntent.OPEN:
            self._book_open(act, o, sig)
        else:
            self._book_close(act, o, reason)
        net_after = self.positions.net_volume()
        if net_before == 0 and net_after != 0:
            self._run_start(o.filled_price, bar, sig)
        elif net_before != 0 and net_after == 0:
            self._run_end()

        self._persist()
        self._sync_state()
        return o

    def _book_open(self, act: "_Action", o: Order,
                   sig: Optional[Signal]) -> None:
        """OPEN 成交 → 簿内新增一笔仓单（FIFO 追加）。

        出场计划不挂在仓单上（那是 run 的职责），这里只占位；
        仓单被平掉时由 `_book_close` 取 run 的计划名写进 Trade。
        """
        entry_ts, entry_date = self._open_time_anchor(sig)
        pos = Position(
            symbol=self.spec.trade_symbol, side=act.side, volume=act.volume,
            entry_price=o.filled_price, entry_at=now_cn(),
            entry_bar_ts=entry_ts, entry_bar_seq=self.bars_seen,
            signal_key=(sig.key if sig is not None else o.signal_key),
            open_order_id=o.order_id,
            exit_plan=ExitPlan(name="run_managed", stop_price=0.0),
            entry_date=entry_date)
        self.positions.add(pos)
        self.ev.write("open", symbol=pos.symbol, side=str(pos.side),
                      volume=pos.volume, entry_price=pos.entry_price,
                      entry_date=entry_date, order_id=o.order_id,
                      transition=act.transition, signal_key=pos.signal_key)

    def _book_close(self, act: "_Action", o: Order, reason: str) -> None:
        """CLOSE 成交 → 记一笔 Trade + 移除被对冲的那笔仓单。

        盈亏按**被平仓单自己的 entry_price** 计（会计锚），与 run 的风控锚
        （`_run_anchor`）是两回事 —— 锁仓期间盈亏挂原仓不兑现，两者本就不相等。
        """
        pos = act.target
        if pos is None:
            return
        exit_price = o.filled_price
        gross = pos.pnl_points(exit_price)
        # CLOSE 恒作用于跨日仓（`_pre_trade_check` 已断言）→ 恒按平昨费率计。
        # 这里仍按 entry_date 动态判定，是为"未来其它调用方"保留防御。
        cost = self.spec.cost_points(
            pos.entry_price, exit_price,
            close_today=bool(self.spec.close_today_first
                             and pos.entry_date >= self._current_trading_day()))
        net = gross - cost
        cash = self.spec.points_to_cash(net, pos.volume)
        bars_held = max(0, self.bars_seen - pos.entry_bar_seq)
        plan = self._run_plan or pos.exit_plan
        self._trade_seq += 1
        t = Trade(
            trade_id="T{:05d}".format(self._trade_seq), symbol=pos.symbol,
            side=pos.side, volume=pos.volume, entry_price=pos.entry_price,
            exit_price=exit_price, entry_at=pos.entry_at, exit_at=now_cn(),
            reason=reason, gross_points=round(gross, 4),
            cost_points=round(cost, 4), net_points=round(net, 4),
            net_cash=round(cash, 2), bars_held=bars_held,
            signal_key=pos.signal_key, exit_plan_name=plan.name,
            exit_plan_params=plan.params)
        self.store.save_trade(t)
        self.positions.remove(pos)
        self.ev.write("close", symbol=t.symbol, side=str(t.side), reason=reason,
                      entry=t.entry_price, exit=t.exit_price,
                      gross=t.gross_points, cost=t.cost_points,
                      net=t.net_points, cash=t.net_cash, bars_held=bars_held,
                      trade_id=t.trade_id, exit_policy=t.exit_plan_name,
                      transition=act.transition,
                      position_signal_key=pos.signal_key)
        # CLOSE 卡单检测：落账即挂 in-flight，若干 bars 后复核真实成交
        # （防"broker 说成交了、CTP 其实没成交"的账实不符）。
        self._close_in_flight = {
            "signal_key": o.signal_key,
            "target_signal_key": pos.signal_key,
            "target_side": pos.side.name,
            "target_snapshot": pos.to_dict(),
            "submit_bar_ts": (self.last_bar.timestamp if self.last_bar else 0),
            "submit_bar_seq": self.bars_seen,
        }

    # ---------------- 运行态（run）----------------
    def _run_start(self, anchor_price: float, bar: Optional[Bar],
                   sig: Optional[Signal]) -> None:
        """开启一段 run（净敞口 0 → 非 0）。**风控锚 = 本次成交价**（D1）。"""
        net = self.positions.net_volume()
        if net == 0 or sig is None:
            # sig 缺失时无法生成出场计划（plan() 需要信号的形态数据）。
            # 不猜一个锚 —— 写事件让它在运维侧可见。
            self.ev.write("run_start_incomplete", net=net,
                          has_signal=sig is not None,
                          note="缺少成交价/信号，无法建立运行态风控锚；"
                               "本次运行不参与 L1-L3，需人工介入")
            self.alert(
                self.ALERT_SEVERE, "run_start_incomplete",
                "净敞口已为 {:+} 手，但本段运行没有建立风控锚（缺成交价/信号），"
                "L1-L3 止盈止损对本段失效。请人工盯盘或手工处理。".format(net),
                net=net)
            return
        side = Side.LONG if net > 0 else Side.SHORT
        plan = self.exit_policy.plan(sig, anchor_price, self.spec,
                                     anchor=anchor_price)
        self._run_side = side
        self._run_anchor = anchor_price
        self._run_volume = abs(net)
        self._run_bar_ts = (bar.timestamp if bar is not None
                            else (self.last_bar.timestamp if self.last_bar else 0))
        self._run_bar_seq = self.bars_seen
        self._run_signal_key = sig.key
        self._run_plan = plan
        self.ev.write("run_start", side=str(side), volume=self._run_volume,
                      anchor=anchor_price, stop=plan.stop_price,
                      tp=plan.tp_price, exit_policy=plan.name,
                      signal_key=sig.key, entry_date=self._current_trading_day(bar))

    def _run_end(self) -> None:
        """结束一段 run（净敞口 → 0）。转移 ④⑤ 的收尾。"""
        self.ev.write("run_end", side=str(self._run_side),
                      anchor=self._run_anchor,
                      account_state=self.account_state().value)
        self._run_reset()

    def _run_reset(self) -> None:
        """清空 run 字段（不写事件）。

        与 `_run_end` 分开，供**非正常收尾**的场景使用（对账清仓、恢复期丢弃
        残留 run）—— 那些场景没有"一段 run 正常结束"的语义，写 `run_end`
        事件会让运维侧误以为真的发生了一次离场。
        """
        self._run_side = None
        self._run_anchor = 0.0
        self._run_volume = 0
        self._run_bar_ts = 0
        self._run_bar_seq = 0
        self._run_signal_key = ""
        self._run_plan = None

    def _run_view(self) -> Optional[Position]:
        """把当前 run 合成一笔"虚拟仓单"供 L1-L3 判定（**不进簿**）。

        run 的 volume = |净敞口|、entry_price = 风控锚，二者与簿内任何一笔真实
        仓单都不一定相等。L1-L3 只认 run —— 这正是"场景 X / Y 等价"的实现方式。
        """
        # 三态判定仍走 account_state()（A1），此处不自带第二套判定。
        if self.account_state() is not AccountState.RUNNING:
            return None
        if self._run_plan is None or self._run_side is None:
            return None
        return Position(
            symbol=self.spec.trade_symbol, side=self._run_side,
            volume=abs(self.positions.net_volume()),
            entry_price=self._run_anchor, entry_at="",
            entry_bar_ts=self._run_bar_ts, entry_bar_seq=self._run_bar_seq,
            signal_key=self._run_signal_key, open_order_id="",
            exit_plan=self._run_plan,
            entry_date=trading_day_of_ms(self._run_bar_ts))

    # ---------------- 离场执行（转移 ④⑤ 的执行体）----------------
    def _force_exit(self, bar: Optional[Bar], reason: str,
                    trigger_price: Optional[float] = None,
                    force: bool = False) -> None:
        """无条件离场。两个调用方走**同一张转移表**（规则 ⑹）：

          · `_settle_positions` —— L1-L3 触发，trigger_price = 触发价
          · 自动下单关闭       —— 无触发价，用最新收盘价

        今仓 → 反向 OPEN（转移 ④）；跨日仓 → CLOSE（转移 ⑤）。
        """
        act = self._decide_exit(bar)
        if act is None:
            return
        price = trigger_price
        if not price:
            # bar 为空时用 self.last_bar 兜底：否则 _execute 内的 today 会退化到
            # **墙钟**（回放 / 补锁场景下与真实交易日不同）→ 今仓被误判成昨仓。
            for anchor in (bar, self.last_bar):
                if anchor is not None and getattr(anchor, "close", 0):
                    price = float(anchor.close)
                    break
        self._execute(act, ref_price=float(price or 0.0), bar=bar,
                      reason=reason, force=force)

    # ════════════════════════════════════════════════════════════════
    # 自动下单关闭（前端开关 → 进程托管触发）
    #   关闭语义（用户拍板，2026-09-11 确认保留）：
    #     ① on_signal 顶部拒收所有买卖点信号
    #     ② 运行态持仓按【规则 ⑹】离场：今仓 → 反向 OPEN（锁仓），昨仓 → CLOSE
    #   幂等：重复关闭只对仍未离场的持仓补做；净敞口归零后无操作。
    #
    #   ⚠️ 关闭后账户的归宿 —— "冻结"语义（用户 2026-09-10 选择，2026-09-11 保留）：
    #     反向 OPEN 留下的锁仓（净敞口 0）**没有自动出口**：
    #       · on_signal 顶部直接 return → 等不到信号；
    #       · 锁仓态不参与 L1-L3，on_bar 也不再触发离场。
    #     两条人工出口：① 重新开启自动下单 + 出现对向信号（走转移 ③ 拆锁）；
    #     ② 在交易所手工平仓。净敞口为 0、PnL 不兑现，不是"危险状态"，
    #     所以只写 `account_frozen` 事件让它可见，不做自动处理。
    # ════════════════════════════════════════════════════════════════
    def shutdown_and_lock_all(self, reason: str = "auto_order_off") -> None:
        """自动下单关闭入口（main.py 收到退出信号时调用）。幂等：重复调用安全。"""
        self.auto_order_enabled = False
        # bar=None → 用 self.last_bar 兜底。两者都为空（进程启动后从未收到 K 线）
        # 时由 _current_trading_day 的墙钟兜底接手 —— 那必然是无 K 线驱动的
        # 托管关闭场景，墙钟交易日与真实交易日一致。
        # force=True：关闭是用户当面点下的收尾动作，不排队等冷却（否则刚被拒
        # 过一次平仓时，关闭会"看起来什么都没做"）
        self._force_exit(self.last_bar, reason=reason, force=True)
        self._persist()
        self._sync_state()
        self.ev.write(
            "auto_order_off", reason=reason,
            account_state=self.account_state().value,
            net_volume=self.positions.net_volume(),
            positions_n=len(self.positions),
            note="停止接收信号 + 运行态持仓已按规则 ⑹ 离场（今仓反向开仓 / 昨仓平仓）")
        if self.account_state() is AccountState.LOCKED:
            self.ev.write(
                "account_frozen", reason=reason,
                positions_n=len(self.positions), net_volume=0,
                note="关闭后账户停在锁仓态（净敞口 0）：引擎无自动清仓路径，"
                     "需人工平仓或重新开启自动下单后由对向信号拆锁")
            self.alert(
                self.ALERT_WARN, "account_frozen",
                "自动下单已关闭，账户停在锁仓态（净敞口 0，持有 {} 笔仓单、"
                "盈亏不再变动）。引擎无自动清仓路径：需人工平仓，或重新开启"
                "自动下单后由对向信号拆锁。".format(len(self.positions)),
                positions_n=len(self.positions))

    # ════════════════════════════════════════════════════════════════
    # D11 告警队列（Phase 6）
    #   触发源三类（分析文档 §5.2.4）：
    #     ① D10 判出的**不可挽救拒单** —— 资金不足 / 非交易时段 / 无权限 / 无此持仓
    #     ② 离场追价跑满 `close_max_chase` 仍未成交（价格不可达，追不动了）
    #     ③ CLOSE 连续被拒达 `close_max_streak` —— 兜底清幻影仓，同时叫人核对实盘
    #   判定都不在这里：①②由 broker 的 D10 分类器给结论（写在 Order.meta），
    #   ③在 `_note_close_rejected`；本段只负责**怎么存、怎么给前端**。
    # ════════════════════════════════════════════════════════════════
    ALERT_SEVERE = "severe"          # 前端 alert() 阻塞弹窗
    ALERT_WARN = "warn"              # 前端 toast 轻提示
    _ALERTS_KV = "alerts"
    _ALERTS_ACK_KV = "alerts_ack_ts"
    _ALERTS_KEEP = 20                # 队列上限：再多也只会淹没前端

    # CTP 拒单分类（D10）→ (告警码, 级别, 标题)
    #   · price（FOK 全撤 / 涨跌停）**不进这张表**：那是"追了有用"的一类，引擎会
    #     继续追；只有追满 close_max_chase 仍不成交才升级（见 `_alert_on_reject`）。
    _REJECT_ALERTS = {
        "funds": ("ctp_reject_funds", "severe",
                  "柜台拒单：资金 / 保证金不足"),
        "not_tradable": ("ctp_reject_not_tradable", "severe",
                         "柜台拒单：当前不可报单（非交易时段 / 无权限 / 无此持仓）"),
    }

    def alert(self, level: str, code: str, msg: str, **extra) -> Dict[str, Any]:
        """登记一条告警（D11）。同 code 未确认 → **合并计数**，不新增条目。

        合并而非追加的理由：一次通道故障会连着产生几十条同因告警，追加会把队列
        淹没，而前端每 5s 轮询一次 —— 每次都会判成"有新告警"。
        合并时**保留首次发生时刻 ts**，只推进 last_ts / n：前端按 ts 去重，
        因此同一次故障只弹一次框；用户确认后条目被移除，下次故障才是新 ts
        （于是重新弹一次 —— 这正好是"故障还在"的提醒节奏）。
        """
        now = time.time()
        for a in self._alerts:
            if a.get("code") == code:
                a["n"] = int(a.get("n") or 1) + 1
                a["last_ts"] = now
                a["msg"] = msg
                a.update(extra)
                self.ev.write("alert", level=level, code=code, n=a["n"],
                              msg=msg, merged=True)
                self._persist_alerts()
                return a
        rec: Dict[str, Any] = {"level": level, "code": code, "msg": msg,
                               "ts": now, "last_ts": now, "n": 1}
        rec.update(extra)
        self._alerts.append(rec)
        if len(self._alerts) > self._ALERTS_KEEP:
            self._alerts = self._alerts[-self._ALERTS_KEEP:]
        self.ev.write("alert", level=level, code=code, n=1, msg=msg, merged=False)
        self._persist_alerts()
        return rec

    def ack_alerts(self, ts: float = 0.0,
                   codes: Optional[List[str]] = None) -> int:
        """确认告警（D11）：确认过的条目不再下发。返回被移除的条数。

        两种口径（可同时给）：
          · ts    —— 水位线：确认该时刻（含）之前发生的全部告警。前端弹完一批框
                     后回它，队列因此有界、不随运行时长增长。
          · codes —— 只确认这几个 code。
        """
        before = len(self._alerts)
        if codes:
            want = {str(c) for c in codes}
            self._alerts = [a for a in self._alerts
                            if str(a.get("code")) not in want]
        if ts:
            ts = float(ts)
            self._alerts_ack_ts = max(self._alerts_ack_ts, ts)
            self._alerts = [a for a in self._alerts
                            if float(a.get("ts") or 0.0) > ts]
        removed = before - len(self._alerts)
        self.ev.write("alert_ack", removed=removed, ts=ts,
                      codes=list(codes or []), left=len(self._alerts))
        self._persist_alerts()
        return removed

    def _load_alerts(self) -> None:
        """恢复告警队列与 ack 水位（D11 落库的目的就是"重启后还在"）。"""
        self._alerts_ack_ts = float(
            self.store.get_json(self._ALERTS_ACK_KV, 0.0) or 0.0)
        raw = self.store.get_json(self._ALERTS_KV)
        items = raw if isinstance(raw, list) else []
        keep = [a for a in items if isinstance(a, dict) and a.get("code")]
        if self._alerts_ack_ts:
            keep = [a for a in keep
                    if float(a.get("ts") or 0.0) > self._alerts_ack_ts]
        self._alerts = keep[-self._ALERTS_KEEP:]

    def _persist_alerts(self) -> None:
        """把队列与 ack 水位写回 state.db，顺便吃掉别处写进来的水位。

        ⚠️ ack 水位每次**从库里现读**（不用内存副本）：ack 由 API 进程写入，
        引擎不接收 HTTP，只有重读才知道用户已确认 —— 否则引擎会把已确认的条目
        又写回去，前端就反复弹同一个框。
        """
        ack = float(self.store.get_json(self._ALERTS_ACK_KV, 0.0) or 0.0)
        self._alerts_ack_ts = max(self._alerts_ack_ts, ack)
        if self._alerts_ack_ts > ack:
            # 引擎自己 ack 过 → 水位必须落库。**不能只留在内存**：水位就是库里
            # 的一个键，不写回去，重启后已确认的告警会"复活"（自验 [2a]/[2d] 钉死）。
            self.store.set_json(self._ALERTS_ACK_KV, self._alerts_ack_ts)
        if self._alerts_ack_ts:
            self._alerts = [a for a in self._alerts
                            if float(a.get("ts") or 0.0) > self._alerts_ack_ts]
        if self._alerts:
            self.store.set_json(self._ALERTS_KV, self._alerts)
        else:
            self.store.delete_key(self._ALERTS_KV)

    def _alert_on_reject(self, act: "_Action", o: Order, why: str) -> None:
        """把一笔被拒的委托按 D10 分类升级成告警（D11 触发源 ①②）。"""
        cls_ = str(o.meta.get("reject_class") or "")
        info = self._REJECT_ALERTS.get(cls_)
        if info is not None:
            code, level, title = info
            self.alert(level, code,
                       "{} —— {} {} {} 手被拒：{}".format(
                           title, o.symbol, act.intent.value, str(o.side), why),
                       signal_key=o.signal_key, reject_class=cls_)
            return
        # 价格不可达 + 追价轮数跑满 → 这笔（离场）彻底没成，必须叫人。
        # 开仓不追价（is_exit=False），一次不成就是不成，没有"跑满"的语义。
        if (cls_ == "price" and act.is_exit
                and int(o.meta.get("attempt") or 0)
                >= int(o.meta.get("max_attempts") or 1)):
            self.alert(self.ALERT_SEVERE, "close_chase_exhausted",
                       "离场追价跑满 {} 轮仍未成交 —— {} {} {} 手，"
                       "净敞口没有变动，请在柜台人工处理。".format(
                           o.meta.get("max_attempts"), o.symbol,
                           act.intent.value, str(o.side)),
                       signal_key=o.signal_key, reject_class=cls_)

    # ── CLOSE 冷却 / 连续被拒兜底（Phase 6 恢复，见 __init__ 注释）──
    def _in_close_cooldown(self) -> bool:
        """是否处于 CLOSE 冷却期（根数口径：bars_seen 序号差）。"""
        if not self._last_close_failed_bar_seq:
            return False
        return (self.bars_seen - self._last_close_failed_bar_seq
                < self._close_retry_bars)

    def _close_cooldown_bars_left(self) -> int:
        """冷却剩余根数（前端 / 诊断用；不在冷却期为 0）。"""
        if not self._in_close_cooldown():
            return 0
        return max(0, self._close_retry_bars
                   - (self.bars_seen - self._last_close_failed_bar_seq))

    def _note_close_rejected(self, act: "_Action", o: Order) -> None:
        """CLOSE 被拒后的记账：进入冷却；连续被拒到上限 → 清幻影仓 + 严重告警。

        阈值口径与旧版一致（连续 `close_max_streak` 次被拒即认定幻影），差别只在
        "清谁"：新模型一次只平一笔（转移 ⑤ 的目标 = 同向最早一单），故只清这一笔，
        不再像旧版把整批一次清掉 —— 清得少一点，错了波及面就小一点；同一根 bar
        的 `_reconcile_positions` 也会独立判一遍幻影，两条路互为兜底。
        """
        self._last_close_failed_bar_seq = self.bars_seen
        self._close_fail_streak += 1
        self.ev.write("close_retry_cooldown",
                      key=o.signal_key, order_id=o.order_id,
                      reject=o.meta.get("reject_reason") or o.status,
                      cooldown_bars=self._close_retry_bars,
                      streak=self._close_fail_streak,
                      note="CLOSE 被拒，冷却 {} 根 bar 内不重复报单".format(
                          self._close_retry_bars))
        if self._close_fail_streak < self._close_max_streak:
            return
        target = act.target
        self._close_fail_streak = 0
        self._last_close_failed_bar_seq = 0
        if target is None or target not in self.positions.positions:
            return
        self.positions.remove(target)
        net = self.positions.net_volume()
        self.ev.write("position_drop", reason="close_repeatedly_rejected",
                      streak=self._close_max_streak,
                      signal_key=target.signal_key, symbol=target.symbol,
                      net_volume=net,
                      note="CLOSE 连续被拒达上限，认定该仓在柜台不存在，从簿中清除")
        self.alert(
            self.ALERT_SEVERE, "close_repeatedly_rejected",
            "平仓连续被拒 {} 次，已按兜底规则把 {} {} {} 手从簿中清除。"
            "请核对柜台真实持仓：若柜台仍有该仓，说明簿面已与实盘不一致，"
            "需人工平仓后重启引擎。".format(
                self._close_max_streak, target.symbol, str(target.side),
                target.volume),
            signal_key=target.signal_key)
        if net == 0:
            self._run_reset()
        self._persist()
        self._sync_state()

    def auto_order_status(self) -> Dict[str, Any]:
        """自动下单状态快照（供后端进程托管 / API / 前端轮询）。"""
        return {
            "enabled": self.auto_order_enabled,
            "state": self._state.value,
            # 需求 ⑴：对外暴露账户三态。此前只能拿到引擎过程态 EngineState
            # （IDLE 同时覆盖空仓与锁仓），"空仓 vs 锁仓"在前端不可见。
            "account_state": self.account_state().value,
            "net_volume": self.positions.net_volume(),
            "positions_n": len(self.positions),
            "run": (None if (self._run_plan is None or self._run_side is None)
                    else {
                        "side": self._run_side.name,
                        "anchor": self._run_anchor,
                        "volume": self._run_volume,
                        "stop": self._run_plan.stop_price,
                        "tp": self._run_plan.tp_price,
                        "name": self._run_plan.name,
                    }),
            "positions": [p.to_dict() for p in self.positions.positions],
            # D11：未确认告警（前端按 code 去重 + 5 分钟冷却后弹窗，确认后回 ack）
            "alerts": list(self._alerts),
            # CLOSE 冷却可见性：否则"为什么半天不补单"在前端完全看不见
            "close_cooldown": {
                "active": self._in_close_cooldown(),
                "bars_left": self._close_cooldown_bars_left(),
                "streak": self._close_fail_streak,
            },
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
            # G1：不用 self.position（legacy_single 在多仓会抛）；直接列全部持仓。
            "open_position": ([p.to_dict() for p in self.positions.positions] or None),
            "exit_policy": self.exit_policy.describe(),
            "entry_policy": self.entry_policy.describe(),
            "spec": {"signal_symbol": self.spec.signal_symbol,
                     "trade_symbol": self.spec.trade_symbol,
                     "price_tick": self.spec.price_tick,
                     "multiplier": self.spec.multiplier},
        }
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
    只是引擎的记账标签（删除）。

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
    风控表现天然一致（场景 X / Y 等价，见分析文档）。
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

from ..Broker.Base import (INTENT_TO_OFFSET, REJECT_POSITION,
                           REJECT_PRICE, Broker)
from ..Config import TradingConfig
from ..Infra.EventLog import EventLog
from ..Infra.Product import CLOSETODAY, ExecPolicy, Product, assert_product_allowed
from .PositionBook import PositionBook, PositionBookError
from .Reconcile import ReconcileMixin
from ..Infra.StateDB import Store
from ..Strategy.Exit import ExitCheck
from ..Infra.Instrument import Instrument
from ..Infra.Records import (BSP_TYPE_FILTER_KEY, AccountState, Bar,
                             DecisionType, EngineState, ExitPlan, Order,
                             OrderIntent, Position, Side, Signal, Trade,
                             is_closure_alert)
from ..Infra.Clock import PLAUSIBLE_DATE_MIN, now_cn, now_ms, trading_day_from_clock, trading_day_of_ms


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
                 entry_policy: "EntryPolicy", exit_policy: "LayeredExitPolicy",
                 store: Store, ev: EventLog,
                 state: Optional["Instrument"] = None):
        self.cfg = cfg
        # ── 合约运行时对象（双类合并）──
        #   原 self.state（静态规格 cfg.instrument）与 self.state（运行时状态）
        #   现在是**同一个 Instrument**：静态身份经 config/product 转发只读，
        #   有效值（SSOT=品种档案）/回填字段（verified/trade_symbol/last_trade_date）
        #   是运行时字段。
        # D-C：原 spec 兼容别名**已删除** —— 本类只有
        #   `self.state` 一个名字（与 Broker / Source 同名），原 spec 点号引用
        #   全部改读 `self.state.xxx`，不再"三个名字指同一块内存"。
        # 所有权（不变）：**一次运行只有一份 Instrument**。默认沿用 broker 的
        #   （不显式给时）；main.py 显式传入同一份只为可读性。第三档（原
        #   getattr 回落 + InstrumentState 自建）随合并消亡 ——
        #   Instrument 构造即完成档案取值，不存在"从部署配置播种"。
        self.state: "Instrument" = (
            state if state is not None
            else getattr(broker, "state", None)
            # 鸭子类型 broker（不继承 Broker、无 .state 的测试替身/外接通道）
            # 回落：由部署配置 + 品种档案现建一份（等价前"从 cfg 播种"）。
            or Instrument(cfg.instrument, cfg.product_profile))
        # 合约规格漂移校验只做一次（verified 首次为真时）：合约规格在一次
        # 会话内不会变，重复检查只会把同 code 告警的 n 刷大。
        self._spec_drift_checked: bool = False
        self.broker = broker
        self.entry_policy = entry_policy
        self.exit_policy = exit_policy
        self.store = store
        self.ev = ev
        # 单笔手数（收敛为**唯一来源**）：每个买卖点只开一笔，一笔挂
        # N 手，N = **品种执行策略表第 3 列**（见下方 `lots_per_order` property，
        # 每次实时读 `self.state.exec_policy`）。
        #   原"仓位管理 PositionSizer/SizingConfig"与后来的"风控层
        #   `risk.max_volume`"两条通道**均已删除** —— 手数不再是 __init__ 里算好
        #   的常量，引擎侧也**不再保留任何可被运行期覆盖的镜像属性**
        #   （旧 `self.lots_per_signal` 已删）。理由：两个旋钮会让"改表 N"不生效、
        #   而启动横幅只显示其中一个（用户拍板删 max_volume）。

        # 持仓簿：按建仓时间先后排列的仓单序列，**没有配对概念**。
        # 不设笔数上限（D2）—— 资金是唯一闸门（钱不够自然开不成功，由柜台拒单兜底）。
        self.positions: PositionBook = PositionBook()
        # 4 态引擎状态机
        #   IDLE     无持仓，等待入场信号
        #   OPENING  正在开仓（瞬态：下单到成交之间）
        #   IN_TRADE 已持仓，等待离场条件
        #   EXITING  正在离场（瞬态：下单到成交之间）
        # 状态转移由 _execute（唯一报单出口）→ _book_open / _book_close 落账，
        # 再由 _sync_state 按净敞口统一刷新；_reconcile_positions 负责对账纠偏。
        # 重构后 _state 是 account_state() 的派生镜像，不再有独立口径。
        # 信号门按此状态决定是否接收新信号、是否触发离场。
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
        #   ⚠️ run_anchor 兼任**会计锚**（run 级配对会计，设计文档 v3.1 §5.1）：
        #      Trade 的 entry = 本段 run 的入场 fill 价 —— 不是被平仓单自己的
        #      entry_price（拆锁场景下二者不同，那正是仓单级口径错误的根源）。
        # ════════════════════════════════════════════════════════════════
        self._run_side: Optional[Side] = None
        self._run_anchor: float = 0.0
        self._run_volume: int = 0
        self._run_bar_ts: int = 0
        self._run_bar_seq: int = 0
        self._run_signal_key: str = ""
        # run 级会计元数据：入场 fill 的 offset 档与成交时点。
        # entry_offset ∈ INTENT_TO_OFFSET 值域（SSOT @ Broker/Base.py），
        # 决定 Trade 入场费率档 —— 拆锁入场是平昨/平今档，不是开仓档。
        self._run_entry_offset: str = ""
        self._run_entry_at: str = ""
        self._run_plan: Optional[ExitPlan] = None
        # 上一次报单被拒的原因（供调用方写 signal_action）。每次 _execute 开头清空。
        self._last_reject: str = ""
        # ════════════════════════════════════════════════════════════════
        # 报单前校验被拒的**可见性**
        #   此前只有"柜台侧拒单"（broker 返回非 filled）会经 `_alert_on_reject`
        #   升级为告警；而**报单前校验链**（`_pre_trade_check`）拦下的拒单
        #   只写一条 `order_rejected` 流水账，**不接告警**，于是前端完全看不见。
        #   两处后果都在此收口：
        #     ① 拒单原因送不到界面（-⑻）—— 用户提的原始诉求；
        #     ② "合约参数取不到 → 一直拒单"（复核 ⑸定稿）——
        #        这条同样是静默的，用户以为引擎在跑，实际一笔都发不出去。
        #   设计沿用既有 D11 通道：`self.alert()` 自带**同 code 合并计数** +
        #   确认水位，故不需要新的去重机制；本段只负责"分级 + 计数升级"。
        #   `_reject_streak`：连续被前置校验拦下的次数（同一 code 口径，成功
        #   报单或冷却清零），用于把"偶发一次"与"卡死了"区分开 —— 后者升级一档。
        # ════════════════════════════════════════════════════════════════
        self._reject_streak: int = 0
        self._reject_streak_code: str = ""
        # ════════════════════════════════════════════════════════════════
        # ════════════════════════════════════════════════════════════════
        # 运行期 run 风控锚自检（补，G2 的运行期对等护栏）
        #   G2 只在**启动期**拒「有净敞口但无 run」；运行期对账改簿可能让
        #   净敞口 0→非 0 而绕过 `_run_start`（run 的
        #   唯一开启点）→ 这里补一道自检，否则 L1-L3 **静默**失效。
        #   `_run_ready`：恢复流程走完才允许自检（`_restore` 中途的 `_sync_state`
        #   会看到"有敞口、run 还没恢复"的瞬时假象）。
        #   `_run_missing_notified`：同一次异常只报一次（D11 队列本就按 code 合并，
        #   这里是挡**事件日志**刷屏 —— 每根 bar 一条会把真正的离场信号淹没）。
        # ════════════════════════════════════════════════════════════════
        self._run_ready: bool = False
        self._run_missing_notified: bool = False
        # ════════════════════════════════════════════════════════════════
        # D11 告警队列
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
        #   False = 关闭语义（用户拍板，确认保留）：
        #       ① 不再接收买卖点信号（on_signal 顶部门，见 auto_order_off skip）
        #       ② 运行态（净敞口 ≠ 0）持仓按规则 ⑹ 离场一次：
        #          今仓 → 反向 OPEN（变锁仓），跨日仓 → CLOSE
        #   由前端开关经后端进程托管触发（App/AppTrader.py → main.py 的
        #   shutdown_and_lock_all），_restore/_persist 持久化，重启不漂移。
        # ════════════════════════════════════════════════════════════════
        # （2026-09-17 改造）：周期 bar 秒数与追价窗口检查已删 ——
        #   追价改为"终态回报到手立即重报"（不 sleep），默认 3 轮追价单笔
        #   在一根 bar 内必然完成，原"追价窗口长于一根 bar"的告警不再有
        #   触发场景；bar 秒数缓存的唯一消费者就是该检查，随块一并移除。

        self.auto_order_enabled: bool = True
        self._restore()
        # （O-2/O-3）：构造期把 broker 已暂存的告警转手进 D11；
        # 此后每根 bar 在 on_bar 的 pulse() 之后续 drain（见 _drain_broker_alerts）。
        self._drain_broker_alerts()

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
    # 当前交易日（SSOT，立）
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
    # 旧版代码（含 P5..P11 测试）读写 engine.position 都是按"单 Position 或 None"
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
    # 启动闸门（收口）—— 共同口径：**宁可启动不了，也不带半新半旧的状态跑**。
    #   旧状态一旦被新口径解释，错误会一路跑到报单且不可逆（典型是对今仓发
    #   平今 CLOSE → 中金所拒单、离场卡住）。故三道闸门一律 fail-fast：
    #
    #     G1 旧 schema 闸门（本段）    持仓记录带已删除的来源键 / kv 残留旧键 → 拒
    #     G2 run 闸门（_restore_run）  净敞口≠0 却无 run、run 与净敞口方向矛盾 → 拒
    #     G3 时间锚闸门（_restore）    entry_date 三源全空 → 拒
    #
    #   ⚠️ 用户拍板：旧库一律**严格拒绝**，不做"自动剥离 + 继续跑"的
    #      兼容迁移。理由：旧记录的持仓语义（锁仓仓算不算敞口）与当前"只看净敞口"
    #      的口径不同，自动迁移只是把账实不符从启动期推迟到报单期。
    # ────────────────────────────────────────────────────────────────
    # 持仓记录曾用 `entry_mode` 键标记来源（后改名 `origin`），随
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
        # ════════════════════════════════════════════════════════════════
        # 品种白名单硬约束（用户拍板）：品种不在 PRODUCT_PROFILES
        #   → **拒绝启动引擎**（抛异常，子进程退出），不发告警、不带病运行。
        #   理由：告警应挂在交易引擎启动（用户确认的位置），而更严格的做法是
        #   未标定品种根本不允许启动——R 下限等执行参数未标定，启动即错。
        #   实盘入口的提前拦截在 App/AppTrader.start（BadRequestError → 400 → 前端
        #   alert 弹出原因）；这里是引擎侧的权威闸门（回放/CLI 直启同样拦）。
        #   注意大小写：前端别名表把 SHFE/DCE 解析成小写主连（KQ.m@SHFE.au），
        #   归一在 parse_product_key 内完成（档案键统一大写）。
        #
        # 判定收敛到 Product.assert_product_allowed
        #   —— ① 只用一处实现（原 App/Engine 两份，文案还不一样）；
        #      ② 解析口径从 parse_product（取末段）换成 parse_product_key（剥月份）：
        #         `CFFEX.IF2609` 原来解析成 "IF2609" 查不到档案 → 已标定的 IF 被
        #         白名单误杀，CLI 直启/回放直接起不来，报错还极具误导性。
        # ════════════════════════════════════════════════════════════════
        _key = assert_product_allowed(self.cfg.instrument.signal_symbol)
        # 启动期 SSOT 断言：
        #   白名单放行的品种 vs `self.state.product`（Instrument 构造期冻结的
        #   那份档案）**必须是同一个品种**，且该品种必须有执行策略行。
        #   见 `_assert_product_ssot` 的 docstring —— 这里把"两个来源只是
        #   碰巧一致"从约定升级为启动期硬失败。
        self._assert_product_ssot(_key)
        # 优先读新版 "positions" list（多仓），回退到老版 "position" 单字段。
        # 老数据库无 "positions" 键时也能恢复，且不破坏现有迁移路径。
        # v1.3（Q5 拍板）：restore 不再用 cfg 容量截断 —— 不限容量，恢复永不丢失持仓（解 D3）。
        restore_max = None
        # v1.4（切合约隔离）：只恢复当前 trade_symbol 的持仓。切换合约（如 IF→IM）后
        # 旧合约持仓留在 state.db 不加载进簿，避免 _restore 末尾的 _reconcile_positions
        # 把旧合约持仓当成「外部平仓」误清（PnL 还会按新合约 spec 算，全错）。
        # 旧合约持仓由 _persist 的分片合并继续保留在库里，切回原合约时可恢复。
        my_symbol = self.state.trade_symbol
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
        # F2：恢复后仍无 entry_date 的持仓 → 拒绝启动。
        #   entry_date 已在 Position.__post_init__（F4）里尽力重建，顺序为
        #     entry_bar_ts（建仓 K 线的毫秒时间戳 —— 权威来源，旧库必定有它，
        #                   因为 entry_bar_ts 比 entry_date 更早引入）
        #     entry_at    （墙钟字符串，兜底）
        #   三者全空 = 这条记录**真的不含任何时间信息**，"今仓/昨仓"无从判定 →
        #   规则 ⑸ 必然判错 → 对今仓发 CLOSE 平今 → CTP 拒单（reject_class=
        #   position）→ 离场卡住、只留 severe 告警。此处宁可拒绝启动，也不猜方向。
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
                "        拒单 → 离场卡住并升 severe 告警 ctp_reject_position。\n"
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
        # R1：三个持久 ID 的序号跨重启恢复。
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
        # D11：恢复未确认告警（severe 落库的目的就是"重启后还在"）
        self._load_alerts()
        # 运行态风控状态（run）：净敞口 ≠ 0 时必须有 run，否则 L1-L3 无从判定。
        self._restore_run()

        # 恢复自动下单开关。关闭语义要跨重启保持
        # （前端关闭 → 子进程退出 → 再启动服务/引擎必须仍是关闭态，
        # 不能悄悄重新开始接收信号）。AppTrader.start 显式置 True 再拉起。
        self.auto_order_enabled = bool(self.store.get_json("auto_order_enabled", True))

        # ════════════════════════════════════════════════════════════════
        # _restore 末尾首拉真实持仓
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
        # 两态机前提守卫 —— 放在恢复完持仓（含尾部的对账）之后、允许运行期
        # 自检之前：**这里才是真实破口**（改表后带旧 R-OPEN 的 state.db 重启，
        # 仓单数会是 2）。早于 `_load_alerts` 会被库内告警覆盖，故必须在其后。
        self._check_two_state_invariant("restore")

        # 恢复流程到此结束 —— 之后才允许运行期 run 自检（见 `__init__` 的
        # `_run_ready` 注释：`_restore` 中途的 `_sync_state` 会看到瞬时假象）。
        self._run_ready = True

    def _check_run_anchor(self, source: str) -> None:
        """运行期 run 风控锚自检 —— G2 的运行期对等护栏（补）。

        缺口：`_run_start` 全仓唯一开启点是 `_execute` 的成交落账分支，而净敞口
        有**一条路径不经过 `_execute`** 直接改簿：
          · 对账删仓（`Reconcile._reconcile_positions` 的 `positions.remove`）
        该路径只补了「净敞口 → 0」的收口边，**0 → 非 0 那条边全缺** → 净敞口
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
        # 双写兼容 —— 新键 "positions"（list）保留扩展空间，
        # 旧键 "position"（单字段）继续写以做审计 / 旧流程回归。
        # 多仓时旧键取 positions[0] —— 是为了保留"看一眼持仓是哪个合约"的旧 API，
        # 不是引擎主入口（主入口走 self.positions）。legacy_single() 在多仓会抛错
        # 是有意的早期守护 E3，这里规避它。
        # v1.4（切合约隔离）：positions 按 trade_symbol 分片写回 —— 只覆盖当前合约的
        # 持仓，保留库里其它合约的持仓，切走再切回时能恢复管理。旧键 "position" 优先
        # 写当前合约首仓，否则退回其它合约首仓（仅供审计「看一眼是哪个合约」）。
        my_symbol = self.state.trade_symbol
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
        # 运行态风控状态（run）：跨重启保持 L1-L3 的风控锚与出场计划。
        self._persist_run()
        # 持久化自动下单开关（跨重启保持关闭语义）
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
            "entry_offset": self._run_entry_offset,
            "entry_at": self._run_entry_at,
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
            # run 级会计元数据（v3.1 §5.1-2）：缺失 = 旧版写入的库。
            # 结算要用 entry_offset 选入场费率档，取错档会静默算错成本，
            # 故与"方向矛盾"同款 fail-fast：启动时拒绝，不留到结算时炸。
            _eo = str(d.get("entry_offset") or "")
            if _eo not in INTENT_TO_OFFSET.values():
                raise RuntimeError(
                    "state.db 的运行态（run）记录缺 entry_offset（旧版写入），\n"
                    "拒绝启动。run 级会计需要它选入场费率档，缺失时无法保证\n"
                    "成本口径正确。\n"
                    "  处理：确认账户无未了结持仓后，删除 Trading/State/state.db 再启动。")
            self._run_entry_offset = _eo
            self._run_entry_at = str(d.get("entry_at") or "")
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


    def _sync_state(self) -> None:
        """`_state` 是账户三态的**派生镜像**（IN_TRADE / IDLE），供外部读取。

        它不是独立状态机：真值恒等于 `account_state()`，此处只做一次投影，
        避免外部（API / 前端 / 旧测试）各自去判三态。

        这里顺带挂一次 run 风控锚自检 —— `_sync_state` 是所有改簿
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
            self.exit_policy.on_bar(bar, self.state)
        except Exception:
            pass

        # （原 on_bar 里的 RiskGate.roll_day 换日统计随风控五道硬闸门整体删除。）

        self.ev.write("bar", date=bar.date, close=bar.close,
                      high=bar.high, low=bar.low, seq=self.bars_seen)

        # 心跳：真实 CTP 通道需要定期收发数据，否则被判"用户不活跃"断连。
        # dry_run 通道的 pulse() 是空实现，零开销。
        try:
            self.broker.pulse()
        except Exception:
            pass

        # （O-2/O-3）：broker 侧 instrument 故障告警
        # 回流 D11 —— SimNow 用 notify() 暂存的诊断（超时 / nan / 与配置不一致）
        # 在这里转手 Engine.alert（同 code 自动合并，不会刷屏）。不支持
        # drain_alerts 的通道（鸭子判断）静默跳过。
        self._drain_broker_alerts()


        # 持仓对账（增强 B）：与券商真实持仓比对。若发现持仓已被外部平掉
        # （如用户在快期3手工平仓）或属幽灵持仓，立即修正引擎账目，
        # 避免继续傻等平仓 / 误判新信号。dry_run 等无真实账户的通道返回 None，跳过。
        # 盲区补（2026-09-18）：账本空 ≠ 柜台无仓（拒单误判会留下账外仓），
        # 对账改为**无条件**执行；账本空侧的比对在 Reconcile 内部处理
        # （账本空但柜台有量 → severe 告警不接管），全程空仓时静默返回。
        self._reconcile_positions()

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
            run, bar, self.state, bars_held=bars_held)
        if check is None:
            return

        prev_phase = (str(self._run_plan.params.get("_phase") or "")
                      if self._run_plan is not None else "")
        if check.plan is not None:
            self._run_plan = check.plan
        if check.only_update:
            # 只更新计划（保本 / 跟踪位移），不触发离场。
            # 阶段跃迁（"" → breakeven → trailing）= 盈利达标时刻 → toast（需求 ⑷(3)(4)）
            new_phase = str(self._run_plan.params.get("_phase") or "")
            if new_phase and new_phase != prev_phase:
                self._notify_run_phase(new_phase)
            self._persist()
            self.ev.write("exit_plan_update", reason=check.reason,
                          stop=self._run_plan.stop_price,
                          tp=self._run_plan.tp_price,
                          symbol=run.symbol,
                          position_signal_key=run.signal_key)
            return

        self._force_exit(bar, reason=check.reason, trigger_price=check.price)


    def pump_broker(self) -> None:
        """空闲泵：驱动一次 broker 的回报处理（P68k，SSE 源每帧·含心跳帧回调）。

        tqsdk wait_update 单线程，只在被驱动时消费网络帧（见 SimNow.pulse
        注释）。交易引擎此前每根 bar 才 pump 一次（keepalive_wait=0.2s 窗口），
        两根 bar 之间到达的委托/持仓回报滞留在缓冲里，gateway.log 的回报
        时刻因此被拉长、real_position 读数也偏旧。由 main.py 把本方法注入
        SSE 源的 on_idle，回报处理收窄到帧级。传 window=0 单轮推进（处理已到达的包、不等新包）：帧率
        （含心跳 ≈10/s）高于 0.2s 保活窗口的消费上限（5/s），沿用窗口会让
        积压反灌 SSE 的 buf；CTP 保活仍由 bar 级 pulse()
        （on_bar）负责。pulse 内部已吞异常，这里不再包裹 —— 单点语义，
        方便将来换 broker 时对齐行为。
        """
        self.broker.pulse(0)

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

        # 买卖点类型过滤门：只放行用户在「显示设置 → 买卖点类型（可多选）」里
        # 勾选的类型（与 K 线图上画哪些买卖点同一份勾选）。
        #   · 每次信号现读 state.db（不缓存）—— 盘中改勾选即刻改变处理策略，
        #     与账户三态无关：空仓态的开仓、锁仓态的拆锁都走这一道门；
        #   · 运行态本就不响应信号（规则 ⑶），其离场走 L1-L3、不经此处，
        #     故「过滤」不会让已有持仓失去止损止盈。
        if not self._bsp_type_allowed(sig.bsp_type):
            self.store.update_signal_action(sig.key, "skip", "bsp_type_filtered")
            self.ev.write("signal_skip", key=sig.key,
                          reason="bsp_type_filtered",
                          type=sig.bsp_type, is_buy=sig.is_buy)
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
            # 空仓开新仓：入场策略只做数据兜底（signal.price<=0 → SKIP），
            # 不再做信号质量过滤（振幅/止损距离上下限）—— 该职责已归缠论分析引擎（见 Q13）
            decision = self.entry_policy.decide(sig, None, self.state)
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

    # ---------------- 买卖点类型过滤（用户勾选 → 信号门） ----------------
    def bsp_type_filter(self) -> Optional[Dict[str, bool]]:
        """**现读**「买卖点类型过滤」勾选表（每次调用都重读 state.db）。

        返回 None = 用户从未推送过勾选（新状态目录 / 升级前部署）→ 调用方按
        「全部放行」处理，绝不因"没有配置"就让自动下单静默停摆（与
        `auto_order_enabled` 默认 True 同一保守方向）。

        刻意不缓存：缓存会让「盘中改勾选立刻生效」这条需求直接失效。state.db
        是 WAL 模式，单次 kv 读取是毫秒级，而信号到达频率是每根 K 线级别 ——
        没有任何性能理由去换一个会过期的值。

        写入方：App/AppTrader（前端设置面板 → POST /api/trader/signal-filter）。
        读取方：只有本文件的 `_bsp_type_allowed`。
        """
        raw = self.store.get_json(BSP_TYPE_FILTER_KEY, None)
        if not isinstance(raw, dict):
            return None
        return {str(k): bool(v) for k, v in raw.items()}

    def _bsp_type_allowed(self, bsp_type: str) -> bool:
        """该买卖点类型是否被用户勾选放行。

        · 过滤表未设置 → 放行（见 `bsp_type_filter` 的说明）。
        · 已设置 → 只有**显式勾选为真**的类型放行；未勾选的类型、以及四类之外
          的类型（11p / 22s / 33a …，由 ChanConfig.bs_type 决定是否会出现）
          一律忽略，并写 `signal_skip reason=bsp_type_filtered` 留痕。
        · 逗号串（"1,11" / "1,2"，同位置合并类型）按段拆，**任一段勾选即放行**。

        ⚠️ 这不是 P44 禁止的「信号质量过滤」：那条禁令针对的是"信号值不值得开仓"
        的自动判断（振幅 / 止损距离），属于缠论分析引擎的职责；本门是**用户显式
        勾选**的表达，与 `auto_order_enabled` 同类，不做任何质量评价。
        """
        filt = self.bsp_type_filter()
        if filt is None:
            return True
        # type2str() 可能是逗号串（同一笔同一右肩 K 上合并出的多个类型，见
        # BuySellPoint/BSPointList.py 的 add_another_bsp_prop）→ 按逗号拆段，
        # 任一段被勾选即放行；与前端 drawBspMarkers 的显示口径保持一致 ——
        # 否则这类点会"图上不画 + 单也不下"，两边同时静默漏掉。
        for seg in str(bsp_type).split(","):
            seg = seg.strip()
            if seg and filt.get(seg):
                return True
        return False

    # ════════════════════════════════════════════════════════════════
    # 决策 / 执行层（架构约束 A1 + A3）
    #   A1：三态与转移动作只在 `account_state` / `_decide_action` /
    #       `_decide_exit` 三个方法里判定，别处不许再写 `if net == 0` 之类
    #       的内联判定 —— 二期加品种护栏时不用回头找"还有哪里判了三态"。
    #   A3：`_execute` 是全引擎**唯一**的报单出口，前面挂 `_pre_trade_check`
    #       校验链（二期 Q1 全品种 / Q3 交割月的护栏都往这条链上加，
    #       不碰转移表结构）。
    # ════════════════════════════════════════════════════════════════
    @property
    def lots_per_order(self) -> int:
        """一笔报单挂几手 —— **开仓手数的唯一口径**（收敛）。

        = 品种执行策略表第 3 列（`EXEC_POLICY[code].lots_per_order`），**就这一个
        来源**。原实现是 `min(risk.max_volume, 表值)`，于是单笔手数有了两个旋钮：
        用户"改表 N"不一定生效（被风控压住），而启动横幅只打表值 —— `max_volume=1`
        而表写 2 时，横幅说"一笔 2 手"、实际挂 1 手（唯一可见的行为变更处反而误导，
        ）。用户拍板：**删掉 `risk.max_volume`，本列就是用来
        替换它的** —— 故这里不再取小，表即唯一来源。

        ⚠️ **平仓不套这个帽子**（这是本 property 名不叫"手数唯一口径"的原因）：
        `_decide_exit` 的转移④⑤、`_decide_action` 的转移③一律按"平满目标"的手数
        走（见各分支注释）—— 跨会话改小表 N 后带旧仓重启，旧仓仍能**全额**平掉，
        不会算出"只平一部分"而撞 `close_volume_below_target`。

        无品种档案 → **1 手**（保守侧：宁可少开，不按猜出来的手数下单；
        与"档案缺失时其余派生值一律取保守侧"同一约定 ——
        原文举的 `Instrument.exchange` 未标定 → "" 一例，随该字段于
        删除而不再存在）。
        """
        pol = self._exec_policy()
        return int(pol.lots_per_order) if pol is not None else 1

    def _open_volume(self) -> int:
        """一笔报单挂几手 —— **开仓手数的唯一来源**。

        2026-09-16 起唯一口径 = `lots_per_order`（品种执行策略表第 3 列），取代
        原「CZCE 钉 1 手」交易所分支 —— 代码不看交易所名字，只读表
        （用户第 3 轮 ⑵ 明令）；也不再与任何风控上限取小（同日起 `risk.max_volume`
        删除，见 `lots_per_order` docstring）。

        注意：设计上**一次信号只报 1 笔**，这里只是决定那 1 笔挂几手，
        **没有任何拆单**（拆 N 笔 1 手的逻辑不存在，也不允许存在）。
        """
        return self.lots_per_order

    def _decide_action(self, sig: Signal, today: str) -> Optional["_Action"]:
        """交易信号到达时的动作决策（转移 ①②③）。**纯函数：不改任何状态。**"""
        st = self.account_state()
        if st is AccountState.RUNNING:
            return None                                    # 规则 ⑶
        if st is AccountState.FLAT:
            # 转移 ①：空仓 → OPEN（信号方向），一笔挂 _open_volume() 手
            #   （= lots_per_order = 品种执行策略表第 3 列；设计上一次信号只
            #     1 笔，无拆单。2026-09-16 起不再与风控上限取小）
            return _Action(OrderIntent.OPEN, sig.side, self._open_volume(),
                           None, is_exit=False, transition=1)

        # 锁仓态：看最近一笔仓单的建仓交易日（D_last）
        latest = self.positions.latest()
        if latest is None or latest.entry_date >= today:
            # 转移 ②：当日锁 → OPEN（信号方向），不动锁仓仓单
            return _Action(OrderIntent.OPEN, sig.side, self._open_volume(),
                           None, is_exit=False, transition=2)

        # 转移 ③：跨日锁 → CLOSE（信号方向）
        #   买信号 = 买平 = 平掉空头仓；卖信号 = 卖平 = 平掉多头仓。
        #   对冲目标 = 持仓序列中**反向最早**的一笔（附录：仓单无配对，纯 FIFO）。
        #   手数 = `target.volume`（**平满目标**）：平仓不接受任何"单笔手数上限"
        #   —— 那顶帽子只属于开仓（`lots_per_order`）。原先这里写
        #   `min(lots_per_signal, target.volume)`，一旦跨会话把风控手数/表 N 调小
        #   就会算出"只平一部分"，而 `_book_close` 是**整笔**记账的（PositionBook
        #   无减仓 API）→ 直接被 `close_volume_below_target` 拒单。手数旋钮收敛后
        #   （删 `risk.max_volume`）这里也就没有取小的必要。
        side = _opposite(sig.side)
        target = self.positions.oldest_opposite(sig.side)
        if target is None:
            # 簿非空却找不对冲目标 —— 锁仓态必是双向，走到这里说明簿被外力改过。
            # 不猜、不开新仓，写事件后放弃本信号。
            self.ev.write("signal_no_close_target", key=sig.key, today=today,
                          note="锁仓态下找不到可对冲的反向仓单，放弃本信号")
            return None
        return _Action(OrderIntent.CLOSE, side,
                       target.volume,
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
        today = self._current_trading_day(bar)
        if latest.entry_date >= today:
            # 转移 ④：今日仓离场。**唯一口径 = 品种执行策略表第 1 列**
            #   （`ExecPolicy.today_exit`） —— 不看交易所名字、不算费率
            #   （用户拍板：人算 → 改表 → 启动，代码只读表）。
            #
            #   · CLOSETODAY → 直接平今（今仓清零 → 回空仓态）。
            #     手数 = min(净敞口, 该笔手数)：平仓必须平满目标，
            #     故这里**不套** `lots_per_order`（那顶帽子只属于 OPEN）。
            #     两态机：这类品种的运行态**有且只有一笔当日仓**（只能由转移①
            #     产生、平完即空仓），锁仓态**结构性不可达** —— 所以 `latest`
            #     就是那笔、也就是唯一的平今目标：不必再查一次 positions，
            #     也不存在"找不到目标"的分支（故无兜底、无 Optional）。
            #   · R-OPEN → 反向 OPEN 锁仓（净敞口归零，进入锁仓态）。
            if self._today_exit() == CLOSETODAY:
                return _Action(OrderIntent.CLOSETODAY, net_side,
                               min(abs(net), latest.volume),
                               latest, is_exit=True, transition=4)
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

    # ════════════════════════════════════════════════════════════════
    # 品种执行策略表的**单点读取**（补）
    #   表第 1/3 列的四个消费点（`lots_per_order` / 转移④ / `_pre_trade_check`
    #   平今判据 / 两态守卫）此前各写一遍 `getattr(self.state, "exec_policy",
    #   None)`。重复读取的风险不是"多打几个字"，而是**守卫与决策可能读到不同
    #   口径**（守卫说"你是两态品种"、决策说"你不是" → 守卫形同虚设）。
    #   故收敛到本方法一处：判据恒同源。
    #
    #   **本方法内部也不再 `getattr`** —— 连"没有该属性就
    #   静默给 None"这最后一个降级口也关掉；同时把 `Optional[Any]` 收紧成
    #   `Optional[ExecPolicy]`。启动期由 `_assert_product_ssot` 断言"已标定品种
    #   恒非 None"，故运行期拿到的永远是表里那一行。
    # ════════════════════════════════════════════════════════════════
    def _exec_policy(self) -> Optional[ExecPolicy]:
        """本品种执行策略（`Infra/Product.py` 的 `EXEC_POLICY[code]` 那一行）。

        真值源 = `Instrument.exec_policy` → `Product.exec_policy`。无品种档案
        → `None`（保守侧：按"不支持平今、按手数兜底"走；**已标定品种恒非 None**，
        见 `_assert_product_ssot`）。

        ⚠️ 2026-09-16 去掉 `getattr(self.state, "exec_policy", None)`：
          `self.state` 恒是本仓的 `Instrument`（`__init__` 回落链保证：显式
          state → broker.state → 新建 Instrument），该属性**恒存在** → 用
          getattr 等于给"哪天属性被改名/搬走"留了一条静默降级：守卫读到 None
          会说"你不是两态品种"、决策读到 None 会走另一套 → **守卫形同虚设，
          且没有任何告警**。改成直接属性访问后，属性没了就是 AttributeError，
          响亮地死在启动期（正是本仓"不搞跨文件 fallback"的立场）。
        """
        return self.state.exec_policy

    def _assert_product_ssot(self, allowed_key: str) -> None:
        """启动期断言：品种档案只有一个来源，且执行策略行必须存在。

        **为什么要断言** —— 本引擎里"当前品种是哪一只"有**两个**读取口径：
          · `cfg.product_profile` —— **实时**按 `cfg.instrument.signal_symbol`
            查表（`Config.py` 的 property，每次调用都重新查一遍）；
          · `self.state.product` —— `Instrument` **构造期**传入并冻结的那份。
        两者恒等只靠一个**约定**：「`--symbol` 的配置重建（main.py）发生在
        `Instrument` 构造之前」。将来任何"先建 Instrument、后改 cfg"的路径都会
        让它们分叉，症状 = **决策按 A 品种、记账按 B 品种**（成本/乘数口径全错，
        而且完全静默）。这里把约定变成启动期硬失败。

        **另断言执行策略行必须存在**：`Instrument.exec_policy` 的类型是
        `Optional[ExecPolicy]`（那一层 Optional 是给"未标定品种"的离线探针留的），
        而品种既然过了白名单闸门，就必然在 `PRODUCT_PROFILES` 里、也就必然在
        `EXEC_POLICY` 里（`_exec_kw()` 在构造期就硬 KeyError）。这里为 `None`
        只可能是"表里漏了行 / 字段被改名"这类真异常 —— **拒绝启动**，而不是让
        引擎静默走保守侧（报单 FOK、今仓 R-OPEN、手数 1）：那等于用一套没人
        宣布过的策略下真单。

        调用点：`_restore`（构造期，紧随白名单闸门）。断言是启动期一次性事实，
        不必每笔报单重复检查。
        """
        p = self.state.product
        if p is None:
            raise ValueError(
                "品种档案缺失：白名单已放行 {!r}（解析品种键 = {}），但运行时对象 "
                "`Instrument.product` 为 None —— Instrument 构造时没拿到该品种档案，"
                "决策侧与记账侧会各按一套参数走。拒绝启动交易引擎。".format(
                    self.cfg.instrument.signal_symbol, allowed_key))
        got = str(p.product or "").strip().upper()
        if got != allowed_key:
            raise ValueError(
                "品种来源分叉：白名单按 cfg.instrument.signal_symbol={!r} 放行品种 "
                "{!r}，但运行时对象持有的档案是 {!r} —— 会造成「按前者决策、按后者"
                "记账」（成本与乘数口径不一致）。请检查 Instrument 的构造参数与 cfg "
                "是否同源（main.py 必须把**同一份** cfg 同时交给 Broker 与引擎）。"
                "拒绝启动交易引擎。".format(
                    self.cfg.instrument.signal_symbol, allowed_key, got))
        if self.state.exec_policy is None:
            raise ValueError(
                "品种 {!r} 缺少执行策略行：它在 PRODUCT_PROFILES 白名单里，但 "
                "`Instrument.exec_policy` 为 None（EXEC_POLICY 漏行，或 "
                "Product.exec_policy 字段被改名）—— 执行策略不确定时不允许启动，"
                "否则引擎会静默按保守侧（报单 FOK / 今仓 R-OPEN / 手数 1）下真单。"
                .format(allowed_key))

    def _today_exit(self) -> Optional[str]:
        """执行策略表第 1 列：**今仓离场走哪条路**（`CLOSETODAY` / `R-OPEN`）。

        `None` = 无品种档案 → 保守侧（照 `R-OPEN` 走锁仓，不生成会被拒的平今单）。

        ⚠️：本方法原名 `_close_mode`（字段名同理），改回 `today_exit`。
        旧名对 8 行里 **5 行**（IF/IH/IC/IM/TA，取值 R-OPEN）是误导 —— 那一支发的是
        **反向 OPEN**，属于开仓而非平仓。改名只动名字、不动任何判据：
        两处比较（转移④ / `_check_two_state_invariant`）与取值域完全不变。
        """
        pol = self._exec_policy()
        return pol.today_exit if pol is not None else None

    def _check_two_state_invariant(self, where: str) -> None:
        """两态机前提的运行时守卫（补）。

        **前提**：`today_exit == CLOSETODAY` 的品种，运行态**有且只有一笔当日仓**
        —— 它只能由转移①（开仓）产生，离场直接平今、平完即回空仓态。故锁仓态
        在这类品种上**结构性不可达**。转移④ 正是靠这条前提才敢用 `latest` 直接
        当平今目标（不回查 positions、不存在"找不到目标"的分支）。

        **为什么需要它** 这条前提此前只有文档声明，没有任何运行时
        检查。唯一能打破它的是「改表（`R-OPEN` → `CLOSETODAY`）后带着旧
        `state.db` 重启」—— 旧库里留着的反向锁仓单会让仓单数变成 2，此时
        `latest` 不再是唯一目标，离场只平一笔、另一笔**静默漏平**（簿面与实盘
        脱节且无人知晓）。本守卫把这条静默路径变显性。

        **处置 = 只告警、不自动修**：该平哪笔、要不要先拆锁，是人的决定；引擎
        自己猜一个方向只会让账实更不一致。故写事件 + 发 SEVERE 告警（前端阻塞
        弹窗），由人核对实盘后处理。

        调用点两处：`_restore`（恢复完持仓后 —— 抓真实破口"带旧库重启"）、
        `_book_open`（落账后 —— 抓运行期任何产生第二笔 OPEN 的路径）。
        """
        if self._today_exit() != CLOSETODAY:
            return
        n = len(self.positions.positions)
        if n <= 1:
            return
        msg = ("两态机前提被破坏：该品种执行策略表今仓离场 = CLOSETODAY（两态品种），"
               "但当前有 {} 笔仓单（应为 1 笔）。最常见原因：品种档案在 R-OPEN → "
               "CLOSETODAY 之间改过表，而 state.db 里还留着旧的反向锁仓单。"
               "此时离场只按最新一笔走，另一笔会漏平 —— 请核对实盘持仓后人工"
               "处理（改表或清库），引擎不自动猜方向。").format(n)
        self.ev.write("two_state_invariant_broken", where=where,
                      positions_n=n, net_volume=self.positions.net_volume(),
                      today_exit=self._today_exit(),
                      note="CLOSETODAY 两态品种出现多笔仓单（锁仓态应不可达）")
        self.alert(self.ALERT_SEVERE, "two_state_invariant_broken", msg,
                   where=where, positions_n=n)

    def _check_spec_drift(self) -> None:
        """合约规格漂移校验（防回潮护栏，2026-09-17 改造后语义）。

        背景：有效 tick/乘数的 **SSOT = 品种档案 Product**（构造期播种，
        无任何行情取值路径）—— state 与档案同源，结构上恒无漂移。
        本校验保留作**防回潮护栏**：若将来有人重新引入"行情覆盖 state"
        的写入通道而档案未同步，这里会把双源漂移从静默变显性
        （warn 轻提示，D11 通道前端 toast，不拒单）。

        一次性：verified 首次为真时查一次即置位（合约规格会话内不变）。
        挂在 A3 校验链（_pre_trade_check）上，遵循"二期扩展往链上加"惯例。
        """
        # 档案来源由 `cfg.product_profile`（**实时**按
        #   cfg.instrument.signal_symbol 查表）改为 `self.state.product`
        #   （唯一运行时对象、构造期冻结）—— 与 `_book_close` 的成本口径同源，
        #   杜绝"按 A 品种判漂移、按 B 品种记账"。启动期已由
        #   `_assert_product_ssot` 断言两者是同一个品种。
        p = self.state.product
        if p is None:
            return
        if self.state.verified:
            if self._spec_drift_checked:
                return
            self._spec_drift_checked = True
            self._check_spec_drift_online(p)

    def _check_spec_drift_online(self, p: "Product") -> None:
        """state 有效值 vs 品种档案（防回潮护栏，正常恒一致）。"""
        diffs = []
        if self.state.price_tick != p.price_tick:
            diffs.append("price_tick 档案={} / state={}".format(
                p.price_tick, self.state.price_tick))
        if self.state.multiplier != p.multiplier:
            diffs.append("multiplier 档案={} / state={}".format(
                p.multiplier, self.state.multiplier))
        if diffs:
            self.alert(
                self.ALERT_WARN, "spec_drift",
                "state 合约规格与品种档案不一致（{}）。有效参数 SSOT=品种档案，"
                "请同步更新 PRODUCT_PROFILES；若存在非档案来源的写入路径"
                "（防回潮护栏触发），请排查。".format("；".join(diffs)),
                signal_symbol=str(self.cfg.instrument.signal_symbol),
                source=self.state.source)

    # 删除 _check_closetoday_economy（共 36 行）：
    #   平今经济性从"成交后建议"改为档案费率启动即派生（Product.prefer_closetoday）；
    #   2026-09-16 起再改一次：改为**品种执行策略表第 1 列直接给定**
    #   （ExecPolicy.today_exit）—— 决策侧不再读费率，建议与执行分叉的裂缝消除。
    def _pre_trade_check(self, act: "_Action", today: str,
                         sig: Optional[Signal] = None,
                         ref_price: float = 0.0) -> Optional[str]:
        """报单前校验链（A3）。返回 None = 通过；返回字符串 = 拒绝原因。

        ⚠️ 二期（Q1 全品种 / Q2 平今开关 / Q3 交割月护栏）一律往这条链上加，
        不要去改 `_decide_action` / `_decide_exit` 的转移表结构。
        （D20 → 2026-09-17 改造）此链上的 A′ item：
          · instrument_unverified —— A′ 在线闸门（在线通道连接成功才放行；
            合约参数 SSOT=品种档案，无行情校验环节）。涨跌停护栏
            （引擎侧参考价粗检 + broker 侧最终限价精确校验）已整体删除：
            报出必然被废的价格由交易所拒单、软件侧弹窗，用户手工干预即可。
        """
        # ── A′ 在线闸门（2026-09-17 改造）──
        #   在线通道（simnow/live）：verified=True = 连接成功（SimNow._connect
        #   置位，source=CONFIG）。未连通（或连接失败）→ 拒单 + 严重告警。
        #   合约参数 SSOT=品种档案，无行情校验环节 —— 就一个逻辑。
        #   离线通道（dry_run）放行：来源已在 main.py 标记 CONFIG_OFFLINE。
        #   verified / source 读自 **self.state**（与 broker 同一份）。
        if not (getattr(self.broker, "is_offline", False)
                or self.state.verified):
            self.alert(self.ALERT_SEVERE, "instrument_unverified",
                       "在线通道未连通（verified=False），已拒单。"
                       "fail-closed：宁可不下单，也不带未确认的通道状态下单；"
                       "连接恢复后自动放行。",
                       source=self.state.source,
                       broker=getattr(self.broker, "name", ""))
            return "instrument_unverified"
        # 合约规格漂移校验（verified 首次为真后查一次；warn 不拒单）
        self._check_spec_drift()
        # ── 交割月护栏（阻塞点 4 · D8）──
        #   三态语义（拍板）：空仓态拦截开仓、锁仓态拦截平仓、运行态不拦截。
        #   原则：交割月附近不让**新进裸仓**、也不让**解锁成裸仓**，已运行的仓位
        #   可正常交易 / 锁仓（运行态不拦）。判据 = 剩余交易日 < delivery_guard_days
        #   即拦（默认 1 = 仅最后交易日当天）。护栏对象 = 现行主力 last_trade_date
        #   （换月自动解除）；last_trade_date 未知（离线 dry_run / 行情未取到）→
        #   不拦。guard_days=0 → 关闭护栏。
        guard_days = int(getattr(self.cfg.risk, "delivery_guard_days", 1))
        if self.state.delivery_guard_blocked(today, threshold_days=guard_days):
            state = self.account_state()
            is_open = act.intent is OrderIntent.OPEN
            is_close = act.intent in (OrderIntent.CLOSE, OrderIntent.CLOSETODAY)
            blocked = ((state is AccountState.FLAT and is_open) or
                       (state is AccountState.LOCKED and is_close))
            if blocked:
                self.alert(
                    self.ALERT_SEVERE, "delivery_guard_blocked",
                    "距最后交易日 {} 不足 {} 个交易日（交割月护栏）—— 已拒绝{}："
                    "{}。当前主力换月后自动解除（运行态仓位不受影响）。"
                    .format(
                        self.state.last_trade_date or "未知", guard_days,
                        "开新仓" if is_open else "平仓/解锁",
                        "空仓态不进裸仓" if is_open else "锁仓态不放开成裸仓"),
                    symbol=self.state.trade_symbol,
                    last_trade_date=self.state.last_trade_date,
                    state=state.value, intent=act.intent.value, guard_days=guard_days)
                return "delivery_guard_blocked"
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
        # ── CLOSE / CLOSETODAY ──
        # 2026-09-16 起判据换源：不看"交易所是否支持平今"，而是看
        # **该品种执行策略表第 1 列是否就是 CLOSETODAY** —— 表是唯一事实源，
        # 动作本来就照着表生成；这里再兜一道是防未来新增调用点绕过
        # `_decide_exit` 直接构造 CLOSETODAY 动作。
        if act.intent is OrderIntent.CLOSETODAY:
            if self._today_exit() != CLOSETODAY:
                return "closetoday_not_supported"
        if act.target is None:
            return "close_without_target"
        if not act.target.entry_date:
            return "close_target_no_entry_date"
        # ★ 全交易所安全性硬约束（契约测试 p35 / p51）：CLOSE 与 CLOSETODAY
        #   的目标日期必须与意图匹配 ——
        #   对今仓发 CLOSE：中金所没有平今指令，会被当平昨处理并按平今费率收费
        #     （0.0345%，是平昨的 15 倍）；上期所/能源中心则需要 CLOSETODAY。
        #   对昨仓发 CLOSETODAY：今仓不足 → 柜台拒单（平今仓位不足），
        #     且簿面按今仓记账会与实盘错位。
        #   规则 ⑷⑸⑹⑺ 保证正常路径不会产生这种报单，这里是最后一道闸。
        if act.intent is OrderIntent.CLOSETODAY:
            if act.target.entry_date < today:
                return "closetoday_target_is_yesterday"
        else:
            if act.target.entry_date >= today:
                return "close_target_is_today"
        if act.volume > act.target.volume:
            return "close_volume_exceeds_target"
        # 补（对称）：`act.volume < target.volume` 同样是账实不符 ——
        # broker 只平掉 `act.volume` 手，而 `_book_close` 是按 `pos.volume`
        # **整笔**记 Trade 并整笔 `positions.remove` 的（它拿不到"实际平了多少"）。
        # 触发场景（删 `risk.max_volume` 时复核收窄；同日端到端实测校准）：
        #   转移 ③④ 已改成"平满目标"，不再产生不足量；**仍可产生**的只剩转移 ⑤ ——
        #   `|net| < 目标那笔的手数`。
        #   ⚠️ 它的前提是「**簿内同向各笔手数不一致**」，而这只可能来自
        #      「**改表第 3 列时簿非空**」：N 恒定时每一笔开仓都挂 N 手，净敞口恒
        #      ∈ {0, ±N}，`min(|净|, 目标手数)` 恒等于目标手数（恒平满）。实测
        #      6 轮锁仓↔运行循环：转移 ⑤ 6/6 平满、零拒单。要凑出两种手数并存，
        #      得是「改 N 时停在锁仓态（簿内 2 手双向）→ 改 N=4 → 顺势开一笔 4 手
        #      新仓」—— 于是簿内出现 2 手笔与 4 手笔，净敞口 2 却要对冲那笔 4 手。
        #      只在空仓态改 N 则簿已清空、旧手数不残留，本条同样不可达。
        #      （原注释把触发条件写成"改小表 N 后带旧仓重启"，漏了这个前提 ——
        #       字面上自相矛盾：不改 N 时簿空，哪来的"旧仓 + 新仓"并存。）
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
          OPEN  → 簿内新增一笔仓单（含 ④ 反向开仓锁仓离场）
          CLOSE → 移除被对冲的那笔仓单
        落账后按**净敞口变化**开启 / 结束 run（风控锚 = 本次成交价，D1）。
        Trade 由 run 结束点的 `_settle_run` 统一写（run 级会计，v3.1 §5.1）。
        """
        self._last_reject = ""
        today = self._current_trading_day(bar)
        why = self._pre_trade_check(act, today, sig, ref_price=ref_price)
        if why is not None:
            self._last_reject = why
            self.ev.write("order_rejected",
                          key=(sig.key if sig is not None else ""),
                          reason=why, transition=act.transition,
                          intent=act.intent.value, volume=act.volume,
                          note="报单前校验未通过，未向柜台发出任何委托")
            # 把"被前置校验拦下"也接进告警通道，否则前端
            # 看不见（原始诉求）+ "合约参数取不到一直拒单"会静默卡住。
            self._note_precheck_reject(act, why, sig)
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
        # ── 审计补全──────────────────────────────
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
            self._sync_state()
            return None

        # 报单真的发出去了 → 前置校验连拒计数器归零（"连续"而非"累计"，
        # 否则偶发几次跨天累积到阈值会误升级成严重告警）。
        self._reject_streak = 0
        self._reject_streak_code = ""
        # ── 成交落账：净敞口的变化决定 run 的开启 / 结束 ──
        # run 级会计（v3.1 §5.1）：Trade 一律由 `_settle_run` 在 run 结束点写，
        # `_book_close` 只负责移除仓单；close 事件在结算后写入并回填 trade_id。
        net_before = self.positions.net_volume()
        if act.intent is OrderIntent.OPEN:
            self._book_open(act, o, sig)
        else:
            self._book_close(act, o, reason)
        net_after = self.positions.net_volume()
        settled: Optional[Trade] = None
        if net_before == 0 and net_after != 0:
            # 本次成交是 run 的**入场**：空仓开仓（OPEN 档）或锁仓拆锁
            # （CLOSE / CLOSETODAY 档）。offset 直接取 intent 映射，
            # 不读任何 broker meta —— SimNow 的 meta["offset"] 是动作标签，
            # 与 CTP 档位语义不等价（设计文档 v3.1 §8）。
            self._run_start(o.filled_price, bar, sig,
                            entry_offset=INTENT_TO_OFFSET[act.intent])
            self._notify_open(o)
        elif net_before != 0 and net_after == 0:
            # run 结束 —— 今仓反向开 / 昨仓平 / shutdown 强平 / on_bar retry
            # 四条路径在此收敛 → 结算唯一出口 `_settle_run`（防双写，[S9]）。
            # 阶段标记必须在 _run_end 清空前取（移动止盈/保本 → 平仓文案；
            # 结算同样要读 _run_plan）。
            settled = self._settle_run(
                exit_price=o.filled_price,
                exit_offset=INTENT_TO_OFFSET[act.intent],
                reason=reason, at=now_cn(), volume=o.volume)
            self._notify_close(o, act, reason)
            if act.intent is not OrderIntent.OPEN and act.target is not None:
                # close 事件（仓单级 gross 口径保留供对账/回放）；run 结算先行，
                # 此处回填其 trade_id，杜绝 events 里的悬空引用（[S10]）。
                # ⚠️ 必须在 `_run_end()` **之前**写：`_run_reset` 会清 `_run_plan`，
                # 而 `_write_close_event` 的 exit_policy 取
                # `self._run_plan or pos.exit_plan` —— 挪到 _run_end 之后会
                # 退化为占位名 run_managed（验收报告 P1-2，2026-09-21）。
                self._write_close_event(
                    act, o, reason,
                    trade_id=(settled.trade_id if settled is not None else ""))
            self._run_end()
        if (act.intent is not OrderIntent.OPEN and act.target is not None
                and net_after != 0):
            # 未收口的 close（净敞口仍非 0，如部分离场）：无 run 结算，
            # trade_id 留空（无悬空引用，[S10]）；run 仍在途、`_run_plan`
            # 未清，exit_policy 仍取 run 的真实计划名。
            self._write_close_event(act, o, reason, trade_id="")

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
            symbol=self.state.trade_symbol, side=act.side, volume=act.volume,
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
        # 两态机前提守卫（CLOSETODAY 品种仓单数应恒为 1）—— 见方法 docstring
        self._check_two_state_invariant("book_open")

    def _book_close(self, act: "_Action", o: Order, reason: str) -> None:
        """CLOSE 成交 → 移除被对冲的那笔仓单。

        run 级会计（设计文档 v3.1 §5.1）后本方法**不再写 Trade**：盈亏按 run
        结算（`_settle_run`，entry 取 run 入场 fill，不是被平仓单自己的
        entry_price —— 拆锁场景下二者不同，那正是仓单级口径错误的根源）。
        close 事件由 `_write_close_event` 在结算后统一写入（trade_id 回填）。
        """
        pos = act.target
        if pos is None:
            return
        self.positions.remove(pos)

    def _settle_run(self, exit_price: float, exit_offset: str,
                    reason: str, at: str,
                    volume: Optional[int] = None) -> Optional[Trade]:
        """run 结算**唯一出口**（run 级配对会计，设计文档 v3.1 §5.1）。

        一笔 Trade = 一次 run = 恰好一对成交：
          entry = 本段 run 的入场 fill（`_run_anchor`，风控锚兼任会计锚）；
          exit  = 本次离场 fill；费率各按自己成交的 offset 档
          （入场档 `_run_entry_offset`、离场档 `exit_offset`，
          单边费 SSOT 仍在品种档案 Fee，`Instrument.single_fee` 只做档位选择）。

        离场路径全部收敛到这里，防双写（[S9]）：
          ① 今仓反向开仓（R-OPEN，开仓档）；② 昨仓/两态机平仓（平昨/平今档）；
          ③ shutdown_and_lock_all 强制锁仓（reason=auto_order_off）；
          ④ on_bar 补做 retry（reason=auto_order_off_retry）；
          ⑤ 对账强平（Reconcile，reason=reconcile_external_partial）。
        强制离场时 reason 取强平 reason，不回落到 run 计划名（§7-④）。

        volume：结算手数。正常离场 = 离场委托手数；对账部分强平 = 被删
        仓单手数（run 仍在途、继续管剩余净敞口）。缺省 = 簿内净敞口。

        run 不在途（`_run_side` 为空）→ 返回 None，调用方自行处理。
        """
        if self._run_side is None or self._run_plan is None:
            return None
        vol = int(volume if volume is not None
                  else abs(self.positions.net_volume()))
        if vol <= 0:
            return None
        entry_price = float(self._run_anchor)
        gross = (exit_price - entry_price) * self._run_side.sign
        cost = (self.state.single_fee(entry_price, self._run_entry_offset, vol)
                + self.state.single_fee(exit_price, exit_offset, vol))
        gross_cash = gross * self.state.multiplier * vol
        net_cash = gross_cash - cost
        bars_held = max(0, self.bars_seen - self._run_bar_seq)
        self._trade_seq += 1
        t = Trade(
            trade_id="T{:05d}".format(self._trade_seq),
            symbol=self.state.trade_symbol, side=self._run_side,
            volume=vol, entry_price=entry_price, exit_price=exit_price,
            entry_at=self._run_entry_at or now_cn(), exit_at=at,
            reason=reason, gross_points=round(gross, 4),
            cost_cash=round(cost, 4), net_cash=round(net_cash, 2),
            bars_held=bars_held,
            signal_key=self._run_signal_key,
            exit_plan_name=self._run_plan.name,
            exit_plan_params=self._run_plan.params)
        self.store.save_trade(t)
        return t

    def _write_close_event(self, act: "_Action", o: Order, reason: str,
                           trade_id: str = "") -> None:
        """close 事件（仓单级口径，供对账/回放）。

        事件里的 entry/gross/cost/net 沿用**被平仓单自己的**仓单级口径
        （与 run 级 Trade 口径不同 —— 拆锁场景下被平仓单的 entry 是前一天的
        价）；`trade_id` 由调用方回填 run 结算产出的 ID。拆锁入场不是离场、
        无 run 结算 → 留空，不产生悬空引用（[S10]）。
        """
        pos = act.target
        if pos is None:
            return
        exit_price = o.filled_price
        gross = pos.pnl_points(exit_price)
        # CLOSE 恒作用于跨日仓（`_pre_trade_check` 已断言）→ 恒按平昨档计。
        # 这里仍按 entry_date 动态判定，是为"未来其它调用方"保留防御。
        # 成本读**品种档案 Fee 两档**（元口径，state 提供有效乘数）——
        # 仅用于事件流的仓单级口径，与 run 级 Trade 无关。
        # **品种档案来源 = `self.state.product`**（构造期冻结的那份），
        # `cost_cash` 连入参都不收 —— "传进来的档案 ≠ 决策用的档案"
        # 在结构上不可能发生。
        closetoday = bool(self.state.closetoday_first
                          and pos.entry_date >= self._current_trading_day())
        cost = self.state.cost_cash(pos.entry_price, exit_price,
                                    closetoday=closetoday, volume=pos.volume)
        gross_cash = gross * self.state.multiplier * pos.volume
        net_cash = gross_cash - cost
        bars_held = max(0, self.bars_seen - pos.entry_bar_seq)
        plan = self._run_plan or pos.exit_plan
        self.ev.write("close", symbol=pos.symbol, side=str(pos.side),
                      reason=reason,
                      entry=pos.entry_price, exit=exit_price,
                      gross=round(gross, 4), cost_cash=round(cost, 4),
                      net_cash=round(net_cash, 2), bars_held=bars_held,
                      trade_id=trade_id, exit_policy=plan.name,
                      transition=act.transition,
                      position_signal_key=pos.signal_key)

    # ---------------- 运行态（run）----------------
    # ---------------- 关键动作轻提示（需求 ⑷，2026-09-18） ----------------
    @staticmethod
    def _fmt_px(v) -> str:
        try:
            return "{:g}".format(float(v))
        except (TypeError, ValueError):
            return "-"

    @property
    def _quote_unit(self) -> str:
        """本品种的报价单位（展示用，只进文案；见 Product.quote_unit）。

        取不到档案（鸭子类型 cfg / 未知品种 / 未标定）→ 空串：**不猜单位**，
        文案侧只给数值。用 getattr 兜底是为了测试替身 cfg（没有 product_profile
        属性）—— property 内部抛 AttributeError 同样会被 getattr 的默认值接住。
        """
        prof = getattr(self.cfg, "product_profile", None)
        return str(getattr(prof, "quote_unit", "") or "")

    def _run_R(self):
        """本段 run 的 R 快照（出场计划 params["R"]）。无计划 / 旧库缺失 → None。"""
        if self._run_plan is None:
            return None
        return self._run_plan.params.get("R")

    def _r_value_text(self, r_multiple: float, R) -> str:
        """R 的绝对量值文案：`5 点` / `15 元/吨`（末缀本品种报价单位）。

        R 缺失或非数值（旧 state.db 恢复的持仓 / run_start_incomplete）→ 空串，
        由调用方退化成"只写倍数"：宁可不给数字，也不编一个。
        """
        try:
            val = abs(float(R)) * abs(float(r_multiple))
        except (TypeError, ValueError):
            return ""
        return ("{:g} {}".format(val, self._quote_unit)).strip()

    def _r_label(self, r_multiple: float, R) -> str:
        """把「N R」渲染成带具体量值的文案：`1R（= 5 点）`（需求 ⑶，2026-09-22）。

        只写「1R」用户没法与盘面对齐，故补「= 多少」。两种退化：
        R 缺失 → `1R`；报价单位未标定 → `1R（= 5）`（数值照给，不编单位）。
        """
        mult = "{:g}R".format(float(r_multiple))
        val = self._r_value_text(r_multiple, R)
        return "{}（= {}）".format(mult, val) if val else mult

    def _notify_open(self, o: Order) -> None:
        """开仓成交 toast：方向/手数/成交价 + 止损价与 1R 距离（需求 ⑷(1)、⑶）。

        ⚠️ 文案原为「止损(1R) = 4010」—— 把**距离**（1R）标成了**价格**，是两回事；
        改为「止损 = 4010（距入场 1R = 5 点）」。
        """
        plan = self._run_plan
        stop = plan.stop_price if plan is not None else None
        head = "开仓成交：{} {}手 @ {}".format(
            str(o.side), o.volume, self._fmt_px(o.filled_price))
        if stop:
            r_txt = self._r_value_text(1.0, self._run_R())
            head += ("｜止损 = {}（距入场 1R = {}）".format(self._fmt_px(stop), r_txt)
                     if r_txt else "｜止损 = " + self._fmt_px(stop))
            self.notify(head, code="open_filled")
        else:
            # 无计划（run_start_incomplete 已另有 severe 告警）：只报成交事实
            self.notify(head, code="open_filled")

    def _notify_close(self, o: Order, act: "_Action", reason: str) -> None:
        """平仓/锁仓成交 toast：说明是止盈还是止损（需求 ⑷(2)）。"""
        phase = ""
        if self._run_plan is not None:
            phase = str(self._run_plan.params.get("_phase") or "")
        if reason == "tp":
            label = "止盈"
        elif reason == "sl":
            if phase == "trailing":
                label = "移动止盈（跟踪止损触发）"
            elif phase == "breakeven":
                label = "保本止损"
            else:
                label = "止损"
        elif reason == "auto_order_off_retry":
            label = "关闭自动下单离场"
        else:
            label = "离场"
        body = "{} {}手 @ {}".format(str(o.side), o.volume,
                                     self._fmt_px(o.filled_price))
        if act.intent is OrderIntent.OPEN:
            # 今仓离场 = 反向开仓锁仓（转移④）：o.side 是对冲方向
            self.notify("锁仓离场·{}：反向开 {}".format(label, body),
                        code="close_filled")
        else:
            self.notify("平仓成交·{}：{}".format(label, body),
                        code="close_filled")

    def _notify_run_phase(self, phase: str) -> None:
        """盈利达标阶段 toast（需求 ⑷(3)(4)）：breakeven_trigger_r·R 保本 /
        r_multiple_tp·R 移动止盈。

        文案带 R 的**绝对量值 + 本品种报价单位**（需求 ⑶，2026-09-22）：R 的单位是
        "报价点数"（IF 是点、CU 是元/吨），只写「1R」用户无法与盘面对齐，故渲染成
        「1R（= 5 点）」。R 拿不到时退化为只写倍数（不编数字）。
        """
        pol = self.exit_policy
        R = self._run_R()
        if phase == "breakeven":
            trigger = getattr(pol, "breakeven_trigger_r", 1.0)
            stop = self._run_plan.stop_price if self._run_plan else None
            msg = "盈利达到 {}，进入保本策略".format(self._r_label(trigger, R))
            if stop:
                msg += "：止损已移至 " + self._fmt_px(stop)
            self.notify(msg, code="run_breakeven")
        elif phase == "trailing":
            trigger = getattr(pol, "r_multiple_tp", 2.0)
            self.notify("盈利达到 {}，进入移动止盈（跟踪止损启动）".format(
                self._r_label(trigger, R)), code="run_trailing")

    def _run_start(self, anchor_price: float, bar: Optional[Bar],
                   sig: Optional[Signal],
                   entry_offset: str = "") -> None:
        """开启一段 run（净敞口 0 → 非 0）。**风控锚 = 本次成交价**（D1）。

        entry_offset：入场 fill 的 offset 档（INTENT_TO_OFFSET 值域），
        供 run 结算取入场费率档（拆锁入场是平昨/平今档，v3.1 §5.1-2）。
        """
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
            # 置上通知锁，否则同一次异常会被 `_sync_state` 里的
            # `_check_run_anchor` 再报一条 `run_missing_anchor` —— 同一根因、
            # 两个 code，用户看到两个弹窗（D11 队列按 code 合并，两条都会弹）。
            # 本条已经把成因说清楚了，后续自检不必重复喊。见 test_p43 [7]。
            self._run_missing_notified = True
            return
        side = Side.LONG if net > 0 else Side.SHORT
        plan = self.exit_policy.plan(sig, anchor_price, self.state,
                                     anchor=anchor_price)
        self._run_side = side
        self._run_anchor = anchor_price
        self._run_volume = abs(net)
        self._run_bar_ts = (bar.timestamp if bar is not None
                            else (self.last_bar.timestamp if self.last_bar else 0))
        self._run_bar_seq = self.bars_seen
        self._run_signal_key = sig.key
        self._run_entry_offset = entry_offset
        self._run_entry_at = now_cn()
        self._run_plan = plan
        self.ev.write("run_start", side=str(side), volume=self._run_volume,
                      anchor=anchor_price, stop=plan.stop_price,
                      tp=plan.tp_price, exit_policy=plan.name,
                      signal_key=sig.key, entry_date=self._current_trading_day(bar),
                      entry_offset=entry_offset)

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
        self._run_entry_offset = ""
        self._run_entry_at = ""
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
            symbol=self.state.trade_symbol, side=self._run_side,
            volume=abs(self.positions.net_volume()),
            entry_price=self._run_anchor, entry_at="",
            entry_bar_ts=self._run_bar_ts, entry_bar_seq=self._run_bar_seq,
            signal_key=self._run_signal_key, open_order_id="",
            exit_plan=self._run_plan,
            entry_date=trading_day_of_ms(self._run_bar_ts))

    # ---------------- 离场执行（转移 ④⑤ 的执行体）----------------
    def _force_exit(self, bar: Optional[Bar], reason: str,
                    trigger_price: Optional[float] = None,
                    force: bool = False) -> Optional[Order]:
        """无条件离场。两个调用方走**同一张转移表**（规则 ⑹）：

          · `_settle_positions` —— L1-L3 触发，trigger_price = 触发价
          · 自动下单关闭       —— 无触发价，用最新收盘价

        今仓 → 反向 OPEN（转移 ④）；跨日仓 → CLOSE（转移 ⑤）。

        返回值：本次离场动作产生的委托
          · `None`   —— 没有离场动作要发（账本已空 / 不在运行态）→ 无需离场
          · `Order`  —— 发过一笔委托（**不代表成交**；未成交时 `_execute`
                        内部已走拒单告警路径并返回 None，故此处拿到的是
                        `Order` 即已 filled）
        收尾路径靠它区分"已清仓"与"离场没成功"。
        """
        act = self._decide_exit(bar)
        if act is None:
            return None
        price = trigger_price
        if not price:
            # bar 为空时用 self.last_bar 兜底：否则 _execute 内的 today 会退化到
            # **墙钟**（回放 / 补锁场景下与真实交易日不同）→ 今仓被误判成昨仓。
            for anchor in (bar, self.last_bar):
                if anchor is not None and getattr(anchor, "close", 0):
                    price = float(anchor.close)
                    break
        return self._execute(act, ref_price=float(price or 0.0), bar=bar,
                             reason=reason, force=force)

    # ════════════════════════════════════════════════════════════════
    # 自动下单关闭（前端开关 → 进程托管触发）
    #   关闭语义（用户拍板，确认保留）：
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
        """自动下单关闭入口（main.py 收到退出信号时调用）。幂等：重复调用安全。

        **收尾确认信号**
          关闭之后必须给用户一个**明确的终局结论** —— 要么"已清仓、无残留"，
          要么"仍有残留（哪些）"。此前的实现只覆盖了"锁仓态"一种，于是：
            · 已清仓（FLAT）→ **一声不响**，用户无法确认是否真的清干净了；
            · 离场失败（平仓被拒 / 没成交）→ 账户仍是运行态、带着仓过夜，
              而**没有任何提示** —— 这正是本条要消灭的"无人知"。
          现在三种终局都发告警（关闭时刻必然可见：落库持久直到确认；
          不跨会话重播 —— `_load_alerts` 启动清场）：
            · FLAT   → warn：已清仓，无残留（正向确认，不吓人）；
            · RUNNING→ severe：**仍有净敞口**，离场没成功，必须人工处理；
            · LOCKED → warn：停在锁仓态（原有的 account_frozen 语义，保留）。
        """
        self.auto_order_enabled = False
        # bar=None → 用 self.last_bar 兜底。两者都为空（进程启动后从未收到 K 线）
        # 时由 _current_trading_day 的墙钟兜底接手 —— 那必然是无 K 线驱动的
        # 托管关闭场景，墙钟交易日与真实交易日一致。
        # force=True：关闭是用户当面点下的收尾动作，不排队等冷却（否则刚被拒
        # 过一次平仓时，关闭会"看起来什么都没做"）
        before_state = self.account_state()
        before_net = self.positions.net_volume()
        exit_order = self._force_exit(self.last_bar, reason=reason, force=True)
        self._persist()
        self._sync_state()
        state = self.account_state()
        net = self.positions.net_volume()
        n_pos = len(self.positions)
        self.ev.write(
            "auto_order_off", reason=reason,
            account_state=state.value,
            net_volume=net,
            positions_n=n_pos,
            exit_submitted=exit_order is not None,
            state_before=before_state.value,
            net_before=before_net,
            note="停止接收信号 + 运行态持仓已按规则 ⑹ 离场（今仓反向开仓 / 昨仓平仓）")

        # ── 终局确认（第 5 条）：三态各自一条，保证"清干净了 / 还有残留"必然可见 ──
        if state is AccountState.FLAT:
            self.ev.write(
                "shutdown_result", reason=reason, result="flat", positions_n=0,
                note="关闭收尾完成：账户空仓，无残留持仓")
            self.alert(
                self.ALERT_WARN, "shutdown_result_flat",
                "自动下单已关闭 —— **已清仓，无残留持仓**（净敞口 0，账本空）。"
                "可以放心离场。",
                reason=reason, net_volume=0, positions_n=0)
        elif state is AccountState.RUNNING:
            # 离场没成功（被拒 / 未成交 / 没有可用的离场动作）→ 带仓过夜风险。
            self.ev.write(
                "shutdown_result", reason=reason, result="residual",
                positions_n=n_pos, net_volume=net,
                exit_submitted=exit_order is not None,
                note="关闭收尾后仍有净敞口：离场未成功，需人工处理")
            self.alert(
                self.ALERT_SEVERE, "shutdown_result_residual",
                "自动下单已关闭，但**账户仍有净敞口 {}（{} 笔仓单）** —— "
                "离场未成功（平仓被拒 / 未成交）。继续持有将带入下一交易日并承担"
                "跳空风险，请在柜台人工平仓或重启引擎后重新关闭。".format(
                    net, n_pos),
                reason=reason, net_volume=net, positions_n=n_pos,
                exit_submitted=exit_order is not None)
        else:  # AccountState.LOCKED
            # 锁仓（净敞口 0）：盈亏锁定，不是危险状态，保留原有 account_frozen 语义。
            self.ev.write(
                "account_frozen", reason=reason,
                positions_n=n_pos, net_volume=0,
                note="关闭后账户停在锁仓态（净敞口 0）：引擎无自动清仓路径，"
                     "需人工平仓或重新开启自动下单后由对向信号拆锁")
            self.ev.write(
                "shutdown_result", reason=reason, result="locked",
                positions_n=n_pos, note="关闭收尾完成：账户停在锁仓态（净敞口 0）")
            self.alert(
                self.ALERT_WARN, "account_frozen",
                "自动下单已关闭，账户停在锁仓态（净敞口 0，持有 {} 笔仓单、"
                "盈亏不再变动）。引擎无自动清仓路径：需人工平仓，或重新开启"
                "自动下单后由对向信号拆锁。".format(n_pos),
                positions_n=n_pos, reason=reason)

    # ════════════════════════════════════════════════════════════════
    # D11 告警队列
    #   触发源两类（分析文档）：
    #     ① D10 判出的**不可挽救拒单** —— 资金不足 / 非交易时段 / 无权限 / 无此持仓
    #     ② 离场追价跑满 `chase_max_number` 仍未成交（价格不可达，追不动了）
    #   判定都不在这里：由 broker 的 D10 分类器给结论（写在 Order.meta）；
    #   本段只负责**怎么存、怎么给前端**。
    # ════════════════════════════════════════════════════════════════
    ALERT_SEVERE = "severe"          # 前端 showAlert 阻塞弹窗
    ALERT_WARN = "warn"              # 前端 toast 轻提示
    _ALERTS_KV = "alerts"
    _ALERTS_ACK_KV = "alerts_ack_ts"
    _ALERTS_KEEP = 20                # 队列上限：再多也只会淹没前端

    # CTP 拒单分类（D10）→ (告警码, 级别, 标题)
    #   · price（FOK 全撤）**不进这张表**：那是"追了有用"的一类，引擎会
    #     继续追；只有追满 chase_max_number 仍不成交才升级（见 `_alert_on_reject`）。
    #   · position（平仓量超过持仓量 / 平昨仓不足）2026-09-13 独立成项：
    #     柜台明确说"没有这笔可平仓"的信号。
    _REJECT_ALERTS = {
        "funds": ("ctp_reject_funds", "severe",
                  "柜台拒单：资金 / 保证金不足"),
        "not_tradable": ("ctp_reject_not_tradable", "severe",
                         "柜台拒单：当前不可报单（非交易时段 / 无权限）"),
        REJECT_POSITION: ("ctp_reject_position", "severe",
                          "柜台拒单：可平持仓不足（柜台很可能没有这笔仓）"),
    }

    def _drain_broker_alerts(self) -> None:
        """broker 侧告警回流 D11（O-2/O-3）。

        把 Broker.notify() 暂存的告警（instrument 超时 / nan / 与配置不一致等
        诊断）逐条转手 `Engine.alert`（沿用 D11 通道：severe → 阻塞弹窗、
        warn → toast；同 code 在 alert 内自动合并计数，不会刷屏）。
        鸭子兼容：broker 未实现 drain_alerts（如测试替身 / 裸通道）→ 跳过。
        """
        drain = getattr(self.broker, "drain_alerts", None)
        if not callable(drain):
            return
        try:
            pending = drain()
        except Exception:
            return
        for a in pending or []:
            extra = {k: v for k, v in a.items()
                     if k not in ("level", "code", "msg")}
            self.alert(str(a.get("level") or self.ALERT_WARN),
                       str(a.get("code") or "broker_alert"),
                       str(a.get("msg") or ""),
                       **extra)

    _TOASTS_KV = "toasts"
    _TOASTS_KEEP = 30                # 轻提示队列上限：前端按 ts 水位去重，历史自然过期

    def notify(self, msg: str, code: str = "", **extra) -> None:
        """轻提示（需求 ⑷，2026-09-18）：关键动作即时 toast，前端 5 秒自动消失。

        与 alert 的分工：alert 是「需要人工介入」的持久队列（同 code 合并计数
        + ack 水位确认）；notify 是「刚才发生了什么」的瞬时播报 —— 开仓/平仓/
        保本/移动止盈/账单同步。不合并、不需确认：同 code 的两次开仓是两个
        独立事件，都要弹。列表有界（尾部 _TOASTS_KEEP 条落 kv），前端首次
        拉取只定水位、不回放历史。
        """
        now = time.time()
        rec: Dict[str, Any] = {"ts": now, "msg": msg}
        if code:
            rec["code"] = code
        if extra:
            rec.update(extra)
        try:
            items = self.store.get_json(self._TOASTS_KV) or []
            if not isinstance(items, list):
                items = []
            items = [t for t in items if isinstance(t, dict)]
            items.append(rec)
            self.store.set_json(self._TOASTS_KV, items[-self._TOASTS_KEEP:])
        except Exception as e:
            self.ev.write("toast_persist_failed",
                          reason="{}: {}".format(type(e).__name__, e))
        self.ev.write("toast", msg=msg, code=code)

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
                # ⚠️ 级别**只升不降**
                #   合并时若把 level 直接覆盖成本次的值，则"同因连拒达阈值 →
                #   从 warn 升级 severe"这条永远不生效 —— 首个 warn 条目会
                #   一直把后面来的 severe 覆盖回去（方向写反）。
                #   反过来（severe → warn）也必须挡住：一旦某次故障已被判为
                #   "需人工介入"，后续同类抖动不该把弹窗降级成 toast。
                if (level == self.ALERT_SEVERE
                        and a.get("level") != self.ALERT_SEVERE):
                    prev_level = str(a.get("level"))
                    a["level"] = self.ALERT_SEVERE
                    self.ev.write("alert_escalated", code=code,
                                  from_level=prev_level, n=a["n"])
                self.ev.write("alert", level=a.get("level"), code=code, n=a["n"],
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
        """恢复告警队列与 ack 水位（D11 落库的目的就是"重启后还在"）。

        跨会话清场：上一场次的**关闭收尾告警**（`shutdown_result_*` 与
        锁仓的 `account_frozen`）在本次启动时清掉，不重播。它们描述的是
        "上一次关闭时账户的归宿"，观众是关闭时刻的用户；用户次日重新
        开启自动下单时再弹"自动下单已关闭……"只会误导（眼前明明是开启
        动作，2026-09-18 用户实录）。清场不丢事实：新会话的真实状态由
        自身重建（持仓恢复后状态面板可见，对账盲区另有 position_mismatch
        兜底），历史记录在 events.jsonl 永久可查。其余告警（如
        position_mismatch）照旧跨重启保留——它们描述的现状不随会话结束
        而消失。清场规则唯一存放于 Records.is_closure_alert（API 侧拉起前
        也清，两处口径不许漂移）。
        """
        self._alerts_ack_ts = float(
            self.store.get_json(self._ALERTS_ACK_KV, 0.0) or 0.0)
        raw = self.store.get_json(self._ALERTS_KV)
        items = raw if isinstance(raw, list) else []
        keep = [a for a in items if isinstance(a, dict) and a.get("code")]
        if self._alerts_ack_ts:
            keep = [a for a in keep
                    if float(a.get("ts") or 0.0) > self._alerts_ack_ts]

        dropped = sum(1 for a in keep if is_closure_alert(a.get("code")))
        self._alerts = [a for a in keep
                        if not is_closure_alert(a.get("code"))][
            -self._ALERTS_KEEP:]
        if dropped:
            self.ev.write("alert_session_prune", removed=dropped,
                          note="上一场次关闭收尾告警已清场，不跨会话重播")
            self._persist_alerts()

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
        if (cls_ == REJECT_PRICE and act.is_exit
                and int(o.meta.get("attempt") or 0)
                >= int(o.meta.get("max_attempts") or 1)):
            self.alert(self.ALERT_SEVERE, "close_chase_exhausted",
                       "离场追价跑满 {} 轮仍未成交 —— {} {} {} 手，"
                       "净敞口没有变动，请在柜台人工处理。".format(
                           o.meta.get("max_attempts"), o.symbol,
                           act.intent.value, str(o.side)),
                       signal_key=o.signal_key, reject_class=cls_)

    # 报单前校验拒单 → 告警分级表
    #   键 = `_pre_trade_check` 返回的原因字符串；(告警码, 级别)。
    #
    #   ⚠️ **只列"自己不会告警"的原因**。有两个原因在 `_pre_trade_check`
    #      自己的分支里**已经**发过告警，绝不能在这里重发一次，否则同一次拒单
    #      会弹两个框（本探针首版就踩了这个坑，实测 1 次拒单产生 2 条告警）：
    #        · `instrument_unverified` —— 已发 severe；
    #        · `delivery_guard_blocked` —— 已发 severe。
    #      这些"已告警"的原因**仍会**被 `_note_precheck_reject` 计数（见该函数
    #      `_PRECHECK_SELF_ALERTED`），只是不再重复发告警 —— 计数用于升级，
    #      但既然本来就 severe，升级无意义，故直接跳过。
    #
    #   分级依据："这件事会不会自己好？"
    #     · 自己不会好、且必须有人处理 → severe（前端 showAlert 阻塞弹窗）；
    #     · 一过性、行情/状态一到就恢复     → warn（前端 toast 轻提示）。
    #
    #   ⚠️ 这张表**只管可见性**，不改任何判定 —— 拒单与否仍由 `_pre_trade_check`
    #      决定（fail-closed 底线不动，用户已明确）。
    _PRECHECK_ALERTS = {
        # CLOSE 的账实不符类：簿面与柜台对不上，必须有人核对，不会自己好。
        "close_volume_exceeds_target": ("precheck_close_volume_mismatch",
                                        "severe"),
        "close_volume_below_target": ("precheck_close_volume_mismatch",
                                      "severe"),
        "close_without_target": ("precheck_close_no_target", "severe"),
        "close_target_no_entry_date": ("precheck_close_no_entry_date",
                                       "severe"),
        # 日期口径错位：可能造成平今/平昨费率错收或柜台拒单，需人工判。
        "closetoday_target_is_yesterday": ("precheck_date_mismatch",
                                          "severe"),
        "close_target_is_today": ("precheck_date_mismatch", "severe"),
        # 动作与品种能力不符：属于配置/代码问题，不会自己好。
        "closetoday_not_supported": ("precheck_closetoday_unsupported",
                                     "severe"),
        # 一过性：缺时间锚 / 交易日 / 手数，下一根 bar 往往就好了。
        "no_time_anchor": ("precheck_no_time_anchor", "warn"),
        "no_trading_day": ("precheck_no_trading_day", "warn"),
        "zero_volume": ("precheck_zero_volume", "warn"),
    }

    # 这两个原因在 `_pre_trade_check` 内**已经**发过告警（见上方注释），
    #   `_note_precheck_reject` 遇到它们只计数、不重发。
    _PRECHECK_SELF_ALERTED = frozenset({
        "instrument_unverified", "delivery_guard_blocked",
    })

    # 同一原因连续被拦多少次 → 升级为 severe（即便该原因本身只是 warn 档）。
    #   "偶发一次"和"卡死了"必须能区分：前者升级成弹窗会让人麻木，后者不弹窗
    #   就等于静默卡住。取 3：一根 bar 一笔，15s 周期下约 45 秒还没好，
    #   基本可以断定不是抖动。
    _PRECHECK_ESCALATE_AT = 3

    def _note_precheck_reject(self, act: "_Action", why: str,
                              sig: Optional[Signal] = None) -> None:
        """把"报单前校验被拒"升级成用户可见的告警。

        覆盖两处原本静默的卡住（见 `__init__` 内 `_reject_streak` 注释）：
          ① 拒单原因送不到界面（-⑻）；
          ② 连续被同一原因拒绝（含"合约参数取不到"）—— 从"偶发"升级为"卡死"。

        复用既有 D11 通道（`self.alert`），因此**自动获得**同 code 合并计数与
        确认水位 —— 连续拒 100 次在队列里仍只有 1 条、n=100，不会刷屏。
        `_reject_streak` 只负责"要不要把级别从 warn 提到 severe"。
        """
        # 已自行告警的原因：只计数（供诊断），不重发，避免同一次拒单弹两个框。
        if why in self._PRECHECK_SELF_ALERTED:
            if why == self._reject_streak_code:
                self._reject_streak += 1
            else:
                self._reject_streak_code = why
                self._reject_streak = 1
            return
        code, level = self._PRECHECK_ALERTS.get(
            why, ("precheck_rejected_" + (why or "unknown"), self.ALERT_WARN))
        # 连续计数：同一原因才算"连续"；换了原因从头数起（否则 A 拦两次 + B 拦
        # 一次会把 B 误升级）。
        if why == self._reject_streak_code:
            self._reject_streak += 1
        else:
            self._reject_streak_code = why
            self._reject_streak = 1
        escalated = False
        if (level != self.ALERT_SEVERE
                and self._reject_streak >= self._PRECHECK_ESCALATE_AT):
            level = self.ALERT_SEVERE
            escalated = True
        who = ((act.target.symbol if act.target is not None else "")
               or self.state.trade_symbol or self.state.signal_symbol)
        self.alert(
            level, code,
            "报单前校验未通过，已拒单（原因：{}）—— {} {} {} 手，"
            "未向柜台发出任何委托。{}".format(
                why, who, act.intent.value, str(act.side), act.volume,
                ("已连续被拒 {} 次，请人工介入（行情/配置可能有问题）。"
                 .format(self._reject_streak)) if escalated else
                ("连续第 {} 次。" .format(self._reject_streak)
                 if self._reject_streak > 1 else "")),
            reason=why, streak=self._reject_streak,
            signal_key=(sig.key if sig is not None else ""),
            transition=act.transition, escalated=escalated)

    def auto_order_status(self) -> Dict[str, Any]:
        """自动下单状态快照（供后端进程托管 / API / 前端轮询）。"""
        return {
            "enabled": self.auto_order_enabled,
            # 买卖点类型过滤的**实际生效值**（None = 用户从未推送，全部放行）。
            # 与 K 线图「显示设置」的勾选同源；放在这里是为了让"自动下单现在
            # 到底认哪几类"可被查询，而不是只能靠猜。
            "bsp_type_filter": self.bsp_type_filter(),
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
        }

    # ---------------- 统计 ----------------
    def summary(self) -> Dict[str, Any]:
        # 统计口径改**元**（net_cash）。原"点"口径的
        # net_points 已随 Trade.cost_points→cost_cash 删除 —— per_lot 费率档
        # 无法在点数口径无损表达，净值只有元是自洽的；毛利仍有点口径
        # （gross_points），净统计一律 net_cash。
        trades = self.store.trades()
        n = len(trades)
        wins = [t for t in trades if t["net_cash"] > 0]
        losses = [t for t in trades if t["net_cash"] <= 0]
        cash = sum(t["net_cash"] for t in trades)
        by_reason: Dict[str, Any] = {}
        for t in trades:
            r = t["reason"]
            by_reason.setdefault(r, {"n": 0, "net": 0.0})
            by_reason[r]["n"] += 1
            by_reason[r]["net"] = round(by_reason[r]["net"] + t["net_cash"], 2)
        return {
            "trades": n,
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / n, 4) if n else 0.0,
            "avg_win": round(sum(t["net_cash"] for t in wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(t["net_cash"] for t in losses) / len(losses), 2) if losses else 0.0,
            "net_cash": round(cash, 2),
            "expectancy_cash": round(cash / n, 2) if n else 0.0,
            "by_reason": by_reason,
            # G1：不用 self.position（legacy_single 在多仓会抛）；直接列全部持仓。
            "open_position": ([p.to_dict() for p in self.positions.positions] or None),
            "exit_policy": self.exit_policy.describe(),
            "entry_policy": self.entry_policy.describe(),
            # tick / 乘数取 **state 的有效值**（引擎实际使用的口径）；
            #   symbol / 交易合约仍取静态 spec。
            "spec": {"signal_symbol": self.state.signal_symbol,
                     "trade_symbol": self.state.trade_symbol,
                     "price_tick": self.state.price_tick,
                     "multiplier": self.state.multiplier},
        }
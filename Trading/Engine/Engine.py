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
    PLAUSIBLE_DATE_MIN,
    AccountState,
    Bar, DecisionType, PositionOrigin, EngineState, ExitMode, ExitPlan, Order, OrderIntent,
    Position, Side, Signal, Trade, now_cn, now_ms, trading_day_from_clock,
    trading_day_of_ms,
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

        # Phase E1（2026-09-05）：引入 PositionBook 容器，为 E2 (UNLOCK_UPGRADE) / E3 (N≥1)
        # 多仓场景预留扩展点。E1 阶段 max=1，语义与单一 self.position 完全等价。
        # Phase E3.1（2026-09-05）：max 改为 cfg.risk.max_open_positions 配置化，默认仍=1
        # —— 所有现存测试（P5..P13）零行为变化。
        # v1.3（Q5 拍板）：max_positions=None —— 不限容量。锁层/笔数不做任何上限，
        #   资金是唯一闸门（钱不够自然开不成功）。这同时消掉了 D2a（add 抛错崩网关）/
        #   D2b（LOCK 落簿静默丢失持仓）/ D3（恢复截断丢失持仓）。
        self.positions: PositionBook = PositionBook(max_positions=None)
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
        # v1.3（S4）：锁仓配对自增序号（生成 lock_pair_id 用，独立于 Trade 序号）。
        self._lock_pair_seq = 0
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
        #          LOCK 软离场落簿反向 SOFT_EXIT_LOCK 仓，次日对向信号解锁）
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
    # 账户三态（P1 SSOT，2026-09-10）
    #   用户口径 ⑴：账户只有三种状态 —— 空仓 / 锁仓 / 运行。
    #   本方法是**全库唯一**的账户态判定点。此前该判定式在 4 处内联重复：
    #     _restore（推初始 state）/ on_signal（运行态忽略信号）/
    #     _close_positions（批次尾部回 IDLE）/ _unlock_position（解锁后回 IN_TRADE|IDLE）
    #   4 份 all(...)/any(...) 写法互为逆否，口径靠"人肉保持一致"，任一处漂移
    #   都会让"运行态忽略信号"与"解锁后回 IN_TRADE"打架。收口到此处。
    #   —— 判定口径与 Old 完全一致（纯重构，零行为变化）：
    #      · 簿空            → FLAT
    #      · 簿内全是 SOFT   → LOCKED（含"今日锁"与"昨日锁"，两者都等信号）
    #      · 否则            → RUNNING
    # ════════════════════════════════════════════════════════════════
    def account_state(self) -> AccountState:
        """账户三态判定（唯一实现点）。详见 `AccountState` 文档。"""
        poss = self.positions.positions
        if not poss:
            return AccountState.FLAT
        if all(p.origin is PositionOrigin.SOFT_EXIT_LOCK for p in poss):
            return AccountState.LOCKED
        return AccountState.RUNNING

    # ════════════════════════════════════════════════════════════════
    # 当前交易日（SSOT，2026-09-10 立）
    #   规则 ⑸ 的 today 口径**唯一实现点**。此前 2 处各自现算：
    #     on_signal        : sig.date[:10] if sig.date else ""
    #     _close_positions : bar.date[:10] if bar.date else now_cn()[:10]
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
        而 _close_positions 早有 now_cn() 兜底 —— 两处口径本就不一致）。
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

    # ---------------- 旧 schema 闸门（2026-09-10 改名配套） ----------------
    # 持仓记录的键由 entry_mode 改为 origin（枚举改名 PositionOrigin）。
    # 若不设闸门：旧 state.db 里的锁仓持仓（原 entry_mode="locked"）会被
    # Position.from_dict 静默回退成 SIGNAL_OPEN —— 引擎把它当"真实净敞口"，
    # 接进 L1-L3 止盈止损，可能对锁仓持仓发平仓单（账实不符）。
    # 而 _reconcile_positions 只在 real_vol > engine_vol 时**告警**、不纠正，
    # 兜不住这个错。故宁可拒绝启动，让用户显式处理旧库。
    _LEGACY_POSITION_KEYS = ("entry_mode",)

    @staticmethod
    def _legacy_position_records(records) -> list:
        """挑出仍含旧键的持仓记录（纯函数，便于单测）。"""
        return [d for d in records if isinstance(d, dict)
                and any(k in d for k in TradingEngine._LEGACY_POSITION_KEYS)]

    def _reject_legacy_state(self, records) -> None:
        """检出旧 schema 持仓记录时抛错，拒绝启动。"""
        legacy = self._legacy_position_records(records)
        if not legacy:
            return
        keys = "/".join(self._LEGACY_POSITION_KEYS)
        self.ev.write("state_schema_incompatible",
                      legacy_key=keys, legacy_n=len(legacy),
                      note="旧 schema 持仓记录，拒绝恢复，避免锁仓持仓被误判为敞口持仓")
        raise RuntimeError(
            "state.db 的持仓记录仍是旧 schema（含 {} 键），与当前代码不兼容：\n"
            "  改名后键为 origin；旧锁仓持仓会被恢复成 SIGNAL_OPEN（敞口持仓），\n"
            "  进而被纳入 L1-L3 止盈止损，可能对锁仓持仓发平仓单。\n"
            "  处理：确认账户无未了结持仓后，删除 Trading/State/state.db 再启动。"
            .format(keys))

    # ---------------- 记录完整性闸门（2026-09-10） ----------------
    # 两类"记录写不出来、只能被外部污染进来"的持仓记录，一律拒绝启动：
    #
    #   (A) 缺 `origin` 键 —— `Position.from_dict` 会静默降级成 SIGNAL_OPEN。
    #       任何版本写出的记录都带 `entry_mode` 或 `origin`（前者已被上一道闸门
    #       拦下），故"两个键都没有"记录不了任何来源 → 无法判定它是不是锁仓仓，
    #       若它其实是锁仓仓，会被当"真实净敞口"接进 L1-L3 止盈止损 → 对锁仓仓
    #       发平仓单（账实不符）。
    #
    #   (B) `origin == soft_exit_lock` 但 `lock_pair_id` 空/缺 ——
    #       SOFT_EXIT_LOCK 必然属于某个锁对：全仓只有 2 个写入点，都在
    #       `_book_lock_pair` 内，两笔用的是**同一个局部变量** pair_id
    #       （Engine._book_lock_pair）。且 lock_pair_id 与 origin 同源改名而来，
    #       所有能写锁仓记录的版本都给两笔写 id → 空 id 的 SOFT_EXIT_LOCK
    #       在自然流程与任何历史版本下都写不出来。
    #       后果：`_upgrade_lock_pair` 首行 `if not locked_pos.lock_pair_id: return`
    #       静默返回（该仓永不升级、一个事件都不写），而 `account_state()` 只按
    #       origin 判 → 账户被判 LOCKED，该仓永久停在 SOFT_EXIT_LOCK
    #       （不止盈止损、不参与 EOD），唯一出路是人工介入。
    #
    # 不变量不是"lock_pair_id 必须非空"：未锁仓的敞口持仓 lock_pair_id 本就为空，
    # 这是合法的（见 (B) 只对 soft_exit_lock 生效）。
    #
    # 处置口径与既有三道闸门一致（reference：`_reject_legacy_state` /
    # `_assert_lock_pair_invariant` / entry_date 三源全空）——拒绝启动，
    # 让操作者在完整上下文（events.jsonl / 快期3 持仓 / gateway.log）里处理。
    # 不做自动迁移：这类记录自然发生率为 0，判错则引擎会在后续解锁时"自信地"
    # 平错一笔/升级错一笔，与 phantom 清仓同级（不可逆、账实不符）。
    _REQUIRED_RECORD_KEYS = ("origin",)
    _ORPHAN_LOCK_ORIGIN = "soft_exit_lock"

    @staticmethod
    def _incomplete_position_records(records) -> dict:
        """挑出不合契约的持仓记录（纯函数，便于单测）。

        返回 {"missing_origin": [...], "orphan_lock": [...]}，两个列表都是**记录本身**
        （便于错误信息里回显 symbol / signal_key）。
        """
        missing_origin, orphan_lock = [], []
        for d in (records or []):
            if not isinstance(d, dict):
                continue
            if any(k not in d for k in TradingEngine._REQUIRED_RECORD_KEYS):
                missing_origin.append(d)
                continue
            if (str(d.get("origin") or "") == TradingEngine._ORPHAN_LOCK_ORIGIN
                    and not str(d.get("lock_pair_id") or "").strip()):
                orphan_lock.append(d)
        return {"missing_origin": missing_origin, "orphan_lock": orphan_lock}

    def _reject_incomplete_records(self, records) -> None:
        """检出不合契约的持仓记录时抛错，拒绝启动。"""
        bad = self._incomplete_position_records(records)
        if not (bad["missing_origin"] or bad["orphan_lock"]):
            return
        detail = {
            "missing_origin": [
                {"symbol": d.get("symbol"), "signal_key": d.get("signal_key"),
                 "keys": sorted(d.keys())}
                for d in bad["missing_origin"]],
            "orphan_lock": [
                {"symbol": d.get("symbol"), "signal_key": d.get("signal_key"),
                 "lock_pair_id": d.get("lock_pair_id")}
                for d in bad["orphan_lock"]],
        }
        self.ev.write(
            "position_record_incomplete",
            n_missing_origin=len(bad["missing_origin"]),
            n_orphan_lock=len(bad["orphan_lock"]),
            detail=detail,
            note="持仓记录缺 origin 键 / 锁仓记录缺 lock_pair_id；正常路径与任何历史"
                 "版本都写不出这类记录，判定为 state.db 被外部污染，拒绝启动")
        raise RuntimeError(
            "state.db 有 {} 笔持仓记录不合契约，拒绝启动：\n"
            "  · 缺 origin 键：{} 笔 —— `from_dict` 会静默降级成 SIGNAL_OPEN，\n"
            "    若它其实是锁仓仓，会被当真实净敞口纳入 L1-L3 止盈止损。\n"
            "  · soft_exit_lock 但 lock_pair_id 为空：{} 笔 —— 该仓永不升级\n"
            "    （`_upgrade_lock_pair` 首行静默 return）、永久停在软离场态，\n"
            "    不参与止盈止损与收盘强平。\n"
            "  原因：这两类记录自然流程与任何已发布版本都写不出来（锁仓落簿只有\n"
            "        2 个写入点，共用同一个 pair_id；origin 与 lock_pair_id 同源），\n"
            "        出现本错误说明 state.db 被手工改过 / 第三方工具改过 /\n"
            "        多进程混写过。\n"
            "  处理：确认账户无未了结持仓后，删除 Trading/State/state.db 再启动。\n"
            "        删库后引擎从零重建（Store 用 CREATE TABLE IF NOT EXISTS），\n"
            "        前提是账户已无未了结持仓 —— 库空时引擎不会对账，\n"
            "        不能靠对账把实盘已有的仓认回来。\n"
            "  明细：{}".format(
                len(bad["missing_origin"]) + len(bad["orphan_lock"]),
                len(bad["missing_origin"]), len(bad["orphan_lock"]), detail))

    # ════════════════════════════════════════════════════════════════
    # R3（2026-09-10）：锁仓配对不变量断言
    #   「一个锁对恰好 2 笔成员（原仓 + 反向仓，方向相反）」是**构造性**成立的：
    #     · lock_pair_id 全仓只有 2 个写入点，都在 `_book_lock_pair` 内
    #       （原仓 `pos.lock_pair_id` 与新建反向仓 `lock_pos.lock_pair_id`），
    #       且用的是**同一个局部变量** pair_id；
    #     · pair_id 来自单调序号（R1 已跨重启恢复），不会重复；
    #     · 成员只会被 remove（解锁 / 平仓 / 对账），不会再有人加入同一组；
    #     · `_upgrade_lock_pair` 只改 origin，不动 id。
    #   故成员数 ≥3 或"2 笔同向"的触发条件只有一个：**库被外部污染**
    #   （手工改过 / 旧版序号复用残留 / 多进程共用一份库）。
    #
    #   为什么是"断言拒绝启动"而不是"拒绝升级 + 告警"的业务策略：
    #     这分支触发时**不需要人类做业务决策** —— 它就是数据坏了。
    #     而让 `_upgrade_lock_pair` 在 ≥3 笔候选里取第一笔的代价是：
    #       升级错误的仓（错仓接风控）+ 本该升级的仓永久滞留 SOFT_EXIT_LOCK
    #       （`_settle_positions` 显式跳过它 → 不参与 L1-L3 止盈止损、不参与 EOD）。
    #     判断标准：需要业务决策 → 策略；不需要 → 断言。
    # ════════════════════════════════════════════════════════════════
    def lock_pair_groups(self) -> Dict[str, List[Position]]:
        """簿内按 `lock_pair_id` 分组（空 id 不参与 —— 未锁 / 已解锁的合法状态）。"""
        groups: Dict[str, List[Position]] = {}
        for p in self.positions.positions:
            if not p.lock_pair_id:
                continue
            groups.setdefault(p.lock_pair_id, []).append(p)
        return groups

    def _assert_lock_pair_invariant(self) -> None:
        """同一 `lock_pair_id` 至多 2 笔成员，且 2 笔必须方向相反。违反 → 拒绝启动。"""
        groups = self.lock_pair_groups()
        too_many = {k: v for k, v in groups.items() if len(v) > 2}
        same_side = {k: v for k, v in groups.items()
                     if len(v) == 2 and v[0].side is v[1].side}
        if not too_many and not same_side:
            return
        detail = {
            "too_many": {k: [p.signal_key for p in v] for k, v in too_many.items()},
            "same_side": {k: ["{} {}".format(p.side.name, p.signal_key)
                              for p in v] for k, v in same_side.items()},
        }
        self.ev.write("lock_pair_id_invariant_violated",
                      lock_pair_ids=sorted(set(detail["too_many"]) |
                                           set(detail["same_side"])),
                      detail=detail,
                      note="同一 lock_pair_id 出现 >2 笔成员或 2 笔同向；"
                           "正常路径不可达（构造性保证），判定为 state.db 被外部污染，"
                           "拒绝启动以免升级错误的仓 / 锁仓仓永久滞留软离场")
        raise RuntimeError(
            "state.db 的锁仓配对不变量被破坏，拒绝启动：\n"
            "  不变量：同一个 lock_pair_id 至多 2 笔持仓，且两笔方向相反。\n"
            "  实际：{}\n"
            "  后果：解锁时「选要平的仓」（方向 + 最老）与「选要升级的仓」\n"
            "        （lock_pair_id 相同）会脱钩 → 平一笔、升另一笔；\n"
            "        被漏掉的那笔会永久停在 SOFT_EXIT_LOCK（不参与 L1-L3 止盈\n"
            "        止损、不参与 EOD），唯一出路是人工介入。\n"
            "  原因：正常路径下 lock_pair_id 由单调序号生成且只有 2 个写入点，\n"
            "        不会重复 —— 出现本错误说明 state.db 被手工改过 / 由旧版\n"
            "        （序号跨重启归零）写入过 / 被多个进程同时写过。\n"
            "  处理：备份 Trading/State/state.db，核对实盘持仓，必要时删除该库重启。"
            .format(detail))

    # ---------------- 状态恢复 ----------------
    @staticmethod
    def _max_lock_pair_seq_in_state(records) -> int:
        """从持久化持仓记录反推已用过的最大锁对序号（'lock_00007' → 7）。

        R1 的第二道保险：即使 kv 里的 `lock_pair_seq` 丢了，也能从**真实数据**
        推出已用过的最大号，保证新号不会与库内既有锁对相撞。

        刻意扫**全部合约分片**（传进来的 records 是 `positions` 键的原始列表，
        不只当前 trade_symbol）—— 切合约后旧合约的锁对仍留在库里，
        "已用过的号"是全局集合，不能按当前合约重新从 1 开始。
        """
        best = 0
        for d in (records or []):
            if not isinstance(d, dict):
                continue
            s = str(d.get("lock_pair_id") or "")
            if not s.startswith("lock_") or not s[5:].isdigit():
                continue
            n = int(s[5:])
            if n > best:
                best = n
        return best

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
        # 记录完整性闸门（2026-09-10）：缺 origin 键 / soft_exit_lock 缺 lock_pair_id。
        #   与 _reject_legacy_state 同层同源（都只吃"原始记录列表"），放在装载进簿之前 ——
        #   坏数据不许进入状态机。刻意扫**全部合约分片**（不按 my_symbol 过滤）：
        #   与 `_max_lock_pair_seq_in_state` 同口径 —— 切合约后旧合约记录仍在库里，
        #   "这份库能不能用"是全局判断，不能按当前合约重新放行。
        self._reject_incomplete_records(
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

        # R3：锁仓配对不变量闸门（详见 _assert_lock_pair_invariant 注释）。
        #   放在持仓装载后、任何业务逻辑前 —— 坏数据不许进入状态机。
        self._assert_lock_pair_invariant()

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
        # Phase A：根据持仓推断初始 state。在持今仓 → IN_TRADE；无持仓 → IDLE。
        # Phase H1：簿内只剩 SOFT_EXIT_LOCK 锁仓（昨日 LOCK 遗留、等对向信号解锁）→ IDLE，
        #   让下个信号走 on_signal 的 E2 UNLOCK 门（unlock_against_signal 记帐 +
        #   CloseYesterday 费率），而不是 IN_TRADE 的 signal_reverse 平仓路径。
        # P1（2026-09-10）：判定收口到 account_state() —— 非运行态（空仓 / 锁仓）一律 IDLE。
        if self.account_state() is not AccountState.RUNNING:
            self._state = EngineState.IDLE
        else:
            self._state = EngineState.IN_TRADE
        self.bars_seen = int(self.store.get_json("bars_seen", 0) or 0)
        # ════════════════════════════════════════════════════════════════
        # R1（2026-09-10）：三个持久 ID 的序号跨重启恢复。
        #   症状（实测）：`_trade_seq` / `_lock_pair_seq` 是**进程内计数器**，
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
        #   为什么不改成"时间戳 ID"：实测同一毫秒内批量锁仓会生成相同毫秒
        #     （`_lock_remaining_positions` 逐笔调用同一毫秒），墙钟回拨后也会
        #     与历史号重复 —— 症状和计数器一模一样，只是触发条件换了。单调
        #     计数器不吃墙钟，且可读性/可续号性都更好。
        #   同时不动 `entry_bar_seq`：它已持久化（bars_seen）且语义是"根数"
        #     （`bars_held = bars_seen - entry_bar_seq`），换算成时间戳要除以
        #     周期 → 引入周期依赖，与 `Engine.py` 注释里刻意保持的"与周期无关"相悖。
        # ════════════════════════════════════════════════════════════════
        state_records = (pd_list if isinstance(pd_list, list)
                         else [p.to_dict() for p in self.positions.positions])
        self._trade_seq = max(int(self.store.get_json("trade_seq", 0) or 0),
                              self.store.max_trade_seq())
        self._lock_pair_seq = max(
            int(self.store.get_json("lock_pair_seq", 0) or 0),
            self._max_lock_pair_seq_in_state(state_records))
        fn_seed = getattr(self.broker, "seed_order_seq", None)
        if callable(fn_seed):
            fn_seed(max(int(self.store.get_json("order_seq", 0) or 0),
                        self.store.max_order_seq(self.broker.name)))
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
        self.store.set_json("lock_pair_seq", int(self._lock_pair_seq))
        fn_seq = getattr(self.broker, "order_seq", None)
        if callable(fn_seq):
            self.store.set_json("order_seq", int(fn_seq()))
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
        # 簿内只剩 SOFT_EXIT_LOCK。开启态维持原行为（settle 止盈止损/收盘强平）。
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
            if pos.origin is PositionOrigin.SOFT_EXIT_LOCK:
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
        # v1.3（S1）信号门 —— 按"账户净敞口"重写，替代旧 Phase B / E3.2 的 state+多仓守卫：
        #   OPENING/EXITING → 下单瞬态，忽略但保留幂等键（不变）
        #   净敞口 != 0（运行态）→ 一律忽略（Q1=B，出场只由 on_bar 的 L1-L3 负责）
        #   净敞口 == 0：
        #       簿空（空仓）→ 开新仓（decide → OPEN）
        #       簿非空（锁仓态）→ 看"与信号反向的最早一笔持仓"的 entry_date：
        #           entry_date < today → 平旧仓（UNLOCK，规则 ⑸-②）
        #           否则（当日锁 / entry_date 缺失）→ 开新仓（OPEN，不动锁仓持仓，规则 ⑸-①）
        # 净敞口 = Σ(side.sign × volume)，锁仓对（多+空）自然抵消为 0。
        # 不再区分"单仓/多仓"两套语义 —— 旧 E3.2 的 G2 多仓守卫随本重写一并移除。
        # ════════════════════════════════════════════════════════════════
        if self._state in (EngineState.OPENING, EngineState.EXITING):
            self.store.update_signal_action(
                sig.key, "in_flight", "state={}".format(self._state.value))
            self.ev.write("signal_in_flight", key=sig.key,
                          state=self._state.value,
                          reason="engine_busy_opening_or_exiting")
            return

        # 运行态 = 簿内存在"非 SOFT_EXIT_LOCK"持仓（真实净敞口）。锁仓态/空仓 = 簿内全 SOFT_EXIT_LOCK 或空。
        #   注意：H1 锁仓落簿 = 1 笔反向 SOFT_EXIT_LOCK 持仓（原仓被 Trade 了结），锁仓持仓 net = ±vol ≠ 0，
        #   故不能用 net 判"锁仓 vs 运行"，必须按 origin 区分。
        #   这与 _restore 的 state 推断（is_empty or all SOFT_EXIT_LOCK → IDLE）口径一致。
        # P1（2026-09-10）：判定收口到 account_state()（SSOT）。
        if self.account_state() is AccountState.RUNNING:
            # 运行态：有真实净敞口 → 一律忽略信号（Q1=B）
            self.store.update_signal_action(
                sig.key, "skip", "running_ignore_signal")
            self.ev.write("signal_skip", key=sig.key,
                          reason="running_ignore_signal")
            return

        # 净敞口 == 0：空仓 或 锁仓态。挑"与信号方向相反、且最早"的一笔看日期。
        # today 取**信号自身**的交易日（与建仓端同一解析链 _day_of_anchor），
        #   取不到才回落到 last_bar / 墙钟。解锁判定比较的是"锁仓那笔仓的建仓日
        #   vs 信号所属交易日"，用 sig 比用 last_bar 更贴合（信号可能滞后 1 根 K）。
        today = self._day_of_anchor(sig) or self._current_trading_day()
        opp = sorted(self.positions.opposite_positions(sig.side),
                     key=lambda p: p.entry_bar_seq)
        if opp and opp[0].entry_date < today:
            # 锁仓·昨仓锁：平旧仓（与信号反向的最早一笔持仓），规则 ⑸-②。
            self._unlock_position(sig, sig.side)
            self.ev.write("signal_unlock", key=sig.key,
                          reason="unlock_yesterday_position")
            return

        # 空仓 或 锁仓·当日锁 → 开新仓。
        # decide 以 position=None 走"无净敞口开仓"决策（含三道过滤）。
        decision = self.entry_policy.decide(sig, None, self.spec)
        if not decision:
            self.store.update_signal_action(sig.key, "skip", decision.reason)
            self.ev.write("signal_skip", key=sig.key, reason=decision.reason)
            return

        # 开仓手数 @2026-09-08：每个买卖点只开一笔，一笔挂 self.lots_per_signal 手
        # （= 风控层 risk.max_volume，默认 2）。手数不再经仓位管理/风控闸门计算。
        lots = self.lots_per_signal
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

        # ════════════════════════════════════════════════════════════════
        # F1（2026-09-10）：建仓必须能确定"建仓所属交易日"，否则**拒绝建仓**。
        #   entry_date 是规则 ⑸ 判「今仓 → 反向开仓锁仓 / 昨仓 → 平仓」的唯一依据，
        #   它空着本身就是非法状态。旧实现会照常落一笔 entry_date="" 的仓，再由
        #   下游 `"" < today` 恒真把它静默解释成"昨仓" → 对今仓发 CLOSE 平今 →
        #   CTP 拒单 → 连锁 phantom 清仓（簿面清空但实盘仍有仓，不可逆）。
        #
        #   时间锚优先级：last_bar.timestamp（成交所在 K 线）→ sig.timestamp
        #   （信号 K 线；与建仓 K 线同源，通常就是同一根）。两者都无效才拒绝。
        #
        #   ⚠️ 本检查位于 broker.submit **之前** —— 拒绝时没有任何报单发出，
        #   账实天然一致。（对比 _book_lock_pair：那里的报单已成交，不能拒绝落簿。）
        # ════════════════════════════════════════════════════════════════
        entry_ts = (self.last_bar.timestamp
                    if (self.last_bar is not None and self.last_bar.timestamp > 0)
                    else int(sig.timestamp or 0))
        # 交易日解析链：成交所在 K 线（last_bar）→ 信号 K 线（sig）。
        #   二者是同一事实（成交时的 K 线时间）的副本，一个取不到就用另一个；
        #   都取不到才拒绝建仓。详见 _day_of_anchor 的可信度说明。
        entry_date = (self._day_of_anchor(self.last_bar)
                      or self._day_of_anchor(sig))
        if not entry_date:
            self.store.update_signal_action(sig.key, "rejected", "no_time_anchor")
            self.ev.write("order_rejected", key=sig.key,
                          reason="no_time_anchor", volume=volume,
                          last_bar_ts=(None if self.last_bar is None
                                       else self.last_bar.timestamp),
                          sig_ts=sig.timestamp,
                          note="无可用时间锚（last_bar.timestamp / sig.timestamp 均无效）"
                               "→ 拒绝建仓：entry_date 不可为空")
            self._state = EngineState.IDLE
            return

        cfg_max = self.cfg.risk.max_open_positions
        # v1.3（G3）：同向守卫只统计"非 SOFT_EXIT_LOCK"持仓（真实净敞口笔数）。
        #   锁仓持仓（SOFT_EXIT_LOCK）是已对冲的，不应占用 max_open_positions 名额，
        #   否则"锁仓后再开新仓"（规则 ⑸-①）会被 open_silenced 挡住。
        same_side_n = len([p for p in self.positions.same_side_positions(side)
                           if p.origin is not PositionOrigin.SOFT_EXIT_LOCK])
        # 静默跳过：现存非 SOFT_EXIT_LOCK 同向持仓已满 cfg.max → 不开、不报错（"静默填到 max"）
        if cfg_max > 0 and same_side_n >= cfg_max:
            self.ev.write(
                "open_silenced", key=sig.key,
                reason="same_side_already_max", cfg_max=cfg_max,
                same_side_n=same_side_n,
                note="引擎静默填到 max_open_positions（仅非 SOFT_EXIT_LOCK 持仓）；本信号不开仓")
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
        # entry_date / entry_bar_ts 在上方 F1 段已确定（= 成交所在 K 线的交易日与时间戳）。
        #   刻意不再从 sig.date 这类**格式化字符串**取 —— 字符串为空时会被下游
        #   静默解释成合法语义，是本次重构要根除的失效模式。
        pos = Position(
            symbol=self.spec.trade_symbol, side=side, volume=volume,
            entry_price=entry_price, entry_at=now_cn(),
            entry_bar_ts=entry_ts,
            entry_bar_seq=self.bars_seen,
            signal_key=sig.key, open_order_id=o.order_id, exit_plan=plan,
            origin=PositionOrigin.SIGNAL_OPEN,
            entry_date=entry_date)
        self.positions.add(pos)

        self.ev.write("open", symbol=pos.symbol, side=str(side),
                      volume=pos.volume, entry_price=entry_price,
                      stop=plan.stop_price, tp=plan.tp_price,
                      exit_policy=plan.name, exit_params=plan.params,
                      signal_key=sig.key, origin=pos.origin.value)

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
                         signal_key: str = "") -> None:
        """【E3.3】多仓平仓——按 FIFO（entry_bar_seq ASC）逐笔平仓。

        语义约定（与 E3.1 单仓 _close_position 等价 + 多仓扩展）：
          · 顺序：FIFO（最早建仓先平）—— 同根 bar 触发的多仓按建仓时间顺序平
          · 拒单策略：第一笔拒单 → 整批停 + retry cooldown（沿用 _close_retry_bars）
            后续笔拒单 → 保留剩余仓位继续（不阻塞已部分成交仓位）
          · phantom 兜底：retry streak 超限 → 清掉所有目标仓位（与 E3.1 一致）
          · 部分成交：剩余仓位保留在 book + state EXITING
          · 全部成交：state IDLE

        离场方式**只由规则 ⑸ 决定**（按建仓日期 → LOCK / CLOSE），没有任何调用方
        可以覆盖它 —— 包括"自动下单关闭"：关闭时今仓锁、昨仓平，与常规离场同一判据。
        （2026-09-10 P4 变体A 之前存在 `force_lock=True` 短路，会让昨仓也走反向开仓。）

        调用方传入的 positions 列表会自动按 entry_bar_seq 排序（防御性）。
        """
        if not positions:
            return

        # FIFO 排序——按建仓时间升序（防御性：即使调用方传乱序也保证 FIFO）
        ordered = sorted(positions, key=lambda p: p.entry_bar_seq)
        # 规则 ⑸：离场方式按"今日单 / 跨日单"判定。today 统一走 _current_trading_day()，
        # 与建仓端 entry_date 同为**交易日**口径（含夜盘归属次日），且与下方成本口径
        # （_is_today_pos）同源，避免任何一处日期口径漂移。
        today_str = self._current_trading_day(bar)

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
            # 规则 ⑸：按【建仓日期】决定离场方式（硬规则，不留开关）。
            #   2026-09-10 起不再看 origin：当日单 → LOCK 反向开仓锁仓；
            #   跨日单 → CLOSE 平昨。（旧实现按 origin 联动，会导致
            #   "当日开、隔日平"的仓被误判为今日单而多余锁一次仓。）
            #   2026-09-10 二次修正（P4 变体A）：删除 force_lock 短路 ——
            #   自动下单关闭时也不再"一律 LOCK"，同样走本判据（今仓锁 / 昨仓平）。
            intent, side = self._exit_intent(pos, today_str)

            # entry_date 传给 broker 供审计/诊断记录（DryRun 写入 Order.meta）。
            # 2026-09-10 起 offset 不再据此分支：规则 ⑸ 保证 CLOSE 只用于跨日单，
            # 中金所平昨报文恒为 CLOSE（原"平今 CLOSETODAY"分支已随不可达路径删除）。
            o = self.broker.submit(intent, side, pos.volume, trigger_price,
                                   signal_key or pos.signal_key,
                                   note=reason, entry_date=pos.entry_date or "")
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

            if intent is OrderIntent.LOCK:
                # ═══ 软离场（锁仓）= 留双向持仓：原仓 → SOFT_EXIT_LOCK + 反向仓 SOFT_EXIT_LOCK，不兑现 PnL ═══
                # 锁仓 = 反向开仓（底层只有开/平，锁仓不是平仓）：原仓保留
                # （entry_price=P₀ 会计锚不动），反向仓作为新 SOFT_EXIT_LOCK 持仓落簿，两笔共享
                # lock_pair_id。原仓 PnL 不记 Trade（继续浮动），净敞口归零。
                self._book_lock_pair(pos, side, exit_price, o, reason, idx,
                                     len(ordered), bar)
            else:
                # ═══ 硬离场（平仓）= 记 Trade + remove 原仓 ═══
                gross = pos.pnl_points(exit_price)
                # 2026-09-10：成本口径必须与 broker 实际发出的报文一致。
                #   规则 ⑸ 保证 OrderIntent.CLOSE 只用于跨日单（entry_date < today），
                #   故 hard exit 恒按**平昨**费率计（0.0023%）。此处仍按 entry_date
                #   动态判定，是为了对"未来其它调用方"保持防御。
                #   entry_date 由 Position.__post_init__ 保证非空且为 YYYY-MM-DD
                #   （F4），故不再需要 `bool(pos.entry_date) and pos.entry_date[:10]`。
                _is_today_pos = pos.entry_date >= today_str
                cost = self.spec.cost_points(
                    pos.entry_price, exit_price,
                    close_today=bool(_is_today_pos and self.spec.close_today_first))
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

                self.ev.write("close", symbol=t.symbol, side=str(t.side), reason=reason,
                              entry=t.entry_price, exit=t.exit_price,
                              gross=t.gross_points, cost=t.cost_points,
                              net=t.net_points, cash=t.net_cash, bars_held=bars_held,
                              trade_id=t.trade_id, exit_policy=t.exit_plan_name,
                              origin=pos.origin.value,
                              exit_mode=intent.value,
                              position_signal_key=pos.signal_key,
                              fifo_index=idx, pos_count=len(ordered))

            n_closed += 1

        # 全部处理完毕（全部成交 / 部分成交 + 后续拒单 / 全部拒单后整批停早 return）
        self._persist()
        # Phase H1：簿空 或 簿内只剩 SOFT_EXIT_LOCK 锁仓（等待次日对向信号解锁）→ IDLE。
        # 锁仓不属于"平仓未完成"，不应让引擎卡在 EXITING。
        # P1（2026-09-10）：判定收口到 account_state()（SSOT）。
        if self.account_state() is not AccountState.RUNNING:
            self._state = EngineState.IDLE
        # else: 仍有在持今仓（部分成交或 cooldown 中）→ 保持 EXITING

    # ---------------- 软离场（锁仓）留双向持仓落簿（Phase S4） ----------------
    def _book_lock_pair(self, pos: Position, side: Side, exit_price: float,
                        o: Order, reason: str, idx: int, pos_count: int,
                        bar: Optional[Bar] = None) -> None:
        """软离场（锁仓）留双向持仓落簿：原仓 → SOFT_EXIT_LOCK + 反向仓 SOFT_EXIT_LOCK，不兑现 PnL。

        锁仓 = 反向开仓（底层只有开/平，锁仓不是平仓）：原仓保留
        （entry_price=P₀ 会计锚不动、entry_date 不动），反向仓作为新 SOFT_EXIT_LOCK 持仓落簿，
        两笔共享 lock_pair_id。原仓 PnL 不记 Trade（继续浮动），净敞口归零。
        对向信号经 on_signal 门触发 UNLOCK（平反向仓 + 升级同向持仓）。
        ⚠️ 该 UNLOCK 出口的**前提是自动下单处于开启态** —— 关闭态下 on_signal 顶部
        直接 return，锁对没有任何自动出口（"冻结"语义，见 _lock_remaining_positions）。
        """
        self._lock_pair_seq += 1
        pair_id = "lock_{:05d}".format(self._lock_pair_seq)

        # 原仓 → SOFT_EXIT_LOCK（保留 entry_price=P₀ 会计锚；不 remove、不记 Trade）
        pos.origin = PositionOrigin.SOFT_EXIT_LOCK
        pos.lock_pair_id = pair_id

        # 反向仓 SOFT_EXIT_LOCK 落簿（entry_price = 锁仓成交价 P₁）
        #
        # F1 配套（2026-09-10）：反向仓同样需要"建仓所属交易日"。但与 _open_position
        #   不同，本处**不能因为拿不到时间锚就拒绝落簿** —— 反向报单已经成交，
        #   不落簿会造成"实盘有反向仓、簿面无记录"的账实不符，比日期缺失更危险。
        #   故取不到 K 线时间戳时用**墙钟**兜底：走到这一步说明进程启动后从未收到
        #   K 线，那必然是实盘/托管关闭场景（回放与实盘推进都靠 K 线驱动），
        #   墙钟的交易日与真实交易日一致。
        #
        #   时间锚必须优先取**触发锁仓的那根 bar**（调用方传入），而不是引擎最近的
        #   self.last_bar —— 二者在"直接调 _close_positions（补锁 / 测试 / 手工触发）"
        #   场景下会分叉，用 last_bar 会把锁仓反向仓的建仓日算成墙钟今天。
        lock_anchor = bar if bar is not None else self.last_bar
        lock_ts = (lock_anchor.timestamp
                   if (lock_anchor is not None and lock_anchor.timestamp > 0)
                   else now_ms())
        lock_entry_date = (self._day_of_anchor(lock_anchor)
                           or trading_day_of_ms(now_ms()))
        lock_pos = Position(
            symbol=pos.symbol, side=side, volume=pos.volume,
            entry_price=exit_price, entry_at=now_cn(),
            entry_bar_ts=lock_ts,
            signal_key=pos.signal_key + "#lock",
            open_order_id=o.order_id,
            exit_plan=ExitPlan(name="locked_await_unlock", stop_price=0.0),
            entry_bar_seq=self.bars_seen,
            origin=PositionOrigin.SOFT_EXIT_LOCK,
            entry_date=lock_entry_date,
            lock_pair_id=pair_id,
        )
        try:
            self.positions.add(lock_pos)
        except PositionBookError as e:
            # 防御：不限容量（max=None）下不该发生。落簿失败 → 反向仓留 broker 端
            # 由对账/人工处理，不阻塞批次。
            self.ev.write(
                "lock_book_failed", symbol=pos.symbol, side=str(side),
                volume=pos.volume, error=str(e),
                order_id=o.order_id, lock_of=pos.signal_key)
            return
        self.ev.write(
            "lock_booked", symbol=lock_pos.symbol,
            side=str(lock_pos.side), volume=lock_pos.volume,
            entry=lock_pos.entry_price, order_id=o.order_id,
            lock_of=pos.signal_key,
            position_signal_key=lock_pos.signal_key,
            lock_pair_id=pair_id,
            reason=reason,
            fifo_index=idx, pos_count=pos_count)

    # ---------------- 解锁入场（Phase E2 UNLOCK_UPGRADE 路径） ----------------
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

        # 容量守卫：补开是"再开一笔持仓"，簿容量不足 1 笔则整笔不补（不能开半笔）。
        # v1.3：max_positions=None（不限）时 headroom 恒为 1（不再截断）。
        if self.positions.max_positions is None:
            headroom = 1
        else:
            headroom = max(0, self.positions.max_positions - len(self.positions))
        if new_lots > 0 and headroom < 1:
            self.ev.write("max_open_cap", key=sig.key,
                          requested=new_lots, effective=0,
                          book_n=len(self.positions),
                          book_max=self.positions.max_positions,
                          note="解锁后补开被簿容量截断（簿已满，无法再开一笔）")
            new_lots = 0

        # （2026-09-08 注：补开手数 = N - 已解锁 V，由"手数直接取 risk.max_volume"
        #   决定，无风控/仓位再检查，仅余簿容量守卫；据此记录解锁/补开结果。）
        self.ev.write("unlock_result", key=sig.key, unlocked=v, want=want,
                      new_open=new_lots,
                      action=("with_new_open" if new_lots > 0 else "pure_unlock"))

        if new_lots > 0:
            # 补开：state 由 _open_position 推进（成交→IN_TRADE / 拒单→IDLE）
            self._open_position(sig, side, new_lots)
        else:
            # v1.3（S3/S4）：留双向持仓解锁后，升级的配对仓已不再是 SOFT_EXIT_LOCK（单边敞口）
            #   → IN_TRADE；若簿内无任何非 SOFT_EXIT_LOCK 持仓（纯解锁回空仓 / 旧数据 1 锁 1 笔）→ IDLE。
            # P1（2026-09-10）：判定收口到 account_state()（SSOT）。
            if self.account_state() is AccountState.RUNNING:
                self._state = EngineState.IN_TRADE
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
        # v1.3（S3/S4）：留双向持仓下，解锁平掉反向仓后，升级配对同向持仓
        #   SOFT_EXIT_LOCK → UNLOCK_UPGRADE + 重算风控锚（= 解锁成交价 P₂）。
        self._upgrade_lock_pair(target, o.filled_price, sig)

        self.ev.write("unlock", symbol=t.symbol, side=str(t.side),
                      entry=t.entry_price, unlock=o.filled_price,
                      gross=t.gross_points, cost=t.cost_points,
                      net=t.net_points, cash=t.net_cash, bars_held=bars_held,
                      trade_id=t.trade_id, unlock_signal_key=sig.key,
                      unlock_order_id=o.order_id,
                      origin=target.origin.value)
        return t

    # ---------------- 解锁后升级配对持仓（Phase S3/S4） ----------------
    def _upgrade_lock_pair(self, locked_pos: Position, unlock_price: float,
                           sig: Signal) -> None:
        """解锁后升级配对同向持仓：SOFT_EXIT_LOCK → UNLOCK_UPGRADE + 重算风控锚（P₂）。

        留双向持仓下，锁仓 = 原仓 + 反向仓（共享 lock_pair_id）。解锁平掉反向仓后，
        同向持仓恢复单边敞口，必须从 SOFT_EXIT_LOCK 升级为 UNLOCK_UPGRADE（接入 L1-L3 止盈止损，
        离场走硬离场平昨），并以解锁成交价 P₂ 为风控锚重算出场计划。
        会计锚 entry_price（P₀）保持不动，风控锚 risk_anchor（P₂）写入 ExitPlan.params。
        """
        if not locked_pos.lock_pair_id:
            # 解锁标的自己没有 lock_pair_id → 无从定位配对仓。
            #   （旧数据 / 1 锁 1 笔旧口径 / 簿被外力改过）
            # 2026-09-10：原先这里 `return` 是**三个防御分支里唯一不写事件**的静默点
            #   （对照：配对不在簿 → unlock_pair_missing；同 id 多候选 →
            #     unlock_pair_ambiguous；配对 origin 异常 → unlock_pair_not_soft_exit_locked）。
            #   行为不变（仍然拒绝升级），只补可见性：该同向仓会滞留 SOFT_EXIT_LOCK，
            #   在 `_settle_positions` 里被跳过 → 不参与 L1-L3 止盈止损，必须留痕。
            self.ev.write("unlock_pair_id_missing",
                          signal_key=sig.key,
                          position_signal_key=locked_pos.signal_key,
                          side=str(locked_pos.side),
                          volume=locked_pos.volume,
                          note="解锁标的缺少 lock_pair_id → 无从定位配对仓，拒绝升级；"
                               "同向仓会滞留 SOFT_EXIT_LOCK（不参与止盈止损），需人工介入")
            return
        # R3（2026-09-10）：原实现遍历取**第一个**匹配就 break（静默）。改为显式
        #   收集全部匹配：≥2 说明"选要平的仓"（方向 + 最老）与"选要升级的仓"
        #   （lock_pair_id 相同）脱钩 —— 平一笔、升另一笔，被漏掉那笔会永久停在
        #   SOFT_EXIT_LOCK（`_settle_positions` 跳过它 → 不参与 L1-L3 止盈止损）。
        #   启动期的不变量断言（_assert_lock_pair_invariant）已保证组内 ≤2 笔，
        #   且调用方在进入本函数前已从簿中 remove 掉 target → matches ≤ 1。
        #   此处的 >1 分支是**运行期纵深防御**（簿被外力改动时），不是策略：
        #   不猜、不升级，写事件后原样返回。
        pair = None
        ambiguous = [p for p in self.positions.positions
                     if p is not locked_pos
                     and p.lock_pair_id == locked_pos.lock_pair_id]
        if len(ambiguous) > 1:
            self.ev.write("unlock_pair_ambiguous",
                          lock_pair_id=locked_pos.lock_pair_id,
                          signal_key=sig.key,
                          candidates=[p.signal_key for p in ambiguous],
                          note="同一 lock_pair_id 匹配到多笔候选仓 —— 不猜哪笔是配对仓，"
                               "拒绝升级（该同向仓会滞留 SOFT_EXIT_LOCK，需人工介入）")
            return
        if ambiguous:
            pair = ambiguous[0]
        if pair is None:
            # 配对持仓已不在簿（异常）→ 防御记录
            self.ev.write("unlock_pair_missing",
                          lock_pair_id=locked_pos.lock_pair_id,
                          signal_key=sig.key)
            return
        if pair.origin is not PositionOrigin.SOFT_EXIT_LOCK:
            # 可见性事件（不改变行为）：被升级的配对仓不是 SOFT_EXIT_LOCK。
            #   唯一可达路径是 `_check_unlock_stuck` 把 target 快照**重建**回簿
            #   （UNLOCK 卡单确认：实盘反向仓还在），此时配对仓已是 UNLOCK_UPGRADE。
            #   该场景下用新的解锁价 P₂ 重锚**是正确语义**（账户重新变成"锁住"
            #   状态，风控锚应更新），故不阻断 —— 但必须可见，不许静默重升。
            self.ev.write("unlock_pair_not_soft_exit_locked",
                          lock_pair_id=pair.lock_pair_id,
                          signal_key=sig.key,
                          pair_signal_key=pair.signal_key,
                          pair_origin=pair.origin.value,
                          note="配对仓当前不是 SOFT_EXIT_LOCK 却走了升级路径"
                               "（通常来自 UNLOCK 卡单快照重建）；仍按新解锁价重锚")
        # 升级：SOFT_EXIT_LOCK → UNLOCK_UPGRADE（审计标签，不参与离场决策）+ 重算 ExitPlan
        #   （anchor = 解锁成交价 P₂；entry_date 保持原开仓日不动，
        #    故该仓必为昨仓 → 离场恒走 CLOSE 平昨，与规则 ⑸ 一致）
        pair.origin = PositionOrigin.UNLOCK_UPGRADE
        pair.exit_plan = self.exit_policy.plan(sig, pair.entry_price, self.spec,
                                               anchor=unlock_price)
        self.ev.write("unlock_pair_upgraded",
                      symbol=pair.symbol, side=str(pair.side), volume=pair.volume,
                      entry_price=pair.entry_price, risk_anchor=unlock_price,
                      lock_pair_id=pair.lock_pair_id,
                      signal_key=sig.key)

    # ---------------- 离场方式（规则 ⑸ 硬规则：按日期判定） ----------------
    @staticmethod
    def _exit_intent(pos: Position, today: str = "") -> "Tuple[OrderIntent, Side]":
        """按**建仓日期**决定离场方式（2026-09-10 用户拍板，替代旧的 origin 联动）。

        规则 ⑸ 字面语义（硬编码，不留配置开关）：
          今日单（entry_date >= today）→ SOFT_EXIT 软离场
              → OrderIntent.LOCK + 反向 side
                （反向开仓锁仓；CTP 报文 offset=Open。避开平今 15× 费率）
          跨日单（entry_date <  today）→ HARD_EXIT 硬离场
              → OrderIntent.CLOSE + pos.side
                （平昨；中金所 offset=Close，0.0023% 费率最优）
          SOFT_EXIT_LOCK → OrderIntent.UNLOCK + pos.side
                （防御分支：_settle_positions 已跳过 SOFT_EXIT_LOCK，正常路径不可达）

        为什么废弃旧"origin 联动"
          旧实现 SIGNAL_OPEN→LOCK / UNLOCK_UPGRADE→CLOSE 隐含假设"SIGNAL_OPEN 仓当日开、
          当日平"。一旦**当日开仓、隔日才触发离场**，该仓物理上已是昨仓，却仍走 LOCK
          （开反向今仓）→ 多付一次开仓费，且次日还要再平两笔（劣于直接平昨）。
          按日期判定才是规则 ⑸ 的用户口径，也是 Q2=A 的既定决策
          （对应的决策记录文档已不在仓库内，2026-09-10 按用户要求删除本引用）。

        entry_date / today 的可空性（2026-09-10 根因治理后）
          · entry_date：由 Position.__post_init__（F4）保证非空且为 YYYY-MM-DD ——
            缺失时依次从 entry_bar_ts（真实毫秒，权威）→ entry_at（墙钟，兜底）重建；
            三者全空的记录会被 _restore（F2）拒绝启动，**不会再流到这里**。
            旧实现把 "" 交给 `"" < today` 恒真、静默当"昨仓"处理，是本次要根除的
            失效模式：判错会对今仓发 CLOSE 平今 → CTP 拒单 → 连锁 phantom 清仓。
          · today：由 _current_trading_day() 保证非空（末端有墙钟兜底）。
          下方 `today and ...` 的判空仅为"防御不可达状态"：真有空值也不猜，走 LOCK
          （反向开仓报文，永不因"持仓不足/平今"被 CTP 拒单），并让 F2/F4 的告警去暴露它。
        """
        if pos.origin is PositionOrigin.SOFT_EXIT_LOCK:
            # 防御分支，正常路径不可达（settle 已跳过 SOFT_EXIT_LOCK）
            return OrderIntent.UNLOCK, pos.side
        if today and pos.entry_date < today:
            # 跨日单 → 硬离场（平昨）
            return OrderIntent.CLOSE, pos.side
        opposite = Side.SHORT if pos.side is Side.LONG else Side.LONG
        return OrderIntent.LOCK, opposite

    # ════════════════════════════════════════════════════════════════
    # Phase I1（2026-09-06）：自动下单关闭（前端开关 → 进程托管触发）
    #   关闭语义（用户拍板）：
    #     ① auto_order_enabled=False → on_signal 顶部拒收所有买卖点信号
    #     ② _lock_remaining_positions → 簿内「未锁定」持仓按【规则 ⑸】离场：
    #          今仓 → LOCK（反向开仓锁仓，避开平今高费率）
    #          昨仓 → CLOSE（平昨，费率正常）
    #        ★ 2026-09-10 P4 变体A 修正：旧实现带 `force_lock=True`，对昨仓也发
    #          反向开仓 → 昨仓被白锁一次（多付一次开仓费，且次日还要再平两笔）。
    #          现在关闭与常规离场走**同一判据**，不再有覆盖开关。
    #   幂等性：重复关闭只对仍未离场的持仓补做；簿内只剩 SOFT_EXIT_LOCK 时无操作。
    #   状态：auto_order_enabled 持久化（_persist），重启保持关闭语义。
    #
    #   ⚠️ 关闭后账户的归宿 —— "冻结"语义（用户 2026-09-10 明确选择 甲 方案）：
    #     今仓被 LOCK 成的锁对（原仓 + 反向仓，双双 SOFT_EXIT_LOCK）**没有自动出口**：
    #       · on_signal 顶部直接 return → 永远等不到 UNLOCK；
    #       · on_bar 关闭态不调 _settle_position，而 _settle_positions 本就跳过
    #         SOFT_EXIT_LOCK → 不止盈止损、不收盘强平；
    #       · auto_order_enabled 持久化 → 重启后状态与出口完全不变。
    #     净敞口为 0，PnL 不兑现，需**人工在交易所平掉**，或重新开启自动下单后
    #     由对向信号走 UNLOCK 管线接管。这是**有意选择的行为，不是遗漏**；
    #     为了让它在运维侧可见（而不是静默），关闭时若留下锁对会写一条
    #     `account_frozen` 事件。护栏见 Trading/Test/test_p30_shutdown_exit_mode.py。
    # ════════════════════════════════════════════════════════════════
    def _lock_remaining_positions(self, bar: Optional[Bar] = None,
                                  reason: str = "auto_order_off") -> None:
        """把簿内所有未锁定持仓按规则 ⑸ 离场（关闭语义 ② 的执行体）。

        on_bar 关闭态下每根 K 线调用一次：上次离场被拒（cooldown 期）的残留持仓
        会在 cooldown 结束后自动补做，直到簿内只剩 SOFT_EXIT_LOCK。
        今仓 → LOCK、昨仓 → CLOSE（同一判据，见 _exit_intent）。
        """
        remaining = [p for p in self.positions.positions
                     if p.origin is not PositionOrigin.SOFT_EXIT_LOCK]
        if not remaining:
            return
        ref_price = 0.0
        if bar is not None and bar.close:
            ref_price = bar.close
        elif self.last_bar is not None and self.last_bar.close:
            ref_price = self.last_bar.close
        # bar 为空时用 self.last_bar 兜底：否则 _close_positions 内的 today 会退化到
        # **墙钟**（回放 / 补锁场景下与真实交易日不同）→ 今仓会被误判成昨仓 → 发平今。
        # 两者都为空（进程启动后从未收到 K 线）时由 _current_trading_day 的墙钟兜底接手
        # —— 那必然是无 K 线驱动的托管关闭场景，墙钟交易日与真实交易日一致。
        self._close_positions(remaining, reason, ref_price,
                              bar if bar is not None else self.last_bar,
                              signal_key="auto_order_off")

    def shutdown_and_lock_all(self, reason: str = "auto_order_off") -> None:
        """自动下单关闭入口（main.py 收到退出信号时调用）。

        ① 停信号门（后续 SSE 推来的买卖点信号一律 skip）→
        ② 未锁定持仓按规则 ⑸ 离场（今仓锁 / 昨仓平）→ ③ 持久化。幂等：重复调用安全。

        关闭后账户归宿见上方类注释的"冻结语义"说明：留下的锁对无自动出口。
        """
        self.auto_order_enabled = False
        self._lock_remaining_positions(reason=reason)
        self._persist()
        self.ev.write("auto_order_off",
                      reason=reason,
                      locked_n=sum(1 for p in self.positions.positions
                                   if p.origin is PositionOrigin.SOFT_EXIT_LOCK),
                      remaining_unlocked=sum(
                          1 for p in self.positions.positions
                          if p.origin is not PositionOrigin.SOFT_EXIT_LOCK),
                      note="停止接收信号 + 未锁定持仓已按规则 ⑸ 离场（今仓锁 / 昨仓平）")
        # 账户归宿可见性（P4 变体A）：关闭后若还挂着持仓，写一条显式事件。
        #   "关闭"在前端只体现为 enabled=false，看不出"账户还停在锁仓态、引擎不会
        #   再自动平仓"。不给这条事件的话，运维必须去翻持仓明细才知道 —— 正是
        #   本轮把它称为"静默冻结"的原因。仅记录，不影响任何交易行为。
        frozen = [p for p in self.positions.positions
                  if p.origin is PositionOrigin.SOFT_EXIT_LOCK]
        if frozen:
            self.ev.write(
                "account_frozen", reason=reason,
                frozen_n=len(frozen),
                lock_pair_ids=sorted({p.lock_pair_id for p in frozen
                                      if p.lock_pair_id}),
                net_exposure=sum((1 if p.side is Side.LONG else -1) * p.volume
                                 for p in frozen),
                note="关闭后账户停在锁仓态（净敞口 0）：引擎无自动清仓路径，"
                     "需人工平仓或重新开启自动下单后由对向信号解锁")

    def auto_order_status(self) -> Dict[str, Any]:
        """自动下单状态快照（供后端进程托管 / API / 前端轮询）。"""
        return {
            "enabled": self.auto_order_enabled,
            "state": self._state.value,
            # P1（2026-09-10）：对外暴露账户三态（用户口径 ⑴）。此前 App/前端只能拿到
            # 引擎过程态 EngineState（IDLE 同时覆盖空仓与锁仓），"空仓 vs 锁仓"不可见。
            "account_state": self.account_state().value,
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
            # G1：不用 self.position（legacy_single 在多仓会抛）；直接列全部持仓。
            "open_position": ([p.to_dict() for p in self.positions.positions] or None),
            "exit_policy": self.exit_policy.describe(),
            "entry_policy": self.entry_policy.describe(),
            "spec": {"signal_symbol": self.spec.signal_symbol,
                     "trade_symbol": self.spec.trade_symbol,
                     "price_tick": self.spec.price_tick,
                     "multiplier": self.spec.multiplier},
        }
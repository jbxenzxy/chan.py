# -*- coding: utf-8 -*-
"""
出场策略（Exit.py）
====================
精简：出场只有一个策略 `LayeredExitPolicy`（L1-L3 分层出场），
原可选的 `DefaultExitPolicy`（简单固定点数出场）与 L4 时间/收盘兜底均已删除。

价格输入口径（2026-09-22 · 用户拍板）：**整层 L1-L3 的价格判定只读本根 K 线的收盘价
`bar.close`**，一律不读 `bar.high` / `bar.low`。理由：判定**时刻**本来就是"这根 K 线闭合
之后"，若判定**依据**回头去看"盘中最小到过多少"，等于拿一个已经消失的价格去触发一笔
按当时盘口成交的单 —— 依据与时刻不是同一个东西。统一到收盘价后，依据与时刻都是
"这根 K 线结束时的那个价"。
    ⚠️ 唯一例外是 `_atr()`：真实波幅 TR 的**定义式**必须用 high/low/前收（这是波动率
    度量，喂 L2 的距离标定），它不参与"是否触发"的判断。引擎侧 `ev.write("bar",
    high=…, low=…)` 只是事件留痕，同样不参与判断。
    由此，「同根 K 线同时触及止盈与止损 → 按止损计（悲观）」这条旧兜底规则**已不可达**
    （一个收盘价不可能既 ≥ 止盈线又 ≤ 止损线）—— 规则随口径一并删除。

两个刻意保留的保守设定（LayeredExitPolicy）
    ① 价格对齐一律往"对自己不利"的方向取整（止损更易触发、止盈更晚更少）
    ② 出场计划里带上参数快照，落盘后可做事后参数敏感性分析

跟踪止盈模式（use_trailing=True，默认）：止盈交给跟踪，不落固定止盈单
    `use_trailing=True`（默认）时 plan() 不生成止盈单（tp_price=None），浮盈完全由 L3
    的 ATR 跟踪止损兑现；L3 启动阈值（= win_loss_ratio×R）直接取品种档案的 `win_loss_ratio`
    （IC/IM=3R、其余=2R），故"盈利到 win_loss_ratio×R 时进 L3 跟踪锁利"，不同品种进 L3
    时机天然不同。名义止盈价（= win_loss_ratio×R）仍写入 params["_tp_nominal"] 供事后对照。
    历史上曾用独立全局 `trailing_trigger_r` 作 L3 触发（与 win_loss_ratio 解耦），
    合并：删 trailing_trigger_r，L3 触发统一走品种级 win_loss_ratio
    （消除"win_loss_ratio=3 是死配置"问题，IC/IM 真正按 3R 进 L3）。
    固定止盈单模式（`use_trailing=False`）：落固定止盈单（= win_loss_ratio×R），无保本、无跟踪。

    ⚠️ 术语沿革（2026-09-15 清理，2026-09-22 续）：出场策略**只有 LayeredExitPolicy
    一套（L1-L3）**，不存在两套可选的出场策略；L1-L3 里也**不存在"硬止损 / 硬止盈"这套
    说法** —— 止损线由 L1（结构 R）+ L2（ATR 定宽）给出，止盈由 L3（保本 / 跟踪）或
    固定止盈单给出，"硬"字没有对应实体，一律不要再用。旧文档 / 旧注释里的「A 方案 /
    B 方案」是本策略 `use_trailing` 两种取值的遗留叫法（B = 跟踪止盈模式、
    A = 固定止盈单模式），同样废弃；新写的代码 / 日志 / 报告一律用 L1-L3 与模式名。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from ..Config import ExitPolicyParams
from ..Infra.Instrument import Instrument
from ..Infra.Records import Bar, ExitPlan, Position, Side, Signal
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass
class ExitCheck:
    """出场判定结果。price 是"触发价"，不是最终成交价（成交价由 broker 决定）。

    fill_price 是"建议成交参考价" = 触发判定用的那根 K 线收盘价（2026-09-22
    回测语义拍板）：触发价只负责触发报单；DryRun 等纸面 broker 的离场成交价
    按触发那根 K 线的收盘价落账 —— 收盘才认的口径下，触发时刻真实可实现的
    价就在收盘价附近，仍记止损/止盈线会系统性偏一格。实盘 / SimNow 的成交价
    以柜台回报为准，不消费本字段。

    only_update=True 表示"只更新出场计划、不登场"——移动止损 / 跟踪止盈走这条路。
    此时 plan 必须给，price 无意义（也不给 fill_price）。
    """
    reason: str                       # tp / sl / time / trailing / custom
    price: float
    fill_price: Optional[float] = None  # 建议成交参考价 = 触发那根 K 线收盘价
    plan: Optional[ExitPlan] = None   # 非空则替换持仓的出场计划
    only_update: bool = False


# 参数默认值单一事实源（严格模式；拆分）：
#   不再从 DEFAULT_CONFIG 抄一份 `_DEF_EXIT_PARAMS` 兜底，直接由 Trading/Config.py
#   的参数模型校验 —— 缺省键用模型字段的默认值，拼错的键（extra="forbid"）立即报错。
#   校验模型用 ExitPolicyParams（继承 ExitConfig + 品种相关出场参数）：ExitConfig 是
#   **部署配置**（品种无关项），本 policy 的入参是 resolved_exit_params() 合并后的
#   完整参数（含品种相关项 win_loss_ratio —— 2026-09-14 前档案提供 min_r_points /
#   win_loss_ratio / breakeven_buffer_ticks 三者，现只剩这一个），
#   故校验/持有模型必须两样都有。

class LayeredExitPolicy:
    name = "LayeredExitPolicy"

    # R 观测阈值（评审补 · 用户拍板「A < 3.0」）：结构距离 A 低于本值时打
    #   WARNING（见 _initial_r）。取值含义 = **已删除的 min_r_points 地板原值** ——
    #   IF/IH 与商品档当时是 3.0，IC/IM 是 5.0。
    #   ⚠️ A 的单位是**报价点数**，不同品种量级不同 → 本值是"观测灵敏度"旋钮，
    #   不是风控参数：它**不参与、也不会改变** R = max(A, B) 的取值，
    #   只决定多早把现场打到控制台。要更早抓样本就调大本值（如按 IC/IM 口径设 5.0）。
    #   本属性**不是配置项**：ExitPolicyParams 是 extra="forbid"，它只走类属性，
    #   避免和"改标定值必须过 git 评审"的纪律混淆。
    r_alert_a_floor: float = 3.0

    # ---------- 参数 ----------
    def __init__(self, params=None):
        self.params = dict(params or {})
        p = ExitPolicyParams(**self.params)
        self.p = p
        # L1 R 倍数定基线
        self.stop_buffer_ticks = float(p.stop_buffer_ticks or 0.0)
        self.win_loss_ratio = float(p.win_loss_ratio)
        # 注：已删除 stop_at_signal_extreme 开关 —— R 的口径唯一：
        #     R = max(分型极值距离 A, atr_sl_multiple × ATR)。信号未带分型时 A 自然为 0，
        #     不需要开关去表达「只靠 ATR」。
        # L2 波动率(ATR)定宽窄
        self.use_atr = p.use_atr
        self.atr_period = int(p.atr_period)
        self.atr_sl_multiple = float(p.atr_sl_multiple)
        # L3 移动/保本锁利
        self.use_trailing = p.use_trailing
        self.breakeven_trigger_r = float(p.breakeven_trigger_r)
        self.breakeven_buffer_r = float(p.breakeven_buffer_r)
        self.trailing_trigger_r = float(p.trailing_trigger_r)
        # 跨日清空 ATR 缓冲用
        self._last_day: str = ""

        # ATR 历史缓冲（on_bar 维护，平着也收）
        self._bars: "deque" = deque(maxlen=self.atr_period + 2)

    # ---------- 基类折叠进来的共享方法（本层仅单一实现，无需抽象基类） ----------
    def describe(self) -> str:
        return "{}({})".format(self.name, self.params)

    def check_with(self, position: Position, bar: Bar, state: "Instrument",
                   bars_held: int = 0) -> Optional["ExitCheck"]:
        """引擎唯一调用入口（兼容旧签名策略）。"""
        return self.check(position, bar, state, bars_held=bars_held)

    # ---------- 钩子：每根 K 线（无论持仓与否）都会调用 ----------
    def on_bar(self, bar: Bar, state: "Instrument") -> None:
        # 跨日清空 ATR 缓冲：昨收 → 今开的隔夜跳空会造出一个巨大 TR。
        # 30m 下一天只有 8 根 bar、缓冲要 atr_period+1=15 根，
        # 一个跳空能把近两天的 ATR 都顶高 → 止损/跟踪距离被系统性放大。
        day = (bar.date or "")[:10]
        if self._last_day and day and day != self._last_day:
            self._bars.clear()
        if day:
            self._last_day = day

        self._bars.append(bar)

    # ---------- ATR ----------
    def _atr(self) -> Optional[float]:
        if len(self._bars) < self.atr_period + 1:
            return None
        bars = list(self._bars)
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) < self.atr_period:
            return None
        return sum(trs[-self.atr_period:]) / self.atr_period

    def current_atr(self) -> Optional[float]:
        """对外暴露当前 ATR（点数）。供外部观测/日志使用。

        （原 atr_risk 仓位模式随动态定仓删除，ATR 仅保留观测用途。）
        样本不足（on_bar 缓冲未攒够 atr_period+1 根）时返回 None。
        """
        return self._atr()

    # ---------- R 计算（L1 结构 + L2 波动率，取最大） ----------
    def _initial_r(self, signal, entry_price: float, state: "Instrument") -> float:
        """初始风险距离 R = max(A, B) —— **唯一的口径**（2026-09-15 去掉 stop_at_signal_extreme 开关）。

        A = 结构止损（分型极值距离）：
              做多 A = entry_price − 底分型最低点(fractal_low)；
              做空 A = 顶分型最高点(fractal_high) − entry_price。
            A ≤ 0（陈旧信号、行情已穿越分型）时钳到 0，交给 B（2×ATR）兜底。
        B = 波动率止损 = atr_sl_multiple × ATR（use_atr 且 ATR 样本足够时）。

        为什么没有「只靠 2×ATR」这个选项（原 `stop_at_signal_extreme=False`）：
            分型极值与 2×ATR 是**取大**关系，不是二选一。信号未携带分型时
            （fractal ≤ 0 哨兵）A 自然为 0，R 自动退化为 2×ATR —— 这个能力本来
            就由「数据缺失」表达，不需要一个配置项去重复表达同一件事。
            留着开关只会让人以为「两种止损方案可选」，而实际上只有一种。

        关于「R 会不会退化」（评审 · 结论：不改口径，只加观测）：
            删除 min_r_points 后，R 不再有绝对点数地板 —— 这是刻意的：
            口径是「有分型才有买卖点 → 有买卖点才入场 → 入场时 A 恒 > 0」，
            且 B（2×ATR）在正常行情下量级远大于旧地板，R 的地板是多余的。
            因此本函数**不兜底、不钳下限**，只打 WARNING 把现场丢到控制台抓样本。
            两个观测点（都**不改变** R 的取值）：

            [1] `A < r_alert_a_floor`（默认 3.0）——评审后**放宽**
                原实现只在 `A == 0` 出声，恰好把真正会出问题的区间吞掉了。
                风险窗口是 `0 < A < 地板`：B 未就绪时（on_bar 每交易日 `_bars.clear()`，
                atr_period=14 → 开盘后前 15 根 bar 的 ATR 必然未就绪）R 只由 A 决定，
                A=0.1 时止损距离从 3.0 点塌到 1 tick —— 实测同一根普通 bar 下
                旧版持仓存活、新版第一根就判 sl。三支文案便于 grep 区分成因：
                  `[R 结构距离缺失]` = 信号压根没带分型（fractal ≤ 0 哨兵）；
                  `[R 结构距离归零]` = 带了分型但 A ≤ 0（穿越分型 / 分型贴身到等于入场价）；
                  `[R 结构距离偏小]` = 0 < A < 地板（分型贴身但未归零）。
                已知成因候选：① 信号未携带分型；② 入场价已穿越分型（陈旧信号）；
                ③ 分型贴身（A 极小）；④ 买卖点无右肩 K 线时 bsp.klu 退回
                bi.get_end_klu()（chan.py BuySellPoint/BS_Point.py），该 K 线收在
                自身极值点时 A = 0。
                ⚠️ 阈值单位是**报价点数**，IC/IM 的旧地板原为 5.0（比 3.0 宽一档）——
                本告警不按品种分档；要按 IC/IM 口径收窄，改类属性
                `LayeredExitPolicy.r_alert_a_floor`。

            [2] `R <= 0` → `[R 归零]`（评审补 · 用户要求"R=0 加控制台告警"）。
                R=0 是唯一会让 L3 整层失效的值（check() 里的 `R > 0` 判定），
                此时 stop/tp 全靠 P2 边界守卫压在入场价外 1 tick。用户判断
                「R 不可能为 0」，故此处**只出声不兜底**，也不给 tp 加对称防护 ——
                真在实盘抓到即用本条日志分析成因。
        """
        is_long = signal.side is Side.LONG
        # A：结构止损（分型极值）
        # fractal_low/fractal_high ≤ 0 表示信号未携带有效分型（哨兵值，价格为 0 不可能），
        # 此时视为「无结构止损信息」，A 钳 0 交给 B（2×ATR）兜底；
        # 否则会被误读成「分型最低点 = 0」→ A = entry_price → 止损打飞到 ~0，SL 永不触发。
        A = 0.0
        _fractal_missing = False
        if is_long:
            if signal.fractal_low > 0:
                A = max(entry_price - signal.fractal_low, 0.0)
            else:
                _fractal_missing = True
        else:
            if signal.fractal_high > 0:
                A = max(signal.fractal_high - entry_price, 0.0)
            else:
                _fractal_missing = True
        # B：波动率止损（2×ATR）。atr 只取一次，供 B 与下面两条告警共用
        #   （评审修 · P3：原实现告警里又调了一次 self._atr()，
        #   同一次判定里重复计算）。
        B = 0.0
        atr = self._atr() if self.use_atr else None
        if atr:
            B = self.atr_sl_multiple * atr
        # 观测告警 [1]：A < 地板（默认 3.0）。不改 R 的取值，只把现场打出来。
        #   分三支，便于 grep 时一眼区分成因（详见 docstring）：
        #     [R 结构距离缺失] = 信号压根没带分型（fractal ≤ 0 哨兵）→ 查信号源；
        #     [R 结构距离归零] = 带了分型但 A ≤ 0（穿越分型 / 分型贴身到等于入场价）；
        #     [R 结构距离偏小] = 0 < A < 地板（分型贴身但未归零）→ 查行情与分型口径。
        if A < self.r_alert_a_floor:
            _kind = ("结构距离缺失" if _fractal_missing
                     else "结构距离归零" if A <= 0.0
                     else "结构距离偏小")
            _log.warning(
                "[R %s] A=%.6g < 阈值 %.6g：R 可能只由结构距离 A 决定（B 未就绪时尤其）"
                "：side=%s entry=%.6g fractal_low=%.6g fractal_high=%.6g atr=%s "
                "B=%.6g signal_key=%s —— 请核对分型是否缺失/穿越/贴身"
                "（本条仅观测，R = max(A, B) 不变；阈值见 LayeredExitPolicy."
                "r_alert_a_floor）",
                _kind, A, self.r_alert_a_floor,
                getattr(signal.side, "value", signal.side), entry_price,
                signal.fractal_low, signal.fractal_high, atr, B,
                getattr(signal, "key", "?"))
        R = max(A, B)
        # 观测告警 [2]：R 归零（评审补 · 用户要求）。
        #   用户判断「R 不可能为 0」→ 本函数不兜底、不钳下限，也不给 tp 加对称防护；
        #   只在真发生时出声，便于事后捞样本分析成因。
        if R <= 0.0:
            _log.warning(
                "[R 归零] R=0（A=%.6g、B=%.6g）：本次入场 stop/tp 会退化为入场价外 "
                "1 tick，且 L3 保本/跟踪整层跳过（check() 的 R > 0 判定）。side=%s "
                "entry=%.6g atr=%s signal_key=%s —— 本条仅观测（不兜底、不钳下限），"
                "请提供该现场供分析",
                A, B, getattr(signal.side, "value", signal.side), entry_price, atr,
                getattr(signal, "key", "?"))
        return R

    # ---------- 开仓时生成出场计划 ----------
    def plan(self, signal: Signal, entry_price: float, state: "Instrument",
             anchor: Optional[float] = None) -> ExitPlan:
        """生成出场计划。

        anchor = 风控锚（解锁重算时传解锁成交价 P₂）；None 时用 entry_price（正常开仓）。
        会计锚 entry_price 与风控锚 anchor 分离：解锁后剩余持仓的 entry_price 保持 P₀（对账不动），
        但止盈/止损/保本/跟踪全部以 anchor（P₂）为基准重算，避免用陈旧的 P₀ 导致
        "开仓即触发"或"止损远在天边"。
        """
        base = anchor if anchor is not None else entry_price
        is_long = signal.side is Side.LONG
        min_gap = state.price_tick
        R = self._initial_r(signal, base, state)
        stop_dist = R
        tp_dist = self.win_loss_ratio * R

        if is_long:
            raw_stop = base - stop_dist - self.stop_buffer_ticks * state.price_tick
            raw_tp = base + tp_dist
            stop = state.round_price(raw_stop, "up")        # 易触发（保守）
            nominal_tp = state.round_price(raw_tp, "down")  # 难触发（保守）
        else:
            raw_stop = base + stop_dist + self.stop_buffer_ticks * state.price_tick
            raw_tp = base - tp_dist
            stop = state.round_price(raw_stop, "down")
            nominal_tp = state.round_price(raw_tp, "up")

        # 跟踪止盈模式（use_trailing=True，默认）：**不落固定止盈单**，止盈交给 L3 的
        #   ATR 跟踪兑现。L3 启动阈值 = win_loss_ratio×R（品种档案，IC/IM=3R、其余=2R），
        #   故不同品种的"进 L3 时机"天然不同；名义止盈价（= win_loss_ratio×R）仍写入
        #   params，供事后对照分析。固定止盈单模式（use_trailing=False）则落固定止盈单。
        tp = None if self.use_trailing else nominal_tp

        # P2 防护：止损必须严格在风控锚的"不利侧"且至少 1 tick 间距，
        # 否则遇到陈旧信号（行情已走远）会变成"开仓即触发止盈"的反向单。
        if is_long:
            if stop is not None and stop >= base - min_gap:
                stop = state.round_price(base - max(stop_dist, min_gap), "down")
        else:
            if stop is not None and stop <= base + min_gap:
                stop = state.round_price(base + max(stop_dist, min_gap), "up")

        params = dict(self.params)
        params["R"] = R
        params["_tp_nominal"] = nominal_tp  # 名义止盈价（跟踪止盈模式下不落单，仅供事后对照）
        params["_trail_best"] = base              # 跟踪极值初值 = 风控锚（或入场价）
        if anchor is not None:
            params["risk_anchor"] = anchor        # 风控锚（解锁重算时 = P₂）
        return ExitPlan(name=self.name, stop_price=stop, tp_price=tp, params=params)

    # ---------- 每根 bar 闭合后判定 ----------
    def check(self, position: Position, bar: Bar, state: "Instrument",
              bars_held: int = 0) -> Optional[ExitCheck]:
        plan = position.exit_plan
        stop = plan.stop_price
        tp = plan.tp_price
        is_long = position.side is Side.LONG
        # 风控基准：解锁重算的持仓用 risk_anchor（P₂），否则用会计锚 entry_price（P₀）。
        #   会计锚 entry_price 只用于 pnl_points 对账；风控（保本/跟踪/止损比较）一律用本基准。
        ra = plan.params.get("risk_anchor")
        entry = ra if ra else position.entry_price
        # R 快照缺失（旧版本 state.db 恢复的持仓）→ L3 跳过：保本/跟踪是 R 倍数语义，
        #   R 未知时激进触发反而危险；止损线 / 止盈线均不依赖 R，不受影响
        R = plan.params.get("R")
        R = float(R) if R is not None else None
        atr = self._atr()
        # ① 止损线 / 止盈线判定（L1 结构 R + L2 ATR 定宽给出的止损线；止盈线由固定止盈单
        #   模式提供，跟踪止盈模式下 tp is None）。
        #   **价格输入 = 本根 K 线的收盘价 `bar.close`**（2026-09-22 口径，见模块 docstring）：
        #   不再用 `bar.low` / `bar.high` —— 判定的时刻是"这根闭合之后"，依据也用"这根结束时
        #   的那个价"，两者才是同一个东西。代价是止损更晚更深（收盘才认），换来的是不被
        #   插针 / 瞬间打穿扫掉。
        #   顺序上 sl 先判、tp 后判：**不是**"同根双破取悲观"那条旧规则（收盘价口径下
        #   "同根既破止损又破止盈"已不可达），只是对畸形计划的确定性兜底。
        #   跟踪止盈模式（use_trailing=True）下 plan 不生成止盈单（tp is None），故 tp 分支
        #   只对固定止盈单模式（use_trailing=False）与旧 state.db 恢复的存量持仓生效。
        close = bar.close
        if is_long:
            if stop and close <= stop:
                return ExitCheck("sl", stop, fill_price=close)
            if tp is not None and close >= tp:
                return ExitCheck("tp", tp, fill_price=close)
        else:
            if stop and close >= stop:
                return ExitCheck("sl", stop, fill_price=close)
            if tp is not None and close <= tp:
                return ExitCheck("tp", tp, fill_price=close)

        # ③ L3 移动/保本锁利（只更新计划、不登场）
        #   R 缺失（旧版本 state.db 恢复的持仓）或 R ≤ 0 → 整层跳过。把 ">0" 显式写出
        #   （评审补）：原先只靠 `and R` 的真值判定，R=0 与 R 缺失混在同一支
        #   里被静默吞掉；显式化后行为不变，但把「R=0 则 L3 不跑」这条写在明处
        #   （R=0 是否发生由 _initial_r 的 [R 归零] 观测点负责；本行只负责不跑 L3）。
        if self.use_trailing and R is not None and R > 0:
            best = float(plan.params.get("_trail_best", entry))
            prev_best = best
            # fav_profit 用**收盘价**衡量（与 L1/L2 的触发判定同一口径，2026-09-22）：
            #   best = 至今见过的最好收盘价（单调），浮盈 = (best − 风控锚)·sign。
            #   与旧的"根内有利极值"口径的差别：盘中冲高到 win_loss_ratio×R 而收盘又回落的
            #   那根 K 线，不再算作"达标" —— 进 L3 / 抬保本都会晚一根。这是刻意的：
            #   依据与时刻统一到收盘价，不用一个已经不存在的极值去抬止损。
            #   win_loss_ratio 即 L3 触发阈值（品种级，不再有独立的 trailing_trigger_r）。
            best = max(best, close) if is_long else min(best, close)
            fav_profit = (best - entry) * position.side.sign  # (best−风控锚)·sign
            # 跟踪是否已启动（best 单调，故启动后恒为 True，不随回落下线）
            tracking_started = (self.win_loss_ratio > 0
                                and fav_profit >= self.win_loss_ratio * R)
            new_stop = stop

            # 保本/锁利：浮盈 ≥ breakeven_trigger_r·R → 止损抬至 入场价 ± breakeven_buffer_r·R
            #   breakeven_buffer_r=0 → 真正保本（止损=入场价）；=0.5 → 锁定 0.5R（与品种/周期解耦）
            if self.breakeven_trigger_r > 0 and fav_profit >= self.breakeven_trigger_r * R:
                be = (entry + self.breakeven_buffer_r * R) if is_long \
                    else (entry - self.breakeven_buffer_r * R)
                be = state.round_price(be, "up" if is_long else "down")
                if (is_long and be > new_stop) or (not is_long and be < new_stop):
                    new_stop = be

            # 跟踪：浮盈 ≥ win_loss_ratio·R → 跟踪止损（trail_dist = trailing_trigger_r × R，
            #   R 倍数口径，与 breakeven_*_r 同单位；只朝有利方向移动）
            if self.win_loss_ratio > 0 and fav_profit >= self.win_loss_ratio * R:
                trail_dist = self.trailing_trigger_r * R
                if trail_dist and trail_dist > 0:
                    tgt = (best - trail_dist) if is_long else (best + trail_dist)
                    tgt = state.round_price(tgt, "up" if is_long else "down")
                    if (is_long and tgt > new_stop) or (not is_long and tgt < new_stop):
                        new_stop = tgt

            # 回写条件（二选一，避免每根 bar 都刷事件日志）：
            #   a) 止损真的动了；
            #   b) 跟踪已启动且"最好收盘价"创新高 —— 补旧实现的缺口：原实现只在 new_stop
            #      变化时回写 _trail_best，"新高但止损未变"（如 ATR 同步放大）时该值被丢弃，
            #      后续跟踪距离偏松。保本阶段（跟踪未启动）不回写，避免日志刷屏。
            # 阶段标记（需求 ⑷(3)(4)，2026-09-18）：引擎据此在阶段跃迁时 toast。
            # 策略层只负责标注当前风控阶段，通知职责在引擎。
            if tracking_started:
                phase = "trailing"
            elif (self.breakeven_trigger_r > 0
                    and fav_profit >= self.breakeven_trigger_r * R):
                phase = "breakeven"
            else:
                phase = ""
            if new_stop != stop or (tracking_started and best != prev_best):
                params = dict(plan.params)
                params["_trail_best"] = best
                params["_phase"] = phase
                return ExitCheck("trailing", 0.0, only_update=True,
                                plan=ExitPlan(self.name, new_stop, tp, params))
        return None



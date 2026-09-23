# -*- coding: utf-8 -*-
"""
出场策略（Exit.py）
====================
精简：出场只有一个策略 `LayeredExitPolicy`（L1-L3 分层出场）。原可选的
`DefaultExitPolicy`（简单固定点数出场）、L4 时间/收盘兜底早已删除；“固定止盈单”
那套（`use_trailing=False` 开关 + `ExitPlan.tp_price`）也已整条删除 —— 出场只有
L1-L3 一套，止盈只有 L3 跟踪一种。

价格输入口径（2026-09-22 · 用户拍板 → 同日二次拍板修正）：**触发判据与达标判据用不同
的价，这是刻意的，不要"顺手统一"它们。**
    · **触发判据**（止损线 / 止盈线 / 保本价 / 跟踪价是否被穿过 → 是否报单离场）
      只读本根 K 线的**收盘价** `bar.close`，不读 `bar.high` / `bar.low`。理由：判定
      **时刻**是"这根 K 线闭合之后"，依据就必须是"这根结束时的那个价"；拿一个盘中到过、
      现在已经消失的价去触发一笔按当时盘口成交的单，依据与时刻不是同一个东西。
      由此，「同根 K 线同时触及止盈与止损 → 按止损计（悲观）」这条旧兜底规则**已不可达**
      （一个收盘价不可能既 > 止盈线又 < 止损线）—— 规则随口径一并删除。
    · **达标判据**（浮盈是否够 1R 进保本 / 够 win_loss_ratio×R 进跟踪，以及跟踪锚
      "至今最好价"）读本根 K 线的**有利侧极值**：做多 `bar.high`、做空 `bar.low`。
      理由：它衡量的不是"能否成交"，而是"这一段行情最远走到过哪里"。用收盘价衡量会把
      "盘中冲高 1.5R、收盘回落到 0.3R"的那根判成**不达标**，于是本可锁住的利润继续裸奔
      到 −1R。极值口径下那根即进保本。代价见下一条。
      ⚠️ 由此产生**当根离场**（2026-09-22 · 用户拍板"先抬价、再判触发"，本段同步改写）：
      达标用的极值可能远优于收盘价，而新保护价是按极值算出来的 —— 于是同一个极值既会把
      保护价上抬，也会让"当前收盘"看起来已经跌破它。若本根收盘确实落在**新**保护价的
      不利侧（例：high 冲到 +3R、跟踪价设在 +2.5R，而收盘回到 +2R），**本根即判离场**，
      以该根收盘价作成交参考价（`fill_price`）。
      这是刻意的：极值与收盘在"这根闭合之后"都已是定局，没有理由把已经回落的区间拖到
      下一根才处理 —— 一根振幅过大的 K 线因此**当根**就能离场。旧实现（触发判据排在
      达标之前、达标命中即 `only_update=True` 返回）把新保护价的生效推迟到下一根，已废弃。
      代价是"冲高回落"形态里下车更早，也更容易被一根长上影线打掉；换来的是不再承担
      "达标根收盘 → 下一根收盘"之间的漂移。两边优劣取决于达标根之后那根的收盘分布，
      **不是"更早锁利"这么单向**。
      本条由 `test_p61` 的 [6]/[7] 两组钉死（抬价后的保护价恒参与本根触发判定；
      达标根收盘落在新价有利侧时才走 `only_update`）。
      对照：**收盘价达标口径**（历史口径，已废弃）下该形态不可能出现 —— 保本阈值
      1R > 保本缓冲 0.5R ⇒ 达标那根 close ≥ entry+1R 必然在 +0.5R 保护价的**有利侧**；
      跟踪同理：`stop = best − 0.5R` 且 best 取自 close ⇒ stop 恒在 close 的不利侧之外。
    · high/low 只允许经 `_fav_extreme()`（达标极值）与 `_atr()`（真实波幅 TR 的
      **定义式**，只做波动率度量、不参与任何判定）两处读，别处一律不得出现 ——
      由 `Trading/Test/test_p61_exit_close_only.py` 用 AST 钉死：`check()` 函数体内
      出现 `.high` / `.low` 即变红，全文件读 high/low 的函数只能是上面这两个。

    · **止损侧边界一律严格不等**（2026-09-22 · 用户拍板「改为需求原文口径 ＜ / ＞」）：
      止损线 / 保本价 / 跟踪价（后两者在本实现里都是**改写 `stop_price`**，见下方 ③）
      用 `close < stop`（多）/ `close > stop`（空）；两个"浮盈达标"阈值（进保本 / 进跟踪）
      用 `fav_profit > 阈值`。**一律不用 `<=` / `>=`** —— 即"收盘价恰好等于保护价"与
      "有利极值恰好等于 k×R"都**不算**触发 / 达标。
      为什么值得单列：价格离散到 tick，"恰好相等"是**可达状态**，不是概率为零的理论
      差异 —— 旧闭区间口径下这类行情会提前一格离场。改后方向一致：离场更晚一点。
      由 `test_p61_exit_close_only.py` 的 [3c] / [3e]（收盘 == 止损线 → 不离场）、
      [4b2]（有利极值 == 入场+1R → 不进保本）与 [5]（== 2R / 3R → 不启动跟踪）钉死。
      价格线**只有一条** `stop_price`（初始止损 → 保本 → 跟踪，逐级改写它），
      所以不存在“只改了止损、漏改止盈”的可能。

两个刻意保留的保守设定（LayeredExitPolicy）
    ① 价格对齐一律往"对自己不利"的方向取整（止损更易触发、止盈更晚更少）
    ② 出场计划里带上参数快照，落盘后可做事后参数敏感性分析

止盈一律交给 L3 跟踪
    plan() 生成**零个止盈单**；浮盈完全由 L3 的跟踪止损兑现。L3 启动阈值
    （= win_loss_ratio×R）直接取品种档案的 `win_loss_ratio`（IC/IM=3R、其余=2R），
    故“盈利到 win_loss_ratio×R 时进 L3 跟踪锁利”，不同品种进 L3 时机天然不同。
    名义止盈价（= win_loss_ratio×R）仍写入 params["_tp_nominal"] 供事后对照（只落盘）。
    ⚠️ 两个「×R」字段的分工（2026-09-14 / 2026-09-22 两次改口径，极易记反）：
      · **进 L3 的触发阈值** = 品种档案的 `win_loss_ratio`（IC/IM=3R、其余=2R）。
        判据：浮盈 **>** win_loss_ratio×R（浮盈按根内有利极值算，见 check() ①）。
      · **L3 的跟踪缓冲距离** = `trailing_trigger_r`（`Config.py:310`，默认 0.5）× R，
        只决定「保护价挂在最好极值下方多远」（check() 里的 `trail_dist`），
        **不参与**"要不要进 L3"的判定。

    沿革（为什么名字与语义对不上）：2026-09-14 前进 L3 用的是**独立的全局**
    `trailing_trigger_r`，与 win_loss_ratio 完全解耦 —— 结果是品种档案里的
    win_loss_ratio=3 成了无人读取的死配置（IC/IM 实际按那个全局值进 L3）。
    2026-09-14 起 L3 触发改走品种级 win_loss_ratio，IC/IM 才真正按 3R 进 L3。
    2026-09-22 又把**跟踪距离**的单位从 ATR 换成 R：删掉 `trailing_atr_multiple`
    （1.0×ATR），把这个**名字**复用为新距离的载体（0.5×R）。
    ⇒ `trailing_trigger_r` **没有被删**（`Config.py:310` 仍在、`check()` 仍在读），
      只是语义从「L3 触发阈值」变成了「跟踪距离」；名字里的 trigger 是历史遗留，
      读代码时按"跟踪距离 R 倍数"理解，别按名字理解。

    ⚠️ 术语沿革（2026-09-15 清理，2026-09-22 续）：出场策略**只有 LayeredExitPolicy
    一套（L1-L3）**，不存在两套可选的出场策略；L1-L3 里也**不存在"硬止损 / 硬止盈"这套
    说法** —— 止损线由 L1（结构 R）+ L2（ATR 定宽）给出，止盈由 L3（保本 / 跟踪）给出，
    “硬”字没有对应实体，一律不要再用。旧文档 / 旧注释里的「A 方案 / B 方案」是本策略
    历史上两套取值的遗留叫法（跟踪止盈 / 固定止盈单），同样废弃 —— 其中“固定止盈单”
    那套已连开关一起删除（见上），出场只有 L1-L3 一套，新写的代码 / 日志 / 报告一律用 L1-L3。
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

    fill_price = **建议成交参考价** = 触发判定用的那根 K 线收盘价（2026-09-22
    回测语义拍板）。⚠️ 它被消费，而且消费了几层要分清：
      · **作报单参考价 —— 三个通道都消费**：`Engine._force_exit` 取
        `ref = fill_price or 触发价`（Engine.py 的 `ref = float(fill_price) if
        fill_price else float(price or 0.0)`），再交给 `_execute(ref_price=ref)`；
        `_execute` 把它原样传给 `broker.submit(ref_price=...)`。DryRun 按它加滑点
        算成交价，SimNow 按它 `_build_limit_price()` 算限价（超价 / 对齐），
        实盘同理 —— 所以**不是**"只有纸面 broker 看它"。
      · **作成交价 —— 只有 DryRun**：纸面通道直接按它落账；SimNow / 实盘的
        实际成交价仍以柜台回报为准，本字段只影响报单价，**不覆盖**回报价。
    为什么要有它：收盘才认的口径下，触发时刻真实可实现的价就在收盘价附近，
    仍拿止损 / 保护价当成交价会系统性偏一格（触发价只负责"要不要报单"）。

    only_update=True 表示"只更新出场计划、不登场"——移动止损 / 跟踪止盈走这条路。
    此时 plan 必须给，price 无意义（也不给 fill_price）。

    ⚠️ 反过来不成立：`only_update=False`（登场离场）时 **plan 也可能非空** —— 同一根
    K 线可以"先按有利侧极值抬保护价、再按收盘价判出跌破"（2026-09-22 时序，见 check()）。
    调用方（Engine._settle_positions）遇到这种结果，要**先**把 plan 落盘 / 发阶段通知、
    **再**走离场 —— 否则"因进保本 / 进跟踪而离场"的因果链会在事件流里断掉。
    """
    reason: str                       # 规则身份（**不表达盈亏**，见 check() 的 reason 口径）：
                                      #   breakeven（保本层保护价）/ trailing（跟踪层保护价）/
                                      #   sl（初始止损线）/ time / custom（其它调用方自定）
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

    # ---------- L3 达标极值（本文件里为"是否达标"读 high/low 的唯一决策点） ----------
    @staticmethod
    def _fav_extreme(bar: Bar, is_long: bool) -> float:
        """本根 K 线的**有利侧极值**：做多 `bar.high`、做空 `bar.low`。

        ⚠️ 单独成函数是刻意的，不是在拆分代码：`check()` 里的**触发判据**（止损线 /
        止盈线 / 保本价 / 跟踪价是否被穿）只读 `bar.close`，而护栏
        `Trading/Test/test_p61_exit_close_only.py` 用 AST 断言「`check()` 函数体内
        不出现 `.high` / `.low`」把这条不变量钉死。达标侧要读极值，就必须**只经本函数**
        —— 谁在 `check()` 里直接写 `bar.high`，护栏立刻变红。
        """
        return float(bar.high if is_long else bar.low)

    # ---------- 保护价的"层身份" → reason ----------
    @staticmethod
    def _phase_reason(plan: ExitPlan) -> str:
        """保护价当前属于哪一层，即"被跌破时该记哪个 reason"（口径见 check()）。

        层身份来自计划快照 `params["_phase"]`（L3 在保护价真的被抬高时写入）。只有两种
        L3 层有名字；其余（未进 L3 的初始段 / 旧 state.db 恢复的、缺 `_phase` 的持仓）
        一律回落 "sl" —— 那正是"还没被抬过的初始保护价"。

        单独成函数是为了让"层身份"只有**一个**读取点：check() 读它、抬价后覆盖它。
        若两处各写一份 `params.get("_phase")` 的映射，早晚会出现"抬价处认了 breakeven、
        触发处仍按旧值记 sl"这类只坏一半的漏改。
        """
        phase = str(plan.params.get("_phase") or "")
        return phase if phase in ("breakeven", "trailing") else "sl"

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
                此时保护价全靠 P2 边界守卫压在入场价外 1 tick。用户判断
                「R 不可能为 0」，故此处**只出声不兜底**，也不给保护价加对称防护 ——
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
        #   用户判断「R 不可能为 0」→ 本函数不兜底、不钳下限，也不给保护价加对称防护；
        #   只在真发生时出声，便于事后捞样本分析成因。
        if R <= 0.0:
            _log.warning(
                "[R 归零] R=0（A=%.6g、B=%.6g）：本次入场的保护价会退化为入场价外 "
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

        # 不落任何止盈单：止盈交给 L3 的跟踪兑现。L3 启动阈值 = win_loss_ratio×R
        #   （品种档案，IC/IM=3R、其余=2R），故不同品种的“进 L3 时机”天然不同；
        #   名义止盈价（= win_loss_ratio×R）仍写入 params，供事后对照分析（只落盘）。

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
        params["_tp_nominal"] = nominal_tp  # 名义止盈价（不落单，仅供事后对照）
        params["_trail_best"] = base              # 跟踪极值初值 = 风控锚（或入场价）
        if anchor is not None:
            params["risk_anchor"] = anchor        # 风控锚（解锁重算时 = P₂）
        return ExitPlan(name=self.name, stop_price=stop, params=params)

    # ---------- 每根 bar 闭合后判定 ----------
    def check(self, position: Position, bar: Bar, state: "Instrument",
              bars_held: int = 0) -> Optional[ExitCheck]:
        plan = position.exit_plan
        stop = plan.stop_price
        is_long = position.side is Side.LONG
        # 风控基准：解锁重算的持仓用 risk_anchor（P₂），否则用会计锚 entry_price（P₀）。
        #   会计锚 entry_price 只用于 pnl_points 对账；风控（保本/跟踪/止损比较）一律用本基准。
        ra = plan.params.get("risk_anchor")
        entry = ra if ra else position.entry_price
        # R 快照缺失（旧版本 state.db 恢复的持仓）→ L3 跳过：保本/跟踪是 R 倍数语义，
        #   R 未知时激进触发反而危险；保护价本身不依赖 R，不受影响
        R = plan.params.get("R")
        R = float(R) if R is not None else None
        atr = self._atr()
        # 触发判据的价格输入 = 本根 K 线的**收盘价** `bar.close`（2026-09-22 口径，见模块
        #   docstring）：不用 `bar.low` / `bar.high` —— 判定的时刻是"这根闭合之后"，依据也用
        #   "这根结束时的那个价"，两者才是同一个东西。代价是止损更晚更深（收盘才认），换来
        #   的是不被插针 / 瞬间打穿扫掉。
        #   （与 L3 的**达标判据**刻意不同：那一层衡量"行情最远走到过哪里"，读
        #   `_fav_extreme()` 的根内极值。两层口径不同是设计，不要"顺手统一"。）
        #   （“同根双破取悲观”那条旧规则已随固定止盈单一并删除：收盘价口径 + 严格
        #   不等下本就不可达，且现在只有保护价一条线，更无从“双破”。）
        #   ⚠️ **止损侧三处一律严格不等**（2026-09-22 · 用户拍板"改为需求原文口径 ＜ / ＞"，
        #   见模块 docstring）：收盘价恰好等于保护价 → 本根不动。保本价、跟踪价的离场也走
        #   下面这两行 —— L3 是**改写 stop_price**、不另立字段（见 ① 末尾），故触发口径天然
        #   是同一条，不存在"只改一半"的可能。
        #   ✏️ reason 记的是**哪一层保护价被跌破**（2026-09-22 拍板：reason 只表达"规则身份"，
        #   **不表达盈亏**）：层身份取 `_phase_reason(plan)`，本根若抬了价则用**新**层覆盖它
        #   （见 ① 末尾）—— 保本离场也可能因滑点净亏、跟踪离场也可能只小赚，那是**成交结果**，
        #   只有 net_cash 说得清（统计侧的 wins / losses 正是按它分的）。
        close = bar.close
        stop_reason = self._phase_reason(plan)
        # 本根抬价后的新计划（None = 计划不动）。非空时 ② 用它的新保护价判定；且无论最终
        #   是"登场离场"还是"只更新计划"，都要把它交给调用方落盘 / 发阶段通知。
        updated: Optional[ExitPlan] = None

        # ① L3 达标判定：读本根**有利侧极值**升保护价 —— **必须排在触发判定之前**
        #   （2026-09-22 · 用户拍板改版；旧顺序"触发在前、达标命中即 return"已废弃）。
        #   为什么这个顺序才对：一根 K 线闭合时，"这段行情最远走到过哪里"（极值）与
        #   "这根收在哪里"（收盘）**都已经成为定局**。先按极值定出本根立即生效的保护价，
        #   再由 ② 用收盘价判它有没有被跌破 —— 振幅过大的那根因此**当根**就能离场，不必把
        #   已经回落的区间拖到下一根才处理（旧顺序下新保护价最快下一根才生效）。
        #   本块的 `_fav_extreme()` 调用行必须早于 ② 的触发比较行 —— 由 test_p61 的 [1] 组
        #   用 AST 行号钉死：只靠行为样本挡不住"有人把两段调回去"。
        #   R 缺失（旧版本 state.db 恢复的持仓）或 R ≤ 0 → 整层跳过。把 ">0" 显式写出
        #   （评审补）：原先只靠 `and R` 的真值判定，R=0 与 R 缺失混在同一支
        #   里被静默吞掉；显式化后行为不变，但把「R=0 则 L3 不跑」这条写在明处
        #   （R=0 是否发生由 _initial_r 的 [R 归零] 观测点负责；本行只负责不跑 L3）。
        if R is not None and R > 0:
            best = float(plan.params.get("_trail_best", entry))
            prev_best = best
            # fav_profit 用**根内有利侧极值**衡量（做多 high / 做空 low，2026-09-22 二次口径，
            #   与触发判据刻意分开，见模块 docstring）：
            #   best = 至今见过的最好极值（单调），浮盈 = (best − 风控锚)·sign。
            #   为什么用极值而不是收盘价：达标衡量的是"行情最远走到过哪里"，不是"能否成交"。
            #   盘中冲高到 win_loss_ratio×R 而收盘回落的那根，即算达标 → 保护价当根就抬；
            #   而那根收盘若已落在**新**保护价的不利侧，紧接着的 ② 就当根判离场（代价见
            #   模块 docstring）。
            #   本文件里为"是否达标"读 high/low 的**唯一**决策点 = `_fav_extreme()`
            #   （护栏 test_p61 钉死：check() 里直接写 bar.high 立刻变红）。
            #   win_loss_ratio 即 L3 触发阈值（品种级；`trailing_trigger_r` 不再是
            #   触发阈值、只是跟踪距离，分工见模块 docstring）。
            _ext = self._fav_extreme(bar, is_long)
            best = max(best, _ext) if is_long else min(best, _ext)
            fav_profit = (best - entry) * position.side.sign  # (best−风控锚)·sign
            # 跟踪是否已启动（best 单调，故启动后恒为 True，不随回落下线）
            #   ⚠️ 阈值比较取**严格大于**：有利极值恰好 == win_loss_ratio×R 不算启动
            #   （2026-09-22 边界口径，见模块 docstring）。
            tracking_started = (self.win_loss_ratio > 0
                                and fav_profit > self.win_loss_ratio * R)
            new_stop = stop

            # 保本/锁利：浮盈 **>** breakeven_trigger_r·R → 保护价抬至 风控锚 ± breakeven_buffer_r·R
            #   breakeven_buffer_r=0 → 真正保本（保护价 = 风控锚）；=0.5 → 锁定 0.5R（与品种/周期解耦）
            #   恰好等值那根不抬（严格不等；与该阈值同为"严格"的还有上面的 tracking_started）。
            if self.breakeven_trigger_r > 0 and fav_profit > self.breakeven_trigger_r * R:
                be = (entry + self.breakeven_buffer_r * R) if is_long \
                    else (entry - self.breakeven_buffer_r * R)
                be = state.round_price(be, "up" if is_long else "down")
                if (is_long and be > new_stop) or (not is_long and be < new_stop):
                    new_stop = be

            # 跟踪：浮盈 **>** win_loss_ratio·R → 跟踪保护价（trail_dist = trailing_trigger_r × R，
            #   R 倍数口径，与 breakeven_*_r 同单位；只朝有利方向移动）
            if tracking_started:
                trail_dist = self.trailing_trigger_r * R
                if trail_dist and trail_dist > 0:
                    tgt = (best - trail_dist) if is_long else (best + trail_dist)
                    tgt = state.round_price(tgt, "up" if is_long else "down")
                    if (is_long and tgt > new_stop) or (not is_long and tgt < new_stop):
                        new_stop = tgt

            # 阶段标记（需求 ⑷(3)(4)，2026-09-18）：引擎据此在阶段跃迁时 toast。
            # 策略层只负责标注当前风控阶段，通知职责在引擎。
            if tracking_started:
                phase = "trailing"
            elif (self.breakeven_trigger_r > 0
                    and fav_profit > self.breakeven_trigger_r * R):
                phase = "breakeven"
            else:
                phase = ""
            # 回写条件（二选一，避免每根 bar 都刷事件日志）：
            #   a) 保护价真的动了；
            #   b) 跟踪已启动且"最好极值"创新高 —— 补旧实现的缺口：原实现只在 new_stop
            #      变化时回写 _trail_best，"新高但保护价未变"（如 ATR 同步放大）时该值被丢弃，
            #      后续跟踪距离偏松。保本阶段（跟踪未启动）不回写，避免日志刷屏。
            #   两个条件都蕴含 phase 非空（抬价 ⇐ 对应层达标），故 reason 一定能跟上新层。
            if new_stop != stop or (tracking_started and best != prev_best):
                params = dict(plan.params)
                params["_trail_best"] = best
                params["_phase"] = phase
                updated = ExitPlan(self.name, new_stop, params=params)
                stop = new_stop                       # 本根起生效：② 判的就是这条**新**保护价
                if phase in ("breakeven", "trailing"):
                    stop_reason = phase               # reason 跟着"最后抬价的那一层"

        # ② 触发判定（只读本根收盘价；比的是**本根生效**的保护价 —— ① 抬过就是新价）
        if is_long:
            if stop and close < stop:
                return ExitCheck(stop_reason, stop, fill_price=close, plan=updated)
        else:
            if stop and close > stop:
                return ExitCheck(stop_reason, stop, fill_price=close, plan=updated)

        # ③ 只更新计划、不报单（保本 / 跟踪位移）：计划已动，交给调用方落盘 + 发阶段通知
        if updated is not None:
            return ExitCheck(stop_reason, 0.0, only_update=True, plan=updated)
        return None



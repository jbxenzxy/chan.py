# -*- coding: utf-8 -*-
"""
Broker 接口（可插拔点 ③）
=========================
引擎只认这个接口，不认 SimNow / 创元 / dry-run。
接真实账户时新建一个类实现 `submit()`，在 Trading/Config.py 里换 name 即可，
引擎与策略层一行都不用改。

submit() 被设计成**同步返回 Order**，是为了让 dry-run 与真实 CTP 语义统一：
真实 CTP 是异步回执，届时在 broker 内部用 wait_update 阻塞到终态再返回，
对外仍是同步的。这样引擎的状态机不用为异步改写成回调地狱。

订单意图（重构）
---------------------------
submit() 只接受两种意图：**OPEN**（开仓）与 **CLOSE**（平仓）。
买还是卖由调用方的 `side` 决定，broker 只负责把 (side, offset) 翻成 CTP 报文。

历史上这里有 LOCK / UNLOCK 两个意图，但它们与 OPEN / CLOSE 报文完全等价，
只是引擎的记账标签，已删除。现在"这笔仓是开出来的还是锁出来的"不是
broker 需要知道的事。

`is_exit` 参数为什么必须有
    broker 无法从 (side, offset) 推断"该不该追价"，而这个差别是刚性的：
      · 入场（交易信号触发）不追价 —— "没成交最多不赚钱，但不会亏钱"
      · 离场（L1-L3 止盈止损触发）必须追价 —— 卡单不追，浮亏扩大、浮盈变浮亏
    两种情形的**报文一模一样**（例：运行态 L1-L3 触发的反向 OPEN，
    与空仓态信号触发的 OPEN 都是 offset=OPEN）。故由引擎按触发源显式给出。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

from ..Infra.Instrument import Instrument
from ..Infra.Records import Order, OrderIntent, Side

BROKERS: Dict[str, Type["Broker"]] = {}


def register_broker(cls: Type["Broker"]) -> Type["Broker"]:
    BROKERS[cls.name] = cls
    return cls


def build_broker(name: str, instrument: "Instrument",
                 params: Optional[Dict[str, Any]] = None,
                 state: Optional["Instrument"] = None) -> "Broker":
    """按名字构造 broker。

    `instrument` = **唯一一份运行时对象**（双类合并：
    静态身份 + 有效 tick/乘数（SSOT=品种档案）+ verified + 定价成本都在它上面）。
    `state` 参数保留仅为调用点兼容：传入值必须与 instrument 同一对象
    （或 None），否则 ValueError —— 合并后"spec 与 state 两份对象"不存在了。
    生产路径 main.py 建好一份同时交给 Broker 与 Engine；漏传时 broker 会
    直接用 instrument 自身（本来就是同一个），不会再出现"两边各建一份"
    导致 SimNow 写的 verified 引擎看不见的问题。
    """
    if name not in BROKERS:
        raise KeyError("未注册的 broker: {}（已注册: {}）".format(name, list(BROKERS)))
    if state is not None and state is not instrument:
        raise ValueError(
            "P-B 合并后 spec 与 state 是同一个 Instrument 对象："
            "build_broker(state=…) 传入的必须是 instrument 自身（或省略）")
    return BROKERS[name](instrument, params or {})


# intent → CTP OpenCloseType 的权威表
# 三个值（需求 ⑵ + D6）：开仓 → OPEN，平昨 → CLOSE，平今 → CLOSETODAY。
# **本表不参与方向决策** —— 买还是卖由调用方给的 `side` 决定。
#
# tqsdk 白名单硬校验（api.py:1353 / lib/utils.py:39 / scenario/tqscenario.py:445）：
#   offset ∈ ("OPEN", "CLOSE", "CLOSETODAY")，其它值**直接 raise**（不是 CTP 拒单，
#   是 SDK 本地抛错，被 except 吞掉后表现为"下单失败"，极易误判成通道问题）。
#   修正（P0）：原 UNLOCK 的值 "CLOSEYESTERDAY" 不在白名单 →
#   实盘 insert_order 本地抛异常 → 跨日解锁 100% 失败。
#   tqsdk 文档口径：上期所/上期能源平昨用 "CLOSE"，**其他交易所（含中金所）平仓直接用 "CLOSE"**。
#
# CLOSETODAY 的启用判据：**品种执行策略表第 1 列**
#   （`ExecPolicy.today_exit == "CLOSETODAY"`）—— 用户按费率自己算定后填表，
#   代码只读表，不从费率推导、也不看交易所名字（原按 SHFE/INE 能力守卫的
#   `Instrument.supports_closetoday` 已删除）。
#   引擎侧守卫 = 转移④ 分支条件 + `_pre_trade_check`（校验该品种表第 1 列确为 CLOSETODAY）。
# 一期本表只有两项、刻意不开平今口子（原 A4 注释）；启用第三项。
INTENT_TO_OFFSET: Dict[OrderIntent, str] = {
    OrderIntent.OPEN: "OPEN",              # 买开 / 卖开；④ 反向开仓锁仓也走它
    OrderIntent.CLOSE: "CLOSE",            # 买平 / 卖平；恒作用于跨日仓（平昨）
    OrderIntent.CLOSETODAY: "CLOSETODAY", # 平今；按品种执行策略表启用，目标恒为今仓
}


# ─── 拒单原因分类（D10，2026-09-11；补第四类 position）──────────
# 追价是有代价的（滑点 + 报撤单额度 + 中金所"频繁报撤单"监管计数，见风险 R13）。
# 但并不是所有拒单都值得追：
#   · 盘口深度不够（FOK 全撤）→ 价格会动，追了有用
#   · 资金不足 / 非交易时段   → 追 100 次也不可能成交，纯亏
#   · 平仓量超过持仓量（柜台无此仓）→ 追 100 次也一样，且它是"簿实不符"的唯一信号
# 分类器把 CTP 的 last_msg 归到四类，调用方据此决定要不要继续追。
REJECT_FUNDS = "funds"              # 资金不足 → 立即停追，需人工加保证金
REJECT_NOT_TRADABLE = "not_tradable"  # 非交易时段 / 集合竞价 / 无权限 → 立即停追
REJECT_POSITION = "position"        # 平仓量超过持仓量 / 平今·平昨仓位不足 → 立即停追
REJECT_PRICE = "price"              # 价格不可达（FOK 全撤）→ 继续追

# 追价无用的三类：命中即 break
NO_CHASE_REJECT_CLASSES = (REJECT_FUNDS, REJECT_NOT_TRADABLE, REJECT_POSITION)

# CTP 错误码（核实，来源：CTP_API 错误代码大全 + 申银万国官方报错释义）
_CTP_CODE_FUNDS = ("31",)                       # 资金不足
_CTP_CODE_NOT_TRADABLE = ("17", "28")           # 17 合约不能交易 / 28 无报单权限
#   原把 30/50/51 一并归在 not_tradable，现独立成 position —— 见下。
_CTP_CODE_POSITION = ("30", "50", "51")
#   30 平仓量超过持仓量 / 50 平今仓位不足 / 51 平昨仓位不足
#
# 为什么这三码必须单独一类：
#   `not_tradable` 与 `position` 对**追价**的判断一致（都停追），但对**引擎记账**
#   的语义完全不同 —— 只有「柜台说没有这个仓」才是"幻影仓"的判据，
#   柜台明确说"没有这笔可平仓"的信号（引擎不做自动清簿）。
#   混在一起会让"资金不足"这种拒单也被当成幻影仓清掉（真仓被误删 = 账实不符）。

# 非交易时段**没有稳定的数字码**，各期货公司文本还不一样 → 只能关键字兜底。
_CTP_KW_NOT_TRADABLE = ("非交易", "不在交易时间", "禁止此操作", "不在报单时间",
                        "未开盘", "已收盘", "交易时间段", "当前状态不允许")
_CTP_KW_FUNDS = ("资金不足", "保证金不足", "可用资金不足")
# 仓位类关键字（补）：各期货公司文本不一致，故按"超持仓 / 仓位不足"
# 两个方向各列几种常见写法；命中即认定柜台无此仓（or 可用量不足）。
_CTP_KW_POSITION = ("平仓量超过持仓量", "平仓量超持仓", "超过持仓量", "超过持仓",
                    "仓位不足", "持仓不足", "可平仓位不足", "可用持仓不足",
                    "平昨仓位不足", "平今仓位不足")


def classify_ctp_reject(last_msg: str) -> str:
    """把 CTP 的 `last_msg` 归到 `funds` / `not_tradable` / `position` / `price` 四类。

    判定顺序：数字码优先（权威），关键字兜底（"非交易时段"无稳定码）。
    认不出来一律归 `price` —— **宁可多追一次，不可漏掉一次真能成的离场**。

    ⚠️ 各期货公司的中文表述不一致（"当前状态禁止此操作" / "不在交易时间" /
    "非交易时间段"…），关键字表需上线后抓真实样本校准（未决问题 Q8）。
    """
    msg = str(last_msg or "")
    # 错误码形态：CTP:12345,xxx  /  "错误码 31"  /  "[31]" 等，取所有数字串逐个比
    for tok in _CTP_CODE_FUNDS:
        if _code_hit(msg, tok):
            return REJECT_FUNDS
    for tok in _CTP_CODE_POSITION:
        if _code_hit(msg, tok):
            return REJECT_POSITION
    for tok in _CTP_CODE_NOT_TRADABLE:
        if _code_hit(msg, tok):
            return REJECT_NOT_TRADABLE
    for kw in _CTP_KW_FUNDS:
        if kw in msg:
            return REJECT_FUNDS
    for kw in _CTP_KW_POSITION:
        if kw in msg:
            return REJECT_POSITION
    for kw in _CTP_KW_NOT_TRADABLE:
        if kw in msg:
            return REJECT_NOT_TRADABLE
    return REJECT_PRICE


def _code_hit(msg: str, code: str) -> bool:
    """判断 msg 里是否含有独立成词的错误码 code（避免匹配到价格/手数里的数字）。"""
    i = msg.find(code)
    while i >= 0:
        before = msg[i - 1] if i > 0 else " "
        after = msg[i + len(code)] if i + len(code) < len(msg) else " "
        if not before.isdigit() and not after.isdigit():
            return True
        i = msg.find(code, i + 1)
    return False


class Broker(ABC):
    name: str = "base"
    # （A′，2026-09-17 改造）：是否离线通道。
    #   基类默认 False（保守）—— 未知/真实通道受 Engine 的在线闸门管束：
    #   连接成功（broker 侧置 `self.state.verified = True`，来源标 CONFIG）
    #   才许下单。仅 dry_run 覆盖为 True。
    #
    #   ⚠️ 新增 broker 通道必读：
    #   默认 False 意味着**不声明就不放行**（Engine._pre_trade_check 的
    #   instrument_unverified 闸门，且拒得很安静 —— 只有 D11 告警）。接新通道时
    #   **二选一**：
    #     ① 在线通道 —— 连接建立成功后置 `self.state.verified = True`
    #        （合约参数 SSOT=品种档案，无行情校验环节；参照 SimNow._connect）；
    #     ② 拿不到在线连接 → 显式 `is_offline = True`（CONFIG_OFFLINE 路径，
    #        仅回测/模拟可接受）。
    is_offline: bool = False

    # 运行时状态引用。类属性声明 + 惰性实例化
    #   （同 `_pending_alerts` 惯例）：`__new__` 手工装配的 broker 子类
    #   （大量单测这么干）不必调 super().__init__ 也能拿到 state。
    _state: Optional["Instrument"] = None

    # ── （O-2/O-3）：broker → Engine 告警回流 ──
    # broker 侧的 instrument 故障（行情超时 / nan / 与配置不一致）原来只写
    # logging，D11 前端完全看不到。现在 broker 用 notify() 暂存进本队列，
    # Engine 每根 bar 调 drain_alerts() 取走并转手 Engine.alert（D11 通道）。
    # 类属性声明 + 惰性实例化：子类（含测试里 __new__ 手工装配的）不必调 super().__init__。
    _pending_alerts: Optional[List[Dict[str, Any]]] = None

    def notify(self, level: str, code: str, msg: str, **extra) -> Dict[str, Any]:
        """broker 侧告警入队（"Broker → Engine.alert()"）。

        level/code 语义与 Engine.alert 对齐（"warn"/"severe"）；extra 透传
        （field / quote / cfg 等诊断字段）。返回入队的 dict（便于测试断言）。
        """
        if self._pending_alerts is None:
            self._pending_alerts = []
        alert: Dict[str, Any] = {"level": level, "code": code, "msg": msg,
                                 "broker": self.name}
        if extra:
            alert.update(extra)
        self._pending_alerts.append(alert)
        return alert

    def drain_alerts(self) -> List[Dict[str, Any]]:
        """取走全部暂存告警并清空队列（Engine 每根 bar 调用一次）。"""
        if not self._pending_alerts:
            return []
        out, self._pending_alerts = self._pending_alerts, []
        return out


    def __init__(self, instrument: "Instrument",
                 params: Optional[Dict[str, Any]] = None,
                 state: Optional["Instrument"] = None):
        # 双类合并 —— 唯一一份运行时对象（instrument）。
        # D-C：原 spec 兼容别名**已删除** —— 本类现在只有
        #   `state` 一个属性名（与 Engine / Source 同名），不再"两个属性指同一
        #   块内存"。`instrument` 与 `state` 参数仍须同一对象，否则显式报错。
        if state is not None and state is not instrument:
            raise ValueError(
                "P-B 合并后 instrument 与 state 是同一个 Instrument 对象（got 不同实例）")
        self._state: "Instrument" = state if state is not None else instrument
        self.params: Dict[str, Any] = dict(params or {})
        # R1：报单序号（跨重启唯一性）。见 order_seq / seed_order_seq 注释。
        self._order_seq: int = 0

    @property
    def state(self) -> "Instrument":
        """合约运行时对象（合并后**唯一一份**；D-C 起本类只有这一个名字）。

        D-C：原 `self.spec` 回落分支已删 —— `__new__` 手工装配的
        替身（不跑 `__init__`）**必须显式赋 `state=`**，否则这里明确报错，
        而不是悄悄回落到另一个名字上（那正是要消掉的歧义）。
        """
        st = getattr(self, "_state", None)
        if st is None:
            raise RuntimeError(
                "broker.state 未初始化：不跑 __init__ 的装配路径必须显式赋 state=…")
        return st

    @state.setter
    def state(self, value: "Instrument") -> None:
        self._state = value

    @abstractmethod
    def submit(self, intent: OrderIntent, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "",
               entry_date: str = "", is_exit: bool = False) -> Order:
        """提交委托并等待终态。

        intent: 订单意图 —— 只有两种（需求 ⑵）
          - OPEN   开仓：买信号 → 买开，卖信号 → 卖开
          - CLOSE  平仓：买信号 → 买平，卖信号 → 卖平
        side:     方向。与 intent 合起来唯一确定 CTP 报文
                  （OPEN+多 = 买开；CLOSE+多 = 卖平 …）
        ref_price: 策略参考价（开仓 = 信号 K 线收盘价；平仓 = 触发价）
        entry_date: 被平持仓的建仓交易日（YYYY-MM-DD）。**仅用于审计/诊断**
          （DryRun 写入 Order.meta），不参与 offset 选择。
        is_exit:  这笔是不是**离场**动作 —— 决定 broker 追不追价，见模块 docstring。
          · False（默认）：交易信号触发的入场，单次超价、不追价
          · True         ：L1-L3 止盈止损触发的离场，失败必须追价
        """
        raise NotImplementedError

    @staticmethod
    def _resolve_intent(intent, side: Side) -> OrderIntent:
        """旧 action="open"|"close" 字符串 → OrderIntent 兼容层。

        新代码一律传 OrderIntent；旧代码（外部脚本/老测试）传字符串也照常工作：
          - "open"  → OrderIntent.OPEN
          - "close" → OrderIntent.CLOSE
          - None    → 默认按 OPEN（防御性兜底，理论上不应发生）
          - 已是 OrderIntent → 原样返回
        """
        if intent is None:
            return OrderIntent.OPEN
        if isinstance(intent, OrderIntent):
            return intent
        s = str(intent).strip().lower()
        if s == "open":
            return OrderIntent.OPEN
        if s in ("close", "close_hard", "hard_exit"):
            return OrderIntent.CLOSE
        raise ValueError("未知的 broker.submit intent/action: {!r}".format(intent))

    def pulse(self, window: Optional[float] = None) -> None:
        """心跳（可选实现）。bar 级保活由交易引擎每根 K 线调一次（on_bar）；
        帧级空闲泵（pump_broker）传 window=0 做非阻塞排空。

        window=None（默认）→ 由实现通道自选窗口（SimNow 用 keepalive_wait=0.2s，
        满足 CTP"用户不活跃"保活约束）；window=0 → wait_update(deadline=now)，
        仍会先处理已到达的回报包、只是不再等新包（tqsdk api.py「先 _fetch_msg
        再判断 deadline」+ baseApi._run_until_task_done 先 _run_once 再判超时）。
        离线通道（dry_run）无需实现，继承本默认空实现。
        """

    def real_position(self, side: "Side") -> Optional[int]:
        """真实持仓查询（可选实现）。引擎持仓对账（增强 B）用。

        返回该方向当前真实持仓手数；不支持/未知返回 None（引擎跳过对账）。
        默认实现返回 None（如 dry_run 离线通道，没有真实账户可查）。
        """
        return None

    def trade_confirmed(self, intent: "OrderIntent", signal_key: str = "") -> bool:
        """真实成交是否到位（报单卡单复核）。

        基于**真实成交明细**（``order.trade_records``）二次判定成交是否到位：
        tqsdk 持仓缓存是**乐观**的，CTP 拒单也不回滚 —— 已两次因此误判
        （见用户级记忆）。诊断/工具用途（引擎主链路不再复核）。

        默认 True（基类兜底：撮合同步的 broker 直接信 submit 返回）。
        dry_run 重写显式 True；SimNow 重写查 tqsdk order.trade_records 累计成交 ≥ volume。
        """
        return True

    def cancel_pending(self, signal_key: str = "") -> int:
        """撤掉该 signal_key 的在途委托，返回撤单请求数。

        撤掉该 signal_key 的在途委托（工具用途；引擎主链路不再自动撤单）。
        默认 0（基类兜底：dry_run 同步撮合无在途单，继承即可）。
        SimNow 重写按 signal_key → raw_order_id 索引逐笔撤单。
        """
        return 0

    # ════════════════════════════════════════════════════════════════
    # 报单序号（R1，2026-09-10）：跨重启的 order_id 唯一性
    #   问题：order_id = "{broker}-{seq:06d}"，seq 是**进程内计数器**（原
    #     itertools.count(1)）→ 进程重启即归零 → 与 state.db 里上一进程写下的
    #     order_id 相撞。orders 表主键冲突 + save_order 的 INSERT OR REPLACE
    #     = 上一进程的委托审计记录被**静默覆盖**。
    #   修法：把序号抬到库内 max 之上（引擎 `_restore` 调 `seed_order_seq`），
    #     并把当前值持久化（`order_seq`），两路取大 —— 与 `trade_seq` 同构。
    #   序号只增不减：`seed_order_seq` 用 max，绝不下调。
    # ════════════════════════════════════════════════════════════════
    def order_seq(self) -> int:
        """当前报单序号（引擎 `_persist` 用它落 kv）。"""
        return int(getattr(self, "_order_seq", 0) or 0)

    def seed_order_seq(self, n: int) -> None:
        """把报单序号抬升到 >= n。引擎 `_restore` 从 state.db 自愈后调用。"""
        self._order_seq = max(self.order_seq(), int(n or 0))

    def _next_order_id(self) -> str:
        """下一个审计用委托号：``"{broker}-{序号:06d}"``。

        序号单调递增（不复用、不依赖墙钟），跨重启由 `seed_order_seq` 抬升。
        """
        self._order_seq = self.order_seq() + 1
        return "{}-{:06d}".format(self.name, self._order_seq)

    def close(self) -> None:
        pass

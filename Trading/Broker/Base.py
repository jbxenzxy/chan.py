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

订单意图（2026-09-11 重构）
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
from typing import Any, Dict, Optional, Type

from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.Types import Order, OrderIntent, Side

BROKERS: Dict[str, Type["Broker"]] = {}


def register_broker(cls: Type["Broker"]) -> Type["Broker"]:
    BROKERS[cls.name] = cls
    return cls


def build_broker(name: str, spec: InstrumentSpec,
                 params: Optional[Dict[str, Any]] = None) -> "Broker":
    if name not in BROKERS:
        raise KeyError("未注册的 broker: {}（已注册: {}）".format(name, list(BROKERS)))
    return BROKERS[name](spec, params or {})


# intent → CTP OpenCloseType 的权威表
# 只有两项（需求 ⑵）：开仓 → OPEN，平仓 → CLOSE。**本表不参与方向决策** ——
# 买还是卖由调用方给的 `side` 决定。
#
# tqsdk 白名单硬校验（api.py:1353 / lib/utils.py:39 / scenario/tqscenario.py:445）：
#   offset ∈ ("OPEN", "CLOSE", "CLOSETODAY")，其它值**直接 raise**（不是 CTP 拒单，
#   是 SDK 本地抛错，被 except 吞掉后表现为"下单失败"，极易误判成通道问题）。
#   2026-09-10 修正（P0）：原 UNLOCK 的值 "CLOSEYESTERDAY" 不在白名单 →
#   实盘 insert_order 本地抛异常 → 跨日解锁 100% 失败。
#   tqsdk 文档口径：上期所/上期能源平昨用 "CLOSE"，**其他交易所（含中金所）平仓直接用 "CLOSE"**。
#
# A4（二期预留）：保持 dict 形态，便于二期加入 "CLOSETODAY"（仅上期所/能源中心可用）。
#   一期**不要加** —— 本系统的 CLOSE 恒作用于跨日仓（断言在 Engine._pre_trade_check），
#   加了只是给"今日单走平今"开出口子，而中金所平今费率是平昨的 15 倍。
INTENT_TO_OFFSET: Dict[OrderIntent, str] = {
    OrderIntent.OPEN: "OPEN",      # 买开 / 卖开
    OrderIntent.CLOSE: "CLOSE",    # 买平 / 卖平；中金所下恒为平昨（无平今分支）
}


# ─── 拒单原因分类（D10，2026-09-11）────────────────────────────────────
# 追价是有代价的（滑点 + 报撤单额度 + 中金所"频繁报撤单"监管计数，见风险 R13）。
# 但并不是所有拒单都值得追：
#   · 盘口深度不够（FOK 全撤）→ 价格会动，追了有用
#   · 资金不足 / 非交易时段   → 追 100 次也不可能成交，纯亏
# 分类器把 CTP 的 last_msg 归到三类，调用方据此决定要不要继续追。
REJECT_FUNDS = "funds"              # 资金不足 → 立即停追，需人工加保证金
REJECT_NOT_TRADABLE = "not_tradable"  # 非交易时段 / 集合竞价 / 无权限 → 立即停追
REJECT_PRICE = "price"              # 价格不可达（FOK 全撤 / 涨跌停）→ 继续追

# 追价无用的两类：命中即 break
NO_CHASE_REJECT_CLASSES = (REJECT_FUNDS, REJECT_NOT_TRADABLE)

# CTP 错误码（2026-09-11 核实，来源：CTP_API 错误代码大全 + 申银万国官方报错释义）
_CTP_CODE_FUNDS = ("31",)                       # 资金不足
_CTP_CODE_NOT_TRADABLE = ("17", "28", "30", "50", "51")
#   17 合约不能交易 / 28 无报单权限 / 30 平仓量超持仓 / 50 平今仓位不足 / 51 平昨仓位不足

# 非交易时段**没有稳定的数字码**，各期货公司文本还不一样 → 只能关键字兜底。
_CTP_KW_NOT_TRADABLE = ("非交易", "不在交易时间", "禁止此操作", "不在报单时间",
                        "未开盘", "已收盘", "交易时间段", "当前状态不允许")
_CTP_KW_FUNDS = ("资金不足", "保证金不足", "可用资金不足")


def classify_ctp_reject(last_msg: str) -> str:
    """把 CTP 的 `last_msg` 归到 `funds` / `not_tradable` / `price` 三类。

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
    for tok in _CTP_CODE_NOT_TRADABLE:
        if _code_hit(msg, tok):
            return REJECT_NOT_TRADABLE
    for kw in _CTP_KW_FUNDS:
        if kw in msg:
            return REJECT_FUNDS
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

    def __init__(self, spec: InstrumentSpec, params: Optional[Dict[str, Any]] = None):
        self.spec = spec
        self.params: Dict[str, Any] = dict(params or {})
        # R1：报单序号（跨重启唯一性）。见 order_seq / seed_order_seq 注释。
        self._order_seq: int = 0

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

    def pulse(self) -> None:
        """心跳（可选实现）。引擎每处理一根 K 线调一次。

        真实 CTP 通道（如 SimNow）需要在长连接空闲期定期收发数据，否则会被
        判为"用户不活跃"而断连。离线通道（dry_run）无需实现。
        """

    def real_position(self, side: "Side") -> Optional[int]:
        """真实持仓查询（可选实现）。引擎持仓对账（增强 B）用。

        返回该方向当前真实持仓手数；不支持/未知返回 None（引擎跳过对账）。
        默认实现返回 None（如 dry_run 离线通道，没有真实账户可查）。
        """
        return None

    def trade_confirmed(self, intent: "OrderIntent", signal_key: str = "") -> bool:
        """真实成交是否到位（报单卡单复核）。

        用于引擎在 submit 返回未成交后隔若干根 K 线复核：CTP broker 可能在
        submit 返回 filled 后实际并未成交（tqsdk 持仓缓存是**乐观**的，
        CTP 拒单也不回滚 —— 已两次因此误判，见用户级记忆）。这里要求 broker
        给出基于真实成交明细（``order.trade_records``）的二次判定。

        默认 True（基类兜底：撮合同步的 broker 直接信 submit 返回）。
        dry_run 重写显式 True；SimNow 重写查 tqsdk order.trade_records 累计成交 ≥ volume。
        """
        return True

    def cancel_pending(self, signal_key: str = "") -> int:
        """撤掉该 signal_key 的在途委托，返回撤单请求数。

        引擎在卡单复核未确认时调用：先撤在途单再按真实持仓修正，
        防止「重建 portfolio 后挂单又成交」的双重平仓。
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

# -*- coding: utf-8 -*-
"""
SimNow 仿真 broker（M2b）
=========================
把 M2a 探针验证过的连接逻辑包进 Broker 接口，接入 M1 引擎。
引擎 / 策略 / 风控 / 回放 全部复用，只换执行通道：`--broker simnow`。

与 dry_run 的唯一差异在撮合：这里发**真实 SimNow 委托**，成交价由仿真撮合
决定（order.trade_price），不做 dry_run 那种"滑点让价"的模拟成交。

关键设计
    - tqsdk **懒加载**：只有真正用 simnow broker 才 import tqsdk，
      保证 dry_run 模式仍零依赖、能离线跑。
    - 主连自动映射：signal_symbol 若为 "KQ.m@..." 主连，用
      quote.underlying_symbol 动态解析主力合约，替代手工写死 trade_symbol。
      解析失败才回退到 config 里的 trade_symbol。
    - 限价单超价（M4）：SimNow 不支持市价单，下单瞬间取实时对手价（买=ask/卖=bid）
      ± overprice_ticks×tick（默认 5 tick；IF=1.0 点，朝成交方向取整到 tick）主动跨价差成交；
      取不到行情则回退到基于信号价的 align_*。
    - 行情新鲜度守卫（2026-09-07 对账加固）：tqsdk 3.10.2 **没有**公开连接状态接口
      （is_connecting 不存在；内部重连 handler 是 _init_connection 局部变量不可达），
      断连→自动重连窗口内 wait_update 正常返回不抛异常，get_position 缓存陈旧——
      曾致换日对账把真实存在的 2 手多单误清（reconcile_real_zero）。故 real_position
      读仓前先校验行情快照新鲜度：IF 交易时段每 0.5s 一个 tick，quote.datetime 停滞
      超过 BrokerConfig.channel.quote_stale_seconds（默认 30s）判数据陈旧 → 返回 None，引擎对账跳过该侧。
      覆盖"断连重连中"与"TCP 假死"两类场景，且不依赖 tqsdk 版本。
    - 报单属性 advanced 取自**品种执行策略表第 2 列**
      （`Instrument.effective_order_advanced()`）—— "FOK" 限价立即全部成交否则
      全部撤销，由交易所撮合引擎强制执行，杜绝部分成交幽灵残留；"FAK" 部分成交
      后撤余量（该品种一笔恒挂 1 手时 FAK ≡ FOK）。
      · **入场**（交易信号触发）：全撤 → 本笔作废（rejected），不追价，等下一信号。
        "入场没成功，最多不赚钱，但不会亏钱。"
      · **离场**（L1-L3 止盈止损触发）：全撤 → 终态回报到手立即按最新对手价重新
        超价报单（不 sleep 等待），最多 chase_max_number 轮（默认 3）；
        轮数用尽仍不成交 → severe 告警转人工处理。
        ⚠️ 但"资金不足 / 非交易时段"两类拒单会**立即停追**（D10 分类器）——
        追 100 轮也不可能成交，只会空耗报撤单额度与中金所监管计数（风险 R13）。
      · fill_timeout_open/close 退化为通道异常兜底 watchdog：正常时交易所毫秒级
        给出终态，超时撤单分支仅在断线/回报丢失时兜底。
      ⚠️ 报单属性是**品种属性**（FOK / FAK），由品种执行策略表按品种给定；
      Broker 只调 `Instrument.effective_order_advanced()` 取生效值，
      不判交易所、不判品种 —— 别在这里写 `if exchange == ...`。
    - offset：OPEN→OPEN；CLOSE→CLOSE（方向由调用方给的 side 决定）。
      删除按持仓当日判今/昨仓选 offset 的逻辑（原 `_close_offset`）。
      规则 ⑸ 保证"今日单离场 = LOCK 反向开仓（offset=OPEN）"、"跨日单离场 = CLOSE
      平昨（offset=CLOSE）"，故 CLOSE 恒为平昨，CLOSETODAY（平今 0.0345%）不可达。

并发与一致性保障（P0/P3/P4/P5/P6）
    P0：close 前等 tqsdk position 同步到 ≥ volume，防 CTP "平仓量超过持仓量"拒单。
    P3：必须 status=="FINISHED" 且 volume_left==0 才算成交，挡"trade_price 已写但实为
        撤单/超时"的假成交。
    P4：open/close 后再验证 tqsdk position 端 delta，order 端 + position 端双重校验。
    P5：gateway 启动时主动等 5 秒让 CTP 推完所有"未确认回报"，建立启动账户基线；
        并把 P4 的 `>=`/`<=` 模糊匹配改为 `abs(cur-target)<=1` 精确容差匹配，
        挡"CTP 重连重发上轮成交通知污染 position"的幽灵成交。
    P6（权威层，取代 P4/P5 的判定权）：用 CTP 真实成交明细 `order.trade_records`
        判定成交。tqsdk 的 position 缓存会在 insert_order 后被**乐观**增减排，
        CTP 拒单也不回滚——P4/P5 拿它做判定会两头误判：
          · 误拒：CTP 真成交但 position 缓存滞后 → 判幽灵（v5 的 21 笔 close 死循环）
          · 误放：CTP 拒单但 position 缓存被 +1 → 判成交（v5 的 4 笔幻象 filled，
            1.5 分钟后查真实账户却是 0 持仓）
        所以 P6 之后：真成交 = P3 两层 + trade_records 成交量 ≥ 委托量；
        P4/P5 降级为纯诊断（只告警、不 reject）。

凭据（优先级：环境变量 > config.broker_params，env 为空才回落 config）
    sn_account / sn_password    SimNow 仿真账号
    tq_account / tq_password    天勤账号
    环境变量名：SN_ACCOUNT / SN_PASSWORD / TQ_ACCOUNT / TQ_PASSWORD

用法
    python main.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m \\
        --broker simnow --out ./run_live
"""
from __future__ import annotations

import logging
import math
import os
import time
import datetime as _dt
from typing import Any, Dict, List, Optional, Tuple

from ..Config import BrokerConfig
from ..Infra.Instrument import Instrument
from ..Infra.Records import Order, OrderIntent, Side
from ..Infra.Clock import now_cn
from .Base import (INTENT_TO_OFFSET, NO_CHASE_REJECT_CLASSES, REJECT_POSITION,
                   Broker, classify_ctp_reject, register_broker)

# broker 参数默认值的**单一事实源**：Trading/Config.py 的 BrokerConfig 模型。
# 本文件不再自带任何兜底数值（2026-09-07 严格模式）—— params 由配置模型构造，
# 键必然齐全；取不到说明配置模型漏了字段，属于代码 bug，直接 fail-fast 抛异常。
# （2026-09-05 曾修：旧代码 fill_timeout_open 兜底 10.0，与配置表的 5.0 矛盾。）

_DIRECTION = {Side.LONG: "BUY", Side.SHORT: "SELL"}
# close 类报文（CLOSE/UNLOCK）的方向：平多=SELL、平空=BUY（与 _DIRECTION 相反）。
# 修复：旧 _submit_close 直接用 _DIRECTION[side]，平多发 BUY —— CTP 会拒单
# 或平错方向；此前 dry_run 撮合不校验 direction 字符串，故回归未暴露。
_CLOSE_DIRECTION = {Side.LONG: "SELL", Side.SHORT: "BUY"}

# 行情新鲜度阈值（秒）：quote.datetime 停滞超过该值判数据陈旧（2026-09-07 对账加固）。
# IF 交易时段每 0.5s 一个 tick，30s 足够宽容；断连/重连中/TCP 假死时行情停滞，
# 此时 get_position 缓存必然不可信。刻意不走 _param（config 单一事实源）——
# 这是通道级安全阈值而非策略参数，避免用户 config 漏键导致 fail-fast 起不来。
#
# ⚠️ 夜盘/非交易时段限制（**本判据本身仍待改**，见下方更新）：
#   非交易时段行情停滞是正常现象，本判据会把"数据陈旧"误判为常态 →
#   real_position 恒返回 None。日盘 IF 无碍（引擎对账只由 bar 事件驱动，
#   交易时段外没有 bar，对账根本不触发）；但夜盘品种（如 au/ag 21:00-02:30、
#   螺纹 21:00-23:00）盘中存在"合约无 tick 的静默段"。
#
#   更新（两件事必须分开看）：
#     ① 【已解决】"21:00-次日 02:30 跨越本地日期变更，quote.datetime 的交易日
#        语义也随之变化" —— 交易日口径已收口到 Infra/Types.trading_day_of_ms()
#        （夜盘成交归属**次一交易日**），建仓端 entry_date 与离场判定端 today
#        共用它，不再各自解析自然日字符串。
#     ② 【仍未解决】本判据把"绝对时钟差"当陈旧依据，夜盘静默段会被恒判陈旧 →
#        real_position 恒 None → 夜盘对账被静默跳过。届时需改为"按合约交易时段表
#        判断是否处于应报价区间"（参考 Infra/Instrument.py 扩展交易时段元数据）。
# ── 微轮询节奏（Step 2.3，拍板 C1：文件级命名常量，不进配置面板）────────
#   行情陈旧阈值已收口到 BrokerConfig.channel.quote_stale_seconds（_timing 读取）；
#   0.1/0.2 的纯轮询节奏无实际调参价值，只消灭字面量、收口为命名常量。
_POLL_INTERVAL_FAST = 0.1   # _verify_position_delta / _wait_position_ok 轮询
_POLL_INTERVAL_SLOW = 0.2   # _wait 通用谓词轮询
# 帧级空闲泵的积压告警阈值：连续多少帧 `_recv_chan` 非空才打一条 WARNING。
# 心跳帧率 ≈ 10/s（AppSSE 100ms 窗口）→ 50 帧 ≈ 5s：日常回报率远达不到，
# 只有回报洪峰真正追上帧率时才会命中（取一个远大于抖动、盘中又能及时暴露的值）。
_PUMP_BACKLOG_WARN_STREAK = 50


def _position_split(api, trade_symbol: str, side: str) -> Optional[Tuple[int, int]]:
    """读 tqsdk 持仓的 **(今仓, 昨仓)** 分解，失败返回 None。

    为什么需要分解（D12 第 4 项，2026-09-13）：
      `CLOSE` 在本系统里**恒作用于跨日仓**（不变量 6，断言在
      `Engine._pre_trade_check`）→ 中金所下恒为**平昨**。而中金所撮合规则是
      **同时有今仓和昨仓时默认先平今**，所以"总量够"并不等于"平昨能成"：
      今仓 2 手 + 昨仓 0 手时，总量判据会放行，CTP 却会用今仓去平（平今费率
      是平昨的 15 倍）或直接拒单。要挡住这件事，必须能分别看到今 / 昨。
    取不到（api 异常）返回 None，调用方按"不可信"处理。
    """
    try:
        pos = api.get_position()
    except Exception:
        return None
    item = None
    if isinstance(pos, dict):
        # 只认 trade_symbol 精确匹配；找不到 = 该合约当前无持仓（返回 0）。
        # 收窄：删除旧的"取第一条多/空非零持仓"兜底——账户同时持有
        # 其他品种时会把别的合约误当本合约读（跨品种误判，污染 P4/P5 校验）。
        # 陈旧缓存 dict 缺键的场景由 real_position 的新鲜度守卫前置拦截。
        item = pos.get(trade_symbol)
    else:
        item = pos
    if item is None:
        return (0, 0)
    if side == "LONG":
        return (int(getattr(item, "pos_long_today", 0) or 0),
                int(getattr(item, "pos_long_his", 0) or 0))
    return (int(getattr(item, "pos_short_today", 0) or 0),
            int(getattr(item, "pos_short_his", 0) or 0))


def _position_total(api, trade_symbol: str, side: str) -> int:
    """读 tqsdk 当前持仓总数（今+昨），失败返回 -1。

    用于 P4 修复的成交后二次校验。注意：传 symbol 也不传时，tqsdk 返回的是
    整个账户的 dict[symbol, Position]；这里取与 trade_symbol 匹配的那一条。
    改为 `_position_split` 求和，避免"今/昨字段名"在两处各写一遍。
    """
    sp = _position_split(api, trade_symbol, side)
    if sp is None:
        return -1
    return sp[0] + sp[1]


def _verify_yesterday_delta(api, trade_symbol: str, side: str,
                            today_baseline: int, his_baseline: int,
                            volume: int, timeout_s: float = 5.0) -> bool:
    """等 **昨仓** 精确减少 `volume`、且 **今仓一分不动**（D12 / p38 不变量的成交后半段）。

    与 `_verify_position_delta` 的分工：
      · 那个看的是**总量**，服务 OPEN（今仓增加，总量增加）；
      · 这个看的是**今/昨分解**，只服务 CLOSE（平昨 → 昨仓减、今仓不动）。
    判据（第 4 项"平昨 CLOSE 是否真的减昨仓"）：
      成立 ⟺ 昨仓落到 `his_baseline - volume`（±1 帧同步漂移）
              且 今仓 == `today_baseline`（**下降即视为平到了今仓 → 不成立**）

    "今仓下降"必须判失败而不是容忍：那说明柜台把 CLOSE 撮合到了今仓，
    触发的是平今费率（中金所 0.0345% ≈ 平昨 15 倍），属于必须有人知道的
    异常，不能静默通过。

    返回 True/False；调用方只用于**诊断告警**（成交权威判据仍是 P6 `trade_records`），
    不据此推翻已成交事实 —— 与 P4/P5 降级后的口径一致。
    """
    if today_baseline < 0 or his_baseline < 0:
        return False
    his_target = his_baseline - int(volume)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        api.wait_update(deadline=deadline)
        sp = _position_split(api, trade_symbol, side)
        if sp is None:
            continue
        today_cur, his_cur = sp
        if today_cur < today_baseline:
            return False                    # 今仓被平掉了 → 平到了今仓，不成立
        if today_cur == today_baseline and abs(his_cur - his_target) <= 1:
            return True
        time.sleep(_POLL_INTERVAL_FAST)
    return False


def _verify_today_delta(api, trade_symbol: str, side: str,
                        today_baseline: int, his_baseline: int,
                        volume: int, timeout_s: float = 5.0) -> bool:
    """等 **今仓** 精确减少 `volume`、且 **昨仓一分不动**（p51 的成交后半段）。

    `_verify_yesterday_delta` 的镜像：那个服务 CLOSE（平昨），这个只服务
    **CLOSETODAY（平今）**。判据：
      成立 ⟺ 今仓落到 `today_baseline - volume`（±1 帧同步漂移）
              且 昨仓 == `his_baseline`（**下降即视为平到了昨仓 → 不成立**）

    "昨仓下降"必须判失败而不是容忍：平今的 CLOSETODAY 报文若被柜台撮合到昨仓，
    账实就会错位（引擎簿按今仓记账），属于必须有人知道的异常，不能静默通过。

    返回 True/False；调用方只用于**诊断告警**（成交权威判据仍是 P6 `trade_records`），
    不据此推翻已成交事实 —— 与 P4/P5 降级后的口径一致。
    """
    if today_baseline < 0 or his_baseline < 0:
        return False
    today_target = today_baseline - int(volume)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        api.wait_update(deadline=deadline)
        sp = _position_split(api, trade_symbol, side)
        if sp is None:
            continue
        today_cur, his_cur = sp
        if his_cur < his_baseline:
            return False                    # 昨仓被平掉了 → 平到了昨仓，不成立
        if his_cur == his_baseline and abs(today_cur - today_target) <= 1:
            return True
        time.sleep(_POLL_INTERVAL_FAST)
    return False


def _verify_position_delta(api, trade_symbol: str, side: str,
                            baseline: int, expected_delta: int,
                            timeout_s: float = 5.0) -> bool:
    """等 tqsdk 持仓从 baseline 出发、按 expected_delta 精确变化。

    P4 修复 + P5 收紧：
      - P4 旧版用 `cur >= target` / `cur <= target` 的模糊匹配，挡不住"CTP 重连重发的
        上轮成交通知污染 position"——比如 baseline=0、target=1，但 CTP 重发让 position
        跳到 3，`>=` 仍判通过，反而把幽灵成交当真。
      - P5 改为精确容差匹配 `abs(cur - target) <= 1`：要求 position 从 baseline 出发、
        按 expected_delta 变化到 target（或 ±1 帧漂移），既挡幽灵又兼容 CTP 同步慢一帧。
      - 调用方必须在 insert_order 之前用 _position_total 读一次"下单前快照"作为 baseline，
        这里只负责监控后续变化，避免"等同步期间又被改"的串扰。
    """
    if baseline < 0:
        return False
    target = baseline + expected_delta
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        api.wait_update(deadline=deadline)
        cur = _position_total(api, trade_symbol, side)
        if cur < 0:
            continue
        # 精确容差匹配：cur 应在 [target-1, target+1] 区间内
        if abs(cur - target) <= 1:
            return True
        time.sleep(_POLL_INTERVAL_FAST)
    return False


def _traded_volume_from_records(order) -> int:
    """P6 权威成交判定：从 tqsdk 的 CTP 真实成交明细里累计成交量。

    tqsdk Order 对象的 ``trade_records`` 字段是交易所（CTP）**真正确认**的成交回报
    明细——只有撮合成功才会写入，不会被本地缓存的乐观更新污染。

    结构（tqsdk 3.x）::

        order["trade_records"] = {
            "<trade_id>": {"trade_id": "...", "volume": 1, "price": 4547.4, ...},
            ...
        }

    返回累计成交量；读不到或异常一律返回 0（保守：宁可漏判成交，也不误判成交）。
    """
    recs = getattr(order, "trade_records", None)
    if not recs:
        return 0
    total = 0
    try:
        items = recs.values() if isinstance(recs, dict) else list(recs)
        for r in items:
            if r is None:
                continue
            vol = r.get("volume", 0) if isinstance(r, dict) \
                else getattr(r, "volume", 0)
            if vol:
                total += int(vol)
    except Exception:
        return 0
    return total


def _traded_price_from_records(order) -> Optional[float]:
    """P6：从 CTP 真实成交明细里取成交均价（按 volume 加权）。

    返回 None 表示没有任何真实成交明细。
    """
    recs = getattr(order, "trade_records", None)
    if not recs:
        return None
    total_vol = 0
    total_amt = 0.0
    try:
        items = recs.values() if isinstance(recs, dict) else list(recs)
        for r in items:
            if r is None:
                continue
            vol = r.get("volume", 0) if isinstance(r, dict) \
                else getattr(r, "volume", 0)
            prc = r.get("price", 0) if isinstance(r, dict) \
                else getattr(r, "price", 0)
            try:
                vol = int(vol)
                prc = float(prc)
            except (TypeError, ValueError):
                continue
            if vol <= 0 or prc <= 0 or math.isnan(prc):
                continue
            total_vol += vol
            total_amt += prc * vol
    except Exception:
        return None
    if total_vol <= 0:
        return None
    return total_amt / total_vol


def _extract_last_trade_date(quote: Any) -> str:
    """从行情对象提取最后交易日（交割月护栏数据位，YYYY-MM-DD）。

    优先读 quote.last_trade_date；tqsdk 部分版本只暴露 expiry_datetime
    （datetime 对象 / int 毫秒三种形态），取其日期部分。解析不出 → 返回 ""。
    """
    def _normalize(v: Any) -> str:
        if isinstance(v, _dt.datetime):
            return v.strftime("%Y-%m-%d")
        if isinstance(v, _dt.date):
            return v.strftime("%Y-%m-%d")
        if isinstance(v, (int, float)) and v > 0:
            try:
                return _dt.datetime.fromtimestamp(v / 1000.0).strftime("%Y-%m-%d")
            except (ValueError, OverflowError, OSError):
                return ""
        s = str(v or "").strip()
        if len(s) >= 10:
            s = s[:10]
        try:
            _dt.date.fromisoformat(s)
            return s
        except (ValueError, TypeError):
            return ""

    if quote is None:
        return ""
    for attr in ("last_trade_date", "expiry_datetime"):
        try:
            got = getattr(quote, attr, None)
        except Exception:
            continue
        if got is None:
            continue
        s = _normalize(got)
        if s:
            return s
    return ""


@register_broker
class SimNowBroker(Broker):
    name = "simnow"

    def __init__(self, instrument: "Instrument",
                 params: Optional[Dict[str, Any]] = None,
                 state: Optional["Instrument"] = None):
        super().__init__(instrument, params, state=state)
        # 严格模式：broker_params 以 Trading/Config.py 的
        # BrokerConfig 为**唯一默认值来源**补齐 —— 调用方可以只传要覆盖的键；
        # 传了模型里没有的键（拼错 / 残留旧键）直接报错，不再静默忽略。
        self.params = BrokerConfig(**(params or {})).model_dump()
        self._api = None
        # 行情快照引用（_connect 成功后订阅），供 _quote_stale 新鲜度守卫读 datetime
        self._quote = None
        self._trade_symbol = instrument.trade_symbol
        # R1：报单序号由 Base 的自增整数提供（原 itertools.count(1)
        #   是进程内计数器，重启归零 → order_id 与上一进程相撞 → orders 表
        #   INSERT OR REPLACE 把上一进程的委托审计记录静默覆盖）。
        #   SimNow 的 order_id 只是**审计用合成号**，真实委托号在 meta["raw_order_id"]，
        #   故这里只需保证跨重启不重复即可。
        self.orders: List[Order] = []
        # signal_key → [raw_order_id] 索引（trade_confirmed / cancel_pending
        # 复查用）。必须在凭据检查**之前**初始化 —— 缺凭据 early-return 时也要保证
        # 字段存在，否则单测实例化（无凭据）后访问会 AttributeError。
        self._sig_orders: Dict[str, List[str]] = {}
        self._conn_error: Optional[str] = None
        # P61：回报链路时延测量锚点 —— 本地报单时刻 / watchdog 撤单时刻（墙钟），
        # _finalize 终判时据此落 otg_latency 日志（见 _otg_latency_fields）。
        self._submit_t0: float = 0.0
        self._watchdog_fired_at: Optional[float] = None
        # 帧级空闲泵的积压观测计数（见 _observe_pump_backlog）：连续非空帧数。
        # 与 _sig_orders 同理，必须在凭据检查**之前**初始化 —— 缺凭据 early-return
        # 的实例也要保证字段存在，否则单测实例化后调 pulse 会 AttributeError。
        self._pump_backlog_streak: int = 0
        # （A′ 2026-09-17 改造）：合约参数 SSOT = 品种档案 Product（构造期播种），
        #   无行情取值路径 —— 原"取值即冻结"开关与 pulse 重试计数器随行情
        #   取值通道一并删除；verified 由 _connect 成功即置位（source=CONFIG）。
        # 删除 _fee_samples 样本缓存（费率反推通道随移除）。

        # ════════════════════════════════════════════════════════════════
        # SimNow 仿真 ↔ 实盘 CTP 账户选择
        #   账户路由（优先级：环境变量 > 配置文件，env 为空才回落 config）：
        #     仿真：sn_account/sn_password（SN_ACCOUNT/SN_PASSWORD）
        #     实盘：live_account/live_password（LIVE_ACCOUNT/LIVE_PASSWORD）
        #   天勤账号 tq_account/tq_password（TQ_ACCOUNT/TQ_PASSWORD）两种模式共用。
        #   market 判定：broker=live（LiveCTPBroker）或 tq_market≠"simnow" → 实盘。
        #   安全闸门：实盘必须显式 confirm_live_trading=true，否则拒绝启动
        #   （broker=live 但 tq_market 仍为 simnow 属于配置矛盾，同样拒绝）。
        # ════════════════════════════════════════════════════════════════
        self.tq_market = self._cred("tq_market", "TQ_MARKET") or "simnow"
        self.confirm_live = bool(self._param("confirm_live_trading"))
        self.live_account = self._cred("live_account", "LIVE_ACCOUNT")
        self.live_password = self._cred("live_password", "LIVE_PASSWORD")
        self.is_live = (self.name == "live"
                        or str(self.tq_market).strip().lower() != "simnow")

        if self.is_live:
            if str(self.tq_market).strip().lower() == "simnow":
                # broker=live 但 tq_market 仍是默认 simnow —— 配置矛盾，fail-fast
                self._conn_error = (
                    "实盘安全闸门：broker='{}' 但 broker_params.tq_market 仍为 "
                    "'simnow'，实盘请填期货公司名（如 '创元期货'）".format(self.name))
                return
            if not self.confirm_live:
                self._conn_error = (
                    "实盘安全闸门未开启：tq_market='{}' 非仿真市场，必须显式设置 "
                    "broker_params.confirm_live_trading=true 才能启动实盘".format(
                        self.tq_market))
                return

        # 凭据：环境变量优先，env 为空才回落 config（防明文反客为主）
        self.sn_account = self._cred("sn_account", "SN_ACCOUNT")
        self.sn_password = self._cred("sn_password", "SN_PASSWORD")
        self.tq_account = self._cred("tq_account", "TQ_ACCOUNT")
        self.tq_password = self._cred("tq_password", "TQ_PASSWORD")

        if self.is_live:
            missing = [k for k, v in
                       (("LIVE_ACCOUNT", self.live_account),
                        ("LIVE_PASSWORD", self.live_password),
                        ("TQ_ACCOUNT", self.tq_account),
                        ("TQ_PASSWORD", self.tq_password))
                       if not v]
            missing_label = "实盘/天勤"
        else:
            missing = [k for k, v in
                       (("SN_ACCOUNT", self.sn_account), ("SN_PASSWORD", self.sn_password),
                        ("TQ_ACCOUNT", self.tq_account), ("TQ_PASSWORD", self.tq_password))
                       if not v]
            missing_label = "SimNow/天勤"
        if missing:
            self._conn_error = "缺少 {} 凭据: {}".format(missing_label, ", ".join(missing))
            return

        # ===== P5 修复：启动账户基线 =====
        # 历史 bug：gateway 启动瞬间，tqsdk 与 CTP 重建连接，CTP 会**重发**上一轮
        # SSE 实时成交的"成交通知"（包括 order_id 不在本轮的回报）。这些回报直接
        # 修改 tqsdk 账户 position（+1 手多仓），但不经过 simnow.py 的 submit 路径，
        # P0/P3/P4 都看不见。
        #
        # P5 在 _connect 后强制等 5 秒，让 CTP 把所有"未确认回报"全推过来，建立
        # 显式的"启动时账户基线" _initial_account_state；如果非 0，立刻在日志里
        # 警告（提醒用户这是历史遗留，不是本轮信号造成的）。
        self._initial_account_state: Dict[str, int] = {}
        self._connect()
        if self._api is not None:
            self._capture_initial_account_state()

    def _cred(self, param_key: str, env_key: str) -> str:
        # 凭据只走环境变量。配置里不再有账号密码字段（若将来
        # 残留明文密码，会反客为主覆盖开发者想用 LIVE_PASSWORD 等环境变量注入
        # 的凭据（与"密码不落盘"的意图相反）。env 有值用 env；env 为空才回落
        # 又在配置里加回明文密码，会反客为主覆盖环境变量，与"密码不落盘"相悖）。
        v = (os.environ.get(env_key) or self.params.get(param_key) or "").strip()
        return v

    def _param(self, key: str) -> Any:
        """读 broker 参数：只从配置模型给全的 params 里取（严格模式，无兜底）。

        params 由 Trading/Config.py 的 BrokerConfig 构造，键必然齐全；
        取不到说明配置模型漏了字段 —— 属于代码 bug，直接抛异常暴露，
        绝不带着"看起来合理"的默认值悄悄跑。
        """
        v = self.params.get(key)
        if v is not None:
            return v
        raise KeyError(
            "broker 参数 '{}' 未在 BrokerConfig（Trading/Config.py）定义，"
            "或构造 broker 时传入的 broker_params 不完整".format(key))

    def _timing(self, key: str) -> Any:
        """读通道时序参数（Step 2.3 归一）：只从 BrokerConfig.channel 里取（严格模式，无兜底）。

        与 _param 同一纪律：params["channel"] 由 ChannelTimingConfig 构造，键必然齐全；
        取不到说明配置模型漏了字段 —— 属于代码 bug，直接抛异常暴露。
        """
        v = self.params.get("channel", {}).get(key)
        if v is not None:
            return v
        raise KeyError(
            "通道时序参数 '{}' 未在 ChannelTimingConfig（Trading/Config.py）定义，"
            "或构造 broker 时传入的 broker_params.channel 不完整".format(key))

    # ---------------- 连接与合约映射 ----------------
    def _connect(self) -> None:
        """登录 CTP（SimNow 仿真 / 实盘期货公司），带重试。

        账户路由按 self.is_live 选择
          · 仿真 → TqAccount("simnow", sn_account, sn_password)
          · 实盘 → TqAccount(tq_market, live_account, live_password)
            （tq_market = 期货公司名，如 "创元期货"）

        SimNow 对短连接很敏感：上一轮 gateway 跑完立即退出，CTP 侧会把会话标成
        "用户不活跃"（实测 14:59:50 重连时直接报 `CTP:用户不活跃` + TqTimeoutError）。
        所以登录失败时不能立刻放弃，退避重试几次通常就能连上。
        """
        from tqsdk import TqApi, TqAuth, TqAccount

        if self.is_live:
            market = str(self.tq_market).strip()
            account = self.live_account
            password = self.live_password
        else:
            market = "simnow"
            account = self.sn_account
            password = self.sn_password

        max_attempts = int(self._param("connect_retries"))
        backoff = float(self._param("connect_backoff"))
        last_err: Optional[str] = None

        for attempt in range(1, max_attempts + 1):
            try:
                self._api = TqApi(
                    TqAccount(market, account, password),
                    auth=TqAuth(self.tq_account, self.tq_password))
            except Exception as e:
                last_err = "CTP 登录失败({}): {}: {}".format(
                    market, type(e).__name__, e)
                self._api = None
                if attempt < max_attempts:
                    import logging
                    logging.getLogger("tg.brokers.simnow").warning(
                        "CTP({}) 第 %d/%d 次登录失败（%.1fs 后重试）: %s",
                        market, attempt, max_attempts, backoff, last_err)
                    time.sleep(backoff)
                    backoff *= self._timing("connect_backoff_factor")  # 默认 1.5：5s → 7.5s → 11.25s
                continue

            # 登录成功，做一次探活：确认连接真的能收数据（挡"用户不活跃"的僵尸连接）
            if not self._probe_alive():
                last_err = "CTP 连接探活失败（疑似 CTP:用户不活跃）"
                try:
                    self._api.close()
                except Exception:
                    pass
                self._api = None
                if attempt < max_attempts:
                    import logging
                    logging.getLogger("tg.brokers.simnow").warning(
                        "CTP({}) 第 %d/%d 次探活失败（%.1fs 后重试）",
                        market, attempt, max_attempts, backoff)
                    time.sleep(backoff)
                    backoff *= self._timing("connect_backoff_factor")
                continue

            self._resolve_trade_symbol()
            # 预订阅 trade_symbol 行情：行情新鲜度守卫（_quote_stale）依赖该订阅；
            #   交割月护栏数据位（last_trade_date）也从该订阅回填。
            # get_quote 非阻塞（仅发起订阅，数据随 wait_update 推送）；首帧未到前
            # datetime 为空 → _quote_stale 判陈旧，real_position 保守返回 None。
            try:
                self._quote = self._api.get_quote(self._trade_symbol)
            except Exception:
                self._quote = None
            # （A′ 2026-09-17 改造）：合约参数 SSOT = 品种档案（Product），
            #   构造期已播种，**没有任何信息需要从行情获取**。
            #   连接成功即置 verified=True —— A′ 闸门语义从"行情参数已通过校验"
            #   收敛为"在线通道已连通"（source=CONFIG）。
            #   唯一仍依赖行情的是交割月护栏的 last_trade_date（静态元数据）：
            #   等行情首帧到（datetime 非空）后回填；取不到保持 ""，
            #   护栏对未知不校验（回填结果在启动日志可见）。
            if self._quote is not None:
                self._wait(lambda: bool(getattr(self._quote, "datetime", None)),
                           timeout_s=self._timing("underlying_map_timeout"))
                self._fill_delivery_calendar(self._quote)
            self.state.verified = True
            self.state.source = self.state.SOURCE_CONFIG
            # 局部绑定 logging：本函数 except 分支里的 import 使 logging 成为
            # 函数局部名，happy path 必须先 import 再用（否则 UnboundLocalError）。
            import logging
            logging.getLogger("tg.brokers.simnow").info(
                "在线通道已连接 → verified=True（source=CONFIG，合约参数 SSOT=品种档案；"
                "交割月护栏 last_trade_date=%r）", self.state.last_trade_date)
            # 持仓镜像初读（P64 测量）：探活与行情首帧已各泵过数秒，持仓快照若能
            # 同步此时应已到位。读数 0 且快期3 显示有仓 = otg 持仓通道未同步的
            # 直接证据（2026-09-18 14:46 事故：镜像整场为空 → 对账误删刚开仓，
            # 引擎侧证据门见 Reconcile._mirror_note）。读数失败不阻断启动。
            try:
                _long0 = _position_total(self._api, self._trade_symbol, "LONG")
                _short0 = _position_total(self._api, self._trade_symbol, "SHORT")
                logging.getLogger("tg.brokers.simnow").info(
                    "登录后持仓镜像初读: LONG=%s SHORT=%s（-1=读数失败；"
                    "0 且快期3 显示有仓 = otg 持仓通道未同步）", _long0, _short0)
            except Exception:
                pass
            return

        self._conn_error = last_err or "CTP 登录失败（未知原因）"
        self._api = None

    def pulse(self, window: Optional[float] = None) -> None:
        """心跳：推一帧数据，保持连接活跃。

        SimNow 的 CTP 会话在空闲期会被标成"用户不活跃"并断连（实测短连接跑完
        立即退出，隔几分钟重连就报 `CTP:用户不活跃`）。SSE 实时模式两根 K 线之间
        可能隔好几分钟，靠 submit 里的 wait_update 不够，所以交易引擎每根 bar
        调一次。

        window=None（默认）→ keepalive_wait（Config.py，0.2s）保活窗口，bar 级用；
        window=0 → 单轮推进：wait_update(deadline=now) 仍会先处理已到达的
        回报包、只是不再等新包（tqsdk api.py:2106-2108 先 _fetch_msg 再判
        deadline；:2105 为其注释）+ baseApi.py:111 max(0,·) + 先 _run_once 再判超时）。帧级空闲泵
        （Engine.pump_broker）用 0：心跳帧率（≈10/s，AppSSE 100ms 窗口）高于
        0.2s 窗口的消费上限（5/s），沿用保活窗口会让积压反灌 SSE 的 buf。

        注意：tqsdk 的 wait_update **不是线程安全的**，必须由调用方在主线程驱动，
        这里不能起后台线程。
        """
        if self._api is None:
            return
        try:
            wait = self._timing("keepalive_wait") if window is None else window
            self._api.wait_update(deadline=time.time() + wait)
        except Exception:
            # 心跳失败不抛——下一根 bar 会再试，真断连了 submit 会自己报错
            pass
        # 观测挂在 try 之外：本轮 wait_update 即使抛了，也照样记一次积压
        self._observe_pump_backlog()

    def _observe_pump_backlog(self) -> None:
        """帧级空闲泵的积压观测：只读、O(1)、独立 try，绝不改行为、绝不抛。

        为什么需要（P3 可观测性）：`pulse(0)` 每轮只放行 **1 个包** —— 这是 tqsdk
        的固有节流而非本仓库的选择（baseApi.py:96-98：`_run_until_idle(async_run=True)`
        单次只从 `_wait_idle_list` pop 一个 future），而 `_fetch_msg`
        （api.py:4125-4132）只负责把 `_recv_chan` 里的 pack 搬进 `_pending_diffs`。
        单帧处理不完的回报就留在 `_recv_chan` 排队等下一帧，即持续排空上限 = 帧率。
        日常回报率远低于帧率，队列应恒空；**连续多帧非空**才说明回报洪峰已追上
        帧率（此时唯一能连续排空的是下一根 bar 的窗口模式 pulse）。

        指标取 `_recv_chan.qsize()`（`TqChan` 继承 `asyncio.Queue`，qsize 即队列
        len，O(1)，不在热路径做扫描或分配）。全程 getattr 兜底：`_recv_chan` 在
        tqsdk 里初值为 None（api.py:288-289），且会被逐级重指到链路末端
        （api.py:3727/3737）—— 取不到就不观测，不报错、不重试。

        告警按「每攒满阈值一次」节流（打一条后计数归零），不刷屏。
        """
        try:
            chan = getattr(self._api, "_recv_chan", None)
            qsize = getattr(chan, "qsize", None)
            if qsize is None:
                return
            backlog = qsize()
        except Exception:
            # 观测绝不允许反过来影响心跳：宁可没有数据，也不能把保活打断
            return
        if backlog > 0:
            # getattr 兜底：单测用 __new__ 跳过 __init__ 造实例（不连网、不查凭据），
            # 那种路径下本字段不存在 —— 观测代码不允许因此把心跳弄崩。
            streak = getattr(self, "_pump_backlog_streak", 0) + 1
            self._pump_backlog_streak = streak
            if streak >= _PUMP_BACKLOG_WARN_STREAK:
                import logging
                logging.getLogger("tg.brokers.simnow").warning(
                    "[回报泵] 回报包积压：_recv_chan 连续 %d 帧非空（当前积压 %d 包）。"
                    "帧级泵每轮只放行 1 个包（tqsdk 固有节流），持续积压说明回报洪峰"
                    "已追上帧率，靠下一根 bar 的窗口模式兜底排空",
                    streak, backlog)
                self._pump_backlog_streak = 0
        else:
            self._pump_backlog_streak = 0

    def _probe_alive(self, timeout_s: Optional[float] = None) -> bool:
        """探活：拿一次行情/账户数据，确认连接不是"用户不活跃"的僵尸连接。

        CTP 的"用户不活跃"不会抛异常，TqApi 构造也不报错，只有真正 wait_update
        收数据时才暴露（表现为超时或连接被断）。所以登录后必须探一次。
        Step 2.3：timeout_s 缺省时走 BrokerConfig.channel.probe_alive_timeout
        （原硬编码 8.0 收口）；显式传入优先。
        """
        if timeout_s is None:
            timeout_s = self._timing("probe_alive_timeout")
        try:
            self._api.wait_update(deadline=time.time() + timeout_s)
            # 拿账户对象，触发一次真实数据请求
            self._api.get_account()
            self._api.wait_update(deadline=time.time() + timeout_s)
            return True
        except Exception as e:
            import logging
            logging.getLogger("tg.brokers.simnow").warning(
                "SimNow 探活异常: %s: %s", type(e).__name__, e)
            return False

    def _resolve_trade_symbol(self) -> None:
        """主连 → 月份合约映射。映射失败（含超时）**显式报错终止启动**（fail-fast）。

        2026-09-17 改造：InstrumentConfig.trade_symbol 默认值已改为空串 ——
        映射失败时不再有"回退默认月份"的路径（那是静默错单的来源）：
        映射不出主力就无法确定订阅与报单目标，宁可不起飞。
        """
        sig = self.state.signal_symbol
        if not sig.startswith("KQ."):
            # signal_symbol 直接就是月份合约：无需映射，trade_symbol 取配置值。
            if not self._trade_symbol:
                raise RuntimeError(
                    "trade_symbol 为空且 signal_symbol={!r} 非主连 —— 请在配置中"
                    "显式指定 instrument.trade_symbol".format(sig))
            return
        try:
            q = self._api.get_quote(sig)
            hit = self._wait(lambda: bool(getattr(q, "underlying_symbol", None)),
                             timeout_s=self._timing("underlying_map_timeout"))
        except Exception as e:
            raise RuntimeError("主连映射失败: {}: {}".format(type(e).__name__, e))
        if not (hit and getattr(q, "underlying_symbol", None)):
            raise RuntimeError(
                "主连映射超时（{}s）：underlying_symbol 未就绪，无法确定交易合约"
                .format(self._timing("underlying_map_timeout")))
        self._trade_symbol = q.underlying_symbol
        # trade_symbol 是运行时身份字段（行情回填）——
        #   原"就地改写配置对象 spec.trade_symbol"（例 1 写入点①）
        #   现在写的是 Instrument 自身的可变字段，配置（frozen）不再被动。
        self.state.trade_symbol = q.underlying_symbol

    # ══════════════════════════════════════════════════════════════════
    # （阻塞点 4 · D8 插入）：交割月护栏的数据位回填
    # ══════════════════════════════════════════════════════════════════
    def _fill_delivery_calendar(self, quote: Any) -> None:
        """从**真实月份合约**行情回填 `last_trade_date`（交割月护栏数据位）。

        优先读 quote.last_trade_date（YYYY-MM-DD）；tqsdk 部分版本只暴露
        expiry_datetime（datetime / int 毫秒戳），取其日期部分；都取不到或
        解析不出合法日期 → 保持 ""（交割月护栏降级为不校验）。

        一切异常静默跳过 —— 取不到交割数据不能拖垮行情参数流程。
        注：交易时段护栏（night_session）已判定为不需要（纯 K 线推送架构下
        非交易时段无 K 线 → 无信号 → 无报单），故不再回填。
        """
        if self.state.last_trade_date:
            return
        try:
            # last_trade_date 是 Instrument 运行时身份字段
            #   （行情回填）—— 原"就地改写配置对象"（例 1 写入点③）随
            #   双类合并归位：写的是运行时对象自身，frozen 配置不再被动。
            self.state.last_trade_date = _extract_last_trade_date(quote)
        except Exception:
            pass

    def _capture_initial_account_state(self) -> None:
        """P5: 在 _connect 后强制等 5 秒，让 CTP 推送所有未确认回报，建立启动时账户快照。

        如果 _initial_account_state 非 0（即账户在启动时已经有非零持仓），
        说明这是历史遗留仓（上轮 SSE 实时成交留下的），不是本轮 gateway 信号造成的。
        把这个信息写入 _initial_account_state 供后续 submit 做交叉校验用。
        """
        try:
            # 给 CTP 5 秒推完所有未确认回报
            self._api.wait_update(deadline=time.time() + self._timing("recover_settle_wait"))
            self._api.wait_update(deadline=time.time() + self._timing("recover_settle_wait"))
            pos = self._api.get_position()
            items = pos.values() if isinstance(pos, dict) else [pos]
            for v in items:
                if v is None:
                    continue
                sym = getattr(v, "exchange_symbol", None) or getattr(v, "symbol", None) \
                      or self._trade_symbol
                long_total = (getattr(v, "pos_long_today", 0) or 0) \
                           + (getattr(v, "pos_long_his", 0) or 0)
                short_total = (getattr(v, "pos_short_today", 0) or 0) \
                            + (getattr(v, "pos_short_his", 0) or 0)
                self._initial_account_state[sym] = (long_total, short_total)
            # 启动时账户基线检查（如果非 0，发出警告日志）
            non_zero = {k: v for k, v in self._initial_account_state.items()
                        if v[0] > 0 or v[1] > 0}
            if non_zero:
                import logging
                log = logging.getLogger("tg.brokers.simnow")
                log.warning(
                    "⚠️  P5: gateway 启动时账户已有非零持仓（疑似上轮遗留仓）: %s。"
                    "  本轮 gateway 信号产生的成交，将按 P5 严格以 _initial_account_state 为锚点做精确校验。",
                    non_zero,
                )
        except Exception as e:
            import logging
            logging.getLogger("tg.brokers.simnow").warning(
                "P5 _capture_initial_account_state 失败: %s: %s（不影响下单流程，仅无法做启动基线校验）",
                type(e).__name__, e,
            )

    # ---------------- 下单 ----------------
    # 下单价策略（M4 改）：不再用"信号K线收盘价朝不利方向取整"的保守挂单，
    # 改为「超价」——下单瞬间取实时对手价（买→ask / 卖→bid），再 ± overprice_ticks×tick
    # （默认 5 tick = IF 1.0 点，向上/向下取整到 tick 以保证不低于该超价），主动跨过价差确保成交。
    #   · 开多 / 平空（买方向）：对手价 = ask，超价 = ask + overprice（向上取整）
    #   · 开空 / 平多（卖方向）：对手价 = bid，超价 = bid - overprice（向下取整）
    # 取不到实时行情时回退到旧的 align_entry/align_exit（基于信号价）。
    #
    # 平仓卡单（最大风险点，M4 处理）：SimNow 不支持市价单，故用「限价追价」——
    # 每一轮都重新取最新对手价 ± overprice 定价（价格归一：超价自带追价属性，
    # 盘口怎么走，下一轮挂价就怎么跟，只要盘口有报价必然立即成交），
    # 最多 chase_max_number 轮（默认 3，终态回报到手立即重报、不 sleep）；
    # close_chase_ticks 仅在行情临时取不到时作兜底步长。
    # 轮数用尽仍不成交才放弃（rejected）→ severe 告警转人工（引擎对账机制兜底）。
    #
    # 报文（见 Base.INTENT_TO_OFFSET）：OPEN → offset=OPEN，CLOSE → offset=CLOSE。
    #   CLOSE 恒作用于跨日仓（引擎断言），故恒为平昨。
    #   平今 CLOSETODAY 只在**品种执行策略表第 1 列 = CLOSETODAY** 的品种上出现
    #   （此前由费率派生 + 交易所能力闸门双判，两者均已删除）——
    #   开不开这个口子由表说了算，Broker 不做任何品种 / 交易所判断。
    #
    # 派发只有两路：OPEN → _submit_open，其余（CLOSE）→ _submit_close。
    # 追不追价由 `is_exit` 决定，不由 intent 决定（理由见 Base 模块 docstring）。
    def submit(self, intent, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "",
               entry_date: str = "", is_exit: bool = False) -> Order:
        intent = self._resolve_intent(intent, side)
        if self._conn_error:
            return self._rejected(signal_key, side, intent.value, volume, ref_price,
                                  note, self._conn_error)
        if self._api is None:
            return self._rejected(signal_key, side, intent.value, volume, ref_price,
                                  note, "未连接")

        # P61：记录本地报单时刻（otg_latency 测量的起点锚）
        self._submit_t0 = time.time()

        if intent is OrderIntent.OPEN:
            return self._submit_open(intent, side, volume, ref_price, signal_key, note,
                                     is_exit=is_exit)
        return self._submit_close(intent, side, volume, ref_price, signal_key, note,
                                  is_exit=is_exit)

    @staticmethod
    def _is_buy(action: str, side: Side) -> bool:
        """该笔委托是不是买方向（决定用 ask 还是 bid 作对手价）。

        方向一律由调用方给出（信号方向 / 净敞口反向），本方法不做任何推断。
        """
        if action == "open":
            return side is Side.LONG
        # close：平空=买回，平多=卖出
        return side is Side.SHORT

    def _overprice_limit(self, action: str, side: Side,
                         overprice: float) -> Optional[float]:
        """超价限价：实时对手价 ± overprice（点数 = overprice_ticks × 品种 tick），并取整到 tick。

        取不到行情（未连接 / 无 tick 数据 / NaN）返回 None，由调用方回退。
        注意：tqsdk 在行情首帧未到 / 集合竞价 / 单边市缺一边报价时，
        ask_price1 / bid_price1 是 float('nan') 而非 None —— NaN 是真值，
        `not (ask and bid)` 拦不住，必须显式 isnan 判断，否则限价会算出 NaN。
        """
        if self._api is None:
            return None
        try:
            q = self._api.get_quote(self._trade_symbol)
        except Exception:
            return None
        ask = getattr(q, "ask_price1", None)
        bid = getattr(q, "bid_price1", None)
        for v in (ask, bid):
            if not isinstance(v, (int, float)) or math.isnan(v):
                return None
        if self._is_buy(action, side):
            # 买方向：对手价=ask，超价=ask+overprice，向上取整（保证 ≥ overprice）
            return self.state.round_price(float(ask) + overprice, "up")
        # 卖方向：对手价=bid，超价=bid-overprice，向下取整（保证 ≥ overprice）
        return self.state.round_price(float(bid) - overprice, "down")

    def _overprice(self) -> float:
        """超价点数 = overprice_ticks × 品种 tick（config broker_params.overprice_ticks）。

        全 FOK 模式要求限价内盘口深度 ≥ 全部手数才给终态，超价越厚全成概率越高，
        默认 5 tick（IF 5×0.2=1.0 点）。二期其它品种加载各自 state.price_tick 自动缩放。
        """
        return float(self._param("overprice_ticks")) * self.state.price_tick

    def _build_limit_price(self, action: str, side: Side, ref_price: float,
                           opp: Optional[float] = None) -> float:
        opp = float(opp) if opp is not None else float(self._param("overprice_ticks")) * self.state.price_tick
        limit = self._overprice_limit(action, side, opp)
        if limit is None:
            st = self.state
            limit = st.align_entry(ref_price, side.sign) if action == "open" \
                else st.align_exit(ref_price, side.sign)
        return limit

    def _take_baseline(self, side_key: str) -> int:
        """P4/P5 下单前持仓快照（等待 CTP 延迟回报同步完毕）。"""
        try:
            self._api.wait_update(deadline=time.time() + self._timing("baseline_settle_wait"))
            self._api.wait_update(deadline=time.time() + self._timing("baseline_settle_wait"))
            return _position_total(self._api, self._trade_symbol, side_key)
        except Exception:
            return 0

    def _take_baseline_split(self, side_key: str) -> Tuple[int, int]:
        """下单前的 **(今仓, 昨仓)** 快照，供 CLOSE 的平昨校验使用。

        失败返回 (-1, -1) —— 与 `_take_baseline` 失败返回 -1 同口径，
        `_verify_yesterday_delta` 见到负基线会直接判 False（不猜、不放行）。
        """
        try:
            self._api.wait_update(deadline=time.time() + self._timing("baseline_settle_wait"))
            self._api.wait_update(deadline=time.time() + self._timing("baseline_settle_wait"))
            sp = _position_split(self._api, self._trade_symbol, side_key)
        except Exception:
            return (-1, -1)
        return (-1, -1) if sp is None else sp

    def _submit_open(self, intent: OrderIntent, side: Side, volume: int, ref_price: float,
                     signal_key: str, note: str, is_exit: bool = False) -> Order:
        """开仓（offset=OPEN）。

        is_exit=False —— **入场**（转移 1/2/3，交易信号触发）：单次超价，不追价。
          2026-09-12 更正：原文写"转移 1/2/6"，而转移表**只有 1~5**
          （`Engine.py` 全部 `transition=` 赋值集合 = {1,2,3,4,5}；文档 L332 明写
          "状态转移表无第 6 种情况"）。转移 ③ 虽是 CLOSE，但 `is_exit=False`。
          "入场没成功，最多不赚钱，但不会亏钱。" 全撤 → rejected，等下一个信号。
        is_exit=True —— **软离场**（转移 4，运行态 L1-L3 触发的反向开仓锁仓）：
          必须追价。锁仓本质也是离场，卡单不追会让浮亏扩大、浮盈变浮亏。

        两种情形**报文完全相同**（offset=OPEN、direction=_DIRECTION[side]），
        差别只在追不追价 —— broker 无法从 (side, offset) 推断，故由引擎按触发源给出。
        """
        direction = _DIRECTION[side]
        offset = INTENT_TO_OFFSET[intent]
        side_key = "LONG" if side is Side.LONG else "SHORT"
        is_buy = self._is_buy("open", side)
        chase_sign = 1 if is_buy else -1          # 买→加价 / 卖→降价，朝成交方向追
        opp = self._overprice()
        max_attempts = int(self._param("chase_max_number")) if is_exit else 1
        per_wait = float(self._param("fill_timeout_close" if is_exit
                                     else "fill_timeout_open"))
        chase_ticks = float(self._param("close_chase_ticks"))

        last: Optional[Order] = None
        prev_limit: Optional[float] = None
        for attempt in range(1, max_attempts + 1):
            limit = self._overprice_limit("open", side, opp)
            if limit is None:
                limit = self._chase_fallback_limit("open", side, ref_price, prev_limit,
                                                   chase_sign, chase_ticks)
            prev_limit = limit
            baseline = self._take_baseline(side_key)
            expected_delta = int(volume)          # 开仓：side_key 方向 +volume
            try:
                order = self._api.insert_order(symbol=self._trade_symbol,
                                               direction=direction, offset=offset,
                                               volume=int(volume), limit_price=limit,
                                               advanced=self.state.effective_order_advanced())
            except Exception as e:
                return self._rejected(signal_key, side, intent.value, volume, ref_price, note,
                                      "下单失败: {}: {}".format(type(e).__name__, e))
            # 终态毫秒级到达；per_wait 仅为通道异常兜底 watchdog
            self._wait_finished(order, timeout_s=per_wait)
            o = self._finalize(order, intent.value, "open", side, volume, ref_price,
                               signal_key, note, baseline, expected_delta, limit,
                               attempt=attempt, max_attempts=max_attempts)
            last = o
            if o.status == "filled":
                return o
            if self._should_stop_chase(o):
                return o                          # 资金不足 / 非交易时段：追也无用
            # 全撤 → 立即进入下一轮重报（终态回报到手即重报，不 sleep 等待）
        return last if last is not None else self._rejected(
            signal_key, side, intent.value, volume, ref_price, note, "开仓未成交")

    def _chase_fallback_limit(self, action: str, side: Side, ref_price: float,
                              prev_limit: Optional[float],
                              chase_sign: int, chase_ticks: float) -> float:
        """行情取不到时的追价限价兜底。

        首笔（无上一笔限价可参考）→ 开仓用 align_entry / 平仓用 align_exit；
        后续轮次 → 在上一笔限价基础上朝成交方向推 chase_ticks 跳，
        保证即便行情断了，价格也单边朝能成交的方向推进。
        """
        if prev_limit is None:
            if action == "open":
                return self.state.align_entry(ref_price, side.sign)
            return self.state.align_exit(ref_price, side.sign)
        tick = self.state.price_tick
        return self.state.round_price(
            prev_limit + chase_sign * chase_ticks * tick,
            "up" if chase_sign > 0 else "down")

    def _submit_close(self, intent: OrderIntent, side: Side, volume: int, ref_price: float,
                      signal_key: str, note: str, is_exit: bool = False) -> Order:
        """平仓（offset 由 intent 决定：CLOSE→平昨 / CLOSETODAY→平今）。

        is_exit=True —— **硬离场**（转移 5，运行态 L1-L3 触发）：必须追价。
        is_exit=False —— **拆锁**（转移 3，锁仓态收到交易信号）：不追价。
          拆锁失败只是"继续锁着"，账户是安全的；为进场去追价反而不划算。

        为什么 CLOSE 恒为平昨：规则 ⑹/⑺ 保证 CLOSE **只作用于跨日仓**
        （断言在 Engine._pre_trade_check），今日单离场默认走反向 OPEN 软离场。
        故平昨报文恒为 tqsdk 白名单内的 "CLOSE"；CLOSETODAY（平今）在本系统
        里由**品种执行策略表第 1 列**开启：该品种
        `ExecPolicy.today_exit == "CLOSETODAY"` 时，转移 ④ 生成 CLOSETODAY 意图 →
        offset=CLOSETODAY、目标恒为**今仓**（引擎 _pre_trade_check 断言）。
        两意图在 P0 可平量判据与成交后今/昨验证上完全相反，见下。
        """
        offset = INTENT_TO_OFFSET[intent]
        is_today = intent is OrderIntent.CLOSETODAY
        # P0：close 前先等 tqsdk 持仓字段同步到 ≥ volume，挡"平仓量超过持仓量"拒单
        # D12/p38：CLOSE 的判据必须是**昨仓** —— 本系统的 CLOSE 恒为平昨
        #   （不变量 6，断言在 Engine._pre_trade_check）。旧的今+昨口径会放行
        #   "只有今仓"的情形，而中金所同时有今昨仓时默认先平今 → 要么平今多付 15 倍
        #   费率、要么被柜台拒，两者都与引擎的"平昨"假设不符。
        # CLOSETODAY 反之 —— 判据必须是**今仓**
        #   （`today_only=True`），否则"只有昨仓、今仓不足"也会被总量放行 → 平今被拒。
        #   等待超时 = 柜台很可能根本没有这笔昨仓/今仓（幻影仓的主路径）→ 带
        #   REJECT_POSITION 类别返回，让引擎的兜底逻辑认得出来（见 Base.REJECT_POSITION）。
        if not self._wait_position_ok(side, int(volume),
                                      timeout_s=self._timing("position_ok_timeout"),
                                      today_only=is_today):
            which = "今仓" if is_today else "昨仓"
            return self._rejected(signal_key, side, intent.value, volume, ref_price, note,
                                  "等待{}持仓更新超时（>{}s，仅认 pos_*_{}；"
                                  "可能柜台无此{}）".format(
                                      which, self._timing("position_ok_timeout"),
                                      "today" if is_today else "his", which),
                                  reject_class=REJECT_POSITION)
        side_key = "LONG" if side is Side.LONG else "SHORT"
        direction = _CLOSE_DIRECTION[side]  # 平多=SELL / 平空=BUY（2026-09-05 方向修复）
        is_buy = self._is_buy("close", side)
        chase_sign = 1 if is_buy else -1          # 买→加价 / 卖→降价，朝成交方向追
        opp = self._overprice()
        max_attempts = int(self._param("chase_max_number")) if is_exit else 1
        per_wait = float(self._param("fill_timeout_close" if is_exit
                                     else "fill_timeout_open"))
        chase_ticks = float(self._param("close_chase_ticks"))

        last: Optional[Order] = None
        prev_limit: Optional[float] = None
        for attempt in range(1, max_attempts + 1):
            # 追价策略（价格归一）：每轮都取「最新对手价 ± overprice」重新定价（overprice = overprice_ticks×tick）。
            # 超价本身自带追价属性——行情朝不利方向走了，下一轮的超价自动跟着盘口走，
            # 挂单价永远比当前对手价多让 overprice 一截，只要盘口有报价必然立即成交。
            # chase_ticks 只在行情临时取不到时作兜底步长。
            limit = self._overprice_limit("close", side, opp)
            if limit is None:
                limit = self._chase_fallback_limit("close", side, ref_price, prev_limit,
                                                   chase_sign, chase_ticks)
            prev_limit = limit
            # p38：平仓的基线要**分今/昨**取 —— 成交后要断言"平昨→昨仓降、今仓不动"
            # / "平今→今仓降、昨仓不动"，一个总量基线做不到这件事
            # （见 `_verify_yesterday_delta` / `_verify_today_delta`）。
            base_today, base_his = self._take_baseline_split(side_key)
            baseline = base_today + base_his if base_today >= 0 else -1
            expected_delta = -int(volume)
            try:
                order = self._api.insert_order(symbol=self._trade_symbol,
                                               direction=direction, offset=offset,
                                               volume=int(volume), limit_price=limit,
                                               advanced=self.state.effective_order_advanced())
            except Exception as e:
                return self._rejected(signal_key, side, intent.value, volume, ref_price, note,
                                      "下单失败: {}: {}".format(type(e).__name__, e))
            # 终态毫秒级到达；per_wait 仅为通道异常兜底 watchdog
            self._wait_finished(order, timeout_s=per_wait)
            o = self._finalize(order, intent.value, "close", side, volume, ref_price, signal_key,
                              note, baseline, expected_delta, limit,
                              attempt=attempt, max_attempts=max_attempts,
                              baseline_split=(base_today, base_his))
            last = o
            if o.status == "filled":
                return o
            if self._should_stop_chase(o):
                return o                          # 资金不足 / 非交易时段：追也无用
            # 全撤 → 立即进入下一轮重报（终态回报到手即重报，不 sleep 等待）
        return last if last is not None else self._rejected(
            signal_key, side, intent.value, volume, ref_price, note, "平仓未成交")

    def _finalize(self, order, intent_str: str, action: str, side: Side, volume: int, ref_price: float,
                  signal_key: str, note: str, baseline: int, expected_delta: int,
                  limit: float, attempt: int = 1, max_attempts: int = 1,
                  baseline_split: Optional[Tuple[int, int]] = None) -> Order:
        # ===== P3：必须 status=="FINISHED" 且 volume_left==0 才是真成交 =====
        is_fully_filled = (getattr(order, "status", "") == "FINISHED"
                           and getattr(order, "volume_left", None) == 0)
        filled = self._trade_price(order) if is_fully_filled else None

        # ===== P6 权威层：用 CTP 真实成交明细判定 =====
        # 真成交必须三层同时成立：① status==FINISHED ② volume_left==0
        # ③ sum(trade_records[*].volume) >= volume。P4/P5 仅作辅助诊断。
        traded_volume = _traded_volume_from_records(order)
        traded_price = _traded_price_from_records(order)
        reject_reason: Optional[str] = None
        if is_fully_filled and traded_volume < int(volume):
            last_msg = getattr(order, "last_msg", "")
            reject_reason = (
                "P6: CTP 成交明细只有 {} 手，不足委托 {} 手 (status=FINISHED, "
                "volume_left=0, trade_price={}, last_msg={})，判定为未成交".format(
                    traded_volume, int(volume),
                    getattr(order, "trade_price", None), last_msg))
            self._note_reject(signal_key, note, order, reject_reason)
            is_fully_filled = False
            filled = None
        # P6 权威成交价：优先取 CTP 成交明细的加权均价
        if is_fully_filled and traded_price:
            filled = traded_price

        # ===== P4/P5 降级为辅助层：只记录诊断，不再据此 reject =====
        # D12/p38：平仓与 OPEN 的
        #   "持仓变化正确性"是两件事，必须分开看 ——
        #     · OPEN：今仓增加 → 总量增加，`_verify_position_delta` 看总量就够；
        #     · CLOSE：平昨 → **昨仓**下降且**今仓一分不动**。总量判据在这里是
        #       "看不见"的：今仓 1 手被平掉、昨仓不变，总量同样减 1，旧判据照样通过。
        #     · CLOSETODAY：平今 → **今仓**下降且**昨仓一分不动**，
        #       是 CLOSE 的镜像，由 `_verify_today_delta` 验证。
        #   故 CLOSE 走 `_verify_yesterday_delta`、CLOSETODAY 走 `_verify_today_delta`
        #   （均有分拆基线时），OPEN 保持原逻辑。
        side_key = "LONG" if side is Side.LONG else "SHORT"
        verified = False
        if is_fully_filled:
            if action == "close" and baseline_split is not None:
                today_b, his_b = baseline_split
                if intent_str == "closetoday":
                    verified = _verify_today_delta(
                        self._api, self._trade_symbol, side_key,
                        today_baseline=today_b, his_baseline=his_b,
                        volume=int(volume),
                        timeout_s=self._timing("verify_delta_timeout"))
                    if not verified:
                        self._note_today_lag(signal_key, side_key,
                                             today_b, his_b, int(volume))
                else:
                    verified = _verify_yesterday_delta(
                        self._api, self._trade_symbol, side_key,
                        today_baseline=today_b, his_baseline=his_b,
                        volume=int(volume),
                        timeout_s=self._timing("verify_delta_timeout"))
                    if not verified:
                        self._note_yesterday_lag(signal_key, side_key,
                                                 today_b, his_b, int(volume))
            else:
                verified = _verify_position_delta(self._api, self._trade_symbol, side_key,
                                                 baseline=baseline,
                                                 expected_delta=expected_delta,
                                                 timeout_s=self._timing("verify_delta_timeout"))
                if not verified:
                    self._note_position_lag(signal_key, action, side_key,
                                            baseline, expected_delta)

        status = "filled" if is_fully_filled else "rejected"
        # D10：拒单原因分类。追价只在"价格不可达"时才有意义，
        #   资金不足 / 非交易时段追 100 轮也不可能成交 —— 由调用方据此处 break。
        reject_class = ""
        if status == "rejected":
            reject_class = classify_ctp_reject(getattr(order, "last_msg", ""))
        if status == "rejected" and getattr(order, "status", "") != "FINISHED":
            last_msg = getattr(order, "last_msg", "")
            reject_reason = (
                "未真正成交: status={}, volume_left={}, trade_price={}, last_msg={}".format(
                    getattr(order, "status", ""),
                    getattr(order, "volume_left", None),
                    getattr(order, "trade_price", None),
                    last_msg))
            self._note_reject(signal_key, note, order, reject_reason)

        o = Order(
            order_id=self._next_order_id(),
            signal_key=signal_key, symbol=self._trade_symbol, side=side,
            action=action, volume=int(volume), price=limit,
            req_price=float(ref_price), filled_price=filled,
            status=status, created_at=now_cn(), broker=self.name, note=note,
            meta={"raw_order_id": str(getattr(order, "order_id", "")),
                  "direction": _DIRECTION[side], "offset": action,
                  # action 仍是 "open"/"close"（兼容旧调用方），intent 表达开/平语义
                  "intent": intent_str,
                  "trade_price": filled,
                  "volume_left": getattr(order, "volume_left", None),
                  "last_msg": getattr(order, "last_msg", ""),
                  "reject_reason": reject_reason,
                  "reject_class": reject_class,
                  "attempt": attempt, "max_attempts": max_attempts},
        )
        # P61：回报链路时延测量 —— 每次终判一行，落 gateway.log。下次再出现
        # 「判拒后柜台成交」，一行日志即可分解滞后属于 otg 链路还是引擎泵间隙。
        try:
            _f = self._otg_latency_fields(order, self._submit_t0,
                                          self._watchdog_fired_at)
            import logging
            logging.getLogger("tg.brokers.simnow").info(
                "otg_latency: signal=%s action=%s status=%s submit→终判=%ss "
                "watchdog→终判=%ss insert→终判=%ss 成交→终判=%ss "
                "insert_dt=%s trade_dt=%s raw_order_id=%s",
                signal_key or "-", action, status, _f["since_submit_s"],
                _f["since_watchdog_s"], _f["insert_lag_s"], _f["trade_lag_s"],
                _f["insert_dt"], _f["trade_dt"],
                str(getattr(order, "order_id", "")))
        except Exception:
            pass

        self.orders.append(o)
        # 登记 signal_key → raw_order_id（trade_confirmed / cancel_pending
        # 复查用）。同一 signal_key 的追价重试会登记多条 raw 单，各自的
        # trade_records 互不重复，复查时累加安全。_rejected 路径没有真实
        # raw 单，不在此登记。
        raw_id = str(getattr(order, "order_id", "") or "")
        if raw_id and signal_key:
            self._sig_orders.setdefault(signal_key, []).append(raw_id)
        return o

    # ---------------- 内部工具 ----------------
    @staticmethod
    def _should_stop_chase(o: Order) -> bool:
        """本轮拒单是否属于"追价无用"的两类（D10）。

        命中 `funds`（资金不足）/ `not_tradable`（非交易时段、集合竞价、无权限）
        → 立即停追并保留 Order 供引擎写告警，不再空耗报撤单额度与监管计数。
        """
        return o.meta.get("reject_class") in NO_CHASE_REJECT_CLASSES

    @staticmethod
    def _otg_latency_fields(order, submit_t0: float,
                            watchdog_at: Optional[float]) -> Dict[str, Any]:
        """回报链路时延测量（P61）：把「交易所侧时刻」与「本地处理时刻」对表。

        tqsdk 的订单对象自带 CTP 侧时间戳（纳秒 epoch）：
          · insert_date_time —— 交易所/CTP 受理委托的时刻；
          · trade_records[*].trade_date_time —— 逐笔成交时刻。
        本地处理时刻（now）减去它们 = 「回报从 otg 到我们进程」的端到端滞后。
        空闲泵（SSE on_idle）生效后，回报在帧级被处理，该读数即逼近真实
        网络滞后；若实测仍大，即坐实 otg-simnow 链路本身慢 —— 与快期3 直连
        毫秒级的差距据此定量，不再靠 gateway.log 的「处理时刻」猜。

        字段缺时间戳 / 未成交 → 对应项 None，绝不抛异常（测量不干扰交易）。
        """
        now = time.time()
        ins_ns = 0.0
        try:
            ins_ns = float(getattr(order, "insert_date_time", 0) or 0)
        except (TypeError, ValueError):
            ins_ns = 0.0
        trades = getattr(order, "trade_records", None) or {}
        trade_ns = 0.0
        try:
            for _t in trades.values():
                d = float(getattr(_t, "trade_date_time", 0) or 0)
                if d > trade_ns:
                    trade_ns = d
        except Exception:
            trade_ns = 0.0
        ins_s = ins_ns / 1e9 if ins_ns > 0 else None
        trade_s = trade_ns / 1e9 if trade_ns > 0 else None
        return {
            "now": round(now, 3),
            "since_submit_s": round(now - submit_t0, 3) if submit_t0 else None,
            "since_watchdog_s": round(now - watchdog_at, 3) if watchdog_at else None,
            "insert_lag_s": round(now - ins_s, 3) if ins_s else None,
            "trade_lag_s": round(now - trade_s, 3) if trade_s else None,
            "insert_dt": round(ins_s, 3) if ins_s else None,
            "trade_dt": round(trade_s, 3) if trade_s else None,
        }

    def _trade_price(self, order) -> Optional[float]:
        tp = getattr(order, "trade_price", None)
        if tp is None:
            return None
        try:
            f = float(tp)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or f <= 0:
            return None
        return f

    def _wait_finished(self, order, timeout_s: float) -> None:
        """等待委托到达 FINISHED 真终态；超时撤单（watchdog）后只等
        cancel_settle_wait 的撤单确认窗口即返回，不额外阻塞。

        P61 修正（2026-09-18）：曾在此加「撤单后继续泵到真终态」的 60s 阻塞
        兜底，被否决 —— 15s 周期图阻塞一分钟不可接受，且它建立在「回报链路
        滞后数十秒是常态」的未证实假设上。把当天证据按代码事实重新分解：
          ① submit 后 5s 等待窗口内 wait_update 连续被泵（行情 tick 持续驱动），
            期间回报仍未到 → 「otg→我们进程」确有秒级以上滞后（交易所侧毫秒
            级成交，撤单回报「不可撤」佐证）；
          ② tqsdk wait_update 单线程、只在被驱动时消费网络帧，而引擎每根 bar
            才 pump 一次（keepalive_wait=0.2s）→ gateway.log 里 tqsdk 的日志
            时刻是「处理时刻」而非「到达时刻」，此前读出的「滞后 25~55s」
            被引擎泵间隙污染，不能直接当网络滞后。
        滞后究竟多大，由 _finalize 的 otg_latency 测量日志（交易所侧时间戳
        vs 本地处理时刻）下次实测分解；引擎泵间隙已由 SSE 源的 on_idle 回调
        （main.py 接线）收窄到帧级。若实测确认 otg 链路确实滞后数十秒，处理
        方案（异步终判 / 换直连通道）再据此设计，不在报单路径里阻塞硬等。

        watchdog 语义保留：防通道彻底失联时 submit 永久阻塞。撤单后若在
        cancel_settle_wait 窗口内收到终态，_finalize 照常据真终态判定；窗口
        外才到的终态 → Order 判 rejected，此时若柜台实际成交，由对账盲区
        告警（Reconcile 空侧检查，无时间宽限）告知，otg_latency 留下精确
        时延证据。
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            # wait_update 必须带 deadline，否则订单无回报时会无限阻塞
            self._api.wait_update(deadline=deadline)
            if getattr(order, "status", "") == "FINISHED":
                return
        # 超时撤单（watchdog 语义保留）
        self._watchdog_fired_at = time.time()
        try:
            self._api.cancel_order(order.order_id)
            self._api.wait_update(deadline=time.time() + self._timing("cancel_settle_wait"))
        except Exception:
            pass

    def _wait(self, predicate, timeout_s: float = 30.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._api.wait_update(deadline=deadline)
            if predicate():
                return True
            time.sleep(_POLL_INTERVAL_SLOW)
        return False

    def _wait_position_ok(self, side: Side, volume: int,
                          timeout_s: Optional[float] = None,
                          require_yesterday: bool = True,
                          today_only: bool = False) -> bool:
        """等 tqsdk position 字段更新到"可平量 ≥ volume"（防 CTP "平仓量超过持仓量"）。

        上一笔 open 成交后，tqsdk 端 position.pos_long_today 等字段不会立刻同步，
        需要若干次 wait_update 推过来。如果直接发 close，CTP 端"看不到"对应持仓会拒。
        Step 2.3：timeout_s 缺省时走 BrokerConfig.channel.position_ok_timeout
        （原硬编码 10.0 收口）；生产调用点均显式传入。

        `require_yesterday`（D12 第 4 项，新增，默认 True）：
          本方法**唯一的生产调用点是 `_submit_close`**，而本系统的 CLOSE 恒作用于
          跨日仓（不变量 6）→ 中金所下恒为**平昨**。因此判据必须是 **昨仓 ≥ volume**，
          不是"今+昨 ≥ volume"。
          旧判据（今+昨）漏掉的正是最危险的一种情形：**今仓 2 手、昨仓 0 手** ——
          总量判据放行，但中金所撮合规则是"同时有今昨仓默认先平今"，于是这笔单要么
          被撮合成平今（费率是平昨的 15 倍），要么因昨仓不足被拒；
          两种结果都和一个"以为在平昨"的引擎不相容。
          传 False 可退回旧的今+昨口径（仅供诊断/对照，生产不要用）。

        `today_only`（p51，新增，默认 False）：
          平今 CLOSETODAY 报文的目标恒为**今仓**（引擎 _pre_trade_check 断言），
          可平量判据必须是 **今仓 ≥ volume**（不是今+昨，也不是昨仓）。
          为 True 时覆盖 require_yesterday 的语义；本方法两个生产调用方
          （CLOSE → require_yesterday=True；CLOSETODAY → today_only=True）
          永远不会同时传 True。
        """
        if timeout_s is None:
            timeout_s = self._timing("position_ok_timeout")

        def _avail() -> Optional[int]:
            """可平量：today_only 只认今仓；require_yesterday 只认昨仓；否则今+昨。"""
            sp = _position_split(self._api, self._trade_symbol,
                                 "LONG" if side is Side.LONG else "SHORT")
            if sp is None:
                return None
            today, his = sp
            if today_only:
                return today
            return his if require_yesterday else today + his

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._api.wait_update(deadline=deadline)
            try:
                avail = _avail()
                if avail is not None and avail >= volume:
                    return True
            except Exception:
                pass
            time.sleep(_POLL_INTERVAL_FAST)
        return False

    def _note_reject(self, signal_key: str, note: str, order, last_msg: str) -> None:
        """记录拒单/追价失败原因（审计用）。

        只写日志，不改变判定结果。原因同时持久化到 Order.meta.reject_reason
        （见 _finalize），保证 events.jsonl 里可追溯「平仓追价为什么没成」。
        """
        import logging
        logging.getLogger("tg.brokers.simnow").warning(
            "委托被拒: signal=%s note=%s 原因=%s raw_order_id=%s",
            signal_key or "-", note or "-", last_msg or "-",
            str(getattr(order, "order_id", "-")))

    def _note_position_lag(self, signal_key: str, action: str, side_key: str,
                           baseline: int, expected_delta: int) -> None:
        """P4/P5 降级后的诊断钩子：position 缓存未按预期变化时只告警，不 reject。

        真成交的权威判定已交给 P6 的 trade_records。这里记录的是"position 端
        与 order 端不同步"的现象，用于事后审计（比如 CTP 同步慢、有历史遗留仓）。
        """
        try:
            cur = _position_total(self._api, self._trade_symbol, side_key)
        except Exception:
            cur = -1
        import logging
        logging.getLogger("tg.brokers.simnow").warning(
            "P4/P5 诊断（不影响成交判定）: %s %s 后 position 端未按预期变化 "
            "(baseline=%s, expected_delta=%s, target=%s, cur=%s, signal=%s)",
            action, side_key, baseline, expected_delta,
            baseline + expected_delta, cur, signal_key or "-")

    def _note_yesterday_lag(self, signal_key: str, side_key: str,
                            today_baseline: int, his_baseline: int,
                            volume: int) -> None:
        """CLOSE 的平昨校验未成立时的诊断钩子（D12/p38，2026-09-13）。

        两种成因，日志里分别是：
          · `today_dropped`：今仓下降了 → 柜台把 CLOSE 撮合到了今仓（平今费率
            中金所 0.0345% ≈ 平昨 15 倍）。这是**必须有人知道**的异常。
          · `his_unchanged`：昨仓没按预期减少 → CTP 回报延迟，或簿面与柜台不一致。
        与 `_note_position_lag` 一样只告警、不改变成交事实（权威判据仍是 P6）。
        """
        try:
            sp = _position_split(self._api, self._trade_symbol, side_key)
        except Exception:
            sp = None
        today_cur, his_cur = sp if sp is not None else (-1, -1)
        if 0 <= today_cur < today_baseline:
            why = "today_dropped（CLOSE 被撮合到了今仓 → 平今费率，须人工核对）"
        else:
            why = "his_unchanged（昨仓未按预期减少；CTP 延迟或簿实不一致）"
        import logging
        logging.getLogger("tg.brokers.simnow").warning(
            "P4/P5 诊断（不影响成交判定）: close %s 平昨校验未成立 [%s] "
            "(today baseline=%s → %s, his baseline=%s → %s, 期望昨仓减 %s, signal=%s)",
            side_key, why, today_baseline, today_cur, his_baseline, his_cur,
            volume, signal_key or "-")

    def _note_today_lag(self, signal_key: str, side_key: str,
                        today_baseline: int, his_baseline: int,
                        volume: int) -> None:
        """CLOSETODAY 的平今校验未成立时的诊断钩子（p51）。

        `_note_yesterday_lag` 的镜像（对应 `_verify_today_delta`）：
          · `his_dropped`：昨仓下降了 → 柜台把 CLOSETODAY 撮合到了昨仓 → 账实错位
            （引擎簿按今仓记账），这是**必须有人知道**的异常。
          · `today_unchanged`：今仓没按预期减少 → CTP 回报延迟，或簿面与柜台不一致。
        与 `_note_position_lag` 一样只告警、不改变成交事实（权威判据仍是 P6）。
        """
        try:
            sp = _position_split(self._api, self._trade_symbol, side_key)
        except Exception:
            sp = None
        today_cur, his_cur = sp if sp is not None else (-1, -1)
        if 0 <= his_cur < his_baseline:
            why = "his_dropped（CLOSETODAY 被撮合到了昨仓 → 账实错位，须人工核对）"
        else:
            why = "today_unchanged（今仓未按预期减少；CTP 延迟或簿实不一致）"
        import logging
        logging.getLogger("tg.brokers.simnow").warning(
            "P4/P5 诊断（不影响成交判定）: closetoday %s 平今校验未成立 [%s] "
            "(today baseline=%s → %s, his baseline=%s → %s, 期望今仓减 %s, signal=%s)",
            side_key, why, today_baseline, today_cur, his_baseline, his_cur,
            volume, signal_key or "-")

    def _rejected(self, signal_key: str, side: Side, action_str: str, volume: int,
                  ref_price: float, note: str, why: str,
                  reject_class: str = "") -> Order:
        # action_str 实际是 OrderIntent.value；为兼容旧调用方沿用 "open"/"close" 字符串
        #
        # `reject_class`（新增）：本地拦下的拒单（没走到 CTP 回执，因此
        # 没有 last_msg 可供 classify_ctp_reject 判）也要带类别 —— 引擎
        # `_alert_on_reject` 靠它查 `_REJECT_ALERTS` 决定弹什么告警；拿不到
        # 类别就只剩 `order_rejected` 流水，前端看不见。
        # "平仓前持仓等待超时"是「柜台无此仓」的典型路径（见 `_submit_close`
        # 的 P0 注释与 D10 的 REJECT_POSITION），分类为 position。
        o = Order(
            order_id=self._next_order_id(),
            signal_key=signal_key, symbol=self.state.trade_symbol, side=side,
            action=action_str, volume=int(volume), price=float(ref_price),
            req_price=float(ref_price), filled_price=None, status="rejected",
            created_at=now_cn(), broker=self.name, note=note,
            meta={"reject_reason": why, "intent": action_str,
                  "reject_class": reject_class},
        )
        self.orders.append(o)
        return o

    def _quote_stale(self) -> bool:
        """行情快照是否陈旧（True = 不可信，读仓应降级 None）。

        判据：quote.datetime（交易所本地时间 = 本机北京时间）距 now 超过
        BrokerConfig.channel.quote_stale_seconds。datetime 为空（订阅后首帧未到 / 格式异常）→ 判陈旧。
        """
        q = self._quote
        if q is None:
            # 惰性订阅（_connect 未走到或旧实例迁移场景）：get_quote 非阻塞，仅发起订阅
            try:
                q = self._quote = self._api.get_quote(self._trade_symbol)
            except Exception:
                return True
        dt = getattr(q, "datetime", "") or ""
        if not dt:
            return True
        try:
            ts = time.mktime(time.strptime(dt.split(".")[0], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return True
        return (time.time() - ts) > self._timing("quote_stale_seconds")

    def _channel_unstable(self) -> bool:
        """通道不稳定 / 数据不可信 → True，读仓应跳过（real_position 返回 None）。

        背景：断连→重连窗口内 `get_position()` 可能返回陈旧或空数据，被
        `real_position` 读成 0 会误导引擎 `_reconcile_positions` 误清真实存在的
        持仓（实测：SimNow OTG 掉线重连期间对账读到 real=0，把一笔 2 手多单在
        引擎内存整笔冲销，而账户实际持仓未动）。故在**读仓前**统一把关。

        加固：tqsdk 3.10.2 **没有**公开连接状态接口——TqApi.is_connecting
        不存在（hasattr=False，全包 grep 0 命中）；内部重连标志
        （TqReconnect._un_processed）挂在 _init_connection 局部变量上，外部不可达。
        因此采用**行情新鲜度判据**（不依赖 tqsdk 版本）：

          · wait_update 抛异常（断连）→ 不稳定；
          · 行情快照 quote.datetime 停滞 > channel.quote_stale_seconds（断连重连中 /
            TCP 假死 / 首帧未到）→ 数据不可信。IF 交易时段每 0.5s 一个 tick，
            30s 阈值足够宽容。

        注意：tqsdk 的 wait_update 须由持有 api 的主线程驱动，引擎 on_bar 与
        broker 同线程满足；本方法在调用方非 wait_update 进行中时调用，嵌套安全。
        """
        if self._api is None:
            return True
        try:
            self._api.wait_update(deadline=time.time() + 0.3)
        except Exception:
            # wait_update 异常（断连）→ 保守判不稳定
            return True
        return self._quote_stale()

    def real_position(self, side: Side) -> Optional[int]:
        """查询 SimNow 真实持仓（引擎对账用）。未连接 / 通道不稳定 / 行情陈旧
        返回 None，引擎对账对应跳过该侧，避免用不可靠读数误清真实持仓。

        加固：新增行情新鲜度守卫（见 _channel_unstable docstring）——
        断连/重连/假死窗口内行情停滞，读数不可信，宁可让对账跳过也不冒误清风险。

        返回该方向当前净持仓手数；供 engine 的持仓对账（增强 B）检测
        「用户在快期3手工平仓 / 幽灵持仓」并修正引擎账目。
        """
        if self._api is None or self._channel_unstable():
            return None
        try:
            return _position_total(self._api, self._trade_symbol,
                                  "LONG" if side is Side.LONG else "SHORT")
        except Exception:
            return None

    def trade_confirmed(self, intent, signal_key: str = "") -> bool:
        """UNLOCK 卡单 5 bars 后复核 —— 查 CTP 真实成交明细。

        复查策略：**不信任** submit 时 ``_finalize`` 的判定（F1 防的正是提交
        时刻的状态漂移——CTP 通道异常会让 tqsdk 端状态与交易所实际不符），而是
        按 signal_key → raw_order_id 索引，用 ``api.get_order(raw_id)`` 重新拉取
        **当前**订单，累计 ``order.trade_records`` 真实成交量再判定。

        判定口径：sum(各 raw 单 trade_records[*].volume) >= 该 signal_key 的
        委托量 → True。追价重试产生的多条 raw 单，trade_records 互不重复，
        累加安全（partial + 重试全成的场景也能正确确认）。

        查不到索引 / api 不可用 / 单笔 get_order 失败（跳过该单）/ 任何异常
        → False（保守），交由引擎 _reconcile_positions 按真实持仓兜底。
        """
        if self._api is None or not signal_key:
            return False
        raw_ids = list(self._sig_orders.get(signal_key) or [])
        if not raw_ids:
            return False

        # 期望成交量：取该 signal_key 下非 rejected 委托的最大 volume
        # （追价重试各 attempt 的 volume 相同，取 max 防御异常数据）
        expected = 0
        for o in self.orders:
            if getattr(o, "signal_key", "") != signal_key:
                continue
            if getattr(o, "status", "") == "rejected":
                continue
            try:
                expected = max(expected, int(o.volume))
            except (TypeError, ValueError):
                continue
        if expected <= 0:
            return False

        # 先给一次同步窗口，让 tqsdk 把该单最新回报推过来（wait_update 必须
        # 由持有 api 的线程驱动——引擎 on_bar 与 broker 同线程，满足）
        try:
            self._api.wait_update(deadline=time.time() + 1.0)
        except Exception:
            pass

        total_traded = 0
        try:
            for raw_id in raw_ids:
                try:
                    raw = self._api.get_order(raw_id)
                except Exception:
                    continue  # 查不到（如会话重建后丢单）→ 该单不计入，保守
                if raw is None:
                    continue
                total_traded += _traded_volume_from_records(raw)
        except Exception:
            return False
        return total_traded >= expected

    def cancel_pending(self, signal_key: str = "") -> int:
        """撤掉该 signal_key 下所有未终态的在途委托，返回撤单请求数。

        先撤在途单、供调用方按真实持仓修正 —— 防止「挂单后又成交」的
        双重平仓。已 FINISHED 的单跳过；api 不可用 / 无索引 / 全部已终态 → 0。
        """
        if self._api is None or not signal_key:
            return 0
        raw_ids = list(self._sig_orders.get(signal_key) or [])
        n = 0
        for raw_id in raw_ids:
            try:
                raw = self._api.get_order(raw_id)
            except Exception:
                continue
            if raw is None:
                continue
            if getattr(raw, "status", "") == "FINISHED":
                continue  # 已终态，撤无可撤
            try:
                self._api.cancel_order(raw_id)
                n += 1
            except Exception:
                continue
        if n > 0:
            # 给 CTP 撤单回报留同步窗口（撤单确认回 local 缓存需要 wait_update）
            try:
                self._api.wait_update(deadline=time.time() + 2.0)
            except Exception:
                pass
        return n

    def close(self) -> None:
        if self._api is not None:
            try:
                self._api.close()
            except Exception:
                pass
            self._api = None
        self._quote = None

    def stats(self) -> Dict[str, Any]:
        return {"broker": self.name, "orders": len(self.orders),
                "trade_symbol": self._trade_symbol,
                "conn_error": self._conn_error,
                # P5：把启动时账户基线暴露到 stats，便于日志/诊断能看到"幽灵仓从哪来"
                "initial_account_state": dict(self._initial_account_state),
                # 暴露账户路由信息（审计用）
                "market": (str(self.tq_market).strip()
                           if self.is_live else "simnow"),
                "is_live": self.is_live,
                "confirm_live_trading": self.confirm_live,
                # 加固：行情新鲜度诊断（True=陈旧，real_position 会降级 None）
                "quote_stale": (self._quote_stale() if self._api is not None else None)}


@register_broker
class LiveCTPBroker(SimNowBroker):
    """实盘 CTP broker。

    Trading/Config.py 里 ``broker = "live"`` 时使用。与 SimNowBroker 共享全部
    逻辑（超价/追价/P0..P6 保障），仅 name 不同 → 账户路由走实盘分支：
    TqAccount(tq_market=期货公司名, live_account, live_password)，
    且必须显式开启 broker_params.confirm_live_trading=true 才允许启动。
    """
    name = "live"

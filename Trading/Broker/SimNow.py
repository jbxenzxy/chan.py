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
      ± overprice_points（默认 1.0 点 = IF 5 tick，朝成交方向取整到 tick）主动跨价差成交；
      取不到行情则回退到基于信号价的 align_*。
    - 行情新鲜度守卫（2026-09-07 对账加固）：tqsdk 3.10.2 **没有**公开连接状态接口
      （is_connecting 不存在；内部重连 handler 是 _init_connection 局部变量不可达），
      断连→自动重连窗口内 wait_update 正常返回不抛异常，get_position 缓存陈旧——
      曾致换日对账把真实存在的 2 手多单误清（reconcile_real_zero）。故 real_position
      读仓前先校验行情快照新鲜度：IF 交易时段每 0.5s 一个 tick，quote.datetime 停滞
      超过 _QUOTE_STALE_SECONDS（30s）判数据陈旧 → 返回 None，引擎对账跳过该侧。
      覆盖"断连重连中"与"TCP 假死"两类场景，且不依赖 tqsdk 版本。
    - 全 FOK 报单（2026-09-06 全量化改造）：四类报单（OPEN 开仓 / UNLOCK 解锁 /
      LOCK 锁仓 / CLOSE 平仓）全部附加 CTP 报单属性 advanced="FOK"——限价立即
      全部成交否则全部撤销，由交易所撮合引擎强制执行，杜绝部分成交幽灵残留。
      · 入场（OPEN/UNLOCK）：全撤 → 本笔作废（rejected），不追价，等下一信号。
      · 离场（LOCK/CLOSE）：全撤 → 立即按最新对手价重新超价报单，最多
        close_max_chase 轮（每轮间隔 chase_interval 秒，防报撤单频率超限）；
        轮数用尽后由引擎跨 K 线持续重试，直到软/硬离场完成。
      · fill_timeout_open/close 退化为通道异常兜底 watchdog：正常时交易所毫秒级
        给出终态，超时撤单分支仅在断线/回报丢失时兜底。
      郑商所期货不支持 FOK（tqsdk 直接抛异常，暂不处理：只交易中金所金融期货）。
    - offset：open→OPEN；close→CLOSE（交易所自动平今/平昨）。
      中金所平今手续费差异只体现在成本模型（dry_run 的 cost_points），
      下单 offset 的精细平今（CLOSETODAY）留到实盘阶段再按持仓当日判定。

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

import itertools
import math
import os
import time
from typing import Any, Dict, List, Optional

from ..Config import BrokerConfig
from ..Config import BrokerConfig
from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.Types import Order, OrderIntent, Side, now_cn
from .Base import INTENT_TO_OFFSET, Broker, register_broker

# broker 参数默认值的**单一事实源**：Trading/Config.py 的 BrokerConfig 模型。
# 本文件不再自带任何兜底数值（2026-09-07 严格模式）—— params 由配置模型构造，
# 键必然齐全；取不到说明配置模型漏了字段，属于代码 bug，直接 fail-fast 抛异常。
# （2026-09-05 曾修：旧代码 fill_timeout_open 兜底 10.0，与配置表的 5.0 矛盾。）

_DIRECTION = {Side.LONG: "BUY", Side.SHORT: "SELL"}
# close 类报文（CLOSE/UNLOCK）的方向：平多=SELL、平空=BUY（与 _DIRECTION 相反）。
# 2026-09-05 修复：旧 _submit_close 直接用 _DIRECTION[side]，平多发 BUY —— CTP 会拒单
# 或平错方向；此前 dry_run 撮合不校验 direction 字符串，故回归未暴露。
_CLOSE_DIRECTION = {Side.LONG: "SELL", Side.SHORT: "BUY"}

# 行情新鲜度阈值（秒）：quote.datetime 停滞超过该值判数据陈旧（2026-09-07 对账加固）。
# IF 交易时段每 0.5s 一个 tick，30s 足够宽容；断连/重连中/TCP 假死时行情停滞，
# 此时 get_position 缓存必然不可信。刻意不走 _param（config 单一事实源）——
# 这是通道级安全阈值而非策略参数，避免用户 config 漏键导致 fail-fast 起不来。
#
# ⚠️ 夜盘/非交易时段限制（将来跑夜盘品种时必改）：
#   非交易时段行情停滞是正常现象，本判据会把"数据陈旧"误判为常态 →
#   real_position 恒返回 None。日盘 IF 无碍（引擎对账只由 bar 事件驱动，
#   交易时段外没有 bar，对账根本不触发）；但夜盘品种（如 au/ag 21:00-02:30、
#   螺纹 21:00-23:00）盘中存在"合约无 tick 的静默段"，且 21:00-次日 02:30
#   跨越本地日期变更，quote.datetime 的交易日语义也会变化。届时需把本判据
#   从"绝对时钟差"改为"按合约交易时段表判断是否处于应报价区间"（参考
#   Infra/InstrumentSpec.py 扩展交易时段元数据），否则夜盘对账会被恒跳过。
_QUOTE_STALE_SECONDS = 30.0


def _position_total(api, trade_symbol: str, side: str) -> int:
    """读 tqsdk 当前持仓总数（今+昨），失败返回 -1。

    用于 P4 修复的成交后二次校验。注意：传 symbol 也不传时，tqsdk 返回的是
    整个账户的 dict[symbol, Position]；这里取与 trade_symbol 匹配的那一条。
    """
    try:
        pos = api.get_position()
    except Exception:
        return -1
    item = None
    if isinstance(pos, dict):
        # 只认 trade_symbol 精确匹配；找不到 = 该合约当前无持仓（返回 0）。
        # 2026-09-07 收窄：删除旧的"取第一条多/空非零持仓"兜底——账户同时持有
        # 其他品种时会把别的合约误当本合约读（跨品种误判，污染 P4/P5 校验）。
        # 陈旧缓存 dict 缺键的场景由 real_position 的新鲜度守卫前置拦截。
        item = pos.get(trade_symbol)
    else:
        item = pos
    if item is None:
        return 0
    if side == "LONG":
        return (getattr(item, "pos_long_today", 0) or 0) \
             + (getattr(item, "pos_long_his", 0) or 0)
    else:
        return (getattr(item, "pos_short_today", 0) or 0) \
             + (getattr(item, "pos_short_his", 0) or 0)


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
        time.sleep(0.1)
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


@register_broker
class SimNowBroker(Broker):
    name = "simnow"

    def __init__(self, spec: InstrumentSpec, params: Optional[Dict[str, Any]] = None):
        super().__init__(spec, params)
        # 严格模式（2026-09-07）：broker_params 以 Trading/Config.py 的
        # BrokerConfig 为**唯一默认值来源**补齐 —— 调用方可以只传要覆盖的键；
        # 传了模型里没有的键（拼错 / 残留旧键）直接报错，不再静默忽略。
        self.params = BrokerConfig(**(params or {})).model_dump()
        self._api = None
        # 行情快照引用（_connect 成功后订阅），供 _quote_stale 新鲜度守卫读 datetime
        self._quote = None
        self._trade_symbol = spec.trade_symbol
        self._seq = itertools.count(1)
        self.orders: List[Order] = []
        # Phase G1：signal_key → [raw_order_id] 索引（trade_confirmed / cancel_pending
        # 复查用）。必须在凭据检查**之前**初始化 —— 缺凭据 early-return 时也要保证
        # 字段存在，否则单测实例化（无凭据）后访问会 AttributeError。
        self._sig_orders: Dict[str, List[str]] = {}
        self._conn_error: Optional[str] = None

        # ════════════════════════════════════════════════════════════════
        # Phase I1（2026-09-06）：SimNow 仿真 ↔ 实盘 CTP 账户选择
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
        # P3-7：凭据只走环境变量。2026-09-07 起配置里不再有账号密码字段（若将来
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

    # ---------------- 连接与合约映射 ----------------
    def _connect(self) -> None:
        """登录 CTP（SimNow 仿真 / 实盘期货公司），带重试。

        Phase I1：账户路由按 self.is_live 选择
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
                    backoff *= 1.5          # 5s → 7.5s → 11.25s
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
                    backoff *= 1.5
                continue

            self._resolve_trade_symbol()
            # 预订阅 trade_symbol 行情：行情新鲜度守卫（_quote_stale）依赖该订阅。
            # get_quote 非阻塞（仅发起订阅，数据随 wait_update 推送）；首帧未到前
            # datetime 为空 → _quote_stale 判陈旧，real_position 保守返回 None。
            try:
                self._quote = self._api.get_quote(self._trade_symbol)
            except Exception:
                self._quote = None
            return

        self._conn_error = last_err or "CTP 登录失败（未知原因）"
        self._api = None

    def pulse(self) -> None:
        """心跳：推一帧数据，保持连接活跃。

        SimNow 的 CTP 会话在空闲期会被标成"用户不活跃"并断连（实测短连接跑完
        立即退出，隔几分钟重连就报 `CTP:用户不活跃`）。SSE 实时模式两根 K 线之间
        可能隔好几分钟，靠 submit 里的 wait_update 不够，所以引擎每根 bar 调一次。

        注意：tqsdk 的 wait_update **不是线程安全的**，必须由调用方在主线程驱动，
        这里不能起后台线程。
        """
        if self._api is None:
            return
        try:
            self._api.wait_update(deadline=time.time() + 0.2)
        except Exception:
            # 心跳失败不抛——下一根 bar 会再试，真断连了 submit 会自己报错
            pass

    def _probe_alive(self, timeout_s: float = 8.0) -> bool:
        """探活：拿一次行情/账户数据，确认连接不是"用户不活跃"的僵尸连接。

        CTP 的"用户不活跃"不会抛异常，TqApi 构造也不报错，只有真正 wait_update
        收数据时才暴露（表现为超时或连接被断）。所以登录后必须探一次。
        """
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
        sig = self.spec.signal_symbol
        if not sig.startswith("KQ."):
            return
        try:
            q = self._api.get_quote(sig)
            hit = self._wait(lambda: bool(getattr(q, "underlying_symbol", None)),
                             timeout_s=20.0)
            if hit and q.underlying_symbol:
                self._trade_symbol = q.underlying_symbol
                self.spec.trade_symbol = q.underlying_symbol
        except Exception as e:
            self._conn_error = "主连映射失败: {}: {}".format(type(e).__name__, e)

    def _capture_initial_account_state(self) -> None:
        """P5: 在 _connect 后强制等 5 秒，让 CTP 推送所有未确认回报，建立启动时账户快照。

        如果 _initial_account_state 非 0（即账户在启动时已经有非零持仓），
        说明这是历史遗留仓（上轮 SSE 实时成交留下的），不是本轮 gateway 信号造成的。
        把这个信息写入 _initial_account_state 供后续 submit 做交叉校验用。
        """
        try:
            # 给 CTP 5 秒推完所有未确认回报
            self._api.wait_update(deadline=time.time() + 5.0)
            self._api.wait_update(deadline=time.time() + 5.0)
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
    # 改为「超价」——下单瞬间取实时对手价（买→ask / 卖→bid），再 ± overprice_points
    # （默认 0.6 点 = 3 tick，向上/向下取整到 tick 以保证不低于该超价），主动跨过价差确保成交。
    #   · 开多 / 平空（买方向）：对手价 = ask，超价 = ask + overprice（向上取整）
    #   · 开空 / 平多（卖方向）：对手价 = bid，超价 = bid - overprice（向下取整）
    # 取不到实时行情时回退到旧的 align_entry/align_exit（基于信号价）。
    #
    # 平仓卡单（最大风险点，M4 处理）：SimNow 不支持市价单，故用「限价追价」——
    # 每一轮都重新取最新对手价 ± overprice 定价（价格归一：超价自带追价属性，
    # 盘口怎么走，下一轮挂价就怎么跟，只要盘口有报价必然立即成交），
    # 最多 close_max_chase 轮；close_chase_ticks 仅在行情临时取不到时作兜底步长。
    # 仍不成交（如涨跌停锁死）才放弃（rejected），由引擎对账机制（增强 B）兜底。
    #
    # Phase C（2026-09-05）：按 intent 区分报文
    #   - OPEN   offset=Open
    #   - LOCK   offset=Open（方向由调用方填反，即"开反向同手数"），与 OPEN 报文相同但语义不同
    #   - CLOSE  offset=CloseToday（spec.close_today_first=True 时）或 CloseAny（False）
    #   - UNLOCK offset=CloseYesterday（避开平今）
    def submit(self, intent, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "") -> Order:
        intent = self._resolve_intent(intent, side)
        if self._conn_error:
            return self._rejected(signal_key, side, intent.value, volume, ref_price,
                                  note, self._conn_error)
        if self._api is None:
            return self._rejected(signal_key, side, intent.value, volume, ref_price,
                                  note, "未连接")

        if intent is OrderIntent.OPEN:
            # 开仓=入场语义：单次超价 + 超时撤单、不追价（"最多不赚钱，但不会亏钱"）
            return self._submit_open(intent, side, volume, ref_price, signal_key, note)
        if intent is OrderIntent.LOCK:
            # 锁仓=软离场，卡单必须追价（同平仓），否则浮亏扩大/浮盈变浮亏
            return self._submit_lock(intent, side, volume, ref_price, signal_key, note)
        if intent is OrderIntent.UNLOCK:
            # 2026-09-05 规格归一：解锁≈开仓（入场语义）——单次超价 + fill_timeout_open
            # 超时撤单、不追价（"入场没成功，最多不赚钱，但不会亏钱"）。
            # 报文仍是 CloseYesterday（平反向昨仓，避开平今费率）；
            # 通道级异常兜底交给引擎 Phase F1（5 bar 后 trade_confirmed 复核 + cancel_pending）。
            return self._submit_unlock(intent, side, volume, ref_price, signal_key, note)
        return self._submit_close(intent, side, volume, ref_price, signal_key, note)

    @staticmethod
    def _is_buy(action: str, side: Side) -> bool:
        """该笔委托是不是买方向（决定用 ask 还是 bid 作对手价）。

        LOCK 的方向已在引擎填为 pos.side 的反向，所以这里只看 side。
        """
        if action == "open":
            return side is Side.LONG
        # close：平空=买回，平多=卖出
        return side is Side.SHORT

    def _overprice_limit(self, action: str, side: Side,
                         overprice_points: float) -> Optional[float]:
        """超价限价：实时对手价 ± overprice_points，并取整到 tick。

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
            return self.spec.round_price(float(ask) + overprice_points, "up")
        # 卖方向：对手价=bid，超价=bid-overprice，向下取整（保证 ≥ overprice）
        return self.spec.round_price(float(bid) - overprice_points, "down")

    def _overprice(self) -> float:
        """超价点数（四类报单共用，config broker_params.overprice_points）。

        全 FOK 模式要求限价内盘口深度 ≥ 全部手数才给终态，超价越厚全成概率越高，
        默认 1.0（IF 5 tick）。
        """
        return float(self._param("overprice_points"))

    def _build_limit_price(self, action: str, side: Side, ref_price: float,
                           opp: Optional[float] = None) -> float:
        opp = float(opp) if opp is not None else float(self._param("overprice_points"))
        limit = self._overprice_limit(action, side, opp)
        if limit is None:
            spec = self.spec
            limit = spec.align_entry(ref_price, side.sign) if action == "open" \
                else spec.align_exit(ref_price, side.sign)
        return limit

    def _take_baseline(self, side_key: str) -> int:
        """P4/P5 下单前持仓快照（等待 CTP 延迟回报同步完毕）。"""
        try:
            self._api.wait_update(deadline=time.time() + 0.5)
            self._api.wait_update(deadline=time.time() + 0.5)
            return _position_total(self._api, self._trade_symbol, side_key)
        except Exception:
            return 0

    def _submit_open(self, intent: OrderIntent, side: Side, volume: int, ref_price: float,
                     signal_key: str, note: str) -> Order:
        direction = _DIRECTION[side]
        # OPEN / LOCK 都是 Open 报文——LOCK 是反向开仓（引擎已填反向 side）
        offset = INTENT_TO_OFFSET[intent]
        side_key = "LONG" if side is Side.LONG else "SHORT"
        # 全 FOK：全成或全撤由交易所撮合引擎保证，无部分成交幽灵残留；
        # 全撤 → 本笔作废（rejected），不追价，等下一信号
        limit = self._build_limit_price("open", side, ref_price, opp=self._overprice())
        baseline = self._take_baseline(side_key)
        expected_delta = int(volume)
        try:
            order = self._api.insert_order(symbol=self._trade_symbol,
                                           direction=direction, offset=offset,
                                           volume=int(volume), limit_price=limit,
                                           advanced="FOK")
        except Exception as e:
            return self._rejected(signal_key, side, intent.value, volume, ref_price, note,
                                  "下单失败: {}: {}".format(type(e).__name__, e))
        # fill_timeout_open 退化为通道异常兜底 watchdog（正常毫秒级终态，不会触发）
        timeout = float(self._param("fill_timeout_open"))
        self._wait_finished(order, timeout_s=timeout)
        return self._finalize(order, intent.value, "open", side, volume, ref_price, signal_key,
                             note, baseline, expected_delta, limit)

    def _submit_unlock(self, intent: OrderIntent, side: Side, volume: int, ref_price: float,
                       signal_key: str, note: str) -> Order:
        """UNLOCK（解锁）：与 OPEN 同为入场语义——单笔 FOK，全撤即作废，不追价。

        "入场没成功，最多不赚钱，但不会亏钱。"全撤 → rejected，锁仓持仓保持
        锁定状态，等下一个对向信号再解。

        与 _submit_open 的两点差异：
          · offset=CLOSEYESTERDAY（平反向昨仓，避开平今高费率）——报文语义不变
          · 保留 close 路径的 _wait_position_ok 前置守卫（close 类报文要求 CTP 侧
            确有持仓，挡"平仓量超过持仓量"拒单；这是提交前检查，不是追价）

        通道级异常兜底（撤单失败 / 回报漂移 / 判定后状态漂移）由引擎的解锁复核
        （unlock reconcile，engine._check_unlock_stuck）接管：in-flight 持久化 +
        5 bar 后 trade_confirmed 复核。
        """
        # P0 守卫：等 tqsdk 持仓字段同步到 ≥ volume，挡"平仓量超过持仓量"拒单
        if not self._wait_position_ok(side, int(volume), timeout_s=10.0):
            return self._rejected(signal_key, side, intent.value, volume, ref_price, note,
                                  "等待持仓更新超时（>10s），可能上游未同步")
        direction = _CLOSE_DIRECTION[side]  # 平多=SELL / 平空=BUY（2026-09-05 方向修复）
        offset = "CLOSEYESTERDAY"
        # 全 FOK：与 OPEN 同源（实时对手价 ± 超价，主动跨价差确保一笔全成）
        opp = self._overprice()
        limit = self._overprice_limit("close", side, opp)
        if limit is None:
            limit = self.spec.align_exit(ref_price, side.sign)
        side_key = "LONG" if side is Side.LONG else "SHORT"
        baseline = self._take_baseline(side_key)
        expected_delta = -int(volume)
        try:
            order = self._api.insert_order(symbol=self._trade_symbol,
                                           direction=direction, offset=offset,
                                           volume=int(volume), limit_price=limit,
                                           advanced="FOK")
        except Exception as e:
            return self._rejected(signal_key, side, intent.value, volume, ref_price, note,
                                  "下单失败: {}: {}".format(type(e).__name__, e))
        # fill_timeout_open 退化为通道异常兜底 watchdog（正常毫秒级终态，不会触发）
        timeout = float(self._param("fill_timeout_open"))
        self._wait_finished(order, timeout_s=timeout)
        return self._finalize(order, intent.value, "close", side, volume, ref_price, signal_key,
                              note, baseline, expected_delta, limit)

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
                return self.spec.align_entry(ref_price, side.sign)
            return self.spec.align_exit(ref_price, side.sign)
        tick = self.spec.price_tick
        return self.spec.round_price(
            prev_limit + chase_sign * chase_ticks * tick,
            "up" if chase_sign > 0 else "down")

    def _submit_close(self, intent: OrderIntent, side: Side, volume: int, ref_price: float,
                      signal_key: str, note: str) -> Order:
        # Phase C：UNLOCK = CloseYesterday 报文；CLOSE 按 spec.close_today_first 选 CloseToday / CloseAny
        # （spec.close_today_first=True 时优先 CloseToday；False 时用 CloseAny 让交易所自动判定）
        if intent is OrderIntent.UNLOCK:
            offset = "CLOSEYESTERDAY"
        else:
            offset = "CLOSETODAY" if bool(self.spec.close_today_first) else "CLOSEANY"
        # P0：close 前先等 tqsdk 持仓字段同步到 ≥ volume，挡"平仓量超过持仓量"拒单
        if not self._wait_position_ok(side, int(volume), timeout_s=10.0):
            return self._rejected(signal_key, side, intent.value, volume, ref_price, note,
                                  "等待持仓更新超时（>10s），可能上游未同步")
        side_key = "LONG" if side is Side.LONG else "SHORT"
        direction = _CLOSE_DIRECTION[side]  # 平多=SELL / 平空=BUY（2026-09-05 方向修复）
        is_buy = self._is_buy("close", side)
        chase_sign = 1 if is_buy else -1          # 买→加价 / 卖→降价，朝成交方向追
        opp = self._overprice()
        max_attempts = int(self._param("close_max_chase"))
        per_wait = float(self._param("fill_timeout_close"))
        chase_ticks = float(self._param("close_chase_ticks"))
        chase_interval = float(self._param("chase_interval"))

        last: Optional[Order] = None
        prev_limit: Optional[float] = None
        for attempt in range(1, max_attempts + 1):
            # 追价策略（价格归一）：每轮都取「最新对手价 ± overprice」重新定价。
            # 超价本身自带追价属性——行情朝不利方向走了，下一轮的超价自动跟着盘口走，
            # 挂单价永远比当前对手价多让 overprice 一截，只要盘口有报价必然立即成交。
            # chase_ticks 只在行情临时取不到时作兜底步长。
            limit = self._overprice_limit("close", side, opp)
            if limit is None:
                limit = self._chase_fallback_limit("close", side, ref_price, prev_limit,
                                                   chase_sign, chase_ticks)
            prev_limit = limit
            baseline = self._take_baseline(side_key)
            expected_delta = -int(volume)
            try:
                order = self._api.insert_order(symbol=self._trade_symbol,
                                               direction=direction, offset=offset,
                                               volume=int(volume), limit_price=limit,
                                               advanced="FOK")
            except Exception as e:
                return self._rejected(signal_key, side, intent.value, volume, ref_price, note,
                                      "下单失败: {}: {}".format(type(e).__name__, e))
            # 全 FOK：终态毫秒级到达；per_wait 仅为通道异常兜底 watchdog
            self._wait_finished(order, timeout_s=per_wait)
            o = self._finalize(order, intent.value, "close", side, volume, ref_price, signal_key,
                              note, baseline, expected_delta, limit,
                              attempt=attempt, max_attempts=max_attempts)
            last = o
            if o.status == "filled":
                return o
            # 全撤：隔一小段间隔立即重报（防报撤单频率超限/FOK 撤单计数爆量）
            if attempt < max_attempts:
                time.sleep(chase_interval)
        return last if last is not None else self._rejected(
            signal_key, side, intent.value, volume, ref_price, note, "平仓追价用尽仍未成交")

    def _submit_lock(self, intent: OrderIntent, side: Side, volume: int, ref_price: float,
                     signal_key: str, note: str) -> Order:
        """锁仓（软离场）追价：与平仓同一 FOK 追价循环，但报文=Open（反向开仓）。

        锁仓本质也是离场（软离场），卡单必须追价，否则浮亏扩大、浮盈变浮亏。
        追价语义与 _submit_close 完全一致：每笔 FOK 全撤后隔 chase_interval 秒，
        按最新对手价 ± overprice 重新定价重报，close_max_chase 轮；轮数用尽后
        由引擎跨 K 线持续重试，直到锁仓完成。
        方向已由引擎填为 pos.side 的反向；offset=OPEN（Open 报文，可反向加仓）。
        """
        offset = INTENT_TO_OFFSET[intent]              # LOCK -> OPEN
        direction = _DIRECTION[side]                    # 开反向：方向=开仓方向
        side_key = "LONG" if side is Side.LONG else "SHORT"
        is_buy = self._is_buy("open", side)             # 开仓：买=side LONG
        chase_sign = 1 if is_buy else -1                # 买→加价 / 卖→降价，朝成交方向追
        opp = self._overprice()
        max_attempts = int(self._param("close_max_chase"))
        per_wait = float(self._param("fill_timeout_close"))
        chase_ticks = float(self._param("close_chase_ticks"))
        chase_interval = float(self._param("chase_interval"))

        last: Optional[Order] = None
        prev_limit: Optional[float] = None
        for attempt in range(1, max_attempts + 1):
            limit = self._overprice_limit("open", side, opp)
            if limit is None:
                limit = self._chase_fallback_limit("open", side, ref_price, prev_limit,
                                                   chase_sign, chase_ticks)
            prev_limit = limit
            baseline = self._take_baseline(side_key)
            expected_delta = int(volume)                # 反向开仓：side_key 方向 +volume
            try:
                order = self._api.insert_order(symbol=self._trade_symbol,
                                               direction=direction, offset=offset,
                                               volume=int(volume), limit_price=limit,
                                               advanced="FOK")
            except Exception as e:
                return self._rejected(signal_key, side, intent.value, volume, ref_price, note,
                                      "下单失败: {}: {}".format(type(e).__name__, e))
            # 全 FOK：终态毫秒级到达；per_wait 仅为通道异常兜底 watchdog
            self._wait_finished(order, timeout_s=per_wait)
            o = self._finalize(order, intent.value, "open", side, volume, ref_price, signal_key,
                              note, baseline, expected_delta, limit,
                              attempt=attempt, max_attempts=max_attempts)
            last = o
            if o.status == "filled":
                return o
            # 全撤：隔一小段间隔立即重报（防报撤单频率超限/FOK 撤单计数爆量）
            if attempt < max_attempts:
                time.sleep(chase_interval)
        # 未成交：轮数用尽，引擎跨 K 线持续重试
        return last if last is not None else self._rejected(
            signal_key, side, intent.value, volume, ref_price, note, "锁仓追价用尽仍未成交")

    def _finalize(self, order, intent_str: str, action: str, side: Side, volume: int, ref_price: float,
                  signal_key: str, note: str, baseline: int, expected_delta: int,
                  limit: float, attempt: int = 1, max_attempts: int = 1) -> Order:
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
        side_key = "LONG" if side is Side.LONG else "SHORT"
        verified = False
        if is_fully_filled:
            verified = _verify_position_delta(self._api, self._trade_symbol, side_key,
                                             baseline=baseline,
                                             expected_delta=expected_delta, timeout_s=5.0)
            if not verified:
                self._note_position_lag(signal_key, action, side_key,
                                        baseline, expected_delta)

        status = "filled" if is_fully_filled else "rejected"
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
            order_id="{}-{:06d}".format(self.name, next(self._seq)),
            signal_key=signal_key, symbol=self._trade_symbol, side=side,
            action=action, volume=int(volume), price=limit,
            req_price=float(ref_price), filled_price=filled,
            status=status, created_at=now_cn(), broker=self.name, note=note,
            meta={"raw_order_id": str(getattr(order, "order_id", "")),
                  "direction": _DIRECTION[side], "offset": action,
                  # Phase C：intent 与 order.action 双向记录
                  # action 仍是 "open"/"close"（兼容旧调用方），intent 表达真实语义
                  "intent": intent_str,
                  "trade_price": filled,
                  "volume_left": getattr(order, "volume_left", None),
                  "last_msg": getattr(order, "last_msg", ""),
                  "reject_reason": reject_reason,
                  "attempt": attempt, "max_attempts": max_attempts},
        )
        self.orders.append(o)
        # Phase G1：登记 signal_key → raw_order_id（trade_confirmed / cancel_pending
        # 复查用）。同一 signal_key 的追价重试会登记多条 raw 单，各自的
        # trade_records 互不重复，复查时累加安全。_rejected 路径没有真实
        # raw 单，不在此登记。
        raw_id = str(getattr(order, "order_id", "") or "")
        if raw_id and signal_key:
            self._sig_orders.setdefault(signal_key, []).append(raw_id)
        return o

    # ---------------- 内部工具 ----------------
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
        """等待委托到达 FINISHED 终态；超时则尝试撤单（通道异常兜底 watchdog）。

        2026-09-06 全 FOK 改造后的语义：
          四类报单全部 advanced="FOK"，交易所撮合引擎保证毫秒级给出终态
          （全成/全撤）。本函数退化为通道异常兜底 watchdog——正常永不触发；
          仅当断线/回报丢失导致订单永不到终态时，超时主动撤单防 submit 永久
          阻塞挂死引擎线程（撤单多半也失败，Order 判 rejected 交引擎复核兜底）。
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            # wait_update 必须带 deadline，否则订单无回报时会无限阻塞
            self._api.wait_update(deadline=deadline)
            if getattr(order, "status", "") == "FINISHED":
                return
        # 超时撤单
        try:
            self._api.cancel_order(order.order_id)
            self._api.wait_update(deadline=time.time() + 5)
        except Exception:
            pass

    def _wait(self, predicate, timeout_s: float = 30.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._api.wait_update(deadline=deadline)
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def _wait_position_ok(self, side: Side, volume: int,
                          timeout_s: float = 10.0) -> bool:
        """等 tqsdk position 字段更新到 ≥ volume（防 CTP "平仓量超过持仓量"）。

        上一笔 open 成交后，tqsdk 端 position.pos_long_today 等字段不会立刻同步，
        需要若干次 wait_update 推过来。如果直接发 close，CTP 端"看不到"对应持仓会拒。
        """
        try:
            pos = self._api.get_position(self._trade_symbol)
        except Exception:
            return False
        if side is Side.LONG:
            def _total():
                return (getattr(pos, "pos_long_today", 0) or 0) \
                     + (getattr(pos, "pos_long_his", 0) or 0)
        else:
            def _total():
                return (getattr(pos, "pos_short_today", 0) or 0) \
                     + (getattr(pos, "pos_short_his", 0) or 0)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._api.wait_update(deadline=deadline)
            try:
                if _total() >= volume:
                    return True
            except Exception:
                pass
            time.sleep(0.1)
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

    def _rejected(self, signal_key: str, side: Side, action_str: str, volume: int,
                  ref_price: float, note: str, why: str) -> Order:
        # Phase C：action_str 实际是 OrderIntent.value；为兼容旧调用方沿用 "open"/"close" 字符串
        o = Order(
            order_id="{}-{:06d}".format(self.name, next(self._seq)),
            signal_key=signal_key, symbol=self.spec.trade_symbol, side=side,
            action=action_str, volume=int(volume), price=float(ref_price),
            req_price=float(ref_price), filled_price=None, status="rejected",
            created_at=now_cn(), broker=self.name, note=note,
            meta={"reject_reason": why, "intent": action_str},
        )
        self.orders.append(o)
        return o

    def _quote_stale(self) -> bool:
        """行情快照是否陈旧（True = 不可信，读仓应降级 None）。

        判据：quote.datetime（交易所本地时间 = 本机北京时间）距 now 超过
        _QUOTE_STALE_SECONDS。datetime 为空（订阅后首帧未到 / 格式异常）→ 判陈旧。
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
        return (time.time() - ts) > _QUOTE_STALE_SECONDS

    def _channel_unstable(self) -> bool:
        """通道不稳定 / 数据不可信 → True，读仓应跳过（real_position 返回 None）。

        背景：断连→重连窗口内 `get_position()` 可能返回陈旧或空数据，被
        `real_position` 读成 0 会误导引擎 `_reconcile_positions` 误清真实存在的
        持仓（实测：SimNow OTG 掉线重连期间对账读到 real=0，把一笔 2 手多单在
        引擎内存整笔冲销，而账户实际持仓未动）。故在**读仓前**统一把关。

        2026-09-07 加固：tqsdk 3.10.2 **没有**公开连接状态接口——TqApi.is_connecting
        不存在（hasattr=False，全包 grep 0 命中）；内部重连标志
        （TqReconnect._un_processed）挂在 _init_connection 局部变量上，外部不可达。
        因此采用**行情新鲜度判据**（不依赖 tqsdk 版本）：

          · wait_update 抛异常（断连）→ 不稳定；
          · 行情快照 quote.datetime 停滞 > _QUOTE_STALE_SECONDS（断连重连中 /
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

        2026-09-07 加固：新增行情新鲜度守卫（见 _channel_unstable docstring）——
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
        """Phase G1：UNLOCK 卡单 5 bars 后复核 —— 查 CTP 真实成交明细。

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
        """Phase G2：撤掉该 signal_key 下所有未终态的在途委托，返回撤单请求数。

        引擎在 5-bar 卡单复核 trade_confirmed=False 时调用：先撤在途单，
        再按真实持仓修正 —— 防止「重建 portfolio 后挂单又成交」的双重平仓。
        已 FINISHED 的单跳过；api 不可用 / 无索引 / 全部已终态 → 0。
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

    def equity(self, source: str = "available") -> Optional[float]:
        """查询 SimNow 账户权益（仓位管理用）。未连接/查询失败返回 None。

        source="available" → 可用资金（已扣保证金占用与挂单冻结），偏保守；
        source="balance"   → 总资产权益（含浮盈、未扣占用）。
        取不到时 PositionSizer 会按 fallback_volume 保守回退，不会乱开仓。
        """
        if self._api is None:
            return None
        try:
            acc = self._api.get_account()
            self._api.wait_update(deadline=time.time() + 1.0)
        except Exception as e:
            import logging
            logging.getLogger("tg.brokers.simnow").warning(
                "equity() 查账户失败: %s: %s（仓位管理将回退 fallback_volume）",
                type(e).__name__, e)
            return None

        primary, backup = ("available", "balance") \
            if str(source).strip().lower() != "balance" else ("balance", "available")
        for field in (primary, backup):
            v = getattr(acc, field, None)
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isnan(f) or math.isinf(f) or f <= 0:
                continue
            return f
        return None

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
                # Phase I1：暴露账户路由信息（审计用）
                "market": (str(self.tq_market).strip()
                           if self.is_live else "simnow"),
                "is_live": self.is_live,
                "confirm_live_trading": self.confirm_live,
                # 2026-09-07 加固：行情新鲜度诊断（True=陈旧，real_position 会降级 None）
                "quote_stale": (self._quote_stale() if self._api is not None else None)}


@register_broker
class LiveCTPBroker(SimNowBroker):
    """实盘 CTP broker（Phase I1）。

    Trading/Config.py 里 ``broker = "live"`` 时使用。与 SimNowBroker 共享全部
    逻辑（超价/追价/P0..P6 保障），仅 name 不同 → 账户路由走实盘分支：
    TqAccount(tq_market=期货公司名, live_account, live_password)，
    且必须显式开启 broker_params.confirm_live_trading=true 才允许启动。
    """
    name = "live"

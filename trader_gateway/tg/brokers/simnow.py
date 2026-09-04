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
      ± overprice_points（默认 0.6 点 = 3 tick，朝成交方向取整到 tick）主动跨价差成交；
      取不到行情则回退到基于信号价的 align_*。平仓卡单时每轮重新按最新对手价超价
      （价格归一，超价自带追价属性），最多 close_max_chase 轮。
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

凭据（优先级：config.broker_params > 环境变量）
    sn_account / sn_password    SimNow 仿真账号
    tq_account / tq_password    天勤账号
    环境变量名：SN_ACCOUNT / SN_PASSWORD / TQ_ACCOUNT / TQ_PASSWORD

用法
    python run_gateway.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m \\
        --broker simnow --out ./run_live
"""
from __future__ import annotations

import itertools
import math
import os
import time
from typing import Any, Dict, List, Optional

from ..symbols import InstrumentSpec
from ..types import Order, Side, now_cn
from .base import Broker, register_broker

_DIRECTION = {Side.LONG: "BUY", Side.SHORT: "SELL"}
_OFFSET = {"open": "OPEN", "close": "CLOSE"}


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
        # 优先按 trade_symbol 精确匹配
        if trade_symbol in pos:
            item = pos[trade_symbol]
        else:
            # 兜底：找第一条多/空非零的（处理 dict key 与 trade_symbol 不一致的情况）
            for v in pos.values():
                if ((getattr(v, "pos_long_today", 0) or 0)
                        + (getattr(v, "pos_long_his", 0) or 0)
                        + (getattr(v, "pos_short_today", 0) or 0)
                        + (getattr(v, "pos_short_his", 0) or 0)) > 0:
                    item = v
                    break
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
        self._api = None
        self._trade_symbol = spec.trade_symbol
        self._seq = itertools.count(1)
        self.orders: List[Order] = []
        self._conn_error: Optional[str] = None

        # 凭据：params 优先，环境变量兜底
        self.sn_account = self._cred("sn_account", "SN_ACCOUNT")
        self.sn_password = self._cred("sn_password", "SN_PASSWORD")
        self.tq_account = self._cred("tq_account", "TQ_ACCOUNT")
        self.tq_password = self._cred("tq_password", "TQ_PASSWORD")

        missing = [k for k, v in
                   (("SN_ACCOUNT", self.sn_account), ("SN_PASSWORD", self.sn_password),
                    ("TQ_ACCOUNT", self.tq_account), ("TQ_PASSWORD", self.tq_password))
                   if not v]
        if missing:
            self._conn_error = "缺少 SimNow/天勤凭据: {}".format(", ".join(missing))
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
        v = (self.params.get(param_key) or os.environ.get(env_key) or "").strip()
        return v

    # ---------------- 连接与合约映射 ----------------
    def _connect(self) -> None:
        """登录 SimNow，带重试。

        SimNow 对短连接很敏感：上一轮 gateway 跑完立即退出，CTP 侧会把会话标成
        "用户不活跃"（实测 14:59:50 重连时直接报 `CTP:用户不活跃` + TqTimeoutError）。
        所以登录失败时不能立刻放弃，退避重试几次通常就能连上。
        """
        from tqsdk import TqApi, TqAuth, TqAccount

        max_attempts = int(self.params.get("connect_retries", 3) or 3)
        backoff = float(self.params.get("connect_backoff", 5.0) or 5.0)
        last_err: Optional[str] = None

        for attempt in range(1, max_attempts + 1):
            try:
                self._api = TqApi(
                    TqAccount("simnow", self.sn_account, self.sn_password),
                    auth=TqAuth(self.tq_account, self.tq_password))
            except Exception as e:
                last_err = "SimNow 登录失败: {}: {}".format(type(e).__name__, e)
                self._api = None
                if attempt < max_attempts:
                    import logging
                    logging.getLogger("tg.brokers.simnow").warning(
                        "SimNow 第 %d/%d 次登录失败（%.1fs 后重试）: %s",
                        attempt, max_attempts, backoff, last_err)
                    time.sleep(backoff)
                    backoff *= 1.5          # 5s → 7.5s → 11.25s
                continue

            # 登录成功，做一次探活：确认连接真的能收数据（挡"用户不活跃"的僵尸连接）
            if not self._probe_alive():
                last_err = "SimNow 连接探活失败（疑似 CTP:用户不活跃）"
                try:
                    self._api.close()
                except Exception:
                    pass
                self._api = None
                if attempt < max_attempts:
                    import logging
                    logging.getLogger("tg.brokers.simnow").warning(
                        "SimNow 第 %d/%d 次探活失败（%.1fs 后重试）",
                        attempt, max_attempts, backoff)
                    time.sleep(backoff)
                    backoff *= 1.5
                continue

            self._resolve_trade_symbol()
            return

        self._conn_error = last_err or "SimNow 登录失败（未知原因）"
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
    def submit(self, action: str, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "") -> Order:
        if self._conn_error:
            return self._rejected(signal_key, side, action, volume, ref_price,
                                  note, self._conn_error)
        if self._api is None:
            return self._rejected(signal_key, side, action, volume, ref_price,
                                  note, "未连接")

        if action == "close":
            return self._submit_close(side, volume, ref_price, signal_key, note)
        return self._submit_open(side, volume, ref_price, signal_key, note)

    @staticmethod
    def _is_buy(action: str, side: Side) -> bool:
        """该笔委托是不是买方向（决定用 ask 还是 bid 作对手价）。"""
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

    def _build_limit_price(self, action: str, side: Side, ref_price: float) -> float:
        opp = float(self.params.get("overprice_points", self.spec.overprice_points))
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

    def _submit_open(self, side: Side, volume: int, ref_price: float,
                     signal_key: str, note: str) -> Order:
        direction = _DIRECTION[side]
        offset = "OPEN"
        side_key = "LONG" if side is Side.LONG else "SHORT"
        # 超价下单：实时对手价 + overprice_points
        limit = self._build_limit_price("open", side, ref_price)
        baseline = self._take_baseline(side_key)
        expected_delta = int(volume)
        try:
            order = self._api.insert_order(symbol=self._trade_symbol,
                                           direction=direction, offset=offset,
                                           volume=int(volume), limit_price=limit)
        except Exception as e:
            return self._rejected(signal_key, side, "open", volume, ref_price, note,
                                  "下单失败: {}: {}".format(type(e).__name__, e))
        # 开仓卡单：超时（默认 10s，可调 fill_timeout_open）自动撤单，不成交即 rejected，
        # 引擎不产生幻影持仓，等下一信号再触发。
        timeout = float(self.params.get("fill_timeout_open", 10.0))
        self._wait_finished(order, timeout_s=timeout)
        return self._finalize(order, "open", side, volume, ref_price, signal_key,
                             note, baseline, expected_delta, limit)

    def _close_fallback_limit(self, side: Side, ref_price: float,
                              prev_limit: Optional[float],
                              chase_sign: int, chase_ticks: float) -> float:
        """行情取不到时的平仓限价兜底。

        首笔（无上一笔限价可参考）→ 回退 align_exit(信号价)；
        后续轮次 → 在上一笔限价基础上朝成交方向推 chase_ticks 跳，
        保证即便行情断了，价格也单边朝能成交的方向推进。
        """
        if prev_limit is None:
            return self.spec.align_exit(ref_price, side.sign)
        tick = self.spec.price_tick
        return self.spec.round_price(
            prev_limit + chase_sign * chase_ticks * tick,
            "up" if chase_sign > 0 else "down")

    def _submit_close(self, side: Side, volume: int, ref_price: float,
                      signal_key: str, note: str) -> Order:
        # P0：close 前先等 tqsdk 持仓字段同步到 ≥ volume，挡"平仓量超过持仓量"拒单
        if not self._wait_position_ok(side, int(volume), timeout_s=10.0):
            return self._rejected(signal_key, side, "close", volume, ref_price, note,
                                  "等待持仓更新超时（>10s），可能上游未同步")
        side_key = "LONG" if side is Side.LONG else "SHORT"
        direction = _DIRECTION[side]
        is_buy = self._is_buy("close", side)
        chase_sign = 1 if is_buy else -1          # 买→加价 / 卖→降价，朝成交方向追
        opp = float(self.params.get("overprice_points", self.spec.overprice_points))
        max_attempts = int(self.params.get("close_max_chase", 20))
        per_wait = float(self.params.get("fill_timeout_close", 5.0))
        chase_ticks = float(self.params.get("close_chase_ticks", 2))

        last: Optional[Order] = None
        prev_limit: Optional[float] = None
        for attempt in range(1, max_attempts + 1):
            # M4.2 追价策略（价格归一）：每轮都取「最新对手价 ± overprice」重新定价。
            # 超价本身自带追价属性——行情朝不利方向走了，下一轮的超价自动跟着盘口走，
            # 挂单价永远比当前对手价多让 overprice 一截，只要盘口有报价必然立即成交。
            # 不再在旧价上累加 chase_ticks。chase_ticks 只在行情临时取不到时作兜底步长。
            limit = self._overprice_limit("close", side, opp)
            if limit is None:
                limit = self._close_fallback_limit(side, ref_price, prev_limit,
                                                   chase_sign, chase_ticks)
            prev_limit = limit
            baseline = self._take_baseline(side_key)
            expected_delta = -int(volume)
            try:
                order = self._api.insert_order(symbol=self._trade_symbol,
                                               direction=direction, offset="CLOSE",
                                               volume=int(volume), limit_price=limit)
            except Exception as e:
                return self._rejected(signal_key, side, "close", volume, ref_price, note,
                                      "下单失败: {}: {}".format(type(e).__name__, e))
            self._wait_finished(order, timeout_s=per_wait)
            o = self._finalize(order, "close", side, volume, ref_price, signal_key,
                              note, baseline, expected_delta, limit,
                              attempt=attempt, max_attempts=max_attempts)
            last = o
            if o.status == "filled":
                return o
            # 未成交：_wait_finished 已撤单，进入下一轮重新超价
        return last if last is not None else self._rejected(
            signal_key, side, "close", volume, ref_price, note, "平仓追价用尽仍未成交")

    def _finalize(self, order, action: str, side: Side, volume: int, ref_price: float,
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
                  "trade_price": filled,
                  "volume_left": getattr(order, "volume_left", None),
                  "last_msg": getattr(order, "last_msg", ""),
                  "reject_reason": reject_reason,
                  "attempt": attempt, "max_attempts": max_attempts},
        )
        self.orders.append(o)
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

    def _rejected(self, signal_key: str, side: Side, action: str, volume: int,
                  ref_price: float, note: str, why: str) -> Order:
        o = Order(
            order_id="{}-{:06d}".format(self.name, next(self._seq)),
            signal_key=signal_key, symbol=self.spec.trade_symbol, side=side,
            action=action, volume=int(volume), price=float(ref_price),
            req_price=float(ref_price), filled_price=None, status="rejected",
            created_at=now_cn(), broker=self.name, note=note,
            meta={"reject_reason": why},
        )
        self.orders.append(o)
        return o

    def real_position(self, side: Side) -> Optional[int]:
        """查询 SimNow 真实持仓（引擎对账用）。未连接返回 None。

        返回该方向当前净持仓手数；供 engine 的持仓对账（增强 B）检测
        「用户在快期3手工平仓 / 幽灵持仓」并修正引擎账目。
        """
        if self._api is None:
            return None
        try:
            return _position_total(self._api, self._trade_symbol,
                                  "LONG" if side is Side.LONG else "SHORT")
        except Exception:
            return None

    def close(self) -> None:
        if self._api is not None:
            try:
                self._api.close()
            except Exception:
                pass
            self._api = None

    def stats(self) -> Dict[str, Any]:
        return {"broker": self.name, "orders": len(self.orders),
                "trade_symbol": self._trade_symbol,
                "conn_error": self._conn_error,
                # P5：把启动时账户基线暴露到 stats，便于日志/诊断能看到"幽灵仓从哪来"
                "initial_account_state": dict(self._initial_account_state)}

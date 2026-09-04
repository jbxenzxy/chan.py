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
    - 限价单追价：SimNow 不支持市价单，用 ref_price 往成交方向让价后下单。
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
    def submit(self, action: str, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "") -> Order:
        if self._conn_error:
            return self._rejected(signal_key, side, action, volume, ref_price,
                                  note, self._conn_error)
        if self._api is None:
            return self._rejected(signal_key, side, action, volume, ref_price,
                                  note, "未连接")

        spec = self.spec
        # 限价追价：开仓/平仓都往成交方向让价（与 dry_run 的对齐语义一致）
        aligned = spec.align_entry(ref_price, side.sign) if action == "open" \
            else spec.align_exit(ref_price, side.sign)

        direction = _DIRECTION[side]
        offset = _OFFSET.get(action, "OPEN")

        # ===== P4 修复：下单前先快照持仓 =====
        # 下单后，tqsdk 的 position 端可能延迟反映；用 insert_order 之前的快照作为基线，
        # 等成交完成后验证 position 是否按预期增减，否则视为幽灵成交。
        #
        # P5 强化：取 baseline 前先 wait_update 2 次（让 CTP 任何"延迟到达的重发回报"
        # 在这里就同步完毕），避免 baseline 不准导致 P4 校验被绕过。
        side_key = "LONG" if side is Side.LONG else "SHORT"
        try:
            self._api.wait_update(deadline=time.time() + 0.5)
            self._api.wait_update(deadline=time.time() + 0.5)
            _before_snapshot = _position_total(self._api, self._trade_symbol, side_key)
        except Exception:
            _before_snapshot = 0
        expected_delta = int(volume) if action == "open" else -int(volume)

        # P0 修复：close 委托前等 tqsdk 持仓字段更新到 ≥ volume。
        # 原因：上一笔 open 成交后，tqsdk 端 position 对象需要 wait_update 若干帧才同步，
        # 引擎在收到下一个反向信号时立刻调 close，CTP 会因"平仓量超过持仓量"拒单（错误码）。
        # 兜底：等待 10s 还同步不上 → 拒单并保留持仓，不发空单。
        if action == "close":
            if not self._wait_position_ok(side, int(volume), timeout_s=10.0):
                return self._rejected(signal_key, side, action, volume, ref_price,
                                      note, "等待持仓更新超时（>10s），可能上游未同步")

        try:
            order = self._api.insert_order(symbol=self._trade_symbol,
                                           direction=direction, offset=offset,
                                           volume=int(volume), limit_price=aligned)
        except Exception as e:
            return self._rejected(signal_key, side, action, volume, ref_price,
                                  note, "下单失败: {}: {}".format(type(e).__name__, e))

        self._wait_finished(order, timeout_s=float(self.params.get("fill_timeout", 30.0)))

        # ===== P3 关键修复：必须 status == "FINISHED" 且 volume_left == 0 才是真成交 =====
        # 历史 bug：tqsdk 在撤单/超时/部分成交时也可能写入 trade_price，
        # 仅靠 trade_price 是否非 None 判断成交，会把"假成交"当真，引擎据此开
        # 出幻影持仓 → 每个反向/止盈/止损信号都触发"平仓量超过持仓量"死循环。
        is_fully_filled = (getattr(order, "status", "") == "FINISHED"
                           and getattr(order, "volume_left", None) == 0)
        filled = self._trade_price(order) if is_fully_filled else None

        # ===== P4 修复：在 P3 基础上加 position 端二次校验 =====
        # 历史 bug：simnow-000001 看似成交（status=FINISHED + volume_left=0 + trade_price=4544.8
        # + CTP 通知"成交"），但 get_position 始终返回 0 多，CTP 后续 20+ 笔 close 全被
        # 拒为"平仓量超过持仓量"。P3 只看 order 字段，挡不住 tqsdk position 滞后或缓存
        # 污染导致的"幽灵成交"。P4 做法：tqsdk 报 filled 后，再去看 position 是否从下单前
        # 快照（_before_snapshot）按预期变化；若 5s 内未到位 → 视为幽灵 → 拒收。
        # ===== P6 权威层：用 CTP 真实成交明细判定是否真成交 =====
        # 历史 bug（P4/P5 都挡不住）：tqsdk 在 insert_order 后会**乐观**把 position 缓存
        # +1/-1，即使 CTP 后续拒单也不回滚。P4/P5 拿 position 缓存做对照，等于拿"被
        # 自己骗过的账本"对账——v5 跑出 4 笔 OPEN "filled"，但 1.5 分钟后查 SimNow
        # 真实账户是 0 持仓，就是这么来的。
        #
        # P6 改用 order.trade_records：CTP 真正确认的成交回报明细，只有交易所撮合
        # 成功才会写入，不受本地缓存乐观更新影响。真成交必须三层同时成立：
        #   ① order.status == "FINISHED"           （P3）
        #   ② order.volume_left == 0               （P3）
        #   ③ sum(trade_records[*].volume) >= volume （P6 新增·权威）
        # 三层都过再走 P4/P5 的 position 端辅助校验（保留作交叉印证，但不再是唯一依据）。
        traded_volume = _traded_volume_from_records(order)
        traded_price = _traded_price_from_records(order)
        if is_fully_filled and traded_volume < int(volume):
            last_msg = getattr(order, "last_msg", "")
            self._note_reject(
                signal_key, note, order,
                "P6: CTP 成交明细只有 {} 手，不足委托 {} 手 (status=FINISHED, "
                "volume_left=0, trade_price={}, last_msg={})，判定为未成交".format(
                    traded_volume, int(volume),
                    getattr(order, "trade_price", None), last_msg))
            is_fully_filled = False
            filled = None

        # P6 权威成交价：优先取 CTP 成交明细的加权均价（比 order.trade_price 更可信，
        # 后者在部分成交/撤单场景下可能被写成首笔成交价而非均价）
        if is_fully_filled and traded_price:
            filled = traded_price

        # ===== P4/P5 降级为辅助层：只记录诊断，不再据此 reject =====
        # 历史 bug（v5 实测）：P4/P5 用 position 缓存做"必过"校验，会同时产生
        # 两种误判——
        #   · 误拒：CTP 真成交了但 position 缓存滞后 5s 才同步 → P4 超时 → 判幽灵
        #     → v5 那 21 笔 close 死循环就是这么来的（真持仓平不掉，反复重试）。
        #   · 误放：CTP 拒了但 position 缓存被乐观更新 +1 → P4 通过 → 判成交
        #     → v5 那 4 笔幻象 "filled" 就是这么来的（真实账户其实 0 持仓）。
        # position 缓存两头都不可靠，所以 P6 之后它只当**交叉印证/诊断信号**，
        # 不再有 reject 权。真成交的权威判定交给 P6 的 trade_records。
        verified = False
        if is_fully_filled:
            verified = _verify_position_delta(self._api, self._trade_symbol,
                                              side_key, baseline=_before_snapshot,
                                              expected_delta=expected_delta,
                                              timeout_s=5.0)
            if not verified:
                # 只告警，不推翻 P6 的判定
                self._note_position_lag(signal_key, action, side_key,
                                        _before_snapshot, expected_delta)

        status = "filled" if is_fully_filled else "rejected"
        if status == "rejected" and not getattr(order, "status", "") == "FINISHED":
            last_msg = getattr(order, "last_msg", "")
            self._note_reject(signal_key, note, order,
                              "未真正成交: status={}, volume_left={}, trade_price={}, last_msg={}".format(
                                  getattr(order, "status", ""),
                                  getattr(order, "volume_left", None),
                                  getattr(order, "trade_price", None),
                                  last_msg))

        o = Order(
            order_id="{}-{:06d}".format(self.name, next(self._seq)),
            signal_key=signal_key, symbol=self._trade_symbol, side=side,
            action=action, volume=int(volume), price=aligned,
            req_price=float(ref_price), filled_price=filled,
            status=status, created_at=now_cn(), broker=self.name, note=note,
            meta={"raw_order_id": str(getattr(order, "order_id", "")),
                  "direction": direction, "offset": offset,
                  "trade_price": filled,
                  "volume_left": getattr(order, "volume_left", None),
                  "last_msg": getattr(order, "last_msg", "")},
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
        pass

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

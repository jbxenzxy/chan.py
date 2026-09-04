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

        self._connect()

    def _cred(self, param_key: str, env_key: str) -> str:
        v = (self.params.get(param_key) or os.environ.get(env_key) or "").strip()
        return v

    # ---------------- 连接与合约映射 ----------------
    def _connect(self) -> None:
        from tqsdk import TqApi, TqAuth, TqAccount
        try:
            self._api = TqApi(TqAccount("simnow", self.sn_account, self.sn_password),
                              auth=TqAuth(self.tq_account, self.tq_password))
        except Exception as e:
            self._conn_error = "SimNow 登录失败: {}: {}".format(type(e).__name__, e)
            self._api = None
            return
        self._resolve_trade_symbol()

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

        try:
            order = self._api.insert_order(symbol=self._trade_symbol,
                                           direction=direction, offset=offset,
                                           volume=int(volume), limit_price=aligned)
        except Exception as e:
            return self._rejected(signal_key, side, action, volume, ref_price,
                                  note, "下单失败: {}: {}".format(type(e).__name__, e))

        self._wait_finished(order, timeout_s=float(self.params.get("fill_timeout", 30.0)))

        filled = self._trade_price(order)
        status = "filled" if filled is not None else "rejected"
        if filled is None:
            status = "rejected"
            last_msg = getattr(order, "last_msg", "")
            self._note_reject(signal_key, note, order, last_msg)

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

    def _note_reject(self, signal_key: str, note: str, order, last_msg: str) -> None:
        pass

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
                "conn_error": self._conn_error}

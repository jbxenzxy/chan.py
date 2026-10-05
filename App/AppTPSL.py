# -*- coding: utf-8 -*-
"""
App/AppTPSL.py —— 股票页「止盈止损」图上推演（设计文档 v1.5 §4；2026-09-27）
=========================================================================

把期货侧 LayeredExitPolicy（L1 结构 R / L2 ATR / L3 保本+跟踪）的**同一份口径**
组装到股票买卖点上，对历史 K 线做**纯图上推演**：不写盘、不进 state.db、
不接告警/toast 通道、不下单、不做持久化（设计 S8）。

已知边界（写进注释防后来者当 bug 修，S9 / R4）：
  - 股票 K 线是**前复权**价，与真实成交价不同；区间可能遇**停牌 / 涨跌停**，
    推演不建模这两者 ⇒ 保护价「可能不可成交」。这是推演的性质，不是缺陷。
  - 相位标签与保护价的一致性（原 R8）：``trailing_trigger_r`` 现为 **≥0 全域
    同一公式**（2026-09-28 统一，0 ⇒ 跟踪线贴最好极值，`Exit.check()` 的特例
    分支已删），「跟踪标签 + 旧价位」的不一致不再存在，无需校验。

依赖方向：AppChart（漏斗壳）→ 本模块 → Trading.Strategy / Trading.Infra（顺向，
App 在最上层，README 层表）。``Trading/Config.py`` 在导入期构造 DEFAULT_CONFIG
（.env 笔误会让 import 失败），故本模块对 Trading 的 import 一律**函数内惰性
+ 异常兜底**（照抄 ``App/AppTrader.py`` ``_load_cfg`` 的既有范式，R3）。

N1 收口（v1.4）：type 归一 + 同根取首 + ``console.warn`` 全部在前端完成，
本模块只校验前端送来的**单个** bsp 对象，不重做"多对象取舍"。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from App.AppErrors import AppError, BadRequestError
from App.AppUtils import STOCK_MARKETS

# 0123 类买卖点（Q3）：BSP_TYPE 的字面值（CEnum.T0/T1/T2/T3）
_TPSL_MAIN_TYPES = ("0", "1", "2", "3")
# type2str() 复合串的分隔符（BS_Point.type 为 list，逗号拼接）
_TYPE_SEP = ","
# K 线条数上限（R6：请求体与推演耗时保护；预热窗口 60 根之外的全序列）
_MAX_KLINES = 2000
# ATR 预热窗口（覆盖 atr_period+1 = 15 根，取整 60）
_WARMUP_BARS = 60
# 买卖点类型 → 终态业务名（reason 三值穷举，v1.5 §3.8）
_OUTCOME_MAP = {"sl": "sl_exit", "breakeven": "be_exit", "trailing": "trail_exit"}

# 股票侧出场参数显式覆盖（2026-09-27 用户拍板的股票口径 —— 只影响股票推演；
#   期货路径经 resolved_exit_params（品种档案）与全局默认，不经过本模块）。
#   数值即下方 dict 字面量；改口径 = 改此 dict + 同步 Test/test_stock_tpsl.py ⑩ 节。
_STOCK_EXIT_OVERRIDES = {"win_loss_ratio": 3.0, "trailing_trigger_r": 1.0}


def _exit_policy_cls():
    """惰性取 LayeredExitPolicy（R3：Trading 导入失败是服务端故障 → AppError 500）。"""
    try:
        from Trading.Strategy.Exit import LayeredExitPolicy
        return LayeredExitPolicy
    except Exception as e:  # noqa: BLE001 —— .env 笔误等导入期副作用必须收敛成明确报错
        raise AppError("止盈止损推演初始化失败（Trading 导入异常）：{}: {}".format(
            type(e).__name__, e)) from e


def _type_hits_0123(type_str: Any) -> bool:
    """买卖点类型命中判定（N1/P1-1）。

    ``type2str()`` 是逗号拼接的**复合串**（``BS_Point.type`` 为 list，同一根 K 线上
    可能合并多个类型标签，如 ``"1,11"``）。归一口径：任一分量去尾部 psab 变体后
    ∈ {0,1,2,3} 即命中 —— 与 ``CEnum.BSP_TYPE.main_type()`` 的
    ``value.rstrip('psab')`` 同语义（前端 ``app.js`` 侧有同构实现，两处口径必须
    同步修改）。
    """
    for seg in str(type_str or "").split(_TYPE_SEP):
        if seg.strip().rstrip("psab") in _TPSL_MAIN_TYPES:
            return True
    return False


def _phase_of(plan_params: Dict[str, Any]) -> str:
    """`_phase` 归一读法（P2-2）：初始 plan **没有** `_phase` 键（非空串），
    缺失或 `""` 都映射为 `sl`；breakeven / trailing 原样。"""
    return str((plan_params or {}).get("_phase") or "") or "sl"


def _bar_from(k: Dict[str, Any]):
    """前端 klines 条目 → Trading 的 Bar（from_dict 直吃同构 dict）。"""
    from Trading.Infra.Records import Bar
    return Bar.from_dict(k)


def compute_stock_tpsl(code: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """股票页「止盈止损」推演主入口（/api/stocks/{code}/tpsl，v1.5 §4.2/§4.3）。

    body: {code, freq, klines, bsp}（路由已把 path 上的 code 注入 body）。
    校验失败抛 ``BadRequestError``（FrontAPI 统一映射 400，不静默 200）；
    成功返回 §4.2 的响应结构（entry / r / a / b / atr / segments / current /
    exit / terminal / params）。出场参数用 ``_STOCK_EXIT_OVERRIDES`` 显式覆盖
    （股票侧口径，2026-09-27 拍板），不走模型默认/品种档案/.env。
    """
    from Trading.Infra.Instrument import Instrument
    from Trading.Infra.Records import Position, Signal

    freq = str(body.get("freq") or "d")
    klines: List[Dict[str, Any]] = body.get("klines") or []
    bsp: Dict[str, Any] = body.get("bsp") or {}

    # ── 校验（客户端输入问题 → 400）─────────────────────────────────
    #    市场前缀取自 `App.AppUtils.STOCK_MARKETS`（单一事实源，与回测入口
    #    `AppBacktest._split_code` 同一份）：页面能打开的标的，两个入口都必须放行。
    if not code or "." in code or code[:2].lower() not in STOCK_MARKETS:
        raise BadRequestError("非股票代码: {!r}".format(code))
    if not isinstance(klines, list) or not klines:
        raise BadRequestError("klines 为空")
    if len(klines) > _MAX_KLINES:
        raise BadRequestError("klines 超上限（{} > {}）".format(len(klines), _MAX_KLINES))
    if not isinstance(bsp, dict):
        raise BadRequestError("bsp 必须是对象")
    for key in ("date", "type", "is_buy", "price", "fractal_low", "fractal_high"):
        if key not in bsp:
            raise BadRequestError("bsp 缺字段: {}".format(key))
    if not isinstance(bsp["is_buy"], bool):
        raise BadRequestError("bsp.is_buy 缺失或非布尔")
    if not _type_hits_0123(bsp["type"]):
        raise BadRequestError("买卖点类型不在 0123 类: {!r}".format(bsp["type"]))
    entry_idx = next((i for i, k in enumerate(klines)
                      if str(k.get("date") or "") == str(bsp["date"])), None)
    if entry_idx is None:
        raise BadRequestError("bsp.date 不在 klines 中: {!r}".format(bsp["date"]))
    # Signal.from_bsp 的 fail-fast 需要 timestamp（幂等键）；前端 chartData.bsps
    # 自带该键（AppEngine 输出），缺失时从命中的 K 线确定性回填（同源同值）。
    bsp = dict(bsp)
    bsp.setdefault("timestamp", klines[entry_idx].get("timestamp"))
    for k in klines:
        if not all(key in k for key in ("date", "timestamp", "open", "high", "low", "close")):
            raise BadRequestError("klines 条目缺 date/timestamp/open/high/low/close 之一")

    # ── 组装（v1.5 §4.4；全部为 Trading 既有口径，零重写）────────────
    LayeredExitPolicy = _exit_policy_cls()
    sig = Signal.from_bsp(bsp, symbol=code, freq=freq)
    inst = Instrument(product=None)  # 无品种档案 ⇒ round_price 原样返回，Q6 不做 tick 对齐
    pol = LayeredExitPolicy(dict(_STOCK_EXIT_OVERRIDES))  # 股票侧显式口径（见 _STOCK_EXIT_OVERRIDES），Q8 不跟 .env/品种档案

    entry_price = float(bsp["price"])
    if abs(float(klines[entry_idx].get("close") or 0.0) - entry_price) > 1e-9:
        raise BadRequestError("bsp.price 与该根 K 线收盘价不一致（需求⑴：入场价=收盘价）")

    # 预热（含入场根；Q5 后 ATR 缓冲跨日连续，无跨日清空可绕）
    for k in klines[max(0, entry_idx - _WARMUP_BARS):entry_idx + 1]:
        pol.on_bar(_bar_from(k), inst)
    # 计划：不传 anchor ⇒ 风控锚 = 入场收盘价（Exit.py base = anchor or entry_price）
    #   第三参实名 state，按位置传参（P1-2）
    plan = pol.plan(sig, entry_price, inst)

    pos = Position(symbol=code, side=sig.side, volume=1, entry_price=entry_price,
                   entry_at="", entry_bar_ts=int(klines[entry_idx].get("timestamp") or 0),
                   signal_key=sig.key, open_order_id="", exit_plan=plan)

    R = float(plan.params.get("R") or 0.0)
    atr_at_entry = pol.current_atr()
    a_val = (max(entry_price - float(bsp["fractal_low"]), 0.0) if bsp["is_buy"]
             else max(float(bsp["fractal_high"]) - entry_price, 0.0))
    b_val = (float(pol.atr_sl_multiple) * atr_at_entry) if atr_at_entry is not None else 0.0

    # ── 逐根推演（入场那根不判出场，与 Engine 同口径；v1.5 §4.3 步 9）──
    segments: List[Dict[str, Any]] = [{
        "phase": _phase_of(plan.params),
        "price": plan.stop_price,
        "start_date": klines[entry_idx]["date"],
        "end_date": klines[entry_idx]["date"],
    }]
    last_date = str(klines[-1]["date"])
    prev_date = str(klines[entry_idx]["date"])
    exit_info: Optional[Dict[str, Any]] = None
    terminal: Optional[Dict[str, Any]] = None

    for k in klines[entry_idx + 1:]:
        bar = _bar_from(k)
        pol.on_bar(bar, inst)
        chk = pol.check(pos, bar, inst)
        if chk is None:
            prev_date = str(k["date"])
            segments[-1]["end_date"] = prev_date
            continue
        if chk.plan is not None:
            # 先抬价：段切换（「因进保本/跟踪而离场」的因果链必须先落段再判离场）
            segments[-1]["end_date"] = prev_date
            segments.append({"phase": _phase_of(chk.plan.params),
                             "price": chk.plan.stop_price,
                             "start_date": str(k["date"]),
                             "end_date": str(k["date"])})
        if not chk.only_update:
            # 终态：收盘价跌破当前保护价（离场后不再推演，已无持仓）
            reason = str(chk.reason)
            outcome = _OUTCOME_MAP.get(reason, "sl_exit")
            exit_info = {"date": str(k["date"]), "price": chk.price,
                         "fill": chk.fill_price, "reason": reason, "outcome": outcome,
                         "r_multiple": (round((chk.fill_price - entry_price) * sig.side.value / R, 4)
                                        if R > 0 else None)}
            # 终态段右端 = 触发 bar（2026-09-27 用户六改⑴：不再延到序列末根，
            #   否则看不出止盈/止损发生在哪根 K 线）
            segments[-1]["end_date"] = str(k["date"])
            segments[-1]["terminal"] = True
            terminal = {"phase": _phase_of({"_phase": reason}), "price": chk.price,
                        "outcome": outcome, "exit_date": str(k["date"]),
                        "end_date": str(k["date"])}
            break
        pos.exit_plan = chk.plan
        prev_date = str(k["date"])
        segments[-1]["end_date"] = prev_date

    # ── 响应（v1.5 §4.2）──────────────────────────────────────────
    return {
        "ok": True,
        "entry": {"date": str(bsp["date"]), "price": entry_price,
                  "side": "long" if bsp["is_buy"] else "short"},
        "r": round(R, 6) if R > 0 else None,
        "a": round(a_val, 6),
        "b": round(b_val, 6),
        "atr": (round(atr_at_entry, 6) if atr_at_entry is not None else None),
        "segments": segments,
        "current": (None if terminal else
                    {"phase": segments[-1]["phase"], "price": segments[-1]["price"],
                     "as_of": last_date}),
        "exit": exit_info,
        "terminal": terminal,
        "params": {"win_loss_ratio": float(pol.p.win_loss_ratio),
                   "breakeven_trigger_r": float(pol.p.breakeven_trigger_r),
                   "breakeven_buffer_r": float(pol.p.breakeven_buffer_r),
                   "trailing_trigger_r": float(pol.p.trailing_trigger_r),
                   "atr_sl_multiple": float(pol.p.atr_sl_multiple),
                   "atr_period": int(pol.p.atr_period)},
    }

# -*- coding: utf-8 -*-
"""
股票侧出场参数与交易成本常量（Backtest/ExitParams.py）
=====================================================
本模块是回测侧**股票出场口径与费率的唯一事实源（SSOT）**。

为什么要在 `Backtest/` 自持一份
---------------------------------------------------------------------
设计文档 §5.9（R31）：`App/AppTPSL.py:45` 的 `_STOCK_EXIT_OVERRIDES`
是全仓**唯一出处**，而 TPSL 后续会删 ⇒ 不抄一份，TPSL 一删股票出场
口径的 SSOT 就没了（这数不走 `.env`、不走品种档案，丢了只能翻 git 历史）。

⇒ `STOCK_EXIT_PARAMS` 就是那份"抄件"，但它同时是**新 SSOT**：
TPSL 删除后它就是唯一来源。

迁移期双源护栏（§5.9 #1，v1.15 用户裁定走"护栏式"）
---------------------------------------------------------------------
`_STOCK_EXIT_OVERRIDES` 要等 TPSL **被删**才消失 ⇒ 中间期**同一口径有两份常量**。
两份相等由 `Backtest/Test/test_bt03_exit_params_contract.py` 钉住
（AppTPSL 侧符号存在则必须相等；不存在则 skip —— TPSL 删除后该测试自动退役）。
**不采用"让 AppTPSL 反向 import 本模块"的根治式**：那会改动 `App/` 一行，
与 §5.1「不碰 `App/`」冲突。

交易成本（设计文档 §5.4b 费率表 + §5.4d-quater 4.2 精确式）
---------------------------------------------------------------------
    k  券商佣金（万一）—— **已包含**交易规费与过户费    0.0001   买 + 卖
    m  单笔最低佣金                                     5.00 元  买 + 卖
    s  印花税                                          0.0005   **仅卖出**
    t  过户费                                          0.0      （已并入 k，保留字段）

取数日期 2026-10-05；印花税以官方公告为准（2023-08-28 起减半）。
"""
from __future__ import annotations

import math
from typing import Dict, Tuple

# ════════════════════════════════════════════════════════════════════
# ① 股票侧出场参数（设计文档 §3.2；2026-09-27 用户拍板的口径）
# ════════════════════════════════════════════════════════════════════
# 与 `App/AppTPSL.py:45` 的 `_STOCK_EXIT_OVERRIDES` **逐键相等**
# （由 Test/test_bt03_exit_params_contract.py 钉住）。
#
# 为什么只有两个键：`LayeredExitPolicy(dict)` 构造时经
# `ExitPolicyParams(**params)` 校验（`extra="forbid"`），未列出的键走模型默认
# （breakeven_trigger_r=1.0 / breakeven_buffer_r=0.5 / atr_period=14 /
#  atr_sl_multiple=2.0 / use_atr=True）。多写一个拼错的键会**当场报错**，
# 这是刻意的（比静默用默认安全）。
STOCK_EXIT_PARAMS: Dict[str, float] = {
    "win_loss_ratio": 3.0,      # L3 启动阈值（= 名义止盈倍数）
    "trailing_trigger_r": 1.0,  # L3 跟踪缓冲距离（R 倍数）
}

# ════════════════════════════════════════════════════════════════════
# ② 费率常量（设计文档 §5.4b）
# ════════════════════════════════════════════════════════════════════
COMMISSION_RATE = 0.0001        # k
MIN_COMMISSION_CASH = 5.0       # m
STAMP_DUTY_RATE = 0.0005        # s（仅卖出）
TRANSFER_FEE_RATE = 0.0         # t（已并入 k；换券商不再包过户费时加回公式）

# 名义双边费率 c_nom = 2k + s = 0.070%（**名义**口径：两腿都以单边成交额 A 为基数）。
# 精确费率按方向不同（§5.4d-quater 4.2）：多头 c = 2k+s+(k+s)g、空头 c = 2k+s−k·g。
NOMINAL_COST_RATE = 2 * COMMISSION_RATE + STAMP_DUTY_RATE

# ════════════════════════════════════════════════════════════════════
# ③ 目标成交额与最小申报单位（设计文档 §5.4d-quater）
# ════════════════════════════════════════════════════════════════════
# `target_amount = m / k` —— **不是偏好值，是数学必然**：
# 5 元最低佣金在 `k·A ≥ m` 时才不被抬起，临界成交额恰好是 `m / k`。
# 写成推导式而非硬编码 50000：券商把最低佣金改成 10 元、或费率改成万 1.2 时，
# 这个临界值自动跟着变，不会在某天变成一个错误答案。
TARGET_AMOUNT = MIN_COMMISSION_CASH / COMMISSION_RATE     # 50 000.0 元

# 板块申报单位（设计文档 §5.4d-quater 第 5 节）：
#   沪深主板 / 创业板  100 股起，100 股递增
#   科创板             200 股起，超过部分允许 1 股递增（此处取 step=100 的**保守超集**）
#   北交所             100 股起，允许 1 股递增
DEFAULT_LOT: Tuple[int, int] = (100, 100)          # (min_lot, step)


def lot_rule(code: str) -> Tuple[int, int]:
    """由股票代码判板块 → `(min_lot, step)`（最小申报单位与递增步长）。

    code 形如 `sh600519` / `sz300015` / `sz002190` / `bj430047`。
    """
    c = str(code or "").lower()
    market, num = c[:2], c[2:]
    if num[:3] in ("688", "689"):                  # 科创板
        return (200, 100)
    if market == "bj" or num[:2] in ("43", "83", "87", "88", "92"):  # 北交所
        return (100, 1)
    return DEFAULT_LOT                              # 沪深主板 / 创业板


def ceil_step(value: float, step: int) -> int:
    """向上取整到 `step` 的整数倍（`ceil` 而非 `floor`：向下取整会让中间价区
    跌破 50 000 又掉回 5 元兜底区）。"""
    if step <= 1:
        return int(math.ceil(value - 1e-9))
    return int(math.ceil(value / step - 1e-9)) * step


def shares_for(price: float, code: str, target_amount: float = TARGET_AMOUNT,
               is_index: bool = False) -> int:
    """`shares = ceil_step(max(min_lot, target_amount / price))`（设计文档 §5.4d-quater 第 4 节）。

    高价端由 `min_lot` 兜底（茅台 100 股 = 12.6 万，实际放大 2.5 倍 —— 报告须披露），
    低价端由 `target_amount` 抬到"免疫 5 元兜底"的临界之上。

    本金视为**充足**（v1.11 定案，`skip` 分支已整体删除）⇒ 本函数没有"买不起"分支。

    `is_index=True`（2026-10-06 用户拍板「指数当个股」）：**跳过 `lot_rule` 的代码前缀
    判板块**，一律用 `DEFAULT_LOT`（1 手 = 100 股、100 股递增）。理由：指数 1 点 = 1 元
    （点位即"股价"），但 `lot_rule` 的北交所分支含 `"88"` 前缀 ⇒ `sh880491` 这类板块指数
    会被误判成 `step=1`，点位 < 500 时出现 167 股这类碎股。**不要**改成"在 lot_rule 里
    加指数前缀规则"——`sz000001` 是平安银行，前缀启发式必误伤；指数判定只能由调用方
    （`Runner` 已注入 `is_index`）显式传入。
    """
    min_lot, step = DEFAULT_LOT if is_index else lot_rule(code)
    p = float(price)
    if p <= 0:
        return int(min_lot)
    return ceil_step(max(float(min_lot), float(target_amount) / p), step)


def round_trip_cost(entry_price: float, exit_price: float, side_sign: int,
                    shares: int) -> float:
    """一次往返（开 + 平）的**双边合计成本，元**（设计文档 §5.4b ③，精确式）。

    多头：买在开仓端、卖在平仓端 ⇒ 卖出腿成交额 = `shares × exit_price`（含印花税）。
    空头：卖在开仓端、买在平仓端 ⇒ 印花税落在**开仓端**，买入平仓腿无印花税。

    `max(k·A, m)` 逐腿取，因此 5 元兜底**按腿**判定 —— 与"名义费率恒 0.070%"
    是两回事（名义费率只在本金充足且成交额 ≥ A* 时成立）。
    """
    n = int(shares)
    sell_price = float(exit_price) if side_sign > 0 else float(entry_price)
    sell_notional = n * sell_price
    # 卖出腿：佣金（含规费与过户费）+ 印花税
    sell_cost = max(COMMISSION_RATE * sell_notional, MIN_COMMISSION_CASH) \
        + STAMP_DUTY_RATE * sell_notional
    # 买入腿：佣金（含规费与过户费），无印花税
    buy_notional = n * (float(entry_price) if side_sign > 0 else float(exit_price))
    buy_cost = max(COMMISSION_RATE * buy_notional, MIN_COMMISSION_CASH)
    return buy_cost + sell_cost

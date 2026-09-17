# -*- coding: utf-8 -*-
"""风控层（精简后）：交割月护栏收敛在 RiskConfig。

二次精简：删除整条"仓位管理 PositionSizing / SizingConfig"通道
（含固定手数、动态 capital_pct / atr_risk 算法）。
更早已删除：风控五道硬闸门 RiskGate、资金闸门（initial_cash）。
** 本层不再持有任何手数旋钮** —— 原 `RiskConfig.max_volume` 已删除，
单笔手数（每个买卖点一笔挂 N 手）的**唯一来源 = 品种执行策略表第 3 列**
（`Infra/Product.py` 的 `EXEC_POLICY[code].lots_per_order`，引擎经
`Engine.lots_per_order` 只读不推）；1..20 越界校验随字段迁到
`ExecPolicy.__post_init__`。
"""

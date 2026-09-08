# -*- coding: utf-8 -*-
"""风控层（精简后）：开仓手数 / 持仓笔数 / 补开开关，全部收敛在 RiskConfig。

2026-09-08 二次精简：删除整条"仓位管理 PositionSizing / SizingConfig"通道
（含固定手数、动态 capital_pct / atr_risk 算法）。
开仓手数由引擎直接取 `RiskConfig.max_volume`（每个买卖点一笔挂 N 手）。
更早已删除：风控五道硬闸门 RiskGate、资金闸门（initial_cash）。
"""

# -*- coding: utf-8 -*-
"""
缠论信号 → 交易执行 网关（自动下单）
====================================
逻辑分层与物理目录一一对应（依赖方向严格单向，Engine 是唯一枢纽）：

    Source/    ① 信号源       SSE 实时 / Replay 回放
    Strategy/  ③ 策略层       EntryPolicy 入场过滤 / LayeredExitPolicy 分层出场（L1-L3）
    Risk/      ④ 风控层       只余交割月护栏（收敛于 RiskConfig）；**不持有手数旋钮**
    Engine/    ⑤ 执行层       Engine 四态状态机编排 / Reconcile 对账+F1 / PositionBook 账本
    Broker/    ⑥ Broker 适配  DryRun 模拟 / SimNow CTP 真实通道
    Infra/     横切基础设施   Records / Clock / Period / Product / StateDB /
                             EventLog / Instrument / TradeStats

依赖规则：Infra 不 import 任何上层；生产代码里没有任何模块反向 import Engine（装配只在 main.py）。
"""

__version__ = "0.2.0"

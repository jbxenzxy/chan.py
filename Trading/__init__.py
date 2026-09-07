# -*- coding: utf-8 -*-
"""
缠论信号 → 交易执行 网关（自动下单）
====================================
逻辑分层与物理目录一一对应（依赖方向严格单向，Engine 是唯一枢纽）：

    Source/    ① 信号源       SSE 实时 / Replay 回放
    Strategy/  ③ 策略层       Entry 入场过滤 / Exit 出场策略（可插拔注册）
    Risk/      ④ 风控闸门     RiskGate 五道硬闸门 / PositionSizing 仓位与资金手数
    Engine/    ⑤ 执行层       Engine 四态状态机编排 / Reconcile 对账+F1 / PositionBook 账本
    Broker/    ⑥ Broker 适配  DryRun 模拟 / SimNow CTP 真实通道
    Infra/     横切基础设施   Types / Config / EventLog / Store / InstrumentSpec

依赖规则：Engine import 全部模块；没有任何模块反向 import Engine。
"""

__version__ = "0.2.0"

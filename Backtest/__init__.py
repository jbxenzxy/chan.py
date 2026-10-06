# -*- coding: utf-8 -*-
"""
胜率回测（Backtest/）
=====================
离线批跑：在**已加载的 K 线序列**上，用 Trading 侧的出场零件跑一遍
「信号 → 两态状态机 → 分层出场」，产出**一只票的完整交易清单**。

设计文档：`Docs/股票回测功能_设计兼交接文档_v2.2.md`（§5.1 目录与分层、
§5.2 核心循环、§5.3 两态状态机、§5.4 指标、§5.9 对 AppTPSL 的零依赖约束）。
⚠ 按版本策略（仓库只留当前版）**只写当前版**：换版时本行随之改指，别写"最新"这类
无法机械校验的说法（旧版本文档会被从仓库删除，指过去就是死链）。

分层（与 `Trading/` 同一条规矩：严格单向、无反向 import）
---------------------------------------------------------------------
    Backtest/  →  Chan / Common / DataAPI / BuySellPoint   （只读消费信号，不参与生成）
               →  Trading.Strategy.Exit / Trading.Infra    （策略零件的唯一来源）
    FrontAPI / Frontend  →  Backtest/   （允许顺向依赖，含常量 / 工厂）

硬约束（设计文档 §5.9 R31，由 `Test/test_bt04_no_app_import.py` 用 AST 钉死）
---------------------------------------------------------------------
    本包**生产代码**不得出现 `import App.` / `from App`。
    `Backtest/Test/` 的比对测试**豁免**（契约测试必须 import App 才做得了）
    —— 写法照抄仓内既有惯例（`Test/test_phase4_guards.py` 按生产模块划范围）。

口径边界（务必先读，否则数字对不上）
---------------------------------------------------------------------
    · 回测口径 = **页面单窗态**（设计文档 §0.3 / §4.4）：单级别 `CChan`、
      不打 `_stocks_dual_sub_freq` ⇒ 区间套门走 `BSPointList` 的兜底放行。
    · 区间 `[L, R]` 由调用方解析好传入（＝ P0-2 方案 B）：本包**只保证
      "给什么区间就测什么区间"**，不解析 `STOCKS_LOOKBACK_CONFIG` / `FULL_DATA_MODE`。
    · 缠论配置由本包自持 `Runner.default_chan_config()`，并由
      `Test/test_bt02_config_contract.py` 钉住"与 `App.AppUtils._make_chan_config()`
      逐字段相等"。
    · 不套 T+1、不建模涨跌停 / 停牌、前复权价 —— 三条偏离均须在报告披露。
"""

__version__ = "0.1.0"

# 配置层归一 · Step 1 交付说明（Trading/ 内部）

日期：2026-09-07
目标：删掉 config.json / config_example.json 这条配置路径，
      Trading/Config.py 成为自动下单所有配置的**唯一总入口**；组件走严格模式（不兜底）。

## 一、需要手工删除的文件（zip 无法表达删除）
- `Trading/Infra/Config.py`
- `Trading/config_example.json`

## 二、本包包含（直接覆盖同名文件）
- 新增 `Trading/Config.py`（配置唯一入口：pydantic 模型 + 环境变量/根 .env 覆盖）
- 改：Infra/InstrumentSpec.py、Risk/PositionSizing.py、Risk/RiskGate.py、
      Strategy/Exit.py、Strategy/Entry.py、Strategy/Base.py（文档串）、
      Broker/SimNow.py、Broker/Base.py（文档串）、Engine/Engine.py、main.py、
      README.md、Test/ 下 18 个测试、仓库根 .gitignore

## 三、优先级
    命令行参数（main.py） > 环境变量/仓库根 .env（TRADING_ 前缀） > Trading/Config.py 模型字段默认值
例：
    TRADING_SOURCE__FREQ=15s
    TRADING_RISK__MAX_VOLUME=5
    TRADING_SIZING__UNLOCK_NO_NEW_OPEN=false

## 四、严格模式（用户拍板：不要兜底）
- 所有配置段都是 extra="forbid" 的 pydantic 模型：未知键立即报错，缺键用模型默认值。
- PositionSizer 只接受 SizingConfig，传裸 dict → TypeError。
- ExitPolicy / EntryPolicy 参数由 ExitParamsConfig / EntryParamsConfig / DefaultExitParamsConfig 校验。
- SimNow 的 broker_params 由 BrokerParamsConfig 补齐并校验（可只传要覆盖的键）。
- Engine 里 `getattr(sizer, "unlock_no_new_open", False)` 的兜底已删除。

## 五、凭据
账号密码**不在** Config.py（它入库）。只走环境变量或仓库根 .env：
SN_ACCOUNT / SN_PASSWORD / TQ_ACCOUNT / TQ_PASSWORD / LIVE_ACCOUNT / LIVE_PASSWORD。
原有 config.json 里的凭据请迁到 .env 或系统环境变量。

## 六、严格模式暴露的历史问题（已顺手处理）
- 测试里给 DefaultExitPolicy 传的 `stop_loss_points` / `stop_distance_points` /
  `tp_distance_points` 都是**错键**（策略实际读 stop_points / take_profit_points），
  此前被静默忽略。已移除，行为与改动前完全一致。
- sizing.mode / equity_source 的非法值此前静默退化成 fixed / available，
  现在构造期直接 ValueError。

## 七、回归结果（沙盒干净目录复验）
19 个测试脚本全通过；test_scenario_e2_verify.py.py 的 B_replay_smoke 失败是
**基线既有问题**（回放目录路径写死仓库根，数据在 Trading/replay_data），与本改动无关。

## 八、Step 2（未做）
App/AppTrader.py 仍在读 config.json（实盘闸门、source 默认值、--config 参数），
下一步改为 import Trading.Config；届时 test_p20 里写临时 config.json 的用例也要一起改。

# Trading/Config.py 归一化 —— Step 2 交付包

**改动范围**：4 个文件（App/AppTrader.py、App/AppOrch.py、FrontAPI.py、Trading/Test/test_p20_phase_i1.py）
**配套**：先合 Step 1（Trading_config_unified_step1.zip，含 Trading/Config.py），再合本包

## 改动摘要

App 层（AppTrader + AppOrch + FrontAPI 注释）去掉 config.json 这条路径，改读唯一入口 Trading/Config.py。
子进程命令行不再传 --config（实际配置双方走同一份 Config.py 共识）。

### App/AppTrader.py
- _DEFAULT_CFG、_load_cfg JSON 加载、--config 命令行参数、_TraderProc.cfg_path 字段全部删除
- 新增模块级常量 _DEFAULT_OUT = Trading/State
- _load_cfg() 改 default_config()（Trading.Config）—— 延迟 import（配置写错不拖垮整个后端服务，只在点开启时报 AppError）
- _check_live_gate(cfg, broker) 接受 GatewayConfig，直接读 cfg.broker_params.tq_market / confirm_live_trading
- start() 签名去掉 cfg_path；信号源从 cfg.source.{symbol,freq,sse_base} 取（SourceConfig 默认值就是单一事实源）
- 相对 state_dir 基准固定为 Trading/（等价于原"以配置文件所在目录为基准"，因为 config.json 原本就在 Trading/ 下）
- _TraderProc 不再带 cfg_path；to_dict / status 同步去该字段
- cmd 不再传 --config

### App/AppOrch.py
- call_trader_start 去掉 cfg_path 参数

### FrontAPI.py
- 注释"走 config.json 的 source 段" 改为"走 Trading/Config.py 的 source 段"

### Trading/Test/test_p20_phase_i1.py
- [7] cfg_with 返回 GatewayConfig 模型（不再是裸 dict）
- [9]/[9i]/[9h] 不再写 config.json，改为 monkeypatch AppTrader._load_cfg 注入 GatewayConfig
  - finally 恢复用 AT.AppTrader._load_cfg = staticmethod(orig_load_cfg)（静态方法恢复的正确姿势）
- [10] _TraderProc 构造去 cfg_path
- [11] "无 config.json" 改为"配置加载失败"（真实失败路径：注入非法 env TRADING_RISK__MAX_VOLUME=abc → default_config() 抛错 → AppTrader._load_cfg 包成 AppError），验证延迟 import 包装
- [12] monkeypatch AT._TG_ROOT 到 tmp 下假家，验证相对 state_dir 基准 = Trading/（不再受后端 CWD 影响）；CWD 恢复
- 头部 docstring 同步去 config.json 提及

## 合并提示

1. 先合 Step 1（Trading_config_unified_step1.zip），确认 Trading/Config.py + 各组件到位、测试绿
2. 再合本包 4 个文件
3. 跑全套测试：python Trading/Test/test_p20_phase_i1.py（应 82/82 通过）以及 run_tests.py

## 设计取舍：为什么 Trading.Config 在 App 层是延迟 import？

Trading/Config.py 在模块级就构造 DEFAULT_CONFIG = default_config().model_dump() 快照。配置非法时
import 阶段即抛 ValidationError。若 AppTrader 顶层 import，会导致：
- 任何一个 .env 笔误（如 TRADING_RISK__MAX_VOLUME=abc）→ 后端服务整体起不来
- 行情页、配置查看、自动下单开关都挂

改为延迟 import 后：
- 行情服务正常启动
- 用户点"开启自动下单"时才触发 _load_cfg() → 抛 AppError → 前端 4xx 提示 + gateway.log 留痕
- 故障面收敛到自动下单功能本身

子进程 Trading/main.py 仍是启动期 fail-fast（它必须读配置才能跑）。

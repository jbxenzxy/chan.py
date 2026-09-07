# 实施方案：trader_gateway → Trading 按层重构（终版 v4）

> 状态：**待确认 ⑹ 对账合入方式 + tests 文件名口径后开工**
> v4 变更：吸收用户 ⑴-⑼ 命名调整；tg/ 层消除 + Trading/ 包已锁定

---

## 一、决策点落定记录

| # | 决策 | 结论 |
|---|---|---|
| ⑴ | tg/ 包层 | **消掉**，Trading/ 即 Python 包（与原项目 App/ 包同风格）✅ |
| ⑵ | 外层名 | `Trading/` ✅ |
| ⑶ | Source 文件名 | `SSE.py / Replay.py / Base.py`（去概念前缀重复）✅ |
| ⑷ | Strategy 拆分 | `Base.py + Entry.py + Exit.py`，ExampleTrailing/LayeredExit 合入 Exit.py ✅（见下） |
| ⑸ | CapitalGate | 合入 `PositionSizing.py`（同属"算手数"职责）✅ |
| ⑹ | Engine 文件名 | `Engine.py` ✅；**Reconcile 合入方式待你拍板**（见下） |
| ⑺ | Broker 文件名 | `Base.py / DryRun.py / SimNow.py` ✅ |
| ⑻ | Infra 文件名 | `Config.py / Types.py / EventLog.py / Store.py / InstrumentSpec.py` ✅ |
| ⑼ | tests/tools | 目录 PascalCase 归一 ✅；tools 文件归一；**tests 文件名建议保持**（见下） |

## 二、终版目录树（v4）

```
Trading/                        # 自动下单网关（Python 包，替代 trader_gateway/）
├── __init__.py                 # （分层说明 + 依赖方向 docstring）
├── run_gateway.py              # （子进程入口；保持小写=对齐原项目入口 main.py 风格）
├── config.example.json         # （配置样例）
├── config_trailing.json        # （移动止损示例配置）
├── README.md                   # （使用说明；路径描述同步更新）
│
├── Source/                     # ① 信号源层
│   ├── SSE.py                  # （SSE 实时信号源：接 FrontAPI 推送的买卖点流）
│   ├── Replay.py               # （历史回放信号源：读 jsonl 重放，测试/复盘用）
│   └── Base.py                 # （信号源抽象基类：统一 start/stop/迭代接口）
│
├── Strategy/                   # ③ 策略层（可插拔）
│   ├── Base.py                 # （策略接口基类：EntryPolicy/ExitPolicy 抽象）
│   ├── Entry.py                # （入场策略：振幅/止损距离三道过滤，DefaultEntryPolicy）
│   └── Exit.py                 # （出场策略合集：默认止盈止损/时间 + 分层离场L1-L4
│                               #     + 移动止损示例[教学注释：三步换策略]）
│
├── Risk/                       # ④ 风控闸门层
│   ├── RiskGate.py             # （五道硬闸门：时段/尾盘/单笔手数/日笔数/日净亏）
│   └── PositionSizing.py       # （仓位与资金手数计算：三模式算法 + 资金闸门上限X）
│
├── Engine/                     # ⑤ 执行层
│   ├── Engine.py               # （交易引擎主体：四态状态机+开仓/解锁/平仓/锁仓编排）
│   ├── Reconcile.py            # （对账+F1卡单监控：账本vs真实持仓比对、解锁超时兜底）
│   └── PositionBook.py         # （持仓账本：一笔持仓=一条记录，含锁仓状态）
│
├── Broker/                     # ⑥ Broker 适配层
│   ├── Base.py                 # （Broker 抽象接口+注册表：下单/撤单/查持仓六方法）
│   ├── DryRun.py               # （本地模拟撮合：不连网、必成交，测试用）
│   └── SimNow.py               # （CTP 真实通道：SimNow 仿真/创元实盘，全FOK+追价）
│
├── Infra/                      # 横切·基础设施层
│   ├── Types.py                # （公共数据结构：Bar/Signal/Order/Position/Side/EntryMode）
│   ├── Config.py               # （配置模型 dataclass + DEFAULT_CONFIG 全部默认值）
│   ├── EventLog.py             # （事件日志：全决策点 jsonl 追加落盘）
│   ├── Store.py                # （sqlite 持久化：持仓快照/信号幂等键/日统计/解锁在途）
│   └── InstrumentSpec.py       # （品种规格：交易时段/价格对齐/保证金/费率）
│
├── Test/                       # 测试（tests/ → Test/）
│   └── test_p*.py ×20          # （文件名保持：phase 编号测试体系，原项目 Test/ 内
│                               #     文件也是小写风格，非概念模块不强行 PascalCase）
└── Tools/                      # 诊断工具（tools/ → Tools/，零依赖包内代码）
    ├── SimNow/                 #   （SimNow 仿真工具）
    │   ├── SimNowDiag.py       #   （连接诊断）
    │   ├── SimNowProbe.py      #   （盘前探测）
    │   ├── ProbeAfterClose.py  #   （收盘后探测）
    │   ├── SimNowForceClose.py #   （强制平仓）
    │   └── SimNowSmoke.py      #   （冒烟交易）
    ├── M0Recorder/             #   （信号录像）
    │   ├── SignalRecorder.py   #   （信号录制器）
    │   └── Analyze.py          #   （录像分析）
    ├── GenReplayData.py        #   （回放数据生成）
    └── MakeDemoData.py         #   （演示数据生成）
```

## 三、两处需要你拍板的点

**A. ⑹ Reconcile.py 的合入方式（推荐独立）**
不建议合入 `PositionBook.py`——理由是依赖方向：PositionBook 是**纯数据结构**（账本，不碰 broker/store/events）；对账逻辑需要**引擎全上下文**（挂修正单要摸 broker、写事件、查日统计），合入会让"账本"文件反向依赖 Broker 层，破坏"Engine 是唯一枢纽"的单向依赖。
- **选项①（推荐）**：Reconcile.py 保持独立 —— Engine/ 三文件，职责最清晰
- **选项②**：并入 Engine.py（Mixin 与主体同文件）—— Engine/ 只剩两文件，但 Engine.py 回到约 1430 行

**B. ⑼ tests 文件名口径（建议保持 test_p*.py）**
原项目 `Test/` 目录内文件本身就是小写下划线风格（`func_map_analyzer.py`、`comparator.py`），并非 PascalCase——测试文件不是"概念模块"。20 个 `test_p*.py` 是 phase 编号测试体系，改名会让测试与文档、历史记录脱钩。若你坚持全部 PascalCase（`TestP9Sizing.py`）也可以做，纯机械。

## 四、分批落地步骤（每批编译检查 + 回归 + git commit 锚点）

- **批次 A1 结构改名**：git mv `trader_gateway/`→`Trading/`、消 `tg/` 层（包内容上提）；改 AppTrader._TG_ROOT、FrontAPI 注释、tests sys.path；import 前缀 `tg.`→`Trading.` 一次替换（~65 处）；回归 + commit
- **批次 A2 命名归一**：目录单数化（Source/Broker）+ 全部文件 PascalCase 重命名（含 Strategy 拆分 Entry/Exit、tools 归一）+ 各 `__init__.py` 与 import 路径同步；回归 + commit
- **批次 A3 横切/风控归位**：建 `Infra/`（五文件移入）+ `Risk/`（gate/sizing 移入）；回归 + commit
- **批次 B 资金闸门合入**：`_capital_gate` 纯函数化并入 `Risk/PositionSizing.py`；engine 留 3 行委托；p22 断言零变化；回归 + commit
- **批次 C 引擎拆分**：`Engine/Reconcile.py`（对账+F1 ~325 行，Mixin 方式按 A/B 拍板落位）+ `Engine.py` 主体 + `PositionBook.py`；删旧不留 shim；回归 + commit
- **批次 D 收口**：双场景全量回归（工作区 + zip 干净解压）；打总包 `Trading_refactor.zip`；更新对比文档映射表与 README 路径

## 五、四条铁律

1. **依赖单向不变**：Engine 唯一枢纽、无人反向 import。
2. **零行为变更**：只搬/拆/改名/import；类名/函数名/配置键不动；测试断言除 import 外零修改。
3. **每批回归 + commit 锚点**，问题即回退，影响面为零（沙盒内）。
4. **交付不变**：最终一个总 zip（整体替换 `Trading/` 并删除旧 `trader_gateway/`），附分批变更清单。

⚠️ 注意：①PascalCase 目录在 Linux 大小写敏感，import 与目录严格一致、一次性改全，Windows 无影响；②本包不适用单文件覆盖合并，必须整体替换目录。

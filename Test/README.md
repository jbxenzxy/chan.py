# 阶段 2.5 回归测试基线

按 `chan-architecture-v10.html` 第 8.4 节建立的快照回归基线。
目标：**在阶段 3 起的一切结构拆分中，任何导致 笔/段/中枢/买卖点/
行业映射/SSE 协议 输出漂移的改动，重跑本套件会立即失败并给出精确
diff 路径**——即后续迁移的安全网。

## 快速开始

```bash
# 仓库根目录执行
python Test/run_all.py             # 全量回归（比对冻结基线，CI 可直接用退出码）
python Test/run_all.py --update    # 迁移改动经人工确认后，重新冻结全部基线
```

全量约 1~2 分钟（合成 fixtures，无外部数据依赖，离线可跑）。

## 组件一览

| 组件 | 脚本 | 验证内容 | 快照 |
|------|------|----------|------|
| fixtures 完整性 | `gen_fixtures.py --check` | 冻结输入未被手改（重生成与磁盘逐字节一致） | — |
| 核心快照回归 | `snapshot_runner.py` | 股票 6 用例 + 期货 2 用例：全量/复盘/step/多级别/缺口/零成交/期货全量/期货复盘；冻结前先过输出契约校验 | `snapshots/stock_d_*.json`、`futures_15s_*.json` |
| trigger_step 回放 | `test_trigger_step_replay.py` | 逐步回放（step=0/-1/-5/-20）冻结 + 收敛一致性 | `snapshots/step_replay_*.json` |
| 阶段 2 成果防护 | `test_phase2_guards.py` | 配置一致性（7 对路径）/ 领域异常链路（404+JSON）/ 引擎边界（错误输入→error 字典）/ 期货日期格式契约 | — |
| 确定性测试 | `test_determinism.py` | 同输入重复调用全量一致 / 跨路径污染（股→期→股结果不变）/ 双窗口语义（契约+周期+数量+时间对齐） | — |
| 行业映射完整性 | `test_industry_mapping.py` | 双路径加载非空、内容一致、双向互逆、条目质量（6位数字/非空/无重复）、sha256 冻结 | `snapshots/industry_mapping_integrity.json` |
| SSE 事件序列 | `test_sse_sequence.py` | 首事件类型/事件序列/正常关闭/异常转 error | `snapshots/sse_event_sequences.json` |

护栏：每组件独立子进程执行，**超时 300s 按失败终止**——阶段 3 重构若引入死循环，CI 不会无限挂死。

## 目录结构

```
Test/
├── gen_fixtures.py            # 确定性合成 K 线生成器（冻结输入）
├── snapshot_runner.py         # 快照采集器（拦截加载层→引擎→契约校验→规范化→冻结/比对）
├── contracts.py               # API 输出契约（必需键集 + meta 计数内部一致性 + 主/子级两套口径）
├── comparator.py              # 递归比对器（float 1e-6 / 时间 1s 容差）
├── test_trigger_step_replay.py
├── test_phase2_guards.py      # 阶段 2 成果防护 + 引擎边界 + 期货日期契约
├── test_determinism.py        # 重复调用一致性 + 跨路径污染 + 双窗口语义
├── test_industry_mapping.py
├── test_sse_sequence.py
├── run_all.py                 # 统一入口（产出 report.json，单组件超时 300s）
├── fixtures/                  # 冻结输入（生成器产物，勿手改）
│   ├── stock_day.json         #   日线 500 根：升→盘整→降→缓升+跳空
│   ├── stock_60m.json         #   60 分钟线 2000 根（与日线同时间窗，跨级别对齐前提）
│   ├── futures_15s.json       #   螺纹钢 15s 期货 2400 根（含夜盘时段）
│   ├── stock_day_gap.json     #   停牌 5 日缺口边界
│   ├── stock_day_zero_vol.json#   零成交停牌日边界
│   └── fixtures_manifest.json #   生成参数（seed 等，单一事实源）
└── snapshots/                 # 冻结基线（比对失败=回归；--update 重冻结）
```

## 设计要点

### 1. 确定性（基线可复现的前提）
- fixtures 由固定 seed 的生成器产生，任何机器重跑逐字节一致
  （`gen_fixtures.py --check` 强制校验）；
- 合成数据注入明确的 趋势→盘整→趋势→跳空 段落，保证引擎产生
  非平凡的 笔/线段/中枢/一二三类买卖点（避免"空输出"假通过）；
- 引擎的进程内缓存与写文件副作用被 `isolate_side_effects()` 隔离。

### 2. 数据源隔离（离线可跑）
`install_data_source()` monkeypatch `read_main_level_records` /
`read_sub_level_records`，把冻结 fixtures 注入
`_analyze_stock_internal` 全路径——不触碰 TDX vipdoc / 天勤等真实数据源，
阶段 3 拆分数据加载层时该注入点即为新旧实现的交汇验证位。

期货路径同理但拦截点不同：`install_futures_data_source()` 注入假
`tqsdk` 模块（断开 TqApi 真实连接）+ 替换 `fetch_futures_kline` 返回冻结
records；引擎侧 `CTqSdkAPI` 本就是纯内存适配器（set_data→get_kl_data），
无需 mock 即真实运行。阶段 3 拆天勤加载层时，同样作为新旧实现交汇位。

### 3. 规范化（剥离环境噪声）
比对前递归剥离不稳定字段（耗时统计、进程路径、实时价、生成时间），
浮点定 10 位小数；`comparator.py` 再按 V10 §7.6 口径比对：
- 浮点：相对容差 1e-6；
- 时间：1s 容差（epoch 数值或可解析时间串）；
- 其余：类型/键集合/列表长度/标量严格相等。

### 4. trigger_step 回放的缠论口径
虚笔/虚段依赖锚点后的未来数据，逐步回放与全量**不要求逐位相同**，
验证两点：
1. 回放的全部「已确认笔」(is_sure=True) 三元组
   (direction, sdt, edt) 必须与「全量在锚点前的已确认笔」完全一致；
2. 回放末尾至多 1 根进行中虚笔，且其起点必须是全量某笔端点。

### 5. 行业映射防静默降级
`tdxhy_mapping_data.py` 有两处硬编码加载
（`DataAPI/TdxAPI.py` 模块级 + `my_chan_main.py` 内部 exec），任一失效
只打印警告并降级为空表。本用例将其转为**硬失败**，并冻结
(键数量, 内容 sha256) 作为完整性基线——阶段 5 目录调整前的持续保险。

### 6. SSE 协议契约冻结
实时数据源不可离线复现，故用确定性 mock handler 驱动
`api_server._sse_generator` 桥接层（正是阶段 3b 要重写的部分），
冻结 事件类型序列/首事件类型/事件总数/正常关闭行为；
异常路径必须转 `event:error` 且正常关闭（不悬挂）。

## 基线维护（迁移各阶段的验收流程）

```bash
# ① 改动前：基线必须全绿
python Test/run_all.py

# ② 阶段 N 拆分/重构后重跑：
python Test/run_all.py
#    全绿 → 改动无回归，可提交
#    失败 → diff 路径即漂移定位；确认属预期行为变化才允许：
python Test/run_all.py --update
git diff Test/snapshots/         # 逐项审查重冻结的差异并随代码一起提交
```

判定纪律：**`--update` 只在"行为变化已被人工确认符合预期"时使用**，
且必须审查快照 diff；禁止用 `--update` 让失败测试变绿。

## 真实数据冻结（可选增强）

当前基线基于合成数据（结构完备、确定性最强）。在用户真实环境
（有 TDX vipdoc 数据）可导出代表性真实样本替换：

```python
# 伪代码：从真实读取层导出冻结样本
rows = my_chan_main.read_main_level_records(0, "600519", "d")
json.dump([{**r, "dt": r["dt"].strftime("%Y-%m-%d %H:%M:%S")} for r in rows],
          open("Test/fixtures/stock_day_real.json", "w"))
```

再在 `snapshot_runner.CASES` 中挂接新用例即可，快照机制不变。
真实样本建议选取包含 2024-09 行情急涨急跌的区间（结构更丰富）。

## 已知边界

- 合成数据非真实行情，快照保证的是**引擎行为不变**，不是行情正确性；
- `_SSEMockHandler` 冻结的是桥接层协议契约，实时数据源本身
  （天勤 SDK）不在离线回归范围（属阶段 3b 灰度验证）；
- 沙箱 Python 3.10 需要 `typing_extensions` 提供 `typing.Self` 垫片
  （用户 Windows 环境 3.11+ 无需）。

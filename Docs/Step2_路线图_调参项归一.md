# Step 2 路线图：调参项架构归一化 + 多 phase 推进计划

- **基线**：`custom-dev @ 5478def` + Step 1B v2 修复（已合并到 real project）
- **沙盒**：`C:/Users/river/WorkBuddy/2026-09-08-06-43-04/sandbox/chan.py`（不复用远端 `14d2cb0`，与本地工作基线一致）
- **日期**：2026-09-08
- **本文件**：Step 2 路径上**所有 phase 的总览 + 当前进度**，下次开工从进度表的"下一步"接续

---

## 0. 用户核心需求（原文）

> 调参我觉得不是最重要的，最重要的是先在代码架构上把各周期可能涉及的调参项归一到 Trading/Config.py 中，方便后续在这一个地方调整，而不是散落在代码各处

**关键词**：**归一**（不是调值）、**一个地方**（不在散落）、**先架构后数值**。

> 因为无法一次性做完，所以以上这些都需要纳入文档管理，下次再接着做的时候，知道做到哪里了，可以延续推进下去

**关键词**：**断点续**、**进度状态**。

---

## 1. 设计决策（先想清楚再动刀）

### 1.1 SSOT 三层结构

```
┌─────────────────────────────────────────────────────────────────────┐
│ Trading/Config.py          ← 用户改调的**唯一入口**                   │
│   ├── ExitParamsConfig / EntryParamsConfig  ← 跨周期不变的字段        │
│   ├── RiskConfig / SizingConfig             ← 跨周期不变的字段         │
│   ├── BrokerParamsConfig                   ← 跨周期不变的字段 + 周期  │
│   ├── SourceConfig                         ← 跨周期不变的字段 + 周期  │
│   └── PeriodProfile  (BaseModel)            ← **周期敏感字段的 SSOT**   │
│         └── PERIOD_PROFILES: dict          ← freq → profile 实例        │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  │  启动期 main.py 按 cfg.source.freq
                                  │  把 PERIOD_PROFILES[freq] 字段值写回
                                  │  cfg.exit_policy.params / broker /
                                  │  source / risk  的对应字段上
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Strategy/Exit.py / Entry.py 等          ← 不变，字段名不动             │
│   通过 cfg.exit_policy.params['atr_period'] 这种现有方式读取            │
└─────────────────────────────────────────────────────────────────────┘
```

**设计要点**：
1. **所有"调参面板"集中在 `Config.py` 一个文件**（含子模块 PeriodProfile），用户改这一处即可
2. **PeriodProfile 是 Config.py 内的 BaseModel 子类**，不放独立文件（与"Config.py 一个地方"原话一致）
3. **下游策略代码不变字段名**，Profile 字段值在**启动期**由 main.py 工厂写回 cfg.* 字段 —— 下游 0 改动
4. **PERIOD_PROFILES 是**模块级 dict，键=freq，值=PeriodProfile 实例，**频率敏感字段值在这里且仅在这里声明一次**
5. **ExitParamsConfig 等保留原字段**（兼容已有 fixture），但**默认值用 PeriodProfile 默认值反推**——避免双默认源

### 1.2 为什么 PeriodProfile 不取代 ExitParamsConfig

- ExitParamsConfig 现已被 5 个 pydantic 模型、3 个测试 fixture、1 份 README 引用
- 直接删除字段会引发连锁改动
- 用"Profile 覆盖写回"的注入方式，下游 0 改动、fixture 不破

### 1.3 跨周期不变 vs 周期敏感字段的判定标准

| 判定 | 含义 |
|---|---|
| **跨周期不变** | 点数 / 百分比 / 时间戳格式 / 字符串键（如 `session_end_hhmm="14:55"`）—— 留在 ExitParamsConfig 等 |
| **周期敏感** | 数值因周期而变的，**任何**有"墙钟等效"/"信号密度"/"成本占比"含义的倍数、周期、根数 —— 进 PeriodProfile |

---

## 2. 104 项调参点全景

来自 Explore 子代理的全面扫描（Trading/ 全代码 + main.py + Recorder）：

| 类 | 已显式 | 散落 | 周期敏感 | 重点候选 |
|---|---|---|---|---|
| A · 策略（Entry/Exit/Base） | 17 | 3 | 11 | `atr_period` / `atr_sl_multiple` / `breakeven_trigger_r` / `trailing_trigger_r` / `trailing_atr_multiple` / `max_hold_seconds` / `bar_secs` |
| B · 风控（RiskGate/PositionSizing） | 17 | 3 | 0 | `_CFFEX_SINGLE_ORDER_MAX=20` / `margin_rate` 兜底重复 |
| C · Broker（SimNow） | 10 | 18 | 5 | `_QUOTE_STALE_SECONDS=30`、`wait_update` 超时簇、退避倍增、追价步长 |
| D · 引擎（Engine/Reconcile） | 1 | 6 | 1 | `_close_retry_bars=5` / `_close_max_streak=20` / `_unlock_stuck_bars=5` / `4.5h` 交易日 |
| E · 行情源（SSE/Replay） | 9 | 6 | 1 | 重连基 5、最大 60、重试 0=无限 |
| F · CLI/Recorder | 0 | 7 | 0 | recorder 重连、最大重连、flush 间隔 |
| **合计** | **54** | **43** | **18** | |

> 全表见 `output/Step2_路线图_调参点全表.md`（未列入文档主体，下方 Phase 中按需引用）

---

## 3. Phase 总览（6 段，本文档是 SSOT 进度源）

| Phase | 主题 | 状态 | 估算 diff | 阻断前置 |
|---|---|---|---|---|
| **2.0 路线图** | 本文件 | ✅ **当前** | 0 | 无 |
| **2.1 周期敏感参数入 PeriodProfile** | 把 A 档 11 项 + C-1 overprice + E signal_max_age + D-7 4.5h 搬到 PeriodProfile，PERIOD_PROFILES 填 4 周期粗估值，启动期覆盖写回 | ⏳ **下一步** | ~250 行 | 用户拍板 1.1 设计 |
| **2.2 引擎常量 EngineConfig 化** | D-1/2/3（close/MAX/stuck 三常量） + D-6 PositionBook 默认容量 显式化 | ⏸ 待启动 | ~80 行 | 2.1 |
| **2.3 Broker/Channel 超时集中** | C-2~C-7（~18 处 wait_update/重试秒数）合并到新 `ChannelTimingConfig`，附在 `BrokerParamsConfig` 内 | ⏸ 待启动 | ~150 行 | 2.1 |
| **2.4 重复常量合并** | B-9 `20` + D-4 `20` 合并 `_CFFEX_LIMIT_MAX`；B-10 删除 `0.15` 重复兜底 | ⏸ 待启动 | ~30 行 | 无（独立） |
| **2.5 Source/Recorder 重连参数化** | E-2/3/4 + F-5/6/7 进 `SourceConfig.reconnect_*`、Recorder 自带 argparse（不污染主程序） | ⏸ 待启动 | ~50 行 | 无 |
| **2.6 测试 + 启动校验** | 每个 Phase 加测试；6 段全完后跑 `--freq {30m,5m,1m,15s}` 启动冒烟全通；干净副本复验 | ⏸ 待启动 | ~150 行 | 全部 Phase |

**总 diff 估算**：~700 行（不含 phase 2.7 调值阶段，那是数据依赖的事）

---

## 4. Phase 2.1 详细执行清单（下次开工直接照做）

### 目标
"归一到 Config.py 一个地方" 这一步落到位。所有 11 个真正"周期敏感"的策略参数 + 3 个 broker/source 周期耦合项 + 1 个时间常量，全部进 `PeriodProfile`，按 freq 列写粗估值。

### 涉及文件

| 文件 | 改动 |
|---|---|
| `Trading/Config.py` | 新增 `PeriodProfile`（BaseModel）+ `PERIOD_PROFILES` dict + `resolve_period_profile(cfg, freq)` 工厂 |
| `Trading/main.py` | 在启动期 cfg 校验后调用 `resolve_period_profile(cfg, cfg.source.freq)` |
| `Trading/Strategy/Exit.py` | **不改**（字段名不变，靠 cfg 覆盖） |
| `Trading/Strategy/Entry.py` | **不改** |
| `Trading/Broker/SimNow.py` | **不改**（overprice 通过 cfg 取值，已经从 cfg.broker 取） |
| `Trading/Source/SSE.py` | **不改** |
| `Trading/Test/test_period_consistency.py` | 加新断言：周期敏感字段在 PeriodProfile 与 Config 字段**同源**（不漂移） |
| `Trading/Test/test_period_matrix.py` | 加新段：4 周期各自能从 PERIOD_PROFILES 取到预期值 |
| `Trading/README.md` | 加段："调参面板在哪"（指向 PeriodProfile） |

### PeriodProfile 字段清单（11 项 + 4 项扩展）

| 字段 | 源（Config.py 现字段） | 粗估 15s | 粗估 1m | 粗估 5m（基线） | 粗估 30m |
|---|---|---|---|---|---|
| `atr_period` | `ExitParamsConfig.atr_period` | 240 | 60 | 14 | 96 |
| `atr_sl_multiple` | `ExitParamsConfig.atr_sl_multiple` | 1.0 | 1.5 | 2.0 | 3.0 |
| `trailing_atr_multiple` | `ExitParamsConfig.trailing_atr_multiple` | 0.5 | 1.0 | 1.5 | 2.5 |
| `r_multiple_tp` | `ExitParamsConfig.r_multiple_tp` | 1.0 | 1.5 | 2.0 | 3.0 |
| `min_r_points` | `ExitParamsConfig.min_r_points` | 0.4 | 1.0 | 2.0 | 6.0 |
| `breakeven_trigger_r` | `ExitParamsConfig.breakeven_trigger_r` | 0.5 | 1.0 | 1.0 | 1.0 |
| `trailing_trigger_r` | `ExitParamsConfig.trailing_trigger_r` | 1.0 | 1.5 | 2.0 | 2.0 |
| `max_hold_seconds` | `ExitParamsConfig.max_hold_seconds` | 1800.0 | 3600.0 | 0.0（不启用） | 0.0 |
| `bar_secs` | `ExitParamsConfig.bar_secs` | 15 | 60 | 300 | 1800 |
| `signal_max_age_minutes` | `SourceConfig.signal_max_age_minutes` | 5.0 | 30.0 | 60.0 | 60.0 |
| `overprice_points` | `BrokerParamsConfig.overprice_points` | 0.2 | 0.4 | 1.0 | 1.5 |
| `session_seconds` (新) | 不在 Config 加，从 PeriodProfile 推出 | 14400 | 14400 | 14400 | 14400 |

> **注**：`session_seconds` 是替换 main.py:91 那行 `(4.5*3600)/_bs` 计算的关键，让"一天的墙钟秒数"也走 Profile（虽跨周期不变，但用户原话要"集中调参"，加进去便于后续差异化）。

### PERIOD_PROFILES dict 结构（按实现草稿）

```python
PERIOD_PROFILES: Dict[str, PeriodProfile] = {
    "15s": PeriodProfile(
        freq="15s",
        atr_period=240, atr_sl_multiple=1.0, trailing_atr_multiple=0.5,
        r_multiple_tp=1.0, min_r_points=0.4,
        breakeven_trigger_r=0.5, trailing_trigger_r=1.0,
        max_hold_seconds=1800.0, bar_secs=15,
        signal_max_age_minutes=5.0, overprice_points=0.2,
        session_seconds=14400,
    ),
    "1m":  PeriodProfile(freq="1m", ...),
    "5m":  PeriodProfile(freq="5m", atr_period=14, atr_sl_multiple=2.0, ..., max_hold_seconds=0.0, overprice_points=1.0, ...),  # 基线
    "30m": PeriodProfile(freq="30m", atr_period=96, atr_sl_multiple=3.0, ..., max_hold_seconds=0.0, overprice_points=1.5, ...),
}
```

### resolve_period_profile 工厂伪代码

```python
def resolve_period_profile(cfg: GatewayConfig) -> GatewayConfig:
    """启动期调用：按 cfg.source.freq 把 PeriodProfile 字段覆盖到 cfg。
    cfg.source.freq 不在白名单 → raise（fail-fast）。
    """
    freq = cfg.source.freq
    if freq not in PERIOD_PROFILES:
        raise ValueError(f"freq={freq!r} 不在 PERIOD_PROFILES 白名单")
    p = PERIOD_PROFILES[freq]
    cfg.exit_policy.params['atr_period'] = p.atr_period
    cfg.exit_policy.params['atr_sl_multiple'] = p.atr_sl_multiple
    cfg.exit_policy.params['trailing_atr_multiple'] = p.trailing_atr_multiple
    cfg.exit_policy.params['r_multiple_tp'] = p.r_multiple_tp
    cfg.exit_policy.params['min_r_points'] = p.min_r_points
    cfg.exit_policy.params['breakeven_trigger_r'] = p.breakeven_trigger_r
    cfg.exit_policy.params['trailing_trigger_r'] = p.trailing_trigger_r
    cfg.exit_policy.params['max_hold_seconds'] = p.max_hold_seconds
    cfg.exit_policy.params['bar_secs'] = p.bar_secs
    cfg.source.signal_max_age_minutes = p.signal_max_age_minutes
    cfg.broker.overprice_points = p.overprice_points
    return cfg
```

### 验收标准（Phase 2.1 完成定义）

| # | 验收项 | 测法 |
|---|---|---|
| 1 | `PeriodProfile` 在 `Config.py` 内声明；`PERIOD_PROFILES` 注册 4 freq 全 | `test_period_consistency.py` 新断言 |
| 2 | `resolve_period_profile()` 启动期调用，对 cfg 字段值有对应改动，且**不破坏**原字段 | 全量现有测试通过 |
| 3 | `--freq 99m` 等非法周期在 **两处** fail-fast（period 白名单已在 Step 1B；新加的 `PeriodProfile` 也要拦） | CLI 冒烟 |
| 4 | 4 周期跑（`--freq 15s / 1m / 5m / 30m`）都成功起 + 完整测试套件不红 | 全量回测 |
| 5 | README 更新："调参先看 Config.py PERIOD_PROFILES" | 文档 |
| 6 | 干净副本（custom-dev@5478def + Step 1B v2 + Phase 2.1 patch）→ 全套测试通过 | 复验 |

### 不在 Phase 2.1 范围

- ❌ 不动 **数值**（只搬位置 + 默认值，**不调**）
- ❌ 不动已有 **fixture**（p8/p20/p15b 测试里直接写字段值的，让 Profile 默认值反推兼容）
- ❌ 不开始 **回测 / 调值**（要数据依赖）

---

## 5. 进度状态（本节是 SSOT，下次开工先看这里）

| Phase | 起始 | 终点 | 状态 | 备注 |
|---|---|---|---|---|
| 2.0 路线图 | 2026-09-08 | 2026-09-08 | ✅ 已完成 | 本文件 |
| 2.1 周期敏感参数入 PeriodProfile | — | — | ⏳ 下次开工 | 等用户拍板设计决策（1.1 三层结构）|
| 2.2 引擎常量 EngineConfig 化 | — | — | ⏸ | — |
| 2.3 Broker/Channel 超时集中 | — | — | ⏸ | — |
| 2.4 重复常量合并 | — | — | ⏸ | — |
| 2.5 Source/Recorder 重连参数化 | — | — | ⏸ | — |
| 2.6 测试 + 启动校验 | — | — | ⏸ | — |

### 已知阻塞项
- 🔴 真实数据缺失：5m 有 144 根 demo，15s/1m/30m 无；调值必须等补录（不在 Phase 2 范围内）
- 🟡 远端未推送：real project 合并了 Step 1B v2 但没推 GitHub；下次沙盒初始化时核对基线

### 用户拍板清单
- [ ] **设计 1.1 三层结构**：Config.py 内嵌 PeriodProfile、PeriodProfile 字段值覆盖 cfg 字段、下游 0 改 —— **接受 / 反对 / 微调**
- [ ] **Phase 2.1 起跑时点**：现在 / 等其他事 / 周末
- [ ] **Phase 2.1 字段范围**：本文件 12 项（含 session_seconds）/ 仅策略 9 项 / 还要加点什么

---

## 6. 调值阶段（Phase 2.7+ 数据依赖，本文档不展开，仅占位）

待 Step 2.6 完成后，且真实数据补录到位（≥ 5 个交易日）后再开：

- **2.7 数据补录**：录制器跑 4 周期 × 5 日
- **2.8 实证回测**：`analyze_backtest.py` 出每周期胜率/期望/最大回撤
- **2.9 网格搜索**：每参数 ±50% 网格找稳态带
- **2.10 SimNow 实盘验证**：挑最稳周期跑 2 周比对

> 这部分**与本路线图独立**，不在 SSOT 结构改动范围内；开始时另起"Step 2 调值路线图"。

---

## 7. 与 Step 1 的关系

| | Step 1A（审计） | Step 1B（修复） | Step 2.1（归一） |
|---|---|---|---|
| 目标 | 找出 bug | 修 bug | 把调参面板集中 |
| 动作 | 文档 + 不改代码 | 改代码 + 测试 | 改代码 + 测试 |
| diff | 0 | ~700 行（含 v2 撤回） | ~250 行 |
| 依赖 | 无 | Step 1A | Step 1B（已合并 ✅） |
| 数据依赖 | 无 | 无 | 无 |

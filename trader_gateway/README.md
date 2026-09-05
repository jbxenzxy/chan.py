# M1 交易网关（dry-run 骨架）

把「缠论买卖点信号」接到「交易执行」的独立进程网关。**当前只做 dry-run（模拟撮合），不发真实委托**，先把信号→执行这条最难改的管道造结实，止盈止损做成可插拔插件，以后调参/换策略/接 SimNow 实盘都不用返工。

> 依赖：仅 Python 3.8+ 标准库，无第三方依赖。随目录拷走即可跑。

---

## 一、目录结构

```
trader_gateway/
├── run_gateway.py          # CLI 入口
├── config.example.json     # 配置模板（--init-config 生成）
├── config_trailing.json    # 移动止损策略示例配置
├── replay_data/            # demo 回放数据（与 M0 录制器格式一致）
├── tools/make_demo_data.py # 生成 demo 数据
└── tg/
    ├── types.py            # Signal/Bar/Position/Trade/ExitPlan 等数据结构
    ├── config.py           # JSON 配置加载（零依赖）
    ├── events.py           # jsonl 事件日志（每笔委托/成交/信号全落盘）
    ├── store.py            # sqlite：信号幂等键、持仓状态、当日统计
    ├── symbols.py          # 合约映射、price_tick 对齐、手续费/滑点成本模型
    ├── risk.py             # 时段/手数/日亏/日次数风控闸门
    ├── engine.py           # 事件驱动状态机（bar 结算 → signal 开仓）
    ├── strategy/           # 可插拔策略（新增文件即注册）
    │   ├── base.py             # EntryPolicy / ExitPolicy 接口 + @register 装饰器
    │   ├── default_policy.py   # 用户当前规则（+10 点止盈 / 信号极值止损）
    │   └── example_trailing.py # 移动止损示例（演示"换策略"怎么做）
    ├── brokers/            # 执行通道（新增 Broker 即注册）
    │   ├── base.py             # Broker 接口
    │   ├── dry_run.py          # 模拟撮合（滑点 + 价格对齐 + 成交回报）
    │   └── simnow.py           # SimNow 仿真下单（TqAccount + 主连自动映射）
    └── sources/            # 信号源（实时 / 回放产出相同事件流）
        ├── base.py
        ├── sse_source.py       # 实时订阅 chan.py 的 SSE
        └── replay_source.py    # 离线回放 M0 录制数据
```

---

## 二、快速上手

```bash
cd trader_gateway

# ① 用 demo 数据离线回放（几秒出结果，先跑这个看效果）
python run_gateway.py --source replay --replay-dir ./replay_data --out ./run1

# ② 生成一份可编辑的配置
python run_gateway.py --init-config ./config.json
#    改完 config.json 后用 --config 启动
python run_gateway.py --config ./config.json

# ③ 实时接入 chan.py（先启动 chan.py，SSE 默认 http://127.0.0.1:18081）
python run_gateway.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m --out ./run_live
```

运行结束后会打印摘要（成交笔数 / 胜率 / 平均盈亏 / 净盈亏 / 单笔期望 / 按出场原因拆分），并落盘两份产物：

- `events.jsonl` —— 全量事件流水（signal / order / open / close / risk_block / …），可复现、可审计
- `state.db` —— sqlite 状态（信号幂等键、持仓、当日统计），**重放同一目录不会重复成交**

---

## 三、架构分层

```
信号源 source ──bar/signal──▶ 引擎 engine ──决策──▶ 策略 policy ──▶ 风控 risk ──▶ broker
   (sse/replay)                 (状态机)          (可插拔)          (闸门)       (dry_run/…)
```

**换策略 = 丢一个 py 文件进 `tg/strategy/` + 改 config.json 里的类名**，engine/broker/source 一行不动。参考 `example_trailing.py` 里的三步注释。

三个刻意保留的保守设定（`default_policy.py`）：

1. 同根 K 线同时触及止盈与止损 → 按止损计（不猜盘中先后顺序）
2. 价格对齐一律往「对自己不利」方向取整（止损更易触发、止盈更晚更少）
3. 出场计划带参数快照落盘，事后可做参数敏感性分析

两处时序防护（回测作弊来源，已在 engine 显式防住）：

1. **入场那根 K 线不参与出场判定**（否则开仓瞬间就可能被止损）
2. **重复/回退的 bar 直接丢弃**（SSE 重发或断线重连补发历史帧）

---

## 四、当前策略与参数（config.json）

```jsonc
{
  "broker": "dry_run",
  "state_dir": "./state",
  "instrument": {
    "signal_symbol": "KQ.m@CFFEX.IF",   // 行情主连（不能直接下单）
    "trade_symbol": "CFFEX.IF2609",      // 实际下单合约（需手工换月）
    "price_tick": 0.2,
    "multiplier": 300.0,
    "open_fee_rate": 0.000023,           // 期指开仓手续费率
    "close_today_fee_rate": 0.000345,    // 平今（贵！）
    "close_fee_rate": 0.000023,
    "slippage_ticks": 1.0,
    "close_today_first": true,           // 上期所/中金所平今优先
    "sessions": ["09:30-11:30", "13:00-15:00"]
  },
  "risk": {
    "max_volume": 1,
    "max_trades_per_day": 20,
    "max_daily_loss_points": 60.0,       // 日亏 60 点后停止开仓
    "enforce_session": true,
    "no_open_after": "14:50",            // 尾盘不再开新仓
    "close_before_session_end": true,    // 收盘前强平
    "block_on_daily_loss": true
  },
  "entry_policy": {
    "name": "DefaultEntryPolicy",
    "params": {
      "reverse_on_opposite_signal": false,  // 反向信号只平今不反手
      "max_signal_range_points": 0.0,       // 0=不限；>0 过滤振幅过大的信号
      "min_stop_distance_points": 0.0
    }
  },
  "exit_policy": {
    "name": "DefaultExitPolicy",
    "params": {
      "take_profit_points": 10.0,        // 止盈 10 点
      "stop_at_signal_extreme": true,    // 止损=信号K线极值（多=最低价/空=最高价）
      "stop_buffer_ticks": 0.0,          // 止损外扩缓冲（跳数）
      "max_hold_bars": 0                 // 0=不限；>0 持有 N 根 K 线后强制离场
    }
  },
  "source": { "type": "replay", "replay_dir": "./replay_data",
              "sse_base": "http://127.0.0.1:18081",
              "symbol": "KQ.m@CFFEX.IF", "freq": "5m" }
}
```

> 把 `stop_at_signal_extreme` 置 false 会改用固定点数止损（`stop_points`），可与信号极值止损做 A/B 对比。`max_signal_range_points` 配合 M0 的「信号振幅分布」结果，能直接过滤掉止损过宽的信号。

---

## 五、信号源：回放 vs 实时

| 维度 | replay | sse |
|---|---|---|
| 数据 | `replay_data/{signals.json,klines.json}` | chan.py SSE 实时流 |
| 用途 | 离线调参、回测、参数敏感性分析 | 实盘前观察 |
| 幂等键 | `{date}|{type}|{B/S}`（与 M0 录制器一致） | 同左 |

M0 录制器（`sandbox/m0_signal_recorder/`）产出的 `events.jsonl` / `signals.json` / `klines.json` 可直接作为回放输入，无需任何转换。拿到 ≥5 个交易日真实信号后：

```bash
# 把 M0 录制的 signals.json + klines.json 放进某个目录，然后
python run_gateway.py --source replay --replay-dir <M0输出目录> --out ./run_real
```

---

## 六、回放结果的正确读法

demo 数据是**合成随机行情**，结果必然负期望，只用于验证链路与幂等。真实结论要以 M0 录制数据为准，并注意：

- 默认会**回放最终消失的信号**（保守）；加 `--only-alive` 只回放存活信号（会高估策略，仅供对比）。
- 期指**平今手续费 0.0345%**（约 1.38 点），是开仓（0.09 点）的 15 倍。成本模型已含，务必把 `close_today_first` 与手续费率填对，否则胜率判断会失真。
- 建议先看 `by_reason` 拆分：如果 `sl` 占比过高，说明止损距离（=信号K线振幅）太紧，用 M0 的振幅分布反推合理的 `min_stop_distance_points`。

---

## 七、接 SimNow 仿真（已内置 `simnow` broker）

`tg/brokers/simnow.py` 已实现，`--broker simnow` 切换即可，与 dry-run 共享同一套引擎/策略/风控。

```bash
# 前提：装 tqsdk（仅 simnow 模式需要，dry_run 仍零依赖）
pip install tqsdk

# Windows CMD 设凭据（优先级：环境变量 > config 的 broker_params）
set SN_ACCOUNT=你的simnow账号&& set SN_PASSWORD=你的simnow密码&& ^
set TQ_ACCOUNT=你的天勤账号&& set TQ_PASSWORD=你的天勤密码&& ^
python run_gateway.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m ^
    --broker simnow --out ./run_live
```

simnow broker 与 dry_run 的两点差异：

1. **真实撮合**：成交价 = SimNow 仿真撮合价（`order.trade_price`），不做 dry_run 的滑点让价。
2. **主连自动映射**：`signal_symbol` 若是 `KQ.m@...` 主连，连接后自动用 `underlying_symbol` 解析主力合约，**不用再手工改 `trade_symbol`**；解析失败才回退到 config。

下单失败（被拒/超时）时引擎**不会产生幽灵持仓**：开仓被拒 → 不开仓并落盘 `order_rejected`；平仓被拒 → 保留持仓等下一根 K 线再试。

> ⚠️ 先跑 M2a 探针（`m2a_simnow_probe`）确认通道，再上 simnow broker；避免账号坑全链路排查。

---

## 八、尚未做 / 安全边界

**尚未做**：

1. **创元实盘**：`TqAccount` 换成创元公司名 + 向期货公司申请程序化 AppID/AuthCode；平仓 offset 的精细平今（`CLOSETODAY`）需按持仓当日判定（当前用 `CLOSE` 让交易所自动处理）。
2. **换月移仓**：主连自动映射只解决「下单落到当前主力」，跨月移仓（平旧月开新月）需单独写。
3. **先跑 M0 一周**：确认信号真实重绘率、振幅分布、期望，再决定是否接实盘。

**安全边界**：

- **dry_run 不产生任何真实委托**；`simnow` 只连 SimNow 仿真（假钱），不影响真实资金。
- 接创元实盘前：SimNow 需 `BrokerID 9999` + AppID/AuthCode；创元需向期货公司申请程序化接入。
- 本网关是**独立进程**，与 chan.py 看盘解耦；崩溃不影响看盘，看盘也不会挤占交易会话额度。

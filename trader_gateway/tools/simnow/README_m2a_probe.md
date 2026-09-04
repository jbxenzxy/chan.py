# M2a · SimNow 通道探针

在写完整 SimNow broker 之前，先用这一个脚本验证通道，把账号/权限/网络问题提前暴露，而不是等全链路写完才发现卡在账号上。

## 它验证四件事

| 步骤 | 验证内容 | 能暴露的坑 |
|---|---|---|
| 1 | SimNow 能连上（登录） | 账号密码错、非交易时段、网络不通 |
| 2 | 账户资金正常 | 账号未初始化、权益为 0 |
| 3 | IF 主连 → 主力合约映射 | 合约代码/行情异常 |
| 4（可选） | 下单 + 撤单通道 | 合约不可交易、下单方式不支持 |

跑通这四步，M2b 的完整 broker 就是把这段连接逻辑包进 `Broker` 接口，不用再摸黑。

## 前置

```bash
pip install tqsdk
```

需要两组凭据（**都不硬编码、不落盘明文，只从环境变量读**）：

| 环境变量 | 含义 | 来源 |
|---|---|---|
| `SN_ACCOUNT` / `SN_PASSWORD` | SimNow 仿真账号 | SimNow 官网注册 |
| `TQ_ACCOUNT` / `TQ_PASSWORD` | 天勤（快期）账号 | 你的 chan.py 行情同款，已在 `DataAPI/TqSdkAPI.py` 使用 |

## 运行（Windows）

CMD：

```bat
set SN_ACCOUNT=你的simnow账号&& set SN_PASSWORD=你的simnow密码&& ^
set TQ_ACCOUNT=你的天勤账号&& set TQ_PASSWORD=你的天勤密码&& ^
python simnow_probe.py --symbol "KQ.m@CFFEX.IF"
```

PowerShell：

```powershell
$env:SN_ACCOUNT="你的simnow账号"; $env:SN_PASSWORD="你的simnow密码"
$env:TQ_ACCOUNT="你的天勤账号"; $env:TQ_PASSWORD="你的天勤密码"
python simnow_probe.py --symbol "KQ.m@CFFEX.IF"
```

> 安全建议：用 `set`/`$env:` 只在当前终端生效，关掉终端即失效，不会写进系统。不要用 `setx`（那会永久写入用户环境变量）。

## 下单通道测试（可选）

默认**只查不下单**（最安全）。确认前四步 OK 后，再加 `--place-order`：

```bat
python simnow_probe.py --symbol "KQ.m@CFFEX.IF" --place-order
```

测试单用**远离市价的限价买单**（约市价的 95%），确保不成交，观察订单状态后立即撤单，不会留下持仓。

## 预期输出（正常）

```
[1/4] 登录 SimNow（天勤中转）...
[OK] 登录成功
[2/4] 查询账户资金 ...
    balance=1000000.00  available=1000000.00  margin=0.00 ...
[3/4] 查询主连映射 KQ.m@CFFEX.IF ...
[OK] 主连 KQ.m@CFFEX.IF -> 主力合约 CFFEX.IF2609
    last_price = 4000.2  datetime = ...
=== 探针完成 ===
```

第 3 步的 `CFFEX.IF2609` 就是主连当前映射的具体合约——**这正是 M2b 里 `trade_symbol` 要自动解析的值**，替代现在 demo 里手工写死的 `IF2609`。

## 失败排查

| 现象 | 最可能原因 | 处理 |
|---|---|---|
| `登录失败` | SimNow 密码错 / 非交易时段 | 核对密码；SimNow 交易时段约 9:00-15:15（夜盘另计），或确认是否 7x24 环境 |
| `登录失败` + 超时 | 天勤中转没连上 | 核对 `TQ_ACCOUNT/TQ_PASSWORD`，确认天勤账号本身能登录 |
| 权益为 0 | 新注册 SimNow 账号未满 3 个交易日 | 等 3 个交易日再试（SimNow 规定） |
| `30s 内未取到 underlying_symbol` | 行情没到 / 合约写错 | 核对 symbol 拼写（`KQ.m@CFFEX.IF` 大小写敏感） |
| 下单异常 | 合约不可交易 / 时段限制 | 确认 SimNow 环境当前开放，且该合约在可交易列表内 |

## 与 M2b 的关系

```
M2a（本脚本）      →  验证通道，确认能连/能查/能下单
M2b（待开发）      →  tg/brokers/simnow.py：把探针逻辑包进 Broker 接口，
                       insert_order 落进引擎，underlying_symbol 自动解析 trade_symbol，
                       CLOSETODAY 平今，接入 M1 的引擎/策略/风控
```

M2a 通过后再做 M2b，可以避免「完整 broker 写完才发现账号连不上」的返工。

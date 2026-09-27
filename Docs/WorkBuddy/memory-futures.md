# memory-futures.md —— 期货：CTP / TqSdk / SimNow / 交易所规则


> 本文件由 `~/.workbuddy/MEMORY.md` 于 2026-09-15 拆分而来，**不常驻上下文**。
> 命中 MEMORY.md 索引表的主题词时，用 Read / Grep 按需读取本文件。
> 内容原样搬运未删减；拆分前全文备份：`MEMORY.full-2026-09-15.md`。

## ⭐ CTP 的 Python 绑定生态（2026-09-19 实测调研；做"自写直连/换绑定库"前先读）

**选型的分水岭 = 纯绑定 vs 框架插件**（混为一谈会写出装不上的方案）：
| 库 | 性质 | win wheel | 许可 |
|---|---|---|---|
| `openctp-ctp`（openctp/openctp-ctp-python，SWIG，`from openctp_ctp import mdapi, tdapi`） | **纯绑定**，与 C++ 同名同类 | cp37~**cp314**（含 cp314-win_amd64） | BSD-3 |
| `ctp-python`（keli/ctp-python，SWIG，`import ctp`） | **纯绑定** | cp38~cp313，**无 cp314** | BSD-2 |
| `ctpwrapper`（nooperpudd，Cython .pyx） | 纯绑定但**只有 sdist，需本机 cython+MSVC 编译** | 0 个 win wheel | 未标注 |
| `vnpy_ctp`（vnpy/vnpy_ctp） | **框架插件，不是绑定库**：`requires_dist=["vnpy>=3.0.0"]`、`__init__.py:26 from .gateway import CtpGateway`、gateway `:6-29` import `vnpy.trader.*` | cp310/cp313（**无 cp311/cp314**） | MIT |

- **实测（本机沙盒 venv，可复跑）**：Python **3.14.7** 上 `pip install openctp-ctp` 成功 + `import` 成功 +
  16 个关键方法 `hasattr` 全 True（ReqAuthenticate/ReqSettlementInfoConfirm/ReqOrderInsert/ReqOrderAction/
  ReqQryInvestorPosition/ReqQryInstrument/ReqQryTradingAccount/RegisterSpi/…+ RegisterUserSystemInfo）。
  → **"cp314 无 wheel"只对特定库成立，不是生态事实。**
- **实测反证**：3.13 venv 里 `pip install --no-deps vnpy_ctp` 后 `import vnpy_ctp.api`
  → `ModuleNotFoundError: No module named 'vnpy'`；3.14 上 `pip install vnpy_ctp` 直接
  `metadata-generation-failed`（无 cp314 wheel，回落 sdist 走 meson 构建失败）。
  想"只用 vnpy_ctp 的绑定层"**做不到**，要它就得装 vnpy 全套（pyside6==6.8.2.1 / ta-lib / numpy / pandas /
  plotly / pyqtgraph / qdarkstyle / pyzmq / deap / nbformat / loguru / qrcode / tzlocal / tqdm）。
- **看穿式采集**：`keli/ctp-python` README 明示「自写 CTP 程序直连**不需要** LinuxDataCollect.so」；
  交叉印证 = openctp-ctp wheel 内只有 thostmd/traderapi 两个 pyd、无 DataCollect。ctpwrapper 仍带
  `datacollect.pyx` 属 6.3.15 前的历史兼容。→ 6.6.9+ 采集已内置，只需 AppID/AuthCode + ReqAuthenticate。
- **版本匹配是硬约束**：各库锁定的 API 版本不同（vnpy_ctp 6.7.11.4 / ctp-python 6.7.7.post1 /
  openctp-ctp 6.7.11.0 / ctpwrapper 6.7.13），期货公司柜台升级不受我们控制；不一致会
  `Decrypt handshake data failed`（keli README）。**选绑定库 = 把跟随柜台升级的责任永久揽到自己身上**
  （停更先例：lovelylain/pyctp 2017、nicai0609/Python-CTPAPI 2021）。
- **适配层有 905 行 MIT 参考实现**：`vnpy_ctp/gateway/ctp_gateway.py`（onRspAuthenticate→login→取
  FrontID/SessionID→自动确认结算单→`while True` 重试 reqQryInstrument 绕过流控→contract_inited 前
  缓存 order_data/trade_data）。自写适配前先读它。
- 生态位结论：**「直连 CTP」是主流，「自写薄适配」不是主流**（主流是进框架用现成 gateway）。

## ⭐ chan.py 平今分支现状 + TqCtp 直连的硬事实（2026-09-18 复核，最优先读）

**① 平今分支是活的（旧记忆「代码里不存在 CLOSETODAY 分支」已过时）**
- `Trading/Broker/Base.py:84-88` `INTENT_TO_OFFSET` 有**三**值：`OPEN`/`CLOSE`/`CLOSETODAY`
  （注释「一期只有两项…**启用第三项**」）。
- `Trading/Infra/Product.py:241-250` `EXEC_POLICY`：**IF/IH/IC/IM=R-OPEN、AU/AG/CU=CLOSETODAY**（上期所三品种）、TA=R-OPEN。
- `Trading/Engine/Engine.py:1059-1060、:1322-1323` 按 `ExecPolicy.today_exit` 生成/守卫该意图；
  `SimNow.py:965-979` 平仓前判据随之分叉（CLOSE 认昨仓 / CLOSETODAY 认今仓）。
- ⚠️ `SimNow.py:44-46` 模块 docstring 仍写「CLOSETODAY 不可达」，**与 :965-979 自相矛盾**，是过时注释，别当证据。
- 铁律不变：**绝不按交易所名字判断**（`Product.py:240` 明文「行尾注释的交易所只是备案」）。
  直连 CTP 时 offset **直接平移**（tqsdk `"OPEN"/"CLOSE"/"CLOSETODAY"` ↔ CTP `CombOffsetFlag '0'/'1'/'3'`），零新增逻辑。

**② TqCtp（tqsdk 直连 CTP）—— 真存在，但是企业版付费功能**
- 类在 `tqsdk/tradeable/otg/tqctp.py:11`，签名 `TqCtp(account_id, password, front_broker, front_url, app_id, auth_code)`；
  `:47` 把 `td_url` 固定为 `zqotg://127.0.0.1:0/trade` → `tqsdk/zq_otg.py:116` 用 `Popen` 拉起本地
  `otg_adapter.exe`（`tqsdk_zq_otg` 包内）→ **交易通道确实不过信易云网关**。
- **行情不受影响，仍走天勤云**：`api.py:3640-3647` → `auth.py:177-190` 请求
  `https://api.shinnytech.com/ns` 名称服务；`base_otg.py:87-88` 对已设 td_url 直接 return，**不碰 md_url**。
  交易链路里的对手价也来自这条云行情（`SimNow.py:591/:832/:1465` 的 `api.get_quote`）。
  → 换 TqCtp **不改善行情延迟**，也不消除「天勤云不可用即交易受损」。
- **付费门槛（成本表里最容易被漏的一项）**：官方《TqSdk 企业版》原文「企业版支持直连 CTP/融航/杰宜斯等柜台，
  **专业版只能通过中继**」，需申请试用或购买（企业版公开价 ~3 万元/年）。代码侧闸门 =
  `tqsdk/api.py:3584-3591 _check_account_auth → _auth._has_feature("tq_direct")`，
  不通过即 `raise`「您的账户不支持…需要购买后才能使用」。
  **验证只需一次调用**：登录后看 `api._auth._grants["features"]` 是否含 `tq_direct`。
- **官方收益口径 = 「交易延迟平均减少 10ms 左右」**（同页）。凡遇到"毫秒~百毫秒级""秒级"的滞后诉求，
  先用这个量级对齐预期 —— **10ms 与 25~55s 差三个数量级，不可能是同一个成因**。
- 依赖关系不是"版本锁死"：`zq_otg.py:37-38` 是**下限校验**（`<3.10.1` 报错），
  METADATA 亦为 `Requires-Dist: tqsdk (>=3.10.1)`。实测 3.10.2 + 3.10.1 已满足。
  ⚠️ `requirements.txt` 里**没有** `tqsdk_zq_otg`（只有 `tqsdk>=3.2.0`）→ 换机会在运行期才报错。

**③ SimNow 直连参数的环境区分（别混用）**
- **第一套（生产仿真，交易时段与实盘一致）**：交易前置 `tcp://180.168.146.187:10201` / 行情 `:10211`（电信第一组）；
  另有 `:10202`/`:10212`（电信第二组）、`218.202.237.33:10203`/`:10213`（移动）。
- **第二套（7×24）**：`180.168.146.187:10130`/`:10131`；**官网明示不提供结算、服务时间 16:00~次日 09:00**，
  且接入地址已公告变更为 `182.254.243.31:40001`/`:40011`。
  → 它**不是第一套的备线**；用它联调恰好测不到「结算确认」「跨日仓」这两类关键场景。用前以官网「产品与服务」当日公告为准。
- 通用认证值（两套一致）：`BrokerID=9999`、`AppID=simnow_client_test`、`AuthCode=0000000000000000`（16 个 0）。

## TqSdk 成交判定（2026-09-04 实测，血泪教训）
判断委托是否真的成交，**唯一可信依据是 `order.trade_records`**（CTP 真正确认的成交回报明细）。

- ❌ `order.status=="FINISHED" and volume_left==0` —— 必要但**不充分**，
  CTP 拒单时 tqsdk 也会置成这个状态（取决于拒单时机）。
- ❌ `api.get_position()` 的持仓缓存 —— **完全不可用作判定依据**，两头误判：
  tqsdk 在 `insert_order` 后**乐观**增减缓存，CTP 拒单也不回滚。
  实测事故：4 笔 OPEN 判 "filled"，1.5 分钟后查真实账户却是 0 持仓；
  反向也出过真成交但缓存滞后 5s 被误判幽灵，导致 21 笔平仓死循环。
- ✅ `sum(trade_records[*].volume) >= 委托量` —— 权威。结构为
  `{"<trade_id>": {"volume": int, "price": float, ...}}`，只能被交易所撮合成功写入。
- 持仓缓存只配当**诊断信号**（记 WARNING），不配拥有判定权。

其它 TqSdk 要点：
- SimNow 对短连接极敏感，进程跑完就退出会被标 **"用户不活跃"**，隔几分钟重连直接
  `TqTimeoutError`。长连接要定期 `wait_update` 心跳；`wait_update` **非线程安全**，
  必须由主线程驱动，不能起后台线程。
- SimNow 不支持市价单，只能限价；CFFEX 股指期货涨跌停 = 前结算价 ±10%，越界报错误码 50。
- **offset 只接受 3 个值**：tqsdk 3.10.2 `api.py:1353` 硬校验 `offset ∈ {"OPEN","CLOSE","CLOSETODAY"}`，
  传 `"CLOSEYESTERDAY"` 直接 `raise Exception("开平标志(offset) ... 错误")`（不是 CTP 拒单，是 SDK 本地抛错，
  被 try/except 吞掉后表现为"下单失败"，极易误判为通道问题）。
  正确用法（tqsdk 文档 api.py:1201）：上期所/能源 平今=`CLOSETODAY`、平昨=`CLOSE`；
  **中金所等其它交易所一律用 `CLOSE` 按交易所规则平仓**（CFFEX 的 CLOSE = 平昨，平今才需 CLOSETODAY）。
  另：`"CLOSEANY"` 同样不在白名单（虽然语义合理）→ 别用。白名单校验共 3 处：
  `api.py:1353`、`lib/utils.py:39`、`scenario/tqscenario.py:445`。
- ~~**平仓 offset 必须按"被平持仓是今仓还是昨仓"决定**：昨仓/日期缺失 → `CLOSE`；今仓且要平今 → `CLOSETODAY`。~~
  **⚠️ 本条已于 2026-09-10 晚些时候被推翻并简化，别再用"按今昨仓选 CLOSETODAY"这套。**
  ~~正确口径（chan.py 规则 ⑸ 改造后）：**`CLOSE` 恒为平昨，代码里根本不存在 `CLOSETODAY` 分支**。~~
  **⚠️ 2026-09-18 复核已推翻**：`CLOSETODAY` 分支**后来被重新启用**，见本文件顶部「chan.py 平今分支现状」。
  用户拍板的业务规则是"今日单离场走反向**开仓**锁仓（offset=OPEN），跨日单才走 `CLOSE`"——
  靠**规则**保证 `CLOSE` 只用于昨仓，而不是靠 offset 分支（分支已删，`_close_offset` 不存在）。
  当时"昨仓发 CLOSETODAY → CFFEX 无今仓则拒单"的判定**偏严**：经核实
  **中金所本身不区分平今/平昨报单参数（没有"平今指令"）**，平仓按"先开先平"减扣，
  所以发 CLOSETODAY 未必被拒（CTP SDK 文档说非上期所平今可用 close 或 closeToday）。
  但删掉它的收益仍然成立：**消掉一个官方口径不一致的不确定项**，且 `CLOSE` 单独就够用。
  `submit(..., entry_date=)` 参数保留但**降级为纯审计**（DryRun 写进 `Order.meta`），不参与 offset 选择。


## 中金所（CFFEX）平仓规则（2026-09-10 核实，来自信易/快期官方文档《期货交易所平仓规则详解》）
**别再想当然 —— 中金所的"持仓减扣顺序"和"手续费计费顺序"是两件分开的事：**
- **持仓层面：按"先开先平"** —— 先有昨仓 1 手、今天又开同向 1 手，随后平仓 1 手，
  **减扣的是早开的昨仓**，账户保留今天开的那手。
- **费用层面（股指期货）：先按平今口径扣** —— 只要账户里有今仓，平仓时**先按平今费率（0.0345%）扣**；
  当按平今扣费的手数达到今仓手数后，后续才按平昨费率（0.0023%）处理。
  ⚠️ **推论**：只有当账户**不存在同向今仓**时，"平昨按平昨费率"的成本模型才正确。
  chan.py 在 `max_open_positions=1`（默认，运行态只有 1 笔仓）下正确；
  **一旦调到 ≥2，成本模型会低估**。
- **中金所没有"平今指令"**（不区分平今/平昨报单参数），平仓按交易所规则减扣。
  → 用 `CLOSE` 平昨仓在中金所是合法且语义正确的。
- ⚠️ **中金所不会替你拦下"平今"**：对今日单误发 `CLOSE`，交易所**会照平**并按平今费率（≈平昨 15 倍）
  收手续费，**不会拒单**。所以"规避平今费"完全靠策略规则，没有交易所侧兜底。
- 对比：**只有上期所/上期能源**才在报单参数上区分（平今必须 `CLOSETODAY`，平昨用 `CLOSE`）。


## 国内六家期货交易所规则矩阵（2026-09-11 核实，做"全品种"时直接查这里）
### ① 平今/平昨报单指令（决定 CLOSE 会不会被废单）
| 交易所 | 有无独立"平今指令" | 报 CLOSE 时 |
|---|---|---|
| 中金所 CFFEX | ❌ | 统一 CLOSE，先开先平；**同时有今仓+昨仓时默认先平今仓（按平今费率收）** |
| 上期所 SHFE | ✅ 必须区分 | CLOSE=平昨，**昨仓不足会废单** |
| 能源中心 INE | ✅ 必须区分 | 同 SHFE |
| 大商所 DCE / 郑商所 CZCE / 广期所 GFEX | ❌ | 统一 CLOSE，先开先平 |
- **关键推论**：只要保证"**CLOSE 的目标必为跨日仓**"，六家都不会废单、都不会误收平今费。
  即"今日单离场走反向开仓锁仓、跨日单才 CLOSE"这条规则 → **让系统永远不需要 CLOSETODAY**，
  天然绕开六家最大的指令差异。这条不变量应写成硬断言。
- ⚠️ 对 CZCE/DCE/GFEX 传 `close_today=True` 会**直接报错**（它们没有这个指令）。

### ② 单向大边保证金（决定锁仓要压多少钱）
| 交易所 | 盘中实时生效？ | 取消优惠时点 |
|---|---|---|
| 中金所 | ✅ 盘中实时（同品种 2014-10-27 起；国债跨品种 2015-07-10；IF/IH/IC/IM 跨品种 2019-06-03） | 国债：交割月前一交易日收盘后 |
| 上期所 / 能源中心 | ✅ 盘中实时（同品种 2013-12-27 起） | **最后交易日前第 5 个交易日**收盘结算后 |
| 大商所 | ❌ **盘中双边，盘后结算才退**（仅套利合约盘中自动单边） | — |
| 郑商所 | ⚠️ 口径冲突（一说需 14:30 前人工申请；一说同合约月份按大单边、成交后释放）**未确认** | — |
- 与是否申请套利头寸无关，自动享受；规则明确"可用资金为正但不足开一手时仍允许反向开仓"。
- ⚠️ 期货公司可能在交易所基础上加收 → 上实盘前必须向期货公司确认。

### ③ FOK / 市价指令（决定"全成或全撤"能不能用）
| 交易所 | 限价+FOK | 市价+FOK | 集合竞价期间 |
|---|---|---|---|
| 中金所 | ✅ | 市价有专属指令（一档/五档即成剩撤、即成剩转限价） | FOK 不可用 |
| 上期所 | ✅（必须配限价） | ❌（上期所无真正市价指令） | **FOK 不可用** |
| 大商所 | ✅ | ✅ | FOK 不可用 |
| 郑商所 | ❌ **仅支持 FAK** | ❌ | — |
| 广期所 | ✅ | ✅ | FOK 不可用 |
- 上期所《交易管理办法》第三十二条：限价/市价/套利指令可附加 FOK 与 FAK。

### ④ 期货"交易日"定义（夜盘正确性的依据）
一个交易日 = **前一自然日夜盘 + 当日日盘**。例：周一 21:00 夜盘属**周二**交易日；周二 21:00 夜盘属**周三**。
→ 用 **20:00 分界**（hour>=20 归次一自然日）与之一致，已推导验证两个方向都正确。
→ 周末/节假日不需精确交易日历：周五夜盘→周六，与下周一不等→判跨日，符合事实（确是昨仓）。
- 夜盘时段：上期所 21:00-01:00（金银到 02:30）、大商所/郑商所 21:00-23:00、能源中心到 02:30；**中金所无夜盘**。


## SimNow 回报字段的坑（2026-09-10 核实）
- **SimNow 环境下各交易所的成交回报 offset 都按上期所规则填**（随报单原样回），
  即使实际平掉的是昨仓、你也可能看到自己报的那个 offset（含 CLOSETODAY）。
  → **首验时绝对不要用 SimNow 回报的 offset 判断"是不是平了该平的那笔"**，要用**持仓明细变化**核对。
- 真实交易所回报本就与报单不相等：上期所/能源中心 = 报单原样；
  **大商所/广期所/郑商所/中金所：平仓一律回报 `THOST_FTDC_OF_Close ('1')`**，不分平今平昨。
- 判断撤单是"自己撤的"还是"交易所拒的"：看 `OrderSysID`（空=拒）、`ActiveUserID`（空=非自己）、
  `OrderSubmitStatus`（`InsertRejected` vs `Accepted`）。注意 FAK/FOK 未成交部分被交易所自动撤单
  属于**正常撤单不是拒单**，会带有效 OrderSysID。


## ⚠️ SimNow 仿真 vs 期货公司实盘 CTP —— 差异清单（2026-09-11 核实）
> 用户硬要求：**凡与 CTP 交互或依赖 CTP 返回的逻辑，务必同时确认 SimNow 与实盘柜台**，
> 否则"SimNow 验证成功"的代码切到实盘可能跑不通。改报单/持仓/成交判定代码前先对照这里。
- **SimNow 不撮合**：价格合适（与对手价比较）就**直接全部成交**，**不会部分成交、不会全撤、不会自成交**。
  实盘 FOK 要求**盘口深度 ≥ 委托手数**否则全撤。
  → **追价循环 / 全撤分支 / 部分成交处理：SimNow 全绿也零证据力**，必须用 FakeApi 契约测试覆盖。
- **SimNow 第二套（7x24）环境不提供结算**（官网明示），账户/钱/仓与上一交易日一致
  （服务时间 16:00~次日 09:00）→ **昨仓恒定，跨日平昨逻辑测不出来**；
  跨日验证要用**第一套环境**（服务时间同生产、有结算）或实盘小资金。
- **中金所报撤单监管**（《异常交易管理办法》2026-04-30 修订，**2026-06-01 施行**）：
  单日单合约撤单 **≥500 次** = 频繁报撤单；**FOK/FAK 形成的撤单计入统计，普通限价单撤单反而不计入**。
  → 全 FOK 策略每次全撤都在累加计数；"资金不足"这类追价无用的场景白追最贵（拒单分类器的合规理由）。
  另：高频交易实行**报撤单收费**（《中金所程序化交易管理办法》2025-08-08 第三十条）。
- **结算单确认**：**每个交易日须确认一次才能下单**（tqsdk Go/JS SDK 文档明示；Python 版未明示是否自动）
  → **实盘首日必验**；不自动则每日首单被拒（SimNow 不暴露，尤其 7x24 环境根本不结算）。
- **程序化交易须报备**（同上办法）：未报备即违规，属上线前合规前置，非代码问题。
- **价格保护带在生产有效**（《中金所风险控制管理办法》中金所发〔2020〕36 号 第三章第八条：
  价格限制制度 = 熔断 + **价格保护带** + 涨跌停板）。带宽 **IF 30 / IH 20 / IC 60 点**，
  越带限价单被系统自动拒绝；**集合竞价不适用**。超价调大或换品种前先算带宽。
- 报单流控：期货公司前置典型 **9 次/秒**（报单+撤单合并计），客户端应自限 ≤7 次/秒。
- 实盘登录常见报错：`CTP:不合法登录`（天勤默认连 CTP **主席**，需切换席位）、
  `CTP:客户端认证失败`（AppID 未与期货账户绑定，需联系期货公司）。
- **两边完全一致、可放心依赖**：`order.trade_records` 成交判定（P6 权威判据）、
  持仓 `pos_long_today` / `pos_long_his` 字段。
- tqsdk 实盘接入：`TqAccount(期货公司名, 账号, 密码)` —— **走天勤中继**（期货公司须在天勤支持列表内）；
  若期货公司只给直连资格 → `TqCtp` + `pip install tqsdk_zq_otg`（要改连接层）。


## CTP 错误码速查（2026-09-11 核实，做期货报单错误处理时直接查这里）
来源：CTP_API 错误代码大全（cnblogs）+ 申银万国期货《常见交易报错和原因》。
报单被拒时 tqsdk 的 `order.last_msg` 里就是这些文本（含错误码）。

| 码 | 提示 | 含义 |
|---|---|---|
| **31** | `CTP:资金不足` | **可用资金不足，无法开仓**（也可能被未成交挂单占用，撤单后可恢复） |
| 17 | `CTP:合约不能交易` | — |
| 28 | `CTP:没有报单交易权限` | 品种权限未开（如铁矿石/PTA 特定品种） |
| 29 | `CTP:只能平仓` | 休眠户 / 证件到期 |
| 30 | `CTP:平仓量超过持仓量` | 本地簿面与柜台不同步时易触发 |
| 50 | `CTP:平今仓位不足` | 也用于 CFFEX 涨跌停风控拒单（曾实测到） |
| 51 | `CTP:平昨仓位不足` | — |
| 42 | `CTP:结算结果未确认` | 登录后需先确认上一日结算单 |

⚠️ **"非交易时段"没有稳定的单一数字码**，各期货公司文本不一，常见：
`CTP:已撤单,报单被拒绝 当前状态禁止此操作`（申银万国官方释义=当前非交易时间）、
"不在交易时间"、"非交易时间段"。
→ 写分类器必须 **数字码 + 中文关键字双管**，并留兜底分支；关键字表最好实盘抓几天
真实 `last_msg` 样本再校准。

## tqsdk Quote 的"到期/交割"字段真名（2026-09-17 实测 · 关键）

- tqsdk 3.10.2 的 Quote **没有 `last_trade_date`，也没有 `expiry_datetime`**；
  真名是 **`expire_datetime`**（epoch **秒**，如 `1789714800.0` = 2026-09-18 15:00 本地）
  + 便利字段 **`expire_rest_days`**（剩几天到期，如 1）。
  证据：全包 grep `last_trade_date` **0 命中**、`expiry_datetime` **0 命中**、
  `expire_datetime` **76 命中**；实测 `quote.last_trade_date` / `quote.expiry_datetime` 都是 `None`。
- 同期可取：`price_tick` / `volume_multiple` / `upper_limit` / `lower_limit` / `pre_settlement`
  / `margin`（一手保证金）/ `commission` / `trading_time`（`{"day": [["09:30","11:30"],["13:00","15:00"]], "night": []}`
  —— 股指无夜盘）/ `open_interest` / `expired` / `ins_class`。
- **教训**：凡"从行情取某字段"的代码，名字必须拿**真实 Quote 的 dir() dump 核过**，
  不能用猜的名字——写错名字 `getattr` 返回 None 不报错，护栏会静默空转，
  而单测里手搓的假 quote 反而带那个假字段 → 测绿链路死。
- 连 SimNow 的最小可用写法（与 chan.py `Broker/SimNow.py` 同构，**3 个位置参数**）：
  `TqApi(TqAccount("simnow", SN_ACCOUNT, SN_PASSWORD), auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))`。
  实盘则 `TqAccount(期货公司名, LIVE_ACCOUNT, LIVE_PASSWORD)`。
- 只读体检脚本（不下单）：`Trading/Tool/SimNow/SimNowProbe.py --symbol "KQ.m@CFFEX.IF"`；
  强平工具：`Trading/Tool/SimNow/SimNowForceClose.py --close-all`。

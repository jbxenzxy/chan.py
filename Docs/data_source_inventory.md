# 数据源清单

> 基于 `custom-dev` 分支（commit `801068db2c8b`，2026-09-12）代码核查。
> 口径：**信息 → 收口适配器 → 取数函数 → 底层真实数据源 → 消费方**。
> 所有行号对应该 commit（行号口径 = `content.count("\n")`，等价 `wc -l`）；函数名后括注定义行，便于定位。
>
> **本版相对上一版（`fde4a1dab9d0`，2026-09-11）刷新了以下内容**：
> ① **PE-TTM / 流通市值从腾讯行情迁到 eltdx**；「按市场 / 标的类型选源」这一业务
   编排规则收口在 App 层 `App/AppRefresh.py::_fetch_pe_ttm_live`（2026-09-12 评审后从
   `DataAPI/MarketStatsAPI.py` 移出，该文件已删除——DataAPI 层各模块应一一对应真实数据源）；
> ② **PE-TTM 再进一步：由「点刷新落盘缓存」改为「打开 K 线页面时实时取数」**（见第一节）。
>    理由：PE 每日随行情变动，而落盘缓存只在点刷新时更新——实际使用中不会每天点刷新，页面读到的是陈旧 PE。
>    随之 `stock_pettm_index.json` 只保留指数归属并**改名 `stock_index_belong.json`**（旧文件自动迁移、保留不删），
>    刷新按钮不再刷 PE（见第三节），新增 `Test/test_pe_ttm_live.py` 守护；
> ③ 上一版基线之后 `DataAPI/TdxAPI.py` 被重构过一次（**2355 → 2067 行**：pytdx 整体退场、板块刷新改走 eltdx、
>    `880xxx` 的 Step4 兜底分支删除、标准指数路由合并为 `_read_standard_index_stocks`），
>    因此第二 / 三节与附 C / 附 D 的**事实与行号一并重取**，不再沿用旧值。

---

## 〇、K 线主数据源（chan 分析入口）

全部 K 线经 `DataAPI/__init__.py:get_stock_api()` 单一工厂分派，**App 层不直连具体数据源类**：

| 代码类型 | 适配器类 | 底层真实数据源 | App 层装配点 |
| --- | --- | --- | --- |
| 期货 / 期指（含 `CFFEX`/`SHFE`/`DCE`/`CZCE`/`INE`/`GFEX`/`SGX` 前缀，或 `KQ.m@` / `KQ.i@` / `KQD.m@`） | `TqSdkAPI.CTqSdkAPI` (`:216`) | **天勤 tqsdk**（SSE 实时接入；历史 K 线经 `fetch_futures_kline` `:398`） | `AppSSE.py:1219` `data_src="custom:TqSdkAPI.CTqSdkAPI"` |
| 其他（A股 / 指数 / 板块 / 港股） | `TdxAPI.CTdxAPI` (`:983`) | **通达信本地 vipdoc**：日线 `.day` / 5分钟 `.lc5` / 1分钟 `.lc1`；周线与 15m/30m 由本地合成；**仅前复权需联网**（eltdx xdxr） | `AppEngine.py:699 / 717 / 739 / 755` `data_src="custom:TdxAPI.CTdxAPI"` |

说明：

* 本地文件路径规则：A股 `{vipdoc}/{sh|sz|bj}/lday/{market}{code}.day`；港股 `ds/lday/31#{code}.day`（指数为 `27#HZ{code}.day`）。周线由日线合成，15m/30m 由 5m 合成。
* 前复权默认关闭，经 `set_tdx_config(forward_adjust_enabled=True)` 开启；开启后前复权流水线才向 `ElTdxAPI` 取 XDXR。
* `CTdxAPI` 的 K 线数据由 `tdx_data_context()` 每请求线程局部注入，实例只在 `__init__` 绑定一次快照引用；脱离上下文直接实例化会返回空而非静默读到他人数据。

---

## 一、六类信息的数据源

| 信息 | 收口模块 | 取数函数 | 底层真实数据源 | 消费方 |
| --- | --- | --- | --- | --- |
| 除息除权 (XDXR) | `DataAPI/ElTdxAPI.py` | `get_xdxr_data(market, code)` (`:235`) | **eltdx**（通达信网络行情，7709 协议 / `0x000f` 命令）· **单一数据源** · **按需逐只取数**（`capital_changes` 是按代码查询的协议命令，非一次性全市场文件，故不与 PE-TTM 同样做成全量预取） | `TdxAPI` 前复权流水线（`TdxAPI.py:672` 导入） |
| 重要股东买卖（减持计划） | `DataAPI/ElTdxAPI.py` | `get_shareholder_reduction_flag(market, code, today)`（`:720`）+ `get_shareholder_reduction_plans`（`:632`）+ `_f10_tqlex_post`（`:600`） | **通达信 F10 7615 网关** `CWServ.tdxf10_gg_gdyj` + section `gdzjcjh`（股东增减持计划）· **按代码查询**（非全市场文件，与 xdxr 同类）· 进程缓存 1 天、不落盘 · 仅覆盖 sh/sz/bj · 若当前交易日 ∈ 某条「拟减持」计划的**公告日~变动截止日**（`N001`~`N010`，`N001` 缺失回退 `N009`）→ 命中（进度 `N011` 仅「进行中」计入，「完成」/「停止实施」已释放/取消的抛压剔除，未知状态保留）；所有命中窗口**合并为一个最大连续区间**（最早起始~最晚截止，因命中窗口均含当日、并集无空洞）→ `windows` 恒 1 条，徽标展示「`减持:√ 07-14~11-03`」 | **请求由 `_f10_tqlex_post` 直发：强制 IPv4 + 禁代理**（eltdx F10Client 的裸 urlopen 在本机 IPv6 出口不通时，每次卡 8~12s 才回落 IPv4——「K 线页加载多 8 秒」的根因；IPv4 直连实测 117~130ms）。AppEngine `_get_reduction_flag` **同步**调用注入 `meta.shareholder_reduction`（`AppEngine.py:1259`）→ `Frontend/app.js` 徽标（置于「归属」之后）。曾有版本把取数拆成独立 `/reduction` 端点 + 前端异步补取，**已废弃**（用户要求同步、易排查） |
| PE-TTM（A股个股） | `App/AppRefresh.py` | `_fetch_pe_ttm_live(market, code)` → `_eltdx_fetch_pe_ttm_all` → `ElTdxAPI.fetch_pe_ttm_all` | **eltdx**：`0x06B9` 服务器文件读取 → `zhb.zip` 内 `tdxstat.cfg` 第 3 列（滚动市盈率）· **单请求覆盖全市场**（取单只与取全市场耗时相同，实测 1.2~1.4s）→ 故做成**进程级全量缓存**：进程内首次取数拉全 A 股并落盘 `App/stock_pettm.json`，之后一律命中内存 | `AppRefresh` 注入 `AppData`（PE 实时层） |
| PE-TTM（指数 / 港股） | `App/AppRefresh.py` | `_fetch_pe_ttm_live(market, code)` → `TxAPI.fetch_pe_ttm` | **腾讯行情** `qt.gtimg.cn/q=`，字段 `[39]`（实测为 **TTM / 滚动**口径）。**指数必须走腾讯**：eltdx 统计文件只含 A 股个股，指数走 eltdx 必然取空（2026-09 修复「输入指数不显示 PE-TTM」） | 同上 |
| PE-TTM（A股批量入口） | `DataAPI/ElTdxAPI.py` | `fetch_pe_ttm_all()` (`:481`) | 返回 `{mkt+code: pe}`，**不做市场分流**（分流属 App 层编排） | `AppRefresh.py:129`（进程级全量缓存的唯一取数点） |
| 流通市值（A股） | `App/AppScan.py` | `fetch_float_mc_all(stock_list)` (`:340`) → `ElTdxAPI.fetch_float_mc` (`:518`) | **eltdx**：`0x0010` 流通股本 × `0x054c` 最新价，单位由「元」换算为「亿元」 | `AppScan.py:589` 扫描前置取数 → `AppScan.py:317` 阈值判定 |
| 股票名字（A股） | `DataAPI/SinaAPI.py` | `fetch_a_names(mkt_code_pairs)` (`:39`) | **新浪财经** `http://hq.sinajs.cn/list=`（`SinaAPI.py:18`），字段 `[0]`（GBK） | `AppRefresh.py:171`（第一轮） |
| 股票名字（港股） | `DataAPI/TxAPI.py` | `fetch_hk_names(hk_codes)` (`:91`) | **腾讯行情** `qt.gtimg.cn/q=`，字段 `[1]`（GBK） | `AppRefresh.py:182`（第二轮） |
| 指数归属 | `DataAPI/AkshareAPI.py` | `fetch_index_cons(index_code)` (`:34`) | **AKShare `index_stock_cons_csindex`**（中证指数公司 csindex） | `AppRefresh.py:194`（线程池，每指数 30s 限时）；落盘 `stock_index_belong.json`（`AppData.save_index_belong_cache` `:1187`） |

说明：

* A股与港股股票名称分属两个数据源（新浪 vs 腾讯）：新浪港股接口已失效，港股改走腾讯并放宽批次间隔至 0.3s 以避限流。
* **A股名称的 list= 参数必须「market 前缀在前」**（`sh600519`）。新浪对非法参数**返回 HTTP 200 +
  `hq_str_sys_auth="FAILED"`** —— 不抛异常、也不打日志，属于典型的静默失败面。2026-09-12 实测事故：
  拼接写成 `f"{m}{c}"` 把顺序解反 → 请求 `list=600519sh` → **5367 只 A 股名称整体丢失**，
  刷新随即用残缺表覆盖名称表（页面名称退化成代码、拼音搜不到，且重启不恢复）。
  现由 `_normalize_sina_pair`（`SinaAPI.py:24`）对两种入参顺序做归一（容错），
  且整批无有效返回时输出 WARNING（`SinaAPI.py:97`）让失败留痕；
  守护用例 `Test/test_sina_name_pairing.py`（不联网，锁 URL 顺序与告警）。
* **名称表落盘前有质量门槛**：新表条目数不足旧表 `_REFRESH_NAMES_MIN_KEEP_RATIO`（`AppRefresh.py:64`，0.5）
  时视为补全链路异常，**保留旧表、不覆盖内存、不落盘**（`AppRefresh.py:488`）——
  防「一次坏刷新把完好名称表永久换成残表」。
* **PE-TTM 与流通市值自 2026-09-12 起由 App 层 `App/AppRefresh.py` 收口选源**：
  A 股（`sh`/`sz`/`bj`）→ eltdx，港股（`hk`）→ 腾讯。分流规则**全项目只有这一份**（实现为 `_split_by_source` `:39`，两个公开取数函数共用），
  调用方（`AppRefresh.py:29` / `AppScan.py:41`）只 import 该模块，不再直接 import `ElTdxAPI` / `TxAPI` 取这两个口径
  （`Test/test_phase4_guards.py` ⑩ 已把装配点白名单固化）。
  之所以必须收口：**两个源都不是全能的**——eltdx 无港股通道（`codes.all("hk")` 抛 `ValueError`、`0x054c` 快照对 hk 不可用），
  腾讯则不提供北交所全量统计口径；把"谁管哪个市场"写在各调用方，规则就会有副本并随数据源变动漂移。
* **PE-TTM 自 2026-09-12 起改为「打开 K 线页面时实时取数」，不再落盘**（`AppData.get_pe_ttm` `:1230`）：
  常态下**每次调用都经 `_ensure_pe_ttm_live`（`:1246`）向注入的取数实现取一次**，取到即写入内存表 `_pe`。
  并发请求由 `_pe_live_lock` 做 **single-flight**——只有抢到锁的线程真去联网，其余立即返回当前表（**不排队等待**），
  避免一个卡住的网络请求把整批请求一并拖住；取数失败**静默降级**（保留旧值、返回 `None`，不向上抛），
  K 线接口不会因元数据取数失败而整体 500。
  - **为什么不做时间缓存**：本软件不提供实时行情（K 线读本地 vipdoc），PE 的新鲜度只需对齐「打开这个标的」这一动作；
    且 `tdxstat.cfg` 是**整体文件**（取单只与取全市场耗时相同，实测 1.2~1.4s），故「每次打开取一次」在体验上已可接受。
  - **依赖倒置**：`AppData` 不得 import `DataAPI`（`Test/test_phase5_guards.py` ④b 防影子双源），
    取数实现由 `AppRefresh.py:42` 在模块导入时经 `set_pe_ttm_live_fetcher`（`AppData.py:1220`）注入。
  - **冻结语义**：`_pe_loaded` 为真表示「PE 表已由外部提供」→ 实时层被短路（纯点查）。
    快照用例（`Test/snapshot_runner.py` / `Test/test_trigger_step_replay.py`）正是注入 `_pe = {sh600519: 20.0}` 后置位该旗来冻结基线，
    故实时化**未破坏快照确定性**（零改动即通过）；`Test/test_pe_ttm_live.py` 为本次新增的专项守护（8 项，全程打桩不联网）。
* **PE-TTM 与指数归属的落盘缓存已拆分**：`stock_pettm_index.json`（PE + 指数归属合并）→ **`stock_index_belong.json`（只存指数归属）**。
  理由：PE 每日随行情变动、指数归属季度调仓才变，两者时间维度不匹配，混在一个文件与同一次刷新里必然产出陈旧 PE。
  旧文件**自动迁移**（`AppData.load_index_belong_cache` `:1134`：新文件不存在而旧文件在时，读其 `index` 字段写成新文件），
  旧文件保留不删（可回退）；`AppConfig.stock_index_belong_file`（`AppConfig.py:289`）为唯一定义点，
  旧路径由 `legacy_stock_pe_ttm_file`（`:301`）显式持有（**仅供迁移读取**）。落盘只写 `{"sh600519": {"index": "沪深300"}}` 形态
  （`AppData.save_index_belong_cache` `:1187`，含空表保护：内存表为空时不落盘，避免网络失败抹掉既有归属）。
* **改造前后等价性实测（2026-09-12；真实 `stock_names.json` 代码集 + 全市场 8037 只对照）**：
  - PE-TTM：真实 A 股股票**零缺失**——旧有值而新无值的 144 个**全部是指数 / 板块代码**（附 D-5），股票侧无损失；港股覆盖 2583 只**完全不变**。
  - 流通市值：A 股股票 5553 只中 5462 只（98.4%）落在腾讯「亿元两位小数」的取整半格内，其余 91 只差异根因是 eltdx `0x0010`
    流通股本滞后于解禁 / 增发（附 D-6）；**基金 / REIT 判据不同**（腾讯 `[44]` 对该类返回"份额 × 价"，与其 `[45]` 同值），详见附 D-6。
  - 取数耗时：PE 12.9s（27 批 HTTP）→ **1.2s**（`read_stats` 单请求，实测 1.16~1.36s）；流通市值 ≈9~14s（27 批）→ **6.7s**（全市场，含坏代码逐只隔离重试）。
* **除息除权现为 eltdx 单一数据源，`mootdx` / `pytdx` 三级回退已于 2026-09 整体删除**（不是"注释保留"）。设计意图是失败即显著报错（`ElTdxAPI.py:272` `log.error`）而非静默降级，避免把「网络/接口故障」伪装成「该股无除权除息数据」。如需新增数据源，在 `get_xdxr_data` 的来源元组中追加即可，**不要**恢复静默 `continue` 式降级。
* 指数归属的 `AKSHARE_INDEX_MAP` 只覆盖 4 个指数（`000300` 沪深300 / `000905` 中证500 / `000852` 中证1000 / `000688` 科创50），与第二节的成分股取数共用同一个 `fetch_index_cons`。
* 腾讯 `_iter_records`（`TxAPI.py:41`）按行前缀 `v_sh` / `v_sz` / `v_hk` 判定市场；改造后**只剩「港股名称」与「港股 PE-TTM」两处在用它**（A 股 PE 与流通市值已迁至 eltdx）。腾讯字段 `[39]` 的解析见 `TxAPI.py:79`。

---

## 二、股票扫描 · 成分股的获取方式

入口：`TdxAPI.get_index_stocks(sector_code)`，**定义于 `DataAPI/TdxAPI.py:1417`**。
上游：`AppScan.py:503`（`Scanner.stock_list` 的 `page_index` 来源，`AppScan.py:534` 注册）→ `FrontAPI.py:358` `GET /api/stocks/scan/read/candidates`。

| 板块代码类型 | 取数方式 | 底层真实数据源 | 代码位置 | spblock.dat 能否取得（本地离线，本机实测） |
| --- | --- | --- | --- | --- |
| `881xxx` 研究行业（新版） | `_read_tdxhy_sector_stocks` | 通达信本地行业配置 `T0002/hq_cache/tdxhy.cfg`（经 App 层注入的 `X↔881` 映射，映射源 `tdxzs3.cfg`） | 分派 `:1431` / 读取 `:1988` / 解析 `_parse_tdxhy_cfg` `:1925` | **否** — spblock.dat 只含交易所／指数公司宽基 + ETF + 债券，不含研究行业分类 |
| 港股指数 `HSTECH` / `HSIDI` | `_read_hk_index_stocks` | 恒生指数公司官网 `hsi.com.hk` Factsheet **PDF** | 分派 `:1438` / 读取 `:1789` | **否** — A股本地文件，不含港股指数 |
| `000001` 上证指数 | `_read_sh_index_stocks_exchange` | 上交所官网 `query.sse.com.cn/sseQuery/commonQuery.do`（主板A `STOCK_TYPE=1` + 科创板 `8` 两段合并） | 分派 `:1869` / 读取 `:1563` | **否** — spblock.dat 无 上证指数（其成分股＝全部沪市，走专用接口） |
| 中证指数 `000300/000905/000852/000688` | `_fetch_csi_index_stocks` → `AkshareAPI.fetch_index_cons` | AKShare / csindex（中证指数公司） | 分派 `:1876`（`CSI_INDICES` `:1875`）/ 读取 `:1831` | **部分** — 中证500(000905)／中证1000(000852) **是**（各 500／1000 只）；沪深300(000300)／科创50(000688)／中证800(000906) **否**（走 infoharbor_block.dat 的 ZS 段） |
| `399xxx` 深交所指数 | `_read_standard_index_stocks` **内联**（深交所官网 XLS 直连） | `www.szse.cn/api/report/ShowReport`（`CATALOGID=1747_zs`，`SHOWTYPE=xls`） | `:1880` ～ `:1912` | **部分** — 深证成指(399001)／国证2000(399303) **是**（各 500／2000 只）；创业板指(399006)／深证100(399004) 等其余 399xxx **否** |
| 其他指数（`000xxx` 非中证 / `932xxx` / `000510` 等） | `_fetch_csi_index_stocks`（末尾兜底） | AKShare / csindex（中证指数公司） | `:1915` | **个别** — 仅超大盘／超小盘宽基：中证2000(932000)／中证A500(000510) **是**（各 2000／500 只）；上证50(000016)／上证180(000010)／上证380(000380) 等其余 **否** |
| `880xxx` 概念 / 风格板块 | `_read_infoharbor_sector_stocks` | 本地 `T0002/hq_cache/infoharbor_block.dat` | 分派 `:1446` / 读取 `:1405` / 解析 `_parse_infoharbor_block` `:1304` | **否** — spblock.dat 不含概念／风格板块 |
| `8803xx` / `8804xx` 旧版行业 | **已无专门分支** —— 落入 `880xxx` 路径；本机 `tdxzs.cfg` 有 132 个此类代码，infoharbor 未命中 → 警告 + 返回空 | （无成分股数据） | 跳过逻辑移至册名阶段（`AppRefresh.py:383`） | **否** |

路由顺序（`get_index_stocks` `:1417`，**3 步 + 末尾兜底，无任何联网下载成分股的路径**）：

1. `881xxx` → `_read_tdxhy_sector_stocks`（`:1431`）；
2. 港股指数（判定用 `_HK_INDEX_CODES` `:201`）→ `_read_hk_index_stocks`（`:1438`）。**必须早于第 3 步**——源码注释指出：否则 `HSTECH` 之类字母代码会落入中证接口 `index_stock_cons_csindex`，返回非 Excel 内容抛 `Excel file format cannot be determined`；
3. 非 `88` 开头 → `_read_standard_index_stocks`（`:1442`）；其内部再分派 `000001` / `CSI_INDICES` / `399xxx` / 兜底（`:1855`～`:1915`）；
4. `880xxx` → `_read_infoharbor_sector_stocks`（`:1446`）；未命中即 `log.warning` + 返回 `[]`（`:1451`），提示点「刷新」重下 `infoharbor_block.dat`。

说明：

* **旧版「Step4 兜底」链路已整体删除。** `_download_block_gn_from_network` 与 `_debug_read_page_index_stocks` 中的 pytdx 服务器下载分支在 pytdx 退场时一并移除（全仓已无该函数定义，App 层的 `_debug_read_page_index_stocks`（`AppScan.py:355`）现在只是 `get_index_stocks` 的薄封装）。因此 `880xxx` 未命中**不再有本地 block 文件联网兜底**，恢复手段只有点「刷新」。
* **市场前缀以 csindex 的「交易所」字段为权威**（`_index_cons_to_stocks` `:1481`）；仅在拿不到时按代码首数字兜底（`_prefix_by_digit` `:1462`，映射表 `_MARKET_PREFIX` `:1459`）。此前按 `first in "689"` 的旧规则会把北交所 `8xxxxx`/`920xxx` 误判为沪市。
* 所有网络抓取均经 `_run_with_timeout`（`:1528`）限时，避免扫描因网络阻塞卡死。
* `infoharbor_block.dat` 实测 547 段（`#GN_` 269 / `#FG_` 161 / `#ZS_` 117），但解析后**带代码板块只有 420 个**（`_read_infoharbor_blocks` `:1380`）——`declared_count` 与实际成员数不一致的段被一致性检查过滤（`:1374`）。这是 `880xxx` 可能"段存在却返回空"的直接原因之一。
* `881xxx` 依赖 App 层启动时注入的 `X↔881` 映射（`set_tdx_hy_mapping` `:146`）：权威源 `tdxzs3.cfg` 实测 **467 条（一级 30 / 二级 128 / 三级 309）**。**裸调 `DataAPI/TdxAPI.py`（不经 App 装配）时该映射为空，`881` 路由会静默返回 0 只** —— 排查 `881` 取数异常时先确认映射是否已装配。
* `880xxx` 已确认无 400 只上限（走 `infoharbor_block.dat`，实测 `880861` 取到 1103 只）；**400 只上限只存在于 `block_*.dat` 路径**（见第四节，该路径当前已不被 `get_index_stocks` 使用）。
* **`spblock.dat` 仅覆盖「特定宽基」，不是「所有上交所／深交所／中证指数」的通用源**（2026-09-12 本机 `D:\new_tdx_hd_test` 实测，共 35 个块）。实测含成分股的宽基仅 6 个：中证500(500)／中证1000(1000)／中证2000(2000)／中证A500(500)／国证2000(2000)／深证成指(500)；另含 ETF（沪深股通ETF 447）、债券（上证／深证做市债券 725／222）、基金（创业和科创基金 187）等非指数块。**不含** 上证指数、上证50／180／380／580、沪深300、中证800、中证100／200、科创50／100、深证100、创业板指／50、国证1000 等。**两文件对宽基指数是「互补存储、不重叠」**（2026-09-12 实测，非按「成分股数 ≤ 800」划分——中证500(500)／深证成指(500)／中证A500(500) 虽 ≤800 却在 `infoharbor_block.dat` 的 ZS 段为空，而中证800(800) 虽 >500 却在 ZS 段有 800 只）：上述 6 个宽基**只**在 spblock.dat；沪深300、中证800、上证50／180／380／580、科创50／100、深证100、创业板指／50 等**只**在 `infoharbor_block.dat` 的 ZS 段（实测各有 50～800 只成员）。**例外**：上证指数、国证1000 在本快照两份文件里均为空（未固化成员列表，通达信可能另算）。因此「扩展 → 成分股」的本地二分是：spblock.dat 存的 6 个宽基走 spblock.dat，其余标准／中小盘宽基走 infoharbor_block.dat 的 ZS 段；`88` 开头（881 研究行业 + 880 概念风格）则分别来自 `tdxhy.cfg` 与 `infoharbor_block.dat`，**均不在 spblock.dat 内**。该列记录的是「可作为离线兜底的真实本地源」，chan.py 当前并未读取 spblock.dat（指数归属仍走 AKShare，见第一节）。

---

## 三、板块文件刷新（`TdxAPI.refresh_block_files`）

| 项 | 内容 |
| --- | --- |
| 入口 | `TdxAPI.refresh_block_files(progress_callback)`，定义 `DataAPI/TdxAPI.py:1234` |
| 上游 | 前端「刷新」按钮（`Frontend/index.html:71`，title 已去掉 PE-TTM）→ `POST /api/stocks/refresh`（`FrontAPI.py:639`）→ `AppOrch` 再导出 → `AppRefresh.refresh_stock_names_async` (`:554`) → `_refresh_stock_names` (`:301`) → `refresh_block_files` 调用点 `AppRefresh.py:523` |
| 底层数据源 | **eltdx `0x06B9` 通用文件读取**（`ElTdxAPI.download_block_files_via_eltdx` `:324`）：单次连接内顺序下载，单文件上限 8 MB（`ElTdxAPI.py:299`）；服务器列表由 `TDX_BLOCK_SERVERS`（**16 台**，`TdxAPI.py:231`）转成 `host:port` 传入（`TdxAPI.py:1268`） |
| 刷新目标 | `{TDX_INSTALL_DIR}/T0002/hq_cache/` 下 `tdxhy.cfg`、`infoharbor_block.dat`、`spblock.dat`、`block_zs.dat`、`block_gn.dat`、`block_fg.dat` 共 6 个文件（`TdxAPI.py:1259`）。`block.dat` 已移除：其 100 个块全部已被 `block_zs/gn/fg` 覆盖（实测独有块=0），属冗余旧整合文件，见第四节 |
| 安全策略 | 不先删旧文件；先下到内存 → `_validate_downloaded_block_file`（`:1189`）逐文件格式校验 → 写 `.tmp` → `os.replace` 原子替换（`_safe_replace_file` `:1159`）；任一文件失败即保留旧文件 |
| 日志格式 | `[板块刷新] ✅ {文件名}{（作用说明）}刷新成功: {字节数} 字节`；括号说明由 `_BLOCK_FILE_DISPLAY`（`:1179`）提供，值为 `None` 则不加括号说明（`block_gn.dat` / `block_fg.dat`） |
| 缓存失效 | 刷新后清空 `_INFOHARBOR_BLOCK_CACHE`（`:1296`）与 `_TDXHY_CACHE`（`:1299`～`:1301`），下次扫描重读新文件 |

> **刷新按钮已不再刷 PE-TTM**（2026-09-12）：PE 改为「打开 K 线页面实时取数」后，本流程的第 4 步由
> 原 `_refresh_pe_ttm` 变为 **`_refresh_index_belong`（`AppRefresh.py:239`）**——只刷指数归属并落盘
> `stock_index_belong.json`（不再写 `pe_ttm` 字段）。前端按钮 title 同步改为「刷新股票名称、板块文件、指数归属」。
> 刷新流程顺序：股票名称 → 板块文件 → 指数归属。

> **pytdx 已从本项目彻底退场。** 源码 0 处引用（全仓仅 `.venv` 内 `tdxpy` 包自身文档注释提到上游仓库）、`requirements.txt` 未声明、`mootdx` 同样 0 处。板块刷新是最后一块迁移点，改走 eltdx `0x06B9` 后 **pytdx 不再承担任何功能**——第二节的 Step4 兜底链路也在同一次改造中移除。

> **`spblock.dat` 刷新现状（2026-09-12 实测）**：eltdx 的通用文件下载通道**不服务 `spblock.dat`**——`ElTdxAPI.py:297` 已注明「实测返回 0 字节」。因此点刷新按钮时 `spblock.dat` 会落到「服务端 size 无效，保留旧文件」分支（`TdxAPI.py:1281`），**当前实际不刷新**。它由通达信客户端的「盘后 / 板块下载」单独写入，chan.py 只读取、不负责刷新。`_validate_downloaded_block_file` 仍保留 `spblock.dat` 的 GBK 文本校验分支（`:1194`），便于将来该通道可用时立即可用。

---

## 四、通达信「扩展 → 成分股」界面真实来源全景

> 本节描述的是**通达信软件自身**「指数页 → 扩展 → 成分股」这一界面动作的数据来源，
> 与第二节「chan.py 扫描时如何取成分股」是两回事（chan.py 走 AKShare / 本地 block 文件兜底，见第二节）。
> 本节结论来自 2026-09-12 对本机 `D:\new_tdx_hd_test\T0002\hq_cache\` 下各文件的**二进制 / 文本实测枚举**。

### 4.1 总机制

通达信在「扩展 → 成分股」时**先查本地缓存、本地没有再联网按需拉取**，并非单一文件、也并非只有两条路：

1. 先在本地的三个 block 文件里查该指数的成分股段：`block_zs.dat`（1999 老格式）、`spblock.dat`（新版文本）、`infoharbor_block.dat`（现代 `#ZS_/#GN_/#FG_` 段）。三者**互补 + 部分重叠**，不是谁替代谁。
2. `88` 开头板块单独走：`881xxx` 研究行业读 `tdxhy.cfg`；`880xxx` 概念/风格读 `infoharbor_block.dat`。
3. **上证指数（000001）是特例**：全市场指数，不在任何 block 文件，通达信用「全部沪市股票」逻辑呈现（源自 `shs.tnf` / 专用接口）。
4. 若三个 block 文件都查不到（如 `国证1000` 本机未缓存），通达信会**联网从指数公司 / 服务端按需拉取**并写回本地缓存——证据：同属国证的 `国证2000` 已缓存在 `spblock.dat`，而 `国证1000` 未缓存 → 是**缓存状态差异**而非结构性缺失。

### 4.2 各指数类型的真实来源（本机实测）

| 指数类型 | 本地源文件 | 格式 | 实测成员数（本机快照） |
| --- | --- | --- | --- |
| 中小盘 / 标准宽基（沪深300、中证800、上证50/180/380/580、科创50/100、深证100、创业板指/50、国证成长/价值、中证央企…） | `block_zs.dat` **与** `infoharbor_block.dat` 的 ZS 段（两者重叠覆盖） | 老格式：384 字节头 + 每记录 13 字节头 + 2800 字节定长；现代：`#ZS_` 文本段 | 沪深300=300、中证800=800、上证580=400、国证成长/价值=332… |
| 超大盘 / 超小盘宽基（中证500、中证1000、中证2000、中证A500、国证2000、深证成指） | **`spblock.dat`**（其余两文件对应段为空） | GBK 文本：`#板块名` 头 + 每行一个 7 位代码（首位=市场 0深/1沪） | 中证1000=1000、中证2000=2000、国证2000=2000、中证500=500、中证A500=500、深证成指=500 |
| `881xxx` 研究行业 | `tdxhy.cfg` | GBK 文本：`市场｜代码｜旧T码｜｜｜X码` | 467 个行业 |
| `880xxx` 概念 / 风格板块 | `infoharbor_block.dat`（`#GN_`/`#FG_` 段） | 现代文本段 | 概念 421+、风格 161 段 |
| 上证指数（000001） | 特例：全部沪市股票（`shs.tnf` / 专用逻辑） | — | 非离散块 |
| 未缓存指数（如 `国证1000` 399311） | 联网按需拉取（不在任何本地 block 文件） | — | 见 4.1 |

> 关键纠正：此前的「两条路 / ≤800」结论是错的——
> 中证500(500) 在 spblock、`block_zs.dat` 里中证央企=400 到顶而深证成指仅 39 残桩，无任何数字边界；
> 真实是 **block_zs.dat + spblock.dat + infoharbor_block.dat 三文件 + 上证特例 + 联网兜底**（详见第三节 `spblock.dat` 刷新现状与第二节说明段）。

### 4.3 `block.dat` 为何不再刷新（冗余旧整合文件）

`block.dat` 是 1999 老格式，本机实测含 **100 个块**：12 个指数块（上证50、沪深300、创业板指…与 `block_zs.dat` 重叠）+ 86 个概念块（3D打印、5G概念、光伏、锂电池…，与 `block_gn.dat` 重叠）+ 融资融券/专精特新（与 `block_fg.dat` 重叠）。**四文件比对：block.dat 的 100 个块 100% 已被 `block_zs.dat` ∪ `block_gn.dat` ∪ `block_fg.dat` 覆盖（独有块=0）**。因此它纯属历史整合文件，刷新它只是重复下载 281KB 冗余数据，已从「刷新目标」与 `_download_block_gn_from_network` 的 `candidate_files` 中移除。

### 4.4 对 chan.py 的指征

chan.py 当前指数归属走 AKShare `fetch_index_cons`（权威、已归一化）；若将来做**离线兜底**取本地，应 **block_zs.dat（中小盘完整）+ spblock.dat（超大盘/超小盘宽基）并用**、infoharbor 补充；上证指数 / 国证类仍需联网或 AKShare，不能依赖任一单一本地文件。注意 `spblock.dat` 当前按钮刷新不到（见第三节），离线兜底读本地即可。

---

## 附 A：`DataAPI/` 适配器一览

| 文件 | 行数 | 数据源 | 提供能力 | 实际消费方 |
| --- | --- | --- | --- | --- |
| `CommonStockAPI.py` | 72 | — | `CCommonStockApi` 数据源抽象基类（承载频率映射 / 别名等元数据接口） | 全部适配器 + `Chan.py:14` |
| `TdxAPI.py` | **2067** | 通达信本地 vipdoc + 多源板块/指数 | K 线主源、前复权流水线、成分股、板块文件刷新（经 eltdx）、vipdoc 代码收集 | `AppEngine`、`AppRefresh`、`AppScan`、`DataAPI/__init__` 工厂 |
| `TqSdkAPI.py` | 584 | 天勤 tqsdk | 期货/期指 K 线（`CTqSdkAPI`）、历史拉取、名称解析、账户加载、回看根数配置 | `DataAPI/__init__` 工厂、`AppEngine:147`、`AppSSE:30`、`App/utils:35`、`BSPointList:822` |
| `TqSdkCSSESource.py` | 360 | 天勤 tqsdk（SSE 流） | SSE 流数据源抽象：`connect` / `get_kline_serial` / `wait_update` / `close_all` | `AppSSE:49`、`FrontAPI` re-export、`TqSdkAPI:263` |
| `ElTdxAPI.py` | **569** | eltdx（通达信网络行情，7709） | 四条链路：除权除息（`0x000f`）、板块文件下载（`0x06B9`）、PE-TTM（`resources.read_stats` → `zhb.zip` 内 `tdxstat.cfg`）、流通市值（`0x0010` 流通股本 × `0x054c` 最新价） | `TdxAPI:672`（XDXR + 板块下载）、`AppRefresh`（PE 全量）、`AppScan`（市值，`:41` 直连） |
| `AkshareAPI.py` | 213 | AKShare | 指数成分股 `fetch_index_cons`、指数归属映射常量、`CAkshare` K 线适配 | `AppRefresh:28`、`TdxAPI:674`、`Chan.py:189` |
| ~~`MarketStatsAPI.py`~~ | — | —（**已删除**） | 原「按市场分流的壳」（项目文档自述『分流壳，不直连数据源』）。2026-09-12 评审：DataAPI 层各模块应一一对应真实数据源，选源属**业务编排**且 App 层本就有先例（`_fetch_names_from_sina_once` 里 A股→新浪、港股→腾讯），故分流逻辑上移到 `App/AppRefresh.py`，本文件删除 | — |
| `TxAPI.py` | **132** | 腾讯财经 `qt.gtimg.cn` | **仅剩港股 PE-TTM（字段 `[39]`）+ 港股名称（字段 `[1]`）**；A 股 PE 与流通市值已迁出，`fetch_float_mc` 已下线 | `AppRefresh:30`（港股名）、`MarketStatsAPI`（港股 PE） |
| `SinaAPI.py` | **100** | 新浪财经 `hq.sinajs.cn` | A股名称（`_normalize_sina_pair` `:24` 归一入参顺序；整批无有效返回时 WARNING `:97`） | `AppRefresh:31` |
| `ThsCloudZxgAPI.py` | 436 | 同花顺云端 Web API `t.10jqka.com.cn` | 云端自选股增删 / 批量替换（`save_scan_to_ths_cloud`） | `AppScan:422`、`AppEngine:172`、`Script/ths_sync_to_tdx.py` |
| `BaoStockAPI.py` | 127 | BaoStock | `CBaoStock` K 线适配 | `Chan.py:180`（仅独立脚本路径） |
| `ccxt.py` | 97 | ccxt（加密货币/外盘） | `CCXT` K 线适配 | `Chan.py:183`（仅独立脚本路径） |
| `csvAPI.py` | 87 | 本地 CSV 文件 | `CSV_API` K 线适配 | `Chan.py:186`（仅独立脚本路径） |
| `__init__.py` | 34 | — | `get_stock_api()` 数据源工厂（唯一分派点） | `AppOrch` 等 |

---

## 附 B：仅在独立脚本路径可达的数据源

`Common/CEnum.py:DATA_SRC` 枚举里的 `BAO_STOCK` / `CCXT` / `CSV` / `AKSHARE` 四条，
只经 `Chan.py:GetStockAPI()`（`:177`）按 `data_src` 参数选择，**API 服务（`python FrontAPI.py`）不会走到**：

| 数据源 | 类 | 选择条件 | 可达场景 |
| --- | --- | --- | --- |
| BaoStock | `CBaoStock` | `data_src == DATA_SRC.BAO_STOCK` | `Debug/strategy_demo*.py`、独立 `Chan.py` 调用 |
| CCXT | `CCXT` | `data_src == DATA_SRC.CCXT` | 独立 `Chan.py` 调用 |
| 本地 CSV | `CSV_API` | `data_src == DATA_SRC.CSV` | 独立 `Chan.py` 调用 |
| AKShare（K 线面） | `CAkshare` | `data_src == DATA_SRC.AKSHARE` | 独立 `Chan.py` 调用 |

> 注意区分：`AkshareAPI.fetch_index_cons`（成分股面）**是** API 服务的活路径（见第二节）；`CAkshare`（K 线面）不是。
> 另有 `custom:<模块>.<类>` 语法可动态加载 `DataAPI/` 下任意适配器，App 层正是用它装配 `TdxAPI.CTdxAPI` / `TqSdkAPI.CTqSdkAPI`。

---

## 附 C：依赖可用性与降级行为

| 依赖 | 声明位置 | 缺失时的真实行为（实测） |
| --- | --- | --- |
| `eltdx` | `requirements.txt`「引擎可选数据源」 | **四条链路同时失效**（2026-09 单源归一化后的既知代价）：① 前复权不可用（`ElTdxAPI.py:272` `log.error` 显著报错，非静默降级）；② **板块文件刷新整体失效**（`TdxAPI.py:1272` WARNING「eltdx 下载异常，保留旧文件」）；③ A 股 PE-TTM 取数失败——**K 线页面本次不显示 PE**（实时层捕获后静默降级、保留旧值，`AppData.py:1246` 记 `log.info`），**不抛错、不影响 K 线本身**；④ A 股流通市值取数抛错（`ElTdxAPI.fetch_float_mc (:518)` 不吞异常，扫描前置取数由 AppScan 的「回退本地缓存 + 过期告警」兜底）。服务照常启动 |
| `pytdx` | **已不使用**（源码 0 处引用、`requirements.txt` 未声明） | 无影响。2026-09 起 pytdx 已从项目彻底退场：板块刷新改走 eltdx `0x06B9`、`880xxx` 的 Step4 兜底链路整体删除。若历史环境仍装有 pytdx，项目也不会导入它 |
| `tqsdk` | `requirements.txt`「引擎可选数据源」 | 模块顶层无硬 import；期货功能不可用，股票侧不受影响 |
| `akshare` | `requirements.txt`「引擎可选数据源」 | 指数归属与中证系指数成分获取跳过（`AkshareAPI.py:44` 捕获 `ImportError`，`:45` 打日志后 `:46` 返回 `[]`） |
| `mootdx` | **未声明** | 无影响（0 处调用） |

---

## 附 D：已知限制与口径提醒

> 本节数字为**实测样本值**，取自 2026-09-12 的通达信数据副本（`T0002/hq_cache/` 下
> `infoharbor_block.dat` / `tdxzs.cfg` / `spblock.dat` / `block_*.dat`）。板块数量会随通达信服务端更新而变化，
> **判据（上限来源、一致性过滤规则、依赖影响面）不变，具体只数需重新实测**。

1. **`block_*.dat` 成分数上限 400 只，且上限来自服务端。** 直接读二进制验证（2026-09-12）：`block_gn.dat`(269 板块) / `block_zs.dat`(117) / `block_fg.dat`(161) / `block.dat`(100) 中，声明成分数**最大即 400，无一超过**；从真实服务器重新下载后仍为 400。记录步长 2800 字节 ÷ 每股 7 字节 = 400，因此改本地解析器无效。**注意该 400 上限只影响 `block_*.dat` 路径，而 `get_index_stocks` 当前已不使用该路径**（`880xxx` 走 `infoharbor_block.dat`，实测 `880861` 取到 1103 只，不受 400 限制）；`block_*.dat` 现仅由 `block.dat` 冗余判定（第四节）与外部脚本读取。
2. **`infoharbor_block.dat` 的 `declared_count` 一致性过滤会剔除「声明数 ≠ 实际数」的段。** 2026-09-12 实测：文件含 **547 段**（`#GN_` 269 / `#FG_` 161 / `#ZS_` 117），但经 `_parse_infoharbor_block` 的 `declared == 0 or declared == len(stocks)` 过滤后（`TdxAPI.py:1374`），`_read_infoharbor_blocks` 只返回 **420 个带代码板块**。这是 `880xxx` 「文件里有这个名字的段、但取成分股却是空」的常见原因之一——表现与「文件里没有」完全一致，排查时需区分。
3. **缺 `eltdx` 的影响面在 2026-09 单源归一化后显著扩大。** 旧版只有「前复权」依赖它；现在 **除权除息、板块文件刷新、A 股 PE-TTM、A 股流通市值** 四条链路全走 eltdx 7709 通道：缺包或通道故障时，前复权显著报错（`ElTdxAPI.py:272`）、板块刷新整体失效（`TdxAPI.py:1272`）、市值取数抛错上浮，**A 股 PE 则表现为页面不显示 PE**（实时层静默降级，见第 8 条）。**这是「一次做完单源归一化」的既知代价**——一个通道故障会同时打掉四个功能；权衡点是「维护成本 vs 单点耦合」，当前选择前者（同源口径统一、规则只有一份）。
4. **本文件此前版本的口径错误已修正**：① `mootdx` / `pytdx` 回退是「**已于 2026-09 整体删除**」而非「注释保留」（`ElTdxAPI.py:230-231` 只剩说明性注释，无被注释的可执行代码）；② **「Step4 兜底」相关章节本轮整体删除**（`_download_block_gn_from_network` 已不存在）；③ `pytdx` 从未在 `requirements.txt` 中以「运行必需」声明（基线即无该行），旧表述有误。
5. **PE-TTM 迁移的差异边界（2026-09-12 实测，全市场 8037 只对照）：真实 A 股股票零缺失。** 「旧路径有值、新路径无值」的 144 个**全部是指数 / 板块代码**（`sh000001`、`sh000300`、`880xxx` 等，eltdx 统计文件只覆盖可交易证券）——股票侧无损失。港股覆盖 2583 只**完全不变**（仍走腾讯）。**取数耗时**：12.9s（27 批 HTTP）→ **1.2s**（`read_stats` 单请求，实测 1.16～1.36s）。
6. **流通市值迁移的差异边界（同批实测）：A 股股票 5553 只中 5462 只（98.4%）落在腾讯「亿元两位小数」的取整半格内。** 其余 91 只差异根因是 eltdx `0x0010` 流通股本**滞后于解禁 / 增发**（季报口径 vs 实时流通口径），非换算错误。**基金 / REIT 类判据不同**：腾讯字段 `[44]` 对该类返回「份额 × 价」（与其 `[45]` 同值，实测大量 80% 相对差集中在此），与 eltdx 的「流通股本 × 价」口径本就不同源——该类不属 A 股股票，不计入差异统计。另注：初期的 2110 处「缓存 vs 实时」不符经核对是**缓存为增量合并旧值**（落盘时间戳可证），非口径错误。**取数耗时**：≈9～14s（27 批 HTTP）→ **6.7s**（全市场，含北交所坏代码逐只隔离重试）。
7. **盘中一致性未验证（已知限制）**：本节 PE / 市值对照为**周六盘后**取样——eltdx `tdxstat.cfg` 为盘后快照、腾讯为实时接口，两者在**交易日盘中**可能因快照刷新时点不同产生偏差，需交易日复测。另注 eltdx 统计口径下 `pe_ttm` 为 `None` 或 `0` 时跳过、负值保留（亏损股），与腾讯 `[39]` 的过滤规则一致。

8. **PE-TTM 实时化带来的行为变化（2026-09-12 改造；属设计取舍，非缺陷）**：

   ① **每次打开 K 线页面固定 +1.2~1.4s**（A 股：eltdx `read_stats` 是整体文件，**取单只与取全市场同耗时**，实测 1.20 / 1.26 / 1.40s）；港股走腾讯单只 ≈0.47s。
   因本软件不提供实时行情（K 线读本地 vipdoc），已与用户确认「打开这个标的就取一次」即满足新鲜度要求，故**未加时间缓存**。
   ⚠ 不要据此再加 TTL/当日缓存——那会重新引入「页面 PE 与打开时刻不同步」这一被本次改造消除的问题。

   ② **并发打开同一标的时，抢不到 single-flight 锁的请求本次不显示 PE**：`_ensure_pe_ttm_live`（`AppData.py:1246`）用**非阻塞** `acquire`，
   没抢到即立即返回当前表（不排队等待）——代价是那一两次请求页面 PE 为空，收益是「一个卡住的网络请求不会把整批请求一起拖住」。
   拉取完成后表已写入，**下次打开即可拿到**。多标签页同时首开同一只票才会遇到，属罕见场景。

   ③ **`_pe` 内存表在常态下只含「本进程内打开过的标的」**（不再像落盘缓存那样一次加载全市场）。
   这不影响任何现有消费方：K 线页面按标点查（**全项目唯一 PE 展示点**是 `Frontend/app.js:2268` 的 K 线左上角）；
   **扫描列表不消费 PE**（`AppScan.py` 零引用）；`index_belong` 仍按全量从文件加载。

   另：`stock_index_belong.json` 的自动迁移只在**新文件不存在且旧文件存在**时触发一次，旧文件保留不删（可回退）。
   落盘带**空表保护**：内存表为空时不写（避免指数归属取数失败时把好文件抹成 `{}`），且不替换内存表（`AppRefresh.py:263` 落盘保护 / `:231` 空结果不 `replace_index_belong`）。

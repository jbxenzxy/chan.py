# 数据源清单

> 基于 `custom-dev` 分支（commit `fde4a1dab9d0`，2026-09-11）代码核查。
> 口径：**信息 → 收口适配器 → 取数函数 → 底层真实数据源 → 消费方**。
> 所有行号对应该 commit；函数名后括注定义行，便于定位。

---

## 〇、K 线主数据源（chan 分析入口）

全部 K 线经 `DataAPI/__init__.py:get_stock_api()` 单一工厂分派，**App 层不直连具体数据源类**：

| 代码类型 | 适配器类 | 底层真实数据源 | App 层装配点 |
| --- | --- | --- | --- |
| 期货 / 期指（含 `CFFEX`/`SHFE`/`DCE`/`CZCE`/`INE`/`GFEX`/`SGX` 前缀，或 `KQ.m@` / `KQ.i@` / `KQD.m@`） | `TqSdkAPI.CTqSdkAPI` (`:216`) | **天勤 tqsdk**（SSE 实时接入；历史 K 线经 `fetch_futures_kline` `:398`） | `AppSSE.py:1219` `data_src="custom:TqSdkAPI.CTqSdkAPI"` |
| 其他（A股 / 指数 / 板块 / 港股） | `TdxAPI.CTdxAPI` (`:985`) | **通达信本地 vipdoc**：日线 `.day` / 5分钟 `.lc5` / 1分钟 `.lc1`；周线与 15m/30m 由本地合成；**仅前复权需联网**（eltdx xdxr） | `AppEngine.py:691 / 709 / 731 / 747` `data_src="custom:TdxAPI.CTdxAPI"` |

说明：

* 本地文件路径规则：A股 `{vipdoc}/{sh|sz|bj}/lday/{market}{code}.day`；港股 `ds/lday/31#{code}.day`（指数为 `27#HZ{code}.day`）。周线由日线合成，15m/30m 由 5m 合成。
* 前复权默认关闭，经 `set_tdx_config(forward_adjust_enabled=True)` 开启；开启后前复权流水线才向 `ElTdxAPI` 取 XDXR。
* `CTdxAPI` 的 K 线数据由 `tdx_data_context()` 每请求线程局部注入，实例只在 `__init__` 绑定一次快照引用；脱离上下文直接实例化会返回空而非静默读到他人数据。

---

## 一、六类信息的数据源

| 信息 | 收口模块 | 取数函数 | 底层真实数据源 | 消费方 |
| --- | --- | --- | --- | --- |
| 除息除权 (XDXR) | `DataAPI/ElTdxAPI.py` | `get_xdxr_data(market, code)` (`:234`) | **eltdx**（通达信网络行情，7709 协议 / `0x000f` 命令）· **单一数据源** | `TdxAPI` 前复权流水线（`TdxAPI.py:673` 导入） |
| 流通市值 | `DataAPI/TxAPI.py` | `fetch_float_mc(stock_list)` (`:80`) | **腾讯行情** `https://qt.gtimg.cn/q=`，字段 `[44]`（亿元） | `AppScan.py:578` 扫描预过滤 |
| 股票名字（A股） | `DataAPI/SinaAPI.py` | `fetch_a_names(mkt_code_pairs)` (`:21`) | **新浪财经** `http://hq.sinajs.cn/list=`，字段 `[0]`（GBK） | `AppRefresh.py:150`（第一轮） |
| 股票名字（港股） | `DataAPI/TxAPI.py` | `fetch_hk_names(hk_codes)` (`:117`) | **腾讯行情** `qt.gtimg.cn/q=`，字段 `[1]`（GBK） | `AppRefresh.py:161`（第二轮） |
| PE-TTM | `DataAPI/TxAPI.py` | `fetch_pe_ttm(mkt_codes)` (`:50`) | **腾讯行情** `qt.gtimg.cn/q=`，字段 `[39]`（市盈率-动态） | `AppRefresh.py:264` |
| 指数归属 | `DataAPI/AkshareAPI.py` | `fetch_index_cons(index_code)` (`:34`) | **AKShare `index_stock_cons_csindex`**（中证指数公司 csindex） | `AppRefresh.py:184`（线程池，每指数 30s 限时） |

说明：

* A股与港股股票名称分属两个数据源（新浪 vs 腾讯）：新浪港股接口已失效，港股改走腾讯并放宽批次间隔至 0.3s 以避限流。
* **除息除权现为 eltdx 单一数据源，`mootdx` / `pytdx` 三级回退已于 2026-09 整体删除**（不是"注释保留"）。设计意图是失败即显著报错（`ElTdxAPI.py:271` `log.error`）而非静默降级，避免把「网络/接口故障」伪装成「该股无除权除息数据」。如需新增数据源，在 `get_xdxr_data` 的来源元组中追加即可，**不要**恢复静默 `continue` 式降级。
* 指数归属的 `AKSHARE_INDEX_MAP` 只覆盖 4 个指数（`000300` 沪深300 / `000905` 中证500 / `000852` 中证1000 / `000688` 科创50），与第二节的成分股取数共用同一个 `fetch_index_cons`。
* 腾讯 `_iter_records` 按行前缀 `v_sh` / `v_sz` / `v_hk` 判定市场；PE-TTM 与流通市值共用该解析但字段索引不同（`[39]` / `[44]`）。

---

## 二、股票扫描 · 成分股的获取方式

入口：`TdxAPI.get_index_stocks(sector_code)`，**定义于 `DataAPI/TdxAPI.py:1665`**。
上游：`AppScan.py:363`（`Scanner.stock_list` 的 `page_index` 来源，`AppScan.py:533` 注册）→ `FrontAPI.py:358` `GET /api/stocks/scan/read/candidates`。

| 板块代码类型 | 取数方式 | 底层真实数据源 | 代码位置 | spblock.dat 能否取得（本地离线，本机实测） |
| --- | --- | --- | --- | --- |
| `881xxx` 研究行业（新版） | `_read_tdxhy_sector_stocks` | 通达信本地行业配置 `T0002/hq_cache/tdxhy.cfg` | `:2276` / 解析 `:2213` | **否** — spblock.dat 只含交易所／指数公司宽基 + ETF + 债券，不含研究行业分类 |
| 港股指数 `HSTECH` / `HSIDI` | `_read_hk_index_stocks` | 恒生指数公司官网 `hsi.com.hk` Factsheet **PDF** | `:2077` | **否** — A股本地文件，不含港股指数 |
| `000001` 上证指数 | `_read_sh_index_stocks_exchange` | 上交所官网 `query.sse.com.cn/sseQuery/commonQuery.do`（主板A `STOCK_TYPE=1` + 科创板 `8` 两段合并） | `:1851` | **否** — spblock.dat 无 上证指数（其成分股＝全部沪市，走专用接口） |
| 中证指数 `000300/000905/000852/000688/000510` | `_fetch_csi_index_stocks` → `AkshareAPI.fetch_index_cons` | AKShare / csindex（中证指数公司） | `:2119` | **部分** — 中证500(000905)／中证1000(000852)／中证A500(000510) **是**（各 500／1000／500 只）；沪深300(000300)／科创50(000688)／中证800(000906) **否**（后三者走 infoharbor_block.dat 的 ZS 段） |
| `399xxx` 深交所指数 | 深交所官网 XLS 直连 | `www.szse.cn/api/report/ShowReport`（`CATALOGID=1747_zs`，`SHOWTYPE=xls`） | `:2168` | **部分** — 深证成指(399001)／国证2000(399303) **是**（各 500／2000 只）；创业板指(399006)／深证100(399004) 等其余 399xxx **否** |
| 其他指数（`000xxx` 非中证 / `932xxx` 等） | `_fetch_csi_index_stocks`（兜底） | AKShare / csindex（中证指数公司） | `:2203` | **个别** — 仅超大盘／超小盘宽基：中证2000(932000) **是**（2000 只）；上证50(000016)／上证180(000010)／上证380(000380) 等其余 **否** |
| `880xxx` 概念 / 风格板块 | **Step3** 优先 `_read_infoharbor_sector_stocks` | 本地 `T0002/hq_cache/infoharbor_block.dat` | `:1694` / `:1653` | **否** — spblock.dat 不含概念／风格板块 |
| `880xxx`（Step3 未命中时） | **Step4** 兜底 `_debug_read_page_index_stocks` 链路下的 `_download_block_gn_from_network` | 本地 `tdxzs.cfg` 查名 → `block_zs/gn/fg/dat` **本地优先，缺失才用 pytdx 联网下载**（`TDX_BLOCK_SERVERS` 16 台，7709） | `:1732` / `:1259` | **否** |
| `8803xx` / `8804xx` 旧版行业 | 直接返回空并提示换用 `881` | （无成分股数据） | `:1727` | **否** |

路由顺序（`get_index_stocks`）：`881xxx` → 港股指数 → 非 `88` 开头走标准指数 → `880xxx` 走 Step3/Step4。

说明：

* **市场前缀以 csindex 的「交易所」字段为权威**（`_index_cons_to_stocks` `:1769`）；仅在拿不到时按代码首数字兜底（`_prefix_by_digit` `:1750`）。此前按 `first in "689"` 的旧规则会把北交所 `8xxxxx`/`920xxx` 误判为沪市。
* 所有网络抓取均经 `_run_with_timeout` 限时，避免扫描因网络阻塞卡死。
* `880xxx` 的 Step3 覆盖 421 个板块；Step4 仅在 Step3 未命中时触发，且 `block_*.dat` 的成分数存在 **400 只上限**（见第四节）。
* **`spblock.dat` 仅覆盖「特定宽基」，不是「所有上交所／深交所／中证指数」的通用源**（2026-09-12 本机 `D:\new_tdx_hd_test` 实测，共 35 个块）。实测含成分股的宽基仅 6 个：中证500(500)／中证1000(1000)／中证2000(2000)／中证A500(500)／国证2000(2000)／深证成指(500)；另含 ETF（沪深股通ETF 447）、债券（上证／深证做市债券 725／222）、基金（创业和科创基金 187）等非指数块。**不含** 上证指数、上证50／180／380／580、沪深300、中证800、中证100／200、科创50／100、深证100、创业板指／50、国证1000 等。**两文件对宽基指数是「互补存储、不重叠」**（2026-09-12 实测，非按「成分股数 ≤ 800」划分——中证500(500)／深证成指(500)／中证A500(500) 虽 ≤800 却在 `infoharbor_block.dat` 的 ZS 段为空，而中证800(800) 虽 >500 却在 ZS 段有 800 只）：上述 6 个宽基**只**在 spblock.dat；沪深300、中证800、上证50／180／380／580、科创50／100、深证100、创业板指／50 等**只**在 `infoharbor_block.dat` 的 ZS 段（实测各有 50～800 只成员）。**例外**：上证指数、国证1000 在本快照两份文件里均为空（未固化成员列表，通达信可能另算）。因此「扩展 → 成分股」的本地二分是：spblock.dat 存的 6 个宽基走 spblock.dat，其余标准／中小盘宽基走 infoharbor_block.dat 的 ZS 段；`88` 开头（881 研究行业 + 880 概念风格）则分别来自 `tdxhy.cfg` 与 `infoharbor_block.dat`，**均不在 spblock.dat 内**。该列记录的是「可作为离线兜底的真实本地源」，chan.py 当前并未读取 spblock.dat（指数归属仍走 AKShare，见第一节）。

---

## 三、板块文件刷新（`TdxAPI.refresh_block_files`）

| 项 | 内容 |
| --- | --- |
| 入口 | `TdxAPI.refresh_block_files(progress_callback)`，定义 `DataAPI/TdxAPI.py:1404` |
| 上游 | 前端「刷新」按钮 → `POST /api/stocks/refresh`（`FrontAPI.py:639`）→ `AppOrch` 再导出 → `AppRefresh.refresh_stock_names_async` (`:577`) → `_refresh_stock_names` (`:344`) → `AppRefresh.py:545` |
| 底层数据源 | **pytdx `TdxHq_API`** → `TDX_BLOCK_SERVERS`（16 台通达信行情服务器，7709） |
| 刷新目标 | `{TDX_INSTALL_DIR}/T0002/hq_cache/` 下 `tdxhy.cfg`、`infoharbor_block.dat`、`spblock.dat`、`block_zs.dat`、`block_gn.dat`、`block_fg.dat` 共 6 个文件（`block.dat` 已移除：其 100 个块全部已被 `block_zs/gn/fg` 覆盖，属冗余旧整合文件，见第四节） |
| 安全策略 | 不先删旧文件；先下到内存 → 校验 → 写 `.tmp` → `os.replace` 原子替换；任一文件失败即保留旧文件 |
| 日志格式 | `[板块刷新] ✅ {文件名}（{作用说明}）刷新成功: {字节数} 字节`；`tdxhy.cfg`/`infoharbor_block.dat`/`spblock.dat`/`block_zs.dat` 带括号说明，`block_gn.dat`/`block_fg.dat` 不带 |

> 这是 **pytdx 在项目中最主要的真实用途**（每次点刷新都执行）。第二节 Step4 的 pytdx 用法是次要的兜底路径。

> **`spblock.dat` 刷新现状（2026-09-12 实测）**：`refresh_block_files` 走 pytdx `get_block_info_meta` 通道（板块目录查询），该目录**不含 `spblock.dat`**——实测 6 台 `TDX_BLOCK_SERVERS` 均返回 `size=0`。因此点刷新按钮时 `spblock.dat` 会落到「服务端 size 无效，保留旧文件」分支，**当前实际不刷新**。`spblock.dat` 由通达信客户端的「盘后 / 板块下载」单独写入，chan.py 只读取、不负责刷新它。若要让按钮也能刷 `spblock.dat`，需改用通达信另一条板块下载协议（非 `get_block_info`），属后续增强项。

---

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
| `TdxAPI.py` | 2355 | 通达信本地 vipdoc + 多源板块/指数 | K 线主源、前复权流水线、成分股、板块文件下载、vipdoc 代码收集 | `AppEngine`、`AppRefresh`、`AppScan`、`DataAPI/__init__` 工厂 |
| `TqSdkAPI.py` | 584 | 天勤 tqsdk | 期货/期指 K 线（`CTqSdkAPI`）、历史拉取、名称解析、账户加载、回看根数配置 | `DataAPI/__init__` 工厂、`AppEngine:147`、`AppSSE:30`、`App/utils:35`、`BSPointList:822` |
| `TqSdkCSSESource.py` | 360 | 天勤 tqsdk（SSE 流） | SSE 流数据源抽象：`connect` / `get_kline_serial` / `wait_update` / `close_all` | `AppSSE:49`、`FrontAPI` re-export、`TqSdkAPI:263` |
| `ElTdxAPI.py` | 290 | eltdx（通达信网络行情，7709 / `0x000f`） | 除权除息（XDXR）；模块定位面向未来扩展 | `TdxAPI:673` |
| `AkshareAPI.py` | 213 | AKShare | 指数成分股 `fetch_index_cons`、指数归属映射常量、`CAkshare` K 线适配 | `AppRefresh:26`、`TdxAPI:674`、`Chan.py:189` |
| `TxAPI.py` | 158 | 腾讯财经 `qt.gtimg.cn` | PE-TTM、流通市值、港股名称 | `AppRefresh:27`、`AppScan:41` |
| `SinaAPI.py` | 70 | 新浪财经 `hq.sinajs.cn` | A股名称 | `AppRefresh:28` |
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
| `eltdx` | `requirements.txt`「引擎可选数据源」 | 前复权不可用（`get_xdxr_data` 显著报错）；K 线本身仍可读 |
| `pytdx` | `requirements.txt`「运行必需」 | 服务照常启动（函数内懒导入，实测 `import DataAPI.TdxAPI` 成功）。**板块文件刷新整体失效**（仅 WARNING，`TdxAPI.py:1440`）；`880xxx` 的 Step4 兜底返回空 |
| `tqsdk` | `requirements.txt`「引擎可选数据源」 | 模块顶层无硬 import；期货功能不可用，股票侧不受影响 |
| `akshare` | `requirements.txt`「引擎可选数据源」 | 指数归属与中证系指数成分获取跳过（`AkshareAPI.py:47` 打日志后返回 `[]`） |
| `mootdx` | **未声明** | 无影响（0 处调用） |

---

## 附 D：已知限制与口径提醒

> 本节数字为**实测样本值**，取自 2026-09-11 的通达信数据副本（`T0002/hq_cache/` 下
> `infoharbor_block.dat` / `tdxzs.cfg` / `block_*.dat`）。板块数量会随通达信服务端更新而变化，
> **判据（上限来源、覆盖口径、import 位置）不变，具体只数需重新实测**。

1. **`block_*.dat` 成分数上限 400 只，且上限来自服务端。** 直接读二进制验证：`block_gn.dat`(269 板块) / `block_zs.dat`(117) / `block_fg.dat`(161) / `block.dat`(100) 中，声明成分数**最大即 400，无一超过**；从真实服务器重新下载后仍为 400。记录步长 2800 字节 ÷ 每股 7 字节 = 400，因此改本地解析器无效。同一板块实测差异：`880861 连续亏损` 走 `infoharbor_block.dat` 得 **1102 只**，走 Step4 兜底只得 **400 只**（缺口 63.7%），且只打一条 WARNING 就照常返回。
2. **Step4 兜底的覆盖面要分两个口径看。** 实测：`infoharbor_block.dat` 覆盖 421 个板块；`tdxzs.cfg` 有 604 个 `880xxx`（其中 132 个是 `8803xx/8804xx`，直接返回空）→ 有效 472 个；Step4 可命中 370 个，但**只有 2 个是 Step4 独有**（`880524 含可转债`、`880735 专精特新`，占 0.4%）。反之，若 `infoharbor_block.dat` 缺失或解析失败，472 个中有 370 个（78%）会落到这条被截断的兜底上。
3. **`pytdx` 的 import 位置早于「本地文件」分支。** `TdxAPI.py:1288` 的 `from pytdx.hq import TdxHq_API` 位于「读本地 `block_*.dat`」之前；当本地文件齐全、`need_download` 为空时 pytdx 一个接口都不会被调用，但缺 pytdx 仍会在 `:1290` 直接 `return {}`，把本可工作的本地解析一并废掉（实测 400 只 → 0 只）。
4. **本文件此前版本的口径错误已修正**：`mootdx` / `pytdx` 回退是「**已于 2026-09 整体删除**」而非「注释保留」（`ElTdxAPI.py:221-231` 只剩说明性注释，无被注释的可执行代码）。

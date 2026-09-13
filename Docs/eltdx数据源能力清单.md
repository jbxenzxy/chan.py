# eltdx 数据源能力清单

> 核查时间：2026-09-13，依据当前项目代码（`DataAPI/ElTdxAPI.py`、`App/AppRefresh.py` 等）梳理。
>
> `eltdx` 是通达信网络数据源的 Python 封装，对外主要暴露**两条数据通道**，互不依赖：

| 通道 | 端口 / 协议 | 传输方式 | 承载数据 |
| --- | --- | --- | --- |
| **7709 行情主站** | TCP 长连接 | 二进制协议 | 行情、K 线、分时、成交明细、代码表、统计文件、通用文件读取 |
| **7615 TQLEX 网关** | HTTP POST | JSON 报文 | F10 资料（公司概况、财务、估值、研报、股东增减持等） |

---

## 一、取数能力一览

| 信息 | 底层接口 / 命令 | 协议 | 传输方式 | 取数模式 | 接入情况 / 消费方 |
| --- | --- | --- | --- | --- | --- |
| 除权除息（XDXR） | `TdxClient.corporate.capital_changes` → `0x000f` 事件表 | 7709 | TCP 长连接 | **单只**（按代码查询） | `ElTdxAPI.get_xdxr_data` → TdxAPI 前复权流水线 |
| **PE-TTM（单只）** | `F10Client.valuation(code, req_id="200191")`（估值表，Entry=`HQServ.hq_nlp_gpsj`） | **7615** | **HTTP POST** | **单只**（按代码查询，实测 ~0.22~0.24s/只） | **当前使用**：AppRefresh `_fetch_pe_ttm_live` A 股个股分支；打开 K 线页面取一次，不落盘 |
| **PE-TTM（批量）** | `TdxClient.resources.read_stats()` → `0x06B9` → `zhb.zip / tdxstat.cfg` | 7709 | TCP 长连接 | **全量**（单次请求覆盖全市场） | **已删除**（2026-09 用户定版：批量取数代码与 `stock_pettm.json` 一并移除） |
| 流通市值 | `corporate.finance_batch`（`0x0010` 流通股本）× `quotes.get_snapshots`（`0x054c` 最新价） | 7709 | TCP 长连接 | **批量**（80 只/批） | `ElTdxAPI.fetch_float_mc` → AppScan 扫描前置取数 |
| 股东增减持计划 | `F10Client.shareholder_change_plans`（Entry=`CWServ.tdxf10_gg_gdyj` + section `gdzjcjh`） | **7615** | **HTTP POST** | **单只**（按代码查询） | `ElTdxAPI.get_shareholder_reduction_*` → AppEngine K 线主分析路径（`include_extra=True`） |
| 板块文件刷新 | `TdxClient.resources.download_file` → `0x06B9` 通用文件读取 | 7709 | TCP 长连接 | **文件级**（整体下载） | `ElTdxAPI.download_block_files_via_eltdx` → TdxAPI `refresh_block_files` |

### PE-TTM：两种接口（重点）

PE-TTM（滚动市盈率）在 eltdx 上有**两种取数接口**，**目前项目使用的是单只接口**：

1. **单只接口（7615 HTTP，当前使用）**
   - `F10Client.valuation(code, req_id="200191")` —— 7615 TQLEX 网关，**HTTP POST**，按代码查询单只股票的估值表（`PETTM` 字段）。
   - 与股东增减持同类：打开一只股票时取一次，**不落盘**（`stock_pettm.json` 不再需要）。
   - 实测 ~0.22~0.24s/只，无启动预热。

2. **批量接口（7709 TCP，已删除）**
   - `TdxClient.resources.read_stats()` → `0x06B9` 服务器文件读取 → `zhb.zip` 内 `tdxstat.cfg`（盘后统计快照）。
   - **单次请求即覆盖全市场**（取 1 只与取全市场耗时相同，实测 1.2~1.4s）。
   - 原用于「进程级全量缓存 + 落盘 + 启动预热」；2026-09 用户定版**整体删除**，不再保留。

---

## 二、当前项目接入概览

- **A 股个股 PE-TTM**：`App/AppRefresh.py::_fetch_pe_ttm_live` → `ElTdxAPI.fetch_pe_ttm_single`（7615 单只）。
- **指数 / 港股 PE-TTM**：同一函数 → `TxAPI.fetch_pe_ttm`（腾讯 `qt.gtimg.cn` 字段 `[39]`；eltdx 统计口径不含指数，指数走 eltdx 必然取空）。
- **股票扫描**：`AppScan` 传 `include_extra=False`，**不涉及** PE-TTM / 指数归属 / 股东减持——只消费 K 线 / 缠论结果与名称（`AppEngine.py` 三个 meta 字段在 `include_extra=False` 时置空）。

---

## 三、TQLEX 是什么

- **TQLEX 就是通达信的网关**，是通达信「F10 / 资料数据」HTTP 网关的服务名 / URL 路径。
- 服务地址：`http://static.tdx.com.cn:7615/TQLEX`（`eltdx/f10/client.py` 的 `DEFAULT_TQLEX_BASE_URL`）——运行在通达信官方域名 `static.tdx.com.cn`、端口 `7615`、路径 `/TQLEX`。
- 它与 7709 行情主站是两条线：**7709 = TCP 长连接行情**；**7615 TQLEX = HTTP POST 资料查询**（公司概况、财务报表、估值、研报、股东增减持等资料类数据）。
- `F10Client` 是 eltdx 对 7615 TQLEX 网关的**封装客户端**（"F10" 即通达信个股资料页的代号）。

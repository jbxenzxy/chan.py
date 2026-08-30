# 数据源清单

> 基于 `custom-dev` 分支（commit `386dd3c API update`）代码核查。口径：**信息 → 收口适配器 → 取数函数 → 底层真实数据源 → 消费方**。

## 一、五类信息的数据源

| 信息          | 收口模块                    | 取数函数                            | 底层真实数据源                                                             | 消费方                                    |
| ----------- | ----------------------- | ------------------------------- | ------------------------------------------------------------------- | -------------------------------------- |
| 除息除权 (XDXR) | `DataAPI/ElTdxAPI.py`   | `get_xdxr_data(market, code)`   | **eltdx**（通达信网络行情，7709 协议 / `0x000f` 命令）；`mootdx` / `pytdx` 回退已注释保留 | `TdxAPI.fetch_main_level` 前复权流水线       |
| 流通市值        | `DataAPI/TxAPI.py`      | `fetch_float_mc(stock_list)`    | **腾讯行情接口** `qt.gtimg.cn/q=`，字段 `[44]`（亿元）                           | `AppScan` 扫描预过滤                        |
| 股票名字（A股）    | `DataAPI/SinaAPI.py`    | `fetch_a_names(mkt_code_pairs)` | **新浪财经** `hq.sinajs.cn/list=`，字段 `[0]`（GBK）                         | `AppRefresh._refresh_stock_names`      |
| 股票名字（港股）    | `DataAPI/TxAPI.py`      | `fetch_hk_names(hk_codes)`      | **腾讯行情接口** `qt.gtimg.cn/q=`，字段 `[1]`（GBK）                           | `AppRefresh._refresh_stock_names`（第二轮） |
| PE-TTM      | `DataAPI/TxAPI.py`      | `fetch_pe_ttm(mkt_codes)`       | **腾讯行情接口** `qt.gtimg.cn/q=`，字段 `[39]`（市盈率-动态）                       | `AppRefresh._refresh_pe_ttm`           |
| 指数归属        | `DataAPI/AkshareAPI.py` | `fetch_index_cons(index_code)`  | **AKShare** **`index_stock_cons_csindex`**（中证指数公司 csindex）          | `AppRefresh`（线程池限时）                    |

说明：

* A股与港股股票名称分属不同数据源（新浪 vs 腾讯），在新浪港股接口失效后港股改走腾讯。

* 除息除权现仅启用 eltdx 单一数据源，`mootdx` / `pytdx` 回退分支整体注释保留，失败即显著报错而非静默降级（便于单测 eltdx 稳定性）。

## 二、股票扫描·成分股的获取方式（`TdxAPI.get_index_stocks`，`DataAPI/TdxAPI.py:1695`）

| 板块代码类型                             | 取数方式                                                       | 底层数据源                                                |
| ---------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------- |
| `881xxx` 研究行业（新版）                  | `_read_tdxhy_sector_stocks`                                | 通达信本地行业配置 `tdxhy.cfg`                                |
| 港股指数（HSTECH / HSIDI 等，恒指）          | `_read_hk_index_stocks`                                    | 恒生指数公司官网 `hsi.com.hk` Factsheet PDF                  |
| `000001` 上证指数                      | `_read_sh_index_stocks_exchange`                           | 上交所官网 `query.sse.com.cn`（沪市全部 A 股）                   |
| 中证指数 `000300/000905/000852/000688` | `_fetch_csi_index_stocks` → `AkshareAPI.fetch_index_cons`  | AKShare / csindex（中证指数公司）                            |
| `399xxx` 深交所指数                     | 深交所官网 XLS 直连                                               | `szse.cn/api/report/ShowReport`（CATALOGID=1747\_zs）  |
| 其他指数（`000xxx` 非中证 / `932xxx`）      | `_fetch_csi_index_stocks`（兜底）                              | AKShare / csindex（中证指数公司）                            |
| `880xxx` 概念 / 风格板块                 | 优先 `infoharbor_block.dat`；失败回退 `tdxzs.cfg` + `block_*.dat` | 本地缓存 / 网络下载（`pytdx TdxHq_API` → `TDX_BLOCK_SERVERS`） |
| `8803xx` / `8804xx` 旧版行业           | 直接返回空并提示换用 `881`                                           | （无成分股数据）                                             |

## 附：数据源适配器一览

`DataAPI/` 目录下的独立数据源适配器（P1-1 数据源抽象单轨化收口点）：

| 适配器             | 数据源                    | 提供能力                                     |
| --------------- | ---------------------- | ---------------------------------------- |
| `ElTdxAPI.py`   | eltdx（通达信网络行情）         | 除权除息（XDXR）                               |
| `AkshareAPI.py` | AKShare                | 指数成分股（csindex）、指数归属；并提供 `CAkshare` K 线适配 |
| `TxAPI.py`      | 腾讯财经行情（`qt.gtimg.cn`）  | PE-TTM、流通市值、港股名称                         |
| `SinaAPI.py`    | 新浪财经行情（`hq.sinajs.cn`） | A股名称                                     |
| `TdxAPI.py`     | 通达信本地 + 多源板块/指数        | 板块成分股、前复权流水线、block 下载等                   |


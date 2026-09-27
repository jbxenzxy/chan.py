# memory-tdx.md —— 通达信与数据源：eltdx / pytdx / 881 行业 / 板块成分股


> 本文件由 `~/.workbuddy/MEMORY.md` 于 2026-09-15 拆分而来，**不常驻上下文**。
> 命中 MEMORY.md 索引表的主题词时，用 Read / Grep 按需读取本文件。
> 内容原样搬运未删减；拆分前全文备份：`MEMORY.full-2026-09-15.md`。

## 数据源可用性（2026-09-03 实测）
- **Tushare 连接器（mcp__tushare）当前 token 基本无接口权限**：`trade_cal`、`daily`、`index_daily`、`sw_daily`、`dc_index`、`ths_daily`、`moneyflow_ind_ths` 等一并返回 40203「没有接口访问权限」。做 A 股取数时**不要先试 Tushare 浪费轮次**，直接用 `agentic_search`（金融专用，会自行多步检索）或 `WebSearch` 取数并双源交叉核对。若后续用户升级了 Tushare 积分，再重新试一次并更新本条。


## 通达信研究行业（881）层级与代码来源（2026-09-11 实测）
- **eltdx 拿得到 881 代码+名称，拿不到层级**：`client.codes.all("sh")` 过滤 881 → 实测 **467 个**（`category=="index"` 规则含 `sh000/880/881/999`）。eltdx 全部 21 个命令中**无任何行业树/层级/成分股接口**；881 代码表无层级字段，编码顺序也不表达层级（一/二/三级混排）。
- **权威源（首选）= `<通达信>/T0002/hq_cache/tdxzs3.cfg`**（GBK，`名称|881代码|12|1|0|X代码`）—— **881↔X 直连映射，不需要名称桥接**。实测 **467 条 = 一级30/二级128/三级309**，**层级 = `len(X)-1`**。本地文件、零联网、**服务器 meta=0（下不到）**。同批盘后下载的邻居：`tdxhy.cfg`(股票→X)、`tdxzsbase.cfg`(可下,235723)、`shs.tnf`(meta=0)。
- **次选 = `<通达信>/T0002/cloud_cfg/hy_tree.xml`**（32540B，GBK，mtime 2024-04-09，安装包自带静态）：`<node caption="名称" blockid="X编码">` 嵌套，**有名称+层级但无 881 代码**，必须按名称桥接（归一化 `其它`→`其他`）。pytdx/eltdx 都下不到此文件。
- **`shs.tnf` 格式**（沪市代码表，无文档，靠实测破解）：**50B 头 + N×360B 定长记录**（N=(size-50)/360，实测 27920，与服务器 `get_security_count(1)` 一致）；`r[0:6]` 为 ASCII 代码，**名称起始于记录内偏移 31**，GBK，到连续两个 `0x00` 结束。
- `incon.dat` 是证监会行业分类（`#ZJHHY`），与通达信研究行业无关；`hy_tree1_total.xml` 是主题/概念树。
- **chan.py 硬编码 `_TDXHY_X_TO_881`（AppData.py，470 条）三类实测错误**：
  ① 漏 4 个已上线二级指数 `881008`油服工程 / `881011`石油化工 / `881026`化学原料 / `881218`汽车零部件；
  ② 留 7 个已下架失效代码 `881080`镍 / `881156` / `881164` / `881255` / `881259` / `881379` / `881403`（服务器 881 表已无）；
  ③ `881219` 错标 —— 硬编码=「汽车零部件」，实际=`X260301`车身附件及饰件（三级）；真正的汽车零部件=`881218`（`X2603` 二级）。
- 修正后「板块指数2/3」= **二级 128 / 三级 309**（权威源 tdxzs3.cfg 口径；hy_tree 桥接算出的 127/312 是含未匹配节点的旧口径，别用）。
- **下游症状分两种（实测，判"某代码已失效"必须同时查两处）**：`tdxzs3.cfg` 无、但 `tdxhy.cfg` 仍挂 1~2 只旧码行的 → **静默返回残票、不报错**（7 个里占 4 个：881080/881156/881259/881379）；两处都没有的 → 真返回空（3 个：881164/881255/881403）。只看"返回是否为空"会漏掉那 4 个静默错误。
- 旁证：`incon.dat` 是证监会行业分类（`#ZJHHY`），与通达信研究行业无关，别用它；`hy_tree1_total.xml` 是主题/概念树，不是行业树。


## ⚠️ 881↔X 映射表已漂移 → 2026-09-11 **已完成「单一权威源」改造**（扫 881 行业时必读）
- **最终状态：内嵌表已整体删除**（交付 `chan_行业映射修复_单一源版.zip`，取代此前的 `_完整版`/`_最小版`）。
  原 470 条快照（含反向表，共 958 行）+ 遗留模块级 `load_tdxhy_mapping()` 全部移除，
  `App/AppData.py` 由 2611 → **1753 行（−858）**，改为一律读本机权威源文件。
  **验证：三树对照，映射 digest 与「完整版」逐条完全相同（`516be0e6…`），功能零变化。**
- **⚠️ 上一轮（完整版/最小版）交付的 bug（本轮已修）**：仓库自带测试**冻结了两处硬数据**，
  任何映射变化都会报红 ——
  - `Test/test_blk_parsing.py:262-266` 断言 `len(tdxhy2)==125` / `len(tdxhy3)==315`（**写死条数**）；
  - `Test/test_industry_mapping.py ⑤` 冻结「内容 sha256 + 470 条」快照
    （`Test/snapshots/industry_mapping_integrity.json`）。
  → 修正后 467 条（二级 128 / 三级 309）**让这 2 个用例失败**。本轮已改为
  「期望值由权威源推出 + 快照改为单源契约」，9 项测试全绿。
  **教训：动数据前先 grep `Test/` 里的冻结值与 `\b125\b|\b315\b|\b470\b`。**
- **权威源 = `<TDX_INSTALL_DIR>/T0002/hq_cache/tdxzs3.cfg`**（GBK，`名称|881代码|12|1|0|X代码`）。
  467 条 = 一级 30 / 二级 128 / 三级 309；层级判定 = `len(X)-1`（X+2/X+4/X+6）。
  该文件**本身服务器 meta=0 下不到**，但见下条 —— 它藏在可下载的 `zhb.zip` 里。
- ⚠️ **上一轮记的「服务器 881 只有 323 个」是错的**：那是 pytdx `get_security_list` **分块失败**
  （st=0 / st=21000 两段静默返回空，重试 5 次仍空）导致的**假阴性**。用 eltdx
  `client.codes.all("sh")` 一次拉全 = **467 个**，与 `tdxzs3.cfg` **双向差 0**。
  → 以后枚举代码表用 eltdx；用 pytdx 分块时**必须统计失败分块**，否则"数量对不上"会被误判成数据差异。
- 名称对齐：`get_security_list` / eltdx 的 881 名称**被截断到 4 个汉字**（`881147` = "预加工食"），
  与本地文件里的全名（"预加工食品"）比对时**只能按前 4 字匹配**。
- 原漂移症状（已随删表消失）：补 4（`881008/881011/881026/881218`）、删 7
  （`881080/881156/881164/881255/881259/881379/881403`）、改 1（`881219` 由"汽车零部件"→"车身附件及饰件"）。
  ⚠️ 「已下架码」**不一定返回空**：7 个里 4 个静默返回 1~2 只存量残票（`tdxhy.cfg` 里留着旧 X 挂靠行），
  只有 3 个真返回空 —— 判失效必须**同时查行业树 + tdxhy.cfg 行数**。


## ⭐ 刷新按钮不覆盖 tdxhy.cfg —— 881 成分股的静默陈旧源（2026-09-11 补充实测）
> 上面「pytdx 就是 0x06b9」那条已证明 **tdxhy.cfg 可从服务器取**；本条补三件**新的实证**。
- **刷新范围是硬编码 5 项**：`AppRefresh.py:539-547` → `TdxAPI.refresh_block_files()`，
  文件清单写死在 `TdxAPI.py:1429-1435`（infoharbor_block.dat / block_zs / block_gn / block_fg / block.dat）。
  **`tdxhy.cfg` 不在其中**，且 `refresh_block_files` 末尾只重置 block 与 infoharbor 的缓存
  （`_BLOCK_GN_CACHE*` / `_INFOHARBOR_BLOCK_CACHE*`），**没重置 `_TDXHY_CACHE*`**。
- **mtime 铁证**（`D:\new_tdx_hd_test\T0002\hq_cache`）：5 个 block 文件 mtime 全是 **18:41**（刷新时刻），
  `tdxhy.cfg` mtime 是 **16:12**（通达信客户端自己的盘后下载，同批还有 `shs.tnf`/`gbbq`/`tdxzsbase.cfg`）。
  → **它由客户端维护，不受刷新按钮控制**；`tdxzs.cfg`（18272，09-10 17:53）同理且服务器上根本没有。
- **今天恰好没出问题，纯属巧合对齐**：本机 `tdxhy.cfg` 与服务器当前版本**字节完全相同**
  （150304 字节，sha1 均 `5c36595f9023`）。所以"刷新不含它"目前无可见后果，
  但只要久不开通达信客户端，**881 研究行业成分股会静默变旧且刷新按钮修不好**（881 分支零联网兜底，`TdxAPI.py:1679-1680`）。
- **正面修复（约 10 行，2026-09-12 已落地出补丁 `tdxhy_cfg_refresh.patch`）**：`block_files` 加 `"tdxhy.cfg"`；
  `_validate_downloaded_block_file`（`TdxAPI.py:1206`）补一条 GBK 文本校验（`"|" in txt and "X" in txt`，
  X 行业码，比"≥6 字段行"更稳，且校验函数对白名单外文件原会 `return False` 导致下不下来）；顺手重置 `_TDXHY_CACHE_LOADED`。
- ⚠️ **`_read_infoharbor_blocks()`（`TdxAPI.py:1628-1650`）是纯本地读、不联网** ——
  本地没有就直接返回 `{}` 落到 Step4。所以 880xxx 是「infoharbor 有就用、没有就换路」，
  **不是**「没有就下载」；真正"本地优先、缺失才联网"的只有 `block_*.dat` 那条兜底路（`:1309-1335`）。
- **2026-09-12 受控实验（决定性，推翻"盘后若下这俩则刷新可删"的假设）**：用户手动删
  `tdxhy.cfg` + `infoharbor_block.dat` → 跑盘后下载 → **两者均未恢复**（06:07 落盘的全是
  `.tcu/.th2`/`Pri*.dat`/配置表，无板块文件；连跨新交易日的 09-12 05:32 增量也没碰它们）。
  → **盘后下载不交付这两个文件**：`tdxhy.cfg` 只在首次 / 「补全数据」这类下载出现（09-11 16:12 那次），
  `infoharbor_block.dat` 从不在盘后集合（16:12 批次只下了同族的 `infoharbor_ex.code/.name` 索引、没下 `.dat`）。
  用户原假设前提被证伪：**「刷新板块」不可删**；且 `tdxhy.cfg` 删后只能靠上面那条修复自助恢复。


## ⭐ `zhb.zip` = 通达信板块 cfg 的**下载载体**（2026-09-11 实测，解开"文件下不到"的死结）
- 服务器 `T0002/hq_cache/zhb.zip` **可下载**：`get_block_info_meta("zhb.zip")` → `size=1406899`
  （本机该文件与之**逐字节相同**；实测 47 次 `get_block_info` 分块取回 0 失败）。
- **内含 46 个条目**，包括 `tdxzs3.cfg` / `tdxzs.cfg` / `tdxstat.cfg` / `incon.dat` / `ilong.dat` /
  `nbcomte.dat` / `profile.dat` / `pttab.dat` … —— 即所有「meta=0 取不到」的板块配置文件，
  **都能从 `zhb.zip` 里解出来**。客户端解压时会把 `incon.dat`/`ilong.dat`/`nbcomte.dat` 放到**安装根目录**，
  其余落到 `T0002/hq_cache/`（解压**保留 zip 内时间戳**）。
- **zip 内时间戳 = 服务端的「最后内容变更时间」**：条目时间跨度 `2024-03-29` ~ `2026-09-10`，
  且 `tdxzs3.cfg` 条目时间 == 磁盘 mtime == `2026-09-10 17:53:24`。
  → 因此**不能用磁盘 mtime 判断"今天有没有下载"**：今天 zhb.zip 本身 mtime = 16:12（确实重下了），
  但内部条目时间仍是 09-10，说明**内容没变所以不落盘**。同理 09-11 16:12 批次里有 `tdxhy.cfg`
  （属首次/补全类下载，非常规每日增量；见上条 09-12 删除实验）却没改写 `tdxzs3.cfg`，不是漏下。
- 实测同步性：`tdxzs3.cfg` 的 881 集合与服务器实时 881 代码表 **467 = 467，双向差 0，名称零不符**。
- **可做但本轮未做**：给「板块刷新」按钮加一项 —— 下载 `zhb.zip` → 解压出所需文件，
  即可让"本机缺 `tdxzs3.cfg`"也可一键自愈（属新增功能，需单独评估）。



## ⭐ pytdx 的"板块文件下载"就是 0x06b9 —— eltdx 可直接平替（2026-09-11 实测，以后别再绕）
- **关键等价关系**：pytdx `TdxHq_API.get_block_info()` 的报文体以 `... b9 06` 结尾（见
  `pytdx/parser/get_block_info.py:33` 的 `0c 37 18 6a 00 01 6e 00 6e 00 b9 06`），即 **`0x06B9` 服务器文件读取**；
  `get_block_info_meta()` 用的是 **`0x02c5`**（只取 size/hash，eltdx 未实现，但不影响拿文件）。
  eltdx 把 0x06b9 封装为 `client.resources.read(path, offset, size)` / `download_file(path, chunk_size=30000)`。
  → **判据简化为「目标文件在不在服务器文件空间里」，与用哪个库无关。**
- **实测字节级一致**（2026-09-11，eltdx 3.1.7 直连）：`block_gn.dat` 757,083 / `block_zs.dat` 329,507 /
  `block_fg.dat` 453,279 / `block.dat` 281,686 / `tdxhy.cfg` 150,304 / `infoharbor_block.dat` 739,135 /
  `zhb.zip` 1,406,899 —— eltdx 与 pytdx 返回首部字节完全相同，chan.py 现成的
  `_parse_raw_block_gn` / `_parse_infoharbor_block` **可原样复用**。
- **服务器上不存在**：`block_hy.dat`、`tdxzs.cfg`（两者均返回 chunk_len=0 / meta.size=0）。
  → 印证 `TdxAPI.py:1297` 注释「block_hy.dat 只存在于本地」，也说明 **Step4 的按名查表离不开本地 tdxzs.cfg**。
- **eltdx 能覆盖哪几类板块代码**（chan.py `get_index_stocks` 8 类）：
  - ✅ `880xxx` 概念/风格（Step3 `infoharbor_block.dat` + Step4 四个 `block_*.dat`）—— 可整体收口到 eltdx
  - 🔶 `881xxx` 研究行业 —— `tdxhy.cfg` 可下载（349 个 X 代码 / 128 父代码 / 5,577 条映射），
    但 `881→X` 映射表在 `App/AppData.py`，由 `set_tdx_hy_mapping()` 注入，**不是 eltdx 能力**
  - 🔶 `000001` 上证指数 —— 无指数成分接口，但 `client.codes.all("sh")` 实测 **A股 2,320 只**
    （主板 1,702 + 科创 618），与上交所官网口径等效，需自建口径
  - 🔶 中证 4 指数 —— 只有 `000300`(300) / `000688`(50) 能从 `block_zs.dat` 覆盖；
    **`中证500`/`中证1000` 在四个 `block_*.dat` 中全部未命中**
  - ❌ 港股指数（eltdx 只覆盖沪深北 market 0/1/2，无港股入口）、`399xxx`、其他 `000xxx`/`932xxx`、
    `8803xx`/`8804xx`（两边本就无数据）
- ⚠️ **别混淆**：`client.helpers.topic_stocks()` / `stock_topics()` 走的是**通达信 F10 题材**体系
  （`topic_id` 如 `"2945"`，且必须给一个种子股票），与 `880xxx` 概念板块**不是同一套分类编码**，不可对齐替代。
- ⚠️ 指数代码出现在 `0x044d` 代码表里（`sh000300` 等均可查到）≠ 能取该指数成分股。
- ⚠️ **环境坑**：`chan_verify` venv 的 eltdx 3.1.1 装坏了（`import eltdx` → `No module named 'eltdx.client'`）；
  能用的是项目 `.venv` 的 **3.1.7**。跑 eltdx 探针请用 `/c/my_chan_project/.venv/Scripts/python.exe`。
- 中文路径从 raw 通道下载必须 `urllib.parse.quote(path)`（eltdx 文档一层 17 个中文文件名会全挂）。


## chan.py 里 pytdx 的依赖面（2026-09-11 核查，别再重复摸底）
- **唯一真实调用点：`DataAPI/TdxAPI.py`**，且是**函数内懒导入**（`import DataAPI.TdxAPI` 不会拉起 pytdx）。
  - `refresh_block_files()`（L1404，import L1438，实例化 L1446）—— 强刷板块文件
  - `_download_block_gn_from_network()`（L1259，import L1288，实例化 L1341）—— 板块成分股兜底
  - 共用 `_download_block_file()`（L1160，`get_block_info_meta` + `get_block_info` 分块）与
    `TDX_BLOCK_SERVERS`（L231，通达信 7709 行情服务器列表），落地 `T0002/hq_cache/`。
- 两条上游流程：
  1. 刷新：`Frontend/app.js:5406` POST `/api/stocks/refresh` → `FrontAPI.py:639` → `AppOrch.refresh_stock_names_async`
     → `AppRefresh.refresh_stock_names_async:577` → `_refresh_stock_names:344` → L545。
  2. 扫描：`AppScan._debug_read_page_index_stocks:354/363` → `TdxAPI.get_index_stocks:1665`
     → 仅 880xxx 且 `infoharbor_block.dat` 不可用时 → Step4 L1732 兜底。
- **`DataAPI/ElTdxAPI.py` 的 pytdx/mootdx 回退已整体注释 = 死代码**（非注释行只在 docstring 里）。
  除权除息现只走 eltdx。
- ⚠️ **缺 pytdx 是静默降级**：`refresh_block_files()` 只打 WARNING，`_download_block_gn_from_network()` 返回 `{}`，
  `get_index_stocks('880501')` 返回 `[]`。默认 venv 里**没装 pytdx**（`requirements.txt:17` 却归入"运行必需"）。


## chan.py 板块成分股双数据源语义（2026-09-11 实测，做扫描/成分股前必读）
- **Step3 与 Step4 读的不是同一个文件，也不是同一种查法**：
  - Step3 `_read_infoharbor_sector_stocks(code)` → `infoharbor[code]`，**按代码**命中
  - Step4 → `tdxzs.cfg[code]` 取板块名 → `block_*[name]`，**按名称**命中
  所以「infoharbor 拿不到 ⇒ Step4 也拿不到」**不成立**（用户 2026-09-11 据此要求删 Step4，已核验推翻）。
- **服务端文件可得性**（直连 `115.238.90.165:7709` via pytdx 实测）：
  可得 = `infoharbor_block.dat`(739135) / `block_zs`(329507) / `block_gn`(757083) / `block_fg`(453279) /
  `block.dat`(281686) / `tdxhy.cfg`(150304)；
  **不可得（size=0）= `block_hy.dat`、`tdxzs.cfg`**。
  ⚠️ `tdxzs.cfg` 不可得 ⇒ Step4 只能靠**本地通达信客户端**装的 tdxzs.cfg 才能按名查表。
- **覆盖度**：infoharbor 421 代码（GN 268 + FG 153，raw 段 GN269/FG161/ZS117 但 ZS 段第 3 字段为空
  被 `_parse_infoharbor_block` 的 `if block_code` 丢弃）；block_* 合并后 534 名称；
  infoharbor 名称 ⊂ block_*，**block_* 独有 113 个**（110 涉 block_zs、专精特新/融资融券=FG、含可转债=GN）。
- **block_*.dat 单板块硬上限约 400 只**（记录固定 2800 字节 / 每票 7 字节）。
  同名 421 个板块里 389 个数量相同、**32 个 block_* 更少、0 个更多**。
  最严重：智能机器 1216→400（丢 816）、连续亏损丢 702、新能源车丢 678、人工智能丢 672。
  代码只在 `len>=400` 时打一条 WARNING 然后**照常 return** ⇒ **静默降级，扫描会漏票**。
- `get_index_stocks` L1727 **硬拦截 8803xx/8804xx**（"旧版行业"），
  故 `block_zs.dat` 那 110 个指数板块极可能根本到不了 Step4（要么被拦、要么走 Step2 的 AKShare 路径）。
- 造合成测试数据时**必须先用真实数据查该代码是否已被占用**：我自造的 `880901/880902/880903`
  撞上了真实 infoharbor 代码（信息安全/特斯拉/水产品），导致对照实验数据失真。
- 驱动 `get_index_stocks` 做对照实验的可用手法：搭假目录 `<tmp>/vipdoc` + `<tmp>/T0002/hq_cache/`，
  把真实 `infoharbor_block.dat`/`block_*.dat` 放进去，写合成 `tdxzs.cfg`（格式 `名称|代码`，GBK 编码），
  再 `set_tdx_config(vipdoc_dir=...)`；每次调用前须重置 `_BLOCK_GN_CACHE*` / `_INFOHARBOR_BLOCK_CACHE*` 四个全局量。


## chan.py「行业映射」注入的行为与开销（2026-09-12 实测）

- **链路**：`AppEngine.py:129` **模块级**执行 `_set_tdx_hy_mapping(*app_data.load_tdxhy_mapping())`
  （"import 即注入"模式，与 `set_tdx_config` 同款）→ 读本机通达信权威源
  `TDX_INSTALL_DIR/T0002/hq_cache/tdxzs3.cfg`（GBK，**32 KB / 467 条**，
  一级30/二级128/三级309）。
- **唯一用途**：「**板块指数2/3**」。路径 _881xxx_ → `_TDXHY_881_TO_X` → X 代码 →
  `_read_tdxhy_sector_stocks` → `tdxhy.cfg` 取成分股。**其余功能一概不用**。
- **为何扫自选股/成分股会打印 N 条**：不是"每次调用都读"，而是 ProcessPool(spawn) 的
  **每个 worker 进程**。`AppScan.py:29 from App import AppEngine as _m` → worker 各
  import 一次 → 各打印一条。N 条 ≡ workers=N，严格一一对应（实测 workers=10 → 10 条）。
- **同进程内不重复**：`AppData._load_tdxhy_mapping` 有 (path,mtime,size) 缓存，命中直接
  `return`（在 `log.info` **之前**）→ 单进程只读盘一次、只打印一次。**行为正确**。
- **开销：可以忽略**。冷读 0.002s，缓存命中 0.0000s；`_load_tdxhy_mapping` 占
  `import AppEngine`(0.81s) 的 **0.00%**。worker 冷启动真实成本在别处：
  pandas/numpy/requests 0.47s、TdxAPI 0.64s、AppEngine 链 0.8~1.4s（10 个并发 4.47s）。
- **worker 并不需要它**：AppScan 用 AppEngine 只为 `analyze_stock` / `_get_market_code`；
  `get_index_stocks`（含 881 分支）**只在主进程侧**跑，用于准备股票列表。
- **结论**：这是「浪费但极廉价」。为性能改没意义；真要改的理由应是**日志刷屏**
  与「文件缺失时每 worker 各打一条 WARNING」。要改就把注入挪进显式 `ensure_*()`
  （由 FrontAPI 启动 + `_read_tdxhy_sector_stocks` 首次触发），但须同步改
  `Test/test_phase5_guards.py` 里"import AppEngine 即完成注入"那条断言。


## eltdx 环境补充（2026-09-11）
- **managed `default` venv 里也已装好 eltdx 3.1.7**（`pip install eltdx` 拉的是 cp310-abi3 wheel，秒装无需 Rust）：
  `C:\Users\river\.workbuddy\binaries\python\envs\default\Scripts\python.exe`。
  该 venv 同时有 pytdx，**做 eltdx vs pytdx 对照实验就用它**（比项目 `.venv` 更干净）。
- FastAPI/uvicorn 是可选依赖，`pip install eltdx` 不会装它们，也不会起额外服务。


## ⭐ eltdx 能拿「PE-TTM」和「流通市值」——别再默认只能拿 XDXR（2026-09-12 全量实测）

用户问过「流通市值/PE 能不能像除息除权那样也从 eltdx 取」。答案：**能**，且 PE 快 10 倍。

| 目标 | 调用 | 单位 | 实测 |
|---|---|---|---|
| **PE-TTM（全市场）** | `client.resources.read_stats()` → `stats.stat[(market_id,code)].pe_ttm` | 倍 | **8037 只 / 1.16~1.36s / 单次 `0x06B9`**（腾讯现状 27 批 / 12.9s） |
| **流通市值** | `client.helpers.stock_profile_table(codes)` → `circulating_market_value` | **元**（腾讯是**亿元**） | 5224 只 7.73s；用 `finance_batch`+`get_snapshots` 自算 ≈2.6s |
| 流通股本 | `client.corporate.finance_batch(codes)`（`0x0010`）→ `circulating_shares` | 股 | 5224 只 66 批 / 1.21s |

- `market_id`：**0=sz / 1=sh / 2=bj**（北交所 347 只，`stats` 里覆盖）。
- 载体是 **`resources.read_stats()`** → 下载 `zhb.zip` 解析其中 **`tdxstat.cfg`（第 3 列=滚动市盈率）/ `tdxstat2.cfg`**。`0x06B9` 是同一命令。
- **一致性（2026-09-12 盘后实测）**：PE 对腾讯 `[39]` → **97.60% 浮点严格相同**（5224 A股：相同 5099 / 差≤0.05 98 / 差>0.05 22，仅 7 例相对差>1%，集中亏损股/科创次新/周期股；eltdx 空 5 只全为次新股，其中 2 只腾讯返回 0 而调用方本就过滤 0）。流通市值对腾讯 `[44]` → **98.75% 的差异只是腾讯「亿元两位小数」取整**（半格 50 万元），最大的 3 例（4.06%/2.27%/1.16%）根因是 **`0x0010` 流通股本滞后于解禁/增发**（价格完全一致）。
- ⚠️ **eltdx 零港股支持**：`codes.all("hk")` → `ValueError: invalid market`；`stock_profile_table(["hk00700"])` → `ProtocolError: invalid code`。而腾讯 `hk00700` 正常返回 PE/市值。**凡同时处理 A 股+港股的链路（如 chan.py `_refresh_pe_ttm`）不能全量替换。**
- ⚠️ **北交所批量快照会整批抛错**：`get_snapshots([bj9200xx])` 单只 20/20 OK，但 `get_snapshots(bj[i:i+10])` 必现 `ProtocolError: snapshot record marker not found: bj920025`（**整批失败，非跳过单条**）。走 `0x054C` 的 `stock_profile_table`/`full_quotes` 同理 → 候选池含北交所须按市场分批隔离。
- ⚠️ **时间口径不可外推**：`tdxstat.cfg` 是**盘后快照**（只有 `stats_date` 日期，无时刻），腾讯是实时接口。**周六盘后测出的 97.6% 一致 ≠ 盘中一致**，盘中须复测。
- 附带纠正：腾讯 `[39]` 文档标签写「市盈率-**动态**」，但其值与 TDX **滚动市盈率**严格相等 → 实为 **TTM** 口径。

**方法论提醒**：用户提「统一到单一数据源方便维护」时，除了答"能不能"，**必须同时算清故障域**——eltdx 已是前复权+板块刷新的单点，再并入 PE/市值会让一次 7709 故障同时打掉 4 个功能；且要给出**可回退的分档方案**（档1 只归一化最受益项 → 档2 → 档3 全量），而非二元答复。


## eltdx 接口补充事实（2026-09-12 实测，eltdx 3.1.7 已装在默认 venv）
- `TdxClient.corporate` 只有 3 个方法：`adjustment_factors` / `capital_changes` /
  `finance_batch`；`resources` 有 `download_file` / `read` / `read_stats`；
  `quotes` 有 `get_snapshots` / `get_depth` / `list_by_category`。
- **`capital_changes(code: str | Sequence[str], batch_size=75)` 支持批量**，但
  「支持批量」**不等于**「该做全量预取」——用户已明确裁决按需逐只（见下节结论）。
- **`resources.read_stats()` 单请求覆盖全 A 股 PE-TTM**（实测 8037 行、约 1.3s）
  → 取 1 只与取全市场成本相同，**应做成进程级全量缓存**，别逐只调。
- **统计文件只含 A 股个股，不含指数**：sh000001 / sh000300 这类指数走 eltdx 必然
  取空 → 指数 PE 必须保留腾讯通道（按**标的类型**分流，不能按市场分流）。

### eltdx 批量接口的性能形态（2026-09-12 实测）
> **结论先行（用户裁决）**：是否做「进程级全量预取」取决于**取数形态是否为一次性
> 全市场**。`read_stats` 是（→ PE-TTM 做全量缓存，正确）；`capital_changes` 不是
> （→ **除权除息按 0912 原样「加载哪只就读哪只」，不预取、不落盘**）。
> 我曾在此犯错：查明形态差异后只用它解释耗时，没回头质疑最初的设计前提，于是在
> 错误方向上又做了一轮优化。识别到前提不成立要**立刻回退整条分支**，别优化它。
> 下面几点是形态事实与调参经验，保留备查。

- **两类数据源形态不同，别当成一回事**：
  · `resources.read_stats()` = **一次性下载全市场统计文件**（1.05s / 8037 行），
    取 1 只与取全市场等价 → 适合进程级全量缓存；
  · `corporate.capital_changes(codes)` = **按代码查询的命令**，没有"全市场打包"
    这回事，耗时 = 往返次数 × 单次成本（单只实测仅 0.11~0.14s）。
- **批大小是主导因素**：全 A 股 5303 只，75/批(71 次往返)=5.39s，
  500/批(11 次)=2.35s。协议实测一次可喂 1000 只，别被库的默认 75 带偏。
- **务必把 `with client:` 放在批次循环外**：写在循环内等于每批一次 TCP 握手，
  实测同样数据量差约 2.2 倍。
- **别在预取阶段构造 DataFrame**：5500 只逐只 `pd.DataFrame` + 归一化实测 ~9.2s，
  而用户通常只打开个位数标的。改成「轻量 records → 取用时才转 DataFrame」，
  两级缓存即可。
- **全 A 股 ~5649 只**（sh 2361 / sz 2942 / bj 346，`codes.all()` 的
  `category in ('a_share','b_share')`；注意 category 是字符串枚举）。
- `codes.all()` 返回 dataclass `SecurityCode`（字段 exchange/market_id/code/
  name/category/board…），**不是 dict**，不能 `.get()`，要用 getattr。

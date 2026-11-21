# 评审报告：custom-dev(`7e1a034`) vs `1120`

- 评审时间：2026-11-21
- 评审对象：github.com/jbxenzxy/chan.py，`1120`（annotated tag → `13ca76b`）与 `origin/custom-dev`（`7e1a034`）之间的全部改动
- 评审方式：远端 fetch + `git archive` 出两棵树 A/B 对照；AST 级符号集合比对；改动相关测试两版全跑；新树全量门禁
- 结论：**改动正确，未发现引入 Bug，可合并**

---

## 一、结论总表

| 项目 | 结论 |
|---|---|
| ref 关系 | `1120` = annotated tag（→ `13ca76b`）；merge-base 即 1120 自身，`custom-dev..1120` 反向 log 为空 ⇒ **纯快进**，共 6 个提交 |
| 改动范围 | 15 文件 +1580/−246，集中在前端 `Frontend/app.js`、`App/AppMargin.py`、`app.html`/`app.css` 及 8 个测试文件 |
| 门禁 | **202/203 绿**；唯一红 `scan_panel_e2e` 为环境 flaky（两版复跑定性，与本改动无关，见 §五） |
| 工作树状态 | 本地未提交的 6 个文件改动与远端 HEAD `7e1a034` **逐字节一致**（`git diff 7e1a034 -- <6文件>` 为空），即本地改动就是 `809dce9`+`7e1a034` 的内容 |

## 二、改动内容（6 提交分三段）

1. **11-20 顶栏收纳（4 个提交：`76799a3`/`7a7ee98`/`fa37d30`/`cd9d3cc`）**
   - 删除「查询」按钮：回车即查；`test_header_topbar_layout` ⑦ 用「按钮已删除」钉住该前提。
   - 刷新键搬到最左端 + 删页内文字进度：消除右半段 72–78px 横移。
   - 5 颗图层开关收进设置抽屉「图层显示」节；笔编号 `biidx` 并入同一套键（不另立映射）。
   - 图层管理收敛为单一映射表 + 唯一回填点 `syncOverlayControls`。

2. **`809dce9`（11-21 09:15）= 上一轮 P3-1 闭环修复的合入**
   - `refreshOpenDataPanels()` 定义 + 3 个挂点 + 3 个测试补强，与 2026-11-20 评审交付逐字一致；定义与挂点同提交自洽。

3. **`7e1a034`（11-21 10:54，HEAD）= 本轮新逻辑**
   - **AppMargin 末点完整性收口**：`_parse_rows` 对「残缺日」整日剔除（判定：某市场集合是上一个已接纳日市场集合的严格子集 ⇒ 该日未披露完），修复末点垂直下扎。
   - **`syncDataBtnState` 三按钮可用性状态机**：期货页全灰、股票页仅日 K 放行两融/量能、扫描恒可用；唯一写入点 + 灰化即关面板 + 三个入口守卫（`refreshOpenDataPanels`、期货单窗 init、扫描结束收口）。

## 三、正确性实证

- **符号集合差（AST 级比对 `symdiff.py`）**：删除 `applyOverlayButtonStates`/`marginIsAvailable`/`updateAmoButtonState`/`updateMarginButtonState`/`onShowBiIdxChange` 后**零活引用残留**（全仓只剩刻意保留的沿革注释）；`toggleOverlay`/`onOverlayChange` 双入口同走 `_overlaySet` 三步，无第二写入点；两版 app.js `node --check` 语法均通过。
- **截断风险排除**：担心「某市场永久缺数 ⇒ 整条曲线截断」——沿数据流核实 `fetch_margin_rows` 无缓存、每次全量重拉，上游补齐后**自愈**，该路径不可达。
- **扫描按钮 disabled 闭环**：扫描期可点（语义=中断）、中断态由流程自管、结束由 `syncDataBtnState` 按页面状态收口；旧 `btn.disabled=false` 已删且确被取代，无孤儿写入点。
- **⑫ 契约翻转是变更不是作弊**：`test_app_margin.py` ⑫ 从「恒可用」翻成「状态机」，fixture 补强（01–03 补北交所行 160→165 亿）+ 正反两向覆盖（缺市剔除 / 集合变大不算残缺），docstring 明确记录取代关系。
- **A/B 回归**：14 个改动相关测试两版全绿；新增测试 `test_header_topbar_layout`、`test_overlay_settings_move` 具备判别力（静态位置/删除面/样式倒挂 + node 真函数行为面）。

## 四、门禁（Test/run_all.py）

- 新树 `7e1a034` 全量 203 组件：**202 绿 / 1 红**。
- 唯一红：`scan_panel_e2e`，报 `EBUSY` 读 Chrome `DevToolsActivePort` 文件锁竞态。
- 定性：两版（old `1120` / new `7e1a034`）单独重跑各 **12/12 全绿** ⇒ 环境 flaky，与既有 `zxg_write_concurrency` 同类；本次改动未碰 `Test/e2e/`。

## 五、遗留问题（均 P4，不影响运行，可顺手修）

1. `App/AppMargin.py:105` 函数级 docstring 仍写「缺失的市场按有则加，不丢弃该交易日」，与同函数 125–139 行的整日剔除逻辑**矛盾**；模块顶部 docstring 已更新、函数级漏同步。
2. `Frontend/app.html:112-121` 注释「2026-11-20 取消灰化 / JS 不再运行时覆写 title（静态值就是唯一来源）」已被 `syncDataBtnState` 的运行时覆写 + 灰化**推翻**，注释失真。

## 六、评审方法备注

- 沙盒物料（可复查）：`C:\Users\river\WorkBuddy\2026-11-21-11-01-50\sbx\`
  - `old/`（1120 树）、`new/`（7e1a034 树）、`symdiff.py`（符号集合比对）、`runner_ab.py`（A/B 批量 runner）、`gate_new.log`（全量门禁日志）。
- 历史基准：上一轮（2026-11-20，§9.59）已评审 `1120..cd9d3cc`；本轮增量评审 `cd9d3cc..7e1a034`（`809dce9`、`7e1a034`）。

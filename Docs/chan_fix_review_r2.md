# chan.py custom-dev 整改复查（第二轮）

- 基线：`1a0aba5`（第一轮整改末态）
- 本轮：`8596d94`（merge，含 `196c1ae`「更新」+ 4 个删 JSON 提交）
- 范围：21 files changed, +582 / −310
- 本轮新增/修改关键文件：
  `DataAPI/ElTdxAPI.py`, `App/AppDownload.py`, `FrontAPI.py`, `App/AppScan.py`,
  `App/AppOrch.py`, `App/AppEngine.py`, `App/AppSSE.py`, `DataAPI/TdxAPI.py`,
  `Test/func_map_check.py`, `Test/run_all.py`, `Test/test_phase3_guards.py`,
  `Test/snapshots/phase3_routes.json`, `Test/test_futures_session_binding.py`(新),
  删除 `App/{last_code_freq,stock_names,stock_pettm_index,stock_float_mc}.json`

---

## 一、本轮修复确认（均正确）

### P0-3 下载后不失效分析缓存 —— ✅ 修对，且修复链路完整
- `ElTdxAPI._download_task` 新增 `on_finish` 回调参数；`AppDownload.start_download_checked`
  注入 `stocks_cache_clear`；新增 `POST /api/stocks/cleanup` 手动入口。
- **`on_finish()` 执行位置验证（读源码 `_download_task` 442–612 行）：**
  放在 `try/except/finally` **之后**、`from chinese_calendar import is_workday`（600 行）**之后**。
  由于单股异常(568)、引擎异常(583)、中止 break(539) 均被各自 `except` 吞掉后落到 `finally`，
  再顺序执行到 `on_finish`——**成功 / 单股报错 / 引擎异常 / 中止 四条路径都会触发**。
- `on_finish` 自带 `try/except`，缓存失效失败不反噬下载收尾。
- 链路闭合验证：`app_data.stocks_cache_clear` 存在于 `AppData.py:1437`，
  `FrontAPI → orch.stocks_cache_clear → AppDownload.stocks_cache_clear → app_data.stocks_cache_clear` 全通。
- `ElTdxAPI` 保持零 App 依赖（失效动作由调用方回调承担），符合分层约束。

### 已跟踪 JSON —— ✅ 已删
4 个 `App/*.json` 从 git 移除。它们是运行时缓存，缺失时由 `AppRefresh`/`AppData` 懒加载重建，
从仓库删除只是避免提交过期缓存；快照比对早已用 `_STRIP_KEYS` 剥离 `name/pe_ttm`，不受影响。
`fixtures_integrity` 不依赖这些文件（Test/ 中出现的匹配仅是文档/映射文件里的字符串）。

### Docs 反增 —— ✅ 已修
Docs 行数 `1a0aba5` 55,430 → 本轮 **25,998**（删约 29k 归档 HTML），低于最初基线 47,978。

### run_all 注册 —— ✅ 修对
`futures_sub_key`、`futures_session_binding` 均已注册；连同上一轮已注册的
`app_amo_behavior`、`stocks_dual_algo`，最有价值的测试现都纳入统一运行。

### AppScan.clear_cache 语义 —— ✅ 修对（修掉上轮「关面板误清整池」
返回 `{"cleared": 0}`，文档明确「共享分析缓存由下载完成回调 / `/api/stocks/cleanup` 失效」。
`test_phase4_guards.py` 同步删除了对「关面板清缓存」的旧断言（本轮 −1 行）。

### TdxAPI 僵尸参数清理 —— ✅ 正确（仅清理不彻底）
`get_index_stocks`、`_read_hk_index_stocks`、`_read_standard_index_stocks`、
`_read_sh_index_stocks_exchange` 的 `abort_check` 形参已删除，调用处也不再下传。
调用方 `AppScan.py:129` 不传 `abort_check`，无回归。
遗留：`_run_with_timeout` 自身的 `abort_check` 参数与轮询逻辑仍保留（现恒为 None，死代码，无害）。

### AppEngine / AppSSE / AppOrch / func_map_check —— ✅ 一致
- `AppEngine` 删除 `_load_float_mc_cache` 死壳；`func_map_check.py` 同步把该映射改为注释。
- `AppSSE` 把误导性变量 `main_records` 改名为 `_main_klines` 并加注释（第二项实为 klines
  DataFrame、本分支不用），纯命名/注释修正，无行为变化，`py_compile` 通过。
- `AppOrch` 导出 `stocks_cache_clear`，`FrontAPI` 可解析。
- `func_map_check` 同步 `api_stocks_scan_close`（仅关面板）/ `api_stocks_cleanup`（P0-3 手动入口）映射。

---

## 二、上一轮已修、本轮完好（未回退）

| 项 | 状态 | 证据 |
|----|------|------|
| P0-1 SSE 记录缓存会话绑定 | ✅ | `test_futures_session_binding.py` 7/7 通过 |
| P0-2 期货下窗缓存键 | ✅ | `test_futures_sub_key.py` 全过；新测试已注册 |
| P0-4 OUTPUT_DIR 启动崩溃 | ✅ | `FrontAPI.py:655/662` 已删不存在符号回退分支 |
| P1-2 AppSSE root 日志 WARNING | ✅ | 本轮未触碰，仍修好 |
| P1-5 扫描中断 | ✅ | 本轮未触碰，仍修好 |
| P1-7 前端四分支合一 | ✅ | 上轮 717→404 行，本轮 `app.js` 无改动 |
| P2 死代码 | ✅ | 本轮进一步删 `_load_float_mc_cache` + 4 JSON |

---

## 三、四个暂缓项确认未动（与你的声明一致）

| 项 | 当前事实 | 是否符合「暂不处理」 |
|----|----------|----------------------|
| P1-1 四把锁未集中 | 无集中锁对象（`EngineLock`/`_LOCKS={}` 等均未出现） | ✅ |
| P1-3 REPLAY_MODE 互踩 | `AppEngine.py:638/644/740` + `AppSSE.py:313-321/718-725` 仍直接改类级全局 | ✅ |
| P1-4 内核反向依赖 | `BuySellPoint/BSPointList.py:698/725` 仍 `from App.AppData import ...` | ✅ |
| step 全链路 · 两个死端点 | `futures/read/status`、`futures/read/config` 仍在 `FrontAPI.py:525/531` | ✅ |

> 备注：两个死端点虽未删，但 `test_phase3_guards.py` 已把它们**重新加回 EXPECTED_ROUTES**，
> 使守卫测试恢复为「反映真实代码」——这是正确做法（决定删端点是后续单独确认的事）。
> 当前快照(31)与 EXPECTED(30) 仍差 1 行（仅当快照文件缺失时 EXPECTED 才生效，不引发误报）。

---

## 四、非暂缓、仍未修的问题（唯一）

- **P1-6 ChanApp.components 死注册表**：`Frontend/app.js` 中仍只有 `ChanApp.components.x = {`
  写入，无任何读取点。无害死代码，低优先级。建议后续连同前端大重构一并清理。

---

## 五、是否引入新问题？—— 否（仅 1 处理论脆弱点，不劣于原状）

1. **P0-3 的 `on_finish` 位于 `chinese_calendar` import 之后（低优先级）**
   若 `chinese_calendar` 未安装，600 行 `from chinese_calendar import is_workday` 抛
   `ImportError` 会跳过 606 行的 `on_finish`，缓存不失效。但该 import 是**改动前就存在**的
   前置代码——它缺失时下载本就会在 600 行失败，旧代码同样不会失效缓存。故「本次未 worse」，
   仅提示：`chinese_calendar` 未列入 `requirements.txt`，建议要么声明依赖、要么将 `on_finish`
   移入独立 `try` 块（放在 chinese_calendar 处理之前/之中），彻底隔离。

2. `_run_with_timeout` 残留 `abort_check` 死参数（见上，无害）。

3. `Test/test_app_amo.py:275` 有 `SyntaxWarning: invalid escape sequence '\d'`
   （字符串应为 raw），属预存 lint 噪音，不影响测试。

---

## 六、验证证据（本轮实测）

```
py_compile FrontAPI.py App/*.py DataAPI/*.py Test/*.py   → 全部通过 ✅
Test/test_futures_session_binding.py   → 7/7 通过 ✅ (P0-1 回归)
Test/test_futures_sub_key.py           → P0-2 断言全过 ✅
Test/func_map_check.py                 → 28函数/28状态/0类/37路由 全部归属且与代码同步 ✅
```

---

## 结论

你本轮修复的 P0-3、已跟踪 JSON、Docs 反增**均修改正确、链路完整、未引入实质回归**；
上一轮已修项（P0-1/P0-2/P0-4/P1-2/P1-5/P1-7/部分 P2）在本轮完好、无回退；
四个暂缓项确按声明未动。唯一「非暂缓却未修」的是 **P1-6 死注册表**（无害，可后置）。
新引入问题：无（仅有 1 处 `chinese_calendar` 排序脆弱点，不劣于原状，优先级低）。

下一步建议（按性价比）：
1. 声明 `chinese_calendar` 依赖，或将 `on_finish` 移入独立 `try` 块（~10 行，杜绝脆弱点）。
2. 收掉 `_run_with_timeout` 残留 `abort_check`（~15 行，纯清理）。
3. 四个暂缓项单独约时间确认；P1-6 可顺带清。

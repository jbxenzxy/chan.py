# Backtest/Exp —— 出场参数实验脚本（**不是单测**）

## 这里是什么

一次性/可重跑的**实验脚本**，用于验证「改一个出场参数到底有没有用」。
配套文档：`Docs/止盈止损/出场参数回测手册.md`（SOP）+ `Docs/止盈止损/00_回测交接说明.md`（上手）。

## 和 `Backtest/Test/` 的区别（别混淆）

| | `Backtest/Test/` | `Backtest/Exp/`（本目录） |
|---|---|---|
| 性质 | 单测 / 契约测试（`test_bt01`~`bt11`） | 人工发起的实验 |
| 执行 | `Test/run_all.py::COMPONENTS` **显式登记**后进出门禁 | **不进**门禁，跑不跑由人决定 |
| 断言 | 有，失败即红 | 无，输出指标给人看 |
| 改了要不要重跑 | 必须 | 视需要 |

**本目录的文件不会自动进入 `Test/run_all.py`**（该门禁用手写 `COMPONENTS` 列表，无自动发现）。
反过来也请注意：任何「已注册组件 vs 可发现脚本」的差集审计都会把本目录算作**未登记**，
这是预期行为，不是漏登记 —— 请不要把实验脚本补登进门禁。

## 怎么跑

```bash
mkdir -p Backtest/Exp/.kcache
cp Backtest/Exp/_universe.json Backtest/Exp/.kcache/     # 固定股票池（可选，但基线锚点需要）
python Backtest/Exp/case_csbm.py sz000158 w 800          # 环境自检
```

脚本会自动定位仓库根（本目录的上两级），可任选工作目录调用。
逐个脚本的用途见手册第 8 节。

## 数据

- `_universe.json`：800 只股票池快照（固定种子打散），**需入库**。
- `.kcache/`：K 线缓存，约 60MB / 800 只，**不入库**（`.gitignore` 应忽略它，删掉会全量重抓）。
- 取数走 `ifzq.gtimg.cn`（腾讯前复权，境内可直连）；`fetch_kline.py::_quality_ok()` 会剔除复权出负价的标的。

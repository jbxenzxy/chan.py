# -*- coding: utf-8 -*-
"""
Test/test_15m_period.py —— 15m 周期 + 30m 分桶回归测试（零第三方依赖）
=========================================================================
覆盖评估《chan.py_custom-dev_vs_v4.0_分析报告》的采纳项：
  P1    _get_date_fmt(15m)=带时分 + INTRADAY_FREQS 双副本一致含 15m
  P2-2  15m+5m 双窗纳入加载量优化分支
  港股30m  anchor 分桶 bug 修复（A股零回归、港股 12:00/16:00 桶正确、单一事实源）
  拒绝 P2-1：保留「单窗15m/5m 灰化双窗口入口；双窗内 15m 仍可作上窗」（见 test_dual_consistency）

设计要点：
  - 沙箱/CI 可能无 pandas/numpy，无法 import 整个 TdxAPI/AppEngine；
    故用 ast 提取被测的目标函数/常量（它们仅依赖 stdlib），保证任何环境可直跑。
  - 30m/15m 分桶用「09:30 锚点」独立参考重采样器逐字段比对照，非自证循环。

运行： python Test/test_15m_period.py
"""
import ast
import os
import re
import sys
from collections import OrderedDict, Counter
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from Common.CEnum import FREQ_TABLE, INTRADAY_FREQS, SUBSECOND_FREQS


# ═══════════ 通用：AST 提取某文件中指定顶层定义并在隔离命名空间执行 ═══════════
def extract_from_file(rel_path, names, extras=None):
    ns = dict(extras or {})
    ns.setdefault("OrderedDict", OrderedDict)
    ns.setdefault("timedelta", timedelta)
    ns.setdefault("datetime", datetime)
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    pending = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    pending.append((t.id, node))
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            pending.append((node.name, node))
    gotten = {}
    for name, node in pending:
        code = compile(ast.Module([node], type_ignores=[]), "<%s>" % name, "exec")
        exec(code, ns)
        gotten[name] = ns.get(name)
    missing = set(names) - set(gotten)
    assert not missing, f"{rel_path} 缺少定义: {missing}"
    return gotten


# ═══════════ 参考重采样器（独立实现，不依赖被测代码） ═══════════
def anchor_bucket(dt, period_min):
    ref = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    ms = int((dt - ref).total_seconds()) // 60
    return ref + timedelta(minutes=((ms + period_min - 1) // period_min) * period_min)


def sess_times(start, end, step=5):
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=step)
    return out


def mk_recs(times):
    return [{"dt": d, "open": 10.0, "high": 10.6, "low": 9.4,
             "close": 10.2, "vol": 100, "amount": 1000.0} for d in times]


def check_resample(loader, period_min):
    """对 15m/30m 两市场做：桶数与锚点一致 + 每桶根数=period/5 + 逐字段一致。"""
    cases = [
        ("sh", sess_times(datetime(2024, 1, 2, 9, 35), datetime(2024, 1, 2, 11, 30))
              + sess_times(datetime(2024, 1, 2, 13, 5), datetime(2024, 1, 2, 15, 0))),
        ("hk", sess_times(datetime(2024, 1, 2, 9, 35), datetime(2024, 1, 2, 12, 0))
              + sess_times(datetime(2024, 1, 2, 13, 5), datetime(2024, 1, 2, 16, 0))),
    ]
    for market, times in cases:
        recs = mk_recs(times)
        out = loader(list(recs), period_min)  # 统一 _resample_5m(records, period_min)
        # 桶时刻 == 独立锚点
        ref_buckets = sorted({anchor_bucket(r["dt"], period_min) for r in recs})
        got_buckets = [r["dt"] for r in out]
        assert got_buckets == ref_buckets, f"[{market}]{period_min}m 桶错位: {got_buckets}"
        # 每桶根数恰 period/5
        cnt = Counter(anchor_bucket(r["dt"], period_min) for r in recs)
        assert all(c == period_min // 5 for c in cnt.values()), \
            f"[{market}]{period_min}m 桶内根数畸形: {cnt}"
        # 逐字段一致
        refmap = {}
        for r in recs:
            refmap.setdefault(anchor_bucket(r["dt"], period_min), []).append(r)
        for k in ["open", "high", "low", "close", "vol"]:
            for r in out:
                g = refmap[r["dt"]]
                if k == "open":
                    val = g[0]["open"]
                elif k == "high":
                    val = max(x["high"] for x in g)
                elif k == "low":
                    val = min(x["low"] for x in g)
                elif k == "close":
                    val = g[-1]["close"]
                else:
                    val = sum(x["vol"] for x in g)
                assert r[k] == val, f"[{market}]{period_min}m 字段{k}不符 @ {r['dt']}"
    return True


# ═══════════ 测试用例 ═══════════
def test_15m_synth(loader15):
    check_resample(loader15, 15)
    print("[PASS] 15m 合成: A股 16桶 / 港股 22桶，桶内3根、逐字段与锚点一致")


def test_30m_synth(loader30):
    check_resample(loader30, 30)
    print("[PASS] 30m 合成: A股 8桶 10:00..15:00（零回归）/ 港股 11桶含12:00、16:00，每桶6根")


def test_get_date_fmt_15m(fu):
    fmt = fu["_get_date_fmt"]("15m")
    assert fmt == "%Y/%m/%d %H:%M", f"_get_date_fmt('15m')={fmt!r}，应为带时分（否则红框时分被截断）"
    assert fu["_get_date_fmt"]("d") == "%Y/%m/%d"
    print("[PASS] _get_date_fmt('15m')=%Y/%m/%d %H:%M（红框时分不截断）")


def test_freq_single_source():
    """周期分类收敛为 Common.CEnum.FREQ_TABLE 单一事实源；func_util/高层仅再导出，不内联副本。"""
    assert "15m" in INTRADAY_FREQS, "Common.CEnum.INTRADAY_FREQS 缺 15m"
    assert "15m" in FREQ_TABLE, "Common.CEnum.FREQ_TABLE 缺 15m 定义"
    # func_util：不再内联集合，改为从 CEnum 导入再导出
    with open(os.path.join(ROOT, "Common/func_util.py"), encoding="utf-8") as f:
        fu_src = f.read()
    assert "INTRADAY_FREQS = {" not in fu_src, "func_util 仍内联 INTRADAY_FREQS"
    assert "SUBSECOND_FREQS = {" not in fu_src, "func_util 仍内联 SUBSECOND_FREQS"
    assert "from .CEnum import" in fu_src and "INTRADAY_FREQS" in fu_src, \
        "func_util 未从 CEnum 导入周期分类"
    # 高层：不复制周期分类，仅从 Common 导入（App/utils 的 _get_date_fmt 来自 func_util 再导出）
    for rel in ("BuySellPoint/BSPointList.py", "App/utils.py"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            src = f.read()
        assert "INTRADAY_FREQS = {" not in src, f"{rel} 仍存在 INTRADAY_FREQS 内联副本"
        assert "SUBSECOND_FREQS = {" not in src, f"{rel} 仍存在 SUBSECOND_FREQS 内联副本"
        assert "def _get_date_fmt(" not in src, f"{rel} 仍存在 _get_date_fmt 内联定义"
        assert "from Common.CEnum import" in src or "from Common.func_util import" in src, \
            f"{rel} 未从 Common 导入周期分类"
    # BSPointList 不再内联 KL↔freq 双向映射
    assert "KL_TYPE.K_15S: '15s'" not in src, "BSPointList 仍内联 KL↔freq 映射（应改从 CEnum 派生）"
    print(f"[PASS] 周期分类单一事实源 Common.CEnum.FREQ_TABLE（含15m），func_util/高层不再复制副本")


def test_30m_single_source():
    with open(os.path.join(ROOT, "DataAPI/TdxAPI.py"), encoding="utf-8") as f:
        src = f.read()
    assert "def _bucket_30min" not in src, "30m 手写分桶副本仍在（应统一为锚点法单一实现）"
    # 评估者合并重构：15m/30m 统一为单一实现 _resample_5m(period_min)
    assert "def _resample_5m(" in src, "缺少统一 _resample_5m(period_min) 合成实现"
    # 两个外层包装均委托统一实现（保持对外函数名/签名不变）
    assert "return _resample_5m(records, 30)" in src, "30m 未委托统一 _resample_5m"
    assert "return _resample_5m(records, 15)" in src, "15m 未委托统一 _resample_5m"
    # read_tdx_min_file 必须委托统一 _resample_5m 合成（单一事实源），不得原地手写 30m 分桶。
    # 不绑定委托的具体参数形式——历史/等价写法有：
    #   return _resample_5m_to_30m(records, market)
    #   return _resample_5m_to_30m(records, market=market)
    #   return _resample_5m_to_30m(records)
    #   return _resample_5m(records, 30)
    # 全部等价，只守护本质：以 _resample_5m 家族为委托点、read_tdx_min_file 内部
    # 不再原地循环分桶。若再因具体参数文本误报，说明本断言又过度绑定实现了。
    m = re.search(r"def read_tdx_5min_file.*?(?=def |\Z)", src, re.S) or \
        re.search(r"def read_tdx_min_file.*?(?=def |\Z)", src, re.S)
    # 最新代码已把 read_tdx_min_file 改名为 read_tdx_5min_file（见 custom-dev
    # DataAPI/TdxAPI.py）；为向后兼容旧名，两者均接受。核心守护的是「此 5m→30m
    # 合成入口必须委托统一 _resample_5m 合成（单一事实源）」，不得原地手写分桶。
    assert m, "未找到 read_tdx_5min_file / read_tdx_min_file（30m 合成入口）"
    assert re.search(r"return\s+_resample_5m(_to_30m)?\(", m.group(0)), \
        "5m→30m 合成入口未委托统一 _resample_5m 合成（仍在原地手写分桶）"
    print("[PASS] 15m/30m 统一 _resample_5m 合成 + 双包装委托 + 5m→30m 入口委托统一合成")


def test_30m_p2_2_optim_branch():
    with open(os.path.join(ROOT, "App/AppEngine.py"), encoding="utf-8") as f:
        src = f.read()
    # 15m+5m 须纳入与 30m+5m 同一条 5m 复用优化分支
    assert re.search(r"freq in \('30m', '15m'\) and sub_freq == '5m'", src), \
        "加载量优化分支未纳入 15m（缺失 15m+5m 复用）"
    print("[PASS] 15m+5m 纳入双窗 5m 文件复用优化分支（P2-2）")


def test_dual_consistency():
    """保留 P2-1 拒绝语义：单窗 15m/5m 灰化入口，但双窗内 15m 仍可作上窗。"""
    # 后端配对表含 15m（双窗内 15m 上窗 → 5m）
    appeng = extract_from_file("App/AppEngine.py", ["_STOCKS_DUAL_PAIRS"])
    assert appeng["_STOCKS_DUAL_PAIRS"]["15m"] == {"5m"}, "_STOCKS_DUAL_PAIRS 缺 15m 配对"
    # 前端配对空间含 15m
    with open(os.path.join(ROOT, "Frontend/app.js"), encoding="utf-8") as f:
        js = f.read()
    assert "'15m': ['5m']" in js, "前端 STOCKS_DUAL_PAIRS_JS 缺 15m 配对"
    # 双窗内 15m 上窗默认下窗
    assert "if (mainFreq === '15m') return '5m';" in js, "getDualSubFreq 缺 15m 映射"
    # 单窗入口灰化：股票 15m/5m 均 disabled（拒绝评估者"放开15m"的建议）
    assert "(currentFreq === '15m' || currentFreq === '5m')" in js, \
        "前端应保持单窗 15m/5m 灰化双窗口按钮"
    assert "disabled = (currentFreq === '5m')" not in js, "前端被改回仅禁 5m（违背单窗15m灰化需求）"
    print("[PASS] 单窗15m/5m 灰化入口 + 双窗内15m可作上窗，两端一致")


def test_market_freq_partition():
    """市场周期支持集单一事实源：股票/期货周期按市场隔离，消费方与其一致，前端镜像守卫。"""
    from Common.CEnum import (STOCKS_FREQS, FUTURES_FREQS, STOCKS_FREQS_ORDERED,
                              FUTURES_FREQS_ORDERED, FREQ_SEC_MAP)
    assert STOCKS_FREQS == frozenset({"w", "d", "30m", "15m", "5m"}), "STOCKS_FREQS 不符合股票周期"
    assert FUTURES_FREQS == frozenset({"30m", "5m", "1m", "15s"}), "FUTURES_FREQS 不符合期货周期"
    # 客观属性仍共享，未按市场复制 FREQ_TABLE（两市场并集须落在客观周期表内）
    assert set(STOCKS_FREQS) | set(FUTURES_FREQS) <= set(FREQ_TABLE), "市场周期并集越出 FREQ_TABLE"
    # 期货 SUPPORTED_FREQS 派生自 FUTURES_FREQS，且顺序 = FREQ_TABLE 序
    tq = extract_from_file("DataAPI/TqSdkAPI.py", ["SUPPORTED_FREQS"],
                           extras={"FUTURES_FREQS_ORDERED": FUTURES_FREQS_ORDERED})
    assert set(tq["SUPPORTED_FREQS"]) == FUTURES_FREQS, "期货 SUPPORTED_FREQS 与 FUTURES_FREQS 失配"
    assert tq["SUPPORTED_FREQS"] == FUTURES_FREQS_ORDERED, "SUPPORTED_FREQS 顺序未按 FREQ_TABLE 序"
    # 回看配置 keys ⊆ 各自市场支持集（纯 AST 语法遍历，避免 exec 触发运行时默认值）
    with open(os.path.join(ROOT, "App/AppConfig.py"), encoding="utf-8") as f:
        appcfg_tree = ast.parse(f.read())

    def _lookback_keys(key):
        for node in appcfg_tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "_FIELD_DEFAULTS" and isinstance(node.value, ast.Dict):
                        for k, val in zip(node.value.keys, node.value.values):
                            if isinstance(k, ast.Constant) and k.value == key and isinstance(val, ast.Dict):
                                return {kk.value for kk in val.keys if isinstance(kk, ast.Constant)}
        raise AssertionError(f"AppConfig 找不到 {key}")

    stk_keys = _lookback_keys("STOCKS_LOOKBACK_CONFIG")
    fut_keys = _lookback_keys("FUTURES_LOOKBACK_CONFIG")
    assert stk_keys <= STOCKS_FREQS, f"STOCKS_LOOKBACK_CONFIG keys {stk_keys} 超出 STOCKS_FREQS"
    assert fut_keys <= FUTURES_FREQS, f"FUTURES_LOOKBACK_CONFIG keys {fut_keys} 超出 FUTURES_FREQS"
    # 股票主周期时长 keys ⊆ STOCKS_FREQS
    appeng = extract_from_file("App/AppEngine.py", ["_STOCKS_MAIN_PERIOD"],
                               extras={"FREQ_SEC_MAP": FREQ_SEC_MAP})
    assert set(appeng["_STOCKS_MAIN_PERIOD"]) <= STOCKS_FREQS, "主周期时长含非股票周期"
    # 前端镜像守卫：levels 全周期表 == 股票 ∪ 期货（跨语言镜像，无法 import，故用守卫测试）
    with open(os.path.join(ROOT, "Frontend/app.js"), encoding="utf-8") as f:
        js = f.read()
    m = re.search(r"const levels = \{(.*?)\};", js, re.S)
    assert m, "前端 levels 未找到"
    js_levels = set(re.findall(r"'(w|d|30m|15m|5m|1m|15s)'", m.group(1)))
    assert js_levels == set(STOCKS_FREQS) | set(FUTURES_FREQS), \
        f"前端 levels 与股票/期货并集失配: {js_levels}"
    print("[PASS] 市场周期支持集单一事实源 STOCKS_FREQS/FUTURES_FREQS，回看/主周期/前端镜像一致")


def main():
    td = extract_from_file("DataAPI/TdxAPI.py", ["_resample_5m"])
    test_15m_synth(td["_resample_5m"])
    test_30m_synth(td["_resample_5m"])
    test_30m_single_source()
    test_30m_p2_2_optim_branch()

    # INTRADAY_FREQS / SUBSECOND_FREQS 现由 Common.CEnum.FREQ_TABLE 派生、
    # func_util 再导出（不再内联定义），AST 无法提取，改直接 import。
    from Common.func_util import _get_date_fmt
    test_get_date_fmt_15m({"_get_date_fmt": _get_date_fmt})
    test_freq_single_source()
    test_market_freq_partition()

    test_dual_consistency()
    print("ALL 15m/30m TESTS PASS")


if __name__ == "__main__":
    main()
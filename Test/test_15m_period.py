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
from collections import OrderedDict, Counter
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def test_get_date_fmt_15m(bs_funcs):
    fmt = bs_funcs["_get_date_fmt"]("15m")
    assert fmt == "%Y/%m/%d %H:%M", f"_get_date_fmt('15m')={fmt!r}，应为带时分（否则红框时分被截断）"
    assert bs_funcs["_get_date_fmt"]("d") == "%Y/%m/%d"
    print("[PASS] _get_date_fmt('15m')=%Y/%m/%d %H:%M（红框时分不截断）")


def test_intraday_freqs_dual_copy(bs_funcs, utils_funcs):
    lhs = bs_funcs["INTRADAY_FREQS"]
    rhs = utils_funcs["INTRADAY_FREQS"]
    assert lhs == rhs, f"INTRADAY_FREQS 双副本不一致: BSPointList={lhs}, utils={rhs}"
    assert "15m" in lhs, "INTRADAY_FREQS 缺 15m"
    print(f"[PASS] INTRADAY_FREQS 双副本一致且含15m: {sorted(lhs)}")


def test_30m_single_source():
    with open(os.path.join(ROOT, "DataAPI/TdxAPI.py"), encoding="utf-8") as f:
        src = f.read()
    assert "def _bucket_30min" not in src, "30m 手写分桶副本仍在（应统一为锚点法单一实现）"
    # 评估者合并重构：15m/30m 统一为单一实现 _resample_5m(period_min)
    assert "def _resample_5m(" in src, "缺少统一 _resample_5m(period_min) 合成实现"
    # 两个外层包装均委托统一实现（保持对外函数名/签名不变）
    assert "return _resample_5m(records, 30)" in src, "30m 未委托统一 _resample_5m"
    assert "return _resample_5m(records, 15)" in src, "15m 未委托统一 _resample_5m"
    # read_tdx_min_file 必须委托 _resample_5m_to_30m
    m = re.search(r"def read_tdx_min_file.*?(?=def |\Z)", src, re.S)
    assert m and "return _resample_5m_to_30m(records, market=market)" in m.group(0), \
        "read_tdx_min_file 未委托 _resample_5m_to_30m（仍在原地 30m 合成）"
    print("[PASS] 15m/30m 统一 _resample_5m 合成 + 双包装委托 + read_tdx_min_file 委托")


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


def main():
    td = extract_from_file("DataAPI/TdxAPI.py", ["_resample_5m"])
    test_15m_synth(td["_resample_5m"])
    test_30m_synth(td["_resample_5m"])
    test_30m_single_source()
    test_30m_p2_2_optim_branch()

    bs = extract_from_file("BuySellPoint/BSPointList.py",
                           ["INTRADAY_FREQS", "SUBSECOND_FREQS", "_get_date_fmt"])
    utils = extract_from_file("App/utils.py", ["INTRADAY_FREQS"])
    test_get_date_fmt_15m(bs)
    test_intraday_freqs_dual_copy(bs, utils)

    test_dual_consistency()
    print("ALL 15m/30m TESTS PASS")


if __name__ == "__main__":
    main()
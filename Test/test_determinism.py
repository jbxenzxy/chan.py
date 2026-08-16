# -*- coding: utf-8 -*-
"""
阶段 2.5：确定性与隔离性测试
=====================================================================
借鉴外部 pytest 实现三处设计（并按更强口径实现）：
  ① 同输入重复调用一致性  外部版只比 meta 计数；本版全量规范化比对
                          （七维输出逐字段一致，非仅计数）
  ② 跨路径污染检查        外部版没有；本版为股票→期货→股票序列，
                          第三次结果必须与第一次完全一致——守护阶段 3
                          拆股票/期货加载层时的共享状态隔离
                          （CTqSdkAPI 类缓存 / CMyBSPointList.REPLAY_MODE
                           等跨路径共享状态的回归在此暴露）
  ③ 双窗口语义断言        外部版 sub.freq==60m + 主级末日期存在于子级；
                          本版 fixtures 已对齐同一时间窗（2000 根 60m ↔
                          500 日），可断言 主级交易日 ⊆ 子级交易日

运行：python Test/test_determinism.py [--update 无操作，仅为入口统一]
"""
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    import typing_extensions
    typing.Self = typing_extensions.Self

from Test.snapshot_runner import CASES, isolate_side_effects, normalize
from Test.comparator import compare
from Test.contracts import validate_result_structure, format_diffs


def _run_case(name):
    """按快照采集口径执行一个用例（含副作用隔离），返回规范化结果"""
    restore = isolate_side_effects()
    try:
        raw = CASES[name]()
    finally:
        restore()
    return normalize(raw)


# ═══════════════════════════════════════════════════════════════════
# ① 同输入重复调用一致性（全量逐字段，非仅计数）
# ═══════════════════════════════════════════════════════════════════
def test_repeat_consistency(failures):
    r1 = _run_case("stock_d_full")
    r2 = _run_case("stock_d_full")
    ok, diff = compare(r1, r2, path="$.repeat")
    if not ok:
        failures.append(f"重复调用不一致:\n{diff}")
        print("[FAIL] ① 重复调用一致性: 同输入两次结果存在字段级差异")
    else:
        m = r1["meta"]
        print(f"[PASS] ① 重复调用一致性: 七维输出全量一致"
              f"（{m['kline_count']}K线/{m['bi_count']}笔/{m['zs_count']}中枢）")


# ═══════════════════════════════════════════════════════════════════
# ② 跨路径污染检查（股票 → 期货 → 股票）
# ═══════════════════════════════════════════════════════════════════
def test_cross_path_pollution(failures):
    stock_1 = _run_case("stock_d_full")
    futures = _run_case("futures_15s_full")     # 中间插入期货路径
    stock_2 = _run_case("stock_d_full")          # 回到股票路径

    # 期货路径自身有效（前置条件，避免误判污染）
    if "error" in futures:
        failures.append(f"跨路径检查前置失败: 期货用例返回 error: {futures['error']}")
        print("[FAIL] ② 跨路径污染: 前置期货用例失败")
        return

    ok, diff = compare(stock_1, stock_2, path="$.after_futures")
    if not ok:
        failures.append(f"跨路径污染: 期货路径后股票结果漂移:\n{diff}")
        print("[FAIL] ② 跨路径污染: 股票→期货→股票，第三次结果 ≠ 第一次")
    else:
        print("[PASS] ② 跨路径污染检查: 股票→期货→股票，前后股票结果完全一致"
              "（CTqSdkAPI 缓存/REPLAY_MODE 等共享状态无泄漏）")


# ═══════════════════════════════════════════════════════════════════
# ③ 双窗口语义断言（子级口径 / 数量关系 / 时间对齐）
# ═══════════════════════════════════════════════════════════════════
def test_dual_semantics(failures):
    r = _run_case("multilevel_d_30m")
    sub = r.get("sub")

    # 3a 契约：主级完整契约 + 子级精简契约（white_hline/is_replay/forward_adjust 不在子级）
    ok_c, diffs_c = validate_result_structure(r, expect_sub=True)
    if not ok_c:
        failures.append(f"双窗口契约失败:\n{format_diffs(diffs_c)}")
        print("[FAIL] ③ 双窗口语义: 契约校验失败")
        return

    main_freq, sub_freq = r["meta"]["freq"], sub["meta"]["freq"]
    n_main, n_sub = len(r["klines"]), len(sub["klines"])

    # 3b 子级周期必须是不同于主级别的更细粒度
    if main_freq == sub_freq:
        failures.append(f"双窗口语义: 子级周期 {sub_freq} 与主级 {main_freq} 相同")
        print(f"[FAIL] ③ 双窗口语义: 子级周期 == 主级（{main_freq}）")
        return

    # 3c 子级 K 线数量 ≥ 主级（更细粒度覆盖同区间必然更多）
    if n_sub < n_main:
        failures.append(f"双窗口语义: 子级K线 {n_sub} < 主级 {n_main}")
        print(f"[FAIL] ③ 双窗口语义: 子级 K 线数 {n_sub} < 主级 {n_main}")
        return

    # 3d 时间对齐：主级每个交易日都必须出现在子级（fixtures 已对齐同一时间窗）
    main_days = {k["date"][:10] for k in r["klines"]}
    sub_days = {k["date"][:10] for k in sub["klines"]}
    missing_days = sorted(main_days - sub_days)
    if missing_days:
        failures.append(f"双窗口语义: {len(missing_days)} 个主级交易日不在子级: {missing_days[:5]}…")
        print(f"[FAIL] ③ 双窗口语义: 主级有 {len(missing_days)} 个交易日缺失于子级")
        return

    print(f"[PASS] ③ 双窗口语义: 契约/子级口径({main_freq}+{sub_freq})/"
          f"数量({n_main}≤{n_sub})/时间对齐({len(main_days)} 个交易日全覆盖)")


# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════
def main():
    failures = []
    test_repeat_consistency(failures)
    test_cross_path_pollution(failures)
    test_dual_semantics(failures)

    print()
    if failures:
        print(f"===== 确定性测试: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x.splitlines()[0])
        return False
    print("===== 确定性测试: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

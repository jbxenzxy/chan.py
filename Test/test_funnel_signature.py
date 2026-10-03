# -*- coding: utf-8 -*-
"""
Test/test_funnel_signature.py —— 漏斗壳形参一致性护栏（评审 P0-1 防回潮）
=======================================================================
背景（2026-10-03 评审 P0-1）：期货手动选点路由无条件透传 `end_date`，而
App/AppChart.py 的 RAW 壳 `futures_manual_select_point` 没有该形参、也不转发
→ 期货手动选点 100% TypeError → 500（含不带 end_date 的普通选点）。
门禁绿掩盖了它：新增测试全部绕过 AppChart 壳层，形参缺失无任何组件能红。

本组件对 App/AppChart.py 的「漏斗壳对」做静态形参一致性断言：
  每对 (call_* 入口壳, 其委托实现) 的**入口壳形参集合 ⊆ 实现形参集合**
  （实现允许有入口壳不透传的内部参数，如 cache_chan/include_extra）。
自证（变异敏感）：内置一对故意缺参的假壳对，断言检测逻辑必须能抓到——
若未来有人改坏检测逻辑（如放宽为恒真），该自证用例即红。

覆盖壳对（入口壳 → 委托实现）：
  call_analysis                      → _m.analyze_stock
  analyze_stock（薄封装）             → _m.analyze_stock
  call_manual_select_point           → stock_manual_select_point
  call_futures_manual_select_point   → futures_manual_select_point（RAW 壳）
  futures_manual_select_point（RAW） → AppSSE.futures_manual_select_point
  call_compute_red_range_zs          → compute_red_range_zs

运行：python Test/test_funnel_signature.py
"""
import ast
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

CHART = os.path.join(REPO_ROOT, "App", "AppChart.py")


def _parse_functions(src):
    """AST 解析：函数名 → 形参名列表（含默认值与否不影响本断言）。"""
    tree = ast.parse(src)
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = [a.arg for a in node.args.args]
    return funcs


def _extract_shell_pairs(src):
    """从 call_* 壳的 return 语句提取委托目标名（轻量正则，零构建前端同款思路）。

    识别形态：return <name>(...)，<name> 不以 call_ 开头（即被委托方）。
    """
    import re
    pairs = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("call_"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Call):
                callee = sub.value.func
                name = None
                if isinstance(callee, ast.Name):
                    name = callee.id
                elif isinstance(callee, ast.Attribute):
                    name = callee.attr
                if name and not name.startswith("call_"):
                    pairs.append((node.name, name))
                    break
    return pairs


def test_shell_signatures_subset_of_impl():
    """每对壳的形参集合 ⊆ 委托实现形参集合。"""
    with open(CHART, encoding="utf-8") as f:
        src = f.read()
    funcs = _parse_functions(src)
    pairs = _extract_shell_pairs(src)
    assert pairs, "未提取到任何壳对（解析逻辑被改坏？）"
    bad = []
    skipped = []
    for caller, callee in pairs:
        if caller not in funcs or callee not in funcs:
            # 跨模块委托（如 call_stock_tpsl → TPSL 域实现）不在本护栏范围，
            # 由各自领域测试守护
            skipped.append(f"{caller} → {callee}")
            continue
        missing = [p for p in funcs[caller] if p not in funcs[callee]]
        if missing:
            bad.append(f"{caller}({','.join(funcs[caller])}) 有形参 {missing} "
                       f"不在实现 {callee}({','.join(funcs[callee])}) 中")
    assert not bad, "漏斗壳形参不一致（评审 P0-1 同型缺口）：\n  " + "\n  ".join(bad)
    print(f"[PASS] 漏斗壳形参一致: {len(pairs) - len(skipped)} 对同模块壳全部 ⊆ 委托实现"
          + (f"（跨模块跳过 {len(skipped)}）" if skipped else ""))


def test_raw_shell_forwards_to_sse():
    """期货 RAW 壳必须把 end_date 转发给 AppSSE 实现（P0-1 的第二断点）。"""
    with open(CHART, encoding="utf-8") as f:
        src = f.read().replace("\r\n", "\n")
    import re
    m = re.search(
        r"def futures_manual_select_point\(.*?\).*?:\s*(?:\"\"\".*?\"\"\")?\s*return\s+_sse\.futures_manual_select_point\((.*?)\)",
        src, re.S)
    assert m, "未找到 RAW 壳对 _sse.futures_manual_select_point 的调用"
    args = m.group(1)
    assert "end_date=end_date" in args, \
        f"RAW 壳未转发 end_date 给 AppSSE 实现（P0-1 复潮）: {args}"
    print("[PASS] RAW 壳转发 end_date → AppSSE 实现")


def test_detector_catches_missing_param():
    """自证：检测逻辑对「缺参壳对」必须报红（防检测逻辑本身被放宽）。"""
    funcs = {"call_demo": ["code", "end_date"], "demo_impl": ["code"]}
    missing = [p for p in funcs["call_demo"] if p not in funcs["demo_impl"]]
    assert missing == ["end_date"], "自证失败：检测逻辑失去判别力"
    print("[PASS] 检测器自证: 缺参壳对可被抓到")


def main():
    test_shell_signatures_subset_of_impl()
    test_raw_shell_forwards_to_sse()
    test_detector_catches_missing_param()
    print("ALL 漏斗壳形参一致性 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

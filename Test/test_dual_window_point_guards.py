# -*- coding: utf-8 -*-
"""
Test/test_dual_window_point_guards.py —— 双窗选点/复盘一致性静态护栏
=====================================================================
背景（2026-10-04 评审，基线 2db7a25）：期货双窗相对股票双窗有 3 处
「文档说归一化了、代码没做」，且都在前端/路由层——跑不到 Python 行为测试的
地方，只能用静态断言钉死，防止回潮：

  1. 期货双窗「取消选点」菜单豁免（`_restartEnabled` 上的
     `!(isDualWindow && market==='futures')`）必须删除——重连侧早已就绪，
     豁免导致菜单恒隐藏，是四期最后一块拼图；
  2. 期货双窗**上窗**选点后重连必须带复盘点 end（第 4 参）——下窗与取消选点
     两处都带了，唯独上窗漏，会让上窗从复盘态掉回实时态；
  3. HTTP 路由 `/api/stocks/{code}/analyze` 不再暴露 `step`（箭头步进的死
     API 面：前端箭头已删）。注意**引擎内部** `step` 必须保留——
     `Test/snapshot_runner.py` 的 `_c_stock_d_step_m5` 用 `step=-5` 做逐步
     回放，是门禁组件 `trigger_step_replay` 的输入。

运行：python Test/test_dual_window_point_guards.py
"""
import ast
import os
import re
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)

APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")
FRONTAPI = os.path.join(REPO_ROOT, "FrontAPI.py")
APP_ENGINE = os.path.join(REPO_ROOT, "App", "AppEngine.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_futures_dual_cancel_point_menu_enabled():
    """期货双窗「取消选点」不得再被豁免（评审 #2）。"""
    src = _read(APP_JS)
    m = re.search(r"^\s*_restartEnabled\s*=\s*(.+?);\s*$", src, re.M)
    assert m, "未找到 _restartEnabled 赋值（取消选点菜单开关）"
    expr = m.group(1)
    assert "isDualWindow" not in expr and "futures" not in expr, \
        f"取消选点菜单仍带双窗/期货豁免（期货双窗取消选点不可达）: {expr!r}"
    assert "hasPoint" in expr, \
        f"_restartEnabled 应为「任一窗有选点即亮」，实为 {expr!r}"
    print("[PASS] 期货双窗取消选点未豁免: _restartEnabled = hasPoint")


def test_dual_main_select_reconnect_keeps_replay_end():
    """双窗上窗选点后重连必须带复盘点 end（评审 #4）。"""
    full = _read(APP_JS)
    # 限定到「选点响应处理」块：从 /select/point 请求构造往后取一段窗口。
    # 全文件搜三参会误伤「切周期」「回实时」两处（那两处不带 end 才是正确的）。
    _anchor = full.find("/select/point")
    assert _anchor > 0, "未找到选点请求构造（/select/point）"
    src = full[_anchor:_anchor + 3000]
    # 三参调用 = 漏传 end（下窗 :3260 与取消选点 :3627 均为四参）
    assert not re.search(r"connectRealtimeDual\(\s*code\s*,\s*freq\s*,\s*dualSubFreq\s*\)", src), \
        "双窗上窗选点重连未带 end：复盘态选点会把上窗掉回实时态"
    m = re.search(r"connectRealtimeDual\(\s*code\s*,\s*freq\s*,\s*dualSubFreq\s*,\s*(\w+)\s*\)", src)
    assert m, "未找到双窗上窗选点的 connectRealtimeDual 调用"
    end_var = m.group(1)
    # end 变量必须来自「复盘态末根日期」的判定，不能是常量 null
    assert re.search(r"const\s+" + re.escape(end_var) + r"\s*=\s*\(", full), \
        f"重连 end 变量 {end_var} 不是按复盘态计算的表达式"
    assert re.search(re.escape(end_var) + r"\s*\?\s*\"&end_date=\"", full), \
        f"{end_var} 未同时用于选点请求的 end_date（两处口径应同源）"
    print(f"[PASS] 双窗上窗选点重连带复盘点 end: {end_var}")


def test_analyze_route_drops_dead_step_param():
    """路由层删死参数 step，引擎内部保留（评审 #11）。"""
    tree = ast.parse(_read(FRONTAPI))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "api_stocks_analyze":
            target = node
            break
    assert target is not None, "未找到 api_stocks_analyze 路由"
    arg_names = [a.arg for a in target.args.args] + [a.arg for a in target.args.kwonlyargs]
    assert "step" not in arg_names, \
        f"路由仍暴露死参数 step（前端箭头步进已删，无调用方）: {arg_names}"
    # 死参数不能只是换个名字继续透传
    for node in ast.walk(target):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                assert kw.arg != "step", "路由仍在向下游透传 step"

    # 反向护栏：引擎内部 step 必须保留（快照回放用例依赖它）
    eng = ast.parse(_read(APP_ENGINE))
    keep = False
    for node in ast.walk(eng):
        if isinstance(node, ast.FunctionDef) and node.name == "_analyze_stock_internal":
            keep = "step" in [a.arg for a in node.args.args]
            break
    assert keep, "_analyze_stock_internal 的 step 被误删（trigger_step_replay 依赖）"
    print("[PASS] 路由层 step 已删、引擎内部 step 保留（快照回放不受影响）")


def main():
    test_futures_dual_cancel_point_menu_enabled()
    test_dual_main_select_reconnect_keeps_replay_end()
    test_analyze_route_drops_dead_step_param()
    print("ALL 双窗选点一致性护栏 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

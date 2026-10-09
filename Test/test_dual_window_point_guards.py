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


def test_futures_select_point_carries_view_start():
    """期货选点请求必须携带**当前视图左边界 L**（start_time）——定位窗口同源。

    后端用 bi_idx 在定位窗口的笔列表上取笔（`bi_list[bi_idx]` → 左肩第一根
    K线 = 新选点 T），而 bi_idx 属于**前端当前视图**的笔列表 ⇒ 定位窗口必须
    就是前端那个窗口（同一个 L）。L 是**冻结**值（方案 §2.1「改 R 不改 L」）：
    冷启动 = 方式C 的实时窗口 L、复盘原样带过来、选点后 = 新选点 T——只有前端
    知道；后端自己推导只在两种情形恰好对上（CSV 有值 / 非复盘的方式C）：

      · 非复盘（IM 1m，CSV=2026/09/29 13:37，2026-10-09 用户实测）：退回
        FUTURES_LOOKBACK_CONFIG 的 1200 根 ⇒ 86 笔 vs 视图 40 笔，双击
        10/08 09:43（bi_idx=23）左肩错到 09/28 13:27。
      · 复盘 + 该周期无选点（IM 1m 复盘到 10/08，真天勤实测）：视图
        [09/24 10:50, 10/08 14:55] 80 笔 vs 推导窗口 [09/23 14:56, 10/08 14:55]
        86 笔，双击 10/08 14:28（bi_idx=79）左肩错到 10/08 11:24（应得 14:27）。

    断言三段：① 前端两处（上窗/下窗）都按「当前视图首根」算出 L；
    ② L 真的拼进了期货选点 URL；③ 路由收下并透传 start_time（否则查询串
    被 FastAPI 静默忽略，前端白带）。
    """
    full = _read(APP_JS)

    # ① + ② 上窗（freq/code/clickedBiIdx）与下窗（_subFreq/_subCode/subBiIdx）
    for var, freq_var, code_var, idx_var, end_var in (
            ("_viewStart", "freq", "code", "clickedBiIdx", "replayEndQuery"),
            ("_subViewStart", "_subFreq", "_subCode", "subBiIdx", "_subReplayEnd")):
        assert re.search(
            r"const\s+" + var + r"\s*=\s*\(.*?inputDateToApi\(klineDateToInput\(chartData\.klines\[0\]\.date,\s*"
            + freq_var + r"\)", full, re.S), (
            f"{var} 未按「当前视图首根 chartData.klines[0]」计算：视图 L 必须是"
            "前端真实视图的左边界，不能用后端推导值代替")
        assert re.search(
            r'"/api/futures/"\s*\+\s*encodeURIComponent\(' + code_var
            + r'\)\s*\+\s*"/select/point\?freq="\s*\+\s*' + freq_var
            + r'\s*\+\s*"&bi_idx="\s*\+\s*' + idx_var + r'\s*\+\s*'
            + end_var + r'\s*\+\s*' + var, full), (
            f"{var} 未拼进期货选点 URL：定位窗口与前端视图不同源 ⇒ bi_idx 整体错位")
    _lits = re.findall(
        r'"&start_time="\s*\+\s*encodeURIComponent\(inputDateToApi\('
        r'klineDateToInput\(chartData\.klines\[0\]\.date', full)
    assert len(_lits) == 2, \
        ("期货选点 URL 的视图 L 必须以 start_time 携带、取值 = 当前视图首根"
         "（上窗/下窗各一处），实为 %d 处" % len(_lits))

    # ③ 路由收参 + 透传
    tree = ast.parse(_read(FRONTAPI))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "api_futures_select_point":
            target = node
            break
    assert target is not None, "未找到 api_futures_select_point 路由"
    arg_names = [a.arg for a in target.args.args] + [a.arg for a in target.args.kwonlyargs]
    assert "start_time" in arg_names, \
        f"选点路由未收 start_time（查询串会被 FastAPI 静默忽略，前端白带）: {arg_names}"
    fwd = [kw.arg for node in ast.walk(target) if isinstance(node, ast.Call)
           for kw in node.keywords]
    assert "start_time" in fwd, f"选点路由未把 start_time 透传给漏斗层: {fwd}"
    print("[PASS] 期货选点携带视图 L：上窗/下窗 ⇒ URL ⇒ 路由透传 三段齐")


def main():
    test_futures_dual_cancel_point_menu_enabled()
    test_dual_main_select_reconnect_keeps_replay_end()
    test_analyze_route_drops_dead_step_param()
    test_futures_select_point_carries_view_start()
    print("ALL 双窗选点一致性护栏 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

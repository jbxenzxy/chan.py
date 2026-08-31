# -*- coding: utf-8 -*-
"""
阶段 4.1·补充：扫描候选 page_index 板块代码归一化守护
=====================================================================
背景：审计 X3 引入新路径 `/api/stocks/scan/read/candidates`，它把前端
`page_index_code` 直接透传给 `AppScan.Scanner.stock_list`。此前归一化
（sh000852→000852）只写在 `set_page_index_code`（旧入口），新路径漏了
这一步，导致 `sh000852` 命中不了 `get_index_stocks` 的 `CSI_INDICES`
精确匹配而落入中证 csindex 兜底，拼出不存在的 `sh000852cons.xls`，
抛出 "Excel file format cannot be determined"，成分为空、扫描池为 0。

先后两次尝试都被审为"兜底掩盖"：在 AkshareAPI / TdxAPI 层做 defensive
normalize 只是把错误往下藏。根因是**调用契约层缺归一化** —— 归一化没有
形成单一事实源，新入口没有复用既有 `utils._get_stock_market_code`。

本守卫锁定"page_index 来源的板块代码在进入成分取数层前必须归一化"这一
契约，防止未来再删/挪归一化、或新增入口漏复用工具函数导致同型回归：

  ① spy 守卫：显式 `page_index_code`（带前缀）经 stock_list 传给成分
     读取器时，收到的是**归一化裸码**；已裸码输入不被误伤。
  ② 会话路径：scanner.start(page_index_code=带前缀) 建设会话后，
     stock_list(source=page_index) 从会话取码，同样归一化后才下发。
  ③ 静态回潮：AppScan.Scanner.stock_list 内 `_SOURCE_READERS` 之前必须
     存在 `_get_stock_market_code` 归一化调用（防删/防挪到读取器之后）。

运行：python Test/test_scan_pageindex_normalize.py   # 校验（run_all 组件）
      python Test/test_scan_pageindex_normalize.py --update  # 兼容 run_all
"""
import argparse
import ast
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

APP_SCAN_PY = os.path.join("App", "AppScan.py")


def _spy_page_index_scan(scanner, scan_mod, page_index_code, scan_token=None):
    """替换 page_index 读取器为 spy，返回它实际收到的 sector_code 列表。"""
    captured = []
    scan_mod._debug_read_page_index_stocks = lambda code: (captured.append(code), [])[1]
    scanner.stock_list("page_index", page_index_code=page_index_code,
                       scan_token=scan_token)
    return captured


def test_explicit_param_normalized(failures):
    """① 显式 page_index_code：带前缀归一为裸码；裸码原样透传。"""
    import App.AppScan as _scan_mod
    from App.AppScan import scanner
    orig_read = _scan_mod._debug_read_page_index_stocks
    orig_fetch = _scan_mod.fetch_float_mc_from_tencent
    try:
        _scan_mod.fetch_float_mc_from_tencent = lambda stock_list: {}
        cases = [
            ("sh000852", "000852"),   # 本次 bug 靶点：中证1000
            ("sh000300", "000300"),
            ("sz399001", "399001"),   # 深交所分支
            ("sh880491", "880491"),   # 概念/风格板块
            ("ds932000", "932000"),   # 其他指数兜底
            ("000852",   "000852"),   # 裸码不误伤
            ("399001",   "399001"),
            ("881001",   "881001"),
        ]
        for inp, expected in cases:
            got = _spy_page_index_scan(scanner, _scan_mod, inp)
            if not got or got[0] != expected:
                failures.append(
                    f"显式 page_index_code={inp!r} 归一化失败: 成分层收到 {got!r}，期望 {expected!r}")
                print(f"[FAIL] ① 显式归一化: {inp!r} -> {got!r}")
                return
        print(f"[PASS] ① 显式归一化: {len(cases)} 组全部归一为裸码（sh000852→000852 等）")
    finally:
        _scan_mod._debug_read_page_index_stocks = orig_read
        _scan_mod.fetch_float_mc_from_tencent = orig_fetch


def test_session_path_normalized(failures):
    """② 会话路径：start 建会话传带前缀，stock_list 从会话取码并归一化。"""
    import App.AppScan as _scan_mod
    from App.AppScan import scanner
    orig_read = _scan_mod._debug_read_page_index_stocks
    orig_fetch = _scan_mod.fetch_float_mc_from_tencent
    token = None
    try:
        _scan_mod.fetch_float_mc_from_tencent = lambda stock_list: {}
        r = scanner.start(page_index_code="sh000852")
        token = r.get("scan_token")
        got = _spy_page_index_scan(scanner, _scan_mod, None, scan_token=token)
        if not got or got[0] != "000852":
            failures.append(
                f"会话路径 page_index_code 归一化失败: 成分层收到 {got!r}，期望 ['000852']")
            print(f"[FAIL] ② 会话归一化: 收到 {got!r}")
            return
        print(f"[PASS] ② 会话归一化: start(sh000852) -> 成分层收到 ['000852']")
    finally:
        _scan_mod._debug_read_page_index_stocks = orig_read
        _scan_mod.fetch_float_mc_from_tencent = orig_fetch
        if token:
            try:
                scanner.end(token)
            except Exception:
                pass


def test_normalize_before_reader_static(failures):
    """③ 静态回潮：stock_list 内归一化必须早于 page_index 读取器定义。"""
    try:
        with open(APP_SCAN_PY, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        method_src = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef) and node.name == "stock_list"
                    and isinstance(getattr(node, '_cls', None), ast.ClassDef) is False):
                # 只取 Scanner.stock_list：向上找父类
                pass
        # 直接用文本片段定位 StockList 方法体（唯一 def stock_list）
        idx_def = src.index("def stock_list")
        # 找该方法定义结束（下一个顶层 def 前，粗略取 ret 足够）
        body = src[idx_def: idx_def + 2500]
        i_norm = body.find("_get_stock_market_code")
        i_readers = body.find("_SOURCE_READERS")
        if i_norm == -1 or i_readers == -1:
            failures.append("Scanner.stock_list 未找到归一化/读取器定义，契约结构被破坏")
            print("[FAIL] ③ 静态回潮: 结构缺失")
            return
        if i_norm > i_readers:
            failures.append(
                "Scanner.stock_list 中 _get_stock_market_code 归一化位于 "
                "_SOURCE_READERS 之后——归一化必须早于成分读取器（防删除/挪位回归）")
            print("[FAIL] ③ 静态回潮: 归一化位置错误")
            return
        print("[PASS] ③ 静态回潮: stock_list 内归一化早于 page_index 读取器定义")
    except Exception as e:
        failures.append(f"静态回潮解析异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


def main():
    ap = argparse.ArgumentParser(
        description="阶段 4.1·补充：scan page_index 板块代码归一化守护")
    ap.add_argument("--update", action="store_true",
                    help="兼容 run_all --update（本守护无冻结基线，等价校验）")
    args = ap.parse_args()

    failures = []
    print("=" * 64)
    print("扫描候选 page_index 归一化守护")
    print("=" * 64)
    test_explicit_param_normalized(failures)
    test_session_path_normalized(failures)
    test_normalize_before_reader_static(failures)
    print("-" * 64)
    if failures:
        print(f"===== 扫描候选归一化守护: 失败 {len(failures)} 项 =====")
        for f in failures:
            print(" -", f)
        return False
    print("===== 扫描候选归一化守护: 全部通过（3 类守护） =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
# -*- coding: utf-8 -*-
"""
阶段 4.1：.blk 解析与自选股扫描 —— 行为回归守护用例
=====================================================================
背景：P2-3 曾将 .blk 解析收敛到顶层中立模块 tdx_blk.py（该模块未随包
交付），导致运行时 ModuleNotFoundError，且仅手工测试才暴露。本守护
用例锁定 .blk 解析与自选股扫描的行为契约，防止此类回归再次漏网：

  ① .blk 解析黄金行为（与 DoubleOptimize 优化前实现逐字节一致）：
     A股 7 位纯数字（任意数字前缀）/ 港股个股 31#（len==8 + isdigit
     + zfill(5)）/ 港股指数 27# / 美股个股 74# / 美股指数 12#A_ /
     空行跳过 / 非法行跳过 / 文件不存在或空路径返回 []
  ② 双解析器一致性：AppData._read_zxg_blk_file 与 TdxAPI.read_blk_file
     对同一输入输出完全一致（各自自含，边界正确，无顶层中立模块）
  ③ 缺失文件守卫：空路径 / 不存在文件 → []
  ④ 自选股读取链路：app_data.read_zxg_stocks() 经 zxg_blk_path 读取
     真实 zxg.blk（mock tdx_install_dir 指向临时目录）
  ⑤ 扫描消费兼容：AppScan.scanner.stock_list(source="zxg") 消费
     read_zxg_stocks 返回的 {code,prefix} 格式（mock 腾讯接口，
     验证合并/去重/来源标注）
  ⑥ 防回归守卫：源码不得再出现 tdx_blk 引用；两解析器函数体自含
     （AST 校验无外部委托）
  ⑦ 成分股扫描：stock_list(source="page_index") 消费 get_index_stocks
     输出，经预过滤 + _source 标注
  ⑧ 板块指数2/3 扫描：stock_list(source="tdxhy2"/"tdxhy3") 走真实行业
     映射数据（125/315 个），且不触发流通市值请求
  ⑨ 多来源合并去重：zxg + page_index 同码去重、非 zxg 来源覆盖 zxg 语义

运行：python Test/test_blk_parsing.py            # 校验（run_all 组件）
      python Test/test_blk_parsing.py --update   # 兼容 run_all --update
"""
import argparse
import ast
import io
import os
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

# 样例 .blk 内容（覆盖全部文档化格式 + 边界非法行）
SAMPLE_LINES = [
    "1600519",      # A股 沪 600519
    "0000001",      # A股 深 000001
    "2830067",      # A股 北 830067
    "3888888",      # A股 任意数字前缀（原版接受任意 7 位数字）
    "31#00700",     # 港股个股 00700
    "27#HZ5489",    # 港股指数 HZ5489
    "74#AAPL",      # 美股个股 AAPL
    "12#A_NBI",     # 美股指数 A_NBI
    "",             # 空行
    "   ",          # 空白行
    "31#ABC",       # 非法：非数字 → 跳过
    "31#",          # 非法：len==3 → 跳过
    "27#",          # 非法：len==3 → 跳过
    "74#",          # 非法：len==3 → 跳过
    "12#",          # 非法：len==3 → 跳过
    "12345678",     # 非法：8 位数字 → 跳过
    "600519",       # 非法：6 位数字 → 跳过
]

EXPECTED = [
    {"prefix": "1", "code": "600519"},
    {"prefix": "0", "code": "000001"},
    {"prefix": "2", "code": "830067"},
    {"prefix": "3", "code": "888888"},
    {"prefix": "hk", "code": "00700"},
    {"prefix": "hk", "code": "HZ5489"},
    {"prefix": "us", "code": "AAPL"},
    {"prefix": "us", "code": "A_NBI"},
]


def _write_blk(tmpdir, lines, name="sample.blk"):
    path = os.path.join(tmpdir, name)
    with io.open(path, "w", encoding="gbk") as f:
        f.write("\n".join(lines) + "\n")
    return path


def test_golden_parsing(failures):
    """① .blk 解析黄金行为（与 DoubleOptimize 优化前一致）"""
    from App.AppData import _read_zxg_blk_file
    with tempfile.TemporaryDirectory() as td:
        path = _write_blk(td, SAMPLE_LINES)
        got = _read_zxg_blk_file(path)
    if got != EXPECTED:
        failures.append(f"AppData._read_zxg_blk_file 黄金行为不符: {got!r} != {EXPECTED!r}")
        print(f"[FAIL] ① 黄金解析: {got!r}")
    else:
        print(f"[PASS] ① 黄金解析: AppData._read_zxg_blk_file 输出 {len(got)} 条与 DoubleOptimize 一致")


def test_dual_parser_consistency(failures):
    """② 双解析器一致性（各自自含）"""
    from App.AppData import _read_zxg_blk_file
    from DataAPI.TdxAPI import read_blk_file
    with tempfile.TemporaryDirectory() as td:
        path = _write_blk(td, SAMPLE_LINES)
        a = _read_zxg_blk_file(path)
        b = read_blk_file(path)
    if a != b:
        failures.append(f"双解析器输出不一致: AppData={a!r} TdxAPI={b!r}")
        print("[FAIL] ② 双解析器一致性: 不一致")
    else:
        print(f"[PASS] ② 双解析器一致性: AppData 与 TdxAPI 输出一致（{len(a)} 条）")


def test_missing_file(failures):
    """③ 缺失文件守卫：空路径 / 不存在文件 → []"""
    from App.AppData import _read_zxg_blk_file
    from DataAPI.TdxAPI import read_blk_file
    bad = []
    if _read_zxg_blk_file("") != []:
        bad.append("AppData 空路径未返回 []")
    if _read_zxg_blk_file("/nonexistent/x.blk") != []:
        bad.append("AppData 不存在文件未返回 []")
    if read_blk_file("") != []:
        bad.append("TdxAPI 空路径未返回 []")
    if read_blk_file("/nonexistent/x.blk") != []:
        bad.append("TdxAPI 不存在文件未返回 []")
    for b in bad:
        failures.append(b)
        print(f"[FAIL] ③ 缺失文件: {b}")
    if not bad:
        print("[PASS] ③ 缺失文件: 两解析器对空路径/不存在文件均返回 []")


def test_read_zxg_stocks_chain(failures):
    """④ 自选股读取链路：app_data.read_zxg_stocks() 读真实 zxg.blk"""
    from App.AppConfig import app_config
    from App.AppData import app_data
    orig = app_config.tdx_install_dir
    try:
        with tempfile.TemporaryDirectory() as td:
            blk_dir = os.path.join(td, "T0002", "blocknew")
            os.makedirs(blk_dir, exist_ok=True)
            _write_blk(blk_dir, ["1600519", "0000001", "31#00700"], name="zxg.blk")
            app_config.tdx_install_dir = td
            got = app_data.read_zxg_stocks()
        expected = [
            {"prefix": "1", "code": "600519"},
            {"prefix": "0", "code": "000001"},
            {"prefix": "hk", "code": "00700"},
        ]
        if got != expected:
            failures.append(f"read_zxg_stocks 链路不符: {got!r} != {expected!r}")
            print(f"[FAIL] ④ 自选股链路: {got!r}")
        else:
            print(f"[PASS] ④ 自选股链路: read_zxg_stocks 经 zxg_blk_path 读取 {len(got)} 条")
    finally:
        app_config.tdx_install_dir = orig


def test_scan_stock_list_consume(failures):
    """⑤ 扫描消费兼容：stock_list(source="zxg") 消费 {code,prefix} 格式"""
    from App.AppConfig import app_config
    from App.AppScan import scanner
    import App.AppScan as _scan_mod
    orig_dir = app_config.tdx_install_dir
    orig_fetch = _scan_mod.fetch_float_mc_from_tencent
    try:
        with tempfile.TemporaryDirectory() as td:
            blk_dir = os.path.join(td, "T0002", "blocknew")
            os.makedirs(blk_dir, exist_ok=True)
            # 含重复行，验证合并去重
            _write_blk(blk_dir, ["1600519", "0000001", "1600519"], name="zxg.blk")
            app_config.tdx_install_dir = td
            _scan_mod.fetch_float_mc_from_tencent = lambda stock_list: {}
            got = scanner.stock_list("zxg")
        # stock_list 返回汇总 dict：{stocks, sources, total, pre_skipped, errors}
        if not isinstance(got, dict) or "stocks" not in got:
            failures.append(f"stock_list 返回结构异常: {got!r}")
            print(f"[FAIL] ⑤ 扫描消费: 返回结构 {got!r}")
            return
        stocks = got["stocks"]
        if len(stocks) != 2:
            failures.append(f"stock_list 去重后数量不符: {len(stocks)} != 2")
            print(f"[FAIL] ⑤ 扫描消费: 数量 {len(stocks)}")
            return
        for stk in stocks:
            if "code" not in stk or "prefix" not in stk or stk.get("_source") != "zxg":
                failures.append(f"stock_list 条目缺 code/prefix/_source: {stk!r}")
                print(f"[FAIL] ⑤ 扫描消费: 条目格式 {stk!r}")
                return
        print(f"[PASS] ⑤ 扫描消费: stock_list(source=zxg) 去重后 {len(stocks)} 条，格式兼容")
    finally:
        app_config.tdx_install_dir = orig_dir
        _scan_mod.fetch_float_mc_from_tencent = orig_fetch


def test_no_tdx_blk_regression(failures):
    """⑥ 防回归守卫：源码无 tdx_blk 引用；两解析器函数体自含"""
    bad = []
    for rel in ("App/AppData.py", "DataAPI/TdxAPI.py"):
        src = io.open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read()
        if "tdx_blk" in src:
            bad.append(f"{rel} 仍引用 tdx_blk（P2-3 错误收敛回归）")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("_read_zxg_blk_file", "read_blk_file"):
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        bad.append(f"{rel}::{node.name} 含 import（应自含解析，无外部委托）")
    for b in bad:
        failures.append(b)
        print(f"[FAIL] ⑥ 防回归: {b}")
    if not bad:
        print("[PASS] ⑥ 防回归: 无 tdx_blk 引用，两解析器函数体自含")


def test_page_index_scan(failures):
    """⑦ 成分股扫描：stock_list(source=page_index) 消费 get_index_stocks 输出"""
    import App.AppScan as _scan_mod
    from App.AppScan import scanner
    orig_code = _scan_mod._page_index_code
    orig_read = _scan_mod._debug_read_page_index_stocks
    orig_pre = _scan_mod._quick_prefilter_pass
    orig_fetch = _scan_mod.fetch_float_mc_from_tencent
    try:
        _scan_mod._page_index_code = "881001"
        _scan_mod._debug_read_page_index_stocks = lambda code: [
            {"code": "600519", "prefix": "1", "name": "贵州茅台"},
            {"code": "000001", "prefix": "0", "name": "平安银行"},
            {"code": "600000", "prefix": "1", "name": "浦发银行"},
        ]
        _scan_mod._quick_prefilter_pass = lambda market, code: (True, None, None)
        _scan_mod.fetch_float_mc_from_tencent = lambda stock_list: {}
        got = scanner.stock_list("page_index")
        stocks = got["stocks"]
        if len(stocks) != 3:
            failures.append(f"page_index 扫描数量不符: {len(stocks)} != 3")
            print(f"[FAIL] ⑦ 成分股: 数量 {len(stocks)}")
            return
        for stk in stocks:
            if "code" not in stk or "prefix" not in stk or stk.get("_source") != "page_index":
                failures.append(f"page_index 条目缺 code/prefix/_source: {stk!r}")
                print(f"[FAIL] ⑦ 成分股: 条目格式 {stk!r}")
                return
        print(f"[PASS] ⑦ 成分股: stock_list(source=page_index) {len(stocks)} 条，经预过滤+来源标注")
    finally:
        _scan_mod._page_index_code = orig_code
        _scan_mod._debug_read_page_index_stocks = orig_read
        _scan_mod._quick_prefilter_pass = orig_pre
        _scan_mod.fetch_float_mc_from_tencent = orig_fetch


def test_tdxhy_scan(failures):
    """⑧ 板块指数2/3 扫描：stock_list(source=tdxhy2/tdxhy3) 走真实行业映射数据"""
    import App.AppScan as _scan_mod
    from App.AppScan import scanner
    orig_fetch = _scan_mod.fetch_float_mc_from_tencent
    try:
        # tdxhy2/tdxhy3 不应触发流通市值请求（_need_float_mc=False）
        _scan_mod.fetch_float_mc_from_tencent = lambda stock_list: (
            (_ for _ in ()).throw(AssertionError("tdxhy 来源不应请求流通市值")))
        got2 = scanner.stock_list("tdxhy2")
        got3 = scanner.stock_list("tdxhy3")
        stocks2, stocks3 = got2["stocks"], got3["stocks"]
        if len(stocks2) != 125:
            failures.append(f"tdxhy2 数量不符: {len(stocks2)} != 125")
            print(f"[FAIL] ⑧ 板块指数2: 数量 {len(stocks2)}")
        if len(stocks3) != 315:
            failures.append(f"tdxhy3 数量不符: {len(stocks3)} != 315")
            print(f"[FAIL] ⑧ 板块指数3: 数量 {len(stocks3)}")
        for stk in stocks2:
            if stk.get("_source") != "tdxhy2" or stk.get("prefix") != "1" or not stk.get("code", "").startswith("881"):
                failures.append(f"tdxhy2 条目格式不符: {stk!r}")
                break
        for stk in stocks3:
            if stk.get("_source") != "tdxhy3" or stk.get("prefix") != "1" or not stk.get("code", "").startswith("881"):
                failures.append(f"tdxhy3 条目格式不符: {stk!r}")
                break
        if not any("⑧" in f for f in failures):
            print(f"[PASS] ⑧ 板块指数2/3: tdxhy2={len(stocks2)} tdxhy3={len(stocks3)}，无流通市值请求")
    finally:
        _scan_mod.fetch_float_mc_from_tencent = orig_fetch


def test_multi_source_merge(failures):
    """⑨ 多来源合并去重：zxg + page_index + tdxhy2 同码去重与来源覆盖"""
    import App.AppScan as _scan_mod
    from App.AppScan import scanner
    from App.AppConfig import app_config
    orig_code = _scan_mod._page_index_code
    orig_read = _scan_mod._debug_read_page_index_stocks
    orig_pre = _scan_mod._quick_prefilter_pass
    orig_fetch = _scan_mod.fetch_float_mc_from_tencent
    try:
        _scan_mod._page_index_code = "881001"
        _scan_mod._debug_read_page_index_stocks = lambda code: [
            {"code": "600519", "prefix": "1", "name": "贵州茅台"},  # 与 zxg 重复
            {"code": "000002", "prefix": "0", "name": "万科A"},
        ]
        _scan_mod._quick_prefilter_pass = lambda market, code: (True, None, None)
        _scan_mod.fetch_float_mc_from_tencent = lambda stock_list: {}
        with tempfile.TemporaryDirectory() as td:
            blk_dir = os.path.join(td, "T0002", "blocknew")
            os.makedirs(blk_dir, exist_ok=True)
            _write_blk(blk_dir, ["1600519", "0000001"], name="zxg.blk")
            app_config.tdx_install_dir = td
            got = scanner.stock_list("zxg,page_index")
        stocks = got["stocks"]
        # zxg(600519,000001) + page_index(600519重复,000002) → 去重后 3 条
        if len(stocks) != 3:
            failures.append(f"多来源合并数量不符: {len(stocks)} != 3")
            print(f"[FAIL] ⑨ 多来源合并: 数量 {len(stocks)}")
            return
        by_code = {s["code"]: s for s in stocks}
        # 600519 在 zxg 与 page_index 均出现 → 非 zxg 来源覆盖为 page_index
        if by_code["600519"].get("_source") != "page_index":
            failures.append(f"600519 来源应为 page_index（非 zxg 覆盖 zxg）: {by_code['600519']!r}")
            print(f"[FAIL] ⑨ 多来源合并: 600519 来源 {by_code['600519'].get('_source')}")
        if by_code["000001"].get("_source") != "zxg":
            failures.append(f"000001 来源应为 zxg: {by_code['000001']!r}")
            print(f"[FAIL] ⑨ 多来源合并: 000001 来源 {by_code['000001'].get('_source')}")
        if by_code["000002"].get("_source") != "page_index":
            failures.append(f"000002 来源应为 page_index: {by_code['000002']!r}")
            print(f"[FAIL] ⑨ 多来源合并: 000002 来源 {by_code['000002'].get('_source')}")
        if not any("⑨" in f for f in failures):
            print("[PASS] ⑨ 多来源合并: zxg+page_index 去重后 3 条，来源覆盖语义正确")
    finally:
        _scan_mod._page_index_code = orig_code
        _scan_mod._debug_read_page_index_stocks = orig_read
        _scan_mod._quick_prefilter_pass = orig_pre
        _scan_mod.fetch_float_mc_from_tencent = orig_fetch


def main():
    ap = argparse.ArgumentParser(description="阶段 4.1 .blk 解析与自选股扫描 · 行为回归守护")
    ap.add_argument("--update", action="store_true",
                    help="兼容 run_all --update（本守护无冻结基线，等价校验）")
    args = ap.parse_args()

    failures = []
    print("=" * 64)
    print("阶段 4.1 成果防护：.blk 解析与自选股扫描")
    print("=" * 64)
    test_golden_parsing(failures)
    test_dual_parser_consistency(failures)
    test_missing_file(failures)
    test_read_zxg_stocks_chain(failures)
    test_scan_stock_list_consume(failures)
    test_no_tdx_blk_regression(failures)
    test_page_index_scan(failures)
    test_tdxhy_scan(failures)
    test_multi_source_merge(failures)
    print("-" * 64)
    if failures:
        print(f"===== 阶段 4.1 成果防护: 失败 {len(failures)} 项 =====")
        for f in failures:
            print(" -", f)
        return False
    print("===== 阶段 4.1 成果防护: 全部通过（9 类守护） =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

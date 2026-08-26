# -*- coding: utf-8 -*-
"""
阶段 5：获取侧抽象完善 —— 成果防护守护用例
=====================================================================
守护阶段 5 的七类结构性成果（设计文档 V10 方案 8.8 / 4.1 / 4.4）：

  ① 非标准导入收敛（设计 8.8 表格逐项）：App/AppEngine.py / FrontAPI.py /
     App/AppOrch.py / BuySellPoint/BSPointList.py 不得再从
     DataAPI.TqSdkAPI import FREQ_SEC_MAP / FREQ_LABEL_CN / FUTURES_ALIASES /
     SUPPORTED_FREQS / DISABLED_FREQS / fetch_futures_kline 六个符号
     （AST 级，含函数内局部 import）；元数据一律经 CTqSdkAPI 接口属性访问。
     允许：CTqSdkAPI / TQ_ACCOUNT / TQ_PASSWORD（数据源选择点与连接凭据，
     不在设计收敛表格内）。
  ② 盘后下载职责内聚：设计 8.8 点名 8 函数 + 下载专用工具 4 个全部
     定义于 DataAPI/ElTdxAPI.py；App/AppEngine 不再含下载实现体
     （兼容壳除外）；_download_state/_download_lock 与 ElTdxAPI 模块
     字段同一对象（identity，防双态漂移）。
  ③ 抽象层元数据接口：CCommonStockApi 基类声明五个元数据属性 +
     fetch_kline（get_kline 家族，默认 NotImplementedError）；CTqSdkAPI
     提供非空值且与模块级常量同一对象（值单源）。
  ④ 依赖方向（设计 4.4）：DataAPI/* 不得 import App 层 / AppEngine /
     FrontAPI（ElTdxAPI/CommonStockAPI/TdxAPI/TqSdkAPI 逐一 AST 校验）；
     App/AppData.py 不得 import DataAPI（防影子双源，沿用阶段 4 ⑧）。
  ⑤ tdxhy 合并与启动注入：tdxhy_mapping_data.py 已并入 AppData.py
     （App/ 与 DataAPI/ 均无独立文件，AppData 内嵌映射表）；
     import AppEngine 即完成 set_tdx_hy_mapping 注入
     （bootstrap 注入链运行时验证，映射非空）。
  ⑥ fetch 打桩点同步：Test/snapshot_runner.py 打桩 CTqSdkAPI.fetch_kline
     （打桩点随 get_kline 家族迁移，防快照用例回退到已删除的
     m.fetch_futures_kline 符号而真实联网）。
  ⑦ 下载入口配置化：AppOrch 下载族直连 ElTdxAPI + 目录经
     app_config.download_dir；禁 _m.DOWNLOAD_DIR 等单体全局引用
     （该符号已随下载族迁出 AppEngine，悬空引用为运行时
     AttributeError——评审对照实现暴露，本守护防其回潮）。

运行：python Test/test_phase5_guards.py          # 校验（run_all 组件）
      python Test/test_phase5_guards.py --update  # 兼容参数（无冻结基线，等价校验）
"""
import argparse
import ast
import io
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    try:
        import typing_extensions
        typing.Self = typing_extensions.Self
    except ImportError:
        pass


def read_src(rel):
    with io.open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def ast_imports_from(src, module):
    """收集源码中所有 `from <module> import 名单`（AST 级，含函数内 import）"""
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            for a in node.names:
                names.add(a.name)
    return names


# ═══════════════════════════════════════════════════════════════════════
# ① 非标准导入收敛（设计 8.8 表格：六符号禁止从具体数据源模块 import）
# ═══════════════════════════════════════════════════════════════════════
CONVERGED_SYMBOLS = {
    "FREQ_SEC_MAP", "FREQ_LABEL_CN", "FUTURES_ALIASES",
    "SUPPORTED_FREQS", "DISABLED_FREQS", "fetch_futures_kline",
}
# 六符号收敛的真实调用点之一（BSPointList 的期货红框周期换算）
BUSINESS_MODULES = [
    "App/AppEngine.py", "FrontAPI.py", "App/AppOrch.py",
    "BuySellPoint/BSPointList.py",
]


def test_converged_imports(failures):
    bad = []
    for rel in BUSINESS_MODULES:
        imported = ast_imports_from(read_src(rel), "DataAPI.TqSdkAPI")
        hit = imported & CONVERGED_SYMBOLS
        if hit:
            bad.append(f"{rel}: {sorted(hit)}")
    if bad:
        failures.append("① 非标准导入回潮: " + "; ".join(bad))
        print(f"[FAIL] ① 非标准导入收敛: {len(bad)} 个业务模块回潮")
        for b in bad:
            print("      -", b)
    else:
        print(f"[PASS] ① 非标准导入收敛: {len(BUSINESS_MODULES)} 个业务模块"
              f"零直连（6 符号一律经 CTqSdkAPI 接口属性）")


# ═══════════════════════════════════════════════════════════════════════
# ② 盘后下载职责内聚（设计 8.8 点名函数族 → DataAPI/ElTdxAPI.py）
# ═══════════════════════════════════════════════════════════════════════
ELTDX_FUNCS = [
    "_tdx_day_record", "_tdx_min_record",
    "_download_day_kline", "_download_min_kline", "_download_task",
    "_start_download", "_stop_download", "_get_download_status",
    "collect_codes_from_vipdoc",
    "_date_to_int", "_date_to_min_packed", "_ensure_dir",
]
MCM_SHELLS = ["_collect_codes_from_vipdoc"]


def _shell_callee(fn_node):
    """纯委托壳判定

    允许形态：docstring? + import* + 单条 `return _ElTdx.<attr>(...)`。
    返回委托目标 attr 名（非纯壳返回 None）——比「体内出现 _ElTdx.」
    更严：壳内夹带任何多余语句（副作用/条件分支）即判非纯壳。
    """
    body = list(fn_node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    while body and isinstance(body[0], (ast.Import, ast.ImportFrom)):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    ret = body[0].value
    if not isinstance(ret, ast.Call):
        return None
    func = ret.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
            and func.value.id == "_ElTdx":
        return func.attr
    return None


def test_eltdx_cohesion(failures):
    # ②a 12 函数全部定义于 ElTdxAPI.py
    eltdx_tree = ast.parse(read_src("DataAPI/ElTdxAPI.py"))
    eltdx_defs = {n.name for n in ast.walk(eltdx_tree)
                  if isinstance(n, ast.FunctionDef)}
    missing = [f for f in ELTDX_FUNCS if f not in eltdx_defs]
    if missing:
        failures.append(f"② ElTdxAPI 缺函数: {missing}")
        print(f"[FAIL] ②a ElTdxAPI 函数族: 缺 {missing}")
    else:
        print(f"[PASS] ②a ElTdxAPI 函数族: {len(ELTDX_FUNCS)} 个全部在位"
              f"（设计 8.8 点名 8 + 下载专用工具 4）")

    # ②b AppEngine 不再含下载实现体（兼容壳除外，壳必须转发 _ElTdx）
    mcm_src = read_src("App/AppEngine.py")
    mcm_tree = ast.parse(mcm_src)
    mcm_defs = {n.name: n for n in ast.walk(mcm_tree)
                if isinstance(n, ast.FunctionDef)}
    impl_leak = [f for f in ELTDX_FUNCS if f in mcm_defs and f not in MCM_SHELLS]
    if impl_leak:
        failures.append(f"② AppEngine 残留下载实现体: {impl_leak}")
        print(f"[FAIL] ②b AppEngine 实现体残留: {impl_leak}")
    else:
        # 纯壳形态校验（AST）+ 委托目标存在性校验（防薄壳假绿）
        shell_bad, callees = [], []
        for fname in MCM_SHELLS:
            if fname not in mcm_defs:
                shell_bad.append(f"{fname} 兼容壳缺失")
                continue
            callee = _shell_callee(mcm_defs[fname])
            if callee is None:
                shell_bad.append(f"{fname} 非纯委托壳")
            else:
                callees.append((fname, callee))
        for fname, callee in callees:
            if callee not in eltdx_defs:
                shell_bad.append(f"{fname} 委托目标 _ElTdx.{callee} 不存在（薄壳假绿）")
        if shell_bad:
            failures.append(f"② AppEngine 兼容壳异常: {shell_bad}")
            print(f"[FAIL] ②b AppEngine 兼容壳: {shell_bad}")
        else:
            print(f"[PASS] ②b AppEngine: 下载实现体清零，"
                  f"{len(callees)} 个兼容壳为纯委托壳且委托目标真实存在")

    # ②c 下载状态同对象（identity，运行时）
    from App import AppEngine as m
    from DataAPI import ElTdxAPI
    if m._download_state is not ElTdxAPI._download_state \
            or m._download_lock is not ElTdxAPI._download_lock:
        failures.append("② 下载状态别名不同对象（双态漂移风险）")
        print("[FAIL] ②c 状态同对象: _download_state/_download_lock 拷贝了")
    else:
        print("[PASS] ②c 状态同对象: _download_state/_download_lock 与"
              " ElTdxAPI 模块字段同一对象")


# ═══════════════════════════════════════════════════════════════════════
# ③ 抽象层元数据接口（契约声明于基类，值由实现类提供）
# ═══════════════════════════════════════════════════════════════════════
META_ATTRS = ["FREQ_SEC_MAP", "FREQ_LABEL_CN", "FUTURES_ALIASES",
              "SUPPORTED_FREQS", "DISABLED_FREQS"]


def test_metadata_interface(failures):
    from DataAPI.CommonStockAPI import CCommonStockApi
    from DataAPI import TqSdkAPI
    from DataAPI.TqSdkAPI import CTqSdkAPI

    # ③a 基类声明契约
    miss = [a for a in META_ATTRS if a not in CCommonStockApi.__dict__]
    if miss or "fetch_kline" not in CCommonStockApi.__dict__:
        failures.append(f"③ 基类缺元数据契约: {miss or ['fetch_kline']}")
        print(f"[FAIL] ③a 基类契约: 缺 {miss or ['fetch_kline']}")
    else:
        print(f"[PASS] ③a 基类契约: 5 个元数据属性 + fetch_kline"
              f"（get_kline 家族）声明于 CCommonStockApi")

    # ③b 基类 fetch_kline 默认 NotImplementedError
    try:
        CCommonStockApi.fetch_kline(None, "x")
        failures.append("③ 基类 fetch_kline 未抛 NotImplementedError")
        print("[FAIL] ③b 基类默认实现: 未抛 NotImplementedError")
    except NotImplementedError:
        print("[PASS] ③b 基类默认实现: fetch_kline 默认 NotImplementedError")

    # ③c 实现类提供非空值，且与模块级常量同一对象（值单源）
    bad = []
    for a in META_ATTRS:
        cls_v = getattr(CTqSdkAPI, a, None)
        mod_v = getattr(TqSdkAPI, a, None)
        if not cls_v:
            bad.append(f"{a}=空")
        elif cls_v is not mod_v:
            bad.append(f"{a} 与模块级常量不同一对象")
    if bad:
        failures.append(f"③ CTqSdkAPI 元数据值异常: {bad}")
        print(f"[FAIL] ③c 实现类元数据值: {bad}")
    elif not CTqSdkAPI.__dict__.get("fetch_kline"):
        failures.append("③ CTqSdkAPI 未实现 fetch_kline")
        print("[FAIL] ③c 实现类 fetch_kline: 未覆盖基类")
    else:
        print("[PASS] ③c 实现类: CTqSdkAPI 5 属性非空且与模块级常量"
              "同一对象，fetch_kline 已实现")

    # ③d fetch_kline 运行时委托验证：mock 模块级 fetch_futures_kline，
    #    验证 CTqSdkAPI.fetch_kline 真实转发（参数逐位透传），
    #    防止「壳存在但委托链断裂」的静态假绿
    calls = []

    def _fake_fetch(api, symbol, freq_sec=15, num_bars=None,
                    display_key=None, start_time=None):
        calls.append((symbol, freq_sec, num_bars, display_key, start_time))
        return "kline_ok"

    orig_fetch = TqSdkAPI.fetch_futures_kline
    try:
        TqSdkAPI.fetch_futures_kline = _fake_fetch
        result = CTqSdkAPI.fetch_kline(None, "rb", freq_sec=60,
                                       num_bars=100, display_key="RB主连",
                                       start_time="2026-01-01")
        if result != "kline_ok" or calls != [
                ("rb", 60, 100, "RB主连", "2026-01-01")]:
            bad.append(f"fetch_kline 委托链断裂: result={result!r} calls={calls!r}")
    finally:
        TqSdkAPI.fetch_futures_kline = orig_fetch
    if bad:
        failures.append(f"③ fetch_kline 运行时委托: {bad}")
        print(f"[FAIL] ③d fetch_kline 运行时委托: {bad}")
    else:
        print("[PASS] ③d fetch_kline 运行时委托: 5 参数逐位透传至"
              "模块级 fetch_futures_kline（mock 验证）")


# ═══════════════════════════════════════════════════════════════════════
# ④ 依赖方向（设计 4.4：DataAPI 不依赖 App，App 数据层不反向依赖）
# ═══════════════════════════════════════════════════════════════════════
DATA_API_MODULES = ["DataAPI/CommonStockAPI.py", "DataAPI/TdxAPI.py",
                    "DataAPI/TqSdkAPI.py", "DataAPI/ElTdxAPI.py"]
FORBIDDEN_UPPER = ("App", "AppEngine", "FrontAPI", "api_server")


def _imports_any(src):
    """收集源码全部 import 顶层目标（模块级+函数内）"""
    targets = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for a in node.names:
                targets.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            targets.add(node.module.split(".")[0])
            if node.module.split(".")[0] == "App":
                # "from App import X"（单段）与 "from App.Sub import Y"（多段）均合法；
                # 依赖方向只关心顶层包，多段时补记 App.子层 供上层过滤
                if len(node.module.split(".")) >= 2:
                    targets.add("App." + node.module.split(".")[1])
    return targets


def test_dependency_direction(failures):
    bad = []
    for rel in DATA_API_MODULES:
        hits = {t for t in _imports_any(read_src(rel))
                if t.startswith(FORBIDDEN_UPPER)}
        if hits:
            bad.append(f"{rel}: {sorted(hits)}")
    if bad:
        failures.append("④ DataAPI 反向依赖上层: " + "; ".join(bad))
        print(f"[FAIL] ④a DataAPI 反向依赖: {len(bad)} 个模块违规")
        for b in bad:
            print("      -", b)
    else:
        print(f"[PASS] ④a DataAPI 反向依赖: {len(DATA_API_MODULES)} 个模块"
              f"零 import App/AppEngine/FrontAPI")

    # ④b AppData 不 import DataAPI / AppEngine / FrontAPI / AppOrch
    #     （防影子双源，阶段 4 ⑧ 在阶段 5 延续；同层 AppConfig 是允许的
    #      依赖方向终点 FrontAPI → AppOrch → AppData → AppConfig）
    appdata_targets = _imports_any(read_src("App/AppData.py"))
    forbidden_appdata = {"AppEngine", "DataAPI", "FrontAPI",
                         "api_server", "App.AppOrch"}
    hit = {t for t in appdata_targets
           if t in forbidden_appdata or t.split(".")[0] in forbidden_appdata}
    if hit:
        failures.append(f"④ App/AppData.py 反向依赖: {sorted(hit)}")
        print(f"[FAIL] ④b AppData 纯度: 反向依赖 {sorted(hit)}")
    else:
        print("[PASS] ④b AppData 纯度: 不 import DataAPI/AppEngine/"
              "FrontAPI/AppOrch（AppConfig 为允许终点）")


# ═══════════════════════════════════════════════════════════════════════
# ⑤ tdxhy 迁移与启动注入链（运行时：import AppEngine 即完成注入）
# ═══════════════════════════════════════════════════════════════════════
def test_tdxhy_bootstrap(failures):
    in_app = os.path.exists(os.path.join(REPO_ROOT, "App", "tdxhy_mapping_data.py"))
    in_dapi = os.path.exists(os.path.join(REPO_ROOT, "DataAPI", "tdxhy_mapping_data.py"))
    appdata_src = open(os.path.join(REPO_ROOT, "App", "AppData.py"),
                       encoding="utf-8").read()
    merged_ok = ("_TDXHY_X_TO_881" in appdata_src and "_TDXHY_881_TO_X" in appdata_src
                 and "def load_tdxhy_mapping" in appdata_src)
    if in_app or in_dapi or not merged_ok:
        failures.append(f"⑤ tdxhy 文件位置异常: App/独立文件={in_app} "
                        f"DataAPI/残留={in_dapi} AppData内嵌={merged_ok}")
        print(f"[FAIL] ⑤a 文件合并: App/独立文件={in_app}, DataAPI/ 残留={in_dapi}, "
              f"AppData内嵌={merged_ok}")
    else:
        print("[PASS] ⑤a 文件合并: tdxhy_mapping_data.py 已并入 AppData.py，"
              "App/ 与 DataAPI/ 均无残留")

    from App import AppEngine as m  # noqa: F401  （bootstrap 注入在 import 时完成）
    from DataAPI import TdxAPI
    x2, t2x = TdxAPI.get_tdx_hy_mapping()
    if not x2 or not t2x:
        failures.append("⑤ bootstrap 注入未生效（TdxAPI 侧映射为空）")
        print("[FAIL] ⑤b 启动注入: import AppEngine 后映射仍为空")
    elif len(x2) < 400 or len(t2x) < 400:
        failures.append(f"⑤ 注入量异常: {len(x2)}/{len(t2x)}（基线 470）")
        print(f"[FAIL] ⑤b 启动注入: 映射量 {len(x2)}/{len(t2x)} 异常")
    else:
        print(f"[PASS] ⑤b 启动注入: import AppEngine 即完成注入，"
              f"TdxAPI 侧 {len(t2x)} 条（基线 470）")


# ═══════════════════════════════════════════════════════════════════════
# ⑥ fetch 打桩点同步（快照用例防真实联网）
# ═══════════════════════════════════════════════════════════════════════
def test_snapshot_stub(failures):
    src = read_src("Test/snapshot_runner.py")
    if "fetch_kline" not in src or "m.fetch_futures_kline" in src:
        failures.append("⑥ snapshot_runner 打桩点未随 get_kline 家族迁移")
        print("[FAIL] ⑥ 打桩点: snapshot_runner 未打桩 CTqSdkAPI.fetch_kline")
    else:
        print("[PASS] ⑥ 打桩点: snapshot_runner 打桩 CTqSdkAPI.fetch_kline"
              "（阶段 5 抽象层类方法）")


# ═══════════════════════════════════════════════════════════════════════
# ⑦ 下载入口配置化
#    AppOrch 下载族直连 ElTdxAPI + 目录经 app_config.download_dir；
#    禁 _m.DOWNLOAD_DIR 单体全局引用（该符号已随下载族迁出 AppEngine，
#    悬空引用为运行时 AttributeError——本守护防其回潮）
# ═══════════════════════════════════════════════════════════════════════
LEGACY_REFS = ("_m.DOWNLOAD_DIR", "_m._ELTDX_AVAILABLE",
               "_m._start_download", "_m._stop_download",
               "_m._get_download_status")


def test_orch_download_config(failures):
    orch_src = read_src("App/AppOrch.py")
    dl_src = read_src("App/AppDownload.py")
    bad = []
    # 源码级：AppOrch + AppDownload 下载入口零单体全局引用；字符串字面量
    # （docstring 注释性历史说明）经 AST 定位行区间后豁免，仅真实代码引用计违规
    for src, label in ((orch_src, "AppOrch.py"), (dl_src, "AppDownload.py")):
        tree_full = ast.parse(src)
        literal_lines = set()
        for node in ast.walk(tree_full):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and any(t in node.value for t in LEGACY_REFS):
                literal_lines.update(range(node.lineno,
                                           getattr(node, "end_lineno", node.lineno) + 1))
        for i, line in enumerate(src.splitlines(), 1):
            s = line.strip()
            if i in literal_lines or s.startswith("#"):
                continue
            for legacy in LEGACY_REFS:
                if legacy in s:
                    bad.append(f"{label}:{i} 仍引用 {legacy}")
    if bad:
        failures.append(f"⑦ 下载入口配置化: {bad[:4]}")
        print(f"[FAIL] ⑦ 下载入口: {len(bad)} 处单体全局引用残留")
        for b in bad[:4]:
            print("      -", b)
        return

    # AST 级：4 个下载入口 + available/dir 均委托 ElTdxAPI，
    # 启动类入口使用 app_config.download_dir
    # （阶段 8 重组：下载域实现随 AppOrch 拆分迁至 App/AppDownload.py）
    tree = ast.parse(dl_src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("start_download", "start_download_checked",
                 "get_download_status", "stop_download"):
        node = funcs.get(name)
        if node is None:
            bad.append(f"AppDownload.{name} 缺失")
            continue
        seg = ast.get_source_segment(dl_src, node) or ""
        if "ElTdxAPI" not in seg:
            bad.append(f"AppDownload.{name} 未委托 DataAPI/ElTdxAPI")
        if name.startswith("start_download") and "app_config.download_dir" not in seg:
            bad.append(f"AppDownload.{name} 未使用 app_config.download_dir")
    if bad:
        failures.append(f"⑦ 下载入口配置化: {bad}")
        print(f"[FAIL] ⑦ 下载入口委托: {bad}")
    else:
        # 运行时：download_dir() 与 app_config 同源
        import App.AppOrch as orch
        from App.AppConfig import app_config
        if orch.download_dir() != app_config.download_dir:
            failures.append("⑦ orch.download_dir() 与 app_config.download_dir 不同源")
            print("[FAIL] ⑦ 下载目录同源: orch.download_dir() ≠ app_config.download_dir")
        else:
            print("[PASS] ⑦ 下载入口: 4 入口直连 ElTdxAPI + "
                  "app_config.download_dir（零单体全局，运行时同源验证）")


def main():
    ap = argparse.ArgumentParser(description="阶段 5 获取侧抽象完善 · 成果防护")
    ap.add_argument("--update", action="store_true",
                    help="兼容 run_all --update（本守护无冻结基线，等价校验）")
    args = ap.parse_args()

    failures = []
    print("=" * 64)
    print("阶段 5 成果防护：获取侧抽象完善（设计 8.8 / 4.1 / 4.4）")
    print("=" * 64)
    test_converged_imports(failures)
    test_eltdx_cohesion(failures)
    test_metadata_interface(failures)
    test_dependency_direction(failures)
    test_tdxhy_bootstrap(failures)
    test_snapshot_stub(failures)
    test_orch_download_config(failures)
    print("-" * 64)
    if failures:
        print(f"===== 阶段 5 成果防护: 失败 {len(failures)} 项 =====")
        for f in failures:
            print(" -", f)
        return False
    print("===== 阶段 5 成果防护: 全部通过（7 类守护） =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

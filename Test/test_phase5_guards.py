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
  ③ 抽象层元数据接口：CCommonStockApi 基类声明五个元数据属性 +
     fetch_kline（get_kline 家族，默认 NotImplementedError）；CTqSdkAPI
     提供非空值且与模块级常量同一对象（值单源）。
  ④ 依赖方向（设计 4.4）：DataAPI/* 不得 import App 层 / AppEngine /
     FrontAPI（CommonStockAPI/TdxAPI/TqSdkAPI 逐一 AST 校验）；
     App/AppData.py 不得 import DataAPI（防影子双源，沿用阶段 4 ⑧）。
  ⑤ tdxhy 合并与启动注入：tdxhy_mapping_data.py 已并入 AppData.py
     （App/ 与 DataAPI/ 均无独立文件，AppData 内嵌映射表）；
     import AppEngine 即完成 set_tdx_hy_mapping 注入
     （bootstrap 注入链运行时验证，映射非空）。
  ⑥ fetch 打桩点同步：Test/snapshot_runner.py 打桩 CTqSdkAPI.fetch_kline
     （打桩点随 get_kline 家族迁移，防快照用例回退到已删除的
     m.fetch_futures_kline 符号而真实联网）。

注：原「盘后下载」功能已整体移除（AppDownload.py、DataAPI/ElTdxAPI.py
   删除，collect_codes_from_vipdoc 归位 DataAPI/TdxAPI.py），对应守护
   ②（下载职责内聚）与 ⑦（下载入口配置化）随之删除。

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
                    "DataAPI/TqSdkAPI.py"]
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
    test_metadata_interface(failures)
    test_dependency_direction(failures)
    test_tdxhy_bootstrap(failures)
    test_snapshot_stub(failures)
    print("-" * 64)
    if failures:
        print(f"===== 阶段 5 成果防护: 失败 {len(failures)} 项 =====")
        for f in failures:
            print(" -", f)
        return False
    print("===== 阶段 5 成果防护: 全部通过（5 类守护） =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

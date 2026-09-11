# -*- coding: utf-8 -*-
"""
行业映射数据完整性用例 · 阶段 5 后架构（原 2.5，随 8.8 迁移升级）
=====================================================================
设计文档 8.4：「验证 tdxhy_mapping_data.py 在迁移全过程中映射表不发生
静默降级（阶段 5 之前任何目录调整都可能触发两处硬编码加载失效）」。
后续演进：tdxhy_mapping_data.py 已整体并入 App/AppData.py（映射表硬编码
内嵌 + 单一加载函数 AppData.load_tdxhy_mapping()），DataAPI 侧经
set_tdx_hy_mapping 注入（与 set_tdx_config 同一注入模式）。

验证点：
  ① 单源化完成：映射数据**已整体移出仓库**（原独立数据文件
     tdxhy_mapping_data.py 与 AppData.py 内嵌快照均已删除），权威源为该
     程序所在机器的通达信文件 T0002/hq_cache/tdxzs3.cfg；
     AppData.py 仅保留单一加载函数 load_tdxhy_mapping
  ② 单一加载点：AppData.load_tdxhy_mapping() 非空（缺失/空表硬失败，
     不静默降级）；DataAPI/TdxAPI.py 源码不再有按自身目录的 exec 寻址
  ③ 注入链：set_tdx_hy_mapping 注入后 TdxAPI 侧内容一致且对象身份同一
  ④ 双向互逆一致性 + ④b 条目质量（沿用原用例）
  ⑤ 单源契约：权威源路径 / 条数下限未漂移 + 无内嵌快照回潮
     + 活体交叉校验（用例独立重解析权威源 ≡ 运行期映射）。
     注：改造后映射数据不在仓库内，「内容 sha256 冻结」已失去意义
     （冻结一份随时会随通达信行业树变化的哈希＝把移动靶当基线）。

运行：python Test/test_industry_mapping.py [--update]
"""
import hashlib
import json
import os
import re
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    import typing_extensions
    typing.Self = typing_extensions.Self

SNAPSHOT = os.path.join(TEST_DIR, "snapshots", "industry_mapping_integrity.json")


def _digest(mapping):
    """映射内容摘要：键数量 + 规范序列化后的 sha256（与阶段 2.5 基线同构）"""
    canon = json.dumps(
        {"x_to_881": mapping["x_to_881"], "881_to_x": mapping["881_to_x"]},
        ensure_ascii=False, sort_keys=True)
    return {"n_x_to_881": len(mapping["x_to_881"]),
            "n_881_to_x": len(mapping["881_to_x"]),
            "sha256": hashlib.sha256(canon.encode("utf-8")).hexdigest()}


def main():
    failures = []
    force_update = "--update" in sys.argv

    # ① 文件合并完成（tdxhy_mapping_data.py 已并入 AppData.py，双源清零）
    map_file = os.path.join(REPO_ROOT, "App", "tdxhy_mapping_data.py")
    legacy_file = os.path.join(REPO_ROOT, "DataAPI", "tdxhy_mapping_data.py")
    appdata_src = open(os.path.join(REPO_ROOT, "App", "AppData.py"),
                       encoding="utf-8").read()
    # 单源化判定：加载点与权威源定位常量在，且**内嵌快照不得回潮**
    merged_ok = ("def load_tdxhy_mapping" in appdata_src
                 and "_TDXZS3_RELPATH" in appdata_src
                 and "_TDXHY_X_TO_881 = {" not in appdata_src
                 and "_TDXHY_881_TO_X = {" not in appdata_src)
    if os.path.exists(map_file):
        print("[FAIL] ① 数据文件未删除:", map_file)
        failures.append("App/tdxhy_mapping_data.py 未删除（应已并入 AppData.py）")
    elif os.path.exists(legacy_file):
        print(f"[FAIL] ① DataAPI/ 下残留旧文件（双份风险）: {legacy_file}")
        failures.append("DataAPI/tdxhy_mapping_data.py 未删除（双源风险）")
    elif not merged_ok:
        print("[FAIL] ① AppData.py 未内嵌映射表（合并未完成）")
        failures.append("AppData.py 未采用单一权威源（或内嵌快照回潮）")
    else:
        print("[PASS] ① 文件合并完成: tdxhy_mapping_data.py 已并入 AppData.py，"
              "App/ 与 DataAPI/ 均无残留")

    # ② 单一加载点：AppData.load_tdxhy_mapping() 非空（防静默降级）
    from App.AppConfig import app_config  # noqa: F401  （加载次序对齐主程序）
    from App.AppData import app_data
    try:
        x_to_881, to_x = app_data.load_tdxhy_mapping()
        loaded = {"x_to_881": x_to_881, "881_to_x": to_x}
        if not x_to_881 or not to_x:
            print("[FAIL] ② 单一加载: 空表（必须硬失败而非返回空）")
            failures.append("load_tdxhy_mapping 返回空表")
        else:
            print(f"[PASS] ② 单一加载点 AppData.load_tdxhy_mapping 非空: "
                  f"x_to_881={len(x_to_881)} 881_to_x={len(to_x)}")
    except Exception as e:      # noqa: BLE001 —— 权威源缺失/残缺均应被视为硬失败
        print(f"[FAIL] ② 单一加载: 硬失败（权威源不可用）: {e}")
        failures.append(f"load_tdxhy_mapping 硬失败: {e}")
        return _report(failures)

    # ②b 源码级守护：DataAPI/TdxAPI.py 不再含按自身目录的 exec 寻址
    #    （原硬编码之一；若回潮，文件移动与加载解耦的设计即被破坏）
    tdxapi_src = open(os.path.join(REPO_ROOT, "DataAPI", "TdxAPI.py"),
                      encoding="utf-8").read()
    if "tdxhy_mapping_data" in tdxapi_src and "exec(" in tdxapi_src:
        bad_zone = tdxapi_src.split("class CCommonStockApi")[0]
        if "tdxhy_mapping_data" in bad_zone and "exec(" in bad_zone:
            print("[FAIL] ②b TdxAPI 源码回潮 exec 寻址加载")
            failures.append("TdxAPI.py 恢复了按自身目录的 exec 加载")
        else:
            print("[PASS] ②b TdxAPI 源码无 exec 寻址（仅注释提及迁移历史）")
    else:
        print("[PASS] ②b TdxAPI 源码无 exec 寻址加载")

    # ③ 注入链：TdxAPI 侧经 set_tdx_hy_mapping 注入，内容一致 + 身份同一
    from DataAPI import TdxAPI
    TdxAPI.set_tdx_hy_mapping(x_to_881, to_x)
    if TdxAPI._TDXHY_X_TO_881 is not x_to_881 or TdxAPI._TDXHY_881_TO_X is not to_x:
        print("[FAIL] ③ 注入链: 对象身份不同一（注入被拷贝/重建）")
        failures.append("set_tdx_hy_mapping 注入对象身份不同一")
    elif TdxAPI._TDXHY_881_TO_X != to_x:
        print("[FAIL] ③ 注入链: 注入后内容不一致")
        failures.append("注入后 TdxAPI 侧内容不一致")
    else:
        print("[PASS] ③ 注入链: set_tdx_hy_mapping 注入身份同一，"
              "TdxAPI 侧可直接用（与 set_tdx_config 同一注入模式）")

    # ④ 双向互逆一致性
    #    _TDXHY_881_TO_X: {881code: (x_code, name)}
    #    _TDXHY_X_TO_881: {x_code:  (name,  881code)}   ← value 同为二元组，次序不同
    fwd = loaded["881_to_x"]
    rev = loaded["x_to_881"]
    bad = []
    for code_881, pair in fwd.items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            bad.append((code_881, "结构异常"))
            continue
        x_code, name = pair
        if rev.get(x_code) != (name, code_881):
            bad.append((code_881, rev.get(x_code)))
    if bad:
        failures.append(f"双向互逆不一致 {len(bad)} 条（样例 {bad[:2]}）")
        print(f"[FAIL] ④ 双向互逆: {len(bad)} 条不一致，样例 {bad[:2]}")
    else:
        print(f"[PASS] ④ 双向互逆: {len(fwd)} 条全部互逆一致")

    # ④b 条目质量校验：sha256 冻结管「身份不变」，本项管「数据质量合法」
    quality_bad = []
    seen_881 = {}
    for x_code, pair in rev.items():          # _TDXHY_X_TO_881: {x_code: (name, 881code)}
        if not isinstance(x_code, str) or not x_code.strip():
            quality_bad.append((x_code, "键非非空字符串"))
            continue
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            quality_bad.append((x_code, "值非二元组"))
            continue
        name, code_881 = pair
        if not (isinstance(name, str) and name.strip()):
            quality_bad.append((x_code, "名称为空"))
        if not (isinstance(code_881, str) and code_881.isdigit() and len(code_881) == 6):
            quality_bad.append((x_code, f"881代码格式错误: {code_881!r}"))
        if code_881 in seen_881:
            quality_bad.append((code_881, f"881代码重复: {seen_881[code_881]} 与 {x_code}"))
        else:
            seen_881[code_881] = x_code
    if quality_bad:
        failures.append(f"条目质量校验失败 {len(quality_bad)} 条（样例 {quality_bad[:3]}）")
        print(f"[FAIL] ④b 条目质量: {len(quality_bad)} 条非法，样例 {quality_bad[:3]}")
    else:
        print(f"[PASS] ④b 条目质量: {len(rev)} 条全部合法"
              f"（键/名称非空，881代码 6 位纯数字且无重复）")

    # ⑤ 单源契约（替代原「内容 sha256 冻结」）
    #    映射数据已移出仓库 → 冻结内容哈希无意义（它是随通达信行业树变化的
    #    移动靶）。改为钉「契约 + 活体校验」：
    #    ⑤a 权威源路径 / 条数下限未漂移（沿用 JSON 冻结机制，防被人悄悄放宽）
    #    ⑤b 内嵌快照不得回潮（防有人把「读文件」改回「抄一份进来」）
    #    ⑤c 用例**独立重解析**权威源，逐条比对运行期映射（活体交叉校验）
    from App.AppConfig import app_config as _cfg
    from App.AppData import _TDXZS3_MIN_ROWS, _TDXZS3_RELPATH
    contract = {
        "source_relpath": list(_TDXZS3_RELPATH),
        "min_rows": _TDXZS3_MIN_ROWS,
        "forbid_embedded_snapshot": True,
    }
    if force_update or not os.path.exists(SNAPSHOT):
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(contract, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"[FROZEN] ⑤ 单源契约基线: {contract}" if not force_update
              else f"[UPDATED] ⑤ 单源契约基线: {contract}")
    else:
        with open(SNAPSHOT, encoding="utf-8") as f:
            expected = json.load(f)
        if expected == contract:
            print(f"[PASS] ⑤a 单源契约: 权威源={'/'.join(contract['source_relpath'])} "
                  f"条数下限={contract['min_rows']} 未漂移")
        else:
            failures.append(f"单源契约漂移: 期望 {expected} 实际 {contract}")
            print(f"[FAIL] ⑤a 单源契约: 漂移\n  期望: {expected}\n  实际: {contract}")

    if "_TDXHY_X_TO_881 = {" in appdata_src or "_TDXHY_881_TO_X = {" in appdata_src:
        failures.append("内嵌映射快照回潮（应只读权威源文件）")
        print("[FAIL] ⑤b 防回潮: AppData.py 又出现内嵌映射快照")
    else:
        print("[PASS] ⑤b 防回潮: AppData.py 无内嵌映射快照（单一权威源）")

    _path = os.path.join(_cfg.tdx_install_dir, *_TDXZS3_RELPATH) if _cfg.tdx_install_dir else ""
    if not _path or not os.path.exists(_path):
        failures.append(f"权威源文件不存在: {_path}")
        print(f"[FAIL] ⑤c 活体校验: 权威源不存在 {_path}")
    else:
        indep = {}
        with open(_path, "rb") as fh:
            for line in fh.read().decode("gbk", errors="ignore").splitlines():
                q = line.split("|")
                if len(q) < 6:
                    continue
                nm, cd, xc = q[0].strip(), q[1].strip(), q[5].strip()
                if not nm or not re.match(r"^881\d{3}$", cd) or not re.match(r"^X\d+$", xc):
                    continue
                indep.setdefault(xc, (nm, cd))
        if indep != rev:
            only_a = sorted(set(indep) - set(rev))[:5]
            only_b = sorted(set(rev) - set(indep))[:5]
            failures.append(f"运行期映射 ≠ 独立重解析权威源（仅文件有 {only_a} / "
                            f"仅运行期有 {only_b}）")
            print(f"[FAIL] ⑤c 活体校验: 独立重解析 {len(indep)} 条 ≠ 运行期 {len(rev)} 条")
        else:
            print(f"[PASS] ⑤c 活体校验: 独立重解析权威源 {len(indep)} 条 ≡ 运行期映射")
        _dg = _digest(loaded)
        print(f"[INFO] ⑤ 当前权威源摘要（仅记录，不作断言）: "
              f"n={_dg['n_881_to_x']} sha256={_dg['sha256'][:12]}…")

    return _report(failures)


def _report(failures):
    print()
    if failures:
        print(f"===== 行业映射完整性: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== 行业映射完整性: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

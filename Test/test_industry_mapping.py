# -*- coding: utf-8 -*-
"""
阶段 2.5：行业映射数据完整性用例
=====================================================================
设计文档 8.4：「验证 tdxhy_mapping_data.py 在迁移全过程中映射表不发生
静默降级（阶段 5 之前任何目录调整都可能触发两处硬编码加载失效）」。

两处硬编码加载点：
  ① DataAPI/TdxAPI.py:34   按自身目录寻址 → 模块级 _TDXHY_X_TO_881/_TDXHY_881_TO_X
  ② my_chan_main.py:1463   硬编码 DataAPI/ 前缀 → _refresh_stock_names 内部 exec

任一路径失效时只打印警告并降级为空表（功能静默退化），本用例将其
转为硬失败，并冻结映射内容哈希作为完整性基线。

运行：python Test/test_industry_mapping.py
"""
import hashlib
import json
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    import typing_extensions
    typing.Self = typing_extensions.Self

SNAPSHOT = os.path.join(TEST_DIR, "snapshots", "industry_mapping_integrity.json")


def _load_via(path_expr):
    """按指定路径表达式 exec 加载映射（复刻两处硬编码的加载方式）"""
    ns = {}
    exec(open(path_expr, encoding="utf-8").read(), ns)
    return {
        "x_to_881": ns.get("_TDXHY_X_TO_881", {}),
        "881_to_x": ns.get("_TDXHY_881_TO_X", {}),
    }


def _digest(mapping):
    """映射内容摘要：键数量 + 规范序列化后的 sha256"""
    canon = json.dumps(
        {"x_to_881": mapping["x_to_881"], "881_to_x": mapping["881_to_x"]},
        ensure_ascii=False, sort_keys=True)
    return {"n_x_to_881": len(mapping["x_to_881"]),
            "n_881_to_x": len(mapping["881_to_x"]),
            "sha256": hashlib.sha256(canon.encode("utf-8")).hexdigest()}


def main():
    failures = []
    force_update = "--update" in sys.argv

    # ① 文件在位（两处加载的前提）
    map_file = os.path.join(REPO_ROOT, "DataAPI", "tdxhy_mapping_data.py")
    if not os.path.exists(map_file):
        print("[FAIL] ① tdxhy_mapping_data.py 文件缺失:", map_file)
        return False
    print(f"[PASS] ① 数据文件在位: DataAPI/tdxhy_mapping_data.py "
          f"({os.path.getsize(map_file)} bytes)")

    # ② 双路径加载均非空（防静默降级）
    from DataAPI import TdxAPI
    via_tdxapi = {
        "x_to_881": TdxAPI._TDXHY_X_TO_881,
        "881_to_x": TdxAPI._TDXHY_881_TO_X,
    }
    if not via_tdxapi["x_to_881"] or not via_tdxapi["881_to_x"]:
        failures.append("TdxAPI 侧映射为空（加载静默降级）")
        print("[FAIL] ② TdxAPI 侧加载: 空表（静默降级）")
    else:
        print(f"[PASS] ② TdxAPI 侧加载非空: "
              f"x_to_881={len(via_tdxapi['x_to_881'])} "
              f"881_to_x={len(via_tdxapi['881_to_x'])}")

    # my_chan_main 侧寻址逻辑（按其代码硬编码 DataAPI/ 前缀复刻）
    mcm_path = os.path.join(REPO_ROOT, "DataAPI", "tdxhy_mapping_data.py")
    if not os.path.exists(mcm_path):
        failures.append("my_chan_main 侧寻址路径失效（DataAPI/ 前缀）")
        print("[FAIL] ② my_chan_main 侧寻址: 路径失效")
    else:
        via_mcm = _load_via(mcm_path)
        if not via_mcm["881_to_x"]:
            failures.append("my_chan_main 侧映射为空")
            print("[FAIL] ② my_chan_main 侧加载: 空表")
        else:
            print(f"[PASS] ② my_chan_main 侧加载非空: "
                  f"881_to_x={len(via_mcm['881_to_x'])}")

    # ③ 双路径内容一致
    via_mcm = _load_via(mcm_path)
    if via_tdxapi["881_to_x"] != via_mcm["881_to_x"]:
        failures.append("两处加载的 881_to_x 内容不一致")
        print("[FAIL] ③ 双路径内容一致性: 881_to_x 不一致")
    elif via_tdxapi["x_to_881"] != via_mcm["x_to_881"]:
        failures.append("两处加载的 x_to_881 内容不一致")
        print("[FAIL] ③ 双路径内容一致性: x_to_881 不一致")
    else:
        print("[PASS] ③ 双路径内容一致: 两处 exec 加载结果完全相同")

    # ④ 双向互逆一致性
    #    _TDXHY_881_TO_X: {881code: (x_code, name)}
    #    _TDXHY_X_TO_881: {x_code:  (name,  881code)}   ← value 同为二元组，次序不同
    fwd = via_mcm["881_to_x"]
    rev = via_mcm["x_to_881"]
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

    # ④b 条目质量校验（借鉴外部 pytest 版）：sha256 冻结管「身份不变」，
    #     本项管「数据质量合法」——防止上游生成脚本产出格式损坏的映射表
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

    # ⑤ 完整性快照（键数量 + 内容 sha256 冻结）
    digest = _digest(via_mcm)
    if force_update or not os.path.exists(SNAPSHOT):
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(digest, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"[FROZEN] ⑤ 完整性基线: {digest}" if not force_update
              else f"[UPDATED] ⑤ 完整性基线: {digest}")
    else:
        with open(SNAPSHOT, encoding="utf-8") as f:
            expected = json.load(f)
        if expected == digest:
            print(f"[PASS] ⑤ 完整性快照: n={digest['n_881_to_x']} "
                  f"sha256={digest['sha256'][:12]}… 一致")
        else:
            failures.append(f"映射内容变化: 期望 {expected} 实际 {digest}")
            print(f"[FAIL] ⑤ 完整性快照: 漂移\n  期望: {expected}\n  实际: {digest}")

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

# -*- coding: utf-8 -*-
"""
阶段 2.5：API 输出契约（显式声明引擎返回结构的必需键集）
=====================================================================
借鉴外部 pytest 实现（tests/tolerance.py 的 validate_result_structure），
按本仓库快照实测键集修正：
  - 「sub」仅 dual 模式存在，不做全局必需键（原实现误设为必需）；
  - 「generated_at」在规范化时已剥离，不进契约（原实现误设为必需）；
  - 新增内部一致性：meta 计数必须与对应列表长度相等（更强的自检）。

契约同时适用于：引擎原始输出 / 规范化后的快照（键集相同）。
用法：
    ok, diffs = validate_result_structure(result, expect_sub=False)
"""
import copy

# 顶层必需键（股票/期货单窗口通用；由 14 份冻结快照实测归纳）
REQUIRED_TOP_KEYS = {
    "meta", "klines", "bis", "fxs", "zs", "zs_stars",
    "segs", "bsps", "white_hline",
}

# meta 必需键（股票/期货通用；index_belong/pe_ttm/saved_selection_date 仅股票，不计入必需）
REQUIRED_META_KEYS = {
    "symbol", "market", "name", "freq", "chan_version",
    "kline_count", "bi_count", "fx_count", "zs_count",
    "seg_count", "bsp_count", "date_range", "is_replay", "forward_adjust",
}

# 子级别（dual 的 sub）实测为精简结构：无 white_hline、无 is_replay/forward_adjust。
# 显式声明两套口径，把该 API 不对称文档化（契约校验首跑即发现，冻结进契约）。
SUB_REQUIRED_TOP_KEYS = REQUIRED_TOP_KEYS - {"white_hline"}
SUB_REQUIRED_META_KEYS = REQUIRED_META_KEYS - {"is_replay", "forward_adjust"}

# meta 计数字段 → 顶层列表字段（内部一致性）
_COUNT_TO_LIST = {
    "kline_count": "klines",
    "bi_count": "bis",
    "fx_count": "fxs",
    "zs_count": "zs",
    "seg_count": "segs",
    "bsp_count": "bsps",
}


def validate_result_structure(result, expect_sub=False, path="$", _sub=False):
    """验证引擎返回结构完整性。返回 (ok: bool, diffs: [(path, got, want), ...])

    expect_sub: dual 模式结果应含 sub 字段（且 sub 自身满足子级契约）。
    _sub:       内部参数，按子级精简口径校验当前节点。
    错误路径（含 error 键）直接返回失败——契约只描述成功路径。
    """
    diffs = []
    if not isinstance(result, dict):
        return False, [(path, type(result).__name__, "dict")]
    if "error" in result:
        return False, [(path, f"error 响应: {str(result['error'])[:60]}", "成功结构")]

    req_top = SUB_REQUIRED_TOP_KEYS if _sub else REQUIRED_TOP_KEYS
    req_meta = SUB_REQUIRED_META_KEYS if _sub else REQUIRED_META_KEYS

    missing = req_top - set(result.keys())
    if missing:
        diffs.append((f"{path}.missing_keys", sorted(missing), "必需顶层键"))

    meta = result.get("meta")
    if not isinstance(meta, dict):
        diffs.append((f"{path}.meta", type(meta).__name__, "dict"))
    else:
        missing_meta = req_meta - set(meta.keys())
        if missing_meta:
            diffs.append((f"{path}.meta.missing_keys", sorted(missing_meta), "必需 meta 键"))

    for lk in ("klines", "bis", "fxs", "zs", "zs_stars", "segs", "bsps"):
        v = result.get(lk)
        if not isinstance(v, list):
            diffs.append((f"{path}.{lk}", type(v).__name__, "list"))

    # 内部一致性：meta 计数 == 列表长度（快照比对抓不到的自相矛盾）
    if isinstance(meta, dict):
        for cnt_key, list_key in _COUNT_TO_LIST.items():
            cnt, lst = meta.get(cnt_key), result.get(list_key)
            if isinstance(cnt, int) and isinstance(lst, list) and cnt != len(lst):
                diffs.append((f"{path}.meta.{cnt_key}", f"{cnt} != len({list_key})={len(lst)}", "计数与列表长度一致"))

    # dual 语义
    if expect_sub:
        sub = result.get("sub")
        if not isinstance(sub, dict):
            diffs.append((f"{path}.sub", type(sub).__name__, "dict"))
        else:
            ok_sub, diffs_sub = validate_result_structure(sub, expect_sub=False,
                                                          path=f"{path}.sub", _sub=True)
            diffs.extend(diffs_sub)
    return (len(diffs) == 0), diffs


def format_diffs(diffs):
    """契约差异 → 可读文本"""
    return "\n".join(f"  {p}: {g} (期望 {w})" for p, g, w in diffs[:20])

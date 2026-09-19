# -*- coding: utf-8 -*-
"""
阶段 2.5：快照递归比对器
=====================================================================
按设计文档 7.6 容差口径实现：
  浮点：相对容差 1e-6（|a-b| <= max(|a|,|b|) * 1e-6 视为相等）
  时间：1s 容差（epoch 秒/毫秒数值字段，或可解析为时间的字符串字段）
  其余：严格相等（类型、键集合、列表长度、逐元素）

返回 (ok, diff_text)；diff_text 给出 JSON 路径定位，首个差异即返回，
避免海量 K 线数据刷屏（可通过 MAX_DIFFS 提高输出上限）。

差异分两类，结构漂移优先（P2-1）：
  结构漂移 —— 键集合变化（多出/缺失）、类型变化、列表长度变化。这类差异
    说明「输出字段或形状变了」，最典型的是代码新增/删了一个展示字段而冻结
    基线没重冻（期货 bsps 的 fractal_low/high 就是这么滞后的）。它**单独收集、
    排在数值差异之前**，否则会被成百上千条 K 线数值差异挤到 MAX_DIFFS 之外
    永远看不见 —— 表现为「快照一直红，但没人知道红在字段上」。
  值差异 —— 数值/时间/标量不等，按 MAX_DIFFS 截断。
"""
import math
from datetime import datetime

REL_TOL = 1e-6          # 7.6 浮点相对容差
TIME_TOL_SEC = 1.0      # 7.6 时间戳容差（秒）
MAX_DIFFS = 8           # 最多报告的**值**差异数
MAX_STRUCT_DIFFS = 16   # 最多报告的**结构漂移**数（见模块 docstring）

_TIME_KEYS = {"timestamp", "ts", "sdt_ts", "edt_ts", "time", "dt"}


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _try_parse_time(v):
    """尝试把字符串解析为时间；失败返回 None"""
    if not isinstance(v, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _close_enough(a, b, key=None):
    """数值比较：时间键按时间容差（秒），其余按浮点相对容差"""
    if not (_is_number(a) and _is_number(b)):
        return False
    if key in _TIME_KEYS and (abs(a) > 1e9 or abs(b) > 1e9):
        # epoch 毫秒/秒量级的时间戳字段：1s 容差
        return abs(a - b) <= TIME_TOL_SEC * (1000 if abs(a) > 1e11 else 1)
    if a == b:
        return True
    return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=1e-12)


def compare(expected, actual, path="$", _diffs=None, _struct=None):
    """递归比对两个已规范化的结构。返回 (ok, diff_text)

    diff_text 中结构漂移段恒排在值差异段之前（理由见模块 docstring）。
    """
    if _diffs is None:
        _diffs = []
    if _struct is None:
        _struct = []
    _walk(expected, actual, path, _diffs, _struct)
    ok = not (_diffs or _struct)
    lines = []
    if _struct:
        lines.append(f"【结构漂移】{len(_struct)} 处（键集合/类型/长度；"
                     f"多为代码改了输出字段而基线未重冻，确认后 --update 重冻）:")
        lines.extend(f"  {s}" for s in _struct[:MAX_STRUCT_DIFFS])
        if len(_struct) > MAX_STRUCT_DIFFS:
            lines.append(f"  ... 另有 {len(_struct) - MAX_STRUCT_DIFFS} 处结构差异未展示")
    if _diffs:
        if lines:
            lines.append(f"【值差异】{min(len(_diffs), MAX_DIFFS)}/{len(_diffs)} 处:")
        lines.extend(_diffs[:MAX_DIFFS])
        if len(_diffs) > MAX_DIFFS:
            lines.append(f"... 另有 {len(_diffs) - MAX_DIFFS} 处差异未展示")
    return ok, "\n".join(lines)


def _walk(exp, act, path, diffs, struct):
    # 内部采集上限：两类差异各按自己的上限截断（对外仍截断到 MAX_*_DIFFS）。
    # 分开计的目的：让 K 线数值差异刷屏时，结构漂移仍能采集到并优先展示。
    if len(diffs) >= MAX_DIFFS * 4 and len(struct) >= MAX_STRUCT_DIFFS * 4:
        return
    # 时间字符串：1s 容差
    te, ta = _try_parse_time(exp), _try_parse_time(act)
    if te is not None and ta is not None:
        if abs((te - ta).total_seconds()) > TIME_TOL_SEC:
            diffs.append(f"{path}: 时间不等 expected={exp!r} actual={act!r}")
        return
    # 数值：容差比较（携带当前键名）
    if _is_number(exp) or _is_number(act):
        key = path.rsplit(".", 1)[-1].split("[")[0]
        if not _close_enough(exp, act, key=key):
            diffs.append(f"{path}: 数值超容差 expected={exp!r} actual={act!r}")
        return
    # 类型不同（结构漂移）
    if type(exp) is not type(act):
        struct.append(f"{path}: 类型不同 expected={type(exp).__name__} actual={type(act).__name__}")
        return
    # dict：键集合 + 递归
    if isinstance(exp, dict):
        for k in sorted(set(exp) - set(act)):
            struct.append(f"{path}.{k}: 期望存在，实际缺失（字段被删？）")
        for k in sorted(set(act) - set(exp)):
            struct.append(f"{path}.{k}: 实际多出，期望无此键（新增展示字段未重冻基线？）")
        for k in sorted(set(exp) & set(act)):
            _walk(exp[k], act[k], f"{path}.{k}", diffs, struct)
        return
    # list：长度 + 逐元素
    if isinstance(exp, list):
        if len(exp) != len(act):
            struct.append(f"{path}: 长度不同 expected={len(exp)} actual={len(act)}")
            return
        for i, (e, a) in enumerate(zip(exp, act)):
            _walk(e, a, f"{path}[{i}]", diffs, struct)
        return
    # 标量：严格相等
    if exp != act:
        diffs.append(f"{path}: 不等 expected={exp!r} actual={act!r}")

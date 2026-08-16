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
"""
import math
from datetime import datetime

REL_TOL = 1e-6          # 7.6 浮点相对容差
TIME_TOL_SEC = 1.0      # 7.6 时间戳容差（秒）
MAX_DIFFS = 8           # 最多报告的差异数

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


def compare(expected, actual, path="$", _diffs=None):
    """递归比对两个已规范化的结构。返回 (ok, diff_text)"""
    if _diffs is None:
        _diffs = []
    _walk(expected, actual, path, _diffs)
    ok = not _diffs
    text = "\n".join(_diffs[:MAX_DIFFS])
    if len(_diffs) > MAX_DIFFS:
        text += f"\n... 另有 {len(_diffs) - MAX_DIFFS} 处差异未展示"
    return ok, text


def _walk(exp, act, path, diffs):
    if len(diffs) >= MAX_DIFFS * 4:   # 内部采集上限（对外仍截断到 MAX_DIFFS）
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
    # 类型不同
    if type(exp) is not type(act):
        diffs.append(f"{path}: 类型不同 expected={type(exp).__name__} actual={type(act).__name__}")
        return
    # dict：键集合 + 递归
    if isinstance(exp, dict):
        for k in sorted(set(exp) - set(act)):
            diffs.append(f"{path}.{k}: 期望存在，实际缺失")
        for k in sorted(set(act) - set(exp)):
            diffs.append(f"{path}.{k}: 实际多出，期望无此键")
        for k in sorted(set(exp) & set(act)):
            _walk(exp[k], act[k], f"{path}.{k}", diffs)
        return
    # list：长度 + 逐元素
    if isinstance(exp, list):
        if len(exp) != len(act):
            diffs.append(f"{path}: 长度不同 expected={len(exp)} actual={len(act)}")
            return
        for i, (e, a) in enumerate(zip(exp, act)):
            _walk(e, a, f"{path}[{i}]", diffs)
        return
    # 标量：严格相等
    if exp != act:
        diffs.append(f"{path}: 不等 expected={exp!r} actual={act!r}")

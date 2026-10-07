# -*- coding: utf-8 -*-
"""旧 `fetch_kline` 接口的**页面同源**实现（签名兼容，后端换成 `tdx_source`）。

存在的理由：一批既有脚本按 `fetch(code, freq, n)` 调用、并按 **`dt` 是字符串**
的约定做后处理（`to_records()` / `datetime.strptime`）。要让它们直接吃页面同源
数据，要么逐个改后处理、要么保留这层薄壳。**选后者** —— 改动面最小，且
"数据来自哪"只有一处事实源（`tdx_source`）。

⚠ 与 `fetch_kline.fetch` 的唯一行为差异：
  · `dt` 仍是字符串（保持旧约约定），但**内容**是 vipdoc 的真实 K 线；
  · `n` **无效**（页面看多少根就是多少根，截断会改笔的起点）；
  · 分钟周期是**前复权**（旧 `fetch_min` 是不复权）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo(_p):
    """向上找到仓库根（含 Chan.py + DataAPI 的那一级）—— 从任意深度都成立。"""
    for _ in range(7):
        if (os.path.isfile(os.path.join(_p, "Chan.py"))
                and os.path.isdir(os.path.join(_p, "DataAPI"))):
            return _p
        _p = os.path.dirname(_p)
    return ""


EXP = HERE if os.path.basename(HERE) == "Exp" else os.path.dirname(HERE)
REPO = (os.environ.get("CHAN_REPO") or _find_repo(HERE)
        or os.path.join(os.path.dirname(os.path.dirname(EXP)), "wt_latest"))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(EXP, "common"))
sys.path.insert(0, HERE)

from tdx_source import CACHE, records as _records, stock_list  # noqa: F401,E402
from tdx_source import usable as _usable                       # noqa: F401,E402


def _fmt(freq: str) -> str:
    return "%Y-%m-%d %H:%M:%S" if freq.endswith("m") else "%Y-%m-%d 00:00:00"


def fetch(code: str, freq: str = "w", n: Optional[int] = None,
          use_cache: bool = True) -> List[Dict]:
    """页面同源 K 线（`dt` 为字符串，与旧 `fetch_kline.fetch` 同构）。"""
    fmt = _fmt(freq)
    out = []
    for r in _records(code, freq):
        rr = dict(r)
        rr["dt"] = r["dt"].strftime(fmt)
        out.append(rr)
    return out


def usable(code: str, freq: str = "w", n: Optional[int] = None) -> bool:
    return _usable(code, freq)


def to_records(rows: List[Dict]) -> List[Dict]:
    """`dt` 字符串 → datetime（与旧 `fetch_kline.to_records` 同构）。"""
    out = []
    for r in rows:
        rr = dict(r)
        if isinstance(rr["dt"], str):
            rr["dt"] = datetime.strptime(rr["dt"], _fmt("m" if len(rr["dt"]) > 10 else "d"))
        out.append(rr)
    return out


__all__ = ["fetch", "usable", "to_records", "stock_list", "CACHE", "os"]

# -*- coding: utf-8 -*-
"""
买卖点类型过滤（Backtest/Filter.py）
====================================
**事前过滤（PRE）**：不通过的类型**不进状态机、不占持仓期** —— 不是事后
从成交里剔除。这条区别是设计文档 §4.7.3 的定案（评审 P1 级）：

    放在开仓之后过滤 = 事后过滤 ⇒ 会引入"被过滤类型间接影响被放行类型"的
    静默耦合（被拒的簇照样占掉持仓期，后面的放行信号被拒收）。

放行判据**照抄** `Trading/Engine/Engine.py:_bsp_type_allowed`，不自造
（设计文档 §4.7.2）。常驻护栏：`Trading/Test/test_p59_bsp_type_filter.py`
把"与引擎同判据"钉死 —— 本模块改口径必须同步它。

判据（逐字照抄，含边界）：
    · 过滤表为 `None` → **放行**（未设置 = 全放行）；
    · 已设置 → 只有**显式勾选为真**的类型放行；
    · `type2str()` 可能是**逗号串**（同一笔同一右肩合并出的多类型，如 `"1,11"`）
      ⇒ 按逗号拆段，**任一段被勾选即放行**；
    · `"0"` / `"1"` / `"2"` / `"3"` 之外的细分类型（`11` / `22s` / `33a` …）
      不在勾选框内 ⇒ 一旦启用过滤就一律被忽略（刻意行为，别"修好"）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def bsp_type_allowed(bsp_type: Any, filt: Optional[Dict[str, Any]]) -> bool:
    """该买卖点类型串是否被放行（判据见模块 docstring，逐字照抄引擎）。"""
    if filt is None:
        return True
    for seg in str(bsp_type).split(","):
        seg = seg.strip()
        if seg and filt.get(seg):
            return True
    return False


def filter_from_choices(choices) -> Dict[str, bool]:
    """`"0,3"` 这样的勾选串 → `{"0": True, "1": False, "2": False, "3": True}`。

    便利函数，供 CLI / 测试构造过滤表；回测内核只吃 `dict` 或 `None`
    （过滤表的**真值源**是 `state.db` 的 `bsp_type_filter`，
    键名常量见 `Trading/Infra/Records.py:BSP_TYPE_CHOICES`）。
    """
    from Trading.Infra.Records import BSP_TYPE_CHOICES
    picked = {str(x).strip() for x in str(choices or "").split(",") if str(x).strip()}
    return {c: (c in picked) for c in BSP_TYPE_CHOICES}

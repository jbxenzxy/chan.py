# -*- coding: utf-8 -*-
"""
P61 出场判定「只用收盘价」防回潮护栏（2026-09-22）
==================================================================
口径（用户拍板）
----------------
`LayeredExitPolicy` 整层的**价格判定只读本根 K 线的收盘价 `bar.close`**，
一律不读 `bar.high` / `bar.low`。

理由：判定**时刻**本来就是"这根 K 线闭合之后"（引擎只在 `on_bar` 里调
`check_with`），若判定**依据**回头去看"盘中最小到过多少"，等于拿一个已经消失的
价格去触发一笔按当时盘口成交的单 —— 依据与时刻不是同一个东西。统一到收盘价后，
依据与时刻都是"这根 K 线结束时的那个价"。

由此**顺带作废**了一条旧规则：「同根 K 线同时触及止盈与止损 → 按止损计（悲观）」
—— 一个收盘价不可能既 ≥ 止盈线又 ≤ 止损线，该场景不可达（规则与对应用例已一并删除）。

为什么写成源码级 AST 断言，而不是只写行为样本
----------------------------------------------
行为样本只能证明"我构造的这几根 bar 走对了"，挡不住别人在**别的分支**里把
`bar.low` 写回来。本护栏直接扫源码：`check()` 函数体内出现 `.high` / `.low`
即变红；文件内任何 `.high` / `.low` 只能出现在豁免函数里。

唯一豁免：`_atr()`
------------------
真实波幅 TR 的**定义式**是 `max(H−L, |H−Pc|, |L−Pc|)`，必须用 high/low/前收 ——
它是波动率度量（喂 L2 的距离标定），不参与"是否触发"的判断。故豁免只给
`_atr()` 这一个函数，且**反过来断言它确实还在读 high/low**（否则"豁免"会退化成
"悄悄改成了别的口径也没人知道"）。

判别力自证（为什么先断言必现项）
--------------------------------
只断言"没有 X"的护栏，在文件搬走 / 类改名 / AST 解析失败时会**静默恒真**。
故本用例先断言必现项（`check()` 里确实出现 `bar.close`、`_atr()` 确实读 high/low），
再断言禁项。必现项一红 = "没扫到"不等于"已经清干净"。

行为级自证用**用户原始场景**：4000 开多、止损 3995、某根 15s K 线的 low 打到 3993
（插针）而收盘 3997 → **不触发**；同一根若收盘 3994 → 触发 sl。

本文件同时接管一条从 p8 搬来的守护：p8 的 [12]/[12b] 是"L3 浮盈用根内极值"的旧用例，
随口径统一已整体删除；其中「L3 启动阈值严格 = r_multiple_tp（2.99R 不启动 /
3.0R 恰好启动）」这条**与价格字段无关**的边界守护不该跟着消失，见本文件 [5]（用收盘价重述，
且 high 故意设成远超阈值 —— 实现一旦回头读 high，该组立刻变红）。

跑法：python Trading/Test/test_p61_exit_close_only.py
"""
from __future__ import annotations

import ast
import io
import os
import sys

_PASS = 0
_FAIL = 0

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SRC = os.path.join(_REPO, "Trading", "Strategy", "Exit.py")

sys.path.insert(0, _REPO)

from Trading.Infra.Instrument import Instrument                    # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES                 # noqa: E402
from Trading.Infra.Records import Bar, ExitPlan, Position, Side    # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy                # noqa: E402

_IF = PRODUCT_PROFILES["IF"]


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {}".format(name))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


# ════════════════════════════════════════════════════════════════
# 一、源码级断言
# ════════════════════════════════════════════════════════════════
def _method(tree, cls_name, fn_name):
    """取 cls_name.fn_name 的 FunctionDef（找不到 → None）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == fn_name:
                    return item
    return None


def _attrs_in(fn_node):
    """函数体内所有 `x.attr` 的属性名集合（docstring 是 Constant，不会混进来）。"""
    return {n.attr for n in ast.walk(fn_node) if isinstance(n, ast.Attribute)}


def _high_low_owners(tree):
    """文件内 `.high` / `.low` 访问的**归属函数名**集合：{attr: {fn, ...}}。"""
    owners = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr in ("high", "low"):
                owners.setdefault(sub.attr, set()).add(node.name)
    return owners


def main():
    print("\n[1] 源码级：check() 的价格输入只有 bar.close")
    with io.open(_SRC, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)

    cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LayeredExitPolicy":
            cls = node
    check("类 LayeredExitPolicy 存在（护栏扫描基成立）", cls is not None, True)

    chk = _method(tree, "LayeredExitPolicy", "check")
    check("方法 check() 存在", chk is not None, True)

    chk_attrs = _attrs_in(chk)
    # 必现项：先证明"确实扫到了这个函数里该有的东西"，再断言禁项
    check("[必现] check() 内出现 bar.close 属性访问",
          "close" in chk_attrs, True)
    check("[禁项] check() 内不出现 .high", "high" in chk_attrs, False)
    check("[禁项] check() 内不出现 .low", "low" in chk_attrs, False)

    # 文件级：.high / .low 只允许出现在 _atr()，且必须还在那里
    owners = _high_low_owners(tree)
    check("[豁免] 全文件读 .high 的只有 _atr()",
          owners.get("high"), {"_atr"})
    check("[豁免] 全文件读 .low 的只有 _atr()",
          owners.get("low"), {"_atr"})

    atr = _method(tree, "LayeredExitPolicy", "_atr")
    check("方法 _atr() 存在（豁免对象在）", atr is not None, True)
    atr_attrs = _attrs_in(atr)
    check("[必现] _atr() 仍读 .high（TR 定义式要求）", "high" in atr_attrs, True)
    check("[必现] _atr() 仍读 .low（TR 定义式要求）", "low" in atr_attrs, True)

    print("\n[2] 源码级：已作废的「同根双破取悲观」规则不得回潮")
    check("保守设定表里不再有「① 同根…按止损计」这一条",
          "① 同根 K 线同时触及止盈与止损" in src, False)
    check("保守设定 ① 已换成「价格对齐」",
          "① 价格对齐一律往" in src, True)
    check("文档已写明该场景「已不可达」",
          "已不可达" in src, True)

    # ════════════════════════════════════════════════════════════
    # 二、行为级断言（用户场景：IF 15s，4000 开多，止损 3995）
    # ════════════════════════════════════════════════════════════
    print("\n[3] 行为级：插针不触发、收盘打穿才触发（多仓）")

    def _bar(h, l, c, ts=2000):
        # open 取上一根收盘（3997），对判定无影响（判定只读 close）
        return Bar(timestamp=ts, date="2026-09-01 09:40", open=3997.0,
                   high=h, low=l, close=c, vol=1)

    def _pos():
        return Position(symbol="CFFEX.IF", side=Side.LONG, volume=1,
                        entry_price=4000.0, entry_at="2026-09-01 09:40",
                        entry_bar_ts=1000, signal_key="k1",
                        open_order_id="o1", entry_bar_seq=1,
                        exit_plan=ExitPlan(name="LayeredExitPolicy",
                                           stop_price=3995.0, tp_price=None,
                                           params={"R": 5.0}))

    pol = LayeredExitPolicy({"use_trailing": False})
    st = Instrument(None, _IF)

    # 3a 插针：low 打到 3993（穿止损），收盘 3997 → 收盘价口径下**不触发**
    chk_wick = pol.check(_pos(), _bar(3997.0, 3993.0, 3997.0), st)
    check("[3a] low 穿止损但收盘在线上 → 不触发（插针不再被扫）",
          chk_wick, None)

    # 3b 同一根若收盘 3994（≤ 3995）→ 触发 sl，触发价 = 止损线
    chk_close = pol.check(_pos(), _bar(3997.0, 3993.0, 3994.0), st)
    check("[3b] 收盘 3994 ≤ 止损 3995 → 触发 sl",
          (chk_close.reason, chk_close.price) if chk_close else None,
          ("sl", 3995.0))

    # 3c 收盘恰好等于止损线 → 触发（≤ 是闭区间，边界不丢）
    chk_edge = pol.check(_pos(), _bar(3997.0, 3993.0, 3995.0), st)
    check("[3c] 收盘 == 止损线 → 触发（边界闭区间）",
          chk_edge.reason if chk_edge else None, "sl")

    # 3d 空仓镜像：high 穿止损但收盘在线上 → 不触发
    short = _pos()
    short.side = Side.SHORT
    short.exit_plan = ExitPlan(name="LayeredExitPolicy", stop_price=4005.0,
                               tp_price=None, params={"R": 5.0})
    chk_short = pol.check(short, Bar(timestamp=2000, date="2026-09-01 09:40",
                                     open=4003.0, high=4007.0, low=4003.0,
                                     close=4003.0, vol=1), st)
    check("[3d] 空仓 high 穿止损、收盘未穿 → 不触发", chk_short, None)

    print("\n[4] 行为级：L3 浮盈达标同样只看收盘价（跟踪止盈模式）")
    pol3 = LayeredExitPolicy({"use_atr": False, "use_trailing": True,
                              "breakeven_trigger_r": 1.0,
                              "breakeven_buffer_r": 0.0,
                              "r_multiple_tp": 99.0})
    # R=10、入场 4000 → 保本阈值 4010。high 冲到 4015 但收盘 4005 → 不算达标
    p1 = _pos()
    p1.exit_plan = ExitPlan(name="LayeredExitPolicy", stop_price=3990.0,
                            tp_price=None,
                            params={"R": 10.0, "_trail_best": 4000.0})
    chk_l3_wick = pol3.check(p1, Bar(timestamp=2000, date="2026-09-01 09:40",
                                     open=4000.0, high=4015.0, low=3999.0,
                                     close=4005.0, vol=1), st)
    check("[4a] high 到过 4015 但收盘 4005（< 1R）→ L3 不动（只更新）",
          chk_l3_wick, None)

    p2 = _pos()
    p2.exit_plan = ExitPlan(name="LayeredExitPolicy", stop_price=3990.0,
                            tp_price=None,
                            params={"R": 10.0, "_trail_best": 4000.0})
    chk_l3_close = pol3.check(p2, Bar(timestamp=2000, date="2026-09-01 09:40",
                                      open=4000.0, high=4015.0, low=3999.0,
                                      close=4011.0, vol=1), st)
    check("[4b] 收盘 4011 ≥ 1R → 保本层生效（only_update，止损抬到 4000）",
          (chk_l3_close.only_update,
           chk_l3_close.plan.stop_price) if chk_l3_close else None,
          (True, 4000.0))

    print("\n[5] L3 启动阈值仍严格 = r_multiple_tp（从 p8[12b] 搬来，用收盘价重述）")
    # 来历：p8 的 [12b] 是 `_l3_started(pol, high, ts)`——把"high 抬多高"当浮盈来钉
    # IC=3R / IF=2R 的启动边界。那两节（[12]/[12b]）随口径统一被整体删除，
    # 但"阈值严格 = r_multiple_tp（2.99R 不启动 / 3.0R 恰好启动）"这条与价格字段
    # **无关**的守护不该跟着一起消失，故在此用收盘价重述。
    pol_ic = LayeredExitPolicy({"use_atr": False, "use_trailing": True,
                                "breakeven_trigger_r": 99.0,
                                "breakeven_buffer_r": 0.0, "r_multiple_tp": 3.0})
    pol_if = LayeredExitPolicy({"use_atr": False, "use_trailing": True,
                                "breakeven_trigger_r": 99.0,
                                "breakeven_buffer_r": 0.0, "r_multiple_tp": 2.0})

    def _l3_started(pol, close, ts):
        """R=10、入场 100 的单根 bar：收盘价抬到 close → L3 是否启动。

        ⚠️ high 故意设成 `close + 50`（远超任何阈值）：若实现回头去读 high，
        "2.99R 不启动"这几条会立刻变红 —— 这是本组断言的判别力来源。
        """
        p = Position(symbol="CFFEX.IF", side=Side.LONG, volume=1,
                     entry_price=100.0, entry_at="2026-09-01 09:40",
                     entry_bar_ts=1000, signal_key="k2", open_order_id="o2",
                     entry_bar_seq=1,
                     exit_plan=ExitPlan(name="LayeredExitPolicy", stop_price=90.0,
                                        tp_price=None,
                                        params={"R": 10.0, "_trail_best": 100.0}))
        bar = Bar(timestamp=ts, date="2026-09-01 09:40", open=100.0,
                  high=close + 50.0, low=95.0, close=close, vol=1)
        chk = pol.check(p, bar, st)
        return bool(chk is not None and chk.only_update)

    check("IC(3R) 收盘 2.99R 不启动 L3（阈值严格 ≥ 的下侧）",
          _l3_started(pol_ic, 129.9, 2710), False)
    check("IC(3R) 收盘 3.0R **恰好**启动（不是 3.5R）",
          _l3_started(pol_ic, 130.0, 2711), True)
    check("IF(2R) 收盘 1.99R 不启动 L3", _l3_started(pol_if, 119.9, 2712), False)
    check("IF(2R) 收盘 2.0R **恰好**启动", _l3_started(pol_if, 120.0, 2713), True)
    check("同一 2.5R 收盘：IC(3R) 不启动", _l3_started(pol_ic, 125.0, 2714), False)
    check("同一 2.5R 收盘：IF(2R) 已启动", _l3_started(pol_if, 125.0, 2715), True)

    print("\n" + "=" * 60)
    print("P61 结果: {} passed, {} failed".format(_PASS, _FAIL))
    print("=" * 60)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

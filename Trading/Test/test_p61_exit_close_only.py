# -*- coding: utf-8 -*-
"""
P61 出场判定「触发看收盘价 / 达标看根内极值」防回潮护栏（2026-09-22）
==================================================================
口径（用户拍板 → 同日二次拍板修正）
------------------------------------
`LayeredExitPolicy` 的**两层判据刻意用不同的价**，不是漏改、不要"顺手统一"：

  · **触发判据** —— 止损线 / 止盈线 / 保本价 / 跟踪价是否被穿过（→ 是否报单离场）
    **只读本根 K 线的收盘价 `bar.close`**，一律不读 `bar.high` / `bar.low`。
    理由：判定**时刻**是"这根 K 线闭合之后"，依据就必须是"这根结束时的那个价"，
    两者才是同一个东西；盘中到过的价此刻已经消失，用它去触发一笔按当时盘口成交的单
    是自欺。由此**顺带作废**了一条旧规则：「同根 K 线同时触及止盈与止损 → 按止损计
    （悲观）」—— 一个收盘价不可能既 > 止盈线又 < 止损线，该场景不可达（用例已删）。

  · **止损侧边界一律严格不等**（2026-09-22 · 用户拍板"改为需求原文口径 ＜ / ＞"）——
    上面两层判据的**比较符号**取严格不等，不用 `<=` / `>=`：
      触发侧：多 `close < stop` / 空 `close > stop`（保本价 / 跟踪价同样走这两行 ——
              L3 是改写 `stop_price`，不另立字段）；
      达标侧：`fav_profit > breakeven_trigger_r×R`、`fav_profit > win_loss_ratio×R`。
    即"收盘价恰好等于保护价"与"有利极值恰好等于 k×R"都**不算**触发 / 达标。
    为什么不是无所谓的细节：价格离散到 tick，"恰好相等"是**可达状态**，
    旧闭区间口径会在这类行情上提前一格离场（多等一根即可）。本文件 [3c] / [3e] /
    [4b2] / [5] 钉死。
    ⚠️ **固定止盈单（`use_trailing=False` 非默认模式）例外，仍是闭区间**：需求原文只描述
    跟踪模式，该模式原文未定义、语义是"目标价触及即走"，其守卫在
    `test_p60_trade_toasts_reconcile_gap.py` 的 [5c]（`close == tp` → 触发止盈）。
    本文件不管那一处，也不得顺手把它改严 —— 要改得连 p60 [5c] 一起改。

  · **达标判据** —— 浮盈是否够 `breakeven_trigger_r×R` 进保本、够 `win_loss_ratio×R`
    进跟踪，以及跟踪锚"至今最好价"—— 读本根 K 线的**有利侧极值**：
    做多 `bar.high`、做空 `bar.low`。
    理由：它衡量的不是"能否成交"，而是"这一段行情最远走到过哪里"。用收盘价衡量会把
    「盘中冲高 1.5R、收盘回落到 0.3R」那根判成不达标，利润继续裸奔到 −1R。
    代价（已知并接受）：达标那根的**收盘价**可能落在**新设的保护价**的不利侧
    （high 到 +1.2R、收盘回到 +0.1R，保本价设 +0.5R）。
    ⚠️ 但代价的形态**不是**"同一根内设定即失效" —— 同一根绝不二次判定：
    ① 触发判据在 `check()` 里排得更早、用的是进入本根时的**旧**保护价；达标命中后立即
    `only_update=True` 返回（只更新计划、不报单），引擎随即 return，同一根不再做任何
    触发判定 → 新保护价最快只能在**下一根**生效。代价的真实形态是"下一根只要收盘仍在
    保护价不利侧就立刻离场"，即"常在设定后的下一根即走"。由本文件 [7] 组钉死该时序。

为什么写成源码级 AST 断言，而不是只写行为样本
----------------------------------------------
行为样本只能证明"我构造的这几根 bar 走对了"，挡不住别人在**别的分支**里改回去。
本护栏直接扫源码：
  · `check()` 函数体内出现 `.high` / `.low` → 变红（**触发判据**不得碰极值）；
  · 全文件读 `.high` / `.low` 的函数**只能**是 `_atr()` 与 `_fav_extreme()` → 多一处变红
    （**达标判据**必须只经 `_fav_extreme()` 读极值，不许在别处再开一个口子）。

两个豁免函数各自的理由
-----------------------
  · `_atr()`：真实波幅 TR 的**定义式**是 `max(H−L, |H−Pc|, |L−Pc|)`，必须用
    high/low/前收 —— 它是波动率度量（喂 L2 的距离标定），不参与"是否触发"的判断；
  · `_fav_extreme()`：L3 **达标判据**的唯一读极值入口（本文件唯一为"是否达标"读
    high/low 的地方），单独成函数就是为了让上面第二条 AST 断言能被表达。

判别力自证（为什么先断言必现项）
--------------------------------
只断言"没有 X"的护栏，在文件搬走 / 类改名 / AST 解析失败时会**静默恒真**。
故本用例先断言必现项（`check()` 里确实出现 `bar.close`、确实**调用**了
`_fav_extreme`、两个豁免函数确实还在读 high/low），再断言禁项。
必现项一红 = "没扫到"不等于"已经清干净"。

行为级自证用**用户原始场景**：
  · 触发侧：4000 开多、止损 3995，某根 15s K 线的 low 打到 3993（插针）而收盘 3997
    → **不触发**；同一根若收盘 3994 → 触发 sl。
  · 达标侧：R=10、入场 4000，某根 high 冲到 4015（过 1R）而收盘 4005
    → **进保本**（极值口径）；high 只到 4008 → 不动。

本文件同时接管一条从 p8 搬来的守护：p8 的 [12]/[12b] 是"L3 浮盈用根内极值"的旧用例，
2026-09-22 首次改口径时被整体删除、等价守护搬到这里；同日二次拍板把达标侧改回极值后，
本文件 [5] 恢复为"用 high 抬多高当浮盈"的原形状，并额外钉住**触发侧仍只看收盘价**
（[3] 与 [1] 的 AST 断言）。

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

# 允许读 high/low 的函数白名单：TR 定义式 + L3 达标极值。多一个就变红。
_HILO_WHITELIST = {"_atr", "_fav_extreme"}


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


def _calls_in(fn_node):
    """函数体内所有被调用者的名字集合（`self.foo()` → "foo"）。"""
    names = set()
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


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
    print("\n[1] 源码级：check() 的价格输入只有 bar.close；达标极值只经 _fav_extreme()")
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

    # 扫不到（函数被删/改名）时不能让 _attrs_in / _calls_in 崩掉 ——
    # 负控树（把 Exit.py 回退成原版）正是这种情况：必须是可引用的 ✗，不是 Traceback。
    chk_attrs = _attrs_in(chk) if chk is not None else set()
    # 必现项：先证明"确实扫到了这个函数里该有的东西"，再断言禁项
    check("[必现] check() 内出现 bar.close 属性访问",
          "close" in chk_attrs, True)
    check("[禁项] check() 内不出现 .high（触发判据不得读极值）",
          "high" in chk_attrs, False)
    check("[禁项] check() 内不出现 .low（触发判据不得读极值）",
          "low" in chk_attrs, False)
    # 达标侧必须是"经 _fav_extreme 读极值"，不能改成别的写法
    check("[必现] check() 内调用了 _fav_extreme（达标极值入口）",
          "_fav_extreme" in (_calls_in(chk) if chk is not None else set()), True)

    # 文件级：.high / .low 只允许出现在白名单那两个函数里，且它们必须还在读
    owners = _high_low_owners(tree)
    check("[豁免] 全文件读 .high 的仅 _atr() 与 _fav_extreme()",
          owners.get("high"), _HILO_WHITELIST)
    check("[豁免] 全文件读 .low 的仅 _atr() 与 _fav_extreme()",
          owners.get("low"), _HILO_WHITELIST)

    atr = _method(tree, "LayeredExitPolicy", "_atr")
    check("方法 _atr() 存在（豁免对象在）", atr is not None, True)
    atr_attrs = _attrs_in(atr) if atr is not None else set()
    check("[必现] _atr() 仍读 .high（TR 定义式要求）", "high" in atr_attrs, True)
    check("[必现] _atr() 仍读 .low（TR 定义式要求）", "low" in atr_attrs, True)

    ext = _method(tree, "LayeredExitPolicy", "_fav_extreme")
    check("方法 _fav_extreme() 存在（L3 达标极值入口在）", ext is not None, True)
    ext_attrs = _attrs_in(ext) if ext is not None else set()
    check("[必现] _fav_extreme() 仍读 .high（做多有利侧）", "high" in ext_attrs, True)
    check("[必现] _fav_extreme() 仍读 .low（做空有利侧）", "low" in ext_attrs, True)

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
    print("\n[3] 触发侧行为级：插针不触发、收盘打穿才触发（多仓）")

    def _bar(h, l, c, ts=2000):
        # open 取上一根收盘（3997），对判定无影响（触发判定只读 close）
        return Bar(timestamp=ts, date="2026-09-01 09:40", open=3997.0,
                   high=h, low=l, close=c, vol=1)

    def _pos(side=Side.LONG, entry=4000.0, stop=3995.0, params=None):
        return Position(symbol="CFFEX.IF", side=side, volume=1,
                        entry_price=entry, entry_at="2026-09-01 09:40",
                        entry_bar_ts=1000, signal_key="k1",
                        open_order_id="o1", entry_bar_seq=1,
                        exit_plan=ExitPlan(name="LayeredExitPolicy",
                                           stop_price=stop, tp_price=None,
                                           params=params or {"R": 5.0}))

    pol = LayeredExitPolicy({"use_trailing": False})
    st = Instrument(None, _IF)

    # 3a 插针：low 打到 3993（穿止损），收盘 3997 → 触发侧口径下**不触发**
    chk_wick = pol.check(_pos(), _bar(3997.0, 3993.0, 3997.0), st)
    check("[3a] low 穿止损但收盘在线上 → 不触发（插针不再被扫）",
          chk_wick, None)

    # 3b 同一根若收盘 3994（< 3995）→ 触发 sl，触发价 = 止损线
    chk_close = pol.check(_pos(), _bar(3997.0, 3993.0, 3994.0), st)
    check("[3b] 收盘 3994 < 止损 3995 → 触发 sl",
          (chk_close.reason, chk_close.price) if chk_close else None,
          ("sl", 3995.0))

    # 3c **边界（严格不等 · 2026-09-22 用户拍板）**：收盘恰好等于止损线 → **不触发**，
    #    要等下一根收盘再低一格才走。旧闭区间口径（`<=`）下这里判"触发"，本断言就是
    #    那条差异的钉子 —— 若哪天被改回 `<=`，[3c] 立刻变红。
    chk_edge = pol.check(_pos(), _bar(3997.0, 3993.0, 3995.0), st)
    check("[3c] 收盘 == 止损线 → **不触发**（严格不等）", chk_edge, None)
    # 3c' 再低 1 tick（IF tick = 0.2）→ 触发。用来证明 [3c] 的 None 是"卡在边界上"，
    #     而不是整段失灵（否则把 check() 改成永远返回 None 也能让 [3c] 变绿）。
    chk_edge2 = pol.check(_pos(), _bar(3997.0, 3993.0, 3994.8), st)
    check("[3c'] 收盘 3994.8（再低 1 tick）→ 触发 sl（边界只差一格）",
          (chk_edge2.reason, chk_edge2.price) if chk_edge2 else None,
          ("sl", 3995.0))

    # 3d 空仓镜像：high 穿止损但收盘在线上 → 不触发
    chk_short = pol.check(_pos(Side.SHORT, 4000.0, 4005.0),
                          Bar(timestamp=2000, date="2026-09-01 09:40",
                              open=4003.0, high=4007.0, low=4003.0,
                              close=4003.0, vol=1), st)
    check("[3d] 空仓 high 穿止损、收盘未穿 → 不触发", chk_short, None)

    # 3e 空仓边界镜像：收盘恰好等于止损线 → 不触发；再高 1 tick → 触发。
    #    多空两侧必须同口径 —— 只改一边（多严格 / 空闭区间）是很容易犯的漏改。
    chk_s_edge = pol.check(_pos(Side.SHORT, 4000.0, 4005.0),
                          Bar(timestamp=2001, date="2026-09-01 09:41",
                              open=4003.0, high=4005.2, low=4003.0,
                              close=4005.0, vol=1), st)
    check("[3e] 空仓 收盘 == 止损线 → **不触发**（严格不等）", chk_s_edge, None)
    chk_s_edge2 = pol.check(_pos(Side.SHORT, 4000.0, 4005.0),
                            Bar(timestamp=2002, date="2026-09-01 09:41",
                                open=4003.0, high=4005.4, low=4003.0,
                                close=4005.2, vol=1), st)
    check("[3e'] 空仓 收盘 4005.2（再高 1 tick）→ 触发 sl",
          (chk_s_edge2.reason, chk_s_edge2.price) if chk_s_edge2 else None,
          ("sl", 4005.0))

    print("\n[4] 达标侧行为级：L3 浮盈看**根内极值**（做多 high / 做空 low）")
    # R=10、入场 4000 → 保本阈值 4010（+1R）。win_loss_ratio=99 关掉跟踪，只验保本层；
    # breakeven_buffer_r=0.0 → 保本落点 = 入场价本身，断言值好读。
    pol3 = LayeredExitPolicy({"use_atr": False, "use_trailing": True,
                              "breakeven_trigger_r": 1.0,
                              "breakeven_buffer_r": 0.0,
                              "win_loss_ratio": 99.0})

    # 4a **本组的判别力来源**：high 到过 4015（≥1R）但收盘 4005（<1R）
    #    → 极值口径下算达标、抬损到 4000；若实现回头只用收盘价，这里必红。
    chk_l3_wick = pol3.check(
        _pos(params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2000, date="2026-09-01 09:40", open=4000.0,
            high=4015.0, low=3999.0, close=4005.0, vol=1), st)
    check("[4a] high 到过 4015 而收盘 4005（< 1R）→ **达标**，保本抬到 4000",
          (chk_l3_wick.only_update, chk_l3_wick.plan.stop_price)
          if chk_l3_wick else None,
          (True, 4000.0))

    # 4b high 也没过 1R → L3 完全不动（防止 4a 被写成"只要返回东西就算过"）
    chk_l3_none = pol3.check(
        _pos(params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2001, date="2026-09-01 09:40", open=4000.0,
            high=4008.0, low=3999.0, close=4005.0, vol=1), st)
    check("[4b] high 只到 4008（< 1R）→ L3 不动", chk_l3_none, None)

    # 4b2 / 4b3 **达标边界（严格不等 · 2026-09-22 拍板）**：high 恰好 == 入场+1R 不达标，
    #    再高 1 tick 才达标。需求原文 ⑶ 情况一写的是「最高价 − 入场价 **＞** 1R」。
    chk_l3_eq = pol3.check(
        _pos(params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2002, date="2026-09-01 09:40", open=4000.0,
            high=4010.0, low=3999.0, close=4005.0, vol=1), st)
    check("[4b2] high == 入场+1R（4010）→ **不达标**（严格不等）",
          chk_l3_eq, None)
    chk_l3_gt = pol3.check(
        _pos(params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2003, date="2026-09-01 09:40", open=4000.0,
            high=4010.2, low=3999.0, close=4005.0, vol=1), st)
    check("[4b3] high 4010.2（再高 1 tick）→ 达标，保本抬到 4000",
          (chk_l3_gt.only_update, chk_l3_gt.plan.stop_price)
          if chk_l3_gt else None, (True, 4000.0))

    # 4c 空仓镜像：low 到过 3985（≥1R）而收盘 3995（<1R）→ 达标，保本压到 4000
    chk_l3_short = pol3.check(
        _pos(Side.SHORT, 4000.0, 4010.0, params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2002, date="2026-09-01 09:40", open=4000.0,
            high=4001.0, low=3985.0, close=3995.0, vol=1), st)
    check("[4c] 空仓 low 到过 3985 而收盘 3995（< 1R）→ 达标，保本压到 4000",
          (chk_l3_short.only_update, chk_l3_short.plan.stop_price)
          if chk_l3_short else None,
          (True, 4000.0))

    print("\n[5] L3 启动阈值仍严格 = win_loss_ratio（IC=3R / IF=2R 边界，比较取严格 >）")
    # ⚠️ 本组把**收盘价固定在入场价**、只让 high 抬到指定倍数 —— 于是"能不能启动"
    #    完全由极值口径决定。若实现回头只看收盘价，2.99R / 3.0R / 3.01R 三条都会失真；
    #    若实现把比较改回 `>=`，则"恰好 3.0R 不启动"那条（严格不等）立刻变红。
    pol_ic = LayeredExitPolicy({"use_atr": False, "use_trailing": True,
                                "breakeven_trigger_r": 99.0,
                                "breakeven_buffer_r": 0.0, "win_loss_ratio": 3.0})
    pol_if = LayeredExitPolicy({"use_atr": False, "use_trailing": True,
                                "breakeven_trigger_r": 99.0,
                                "breakeven_buffer_r": 0.0, "win_loss_ratio": 2.0})

    def _l3_started(pol, high, ts):
        """R=10、入场 100 的单根 bar：high 抬到 high → L3 是否启动。"""
        p = Position(symbol="CFFEX.IF", side=Side.LONG, volume=1,
                     entry_price=100.0, entry_at="2026-09-01 09:40",
                     entry_bar_ts=1000, signal_key="k2", open_order_id="o2",
                     entry_bar_seq=1,
                     exit_plan=ExitPlan(name="LayeredExitPolicy", stop_price=90.0,
                                        tp_price=None,
                                        params={"R": 10.0, "_trail_best": 100.0}))
        bar = Bar(timestamp=ts, date="2026-09-01 09:40", open=100.0,
                  high=high, low=95.0, close=100.0, vol=1)
        chk = pol.check(p, bar, st)
        return bool(chk is not None and chk.only_update)

    check("IC(3R) high 2.99R 不启动 L3（阈值严格 > 的下侧）",
          _l3_started(pol_ic, 129.9, 2710), False)
    check("IC(3R) high 3.0R **恰好不启动**（严格不等：> 3R 才算）",
          _l3_started(pol_ic, 130.0, 2711), False)
    check("IC(3R) high 3.01R 启动（越过阈值 1 tick 即算）",
          _l3_started(pol_ic, 130.1, 2712), True)
    check("IF(2R) high 1.99R 不启动 L3", _l3_started(pol_if, 119.9, 2713), False)
    check("IF(2R) high 2.0R **恰好不启动**（严格不等）",
          _l3_started(pol_if, 120.0, 2714), False)
    check("IF(2R) high 2.01R 启动", _l3_started(pol_if, 120.1, 2715), True)
    check("同一 2.5R：IC(3R) 不启动", _l3_started(pol_ic, 125.0, 2716), False)
    check("同一 2.5R：IF(2R) 已启动", _l3_started(pol_if, 125.0, 2717), True)

    print("\n[6] 两层判据的优先级：同一根 K 线上「收盘打穿止损」优先于「极值达标」")
    # 触发判据在 check() 里排在 L3 之前：一根 high 冲到 2R、收盘却砸穿 −1R 止损的巨阴
    # → 必须先离场（返回 sl），不能因为极值达标而去抬损、把仓留着。
    chk_both = LayeredExitPolicy(
        {"use_atr": False, "use_trailing": True, "breakeven_trigger_r": 1.0,
         "breakeven_buffer_r": 0.5, "win_loss_ratio": 2.0}).check(
        _pos(params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2000, date="2026-09-01 09:40", open=4000.0,
            high=4025.0, low=3985.0, close=3988.0, vol=1), st)
    check("[6a] 同根 high 达标 + 收盘破止损 → 判 sl（触发优先，不抬损）",
          (chk_both.reason, chk_both.price) if chk_both else None,
          ("sl", 3995.0))
    check("[6b] 该结果不是 only_update（确实登场离场）",
          chk_both.only_update if chk_both else None, False)

    print("\n[7] 时序：达标当根**不登场**，新保护价最快下一根才生效")
    # 针对一个易被误读的点：极值口径下"达标那根的 close 落在新保护价不利侧"，
    #   形似"保护价一设定就已失效"。代码事实是**同一根绝不二次判定**：
    #     · ① 触发判据用**旧**保护价（3990）判本根 close —— 没穿，故不返回；
    #     · ② L3 用 high 达标 → 只更新计划（only_update=True），随即 return；
    #       Trading/Engine/Engine.py 收到 only_update 后也只 `_persist()` 就 return。
    #   → 离场最快只能发生在**下一根**。若有人把 L3 挪到 ① 之前，或让 L3 命中后继续
    #     往下判触发（改成 same-bar 二次判定），[7a]/[7b] 必红。
    pol4 = LayeredExitPolicy({"use_atr": False, "use_trailing": True,
                              "breakeven_trigger_r": 1.0,
                              "breakeven_buffer_r": 0.5,
                              "win_loss_ratio": 99.0})
    # 7a 达标根：high=4015（≥1R）→ 保本价从 3990 抬到 4005；而这根 close=4001 **低于** 4005
    #    （即"新保护价不利侧"）；low=3991 未穿旧止损 3990。
    chk_d0 = pol4.check(
        _pos(stop=3990.0, params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2000, date="2026-09-01 09:40", open=4000.0,
            high=4015.0, low=3991.0, close=4001.0, vol=1), st)
    check("[7a] 达标根：close 落在**新**保本价不利侧 → 仍只更新计划、保本抬到 4005",
          # plan 可能为 None（若实现被改成"同根直接登场"）→ 必须先判空，
          # 否则负控树里会崩成 rc=2，证据从"红"退化成"崩"。
          (chk_d0.reason, chk_d0.only_update,
           chk_d0.plan.stop_price if chk_d0.plan is not None else None)
          if chk_d0 else None, ("trailing", True, 4005.0))
    check("[7b] 该根**没有**被新保护价触发离场（reason 不是 sl/tp）",
          (chk_d0.reason in ("sl", "tp")) if chk_d0 else None, False)

    # 7c 下一根：close=4002 仍 < 4005 → 这时才以新保护价判 sl（fill 以收盘成交价为准）
    chk_d1 = pol4.check(
        _pos(stop=4005.0, params={"R": 10.0, "_trail_best": 4015.0}),
        Bar(timestamp=2001, date="2026-09-01 09:41", open=4002.0,
            high=4006.0, low=3998.0, close=4002.0, vol=1), st)
    check("[7c] 下一根 close 4002 < 新保护价 4005 → 这时才离场",
          (chk_d1.reason, chk_d1.price, chk_d1.fill_price, chk_d1.only_update)
          if chk_d1 else None, ("sl", 4005.0, 4002.0, False))

    # 7d 空仓镜像：low 达标 → 保本压到 3995，而该根 close=3999 在不利侧 → 仍不登场
    chk_s0 = pol4.check(
        _pos(Side.SHORT, 4000.0, 4010.0, params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2002, date="2026-09-01 09:40", open=4000.0,
            high=4009.0, low=3985.0, close=3999.0, vol=1), st)
    check("[7d] 空仓镜像：close 落在**新**保本价不利侧 → 仍只更新计划、保本压到 3995",
          (chk_s0.reason, chk_s0.only_update,
           chk_s0.plan.stop_price if chk_s0.plan is not None else None)
          if chk_s0 else None, ("trailing", True, 3995.0))

    print("\n[8] reason 细分：保护价被跌破时标的是**哪一层**（2026-09-22 拍板）")
    # reason 只表达"哪条规则触发的离场"，**不表达盈亏**（保本离场也可能被滑点打成净亏，
    #   盈亏一律由 net_cash 符号在统计侧分 —— 见 Infra/TradeStats.py 的 by_reason 说明）。
    #   判定依据 = `plan.params["_phase"]`（L3 在保护价真被抬高时写入），它与"最后把保护价
    #   抬上去的那一层"恒同源 → 标的必然就是被跌破的那条线。
    #   本组用的策略对象是 use_trailing=False（L3 不跑）、`_phase` 直接注入计划参数 ——
    #   这恰好也在证明"reason 路由只读计划快照"：既与 L3 是否启用无关，也覆盖旧 state.db
    #   恢复的、缺 `_phase` 的持仓（退回 "sl"）。
    for _ph, _want, _name in (("", "sl", "未进 L3（无 _phase）"),
                              ("breakeven", "breakeven", "已进保本"),
                              ("trailing", "trailing", "已进跟踪")):
        _p = {"R": 5.0}
        if _ph:
            _p["_phase"] = _ph
        _chk_r = pol.check(_pos(params=_p), _bar(3997.0, 3993.0, 3994.0), st)
        check("[8] 多仓 {} → reason = {}".format(_name, _want),
              _chk_r.reason if _chk_r else None, _want)
    _chk_s = pol.check(_pos(Side.SHORT, 4000.0, 4005.0,
                            params={"R": 5.0, "_phase": "trailing"}),
                       _bar(4007.0, 4003.0, 4006.0), st)
    check("[8] 空仓镜像（跟踪层）→ reason = trailing",
          _chk_s.reason if _chk_s else None, "trailing")
    check("[8] reason 细分不改变 price 语义（触发价仍是保护价）",
          _chk_s.price if _chk_s else None, 4005.0)

    print("\n" + "=" * 60)
    print("P61 结果: {} passed, {} failed".format(_PASS, _FAIL))
    print("=" * 60)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

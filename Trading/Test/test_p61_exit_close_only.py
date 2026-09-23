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

  · **达标判据** —— 浮盈是否够 `breakeven_trigger_r×R` 进保本、够 `win_loss_ratio×R`
    进跟踪，以及跟踪锚"至今最好价"—— 读本根 K 线的**有利侧极值**：
    做多 `bar.high`、做空 `bar.low`。
    理由：它衡量的不是"能否成交"，而是"这一段行情最远走到过哪里"。用收盘价衡量会把
    「盘中冲高 1.5R、收盘回落到 0.3R」那根判成不达标，利润继续裸奔到 −1R。

  · **两步的顺序 = 先按极值抬保护价，再用本根收盘价判触发**（2026-09-22 · 用户拍板改版；
    旧顺序"触发判据在前、达标命中即 `only_update` 返回"已废弃）。理由：一根 K 线闭合时，
    "最远走到过哪里"与"收在哪里"都已成定局，没有理由把已经回落的区间拖到下一根 ——
    一根振幅过大的 K 线因此**当根**就能离场。
    ⚠️ 由此产生的**当根离场**是刻意接受的代价：达标用的极值可能远优于收盘价，而新保护价
    正是按极值算出来的 → 本根收盘若已落在**新**保护价的不利侧（high 到 +3R、跟踪价设在
    +2.5R、收盘回到 +2R），本根即判离场、以该根收盘价成交。代价是"冲高回落"形态里下车
    更早、更容易被一根长上影打掉；收益是不再承担"达标根收盘 → 下一根收盘"之间的漂移。
    两边优劣取决于达标根之后那根的收盘分布，**不是"更早锁利"这么单向**。
    本文件 [6]/[7] 两组钉死它：抬价后的保护价恒参与本根触发判定；达标根收盘落在新价
    **有利侧**时才走 `only_update`；两段判据的**先后**另由 [1] 组的 AST 行号断言钉死。

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
[5] 判"L3 是否启动"用的是计划里的阶段身份 `plan.params["_phase"]`，**不是** `only_update`
—— 时序改版后同一根可以"先抬价、再判出跌破"，启动的那几根本根就离场了
（`only_update=False`），拿 `only_update` 当启动判据会在新时序下整片假红。

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


def _line_of(fn_node, pred):
    """fn_node 内满足 pred 的节点里**最小**行号（找不到 → None）。

    用于"两段代码谁在前"的先后断言：AST 行号是 1-based、且随文件整体移动，
    所以比"绝对行号等于多少"稳健得多 —— 这里要钉的正是**相对顺序**本身。
    """
    if fn_node is None:
        return None
    lns = [getattr(n, "lineno", None) for n in ast.walk(fn_node) if pred(n)]
    lns = [x for x in lns if x is not None]
    return min(lns) if lns else None


def _close_trigger_line(fn_node):
    """触发比较（`close < stop` / `close > stop`）所在 If 的行号（找不到 → None）。

    识别方式 = If 的 test 里有一个 Compare，其操作数含名为 `close` 的局部变量 ——
    这正是"触发判据只读本根收盘价"在源码里的形状。
    """
    if fn_node is None:
        return None
    lns = []
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.If):
            continue
        for sub in ast.walk(node.test):
            if not isinstance(sub, ast.Compare):
                continue
            operands = [sub.left] + list(sub.comparators)
            if any(isinstance(x, ast.Name) and x.id == "close" for x in operands):
                lns.append(node.lineno)
    return min(lns) if lns else None



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

    # 两段判据的**先后**（2026-09-22 用户拍板改版）：L3 达标判定必须排在触发判定**之前**
    #   —— 顺序一反，"本根抬价后的保护价立即参与本根触发判定"就断了（当根离场失效）。
    #   行为样本只能证明"我构造的这几根走对了"，挡不住有人把两段调回去 → 这里比 AST 行号。
    _ln_ext = _line_of(chk, lambda n: isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "_fav_extreme")
    _ln_trig = _close_trigger_line(chk)
    check("[必现] 找得到 _fav_extreme() 调用行与触发比较行（先后断言的判据）",
          (isinstance(_ln_ext, int), isinstance(_ln_trig, int)), (True, True))
    check("[时序] L3 达标判定（_fav_extreme）排在触发判定（close 比较）之前",
          (_ln_ext is not None and _ln_trig is not None and _ln_ext < _ln_trig), True)

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
                                           stop_price=stop,
                                           params=params or {"R": 5.0}))

    pol = LayeredExitPolicy()
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
    pol3 = LayeredExitPolicy({"use_atr": False, 
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

    print("\n[5] L3 启动阈值仍严格 = win_loss_ratio（3R / 2R 两档边界，比较取严格 >）")
    # ⚠️ 本组把**收盘价固定在入场价**、只让 high 抬到指定倍数 —— 于是"能不能启动"
    #    完全由极值口径决定。若实现回头只看收盘价，2.99R / 3.0R / 3.01R 三条都会失真；
    #    若实现把比较改回 `>=`，则"恰好 3.0R 不启动"那条（严格不等）立刻变红。
    # ⚠️ 两个探针只代表**两个不同阈值档**，不绑定品种、也不代表档案当前取值。
    #    保留高倍数一档纯粹为"两个阈值各验一遍"（判别力来自阈值本身，不来自品种）。
    pol_r3 = LayeredExitPolicy({"use_atr": False, 
                                "breakeven_trigger_r": 99.0,
                                "breakeven_buffer_r": 0.0, "win_loss_ratio": 3.0})
    pol_r2 = LayeredExitPolicy({"use_atr": False, 
                                "breakeven_trigger_r": 99.0,
                                "breakeven_buffer_r": 0.0, "win_loss_ratio": 2.0})

    def _l3_started(pol, high, ts):
        """R=10、入场 100 的单根 bar：high 抬到 high → L3 是否启动。

        ⚠️ 判据是**计划里的阶段身份** `_phase`，不是 `only_update`：2026-09-22 时序改版后
        同一根可以"先按极值抬价、再按收盘价判出跌破"。本组把收盘价固定在入场价 100，而
        跟踪价（= high − trailing_trigger_r×R ≈ 125）必然在它上方 → "启动"的那几根本根
        就离场了（`only_update=False`）。用 `_phase` 判"这根有没有把保护价抬进 L3"，
        与"这根是否离场"解耦 —— 阈值边界才是本组真正要钉的东西。
        """
        p = Position(symbol="CFFEX.IF", side=Side.LONG, volume=1,
                     entry_price=100.0, entry_at="2026-09-01 09:40",
                     entry_bar_ts=1000, signal_key="k2", open_order_id="o2",
                     entry_bar_seq=1,
                     exit_plan=ExitPlan(name="LayeredExitPolicy", stop_price=90.0,
                                        params={"R": 10.0, "_trail_best": 100.0}))
        bar = Bar(timestamp=ts, date="2026-09-01 09:40", open=100.0,
                  high=high, low=95.0, close=100.0, vol=1)
        chk = pol.check(p, bar, st)
        return bool(chk is not None and chk.plan is not None
                    and chk.plan.params.get("_phase") == "trailing")

    check("阈值3R high 2.99R 不启动 L3（阈值严格 > 的下侧）",
          _l3_started(pol_r3, 129.9, 2710), False)
    check("阈值3R high 3.0R **恰好不启动**（严格不等：> 3R 才算）",
          _l3_started(pol_r3, 130.0, 2711), False)
    check("阈值3R high 3.01R 启动（越过阈值 1 tick 即算）",
          _l3_started(pol_r3, 130.1, 2712), True)
    check("阈值2R high 1.99R 不启动 L3", _l3_started(pol_r2, 119.9, 2713), False)
    check("阈值2R high 2.0R **恰好不启动**（严格不等）",
          _l3_started(pol_r2, 120.0, 2714), False)
    check("阈值2R high 2.01R 启动", _l3_started(pol_r2, 120.1, 2715), True)
    check("同一 2.5R：阈值3R 不启动", _l3_started(pol_r3, 125.0, 2716), False)
    check("同一 2.5R：阈值2R 已启动", _l3_started(pol_r2, 125.0, 2717), True)

    print("\n[6] 同一根：极值达标抬价 → 收盘跌破新价 → **当根离场**")
    # 一根 high 冲到 2R、收盘又砸回 −1R 之外的巨阴：新时序下先按极值把保护价抬到跟踪位
    #   （3995 → 保本 4005 → 跟踪 4020），再用本根收盘 3988 判出跌破 → 当根离场。
    #   ⚠️ 这条**替换**了旧的"触发优先（判 sl、不抬损）"语义：旧语义只成立于"触发判据排在
    #   达标之前"的时序，该时序已废弃（2026-09-22 拍板）。离场原因从此记的是"哪一层保护价
    #   被跌破"，而不是"本根极值最高到过哪、收盘又跌回哪里"。
    chk_both = LayeredExitPolicy(
        {"use_atr": False, "breakeven_trigger_r": 1.0,
         "breakeven_buffer_r": 0.5, "win_loss_ratio": 2.0}).check(
        _pos(params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2000, date="2026-09-01 09:40", open=4000.0,
            high=4025.0, low=3985.0, close=3988.0, vol=1), st)
    check("[6a] 同根达标 + 收盘跌破新保护价 → 当根离场，触发价 = 抬价后的保护价",
          (chk_both.reason, chk_both.price, chk_both.fill_price) if chk_both else None,
          ("trailing", 4020.0, 3988.0))
    check("[6b] 该结果不是 only_update（确实登场离场）",
          chk_both.only_update if chk_both else None, False)
    # [6c] 登场离场时 **plan 也必须带出**：引擎靠它落盘"保护价已抬到 4020"并补发阶段
    #      通知。若实现只在 only_update 路径给 plan，这条立即变红
    #      （引擎侧的"因进跟踪而离场"因果链会断）。
    check("[6c] 登场离场同时带出计划（新保护价 + 新层身份）",
          (chk_both.plan.stop_price,
           chk_both.plan.params.get("_phase")) if chk_both and chk_both.plan else None,
          (4020.0, "trailing"))

    print("\n[7] 时序：达标根**当根**即用新保护价判触发（先抬价、再判跌破）")
    # 2026-09-22 用户拍板改版：同一根 K 线先按有利侧极值抬高保护价，再用同一根收盘价判
    #   它有没有被跌破 —— 振幅过大的一根因此**当根**就能离场。旧实现（达标即 return、
    #   新保护价下一根才生效）已废弃；本组两条即那条时序的钉子：
    #     · 若有人把 L3 挪到触发判定之后 → [7a] 退回"只更新计划"（变红）；
    #     · 若有人让 L3 命中后仍只有**旧**保护价参与触发（新价本根不生效）→
    #       [7a] 的 price 会变成旧保护价 3990，断言变红。
    pol4 = LayeredExitPolicy({"use_atr": False,
                              "breakeven_trigger_r": 1.0,
                              "breakeven_buffer_r": 0.5,
                              "win_loss_ratio": 99.0})
    # 7a 达标根 close 落在**新**保护价不利侧：high=4015（> 1R）→ 保本价 3990 → 4005；
    #    本根 close=4001 < 4005 → **当根离场**，reason = 被跌破的那一层（breakeven）。
    chk_d0 = pol4.check(
        _pos(stop=3990.0, params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2000, date="2026-09-01 09:40", open=4000.0,
            high=4015.0, low=3991.0, close=4001.0, vol=1), st)
    check("[7a] 达标根 close 落在**新**保本价不利侧 → 当根离场（reason = 抬价后的层）",
          (chk_d0.reason, chk_d0.price, chk_d0.fill_price, chk_d0.only_update,
           chk_d0.plan.stop_price if chk_d0 and chk_d0.plan is not None else None)
          if chk_d0 else None, ("breakeven", 4005.0, 4001.0, False, 4005.0))

    # 7b 达标根 close 落在**新**保护价有利侧（4008 > 4005）→ 只更新计划、不离场。
    #    与 [7a] 成对：证明新时序**不是**"一达标就离场" —— 离场仍只由"收盘跌破保护价"决定。
    chk_d0b = pol4.check(
        _pos(stop=3990.0, params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2001, date="2026-09-01 09:41", open=4000.0,
            high=4015.0, low=3991.0, close=4008.0, vol=1), st)
    check("[7b] 达标但 close 在新保本价有利侧 → 只更新计划、当根不离场",
          (chk_d0b.reason, chk_d0b.only_update,
           chk_d0b.plan.stop_price if chk_d0b and chk_d0b.plan is not None else None)
          if chk_d0b else None, ("breakeven", True, 4005.0))
    check("[7b'] only_update 路径不带成交参考价（不得凭空给 fill_price）",
          chk_d0b.fill_price if chk_d0b else None, None)

    # 7c 未达标根（high 只到 4008 < 4010）→ 计划不动、也不触发（本根无任何返回）
    chk_none = pol4.check(
        _pos(stop=3990.0, params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2002, date="2026-09-01 09:42", open=4000.0,
            high=4008.0, low=3991.0, close=4001.0, vol=1), st)
    check("[7c] 未达标根：close 未破旧保护价 → 返回 None（既不抬价也不离场）",
          chk_none, None)

    # 7d 跨根路径未变：保护价已是 4005、本根未再抬价，close=4002 < 4005 → 照常离场。
    #    （reason 走计划快照：plan 无 `_phase` → "sl"，与本根是否抬价无关）
    chk_d1 = pol4.check(
        _pos(stop=4005.0, params={"R": 10.0, "_trail_best": 4015.0}),
        Bar(timestamp=2003, date="2026-09-01 09:43", open=4002.0,
            high=4006.0, low=3998.0, close=4002.0, vol=1), st)
    check("[7d] 后续根照常按既有保护价触发（跨根路径未变）",
          (chk_d1.reason, chk_d1.price, chk_d1.fill_price, chk_d1.only_update)
          if chk_d1 else None, ("sl", 4005.0, 4002.0, False))

    # 7e 空仓镜像：low=3985 达标 → 保本价 4010 → 3995；本根 close=3999 > 3995（不利侧）
    #    → 当根离场。多空两侧必须同口径（只改多不改空是最容易犯的漏改）。
    chk_s0 = pol4.check(
        _pos(Side.SHORT, 4000.0, 4010.0, params={"R": 10.0, "_trail_best": 4000.0}),
        Bar(timestamp=2004, date="2026-09-01 09:44", open=4000.0,
            high=4009.0, low=3985.0, close=3999.0, vol=1), st)
    check("[7e] 空仓镜像：达标根 close 在新保本价不利侧 → 当根离场（压到 3995）",
          (chk_s0.reason, chk_s0.price, chk_s0.fill_price, chk_s0.only_update)
          if chk_s0 else None, ("breakeven", 3995.0, 3999.0, False))

    print("\n[8] reason 细分：保护价被跌破时标的是**哪一层**（2026-09-22 拍板）")
    # reason 只表达"哪条规则触发的离场"，**不表达盈亏**（保本离场也可能被滑点打成净亏，
    #   盈亏一律由 net_cash 符号在统计侧分 —— 见 Infra/TradeStats.py 的 by_reason 说明）。
    #   判定依据 = `plan.params["_phase"]`（L3 在保护价真被抬高时写入），它与"最后把保护价
    #   抬上去的那一层"恒同源 → 标的必然就是被跌破的那条线。
    #   本组把 `_phase` **直接注入计划参数**（不靠 L3 真跑出来）——
    #   这恰好也在证明"reason 路由只读计划快照"：也覆盖旧 state.db
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

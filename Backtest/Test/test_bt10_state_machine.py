# -*- coding: utf-8 -*-
"""两态状态机 + 「唯一改写点」护栏（v1.19）。

为什么要有这个组件
---------------------------------------------------------------------
`Backtest/State.py` 是设计文档 §5.3 定案的**两态机**，它的全部价值写在
`next_state()` 的 docstring 里：「不可能的状态转移不可能表达」。但这个价值
只在这条路径成立时才是真的 —— **`Runner` 必须走它**。

v1.18 及以前的实情：`Runner.py` 里是 `state = State.FLAT` / `state = State.IN_TRADE`
两处**直接赋值**，`next_state()` 全仓零调用（唯一同名出现在 `.venv/pygments`）。
于是 docstring 里"（**唯一**改写点）"是假的，三个 `raise ValueError` 分支一次
都没执行过 —— 一个从没跑过的防守分支，等于没有。2026-10-05 的审核把这层
"文档说唯一、代码没接线"点了出来（v1.18 审核结论 §4.2）。

本组件钉三件事
---------------------------------------------------------------------
  ① **真值表穷举**：2 态 × entry × exit_ = 8 组合全走一遍，4 合法（断言终态）
     + 4 非法（断言 `ValueError`，且**只**接 `ValueError` —— 换成别的异常同样是错）。
  ② **唯一改写点**（AST，不是 grep）：`Backtest/Runner.py` 里
        · `state = State.<X>` 直接赋值**恰好 1 处**（循环前的初始化）；
        · `state = next_state(...)` 调用**恰好 2 处**（两个转移点），行号都晚于初始化。
     判据写在 `_scan()` 里一份，真文件与 ④ 的自证样本共用。
  ③ **判别力自证**：把真实源码在内存里改回旧写法（两处直接赋值），`_scan()` 必须
     报出 3 处直赋值 / 0 次调用。没有这一步，"断言恒真"和"护栏有效"分不开。

为什么不做成冻结快照：这里没有"数值基线"，全是结构与集合关系；真要证"接线没改
行为"，那是 `bt01`（6 笔逐笔）/`bt06`（五周期）/`bt08`（A/B 两臂）/`bt09` 的活 ——
它们都对着冻结基线比对。接线若改变结果，那四个组件会同时红。

用法：python Backtest/Test/test_bt10_state_machine.py（退出码 0 = 通过）
"""
import ast
import io
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from Backtest.State import State, next_state              # noqa: E402

_passed = 0
_failed = 0


def check(name, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print("  \u2713 %s" % name)
    else:
        _failed += 1
        print("  \u2717 %s -> got %r, want %r" % (name, got, want))


# ══════════════════════════════════════════════════════════════════════
# 尺子（单一来源：真实文件与 ④ 的自证样本共用同一份判据）
# ══════════════════════════════════════════════════════════════════════
def _scan(src):
    """→ (直接赋值点 [(行号, 枚举名)], `next_state(...)` 调用行号列表)。

    为什么用 AST 不用 grep：`state = State.FLAT` 这串字也出现在注释与 docstring
    里（`State.py`、`Runner.py`、本文件都在写它），grep 会把**文档**当代码数 ——
    而这次要判的恰恰就是"文档说的和代码做的是否一致"。
    只认「`state` ← `State.<属性>`」这一种赋值形态；`state = next_state(...)`
    （Call，不是 Attribute）天然不在其中 ⇒ "接线"与"直赋值"自动分开，无需额外规则。
    """
    tree = ast.parse(src)
    direct, call_lines = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            v = node.value
            if (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
                    and v.value.id == "State"):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "state":
                        direct.append((node.lineno, v.attr))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "next_state"):
            call_lines.append(node.lineno)
    return direct, call_lines


RUNNER = os.path.join(ROOT, "Backtest", "Runner.py")


# ══════════════════════════════════════════════════════════════════════
# ① 真值表穷举（8 组合：4 合法 + 4 非法）
# ══════════════════════════════════════════════════════════════════════
print("\n[1] next_state 真值表（2 态 × entry × exit_ = 8 组合，穷举）")
LEGAL = {
    (State.FLAT, False, False): State.FLAT,
    (State.FLAT, True, False): State.IN_TRADE,
    (State.IN_TRADE, False, False): State.IN_TRADE,
    (State.IN_TRADE, False, True): State.FLAT,
}
ILLEGAL = [
    (State.FLAT, False, True),       # 空仓不可能触发出场
    (State.IN_TRADE, True, False),   # 持仓期不得开新仓（Q13）
    (State.FLAT, True, True),        # 同根既开又平
    (State.IN_TRADE, True, True),    # 同上
]
_ok_cases, _err_cases = [], []
for _cur in (State.FLAT, State.IN_TRADE):
    for _e in (False, True):
        for _x in (False, True):
            try:
                _got = next_state(_cur, entry=_e, exit_=_x)
            except Exception as _exc:                       # noqa: BLE001
                _err_cases.append(((_cur, _e, _x), type(_exc).__name__))
            else:
                _ok_cases.append(((_cur, _e, _x), _got))

check("合法组合恰好 4 个（列表逐项等于真值表）",
      sorted((k, str(v)) for k, v in _ok_cases),
      sorted((k, str(v)) for k, v in LEGAL.items()))
check("非法组合恰好 4 个（列表逐项等于反例表）",
      sorted(k for k, _ in _err_cases), sorted(ILLEGAL))
check("非法组合一律抛 ValueError（不是别的异常，也不静默兜底）",
      sorted({t for _, t in _err_cases}), ["ValueError"])

# ══════════════════════════════════════════════════════════════════════
# ② 唯一改写点（AST 扫真实 Runner.py）
# ══════════════════════════════════════════════════════════════════════
print("\n[2] 唯一改写点：Runner.py 的状态只能经 next_state() 改")
with io.open(RUNNER, encoding="utf-8") as f:
    runner_src = f.read()
direct, call_lines = _scan(runner_src)

check("`state = State.<X>` 直接赋值恰好 1 处，且为 FLAT 初始化",
      (len(direct), direct[0][1] if len(direct) == 1 else None), (1, "FLAT"))
check("`state = next_state(...)` 恰好 2 处（出场转移 + 入场转移）",
      len(call_lines), 2)
check("两处转移的行号都晚于初始化（⇒ 初始化在循环外，循环内没有漏网的直赋值）",
      len(direct) == 1 and all(ln > direct[0][0] for ln in call_lines), True)

# ══════════════════════════════════════════════════════════════════════
# ③ 状态枚举本身：两态、穷举、可打印
# ══════════════════════════════════════════════════════════════════════
print("\n[3] State 枚举（两态，穷举）")
check("成员恰好 2 个且按定义序", [m.name for m in State], ["FLAT", "IN_TRADE"])
check("值 = 落盘/打印用的字符串（继承 str）",
      [m.value for m in State], ["flat", "in_trade"])
check("`state is State.FLAT` 式身份比较可用（枚举成员唯一）",
      State("flat") is State.FLAT, True)

# ══════════════════════════════════════════════════════════════════════
# ④ 判别力自证：把源码改回旧写法，尺子必须响
# ══════════════════════════════════════════════════════════════════════
print("\n[4] 判别力自证（正反两向）")
_old = runner_src.replace("state = next_state(state, exit_=True)", "state = State.FLAT")
_old = _old.replace("state = next_state(state, entry=True)", "state = State.IN_TRADE")
check("反例确实被改写（不是改了个寂寞）", _old != runner_src and
      "state = next_state(" not in _old, True)
_d_old, _c_old = _scan(_old)
check("反例（改回两处直接赋值）⇒ 尺子报 (3 处直赋值, 0 次调用)",
      (len(_d_old), len(_c_old)), (3, 0))
_fake = ("state = State.FLAT\n"
         "state = next_state(state, entry=True)\n"
         "state = next_state(state, exit_=True)\n")
check("正例（合成源码：1 直赋值 + 2 调用）⇒ 尺子读数 (1, 2)",
      (len(_scan(_fake)[0]), len(_scan(_fake)[1])), (1, 2))
check("尺子只看赋值形态：注释 / 字符串里的 `state = State.FLAT` 不计入",
      _scan("# state = State.FLAT\nx = 'state = State.IN_TRADE'\n"), ([], []))

print("\n" + "=" * 60)
print("bt10_state_machine: {} passed, {} failed".format(_passed, _failed))
print("=" * 60)
sys.exit(1 if _failed else 0)

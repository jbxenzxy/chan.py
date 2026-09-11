# -*- coding: utf-8 -*-
"""
P32 净敞口 SSOT 契约测试（Phase 7 新增，2026-09-11）
================================================================
背景（这条契约替换了什么）
--------------------------
改造前，账户三态判定式在 Engine 里**内联重复 4 处**，而且判据是"持仓来源标记"
（`Position.origin` / `PositionOrigin.SOFT_EXIT_LOCK`）：

    _restore            : is_empty() or all(SOFT_EXIT_LOCK)   → IDLE
    on_signal           : any(not SOFT_EXIT_LOCK)             → 运行态，忽略信号
    _close_positions 尾 : is_empty() or all(SOFT_EXIT_LOCK)   → IDLE
    _unlock_position 尾 : any(not SOFT_EXIT_LOCK)             → IN_TRADE else IDLE

改造后（需求 ⑴）判据只剩一个**可观测的数值**：净敞口。

    FLAT    : 簿内无仓单
    LOCKED  : 簿非空 且 净敞口 == 0
    RUNNING : 净敞口 != 0

`PositionOrigin` 整个枚举连同 `Position.lock_pair_id` 一起删除，所以
"三态还能不能算错"这件事**不再靠人肉维持口径一致**。

⚠️ 口径变化（有意，不是纯重构）
------------------------------
旧 `LOCKED` = 簿非空且**每一笔**都是 SOFT_EXIT_LOCK —— **单笔** SOFT_EXIT_LOCK
也算 LOCKED（锁一层）。新 `LOCKED` = 簿非空且 net == 0。

因此**单笔持仓必然是 RUNNING**（它带着敞口）。这不是等价改写，是有意的收窄：
旧口径下"锁一层"既不是空仓也不是完全敞口，落在 LOCKED 里；新口径下它只是
一个净敞口非 0 的运行段。本测试 [1c] 把这条差异**正面钉死**，防止有人
按"新旧等价"的直觉把它改回去。

四层护栏
--------
  [1] 判定正确性：7 种簿面 → FLAT / LOCKED / RUNNING
  [2] 源码契约：`account_state()` 是全仓**唯一**返回 AccountState 的地方
      （AST 级：任何 `return ... AccountState.X` 必须落在 account_state 方法内）
  [3] 判据唯一：`net_volume()` 是唯一原始量；来源标记类符号在生产代码中归零
  [4] 穷举不变量：16 种多空笔数组合下，`account_state()` 与独立算出的
      净敞口谓词**逐一相等**
  [5] 对外口径：`auto_order_status()` 暴露 account_state + net_volume，
      且 `_state` 是它的派生镜像（不是第二个独立状态机）

跑法：PYTHONPATH=<repo root> python Trading/Test/test_p32_net_exposure_ssot.py
"""
from __future__ import annotations

import ast
import inspect
import io
import os
import re
import shutil
import sys
import tempfile
import textwrap
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
_ROOT = os.path.dirname(_TG_ROOT)               # 仓库根
sys.path.insert(0, _ROOT)

from Trading import Broker  # noqa: E402  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Engine.PositionBook import PositionBook  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, EngineState, ExitPlan, Position, Side,
)
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("  ✓ " if ok else "  ✗ ") + name
          + ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % detail if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p32_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_cfg():
    import copy
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"]["max_volume"] = 2
    base["exit_params"].update({"use_atr": False, "min_r_points": 3.0})
    return TradingConfig.from_dict(base)


def make_pos(side=Side.LONG, vol=2, entry_price=4500.0, key="P32",
             entry_bar_seq=1, entry_date="2026-09-02"):
    """构造一笔仓单。新 schema 已无 origin / lock_pair_id —— 本测试不传（也不该传）。"""
    return Position(
        symbol="CFFEX.IF2609", side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:00",
        entry_bar_ts=entry_bar_seq * 1000, signal_key=key, open_order_id="p32-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_bar_seq=entry_bar_seq, entry_date=entry_date)


def build_engine(tmpdir, tag="a"):
    spec = InstrumentSpec()
    return TradingEngine(
        make_cfg(), DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        DefaultEntryPolicy({}), LayeredExitPolicy(),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag), echo=False, echo_kinds=None))


def fill(eng, spec):
    """spec = [Side, ...] 逐笔加入。"""
    eng.positions.clear()
    for i, sd in enumerate(spec):
        eng.positions.add(make_pos(side=sd, key="F%d" % i, entry_bar_seq=i + 1))


def net_predicate(eng) -> str:
    """**独立**算出的三态谓词（不复用 Engine.account_state 的实现）。"""
    poss = eng.positions.positions
    if not poss:
        return "flat"
    net = sum(p.volume * p.side.sign for p in poss)
    return "locked" if net == 0 else "running"


# ════════════════════════════════════════════════════════════════
print("\n[1] 判定正确性：三种簿面 → FLAT / LOCKED / RUNNING")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c1")
    check("[1a] 空簿 → FLAT", eng.account_state(), AccountState.FLAT)
    check("[1a] 空簿 net_volume == 0", eng.positions.net_volume(), 0)

    fill(eng, [Side.LONG])
    check("[1b] 单笔多头（net=+2）→ RUNNING",
          eng.account_state(), AccountState.RUNNING)
    check("[1b] net_volume == +2", eng.positions.net_volume(), 2)

    # ★ 口径收窄的正面钉死：旧实现把"单笔 SOFT_EXIT_LOCK"判成 LOCKED（锁一层），
    #   新实现只看净敞口 —— 单笔必然带敞口 → 必是 RUNNING。
    check_true("[1c] 单笔持仓**不得**被判为 LOCKED（新口径：单笔敞口就是运行态）",
               eng.account_state() is not AccountState.LOCKED,
               "got=%s" % eng.account_state().value)

    fill(eng, [Side.LONG, Side.SHORT])
    check("[1d] 一多一空等量（net=0）→ LOCKED",
          eng.account_state(), AccountState.LOCKED)
    check("[1d] net_volume == 0", eng.positions.net_volume(), 0)

    fill(eng, [Side.LONG, Side.SHORT, Side.LONG, Side.SHORT])
    check("[1e] 两多两空等量（net=0）→ LOCKED",
          eng.account_state(), AccountState.LOCKED)
    check("[1e] 4 笔仍 LOCKED（笔数不影响判定）", len(eng.positions), 4)

    fill(eng, [Side.LONG, Side.SHORT, Side.LONG])
    check("[1f] 多空不等量（net=+2）→ RUNNING",
          eng.account_state(), AccountState.RUNNING)

    fill(eng, [Side.SHORT, Side.SHORT, Side.LONG])
    check("[1g] 净敞口为负（net=-2）→ RUNNING",
          eng.account_state(), AccountState.RUNNING)
    check("[1g] net_volume == -2", eng.positions.net_volume(), -2)

# 昨日仓与今日仓对三态无影响（三态不涉及日期，日期只影响"怎么离场"）
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c2")
    eng.positions.add(make_pos(side=Side.LONG, key="OLD", entry_date="2026-08-31"))
    eng.positions.add(make_pos(side=Side.SHORT, key="OLD2", entry_date="2026-08-31"))
    check("[1h] 昨日双向等量 → LOCKED（三态与建仓日期无关）",
          eng.account_state(), AccountState.LOCKED)


# ════════════════════════════════════════════════════════════════
print("\n[2] 源码契约：account_state() 是全仓唯一返回三态的地方")
# ════════════════════════════════════════════════════════════════
# [2a] 全树只有一处定义
_defs = []
for dp, dns, fns in os.walk(_TG_ROOT):
    dns[:] = [d for d in dns if d not in ("__pycache__",)]
    for fn in fns:
        if not fn.endswith(".py"):
            continue
        fp = os.path.join(dp, fn)
        try:
            txt = io.open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for i, ln in enumerate(txt.splitlines(), 1):
            if re.search(r"^\s*def\s+account_state\b", ln):
                _defs.append("%s:%d" % (os.path.relpath(fp, _ROOT).replace("\\", "/"), i))
check("[2a] `def account_state` 全树仅 1 处", len(_defs), 1)
if len(_defs) != 1:
    print("      命中：%s" % _defs)

# [2b] AST：任何"把 AccountState.X 当值返回"的 return 必须落在 account_state 内
_src = textwrap.dedent(inspect.getsource(TradingEngine))
_tree = ast.parse(_src)
_ret_sites = []


def _mentions_account_state(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.value.id == "AccountState":
            return True
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self):
        self.stack = []

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Return(self, node):
        if node.value is not None and _mentions_account_state(node.value):
            _ret_sites.append(".".join(self.stack[-2:]) or "?")
        self.generic_visit(node)


_Visitor().visit(_tree)
check("[2b] `return AccountState.X` 的出现次数 == 3（RUNNING / FLAT / LOCKED）",
      len(_ret_sites), 3)
check("[2b] 全部落在 account_state 方法内",
      sorted(set(_ret_sites)), ["TradingEngine.account_state"])

# [2c] 判据是净敞口，不是"来源标记"
_acct_src = inspect.getsource(TradingEngine.account_state)
check_true("[2c] account_state 用 net_volume() 作判据", "net_volume()" in _acct_src)
check_true("[2c] account_state 用 is_empty() 区分 FLAT / LOCKED",
           "is_empty()" in _acct_src)
check_true("[2c] account_state 不再引用来源标记概念",
           not re.search(r"origin|SOFT_EXIT_LOCK|lock_pair", _acct_src))

# [2d] 三态**不再**有第二个内联实现：生产代码不得出现旧判定式
#      （只扫生产目录 —— 本条的语义是"实现里没有第二份判定"，测试文件里
#       残留的旧断言由各自的重写负责清掉，不属于"实现分叉"）
_PROD_DIRS = ("Trading/Engine", "Trading/Infra", "Trading/Strategy",
              "Trading/Broker", "Trading/Risk", "Trading/Source", "Trading/Tool")
_old_pats = [
    (r"all\(\s*p\.origin\s+is\s+PositionOrigin", "all(origin is SOFT_EXIT_LOCK)"),
    (r"any\(\s*p\.origin\s+is\s+not\s+PositionOrigin", "any(origin is not SOFT_EXIT_LOCK)"),
    (r"p\.origin\s+is\s+PositionOrigin", "p.origin is PositionOrigin"),
]
for pat, label in _old_pats:
    hits = []
    for sub in _PROD_DIRS:
        base = os.path.join(_ROOT, sub)
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in ("__pycache__",)]
            for fn in fns:
                if fn.endswith(".py"):
                    fp = os.path.join(dp, fn)
                    try:
                        txt = io.open(fp, encoding="utf-8", errors="replace").read()
                    except OSError:
                        continue
                    if re.search(pat, txt):
                        hits.append(os.path.relpath(fp, _ROOT).replace("\\", "/"))
    check("[2d] 生产代码中旧内联判定式 %s 已归零" % label, hits, [])


# ════════════════════════════════════════════════════════════════
print("\n[3] 判据唯一：net_volume() 是唯一原始量；来源标记类符号在生产代码归零")
# ════════════════════════════════════════════════════════════════
_pb = inspect.getsource(PositionBook)
check_true("[3a] PositionBook 提供 net_volume()",
           re.search(r"def net_volume\(", _pb) is not None)
check_true("[3b] PositionBook 提供 is_empty()",
           re.search(r"def is_empty\(", _pb) is not None)

# 生产代码（不含 Test）中，已删概念不得作为活标识符出现
_prod_hits = []
for sub in ("Trading/Engine", "Trading/Infra", "Trading/Strategy",
            "Trading/Broker", "Trading/Risk", "Trading/Source", "Trading/Tool"):
    base = os.path.join(_ROOT, sub)
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in ("__pycache__", "Test")]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dp, fn)
            rel = os.path.relpath(fp, _ROOT).replace("\\", "/")
            if rel.endswith("Trading/Infra/Types.py") or rel.endswith("Engine/Engine.py"):
                continue          # 这两个文件里有"已删除"说明注释与 legacy 键清单
            txt = io.open(fp, encoding="utf-8", errors="replace").read()
            for w in ("PositionOrigin", "ExitMode", "SOFT_EXIT_LOCK"):
                if w in txt:
                    _prod_hits.append("%s[%s]" % (rel, w))
check("[3c] 生产代码（除 Types/Engine 外）无 PositionOrigin/ExitMode/SOFT_EXIT_LOCK",
      _prod_hits, [])

# Engine 里这三者只允许出现在"已删除说明"注释与 legacy 键清单中
_eng = io.open(os.path.join(_TG_ROOT, "Engine", "Engine.py"),
               encoding="utf-8", errors="replace").read()
_eng_lines = [l for l in _eng.splitlines()
              if ("PositionOrigin" in l or "ExitMode" in l or "SOFT_EXIT_LOCK" in l)]
check_true("[3d] Engine 内命中行全部是注释或 legacy 键清单（≤2 行）",
           len(_eng_lines) <= 2, "n=%d" % len(_eng_lines))
for _l in _eng_lines:
    check_true("[3d] 命中行可解释: %s" % _l.strip()[:60],
               _l.strip().startswith("#") or "_LEGACY_POSITION_KEYS" in _l)


# ════════════════════════════════════════════════════════════════
print("\n[4] 穷举不变量：16 种多空笔数组合下 判定 == 独立净敞口谓词")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c4")
    bad = []
    for nl in range(4):
        for ns in range(4):
            spec = [Side.LONG] * nl + [Side.SHORT] * ns
            fill(eng, spec)
            got = eng.account_state().value
            want = net_predicate(eng)
            if got != want:
                bad.append((nl, ns, got, want))
    check("[4a] 16 种组合全部一致（0 个反例）", bad, [])

    # 元护栏：谓词本身不是恒等函数 —— 三种结果都真实出现过
    seen = set()
    for nl in range(4):
        for ns in range(4):
            fill(eng, [Side.LONG] * nl + [Side.SHORT] * ns)
            seen.add(net_predicate(eng))
    check("[4b] 元护栏：独立谓词确实产出全部 3 种结果", sorted(seen),
          ["flat", "locked", "running"])


# ════════════════════════════════════════════════════════════════
print("\n[5] 对外口径 + _state 只是派生镜像")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c5")
    st = eng.auto_order_status()
    check("[5a] 快照暴露 account_state（FLAT）", st.get("account_state"), "flat")
    check("[5a] 快照暴露 net_volume == 0", st.get("net_volume"), 0)

    fill(eng, [Side.LONG, Side.SHORT])
    st = eng.auto_order_status()
    check("[5b] 快照 account_state=locked", st.get("account_state"), "locked")
    check("[5b] 快照 net_volume == 0", st.get("net_volume"), 0)

    fill(eng, [Side.LONG])
    st = eng.auto_order_status()
    check("[5c] 快照 account_state=running", st.get("account_state"), "running")
    check("[5c] 快照 net_volume == +2", st.get("net_volume"), 2)

# _sync_state：_state 是 account_state() 的投影，不是第二个状态机
_sync = inspect.getsource(TradingEngine._sync_state)
check_true("[5d] _sync_state 读 account_state()", "account_state()" in _sync)
check_true("[5e] _sync_state 只产出 IDLE / IN_TRADE 两种镜像值",
           "EngineState.IN_TRADE" in _sync and "EngineState.IDLE" in _sync)
check_true("[5f] _sync_state 不再自行判净敞口（不直接读 positions）",
           "net_volume" not in _sync and "is_empty" not in _sync)

# 行为层验证：三种簿面下 _state 恒与三态一致
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c6")
    cases = [([], EngineState.IDLE),
             ([Side.LONG, Side.SHORT], EngineState.IDLE),
             ([Side.LONG], EngineState.IN_TRADE)]
    all_ok = True
    for spec, want in cases:
        fill(eng, spec)
        eng._sync_state()
        if eng._state is not want:
            all_ok = False
    check("[5g] 三态 → _state 投影正确（LOCKED/FLAT→IDLE，RUNNING→IN_TRADE）",
          all_ok, True)

# AccountState 枚举本身是 3 值
check("[5h] AccountState 恰有 3 个值", sorted(e.value for e in AccountState),
      ["flat", "locked", "running"])

print("\n" + "=" * 60)
print("P32 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

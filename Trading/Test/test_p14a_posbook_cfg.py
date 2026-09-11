# -*- coding: utf-8 -*-
"""
P14a PositionBook 容量语义 + 已删配置键的处置（Phase 7 重写，2026-09-11）
====================================================================
本文件原来测的是「`cfg.risk.max_open_positions` 配置化 + 容器多仓支持」。该配置项
已在 Phase 1-4 **整体删除**（D2），原因是它是 Phase E1 引入 PositionBook 时为
"让现存测试零行为变化"钉出来的纯迁移脚手架，钉住容量 = 1；而在新模型里它会造成
**静默挡单**：容器满了 `add` 抛错 → 信号被吞 → 账户停摆且没有出口。

D2 的结论：**删除该字段**（不是改成 None），资金是唯一闸门
（`Engine` 的设计意图：钱不够自然开不成功，CTP 会拒单，拒单有告警）。

所以本文件改成两件事：
  A. `PositionBook` 的**容量语义本身**仍然保留并要正确（显式传 max 时才生效）；
  B. 已删的两个配置键（`max_open_positions` / `unlock_no_new_open`）必须按
     **白名单静默丢弃**（D17）—— 老配置文件不至于让引擎起不来，但也不能
     把"丢弃白名单"做成"放行任意未知键"（那会让 RiskConfig 的严格模式失效）。

覆盖
----
  [1] 容量 API：DEFAULT_MAX=None / set_max / max_positions
  [2] add 语义：不限容量时可自由叠加；显式上限则"满了抛错"（不静默丢弃/合并）
  [3] legacy_single / set_legacy 的单仓守护
  [4] replace_with：仅在设了上限时截断，并记入 truncated_on_restore
  [5] 配置层：两个旧键已删 + 白名单丢弃（D17）+ 严格模式仍然生效
  [6] 引擎侧：容器不受配置约束（max is None）+ 源码不再读 cfg.risk.max_open_positions
  [7] 端到端冒烟：默认配置下一开一平

跑法：PYTHONPATH=<repo root> python Trading/Test/test_p14a_posbook_cfg.py
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import shutil
import sys
import tempfile
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
sys.path.insert(0, os.path.dirname(_TG_ROOT))


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p14a_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, RiskConfig, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Engine.PositionBook import PositionBook, PositionBookError  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, Bar, ExitPlan, OrderIntent, Position, Side, Signal,
)
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, actual, expected):
    global _PASS, _FAIL
    ok = actual == expected
    if ok:
        _PASS += 1
        print("  ✓ {}".format(name))
    else:
        _FAIL += 1
        print("  ✗ {}  -> got={!r} expected={!r}".format(name, actual, expected))


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % detail if detail else ""), bool(cond), True)


def check_raises(name, fn, exc_type=PositionBookError):
    global _PASS, _FAIL
    try:
        fn()
        _FAIL += 1
        print("  ✗ {}  -> 未抛异常（期望 {}）".format(name, exc_type.__name__))
    except exc_type:
        _PASS += 1
        print("  ✓ {}".format(name))
    except Exception as e:                                   # noqa: BLE001
        _FAIL += 1
        print("  ✗ {}  -> got {} (期望 {})".format(
            name, type(e).__name__, exc_type.__name__))


def make_pos(side, entry_price=4500.0, vol=1, signal_key="test_key", seq=10):
    return Position(
        symbol="CFFEX.IF2609", side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:30:00",
        entry_bar_seq=seq, entry_bar_ts=4000,
        signal_key=signal_key, open_order_id="dry_run-test",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 5.0,
                           tp_price=None, params={}),
    )


def make_engine(tmpdir, tag="a", broker=None):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    return TradingEngine(
        cfg, broker, DefaultEntryPolicy({"reverse_on_opposite_signal": False}),
        LayeredExitPolicy(),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag), echo=False,
                 echo_kinds=None))


def make_bar(ts=5000, close=4550.0, date="2026-09-01 09:30"):
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=ts, vol=0)


# ════════════════════════════════════════════════════════════════
print("\n[1] 容量 API：DEFAULT_MAX=None / set_max / max_positions")
# ════════════════════════════════════════════════════════════════
check("[1a] PositionBook.DEFAULT_MAX is None（D2：笔数上限已删）",
      PositionBook.DEFAULT_MAX, None)
check("[1b] 无参构造 → max_positions is None",
      PositionBook().max_positions, None)
check("[1c] 显式 max_positions=3 → 3",
      PositionBook(max_positions=3).max_positions, 3)
check_raises("[1d] max_positions=0 非法（必须 >= 1）",
             lambda: PositionBook(max_positions=0))

b = PositionBook()
b.set_max(5)
check("[1e] set_max(5) → 5", b.max_positions, 5)
b.set_max(None)
check("[1f] set_max(None) → 不限容量", b.max_positions, None)
check_raises("[1g] 不允许把上限缩到现存笔数以下",
             lambda: (lambda bb: (bb.add(make_pos(Side.LONG)),
                                  bb.add(make_pos(Side.SHORT)),
                                  bb.set_max(1)))(PositionBook()))


# ════════════════════════════════════════════════════════════════
print("\n[2] add 语义：不限容量可自由叠加；有上限则满了抛错")
# ════════════════════════════════════════════════════════════════
b2 = PositionBook()                      # 默认不限容量
for i in range(4):
    b2.add(make_pos(Side.LONG if i % 2 == 0 else Side.SHORT, signal_key="P%d" % i))
check("[2a] 不限容量：4 笔（第 1 日 2 锁 = 4 笔）全部入簿，无静默挡单",
      len(b2), 4)
check("[2b] FIFO 顺序 = 添加顺序",
      [p.signal_key for p in b2.positions], ["P0", "P1", "P2", "P3"])
check("[2c] 不限容量下 net 正确", b2.net_volume(), 0)

b3 = PositionBook(max_positions=1)
b3.add(make_pos(Side.LONG, signal_key="only"))
check_raises("[2d] 上限 1：第 2 笔抛 PositionBookError（不静默丢弃）",
             lambda: b3.add(make_pos(Side.SHORT)))
check("[2e] 抛错后簿面未被破坏（仍是 1 笔）", len(b3), 1)


# ════════════════════════════════════════════════════════════════
print("\n[3] legacy_single / set_legacy 的单仓守护")
# ════════════════════════════════════════════════════════════════
b4 = PositionBook()
check("[3a] 空簿 legacy_single() → None", b4.legacy_single(), None)
b4.add(make_pos(Side.LONG, signal_key="solo"))
check("[3b] 1 笔 → 返回该笔", b4.legacy_single().signal_key, "solo")
b4.add(make_pos(Side.SHORT, signal_key="second"))
check_raises("[3c] 多笔 → 抛 PositionBookError（禁止单仓 API 操作多仓）",
             lambda: b4.legacy_single())

b5 = PositionBook()
b5.add(make_pos(Side.LONG, signal_key="old"))
b5.set_legacy(make_pos(Side.SHORT, signal_key="new"))
check("[3d] set_legacy 整簿替换（不留旧仓）",
      [p.signal_key for p in b5.positions], ["new"])
b5.set_legacy(None)
check("[3e] set_legacy(None) → 清空", len(b5), 0)


# ════════════════════════════════════════════════════════════════
print("\n[4] replace_with：仅在设了上限时截断，截断内容可查")
# ════════════════════════════════════════════════════════════════
src = PositionBook()
for i in range(3):
    src.add(make_pos(Side.LONG, signal_key="R%d" % i))

capped = PositionBook(max_positions=2)
capped.replace_with(src)
check("[4a] 上限 2 + 持久化 3 笔 → 保留 2 笔", len(capped), 2)
check("[4b] 被截断的 1 笔可查（供 _restore 写 warning）",
      [p.signal_key for p in capped.truncated_on_restore], ["R2"])

free = PositionBook()                     # 默认不限容量
free.replace_with(src)
check("[4c] 默认不限容量 → 3 笔全保留，不截断", len(free), 3)
check("[4d] 未截断时 truncated_on_restore 为空", free.truncated_on_restore, [])

check("[4e] from_dict 接受 max_positions 并写入容量字段",
      PositionBook.from_dict(src.to_dict(), max_positions=2).max_positions, 2)
# 已知不一致（不可达路径，如实记录）：from_dict 直接 append 进内部 list、绕过 add，
# 所以**不做截断**。引擎的恢复路径已不再传上限（D2），因此这条在线上跑不到；
# 保留断言是为了防止有人误以为"from_dict 会截断"。
check("[4e2] from_dict 不截断（绕过 add）；引擎不传上限故不可达",
      len(PositionBook.from_dict(src.to_dict(), max_positions=2)), 3)


# ════════════════════════════════════════════════════════════════
print("\n[5] 配置层：两个旧键已删（D2）+ 白名单静默丢弃（D17）")
# ════════════════════════════════════════════════════════════════
check("[5a] RiskConfig 已无 max_open_positions 字段",
      "max_open_positions" in RiskConfig.model_fields, False)
check("[5b] RiskConfig 已无 unlock_no_new_open 字段",
      "unlock_no_new_open" in RiskConfig.model_fields, False)

legacy = RiskConfig(**{"max_open_positions": 3, "unlock_no_new_open": True,
                       "max_volume": 2})
check("[5c] 带旧键构造不报错（老配置文件仍可用）", legacy.max_volume, 2)
check_true("[5d] 旧键被记入 dropped_legacy_keys 白名单",
           "max_open_positions" in RiskConfig.dropped_legacy_keys
           and "unlock_no_new_open" in RiskConfig.dropped_legacy_keys,
           "got=%s" % RiskConfig.dropped_legacy_keys)

cfg0 = TradingConfig.from_dict(DEFAULT_CONFIG)
check("[5e] DEFAULT_CONFIG.risk 不含旧键（磁盘上也没有）",
      ("max_open_positions" in (DEFAULT_CONFIG.get("risk") or {}),
       "unlock_no_new_open" in (DEFAULT_CONFIG.get("risk") or {})), (False, False))
check("[5f] 从 DEFAULT_CONFIG 构造后 risk.max_volume 正常", cfg0.risk.max_volume, 2)

# ★ 关键护栏：白名单丢弃 ≠ 放行任意未知键（否则 RiskConfig 的严格模式失效）
check_raises("[5g] 严格模式仍然生效：真正的未知键必须报错",
             lambda: RiskConfig(bogus_key=1), Exception)
check("[5h] 元护栏：丢弃白名单确实非空（否则 [5c] 是假绿）",
      len(RiskConfig.dropped_legacy_keys) >= 2, True)


# ════════════════════════════════════════════════════════════════
print("\n[6] 引擎侧：容器不受配置约束 + 源码不再读 cfg.risk.max_open_positions")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as td:
    eng = make_engine(td, "c6")
    check("[6a] 引擎容器 max_positions is None（不限容量）",
          eng.positions.max_positions, None)
    check("[6b] 一笔手数来自 cfg.risk.max_volume（唯一还在用的 risk 字段）",
          eng.lots_per_signal, cfg0.risk.max_volume)

_esrc = inspect.getsource(TradingEngine.__init__)
check_true("[6c] Engine.__init__ 不再读 max_open_positions",
           "max_open_positions" not in _esrc)
check_true("[6d] Engine.__init__ 不再把 cfg 上限传给 PositionBook",
           "PositionBook(" not in _esrc or "max_positions=" not in _esrc)

# 生产代码里该配置键不得再被**当作标识符引用**。
# 用 AST 区分"代码引用"（ast.Name / ast.Attribute）与"字符串常量"
# （丢弃白名单里的字面量、以及"历史上曾用 X"这类说明性 docstring）——
# 只把前者算违规，否则护栏会被正常的白名单/文档注释误伤。
_prod_hits = []
_prod_notes = []
for sub in ("Trading/Engine", "Trading/Infra", "Trading/Strategy",
            "Trading/Broker", "App", "Frontend"):
    base = os.path.join(os.path.dirname(_TG_ROOT), sub)
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in ("__pycache__", "Test")]
        for fn in fns:
            if not fn.endswith((".py", ".js")):
                continue
            fp = os.path.join(dp, fn)
            rel = os.path.relpath(fp, os.path.dirname(_TG_ROOT)).replace("\\", "/")
            txt = open(fp, encoding="utf-8", errors="replace").read()
            if fn.endswith(".js"):
                if re.search(r"(?<![A-Za-z_])max_open_positions(?![A-Za-z_])", txt):
                    _prod_hits.append(rel)
                continue
            try:
                _tree = ast.parse(txt)
            except SyntaxError:
                continue
            for node in ast.walk(_tree):
                if isinstance(node, ast.Attribute) and node.attr == "max_open_positions":
                    _prod_hits.append("%s:%d" % (rel, node.lineno))
                elif isinstance(node, ast.Name) and node.id == "max_open_positions":
                    _prod_hits.append("%s:%d" % (rel, node.lineno))
                elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and "max_open_positions" in node.value):
                    _prod_notes.append("%s:%d" % (rel, node.lineno))
check("[6e] 生产代码无 max_open_positions 的**代码引用**", _prod_hits, [])
check_true("[6e2] 元护栏：那些只有字符串常量的位置确实存在（证明 [6e] 不是假绿）",
           len(_prod_notes) >= 1, "n=%d %s" % (len(_prod_notes), _prod_notes[:3]))


# ════════════════════════════════════════════════════════════════
print("\n[7] 端到端冒烟：默认配置下一开一平")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as td:
    eng = make_engine(td, "c7")
    eng.on_bar(make_bar(5000, date="2026-09-01 09:30"))
    sig = Signal(key="P14A-BUY", symbol="CFFEX.IF2609", freq="5m",
                 timestamp=5000, date="2026-09-01 09:35", bsp_type="buy",
                 is_buy=True, price=4550.0, high=4555.0, low=4545.0)
    eng.on_signal(sig)
    check("[7a] 信号 → 开仓 1 笔", len(eng.positions), 1)
    check("[7b] 方向 LONG", eng.positions.positions[0].side, Side.LONG)
    check("[7c] 状态 RUNNING", eng.account_state(), AccountState.RUNNING)

    act = eng._decide_exit(make_bar(5100, date="2026-09-02 09:30"))
    check("[7d] 跨日 → 转移 ⑤ CLOSE",
          (act.transition, act.intent), (5, OrderIntent.CLOSE))
    eng._force_exit(make_bar(5100, date="2026-09-02 09:30"),
                    reason="p14a_smoke", trigger_price=4560.0)
    check("[7e] 平仓后簿空", len(eng.positions), 0)
    check("[7f] 状态 FLAT", eng.account_state(), AccountState.FLAT)
    check("[7g] 记了 1 笔成交", len(eng.store.trades()), 1)

print("\n" + "=" * 60)
print("P14a 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

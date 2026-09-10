# -*- coding: utf-8 -*-
"""
P31 —— 持仓记录契约闸门（缺 origin 键 / soft_exit_lock 缺 lock_pair_id）
=======================================================================
背景（2026-09-10，D3/D4 收口，用户拍板"有问题把库删了就行"）
------------------------------------------------------------
报告的 P2/P3 两条是同一个根因：`Position.from_dict` 的**缺省空值反模式**——
  · 缺 `origin` 键  → 静默降级 SIGNAL_OPEN（Types.py:438）
  · 缺 `lock_pair_id` → 静默补 ""（Types.py:453）

核实结论（见 deliverables/20260910_P2P3配对根因/verify_p2_p3_rootcause.py）：
  这两类记录**自然流程与任何已发布版本都写不出来** ——
  `SOFT_EXIT_LOCK` 全仓只有 2 个写入点，都在 `_book_lock_pair` 内，
  两笔共用同一个局部变量 pair_id（Engine.py:1086 / :1116）；
  `origin` 与 `lock_pair_id` 是同一提交引入的同源键。
  ⇒ 唯一入口是外部污染（手工改库 / 第三方工具 / 多进程混写）。

处置（不做迁移，与既有三道闸门同口径）：
  `_reject_incomplete_records` 在 `_restore` 装载进簿**之前**拒绝启动；
  错误信息给出的处置办法就是"确认账户无未了结持仓后删除 Trading/State/state.db
  再启动"（与本项目既有 `_reject_legacy_state` 措辞一致）。

本测试覆盖 6 组：
  [1] 纯函数 `_incomplete_position_records` 的判定边界
  [2] 缺 origin 键 → 拒绝启动 + 事件
  [3] soft_exit_lock 且 lock_pair_id 空 → 拒绝启动 + 事件
  [4] 不误伤：未锁仓敞口（空 id 合法）/ 合法锁对 / 空库
  [5] 自然流程产物（开仓 → 关闭锁仓 → 重启）不被误伤
  [6] `wipe_runtime_state` 补清 `positions`：`--fresh` 才真正等价于"删库重来"

跑法：cd <repo root> && PYTHONPATH=. python Trading/Test/test_p31_record_contract_gate.py
"""
from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import tempfile
import sys
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import (Bar, ExitPlan, Position,  # noqa: E402
                                 PositionOrigin, Side, Signal)
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0

D1 = "2026-09-02"
SYMBOL = "CFFEX.IF2609"


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("  OK  " if ok else "  **  ") + name
          + ("" if ok else "   -> got={!r} expected={!r}".format(got, expected)))
    _PASS += 1 if ok else 0
    _FAIL += 0 if ok else 1


def check_true(name, ok, detail=""):
    global _PASS, _FAIL
    print(("  OK  " if ok else "  **  ") + name
          + ("" if ok else "   -> " + str(detail)))
    _PASS += 1 if ok else 0
    _FAIL += 0 if ok else 1


def section(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="p31_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def ms(y, mo, d, h, mi):
    return int(datetime.datetime(y, mo, d, h, mi,
                                 tzinfo=datetime.timezone.utc).timestamp() * 1000)


def make_bar(date, hhmm, close, high, low, ts):
    return Bar(timestamp=ts, date="%s %s" % (date, hhmm), open=close,
               high=high, low=low, close=close)


def make_sig(date, hhmm, is_buy, price, ts, key=None):
    t = "B" if is_buy else "S"
    return Signal(key=key or "%s %s|1|%s" % (date, hhmm, t),
                  symbol=SYMBOL, freq="5m", date="%s %s" % (date, hhmm),
                  timestamp=ts, bsp_type="1", is_buy=is_buy, price=price,
                  high=price + 5, low=price - 5,
                  fractal_low=price - 15, fractal_high=price + 15)


def cfg_of():
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_volume = 2
    cfg.risk.max_open_positions = 3
    cfg.exit_params.use_atr = False
    return cfg


def record(origin=PositionOrigin.SIGNAL_OPEN, lock_pair_id="",
           signal_key="k1", side=Side.LONG, entry_date=D1):
    """造一条**结构完整**的持仓记录；origin/lock_pair_id 可控（模拟外部污染）。"""
    p = Position(symbol=SYMBOL, side=side, volume=2, entry_price=4500.0,
                 entry_at="%s 09:40" % entry_date, entry_bar_ts=ms(2026, 9, 2, 9, 40),
                 entry_bar_seq=1, signal_key=signal_key, open_order_id="o1",
                 exit_plan=ExitPlan(name="ep", stop_price=4490.0),
                 origin=origin, entry_date=entry_date, lock_pair_id=lock_pair_id)
    return p.to_dict()


def start(tmp, tag, records=None):
    """建库（可选预置 positions 记录）→ 起引擎（= 走真实 `_restore`）。

    返回 (eng_or_None, err_or_None, store, ev)。
    store / ev 在**构造引擎之前**就创建并返回 —— EventLog 是带缓冲的（每秒或每 64 条
    才落盘），必须复用同一实例才能 flush 出「构造期抛错前写下的那条事件」。
    """
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    db = os.path.join(tmp, "state_%s.db" % tag)
    store = Store(db)
    if records is not None:
        store.set_json("positions", records)
    ev = EventLog(os.path.join(tmp, "events_%s.jsonl" % tag),
                  echo=False, echo_kinds=None)
    try:
        eng = TradingEngine(cfg_of(), broker, DefaultEntryPolicy({}),
                            LayeredExitPolicy(cfg_of().exit_params.model_dump()),
                            store, ev)
        eng.spec = spec
        return eng, None, store, ev
    except RuntimeError as e:
        return None, e, store, ev


def read_events(ev):
    ev.flush()
    path = getattr(ev, "path", None) or getattr(ev, "_path", None)
    if not path or not os.path.isfile(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    return out


def kinds(ev):
    return [d.get("kind") for d in read_events(ev)]


def book(eng):
    return [(str(p.side), p.volume, p.origin.value, p.lock_pair_id or "-")
            for p in eng.positions.positions]


# ════════════════════════════════════════════════════════════════════
section("[1] 纯函数 _incomplete_position_records 的判定边界")
# ════════════════════════════════════════════════════════════════════
f = getattr(TradingEngine, "_incomplete_position_records", None)
if f is None:
    # 契约闸门尚未实现（A/B 的"修复前"树走这条路）→ 本组直接判失败，
    # 但**不中断**，让后面 [2]/[3]/[6]/[7] 继续跑出行为级失败证据。
    check_true("[1] TradingEngine._incomplete_position_records 存在", False,
               "方法缺失 —— 契约闸门未实现")
else:
    r_open = record(PositionOrigin.SIGNAL_OPEN, "")
    r_lock_ok = record(PositionOrigin.SOFT_EXIT_LOCK, "lock_00001")
    r_lock_bad = record(PositionOrigin.SOFT_EXIT_LOCK, "")
    r_upgrade = record(PositionOrigin.UNLOCK_UPGRADE, "")
    r_no_origin = record(PositionOrigin.SIGNAL_OPEN, "")
    r_no_origin.pop("origin")

    bad = f([r_open, r_lock_ok, r_lock_bad, r_upgrade, r_no_origin])
    check("[1a] 缺 origin 键被挑出（1 笔）", len(bad["missing_origin"]), 1)
    check("[1b] soft_exit_lock 空 id 被挑出（1 笔）", len(bad["orphan_lock"]), 1)
    check("[1c] 未锁仓敞口（signal_open + 空 id）不被误判",
          f([r_open]), {"missing_origin": [], "orphan_lock": []})
    check("[1d] 合法锁对成员（soft_exit_lock + 非空 id）不被误判",
          f([r_lock_ok]), {"missing_origin": [], "orphan_lock": []})
    check("[1e] unlock_upgrade + 空 id 合法（升级后不再是锁仓态）",
          f([r_upgrade]), {"missing_origin": [], "orphan_lock": []})
    check("[1f] 空/None 输入安全", f(None), {"missing_origin": [], "orphan_lock": []})
    check("[1g] 非 dict 项被跳过", f(["x", 1, None]),
          {"missing_origin": [], "orphan_lock": []})
    check("[1h] lock_pair_id 为 None / 空白串同样算空",
          [len(f([record(PositionOrigin.SOFT_EXIT_LOCK, None)])["orphan_lock"]),
           len(f([record(PositionOrigin.SOFT_EXIT_LOCK, "   ")])["orphan_lock"])],
          [1, 1])

# ════════════════════════════════════════════════════════════════════
section("[2] 缺 origin 键 → 拒绝启动 + 事件")
# ════════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    rec = record(PositionOrigin.SIGNAL_OPEN, "")
    rec.pop("origin")
    eng, err, store, ev = start(tmp, "b", [rec])
    check_true("[2a] 抛出 RuntimeError（拒绝启动）", err is not None, repr(err))
    check_true("[2b] 错误信息含处置指引「删除 Trading/State/state.db」",
               err is not None and "state.db" in str(err) and "删除" in str(err),
               str(err)[:200] if err else "")
    check_true("[2c] 错误信息含前提「账户无未了结持仓」",
               err is not None and "无未了结持仓" in str(err), "")
    ks = kinds(ev)
    check_true("[2d] 写了事件 position_record_incomplete",
               "position_record_incomplete" in ks, ks)
    evt = [d for d in read_events(ev)
           if d.get("kind") == "position_record_incomplete"]
    if evt:
        check("[2e] 事件计数正确（missing_origin=1 / orphan_lock=0）",
              [evt[0].get("n_missing_origin"), evt[0].get("n_orphan_lock")], [1, 0])
    check_true("[2f] 坏数据未进入状态机（簿面为空）",
               eng is None, book(eng) if eng else "")
    store.close()

# ════════════════════════════════════════════════════════════════════
section("[3] soft_exit_lock 且 lock_pair_id 为空 → 拒绝启动 + 事件")
# ════════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng, err, store, ev = start(
        tmp, "c", [record(PositionOrigin.SOFT_EXIT_LOCK, "")])
    check_true("[3a] 抛出 RuntimeError（拒绝启动）", err is not None, repr(err))
    check_true("[3b] 错误信息点出「永不升级」的后果",
               err is not None and "永不升级" in str(err),
               str(err)[:300] if err else "")
    check_true("[3c] 错误信息给出处置：「删除 Trading/State/state.db」",
               err is not None and "删除" in str(err), "")
    evt = [d for d in read_events(ev)
           if d.get("kind") == "position_record_incomplete"]
    check("[3d] 事件计数正确（missing_origin=0 / orphan_lock=1）",
          [evt[0].get("n_missing_origin"), evt[0].get("n_orphan_lock")] if evt else None,
          [0, 1])
    check_true("[3e] 坏数据未进入状态机",
               eng is None, book(eng) if eng else "")
    store.close()

with tmp_dir() as tmp:
    # 2 笔同为空 id 的 soft_exit_lock（外部污染最常见形态）
    eng, err, store, ev = start(tmp, "c2", [
        record(PositionOrigin.SOFT_EXIT_LOCK, "", signal_key="kA"),
        record(PositionOrigin.SOFT_EXIT_LOCK, "", signal_key="kB", side=Side.SHORT),
    ])
    check_true("[3f] n≥1 一律拦（2 笔空 id 也不放行）", err is not None, "")
    evt = [d for d in read_events(ev)
           if d.get("kind") == "position_record_incomplete"]
    check("[3g] 事件记到 2 笔", evt[0].get("n_orphan_lock") if evt else None, 2)
    store.close()

# ════════════════════════════════════════════════════════════════════
section("[4] 不误伤：合法记录必须能正常启动")
# ════════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng, err, store, ev = start(tmp, "d1", [])
    check_true("[4a] 空库正常启动", err is None and eng is not None, repr(err))
    store.close()

with tmp_dir() as tmp:
    # 未锁仓的敞口持仓：lock_pair_id 为空是**合法**的
    eng, err, store, ev = start(tmp, "d2", [record(PositionOrigin.SIGNAL_OPEN, "")])
    check_true("[4b] signal_open + 空 lock_pair_id 正常启动（不误伤敞口持仓）",
               err is None, repr(err))
    check("[4c] 记录被装载进簿", book(eng),
          [("多", 2, "signal_open", "-")])
    store.close()

with tmp_dir() as tmp:
    # 合法锁对：两笔 soft_exit_lock 共享同一 id，方向相反
    eng, err, store, ev = start(tmp, "d3", [
        record(PositionOrigin.SOFT_EXIT_LOCK, "lock_00001",
               signal_key="kL", side=Side.LONG),
        record(PositionOrigin.SOFT_EXIT_LOCK, "lock_00001",
               signal_key="kS", side=Side.SHORT),
    ])
    check_true("[4d] 合法锁对正常启动", err is None, repr(err))
    check("[4e] 锁对两笔均装载且 id 一致", book(eng),
          [("多", 2, "soft_exit_lock", "lock_00001"),
           ("空", 2, "soft_exit_lock", "lock_00001")])
    check("[4f] 账户态 = LOCKED（锁对净敞口 0）",
          eng.account_state().value if hasattr(eng.account_state(), "value")
          else str(eng.account_state()), "locked")
    store.close()

with tmp_dir() as tmp:
    # 旧 schema（entry_mode 键）：仍由既有闸门负责，本闸门不抢答
    rec = record(PositionOrigin.SIGNAL_OPEN, "")
    rec["entry_mode"] = "locked"      # 旧 schema 的锁仓值（键本身才是闸门判据）
    eng, err, store, ev = start(tmp, "d4", [rec])
    check_true("[4g] 含 entry_mode 旧键仍由 _reject_legacy_state 拦（两条闸门不抢答）",
               err is not None and "旧 schema" in str(err), str(err)[:120] if err else "")
    store.close()

# ════════════════════════════════════════════════════════════════════
section("[5] 自然流程产物不被误伤：开仓 → 关闭锁仓 → 重启")
# ════════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    db = os.path.join(tmp, "state_e.db")
    store = Store(db)
    ev = EventLog(os.path.join(tmp, "events_e.jsonl"), echo=False, echo_kinds=None)
    eng = TradingEngine(cfg_of(), broker, DefaultEntryPolicy({}),
                        LayeredExitPolicy(cfg_of().exit_params.model_dump()),
                        store, ev)
    eng.spec = spec
    eng.auto_order_enabled = True
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    check("[5a] 自然开仓 1 笔", len(eng.positions.positions), 1)
    r = eng.shutdown_and_lock_all()      # 关闭托管 → 今仓 LOCK → 落锁对
    nat = book(eng)
    check_true("[5b] 关闭后落锁对（两笔 soft_exit_lock）",
               len(nat) == 2 and all(x[2] == "soft_exit_lock" for x in nat), nat)
    check_true("[5c] 自然产物的 lock_pair_id 非空（构造性保证）",
               all(x[3] != "-" for x in nat), nat)
    check_true("[5d] 两笔共用一个 id", len(set(x[3] for x in nat)) == 1, nat)
    store.close()

    # 重启：闸门必须放行
    eng2, err, store2, ev2 = start(tmp, "e")
    check_true("[5e] 重启通过闸门（不误伤自然产出的锁对）", err is None, repr(err))
    check("[5f] 锁对完好恢复", book(eng2), nat)
    store2.close()

# ════════════════════════════════════════════════════════════════════
section("[6] wipe_runtime_state 补清 positions：--fresh 才等价于删库重来")
# ════════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    db = os.path.join(tmp, "state_f.db")
    store = Store(db)
    ev = EventLog(os.path.join(tmp, "events_f.jsonl"), echo=False, echo_kinds=None)
    eng = TradingEngine(cfg_of(), broker, DefaultEntryPolicy({}),
                        LayeredExitPolicy(cfg_of().exit_params.model_dump()),
                        store, ev)
    eng.spec = spec
    eng.auto_order_enabled = True
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    eng.shutdown_and_lock_all()
    n_before = len(store.get_json("positions") or [])
    check_true("[6a] 库里确有持久化持仓记录（复现前提）", n_before == 2, n_before)

    store.wipe_runtime_state()
    check("[6b] wipe 后 positions 键已清空",
          store.get_json("positions"), None)
    check_true("[6c] wipe 后 kv 里不再残留 positions 键",
               store.conn.execute("SELECT COUNT(*) AS n FROM kv WHERE k='positions'")
               .fetchone()["n"] == 0)
    check_true("[6d] 审计底稿仍在（orders 不被 wipe 动）",
               store.conn.execute("SELECT COUNT(*) AS n FROM orders")
               .fetchone()["n"] >= 1)
    store.close()

    eng2, err, store2, ev2 = start(tmp, "f")
    check_true("[6e] wipe 后重启能正常启动", err is None, repr(err))
    check_true("[6f] 簿面为空 —— --fresh 后不再残留上一轮持仓（原本会整对残留）",
               eng2.positions.is_empty(), book(eng2))
    store2.close()

# ════════════════════════════════════════════════════════════════════
section("[7] 源码护栏（防回潮）")
# ════════════════════════════════════════════════════════════════════


def read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def code_only(src):
    """剥掉每行 # 之后的内容（注释里的举例不算"还在用"）。"""
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def body_of(src, name):
    i = src.index("def {}(".format(name))
    j = src.find("\n    def ", i + 10)
    return src[i:j if j > 0 else len(src)]


def _safe_body(src, name):
    """函数不存在时返回空串（A/B 的"修复前"树走这条路，本组应判失败而不是崩掉）。"""
    try:
        return body_of(src, name)
    except ValueError:
        return ""


eng_src = code_only(read("Trading/Engine/Engine.py"))
store_src = code_only(read("Trading/Infra/Store.py"))

check_true("[7a] _restore 里调用 _reject_incomplete_records（放在装载进簿之前）",
           "_reject_incomplete_records(" in body_of(eng_src, "_restore"))
check_true("[7b] 必填记录键写死为 origin（不得被删空）",
           '_REQUIRED_RECORD_KEYS = ("origin",)' in eng_src,
           [l.strip() for l in eng_src.splitlines()
            if "_REQUIRED_RECORD_KEYS =" in l][:2])
check_true("[7c] 空 id 只对 soft_exit_lock 生效（不得扩大成全 origin）",
           '_ORPHAN_LOCK_ORIGIN = "soft_exit_lock"' in eng_src)
check_true("[7d] 拒启动错误信息里给出「删库」处置（与既有闸门同口径）",
           "删除 Trading/State/state.db 再启动" in _safe_body(
               eng_src, "_reject_incomplete_records"))
wipe_body = body_of(store_src, "wipe_runtime_state")
check_true("[7e] wipe 清 positions（复数 = 多仓主键，漏它就不是干净重跑）",
           '"positions"' in wipe_body)
check_true("[7f] wipe 仍不清 order_seq（orders 是审计底稿，序号只增不减）",
           '"order_seq"' not in wipe_body)

print("\n" + "=" * 72)
print("PASS={}  FAIL={}".format(_PASS, _FAIL))
print("=" * 72)
sys.exit(1 if _FAIL else 0)
# -*- coding: utf-8 -*-
"""
P30 关闭自动下单：离场口径（今仓锁 / 昨仓平）＋「冻结」语义契约
================================================================
背景（用户 2026-09-10 拍板 = 甲 方案）
    关闭自动下单（shutdown_and_lock_all / 关闭态 on_bar 补做）时：
        今仓 → LOCK （反向开仓锁仓，避开平今高费率）
        昨仓 → CLOSE（平昨，费率正常）
    判据与常规离场是**同一个** `_exit_intent(pos, today)`（规则 ⑸），
    不存在任何"关闭时一律锁仓"的覆盖开关 —— 旧 `force_lock=True` 已删除。
    （删除前实测：昨仓被反向开仓 → 多付一次开仓费，次日还要再平两笔。）

    ★ 关闭后账户归宿 = **冻结**（用户明确选择，不是遗漏）：
      今仓被 LOCK 成的锁对（原仓 + 反向仓，双双 SOFT_EXIT_LOCK）没有自动出口：
        · on_signal 顶部 return → 永远等不到 UNLOCK；
        · on_bar 关闭态不调 _settle_position，而 _settle_positions 本就跳过
          SOFT_EXIT_LOCK → 不止盈止损、不收盘强平；
        · auto_order_enabled 持久化 → 重启后状态与出口完全不变。
      需**人工在交易所平掉**，或重新开启自动下单后由对向信号走 UNLOCK 管线接管。
      为避免"静默"，关闭时若还挂着持仓会写一条 `account_frozen` 事件。

硬性要求（本测试锁死）
    [1] 今仓 → LOCK（自然流程：day1 开仓 → 当日关闭）
    [2] 昨仓 → CLOSE（自然流程：day1 开仓 → day2 关闭）→ 簿空 + PnL 兑现
    [3] 冻结：关闭后 3 根 bar + 反向信号 → 0 新报单 / 0 新成交 / 簿面不变
    [4] 对照：不关闭时同一剧本 → 止损平仓（证明唯一变量是"关闭"）
    [5] 重启：关闭态持久化 → 反向信号不触发 UNLOCK
    [6] 幂等：重复 shutdown 不产生新单
    [7] 可见性：留下持仓 → 写 account_frozen；当天平干净 → 不写
    [8] 源码护栏（ast 解析，不受注释影响）：force_lock 彻底消失
    [9] P20 夹具口径：make_pos 显式给 entry_date（今仓语义）

全部走 on_bar / on_signal 正常入口，不注入持仓。
跑法：PYTHONPATH=. python Trading/Test/test_p30_shutdown_exit_mode.py
"""
from __future__ import annotations

import ast
import datetime
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(
                os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/Test/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
_REPO_ROOT = os.path.dirname(_TG_ROOT)
for _p in (os.path.dirname(_TG_ROOT), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine, OrderIntent  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import Bar, Side, Signal  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0

D1, D2, D3, D4 = "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓ " if ok else "✗ ") + name
          + ("" if ok else "   -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, ok, detail=""):
    global _PASS, _FAIL
    print(("✓ " if ok else "✗ ") + name + ("" if ok else "   -> " + str(detail)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="p30_%s_" % tag)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)   # Windows：EventLog 占住 jsonl


def ms(y, mo, d, h, mi):
    return int(datetime.datetime(y, mo, d, h, mi,
                                 tzinfo=datetime.timezone.utc).timestamp() * 1000)


def make_bar(date, hhmm, close, high, low, ts):
    return Bar(timestamp=ts, date="%s %s" % (date, hhmm), open=close,
               high=high, low=low, close=close)


def make_sig(date, hhmm, is_buy, price, ts, key=None):
    t = "B" if is_buy else "S"
    return Signal(key=key or "%s %s|1|%s" % (date, hhmm, t),
                  symbol="CFFEX.IF2609", freq="5m", date="%s %s" % (date, hhmm),
                  timestamp=ts, bsp_type="1", is_buy=is_buy, price=price,
                  high=price + 5, low=price - 5,
                  fractal_low=price - 15, fractal_high=price + 15)


def build(tmpdir, tag="a", store_path=None):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_volume = 2
    cfg.risk.max_open_positions = 1
    cfg.risk.unlock_no_new_open = True
    cfg.exit_params.use_atr = False
    cfg.exit_params.min_r_points = 3.0
    cfg.exit_params.use_trailing = True
    cfg.exit_params.breakeven_trigger_r = 1.0
    cfg.exit_params.trailing_trigger_r = 2.0
    cfg.exit_params.trailing_distance_points = 10.0
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    db = store_path or os.path.join(tmpdir, "state_%s.db" % tag)
    store = Store(db)
    ev = EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                  echo=False, echo_kinds=None)
    eng = TradingEngine(cfg, broker, DefaultEntryPolicy({}),
                        LayeredExitPolicy(cfg.exit_params.model_dump()), store, ev)
    eng.spec = spec
    return eng, store, broker, ev, db


def net_exposure(eng):
    return sum((1 if p.side is Side.LONG else -1) * p.volume
               for p in eng.positions.positions)


def book_sig(eng):
    """簿面指纹（笔数 + 方向 + 手数 + 来源 + 配对号），用于断言"纹丝不动"。"""
    return sorted((str(p.side), p.volume, p.origin.value, p.lock_pair_id)
                  for p in eng.positions.positions)


def oid_sig(broker, since=0):
    return [o.order_id for o in broker.orders[since:]]


def new_order(broker, since):
    return broker.orders[since] if len(broker.orders) > since else None


def event_dicts(tmp, tag="a"):
    path = os.path.join(tmp, "events_%s.jsonl" % tag)
    out = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def read(rel):
    with open(os.path.join(_REPO_ROOT, rel.replace("/", os.sep)),
              encoding="utf-8") as f:
        return f.read()


# ════════════════════════════════════════════════════════════════════
print("\n[1] 今仓 → LOCK（自然流程：day1 开仓 → 当日关闭）")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("cur") as tmp:
    eng, store, broker, ev, _ = build(tmp)
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    check("[1a] day1 开多 → 簿内 1 笔 SIGNAL_OPEN",
          [(str(p.side), p.origin.value) for p in eng.positions.positions],
          [("多", "signal_open")])
    check("[1b] 该仓 entry_date = 引擎 today（今仓）",
          (eng.positions.positions[0].entry_date, eng._current_trading_day()), (D1, D1))

    n0 = len(broker.orders)
    eng.shutdown_and_lock_all()
    o = new_order(broker, n0)
    check("[1c] 今仓 → intent=lock / offset=OPEN / 反向 side=空",
          (o.meta.get("intent"), o.meta.get("offset"), str(o.side)),
          ("lock", "OPEN", "空"))
    check("[1d] 簿内 2 笔全 SOFT_EXIT_LOCK（原仓保留 + 反向仓落簿）",
          sorted(p.origin.value for p in eng.positions.positions),
          ["soft_exit_lock", "soft_exit_lock"])
    check("[1e] 净敞口归零", net_exposure(eng), 0)
    check("[1f] 软离场不兑现 PnL → 0 笔 Trade", len(store.trades()), 0)
    check("[1g] enabled=False 已持久化",
          store.get_json("auto_order_enabled", True), False)
    ev.flush()
    kinds = [d.get("kind") for d in event_dicts(tmp)]
    check("[1h] auto_order_off 事件 ×1（首次关闭）", kinds.count("auto_order_off"), 1)
    ev_dict = [d for d in event_dicts(tmp) if d.get("kind") == "auto_order_off"][0]
    check("[1i] 事件 note 已更新为「按规则 ⑸ 离场（今仓锁 / 昨仓平）」",
          "今仓锁" in ev_dict.get("note", "") and "昨仓平" in ev_dict.get("note", ""),
          True)
    check("[1j] lock_booked ×1", kinds.count("lock_booked"), 1)

    # 幂等放在最后：第 2 次关闭只补写事件，不得产生新报单/新成交
    n_re = len(broker.orders)
    t_re = len(store.trades())
    eng.shutdown_and_lock_all()          # 簿内只剩 SOFT_EXIT_LOCK → no-op
    check("[1k] 重复关闭幂等：第 2 次 0 新报单 / 0 新成交",
          (len(broker.orders) - n_re, len(store.trades()) - t_re), (0, 0))
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[2] 昨仓 → CLOSE（自然流程：day1 开仓 → day2 关闭）")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("yd") as tmp:
    eng, store, broker, ev, _ = build(tmp)
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    # day2 一根不触发任何离场的 K 线（止损 4490 / 止盈 4535.6），只为把 last_bar 推到 D2
    eng.on_bar(make_bar(D2, "09:35", 4510, 4520, 4500, ms(2026, 9, 3, 9, 35)))
    pos = eng.positions.positions[0]
    check("[2a] 关闭前：簿内 1 笔 / entry_date=day1 / today=day2",
          (len(eng.positions.positions), pos.entry_date, eng._current_trading_day()),
          (1, D1, D2))
    intent, side = eng._exit_intent(pos, eng._current_trading_day())
    check("[2b] 规则 ⑸ 对该昨仓给出 CLOSE / LONG",
          (intent.value, side.name), (OrderIntent.CLOSE.value, "LONG"))

    n1 = len(broker.orders)
    eng.shutdown_and_lock_all()
    o = new_order(broker, n1)
    check("[2c] 昨仓 → intent=close / offset=CLOSE / side=多（不再反向开仓）",
          (o.meta.get("intent"), o.meta.get("offset"), str(o.side)),
          ("close", "CLOSE", "多"))
    check("[2d] 昨仓被平掉 → 簿空", len(eng.positions.positions), 0)
    trades = store.trades()
    check("[2e] PnL 兑现 → 记 1 笔 Trade（reason=auto_order_off）",
          [(t["reason"], t["side"], t["volume"]) for t in trades],
          [("auto_order_off", "LONG", 2)])
    check("[2f] 无锁对残留（lock_booked 事件 0 条）",
          [d.get("kind") for d in event_dicts(tmp)].count("lock_booked"), 0)
    ev.flush()
    check("[2g] 账户已平干净 → 不写 account_frozen",
          [d.get("kind") for d in event_dicts(tmp)].count("account_frozen"), 0)
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[3] 冻结语义：关闭后 3 根 bar + 反向信号 → 0 新报单 / 0 新成交 / 簿面不变")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("frz") as tmp:
    eng, store, broker, ev, _ = build(tmp)
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    eng.shutdown_and_lock_all()

    n2 = len(broker.orders)
    t2 = len(store.trades())
    book2 = book_sig(eng)
    check("[3a] 关闭瞬间已进入冻结：簿内 2 笔锁对 / 净敞口 0",
          (len(book2), net_exposure(eng)), (2, 0))
    ev.flush()
    frozen_ev = [d for d in event_dicts(tmp) if d.get("kind") == "account_frozen"]
    check("[3b] account_frozen 事件 ×1", len(frozen_ev), 1)
    # 取不到时用空 dict（不 IndexError）：修复前树上要"红"，不要"崩"。
    ev0 = frozen_ev[0] if frozen_ev else {}
    check("[3c] 事件内容：frozen_n=2 / net_exposure=0 / 带 lock_pair_id",
          (ev0.get("frozen_n"), ev0.get("net_exposure"),
           len(ev0.get("lock_pair_ids") or [])),
          (2, 0, 1))
    check_true("[3d] 事件 note 明确「引擎无自动清仓路径」",
               "无自动清仓路径" in (ev0.get("note") or ""),
               ev0.get("note"))

    # day2 大阴线（-1500 点，本该触发 L1 止损）+ 反向信号
    eng.on_bar(make_bar(D2, "09:40", 3050, 3060, 3000, ms(2026, 9, 3, 9, 40)))
    eng.on_signal(make_sig(D2, "09:40", False, 3050.0, ms(2026, 9, 3, 9, 40)))
    # day3 / day4 继续喂 K 线
    eng.on_bar(make_bar(D3, "09:40", 3000, 3010, 2990, ms(2026, 9, 4, 9, 40)))
    eng.on_bar(make_bar(D4, "09:40", 3100, 3110, 3090, ms(2026, 9, 7, 9, 40)))

    check("[3e] 关闭后 0 新报单", len(broker.orders) - n2, 0)
    check("[3f] 关闭后 0 新成交（PnL 永不兑现）", len(store.trades()) - t2, 0)
    check("[3g] 簿面纹丝不动（笔数/方向/来源/配对号全同）", book_sig(eng), book2)
    row = store.conn.execute(
        "SELECT action, note FROM processed_signals WHERE signal_key=?",
        (make_sig(D2, "09:40", False, 3050.0, 0).key,)).fetchone()
    check("[3h] day2 反向信号被关闭门消费为 skip/auto_order_off",
          (dict(row)["action"], dict(row)["note"]) if row else None,
          ("skip", "auto_order_off"))
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[4] 对照：同一剧本但【不关闭】→ day2 大阴线触发止损 → 簿空")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("on") as tmp:
    eng, store, broker, ev, _ = build(tmp)
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    n3 = len(broker.orders)
    eng.on_bar(make_bar(D2, "09:40", 3050, 3060, 3000, ms(2026, 9, 3, 9, 40)))
    o = new_order(broker, n3)
    check("[4a] 不关闭 → day2 止损报单 intent=close（跨日 → CLOSE）",
          o.meta.get("intent") if o else None, "close")
    check("[4b] 簿空", len(eng.positions.positions), 0)
    check("[4c] 记 1 笔 Trade（reason=sl）",
          [t["reason"] for t in store.trades()], ["sl"])
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[5] 重启：关闭态持久化 → 反向信号不触发 UNLOCK")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("rs") as tmp:
    eng, store, broker, ev, db = build(tmp, "a")
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    eng.shutdown_and_lock_all()
    book_before = book_sig(eng)
    store.close()

    eng2, store2, broker2, ev2, _ = build(tmp, "b", store_path=db)
    check("[5a] 重启后 enabled 仍 False", eng2.auto_order_enabled, False)
    check("[5b] 重启后簿面一致", book_sig(eng2), book_before)
    eng2.on_bar(make_bar(D3, "09:40", 4000, 4010, 3990, ms(2026, 9, 4, 9, 40)))
    eng2.on_signal(make_sig(D3, "09:40", False, 4000.0, ms(2026, 9, 4, 9, 40)))
    check("[5c] 反向信号后簿面仍不变（无 UNLOCK 出口）", book_sig(eng2), book_before)
    check("[5d] 0 新报单", len(broker2.orders), 0)
    store2.close()

# ════════════════════════════════════════════════════════════════════
print("\n[6] 源码护栏（ast 解析；注释里出现 force_lock 字样不算违规）")
# ════════════════════════════════════════════════════════════════════
eng_src = read("Trading/Engine/Engine.py")
eng_tree = ast.parse(eng_src)
fns = {n.name: n for n in ast.walk(eng_tree)
       if isinstance(n, ast.FunctionDef)}


def _arg_names(fn):
    return [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]


def _arg_default(fn, name):
    args = [a.arg for a in fn.args.args]
    if name not in args:
        return None, False
    i = args.index(name)
    base = len(args) - len(fn.args.defaults)
    if i < base:
        return None, False
    return ast.literal_eval(fn.args.defaults[i - base]), True


check_true("[6a] _close_positions 签名已无 force_lock 参数",
           "force_lock" not in _arg_names(fns["_close_positions"]),
           _arg_names(fns["_close_positions"]))

kw_hits = [k.arg for k in ast.walk(eng_tree)
           if isinstance(k, ast.keyword) and k.arg == "force_lock"]
check("[6b] 全模块已无任何 force_lock= 关键字调用", kw_hits, [])

calls = [c for c in ast.walk(fns["_close_positions"])
         if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "_exit_intent"]
check_true("[6c] _close_positions 内确实调用了 _exit_intent（唯一离场判据）",
           len(calls) >= 1, "命中 {} 次".format(len(calls)))

calls2 = [c for c in ast.walk(fns["_lock_remaining_positions"])
          if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "_close_positions"]
check_true("[6d] _lock_remaining_positions 调用了 _close_positions",
           len(calls2) == 1, "命中 {} 次".format(len(calls2)))
check_true("[6e] 该调用不带 force_lock 关键字",
           "force_lock" not in [k.arg for c in calls2 for k in c.keywords],
           [k.arg for c in calls2 for k in c.keywords])

up = fns["_upgrade_lock_pair"]
check_true("[6f] _upgrade_lock_pair 仍在（P2/R3 相关，本改动不应波及）",
           "SOFT_EXIT_LOCK" in ast.dump(up), "")

# P20 兼容：夹具必须显式给 entry_date（今仓语义）
p20_tree = ast.parse(read("Trading/Test/test_p20_phase_i1.py"))
p20_fn = next(n for n in ast.walk(p20_tree)
              if isinstance(n, ast.FunctionDef) and n.name == "make_pos")
dflt, has = _arg_default(p20_fn, "entry_date")
check("[6g] p20 的 make_pos 显式给 entry_date（默认 = 夹具 bar 的交易日）",
      (has, dflt), (True, "2026-09-03"))
check_true("[6h] p20 把 entry_date 透传进 Position(...)",
           any(k.arg == "entry_date" for k in ast.walk(p20_fn)
               if isinstance(k, ast.keyword)), "")

# ════════════════════════════════════════════════════════════════════
print("\n" + "=" * 62)
print("P30 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
if _FAIL:
    raise SystemExit(1)

# -*- coding: utf-8 -*-
"""
P30 关闭自动下单：离场口径（今仓反向开 / 昨仓平）＋「冻结」语义契约
====================================================================
背景（用户 2026-09-10 拍板 = 甲 方案；2026-09-11 Phase 7 贴合新转移表改写）
    关闭自动下单（shutdown_and_lock_all / 关闭态 on_bar 补做）时：
        今仓 → 转移 ④（反向 OPEN：净敞口归零，进入锁仓态，避开平今高费率）
        昨仓 → 转移 ⑤（CLOSE 平昨，费率正常）
    判据与常规离场是**同一个** `_decide_exit(bar)`（规则 ⑸⑹），
    不存在任何"关闭时一律反向开仓"的覆盖开关 —— 旧 `force_lock=True` 已删除。
    （删除前实测：昨仓被反向开仓 → 多付一次开仓费，次日还要再平两笔。）

    ★ 关闭后账户归宿 = **冻结**（用户明确选择，不是遗漏）：
      今仓被转移 ④ 反向开仓后净敞口归零（= LOCKED），没有自动出口：
        · on_signal 顶部 return → 永远等不到 ③ 拆锁；
        · on_bar 关闭态不调 _settle_positions，而 LOCKED 态本就不参与 L1-L3
          → 不止盈止损、不收盘强平；
        · auto_order_enabled 持久化 → 重启后状态与出口完全不变。
      两条人工出口：① 重新开启自动下单 + 出现对向信号（走转移 ③ 拆锁）；
                    ② 在交易所手工平仓。
      为避免"静默"，关闭时若净敞口已归零但簿非空 → 写 `account_frozen` 事件。

2026-09-11 改写说明
    · "来源标记"（origin）/ 配对号（lock_pair_id）/ `lock_booked` 事件随
      "配对"概念一并删除 → 簿面指纹不再含这两项，改用
      (方向, 手数, 建仓日, 建仓价) 表达"纹丝不动"。
    · 离场判据由 `_exit_intent(pos, today)` 改为纯函数 `_decide_exit(bar)`
      —— 后者直接产出转移动作（含 transition=4/5），不再由测试自己拼 intent。
    · `orders.meta["intent"]` 收敛为 2 值（open/close）；④ 与 ① 都记 open，
      改由引擎统一补写的 `meta["is_exit"]` / `meta["transition"]` 区分（审计补全）。

硬性要求（本测试锁死）
    [1] 今仓 → 转移 ④（自然流程：day1 开仓 → 当日关闭）→ 双向持仓、net 0、LOCKED
    [2] 昨仓 → 转移 ⑤（自然流程：day1 开仓 → day2 关闭）→ 簿空 + PnL 兑现
    [3] 冻结：关闭后 3 根 bar + 反向信号 → 0 新报单 / 0 新成交 / 簿面不变
    [4] 对照：不关闭时同一剧本 → 止损平仓（证明唯一变量是"关闭"）
    [5] 重启：关闭态持久化 → 反向信号不触发拆锁
    [6] 幂等：重复 shutdown 不产生新单
    [7] 可见性：关闭后停在锁仓态 → 写 account_frozen；当天平干净 → 不写
    [8] 源码护栏（ast 解析，不受注释影响）：force_lock / 配对方法彻底消失

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
from Trading.Infra.Types import AccountState, Bar, Side, Signal  # noqa: E402
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
    # max_open_positions / unlock_no_new_open 已于 Phase 1-4 删除（D2）：前者是同向笔数门，
    # 后者在 D1（风控锚改挂在 run 上）后失去意义。
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
    """簿面指纹（方向 + 手数 + 建仓日 + 建仓价），用于断言"纹丝不动"。

    2026-09-11：原来还含 p.origin / p.lock_pair_id —— 两者随"来源 / 配对"
    概念删除，改用建仓日 + 建仓价表达"同一笔仓单没被动过"（这对一个只做
    FIFO 对消的簿来说是充分的：既没被平掉、也没被替换）。
    """
    return sorted((p.side.name, p.volume, p.entry_date, round(p.entry_price, 3))
                  for p in eng.positions.positions)


def order_sig(broker, since=0):
    """报单指纹：intent + offset + is_exit + transition（④ 与 ① 靠后两者区分）。"""
    return [(o.meta.get("intent"), o.meta.get("offset"), o.meta.get("is_exit"),
             o.meta.get("transition")) for o in broker.orders[since:]]


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
print("\n[1] 今仓 → 转移 ④ 反向 OPEN（自然流程：day1 开仓 → 当日关闭）")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("cur") as tmp:
    eng, store, broker, ev, _ = build(tmp)
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    check("[1a] day1 开多 → 簿内 1 笔（多 / 2 手）",
          [(p.side.name, p.volume) for p in eng.positions.positions],
          [("LONG", 2)])
    check("[1b] 该仓 entry_date = 引擎 today（今仓）",
          (eng.positions.positions[0].entry_date, eng._current_trading_day()), (D1, D1))

    # _decide_exit 是唯一离场判据：先直接问它（纯函数），再看关闭是否照着执行
    act = eng._decide_exit(eng.last_bar)
    check("[1b2] 规则 ⑹ 对该今仓给出转移 ④（反向 OPEN）",
          (act.intent.value, act.side.name, act.volume, act.transition, act.is_exit),
          ("open", "SHORT", 2, 4, True))

    n0 = len(broker.orders)
    eng.shutdown_and_lock_all()
    o = new_order(broker, n0)
    check("[1c] 今仓 → intent=open / offset=OPEN / 反向 side=空",
          (o.meta.get("intent"), o.meta.get("offset"), str(o.side)),
          ("open", "OPEN", "空"))
    check("[1c2] ④ 与 ① 靠审计补全的 is_exit / transition 区分",
          (o.meta.get("is_exit"), o.meta.get("transition")), (True, 4))
    check("[1d] 簿内 2 笔双向持仓（原多 + 反向空），净敞口归零",
          sorted((p.side.name, p.volume) for p in eng.positions.positions),
          [("LONG", 2), ("SHORT", 2)])
    check("[1e] 净敞口归零", net_exposure(eng), 0)
    check("[1e2] 账户态 = LOCKED（净敞口 0 且簿非空）",
          eng.account_state().value, "locked")
    check("[1f] 软离场不兑现 PnL → 0 笔 Trade", len(store.trades()), 0)
    check("[1g] enabled=False 已持久化",
          store.get_json("auto_order_enabled", True), False)
    ev.flush()
    kinds = [d.get("kind") for d in event_dicts(tmp)]
    check("[1h] auto_order_off 事件 ×1（首次关闭）", kinds.count("auto_order_off"), 1)
    check_true("[1h2] account_frozen ×1（净敞口 0 但簿非空 → 冻结可见）",
               kinds.count("account_frozen") == 1, kinds)
    ev_dict = [d for d in event_dicts(tmp) if d.get("kind") == "auto_order_off"][0]
    check_true("[1i] 事件 note 指向规则 ⑹ 的 4/5 两条离场口径",
               "⑹" in ev_dict.get("note", "")
               and "反向开仓" in ev_dict.get("note", "")
               and "平仓" in ev_dict.get("note", ""),
               ev_dict.get("note"))
    check("[1i2] 事件带上账户态与净敞口（可自查）",
          (ev_dict.get("account_state"), ev_dict.get("net_volume")), ("locked", 0))
    check_true("[1j] `lock_booked` 事件已随配对概念删除（不再出现）",
               "lock_booked" not in kinds, kinds)

    # 幂等放在最后：第 2 次关闭只补写事件，不得产生新报单/新成交
    n_re = len(broker.orders)
    t_re = len(store.trades())
    eng.shutdown_and_lock_all()          # 净敞口已 0 → _decide_exit 返回 None → no-op
    check("[1k] 重复关闭幂等：第 2 次 0 新报单 / 0 新成交",
          (len(broker.orders) - n_re, len(store.trades()) - t_re), (0, 0))
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[2] 昨仓 → 转移 ⑤ CLOSE（自然流程：day1 开仓 → day2 关闭）")
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

    act = eng._decide_exit(eng.last_bar)
    check("[2b] 规则 ⑹ 对该昨仓给出转移 ⑤（CLOSE / 目标=该仓本身）",
          (act.intent.value, act.side.name, act.transition, act.is_exit,
           act.target is pos),
          ("close", "LONG", 5, True, True))

    n1 = len(broker.orders)
    eng.shutdown_and_lock_all()
    o = new_order(broker, n1)
    check("[2c] 昨仓 → intent=close / offset=CLOSE / side=多（不反向开仓）",
          (o.meta.get("intent"), o.meta.get("offset"), str(o.side)),
          ("close", "CLOSE", "多"))
    check("[2c2] 审计补全：is_exit=True / transition=5",
          (o.meta.get("is_exit"), o.meta.get("transition")), (True, 5))
    check("[2d] 昨仓被平掉 → 簿空", len(eng.positions.positions), 0)
    check("[2d2] 平干净 → 账户态 FLAT", eng.account_state().value, "flat")
    trades = store.trades()
    check("[2e] PnL 兑现 → 记 1 笔 Trade（reason=auto_order_off）",
          [(t["reason"], t["side"], t["volume"]) for t in trades],
          [("auto_order_off", "LONG", 2)])
    ev.flush()
    kinds = [d.get("kind") for d in event_dicts(tmp)]
    check("[2f] 未产生任何反向仓 / 配对事件（lock_booked 已删除）",
          (kinds.count("lock_booked"), len(eng.positions.positions)), (0, 0))
    check("[2g] 账户已平干净 → 不写 account_frozen",
          kinds.count("account_frozen"), 0)
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
    check("[3a] 关闭瞬间已进入冻结：簿内 2 笔 / 净敞口 0 / 态=LOCKED",
          (len(book2), net_exposure(eng), eng.account_state().value),
          (2, 0, "locked"))
    ev.flush()
    frozen_ev = [d for d in event_dicts(tmp) if d.get("kind") == "account_frozen"]
    check("[3b] account_frozen 事件 ×1", len(frozen_ev), 1)
    # 取不到时用空 dict（不 IndexError）：修复前树上要"红"，不要"崩"。
    ev0 = frozen_ev[0] if frozen_ev else {}
    check("[3c] 事件内容：positions_n=2 / net_volume=0",
          (ev0.get("positions_n"), ev0.get("net_volume")), (2, 0))
    check_true("[3c2] 事件**不再**带 lock_pair_ids（配对概念已删）",
               "lock_pair_ids" not in ev0, sorted(ev0.keys()))
    check_true("[3d] 事件 note 明确「引擎无自动清仓路径」",
               "无自动清仓路径" in (ev0.get("note") or ""),
               ev0.get("note"))
    check_true("[3d2] 事件 note 给两条人工出口（重开自动下单 / 手工平仓）",
               "重新开启自动下单" in (ev0.get("note") or "")
               and "人工平仓" in (ev0.get("note") or ""),
               ev0.get("note"))

    # day2 大阴线（-1500 点，本该触发 L1 止损）+ 反向信号
    eng.on_bar(make_bar(D2, "09:40", 3050, 3060, 3000, ms(2026, 9, 3, 9, 40)))
    eng.on_signal(make_sig(D2, "09:40", False, 3050.0, ms(2026, 9, 3, 9, 40)))
    # day3 / day4 继续喂 K 线
    eng.on_bar(make_bar(D3, "09:40", 3000, 3010, 2990, ms(2026, 9, 4, 9, 40)))
    eng.on_bar(make_bar(D4, "09:40", 3100, 3110, 3090, ms(2026, 9, 7, 9, 40)))

    check("[3e] 关闭后 0 新报单", len(broker.orders) - n2, 0)
    check("[3f] 关闭后 0 新成交（PnL 永不兑现）", len(store.trades()) - t2, 0)
    check("[3g] 簿面纹丝不动（方向/手数/建仓日/建仓价全同）", book_sig(eng), book2)
    check("[3g2] 账户态纹丝不动（仍 LOCKED）", eng.account_state().value, "locked")
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
    check("[4a] 不关闭 → day2 止损报单 intent=close（跨日 → 转移 ⑤）",
          (o.meta.get("intent"), o.meta.get("transition")) if o else None,
          ("close", 5))
    check("[4b] 簿空", len(eng.positions.positions), 0)
    check("[4b2] 账户态 FLAT", eng.account_state().value, "flat")
    check("[4c] 记 1 笔 Trade（reason=sl）",
          [t["reason"] for t in store.trades()], ["sl"])
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[5] 重启：关闭态持久化 → 反向信号不触发转移 ③（拆锁）")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("rs") as tmp:
    eng, store, broker, ev, db = build(tmp, "a")
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    eng.shutdown_and_lock_all()
    book_before = book_sig(eng)
    check("[5a0] 关闭后进入 LOCKED（待重启验证状态保持）",
          eng.account_state().value, "locked")
    store.close()

    eng2, store2, broker2, ev2, _ = build(tmp, "b", store_path=db)
    check("[5a] 重启后 enabled 仍 False", eng2.auto_order_enabled, False)
    check("[5b] 重启后簿面一致", book_sig(eng2), book_before)
    check("[5b2] 重启后账户态仍 LOCKED", eng2.account_state().value, "locked")
    eng2.on_bar(make_bar(D3, "09:40", 4000, 4010, 3990, ms(2026, 9, 4, 9, 40)))
    eng2.on_signal(make_sig(D3, "09:40", False, 4000.0, ms(2026, 9, 4, 9, 40)))
    check("[5c] 反向信号后簿面仍不变（无拆锁出口）", book_sig(eng2), book_before)
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


kw_hits = [k.arg for k in ast.walk(eng_tree)
           if isinstance(k, ast.keyword) and k.arg == "force_lock"]
check_true("[6a] 全模块已无任何 force_lock= 关键字调用", kw_hits == [], kw_hits)
check_true("[6a2] 也没有叫 force_lock 的函数参数",
           all("force_lock" not in _arg_names(f) for f in fns.values()),
           [n for n, f in fns.items() if "force_lock" in _arg_names(f)])

# 关闭走的是与常规离场**同一条链**：shutdown_and_lock_all → _force_exit → _decide_exit
calls = [c for c in ast.walk(fns["shutdown_and_lock_all"])
         if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "_force_exit"]
check_true("[6b] shutdown_and_lock_all 调用 _force_exit（同一出口）",
           len(calls) == 1, "命中 {} 次".format(len(calls)))

calls2 = [c for c in ast.walk(fns["_force_exit"])
          if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "_decide_exit"]
check_true("[6c] _force_exit 内调用 _decide_exit（唯一离场判据）",
           len(calls2) >= 1, "命中 {} 次".format(len(calls2)))

check_true("[6d] _force_exit 不再带 force_lock 关键字（改用 force 绕冷却）",
           "force_lock" not in [k.arg for c in calls for k in c.keywords],
           [k.arg for c in calls for k in c.keywords])

# 配对时代的方法必须**连名带体**消失（防半途回潮），不只是调用点被删
for _m in ("_book_lock_pair", "_upgrade_lock_pair", "_assert_lock_pair_invariant",
           "_close_positions", "_lock_remaining_positions", "_exit_intent"):
    check_true("[6e] 配对/四意图时代的方法 {} 已删除".format(_m),
               _m not in fns, "仍在" if _m in fns else "")

# P20 兼容：夹具必须显式给 entry_date（今仓语义）
p20_tree = ast.parse(read("Trading/Test/test_p20_phase_i1.py"))
p20_fn = next(n for n in ast.walk(p20_tree)
              if isinstance(n, ast.FunctionDef) and n.name == "make_pos")
dflt, has = _arg_default(p20_fn, "entry_date")
check("[6f] p20 的 make_pos 显式给 entry_date（默认 = 夹具 bar 的交易日）",
      (has, dflt), (True, "2026-09-03"))
check_true("[6g] p20 把 entry_date 透传进 Position(...)",
           any(k.arg == "entry_date" for k in ast.walk(p20_fn)
               if isinstance(k, ast.keyword)), "")

# ════════════════════════════════════════════════════════════════════
print("\n" + "=" * 62)
print("P30 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
if _FAIL:
    raise SystemExit(1)

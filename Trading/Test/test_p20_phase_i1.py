# -*- coding: utf-8 -*-
"""
P20 Phase I1：自动下单开关（关闭离场 / live 配置）单元测试
==========================================================
2026-09-11 Phase 7 改写。旧版 [1]-[6] 建立在两个已删概念上：
  · `OrderIntent.LOCK` / `PositionOrigin.SOFT_EXIT_LOCK` / `lock_pair_id`
    —— "锁仓"不再是一种**打标的操作**，而是"净敞口 = 0 且簿非空"的**状态**
    （`AccountState.LOCKED`）。今日仓离场走转移 ④（反向 OPEN），仓单上不打标。
  · `cfg.risk.max_open_positions` —— D2 删除，夹具不再配置它。

背景（用户拍板关闭语义，2026-09-11 复核后口径不变）
    关闭自动下单：
      ① 不再接收买卖点信号（on_signal 顶部拒收，幂等键照常消费）
      ② 运行态持仓按【规则 ⑹】离场（判据是建仓日期，与来源无关）：
         今仓 → 转移 ④ 反向 OPEN（整段净敞口一次锁住）→ 账户停在 LOCKED
         昨仓 → 转移 ⑤ CLOSE（平昨）
    状态持久化：auto_order_enabled 落盘 state.db，重启保持关闭语义。
    ★ 关闭后 LOCKED 是**没有自动出口**的冻结态：既不收信号、也不参与 L1-L3。
      两条人工出口 = 重新开启后由对向信号拆锁（转移 ③）/ 交易所手工平仓。
      这条契约由 test_p30_shutdown_exit_mode.py 做完整覆盖，本文件只做基本确认。

Phase I1 配置（账户选择）—— 配置唯一入口 Trading/Config.py（无 config.json）
    broker = dry_run / simnow / live
    broker_params.tq_market：simnow=仿真；实盘填期货公司名（如"创元期货"）
    实盘安全闸门：broker=live 或 tq_market≠simnow 时必须显式
      confirm_live_trading=true，否则拒绝启动（AppTrader.start 预检）。

硬性要求（本测试锁死）
    [1] 关闭后 on_signal 拒收（signal_action=skip / note=auto_order_off）
    [2] shutdown_and_lock_all：今日仓 → 转移 ④ 反向 OPEN（**1 笔报单**把整段净敞口
        锁住）→ 簿内 3 笔（2 原仓 + 1 反向仓）、net 0、LOCKED、0 Trade；
        enabled=False 持久化；auto_order_off + account_frozen 事件
    [3] 幂等：重复 shutdown 不产生新单 / 新 trade
    [4] 重启保持关闭：同 store 新引擎 auto_order_enabled=False，信号仍拒收
    [5] 关闭态 on_bar 补锁：④ 被拒的残留持仓在后续 bar 自动补锁
        （reason=auto_order_off_retry）；⑤（跨日 CLOSE）则要等满冷却根数
    [6] 开启恢复：_persist True → 重启后 on_signal 正常开仓
    [7] 实盘安全闸门（AppTrader._check_live_gate 三分支）
    [8] broker 路由：SimNowBroker.is_live 判定 + LiveCTPBroker 注册
    [9]-[12] AppTrader 子进程 / 状态 / CWD 行为（依赖仓库根 App/ 包）

不需要真实 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。
跑法：python Trading/Test/test_p20_phase_i1.py
"""
from __future__ import annotations

import copy
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
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d  # Trading 包目录本身（消 tg/ 层后 Trading 即包）
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))
_REPO_ROOT = os.path.dirname(_TG_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)   # App/ 包（AppTrader 安全闸门单测）


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p20_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from Trading import Broker  # noqa: E402  注册 dry_run/simnow/live
from Trading.Broker.Base import BROKERS, build_broker  # noqa: E402
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Broker.SimNow import LiveCTPBroker, SimNowBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    Bar, Order, OrderIntent, Position, ExitPlan, Side, Signal, now_cn,
)

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name
          + ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("✓" if ok else "✗") + " " + name
          + ("" if ok else "  -> got={!r}".format(got)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def make_cfg(max_pos=None):
    """构造配置。2026-09-11：`max_open_positions` 已按 D2 删除（容器不限容量），
    max_pos 形参仅为兼容旧调用点保留 —— 传了也不再生效（旧键会被 D17 白名单丢弃）。"""
    d = copy.deepcopy(DEFAULT_CONFIG)
    if max_pos is not None:
        d["risk"]["max_open_positions"] = max_pos
    return TradingConfig.from_dict(d)


def make_signal(is_buy, price=4500.0, high=4552.0, low=4548.0,
                date="2026-09-03 09:35", bsp_type="1", sig_key=None):
    sig_key = sig_key or ("{}|{}|{}".format(date, bsp_type, "B" if is_buy else "S"))
    return Signal(key=sig_key, symbol="CFFEX.IF2609", freq="5m", date=date,
                  timestamp=0, bsp_type=bsp_type, is_buy=is_buy,
                  price=price, high=high, low=low)


def make_bar(ts, o=4500.0, h=4510.0, l=4490.0, c=4505.0,
             date="2026-09-03 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_pos(symbol="CFFEX.IF2609", side=Side.LONG, vol=1, entry_price=4500.0,
             signal_key="P20-pos", entry_bar_seq=1, entry_date="2026-09-03"):
    """构造一笔"关闭时正在运行、且【当日】开仓"的持仓。

    2026-09-11：**不再传 origin**（字段已删）。关闭时按 entry_date 判今/昨仓
    （今仓 → 转移 ④ 反向 OPEN 锁仓 / 昨仓 → 转移 ⑤ CLOSE）。
      本组夹具的语义是"当日开、当日关" → entry_date 必须等于引擎的 today，
      即 make_bar() 的日期 2026-09-03；否则会被判成昨仓走 CLOSE，
      与 [2]-[5] 组"关闭 = 锁仓"的断言不符。
    """
    return Position(
        symbol=symbol, side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-02 09:00",
        entry_bar_ts=entry_bar_seq * 1000, signal_key=signal_key,
        open_order_id="p20-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_bar_seq=entry_bar_seq,
        entry_date=entry_date)


class LockRejectBroker(DryRunBroker):
    """前 reject_n 次**离场向**报单拒绝（模拟锁仓/平仓拒单），之后放行。

    2026-09-11：`OrderIntent.LOCK` 已删 —— "锁仓"现在是转移 ④ 的
    `OPEN + is_exit=True`。拒单条件随之从 `intent is LOCK` 改为
    `is_exit=True`（覆盖 ④⑤ 两条离场路径；开仓的 OPEN 不拒）。
    """

    def __init__(self, spec, params, reject_n=1):
        super().__init__(spec, params)
        self._reject_left = reject_n

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        intent = self._resolve_intent(intent, side)
        if is_exit and self._reject_left > 0:
            self._reject_left -= 1
            o = Order(
                order_id="dry-reject-{:06d}".format(len(self.orders)),
                signal_key=signal_key, symbol=self.spec.trade_symbol, side=side,
                action="close" if intent is OrderIntent.CLOSE else "open",
                volume=int(volume), price=0.0,
                req_price=float(ref_price), filled_price=None,
                status="rejected", created_at=now_cn(), broker=self.name, note=note,
                meta={"intent": intent.value, "offset": "OPEN",
                      "reject_reason": "sim_reject"})
            self.orders.append(o)
            return o
        return super().submit(intent, side, volume, ref_price, signal_key,
                              note=note, entry_date=entry_date, is_exit=is_exit)


def build_engine(tmpdir, exit_policy=None, broker=None, cfg=None,
                 store=None, ev=None):
    # 2026-09-11：不再传 max_pos —— D2 删除该键后传它只会触发 D17 丢弃告警。
    cfg = cfg or make_cfg()
    spec = InstrumentSpec()
    broker = broker or DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    from Trading.Strategy.Entry import DefaultEntryPolicy
    from Trading.Strategy.Exit import LayeredExitPolicy
    entry = DefaultEntryPolicy({})
    exitp = exit_policy or LayeredExitPolicy()
    store = store or Store(os.path.join(tmpdir, "state.db"))
    ev = ev or EventLog(os.path.join(tmpdir, "events.jsonl"),
                        echo=False, echo_kinds=None)
    engine = TradingEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker, ev


def event_kinds(ev_path):
    kinds = []
    with open(ev_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                kinds.append(json.loads(line).get("kind", ""))
    return kinds


def lock_orders(broker):
    """离场向报单（④ 反向 OPEN / ⑤ CLOSE）。

    2026-09-11：旧判据 `meta["intent"] == "lock"` 随 4→2 intent 收敛失效
    （① 与 ④ 现在都是 intent="open"）。改判 `is_exit` —— 它由引擎在报单出口
    统一写进 Order.meta（Engine._execute，Phase 7 审计补全）。
    """
    return [o for o in broker.orders if o.meta.get("is_exit")]


def sides_of(book):
    return sorted(p.side.name for p in book.positions)


# ════════════════════════════════════════════════════════════════
# [1] 关闭后 on_signal 拒收（signal_action=skip / note=auto_order_off）
# ════════════════════════════════════════════════════════════════
print("\n[1] 关闭后 on_signal 拒收（幂等键消费 + skip 落盘 + 事件）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.shutdown_and_lock_all()          # 空簿：无锁仓操作，只关信号门
    sig = make_signal(is_buy=True, sig_key="P20-1|1|B")
    engine.on_signal(sig)
    check("[1a] auto_order_enabled=False",
          engine.auto_order_enabled, False)
    check("[1b] 信号 action=skip", store.signal_action(sig.key), "skip")
    check("[1c] 零报单（信号被拒收）", len(broker.orders), 0)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[1d] signal_skip 事件已写", "signal_skip" in kinds, True)
    check("[1e] auto_order_off 事件已写", "auto_order_off" in kinds, True)


# ════════════════════════════════════════════════════════════════
# [2] shutdown_and_lock_all：2 笔今仓 → 转移 ④ 反向 OPEN 整段锁住
# ════════════════════════════════════════════════════════════════
print("\n[2] shutdown_and_lock_all：2 笔今仓 → ④ 反向 OPEN（1 单锁整段）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.positions.add(make_pos(signal_key="P20-2A", entry_bar_seq=1))
    engine.positions.add(make_pos(signal_key="P20-2B", entry_bar_seq=2))

    engine.shutdown_and_lock_all()
    check("[2a] enabled=False", engine.auto_order_enabled, False)
    # 2026-09-11 新口径：2 笔今仓多单（net=+2）→ 转移 ④ 反向 OPEN 2 手
    #   → 簿内 2 原仓 + 1 反向仓 = 3 笔（旧口径是逐笔锁 → 4 笔）
    check("[2b] 簿 3 笔：2 原仓 + 1 笔反向仓（整段净敞口一次锁住）",
          len(engine.positions), 3)
    check("[2c] 方向组合 = 2 LONG + 1 SHORT",
          [sides_of(engine.positions).count("LONG"),
           sides_of(engine.positions).count("SHORT")], [2, 1])
    check("[2c2] 反向仓 2 手（= 净敞口，不是逐笔 1 手 ×2）",
          [p.volume for p in engine.positions.positions
           if p.side is Side.SHORT], [2])
    check("[2d] 净敞口归零 → account_state LOCKED",
          engine.account_state().value, "locked")
    check("[2d2] LOCKED → _state=IDLE（IDLE 同时覆盖 FLAT 与 LOCKED）",
          engine._state.name, "IDLE")
    # 报单：整段只有 1 笔（旧口径 2 笔 LOCK）
    check("[2e] 1 笔离场报单", len(lock_orders(broker)), 1)
    check("[2e2] 该单 2 手", lock_orders(broker)[0].volume, 2)
    check("[2e3] 该单 intent=open（④ 是反向开仓）",
          lock_orders(broker)[0].meta.get("intent"), "open")
    check("[2e4] 该单 transition=4",
          lock_orders(broker)[0].meta.get("transition"), 4)
    check("[2f] 锁仓不兑现 PnL → 0 笔 Trade（④ 不记 Trade）",
          len(store.trades()), 0)
    check("[2g] enabled=False 已持久化",
          store.get_json("auto_order_enabled", True), False)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[2h] auto_order_off 事件 ×1", kinds.count("auto_order_off"), 1)
    check("[2h2] account_frozen 事件 ×1（LOCKED 是冻结态）",
          kinds.count("account_frozen"), 1)
    check("[2i] 无 lock_booked 事件（软离场打标已随来源概念删除）",
          kinds.count("lock_booked"), 0)
    check_true("[2i2] LOCKED 冻结态升级告警（D11）", len(engine._alerts) >= 1)


# ════════════════════════════════════════════════════════════════
# [3] 幂等：重复 shutdown 不产生新单 / 新 trade
# ════════════════════════════════════════════════════════════════
print("\n[3] 幂等：净敞口已归零时重复 shutdown 无操作")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.positions.add(make_pos(signal_key="P20-3A", entry_bar_seq=1))
    engine.shutdown_and_lock_all()
    n_orders = len(broker.orders)
    n_trades = len(store.trades())
    n_pos = len(engine.positions)

    engine.shutdown_and_lock_all()          # 第二次：net==0 → _decide_exit 返回 None
    check("[3a] 无新增报单", len(broker.orders), n_orders)
    check("[3b] 无新增 trade", len(store.trades()), n_trades)
    check("[3c] 簿仍 2 笔（原仓 + 反向仓，net=0）",
          (len(engine.positions), n_pos), (2, 2))
    check("[3c2] 仍是双向持仓",
          sides_of(engine.positions), ["LONG", "SHORT"])
    check("[3d] enabled 仍 False", engine.auto_order_enabled, False)
    check("[3e] account_state 仍 LOCKED", engine.account_state().value, "locked")


# ════════════════════════════════════════════════════════════════
# [4] 重启保持关闭：同 store 新引擎 enabled=False，信号仍拒收
# ════════════════════════════════════════════════════════════════
print("\n[4] 重启保持关闭语义（state.db 持久化）")
with tmp_dir() as tmp:
    engine1, store1, broker1, ev1 = build_engine(tmp)
    engine1.on_bar(make_bar(1000))
    engine1.positions.add(make_pos(signal_key="P20-4A", entry_bar_seq=1))
    engine1.shutdown_and_lock_all()
    check("[4a0] 关闭后 1 原仓 + 1 反向仓 = 2 笔", len(engine1.positions), 2)

    engine2, store2, broker2, ev2 = build_engine(tmp)
    check("[4a] 重启后 enabled=False", engine2.auto_order_enabled, False)
    check("[4b] 簿内双向持仓已恢复（LOCKED 跨重启保持）",
          sides_of(engine2.positions), ["LONG", "SHORT"])
    check("[4b2] 净敞口仍 0", engine2.positions.net_volume(), 0)
    check("[4b3] account_state LOCKED", engine2.account_state().value, "locked")
    sig = make_signal(is_buy=True, sig_key="P20-4|2|B")
    engine2.on_signal(sig)
    check("[4c] 重启后信号仍拒收（skip）",
          store2.signal_action(sig.key), "skip")
    check("[4d] 重启后零报单", len(broker2.orders), 0)


# ════════════════════════════════════════════════════════════════
# [5] 关闭态 on_bar 补锁：首轮离场被拒 → 后续 bar 自动补
# ════════════════════════════════════════════════════════════════
print("\n[5] 关闭态 on_bar 补锁（首轮被拒 → 后续 bar 自动补）")

# 5A 今仓（转移 ④ OPEN）被拒 → **下一根 bar 立即**补（OPEN 不受 CLOSE 冷却约束）
with tmp_dir() as tmp:
    broker = LockRejectBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                              reject_n=1)
    engine, store, broker, ev = build_engine(tmp, broker=broker)
    engine.on_bar(make_bar(1000))
    engine.positions.add(make_pos(signal_key="P20-5A", entry_bar_seq=1))

    engine.shutdown_and_lock_all()
    check("[5a] 首轮锁仓被拒：持仓未锁（净敞口仍 +1）",
          engine.positions.net_volume(), 1)
    check("[5a2] 被拒后 state=IN_TRADE（净敞口≠0 → RUNNING 的镜像）",
          engine._state.name, "IN_TRADE")
    check("[5a3] 簿内仍 1 笔（未变）", len(engine.positions), 1)
    ev.flush()      # EventLog 有缓冲，读文件前必须 flush，否则漏读
    check_true("[5a4] 写 order_rejected 事件",
               "order_rejected" in event_kinds(os.path.join(tmp, "events.jsonl")))

    # ④ 是 OPEN → 不进入 CLOSE 冷却 → 下一根 bar 就该补上
    engine.on_bar(make_bar(2000))
    check("[5b] 补锁后簿 2 笔（原仓 + 反向仓）", len(engine.positions), 2)
    check("[5b2] 补锁后 net=0 → LOCKED", engine.account_state().value, "locked")
    check("[5b3] 补锁后 state=IDLE", engine._state.name, "IDLE")
    check("[5b4] 补锁报单 reason=auto_order_off_retry",
          [o.note for o in broker.orders if o.meta.get("is_exit")][-1],
          "auto_order_off_retry")
    check("[5b5] 补锁不兑现 PnL → 0 笔 Trade", len(store.trades()), 0)

# 5B 昨仓（转移 ⑤ CLOSE）被拒 → 必须等满冷却根数才重试
with tmp_dir() as tmp:
    broker = LockRejectBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                              reject_n=1)
    engine, store, broker, ev = build_engine(tmp, broker=broker)
    engine.on_bar(make_bar(1000))
    # entry_date 早于 bar 日 → 昨仓 → 转移 ⑤ CLOSE
    engine.positions.add(make_pos(signal_key="P20-5B", entry_bar_seq=1,
                                  entry_date="2026-09-02"))

    engine.shutdown_and_lock_all()
    check("[5c] 昨仓离场被拒：簿仍 1 笔", len(engine.positions), 1)
    check("[5c2] 进入 CLOSE 冷却", engine._in_close_cooldown(), True)

    engine.on_bar(make_bar(2000))          # 仅 1 根 → 仍在冷却
    check("[5d] 冷却中：簿仍 1 笔（未重试）", len(engine.positions), 1)
    check("[5d2] 冷却中离场报单仍只有首轮那 1 笔",
          len([o for o in broker.orders if o.meta.get("is_exit")]), 1)

    for i in range(engine._close_retry_bars):
        engine.on_bar(make_bar(3000 + i))
    check("[5e] 冷却期满后补平：簿清空", len(engine.positions), 0)
    check("[5e2] account_state FLAT", engine.account_state().value, "flat")
    check("[5e3] ⑤ 是 CLOSE → 兑现 1 笔 Trade", len(store.trades()), 1)


# ════════════════════════════════════════════════════════════════
# [6] 开启恢复：_persist True → 重启后 on_signal 正常开仓
# ════════════════════════════════════════════════════════════════
print("\n[6] 开启恢复：显式置 True 持久化 → 重启后信号正常开仓")
with tmp_dir() as tmp:
    engine1, store1, broker1, ev1 = build_engine(tmp)
    engine1.shutdown_and_lock_all()
    check("[6a] 先关闭", engine1.auto_order_enabled, False)

    engine1.auto_order_enabled = True       # 模拟 AppTrader._reset_engine_switch
    engine1._persist()
    engine2, store2, broker2, ev2 = build_engine(tmp)
    check("[6b] 重启后 enabled=True", engine2.auto_order_enabled, True)
    sig = make_signal(is_buy=True, sig_key="P20-6|1|B")
    engine2.on_signal(sig)
    check("[6c] 信号 action=opened", store2.signal_action(sig.key), "opened")
    check("[6d] 1 笔开仓报单", len(broker2.orders), 1)
    check("[6d2] 该单是入场（is_exit=False），不是离场",
          broker2.orders[0].meta.get("is_exit"), False)
    check("[6d3] 该单 transition=1（空仓开新仓）",
          broker2.orders[0].meta.get("transition"), 1)
    check("[6e] 簿内 1 笔 LONG",
          sides_of(engine2.positions), ["LONG"])
    check("[6f] 净敞口 > 0 → RUNNING",
          engine2.account_state().value, "running")


# ════════════════════════════════════════════════════════════════
# [7] 实盘安全闸门（AppTrader._check_live_gate 三分支）
#     依赖仓库根 App/ 包——Trading/ 单独解压运行（无 App/）时整节跳过；
#     完整仓库内（App/ 存在）照常执行全部断言。
# ════════════════════════════════════════════════════════════════
print("\n[7] 实盘安全闸门（AppTrader._check_live_gate）")
try:
    from App.AppTrader import AppTrader  # noqa: E402
    from App.AppErrors import AppError  # noqa: E402
    _HAS_APP = True
except ImportError:
    _HAS_APP = False
    print("  - SKIP：当前目录无 App/ 包（Trading 独立运行），"
          "本节在完整仓库内执行")

if _HAS_APP:

    def cfg_with(broker, bp):
        d = copy.deepcopy(DEFAULT_CONFIG)
        d["broker"] = broker
        d["broker_params"].update(bp)
        return TradingConfig(**d)      # 配置是模型，不再是裸 dict


    def raises_apperror(fn, name):
        try:
            fn()
        except AppError:
            check(name, True, True)
            return
        check(name, True, False)


    # [7a] broker=live 但 tq_market 仍 simnow → 配置矛盾拒绝
    raises_apperror(
        lambda: AppTrader._check_live_gate(cfg_with("live", {}), "live"),
        "[7a] broker=live + tq_market=simnow → 拒绝")

    # [7b] tq_market≠simnow 但未开 confirm_live_trading → 拒绝
    raises_apperror(
        lambda: AppTrader._check_live_gate(
            cfg_with("simnow", {"tq_market": "创元期货"}), "simnow"),
        "[7b] tq_market=创元期货 未确认实盘 → 拒绝")

    # [7c] tq_market≠simnow + confirm_live_trading=true → 放行
    try:
        AppTrader._check_live_gate(
            cfg_with("simnow", {"tq_market": "创元期货",
                                "confirm_live_trading": True}), "simnow")
        check("[7c] 实盘双确认 → 放行", True, True)
    except AppError as e:
        check("[7c] 实盘双确认 → 放行", str(e), "NO_ERROR")

    # [7d] dry_run / simnow 仿真 → 放行
    try:
        AppTrader._check_live_gate(cfg_with("dry_run", {}), "dry_run")
        AppTrader._check_live_gate(cfg_with("simnow", {}), "simnow")
        check("[7d] dry_run / simnow 仿真 → 放行", True, True)
    except AppError as e:
        check("[7d] dry_run / simnow 仿真 → 放行", str(e), "NO_ERROR")


# ════════════════════════════════════════════════════════════════
# [8] broker 路由：is_live 判定 + LiveCTPBroker 注册
# ════════════════════════════════════════════════════════════════
print("\n[8] broker 路由（SimNowBroker.is_live / LiveCTPBroker 注册）")
spec = InstrumentSpec()

_BP = dict(DEFAULT_CONFIG["broker_params"])   # 严格模式：params 必须完整
b_sim = SimNowBroker(spec, dict(_BP, tq_market="simnow"))
check("[8a] tq_market=simnow → is_live=False", b_sim.is_live, False)

b_live = SimNowBroker(spec, dict(_BP, tq_market="创元期货",
                                  confirm_live_trading=True))
check("[8b] tq_market=创元期货 → is_live=True", b_live.is_live, True)
check("[8c] 无凭据 → _conn_error 指向实盘/天勤",
      b_live._conn_error is not None
      and "实盘/天勤" in b_live._conn_error, True)

b_gate = SimNowBroker(spec, {"tq_market": "创元期货"})
check("[8d] 未确认实盘 → 安全闸门拦截",
      b_gate._conn_error is not None
      and "confirm_live_trading" in b_gate._conn_error, True)

check("[8e] LiveCTPBroker 已注册", "live" in BROKERS, True)
b_live2 = build_broker("live", spec, {})
check("[8f] build_broker('live') 返回 LiveCTPBroker",
      isinstance(b_live2, LiveCTPBroker), True)
check("[8g] LiveCTPBroker.is_live=True", b_live2.is_live, True)
check("[8h] live 别名未覆盖 simnow 注册", "simnow" in BROKERS, True)


# [9]-[12] 全部依赖仓库根 App/ 包（AppTrader 子进程/状态/CWD 行为）。
# Trading/ 单独解压运行（无 App/）时整段跳过并正常退出；
# 完整仓库内（App/ 存在）照常执行全部断言。
if not _HAS_APP:
    print("\n  - SKIP [9]-[12]：当前目录无 App/ 包（Trading 独立运行）")
    print("\n" + "=" * 60)
    print("P20 Phase I1 结果: {} 通过 / {} 失败（[9]-[12] 已跳过）".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(0 if _FAIL == 0 else 1)


# ════════════════════════════════════════════════════════════════
# [9] AppTrader.start 子进程 cmd 组装（--source sse + 品种/周期/地址 + 日志落盘）
#     回归：曾经不带 --source sse → cfg.source 缺省 type=replay → 回放源
#     无数据立即跑完 → 子进程退出 → 前端开关自动关闭（用户报告的问题）。
# ════════════════════════════════════════════════════════════════
print("\n[9] AppTrader.start：--source sse 组装 + gateway.log 落盘")
from App import AppTrader as AT  # noqa: E402


class _FakeProc:
    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.args = cmd          # subprocess.run 读 process.args 组装返回
        self.pid = os.getpid()   # 存活 pid，让 running 属性为 True

    def poll(self):
        return None

    def send_signal(self, sig):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    # 第四轮 P0 回归修复：AT.subprocess 就是真实 subprocess 模块，测试把
    # Popen 换成 _FakeProc 后，stop() 强杀分支的 _taskkill 内部
    # subprocess.run(...) 也会命中 _FakeProc，而 run 会 `with process:`
    # 复用返回值的 context manager 协议。若不实现 __enter__/__exit__，
    # Windows 上 p20 在 [9i] 强杀路径抛
    # "'_FakeProc' object does not support the context manager protocol"，
    # 导致 p20 在用户 Windows 环境整段 abort、[9i]/[9j] 全不执行。
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # subprocess.Popen.__exit__ 等价于 wait()。subprocess.run 在 `with
        # process:` 块内还会调 communicate() 采集 stdout/stderr，并由
        # process.returncode 判定返回码——故还需补 communicate/returncode。
        self.wait()
        return False  # 不吞异常

    def communicate(self, input=None, timeout=None):
        # 模拟"命令执行成功但无输出"：返回 (stdout=b"", stderr=b"")。
        # subprocess.run(capture_output=True) 依赖它为 Popen 填充输出，
        # 缺失会在 Windows 上抛 AttributeError。
        return b"", b""

    @property
    def returncode(self):
        return 0


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


with tmp_dir() as tmp:
    out_dir = os.path.join(tmp, "state")
    state_file = os.path.join(tmp, "auto_trader_state.json")

    captured = {}
    orig_popen = AT.subprocess.Popen
    orig_state_file = AT._STATE_FILE
    orig_load_cfg = AT.AppTrader._load_cfg
    try:
        AT._STATE_FILE = state_file
        # 配置不再来自 config.json：直接注入一份 TradingConfig（dry_run）
        AT.AppTrader._load_cfg = staticmethod(
            lambda: TradingConfig(broker="dry_run", state_dir=out_dir))
        AT.subprocess.Popen = (lambda cmd, **kw:
                               captured.update(cmd=cmd) or _FakeProc(cmd))
        t = AT.AppTrader()
        res = t.start(out_dir=out_dir,
                      symbol="KQ.m@CFFEX.SH", freq="5m",
                      sse_base="http://127.0.0.1:18081")
        cmd = captured.get("cmd") or []
        check("[9a] 带 --source sse", "--source" in cmd
              and cmd[cmd.index("--source") + 1] == "sse", True)
        check("[9b] 带 --symbol 当前页面品种",
              "--symbol" in cmd
              and cmd[cmd.index("--symbol") + 1] == "KQ.m@CFFEX.SH", True)
        check("[9c] 带 --freq 当前页面周期",
              "--freq" in cmd
              and cmd[cmd.index("--freq") + 1] == "5m", True)
        check("[9d] 带 --sse-base 服务地址",
              "--sse-base" in cmd
              and cmd[cmd.index("--sse-base") + 1] == "http://127.0.0.1:18081",
              True)
        check("[9e] gateway.log 已创建", os.path.isfile(
            os.path.join(out_dir, "gateway.log")), True)
        check("[9f] handle 记录 symbol/freq/sse_base",
              (res.get("symbol"), res.get("freq"), res.get("sse_base")),
              ("KQ.m@CFFEX.SH", "5m", "http://127.0.0.1:18081"))
        check("[9g] 状态文件含来源参数",
              _read_json(state_file).get("symbol"), "KQ.m@CFFEX.SH")
        check("[9h0] 子进程命令不再带 --config（配置归一：无 config.json）",
              "--config" in cmd, False)
    finally:
        AT.subprocess.Popen = orig_popen
        AT._STATE_FILE = orig_state_file
        AT.AppTrader._load_cfg = staticmethod(orig_load_cfg)

# [9i] P1-3：start() 必须清除上次遗留 .stop_request，否则第二次开启秒退
#（残留 flag 会让新看护线程一眼就叫停并锁仓退出）。
with tmp_dir() as tmp:
    out_dir = os.path.join(tmp, "state")
    os.makedirs(out_dir, exist_ok=True)
    # 预置"上次 stop 残留"的 flag
    leftover = os.path.join(out_dir, ".stop_request")
    with open(leftover, "w", encoding="utf-8") as f:
        f.write("leftover from previous round\n")
    orig_popen = AT.subprocess.Popen
    orig_state_file = AT._STATE_FILE
    orig_load_cfg = AT.AppTrader._load_cfg
    try:
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        AT.AppTrader._load_cfg = staticmethod(
            lambda: TradingConfig(broker="dry_run", state_dir=out_dir))
        AT.subprocess.Popen = (lambda cmd, **kw: _FakeProc(cmd))
        t = AT.AppTrader()
        res = t.start(out_dir=out_dir)   # 不应抛错、不应秒退
        check("[9i] start() 清除了上一轮遗留的 .stop_request（P1-3）",
              not os.path.exists(leftover), True)
        check("[9i] 清除后子进程仍正常启动（running=True）", res.get("running"), True)
        # 清理用极短 timeout：_FakeProc 永不退出，默认 timeout=150s 会空等满
        # 并走强杀分支（在 Windows 上旧代码还会因 SIGKILL 崩溃，P0）；短
        # timeout 让强杀分支立即执行，兼作 P0 回归（不应抛 AttributeError）。
        t.stop(timeout=0.1)   # 收尾清理，避免污染本目录
    finally:
        AT.subprocess.Popen = orig_popen
        AT._STATE_FILE = orig_state_file
        AT.AppTrader._load_cfg = staticmethod(orig_load_cfg)

# [9j] P2-5 回援分支修正：只认本轮新增事件，历史 off + 上轮残留 state=false
# 不得判优雅（否则"本轮超时强杀没锁仓"仍被谎报 graceful=True）。
with tmp_dir() as tmp:
    out_dir = tmp
    evt = os.path.join(out_dir, "events.jsonl")
    # 历史已有 2 条 off（模拟往常有两次成功收尾）+ off_before=2（本轮无新增）
    with open(evt, "w", encoding="utf-8") as f:
        for _ in range(2):
            json.dump({"kind": "auto_order_off"}, f)
            f.write("\n")
    from Trading.Infra.Store import Store  # noqa: E402
    s = Store(os.path.join(out_dir, "state.db"))
    s.set_json("auto_order_enabled", False)   # 恰好是上一轮遗留的 false
    s.close()
    check("[9j] 历史off且state=false 不下回援（拒绝谎报 graceful）",
          AT._graceful_by_result(out_dir, 2), False)
    # 本轮确实新增 1 条 -> 判优雅
    with open(evt, "a", encoding="utf-8") as f:
        json.dump({"kind": "auto_order_off"}, f)
        f.write("\n")
    check("[9j] 本轮新增 auto_order_off 判优雅",
          AT._graceful_by_result(out_dir, 2), True)
    # 全新目录 events 从未有 off、state=false -> 唯一合法回援场景
    fresh = os.path.join(tmp, "fresh")
    os.makedirs(fresh, exist_ok=True)
    s2 = Store(os.path.join(fresh, "state.db"))
    s2.set_json("auto_order_enabled", False)
    s2.close()
    check("[9j] 空events+state=false 才回援判优雅",
          AT._graceful_by_result(fresh, 0), True)

# [9h] 未传 symbol/freq → 回落到 cfg.source / 内置默认（不再落到 replay）
with tmp_dir() as tmp:
    captured = {}
    orig_popen = AT.subprocess.Popen
    orig_state_file = AT._STATE_FILE
    orig_load_cfg = AT.AppTrader._load_cfg
    try:
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        AT.AppTrader._load_cfg = staticmethod(lambda: TradingConfig(
            broker="dry_run", state_dir=os.path.join(tmp, "State"),
            source={"symbol": "KQ.m@CFFEX.RB", "freq": "15m"}))
        AT.subprocess.Popen = (lambda cmd, **kw:
                               captured.update(cmd=cmd) or _FakeProc(cmd))
        t = AT.AppTrader()
        res = t.start(out_dir=os.path.join(tmp, "State"))
        cmd = captured.get("cmd") or []
        check("[9h] 缺省品种走 cfg.source.symbol",
              cmd[cmd.index("--symbol") + 1], "KQ.m@CFFEX.RB")
        check("[9i] 缺省周期走 cfg.source.freq",
              cmd[cmd.index("--freq") + 1], "15m")
    finally:
        AT.subprocess.Popen = orig_popen
        AT._STATE_FILE = orig_state_file
        AT.AppTrader._load_cfg = staticmethod(orig_load_cfg)


# ════════════════════════════════════════════════════════════════
# [10] AppTrader.status：进程退出后附带 gateway.log 尾部（定位"自动关闭"）
# ════════════════════════════════════════════════════════════════
print("\n[10] AppTrader.status：退出后附带日志尾部")
with tmp_dir() as tmp:
    out_dir = os.path.join(tmp, "state")
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "gateway.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("[gw] 启动 pid=123 source=sse symbol=KQ.m@CFFEX.IF freq=5m\n")
        f.write("Traceback (most recent call last):\n")
        f.write("  File \"tg/engine.py\", line 42, in on_bar\n")
        f.write("RuntimeError: boom\n")

    class _ExitedProc:
        pid = 123

        def poll(self):
            return 1   # 已退出

    orig_state_file = AT._STATE_FILE
    try:
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        t = AT.AppTrader()
        t._handle = AT._TraderProc(
            _ExitedProc(), out_dir=out_dir, started_at="2026-09-06 10:00:00",
            broker="dry_run", symbol="KQ.m@CFFEX.IF", freq="5m",
            sse_base="http://127.0.0.1:18081")
        st = t.status()
        check("[10a] running=False", st["running"], False)
        check("[10b] log_file 路径正确", st["log_file"], log_path)
        check("[10c] log_tail 带回异常尾部",
              "RuntimeError: boom" in (st.get("log_tail") or ""), True)
        check("[10d] 日志尾部含启动摘要",
              "source=sse" in (st.get("log_tail") or ""), True)
        check("[10g] exit_rc 带回退出码", st.get("exit_rc"), 1)
        # 日志雪崩回归：退出上报只写一次，多次轮询 status() 不得把日志
        # 尾部反复 Echo 回 gateway.log（曾导致指数级自嵌套膨胀）。
        size_after_first = 0
        with open(log_path, "r", encoding="utf-8") as f:
            size_after_first = len(f.read())
        for _ in range(5):
            st2 = t.status()
            check("[10e] 退出上报一次后仍带回 log_tail",
                  "RuntimeError: boom" in (st2.get("log_tail") or ""), True)
        with open(log_path, "r", encoding="utf-8") as f:
            size_after_polls = len(f.read())
        check("[10f] 多次轮询日志不再增长（无雪崩）",
              size_after_polls == size_after_first, True)
    finally:
        AT._STATE_FILE = orig_state_file


# ════════════════════════════════════════════════════════════════
# [11] 配置加载失败：目录与 gateway.log 也必须创建，并记录失败原因
#      回归：曾经校验失败在 makedirs 之前 raise → 什么都没有，无法定位
# ════════════════════════════════════════════════════════════════
print("\n[11] 配置加载失败：仍落盘 gateway.log 记录失败原因")
with tmp_dir() as tmp:
    out_dir = os.path.join(tmp, "State")
    orig_state_file = AT._STATE_FILE
    _env_bak = os.environ.get("TRADING_RISK__MAX_VOLUME")
    try:
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        # 真实失败路径：.env / 环境变量写错 → Trading/Config.py 严格校验抛错。
        # 不再有"配置文件不存在"这种失败 —— 配置本来就不是文件了。
        os.environ["TRADING_RISK__MAX_VOLUME"] = "abc"
        t = AT.AppTrader()
        try:
            t.start(out_dir=out_dir)
            check("[11a] 应抛 AppError", "no_raise", "AppError")
        except AppError as e:
            check("[11a] 应抛 AppError", "AppError", "AppError")
            check("[11b] 报错含配置来源", "Trading/Config.py" in str(e), True)
        log_path = os.path.join(out_dir, "gateway.log")
        check("[11c] state 目录已创建", os.path.isdir(out_dir), True)
        check("[11d] gateway.log 已创建", os.path.isfile(log_path), True)
        content = ""
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            pass
        check("[11e] 日志含开启请求", "收到开启请求" in content, True)
        check("[11f] 日志含失败原因", "读取配置失败" in content, True)
    finally:
        if _env_bak is None:
            os.environ.pop("TRADING_RISK__MAX_VOLUME", None)
        else:
            os.environ["TRADING_RISK__MAX_VOLUME"] = _env_bak
        AT._STATE_FILE = orig_state_file


# ════════════════════════════════════════════════════════════════
# [12] 相对 state_dir（默认 "./State"）以配置文件所在目录为基准
#      回归：曾经 os.path.abspath("./State") 落到后端进程 CWD，
#      用户按 Trading/State 找不到目录/日志
# ════════════════════════════════════════════════════════════════
print("\n[12] 相对 state_dir 以 Trading/ 为基准（不受后端进程 CWD 影响）")
_cwd0 = os.getcwd()
with tmp_dir() as tmp:
    orig_state_file = AT._STATE_FILE
    orig_popen = AT.subprocess.Popen
    orig_root = AT._TG_ROOT
    orig_load_cfg = AT.AppTrader._load_cfg
    try:
        # 把"Trading 目录"临时指向 tmp 下的假家，避免测试污染真实仓库
        AT._TG_ROOT = os.path.join(tmp, "Trading")
        os.makedirs(AT._TG_ROOT, exist_ok=True)
        AT.AppTrader._load_cfg = staticmethod(
            lambda: TradingConfig(broker="dry_run", state_dir="./State"))
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        captured = {}
        AT.subprocess.Popen = (lambda cmd, **kw:
                               captured.update(cmd=cmd) or _FakeProc(cmd))
        t = AT.AppTrader()
        res = t.start()   # 不传 out_dir：走 state_dir 解析
        expected = os.path.join(AT._TG_ROOT, "State")
        cmd = captured.get("cmd") or []
        check("[12a] --out 解析到 Trading/State",
              "--out" in cmd and cmd[cmd.index("--out") + 1] == expected, True)
        check("[12b] state 目录在 Trading 下", os.path.isdir(expected), True)
        check("[12c] gateway.log 在 Trading/State 下",
              os.path.isfile(os.path.join(expected, "gateway.log")), True)
        # 模拟用户后端从仓库根启动：CWD=tmp 时也不得落到 tmp/State
        os.chdir(tmp)
        check("[12d] 未污染进程 CWD", os.path.isdir(os.path.join(tmp, "State")),
              False)
        check("[12e] 落到 Trading/State", os.path.isdir(expected), True)
        check("[12f] 状态文件记录 out_dir", res.get("out_dir"), expected)
    finally:
        os.chdir(_cwd0)
        AT.subprocess.Popen = orig_popen
        AT._STATE_FILE = orig_state_file
        AT._TG_ROOT = orig_root
        AT.AppTrader._load_cfg = staticmethod(orig_load_cfg)


print("\n" + "=" * 60)
print("P20 Phase I1 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

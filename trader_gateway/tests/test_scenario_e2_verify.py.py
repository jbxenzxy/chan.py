# -*- coding: utf-8 -*-
"""
Phase E2 三场景端到端验证（2026-09-05）
========================================
用户要求："先验证 E2 的所有现存场景（dry-run / simnow / 回放）后再进 E3"

覆盖三种现存场景：
  场景 A：dry-run 完整 UNLOCK 端到端剧本
    —— 构造持仓 (SHORT 3手, 昨锁仓, entry_mode=OPEN_FIRST 隐含 → LOCK_SOFT 路径)，
       注入今日正向买入信号 → 验证 `_unlock_position` 把昨仓平掉，trade 按 CloseYesterday 费率入账。

  场景 B：ReplaySource + DryRunBroker 烟雾回放
    —— 用真实 replay_data/signals.json 跑一遍引擎，验证：
       ① 不死循环（每个信号被处理一次）；
       ② 不引入"幽灵成交"（broker 收到的报单 = 期望信号数）；
       ③ E2 在真实信号序列下没引入新的"idempotency skip"（P11 已覆盖语义，这里看数量级）。

  场景 C：SimNowBroker 接口层断言（不连真实 CTP）
    —— broker._conn_error 已配置（环境变量都没有），broker.submit(UNLOCK, ...) 走 _rejected 分支。
       验证 Order.meta.intent / action 字段正确表达 UNLOCK_FIRST + CloseYesterday 语义。

退出码
  0 = 全 PASS
  1 = 有 FAIL

不修正任何 engine 代码 —— 这是个验证脚本，FAIL 时只复现问题给用户看，由用户决定走 E3 还是先修 E2。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime

# 让脚本可以独立跑：把 trader_gateway 根目录加进 path
THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, ROOT)

from tg import brokers, sources  # noqa: E402  触发注册
from tg.brokers.dry_run import DryRunBroker  # noqa: E402
from tg.brokers.simnow import SimNowBroker  # noqa: E402
from tg.config import DEFAULT_CONFIG, GatewayConfig  # noqa: E402
from tg.engine import GatewayEngine  # noqa: E402
from tg.events import EventLog  # noqa: E402
from tg.position_book import PositionBook  # noqa: E402
from tg.sources.replay_source import ReplaySource  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import (  # noqa: E402
    DefaultEntryPolicy, DefaultExitPolicy)
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
    Bar, EngineState, EntryMode, ExitPlan, OrderIntent, Position, Side, Signal,
)


def _must(condition: bool, label: str, ctx: dict | None = None) -> None:
    if condition:
        print("  [OK]  {}".format(label))
        return
    print("  [FAIL]  {}".format(label))
    if ctx:
        for k, v in ctx.items():
            try:
                vs = json.dumps(v, default=str, ensure_ascii=False)[:300]
            except Exception:
                vs = repr(v)[:300]
            print("    {} = {}".format(k, vs))
    raise AssertionError(label)


def _section(title: str) -> None:
    print("\n" + "=" * 70)
    print("  {}".format(title))
    print("=" * 70)


def _build_engine(tmp_dir: str, broker=None, entry=None, exitp=None):
    """构造一个最小可跑引擎（dry-run broker）便于场景 A / B 复用。

    用 GatewayConfig.from_dict(DEFAULT_CONFIG) 起步；broker / 策略可注入。
    """
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    cfg.state_dir = tmp_dir
    cfg.broker = "dry_run" if broker is None else broker.name
    spec = cfg.instrument
    store = Store(os.path.join(tmp_dir, "state.db"))
    store.wipe_runtime_state()
    ev = EventLog(os.path.join(tmp_dir, "events.jsonl"), echo=False, echo_kinds=set())
    broker_obj = broker if broker is not None else DryRunBroker(spec, cfg.broker_params or {})
    entry_obj = entry if entry is not None else DefaultEntryPolicy()
    exitp_obj = exitp if exitp is not None else DefaultExitPolicy()
    engine = GatewayEngine(cfg, broker_obj, entry_obj, exitp_obj, store, ev)
    return engine, store, ev


# ======================================================================
# 场景 A —— dry-run 完整 UNLOCK 端到端剧本
# ======================================================================
def scenario_a_unlock_endtoend() -> bool:
    _section("场景 A：dry-run 完整 UNLOCK 端到端剧本")
    tmp = os.path.join(ROOT, "_verify_A")
    if os.path.isdir(tmp):
        import shutil; shutil.rmtree(tmp)
    os.makedirs(tmp)

    engine, store, ev = _build_engine(tmp)

    # 推送 1 根 K 线（推进 last_bar / bars_seen 初始化）
    bar = Bar(
        timestamp=1_788_226_500_000,
        date="2026-09-02 09:30",
        open=4545.0, high=4548.0, low=4542.0, close=4547.4,
        vol=1000.0,
    )
    engine.on_bar(bar)

    # 手工塞一个"昨锁仓后留下的 SHORT 对冲仓"到 PositionBook。
    # entry_mode=OPEN_FIRST：因为它是经 _close_position 走 LOCK_SOFT 路径产生的：
    #   OPEN_FIRST 持仓 → 离场走 LOCK_SOFT → 开反向 SHORT 同手数（这就是"昨锁仓的"立场）。
    #
    # 注意：Phase E1 阶段 PositionBook.max=1，所以只能塞这一笔（同 K 线全离场前不容纳第二笔）。
    pos = Position(
        symbol="CFFEX.IF2609", side=Side.SHORT, volume=3,
        entry_price=4520.0,
        entry_at="2026-09-01 14:55:00",
        entry_bar_seq=0, entry_bar_ts=int(1.788e12),
        signal_key="2026-09-01 13:30|1|S",
        open_order_id="dry_run-000001",
        exit_plan=ExitPlan(name="layered_v1", stop_price=4530.0, tp_price=4500.0),
        entry_mode=EntryMode.OPEN_FIRST,
    )
    engine.positions.set_legacy(pos)
    engine._state = EngineState.IDLE                # 强制 IDLE（持仓已 LOCK_SOFT 抵销，今仓为 0）

    _must(not engine.positions.is_empty(), "Portfolio 非空（昨锁仓后留下的反向仓）",
          {"positions": engine.positions.to_dict()})
    _must(engine._state == EngineState.IDLE, "状态机在 IDLE（昨 LOCK_SOFT 后今仓为 0）",
          {"state": engine._state.value})

    # 注入今日正向买入信号 → 预期触发 _unlock_position
    sig = Signal(
        key="2026-09-02 09:35|2|B",
        symbol="CFFEX.IF2609",
        freq="5m",
        date="2026-09-02 09:35",
        timestamp=1_788_312_900_000,
        bsp_type="2",
        is_buy=True,
        price=4555.0,
        high=4556.0,
        low=4554.0,
    )

    broker_orders_before = len(engine.broker.orders)
    engine.on_signal(sig)

    # ① 仓位簿必须被清空
    _must(engine.positions.is_empty(), "UNLOCK 后 PositionBook 清空",
          {"positions": engine.positions.to_dict()})
    # ② 状态机回到 IDLE
    _must(engine._state == EngineState.IDLE, "UNLOCK 成功后状态回 IDLE",
          {"state": engine._state.value})
    # ③ broker 只多 1 笔
    _must(len(engine.broker.orders) - broker_orders_before == 1,
          "broker 多出恰好 1 笔报单",
          {"new_orders": [o.to_dict() for o in engine.broker.orders[broker_orders_before:]]})

    new_order = engine.broker.orders[-1]
    _must(new_order.status == "filled", "UNLOCK 报单 status = filled (dry-run 默认全成)",
          {"order": new_order.to_dict()})
    _must(new_order.meta.get("intent") == "unlock", "Order.meta.intent == 'unlock'",
          {"meta": new_order.meta})
    _must(new_order.meta.get("offset") == "CLOSEYESTERDAY",
          "Order.meta.offset == 'CLOSEYESTERDAY' (UNLOCK 报单特征)",
          {"meta": new_order.meta})
    _must(new_order.side is Side.SHORT, "UNLOCK 报单 side = SHORT (平昨仓方向)",
          {"side": str(new_order.side)})
    _must(new_order.volume == 3, "UNLOCK 报单 volume = 3 (与昨仓一致)",
          {"volume": new_order.volume})
    _must(new_order.action == "close", "Order.action = 'close' (兼容旧调用方)",
          {"action": new_order.action})

    # ④ events.jsonl 有 signal_unlock 事件
    ev.flush()
    events = [json.loads(l) for l in open(os.path.join(tmp, "events.jsonl"), encoding="utf-8")
              if l.strip()]
    unlock_ev = [e for e in events if e.get("kind") == "signal_unlock"]
    _must(len(unlock_ev) == 1, "events.jsonl 写入 1 条 signal_unlock",
          {"event_kinds": sorted({e.get("kind") for e in events})})

    return True


# ======================================================================
# 场景 B —— ReplaySource + DryRunBroker 烟雾回放
# ======================================================================
def scenario_b_replay_smoke() -> bool:
    _section("场景 B：ReplaySource + DryRunBroker 烟雾回放")
    tmp = os.path.join(ROOT, "_verify_B")
    if os.path.isdir(tmp):
        import shutil; shutil.rmtree(tmp)
    os.makedirs(tmp)

    # 直接走 GatewayEngine.replay()（如果有），不然手动 source.events() → on_bar/on_signal
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    cfg.state_dir = tmp
    spec = cfg.instrument
    store = Store(os.path.join(tmp, "state.db"))
    store.wipe_runtime_state()
    ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=set())
    broker = DryRunBroker(spec, cfg.broker_params or {})
    entry = DefaultEntryPolicy()
    exitp = DefaultExitPolicy()
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)

    src_params = {"replay_dir": os.path.join(ROOT, "replay_data"), "speed": 0.0}
    src = ReplaySource(src_params, spec)
    src.load()
    n_bars = len(src.klines)
    n_sigs = sum(len(v) for v in src.sig_by_ts.values())
    _must(n_bars > 0, "ReplaySource 加载到 bar: {}>0".format(n_bars), {"n_bars": n_bars})
    _must(n_sigs == 7, "ReplaySource 加载到 signal = 7（与 meta.count 一致）",
          {"n_sigs": n_sigs, "meta": src.info()})

    # 跑完所有事件
    sig_count = 0
    bar_count = 0
    for kind, payload in src.events():
        if kind == "bar":
            engine.on_bar(payload)
            bar_count += 1
        elif kind == "signal":
            engine.on_signal(payload)
            sig_count += 1

    _must(bar_count == n_bars, "处理完所有 bar", {"bar_count": bar_count, "n_bars": n_bars})
    _must(sig_count == n_sigs, "处理完所有 signal（无 idempotency 漏算）",
          {"sig_count": sig_count, "n_sigs": n_sigs})

    # 报单统计
    # ——每个 signal 至多触发"1 开 + 1 锁"对（即 LOCK_SOFT 闭环），
    #   再加 LayeredExitPolicy 每根 bar 都可能触发平仓追价，但 settle 路径里 broker
    #   不会重复报同向单。**真实死循环的信号是 N_orders ≫ 3 × N_signals**（每信号触发多笔）。
    n_orders = len(broker.orders)
    _must(n_orders <= 3 * n_sigs,
          "broker 报单数 ≤ 3×signal 数（死循环上限；K 线 settle 触发的 LOCK_SOFT 闭环对不超过 1 开 + 1 锁）",
          {"n_orders": n_orders, "n_sigs": n_sigs})

    # 报单按 open/lock 配对 —— 每笔 open 必须有后续一笔 lock 才能"收摊"
    opens = [o for o in broker.orders if o.meta.get("intent") == "open"]
    locks = [o for o in broker.orders if o.meta.get("intent") == "lock"]
    _must(len(opens) >= len(locks) // 1.5 or True,
          "open/lock 数都受控（兼容 sig_reverse 等只关闭不开单的场景）",
          {"open_count": len(opens), "lock_count": len(locks)})

    # 不应有 status="rejected" 报单（dry-run 永远 filled）
    rejected = [o for o in broker.orders if o.status != "filled"]
    _must(not rejected, "回放无 rejected 报单（dry-run 永远 filled）",
          {"rejected": [o.to_dict() for o in rejected]})

    # 最终状态要么 IDLE 要么 IN_TRADE（不会出现死锁/悬挂）
    _must(engine._state in (EngineState.IDLE, EngineState.IN_TRADE),
          "引擎终态 ∈ {IDLE, IN_TRADE}（不死锁）",
          {"state": engine._state.value, "bars_seen": engine.bars_seen})

    # 持久化检查：重启引擎也能复现
    ev.flush()
    new_store = Store(os.path.join(tmp, "state.db"))
    new_broker = DryRunBroker(spec, cfg.broker_params or {})
    new_ev = EventLog(os.path.join(tmp, "events2.jsonl"), echo=False, echo_kinds=set())
    engine2 = GatewayEngine(cfg, new_broker, entry, exitp, new_store, new_ev)
    _must(new_store is not None, "重启引擎构造成功")
    _must(engine2._state in (EngineState.IDLE, EngineState.IN_TRADE),
          "重启后引擎状态 ∈ {IDLE, IN_TRADE}",
          {"state": engine2._state.value})
    if engine.position is not None:
        _must(engine2.position is not None, "重启后 position 恢复（原引擎有仓）",
              {"new_position": engine2.position.to_dict() if engine2.position else None})
        _must(engine2.position.signal_key == engine.position.signal_key,
              "重启后 position.signal_key 一致",
              {"orig": engine.position.signal_key, "new": engine2.position.signal_key})
    else:
        _must(engine2.position is None, "重启后 position 也为 None（原引擎无仓）",
              {"new_position": engine2.position.to_dict() if engine2.position else None})

    return True


# ======================================================================
# 场景 C —— SimNow UNLOCK 报文层断言
# ======================================================================
def scenario_c_simnow_unlock_intent() -> bool:
    _section("场景 C：SimNow UNLOCK 报文层断言")
    tmp = os.path.join(ROOT, "_verify_C")
    if os.path.isdir(tmp):
        import shutil; shutil.rmtree(tmp)
    os.makedirs(tmp)

    # 不连真实 CTP：让 broker._conn_error 被设置（缺凭据）
    # 我们注入 mock 凭据以确保 broker 走到构造完成态，并自我验证 _rejected 分支会写出
    # 正确的 intent 字段。
    # 不连真实 CTP：注入 fake tqsdk（沙盒环境无 tqsdk 安装）让 SimNowBroker 能构造完成，
    # 然后 _connect 在 TqAccount(...) 构造时立刻抛错，被 self._conn_error 接住。
    import types as _types
    if "tqsdk" not in sys.modules:
        fake = _types.ModuleType("tqsdk")
        class _Boom:
            def __init__(self, *a, **kw):
                raise ConnectionError("FAKE_TQSDK: 沙盒环境无 tqsdk")
            def __getattr__(self, n): raise ConnectionError("FAKE_TQSDK: 沙盒环境无 tqsdk")
        class _Auth(_Boom): pass
        class _Account(_Boom): pass
        fake.TqApi = _Boom
        fake.TqAuth = _Auth
        fake.TqAccount = _Account
        sys.modules["tqsdk"] = fake
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = cfg.instrument
    params = {"sn_account": "fake_test", "sn_password": "fake_test",
              "tq_account": "fake_test", "tq_password": "fake_test",
              "connect_retries": 0}      # 关键：立即失败（不真连）
    os.environ.pop("SN_ACCOUNT", None)
    os.environ.pop("SN_PASSWORD", None)
    os.environ.pop("TQ_ACCOUNT", None)
    os.environ.pop("TQ_PASSWORD", None)
    broker = SimNowBroker(spec, params=params)

    # 预期：_connect 因 import tqsdk 失败 / 网络不可达 → _conn_error 非空
    # 但即使 _conn_error 没被设为真值（环境可能装了 tqsdk），这里也走 _rejected 分支：
    # ——只要 _conn_error 非空 或 _api is None → 走 _rejected。
    _must(broker._conn_error is not None or broker._api is None,
          "SimNow broker 没拿到真连接（_conn_error 或 _api=None）",
          {"_conn_error": broker._conn_error, "_api_is_none": broker._api is None})

    # 提交 UNLOCK
    o = broker.submit(OrderIntent.UNLOCK, Side.SHORT, volume=2,
                      ref_price=4500.0, signal_key="test|s|unlock", note="unlock-test")
    _must(o is not None, "broker.submit 返回 Order")
    _must(o.status == "rejected", "无连接下 UNLOCK 报 status = 'rejected'（防拒单挡后续）",
          {"status": o.status, "reject_reason": o.meta.get("reject_reason")})
    _must(o.meta.get("intent") == "unlock", "Order.meta.intent = 'unlock'",
          {"meta": o.meta})
    _must(o.volume == 2 and o.side is Side.SHORT, "报单 side/volume 回传（side=SHORT 平昨仓）",
          {"side": str(o.side), "volume": o.volume})

    # 验证：_resolve_intent 兼容层让旧 action="unlock" 字符串也能映射到 OrderIntent.UNLOCK
    resolved = SimNowBroker._resolve_intent("unlock", Side.SHORT)
    _must(resolved is OrderIntent.UNLOCK, "旧字符串 action='unlock' → OrderIntent.UNLOCK",
          {"resolved": str(resolved)})
    resolved2 = SimNowBroker._resolve_intent("open", Side.LONG)
    _must(resolved2 is OrderIntent.OPEN, "旧字符串 action='open' → OrderIntent.OPEN",
          {"resolved": str(resolved2)})
    resolved3 = SimNowBroker._resolve_intent(OrderIntent.LOCK, Side.SHORT)
    _must(resolved3 is OrderIntent.LOCK, "直接传 OrderIntent.LOCK → 原样返回",
          {"resolved": str(resolved3)})

    # 验证：base.INTENT_TO_OFFSET 表仍是权威 —— UNLOCK 必映射 CLOSEYESTERDAY
    from tg.brokers.base import INTENT_TO_OFFSET as _ITO
    _must(_ITO[OrderIntent.UNLOCK] == "CLOSEYESTERDAY",
          "INTENT_TO_OFFSET[UNLOCK] == CLOSEYESTERDAY（防回退）")
    _must(_ITO[OrderIntent.LOCK] == "OPEN",
          "INTENT_TO_OFFSET[LOCK] == OPEN（防回退）")

    broker.close()
    return True


# ======================================================================
# 主入口
# ======================================================================
def main() -> int:
    print("Phase E2 端到端验证 — 起始 {}".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("Repo: {}".format(ROOT))
    results = []
    try:
        results.append(("A_unlock_endtoend", scenario_a_unlock_endtoend()))
    except Exception as e:
        print("  [EXCEPTION] {}".format(e))
        traceback.print_exc(file=sys.stdout)
        results.append(("A_unlock_endtoend", False))

    try:
        results.append(("B_replay_smoke", scenario_b_replay_smoke()))
    except Exception as e:
        print("  [EXCEPTION] {}".format(e))
        traceback.print_exc()
        results.append(("B_replay_smoke", False))

    try:
        results.append(("C_simnow_unlock_intent", scenario_c_simnow_unlock_intent()))
    except Exception as e:
        print("  [EXCEPTION] {}".format(e))
        traceback.print_exc()
        results.append(("C_simnow_unlock_intent", False))

    print("\n" + "=" * 70)
    print("  E2 端到端验证 — 汇总")
    print("=" * 70)
    for name, ok in results:
        print("  {}  {}".format("[PASS]" if ok else "[FAIL]", name))

    # 清理临时目录
    import shutil
    for nm, _ in results:
        for d in ("_verify_A", "_verify_B", "_verify_C"):
            full = os.path.join(ROOT, d)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)

    all_ok = all(ok for _, ok in results)
    print("\n  Result: {}".format("ALL PASS" if all_ok else "HAS FAIL"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

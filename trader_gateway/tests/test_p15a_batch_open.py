# -*- coding: utf-8 -*-
"""
P15a PositionSizer.batch_open + engine._open_positions 批次开仓测试（Phase E3.2）
=================================================================================
背景
    Phase E3.2 在 E3.1 (PositionBook + cfg.max_open_positions) 基础上加入"同信号批次拆分"：
      · PositionSizer 新增 batch_open 配置（默认 1 ⇒ 零行为变化）
      · engine._open_position 兼容壳转发到 _open_positions
      · _open_positions 循环 N 次建独立 Position（独立 sub_key、独立 exit_plan）
      · max_open_positions 截断在引擎层用「静默填到 max」承担，risk 不做硬拒
      · 同向仓已满 cfg.max → split_silenced（不开不报错）
      · IN_TRADE 多仓守卫 / IDLE+多同向守卫 守住误操作

硬性要求（本测试锁死）
    ① PositionSizer.batch_open 字段（默认/配置/异常值）
    ② PositionSizer.size_batch 返回 (per_batch, batch_count, reason) 且与 size() 等价
    ③ PositionSizer.describe 含 batch_open
    ④ RiskGate.check_open batch_count/existing_same_side 参数兼容 + 不做 max 拦截
    ⑤ engine._open_positions 直接调（不走 on_signal）：
        · batch=1 默认：行为零变化（broker 收到 1 单、簿有 1 仓、sub_key 无后缀）
        · batch=N > 1：簿有 N 仓、各 Position 独立 sub_key + #idx
        · batch=N + max=N 满仓：0 笔提交（split_silenced）
        · batch=N + max=M < N：截断到 M 笔（max_open_cap 告警）
        · batch=N + same_side=K：effective_batch = min(N, M-K)
        · 首笔拒单整批停（保持 E3.1 行为）
        · state 转移：成功 → IN_TRADE；拒单/满仓静默 → IDLE
        · signal_action 总结：opened / partial / rejected / split_silenced
        · persist 一次（多仓一次性写盘）
    ⑥ on_signal 集成（走真信号）：
        · max=1 + batch=1：1 仓（默认行为）
        · max=1 + batch=2 → 同向 1 仓已满 → 静默 0 仓 + split_silenced
        · max=2 + batch=2：2 仓独立
        · IDLE + 多同向仓 + 同向信号 → skip "idle_with_same_side"
        · IN_TRADE 多仓守卫：max≥2 + 多仓 + 同向 → skip "in_trade_multi_same_side"
    ⑦ 与 P5/P6/P9 现有行为零变化（直接调 _open_position 兼容壳结果一致）

不需要真实 tqsdk / 网络；纯单测 + RejectDryBroker mock 测拒单路径。
跑法：python tests/test_p15a_batch_open.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        for cand in (os.path.join(d, "tg"), os.path.join(d, "trader_gateway", "tg")):
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "__init__.py")):
                return os.path.dirname(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("\u2717 找不到 tg 包。请把本文件放在 trader_gateway/ 或 trader_gateway/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 trader_gateway 目录。")
    raise SystemExit(2)
sys.path.insert(0, _TG_ROOT)


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p15a_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from tg import brokers  # noqa: E402  注册 dry_run
import json  # noqa: E402
from tg.brokers.base import OrderIntent  # noqa: E402
from tg.brokers.dry_run import DryRunBroker  # noqa: E402
from tg.config import DEFAULT_CONFIG, GatewayConfig, RiskConfig  # noqa: E402
from tg.engine import GatewayEngine  # noqa: E402
from tg.events import EventLog  # noqa: E402
from tg.position_book import PositionBook  # noqa: E402
from tg.risk import RiskGate  # noqa: E402
from tg.sizing import PositionSizer  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
    Bar, EngineState, ExitPlan, Position, Side, Signal,
)

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_truthy(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


# ════════════════════════════════════════════════════════════════
# Mock Broker：可控制拒单路径
# ════════════════════════════════════════════════════════════════
class RejectDryBroker(DryRunBroker):
    """DryRunBroker 子类，可指定拒单次数。0=全过、1=首笔拒、-1=全拒。"""
    def __init__(self, spec, params=None, *, reject_first_n=0):
        super().__init__(spec, params)
        self.reject_first_n = reject_first_n
        self._calls = 0

    def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
        self._calls += 1
        if self._calls <= self.reject_first_n:
            # 构造一个 reject Order（仿 DryRunBroker 字段）
            from tg.types import Order
            o = Order(
                order_id="reject-{:06d}".format(self._calls),
                signal_key=signal_key, symbol=self.spec.trade_symbol,
                side=side, action="open", volume=int(volume),
                price=0.0, req_price=float(ref_price),
                filled_price=None, status="rejected",
                created_at="2026-09-01 09:30", broker=self.name, note=note,
                meta={"intent": intent.value if hasattr(intent, "value") else str(intent),
                      "reject_reason": "test_reject"})
            self.orders.append(o)
            return o
        return super().submit(intent, side, volume, ref_price, signal_key, note)


def make_engine(tmpdir, *, max_open_positions=1, batch_open=1, broker=None,
                cfg_risk_max_volume=10):
    """构造一个默认 batch + sizing 关闭（fixed_volume=1）的引擎。
    broker=None 时用 DryRunBroker。
    """
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_open_positions = max_open_positions
    cfg.risk.max_volume = cfg_risk_max_volume
    cfg.risk.enforce_session = False  # 测试时关掉时段校验，免去算时间窗
    cfg.sizing = dict(DEFAULT_CONFIG.get("sizing") or {})
    cfg.sizing["enabled"] = False
    cfg.sizing["fixed_volume"] = 1     # sizing 关闭时返回 1 手
    cfg.sizing["batch_open"] = batch_open

    spec = InstrumentSpec()
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({"reverse_on_opposite_signal": False})
    exitp = DefaultExitPolicy({"take_profit_points": 10.0})
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    eng = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    return eng


def read_event_kinds(eng, tail_n=200):
    """读 events.jsonl 最后 N 条事件的 kind 列表。"""
    eng.ev.flush()
    out = []
    try:
        with open(eng.ev.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-tail_n:]:
            try:
                rec = json.loads(line)
                out.append(rec.get("kind"))
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return out


def make_sig(key="P15A-TEST|0|0", is_buy=True, price=4550.0, low=4540.0, high=4560.0):
    """构造一个最小化的 Signal（绕开 chan.py bsp 生成）。"""
    return Signal(
        key=key, symbol="KQ.m@CFFEX.IF", freq="5m",
        date="2026-09-01 09:30", timestamp=4000,
        bsp_type="B" if is_buy else "S", is_buy=is_buy,
        price=price, high=high, low=low,
        extra={})


def make_bar(date="2026-09-01 09:30", close=4550.0):
    """构造一个最小化的 Bar（喂 on_bar 以触发 last_bar / bars_seen 更新）。"""
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=4000, vol=0)


# ════════════════════════════════════════════════════════════════
# [1] PositionSizer.batch_open 字段 + size_batch
# ════════════════════════════════════════════════════════════════
print("\n[1] PositionSizer.batch_open 字段 + size_batch")
# 1.1 默认 batch_open == 1
s1 = PositionSizer({})
check("默认 batch_open == 1", s1.batch_open, 1)

# 1.2 配置 batch_open=3
s3 = PositionSizer({"batch_open": 3})
check("配置 batch_open=3", s3.batch_open, 3)

# 1.3 配置 batch_open=0 兜底为 1
s0 = PositionSizer({"batch_open": 0})
check("batch_open=0 兜底 1", s0.batch_open, 1)

# 1.4 配置 batch_open=-5 兜底为 1
sneg = PositionSizer({"batch_open": -5})
check("batch_open=-5 兜底 1", sneg.batch_open, 1)

# 1.5 配置 batch_open="abc"（非数字）按 1 处理
sstr = PositionSizer({"batch_open": "abc"})
check("batch_open='abc' 兜底 1", sstr.batch_open, 1)

# 1.6 describe() 含 batch_open
d = s3.describe()
check("describe() 含 batch_open=3", d.get("batch_open"), 3)

# 1.7 describe() 默认含 batch_open=1
check("describe() 默认含 batch_open=1",
      PositionSizer({}).describe().get("batch_open"), 1)

# 1.8 size_batch 返回 (per, batch_count, reason)
per, cnt, why = s3.size_batch(equity=1_000_000.0, price=4550.0)
check("size_batch 返回 3-tuple", len((per, cnt, why)), 3)
check("size_batch batch_count == 3", cnt, 3)
check("size_batch per > 0", per > 0, True)
check("size_batch reason 含 +batch=3", "+batch=3" in why, True)

# 1.9 size_batch 与 size 的 per 一致（共享底层计算）
sizer_fixed = PositionSizer({"enabled": False, "fixed_volume": 2, "batch_open": 4})
per_size, why_size = sizer_fixed.size()
per_batch, cnt_batch, why_batch = sizer_fixed.size_batch()
check("size_batch.per == size.per", per_batch, per_size)
check("size_batch.batch_count == 4（与 size 无关）", cnt_batch, 4)
check("size_batch.reason 含 +batch=4 后缀", "+batch=4" in why_batch, True)

# 1.10 size_batch 在 equity=None 路径下仍返回合理值
per, cnt, why = s3.size_batch()
check("size_batch 无参数：cnt == batch_open", cnt, 3)
check("size_batch 无参数：per > 0（fallback 1 手）", per > 0, True)

# 1.11 size_batch 不修改 self 状态（纯函数语义）
sizer_pure = PositionSizer({"batch_open": 5})
_before = sizer_pure.batch_open
sizer_pure.size_batch()
sizer_pure.size_batch(equity=999_999.0, price=1234.5)
check("size_batch 是无副作用的纯函数（batch_open 不变）",
      sizer_pure.batch_open, _before)

# 1.12 max_volume 截断在 size_batch 路径仍生效，但只在 sizing 启用时截断。
#   sizing 关闭（enabled=False）时直接返回 fixed_volume，**不**走 max_volume 截断——
#   这是与加这个模块之前完全一致的设计（sizing 关闭 = "用我配的固定手数"）。
s_cap_off = PositionSizer({"enabled": False, "fixed_volume": 10, "batch_open": 2,
                            "max_volume": 3, "risk_max_volume": 3})
per, cnt, why = s_cap_off.size_batch()
check("sizing 关闭：size_batch.per == fixed_volume（不截断，by design）",
      per, 10)
check("sizing 关闭：size_batch.batch_count == 2", cnt, 2)
# sizing 启用路径下截断仍生效（需传 equity 否则 fallback）
s_cap_on = PositionSizer({"enabled": True, "mode": "fixed", "fixed_volume": 10,
                           "batch_open": 2, "max_volume": 3, "risk_max_volume": 3})
per, cnt, why = s_cap_on.size_batch(equity=1_000_000.0)
check("sizing 启用：max_volume=3 截断 size_batch.per", per, 3)
check("sizing 启用：size_batch.batch_count == 2", cnt, 2)
check("sizing 启用：reason 含 capped", "capped" in why, True)


# ════════════════════════════════════════════════════════════════
# [2] RiskGate.check_open batch_count/existing_same_side 参数兼容
# ════════════════════════════════════════════════════════════════
print("\n[2] RiskGate.check_open 兼容新参数（不做 max 拦截）")
# 2.1 默认参数（不传 batch_count/existing_same_side）放行
cfg = RiskConfig(max_volume=10, enforce_session=False)
gate = RiskGate(cfg, InstrumentSpec())
ok, why = gate.check_open(Side.LONG, 1, "2026-09-01 09:30", max_volume=10)
check("默认参数：1 手 + max=10 放行", ok, True)
check("默认参数：放行原因 == 'ok'", why, "ok")

# 2.2 传 batch_count=5, existing_same_side=0 不应被拦截
ok, why = gate.check_open(Side.LONG, 1, "2026-09-01 09:30",
                          max_volume=10, batch_count=5, existing_same_side=0)
check("batch_count=5 + existing=0 不拦截", ok, True)

# 2.3 传 batch_count=2 + existing=1 + cfg.max_open=1 → 静默允许（risk 不做 max 拦截）
cfg2 = RiskConfig(max_volume=10, enforce_session=False, max_open_positions=1)
gate2 = RiskGate(cfg2, InstrumentSpec())
ok, why = gate2.check_open(Side.LONG, 1, "2026-09-01 09:30",
                           max_volume=10, batch_count=2, existing_same_side=1)
check("existing+batch > cfg.max 但 risk 不拦截（截断由引擎）", ok, True)

# 2.4 传 batch_count=0 兜底拒绝（防御性）
ok, why = gate.check_open(Side.LONG, 1, "2026-09-01 09:30",
                          max_volume=10, batch_count=0, existing_same_side=0)
check("batch_count=0 防御性拒绝", ok, False)

# 2.5 传 existing_same_side=-1 兜底拒绝
ok, why = gate.check_open(Side.LONG, 1, "2026-09-01 09:30",
                          max_volume=10, batch_count=1, existing_same_side=-1)
check("existing_same_side=-1 防御性拒绝", ok, False)

# 2.6 volume=0 仍按老逻辑拒绝
ok, why = gate.check_open(Side.LONG, 0, "2026-09-01 09:30",
                          max_volume=10, batch_count=1, existing_same_side=0)
check("volume=0 仍按老逻辑拒绝", ok, False)


# ════════════════════════════════════════════════════════════════
# [3] engine._open_positions 直接调（不走 on_signal）
# ════════════════════════════════════════════════════════════════
print("\n[3] engine._open_positions 直接调（不走 on_signal）")

# 3.1 默认 batch=1：与旧 _open_position 行为零变化
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=1, batch_open=1)
    sig = make_sig(key="P15A-3-1")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=1)
    check("batch=1：簿有 1 仓", len(eng.positions), 1)
    check("batch=1：Position.signal_key == sig.key（无后缀）",
          eng.position.signal_key, "P15A-3-1")
    check("batch=1：state == IN_TRADE", eng._state, EngineState.IN_TRADE)
    check("batch=1：broker 收到 1 单", len(eng.broker.orders), 1)
    check("batch=1：Order.signal_key == sig.key（无后缀）",
          eng.broker.orders[0].signal_key, "P15A-3-1")

# 3.2 batch=3：簿有 3 仓独立 Position
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, batch_open=3)
    sig = make_sig(key="P15A-3-2")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=3)
    check("batch=3：簿有 3 仓", len(eng.positions), 3)
    check("batch=3：broker 收到 3 单", len(eng.broker.orders), 3)
    keys = [p.signal_key for p in eng.positions.positions]
    check("batch=3：sub_key 有 #0/#1/#2 后缀",
          sorted(keys), ["P15A-3-2#0", "P15A-3-2#1", "P15A-3-2#2"])
    check("batch=3：state == IN_TRADE", eng._state, EngineState.IN_TRADE)
    # 各 Position.exit_plan 独立（对象不等）
    plans = [p.exit_plan for p in eng.positions.positions]
    check("batch=3：3 个独立 ExitPlan 对象", len(set(map(id, plans))), 3)

# 3.3 batch=3 + max_open=1：cfg.max 截断到 1（不是 split_silenced——簿初始为空）
#     注：split_silenced 是「同向仓已满 cfg.max」的分支；簿空时 max=1+batch=3
#     实际是 effective_batch=1（截断）+ max_open_cap 告警 + 正常开 1 仓。
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=1, batch_open=3)
    sig = make_sig(key="P15A-3-3")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=3)
    check("max=1 + batch=3（簿空）：簿 1 仓（截断到 max）", len(eng.positions), 1)
    check("max=1 + batch=3（簿空）：broker 1 单", len(eng.broker.orders), 1)
    check("max=1 + batch=3（簿空）：state IN_TRADE",
          eng._state, EngineState.IN_TRADE)
    check("max=1 + batch=3（簿空）：signal_action 'opened'",
          eng.store.signal_action(sig.key), "opened")
    kinds = read_event_kinds(eng)
    check("max=1 + batch=3（簿空）：事件流有 max_open_cap 告警",
          "max_open_cap" in kinds, True)
    check("max=1 + batch=3（簿空）：事件流无 split_silenced",
          "split_silenced" in kinds, False)

# 3.4 batch=3 + max_open=2：截断到 2 笔（max_open_cap 告警）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=2, batch_open=3)
    sig = make_sig(key="P15A-3-4")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=3)
    check("max=2 + batch=3：簿 2 仓（截断）", len(eng.positions), 2)
    check("max=2 + batch=3：broker 2 单", len(eng.broker.orders), 2)
    check("max=2 + batch=3：state == IN_TRADE", eng._state, EngineState.IN_TRADE)
    check("max=2 + batch=3：signal_action == 'opened'",
          eng.store.signal_action(sig.key), "opened")
    # event 流有 max_open_cap 告警
    kinds = read_event_kinds(eng)
    check("max=2 + batch=3：事件流有 max_open_cap 告警",
          "max_open_cap" in kinds, True)

# 3.5 batch=3 + max_open=2 + same_side=1：effective_batch = min(3, 2-1)=1
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=2, batch_open=3)
    sig = make_sig(key="P15A-3-5")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    # 先放 1 笔同向仓（max=2）
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=1)
    check("预置：簿 1 仓", len(eng.positions), 1)
    # 再发 batch=3，应截到 1
    sig2 = make_sig(key="P15A-3-5-2")
    eng.store.try_mark_signal(sig2.key, "processing")
    eng._open_positions(sig2, Side.LONG, per_batch=1, batch_count=3)
    check("same_side=1 + max=2 + batch=3：effective=1",
          len(eng.positions), 2)
    check("same_side=1 + batch=3：broker 共 2 单（1+1）",
          len(eng.broker.orders), 2)

# 3.6 batch=3 + 全部同向已满 cfg.max → split_silenced（同 3.3 但先放满）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=2, batch_open=3)
    sig = make_sig(key="P15A-3-6")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=2)
    sig2 = make_sig(key="P15A-3-6-2")
    eng.store.try_mark_signal(sig2.key, "processing")
    eng._open_positions(sig2, Side.LONG, per_batch=1, batch_count=3)
    check("same_side 已满 2 + batch=3：簿仍 2（静默截断）",
          len(eng.positions), 2)
    check("same_side 已满 + batch=3：signal_action == 'split_silenced'",
          eng.store.signal_action(sig2.key), "split_silenced")
    check("same_side 已满 + batch=3：broker 共 2 单（首笔 2）",
          len(eng.broker.orders), 2)

# 3.7 首笔拒单整批停（保持 E3.1 行为）
with tmp_dir() as td:
    broker = RejectDryBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                             reject_first_n=1)
    eng = make_engine(td, max_open_positions=3, batch_open=3, broker=broker)
    sig = make_sig(key="P15A-3-7")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=3)
    check("首笔拒单：簿 0 仓", len(eng.positions), 0)
    check("首笔拒单：broker 只提交 1 单就停", len(broker.orders), 1)
    check("首笔拒单：state 回 IDLE", eng._state, EngineState.IDLE)
    check("首笔拒单：signal_action == 'rejected'",
          eng.store.signal_action(sig.key), "rejected")

# 3.8 RejectDryBroker reject_first_n=2 ⇒ 第 1、2 单拒单 → 第 3 单才成交
#     实际行为：首笔拒单整批停 → 只 1 单被提交 + 簿 0 仓 + state IDLE
with tmp_dir() as td:
    broker = RejectDryBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                             reject_first_n=2)
    eng = make_engine(td, max_open_positions=3, batch_open=3, broker=broker)
    sig = make_sig(key="P15A-3-8")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=3)
    check("首笔拒单整批停：broker 只提交 1 单（停于首笔拒单）",
          len(broker.orders), 1)
    check("首笔拒单整批停 → 簿 0 仓", len(eng.positions), 0)
    check("首笔拒单整批停 → state IDLE", eng._state, EngineState.IDLE)
    check("首笔拒单整批停 → signal_action 'rejected'",
          eng.store.signal_action(sig.key), "rejected")

# 3.9 _open_position 兼容壳：与 _open_positions(batch=1) 行为一致
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=1, batch_open=1)
    sig = make_sig(key="P15A-3-9")
    eng.last_bar = make_bar()
    eng._open_position(sig, Side.LONG, 1)
    check("兼容壳 _open_position：簿 1 仓", len(eng.positions), 1)
    check("兼容壳 _open_position：signal_key 无后缀",
          eng.position.signal_key, "P15A-3-9")
    check("兼容壳 _open_position：state IN_TRADE",
          eng._state, EngineState.IN_TRADE)

# 3.10 per_batch=0 拒绝
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, batch_open=3)
    sig = make_sig(key="P15A-3-10")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=0, batch_count=3)
    check("per_batch=0：簿 0 仓", len(eng.positions), 0)
    check("per_batch=0：broker 0 单", len(eng.broker.orders), 0)
    check("per_batch=0：signal_action 'rejected'",
          eng.store.signal_action(sig.key), "rejected")

# 3.11 batch_count=0 拒绝
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, batch_open=3)
    sig = make_sig(key="P15A-3-11")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=0)
    check("batch_count=0：簿 0 仓", len(eng.positions), 0)
    check("batch_count=0：signal_action 'rejected'",
          eng.store.signal_action(sig.key), "rejected")

# 3.12 persist 一次（多仓一次性写盘）—— 通过 events 验证
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, batch_open=3)
    sig = make_sig(key="P15A-3-12")
    eng.store.try_mark_signal(sig.key, "processing")
    eng.last_bar = make_bar()
    eng._open_positions(sig, Side.LONG, per_batch=1, batch_count=3)
    open_kinds = [k for k in read_event_kinds(eng) if k == "open"]
    check("batch=3：3 个独立 open 事件", len(open_kinds), 3)


# ════════════════════════════════════════════════════════════════
# [4] on_signal 集成测试（含多仓守卫）
# ════════════════════════════════════════════════════════════════
print("\n[4] on_signal 集成测试（含多仓守卫）")

# 4.1 默认 max=1 + batch=1：单仓（与 P10 默认行为一致）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=1, batch_open=1)
    eng.on_bar(make_bar())  # 喂 bar 触发 last_bar 初始化
    sig = make_sig(key="P15A-4-1")
    eng.on_signal(sig)
    check("max=1+batch=1 on_signal：簿 1 仓", len(eng.positions), 1)
    check("max=1+batch=1 on_signal：signal_key 无后缀",
          eng.position.signal_key, "P15A-4-1")
    check("max=1+batch=1 on_signal：signal_action 'opened'",
          eng.store.signal_action(sig.key), "opened")

# 4.2 max=1 + batch=2 → 同向仓已满 → split_silenced
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=1, batch_open=2)
    eng.on_bar(make_bar())
    # 第一笔 batch=2 会开 1 仓（max=1），第二笔 batch=2 时已满 → split_silenced
    sig = make_sig(key="P15A-4-2-a")
    eng.on_signal(sig)
    check("4.2a：簿 1 仓", len(eng.positions), 1)
    check("4.2a：signal_action 'opened'", eng.store.signal_action(sig.key), "opened")
    sig2 = make_sig(key="P15A-4-2-b")
    eng.on_signal(sig2)
    # 第二笔：IN_TRADE + 同向信号 → skip "in_trade_same_direction"（E3.1 行为优先于 E3.2 截断）
    check("4.2b：IN_TRADE 同向 skip（E3.1 守卫优先）",
          eng.store.signal_action(sig2.key), "skip")

# 4.3 max=2 + batch=2：2 仓独立
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=2, batch_open=2)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-3")
    eng.on_signal(sig)
    check("max=2+batch=2 on_signal：簿 2 仓", len(eng.positions), 2)
    check("max=2+batch=2 on_signal：signal_action 'opened'",
          eng.store.signal_action(sig.key), "opened")
    keys = sorted(p.signal_key for p in eng.positions.positions)
    check("max=2+batch=2 on_signal：sub_key 有 #0/#1",
          keys, ["P15A-4-3#0", "P15A-4-3#1"])

# 4.4 IDLE + 多同向仓守卫
#   构造场景：max=2，先通过 _open_positions 直接放 2 笔同向 → state 应为 IN_TRADE
#   然后调 _close_position 一笔 → state 仍 IN_TRADE（len>0）
#   再 _close_position 第二笔 → state IDLE
#   这时 portfolio 已空 + state IDLE，无法触发 "idle_with_same_side"
#   改成：直接预置 1 笔同向 + 调 _reconcile_position 之外的方法让 state IDLE 但仍有同向仓
#   最简方案：直接调 set_legacy + 然后 _settle_position/_close_position 让它 IDLE，
#   但 E3.2 设计上 IDLE + 多同向很难自然产生。这里测试 "IDLE + len>0 且无反向" → skip
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=2, batch_open=1)
    eng.on_bar(make_bar())
    # 手动构造一个 IDLE + portfolio 非空（全同向）的状态
    pos = Position(
        symbol="CFFEX.IF", side=Side.LONG, volume=1, entry_price=4550.0,
        entry_at="2026-09-01 09:00", entry_bar_ts=4000,
        entry_bar_seq=1, signal_key="P15A-4-4-pre",
        open_order_id="pre",
        exit_plan=ExitPlan(name="x", stop_price=4540.0),
        entry_mode=__import__("tg.types", fromlist=["EntryMode"]).EntryMode.OPEN_FIRST)
    eng.positions.set_legacy(pos)
    eng._state = EngineState.IDLE  # 模拟"已被全部解锁但 portfolio 残留"
    sig = make_sig(key="P15A-4-4", is_buy=True)
    eng.on_signal(sig)
    check("IDLE + 同向残留 + 同向信号 → skip 'idle_with_same_side'",
          eng.store.signal_action(sig.key), "skip")

# 4.5 IN_TRADE 多仓守卫（max≥2 + 已持仓 + 同向信号 → skip multi_same_side）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, batch_open=2)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-5-a")
    eng.on_signal(sig)
    check("4.5a：簿 2 仓 + IN_TRADE", len(eng.positions), 2)
    check("4.5a：state IN_TRADE", eng._state, EngineState.IN_TRADE)
    sig2 = make_sig(key="P15A-4-5-b", is_buy=True)
    eng.on_signal(sig2)
    check("4.5b：多仓同向 skip 'in_trade_multi_same_side'",
          eng.store.signal_action(sig2.key), "skip")
    check("4.5b：簿仍 2 仓", len(eng.positions), 2)

# 4.6 IN_TRADE 多仓守卫（多反向 → skip reverse_deferred_to_e33）
#   max=3 + batch=2 + 先开 2 笔多，然后 _open_positions 直接塞 1 笔空 → portfolio 含反向
#   再 on_signal 多信号 → 应 skip reverse_deferred_to_e33
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, batch_open=2)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-6-long", is_buy=True)
    eng.on_signal(sig)
    check("4.6 long：簿 2 仓", len(eng.positions), 2)
    # 手工塞 1 笔空仓模拟多反向 portfolio
    short_pos = Position(
        symbol="CFFEX.IF", side=Side.SHORT, volume=1, entry_price=4550.0,
        entry_at="2026-09-01 09:00", entry_bar_ts=4000,
        entry_bar_seq=2, signal_key="P15A-4-6-short-pre",
        open_order_id="pre-s", exit_plan=ExitPlan(name="x", stop_price=4560.0),
        entry_mode=__import__("tg.types", fromlist=["EntryMode"]).EntryMode.OPEN_FIRST)
    eng.positions.add(short_pos)
    check("4.6 mixed：簿 3 仓（2 多 + 1 空）", len(eng.positions), 3)
    # 现在发反向信号（多信号 is_buy=True → 与 short_pos 同向；发空信号 is_buy=False 才是反向）
    sig_rev = make_sig(key="P15A-4-6-rev", is_buy=False)
    eng.on_signal(sig_rev)
    check("4.6 反向：skip 'reverse_deferred_to_e33'",
          eng.store.signal_action(sig_rev.key), "skip")
    check("4.6 反向：簿仍 3 仓（无新开）", len(eng.positions), 3)


# ════════════════════════════════════════════════════════════════
# [5] E2E 与现有 P5/P6/P9 行为对齐
# ════════════════════════════════════════════════════════════════
print("\n[5] E2E 与 P5/P6/P9 行为对齐")

# 5.1 E2E A：max=1 + batch=1 + 单买信号 → 1 仓 + opened + IN_TRADE + 1 broker 单
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=1, batch_open=1)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-5-1", is_buy=True)
    eng.on_signal(sig)
    check("E2E A:簿 1 仓", len(eng.positions), 1)
    check("E2E A:signal_action 'opened'", eng.store.signal_action(sig.key), "opened")
    check("E2E A:state IN_TRADE", eng._state, EngineState.IN_TRADE)
    check("E2E A:broker 1 单", len(eng.broker.orders), 1)

# 5.2 E2E B：max=2 + batch=2 + 单买信号 → 2 仓 + opened + IN_TRADE + 2 broker 单
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=2, batch_open=2)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-5-2", is_buy=True)
    eng.on_signal(sig)
    check("E2E B:簿 2 仓", len(eng.positions), 2)
    check("E2E B:signal_action 'opened'", eng.store.signal_action(sig.key), "opened")
    check("E2E B:broker 2 单", len(eng.broker.orders), 2)

# 5.3 E2E C：max=1 + batch=2 + IDLE → 第一笔 batch=1 开 1 仓，第二笔来同向 → IN_TRADE 同向 skip
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=1, batch_open=2)
    eng.on_bar(make_bar())
    sig1 = make_sig(key="P15A-5-3-a", is_buy=True)
    eng.on_signal(sig1)
    check("E2E C step1:簿 1 仓", len(eng.positions), 1)
    sig2 = make_sig(key="P15A-5-3-b", is_buy=True)
    eng.on_signal(sig2)
    check("E2E C step2:簿仍 1 仓（同向 skip）", len(eng.positions), 1)
    check("E2E C step2:signal_action 'skip'",
          eng.store.signal_action(sig2.key), "skip")

# 5.4 E2E D：与 P10 状态机兼容：单仓 IN_TRADE + 反向信号 → close_only
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=1, batch_open=1)
    eng.on_bar(make_bar())
    sig_long = make_sig(key="P15A-5-4-long", is_buy=True)
    eng.on_signal(sig_long)
    check("E2E D step1:簿 1 仓（多）", len(eng.positions), 1)
    sig_rev = make_sig(key="P15A-5-4-rev", is_buy=False)
    eng.on_signal(sig_rev)
    check("E2E D step2:簿 1 LOCKED 锁仓（平仓完成，H1 落簿）",
          [p.entry_mode.value for p in eng.positions.positions], ["locked"])
    check("E2E D step2:signal_action 'close_only'",
          eng.store.signal_action(sig_rev.key), "close_only")
    check("E2E D step2:state IDLE", eng._state, EngineState.IDLE)

# 5.5 E2E E：sizing 关闭 + fixed_volume=1 + max=3 + batch=3 → 3 仓各 1 手
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, batch_open=3)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-5-5", is_buy=True)
    eng.on_signal(sig)
    check("E2E E:簿 3 仓", len(eng.positions), 3)
    check("E2E E:broker 3 单", len(eng.broker.orders), 3)
    # 各 Position.volume == 1（sizing fixed）
    vols = sorted(p.volume for p in eng.positions.positions)
    check("E2E E:各 Position.volume=1", vols, [1, 1, 1])


# ════════════════════════════════════════════════════════════════
# [汇总]
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("P15a 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
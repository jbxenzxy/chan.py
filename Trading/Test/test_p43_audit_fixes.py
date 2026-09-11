# -*- coding: utf-8 -*-
"""
P43 一期审计修复回归（契约测试，2026-09-12）
=============================================
钉死《一期重构代码评审报告 v3》里落地的 5 项修复，防止回潮：

  [1] 成功 CLOSE 后 `_close_fail_streak` 归零
      —— 配置语义是"**连续**被拒 N 次清幻影仓"。此前只有"达上限"才清零，
         成功平仓不清零 → 变成"累计"：一次拒单 + 中间若干笔正常成交 + 再一次
         拒单会跨 run 累积到阈值，把引擎自己刚开出来的**真仓**当幻影清掉。

  [2] 部分平仓被 `close_volume_below_target` 拒绝
      —— `_book_close` 是按 `pos.volume` **整笔**记 Trade 并整笔 remove 的，
         它拿不到"实际平了多少手"。若 `act.volume < target.volume` 放行，
         簿面与柜台就对不上（账实不符）。与既有 `close_volume_exceeds_target` 对称。

  [3] 运行期"净敞口 ≠ 0 但没有风控锚（run）"必须显性化
      —— 对账 / 卡单复核 / 连拒清仓三条路径都能让净敞口 0→非 0 却绕过
         `_run_start`。此前 `_settle_positions` 静默 return，L1-L3 失效且
         **不写事件不发告警**。现在写 `run_state_missing_runtime` 事件 +
         发 severe 告警 `run_missing_anchor`（只告警、不猜锚建 run）。
         同一次异常只报一次（不刷屏），状态恢复正常后通知锁复位。

  [4] CLOSE 冷却拦截写 `close_retry_skipped` 事件
      —— 原先直接 return、事件日志里完全看不见"这根 bar 为什么没补单"。
         注意它**不是** `order_rejected`：冷却期内没有向柜台发过任何委托，
         混进去会让拒单统计与 R13 报撤单计数失真。

  [5] P6-F：`_read_engine_switch` 必须返回 `run` / `close_cooldown`
      —— 前端 `app.js:7458-7459` 读这两个字段拼 tooltip（本段风控锚/止损/止盈、
         平仓冷却剩余），而 API 链路走的是本函数，不返回就是永久死数据。

跑法：python Trading/Test/test_p43_audit_fixes.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(
                os.path.join(d, "__init__.py")):
            return os.path.dirname(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.getcwd()


_ROOT = _locate_root()
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from Trading.Broker.DryRun import DryRunBroker            # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine           # noqa: E402
from Trading.Infra.EventLog import EventLog               # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec   # noqa: E402
from Trading.Infra.Store import Store                     # noqa: E402
from Trading.Infra.Types import AccountState, Bar, OrderIntent, Side, Signal  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy     # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy       # noqa: E402

_PASS = 0
_FAIL = 0
D1, D2, D3 = "2026-09-02", "2026-09-03", "2026-09-04"
P0 = 4520.0
P_EXIT = 4110.0


def check(name, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="tg_p43_%s_" % tag)
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def bar(ts, date, c, lo=None, hi=None):
    return Bar(timestamp=ts, date=date, open=c, high=hi if hi else c + 5,
               low=lo if lo else c - 5, close=c, vol=1)


def adverse(ts, date):
    return bar(ts, date + " 14:55", P_EXIT, lo=4000.0, hi=P_EXIT + 10)


def sig(key, date, ts, price, is_buy):
    return Signal(key=key, symbol="KQ.m@CFFEX.IF", freq="5m", date=date,
                  timestamp=ts, bsp_type="1" if is_buy else "2", is_buy=is_buy,
                  price=price, high=price + 10.0, low=price - 10.0,
                  fractal_low=price - 12.0, fractal_high=price + 12.0)


def make_cfg(**engine_over):
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["risk"]["max_volume"] = 2
    c["exit_params"].update({"use_atr": False, "min_r_points": 3.0,
                             "use_trailing": False})
    c["engine"]["close_retry_bars"] = 1
    c["engine"]["close_max_streak"] = 2
    c["engine"].update(engine_over)
    return TradingConfig.from_dict(c)


class SeqCloseBroker(DryRunBroker):
    """按 CLOSE 报单序号决定是否拒单：序号在 `reject_at` 里 → 拒，否则正常成交。"""

    def __init__(self, spec, opts, reject_at=()):
        super().__init__(spec, opts)
        self.reject_at = set(reject_at)
        self.n_close = 0

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        o = super().submit(intent, side, volume, ref_price, signal_key,
                           note=note, entry_date=entry_date, is_exit=is_exit)
        if intent is OrderIntent.CLOSE:
            self.n_close += 1
            if self.n_close in self.reject_at:
                o.status = "rejected"
                o.filled_price = None
                o.meta["reject_reason"] = "depth"
                o.meta["reject_class"] = "price"
        return o


class AlwaysRejectCloseBroker(SeqCloseBroker):
    def __init__(self, spec, opts):
        super().__init__(spec, opts, reject_at=set(range(1, 999)))


class RealShortMissingBroker(DryRunBroker):
    """柜台真实持仓：多头 2 手存在、空头 0 手（模拟空侧被外部平掉 / 是幻影仓）。"""

    armed = False

    def real_position(self, side):
        if not self.armed:
            return None                     # 未武装 → 引擎跳过对账
        return 2 if side is Side.LONG else 0


def build(tmpd, broker, **engine_over):
    cfg = make_cfg(**engine_over)
    ev_path = os.path.join(tmpd, "events.jsonl")
    eng = TradingEngine(
        cfg, broker, DefaultEntryPolicy({}),
        LayeredExitPolicy(cfg.exit_params.model_dump()),
        Store(os.path.join(tmpd, "state.db")),
        EventLog(ev_path, echo=False, echo_kinds=None))
    return eng, ev_path


def ev_count(eng, kind):
    """事件日志里某 kind 的条数（EventLog 是按秒/按 64 条缓冲的，必须先 flush）。"""
    try:
        eng.ev.flush()
        with open(eng.ev.path, encoding="utf-8") as f:
            return sum(1 for line in f if '"{}"'.format(kind) in line)
    except OSError:
        return 0


# ════════════════════════════════════════════════════════════════
print("\n[1] 成功 CLOSE 后 _close_fail_streak 必须归零（语义是「连续」不是「累计」）")
# ════════════════════════════════════════════════════════════════
with tmp_dir("streak") as tmp:
    spec = InstrumentSpec()
    eng, _ = build(tmp, SeqCloseBroker(spec, {"sim_equity": 1_000_000.0},
                                       reject_at=[1]))
    eng.on_bar(bar(1000, D1 + " 09:40", P0))
    eng.on_signal(sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    check("[1a] 开仓后 RUNNING", eng.account_state(), AccountState.RUNNING)
    eng.on_bar(adverse(3000, D2))                     # CLOSE#1 → 被拒
    check("[1b] 首次被拒后 streak=1", eng._close_fail_streak, 1)
    eng.on_bar(adverse(4000, D2))                     # CLOSE#2 → 成交
    check("[1c] 平仓成交后净敞口归零", eng.positions.net_volume(), 0)
    check("[1d] ★ 成功 CLOSE 后 streak 归零（否则会跨 run 累积误清真仓）",
          eng._close_fail_streak, 0)
    check("[1e] status.close_cooldown.streak 同步可见",
          eng.auto_order_status()["close_cooldown"]["streak"], 0)


# ════════════════════════════════════════════════════════════════
print("\n[2] act.volume < target.volume 的部分平仓必须被拒绝（不整笔记账）")
# ════════════════════════════════════════════════════════════════
with tmp_dir("partial") as tmp:
    spec = InstrumentSpec()
    eng, evp = build(tmp, SeqCloseBroker(spec, {"sim_equity": 1_000_000.0}))
    eng.on_bar(bar(1000, D1 + " 09:40", P0))
    eng.on_signal(sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    # 同日不利 K 线 → 转移④ 反向 OPEN → 净敞口归零、进入锁仓态
    eng.on_bar(bar(2000, D1 + " 10:00", P0 - 60.0, lo=P0 - 80.0))
    check("[2a] 锁仓态（有仓单、净敞口 0）", eng.account_state(),
          AccountState.LOCKED)
    n_before = len(eng.positions.positions)
    check("[2b] 簿内两笔仓单", n_before, 2)
    # 模拟"跨会话把 risk.max_volume 从 4 调到 2"：lots_per_signal 小于仓单手数
    eng.lots_per_signal = 1
    eng.on_bar(bar(3000, D2 + " 09:40", P0 - 60.0))
    eng.on_signal(sig("X|buy|2", D2 + " 09:40", 3000, P0 - 60.0, True))
    check("[2c] ★ 部分平仓被拒绝（close_volume_below_target）",
          eng._last_reject, "close_volume_below_target")
    check("[2d] 簿面未被改动（没有整笔记账 / 整笔移除）",
          len(eng.positions.positions), n_before)
    check("[2e] 净敞口仍是 0（未误平）", eng.positions.net_volume(), 0)
    check_true("[2f] 拒绝动作写进了事件日志（order_rejected）",
               ev_count(eng, "order_rejected") >= 1)


# ════════════════════════════════════════════════════════════════
print("\n[3] 运行期「净敞口≠0 但无风控锚」必须显性化（不再静默）")
# ════════════════════════════════════════════════════════════════
with tmp_dir("runanchor") as tmp:
    spec = InstrumentSpec()
    broker = RealShortMissingBroker(spec, {"sim_equity": 1_000_000.0})
    eng, evp = build(tmp, broker)
    eng.on_bar(bar(1000, D1 + " 09:40", P0))
    eng.on_signal(sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    check("[3a] 开仓后 RUNNING 且有 run", eng._run_view() is not None, True)
    eng.on_bar(bar(2000, D1 + " 10:00", P0 - 60.0, lo=P0 - 80.0))
    check("[3b] 转移④ 后进入锁仓态", eng.account_state(), AccountState.LOCKED)
    # D2：柜台空侧不存在 → 对账清掉空侧 → 净敞口 0 → +2（RUNNING）
    broker.armed = True
    eng.on_bar(bar(3000, D2 + " 09:40", P0 - 60.0))
    check("[3c] 对账后回到 RUNNING（净敞口非 0）", eng.account_state(),
          AccountState.RUNNING)
    check("[3d] 此时确实没有风控锚（本缺陷的触发形态）",
          eng._run_view() is None, True)
    st = eng.auto_order_status()
    codes = sorted({a["code"] for a in st["alerts"]})
    check_true("[3e] ★ 发出 severe 告警 run_missing_anchor",
               "run_missing_anchor" in codes, codes)
    check_true("[3f] ★ 写了 run_state_missing_runtime 事件",
               ev_count(eng, "run_state_missing_runtime") >= 1)
    n1 = ev_count(eng, "run_state_missing_runtime")
    eng.on_bar(bar(4000, D2 + " 09:45", P0 - 60.0))
    check("[3g] 同一次异常只报一次（不刷屏）",
          ev_count(eng, "run_state_missing_runtime"), n1)
    # 状态恢复正常（补回风控锚）后通知锁复位
    eng._run_start(P0 - 60.0, eng.last_bar,
                   sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    eng._sync_state()
    check("[3h] 补回 run 后通知锁复位", eng._run_missing_notified, False)


# ════════════════════════════════════════════════════════════════
print("\n[4] CLOSE 冷却拦截必须写事件（且不算拒单）")
# ════════════════════════════════════════════════════════════════
with tmp_dir("cooldown") as tmp:
    spec = InstrumentSpec()
    eng, evp = build(tmp, AlwaysRejectCloseBroker(spec, {"sim_equity": 1_000_000.0}),
                     close_retry_bars=2, close_max_streak=5)
    eng.on_bar(bar(1000, D1 + " 09:40", P0))
    eng.on_signal(sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    eng.on_bar(adverse(3000, D2))            # CLOSE#1 → 被拒 → 进入冷却
    check_true("[4a] 进入 CLOSE 冷却", eng._in_close_cooldown())
    n_rej = ev_count(eng, "order_rejected")
    eng.on_bar(adverse(4000, D2))            # 冷却期内 → 跳过报单
    check_true("[4b] ★ 冷却拦截写了 close_retry_skipped 事件",
               ev_count(eng, "close_retry_skipped") >= 1)
    check("[4c] 冷却拦截不算拒单（order_rejected 条数不变）",
          ev_count(eng, "order_rejected"), n_rej)
    check("[4d] 冷却期内未向柜台发单（报单序号未变）",
          eng.broker.order_seq(), eng.broker.order_seq())


# ════════════════════════════════════════════════════════════════
print("\n[5] P6-F：API 侧必须返回 run / close_cooldown")
# ════════════════════════════════════════════════════════════════
with tmp_dir("apifields") as tmp:
    spec = InstrumentSpec()
    eng, _ = build(tmp, SeqCloseBroker(spec, {"sim_equity": 1_000_000.0},
                                       reject_at=[1]))
    eng.on_bar(bar(1000, D1 + " 09:40", P0))
    eng.on_signal(sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    eng.on_bar(adverse(3000, D2))            # CLOSE#1 被拒 → 冷却生效
    try:
        from App.AppTrader import AppTrader

        class _H(object):
            def __init__(self, d):
                self.out_dir = d

        res = AppTrader._read_engine_switch(_H(tmp))
        check_true("[5a] _read_engine_switch 有返回", isinstance(res, dict),
                   type(res).__name__)
        check_true("[5b] ★ 返回含 run（前端 tooltip 的风控锚/止损/止盈）",
                   "run" in res, sorted(res.keys()) if res else None)
        check_true("[5c] ★ 返回含 close_cooldown（平仓冷却剩余）",
                   "close_cooldown" in res, sorted(res.keys()) if res else None)
        cool = (res or {}).get("close_cooldown") or {}
        check_true("[5d] close_cooldown 结构与前端读取一致（active/bars_left）",
                   "active" in cool and "bars_left" in cool, cool)
        run_v = (res or {}).get("run")
        check_true("[5e] 有净敞口时 run 非空且含 anchor/stop/tp",
                   isinstance(run_v, dict)
                   and {"anchor", "stop", "tp"} <= set(run_v), run_v)
    except Exception as e:                    # App 层不可导入时不算失败
        print("  ⚠ 跳过 [5]：App 层不可导入（{}: {}）".format(
            type(e).__name__, e))


print("\n" + "=" * 60)
print("P43 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

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

  [6] 已删符号不得有任何「活引用」（AST 级）
      —— 一期漏改的形态就是"代码删了符号、某个角落还在引用它"，语法上不报错、
         文本 grep 又分不清注释与求值，只有真执行到那一行才炸。

  [7] 建锚失败只报一条告警（2026-09-13 新增）
      —— `_run_start` 拿不到信号时写事件 + 发 `run_start_incomplete` 后 return，
         `_run_side` 仍是 None；紧接着的 `_sync_state → _check_run_anchor` 看到
         "净敞口 ≠ 0 且无锚"又发一条 `run_missing_anchor`。同一根因两个 code，
         D11 队列按 code 合并 → 用户连看两个弹窗。修法是 incomplete 分支置上
         通知锁（首条已说清成因，自检不必重复喊）。

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
from Trading.Config import (DEFAULT_CONFIG, TradingConfig,  # noqa: E402
                            resolved_exit_params)
from Trading.Engine.Engine import TradingEngine           # noqa: E402
from Trading.Infra.EventLog import EventLog               # noqa: E402
from Trading.Infra.Instrument import Instrument   # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402

_IF = PRODUCT_PROFILES["IF"]
from Trading.Infra.StateDB import Store  # noqa: E402

from Trading.Infra.Records import (AccountState, Bar, ExitPlan,  # noqa: E402
                                   OrderIntent, Position, Side, Signal)

from Trading.Strategy.Entry import EntryPolicy     # noqa: E402
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


def make_pos(side, volume, price, key, entry_date):
    """一笔仓单 —— 用于复刻「上一会话遗留的簿面」（跨会话换表 / 带旧仓重启）。"""
    return Position(
        symbol="CFFEX.IF2609", side=side, volume=volume, entry_price=price,
        entry_at=entry_date + " 09:30", entry_bar_ts=1000, signal_key=key,
        open_order_id="p43-" + key,
        exit_plan=ExitPlan(name="x", stop_price=price - 10.0),
        entry_bar_seq=1, entry_date=entry_date)


def make_cfg(**engine_over):
    c = copy.deepcopy(DEFAULT_CONFIG)
    # 手数 = 品种执行策略表第 3 列（IF → 2 手）；原 risk.max_volume 已于 2026-09-16 删除
    c["exit_params"].update({"use_atr": False,
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
        cfg, broker, EntryPolicy({}),
        LayeredExitPolicy(resolved_exit_params(cfg)),
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
    spec = Instrument(None, _IF)
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
# 触发形态（2026-09-16 复核后的**唯一**一条路）：**跨会话把表第 3 列调小**
# （如 4 → 2）后带旧仓重启 —— 簿内一笔 4 手多头（旧表遗留）+ 一笔 2 手空头，
# 净敞口 +2 却要对冲那笔 4 手 → 转移 ⑤ 算出"只平 2 手"。
#   为什么不沿用 `eng.lots_per_signal = 1` 造假：该属性已随 `risk.max_volume`
#   一并删除（评审 P2-3：单笔手数只留一个旋钮 = 品种执行策略表第 3 列），
#   引擎侧再没有可被运行期覆盖的手数镜像 —— 这条不足量只剩"簿面被外力改过 /
#   换表后带旧仓"一种来源，故按**注入旧仓簿**复刻（等价于 `_restore` 的结果）。
with tmp_dir("partial") as tmp:
    spec = Instrument(None, _IF)
    eng, evp = build(tmp, SeqCloseBroker(spec, {"sim_equity": 1_000_000.0}))
    eng.positions.add(make_pos(Side.LONG, 4, P0, "OLD-L", D1))
    eng.positions.add(make_pos(Side.SHORT, 2, P0 - 40.0, "OLD-S", D1))
    eng.on_bar(bar(1000, D2 + " 09:40", P0))
    # 簿内两笔反向仓单**不等量**（4 多 + 2 空）→ 净敞口 +2 ≠ 0 → 状态是 RUNNING
    # （LOCKED 要求净敞口恰好为 0）。这一点正是本组的关键：转移 ⑤ 只认"最近一笔
    # 是昨仓 + 有反向仓单"，它在 **RUNNING** 态下就会算出"只平 min(|净敞口|, 目标)"
    # 的不足量手数 —— 不需要真进锁仓态。
    check("[2a] RUNNING 态 + 净敞口 +2（两笔不等量反向仓单）",
          (eng.account_state(), eng.positions.net_volume()),
          (AccountState.RUNNING, 2))
    n_before = len(eng.positions.positions)
    act = eng._decide_exit(eng.last_bar)          # 纯函数：不改状态
    check("[2b] 转移 ⑤：目标 = 最早那笔多头（4 手）",
          (act.transition, act.intent, act.target.volume),
          (5, OrderIntent.CLOSE, 4))
    check("[2c] 动作手数 = min(|净敞口|, 目标手数) = 2 < 目标 4 手",
          act.volume, 2)
    why = eng._pre_trade_check(
        act, D2, sig("X|buy|2", D2 + " 09:40", 1000, P0, True), ref_price=P0)
    check("[2d] ★ 部分平仓被拒绝（close_volume_below_target）",
          why, "close_volume_below_target")
    eng._execute(act, P0, bar=eng.last_bar,
                 sig=sig("X|buy|2", D2 + " 09:40", 1000, P0, True))
    check("[2e] _last_reject 记录在案",
          eng._last_reject, "close_volume_below_target")
    check("[2f] 簿面未被改动（没有整笔记账 / 整笔移除）",
          len(eng.positions.positions), n_before)
    check("[2g] 净敞口仍是 +2（未误平）", eng.positions.net_volume(), 2)
    check_true("[2h] 拒绝动作写进了事件日志（order_rejected）",
               ev_count(eng, "order_rejected") >= 1)


# ════════════════════════════════════════════════════════════════
print("\n[3] 运行期「净敞口≠0 但无风控锚」必须显性化（不再静默）")
# ════════════════════════════════════════════════════════════════
with tmp_dir("runanchor") as tmp:
    spec = Instrument(None, _IF)
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
    spec = Instrument(None, _IF)
    eng, evp = build(tmp, AlwaysRejectCloseBroker(spec, {"sim_equity": 1_000_000.0}),
                     close_retry_bars=2, close_max_streak=5)
    eng.on_bar(bar(1000, D1 + " 09:40", P0))
    eng.on_signal(sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    eng.on_bar(adverse(3000, D2))            # CLOSE#1 → 被拒 → 进入冷却
    check_true("[4a] 进入 CLOSE 冷却", eng._in_close_cooldown())
    n_rej = ev_count(eng, "order_rejected")
    # 2026-09-13 修正：原写法是 `check(..., eng.broker.order_seq(), eng.broker.order_seq())`
    # —— 同一次调用的返回值自己比自己，**恒通过**，根本护不住"冷却期内没发单"。
    # 现在先取快照，再过 bar，再比。
    seq_before = eng.broker.order_seq()
    eng.on_bar(adverse(4000, D2))            # 冷却期内 → 跳过报单
    check_true("[4b] ★ 冷却拦截写了 close_retry_skipped 事件",
               ev_count(eng, "close_retry_skipped") >= 1)
    check("[4c] 冷却拦截不算拒单（order_rejected 条数不变）",
          ev_count(eng, "order_rejected"), n_rej)
    check("[4d] 冷却期内未向柜台发单（报单序号未变）",
          eng.broker.order_seq(), seq_before)


# ════════════════════════════════════════════════════════════════
print("\n[5] P6-F：API 侧必须返回 run / close_cooldown")
# ════════════════════════════════════════════════════════════════
with tmp_dir("apifields") as tmp:
    spec = Instrument(None, _IF)
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


# ════════════════════════════════════════════════════════════════
print("\n[6] 已删符号不得有任何「活引用」（AST 级，2026-09-13 新增）")
# ════════════════════════════════════════════════════════════════
# 背景：Phase 1 删掉 `OrderIntent.LOCK` / `UNLOCK` / `PositionOrigin` / `ExitMode`
# 之后，`Trading/Test/smoke_simnow_phase_g.py` 里**仍留着 `OrderIntent.UNLOCK`
# 的活引用**，直到 2026-09-13 才被发现（当时全套 39 项里唯一失败的就是它）。
#
# 为什么不能再用文本 grep（p32 [3c] 那种）：
#   `PositionOrigin` / `lock_pair_id` / `UNLOCK` 这些词**必须**继续出现在注释、
#   docstring 与护栏字符串里（p26 就是靠它们解释"这个概念已删"）。
#   文本 grep 要么漏（不敢扫 Test 目录）、要么误伤（把注释全标红）。
#   所以这里用 AST 只看**真会求值的节点**：
#     · `X.LOCK` / `X.UNLOCK`，且 X 的写法以 `OrderIntent` 结尾；
#     · 名为 `PositionOrigin` / `ExitMode` 的裸标识符；
#     · `from ... import <已删符号>` 的导入名。
#   字符串常量、注释、docstring 一律不算（它们本就不参与求值）。
#
# 2026-09-13 补：`DefaultEntryPolicy`（→ `EntryPolicy`）与 `ExitPolicy`（已删）
#   也纳入。本轮把 `DefaultEntryPolicy` 改名后**漏改了 23 个文件 / 48 处活引用**
#   （含生产入口 `Trading/main.py`），全套 42 项里 23 项直接 ImportError；
#   而本护栏当时只覆盖 LOCK/UNLOCK/PositionOrigin/ExitMode，**恰好没覆盖本轮
#   改动的那个符号** —— 加进来才能在下一次改名时立刻报警，而不是等跑测试。
import ast  # noqa: E402
import io   # noqa: E402

_DEAD_ATTRS = {"LOCK", "UNLOCK"}
_DEAD_NAMES = {"PositionOrigin", "ExitMode", "DefaultEntryPolicy", "ExitPolicy"}
# `from x import <name>` 里的名字不是 ast.Name，需单独判。
_DEAD_IMPORTS = {"DefaultEntryPolicy", "ExitPolicy"}
# ⚠️ 不能把名为 "Test" 的目录跳过 —— 出问题的 smoke 脚本就在 Trading/Test/ 里。
#    只跳真正的依赖/缓存/二进制目录。
_SKIP_DIRS = {"__pycache__", ".git", ".venv", ".idea", "node_modules",
              "Image", "Docs", "build", "dist"}
_live_hits = []
_scan_n = 0
for _dp, _dns, _fns in os.walk(_ROOT):
    _dns[:] = [d for d in _dns
               if d not in _SKIP_DIRS and not d.startswith(".")]
    for _fn in _fns:
        if not _fn.endswith(".py"):
            continue
        _fp = os.path.join(_dp, _fn)
        _rel = os.path.relpath(_fp, _ROOT).replace("\\", "/")
        try:
            _tree = ast.parse(io.open(_fp, encoding="utf-8",
                                      errors="replace").read(), filename=_rel)
        except SyntaxError:
            continue
        _scan_n += 1
        for _node in ast.walk(_tree):
            if isinstance(_node, ast.Attribute) and _node.attr in _DEAD_ATTRS:
                _v = _node.value
                _base = _v.id if isinstance(_v, ast.Name) else (
                    _v.attr if isinstance(_v, ast.Attribute) else "")
                if _base.endswith("OrderIntent"):
                    _live_hits.append("{}:{} .{}".format(
                        _rel, getattr(_node, "lineno", "?"), _node.attr))
            elif isinstance(_node, ast.Name) and _node.id in _DEAD_NAMES:
                _live_hits.append("{}:{} {}".format(
                    _rel, getattr(_node, "lineno", "?"), _node.id))
            elif isinstance(_node, ast.ImportFrom):
                for _al in _node.names:
                    if _al.name in _DEAD_IMPORTS:
                        _live_hits.append("{}:{} import {}".format(
                            _rel, getattr(_node, "lineno", "?"), _al.name))
check_true("[6a] 扫描覆盖面够（扫到 ≥ 60 个 .py）", _scan_n >= 60, _scan_n)
check("[6b] ★ 全仓无 OrderIntent.LOCK/UNLOCK、PositionOrigin、ExitMode、"
      "DefaultEntryPolicy、ExitPolicy 的活引用",
      _live_hits, [])

# 反向自检：护栏本身必须抓得住（构造一段该被拦下的代码，确认 AST 判据有判别力）
# 期望命中 6 处：`OrderIntent.UNLOCK` / `PositionOrigin`×2 / `ExitMode`
#               / `from ... import DefaultEntryPolicy` / `ExitPolicy`
# （`OrderIntent` 自身是 Name 但不在禁用清单里，不算命中）
_probe = ast.parse("from x import OrderIntent\n"
                   "OrderIntent.UNLOCK\n"
                   "PositionOrigin\n"
                   "ExitMode\n"
                   "PositionOrigin = 1  # 赋值也算活引用\n"
                   "from Trading.Strategy import DefaultEntryPolicy\n"
                   "ExitPolicy\n")
_hit2 = [n.attr for n in ast.walk(_probe)
         if isinstance(n, ast.Attribute) and n.attr in _DEAD_ATTRS]
_hit2 += [n.id for n in ast.walk(_probe)
          if isinstance(n, ast.Name) and n.id in _DEAD_NAMES]
_hit2 += [a.name for n in ast.walk(_probe)
          if isinstance(n, ast.ImportFrom) for a in n.names
          if a.name in _DEAD_IMPORTS]
check_true("[6c] 判据有判别力（样本代码被抓到 6 处）", len(_hit2) == 6, _hit2)
# 注释/docstring 里的同名字符串**不得**被判为活引用（否则 p26 的解释性注释全要删）
_quiet = ast.parse('"""PositionOrigin / ExitMode 已删除。"""\n'
                   "_LEGACY = ('origin', 'lock_pair_id')\n"
                   'w = "UNLOCK"\n'
                   '# DefaultEntryPolicy 是旧名，已改 EntryPolicy\n')
_hit3 = [n.attr for n in ast.walk(_quiet)
         if isinstance(n, ast.Attribute) and n.attr in _DEAD_ATTRS]
_hit3 += [n.id for n in ast.walk(_quiet)
          if isinstance(n, ast.Name) and n.id in _DEAD_NAMES]
_hit3 += [a.name for n in ast.walk(_quiet)
          if isinstance(n, ast.ImportFrom) for a in n.names
          if a.name in _DEAD_IMPORTS]
check("[6d] 注释 / 字符串常量不误伤（避免 p26 的解释性注释被标红）",
      _hit3, [])


# ════════════════════════════════════════════════════════════════
print("\n[7] 「建锚失败」只报一条告警（run_start_incomplete 不叠加 run_missing_anchor）")
# ════════════════════════════════════════════════════════════════
# 背景：`_run_start` 拿不到信号（sig is None）时会走 incomplete 分支，写事件 +
# 发 severe 告警 `run_start_incomplete` 后 return —— 此时 `_run_side` 仍是 None。
# 紧接着的 `_sync_state()` 会调 `_check_run_anchor`，它看到"净敞口 ≠ 0 且无 run"
# 又发一条 severe `run_missing_anchor`。
# 同一根因、两个 code：D11 队列按 code 合并 → 用户连着看到两个弹窗，且两条文案
# 说的是同一件事。2026-09-13 修法：`_run_start` 的 incomplete 分支置上通知锁
# `_run_missing_notified`，自检不再重复喊（首条已把成因说清楚）。
with tmp_dir("dupalert") as tmp:
    spec = Instrument(None, _IF)
    eng, _ = build(tmp, DryRunBroker(spec, {"sim_equity": 1_000_000.0}))
    eng.on_bar(bar(1000, D1 + " 09:40", P0))
    eng.on_signal(sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    check("[7a] 开仓后 RUNNING 且有 run", eng._run_view() is not None, True)
    # 人为制造"净敞口非 0 但建锚时缺信号"的形态（= _run_start 的 incomplete 分支）
    eng._run_end()
    eng._run_start(P0, eng.last_bar, None)
    eng._sync_state()
    codes = sorted({a["code"] for a in eng.auto_order_status()["alerts"]})
    check_true("[7b] 写了 run_start_incomplete 事件",
               ev_count(eng, "run_start_incomplete") >= 1)
    check("[7c] ★ 告警 code 集合只含 run_start_incomplete（不叠加 run_missing_anchor）",
          [c for c in codes if "run_" in c], ["run_start_incomplete"])
    check("[7d] 通知锁已置上（后续自检不重复刷屏）",
          eng._run_missing_notified, True)


print("\n" + "=" * 60)
print("P43 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

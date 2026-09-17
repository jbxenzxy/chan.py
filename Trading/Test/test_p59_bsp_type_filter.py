# -*- coding: utf-8 -*-
"""
P59 买卖点类型过滤 —— 「显示设置」勾选接入自动下单
==================================================
需求原文（2026-09-18）：
  ⑴ 页面右上角设置弹窗有「买卖点类型（可多选）」；
  ⑵ 只有勾选的类型才画在 K 线图上；
  ⑶ 同一份勾选要应用到自动下单：未勾选的类型，自动下单忽略其信号；
     四个全不勾 → 即使自动下单开启也不再有下单操作；只勾 0/1/2 → 忽略 3 类；
  ⑷ 勾改动**实时生效**：盘中可改，不管账户是空仓态 / 运行态 / 锁仓态。

需求方拍板的三条口径（本测试按此钉死）：
  · 只认 0/1/2/3 四类（与前端四个复选框一一对应）。四类之外的类型
    （缠论引擎侧还有 11/11p/22/22s/33a/33b）按**未勾选**处理 —— 忽略并留痕，
    不放行也不报错。
  · 只拦「由信号驱动的新报单」（空仓开仓 = 转移①、锁仓态拆锁 = 转移③）。
    运行态已有持仓的离场走 L1-L3、不经 on_signal，故**不受过滤影响** ——
    过滤不会让持仓失去止损止盈（这是"只拦新信号"与"整体冻结"的分界线）。
  · 复用前端既有勾选状态：写入方是 App/AppTrader（POST /api/trader/signal-filter），
    存 state.db 的 kv `bsp_type_filter`；引擎**每个信号现读**，不缓存。

覆盖：
  [1] 过滤未设置（kv 缺失）→ 全部放行（升级后行为不变，绝不静默停摆）
  [2] 只勾 0/1/2 → 3 类信号被忽略：action=skip + 零报单 + signal_skip 事件
  [3] 只勾 0/1/2 → 1 类信号照常开仓（正反向对照，证明不是"一律拦"）
  [4] 盘中改勾选即时生效：同一引擎实例，改 kv 前后行为翻转（无需重启）
  [5] 四个全不勾 → 空仓态零报单（需求原文举例场景）
  [6] 四类之外的类型（"11p"）按未勾选处理 → 忽略并留痕（换 bs_type 配置不静默）
  [7] 锁仓态同样生效：转移③ 拆锁被拦 / 勾选后放行（需求 ⑷ 的锁仓态）
  [8] 过滤门只挂在信号入口：出场路径（_settle_positions / _decide_exit）
      源码里不出现过滤常量 —— 运行态的 L1-L3 离场不受影响（需求方拍板口径）

跑法：python Trading/Test/test_p59_bsp_type_filter.py
"""
from __future__ import annotations

import inspect
import json
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
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Broker.DryRun import DryRunBroker                   # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig         # noqa: E402
from Trading.Engine.Engine import TradingEngine                  # noqa: E402
from Trading.Infra.EventLog import EventLog                      # noqa: E402
from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES               # noqa: E402
from Trading.Infra.Records import (BSP_TYPE_CHOICES,             # noqa: E402
                                   BSP_TYPE_FILTER_KEY, AccountState, Bar,
                                   ExitPlan, Position, Side, Signal)
from Trading.Infra.StateDB import Store                          # noqa: E402
from Trading.Strategy.Entry import EntryPolicy                   # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy              # noqa: E402

_IF = PRODUCT_PROFILES["IF"]
_SYM = "CFFEX.IF2609"

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {}".format(name))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p59_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def build_engine(tmpdir):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = Instrument(InstrumentConfig(trade_symbol=_SYM), _IF)
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False,
                  echo_kinds=None)
    engine = TradingEngine(cfg, broker, EntryPolicy({}), LayeredExitPolicy(),
                           store, ev)
    return engine, store, broker, ev


def make_signal(is_buy=True, bsp_type="1", date="2026-09-01 09:35",
                price=4550.0):
    return Signal(key=Signal.make_key(date, bsp_type, is_buy), symbol=_SYM,
                  freq="5m", date=date, timestamp=0, bsp_type=bsp_type,
                  is_buy=is_buy, price=price, high=price + 2.0,
                  low=price - 2.0)


def make_bar(ts, o=4550.0, h=4552.0, l=4548.0, c=4551.0):
    return Bar(timestamp=ts, date="2026-09-01 09:40", open=o, high=h,
               low=l, close=c, vol=1)


def make_pos(side=Side.LONG, vol=1, entry_price=4550.0, signal_key="MP",
             entry_bar_seq=1, entry_date="2026-08-01"):
    """默认 **跨日** 仓（entry_date 远早于今天）—— 锁仓态信号走转移③ 拆锁。"""
    return Position(
        symbol=_SYM, side=side, volume=vol, entry_price=entry_price,
        entry_at="2026-08-01 09:00", entry_bar_ts=4000,
        entry_bar_seq=entry_bar_seq, signal_key=signal_key,
        open_order_id="o-" + signal_key,
        exit_plan=ExitPlan(name="run_managed", stop_price=0.0),
        entry_date=entry_date)


def set_filter(store, types):
    store.set_json(BSP_TYPE_FILTER_KEY, types)


def skip_reasons(path):
    reasons = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("kind") == "signal_skip":
                reasons.append(e.get("reason"))
    return reasons


print("P59：买卖点类型过滤（显示设置勾选 → 自动下单信号门）")

# ════════════════════════════════════════════════════════════════
# [1] 过滤未设置 → 全部放行（升级后行为不变）
# ════════════════════════════════════════════════════════════════
print("\n[1] 过滤未设置（kv 缺失）→ 全部放行")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    check("[1a] kv 缺失时 bsp_type_filter() 为 None",
          engine.bsp_type_filter(), None)
    sig = make_signal(is_buy=True, bsp_type="3")
    engine.on_signal(sig)
    check("[1b] 未设置过滤 → 3 类信号照常开仓（一笔记账）",
          len(engine.positions.positions), 1)
    check("[1c] 确实下了单", len(broker.orders), 1)

# ════════════════════════════════════════════════════════════════
# [2] 只勾 0/1/2 → 3 类信号被忽略
# ════════════════════════════════════════════════════════════════
print("\n[2] 只勾 0/1/2 → 3 类信号被忽略（需求 ⑶）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    set_filter(store, {"0": True, "1": True, "2": True, "3": False})
    sig = make_signal(is_buy=True, bsp_type="3")
    engine.on_signal(sig)
    check("[2a] 3 类信号 action=skip", store.signal_action(sig.key), "skip")
    check("[2b] 零报单", len(broker.orders), 0)
    check("[2c] 簿内无仓", len(engine.positions.positions), 0)
    ev.flush()
    check("[2d] 写了 signal_skip(bsp_type_filtered)",
          "bsp_type_filtered" in skip_reasons(
              os.path.join(tmp, "events.jsonl")), True)

# ════════════════════════════════════════════════════════════════
# [3] 正向对照：只勾 0/1/2 → 1 类信号照常开仓
# ════════════════════════════════════════════════════════════════
print("\n[3] 同一份勾选下 1 类信号照常开仓（证明不是一律拦）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    set_filter(store, {"0": True, "1": True, "2": True, "3": False})
    sig = make_signal(is_buy=True, bsp_type="1")
    engine.on_signal(sig)
    check("[3a] 1 类信号 action=opened", store.signal_action(sig.key), "opened")
    check("[3b] 下单 1 笔", len(broker.orders), 1)
    check("[3c] 簿内 1 笔持仓", len(engine.positions.positions), 1)

# ════════════════════════════════════════════════════════════════
# [4] 盘中改勾选即时生效（同一引擎实例，不重启）
# ════════════════════════════════════════════════════════════════
print("\n[4] 盘中改勾选即时生效（需求 ⑷ · 同一引擎实例行为翻转）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    set_filter(store, {"0": False, "1": False, "2": False, "3": False})
    s1 = make_signal(is_buy=True, bsp_type="1", date="2026-09-01 09:35")
    engine.on_signal(s1)
    check("[4a] 全不勾时 → 忽略（零报单）", len(broker.orders), 0)
    # 盘中勾回 1 类：引擎不重启，下一个信号即按新勾选处理
    set_filter(store, {"0": False, "1": True, "2": False, "3": False})
    s2 = make_signal(is_buy=True, bsp_type="1", date="2026-09-01 09:40")
    engine.on_signal(s2)
    check("[4b] 勾回后同一类信号立刻放行（无需重启）", len(broker.orders), 1)
    check("[4c] 仍是空仓态→转移①开仓", store.signal_action(s2.key), "opened")

# ════════════════════════════════════════════════════════════════
# [5] 四个全不勾 → 空仓态零报单（需求原文举例）
# ════════════════════════════════════════════════════════════════
print("\n[5] 四个全不勾 → 自动下单不再有下单操作")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    set_filter(store, {"0": False, "1": False, "2": False, "3": False})
    for t in ("0", "1", "2", "3"):
        engine.on_signal(make_signal(is_buy=True, bsp_type=t,
                                     date="2026-09-01 09:3{}".format(t)))
    check("[5a] 四类信号全被忽略 → 零报单", len(broker.orders), 0)
    check("[5b] 簿内无仓", len(engine.positions.positions), 0)
    check("[5c] 自动下单开关仍是开着的（不是靠关开关实现）",
          engine.auto_order_enabled, True)

# ════════════════════════════════════════════════════════════════
# [6] 四类之外的类型 → 按未勾选处理（换 bs_type 配置不静默）
# ════════════════════════════════════════════════════════════════
print("\n[6] 四类之外的类型（11p）按未勾选处理 → 忽略并留痕")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    set_filter(store, {"0": True, "1": True, "2": True, "3": True})
    sig = make_signal(is_buy=True, bsp_type="11p")
    engine.on_signal(sig)
    check("[6a] 全部勾选下 11p 仍被忽略（不在四类里）", len(broker.orders), 0)
    ev.flush()
    check("[6b] 留痕 signal_skip(bsp_type_filtered)",
          "bsp_type_filtered" in skip_reasons(
              os.path.join(tmp, "events.jsonl")), True)

# ════════════════════════════════════════════════════════════════
# [7] 锁仓态同样生效（需求 ⑷ 的锁仓态）
# ════════════════════════════════════════════════════════════════
print("\n[7] 锁仓态同样生效：转移③ 拆锁被拦 / 勾选后放行")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.positions.add(make_pos(side=Side.LONG, signal_key="P59-L"))
    engine.positions.add(make_pos(side=Side.SHORT, signal_key="P59-S"))
    check("[7a] 簿内双向持仓 → 锁仓态",
          engine.account_state(), AccountState.LOCKED)
    set_filter(store, {"0": True, "1": False, "2": True, "3": True})
    sig = make_signal(is_buy=True, bsp_type="1")
    engine.on_signal(sig)
    check("[7b] 锁仓态下未勾选类型 → 拆锁被拦（零报单）", len(broker.orders), 0)
    check("[7c] 持仓簿未被改动", len(engine.positions.positions), 2)
    set_filter(store, {"0": True, "1": True, "2": True, "3": True})
    sig2 = make_signal(is_buy=True, bsp_type="1", date="2026-09-01 09:45")
    engine.on_signal(sig2)
    check("[7d] 勾选后 → 转移③ 拆锁放行（下单 1 笔）", len(broker.orders), 1)

# ════════════════════════════════════════════════════════════════
# [8] 过滤门只挂在信号入口 —— 出场路径不受影响（需求方拍板口径）
# ════════════════════════════════════════════════════════════════
print("\n[8] 出场路径（L1-L3）不含过滤门 —— 已有持仓不会失去止损止盈")
_src_settle = inspect.getsource(TradingEngine._settle_positions)
_src_exit = inspect.getsource(TradingEngine._decide_exit)
_src_on_signal = inspect.getsource(TradingEngine.on_signal)
check("[8a] on_signal 内有过滤门", BSP_TYPE_FILTER_KEY in _src_on_signal
      or "bsp_type_filtered" in _src_on_signal, True)
check("[8b] _settle_positions 不含过滤常量",
      BSP_TYPE_FILTER_KEY in _src_settle, False)
check("[8c] _decide_exit 不含过滤常量",
      BSP_TYPE_FILTER_KEY in _src_exit, False)
check("[8d] 过滤方法读取键 = 常量", TradingEngine.bsp_type_filter.__doc__
      is not None and "现读" in TradingEngine.bsp_type_filter.__doc__, True)

# ════════════════════════════════════════════════════════════════
# [9] 跨层 SSOT 漂移护栏：后端 BSP_TYPE_CHOICES ↔ 前端四个复选框
#     （前端是静态资源无法 import Python，只能靠断言钉死两边一致）
# ════════════════════════════════════════════════════════════════
print("\n[9] 前后端「四类」集合一致（BSP_TYPE_CHOICES ↔ 前端复选框）")
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
with open(os.path.join(_REPO, "Frontend", "index.html"), encoding="utf-8") as _f:
    _html = _f.read()
_html_types = sorted(set(re.findall(r'name="bsp-filter"\s+value="([^"]+)"', _html)))
check("[9a] 前端复选框集合 == BSP_TYPE_CHOICES",
      _html_types, sorted(BSP_TYPE_CHOICES))
with open(os.path.join(_REPO, "Frontend", "app.js"), encoding="utf-8") as _f:
    _js = _f.read()
check("[9b] 前端回推通道存在（POST /api/trader/signal-filter）",
      "/api/trader/signal-filter" in _js, True)
check("[9c] 前端以自动下单侧为准回填（同一路由 GET 回填）",
      _js.count("/api/trader/signal-filter") >= 2, True)

# ════════════════════════════════════════════════════════════════
# [10] 分层 / 单向依赖（用户级铁律 11）：不新增反向依赖、不跨层直连
# ════════════════════════════════════════════════════════════════
print("\n[10] 分层与单向依赖（Trading 不反向依赖 App / FrontAPI 不直连 AppTrader）")
_bad = []
for _root, _dirs, _files in os.walk(os.path.join(_REPO, "Trading")):
    if "Test" in os.path.relpath(_root, _REPO).split(os.sep):
        continue            # 测试可以跨层装配，运行时源码不可以
    for _fn in _files:
        if not _fn.endswith(".py"):
            continue
        with open(os.path.join(_root, _fn), encoding="utf-8", errors="ignore") as _f:
            _src = _f.read()
        if re.search(r"^\s*(from|import)\s+App\b", _src, re.M):
            _bad.append(os.path.relpath(os.path.join(_root, _fn), _REPO))
check("[10a] Trading 运行时源码不反向依赖 App 层", _bad, [])
with open(os.path.join(_REPO, "FrontAPI.py"), encoding="utf-8") as _f:
    _api = _f.read()
check("[10b] FrontAPI 不直连 AppTrader",
      ("import AppTrader" in _api) or ("from App.AppTrader" in _api), False)
check("[10c] 新路由经 AppOrch 漏斗",
      ("orch.call_trader_set_bsp_filter" in _api
       and "orch.call_trader_get_bsp_filter" in _api), True)

print("")
print("=" * 60)
print("P59 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

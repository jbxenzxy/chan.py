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
  [9] 前后端「四类」集合一致（SSOT 漂移护栏）+ 前端回推通道存在
  [10] 分层与单向依赖：Trading 不反向依赖 App / FrontAPI 不直连 AppTrader
  [11] 空 / 缺键输入一律拒绝且不落库；完整四键与显式全 False 接受
  [12] 逗号串类型按段匹配（任一段命中即放行），两段都未勾则忽略并留痕
  [13] 状态目录同源：写入落点 == 子进程 --out（含 TRADING_STATE_DIR 覆盖）
  [14] 拒绝语义 400 / 留痕必落盘 / 只读不建库 / 推送后刷新（二轮评审 R1-R7 回归）

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
with open(os.path.join(_REPO, "Frontend", "app.html"), encoding="utf-8") as _f:
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

# ════════════════════════════════════════════════════════════════
# [11] 不完整的勾选一律拒绝（不许"缺键当未勾选"把信号门静默关死）
# ════════════════════════════════════════════════════════════════
print("\n[11] 空 / 缺键输入 → 拒绝写入（4xx，不是 HTTP 200 静默全关）")
import App.AppTrader as _AT                                     # noqa: E402

with tmp_dir() as tmp:
    os.environ["TRADING_STATE_DIR"] = tmp       # 写入落点重定向到临时目录
    try:
        _at = _AT.AppTrader.__new__(_AT.AppTrader)   # 不跑 __init__：不碰运行中的状态文件
        _at._handle = None
        _rejected = 0
        for _bad in (None, {}, {"0": True}, {"0": True, "1": True, "2": True},
                     {"0": True, "1": True, "2": True, "3": True, "9": True}):
            try:
                _at.set_bsp_filter(_bad)
            except Exception as _e:
                # 用 isinstance（不是类名相等）：400 语义落地后这里抛的是
                # AppError 的子类 BadRequestError，类名相等的写法会把它漏掉
                if isinstance(_e, _AT.AppError):
                    _rejected += 1
        check("[11a] 5 种不完整输入全部被拒", _rejected, 5)
        check("[11b] 被拒后没有落库",
              os.path.isfile(os.path.join(tmp, "state.db")), False)
        _r = _at.set_bsp_filter({t: True for t in BSP_TYPE_CHOICES})
        check("[11c] 完整四键 → 接受", sorted(_r["bsp_type_filter"]),
              sorted(BSP_TYPE_CHOICES))
        check("[11d] 全不勾必须显式四个 False → 接受",
              _at.set_bsp_filter({t: False for t in BSP_TYPE_CHOICES})
              ["bsp_type_filter"], {t: False for t in BSP_TYPE_CHOICES})
    finally:
        os.environ.pop("TRADING_STATE_DIR", None)

# ════════════════════════════════════════════════════════════════
# [12] 逗号串类型（同一笔同一右肩 K 合并出的多类型）按段匹配
#     type2str() 会产出 "1,2" 这类串：整串精确匹配会让"图上不画 + 单也不下"
# ════════════════════════════════════════════════════════════════
print("\n[12] 逗号串类型按段匹配（任一段勾选即放行）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    set_filter(store, {"0": False, "1": True, "2": False, "3": False})
    engine.on_signal(make_signal(is_buy=True, bsp_type="1,2",
                                date="2026-09-01 09:36"))
    check("[12a] 勾着的那一段命中 → 放行开仓", len(broker.orders), 1)
    set_filter(store, {"0": False, "1": False, "2": False, "3": True})
    engine.on_signal(make_signal(is_buy=True, bsp_type="1,2",
                                date="2026-09-01 09:37"))
    check("[12b] 两段都没勾 → 忽略（报单数不变）", len(broker.orders), 1)
    ev.flush()
    check("[12c] 忽略有留痕", "bsp_type_filtered" in skip_reasons(
        os.path.join(tmp, "events.jsonl")), True)

# ════════════════════════════════════════════════════════════════
# [13] 状态目录同源：写的一侧与子进程启动的一侧必须解析到同一个目录
#     （不一致 = 勾选写进引擎不读的 state.db = 过滤静默失效）
# ════════════════════════════════════════════════════════════════
print("\n[13] 状态目录同源（_filter_out_dir 与 start 缺省目录）")
_src_cfgres = inspect.getsource(_AT.AppTrader._cfg_out_dir)
_src_filt = inspect.getsource(_AT.AppTrader._filter_out_dir)
_src_start = inspect.getsource(_AT.AppTrader.start)
check("[13a] _filter_out_dir 走统一解析", "_cfg_out_dir()" in _src_filt, True)
check("[13b] start 缺省分支走同一解析", "_cfg_out_dir()" in _src_start, True)
check("[13c] _filter_out_dir 不再硬编码默认目录",
      "_DEFAULT_OUT" in _src_filt, False)
check("[13d] 解析基准 = Trading/", "_TG_ROOT" in _src_cfgres, True)
with tmp_dir() as tmp:
    os.environ["TRADING_STATE_DIR"] = tmp
    try:
        _at = _AT.AppTrader.__new__(_AT.AppTrader)
        _at._handle = None
        check("[13e] 环境变量覆盖 state_dir 时写入目录随之改变",
              os.path.normcase(os.path.abspath(
                  _at.set_bsp_filter({t: True for t in BSP_TYPE_CHOICES})
                  ["out_dir"])), os.path.normcase(os.path.abspath(tmp)))
    finally:
        os.environ.pop("TRADING_STATE_DIR", None)


# ════════════════════════════════════════════════════════════════
# [14] 二轮评审遗留项回归（R1 400 语义 / R2 留痕落盘 / R7 只读不建库 /
#      前端推送后刷新与在飞竞态）
# ════════════════════════════════════════════════════════════════
print("\n[14] 拒绝语义 400 + 留痕必落盘 + GET 不凭空建库 + 前端门刷新")
with tmp_dir() as tmp:
    os.environ["TRADING_STATE_DIR"] = tmp
    try:
        _at14 = _AT.AppTrader.__new__(_AT.AppTrader)   # 不跑 __init__：不碰运行中的状态文件
        _at14._handle = None
        _err = None
        try:
            _at14.set_bsp_filter({"0": True})
        except Exception as _e:
            _err = _e
        check("[14a] 缺键 → BadRequestError（子类，统一处理器照样捕）",
              type(_err).__name__, "BadRequestError")
        check("[14b] 状态码 400（用户少传字段不该显示「服务器错误」）",
              getattr(_err, "status_code", None), 400)
        check("[14c] 仍是领域异常基类（except AppError 仍兜得住）",
              isinstance(_err, _AT.AppError), True)

        # R7：目录存在但库还没建 → 读回 None，且不创建文件
        check("[14d] 无 state.db 时读回 None（= 从未推送 = 全部放行）",
              _at14.get_bsp_filter()["bsp_type_filter"], None)
        check("[14e] GET 不凭空创建 state.db（只读不建库）",
              os.path.isfile(os.path.join(tmp, "state.db")), False)

        # R2：留痕必须落到 out_dir/gateway.log —— 新部署"先勾好再开"时
        #      子进程还没起过，tee 从未安装，这条日志以前只进终端
        _r14 = _at14.set_bsp_filter({t: True for t in BSP_TYPE_CHOICES})
        _gl14 = os.path.join(_r14["out_dir"], "gateway.log")
        check("[14f] 首次推送也落 gateway.log（tee 就地安装）",
              os.path.isfile(_gl14), True)
        _txt14 = ""
        if os.path.isfile(_gl14):
            with open(_gl14, encoding="utf-8") as _f:
                _txt14 = _f.read()
        check("[14g] 日志内容含「买卖点类型过滤生效」（可复盘谁改的）",
              "买卖点类型过滤生效" in _txt14, True)
        check("[14h] 写盘后立刻读回同一份（写/读同一通道）",
              _at14.get_bsp_filter()["bsp_type_filter"],
              {t: True for t in BSP_TYPE_CHOICES})
    finally:
        os.environ.pop("TRADING_STATE_DIR", None)
        _h14 = getattr(_AT, "_log_file_handler", None)   # 收尾：别留着指向临时目录的 handler
        if _h14 is not None:
            try:
                _AT.log.removeHandler(_h14)
                _h14.close()
            except Exception:
                pass
            _AT._log_file_handler = None

# 前端四处（推送后刷新 / 非 2xx 落"未同步" / 在飞期间丢弃回填 / 绘制门先判空）：
# 前端是静态资源、无法 import，沿用 [9] 的读源码断言方式钉住不变量。
_i_guard = _js.find("if (bspFilter) {")
_i_access = _js.find("bspFilter[seg.trim()]")
check("[14i] 绘制门先判空再按键取值（bspFilter 被置空不会中断整段绘制）",
      0 <= _i_guard < _i_access, True)
check("[14j] 推送成功后刷新状态行（盘中改完立刻可核对）",
      "renderBspFilterEngineState(bspFilter);" in _js, True)
check("[14k] 非 2xx 与网络失败走同一「未同步」分支（不永停「读取中…」）",
      _js.count("renderBspFilterEngineState(null, true)") >= 3, True)
check("[14l] 推送在飞期间丢弃回填（不把用户刚改的勾选回滚）",
      "bspFilterPushInFlight" in _js, True)
check("[14m] POST 固定发满四键（缺键责任在唯一生产者一侧）",
      all(('"{}": !!bspFilter["{}"]'.format(_t, _t)) in _js
          for _t in BSP_TYPE_CHOICES), True)

print("")
print("=" * 60)
print("P59 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

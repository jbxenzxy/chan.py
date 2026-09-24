# -*- coding: utf-8 -*-
"""
P62 运行态保护价：后端投影 + 前端常驻显示
==========================================
背景（2026-09-23 当日 IM 实盘）：
  10:11 空 2 手 → 转多 2 手 @7584.6，R = 7.8 点；
  10:20 浮盈过 1R → 保护价抬到 7588.6（保本层，锁 0.5R）；
  此后一路到 10:42 最高 7605.0（2.6R），再回撤 —— **保护价全程 7588.6 没动**，
  而用户看不到它在哪，也无法判断"回撤到哪才会离场"。
  根因不是保护价算错（收盘价未跌破 7588.6，不平仓是对的），
  而是「保护价只存在于后端 `_run_plan.stop_price`，出口只有一个悬停 tooltip」。

本测试钉死两件事：
  [A] 后端投影：`auto_order_status()["run"]` 除 stop 外还给出它的**解释**
      （phase / r / tp），且三项与 `_run_plan` 实时一致、跨重启一致；
  [B] 前端出口（2026-09-24 改版：账本旁徽标 → K线主图横虚线）：
      `Frontend/app.js` 的 `calcProtectionLine` 消费 run 的 stop / side / phase
      构造画线状态（无运行段 → 不画），`drawProtectionLine` 在主图渲染管线里
      画橙色横虚线 + 右端「保护 价·层」标签；
      （资源版本号由 Test/test_aol_ledger_display.py ⑤ 组独占守卫）。

覆盖清单：
  [1] 初始层：投影 stop == 计划真值（非 0 = 有保护价）；phase 空；r / tp 就位
  [2] 保本层：浮盈过 breakeven_trigger_r·R → phase=breakeven、stop 抬到锚 + 0.5R
  [3] 跟踪层：浮盈过 win_loss_ratio·R → phase=trailing、stop = best − 0.5R
  [4] 实时性：跟踪层内再创新高 → stop 继续抬（杀掉"开仓快照"式实现）
  [5] 跨重启：_persist → 新引擎 _restore_run → 投影四项与恢复前逐个相等
  [6] 无运行段：空仓 / 锁仓 → run 为 None（前端据此不画线）
  [7] toast：移动止盈 toast 与保本 toast 一样带出保护价（2026-09-23 对称化）
  [8] 前端静态层：徽标元素与样式已删 / 画线组件就位 / 消费字段 / 只挂主图 /
      无运行段默认不画
  [9] 源码护栏：run 字典键必需项齐全，三项取值来源是 _run_plan.params，
      且 stop 必须来自引擎级实时计划（不得读仓单快照）
  [10] 前端行为层：`calcProtectionLine` 抽到 node 里跑真函数 ——
      三态（运行 / 空仓 / 非法值）、变化判定（价 / 层 / 向任一变 = 变）
      逐样本比对（node 不在位则跳过）
  [11] 两条投影同构：真实 `state.db` 上，引擎 `auto_order_status()["run"]` 与
      API 侧 `AppTrader._read_engine_switch()["run"]` **逐字段相等**；
      静态层另钉住两处键集合一致、取值来源一致

判别力（护栏不恒真）：
  删 `Engine.py` run 里 phase 一行      → [1c] / [2a] / [3a] / [4a] / [9b] / [9c] / [9d]
  stop 改读仓单 exit_plan（开仓冻结）   → [1b] / [2b] / [3b] / [4b] / [4c] / [9f]
  移动止盈 toast 去掉价位               → [7d] / [7e]
  徽标元素加回 app.html / 样式加回 css  → [8a] / [8i]
  画线组件改名或删掉                    → [8d] / [8e] / [10a]
  副图也画线                            → [8i2]
  空仓不撤线（changed 恒 False）        → [10g]
  phase 变价不变判为"没变"（不重绘）    → [10d]

投影字段一律用 `g()` / `num()` 读：缺键时报红而不是 KeyError 中断用例 ——
否则删掉一个键只会让这条用例在第 3 行崩掉，后面 40 项一条都看不到。

跑法：python Trading/Test/test_p62_ao_protection_price.py
"""
from __future__ import annotations

import ast
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
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
_REPO = os.path.dirname(_TG_ROOT)
sys.path.insert(0, _REPO)

from Trading.Broker.DryRun import DryRunBroker                   # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig         # noqa: E402
from Trading.Engine.Engine import TradingEngine                  # noqa: E402
from Trading.Infra.EventLog import EventLog                      # noqa: E402
from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES               # noqa: E402
from Trading.Infra.Records import Bar, Side, Signal              # noqa: E402
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


def g(d, k):
    """读投影字段：缺键返回 None（红），不抛 KeyError 中断用例。"""
    return (d or {}).get(k)


def num(d, k):
    """读投影里的数值：读不到返回 nan（比较恒 False → 报红）。"""
    try:
        return float(g(d, k))
    except (TypeError, ValueError):
        return float("nan")


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p62_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def build_engine(tmpdir, out_name="state.db", ev_name="events.jsonl"):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = Instrument(InstrumentConfig(trade_symbol=_SYM), _IF)
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    store = Store(os.path.join(tmpdir, out_name))
    ev = EventLog(os.path.join(tmpdir, ev_name), echo=False, echo_kinds=None)
    engine = TradingEngine(cfg, broker, EntryPolicy({}),
                           LayeredExitPolicy(), store, ev)
    return engine, store, broker, ev


def make_signal(is_buy=True, bsp_type="1", date="2026-09-01 09:35",
                price=4550.0, fractal_low=0.0, fractal_high=0.0):
    return Signal(key=Signal.make_key(date, bsp_type, is_buy), symbol=_SYM,
                  freq="5m", date=date, timestamp=0, bsp_type=bsp_type,
                  is_buy=is_buy, price=price, high=price + 2.0,
                  low=price - 2.0, fractal_low=fractal_low,
                  fractal_high=fractal_high)


def make_bar(ts, o=4550.0, h=4552.0, l=4548.0, c=4551.0):
    return Bar(timestamp=ts, date="2026-09-01 09:40", open=o, high=h,
               low=l, close=c, vol=1)


def run_view(engine):
    """投影里的本段运行视图（本测试的被测对象）。"""
    return engine.auto_order_status().get("run")


def toasts_of(store):
    return store.get_json("toasts") or []


def plan_of(engine):
    return engine._run_plan


# ════════════════════════════════════════════════════════════════
# [1] 初始层：投影四项 == 计划真值
# ════════════════════════════════════════════════════════════════
print("\n[1] 初始层：run 投影 stop / phase / r / tp == _run_plan 真值")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    rv = run_view(engine)
    pl = plan_of(engine)
    check("[1a] 有本段运行（run 非空）", rv is not None, True)
    check("[1b] stop == 计划保护价（且非 0 = 有保护价）",
          (g(rv, "stop") == pl.stop_price) and bool(g(rv, "stop")), True)
    check("[1c] phase == 计划快照 _phase（初始层为空串）",
          g(rv, "phase"), str(pl.params.get("_phase") or ""))
    check("[1d] r == 计划 R（1R 的点数）", g(rv, "r"), pl.params.get("R"))
    check("[1e] tp == 计划 _tp_nominal（转入跟踪层的那个价）",
          g(rv, "tp"), pl.params.get("_tp_nominal"))
    check("[1f] tp 在风控锚的有利侧（多仓：tp > anchor）",
          num(rv, "tp") > num(rv, "anchor"), True)
    check("[1g] side / anchor / volume / name 四项未被影响",
          (g(rv, "side") == "LONG" and g(rv, "volume") == 2
           and g(rv, "name") == pl.name), True)

# ════════════════════════════════════════════════════════════════
# [2] 保本层
# ════════════════════════════════════════════════════════════════
print("\n[2] 保本层：浮盈过 breakeven_trigger_r·R → phase=breakeven + 保护价抬升")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    entry = engine._run_anchor
    R = engine._run_plan.params["R"]
    be_trig = engine.exit_policy.breakeven_trigger_r
    be_buf = engine.exit_policy.breakeven_buffer_r
    init_stop = engine._run_plan.stop_price
    h1 = entry + be_trig * R + 10.0
    engine.on_bar(make_bar(2000, h=h1, l=entry - 5.0, c=h1 - 5.0))
    rv = run_view(engine)
    check("[2a] phase 抬到 breakeven", g(rv, "phase"), "breakeven")
    check("[2b] 保护价 == 风控锚 + breakeven_buffer_r·R（tick 取整后）",
          g(rv, "stop"), engine._run_plan.stop_price)
    check("[2c] 保护价确实高于初始止损",
          num(rv, "stop") > float(init_stop), True)
    check("[2d] 抬升量 ≈ breakeven_buffer_r·R",
          abs((num(rv, "stop") - entry) - be_buf * R) <= 0.2, True)
    check("[2e] r / tp 不随抬价而变（同一段 run 的常量）",
          (g(rv, "r") == R and g(rv, "tp") == engine._run_plan.params["_tp_nominal"]),
          True)

# ════════════════════════════════════════════════════════════════
# [3][4] 跟踪层 + 实时性
# ════════════════════════════════════════════════════════════════
print("\n[3] 跟踪层：浮盈过 win_loss_ratio·R → phase=trailing + 保护价 = best − 0.5R")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    entry = engine._run_anchor
    R = engine._run_plan.params["R"]
    wlr = engine.exit_policy.win_loss_ratio
    trail = engine.exit_policy.trailing_trigger_r
    h1 = entry + wlr * R + 10.0
    engine.on_bar(make_bar(2000, h=h1, l=entry, c=h1 - 5.0))
    rv = run_view(engine)
    check("[3a] phase 抬到 trailing", g(rv, "phase"), "trailing")
    check("[3b] 保护价 == 计划真值（不是开仓快照）",
          g(rv, "stop"), engine._run_plan.stop_price)
    check("[3c] 保护价 == 该根有利极值 − trailing_trigger_r·R（tick 取整后）",
          abs((h1 - trail * R) - num(rv, "stop")) <= 0.2, True)

    print("\n[4] 实时性：跟踪层内再创新高 → 保护价逐根抬高")
    stop_before = num(rv, "stop")
    h2 = h1 + 20.0
    engine.on_bar(make_bar(3000, h=h2, l=h1 - 10.0, c=h2 - 2.0))
    rv2 = run_view(engine)
    check("[4a] phase 仍是 trailing", g(rv2, "phase"), "trailing")
    check("[4b] 保护价已随新高抬高", num(rv2, "stop") > stop_before, True)
    check("[4c] 抬高后的值 == 计划真值", g(rv2, "stop"),
          engine._run_plan.stop_price)
    check("[4d] 抬高量与极值增量一致",
          abs((num(rv2, "stop") - stop_before) - (h2 - h1)) <= 0.2, True)

    # 回撤但不破保护价 → 保护价不回吐（单调）
    stop_peak = num(rv2, "stop")
    engine.on_bar(make_bar(4000, h=h2 - 1.0, l=h2 - 15.0, c=h2 - 12.0))
    rv3 = run_view(engine)
    check("[4e] 回撤时保护价不下降（单调）",
          num(rv3, "stop") >= stop_peak, True)

# ════════════════════════════════════════════════════════════════
# [5] 跨重启：投影四项逐项一致
# ════════════════════════════════════════════════════════════════
print("\n[5] 跨重启：_persist → 新引擎 _restore_run → 投影一致")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    entry = engine._run_anchor
    R = engine._run_plan.params["R"]
    h1 = entry + engine.exit_policy.breakeven_trigger_r * R + 10.0
    engine.on_bar(make_bar(2000, h=h1, l=entry - 5.0, c=h1 - 5.0))
    engine._persist()
    before = run_view(engine)
    ev.flush()
    store.close()            # 关掉写连接：WAL 未 checkpoint 时直接拷库会漏数据
    # 同一份 state.db 换一个引擎实例（构造即 _restore → _restore_run）
    engine2, store2, broker2, ev2 = build_engine(tmp, "state.db",
                                                 "events2.jsonl")
    after = run_view(engine2)
    check("[5a] 恢复后 run 非空", after is not None, True)
    check("[5b] 四项（stop/phase/r/tp）逐项相等",
          (g(after, "stop") == g(before, "stop")
           and g(after, "phase") == g(before, "phase")
           and g(after, "r") == g(before, "r")
           and g(after, "tp") == g(before, "tp")), True)
    check("[5c] 恢复后 stop 非 0（保护价跨重启不丢）",
          bool(g(after, "stop")), True)

# ════════════════════════════════════════════════════════════════
# [6] 无运行段 → run 为 None（前端据此隐藏）
# ════════════════════════════════════════════════════════════════
print("\n[6] 无运行段：空仓 / 锁仓 → run 为 None")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    check("[6a] 空仓时 run 为 None", run_view(engine), None)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    check("[6b] 开仓后 run 非空", run_view(engine) is not None, True)
    # 关闭自动下单 → 清场（今仓反向开仓锁仓）→ 净敞口 0
    engine.shutdown_and_lock_all()
    check("[6c] 清场后 run 为 None（不残留旧保护价）", run_view(engine), None)

# ════════════════════════════════════════════════════════════════
# [7] toast：移动止盈与保本同样带出保护价
# ════════════════════════════════════════════════════════════════
print("\n[7] 阶段 toast 都带保护价（保本 / 移动止盈对称）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    entry = engine._run_anchor
    R = engine._run_plan.params["R"]
    h1 = entry + engine.exit_policy.breakeven_trigger_r * R + 10.0
    engine.on_bar(make_bar(2000, h=h1, l=entry - 5.0, c=h1 - 5.0))
    msgs = [t["msg"] for t in toasts_of(store)]
    be = [m for m in msgs if "保本" in m and "平仓" not in m]
    check("[7a] 保本 toast 带保护价", bool(be) and "止损已移至" in be[-1], True)
    check("[7b] 保本 toast 的价位 == 当时的计划保护价", "{:g}".format(
        engine._run_plan.stop_price) in be[-1], True)

    h2 = entry + engine.exit_policy.win_loss_ratio * R + 10.0
    engine.on_bar(make_bar(3000, h=h2, l=h1 - 10.0, c=h2 - 5.0))
    msgs = [t["msg"] for t in toasts_of(store)]
    tr = [m for m in msgs if "移动止盈" in m and "平仓" not in m]
    check("[7c] 移动止盈 toast 出现", bool(tr), True)
    check("[7d] 移动止盈 toast 也带保护价（2026-09-23 对称化）",
          bool(tr) and "止损已移至" in tr[-1], True)
    check("[7e] 移动止盈 toast 的价位 == 当时的计划保护价",
          bool(tr) and "{:g}".format(engine._run_plan.stop_price) in tr[-1], True)

# ════════════════════════════════════════════════════════════════
# [8] 前端静态层（2026-09-24 改版：账本旁徽标 → K线主图横虚线）
# ════════════════════════════════════════════════════════════════
print("\n[8] 前端：徽标已删 + 画线组件就位 + 无运行段不画")
_html_p = os.path.join(_REPO, "Frontend", "app.html")
_js_p = os.path.join(_REPO, "Frontend", "app.js")
_css_p = os.path.join(_REPO, "Frontend", "app.css")
if not (os.path.exists(_html_p) and os.path.exists(_js_p)):
    print("  [SKIP] Frontend 不在场（部分树），跳过前端层")
else:
    HTML = io.open(_html_p, encoding="utf-8").read()
    JS = io.open(_js_p, encoding="utf-8").read()
    CSS = io.open(_css_p, encoding="utf-8").read()
    # ── 徽标退场（2026-09-24 用户拍板：账本后面没空间，改画 K线横虚线）──
    check("[8a] app.html 徽标元素已删除（auto-order-px 零残留）",
          "auto-order-px" in HTML, False)
    # 资源版本号不在这里断言 —— 它由 Test/test_aol_ledger_display.py 的 ⑤ 组
    #   独占守卫（那条契约的存在意义就是"改前端必须抬版本号"）。两处各写一份
    #   只会让抬版本号时要改两个文件，且不增加任何拦截力。
    # ── 位置契约：徽标删掉后 auto-order 一组子元素清单同步收缩 ──
    #   ⚠️ 断言形态仍是"全等"（不是"包含"）：序列一变 ⇒ 有人动过展示顺序，
    #   必须回来改这条。
    _wrap_seg = HTML[HTML.index('id="auto-order-wrap"'):
                     HTML.index('id="auto-order-ledger-panel"')]
    check("[8b] auto-order 一组子元素顺序 = 受控清单（徽标已从清单移除）",
          re.findall(r'id="([\w-]+)"', _wrap_seg),
          ["auto-order-wrap", "auto-order-dot", "auto-order-label",
           "auto-order-hint", "auto-order-checkbox", "auto-order-ledger-btn"])
    check("[8b2] 旧渲染函数 renderAutoOrderPrice 在 app.js 零残留",
          "renderAutoOrderPrice" in JS, False)
    # ── 画线组件就位 ──
    check("[8d] app.js 定义了纯函数 calcProtectionLine 与绘制函数 drawProtectionLine",
          ("function calcProtectionLine(" in JS
           and "function drawProtectionLine(" in JS), True)
    check("[8e] 轮询回调里真的调用了它（changed 才赋值 + 重绘）",
          ("calcProtectionLine(aoRun, protectionLine)" in JS
           and "protectionLine = _pl.line" in JS), True)
    check("[8f] 画线消费 run.stop / run.side / run.phase（三态的三个来源）",
          ("aoRun.stop" in JS and "aoRun.side" in JS and "aoRun.phase" in JS),
          True)
    check("[8g] 徽标专属解释字段 r / tp 前端零残留（画线只认价格与层）",
          ("aoRun.r" in JS or "aoRun.tp" in JS), False)
    check("[8h] 无运行段默认不画：protectionLine 初始 null + 绘制函数开头早退",
          ("let protectionLine = null;" in JS
           and "if (!protectionLine || !isFinite(protectionLine.price)) return;" in JS),
          True)
    check("[8i] app.css 徽标样式已删除（.auto-order-px 零残留）",
          ".auto-order-px" in CSS, False)
    check("[8i2] 画线只挂主图（dualSubData 副图不画）",
          "if (data !== dualSubData) drawProtectionLine(" in JS, True)
    check("[8j] 账本面板「持仓行无止损列」契约未被破坏"
          "（renderAutoOrderLedger 区块仍零命中）",
          JS[JS.index("function renderAutoOrderLedger(data) {"):
             JS.index("// 价格显示")].count("止损"), 0)

# ════════════════════════════════════════════════════════════════
# [9] 源码护栏（AST）：投影键齐全 + 取值来源
# ════════════════════════════════════════════════════════════════
print("\n[9] 源码护栏：run 投影键与取值来源")
_src = textwrap.dedent(inspect.getsource(TradingEngine.auto_order_status))
_tree = ast.parse(_src)
_fn = _tree.body[0]
_run_dict = None
for _node in ast.walk(_fn):
    if isinstance(_node, ast.Dict):
        _ks = [k.value for k in _node.keys if isinstance(k, ast.Constant)]
        if "stop" in _ks and "anchor" in _ks:
            _run_dict = _node
            break
check("[9a] 定位到 run 字典字面量（防抓空）", _run_dict is not None, True)
if _run_dict is not None:
    _keys = sorted(k.value for k in _run_dict.keys
                   if isinstance(k, ast.Constant))
    _need = {"stop", "phase", "r", "tp"}
    check("[9b] 键集合含 stop/phase/r/tp",
          _need.issubset(set(_keys)), True)
    check("[9c] 键集合即受控清单（新增须同步本行）", _keys,
          ["anchor", "name", "phase", "r", "side", "stop", "tp", "volume"])
    # 三项新字段必须取自 self._run_plan.params（stop 是计划的顶层字段，见 [9e]）
    _need_params = {"phase", "r", "tp"}
    _from_params = set()
    for _k, _v in zip(_run_dict.keys, _run_dict.values):
        if not isinstance(_k, ast.Constant):
            continue
        if _k.value not in _need_params:
            continue
        if "_run_plan.params" in ast.unparse(_v):
            _from_params.add(_k.value)
    check("[9d] phase / r / tp 三项全部取自 _run_plan.params",
          sorted(_from_params), sorted(_need_params))
    # stop 必须是实时计划（而不是仓单上冻结的那份）
    _stop_val = [ast.unparse(_v) for _k, _v in zip(_run_dict.keys,
                                                   _run_dict.values)
                 if isinstance(_k, ast.Constant) and _k.value == "stop"]
    _stop_src = _stop_val[0] if _stop_val else ""
    check("[9e] stop 取自 _run_plan（实时计划，不是仓单快照）",
          "_run_plan.stop_price" in _stop_src, True)
    # 负向子句：即便写成"优先读仓单、读不到再退回计划"也放过 —— 那正是
    #   position.exit_plan 冻结在开仓那一刻的形态（仓单在开仓时被塞进同一份
    #   plan 引用，之后引擎换新对象、仓单上那份不再更新）。
    check("[9f] stop 表达式不读仓单快照（self.positions / .exit_plan 零命中）",
          ("self.positions" not in _stop_src
           and ".exit_plan" not in _stop_src), True)

# ════════════════════════════════════════════════════════════════
# [10] 前端行为层：calcProtectionLine 抽到 node 里跑真函数
# ════════════════════════════════════════════════════════════════
print("\n[10] 前端行为层（node 真函数）：画线状态机 / 三态 / 变化判定")
if not (os.path.exists(_html_p) and os.path.exists(_js_p)):
    print("  [SKIP] Frontend 不在场（部分树），跳过行为层")
else:
    _js = io.open(_js_p, encoding="utf-8").read()
    _m_fn = re.search(
        r"(        function calcProtectionLine\(aoRun, prev\) \{[\s\S]*?\n        \})",
        _js)
    check("[10a] 函数源码可抽取（防抓空）", _m_fn is not None, True)
    _node = shutil.which("node")
    if _m_fn is None:
        pass
    elif not _node:
        print("  [SKIP] node 不在位，只跑静态层")
    else:
        _fn_src = re.sub(r"^        ", "", _m_fn.group(1), flags=re.M)
        _base = {"side": "LONG", "anchor": 7584.6, "volume": 2,
                 "name": "run_managed", "stop": 7588.6, "phase": "breakeven",
                 "r": 7.8, "tp": 7608}

        def _case(**kw):
            d = dict(_base)
            d.update(kw)
            return d

        def _line(price, side="LONG", phase="breakeven"):
            return {"price": price, "side": side, "phase": phase}

        _cases = [
            {"id": "[10b]", "what": "运行态（保本层）→ 画出该价",
             "prev": None, "run": _case(),
             "want_line": _line(7588.6), "want_changed": True},
            {"id": "[10c]", "what": "phase 抬到跟踪层（价也变）→ 变化",
             "prev": _line(7588.6), "run": _case(phase="trailing", stop=7608.2),
             "want_line": _line(7608.2, phase="trailing"), "want_changed": True},
            {"id": "[10d]", "what": "同价不同层（phase 变）→ 也算变化（标签要跟着跳）",
             "prev": _line(7608.2, phase="breakeven"),
             "run": _case(phase="trailing", stop=7608.2),
             "want_line": _line(7608.2, phase="trailing"), "want_changed": True},
            {"id": "[10e]", "what": "方向反转（多转空）→ 变化",
             "prev": _line(7577.2, side="SHORT"),
             "run": _case(side="LONG", stop=7577.2),
             "want_line": _line(7577.2), "want_changed": True},
            {"id": "[10f]", "what": "轮询值完全没变 → 不重绘（5s 一次别白画）",
             "prev": _line(7588.6), "run": _case(),
             "want_line": _line(7588.6), "want_changed": False},
            {"id": "[10g]", "what": "空仓（run=None）→ 撤线",
             "prev": _line(7588.6), "run": None,
             "want_line": None, "want_changed": True},
            {"id": "[10h]", "what": "连续空仓（None→None）→ 不重绘",
             "prev": None, "run": None,
             "want_line": None, "want_changed": False},
            {"id": "[10i]", "what": "保护价为 0 且本就无线 → 不画、也不重绘（没线→没线视觉零变化）",
             "prev": None, "run": _case(stop=0),
             "want_line": None, "want_changed": False},
            {"id": "[10j]", "what": "stop 非数值且本就无线 → 不画、也不重绘",
             "prev": None, "run": _case(stop="abc"),
             "want_line": None, "want_changed": False},
        ]
        _harness = """
const cases = __CASES__;
const out = cases.map(function (c) {
    let err = '';
    let r = null;
    try { r = calcProtectionLine(c.run, c.prev); } catch (e) { err = String(e); }
    if (err) return { err: err };
    return { err: err, line: r.line, changed: r.changed };
});
console.log(JSON.stringify(out));
"""
        with tempfile.TemporaryDirectory(prefix="tg_p62_js_") as _td:
            _jsp = os.path.join(_td, "probe.js")
            io.open(_jsp, "w", encoding="utf-8").write(
                _fn_src + "\n" + _harness.replace("__CASES__", json.dumps(
                    [{"run": c.get("run"), "prev": c.get("prev")}
                     for c in _cases], ensure_ascii=False)))
            r = subprocess.run([_node, _jsp], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=30)
            if r.returncode != 0:
                check("[10b..] node 执行状态机", r.stderr.strip()[-200:], "")
            else:
                got = json.loads(r.stdout.strip())
                check("[10b..] 九个样本全部无异常",
                      [o["err"] for o in got], [""] * len(_cases))
                for c, o in zip(_cases, got):
                    tag = "{} {}".format(c["id"], c["what"])
                    check(tag + " → line / changed",
                          (o.get("line"), o.get("changed")),
                          (c["want_line"], c["want_changed"]))

# ════════════════════════════════════════════════════════════════
# [11] 两条投影同构：引擎侧 vs API 侧（前端只认一种形状）
# ════════════════════════════════════════════════════════════════
# 子进程架构下前端拿到的是 `AppTrader._read_engine_switch()` 从 state.db 的 kv
# `run` 重新投影出来的那份，**不是** `Engine.auto_order_status()` 的返回值。
# 两处各自成文（API 侧刻意不 import 引擎），所以必须钉"输出同构"：
# 键名 / 取值来源一旦分叉，徽标拿到的字段就是死的。
print("\n[11] 两条投影同构：引擎 auto_order_status() vs API _read_engine_switch()")
_APP_P = os.path.join(_REPO, "App", "AppTrader.py")
if not os.path.exists(_APP_P):
    print("  [SKIP] App/AppTrader.py 不在场（只解压了 Trading/），跳过同构层")
else:
    _APP = io.open(_APP_P, encoding="utf-8").read()
    _app_tree = ast.parse(_APP)


    def _find_run_dict(tree, owner):
        """按"含 stop 与 anchor 两个键的 dict 字面量"定位投影块。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == owner:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Dict):
                        ks = [k.value for k in sub.keys
                              if isinstance(k, ast.Constant)]
                        if "stop" in ks and "anchor" in ks:
                            return sub
        return None


    _eng_run = _find_run_dict(ast.parse(textwrap.dedent(
        inspect.getsource(TradingEngine.auto_order_status))),
        "auto_order_status")
    _api_run = _find_run_dict(_app_tree, "_read_engine_switch")
    check("[11a] 两处 run 投影块都定位到（防抓空）",
          (_eng_run is not None and _api_run is not None), True)
    if _eng_run is not None and _api_run is not None:
        _k_eng = sorted(k.value for k in _eng_run.keys
                        if isinstance(k, ast.Constant))
        _k_api = sorted(k.value for k in _api_run.keys
                        if isinstance(k, ast.Constant))
        check("[11b] 键集合逐字一致（两处各造一套键名 = 前端有一半字段是死的）",
              _k_api, _k_eng)
        _src_api = {k.value: ast.unparse(v) for k, v in
                    zip(_api_run.keys, _api_run.values)
                    if isinstance(k, ast.Constant)}
        # 取值来源钉到"kv 里与引擎 params 同名的那套键"：`plan.params` 的
        # `_phase` / `R` / `_tp_nominal`。键名一旦分叉（比如 API 侧改读
        # `phase` / `r` / `tp`），拿到的就是 None，而这里能先一步红。
        def _get_keys(expr_src):
            """表达式里所有 `x.get('K')` 的键名（按 AST 取常量，不做子串匹配；
            允许包一层 `str(... or '')`"。"""
            try:
                node = ast.parse(expr_src, mode="eval").body
            except SyntaxError:
                return set()
            out = set()
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "get" and len(sub.args) == 1
                        and isinstance(sub.args[0], ast.Constant)):
                    out.add(sub.args[0].value)
            return out


        _want_param_key = {"phase": "_phase", "r": "R", "tp": "_tp_nominal"}
        _bad = sorted(k for k, v in _want_param_key.items()
                      if v not in _get_keys(_src_api.get(k, "")))
        check("[11c] API 侧 phase / r / tp 三项取自 plan.params 的同名键",
              _bad, [])
        check("[11c2] API 侧读的 kv 键就是 `params`（与引擎 params 同源）",
              'plan.get("params")' in _APP, True)
        check("[11d] API 侧 stop 取自 kv 计划快照 plan.stop_price",
              "stop_price" in _get_keys(_src_api.get("stop", "")), True)

    # ── 运行态：同一份 state.db，两条投影逐字段相等 ──
    from App.AppTrader import AppTrader                             # noqa: E402

    def _api_run_of(tmpdir):
        d = AppTrader._read_engine_switch(tmpdir)
        return None if not isinstance(d, dict) else d.get("run")

    _stages = []
    with tmp_dir() as tmp:
        engine, store, broker, ev = build_engine(tmp)
        engine.on_bar(make_bar(1000))
        engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                     fractal_low=4500.0))
        _entry = engine._run_anchor
        _R = engine._run_plan.params["R"]
        engine._persist()
        _stages.append(("初始层", run_view(engine), _api_run_of(tmp)))
        engine.on_bar(make_bar(2000,
                               h=_entry + engine.exit_policy.breakeven_trigger_r * _R + 10.0,
                               l=_entry - 5.0,
                               c=_entry + engine.exit_policy.breakeven_trigger_r * _R + 5.0))
        engine._persist()
        _stages.append(("保本层", run_view(engine), _api_run_of(tmp)))
        engine.on_bar(make_bar(3000,
                               h=_entry + engine.exit_policy.win_loss_ratio * _R + 10.0,
                               l=_entry,
                               c=_entry + engine.exit_policy.win_loss_ratio * _R))
        engine._persist()
        _stages.append(("跟踪层", run_view(engine), _api_run_of(tmp)))
        store.close()
    for _name, _ev, _av in _stages:
        check("[11e] {} 两条投影逐字段相等（键 + 值）".format(_name), _av, _ev)
    check("[11f] 三个时点的 phase 依次为 空 / breakeven / trailing",
          [s[1].get("phase") for s in _stages], ["", "breakeven", "trailing"])
    check("[11g] API 侧同样给出 1R 与止盈启动价（非 None）",
          [s[2].get("r") is not None and s[2].get("tp") is not None
           for s in _stages], [True, True, True])
    with tmp_dir() as tmp:
        engine, store, broker, ev = build_engine(tmp)
        engine._persist()
        check("[11h] 无运行段：两条投影同为 None",
              (run_view(engine), _api_run_of(tmp)), (None, None))
        store.close()

print("\n" + "=" * 60)
print("p62_ao_protection_price: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

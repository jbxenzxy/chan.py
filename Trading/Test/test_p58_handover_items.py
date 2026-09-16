# -*- coding: utf-8 -*-
"""
P58 交接待补清单第 1/4/5 条契约（2026-09-16）
================================================================
本轮按需求方裁定，落地交接文档「§7 待补清单」的**第 1、4、5 条**
（第 2/3/6/7/8/9/10 条由需求方明确关闭，不在本测试范围）。

第 1 条 —— 报单前校验拒单的可见性
----------------------------------------------------------------
**问题**：此前只有"柜台侧拒单"（broker 返回非 filled）会经 `_alert_on_reject`
升级为告警；而**报单前校验链**（`_pre_trade_check`）拦下的拒单只写一条
`order_rejected` 流水账，**不接告警** → 前端完全看不见。后果有两处：
  ① 拒单原因送不到界面（原诉求）；
  ② "合约参数取不到 → 一直拒单"静默卡住（用户以为引擎在跑，实际一笔没发）。

**修法**：`_execute` 在 `_pre_trade_check` 返回非 None 时调
`_note_precheck_reject`，把拒因按分级表接进既有 D11 告警通道（自动获得
同 code 合并计数 + 确认水位），并对"同因连拒 N 次"升级一档。

覆盖：
  [1a] 原本静默的拒因（no_time_anchor）→ 产生告警，extra.reason 带拒因
  [1b] 一过性拒因给 warn（不打断人）
  [1c] 账实不符类拒因给 severe（需人工介入）
  [1d] **已自行告警的拒因不重发**（instrument_unverified 只 1 条，防弹两框）
  [1e] 同因连拒 → 合并计数（队列不膨胀）、streak 递增
  [1f] warn 档连拒达阈值 → 自动升级 severe（escalated=True）
  [1g] 换了原因 → 计数重来（避免 A 拦 2 次 + B 拦 1 次把 B 误升级）
  [1h] 报单真的成功 → 连拒计数器归零

第 4 条 —— 配置样例补交易网关段
----------------------------------------------------------------
`.env.example` 此前**完全没有交易参数**。补一段注释块，覆盖：
通道选择 / 市场名 / 实盘确认开关 / 取值策略 / 状态目录 / 凭据说明 / 手数说明。

覆盖：
  [4a] `.env.example` 含交易网关段落标记
  [4b] 六类键名都在（TRADING_BROKER / TQ_MARKET / CONFIRM_LIVE_TRADING /
       INSTRUMENT_FETCH_POLICY / STATE_DIR / SOURCE__FREQ）
  [4c] 六类键**实际都能被 TradingConfig 解析**（不只写了名字）
  [4d] 明示凭据走环境变量、不进配置文件
  [4e] 明示手数不在配置里（旧的 max_volume 旋钮已删）

第 5 条 —— 收尾确认信号
----------------------------------------------------------------
关闭自动下单后必须给**明确终局结论**："已清仓、无残留" 或 "仍有残留（哪些）"。
此前只覆盖"锁仓态"一种，FLAT **一声不响**、离场失败 **毫无提示**（带仓过夜）。

覆盖：
  [5a] FLAT   → warn shutdown_result_flat（正向确认）
  [5b] RUNNING→ severe shutdown_result_residual（离场未成功，必须人工）
  [5c] LOCKED → warn account_frozen（保留原语义）
  [5d] `_force_exit` 返回本次委托（收尾路径据此区分成败）
  [5e] 三态各自都写 `shutdown_result` 事件（可审计）

跑法：python Trading/Test/test_p58_handover_items.py
"""
from __future__ import annotations

import dataclasses
import inspect
import os
import subprocess
import sys
import tempfile

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

from Trading.Broker.Base import Broker                      # noqa: E402
from Trading.Broker.DryRun import DryRunBroker              # noqa: E402
from Trading.Config import TradingConfig, resolved_exit_params  # noqa: E402
from Trading.Engine.Engine import TradingEngine, _Action    # noqa: E402
from Trading.Engine.PositionBook import PositionBook        # noqa: E402
from Trading.Infra.EventLog import EventLog                 # noqa: E402
from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES          # noqa: E402
from Trading.Infra.Records import (AccountState, Bar, ExitPlan,  # noqa: E402
                                   Order, OrderIntent, Position, Side)
from Trading.Infra.StateDB import Store                     # noqa: E402
from Trading.Strategy.Entry import EntryPolicy              # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy         # noqa: E402

_IF = PRODUCT_PROFILES["IF"]

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


# ════════════════════════════════════════════════════════════════
# 测试替身
# ════════════════════════════════════════════════════════════════
class _CountingLiveBroker(Broker):
    """在线通道替身：is_offline=False → A′ 闸门生效；submit 记账。"""
    name = "simnow"
    is_offline = False

    def __init__(self, state):
        self.state = state
        self.submit_calls = []
        self.params = {"instrument_fetch_policy": "strict"}

    def _param(self, key, default=None):
        return (self.params or {}).get(key, default)

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        self.submit_calls.append(signal_key)
        raise AssertionError("本测试不应触发真实报单")


class _FillBroker(_CountingLiveBroker):
    """在线通道替身 + 立即成交（用于验证成功后计数器归零）。"""
    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        self.submit_calls.append(signal_key)
        return Order(order_id="o1", signal_key=signal_key,
                     symbol=self.state.trade_symbol, side=side,
                     action=intent.value, volume=volume, price=ref_price,
                     req_price=ref_price, filled_price=ref_price,
                     status="filled", broker=self.name,
                     meta={"intent": intent.value})


def _st(verified=False) -> Instrument:
    st = Instrument(InstrumentConfig(signal_symbol="KQ.m@CFFEX.IF",
                                     trade_symbol="CFFEX.IF2609"), _IF)
    st.verified = verified
    return st


def make_engine(broker, state, tmpdir, capture_alerts=True):
    """造一台引擎。

    返回的 `eng` 挂两个测试用属性（不影响生产代码）：
      · `eng._t_ev`     —— 本次用的 EventLog，读事件前须 `.flush()`
      · `eng._t_alerts` —— 捕获到的告警条目列表（capture_alerts=True 时）
    注意 `EventLog.write` 是**缓冲**的（满 64 条或距上次 flush ≥1s 才落盘），
    所以 `_events()` 必须先 flush 再读，否则刚写的事件还在内存里。
    """
    cfg = TradingConfig()
    cfg.instrument = state
    alerts = []
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False,
                  echo_kinds=None)
    eng = TradingEngine(
        cfg, broker, EntryPolicy({}),
        LayeredExitPolicy(resolved_exit_params(TradingConfig())),
        Store(os.path.join(tmpdir, "state.db")),
        ev, state=state)
    eng._t_ev = ev
    eng._t_alerts = alerts

    if capture_alerts:
        # 替身复刻真实 alert() 的合并语义（否则测不到 [1e]/[1f] 的行为）：
        #   ① 同 code 合并：不新开条目，只推进 n / last_ts（保留首次 ts）
        #   ② 级别只升不降：severe 覆盖 warn，反向不动
        # 与 Engine.alert 的差异仅在于不落盘 / 不写 ev（测试只关心队列形状）。
        def _cap(level, code, msg, **extra):
            for a in eng._alerts:
                if a.get("code") == code:
                    a["n"] = int(a.get("n") or 1) + 1
                    a["msg"] = msg
                    a.update(extra)
                    if level == TradingEngine.ALERT_SEVERE \
                            and a.get("level") != TradingEngine.ALERT_SEVERE:
                        a["level"] = TradingEngine.ALERT_SEVERE
                    return a
            rec = {"level": level, "code": code, "msg": msg, "n": 1}
            rec.update(extra)
            eng._alerts.append(rec)
            alerts.append(rec)
            return rec
        eng.alert = _cap
    return eng, alerts, tmpdir


def _act_open(**kw):
    base = dict(intent=OrderIntent.OPEN, side=Side.LONG, volume=2,
                target=None, is_exit=False, transition=1)
    base.update(kw)
    return _Action(**base)


def _mkpos(side, vol, price, seq, key):
    return Position(symbol="CFFEX.IF2609", side=side, volume=vol,
                    entry_price=price, entry_at="2026-09-01 09:30:00",
                    entry_bar_ts=0, signal_key=key, open_order_id="",
                    exit_plan=ExitPlan(name="t", stop_price=price - 20.0,
                                       tp_price=price + 40.0),
                    entry_bar_seq=seq, entry_date="2026-09-01")


def _events(tmpdir, eng=None):
    """读回 events.jsonl（每行一个 dict）。

    EventLog 是缓冲写（满 64 条 / 距上次 flush ≥1s 才落盘），所以传 `eng` 时
    先 `flush()` —— 否则刚写的事件还在内存 `_buf` 里，读文件读不到。
    """
    import json
    if eng is not None:
        try:
            eng._t_ev.flush()
        except Exception:
            pass
    p = os.path.join(tmpdir, "events.jsonl")
    out = []
    if not os.path.isfile(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


_BAR = Bar(timestamp=1758038400000, date="2026-09-17", open=4500.0,
           high=4510.0, low=4490.0, close=4505.0, vol=100)

print("\nP58：交接待补清单第 1/4/5 条")

# ════════════════════════════════════════════════════════════════
print("\n[1] 第 1 条：报单前校验拒单接进告警通道")

# [1a] 原本静默的拒因现在有告警
eng, alerts, _t = make_engine(_CountingLiveBroker(_st(verified=True)),
                              _st(verified=True),
                              tempfile.mkdtemp(prefix="tg_p58_a_"))
eng.auto_order_enabled = True
eng._execute(_act_open(), ref_price=4500.0)
check("[1a] 静默拒因（no_time_anchor）现在有告警", len(alerts), 1)
check("[1a] extra.reason 带拒因",
      alerts[0].get("reason") if alerts else None, "no_time_anchor")

# [1b] 一过性拒因给 warn
check("[1b] 一过性拒因 → warn 档", alerts[0]["level"] if alerts else None,
      "warn")

# [1c] 账实不符类给 severe
eng_c, alerts_c, _tc = make_engine(_CountingLiveBroker(_st(verified=True)),
                                   _st(verified=True),
                                   tempfile.mkdtemp(prefix="tg_p58_c_"))
eng_c._execute(_act_open(intent=OrderIntent.CLOSE, side=Side.LONG,
                         volume=2, target=None, is_exit=True, transition=5),
               ref_price=4500.0)
check("[1c] 账实不符类（close_without_target）→ severe 档",
      alerts_c[0]["level"] if alerts_c else None, "severe")

# [1d] 已自行告警的拒因不重发
eng_d, alerts_d, _td = make_engine(_CountingLiveBroker(_st()),
                                   _st(), tempfile.mkdtemp(prefix="tg_p58_d_"))
eng_d._execute(_act_open(), ref_price=4500.0)
check("[1d] 闸门自带告警不重发（只 1 条）", len(alerts_d), 1)
check("[1d] 是闸门那条（instrument_unverified）",
      alerts_d[0]["code"] if alerts_d else None, "instrument_unverified")
check("[1d] 但 streak 照常计数", eng_d._reject_streak, 1)

# [1e] 同因连拒 → 合并计数
for _ in range(4):
    eng._execute(_act_open(), ref_price=4500.0)
check("[1e] 队列仍只 1 条（同 code 合并）", len(alerts), 1)
check("[1e] n 合并计数 = 5", alerts[0]["n"] if alerts else None, 5)
check("[1e] streak = 5", alerts[0].get("streak") if alerts else None, 5)

# [1f] warn 档连拒达阈值 → 升级 severe
eng_f, alerts_f, _tf = make_engine(_CountingLiveBroker(_st(verified=True)),
                                   _st(verified=True),
                                   tempfile.mkdtemp(prefix="tg_p58_f_"))
eng_f._pre_trade_check = lambda *a, **kw: "no_time_anchor"
lvls = []
for _ in range(eng_f._PRECHECK_ESCALATE_AT):
    eng_f._execute(_act_open(), ref_price=4500.0)
    lvls.append(alerts_f[0]["level"])
check("[1f] 阈值前保持 warn", lvls[:2], ["warn", "warn"])
check("[1f] 达阈值升级 severe", lvls[-1], "severe")
check("[1f] escalated 标记 True",
      alerts_f[0].get("escalated") if alerts_f else None, True)

# [1g] 换原因 → 计数重来
eng_g, alerts_g, _tg = make_engine(_CountingLiveBroker(_st(verified=True)),
                                   _st(verified=True),
                                   tempfile.mkdtemp(prefix="tg_p58_g_"))
streaks = []
for r in ["no_time_anchor", "no_time_anchor", "no_trading_day"]:
    eng_g._pre_trade_check = (lambda rr: (lambda *a, **kw: rr))(r)
    eng_g._execute(_act_open(), ref_price=4500.0)
    streaks.append(alerts_g[-1].get("streak"))
check("[1g] 换因后计数重来（第 3 条 streak=1）", streaks[-1], 1)

# [1h] 成功后归零
eng_h, _ah, _th = make_engine(_FillBroker(_st(verified=True)),
                              _st(verified=True),
                              tempfile.mkdtemp(prefix="tg_p58_h_"))
eng_h._reject_streak = 7
eng_h._reject_streak_code = "no_time_anchor"
eng_h.last_bar = _BAR
_o = eng_h._execute(_act_open(), ref_price=4500.0, bar=_BAR)
check("[1h] 报单成功（拿到 Order）", _o is not None, True)
check("[1h] 成功后 streak 归零", eng_h._reject_streak, 0)
check("[1h] 成功后 code 清空", eng_h._reject_streak_code, "")

# ════════════════════════════════════════════════════════════════
print("\n[4] 第 4 条：`.env.example` 补交易网关段")
_env_path = os.path.join(_REPO, ".env.example")
check_true("[4a] .env.example 存在", os.path.isfile(_env_path), _env_path)
_env = open(_env_path, encoding="utf-8").read() if os.path.isfile(_env_path) else ""
check_true("[4a] 含交易网关注释段标记",
           "自动下单 / 交易网关" in _env, "缺失交易网关段")
for _k in ("TRADING_BROKER=", "TRADING_BROKER_PARAMS__TQ_MARKET=",
           "TRADING_BROKER_PARAMS__CONFIRM_LIVE_TRADING=",
           "TRADING_BROKER_PARAMS__INSTRUMENT_FETCH_POLICY=",
           "TRADING_STATE_DIR=", "TRADING_SOURCE__FREQ="):
    check_true("[4b] 键名在样例中：{}".format(_k), _k in _env, _k)
check_true("[4c] 明示凭据走环境变量、不进配置",
           "绝不写进本文件" in _env or "只走环境变量" in _env, "")
check_true("[4d] 明示手数不在配置里（旧 max_volume 已删）",
           "max_volume" in _env and "已删除" in _env, "")
# [4e] 六个键实际可被 TradingConfig 解析（真跑一遍，不只查名字）
_probe = (
    "import os,sys\n"
    "sys.path.insert(0,{repo!r})\n"
    "from Trading.Config import TradingConfig\n"
    "c=TradingConfig()\n"
    "print(c.broker, c.broker_params.tq_market, c.broker_params.confirm_live_trading,\n"
    "      c.broker_params.instrument_fetch_policy, c.state_dir, c.source.freq)\n"
).format(repo=_REPO)
_envv = dict(os.environ)
_envv.update({
    "TRADING_BROKER": "simnow",
    "TRADING_BROKER_PARAMS__TQ_MARKET": "simnow",
    "TRADING_BROKER_PARAMS__CONFIRM_LIVE_TRADING": "false",
    "TRADING_BROKER_PARAMS__INSTRUMENT_FETCH_POLICY": "strict",
    "TRADING_STATE_DIR": "./State",
    "TRADING_SOURCE__FREQ": "5m",
    "PYTHONPATH": _REPO,
    "PYTHONIOENCODING": "utf-8",
})
_r = subprocess.run([sys.executable, "-c", _probe], env=_envv,
                    capture_output=True, cwd=_REPO)
_out = _r.stdout.decode("utf-8", "replace").strip()
check("[4e] 六个键实际解析结果",
      _out, "simnow simnow False strict ./State 5m")
check_true("[4e] 解析无异常（rc=0）", _r.returncode == 0,
           _r.stderr.decode("utf-8", "replace")[-200:])

# ════════════════════════════════════════════════════════════════
print("\n[5] 第 5 条：关闭自动下单的收尾确认信号")

# [5a] FLAT
_st5a = _st()
eng5a, alerts5a, t5a = make_engine(DryRunBroker(_st5a, state=_st5a), _st5a,
                                   tempfile.mkdtemp(prefix="tg_p58_5a_"))
check("[5a] 关闭前 FLAT", eng5a.account_state(), AccountState.FLAT)
eng5a.shutdown_and_lock_all()
check("[5a] 关闭后 FLAT", eng5a.account_state(), AccountState.FLAT)
check("[5a] FLAT → shutdown_result_flat",
      [a["code"] for a in alerts5a], ["shutdown_result_flat"])
check("[5a] 级别 warn", alerts5a[0]["level"] if alerts5a else None, "warn")
check_true("[5a] 文案含「已清仓」",
           "已清仓" in (alerts5a[0]["msg"] if alerts5a else ""), "")

# [5b] RUNNING（离场没做成）
_st5b = _st()
eng5b, alerts5b, t5b = make_engine(DryRunBroker(_st5b, state=_st5b), _st5b,
                                   tempfile.mkdtemp(prefix="tg_p58_5b_"))
eng5b.positions = PositionBook()
eng5b.positions.add(_mkpos(Side.LONG, 2, 4500.0, 1, "IF|buy|1"))
eng5b._run_ready = True
eng5b._sync_state()
check("[5b] 关闭前 RUNNING", eng5b.account_state(), AccountState.RUNNING)
eng5b._decide_exit = lambda bar: None        # 模拟"没有可用离场动作"
eng5b.shutdown_and_lock_all()
check("[5b] 关闭后仍 RUNNING（残留）",
      eng5b.account_state(), AccountState.RUNNING)
# 注：`_sync_state()` 会另发一条 run_missing_anchor（本条测试只造簿内仓单、
# 不造 run，故账户一旦进 RUNNING 就会触发它）—— 那是**另一条**既有告警，
# 不属于第 5 条范围。这里只断言收尾告警本身在、且是 severe。
_res5b = [(a["code"], a["level"]) for a in alerts5b
          if a["code"] == "shutdown_result_residual"]
check("[5b] RUNNING → shutdown_result_residual（severe）",
      _res5b, [("shutdown_result_residual", "severe")])
check_true("[5b] 文案含「仍有净敞口」",
           "仍有净敞口" in (
               [a for a in alerts5b
                if a["code"] == "shutdown_result_residual"][0]["msg"]
               if _res5b else ""), "")

# [5c] LOCKED
_st5c = _st()
eng5c, alerts5c, t5c = make_engine(DryRunBroker(_st5c, state=_st5c), _st5c,
                                   tempfile.mkdtemp(prefix="tg_p58_5c_"))
eng5c.positions = PositionBook()
eng5c.positions.add(_mkpos(Side.LONG, 2, 4500.0, 1, "IF|buy|1"))
eng5c.positions.add(_mkpos(Side.SHORT, 2, 4510.0, 2, "IF|sell|1"))
eng5c._run_ready = True
eng5c._sync_state()
check("[5c] 关闭前 LOCKED", eng5c.account_state(), AccountState.LOCKED)
eng5c._decide_exit = lambda bar: None
eng5c.shutdown_and_lock_all()
check("[5c] 关闭后 LOCKED", eng5c.account_state(), AccountState.LOCKED)
check("[5c] LOCKED → account_frozen（warn）",
      [(a["code"], a["level"]) for a in alerts5c],
      [("account_frozen", "warn")])
check("[5c] 净敞口 0", eng5c.positions.net_volume(), 0)

# [5d] _force_exit 返回委托
_sig = inspect.signature(TradingEngine._force_exit)
check_true("[5d] _force_exit 有返回值注解 Optional[Order]",
           "Order" in str(_sig.return_annotation), _sig.return_annotation)
check_true("[5d] shutdown 用到了它的返回值（源码断言）",
           "exit_order = self._force_exit(" in
           inspect.getsource(TradingEngine.shutdown_and_lock_all), "")

# [5e] 三态都写 shutdown_result 事件
for _tag, _t, _eng, _want in (("flat", t5a, eng5a, "flat"),
                              ("residual", t5b, eng5b, "residual"),
                              ("locked", t5c, eng5c, "locked")):
    _res = [e for e in _events(_t, _eng) if e.get("kind") == "shutdown_result"]
    check("[5e] {} 态写了 shutdown_result（result={}）".format(_tag, _want),
          [e.get("result") for e in _res], [_want])

# ════════════════════════════════════════════════════════════════
print("\n[6] 防回潮：fail-closed 底线未被第 1 条改动破坏")
check_true("[6a] _pre_trade_check 仍返回 instrument_unverified 拒单",
           "instrument_unverified" in
           inspect.getsource(TradingEngine._pre_trade_check), "")
check_true("[6b] 分级表中不含 instrument_unverified（不重发）",
           "instrument_unverified" not in
           TradingEngine._PRECHECK_ALERTS, "")
check_true("[6c] 已自行告警集合含闸门码",
           "instrument_unverified" in TradingEngine._PRECHECK_SELF_ALERTED, "")
check_true("[6d] alert 合并时级别只升不降（源码断言）",
           "alert_escalated" in inspect.getsource(TradingEngine.alert), "")

print("\n" + "=" * 60)
print("P58 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

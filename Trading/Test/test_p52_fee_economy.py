# -*- coding: utf-8 -*-
"""
P52 平今经济性判定（Phase 12 · D6 喂数）契约（2026-09-14）
===========================================================
背景（文档 Phase 12 行，2026-09-14 在原 Phase 11 后插入）
--------------------------------------------------------
Phase 10（D6）落地 `prefer_lock_over_closetoday` 品种开关时是**静态**配置：
"能不能平今"（= 交易所是否支持 CLOSETODAY）运行时能判定，但"贵不贵"（费率）
读不到 —— 只能靠品种档案人工配置开关。Phase 12 把费率自动获取补上：

  费率来源（三通道，按优先级判定）：
    · FEE_QUOTE  —— 纯模拟 `TqSim.get_commission`（SimNow._apply_fee_rates）
    · FEE_TRADE  —— 在线 CTP/SimNow 从成交回报 commission **反推**
                    （SimNow._sample_fee_from_fill，三档都攒到才回填）
    · FEE_CONFIG —— 离线 dry_run/replay 显式声明用配置默认值（mark_fee_config）
    · ""（空串） —— 未获取 → 经济性判定 **fail-closed**，平今开关走保守侧锁仓

  判定规则（evaluate_closetoday_economy，纯函数）：
    closetoday_fee_rate ≤ close_fee_rate → 平今免收/便宜
      → 建议 prefer_lock_over_closetoday=False（今仓直接平今，省开仓+跨日平仓费）
    closetoday_fee_rate >  close_fee_rate → 平今更贵 → 建议 True（锁仓优先）

  引擎侧（Engine._check_closetoday_economy）：
    · 只读**建议**，永不改写 prefer_lock_over_closetoday（决策留给人）；
    · 一次性检查（费率会话内不变），挂 A3 校验链（_pre_trade_check）；
    · fee_source 为空时不置位（费率可能经成交回报晚到，不能吞掉后续判定）。

覆盖
--------------------------------------------------------------------------
  [1] apply_fee_rates 原子回填：
        a. 合法三档 + FEE_QUOTE → 字段更新，fee_source=FEE_QUOTE，返回变更字段
        b. 非 SOURCE_FEE_* 来源 → ValueError（防拼错）
        c. 负费率 / nan → ValueError **且一档都不改**（原子性）
        d. 允许 0 费率（平今免收，如沪金 AU）
        e. 三档全相同 → 返回 []（无变化，仅确认来源）
  [2] mark_fee_config → fee_source=FEE_CONFIG（离线显式声明）
  [3] evaluate_closetoday_economy 判定：
        a. fee_source 空 → None（fail-closed，不判定）
        b. 平今更便宜（AU 0 vs 平昨 0.00002）→ suggest_lock=False / cheaper=closetoday
        c. 平今更贵（IF 0.000345 vs 平昨 0.000023）→ suggest_lock=True / cheaper=close
        d. 平今=平昨 → suggest_lock=False（≤ 规则归入平今便宜侧）/ cheaper=same
  [4] Engine._check_closetoday_economy：
        a. fee_source 空 → 无告警（fail-closed）
        b. 回填费率后触发 → alert code=closetoday_suggestion，event kind=closetoday_economy
        c. 二次调用不重复（一次性标志置位，同 code 只 n=1）
  [5] SimNow._sample_fee_from_fill（成交回报反推）：
        a. 三档（OPEN/CLOSE/CLOSETODAY）各成交一笔完整样本 → 原子回填 FEE_TRADE
        b. 只攒两档 → 不回填（fail-closed，等齐第三档）
        c. 已回填（fee_source 非空）→ 直接返回不再采样
        d. 单笔异常（无 trade_id 等）→ 静默跳过，不炸

跑法：python Trading/Test/test_p52_fee_economy.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace

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

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

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
    check(name + ("（%s" % str(detail) + "）" if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="tg_p52_%s_" % tag)
    try:
        yield d
    finally:
        pass


def build_engine(tmpdir, cfg, spec, tag="a"):
    return TradingEngine(
        cfg, DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        EntryPolicy({}), LayeredExitPolicy(),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                 echo=False, echo_kinds=None))


def make_cfg(signal_symbol: str = "KQ.m@SHFE.IF"):
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["instrument"]["signal_symbol"] = signal_symbol
    base["risk"]["max_volume"] = 2
    base["exit_params"].update({"use_atr": False, "min_r_points": 3.0,
                                "use_trailing": False})
    return TradingConfig.from_dict(base)


def _find_alert(eng, code):
    for a in eng._alerts:
        if a.get("code") == code:
            return a
    return None


# ══════════════════════════════════════════════════════════════
print("\n[1] apply_fee_rates 原子回填")
# ══════════════════════════════════════════════════════════════
# [1a] 合法三档 + FEE_QUOTE → 字段更新，fee_source 置位，返回变更字段
S = InstrumentSpec()  # 默认 IF：open=0.000023 close=0.000023 ct=0.000345
assert S.fee_source == ""
changed = S.apply_fee_rates(3e-5, 2e-5, 1e-5, S.SOURCE_FEE_QUOTE)
check("三档费率全部变更", set(changed), {"open_fee_rate", "close_fee_rate",
                                     "closetoday_fee_rate"})
check("fee_source → FEE_QUOTE", S.fee_source, "FEE_QUOTE")
check("closetoday 已更新为 1e-5", S.closetoday_fee_rate, 1e-5)

# [1e] 三档全相同 → 无变化，仅确认来源
changed2 = S.apply_fee_rates(3e-5, 2e-5, 1e-5, S.SOURCE_FEE_QUOTE)
check("三档无变化 → 返回 []", changed2, [])

# [1b] 非 SOURCE_FEE_* 来源 → ValueError
try:
    S.apply_fee_rates(1e-5, 1e-5, 1e-5, "FEE_BOGUS")
    check_true("[1b] 非法来源应抛 ValueError", False)
except ValueError:
    check_true("[1b] 非法来源抛 ValueError", True)

# [1c] 负费率 / nan → ValueError 且原子（一档都不改）
S2 = InstrumentSpec()
snapshot = (S2.open_fee_rate, S2.close_fee_rate, S2.closetoday_fee_rate, S2.fee_source)
for bad in (-1e-5, float("nan"), float("inf")):
    try:
        S2.apply_fee_rates(1e-5, bad, 1e-5, S2.SOURCE_FEE_CONFIG)
        check_true("[1c] 负/nan 费率应抛 ValueError", False)
    except ValueError:
        check_true("[1c] 非法费率 {!r} 抛 ValueError".format(bad), True)
    cur = (S2.open_fee_rate, S2.close_fee_rate, S2.closetoday_fee_rate, S2.fee_source)
    check("  原子性：三档+来源保持不变", cur, snapshot)

# [1d] 允许 0 费率（平今免收，沪金 AU）
S3 = InstrumentSpec()
S3.apply_fee_rates(2e-5, 2e-5, 0.0, S3.SOURCE_FEE_QUOTE)
check("  0 费率合法（平今免收）", S3.closetoday_fee_rate, 0.0)
check("  0 费率下 fee_source 仍置位", S3.fee_source, "FEE_QUOTE")

# ══════════════════════════════════════════════════════════════
print("\n[2] mark_fee_config（离线显式声明）")
# ══════════════════════════════════════════════════════════════
S4 = InstrumentSpec()
S4.mark_fee_config()
check("mark_fee_config → FEE_CONFIG", S4.fee_source, "FEE_CONFIG")

# ══════════════════════════════════════════════════════════════
print("\n[3] evaluate_closetoday_economy 判定")
# ══════════════════════════════════════════════════════════════
# [3a] fee_source 空 → None（fail-closed）
S5 = InstrumentSpec()          # fee_source = ""
check("fee_source 空 → None（fail-closed）", S5.evaluate_closetoday_economy(), None)

# [3b] 平今更便宜（AU：ct=0, close=2e-5）
S6 = InstrumentSpec()
S6.apply_fee_rates(2e-5, 2e-5, 0.0, S6.SOURCE_FEE_QUOTE)
r = S6.evaluate_closetoday_economy()
check("平今免收 → suggest_lock=False", r["suggest_lock"], False)
check("平今免收 → cheaper=closetoday", r["cheaper"], "closetoday")
check("平今免收 → source=FEE_QUOTE", r["source"], "FEE_QUOTE")

# [3c] 平今更贵（IF 默认：ct=0.000345, close=0.000023）
S7 = InstrumentSpec()
S7.apply_fee_rates(2.3e-5, 2.3e-5, 3.45e-4, S7.SOURCE_FEE_TRADE)
r = S7.evaluate_closetoday_economy()
check("平今更贵 → suggest_lock=True", r["suggest_lock"], True)
check("平今更贵 → cheaper=close", r["cheaper"], "close")
check("平今更贵 → source=FEE_TRADE", r["source"], "FEE_TRADE")

# [3d] 平今=平昨 → suggest_lock=False（≤ 规则）/ cheaper=same
S8 = InstrumentSpec()
S8.apply_fee_rates(2e-5, 2e-5, 2e-5, S8.SOURCE_FEE_QUOTE)
r = S8.evaluate_closetoday_economy()
check("平今=平昨 → suggest_lock=False", r["suggest_lock"], False)
check("平今=平昨 → cheaper=same", r["cheaper"], "same")

# ══════════════════════════════════════════════════════════════
print("\n[4] Engine._check_closetoday_economy")
# ══════════════════════════════════════════════════════════════
# [4a] fee_source 空 → 无告警、无事件（fail-closed）
with tmp_dir("eng_empty") as td:
    spec0 = InstrumentSpec()
    eng0 = build_engine(td, make_cfg(), spec0, "empty")
    eng0._check_closetoday_economy()
    check("fee_source 空 → 无 closetoday_suggestion 告警",
          _find_alert(eng0, "closetoday_suggestion"), None)
    check("fee_source 空 → 判定标志未置位（不吞后续）",
          eng0._closetoday_economy_checked, False)

# [4b] 回填费率后触发 → alert + event
#   ⚠️ 引擎的费率来自 self.spec = cfg.instrument（不是 build_engine 传给 broker
#   的独立 spec），故直接对 engB.spec 回填费率再触发。
with tmp_dir("eng_fire") as td:
    engB = build_engine(td, make_cfg(), InstrumentSpec(), "fire")
    engB.spec.apply_fee_rates(2e-5, 2.3e-5, 1e-5, engB.spec.SOURCE_FEE_QUOTE)  # 平今更便宜
    engB._check_closetoday_economy()
    a = _find_alert(engB, "closetoday_suggestion")
    check_true("[4b] 触发 closetoday_suggestion 告警", a is not None)
    if a:
        check("[4b] 告警建议值 suggest_lock=False", a.get("suggest_lock"), False)
        check("[4b] 告警来源 source=FEE_QUOTE", a.get("source"), "FEE_QUOTE")
    ev = None
    # EventLog 满 1s / 64 条会自动 flush → 事件可能已落文件、_buf 已清空。
    # 直接从事件文件读，最稳。
    engB.ev.flush()
    _evpath = os.path.join(td, "events_fire.jsonl")
    if os.path.exists(_evpath):
        with open(_evpath, encoding="utf-8") as _f:
            for _line in _f:
                if "closetoday_economy" in _line:
                    ev = _line
    check_true("[4b] 写入 closetoday_economy 事件", ev is not None)
    check("[4b] 判定标志已置位", engB._closetoday_economy_checked, True)

# [4c] 二次调用不重复（同 code 只 n=1）
    engB._check_closetoday_economy()
    a2 = _find_alert(engB, "closetoday_suggestion")
    if a2 is not None:
        check("[4c] 二次调用告警仍为 n=1（一次性）", a2.get("n"), 1)
    else:
        check("[4c] 二次调用后告警仍存在", a2 is not None, True)

# ══════════════════════════════════════════════════════════════
print("\n[5] SimNow._sample_fee_from_fill（成交回报反推）")
# ══════════════════════════════════════════════════════════════
from Trading.Broker.SimNow import SimNowBroker  # noqa: E402


def _mk_fake_api(prices, comms):
    """fake api.get_trade(tid) → SimpleNamespace(commission=...)"""
    trades = {tid: SimpleNamespace(commission=c)
              for tid, c in comms.items()}

    def get_trade(tid):
        return trades.get(tid)

    # 记录已成交的 trade_id，供采样校验
    return SimpleNamespace(get_trade=get_trade, _seen=set()), trades


def _fill_order(tids):
    """构造含 trade_records 的 order（dict 或 list 两种形态都覆盖）。"""
    return SimpleNamespace(
        trade_records={tid: {"trade_id": tid, "price": 4000.0, "volume": 1, }
                       for tid in tids},
        volume=1)


# [5a] 三档各成交一笔 → 原子回填 FEE_TRADE
#   ⚠️ SimNowBroker.__init__ 会 _connect() 连 CTP（需 tqsdk + 网络）。这里用
#   __new__ 绕过构造，只喂 `_sample_fee_from_fill` 用到的 spec/_api/_fee_samples。
def _mk_simnow(spec, api):
    b = SimNowBroker.__new__(SimNowBroker)
    b.spec = spec
    b._api = api
    b._fee_samples = {}
    return b


_spec = InstrumentSpec(trade_symbol="SHFE.AU2602", multiplier=1000.0)
_fake_api, _trades = _mk_fake_api(
    ["t1", "t2", "t3"], {"t1": 6.0, "t2": 4.0, "t3": 2.0})
_broker = _mk_simnow(_spec, _fake_api)
# 价×手数×乘数 = 4000×1000 = 4e6；费率 = comm/4e6
_broker._sample_fee_from_fill(_fill_order(["t1"]), "open", "open")           # OPEN
_broker._sample_fee_from_fill(_fill_order(["t2"]), "close", "open")          # CLOSE (跨日平仓)
_broker._sample_fee_from_fill(_fill_order(["t3"]), "close", "closetoday")    # CLOSETODAY
check("[5a] 三档齐 → fee_source=FEE_TRADE", _spec.fee_source, "FEE_TRADE")
check("[5a] OPEN 费率回填 6/4e6", _spec.open_fee_rate, 6.0 / 4_000_000.0)
check("[5a] CLOSE 费率回填 4/4e6", _spec.close_fee_rate, 4.0 / 4_000_000.0)
check("[5a] CLOSETODAY 费率回填 2/4e6", _spec.closetoday_fee_rate, 2.0 / 4_000_000.0)
check("[5a] 样本缓存已清空", _broker._fee_samples, {})

# [5c] 已回填 → 直接返回不再采样样本不增长
_broker._sample_fee_from_fill(_fill_order(["t9"]), "open", "open")
check("[5c] 已回填后不再采样（fee_source 已定）", _spec.fee_source, "FEE_TRADE")

# [5b] 只攒两档 → 不回填（fail-closed，等齐第三档）
_spec2 = InstrumentSpec(trade_symbol="SHFE.AU2602", multiplier=1000.0)
_fake2, _ = _mk_fake_api([], {})
_broker2 = _mk_simnow(_spec2, _fake2)
_broker2._sample_fee_from_fill(_fill_order(["a1"]), "open", "open")         # OPEN 只有一档
_broker2._sample_fee_from_fill(_fill_order(["a2"]), "close", "open")        # CLOSE 两档
check("[5b] 只攒两档 → fee_source 仍为空（fail-closed）", _spec2.fee_source, "")
check("[5b] 只攒两档 → 样本数=2", len(_broker2._fee_samples), 2)

# [5d] 单笔异常（无 trade_id）→ 静默跳过，不炸
_spec3 = InstrumentSpec(trade_symbol="SHFE.AU2602", multiplier=1000.0)
_broker3 = _mk_simnow(
    _spec3, SimpleNamespace(get_trade=lambda tid: SimpleNamespace(commission=3.0)))
# 一条记录 trade_id 为空、一条正常 → 空记录跳过，正常记录计入
try:
    _broker3._sample_fee_from_fill(
        SimpleNamespace(trade_records={"bad": {"trade_id": None,
                                               "price": 4000.0, "volume": 1}},
                        volume=1), "open", "open")
    check_true("[5d] 无 trade_id 记录静默跳过即通过", True)
except Exception:
    check_true("[5d] 无 trade_id 记录不应抛异常", False)

# ══════════════════════════════════════════════════════════════
print("\n===== P52 结果: {} 通过 / {} 失败 =====".format(_PASS, _FAIL))
if _FAIL:
    sys.exit(1)
# -*- coding: utf-8 -*-
"""
P39 D11 告警队列 / 确认（契约测试，2026-09-11）
================================================
背景（文档 Phase 6 P6-B / §10）
---------------------------------
后端把严重告警写进 `auto_order_status()` 的 `alerts` 字段，前端在已有轮询回调里
检测新告警 → `alert()` 弹窗。队列走 state.db 已有的 kv 通道（D18 不新增 IPC），
落盘目的就是「重启后还在」。

本测试钉死 D11 队列的四条不变量：
  [1] 登记：alert() 入队，auto_order_status 立即可见；
  [2] 合并：同 code 未确认 → 合并计数（n+1），不新增条目（防通道故障淹没队列）；
  [3] 确认：ack_alerts(codes=) 按 code 移除；ack_alerts(ts=) 按水位线移除；
  [4] 落盘：确认后 kv 被清 / 水位线写出，_load_alerts 重启恢复与水位一致。

覆盖
  [1]~[4] 如上；另验 severe 级告警确实进 status.alerts（前端弹窗依据）

跑法：python Trading/Test/test_p39_alert_queue_ack.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import time
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

from Trading import Broker  # noqa: E402,F401
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
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
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="tg_p39_%s_" % tag)
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def make_cfg():
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"]["max_volume"] = 2
    return TradingConfig.from_dict(base)


def build_engine(tmpdir):
    spec = InstrumentSpec()
    return TradingEngine(
        make_cfg(), DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        DefaultEntryPolicy({}),
        LayeredExitPolicy(make_cfg().exit_params.model_dump()),
        Store(os.path.join(tmpdir, "state.db")),
        EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False,
                 echo_kinds=None))


print("\n[1] 登记：alert() 入队，auto_order_status 立即可见")
with tmp_dir("q") as tmp:
    eng = build_engine(tmp)
    rec = eng.alert("warn", "ctp_reject_funds", "资金不足，请加保证金")
    check("[1a] 返回记录含 code", rec.get("code"), "ctp_reject_funds")
    check("[1b] 队内条目数 = 1", len(eng._alerts), 1)
    st = eng.auto_order_status()
    check("[1c] status.alerts 含该告警", len(st["alerts"]), 1)
    check("[1d] 告警 code 透传到前端", st["alerts"][0]["code"], "ctp_reject_funds")


    print("\n[2] 合并：同 code 未确认 → n+1，不新增条目")
    eng.alert("warn", "ctp_reject_funds", "资金不足，请加保证金")
    check("[2a] 队内仍是 1 条（合并不追加）", len(eng._alerts), 1)
    check("[2b] 合并计数 n = 2", eng._alerts[0].get("n"), 2)


    print("\n[3] 确认（按 code）：ack_alerts(codes=) 移除指定 code")
    eng.alert("severe", "close_chase_exhausted", "追价跑满仍未成交")
    check("[3a] 新增 severe 后队内 = 2", len(eng._alerts), 2)
    removed = eng.ack_alerts(codes=["ctp_reject_funds"])
    check("[3b] 按 code 确认移除 1 条", removed, 1)
    check("[3c] 剩余 1 条（close_chase_exhausted）", len(eng._alerts), 1)
    check("[3d] status.alerts 同步减少", len(eng.auto_order_status()["alerts"]), 1)


    print("\n[4] 确认（按水位线 ts）：ack_alerts(ts=) 移除该时刻前的全部")
    t0 = time.time()
    eng.alert("warn", "ctp_reject_not_tradable", "非交易时段")
    time.sleep(0.01)
    t1 = time.time()
    eng.alert("warn", "ctp_reject_price", "价格不可达")
    eng.ack_alerts(ts=t1)  # 确认 t1（含）之前 → 清掉前两条，保留 t1 之后那条
    codes_left = {a["code"] for a in eng._alerts}
    check("[4a] 水位线确认后仅剩 t1 之后的告警", codes_left,
          {"ctp_reject_price"})


print("\n[5] 落盘与重启恢复：确认后 kv 被清 / 水位写出，_load_alerts 一致")
with tmp_dir("persist") as tmp:
    eng = build_engine(tmp)
    eng.alert("severe", "close_repeatedly_rejected", "平仓连续被拒，已清幻影仓")
    eng.ack_alerts(ts=time.time() + 1.0)  # 确认全部
    check("[5a] 确认后内存队列清空", len(eng._alerts), 0)
    check("[5b] 落盘后 alerts kv 被删除（空队列不写）",
          eng.store.get_json(eng._ALERTS_KV), None)
    eng._load_alerts()  # 模拟重启恢复
    check("[5c] 重启恢复后队列仍为空（水位线已排除）", len(eng._alerts), 0)


print("\n" + "=" * 60)
print("P39 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

# -*- coding: utf-8 -*-
"""
P36 持久化无遗留键（state.db 干净 · 契约测试，2026-09-11）
============================================================
背景（文档 G1 旧 schema 闸门 / 退役 ledger）
-------------------------------------------------------
一期把持仓模型从「按笔记录的多仓 + PositionOrigin/ExitMode/lock_pair_id」收敛为
「净敞口 + 单意图」。旧的 schema 残留一律视为非法：
  · 持仓记录字段：entry_mode / origin / lock_pair_id / exit_mode
  · kv 残留键：lock_pair_seq / unlock_in_flight
  · 配置键：max_open_positions / unlock_no_new_open（属配置层，由 RiskConfig
    丢弃，本测试只盯 state.db 落盘内容）

G1 闸门负责「老库启动即拒绝」（p29 已覆盖）。本测试补另一半：**正常跑一轮
后，state.db 里不得出现任何遗留键**——防止新代码不经意把旧字段又写回去
（这正是 p26 护栏在注释层防的事，本测试在落盘层再钉一道）。

覆盖
  [1] 跑一个完整开→平周期（跨日），让 positions / orders / trades 都落盘
  [2] 递归扫描 kv 全部键值 + orders.meta + trades.exit_plan_params，
      断言不含任何遗留键名

跑法：python Trading/Test/test_p36_state_db_no_legacy.py
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
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

from Trading import Broker  # noqa: E402,F401
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import Bar, Side, Signal  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0
D1 = "2026-09-02"
D2 = "2026-09-03"
P0 = 4520.0
P_EXIT = 4110.0

# 遗留键名（字段名 / kv 键），正常落盘不得出现任何一个
_LEGACY = {"entry_mode", "origin", "lock_pair_id", "exit_mode",
           "lock_pair_seq", "unlock_in_flight"}


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def _scan_obj(obj, path, hits):
    """递归扫描：遇到 dict 时检查其键是否为遗留名。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _LEGACY:
                hits.append((path + "." + str(k), k))
            _scan_obj(v, path + "." + str(k), hits)
    elif isinstance(obj, (list, tuple, set)):
        for i, v in enumerate(obj):
            _scan_obj(v, "{}[{}]".format(path, i), hits)
    # 基础类型无需继续


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="tg_p36_%s_" % tag)
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def make_cfg():
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"]["max_volume"] = 2
    base["exit_params"].update({"use_atr": False, "min_r_points": 3.0,
                                 "use_trailing": False})
    return TradingConfig.from_dict(base)


def make_bar(ts, date, o, h, l, c):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_sig(key, date, ts, price, is_buy):
    return Signal(key=key, symbol="KQ.m@CFFEX.IF", freq="5m", date=date,
                  timestamp=ts, bsp_type="1" if is_buy else "2", is_buy=is_buy,
                  price=price, high=price + 10.0, low=price - 10.0,
                  fractal_low=price - 12.0, fractal_high=price + 12.0)


print("\n[1] 跑完整开→平周期，让 positions / orders / trades 全部落盘")
with tmp_dir("cycle") as tmp:
    db_path = os.path.join(tmp, "state.db")
    spec = InstrumentSpec()
    eng = TradingEngine(
        make_cfg(), DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        DefaultEntryPolicy({}),
        LayeredExitPolicy(make_cfg().exit_params.model_dump()),
        Store(db_path),
        EventLog(os.path.join(tmp, "events.jsonl"), echo=False,
                 echo_kinds=None))
    eng.on_bar(make_bar(1000, D1 + " 09:40", P0, P0 + 10, P0 - 10, P0))
    eng.on_signal(make_sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    check("[1a] 开仓后 RUNNING", eng.account_state().name, "RUNNING")
    eng.on_bar(make_bar(3000, D2 + " 14:55", P_EXIT, P_EXIT + 10, 4000.0, P_EXIT))
    check("[1b] 跨日离场后 FLAT", eng.account_state().name, "FLAT")
    check("[1c] 至少 1 笔成交已落盘", len(eng.store.trades()) >= 1, True)

    print("\n[2] 递归扫描 state.db 全部落盘内容，断言无遗留键")
    hits = []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # kv 表：键本身 + 值（json）
    for row in conn.execute("SELECT k, v FROM kv"):
        if row["k"] in _LEGACY:
            hits.append(("kv.k=" + row["k"], row["k"]))
        try:
            val = json.loads(row["v"])
        except Exception:
            val = None
        if val is not None:
            _scan_obj(val, "kv.{}".format(row["k"]), hits)
    # orders 表：meta 列（json）
    for row in conn.execute("SELECT order_id, meta FROM orders"):
        try:
            val = json.loads(row["meta"]) if row["meta"] else None
        except Exception:
            val = None
        if val is not None:
            _scan_obj(val, "orders.{}".format(row["order_id"]), hits)
    # trades 表：exit_plan_params 列（json）
    for row in conn.execute("SELECT trade_id, exit_plan_params FROM trades"):
        try:
            val = json.loads(row["exit_plan_params"]) if row["exit_plan_params"] else None
        except Exception:
            val = None
        if val is not None:
            _scan_obj(val, "trades.{}".format(row["trade_id"]), hits)
    conn.close()

    check("[2a] kv / orders / trades 中**无任何遗留键**", hits, [])
    if hits:
        for where, key in hits:
            print("      !! 命中遗留键 {} @ {}".format(key, where))


print("\n" + "=" * 60)
print("P36 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

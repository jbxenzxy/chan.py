# -*- coding: utf-8 -*-
"""
持久化（sqlite）
================
为什么要落盘而不是放内存：切换周期 / 重启进程 / 断线重连后，
"这个信号是否已经处理过" 和 "我现在有没有持仓" 必须仍然是正确答案。
因此：
  - processed_signals.signal_key 用 PRIMARY KEY 做**数据库级幂等**
    （不用先 SELECT 再 INSERT，那是典型的 TOCTOU 竞态）
  - 持仓与当日统计放 kv 表，进程重启后可恢复
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .types import Order, Trade, now_cn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_signals (
    signal_key TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    action     TEXT,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    order_id   TEXT PRIMARY KEY,
    signal_key TEXT,
    ts         TEXT,
    symbol     TEXT,
    side       TEXT,
    action     TEXT,
    volume     INTEGER,
    price      REAL,
    filled     REAL,
    status     TEXT,
    broker     TEXT,
    note       TEXT,
    meta       TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id   TEXT PRIMARY KEY,
    signal_key TEXT,
    symbol     TEXT,
    side       TEXT,
    volume     INTEGER,
    entry_price REAL, exit_price REAL,
    entry_at   TEXT, exit_at TEXT,
    reason     TEXT,
    gross_points REAL, cost_points REAL,
    net_points REAL, net_cash REAL,
    bars_held  INTEGER,
    exit_plan_name TEXT,
    exit_plan_params TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_at);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # 默认 journal_mode=delete 下每次 commit 都要 fsync，Windows 上实测
        # 单笔往返要 250ms（26 次提交 ≈ 6.5s）。改 WAL + NORMAL 后降到毫秒级。
        # 持久性说明：权威记录是 events.jsonl（append-only，每秒 flush），
        # sqlite 只存"可重建的派生状态"（持仓、幂等键、统计）。
        # 极端断电最多丢最后 1 秒的 sqlite 写入，可用 events.jsonl 重放恢复。
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---------- 信号幂等 ----------
    def try_mark_signal(self, key: str, action: str, note: str = "") -> bool:
        """原子占位。返回 True = 首次处理；False = 已处理过（调用方应直接跳过）。"""
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO processed_signals (signal_key, first_seen, action, note)"
                    " VALUES (?,?,?,?)", (key, now_cn(), action, note))
            return True
        except sqlite3.IntegrityError:
            return False

    def signal_action(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT action FROM processed_signals WHERE signal_key=?",
                                (key,)).fetchone()
        return row["action"] if row else None

    def update_signal_action(self, key: str, action: str, note: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE processed_signals SET action=?, note=? WHERE signal_key=?",
                (action, note, key))

    # ---------- 委托 ----------
    def save_order(self, o: Order) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o.order_id, o.signal_key, o.created_at or now_cn(), o.symbol,
                 o.side.name, o.action, o.volume, o.price, o.filled_price,
                 o.status, o.broker, o.note,
                 json.dumps(o.meta, ensure_ascii=False)))

    # ---------- 成交 ----------
    def save_trade(self, t: Trade) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.trade_id, t.signal_key, t.symbol, t.side.name, t.volume,
                 t.entry_price, t.exit_price, t.entry_at, t.exit_at, t.reason,
                 t.gross_points, t.cost_points, t.net_points, t.net_cash,
                 t.bars_held, t.exit_plan_name,
                 json.dumps(t.exit_plan_params, ensure_ascii=False)))

    def trades(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM trades ORDER BY exit_at").fetchall()
        return [dict(r) for r in rows]

    # ---------- 状态重置（回放重跑用） ----------
    def wipe_runtime_state(self) -> Dict[str, int]:
        """清空"可重建的派生状态"，让回放可以干净重跑。

        清三样（保留 orders 表作为审计底稿，不动）：
          - processed_signals：信号幂等键。不清的话，上一轮回放标记过的 signal_key
            会被 `try_mark_signal` 判为重复 → 全部 signal_dup → 本轮 0 笔成交。
            这正是 v6 跑出 trades=0 的直接原因。
          - trades：成交记录。
          - kv 里的 position / day_stats / bars_seen。

        返回各表被删的行数，便于打印确认。
        """
        counts = {}
        with self.conn:
            for tbl in ("processed_signals", "trades"):
                row = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM {}".format(tbl)).fetchone()
                counts[tbl] = int(row["n"]) if row else 0
                self.conn.execute("DELETE FROM {}".format(tbl))
            for k in ("position", "day_stats", "bars_seen"):
                self.conn.execute("DELETE FROM kv WHERE k=?", (k,))
        return counts

    # ---------- kv（持仓 / 当日统计） ----------
    def set_json(self, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",
                              (key, json.dumps(value, ensure_ascii=False, default=str)))

    def get_json(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except Exception:
            return default

    def delete_key(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM kv WHERE k=?", (key,))

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

# -*- coding: utf-8 -*-
"""
持久化（sqlite）
================
为什么要落盘而不是放内存：切换周期 / 重启进程 / 断线重连后，
"这个信号是否已经处理过" 和 "我现在有没有持仓" 必须仍然是正确答案。
因此：
  - processed_signals.signal_key 用 PRIMARY KEY 做**数据库级幂等**
    （不用先 SELECT 再 INSERT，那是典型的 TOCTOU 竞态）
  - 持仓状态放 kv 表，进程重启后可恢复
    （kv 里另存的 day_stats / bars_seen 为**历史键**，仅清理旧库时删，不再写入）
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .Types import Order, Trade, now_cn

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


class IdCollisionError(RuntimeError):
    """持久 ID 撞号（PRIMARY KEY 冲突）。

    R2（2026-09-10）：trades / orders 是**审计底稿**，一条记录被覆盖 =
    历史成交/报单凭空消失。撞号说明 ID 生成端出了问题（序号没跨重启恢复 /
    state.db 被外部改过 / 多进程共用一份库），此时宁可让进程死在写入点，
    也不留一份自相矛盾的账。故这两张表的写入一律 fail-fast。
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
        """写委托审计记录。**fail-fast**：order_id 撞号即抛 `IdCollisionError`。

        R2（2026-09-10）：原实现是 `INSERT OR REPLACE` —— 撞号时把**上一进程**的
        同号记录静默覆盖。审计底稿从"N 条"变成"1 条"，无任何告警、无任何痕迹。
        order_id 的唯一性由 R1 保证（broker 序号在 `_restore` 时从库内自愈抬升），
        故此处的冲突只可能来自"库被外部改过 / 多实例共用一个库"这类真异常 ——
        对这种异常，响亮地死比留一份自相矛盾的账要好。
        """
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (o.order_id, o.signal_key, o.created_at or now_cn(), o.symbol,
                     o.side.name, o.action, o.volume, o.price, o.filled_price,
                     o.status, o.broker, o.note,
                     json.dumps(o.meta, ensure_ascii=False)))
        except sqlite3.IntegrityError as e:
            raise IdCollisionError(
                "orders 表主键冲突：order_id={!r} 已存在（{}）。\n"
                "  含义：本次委托号与库内一条历史委托号相同 —— 若沿用旧的\n"
                "        INSERT OR REPLACE，这条历史审计记录会被**静默覆盖**。\n"
                "  原因：报单序号没跨重启恢复（R1）／state.db 被外部改过／\n"
                "        同一份 state.db 被多个进程同时写。\n"
                "  处理：先备份 Trading/State/state.db，再核对 orders 表的\n"
                "        order_id 与事件的 order_id 是否对得上。"
                .format(o.order_id, e))

    # ---------- 成交 ----------
    def save_trade(self, t: Trade) -> None:
        """写成交流水。**fail-fast**：trade_id 撞号即抛 `IdCollisionError`。

        R2（2026-09-10）：理由同 `save_order` —— trades 是成交审计底稿，
        被覆盖等于历史成交凭空消失。trade_id 的唯一性由 R1 保证
        （`_trade_seq` 落 kv + 恢复时与库内 max 取大）。
        """
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t.trade_id, t.signal_key, t.symbol, t.side.name, t.volume,
                     t.entry_price, t.exit_price, t.entry_at, t.exit_at, t.reason,
                     t.gross_points, t.cost_points, t.net_points, t.net_cash,
                     t.bars_held, t.exit_plan_name,
                     json.dumps(t.exit_plan_params, ensure_ascii=False)))
        except sqlite3.IntegrityError as e:
            raise IdCollisionError(
                "trades 表主键冲突：trade_id={!r} 已存在（{}）。\n"
                "  含义：本次成交号与库内一条历史成交号相同 —— 若沿用旧的\n"
                "        INSERT OR REPLACE，这条历史成交流水会被**静默覆盖**。\n"
                "  原因：成交序号没跨重启恢复（R1）／state.db 被外部改过／\n"
                "        同一份 state.db 被多个进程同时写。\n"
                "  处理：先备份 Trading/State/state.db，再核对 trades 表与\n"
                "        events.jsonl 里 close/unlock 事件的 trade_id。"
                .format(t.trade_id, e))

    def trades(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM trades ORDER BY exit_at").fetchall()
        return [dict(r) for r in rows]

    # ---------- 序号自愈（R1 的数据侧：库内 max） ----------
    def max_trade_seq(self) -> int:
        """库内 `trades` 的最大成交序号（'T00007' → 7）。空表 / 全不成格式 → 0。

        用途：R1 跨重启自愈的第二道保险 —— 即使 kv 里的 `trade_seq` 丢了
        （库被外部清理 / 换过库文件），也能从**真实落库的数据**反推出
        已用过的最大号，保证新号绝不与库内既有号相撞。
        """
        return self._max_int_suffix("trades", "trade_id", "T")

    def max_order_seq(self, broker: str) -> int:
        """库内**指定 broker** 的最大报单序号（'dry_run-000007' → 7）。

        用途：order_id 由 broker 端计数器生成（`{broker}-{seq:06d}`），
        进程重启后 broker 计数器归零 → 与库内既有号相撞。`_restore` 用本方法
        把 broker 序号抬到库内 max 之上，从数据侧消除复用。
        """
        return self._max_int_suffix("orders", "order_id", "{}-".format(broker))

    def _max_int_suffix(self, table: str, column: str, prefix: str = "") -> int:
        """扫 table.column，取 "prefix + 纯数字" 形式的最大数字后缀。

        带前缀时不匹配的行直接跳过（不把裸数字或别的格式混进来）——
        宁可少算，也不要把无关记录的数字当成序号。
        """
        best = 0
        for row in self.conn.execute(
                "SELECT {} AS v FROM {}".format(column, table)):
            s = str(row["v"] or "")
            if prefix:
                if not s.startswith(prefix):
                    continue
                tail = s[len(prefix):]
            else:
                tail = s
            if not tail.isdigit():
                continue
            n = int(tail)
            if n > best:
                best = n
        return best

    # ---------- 状态重置（回放重跑用） ----------
    def wipe_runtime_state(self) -> Dict[str, int]:
        """清空"可重建的派生状态"，让回放可以干净重跑。

        清三样（保留 orders 表作为审计底稿，不动）：
          - processed_signals：信号幂等键。不清的话，上一轮回放标记过的 signal_key
            会被 `try_mark_signal` 判为重复 → 全部 signal_dup → 本轮 0 笔成交。
            这正是 v6 跑出 trades=0 的直接原因。
          - trades：成交记录。
          - kv 里的 position / positions / day_stats / bars_seen。
            （position 为单仓兼容壳（审计用）；**positions 才是多仓主键**，
            不清它 = 上一轮持仓会随 restore 回来；day_stats / bars_seen 为历史键，
            一并清掉防旧库残留。）

        2026-09-10（R1 配套）：一并清掉 `trade_seq` ——
        它对应的数据（trades / positions）刚刚被清空，序号理应回到 1，
        让重跑的 trade_id **逐轮一致**（回放可比对性）。
        2026-09-11：`run` 也一并清 —— 它是运行态的风控锚，随持仓一起归零。
        **刻意不清 `order_seq`**：orders 表保留作审计底稿，序号必须只增不减，
        否则重跑会与保留下来的历史委托号相撞（R2 之后会直接抛 IdCollisionError）。

        返回各表被删的行数，便于打印确认。
        """
        counts = {}
        with self.conn:
            for tbl in ("processed_signals", "trades"):
                row = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM {}".format(tbl)).fetchone()
                counts[tbl] = int(row["n"]) if row else 0
                self.conn.execute("DELETE FROM {}".format(tbl))
            # 2026-09-10 修正：补清 `positions`（复数）。此前只清 `position`（单数，
            # 仅审计用），而**复数键才是多仓主键** —— `_persist` 写它、`_restore`
            # 优先读它。漏清的后果：回放 `--fresh`（main.py:137）后簿内仍留着上一轮
            # 的持仓（实测整对锁仓 lock_00001 残留）→ account_state() 判 LOCKED，
            # 而 trades 已归零 → 账实不一致，"干净重跑"并不干净。
            # 用 `positions` 键与 `--fresh` 的语义对齐：清派生状态 = 等价于删库重启
            # （区别仅在 orders 表作为审计底稿保留，故 order_seq 仍刻意不清）。
            for k in ("position", "positions", "day_stats", "bars_seen",
                      "trade_seq", "run"):
                self.conn.execute("DELETE FROM kv WHERE k=?", (k,))
        return counts

    # ---------- kv（持仓状态） ----------
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

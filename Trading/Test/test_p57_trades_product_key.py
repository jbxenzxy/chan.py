# -*- coding: utf-8 -*-
"""
P57 `trades.product_key` 落库契约
============================================================
背景：品种键归一从**查询侧**搬到**写入侧**
--------------------------------------------
旧口径（2026-09-16 前）：`trades` 只存 `symbol`（月份合约 `CFFEX.IF2609`），
"同品种多合约月份合并统计"靠查询时读回全表后**逐行现算**品种键。于是同一件事
有两个口径来源：**写库那一刻的 symbol** 与**查库时现算的键**。

新口径：品种键在**落库时**算好写进 `trades.product_key`，查询侧只做
`WHERE product_key = ?`（**列相等**），不再解读 `symbol`。

三件事（用户拍板）
--------------------------
  1. `trades` 加列 `product_key`；
  2. 老库回填（`Store.__init__` 迁移区，**幂等**）；
  3. 查询侧等值（`TradeStats.load_trades_report`）。

用户明确：**不会遇到未迁移的老库**，故查询侧不写"缺列兼容"分支 ——
缺列就是 `query_failed`，响亮地失败，不悄悄换一套口径。

覆盖
--------------------------------------------------------------------------
  [0] 元护栏：断言手法的自检（朴素 `in` 会被**自己写的注释**蒙过）
  [1] 写入侧：`save_trade` 落 product_key（新库）
  [2] 老库回填：加列 / 逐行正确 / 不动其他列 / 幂等 / 中断自愈 / 旧 schema 迁移
  [3] 查询侧：列相等（合并 / 不命中 / 解析不出 / 全库）
  [4] 归一规则的唯一住址（`Product.product_key_of`）
  [5] 结构与源码级防回潮（显式列名 / 列在表尾 / 写入侧算键）

跑法：python Trading/Test/test_p57_trades_product_key.py
"""
from __future__ import annotations

import io
import os
import shutil
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
_REPO = os.path.dirname(_TG_ROOT)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import Trading.Infra.Product as _P                    # noqa: E402
import Trading.Infra.TradeStats as _TS                # noqa: E402
from Trading.Infra.Product import (parse_product_key,  # noqa: E402
                                   product_key_of)
from Trading.Infra.Records import Side, Trade         # noqa: E402
from Trading.Infra.StateDB import Store               # noqa: E402
from Trading.Infra.TradeStats import (load_trades_report,  # noqa: E402
                                      product_key_of as _via_stats)

_PASS = 0
_FAIL = 0


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
    d = tempfile.mkdtemp(prefix="tg_p57_%s_" % tag)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def mk_trade(tid, symbol, exit_at, net=10.0, volume=1):
    return Trade(
        trade_id=tid, symbol=symbol, side=Side.LONG, volume=volume,
        entry_price=4500.0, exit_price=4500.0 + net,
        entry_at="2026-09-01 09:00", exit_at=exit_at, reason="trailing",
        gross_points=net, cost_cash=0.0, net_cash=net, bars_held=3,
        signal_key="p57|" + tid, exit_plan_name="x")


def cols_of(conn, table="trades"):
    return [r[1] for r in conn.execute("PRAGMA table_info({})".format(table))]


def rows_of(conn, sql="SELECT * FROM trades ORDER BY trade_id"):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql)]


def read(rel):
    with io.open(os.path.join(_REPO, rel), encoding="utf-8") as f:
        return f.read()


def code_only(src):
    """剥掉每行 `#` 之后的内容 —— 注释里的留档举例不算"还在用"。"""
    return "\n".join(line.split("#")[0] for line in src.splitlines())


# 旧 schema（本次加列之前的那一版，16 列、无 product_key）—— 用于造"老库"
_LEGACY_SCHEMA = """
CREATE TABLE trades (
    trade_id   TEXT PRIMARY KEY,
    signal_key TEXT,
    symbol     TEXT,
    side       TEXT,
    volume     INTEGER,
    entry_price REAL, exit_price REAL,
    entry_at   TEXT, exit_at TEXT,
    reason     TEXT,
    gross_points REAL, cost_cash REAL,
    net_cash REAL,
    bars_held  INTEGER,
    exit_plan_name TEXT,
    exit_plan_params TEXT
);
"""


def build_legacy_db(path, rows):
    """用**原生 sqlite3** 造一个"加列之前"的库（不走 Store，避免被自动迁移）。"""
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    for t in rows:
        conn.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.trade_id, t.signal_key, t.symbol, t.side.name, t.volume,
             t.entry_price, t.exit_price, t.entry_at, t.exit_at, t.reason,
             t.gross_points, t.cost_cash, t.net_cash, t.bars_held,
             t.exit_plan_name, "{}"))
    conn.commit()
    conn.close()
    return path


# ══════════════════════════════════════════════════════════════════
print("\n[0] 元护栏：断言手法的自检（朴素 in 会被自己的注释蒙过）")
# ══════════════════════════════════════════════════════════════════
_SAMPLE = (
    '                # ① 显式列名取代 `INSERT INTO trades VALUES (?...)` ——\n'
    '                self.conn.execute("INSERT INTO trades (a, b) VALUES (?,?)")\n'
)
check_true("[0a] 朴素 in 判定：注释里的举例让「裸 INSERT」断言假通过（**这就是坑**）",
           "INSERT INTO trades VALUES" in _SAMPLE, True)
check("[0b] 剥注释后同一断言正确判 False（说明 code_only 有效）",
      "INSERT INTO trades VALUES" in code_only(_SAMPLE), False)
check("[0c] 真正要匹配的显式列名形态在剥注释后可见",
      "INSERT INTO trades (a, b)" in code_only(_SAMPLE), True)

# ══════════════════════════════════════════════════════════════════
print("\n[1] 写入侧：save_trade 落 product_key（新库）")
# ══════════════════════════════════════════════════════════════════
with tmp_dir("write") as tmp:
    db = os.path.join(tmp, "state.db")
    st = Store(db)
    st.close()
    conn = sqlite3.connect(db)
    cols = cols_of(conn)
    check_true("[1a] 新库 trades 表含 product_key 列", "product_key" in cols,
               cols)
    check("[1b] ★ product_key 是**最后一列**（与 ALTER 迁移后的列序一致，"
          "位置绑定才不会被历史库搞乱）", cols[-1], "product_key")
    conn.close()

    st = Store(db)
    st.save_trade(mk_trade("T00001", "CFFEX.IF2609", "2026-09-02 10:00"))
    st.save_trade(mk_trade("T00002", "KQ.m@CFFEX.IF", "2026-09-02 11:00"))
    st.save_trade(mk_trade("T00003", "SHFE.au2512", "2026-09-02 12:00"))
    st.save_trade(mk_trade("T00004", "IF2609", "2026-09-02 13:00"))
    st.save_trade(mk_trade("T00005", "CFFEX.", "2026-09-02 14:00"))
    st.save_trade(mk_trade("T00006", "IH2609", "2026-09-02 15:00"))
    st.close()

    conn = sqlite3.connect(db)
    got = {r["trade_id"]: r["product_key"] for r in rows_of(conn)}
    conn.close()
    check("[1c] ★ 月份合约 → 品种键",
          got["T00001"], product_key_of("CFFEX.IF2609"))
    check("[1d] ★ 主连（前端取值）→ 同键", got["T00002"], "IF")
    check("[1e] 商品小写月份合约 → 大写键", got["T00003"], "AU")
    check("[1f] 裸代码 → 同键", got["T00004"], "IF")
    check("[1g] 解析不出（末段为空）→ 空串", got["T00005"], "")
    check("[1h] 另一品种 → 自己的键", got["T00006"], "IH")
    check("[1i] ★ 逐行核对：product_key == product_key_of(symbol)",
          sorted(got.items()),
          sorted((t, product_key_of(s)) for t, s in [
              ("T00001", "CFFEX.IF2609"), ("T00002", "KQ.m@CFFEX.IF"),
              ("T00003", "SHFE.au2512"), ("T00004", "IF2609"),
              ("T00005", "CFFEX."), ("T00006", "IH2609")]))

# ══════════════════════════════════════════════════════════════════
print("\n[2] 老库回填：加列 / 逐行正确 / 不动其他列 / 幂等 / 中断自愈")
# ══════════════════════════════════════════════════════════════════
_LEGACY_ROWS = [
    mk_trade("T00001", "CFFEX.IF2609", "2026-09-02 10:00"),
    mk_trade("T00002", "KQ.m@CFFEX.IF", "2026-09-02 11:00"),
    mk_trade("T00003", "SHFE.au2512", "2026-09-02 12:00"),
    mk_trade("T00004", "CFFEX.", "2026-09-02 13:00", net=-5.0),
    mk_trade("T00005", "IF2612", "2026-09-02 14:00", net=7.5),
]

with tmp_dir("migrate") as tmp:
    db = os.path.join(tmp, "state.db")
    build_legacy_db(db, _LEGACY_ROWS)

    # 迁移前：确认这是"真·老库"
    conn = sqlite3.connect(db)
    check("[2a] 造出的老库确实没有 product_key 列",
          "product_key" in cols_of(conn), False)
    before = {r["trade_id"]: r for r in rows_of(conn)}
    conn.close()

    st = Store(db)                       # ← 打开即迁移（加列 + 回填）
    st.close()

    conn = sqlite3.connect(db)
    cols = cols_of(conn)
    check("[2b] 打开后列出现，且在表尾", (("product_key" in cols), cols[-1]),
          (True, "product_key"))
    after = {r["trade_id"]: r for r in rows_of(conn)}
    backfilled = {k: v["product_key"] for k, v in after.items()}
    check("[2c] ★ 回填值逐行正确",
          sorted(backfilled.items()),
          sorted((t.trade_id, product_key_of(t.symbol))
                 for t in _LEGACY_ROWS))
    check("[2d] 解析不出的行回填成空串（不是 NULL，也不是 symbol 原值）",
          backfilled["T00004"], "")
    _stripped = [{k: v for k, v in after[i].items() if k != "product_key"}
                 for i in sorted(after)]
    check_true("[2e] ★ 回填**不动其他任何列**（逐行去掉新列后与原行相同）",
               _stripped == [before[i] for i in sorted(before)],
               "{} 行参与比对".format(len(_stripped)))
    check("[2f] 回填后全表无 NULL", conn.execute(
        "SELECT count(*) FROM trades WHERE product_key IS NULL"
    ).fetchone()[0], 0)

    snap = {i: dict(after[i]) for i in after}
    conn.close()

    # 幂等：再打开一次，全表逐字节不变
    st = Store(db)
    st.close()
    conn = sqlite3.connect(db)
    again = {r["trade_id"]: r for r in rows_of(conn)}
    check_true("[2g] ★ 迁移幂等：再打开一次全表不变（含 product_key）",
               again == snap, "{} 行参与比对".format(len(again)))

    # 中断自愈：手工把一行置 NULL（模拟"加列成功、回填前进程挂了"）
    conn.execute("UPDATE trades SET product_key=NULL WHERE trade_id='T00003'")
    conn.commit()
    check("[2h] 制造中断态：该行 product_key 为 NULL",
          conn.execute("SELECT product_key FROM trades WHERE trade_id='T00003'"
                       ).fetchone()[0], None)
    conn.close()

    st = Store(db)
    st.close()
    conn = sqlite3.connect(db)
    check("[2i] ★ 中断自愈：再打开一次把 NULL 行补回（幂等判据用 IS NULL）",
          conn.execute("SELECT product_key FROM trades WHERE trade_id='T00003'"
                       ).fetchone()[0],
          product_key_of("SHFE.au2512"))
    _after2 = {r["trade_id"]: dict(r) for r in rows_of(conn)}
    check_true("[2j] 自愈只补 NULL 行，其余行不变", _after2 == snap,
               "{} 行参与比对".format(len(_after2)))
    conn.close()

# ── 更老的 schema（含 net_points / cost_points 点口径）：整表改名 + 新表带列 ──
with tmp_dir("legacy2") as tmp:
    db = os.path.join(tmp, "state.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE trades ("
        " trade_id TEXT PRIMARY KEY, signal_key TEXT, symbol TEXT, side TEXT,"
        " volume INTEGER, entry_price REAL, exit_price REAL,"
        " entry_at TEXT, exit_at TEXT, reason TEXT,"
        " gross_points REAL, cost_points REAL, net_points REAL,"
        " bars_held INTEGER, exit_plan_name TEXT, exit_plan_params TEXT);")
    conn.execute(
        "INSERT INTO trades VALUES ('T00001','k','CFFEX.IF2609','LONG',1,"
        "4500.0,4510.0,'2026-09-01 09:00','2026-09-02 10:00','trailing',10.0,1.0,"
        "11.0,3,'x','{}')")
    conn.commit()
    conn.close()

    st = Store(db)
    st.close()
    conn = sqlite3.connect(db)
    tabs = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check_true("[2k] 含 net_points 的旧库：整表改名 trades_legacy_points 归档",
               "trades_legacy_points" in tabs, tabs)
    check("[2l] 新 trades 表带 product_key 列",
          "product_key" in cols_of(conn), True)
    check("[2m] 新表为空（旧行留在归档表里不参与新口径）",
          conn.execute("SELECT count(*) FROM trades").fetchone()[0], 0)
    check("[2n] 归档表内容原样保留（审计）",
          conn.execute("SELECT symbol, net_points FROM trades_legacy_points"
                       ).fetchone()[0], "CFFEX.IF2609")
    conn.close()

# ══════════════════════════════════════════════════════════════════
print("\n[3] 查询侧：列相等（合并 / 不命中 / 解析不出 / 全库）")
# ══════════════════════════════════════════════════════════════════
with tmp_dir("query") as tmp:
    db = os.path.join(tmp, "state.db")
    st = Store(db)
    for t in [
        mk_trade("T00001", "CFFEX.IF2609", "2026-09-02 10:00", net=100.0),
        mk_trade("T00002", "CFFEX.IF2612", "2026-09-03 10:00", net=200.0),
        mk_trade("T00003", "KQ.m@CFFEX.IF", "2026-09-04 10:00", net=-40.0),
        mk_trade("T00004", "CFFEX.IH2609", "2026-09-05 10:00", net=50.0),
    ]:
        st.save_trade(t)
    st.close()

    rep = load_trades_report([db], "KQ.m@CFFEX.IF")
    check("[3a] ★ 主连查 IF → 合并 3 笔（两个月份合约 + 一行主连写法）",
          len(rep["rows"]), 3)
    check("[3b] symbol_key = 品种键", rep["symbol_key"], "IF")
    check("[3c] 合约拆分 by_symbol", rep["by_symbol"],
          {"CFFEX.IF2609": 1, "CFFEX.IF2612": 1, "KQ.m@CFFEX.IF": 1})
    check("[3d] 按 exit_at 升序", [r["trade_id"] for r in rep["rows"]],
          ["T00001", "T00002", "T00003"])

    rep2 = load_trades_report([db], "IF2609")          # 裸代码
    check("[3e] 裸代码查 → 同键、同结果", len(rep2["rows"]), 3)

    rep3 = load_trades_report([db], "CFFEX.IH2609")
    check("[3f] 负向对照：查 IH → 只中 IH 那 1 笔（不跨品种）",
          [r["trade_id"] for r in rep3["rows"]], ["T00004"])

    rep4 = load_trades_report([db], "CFFEX.")
    check("[3g] ★ 解析不出品种键 → 0 笔（**绝不退回「不过滤」**）",
          (len(rep4["rows"]), rep4["symbol_key"]), (0, ""))

    rep5 = load_trades_report([db], None)
    check("[3h] 不传 symbol → 全库 4 笔",
          (len(rep5["rows"]), rep5["symbol_key"]), (4, ""))

    # ── 查询侧确实**不解读 symbol**：把 symbol 改成与品种无关的串，
    #    只要列值还在，命中结果就不变（旧口径下这里必然 0 笔）。
    conn = sqlite3.connect(db)
    conn.execute("UPDATE trades SET symbol='ZZZ9999' WHERE trade_id='T00001'")
    conn.commit()
    conn.close()
    rep6 = load_trades_report([db], "KQ.m@CFFEX.IF")
    check("[3i] ★ 判据是**列**不是 symbol：改坏 symbol 后命中数不变（仍 3 笔）",
          len(rep6["rows"]), 3)

# ══════════════════════════════════════════════════════════════════
print("\n[4] 归一规则的唯一住址")
# ══════════════════════════════════════════════════════════════════
check_true("[4a] ★ TradeStats.product_key_of 就是 Product.product_key_of"
           "（同一个函数对象，不是第二份实现）",
           _via_stats is _P.product_key_of)
check_true("[4b] Product.product_key_of 由 parse_product_key 派生"
           "（归一规则只有一份）",
           product_key_of("CFFEX.IF2609") == parse_product_key("CFFEX.IF2609"))
check("[4c] 裸代码补前导点后复用同一套规则",
      product_key_of("IF2609"), parse_product_key(".IF2609"))
check("[4d] 空 / None / 空白", (product_key_of(""), product_key_of(None),
                                product_key_of("   ")), ("", "", ""))

# ══════════════════════════════════════════════════════════════════
print("\n[5] 结构与源码级防回潮")
# ══════════════════════════════════════════════════════════════════
_sdb = read("Trading/Infra/StateDB.py")
_sdb_code = code_only(_sdb)
_st_body = _sdb[_sdb.index("def save_trade("):
                _sdb.find("\n    def ", _sdb.index("def save_trade(") + 10)]
_st_code = code_only(_st_body)

check_true("[5a] ★ save_trade 用**显式列名** INSERT（不再位置绑定）",
           "INSERT INTO trades (" in _st_code
           and "INSERT INTO trades VALUES" not in _st_code)
check_true("[5b] 显式列名里含 product_key，参数表里也含 product_key_of",
           "product_key)" in _st_code and "product_key_of(t.symbol)" in _st_code)
check_true("[5c] 仍是裸 INSERT（不带 OR REPLACE —— 撞号必须响）",
           "INSERT OR REPLACE INTO trades" not in _st_code)
check_true("[5d] _SCHEMA 里 trades 表声明了 product_key",
           "product_key TEXT" in _sdb)
check_true("[5e] 迁移区含 ALTER TABLE trades ADD COLUMN product_key",
           "ALTER TABLE trades ADD COLUMN product_key" in _sdb)
check_true("[5f] 回填判据用 IS NULL（幂等 + 可自愈）",
           "product_key IS NULL" in _sdb)

_ts = read("Trading/Infra/TradeStats.py")
_ts_code = code_only(_ts)
check_true("[5g] ★ 查询侧 SQL 走 product_key 列相等",
           'WHERE product_key = ?' in _ts_code)
check_true("[5h] 查询侧不再逐行现算（旧的 row 级 product_key_of 过滤已删）",
           'product_key_of(r.get("symbol"))' not in _ts_code)
check_true("[5i] 查询侧 import 的是 Product 的归一函数",
           "from .Product import product_key_of" in _ts)

print("\n" + "=" * 62)
print("P57 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
sys.exit(1 if _FAIL else 0)

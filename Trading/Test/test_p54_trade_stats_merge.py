# -*- coding: utf-8 -*-
"""
P54 成交统计「按品种键合并」契约（修复）
============================================================
背景（评审报告）
--------------------
「成交统计」面板**恒不可用**：前端发的是 `chartData.meta.symbol` = **主连**
`KQ.m@CFFEX.IF`（`DataAPI/TqSdkAPI.py` FUTURES_ALIASES → `App/AppChart.py`），
而库内 `trades.symbol` = `self.state.trade_symbol` = **月份合约**
`CFFEX.IF2609`（实盘由 `Broker/SimNow.py` 从 `quote.underlying_symbol` 回填）。
旧口径「双向后缀 GLOB」对这组取值**不成立** —— `KQ.m@CFFEX.IF` 里根本没有
"IF2609" 这一段，于是查询恒返回 0 笔，面板永远显示「该品种暂无历史成交」。

旧验证为什么一直绿（本轮最值钱的一条方法论）
---------------------------------------------
旧验收刻意挑了**互为后缀**的一对符号（`IF2609` ↔ `CFFEX.IF2609`）→ 必然命中，
恰好绕开真实取值。**答不上 `FAILS-IF:` 的用例，就是没在验证任何东西。**

用户拍板
----------------------
**同品种的多个月份合约合并统计** —— 判据 = **品种键相等**（归一规则只有一份：
`Infra/Product.parse_product_key`）。

（同日）：归一从**查询侧**搬到**写入侧** —— `product_key_of`（裸代码
补前导点）也移居 `Infra/Product.py`，`Store.save_trade` 落列 `trades.product_key`，
本文件验的"合并统计"因此走 `WHERE product_key = ?`（列相等）。
落库/回填/列相等的专门契约见 `test_p57_trades_product_key.py`。

覆盖
--------------------------------------------------------------------------
  [1] `product_key_of` 归一：主连 / 月份合约 / 裸代码 / 空 → 同一个品种键
  [2] ★ 旧口径对照：双向后缀 GLOB 在**真实取值**上恒不命中（回归护栏：
      有人退回 GLOB 这条就红）
  [3] 合并统计（真实 state.db，走 Store 落库）：
        a. 主连查 IF → 合并 IF2609 + IF2612
        b. 换月份查 → 同品种仍全中（口径不看月份）
        c. 负向对照：换品种 IH → 只中 IH；未标定 RB → 0 笔
        d. 不传 symbol → 全库汇总
        e. 摘要不再携带单合约 `symbol`（口径是品种，不是某一个合约）
  [4] 品种键解析失败**可见**（不伪装成"没有成交"）
  [5] 读库失败仍可见（与修复不互相掩盖）

跑法：python Trading/Test/test_p54_trade_stats_merge.py
"""
from __future__ import annotations

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
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Infra.Records import Side, Trade  # noqa: E402
from Trading.Infra.StateDB import Store  # noqa: E402
from Trading.Infra.TradeStats import (  # noqa: E402
    SRC_QUERY_FAILED, compute_trade_stats, load_trades_report, product_key_of)

_PASS = 0
_FAIL = 0

# 真实取值（不是构造出来的"互为后缀"对）：
_FRONT_SYMBOL = "KQ.m@CFFEX.IF"      # 前端 chartData.meta.symbol（主连）
_DB_SYMBOL = "CFFEX.IF2609"          # 库内 trades.symbol（月份合约）
_OLD_TEST_PAIR = "IF2609"            # 旧验收挑的符号（`CFFEX.IF2609` 的后缀）


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
    d = tempfile.mkdtemp(prefix="tg_p54_%s_" % tag)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def mk_trade(tid, symbol, net_cash, exit_at, reason="tp", volume=1):
    return Trade(
        trade_id=tid, symbol=symbol, side=Side.LONG, volume=volume,
        entry_price=4500.0, exit_price=4500.0 + net_cash,
        entry_at="2026-09-01 09:00", exit_at=exit_at, reason=reason,
        gross_points=float(net_cash), cost_cash=0.0, net_cash=float(net_cash),
        bars_held=3, signal_key="p54|" + tid, exit_plan_name="x")


def build_db(path, rows):
    """用真实 Store 落库（schema + INSERT 路径都是生产那一套）。"""
    st = Store(path)
    for t in rows:
        st.save_trade(t)
    st.close()
    return path


# ══════════════════════════════════════════════════════════════
print("\n[1] product_key_of：任意写法 → 同一个品种键（合并的唯一口径）")
# ══════════════════════════════════════════════════════════════
check("[1a] 主连（前端取值）", product_key_of(_FRONT_SYMBOL), "IF")
check("[1b] 月份合约（库内取值）", product_key_of(_DB_SYMBOL), "IF")
check("[1c] 裸代码（无交易所前缀）", product_key_of(_OLD_TEST_PAIR), "IF")
check("[1d] 商品小写主连", product_key_of("KQ.m@SHFE.au"), "AU")
check("[1e] 商品月份合约", product_key_of("SHFE.au2512"), "AU")
check("[1f] 空 / None", (product_key_of(""), product_key_of(None)), ("", ""))
check("[1g] ★ 主连与月份合约**同键** —— 本 bug 的核心判据",
      product_key_of(_FRONT_SYMBOL) == product_key_of(_DB_SYMBOL), True)


# ══════════════════════════════════════════════════════════════
print("\n[2] ★ 旧口径对照：双向后缀 GLOB 在真实取值上恒不命中")
# ══════════════════════════════════════════════════════════════
with tmp_dir("old") as tmp:
    db = build_db(os.path.join(tmp, "s.db"), [
        mk_trade("T1", _DB_SYMBOL, 100.0, "2026-09-02 10:00"),
        mk_trade("T2", _DB_SYMBOL, -50.0, "2026-09-03 10:00"),
        mk_trade("T3", _DB_SYMBOL, 30.0, "2026-09-04 10:00"),
    ])
    con = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)

    def old_glob(q):
        """旧实现的原样 SQL 谓词（exact OR 双向后缀 GLOB）。"""
        return con.execute(
            "SELECT count(*) FROM trades WHERE symbol=? "
            "OR ? GLOB ('*'||symbol) OR symbol GLOB ('*'||?)",
            (q, q, q)).fetchone()[0]

    check("[2a] 旧口径查真实前端取值 → 0 行（这就是 P0 的复现）",
          old_glob(_FRONT_SYMBOL), 0)
    check("[2b] 旧口径查旧测试挑的符号 → 3 行（所以旧验收永远是绿的）",
          old_glob(_OLD_TEST_PAIR), 3)
    check("[2c] 新口径查同一个前端取值 → 3 行（修复生效）",
          len(load_trades_report([db], _FRONT_SYMBOL)["rows"]), 3)
    con.close()


# ══════════════════════════════════════════════════════════════
print("\n[3] 合并统计：同品种多个月份合约合并（含跨品种负向对照）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("merge") as tmp:
    db = build_db(os.path.join(tmp, "s.db"), [
        mk_trade("T1", "CFFEX.IF2609", 100.0, "2026-09-02 10:00"),
        mk_trade("T2", "CFFEX.IF2609", -50.0, "2026-09-03 10:00"),
        mk_trade("T3", "CFFEX.IF2609", 30.0, "2026-09-04 10:00"),
        mk_trade("T4", "CFFEX.IF2612", 200.0, "2026-09-05 10:00"),
        mk_trade("T5", "CFFEX.IF2612", -20.0, "2026-09-06 10:00"),
        mk_trade("T6", "CFFEX.IH2609", 500.0, "2026-09-07 10:00"),
        mk_trade("T7", "SHFE.au2512", -70.0, "2026-09-08 10:00"),
    ])
    rep = load_trades_report([db], _FRONT_SYMBOL)
    check("[3a] symbol_key = 品种键", rep["symbol_key"], "IF")
    check("[3b] ★ 主连查 IF → 合并两个月共 5 笔（旧口径 0 笔）",
          len(rep["rows"]), 5)
    check("[3c] 按 exit_at 升序", [r["trade_id"] for r in rep["rows"]],
          ["T1", "T2", "T3", "T4", "T5"])
    check("[3d] 合约拆分 by_symbol", rep["by_symbol"],
          {"CFFEX.IF2609": 3, "CFFEX.IF2612": 2})
    stats = compute_trade_stats(rep["rows"])
    check("[3e] 总净盈亏 = 5 笔之和", stats["total_net"], 260.0)
    check("[3f] 笔数 / 胜 / 亏", (stats["count"], stats["wins"], stats["losses"]),
          (5, 3, 2))
    check_true("[3g] ★ 摘要不再携带单合约 symbol（口径 = 品种，不是某一个合约）",
               "symbol" not in stats, sorted(stats)[:8])

    # 换一个月份查 → 品种口径，月份不参与判据
    rep2 = load_trades_report([db], "KQ.m@CFFEX.IF2703")
    check("[3h] 换月份查同一品种 → 仍 5 笔（月份不参与判据）",
          len(rep2["rows"]), 5)

    # 负向对照：品种不同就不能串
    rep3 = load_trades_report([db], "KQ.m@CFFEX.IH")
    check("[3i] 负向对照：查 IH → 只中 IH 的 1 笔（不跨品种）",
          (len(rep3["rows"]), rep3["by_symbol"]), (1, {"CFFEX.IH2609": 1}))
    rep4 = load_trades_report([db], "KQ.m@CFFEX.RB")
    check("[3j] 未标定品种 → 0 笔", len(rep4["rows"]), 0)

    # 不传 symbol → 全库汇总（旧行为不变）
    rep5 = load_trades_report([db], None)
    check("[3k] 不传 symbol → 全库 7 笔", len(rep5["rows"]), 7)
    check("[3l] 不传 symbol 时 symbol_key 为空串", rep5["symbol_key"], "")

    # 商品品种同理（小写主连 ↔ 月份合约）
    rep6 = load_trades_report([db], "KQ.m@SHFE.au")
    check("[3m] 商品：KQ.m@SHFE.au → 命中 SHFE.au2512",
          [r["trade_id"] for r in rep6["rows"]], ["T7"])


# ══════════════════════════════════════════════════════════════
print("\n[4] 品种键解析失败必须**可见**（不伪装成「该品种暂无成交」）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("bad") as tmp:
    db = build_db(os.path.join(tmp, "s.db"), [
        mk_trade("T1", "CFFEX.IF2609", 10.0, "2026-09-02 10:00")])
    # ① 有 "." 但末段为空（"CFFEX." / 形如 "KQ.m@CFFEX." 的残缺符号）→ 品种键 = ""
    rep = load_trades_report([db], "CFFEX.")
    check("[4a] 末段为空 → 解析不出品种键，0 笔", len(rep["rows"]), 0)
    check("[4b] symbol_key 为空串（前端据此提示「无法解析品种键」）",
          rep["symbol_key"], "")
    check("[4c] 原始符号原样回传（前端要把它显示出来）",
          rep["symbol_raw"], "CFFEX.")
    # ② 纯数字（无字母）→ 剥月份后同样为空
    check("[4d] 纯数字符号 → 品种键同样为空",
          load_trades_report([db], "123456")["symbol_key"], "")
    # ③ 无 "." 的裸代码：按同一套规则当品种键（"GARBAGE"），命中不了但**可见**
    #    —— 与旧实现"悄悄 0 笔"的区别就在这里：面板会把查询用的键打出来。
    rep2 = load_trades_report([db], "garbage")
    check("[4e] 裸代码 → 归一成品种键（可见），仍然 0 笔",
          (rep2["symbol_key"], len(rep2["rows"])), ("GARBAGE", 0))


# ══════════════════════════════════════════════════════════════
print("\n[5] 读库失败仍可见（与 P0-1 修复不互相掩盖）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("fail") as tmp:
    good = build_db(os.path.join(tmp, "good.db"), [
        mk_trade("T1", "CFFEX.IF2609", 10.0, "2026-09-02 10:00")])
    broken = os.path.join(tmp, "broken.db")
    with open(broken, "wb") as fh:
        fh.write(b"this is not a sqlite file")
    missing = os.path.join(tmp, "never_created.db")

    rep = load_trades_report([good, broken, missing], _FRONT_SYMBOL)
    check("[5a] 好库的数据照常拿到（部分失败不影响已读到的）",
          len(rep["rows"]), 1)
    check("[5b] 三个库逐库上报 status",
          [s["status"] for s in rep["sources"]],
          ["ok", SRC_QUERY_FAILED, "missing"])
    check("[5c] dbs_failed = 1（missing 不算故障）", rep["dbs_failed"], 1)
    check("[5d] dbs_ok = 1", rep["dbs_ok"], 1)
    check_true("[5e] 真故障带 error 文案（可显示给用户）",
               bool([s for s in rep["sources"]
                     if s["status"] == SRC_QUERY_FAILED][0]["error"]), True)


print("\n" + "=" * 62)
print("P54 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
if _FAIL:
    raise SystemExit(1)

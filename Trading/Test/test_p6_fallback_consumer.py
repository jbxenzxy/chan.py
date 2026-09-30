# -*- coding: utf-8 -*-
"""
P6 成交价回落 · **引擎侧消费点**护栏（2026-10-01 补）
====================================================
为什么必须有它（起因，可复跑的那次实测）
    2026-10-01 复核发现：broker 写进 `meta["price_source"]` 的"成交价来源"标记，
    在 2026-09-30 版里**全仓零消费点** —— 当成交明细晚于委托终态到达时，
    `_finalize` 会先用**委托限价**把单子落成成交（价格晚到，成交事实已经成立），
    这笔"用限价回填的入场价"却没有任何环节回头纠正，一路用到平仓结算。
    修复给 `Engine._execute` 补了 `order_price_fallback` 事件的写入；但当时
    **没有任何测试盯这条写入**：把 `if _psrc == "limit":` 改成 `if False:` 后
    跑全套门禁，结果项与基线**一字不差**（同为 137/139、同为那两条既有红）——
    也就是说这条留痕可以被静默删掉而无人察觉。本文件把这个消费点钉成门禁一等组件。

三层分工（各盯一段，缺一层就是上面那种"链中间空了一环"）
    · `test_p6_fix.py`              —— 判据函数 `_p6_is_filled`（成不成交）；
    · `test_p6_finalize_wiring.py`  —— broker 侧 `_finalize` 的接线
                                       （判成交 / 成交价回落取谁 / `meta` 标来源）；
    · 本文件                        —— **引擎侧消费**：读到回落来源后要不要落事件、
                                       落哪些字段、拒单的单子不许落。

不变量（本文件钉住）
    1. 判成交 + 来源为回落价 ⇒ 恰好一条 `order_price_fallback`，字段可用于对账；
    2. 明细真实价 / 未标来源 ⇒ **零条**（这条报警不能变成"每笔成交都报"的噪音）；
    3. 拒单路径 ⇒ 零条（消费点在 `o.filled_price is None` 早退**之后**）；
    4. 历史来源值 `"ref_price"`（2026-09-30 版用信号价回落）**停收**；
    5. 事件字典里有中文标签（控制台 Echo 打印不会退化成原始 kind）。

真实程度：真 import 生产代码 + 真 `TradingEngine._execute`（dry_run broker 的
报单出口被换成一份**伪造 Order**），事件经真 `EventLog` 落 jsonl 后回读。
零网络、无需凭据。跑法：python Trading/Test/test_p6_fallback_consumer.py
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    """从本文件位置向上找 Trading 包目录（与仓库其余 Trading/Test/*.py 同款）。"""
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/Test/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

try:
    from Trading import Broker  # noqa: E401,F401  注册 dry_run（副作用 import）
    from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
    from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
    from Trading.Engine.Engine import TradingEngine, _Action  # noqa: E402
    from Trading.Infra.EventLog import EventLog, _KIND_LABEL  # noqa: E402
    from Trading.Infra.StateDB import Store  # noqa: E402
    from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
    from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402
    from Trading.Infra.Records import Order, OrderIntent, Side, Signal  # noqa: E402
    from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
    from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
except Exception as e:  # pragma: no cover
    print("✗ 无法 import 被测模块: {}: {}".format(type(e).__name__, e))
    print("  Trading 根目录解析为: {}".format(_TG_ROOT))
    raise SystemExit(2)


# ── 场景常数（对齐事故现场口径：委托限价 ≠ 信号价，让"回落有没有被看见"可判别）──
_IF = PRODUCT_PROFILES["IF"]
_SYM = "CFFEX.IF2609"
_LIMIT = 4550.4          # 委托限价（`_finalize` 回落时就用这个值落账）
_REQ = 4548.0            # 策略原始信号价
_VOLUME = 2
_ORDER_ID = "probe-fallback-1"
_SIG_KEY = "2026/09/30 10:29:00|0|B"

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def _make_order(price_source, *, filled=True):
    """伪造一笔已由 broker 判定完毕的 Order（供给侧：替掉 dry_run 的 submit 出口）。

    filled=False 时给出"被拒"形态（`status=rejected` + `filled_price=None`）—
    引擎 `_execute` 会在 `o.filled_price is None` 处早退，本文件据此钉住**消费点位置**。
    """
    return Order(
        order_id=_ORDER_ID, signal_key=_SIG_KEY, symbol=_SYM, side=Side.LONG,
        action="open", volume=_VOLUME, price=_LIMIT, req_price=_REQ,
        filled_price=(_LIMIT if filled else None),
        status=("filled" if filled else "rejected"),
        created_at="2026-09-01 09:35:00", broker="dry_run", note="probe",
        meta={"intent": "OPEN", "raw_order_id": "raw-" + _ORDER_ID,
              "direction": "BUY", "offset": "OPEN",
              "is_exit": False, "transition": 1,
              "price_source": price_source})


def execute_once(price_source, *, filled=True):
    """跑一次真实 `Engine._execute`，返回回读到的事件列表。

    每个用例独立一套临时目录（Store / EventLog 各一份），跑完显式 `flush` +
    `Store.close()` 再删 —— Windows 上 sqlite 句柄不关会让 `rmtree` 失败
    （`WinError 32`），且它抛在清理阶段会把结果行一起吞掉。
    """
    d = tempfile.mkdtemp(prefix="tg_p6fb_")
    events = []
    try:
        cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
        spec = Instrument(InstrumentConfig(trade_symbol=_SYM), _IF)
        broker = DryRunBroker(spec, {"sim_equity": 1000000.0})
        entry = EntryPolicy({})
        exitp = LayeredExitPolicy()
        store = Store(os.path.join(d, "state.db"))
        ev = EventLog(os.path.join(d, "events.jsonl"), echo=False, echo_kinds=None)
        engine = TradingEngine(cfg, broker, entry, exitp, store, ev)

        broker.submit = lambda *a, **k: _make_order(price_source, filled=filled)
        sig = Signal(key=_SIG_KEY, symbol=_SYM, freq="5m", date="2026-09-01 09:35",
                     timestamp=0, bsp_type="1", is_buy=True,
                     price=_REQ, high=_REQ + 2.0, low=_REQ - 2.0)
        act = _Action(intent=OrderIntent.OPEN, side=Side.LONG, volume=_VOLUME,
                      target=None, is_exit=False, transition=1)
        # `_execute` 会经 _run_start 打印一批"R 结构距离缺失"观测告警（与被测
        # 语义无关、且本文件断言读的是 jsonl 不是 stdout）—— 静音掉，别污染门禁日志。
        _saved_disable = logging.root.manager.disable
        logging.disable(logging.WARNING)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                engine._execute(act, _REQ, None, sig, reason="probe", force=True)
        finally:
            logging.disable(_saved_disable)
        ev.flush()
        store.close()

        path = os.path.join(d, "events.jsonl")
        if os.path.isfile(path):
            with io.open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return events


def kinds_of(events):
    return [r.get("kind") for r in events]


def fallback_records(events):
    return [r for r in events if r.get("kind") == "order_price_fallback"]


# ── 用例 ────────────────────────────────────────────────────────────────────
def test_filled_with_fallback_price() -> None:
    """不变量 1 + 2：判成交且价格来源为回落价 ⇒ 落一条，且字段够对账/复盘用。"""
    print("\n[1] 判成交 + price_source='limit' ⇒ 落一条 order_price_fallback")
    events = execute_once("limit")
    fb = fallback_records(events)
    check("[1a] 恰好落 1 条（多了是噪音，少了是断链）", len(fb), 1)
    check_true("[1b] 这条成交确实走完落账（有 open 事件）",
               "open" in kinds_of(events))
    # 不 early-return：断链时把每一条字段断言都照打一遍，红得越具体越好定位
    # （否则只剩"[1a] 条数不对"，看不出到底断在哪）。
    rec = fb[0] if fb else {}
    print("    payload: " + json.dumps(
        {k: v for k, v in rec.items() if k != "at"}, ensure_ascii=False,
        sort_keys=True))
    check("[1c] order_id 指向这笔委托", rec.get("order_id"), _ORDER_ID)
    check("[1d] price_source 如实回写", rec.get("price_source"), "limit")
    check("[1e] fill_price = 实际落账的成交价", rec.get("fill_price"), _LIMIT)
    check("[1f] limit = 委托限价（= Order.price，不是 req_price）",
          rec.get("limit"), _LIMIT)
    check("[1g] req_price = 信号价（对账时看得出两者差多少）",
          rec.get("req_price"), _REQ)
    check("[1h] volume 如实", rec.get("volume"), _VOLUME)
    check("[1i] signal_key 可用于回查信号", rec.get("signal_key"), _SIG_KEY)


def test_real_and_unmarked_prices_stay_silent() -> None:
    """不变量 2：明细真实价与未标来源都不许报 —— 否则每笔成交一条，报警等于没有。"""
    print("\n[2] 来源不是回落价 ⇒ 零条")
    for src, label in (("trade_records", "明细真实价"),
                       ("order.trade_price", "委托对象均价"),
                       ("", "未标来源（其它 broker / dry_run）")):
        fb = fallback_records(execute_once(src))
        check("[2] {} ⇒ 零条".format(label), len(fb), 0)


def test_rejected_never_falls_back() -> None:
    """不变量 3：拒单路径零条 —— 消费点必须在 `filled_price is None` 早退之后。"""
    print("\n[3] 拒单即使带着 price_source='limit' ⇒ 零条")
    events = execute_once("limit", filled=False)
    check("[3a] 零条 order_price_fallback", len(fallback_records(events)), 0)
    check_true("[3b] 确实走到了拒单分支（有 order_rejected）",
               "order_rejected" in kinds_of(events))


def test_legacy_ref_price_source_is_retired() -> None:
    """不变量 4：历史来源值 `"ref_price"` 已停收（2026-10-01 删死分支的配套断言）。

    2026-09-30 版 `_finalize` 用信号价（`ref_price`）回落，2026-10-01 起改回落
    委托限价（`limit`），`"ref_price"` **不再有生产者**，消费端同步停收：
    既避免"无主来源"被当成回落报警，也把这条决定留在门禁里 ——
    将来新增任何回落来源，必须同时扩 `_execute` 的判据与本文件。
    """
    print("\n[4] 历史来源值 'ref_price' ⇒ 零条（已停收）")
    check("[4] 零条", len(fallback_records(execute_once("ref_price"))), 0)


def test_event_label_registered() -> None:
    """不变量 5：事件字典里有中文标签（控制台 Echo 不会退化成原始 kind）。"""
    print("\n[5] EventLog 事件字典登记了中文标签")
    check("[5] _KIND_LABEL['order_price_fallback']",
          _KIND_LABEL.get("order_price_fallback"), "成交价回落")


def main() -> int:
    print("=" * 72)
    print("P6 成交价回落 · 引擎侧消费点护栏（2026-10-01 补）")
    print("被测: {}/Engine/Engine.py（真 import，非副本）".format(_TG_ROOT))
    print("=" * 72)
    test_filled_with_fallback_price()
    test_real_and_unmarked_prices_stay_silent()
    test_rejected_never_falls_back()
    test_legacy_ref_price_source_is_retired()
    test_event_label_registered()
    print("\n" + "=" * 72)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 72)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

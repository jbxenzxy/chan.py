# -*- coding: utf-8 -*-
"""P28 —— entry_date 不变量 / 交易日 SSOT 契约测试

背景（为什么要这个文件）
    规则 ⑸ 用 `Position.entry_date` 决定离场走**反向 OPEN**（今仓：反向开仓软离场，避开平今高费率）
    还是 CLOSE（昨仓：平仓）。此前该字段是**可空字符串 + 默认 ""**，而判定式
    `"" < today` 恒真 → 空值被**静默解释成"昨仓"** → 对今仓发 CLOSE 平今 →
    中金所拒单 → 连续拒单触发 phantom 清仓（簿面清空但实盘仍有仓，不可逆）。
    并且它取的是 bar 的**自然日**，夜盘品种会与真实**交易日**差一天。

本文件把修复后的契约钉死，共 6 组：
    [1] 交易日换算 SSOT（trading_day_of_ms，含夜盘归属次日）
    [2] Position 三段派生（F4：entry_bar_ts → entry_at → 空）
    [3] Signal.from_bsp fail-fast（F3：缺 date/timestamp 不得静默补空串）
    [4] F1 建仓时间锚（无时间锚 → 拒绝建仓，且**不报单**）
    [5] F2 恢复期（旧库自动修复 / 三源全空 → 拒绝启动）
    [6] 源码护栏（判定式里不得再出现自然日字符串切片；不得手写第二处夜盘偏移
        —— 后者按「夜盘语义」判定，纯日历算术不算，见 6c 的判据自测）
"""
import ast
import copy
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from Trading.Broker.DryRun import DryRunBroker
from Trading.Config import DEFAULT_CONFIG, TradingConfig, resolved_exit_params
from Trading.Engine.Engine import TradingEngine
from Trading.Infra.EventLog import EventLog
from Trading.Infra.Instrument import Instrument, InstrumentConfig
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402


_IF = PRODUCT_PROFILES["IF"]
from Trading.Infra.StateDB import Store
from Trading.Infra.Records import Bar, ExitPlan, Position, Side, Signal
from Trading.Infra.Clock import CN_TZ, NIGHT_SESSION_START_HOUR, PLAUSIBLE_DATE_MIN, trading_day_from_clock, trading_day_of_ms
from Trading.Strategy.Entry import EntryPolicy
from Trading.Strategy.Exit import LayeredExitPolicy

PASSED = 0
FAILED = 0
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def check(name, got, want):
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        print("  [OK ] {}".format(name))
    else:
        FAILED += 1
        print("  [FAIL] {}\n         got={!r}\n         want={!r}".format(name, got, want))


def check_true(name, cond, detail=""):
    check(name + ("  — " + detail if detail else ""), bool(cond), True)


def ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=CN_TZ).timestamp() * 1000)


class tmp_dir(object):
    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="p28_")
        return self.path

    def __exit__(self, *a):
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def build(tmp, tag, store_name=None):
    cfg = TradingConfig.from_dict(copy.deepcopy(DEFAULT_CONFIG))
    # 手数 = 品种执行策略表第 3 列（IF → 2 手）；原 risk.max_volume 删除
    # max_open_positions 已删除（D2）：同向持仓不再有"笔数上限静默门"，
    # 同向信号会正常开新仓。本文件不依赖该门。
    cfg.exit_params.use_atr = False
    spec = Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF)
    broker = DryRunBroker(spec, {})
    store = Store(os.path.join(tmp, store_name or ("state_%s.db" % tag)))
    ev = EventLog(os.path.join(tmp, "events_%s.jsonl" % tag),
                  echo=False, echo_kinds=None)
    eng = TradingEngine(cfg, broker, EntryPolicy({}),
                        LayeredExitPolicy(resolved_exit_params(cfg)), store, ev)
    return eng, broker, spec


def mk_sig(date, price, key, ts, is_buy=True):
    return Signal(key=key, symbol="CFFEX.IF2609", freq="5m", date=date,
                  timestamp=ts, bsp_type="1", is_buy=is_buy, price=price,
                  high=price + 5, low=price - 5,
                  fractal_low=price - 10, fractal_high=price + 30)


# ════════════════════════════════════════════════════════════════════
print("\n[1] 交易日换算 SSOT（夜盘归属次日）")
TS_DAY = ms(2026, 9, 10, 9, 35)          # 日盘
TS_NIGHT = ms(2026, 9, 10, 21, 0)        # 夜盘开盘
TS_LATE = ms(2026, 9, 10, 23, 59)        # 夜盘尾
TS_DAWN = ms(2026, 9, 11, 1, 0)          # 次日凌晨（夜盘延续）
TS_PRE = ms(2026, 9, 10, 19, 59)         # 夜盘前一分钟

check("[1a] 日盘 09:35 → 当日", trading_day_of_ms(TS_DAY), "2026-09-10")
check("[1b] 夜盘 21:00 → 次一交易日", trading_day_of_ms(TS_NIGHT), "2026-09-11")
check("[1c] 夜盘 23:59 → 次一交易日", trading_day_of_ms(TS_LATE), "2026-09-11")
check("[1d] 次日凌晨 01:00 → 同一交易日（与夜盘同属 09-11）",
      trading_day_of_ms(TS_DAWN), "2026-09-11")
check("[1e] 夜盘起点前一分钟 19:59 → 仍是当日",
      trading_day_of_ms(TS_PRE), "2026-09-10")
check("[1f] 夜盘起点常量 = 20", NIGHT_SESSION_START_HOUR, 20)
check("[1g] 无效时间戳 → ''（不得编造日期）", trading_day_of_ms(0), "")
check("[1h] 负时间戳 → ''", trading_day_of_ms(-1), "")
check("[1i] None → ''", trading_day_of_ms(None), "")

# 核心不变式：同一天的夜盘 + 次日日盘必须判为**同一交易日**
check("[1j] 夜盘仓 vs 次日日盘 = 同一交易日（关键：不会误判成昨仓）",
      trading_day_of_ms(TS_NIGHT) == trading_day_of_ms(ms(2026, 9, 11, 10, 0)), True)
check("[1k] 夜盘仓 vs 当晚日盘 ≠ 同一交易日（跨了交易日）",
      trading_day_of_ms(TS_NIGHT) != trading_day_of_ms(TS_DAY), True)

check("[1l] 墙钟解析：ISO 串取日期", trading_day_from_clock("2026-09-01T09:00:00+08:00"), "2026-09-01")
check("[1m] 墙钟解析：空格分隔取日期", trading_day_from_clock("2026-09-01 09:00"), "2026-09-01")
check("[1n] 墙钟解析：空串 → ''", trading_day_from_clock(""), "")
check("[1o] 墙钟解析：占位符 '?' → ''", trading_day_from_clock("?"), "")
check("[1p] 墙钟解析：早于可信下限 → ''", trading_day_from_clock("1970-01-01 00:00"), "")
check("[1q] 可信下限常量", PLAUSIBLE_DATE_MIN, "2020-01-01")

# ════════════════════════════════════════════════════════════════════
print("\n[2] Position 三段派生（F4：entry_date 不再是可空的自由字段）")


def mkpos(**kw):
    base = dict(symbol="CFFEX.IF2609", side=Side.LONG, volume=2,
                entry_price=4490.2, entry_at="2026-09-01T09:00:00+08:00",
                entry_bar_ts=0, signal_key="k", open_order_id="o",
                exit_plan=ExitPlan(name="x", stop_price=0.0))
    base.update(kw)
    return Position(**base)


p = mkpos(entry_date="2026-09-05", entry_bar_ts=TS_NIGHT)
check("[2a] 显式 entry_date 优先（不做派生覆盖）", p.entry_date, "2026-09-05")

p = mkpos(entry_bar_ts=TS_NIGHT)
check("[2b] 无 entry_date + 有效 bar_ts（夜盘）→ 派生出交易日",
      p.entry_date, "2026-09-11")

p = mkpos(entry_bar_ts=ms(2026, 9, 2, 10, 0))
check("[2c] 无 entry_date + 有效 bar_ts（日盘）→ 派生出当日",
      p.entry_date, "2026-09-02")

p = mkpos(entry_bar_ts=4000)         # 夹具常见：把序号当时间戳
check("[2d] bar_ts 不是真实毫秒（4000）→ 拒绝采用，回落 entry_at",
      p.entry_date, "2026-09-01")

p = mkpos(entry_bar_ts=0)
check("[2e] bar_ts=0 → 回落 entry_at", p.entry_date, "2026-09-01")

p = mkpos(entry_bar_ts=0, entry_at="")
check("[2f] 三源全空 → 保持空串（交由调用方 fail-fast，绝不猜）",
      p.entry_date, "")

p = mkpos(entry_bar_ts=0, entry_at="garbage")
check("[2g] entry_at 非法 → 空串", p.entry_date, "")

# 幂等：派生结果再进 from_dict 必须稳定
p = mkpos(entry_bar_ts=TS_NIGHT)
p2 = Position.from_dict(p.to_dict())
check("[2h] roundtrip 稳定（派生值被持久化）", p2.entry_date, "2026-09-11")

# 旧 dict 缺 entry_date 键 + 有真实 bar_ts → 自动修复
old = {"symbol": "CFFEX.IF2609", "side": "LONG", "volume": 2,
       "entry_price": 4490.2, "entry_at": "2026-09-01T09:00:00+08:00",
       "entry_bar_ts": ms(2026, 9, 2, 9, 35), "signal_key": "OLD", "open_order_id": "o",
       "exit_plan": {"name": "x", "stop_price": 0.0}, "origin": "signal_open"}
check("[2i] 旧库缺 entry_date 键 → 从 bar_ts 精确重建（不是猜方向）",
      Position.from_dict(old).entry_date, "2026-09-02")

# ════════════════════════════════════════════════════════════════════
print("\n[3] Signal.from_bsp fail-fast（F3：保住幂等键）")

GOOD = {"date": "2026-09-01 09:35", "timestamp": ms(2026, 9, 1, 9, 35), "type": "1",
        "is_buy": True, "price": 4490.2, "high": 4495.2, "low": 4485.2,
        "fractal_low": 4480.2, "fractal_high": 4520.2}
s = Signal.from_bsp(GOOD, "CFFEX.IF2609", "5m")
check("[3a] 正常 bsp → date 非空且 key 含 date",
      (s.date, s.key), ("2026-09-01 09:35", "2026-09-01 09:35|1|B"))

for label, bad in (("缺 date", {k: v for k, v in GOOD.items() if k != "date"}),
                   ("date 为空串", dict(GOOD, date="")),
                   ("缺 timestamp", {k: v for k, v in GOOD.items() if k != "timestamp"}),
                   ("timestamp=0", dict(GOOD, timestamp=0))):
    try:
        Signal.from_bsp(bad, "CFFEX.IF2609", "5m")
        check("[3b] {} → 必须抛 ValueError".format(label), "未抛错", "ValueError")
    except ValueError:
        check_true("[3b] {} → 抛 ValueError（不静默补空串）".format(label), True)

# 幂等键污染的后果演示：两条不同 K 线的 bsp 若都缺 date，键会撞在一起
k1 = Signal.make_key("", "1", True)
k2 = Signal.make_key("", "1", True)
check("[3c] 缺 date 时幂等键必然相同（这正是不允许静默补空串的原因）", k1, k2)

# ════════════════════════════════════════════════════════════════════
print("\n[4] F1 建仓时间锚（无时间锚 → 拒绝建仓且不报单）")

with tmp_dir() as tmp:
    eng, broker, spec = build(tmp, "f1a")
    eng.on_bar(Bar(timestamp=ms(2026, 9, 2, 9, 35), date="2026-09-02 09:35",
                   open=4490.0, high=4495.0, low=4485.0, close=4490.2))
    eng.on_signal(mk_sig("2026-09-02 09:35", 4490.2, "F1|1|B", ms(2026, 9, 2, 9, 35)))
    ps = eng.positions.positions
    check("[4a] 正常路径：建仓成功且 entry_date 非空",
          (len(ps), ps[0].entry_date if ps else None), (1, "2026-09-02"))
    check("[4b] 建仓的 entry_bar_ts 必 > 0（可派生保证）",
          ps[0].entry_bar_ts > 0 if ps else False, True)

with tmp_dir() as tmp:
    # 无 bar，但信号自带有效时间戳 → sig.timestamp 兜底，仍可建仓
    eng, broker, spec = build(tmp, "f1b")
    eng.on_signal(mk_sig("2026-09-02 09:35", 4490.2, "F1B|1|B",
                         ms(2026, 9, 2, 9, 35)))
    ps = eng.positions.positions
    check("[4c] 无 bar 但 sig.timestamp 有效 → 可建仓，entry_date 正确",
          (len(ps), ps[0].entry_date if ps else None), (1, "2026-09-02"))

with tmp_dir() as tmp:
    # 无 bar 且信号时间戳也无效 → 必须拒绝建仓，且**不发出任何报单**
    eng, broker, spec = build(tmp, "f1c")
    eng.on_signal(mk_sig("", 4490.2, "F1C|1|B", 0))
    check("[4d] 无任何时间锚 → 簿面为空（拒绝建仓）",
          len(eng.positions.positions), 0)
    check("[4e] 无任何时间锚 → 没有报单发出（拒绝在 submit 之前）",
          len(broker.orders), 0)
    acts = eng.store.signal_action("F1C|1|B")
    check("[4f] signal_action 记为 rejected/no_time_anchor",
          acts, "rejected")

with tmp_dir() as tmp:
    # 夜盘建仓 → entry_date 必须是次一交易日
    eng, broker, spec = build(tmp, "f1d")
    eng.on_bar(Bar(timestamp=ms(2026, 9, 10, 21, 5), date="2026-09-10 21:05",
                   open=4490.0, high=4495.0, low=4485.0, close=4490.2))
    eng.on_signal(mk_sig("2026-09-10 21:05", 4490.2, "F1D|1|B",
                         ms(2026, 9, 10, 21, 5)))
    ps = eng.positions.positions
    check("[4g] 夜盘 21:05 建仓 → entry_date = 次一交易日 2026-09-11",
          ps[0].entry_date if ps else None, "2026-09-11")

# ════════════════════════════════════════════════════════════════════
print("\n[5] F2 恢复期（旧 schema 严格拒绝 D16 / 三源全空拒绝启动）")

with tmp_dir() as tmp:
    # 旧 schema 持仓记录（含 origin 等已删键）写入 state.db
    # → G1 闸门必须**拒绝启动**（D16：宁停不错，不做兼容迁移）
    build(tmp, "r1")
    eng2, broker2, spec2 = build(tmp, "r1b", store_name="state_r1.db")
    eng2.store.set_json("positions", [dict(old, symbol=spec2.trade_symbol)])
    raised_legacy = None
    try:
        build(tmp, "r1c", store_name="state_r1.db")
    except RuntimeError as e:
        raised_legacy = str(e)
    check_true("[5a] 旧 schema 持仓记录 → 拒绝启动（D16 严格拒绝，不再自动修复）",
               raised_legacy is not None,
               "e.g. {}".format((raised_legacy or "")[:40]))
    check_true("[5a2] 拒绝原因点名旧键名（可定位到要删的字段）",
               bool(raised_legacy) and "origin" in raised_legacy)
    check_true("[5a3] 拒绝原因给出处理方式（指向 state.db）",
               bool(raised_legacy) and "state.db" in raised_legacy)

with tmp_dir() as tmp:
    # 三源全空 → 拒绝启动（G3 时间锚闸门）
    build(tmp, "r2")
    eng, _, spec = build(tmp, "r2b", store_name="state_r2.db")
    # 注意：本条记录**不含**已删键，专测时间锚闸门（否则会先被 G1 拦掉）
    eng.store.set_json("positions", [{
        "symbol": spec.trade_symbol, "side": "LONG", "volume": 2,
        "entry_price": 4490.2, "entry_at": "", "entry_bar_ts": 0,
        "signal_key": "DEAD", "open_order_id": "o",
        "exit_plan": {"name": "x", "stop_price": 0.0}}])
    raised = None
    try:
        build(tmp, "r2c", store_name="state_r2.db")
    except RuntimeError as e:
        raised = str(e)
    check_true("[5c] 三源全空 → _restore 抛 RuntimeError（拒绝启动）",
               raised is not None, "e.g. {}".format((raised or "")[:40]))
    check_true("[5d] 错误信息指明三个时间源与处理方式",
               raised is not None and "entry_date / entry_bar_ts / entry_at" in raised
               and "state.db" in raised)

# ════════════════════════════════════════════════════════════════════
print("\n[6] 源码护栏（判定式不得再用自然日字符串切片）")

# 只扫"判定/取值"用法：xxx.date[:10]、now_cn()[:10]、entry_date[:10]
BAD = re.compile(r"(?:\b(?:sig|bar|last_bar|pos|p)\.(?:entry_)?date\[:10\])"
                 r"|(?:now_cn\(\)\[:10\])")
violations = []
for sub in ("Trading/Engine", "Trading/Infra", "Trading/Source", "Trading/Broker",
            "Trading/Strategy"):
    d = os.path.join(ROOT, sub)
    for dirpath, _dirs, files in os.walk(d):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            with open(fp, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    code = line.split("#")[0]
                    if BAD.search(code):
                        violations.append("{}:{}: {}".format(
                            os.path.relpath(fp, ROOT).replace("\\", "/"), i,
                            code.strip()[:90]))
check_true("[6a] 全库无 date[:10] 形式的日期判定残留",
           not violations, "; ".join(violations[:3]))

# entry_date 的默认值不得再由下游解释：判定处不得出现 bool(pos.entry_date)
src_engine = open(os.path.join(ROOT, "Trading/Engine/Engine.py"), encoding="utf-8").read()
engine_code = "\n".join(l.split("#")[0] for l in src_engine.splitlines())
check_true("[6b] Engine 代码里不再出现 bool(pos.entry_date) 这类'空串当假值'写法",
           "bool(pos.entry_date)" not in engine_code)
check_true("[6b2] Engine 代码里 entry_date 不再做 [:10] 切片比较",
           "entry_date[:10]" not in engine_code)

# trading_day_of_ms 必须是唯一换算入口：不得有第二处手写夜盘偏移。
#
# ⚠️ 2026-09-14 判据收窄（原判据误伤）：
#   原判据 = "出现 timedelta(days=1) 即违规"，会把新增的
#   Instrument._weekdays_between() 判成手写夜盘偏移 —— 但那里是在**数工作日**
#   （纯日历算术），与夜盘归属次日无关。改成"按夜盘语义判定"：
#     同一个代码块（每个函数体 / 模块顶层）内**同时**出现
#       (a) `timedelta(days=1)`，且
#       (b) 夜盘语义信号 —— 引用 NIGHT_SESSION_START_HOUR，或 `.hour` 与
#           0/20/21/23 这几个夜盘小时常量比较
#   纯日历算术不含 (b) → 不违规；真手写夜盘偏移必含 (b) → 照样被抓（见下方自测）。
#   ⚠️ 刻意**不采用**"把 Instrument.py 加进白名单"的修法 —— 那会连该文件里将来
#   真出现的手写夜盘偏移一起放过，属于削弱护栏。
NIGHT_HOURS = {0, 20, 21, 23}
_RE_HOUR_L = re.compile(r"\.hour\s*(?:>=|<=|==|!=|>|<)\s*(\d{1,2})")
_RE_HOUR_R = re.compile(r"(\d{1,2})\s*(?:>=|<=|==|!=|>|<)\s*[\w.]*\.hour")


def _strip_comments(text):
    return "\n".join(l.split("#")[0] for l in text.splitlines())


def _night_signal(text):
    """块内是否含夜盘语义信号（夜盘小时常量比较 / 引用夜盘起始小时）。"""
    if "NIGHT_SESSION_START_HOUR" in text:
        return True
    for rx in (_RE_HOUR_L, _RE_HOUR_R):
        for m in rx.finditer(text):
            if int(m.group(1)) in NIGHT_HOURS:
                return True
    return False


def hand_rolled_night_offset(source_text):
    """返回 source_text 中「手写第二处夜盘偏移」的块起始行（1-based）。"""
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []
    lines = source_text.splitlines()
    fn_blocks, covered = [], set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_blocks.append((node.lineno, node.end_lineno))
            covered.update(range(node.lineno, node.end_lineno + 1))
    hits = set()
    for a, b in fn_blocks:
        seg = _strip_comments("\n".join(lines[a - 1:b]))
        if "timedelta(days=1)" in seg and _night_signal(seg):
            hits.add(a)
    top_lines = [i for i in range(1, len(lines) + 1) if i not in covered]
    if top_lines:
        seg = _strip_comments("\n".join(lines[i - 1] for i in top_lines))
        if "timedelta(days=1)" in seg and _night_signal(seg):
            hits.add(top_lines[0])
    return sorted(hits)


hand_rolled = []
for sub in ("Trading/Engine", "Trading/Infra", "Trading/Source", "Trading/Broker"):
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, sub)):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            if "Clock.py" in fp or "Period.py" in fp:
                continue
            txt = open(fp, encoding="utf-8").read()
            for ln in hand_rolled_night_offset(txt):
                hand_rolled.append("{}:{}".format(
                    os.path.relpath(fp, ROOT).replace("\\", "/"), ln))
check_true("[6c] 夜盘日期偏移只在 Infra/Clock.py 一处实现（按夜盘语义判定）",
           not hand_rolled, "另见: {}".format(hand_rolled[:5]))

# 6c 判据自测：证明"收窄"没有变成"削弱护栏"。
check("[6c-s1] 判据不误伤纯日历算术（_weekdays_between 形态）",
      hand_rolled_night_offset(
          "def f(a, b):\n    d = a\n    d += timedelta(days=1)\n    return d\n"),
      [])
check("[6c-s2] 判据仍抓得住手写夜盘偏移（dt.hour >= 21 加一天）",
      hand_rolled_night_offset(
          "def f(dt):\n    if dt.hour >= 21:\n        dt += timedelta(days=1)\n"
          "    return dt\n"),
      [1])
check("[6c-s3] 判据仍抓得住引用夜盘常量的手写偏移",
      hand_rolled_night_offset(
          "def f(dt):\n    if dt.hour >= NIGHT_SESSION_START_HOUR:\n"
          "        dt = dt + timedelta(days=1)\n    return dt\n"),
      [1])
check("[6c-s4] 判据仍抓得住模块顶层的手写夜盘偏移",
      hand_rolled_night_offset(
          "X = 1\nif dt.hour >= 20:\n    X = dt + timedelta(days=1)\n"),
      [1])

# ════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("P28 结果: {} passed, {} failed".format(PASSED, FAILED))
print("=" * 60)
sys.exit(1 if FAILED else 0)

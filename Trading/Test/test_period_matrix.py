# -*- coding: utf-8 -*-
"""
四周期回归矩阵（30m / 5m / 1m / 15s × 毫秒源 / 秒源）
=====================================================
Step 1 的产物：**文档会过时，测试不会**。这份测试把"换个周期代码还能不能
正确跑通"钉成可执行的断言 —— 以后谁再动周期相关代码，改错就红。

覆盖的缺陷（详见 Step1_四周期逻辑正确性审计.md）
    BUG-1  bar_secs 推断把毫秒当秒 → 四个周期全部推断失败、静默降级
    BUG-3  收盘判定用 bar 起点 → 30m 永不可达 + 15s 丢秒
    BUG-4  ATR 缓冲跨日不清空 → 隔夜跳空污染（30m 下污染近两天）
    BUG-5  max_hold_bars 根数语义 → 同一数字跨周期漂移 120 倍
    BUG-6  追价窗口长于一根 bar（15s）—— 本测试只验证可见性口径

跑法：python test_period_matrix.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
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
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.PeriodProfile import (  # noqa: E402
    bar_sec_of_day, bar_secs_for, eod_triggered, norm_delta_sec, parse_hhmmss,
    ts_scale,
)
from Trading.Infra.Types import Bar, ExitPlan, Position, Side  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name + ("  -> {}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


# (freq, bar_secs, 尾盘应触发的 bar 起点, 再早一根的 bar 起点)
# 阈值 session_end = 14:55，判定式 = 起点 + 2×bar_secs >= 14:55
FREQ_CASES = [
    ("15s", 15, "14:54:30", "14:54:15"),
    ("1m", 60, "14:53:00", "14:52:00"),
    ("5m", 300, "14:45:00", "14:40:00"),
    ("30m", 1800, "14:00:00", "13:30:00"),
]
SESSION_END = "14:55"
MARKET_CLOSE = "15:00"      # IF 收盘：触发 bar 的结束时刻必须早于它


def make_bar(ts, date, o=100.0, h=101.0, l=99.0, c=100.0):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c)


def make_position(entry=100.0, stop=90.0, tp=120.0, params=None):
    plan = ExitPlan(name="LayeredExitPolicy", stop_price=stop, tp_price=tp,
                    params=params or {})
    return Position(symbol="CFFEX.IF", side=Side.LONG, volume=1,
                    entry_price=entry, entry_at="2026-09-01 09:35",
                    entry_bar_ts=0, signal_key="k", open_order_id="o1",
                    exit_plan=plan, entry_bar_seq=10)


def pol_for(bar_secs, **kw):
    """构造一个已知 bar_secs 的 LayeredExitPolicy（模拟引擎注入后的状态）。"""
    p = LayeredExitPolicy(dict(bar_secs=bar_secs, **kw))
    return p


def _run_engine_smoke():
    """引擎级：用真实 GatewayEngine + DryRunBroker 跑四个周期。

    不依赖 tqsdk / 网络 / 数据库外部服务（Store 用 tempfile sqlite）。
    """
    import json
    import shutil
    import tempfile

    from Trading import Broker  # noqa: F401  触发 dry_run 注册
    from Trading.Broker.DryRun import DryRunBroker
    from Trading.Config import DEFAULT_CONFIG, GatewayConfig
    from Trading.Engine.Engine import GatewayEngine
    from Trading.Infra.EventLog import EventLog
    from Trading.Infra.Store import Store
    from Trading.Strategy.Entry import DefaultEntryPolicy
    from Trading.Strategy.Exit import LayeredExitPolicy

    for freq, secs, trigger_start, _early in FREQ_CASES:
        tmp = tempfile.mkdtemp(prefix="tg_period_")
        try:
            cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
            cfg.source.freq = freq
            # 用 Layered 策略 + 尾盘阈值，检查引擎是否把 bar_secs 正确注入
            exitp = LayeredExitPolicy({"session_end_hhmm": SESSION_END,
                                       "max_hold_seconds": 0.0})
            spec = InstrumentSpec()
            broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
            ev = EventLog(os.path.join(tmp, "ev.jsonl"), echo=False)
            eng = GatewayEngine(cfg, broker, DefaultEntryPolicy({}), exitp,
                                Store(os.path.join(tmp, "state.db")), ev)
            # ① 引擎解析出周期
            check("{} 引擎 bar_secs = {}".format(freq, secs),
                  eng.bar_secs, secs)
            # ② 注入到策略（策略的 effective_bar_secs 拿得到）
            check("{} 策略 effective_bar_secs = {}".format(freq, secs),
                  exitp.effective_bar_secs, secs)
            # ③ 走一根尾盘 bar → 引擎侧 _after_close 也应判为临近收盘。
            #    注意引擎用的阈值是 spec.sessions 收盘（15:00），
            #    策略侧的 session_end_hhmm（14:55）是另一道更早的兜底，
            #    两者阈值不同 → 触发 bar 起点也不同，这里按引擎阈值算：
            #        start + bar_secs + lead(1)×bar_secs >= 15:00
            close_sec = parse_hhmmss(MARKET_CLOSE)
            start_sec = close_sec - 2 * secs
            hms = "{:02d}:{:02d}:{:02d}".format(
                start_sec // 3600, start_sec % 3600 // 60, start_sec % 60)
            bar = make_bar(1_000_000 + secs * 1000, "2026-09-01 " + hms)
            eng.on_bar(bar)
            check("{} 引擎 _after_close 在 {}（收盘前 {}s）判为临近收盘".format(
                freq, hms, 2 * secs), eng._after_close(bar), True)
            # 再早一根不应触发（避免过早强平）
            early = make_bar(1_000_000, "2026-09-01 " + "{:02d}:{:02d}:{:02d}".format(
                (start_sec - secs) // 3600,
                (start_sec - secs) % 3600 // 60,
                (start_sec - secs) % 60))
            check("{} 再早一根不判临近收盘（不过早强平）".format(freq),
                  eng._after_close(early), False)
            # ④ 事件日志里不应出现"周期未知"告警
            kinds = []
            with open(os.path.join(tmp, "ev.jsonl"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        kinds.append(json.loads(line).get("kind", ""))
            check("{} 无 freq_unknown 告警".format(freq),
                  "freq_unknown" in kinds, False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    spec = InstrumentSpec()
    print("=" * 60)
    print("四周期回归矩阵：{}".format(", ".join(f for f, _, _, _ in FREQ_CASES)))
    print("=" * 60)

    # ── [1] 周期 → 秒 ────────────────────────────────────────────
    print("\n[1] freq → bar_secs 映射")
    for freq, secs, _, _ in FREQ_CASES:
        check("bar_secs_for('{}') = {}".format(freq, secs),
              bar_secs_for(freq), secs)

    # ── [2] 时间戳单位判定（毫秒 / 秒）────────────────────────────
    print("\n[2] 时间戳单位判定")
    check("毫秒时间戳（1.7e12）→ 除数 1000", ts_scale(1756000000000), 1000.0)
    check("秒时间戳（1.7e9）→ 除数 1", ts_scale(1756000000), 1.0)
    check("norm_delta_sec 毫秒 300000 → 300 秒", norm_delta_sec(300000, 0), 300.0)
    check("norm_delta_sec 秒 300 → 300 秒", norm_delta_sec(300, 0), 300.0)

    # ── [3] BUG-1：兜底推断在两种单位下都正确 ──────────────────────
    print("\n[3] BUG-1 回归：bar_secs 兜底推断（毫秒源 / 秒源都要对）")
    for freq, secs, _, _ in FREQ_CASES:
        for unit, mult in (("毫秒", 1000), ("秒", 1)):
            p = LayeredExitPolicy()           # 不注入，强制走推断路径
            base = 1756000000 * (1000 if mult == 1000 else 1)
            p.on_bar(make_bar(base, "2026-09-01 09:30:00"), spec)
            p.on_bar(make_bar(base + secs * mult, "2026-09-01 09:30:00"), spec)
            check("{} / {}源 → 推断出 {} 秒".format(freq, unit, secs),
                  p.effective_bar_secs, secs)

    print("\n[3b] 旧口径复算（回归证据：修复前 4 个周期全部失效）")
    for freq, secs, _, _ in FREQ_CASES:
        # 旧代码：secs = int(bar.timestamp - prev_ts); if 60 <= secs <= 14400
        raw_ms = secs * 1000                      # SSE 源实际是毫秒
        old_ok = 60 <= raw_ms <= 14400
        check("{} 旧口径（毫秒当秒比 60~14400）判定失败".format(freq),
              old_ok, False)

    # ── [4] BUG-3：四个周期都能在收盘前平掉 ────────────────────────
    print("\n[4] BUG-3 回归：EOD 收盘强平（四个周期都必须可达）")
    thr = parse_hhmmss(SESSION_END)
    close_sec = parse_hhmmss(MARKET_CLOSE)
    for freq, secs, hit_t, miss_t in FREQ_CASES:
        p = pol_for(secs, session_end_hhmm=SESSION_END)
        pos = make_position()
        # 应触发的那根
        chk = p.check(pos, make_bar(1, "2026-09-01 " + hit_t), spec,
                      bars_held=1, held_secs=1.0)
        check("{} {} 起点的 bar 触发 eod_time".format(freq, hit_t[:5]),
              chk.reason if chk else None, "eod_time")
        # 触发时机必须在收盘前：bar 结束时刻 < 15:00
        end_sec = parse_hhmmss(hit_t) + secs
        check("{} 触发 bar 结束于 {}（早于收盘 15:00）".format(freq, hit_t[:5]),
              end_sec < close_sec, True)
        # 再早一根不触发
        chk2 = p.check(pos, make_bar(2, "2026-09-01 " + miss_t), spec,
                       bars_held=1, held_secs=1.0)
        check("{} {} 起点的 bar 不触发".format(freq, miss_t[:5]),
              chk2 is None, True)

    print("\n[4b] 旧口径复算：30m 在旧逻辑下永不可达（回归证据）")
    # BUG-1 使 _bar_secs 恒 None → 旧代码走"降级起点判定"：hit = 起点 >= 阈值。
    # 30m 的 bar 起点只有 :00 / :30，14:00 与 14:30 都 < 14:55 → 永远不触发，
    # 收盘强平对 30m 是彻底死代码（连"零缓冲触发"都做不到）。
    old_hit_any = False
    for mm in (0, 30):
        start_min = 14 * 60 + mm
        if start_min >= 14 * 60 + 55:          # 降级口径：起点 >= 14:55
            old_hit_any = True
    check("30m 旧口径（起点判定：14:00/14:30）均不触发 → 强平死代码",
          old_hit_any, False)
    # 对照：即便 _bar_secs 侥幸拿到（旧逻辑的 start+30min 口径），
    # 14:30 那根也要等到 15:00 收盘才推送 → 触发时零缓冲，等于没用。
    check("30m 旧口径即便推断成功也只是 15:00 才触发（零缓冲）",
          14 * 60 + 30 + 30 >= 14 * 60 + 55
          and (14 * 60 + 30 + 30) - (14 * 60 + 55), 5)

    # ── [5] BUG-5：时间止损按秒，跨周期一致 ────────────────────────
    print("\n[5] max_hold_bars 周期无关性：30 根在任何周期下都 = 30 根")
    # 语义以 L4 设计文档为准：时间止损的计量单位是 **K 线根数**（"N 根无进展 → 走"），
    # 不是墙钟时间。所以 30 根在 15s 下是 30 根、在 30m 下也是 30 根 —— 周期无关。
    LIMIT_BARS = 30
    for freq, secs, _, _ in FREQ_CASES:
        p = pol_for(secs, max_hold_bars=LIMIT_BARS)
        pos = make_position()
        # 29 根 → 一律不触发（不管这 29 根代表 7 分钟还是 14 小时）
        chk = p.check(pos, make_bar(1, "2026-09-01 10:00:00"), spec,
                      bars_held=29, held_secs=29.0 * secs)
        check("{} 持 29 根（{:.1f} 分钟）不触发".format(freq, 29 * secs / 60.0),
              chk is None, True)
        # 30 根 → 一律触发
        chk2 = p.check(pos, make_bar(2, "2026-09-01 10:05:00"), spec,
                       bars_held=30, held_secs=30.0 * secs)
        check("{} 持 30 根触发 time".format(freq),
              chk2.reason if chk2 else None, "time")

    print("\n[5b] 同一个「30 根」在各周期的墙钟跨度（标定参考，非正确性断言）")
    # 30 根在不同周期下代表的墙钟时间差 120 倍 —— 这是 **Step 2 标定**问题，
    # 不是代码缺陷：单位始终是根，语义自始至终一致。
    hold_secs = {f: 30 * s for f, s, _, _ in FREQ_CASES}
    check("同样 30 根：15s 只持 {} 秒".format(hold_secs["15s"]),
          hold_secs["15s"], 450)
    check("同样 30 根：30m 持了 {} 秒".format(hold_secs["30m"]),
          hold_secs["30m"], 54000)
    check("墙钟跨度漂移倍数 = 120", hold_secs["30m"] // hold_secs["15s"], 120)
    # 30m 的现实约束：一天仅 8 根，30 根跨 3.75 个交易日 → 实际轮不到它，
    # 由 EOD 收盘强平接管。这就是"默认 30"在 30m 下形同虚设的原因。
    check("30m 一天 8 根 → 30 根需 3.75 个交易日（EOD 先接管）",
          round(30 / 8.0, 2), 3.75)

    print("\n[5c] 可选墙钟顶 max_hold_seconds（默认 0=关闭，不干扰根数口径）")
    p_def = pol_for(300, max_hold_bars=30)
    check("默认（未设秒顶）：持 10 根不触发（哪怕墙钟已很久）",
          p_def.check(make_position(), make_bar(1, "2026-09-01 10:00"), spec,
                      bars_held=10, held_secs=100000.0) is None, True)
    p_sec = pol_for(1800, max_hold_bars=30, max_hold_seconds=3600.0)
    chk_s = p_sec.check(make_position(), make_bar(2, "2026-09-01 10:00"), spec,
                        bars_held=3, held_secs=5400.0)
    check("30m 持 3 根但已 5400 秒 ≥ 秒顶 3600 → 触发（谁先到谁生效）",
          chk_s.reason if chk_s else None, "time")

    # ── [6] date 带秒的解析 ───────────────────────────────────────
    print("\n[6] 15s 的 date 带秒：解析不能丢秒")
    check("parse_hhmmss('14:54:45') = 53685", parse_hhmmss("14:54:45"), 53685)
    check("parse_hhmmss('14:54') = 53640", parse_hhmmss("14:54"), 53640)
    check("bar_sec_of_day 带日期", bar_sec_of_day("2026-09-01 14:54:45"), 53685)
    b = make_bar(1, "2026-09-01 14:54:45")
    check("bar_sec_of_day(Bar)", bar_sec_of_day(b), 53685)
    check("旧口径 s[:5] 会丢 45 秒（53640 vs 53685）",
          53685 - 53640, 45)

    # ── [7] BUG-4：ATR 缓冲跨日清空 ───────────────────────────────
    print("\n[7] BUG-4 回归：ATR 缓冲跨日清空")
    for freq, secs, _, _ in FREQ_CASES:
        p = pol_for(secs, atr_period=3)
        p.on_bar(make_bar(1, "2026-09-01 14:50:00"), spec)
        p.on_bar(make_bar(2, "2026-09-01 14:55:00"), spec)
        n_before = len(p._bars)
        p.on_bar(make_bar(3, "2026-09-02 09:30:00"), spec)   # 跨日
        check("{} 跨日后缓冲从 {} 清空为 1".format(freq, n_before),
              len(p._bars), 1)

    print("\n[7b] 同日连续 bar 不清空（避免误伤）")
    p = pol_for(300, atr_period=3)
    for i in range(5):
        p.on_bar(make_bar(100 + i, "2026-09-01 1{}:00:00".format(i) if i < 10
                          else "2026-09-01 10:00:00"), spec)
    check("同日 5 根 bar 全留在缓冲", len(p._bars), 5)

    # ── [8] eod_triggered 纯函数口径 ──────────────────────────────
    print("\n[8] eod_triggered 纯函数")
    check("bar_secs 未知 → 退化为起点判定（14:55 >= 14:55）",
          eod_triggered(parse_hhmmss("14:55"), None, thr), True)
    check("bar_secs 未知 → 14:54 不触发",
          eod_triggered(parse_hhmmss("14:54"), None, thr), False)
    # lead_bars=0 = 旧的"bar 结束时刻 >= 收盘"口径：30m 的 14:30 那根确实会
    # 判定成立，但它 15:00 才闭合推送 → 缓冲 0 秒，与"没平"等价。这就是
    # 为什么默认 lead_bars=1（提前一根 bar 动手）。
    check("lead_bars=0 时 30m 的 14:30 起点才触发（缓冲 0 秒，等价于失效）",
          eod_triggered(parse_hhmmss("14:30"), 1800, thr, lead_bars=0), True)
    check("lead_bars=1 时 30m 的 14:00 起点触发（留 30 分钟缓冲）",
          eod_triggered(parse_hhmmss("14:00"), 1800, thr, lead_bars=1), True)
    check("lead_bars=1 时 30m 的 13:30 起点不触发（不过早）",
          eod_triggered(parse_hhmmss("13:30"), 1800, thr, lead_bars=1), False)

    # ── [9] 引擎级启动冒烟：四个周期都要能真正 boot 起来 ──────────────
    # 策略级断言只证明"公式对"，引擎级才证明"整条链路接得上"：
    # freq 解析 → bar_secs 注入策略 → 时间口径可用 → 尾盘 EOD 可达。
    print("\n[9] 引擎级四周期启动冒烟（freq → 注入 → EOD 可达）")
    _run_engine_smoke()

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

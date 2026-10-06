# -*- coding: utf-8 -*-
"""
出场原因显示文案 + 未平仓浮动估值（Backtest/Test/test_bt09_display_labels.py）
================================================================================
v1.18 的两条**显示契约**（2026-10-05 用户裁定），⑩ 的含浮口径为 2026-10-06 追加：

    ⑴ 出场原因三选一 —— 止损 / 保本 / 跟踪止盈（名称**静态**，不含任何数字）
    ⑵ 未平仓笔显示「截止最新 K 线收盘价的盈亏百分比(xxR)」

为什么这两条值得钉成常驻护栏
---------------------------------------------------------------------
· 文案能"漂"的地方比想象的多：`Backtest/Report.py`（控制台摘要 + 逐笔表）、
  `App/AppBacktest.py`（下发给前端的两张映射）、`Frontend/app.js`（渲染）——
  三处任一处自己抄一份，改一处就漏两处。所以本用例钉的不是"文案长什么样"，
  而是**文案只有一份来源**（`exit_reason_labels()`），且**名称里不带任何数字**
  （2026-10-06 用户裁定去掉「保本(1R)」的括号 ⇒ 阈值怎么调都不会与名字脱节）；
  带数字的解释归 `exit_reason_legend`，两处同源取参。
· 浮动估值最危险的错法不是"算错"，而是**填错槽位**：把估值写进 `net_return` /
  `r_multiple`，它就进了 `Metrics` 的胜率分母（`Metrics` 判据是 `net_return` 的
  符号、只吃 `closed`）⇒"一笔没平的仓位先算进胜率，且符号随最后一根 K 线跳"。
  本用例把"两族字段互不越界"钉在 ⑨，把"估值**只进含浮口径、不进已实现口径**"
  钉在 ⑩（`avg_net_return` 逐字段不变 + `avg_net_return_with_open` 必变，
  两条合起来才构成隔离证明）。

覆盖
---------------------------------------------------------------------
  ① 标签映射三键值 + 键序 ≡ `EXIT_REASON_ORDER`（稳定顺序，前端不必再排一次）
  ② 名称**静态**：`breakeven_trigger_r` 改 1.5 后三选一文案逐字不变、且不含数字
  ③ 图例三条非空、与标签同源取参、且不含 Markdown 强调符（`**` 会原样吐到页面上）
  ③b ★ 图例**双向成立**：回测双向开仓、图例每轮一份 ⇒ 三条都必须同时给出多空两支
      （「跌破」/「入场价 − R」/「抬到上方」这类只对多头成立的写法一律红）
  ④ 引擎 reason 词表 ≡ `EXIT_REASON_ORDER`（穷举 `_phase_reason` 的 `_phase` 输入）
  ⑤ 日线切片实跑：三种原因**都真实出现过**（映射的每个键都有样本覆盖，不是死配置）
  ⑥ 「分原因」摘要行印中文标签（不再出现 `sl:` / `breakeven:` / `trailing:`）
  ⑦ `trades_table` 原因列中文；**未平仓行原因列留空**（状态不冒充原因）
  ⑧ 30m 切片实跑：未平仓笔 `unrealized_*` 与手算逐项一致（价 / R / 净收益率）
  ⑨ ★ 槽位不串：未平仓笔的已实现字段恒 `None`；已平仓笔的 `unrealized_*` 恒 `None`
  ⑩ 估值**不进** `Metrics`：把未平仓笔的 `unrealized_*` 改成 ±999 后重算，指标不变
  ⑪ `RunResult.exit_params` 记录本轮**实际使用**的参数，口径行据此自述（不是常量）
  ⑫ 冻结基线 `snapshots/p0_display_labels.json`（`--freeze` 重冻）

零网络、零 vipdoc 依赖（数据全部来自 `Test/fixtures_real/` 冻结切片；行情源走
`TdxAPI.tdx_data_context` 的 custom 免连网分支）。
跑法：`python Backtest/Test/test_bt09_display_labels.py`（退出码 0/1 即判决；
已登记进 `Test/run_all.py` 的 COMPONENTS）
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKTEST_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIX_DIR = os.path.join(ROOT, "Test", "fixtures_real")
SNAP_DIR = os.path.join(HERE, "snapshots")
SNAP_PATH = os.path.join(SNAP_DIR, "p0_display_labels.json")

# 日线切片：三种 reason 都出现过。30m 切片：末尾留 1 笔未平仓（⑧⑨⑩ 的样本）。
SAMPLE_ALL_REASONS = ("sz002190_d.json", "d")
SAMPLE_OPEN = ("sz002190_30m.json", "30m")

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[PASS] %s" % name)
    else:
        FAIL += 1
        print("[FAIL] %s" % name)
        if detail:
            print("        " + str(detail).replace("\n", "\n        "))


def _run(fixture, freq):
    """跑一份冻结切片 → `(RunResult, Metrics, records)`。"""
    from Backtest.Metrics import compute
    from Backtest.Runner import load_records, run

    recs = load_records(os.path.join(FIX_DIR, fixture))
    res = run("sz", "002190", freq, records=recs)
    return res, compute(res), recs


def _r(x, nd):
    return None if x is None else round(float(x), nd)


def build_snapshot():
    """现算一份快照（`--freeze` 写盘 / 无参时比对）。"""
    from Backtest.Report import (EXIT_REASON_ORDER, exit_reason_labels,
                                 exit_reason_legend, resolved_exit_params)
    from Backtest.Report import summary_text, trades_table

    res_d, met_d, _ = _run(*SAMPLE_ALL_REASONS)
    res_m, met_m, recs_m = _run(*SAMPLE_OPEN)

    by_reason = {k: int(v["n"]) for k, v in met_d.by_reason.items()}
    open_tr = res_m.still_open[0] if res_m.still_open else None

    return {
        "kind": "backtest-display-labels",
        "version": 1,
        "note": ("出场原因显示文案（三选一）与未平仓浮动估值的冻结基线。"
                 "改 Backtest/Report.py 的文案或 Runner._mark_open_positions 的估值口径时，"
                 "先看 diff 是不是你**故意**改的那件事，再跑 --freeze。"),
        "exit_reason_order": list(EXIT_REASON_ORDER),
        "labels": exit_reason_labels(),
        "legend": exit_reason_legend(),
        "resolved_exit_params": resolved_exit_params(),
        "d": {
            "fixture": SAMPLE_ALL_REASONS[0],
            "by_reason": by_reason,
            "n": int(met_d.n),
            "open": int(met_d.u),
            "labels_in_trade_order": [
                exit_reason_labels().get(str(t.exit_reason), str(t.exit_reason))
                for t in res_d.trades],
            "summary_last_lines": summary_text(res_d, met_d).splitlines()[-2:],
        },
        "m30": {
            "fixture": SAMPLE_OPEN[0],
            "last_close": _r(recs_m[-1]["close"], 6),
            "n": int(met_m.n),
            "open": int(met_m.u),
            "open_trade": None if open_tr is None else {
                "trade_id": int(open_tr.trade_id),
                "side": str(open_tr.side),
                "entry_price": _r(open_tr.entry_price, 6),
                "r_distance": _r(open_tr.r_distance, 6),
                "unrealized_price": _r(open_tr.unrealized_price, 6),
                "unrealized_r": _r(open_tr.unrealized_r, 6),
                "unrealized_net_return": _r(open_tr.unrealized_net_return, 10),
            },
        },
    }


def diff(got, want, path="", out=None):
    if out is None:
        out = []
    if len(out) > 40:
        return out
    if isinstance(want, dict) and isinstance(got, dict):
        for k in sorted(set(want) | set(got)):
            if k not in got:
                out.append("%s.%s 缺失（基线有 %r）" % (path, k, want[k]))
            elif k not in want:
                out.append("%s.%s 多出（基线无 %r）" % (path, k, got[k]))
            else:
                diff(got[k], want[k], "%s.%s" % (path, k), out)
    elif isinstance(want, list) and isinstance(got, list):
        if len(got) != len(want):
            out.append("%s 长度 %d ≠ 基线 %d" % (path, len(got), len(want)))
        for i in range(min(len(got), len(want))):
            diff(got[i], want[i], "%s[%d]" % (path, i), out)
    elif got != want:
        out.append("%s got=%r 基线=%r" % (path, got, want))
    return out


def part_labels():
    """① ② ③ ④：文案映射本身（不跑回测）。"""
    print("══ ①②③④ 出场原因文案（三选一 / 1R 动态 / 图例 / 引擎词表）══")
    from Backtest.Report import (EXIT_REASON_ORDER, exit_reason_labels,
                                 exit_reason_legend, resolved_exit_params)
    from Backtest.Report import sort_exit_reasons

    lab = exit_reason_labels()
    check("① 三选一文案 = 止损 / 保本 / 跟踪止盈（名称里**不带**数字，2026-10-06）",
          lab == {"sl": "止损", "breakeven": "保本", "trailing": "跟踪止盈"},
          "labels=%r" % lab)
    check("① 键序 ≡ EXIT_REASON_ORDER（稳定顺序，前端不必再排）",
          list(lab) == list(EXIT_REASON_ORDER) == ["sl", "breakeven", "trailing"],
          "keys=%r" % list(lab))
    check("① sort_exit_reasons：已知键按序、未知键不丢（排在其后）",
          sort_exit_reasons(["trailing", "zzz", "sl", "aaa", "breakeven"])
          == ["sl", "breakeven", "trailing", "aaa", "zzz"])

    # ② 名称是**静态词**（2026-10-06 用户裁定：去掉「保本(1R)」的括号）——
    #    名称里没有数字，就不存在"阈值改了、名字里的数字没跟着改"这种漂移；
    #    带数字的解释全在 `exit_reason_legend`（下面 ③ 段单独钉）。
    lab15 = exit_reason_labels({"breakeven_trigger_r": 1.5, "win_loss_ratio": 3.0})
    check("② 换过阈值后三选一文案**逐字不变**（名称不消费任何参数）",
          lab15 == lab, "lab15=%r lab=%r" % (lab15, lab))
    check("② 名称里不含任何数字（`保本(1R)` / `保本(1.5R)` 都不许回来）",
          not any(ch.isdigit() for v in lab.values() for ch in v),
          "labels=%r" % lab)
    check("② 空表 / None 都按「未指定」→ 回落 STOCK_EXIT_PARAMS",
          resolved_exit_params({}) == resolved_exit_params(None) == resolved_exit_params(),
          "none=%r empty=%r" % (resolved_exit_params(None), resolved_exit_params({})))
    check("② resolved_exit_params 四项 R 口径齐全且与默认一致",
          resolved_exit_params() == {"win_loss_ratio": 3.0, "trailing_trigger_r": 1.0,
                                     "breakeven_trigger_r": 1.0, "breakeven_buffer_r": 0.5},
          "got=%r" % resolved_exit_params())

    # ③ 图例：非空、与标签同源取参、无 Markdown 强调符
    lg = exit_reason_legend()
    check("③ 图例三键齐全且逐条非空",
          set(lg) == set(EXIT_REASON_ORDER) and all(str(v).strip() for v in lg.values()),
          "legend=%r" % lg)
    check("③ 图例不含 Markdown 强调符（`**` 在页面上会原样吐出来）",
          not any("**" in v for v in lg.values()), "legend=%r" % lg)
    lg15 = exit_reason_legend({"breakeven_trigger_r": 1.5, "win_loss_ratio": 3.0})
    check("③ 图例**消费参数**（阈值改了图例跟着改 —— 数字现在只住在图例里）",
          "1.5R" in lg15["breakeven"],
          "legend=%r" % lg15["breakeven"])
    check("③ 图例与标签**分工**：名称静态（②已钉）、带数字的解释全在图例",
          "1R" not in lab["breakeven"] and "1.5R" in lg15["breakeven"],
          "label=%r legend=%r" % (lab["breakeven"], lg15["breakeven"]))
    check("③ 图例把「保本保护线位置 = 入场价 ± 0.5R」写出来了（1R 是阈值、不是线位）",
          "0.5R" in lg["breakeven"], "legend=%r" % lg["breakeven"])

    # ③b ★ 图例**双向成立**（2026-10-06 追加）：回测是**双向开仓**的（`m30` 切片末尾
    #     那笔未平仓就是 `side=short`），而图例**每轮一份**、不区分多空 ⇒ 只写
    #     「跌破」「入场价 − R」「抬到入场价上方」这类多头口径的文案，对着空头持仓读就是错的。
    #     下面四条把「三条必须同时含多空」钉死 —— 只改标题、漏改正文的漂移会立刻红。
    check("③b ★ 图例三条都同时写了多空两支（不许只对多头成立）",
          all(("多头" in v and "空头" in v) for v in lg.values()), "legend=%r" % lg)
    check("③b ★ 触发动词双向：多空各自的「跌破 / 涨破」都出现（旧文案只有「跌破」）",
          "跌破" in lg["sl"] and "涨破" in lg["sl"], "sl=%r" % lg["sl"])
    check("③b ★ sl 图例同时给出两支保护价（多头 入场价 − R、空头 入场价 + R）",
          "多头 入场价 − R" in lg["sl"] and "空头 入场价 + R" in lg["sl"],
          "sl=%r" % lg["sl"])
    check("③b ★ 保本线两侧随方向翻转（多空各取相反的一侧，不共用同一个词）",
          ("多头在入场价上方" in lg["breakeven"]
           and "空头在入场价下方" in lg["breakeven"])
          or ("多头在入场价下方" in lg["breakeven"]
              and "空头在入场价上方" in lg["breakeven"]),
          "breakeven=%r" % lg["breakeven"])
    check("③b ★ 保本图例用双向动词「收紧到」（「抬到」只对多头成立）",
          "收紧到" in lg["breakeven"] and "抬到" not in lg["breakeven"],
          "breakeven=%r" % lg["breakeven"])

    # ④ 引擎词表 ≡ 报告层映射键 —— 引擎哪天多一相而报告层没跟，这里立刻红
    from Trading.Infra.Records import ExitPlan
    from Trading.Strategy.Exit import LayeredExitPolicy
    probes = {"": "sl", "sl": "sl", "breakeven": "breakeven", "trailing": "trailing",
              "unknown_phase": "sl"}
    got = {v: LayeredExitPolicy._phase_reason(
        ExitPlan("LayeredExitPolicy", 1.0, params=({} if v == "" else {"_phase": v})))
        for v in probes}
    check("④ _phase_reason 的取值域 ≡ EXIT_REASON_ORDER（穷举 _phase 输入）",
          set(got.values()) == set(EXIT_REASON_ORDER), "probes→%r" % got)
    check("④ 未抬过价的持仓回落 `sl`（初始止损），不留空串",
          got[""] == "sl" and got["unknown_phase"] == "sl", "got=%r" % got)
    return lab


def part_trades(lab):
    """⑤ ⑥ ⑦ ⑪：日线切片实跑 —— 三种原因都出现 / 摘要与逐笔表的中文。"""
    print("══ ⑤⑥⑦⑪ 日线切片实跑（三种原因覆盖 / 摘要 / 逐笔表）══")
    from Backtest.Report import EXIT_REASON_ORDER, summary_text, trades_table

    res, met, _ = _run(*SAMPLE_ALL_REASONS)
    reasons = sorted({str(t.exit_reason) for t in res.closed})
    check("⑤ 日线切片三种原因**都真实出现过**（映射每个键都有样本覆盖）",
          set(reasons) == set(lab) == set(EXIT_REASON_ORDER), "reasons=%r" % reasons)
    check("⑤ 未平仓 0 笔（该切片跑完刚好全平 —— ⑧⑨⑩ 另用 30m 切片）",
          met.u == 0 and len(res.closed) == met.n, "u=%r n=%r" % (met.u, met.n))

    txt = summary_text(res, met)
    line = [x for x in txt.splitlines() if x.startswith("分原因")]
    check("⑥ 摘要「分原因」行存在且印中文标签",
          len(line) == 1 and "止损" in line[0] and "跟踪止盈" in line[0]
          and "保本" in line[0], "line=%r" % line)
    check("⑥ 摘要「分原因」行**不再**出现英文标识符（sl:/breakeven:/trailing:）",
          not any(k + ":" in line[0] for k in EXIT_REASON_ORDER), "line=%r" % line)
    check("⑥ 摘要里三种原因各出现一次（三选一，无遗漏无重复）",
          all(line[0].count(x) == 1 for x in ("止损", "保本", "跟踪止盈")),
          "line=%r" % line)
    check("⑥ 摘要行里**没有**旧写法「保本(1R)」（名称已去掉括号里的数字）",
          "保本(1R)" not in line[0], "line=%r" % line)

    tbl = trades_table(res)
    head = tbl.splitlines()[0]
    check("⑦ 逐笔表表头含「原因」（列仍在，只是值中文化）", "原因" in head, "head=%r" % head)
    check("⑦ 逐笔表不再出现英文 reason（全表零 `sl` / `breakeven` / `trailing`）",
          not any(k in tbl for k in EXIT_REASON_ORDER), "tbl=%r" % tbl[:200])
    check("⑦ 逐笔表每笔都印出了对应中文原因",
          all(lab[str(t.exit_reason)] in tbl for t in res.closed), "tbl=%r" % tbl)

    # ⑪ 口径行印的是**本轮实际用的**参数（`RunResult.exit_params`），不是常量
    check("⑪ RunResult.exit_params 记录了本轮实际使用的出场参数",
          res.exit_params == {"win_loss_ratio": 3.0, "trailing_trigger_r": 1.0},
          "exit_params=%r" % res.exit_params)
    from Backtest.Report import caliber_lines
    ep_line = caliber_lines(res)[1]
    check("⑪ 口径行「出场参数」两支与 exit_params 一致",
          "win_loss_ratio=3.0" in ep_line and "trailing_trigger_r=1.0" in ep_line,
          "line=%r" % ep_line)
    return res, met


def part_open(lab):
    """⑧ ⑨ ⑩：30m 切片 —— 未平仓浮动估值的正确性与槽位隔离。"""
    print("══ ⑧⑨⑩ 30m 切片（未平仓浮动估值 / 槽位不串 / 不进指标）══")
    from Backtest.ExitParams import round_trip_cost
    from Backtest.Metrics import compute
    from Backtest.Report import EXIT_REASON_ORDER, trades_table

    res, met, recs = _run(*SAMPLE_OPEN)
    opens = res.still_open
    check("⑧ 该切片末尾留 1 笔未平仓（样本前提，不是断言结果本身）",
          len(opens) == 1, "open=%d closed=%d" % (len(opens), met.n))

    last_close = float(recs[-1]["close"])
    t = opens[0]
    sign = 1 if str(t.side) == "long" else -1
    entry = float(t.entry_price)
    R = float(t.r_distance)
    exp_r = round((last_close - entry) * sign / R, 4)
    gross = (last_close - entry) * sign / entry
    notional = float(t.shares) * entry
    exp_net = gross - round_trip_cost(entry, last_close, sign, t.shares) / notional

    check("⑧ unrealized_price == 最后一根 K 线收盘价（估值锚点唯一）",
          t.unrealized_price == last_close, "got=%r want=%r" % (t.unrealized_price, last_close))
    check("⑧ unrealized_r 与手算 (last−entry)·sign/R 一致",
          t.unrealized_r == exp_r, "got=%r want=%r" % (t.unrealized_r, exp_r))
    check("⑧ unrealized_net_return 与手算「按该价平仓估」一致（同一套成本函数）",
          abs(t.unrealized_net_return - exp_net) < 1e-12,
          "got=%r want=%r" % (t.unrealized_net_return, exp_net))
    check("⑧ 净收益率 < 毛收益率（成本恒为正，估值不是纯价格比）",
          t.unrealized_net_return < gross, "net=%r gross=%r" % (t.unrealized_net_return, gross))
    check("⑧ unrealized_r 与浮盈同号（符号口径自洽）",
          (t.unrealized_r > 0) == ((last_close - entry) * sign > 0),
          "r=%r px=%r entry=%r" % (t.unrealized_r, last_close, entry))

    # ⑨ ★ 槽位隔离：两族字段互不越界
    realized_fields = ("r_multiple", "gross_return", "net_return", "cost_cash",
                       "exit_reason", "exit_date", "bars_held")
    bad_open = {f: getattr(t, f) for f in realized_fields if getattr(t, f) is not None}
    check("⑨ ★ 未平仓笔的**已实现**字段恒为 None（估值不许写进成交结果槽位）",
          not bad_open, "脏字段=%r" % bad_open)
    bad_closed = [(x.trade_id, x.unrealized_price, x.unrealized_r, x.unrealized_net_return)
                  for x in res.closed
                  if x.unrealized_price is not None or x.unrealized_r is not None
                  or x.unrealized_net_return is not None]
    check("⑨ ★ 已平仓笔的**浮动**字段恒为 None（两族字段不共用槽位）",
          not bad_closed, "脏笔=%r" % bad_closed[:3])
    check("⑨ open_ 标志与属性分区一致（closed/still_open 就是那两族的分界）",
          all(x.open_ for x in opens) and all(not x.open_ for x in res.closed))

    # 未平仓行：原因列留空（状态不冒充原因），浮动列给出 R
    tbl = trades_table(res)
    open_line = [x for x in tbl.splitlines() if "(未平仓)" in x]
    check("⑨ 逐笔表未平仓行有且只有 1 行，且原因列留空",
          len(open_line) == 1
          and not any(k in open_line[0] for k in EXIT_REASON_ORDER),
          "line=%r" % open_line)
    check("⑨ 逐笔表未平仓行印出浮动 R（%.4f）" % t.unrealized_r,
          ("%.4f" % t.unrealized_r) in open_line[0], "line=%r" % open_line)

    # ⑩ 估值**只进含浮那一族**，不进任何已实现口径指标
    #    2026-10-06 把口径拆成两个字段之后，这条护栏的判别力反而更强：
    #    `avg_net_return`（已实现）必须**不变**，`avg_net_return_with_open`（含浮）
    #    必须**跟着变**。只证前者 = 漏掉新口径；只证后者 = 没证明隔离。
    before = compute(res)
    t.unrealized_r, t.unrealized_net_return = 999.0, -999.0
    after = compute(res)
    t.unrealized_r, t.unrealized_net_return = exp_r, exp_net
    fields = ("n", "w", "l", "e", "u", "win_rate", "expectancy_r", "avg_net_return",
              "profit_loss_ratio", "profit_factor", "avg_bars_held",
              "max_win_r", "max_loss_r")
    check("⑩ ★ 把未平仓笔的 unrealized_* 改成 ±999，**已实现**口径逐字段不变",
          all(getattr(before, f) == getattr(after, f) for f in fields)
          and before.by_reason == after.by_reason
          and before.by_bsp_type == after.by_bsp_type,
          {f: (getattr(before, f), getattr(after, f)) for f in fields
           if getattr(before, f) != getattr(after, f)})
    check("⑩ ★ 同一改动下**含浮**口径必须跟着变（证它真的含浮，不是摆设）",
          before.avg_net_return_with_open is not None
          and after.avg_net_return_with_open is not None
          and before.avg_net_return_with_open != after.avg_net_return_with_open,
          "before=%r after=%r" % (before.avg_net_return_with_open,
                                  after.avg_net_return_with_open))
    check("⑩ 含浮口径 ≡ (Σ已平 + Σ未平浮动) / (已平 + 未平) —— 两个字段各自算对",
          before.avg_net_return is not None
          and abs(before.avg_net_return_with_open
                  - (before.avg_net_return * met.n + exp_net) / (met.n + 1)) < 1e-12,
          "with_open=%r realized=%r n_closed=%r open_net=%r" % (
              before.avg_net_return_with_open, before.avg_net_return, met.n, exp_net))
    check("⑩ 指标分母只含已平仓笔（未平仓不计胜负）",
          before.n == len(res.closed) and before.u == len(opens)
          and before.w + before.l + before.e == before.n,
          "n=%r u=%r" % (before.n, before.u))
    return res, met


def main(argv):
    freeze = "--freeze" in (argv or sys.argv[1:])

    print("=" * 68)
    print("出场原因显示文案 + 未平仓浮动估值（test_bt09_display_labels.py）")
    print("=" * 68)

    lab = part_labels()
    part_trades(lab)
    part_open(lab)

    print("──── ⑫ 冻结基线 ────")
    got = build_snapshot()
    if freeze:
        os.makedirs(SNAP_DIR, exist_ok=True)
        with io.open(SNAP_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(got, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print("[FREEZE] 基线已写入 %s" % os.path.relpath(SNAP_PATH, ROOT))
        print("         labels=%r" % got["labels"])
        print("         d.by_reason=%r  m30.open=%r" % (got["d"]["by_reason"], got["m30"]["open"]))
        return 0

    if not os.path.exists(SNAP_PATH):
        check("⑫ 基线文件存在", False,
              "缺 %s —— 先跑 --freeze" % os.path.relpath(SNAP_PATH, ROOT))
        return 1
    with io.open(SNAP_PATH, encoding="utf-8") as f:
        want = json.load(f)
    d = diff(got, want, "")
    check("⑫ 文案 / 口径 / 逐笔标签 / 未平仓估值 与基线一致", not d, "\n".join(d))

    print("-" * 68)
    print("合计 %d 项，通过 %d，失败 %d" % (PASS + FAIL, PASS, FAIL))
    if FAIL:
        print("\n提示：若差异正是你**本轮故意**改的那件事，跑 "
              "`python Backtest/Test/test_bt09_display_labels.py --freeze` 重冻，"
              "并在交付说明里写明改了哪一项。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

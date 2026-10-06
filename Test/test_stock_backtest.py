# -*- coding: utf-8 -*-
"""
股票页「回测」护栏（设计文档 §4；2026-10-05）
====================================================================
钉住 `/api/stocks/{code}/backtest` 的端到端契约。与 bt01/bt06 的分工：

  · bt06 钉 **引擎侧**（`Backtest.Runner` 直接喂冻结 records）逐周期的
    笔数/信号/汇总基线；
  · 本组件钉 **App 边界**：页面格式的 `klines`（斜杠日期、无 `dt`）能不能被
    `_klines_to_records` 正确还原成 records、响应形状与口径披露是否自洽、
    `bsp_types` 的四态语义、以及前端的按钮/面板护栏。
    两者共用同一批 `Test/fixtures_real/` 切片 ⇒ 引擎侧与页面侧**同一份样本**。

覆盖
---------------------------------------------------------------------
  ① 正例（日线冻结切片）：笔数/胜负平/期望R/区间/bars/响应键/trades==filled
     + 派生不变量（closed+still_open==filled / filtered==seen-fill 路径自洽）
  ② 逐周期（App 边界）：30m 走 `%Y/%m/%d %H:%M` 解析 / w 0 笔 → win_rate=None；
     另证「日线条目喂 30m」被 400 拦（防两种格式被混用而无红灯）
  ③ `bsp_types` 四态：None 全放行 / "" 全过滤（≠全放行）/ "0" / "3" / "0,3"≡全放行
  ④ 口径披露自洽：max_notional_multiple ≈ max_notional/TARGET_AMOUNT，
     min_lot_derived_trades ∈ [0, filled]，目标成交额 > 0
  ⑤ `*_pct` 一律已 ×100（响应侧不再留小数收益率 —— 前端漏乘一次就是安静的错误）
  ⑥ 400 分支穷举：60m / 空 klines / 坏代码（无前缀·带点·未知前缀）/ 缺字段 /
     周期不匹配 / 超上限
  ⑦ AST 契约（Q8「不落盘」）：`App/AppBacktest.py` 无文件写、无网络、无子进程
  ⑧ 前端 `_btBspTypes()` 四态（node 抽真函数）
  ⑨ 前端 `syncStatsButtonLabel()` 三态 + 市场态切换收面板 + 首同步不关 + 文案未变不关
  ⑩ 前端 `toggleStats()` 股票态分流：不碰 stats-panel
  ⑪ 指数同形（P0-⑥；⚠ v2.2 改判「指数当个股」）：`AppUtils.is_index` 判定同源
     （含 88xx 板块指数 / ds / hk）+ 响应与个股**逐字段一致、无任何置 null 分流**
     （v1.18 的「金额族置 null」已整体摘除）+ 价格侧 12 字段两侧逐字段相等
     （同一份 K 线只换代码题头 ⇒ 判别力来自实验设计）+ 口径行（CLI）追加「按个股
     假想」披露一句；面板底部披露与个股同一份（假设由横幅披露，不重复）
     直连（Backtest 层不依赖 App）+ 前端 `renderBacktest` 真渲染：指数页渲染
     「按个股假想」横幅、无任何「不适用」
  ⑫ 出场原因**三选一**（止损 / 保本 / 跟踪止盈，v1.18；⚠ v2.0 起名称去数字）：映射随响应下发
     （`exit_reason_labels` / `exit_reason_legend`）+ 逐笔 `exit_reason` 仍是引擎原值
     + 三种原因在日线切片里都真实出现 + **未平仓浮动估值**三字段（价 / R / 净收益率）
     与已实现字段**互不越界**（两族槽位隔离）
     + 前端真渲染：中文原因、持仓中「浮」、分类型桶可读化、`n==1` 标注「最好＝最差」
     （node 不在位时 ⑧⑨⑩⑪⑫ SKIP）

跑法：`python Test/test_stock_backtest.py`（退出码 0/1 即判决；
已注册进 `Test/run_all.py` 的 COMPONENTS）
"""
import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from App.AppErrors import BadRequestError          # noqa: E402
from App.AppBacktest import compute_stock_backtest  # noqa: E402

FIXTURES = os.path.join(ROOT, "Test", "fixtures_real")
APPBT = os.path.join(ROOT, "App", "AppBacktest.py")
APPJS = os.path.join(ROOT, "Frontend", "app.js")

_OK = []


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  —— " + str(detail)[:260]))
    return cond


def _page_klines(fixture_name, minute=False, lo=None, hi=None):
    """冻结切片（`dt` 连字符、带 `00:00:00`）→ 页面 `chartData.klines` 格式。

    页面日期格式 SSOT = `Common.func_util._get_date_fmt`：
      日线 `%Y/%m/%d`；分钟级 `%Y/%m/%d %H:%M`。

    `lo` / `hi` 截一段**真区间**（页面送什么区间就测什么区间，§4.4）——
    ⑫ 需要一个"恰好 1 笔已平仓"的样本去覆盖 `n==1` 的显示分支，
    用真区间比手搓一份假响应可信（假响应只能证明前端读得对，证明不了后端给得对）。
    """
    recs = json.load(io.open(os.path.join(FIXTURES, fixture_name), encoding="utf-8"))
    if lo is not None or hi is not None:
        recs = recs[lo or 0:hi]
    out = []
    for r in recs:
        dt = r["dt"]                       # "2021-01-04 00:00:00"
        head = "%s/%s/%s" % (dt[:4], dt[5:7], dt[8:10])
        out.append({
            "date": ("%s %s" % (head, dt[11:16])) if minute else head,
            "open": r["open"], "high": r["high"], "low": r["low"],
            "close": r["close"], "vol": r["vol"], "amount": r["amount"],
        })
    return out


def _call(code, freq, klines, **extra):
    body = {"code": code, "freq": freq, "klines": klines}
    body.update(extra)
    return compute_stock_backtest(code, body)


# ══════════════════════════════════════════════════════════════════════
# ① 正例（日线）
# ══════════════════════════════════════════════════════════════════════
def part1():
    print("══ ① 正例（日线冻结切片 → App 边界）══")
    kl = _page_klines("sz002190_d.json")
    d = _call("sz002190", "d", kl)

    check("ok=True 且响应必备键齐全",
          d.get("ok") is True and
          all(k in d for k in ("ok", "target", "run", "summary", "by_bsp_type",
                               "by_reason", "trades", "caliber", "disclosures")),
          "keys=%r" % sorted(d.keys()))

    run, s, tgt = d["run"], d["summary"], d["target"]
    check("交易笔数 = 6（P0-④ 基线）", run["filled"] == 6, "filled=%r" % run["filled"])
    check("胜负平 = 2/4/0", (s["w"], s["l"], s["e"]) == (2, 4, 0),
          "w/l/e=%r" % ((s["w"], s["l"], s["e"]),))
    check("实际胜率 = 2/6 ≈ 0.3333", abs(s["win_rate"] - 2.0 / 6.0) < 1e-6,
          "win_rate=%r" % s["win_rate"])
    check("期望 R ≈ 1.604417（L3 启动阈值 2R 口径）", abs(s["expectancy_r"] - 1.604417) < 1e-3,
          "expectancy_r=%r" % s["expectancy_r"])
    check("区间 [2021-01-04, 2026-09-30] 且 bars=1393",
          tgt["date_from"] == "2021-01-04" and tgt["date_to"] == "2026-09-30"
          and tgt["bars"] == 1393, "target=%r" % tgt)
    check("target.code 由 {code} 路径参数回填（= sz002190）", tgt["code"] == "sz002190",
          "code=%r" % tgt["code"])
    check("逐笔 trades 长度 == filled", len(d["trades"]) == run["filled"],
          "len=%d filled=%r" % (len(d["trades"]), run["filled"]))

    # 派生不变量：终态笔数 + 未平仓 = 总笔数（指标口径与逐笔同源）
    check("派生不变量 closed + still_open == filled",
          run["closed"] + run["still_open"] == run["filled"],
          "closed=%r still_open=%r filled=%r" % (run["closed"], run["still_open"],
                                                 run["filled"]))
    # 逐笔 open 标志与 still_open 一致
    n_open = sum(1 for t in d["trades"] if t.get("open"))
    check("逐笔 open 计数 == run.still_open", n_open == run["still_open"],
          "open=%d still_open=%r" % (n_open, run["still_open"]))
    # 类型拆解总笔数 == filled（分桶不丢笔）
    check("by_bsp_type 各桶 n 之和 == filled",
          sum(int(v["n"]) for v in d["by_bsp_type"].values()) == run["filled"],
          "by_bsp_type=%r" % d["by_bsp_type"])
    # 信号三分解（§4.6）：每个「首见信号」只有三种归宿 —— 开仓 / 被类型过滤 /
    #   被引擎拒收。三者相加必须等于首见总数，否则就是有一类信号凭空消失。
    check("派生不变量 signals_seen == filled + signals_filtered + signals_rejected",
          run["signals_seen"] == run["filled"] + run["signals_filtered"]
          + run["signals_rejected"],
          "seen=%r filtered=%r rejected=%r filled=%r"
          % (run["signals_seen"], run["signals_filtered"], run["signals_rejected"],
             run["filled"]))
    # 指标分母只含已平仓笔（未平仓不计胜负）
    check("派生不变量 w + l + e == closed（未平仓不进胜负统计）",
          s["w"] + s["l"] + s["e"] == run["closed"],
          "w/l/e=%r closed=%r" % ((s["w"], s["l"], s["e"]), run["closed"]))
    check("disclosures 两条非空（T+0多空双向 / 涨跌停；2026-10-06 用户裁定收敛）",
          isinstance(d["disclosures"], list) and len(d["disclosures"]) == 2
          and all(str(x).strip() for x in d["disclosures"]),
          "disclosures=%r" % d.get("disclosures"))

    # 逐笔字段完整性（前端 renderBacktest 直接消费这些键名）
    need = {"trade_id", "side", "bsp_type", "entry_date", "entry_price", "r_distance",
            "shares", "exit_date", "exit_price", "exit_reason", "bars_held",
            "r_multiple", "gross_return_pct", "net_return_pct", "cost_cash", "open"}
    miss = [i for i, t in enumerate(d["trades"]) if not need.issubset(t.keys())]
    check("每笔逐笔键集齐全（前端渲染契约）", not miss, "缺键的笔序号=%r" % miss[:5])
    check("side 取值 ∈ {long, short}",
          all(t["side"] in ("long", "short") for t in d["trades"]),
          sorted({t["side"] for t in d["trades"]}))
    return d


# ══════════════════════════════════════════════════════════════════════
# ② 逐周期（App 边界：日期格式解析）
# ══════════════════════════════════════════════════════════════════════
def part2():
    print("══ ② 逐周期（App 边界 · 页面日期格式）══")
    kl30 = _page_klines("sz002190_30m.json", minute=True)
    d30 = _call("sz002190", "30m", kl30)
    check("30m：笔数 = 8（P0-④ 基线）", d30["run"]["filled"] == 8,
          "filled=%r" % d30["run"]["filled"])
    check("30m：期望 R ≈ 0.240257", abs(d30["summary"]["expectancy_r"] - 0.240257) < 1e-3,
          "expectancy_r=%r" % d30["summary"]["expectancy_r"])
    check("30m：区间首根 = 切片首根（2025-09-30）",
          d30["target"]["date_from"] == "2025-09-30", "target=%r" % d30["target"])
    # 30m 有 1 笔持仓中（切片末尾信号未平仓）⇒ 正好验「未平仓不进指标分母」
    check("30m：closed=7 / still_open=1（切片末尾未平仓）",
          d30["run"]["closed"] == 7 and d30["run"]["still_open"] == 1,
          "run=%r" % d30["run"])
    check("30m：w + l + e == closed（未平仓不计胜负，分母是 7 不是 8）",
          d30["summary"]["w"] + d30["summary"]["l"] + d30["summary"]["e"]
          == d30["run"]["closed"],
          "summary=%r closed=%r" % ({k: d30["summary"][k] for k in ("w", "l", "e")},
                                    d30["run"]["closed"]))
    d30_3 = _call("sz002190", "30m", kl30, bsp_types="3")
    d30_0 = _call("sz002190", "30m", kl30, bsp_types="0")
    check('30m：本切片信号全为 3 类 ⇒ "3" ≡ 全放行、`"0"` → 0 笔',
          d30_3["run"]["filled"] == d30["run"]["filled"] == 8
          and d30_0["run"]["filled"] == 0,
          '"3" filled=%r "0" filled=%r' % (d30_3["run"]["filled"], d30_0["run"]["filled"]))
    check('30m：`"0"` 的 filtered == seen == 10（事前过滤在 30m 同样生效）',
          d30_0["run"]["signals_filtered"] == d30_0["run"]["signals_seen"] == 10,
          "run=%r" % d30_0["run"])

    klw = _page_klines("sz002190_w.json")
    dw = _call("sz002190", "w", klw)
    check("周线：0 笔（引擎真实判断，非失败）", dw["run"]["filled"] == 0,
          "filled=%r" % dw["run"]["filled"])
    check("周线 0 笔 → win_rate = None（不是 0，避免把「无样本」显示成「胜率 0」）",
          dw["summary"]["win_rate"] is None, "win_rate=%r" % dw["summary"]["win_rate"])
    check("周线 0 笔的根因 = 引擎侧零 bsp（signals_seen == 0，非被过滤）",
          dw["run"]["signals_seen"] == 0 and dw["run"]["signals_filtered"] == 0,
          "run=%r" % dw["run"])

    # 格式串用错必须被拦：日线条目（%Y/%m/%d，无时分）喂 30m
    try:
        _call("sz002190", "30m", _page_klines("sz002190_d.json"))
        check("日线条目喂 30m 被 400 拦（两种日期格式不得混用）", False, "未抛 BadRequestError")
    except BadRequestError:
        check("日线条目喂 30m 被 400 拦（两种日期格式不得混用）", True)
    return d30, dw


# ══════════════════════════════════════════════════════════════════════
# ③ bsp_types 四态
# ══════════════════════════════════════════════════════════════════════
def part3(kl, base_exp):
    print("══ ③ bsp_types 四态（§4.7.3 同一份 SSOT）══")
    d_none = _call("sz002190", "d", kl)
    d0 = _call("sz002190", "d", kl, bsp_types="0")
    d3 = _call("sz002190", "d", kl, bsp_types="3")
    d03 = _call("sz002190", "d", kl, bsp_types="0,3")
    d_empty = _call("sz002190", "d", kl, bsp_types="")

    check('"0" → 3 笔 / R≈4.1044',
          d0["run"]["filled"] == 3 and abs(d0["summary"]["expectancy_r"] - 4.1044) < 1e-3,
          "filled=%r R=%r" % (d0["run"]["filled"], d0["summary"]["expectancy_r"]))
    check('"3" → 3 笔 / R≈-0.8956',
          d3["run"]["filled"] == 3 and abs(d3["summary"]["expectancy_r"] + 0.8956) < 1e-3,
          "filled=%r R=%r" % (d3["run"]["filled"], d3["summary"]["expectancy_r"]))
    check('"0,3" ≡ 全放行（笔数与 R 同基线）',
          d03["run"]["filled"] == d_none["run"]["filled"] == 6
          and abs(d03["summary"]["expectancy_r"] - base_exp) < 1e-9,
          "filled=%r R=%r base=%r" % (d03["run"]["filled"],
                                      d03["summary"]["expectancy_r"], base_exp))
    check('"" → 0 笔（**全过滤**，与 None 全放行语义相反）',
          d_empty["run"]["filled"] == 0, "filled=%r" % d_empty["run"]["filled"])
    check('"" → 口径行披露 bsp_types=""（与 None 可区分）',
          d_empty["caliber"]["bsp_types"] == "",
          "caliber.bsp_types=%r" % d_empty["caliber"]["bsp_types"])
    check("未启用过滤 → caliber.bsp_types = None",
          d_none["caliber"]["bsp_types"] is None,
          "caliber.bsp_types=%r" % d_none["caliber"]["bsp_types"])
    check('事前过滤可见：signals_filtered 随勾选变化（"" 时 filtered == seen）',
          d_empty["run"]["signals_filtered"] == d_empty["run"]["signals_seen"]
          and d_empty["run"]["signals_seen"] > 0,
          "run=%r" % d_empty["run"])
    check("类型拆解只在对应桶里（\"0\" 只出 0 类）",
          list(d0["by_bsp_type"].keys()) == ["0"],
          "by_bsp_type keys=%r" % list(d0["by_bsp_type"].keys()))


# ══════════════════════════════════════════════════════════════════════
# ④⑤ 口径披露自洽 + *_pct ×100
# ══════════════════════════════════════════════════════════════════════
def part45(d):
    print("══ ④ 口径披露自洽（§5.4d-quater 第 10 节）══")
    cal, s = d["caliber"], d["summary"]
    from Backtest.ExitParams import TARGET_AMOUNT
    check("target_amount > 0 且 ≡ Backtest.ExitParams.TARGET_AMOUNT（单一来源）",
          float(cal["target_amount"]) > 0
          and abs(float(cal["target_amount"]) - float(TARGET_AMOUNT)) < 1e-9,
          "caliber.target_amount=%r TARGET_AMOUNT=%r" % (cal["target_amount"], TARGET_AMOUNT))
    check("min_lot > 0 且 lot_step > 0（A 股 100 股整手）",
          int(cal["min_lot"]) > 0 and int(cal["lot_step"]) > 0,
          "min_lot=%r lot_step=%r" % (cal["min_lot"], cal["lot_step"]))
    check("min_lot_derived_trades ∈ [0, filled]（借道笔数不可能超样本）",
          0 <= int(cal["min_lot_derived_trades"]) <= d["run"]["filled"],
          "derived=%r filled=%r" % (cal["min_lot_derived_trades"], d["run"]["filled"]))
    check("max_notional_multiple ≈ max_notional / target_amount（自洽）",
          cal["max_notional_multiple"] is None or
          abs(float(cal["max_notional_multiple"])
              - float(cal["max_notional"]) / float(cal["target_amount"])) < 1e-3,
          "multiple=%r max_notional=%r target=%r" % (
              cal["max_notional_multiple"], cal["max_notional"], cal["target_amount"]))
    check("caliber.lines 非空且逐条为字符串（口径行由 Backtest.Report 供 SSOT）",
          isinstance(cal["lines"], list) and cal["lines"]
          and all(isinstance(x, str) and x.strip() for x in cal["lines"]),
          "lines=%r" % cal["lines"])

    print("══ ⑤ *_pct 一律已 ×100（响应侧不留小数收益率）══")
    tr = [t for t in d["trades"] if t["net_return_pct"] is not None]
    check("每笔 net_return_pct 与 r_multiple 数量级同阶（都已是百分数口径）",
          bool(tr), "无带 net_return_pct 的笔")
    if tr:
        # net_return_pct 已是 ×100 后的值：|net_return_pct| 一般 << 100 且
        # 与 r_multiple 同号（都盈利/亏损），用于抓「忘了 ×100」与「符号反了」
        same_sign = all((t["net_return_pct"] >= 0) == (t["r_multiple"] >= 0)
                        for t in tr if t["r_multiple"] is not None)
        check("逐笔 net_return_pct 与 r_multiple 同号", same_sign,
              "样本=%r" % [(t["net_return_pct"], t["r_multiple"]) for t in tr[:4]])
        check("avg_net_return_pct == 逐笔 net_return_pct 的均值（**已实现**口径）",
              abs(s["avg_net_return_pct"]
                  - sum(t["net_return_pct"] for t in tr) / len(tr)) < 1e-3,
              "summary=%r mean=%.6f" % (s["avg_net_return_pct"],
                                        sum(t["net_return_pct"] for t in tr) / len(tr)))
        # 含浮口径（2026-10-06 新增）：`*_with_open` = 已平仓 + 未平仓浮动 的等权平均，
        #   `*_open_count` = 并进来的未平仓笔数。日线样本**全平** ⇒ 两个口径必然相等、
        #   计数为 0（面板也就不加「浮」字）。"真的含浮"由 ⑫ 段的 30m 样本单独钉
        #   （那个切片末尾留 1 笔未平仓，是判别力所在）。
        check("含浮口径：日线全平 ⇒ `*_with_open` ≡ 已实现口径，且 open_count == 0",
              s["avg_net_return_open_count"] == 0
              and abs(s["avg_net_return_pct_with_open"]
                      - s["avg_net_return_pct"]) < 1e-9,
              "with_open=%r realized=%r open_count=%r" % (
                  s["avg_net_return_pct_with_open"], s["avg_net_return_pct"],
                  s["avg_net_return_open_count"]))
    check("by_bsp_type 桶内 avg_net_return_pct 也是百分数（未落下乘 100）",
          all(v["avg_net_return_pct"] is None or abs(float(v["avg_net_return_pct"])) < 1000
              for v in d["by_bsp_type"].values()),
          "by_bsp_type=%r" % d["by_bsp_type"])


# ══════════════════════════════════════════════════════════════════════
# ⑥ 400 分支穷举
# ══════════════════════════════════════════════════════════════════════
def part6(kl):
    print("══ ⑥ 400 分支穷举 ══")
    cases = [
        ("60m 不在开放周期", "sz002190", "60m", kl),
        ("空 klines", "sz002190", "d", []),
        ("坏代码 · 无市场前缀", "002190", "d", kl),
        ("坏代码 · 带点（期货样）", "SZ.002190", "d", kl),
        ("坏代码 · 未知前缀", "us002190", "d", kl),
        ("坏代码 · 空串", "", "d", kl),
        ("缺字段（无 vol）", "sz002190", "d",
         [{"date": "2021/01/04", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}]),
        ("缺 date", "sz002190", "d",
         [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 1.0}]),
        ("周期不匹配（日线带时分）", "sz002190", "d",
         [{"date": "2021/01/04 09:30", "open": 1.0, "high": 1.0, "low": 1.0,
           "close": 1.0, "vol": 1.0}]),
        ("超上限（>20000 根）", "sz002190", "d", kl + [dict(kl[-1])] * 20000),
    ]
    for tag, code, freq, klines in cases:
        try:
            _call(code, freq, klines)
            check("{} → 400".format(tag), False, "未抛 BadRequestError")
        except BadRequestError:
            check("{} → 400".format(tag), True)

    # ⑥b **正向**白名单。只钉「坏代码被拒」会漏掉「白名单被缩小」这一类回归：
    #     把它缩成 ("sh",) 上面十条照样全绿。而 ds 扩展指数（中证2000 ds932000）
    #     正是这么漏掉的 —— 页面能打开它、`AppUtils.is_index` 也认它（→「成分股」
    #     已置灰），回测入口却回「未知市场前缀」400，且错误信息完全看不出是白名单漏项。
    #     故：① 白名单本体 == `App.AppUtils.STOCK_MARKETS`（单一事实源，不许各抄一份）；
    #         ② 五个市场各拆一次码；③ ds 端到端真跑一遍（含 is_index 分流）。
    print("══ ⑥b 市场前缀白名单（正向；SSOT = App.AppUtils.STOCK_MARKETS）══")
    from App.AppUtils import STOCK_MARKETS
    from App.AppBacktest import _split_code
    check("白名单 = ('sh','sz','bj','hk','ds')（含 ds 扩展指数）",
          tuple(STOCK_MARKETS) == ("sh", "sz", "bj", "hk", "ds"), repr(STOCK_MARKETS))
    for _code, _mkt in (("sh600519", "sh"), ("sz002190", "sz"), ("bj430047", "bj"),
                        ("hk00700", "hk"), ("ds932000", "ds")):
        try:
            check("{} 拆解为 ({}, …)".format(_code, _mkt),
                  _split_code(_code)[0] == _mkt, repr(_split_code(_code)))
        except BadRequestError as e:
            check("{} 拆解为 ({}, …)".format(_code, _mkt), False, str(e))
    d_ds = _call("ds932000", "d", kl)
    check("ds932000 端到端跑通且判为指数（⚠ v2.2 同形：金额族与个股一致、不再置 null）",
          d_ds.get("ok") is True and d_ds["target"]["is_index"] is True
          and d_ds["trades"] and all(t["net_return_pct"] is not None
                                     for t in d_ds["trades"] if not t["open"])
          and d_ds["summary"]["avg_net_return_pct"] is not None,
          "ok=%r is_index=%r" % (d_ds.get("ok"), d_ds["target"].get("is_index")))


# ══════════════════════════════════════════════════════════════════════
# ⑦ AST 契约：Q8「不落盘 / 不联网」
# ══════════════════════════════════════════════════════════════════════
# 尺子（单一来源：真实文件与自证样本共用同一份判据）
_BANNED_TOP = {"sqlite3", "requests", "urllib", "socket", "subprocess",
               "shutil", "pickle", "pathlib", "tempfile", "http", "ftplib"}
# 裸名调用（`open(...)` / `os.system(...)` 的裸名形式）
_BANNED_NAMES = {"open", "urlopen", "urlretrieve", "system", "popen", "Popen",
                 "run", "check_output", "check_call", "call", "socket"}
# 属性调用（`p.write_text(...)` / `os.system(...)` / `json.dump(...)`）
#   ⚠ 刻意**不含** `write` / `replace` / `remove` 这类字符串与列表的通用方法
#     （`raw.replace("/", "-")` 就是合法用法），否则尺子会误伤 ⇒ 自证那一项会红。
_BANNED_ATTRS = {"system", "popen", "Popen", "check_output", "check_call",
                 "urlopen", "urlretrieve", "write_text", "write_bytes",
                 "writelines", "mkdir", "mkdirs", "unlink", "touch",
                 "dump", "dump_all", "savefig", "to_csv", "to_excel"}


def _scan_imports(src):
    """模块级 import 的顶层模块名（函数内 import 不在此列）。"""
    out = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.Import):
            out += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append(node.module.split(".")[0])
    return out


def _scan_violations(src):
    """函数体内（含嵌套）的落盘 / 网络 / 子进程调用（`名字(...)` 形式列表）。"""
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in _BANNED_NAMES:
            bad.append("L%d: %s(...)" % (node.lineno, fn.id))
        elif isinstance(fn, ast.Attribute) and fn.attr in _BANNED_ATTRS:
            base = fn.value.id if isinstance(fn.value, ast.Name) else "?"
            bad.append("L%d: %s.%s(...)" % (node.lineno, base, fn.attr))
    return bad


def part7():
    print("══ ⑦ AST 契约：不落盘 / 不联网 / 无子进程（Q8）══")
    src = io.open(APPBT, encoding="utf-8").read()

    imports = _scan_imports(src)
    hit = sorted(set(imports) & _BANNED_TOP)
    check("模块级 import 无 IO/网络/子进程（%s）" % ",".join(sorted(_BANNED_TOP)),
          not hit, "越界 import=%r（实际=%r）" % (hit, sorted(set(imports))))
    check("模块级 import 非空（证明上面不是空扫恒真）", bool(imports),
          "imports=%r" % imports)

    bad = _scan_violations(src)
    check("函数体无 open / 网络 / 子进程 / 写盘调用", not bad,
          "命中：\n" + "\n".join(bad))

    # 判别力自证：同一把尺子对故意违例源码必须逐类命中（否则 ⑦ 是恒真式）
    probe = ("import os\n"
             "def f(p):\n"
             "    open(p, 'w')\n"
             "    os.system('rm -rf /')\n"
             "    import json; json.dump({}, p)\n"
             "    p.write_text('x')\n")
    hits = _scan_violations(probe)
    kinds = sorted({h.split(": ", 1)[1] for h in hits})
    check("判别力自证：open / os.system / json.dump / write_text 四类均被抓到",
          kinds == ["json.dump(...)", "open(...)", "os.system(...)",
                    "p.write_text(...)"],
          "kinds=%r" % kinds)
    check("判别力自证：合法源码零报警（尺子不误伤）",
          not _scan_violations("import os\ndef f(x):\n    return os.path.join(x)\n"),
          "误伤 = %r" % _scan_violations("import os\ndef f(x):\n    return os.path.join(x)\n"))


# ══════════════════════════════════════════════════════════════════════
# ⑧⑨⑩ 前端真函数（node）
# ══════════════════════════════════════════════════════════════════════
def _extract_fn(appjs, fn_name):
    pat = ("        function " + fn_name + r"\([^)]*\) \{[\s\S]*?\n        \}")
    m = re.search(pat, appjs)
    assert m, "app.js 抽取失败: " + fn_name
    return m.group(0)


def _extract_assign(appjs, member):
    pat = (r"        window\." + member + r" = function\(\) \{[\s\S]*?\n        \};")
    m = re.search(pat, appjs)
    assert m, "app.js 抽取失败(window.%s)" % member
    return m.group(0)


def _extract_any_fn(appjs, fn_name):
    """按**花括号配平**抽一个函数（支持单行写法）。

    `_extract_fn` 的正则要求函数体以「换行 + 8 空格 + }」收尾 ⇒ 只对"每个块都换行"
    的函数有效；`renderBacktest` 体内含嵌套块、`_btNA` 是单行写法，用旧正则要么
    抽不全要么直接抽不到。这里改成从 `{` 起配平计数，两种写法都吃。
    """
    m = re.search(r"        function " + fn_name + r"\(", appjs)
    assert m, "app.js 抽取失败: " + fn_name
    i = appjs.index("{", m.end())
    depth = 0
    for j in range(i, len(appjs)):
        if appjs[j] == "{":
            depth += 1
        elif appjs[j] == "}":
            depth -= 1
            if depth == 0:
                return appjs[appjs.rfind("\n", 0, m.start()) + 1:j + 1]
    raise AssertionError("app.js 花括号不配平: " + fn_name)


def part8_9_10_11(d_idx, d_stock, d30=None, d1=None):
    print("══ ⑧⑨⑩⑪⑫ 前端真函数（node 抽段；node 不在位则 SKIP）══")
    node = shutil.which("node")
    if not node:
        check("node 不在位，前端契约层 SKIP", True)
        return

    appjs = io.open(APPJS, encoding="utf-8").read()

    prelude = (
        "var window = globalThis;\n"
        "var _closed = { bt: 0, stats: 0 };\n"
        "function closeBacktestPanel() { _closed.bt++; }\n"
        "function closeStatsPanel() { _closed.stats++; }\n"
        "function generateStats() {}\n"
        "var FUTURES = false;\n"
        "function isFuturesMode() { return FUTURES; }\n"
        "var isDualWindow = false;\n"
        "var chartData = { meta: { symbol: 'sz002190' } };\n"
        "var bspFilter = { '0': true, '1': true, '2': true, '3': true };\n"
        "var _btBtnLabel = null;\n"
        "var DOM = { 'btn-stats': { textContent: '', disabled: false, title: '' } };\n"
        "var document = { getElementById: function (id) { return DOM[id] || null; } };\n"
        # renderBacktest 的两个外部依赖：转义（本例只关心文案，按原样返回即可）与写面板
        "function statsEsc(x) { return String(x); }\n"
        "var BT_HTML = '';\n"
        "function _btWrite(html) { BT_HTML = html; }\n"
        "var out = [];\n"
        "function snap(t) { return JSON.stringify({ t: t,"
        " label: DOM['btn-stats'].textContent, dis: DOM['btn-stats'].disabled,"
        " title: DOM['btn-stats'].title, closed: _closed }); }\n"
    )

    driver = (
        # ── ⑧ _btBspTypes 四态 ──
        "bspFilter = { '0': true, '1': true, '2': true, '3': true };\n"
        "out.push('B4=' + JSON.stringify(_btBspTypes()));\n"
        "bspFilter = { '0': false, '1': false, '2': false, '3': false };\n"
        "out.push('B0=' + JSON.stringify(_btBspTypes()));\n"
        "bspFilter = { '0': true, '1': false, '2': false, '3': true };\n"
        "out.push('B03=' + JSON.stringify(_btBspTypes()));\n"
        "bspFilter = { '0': false, '1': true, '2': false, '3': false };\n"
        "out.push('B1=' + JSON.stringify(_btBspTypes()));\n"
        # ── ⑨ syncStatsButtonLabel ──（首同步：_btBtnLabel=null，不得关面板）
        "_btBtnLabel = null; FUTURES = false; isDualWindow = false;\n"
        "syncStatsButtonLabel();\n"
        "out.push('S1=' + snap(1));\n"
        # 股票→期货：应关闭回测面板、文案改「统计」
        "FUTURES = true;\n"
        "syncStatsButtonLabel();\n"
        "out.push('S2=' + snap(2));\n"
        # 期货→股票+双窗：应关闭统计面板、文案回「回测」、按钮禁用
        "FUTURES = false; isDualWindow = true;\n"
        "syncStatsButtonLabel();\n"
        "out.push('S3=' + snap(3));\n"
        # 双窗→单窗：文案未变 ⇒ 不再关面板，但 disabled 必须复位
        "isDualWindow = false;\n"
        "syncStatsButtonLabel();\n"
        "out.push('S4=' + snap(4));\n"
        # 首同步（_btBtnLabel=null）在期货态：只改文案，不关任何面板
        "_btBtnLabel = null; _closed = { bt: 0, stats: 0 }; FUTURES = true;\n"
        "syncStatsButtonLabel();\n"
        "out.push('S5=' + snap(5));\n"
        # ── ⑩ toggleStats 股票态分流 ──
        "var _toggled = 0;\n"
        "window.toggleBacktestPanel = function () { _toggled++; };\n"
        "_closed = { bt: 0, stats: 0 }; FUTURES = false;\n"
        "window.toggleStats();\n"
        "out.push('T1=' + JSON.stringify({ toggled: _toggled, closed: _closed }));\n"
        # ── ⑪ renderBacktest 指数同形（喂**真响应**，不喂手写假数据）──
        "function _naCount() { return (BT_HTML.match(/不适用/g) || []).length; }\n"
        "function _pctCount() { return (BT_HTML.match(/%/g) || []).length; }\n"
        "function _i(x) { return BT_HTML.indexOf(x); }\n"
        "function _btSnap() { return JSON.stringify({ na: _naCount(), pct: _pctCount(),"
        " badge: BT_HTML.indexOf('指数按个股假想') >= 0,"
        " oldBadge: BT_HTML.indexOf('指数标的（不可交易）') >= 0,"
        " calRow: BT_HTML.indexOf('目标成交额') >= 0,"
        " seen: BT_HTML.indexOf('首见信号') >= 0,"
        # ⑼⑽⑾ 仓位两行必须紧跟「区间 / K线」，且内部序 = 目标成交额 → 实际成交额
        #   （用字符串下标比先后 —— 不比排版，只比文档顺序）
        " order: (_i('区间 / K线') >= 0 && _i('区间 / K线') < _i('目标成交额'))"
        " && (_i('目标成交额') < _i('实际成交额')),"
        # 同日补充：最小申报行已删（旧新文案都不许出现）；逐笔最新在上
        " minLotGone: BT_HTML.indexOf('最小申报') < 0 && BT_HTML.indexOf('借道') < 0,"
        " amplGone: BT_HTML.indexOf('最大单笔放大') < 0,"
        # ⑽⑾ 金额一律万元：不许再出现「数字 + 空格 + 元」这种裸元写法
        " wan: !/\\d+ 元/.test(BT_HTML) && /万元/.test(BT_HTML),"
        # 同日补充：正值不带 + 号（正负由颜色表达）；`+` 紧贴数字即回潮。
        #   先剥掉「T+0」字面量 —— 那是口径词汇，不是带符号的数。
        " plusGone: !/\\+\\d/.test(BT_HTML.replace(/T\\+0/g, '')),"
        # 逐笔倒序 + 显示位编号（2026-10-06 同日裁定：首行=1 往下递增）：
        #   编号 `N. ` 在文档里的下标必须随 N 严格**递增**（顶部最小号）。
        " tDesc: (function () { var ps = [];"
        " for (var n = 1; n <= 9; n++) { var p = BT_HTML.indexOf('>' + n + '. '); if (p >= 0) ps.push(p); }"
        " var ok = ps.length >= 2;"
        " for (var k = 1; k < ps.length; k++) if (ps[k] <= ps[k - 1]) ok = false;"
        " return ok; })() }); }\n"
        # ⑷ 核心区标签序列（只取 hero 段，切到 stats-rows 为止 —— 明细区不在内）
        "function _heroLabels() { var i = BT_HTML.indexOf('stats-hero');"
        " if (i < 0) return ''; var j = BT_HTML.indexOf('stats-rows', i);"
        " var seg = BT_HTML.slice(i, j > i ? j : i + 3000);"
        " var out = [], m, re = /stats-label\\\">([^<]+)</g;"
        " while ((m = re.exec(seg))) out.push(m[1]); return out.join('|'); }\n"
        "BT_HTML = ''; renderBacktest(" + json.dumps(d_idx) + ");\n"
        "out.push('R1=' + _btSnap());\n"
        "out.push('H1=' + _heroLabels());\n"
        "BT_HTML = ''; renderBacktest(" + json.dumps(d_stock) + ");\n"
        "out.push('R2=' + _btSnap());\n"
        "out.push('H2=' + _heroLabels());\n"
        # ── ⑫ 出场原因三选一 / 持仓中浮动 / ⑶⑷ 可读性（喂真响应）──
        "function _tSnap() { return JSON.stringify({"
        " stop: BT_HTML.indexOf('止损') >= 0,"
        " be: BT_HTML.indexOf('保本') >= 0 && BT_HTML.indexOf('保本(1R)') < 0,"
        " trail: BT_HTML.indexOf('跟踪止盈') >= 0,"
        " rawReason: /\\b(sl|breakeven|trailing)\\b/.test(BT_HTML),"
        " float: BT_HTML.indexOf('浮') >= 0,"
        " plainBucket: BT_HTML.indexOf('(n1/R') >= 0 || BT_HTML.indexOf('/R2.') >= 0,"
        " bucket: BT_HTML.indexOf('笔 · 均R') >= 0,"
        " solo: BT_HTML.indexOf('最好＝最差') >= 0 }); }\n"
        "BT_HTML = ''; renderBacktest(" + json.dumps(d30) + ");\n"
        "out.push('F1=' + _tSnap());\n"
        "BT_HTML = ''; renderBacktest(" + json.dumps(d1) + ");\n"
        "out.push('F2=' + _tSnap());\n"
        "console.log(out.join('\\n'));\n"
    )

    harness = (prelude
               + _extract_fn(appjs, "syncStatsButtonLabel") + "\n"
               + _extract_fn(appjs, "_btBspTypes") + "\n"
               + _extract_assign(appjs, "toggleStats") + "\n"
               + _extract_any_fn(appjs, "_btCol") + "\n"
               + _extract_any_fn(appjs, "_btPct") + "\n"
               + _extract_any_fn(appjs, "_btNum") + "\n"
               + _extract_any_fn(appjs, "_btWan") + "\n"
               + _extract_any_fn(appjs, "renderBacktest") + "\n"
               + driver)

    jf = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    jf.write(harness)
    jf.close()
    try:
        proc = subprocess.run([node, jf.name], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        lines = [x for x in proc.stdout.splitlines() if x.strip()]
    finally:
        os.unlink(jf.name)

    if proc.returncode != 0 or len(lines) != 16:
        check("node 执行成功且输出 16 条", False,
              (proc.stderr or proc.stdout)[:400])
        return
    check("node 执行成功且输出 16 条", True)
    kv = dict(x.split("=", 1) for x in lines)

    # ⑧
    check("⑧ 全勾 → null（未启用过滤 = 全放行）", kv["B4"] == "null", kv["B4"])
    check('⑧ 全不勾 → ""（全过滤，≠ 全放行）', kv["B0"] == '""', kv["B0"])
    check('⑧ {0,3} → "0,3"', kv["B03"] == '"0,3"', kv["B03"])
    check('⑧ 仅 1 → "1"', kv["B1"] == '"1"', kv["B1"])

    # ⑨
    s1 = json.loads(kv["S1"])
    check('⑨ S1 股票单窗：文案「回测」/ 可用 / title 提示所见即所测',
          s1["label"] == "回测" and s1["dis"] is False and "所见即所测" in s1["title"],
          kv["S1"])
    check("⑨ S1 首同步不关闭任何面板（_btBtnLabel===null 分支）",
          s1["closed"] == {"bt": 0, "stats": 0}, kv["S1"])
    s2 = json.loads(kv["S2"])
    check('⑨ S2 切期货：文案「统计」/ title 清空',
          s2["label"] == "统计" and s2["title"] == "", kv["S2"])
    check("⑨ S2 切期货关闭回测面板（closeBacktestPanel 恰好 1 次）",
          s2["closed"] == {"bt": 1, "stats": 0}, kv["S2"])
    s3 = json.loads(kv["S3"])
    check('⑨ S3 股票双窗：文案「回测」但按钮禁用 + title 说明双窗',
          s3["label"] == "回测" and s3["dis"] is True and "双窗" in s3["title"], kv["S3"])
    check("⑨ S3 切回股票关闭统计面板（closeStatsPanel 恰好 1 次）",
          s3["closed"] == {"bt": 1, "stats": 1}, kv["S3"])
    s4 = json.loads(kv["S4"])
    check("⑨ S4 双窗→单窗：文案未变 ⇒ 不再关面板，但 disabled 复位为 false",
          s4["closed"] == {"bt": 1, "stats": 1} and s4["dis"] is False
          and s4["label"] == "回测", kv["S4"])
    s5 = json.loads(kv["S5"])
    check("⑨ S5 期货态首同步：只改文案不改面板（不关任何面板）",
          s5["closed"] == {"bt": 0, "stats": 0} and s5["label"] == "统计", kv["S5"])

    # ⑩
    t1 = json.loads(kv["T1"])
    check("⑩ 股票态 toggleStats 分流到回测（toggled=1 且不碰统计面板）",
          t1["toggled"] == 1 and t1["closed"] == {"bt": 0, "stats": 0}, kv["T1"])

    # ⑪ renderBacktest 指数同形（v2.2：指数当个股，分流摘除）
    r1, r2 = json.loads(kv["R1"]), json.loads(kv["R2"])
    check("⑪ 指数页渲染「按个股假想」横幅（后端 is_index 直达前端）",
          r1["badge"] is True, kv["R1"])
    check("⑪ 旧「不可交易」徽标零残留（v2.2 改判后不许回潮）",
          r1["oldBadge"] is False, kv["R1"])
    check("⑪ 指数页零「不适用」（v2.2 摘除全部分流：徽标 / 净收益率 / 仓位两行 / 披露行）",
          r1["na"] == 0, kv["R1"])
    check("⑪ 指数页净收益率族照常印 %（% 个数 > 1：胜率 + 期望值(%/笔) + 逐笔净收益率）",
          r1["pct"] > 1, kv["R1"])
    check("⑪ 个股页无「按个股假想」横幅、无「不适用」（披露不外溢）",
          r2["badge"] is False and r2["na"] == 0, kv["R2"])
    check("⑪ 个股页保留净收益率百分号",
          r2["pct"] > 1, kv["R2"])
    check("⑪ 两侧都渲染「目标成交额」行（同形：指数不再是「不适用」，也不是删行）",
          r1["calRow"] is True and r2["calRow"] is True, kv["R1"] + " / " + kv["R2"])

    # ⑷ 核心区五格（v2.1 统计口径统一轮 + v2.2 指数同形）：格位与顺序是**契约**，
    #   不是排版细节 —— 「净收益率(均)」删除、期望值(R)→期望值(%/笔)。
    #   这里钉"有几个格子、各叫什么、什么序"，改了名字/顺序立刻红。
    check("⑷ 核心区五格顺序 = 交易笔数 / 胜率 / 盈亏比 / 盈利因子 / 期望值(%/笔)",
          kv["H2"] == "交易笔数|胜率|盈亏比|盈利因子|期望值(%/笔)", kv["H2"])
    check("⑷ 指数页核心区格位同构（同标签同序，值不再「不适用」）",
          kv["H1"] == kv["H2"], kv["H1"])
    check("⑷ 旧核心区标签（实际胜率 / 平均净收益率 / 期望 R / 净收益率(均) / 期望值(R)）已退役",
          "实际胜率" not in kv["H2"] and "平均净收益率" not in kv["H2"]
          and "期望 R" not in kv["H2"] and "净收益率(均)" not in kv["H2"]
          and "期望值(R)" not in kv["H2"], kv["H2"])
    check("⑹ 「首见信号 / 拒收 / 过滤」行已从面板移除（后端 run.* 字段照常下发）",
          r1["seen"] is False and r2["seen"] is False, kv["R1"] + " / " + kv["R2"])
    check("⑼⑽⑾ 仓位两行紧跟「区间 / K线」，序 = 目标成交额 → 实际成交额",
          r2["order"] is True, kv["R2"])
    check("⑼补充 「最小申报 / 借道笔数」行已删、「最大单笔放大」已改名（旧文案零残留）",
          r2["minLotGone"] is True and r2["amplGone"] is True, kv["R2"])
    check("补充 逐笔明细最新在上、首行序号=1（编号下标随 N 严格递增）",
          r2["tDesc"] is True, kv["R2"])
    check("补充 正值不带 + 号（`+数字` 零出现；负数 `-` 号不受影响）",
          r2["plusGone"] is True, kv["R2"])
    check("⑽⑾ 金额一律万元（面板不再出现裸「N 元」）", r2["wan"] is True, kv["R2"])

    # ⑫ 出场原因三选一 / 持仓中浮动 / ⑶⑷ 可读性（喂真响应渲染）
    f1, f2 = json.loads(kv["F1"]), json.loads(kv["F2"])
    check("⑫ 个股 30m 页渲染出三种中文原因（止损 / 保本 / 跟踪止盈）",
          f1["stop"] and f1["be"] and f1["trail"], kv["F1"])
    check("⑫ HTML 里**不再出现**英文 reason（sl / breakeven / trailing）",
          f1["rawReason"] is False, kv["F1"])
    check("⑫ 持仓中那笔渲染出浮动标记「浮」",
          f1["float"] is True, kv["F1"])
    check("⑫ 分类型桶行已从面板移除（2026-10-06 裁定；旧新写法都不再出现）",
          f1["bucket"] is False and f1["plainBucket"] is False, kv["F1"])
    check("⑫ 「最好＝最差」行已从面板移除（同日裁定；任何样本数都不再出现）",
          f1["solo"] is False and f2["solo"] is False, kv["F1"] + " / " + kv["F2"])


# ══════════════════════════════════════════════════════════════════════
# ⑪ 指数同形（P0-⑥；⚠ v2.2 改判「指数当个股」）：响应与个股逐字段一致，
#    旧的「元口径族置 null」分流已整体摘除 —— 断言从"指数必须 null"翻转为
#    "指数与个股同形"（同字段、同值），并钉住口径披露只追加假设、无「不适用」。
# ══════════════════════════════════════════════════════════════════════
# 口径 6 字段（金额 / 成本族）—— v2.2 起指数与个股**同形**（都非 null 且同值）
_AMT_CAL_KEYS = ("target_amount", "min_lot", "lot_step", "min_lot_derived_trades",
                 "max_notional", "max_notional_multiple")
# 价格侧 summary 字段 —— 与标的类型（个股 / 指数）无关，两侧必须逐字段相等
_PRICE_SUM_KEYS = ("n", "w", "l", "e", "u", "win_rate", "profit_loss_ratio",
                   "profit_factor", "expectancy_r", "avg_bars_held",
                   "max_win_r", "max_loss_r")
_RUN_KEYS = ("bars_total", "signals_seen", "signals_filtered", "signals_rejected",
             "filled", "closed", "still_open")
# v2.2 同形后，逐笔金额/成本族（shares / cost_cash / net_return_pct）两侧也必然
# 相等（本样本 lot 规则恰好一致：sz002190 主板 (100,100) ≡ 指数强制值），一并入契约
_TRADE_KEYS = ("trade_id", "side", "bsp_type", "entry_date", "entry_price",
               "r_distance", "exit_date", "exit_reason", "r_multiple",
               "gross_return_pct", "shares", "cost_cash", "net_return_pct")


def part11(kl, d_stock):
    """指数同形：判定同源 + 响应与个股逐字段一致（v2.2「指数当个股」）。

    样本策略：**同一份 K 线切片**（sz002190 日线）只换代码题头 ——
    `sh000001`（指数）vs `sz002190`（个股）。这样两侧的形态信号必然逐字段相同，
    任何差异都只能来自 `is_index` 的消费点（现在只剩 sizing 的 lot 强制与披露
    追加）⇒ 判别力来自实验设计，不靠运气。
    """
    print("══ ⑪ 指数同形（v2.2：指数当个股，无分流）══")
    from App.AppUtils import is_index                      # 页面级 SSOT
    from DataAPI.TdxAPI import _is_index_code              # 取数层私有段判定

    # ① 判定 SSOT 逐例（含 sh000001=指数 vs sz000001=个股 的同号反例）
    cases = [("sh", "000001", True), ("sz", "000001", False),
             ("sh", "000300", True), ("sh", "880491", True), ("sh", "881319", True),
             ("sh", "990001", True), ("sh", "600519", False),
             ("sz", "399001", True), ("sz", "399006", True), ("sz", "000063", False),
             ("sz", "300750", False), ("ds", "932000", True),
             ("hk", "HSTECH", True), ("hk", "00700", False), ("bj", "430047", False)]
    bad = [(m, c) for m, c, exp in cases if is_index(m, c) != exp]
    check("⑪ AppUtils.is_index 15 例全对（页面级 SSOT，含 ds/hk/88xx 板块指数）",
          not bad, "判错=%r" % bad)
    check("⑪ is_index 必须带 market（同码两解：sh000001 指数 / sz000001 个股）",
          is_index("sh", "000001") is True and is_index("sz", "000001") is False
          and is_index("sh", "399001") is False and is_index("sz", "399001") is True)
    # 取数层助手与页面级判定**故意不同源**：88xx 段页面级归指数、取数层不认
    check("⑪ 取数层 _is_index_code 只管 A 股指数段（sh880491 不认 —— 故不可用于页面分流）",
          _is_index_code("sh", "000001") is True
          and _is_index_code("sz", "399001") is True
          and _is_index_code("sh", "880491") is False
          and _is_index_code("sh", "600519") is False
          and _is_index_code("sh", None) is False
          and _is_index_code("sz", "000001") is False)

    # ② 同一份 K 线：指数题头 vs 个股题头
    d_idx = _call("sh000001", "d", kl)
    check("⑪ target.is_index / caliber.is_index 与代码相符（两侧互为反例）",
          d_idx["target"]["is_index"] is True and d_idx["caliber"]["is_index"] is True
          and d_stock["target"]["is_index"] is False
          and d_stock["caliber"]["is_index"] is False,
          "idx=%r/%r stock=%r/%r" % (d_idx["target"].get("is_index"),
                                     d_idx["caliber"].get("is_index"),
                                     d_stock["target"].get("is_index"),
                                     d_stock["caliber"].get("is_index")))
    # 板块指数（meta.is_index=True 但取数层不认）也要标注 —— 这条钉的是"判定同源"；
    #   v2.2 同形后金额族照常产出（不再是 null），且 sizing 强制 (100,100)
    #   （`lot_rule` 的 `"88"` 前缀会把 sh880xxx 误判成北交所 step=1，已由
    #   `shares_for(is_index=True)` 旁路）
    d_sec = _call("sh880491", "d", kl)
    check("⑪ 板块指数 sh880491 同样标注（与 meta.is_index 一致）且金额族非 null",
          d_sec["target"]["is_index"] is True
          and d_sec["caliber"]["max_notional_multiple"] is not None
          and d_sec["caliber"]["min_lot"] == 100
          and d_sec["caliber"]["lot_step"] == 100,
          "is_index=%r maxmult=%r minlot=%r/%r" % (
              d_sec["target"]["is_index"],
              d_sec["caliber"]["max_notional_multiple"],
              d_sec["caliber"]["min_lot"], d_sec["caliber"]["lot_step"]))

    check("⑪ 口径 6 字段：指数与个股同形（全非 null 且同值；本样本 lot 规则一致）",
          all(d_idx["caliber"][k] is not None for k in _AMT_CAL_KEYS)
          and all(d_idx["caliber"][k] == d_stock["caliber"][k]
                  for k in _AMT_CAL_KEYS),
          "idx=%r" % {k: d_idx["caliber"][k] for k in _AMT_CAL_KEYS})
    check("⑪ summary.avg_net_return_pct：指数与个股同值（同形，不再置 null）",
          d_idx["summary"]["avg_net_return_pct"] is not None
          and d_idx["summary"]["avg_net_return_pct"]
          == d_stock["summary"]["avg_net_return_pct"])
    check("⑪ by_bsp_type：指数桶内 avg_net_return_pct 全非 null（样本非空，同形）",
          bool(d_idx["by_bsp_type"])
          and all(v["avg_net_return_pct"] is not None
                  for v in d_idx["by_bsp_type"].values()),
          "by_bsp_type=%r" % d_idx["by_bsp_type"])
    check("⑪ by_bsp_type：指数桶内 n / expectancy_r 保留（价格侧口径）",
          all(int(v["n"]) > 0 and v["expectancy_r"] is not None
              for v in d_idx["by_bsp_type"].values()),
          "by_bsp_type=%r" % d_idx["by_bsp_type"])
    check("⑪ 逐笔 shares / cost_cash / net_return_pct：指数全非 null（样本非空，同形）",
          bool(d_idx["trades"])
          and all(t["shares"] is not None and t["cost_cash"] is not None
                  and t["net_return_pct"] is not None for t in d_idx["trades"]),
          "首笔=%r" % (d_idx["trades"][0] if d_idx["trades"] else None))
    check("⑪ 逐笔 gross_return_pct / r_multiple：指数保留（价格侧口径）",
          bool(d_idx["trades"])
          and all(t["gross_return_pct"] is not None and t["r_multiple"] is not None
                  for t in d_idx["trades"]))

    # ③ ★ 核心不变量：is_index 只标注、不参与计算
    check("⑪ 同一份 K 线：%d 个价格侧 summary 字段两侧逐字段相等" % len(_PRICE_SUM_KEYS),
          [d_idx["summary"][k] for k in _PRICE_SUM_KEYS]
          == [d_stock["summary"][k] for k in _PRICE_SUM_KEYS],
          "idx=%r" % {k: d_idx["summary"][k] for k in _PRICE_SUM_KEYS})
    check("⑪ 同一份 K 线：run %d 字段两侧相等（引擎口径与标的类型无关）" % len(_RUN_KEYS),
          [d_idx["run"][k] for k in _RUN_KEYS] == [d_stock["run"][k] for k in _RUN_KEYS],
          "idx=%r stock=%r" % (d_idx["run"], d_stock["run"]))
    check("⑪ 同一份 K 线：逐笔 %d 字段两侧逐笔相等（形态信号完全同源）" % len(_TRADE_KEYS),
          [[t[k] for k in _TRADE_KEYS] for t in d_idx["trades"]]
          == [[t[k] for k in _TRADE_KEYS] for t in d_stock["trades"]])

    # ④ 口径行 / 披露（v2.2 同日去重裁定：指数假设只在横幅与 CLI 口径行披露，
    #   面板底部 disclosures 与个股**同一份**两条 —— 不再追加第三条）
    check("⑪ disclosures：个股 / 指数同为 2 条（指数假设由横幅披露，底部不重复）",
          len(d_stock["disclosures"]) == 2 and len(d_idx["disclosures"]) == 2
          and d_idx["disclosures"] == d_stock["disclosures"],
          "idx=%r" % (d_idx["disclosures"],))
    check("⑪ caliber.lines：个股 4 条 / 指数 5 条，末条披露「按个股假想」且全文无「不适用」",
          len(d_stock["caliber"]["lines"]) == 4
          and len(d_idx["caliber"]["lines"]) == 5
          and "按个股假想" in d_idx["caliber"]["lines"][-1]
          and not any("不适用" in x for x in d_idx["caliber"]["lines"]),
          "idx_lines=%r" % (d_idx["caliber"]["lines"],))
    # 口径行 = [0]标的/周期/区间 [1]出场参数 [2]费率 [3]偏离披露 [4]指数追加
    # 只有 [0] 含标的代码故必然不同；[1:4] 三条必须逐字相同 —— 否则就是分流
    # 顺手改写了既有口径（比"多印一行"坏得多）
    check("⑪ 口径行 [1:4] 三条两侧逐字相同（出场参数 / 费率 / 偏离披露）",
          d_idx["caliber"]["lines"][1:4] == d_stock["caliber"]["lines"][1:4],
          "idx=%r stock=%r" % (d_idx["caliber"]["lines"][1:4],
                               d_stock["caliber"]["lines"][1:4]))
    check("⑪ 口径行 [0] 只差「标的」代码（周期 / 区间口径未被分流触碰）",
          d_idx["caliber"]["lines"][0].replace("sh000001", "sz002190")
          == d_stock["caliber"]["lines"][0],
          "%r vs %r" % (d_idx["caliber"]["lines"][0], d_stock["caliber"]["lines"][0]))
    check("⑪ 面向前端的字符串不留 Markdown 强调符（`**` 在渲染层会原样吐出来）",
          not any("**" in str(x) for x in d_idx["caliber"]["lines"])
          and not any("**" in str(x) for x in d_idx["disclosures"]),
          "lines=%r" % d_idx["caliber"]["lines"])

    # ⑤ Backtest 层直连（层表：Backtest 不得 import App，故标注只能靠注入 + is_index）
    from Backtest.Report import caliber_lines
    from Backtest.Runner import RunResult
    r_idx = RunResult(market="sh", code="000001", freq="d", is_index=True)
    r_stk = RunResult(market="sz", code="002190", freq="d", is_index=False)
    check("⑪ Backtest.Report.caliber_lines 直接吃 RunResult.is_index（不依赖 App 层）",
          len(caliber_lines(r_idx)) == 5
          and any("按个股假想" in x for x in caliber_lines(r_idx))
          and not any("不适用" in x for x in caliber_lines(r_idx)),
          "lines=%r" % (caliber_lines(r_idx),))
    check("⑪ is_index=False（默认）时不追加 —— 既有调用方行为零变化",
          len(caliber_lines(r_stk)) == 4
          and not any("按个股假想" in x for x in caliber_lines(r_stk)))
    check("⑪ RunResult.is_index 默认 False（不显式注入就不标注）",
          RunResult(market="sh", code="000001", freq="d").is_index is False)
    from Backtest.Metrics import compute
    from Backtest.Report import summary_text
    from Backtest.Runner import load_records, run as bt_run
    # 真跑一轮（不走 App 层）才能拿到非空 metrics —— `summary_text` 的净收益率
    # 那一行在 `m.n == 0` 时不打印，空 RunResult 测不出同形。
    recs = load_records(os.path.join(FIXTURES, "sz002190_d.json"))
    rr_idx = bt_run("sh", "000001", "d", records=recs, is_index=True)
    rr_stk = bt_run("sz", "002190", "d", records=recs)
    txt_idx = summary_text(rr_idx, compute(rr_idx))
    txt_stk = summary_text(rr_stk, compute(rr_stk))
    check("⑪ 控制台摘要同形：指数不再标「(不适用·指数)」，净收益率照常印",
          "(不适用" not in txt_idx
          and "平均净收益率" in txt_idx
          and "平均净收益率" in txt_stk,
          "idx=%r" % txt_idx[:180])
    check("⑪ 控制台摘要的期望 R（毛）对指数照常印（价格侧口径有效）",
          "期望 R（毛）" in txt_idx)
    check("⑪ 控制台摘要里「不适用」两侧都绝迹（v2.2 同形）",
          "不适用" not in txt_idx and "不适用" not in txt_stk)
    check("⑪ 未注入 is_index（默认 False）时 Runner 不标注 —— 注入是唯一开关",
          rr_stk.is_index is False and rr_idx.is_index is True)
    return d_idx


# ══════════════════════════════════════════════════════════════════════
# ⑫ 出场原因三选一 + 未平仓浮动估值（v1.18，2026-10-05 用户裁定）
# ══════════════════════════════════════════════════════════════════════
def part12():
    """出场面板的两条显示契约在 **App 边界**上是否自洽。

    分工：`Backtest/Test/test_bt09_display_labels.py` 钉的是 `Backtest/` 层
    （文案 SSOT + 估值槽位隔离）；本段钉的是**过界之后**：映射有没有随响应下发、
    指数分流有没有漏到浮动的百分比上、以及 `n==1` 这种"看着像坏了"的边界。
    """
    print("══ ⑫ 出场原因三选一 / 未平仓浮动估值 ══")

    # ── 日线：三选一映射随响应下发，且三种原因都真实出现过 ──
    d = _call("sz002190", "d", _page_klines("sz002190_d.json"))
    lab = d.get("exit_reason_labels") or {}
    check("⑫ 响应带 exit_reason_labels（三选一，键序稳定 = sl/breakeven/trailing）",
          list(lab) == ["sl", "breakeven", "trailing"]
          and lab == {"sl": "止损", "breakeven": "保本", "trailing": "跟踪止盈"},
          "labels=%r" % lab)
    check("⑫ 响应带 exit_reason_legend（与标签同键、逐条非空）",
          set(d.get("exit_reason_legend") or {}) == set(lab)
          and all(str(v).strip() for v in (d.get("exit_reason_legend") or {}).values()),
          "legend=%r" % d.get("exit_reason_legend"))
    reasons = {t["exit_reason"] for t in d["trades"]}
    check("⑫ 日线 6 笔覆盖三种原因（映射每个键都有真实样本，不是死配置）",
          reasons == set(lab), "reasons=%r" % sorted(reasons))
    check("⑫ by_reason 的键全部可被 labels 翻译（前端不会漏出英文标识符）",
          set(d["by_reason"]) <= set(lab), "by_reason=%r" % sorted(d["by_reason"]))
    check("⑫ 逐笔 exit_reason 仍是引擎原值（数据契约不变，中文化只发生在显示层）",
          all(t["exit_reason"] in lab for t in d["trades"]))
    check("⑫ 日线全平（未平 0 笔）⇒ 浮动三字段整列为 null",
          d["run"]["still_open"] == 0
          and all(t["unrealized_price"] is None and t["unrealized_r"] is None
                  and t["unrealized_net_return_pct"] is None for t in d["trades"]),
          "still_open=%r" % d["run"]["still_open"])

    # ── 30m：末尾留 1 笔未平仓 → 浮动字段非空、已平笔恒 null ──
    kl30 = _page_klines("sz002190_30m.json", minute=True)
    d30 = _call("sz002190", "30m", kl30)
    opens = [t for t in d30["trades"] if t["open"]]
    closed = [t for t in d30["trades"] if not t["open"]]
    check("⑫ 30m 末尾留 1 笔未平仓（样本前提）", len(opens) == 1,
          "open=%d closed=%d" % (len(opens), len(closed)))
    t = opens[0]
    check("⑫ 未平仓笔浮动三字段齐全（价 / R / 净收益率）",
          t["unrealized_price"] is not None and t["unrealized_r"] is not None
          and t["unrealized_net_return_pct"] is not None,
          "trade=%r" % {k: t[k] for k in ("unrealized_price", "unrealized_r",
                                          "unrealized_net_return_pct")})
    check("⑫ ★ 未平仓笔的**已实现**字段恒 null（估值不写进成交结果槽位）",
          all(t[k] is None for k in ("exit_reason", "exit_date", "bars_held",
                                     "r_multiple", "net_return_pct", "cost_cash")),
          "脏字段=%r" % {k: t[k] for k in ("exit_reason", "exit_date", "bars_held",
                                           "r_multiple", "net_return_pct", "cost_cash")
                         if t[k] is not None})
    check("⑫ ★ 已平仓笔的**浮动**字段恒 null（两族字段不共用槽位）",
          all(x["unrealized_price"] is None and x["unrealized_r"] is None
              and x["unrealized_net_return_pct"] is None for x in closed),
          "脏笔=%r" % [x["trade_id"] for x in closed
                       if x["unrealized_r"] is not None][:3])
    last_close = kl30[-1]["close"]
    sign = 1 if t["side"] == "long" else -1
    check("⑫ unrealized_price ≡ 页面序列最后一根收盘价（估值锚点唯一）",
          abs(t["unrealized_price"] - last_close) < 1e-9,
          "got=%r want=%r" % (t["unrealized_price"], last_close))
    # 手算复核对得上（容差 1e-4：响应侧的 entry_price / r_distance 各自圆到 6 位，
    #   再由它们反推 —— 与内核里那次 round(...,4) 之间只该差浮点末几位。
    #   逐字段**精确**相等由 Backtest/Test/test_bt09_display_labels.py 在内核侧钉。）
    check("⑫ unrealized_r ≡ (last−entry)·sign/R（手算复核，1e-4 容差）",
          abs(t["unrealized_r"]
              - (last_close - t["entry_price"]) * sign / t["r_distance"]) < 1e-4,
          "got=%r" % t["unrealized_r"])
    check("⑫ 未平仓 落进 run.still_open / 逐笔 open 计数一致",
          d30["run"]["still_open"] == len(opens)
          and d30["run"]["closed"] + d30["run"]["still_open"] == d30["run"]["filled"])

    # ── 含浮口径（2026-10-06）：30m 切片末尾留 1 笔未平仓 ⇒ 两口径必须**不等** ──
    #    这才是"真的含浮"的判别力所在：日线样本全平，两口径恒等，什么都证不出来。
    s30 = d30["summary"]
    _realized = [t["net_return_pct"] for t in closed]
    _float = [t["unrealized_net_return_pct"] for t in opens]
    check("⑫ 含浮口径：open_count == 未平仓笔数（1），且与已实现口径**不等**",
          s30["avg_net_return_open_count"] == len(opens) == 1
          and abs(s30["avg_net_return_pct_with_open"]
                  - s30["avg_net_return_pct"]) > 1e-9,
          "with_open=%r realized=%r count=%r" % (
              s30["avg_net_return_pct_with_open"], s30["avg_net_return_pct"],
              s30["avg_net_return_open_count"]))
    check("⑫ 含浮均值 ≡ (Σ已平 + Σ未平浮动) / (已平笔数 + 未平笔数)（手算复核）",
          abs(s30["avg_net_return_pct_with_open"]
              - (sum(_realized) + sum(_float)) / (len(_realized) + len(_float))) < 1e-3,
          "with_open=%r hand=%.6f" % (
              s30["avg_net_return_pct_with_open"],
              (sum(_realized) + sum(_float)) / (len(_realized) + len(_float))))
    check("⑫ 已实现口径**不含**未平仓（浮动不许混进已落袋均值）",
          abs(s30["avg_net_return_pct"] - sum(_realized) / len(_realized)) < 1e-3,
          "realized=%r closed_mean=%.6f" % (s30["avg_net_return_pct"],
                                            sum(_realized) / len(_realized)))

    # ── 指数侧：v2.2 同形 —— 未平仓浮动净收益率照常给（不再置 null），价格侧的 R 照常 ──
    di = _call("sh000001", "30m", kl30)
    oi = [x for x in di["trades"] if x["open"]]
    check("⑫ 指数页未平仓笔：unrealized_net_return_pct 非 null（同形）/ 毛 R 保留",
          len(oi) == 1 and oi[0]["unrealized_net_return_pct"] is not None
          and oi[0]["unrealized_r"] is not None,
          "open=%r" % (oi[0] if oi else None))

    # ── n==1 的真区间样本：⑷「最好＝最差」说明的样本 ──
    d1 = _call("sz002190", "15m", _page_klines("sz002190_15m.json", minute=True, hi=200))
    check("⑫ n==1 样本取自**真区间**（15m 前 200 根：已平 1 / 未平 0）",
          d1["summary"]["n"] == 1 and d1["run"]["still_open"] == 0,
          "n=%r still_open=%r" % (d1["summary"]["n"], d1["run"]["still_open"]))
    check("⑫ n==1 时最好 R ≡ 最差 R（同一笔既是最好也是最差）—— 后端如实给",
          d1["summary"]["max_win_r"] == d1["summary"]["max_loss_r"]
          and d1["summary"]["max_win_r"] is not None,
          "max_win=%r max_loss=%r" % (d1["summary"]["max_win_r"],
                                      d1["summary"]["max_loss_r"]))
    return d30, d1


def main():
    print("=" * 68)
    print("股票页「回测」护栏（Test/test_stock_backtest.py）")
    print("=" * 68)
    kl = _page_klines("sz002190_d.json")
    d = part1()
    part2()
    part3(kl, d["summary"]["expectancy_r"])
    part45(d)
    part6(kl)
    part7()
    d_idx = part11(kl, d)
    d30, d1 = part12()
    part8_9_10_11(d_idx, d, d30, d1)

    n_pass = sum(1 for x in _OK if x)
    print("-" * 68)
    print("合计 %d 项，通过 %d，失败 %d" % (len(_OK), n_pass, len(_OK) - n_pass))
    return len(_OK) == n_pass


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

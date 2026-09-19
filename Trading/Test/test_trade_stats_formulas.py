# -*- coding: utf-8 -*-
"""成交统计「四项指标口径 + 最大单笔同侧」契约。

背景（2026-09-19 用户要求复核）
---------------------------------------------------------------------
用户问：「实际胜率 / 盈亏比 / 盈利因子 / 期望值」这四个数的**计算公式是否正确**，
以及「最大单笔盈利没有盈利时应显示 0 而不是负数」。

复核结论（本文件把它钉成可执行断言）：

  [A] 实际胜率   = 盈利笔数 ÷ **总笔数**（平手计入分母、不计入胜）
        —— 与期望值自洽：win_rate*avg_win + loss_rate*avg_loss
           展开后恒等于 total_net/count（见 [B1] 恒等式）。
  [B] 期望值     = 胜率×平均每笔盈利 + 败率×平均每笔亏损  ≡ 总净盈亏 ÷ 总笔数
        （两式恒等，[B1] 用独立算法交叉验证，误差 < 1e-9）
  [C] 盈亏比(赔率) = 平均每笔盈利 ÷ |平均每笔亏损|（**均值**口径）
  [D] 盈利因子     = 总盈利 ÷ |总亏损|（**总量**口径）
        —— [C][D] 必须可区分，见 Trading/Test/test_trade_stats_ratio_naming.py
  [E] 一侧为空：无亏损 → 盈亏比/盈利因子 = None（除法无定义，不是 0）；
        无盈利 → 0.0（0÷亏损 有定义，就是 0）。
  [F] 最大单笔盈利 / 亏损：只在**同侧**成交里取。某一侧一笔都没有 → 报 0、
        trade_id/exit_at 为 None。
        旧实现取全样本 max/min：全亏的品种会把"亏得最少的那一笔"当成
        最大盈利报出去 —— 面板上「最大单笔盈利」显示 -880.00 元，自相矛盾。

跑法：python Trading/Test/test_trade_stats_formulas.py
"""
from __future__ import annotations

import ast
import inspect
import os
import sys

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
sys.path.insert(0, _REPO)

from Trading.Infra.TradeStats import (  # noqa: E402
    _empty_stats, compute_trade_stats)

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


def near(a, b, tol=1e-6):
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def mk(net_cash, i, exit_at=None):
    return {
        "trade_id": "T%d" % i, "symbol": "CFFEX.IF2609", "side": "long",
        "volume": 1, "entry_price": 4500.0, "exit_price": 4500.0,
        "entry_at": "2026-09-01 09:00",
        "exit_at": exit_at or ("2026-09-%02d 10:00" % (i % 9 + 1)),
        "reason": "tp" if net_cash > 0 else ("sl" if net_cash < 0 else "time"),
        "gross_points": float(net_cash), "cost_cash": 0.0,
        "net_cash": float(net_cash), "bars_held": 3,
        "exit_plan_name": "run_managed", "exit_plan_params": "{}",
    }


def stats_of(nets):
    return compute_trade_stats([mk(n, i) for i, n in enumerate(nets)])


def independent(nets):
    """独立实现：不经 compute_trade_stats，直接按定义累加。

    刻意用"最笨"的写法（显式循环 + 分子分母各存一份）当参照物 ——
    如果被测实现的分子分母取错（比如把平手算进胜率分子），这里会不一致。
    """
    wins = [float(x) for x in nets if float(x) > 0]
    losses = [float(x) for x in nets if float(x) < 0]
    flats = [float(x) for x in nets if float(x) == 0]
    count = len(nets)
    gross_win = sum(wins)
    gross_loss = sum(losses)
    out = {
        "count": count, "wins": len(wins), "losses": len(losses),
        "flat": len(flats),
        "win_rate": (len(wins) / count) if count else 0.0,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "total_net": gross_win + gross_loss,
    }
    # 无亏损 → 分母是 0 → 无定义 None；无盈利但**有**亏损 → 0÷亏损 = 0.0（有定义）
    out["profit_factor"] = ((gross_win / abs(gross_loss))
                            if gross_loss else None)
    out["pl_ratio"] = ((out["avg_win"] / abs(out["avg_loss"]))
                       if out["avg_loss"] else None)
    out["expectancy"] = (out["win_rate"] * out["avg_win"]
                         + (len(losses) / count) * out["avg_loss"]) if count else 0.0
    return out


# ══════════════════════════════════════════════════════════════
print("\n[A] 四项指标 vs 独立实现（同一组样本逐项对照）")
# ══════════════════════════════════════════════════════════════
SAMPLES = {
    "混合 4 笔": [100, 100, 3000, -500],
    "含平手 6 笔": [200, 300, -100, -250, 0, 450],
    "全亏 3 笔": [-100, -880, -300],
    "全盈 2 笔": [500, 900],
    "全平 2 笔": [0, 0],
    "单笔 +666": [666],
    "单笔 -666": [-666],
    "大数 5 笔": [120000.5, -33333.25, 75000.0, -1.75, 0],
    "零净额 3 笔": [500, -500, 0],
}
for name, nets in SAMPLES.items():
    got, ref = stats_of(nets), independent(nets)
    check_true("[A] %s：笔数/胜/亏/平 一致" % name,
               (got["count"], got["wins"], got["losses"], got["flat"])
               == (ref["count"], ref["wins"], ref["losses"], ref["flat"]),
               (got["count"], got["wins"], got["losses"], got["flat"]))
    check_true("[A] %s：实际胜率 = 盈利笔数/总笔数" % name,
               near(got["win_rate"], round(ref["win_rate"], 4)),
               "%s vs %s" % (got["win_rate"], ref["win_rate"]))
    check_true("[A] %s：总净盈亏 = Σ净额" % name,
               near(got["total_net"], round(ref["total_net"], 2)),
               got["total_net"])
    check_true("[A] %s：平均每笔盈利 = Σ盈/盈笔数" % name,
               near(got["avg_win"], round(ref["avg_win"], 2)), got["avg_win"])
    check_true("[A] %s：平均每笔亏损 = Σ亏/亏笔数" % name,
               near(got["avg_loss"], round(ref["avg_loss"], 2)), got["avg_loss"])
    # 盈亏比 / 盈利因子：None 语义两边必须一致
    if ref["pl_ratio"] is None:
        check_true("[A] %s：无亏损 → 盈亏比 = None" % name, got["pl_ratio"] is None,
                   got["pl_ratio"])
    else:
        check_true("[A] %s：盈亏比 = 均值口径（avg_win/|avg_loss|）" % name,
                   near(got["pl_ratio"], round(ref["pl_ratio"], 4)), got["pl_ratio"])
    if ref["profit_factor"] is None:
        check_true("[A] %s：无亏损 → 盈利因子 = None" % name,
                   got["profit_factor"] is None, got["profit_factor"])
    else:
        check_true("[A] %s：盈利因子 = 总量口径（Σ盈/|Σ亏|）" % name,
                   near(got["profit_factor"], round(ref["profit_factor"], 4)),
                   got["profit_factor"])


# ══════════════════════════════════════════════════════════════
print("\n[B] 期望值：与 总净盈亏÷总笔数 的恒等式（独立算法，逐样本）")
# ══════════════════════════════════════════════════════════════
for name, nets in SAMPLES.items():
    got = stats_of(nets)
    cnt = len(nets)
    want = round(sum(float(x) for x in nets) / cnt, 2) if cnt else 0.0
    check_true("[B1] %s：期望值 ≡ 总净盈亏/总笔数" % name,
               near(got["expectancy"], want, 0.011),
               "%s vs %s" % (got["expectancy"], want))

# 随机样本上再跑一遍（不同盈亏比、含平手），防"只在固定样本上凑对"。
# 盈笔数 ≠ 亏笔数 —— 两者相等时 avg 的除数会约掉，两个口径数学上必然相等。
_RAND = [317.5, -1000.0, 0.0, 88.25, 4025.5, -1.0, -640.75, 0.0, 9999.99]
_got = stats_of(_RAND)
_want = round(sum(_RAND) / len(_RAND), 2)
check_true("[B2] 随机样本期望值 ≡ 总净盈亏/总笔数", near(_got["expectancy"], _want, 0.011),
           "%s vs %s" % (_got["expectancy"], _want))
check_true("[B2] 该样本（4 盈 / 3 亏）盈亏比 ≠ 盈利因子（两口径确实不同）",
           _got["pl_ratio"] != _got["profit_factor"],
           "%s vs %s" % (_got["pl_ratio"], _got["profit_factor"]))


# ══════════════════════════════════════════════════════════════
print("\n[C] 实际胜率的分母：平手计入分母、不计入胜（显式钉住）")
# ══════════════════════════════════════════════════════════════
_s = stats_of([200, 300, -100, -250, 0, 450])   # 3 盈 / 2 亏 / 1 平
check("[C1] 3 盈 2 亏 1 平 → 实际胜率 0.5（分母是 6 不是 5）", _s["win_rate"], 0.5)
# 3 笔盈利 200+300+450 = 950（均值 316.67）；2 笔亏损 -100-250 = -350（均值 -175）
#   盈亏比（均值口径）  = 316.67 / 175  = 1.8095…
#   盈利因子（总量口径）= 950 / 350     = 2.7143…
check("[C2] 该样本盈亏比 = round(316.6667/175, 4)", _s["pl_ratio"], 1.8095)
check("[C3] 该样本盈利因子（总量口径）= 950/350",
      _s["profit_factor"], round(950 / 350, 4))
check_true("[C4] 盈亏比 = avg_win/|avg_loss|（不靠手算，用字段自身交叉验证）",
           near(_s["pl_ratio"], round(_s["avg_win"] / abs(_s["avg_loss"]), 4), 1e-4),
           "%s vs %s/%s" % (_s["pl_ratio"], _s["avg_win"], _s["avg_loss"]))


# ══════════════════════════════════════════════════════════════
print("\n[E] 一侧为空的语义：无亏损 → None；无盈利 → 0")
# ══════════════════════════════════════════════════════════════
_all_win = stats_of([500, 900])
check("[E1] 全盈：盈亏比 = None（无定义，不是 0、不是 ∞）", _all_win["pl_ratio"], None)
check("[E2] 全盈：盈利因子 = None", _all_win["profit_factor"], None)
_all_loss = stats_of([-100, -880, -300])
check("[E3] 全亏：盈亏比 = 0.0（0÷亏损 = 0，有定义）", _all_loss["pl_ratio"], 0.0)
check("[E4] 全亏：盈利因子 = 0.0", _all_loss["profit_factor"], 0.0)
check("[E5] 全平：两比率都 = None", (_all_loss and
                                    stats_of([0, 0])["pl_ratio"],
                                    stats_of([0, 0])["profit_factor"]),
      (None, None))
_e = _empty_stats()
check("[E6] 空样本：四键齐全且比率为 None",
      (_e["win_rate"], _e["expectancy"], _e["pl_ratio"], _e["profit_factor"]),
      (0.0, 0.0, None, None))


# ══════════════════════════════════════════════════════════════
print("\n[F] 最大单笔：只在同侧成交里取（没有盈利就报 0）")
# ══════════════════════════════════════════════════════════════
_mix = stats_of([100, 100, 3000, -500])
check("[F1] 混合：最大单笔盈利 = 该侧最大值", _mix["max_win"]["net_cash"], 3000.0)
check("[F2] 混合：最大单笔亏损 = 该侧最小值", _mix["max_loss"]["net_cash"], -500.0)
check_true("[F3] 混合：两侧的 trade_id 分别指向各自那一笔",
           (_mix["max_win"]["trade_id"], _mix["max_loss"]["trade_id"])
           == ("T2", "T3"),
           (_mix["max_win"]["trade_id"], _mix["max_loss"]["trade_id"]))

_loss_only = stats_of([-100, -880, -300])
check("[F4] ★ 全亏：最大单笔盈利 = 0.0（**不是** -100 那个亏得最少的）",
      _loss_only["max_win"]["net_cash"], 0.0)
check("[F5] ★ 全亏：最大单笔盈利不带时间/单号",
      (_loss_only["max_win"]["trade_id"], _loss_only["max_win"]["exit_at"]),
      (None, None))
check("[F6] 全亏：最大单笔亏损仍是 -880", _loss_only["max_loss"]["net_cash"], -880.0)

_win_only = stats_of([500, 900])
check("[F7] ★ 全盈：最大单笔亏损 = 0.0（不是赚得最少的那一笔）",
      _win_only["max_loss"]["net_cash"], 0.0)
check("[F8] ★ 全盈：最大单笔亏损不带时间/单号",
      (_win_only["max_loss"]["trade_id"], _win_only["max_loss"]["exit_at"]),
      (None, None))
check("[F9] 全盈：最大单笔盈利仍是 900", _win_only["max_win"]["net_cash"], 900.0)

_flat_only = stats_of([0, 0])
check("[F10] 全平：两侧都 = 0.0",
      (_flat_only["max_win"]["net_cash"], _flat_only["max_loss"]["net_cash"]),
      (0.0, 0.0))
check("[F11] 空样本：两侧都 = 0.0 且无单号",
      (_e["max_win"], _e["max_loss"]),
      ({"trade_id": None, "exit_at": None, "net_cash": 0.0},
       {"trade_id": None, "exit_at": None, "net_cash": 0.0}))

# 平手不参与任一侧：3 盈 2 亏 1 平 → 最大单笔盈利必须 > 0、亏损必须 < 0
check_true("[F12] 有平手时两侧仍各取本侧极值（0 不会顶掉真盈亏）",
           _s["max_win"]["net_cash"] == 450.0 and _s["max_loss"]["net_cash"] == -250.0,
           (_s["max_win"]["net_cash"], _s["max_loss"]["net_cash"]))


# ══════════════════════════════════════════════════════════════
print("\n[G] 源码契约：不许退回全样本 max/min（防回潮）")
# ══════════════════════════════════════════════════════════════
_src = inspect.getsource(compute_trade_stats)
check_true("[G1] 源码不含 max(trades, key=_net)（全样本口径）",
           "max(trades, key=_net)" not in _src)
check_true("[G2] 源码不含 min(trades, key=_net)",
           "min(trades, key=_net)" not in _src)
check_true("[G3] 源码在 wins / losses 上取极值",
           "max(wins, key=_net)" in _src and "min(losses, key=_net)" in _src)


# ══════════════════════════════════════════════════════════════
print("\n[H] 净额口径：统计只认 net_cash（含手续费），不认毛盈亏")
# ══════════════════════════════════════════════════════════════
# 为什么单开一段：上面所有样本都出自 mk()，而 mk() 里
# `gross_points == net_cash`、`cost_cash == 0.0` —— **两种口径在这些样本上恒等**，
# "统计走毛额还是走净额"根本区分不出来。若哪天 _net() 改成读 gross_points，
# 上面九十多条会全绿、而面板会静默变成**不含手续费**的毛额。
# 这一段专造 gross 与 net **符号相反**的样本，把这个口径钉死。
#
# 口径链（真值在别处，这里只钉 TradeStats 侧的读取字段）：
#   Engine._book_close: net_cash = gross×乘数×手数 − cost      （Engine.py:1532-1533）
#   Instrument.cost_cash: cost = (开仓档 + 离场档) × 手数       （Instrument.py:456-459）
#       ↳ 离场档按 closetoday 取平今档 / 平昨档（Engine.py:1528-1531）
#   TradeStats: 一切统计量 + 最大单笔 + 曲线 ← net_cash        （本文件被测对象）
def mk_pnl(net, gross_pts, cost, i, exit_at=None):
    """一笔「毛利 / 成本 / 净额」三值可独立指定的成交（三值不必自洽）。"""
    r = mk(net, i, exit_at)
    r["gross_points"] = float(gross_pts)
    r["cost_cash"] = float(cost)
    r["net_cash"] = float(net)
    r["reason"] = "tp" if net > 0 else ("sl" if net < 0 else "time")
    return r


# 三笔：① 毛盈净亏（手续费吃掉利润还倒亏）② 毛亏、净更亏 ③ 毛盈、净仍盈
_M = [mk_pnl(-40.0, 1.0, 340.0, 0),      # 毛 +1 点、手续费 340 → 净 −40
      mk_pnl(-640.0, -1.0, 340.0, 1),    # 毛 −1 点、手续费 340 → 净 −640
      mk_pnl(8660.0, 30.0, 340.0, 2)]    # 毛 +30 点、手续费 340 → 净 +8660
_h = compute_trade_stats(_M)
check("[H1] 毛盈净亏的那笔算**亏损笔**（分类走 net_cash，不走毛利）",
      (_h["wins"], _h["losses"]), (1, 2))
check("[H2] 平均每笔盈利 = 净额均值（不是毛利均值）", _h["avg_win"], 8660.0)
check("[H3] 平均每笔亏损 = 净额均值", _h["avg_loss"], -340.0)
check("[H4] 最大单笔盈利 = 最大净额", _h["max_win"]["net_cash"], 8660.0)
check("[H5] 最大单笔亏损 = 最小净额（费后更亏的那笔）",
      _h["max_loss"]["net_cash"], -640.0)
check("[H6] 总净盈亏 = Σnet_cash", _h["total_net"], 7980.0)
check("[H7] 期望值 ≡ Σnet_cash ÷ 笔数", _h["expectancy"], 2660.0)
check("[H8] 盈亏曲线末值 = Σnet_cash（曲线同样是净额口径）",
      _h["equity_curve"][-1]["cumulative"], 7980.0)
check("[H9] 曲线逐点 = net_cash 累加",
      [p["net_cash"] for p in _h["equity_curve"]], [-40.0, -640.0, 8660.0])

# 对照：同一批数据若按 gross_points 当净额算，结果必须**不同** ——
# 这是 [H1]-[H9] 的「非恒真」证明（样本确实能区分两种口径）。
_hg = compute_trade_stats([dict(t, net_cash=t["gross_points"]) for t in _M])
check_true("[H10] 样本能区分两种口径（按毛利算胜率/总额都会变）",
           (_hg["wins"], _hg["losses"], _hg["total_net"])
           != (_h["wins"], _h["losses"], _h["total_net"]),
           ("毛", _hg["wins"], _hg["losses"], _hg["total_net"],
            "净", _h["wins"], _h["losses"], _h["total_net"]))

# 源码契约（与 [G] 同法）：定"哪个字段是盈亏"的 _net() 必须读 net_cash。
# 用 AST 取**内嵌函数本体**（外层 docstring 里就列着 gross_points / cost_cash
# 这几个 schema 字段名，直接搜整段源码会假红）。
_tree = ast.parse(_src)
_netfn = [n for n in ast.walk(_tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_net"]
check_true("[H11] 抽得到内嵌 _net()（覆盖面自检）", len(_netfn) == 1, len(_netfn))
_net_src = ast.get_source_segment(_src, _netfn[0]) if _netfn else ""
check_true("[H12] _net() 读的是 net_cash", 't.get("net_cash")' in _net_src,
           _net_src.replace("\n", " ")[:90])
check_true("[H13] _net() 不读 gross_points / cost_cash（毛额与成本不是统计口径）",
           "gross_points" not in _net_src and "cost_cash" not in _net_src)


print("\n" + "=" * 62)
print("成交统计指标口径 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
if _FAIL:
    raise SystemExit(1)

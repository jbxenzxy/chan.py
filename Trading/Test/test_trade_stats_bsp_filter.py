# -*- coding: utf-8 -*-
"""成交统计「买卖点类型过滤联动」契约（2026-09-26 新增需求 ③）。

背景
---------------------------------------------------------------------
用户在「显示设置 → 买卖点类型（可多选）」取消勾选某类（0/1/2/3）后，除页面显示
与自动下单外，**成交统计**也要把该类历史成交排除在汇总之外：总净盈亏 / 实际胜率 /
盈亏比 / 盈利因子 / 成交笔数 / 期望值 / 平均每笔 / 最大单笔全部据此重算，
by_bsp_type 分组也不出现被排除的类。

本文件钉死两点：
  [F1] filter_trades_by_bsp_type 的放行口径必须 == 引擎 _bsp_type_allowed：
       signal_key 中段（逗号拆段）任一段命中允许集即保留；中段无 0-3 段（人工单 /
       回放 RUN）在"已设过滤"时排除、在"未设过滤(None)"时保留。
  [F2] trades_stats 端点把被排除类的成交滤掉后，compute_trade_stats 的 count /
       total_net / by_bsp_type 等汇总指标随之变化（集成层由 AppTrader 保证，这里用
       "过滤行 → compute_trade_stats" 复刻该链路，确认口径落地正确）。

跑法：python Trading/Test/test_trade_stats_bsp_filter.py
"""
from __future__ import annotations

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
    compute_trade_stats, filter_trades_by_bsp_type)


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


def mk(trade_id, bsp_type, net_cash):
    """造一条成交：signal_key 中段 = bsp_type（逗号串也行），net_cash 为净额。"""
    return {
        "trade_id": trade_id, "symbol": "CFFEX.IF2609", "side": "long",
        "volume": 1, "entry_price": 4500.0, "exit_price": 4500.0,
        "entry_at": "2026-09-01 09:00", "exit_at": "2026-09-01 10:00",
        "reason": "trailing" if net_cash > 0 else ("sl" if net_cash < 0 else "time"),
        "gross_points": float(net_cash), "cost_cash": 0.0,
        "net_cash": float(net_cash), "bars_held": 3,
        "exit_plan_name": "run_managed", "exit_plan_params": "{}",
        "signal_key": "2026-09-01 09:00|{}|B".format(bsp_type),
    }


def main():
    print("== [F1] filter_trades_by_bsp_type 放行口径 ==")
    rows = [
        mk("T0", "0", 100.0),
        mk("T1", "1", -50.0),
        mk("T2", "2", 80.0),
        mk("T3", "3", -30.0),
        mk("T11", "1,11", 200.0),     # 逗号合并类型：与引擎同一右肩 K 的多类型
        mk("Tmanual", "manual", 10.0),  # 人工单：中段非 0-3
    ]

    # 未设过滤（None）→ 全部保留
    got = filter_trades_by_bsp_type(rows, None)
    check("None 不过滤保留全部 6 笔", len(got), 6)

    # 勾选 0/1/2，排除 3
    allowed = {"0", "1", "2"}
    got = filter_trades_by_bsp_type(rows, allowed)
    kept = {t["trade_id"] for t in got}
    check_true("排除 3 类", "T3" not in kept, kept)
    check_true("0/1/2 类保留", {"T0", "T1", "T2"} <= kept, kept)
    # [F1] 逗号合并 "1,11" 任一段(1)命中允许集 → 保留（与引擎 _bsp_type_allowed 一致）
    check_true("逗号合并 1,11 在勾选 1 时保留", "T11" in kept, kept)
    # [F1] 人工单中段无 0-3 → 已设过滤时排除
    check_true("人工单在已设过滤时排除", "Tmanual" not in kept, kept)

    # 只勾选 0 → 连 1,11 也排除（11 段不在允许集）
    got = filter_trades_by_bsp_type(rows, {"0"})
    kept = {t["trade_id"] for t in got}
    check_true("只勾 0 时 1,11 被排除", "T11" not in kept, kept)
    check_true("只勾 0 时 0 类保留", "T0" in kept, kept)

    print("== [F2] 过滤后 compute_trade_stats 汇总随口径变化 ==")
    # 全部 6 笔：0(+100) 1(-50) 2(+80) 3(-30) 1,11(+200) manual(+10)
    full = compute_trade_stats(rows)
    check("全量 count = 6", full["count"], 6)
    check("全量 total_net = 310.0",
          full["total_net"], 310.0)
    check_true("全量 by_bsp_type 含 3 类", "3" in full["by_bsp_type"])

    # 排除 3 类后：去掉 T3(-30) 与人工单 Tmanual(+10，中段无 0-3 → 一并排除)，
    # 剩 T0(+100) T1(-50) T2(+80) T11(+200) 共 4 笔，total_net = 330.0。
    # T11(1,11) 因中段段 "1" 命中允许集而保留 —— 与引擎 _bsp_type_allowed 一致。
    filtered = filter_trades_by_bsp_type(rows, {"0", "1", "2"})
    s = compute_trade_stats(filtered)
    check("排除3类后 count = 4", s["count"], 4)
    check("排除3类后 total_net = 330.0",
          s["total_net"], 330.0)
    check_true("排除3类后 by_bsp_type 不含 3", "3" not in s["by_bsp_type"])
    # 1,11 合并类型被计入汇总（count/total_net 内，未被误排除），与引擎放行一致；
    # by_bsp_type 按中段整段 "1,11" 独立成桶（非 0-3 单字符），不混入 "1" 桶。
    check_true("1,11 成交仍在汇总 count 内", s["count"] == 4, s["count"])

    print("== [F3] 端点过滤块胶水逻辑（复刻 AppTrader.trades_stats）==")
    from Trading.Infra.Records import BSP_TYPE_CHOICES  # noqa: E402

    def endpoint_derive(raw_filt):
        """复刻 trades_stats 里把 raw_filt 推导成 allowed / bsp_types_included 的块。"""
        allowed = None
        if isinstance(raw_filt, dict):
            allowed = {t for t in BSP_TYPE_CHOICES if raw_filt.get(t)}
        return (allowed,
                sorted(allowed) if allowed is not None
                else list(BSP_TYPE_CHOICES))

    a, inc = endpoint_derive({"0": True, "1": True, "2": True, "3": False})
    check("端点：勾选 0/1/2 → allowed={0,1,2}", a, {"0", "1", "2"})
    check("端点：bsp_types_included = ['0','1','2']", inc, ["0", "1", "2"])

    a, inc = endpoint_derive(None)
    check_true("端点：未设过滤 → allowed=None（全部放行）", a is None)
    check("端点：未设过滤 → included 全四类", inc, ["0", "1", "2", "3"])

    a, inc = endpoint_derive({"0": False, "1": False, "2": False, "3": False})
    check("端点：全不勾 → allowed=空集", a, set())
    check("端点：全不勾 → included=[]（全部排除）", inc, [])

    print("== 结果：PASS={} FAIL={} ==".format(_PASS, _FAIL))
    raise SystemExit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

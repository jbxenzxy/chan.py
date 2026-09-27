# -*- coding: utf-8 -*-
"""
股票页「止盈止损」图上推演护栏（v1.5 §9 步 6；2026-09-27）
====================================================================
钉住 App/AppTPSL.compute_stock_tpsl 的推演契约（纯计算、不联网、不依赖凭据）：

  ① 入场价 = 买卖点 K 线收盘价（需求⑴）；bsp.price 与收盘不一致 → 400；
  ② R = max(A, 2×ATR)（ATR 就绪时 b=2×atr）；
  ③ 保本落点 = 入场 ± 0.5R；跟踪缓冲 = 0.5R；
  ④ 边界一律严格不等（达标 / 触发恰好等于阈值都不算）；
  ⑤ 三种终态 = `ExitCheck.reason` 三值穷举：outcome 映射、画线价位=触发价、
     末段 end_date = 序列末根（§3.8 T1/T2/T3 直接搬为用例）；
  ⑥ 离场 R 数含负（T2「保本止盈」净亏 −0.10R）；
  ⑦ 卖点对称（side=short，零分支）；
  ⑧ 校验 4xx：非股票 / 空 klines / 缺字段 / 纯变体 type（"11"）/ date 不在 klines；
  ⑨ 跨日 ATR 连续（P2-4，Q5）：跨多日 bar 序列 ATR 累计不断档 —— 钉死
     「移除 Exit.on_bar 跨日清空」的新口径，防将来被"顺手恢复"而无红灯。

跑法：python Test/test_stock_tpsl.py（退出码 0/1 即判决；已注册进
Test/run_all.py 的 COMPONENTS）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from App.AppErrors import BadRequestError
from App.AppTPSL import compute_stock_tpsl

_ok = []


def check(name, cond, detail=""):
    _ok.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  —— " + str(detail)[:200]))
    return cond


_TS0 = 1767600000000  # 2026-01-05 00:00 UTC 基准（毫秒）


def _kl(seq, day, o, h, l, c):
    """构造前端 klines 条目；seq 全局序号保证 timestamp 单调，day 用于跨日构造。"""
    return {"date": "2026-01-{:02d}".format(day), "timestamp": _TS0 + seq * 3600000,
            "open": o, "high": h, "low": l, "close": c}


def _bsp(k, is_buy=True, price=None, fractal_low=9.0, fractal_high=10.8, type_="1"):
    return {"date": k["date"], "timestamp": k["timestamp"], "type": type_,
            "is_buy": is_buy, "price": price if price is not None else k["close"],
            "high": k["high"], "low": k["low"],
            "fractal_low": fractal_low, "fractal_high": fractal_high}


_ENTRY = _kl(0, 5, 10.0, 10.3, 9.8, 10.0)  # 入场根：收盘 10.0


def _payload(klines, bsp):
    return {"code": "sh600519", "freq": "d", "klines": klines, "bsp": bsp}


def main():
    print("══ ① 入场价 = 收盘价；不一致 → 400 ══")
    bsp = _bsp(_ENTRY)
    try:
        bad = _payload([_ENTRY], dict(bsp, price=10.05))
        compute_stock_tpsl(bad["code"], bad)
        check("bsp.price 与收盘不一致被 400 拒绝", False, "未抛 BadRequestError")
    except BadRequestError:
        check("bsp.price 与收盘不一致被 400 拒绝", True)
    r = compute_stock_tpsl("sh600519", _payload([_ENTRY], bsp))
    check("入场价 = 该根收盘价 10.0（需求⑴）", r["entry"]["price"] == 10.0)
    check("入场方向序列化 = long", r["entry"]["side"] == "long")

    print("══ ② R = max(A, 2×ATR)（ATR 就绪）══")
    warm = [_kl(i, 5 + i, 10.0, 10.5, 9.5, 10.0) for i in range(60)]  # 恒 TR=1
    entry2 = _kl(60, 65, 10.0, 10.5, 9.5, 10.0)
    bsp2 = _bsp(entry2, fractal_low=9.0)  # A = 1.0
    r2 = compute_stock_tpsl("sh600519", _payload(warm + [entry2], bsp2))
    check("atr = 1.0（恒 TR=1）", r2["atr"] == 1.0, r2["atr"])
    check("b = 2×ATR = 2.0", r2["b"] == 2.0, r2["b"])
    check("R = max(A=1, B=2) = 2.0", r2["r"] == 2.0, r2["r"])

    print("══ ③④⑤⑥ 三种终态（§3.8 T1/T2/T3）+ 严格不等 ══")
    # T1 已止损：有利极值仅 +0.2R，收盘 8.9 < 止损 9.0
    k1 = [_ENTRY, _kl(1, 6, 10.0, 10.2, 8.85, 8.9)]
    r1 = compute_stock_tpsl("sh600519", _payload(k1, bsp))
    check("T1 reason=sl / outcome=sl_exit",
          r1["exit"]["reason"] == "sl" and r1["exit"]["outcome"] == "sl_exit", r1["exit"])
    check("T1 画线价位 = 触发价 9.0（初始止损位）", r1["exit"]["price"] == 9.0, r1["exit"])
    check("T1 fill = 收盘 8.9", r1["exit"]["fill"] == 8.9)
    check("T1 r_multiple = −1.10（含负）", r1["exit"]["r_multiple"] == -1.1, r1["exit"])
    check("T1 末段 end_date = 序列末根（线画到最右）",
          r1["segments"][-1]["end_date"] == "2026-01-06" and
          r1["terminal"]["end_date"] == "2026-01-06", r1["segments"])
    # T2 保本止盈·净亏：同根先抬价（10.5）再跌破（9.9）
    k2 = [_ENTRY, _kl(1, 6, 10.0, 11.5, 9.8, 9.9)]
    r2t = compute_stock_tpsl("sh600519", _payload(k2, bsp))
    check("T2 reason=breakeven / outcome=be_exit",
          r2t["exit"]["reason"] == "breakeven" and r2t["exit"]["outcome"] == "be_exit",
          r2t["exit"])
    check("T2 画线价位 = 保本位 10.5（先抬价再判触发）", r2t["exit"]["price"] == 10.5, r2t["exit"])
    check("T2 fill = 9.9、r_multiple = −0.10（保本止盈也可净亏）",
          r2t["exit"]["fill"] == 9.9 and r2t["exit"]["r_multiple"] == -0.1, r2t["exit"])
    check("T2 段切换因果链：sl 段止于入场根，breakeven 段自触发根起",
          r2t["segments"][0]["phase"] == "sl" and r2t["segments"][0]["end_date"] == "2026-01-05"
          and r2t["segments"][1]["phase"] == "breakeven"
          and r2t["segments"][1]["start_date"] == "2026-01-06", r2t["segments"])
    # T3 跟踪止盈：第 1 根进跟踪（11.6，only_update），第 2 根跌破
    k3 = [_ENTRY, _kl(1, 6, 10.0, 12.1, 10.0, 11.8), _kl(2, 7, 11.8, 11.9, 11.4, 11.5)]
    r3 = compute_stock_tpsl("sh600519", _payload(k3, bsp))
    check("T3 reason=trailing / outcome=trail_exit / 画线 11.6 / fill 11.5",
          r3["exit"]["reason"] == "trailing" and r3["exit"]["price"] == 11.6
          and r3["exit"]["fill"] == 11.5 and r3["exit"]["r_multiple"] == 1.5, r3["exit"])
    check("T3 未离场时 current 为 null", r3["current"] is None)
    # 严格不等：达标恰好 1R 不进保本；触发恰好等于保护价不判破
    k4 = [_ENTRY, _kl(1, 6, 10.0, 11.0, 9.5, 10.2)]  # high=11.0 → fav 恰 1R
    r4 = compute_stock_tpsl("sh600519", _payload(k4, bsp))
    check("达标恰好 1R 不进保本（严格不等）",
          r4["exit"] is None and len(r4["segments"]) == 1
          and r4["segments"][0]["phase"] == "sl", r4["segments"])
    check("未离场时 current.phase = sl / as_of = 末根",
          r4["current"]["phase"] == "sl" and r4["current"]["as_of"] == "2026-01-06",
          r4["current"])
    k5 = [_ENTRY, _kl(1, 6, 10.0, 10.2, 8.9, 9.0)]  # close 恰 = 止损 9.0
    r5 = compute_stock_tpsl("sh600519", _payload(k5, bsp))
    check("收盘恰好等于保护价不判破（严格不等）", r5["exit"] is None, r5.get("exit"))

    print("══ ⑦ 卖点对称（side=short，零分支）══")
    bsp_s = _bsp(_ENTRY, is_buy=False, fractal_high=11.0)  # A = 1.0 → R = 1，止损 11.0
    k6 = [_ENTRY, _kl(1, 6, 10.0, 10.5, 9.5, 11.1)]  # 收盘 11.1 > 止损 11.0
    r6 = compute_stock_tpsl("sh600519", _payload(k6, bsp_s))
    check("卖点：止损位 = 入场 + R = 11.0", r6["segments"][0]["price"] == 11.0, r6["segments"])
    check("卖点：reason=sl / r_multiple = −1.10（short 被止损在入场价上方 = 亏损，符号经 Side 枚举）",
          r6["exit"]["reason"] == "sl" and r6["exit"]["r_multiple"] == -1.1, r6["exit"])
    # short 的盈利出场走跟踪层：先跌 3R（fav=3R > 2R 进跟踪，跟踪位 = low 7.9 + 0.5R = 8.4），
    # 再收 8.5 > 8.4 判破 → fill 8.5，r_multiple = (8.5−10)×(−1) = +1.50（盈利为正）
    k7 = [_ENTRY, _kl(1, 6, 10.0, 10.1, 7.9, 8.0), _kl(2, 7, 8.0, 8.6, 8.4, 8.5)]
    r7s = compute_stock_tpsl("sh600519", _payload(k7, bsp_s))
    check("卖点：reason=trailing / 画线 8.4 / r_multiple = +1.50（(fill−entry)×SHORT.value，盈利为正）",
          r7s["exit"]["reason"] == "trailing" and r7s["exit"]["r_multiple"] == 1.5
          and r7s["exit"]["price"] == 8.4, r7s["exit"])

    print("══ ⑧ 校验 4xx ══")
    cases = [
        ("非股票代码", {"code": "CFFEX.IF2509", "freq": "d", "klines": [_ENTRY], "bsp": bsp}),
        ("空 klines", {"code": "sh600519", "freq": "d", "klines": [], "bsp": bsp}),
        ("纯变体 type '11'", {"code": "sh600519", "freq": "d", "klines": [_ENTRY],
                              "bsp": dict(bsp, type="11")}),
        ("缺 fractal_low", {"code": "sh600519", "freq": "d", "klines": [_ENTRY],
                            "bsp": {k: v for k, v in bsp.items() if k != "fractal_low"}}),
        ("bsp.date 不在 klines", {"code": "sh600519", "freq": "d", "klines": [_ENTRY],
                                   "bsp": dict(bsp, date="2026-02-01")}),
    ]
    for tag, body in cases:
        try:
            compute_stock_tpsl(body["code"], body)
            check("{} → 400".format(tag), False, "未抛 BadRequestError")
        except BadRequestError:
            check("{} → 400".format(tag), True)

    print("══ ⑨ 跨日 ATR 连续（P2-4，Q5：移除 Exit.on_bar 跨日清空）══")
    # 25 根 bar 各自独立日期（每根都"跨日"）：若跨日清空回潮，入场时缓冲只剩 1 根
    # → ATR 未就绪 → R 退化到 A=0.01；连续口径下 ATR=1.0 → R = 2×ATR = 2.0。
    seq = [_kl(i, 5 + i, 10.0, 10.5, 9.5, 10.0) for i in range(25)]
    entry3 = seq[-1]
    bsp3 = _bsp(entry3, fractal_low=9.99)  # A = 0.01（刻意极小，逼 R 只能来自 B）
    r7 = compute_stock_tpsl("sh600519", _payload(seq, bsp3))
    check("跨日连续：ATR = 1.0（未被跨日清空打断）", r7["atr"] == 1.0, r7["atr"])
    check("跨日连续：R = 2×ATR = 2.0（而非退化为 A=0.01）", r7["r"] == 2.0, r7["r"])

    n_pass = sum(1 for x in _ok if x)
    print("\n合计: {} 通过 / {} 失败".format(n_pass, len(_ok) - n_pass))
    return len(_ok) == n_pass


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

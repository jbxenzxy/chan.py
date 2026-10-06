# -*- coding: utf-8 -*-
"""
股票页「止盈止损」图上推演护栏（v1.5 §9 步 6；2026-09-27）
====================================================================
钉住 App/AppTPSL.compute_stock_tpsl 的推演契约（纯计算、不联网、不依赖凭据）：

  ① 入场价 = 买卖点 K 线收盘价（需求⑴）；bsp.price 与收盘不一致 → 400；
  ② R = max(A, 1×ATR)（ATR 就绪时 b=1×atr；2026-10-06 由 2×ATR 收紧）；
  ③ 保本落点 = 入场 ± 0.5R；跟踪缓冲与盈亏比走股票侧覆盖口径
     （AppTPSL._STOCK_EXIT_OVERRIDES，2026-09-27 拍板，只影响股票推演）；
  ④ 边界一律严格不等（达标 / 触发恰好等于阈值都不算）；
  ⑤ 三种终态 = `ExitCheck.reason` 三值穷举：outcome 映射、画线价位=触发价、
     末段 end_date = 序列末根（§3.8 T1/T2/T3 直接搬为用例）；
  ⑥ 离场 R 数含负（T2「保本止盈」净亏 −0.10R）；
  ⑦ 卖点对称（side=short，零分支）；
  ⑧ 校验 4xx：非股票 / 空 klines / 缺字段 / 纯变体 type（"11"）/ date 不在 klines；
  ⑨ 跨日 ATR 连续（P2-4，Q5）：跨多日 bar 序列 ATR 累计不断档 —— 钉死
     「移除 Exit.on_bar 跨日清空」的新口径，防将来被"顺手恢复"而无红灯；
  ⑩ 前端分段标签口径（2026-09-27 用户三/四/五/六改）：从 Frontend/app.js 抽
     真函数 tpslSegLabel 到 node 里跑 —— 止损/保本段 `止损 9.00 R（…）` /
     `保本 10.50 R（…）`；跟踪段区分 初始跟踪（3R 阈值线，`初始跟踪 21.63
     3R（2.64）`）与 移动跟踪（逐级上移线，`移动跟踪 23.21 4.78R（4.21）`，
     4.78R = 锁盈 R 数、4.21 = 对应点数）；终态段追加 已止损/已止盈；
     node 不在位则 SKIP。
  ⑪ 终态段右端 = 触发止损止盈的那根 K 线（2026-09-27 用户六改⑴）：
     不再延伸到序列末根，否则看不出止盈/止损发生在哪根。

跑法：python Test/test_stock_tpsl.py（退出码 0/1 即判决；已注册进
Test/run_all.py 的 COMPONENTS）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from App.AppErrors import BadRequestError
from App.AppTPSL import _STOCK_EXIT_OVERRIDES, compute_stock_tpsl

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

    print("══ ② R = max(A, 1×ATR)（ATR 就绪）══")
    warm = [_kl(i, 5 + i, 10.0, 10.5, 9.5, 10.0) for i in range(60)]  # 恒 TR=1
    entry2 = _kl(60, 65, 10.0, 10.5, 9.5, 10.0)
    bsp2 = _bsp(entry2, fractal_low=9.0)  # A = 1.0
    r2 = compute_stock_tpsl("sh600519", _payload(warm + [entry2], bsp2))
    check("atr = 1.0（恒 TR=1）", r2["atr"] == 1.0, r2["atr"])
    check("b = 1×ATR = 1.0", r2["b"] == 1.0, r2["b"])
    check("R = max(A=1, B=1) = 1.0", r2["r"] == 1.0, r2["r"])
    check("股票侧覆盖生效：params 与 _STOCK_EXIT_OVERRIDES 逐键一致（⑴）",
          all(r2["params"][k] == v for k, v in _STOCK_EXIT_OVERRIDES.items()),
          {k: (r2["params"].get(k), v) for k, v in _STOCK_EXIT_OVERRIDES.items()})

    print("══ ③④⑤⑥ 三种终态（§3.8 T1/T2/T3）+ 严格不等 ══")
    # T1 已止损：有利极值仅 +0.2R，收盘 8.9 < 止损 9.0
    k1 = [_ENTRY, _kl(1, 6, 10.0, 10.2, 8.85, 8.9)]
    r1 = compute_stock_tpsl("sh600519", _payload(k1, bsp))
    check("T1 reason=sl / outcome=sl_exit",
          r1["exit"]["reason"] == "sl" and r1["exit"]["outcome"] == "sl_exit", r1["exit"])
    check("T1 画线价位 = 触发价 9.0（初始止损位）", r1["exit"]["price"] == 9.0, r1["exit"])
    check("T1 fill = 收盘 8.9", r1["exit"]["fill"] == 8.9)
    check("T1 r_multiple = −1.10（含负）", r1["exit"]["r_multiple"] == -1.1, r1["exit"])
    check("T1 末段 end_date = 序列末根（离场即末根场景）",
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
    # T3 跟踪止盈（3:1 + 缓冲 1R）：第 1 根 high 13.2 → fav=3.2R > 3R 进跟踪
    #   （跟踪位 = 13.2 − 1×R = 12.2，only_update；当根收盘 12.5 在新保护价上方 → 存活），
    #   第 2 根收 12.1 < 12.2 判破 → fill 12.1，r_multiple = +2.1
    k3 = [_ENTRY, _kl(1, 6, 10.0, 13.2, 10.0, 12.5), _kl(2, 7, 12.5, 12.8, 12.1, 12.1)]
    r3 = compute_stock_tpsl("sh600519", _payload(k3, bsp))
    check("T3 reason=trailing / outcome=trail_exit / 画线 12.2 / fill 12.1",
          r3["exit"]["reason"] == "trailing" and r3["exit"]["price"] == 12.2
          and r3["exit"]["fill"] == 12.1 and r3["exit"]["r_multiple"] == 2.1, r3["exit"])
    check("T3 未离场时 current 为 null", r3["current"] is None)
    # 严格不等：达标恰好 1R 不进保本；触发恰好等于保护价不判破；fav 恰 3R 不进跟踪
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
    k5b = [_ENTRY, _kl(1, 6, 10.0, 13.0, 9.5, 12.5)]  # high 13.0 → fav 恰 3R
    r5b = compute_stock_tpsl("sh600519", _payload(k5b, bsp))
    check("有利极值恰好 3R 不进跟踪（严格不等）",
          r5b["exit"] is None and all(s["phase"] != "trailing" for s in r5b["segments"]),
          r5b["segments"])

    print("══ ⑦ 卖点对称（side=short，零分支）══")
    bsp_s = _bsp(_ENTRY, is_buy=False, fractal_high=11.0)  # A = 1.0 → R = 1，止损 11.0
    k6 = [_ENTRY, _kl(1, 6, 10.0, 10.5, 9.5, 11.1)]  # 收盘 11.1 > 止损 11.0
    r6 = compute_stock_tpsl("sh600519", _payload(k6, bsp_s))
    check("卖点：止损位 = 入场 + R = 11.0", r6["segments"][0]["price"] == 11.0, r6["segments"])
    check("卖点：reason=sl / r_multiple = −1.10（short 被止损在入场价上方 = 亏损，符号经 Side 枚举）",
          r6["exit"]["reason"] == "sl" and r6["exit"]["r_multiple"] == -1.1, r6["exit"])
    # short 的盈利出场走跟踪层：先跌 3.1R（fav > 3R 进跟踪，跟踪位 = low 6.9 + 1R = 7.9），
    # 再收 8.0 > 7.9 判破 → fill 8.0，r_multiple = (8.0−10)×(−1) = +2.00（盈利为正）
    k7 = [_ENTRY, _kl(1, 6, 10.0, 10.1, 6.9, 7.0), _kl(2, 7, 7.0, 8.1, 7.8, 8.0)]
    r7s = compute_stock_tpsl("sh600519", _payload(k7, bsp_s))
    check("卖点：reason=trailing / 画线 7.9 / r_multiple = +2.00（(fill−entry)×SHORT.value，盈利为正）",
          r7s["exit"]["reason"] == "trailing" and r7s["exit"]["r_multiple"] == 2.0
          and r7s["exit"]["price"] == 7.9, r7s["exit"])

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

    # ⑧b **正向**白名单：只钉「坏代码被拒」是不够的 —— 把白名单缩成 ("sh",)
    #     照样全绿，而 ds 扩展指数（中证2000）就是被这么漏掉的：页面能打开它、
    #     `AppUtils.is_index` 也认它，入口却回「未知市场前缀」。故页面可选中的
    #     市场必须整体放行，且白名单来源 = `App.AppUtils.STOCK_MARKETS`（单一事实源，
    #     回测入口 `AppBacktest._split_code` 用同一份）。
    print("══ ⑧b 市场前缀白名单（正向，SSOT = App.AppUtils.STOCK_MARKETS）══")
    from App.AppUtils import STOCK_MARKETS as _mk
    check("白名单 = ('sh','sz','bj','hk','ds')（含 ds 扩展指数）",
          tuple(_mk) == ("sh", "sz", "bj", "hk", "ds"), repr(_mk))
    for _code in ("sh600519", "sz002190", "bj430047", "hk00700", "ds932000"):
        _body = dict(_payload(k7, bsp_s), code=_code)
        try:
            compute_stock_tpsl(_code, _body)
            check("{} 被放行（页面可选市场）".format(_code), True)
        except BadRequestError as _e:
            check("{} 被放行（页面可选市场）".format(_code), False, str(_e))

    print("══ ⑨ 跨日 ATR 连续（P2-4，Q5：移除 Exit.on_bar 跨日清空）══")
    # 25 根 bar 各自独立日期（每根都"跨日"）：若跨日清空回潮，入场时缓冲只剩 1 根
    # → ATR 未就绪 → R 退化到 A=0.01；连续口径下 ATR=1.0 → R = 1×ATR = 1.0。
    seq = [_kl(i, 5 + i, 10.0, 10.5, 9.5, 10.0) for i in range(25)]
    entry3 = seq[-1]
    bsp3 = _bsp(entry3, fractal_low=9.99)  # A = 0.01（刻意极小，逼 R 只能来自 B）
    r9 = compute_stock_tpsl("sh600519", _payload(seq, bsp3))
    check("跨日连续：ATR = 1.0（未被跨日清空打断）", r9["atr"] == 1.0, r9["atr"])
    check("跨日连续：R = 1×ATR = 1.0（而非退化为 A=0.01）", r9["r"] == 1.0, r9["r"])

    print("══ ⑩ 前端分段标签口径（node 抽真函数 tpslSegLabel；node 不在位则 SKIP）══")
    import io as _io
    import re as _re
    import shutil as _shutil
    import subprocess as _subprocess
    import tempfile as _tempfile
    node = _shutil.which("node")
    if not node:
        check("node 不在位，标签口径静态层 SKIP", True)
    else:
        appjs = _io.open(os.path.join(ROOT, "Frontend", "app.js"),
                         encoding="utf-8").read()

        def _extract(fn_name):
            pat = ("        function " + fn_name +
                   r"\([^)]*\) \{[\s\S]*?\n        \}")
            m = _re.search(pat, appjs)
            assert m, "app.js 抽取失败: " + fn_name
            return m.group(0)

        harness = _extract("_fmtPrice") + "\n" + _extract("tpslSegLabel") + "\n" + (
            "const P = (o) => Object.assign({ params: { win_loss_ratio: 3.0 },"
            " entry: { price: 19.0, side: 'long' } }, o);\n"
            "const out = [];\n"
            "out.push(tpslSegLabel({ phase: 'sl', price: 9.0 }, P({}), null));\n"
            "out.push(tpslSegLabel({ phase: 'sl', price: 9.0, terminal: true },\n"
            "    P({ r: 1.0, exit: { r_multiple: -1.1 },"
            " terminal: { outcome: 'sl_exit' } }), null));\n"
            "out.push(tpslSegLabel({ phase: 'breakeven', price: 10.5, terminal: true },\n"
            "    P({ r: 0.5, exit: { r_multiple: -0.1 },"
            " terminal: { outcome: 'be_exit' } }), null));\n"
            "out.push(tpslSegLabel({ phase: 'trailing', price: 21.63 },"
            " P({ r: 0.88 }), 'initial'));\n"
            "out.push(tpslSegLabel({ phase: 'trailing', price: 21.63, terminal: true },\n"
            "    P({ r: 0.88, terminal: { outcome: 'trail_exit' } }), 'initial'));\n"
            "out.push(tpslSegLabel({ phase: 'trailing', price: 23.21 },"
            " P({ r: 0.88 }), 'moved'));\n"
            "out.push(tpslSegLabel({ phase: 'trailing', price: 23.21, terminal: true },\n"
            "    P({ r: 0.88, terminal: { outcome: 'trail_exit' } }), 'moved'));\n"
            "console.log(out.join('\\n'));\n")
        jf = _tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
        jf.write(harness)
        jf.close()
        try:
            proc = _subprocess.run([node, jf.name], capture_output=True, text=True,
                                   encoding="utf-8", timeout=30)
            lines = [x for x in proc.stdout.splitlines() if x.strip()]
        finally:
            os.unlink(jf.name)
        check("node 执行成功且输出 7 条",
              proc.returncode == 0 and len(lines) == 7,
              proc.stderr[:160] or proc.stdout[:160])
        if proc.returncode == 0 and len(lines) == 7:
            check("止损段（无终态、无 r）：止损 9.00", lines[0] == "止损 9.00", lines[0])
            check("止损终态：止损 9.00 R（1） 已止损",
                  lines[1] == "止损 9.00 R（1） 已止损", lines[1])
            check("保本终态：保本 10.50 R（0.5） 已止盈",
                  lines[2] == "保本 10.50 R（0.5） 已止盈", lines[2])
            check("初始跟踪（非终态）：初始跟踪 21.63 3R（2.64）（⑸，3×R）",
                  lines[3] == "初始跟踪 21.63 3R（2.64）", lines[3])
            check("初始跟踪终态：… 已止盈",
                  lines[4] == "初始跟踪 21.63 3R（2.64） 已止盈", lines[4])
            check("移动跟踪（非终态）：移动跟踪 23.21 4.78R（4.21）（⑹，锁盈 R 与点数）",
                  lines[5] == "移动跟踪 23.21 4.78R（4.21）", lines[5])
            check("移动跟踪终态：… 已止盈",
                  lines[6] == "移动跟踪 23.21 4.78R（4.21） 已止盈", lines[6])

    print("══ ⑪ 终态段右端 = 触发止损止盈的那根 K 线（2026-09-27 用户六改⑴）══")
    k8 = [_ENTRY, _kl(1, 6, 10.0, 10.2, 8.85, 8.9),   # bar1 收 8.9 < 止损 9.0 → 触发离场
          _kl(2, 7, 9.0, 9.6, 8.8, 9.4)]              # bar2 在离场之后，不应被终态段覆盖
    r8 = compute_stock_tpsl("sh600519", _payload(k8, bsp))
    check("终态段 end_date = 触发 bar（2026-01-06）",
          r8["terminal"]["end_date"] == "2026-01-06"
          and r8["segments"][-1]["end_date"] == "2026-01-06", r8["terminal"])
    check("终态段不再延伸到序列末根（2026-01-07）",
          r8["terminal"]["end_date"] != k8[-1]["date"], r8["terminal"])

    n_pass = sum(1 for x in _ok if x)
    print("\n合计: {} 通过 / {} 失败".format(n_pass, len(_ok) - n_pass))
    return len(_ok) == n_pass


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

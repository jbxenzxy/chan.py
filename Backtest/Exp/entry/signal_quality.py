# -*- coding: utf-8 -*-
"""信号质量回测：缠论买/卖点入场后，结构是「走出反向笔」还是「原笔延伸」。

用户定义（以上涨为例，下跌镜像）
--------------------------------
  · 买点入场后**走出了向上笔** ⇒ 该买点质量好（原向下笔被终结、新结构成立）；
  · 买点入场后**没有向上笔、原向下笔直接延伸（创新低）** ⇒ 该买点质量差。

口径一：结构口径（忠实于用户描述，逐帧流式、无未来函数）
--------------------------------------------------------
  信号首见帧 = t0；锚点 = 信号所依据的笔 `bsp.bi` 的**端点** klu.idx
  （与 `Backtest/Runner.bsp_to_dict` 取 fractal_low/high 的落点同源）：

    走出反向笔 SUCC : 出现新的**反向笔**（买点→向上笔 / 卖点→向下笔）且其
                      **起点** klu.idx ≥ 锚点 ⇒ 原笔被终结、新笔成立
    原笔延伸   FAIL : 价格**先**打穿信号的分型极值
                      （买点 bar.low < fractal_low / 卖点 bar.high > fractal_high）
    超时     CENSOR : 窗口内两者都没发生
  两者取**先到**，同帧同时命中记 FAIL（保守）。

  ⚠ **口径一的边界（2026-10-07 更正 —— 原表述「`FAIL ⟺ 结构止损被触碰`」是错的）**

    `FAIL` 只等价于"**价格穿过了信号分型极值**"，**不等价于**"结构止损被跌破"。
    两件事在代码里是**两族不同结局**，逐笔复算见 `exit_fate.py`：

      · L1 初始保护价 = 入场价 ∓ R（`Strategy/Exit.py:353`），
        `R = max(A, atr_sl_multiple×ATR)`（`Exit.py:321`，`atr_sl_multiple=1.0`），
        `A` = 入场价到分型极值的距离（`Exit.py:289`）⇒ **保护价 ≤ 分型极值**。
        所以"穿过分型极值"是"跌破保护价"的**必要不充分**条件。
        （反例：`sz000601 2014-06-13` A=0.160 < R=0.239，分型被破却以 +9.58% 跟踪离场。）
      · 反向更常见：保护价可以在**反向笔早已走出之后**才被跌破
        （`Backtest/Runner.py:423` 每根闭合 bar 都调 `check()`，持仓未平就一直判）。
        此时原笔并未延伸，本口径不会记 `FAIL`。

    ⇒ 结构止损离场 = **原笔延伸型**（反向笔没走出，前提当场破裂）
                     + **走出反向笔型**（浮盈没够到保本触发，最终回到初始保护价）。
    本口径只能回答"信号的前提有没有当场被破"（这一问本身有价值），
    **不能**回答"这笔最终是不是止损离场"，更**不能**用来论证买卖点优劣。
    完整分解见 `exit_fate.py`（真实 `LayeredExitPolicy` 逐根回放，
    与 `Backtest.Runner.run` 的笔数/离场层分布完全一致）。

口径二：事件研究（超额收益，回答"信号本身有没有 alpha"）
--------------------------------------------------------
  对每只标的：
    · 基准 = 该标的**全部** K 线的 H 根前瞻收益均值（= 随机入场的期望）
    · 信号 = 全部买卖点入场后的 H 根前瞻收益均值
    · 超额 = 信号 − 基准（同一标的内部相减 ⇒ 自然控制标的自身涨跌）
  再按标的配对做 bootstrap，检验"超额是否显著 > 0"。

  只有口径二能区分：**「买点只是踩上了标的整体上涨」** vs **「买点本身挑得准」**。

跨周期
------
周期由 `--freq` 决定，五个周期（w / d / 30m / 15m / 5m）**全部走页面同源数据**
（`tdx_source.py` → `CTdxAPI.fetch_main_level`，通达信本地 vipdoc + 自实现前复权）。
根数由 vipdoc 决定，**不截断**：页面看多少根，这里就是多少根 —— 截断会改变笔的
起点，等于换了一条序列（实测日K 差 43 笔、周K 差 25 笔，见 `data_parity.py`）。

⚠ 每一期只有一段历史（本机 vipdoc：日K 1393 根 ≈ 5.5 年、周K 294 根 ≈ 5.7 年、
  分钟线 ≈ 242 个交易日），这是**页面实际能看到的数据量**，不是可以随便加长的。
  ⇒ 跨周期比较时**不要**拿"H 根"当同一段时间：周K 的 H=30 根 ≈ 7 个月，
  5分 的 H=30 根 ≈ 0.6 个交易日。口径二只在**同周期内部**做减基准。

附带字段（供 `sweep_regret.py` 用）
----------------------------------
  `mfe_A` = 窗口内最大有利偏移 / A（**只在 `bars ≤ horizon` 内累计**）
  `mae_A` = 窗口内最大不利偏移 / A
  `mfe_fail_A` = **被打止损之前**已累积的最大有利偏移 / A
      ⇒ `mfe_fail_A ≥ 1 且 outcome == fail` 就是用户说的「浮盈 1A 了反而被扫掉」。

用法::

    python signal_quality.py --freq d  --limit 800 --horizon 30 \\
        --procs 8 --out sq_d.json
    python signal_quality.py --freq 30m --limit 300 --horizon 30 \\
        --procs 5 --out sq_30m.json

`--n` 已废弃（根数由 vipdoc 决定，不做截断）；`--workers` 已改名 `--procs`，
因为这一步是纯 CPU 活，线程池会被 GIL 串行化（见 `common/bench_pool.py`）。
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
def _find_repo(_p):
    """向上找到仓库根（含 Chan.py + DataAPI 的那一级）—— 从任意深度都成立。"""
    for _ in range(7):
        if (os.path.isfile(os.path.join(_p, "Chan.py"))
                and os.path.isdir(os.path.join(_p, "DataAPI"))):
            return _p
        _p = os.path.dirname(_p)
    return ""


EXP = HERE if os.path.basename(HERE) == "Exp" else os.path.dirname(HERE)
# 优先级：显式 CHAN_REPO > 向上找仓库根 > 沙盒布局（<work>/wt_latest）
REPO = (os.environ.get("CHAN_REPO") or _find_repo(HERE)
        or os.path.join(os.path.dirname(os.path.dirname(EXP)), "wt_latest"))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(EXP, "common"))     # 共享层（tdx_source / exp_policy …）
sys.path.insert(0, HERE)
os.chdir(REPO)

from tdx_source import records as _page_records, stock_list   # noqa: E402
from Backtest.Runner import default_chan_config, date_fmt_of, kl_type_of  # noqa: E402


def recs_src(code: str, freq: str, n: int = None, src: str = "tdx") -> list:
    """**页面同源** K 线（`tdx_source` → 通达信 vipdoc + 自实现前复权）。

    `src` / `n` 仅为兼容旧签名保留：`src="ext"` 走旧的腾讯/新浪链路（**非页面口径**，
    只用于 `data_parity.py` 对照），默认 `"tdx"` 才是页面口径。
    """
    if src in ("tencent", "sina", "ext"):
        from datetime import datetime
        if src == "sina":
            from fetch_min import fetch as fmin
            return [dict(r, dt=datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S"))
                    for r in fmin(code, freq, n or 2000)]
        from fetch_kline import fetch as fk
        return [dict(r, dt=datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S"))
                for r in fk(code, freq, n or 800)]
    return _page_records(code, freq)


@dataclass
class Case:
    code: str
    freq: str
    side: int           # +1 做多 / -1 做空
    btype: str
    t0: int             # 信号首见帧号（1 起）
    date: str
    entry: float
    ext: float          # fractal_low(多) / fractal_high(空)
    A: float            # |entry − ext|
    anchor: int         # 信号所依据的笔端点 klu.idx
    succ_frame: int = 0
    succ_sure: bool = False
    fail_frame: int = 0
    mfe: float = 0.0
    mae: float = 0.0
    mfe_at_fail: float = None   # 被打止损**之前**已达到的最大有利偏移
    mae_at_fail: float = None
    bars: int = 0
    ret_H: float = None
    post_H: float = None    # 「确认后入场」：从反向笔确认那根起算的 H 根收益


def _finalize(c: Case, horizon: int) -> dict:
    """结算一条信号。**只有窗口内的笔事件才算数**（超过 horizon 根才出现的反向笔
    不记 SUCC —— 否则"猴年马月才走出一笔"会被算成质量好，指标就废了）。"""
    s = c.succ_frame - c.t0 if c.succ_frame else None
    f = c.fail_frame - c.t0 if c.fail_frame else None
    s = s if (s is not None and s <= horizon) else None
    f = f if (f is not None and f <= horizon) else None
    if s is not None and f is not None:
        outcome = "succ" if s < f else "fail"
    elif s is not None:
        outcome = "succ"
    elif f is not None:
        outcome = "fail"
    else:
        outcome = "censor"
    return {
        "code": c.code, "freq": c.freq, "side": "long" if c.side > 0 else "short",
        "type": c.btype, "date": c.date, "entry": c.entry,
        "A": c.A, "A_pct": (c.A / c.entry * 100) if c.entry else 0.0,
        "outcome": outcome,
        "succ_bars": s, "fail_bars": f,
        "succ_sure": bool(c.succ_sure),
        "mfe": c.mfe, "mae": c.mae,
        "mfe_A": (c.mfe / c.A) if c.A > 0 else None,
        "mae_A": (c.mae / c.A) if c.A > 0 else None,
        "mfe_fail_A": (c.mfe_at_fail / c.A)
        if (c.A > 0 and c.mfe_at_fail is not None) else None,
        "ret_H": c.ret_H,
        "post_H": c.post_H,
        "bars": c.bars,
    }


def scan(code: str, freq: str, n: int = None, horizon: int = 30, src: str = "tdx"):
    """跑一只标的 ⇒ (信号质量记录列表, 基准统计)。

    基准 = 该标的**全部** K 线的 horizon 根前瞻收益（多空方向按"做多"计，
    卖点用镜像），即"随机入场"的期望 —— 事件研究的对照组。
    """
    import Chan
    from DataAPI import TdxAPI
    from Common.CEnum import AUTYPE

    recs = recs_src(code, freq, n, src)
    if not recs:
        return [], {}
    date_fmt = date_fmt_of(freq)
    cases: list = []
    closes: list = []
    seen: set = set()
    frame = 0
    with TdxAPI.tdx_data_context(recs):
        chan = Chan.CChan(
            code="{}{}".format(code[:2], code[2:]), begin_time=None, end_time=None,
            data_src="custom:TdxAPI.CTdxAPI", lv_list=[kl_type_of(freq)],
            config=default_chan_config(), autype=AUTYPE.NONE, market_type="stock")
        for _ in chan.step_load():
            frame += 1
            lst = chan[0]
            cur_klu = lst[-1][-1]
            hi, lo, cl = (float(cur_klu.high), float(cur_klu.low),
                          float(cur_klu.close))
            closes.append(cl)

            # 每帧只扫一次笔表：最新反向笔的**起点 idx**
            #（笔序列起点沿时间单调递增 ⇒「最新反向笔起点 ≥ 锚点」等价于「存在这样一笔」）
            newest_up, newest_dn = -1, -1
            up_sure = dn_sure = False
            for bi in lst.bi_list:
                b = bi.get_begin_klu().idx
                if bi.is_up():
                    if b > newest_up:
                        newest_up, up_sure = b, bool(bi.is_sure)   # is_sure 是 property
                elif b > newest_dn:
                    newest_dn, dn_sure = b, bool(bi.is_sure)

            # 1) 推进已登记案例
            #    顺序很关键：**先判事件、后累计极值** —— 否则「被打止损之前」的浮盈
            #    会被"打止损那一根"自身的最高价污染（那一根的高点属于事后信息）。
            #    另外极值只在测量窗口 `bars <= horizon` 内累计，否则 mfe 会一路累积到
            #    样本末尾（实测会虚高到 11×A，完全不能用来谈"浮盈锁没锁住"）。
            for c in cases:
                c.bars += 1
                if c.side > 0:
                    if not c.fail_frame and lo < c.ext:
                        c.fail_frame = frame
                        c.mfe_at_fail, c.mae_at_fail = c.mfe, c.mae
                    if not c.succ_frame and newest_up >= c.anchor:
                        c.succ_frame, c.succ_sure = frame, up_sure
                    if c.bars <= horizon:
                        c.mfe = max(c.mfe, hi - c.entry)
                        c.mae = max(c.mae, c.entry - lo)
                else:
                    if not c.fail_frame and hi > c.ext:
                        c.fail_frame = frame
                        c.mfe_at_fail, c.mae_at_fail = c.mfe, c.mae
                    if not c.succ_frame and newest_dn >= c.anchor:
                        c.succ_frame, c.succ_sure = frame, dn_sure
                    if c.bars <= horizon:
                        c.mfe = max(c.mfe, c.entry - lo)
                        c.mae = max(c.mae, hi - c.entry)

            # 2) 登记本帧首见信号
            for bsp in lst.bs_point_lst.bsp_iter():
                key = (str(bsp.klu.time), bool(bsp.is_buy))
                if key in seen:
                    continue
                if bsp.klu.idx != cur_klu.idx:
                    continue
                seen.add(key)
                try:
                    f_klu = bsp.bi.get_end_klu()
                except Exception:                      # noqa: BLE001
                    continue
                is_buy = bool(bsp.is_buy)
                entry = float(bsp.klu.close)
                ext = float(f_klu.low) if is_buy else float(f_klu.high)
                cases.append(Case(
                    code=code, freq=freq, side=(1 if is_buy else -1),
                    btype=str(bsp.type2str()),
                    t0=frame, date=cur_klu.time.toFmtStr(date_fmt),
                    entry=entry, ext=ext, A=abs(entry - ext),
                    anchor=int(f_klu.idx)))

    # 统一 finalize 并补 ret_H / post_H（用同一份 closes ⇒ 与基准同源）
    out = []
    for c in cases:
        i = c.t0 + horizon - 1
        if i < len(closes) and c.entry:
            c.ret_H = (closes[i] - c.entry) * c.side / c.entry
        # 「确认后入场」：从反向笔确认那一根的收盘起算（不落在信号那根上涨的段里）
        if c.succ_frame:
            j = c.succ_frame - 1
            k2 = j + horizon
            if k2 < len(closes) and closes[j]:
                c.post_H = (closes[k2] - closes[j]) * c.side / closes[j]
        out.append(_finalize(c, horizon))

    base = _baseline(closes, horizon)
    if base:
        base["code"] = code
    return out, base


def _baseline(closes: list, horizon: int) -> dict:
    """全序列 horizon 根前瞻收益（做多口径）——"随机入场"的对照组。"""
    rs = []
    for i in range(len(closes) - horizon):
        p0 = closes[i]
        if p0:
            rs.append((closes[i + horizon] - p0) / p0)
    if not rs:
        return {}
    rs.sort()
    return {"n": len(rs), "mean": sum(rs) / len(rs),
            "median": rs[len(rs) // 2]}


# ════════════════════════════════════════════════════════════════════
# 汇总
# ════════════════════════════════════════════════════════════════════
def _avg(v):
    return (sum(v) / len(v)) if v else 0.0


def agg(rows: list) -> dict:
    if not rows:
        return {"n": 0}
    n = len(rows)
    n_s = sum(1 for r in rows if r["outcome"] == "succ")
    n_f = sum(1 for r in rows if r["outcome"] == "fail")
    ret = [r["ret_H"] for r in rows if r["ret_H"] is not None]
    return {
        "n": n,
        "succ_pct": n_s / n * 100, "fail_pct": n_f / n * 100,
        "censor_pct": (n - n_s - n_f) / n * 100,
        "succ_pct_sure": sum(1 for r in rows if r["outcome"] == "succ"
                             and r["succ_sure"]) / n * 100,
        "succ_bars": _avg([r["succ_bars"] for r in rows
                           if r["succ_bars"] is not None]),
        "fail_bars": _avg([r["fail_bars"] for r in rows
                           if r["fail_bars"] is not None]),
        "ret_all": _avg(ret) * 100,
        "ret_succ": _avg([r["ret_H"] for r in rows if r["outcome"] == "succ"
                          and r["ret_H"] is not None]) * 100,
        "ret_fail": _avg([r["ret_H"] for r in rows if r["outcome"] == "fail"
                          and r["ret_H"] is not None]) * 100,
        "mfeA_succ": _avg([r["mfe_A"] for r in rows if r["outcome"] == "succ"
                           and r["mfe_A"] is not None]),
        "mfeA_fail": _avg([r["mfe_A"] for r in rows if r["outcome"] == "fail"
                           and r["mfe_A"] is not None]),
        "A_pct": _avg([r["A_pct"] for r in rows]),
    }


def line(title: str, rows: list) -> None:
    m = agg(rows)
    if not m["n"]:
        print("{:<28} （无样本）".format(title))
        return
    print("{:<28} {:>6} {:>7} {:>7} {:>7} {:>7} {:>8} {:>9} {:>9} {:>7}".format(
        title, m["n"], "{:.1f}%".format(m["succ_pct"]),
        "{:.1f}%".format(m["fail_pct"]), "{:.1f}%".format(m["censor_pct"]),
        "{:.1f}".format(m["succ_bars"]), "{:.1f}".format(m["A_pct"]),
        "{:+.2f}%".format(m["ret_all"]), "{:+.2f}%".format(m["ret_succ"]),
        "{:+.2f}%".format(m["ret_fail"])))


def paired_boot(d, iters=10000, seed=7):
    if not d:
        return 0.0, 1.0, 0
    obs = sum(d) / len(d)
    rnd = random.Random(seed)
    n = len(d)
    cnt = sum(1 for _ in range(iters)
              if sum(d[rnd.randrange(n)] for _ in range(n)) / n <= 0)
    p = 2 * min(cnt / iters, 1 - cnt / iters)
    return obs, min(p, 1.0), n


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=None,
                    help="已废弃：根数由 vipdoc 决定，不再截断（保留仅为兼容旧命令行）")
    ap.add_argument("--src", default="tdx",
                    help="数据源：tdx=页面同源（默认）。ext/tencent/sina=旧外部链路，**非页面口径**")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--workers", type=int, default=12,
                    help="预热用线程数（I/O 密集）")
    ap.add_argument("--procs", type=int, default=8,
                    help="扫描用进程数（纯 CPU；线程池会被 GIL 串行化）")
    ap.add_argument("--out", default="sq.json")
    a = ap.parse_args()

    codes = stock_list(a.limit)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(_warm, [(c, a.freq, a.n, a.src) for c in codes]))
    codes = [c for c in codes if recs_src(c, a.freq, a.n, a.src)]
    print("周期={} 源={} 有效标的 {} 只（{} 只被质量门/接口剔除）预热 {:.0f}s".format(
        a.freq, a.src, len(codes), a.limit - len(codes), time.time() - t0), flush=True)

    t0 = time.time()
    # ⚠ 扫描是纯 CPU：用线程池会被 GIL 串行化（实测 5000 根 × 300 只 = 18 分钟），
    #   换成进程池后同样的活只要 ~2 分钟。见 `_scan`。
    with ProcessPoolExecutor(max_workers=a.procs) as ex:
        res = list(ex.map(_scan, [(c, a.freq, a.n, a.horizon, a.src)
                                  for c in codes], chunksize=4))
    rows = [r for sub, _ in res for r in sub]
    bases = [b for _, b in res if b]
    print("信号 {} 条 / 基准序列 {} 只，扫描 {:.0f}s（{} 进程）\n".format(
        len(rows), len(bases), time.time() - t0, a.procs), flush=True)

    base_mean = _avg([b["mean"] for b in bases]) * 100
    print("【口径一】结构口径：入场后是「走出反向笔」还是「原笔延伸」（窗口 {} 根）".format(
        a.horizon))
    print("{:<28} {:>6} {:>7} {:>7} {:>7} {:>7} {:>8} {:>9} {:>9} {:>7}".format(
        "分组", "信号数", "走出反向笔", "原笔延伸", "超时未定", "成功耗时",
        "均 A%", "全组 ret_H", "成功组", "失败组"))
    print("-" * 118)
    line("全部", rows)
    for sd, lab in ((1, "做多（买点）"), (-1, "做空（卖点）")):
        line(lab, [r for r in rows if (r["side"] == "long") == (sd > 0)])
    print()
    types = sorted({r["type"] for r in rows})
    for t in types:
        line("  类型 " + t, [r for r in rows if r["type"] == t])
    print()
    for sd, lab in ((1, "做多"), (-1, "做空")):
        for t in types:
            sub = [r for r in rows
                   if (r["side"] == "long") == (sd > 0) and r["type"] == t]
            if len(sub) >= 50:
                line("  {}·类型 {}".format(lab, t), sub)

    print("\n【口径二】事件研究：信号 vs 随机入场（同标的内部相减，H={} 根）".format(
        a.horizon))
    print("基准（全 K 线任意位置「做多」入场）平均 H 根收益 = {:+.2f}%".format(base_mean))
    print("⚠ 卖点的随机对照 = **随机做空** = −基准（已按逐条信号的方向取符号，"
          "不是拿卖点收益去减做多基准）\n")
    by_code = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)
    bmap = {b["code"]: b for b in bases}
    print("{:<22} {:>7} {:>11} {:>11} {:>12} {:>16}".format(
        "分组", "标的数", "信号 ret_H", "随机对照", "超额 Δ", "配对 p"))
    print("-" * 84)
    groups = [("全部信号", rows)]
    groups += [("做多（买点）", [r for r in rows if r["side"] == "long"]),
               ("做空（卖点）", [r for r in rows if r["side"] == "short"])]
    groups += [("  类型 " + t, [r for r in rows if r["type"] == t]) for t in types]
    groups += [("  ├ 走出反向笔后", [r for r in rows if r["outcome"] == "succ"]),
               ("  └ 原笔延伸后", [r for r in rows if r["outcome"] == "fail"])]
    stat = {}
    for lab, sub in groups:
        ex, sig_r, nul_r = [], [], []
        for c, rs in _by_code(sub).items():
            b = bmap.get(c)
            if not b:
                continue
            diffs, sg_r, nl_r = [], [], []
            for r in rs:
                if r["ret_H"] is None:
                    continue
                sg = 1.0 if r["side"] == "long" else -1.0
                diffs.append(r["ret_H"] - sg * b["mean"])
                sg_r.append(r["ret_H"])
                nl_r.append(sg * b["mean"])
            if not diffs:
                continue
            ex.append(_avg(diffs))
            sig_r.append(_avg(sg_r))
            nul_r.append(_avg(nl_r))
        obs, p, nc = paired_boot(ex)
        stat[lab] = {"delta": obs, "p": p, "n_codes": nc,
                     "sig": _avg(sig_r) * 100, "base": _avg(nul_r) * 100}
        star = ("***" if p < 0.001 else "**" if p < 0.01 else
                "*" if p < 0.05 else "n.s.")
        print("{:<22} {:>7} {:>10.2f}% {:>10.2f}% {:>+11.2f}% {:>15}".format(
            lab, nc, _avg(sig_r) * 100, _avg(nul_r) * 100, obs * 100,
            "{:.4f}{}".format(p, star)))

    print("\n【口径三】确认后入场（延迟进场：等反向笔确认了再买，钱换确定性）")
    print("从**确认那一根**的收盘起算，而不是从信号那根 —— 这样不会把"
          "「信号到确认」那截已经涨掉的行情算进收益里\n")
    print("{:<24} {:>7} {:>12} {:>11} {:>12} {:>16}".format(
        "分组", "标的数", "确认后 ret_H", "随机对照", "超额 Δ", "配对 p"))
    print("-" * 88)
    stat3 = {}
    g3 = [("全部确认后", [r for r in rows if r["outcome"] == "succ"])]
    g3 += [("  ├ 做多·确认后", [r for r in rows if r["outcome"] == "succ"
                                and r["side"] == "long"]),
           ("  └ 做空·确认后", [r for r in rows if r["outcome"] == "succ"
                                and r["side"] == "short"])]
    g3 += [("    类型 {}·确认后".format(t),
            [r for r in rows if r["outcome"] == "succ" and r["type"] == t])
           for t in types]
    for lab, sub in g3:
        ex, v_r, n_r = [], [], []
        for c, rs in _by_code(sub).items():
            b = bmap.get(c)
            if not b:
                continue
            diffs, vv, nn = [], [], []
            for r in rs:
                if r["post_H"] is None:
                    continue
                sg = 1.0 if r["side"] == "long" else -1.0
                diffs.append(r["post_H"] - sg * b["mean"])
                vv.append(r["post_H"])
                nn.append(sg * b["mean"])
            if not diffs:
                continue
            ex.append(_avg(diffs))
            v_r.append(_avg(vv))
            n_r.append(_avg(nn))
        obs, p, nc = paired_boot(ex)
        stat3[lab] = {"delta": obs, "p": p, "n_codes": nc,
                      "sig": _avg(v_r) * 100, "base": _avg(n_r) * 100}
        star = ("***" if p < 0.001 else "**" if p < 0.01 else
                "*" if p < 0.05 else "n.s.")
        print("{:<24} {:>7} {:>11.2f}% {:>10.2f}% {:>+11.2f}% {:>15}".format(
            lab, nc, _avg(v_r) * 100, _avg(n_r) * 100, obs * 100,
            "{:.4f}{}".format(p, star)))

    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump({"meta": vars(a), "base_mean_pct": base_mean,
                   "overall": agg(rows),
                   "by_type": {t: agg([r for r in rows if r["type"] == t])
                               for t in types},
                   "event_study": stat,
                   "event_study_post_confirm": stat3,
                   "baseline": {b["code"]: b for b in bases},
                   "rows": rows}, f, ensure_ascii=False)
    print("\n→ " + a.out)


def _by_code(rows):
    acc = {}
    for r in rows:
        acc.setdefault(r["code"], []).append(r)
    return acc


# ── 进程池用的顶层函数（lambda 不能被 pickle ⇒ 必须写在模块级） ──────────
def _warm(args):
    """预热取数（I/O 密集 ⇒ 用线程池即可）。"""
    return bool(recs_src(*args))


def _scan(args):
    """单只标的扫描（**纯 CPU** ⇒ 必须用进程池，线程池会被 GIL 串行化）。"""
    return scan(*args)


if __name__ == "__main__":
    main()

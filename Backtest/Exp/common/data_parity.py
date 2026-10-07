# -*- coding: utf-8 -*-
"""回测数据源 vs 页面数据源 一致性核验（证据脚本）。

页面/实盘链路（`App/AppEngine._analyze_stock_internal`）：
    CTdxAPI.fetch_main_level(market, code, freq)          ← 通达信本地 vipdoc
      → read_main_level_records →（w 由日线合成 / 30m·15m 由 5m 合成）
      → _forward_adjust(records, market, code, end_date)  ← 自实现前复权（xdxr）
      → tdx_data_context(records) → CChan(data_src="custom:TdxAPI.CTdxAPI",
                                          autype=AUTYPE.NONE)

本实验原有链路（`fetch_kline.py` / `fetch_min.py`）：
    腾讯 ifzq.gtimg.cn  qfq（w/d）；新浪分钟（**不复权**）

⇒ 两条链路的 **CChan 构造完全一致**（同一 data_src / autype / CChanConfig，
由 `Test/test_bt02_config_contract.py` 钉住），**喂进去的 records 不同**。
本脚本量化这个不同。

用法::

    python data_parity.py --n 20 --freqs d,w --bs 10
"""
from __future__ import annotations

import argparse
import os
import statistics as st
import sys
from collections import Counter

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
REPO = os.environ.get("CHAN_REPO", r"D:\CChan")
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from DataAPI.TdxAPI import CTdxAPI, set_tdx_config, tdx_data_context  # noqa: E402
from Chan import CChan                                                 # noqa: E402
from Common.CEnum import AUTYPE                                        # noqa: E402
from Backtest.Runner import default_chan_config, kl_type_of            # noqa: E402

TDX_INSTALL = os.environ.get("TDX_INSTALL_DIR", r"D:\new_tdx_hd_test")
set_tdx_config(vipdoc_dir=os.path.join(TDX_INSTALL, "vipdoc"),
               forward_adjust_enabled=True)

_DATE_FMT = {"d": "%Y-%m-%d", "w": "%Y-%m-%d", "m": "%Y-%m-%d"}


def _ds(dt, freq):
    """统一成可比字符串。"""
    if hasattr(dt, "strftime"):
        if freq in ("d", "w", "m"):
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d %H:%M")
    s = str(dt)
    return s[:10] if freq in ("d", "w", "m") else s[:16]


def tdx_recs(code, freq):
    r = CTdxAPI.fetch_main_level(code[:2], code[2:], freq)
    recs, fa = r if isinstance(r, tuple) else (r, None)
    return recs, fa


def ext_recs(code, freq, n, src):
    import signal_quality as sq
    return sq.recs_src(code, freq, n, src)


def chan_stats(recs, code, freq):
    """喂给与页面同一条 CChan 构造，数笔 / 买卖点。"""
    if not recs:
        return None
    with tdx_data_context(recs):
        chan = CChan(code="{}{}".format(code[:2], code[2:]), begin_time=None,
                     end_time=None, data_src="custom:TdxAPI.CTdxAPI",
                     lv_list=[kl_type_of(freq)], config=default_chan_config(),
                     autype=AUTYPE.NONE, market_type="stock")
        for _ in chan.step_load():
            pass
    lst = chan[0]
    types = Counter()
    sides = Counter()
    for bsp in lst.bs_point_lst.bsp_iter():
        types[bsp.type2str()] += 1
        sides["买" if bsp.is_buy else "卖"] += 1
    return {"bi": len(list(lst.bi_list)), "bsp": sum(types.values()),
            "types": dict(types), "sides": dict(sides)}


def cmp_one(code, freq, n, src):
    tr, fa = tdx_recs(code, freq)
    er = ext_recs(code, freq, n, src)
    if not tr or not er:
        return {"code": code, "freq": freq, "skip": "tdx=%d ext=%d" % (len(tr), len(er))}
    tmap = {_ds(r["dt"], freq): r for r in tr}
    emap = {_ds(r["dt"], freq): r for r in er}
    common = sorted(set(tmap) & set(emap))
    diffs = []
    for k in common:
        a, b = float(tmap[k]["close"]), float(emap[k]["close"])
        diffs.append(abs(a - b) / max(abs(a), 1e-9))
    out = {
        "code": code, "freq": freq,
        "tdx_n": len(tr), "ext_n": len(er),
        "tdx_span": (_ds(tr[0]["dt"], freq), _ds(tr[-1]["dt"], freq)),
        "ext_span": (_ds(er[0]["dt"], freq), _ds(er[-1]["dt"], freq)),
        "common": len(common),
        "close_med": st.median(diffs) * 100 if diffs else None,
        "close_max": max(diffs) * 100 if diffs else None,
        "close_gt1pct": sum(1 for d in diffs if d > 0.01),
        "fa": fa,
    }
    out["tdx"] = chan_stats(tr, code, freq)
    out["ext"] = chan_stats(er, code, freq)
    if out["tdx"] and out["ext"]:
        out["bi_delta"] = out["ext"]["bi"] - out["tdx"]["bi"]
        out["bsp_delta"] = out["ext"]["bsp"] - out["tdx"]["bsp"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="样本只数")
    ap.add_argument("--freqs", default="d,w")
    ap.add_argument("--bs", type=int, default=10, help="跑买卖点对比的只数")
    ap.add_argument("--limit", type=int, default=800, help="实验侧股票池大小")
    args = ap.parse_args()

    import fetch_kline
    import fetch_min
    codes = [c for c in fetch_kline.stock_list(args.limit)
             if os.path.exists(os.path.join(TDX_INSTALL, "vipdoc", c[:2], "lday",
                                            "{}.day".format(c)))]
    print("股票池 %d 只，其中本机通达信覆盖 %d 只" % (args.limit, len(codes)))
    codes = codes[:args.n]
    print("抽 %d 只做对比\n" % len(codes))

    n_hint = {"d": 800, "w": 800, "30m": 4000, "15m": 3000, "5m": 2000}
    for freq in [f for f in args.freqs.split(",") if f]:
        src = "sina" if freq.endswith("m") else "tencent"
        n = n_hint.get(freq, 800)
        rows = []
        for c in codes:
            rows.append(cmp_one(c, freq, n, src))
        ok = [r for r in rows if not r.get("skip")]
        print("═" * 96)
        print("周期 %s   （实验侧源=%s, n=%d）" % (freq, src, n))
        print("═" * 96)
        if not ok:
            for r in rows:
                print("  %s 跳过: %s" % (r["code"], r["skip"]))
            continue
        tn = [r["tdx_n"] for r in ok]
        en = [r["ext_n"] for r in ok]
        print("根数     通达信 中位 %6.0f  [%d, %d]      实验侧 中位 %6.0f  [%d, %d]"
              % (st.median(tn), min(tn), max(tn), st.median(en), min(en), max(en)))
        cm = [r["common"] for r in ok]
        print("重叠日期 中位 %6.0f  (占通达信 %.1f%%)" % (st.median(cm),
              st.median(cm) / st.median(tn) * 100))
        md = [r["close_med"] for r in ok if r["close_med"] is not None]
        mx = [r["close_max"] for r in ok if r["close_max"] is not None]
        bad = sum(r["close_gt1pct"] for r in ok)
        tot = sum(r["common"] for r in ok)
        print("重叠日收盘价差 |Δ|  中位 %.3f%%   最大 %.2f%%   >1%% 的占 %d/%d = %.1f%%"
              % (st.median(md), max(mx), bad, tot, bad / max(tot, 1) * 100))
        if args.bs and len(ok) >= 1:
            print("\n  %-10s %-24s %-24s" % ("代码", "通达信 笔/买卖点", "实验侧 笔/买卖点"))
            for r in ok[:args.bs]:
                t, e = r["tdx"], r["ext"]
                if not t or not e:
                    continue
                print("  %-10s %4d 笔 / %4d 点 %-8s %4d 笔 / %4d 点   Δ笔%+d Δ点%+d"
                      % (r["code"], t["bi"], t["bsp"], "", e["bi"], e["bsp"],
                         r["bi_delta"], r["bsp_delta"]))
        bd = [r["bi_delta"] for r in ok if "bi_delta" in r]
        pd_ = [r["bsp_delta"] for r in ok if "bsp_delta" in r]
        if bd:
            print("\n  笔数差 Δ = 实验侧 − 通达信：中位 %+.0f  范围 [%+d, %+d]  "
                  "完全一致 %d/%d 只"
                  % (st.median(bd), min(bd), max(bd), sum(1 for x in bd if x == 0), len(bd)))
            print("  买卖点数差 Δ：中位 %+.0f  范围 [%+d, %+d]  完全一致 %d/%d 只"
                  % (st.median(pd_), min(pd_), max(pd_),
                     sum(1 for x in pd_ if x == 0), len(pd_)))
        print()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""xdxr 磁盘快照 ⟷ 实盘链路（eltdx 现场网络请求）逐项对拍。

要回答的问题
------------
回测的前复权靠 `get_xdxr_data(market, code)`，而它是 **eltdx 网络调用**
（通达信 7709 / 0x000f，全局锁串行、单只 0.4~0.5s、偶发挂起）。`tdx_source`
把它换成**磁盘快照**（`--xdxr 900` 一次性抓下来冻结）。本脚本证明这个替换
**没有改变任何结果**，把「快照」与「实盘」的差异限定在一处可枚举的地方。

三层证据
--------
A 覆盖：全池每只都有快照文件吗？（缺文件 ⇒ 前复权被静默跳过）
B 内容：对抽样标的**现场再问一次 eltdx**，与快照逐列比对（形状 / 日期 / 数值）
C 结果：用「现场值」和「快照值」各做一遍前复权，喂给**同一个** CChan 构造，
        比对 笔数 + 买卖点数（逐类型）—— 这才是有意义的等价性判据

D 附加：`records()` 连调两次的价格是否一致（防"读缓冲被原地改"式的重复复权）

用法::

    python xdxr_parity.py --n 8          # 抽样 8 只做 B/C
    python xdxr_parity.py --n 0          # 只跑 A（不看网络）
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo(_p):
    for _ in range(7):
        if (os.path.isfile(os.path.join(_p, "Chan.py"))
                and os.path.isdir(os.path.join(_p, "DataAPI"))):
            return _p
        _p = os.path.dirname(_p)
    return ""


EXP = HERE if os.path.basename(HERE) == "Exp" else os.path.dirname(HERE)
REPO = (os.environ.get("CHAN_REPO") or _find_repo(HERE)
        or os.path.join(os.path.dirname(os.path.dirname(EXP)), "wt_latest"))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
os.chdir(REPO)

from tdx_source import (records, raw_records, stock_list, XDXR_DIR,   # noqa: E402
                        _xdxr_path, install_xdxr)
from data_parity import chan_stats                                    # noqa: E402


class _Missing:
    """缺文件哨兵（不能用字符串 —— `DataFrame == "x"` 返回的是 DataFrame）。"""

    def __repr__(self):
        return "MISSING"


MISSING = _Missing()


def load_pkl(code):
    p = _xdxr_path(code)
    if not os.path.exists(p):
        return MISSING
    with open(p, "rb") as f:
        return pickle.load(f)


def fmt_df(df):
    """(行数, 日期列表, 列名) —— DataFrame / None 统一描述。"""
    if df is None:
        return (0, [], [])
    if hasattr(df, "shape"):
        return (len(df), [str(x) for x in df["date"]], list(df.columns))
    return (0, [], [])


def cmp_df(a, b):
    """返回 (是否一致, 一句话说明)。列级比对：日期 + 每个数值列。"""
    ra, da, ca = fmt_df(a)
    rb, db, cb = fmt_df(b)
    if ra == 0 and rb == 0:
        return True, "两边都为空"
    if ra != rb:
        return False, "行数 %d vs %d" % (ra, rb)
    if da != db:
        n = sum(1 for x, y in zip(da, db) if x != y)
        return False, "日期序列有 %d 处不同" % n
    bad = []
    for col in ("fenhong", "peigu", "peigujia", "songgu", "zhuanzeng"):
        if col in ca and col in cb:
            va = [None if _nan(x) else round(float(x), 6) for x in a[col]]
            vb = [None if _nan(x) else round(float(x), 6) for x in b[col]]
            if va != vb:
                bad.append("%s(%d处)" % (col, sum(1 for x, y in zip(va, vb) if x != y)))
    return (not bad), ("全列相等" if not bad else "列差异: " + ",".join(bad))


def _nan(x):
    try:
        return x != x
    except Exception:                                        # noqa: BLE001
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8, help="抽样只数（做 B/C，走网络）")
    ap.add_argument("--pool", type=int, default=800)
    ap.add_argument("--freq", default="d")
    a = ap.parse_args()

    install_xdxr(verbose=False)
    pool = stock_list(a.pool)
    print("=" * 92)
    print("A. 快照覆盖（股票池 %d 只）" % len(pool))
    print("=" * 92)
    miss, none_, thr = [], [], []
    for c in pool:
        df = load_pkl(c)
        if df is MISSING:
            miss.append(c)
        elif df is None or len(df) == 0:
            none_.append(c)
        else:
            thr.append((c, len(df)))
    print("  快照目录        : %s" % XDXR_DIR)
    print("  文件数          : %d" % (len(miss) + len(none_) + len(thr)))
    print("  有效（有除权）  : %d" % len(thr))
    print("  空值（真无除权）: %d" % len(none_))
    print("  ⚠ 缺文件        : %d %s" % (len(miss), miss[:10] if miss else ""))
    if thr:
        ns = sorted(n for _, n in thr)
        print("  事件条数 中位/最大: %d / %d" % (ns[len(ns) // 2], ns[-1]))

    print()
    print("D. records() 连调两次是否一致（防重复复权）")
    c0 = pool[0]
    r1 = records(c0, a.freq, use_cache=False)
    r2 = records(c0, a.freq, use_cache=False)
    same = (len(r1) == len(r2)
            and all(abs(x["close"] - y["close"]) < 1e-9 for x, y in zip(r1, r2)))
    print("  %s/%s  %d 根  close 逐根一致: %s" % (c0, a.freq, len(r1), same))

    if a.n <= 0:
        return

    # 现场 live 版本（注意：要替换的是 TdxAPI 模块属性，不是 ElTdxAPI 的）
    from DataAPI import TdxAPI
    from DataAPI.ElTdxAPI import get_xdxr_data as live_get

    snap_map = {}

    def _snap(market, code):
        return snap_map.get(market + code)

    print()
    print("=" * 92)
    print("B/C. 现场 eltdx  vs  磁盘快照   （抽 %d 只）" % a.n)
    print("=" * 92)
    print("%-10s %-8s %-28s %-24s" % ("code", "事件数", "B 内容比对", "C 结构比对(笔/买卖点)"))
    print("-" * 92)
    n_bad = 0
    for code in pool[:a.n]:
        df_snap = load_pkl(code)
        if df_snap is MISSING:
            print("%-10s %-8s %s" % (code, "-", "无快照，跳过"))
            continue
        try:
            df_live = live_get(code[:2], code[2:])
        except Exception as e:                               # noqa: BLE001
            print("%-10s %-8s %s" % (code, "-", "现场取数失败: %s" % e))
            continue
        ok, why = cmp_df(df_live, df_snap)

        # C: 两条复权路径各算一遍结构
        snap_map[code] = df_snap
        TdxAPI.get_xdxr_data = _snap
        st_snap = chan_stats(records(code, a.freq, use_cache=False), code, a.freq)
        snap_map[code] = df_live
        st_live = chan_stats(records(code, a.freq, use_cache=False), code, a.freq)
        TdxAPI.get_xdxr_data = _snap

        if st_snap and st_live:
            same_struct = (st_snap["bi"] == st_live["bi"]
                           and st_snap["types"] == st_live["types"])
            sc = "笔 %d/%d 买卖点 %d/%d %s" % (
                st_snap["bi"], st_live["bi"], st_snap["bsp"], st_live["bsp"],
                "✅逐类相同" if same_struct else "❌不同 %s vs %s"
                % (st_snap["types"], st_live["types"]))
        else:
            same_struct, sc = False, "结构算不出"
        if not (ok and same_struct):
            n_bad += 1
        print("%-10s %-8s %-28s %-24s" % (
            code, "%d/%d" % (len(df_live) if df_live is not None else 0,
                             len(df_snap) if df_snap is not None else 0),
            ("✅ " + why) if ok else ("❌ " + why), sc))

    print("-" * 92)
    print("结论: %d/%d 只完全一致（内容 + 结构）" % (a.n - n_bad, a.n))


if __name__ == "__main__":
    main()

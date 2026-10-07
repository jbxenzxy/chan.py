# -*- coding: utf-8 -*-
"""「浮盈没锁住 / 被扫掉」的量化 —— 回答"回撤控制严格容易被扫掉"到底有多严重。

用户原话（上一轮）：「控制严格，容易被扫掉；控制宽松，又容易浮盈变止损」。
这两句话是**两个不同的、都可测量的现象**，本脚本分开量：

  ① **被扫掉（止损后悔率）**
     信号入场后，**在结构止损被打穿之前**已经浮盈 ≥ k×A，最后仍然以"原笔延伸"
     （= 结构止损被触碰）收场 ⇒ 事后看止损放得太紧，吃了噪声。
     口径：`outcome == fail` 且 `mfe_fail_A ≥ k`。
     `mfe_fail_A` 由 `signal_quality.py` 在**打止损那一帧之前**冻结（不含该帧自己的
     最高价）⇒ 这个数是"真·事前已经拿到的浮盈"。

     这类样本有个**不是上界、而是确定的**反事实：
       L1 保本层若在浮盈 k×A 时把保护价抬到入场价 +0.4A，那么价格后来既然一路
       走到了 −1A（结构止损），就一定穿过了 +0.4A ⇒ 该笔离场价确定从 −1A 变成 +0.4A。
     ⚠ 唯一失效条件：跳空直接跳过保护价（日线以下周期罕见，但不能说没有）。

  ② **浮盈变亏损（跟踪太松）**
     窗口内浮盈曾 ≥ k×A，但 H 根后收益为负 ⇒ 保护价抬得太慢 / 回撤给得太大。
     口径：`mfe_A ≥ k` 且 `ret_H < 0`。

用法::
    python sweep_regret.py                       # 默认 sq_w800 + sq_d800
    python sweep_regret.py sq_30m.json sq_15m.json sq_5m.json
"""
from __future__ import annotations

import json
import os
import sys

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
# L1 保本层当前的落点（相对入场价的 R 偏移）；用户口径下 = 入场价 + 2%
BE_OFFSET_A = 0.4          # 保护价落点（×A）


def _avg(v):
    return (sum(v) / len(v)) if v else 0.0


def load(fp):
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def report(lab: str, rows: list, ks=(0.5, 1.0, 2.0)) -> dict:
    n = len(rows)
    fails = [r for r in rows if r["outcome"] == "fail"]
    print("─" * 108)
    print("{}    信号 {} 条   其中「原笔延伸」{} 条（{:.1f}%）".format(
        lab, n, len(fails), len(fails) / n * 100 if n else 0))
    print("─" * 108)
    out = {"n": n, "n_fail": len(fails)}
    print("  ① 被扫掉：在结构止损被打穿**之前**就浮盈 ≥ k×A 的占比")
    print("  {:<9} {:>9} {:>10} {:>12} {:>14} {:>18}".format(
        "浮盈门槛", "条数", "占全部", "占原笔延伸", "打止损前均浮盈",
        "保本层可救回(×A/笔)"))
    for k in ks:
        hit = [r for r in fails if r["mfe_fail_A"] is not None
               and r["mfe_fail_A"] >= k]
        if not hit:
            continue
        rec = BE_OFFSET_A + 1.0            # 从 −1A 变成 +0.4A，改善 1.4×A
        print("  {:<9} {:>9} {:>10} {:>12} {:>14} {:>18}".format(
            "≥{:.1f}×A".format(k), len(hit),
            "{:.1f}%".format(len(hit) / n * 100) if n else "-",
            "{:.1f}%".format(len(hit) / len(fails) * 100) if fails else "-",
            "{:.2f}×A".format(_avg([r["mfe_fail_A"] for r in hit])),
            "+{:.2f}×A".format(rec)))
        out["k{:.1f}".format(k)] = {
            "n_hit": len(hit),
            "pct_all": len(hit) / n * 100 if n else 0,
            "pct_fail": len(hit) / len(fails) * 100 if fails else 0,
            "mfe_fail": _avg([r["mfe_fail_A"] for r in hit]),
            "rec_a": rec}

    print()
    print("  ② 浮盈变亏损：窗口内浮盈曾 ≥ k×A，但 H 根后收益为负")
    print("  {:<9} {:>9} {:>10} {:>20} {:>16}".format(
        "浮盈门槛", "条数", "占全部", "这些样本的均 ret_H", "占该门槛样本"))
    for k in ks:
        hit = [r for r in rows if r["mfe_A"] is not None and r["mfe_A"] >= k]
        if not hit:
            continue
        neg = [r for r in hit if r["ret_H"] is not None and r["ret_H"] < 0]
        print("  {:<9} {:>9} {:>10} {:>20} {:>16}".format(
            "≥{:.1f}×A".format(k), len(neg),
            "{:.1f}%".format(len(neg) / n * 100) if n else "-",
            "{:+.2f}%".format(_avg([r["ret_H"] for r in neg
                                    if r["ret_H"] is not None]) * 100),
            "{:.1f}%".format(len(neg) / len(hit) * 100)))
        out.setdefault("neg", {})["k{:.1f}".format(k)] = {
            "n_neg": len(neg),
            "pct_all": len(neg) / n * 100 if n else 0,
            "pct_hit": len(neg) / len(hit) * 100,
            "ret": _avg([r["ret_H"] for r in neg
                         if r["ret_H"] is not None]) * 100}

    print()
    print("  ③ 结构结局 × 窗口内浮盈分布（mfe_A，仅在 H 根窗口内累计）")
    for oc, lab2 in (("succ", "走出反向笔"), ("fail", "原笔延伸"),
                     ("censor", "超时未定")):
        sub = [r["mfe_A"] for r in rows
               if r["outcome"] == oc and r["mfe_A"] is not None]
        if not sub:
            continue
        s = sorted(sub)
        print("     {:<10} n={:<6} 均值 {:>5.2f}×A  中位 {:>5.2f}×A  "
              "≥1A 占比 {:>5.1f}%  ≥2A 占比 {:>5.1f}%".format(
                  lab2, len(sub), _avg(sub), s[len(s) // 2],
                  sum(1 for v in sub if v >= 1.0) / len(sub) * 100,
                  sum(1 for v in sub if v >= 2.0) / len(sub) * 100))
    print()
    return out


def main():
    if len(sys.argv) > 1:
        fps = sys.argv[1:]
    else:
        # 兼容两种命名：旧版 sq_w800.json / 现行 sq_w.json —— 同一周期只取一个
        fps = []
        for base in ("w", "d", "30m", "15m", "5m"):
            for cand in (base, base + "800"):
                fp = os.path.join(HERE, "sq_{}.json".format(cand))
                if os.path.exists(fp):
                    fps.append(fp)
                    break
    acc = {}
    for fp in fps:
        if not os.path.exists(fp):
            print("跳过（不存在）：" + fp)
            continue
        d = load(fp)
        if d["rows"] and "mfe_fail_A" not in d["rows"][0]:
            print("⚠ {} 是用旧版 signal_quality 产的（无 mfe_fail_A 字段），"
                  "请重跑该周期".format(os.path.basename(fp)))
            continue
        lab = os.path.basename(fp)[3:-5]
        acc[lab] = report(lab, d["rows"])
        for side, sl in (("long", "  做多（买点）"), ("short", "  做空（卖点）")):
            acc[lab + "_" + side] = report(
                sl, [r for r in d["rows"] if r["side"] == side])
    with open(os.path.join(HERE, "sweep_regret.json"), "w", encoding="utf-8") as f:
        json.dump(acc, f, ensure_ascii=False, indent=1)
    print("→ sweep_regret.json")


if __name__ == "__main__":
    main()

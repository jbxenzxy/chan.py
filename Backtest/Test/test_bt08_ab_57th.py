# -*- coding: utf-8 -*-
"""P1 A/B 对照：0 类点 57th ㈠ 段「严格闸 vs 放宽判据」对**交易层**的影响。

为什么需要这一条（设计文档 §2.7 / §6 P1）
--------------------------------------------------
信号层的判别力已由 `Test/test_bs0_ozs57_realdata.py` 钉住 —— 它证明的是
「放宽后 57th 多产出 6 个 T0，严格下归零」。但那只是**信号数**。

§2.7 的实测结论是：本样本 0 类买点 **8 个信号只对应 3 个独立交易机会**
（04/27、07/17、08/18 三个簇，簇②连续 4 根右肩是同一笔）。⇒ 裁决
「放宽多出来的信号是正期望还是稀释/重复」**必须比状态机去重后的交易笔数
与期望 R，比信号数看不出来**。

本用例就把这个裁决做成常驻护栏：同一样本、同一出场参数，只切 ㈠ 段口径
（严格 = 相邻回调笔逐级收窄；放宽 = 各回调笔极值均不越过笔1，= 当前源码），
各跑一遍，比**交易笔数与期望 R**。

两臂怎么切
----------
严格闸是**前置**在 ㈠ 段外层的 monkeypatch：任一环不成立即原样 `return`。
与「把 ㈠ 段源码换成旧实现」逐分支等价 —— 严格拒时两者在同一位置 return；
严格收时放宽必收（严格 ⊆ 放宽，已由 `test_bs0_ozs57_lightning.py` 证），
后续 ㈡㈢㈣ 走的是同一段未改动源码。

⚠ 本文件**刻意自持**这段严格闸，不从 `Test/test_bs0_ozs57_realdata.py` import：
   `Backtest/Test/` 目前对顶层 `Test/` 零依赖（全目录实测），引入会让回测测试
   套件反向依赖顶层测试目录。两处实现各服务一种粒度（信号层 / 交易层），
   且严格口径的**权威定义**是 `BuySellPoint/BSPointList.py` 里那段保留备查的
   注释源码 —— 两处都只是它的复刻，不是新的 SSOT。

冻结基线
--------
`Backtest/Test/snapshots/p1_ab_57th.json`（跑 `--freeze` 生成）。
两臂 × 三口径的 (filled / closed / win_rate / expectancy_r / sum_r)。

零网络、零 vipdoc（数据全部来自冻结切片）。跑法：
    python Backtest/Test/test_bt08_ab_57th.py [--freeze]
"""
import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIX_DIR = os.path.join(ROOT, "Test", "fixtures_real")
SAMPLE = "sz002190_d.json"
SNAP_DIR = os.path.join(HERE, "snapshots")
SNAP_PATH = os.path.join(SNAP_DIR, "p1_ab_57th.json")

MARKET, CODE, FREQ = "sz", "002190", "d"
GATE_NAME = "_cal_bs0point_nth_ozs_57th"

# 三个口径：全放行 / 只放行 0 类 / 只放行 3 类
#   只放行 3 类是**判别力对照臂**：改动边界只落在 0 类 ⇒ 3 类必须逐笔全等。
SCENARIOS = (
    ("all", None, "全放行"),
    ("only0", "0", "只放行 0 类（57th 只影响这里）"),
    ("only3", "3", "只放行 3 类（判别力对照：必须零影响）"),
)

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
            print("       %s" % detail)


def _r(x, nd=6):
    return None if x is None else round(float(x), nd)


# ════════════════════════════════════════════════════════════════════
# 严格闸（复刻保留在 BSPointList.py 的旧实现；见 docstring「两臂怎么切」）
# ════════════════════════════════════════════════════════════════════
def _strict_gate(orig):
    """相邻回调笔逐级收窄；任一环不成立即原样 return（= 换掉 ㈠ 段）。"""

    def wrapper(self, bi_list, pivot_a, stroke_n):
        nth = stroke_n.idx - pivot_a.begin_bi.idx + 1
        begin = pivot_a.begin_bi.idx
        odd = [bi_list[begin + i] for i in range(0, nth, 2)]
        for j in range(1, len(odd)):
            cur, prev = odd[j], odd[j - 1]
            ok = (cur._high() <= prev._high()) if stroke_n.is_down() \
                else (cur._low() >= prev._low())
            if not ok:
                return
        return orig(self, bi_list, pivot_a, stroke_n)

    return wrapper


def _arm(strict, pick, records):
    """跑一臂一个口径。`strict` = 是否套严格闸；`pick` = bsp_types 选择串。

    ⚠ monkeypatch 用 try/finally 还原类属性 —— 这是本文件唯一的全局副作用，
      不还原会污染同进程里后续用例（门禁是逐组件子进程，但单跑时同进程）。
    """
    from Backtest import Runner
    from Backtest.Filter import filter_from_choices
    from Backtest.Metrics import compute
    from BuySellPoint import BSPointList as BSP

    cls = BSP.CMyBSPointList
    orig = getattr(cls, GATE_NAME)
    if strict:
        setattr(cls, GATE_NAME, _strict_gate(orig))
    try:
        filt = None if pick is None else filter_from_choices(pick)
        res = Runner.run(MARKET, CODE, FREQ, records=records, bsp_filter=filt)
        met = compute(res)
        return {
            "signals_seen": int(res.signals_seen),
            "signals_filtered": int(res.signals_filtered),
            "filled": len(res.trades),
            "closed": int(met.n),
            "still_open": int(met.u),
            "w": int(met.w), "l": int(met.l), "e": int(met.e),
            "win_rate": _r(met.win_rate),
            "expectancy_r": _r(met.expectancy_r),
            "sum_r": _r(sum(float(t.r_multiple or 0.0) for t in res.trades)),
            "entries": [t.entry_date for t in res.trades],
            "bps": [t.bsp_type for t in res.trades],
            "bars_total": int(res.bars_total),
        }
    finally:
        setattr(cls, GATE_NAME, orig)


def fixture_entry():
    with io.open(os.path.join(FIX_DIR, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    hit = [e for e in manifest["entries"] if e["file"] == SAMPLE]
    assert len(hit) == 1, "manifest 里找不到 %s" % SAMPLE
    return hit[0]


def build_snapshot():
    """现算快照（`--freeze` 写盘 / 无参时比对）。"""
    from Backtest.Runner import load_records

    entry = fixture_entry()
    records = load_records(os.path.join(FIX_DIR, SAMPLE))
    snap = {
        "kind": "backtest-p1-ab57",
        "version": 1,
        "note": ("P1 A/B 对照冻结基线：0 类点 57th ㈠ 段「严格闸 vs 放宽判据」"
                 "在**交易层**（状态机去重后）的差异。来源 = 冻结切片 "
                 "Test/fixtures_real/sz002190_d.json。"
                 "改了内核或改了出场参数要刷新时跑 test_bt08_ab_57th.py --freeze。"),
        "fixture": {"dir": "Test/fixtures_real", "file": SAMPLE,
                    "sha256": entry["sha256"], "bars": int(entry["bars"])},
        "target": {"market": MARKET, "code": CODE, "freq": FREQ},
        "arms": {},
    }
    for arm, strict in (("A_strict", True), ("B_lax", False)):
        snap["arms"][arm] = {
            "gate": "strict-chain" if strict else "lax-single-ref",
            "scenarios": {name: _arm(strict, pick, records)
                          for name, pick, _label in SCENARIOS},
        }
    return snap


def diff(got, want, path="", out=None):
    """递归比对，把差异定位到 JSON 路径。"""
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


def main(argv):
    freeze = "--freeze" in (argv or sys.argv[1:])

    print("=" * 68)
    print("P1 A/B 对照：0 类 57th 严格闸 vs 放宽（Backtest/Test/test_bt08_ab_57th.py）")
    print("=" * 68)

    entry = fixture_entry()
    got_sha = hashlib.sha256(
        io.open(os.path.join(FIX_DIR, SAMPLE), "rb").read()).hexdigest()
    check("① 冻结切片 sha256 与 manifest 一致（防手改）",
          got_sha == entry["sha256"],
          "got=%s\nmanifest=%s" % (got_sha, entry["sha256"]))

    got = build_snapshot()
    A = got["arms"]["A_strict"]["scenarios"]
    B = got["arms"]["B_lax"]["scenarios"]

    if freeze:
        os.makedirs(SNAP_DIR, exist_ok=True)
        with io.open(SNAP_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(got, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print("[FREEZE] 基线已写入 %s" % os.path.relpath(SNAP_PATH, ROOT))
        for name, _p, _l in SCENARIOS:
            print("         %-6s 严格 filled=%d 期望R=%s | 放宽 filled=%d 期望R=%s"
                  % (name, A[name]["filled"], A[name]["expectancy_r"],
                     B[name]["filled"], B[name]["expectancy_r"]))
        return 0

    if not os.path.exists(SNAP_PATH):
        check("基线文件存在", False,
              "缺 %s —— 先跑 --freeze 生成" % os.path.relpath(SNAP_PATH, ROOT))
        return 1

    with io.open(SNAP_PATH, encoding="utf-8") as f:
        want = json.load(f)

    # ② 两臂都跑满同一切片（闸不改可达性）
    for arm in ("A_strict", "B_lax"):
        name = got["arms"][arm]["scenarios"]["all"]
        check("②[%s] 跑满整条切片（bars_total == 切片根数）" % arm,
              name["bars_total"] == int(entry["bars"]),
              "got=%r want=%r" % (name["bars_total"], entry["bars"]))

    # ③ 判别力自证：改动边界只落在 0 类 ⇒ 只放行 3 类时两臂必须**逐笔全等**
    a3, b3 = A["only3"], B["only3"]
    check("③[diff-arm] 只放行 3 类时两臂逐笔全等（改动边界只落 0 类）",
          {k: a3[k] for k in ("filled", "closed", "w", "l", "e",
                              "expectancy_r", "sum_r", "entries", "bps")}
          == {k: b3[k] for k in ("filled", "closed", "w", "l", "e",
                                 "expectancy_r", "sum_r", "entries", "bps")},
          "A=%r\nB=%r" % (a3, b3))

    # ④ 放宽确实改变了 0 类：交易笔数 / 信号数都变多
    a0, b0 = A["only0"], B["only0"]
    check("④[reach] 放宽后 0 类信号数 > 严格（闸门真起作用）",
          b0["signals_seen"] - b0["signals_filtered"] >
          a0["signals_seen"] - a0["signals_filtered"],
          "严格 0类信号=%d 放宽=%d"
          % (a0["signals_seen"] - a0["signals_filtered"],
             b0["signals_seen"] - b0["signals_filtered"]))
    check("④[reach] 放宽后 0 类**交易笔数** > 严格（状态机去重后仍多）",
          b0["filled"] > a0["filled"],
          "严格 filled=%d 放宽=%d" % (a0["filled"], b0["filled"]))

    # ⑤ ★ 核心裁决：比交易笔数与期望 R（**不是**比信号数，§2.7）
    d_filled = b0["filled"] - a0["filled"]
    d_sum = b0["sum_r"] - a0["sum_r"]
    print("\n── ⑤ 核心裁决（只放行 0 类：比交易笔数 + 期望 R）──")
    print("   严格   filled=%d  期望R=%s  合计R=%s  胜率=%s"
          % (a0["filled"], a0["expectancy_r"], a0["sum_r"], a0["win_rate"]))
    print("   放宽   filled=%d  期望R=%s  合计R=%s  胜率=%s"
          % (b0["filled"], b0["expectancy_r"], b0["sum_r"], b0["win_rate"]))
    print("   增量   Δfilled=%+d  Δ合计R=%+.4f  (新增笔均值 R=%s)"
          % (d_filled, d_sum, _r(d_sum / d_filled) if d_filled else None))
    check("⑤ 放宽新增的 0 类交易 > 0 笔", d_filled > 0, "Δfilled=%d" % d_filled)
    check("⑤ 放宽新增部分是**正贡献**（Δ合计R > 0，非稀释）",
          d_sum > 0, "Δ合计R=%.4f" % d_sum)
    check("⑤ 放宽后 0 类期望 R 不低于严格（口径改善而非恶化）",
          b0["expectancy_r"] is not None and a0["expectancy_r"] is not None
          and b0["expectancy_r"] >= a0["expectancy_r"],
          "严格=%s 放宽=%s" % (a0["expectancy_r"], b0["expectancy_r"]))

    # ⑥ 全放行口径同样对照（两类混合后的总账）
    aa, ba = A["all"], B["all"]
    print("\n── ⑥ 全放行口径 ──")
    print("   严格   filled=%d  期望R=%s" % (aa["filled"], aa["expectancy_r"]))
    print("   放宽   filled=%d  期望R=%s" % (ba["filled"], ba["expectancy_r"]))
    check("⑥ 全放行口径下放宽同样多出交易且期望 R 更高",
          ba["filled"] > aa["filled"] and ba["expectancy_r"] > aa["expectancy_r"],
          "Δfilled=%d Δ期望R=%s"
          % (ba["filled"] - aa["filled"],
             _r(ba["expectancy_r"] - aa["expectancy_r"])))

    # ⑦ §2.7 实证：0 类「信号数 / 交易笔数」虚高倍率 —— 证明报表必须报笔数
    def _ratio(sc):
        sig = sc["signals_seen"] - sc["signals_filtered"]
        return None if not sc["filled"] else round(sig / sc["filled"], 4)
    print("\n── ⑦ 信号数 vs 交易笔数（§2.7 簇问题实证）──")
    for arm, sc in (("严格", a0), ("放宽", b0)):
        print("   %s  0类信号=%d  交易笔数=%d  虚高=%.2f×"
              % (arm, sc["signals_seen"] - sc["signals_filtered"],
                 sc["filled"], _ratio(sc)))
    check("⑦ 放宽臂 0 类「信号数/笔数」虚高 > 2×（簇确实存在，报表必须报笔数）",
          _ratio(b0) is not None and _ratio(b0) > 2.0,
          "虚高=%s" % _ratio(b0))
    check("⑦ 若按信号数计会高估放宽的增益（Δ信号 > Δ笔数）",
          (b0["signals_seen"] - b0["signals_filtered"])
          - (a0["signals_seen"] - a0["signals_filtered"]) > d_filled,
          "Δ信号=%d Δ笔数=%d"
          % ((b0["signals_seen"] - b0["signals_filtered"])
             - (a0["signals_seen"] - a0["signals_filtered"]), d_filled))

    # ⑧ 快照一致（两臂三口径逐字段；改了内核或出场参数须 --freeze 重冻）
    print("")
    d = diff(got, want, "snap")
    check("⑧ 两臂 × 三口径与冻结基线逐字段一致（改了内核请 --freeze 重冻）", not d,
          "\n".join(d[:12]))

    print("\n────────────────────────────────────────────────────────────────────")
    print("合计 %d 项，通过 %d，失败 %d" % (PASS + FAIL, PASS, FAIL))
    print("\n★ 结论（本样本 sz002190 日线，1393 根）：")
    print("  放宽 57th ㈠ 段在 0 类上**新增 %d 笔正期望交易**（合计 R %+.4f），"
          "严格臂的 0 类仅 %d 笔。" % (d_filled, d_sum, a0["filled"]))
    print("  对 3 类零影响（逐笔全等）。⚠ 样本 n=%d 无统计意义，"
          "方向性结论须全 A 扫描裁决（§8 第 5 条 / P3）。" % b0["filled"])
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:]) else 1)

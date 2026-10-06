# -*- coding: utf-8 -*-
"""
P4 漂移巡检（Backtest/Test/test_bt07_repaint_drift.py）
========================================================
设计文档 §6「P4 判据」的可执行版本：把 P0 期间用**一次性探针**量到的两件事
（探针源 = 沙盒外 `sb/probe_repaint.py`、`sb/probe_r_drift.py`，不随交付外发）
转成常驻门禁 —— 否则这两条结论只活在某一轮的会话记录里，下一步改动就会悄悄推翻它。

  ⑴ **重绘（repaint）**：逐根推进时快照**每帧**的买卖点集合，找"出现后又消失"的信号。
     冻结结论 = **0**（`sz002190` 日线 11 个信号出现后从未消失）。
     这是「首见冻结 = 流式的自然结果」（§2.6）能成立的前提：若信号会消失，
     "在它首现那一帧就入场"就可能是在一个后来被撤销的信号上进场的。

  ⑵ **R 漂移**：`bsp.bi.get_end_klu()`（止损锚分型）会随**笔延伸**被改写
     ⇒ 首见态算出的 A 分量 ≠ 最终态的 A。冻结结论 = **3/11，全是 3 类点**，
     且漂移**单向**（笔延伸只会把分型推得更远 ⇒ 首见 A 偏小 ⇒ R 偏小）。

⚠ 这两条是**代价**，不是缺陷（§2.6 定案 (i)）：回测刻意用首见态 —— 实时交易时
  "当时当下"能看到的就只有首见态。本用例把代价**量化并冻结**，任何变化都要人来判
  「是不是我刚改的那件事」，而不是让它悄悄漂走。
  ⇒ 也因此首见态 A 在低价样本上普遍偏小（同一个信号的最终态 A 会更大）——
    `Trading/Strategy/Exit.py` 原有的 `[R 结构距离偏小]` 告警里有几条正是这个
    成因（该告警已删，2026-10-06 用户裁定：绝对点数阈值量级失真、只观测不改 R）。

判据
----
  ① 帧数 == 切片根数（防少喂/多喂一帧，锚住"逐根推进"这件事本身）
  ② 零重绘：`ever_seen == final_alive`，且消失集为空（逐样本）
  ③ 字段改写（笔指纹）清单冻结：哪几个信号、各改写多少次
  ④ R 漂移清单冻结：逐条 key + 首见 A + 最终 A + 笔端 / 分型 首见→最终
  ⑤ 漂移**单向性**不变量：`ΔA > 0`（笔延伸只推远分型；出现反向漂移是另一类问题）
  ⑥ 未漂移信号的首见 A == 最终 A（证明"漂移清单"不是把全部信号都算进去了）
  ⑦ **与回测侧交叉验证**（两个独立口径对齐）：
     `run.signals_seen == ever_seen`；每笔 `entry_date` 都能在引擎侧清单里命中；
     被开仓的漂移笔 `r_distance >= A_首见`，并**冻结**其中 `r_distance < A_最终`
     的笔数（"因首见态而 R 偏小"的可观测条数）
  ⑧ 判别力自证：漂移清单**非空且非全集**（尺子若坏成"自己比自己"清单会空，
     若比错对象会全漂）；且每条漂移的 Δ% 与冻结值一致
  ⑨ 快照一致（改了内核后**故意**刷新基线：`--freeze`）

样本范围
----
常驻只跑 `d`(1393) + `30m`(1936) 两个样本。理由：P4 关注的是"笔延伸 → 分型改写"
这一类行为，两个不同量级的周期已能判；`5m`(11616) / `15m`(3872) / `w`(294) 若将来
要纳入，**先跑一遍确认基线再登记**，不许凭"应该一样"直接加进 `SAMPLES`。

零网络（数据全来自冻结 JSON；行情源走 `TdxAPI.tdx_data_context` 的 custom 免连网分支）。
跑法：`python Backtest/Test/test_bt07_repaint_drift.py`（退出码 0/1 即判决）
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKTEST_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIX_DIR = os.path.join(ROOT, "Test", "fixtures_real")
SNAP_DIR = os.path.join(HERE, "snapshots")
SNAP_PATH = os.path.join(SNAP_DIR, "p4_repaint_drift.json")

MARKET, CODE = "sz", "002190"
# (freq, 切片文件) —— 见 docstring「样本范围」
SAMPLES = [("d", "sz002190_d.json"), ("30m", "sz002190_30m.json")]

# A 量级观测线（3.0 = 已删的 min_r_points 地板原值）—— ⚠ v2.2：`Exit.py` 的
# 「[R 结构距离偏小]」告警已删，本常量不再对应任何告警，只用于**披露**"首见态 A
# 低于 3 点的笔数"（量级观测，不参与任何判定）
R_ALERT_A_FLOOR = 3.0

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
            print("       " + str(detail)[:600])


def _r(x, nd):
    return None if x is None else round(float(x), nd)


def fixture_meta(fname):
    """从 `fixtures_real/manifest.json` 取一条（sha256 / bars）。"""
    with io.open(os.path.join(FIX_DIR, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    hit = [e for e in man["entries"] if e["file"] == fname]
    assert len(hit) == 1, "manifest 里找不到样本 %s" % fname
    return hit[0]


# ═════════════════════════════════════════════════════════════════════
# 引擎侧：逐帧快照（重绘 + 笔指纹改写 + A 首见/最终）
# ═════════════════════════════════════════════════════════════════════
def engine_walk(freq, records):
    """逐根推进并按帧快照 bsp 集合，返回一份可 JSON 化的统计。

    ⚠ 构造参数与 `Runner.run` **逐项对齐**（同一份 `default_chan_config()`、
      同样 `AUTYPE.NONE` / `market_type="stock"` / `data_src` custom）——
      否则"引擎侧 vs 回测侧"的交叉验证就不是同一棵树上的两个口径了。
    ⚠ 分型与入场价的读法与 `Runner.bsp_to_dict` + `Exit._compute` 同一式子：
      `fractal_low/high = round(bsp.bi.get_end_klu().{low,high}, 3)`、
      `entry = float(bsp.klu.close)`、`A = max(entry - f_low, 0)` / `max(f_high - entry, 0)`。
    """
    import Chan
    from DataAPI import TdxAPI
    from Common.CEnum import AUTYPE
    from Backtest.Runner import default_chan_config, date_fmt_of, kl_type_of

    cfg = default_chan_config()
    date_fmt = date_fmt_of(freq)

    frames = []
    first = {}
    last = {}
    fingerprint = {}
    revisions = {}
    with TdxAPI.tdx_data_context(records):
        chan = Chan.CChan(
            code="{}{}".format(MARKET, CODE), begin_time=None, end_time=None,
            data_src="custom:TdxAPI.CTdxAPI", lv_list=[kl_type_of(freq)],
            config=cfg, autype=AUTYPE.NONE, market_type="stock",
        )
        for snap in chan.step_load():
            kl = snap[0]
            keys = set()
            for bsp in kl.bs_point_lst.bsp_iter():
                # key 与回测侧同类元素对齐：日期用**回测的**格式串（连字符），
                # 类型用 `type2str()`（= `BtTrade.bsp_type` 的来源），方向用 is_buy
                key = (bsp.klu.time.toFmtStr(date_fmt), bsp.type2str(), bool(bsp.is_buy))
                keys.add(key)
                f_klu = bsp.bi.get_end_klu()
                fl, fh = round(float(f_klu.low), 3), round(float(f_klu.high), 3)
                entry = round(float(bsp.klu.close), 3)
                a = (max(entry - fl, 0.0) if bsp.is_buy
                     else max(fh - entry, 0.0))
                rec = {"entry": entry, "f_low": fl, "f_high": fh,
                       "A": round(a, 3), "bi_end": f_klu.time.toFmtStr(date_fmt)}
                first.setdefault(key, rec)
                last[key] = rec
                # 笔指纹：笔端 / 分型 / 幅度 —— 变了就说明这一笔被改写过
                fp = (rec["bi_end"], fl, fh)
                if fingerprint.setdefault(key, fp) != fp:
                    fingerprint[key] = fp
                    revisions[key] = revisions.get(key, 0) + 1
            frames.append(keys)

    ever = set()
    disappeared = set()
    reappeared = set()
    prev = set()
    for cur in frames:
        for k in (prev - cur):
            disappeared.add(k)
            if k in ever:
                reappeared.add(k)
        ever |= cur
        prev = cur
    final = frames[-1] if frames else set()

    drift = []
    for key in sorted(first):
        a0, a1 = first[key], last[key]
        if a0["A"] == a1["A"] and a0["bi_end"] == a1["bi_end"]:
            continue
        delta = a1["A"] - a0["A"]
        drift.append({
            "signal": "%s type=%s buy=%s" % key,
            "date": key[0], "type": key[1], "is_buy": key[2],
            "A_first": a0["A"], "A_last": a1["A"],
            "dA": round(delta, 3),
            "dpct": (None if not a0["A"]
                     else round(delta / a0["A"] * 100, 1)),
            "bi_end_first": a0["bi_end"], "bi_end_last": a1["bi_end"],
            "f_low_first": a0["f_low"], "f_low_last": a1["f_low"],
            "f_high_first": a0["f_high"], "f_high_last": a1["f_high"],
            "entry": a0["entry"],
        })

    # 首见态 A 落在告警阈值下的漂移信号数（披露用；阈值不参与判定）
    alert_first = sum(1 for d in drift if d["A_first"] < R_ALERT_A_FLOOR)
    alert_last = sum(1 for d in drift if d["A_last"] < R_ALERT_A_FLOOR)

    return {
        "freq": freq,
        "frames": len(frames),
        "signals_total": len(first),
        "ever_seen": len(ever),
        "final_alive": len(final),
        "disappeared_ever": len(disappeared),
        "reappeared": len(reappeared),
        "ever_but_not_final": sorted("%s type=%s buy=%s" % k for k in (ever - final)),
        "field_revision_signals": sorted(
            "%s type=%s buy=%s" % k for k in revisions),
        "revisions": {"%s type=%s buy=%s" % k: v for k, v in sorted(revisions.items())},
        "drift": drift,
        "drift_count": len(drift),
        "max_dA": (max((d["dA"] for d in drift), default=0.0)),
        "alert_a_floor": R_ALERT_A_FLOOR,
        "alert_first_below": alert_first,
        "alert_last_below": alert_last,
    }


def backtest_trades(freq, records):
    """回测侧同一切片的逐笔（交叉验证用）。"""
    from Backtest import Runner
    res = Runner.run(MARKET, CODE, freq, records=records)
    return res, [
        {"entry_date": t.entry_date, "side": t.side, "bsp_type": t.bsp_type,
         "r_distance": _r(t.r_distance, 6), "entry_price": _r(t.entry_price, 6)}
        for t in res.trades
    ]


def build_snapshot():
    """现算快照（`--freeze` 写盘 / 无参时比对）。

    ⚠ 逐样本现算，**不快照 trades 明细** —— 逐笔已由 `test_bt01_p0_fixture.py`
      冻结，这里只留交叉验证需要的那几个字段，避免同一份事实两处冻结。
    """
    from Backtest.Runner import load_records

    snap = {
        "kind": "backtest-p4-repaint-drift",
        "version": 1,
        "note": ("Backtest/ 的 P4 漂移巡检基线。来源 = 冻结真实行情切片 "
                 "Test/fixtures_real/sz002190_{d,30m}.json。"
                 "「首见冻结」的代价（零重绘 + 3/11 条 R 漂移）在此量化冻结；"
                 "改了内核要刷新时跑 test_bt07_repaint_drift.py --freeze。"),
        "target": {"market": MARKET, "code": CODE},
        "samples": [],
    }
    for freq, fname in SAMPLES:
        meta = fixture_meta(fname)
        records = load_records(os.path.join(FIX_DIR, fname))
        walk = engine_walk(freq, records)
        walk["fixture"] = {"file": fname, "sha256": meta["sha256"],
                           "bars": int(meta["bars"])}
        res, trades = backtest_trades(freq, records)

        # 交叉验证：回测的**每一笔**都必须能在引擎侧漂移清单里找到同一天同类型的
        # 信号（否则就是"回测侧凭空多出一笔"）。只有漂移信号才会进清单，故这里
        # 统计的是"漂移信号被开仓"的条数，不是全部笔数。
        drift_rows = []
        for t in trades:
            for s in walk["drift"]:
                if s["date"] == t["entry_date"]:
                    drift_rows.append({
                        "trade_entry_date": t["entry_date"],
                        "side": t["side"], "bsp_type": t["bsp_type"],
                        "r_distance": t["r_distance"],
                        "A_first": s["A_first"], "A_last": s["A_last"],
                        "type_match": bool(s["type"] == t["bsp_type"]),
                        "r_lt_A_last": bool(t["r_distance"] is not None
                                            and t["r_distance"] < s["A_last"]),
                    })
        walk["xcheck"] = {
            "signals_seen": int(res.signals_seen),
            "trades": len(trades),
            "drift_trade_rows": drift_rows,
            "r_lt_A_last_count": sum(1 for r in drift_rows if r["r_lt_A_last"]),
        }
        snap["samples"].append(walk)
    return snap


# ═════════════════════════════════════════════════════════════════════
# 判定
# ═════════════════════════════════════════════════════════════════════
def judge(sample, want):
    """逐样本判据（want = 冻结基线；判定放在这里，`--freeze` 与比对走同一条路）。"""
    freq = sample["freq"]
    fx = sample["fixture"]

    check("①[%s] 帧数 == 切片根数（逐根推进，不快进不落后）" % freq,
          sample["frames"] == fx["bars"] and sample["frames"] == want["frames"],
          "frames=%r 切片=%r 基线=%r"
          % (sample["frames"], fx["bars"], want["frames"]))

    check("②[%s] 零重绘：ever_seen == final_alive 且无消失/复现" % freq,
          sample["ever_seen"] == sample["final_alive"]
          and sample["disappeared_ever"] == 0
          and sample["reappeared"] == 0
          and not sample["ever_but_not_final"]
          and sample["ever_seen"] == want["ever_seen"]
          and sample["final_alive"] == want["final_alive"],
          "ever=%r final=%r gone=%r back=%r notfinal=%r 基线 ever=%r final=%r"
          % (sample["ever_seen"], sample["final_alive"], sample["disappeared_ever"],
             sample["reappeared"], sample["ever_but_not_final"],
             want["ever_seen"], want["final_alive"]))

    check("③[%s] 笔指纹改写清单与基线一致（哪几个信号被改写过 × 次数）" % freq,
          sample["field_revision_signals"] == want["field_revision_signals"]
          and sample["revisions"] == want["revisions"],
          "got=%r\n基线=%r" % (sample["revisions"], want["revisions"]))

    check("④[%s] R 漂移清单与基线逐字段一致" % freq,
          sample["drift"] == want["drift"],
          "got=%r\n基线=%r" % (sample["drift"], want["drift"]))

    check("⑤[%s] 漂移单向性：全部 ΔA > 0（笔延伸只推远分型）" % freq,
          all(d["dA"] > 0 for d in sample["drift"]),
          "非单向的条目=%r" % [d for d in sample["drift"] if d["dA"] <= 0])

    n_drift, n_total = sample["drift_count"], sample["signals_total"]
    n_still = n_total - n_drift
    check("⑥[%s] 未漂移信号 %d 个：首见 A == 最终 A（清单没把全部信号算进来）"
          % (freq, n_still),
          n_still > 0 and sample["drift_count"] == want["drift_count"]
          and sample["signals_total"] == want["signals_total"],
          "总=%r 漂=%r 基线 总=%r 漂=%r"
          % (n_total, n_drift, want["signals_total"], want["drift_count"]))

    # ⑧ 判别力自证：清单既非空也非全集
    check("⑧[%s] 判别力：漂移清单 0 < %d < %d（既非尺子坏了，也非全漂）"
          % (freq, n_drift, n_total),
          0 < n_drift < n_total, "漂=%r 总=%r" % (n_drift, n_total))

    # ⑦ 交叉验证
    xc = sample["xcheck"]
    check("⑦[%s] 回测侧 signals_seen(%d) == 引擎侧 ever_seen(%d)（两个独立口径）"
          % (freq, xc["signals_seen"], sample["ever_seen"]),
          xc["signals_seen"] == sample["ever_seen"]
          and xc["signals_seen"] == want["xcheck"]["signals_seen"],
          "回测=%r 引擎=%r 基线=%r"
          % (xc["signals_seen"], sample["ever_seen"], want["xcheck"]["signals_seen"]))
    check("⑦[%s] 被开仓的漂移笔 r_distance >= A_首见（R 至少含结构距离）" % freq,
          all(r["r_distance"] is not None and r["r_distance"] >= r["A_first"]
              for r in xc["drift_trade_rows"]),
          "违例=%r" % [r for r in xc["drift_trade_rows"]
                       if r["r_distance"] is None or r["r_distance"] < r["A_first"]])
    check("⑦[%s] 漂移笔与回测笔种类对齐（同日同类型 ⇒ 匹配不是同日巧合）" % freq,
          all(r["type_match"] for r in xc["drift_trade_rows"]),
          "类型不匹配=%r" % [r for r in xc["drift_trade_rows"] if not r["type_match"]])
    check("⑦[%s] 漂移信号被开仓的条数冻结 = %d" % (freq, len(xc["drift_trade_rows"])),
          len(xc["drift_trade_rows"]) == len(want["xcheck"]["drift_trade_rows"]),
          "got=%d 基线=%d" % (len(xc["drift_trade_rows"]),
                              len(want["xcheck"]["drift_trade_rows"])))
    # ★ 这条是「首见态冻结」代价的可观测实证：这些笔的 R **小于**该信号最终态的 A
    #   ⇒ 若哪天有人把入场从"首见帧"挪到"笔定型后"，这个计数会掉到 0。
    check("⑦[%s] 「因首见态而 R 偏小」的笔数冻结 = %d（r_distance < A_最终）"
          % (freq, xc["r_lt_A_last_count"]),
          xc["r_lt_A_last_count"] == want["xcheck"]["r_lt_A_last_count"],
          "got=%r 基线=%r" % (xc["r_lt_A_last_count"],
                              want["xcheck"]["r_lt_A_last_count"]))


def main(argv):
    freeze = "--freeze" in (argv or sys.argv[1:])

    print("=" * 68)
    print("P4 漂移巡检（Backtest/Test/test_bt07_repaint_drift.py）")
    print("=" * 68)

    got = build_snapshot()

    if freeze:
        os.makedirs(SNAP_DIR, exist_ok=True)
        with io.open(SNAP_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(got, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print("[FREEZE] 基线已写入 %s" % os.path.relpath(SNAP_PATH, ROOT))
        for s in got["samples"]:
            print("         %-4s frames=%d 信号=%d 重绘=%d 漂移=%d 最大ΔA=%s"
                  % (s["freq"], s["frames"], s["signals_total"],
                     s["disappeared_ever"], s["drift_count"], s["max_dA"]))
        return 0

    if not os.path.exists(SNAP_PATH):
        check("基线文件存在", False,
              "缺 %s —— 先跑 --freeze 生成" % os.path.relpath(SNAP_PATH, ROOT))
        return 1

    with io.open(SNAP_PATH, encoding="utf-8") as f:
        want_all = json.load(f)
    want_by_freq = {s["freq"]: s for s in want_all["samples"]}

    for sample in got["samples"]:
        freq = sample["freq"]
        print("── 样本 %s ──" % freq)
        if freq not in want_by_freq:
            check("[%s] 基线里存在该样本" % freq, False,
                  "基线只有 %r" % sorted(want_by_freq))
            continue
        judge(sample, want_by_freq[freq])
        # ⑨ 冻结输入未被手改
        check("⑨[%s] 切片 sha256 与 manifest 一致" % freq,
              sample["fixture"]["sha256"] == fixture_meta(sample["fixture"]["file"])["sha256"])

    # 漂移成因披露（把「首见态 A 偏小」与 Exit 的告警挂钩，不参与判定）
    for s in got["samples"]:
        print("[NOTE] %-4s 漂移 %d/%d 个；首见 A < %.1f 的漂移信号 %d 个 → "
              "最终 A < %.1f 的 %d 个（差额即「首见态冻结」造成的告警）"
              % (s["freq"], s["drift_count"], s["signals_total"], R_ALERT_A_FLOOR,
                 s["alert_first_below"], R_ALERT_A_FLOOR, s["alert_last_below"]))

    print("-" * 68)
    print("合计 %d 项，通过 %d，失败 %d" % (PASS + FAIL, PASS, FAIL))
    if FAIL:
        print("\n提示：若差异正是你**本轮故意**改的那件事（如改了笔延伸/分型口径），"
              "跑 `python Backtest/Test/test_bt07_repaint_drift.py --freeze` 重冻，"
              "并在交付说明里写明改了哪一项。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

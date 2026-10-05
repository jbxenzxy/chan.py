# -*- coding: utf-8 -*-
"""
P0 端到端冻结快照（Backtest/Test/test_bt01_p0_fixture.py）
============================================================
设计文档 §6「P0 判据」①②③的可执行版本：拿**冻结的真实行情切片**
（`Test/fixtures_real/sz002190_d.json`，1393 根日线）把 `Backtest/` 跑一遍，
逐笔 + 汇总指标与冻结快照比对。

为什么必须是**逐笔**而不是只比笔数
---------------------------------------------------------------------
"笔数对得上"是最弱的断言：入场日错一天、R 取错锚（最终态而非首见态）、
A/B 成本口径搞反 —— 任何一种都能保住笔数 6，但每一笔的数字全变。
§5.6 的黄金基线是**逐笔明细**，本用例照抄同一口径（含 `r_distance` / `r_multiple` /
`net_return` / `exit_reason` / `bars_held`）。

覆盖（对应 §6 判据）
---------------------------------------------------------------------
  ① 冻结输入未被手改（sha256 vs `fixtures_real/manifest.json`）
  ② 全放行：6 笔逐笔一致 + 汇总指标一致（判据①）
  ③ 只放行 `0` 类：3 笔、期望 R（毛）+1.802（判据③ 前半）
  ④ 只放行 `3` 类：3 笔、期望 R（毛）−0.650（判据③ 后半）
  ⑤ `type_appended_after_freeze == 0`（§2.6 代价①，必须是 0 才算首见冻结成立）
  ⑥ 未平仓笔数 0（样本末根之前最后一笔已平；不为 0 说明出场链有分支没走到）
  ⑦ `Runner.load_records`（生产装载器）≡ `Test.gen_fixtures.load_records`
     （冻结切片的装载器）—— CLI 吃的是同一份格式，分叉了会静默拿到错数据

重冻结（改了内核之后**故意**刷新基线）
---------------------------------------------------------------------
    python Backtest/Test/test_bt01_p0_fixture.py --freeze

⚠ `--freeze` 会**覆盖**基线。先看 diff 里变的是不是"你刚改的那件事"再冻 ——
  基线一次全量刷新会把顺带引入的其他漂移一起洗白。

零网络、零 vipdoc 依赖（数据全部来自冻结 JSON；行情源是 `TdxAPI.tdx_data_context`
的 custom 免连网分支）。跑法：`python Backtest/Test/test_bt01_p0_fixture.py`
（退出码 0/1 即判决；已登记进 `Test/run_all.py` 的 COMPONENTS）
"""
import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKTEST_DIR)
for _p in (ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIX_DIR = os.path.join(ROOT, "Test", "fixtures_real")
SAMPLE = "sz002190_d.json"
SNAP_DIR = os.path.join(HERE, "snapshots")
SNAP_PATH = os.path.join(SNAP_DIR, "p0_sz002190_d.json")

MARKET, CODE, FREQ = "sz", "002190", "d"

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
            print("        " + str(detail).replace("\n", "\n        "))


# ─────────────────────────────────────────────────────────────────────
# 冻结输入完整性
# ─────────────────────────────────────────────────────────────────────
def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fixture_manifest_entry():
    with io.open(os.path.join(FIX_DIR, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    hit = [e for e in man["entries"] if e["file"] == SAMPLE]
    assert len(hit) == 1, "manifest 里找不到样本 %s" % SAMPLE
    return hit[0]


# ─────────────────────────────────────────────────────────────────────
# 取值 → 可 JSON 化（统一舍入：避免末位浮点噪声把基线打成红）
# ─────────────────────────────────────────────────────────────────────
def _r(x, nd):
    return None if x is None else round(float(x), nd)


def _bucket_snap(groups):
    out = {}
    for k, v in sorted(groups.items()):
        out[k] = {
            "n": int(v["n"]), "w": int(v["w"]), "l": int(v["l"]), "e": int(v["e"]),
            "avg_net_return": _r(v.get("avg_net_return"), 10),
            "expectancy_r": _r(v.get("expectancy_r"), 8),
        }
    return out


def _trade_snap(t):
    return {
        "trade_id": int(t.trade_id),
        "side": str(t.side),
        "bsp_type": str(t.bsp_type),
        "entry_date": t.entry_date,
        "entry_price": _r(t.entry_price, 6),
        "r_distance": _r(t.r_distance, 6),
        "shares": int(t.shares),
        "exit_date": t.exit_date,
        "exit_price": _r(t.exit_price, 6),
        "exit_reason": t.exit_reason,
        "bars_held": None if t.bars_held is None else int(t.bars_held),
        "r_multiple": _r(t.r_multiple, 8),
        "gross_return": _r(t.gross_return, 10),
        "net_return": _r(t.net_return, 10),
        "cost_cash": _r(t.cost_cash, 6),
        "open": bool(t.open_),
    }


def _metrics_snap(m):
    return {
        "n": int(m.n), "w": int(m.w), "l": int(m.l), "e": int(m.e), "u": int(m.u),
        "rej": int(m.rej), "filtered": int(m.filtered), "bars_total": int(m.bars_total),
        "win_rate": _r(m.win_rate, 10),
        "profit_loss_ratio": _r(m.profit_loss_ratio, 8),
        "profit_factor": _r(m.profit_factor, 8),
        "avg_net_return": _r(m.avg_net_return, 10),
        "expectancy_r": _r(m.expectancy_r, 8),
        "avg_bars_held": _r(m.avg_bars_held, 8),
        "max_win_r": _r(m.max_win_r, 8),
        "max_loss_r": _r(m.max_loss_r, 8),
        "by_bsp_type": _bucket_snap(m.by_bsp_type),
        "by_reason": _bucket_snap(m.by_reason),
    }


def _run_once(records, bsp_types=None):
    from Backtest import Runner
    from Backtest.Metrics import compute
    filt = None
    if bsp_types:
        from Backtest.Filter import filter_from_choices
        filt = filter_from_choices(bsp_types)
    res = Runner.run(MARKET, CODE, FREQ, records=records, bsp_filter=filt)
    return res, compute(res)


def build_snapshot():
    """现算一份快照（`--freeze` 写盘 / 无参时比对）。"""
    from Backtest.Runner import load_records
    entry = fixture_manifest_entry()
    records = load_records(os.path.join(FIX_DIR, SAMPLE))

    res, met = _run_once(records)
    snap = {
        "kind": "backtest-p0-freeze",
        "version": 1,
        "note": ("Backtest/ 的 P0 冻结基线。来源 = 冻结真实行情切片 "
                 "Test/fixtures_real/sz002190_d.json；逐笔口径见本文件对应测试的 "
                 "docstring。改了内核要刷新时跑 test_bt01_p0_fixture.py --freeze。"),
        "fixture": {
            "dir": "Test/fixtures_real", "file": SAMPLE,
            "sha256": entry["sha256"], "bars": int(entry["bars"]),
        },
        "target": {"market": MARKET, "code": CODE, "freq": FREQ},
        "run": {
            "bars_total": int(res.bars_total),
            "signals_seen": int(res.signals_seen),
            "signals_filtered": int(res.signals_filtered),
            "signals_rejected": int(res.signals_rejected),
            "type_appended_after_freeze": int(res.type_appended_after_freeze),
        },
        "metrics": _metrics_snap(met),
        "trades": [_trade_snap(t) for t in res.trades],
        "filter_scenarios": [],
    }
    for name, pick in (("only-0", "0"), ("only-3", "3")):
        sres, smet = _run_once(records, bsp_types=pick)
        snap["filter_scenarios"].append({
            "name": name, "bsp_types": pick,
            "n": int(smet.n), "expectancy_r": _r(smet.expectancy_r, 8),
            "trades": int(len(sres.trades)),
            "trade_ids": [int(t.trade_id) for t in sres.trades],
        })
    return snap


# ─────────────────────────────────────────────────────────────────────
# 递归比对（把差异定位到 JSON 路径，别只报"不相等"）
# ─────────────────────────────────────────────────────────────────────
def diff(got, want, path="", out=None):
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
    print("P0 端到端冻结快照（Backtest/Test/test_bt01_p0_fixture.py）")
    print("=" * 68)

    # ① 冻结输入未被手改
    entry = fixture_manifest_entry()
    got_sha = _sha256(os.path.join(FIX_DIR, SAMPLE))
    check("① 冻结输入 sha256 与 manifest 一致",
          got_sha == entry["sha256"],
          "got  =%s\nmanifest=%s" % (got_sha, entry["sha256"]))

    got = build_snapshot()

    if freeze:
        os.makedirs(SNAP_DIR, exist_ok=True)
        with io.open(SNAP_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(got, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print("[FREEZE] 基线已写入 %s" % os.path.relpath(SNAP_PATH, ROOT))
        print("         bars=%d signals=%d trades=%d"
              % (got["run"]["bars_total"], got["run"]["signals_seen"],
                 len(got["trades"])))
        return 0

    if not os.path.exists(SNAP_PATH):
        check("基线文件存在", False,
              "缺 %s —— 先跑 --freeze 生成" % os.path.relpath(SNAP_PATH, ROOT))
        return 1

    with io.open(SNAP_PATH, encoding="utf-8") as f:
        want = json.load(f)

    # ② 全放行逐笔 + 汇总
    d_run = diff(got["run"], want["run"], "run")
    d_tr = diff(got["trades"], want["trades"], "trades")
    d_met = diff(got["metrics"], want["metrics"], "metrics")
    d_tgt = diff(got["target"], want["target"], "target")
    d_fix = diff(got["fixture"], want["fixture"], "fixture")
    check("② 全放行：逐笔 + 汇总与基线一致",
          not (d_run or d_tr or d_met or d_tgt or d_fix),
          "\n".join(d_run + d_tr + d_met + d_tgt + d_fix))

    # ③④ 类型过滤两场景
    d_sc = diff(got["filter_scenarios"], want["filter_scenarios"], "filter_scenarios")
    check("③④ 类型过滤两场景与基线一致（only-0 / only-3）",
          not d_sc, "\n".join(d_sc))

    # ⑤ 冻结后类型追加必须为 0
    check("⑤ type_appended_after_freeze == 0（首见冻结成立，§2.6 代价①）",
          got["run"]["type_appended_after_freeze"] == 0,
          "got=%r" % got["run"]["type_appended_after_freeze"])

    # ⑥ 未平仓必须为 0
    n_open = sum(1 for t in got["trades"] if t["open"])
    check("⑥ 未平仓笔数 == 0（出场链所有分支都走到）", n_open == 0,
          "未平仓 %d 笔" % n_open)

    # ⑦ 生产的 records 装载器 ≡ 冻结切片的装载器（CLI 吃的是同一格式）
    try:
        from Test.gen_fixtures import load_records as _ref_load
        from Backtest.Runner import load_records as _bt_load
        p = os.path.join(FIX_DIR, SAMPLE)
        check("⑦ Runner.load_records ≡ Test.gen_fixtures.load_records（逐字段）",
              _bt_load(p) == _ref_load(p),
              "两份装载器输出不等 —— CLI 输入格式与 fixtures 格式已分叉")
    except Exception as e:                                     # noqa: BLE001
        check("⑦ Runner.load_records ≡ Test.gen_fixtures.load_records（逐字段）", False,
              "%s: %s" % (type(e).__name__, e))

    print("-" * 68)
    print("合计 %d 项，通过 %d，失败 %d" % (PASS + FAIL, PASS, FAIL))
    if FAIL:
        print("\n提示：若差异正是你**本轮故意**改的那件事，跑 "
              "`python Backtest/Test/test_bt01_p0_fixture.py --freeze` 重冻，"
              "并在交付说明里写明改了哪一项。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

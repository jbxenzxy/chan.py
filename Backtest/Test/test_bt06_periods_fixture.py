# -*- coding: utf-8 -*-
"""
逐周期冻结快照（Backtest/Test/test_bt06_periods_fixture.py）
============================================================
设计文档 §6「P0 判据④：★ 逐周期各跑一遍（`w / d / 30m / 15m / 5m`）」的可执行版本。

为什么单独立一个用例、而不是往 bt01 里加循环
---------------------------------------------------------------------
判据④ 的未知不在"跑得快不快"，而在**分钟级这条路径此前从未走过**：
切片来源不同（`fzline/*.lc5` 而非 `lday/*.day`）、合成链不同（`_resample_5m_to_30m`
/ `_resample_5m_to_15m` / `_resample_day_to_week`）、频率→`KL_TYPE` 映射不同。
任一处错（如把 30m 的 `KL_TYPE` 映射错、把周线当日期格式解析），都可能**安静地**
给出一个"看着像样"的结果。所以这里把**五个周期全部逐笔冻住**，任何人改动
内核或取数链，都必须在五个周期上同时自证没有漂移。

覆盖
---------------------------------------------------------------------
  ① 五份切片的 sha256 与 `fixtures_real/manifest.json` 一致（输入未被手改）
  ② 逐周期：`bars_total` / 信号三分类 / 逐笔 / 汇总指标 == 冻结基线
  ③ ★ 派生不变量：`signals_seen == 放行 + 过滤 + 拒收`（逐周期）
  ④ ★ 交叉验证：`signals_seen`（回测侧首见采集）== 引擎侧**最终** `bsp_iter()` 总数
     —— 两个独立口径数出来的买卖点数量必须相等。这是"数据真进了引擎、且采集没漏"
     的硬证据（尤其对 ⑤ 的周线 0 笔）。
  ⑤ ★ 周线 0 笔的**判别力自证**：断言周线上 `bi > 0` 且 `seg > 0` 且 `bsp == 0`
     —— 证明"0 笔"是**引擎判断的结果**，不是"数据没注入/映射错导致引擎空转"。
     没有这条，一个把周线映射成日线的 bug 也能产出"0 笔"并被当成合法。
  ⑥ `type_appended_after_freeze == 0`（逐周期；§2.6 代价①）

重冻结
---------------------------------------------------------------------
    python Backtest/Test/test_bt06_periods_fixture.py --freeze

⚠ `--freeze` 会覆盖基线。先确认 diff 里变的是"你刚改的那件事"。

零网络、零 vipdoc 依赖（数据全部来自冻结 JSON）。跑法：
    python Backtest/Test/test_bt06_periods_fixture.py
"""
import hashlib
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
SNAP_PATH = os.path.join(SNAP_DIR, "p0_periods.json")

MARKET, CODE = "sz", "002190"

# (freq, 切片文件, 用途说明) —— 顺序即 A 股开放周期顺序（Common.CEnum.STOCKS_FREQS）
PERIODS = [
    ("w", "sz002190_w.json", "周线（由日线合成）；本样本上 bsp=0 —— 见 ⑤ 的判别力自证"),
    ("d", "sz002190_d.json", "日线；基准样本（p0_sz002190_d.json 详版在 bt01）"),
    ("30m", "sz002190_30m.json", "30m（由 5m 合成）"),
    ("15m", "sz002190_15m.json", "15m（由 5m 合成）"),
    ("5m", "sz002190_5m.json", "5m（vipdoc fzline 原始周期）"),
]

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


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fixture_manifest():
    with io.open(os.path.join(FIX_DIR, "manifest.json"), encoding="utf-8") as f:
        return {e["file"]: e for e in json.load(f)["entries"]}


def _r(x, nd):
    return None if x is None else round(float(x), nd)


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
        "net_return": _r(t.net_return, 10),
        "open": bool(t.open_),
    }


def _metrics_snap(m):
    return {
        "n": int(m.n), "w": int(m.w), "l": int(m.l), "e": int(m.e), "u": int(m.u),
        "win_rate": _r(m.win_rate, 10),
        "expectancy_r": _r(m.expectancy_r, 8),
        "profit_factor": _r(m.profit_factor, 8),
        "avg_net_return": _r(m.avg_net_return, 10),
        "avg_bars_held": _r(m.avg_bars_held, 8),
        "max_win_r": _r(m.max_win_r, 8),
        "max_loss_r": _r(m.max_loss_r, 8),
    }


# ─────────────────────────────────────────────────────────────────────
# 引擎侧独立口径：直接构造 CChan 数最终 bsp / bi / seg
#   —— 与 Runner 的"首见采集"是两条不同的路：一个数**最终态**，一个数**首见帧**。
#      对得上才说明"采集没漏、且这段行情上没发生 bsp 被 clear_store_end 删除"。
# ─────────────────────────────────────────────────────────────────────
def engine_counts(freq, records):
    import Chan
    import DataAPI.TdxAPI as TdxAPI
    from Common.CEnum import AUTYPE
    from Backtest.Runner import kl_type_of, default_chan_config

    with TdxAPI.tdx_data_context(records):
        chan = Chan.CChan(
            code="%s%s" % (MARKET, CODE), begin_time=None, end_time=None,
            data_src="custom:TdxAPI.CTdxAPI", lv_list=[kl_type_of(freq)],
            config=default_chan_config(), autype=AUTYPE.NONE, market_type="stock",
        )
        for _ in chan.step_load():
            pass
    kl = chan[0]
    return {
        "klu": len(list(kl.klu_iter())),
        "bi": len(list(kl.bi_list)),
        "seg": len(list(kl.seg_list)),
        "bsp": len(list(kl.bs_point_lst.bsp_iter())),
    }


def build_snapshot():
    from Backtest import Runner
    from Backtest.Metrics import compute
    from Backtest.Runner import load_records

    out = {"kind": "backtest-p0-periods-freeze", "version": 1,
           "note": ("Backtest/ 的逐周期（P0 判据④）冻结基线。来源 = Test/fixtures_real/ "
                    "的五份真实行情切片；重冻跑 test_bt06_periods_fixture.py --freeze。"),
           "target": {"market": MARKET, "code": CODE},
           "periods": []}
    man = fixture_manifest()
    for freq, fname, note in PERIODS:
        records = load_records(os.path.join(FIX_DIR, fname))
        res = Runner.run(MARKET, CODE, freq, records=records)
        met = compute(res)
        out["periods"].append({
            "freq": freq,
            "file": fname,
            "note": note,
            "sha256": man[fname]["sha256"],
            "bars_in_fixture": int(man[fname]["bars"]),
            "run": {
                "bars_total": int(res.bars_total),
                "signals_seen": int(res.signals_seen),
                "signals_filtered": int(res.signals_filtered),
                "signals_rejected": int(res.signals_rejected),
                "type_appended_after_freeze": int(res.type_appended_after_freeze),
            },
            "metrics": _metrics_snap(met),
            "trades": [_trade_snap(t) for t in res.trades],
        })
    return out


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
    print("逐周期冻结快照（Backtest/Test/test_bt06_periods_fixture.py）")
    print("=" * 68)

    man = fixture_manifest()

    # ① 输入完整性
    bad_sha = []
    for freq, fname, _ in PERIODS:
        p = os.path.join(FIX_DIR, fname)
        if not os.path.exists(p):
            bad_sha.append("%s 缺文件" % fname)
            continue
        if _sha256(p) != man.get(fname, {}).get("sha256"):
            bad_sha.append("%s sha256 漂移" % fname)
    check("① 五份切片 sha256 与 manifest 一致", not bad_sha, "\n".join(bad_sha))

    got = build_snapshot()

    if freeze:
        os.makedirs(SNAP_DIR, exist_ok=True)
        with io.open(SNAP_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(got, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print("[FREEZE] 基线已写入 %s" % os.path.relpath(SNAP_PATH, ROOT))
        for pr in got["periods"]:
            print("         %-4s bars=%-6d signals=%-4d trades=%d"
                  % (pr["freq"], pr["run"]["bars_total"],
                     pr["run"]["signals_seen"], len(pr["trades"])))
        return 0

    if not os.path.exists(SNAP_PATH):
        check("基线文件存在", False,
              "缺 %s —— 先跑 --freeze 生成" % os.path.relpath(SNAP_PATH, ROOT))
        return 1

    with io.open(SNAP_PATH, encoding="utf-8") as f:
        want = json.load(f)

    # ② 逐周期与基线一致（一次报出所有周期的差异）
    d_all = diff(got["periods"], want["periods"], "periods")
    check("② 逐周期 bars / 信号 / 逐笔 / 汇总 == 基线（五周期）",
          not d_all, "\n".join(d_all))

    # ③ ★ 派生不变量：放行信号数 == 开仓笔数
    #    "放行" = seen − 过滤 − 拒收；每笔交易由一次放行开仓产生 ⇒ 两者必须相等。
    #    ⚠ 本等式依赖"同一帧最多一个放行信号"（Runner 开仓后即 break，同帧后续放行
    #      信号不进任何桶）—— 在本样本的五周期上均成立。将来若某样本出现同帧多放行，
    #      本条要放宽为 ≥，并给多出的信号补一个桶（那属于内核口径改动，须同步改 Runner）。
    bad_inv = []
    for pr in got["periods"]:
        r = pr["run"]
        allowed = r["signals_seen"] - r["signals_filtered"] - r["signals_rejected"]
        if allowed != len(pr["trades"]):
            bad_inv.append("%s: 放行 %d ≠ 开仓 %d 笔（seen=%d 过滤=%d 拒收=%d）"
                           % (pr["freq"], allowed, len(pr["trades"]),
                              r["signals_seen"], r["signals_filtered"], r["signals_rejected"]))
    check("③ 派生不变量：放行信号数 == 开仓笔数（逐周期）", not bad_inv, "\n".join(bad_inv))

    # ④ 交叉验证：回测侧首见采集 == 引擎侧最终 bsp（独立口径）
    from Backtest.Runner import load_records
    eng_cache = {}

    def _eng(freq, fname):
        if freq not in eng_cache:
            eng_cache[freq] = engine_counts(
                freq, load_records(os.path.join(FIX_DIR, fname)))
        return eng_cache[freq]

    bad_x, detail_x = [], []
    for pr in got["periods"]:
        eng = _eng(pr["freq"], pr["file"])
        got_seen = pr["run"]["signals_seen"]
        detail_x.append("  %-4s 回测首见=%-4d 引擎最终 bsp=%-4d (bi=%d seg=%d)"
                        % (pr["freq"], got_seen, eng["bsp"], eng["bi"], eng["seg"]))
        if got_seen != eng["bsp"]:
            bad_x.append("%s: 回测首见 %d ≠ 引擎 bsp %d" % (pr["freq"], got_seen, eng["bsp"]))
    print("  引擎侧独立口径对照：")
    for line in detail_x:
        print(line)
    check("④ 交叉验证：signals_seen == 引擎最终 bsp 总数（逐周期）",
          not bad_x, "\n".join(bad_x))

    # ⑤ 周线 0 笔的判别力自证（0 只能是"引擎判断"，不能是"数据没进去"）
    wk = [pr for pr in got["periods"] if pr["freq"] == "w"][0]
    eng_w = _eng("w", wk["file"])
    ok_w = (eng_w["bi"] > 0 and eng_w["seg"] > 0 and eng_w["bsp"] == 0
            and eng_w["klu"] == wk["run"]["bars_total"])
    check("⑤ 周线 0 笔是引擎判断（bi>0 且 seg>0 且 bsp==0，且 klu==bars_total）",
          ok_w,
          "klu=%d bi=%d seg=%d bsp=%d（期望 klu==bars_total、bi>0、seg>0、bsp==0）"
          % (eng_w["klu"], eng_w["bi"], eng_w["seg"], eng_w["bsp"]))

    # ⑥ 首见冻结成立（逐周期）
    bad_fr = [pr["freq"] for pr in got["periods"]
              if pr["run"]["type_appended_after_freeze"] != 0]
    check("⑥ type_appended_after_freeze == 0（逐周期）", not bad_fr,
          "非 0 的周期：%s" % bad_fr)

    print("-" * 68)
    print("合计 %d 项，通过 %d，失败 %d" % (PASS + FAIL, PASS, FAIL))
    if FAIL:
        print("\n提示：若差异正是你**本轮故意**改的那件事，跑 "
              "`python Backtest/Test/test_bt06_periods_fixture.py --freeze` 重冻，"
              "并在交付说明里写明改了哪一项。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

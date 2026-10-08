# -*- coding: utf-8 -*-
"""Test/fixtures_real/ 的生成器与校验器（可复现重冻）

背景
----
本目录是**真实行情冻结切片**（与 `Test/fixtures/` 的合成数据不同）：给回测
（`Backtest/`）提供零网络、零 vipdoc 依赖的离线样本。

原状态：切片一次性冻结，**仓内没有生成器** ⇒ 想加一个周期（P0 判据④ 需要
`w/30m/15m/5m`）或换一个截止日，只能手工重写 JSON、无从复现。
本脚本补上生成器，把「重冻」变成一条命令。

数据源
------
本机通达信安装目录下的 `vipdoc`（`<market>/lday/*.day` 日线、`<market>/fzline/*.lc5`
5 分钟线），经 `DataAPI.TdxAPI.read_main_level_records` **前复权**读取 —— 与页面
取数走同一条路，保证回测样本与页面所见同源。

⇒ 生成必须依赖本机 vipdoc（这是数据的客观来源，无 vipdoc 时明确报错退出，
   不造合成数据兜底 —— 那会让「真实样本」名不副实）。
⇒ 校验（`--check`）只读冻结 JSON + manifest，**零 vipdoc 依赖**，任何机器可跑，
   门禁里用的就是它。

用法
----
  python Test/gen_fixtures_real.py --vipdoc <本机通达信目录>\\vipdoc
      （路径不写死：取本机 AppConfig.tdx_install_dir 或按环境变量
        TDX_INSTALL_DIR 指定，默认值见 App/AppConfig.py _default_tdx_install_dir()）
      生成全部样本。已存在且字节一致的**不动**；已存在但字节不同的**默认拒绝
      覆盖**并报出差异（防止无意重冻让下游基线全红），要覆盖加 `--force`。

  python Test/gen_fixtures_real.py --check
      只按 manifest 里的 sha256 校验现有切片未被手改。零外部依赖。

  python Test/gen_fixtures_real.py --check --update-hashes
      有意重冻后刷新 manifest 的 sha256 与根数/区间（不改数据文件）。

注意
----
`vipdoc_dir` 是被冻结进 manifest 的**读取配置**（供复现时核对），不是本脚本的
环境依赖；换机重跑生成时须显式传 `--vipdoc`。
"""
import argparse
import datetime
import hashlib
import json
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TEST_DIR)
for _p in (TEST_DIR, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIX_DIR = os.path.join(TEST_DIR, "fixtures_real")
MANIFEST = os.path.join(FIX_DIR, "manifest.json")

# ── 样本表（生成顺序 = manifest entries 顺序）─────────────────────────────
# (market, code, freq, note)
# 频段说明：A 股开放周期 = w / d / 30m / 15m / 5m（Common.CEnum.STOCKS_FREQS）。
# vipdoc 的 lc5 只保留近期数据（本机约 1 年），故分钟级样本的时间窗短于日线
# ——这不是缺陷，是数据源的客观边界，已写进各条 note。
SAMPLES = [
    ("sz", "002190", "d", "日线 5.7 年。判别力最强：宽松 6 个 57th-T0 / 严格 0 个"),
    ("sz", "002190", "w", "周线；由前复权日线合成（TdxAPI._resample_day_to_week）"),
    ("sz", "002190", "30m", "30m；由前复权 5m 合成（_resample_5m_to_30m）"),
    ("sz", "002190", "15m", "15m；由前复权 5m 合成（_resample_5m_to_15m）"),
    ("sz", "002190", "5m", "5m；vipdoc fzline 原始周期（lc5 仅保留约 1 年）"),
]

MANIFEST_NOTE = (
    "来源：本机通达信 vipdoc（经 TdxAPI.read_main_level_records 前复权读取）的真实行情切片，"
    "冻结为离线 JSON。与 fixtures/ 不同 —— fixtures/ 为 gen_fixtures.py 合成且受 "
    "fixtures_integrity 校验，本目录为真实样本，由 Test/gen_fixtures_real.py 生成、"
    "并由其 --check 校验 sha256。"
)


def _file_name(market, code, freq):
    return "%s%s_%s.json" % (market, code, freq)


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def dump_records(bars):
    """datetime → ISO 字符串。与 Test/gen_fixtures.py 的同名函数同形。"""
    out = []
    for b in bars:
        r = dict(b)
        r["dt"] = b["dt"].strftime("%Y-%m-%d %H:%M:%S")
        out.append(r)
    return out


def _write_json(path, obj):
    """落盘形态与既有切片一致（indent=1 / sort_keys / LF / 无 BOM）。

    newline="\\n" 必须显式指定：文本模式在 Windows 会把 \\n 翻成 \\r\\n，
    让冻结文件随「在哪台机器上生成」而变形。
    """
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)


def load_manifest():
    with open(MANIFEST, "r", encoding="utf-8") as f:
        return json.load(f)


def _entry_for(market, code, freq, note, obj):
    path = os.path.join(FIX_DIR, _file_name(market, code, freq))
    return {
        "bars": len(obj),
        "date_from": obj[0]["dt"],
        "date_to": obj[-1]["dt"],
        "file": _file_name(market, code, freq),
        "freq": freq,
        "note": note,
        "sha256": _sha256(path) if os.path.exists(path) else "",
        "symbol": "%s%s" % (market, code),
    }


# ── 生成 ────────────────────────────────────────────────────────────────
def do_generate(vipdoc_dir, force=False):
    from DataAPI.TdxAPI import read_main_level_records, set_tdx_config

    set_tdx_config(vipdoc_dir=vipdoc_dir, forward_adjust_enabled=True)
    os.makedirs(FIX_DIR, exist_ok=True)

    created, unchanged, conflict = [], [], []
    for market, code, freq, note in SAMPLES:
        records, did_adjust = read_main_level_records(market, code, freq)
        if not records:
            print("[FAIL] %s%s %s：vipdoc 无数据（检查 vipdoc 路径 / 该周期是否已下载）"
                  % (market, code, freq))
            return 2
        obj = dump_records(records)
        path = os.path.join(FIX_DIR, _file_name(market, code, freq))
        if os.path.exists(path):
            old = open(path, "rb").read()
            new = json.dumps(obj, ensure_ascii=False, indent=1,
                             sort_keys=True).encode("utf-8")
            if old == new:
                unchanged.append(path)
                print("[ok]   %-16s 字节一致，未改动（%d 根，前复权=%s）"
                      % (os.path.basename(path), len(obj), did_adjust))
                continue
            conflict.append(path)
            if not force:
                print("[DIFF] %-16s 已存在但内容不同（%d 根 vs 现文件）——默认拒绝覆盖"
                      % (os.path.basename(path), len(obj)))
                continue
        _write_json(path, obj)
        created.append(path)
        print("[gen]  %-16s %d 根  %s -> %s  前复权=%s"
              % (os.path.basename(path), len(obj), obj[0]["dt"], obj[-1]["dt"], did_adjust))

    if conflict and not force:
        print("\n[STOP] 有 %d 份切片与生成结果不一致；确认是有意重冻后加 --force 重跑。"
              % len(conflict))
        return 3

    entries = [_entry_for(m, c, f, n, dump_records(read_main_level_records(m, c, f)[0]))
               for m, c, f, n in SAMPLES]
    manifest = {
        "entries": entries,
        "kind": "real-market-slice",
        "note": MANIFEST_NOTE,
        "read_config": {
            "forward_adjust_enabled": True,
            "vipdoc_dir": vipdoc_dir,
        },
        "version": 1,
    }
    _write_json(MANIFEST, manifest)
    print("\n[gen]  manifest.json：%d 条" % len(entries))
    print("[summary] 新建 %d / 未变 %d / 冲突 %d" % (len(created), len(unchanged), len(conflict)))
    return 0


# ── 校验（零 vipdoc 依赖）───────────────────────────────────────────────
def do_check(update_hashes=False):
    mf = load_manifest()
    rows, bad = [], 0
    for e in mf["entries"]:
        path = os.path.join(FIX_DIR, e["file"])
        if not os.path.exists(path):
            print("[FAIL] 缺文件 %s" % e["file"])
            bad += 1
            continue
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        got = _sha256(path)
        ok_sha = (got == e.get("sha256"))
        ok_bars = (len(obj) == e.get("bars"))
        ok_span = (obj[0]["dt"] == e.get("date_from") and obj[-1]["dt"] == e.get("date_to"))
        status = "PASS" if (ok_sha and ok_bars and ok_span) else "FAIL"
        if status == "FAIL":
            bad += 1
        print("[%s] %-16s bars=%-6d %s -> %s  sha256=%s"
              % (status, e["file"], len(obj), obj[0]["dt"], obj[-1]["dt"],
                 "ok" if ok_sha else "漂移"))
        if update_hashes and not ok_sha:
            e["sha256"] = got
            e["bars"] = len(obj)
            e["date_from"], e["date_to"] = obj[0]["dt"], obj[-1]["dt"]
            rows.append(e["file"])
    if update_hashes and rows:
        _write_json(MANIFEST, mf)
        print("[check] 已刷新 manifest：%s" % ", ".join(rows))
    print("[check] %d 条，失败 %d 条" % (len(mf["entries"]), bad))
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Test/fixtures_real/ 生成器与校验器")
    ap.add_argument("--vipdoc", help="通达信 vipdoc 目录（生成时必填）")
    ap.add_argument("--check", action="store_true", help="只校验 sha256（零外部依赖）")
    ap.add_argument("--update-hashes", action="store_true",
                    help="配合 --check：刷新 manifest 的 sha256/根数/区间")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在但内容不同的切片")
    args = ap.parse_args(argv)

    if args.check:
        return do_check(update_hashes=args.update_hashes)
    if not args.vipdoc:
        ap.error("生成需显式指定 --vipdoc <通达信 vipdoc 目录>（或改用 --check）")
    return do_generate(args.vipdoc, force=args.force)


if __name__ == "__main__":
    sys.exit(main())

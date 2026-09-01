# -*- coding: utf-8 -*-
"""
G4 · 期货选点/删点 —— 并发守护（对应审计矩阵 G4，P2）

期货 select_point / delete_point 此前**零并发覆盖**，只有 test_phase3 的
静态边界检查。本用例直达**存储层**（选点管线本身要真实 TqApi 连接，
离线不可达，但并发安全的关键在存储层）：

  AppData.save_point_time          (写选点，持 _user_store_lock)
  AppData.clear_saved_point_time   (清点，持 _user_store_lock)
  AppData.get_saved_point_time     (锁内读，审计 P2 收敛后的安全读)
  AppSSE._get_saved_point         (审计点名的**无锁读**，check-then-act 隐患)

守护目标：
  ① 不同合约并发选点：全部落盘、无丢失、无重复行、内存态与 CSV 一致
  ② 同合约「选点 / 清点」高频并发：不崩溃，CSV 始终整行有效或整行空
  ③ 无锁读路径 _get_saved_point 在并发写下永不抛异常（KeyError/中途迭代）
  ④ 锁内读 get_saved_point_time 在并发写下返回自洽值（不读到撕裂态）

退出码语义：发现缺陷 → 非 0；全绿 → 0（可直接接入 CI）。
"""
import csv
import io
import os
import random
import sys
import tempfile
import threading
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)

import _stub_env  # noqa: E402
_stub_env.install()

from App import AppData, AppSSE  # noqa: E402
from App.AppData import app_data  # noqa: E402

_results = []


def rec(tag, desc, ok, detail=""):
    _results.append((tag, ok))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark}  {tag}  {desc}")
    if detail:
        print(f"           └─ {detail}")


def _tmp_file():
    d = tempfile.mkdtemp(prefix="g4_saved_point_")
    return os.path.join(d, "saved_point.csv")


class _CfgProxy:
    """只读代理：覆盖 app_config 的若干派生属性（saved_point_file 等），
    其余透传到真实配置。用于把选点持久化文件重定向到临时目录，
    **绝不触碰真实通达信数据**，退出即随临时目录回收。"""

    def __init__(self, real, **overrides):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_ov", overrides)

    def __getattr__(self, name):
        ov = object.__getattribute__(self, "_ov")
        if name in ov:
            return ov[name]
        return getattr(object.__getattribute__(self, "_real"), name)


def _redirect_saved_point(tmp_path):
    """把 app_data 的选点文件重定向到临时路径（不改真实数据）。"""
    import AppData as _ad
    _ad.app_config = _CfgProxy(_ad.app_config, saved_point_file=tmp_path)


def _read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _valid_time(s):
    s = (s or "").strip()
    return s == "" or (" " in s and s[0].isdigit())


def test_concurrent_distinct_saves():
    """① 多页同合约并发选点：全部落盘、无丢失、无重复、内存态与 CSV 一致。

    模拟真实并发：4 个线程各对**全部 16 个合约**并发选点（同合约多页竞写）。
    各写者对同一合约写入**相同**时间值，故锁内 last-writer-wins 是安全的；
    一旦去掉 _user_store_lock（见 _mutate_p2.py g4-lock），读-改-写窗口暴露，
    写者交错会丢失部分合约的选点 → 本条变红。
    """
    _redirect_saved_point(_tmp_file())
    app_data._saved_point_times = {}
    freq = next((f for f in ("15s", "d") if app_data.freq_to_col(f)), "d")
    col = app_data.freq_to_col(freq)

    n = 16
    syms = [f"KQ.m{i:04d}" for i in range(n)]
    # 同一合约各写者时间值一致 → 锁内/解锁后时间都合法，仅「是否丢合约」区分成败
    times = {s: f"2026-01-{i + 1:02d} 09:30:00" for i, s in enumerate(syms)}
    expected_times = set(times.values())
    errs = []

    def worker(tid):
        try:
            for s in syms:  # 每个线程写全部 16 个合约（重叠竞写）
                app_data.save_point_time(s, f"name_{s}", freq, times[s])
        except Exception as exc:  # noqa: BLE001
            errs.append(f"T{tid}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)

    rows = _read_rows(app_data.saved_point_file)
    by_code = {r["code"]: r for r in rows}
    all_present = all(s in by_code for s in syms)
    # 时间合法（同合约各写者值一致，落盘值必在预期集合）
    csv_times_ok = all(by_code[s][col] in expected_times for s in syms)
    mem_ok = all(
        app_data.get_saved_point_time(s, freq) in expected_times for s in syms
    )
    no_dup = len(by_code) == len(rows)
    ok = (not errs) and all_present and csv_times_ok and mem_ok and no_dup
    rec("①", "16 合约并发选点(多页竞写)：全部落盘 / 无丢失 / 无重复行",
        ok,
        f"errs={len(errs)} present={all_present} csv_ok={csv_times_ok} "
        f"mem_ok={mem_ok} no_dup={no_dup}; 缺失="
        f"{[s for s in syms if s not in by_code]}" if not ok else "")


def test_save_clear_race():
    """② 同合约「选点 / 清点」高频并发：CSV 始终整行有效或整行空，不崩溃。"""
    _redirect_saved_point(_tmp_file())
    app_data._saved_point_times = {}
    freq = next((f for f in ("15s", "d") if app_data.freq_to_col(f)), "d")
    col = app_data.freq_to_col(freq)
    sym = "KQ.i9999"
    t_val = "2026-05-05 14:00:00"
    errs = []
    rounds = 200

    def saver():
        for _ in range(rounds):
            try:
                app_data.save_point_time(sym, "idx", freq, t_val)
            except Exception as exc:  # noqa: BLE001
                errs.append(f"save: {type(exc).__name__}: {exc}")

    def clearer():
        for _ in range(rounds):
            try:
                app_data.clear_saved_point_time(sym, freq)
            except Exception as exc:  # noqa: BLE001
                errs.append(f"clear: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=saver), threading.Thread(target=clearer)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)

    rows = _read_rows(app_data.saved_point_file)
    # 行级完整性：每个 code 行的 col 要么是合法时间，要么为空；绝不半截
    csv_ok = all(_valid_time(r.get(col, "")) for r in rows)
    ok = (not errs) and csv_ok
    rec("②", "同合约 选点×清点 高频并发：不崩溃且 CSV 始终整行有效/整行空",
        ok, f"errs={len(errs)} csv_ok={csv_ok} rows={len(rows)}")


def test_unguarded_reader_under_writers():
    """③ 无锁读 _get_saved_point 在并发写下永不抛异常。"""
    _redirect_saved_point(_tmp_file())
    app_data._saved_point_times = {}
    freq = next((f for f in ("15s", "d") if app_data.freq_to_col(f)), "d")
    syms = [f"KQ.r{i:03d}" for i in range(8)]
    for i, s in enumerate(syms):
        app_data.save_point_time(s, "n", freq, f"2026-02-{i + 1:02d} 10:00:00")

    stop = threading.Event()
    reader_errs = []
    writer_errs = []

    def reader():
        while not stop.is_set():
            s = random.choice(syms)
            try:
                a = AppSSE._get_saved_point(s, freq)        # 审计点名的无锁读
                b = app_data.get_saved_point_time(s, freq)  # 锁内读
                # 值必须合法：合法时间串或空串，绝不能是 None / 非 str
                if a is not None and not isinstance(a, str):
                    reader_errs.append(f"_get 返回 {type(a)}")
                if not isinstance(b, str):
                    reader_errs.append(f"get_saved 返回 {type(b)}")
            except Exception as exc:  # noqa: BLE001
                reader_errs.append(f"{type(exc).__name__}: {exc}")

    def writer():
        while not stop.is_set():
            try:
                app_data.save_point_time(
                    random.choice(syms), "n", freq,
                    f"2026-03-{random.randint(1, 28):02d} 11:11:11")
            except Exception as exc:  # noqa: BLE001
                writer_errs.append(f"{type(exc).__name__}: {exc}")

    rt = threading.Thread(target=reader)
    wt = threading.Thread(target=writer)
    rt.start()
    wt.start()
    rt.join(1.5)
    wt.join(1.5)
    stop.set()
    rt.join(5)
    wt.join(5)

    ok = (not reader_errs) and (not writer_errs)
    rec("③", "无锁读 _get_saved_point 在并发写下永不抛异常", ok,
        f"reader_errs={len(reader_errs)} writer_errs={len(writer_errs)} "
        f"samples={reader_errs[:2]}" if not ok else "")


def test_guarded_read_consistency():
    """④ 锁内读 get_saved_point_time 在并发写下返回自洽值（无撕裂态）。"""
    _redirect_saved_point(_tmp_file())
    app_data._saved_point_times = {}
    freq = next((f for f in ("15s", "d") if app_data.freq_to_col(f)), "d")
    sym = "KQ.c9999"
    final = "2026-06-06 08:08:08"
    stop = threading.Event()
    read_errs = []

    def reader():
        while not stop.is_set():
            try:
                v = app_data.get_saved_point_time(sym, freq)
                # 锁内读：要么 "" 要么合法时间，绝不半截串
                if v and not _valid_time(v):
                    read_errs.append(f"非法值: {v!r}")
            except Exception as exc:  # noqa: BLE001
                read_errs.append(f"{type(exc).__name__}: {exc}")

    def writer():
        while not stop.is_set():
            try:
                app_data.save_point_time(sym, "c", freq, final)
            except Exception:  # noqa: BLE001
                pass

    rt = threading.Thread(target=reader)
    wt = threading.Thread(target=writer)
    rt.start()
    wt.start()
    rt.join(1.0)
    wt.join(1.0)
    stop.set()
    rt.join(5)
    wt.join(5)
    # 终态必须收敛到最终值（写者全程写同一 final）
    final_val = app_data.get_saved_point_time(sym, freq)
    ok = (not read_errs) and (final_val == final)
    rec("④", "锁内读 get_saved_point_time 并发下自洽，终态收敛", ok,
        f"read_errs={len(read_errs)} final={final_val!r} expect={final!r}")


def main():
    print("=" * 68)
    print("G4 · 期货选点/删点 并发守护")
    print("=" * 68)
    try:
        test_concurrent_distinct_saves()
        test_save_clear_race()
        test_unguarded_reader_under_writers()
        test_guarded_read_consistency()
    except Exception:
        traceback.print_exc()
        return 2
    failed = [t for (t, ok) in _results if not ok]
    print("-" * 68)
    print(f"合计 {len(_results)} 项，{'全部通过' if not failed else str(len(failed)) + ' 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

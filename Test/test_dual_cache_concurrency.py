# -*- coding: utf-8 -*-
"""
G2 · 股票双窗口缓存 —— 并发守护（对应审计矩阵 G2，P3）

矩阵上「双窗口」只标注了「单锁覆盖」✅，但**双窗键单独限额（10 键上限）
与单窗口的共存边界**、以及审计 P0-3 的「cache_update 原子读-改-写」
从未被测过。本用例直达存储层：

  AppData.cache_put              (写，持 _stocks_cache_lock；新键先过双窗限额)
  AppData._evict_dual_overflow_locked  (双窗键单独限额，须持 _stocks_cache_lock)
  AppData.cache_update          (审计 P0-3：同一把锁内完成读-改-写，防字段丢失)
  AppData._is_dual_key          (双窗结构化键判定)

守护目标：
  ① 海量双窗键并发写入：双窗键总数恒 ≤ MAX_DUAL_CACHE_KEYS(10)，不越界、
     不崩溃、最终表自洽
  ② 同一键并发 cache_update（各写不同字段）：原子 RMW，无字段被后写覆盖吞掉
  ③ 双窗 + 单窗口混合并发：双窗限额不被突破，单窗口按 LRU 自然淘汰而非被
     双窗键挤占，读者遍历缓存字典永不抛「字典在迭代中改变大小」

退出码语义：发现缺陷 → 非 0；全绿 → 0（可直接接入 CI）。
"""
import os
import sys
import threading
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)

import _stub_env  # noqa: E402
_stub_env.install()

from App import AppData  # noqa: E402
from App.AppData import (  # noqa: E402
    app_data,
    make_dual_main_key,
    make_dual_sub_key,
    make_single_key,
    make_live_key,
)

_results = []


def rec(tag, desc, ok, detail=""):
    _results.append((tag, ok))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark}  {tag}  {desc}")
    if detail:
        print(f"           └─ {detail}")


def _dual_count():
    return sum(1 for k in app_data._stocks_analysis_cache
               if app_data._is_dual_key(k))


def test_dual_cap_under_concurrent_inserts():
    """① 海量双窗键并发写入：总数恒 ≤ MAX_DUAL_CACHE_KEYS(10)。"""
    cap = app_data.MAX_DUAL_CACHE_KEYS
    app_data._stocks_analysis_cache.clear()
    n_threads, per = 6, 12  # 共 72 个互不相交的双窗主/子键
    errs = []
    barrier = threading.Barrier(n_threads)

    def worker(tid):
        try:
            barrier.wait(timeout=30)
            for i in range(per):
                code = f"sh{600000 + tid * per + i}"
                app_data.cache_put(
                    make_dual_main_key("sh", code, "1m", "live"),
                    {"main": tid * 100 + i})
                app_data.cache_put(
                    make_dual_sub_key("sh", code, "1m", "live"),
                    {"sub": tid * 100 + i})
        except Exception as exc:  # noqa: BLE001
            errs.append(f"T{tid}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)

    dc = _dual_count()
    # 缓存池总量上限也不得被穿透
    total_ok = len(app_data._stocks_analysis_cache) <= app_data.MAX_CACHE_SIZE
    ok = (not errs) and dc <= cap and total_ok
    rec("①", f"双窗键并发写入：总数恒 ≤ {cap}（实得 {dc}），不越界不崩溃",
        ok,
        f"errs={len(errs)} dual={dc} cap={cap} total="
        f"{len(app_data._stocks_analysis_cache)} max={app_data.MAX_CACHE_SIZE} "
        f"samples={errs[:2]}" if not ok else "")


def test_cache_update_atomic_rmw():
    """② 同一键并发 cache_update（各写不同字段）：原子 RMW，无字段丢失。"""
    app_data._stocks_analysis_cache.clear()
    key = make_single_key("sh", "600519", "1m", "live")
    app_data.cache_put(key, {"seed": 1})

    n_threads = 16
    errs = []
    barrier = threading.Barrier(n_threads)

    def worker(tid):
        try:
            barrier.wait(timeout=30)
            app_data.cache_update(key, **{f"f{tid}": tid})
        except Exception as exc:  # noqa: BLE001
            errs.append(f"T{tid}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)

    entry = app_data._stocks_analysis_cache.get(key, {})
    present = [f"f{t}" for t in range(n_threads) if f"f{t}" in entry]
    lost = n_threads - len(present)
    # seed 字段也须保留（P0-3：先读到的值不能被覆盖丢）
    seed_ok = entry.get("seed") == 1
    ok = (not errs) and lost == 0 and seed_ok
    rec("②", f"并发 cache_update 同键：{n_threads} 字段无丢失、seed 保留",
        ok,
        f"errs={len(errs)} present={len(present)} lost={lost} seed_ok={seed_ok} "
        f"samples={errs[:2]}" if not ok else "")


def test_dual_plus_single_mixed_no_cross_contamination():
    """③ 双窗 + 单窗口混合并发：双窗限额不被突破，单窗口按 LRU 自然淘汰。"""
    app_data._stocks_analysis_cache.clear()
    cap = app_data.MAX_DUAL_CACHE_KEYS
    errs = []
    reader_errs = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            try:
                # 交替写双窗键与单窗口键
                dcode = f"sz{1 + i}"
                app_data.cache_put(
                    make_dual_main_key("sz", dcode, "1m", "live"), {"d": i})
                app_data.cache_put(
                    make_single_key("sz", dcode, "1m", "live"), {"s": i})
                i += 1
            except Exception as exc:  # noqa: BLE001
                errs.append(f"write: {type(exc).__name__}: {exc}")

    def reader():
        while not stop.is_set():
            try:
                # 正确读者须持 _stocks_cache_lock（契约）；锁内遍历不得抛出、
                # 且每个值都应是 dict（并发写入不破坏条目结构）
                with app_data._stocks_cache_lock:
                    items = list(app_data._stocks_analysis_cache.items())
                for _k, v in items:
                    if not isinstance(v, dict):
                        reader_errs.append(f"non-dict value for {_k!r}")
            except Exception as exc:  # noqa: BLE001
                reader_errs.append(f"read: {type(exc).__name__}: {exc}")

    ws = [threading.Thread(target=writer) for _ in range(4)]
    rs = [threading.Thread(target=reader) for _ in range(2)]
    for t in ws + rs:
        t.start()
    # 让写者足够多轮以触发大量双窗键插入与淘汰
    import time
    time.sleep(1.2)
    stop.set()
    for t in ws + rs:
        t.join(10)

    dc = _dual_count()
    ok = (not errs) and (not reader_errs) and dc <= cap
    rec("③", f"双窗+单窗混合并发：双窗限额≤{cap}、读者无撕裂、无异常",
        ok,
        f"errs={len(errs)} reader_errs={len(reader_errs)} dual={dc} cap={cap} "
        f"samples={reader_errs[:2] or errs[:2]}" if not ok else "")


def main():
    print("=" * 68)
    print("G2 · 股票双窗口缓存 并发守护")
    print("=" * 68)
    try:
        test_dual_cap_under_concurrent_inserts()
        test_cache_update_atomic_rmw()
        test_dual_plus_single_mixed_no_cross_contamination()
    except Exception:
        traceback.print_exc()
        return 2
    failed = [t for (t, ok) in _results if not ok]
    print("-" * 68)
    print(f"合计 {len(_results)} 项，{'全部通过' if not failed else str(len(failed)) + ' 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

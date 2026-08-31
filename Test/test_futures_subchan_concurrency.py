# -*- coding: utf-8 -*-
"""
G10 · 期货双窗口 SSE 子缠论 —— 并发守护（对应审计矩阵 G10，P3）

矩阵上「期货双窗口 SSE」此前只标注了「set/get 在 _futures_cache_lock 下」，
但缓存里存的是**活着的 CChan 对象图**：SSE 线程每根 K 线 do_init 会就地
清空重建 kl_datas，而 REST 读取方拿到指针后在锁外遍历 bi_list。容器锁只
护「取指针」护不住对象图（审计 P0-2）。本用例直达这一层：

  AppData.set_futures_sub_chan / get_futures_sub_chan  (_futures_cache_lock)
  AppData.futures_sub_chan_lock     (按 key 稳定的对象图锁，写侧取)
  AppData.futures_sub_chan_guarded  (读取方持对象图锁做快照，读侧取)
  AppData._futures_chan_locks       (对象图锁登记表，随缓存条目回收)

守护目标：
  ① 多合约并发 set + 读者 get：不崩溃、无 KeyError，读取方拿到的是某一次
     完整写入（指针一致）
  ② 对象图锁真正生效：SSE 写侧持锁重建（清空→回填 bi_list）期间，读取方
     经 futures_sub_chan_guarded 快照，永不读到「半清空/半回填」的撕裂态
  ③ 对象图锁登记表随缓存条目回收：海量 set→pop 后 _futures_chan_locks
     不无界增长（与 futures_cache_pop 的生命周期对齐）

退出码语义：发现缺陷 → 非 0；全绿 → 0（可直接接入 CI）。
"""
import os
import sys
import threading
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)

import _stub_env  # noqa: E402
_stub_env.install()

from App import AppData  # noqa: E402
from App.AppData import app_data  # noqa: E402

_results = []


def rec(tag, desc, ok, detail=""):
    _results.append((tag, ok))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark}  {tag}  {desc}")
    if detail:
        print(f"           └─ {detail}")


class _FakeChan:
    """最小 CChan 替身：仅暴露 bi_list，模拟「活的对象图」。"""
    def __init__(self, bi):
        self.bi_list = list(bi)


def test_concurrent_set_get_distinct_symbols():
    """① 多合约并发 set + 读者 get：指针一致，无异常。"""
    app_data._futures_analysis_cache.clear()
    app_data._futures_chan_locks.clear()
    n_sym = 24
    syms = [f"rb{t:02d}" for t in range(n_sym)]
    final_vals = {s: f"v_{s}_final" for s in syms}
    errs = []
    barrier = threading.Barrier(n_sym)

    def writer(s):
        try:
            barrier.wait(timeout=30)
            for i in range(20):
                app_data.set_futures_sub_chan(s, "1m", _FakeChan([i, i + 1]))
            app_data.set_futures_sub_chan(s, "1m", _FakeChan([final_vals[s]]))
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{s}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=writer, args=(s,)) for s in syms]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)

    read_errs = []
    for s in syms:
        got = app_data.get_futures_sub_chan(s, "1m")
        if got is None:
            read_errs.append(f"{s}: None")
        elif list(got.bi_list) != [final_vals[s]]:
            read_errs.append(f"{s}: {got.bi_list!r}")
    ok = (not errs) and (not read_errs)
    rec("①", f"{n_sym} 合约并发 set/get：指针一致、无异常", ok,
        f"errs={len(errs)} read_errs={len(read_errs)} "
        f"samples={read_errs[:2] or errs[:2]}" if not ok else "")


def test_object_graph_lock_blocks_torn_read():
    """② 对象图锁生效：写侧持锁重建，读取方快照永不撕裂。"""
    app_data._futures_analysis_cache.clear()
    app_data._futures_chan_locks.clear()
    sym, sub = "AG", "1m"
    FULL = list(range(100))
    obj = _FakeChan(FULL)
    app_data.set_futures_sub_chan(sym, sub, obj)

    reader_errs = []
    stop = threading.Event()

    def writer():
        # 写侧持对象图锁（与读取方同一把），模拟 do_init 清空→回填
        lk = app_data.futures_sub_chan_lock(sym, sub)
        while not stop.is_set():
            with lk:
                obj.bi_list[:] = []                       # 清空 kl_datas
                time.sleep(0.0003)                        # 重建窗口（GIL 让出）
                for v in range(100):                      # 逐根回填
                    obj.bi_list.append(v)
                    if v % 20 == 0:
                        time.sleep(0.00005)

    def reader():
        while not stop.is_set():
            with app_data.futures_sub_chan_guarded(sym, sub) as chan:
                snap = list(chan.bi_list)
                # 合法瞬时态：空（mid-清空，可接受）或满（100 且内容正确）
                # 撕裂态：0 < len < 100（半清空/半回填）—— 绝不应出现
                if 0 < len(snap) < 100:
                    reader_errs.append(f"torn len={len(snap)}")

    wt = threading.Thread(target=writer)
    rt = threading.Thread(target=reader)
    wt.start()
    rt.start()
    time.sleep(1.2)
    stop.set()
    wt.join(10)
    rt.join(10)

    ok = (not reader_errs)
    rec("②", "对象图锁：写侧重建期间读取方快照永不撕裂", ok,
        f"torn_reads={len(reader_errs)} samples={reader_errs[:3]}" if not ok else "")


def test_object_graph_lock_registry_reclaimed():
    """③ 对象图锁登记表随 pop 回收：海量 set→取锁→pop 后不无界增长。

    关键：先调 futures_sub_chan_lock 让登记表（_futures_chan_locks）真正
    长出条目，再 pop。否则只 set/pop 时登记表恒为空，pop 是否回收锁根本无从
    观测（假绿）。真实 pop 会一并摘除 lock 条目 → 登记表回落 0；退化版
    pop 不摘 → 登记表线性累积 → 守卫变红。
    """
    app_data._futures_analysis_cache.clear()
    app_data._futures_chan_locks.clear()
    n = 60
    errs = []
    for i in range(n):
        s = f"cu{i:03d}"
        try:
            app_data.set_futures_sub_chan(s, "1m", _FakeChan([i]))
            # 读取方/写侧在用到该 key 时按需取对象图锁 → 登记表由此增长
            app_data.futures_sub_chan_lock(s, "1m")
            # 立刻弹出（模拟子级别切换 / 连接关闭）
            app_data.pop_futures_sub_chan(s, "1m")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{s}: {type(exc).__name__}: {exc}")

    reg = len(app_data._futures_chan_locks)
    # pop 应一并回收对象图锁：登记表应回落到 0
    ok = (not errs) and reg == 0
    rec("③", f"对象图锁登记表随 pop 回收（set {n} 次后登记 {reg}，期望 0）",
        ok, f"errs={len(errs)} registry={reg}" if not ok else "")


def main():
    print("=" * 68)
    print("G10 · 期货双窗口 SSE 子缠论 并发守护")
    print("=" * 68)
    try:
        test_concurrent_set_get_distinct_symbols()
        test_object_graph_lock_blocks_torn_read()
        test_object_graph_lock_registry_reclaimed()
    except Exception:
        traceback.print_exc()
        return 2
    failed = [t for (t, ok) in _results if not ok]
    print("-" * 68)
    print(f"合计 {len(_results)} 项，{'全部通过' if not failed else str(len(failed)) + ' 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
G9 · 数据刷新中交错 —— 并发守护（对应审计矩阵 G9，P2）

矩阵上「数据刷新」仅标注了 refresh 单飞 CAS 守护，但**刷新进行中**
analyze/search/标注写等读路径的并发交错从未测过。本用例直达刷新写路径：

  _reset_refresh_running  (装饰器，_refresh_state_lock 内 CAS 单飞)
  AppData.replace_names   (刷新完成整体换名缓存，持 _meta_cache_lock 的 RMW)
  AppData.update_pe_ttm   (刷新批量写 PE-TTM，持 _meta_cache_lock 的 RMW)

守护目标：
  ① 刷新单飞 CAS：N 并发触发刷新，仅 1 个真正进入（其余撞守卫早退）
  ② replace_names 原子换表：并发换表 + 并发读者，读者永不读到半截/混合表
  ③ update_pe_ttm 并发写：N 并发写 + 读者快照，无丢失、读者不读到撕裂态

退出码语义：发现缺陷 → 非 0；全绿 → 0（可直接接入 CI）。
"""
import os
import random
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

from App import AppRefresh, AppData  # noqa: E402
from App.AppData import app_data  # noqa: E402

_results = []


def rec(tag, desc, ok, detail=""):
    _results.append((tag, ok))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark}  {tag}  {desc}")
    if detail:
        print(f"           └─ {detail}")


def test_refresh_single_flight_cas():
    """① 刷新单飞 CAS：N 并发触发，仅 1 个真正进入。"""
    AppRefresh._refresh_status["running"] = False
    runs = {"n": 0}
    barrier = threading.Barrier(32)

    def work():
        runs["n"] += 1
        time.sleep(0.1)  # 制造与并发调用的时间重叠窗口

    wrapped = AppRefresh._reset_refresh_running(work)

    def worker():
        barrier.wait()
        wrapped()

    ts = [threading.Thread(target=worker) for _ in range(32)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)

    ok = runs["n"] == 1 and AppRefresh._refresh_status["running"] is False
    rec("①", "刷新单飞 CAS：32 并发仅 1 个真正进入，运行旗复位", ok,
        f"runs={runs['n']} running={AppRefresh._refresh_status['running']}")


def test_replace_names_atomic_swap():
    """② replace_names 原子换表：并发换表 + 并发读者，读者不读到半截/混合表。"""
    app_data._names = {}
    app_data._names_loaded = True
    W = 6
    writer_maps = [
        {f"W{w}_{i}": f"name{w}_{i}" for i in range(50)} for w in range(W)
    ]
    errs = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            try:
                app_data.replace_names(random.choice(writer_maps))
            except Exception as exc:  # noqa: BLE001
                errs.append(f"write: {type(exc).__name__}: {exc}")

    def reader():
        while not stop.is_set():
            try:
                snap = dict(app_data._names)
                # 整表自洽：要么某个写者的全量，要么空(初始)；绝不半截/混合
                ok_snap = (len(snap) == 0) or any(snap == m for m in writer_maps)
                if not ok_snap:
                    errs.append(f"torn snapshot len={len(snap)}")
            except Exception as exc:  # noqa: BLE001
                errs.append(f"read: {type(exc).__name__}: {exc}")

    ws = [threading.Thread(target=writer) for _ in range(4)]
    rs = [threading.Thread(target=reader) for _ in range(2)]
    for t in ws + rs:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in ws + rs:
        t.join(5)

    final = dict(app_data._names)
    final_ok = any(final == m for m in writer_maps)
    ok = (not errs) and final_ok
    rec("②", "replace_names 并发换表：读者永不读到半截/混合表", ok,
        f"errs={len(errs)} final_ok={final_ok} final_len={len(final)} "
        f"samples={errs[:2]}" if not ok else "")


def test_update_pe_ttm_concurrent():
    """③ update_pe_ttm 并发写：N 并发写 + 读者快照，无丢失、读者不读到撕裂态。"""
    app_data._pe = {}
    app_data._pe_loaded = True
    N = 40
    keys = [f"sh{i:06d}" for i in range(N)]
    # 每个写者把整张表设为自己的全量视图（不同值），模拟刷新覆盖式写
    writer_values = [{k: (w * 1000 + i) for i, k in enumerate(keys)} for w in range(4)]
    errs = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            try:
                app_data.update_pe_ttm(random.choice(writer_values))
            except Exception as exc:  # noqa: BLE001
                errs.append(f"write: {type(exc).__name__}: {exc}")

    def reader():
        while not stop.is_set():
            try:
                snap = dict(app_data._pe)
                # 读取时刻必须是某写者全量之一（RMW 原子），或空(初始)
                ok_snap = (len(snap) == 0) or any(
                    snap == v for v in writer_values)
                if not ok_snap:
                    errs.append(f"torn pe len={len(snap)}")
            except Exception as exc:  # noqa: BLE001
                errs.append(f"read: {type(exc).__name__}: {exc}")

    ws = [threading.Thread(target=writer) for _ in range(4)]
    rs = [threading.Thread(target=reader) for _ in range(2)]
    for t in ws + rs:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in ws + rs:
        t.join(5)

    final = dict(app_data._pe)
    final_ok = any(final == v for v in writer_values)
    ok = (not errs) and final_ok
    rec("③", "update_pe_ttm 并发写：读者不读到撕裂态，终态自洽", ok,
        f"errs={len(errs)} final_ok={final_ok} final_len={len(final)} "
        f"samples={errs[:2]}" if not ok else "")


def main():
    print("=" * 68)
    print("G9 · 数据刷新中交错 并发守护")
    print("=" * 68)
    try:
        test_refresh_single_flight_cas()
        test_replace_names_atomic_swap()
        test_update_pe_ttm_concurrent()
    except Exception:
        traceback.print_exc()
        return 2
    failed = [t for (t, ok) in _results if not ok]
    print("-" * 68)
    print(f"合计 {len(_results)} 项，{'全部通过' if not failed else str(len(failed)) + ' 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

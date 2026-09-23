# -*- coding: utf-8 -*-
"""
收割线程「结果缺口补齐」测试
=====================================================================
背景：worker 侧 `AppScanPool._worker_scan_one` 对 `store.put_result` 的异常是
**有意吞掉**的（单票落库失败不该让整批扫描失败，业务结果照常返回）→ 该
future **不以异常收场**。而收割线程原有的兜底只挂在「future 抛异常」这一条
分支上，于是「worker 落库失败」既不走 future 异常分支、也没有结果行：

  · completed 停在 total 之下 → 前端进度悬挂；
  · 该票既不在结果列表、也不在跳过汇总 → **静默少一只**（比数字更严重）。

修复：收割线程拿 futures 登记的全部 seq 与已落库 seq 比对，缺哪票补哪票
（status=error，经 iter_error_rows 进跳过汇总 → 失败可见）。completed 仍由
结果行数 COUNT 派生（单一事实源不变），收敛靠补齐行达成。

本用例**故障注入**驱动，不走真实进程池（真实池下该失败是 ~30% 偶发，
不可当护栏）：直接构造假 future（只被 `_monitor_task` 调 `.result()`）
+ 真实 tempfile 库，逐项钉死上面四条语义：

  ① 缺口补齐：只落库 2/3 票 → 收割后 completed==total，缺的那票有 error 行
  ② 失败可见：补出的行 status=error、文案非空，且 iter_error_rows 能取到
  ③ 不误伤：无缺口时不新增行、已落库的真实结果不被覆盖
  ④ 中止口径：任务已请求中止时缺口补为 aborted（data.aborted=True），
    与 worker 侧中止行同形（汇总时按 data.aborted 排除）
  ⑤ 补写再失败：不抛异常、池引用正常归还（_active_scans 归零）

隔离：SCAN_TASK_DB 指向临时文件，避免污染真实扫描任务库。

运行：python Test/test_scanpool_result_gap.py [--update]
"""
import argparse
import os
import sys
import tempfile
from unittest import mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

# 测试隔离：扫描任务库指向临时文件（在 import AppScanPool 前设置）
_scan_db = os.environ.setdefault("SCAN_TASK_DB",
                                 tempfile.mktemp(suffix="_scan_tasks_gap.db"))

import typing
if not hasattr(typing, "Self"):
    try:
        import typing_extensions
        typing.Self = typing_extensions.Self
    except ImportError:
        pass

from App import AppScanPool as pool_mod
from App.AppScanStore import get_scan_store


GAP_MSG_KEY = "结果未落库"


class _StubFuture:
    """假 future：`_monitor_task` 只对它调 `.result()`。

    不抛异常 = worker 正常返回（落库失败已被 worker 自己吞掉，正是本用例
    要覆盖的那条路径）。
    """

    def __init__(self, raise_exc=None):
        self._exc = raise_exc

    def result(self):
        if self._exc is not None:
            raise self._exc
        return {"code": "stub"}


def _new_task(store, total):
    tid = store.create_task(total=total)
    store.set_status(tid, "running")
    return tid


def test_gap_filled(failures):
    """① 只落库 1/3 票 → 收割后 completed==total 且缺票补为 error 行

    复刻门禁历史偶发红的实测现场（phase7_guards ⑧）：
        [扫描池落库失败] ... seq=1 ... OperationalError: attempt to write a readonly database
        [扫描池落库失败] ... seq=2 ... OperationalError: attempt to write a readonly database
        [FAIL] ⑧ completed=1 != total=3（进度悬挂）
        [FAIL] ⑧ 结果行 seq 不连续: [0] != [0, 1, 2]
    即「只有 seq=0 落库成功、其余两票 worker 写库失败且 future 未抛异常」。
    """
    store = get_scan_store()
    tid = _new_task(store, 3)
    codes = {0: "ZZ0000", 1: "ZZ0001", 2: "ZZ0002"}
    store.put_result(tid, 0, codes[0], "ok", {"code": codes[0]})
    futures = [(_StubFuture(), s, c) for s, c in codes.items()]

    pool_mod._monitor_task(tid, futures)

    task = store.get_task(tid)
    if task.get("completed") != 3:
        failures.append(f"① 缺口未补齐: completed={task.get('completed')} != 3（进度悬挂）")
        print(f"[FAIL] ① 缺口未补齐: completed={task.get('completed')}")
        return
    if task.get("status") != "done":
        failures.append(f"① 终态异常: {task.get('status')}")
        print(f"[FAIL] ① 终态: {task.get('status')}")
        return
    rows = {r["seq"]: r for r in store.get_results(tid, since=0)}
    if sorted(rows) != [0, 1, 2]:
        failures.append(f"① 结果行 seq 不连续: {sorted(rows)} != [0, 1, 2]")
        print(f"[FAIL] ① 结果行 seq: {sorted(rows)}")
        return
    for s in (1, 2):
        if rows[s]["status"] != "error" or rows[s]["code"] != codes[s]:
            failures.append(f"① 缺票 seq={s} 状态/归属异常: {rows[s]}")
            print(f"[FAIL] ① 缺票 seq={s}: {rows[s]}")
            return
    print("[PASS] ① 缺口补齐：completed 3/3、两票缺票补为 error 行、seq 连续")


def test_gap_visible(failures):
    """② 补出的行必须能被 iter_error_rows 取到（否则只是换个地方静默）"""
    store = get_scan_store()
    tid = _new_task(store, 2)
    store.put_result(tid, 0, "600519", "ok", {"code": "600519"})
    futures = [(_StubFuture(), 0, "600519"), (_StubFuture(), 1, "000001")]

    pool_mod._monitor_task(tid, futures)

    errs = store.iter_error_rows(tid)
    hit = [e for e in errs if e["code"] == "000001"]
    if not hit:
        failures.append(f"② 缺口行未进错误行汇总: {[e['code'] for e in errs]}")
        print(f"[FAIL] ② 缺口行未进错误行: {[e['code'] for e in errs]}")
        return
    msg = (hit[0]["data"] or {}).get("error", "")
    if GAP_MSG_KEY not in str(msg):
        failures.append(f"② 缺口行文案异常: {msg!r}")
        print(f"[FAIL] ② 缺口行文案: {msg!r}")
        return
    print(f"[PASS] ② 失败可见：iter_error_rows 含缺口行，文案 {msg!r}")


def test_no_gap_untouched(failures):
    """③ 无缺口时不新增行、已落库的真实结果不被覆盖"""
    store = get_scan_store()
    tid = _new_task(store, 2)
    payload = {"code": "600519", "buy_points": [1, 2, 3], "marker": "真实结果"}
    store.put_result(tid, 0, "600519", "ok", payload)
    store.put_result(tid, 1, "000001", "ok", {"code": "000001"})
    futures = [(_StubFuture(), 0, "600519"), (_StubFuture(), 1, "000001")]

    pool_mod._monitor_task(tid, futures)

    rows = {r["seq"]: r for r in store.get_results(tid, since=0)}
    if sorted(rows) != [0, 1]:
        failures.append(f"③ 无缺口却增删了行: {sorted(rows)}")
        print(f"[FAIL] ③ 行集合: {sorted(rows)}")
        return
    if rows[0]["data"].get("marker") != "真实结果" or rows[0]["status"] != "ok":
        failures.append(f"③ 真实结果被覆盖: {rows[0]}")
        print(f"[FAIL] ③ 真实结果被覆盖: {rows[0]}")
        return
    print("[PASS] ③ 不误伤：无缺口零新增，真实结果原样保留")


def test_aborted_gap_shape(failures):
    """④ 中止态下缺口补为 aborted，与 worker 侧中止行同形（汇总时排除）"""
    store = get_scan_store()
    tid = _new_task(store, 2)
    store.request_abort(tid)
    store.put_result(tid, 0, "600519", "ok", {"code": "600519"})
    futures = [(_StubFuture(), 0, "600519"), (_StubFuture(), 1, "000001")]

    pool_mod._monitor_task(tid, futures)

    rows = {r["seq"]: r for r in store.get_results(tid, since=0)}
    if sorted(rows) != [0, 1]:
        failures.append(f"④ 中止态缺口未补齐: {sorted(rows)}")
        print(f"[FAIL] ④ 缺口未补齐: {sorted(rows)}")
        return
    data = rows[1]["data"] or {}
    if rows[1]["status"] != "aborted" or not data.get("aborted"):
        failures.append(f"④ 中止行形状异常: status={rows[1]['status']} data={data}")
        print(f"[FAIL] ④ 中止行形状: {rows[1]}")
        return
    if store.get_task(tid).get("status") != "aborted":
        failures.append(f"④ 终态非 aborted: {store.get_task(tid).get('status')}")
        print(f"[FAIL] ④ 终态: {store.get_task(tid).get('status')}")
        return
    print("[PASS] ④ 中止口径：缺口补为 aborted 行（data.aborted=True）、终态 aborted")


def test_backfill_failure_safe(failures):
    """⑤ 补写再失败：不抛异常，且池引用仍归还（_active_scans 归零）"""
    store = get_scan_store()
    tid = _new_task(store, 2)
    store.put_result(tid, 0, "600519", "ok", {"code": "600519"})
    futures = [(_StubFuture(), 0, "600519"), (_StubFuture(), 1, "000001")]

    saved = (pool_mod._pool, pool_mod._pool_engine, pool_mod._active_scans)
    pool_mod._pool = None
    pool_mod._active_scans = 1          # 模拟本批已持有一个引用
    try:
        with mock.patch.object(store, "put_result",
                               side_effect=RuntimeError("mock 写库失败")):
            try:
                pool_mod._monitor_task(tid, futures)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"⑤ 补写失败被抛出: {type(exc).__name__}: {exc}")
                print(f"[FAIL] ⑤ 抛出异常: {type(exc).__name__}: {exc}")
                return
        if pool_mod._active_scans != 0:
            failures.append(f"⑤ 池引用未归还: _active_scans={pool_mod._active_scans}")
            print(f"[FAIL] ⑤ _active_scans={pool_mod._active_scans}")
            return
        print("[PASS] ⑤ 补写再失败：不抛异常、池引用归零")
    finally:
        (pool_mod._pool, pool_mod._pool_engine,
         pool_mod._active_scans) = saved


def main():
    ap = argparse.ArgumentParser(description="收割线程结果缺口补齐测试")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    ap.parse_args()

    failures = []
    test_gap_filled(failures)
    test_gap_visible(failures)
    test_no_gap_untouched(failures)
    test_aborted_gap_shape(failures)
    test_backfill_failure_safe(failures)

    # 清理临时 DB
    try:
        for p in (_scan_db, _scan_db + "-wal", _scan_db + "-shm"):
            if os.path.exists(p):
                os.remove(p)
    except OSError:
        pass

    print()
    if failures:
        print(f"===== 收割线程结果缺口补齐测试: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== 收割线程结果缺口补齐测试: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

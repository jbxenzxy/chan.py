# -*- coding: utf-8 -*-
"""
P2-2 补测试缺口 ③ —— 扫描进程池失败收敛测试
=====================================================================
背景：批量扫描仅用 ProcessPool(spawn)，**不提供线程降级**。受限容器
（无 /dev/shm、seccomp 限制等）下装配或首次 submit 会失败。正确的行为是
"失败收敛且可自愈"，而非静默降级到同进程线程池。本用例守护失败路径：

  ① 装配失败收敛：_get_pool() 构造抛异常 → submit_batch_scan 返回
     {"error": ...}；_pool 保持 None、_active_scans 归零、
     _pool_created_count 不虚增；SQLite 任务终态置 error。
  ② 派发失败收敛：首次 submit 抛 BrokenProcessPool → 返回 {"error": ...}，
     坏池被销毁（_pool=None、_active_scans=0），下次可重新装配自愈；
     任务终态置 error。
  ③ 恢复：故障注入移除后正常装配 engine=process_pool，全局池状态无残留。

隔离：SCAN_TASK_DB 指向临时文件，避免污染真实扫描任务库。
（spawn worker 会重导入本模块，故一律 setdefault，见 test_phase7 同款警告）

运行：python Test/test_scanpool_fallback.py [--update]
"""
import argparse
import os
import sys
import tempfile
import time
from unittest import mock
from concurrent.futures.process import BrokenProcessPool

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

# 测试隔离：扫描任务库指向临时文件（在 import AppScanPool 前设置）
_scan_db = os.environ.setdefault("SCAN_TASK_DB", tempfile.mktemp(suffix="_scan_tasks_test.db"))

import typing
if not hasattr(typing, "Self"):
    try:
        import typing_extensions
        typing.Self = typing_extensions.Self
    except ImportError:
        pass

from App import AppScanPool as pool_mod
from App.AppScanStore import get_scan_store


def _reset_pool():
    """清空全局池状态（保存现场，测试后恢复）"""
    pool_mod._pool = None
    pool_mod._pool_engine = None
    pool_mod._pool_created_count = 0
    pool_mod._active_scans = 0


def _submit(stocks=None, **kw):
    """真实 submit_batch_scan（返回 dict 或抛异常）"""
    stocks = stocks or [{"code": "600519", "prefix": "1"},
                        {"code": "000001", "prefix": "0"}]
    return pool_mod.submit_batch_scan(stocks, freq="d", mode="",
                                      recent="1", source="zxg")


def _scan_one_ok(code, **kw):
    """scan_one 打桩：快速返回，避免真实分析（用于派发/恢复场景）"""
    return {"code": code, "name": "测试", "ok": True,
            "meta": {"kline_count": 10, "bi_count": 1, "zs_count": 0}}


def test_assembly_fail_converge(failures):
    """① 装配失败 → 返回 error、池/计数无残留、任务置 error"""
    orig = (pool_mod._pool, pool_mod._pool_engine,
            pool_mod._pool_created_count, pool_mod._active_scans)
    try:
        _reset_pool()

        # 构造立即抛 OSError（受限容器无 /dev/shm）——不在 __init__ 上打桩
        # （易出现 mock 装箱参数错位），而是整体替换类，语义与真实一致。
        class _BoomPool:
            def __init__(self, *a, **k):
                raise OSError("[Errno 38] Function not implemented "
                              "(/dev/shm) （mock 受限环境）")
            def submit(self, *a, **k):
                raise AssertionError("装配失败后不应调用 submit")
            def shutdown(self, *a, **k):
                pass

        with mock.patch("App.AppScanPool.ProcessPoolExecutor", _BoomPool):
            resp = _submit()

        if not isinstance(resp, dict) or "error" not in resp:
            failures.append(f"① 装配失败未收敛为 {{'error'}}: {resp}")
            print(f"[FAIL] ① 装配失败未收敛: {resp}")
            return
        if pool_mod._pool is not None:
            failures.append("① 装配失败后 _pool 非 None")
            print("[FAIL] ① 装配失败后 _pool 非 None")
            return
        if pool_mod._active_scans != 0:
            failures.append(f"① _active_scans 泄漏: {pool_mod._active_scans}")
            print(f"[FAIL] ① _active_scans: {pool_mod._active_scans}")
            return
        if pool_mod._pool_created_count != 0:
            failures.append(f"① _pool_created_count 虚增: {pool_mod._pool_created_count}")
            print(f"[FAIL] ① _pool_created_count: {pool_mod._pool_created_count}")
            return
        print(f"[PASS] ① 装配失败收敛: {resp.get('error')}")
    finally:
        (pool_mod._pool, pool_mod._pool_engine,
         pool_mod._pool_created_count, pool_mod._active_scans) = orig


def test_dispatch_fail_converge(failures):
    """② 派发(首 submit)失败 → 返回 error、坏池销毁、可自愈、任务置 error"""
    orig = (pool_mod._pool, pool_mod._pool_engine,
            pool_mod._pool_created_count, pool_mod._active_scans)
    try:
        _reset_pool()

        # 构造成功但首次 submit 即抛 BrokenProcessPool（受限容器队列创建失败）
        class _BrokenPool:
            def submit(self, *a, **k):
                raise BrokenProcessPool("sem_open: Function not implemented "
                                        "(/dev/shm)")
            def shutdown(self, wait=False, cancel_futures=True):
                pass

        with mock.patch("App.AppScanPool.ProcessPoolExecutor",
                        return_value=_BrokenPool()):
            resp = _submit()

        if not isinstance(resp, dict) or "error" not in resp:
            failures.append(f"② 派发失败未收敛为 {{'error'}}: {resp}")
            print(f"[FAIL] ② 派发失败未收敛: {resp}")
            return
        if pool_mod._pool is not None:
            failures.append("② 派发失败后坏池未被销毁（_pool 非 None）")
            print("[FAIL] ② 坏池未销毁")
            return
        if pool_mod._active_scans != 0:
            failures.append(f"② _active_scans 泄漏: {pool_mod._active_scans}")
            print(f"[FAIL] ② _active_scans: {pool_mod._active_scans}")
            return
        print(f"[PASS] ② 派发失败收敛（坏池已销毁）: {resp.get('error')}")
    finally:
        (pool_mod._pool, pool_mod._pool_engine,
         pool_mod._pool_created_count, pool_mod._active_scans) = orig


def test_restore_recover(failures):
    """③ 恢复正常装配 engine=process_pool，状态无残留"""
    orig = (pool_mod._pool, pool_mod._pool_engine,
            pool_mod._pool_created_count, pool_mod._active_scans)
    try:
        _reset_pool()

        from App.AppOrch import scanner
        orig_scan_one = scanner.scan_one
        scanner.scan_one = _scan_one_ok
        try:
            resp = _submit()
        finally:
            scanner.scan_one = orig_scan_one

        if not isinstance(resp, dict) or "error" in resp:
            failures.append(f"③ 恢复正常后提交失败: {resp}")
            print(f"[FAIL] ③ 恢复正常提交: {resp}")
            return
        if resp.get("engine") != "process_pool":
            failures.append(f"③ 恢复正常引擎 {resp.get('engine')} != process_pool")
            print(f"[FAIL] ③ 恢复正常引擎: {resp}")
            return
        task_id = resp.get("task_id")
        if not task_id or resp.get("total") != 2:
            failures.append(f"③ 提交响应异常: {resp}")
            print(f"[FAIL] ③ 提交响应: {resp}")
            return

        # 轮询直至收敛
        deadline = time.time() + 30
        final = None
        while time.time() < deadline:
            st = pool_mod.get_status(task_id)
            if st and st.get("status") in ("done", "error", "aborted"):
                final = st
                break
            time.sleep(0.2)
        if final is None or final.get("status") != "done":
            failures.append(f"③ 任务未收敛 done: {final}")
            print(f"[FAIL] ③ 任务未收敛: {final}")
            return
        if final.get("completed") != 2:
            failures.append(f"③ completed {final.get('completed')} != 2")
            print(f"[FAIL] ③ completed: {final}")
            return
        if pool_mod._active_scans != 0 or pool_mod._pool is not None:
            failures.append(f"③ 正常结束池状态未归零: "
                            f"active={pool_mod._active_scans} pool={pool_mod._pool}")
            print("[FAIL] ③ 正常结束池状态未归零")
            return
        print("[PASS] ③ 恢复正常装配 + 2/2 收敛 done + 池状态归零")
    finally:
        (pool_mod._pool, pool_mod._pool_engine,
         pool_mod._pool_created_count, pool_mod._active_scans) = orig


def main():
    ap = argparse.ArgumentParser(description="P2-2 ③ 扫描进程池失败收敛测试")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    ap.parse_args()

    failures = []
    test_assembly_fail_converge(failures)
    test_dispatch_fail_converge(failures)
    test_restore_recover(failures)

    # 清理临时 DB
    try:
        for p in (_scan_db, _scan_db + "-wal", _scan_db + "-shm"):
            if os.path.exists(p):
                os.remove(p)
    except OSError:
        pass

    print()
    if failures:
        print(f"===== P2-2 ③ 扫描进程池失败收敛测试: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== P2-2 ③ 扫描进程池失败收敛测试: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
# -*- coding: utf-8 -*-
"""
P2-2 补测试缺口 ③ —— ProcessPool 降级路径测试
=====================================================================
背景：批量扫描优先 ProcessPool(spawn)，受限容器（无 /dev/shm、seccomp
限制等）下创建失败时自动降级 ThreadPoolExecutor 执行同一 worker 函数
（scan_one 内 _scan_lock 自动回归「引擎串行、锁外并发」旧语义）。本用例
守护降级路径不被重构破坏：

  ① _get_pool() 降级：ProcessPoolExecutor 构造抛异常 → 返回
     thread_fallback 引擎，且后续可正常提交任务
  ② submit_batch_scan 降级透传：提交响应带 engine=thread_fallback，
     任务经线程池执行并收敛到 done（scan_one 打桩，避免真实分析）
  ③ 恢复：还原后 _get_pool() 回到 process_pool（或按环境自适应），
     全局池状态不残留

隔离：SCAN_TASK_DB 指向临时文件，避免污染真实扫描任务库。

运行：python Test/test_scanpool_fallback.py [--update]
"""
import argparse
import os
import sys
import tempfile
import time
from unittest import mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

# 测试隔离：扫描任务库指向临时文件（在 import AppScanPool 前设置）
_tmp_db = tempfile.mktemp(suffix="_scan_tasks_test.db")
os.environ["SCAN_TASK_DB"] = _tmp_db

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


def test_get_pool_fallback(failures):
    """① ProcessPool 创建失败 → thread_fallback 降级"""
    orig_pool = pool_mod._pool
    orig_engine = pool_mod._pool_engine
    orig_count = pool_mod._pool_created_count
    try:
        _reset_pool()
        with mock.patch("concurrent.futures.ProcessPoolExecutor.__init__",
                        side_effect=RuntimeError("mock 受限环境无 /dev/shm")):
            pool, engine = pool_mod._get_pool()
        if engine != "thread_fallback":
            failures.append(f"① 降级引擎 {engine} != thread_fallback")
            print(f"[FAIL] ① 降级引擎: {engine}")
            return
        if pool is None:
            failures.append("① 降级后 pool 为 None")
            print("[FAIL] ① 降级后 pool 为 None")
            return
        print("[PASS] ① ProcessPool 失败 → ThreadPool 降级（engine=thread_fallback）")
    finally:
        pool_mod._pool = orig_pool
        pool_mod._pool_engine = orig_engine
        pool_mod._pool_created_count = orig_count


def test_submit_batch_scan_fallback(failures):
    """② 降级路径下提交任务：engine=thread_fallback + 收敛到 done"""
    orig_pool = pool_mod._pool
    orig_engine = pool_mod._pool_engine
    orig_count = pool_mod._pool_created_count
    try:
        _reset_pool()
        # scan_one 打桩：快速返回，避免真实分析
        with mock.patch("concurrent.futures.ProcessPoolExecutor.__init__",
                        side_effect=RuntimeError("mock 受限环境")):
            from App.AppOrch import scanner
            orig_scan_one = scanner.scan_one
            scanner.scan_one = lambda code, **kw: {
                "code": code, "name": "测试", "ok": True,
                "meta": {"kline_count": 10, "bi_count": 1, "zs_count": 0}}

            try:
                resp = pool_mod.submit_batch_scan(
                    [{"code": "600519", "prefix": "1"},
                     {"code": "000001", "prefix": "0"}],
                    freq="d", mode="", recent="1", source="zxg")
            finally:
                scanner.scan_one = orig_scan_one

        if resp.get("engine") != "thread_fallback":
            failures.append(f"② 提交引擎 {resp.get('engine')} != thread_fallback")
            print(f"[FAIL] ② 提交引擎: {resp}")
            return
        task_id = resp.get("task_id")
        if not task_id or resp.get("total") != 2:
            failures.append(f"② 提交响应异常: {resp}")
            print(f"[FAIL] ② 提交响应: {resp}")
            return

        # 轮询直至收敛（线程池执行，应快速完成）
        deadline = time.time() + 15
        final = None
        while time.time() < deadline:
            st = pool_mod.get_status(task_id)
            if st and st.get("status") in ("done", "error", "aborted"):
                final = st
                break
            time.sleep(0.2)
        if final is None:
            failures.append(f"② 任务未收敛: {pool_mod.get_status(task_id)}")
            print(f"[FAIL] ② 任务未收敛: {pool_mod.get_status(task_id)}")
            return
        if final.get("status") != "done":
            failures.append(f"② 终态 {final.get('status')} != done: {final}")
            print(f"[FAIL] ② 终态: {final}")
            return
        if final.get("completed") != 2:
            failures.append(f"② completed {final.get('completed')} != 2")
            print(f"[FAIL] ② completed: {final}")
            return
        print("[PASS] ② 降级提交: engine=thread_fallback + 2/2 收敛到 done")
    finally:
        pool_mod._pool = orig_pool
        pool_mod._pool_engine = orig_engine
        pool_mod._pool_created_count = orig_count


def test_pool_restore(failures):
    """③ 还原后回到正常装配（process_pool 或环境自适应），状态不残留"""
    orig_pool = pool_mod._pool
    orig_engine = pool_mod._pool_engine
    orig_count = pool_mod._pool_created_count
    try:
        _reset_pool()
        pool, engine = pool_mod._get_pool()
        if pool is None:
            failures.append("③ 还原后 pool 为 None")
            print("[FAIL] ③ 还原后 pool 为 None")
            return
        if engine not in ("process_pool", "thread_fallback"):
            failures.append(f"③ 还原后引擎异常: {engine}")
            print(f"[FAIL] ③ 还原后引擎: {engine}")
            return
        print(f"[PASS] ③ 还原后正常装配（engine={engine}）")
    finally:
        pool_mod._pool = orig_pool
        pool_mod._pool_engine = orig_engine
        pool_mod._pool_created_count = orig_count


def main():
    ap = argparse.ArgumentParser(description="P2-2 ③ ProcessPool 降级路径测试")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    args = ap.parse_args()

    failures = []
    test_get_pool_fallback(failures)
    test_submit_batch_scan_fallback(failures)
    test_pool_restore(failures)

    # 清理临时 DB
    try:
        if os.path.exists(_tmp_db):
            os.remove(_tmp_db)
        for suffix in ("-wal", "-shm"):
            p = _tmp_db + suffix
            if os.path.exists(p):
                os.remove(p)
    except OSError:
        pass

    print()
    if failures:
        print(f"===== P2-2 ③ ProcessPool 降级测试: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== P2-2 ③ ProcessPool 降级测试: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

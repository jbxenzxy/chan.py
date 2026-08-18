# -*- coding: utf-8 -*-
"""
Test/smoke_phase7.py —— 阶段 7 批量扫描 ProcessPool 端到端冒烟（手动工具）
=====================================================================
独立于守护用例的手工验证工具（合并自对方交付的 工具/smoke_phase7.py
形式；实现改为 spawn 安全：不依赖 fork 继承 monkeypatch，直接用真实
worker + 虚拟票码，spawn / 线程降级两种引擎下均可运行）。

验证链路：提交 → 轮询 → 终态收敛 → 中止传播 → 不存在任务兜底。
虚拟票码（ZZxxxx）使 scan_one 快速走错误分支落库，免联网、免打桩，
同时覆盖「单票业务错误 → error 行 → completed 收敛」路径。

用法（仓库根目录）：
    python Test/smoke_phase7.py

退出码：0 = 全部通过；1 = 任一断言失败。
（不注册进 run_all —— 端到端冒烟已内置于 test_phase7_guards ⑧，
本工具供交付后手工复核使用。）
"""
import os
import sys
import tempfile
import time

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_TEST_DIR)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# 环境隔离：必须在任何 App 模块导入前注入（与守护用例同模式）。
# ⚠ 一律 setdefault：spawn worker 会重导入本模块（__mp_main__），
# 直接赋值会用各自新生成的临时路径覆盖继承值 → worker 写入孤立库
# （实测踩坑：completed 收敛为 0）。setdefault 保证继承值优先。
_TMP = tempfile.mkdtemp(prefix="chan_p7_smoke_")
os.environ.setdefault("SCAN_TASK_DB", os.path.join(_TMP, "smoke_tasks.db"))
os.environ.setdefault("SCAN_CONCURRENCY", "2")
os.environ.setdefault("TDX_INSTALL_DIR", _TMP)  # 防 Windows 默认路径在 Linux 不可用


def _poll_terminal(orch, task_id, deadline_s=150):
    """轮询至终态，返回终态 status 与最终快照（since=0 全量）。"""
    deadline = time.time() + deadline_s
    since = 0
    while time.time() < deadline:
        st = orch.scanner.get_batch_scan_status(task_id, since=since)
        if st is None:
            raise AssertionError(f"任务不存在: {task_id}")
        for row in st["results"]:
            since = max(since, row["seq"] + 1)
        if st["status"] in ("done", "aborted", "error"):
            return st["status"], orch.scanner.get_batch_scan_status(task_id, since=0)
        time.sleep(0.4)
    raise AssertionError(f"{deadline_s}s 内未到终态")


def main():
    from App import AppOrch as orch

    failures = []
    print("===== 阶段 7 冒烟（真实 ProcessPool → SQLite 回流） =====")

    # ── 1) 提交 → 终态收敛 ──────────────────────────────────────────
    stocks = [{"code": "ZZ%04d" % i, "prefix": "1", "_source": "zxg"}
              for i in range(4)]
    sub = orch.scanner.submit_batch_scan(stocks, freq="d", mode="", recent="1")
    if "error" in sub:
        print(f"[FAIL] 1) 提交失败: {sub}")
        return 1
    tid, total = sub["task_id"], sub["total"]
    print(f"[..] 任务 {tid}: total={total}, engine={sub.get('engine')}, "
          f"workers={sub.get('workers')}")

    status, final = _poll_terminal(orch, tid)
    seqs = sorted(r["seq"] for r in final.get("results", []))
    if final.get("completed") != total:
        failures.append(f"completed={final.get('completed')} != total={total}")
    if seqs != list(range(total)):
        failures.append(f"结果 seq 不连续: {seqs}")
    if status not in ("done", "error"):
        failures.append(f"终态异常: {status}")
    if status == "done":
        print(f"[PASS] 1) 提交→收敛: {total} 票全部回流，completed={final['completed']}")
    else:
        # 虚拟票业务错误 + 无崩溃 future → 应为 done；error 表示基础设施异常
        failures.append(f"终态应为 done（业务错误行不算崩溃），实际 {status}")

    # ── 2) 中止传播 ─────────────────────────────────────────────────
    stocks2 = [{"code": "ZZ9%03d" % i, "prefix": "1", "_source": "zxg"}
               for i in range(6)]
    sub2 = orch.scanner.submit_batch_scan(stocks2, freq="d", mode="", recent="1")
    if "error" in sub2:
        print(f"[FAIL] 2) 提交失败: {sub2}")
        return 1
    tid2 = sub2["task_id"]
    orch.scanner.abort_batch_scan(tid2)
    status2, final2 = _poll_terminal(orch, tid2)
    if status2 != "aborted":
        failures.append(f"中止后终态应为 aborted，实际 {status2}")
    if final2.get("completed") != sub2["total"]:
        failures.append(f"中止后 completed={final2.get('completed')} "
                        f"!= total={sub2['total']}（未收敛）")
    print(f"[PASS] 2) 中止传播: 终态=aborted, completed 收敛 "
          f"{final2.get('completed')}/{sub2['total']}")

    # ── 3) 不存在任务兜底 ───────────────────────────────────────────
    r = orch.scanner.get_batch_scan_status("no-such-task", since=0)
    if r is not None:
        failures.append(f"不存在任务应返回 None: {r}")
    else:
        print("[PASS] 3) 不存在任务: 正确返回 None")

    from App.ScanPool import shutdown as _pool_shutdown
    _pool_shutdown()

    print("-" * 64)
    if failures:
        for f in failures:
            print(f"[FAIL] {f}")
        print("===== 阶段 7 冒烟: 失败 =====")
        return 1
    print("===== 阶段 7 冒烟: 全部通过 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())

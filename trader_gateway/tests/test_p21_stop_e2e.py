# -*- coding: utf-8 -*-
"""
P21 Phase I1 · 真实停止链路端到端测试（P2-3 修复）
====================================================
评审 P2-3：新增的 77 条 p20 都是 in-process 直接调 engine.shutdown_and_lock_all()，
没有一条覆盖「真实跨进程停止链路」。桩测不出 SIGTERM/flag 语义——必须真实
Popen 拉 run_gateway 子进程，再真实触发停止，断言落盘结果。

本测试真实链路：
    run_gateway.py --source replay --speed 0.05 ...
    → 子进程主循环开始消费 K 线（一直活着）
    → 测试写 {out}/.stop_request（AppTrader.stop 的真实停止协议，P1-1/P1-2）
    → 子进程看护线程观测到 flag → 停行情源 → 主循环 finally 里
      engine.shutdown_and_lock_all()（停信号门 + 锁仓 + 持久化）→ 退出 0
    → 断言：
        [1] 子进程退出码 0
        [2] state.db auto_order_enabled == false（关闭态持久化）
        [3] events.jsonl 含 auto_order_off（shutdown 真实执行过）
        [4] gateway.log 含「收到停止请求」（flag 协议确实被观测到）

这 4 条在 Windows 上均会失败（SIGTERM=TerminateProcess），是 P1-1 的回归防线。
跑法：python tests/test_p21_stop_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        for cand in (os.path.join(d, "tg"), os.path.join(d, "trader_gateway", "tg")):
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "__init__.py")):
                return os.path.dirname(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 tg 包。")
    raise SystemExit(2)
sys.path.insert(0, _TG_ROOT)

from tg.store import Store  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name
          + ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def _event_kinds(events_path):
    kinds = []
    try:
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    kinds.append(json.loads(line).get("kind"))
                except (ValueError, OSError):
                    continue
    except OSError:
        pass
    return kinds


def _wait_for(pred, timeout=10.0, interval=0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _gen_demo_replay(out_dir: str) -> None:
    """用工具生成一段回放数据（K 线 + 信号），让子进程主循环持续运行。"""
    from tools.make_demo_data import gen as gen_demo
    gen_demo(out_dir, days=3, seed=7)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="tg_p21_")
    try:
        replay_dir = os.path.join(tmp, "replay")
        out_dir = os.path.join(tmp, "state")
        os.makedirs(out_dir, exist_ok=True)
        _gen_demo_replay(replay_dir)

        run_gw = os.path.join(_TG_ROOT, "run_gateway.py")
        # 真实子进程：broker=dry_run（离线、无 CTP），replay + speed 让主循环
        # 持续活着直到 flag 到达。--no-fresh 保留关闭前状态（与真实 AppTrader 一致）。
        cmd = [sys.executable, run_gw,
               "--source", "replay",
               "--replay-dir", replay_dir,
               "--speed", "0.05",
               "--broker", "dry_run",
               "--out", out_dir,
               "--no-fresh",
               "--quiet"]
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env)
        events_path = os.path.join(out_dir, "events.jsonl")

        try:
            # 等子进程真正跑起来（events.jsonl 出现 start 事件）
            started = _wait_for(
                lambda: any(k == "start" for k in _event_kinds(events_path)),
                timeout=15.0)
            check("[e2e-1] 子进程启动并落盘 start 事件", started, True)

            if started:
                # ── 真实停止触发：写 .stop_request（AppTrader.stop 的唯一跨平台协议）──
                stop_flag = os.path.join(out_dir, ".stop_request")
                with open(stop_flag, "w", encoding="utf-8") as f:
                    f.write("e2e-test ts={}\n".format(time.strftime("%Y-%m-%d %H:%M:%S")))

                # 等子进程因 flag 优雅退出（退出码 0）
                try:
                    rc = proc.wait(timeout=20.0)
                except subprocess.TimeoutExpired:
                    rc = None
                    proc.kill()
                check("[e2e-2] 子进程因 flag 退出（退出码 0）", rc, 0)

                # ── 结果断言：关闭态确实落盘 / 事件确实写出 ──
                store = Store(os.path.join(out_dir, "state.db"))
                enabled = bool(store.get_json("auto_order_enabled", True))
                store.close()
                check("[e2e-3] state.db auto_order_enabled 已持久化为 false",
                      enabled, False)

                kinds = _event_kinds(events_path)
                check("[e2e-4] events.jsonl 含 auto_order_off（shutdown 真实执行）",
                      "auto_order_off" in kinds, True)

                log_text = ""
                try:
                    with open(os.path.join(out_dir, "gateway.log"),
                              "r", encoding="utf-8") as f:
                        log_text = f.read()
                except OSError:
                    pass
                check("[e2e-5] gateway.log 含「收到停止请求（flag）」",
                      "收到停止请求" in log_text, True)
            else:
                # 启动失败：不再继续断言停止链路，直接收尾
                try:
                    proc.kill()
                except OSError:
                    pass
                print("  （子进程未启动，跳过停止链路断言）")
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print("P21 真实停止链路 E2E 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
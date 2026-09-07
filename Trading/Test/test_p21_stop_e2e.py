# -*- coding: utf-8 -*-
"""
P21 Phase I1 · 真实停止链路端到端测试（P2-3 修复）
====================================================
评审 P2-3：新增的 77 条 p20 都是 in-process 直接调 engine.shutdown_and_lock_all()，
没有一条覆盖「真实跨进程停止链路」。桩测不出 SIGTERM/flag 语义——必须真实
Popen 拉 main.py 子进程，再真实触发停止，断言落盘结果。

本测试真实链路：
    main.py --source replay --speed 0.05 ...
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

第二轮（P1-3 CLI 路径守护）：
    第一轮停止后 .stop_request 仍残留磁盘（AppTrader.stop 只写不删，由
    AppTrader.start 在下次启动时删）。若 main.py 自身不自清该 flag，则
    任何"不走 AppTrader.start 的直启/重启路径"（进程崩溃后手动重启、CI 复跑、
    直接 CLI 拉起）一启动就会被残留 flag 看护线程立刻关停（实测存活约 1s 即退）。
    本测试第二轮用「相同 out_dir、纯 CLI 直启」复现该场景，断言：
        [6] 第二轮成功落盘 start 事件（未被残留 flag 误杀）
        [7] 第二轮子进程存活 ≥10s（P1-3 在 CLI 路径闭环，探针侧 wallclock）
        [8] 第二轮可被正常 flag 停止（退出码 0）
        [9] events.jsonl 含第二条 auto_order_off（shutdown 再次执行）
        [10] 第二轮关闭态仍持久化为 false
        [11] 两轮 auto_order_off `at` 间隔 ≥5s（事件日志侧审计，与 [7] 互证）
    配合 main.py 启动时自清 .stop_request（P1-3 防御），第二轮应稳定存活。

[9]/[10]/[11] 在误杀场景下"shutdown 路径仍真实执行过"，所以它们的"通过"
不能区分"误杀"与"正常"。真正拦截 P1-3 回归的核心是 [7]（探针侧 wallclock）
与 [11]（事件日志侧时戳审计）两条从不同维度互证。

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
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d  # Trading 包目录本身（消 tg/ 层后 Trading 即包）
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Infra.Store import Store  # noqa: E402

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


def _event_ats(events_path, kind):
    """返回所有 kind 事件的 at 字段（解析为 datetime）。"""
    ats = []
    try:
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("kind") == kind:
                        ats.append(datetime.fromisoformat(rec.get("at")))
                except (ValueError, OSError):
                    continue
    except OSError:
        pass
    return ats


def _wait_for(pred, timeout=10.0, interval=0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _gen_demo_replay(out_dir: str) -> None:
    """用工具生成一段回放数据（K 线 + 信号），让子进程主循环持续运行。"""
    from Trading.Tool.MakeDemoData import gen as gen_demo
    gen_demo(out_dir, days=3, seed=7)


def _launch_gateway(replay_dir: str, out_dir: str, speed: str = "0.05") -> subprocess.Popen:
    """真实子进程：broker=dry_run（离线、无 CTP），replay + speed 让主循环
    持续活着直到 flag 到达。--no-fresh 保留关闭前状态（与真实 AppTrader 一致）。

    speed 控制回放速率：replay 源每个 bar 休眠 speed 秒、播完即自然退出且不锁仓。
    第二轮用更慢的 speed（如 0.2），让播放时长(≈144*0.2≈29s) 远大于存活断言窗口，
    从而把『残留 flag 误杀』与『replay 自然播完』两种退出区分开。"""
    cmd = [sys.executable, os.path.join(_TG_ROOT, "main.py"),
           "--source", "replay",
           "--replay-dir", replay_dir,
           "--speed", speed,
           "--broker", "dry_run",
           "--out", out_dir,
           "--no-fresh",
           "--quiet"]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)


def _stop_via_flag(proc: subprocess.Popen, out_dir: str, timeout: float = 20.0):
    """写 .stop_request（AppTrader.stop 的真实协议），等优雅退出。返回退出码。"""
    stop_flag = os.path.join(out_dir, ".stop_request")
    with open(stop_flag, "w", encoding="utf-8") as f:
        f.write("e2e-test ts={}\n".format(time.strftime("%Y-%m-%d %H:%M:%S")))
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        return None


def _await_start(events_path: str, timeout: float = 15.0) -> bool:
    return _wait_for(
        lambda: any(k == "start" for k in _event_kinds(events_path)),
        timeout=timeout)


def _force_kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="tg_p21_")
    proc = None
    proc2 = None
    try:
        replay_dir = os.path.join(tmp, "replay")
        out_dir = os.path.join(tmp, "state")
        os.makedirs(out_dir, exist_ok=True)
        _gen_demo_replay(replay_dir)
        events_path = os.path.join(out_dir, "events.jsonl")

        # ───────── 第一轮：启动 → 真实 flag 停止 → 断言关闭链路 ─────────
        proc = _launch_gateway(replay_dir, out_dir)
        try:
            started = _await_start(events_path)
            check("[e2e-1] 子进程启动并落盘 start 事件", started, True)
            if not started:
                print("  （子进程未启动，跳过后续断言）")
                return 1

            rc = _stop_via_flag(proc, out_dir)
            check("[e2e-2] 子进程因 flag 退出（退出码 0）", rc, 0)

            store = Store(os.path.join(out_dir, "state.db"))
            enabled = bool(store.get_json("auto_order_enabled", True))
            store.close()
            check("[e2e-3] state.db auto_order_enabled 已持久化为 false", enabled, False)

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
        finally:
            _force_kill(proc)

        # ───────── 第二轮（P1-3 CLI 路径守护）─────────
        # 第一轮停止后 .stop_request 仍残留磁盘（AppTrader.stop 只写不删）。
        # 若 main.py 不自清，第二轮一启动即被看护线程误杀。此处断言：
        # 第二轮成功启动且存活 ≥10s（未被残留 flag 误杀），随后可被正常 flag 停止。
        proc2 = _launch_gateway(replay_dir, out_dir, speed="0.2")
        try:
            started2 = _await_start(events_path)
            check("[e2e-6] 第二轮直启成功落盘 start 事件（未被残留 flag 误杀）",
                  started2, True)
            if not started2:
                print("  （第二轮未启动：残留 flag 触发误杀，P1-3 CLI 路径未闭环）")
                return 1

            # 存活观察：连续 10s 内子进程不应退出
            survive_start = time.time()
            killed_early = False
            while time.time() - survive_start < 10.0:
                if proc2.poll() is not None:
                    killed_early = True
                    break
                time.sleep(0.3)
            check("[e2e-7] 第二轮子进程存活 ≥10s（未被残留 .stop_request 误杀）",
                  not killed_early, True)

            rc2 = _stop_via_flag(proc2, out_dir)
            check("[e2e-8] 第二轮可被正常 flag 停止（退出码 0）", rc2, 0)

            kinds2 = _event_kinds(events_path)
            check("[e2e-9] 第二轮 events.jsonl 含第二条 auto_order_off（shutdown 再次执行）",
                  kinds2.count("auto_order_off") >= 2, True)

            store2 = Store(os.path.join(out_dir, "state.db"))
            enabled2 = bool(store2.get_json("auto_order_enabled", True))
            store2.close()
            check("[e2e-10] 第二轮关闭态仍持久化为 false", enabled2, False)

            # [e2e-11] 事件日志侧审计：两轮 auto_order_off `at` 间隔 ≥5s
            # 目的：与 [e2e-7]（探针侧 wallclock 实时感知）从不同维度互证
            # "第二轮确实活过了观察窗口"。误杀场景下两 off 间隔约 1~2s，
            # 正常场景下 ≥10s，5s 阈值给两边都留缓冲。
            off_ats = _event_ats(events_path, "auto_order_off")
            if len(off_ats) >= 2:
                gap = (off_ats[1] - off_ats[0]).total_seconds()
                check("[e2e-11] 两轮 auto_order_off `at` 间隔 ≥5s（事件日志侧审计）",
                      gap >= 5.0, True)
                if gap < 5.0:
                    print("  → 实测间隔=%.1fs（误杀场景下通常 <2s）" % gap)
            else:
                check("[e2e-11] 两轮 auto_order_off `at` 间隔 ≥5s（事件日志侧审计）",
                      False, True)
        finally:
            _force_kill(proc2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print("P21 真实停止链路 E2E 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

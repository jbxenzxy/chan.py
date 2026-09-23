# -*- coding: utf-8 -*-
"""
P70 停止分档宽限护栏（启动链未就绪短宽限快杀，盘后关闭不再干等 150s）
========================================================================
背景（2026-09-23 19:19 / 19:20 IM 盘后实测）：r9 修复 flag 竞态合入后
用户复测现象不变——盘后点关闭仍挂 150s 强杀。定性为第二层缺口：
build_runtime（TqApi 构造 + SimNow 登录 + 持仓锚点，tqsdk 同步阻塞）
卡住期间，子进程"聋哑"（看护线程与 signal handler 都在其后才启动），
停止 flag 无人消费，AppTrader.stop() 只能等满 150s 强杀。19:19 会话
登录 59s+ 无 [gw] 启动横幅；19:19:46 的 stop 无收尾日志 + 19:20:37
start 秒进同一把 threading.Lock → 用户等不及重启了后端。

修复（r10 停止分档宽限，用户拍板方案 A）：
  - 子进程 main.py 完成 build_runtime（即将进主循环）时原子写
    {out}/.ready 就绪标志；
  - AppTrader.stop() 分档：就绪 → 完整宽限 _STOP_TIMEOUT(150s，覆盖
    最坏锁仓)；未就绪 → 短宽限 _STARTING_STOP_TIMEOUT(15s)——此窗口
    交易引擎未进主循环、无成交能力，强杀不产生锁仓风险；等待期间
    就绪标志出现（登录 15s 内恰好完成，18:35 会话实测 ~8s）则从该
    时刻起切换为完整宽限；
  - AppTrader.start() 在 Popen 前清上一轮遗留 .ready（.ready 只有
    子进程侧写，此处清无"删掉真请求"竞态）；不清会让 stop() 读到
    上一轮标志误判已就绪，短宽限失效回 150s。

本测试钉死：
  [1] 行为（子进程跑真函数）：_write_ready_flag 原子写——.ready 存在、
      含 pid、无 .tmp 残留、重复调用幂等；
  [2] 行为：未就绪 + 子进程不退 → 短宽限快杀（耗时 ≈ _STARTING_STOP_TIMEOUT，
      绝不用 _STOP_TIMEOUT）；
  [3] 行为：就绪（.ready 预置）→ 完整宽限（耗时 ≈ _STOP_TIMEOUT）；
  [4] 行为：未就绪等待中 .ready 出现 → 宽限切换为完整（总耗时 ≥ 切换点 +
      _STOP_TIMEOUT 量级，而非短宽限就返回）；
  [5] 行为：就绪 + 子进程提前自退 → 立即返回（exited 路径不被分档改造破坏）；
  [6] 源码：main.py 定义 _write_ready_flag 且 run() 在启动横幅后调用；
  [7] 源码：main.py 原子写（tmp + os.replace）；
  [8] 源码：AppTrader 常量 _READY_FLAG / _STARTING_STOP_TIMEOUT；
  [9] 源码：AppTrader.start 的 Popen 前清 .ready（唯一清点不回退）；
  [10] 源码：stop() 分档引用 _STARTING_STOP_TIMEOUT + 就绪切换行；
  [11] 源码：r9 判据不回退（--managed 传参 + 父侧 .stop_request 清点）。

判别力（护栏不恒真）：
  删 main.py 写 .ready            → [1][6] 红（AppTrader 永判未就绪，
                                     就绪子进程也被 15s 快杀 → 盘中锁仓
                                     收尾被打断，比现状更糟的回归）
  删 start 清 .ready             → [9] 红（上一轮残留误判已就绪 → 短宽限
                                     失效 → 启动链卡死场景回 150s 干等）
  stop 恒用 _STOP_TIMEOUT        → [2] 红（启动链卡死场景回 150s 干等）
  stop 恒用短宽限（分档删就绪档）→ [3] 红（就绪子进程锁仓收尾被打断）
  删就绪切换逻辑                  → [4] 红（15s 内登录完成的优雅收尾被误杀）
  删 exited 提前返回              → [5] 红（正常退出也要等满宽限）

跑法：python Trading/Test/test_p70_stop_ready_flex.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or os.path.dirname(_HERE)
_REPO = os.path.dirname(_TG_ROOT)

_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  [PASS] {}".format(name))
    else:
        _FAIL += 1
        print("  [FAIL] {}{}".format(name, ("：{}".format(detail)) if detail else ""))


# ── driver A：main.py _write_ready_flag 真函数（子进程隔离）──
# import Trading.main 会拉起交易引擎模块链，与测试进程隔离。
_DRIVER_READY = r'''
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
from Trading.main import _write_ready_flag
out = tempfile.mkdtemp(prefix="p70r_")
res = {}
_write_ready_flag(out)
ready = os.path.join(out, ".ready")
res["t1_exists"] = os.path.exists(ready)
if res["t1_exists"]:
    with open(ready, encoding="utf-8") as f:
        res["t1_has_pid"] = ("pid=" in f.read())
res["t1_no_tmp"] = not os.path.exists(ready + ".tmp")
# 幂等：重复调用不抛且仍完整
try:
    _write_ready_flag(out)
    with open(ready, encoding="utf-8") as f:
        res["t1_idempotent"] = ("pid=" in f.read())
except Exception as e:  # noqa: BLE001
    res["t1_idempotent"] = "{}: {}".format(type(e).__name__, e)
print(json.dumps(res))
'''

# ── driver B：AppTrader.stop 分档宽限（真 Popen 假子进程）──
# stop() 的强杀目标是本 driver 自己 Popen 的 sleep 子进程，无副作用。
_DRIVER_STOP = r'''
import json, os, sys, tempfile, threading, time, subprocess
sys.path.insert(0, sys.argv[1])
import App.AppTrader as M
from App.AppTrader import AppTrader, _TraderProc
STOP_LONG = float(sys.argv[2])    # 覆盖 _STOP_TIMEOUT
STOP_SHORT = float(sys.argv[3])   # 覆盖 _STARTING_STOP_TIMEOUT
M._STOP_TIMEOUT = STOP_LONG
M._STARTING_STOP_TIMEOUT = STOP_SHORT

out = tempfile.mkdtemp(prefix="p70s_")


def new_handle(sleep_secs, out_dir):
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import time;time.sleep({})".format(sleep_secs)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _TraderProc(proc, out_dir, "t", "dry_run")


def run_stop(prepare=None, sleep_secs=60):
    """独立 AppTrader 实例 + 独立 out_dir 跑一次 stop，返回 (elapsed, result)。"""
    at = AppTrader()
    d = tempfile.mkdtemp(prefix="p70c_")
    if prepare:
        prepare(d)
    h = new_handle(sleep_secs, d)
    at._handle = h
    t0 = time.time()
    r = at.stop()
    return round(time.time() - t0, 2), r


res = {}
# [2] 未就绪 + 子进程不退 → 短宽限快杀（失效时用 STOP_LONG → 耗时 >= LONG-0.5）
el, r = run_stop()
res["t2_elapsed"] = el
# 分界说明：短宽限路径 ≈ SHORT + 轮询粒度 + taskkill ≈ 2.1s（实测）；
# 分档失效路径 ≈ LONG - 0.3 + taskkill ≈ 4.4s+。取 LONG-1.0 双侧余量。
res["t2_fast"] = el < max(1.0, STOP_LONG - 1.0)
res["t2_handle_cleared"] = r.get("running") is False

# [3] 就绪（预置 .ready）→ 完整宽限（失效时用 STOP_SHORT → 耗时 ≈ SHORT）
def prep_ready(d):
    with open(os.path.join(d, ".ready"), "w", encoding="utf-8") as f:
        f.write("pid=1")
el, r = run_stop(prep_ready)
res["t3_elapsed"] = el
res["t3_full"] = el >= STOP_LONG - 1.0

# [4] 未就绪等待中 .ready 出现 → 切换完整宽限（不切换 → 耗时 ≈ SHORT）
def prep_late_ready(d):
    def late():
        time.sleep(0.4)
        with open(os.path.join(d, ".ready"), "w", encoding="utf-8") as f:
            f.write("pid=1")
    threading.Thread(target=late, daemon=True).start()
el, r = run_stop(prep_late_ready)
res["t4_elapsed"] = el
res["t4_flexed"] = el >= STOP_LONG - 1.0

# [5] 就绪 + 子进程提前自退 → 立即返回（exited 路径健在）
def prep_ready2(d):
    with open(os.path.join(d, ".ready"), "w", encoding="utf-8") as f:
        f.write("pid=1")
el, r = run_stop(prep_ready2, sleep_secs=1)
res["t5_elapsed"] = el
res["t5_early_exit"] = el < 6.0 and r.get("running") is False

print(json.dumps(res))
'''


def main() -> int:
    main_py = os.path.join(_TG_ROOT, "main.py")
    apptrader_py = os.path.join(_REPO, "App", "AppTrader.py")
    src_main = io.open(main_py, encoding="utf-8").read()
    src_at = io.open(apptrader_py, encoding="utf-8").read()

    # ── [1] 行为：_write_ready_flag 真函数 ──
    print("── [1] 行为：_write_ready_flag 原子写（子进程隔离）──")
    d = tempfile.mkdtemp(prefix="p70_driver_")
    driver = os.path.join(d, "driver_ready.py")
    with io.open(driver, "w", encoding="utf-8", newline="\n") as f:
        f.write(_DRIVER_READY)
    env = dict(os.environ)
    env.pop("TRADER_GATEWAY_HOME", None)
    proc = subprocess.run(
        [sys.executable, driver, _REPO],
        capture_output=True, text=True, timeout=120, env=env)
    if proc.returncode != 0:
        check("[1] driver 子进程执行", False,
              "rc={} stderr={}".format(proc.returncode,
                                       proc.stderr.strip()[-300:]))
    else:
        import json as _json
        line = next((l for l in proc.stdout.splitlines()
                     if l.startswith("{")), "{}")
        res = _json.loads(line)
        check("[1] _write_ready_flag 原子写 .ready（含 pid、无 .tmp 残留、幂等）",
              res.get("t1_exists") is True and res.get("t1_has_pid") is True
              and res.get("t1_no_tmp") is True
              and res.get("t1_idempotent") is True,
              "结果={}".format(res))

    # ── [2-5] 行为：AppTrader.stop 分档宽限 ──
    print("── [2-5] 行为：stop 分档宽限（真 Popen，短=1s 长=4s 加速）──")
    driver2 = os.path.join(d, "driver_stop.py")
    with io.open(driver2, "w", encoding="utf-8", newline="\n") as f:
        f.write(_DRIVER_STOP)
    proc = subprocess.run(
        [sys.executable, driver2, _REPO, "4.0", "1.0"],
        capture_output=True, text=True, timeout=180, env=env)
    if proc.returncode != 0:
        check("[2-5] driver 子进程执行", False,
              "rc={} stderr={}".format(proc.returncode,
                                       proc.stderr.strip()[-300:]))
    else:
        import json as _json
        line = next((l for l in proc.stdout.splitlines()
                     if l.startswith("{")), "{}")
        res = _json.loads(line)
        check("[2] 未就绪 + 子进程不退 → 短宽限快杀（≈1s，非 4s）",
              res.get("t2_fast") is True,
              "耗时={}s 结果={}".format(res.get("t2_elapsed"), res))
        check("[3] 就绪（.ready 预置）→ 完整宽限（≈4s，非 1s）",
              res.get("t3_full") is True,
              "耗时={}s 结果={}".format(res.get("t3_elapsed"), res))
        check("[4] 未就绪等待中 .ready 出现 → 切换完整宽限",
              res.get("t4_flexed") is True,
              "耗时={}s 结果={}".format(res.get("t4_elapsed"), res))
        check("[5] 就绪 + 子进程提前自退 → 立即返回（exited 路径健在）",
              res.get("t5_early_exit") is True,
              "耗时={}s 结果={}".format(res.get("t5_elapsed"), res))

    # ── [6-11] 源码判据（防回潮）──
    print("── [6-11] 源码判据（防回潮）──")
    check("[6] main.py 定义 _write_ready_flag 且 run() 在启动横幅后调用",
          "def _write_ready_flag(out_dir: str) -> None:" in src_main
          and "    _write_ready_flag(out)" in src_main)
    check("[7] main.py 原子写（tmp + os.replace）",
          "os.replace(tmp, dst)" in src_main
          and 'os.path.join(out_dir, _READY_FLAG + ".tmp")' in src_main)
    check("[8] AppTrader 常量 _READY_FLAG / _STARTING_STOP_TIMEOUT",
          '_READY_FLAG = ".ready"' in src_at
          and "_STARTING_STOP_TIMEOUT = 15.0" in src_at)
    check("[9] AppTrader.start 的 Popen 前清 .ready（唯一清点不回退）",
          "ready_flag = os.path.join(out_dir, _READY_FLAG)" in src_at
          and "已清除上一轮遗留就绪标志" in src_at)
    check("[10] stop() 分档引用 _STARTING_STOP_TIMEOUT + 就绪切换行",
          "wait_secs = _STARTING_STOP_TIMEOUT" in src_at
          and "宽限切换为" in src_at)
    check("[11] r9 判据不回退（--managed 传参 + 父侧 .stop_request 清点）",
          '"--managed"]' in src_at
          and "已清除上一轮遗留停止 flag" in src_at)

    print()
    print("P70：{} passed / {} failed".format(_PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

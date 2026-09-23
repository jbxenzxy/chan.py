# -*- coding: utf-8 -*-
"""
P69 托管模式停止 flag 竞态护栏（盘后关闭 150s 强杀修复）
========================================================
背景（2026-09-23 18:35 / 18:42 IM 实盘两次复现）：盘后开启自动下单，
9~10 秒内点关闭 → Web「关闭中...」挂 150 秒 → 强杀兜底（rc=1，
graceful=False）。

根因：AppTrader.stop() 的唯一可靠停止触发是写 {out}/.stop_request；
子进程 main.run() 启动链里 build_runtime（TqApi 构造 + SimNow 登录 +
持仓查询）在盘后登录慢时可达数十秒，返回后执行的「清残留 .stop_request」
把父进程在此窗口内写入的**真停止请求**当残留删掉；看护线程在清 flag
之后才启动，永远观测不到 → AppTrader 等满 150s 宽限强杀。
（盘中关闭快，是因为关闭都发生在启动数分钟后——早已过清 flag 点。）

修复（--managed 托管模式区分）：
  - AppTrader 启动命令行追加 --managed；子进程托管模式**跳过清残留**
    （父进程在 Popen 前已清，AppTrader.start 持锁内删，先于子进程存在，
    无竞态）；
  - CLI 直启保持清残留（防"崩溃后手动重启/CI 复跑被残留 flag 秒关停"）。

本测试钉死三件事：
  [1] 行为（子进程跑真函数）：managed=True 预置 flag **保留**（不清）；
  [2] 行为：managed=False 预置 flag 被删（CLI 直启防秒退语义不回退）；
  [3] 行为：flag 不存在时两种模式均幂等不抛；
  [4] 源码：main.py argparse 注册 --managed（带理由 help）；
  [5] 源码：AppTrader 启动命令行含 "--managed"；
  [6] 源码：run() 调用点把 managed 传入 _clear_leftover_stop_flag；
  [7] 源码：AppTrader.start 的 Popen 前清残留仍在（唯一清点不回退——
      托管模式跳过子进程清残留后，这里是防残留 flag 的唯一防线）。

判别力（护栏不恒真）：
  子进程恢复无条件清残留        → [1] 红（停止请求再丢 → 150s 强杀回归）
  删掉 CLI 直启清残留          → [2] 红（残留 flag 秒退回归）
  AppTrader 不传 --managed     → [5] 红（托管模式失效 → [1] 语义不生效）
  删掉父进程 Popen 前清点      → [7] 红（托管模式首启被残留 flag 秒关）

跑法：python Trading/Test/test_p69_stop_flag_managed.py
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


# 子进程 driver：真实调用 _clear_leftover_stop_flag 的三种场景，输出 JSON。
# 走子进程是因为 import Trading.main 会拉起交易引擎模块链，与测试进程隔离。
_DRIVER = r'''
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
from Trading.main import _clear_leftover_stop_flag
out = tempfile.mkdtemp(prefix="p69_")
flag = os.path.join(out, ".stop_request")
res = {}
# [1] managed=True：预置 → 必须保留（真停止请求不可删）
with open(flag, "w", encoding="utf-8") as f:
    f.write("requested_by=apptrader")
_clear_leftover_stop_flag(out, True)
res["t1_kept"] = os.path.exists(flag)
# [2] managed=False：预置 → 必须删除（CLI 直启防秒退）
with open(flag, "w", encoding="utf-8") as f:
    f.write("requested_by=apptrader")
_clear_leftover_stop_flag(out, False)
res["t2_removed"] = not os.path.exists(flag)
# [3] 幂等：不存在 → 不抛
try:
    _clear_leftover_stop_flag(out, True)
    _clear_leftover_stop_flag(out, False)
    res["t3_idempotent"] = True
except Exception as e:  # noqa: BLE001
    res["t3_idempotent"] = "{}: {}".format(type(e).__name__, e)
print(json.dumps(res))
'''


def main() -> int:
    main_py = os.path.join(_TG_ROOT, "main.py")
    apptrader_py = os.path.join(_REPO, "App", "AppTrader.py")
    src_main = io.open(main_py, encoding="utf-8").read()
    src_at = io.open(apptrader_py, encoding="utf-8").read()

    print("── [1-3] 行为：_clear_leftover_stop_flag 真函数（子进程隔离）──")
    d = tempfile.mkdtemp(prefix="p69_driver_")
    driver = os.path.join(d, "driver.py")
    with io.open(driver, "w", encoding="utf-8", newline="\n") as f:
        f.write(_DRIVER)
    env = dict(os.environ)
    env.pop("TRADER_GATEWAY_HOME", None)  # driver 只认 argv[1]，防外部干扰
    proc = subprocess.run(
        [sys.executable, driver, _REPO],
        capture_output=True, text=True, timeout=120, env=env)
    if proc.returncode != 0:
        check("[1-3] driver 子进程执行", False,
              "rc={} stderr={}".format(proc.returncode,
                                       proc.stderr.strip()[-300:]))
    else:
        line = next((l for l in proc.stdout.splitlines()
                     if l.startswith("{")), "{}")
        import json as _json
        res = _json.loads(line)
        check("[1] 托管模式（managed=True）预置停止 flag 保留不清",
              res.get("t1_kept") is True,
              "结果={}".format(res))
        check("[2] CLI 直启（managed=False）预置停止 flag 被清（防秒退）",
              res.get("t2_removed") is True, "结果={}".format(res))
        check("[3] flag 不存在时两种模式均幂等不抛",
              res.get("t3_idempotent") is True, "结果={}".format(res))

    print("── [4-7] 源码判据（防回潮）──")
    check("[4a] main.py argparse 注册 --managed",
          'add_argument("--managed", action="store_true"' in src_main)
    check("[4b] --managed help 写明理由（build_runtime 期间真停止请求）",
          "真停止请求" in src_main)
    check("[5] AppTrader 启动命令行含 --managed",
          '"--managed"]' in src_at)
    check("[6] run() 把 managed 传入清残留函数",
          "_clear_leftover_stop_flag(out, bool(getattr(args, \"managed\", False)))"
          in src_main)
    check("[7] AppTrader.start 的 Popen 前清残留仍在（唯一清点不回退）",
          'stop_flag = os.path.join(out_dir, _STOP_REQUEST)' in src_at
          and "已清除上一轮遗留停止 flag" in src_at)

    print()
    print("P69：{} passed / {} failed".format(_PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

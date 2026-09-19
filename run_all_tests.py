# -*- coding: utf-8 -*-
"""
Test/ 与 Trading/Test/ 全量测试统一执行入口
=====================================================================
与 `Test/run_all.py` 的分工（两者不重复、互为补充）：

  · `Test/run_all.py`         —— **门禁入口**：只跑「已注册进 COMPONENTS 的
                                52 个组件」，带冻结基线比对、按依赖排序、
                                单组件 300s 超时，是验收 / CI 用的那一条命令。
  · 本文件 `run_all_tests.py` —— **全量发现入口**：把 `Test/` 与
                                `Trading/Test/` 下**所有** test_*/repro_*/smoke_*
                                脚本扫出来逐个跑一遍，**不管它有没有被注册**。
                                用来回答「有没有测试写了却从来没人跑」。

为什么必须「每个文件一个独立子进程」
--------------------------------------
两目录下的用例一律是**独立脚本风格**（文件末尾 `sys.exit(1 if _FAIL else 0)`，
不是 pytest 用例），且大量用例在文件顶层就改 `sys.path` / `sys.stdout`、
monkeypatch 模块属性、注册 broker 装饰器。同一进程里连跑两个会互相污染，
所以逐文件起新进程执行，**退出码 0 = 通过**。

凭据隔离（默认开启，重要）
--------------------------
`Trading/Broker/SimNow.py` 的 `__init__` 在**凭据齐全时就会真的 `_connect()`
登录 CTP**（SimNow 仿真 / 实盘）。本机环境变量里若已有 `SN_ACCOUNT` /
`TQ_ACCOUNT` / `TQ_PASSWORD` 等，则像 `test_p20_phase_i1.py`、
`test_p60_trade_toasts_reconcile_gap.py` 这类「只想构造 broker 注入 FakeApi」
的用例会意外**发起真实登录**，轻则拖慢几十秒、重则卡住或触碰真实账户。

因此本入口默认在子进程里**清空这些凭据类环境变量**，让需要连接的用例走
SimNow.py 自带的「缺少凭据」快路径（不联网、秒返回）。真要走连接路径
请显式加 `--keep-credentials`（需要网络与有效账号，慎用）。

用法（在仓库根目录执行）
------------------------
    python run_all_tests.py                        # 全量（Test/ + Trading/Test/）
    python run_all_tests.py --only Trading/Test    # 只跑一个目录
    python run_all_tests.py --filter stats         # 文件名含关键字的
    python run_all_tests.py --exclude simnow       # 排除文件名含关键字的
    python run_all_tests.py --list                 # 只列清单，不执行
    python run_all_tests.py --severity core        # 只跑 test_*（跳过 repro_*/smoke_*）
    python run_all_tests.py --timeout 600          # 放宽单条超时（默认 300s）
    python run_all_tests.py --json report.json     # 落盘机器可读报告

退出码：全部通过 0；任一条失败 / 超时非 0。

注意
----
1. 本入口**不比对冻结基线**、**没有 `--update`** —— 避免误刷基线。
   要重新冻结基线请用 `python Test/run_all.py --update`。
2. 部分用例会往仓库内写文件（快照、report.json、临时状态目录）。
   本脚本默认在跑前 / 跑后各取一次 `git status --porcelain`，把**跑测试
   新产生的工作区改动**列出来，便于判断是否需要清理。
   不需要就加 `--no-git-check`。
3. 有若干用例是**诊断脚本**或**环境依赖型**，红/绿不能直接当作回归结论：
   · `repro_n2_bare_property.py`  当前**语法错误**（文件内括号未闭合），必红；
   · `repro_n4_cleanup_race.py`   恒返 0，无拦截力；
   · `smoke_simnow_phase_g.py` / `test_p20_phase_i1.py` /
     `test_p60_trade_toasts_reconcile_gap.py`  需 SimNow/tqsdk 环境；
   · `test_product_fee_table.py`  的 GENERATED 区块断言是**行尾敏感**的
     （`^...$` + `re.M` 在 CRLF 工作区命中 0、LF 工作区命中 1）。
   请结合失败输出判断，别只看红点数。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

# ── 扫描范围与文件名前缀 ────────────────────────────────────────────────
TEST_ROOTS = ("Test", "Trading/Test")
NAME_PREFIXES = ("test_", "repro_", "smoke_")
SKIP_DIRS = {"__pycache__", ".pytest_cache", "fixtures", "snapshots"}

COMPONENT_TIMEOUT_S = 300

# 会被 SimNow.py 当作登录凭据读走的环境变量（默认在子进程里清空）
CREDENTIAL_ENV_KEYS = (
    "SN_ACCOUNT", "SN_PASSWORD",
    "TQ_ACCOUNT", "TQ_PASSWORD",
    "LIVE_ACCOUNT", "LIVE_PASSWORD",
)


def discover(repo_root: str, roots=TEST_ROOTS, severity="all",
             keyword=None, exclude=None):
    """递归发现测试脚本，返回相对仓库根的 posix 路径列表（已排序）。"""
    found = []
    for rel_root in roots:
        abs_root = os.path.join(repo_root, rel_root)
        if not os.path.isdir(abs_root):
            continue
        for dirpath, dirnames, filenames in os.walk(abs_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                if not fn.endswith(".py") or fn.startswith("__"):
                    continue
                if not fn.startswith(NAME_PREFIXES):
                    continue
                if severity == "core" and not fn.startswith("test_"):
                    continue
                if severity == "aux" and fn.startswith("test_"):
                    continue
                hay = (fn + " " + dirpath.replace("\\", "/")).lower()
                if keyword and keyword.lower() not in hay:
                    continue
                if exclude and exclude.lower() in hay:
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), repo_root)
                found.append(rel.replace("\\", "/"))
    return sorted(found)


def build_env(keep_credentials: bool):
    """子进程环境：UTF-8 + 默认清空 SimNow/实盘 凭据（防意外真实登录）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if not keep_credentials:
        cleared = []
        for k in CREDENTIAL_ENV_KEYS:
            if env.pop(k, None) is not None:
                cleared.append(k)
        env["_RUN_ALL_TESTS_CLEARED_CREDS"] = ",".join(cleared)
    return env, ([] if keep_credentials else
                 [k for k in CREDENTIAL_ENV_KEYS if os.environ.get(k)])


def git_status(repo_root: str):
    """返回 git status --porcelain 行集合；非 git 仓库或 git 不可用返回 None。"""
    try:
        proc = subprocess.run(["git", "status", "--porcelain"],
                              cwd=repo_root, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return set(proc.stdout.splitlines())


def run_one(repo_root: str, rel: str, timeout: int, tail: int, env: dict):
    """执行单个测试脚本，返回记录 dict。超时按失败处理（不无限等待）。"""
    cmd = [sys.executable, rel.replace("/", os.sep)]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=repo_root, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
        ok, code, timed_out = proc.returncode == 0, proc.returncode, False
        out = proc.stdout or ""
    except subprocess.TimeoutExpired as e:
        ok, code, timed_out = False, None, True
        got = e.stdout or ""
        if isinstance(got, bytes):
            got = got.decode("utf-8", "replace")
        out = got + "\n[TIMEOUT] %s 超过 %ss 被终止" % (rel, timeout)
    elapsed = round(time.time() - t0, 2)
    lines = (out or "").strip().splitlines()
    return {
        "file": rel,
        "ok": ok,
        "exit_code": code,
        "timed_out": timed_out,
        "elapsed_s": elapsed,
        "tail": lines[-tail:] if tail else [],
    }


def main():
    ap = argparse.ArgumentParser(
        description="Test/ 与 Trading/Test/ 全量测试统一执行入口",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", action="append", metavar="DIR",
                    help="只跑指定目录（可重复；默认 %s）" % ", ".join(TEST_ROOTS))
    ap.add_argument("--filter", metavar="KW",
                    help="只跑路径含该关键字（不区分大小写）")
    ap.add_argument("--exclude", metavar="KW",
                    help="排除路径含该关键字（不区分大小写），优先级高于 --filter")
    ap.add_argument("--severity", choices=["all", "core", "aux"], default="all",
                    help="core=只跑 test_*；aux=只跑 repro_*/smoke_*（默认 all）")
    ap.add_argument("--timeout", type=int, default=COMPONENT_TIMEOUT_S,
                    help="单条超时秒数（默认 %d）" % COMPONENT_TIMEOUT_S)
    ap.add_argument("--tail", type=int, default=15,
                    help="失败时回显输出尾部行数（默认 15，0 表示不回显）")
    ap.add_argument("--list", action="store_true", help="只列清单，不执行")
    ap.add_argument("--json", metavar="PATH", help="落盘机器可读 JSON 报告")
    ap.add_argument("--no-git-check", action="store_true",
                    help="跳过跑前/跑后的 git 工作区变化对比")
    ap.add_argument("--keep-credentials", action="store_true",
                    help="保留 SN_ACCOUNT/TQ_ACCOUNT 等凭据环境变量"
                         "（默认清空，防止用例意外真实登录 SimNow/实盘）")
    args = ap.parse_args()

    repo_root = os.getcwd()
    if not os.path.isdir(os.path.join(repo_root, "Test")):
        print("！请把本脚本放在仓库根目录、并在仓库根目录执行（找不到 ./Test/）")
        return 2

    roots = tuple(args.only) if args.only else TEST_ROOTS
    files = discover(repo_root, roots, args.severity, args.filter, args.exclude)
    if not files:
        print("没有发现符合条件的测试脚本（roots=%s, filter=%s, exclude=%s）"
              % (roots, args.filter, args.exclude))
        return 2

    env, cleared = build_env(args.keep_credentials)

    print("=" * 72)
    print("全量测试执行（发现式，不比对冻结基线）")
    print("仓库根: %s" % repo_root)
    print("解释器: %s" % sys.executable)
    print("扫描目录: %s    命中: %d 个脚本" % (", ".join(roots), len(files)))
    if args.keep_credentials:
        print("凭据: **保留**原始环境变量（--keep-credentials，可能触发真实登录）")
    elif cleared:
        print("凭据: 已在子进程中清空 %s（防意外真实登录）" % ", ".join(cleared))
    else:
        print("凭据: 环境中本就没有 SimNow/实盘 凭据")
    print("时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 72)

    if args.list:
        for i, rel in enumerate(files, 1):
            print("  %3d. %s" % (i, rel))
        print("\n共 %d 个（--list 模式未执行任何脚本）" % len(files))
        return 0

    before = None if args.no_git_check else git_status(repo_root)
    if before is None and not args.no_git_check:
        print("（未取到 git 状态：不是 git 仓库或 git 不可用，跳过工作区变化对比）")

    records = []
    t_start = time.time()
    for i, rel in enumerate(files, 1):
        print("\n──── [%d/%d] %s ────" % (i, len(files), rel))
        rec = run_one(repo_root, rel, args.timeout, args.tail, env)
        records.append(rec)
        if rec["ok"]:
            print("  PASS (%ss)" % rec["elapsed_s"])
        else:
            reason = "TIMEOUT" if rec["timed_out"] else "exit=%s" % rec["exit_code"]
            if rec["tail"]:
                print("\n".join(rec["tail"]))
            print("  FAIL (%s, %ss)" % (reason, rec["elapsed_s"]))

    n_ok = sum(1 for r in records if r["ok"])
    n_to = sum(1 for r in records if r["timed_out"])
    n_all = len(records)

    print("\n" + "=" * 72)
    print("逐条结果")
    print("=" * 72)
    for r in records:
        flag = "PASS" if r["ok"] else ("TIME" if r["timed_out"] else "FAIL")
        print("  [%s] %-56s %7ss" % (flag, r["file"], r["elapsed_s"]))
    print("=" * 72)
    print("结果: %d/%d 通过 | 失败 %d（其中超时 %d）| 总耗时 %.1fs"
          % (n_ok, n_all, n_all - n_ok, n_to, time.time() - t_start))

    if n_ok != n_all:
        print("\n未通过清单：")
        for r in records:
            if not r["ok"]:
                reason = "TIMEOUT" if r["timed_out"] else "exit=%s" % r["exit_code"]
                print("  · %-56s %s" % (r["file"], reason))

    if before is not None:
        after = git_status(repo_root)
        if after is not None:
            new = sorted(after - before)
            if new:
                print("\n跑测试新产生的工作区改动（%d 项，检查后按需清理）：" % len(new))
                for line in new:
                    print("  " + line)
            else:
                print("\n跑测试未新增任何工作区改动。")

    if args.json:
        report = {
            "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version.split()[0],
            "repo_root": repo_root,
            "roots": list(roots),
            "credentials_cleared": cleared,
            "total": n_all,
            "passed": n_ok,
            "failed": n_all - n_ok,
            "timed_out": n_to,
            "components": records,
        }
        try:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
            print("报告: %s" % args.json)
        except OSError as e:
            # 报告写不出来不该吞掉测试结论，只降级为警告
            print("！报告未能写入 %s（%s）；测试结论见上方汇总" % (args.json, e))

    return 0 if n_ok == n_all else 1


if __name__ == "__main__":
    sys.exit(main())

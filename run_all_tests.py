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

健壮性（为什么不用一句 subprocess.run）
---------------------------------------
逐个子进程执行有个经典陷阱：**子进程自己退出后，它派生的孙进程（或未被回收的
后台线程持有的句柄）仍握着 stdout 管道写端**。此时 `subprocess.run(...)` 的
`communicate()` 会一直等管道 EOF，**永远等不到**——表现就是整个全量跑「卡死在某一条
上、一行输出都没有」，Ctrl+C 之前什么都拿不到。`subprocess.run(timeout=...)` 也治不了：
它在超时后会再调一次无超时的 `communicate()`，照样卡住。

所以本入口自己用 `Popen` 管生命周期：
  1. `stdin=DEVNULL`           —— 用例永远不可能卡在等控制台输入上；
  2. 独立读线程抽干 stdout      —— 管道写满不会死锁（可选 `--stream` 实时回显）；
  3. 进程退出后只给管道 **3s** 收尾——仍不 EOF 就判定「有孙进程持有管道」，
     `taskkill /F /T` 清进程树并**继续跑下一条**，绝不无限等待；
  4. Windows 上子进程进独立进程组，控制台 Ctrl+C 不会把用例撕成半截，
     由本脚本统一收尾；
  5. Ctrl+C / 中断：**已完成的结果一条不丢**——照常打印逐条清单与汇总、
     照常落 `--json`，退出码 130。

用法（在仓库根目录执行）
------------------------
    python run_all_tests.py                        # 全量（Test/ + Trading/Test/）
    python run_all_tests.py --only Trading/Test    # 只跑一个目录
    python run_all_tests.py --filter stats         # 文件名含关键字的
    python run_all_tests.py --exclude simnow       # 排除文件名含关键字的
    python run_all_tests.py --list                 # 只列清单，不执行
    python run_all_tests.py --severity core        # 只跑 test_*（跳过 repro_*/smoke_*）
    python run_all_tests.py --timeout 600          # 放宽单条超时（默认 300s）
    python run_all_tests.py --stream               # 实时回显每个用例的输出（排障用）
    python run_all_tests.py --heartbeat 30         # 长用例每 30s 打一次"仍在运行"（0=关）
    python run_all_tests.py --json report.json     # 落盘机器可读报告

退出码：全部通过 0；任一条失败 / 超时非 0；被 Ctrl+C 中断 130。

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
   · `smoke_simnow_phase_g.py`    需要真实 SimNow 连接，凭据被隔离后必红
     （要用真连接请加 `--keep-credentials`）；
   · `test_p20_phase_i1.py` / `test_p60_trade_toasts_reconcile_gap.py`
     **不需要**联网，它们只是「凭据齐全时会真去登录」的那一类 —— 默认隔离下
     是绿的，**不要**为了它们加 `--keep-credentials`；
   · `test_product_fee_table.py`  的 GENERATED 区块断言是**行尾敏感**的
     （`^...$` + `re.M` 在 CRLF 工作区命中 0、LF 工作区命中 1）。
   请结合失败输出判断，别只看红点数。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

# ── 扫描范围与文件名前缀 ────────────────────────────────────────────────
TEST_ROOTS = ("Test", "Trading/Test")
NAME_PREFIXES = ("test_", "repro_", "smoke_")
SKIP_DIRS = {"__pycache__", ".pytest_cache", "fixtures", "snapshots"}

COMPONENT_TIMEOUT_S = 300
PIPE_EOF_GRACE_S = 3.0        # 子进程退出后，等 stdout 管道 EOF 的宽限期
KILL_TREE_TIMEOUT_S = 20.0    # taskkill / killpg 自身的兜底超时
HEARTBEAT_S = 15.0            # 长用例心跳间隔（0 = 关闭）

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


def _spawn_kwargs():
    """让子进程自成一个进程组：控制台 Ctrl+C 不会把用例撕成半截，
    由本脚本统一决定何时终止（否则被中断时用户既拿不到结果、也拿不到收尾）。"""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(pid) -> bool:
    """强杀整个进程树（含孙进程）。孙进程持有 stdout 管道时唯一的解药。"""
    if pid is None:
        return False
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=KILL_TREE_TIMEOUT_S)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True
    except OSError:
        return False


class _Drain(threading.Thread):
    """后台抽干子进程 stdout（防止管道写满死锁）；`echo=True` 时实时回显。"""

    def __init__(self, stream, chunks, echo=False):
        super().__init__(daemon=True)
        self.stream = stream
        self.chunks = chunks
        self.echo = echo

    def run(self):
        try:
            for line in self.stream:
                self.chunks.append(line)
                if self.echo:
                    sys.stdout.write("      | " + line.rstrip("\n") + "\n")
                    sys.stdout.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass


def run_one(repo_root: str, rel: str, timeout: int, tail: int, env: dict,
            stream: bool = False, heartbeat: float = HEARTBEAT_S):
    """执行单个测试脚本，返回记录 dict。

    不会无限等待：子进程退出后管道若仍被孙进程持有，最多等 PIPE_EOF_GRACE_S，
    然后强杀该进程树并继续。超时同样按「强杀进程树 -> 记 FAIL」处理。
    """
    cmd = [sys.executable, rel.replace("/", os.sep)]
    t0 = time.time()
    chunks = []
    proc = subprocess.Popen(cmd, cwd=repo_root, env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            **_spawn_kwargs())
    drain = _Drain(proc.stdout, chunks, echo=stream)
    drain.start()

    timed_out = False
    orphan_pipe = False
    next_beat = t0 + heartbeat if heartbeat else float("inf")
    try:
        deadline = t0 + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                timed_out = True
                break
            step = left if heartbeat <= 0 else min(heartbeat, left)
            try:
                proc.wait(timeout=step)
                break
            except subprocess.TimeoutExpired:
                now = time.time()
                if now >= next_beat and now < deadline:
                    print("    … 运行中 %.0fs（长用例心跳；加 --stream 可实时看输出）"
                          % (now - t0))
                    sys.stdout.flush()
                    next_beat = now + heartbeat
    finally:
        # 无论超时、异常还是正常，只要进程还活着就清进程树；
        # 进程已退出但管道没 EOF 的，也要清「握着管道的孙进程」。
        if proc.poll() is None:
            kill_tree(proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        drain.join(PIPE_EOF_GRACE_S)
        if drain.is_alive():
            orphan_pipe = True
            kill_tree(proc.pid)
            drain.join(2.0)

    elapsed = round(time.time() - t0, 2)
    out = "".join(chunks)
    if timed_out:
        ok, code = False, None
        out += ("\n[TIMEOUT] %s 超过 %ss 被终止（已强杀进程树）" % (rel, timeout))
    else:
        code = proc.returncode
        ok = code == 0
    if orphan_pipe:
        out += ("\n[WARN] 直接子进程已退出，但 stdout 管道未关闭：有孙进程继承了句柄"
                "并在继续运行（已强杀该进程树）。本用例自身的断言结果仍按退出码判定。")
    lines = (out or "").strip().splitlines()
    return {
        "file": rel,
        "ok": ok,
        "exit_code": code,
        "timed_out": timed_out,
        "orphan_pipe": orphan_pipe,
        "elapsed_s": elapsed,
        "tail": lines[-tail:] if tail else [],
    }


def _print_report(records, files, timeout, interrupted, t_start, repo_root,
                  before, args, n_files_total):
    """逐条清单 + 汇总 + git 对比 + JSON 落盘（正常结束与中断都走这里）。"""
    n_ok = sum(1 for r in records if r["ok"])
    n_to = sum(1 for r in records if r["timed_out"])
    n_orphan = sum(1 for r in records if r.get("orphan_pipe"))
    n_all = len(records)

    print("\n" + "=" * 72)
    print("逐条结果（已执行 %d 条%s）"
          % (n_all, "，剩余 %d 条未跑" % (len(files) - n_all)
             if len(files) > n_all else ""))
    print("=" * 72)
    for r in records:
        flag = "PASS" if r["ok"] else ("TIME" if r["timed_out"] else "FAIL")
        extra = "  [管道被孙进程持有]" if r.get("orphan_pipe") else ""
        print("  [%s] %-56s %7ss%s" % (flag, r["file"], r["elapsed_s"], extra))
    print("=" * 72)
    print("结果: %d/%d 通过 | 失败 %d（其中超时 %d）| 总耗时 %.1fs"
          % (n_ok, n_all, n_all - n_ok, n_to, time.time() - t_start))
    if n_orphan:
        print("另有 %d 条出现「子进程退出后管道被孙进程持有」，已强杀进程树" % n_orphan)
    if interrupted is not None:
        print("⚠ 本轮被中断（%s）：已完成的结果如上，未执行 %d 条。"
              % (interrupted, len(files) - n_all))
        print("  建议重跑未执行部分：python %s --filter <关键字>"
              % os.path.basename(__file__))

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
            "timeout_s": timeout,
            "credentials_cleared": args.cleared,
            "interrupted": interrupted,
            "discovered": n_files_total,
            "total": n_all,
            "passed": n_ok,
            "failed": n_all - n_ok,
            "timed_out": n_to,
            "orphan_pipe": n_orphan,
            "components": records,
        }
        try:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
            print("报告: %s" % args.json)
        except OSError as e:
            # 报告写不出来不该吞掉测试结论，只降级为警告
            print("！报告未能写入 %s（%s）；测试结论见上方汇总" % (args.json, e))

    if interrupted is not None:
        return 130
    return 0 if n_ok == n_all else 1


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
    ap.add_argument("--stream", action="store_true",
                    help="实时回显每个用例的输出（定位「卡在哪一步」时用）")
    ap.add_argument("--heartbeat", type=float, default=HEARTBEAT_S,
                    help="长用例每 N 秒打一次「仍在运行」（默认 %g，0=关闭）"
                         % HEARTBEAT_S)
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
    args.cleared = cleared

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
    print("中断安全: Ctrl+C 会终止当前用例并保留已完成结果（不再全部丢弃）")
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
    interrupted = None
    t_start = time.time()
    try:
        for i, rel in enumerate(files, 1):
            print("\n──── [%d/%d] %s ────" % (i, len(files), rel))
            sys.stdout.flush()
            # run_one 内部自己保证：异常 / 超时 / 中断都会先清进程树再抛出
            rec = run_one(repo_root, rel, args.timeout, args.tail, env,
                          stream=args.stream, heartbeat=args.heartbeat)
            records.append(rec)
            if rec["ok"]:
                print("  PASS (%ss)" % rec["elapsed_s"])
                if rec.get("orphan_pipe"):
                    print("  [WARN] 有孙进程持有 stdout 管道，已强杀进程树")
            else:
                reason = "TIMEOUT" if rec["timed_out"] else "exit=%s" % rec["exit_code"]
                if rec["tail"]:
                    print("\n".join(rec["tail"]))
                print("  FAIL (%s, %ss)" % (reason, rec["elapsed_s"]))
            sys.stdout.flush()
    except KeyboardInterrupt:
        interrupted = files[len(records)] if len(records) < len(files) else "<结束前>"
        print("\n\n！！收到 Ctrl+C / 中断信号：已终止当前用例，"
              "下面照常汇总已完成的结果")

    return _print_report(records, files, args.timeout, interrupted, t_start,
                         repo_root, before, args, len(files))


if __name__ == "__main__":
    sys.exit(main())

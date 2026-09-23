# -*- coding: utf-8 -*-
"""门禁「按组件清凭据」机制的自证：常量不许漂移，登记表不许指向空气

背景：`Test/run_all.py` 的 `CREDENTIAL_ENV_KEYS` 与发现式 `run_all_tests.py`
里的同名常量是**同源的两份定义**（两个入口互不 import）。任一份被单独改掉，
现象是「门禁里那两个用例又悄悄去连行情服务器了」—— 慢几倍、还带真实登录，
但**不会有任何测试报红**。本用例把这件事变成可执行断言。

更难发现的第二类失效：`CLEAN_CREDENTIALS_COMPONENTS` 里的名字写错，或该组件
后来被改名 / 移出 COMPONENTS —— 清凭据就整条静默失效（不报错、只是又变慢）。

覆盖：
  ① 两份 CREDENTIAL_ENV_KEYS 逐项相等（值 + 顺序）
  ② CLEAN_CREDENTIALS_COMPONENTS ⊆ COMPONENTS 的组件名集合，且非空
  ③ 登记组件的脚本文件真实存在（名字对、但脚本没了 = 静默失效）
  ④ 行为：登记组件的子进程 env 无凭据键，且其余变量原样保留
  ⑤ 行为：未登记组件不受影响（凭据原样带入）
  ⑥ 行为：清凭据不改调用方那份 env（后续组件不被污染）
"""
import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
GATE_PATH = os.path.join(_HERE, "run_all.py")
DISCOVER_PATH = os.path.join(_REPO_ROOT, "run_all_tests.py")

results = []


def rec(no, title, ok, detail=""):
    results.append((ok, no, title, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {no} {title}")
    if detail:
        print(f"        {detail}")


def _load(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load(GATE_PATH, "_gate_run_all")
entry = _load(DISCOVER_PATH, "_run_all_tests_entry")

CREDS = tuple(gate.CREDENTIAL_ENV_KEYS)
SAMPLE_ENV = dict({k: "stub-" + k for k in CREDS},
                  PATH="/usr/bin", HOME="/root", KEEP_ME="1")


# ── ① 两份常量不许漂移 ────────────────────────────────────────────
same = tuple(entry.CREDENTIAL_ENV_KEYS) == CREDS
rec("①", "两份 CREDENTIAL_ENV_KEYS 一致（门禁 vs 发现式）", same,
    f"门禁={CREDS} 发现式={tuple(entry.CREDENTIAL_ENV_KEYS)}")


# ── ② 登记表 ⊆ 门禁组件名 ────────────────────────────────────────
names = [name for name, _cmd in gate.COMPONENTS]
missing = sorted(gate.CLEAN_CREDENTIALS_COMPONENTS - set(names))
rec("②", "登记表里的组件都在 COMPONENTS 中（且非空）",
    not missing and bool(gate.CLEAN_CREDENTIALS_COMPONENTS),
    f"不在册: {missing or '无'} | 登记数={len(gate.CLEAN_CREDENTIALS_COMPONENTS)}")


# ── ③ 登记组件的脚本文件存在 ─────────────────────────────────────
cmds = dict(gate.COMPONENTS)
absent = []
for name in sorted(gate.CLEAN_CREDENTIALS_COMPONENTS):
    cmd = list(cmds.get(name) or [])
    scripts = [c for c in cmd[1:] if str(c).endswith(".py")]
    if not scripts or not os.path.isfile(os.path.join(_REPO_ROOT, scripts[-1])):
        absent.append(name)
rec("③", "登记组件的脚本文件存在", not absent,
    f"缺文件: {absent or '无'}")


# ── ④⑤⑥ 行为断言：真调 run_component，桩掉 subprocess 看它拿到的 env ──
def probe(name):
    """调 run_component(name,...) 并返回 (调用方 env 事后内容, 子进程 env)。"""
    caller_env = dict(SAMPLE_ENV)
    seen = []

    class _FakeProc:
        returncode = 0
        stdout = "[stub] 本用例不真正执行子进程"

    def _fake_run(cmd, **kw):
        seen.append(kw.get("env"))
        return _FakeProc()

    orig = gate.subprocess
    gate.subprocess = types.SimpleNamespace(
        run=_fake_run, PIPE=orig.PIPE, STDOUT=orig.STDOUT,
        TimeoutExpired=orig.TimeoutExpired)
    try:
        gate.run_component(name, [sys.executable, "stub_target.py"], env=caller_env)
    finally:
        gate.subprocess = orig
    return caller_env, (seen[0] if seen else {})


registered = sorted(gate.CLEAN_CREDENTIALS_COMPONENTS)[0]
caller_after, child = probe(registered)
leaked = [k for k in CREDS if k in child]
kept = all(child.get(k) == SAMPLE_ENV[k] for k in ("PATH", "HOME", "KEEP_ME"))
rec("④", f"登记组件 {registered} 的子进程 env 无凭据键", not leaked,
    f"泄漏: {leaked or '无'}（子进程凭据键数={sum(1 for k in CREDS if k in child)}）")
rec("④b", f"登记组件 {registered} 的非凭据变量原样保留", kept,
    "PATH/HOME/KEEP_ME " + ("全部保留" if kept else "有丢失"))


other = "p26_terminology_guard"
_call_caller2, child2 = probe(other)
intact = all(child2.get(k) == SAMPLE_ENV[k] for k in CREDS)
rec("⑤", f"未登记组件 {other} 不受影响（凭据原样带入）", intact,
    f"带入凭据键数={sum(1 for k in CREDS if k in child2)}/{len(CREDS)}")


mutated = [k for k in CREDS if k not in caller_after]
rec("⑥", "清凭据不就地修改调用方 env（后续组件不被污染）", not mutated,
    f"调用方 env 被改掉的键: {mutated or '无'}")


def main():
    print("=" * 60)
    print("门禁凭据隔离自证（Test/run_all.py 的 CLEAN_CREDENTIALS_COMPONENTS）")
    print("=" * 60)
    failed = [r for r in results if not r[0]]
    print("=" * 60)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

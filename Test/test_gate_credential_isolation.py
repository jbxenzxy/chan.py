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
  ⑦ 发现式 docstring 的组件数 == len(COMPONENTS)（评审 P2-2：已陈旧三次）
  ⑧ SKIP 统计三分口径：passed 不含 skipped、failed = total−passed−skipped、
    退出码只看 failed（复审 8a98cd7 P3-1：旧口径 passed+skipped>total 自相矛盾）
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


# ── ⑦ 发现式 docstring 的组件数 == len(COMPONENTS)（评审 P2-2） ────
#     文档 §5 自己点名的教训「新测试手加 COMPONENTS 行，并同步 run_all_tests.py
#     docstring 的组件数」，2026-09-28/29 漏了两次，本次是第三次（136 vs 137）
#     —— 两次都靠人肉发现，因为**没有任何断言钉它**。这里钉死：数字从
#     COMPONENTS 现算，不在断言里硬编码，所以加/删组件时它会立刻变红。
with open(DISCOVER_PATH, encoding="utf-8") as _f:
    _discover_src = _f.read()
_n_comp = len(gate.COMPONENTS)
rec("⑦", f"发现式 docstring 的组件数 == len(COMPONENTS)（{_n_comp}）",
    f"{_n_comp} 个组件" in _discover_src,
    f"run_all_tests.py 顶部须写「{_n_comp} 个组件」（加/删 COMPONENTS 时同步）")


# ── ⑧ SKIP 统计三分口径（复审 8a98cd7 P3-1） ─────────────────────────
#     背景：run_all.py 引入 [SKIPPED] 哨兵后，main() 的统计公式一度仍是
#     n_ok = sum(... if r["ok"]) —— skip 组件 ok=True，被同时算进 passed
#     与 skipped，report.json 出现 passed+skipped>total 的账目矛盾，且
#     打印「N/N 通过（其中 K 项 SKIPPED…）」自相矛盾。本条钉死三件事：
#     ① run_component 对带哨兵的 exit 0 输出必须判 skipped（行为）；
#     ② 统计公式必须排除 skipped、failed 按 total−passed−skipped（静态）；
#     ③ 退出码只看 failed == 0，缺依赖不阻断 CI（静态）。
def _run_with_stdout(stdout_text):
    """桩 subprocess.run 喂指定 stdout，跑 run_component 拿 rec。"""
    seen = []

    class _FakeProc8:
        returncode = 0
        stdout = stdout_text

    def _fake_run8(cmd, **kw):
        seen.append(1)
        return _FakeProc8()

    orig = gate.subprocess
    gate.subprocess = types.SimpleNamespace(
        run=_fake_run8, PIPE=orig.PIPE, STDOUT=orig.STDOUT,
        TimeoutExpired=orig.TimeoutExpired)
    try:
        return gate.run_component("stub_skip_stat", [sys.executable, "stub.py"],
                                  env=dict(SAMPLE_ENV))
    finally:
        gate.subprocess = orig


_rec_skip = _run_with_stdout("[SKIPPED] fake dependency missing\n")
_rec_pass = _run_with_stdout("[stub] ordinary output, no sentinel\n")
_ok8a = (_rec_skip["ok"] is True and _rec_skip["skipped"] is True
         and "SKIPPED" in _rec_skip["skip_reason"])
_ok8b = (_rec_pass["ok"] is True and not _rec_pass["skipped"])
# 按新公式对这两条 rec 复算：passed 不含 skip 项，三分恒等。
_ok8c = (sum(1 for r in (_rec_skip, _rec_pass) if r["ok"] and not r.get("skipped")) == 1
         and sum(1 for r in (_rec_skip, _rec_pass) if r.get("skipped")) == 1
         and (2 - 1 - 1) == 0)
with open(GATE_PATH, encoding="utf-8") as _f:
    _gate_src8 = _f.read()
_ok8d = ('n_ok = sum(1 for r in records if r["ok"] and not r.get("skipped"))' in _gate_src8
         and "n_failed = n_all - n_ok - n_skipped" in _gate_src8
         and "return n_failed == 0" in _gate_src8)
rec("⑧", "SKIP 统计三分口径：哨兵→skipped / passed 排除 skip / failed 恒等 / 退出码只看 failed",
    _ok8a and _ok8b and _ok8c and _ok8d,
    f"哨兵识别={_ok8a} 普通输出不误判={_ok8b} 三分复算={_ok8c} 静态公式与退出码={_ok8d}")


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

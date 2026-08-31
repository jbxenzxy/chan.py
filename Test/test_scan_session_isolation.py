# -*- coding: utf-8 -*-
"""X3 动态守护：两个扫描会话（两标签页）并发互不串参
=====================================================================
背景：审计 v1.3 §四「功能 × 并发安全矩阵」把 **批量扫描** 的多标签页
安全性押在 X3 会话化（scan_token → _ScanSession）上，但修复当时仅有：
  - 静态/归一化守护 test_scan_pageindex_normalize.py（page_index 归一）
  - 会话 TTL 回归 repro_n3_scan_session_leak.py（弃扫泄漏）
X3 的**核心承诺**——两个会话并发读写各自状态互不串——没有动态测试。
本用例补上（确定性构造 + 并发压测，不依赖调度运气）。

覆盖（对应审计报告 §四 批量扫描行「✅ 多标签页」判定的依据）：
  ① 两会话并发 append/snapshot skip_log —— 记录按 token 归位，零串写
  ② 会话 page_index_code 互不覆盖（X3 病灶本身：任务级状态曾放进程级）
  ③ drop 一方不影响另一方（一页关扫描不误伤另一页）
  ④ legacy 兜底指向最近一次会话（旧客户端兼容语义不回潮）
  ⑤ task_token 绑定/反查/回收与会话生命周期一致（收割回写路由正确）
  ⑥ 并发建会话压测：注册表无丢失、token 唯一

运行：python Test/test_scan_session_isolation.py
"""
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)

# ── 依赖兜底：缺三方库时自包含注入（详见 Test/_stub_env.py 的说明）──
# 仓库内其余并发用例依赖仓库**外部**的 stubs/ 目录，而该目录不在版本库中；
# 纯净环境缺 pandas 时它们会在 import 阶段直接失败，断言一行未跑，守护
# 静默失效。本用例走 _stub_env，环境齐全时零介入，缺失时自动兜底。
import _stub_env                                          # noqa: E402
_stub_env.install()

from App import AppScan  # noqa: E402

results = []


def rec(no, title, ok, detail=""):
    results.append((ok, no, title, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {no} {title}")
    if detail:
        print(f"        {detail}")


def _cleanup():
    """还原模块级状态，避免污染同进程的其他断言"""
    with AppScan._scan_sessions_lock:
        AppScan._scan_sessions.clear()
        AppScan._legacy_scan_session = None
        AppScan._task_scan_token.clear()


# ── ① 两会话并发 append/snapshot：记录按 token 归位 ────────────────
def test_concurrent_skip_log_isolation():
    _cleanup()
    N_THREADS, N_MSG = 4, 200
    tokens = {}

    def worker(i):
        t, _ = AppScan.new_scan_session(f"sh.88110{i}")
        tokens[i] = t
        for n in range(N_MSG):
            AppScan.append_scan_skip(f"owner{i}:{n}", scan_token=t)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)

    errs = []
    total = 0
    for i, tok in tokens.items():
        snap = AppScan.scan_skip_snapshot(scan_token=tok)
        total += len(snap)
        foreign = [m for m in snap if not m.startswith(f"owner{i}:")]
        if foreign:
            errs.append(f"会话{i} 串入 {len(foreign)} 条外来记录: {foreign[:2]}")
        if len(snap) != N_MSG:
            errs.append(f"会话{i} 记录数 {len(snap)} != {N_MSG}（丢失或重复）")
    ok = not errs and total == N_THREADS * N_MSG
    rec("①", "两会话并发 skip_log 零串写", ok,
        f"{N_THREADS} 会话 × {N_MSG} 条并发写入，共 {total} 条，按 token 归位"
        + ("" if ok else f"；问题: {errs[0]}"))
    _cleanup()


# ── ② 会话 page_index_code 互不覆盖（X3 病灶本身）──────────────────
def test_page_index_not_clobbered():
    _cleanup()
    t1, s1 = AppScan.new_scan_session("sh.881101")
    t2, s2 = AppScan.new_scan_session("sz.880325")     # 第二页建会话
    ok = (s1.page_index_code == "sh.881101"
          and s2.page_index_code == "sz.880325"
          and AppScan.get_scan_session(t1).page_index_code == "sh.881101")
    rec("②", "两会话 page_index_code 互不覆盖", ok,
        f"s1={s1.page_index_code} / s2={s2.page_index_code}"
        + ("，各自持有" if ok else "，发生串写！"))
    _cleanup()


# ── ③ drop 一方不影响另一方 ────────────────────────────────────────
def test_drop_one_keeps_other():
    _cleanup()
    t1, s1 = AppScan.new_scan_session("sh.881101")
    t2, s2 = AppScan.new_scan_session("sz.880325")
    AppScan.append_scan_skip("keep-me", scan_token=t1)
    AppScan.drop_scan_session(t2)
    got = AppScan.get_scan_session(t1)
    ok = (got is s1
          and AppScan.scan_skip_snapshot(scan_token=t1) == ["keep-me"]
          and AppScan.get_scan_session(t2) is None)
    rec("③", "drop 一方不影响另一方", ok,
        "关页 B 后页 A 的会话与跳过记录完整保留")
    _cleanup()


# ── ④ legacy 兜底指向最近一次会话 ──────────────────────────────────
def test_legacy_fallback_latest():
    _cleanup()
    t1, _ = AppScan.new_scan_session("sh.881101")
    t2, s2 = AppScan.new_scan_session("sz.880325")
    got = AppScan.get_scan_session(None)               # 旧客户端不传 token
    ok = got is s2
    rec("④", "legacy 兜底 = 最近一次会话", ok,
        f"get_scan_session(None) -> {got.token}（期望 {t2}）")
    _cleanup()


# ── ⑤ task_token 绑定/反查/回收与会话生命周期一致 ──────────────────
def test_task_token_lifecycle():
    _cleanup()
    t1, _ = AppScan.new_scan_session("sh.881101")
    t2, _ = AppScan.new_scan_session("sz.880325")
    AppScan.bind_task_scan_token("task-a", t1)
    AppScan.bind_task_scan_token("task-b", t2)
    ok_bind = (AppScan.scan_token_for_task("task-a") == t1
               and AppScan.scan_token_for_task("task-b") == t2)
    AppScan.drop_task_scan_token("task-a")
    ok_unbind = AppScan.scan_token_for_task("task-a") is None
    AppScan.drop_scan_session(t2)                       # 会话注销
    AppScan.drop_task_scan_token("task-b")
    ok = ok_bind and ok_unbind
    rec("⑤", "task_token 绑定/反查/回收一致", ok,
        f"绑定后反查正确={ok_bind}，解绑后为 None={ok_unbind}")
    _cleanup()


# ── ⑥ 并发建会话压测：注册表无丢失、token 唯一 ─────────────────────
def test_concurrent_session_creation():
    _cleanup()
    N_THREADS, N_PER = 8, 25
    created = []
    lock = threading.Lock()
    errs = []

    def worker():
        try:
            local = []
            for _ in range(N_PER):
                t, s = AppScan.new_scan_session("sh.881199")
                local.append(t)
            with lock:
                created.extend(local)
        except Exception as e:                          # noqa: BLE001
            errs.append(repr(e))

    ts = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    expect = N_THREADS * N_PER
    ok = (not errs and len(created) == expect
          and len(set(created)) == expect
          and len(AppScan._scan_sessions) == expect)
    rec("⑥", "并发建会话无丢失/无重复", ok,
        f"{N_THREADS} 线程 × {N_PER} 次 → {len(created)} 个 token，"
        f"唯一 {len(set(created))}，注册表 {len(AppScan._scan_sessions)}")
    _cleanup()


def main():
    print("=" * 60)
    print("X3 动态守护：扫描会话隔离（审计 v1.3 §四 批量扫描行）")
    print("=" * 60)
    test_concurrent_skip_log_isolation()
    test_page_index_not_clobbered()
    test_drop_one_keeps_other()
    test_legacy_fallback_latest()
    test_task_token_lifecycle()
    test_concurrent_session_creation()

    print()
    bad = [r for r in results if not r[0]]
    if bad:
        print(f"===== 扫描会话隔离守护: 失败 {len(bad)} 项 =====")
        for ok, no, title, _ in bad:
            print(" -", no, title)
        return False
    print(f"===== 扫描会话隔离守护: 全部通过（{len(results)} 项）=====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

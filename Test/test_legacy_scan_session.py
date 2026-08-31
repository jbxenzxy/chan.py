# -*- coding: utf-8 -*-
"""
G8 · 旧客户端参数交叉污染 —— 并发守护（对应审计矩阵 G8，P3）

矩阵上「扫描会话」标注了「每页私有会话 + scan_token 隔离」✅，但**旧客户端
（不传 scan_token）回退到 _legacy_scan_session** 这条兼容路径从未被测过。
风险：旧客户端走 `get_scan_session(None)` → 返回最近一次会话；若 token 化
的新扫描与会话解耦不当，旧客户端会被静默串到别的页参数上（维度 0.2
「模式 A」的回潮）。本用例直达这条回退路径：

  AppScan.new_scan_session          (建私有会话，同时刷新 _legacy_scan_session)
  AppScan.get_scan_session          (无 token → 回退 _legacy_scan_session)
  AppScan.drop_scan_session         (注销；仅当被注销者==legacy 时才清 legacy)
  AppScan.bind_task_scan_token / scan_token_for_task  (task→token 反查)
  _reap_stale_scan_sessions_locked (清扫超时会话时对 legacy 的处置)

守护目标：
  ① 多页并发 new_scan_session：各页凭自己的 token 取到**自己的**会话，
     绝不串到别的页；get_scan_session(None) 始终返回最近一次
  ② drop 一个旧 token 不会误伤 legacy：legacy 仍指向最新会话
  ③ 并发 bind_task_scan_token：各 task 反查到各自会话，互不串
  ④ 清扫器(reaper) 仅当被清的是 legacy 会话时才置空 legacy，否则保留

退出码语义：发现缺陷 → 非 0；全绿 → 0（可直接接入 CI）。
"""
import os
import sys
import threading
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)

import _stub_env  # noqa: E402
_stub_env.install()

from App import AppScan  # noqa: E402

_results = []


def rec(tag, desc, ok, detail=""):
    _results.append((tag, ok))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark}  {tag}  {desc}")
    if detail:
        print(f"           └─ {detail}")


def test_token_isolation_under_concurrent_new():
    """① 多页并发 new_scan_session：各页凭 token 取到自己的会话，不串页。"""
    errs = []
    n = 8
    results = {}
    barrier = threading.Barrier(n)

    def page(tid):
        try:
            token, sess = AppScan.new_scan_session(page_index_code=f"PAGE_{tid}")
            sess.page_index_code = f"PAGE_{tid}"      # 打标，便于回查
            results[tid] = (token, sess)
            barrier.wait(timeout=30)
            # 立即凭自己的 token 回查，必须是自己的会话
            got = AppScan.get_scan_session(token)
            if got is not sess:
                errs.append(f"PAGE_{tid}: 回查到非自身会话")
        except Exception as exc:   # noqa: BLE001
            errs.append(f"PAGE_{tid}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=page, args=(t,)) for t in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)

    # 交叉验证：任意页的 token 不得取到其他页的会话
    for tid, (tok, sess) in results.items():
        other = AppScan.get_scan_session(tok)
        if other is not sess:
            errs.append(f"PAGE_{tid}: 交叉验证失败")
    # legacy 回退：返回最近一次（最后一个创建的）
    legacy = AppScan.get_scan_session(None)
    last_tid = n - 1
    last_sess = results[last_tid][1]
    legacy_ok = (legacy is last_sess)
    ok = (not errs) and legacy_ok
    rec("①", f"{n} 页并发 new：各凭 token 取自身会话、legacy=最新、不串页",
        ok,
        f"errs={len(errs)} legacy_ok={legacy_ok} "
        f"samples={errs[:2]}" if not ok else "")


def test_drop_old_token_keeps_legacy():
    """② drop 旧 token 不误伤 legacy：legacy 仍指向最新会话。"""
    AppScan._scan_sessions.clear()
    AppScan._task_scan_token.clear()
    AppScan._legacy_scan_session = None

    _, old = AppScan.new_scan_session(page_index_code="OLD")
    _, new = AppScan.new_scan_session(page_index_code="NEW")  # legacy→NEW
    old_tok = None
    for t, s in AppScan._scan_sessions.items():
        if s is old:
            old_tok = t
            break

    AppScan.drop_scan_session(old_tok)   # 注销旧会话，应不影响 legacy

    legacy = AppScan.get_scan_session(None)
    old_gone = AppScan.get_scan_session(old_tok) is None
    new_ok = AppScan.get_scan_session(
        [t for t, s in AppScan._scan_sessions.items() if s is new][0]) is new
    ok = (legacy is new) and old_gone and new_ok
    rec("②", "drop 旧 token 后 legacy 仍指向最新会话、旧会话已注销", ok,
        f"legacy_is_new={legacy is new} old_gone={old_gone} new_ok={new_ok}")


def test_task_token_binding_isolation():
    """③ 并发 bind_task_scan_token：各 task 反查到各自会话，互不串。"""
    AppScan._scan_sessions.clear()
    AppScan._task_scan_token.clear()
    AppScan._legacy_scan_session = None

    n = 10
    toks = {}
    for i in range(n):
        tok, _ = AppScan.new_scan_session(page_index_code=f"T{i}")
        toks[i] = tok

    errs = []
    barrier = threading.Barrier(n)

    def binder(i):
        try:
            barrier.wait(timeout=30)
            AppScan.bind_task_scan_token(f"task_{i}", toks[i])
        except Exception as exc:   # noqa: BLE001
            errs.append(f"task_{i}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=binder, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)

    cross = False
    for i in range(n):
        back = AppScan.scan_token_for_task(f"task_{i}")
        if back != toks[i]:
            cross = True
            errs.append(f"task_{i}: 反查串号 {back} != {toks[i]}")
    ok = (not errs) and (not cross)
    rec("③", f"{n} 个 task 并发绑定：各反查自身 token，互不串", ok,
        f"errs={len(errs)} samples={errs[:2]}" if not ok else "")


def test_reaper_only_nulls_legacy_when_reaping_legacy():
    """④ 清扫器仅当被清的是 legacy 会话时才置空 legacy。"""
    AppScan._scan_sessions.clear()
    AppScan._task_scan_token.clear()
    AppScan._legacy_scan_session = None

    _, a = AppScan.new_scan_session(page_index_code="A")   # legacy=A
    _, b = AppScan.new_scan_session(page_index_code="B")   # legacy=B (newer)
    a_tok = [t for t, s in AppScan._scan_sessions.items() if s is a][0]

    # 让 A 超时（start_time 置 0），B 保持新鲜
    a.start_time = 0
    with AppScan._scan_sessions_lock:
        AppScan._reap_stale_scan_sessions_locked()

    # A 被清；legacy 是 B（未超时被保留）→ legacy 仍 == B
    legacy_after = AppScan.get_scan_session(None)
    a_gone = AppScan.get_scan_session(a_tok) is None
    b_alive = (legacy_after is b) and a_gone
    rec("④a", "清扫超时 A（非 legacy）后，legacy 仍指向新鲜 B", b_alive,
        f"legacy_is_B={(legacy_after is b)} a_gone={a_gone}")

    # 再让 B 超时，清扫 → legacy 置空
    b.start_time = 0
    with AppScan._scan_sessions_lock:
        AppScan._reap_stale_scan_sessions_locked()
    legacy_none = AppScan.get_scan_session(None) is None
    rec("④b", "清扫 legacy 会话 B 后，legacy 正确置空", legacy_none,
        f"legacy_none={legacy_none}")


def main():
    print("=" * 68)
    print("G8 · 旧客户端参数交叉污染 并发守护")
    print("=" * 68)
    try:
        test_token_isolation_under_concurrent_new()
        test_drop_old_token_keeps_legacy()
        test_task_token_binding_isolation()
        test_reaper_only_nulls_legacy_when_reaping_legacy()
    except Exception:
        traceback.print_exc()
        return 2
    failed = [t for (t, ok) in _results if not ok]
    print("-" * 68)
    print(f"合计 {len(_results)} 项，{'全部通过' if not failed else str(len(failed)) + ' 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

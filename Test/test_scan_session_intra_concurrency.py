# -*- coding: utf-8 -*-
"""
G7 · 批量扫描单会话内并发 —— 守护（对应审计矩阵 G7，P2）

已有 test_scan_session_isolation 覆盖**跨会话**隔离（N3 懒回收）。
本用例补**同一会话内**的并发正确性，直达扫描注册表与收割回写路径：

  new_scan_session / drop_scan_session   (注册表，持 _scan_sessions_lock)
  bind_task_scan_token / scan_token_for_task / drop_task_scan_token
  append_scan_skip                      (按 scan_token 归属，持会话级 lock)

真实流程：一次扫描 = 一个会话；页面并发 submit 各自产生 task_id 并
bind_task_scan_token(task_id, token)；ProcessPool 收割线程只拿得到 task_id，
经 scan_token_for_task 反查回**发起它的那次扫描**再 append_scan_skip。
会话被 end()/drop 时，映射(task_id→token) 与会话对象解耦（死键由
new_scan_session 惰性回收），收割线程不得崩。

守护目标：
  ① 单会话内 N 并发 submit → task_id→会话 映射完整无丢失
  ② 收割线程 N 并发按 task_id 回写 skip → 恰 N 条、不丢不重
  ③ bind 与 new_scan_session(触发死键清扫) 并发 → 不抛「字典迭代中变更」类异常
  ④ bind+收割+drop 会话 三方并发 → 不崩溃；会话注销后映射仍在、append 安全空转

退出码语义：发现缺陷 → 非 0；全绿 → 0（可直接接入 CI）。
"""
import os
import sys
import threading
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


def test_concurrent_submit_binding():
    """① 单会话内 N 并发 submit：task_id→会话 映射完整无丢失。"""
    token, _ = AppScan.new_scan_session("IDX")
    n = 32
    task_ids = [f"task-{i:03d}" for i in range(n)]
    errs = []

    def binder(i):
        try:
            AppScan.bind_task_scan_token(task_ids[i], token)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"bind{i}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=binder, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)

    bound = [AppScan.scan_token_for_task(t) for t in task_ids]
    all_bound = all(b == token for b in bound)
    ok = (not errs) and all_bound and len(set(task_ids)) == n
    rec("①", "单会话 N 并发 submit：task_id→会话 映射完整无丢失", ok,
        f"errs={len(errs)} all_bound={all_bound} n={n}" if not ok else "")
    AppScan.drop_scan_session(token)
    for t in task_ids:
        AppScan.drop_task_scan_token(t)


def test_concurrent_reaper_writeback():
    """② 收割线程 N 并发按 task_id 回写 skip：恰 N 条、不丢不重。"""
    token, sess = AppScan.new_scan_session("IDX")
    n = 32
    task_ids = [f"task-{i:03d}" for i in range(n)]
    for t in task_ids:
        AppScan.bind_task_scan_token(t, token)
    errs = []

    def reaper(i):
        try:
            tid = task_ids[i]
            tok = AppScan.scan_token_for_task(tid)
            if tok:
                AppScan.append_scan_skip(f"skip-{tid}", tok)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"reap{i}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=reaper, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)

    skips = list(sess.skip_log)
    ok = (not errs) and (len(skips) == n) and (len(set(skips)) == n)
    rec("②", "收割 N 并发回写：恰 N 条 skip、不丢不重", ok,
        f"errs={len(errs)} got={len(skips)} unique={len(set(skips))}" if not ok else "")
    AppScan.drop_scan_session(token)
    for t in task_ids:
        AppScan.drop_task_scan_token(t)


def test_bind_vs_reaper_scan_race():
    """③ bind 与 new_scan_session(触发死键清扫迭代 _task_scan_token) 并发：
    不抛「字典迭代中变更」类异常，且绑定不丢。"""
    token, _ = AppScan.new_scan_session("IDX")
    n = 40
    task_ids = [f"race-{i:03d}" for i in range(n)]
    errs = []
    stop = threading.Event()

    def binder():
        i = 0
        while not stop.is_set():
            try:
                AppScan.bind_task_scan_token(task_ids[i % n], token)
            except Exception as exc:  # noqa: BLE001
                errs.append(f"bind: {type(exc).__name__}: {exc}")
                break
            i += 1

    def session_spawner():
        # 每次 new_scan_session 都触发 _reap_stale_scan_sessions_locked，
        # 其中会遍历 _task_scan_token 回收死键（与会话注销解耦）。
        # 若 bind 不持 _scan_sessions_lock，这里遍历期间并发插入会崩。
        while not stop.is_set():
            try:
                tk, _ = AppScan.new_scan_session("IDX2")
                AppScan.drop_scan_session(tk)
            except Exception as exc:  # noqa: BLE001
                errs.append(f"spawn: {type(exc).__name__}: {exc}")
                break

    bt = threading.Thread(target=binder)
    st = threading.Thread(target=session_spawner)
    bt.start()
    st.start()
    bt.join(1.0)
    st.join(1.0)
    stop.set()
    bt.join(5)
    st.join(5)

    bound = [AppScan.scan_token_for_task(t) for t in task_ids]
    all_bound = all(b == token for b in bound)
    ok = (not errs) and all_bound
    rec("③", "bind×new_scan_session 并发：无迭代崩溃且绑定不丢", ok,
        f"errs={errs[:2]} all_bound={all_bound}" if not ok else "")
    AppScan.drop_scan_session(token)
    for t in task_ids:
        AppScan.drop_task_scan_token(t)


def test_bind_reap_drop_race():
    """④ bind + 收割回写 + drop 会话 三方并发：不崩溃；
    会话注销后映射(task_id→token)仍在、append_scan_skip 安全空转。"""
    token, sess = AppScan.new_scan_session("IDX")
    n = 24
    task_ids = [f"dr-{i:03d}" for i in range(n)]
    for t in task_ids:
        AppScan.bind_task_scan_token(t, token)
    errs = []
    stop = threading.Event()

    def binder():
        i = 0
        while not stop.is_set():
            try:
                AppScan.bind_task_scan_token(task_ids[i % n], token)
            except Exception as exc:  # noqa: BLE001
                errs.append(f"bind: {exc}")
                break
            i += 1

    _ridx = {"i": 0}

    def reaper():
        while not stop.is_set():
            tid = task_ids[_ridx["i"] % n]
            _ridx["i"] += 1
            try:
                tok = AppScan.scan_token_for_task(tid)
                AppScan.append_scan_skip(f"skip-{tid}", tok)  # tok=None→空转
            except Exception as exc:  # noqa: BLE001
                errs.append(f"reap: {exc}")
                break

    def dropper():
        # 中途把会话注销，验证映射解耦 + append 空转不崩
        try:
            AppScan.drop_scan_session(token)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"drop: {exc}")

    bt = threading.Thread(target=binder)
    rt = threading.Thread(target=reaper)
    bt.start()
    rt.start()
    bt.join(0.4)
    rt.join(0.4)
    stop.set()
    dropper()  # 会话注销发生在 bind/收割进行中
    bt.join(5)
    rt.join(5)

    # 会话注销后：映射仍在（解耦），get_scan_session 返回 None
    mapping_alive = all(
        AppScan.scan_token_for_task(t) == token for t in task_ids)
    sess_gone = AppScan.get_scan_session(token) is None
    ok = (not errs) and mapping_alive and sess_gone
    rec("④", "bind+收割+drop 三方并发：不崩；注销后映射仍在、append 空转", ok,
        f"errs={errs[:2]} mapping_alive={mapping_alive} sess_gone={sess_gone}"
        if not ok else "")
    for t in task_ids:
        AppScan.drop_task_scan_token(t)


def main():
    print("=" * 68)
    print("G7 · 批量扫描单会话内并发守护")
    print("=" * 68)
    try:
        test_concurrent_submit_binding()
        test_concurrent_reaper_writeback()
        test_bind_vs_reaper_scan_race()
        test_bind_reap_drop_race()
    except Exception:
        traceback.print_exc()
        return 2
    failed = [t for (t, ok) in _results if not ok]
    print("-" * 68)
    print(f"合计 {len(_results)} 项，{'全部通过' if not failed else str(len(failed)) + ' 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

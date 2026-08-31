# -*- coding: utf-8 -*-
"""user_store 并发 RMW 动态守护：标注并发读写不丢失更新
=====================================================================
背景：审计 v1.3 §四「功能 × 并发安全矩阵」的 **选点/删点/标注** 行，
其多标签页 ✅ 判定依据是 `_user_store_lock`（RLock）串行化全部用户
持久化数据的读-改-写 + 原子落盘。此前该锁只有静态扫描守护
（test_lock_completeness）与遍历安全性动态测试（test_lock_v6_fixes ①，
读者 vs 写者不崩），**「并发写者之间不丢失更新」没有动态用例**——
若有人把 add_annotation 的锁去掉或改细粒度，RMW 立刻丢更新，静默。

本用例用并发压测锁死 RMW 原子性（压测量级足以让无锁版必然翻车）：
  ① N 线程并发 add_annotation 到不同 key —— 终态条数 == N×M（无丢失）
  ② 同 key 并发 add 不同文本 —— 终态 == N×M 条唯一文本（无丢失）
  ③ 同 key 并发 add 相同文本 —— 去重语义成立：终态每文本恰 1 条
  ④ 先并发 add 后并发 delete —— 终态 == 添加数 − 删除数（无复活/误删）

注：get_annotations_for 返回的是内部 list 本体（与 N2 裸 property 同款
契约缺口，下游仅序列化只读使用）。本用例不锁定该泄漏行为，只锁并发
正确性；若后续给读路径补快照，本用例无需改动。

运行：python Test/test_user_store_rmw.py
"""
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

from App.AppData import app_data  # noqa: E402

results = []


def rec(no, title, ok, detail=""):
    results.append((ok, no, title, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {no} {title}")
    if detail:
        print(f"        {detail}")


_CLEANUP_CODES = []


def _mk_code(i):
    """压测专用代码段（sh9xxxxx 不与真实行情/历史数据混淆）"""
    _CLEANUP_CODES.append(f"sh{900000 + i}")
    return _CLEANUP_CODES[-1]


def _purge_test_range():
    """清扫 sh9xxxxx 压测专用区间（含历史残留），并回写落盘。

    本用例直接使用全局 app_data，add_annotation 会原子落盘到真实
    annotations 文件；压测前先清区间保证计数基线干净，压测后清理
    避免污染真实数据。
    """
    junk = [k for k in list(app_data._annotations) if k.startswith("sh9")]
    for k in junk:
        app_data._annotations.pop(k, None)
    if junk:
        app_data.save_annotations()


def _cleanup():
    for c in _CLEANUP_CODES:
        app_data.delete_all_annotations(c, "d")
        app_data._annotations.pop(f"{c}_d", None)
    _CLEANUP_CODES.clear()
    _purge_test_range()


def _run_threads(jobs):
    """jobs: [(fn, args), ...] —— 参数显式随任务携带，防丢参静默崩溃"""
    ts = [threading.Thread(target=fn, args=args) for fn, args in jobs]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return ts


# ── ① 跨 key 并发 add：无丢失更新 ──────────────────────────────────
def test_add_distinct_keys():
    N_THREADS, N_KEYS = 4, 100
    errs = []

    def worker(w):
        try:
            for k in range(N_KEYS):
                code = f"sh{900000 + w * N_KEYS + k}"
                if not app_data.add_annotation(code, "d", "2024-01-02", f"t{w}"):
                    errs.append(f"w{w} add {code} 返回 False")
        except Exception as e:                          # noqa: BLE001
            errs.append(repr(e))

    _run_threads([(worker, (w,)) for w in range(N_THREADS)])
    expected_codes = [f"sh{900000 + i}" for i in range(N_THREADS * N_KEYS)]
    total = sum(len(app_data._annotations.get(f"{c}_d", []))
                for c in expected_codes)
    ok = not errs and total == N_THREADS * N_KEYS
    rec("①", "跨 key 并发 add 无丢失更新", ok,
        f"{N_THREADS} 线程 × {N_KEYS} key → 终态 {total} 条（期望 {N_THREADS * N_KEYS}）"
        + ("" if ok else f"；问题: {errs[0] if errs else '终态条数不符（丢失更新）'}"))


# ── ② 同 key 并发 add 不同文本：无丢失更新 ─────────────────────────
def test_add_same_key_distinct_text():
    code = _mk_code(990001)
    N_THREADS, N_TEXT = 4, 50
    errs = []

    def worker(w):
        try:
            for n in range(N_TEXT):
                app_data.add_annotation(code, "d", "2024-01-03", f"w{w}:{n}")
        except Exception as e:                          # noqa: BLE001
            errs.append(repr(e))

    _run_threads([(worker, (w,)) for w in range(N_THREADS)])
    got = app_data._annotations.get(f"{code}_d", [])
    ok = not errs and len(got) == N_THREADS * N_TEXT
    rec("②", "同 key 并发 add 不同文本无丢失", ok,
        f"{N_THREADS} 线程 × {N_TEXT} 文本 → 终态 {len(got)} 条"
        f"（期望 {N_THREADS * N_TEXT}）" + ("" if ok else f"；问题: {errs[0] if errs else '终态条数不符（丢失更新）'}"))


# ── ③ 同 key 并发 add 相同文本：去重语义成立 ───────────────────────
def test_add_same_key_dedup():
    code = _mk_code(990002)
    N_THREADS, N_TRY = 4, 50
    errs = []

    def worker(w):
        try:
            for _ in range(N_TRY):
                app_data.add_annotation(code, "d", "2024-01-04", "dup")
        except Exception as e:                          # noqa: BLE001
            errs.append(repr(e))

    _run_threads([(worker, (w,)) for w in range(N_THREADS)])
    got = app_data._annotations.get(f"{code}_d", [])
    ok = not errs and len(got) == 1
    rec("③", "同 key 并发 add 相同文本恰去重为 1", ok,
        f"{N_THREADS} 线程 × {N_TRY} 次重复添加 → 终态 {len(got)} 条（期望 1）")


# ── ④ 先并发 add 后并发 delete：无复活/误删 ────────────────────────
def test_concurrent_delete():
    code = _mk_code(990003)
    N_THREADS, N_TEXT = 4, 50
    texts = [f"w{w}:{n}" for w in range(N_THREADS) for n in range(N_TEXT)]

    def adder(w):
        for n in range(N_TEXT):
            app_data.add_annotation(code, "d", "2024-01-05", f"w{w}:{n}")

    _run_threads([(adder, (w,)) for w in range(N_THREADS)])
    before = len(app_data._annotations.get(f"{code}_d", []))

    # 删除一半文本（确定性目标集），并发执行
    to_delete = texts[: len(texts) // 2]
    chunk = (len(to_delete) + N_THREADS - 1) // N_THREADS

    def deleter(w):
        part = to_delete[w * chunk:(w + 1) * chunk]
        for t in part:
            app_data.delete_annotation(code, "d", "2024-01-05", t)

    _run_threads([(deleter, (w,)) for w in range(N_THREADS)])
    remain = {a["text"] for a in app_data._annotations.get(f"{code}_d", [])}
    expect = set(texts[len(to_delete):])
    ok = before == len(texts) and remain == expect
    rec("④", "并发 delete 无复活/误删", ok,
        f"添加 {before}（期望 {len(texts)}）→ 删 {len(to_delete)} → "
        f"剩 {len(remain)}（期望 {len(expect)}）")


def main():
    print("=" * 60)
    print("user_store 并发 RMW 守护（审计 v1.3 §四 选点/删点/标注行）")
    print("=" * 60)
    _purge_test_range()          # 压测前清 sh9* 基线（含历史残留）
    try:
        test_add_distinct_keys()
        test_add_same_key_distinct_text()
        test_add_same_key_dedup()
        test_concurrent_delete()
    finally:
        _cleanup()

    print()
    bad = [r for r in results if not r[0]]
    if bad:
        print(f"===== user_store RMW 守护: 失败 {len(bad)} 项 =====")
        for ok, no, title, _ in bad:
            print(" -", no, title)
        return False
    print(f"===== user_store RMW 守护: 全部通过（{len(results)} 项）=====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

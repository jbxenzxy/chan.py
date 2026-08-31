# -*- coding: utf-8 -*-
"""G1 守护：股票分析（同页快速连点）的**缓存并发**完整性

## 背景（审计矩阵 G1 缺口）

矩阵里「股票查询分析」单页内维度标的是 ✅，但那个 ✅ 是**推导出来的**：
现有 `test_determinism` 是串行重复调用，没有任何一条用例让多个线程
同时打 analyze 的缓存路径。而这条路径恰恰是**读-改-写**最密集的地方：

    cache_get(key) → miss → 构建 CChan（耗时）→ cache_put(key, value)
    cache_get(key) → hit  → 浅拷贝 result → 挂 sub → 返回

用户在页面上快速连点「分析」（换周期、换股票、反复点），落在服务端
就是一批**同键 / 异键混合、miss 与 hit 交错**的并发请求。

## 本用例守护的四件事

  ① 同键并发首次分析：结果一致且完整（不得出现 None / 半成品）
  ② 命中路径并发读：命中率 100%，不抛异常
  ③ LRU 总上限在并发下不被突破（MAX_CACHE_SIZE）
  ④ 双窗键独立限额在并发下不被突破（MAX_DUAL_CACHE_KEYS）

## 与既有用例的分工

v6 的 `test_cache_update`（并发读-改-写字段不丢）与 `test_reader_no_mutation`
（命中返回副本）已覆盖 P0-3 的**机制层**；本用例补的是**调用模式层**——
真实 analyze 的 miss/hit 混合编排与容量约束，两者不重复。

运行：把本文件放在仓库 Test/ 下，在仓库根目录执行
    python Test/test_analyze_cache_concurrency.py
退出码：0 = 全部通过；1 = 有守护失败
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # Test/ 同目录
import _stub_env                                                  # noqa: E402
_stub_env.install()      # 缺三方依赖时兜底；环境齐全则零介入

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from App.AppData import (                             # noqa: E402
    app_data, make_single_key, make_dual_main_key,
)

# ── 结果记录 ──────────────────────────────────────────────────────────
_RESULTS = []


def rec(tag, name, ok, detail=""):
    _RESULTS.append((tag, name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tag} {name}")
    if detail:
        print(f"         {detail}")


def _fresh_cache():
    """清空股票分析缓存（本进程独占，不影响真实服务）。"""
    app_data.stocks_cache_clear()


def _compute(key):
    """模拟一次真实的「冷启动分析」：耗时、且结果是**多字段**的。

    故意分两步构造（先建壳再填字段），这样若 put 发生在填充中途，
    其他线程就会读到**半成品**——正是要守护的事故形态。
    """
    value = {"result": {"code": key[-1] if isinstance(key, tuple) else str(key),
                        "bars": 0, "bi": []}}
    value["result"]["bars"] = 500
    value["result"]["bi"] = [1, 2, 3]
    value["chan"] = object()
    return value


# ── ① 同键并发首次分析：结果一致且完整 ────────────────────────────────
def test_concurrent_first_click_consistent():
    _fresh_cache()
    key = make_single_key("sh", "600519", "d", "live")

    n_threads = 16
    barrier = threading.Barrier(n_threads)
    got = []
    errors = []
    lock = threading.Lock()

    def click(tid):
        """复刻 analyze 的查-算-存编排（含命中路径的浅拷贝发布）。"""
        try:
            barrier.wait(timeout=15)
            for _ in range(12):
                cached = app_data.cache_get(key)
                if cached is not None and "result" in cached:
                    result = dict(cached["result"])       # 命中：先拷贝再发布
                else:
                    result = _compute(key)["result"]      # 未命中：算
                    app_data.cache_put(key, {"result": result, "chan": "C"})
                with lock:
                    got.append((tid, result))
        except Exception as exc:                          # noqa: BLE001
            with lock:
                errors.append(f"T{tid}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=click, args=(t,)) for t in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)

    rec("①", f"同键并发连点无异常（{n_threads} 线程）", not errors,
        f"错误={errors[:3] if errors else '无'}")

    bad = [(tid, r) for tid, r in got
           if not isinstance(r, dict) or r.get("bars") != 500 or r.get("bi") != [1, 2, 3]]
    rec("①", "每次点击都拿到完整结果（无 None / 无半成品）", not bad,
        f"样本={len(got)} 条，异常={len(bad)} 条"
        + (f"，例：{bad[:2]}" if bad else "（期望 0）"))

    distinct = {repr(sorted(r.items())) for _t, r in got if isinstance(r, dict)}
    rec("①", "同一键的所有返回结果彼此一致", len(distinct) == 1,
        f"结果去重后={len(distinct)} 种（期望 1：同键同结果）")

    final = app_data.cache_get(key)
    rec("①", "缓存本体最终为完整条目", bool(final) and final.get("result", {}).get("bars") == 500,
        f"最终缓存字段={sorted((final or {}).keys())}（期望含 result/chan）")


# ── ② 命中路径并发读：命中率 100% ─────────────────────────────────────
def test_concurrent_hits_never_miss():
    _fresh_cache()
    keys = [make_single_key("sh", f"{600000 + i}", "d", "live") for i in range(12)]
    for k in keys:
        app_data.cache_put(k, _compute(k))

    n_threads = 12
    barrier = threading.Barrier(n_threads)
    miss = {"n": 0}
    errors = []
    lock = threading.Lock()

    def reader(tid):
        try:
            barrier.wait(timeout=15)
            for i in range(200):
                v = app_data.cache_get(keys[(tid + i) % len(keys)])
                if v is None or "result" not in v:
                    with lock:
                        miss["n"] += 1
        except Exception as exc:                          # noqa: BLE001
            with lock:
                errors.append(f"T{tid}: {type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=reader, args=(t,)) for t in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)

    rec("②", "并发命中读取无异常", not errors,
        f"错误={errors[:3] if errors else '无'}（含「遍历中字典被改」类）")
    rec("②", f"已缓存键在并发读下命中率 100%（{n_threads}×200 次）", miss["n"] == 0,
        f"未命中={miss['n']} 次（期望 0：LRU 读不应把条目读丢）")
    rec("②", "并发读后缓存条目数不变", len(app_data._stocks_analysis_cache) == len(keys),
        f"条目数={len(app_data._stocks_analysis_cache)}（期望 {len(keys)}）")


# ── ③ LRU 总上限在并发下不被突破 ──────────────────────────────────────
def test_lru_cap_under_concurrency():
    _fresh_cache()
    cap = app_data.MAX_CACHE_SIZE
    n_threads, per_thread = 8, 25          # 共 200 键，远超 cap=50
    stop = threading.Event()
    peak = {"n": 0}

    def monitor():
        while not stop.is_set():
            n = len(app_data._stocks_analysis_cache)   # 故意无锁读，观察真实可见态
            if n > peak["n"]:
                peak["n"] = n

    def writer(tid):
        for i in range(per_thread):
            code = f"{600000 + tid * per_thread + i}"
            app_data.cache_put(make_single_key("sh", code, "d", "live"),
                               _compute(code))

    mt = threading.Thread(target=monitor, daemon=True)
    mt.start()
    ts = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(180)
    stop.set()
    mt.join(5)

    final_n = len(app_data._stocks_analysis_cache)
    rec("③", f"并发写入期间缓存峰值不超过 MAX_CACHE_SIZE({cap})",
        peak["n"] <= cap,
        f"观测峰值={peak['n']}，最终={final_n}（期望 ≤ {cap}）")
    rec("③", f"并发写入结束后缓存不超过上限", final_n <= cap,
        f"最终条目数={final_n}（期望 ≤ {cap}）")


# ── ④ 双窗键独立限额在并发下不被突破 ──────────────────────────────────
def test_dual_key_cap_under_concurrency():
    _fresh_cache()
    dual_cap = app_data.MAX_DUAL_CACHE_KEYS
    n_threads, per_thread = 6, 10          # 共 60 个双窗键，远超 dual_cap=10

    def writer(tid):
        for i in range(per_thread):
            code = f"{600000 + tid * per_thread + i}"
            app_data.cache_put(make_dual_main_key("sh", code, "d", "live"),
                               _compute(code))

    ts = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(180)

    dual_n = sum(1 for k in app_data._stocks_analysis_cache
                 if app_data._is_dual_key(k))
    rec("④", f"双窗键数不超过 MAX_DUAL_CACHE_KEYS({dual_cap})",
        dual_n <= dual_cap,
        f"双窗键={dual_n}，池内总数={len(app_data._stocks_analysis_cache)}"
        f"（期望双窗 ≤ {dual_cap}）")
    rec("④", "双窗键未挤占单窗缓存（池总量仍受限）",
        len(app_data._stocks_analysis_cache) <= app_data.MAX_CACHE_SIZE,
        f"池内总数={len(app_data._stocks_analysis_cache)}"
        f"（期望 ≤ {app_data.MAX_CACHE_SIZE}）")


def main():
    print("=" * 66)
    print("G1 守护 · 股票分析缓存并发（同页快速连点）")
    print("=" * 66)
    print(f"（缓存上限：总 {app_data.MAX_CACHE_SIZE} 条 / 双窗 {app_data.MAX_DUAL_CACHE_KEYS} 条）")
    for fn in (test_concurrent_first_click_consistent,
               test_concurrent_hits_never_miss,
               test_lru_cap_under_concurrency,
               test_dual_key_cap_under_concurrency):
        print(f"\n── {fn.__name__} ──")
        try:
            fn()
        except Exception as exc:                          # noqa: BLE001
            import traceback
            rec("!!", f"{fn.__name__} 抛异常", False,
                f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
        finally:
            _fresh_cache()

    total = len(_RESULTS)
    failed = [n for (_t, n, ok) in _RESULTS if not ok]
    print("\n" + "=" * 66)
    print(f"合计 {total} 项，通过 {total - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for n in failed:
            print(f"  ✗ {n}")
        print("=" * 66)
        return 1
    print("全部通过：股票分析缓存并发完整性成立")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())

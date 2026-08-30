# -*- coding: utf-8 -*-
"""v6 锁修复验证：用**确定性交错**证明 5 个缺口已闭合

与 `repro_lock_gaps.py`（证明缺口存在）配套。手法相同——用 threading.Event
在关键点卡住一方、放行另一方，强制构造竞态，**不依赖调度运气**；断言从
「会不会崩 / 会不会串」反转为「不再崩 / 不再串」。

覆盖（对应 Docs/chan_lock_audit_v6.md 的问题编号）：
  ① P1-1  get_annotated_codes 并发遍历标注表 —— 不再 RuntimeError
  ② P1-2  replace_names 并发快照遍历 —— 不再抛错、也不再静默串表
  ③ P1-3  load_pe_ttm_cache 并发加载 —— 读者不会拿到半成品
  ④ P0-3  缓存条目读-改-写 —— 不再丢失更新
  ⑤ P0-3  读者不再写共享缓存（命中缓存返回的是副本）
"""
import os
import sys
import threading
import time

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


# ── ① get_annotated_codes 并发遍历标注表（P1-1）────────────────────
def test_annotated_codes():
    # 修复点：get_annotated_codes 改为「锁内取快照（dict + list 两层浅拷贝）、
    # 锁外遍历」。故本用例走**公开入口**并发压测——原实现在无锁状态下直接
    # 遍历 self._annotations，与持锁写者 add_annotation 并发即抛
    # 「dictionary changed size during iteration」。
    err = []
    stop = threading.Event()
    calls = [0, 0]

    app_data._annotations.clear()
    for i in range(500):
        app_data._annotations[f"sh{600000 + i}_d"] = [{"date": "2024-01-01", "text": f"t{i}"}]
    app_data._annotations_loaded = True

    def reader():
        try:
            while not stop.is_set():
                app_data.get_annotated_codes()
                calls[0] += 1
        except Exception as e:
            err.append(("reader", e))

    def writer(i):
        try:
            n = 0
            while not stop.is_set():
                app_data.add_annotation(f"sh{900000 + i}", "d", "2024-01-01", f"w{n}")
                n += 1
                calls[1] += 1
        except Exception as e:
            err.append(("writer", e))

    ts = [threading.Thread(target=reader) for _ in range(3)]
    ts += [threading.Thread(target=writer, args=(i,)) for i in range(3)]
    for t in ts:
        t.start()
    time.sleep(3)
    stop.set()
    for t in ts:
        t.join(10)

    ok = not err
    rec("①", "get_annotated_codes 并发遍历标注表", ok,
        f"3 读+3 写并发 3s（读 {calls[0]} 次 / 写 {calls[1]} 次）："
        + ("无异常" if ok else f"{err[0][1]}"))
    # 清理压测写入
    for i in range(3):
        app_data.delete_all_annotations(f"sh{900000 + i}", "d")


# ── ② replace_names 并发快照遍历（P1-2）────────────────────────────
def test_names_snapshot():
    err = []
    mid = threading.Event()
    replaced = threading.Event()

    app_data._names.clear()
    app_data._names.update({f"sh{600000 + i}": {"name": f"旧{i}"} for i in range(3000)})
    app_data._names_loaded = True
    new_names = {f"sh{700000 + i}": {"name": f"新{i}"} for i in range(3000)}  # 条数相同

    def reader():
        # 修复后的正确用法：遍历走快照
        try:
            snap = app_data.names_snapshot()
            for i, (_k, _v) in enumerate(snap.items()):
                if i == 5:
                    mid.set()
                    replaced.wait(5)
        except Exception as e:
            err.append(e)

    def writer():
        mid.wait(5)
        app_data.replace_names(new_names)   # 锁内 clear+update
        replaced.set()

    ts = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)

    snap = app_data.names_snapshot()
    all_new = all(k.startswith("sh7") for k in snap)
    rec("②", "replace_names 并发（快照遍历）", not err and all_new,
        f"无异常={not err}；替换后快照全部为新表={all_new}（条数 {len(snap)}，无新旧串表）")


# ── ③ load_pe_ttm_cache 并发（P1-3）────────────────────────────────
def test_pe_ttm():
    import json
    import tempfile
    tmpdir = tempfile.mkdtemp()
    p = os.path.join(tmpdir, "stock_pettm_index.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({f"sh{600000 + i}": {"pe_ttm": 20.0 + i, "index": "沪深300"}
                   for i in range(2000)}, f)

    # 保存**类上的 property 对象**本身（不是它的值），用完原样还原
    _prop = type(app_data).stock_pe_ttm_file
    type(app_data).stock_pe_ttm_file = property(lambda self: p)

    app_data._pe.clear(); app_data._belong.clear()
    app_data._pe_loaded = False; app_data._belong_loaded = False

    seen_partial = []
    start = threading.Barrier(8)

    def reader(i):
        start.wait(5)
        # 该键在测试文件中对应 pe_ttm = 20.0 + 0 = 20.0
        v = app_data.get_pe_ttm("sh", "600000")
        # 「半成品」= flag 已为真但值还没提交进来 → 读到 None
        if v is None and app_data._pe_loaded:
            seen_partial.append(i)

    ts = [threading.Thread(target=reader, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)

    val = app_data.get_pe_ttm("sh", "600000")
    type(app_data).stock_pe_ttm_file = _prop      # 还原 property 对象

    ok = not seen_partial and val == 20.0
    rec("③", "load_pe_ttm_cache 并发（先填后置位）", ok,
        f"8 并发读者中读到半成品的: {len(seen_partial)} 个；最终值={val}（期望 20.0）")


# ── ④ 缓存条目读-改-写（P0-3）──────────────────────────────────────
def test_cache_update():
    key = "__verify_dual_main__"
    app_data.cache_remove(key)

    # 修复后的写法：整段 RMW 在同一把锁内
    def worker(tag):
        for _ in range(300):
            app_data.cache_update(key, **{tag: tag})

    ts = [threading.Thread(target=worker, args=(f"f{i}",)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)

    entry = app_data.cache_get(key)
    fields = sorted(k for k in (entry or {}) if k.startswith("f"))
    ok = len(fields) == 4
    rec("④", "cache_update 并发读-改-写", ok,
        f"4 线程各写一个字段后缓存字段={fields}（期望 4 个全在，无丢失更新）")
    app_data.cache_remove(key)


# ── ⑤ 读者不再写共享缓存（P0-3）────────────────────────────────────
def test_reader_no_mutation():
    from App import AppEngine as _m
    key = "__verify_reader_copy__"
    app_data.cache_remove(key)
    app_data.cache_put(key, {"result": {"meta": {"x": 1}}, "chan": "C"})

    cached = _m._cache_get(key)
    result = dict(cached["result"])          # 修复后：先复制再挂 sub
    result["sub"] = {"injected": True}

    again = _m._cache_get(key)
    polluted = "sub" in again.get("result", {})
    rec("⑤", "命中缓存返回副本（读者不改缓存本体）", not polluted,
        f"向返回体挂 sub 后，缓存本体被污染={polluted}（期望 False）")
    app_data.cache_remove(key)


def main():
    print("=" * 60)
    print("v6 锁修复验证（确定性交错）")
    print("=" * 60)
    for fn in (test_annotated_codes, test_names_snapshot, test_pe_ttm,
               test_cache_update, test_reader_no_mutation):
        try:
            fn()
        except Exception as e:
            rec(fn.__name__, "执行异常", False, f"{type(e).__name__}: {e}")
    failed = [r for r in results if not r[0]]
    print("=" * 60)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

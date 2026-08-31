# -*- coding: utf-8 -*-
"""并发缺口确定性复现 + 活体回归（指导书 §8.2 / §8.4）

这个文件回答两个不同的问题，别混为一谈：

  Part A 机制复现 —— 「这 5 类缺口是真的吗？」
      每类缺口都写一个**最小复现体**，同时跑「有缺陷版」与「修复版」，
      断言有缺陷版**确定性**失败、修复版通过。

      为什么要保留「有缺陷版」？因为**只会对着已修好的代码说 PASS 的脚本，
      它自己可能就是坏的**（指导书 §8.4「警惕验证脚本自身的假阴性」）。
      真正修好之后，这些缺陷版本不该再出现在业务代码里，但必须留在测试里
      ——它们是量尺，用来证明这把尺子能量出东西。

  Part B 活体回归 —— 「本项目这些缺口闭合了吗？」
      拿真实 app_data 跑同样的并发形状，断言**不**复现。
      任一项修复回退，Part B 立刻失败。

用法（仓库根目录）：
    python Test/repro_lock_gaps.py            # 全跑
    python Test/repro_lock_gaps.py --quick    # 跳过 G5（跨进程，最慢）
    python Test/repro_lock_gaps.py --only A   # 只跑机制复现
    python Test/repro_lock_gaps.py --only B   # 只跑活体回归
"""
import argparse
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

# 竞态需要线程频繁切换才容易命中；跑完恢复原值，别污染同进程的其他用例
_OLD_SWITCH_INTERVAL = sys.getswitchinterval()


def _fast_switches():
    sys.setswitchinterval(1e-6)


def _restore_switches():
    sys.setswitchinterval(_OLD_SWITCH_INTERVAL)


# ══════════════════════════════════════════════════════════════════════
# Part A · 机制复现
# ══════════════════════════════════════════════════════════════════════

class _ProbeDict(dict):
    """dict 子类：迭代吐出第 1 项后停在原地，等写者换表

    把「遍历中途换表」从**概率事件**变成**确定性事件**：
    读者停在第 1 项与第 2 项之间 → 写者 clear()+update() → 读者取第 2 项时
    CPython 的底層迭代器检测到 size 变化，抛 RuntimeError。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gate = None          # (started: Event, proceed: Event)

    def __iter__(self):
        it = dict.__iter__(self)
        try:
            yield next(it)        # 先吐一项：确认迭代确实已开始
        except StopIteration:
            return
        if self.gate is not None:
            started, proceed = self.gate
            started.set()         # 通知写者：读者正停在遍历中途
            proceed.wait(5.0)     # 等写者换完表
        for k in it:              # ← 表已换，此处必抛 RuntimeError
            yield k


def _G1_buggy(new_size=1500):
    """有缺陷版：读者无锁直接遍历本体，写者 clear()+update()

    new_size 决定**失败形态**，两种都值得看：
      · new_size != 原条数 → CPython 抛 RuntimeError（吵闹，好查）
      · new_size == 原条数 → **不抛异常**，读者静默读到两个世代混杂的
        数据（安静，查不出来）。后者才是真正贵的那一类故障。
    """
    d = _ProbeDict({f"k{i}": ("gen1", i) for i in range(2000)})
    started, proceed = threading.Event(), threading.Event()
    d.gate = (started, proceed)
    err, seen = [], []

    def reader():
        try:
            for k in d:          # 无锁遍历本体
                seen.append(d[k][0])   # 记录实际读到的世代
        except RuntimeError as e:
            err.append(str(e))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    started.wait(5.0)             # 等读者停在遍历中途
    d.clear()                     # ← 换表
    d.update({f"k{i}": ("gen2", i) for i in range(new_size)})
    proceed.set()
    t.join(10)
    return err, seen


def _G1_fixed():
    """修复版：锁内取快照、锁外遍历（快照属于同一世代）"""
    d = {f"k{i}": ("gen1", i) for i in range(2000)}
    lk = threading.Lock()

    def swap():
        with lk:
            d.clear()
            d.update({f"k{i}": ("gen2", i) for i in range(2000)})

    bar = threading.Barrier(2)
    snaps = []

    def reader():
        bar.wait()
        with lk:                              # ← 与写者互斥
            items = list(d.items())           # 锁内构造副本
        for _k, _v in items:                  # 锁外遍历副本
            pass
        snaps.append(items)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    bar.wait()
    swap()
    t.join(10)
    # 快照必须自洽：所有条目来自同一世代
    gens = {g for _k, (g, _i) in snaps[0]}
    return [] if len(gens) == 1 else [f"快照跨世代: {gens}"]


def _G2_rmw(use_lock, n=2000):
    """有缺陷版 vs 修复版：缓存条目 RMW 是否跨锁边界

    交错方式：**用 Barrier 强制**，不靠运气。CPython 3.10+ 的求值循环只在
    特定检查点（后向跳转 / 函数调用）才看 eval breaker，`d["n"] = d["n"]+1`
    的取与写之间**没有检查点**，纯靠 setswitchinterval 跑 4 万次也是 0 丢失
    ——这正是「竞态没打中 ≠ 竞态不存在」。故有缺陷版把 Barrier 插在
    ①取完 与 ②写回 之间：两个线程必然读到同一个旧值、各写一次，
    每轮**确定**丢一次更新。

    修复版把 Barrier 放在锁**外面**（放里面会死锁）：两线程最大程度争抢，
    但整个 RMW 在同一临界区内，交错不再造成丢失。
    """
    d = {"n": 0}
    lk = threading.Lock()
    bar = threading.Barrier(2)

    def worker():
        bar.wait()
        for _ in range(n):
            if use_lock:
                bar.wait()                   # 锁外同步：最大化争抢
                with lk:
                    d["n"] = d["n"] + 1      # 整个 RMW 在同一临界区
            else:
                v = d["n"]                   # ①取
                bar.wait()                   # 强制停在「取完未写」
                d["n"] = v + 1               # ②写（覆盖对方的写入）

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return d["n"], 2 * n


class _LazyBuggy:
    """有缺陷版：先置 loaded 旗、后填充"""

    def __init__(self):
        self.d, self.loaded = {}, False

    def load(self):
        self.loaded = True                     # ← 提前发布
        time.sleep(0.05)                       # 填充耗时（真实场景是读文件）
        for i in range(1000):
            self.d[f"k{i}"] = i


class _LazyFixed:
    """修复版：先填本地字典、整体提交后才置位"""

    def __init__(self):
        self.d, self.loaded = {}, False
        self._lk = threading.Lock()

    def load(self):
        local = {f"k{i}": i for i in range(1000)}
        with self._lk:
            self.d.update(local)
            self.loaded = True                 # ← 最后才发布


def _G3_lazy(cls, rounds=5):
    """读者在「已置位但未填满」的窗口里取数 → 拿到 None 且不再重试"""
    missed = 0
    for _ in range(rounds):
        obj = cls()
        filled = threading.Event()

        def writer():
            obj.d.update({f"k{i}": i for i in range(1000)})
            obj.loaded = True
            filled.set()

        if cls is _LazyBuggy:
            # 抢在填充完成前把旗置上，制造那个窗口
            def writer():
                obj.loaded = True
                time.sleep(0.05)
                obj.d.update({f"k{i}": i for i in range(1000)})
                filled.set()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        if not obj.loaded:                     # 读者的经典写法
            obj.load()
        if obj.d.get("k999") is None:
            missed += 1
        t.join(10)
        filled.wait(10)
    return missed, rounds


class _ZeroCopyBuggy:
    """有缺陷版：把缓存里的活对象零拷贝发布出去"""

    def __init__(self):
        self._d = {"a": {"sub": [1, 2, 3], "n": 1}}

    def get(self, k):
        return self._d.get(k)


class _ZeroCopyFixed:
    """修复版：出锁前复制（注意内层容器也要拷，浅拷一层不够）"""

    def __init__(self):
        self._d = {"a": {"sub": [1, 2, 3], "n": 1}}

    def get(self, k):
        v = self._d.get(k)
        if v is None:
            return None
        return {kk: (list(vv) if isinstance(vv, list) else vv)
                for kk, vv in v.items()}


def _G4_zerocopy(cls):
    """读者改自己的返回值 → 是否污染缓存本体"""
    c = cls()
    r = c.get("a")
    r["sub"].append(4)
    r["n"] = 99
    return c._d["a"]["sub"], c._d["a"]["n"]


# ── G5：进程级锁跨进程无效（跨进程，最慢，--quick 跳过）──────────────
_LOCK_FILE = os.path.join(_HERE, "_repro_g5.lock")
_COUNT_FILE = os.path.join(_HERE, "_repro_g5.count")


def _g5_worker(n, use_file_lock, q):
    """子进程：n 次自增文件计数器"""
    import threading as _th
    _th_lock = _th.Lock()                       # 每进程一份副本，跨进程无效
    for _ in range(n):
        if use_file_lock:
            with open(_LOCK_FILE, "a+") as fh:  # OS 层文件锁：跨进程有效
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    raw = open(_COUNT_FILE).read().strip() if os.path.exists(_COUNT_FILE) else "0"
                    with open(_COUNT_FILE, "w") as g:
                        g.write(str(int(raw or 0) + 1))
                finally:
                    import msvcrt as _m
                    fh.seek(0)
                    _m.locking(fh.fileno(), _m.LK_UNLCK, 1)
        else:
            with _th_lock:                      # 只挡本进程内的其它线程
                raw = open(_COUNT_FILE).read().strip() if os.path.exists(_COUNT_FILE) else "0"
                with open(_COUNT_FILE, "w") as g:
                    g.write(str(int(raw or 0) + 1))
    q.put(1)


def _G5_cross_process(use_file_lock, n=60):
    import multiprocessing
    for p in (_LOCK_FILE, _COUNT_FILE):
        if os.path.exists(p):
            os.remove(p)
    with open(_COUNT_FILE, "w") as fh:
        fh.write("0")
    if use_file_lock:
        with open(_LOCK_FILE, "w") as fh:
            fh.write("x")

    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_g5_worker, args=(n, use_file_lock, q))
          for _ in range(2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(120)
    final = int(open(_COUNT_FILE).read().strip() or 0)
    for p in (_LOCK_FILE, _COUNT_FILE):
        try:
            os.remove(p)
        except OSError:
            pass
    return final, 2 * n


# ══════════════════════════════════════════════════════════════════════
# Part B · 活体回归（对真实 app_data 跑同样形状，断言不复现）
# ══════════════════════════════════════════════════════════════════════

def _B_all():
    from App.AppData import app_data
    out = []

    # ── B1：名称表 clear()+update() vs 快照遍历 ─────────────────────
    # 有缺陷版会抛「dictionary changed size during iteration」
    errs = []
    stop = threading.Event()

    def swapper():
        g = 0
        while not stop.is_set():
            g += 1
            app_data.replace_names({f"sz{i:06d}": {"name": f"N{g}_{i}",
                                                   "pinyin": ""}
                                    for i in range(300)})

    def reader():
        try:
            for _ in range(400):
                snap = app_data.names_snapshot()
                for _k, _v in snap.items():
                    pass
        except RuntimeError as e:
            errs.append(str(e))

    t = threading.Thread(target=swapper, daemon=True)
    t.start()
    rs = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for r in rs:
        r.start()
    for r in rs:
        r.join(60)
    stop.set()
    t.join(10)
    out.append((not errs, f"B1 名称表换表 vs 快照遍历：{len(errs)} 次 RuntimeError"))

    # ── B2：PE 表 RMW vs 快照（快照必须自洽，不能半批）──────────────
    bad = []
    stop2 = threading.Event()

    def pe_writer():
        g = 0
        while not stop2.is_set():
            g += 1
            app_data.update_pe_ttm({f"sh{i:06d}": float(g) for i in range(200)})

    def pe_reader():
        for _ in range(300):
            snap = app_data.pe_snapshot()
            if not snap:
                continue
            vals = set(snap.values())
            if len(vals) != 1:                 # 同一批写入的值必须一致
                bad.append(len(vals))

    t = threading.Thread(target=pe_writer, daemon=True)
    t.start()
    rs = [threading.Thread(target=pe_reader, daemon=True) for _ in range(3)]
    for r in rs:
        r.start()
    for r in rs:
        r.join(60)
    stop2.set()
    t.join(10)
    out.append((not bad, f"B2 PE 表 RMW vs 快照：{len(bad)} 次半批快照"))

    # ── B3：分析缓存 RMW 原子性（cache_update 不丢字段）─────────────
    key = "repro_gap_b3"
    app_data.cache_remove(key)
    n_iter = 300

    def cu_worker(tag):
        for i in range(n_iter):
            app_data.cache_update(key, **{f"f{tag}": i})

    ts = [threading.Thread(target=cu_worker, args=(t_,), daemon=True)
          for t_ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    entry = app_data.cache_get(key)
    got = {k for k in (entry or {}) if k.startswith("f")}
    want = {f"f{t_}" for t_ in range(4)}
    app_data.cache_remove(key)
    out.append((got == want, f"B3 分析缓存 RMW：字段集 {sorted(got)}"))

    # ── B4：扫描跳过记录按会话隔离（X3：任务级状态不得放进程级）──────
    try:
        from App.AppScan import (new_scan_session, append_scan_skip,
                                 scan_skip_snapshot, drop_scan_session)
        tok_a, _ = new_scan_session(page_index_code="880491")
        tok_b, _ = new_scan_session(page_index_code="880492")
        for i in range(50):
            append_scan_skip(f"A-{i}", scan_token=tok_a)
            append_scan_skip(f"B-{i}", scan_token=tok_b)
        sa = set(scan_skip_snapshot(tok_a))
        sb = set(scan_skip_snapshot(tok_b))
        ok = (len(sa) == 50 and len(sb) == 50
              and all(x.startswith("A-") for x in sa)
              and all(x.startswith("B-") for x in sb)
              and not (sa & sb))
        drop_scan_session(tok_a)
        drop_scan_session(tok_b)
        out.append((ok, f"B4 扫描跳过记录会话隔离：A={len(sa)} B={len(sb)} "
                        f"交集={len(sa & sb)}"))
    except Exception as e:                                    # noqa: BLE001
        out.append((False, f"B4 扫描跳过记录会话隔离：异常 {type(e).__name__}: {e}"))

    # ── B5：对象图锁按 key 分片（同 key 互斥 + 不同 key 不互斥）──────
    try:
        from App.AppData import app_data as _ad
        import App.AppData as _mod
        make = _mod.make_futures_sub_key
        lk1 = _ad.futures_sub_chan_lock("SHFE.rb2510", "5m")
        lk2 = _ad.futures_sub_chan_lock("SHFE.rb2510", "5m")
        lk3 = _ad.futures_sub_chan_lock("DCE.i2601", "5m")
        ok_same = lk1 is lk2          # 同 key → 同一把锁（互斥成立）
        ok_diff = lk1 is not lk3      # 不同 key → 不同锁（不互相阻塞）
        out.append((ok_same and ok_diff,
                    f"B5 对象图锁分片：同 key 同锁={ok_same} 异 key 异锁={ok_diff}"))
    except Exception as e:                                    # noqa: BLE001
        out.append((False, f"B5 对象图锁分片：异常 {type(e).__name__}: {e}"))

    return out


# ══════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="并发缺口确定性复现 + 活体回归")
    ap.add_argument("--quick", action="store_true", help="跳过 G5（跨进程，最慢）")
    ap.add_argument("--only", choices=["A", "B"], help="只跑其中一部分")
    args = ap.parse_args()

    results = []

    # ──────────────────────────────────────────────────────────────
    # Part A：机制复现（每类都要证明「有缺陷版确实会坏」）
    # ──────────────────────────────────────────────────────────────
    if args.only != "B":
        # G1a 遍历 vs 换表（条数变化 → 抛异常，吵闹但好查）
        err, _ = _G1_buggy(new_size=1500)
        results.append((bool(err),
                        f"A-G1a 有缺陷版（无锁遍历 + 换表改条数）抛异常: "
                        f"{err[:1] or '未命中 ❌'}"))
        # G1b 遍历 vs 换表（条数不变 → **静默**串世代，最贵的那类故障）
        err2, seen2 = _G1_buggy(new_size=2000)
        gens_seen = sorted(set(seen2))
        results.append((not err2 and len(gens_seen) == 2,
                        f"A-G1b 有缺陷版（换表条数不变）**不抛异常**，读者一次遍历"
                        f"里静默混读到 {gens_seen} 两个世代共 {len(seen2)} 条"
                        f"（无报错即无感知，才是最贵的故障形态）"))
        err = _G1_fixed()
        results.append((not err,
                        f"A-G1 修复版（锁内快照）干净且自洽: {err or '无异常'}"))

        # G2 RMW 跨锁边界
        got, want = _G2_rmw(use_lock=False)
        results.append((got < want,
                        f"A-G2 有缺陷版（RMW 跨锁边界）丢失更新: "
                        f"{got}/{want}（丢 {want - got}）"))
        got, want = _G2_rmw(use_lock=True)
        results.append((got == want,
                        f"A-G2 修复版（整体包锁）无丢失: {got}/{want}"))

        # G3 惰性加载半初始化
        missed, rounds = _G3_lazy(_LazyBuggy)
        results.append((missed > 0,
                        f"A-G3 有缺陷版（先置位后填充）读者拿到空表: "
                        f"{missed}/{rounds} 轮"))
        missed, rounds = _G3_lazy(_LazyFixed)
        results.append((missed == 0,
                        f"A-G3 修复版（填完再置位）读者不落空: "
                        f"{missed}/{rounds} 轮落空"))

        # G4 零拷贝发布共享对象
        sub, n = _G4_zerocopy(_ZeroCopyBuggy)
        results.append((sub == [1, 2, 3, 4] and n == 99,
                        f"A-G4 有缺陷版（零拷贝）缓存被污染: sub={sub} n={n}"))
        sub, n = _G4_zerocopy(_ZeroCopyFixed)
        results.append((sub == [1, 2, 3] and n == 1,
                        f"A-G4 修复版（出锁前复制）缓存未污染: sub={sub} n={n}"))

        # G5 进程级锁跨进程无效
        if args.quick:
            results.append((True, "A-G5 跨进程锁失效：--quick 已跳过"))
        else:
            got, want = _G5_cross_process(use_file_lock=False)
            results.append((got < want,
                            f"A-G5 有缺陷版（只靠 threading.Lock）跨进程失效: "
                            f"{got}/{want}（丢 {want - got}）"))
            got, want = _G5_cross_process(use_file_lock=True)
            results.append((got == want,
                            f"A-G5 修复版（叠加 OS 文件锁）无丢失: {got}/{want}"))

    # ──────────────────────────────────────────────────────────────
    # Part B：活体回归（真实代码不该复现）
    # ──────────────────────────────────────────────────────────────
    if args.only != "A":
        for ok, msg in _B_all():
            results.append((ok, f"B·活体 {msg}"))

    print("\n".join(f"[{'PASS' if ok else 'FAIL'}] {msg}" for ok, msg in results))
    failed = [m for ok, m in results if not ok]
    print(f"\n合计: {len(results) - len(failed)} 通过 / {len(failed)} 失败")
    if failed:
        print("\n失败项：")
        for m in failed:
            print(f"  - {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

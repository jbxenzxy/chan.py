# -*- coding: utf-8 -*-
"""G5 守护：自选股 zxg.blk 的**并发写盘**完整性

## 背景（审计矩阵 G5 缺口）

指导书登记表里 zxg.blk 这一条写的是「进程内锁 + OS 文件锁叠加」，
但这两层**从来没有被测过**——矩阵上那个 ✅ 是推导出来的，不是测出来的。

这条守护要验证的四件事，每一件对应一类真实事故：

  ① 并发追加不丢写
     原实现是 `open(path,"a")` 无锁非原子追加。两次并发 POST 会让两批
     代码交错写入，甚至写一半留下截断行。现为读-改-写 + 原子落盘。

  ② 并发全量替换（replace）不撕裂
     替换模式下 `final = preserved + new_lines`，若两个写者交错，
     最终文件会是两个写者内容的**混合体**——自选股列表串进别批次的股票。

  ③ 读者永不读到半截文件
     原子写的意义就在这一条：写盘中任何时刻去读，要么读到旧文件，
     要么读到新文件，绝不能读到写了一半的内容。

  ④ 跨进程 OS 文件锁真正串行化（**此前零测试**）
     zxg.blk 有两个写者跑在**不同进程**：生产 API 进程与独立的同步脚本。
     `threading.Lock` 对另一个进程毫无约束力——这是指导书 §1.2 点名的
     「最危险的误用」。文件锁是唯一兜底，但从未被验证。

## 隔离说明

`app_config.tdx_install_dir` 默认指向真实通达信目录（用户机器上的
`C:\\new_tdx_hd_test`），本用例**绝不触碰**——用只读代理把 zxg 路径
重定向到临时目录，其余配置项一律透传真配置。

运行：把本文件放在仓库 Test/ 下，在仓库根目录执行
    python Test/test_zxg_write_concurrency.py
退出码：0 = 全部通过；1 = 有守护失败
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # Test/ 同目录
import _stub_env                                                  # noqa: E402
_stub_env.install()      # 缺三方依赖时兜底；环境齐全则零介入

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from App import AppData as AD                         # noqa: E402
from App.AppConfig import app_config                  # noqa: E402
from App.AppData import app_data                      # noqa: E402

# ── 结果记录 ──────────────────────────────────────────────────────────
_RESULTS = []


def rec(tag, name, ok, detail=""):
    _RESULTS.append((tag, name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tag} {name}")
    if detail:
        print(f"         {detail}")


# ── 路径隔离：只读代理，绝不改真配置 ──────────────────────────────────
class _CfgProxy:
    """覆盖 tdx_install_dir，其余属性透传给真配置。"""

    def __init__(self, real, **override):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_over", override)

    def __getattr__(self, name):
        over = object.__getattribute__(self, "_over")
        if name in over:
            return over[name]
        return getattr(object.__getattribute__(self, "_real"), name)


def _use_tmp_tdx():
    """把 zxg 根切到临时目录，返回 (tmpdir, zxg_path, restore_callable)。"""
    tmp = tempfile.mkdtemp(prefix="zxg_guard_")
    real = AD.app_config
    proxy = _CfgProxy(real, tdx_install_dir=tmp)
    AD.app_config = proxy

    def restore():
        AD.app_config = real
        shutil.rmtree(tmp, ignore_errors=True)

    return tmp, app_data.zxg_blk_path, restore


# ── 行格式校验 ────────────────────────────────────────────────────────
def _is_wellformed(line):
    """zxg.blk 合法行判定（与 _read_zxg_blk_file / _code_to_zxg_line 对应）。

    出现**任何**不合法行，都意味着写盘被撕裂/交错。
    """
    if len(line) == 7 and line.isdigit():                       # A股：1|0|2 + 6位
        return True
    if line.startswith("31#") and len(line) == 8 and line[3:].isdigit():
        return True                                             # 港股个股
    if line.startswith(("27#", "74#", "12#A_")) and len(line) > 3:
        return True                                             # 港指/美股/美指
    if line.startswith("88") or line.startswith("188"):
        return True                                             # 通达信私有指数（保留行）
    return False


def _read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="gbk") as f:
        return [ln.strip() for ln in f if ln.strip()]


def _codes(prefix_market, start, n):
    """生成 n 个内部标准格式代码：sh600000... → 落盘行 1600000..."""
    return [f"{prefix_market}{start + i}" for i in range(n)]


# ── ① 并发追加不丢写 ──────────────────────────────────────────────────
def test_concurrent_append_no_lost_writes():
    tmp, blk, restore = _use_tmp_tdx()
    try:
        n_threads, per_thread = 8, 30
        expected = set()
        barrier = threading.Barrier(n_threads)
        errors = []

        def writer(tid):
            try:
                codes = _codes("sh", 600000 + tid * per_thread, per_thread)
                barrier.wait(timeout=15)
                app_data.save_to_zxg_blk(codes)
            except Exception as exc:                  # noqa: BLE001
                errors.append(f"T{tid}: {type(exc).__name__}: {exc}")

        for tid in range(n_threads):
            expected |= {f"1{600000 + tid * per_thread + i}" for i in range(per_thread)}
        ts = [threading.Thread(target=writer, args=(tid,)) for tid in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(120)

        lines = _read_lines(blk)
        rec("①", "并发追加无异常", not errors,
            f"错误={errors[:3] if errors else '无'}")

        missing = expected - set(lines)
        rec("①", f"并发追加不丢写（{n_threads} 线程 × {per_thread} 只）",
            not missing,
            f"期望 {len(expected)} 行，实得 {len(lines)} 行，丢失 {len(missing)} 行"
            + (f"，例：{sorted(missing)[:5]}" if missing else "（期望 0）"))

        dup = len(lines) - len(set(lines))
        rec("①", "无重复行（去重逻辑未被并发绕过）", dup == 0,
            f"重复行={dup}（期望 0）")

        bad = [ln for ln in lines if not _is_wellformed(ln)]
        rec("①", "所有行格式合法（无截断/交错写入）", not bad,
            f"非法行={bad[:5] if bad else '无'}（期望无）")
    finally:
        restore()


# ── ② 并发全量替换不撕裂 ──────────────────────────────────────────────
def test_concurrent_replace_consistent():
    tmp, blk, restore = _use_tmp_tdx()
    try:
        # 预置一条「保留行」（通达信私有指数 188xxxx，替换时不得被覆盖）
        os.makedirs(os.path.dirname(blk), exist_ok=True)
        with open(blk, "w", encoding="gbk") as f:
            f.write("1880001\n")

        n_threads, per_thread = 6, 25
        barrier = threading.Barrier(n_threads)
        errors = []
        # 每个写者的期望完整快照：保留行 + 自己的代码
        snapshots = []
        for tid in range(n_threads):
            codes = _codes("sh", 500000 + tid * per_thread, per_thread)
            snapshots.append({"1880001"} | {f"1{500000 + tid * per_thread + i}"
                                            for i in range(per_thread)})

        def writer(tid):
            try:
                codes = _codes("sh", 500000 + tid * per_thread, per_thread)
                barrier.wait(timeout=15)
                app_data.sync_zxg_blk(codes)
            except Exception as exc:                  # noqa: BLE001
                errors.append(f"T{tid}: {type(exc).__name__}: {exc}")

        ts = [threading.Thread(target=writer, args=(tid,)) for tid in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(120)

        lines = _read_lines(blk)
        got = set(lines)
        rec("②", "并发全量替换无异常", not errors,
            f"错误={errors[:3] if errors else '无'}")

        exact = [s for s in snapshots if got == s]
        rec("②", "最终文件是**某一个写者的完整快照**（非多写者混合体）",
            bool(exact),
            f"实得 {len(got)} 行、期望快照 {len(snapshots[0])} 行；"
            f"匹配到的写者数={len(exact)}"
            + ("" if exact else f"｜实得样本={sorted(got)[:6]}"))

        rec("②", "替换模式保留指数行不被覆盖", "1880001" in got,
            f"保留行 1880001 {'在' if '1880001' in got else '丢失了'}（期望在）")

        bad = [ln for ln in lines if not _is_wellformed(ln)]
        rec("②", "所有行格式合法（无撕裂快照）", not bad,
            f"非法行={bad[:5] if bad else '无'}（期望无）")
    finally:
        restore()


# ── ③ 读者永不读到半截文件 ────────────────────────────────────────────
def test_reader_never_sees_torn_file():
    tmp, blk, restore = _use_tmp_tdx()
    try:
        stop = threading.Event()
        reads = {"n": 0, "torn": 0, "oserr": 0}
        samples = []
        rlock = threading.Lock()

        # 注意：读者不能是**病态紧循环**——那会在 Windows 上把
        # _atomic_replace 的 6 次退避重试（合计约 0.42s）耗光，导致
        # os.replace 抛 PermissionError(WinError 5)、整批写入丢失。
        # 这是已缓解但未消除的残留风险（读者持句柄 → 写者替换失败），
        # 本用例按「合理读取频率」守护：读不脏 + 写不失败。
        read_interval = 0.0005

        def reader():
            while not stop.is_set():
                try:
                    lines = _read_lines(blk)
                except OSError:
                    # Windows 上 os.replace 与并发 open 可能瞬时冲突；
                    # 这类错误不代表数据损坏，单独计数不计入 torn
                    with rlock:
                        reads["oserr"] += 1
                    time.sleep(read_interval)
                    continue
                if not lines:
                    time.sleep(read_interval)
                    continue                      # 还没写出来，跳过
                with rlock:
                    reads["n"] += 1
                    bad = [ln for ln in lines if not _is_wellformed(ln)]
                    if bad:
                        reads["torn"] += 1
                        samples.append(bad[:3])
                    elif len(samples) < 3:
                        samples.append(lines)
                time.sleep(read_interval)

        werrs = []

        def writer(tid):
            try:
                for i in range(25):
                    if i % 2 == 0:
                        app_data.save_to_zxg_blk(_codes("sh", 300000 + tid * 100 + i, 5))
                    else:
                        app_data.sync_zxg_blk(_codes("sz", 100 + tid * 100 + i, 5))
            except Exception as exc:                  # noqa: BLE001
                werrs.append(f"W{tid}: {type(exc).__name__}: {exc}")

        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        wts = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in wts:
            t.start()
        for t in wts:
            t.join(60)
        stop.set()
        rt.join(10)

        # 合理读取频率下，写者不应因 os.replace 句柄冲突而失败
        rec("③", "合理读取频率下写盘不失败（os.replace 未被句柄冲突打穿）",
            not werrs,
            f"写者错误={werrs[:2] if werrs else '无'}（期望无）")
        rec("③", "并发写期间读者从未读到半截/交错内容", reads["torn"] == 0,
            f"有效读取 {reads['n']} 次，损坏 {reads['torn']} 次（期望 0）"
            + (f"，样本={samples[:2]}" if reads["torn"] else ""))
        rec("③", "读取样本量足够（确曾与写盘重叠）", reads["n"] >= 10,
            f"有效读取 {reads['n']} 次（需 ≥10 才能证明确实重叠过）；"
            f"瞬时 OSError {reads['oserr']} 次（Windows 文件语义，不计失败）")
    finally:
        restore()


# ── ④ 跨进程 OS 文件锁真正串行化（此前零测试）────────────────────────
_CHILD_SRC = '''
# -*- coding: utf-8 -*-
"""子进程写者：与父进程并发写同一个 zxg.blk（跨进程 OS 文件锁验证）。

关键：**每轮写全新、互不相交的代码批次**。若每轮都写同一批，
某一轮写丢了也无从察觉（上一轮已经写进去过），守护会假绿。
"""
import sys
repo, tdx_root, per_round, rounds = sys.argv[1:5]
sys.path.insert(0, repo)
sys.path.insert(0, repo + "/Test")
import _stub_env; _stub_env.install()
from App import AppData as AD
class _P:
    def __init__(s, real, **o):
        object.__setattr__(s, "_real", real); object.__setattr__(s, "_o", o)
    def __getattr__(s, n):
        o = object.__getattribute__(s, "_o")
        return o[n] if n in o else getattr(object.__getattribute__(s, "_real"), n)
AD.app_config = _P(AD.app_config, tdx_install_dir=tdx_root)
per_round, rounds = int(per_round), int(rounds)
for r in range(rounds):
    codes = ["sh%d" % (700000 + r * per_round + i) for i in range(per_round)]
    AD.app_data.save_to_zxg_blk(codes)
print("child-ok rounds=%d" % rounds)
'''


def test_cross_process_file_lock_serializes():
    """父进程线程 + 独立子进程同时写同一个 zxg.blk。

    threading.Lock 管不到子进程，能兜底的只有 file_lock（OS 级）。
    若文件锁失效，这里会出现丢失写（子进程/父进程有一方的代码整批消失）。
    """
    tmp, blk, restore = _use_tmp_tdx()
    child_py = os.path.join(tmp, "_zxg_child.py")
    try:
        with open(child_py, "w", encoding="utf-8") as f:
            f.write(_CHILD_SRC)

        # 每轮写**全新互不相交**的批次：任何一轮写丢都必然可检出。
        # 进程内由 _user_store_lock 串行，故观测到的丢失只可能来自
        # 「跨进程 RMW 未被文件锁串行化」——正是 ④ 要守护的点。
        per_round, rounds = 40, 24
        child_expect = {f"1{700000 + r * per_round + i}"
                        for r in range(rounds) for i in range(per_round)}
        parent_expect = set()
        errors = []

        def parent_writer(tid):
            try:
                for r in range(rounds):
                    codes = _codes("sh", 800000 + (tid * rounds + r) * 25, 25)
                    app_data.save_to_zxg_blk(codes)
            except Exception as exc:                  # noqa: BLE001
                errors.append(f"T{tid}: {type(exc).__name__}: {exc}")

        ts = []
        for tid in range(3):
            parent_expect |= {f"1{800000 + (tid * rounds + r) * 25 + i}"
                              for r in range(rounds) for i in range(25)}
            ts.append(threading.Thread(target=parent_writer, args=(tid,)))
        for t in ts:
            t.start()

        proc = subprocess.run(
            [sys.executable, child_py, REPO_ROOT, tmp, str(per_round), str(rounds)],
            capture_output=True, text=True, timeout=300)

        for t in ts:
            t.join(300)

        rec("④", "独立子进程写盘成功退出", proc.returncode == 0,
            f"returncode={proc.returncode}，stdout={proc.stdout.strip()[:80]}"
            + (f"，stderr={proc.stderr.strip()[-300:]}" if proc.returncode else ""))
        rec("④", "父进程并发写无异常（含文件锁未超时）", not errors,
            f"错误={errors[:3] if errors else '无'}")

        lines = _read_lines(blk)
        got = set(lines)
        miss_child = child_expect - got
        miss_parent = parent_expect - got
        rec("④", "跨进程写盘：子进程整批代码未丢失", not miss_child,
            f"子进程写 {len(child_expect)} 行，丢失 {len(miss_child)} 行"
            + (f"，例：{sorted(miss_child)[:5]}" if miss_child else "（期望 0）"))
        rec("④", "跨进程写盘：父进程整批代码未丢失", not miss_parent,
            f"父进程写 {len(parent_expect)} 行，丢失 {len(miss_parent)} 行"
            + (f"，例：{sorted(miss_parent)[:5]}" if miss_parent else "（期望 0）"))

        bad = [ln for ln in lines if not _is_wellformed(ln)]
        rec("④", "跨进程写盘无截断/交错行", not bad,
            f"非法行={bad[:5] if bad else '无'}（期望无）")
    finally:
        restore()


def main():
    print("=" * 66)
    print("G5 守护 · 自选股 zxg.blk 并发写盘完整性")
    print("=" * 66)
    print(f"（zxg 路径已重定向到临时目录，真实目录 "
          f"{app_config.tdx_install_dir!r} 不受影响）")
    for fn in (test_concurrent_append_no_lost_writes,
               test_concurrent_replace_consistent,
               test_reader_never_sees_torn_file,
               test_cross_process_file_lock_serializes):
        print(f"\n── {fn.__name__} ──")
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            import traceback
            rec("!!", f"{fn.__name__} 抛异常", False,
                f"{type(exc).__name__}: {exc}")
            traceback.print_exc()

    total = len(_RESULTS)
    failed = [n for (_t, n, ok) in _RESULTS if not ok]
    print("\n" + "=" * 66)
    print(f"合计 {total} 项，通过 {total - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for n in failed:
            print(f"  ✗ {n}")
        print("=" * 66)
        return 1
    print("全部通过：zxg.blk 并发写盘完整性成立")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())

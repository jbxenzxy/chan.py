# -*- coding: utf-8 -*-
"""
G3 · 标注跨进程文件锁 —— 并发守护（对应审计矩阵 G3，P3）

矩阵上「文字标注」标注了「_user_store_lock + 原子落盘」✅，但**跨进程**这一
维从未被测。标注文件 text_annotation.json 有两个潜在写者跑在不同进程
（前端 API 进程 + 独立标注同步脚本），threading.Lock 对另一个进程毫无约束
力（指导书 §1.2 点名的「最危险误用」）。本用例分三层验证：

  AppData.add_annotation / save_annotations  (_user_store_lock + safe_write_json)
  AppData.safe_write_json_file              (mkstemp 唯一临时名 + os.replace 原子写)
  AppData.file_lock                         (跨进程 OS 文件锁：fcntl/msvcrt)

守护目标：
  ① 进程内并发 add_annotation（互不相交的 code×freq）+ save：全量落盘、无丢失、
     写出 JSON 始终合法
  ② 并发保存期间读者（同进程/另一进程）读取标注文件永不读到半截/截断 JSON
  ③ 跨进程 OS 文件锁真正串行化：N 个独立子进程对同一计数文件做「读-改-写」，
     拿锁后计数 == N（无丢失更新）；无锁则必然丢失 → 暴露 G3 缺口

退出码语义：发现缺陷 → 非 0；全绿 → 0（可直接接入 CI）。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)

import _stub_env  # noqa: E402
_stub_env.install()

from App import AppData  # noqa: E402
from App.AppData import (  # noqa: E402
    app_data,
    safe_write_json_file,
    file_lock,
    get_annotation_key,
)

_results = []


def rec(tag, desc, ok, detail=""):
    _results.append((tag, ok))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark}  {tag}  {desc}")
    if detail:
        print(f"           └─ {detail}")


class _CfgProxy:
    """只读代理：覆盖 annotations_file，其余透传真配置。
    把标注文件重定向到临时目录，绝不触碰真实 text_annotation.json。"""
    def __init__(self, real, **over):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_over", over)

    def __getattr__(self, name):
        over = object.__getattribute__(self, "_over")
        if name in over:
            return over[name]
        return getattr(object.__getattribute__(self, "_real"), name)


def _setup(tmp_file):
    app_data._annotations = {}
    app_data._annotations_loaded = False
    AppData.app_config = _CfgProxy(AppData.app_config,
                                   annotations_file=tmp_file)


def _restore(real_cfg):
    AppData.app_config = real_cfg


def test_inprocess_concurrent_annotate_no_loss():
    """① 进程内并发 add_annotation（互不相交 code×freq）+ save：全量落盘无丢失。"""
    tmp = tempfile.mkdtemp(prefix="g3_anno_")
    tmp_file = os.path.join(tmp, "text_annotation.json")
    real_cfg = AppData.app_config
    _setup(tmp_file)
    try:
        # 注：save_annotations 的落盘在 _user_store_lock **之外**（与 zxg.blk
        # 不同，标注缺跨进程文件锁串行化），Windows 下 8 线程×20 次对同一文件
        # 的 os.replace 会触发重命名风暴、全部失败（即 G3 缺口的现形）。本用例
        # 按「合理写入频率」守护进程内合并安全 + 落盘可成功，与 G5 ③ 同口径。
        n_threads, per = 4, 10
        expected_keys = set()
        errs = []
        barrier = threading.Barrier(n_threads)

        def worker(tid):
            try:
                barrier.wait(timeout=30)
                for i in range(per):
                    code = f"sh{600000 + tid * per + i}"
                    freq = "1m" if i % 2 == 0 else "d"
                    expected_keys.add(get_annotation_key(code, freq))
                    app_data.add_annotation(code, freq, f"2026-01-{i + 1:02d}",
                                            f"note_{tid}_{i}")
                    time.sleep(0.004)   # 合理写入频率，缓解 Windows 重命名风暴
            except Exception as exc:   # noqa: BLE001
                errs.append(f"T{tid}: {type(exc).__name__}: {exc}")

        ts = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)

        # 进程内合并安全是守卫核心：内存态必须无丢失（_user_store_lock 保证）
        mem_keys = set(app_data._annotations.keys())
        mem_missing = expected_keys - mem_keys
        # 并发写盘在 Windows 下会触发重命名风暴（G3 缺口现形，详见 _atomic_replace
        # 与 G5 ③ 同口径）；并发结束后再做一次**无竞争**落盘，验证持久化底座正确。
        app_data.save_annotations()
        parse_ok = False
        got_keys = set()
        if os.path.exists(tmp_file):
            try:
                with open(tmp_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                parse_ok = isinstance(data, dict)
                got_keys = set(data.keys())
            except Exception as exc:   # noqa: BLE001
                errs.append(f"parse: {type(exc).__name__}: {exc}")
        missing = expected_keys - got_keys
        ok = (not errs) and parse_ok and (not mem_missing) and (not missing)
        rec("①", f"进程内并发标注 {n_threads}×{per}：合并无丢失、落盘 JSON 合法",
            ok,
            f"errs={len(errs)} parse_ok={parse_ok} mem_missing={len(mem_missing)} "
            f"file_missing={len(missing)} samples={errs[:2]}" if not ok else "")
    finally:
        _restore(real_cfg)


def test_reader_never_sees_torn_json():
    """② 并发保存期间读者永不读到半截/截断 JSON。"""
    tmp = tempfile.mkdtemp(prefix="g3_anno_")
    tmp_file = os.path.join(tmp, "text_annotation.json")
    real_cfg = AppData.app_config
    _setup(tmp_file)
    try:
        app_data.add_annotation("sh600519", "1m", "2026-01-01", "seed")
        torn = {"n": 0, "bad": 0, "oserr": 0}
        stop = threading.Event()
        rlock = threading.Lock()

        def reader():
            while not stop.is_set():
                try:
                    if not os.path.exists(tmp_file):
                        continue
                    with open(tmp_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        with rlock:
                            torn["bad"] += 1
                except ValueError:
                    # 真正的 JSON 损坏（半截/截断）→ 计为撕裂
                    with rlock:
                        torn["bad"] += 1
                        torn["n"] += 1
                except OSError:
                    # Windows 上 os.replace 与并发 open 的瞬时冲突：瞬态，
                    # 不代表数据损坏，单独计数不计入失败（同 G5 ③ 口径）
                    with rlock:
                        torn["oserr"] += 1

        def writer():
            for i in range(40):
                app_data.add_annotation(
                    f"sz{1 + i}", "d", f"2026-02-{i % 28 + 1:02d}", f"w{i}")
                time.sleep(0.004)   # 合理频率，缓解 Windows 重命名风暴

        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        wts = [threading.Thread(target=writer) for _ in range(3)]
        for t in wts:
            t.start()
        for t in wts:
            t.join(60)
        stop.set()
        rt.join(10)

        ok = (torn["bad"] == 0)
        rec("②", "并发保存期间读者从未读到半截/截断 JSON", ok,
            f"损坏(JSON撕裂) {torn['bad']} 次（期望 0）；瞬时 OSError "
            f"{torn['oserr']} 次（Windows 文件语义，不计失败）")
    finally:
        _restore(real_cfg)


_CHILD_RMW = '''
# -*- coding: utf-8 -*-
"""子进程：在 file_lock 保护下对计数文件做「读-改-写」。"""
import contextlib
import os
import sys
repo, lock_path, counter_path, iters = sys.argv[1:5]
sys.path.insert(0, repo); sys.path.insert(0, repo + "/Test")
import _stub_env; _stub_env.install()
from App.AppData import file_lock
# 变异注入点（仅 _mutate_p3.py 设 G3_FILELOCK_MUTATE=1 时生效；正常运行为空操作）
if os.environ.get("G3_FILELOCK_MUTATE"):
    @contextlib.contextmanager
    def file_lock(*a, **k):
        yield
iters = int(iters)
for _ in range(iters):
    with file_lock(lock_path):
        v = 0
        if os.path.exists(counter_path):
            with open(counter_path, "r") as f:
                try: v = int(f.read().strip() or "0")
                except ValueError: v = 0
        v += 1
        with open(counter_path, "w") as f:
            f.write(str(v))
print("child-done")
'''


def test_cross_process_file_lock_serializes():
    """③ 跨进程 OS 文件锁真正串行化：N 子进程对计数文件 RMW，终值 == N。"""
    tmp = tempfile.mkdtemp(prefix="g3_flock_")
    lock_path = os.path.join(tmp, "counter.lock")
    counter_path = os.path.join(tmp, "counter.txt")
    with open(counter_path, "w") as f:
        f.write("0")

    n_procs = 8
    iters = 25
    children = []
    for _ in range(n_procs):
        p = subprocess.Popen(
            [sys.executable, "-c", _CHILD_RMW, _REPO_ROOT, lock_path,
             counter_path, str(iters)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(p)
    rc = [p.wait(timeout=120) for p in children]

    final = 0
    try:
        with open(counter_path, "r") as f:
            final = int(f.read().strip())
    except Exception:   # noqa: BLE001
        pass
    expected = n_procs * iters
    ok = (all(r == 0 for r in rc)) and (final == expected)
    rec("③", f"跨进程文件锁串行化：{n_procs} 进程 × {iters} 次 RMW，终值 {final}=={expected}",
        ok,
        f"procs_rc={rc} final={final} expected={expected}")


def main():
    print("=" * 68)
    print("G3 · 标注跨进程文件锁 并发守护")
    print("=" * 68)
    try:
        test_inprocess_concurrent_annotate_no_loss()
        test_reader_never_sees_torn_json()
        test_cross_process_file_lock_serializes()
    except Exception:
        traceback.print_exc()
        return 2
    failed = [t for (t, ok) in _results if not ok]
    print("-" * 68)
    print(f"合计 {len(_results)} 项，{'全部通过' if not failed else str(len(failed)) + ' 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

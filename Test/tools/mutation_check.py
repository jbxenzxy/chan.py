# -*- coding: utf-8 -*-
"""变异测试：把 G5 / G1 守护所防范的缺陷**人为塞回去**，验证守护会变红。

用法（在仓库根目录执行，或把本脚本放进仓库内任意位置）：
    python tools/mutation_check.py g5-append     # save_to_zxg_blk 退回「覆盖式写，不读现有内容」
    python tools/mutation_check.py g5-filelock   # 去掉跨进程 OS 文件锁
    python tools/mutation_check.py g1-cap        # 去掉 LRU 容量淘汰

判定：任一子项变红 = 守护有效；全绿 = 守护是摆设（该用例需要重写）。
"""
import contextlib
import importlib.util
import os
import sys


def _find_repo_root():
    """自动探测仓库根（同时含 App/AppData.py 与 Test/ 的目录）。

    找不到时用环境变量覆盖：
        set CHAN_REPO_ROOT=D:\\my_chan_project   （Windows cmd）
        export CHAN_REPO_ROOT=/path/to/chan.py   （bash）
    """
    env = os.environ.get("CHAN_REPO_ROOT")
    candidates = []
    if env:
        candidates.append(env)
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):                       # 向上找 4 层，覆盖 tools/ 与 Test/
        candidates.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    candidates.append(os.getcwd())
    for c in candidates:
        if os.path.isfile(os.path.join(c, "App", "AppData.py")) \
                and os.path.isdir(os.path.join(c, "Test")):
            return c
    raise SystemExit(
        "找不到仓库根（需同时含 App/AppData.py 与 Test/）。\n"
        "请把本脚本放进 chan.py 仓库内，或设置环境变量 CHAN_REPO_ROOT 指向仓库根。")


CWD = _find_repo_root()
sys.path.insert(0, os.path.join(CWD, "Test"))
sys.path.insert(0, CWD)
import _stub_env                                     # noqa: E402
_stub_env.install()

from App import AppData as AD                        # noqa: E402
from App.AppData import app_data                     # noqa: E402


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(CWD, "Test", path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def report(m, label):
    failed = [n for (_t, n, ok) in m._RESULTS if not ok]
    total = len(m._RESULTS)
    print(f"\n>>> 【{label}】变异后：{total - len(failed)} 通过 / {len(failed)} 失败")
    print(f">>> 判定：{'有效 —— 守护成功变红' if failed else '无效 —— 缺陷未被拦截！'}")
    return bool(failed)


mode = sys.argv[1] if len(sys.argv) > 1 else ""

if mode == "g5-append":
    # 变异：覆盖式写，不读现有内容（经典丢失更新）
    def regressed_save(codes):
        path = app_data.zxg_blk_path
        if not path:
            return 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines = [l for l in (AD._code_to_zxg_line(c) for c in codes) if l]
        with open(path, "w", encoding="gbk") as f:
            f.write("\n".join(lines) + "\n")
        return len(lines)

    app_data.save_to_zxg_blk = regressed_save
    m = load("test_zxg_write_concurrency.py", "g5")
    m.test_concurrent_append_no_lost_writes()
    sys.exit(0 if report(m, "G5① 丢失更新") else 1)

elif mode == "g5-filelock":
    # 变异：去掉跨进程 OS 文件锁（threading.Lock 管不到子进程）
    AD.file_lock = lambda *a, **k: contextlib.nullcontext()
    m = load("test_zxg_write_concurrency.py", "g5")
    m.test_cross_process_file_lock_serializes()
    sys.exit(0 if report(m, "G5④ 跨进程文件锁失效") else 1)

elif mode == "g1-cap":
    # 变异：去掉 LRU 容量淘汰
    def no_cap_put(key, value):
        with app_data._stocks_cache_lock:
            app_data._stocks_analysis_cache[key] = value

    app_data.cache_put = no_cap_put
    m = load("test_analyze_cache_concurrency.py", "g1")
    m.test_lru_cap_under_concurrency()
    sys.exit(0 if report(m, "G1③ LRU 上限失效") else 1)

else:
    print(__doc__)
    sys.exit(2)

# -*- coding: utf-8 -*-
"""低并发预抓 K 线（腾讯接口有速率限制，14 并发会被限流返回非 JSON）。"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
def _find_repo(_p):
    """向上找到仓库根（含 Chan.py + DataAPI 的那一级）—— 从任意深度都成立。"""
    for _ in range(7):
        if (os.path.isfile(os.path.join(_p, "Chan.py"))
                and os.path.isdir(os.path.join(_p, "DataAPI"))):
            return _p
        _p = os.path.dirname(_p)
    return ""


EXP = HERE if os.path.basename(HERE) == "Exp" else os.path.dirname(HERE)
# 优先级：显式 CHAN_REPO > 向上找仓库根 > 沙盒布局（<work>/wt_latest）
REPO = (os.environ.get("CHAN_REPO") or _find_repo(HERE)
        or os.path.join(os.path.dirname(os.path.dirname(EXP)), "wt_latest"))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(EXP, "common"))     # 共享层（tdx_source / exp_policy …）
sys.path.insert(0, HERE)
os.chdir(REPO)

from page_kline import fetch, stock_list, CACHE  # noqa: E402


def have(code, freq, n):
    return os.path.exists(os.path.join(CACHE, "{}_{}_{}.json".format(code, freq, n)))


def main():
    freq = sys.argv[1] if len(sys.argv) > 1 else "w"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 800
    start = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    end = int(sys.argv[4]) if len(sys.argv) > 4 else 800
    workers = int(sys.argv[5]) if len(sys.argv) > 5 else 4

    codes = stock_list(end)[start:end]
    todo = [c for c in codes if not have(c, freq, n)]
    print("待抓 {} 只（已有缓存 {}）".format(len(todo), len(codes) - len(todo)),
          flush=True)

    ok = [0]
    fail = []

    def work(c):
        for _ in range(3):
            try:
                r = fetch(c, freq, n)
                if r:
                    ok[0] += 1
                    return
            except Exception:                         # noqa: BLE001
                pass
            time.sleep(1.5)
        fail.append(c)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, todo))
    print("成功 {} / 失败 {} / 耗时 {:.1f}s".format(ok[0], len(fail), time.time() - t0))
    if fail:
        print("失败样本（前 20）:", fail[:20])
    # 二次补抓失败项（串行，慢速）
    if fail:
        print("串行补抓 {} 只...".format(len(fail)), flush=True)
        left = []
        for c in fail:
            try:
                if fetch(c, freq, n):
                    continue
            except Exception:                         # noqa: BLE001
                pass
            time.sleep(0.8)
            left.append(c)
        print("最终仍失败 {} 只".format(len(left)))
        if left:
            print(left[:30])


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""新浪**股票分钟 K 线**取数（5m / 15m / 30m / 60m）→ 与 `fetch_kline` 同构的 records。

为什么必须另开一个取数器
------------------------
腾讯前复权接口（`fetch_kline.py` 用的 `fqkline/get`）**只支持 day/week/month** ——
对 `30` / `m30` / `15` 一律返回空（已逐参数实测）。腾讯的 `kline/mkline` 能拿分钟线，
但**上限只有 320 根**（m30 仅 ~40 个交易日），长度不足以让缠论建立结构。

新浪 `CN_MarketData.getKLineData` 支持 `scale=5|15|30|60`，`datalen` 实测可到 **5000**：
    30m × 5000 ≈ 625 个交易日（约 2.5 年）
    15m × 5000 ≈ 312 个交易日
     5m × 5000 ≈ 104 个交易日

⚠ **两个必须披露的口径差异（与周/日线不可完全等同）**
  1. **不复权**：新浪分钟线是原始价，遇到除权除息会有跳空，而周/日线走的是腾讯**前复权**。
     跨越分红日的样本会有一次性价格跳变。本模块不做复权处理，只在报告里披露。
  2. **长度受限**：5m 最多 ~104 个交易日，缠论的线段/中枢级别会比周/日线浅。

用法::

    python fetch_min.py sz000158 30m 5000      # 单只
    python fetch_min.py --prefetch 300 5       # 批量预热 300 只 × 3 周期，并发 5
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

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

CACHE = os.path.join(HERE, ".kcache_min")
URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
       "CN_MarketData.getKLineData")
SCALE = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}


def fetch(code: str, freq: str = "30m", n: int = 5000, use_cache: bool = True
          ) -> list:
    """`code` 形如 sz000158；`freq` ∈ {5m,15m,30m,60m}。返回按时间升序的 records。"""
    if freq not in SCALE:
        raise ValueError("未知分钟周期 {!r}（合法：{}）".format(freq, list(SCALE)))
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, "{}_{}_{}.json".format(code, freq, n))
    if use_cache and os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)

    url = "{}?symbol={}&scale={}&ma=no&datalen={}".format(
        URL, code, SCALE[freq], n)
    arr = None
    for attempt in range(4):
        try:
            txt = requests.get(url, timeout=25).text.strip()
            m = re.search(r"(\[.*\])", txt, re.S)
            if not m:
                return []
            arr = json.loads(m.group(1))
            break
        except Exception:                                  # noqa: BLE001
            time.sleep(1.2 * (attempt + 1))
    if not arr:
        return []

    out = []
    for it in arr:
        try:
            o, h, l, c = (float(it["open"]), float(it["high"]),
                          float(it["low"]), float(it["close"]))
            v = float(it.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        d = str(it.get("day") or "")
        if not d or min(o, h, l, c) <= 0:
            continue
        out.append({"dt": d, "open": o, "close": c, "high": h, "low": l,
                    "vol": v, "amount": c * v})
    out.sort(key=lambda r: r["dt"])
    if out:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(out, f)
    return out


def prefetch(limit: int = 300, workers: int = 5):
    from fetch_kline import stock_list
    codes = stock_list(limit)
    jobs = [(c, f) for c in codes for f in ("5m", "15m", "30m")]
    # 实测新浪 `datalen` **硬上限 5001 根**（传 6000 也只回 5001）⇒ 三个周期都按上限取满
    nb = {"5m": 5000, "15m": 5000, "30m": 5000}
    print("待抓 {} 只 × 3 周期 = {} 个请求".format(len(codes), len(jobs)), flush=True)
    t0 = time.time()

    def one(cf):
        c, f = cf
        return bool(fetch(c, f, nb[f]))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(one, jobs))
    print("成功 {} / {}，耗时 {:.0f}s".format(sum(res), len(jobs),
                                          time.time() - t0), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--prefetch":
        prefetch(int(sys.argv[2]) if len(sys.argv) > 2 else 300,
                 int(sys.argv[3]) if len(sys.argv) > 3 else 5)
    else:
        code = sys.argv[1] if len(sys.argv) > 1 else "sz000158"
        freq = sys.argv[2] if len(sys.argv) > 2 else "30m"
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 5000
        r = fetch(code, freq, n)
        print(code, freq, len(r), r[0]["dt"] if r else None,
              r[-1]["dt"] if r else None)

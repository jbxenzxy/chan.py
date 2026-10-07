# -*- coding: utf-8 -*-
"""新浪期货 K 线取数（IF / IH / IC / IM 主力连续）→ 与 `fetch_kline` 同构的 records。

为什么另开一个取数器：`fetch_kline.py` 走的是腾讯**股票前复权**接口 —— 期货没有
"前复权"概念、代码也不是 6 位数字，两者不同源。且它的质量门（任一 OHLC ≤ 0 即整只
剔除）是为"过度复权出负价"设的，对期货不适用，混在一起会误伤。

周期 freq: 1m / 5m / 15m / 30m / 60m / d
  · 分钟：`getFewMinLine?symbol=IF0&type=<1|5|15|30|60>` —— **固定只返回最近 1023 根**
  · 日线：`getDailyKLine?symbol=IF0` —— 返回全部历史（IF0 ≈ 2017 至今 2356 根）

⚠ 数据是**主力连续**（`IF0` = 主力合约拼接），换月处有跳空 ⇒ 只用于"波动量级 /
   周期换算"这类统计，不做跨月或长周期持有类研究。
"""
from __future__ import annotations

import json
import os
import re

import requests
import os
import sys
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

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kcache_fut")
BASE = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/x/"
        "InnerFuturesNewService.")
MIN_TYPE = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
ALL_FREQ = list(MIN_TYPE) + ["d"]


def fetch(symbol: str = "IF0", freq: str = "1m", use_cache: bool = True) -> list:
    """取期货 K 线 → `[{dt, open, high, low, close, volume}, ...]`（按时间升序）。"""
    if freq not in ALL_FREQ:
        raise ValueError("未知周期 {!r}（合法：{}）".format(freq, ALL_FREQ))
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, "{}_{}.json".format(symbol, freq))
    if use_cache and os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)

    url = (BASE + "getDailyKLine?symbol=" + symbol if freq == "d" else
           BASE + "getFewMinLine?symbol={}&type={}".format(symbol, MIN_TYPE[freq]))
    r = requests.get(url, timeout=25)
    m = re.search(r"\((\[.*\])\)", r.text, re.S)
    if not m:
        return []
    out = []
    for it in json.loads(m.group(1)):
        d = str(it.get("d") or "")
        if not d:
            continue
        try:
            out.append({
                "dt": d if len(d) > 10 else d + " 00:00:00",
                "open": float(it["o"]), "high": float(it["h"]),
                "low": float(it["l"]), "close": float(it["c"]),
                "volume": float(it.get("v") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["dt"])
    if out:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(out, f)
    return out


def products() -> list:
    """默认标的集（主力连续）：IF 沪深300 / IH 上证50 / IC 中证500 / IM 中证1000。"""
    return ["IF0", "IH0", "IC0", "IM0"]


if __name__ == "__main__":
    for s in products():
        print(s, [len(fetch(s, f)) for f in ALL_FREQ])

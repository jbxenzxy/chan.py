# -*- coding: utf-8 -*-
"""腾讯前复权 K 线抓取（chan.py 回测 records 格式）。

接口: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=<code>,<period>,,,<n>,qfq
返回 [date, open, close, high, low, volume] —— 注意第 3 位是 close（不是 high）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

import requests as req
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

# ⚠ 必须用 ifzq.gtimg.cn（web.ifzq.gtimg.cn 在批量请求下会返回 501 限流页）
BASE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kcache")
PERIOD_MAP = {"w": "week", "d": "day", "m": "month", "30m": "30", "60m": "60"}


def _ensure_cache():
    os.makedirs(CACHE, exist_ok=True)


def fetch(code: str, freq: str = "w", n: int = 800, use_cache: bool = True
          ) -> List[Dict]:
    """code 形如 sz000158 / sh600519；freq: w / d / m。返回 records（dt 为字符串）。"""
    _ensure_cache()
    fp = os.path.join(CACHE, "{}_{}_{}.json".format(code, freq, n))
    if use_cache and os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as f:
            cached = json.load(f)
        # 已落盘的也要过质量门（旧缓存可能含过度复权的负价）
        return cached if _quality_ok(cached) else []

    period = PERIOD_MAP.get(freq, freq)
    url = "{BASE}?param={code},{period},,,{n},qfq".format(
        BASE=BASE, code=code, period=period, n=n)
    last = None
    for attempt in range(4):
        try:
            r = req.get(url, timeout=20)
            d = r.json()
            break
        except Exception as e:          # noqa: BLE001
            last = e
            time.sleep(1.2 * (attempt + 1))
    else:
        # 连续失败（退市 / 停牌 / 代理偶发）→ 返回空由上层跳过，不让单只打断批量
        return []
    if not isinstance(d, dict):
        return []

    node = (d.get("data") or {}).get(code) or {}
    rows = None
    for key in ("qfq" + period, period):
        if node.get(key):
            rows = node[key]
            break
    if not rows:
        # 退市/长期停牌/接口偶发非 JSON：整体失败会打断批量实验，这里返回空
        # 由上层跳过（样本池 4400 只，丢几只不影响分布）
        return []

    out = []
    for it in rows:
        # [date, open, close, high, low, volume]
        dt, o, c, h, l, v = it[0], it[1], it[2], it[3], it[4], it[5]
        fmt = "%Y-%m-%d %H:%M:%S" if len(dt) > 10 else "%Y-%m-%d 00:00:00"
        out.append({
            "dt": dt if len(dt) > 10 else dt + " 00:00:00",
            "open": float(o), "close": float(c),
            "high": float(h), "low": float(l),
            "vol": float(v),
            # amount 腾讯周/日线不返回；缠论与出场判定都不读它，仅部分展示路径可能用
            "amount": float(c) * float(v),
        })
    # 去重 + 按日期升序
    seen = set()
    uniq = []
    for r in out:
        if r["dt"] in seen:
            continue
        seen.add(r["dt"])
        uniq.append(r)
    uniq.sort(key=lambda r: r["dt"])
    if not _quality_ok(uniq):
        return []
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(uniq, f)
    return uniq


def _quality_ok(rows: List[Dict]) -> bool:
    """质量门：腾讯 qfq 对长期高分红股会**过度复权**出负价（实测泸州老窖 -19.02、
    山西汾酒 -15.47）。负价会让 R、ATR、收益率全部失真 ⇒ 整只剔除，
    不做截断（截断会改变缠论历史长度，等于换了一个标的）。"""
    if not rows:
        return False
    return not any(r["close"] <= 0 or r["open"] <= 0 or r["high"] <= 0
                   or r["low"] <= 0 for r in rows)


def usable(code: str, freq: str, n: int) -> bool:
    """该标的是否通过质量门（供样本池预筛，避免跑到一半才剔除）。"""
    return bool(fetch(code, freq, n))


def to_records(rows: List[Dict]) -> List[Dict]:
    """→ Backtest.Runner.load_records 的等价结构（dt 转 datetime）。"""
    from datetime import datetime
    out = []
    for r in rows:
        rr = dict(r)
        rr["dt"] = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S")
        out.append(rr)
    return out


def stock_list(limit: Optional[int] = None, shuffle_seed: int = 7) -> List[str]:
    """A 股沪深主板 + 创业板代码清单（新浪行情列表）。

    过滤：北交所(bj)、科创板(688/689)、名称含 ST/退/PT 的一律排除 —— 它们的
    波动结构与涨跌幅限制不同，混进样本会污染 R% 分布。
    返回前按固定种子打散 ⇒ 按代码顺序截取时不会只拿到同一个板块。
    """
    import random
    _ensure_cache()
    fp = os.path.join(CACHE, "_universe.json")
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as f:
            codes = json.load(f)
        return codes[:limit] if limit else codes

    codes = []
    for page in range(1, 90):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page={}&num=100&sort=symbol&asc=1&node=hs_a"
               .format(page))
        try:
            r = req.get(url, timeout=20)
            arr = r.json()
        except Exception:                             # noqa: BLE001
            break
        if not arr:
            break
        for it in arr:
            sym = str(it.get("symbol") or "")
            name = str(it.get("name") or "")
            if len(sym) != 8 or not sym[:2].lower() in ("sh", "sz"):
                continue
            num = sym[2:]
            if num[:3] in ("688", "689"):
                continue
            if num[:2] not in ("60", "00", "30"):
                continue
            if any(k in name for k in ("ST", "退", "PT", "N ", "C ")):
                continue
            codes.append(sym.lower())
        if len(arr) < 100:
            break
    codes = sorted(set(codes))
    random.Random(shuffle_seed).shuffle(codes)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(codes, f)
    return codes[:limit] if limit else codes

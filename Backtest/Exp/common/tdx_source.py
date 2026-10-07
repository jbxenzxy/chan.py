# -*- coding: utf-8 -*-
"""页面同源数据层（回测 K 线 = 页面 K 线）。

为什么必须有这一层
------------------
页面/实盘链路（`App/AppEngine._analyze_stock_internal`）读的是**通达信本地
vipdoc**，并在取数阶段做**自实现前复权**（按 xdxr 事件从最新往旧递推）：

    CTdxAPI.fetch_main_level(market, code, freq)
      → read_main_level_records
          · w   ← 由前复权**日线**合成（_resample_day_to_week）
          · 30m ← 由前复权 5m 合成（_resample_5m_to_30m）
          · 15m ← 由前复权 5m 合成（_resample_5m_to_15m）
          · d/5m ← 直接读 .day / .lc5
      → _forward_adjust(records, market, code, end_date)
      → tdx_data_context(records) → CChan(data_src="custom:TdxAPI.CTdxAPI",
                                          autype=AUTYPE.NONE)

本模块 **直接调用同一个 `CTdxAPI.fetch_main_level`**，因此：
  · 同一个数据源、同一份文件、同一个前复权实现；
  · 同样的根数与起点（vipdoc 里有多少根就是多少根，页面不会多也不会少）；
  · 喂给 CChan 的构造参数与页面逐参数一致（`autype=AUTYPE.NONE` +
    `CChanConfig()` 全默认，后者由 `Test/test_bt02_config_contract.py` 钉住）。

⚠ **不要**再用 `fetch_kline.py`（腾讯 qfq）/ `fetch_min.py`（新浪，**不复权**）
   跑结论 —— 那两条链路的 K 线序列与页面**不是同一条**，实测差异见
   `data_parity.py` / `log_data_parity.txt`：
     · 日K 根数 1393（页面）vs 800（腾讯）⇒ 实验侧少 43 笔；
     · 周K 是**合成**的（页面）vs 交易所原始周线（腾讯）⇒ 收盘价 >1% 差异占 6.2%；
     · 分钟线页面是**前复权**、新浪是**不复权**。
   这两个旧模块仅保留作外部对照，**不得**作为结论依据。

用法::

    from tdx_source import records, stock_list, prefetch
    recs = records("sz000158", "w")          # 页面同源，含前复权
    codes = stock_list(800)                  # 固定种子打散的股票池
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional

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
REPO = os.environ.get("CHAN_REPO") or next(
    (p for p in (os.path.dirname(HERE), r"D:\CChan") if os.path.isdir(os.path.join(p, "DataAPI"))),
    r"D:\CChan",
)
for _p in (REPO, HERE, os.path.dirname(HERE)):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from DataAPI.TdxAPI import CTdxAPI, set_tdx_config          # noqa: E402

# ── 通达信安装目录：与 App 层同源（AppConfig 读 .env 的 TDX_INSTALL_DIR）──
try:
    from App.AppConfig import app_config as _ac
    TDX_INSTALL = os.environ.get("TDX_INSTALL_DIR") or _ac.tdx_install_dir
    _VIPDOC = _ac.vipdoc_dir
    set_tdx_config(vipdoc_dir=_VIPDOC, forward_adjust_enabled=_ac.forward_adjust_enabled)
except Exception:                                            # noqa: BLE001
    TDX_INSTALL = os.environ.get("TDX_INSTALL_DIR", r"D:\new_tdx_hd_test")
    _VIPDOC = os.path.join(TDX_INSTALL, "vipdoc")
    set_tdx_config(vipdoc_dir=_VIPDOC, forward_adjust_enabled=True)

FREQS = ("w", "d", "30m", "15m", "5m")
CACHE = os.path.join(HERE, ".kcache_tdx")
XDXR_DIR = os.path.join(CACHE, "xdxr")

_MEMO: Dict[tuple, List[Dict]] = {}
# 股票池快照：历轮固定在 .kcache/_universe.json（与 fetch_kline.stock_list 同一份）
_UNIVERSE_CANDIDATES = (
    os.path.join(HERE, ".kcache", "_universe.json"),
    os.path.join(HERE, "_universe.json"),
)
_UNIVERSE_FILE = _UNIVERSE_CANDIDATES[0]


# ────────────────────────────────────────────────────────────
# K 线（页面同源）
# ────────────────────────────────────────────────────────────
def records(code: str, freq: str, use_cache: bool = True) -> List[Dict]:
    """页面同源 records。code 形如 `sz000158`；freq ∈ FREQS。

    返回 `[{"dt": datetime, "open","high","low","close","vol","amount"}, ...]`，
    与 `Backtest.Runner.run(records=...)` 期望的结构完全一致。
    取不到（文件缺失 / 新股不足）返回 `[]`。
    """
    key = (code, freq)
    if use_cache and key in _MEMO:
        return _MEMO[key]
    if _XDXR_MEMO is None:
        install_xdxr()          # 首次取数即装载 xdxr 快照（子进程亦自动生效）
    market, num = code[:2], code[2:]
    if market not in ("sh", "sz"):
        return []
    try:
        r = CTdxAPI.fetch_main_level(market, num, freq)
    except Exception:                                        # noqa: BLE001
        return []
    recs = r[0] if isinstance(r, tuple) else r
    recs = list(recs or [])
    if use_cache:
        _MEMO[key] = recs
    return recs


def signature(code: str, freq: str):
    """(根数, 首日, 末日) —— 用于核对"我拿到的跟页面是不是同一段"。"""
    r = records(code, freq)
    if not r:
        return (0, None, None)
    return (len(r), r[0]["dt"], r[-1]["dt"])


def usable(code: str, freq: str, min_bars: int = 20) -> bool:
    """该标的在该周期上是否有足够根数（供样本池预筛）。"""
    return len(records(code, freq)) >= min_bars


def prefetch(codes, freqs=FREQS, workers: int = 8):
    """预热（本地文件读取，无网络）。返回 (code, freq) → 根数 的统计。"""
    from concurrent.futures import ThreadPoolExecutor
    jobs = [(c, f) for c in codes for f in freqs]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda t: records(*t), jobs))
    stat = {}
    for f in freqs:
        ns = [len(records(c, f)) for c in codes]
        ok = [n for n in ns if n > 0]
        stat[f] = {"ok": len(ok), "total": len(codes),
                   "median": sorted(ok)[len(ok) // 2] if ok else 0,
                   "min": min(ok) if ok else 0, "max": max(ok) if ok else 0}
    return stat


# ────────────────────────────────────────────────────────────
# 除权除息（xdxr）快照
# ────────────────────────────────────────────────────────────
# 为什么必须做这一层：前复权靠 `get_xdxr_data(market, code)` —— 它是 **eltdx
# 网络调用**（通达信 7709 / 0x000f），且被 `ElTdxAPI._xdxr_lock` 全局串行化，
# 单只 ~370~550ms、无并行空间。4400 只 ⇒ 半小时；进程池下每个子进程都要重来一遍。
# 它又**只缓存非空结果**（无分红股每次都会重新请求并超时）。
#
# 处理方式：一次性把「当前时点 eltdx 会返回的答案」抓下来冻结到磁盘，之后所有
# 进程从快照读。语义 = 与冻结的 K 线文件（vipdoc，末日 2026-09-30）配对的一份
# **数据快照**，与页面"此刻去问 eltdx"等价（差异仅在抓取后再发生的新除权事件）。
_XDXR_MEMO: Optional[Dict[str, object]] = None


def _xdxr_path(code: str) -> str:
    return os.path.join(XDXR_DIR, code + ".pkl")


def _xdxr_batch(codes):
    """在一个独立进程里抓一批并落盘。返回 (有分红数, 总数)。"""
    import pickle
    from DataAPI.ElTdxAPI import get_xdxr_data
    hit = 0
    for c in codes:
        try:
            df = get_xdxr_data(c[:2], c[2:])
        except Exception:                                    # noqa: BLE001
            df = None
        if df is not None and len(df) > 0:
            hit += 1
        try:
            with open(_xdxr_path(c), "wb") as f:
                pickle.dump(df, f)
        except Exception:                                    # noqa: BLE001
            pass
    return hit, len(codes)


def prefetch_xdxr(codes, force: bool = False, chunk: int = 50, timeout: int = 180,
                  every: int = 100):
    """抓 xdxr 快照到磁盘。

    ⚠ 底层是 eltdx **网络**调用（通达信 7709），`_xdxr_lock` 全局串行 ⇒ 并行无收益；
      且实测**偶发无限挂起**（无超时兜底）。故每 `chunk` 只交给一个独立子进程抓，
      子进程超时即被杀、自动降级为逐只重试 —— 已经落盘的文件跳过，整体可**断点续跑**。
    """
    import subprocess
    import time
    os.makedirs(XDXR_DIR, exist_ok=True)
    todo = [c for c in codes if force or not os.path.exists(_xdxr_path(c))]
    script = os.path.abspath(__file__)
    t0 = time.time()
    hit = 0
    i = 0
    while i < len(todo):
        batch = todo[i:i + chunk]
        try:
            p = subprocess.run([sys.executable, script, "--_xdxr_batch", ",".join(batch)],
                               capture_output=True, timeout=timeout)
            ok = p.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        if not ok:
            # 降级：逐只重试，每只单独设超时（挂起的那只会被跳过）
            for c in batch:
                if os.path.exists(_xdxr_path(c)):
                    continue
                try:
                    subprocess.run([sys.executable, script, "--_xdxr_batch", c],
                                   capture_output=True, timeout=20)
                except subprocess.TimeoutExpired:
                    with open(_xdxr_path(c), "wb") as f:
                        import pickle
                        pickle.dump(None, f)     # 记为「无数据」，避免下次再挂在这只上
        i += len(batch)
        if every and (i % every < chunk or i >= len(todo)):
            # 只在打点时点数（O(n²) 的 pickle 全量重读放到这里，别每批都做）
            hit = sum(1 for c in todo[:i]
                      if os.path.exists(_xdxr_path(c)) and _xdxr_nonempty(_xdxr_path(c)))
            el = time.time() - t0
            print("  xdxr %4d/%4d  有分红 %d  已用 %.0fs  预计剩余 %.0fs"
                  % (i, len(todo), hit, el, el / max(i, 1) * (len(todo) - i)), flush=True)
        else:
            hit = 0
    return {"done": len(todo), "with_xdxr": hit, "seconds": time.time() - t0}


def _xdxr_nonempty(path):
    import pickle
    try:
        with open(path, "rb") as f:
            d = pickle.load(f)
        return d is not None and len(d) > 0
    except Exception:                                        # noqa: BLE001
        return False


def install_xdxr(codes=None, verbose: bool = False) -> int:
    """把磁盘 xdxr 快照装进取数链，返回装载条数。

    直接替换 `DataAPI.TdxAPI.get_xdxr_data`（该名字在 `TdxAPI.py:765` 被
    `from ... import get_xdxr_data` **绑定**成模块属性，故必须替换这个属性，
    改 `ElTdxAPI` 侧无效）。
    """
    global _XDXR_MEMO
    import pickle
    from DataAPI import TdxAPI
    if _XDXR_MEMO is None:
        memo: Dict[str, object] = {}
        if os.path.isdir(XDXR_DIR):
            for fn in os.listdir(XDXR_DIR):
                if not fn.endswith(".pkl"):
                    continue
                code = fn[:-4]
                if codes is not None and code not in codes:
                    continue
                try:
                    with open(os.path.join(XDXR_DIR, fn), "rb") as f:
                        memo[code] = pickle.load(f)
                except Exception:                            # noqa: BLE001
                    continue
        # 快照为空 ⇒ 前复权会被**静默跳过**，回测结果看起来正常但是错的
        # （没有除权事件 = 长期高分红股的价格序列整个变样）。必须吵出来。
        if not memo:
            sys.stderr.write(
                "\n[tdx_source] ⚠ xdxr 快照为空（%s 不存在或没有 .pkl）——\n"
                "            前复权将被【静默跳过】，回测结果会与页面不一致！\n"
                "            请先跑一次：python %s --xdxr 900\n\n"
                % (XDXR_DIR, os.path.relpath(__file__))
            )
            sys.stderr.flush()
        _XDXR_MEMO = memo

    def _snapshot(market, code):
        key = market + code if len(code) == 6 else code
        df = _XDXR_MEMO.get(key)
        if df is None:
            return None
        return df.copy() if hasattr(df, "copy") else df

    TdxAPI.get_xdxr_data = _snapshot
    if verbose:
        n_with = sum(1 for v in _XDXR_MEMO.values() if v is not None and len(v) > 0)
        print("  [xdxr] 快照装载 %d 只（其中有除权除息记录 %d 只）"
              % (len(_XDXR_MEMO), n_with), flush=True)
    return len(_XDXR_MEMO)


# ────────────────────────────────────────────────────────────
# 股票池
# ────────────────────────────────────────────────────────────
def stock_list(limit: Optional[int] = None, shuffle_seed: int = 7,
               require_freq: str = "d", min_bars: int = 0) -> List[str]:
    """A 股股票池（与历史各轮一致：固定种子打散，剔除北交所/科创板/ST）。

    优先复用 `_universe.json`（历轮同一份、可比），再按 vipdoc 覆盖情况过滤；
    缺失时从 vipdoc 重新构建同口径清单。`require_freq/min_bars` 用于进一步
    只保留在指定周期上有足够根数的标的。
    """
    codes = _load_universe()
    codes = [c for c in codes
             if os.path.exists(os.path.join(_VIPDOC, c[:2], "lday", "{}.day".format(c)))]
    if require_freq and min_bars:
        codes = [c for c in codes if len(records(c, require_freq)) >= min_bars]
    return codes[:limit] if limit else codes


def _load_universe() -> List[str]:
    for p in _UNIVERSE_CANDIDATES:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return list(json.load(f))
    return _build_universe()


def _build_universe() -> List[str]:
    """从 vipdoc 重建同口径清单（无 `_universe.json` 时的兜底）。"""
    import random
    from DataAPI.TdxAPI import collect_codes_from_vipdoc
    names = {}
    nfp = os.path.join(REPO, "App", "stock_names.json")
    if os.path.exists(nfp):
        with open(nfp, "r", encoding="utf-8") as f:
            names = json.load(f)
    out = []
    for k in collect_codes_from_vipdoc(_VIPDOC):
        mkt, code = k[:2], k[2:]
        if mkt not in ("sh", "sz"):
            continue
        if mkt == "sh" and code[:3] in ("688", "689"):       # 科创板
            continue
        if mkt == "sh" and code[0] != "6":                   # 指数 / ETF / 债
            continue
        if mkt == "sz" and code[:3] not in ("000", "001", "002", "003", "300", "301"):
            continue
        nm = (names.get(k) or {}).get("name", "")
        if any(t in nm for t in ("ST", "退", "PT")):
            continue
        out.append(k)
    out.sort()
    random.Random(7).shuffle(out)
    return out


def write_universe(out_path: Optional[str] = None):
    """把当前池快照落盘（复现凭据）。"""
    codes = _build_universe()
    p = out_path or _UNIVERSE_FILE
    with open(p, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False)
    return p, len(codes)


# ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="sz000158")
    ap.add_argument("--list", type=int, default=0, help="打印前 N 只股票池")
    ap.add_argument("--coverage", type=int, default=200, help="抽样检查覆盖情况")
    ap.add_argument("--xdxr", type=int, default=0, help="为前 N 只抓 xdxr 快照（一次性，断点续跑）")
    ap.add_argument("--_xdxr_batch", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a._xdxr_batch:
        hit, n = _xdxr_batch([c.strip() for c in a._xdxr_batch.split(",") if c.strip()])
        print("%d/%d" % (hit, n))
        raise SystemExit(0)

    print("TDX_INSTALL =", TDX_INSTALL)
    print("vipdoc      =", _VIPDOC)
    print()
    if a.xdxr:
        cs = stock_list()[:a.xdxr]
        print("== 抓 xdxr 快照（%d 只，串行）==" % len(cs))
        r = prefetch_xdxr(cs)
        print("   完成 %d 只（有分红 %d），耗时 %.0fs"
              % (r["done"], r["with_xdxr"], r["seconds"]))
        raise SystemExit(0)
    install_xdxr(verbose=True)
    print()
    print("== 单只签名 (%s) —— 与页面同一段数据 ==" % a.code)
    for f in FREQS:
        n, d0, d1 = signature(a.code, f)
        print("   %-4s 根数 %6d   %s ~ %s" % (f, n, d0, d1))
    if a.list:
        cs = stock_list()
        print("\n== 股票池（前 %d 只 / 共 %d 只）==" % (a.list, len(cs)))
        print("  ", " ".join(cs[:a.list]))
    if a.coverage:
        cs = stock_list()[:a.coverage]
        st = prefetch(cs, FREQS)
        print("\n== 覆盖情况（抽 %d 只）==" % len(cs))
        print("   %-5s %-14s %-10s %s" % ("周期", "有效/总数", "根数中位", "[最小, 最大]"))
        for f in FREQS:
            s = st[f]
            print("   %-5s %-14s %-10d [%d, %d]"
                  % (f, "%d/%d" % (s["ok"], s["total"]), s["median"], s["min"], s["max"]))

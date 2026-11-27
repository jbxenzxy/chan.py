# -*- coding: utf-8 -*-
"""
App/AppEnergy.py —— 预测量能功能域（沪深京盘中预测量能）
=========================================================================
点击顶栏「预测量能」按钮后的操作：按「历史同时段累计占比」推算当日
沪深京三市全日成交额，并给出从开盘到当前时刻的预测曲线。

与「市场量能」（AppAMO，读本地 .day 日线，只看日频历史）的区别：
本模块是**盘中实时**口径，数据全部来自 eltdx 7709 行情通道。

数据源（在线，无本地兜底）：
  - eltdx TdxClient（项目已要求 eltdx>=3.3.2，与 AppMargin 同一条依赖）
  - 实时累计成交额：quotes.get_snapshots(["sh000001","sz399001","bj899050"])
    三只指数的 QuoteSnapshot.amount（元）相加 = 沪深京合计成交额。
  - 当日/历史分钟成交额：bars.get(code, period="1m", count=..., all_pages=True)
    1 分钟 K 线自带 amount 字段（元），每日固定 240 根
    （09:31..11:30 + 13:01..15:00）。
    注意 all_pages=True 时 count 不生效（eltdx 只校验 page_size，逐页拉到服务端
    空页为止）：实测单只 23280 根 / 97 个交易日 / 约 3~4s。样本不是「2400 根」，
    而是由 _RATIO_DAYS=20 从这堆全量里截最近 20 个完整交易日。

三市口径（2026-11-26 与开盘红 App 逐位对齐，见 Docs/预测量能.md）：
  - 沪市 = sh000001（上证指数，amount = 沪市成交额）
  - 深市 = sz399001（深证成指，amount = 深市成交额；与深证综指 sz399106 同值）
  - 北交所 = bj899050（北证50，仅覆盖 50 只，占全市场 <1%，按近似处理）
  实测 2026-11-26 11:30：3687.19 + 4275.78 + 54.83 = 8017.80 亿。

预测模型（r 表法）：
    P(t) = C(t) / r(t)
    C(t) = 当日截至第 t 根 bar 的累计成交额
    r(t) = 过去 N 个交易日「同 bar 序号累计 / 全日总量」的加权均值
  bar 序号而非时间标签对齐：盘中 11:30 那根 bar 服务端会标成 13:00，
  按时间对齐会整体错位；按序号 0..239 对齐天然免疫。
  权重 = 0.5 ** ((N-1-i)/halflife)，越近权重越大（默认 N=20、halflife=10）。
  回测（2026-10-15~11-25，30 个交易日，r 表用前 20 日）：11:30 MAPE 5.7%、
  14:00 3.3%、收盘 0.1%。

基准（面板「昨日|总量能」）= **展示日的前一个交易日**，取自与曲线同一次分钟线
拉取（`_fetch_today_minutes` 的 520 根窗口装得下「当日 + 上一交易日」）。
不取 r 表里的日总量：那是「建表那一刻」的快照，收盘后首次重建落在 15:00:13，
服务端那时还没发布当日 15:00 那根（收盘集合竞价），当日的合计会少一根。

r 表缓存：落 {app_data_dir}/energy_ratio.json，每天收盘后首次请求重建一次
（重建走全量翻页：三只各约 3~4s、合计约 10s，不能每请求都拉）。
实时快照 2s TTL、当日分钟线 60s TTL，避免前端轮询打穿行情主站。

锁：本模块**无锁**。只读在线行情 + 算统计，不进 CChan 引擎、不写共享状态；
缓存是模块级 dict 的键级赋值 / 整体替换（GIL 下单键写原子），不做「读-改-写」
序列、也不原地 append —— 读方最坏读到「新 cum 配旧 ts」，多拉一次行情。
"""
import json
import os
import threading
import time
from datetime import datetime

# 配置中心（缓存目录单一事实源）
from App.AppConfig import app_config
# 领域异常（数据源失败 → DataFetchError）
from App.AppErrors import DataFetchError
from App.AppLog import get_logger

log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════════════

# 沪深京三市：沪市 / 深市 / 北交所（顺序即面板「实际量能」的相加顺序）
_CODES = ("sh000001", "sz399001", "bj899050")

# 每交易日固定 240 根 1 分钟 K 线（09:31..11:30 + 13:01..15:00）
_BARS_PER_DAY = 240

# 预测模型参数（见 Docs/预测量能.md 的敏感性扫描：N=20/halflife=10 最优）
_RATIO_DAYS = 20
_RATIO_HALFLIFE = 10.0

# eltdx 连接超时（秒）。主站握手 + 一条请求的保守上限
_TDX_TIMEOUT_SEC = 8.0

# 行情主站快照实测约 1.3 秒刷新一次（2026-11-26 13:00 盘中 60s 窗口、0.45s
# 轮询分辨率下检出 45 次变化，间隔中位 1.37s）。TTL 取 2s：既贴身跟随主站，
# 又把「多标签页 / 多客户端同时轮询」合并成每 2 秒最多一次上游请求。
_SNAP_TTL_SEC = 2.0
# 当日分钟线 60s TTL：曲线是分钟粒度，一分钟内不会变；拉一次要 3 次
# bars 调用（≈3s），不能跟着前端 3s 轮询走。
# 注意末点不吃这个 TTL —— call_energy 会用实时快照覆盖 cum[-1]，
# 所以曲线最右端仍然每 2 秒动一次，只是横轴位置一分钟才右移一格。
_MINUTE_TTL_SEC = 60.0

# 拉分钟线的根数：240 当日 + 240 上一交易日 + 余量。**必须装得下两天**：
# 「昨日总量能」直接从这条窗口里取（见 `_fetch_today_minutes`），窗口只够一天
# 就只能回头去求 r 表的快照值 —— 那正是 2026-11-26 把 13261.46 亿记成
# 13104.88 亿的来路。实测 520 根 = 11-25 240 根 + 11-26 240 根 + 今日 1 根，
# 三只合计 0.17s（与 320 根的耗时无差别，都是一次请求）。
_MINUTE_COUNT = 520

# 「昨日总量能」的完整性以沪深两市为准（北交所 bj899050 只覆盖 50 只、
# 占全市场 <1%，缺日按 0 计 —— 与当日曲线的合并口径一致）
_BASIS_CODES = _CODES[:2]

# 交易时段（本地时间，含边界）
_AM_OPEN, _AM_CLOSE = (9, 30), (11, 30)
_PM_OPEN, _PM_CLOSE = (13, 0), (15, 0)

# 缓存文件版本号：结构变更时自增，旧缓存自动失效重建
# v2：删掉「表内记昨日总量」（yesterday_total / prev_total），基准改由实时分钟线
#     窗口提供 —— v1 的表会把 2026-11-26 记成 13104.88 亿（少一根 15:00）
_CACHE_VERSION = 2

# 模块级缓存（键级赋值 / 整体替换，无锁）
_RATIO_CACHE = None          # {"trade_date","bars","samples","built_at"}
_SNAP_CACHE = {"ts": 0.0, "amount": 0.0}
_MINUTE_CACHE = {"ts": 0.0, "day": "", "prev_day": "", "prev_total": 0.0, "cum": []}


# ═══════════════════════════════════════════════════════════════════════
# 数据源：eltdx 取数
# ═══════════════════════════════════════════════════════════════════════

def _new_client():
    from eltdx import TdxClient
    return TdxClient(timeout=_TDX_TIMEOUT_SEC)


# 线程内常驻连接（thread-local，不跨线程共享 ⇒ 无需锁）。
# 为什么必须复用：eltdx 首次请求要建 TCP + 7709 握手，实测 3.2s；握手完成后
# 单次快照只要 0.05s。若每个请求都新建，前端 3s 轮询会退化成「每次 3 秒」，
# 还会反复占用主站握手名额。thread-local 让每个线程各持一条，既无共享状态，
# 又不用把连接生命周期挂到调用方身上。
_LOCAL = threading.local()


def _client():
    cli = getattr(_LOCAL, "cli", None)
    if cli is None:
        cli = _new_client()
        _LOCAL.cli = cli
    return cli


def _drop_client():
    cli = getattr(_LOCAL, "cli", None)
    _LOCAL.cli = None
    if cli is not None:
        try:
            cli.close()
        except Exception:      # noqa: BLE001 —— 关闭失败不影响重建
            pass


def _call(fn):
    """在当前线程的常驻连接上执行 fn(client)；连接失效时重建一次重试。"""
    try:
        return fn(_client())
    except Exception as exc:   # noqa: BLE001 —— 主站断连/超时统一在此重建
        log.warning(f"[预测量能] 行情连接异常，重建后重试: {exc}")
        _drop_client()
        return fn(_client())


def _fetch_minute_days(client, code: str) -> dict:
    """拉某标的的 1 分钟 K 线，按交易日切分 → {日期: [每分钟成交额(元), ...]}

    all_pages=True 时 count 不生效（eltdx 只校验 page_size，逐页拉到服务端返回
    空页为止），传 2400 只是表达「要分钟线」的意图，实收是全量：实测单只 23280
    根 / 97 个交易日。样本由 _build_ratio 截最近 _RATIO_DAYS=20 个完整交易日。

    ⚠️ 返回的日数组是**变长**的（每日本身有多少根就多少根，只封顶 240），
    且收盘那一刻最后一根会先带一个几乎为 0 的占位值（见 `_build_ratio`），
    所以「当日能不能进样本」由 `_build_ratio` 的日历条件决定，不看这里的形状。
    """
    series = client.bars.get(code, period="1m", count=2400, all_pages=True)
    days = {}
    for bar in series.bars:
        day = bar.time.strftime("%Y-%m-%d")
        arr = days.setdefault(day, [])
        if len(arr) < _BARS_PER_DAY:
            arr.append(float(bar.amount))
    return days


def _fetch_snapshot_total(client) -> float:
    """三市实时累计成交额合计（元）"""
    quotes = client.quotes.get_snapshots(list(_CODES))
    total = 0.0
    for q in quotes:
        total += float(q.amount)
    return total


def _fetch_today_minutes(client) -> tuple:
    """当日（或最近交易日）三市分钟成交额

    返回 (日期, [累计成交额(元), ...], bar 数, 上一交易日, 上一交易日总量(元))

    注意 count 是**跨日总根数**，必须按日期切出最后一天；直接累加会把昨日
    尾巴算进来（实测会把 8017 亿做成 13591 亿）。

    上一交易日与当日出自**同一次拉取、同一条窗口**（`_MINUTE_COUNT` 装得下两天），
    所以「昨日总量能」恒等于展示日的前一个交易日，与 r 表何时重建无关。
    上一交易日总量在沪深两市都满 240 根时才认，否则回 0（调用方据此报错），
    绝不拿一条残缺的日线去当基准。
    """
    per_code = {}
    last_day = ""
    for code in _CODES:
        series = client.bars.get(code, period="1m", count=_MINUTE_COUNT)
        if not series.bars:
            raise DataFetchError(f"eltdx 未返回分钟线: {code}")
        days = {}
        for b in series.bars:
            days.setdefault(b.time.strftime("%Y-%m-%d"), []).append(float(b.amount))
        day = series.bars[-1].time.strftime("%Y-%m-%d")
        last_day = max(last_day, day)
        per_code[code] = days

    # 上一交易日 = 窗口里早于 last_day 的最大日期（取 max 而非 min：窗口最左
    # 那天常被截断，比如 11-24 只有尾巴 39 根）
    prev_day = max((d for days in per_code.values() for d in days if d < last_day),
                   default="")

    # 只用「三市共同的最新交易日」；个别市场缺当日数据时按 0 计（北交所 <1%）
    merged = [0.0] * _BARS_PER_DAY
    bars_seen = 0
    for code, days in per_code.items():
        arr = days.get(last_day)
        if arr is None:
            log.warning(f"[预测量能] {code} 无 {last_day} 分钟线，按 0 计")
            continue
        bars_seen = max(bars_seen, min(len(arr), _BARS_PER_DAY))
        for i, amt in enumerate(arr[:_BARS_PER_DAY]):
            merged[i] += amt

    cum = []
    acc = 0.0
    for i in range(bars_seen):
        acc += merged[i]
        cum.append(acc)

    prev_total = 0.0
    if prev_day:
        complete = True
        for code, days in per_code.items():
            arr = days.get(prev_day) or []
            if code in _BASIS_CODES and len(arr) < _BARS_PER_DAY:
                complete = False
            prev_total += sum(arr)
        if not complete:
            log.warning(f"[预测量能] 上一交易日 {prev_day} 分钟线不足 "
                        f"{_BARS_PER_DAY} 根，基准不可用")
            prev_total = 0.0
    return last_day, cum, bars_seen, prev_day, prev_total


# ═══════════════════════════════════════════════════════════════════════
# r(t) 表：构建 / 缓存
# ═══════════════════════════════════════════════════════════════════════

def _build_ratio(client) -> dict:
    """拉历史分钟线构建 r(t) 表（最近 _RATIO_DAYS 个完整交易日）"""
    per_day = {}
    for code in _CODES:
        for day, arr in _fetch_minute_days(client, code).items():
            agg = per_day.get(day)
            if agg is None:
                agg = [0.0] * _BARS_PER_DAY
                per_day[day] = agg
            for i, amt in enumerate(arr):
                agg[i] += amt

    # 完整交易日：① **当日一律不进样本** —— 收盘后首次重建落在 15:00:13，服务端
    # 那时才吐出 15:00 那根，里面只有收盘竞价的前几秒（2026-11-26 实测那一刻合计
    # 0.08 亿，回填后 156.67 亿）；它根数够、末格也有量，任何「结构完整性」判据都
    # 看不出来，只有「今天是哪天」这个日历条件挡得住。② 首尾都有量（剔除半天/
    # 停市/残缺日：per_day 的数组是 240 格定长，残缺日的尾格是 0）。
    today = datetime.now().strftime("%Y-%m-%d")
    full = {d: v for d, v in per_day.items()
            if d < today and v[0] > 0 and v[-1] > 0}
    hist = sorted(full)[-_RATIO_DAYS:]
    if len(hist) < 5:
        raise DataFetchError(f"eltdx 可用完整交易日不足（{len(hist)} 天），无法构建 r(t) 表")

    cum_of, tot_of = {}, {}
    for day in hist:
        acc, c = 0.0, []
        for amt in full[day]:
            acc += amt
            c.append(acc)
        cum_of[day] = c
        tot_of[day] = acc

    weights = [0.5 ** ((len(hist) - 1 - i) / _RATIO_HALFLIFE) for i in range(len(hist))]
    wsum = sum(weights)
    bars = []
    for k in range(_BARS_PER_DAY):
        num = 0.0
        for w, day in zip(weights, hist):
            num += w * (cum_of[day][k] / tot_of[day])
        bars.append(round(num / wsum, 8))

    return {
        "version": _CACHE_VERSION,
        "trade_date": hist[-1],                  # r 表的样本末日（最近一个完整交易日）
        "bars": bars,
        "samples": len(hist),
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _ratio_file() -> str:
    return app_config.energy_ratio_file


def _load_ratio_disk():
    path = _ratio_file()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning(f"[预测量能] r(t) 缓存读取失败，将重建: {path} ({exc})")
        return None
    if (data.get("version") != _CACHE_VERSION
            or len(data.get("bars") or []) != _BARS_PER_DAY
            or not data.get("trade_date")):
        log.info("[预测量能] r(t) 缓存结构过期，将重建")
        return None
    return data


def _save_ratio_disk(data: dict) -> None:
    path = _ratio_file()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        # 落盘失败不影响本次预测，下次再试
        log.warning(f"[预测量能] r(t) 缓存写入失败: {path} ({exc})")


def _is_stale(data: dict) -> bool:
    """r(t) 缓存是否该重建

    判据：样本末日落后于「最近一个已收盘交易日」才重建。周末与法定假日行情源
    不产生新分钟线，重建也推不动样本末日；若没有「今天收盘后已试过」这层兜底，
    同一个缓存会一直被判定过期 —— 15:00 之后每个请求都重拉全量分钟线（三只各
    约 3~4s），长假里天天如此。所以用 built_at 记一笔：今天收盘后已经建过就
    不再试。反向条件同样重要：built_at 是今天**收盘前**的（例如盘前冷启动建过
    一次），15:00 后仍要重建一次，好把**昨日**的完整数据收进样本
    （当日永不进样本，见 `_build_ratio`）。
    """
    trade_date = data.get("trade_date") or ""
    if not trade_date:
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    if trade_date > today:
        return True                      # 脏数据 / 系统时钟回拨
    if trade_date >= today:
        return False                     # 当日不进样本，正常走不到这里；留作防御
    if not _now_after_close():
        return False                     # 今天还没收盘，今天的数据不该进样本
    # 15:00 之后样本末日仍落后：今天可能不是交易日（周末/法定假日），也可能
    # 行情源还没更新。built_at 是「今天且已过收盘」⇒ 已经试过，别反复拉。
    built = data.get("built_at") or ""
    if built[:10] != today:
        return True
    return built[11:16] < "%02d:%02d" % _PM_CLOSE


def _now_after_close() -> bool:
    now = datetime.now()
    return (now.hour, now.minute) >= _PM_CLOSE


def _get_ratio() -> dict:
    """取 r(t) 表（内存 → 磁盘 → 重建）"""
    global _RATIO_CACHE
    if _RATIO_CACHE is not None and not _is_stale(_RATIO_CACHE):
        return _RATIO_CACHE

    data = None if _RATIO_CACHE and _is_stale(_RATIO_CACHE) else _load_ratio_disk()
    if data is not None and not _is_stale(data):
        _RATIO_CACHE = data
        return data

    t0 = time.time()
    data = _call(_build_ratio)
    _save_ratio_disk(data)
    _RATIO_CACHE = data
    log.info(f"[预测量能] r(t) 表重建完成: 样本 {data['samples']} 天 "
             f"({data['trade_date']} 止) 耗时 {time.time() - t0:.2f}s")
    return data


# ═══════════════════════════════════════════════════════════════════════
# 实时快照 / 当日曲线（带 TTL）
# ═══════════════════════════════════════════════════════════════════════

def _live() -> dict:
    """实时取数（单连接内按各自 TTL 决定要不要真拉）

    曲线（3 次 bars）与快照（1 次 snapshots）的 TTL 不同：曲线 60s、快照 2s。
    两者都没过期时不建连接；只有一个过期时共用同一条连接，省一次握手。
    返回 {"day", "cum": [...], "snap": 三市合计(元), "prev_day", "prev_total"}
    其中 prev_day/prev_total 是「展示日的前一个交易日」及其总量 —— 基准与曲线
    同源，两者一起进 60s 缓存，不会出现「曲线换了日、基准还停在上一版」。
    """
    now = time.time()
    minute_fresh = bool(_MINUTE_CACHE["cum"]) and (now - _MINUTE_CACHE["ts"] < _MINUTE_TTL_SEC)
    snap_fresh = _SNAP_CACHE["amount"] > 0 and (now - _SNAP_CACHE["ts"] < _SNAP_TTL_SEC)

    if not (minute_fresh and snap_fresh):
        def _pull(client):
            if not minute_fresh:
                day, cum, _, prev_day, prev_total = _fetch_today_minutes(client)
                _MINUTE_CACHE["day"] = day
                _MINUTE_CACHE["cum"] = cum
                _MINUTE_CACHE["prev_day"] = prev_day
                _MINUTE_CACHE["prev_total"] = prev_total
                _MINUTE_CACHE["ts"] = time.time()
            if not snap_fresh:
                _SNAP_CACHE["amount"] = _fetch_snapshot_total(client)
                _SNAP_CACHE["ts"] = time.time()
        _call(_pull)

    return {
        "day": _MINUTE_CACHE["day"],
        "cum": _MINUTE_CACHE["cum"],
        "snap": _SNAP_CACHE["amount"],
        "prev_day": _MINUTE_CACHE["prev_day"],
        "prev_total": _MINUTE_CACHE["prev_total"],
    }


# ═══════════════════════════════════════════════════════════════════════
# 预测与对外结构
# ═══════════════════════════════════════════════════════════════════════

def _predict(ratio_bars, cum_yuan: float, k: int):
    """第 k 根 bar（1-based）的预测量能（元）；r 越界返回 None"""
    if k <= 0 or k > len(ratio_bars):
        return None
    r = ratio_bars[k - 1]
    if r <= 0:
        return None
    return cum_yuan / r


def _session_status(day: str) -> str:
    """pre（盘前）/ trading（连续竞价）/ break（午休）/ closed（收盘或非交易日）"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if day != today:
        return "closed"
    hm = (now.hour, now.minute)
    if hm < _AM_OPEN:
        return "pre"
    if hm <= _AM_CLOSE or _PM_OPEN <= hm <= _PM_CLOSE:
        return "trading"
    if _AM_CLOSE < hm < _PM_OPEN:
        return "break"
    return "closed"


def _yi(yuan: float) -> float:
    """元 → 亿元（保留 2 位小数）"""
    return round(yuan / 1e8, 2)


def call_energy():
    """预测量能数据（同步入口，REST 路由经 run_in_threadpool 调用）

    返回结构（全部为「亿元」，除 pct）：
      {
        "date": "11-26",            # 面板标题用（MM-DD）
        "trade_date": "2026-11-26",
        "status": "trading|break|closed|pre",
        "actual": 8017.80,          # 实际量能
        "predict": 13140,           # 预测量能（整数）
        "yesterday": 13928.30,      # 昨日总量能（= 展示日的前一个交易日）
        "pct": -5.65,               # 较昨日（%，两位小数）
        "delta": -787,              # 增量(+) / 缩量(-)，整数亿
        "shrink": True,             # 是否缩量（前端配色用）
        "bars_done": 120,           # 已走完的分钟 bar 数
        "points": [[k, pct], ...],  # 曲线：bar 序号 → 较昨日 %
        "marks": [{"k":0,"label":"09:30"}, ...]   # 横轴刻度
      }

    盘前（当日还没有成交量）时 bars_done=0、points=[]、predict=0、pct=0.0、
    delta=0 —— 只把「昨日总量能」报准，不吐 -100% 这种假信号。
    """
    t0 = time.time()
    ratio = _get_ratio()
    ratio_bars = ratio["bars"]

    curve = _live()
    day, cum = curve["day"], curve["cum"]
    if not cum:
        raise DataFetchError("eltdx 未返回当日分钟线（行情主站不可用）")

    # 基准 = 展示日的前一个交易日，与曲线出自同一次分钟线拉取（`_live` 的 60s 缓存）。
    # **不读 r 表里的日总量**：那是「建表那一刻」的快照，收盘后首次重建落在 15:00:13，
    # 服务端那时还没发布当日 15:00 那根（收盘集合竞价），当日合计会少一根；
    # 且当天不会再次重建（`_is_stale` 首判 trade_date >= today），错值一直冻到次日。
    yesterday = float(curve.get("prev_total") or 0.0)
    if yesterday <= 0:
        raise DataFetchError("未取到上一交易日总量（分钟线窗口不足两个交易日）")

    # 当日累计：末尾用实时快照覆盖（快照比分钟线新一拍）
    snap_total = curve["snap"]
    is_today = (day == datetime.now().strftime("%Y-%m-%d"))
    if is_today and snap_total > cum[-1]:
        cum = cum[:-1] + [snap_total]

    bars_done = len(cum)
    actual = cum[-1]
    if actual <= 0:
        # 盘前：服务端已经吐出当日 09:31 那根占位 bar（amount=0），照常换算会得到
        # 「预测 0 亿 / -100.00% / 缩量 yesterday 亿」纯噪声 —— 盘前只报基准。
        bars_done = 0
        predict, pct, delta, shrink = 0, 0.0, 0, False
    else:
        predict = _predict(ratio_bars, actual, bars_done) or actual

        # 走完 240 根即收盘，预测值直接取实际值。r 表末根其实恒等于 1（_build_ratio
        # 的分母就是当日 cum[-1]，末根分子也是 cum[-1]），所以这一步通常等于没做；
        # 保留它是兜底：r 表被外部改坏 / 缓存被手改成末根 < 1 时，末点会冒出虚假正偏差。
        if bars_done >= _BARS_PER_DAY:
            predict = actual

        pct = round((predict / yesterday - 1.0) * 100.0, 2)
        delta = int(round((predict - yesterday) / 1e8))
        shrink = predict < yesterday

    points = []
    for k in range(1, bars_done + 1):
        p = _predict(ratio_bars, cum[k - 1], k)
        if p is None:
            continue
        points.append([k, round((p / yesterday - 1.0) * 100.0, 2)])

    result = {
        "date": day[5:],
        "trade_date": day,
        "status": _session_status(day),
        "actual": _yi(actual),
        "predict": int(round(_yi(predict))),
        "yesterday": _yi(yesterday),
        "pct": pct,
        "delta": delta,
        "shrink": shrink,
        "bars_done": bars_done,
        "points": points,
        "marks": [
            {"k": 0, "label": "09:30"},
            {"k": 60, "label": "10:30"},
            {"k": 120, "label": "11:30"},
            {"k": 180, "label": "14:00"},
            {"k": 240, "label": "15:00"},
        ],
    }
    log.debug(f"[api] /api/energy/read 完成: {day} bar={bars_done} "
              f"实际={result['actual']}亿 预测={result['predict']}亿 "
              f"耗时 {time.time() - t0:.2f}s")
    return result


def clear_cache() -> None:
    """清空进程内缓存（当前无调用方，保留作手动刷新 / 排障入口；不动磁盘）

    全仓无人调用、测试也是直接 mock _get_ratio/_live，评审记为清洁度项。保留的
    理由是它只做键级复位、无副作用，出问题时能一行把进程拉回「冷启动」语义。
    """
    global _RATIO_CACHE
    _RATIO_CACHE = None
    _SNAP_CACHE.update({"ts": 0.0, "amount": 0.0})
    _MINUTE_CACHE.update({"ts": 0.0, "day": "", "prev_day": "", "prev_total": 0.0, "cum": []})

"""
eltdx 数据源适配器（通达信网络行情客户端封装）。

定位：本模块封装 eltdx 客户端这一个通达信网络数据源，对外暴露 eltdx 的
三类取数能力（均**仅覆盖 A 股**）：
  1. 除权除息（XDXR）——0x000f 事件表，供 TdxAPI 前复权流水线消费；
  2. PE-TTM（滚动市盈率）——0x06B9 统计文件（zhb.zip / tdxstat.cfg）；
  3. 流通市值——0x0010 财务批量（流通股本）× 0x054c 快照（最新价）。
后两项与 DataAPI/TxAPI.py 的同名函数**契约一致**（PE 单位：倍；流通市值单位：
亿元）。「按市场 / 标的类型选源」是业务编排规则，收口在 App 层
（App/AppRefresh.py 的 _fetch_pe_ttm_live），不在 DataAPI 层。

职责：为使用方提供统一、标准化的 eltdx 取数入口。App 层不直接依赖本模块的
统计取数；本模块是 TdxAPI 前复权流水线与 App 层 PE-TTM 取数共同依赖的
下游数据源。

依赖方向：TdxAPI / App 层 → 本模块（单向）。本模块不反向 import
二者，避免 import 环。

数据源：
  - eltdx（7709 协议）——唯一数据源。
  - 单一数据源是刻意设计：失败即显著报错（见 get_xdxr_data 的 log.error），
    不做静默降级。

**港股不支持**：eltdx 无 hk 通道（codes.all("hk") 抛 ValueError、0x054c
快照对 hk 不可用），故本模块三个入口一律只服务 sh / sz / bj。港股 PE-TTM
仍由 DataAPI/TxAPI.py 提供。
"""
import threading
import logging
import time

import pandas as pd
from datetime import datetime

log = logging.getLogger(__name__)

_xdxr_lock = threading.Lock()

# xdxr 独立缓存：key=(market, code)，同一股票跨周期不重复拉取
_xdxr_cache = {}

# 失败退避：key=(market, code) → 上次失败时刻(epoch)。
# eltdx 接口失败时不写负缓存（保可重试），但用短 TTL 抑制高频重试与刷屏：
# 同股票的失败在 XDXR_RETRY_TTL 秒内直接快速返回 None，逾期后再重试并重新报错。
XDXR_RETRY_TTL = 60
_xdxr_fail_ttl = {}


# ============================================================
# 列名标准化
# ============================================================
# eltdx 返回的字段本就按标准名构造，这里再做一层标准化兜底；
# col_map 同时保留一批历史别名，使上游字段名变动时无需改调用方。
def _normalize_xdxr_df(df):
    """
    将传入的 DataFrame 列名统一为标准列名。

    date 可由 (year, month, day) 三列合成，也可直接给 date / 除权日 等别名。
    songzhuangu = 送股+转增 合计（每10股），随后拆分到 songgu。
    最终保证输出含 date, category, fenhong, songgu, zhuanzeng, peigu, peigujia。
    """
    if df is None or len(df) == 0:
        return df

    # ── 处理拆分日期 (year, month, day → date) ──
    if 'year' in df.columns and 'month' in df.columns and 'day' in df.columns:
        def _make_date(row):
            try:
                y = int(row['year']); m = int(row['month']); d = int(row['day'])
                return datetime(y, m, d)
            except Exception:
                return None
        df['date'] = df.apply(_make_date, axis=1)
        # 日期列已创建，不再需要 day->date 映射

    # ── 列名映射 ──
    col_map = {
        # 日期列（仅在无 year/month/day 时生效）
        'date': 'date', 'ex_date': 'date', 'datetime': 'date', 'time': 'date',
        'td': 'date', '除权除息日': 'date', '除权日': 'date',
        'ex_dividend_date': 'date', 'trade_date': 'date',
        # 事件类别
        'category': 'category', 'type': 'category', '类别': 'category',
        'event_type': 'category', 'event': 'category',
        # 分红（每10股）
        'fenhong': 'fenhong', 'cash_div': 'fenhong', 'cash': 'fenhong',
        '分红': 'fenhong', 'dividend': 'fenhong', 'div': 'fenhong',
        # 送转股合计（每10股）— eltdx c3_value 即映射到此处
        'songzhuangu': 'songzhuangu',
        # 送股（每10股）
        'songgu': 'songgu', 'bonus_share': 'songgu', '送股': 'songgu',
        'bonus': 'songgu', 'stock_div': 'songgu', 'sg': 'songgu', 'song': 'songgu',
        # 转增（每10股）
        'zhuanzeng': 'zhuanzeng', 'transfer': 'zhuanzeng', '转增': 'zhuanzeng',
        'zhuan': 'zhuanzeng', 'zz': 'zhuanzeng', 'trans': 'zhuanzeng',
        # 配股（每10股）
        'peigu': 'peigu', 'rights_issue': 'peigu', '配股': 'peigu',
        'allotment': 'peigu', 'rights': 'peigu', 'pg': 'peigu',
        # 配股价
        'peigujia': 'peigujia', 'rights_price': 'peigujia', '配股价': 'peigujia',
        'allotment_price': 'peigujia', 'pgj': 'peigujia',
        # 股票代码
        'code': 'code', 'symbol': 'code', '股票代码': 'code',
    }

    rename = {}
    for col in df.columns:
        col_lower = col.lower().strip().replace('_', '').replace(' ', '')
        if col_lower in col_map:
            rename[col] = col_map[col_lower]
        elif col in col_map:
            rename[col] = col_map[col]
        else:
            for key, target in col_map.items():
                if len(key) >= 3 and key in col_lower:
                    rename[col] = target
                    break

    if rename:
        df = df.rename(columns=rename)
        pass  # 列名映射完成

    # ── 统一 songgu/zhuanzeng: songzhuangu 是送转合计，拆到 songgu ──
    if 'songzhuangu' in df.columns:
        # 将 songzhuangu 的值作为 songgu（送转合计），zhuanzeng 留 0
        if 'songgu' not in df.columns:
            df['songgu'] = df['songzhuangu'].fillna(0)
        else:
            df['songgu'] = df['songgu'].fillna(0) + df['songzhuangu'].fillna(0)
        df.drop(columns=['songzhuangu'], inplace=True)

    # 确保必要的列存在
    required_cols = ['date', 'category', 'fenhong', 'songgu', 'zhuanzeng', 'peigu', 'peigujia']
    for col in required_cols:
        if col not in df.columns:
            df[col] = 0

    return df


# ============================================================
# eltdx 除权除息（活动数据源，基于 7709 协议 0x000f 命令）
# ============================================================
# eltdx TdxClient 单例复用：每次新建 TdxClient 都会重新解析 hosts 并可能触发
# 服务器探测（probe_hosts），探测后写排名缓存（persist=True）在 Windows 下常因
# 文件被占用抛 OSError → RuntimeWarning（"unable to persist eltdx server ranking"）。
# 扫描每票都走 xdxr，若每次新建会反复触发该警告。改为模块级单例 + probe_hosts=False：
#   - 单例：连接复用，避免重复探测/重复解析 hosts；
#   - probe_hosts=False：关闭启动探测（探测仅用于选最快服务器，非必需；
#     连接失败时 eltdx 内部仍会按 hosts 顺序重连），从根上消除该警告。
_eltdx_client = None
_eltdx_client_ready = False


def _check_eltdx_api_compat(client):
    """校验当前 eltdx 是否具备所需的 corporate.capital_changes 接口。

    若旧版 eltdx（缺少 capital_changes）仍在运行，直接抛 RuntimeError（附升级指引），
    让用户及时得知接口失效，而不是静默返回空。
    """
    corporate = getattr(client, "corporate", None)
    if corporate is None or not hasattr(corporate, "capital_changes"):
        raise RuntimeError(
            "[eltdx 接口不兼容] 前复权所需的 client.corporate.capital_changes 不存在："
            "当前 eltdx 版本过旧，请升级：pip install -U 'eltdx>=3.0.0'。"
        )
    return client


def _ensure_eltdx_client():
    """确保 eltdx TdxClient 单例已创建，返回 client 或 None。线程安全。"""
    global _eltdx_client, _eltdx_client_ready
    if _eltdx_client_ready and _eltdx_client is not None:
        return _eltdx_client
    try:
        from eltdx import TdxClient
        # probe_hosts=False：关闭启动服务器探测（探测会写排名缓存，
        # Windows 下文件占用会抛 OSError → RuntimeWarning，且非必需）
        _eltdx_client = TdxClient(timeout=10, probe_hosts=False)
        _eltdx_client_ready = True
        return _eltdx_client
    except Exception:
        _eltdx_client_ready = False
        _eltdx_client = None
        return None


def _get_xdxr_eltdx(market, code):
    """通过 eltdx 获取除权除息数据。在锁内调用。
    返回与 _normalize_xdxr_df 兼容的 DataFrame，失败返回 None。
    使用 client.corporate.capital_changes(code)，其中 0x000f 标签 1 即除权除息事件。字段映射：
      CapitalChangeRecord.c1_value=分红(每10股) · c2_value=配股价 ·
      c3_value=送转(每10股) · c4_value=配股数量(每10股)。
    """
    # 注意：此处不包 try/except——网络 / 接口（含 _check_eltdx_api_compat 的
    # RuntimeError 升级指引）异常一律上抛，由 get_xdxr_data 显著上报，不吞成「无数据」。
    client = _ensure_eltdx_client()
    if client is None:
        return None
    _check_eltdx_api_compat(client)   # 接口失效即抛错，避免静默降级
    market_code = f"{market.lower()}{code}"
    with client:
        block = client.corporate.capital_changes(market_code)
    records = getattr(block, "records", ()) or ()
    rows = []
    for r in records:
        # 仅保留除权除息（标签 1）事件
        if int(getattr(r, "category_raw", 0)) != 1:
            continue
        d = getattr(r, "date", None)
        if d is not None and not isinstance(d, datetime):
            d = datetime(d.year, d.month, d.day)
        rows.append({
            'code': getattr(r, 'code', code),
            'date': d,
            'category': int(getattr(r, 'category_raw', 1)),
            'fenhong': float(getattr(r, 'c1_value', 0) or 0),
            'peigujia': float(getattr(r, 'c2_value', 0) or 0),
            'songzhuangu': float(getattr(r, 'c3_value', 0) or 0),
            'peigu': float(getattr(r, 'c4_value', 0) or 0),
        })
    df = pd.DataFrame(rows, columns=['code', 'date', 'category',
                                     'fenhong', 'peigujia', 'songzhuangu', 'peigu'])
    if len(df) == 0:
        # 该股票历史上无除权除息事件：属「正常空结果」，返回 None（区别于异常）
        return None
    return _normalize_xdxr_df(df)


# 如需新增数据源：在 get_xdxr_data 的来源元组中追加 (名称, 取数函数) 即可，
# 返回值需能被 _normalize_xdxr_df 标准化；**不要**恢复静默 continue 式降级。


# ── 全 A 股除权除息：进程级一次性预取 + 落盘镜像 ──────────────────
# 为什么需要：逐只调 capital_changes 时，每打开一只新股票都要走一次网络；
# 而 eltdx 的 capital_changes 接受**代码序列**并内部分批（默认 75/批），
# 一次把全 A 股拉完，成本远低于逐只。故做成进程级全量缓存：
#   · FastAPI 进程内首次用到除权除息时，批量拉全 A 股 → 内存 + 落盘
#     stock_xdxr.json；进程不重启则后续一律命中内存，不再调 eltdx；
#   · eltdx 整体失败 → 退回读落盘镜像；镜像也没有 → log.error 显著报错
#     （不抛异常：本函数是前复权流水线的必经路径，抛异常会让 K 线接口整体
#     500；这里与 PE-TTM 同款取舍——报 error 而不是静默返回空表）。
#
# 落盘路径与代码全集由 App 层注入（DataAPI 不得 import App，phase5 守卫 ④a）：
# 用 set_xdxr_store() 注入读写实现，用 set_xdxr_universe_provider() 注入
# 「全部 A 股代码」的获取实现。未注入时自动退化为改造前的「按需逐只取数」，
# 行为与改造前完全一致（不影响任何既有调用方）。
_xdxr_store = {"load": None, "save": None}
_xdxr_universe_provider = None
_XDXR_PRIMED = False
_XDXR_PRIME_LOCK = threading.Lock()
_XDXR_PRIME_LAST_FAIL = 0.0
XDXR_PRIME_RETRY_TTL = 60        # 全量预取失败后的退避秒数
XDXR_PRIME_TIME_BUDGET = 90.0    # 全量预取的总时间预算（秒），超时退化为逐只
XDXR_BATCH = 75                  # capital_changes 内部批大小（与 eltdx 默认值一致）


def set_xdxr_store(load_fn=None, save_fn=None):
    """注入除权除息落盘镜像的读写实现（依赖倒置，路径由 App 层决定）。

    load_fn() -> {market: {code: [record, ...]}} 或 {f"{mkt}{code}": [record...]}
    save_fn(dict) -> None
    未注入则不做落盘（等价于改造前行为）。
    """
    if load_fn is not None:
        _xdxr_store["load"] = load_fn
    if save_fn is not None:
        _xdxr_store["save"] = save_fn


def set_xdxr_universe_provider(fn):
    """注入「全部 A 股代码」获取实现：fn() -> [(market, code), ...]。

    未注入时不做全量预取，退化为按需逐只取数（改造前行为）。
    """
    global _xdxr_universe_provider
    _xdxr_universe_provider = fn


def _df_to_records(df):
    """DataFrame → 可 JSON 序列化的记录列表。"""
    if df is None or len(df) == 0:
        return []
    out = []
    for _, r in df.iterrows():
        d = r.get("date")
        try:
            d = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        except Exception:
            d = str(d)
        out.append({
            "date": d,
            "category": int(r.get("category", 1) or 1),
            "fenhong": float(r.get("fenhong", 0) or 0),
            "peigu": float(r.get("peigu", 0) or 0),
            "peigujia": float(r.get("peigujia", 0) or 0),
            "songgu": float(r.get("songgu", 0) or 0),
            "zhuanzeng": float(r.get("zhuanzeng", 0) or 0),
        })
    return out


def _records_to_df(records, code):
    """记录列表 → 标准化 DataFrame（None/空 → None）。"""
    if not records:
        return None
    df = pd.DataFrame(records)
    df["code"] = code
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return _normalize_xdxr_df(df)


def _fetch_xdxr_bulk(pairs):
    """批量拉除权除息，返回 {(market, code): DataFrame}。

    capital_changes 接受代码序列并内部分批；批次内失败时逐只重试隔离坏代码，
    避免一颗「毒丸」拖掉整批。
    """
    client = _ensure_eltdx_client()
    if client is None:
        raise RuntimeError(
            "[eltdx 不可用] 除权除息取数需要 eltdx，请安装/升级："
            "pip install -U 'eltdx>=3.0.0'")
    _check_eltdx_api_compat(client)

    out = {}
    deadline = time.time() + XDXR_PRIME_TIME_BUDGET
    full_codes = [f"{m.lower()}{c}" for m, c in pairs]
    for start in range(0, len(full_codes), XDXR_BATCH):
        if time.time() > deadline:
            log.warning("[xdxr] 全量预取超出时间预算 %.0fs，已取 %d/%d 只，"
                        "剩余退化为按需逐只", XDXR_PRIME_TIME_BUDGET,
                        len(out), len(full_codes))
            break
        chunk = full_codes[start:start + XDXR_BATCH]
        try:
            with client:
                block = client.corporate.capital_changes(chunk)
            _collect_capital_changes(block, chunk, out)
        except Exception as e:
            log.warning("[xdxr] 批次 %d-%d 取数失败，逐只隔离重试：%s: %s",
                        start, start + len(chunk), type(e).__name__, e)
            for one in chunk:
                try:
                    with client:
                        blk = client.corporate.capital_changes(one)
                    _collect_capital_changes(blk, [one], out)
                except Exception:
                    continue
    return out


def _collect_capital_changes(block, full_codes, out):
    """把 capital_changes 返回块解析进 out（仅保留标签 1 = 除权除息）。"""
    recs = list(getattr(block, "records", ()) or ())
    if not recs:
        return
    # 按代码分组：批量返回里 code 字段形如 "sh600519" 或 "600519"
    grouped = {}
    for r in recs:
        if int(getattr(r, "category_raw", 0)) != 1:
            continue
        raw = str(getattr(r, "code", "") or "")
        key = None
        for fc in full_codes:
            if raw == fc or raw.endswith(fc[2:]) or fc.endswith(raw):
                key = fc
                break
        if key is None:
            key = full_codes[0] if len(full_codes) == 1 else raw
        grouped.setdefault(key, []).append(r)
    for full_code, rows in grouped.items():
        mkt, code = full_code[:2], full_code[2:]
        data = []
        for r in rows:
            d = getattr(r, "date", None)
            if d is not None and not isinstance(d, datetime):
                d = datetime(d.year, d.month, d.day)
            data.append({
                "code": code,
                "date": d,
                "category": int(getattr(r, "category_raw", 1)),
                "fenhong": float(getattr(r, "c1_value", 0) or 0),
                "peigujia": float(getattr(r, "c2_value", 0) or 0),
                "songzhuangu": float(getattr(r, "c3_value", 0) or 0),
                "peigu": float(getattr(r, "c4_value", 0) or 0),
            })
        if data:
            df = pd.DataFrame(data, columns=["code", "date", "category",
                                             "fenhong", "peigujia",
                                             "songzhuangu", "peigu"])
            out[(mkt, code)] = _normalize_xdxr_df(df)


def _prime_xdxr_all():
    """构建全 A 股除权除息进程缓存（在 _XDXR_PRIME_LOCK 内调用）。"""
    global _XDXR_PRIMED, _XDXR_PRIME_LAST_FAIL
    pairs = []
    try:
        pairs = list(_xdxr_universe_provider() or ())
    except Exception as e:
        log.error("[xdxr] 获取全 A 股代码列表失败，退化为按需逐只: %s: %s",
                  type(e).__name__, e)
        _XDXR_PRIMED = True
        return

    # ① eltdx 全量
    got = {}
    try:
        got = _fetch_xdxr_bulk(pairs)
    except Exception as e:
        log.error("[xdxr] eltdx 全量除权除息取数失败: %s: %s", type(e).__name__, e)

    if got:
        with _xdxr_lock:
            for k, v in got.items():
                _xdxr_cache.setdefault(k, v)
        _save_xdxr_snapshot()
        _XDXR_PRIMED = True
        log.info("[xdxr] 全量预取完成：%d 只股票有除权除息记录（共 %d 只）",
                 len(got), len(pairs))
        return

    # ② 落盘镜像
    if _load_xdxr_snapshot():
        _XDXR_PRIMED = True
        log.warning("[xdxr] eltdx 不可用，本次使用本地镜像 %s（数据可能陈旧）",
                    "stock_xdxr.json")
        return

    # ③ 都没有：显著报错
    _XDXR_PRIME_LAST_FAIL = time.time()
    _XDXR_PRIMED = True
    log.error("[xdxr] 无法获取除权除息：eltdx 全量取数失败，且本地镜像不存在/不可用 "
              "→ 本次前复权将缺少除权除息（价格可能不正确）。请检查 eltdx/网络，"
              "或先在能联网时打开过一次股票以生成镜像。")


def _load_xdxr_snapshot():
    """从落盘镜像填充内存缓存，返回是否成功。"""
    fn = _xdxr_store.get("load")
    if fn is None:
        return False
    try:
        data = fn()
    except Exception as e:
        log.warning("[xdxr] 本地镜像读取失败: %s: %s", type(e).__name__, e)
        return False
    if not isinstance(data, dict) or not data:
        return False
    n = 0
    with _xdxr_lock:
        for key, records in data.items():
            if not isinstance(key, str) or len(key) <= 2:
                continue
            mkt, code = key[:2], key[2:]
            df = _records_to_df(records, code)
            if df is not None and len(df) > 0:
                _xdxr_cache.setdefault((mkt, code), df)
                n += 1
    log.info("[xdxr] 本地镜像载入 %d 只", n)
    return n > 0


def _save_xdxr_snapshot():
    """把内存中的除权除息落盘（增量覆盖）。"""
    fn = _xdxr_store.get("save")
    if fn is None:
        return
    try:
        with _xdxr_lock:
            snap = {f"{m}{c}": _df_to_records(df)
                    for (m, c), df in _xdxr_cache.items() if df is not None}
        fn(snap)
        log.info("[xdxr] 落盘 %d 只到 stock_xdxr.json", len(snap))
    except Exception as e:
        log.warning("[xdxr] 落盘失败: %s: %s", type(e).__name__, e)


def _ensure_xdxr_primed():
    """进程内首次用到除权除息时触发一次全量预取（single-flight）。"""
    global _XDXR_PRIMED
    if _XDXR_PRIMED or _xdxr_universe_provider is None:
        _XDXR_PRIMED = True          # 未注入代码源：保持改造前的逐只行为
        return
    if (_XDXR_PRIME_LAST_FAIL
            and time.time() - _XDXR_PRIME_LAST_FAIL < XDXR_PRIME_RETRY_TTL):
        return
    if not _XDXR_PRIME_LOCK.acquire(blocking=False):
        return                        # 已有线程在预取：本次直接用当前缓存
    try:
        if not _XDXR_PRIMED:
            _prime_xdxr_all()
    finally:
        _XDXR_PRIME_LOCK.release()


def get_xdxr_data(market, code):
    """
    获取指定股票的除权除息数据。
    线程安全：多线程并发时，网络请求串行化，避免 socket 竞争。

    优先级：
      0. 全 A 股进程级缓存（FastAPI 进程内首次调用时一次性批量预取，
         见 _ensure_xdxr_primed；进程不重启则后续全部命中内存）
      1. 缓存（内存命中，跳过网络请求）
      2. eltdx（唯一数据源，基于 7709 协议、0x000f 命令；失败即报错而非静默降级）

    返回 pandas DataFrame，统一列名：
      date, category, fenhong, peigu, peigujia, songgu, zhuanzeng
    其中 fenhong/songgu/zhuanzeng/peigu 均为"每10股"单位。
    返回 None 表示无除权除息数据或 eltdx 获取失败。
    """
    if market.lower() not in ('sh', 'sz'):
        return None

    _ensure_xdxr_primed()

    cache_key = (market, code)
    with _xdxr_lock:
        if cache_key in _xdxr_cache:
            return _xdxr_cache[cache_key]

        # 失败退避：eltdx 刚失败过（TTL 内）不再重试，快速返回 None，避免反复触发
        # 10s 超时 + 重复报错；逾期后自然重试并重新显著报错。
        _last_fail = _xdxr_fail_ttl.get(cache_key)
        if _last_fail is not None and time.time() - _last_fail < XDXR_RETRY_TTL:
            return None

        _failed = False
        for _src_name, _src_fn in (
            ("eltdx", _get_xdxr_eltdx),
        ):
            try:
                df = _src_fn(market, code)
            except Exception as _e:
                # 网络 / 接口（含 _check_eltdx_api_compat 的 RuntimeError 升级指引）异常：显著上报
                _failed = True
                log.error("[xdxr] %s 取数失败(market=%s, code=%s): %s",
                          _src_name, market, code, _e)
                # 尝试下一数据源；当前仅 eltdx 一个，continue 后即进入 _failed 分支
                continue
            if df is not None and len(df) > 0:
                _xdxr_fail_ttl.pop(cache_key, None)   # 取数成功：清除失败退避记录
                _xdxr_cache[cache_key] = df
                return df
            # df is None：该源无记录，继续尝试下一数据源
            continue

        if _failed:
            # 至少一个源异常且无成功数据：记录失败时刻用于退避、不写负缓存（保留可重试）
            _xdxr_fail_ttl[cache_key] = time.time()
            return None
        # 所有数据源均空结果 = 该股历史上无除权除息（正常空结果）：走 info，避免全市场
        # 扫描被「历史上无除权」刷满 ERROR；缓存 None 避免重复查询。
        log.info("[xdxr] %s: %s 无除权除息记录（正常空结果；网络/接口异常会另行上报 error）",
                 market, code)
        _xdxr_cache[cache_key] = None
        return None


# ============================================================
# 板块文件下载 / 刷新（基于 eltdx 0x06B9 通用文件读取）
# ============================================================
# 注意：spblock.dat 服务器不经文件下载通道服务（实测返回 0 字节），
# 刷新时保留旧文件（该文件由通达信客户端「盘后下载」单独写入）。
_MAX_BLOCK_FILE_BYTES = 8_000_000


def download_block_file_via_eltdx(file_name, hosts=None):
    """通过 eltdx 0x06B9 下载单个板块文件，返回原始字节；不可用/失败返回 None。

    hosts: 'host:port' 字符串列表；为 None 时使用 eltdx 默认服务器列表。
    """
    try:
        from eltdx import TdxClient
    except Exception:
        return None
    try:
        client = TdxClient(timeout=10, probe_hosts=False, hosts=hosts)
        with client:
            data = client.resources.download_file(
                file_name, max_bytes=_MAX_BLOCK_FILE_BYTES, chunk_size=0x4000
            )
        if not data:
            return None
        return data
    except Exception:
        return None


def download_block_files_via_eltdx(file_names, hosts=None, on_file=None):
    """批量下载板块文件，返回 {file_name: bytes}（失败/不可用的不计入）。

    单次连接内顺序下载，避免每文件重建连接；任一文件失败不影响其余。

    on_file: 可选回调 on_file(file_name, ok, nbytes)，每下完一个文件调用一次。
    刷新链路用它把「正在下载第 i/N 个文件」实时报到前端 —— 批量下载本身耗时
    可观（见 TdxAPI.refresh_block_files），没有逐文件回调时 UI 的 step 文字会
    在整个下载期间静止不动，用户只会感觉到"卡住了"。
    """
    result = {}
    try:
        from eltdx import TdxClient
    except Exception:
        return result
    try:
        client = TdxClient(timeout=10, probe_hosts=False, hosts=hosts)
        with client:
            for file_name in file_names:
                data = None
                try:
                    data = client.resources.download_file(
                        file_name, max_bytes=_MAX_BLOCK_FILE_BYTES, chunk_size=0x4000
                    )
                except Exception:
                    data = None
                if data:
                    result[file_name] = data
                if on_file is not None:
                    try:
                        on_file(file_name, bool(data), len(data) if data else 0)
                    except Exception:
                        pass
    except Exception:
        pass
    return result


# ============================================================
# 行情统计：PE-TTM / 流通市值（仅 A 股；eltdx 无港股通道）
# ============================================================
# 两个函数与 DataAPI/TxAPI.py 的同名函数**契约完全一致**，使
# App 层（AppRefresh._fetch_pe_ttm_live）能按标的类型选源后互换调用：
#   - fetch_pe_ttm(mkt_codes)    → {mkt+code: PE-TTM(倍)}
#   - fetch_float_mc(stock_list) → {code: 流通市值(亿元)}
#
# 选源规则（唯一一份）在 App/AppRefresh.py：A 股个股 → 本模块；指数 / 港股 → TxAPI。
# 港股不在本模块服务范围：eltdx 的 codes.all("hk") / 0x054c 对 hk 均不可用。
ELTDX_MARKETS = ("sh", "sz", "bj")

# 逐批请求上限。实测 0x0010 财务批量与 0x054c 快照在 **80 只/批** 稳定；
# 单批放大到 200 只时服务端会截断（返回 100 条）或给出错位响应，
# 故取 80（与 eltdx helpers 内部同值）。
STATS_BATCH = 80

# eltdx 统计文件里的 market_id ↔ 市场前缀（与 chan.py 的 prefix 语义同构：
# 0→sz / 1→sh / 2→bj）
_MARKET_ID_TO_MKT = {0: "sz", 1: "sh", 2: "bj"}


def _chunked_stats_fetch(full_codes, batch_fn, extract):
    """按 STATS_BATCH 分批取数，返回 {full_code: 值}。

    单批异常时**逐只重试**以隔离坏代码，避免一颗「毒丸」拖掉整批 80 只：
    实测 0x054c 快照遇到个别代码（如北交所 bj920025）会抛 ProtocolError
    （"snapshot record marker not found"），且是**整批失败**而非跳过单条。
    """
    out = {}
    for start in range(0, len(full_codes), STATS_BATCH):
        chunk = full_codes[start:start + STATS_BATCH]
        try:
            rows = batch_fn(chunk)
        except Exception as _e:
            log.warning("[eltdx 统计] 批次 %d-%d 取数失败，逐只隔离重试：%s: %s",
                        start, start + len(chunk), type(_e).__name__, _e)
            rows = []
            for _code in chunk:
                try:
                    rows.extend(batch_fn([_code]))
                except Exception:
                    continue
        out.update(extract(rows))
    return out


def _finance_shares_map(records):
    """0x0010 财务批量记录 → {full_code: 流通股本(股)}。

    流通股本为 0/None 的代码不计入（如部分北交所标的无流通股本，
    市值本就无从计算）——不写 0，交由调用方按「未获取到」统计。
    """
    out = {}
    for r in records:
        exchange = getattr(r, "exchange", None) or ""
        code = getattr(r, "code", None)
        shares = getattr(r, "circulating_shares", None)
        if exchange and code and shares:
            out[f"{exchange}{code}"] = float(shares)
    return out


def _snapshot_price_map(rows):
    """0x054c 快照 → {full_code: 最新价(元)}"""
    out = {}
    for r in rows:
        full_code = getattr(r, "full_code", None)
        price = getattr(r, "last_price", None)
        if full_code and price:
            out[full_code] = float(price)
    return out


def fetch_pe_ttm(mkt_codes):
    """eltdx 批量获取 **A 股** PE-TTM（滚动市盈率），返回 {mkt+code: float}。

    mkt_codes: list[(mkt, code)]，mkt ∈ {sh, sz, bj}；其余市场（如 hk）
               直接忽略——返回结果不含这些键，选源见 App/AppRefresh.py。
    数据源：0x06B9 服务器文件读取 → zhb.zip 内 tdxstat.cfg（**盘后**统计
            快照），**单次请求即覆盖全市场**，无需按票分批。
    取值：pe_ttm 为 None（无值）或 0 的代码跳过；**负值保留**（亏损股口径）。

    与腾讯 [39] 的实测一致性：A 股 5224 只中 97.6% 浮点严格相等、99.6% 差
    ≤0.05；eltdx 无值而腾讯有值的 5 只全为次新股（口径差，非缺失）。
    """
    pairs = [(m, c) for m, c in (mkt_codes or ()) if m in ELTDX_MARKETS]
    if not pairs:
        return {}
    client = _ensure_eltdx_client()
    if client is None:
        raise RuntimeError(
            "[eltdx 不可用] PE-TTM 取数需要 eltdx，请安装/升级："
            "pip install -U 'eltdx>=3.0.0'"
        )
    with client:
        stats = client.resources.read_stats()
    log.info("[eltdx 统计] PE-TTM 统计日期=%s，全表 %d 行",
             getattr(stats, "stats_date", None), getattr(stats, "stat_count", 0))

    # 全表一次建成索引：key=(市场, 6 位代码)。统计文件里的 code 未保证补零，
    # 统一 zfill(6) 后再比对，避免 "1" / "000001" 这类同票不同写法漏配。
    index = {}
    for (market_id, code), row in stats.stat.items():
        mkt = _MARKET_ID_TO_MKT.get(market_id)
        if mkt:
            index[(mkt, str(code).zfill(6))] = row

    result = {}
    for mkt, code in pairs:
        row = index.get((mkt, str(code).zfill(6)))
        pe_val = getattr(row, "pe_ttm", None) if row is not None else None
        if pe_val is None or pe_val == 0:
            continue
        result[mkt + code] = float(pe_val)
    return result


def fetch_pe_ttm_all():
    """eltdx 一次性获取**全 A 股** PE-TTM，返回 {mkt+code: float}（不按 pair 过滤）。

    与 fetch_pe_ttm 的关系：二者共用同一个数据源（0x06B9 → zhb.zip 内
    tdxstat.cfg，单次请求覆盖全市场），fetch_pe_ttm 只是本函数的「按指定
    pair 过滤」视图。取 1 只与取全市场网络成本相同，故「打开 K 线页面取
    PE」这类场景应使用本函数一次拉全表、在进程内缓存，而不是逐只调用
    fetch_pe_ttm（否则每打开一只股票都重下一次全表）。

    返回不含 PE 为 None / 0 的代码；**负值保留**（亏损股口径）。
    """
    client = _ensure_eltdx_client()
    if client is None:
        raise RuntimeError(
            "[eltdx 不可用] PE-TTM 取数需要 eltdx，请安装/升级："
            "pip install -U 'eltdx>=3.0.0'"
        )
    with client:
        stats = client.resources.read_stats()
    log.info("[eltdx 统计] PE-TTM 统计日期=%s，全表 %d 行",
             getattr(stats, "stats_date", None), getattr(stats, "stat_count", 0))

    index = {}
    for (market_id, code), row in stats.stat.items():
        mkt = _MARKET_ID_TO_MKT.get(market_id)
        if mkt:
            index[(mkt, str(code).zfill(6))] = row

    result = {}
    for (mkt, code6), row in index.items():
        pe_val = getattr(row, "pe_ttm", None) if row is not None else None
        if pe_val is None or pe_val == 0:
            continue
        result[mkt + code6] = float(pe_val)
    return result


def fetch_float_mc(stock_list):
    """eltdx 批量获取 **A 股** 流通市值，返回 {code: 流通市值(亿元)}。

    stock_list: list[{"code": "600519", "prefix": "1"}, ...]，prefix
                0→sz / 1→sh / 2→bj；其它前缀（hk / us）一律跳过——与改造前的
                腾讯路径一致：流通市值本就只覆盖 A 股。
    口径：流通市值 = 流通股本(0x0010) × 最新价(0x054c)，与 eltdx
          StockProfile.circulating_market_value 同源同式；单位由「元」→「亿元」
          （腾讯 [44] 返回亿元且只有两位小数，本路径为原值，精度更高）。
    缺失语义：无股本 / 股本为 0 / 无快照的代码不计入结果，不写 0。
    """
    _PFX_TO_MKT = {"0": "sz", "1": "sh", "2": "bj"}
    full_codes = []
    for stk in stock_list or ():
        mkt = _PFX_TO_MKT.get(stk.get("prefix", ""), "")
        code = stk.get("code", "")
        if mkt and code:
            full_codes.append(mkt + code)
    if not full_codes:
        return {}
    full_codes = list(dict.fromkeys(full_codes))    # 去重保序：同一票不重复请求

    client = _ensure_eltdx_client()
    if client is None:
        raise RuntimeError(
            "[eltdx 不可用] 流通市值取数需要 eltdx，请安装/升级："
            "pip install -U 'eltdx>=3.0.0'"
        )
    with client:
        # 先跑完股本批再取快照批：实测同一连接上「前一个请求失败」会让紧随其后的
        # 一个请求读到错位响应（invalid ASCII response code）。把股本批放在前面，
        # 快照批的坏代码隔离就不会牵连股本数据。
        shares = _chunked_stats_fetch(
            full_codes,
            lambda ch: list(getattr(client.corporate.finance_batch(ch), "records", ()) or ()),
            _finance_shares_map,
        )
        prices = _chunked_stats_fetch(
            full_codes,
            lambda ch: list(client.quotes.get_snapshots(ch)),
            _snapshot_price_map,
        )

    result = {}
    for full_code in full_codes:
        shares_val = shares.get(full_code)
        price_val = prices.get(full_code)
        if not shares_val or not price_val:
            continue
        result[full_code[2:]] = shares_val * price_val / 1e8
    return result

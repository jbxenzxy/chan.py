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
import json
import socket
import urllib.parse
import urllib.request

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


def get_xdxr_data(market, code):
    """
    获取指定股票的除权除息数据。
    线程安全：多线程并发时，网络请求串行化，避免 socket 竞争。

    优先级：
      1. 缓存（内存命中，跳过网络请求）
      2. eltdx（唯一数据源，基于 7709 协议、0x000f 命令；失败即报错而非静默降级）

    返回 pandas DataFrame，统一列名：
      date, category, fenhong, peigu, peigujia, songgu, zhuanzeng
    其中 fenhong/songgu/zhuanzeng/peigu 均为"每10股"单位。
    返回 None 表示无除权除息数据或 eltdx 获取失败。
    """
    if market.lower() not in ('sh', 'sz'):
        return None

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


# ============================================================
# 重要股东买卖：股东增减持计划（通达信 F10 7615 网关，按代码查询）
# ============================================================
# 数据源：7615 TQLEX 网关 Entry=CWServ.tdxf10_gg_gdyj + section gdzjcjh
#   （与 eltdx.f10.F10Client.shareholder_change_plans 同一端点，但请求由
#   _f10_tqlex_post 直发：强制 IPv4 + 禁代理，绕开本机 IPv6 黑洞导致的
#   每请求 8~12s 卡顿——见 _f10_tqlex_post docstring 的实测数据）。
# 这是「按代码查询」的协议命令（与 xdxr 同类），**不是** PE-TTM 那样一次性
# 下载全市场统计文件；故按单只股票取数 + 进程缓存，不落盘全市场文件。
# 列名（实测 7615 网关 ColName 返回 N001..N012，与 eltdx 解析一致）：
#   N001 公告日期  N002 拟减持/拟增持  N003 股东名称  N004 股东身份
#   N005 拟减持股数  N006 占总股本%  N007/N008 拟增持股数(下限/上限)
#   N009 变动起始日期  N010 变动截止日期  N011 进度(完成/进行中/未实施)
_REDUCTION_CACHE = {}          # mkt+code -> (epoch, [plans])
_REDUCTION_CACHE_TTL = 24 * 3600
_REDUCTION_LOCK = threading.Lock()
# 7615 F10 网关 HTTP 超时。IPv4 直连实测 117~350ms，5s 余量已极大；
# 不再用 eltdx F10Client（其 urlopen 无法控制地址族，见 _f10_tqlex_post）。
_REDUCTION_TIMEOUT = 5.0

# 7615 TQLEX 网关请求头（与 eltdx F10Client 同款；URL 在 _f10_tqlex_post
# 里按解析出的 IPv4 直连地址构造，Host 头固定回填域名）
_TQLEX_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "eltdx/1.0",
}


def _f10_tqlex_post(entry, params, timeout=_REDUCTION_TIMEOUT):
    """直连 7615 TQLEX 网关的 POST（**强制 IPv4 + 禁代理**）。

    为什么不复用 eltdx F10Client：其 _post 用裸 urlopen，无法控制地址族。
    Windows 上 getaddrinfo 会把 IPv6 排在 IPv4 前面，而本机 IPv6 出口对
    static.tdx.com.cn 不通（实测 TCP 握手 12s 超时），urllib 只能等 IPv6
    SYN 重传失败后才回落 IPv4 → 每次请求固定卡 8~12 秒（这是「K 线页加载
    凭空多 8 秒」的真正根因；走代理的环境则无此问题，因为代理客户端自己
    连目标，不经本机 IPv6）。

    修复：getaddrinfo 限定 AF_INET 拿 IPv4 → 直接连 IP、Host 头带域名 →
    实测完整 POST 117ms（对比 eltdx 默认路径 8100ms，约 70 倍）。
    同时用 ProxyHandler({}) 禁代理——TDX 国内网关直连即可，且不依赖用户
    终端是否挂了代理（有代理走代理也快，但直连更快、环境更少依赖）。

    返回 TQLEX JSON dict；失败抛异常（调用方决定吞不吞）。
    """
    ipv4 = socket.getaddrinfo("static.tdx.com.cn", 7615, socket.AF_INET,
                              socket.SOCK_STREAM)[0][4][0]
    url = f"http://{ipv4}:7615/TQLEX?{urllib.parse.urlencode({'Entry': entry})}"
    body = json.dumps({"Params": list(params)}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=_TQLEX_HEADERS, method="POST")
    request.add_unredirected_header("Host", "static.tdx.com.cn")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8-sig"))


def get_shareholder_reduction_plans(market, code):
    """获取某股票的「股东增减持计划」原始列表（按代码查，进程缓存 1 天）。

    返回 list[dict]，每条字段：
        announce_date, direction, holder, identity,
        reduce_shares, reduce_pct, start, end, progress
    非 A 股（指数 / 港股 / 期货等）、取数失败 → 返回 []。

    注意：请求直接经 _f10_tqlex_post（强制 IPv4 直连 7615 网关），
    **不再经过 eltdx F10Client**（其 urlopen 在本机 IPv6 不通时每次卡 8~12s）。
    """
    if market.lower() not in ('sh', 'sz', 'bj'):
        return []
    key = market.lower() + code
    now = time.time()
    with _REDUCTION_LOCK:
        cached = _REDUCTION_CACHE.get(key)
        if cached and now - cached[0] < _REDUCTION_CACHE_TTL:
            return cached[1]
    try:
        raw = _f10_tqlex_post(
            "CWServ.tdxf10_gg_gdyj",
            [code, "gdzjcjh", "", "", "1", "1", "20"],
        )
        error_code = raw.get("ErrorCode")
        result_sets = raw.get("ResultSets") or ()
        if error_code not in (None, 0) or not result_sets:
            log.warning("[股东增减持] 网关返回 ErrorCode=%s(%s%s)", error_code, market, code)
            return []
        rs0 = result_sets[0]
        rows_raw = rs0.get("Content") or ()
        col_names = [str(c) for c in (rs0.get("ColName") or ())]
        plans = []
        for row in rows_raw:
            if isinstance(row, dict):           # 防御：网关某些 Entry 返回 dict
                item = row
            elif col_names and isinstance(row, (list, tuple)):
                item = dict(zip(col_names, row))
            else:
                continue
            plans.append({
                "announce_date": item.get("N001"),
                "direction": item.get("N002"),
                "holder": item.get("N003"),
                "identity": item.get("N004"),
                "reduce_shares": item.get("N005"),
                "reduce_pct": item.get("N006"),
                "start": item.get("N009"),
                "end": item.get("N010"),
                "progress": item.get("N011"),
            })
        with _REDUCTION_LOCK:
            _REDUCTION_CACHE[key] = (now, plans)
        return plans
    except Exception as _e:
        log.warning("[股东增减持] 取数失败(%s%s): %s", market, code, _e)
        return []


def _parse_plan_date(s):
    """把 'YYYY-MM-DD' / 'YYYY/MM/DD' / 'YYYYMMDD'（可带 ' HH:MM[:SS]' 时间尾部，
    来自日内周期 K 线日期）解析为 date；无法解析返回 None。

    注意：应用里 K 线日期用 func_util._get_date_fmt 的 %Y/%m/%d（斜杠）格式，
    而 eltdx 网关返回的减持计划日期是 %Y-%m-%d（短横）；两者都要兼容。
    """
    if not s:
        return None
    s = str(s).strip()
    s = s.split(" ")[0]          # 丢弃时间部分（日内周期日期形如 2026/09/11 15:00）
    s = s.replace("/", "-")      # 兼容 / 与 - 分隔符
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _fmt_mmdd(d):
    """date → 'MM-DD'（徽标里展示的紧凑形式），None 原样返回 None。"""
    if d is None:
        return None
    return d.strftime("%m-%d")


def get_shareholder_reduction_flag(market, code, today):
    """判断「当前交易日 today」是否落在某条「拟减持」计划的 减持窗口（公告日~变动截止日）内。

    today: 'YYYY-MM-DD' / 'YYYY/MM/DD' / 'YYYYMMDD'（建议传 K 线最新一根日期 = 屏幕上当前交易日）。
    命中任一条「进行中」拟减持计划即返回 active=True，并把**所有被命中窗口合并为
    一个最大连续区间**（最早起始 ~ 最晚截止；因命中窗口都包含 today，并集天然
    连续无空洞，整段即潜在抛压区间）；windows 恒为 1 条，另附最大拟减持股比 /
    股东名单；否则返回 {'active': False}。

    进度（N011）过滤：仅「进行中」的计划计入抛压——「完成」（已实施完毕、
    抛压已释放）与「停止实施」（计划取消）不计入；未知状态默认保留。

    窗口 = **公告日(N001) ~ 变动截止日(N010)**。语义：减持计划一经公告，
    潜在抛压即告成立，持续到窗口截止——这正是「公告日到截止日」的业务含义。
    N009（变动起始日）仅作 N001 缺失时的回退，不作为常规下界。
    若源数据错乱导致 起始 > 截止，该行跳过。

    注意：本函数触发 7615 网关 HTTP 调用（强制 IPv4 直连，实测 117~130ms，
    超时 5s；进程内每代码缓存 1 天），**可安全同步调用**——AppEngine 在 K 线
    主分析路径上直接调用它。曾经「每次 8~12s」的卡顿根因是 eltdx 默认 urlopen
    走了本机不通的 IPv6（详见 _f10_tqlex_post），与该函数本身的逻辑无关。
    """
    if market.lower() not in ('sh', 'sz', 'bj'):
        return {"active": False}
    today_d = _parse_plan_date(today)
    if today_d is None:
        return {"active": False}
    plans = get_shareholder_reduction_plans(market, code)
    windows = []      # [(end, start_str, end_str, pct, holder), ...] 只保留「今日命中」的窗口
    hit_pct = None
    all_holders = []
    for p in plans:
        direction = p.get("direction") or ""
        if "减持" not in direction:
            continue
        # 进度过滤（N011 实测词表：进行中 / 完成 / 停止实施）：
        # 「完成」= 已实施完毕、抛压已释放；「停止实施」= 计划取消。均不计入抛压区间。
        # 用黑名单匹配而非白名单（未知新状态默认保留，宁可多显示不漏抛压）。
        progress = str(p.get("progress") or "")
        if any(k in progress for k in ("完成", "停止实施", "失效", "终止", "结束", "取消")):
            continue
        # 窗口 = 公告日(N001) ~ 变动截止日(N010)。语义：公告披露后即可视为
        # 潜在抛压开始，直到窗口截止；N009（变动起始日）仅作 N001 缺失时的回退。
        lo = _parse_plan_date(p.get("announce_date")) or _parse_plan_date(p.get("start"))
        hi = _parse_plan_date(p.get("end"))
        if lo is None or hi is None or lo > hi:
            continue
        if lo <= today_d <= hi:
            window = {
                "start": _fmt_mmdd(lo),
                "end": _fmt_mmdd(hi),
                "start_full": lo.strftime("%Y-%m-%d"),
                "end_full": hi.strftime("%Y-%m-%d"),
            }
            try:
                pct = float(p["reduce_pct"])
            except (TypeError, ValueError):
                pct = None
            window["pct"] = pct
            window["holder"] = p.get("holder")
            windows.append(window)
            if pct is not None and (hit_pct is None or pct > hit_pct):
                hit_pct = pct
            holder = p.get("holder")
            if holder and holder not in all_holders:
                all_holders.append(holder)
    if not windows:
        return {"active": False}
    # 合并为**最大连续区间**（最早起始 ~ 最晚截止）：能进入 windows 的窗口都
    # 包含 today，故它们的并集天然连续、无空洞——整段都可视作潜在抛压区间
    # （用户定版口径：不管命中几条计划，徽标只显示一个「06-14~12-16」式大区间）。
    # 股东名「/」串接去重、占比取最大。
    lo_full = min(w["start_full"] for w in windows)   # ISO 日期可直接按字符串比较
    hi_full = max(w["end_full"] for w in windows)
    span_holders = []
    span_pct = None
    for w in windows:
        h = w.get("holder")
        if h and h not in span_holders:
            span_holders.append(h)
        if w.get("pct") is not None and (span_pct is None or w["pct"] > span_pct):
            span_pct = w["pct"]
    windows = [{
        "start": lo_full[5:],                # MM-DD
        "end": hi_full[5:],                  # MM-DD
        "start_full": lo_full,
        "end_full": hi_full,
        "pct": span_pct,
        "holder": "/".join(span_holders),
    }]
    return {
        "active": True,
        "windows": windows,                  # 恒 1 条：{start, end, start_full, end_full, pct, holder}
        "max_pct": hit_pct,
        "holders": all_holders,
        # 兼容旧字段（首个被命中窗口 = 区间截止日）—— 前端徽标默认取第一个
        "end": windows[0]["end_full"],
    }

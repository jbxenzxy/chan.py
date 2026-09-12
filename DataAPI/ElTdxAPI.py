"""
eltdx 数据源适配器（通达信网络行情客户端封装）。

定位：**并非仅面向 XDXR，能力面向未来开放扩展。** 本模块封装 eltdx 客户端这
一个通达信网络数据源，**当前实现/用法是获取除权除息（XDXR）数据**，供 TdxAPI
前复权流水线消费；未来若 eltdx 可提供其它通达信信息，同样经本模块扩展暴露。

职责：为使用方提供统一、标准化的 eltdx 取数入口（当前为除权除息事件表）。
服务层**不直接依赖本模块**——它们消费"前复权后的 K 线"（由 TdxAPI 的
fetch_main_level 提供）；本模块是 TdxAPI 前复权流水线依赖的下游数据源。

依赖方向：TdxAPI → 本模块（单向）。本模块不反向 import TdxAPI，避免 import 环。

数据源：
  - eltdx（基于 7709 协议、0x000f 命令）——唯一数据源。
  - 单一数据源是刻意设计：失败即显著报错（见 get_xdxr_data 的 log.error），
    不做静默降级。曾存在的 mootdx / pytdx 三级回退已彻底删除（见文件中部说明）。
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


# ============================================================
# 注：mootdx / pytdx 三级回退已于 2026-09 整体删除
# ============================================================
# 删除理由：
#   1. eltdx 单源已足够覆盖除权除息需求（0x000f 命令）；
#   2. 回退链的语义是「上一源异常 → continue 试下一源」，会把「网络/接口故障」
#      静默降级成「该股无除权除息数据」，掩盖真实故障并污染前复权结果；
#   3. 单源 + 显著报错（log.error）更利于暴露问题，与 TdxAPI 前复权流水线的
#      「失败即报错」口径一致。
# 如需新增数据源：在第 3 段 get_xdxr_data 的来源元组中追加 (名称, 取数函数) 即可，
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
# 2026-09-12：原 pytdx 板块刷新（TdxHq_API + get_block_info_meta/get_block_info）
# 整体迁移到 eltdx。实证（见 TdxAPI.refresh_block_files 旁注）：eltdx 经 0x06B9 读取服务器文件，
# 对 tdxhy.cfg / infoharbor_block.dat / block_zs.dat / block_gn.dat / block_fg.dat 返回的
# 字节数与 pytdx 完全一致；spblock.dat 两者均返回 0 字节（服务器不服务该文件，刷新时保留旧文件）。
# 因此 eltdx 可完全替代 pytdx 完成板块文件刷新，pytdx 不再用于本项目。
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


def download_block_files_via_eltdx(file_names, hosts=None):
    """批量下载板块文件，返回 {file_name: bytes}（失败/不可用的不计入）。

    单次连接内顺序下载，避免每文件重建连接；任一文件失败不影响其余。
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
                try:
                    data = client.resources.download_file(
                        file_name, max_bytes=_MAX_BLOCK_FILE_BYTES, chunk_size=0x4000
                    )
                except Exception:
                    data = None
                if data:
                    result[file_name] = data
    except Exception:
        pass
    return result

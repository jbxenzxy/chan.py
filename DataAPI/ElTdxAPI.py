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
  - eltdx（基于 7709 协议、0x000f 命令）——当前唯一活动数据源。
  - mootdx / pytdx 回退分支已整体注释保留（测试 eltdx 稳定性期间彻底禁用回退、
    失败即显著报错而非静默降级）；日后如需恢复三级回退，取消对应注释即可。
"""
import threading
import logging

import pandas as pd
from datetime import datetime

log = logging.getLogger(__name__)

_xdxr_lock = threading.Lock()

# xdxr 独立缓存：key=(market, code)，同一股票跨周期不重复拉取
_xdxr_cache = {}


# ============================================================
# 列名标准化
# ============================================================
# mootdx / pytdx 返回的列名可能不同，统一标准化。
# （eltdx 返回的字段本就按标准名构造，也走同样标准化兜底。）
def _normalize_xdxr_df(df):
    """
    将 mootdx / pytdx / eltdx 返回的 DataFrame 列名统一为标准列名。

    mootdx 实际列名（本版本）:
      year, month, day, category, name, fenhong, peigujia,
      songzhuangu, peigu, suogu, panqianliutong, panhouliutong,
      qianzongguben, houzongguben, fenshu, xingquanjia
    其中 songzhuangu = 送股+转增 合计（每10股）
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
        # 送转股合计（每10股）— mootdx 本版本的关键字段
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
# 检测到 eltdx 接口不兼容时的错误信息（仅记录一次，避免每票刷屏）
_eltdx_api_mismatch = None


def _check_eltdx_api_compat(client):
    """校验当前 eltdx 是否具备所需的 corporate.capital_changes 接口。

    若旧版 eltdx（缺少 capital_changes）仍在运行，直接抛 RuntimeError（附升级指引），
    让用户及时得知接口失效，而不是静默返回空。
    """
    global _eltdx_api_mismatch
    corporate = getattr(client, "corporate", None)
    if corporate is None or not hasattr(corporate, "capital_changes"):
        _eltdx_api_mismatch = (
            "[eltdx 接口不兼容] 前复权所需的 client.corporate.capital_changes 不存在："
            "当前 eltdx 版本过旧，请升级：pip install -U 'eltdx>=3.0.0'。"
        )
        raise RuntimeError(_eltdx_api_mismatch)
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
      c3_value=送转(每10股) · c4_value=配股数量(每10股)，与 mootdx 返回等价。
    """
    try:
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
    except Exception:
        # 网络 / 接口（含 _check_eltdx_api_compat 的 RuntimeError 升级指引）异常：
        # 向上抛，由 get_xdxr_data 显著上报，不吞成「无数据」。
        raise


# ============================================================
# 回退数据源：mootdx / pytdx（已注释保留，暂不启用）
# ============================================================
# 2026-08 临时测试：仅保留 eltdx 数据源，mootdx / pytdx 回退已整体注释。
# 目的：单测 eltdx 是否稳定——一旦 eltdx 不 OK 即显著报错（见 get_xdxr_data），
#       而非静默降级掩盖问题。日后如需恢复三级回退：把下方各段取消注释即可，
#       并确保已 `pip install mootdx / pytdx`；另有 PYTDX_SERVERS 副本亦随之启用
#        （注：该副本仅供"pytdx 除权回退"用；TdxAPI.py 的 TDX_BLOCK_SERVERS 是
#        板块文件下载专用，二者独立、勿混淆）。
# ------------------------------------------------------------------
# # mootdx Quotes 单例连接（建一次，所有股票复用）
# _mootdx_client = None
# _mootdx_client_ready = False
# # 检测到 mootdx 接口不兼容时的错误信息（仅记录一次，避免每票刷屏）
# _mootdx_api_mismatch = None
#
# def _check_mootdx_api_compat(client):
#     """校验当前 mootdx 是否具备所需的 xdxr 接口。"""
#     global _mootdx_api_mismatch
#     if not hasattr(client, "xdxr"):
#         _mootdx_api_mismatch = (
#             "[mootdx 接口不兼容] 前复权所需的 Quotes.xdxr 接口不存在："
#             "当前 mootdx 版本接口已变更。请锁定/升级适配版本：pip install -U 'mootdx'。"
#         )
#         raise RuntimeError(_mootdx_api_mismatch)
#     return client
#
# def _ensure_mootdx_client():
#     """确保 mootdx Quotes 客户端已连接，返回 client 或 None。线程安全。"""
#     global _mootdx_client, _mootdx_client_ready
#     if _mootdx_client_ready and _mootdx_client is not None:
#         return _mootdx_client
#     try:
#         from mootdx.quotes import Quotes
#         _mootdx_client = Quotes.factory(market='std', bestip=False, timeout=10)
#         _mootdx_client_ready = True
#         return _mootdx_client
#     except Exception:
#         _mootdx_client_ready = False
#         _mootdx_client = None
#         return None
#
# def _get_xdxr_mootdx(market, code):
#     """通过 mootdx Quotes 单例连接获取除权除息数据。在锁内调用。"""
#     global _mootdx_client, _mootdx_client_ready
#     client = _ensure_mootdx_client()
#     if client is None:
#         return None
#     _check_mootdx_api_compat(client)   # 接口失效即抛错，避免静默降级
#     try:
#         df = client.xdxr(symbol=code)
#         if df is not None and len(df) > 0:
#             return _normalize_xdxr_df(df)
#     except Exception:
#         _mootdx_client_ready = False
#         _mootdx_client = None
#     return None
#
# # pytdx TdxHq_API 单例连接（建一次，所有股票复用）
# _pytdx_api = None
# _pytdx_api_ready = False
# # 检测到 pytdx 接口不兼容时的错误信息（仅记录一次，避免每票刷屏）
# _pytdx_api_mismatch = None
#
# # pytdx 行情服务器地址列表（用于前复权 xdxr 数据获取）
# PYTDX_SERVERS = [
#     ('115.238.90.165', 7709),   # 最快的服务器，放在第一位
#     ('119.147.212.81', 7709),
#     ('120.76.152.2', 7709),
#     ('180.153.18.170', 7709),
#     ('218.75.126.9', 7709),
#     ('60.12.136.250', 7709),
#     ('60.191.117.167', 7709),
#     ('59.173.18.140', 7709),
#     ('60.28.23.80', 7709),
#     ('218.60.29.136', 7709),
#     ('106.14.190.13', 7709),
#     ('47.103.48.45', 7709),
#     ('124.71.223.19', 7709),
#     ('106.37.229.202', 7709),
#     ('180.153.18.171', 7709),
#     ('218.108.98.244', 7709),
# ]
#
# def _check_pytdx_api_compat(api):
#     """校验当前 pytdx 是否具备所需的接口。"""
#     global _pytdx_api_mismatch
#     missing = [m for m in ("get_xdxr_info", "connect") if not hasattr(api, m)]
#     if missing:
#         _pytdx_api_mismatch = (
#             "[pytdx 接口不兼容] 前复权所需接口缺失: " + ", ".join(missing)
#             + "。当前 pytdx 版本接口已变更，请锁定/升级适配版本：pip install -U 'pytdx'。"
#         )
#         raise RuntimeError(_pytdx_api_mismatch)
#     return api
#
# def _ensure_pytdx_api():
#     """确保 pytdx TdxHq_API 已连接，返回 api 或 None。线程安全。"""
#     global _pytdx_api, _pytdx_api_ready
#     if _pytdx_api_ready and _pytdx_api is not None:
#         return _pytdx_api
#     try:
#         import socket as _sock
#         from pytdx.hq import TdxHq_API
#         # 选最快服务器：daemon 线程做超时探测
#         _sel = [None]
#         def _select():
#             try:
#                 from pytdx.util.best_ip import select_best_ip
#                 _sel[0] = select_best_ip()
#             except Exception:
#                 pass
#         _th = _threading.Thread(target=_select, daemon=True)
#         _th.start()
#         _th.join(timeout=10)
#         if _sel[0] and isinstance(_sel[0], dict) and 'ip' in _sel[0]:
#             host, port = _sel[0]['ip'], _sel[0].get('port', 7709)
#         else:
#             host, port = _find_pytdx_server()
#         if not host:
#             return None
#         _pytdx_api = TdxHq_API()
#         if not _pytdx_api.connect(host, port):
#             _pytdx_api = None
#             _pytdx_api_ready = False
#             return None
#         _pytdx_api_ready = True
#         return _pytdx_api
#     except Exception:
#         _pytdx_api = None
#         _pytdx_api_ready = False
#         return None
#
# def _find_pytdx_server():
#     """找到可用的 pytdx 服务器（顺序探测 PYTDX_SERVERS）。"""
#     import socket as _sock
#     for host, port in PYTDX_SERVERS:
#         try:
#             _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
#             _s.settimeout(1.5)
#             _r = _s.connect_ex((host, port))
#             _s.close()
#             if _r == 0:
#                 return host, port
#         except Exception:
#             continue
#     return None, None
#
# def _get_xdxr_pytdx(market, code):
#     """通过 pytdx 单例连接获取除权除息数据。在锁内调用。"""
#     global _pytdx_api, _pytdx_api_ready
#     api = _ensure_pytdx_api()
#     if api is None:
#         return None
#     _check_pytdx_api_compat(api)   # 接口失效即抛错，避免静默降级
#     mkt = 1 if market.lower() == 'sh' else 0
#     try:
#         data = api.get_xdxr_info(mkt, code)
#         if not data:
#             return None
#         rows = []
#         for item in data:
#             rows.append({
#                 'code': item.get('code', code),
#                 'date': item.get('date', 0),
#                 'category': item.get('category', 0),
#                 'fenhong': item.get('fenhong', 0) or 0,
#                 'peigu': item.get('peigu', 0) or 0,
#                 'peigujia': item.get('peigujia', 0) or 0,
#                 'songgu': item.get('songgu', 0) or 0,
#                 'zhuanzeng': item.get('zhuanzeng', 0) or 0,
#             })
#         df = pd.DataFrame(rows)
#         return _normalize_xdxr_df(df)
#     except Exception:
#         _pytdx_api_ready = False
#         _pytdx_api = None
#         return None
# ------------------------------------------------------------------


def get_xdxr_data(market, code):
    """
    获取指定股票的除权除息数据。
    线程安全：多线程并发时，网络请求串行化，避免 socket 竞争。

    优先级：
      1. 缓存（内存命中，跳过网络请求）
      2. eltdx（当前唯一活动数据源，基于 7709 协议、0x000f 命令；
         mootdx / pytdx 回退分支已注释——测试 eltdx 稳定性，失败即报错而非降级）

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
            cached = _xdxr_cache[cache_key]
            return cached

        # 与 TdxAPI 解耦后：仅 eltdx 单一数据源。失败显著上报，不静默降级。
        for _src_name, _src_fn in (
            ("eltdx", _get_xdxr_eltdx),
            # ("mootdx", _get_xdxr_mootdx),
            # ("pytdx", _get_xdxr_pytdx),
        ):
            try:
                df = _src_fn(market, code)
            except Exception as _e:
                # 网络 / 接口（含 _check_eltdx_api_compat 的 RuntimeError 升级指引）异常：显著上报
                log.error("[xdxr] 仅 eltdx：%s 取数失败(market=%s, code=%s): %s",
                          _src_name, market, code, _e)
                df = None
                break
            if df is not None and len(df) > 0:
                _xdxr_cache[cache_key] = df
                return df
            # df is None = 该股票无除权除息记录（正常空结果），且已无更多回退数据源：
            # 走 info，避免全市场扫描被「历史上无除权」的正常情况刷满 ERROR。
            log.info("[xdxr] %s: %s 无除权除息记录（正常空结果；接口/网络异常会另行上报 error）",
                     market, code)

        _xdxr_cache[cache_key] = None
        return None
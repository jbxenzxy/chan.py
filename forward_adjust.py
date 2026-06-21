"""
通达信前复权模块 - forward_adjust.py
========================================
基于通达信除权除息数据（xdxr），对原始K线进行前复权处理。

核心原理（通达信官方公式，符合交易所"股东财富不变"原则）：
  前复权后价格 = (复权前价格 - 每股现金红利 + 配股比例 × 配股价)
                / (1 + 送股比例 + 转增比例 + 配股比例)

  转化为 y = a × x + b 的线性形式：
    a = 1 / (1 + 送股比例 + 转增比例 + 配股比例)
    b = (配股比例 × 配股价 - 每股现金红利) / (1 + 送股比例 + 转增比例 + 配股比例)

前复权递推方式：
  从最新日期向前，遇到除权除息日时，该日之前的所有OHLC都乘以 a 再加 b。

数据获取策略（按优先级）：
  1. mootdx Quotes 网络接口（优先，已验证可用）
  2. pytdx 网络接口（自动测速）

使用方法：
  from forward_adjust import apply_forward_adjust
  records = apply_forward_adjust(records, market="sh", code="600000")
"""

import os
import math
import socket
import logging
import warnings
import pandas as pd
from datetime import datetime

# ============================================================
# 配置
# ============================================================
TDX_DIR = r"C:\new_tdx_test"

# pytdx 行情服务器地址列表
PYTDX_SERVERS = [
    ('119.147.212.81', 7709),
    ('120.76.152.2', 7709),
    ('115.238.90.165', 7709),
    ('180.153.18.170', 7709),
    ('218.75.126.9', 7709),
    ('60.12.136.250', 7709),
    ('60.191.117.167', 7709),
    ('59.173.18.140', 7709),
    ('60.28.23.80', 7709),
    ('218.60.29.136', 7709),
    ('106.14.190.13', 7709),
    ('47.103.48.45', 7709),
    ('124.71.223.19', 7709),
    ('106.37.229.202', 7709),
    ('180.153.18.171', 7709),
    ('218.108.98.244', 7709),
]


# ============================================================
# mootdx / pytdx 返回的列名可能不同，统一标准化
# ============================================================
def _normalize_xdxr_df(df):
    """
    将 mootdx 或 pytdx 返回的 DataFrame 列名统一为标准列名。

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
# 策略1: mootdx Quotes 网络接口（优先）
# ============================================================
def _get_xdxr_mootdx(market, code):
    """通过 mootdx Quotes 网络接口获取除权除息数据"""
    try:
        from mootdx.quotes import Quotes
    except ImportError:
        return None

    import threading as _threading

    # 用 bestip=False 绕过 pytdx 的 select_best_ip（它可能因版本冲突卡死）
    # 10s 超时兜底，防止 factory() 内部阻塞
    for bestip, timeout_val in [(False, 10)]:
        result = [None]

        def _fetch():
            try:
                client = Quotes.factory(market='std', bestip=bestip, timeout=timeout_val)
                df = client.xdxr(symbol=code)
                if df is not None and len(df) > 0:
                    result[0] = _normalize_xdxr_df(df)
            except Exception:
                pass

        t = _threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(timeout=timeout_val + 5)
        if result[0] is not None:
            return result[0]

    return None


# ============================================================
# 策略2: pytdx 网络接口（自动测速，备用）
# ============================================================
def _find_pytdx_server():
    """找到可用的 pytdx 服务器，抑制版本冲突警告"""
    # 抑制 pytdx 内部版本冲突的日志刷屏
    for name in ['pytdx', 'mootdx']:
        logger = logging.getLogger(name)
        logger.setLevel(logging.ERROR)

    import threading as _threading
    result = [None]

    def _select():
        try:
            from pytdx.util.best_ip import select_best_ip
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result[0] = select_best_ip()
        except Exception:
            pass

    t = _threading.Thread(target=_select, daemon=True)
    t.start()
    t.join(timeout=10)

    if result[0] and isinstance(result[0], dict) and 'ip' in result[0]:
        host = result[0]['ip']
        port = result[0].get('port', 7709)
        return host, port

    # TCP 扫描列表
    for host, port in PYTDX_SERVERS:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.5)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                return host, port
        except Exception:
            continue

    return None, None


def _get_xdxr_pytdx(market, code):
    """通过 pytdx 网络接口获取除权除息数据"""
    try:
        from pytdx.hq import TdxHq_API
    except ImportError:
        return None

    host, port = _find_pytdx_server()
    if not host:
        return None

    mkt = 1 if market.lower() == 'sh' else 0

    try:
        api = TdxHq_API()
        if not api.connect(host, port):
            return None

        data = api.get_xdxr_info(mkt, code)
        api.disconnect()

        if not data:
            return None

        rows = []
        for item in data:
            rows.append({
                'code': item.get('code', code),
                'date': item.get('date', 0),
                'category': item.get('category', 0),
                'fenhong': item.get('fenhong', 0) or 0,
                'peigu': item.get('peigu', 0) or 0,
                'peigujia': item.get('peigujia', 0) or 0,
                'songgu': item.get('songgu', 0) or 0,
                'zhuanzeng': item.get('zhuanzeng', 0) or 0,
            })
        df = pd.DataFrame(rows)
        return _normalize_xdxr_df(df)

    except Exception:
        try:
            api.disconnect()
        except Exception:
            pass
        return None


# ============================================================
# 统一入口：获取除权除息数据
# ============================================================

# xdxr 独立缓存：key=(market, code)，同一股票跨周期不重复拉取
# 除权除息是历史数据（已发生的事件不会变），永久有效，冷启动时自然刷新
_xdxr_cache = {}

def get_xdxr_data(market, code):
    """
    获取指定股票的除权除息数据。

    优先级：
      1. 缓存（内存命中，跳过网络请求）
      2. mootdx Quotes（优先，已验证可用）
      3. pytdx（自动测速，备用）

    返回 pandas DataFrame，统一列名：
      date, category, fenhong, peigu, peigujia, songgu, zhuanzeng
    其中 fenhong/songgu/zhuanzeng/peigu 均为"每10股"单位。
    返回 None 表示无除权除息数据或所有方法均失败。
    """
    # 仅沪深市场有 xdxr 数据源（mootdx/pytdx 不支持港股 ds）
    if market.lower() not in ('sh', 'sz'):
        return None

    cache_key = (market, code)
    if cache_key in _xdxr_cache:
        return _xdxr_cache[cache_key]

    # 策略1: mootdx（优先）
    df = _get_xdxr_mootdx(market, code)
    if df is not None and len(df) > 0:
        _xdxr_cache[cache_key] = df
        return df

    # 策略2: pytdx（备用）
    df = _get_xdxr_pytdx(market, code)
    if df is not None and len(df) > 0:
        _xdxr_cache[cache_key] = df
        return df

    # 无除权数据也缓存 None，避免重复重试
    _xdxr_cache[cache_key] = None
    return None


def get_float_shares_from_xdxr(market, code):
    """
    从 xdxr 数据中提取最新的流通股本（单位：股）。
    返回 None 表示无数据。

    与 gbbq 不同，这里直接复用已获取的 xdxr 数据（来自 get_xdxr_data 缓存），
    无需额外的网络请求或本地文件解密。
    xdxr 中每个事件都记录了 panhouliutong（盘后流通，单位：万股），
    取最新一条记录即为当前流通股本。
    """
    xdxr_df = get_xdxr_data(market, code)
    if xdxr_df is None or len(xdxr_df) == 0:
        return None

    # 按日期降序排列，取最新一条
    df_sorted = xdxr_df.sort_values('date', ascending=False)
    latest = df_sorted.iloc[0]

    # panhouliutong: 盘后流通股本（万股）
    shares_wan = latest.get('panhouliutong', 0)
    if shares_wan is None:
        shares_wan = 0
    try:
        shares_wan = float(shares_wan)
    except (ValueError, TypeError):
        return None

    if math.isnan(shares_wan) or shares_wan <= 0:
        return None

    return shares_wan * 10000  # 万股 → 股


# ============================================================
# 构建复权事件
# ============================================================
def _build_adjust_events(xdxr_df):
    """
    从 xdxr DataFrame 构建复权事件列表。
    每个事件包含：(日期, a系数, b系数)

    通达信前复权公式：
      a = 1 / (1 + 送股比例 + 转增比例 + 配股比例)
      b = (配股比例 × 配股价 - 每股现金红利) / (1 + 送股比例 + 转增比例 + 配股比例)

    注意：通达信送股/转增/配股/分红单位均为"每10股"，需除以10转为每股。
    """
    events = []

    for _, row in xdxr_df.iterrows():
        # 日期列已标准化为 'date'（datetime 对象）
        date_val = row['date']
        if date_val is None or (isinstance(date_val, float) and pd.isna(date_val)):
            continue
        if isinstance(date_val, datetime):
            event_date = date_val
        elif isinstance(date_val, str):
            date_str = date_val.replace('-', '').replace('/', '')
            if len(date_str) != 8:
                continue
            event_date = datetime.strptime(date_str, '%Y%m%d')
        else:
            date_str = str(int(date_val))
            if len(date_str) != 8:
                continue
            event_date = datetime.strptime(date_str, '%Y%m%d')

        # 每10股 → 每股，遇 NaN 视为 0
        def _safe_float(val):
            try:
                v = float(val)
            except (ValueError, TypeError):
                return 0.0
            if math.isnan(v) or math.isinf(v):
                return 0.0
            return v

        songgu = _safe_float(row.get('songgu', 0)) / 10.0
        zhuanzeng = _safe_float(row.get('zhuanzeng', 0)) / 10.0
        peigu = _safe_float(row.get('peigu', 0)) / 10.0
        peigujia = _safe_float(row.get('peigujia', 0))
        fenhong = _safe_float(row.get('fenhong', 0)) / 10.0

        # 通达信官方前复权公式（配股比例计入分母，符合交易所"股东财富不变"原则）：
        #   前复权后价格 = (复权前价格 - 每股现金红利 + 配股比例 × 配股价)
        #                 / (1 + 送股比例 + 转增比例 + 配股比例)
        total_ratio = songgu + zhuanzeng + peigu

        if total_ratio == 0 and fenhong == 0:
            continue

        if total_ratio == 0:
            a = 1.0
            b = -fenhong
        else:
            a = 1.0 / (1.0 + total_ratio)
            b = (peigu * peigujia - fenhong) / (1.0 + total_ratio)

        events.append((event_date, a, b))

    events.sort(key=lambda x: x[0])
    return events


# ============================================================
# 前复权主函数
# ============================================================
def apply_forward_adjust(records, market, code):
    """
    对原始K线数据进行前复权处理。

    参数:
      records: list[dict], 原始K线数据
               每条记录: dt, open, high, low, close, vol, amount
      market:  str, "sh" 或 "sz"
      code:    str, 6位股票代码

    返回:
      (records, did_adjust): records 为前复权后的K线数据（原地修改），
                             did_adjust 为 True 表示实际执行了复权处理
    """
    # 市场过滤：仅沪深个股支持前复权（港股 ds 无 xdxr 数据源，跳过）
    if market.lower() not in ('sh', 'sz'):
        return records, False

    # 入口过滤：根据 market 和 code 前缀判断是否为个股
    # mootdx xdxr 接口不区分 SH/SZ（000001 永远返回 000001.SZ 平安银行），
    # 必须用前缀规则在入口拦截，避免指数被错误复权
    # SH：6 开头为个股（600/601/603/605/688），其余为指数/ETF/板块
    # SZ：000/001/002/003/300/301 为个股，399 为指数，其余为 ETF/板块
    if market.lower() == 'sh' and not code.startswith('6'):
        return records, False
    if market.lower() == 'sz' and not code.startswith(('000', '001', '002', '003', '300', '301')):
        return records, False

    xdxr_df = get_xdxr_data(market, code)
    if xdxr_df is None or len(xdxr_df) == 0:
        return records, False

    events = _build_adjust_events(xdxr_df)
    if not events:
        return records, False

    import time as _time
    t_start = _time.time()
    print(f"[复权][信息] {code} 共{len(events)}个除权除息事件，开始前复权...")

    if len(records) > 1 and records[0]["dt"] > records[1]["dt"]:
        records.sort(key=lambda r: r["dt"])

    for event_date, a, b in reversed(events):
        adjusted_count = 0
        for r in records:
            if r["dt"] < event_date:
                r["open"] = r["open"] * a + b
                r["high"] = r["high"] * a + b
                r["low"] = r["low"] * a + b
                r["close"] = r["close"] * a + b
                adjusted_count += 1

    print(f"[复权][信息] {code} 前复权完成，耗时 {_time.time()-t_start:.3f}s")
    return records, True


# ============================================================
# 测试入口
# ============================================================
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from datetime import timedelta
    test_records = []
    base = datetime(2024, 1, 2)
    for i in range(60):
        dt = base + timedelta(days=i)
        price = 10.0 + i * 0.05
        test_records.append({
            "dt": dt,
            "open": price,
            "high": price + 0.1,
            "low": price - 0.1,
            "close": price,
            "vol": 1000000,
            "amount": price * 1000000,
        })

    print("=" * 60)
    print("前复权模块测试")
    print("=" * 60)

    result, did_adjust = apply_forward_adjust(test_records, market="sh", code="600000")

    print(f"\n复权状态: {'前复权' if did_adjust else '不复权'}")
    if result:
        print(f"\n测试结果（前3条）:")
        for r in result[:3]:
            print(f"  {r['dt'].strftime('%Y-%m-%d')} "
                  f"O={r['open']:.3f} H={r['high']:.3f} "
                  f"L={r['low']:.3f} C={r['close']:.3f}")

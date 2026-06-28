"""
通达信本地文件数据源适配器（供 chan.py 的 custom 数据源使用）
包含：适配器类 + 二进制文件读取 + 周期合成 + 前复权处理 + 数据加载管道

前复权功能：
  基于通达信除权除息数据（xdxr），对原始K线进行前复权处理。
  数据获取策略（按优先级）：mootdx Quotes 网络接口 -> pytdx 网络接口（自动测速）。
  通过 set_tdx_config(forward_adjust_enabled=True) 启用。

使用方法：将此文件放到 chan.py 仓库的 DataAPI/ 目录下
"""

import os
import math
import socket
import struct
import logging
import warnings
import pandas as pd
from datetime import datetime, timedelta
from collections import OrderedDict
import numpy as np
from chinese_calendar import is_holiday

from Common.CEnum import AUTYPE, KL_TYPE, DATA_FIELD
from DataAPI.CommonStockAPI import CCommonStockApi
from KLine.KLine_Unit import CKLine_Unit


# ============================================================
# 模块级配置（由 my_chan_main.py 调用 set_tdx_config() 设置）
# ============================================================
_tdx_config = {
    "vipdoc_dir": None,
    "forward_adjust_enabled": False,
}


def set_tdx_config(vipdoc_dir=None, forward_adjust_enabled=None):
    """由 my_chan_main.py 调用，设置通达信数据源所需的全局配置"""
    if vipdoc_dir is not None:
        _tdx_config["vipdoc_dir"] = vipdoc_dir
    if forward_adjust_enabled is not None:
        _tdx_config["forward_adjust_enabled"] = forward_adjust_enabled


# pytdx 行情服务器地址列表（用于前复权 xdxr 数据获取）
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
# 通达信数据读取
# ============================================================
def read_tdx_day_file(filepath, max_records=None, market=None):
    """
    读取通达信日线 .day 文件
    格式：每条记录32字节

    A股格式（sh/sz）：
      日期(I4) 开盘(I4) 最高(I4) 最低(I4) 收盘(I4) 成交额(f4) 成交量(I4) 保留(I4)
      价格需除以100得到实际价格（单位：元）

    扩展市场格式（港股/期货等，ds目录）：
      日期(I4) 开盘(f4) 最高(f4) 最低(f4) 收盘(f4) 成交量(I4) 成交额(f4) 结算价(f4)
      价格是float类型，直接就是实际价格，不需要除以100
    """
    is_ext_market = (market == 'hk')
    records = []
    with open(filepath, "rb") as f:
        data = f.read()
    record_size = 32
    total = len(data) // record_size
    print(f"[stock][调试] 文件: {filepath}, 大小: {len(data)}字节, record_size={record_size}, 总记录数: {total}, 市场={'扩展' if is_ext_market else '标准A股'}")

    if max_records and max_records < total:
        start = (total - max_records) * record_size
    else:
        start = 0
    for idx_offset, i in enumerate(range(start, len(data), record_size)):
        row = data[i:i + record_size]
        if len(row) < record_size:
            break
        try:
            year = 0
            month = 0
            day = 0
            if is_ext_market:
                # 扩展市场（港股/期货）：价格是float，直接是实际值
                date_int, o, h, l, c, vol, amount, jiesuan = struct.unpack('<IffffIfI', row)
            else:
                # 标准A股：价格是int，需除以100
                date_int, o, h, l, c, amount, vol, _ = struct.unpack('<IIIIIfII', row)
            year = date_int // 10000
            month = (date_int % 10000) // 100
            day = date_int % 100
            if year < 1990 or year > 2030 or month < 1 or month > 12 or day < 1 or day > 31:
                continue
            dt = datetime(year, month, day)
            if is_ext_market:
                # 扩展市场价格是float，直接使用（已经是实际价格）
                # float精度修正，保留3位小数
                o, h, l, c = round(o, 3), round(h, 3), round(l, 3), round(c, 3)
            else:
                # A股价格除以100，得到实际价格（单位：元）
                o, h, l, c = o / 100.0, h / 100.0, l / 100.0, c / 100.0
            # 通达信原始数据中偶尔存在OHLC不一致（如收盘价<最低价），
            # chan.py严格校验 high>=max(o,c) 且 low<=min(o,c)，需修正
            h = max(h, o, c)
            l = min(l, o, c)
            records.append({
                "dt": dt,
                "open": o, "high": h, "low": l, "close": c,
                "vol": vol, "amount": amount,
            })
        except (ValueError, OverflowError, struct.error):
            continue
    _check_and_report_gaps(records)
    return records


def _count_trading_days(prev_dt, curr_dt):
    """计算两个日期之间（不含两端）的A股交易日数（周一至周五且非法定节假日）"""
    count = 0
    d = prev_dt.date() + timedelta(days=1)
    end = curr_dt.date()
    while d < end:
        if d.weekday() < 5 and not is_holiday(d):
            count += 1
        d += timedelta(days=1)
    return count


def _check_and_report_gaps(records):
    """检测数据缺口（相邻记录间隔超过0个交易日即视为缺失K线），仅打印提示，不截断"""
    gap_indices = []
    for i in range(1, len(records)):
        prev_dt = records[i-1]["dt"]
        curr_dt = records[i]["dt"]
        gap_trading_days = _count_trading_days(prev_dt, curr_dt)
        if gap_trading_days > 20:  # 用 >0 可识别出个股各种停牌日期（因股东大会、筹划重大事项、重大事项紧急、重大资产重组等停牌）；但打印信息太多
            gap_indices.append((i, gap_trading_days))

    if gap_indices:
        print(f"[stock][警告] 检测到 {len(gap_indices)} 处数据缺口（请补全数据）:")
        for idx, (gi, gap_td) in enumerate(gap_indices):
            prev_dt = records[gi-1]["dt"].strftime("%Y-%m-%d")
            curr_dt = records[gi]["dt"].strftime("%Y-%m-%d")
            print(f"[stock][警告]   缺口{idx+1}: {prev_dt} -> {curr_dt} (间隔{gap_td}个交易日)")
    else:
        old_start = records[0]["dt"].strftime("%Y-%m-%d")
        old_end = records[-1]["dt"].strftime("%Y-%m-%d")
        print(f"[stock][信息] 检测到 0 处数据缺口")
        print(f"[stock][信息]   数据范围: {old_start} ~ {old_end} ({len(records)}条)")
    print(f"[stock][调试] 共解析有效记录: {len(records)}条")


def read_tdx_min_file(filepath, market="sh", aggregate_30m=True):
    """
    解析通达信5分钟线二进制文件(.lc5) -- numpy批量读取优化版
    通达信 .lc5 文件格式（每条记录 32 字节，小端序）：
      H(日期) + H(时间) + f(开) + f(高) + f(低) + f(收) + f(成交额) + I(成交量) + I(保留)
      日期编码: year = num // 2048 + 2004, month = (num % 2048) // 100, day = (num % 2048) % 100
      时间: 从0点开始的分钟数 (HH*60+MM)
      价格字段是 float 类型，直接使用
    aggregate_30m=True: 从5分钟线合成为30分钟线（默认，兼容旧行为）
    aggregate_30m=False: 直接返回5分钟线原始数据
    """
    import time as _time
    t0 = _time.time()

    record_size = 32
    with open(filepath, "rb") as f:
        raw = f.read()

    n_records = len(raw) // record_size
    if n_records == 0:
        return []

    dt = np.dtype([
        ("date_raw", "<u2"),
        ("time_raw", "<u2"),
        ("open", "<f4"),
        ("high", "<f4"),
        ("low", "<f4"),
        ("close", "<f4"),
        ("amount", "<f4"),
        ("vol", "<u4"),
        ("reserved", "<u4"),
    ])
    arr = np.frombuffer(raw[:n_records * record_size], dtype=dt)

    date_raw = arr["date_raw"]
    years = date_raw // 2048 + 2004
    months = (date_raw % 2048) // 100
    days = date_raw % 2048 % 100

    time_raw = arr["time_raw"]
    hours = time_raw // 60
    minutes = time_raw % 60

    valid = (
        (years >= 1990) & (years <= 2100) &
        (months >= 1) & (months <= 12) &
        (days >= 1) & (days <= 31) &
        (hours <= 23) & (minutes <= 59)
    )

    # 交易时间过滤：A股 9:30-11:30, 13:00-15:00；港股 9:30-12:00, 13:00-16:00
    if market == 'hk':
        valid &= (
            ((hours == 9) & (minutes >= 30)) |
            (hours == 10) |
            (hours == 11) |
            ((hours == 12) & (minutes == 0)) |
            (hours == 13) |
            (hours == 14) |
            (hours == 15) |
            ((hours == 16) & (minutes == 0))
        )
    else:
        valid &= (
            ((hours == 9) & (minutes >= 30)) |
            (hours == 10) |
            (hours == 11) |
            (hours == 13) |
            (hours == 14) |
            ((hours == 15) & (minutes == 0))
        )

    years = years[valid]
    months = months[valid]
    days = days[valid]
    hours = hours[valid]
    minutes = minutes[valid]
    opens = arr["open"][valid]
    highs = arr["high"][valid]
    lows = arr["low"][valid]
    closes = arr["close"][valid]
    amounts = arr["amount"][valid]
    vols = arr["vol"][valid]

    df = np.column_stack([years, months, days, hours, minutes, opens, highs, lows, closes, vols, amounts])

    if len(df) == 0:
        return []

    records = []
    for row in df:
        yr, mo, dy, hr, mn, o, h, l, c, v, a = row
        dt_obj = datetime(int(yr), int(mo), int(dy), int(hr), int(mn))
        # OHLC一致性修正（通达信原始数据偶尔存在close<low或close>high）
        h = max(float(h), float(o), float(c))
        l = min(float(l), float(o), float(c))
        records.append({
            "dt": dt_obj,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "vol": int(v),
            "amount": float(a),
        })

    # 如果不需要合成30分钟线，直接返回5分钟线原始数据
    if not aggregate_30m:
        result = []
        for r in records:
            result.append({
                "dt": r["dt"],
                "open": round(r["open"], 3),
                "high": round(r["high"], 3),
                "low": round(r["low"], 3),
                "close": round(r["close"], 3),
                "vol": r["vol"],
                "amount": round(r["amount"], 2),
            })
        print(f"[耗时] 读取5分钟线: {_time.time()-t0:.3f}s")
        return result

    # 合成30分钟线
    # A股交易时间: 9:30-11:30, 13:00-15:00
    # 港股交易时间: 9:30-12:00, 13:00-16:00
    if market == 'hk':
        def _bucket_30min(dt_obj):
            h, m = dt_obj.hour, dt_obj.minute
            if h == 9:
                return dt_obj.replace(minute=0, hour=10)
            elif h == 10:
                return dt_obj.replace(minute=0, hour=10) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=11)
            elif h == 11:
                return dt_obj.replace(minute=0, hour=11) if m == 0 else dt_obj.replace(minute=30)
            elif h == 12:
                return dt_obj.replace(minute=0)
            elif h == 13:
                return dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=14)
            elif h == 14:
                return dt_obj.replace(minute=0, hour=14) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=15)
            elif h == 15:
                return dt_obj.replace(minute=0, hour=15) if m == 0 else dt_obj.replace(minute=30)
            elif h == 16:
                return dt_obj.replace(minute=0)
            return dt_obj
    else:
        def _bucket_30min(dt_obj):
            h, m = dt_obj.hour, dt_obj.minute
            if h == 9:
                return dt_obj.replace(minute=0, hour=10)
            elif h == 10:
                return dt_obj.replace(minute=0, hour=10) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=11)
            elif h == 11:
                return dt_obj.replace(minute=0, hour=11) if m == 0 else dt_obj.replace(minute=30)
            elif h == 13:
                return dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=14)
            elif h == 14:
                return dt_obj.replace(minute=0, hour=14) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=15)
            elif h == 15:
                return dt_obj.replace(minute=0)
            return dt_obj

    for r in records:
        r["bucket"] = _bucket_30min(r["dt"])

    buckets = OrderedDict()
    for r in records:
        b = r["bucket"]
        if b not in buckets:
            buckets[b] = {
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "vol": r["vol"], "amount": r["amount"],
            }
        else:
            buckets[b]["high"] = max(buckets[b]["high"], r["high"])
            buckets[b]["low"] = min(buckets[b]["low"], r["low"])
            buckets[b]["close"] = r["close"]
            buckets[b]["vol"] += r["vol"]
            buckets[b]["amount"] += r["amount"]

    result = []
    for b, v in buckets.items():
        o2, h2, l2, c2 = v["open"], v["high"], v["low"], v["close"]
        h2 = max(h2, o2, c2)
        l2 = min(l2, o2, c2)
        result.append({
            "dt": b,
            "open": round(o2, 3),
            "high": round(h2, 3),
            "low": round(l2, 3),
            "close": round(c2, 3),
            "vol": v["vol"],
            "amount": round(v["amount"], 2),
        })

    print(f"[耗时] 读取并合成30分钟线: {_time.time()-t0:.3f}s")
    return result


def _resample_5m_to_30m(records, market="sh"):
    """
    将5分钟K线合成为30分钟K线（从 read_tdx_min_file 中提取的独立函数）
    供外部在5分钟前复权后调用，避免对30分钟K线做二次复权
    """
    import time as _time

    if not records:
        return []

    t0 = _time.time()

    # A股交易时间: 9:30-11:30, 13:00-15:00
    # 港股交易时间: 9:30-12:00, 13:00-16:00
    if market == 'hk':
        def _bucket_30min(dt_obj):
            h, m = dt_obj.hour, dt_obj.minute
            if h == 9:
                return dt_obj.replace(minute=0, hour=10)
            elif h == 10:
                return dt_obj.replace(minute=0, hour=10) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=11)
            elif h == 11:
                return dt_obj.replace(minute=0, hour=11) if m == 0 else dt_obj.replace(minute=30)
            elif h == 12:
                return dt_obj.replace(minute=0)
            elif h == 13:
                return dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=14)
            elif h == 14:
                return dt_obj.replace(minute=0, hour=14) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=15)
            elif h == 15:
                return dt_obj.replace(minute=0, hour=15) if m == 0 else dt_obj.replace(minute=30)
            elif h == 16:
                return dt_obj.replace(minute=0)
            return dt_obj
    else:
        def _bucket_30min(dt_obj):
            h, m = dt_obj.hour, dt_obj.minute
            if h == 9:
                return dt_obj.replace(minute=0, hour=10)
            elif h == 10:
                return dt_obj.replace(minute=0, hour=10) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=11)
            elif h == 11:
                return dt_obj.replace(minute=0, hour=11) if m == 0 else dt_obj.replace(minute=30)
            elif h == 13:
                return dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=14)
            elif h == 14:
                return dt_obj.replace(minute=0, hour=14) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=15)
            elif h == 15:
                return dt_obj.replace(minute=0)
            return dt_obj

    for r in records:
        r["bucket"] = _bucket_30min(r["dt"])

    buckets = OrderedDict()
    for r in records:
        b = r["bucket"]
        if b not in buckets:
            buckets[b] = {
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "vol": r["vol"], "amount": r["amount"],
            }
        else:
            buckets[b]["high"] = max(buckets[b]["high"], r["high"])
            buckets[b]["low"] = min(buckets[b]["low"], r["low"])
            buckets[b]["close"] = r["close"]
            buckets[b]["vol"] += r["vol"]
            buckets[b]["amount"] += r["amount"]

    result = []
    for b, v in buckets.items():
        o2, h2, l2, c2 = v["open"], v["high"], v["low"], v["close"]
        h2 = max(h2, o2, c2)
        l2 = min(l2, o2, c2)
        result.append({
            "dt": b,
            "open": round(o2, 3),
            "high": round(h2, 3),
            "low": round(l2, 3),
            "close": round(c2, 3),
            "vol": v["vol"],
            "amount": round(v["amount"], 2),
        })

    # 清理临时 bucket 属性
    for r in records:
        r.pop("bucket", None)

    print(f"[耗时] 合成30分钟线: {_time.time()-t0:.3f}s")
    return result


def _resample_day_to_week(day_records):
    """
    将日线数据合成为周线数据
    规则：每周一到周五的数据合成一根周K线
    - 周开盘价 = 本周第一个交易日的开盘价
    - 周收盘价 = 本周最后一个交易日的收盘价
    - 周最高价 = 本周最高价的最大值
    - 周最低价 = 本周最低价的最小值
    - 周成交量 = 本周成交量之和
    - 周成交额 = 本周成交额之和
    - 周显示日期 = 本周最后一个交易日（周五或最新）
    """
    if not day_records:
        return []
    import datetime

    weeks = OrderedDict()
    for r in day_records:
        dt = r["dt"]
        # 计算该日期所在周的周一
        monday = dt - datetime.timedelta(days=dt.weekday())
        week_key = monday.strftime("%Y-%m-%d")
        if week_key not in weeks:
            weeks[week_key] = {
                "dt": monday,
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "vol": r["vol"],
                "amount": r["amount"],
                "last_dt": dt,
            }
        else:
            weeks[week_key]["high"] = max(weeks[week_key]["high"], r["high"])
            weeks[week_key]["low"] = min(weeks[week_key]["low"], r["low"])
            weeks[week_key]["close"] = r["close"]
            weeks[week_key]["vol"] += r["vol"]
            weeks[week_key]["amount"] += r["amount"]
            weeks[week_key]["last_dt"] = dt

    result = []
    for week_key, v in weeks.items():
        o2, h2, l2, c2 = v["open"], v["high"], v["low"], v["close"]
        h2 = max(h2, o2, c2)
        l2 = min(l2, o2, c2)
        result.append({
            "dt": v["last_dt"],
            "open": round(o2, 2),
            "high": round(h2, 2),
            "low": round(l2, 2),
            "close": round(c2, 2),
            "vol": v["vol"],
            "amount": round(v["amount"], 2),
        })
    return result


# ============================================================
# 前复权模块
# 核心原理（通达信官方公式，符合交易所"股东财富不变"原则）：
#   前复权后价格 = (复权前价格 - 每股现金红利 + 配股比例 × 配股价)
#                 / (1 + 送股比例 + 转增比例 + 配股比例)
# 前复权递推方式：从最新日期向前，遇到除权除息日时，该日之前的所有OHLC都乘以 a 再加 b。
# 数据获取策略（按优先级）：
#   1. mootdx Quotes 网络接口（优先，已验证可用）
#   2. pytdx 网络接口（自动测速）
# ============================================================

# mootdx / pytdx 返回的列名可能不同，统一标准化
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


# 策略1: mootdx Quotes 网络接口（优先）
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


# 策略2: pytdx 网络接口（自动测速，备用）
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


# 构建复权事件
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


# 前复权主函数
def _forward_adjust(records, market, code):
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


def find_day_file(market, code):
    """查找日线数据文件路径"""
    if market == 'hk':
        return os.path.join(_tdx_config["vipdoc_dir"], "ds", "lday", f"31#{code}.day")
    return os.path.join(_tdx_config["vipdoc_dir"], market, "lday", f"{market}{code}.day")


def read_main_level_records(market, code, freq, return_raw=False):
    """
    从通达信文件读取主级别K线数据，含前复权和周期合成。
    各周期数据加载 + 前复权（统一在原始数据层面处理，避免二次复权）。

    当 return_raw=True 时：
      - freq='30m': 返回 (records_30m, raw_5m) 元组，raw_5m 是前复权后的5m数据
      - freq='w':    返回 (records_w, raw_d) 元组，raw_d 是前复权后的日线数据
    供双窗口子级别复用，避免重复读取和二次复权。
    """
    if freq in ('30m', '5m'):
        if market == 'hk':
            data_file = os.path.join(_tdx_config["vipdoc_dir"], "ds", "fzline", f"31#{code}.lc5")
        else:
            data_file = os.path.join(_tdx_config["vipdoc_dir"], market, "fzline", f"{market}{code}.lc5")
        if not os.path.exists(data_file):
            return [] if not return_raw else ([], [])
    else:
        data_file = find_day_file(market, code)
        if not os.path.exists(data_file):
            return [] if not return_raw else ([], [])

    if freq == '30m':
        raw_5m = read_tdx_min_file(data_file, market=market, aggregate_30m=False)
        if _tdx_config["forward_adjust_enabled"]:
            raw_5m, _ = _forward_adjust(raw_5m, market=market, code=code)
        records_30m = _resample_5m_to_30m(list(raw_5m), market=market)
        if return_raw:
            return records_30m, raw_5m
        return records_30m
    elif freq == '5m':
        records = read_tdx_min_file(data_file, market=market, aggregate_30m=False)
        if _tdx_config["forward_adjust_enabled"]:
            records, _ = _forward_adjust(records, market=market, code=code)
    elif freq == 'w':
        records = read_tdx_day_file(data_file, market=market)
        if _tdx_config["forward_adjust_enabled"]:
            records, _ = _forward_adjust(records, market=market, code=code)
        records_w = _resample_day_to_week(records)
        if return_raw:
            return records_w, records
        return records_w
    else:
        records = read_tdx_day_file(data_file, market=market)
        if _tdx_config["forward_adjust_enabled"]:
            records, _ = _forward_adjust(records, market=market, code=code)
    return records


def read_sub_level_records(market, code, freq, sub_freq, records):
    """
    双窗口模式：加载子级别K线数据。
    返回与主级别相同时间范围的子级别records列表。
    数据来源与主级别一致：从通达信原始文件读取，做前复权处理。
    """
    if sub_freq in ('30m', '5m'):
        if market == 'hk':
            min_file = os.path.join(_tdx_config["vipdoc_dir"], "ds", "fzline", f"31#{code}.lc5")
        else:
            min_file = os.path.join(_tdx_config["vipdoc_dir"], market, "fzline", f"{market}{code}.lc5")
        if not os.path.exists(min_file):
            print(f"[stock][警告] 子级别数据文件不存在: {min_file}")
            return None
        sub_records = read_tdx_min_file(min_file, market=market, aggregate_30m=False)
        if _tdx_config["forward_adjust_enabled"]:
            sub_records, _ = _forward_adjust(sub_records, market=market, code=code)
        if sub_freq == '30m':
            sub_records = _resample_5m_to_30m(sub_records, market=market)
    elif sub_freq == 'd':
        day_file = find_day_file(market, code)
        if not os.path.exists(day_file):
            print(f"[stock][警告] 子级别数据文件不存在: {day_file}")
            return None
        sub_records = read_tdx_day_file(day_file, market=market)
        if _tdx_config["forward_adjust_enabled"]:
            sub_records, _ = _forward_adjust(sub_records, market=market, code=code)
    else:
        return None

    if len(sub_records) < 5:
        print(f"[stock][警告] 子级别数据不足({len(sub_records)}条)")
        return None

    # 过滤到与主级别相同的时间范围（略大一点，确保边界包含）
    if records:
        main_start = records[0]["dt"]
        main_end = records[-1]["dt"]
        sub_records = [r for r in sub_records if main_start - timedelta(days=1) <= r["dt"] <= main_end + timedelta(days=1)]

    print(f"[stock][信息] 子级别({sub_freq})数据加载: {len(sub_records)}条")
    return sub_records


# ============================================================
# 适配器类
# ============================================================
class CTdxAPI(CCommonStockApi):
    """通达信本地文件数据源适配器"""

    # 类变量，由 my_chan_main.py 外部设置
    _tdx_data = None  # list of dict 或 dict of {KL_TYPE: list of dict}

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        super().__init__(code, k_type, begin_date, end_date, autype)
        self.is_stock = True

    @classmethod
    def set_data(cls, data):
        """设置K线数据（由 my_chan_main.py 调用）
        data 可以是:
        - list of dict: 单级别模式，所有级别共用同一份数据
        - dict of {KL_TYPE: list of dict}: 多级别模式，按 k_type 区分
        """
        cls._tdx_data = data

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass

    def SetBasciInfo(self):
        self.is_stock = True

    def _get_records(self):
        """根据 self.k_type 从 _tdx_data 中取出对应级别的 records"""
        if not CTdxAPI._tdx_data:
            return []
        if isinstance(CTdxAPI._tdx_data, dict):
            # 多级别模式：按 k_type 取对应级别的数据
            return CTdxAPI._tdx_data.get(self.k_type, [])
        else:
            # 单级别模式：直接使用
            return CTdxAPI._tdx_data

    def get_kl_data(self):
        """逐根 yield 返回 CKLine_Unit"""
        from Common.CTime import CTime
        for row in self._get_records():
            dt = row["dt"]
            time = CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute)
            klu_dict = {
                DATA_FIELD.FIELD_TIME: time,
                DATA_FIELD.FIELD_OPEN: row["open"],
                DATA_FIELD.FIELD_CLOSE: row["close"],
                DATA_FIELD.FIELD_HIGH: row["high"],
                DATA_FIELD.FIELD_LOW: row["low"],
                DATA_FIELD.FIELD_VOLUME: row["vol"],
                DATA_FIELD.FIELD_TURNOVER: row["amount"],
            }
            yield CKLine_Unit(klu_dict)


class CTdxAPI_Sliced(CTdxAPI):
    """
    切片数据源适配器：只返回从指定日期开始的K线数据。
    用于双击选点后，基于 chan.py 内部算法重新计算中枢/线段/买卖点。
    """
    _sliced_data = []

    @classmethod
    def set_sliced_data(cls, records, start_dt):
        """
        设置切片数据：只保留从 start_dt 开始的记录（含等于）

        Args:
            records: 完整的K线记录列表
            start_dt: 开始日期时间（datetime对象）
        """
        cls._sliced_data = [r for r in records if r["dt"] >= start_dt]
        print(f"[信息] 切片数据源: 从 {start_dt} 开始，共 {len(cls._sliced_data)} 条K线")

    @classmethod
    def clear_sliced_data(cls):
        """清空切片数据"""
        cls._sliced_data = []

    def get_kl_data(self):
        """逐根 yield 返回切片后的 CKLine_Unit"""
        if not CTdxAPI_Sliced._sliced_data:
            return
        from Common.CTime import CTime
        for row in CTdxAPI_Sliced._sliced_data:
            dt = row["dt"]
            time = CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute)
            klu_dict = {
                DATA_FIELD.FIELD_TIME: time,
                DATA_FIELD.FIELD_OPEN: row["open"],
                DATA_FIELD.FIELD_CLOSE: row["close"],
                DATA_FIELD.FIELD_HIGH: row["high"],
                DATA_FIELD.FIELD_LOW: row["low"],
                DATA_FIELD.FIELD_VOLUME: row["vol"],
                DATA_FIELD.FIELD_TURNOVER: row["amount"],
            }
            yield CKLine_Unit(klu_dict)


# ============================================================
if __name__ == "__main__":
    print("通达信本地文件数据源模块已加载。")
    print("可用功能：")
    print("  - CTdxAPI / CTdxAPI_Sliced: 适配器类")
    print("  - set_tdx_config(): 设置模块级配置（vipdoc_dir, forward_adjust_enabled）")
    print("  - read_tdx_day_file(): 读取日线 .day 文件")
    print("  - read_tdx_min_file(): 读取分钟线 .lc5 文件")
    print("  - read_main_level_records(): 主级别数据加载管道")
    print("  - read_sub_level_records(): 子级别数据加载管道")
    print("  - _resample_day_to_week(): 日线->周线合成")
    print("  - _resample_5m_to_30m(): 5m->30m 合成")
    print("")
    print("前复权功能：")
    print("  - _forward_adjust(): 对原始K线进行前复权处理")
    print("  - get_xdxr_data(): 获取除权除息数据（mootdx/pytdx）")
    print("  - 通过 set_tdx_config(forward_adjust_enabled=True) 启用前复权")
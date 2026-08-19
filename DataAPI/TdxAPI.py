"""
通达信本地文件数据源适配器（供 chan.py 的 custom 数据源使用）
包含：适配器类 + 二进制文件读取 + 周期合成 + 前复权处理 + 数据加载管道

前复权功能：
  基于通达信除权除息数据（xdxr），对原始K线进行前复权处理。
  数据获取策略（按优先级）：eltdx -> mootdx Quotes 网络接口 -> pytdx 网络接口（自动测速）。
  通过 set_tdx_config(forward_adjust_enabled=True) 启用。

使用方法：将此文件放到 chan.py 仓库的 DataAPI/ 目录下
"""

import os
import re
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

# 通达信研究行业 X代码↔881代码映射表（从官方PDF 3.6节提取）
# ── 阶段 5（设计 8.8/4.1）：tdxhy_mapping_data.py 已整体迁入 App/ 目录 ──
# 本模块不再按自身目录寻址加载（原 try-import App 反向依赖 + 失败静默降级
# 空表，设计 4.4 依赖方向违反）；映射数据改由 App 层单一加载函数
# AppData.load_tdxhy_mapping() 加载后，经 set_tdx_hy_mapping 注入（与
# set_tdx_config 同一注入模式，DataAPI 与 App 互不依赖）。
_TDXHY_X_TO_881 = {}
_TDXHY_881_TO_X = {}


def set_tdx_hy_mapping(x_to_881=None, to_x=None):
    """由 my_chan_main.py 启动时调用，注入通达信研究行业映射表

    数据文件 App/tdxhy_mapping_data.py 由 AppData.load_tdxhy_mapping() 单一
    加载（设计 8.8），本函数只收值不寻址；注入对象直存（同一 dict 身份），
    注入前调用方为空表（跨层依赖方向保持 DataAPI → 不 import App）。

    fail-fast 注入模式（设计 8.4 根除静默降级）：任一侧注入后若双表仍
    存在空表，直接抛 ValueError，不再以空表继续运行。
    """
    global _TDXHY_X_TO_881, _TDXHY_881_TO_X
    if x_to_881:
        if not to_x and not _TDXHY_881_TO_X:
            raise ValueError("行业映射 to_x 为空，注入失败（静默降级已禁止）")
        _TDXHY_X_TO_881 = x_to_881
    if to_x:
        if not x_to_881 and not _TDXHY_X_TO_881:
            raise ValueError("行业映射 x_to_881 为空，注入失败（静默降级已禁止）")
        _TDXHY_881_TO_X = to_x
    if not _TDXHY_X_TO_881 or not _TDXHY_881_TO_X:
        raise ValueError("行业映射数据为空，注入失败（静默降级已禁止）")


def get_tdx_hy_mapping():
    """返回当前行业映射的只读副本 (x_to_881, to_x)（守护用例/调试用）"""
    return dict(_TDXHY_X_TO_881), dict(_TDXHY_881_TO_X)


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
    ('115.238.90.165', 7709),   # 最快的服务器，放在第一位
    ('119.147.212.81', 7709),
    ('120.76.152.2', 7709),
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

    扩展市场格式（港股/通达信扩展指数等，ds目录）：
      日期(I4) 开盘(f4) 最高(f4) 最低(f4) 收盘(f4) 成交额(f4) 成交量(I4) 结算价(I4)
      价格是float类型，直接就是实际价格，不需要除以100
    """
    is_ext_market = (market in ('hk', 'ds'))
    records = []
    with open(filepath, "rb") as f:
        data = f.read()
    record_size = 32
    total = len(data) // record_size
    print(f"[调试] 文件: {filepath}, 大小: {len(data)}字节, record_size={record_size}, 总记录数: {total}, 市场={'扩展' if is_ext_market else '标准A股'}")

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
                date_int, o, h, l, c, amount, vol, _ = struct.unpack('<IfffffII', row)
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
        print(f"[警告] 检测到 {len(gap_indices)} 处数据缺口（请补全数据）:")
        for idx, (gi, gap_td) in enumerate(gap_indices):
            prev_dt = records[gi-1]["dt"].strftime("%Y-%m-%d")
            curr_dt = records[gi]["dt"].strftime("%Y-%m-%d")
            print(f"[警告]   缺口{idx+1}: {prev_dt} -> {curr_dt} (间隔{gap_td}个交易日)")
    else:
        old_start = records[0]["dt"].strftime("%Y-%m-%d")
        old_end = records[-1]["dt"].strftime("%Y-%m-%d")
        print(f"[信息] 检测到 0 处数据缺口")
        print(f"[信息]   数据范围: {old_start} ~ {old_end} ({len(records)}条)")
    print(f"[调试] 共解析有效记录: {len(records)}条")


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
    reserved = arr["reserved"][valid]

    # 检测是否为指数文件（指数 .lc5 的保留字段存的是涨跌家数，非零；个股 .lc5 保留字段为 0）
    # 经通达信官方确认：指数的分钟线数据只有成交额，没有成交量。
    # 指数 .lc5 文件中成交量字段实际存的是"成交额/100"（非真实成交量），需要忽略，成交量设为0。
    is_index = len(reserved) > 0 and np.any(reserved != 0)
    if is_index:
        print(f"[信息] 检测到指数文件，成交量字段不可靠（通达信确认指数分钟线仅有成交额，无成交量），将设为0")

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
            "vol": int(v) // 100,  # 通达信 .lc5 成交量单位是"股"，除以100转为"手"（柱状图已改用成交额绘制，此字段仅作参考）
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
#   1. eltdx（优先，基于 7709 协议，字段与 mootdx 完全等价）
#   2. mootdx Quotes 网络接口（备用）
#   3. pytdx 网络接口（自动测速，最后备用）
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


# ============================================================
# xdxr 网络连接管理（连接复用 + 线程锁，防止多线程并发冲突）
# ============================================================
import threading as _threading
_xdxr_lock = _threading.Lock()

# ============================================================
# eltdx 除权除息（优先，基于 7709 协议 0x000f 命令）
# ============================================================
# eltdx TdxClient 单例复用（与 mootdx 单例对称）：每次新建 TdxClient 都会
# 重新解析 hosts 并可能触发服务器探测（probe_hosts），探测后写排名缓存
# （persist=True）在 Windows 下常因文件被占用抛 OSError → RuntimeWarning
# （"unable to persist eltdx server ranking"）。扫描每票都走 xdxr，若每次
# 新建会反复触发该警告。改为模块级单例 + probe_hosts=False：
#   - 单例：连接复用，避免重复探测/重复解析 hosts；
#   - probe_hosts=False：关闭启动探测（探测仅用于选最快服务器，非必需；
#     连接失败时 eltdx 内部仍会按 hosts 顺序重连），从根上消除该警告。
_eltdx_client = None
_eltdx_client_ready = False

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
    eltdx 的 XdxrRecord 字段：code, date, category, fenhong, peigujia, songzhuangu, peigu
    与 mootdx 返回的字段完全等价（同为 7709 协议 0x000f 命令）。
    """
    try:
        client = _ensure_eltdx_client()
        if client is None:
            return None
        market_code = f"{market.lower()}{code}"
        with client:
            records = client.get_xdxr(market_code)
        if not records:
            return None
        # 将 XdxrRecord 列表转为 DataFrame，再走统一的 _normalize_xdxr_df 标准化
        rows = []
        for r in records:
            # XdxrRecord.date 是 datetime.date 类型，需转为 datetime.datetime
            d = r.date
            if d is not None and not isinstance(d, datetime):
                d = datetime(d.year, d.month, d.day)
            rows.append({
                'code': r.code,
                'date': d,
                'category': r.category,
                'fenhong': float(r.fenhong),
                'peigujia': float(r.peigujia),
                'songzhuangu': float(r.songzhuangu),
                'peigu': float(r.peigu),
            })
        df = pd.DataFrame(rows)
        if len(df) == 0:
            return None
        return _normalize_xdxr_df(df)
    except Exception:
        return None

# mootdx Quotes 单例连接（建一次，所有股票复用）
_mootdx_client = None
_mootdx_client_ready = False

def _ensure_mootdx_client():
    """确保 mootdx Quotes 客户端已连接，返回 client 或 None。线程安全。"""
    global _mootdx_client, _mootdx_client_ready
    if _mootdx_client_ready and _mootdx_client is not None:
        return _mootdx_client
    try:
        from mootdx.quotes import Quotes
        _mootdx_client = Quotes.factory(market='std', bestip=False, timeout=10)
        _mootdx_client_ready = True
        return _mootdx_client
    except Exception:
        _mootdx_client_ready = False
        _mootdx_client = None
        return None


def _get_xdxr_mootdx(market, code):
    """通过 mootdx Quotes 单例连接获取除权除息数据。在锁内调用。"""
    client = _ensure_mootdx_client()
    if client is None:
        return None
    try:
        df = client.xdxr(symbol=code)
        if df is not None and len(df) > 0:
            return _normalize_xdxr_df(df)
    except Exception:
        _mootdx_client_ready = False
        _mootdx_client = None
    return None


# pytdx TdxHq_API 单例连接（建一次，所有股票复用）
_pytdx_api = None
_pytdx_api_ready = False

def _ensure_pytdx_api():
    """确保 pytdx TdxHq_API 已连接，返回 api 或 None。线程安全。"""
    global _pytdx_api, _pytdx_api_ready
    if _pytdx_api_ready and _pytdx_api is not None:
        return _pytdx_api
    try:
        from pytdx.hq import TdxHq_API
        host, port = _find_pytdx_server()
        if not host:
            return None
        _pytdx_api = TdxHq_API()
        if not _pytdx_api.connect(host, port):
            _pytdx_api = None
            _pytdx_api_ready = False
            return None
        _pytdx_api_ready = True
        return _pytdx_api
    except Exception:
        _pytdx_api = None
        _pytdx_api_ready = False
        return None


def _find_pytdx_server():
    """找到可用的 pytdx 服务器（内部用 daemon 线程做超时探测）"""
    result = [None]
    def _select():
        try:
            from pytdx.util.best_ip import select_best_ip
            result[0] = select_best_ip()
        except Exception:
            pass
    t = _threading.Thread(target=_select, daemon=True)
    t.start()
    t.join(timeout=10)
    if result[0] and isinstance(result[0], dict) and 'ip' in result[0]:
        return result[0]['ip'], result[0].get('port', 7709)
    for host, port in PYTDX_SERVERS:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.5)
            _r = sock.connect_ex((host, port))
            sock.close()
            if _r == 0:
                return host, port
        except Exception:
            continue
    return None, None


def _get_xdxr_pytdx(market, code):
    """通过 pytdx 单例连接获取除权除息数据。在锁内调用。"""
    api = _ensure_pytdx_api()
    if api is None:
        return None
    mkt = 1 if market.lower() == 'sh' else 0
    try:
        data = api.get_xdxr_info(mkt, code)
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
        _pytdx_api_ready = False
        _pytdx_api = None
        return None


# xdxr 独立缓存：key=(market, code)，同一股票跨周期不重复拉取
_xdxr_cache = {}

def get_xdxr_data(market, code):
    """
    获取指定股票的除权除息数据。
    线程安全：多线程并发时，网络请求串行化，避免 pytdx socket 竞争。

    优先级：
      1. 缓存（内存命中，跳过网络请求）
      2. eltdx（优先，基于 7709 协议，字段与 mootdx 完全等价）
      3. mootdx Quotes（备用）
      4. pytdx（自动测速，最后备用）

    返回 pandas DataFrame，统一列名：
      date, category, fenhong, peigu, peigujia, songgu, zhuanzeng
    其中 fenhong/songgu/zhuanzeng/peigu 均为"每10股"单位。
    返回 None 表示无除权除息数据或所有方法均失败。
    """
    if market.lower() not in ('sh', 'sz'):
        return None

    cache_key = (market, code)
    with _xdxr_lock:
        if cache_key in _xdxr_cache:
            return _xdxr_cache[cache_key]

        df = _get_xdxr_eltdx(market, code)
        if df is not None and len(df) > 0:
            _xdxr_cache[cache_key] = df
            return df

        df = _get_xdxr_mootdx(market, code)
        if df is not None and len(df) > 0:
            _xdxr_cache[cache_key] = df
            return df

        df = _get_xdxr_pytdx(market, code)
        if df is not None and len(df) > 0:
            _xdxr_cache[cache_key] = df
            return df

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
def _forward_adjust(records, market, code, end_date=None):
    """
    对原始K线数据进行前复权处理。

    参数:
      records:  list[dict], 原始K线数据
               每条记录: dt, open, high, low, close, vol, amount
      market:   str, "sh" 或 "sz"
      code:     str, 6位股票代码
      end_date: datetime 或 None, 复盘截止日期。
                当 end_date 不为 None 时（复盘模式），只使用 date <= end_date
                的除权除息事件做前复权，以 end_date 为最新锚点递推。
                当 end_date 为 None 时（冷启动模式），使用全部历史事件，
                以最新日期为锚点递推。

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

    # 复盘模式：只保留 end_date 及之前的除权除息事件
    # 以 end_date 为"最新锚点"做前复权，A 之后的事件不纳入计算
    if end_date is not None:
        events = [(evt_date, a, b) for evt_date, a, b in events if evt_date <= end_date]
        if not events:
            return records, False

    import time as _time
    t_start = _time.time()
    anchor_desc = f"截止到 {end_date.strftime('%Y-%m-%d')}" if end_date is not None else "全部历史"
    print(f"[复权] {code} 共{len(events)}个除权除息事件({anchor_desc})，开始前复权...")

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

    print(f"[复权] {code} 前复权完成，耗时 {_time.time()-t_start:.3f}s")
    return records, True


def find_day_file(market, code):
    """查找日线数据文件路径"""
    if market == 'hk':
        return os.path.join(_tdx_config["vipdoc_dir"], "ds", "lday", f"31#{code}.day")
    if market == 'ds':
        return os.path.join(_tdx_config["vipdoc_dir"], "ds", "lday", f"62#{code}.day")
    return os.path.join(_tdx_config["vipdoc_dir"], market, "lday", f"{market}{code}.day")


def read_main_level_records(market, code, freq, return_raw=False, end_date=None):
    """
    从通达信文件读取主级别K线数据，含前复权和周期合成。
    各周期数据加载 + 前复权（统一在原始数据层面处理，避免二次复权）。

    返回值统一为 (records, did_adjust) 二元组，did_adjust 表示是否实际执行了前复权。
    当 return_raw=True 时，返回 (records, raw_records, did_adjust) 三元组。

    当 return_raw=True 时：
      - freq='30m': 返回 (records_30m, raw_5m, did_adjust) 三元组，raw_5m 是前复权后的5m数据
      - freq='w':    返回 (records_w, raw_d, did_adjust) 三元组，raw_d 是前复权后的日线数据
    供双窗口子级别复用，避免重复读取和二次复权。

    end_date: datetime 或 None, 复盘截止日期。传入后前复权只以 end_date
              为最新锚点递推，A 之后的除权事件不参与复权。
    """
    if freq in ('30m', '5m'):
        if market == 'hk':
            data_file = os.path.join(_tdx_config["vipdoc_dir"], "ds", "fzline", f"31#{code}.lc5")
        elif market == 'ds':
            data_file = os.path.join(_tdx_config["vipdoc_dir"], "ds", "fzline", f"62#{code}.lc5")
        else:
            data_file = os.path.join(_tdx_config["vipdoc_dir"], market, "fzline", f"{market}{code}.lc5")
        if not os.path.exists(data_file):
            return ([], [], False) if return_raw else ([], False)
    else:
        data_file = find_day_file(market, code)
        if not os.path.exists(data_file):
            return ([], [], False) if return_raw else ([], False)

    if freq == '30m':
        raw_5m = read_tdx_min_file(data_file, market=market, aggregate_30m=False)
        did_adjust = False
        if _tdx_config["forward_adjust_enabled"]:
            raw_5m, did_adjust = _forward_adjust(raw_5m, market=market, code=code, end_date=end_date)
        records_30m = _resample_5m_to_30m(list(raw_5m), market=market)
        if return_raw:
            return records_30m, raw_5m, did_adjust
        return records_30m, did_adjust
    elif freq == '5m':
        records = read_tdx_min_file(data_file, market=market, aggregate_30m=False)
        did_adjust = False
        if _tdx_config["forward_adjust_enabled"]:
            records, did_adjust = _forward_adjust(records, market=market, code=code, end_date=end_date)
        return records, did_adjust
    elif freq == 'w':
        records = read_tdx_day_file(data_file, market=market)
        did_adjust = False
        if _tdx_config["forward_adjust_enabled"]:
            records, did_adjust = _forward_adjust(records, market=market, code=code, end_date=end_date)
        records_w = _resample_day_to_week(records)
        if return_raw:
            return records_w, records, did_adjust
        return records_w, did_adjust
    else:
        records = read_tdx_day_file(data_file, market=market)
        did_adjust = False
        if _tdx_config["forward_adjust_enabled"]:
            records, did_adjust = _forward_adjust(records, market=market, code=code, end_date=end_date)
        return records, did_adjust


def read_sub_level_records(market, code, freq, sub_freq, records, end_date=None):
    """
    双窗口模式：加载子级别K线数据。
    返回与主级别相同时间范围的子级别records列表。
    数据来源与主级别一致：从通达信原始文件读取，做前复权处理。

    end_date: datetime 或 None, 复盘截止日期。传入后前复权只以 end_date
              为最新锚点递推。
    """
    if sub_freq in ('30m', '5m'):
        if market == 'hk':
            min_file = os.path.join(_tdx_config["vipdoc_dir"], "ds", "fzline", f"31#{code}.lc5")
        elif market == 'ds':
            min_file = os.path.join(_tdx_config["vipdoc_dir"], "ds", "fzline", f"62#{code}.lc5")
        else:
            min_file = os.path.join(_tdx_config["vipdoc_dir"], market, "fzline", f"{market}{code}.lc5")
        if not os.path.exists(min_file):
            print(f"[警告] 子级别数据文件不存在: {min_file}")
            return None
        sub_records = read_tdx_min_file(min_file, market=market, aggregate_30m=False)
        if _tdx_config["forward_adjust_enabled"]:
            sub_records, _ = _forward_adjust(sub_records, market=market, code=code, end_date=end_date)
        if sub_freq == '30m':
            sub_records = _resample_5m_to_30m(sub_records, market=market)
    elif sub_freq == 'd':
        day_file = find_day_file(market, code)
        if not os.path.exists(day_file):
            print(f"[警告] 子级别数据文件不存在: {day_file}")
            return None
        sub_records = read_tdx_day_file(day_file, market=market)
        if _tdx_config["forward_adjust_enabled"]:
            sub_records, _ = _forward_adjust(sub_records, market=market, code=code, end_date=end_date)
    else:
        return None

    if len(sub_records) < 5:
        print(f"[警告] 子级别数据不足({len(sub_records)}条)")
        return None

    # 过滤到与主级别相同的时间范围（略大一点，确保边界包含）
    if records:
        main_start = records[0]["dt"]
        main_end = records[-1]["dt"]
        sub_records = [r for r in sub_records if main_start - timedelta(days=1) <= r["dt"] <= main_end + timedelta(days=1)]

    print(f"[信息] 子级别({sub_freq})数据加载: {len(sub_records)}条")
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
# 板块文件（.blk）读写
# ============================================================

def _get_blocknew_dir():
    """从 vipdoc_dir 推导 T0002/blocknew 目录"""
    vipdoc = _tdx_config.get("vipdoc_dir", "")
    if not vipdoc:
        return ""
    return os.path.join(os.path.dirname(vipdoc), "T0002", "blocknew")


def _get_hq_cache_dir():
    """从 vipdoc_dir 推导 T0002/hq_cache 目录"""
    vipdoc = _tdx_config.get("vipdoc_dir", "")
    if not vipdoc:
        return ""
    return os.path.join(os.path.dirname(vipdoc), "T0002", "hq_cache")


def get_blk_path(blk_name):
    """
    获取板块文件路径。
    blk_name: "zxg" → T0002/blocknew/zxg.blk
              "中证1000" → T0002/blocknew/中证1000.blk
    """
    return os.path.join(_get_blocknew_dir(), blk_name + ".blk") if _get_blocknew_dir() else ""


def read_blk_file(blk_path):
    """
    读取通达信 .blk 板块文件，返回股票代码列表。
    文件格式：GBK编码，每行一个代码。
    A股格式：7位纯数字（1位交易所前缀 + 6位股票代码），如 "0600000"、"1600001"
    港股个股：31#{5位代码}，如 31#00700
    港股指数：27#{HZ代码}，如 27#HZ5489
    美股个股：74#{代码}，如 74#XBI
    美股指数：12#A_{代码}，如 12#A_NBI
    """
    if not blk_path or not os.path.exists(blk_path):
        return []
    stocks = []
    try:
        with open(blk_path, "r", encoding="gbk") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if len(line) == 7 and line.isdigit():
                    # A 股：7位纯数字（前缀0/1/2 + 6位代码）
                    prefix = line[0]
                    code = line[1:7]
                    stocks.append({"prefix": prefix, "code": code})
                elif line.startswith("31#") and len(line) == 8:
                    # 港股个股：31# + 5位数字
                    code = line[3:].strip()
                    if code.isdigit():
                        code = code.zfill(5)
                        stocks.append({"prefix": "hk", "code": code})
                elif line.startswith("74#") and len(line) > 3:
                    # 美股个股：74# + 代码（如 74#XBI）
                    code = line[3:].strip()
                    if code:
                        stocks.append({"prefix": "us", "code": code})
                elif line.startswith("27#") and len(line) > 3:
                    # 港股指数：27# + HZ代码（如 27#HZ5489）
                    code = line[3:].strip()
                    if code:
                        stocks.append({"prefix": "hk", "code": code})
                elif line.startswith("12#") and len(line) > 3:
                    # 美股指数：12# + 代码（如 12#A_NBI）
                    code = line[3:].strip()
                    if code:
                        stocks.append({"prefix": "us", "code": code})
    except Exception as e:
        print(f"[错误] 读取板块文件失败 {blk_path}: {e}")
    return stocks


# ============================================================
# [阶段 4 墓碑] read_zxg_stocks / save_to_zxg_blk / _ZXG_HK_INDEX_MAP /
# _ZXG_US_INDEX_MAP 已于阶段 4（数据层收敛）迁出至 App/AppData.py。
# 自选股属业务数据层职责（存哪里、怎么存），含路径推导与 blk 解析
# （AppData.zxg_blk_path / _read_zxg_blk_file 自含，与本模块互不依赖）；
# 本模块的 get_blk_path / read_blk_file 仅服务自身指数成分读取。
# 迁移入口：from App.AppData import app_data
#           app_data.read_zxg_stocks() / app_data.save_to_zxg_blk(codes)
# ============================================================


def read_zz1000_stocks():
    """
    读取通达信中证1000板块文件，返回股票代码列表。
    用户需先在通达信中创建/下载"中证1000"板块。
    """
    path = get_blk_path("ZZ1000")
    if not os.path.exists(path):
        print(f"[警告] 中证1000板块文件不存在: {path}")
        return []
    return read_blk_file(path)


def read_sz50_stocks():
    """
    读取通达信上证50板块文件，返回股票代码列表。
    用户需先在通达信中创建/下载"上证50"板块。
    """
    path = get_blk_path("SZ50")
    if not os.path.exists(path):
        print(f"[警告] 上证50板块文件不存在: {path}")
        return []
    return read_blk_file(path)


def read_hs300_stocks():
    """
    读取通达信沪深300板块文件，返回股票代码列表。
    用户需先在通达信中创建/下载"沪深300"板块。
    """
    path = get_blk_path("HS300")
    if not os.path.exists(path):
        print(f"[警告] 沪深300板块文件不存在: {path}")
        return []
    return read_blk_file(path)


def read_zz500_stocks():
    """
    读取通达信中证500板块文件，返回股票代码列表。
    用户需先在通达信中创建/下载"中证500"板块。
    """
    path = get_blk_path("ZZ500")
    if not os.path.exists(path):
        print(f"[警告] 中证500板块文件不存在: {path}")
        return []
    return read_blk_file(path)


# ============================================================
# 板块成分股缓存（网络下载，全量缓存，支持所有88指数）
# ============================================================
_BLOCK_GN_CACHE = None       # dict: sector_name → [{"code","prefix","name"}, ...]
_BLOCK_GN_CACHE_LOADED = False
_INFOHARBOR_BLOCK_CACHE = None       # dict: sector_code → {"name": str, "stocks": [...]}
_INFOHARBOR_BLOCK_CACHE_LOADED = False


def _download_block_file(api, host, port, block_file):
    """通过已连接的 PyTDX API 下载单个板块文件并返回原始字节"""
    meta = api.get_block_info_meta(block_file)
    if not meta or 'size' not in meta or meta['size'] == 0:
        return None

    total_size = meta['size']

    ONE_CHUNK = 0x7530
    chunks = (total_size + ONE_CHUNK - 1) // ONE_CHUNK
    raw_data = bytearray()

    for seg in range(chunks):
        start = seg * ONE_CHUNK
        chunk_size = min(ONE_CHUNK, total_size - start)
        piece = api.get_block_info(block_file, start, chunk_size)
        if piece is None or len(piece) == 0:
            return None
        raw_data.extend(piece)

    if len(raw_data) >= total_size:
        return raw_data
    elif len(raw_data) > 386:
        return raw_data
    return None




def _safe_replace_file(path, raw_data):
    """先写临时文件，再用 os.replace 原子替换正式文件；失败时保留旧文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(raw_data)
        os.replace(tmp_path, path)
        return True
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _validate_downloaded_block_file(file_name, raw_data):
    """下载后做轻量格式校验，校验通过才覆盖旧文件。"""
    if not raw_data:
        return False

    if file_name == "infoharbor_block.dat":
        try:
            head = raw_data[:4096].decode("gbk", errors="ignore")
        except Exception:
            return False
        return "#GN_" in head or "#FG_" in head or "#ZS_" in head

    if file_name in ("block_zs.dat", "block_gn.dat", "block_fg.dat", "block.dat"):
        if len(raw_data) < 386:
            return False
        try:
            block_count = struct.unpack_from("<H", raw_data, 384)[0]
        except Exception:
            return False
        return 0 < block_count < 10000

    return False


def _safe_refresh_one_block_file(api, host, port, file_name, block_cache_dir, progress_callback=None):
    """下载单个板块文件，成功校验后再覆盖旧文件；失败时旧文件保持不变。"""
    if progress_callback:
        progress_callback(f"下载成分股: {file_name}...")
    try:
        meta = api.get_block_info_meta(file_name)
        total_size = int(meta.get("size", 0) or 0) if meta else 0
        if total_size <= 0:
            print(f"[板块刷新] {file_name} 服务端 size 无效，保留旧文件")
            return False

        raw = _download_block_file(api, host, port, file_name)
        if not raw or len(raw) != total_size:
            print(f"[板块刷新] {file_name} 下载不完整，保留旧文件: {len(raw) if raw else 0}/{total_size}")
            return False

        if not _validate_downloaded_block_file(file_name, raw):
            print(f"[板块刷新] {file_name} 格式校验失败，保留旧文件")
            return False

        local_path = os.path.join(block_cache_dir, file_name)
        _safe_replace_file(local_path, raw)
        print(f"[板块刷新] ✅ {file_name} 刷新成功: {total_size} 字节")
        return True
    except Exception as e:
        print(f"[板块刷新] {file_name} 刷新失败，保留旧文件: {e}")
        return False


def _download_block_gn_from_network(progress_callback=None):
    """
    通过 PyTDX 网络接口下载全量板块成分股数据。
    
    下载 block_hy(二级行业) + block_zs(指数) + block_gn(概念) + block_fg(风格) 四个文件并合并。
    注意：block.dat 只有精选指数（约100条），不含行业板块，不能替代上述文件。
    
    磁盘缓存逻辑：
      1. block_cache_dir 即 _tdx_config['vipdoc_dir']（和 stock_names.json 同目录）
      2. 每个 block_*.dat 先尝试读本地文件，存在直接解析
      3. 本地不存在才从服务器下载，下载后写入本地文件
      4. 结果缓存在全局 _BLOCK_GN_CACHE，下次程序启动再走磁盘缓存

    返回 dict: {sector_name: [{"code": "000001", "prefix": "0", "name": "000001"}, ...], ...}
    """
    global _BLOCK_GN_CACHE, _BLOCK_GN_CACHE_LOADED
    if _BLOCK_GN_CACHE_LOADED:
        return _BLOCK_GN_CACHE or {}

    result = {}

    # 本地缓存目录 = T0002/hq_cache/（和 tdxzs.cfg / tdxhy.cfg 同目录）
    vipdoc_dir = _tdx_config.get("vipdoc_dir", "")
    if vipdoc_dir:
        block_cache_dir = os.path.join(os.path.dirname(vipdoc_dir), "T0002", "hq_cache")
    else:
        block_cache_dir = None

    try:
        from pytdx.hq import TdxHq_API
    except ImportError:
        _BLOCK_GN_CACHE_LOADED = True
        return {}

    # 通达信服务器提供的板块文件（通过网络 get_block_info 协议下载）
    # block_zs.dat: 精选指数板块（沪深300、中证500等）
    # block_gn.dat: 概念板块（8805xx，锂电池、人工智能等）
    # block_fg.dat: 风格板块（8808xx，大盘股、小盘股等）
    # 注意：block_hy.dat（二级行业，含880491"半导体"）不在服务器上，
    #       它只存在于本地 T0002/hq_cache/ 目录，格式也不同（480字节/条 vs 2800字节/条）
    candidate_files = [
        "block_zs.dat",
        "block_gn.dat",
        "block_fg.dat",
        "block.dat",
    ]

    result = {}
    need_download = []

    # Step 1: 先读本地文件
    if block_cache_dir:
        for bf in candidate_files:
            local_path = os.path.join(block_cache_dir, bf)
            if not os.path.exists(local_path):
                need_download.append(bf)
                continue
            try:
                with open(local_path, "rb") as f:
                    raw = f.read()
                parsed = _parse_raw_block_gn(raw, bf)
                if parsed:
                    print(f"[板块成分股] ✅ 从本地缓存读取 {bf}: {len(parsed)} 个板块")
                    result.update(parsed)
            except Exception as e:
                print(f"[板块成分股] ⚠️ 本地缓存 {bf} 读取失败，尝试从网络下载: {e}")
                need_download.append(bf)
    else:
        # 没有配置通达信目录，全部从网络下载
        need_download = candidate_files[:]

    servers = PYTDX_SERVERS[:]

    if need_download:
        print(f"[板块成分股] 需从网络下载: {need_download}")
    else:
        print(f"[板块成分股] 所有板块文件已从本地缓存加载，无需下载")

    for host, port in servers:
        if not need_download:
            break
        try:
            api = TdxHq_API(multithread=True)
            if not api.connect(host, port):
                continue

            # Step 1: 快速探测哪些文件存在（只取 meta，不下载）
            existing_files = []
            for bf in need_download:
                try:
                    meta = api.get_block_info_meta(bf)
                    if meta and meta.get('size', 0) > 0:
                        existing_files.append(bf)
                except Exception:
                    pass
            if not existing_files:
                api.disconnect()
                continue

            # Step 2: 下载并解析，写入本地缓存
            for bf in existing_files:
                if progress_callback:
                    progress_callback(f"下载成分股: {bf}...")
                print(f"[板块成分股] 开始下载 {bf}...")
                raw = _download_block_file(api, host, port, bf)
                if raw and len(raw) > 386:
                    parsed = _parse_raw_block_gn(raw, bf)
                    if parsed:
                        result.update(parsed)
                        print(f"[板块成分股] ✅ {bf} 下载完成: {len(parsed)} 个板块")
                        # 写入本地缓存文件：先写临时文件，校验成功后原子替换，避免下载失败破坏旧文件
                        if block_cache_dir and _validate_downloaded_block_file(bf, raw):
                            try:
                                local_path = os.path.join(block_cache_dir, bf)
                                _safe_replace_file(local_path, raw)
                            except Exception as e:
                                print(f"[板块成分股] ⚠️ 写入本地缓存 {bf} 失败: {e}")
                        need_download.remove(bf)
                else:
                    print(f"[板块成分股] ⚠️ {bf} 下载失败或数据无效")

            api.disconnect()
            if not need_download:
                break

        except TypeError as e:
            import traceback
            traceback.print_exc()
            continue
        except Exception as e:
            import traceback
            traceback.print_exc()
            continue

    if not result:
        print("[板块成分股] 所有服务器均下载失败，板块数据不可用")
        _BLOCK_GN_CACHE_LOADED = True
        return {}

    _BLOCK_GN_CACHE = result
    _BLOCK_GN_CACHE_LOADED = True
    print(f"[板块成分股] 解析完成，共 {len(result)} 个板块有成分股数据")
    return result


def refresh_block_files(progress_callback=None):
    """
    公开函数：强制刷新板块文件。

    安全刷新策略：
      1. 不先删除旧文件；
      2. 先从 PyTDX 下载到内存；
      3. 校验成功后写入 .tmp，再用 os.replace 原子替换；
      4. 任一文件刷新失败时保留旧文件。
    """
    global _BLOCK_GN_CACHE, _BLOCK_GN_CACHE_LOADED
    global _INFOHARBOR_BLOCK_CACHE, _INFOHARBOR_BLOCK_CACHE_LOADED

    vipdoc_dir = _tdx_config.get("vipdoc_dir", "")
    if vipdoc_dir:
        block_cache_dir = os.path.join(os.path.dirname(vipdoc_dir), "T0002", "hq_cache")
    else:
        block_cache_dir = None

    if not block_cache_dir:
        print("[板块刷新] 无法确定 hq_cache 目录，跳过板块文件下载")
        return

    os.makedirs(block_cache_dir, exist_ok=True)

    block_files = [
        "infoharbor_block.dat",
        "block_zs.dat",
        "block_gn.dat",
        "block_fg.dat",
        "block.dat",
    ]

    try:
        from pytdx.hq import TdxHq_API
    except ImportError:
        print("[板块刷新] pytdx 未安装，无法刷新板块文件；保留旧文件")
        return

    refreshed = 0
    for host, port in PYTDX_SERVERS[:]:
        try:
            api = TdxHq_API(multithread=True)
            if not api.connect(host, port):
                continue
            print(f"[板块刷新] 已连接服务器 {host}:{port}")
            for bf in block_files:
                if _safe_refresh_one_block_file(api, host, port, bf, block_cache_dir, progress_callback=progress_callback):
                    refreshed += 1
            api.disconnect()
            break
        except Exception as e:
            print(f"[板块刷新] 服务器 {host}:{port} 刷新失败: {e}")
            try:
                api.disconnect()
            except Exception:
                pass
            continue

    if refreshed == 0:
        print("[板块刷新] 所有文件均未刷新成功，继续使用旧文件")
    else:
        print(f"[板块刷新] 刷新完成: {refreshed}/{len(block_files)} 个文件成功")

    _BLOCK_GN_CACHE = None
    _BLOCK_GN_CACHE_LOADED = False
    _INFOHARBOR_BLOCK_CACHE = None
    _INFOHARBOR_BLOCK_CACHE_LOADED = False


def _parse_raw_block_gn(data, block_file="block_gn.dat"):
    """
    解析 block_*.dat 二进制数据（完全参考 pytdx BlockReader 源码）。
    block_gn.dat / block_zs.dat / block_fg.dat 格式相同。
    格式：384字节文件头 + 2字节板块数 + N条板块记录

    每条板块记录：
      9字节名称(GBK) + 2字节成分股数(uint16) + 2字节类别(uint16)
      + 成分股列表(每只7字节，UTF-8编码，格式如 "0000001" = 市场前缀+6位代码)
    每条记录固定占 2800 字节（从成分股列表起始位置算起，包含股票代码数据 + 尾部填充）
    """
    import struct
    result = {}

    if len(data) < 386:
        return result

    # 跳过384字节文件头，读取板块数量
    pos = 384
    block_count = struct.unpack_from("<H", data, pos)[0]
    pos += 2

    for i in range(block_count):
        if pos + 13 > len(data):
            break

        # 板块名称（9字节 GBK）
        raw_name = data[pos:pos + 9]
        pos += 9
        block_name = raw_name.decode("gbk", errors="ignore").rstrip("\x00")

        # 成分股数量 + 板块类别（各2字节 uint16 LE）
        stock_count, block_type = struct.unpack_from("<HH", data, pos)
        pos += 4

        # 记录成分股列表起始位置（用于后续跳转到下一条记录）
        block_stock_begin = pos

        # 调试打印：只保留 block_count 总数打印，不打印单个板块（已移除详细输出）

        if block_name and stock_count > 0 and stock_count < 10000:
            stocks = []
            for j in range(stock_count):
                if pos + 7 > len(data):
                    break
                raw_stock = data[pos:pos + 7]
                pos += 7
                # 关键：使用 UTF-8 解码（与 pytdx BlockReader 一致）
                one_code = raw_stock.decode("utf-8", errors="ignore").rstrip("\x00")
                # block_*.dat 中存储的是 6 位纯数字股票代码（如 "600028"）
                if len(one_code) == 6 and one_code.isdigit():
                    # 根据代码规则推断市场前缀
                    first = one_code[0]
                    if first in "689":
                        prefix = "1"   # 沪市（含主板、科创板）
                    elif first in "03":
                        prefix = "0"   # 深市（含主板、创业板）
                    elif first in "24":
                        prefix = "2"   # 北交所/新三板
                    else:
                        prefix = "1"   # 默认沪市
                    stocks.append({
                        "code": one_code,
                        "prefix": prefix,
                        "name": one_code,
                    })

            if stocks:
                result[block_name] = stocks

        # 跳到下一个板块：从 block_stock_begin 起跳过 2800 字节
        # （参考 pytdx BlockReader: pos = block_stock_begin + 2800）
        pos = block_stock_begin + 2800

    return result




def _parse_infoharbor_block(raw_data):
    """
    解析新版通达信 infoharbor_block.dat。

    样例格式：
      #GN_商业航天,522,880548,20240530,20260710,,
      0#000026,0#000032,1#600xxx,...

    返回:
      {
        "880548": {"name": "商业航天", "type": "GN", "stocks": [...]},
        ...
      }
    """
    result = {}
    if not raw_data:
        return result

    try:
        text = raw_data.decode("gbk", errors="ignore")
    except Exception:
        return result

    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split(",")
            title = parts[0][1:].strip()
            if "_" in title:
                block_type, block_name = title.split("_", 1)
            else:
                block_type, block_name = "", title
            declared_count = 0
            if len(parts) > 1 and parts[1].strip().isdigit():
                declared_count = int(parts[1].strip())
            block_code = parts[2].strip() if len(parts) > 2 else ""
            current = {
                "type": block_type,
                "name": block_name,
                "declared_count": declared_count,
                "stocks": [],
            }
            if block_code:
                result[block_code] = current
            continue

        if current is None:
            continue

        for token in line.split(","):
            token = token.strip()
            if not token:
                continue
            m = re.fullmatch(r"(\d)#(\d{5,6})", token)
            if not m:
                continue
            prefix, code = m.group(1), m.group(2)
            current["stocks"].append({
                "code": code,
                "prefix": prefix,
                "name": code,
            })

    # 仅保留数量自洽且有成分股的板块；declared_count=0 的空板块不参与成分股查询
    filtered = {}
    for code, item in result.items():
        stocks = item.get("stocks", [])
        declared = item.get("declared_count", 0)
        if stocks and (declared == 0 or declared == len(stocks)):
            filtered[code] = item
    return filtered


def _read_infoharbor_blocks():
    """读取本地 infoharbor_block.dat，返回 sector_code → 成分股信息。"""
    global _INFOHARBOR_BLOCK_CACHE, _INFOHARBOR_BLOCK_CACHE_LOADED
    if _INFOHARBOR_BLOCK_CACHE_LOADED:
        return _INFOHARBOR_BLOCK_CACHE or {}

    result = {}
    hq_cache = _get_hq_cache_dir()
    if hq_cache:
        path = os.path.join(hq_cache, "infoharbor_block.dat")
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    raw = f.read()
                result = _parse_infoharbor_block(raw)
                if result:
                    print(f"[板块成分股] ✅ 从 infoharbor_block.dat 读取 {len(result)} 个带代码板块")
            except Exception as e:
                print(f"[板块成分股] ⚠️ 读取 infoharbor_block.dat 失败: {e}")

    _INFOHARBOR_BLOCK_CACHE = result
    _INFOHARBOR_BLOCK_CACHE_LOADED = True
    return result


def _read_infoharbor_sector_stocks(sector_code):
    """优先按 880xxx 代码从 infoharbor_block.dat 读取成分股。"""
    blocks = _read_infoharbor_blocks()
    item = blocks.get(sector_code)
    if not item:
        return []
    stocks = item.get("stocks", [])
    if stocks:
        print(f"[板块成分股] ✅ 从 infoharbor_block.dat 找到 '{item.get('name', sector_code)}' 共 {len(stocks)} 只成分股")
    return stocks


def get_index_stocks(sector_code):
    """
    根据通达信板块指数代码，获取其成分股列表。

    支持的类型：
    - 881xxx: 研究行业(新版) → 本地 tdxhy.cfg
    - 880xxx: 概念/风格板块 → 优先 infoharbor_block.dat，失败再用 tdxzs.cfg + block_*.dat
    - 000xxx/399xxx: 标准指数 → AKShare (中证指数公司)

    返回: [{"code": "000001", "prefix": "0", "name": "000001"}, ...]
    """
    print(f"[板块成分股] 查询 sector_code={sector_code}")

    # Step 1: 881xxx（研究行业新版）→ 本地 tdxhy.cfg
    if sector_code.startswith("881"):
        return _read_tdxhy_sector_stocks(sector_code)

    # Step 2: 标准指数（000xxx / 399xxx 等）→ AKShare 统一获取
    if not sector_code.startswith("88"):
        return _read_standard_index_stocks(sector_code)

    # Step 3: 880xxx（概念/风格板块）→ 优先读取 infoharbor_block.dat
    stocks = _read_infoharbor_sector_stocks(sector_code)
    if stocks:
        return stocks

    # Step 4: infoharbor 不可用时，回退到 tdxzs.cfg + block_*.dat
    hq_cache = _get_hq_cache_dir()
    sector_name = None
    if hq_cache:
        tdxzs_file = os.path.join(hq_cache, "tdxzs.cfg")
        if os.path.exists(tdxzs_file):
            try:
                with open(tdxzs_file, "r", encoding="gbk", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split("|")
                        if len(parts) >= 2:
                            name = parts[0].strip()
                            code = parts[1].strip()
                            if "." in code:
                                code = code.split(".")[0]
                            if code == sector_code:
                                sector_name = name
                                break
            except Exception as e:
                print(f"[板块成分股] 读取tdxzs.cfg失败: {e}")

    if not sector_name:
        print(f"[板块成分股] 未在tdxzs.cfg中找到板块代码 {sector_code}")
        return []

    # 8803xx-8804xx（旧版行业）无成分股数据
    if sector_code.startswith("8803") or sector_code.startswith("8804"):
        print(f"[板块成分股] 旧版行业代码 {sector_code}，无成分股数据。请使用 881 研究行业代码。")
        return []

    # 从 block_*.dat 缓存中查找
    cache = _download_block_gn_from_network()
    stocks = cache.get(sector_name, [])

    if stocks:
        if len(stocks) >= 400:
            print(f"[板块成分股] ⚠️ 从旧 block_*.dat 找到 '{sector_name}' 共 {len(stocks)} 只，可能受 400 只上限影响")
        else:
            print(f"[板块成分股] ✅ 从旧 block_*.dat 找到 '{sector_name}' 共 {len(stocks)} 只成分股")
    else:
        print(f"[板块成分股] ❌ 旧 block_*.dat 缓存中未找到板块 '{sector_name}'")

    return stocks


def _parse_stocks_from_df(df, source_label):
    """从 AKShare DataFrame 中提取成分股列表（公共逻辑）"""
    stocks = []
    code_col = None
    for col in ["成分券代码", "证券代码", "品种代码", "代码", "con_code", "symbol", "stock_code"]:
        if col in df.columns:
            code_col = col
            break
    if code_col is None:
        print(f"[板块成分股] {source_label} 返回未知列名: {list(df.columns)}")
        return []

    seen_codes = set()
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        if "." in code:
            code = code.split(".")[0]
        if len(code) == 6 and code.isdigit():
            if code in seen_codes:
                continue
            seen_codes.add(code)
            first = code[0]
            if first in "689":
                prefix = "1"
            elif first in "03":
                prefix = "0"
            elif first in "24":
                prefix = "2"
            else:
                prefix = "1"
            stocks.append({"code": code, "prefix": prefix, "name": code})

    return stocks


def _read_standard_index_stocks(sector_code):
    """
    根据指数代码获取成分股。

    路由逻辑：
    - 中证指数（000300/000905/000852/000688 等）：中证指数官网 OSS XLS（官方直连）
    - 深交所指数（399xxx）：深交所官网 ShowReport XLS（官方直连）
    - 上证指数（000001）：综合指数，无成分股概念，返回空
    """
    try:
        import akshare as ak
    except ImportError:
        print(f"[板块成分股] AKShare 未安装，无法获取标准指数成分股")
        return []

    # ── 上证指数（000001）：综合指数，无成分股 ──
    if sector_code == "000001":
        return []

    # ── 中证指数（000300/000905/000852/000688 等）→ csindex ──
    CSI_INDICES = {"000300", "000905", "000852", "000688"}
    if sector_code in CSI_INDICES:
        try:
            df = ak.index_stock_cons_csindex(symbol=sector_code)
            if df is None or df.empty:
                print(f"[板块成分股] 中证指数 返回空数据: {sector_code}")
                return []
            stocks = _parse_stocks_from_df(df, f"csindex({sector_code})")
            print(f"[板块成分股] ✅ 中证指数 获取 '{sector_code}' 共 {len(stocks)} 只成分股")
            return stocks
        except Exception as e:
            print(f"[板块成分股] 中证指数 获取 {sector_code} 失败: {e}")
            import traceback
            traceback.print_exc()
            return []

    # ── 深交所指数（399xxx）→ 深交所官网 XLS 直连 ──
    if sector_code.startswith("399"):
        try:
            import requests
            import pandas as _pd
            from io import BytesIO
            url = f"https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xls&CATALOGID=1747_zs&ZSDM={sector_code}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            # 深交所返回 .xls，第一行是合并单元格标题"指数样本股"，pandas 读入时自动忽略
            for engine in [None, "xlrd"]:
                try:
                    df = _pd.read_excel(BytesIO(r.content), dtype=str, engine=engine)
                    break
                except Exception:
                    if engine is None:
                        continue
                    raise
            if df is None or df.empty:
                print(f"[板块成分股] 深交所 返回空数据: {sector_code}")
                return []
            stocks = _parse_stocks_from_df(df, f"深交所({sector_code})")
            print(f"[板块成分股] ✅ 深交所 获取 '{sector_code}' 共 {len(stocks)} 只成分股")
            return stocks
        except Exception as e:
            print(f"[板块成分股] 深交所 获取 {sector_code} 失败: {e}")
            import traceback
            traceback.print_exc()
            return []

    # ── 其他指数（000xxx 非中证、932xxx 等）→ 尝试 csindex ──
    try:
        df = ak.index_stock_cons_csindex(symbol=sector_code)
        if df is None or df.empty:
            print(f"[板块成分股] 中证指数 返回空数据: {sector_code}")
            return []
        stocks = _parse_stocks_from_df(df, f"csindex({sector_code})")
        print(f"[板块成分股] ✅ 中证指数 获取 '{sector_code}' 共 {len(stocks)} 只成分股")
        return stocks
    except Exception as e:
        print(f"[板块成分股] 中证指数 获取 {sector_code} 失败: {e}")
        import traceback
        traceback.print_exc()
        return []


# ============================================================
# 研究行业成分股（新版，881xxx代码，从本地 tdxhy.cfg 读取）
# ============================================================
_TDXHY_CACHE = None       # dict: X_code → [{"code","prefix","name"}, ...]
_TDXHY_CACHE_LOADED = False


def _parse_tdxhy_cfg():
    """
    解析本地 tdxhy.cfg 文件，构建 X代码 → 成分股映射。
    
    tdxhy.cfg 格式：market|stock_code|old_T_code|||new_X_code
    例如：0|000001|T1001|||X500102
    
    返回：{X_code: [{"code","prefix","name"}, ...]}
    """
    global _TDXHY_CACHE, _TDXHY_CACHE_LOADED
    if _TDXHY_CACHE_LOADED:
        return _TDXHY_CACHE or {}

    _TDXHY_CACHE_LOADED = True
    result = {}

    hq_cache = _get_hq_cache_dir()
    if not hq_cache:
        print("[板块成分股] hq_cache 目录不存在，无法读取 tdxhy.cfg")
        _TDXHY_CACHE = result
        return result

    tdxhy_path = os.path.join(hq_cache, "tdxhy.cfg")
    if not os.path.exists(tdxhy_path):
        print(f"[板块成分股] tdxhy.cfg 不存在: {tdxhy_path}")
        _TDXHY_CACHE = result
        return result

    try:
        with open(tdxhy_path, "r", encoding="gbk", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"[板块成分股] 读取 tdxhy.cfg 失败: {e}")
        _TDXHY_CACHE = result
        return result

    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        market = parts[0]
        stock_code = parts[1]
        x_code = parts[5].strip()
        if not x_code:
            continue

        # 市场前缀映射
        prefix_map = {"0": "0", "1": "1", "2": "2"}
        prefix = prefix_map.get(market, market)

        if x_code not in result:
            result[x_code] = []
        result[x_code].append({"code": stock_code, "prefix": prefix, "name": stock_code})

    _TDXHY_CACHE = result
    print(f"[板块成分股] 解析 tdxhy.cfg 完成: {len(lines)} 行, {len(result)} 个X代码, "
          f"共 {sum(len(v) for v in result.values())} 条股票映射")
    return result


def _read_tdxhy_sector_stocks(sector_code):
    """
    根据 881xxx 研究行业代码，从 tdxhy.cfg 获取成分股。
    
    对于父级X代码（如 X4001 半导体），聚合并所有子级X代码的股票。
    对于子级X代码（如 X400101 半导体材料），只返回该子级股票。
    
    返回：[{"code":"000001","prefix":"0","name":"000001"}, ...]
    """
    if sector_code not in _TDXHY_881_TO_X:
        print(f"[板块成分股] 881代码 {sector_code} 不在映射表中")
        return []

    x_code, sector_name = _TDXHY_881_TO_X[sector_code]
    print(f"[板块成分股] 板块名称: '{sector_name}' (代码={sector_code}, X={x_code})")

    # 解析 tdxhy.cfg
    x_to_stocks = _parse_tdxhy_cfg()
    if not x_to_stocks:
        print("[板块成分股] tdxhy.cfg 缓存为空")
        return []

    # 找到所有属于该X代码的子级代码
    children = [c for c in x_to_stocks if c.startswith(x_code)]
    all_stocks = []
    for child in sorted(children):
        child_stocks = x_to_stocks.get(child, [])
        all_stocks.extend(child_stocks)

    if children:
        level = "父级" if len(children) > 1 else "子级"
        child_codes = sorted(children)[:5]
        more = f" ...共{len(children)}个" if len(children) > 5 else ""
        print(f"[板块成分股] {level}聚合: X={x_code} → 子级={child_codes}{more} → 共 {len(all_stocks)} 只成分股")

    # 去重
    seen = set()
    stocks = []
    for s in all_stocks:
        key = s["code"]
        if key not in seen:
            seen.add(key)
            stocks.append(s)

    if stocks:
        print(f"[板块成分股] ✅ tdxhy.cfg 找到 '{sector_name}' 共 {len(stocks)} 只成分股")
    else:
        print(f"[板块成分股] ❌ tdxhy.cfg 中未找到 '{sector_name}' 的成分股")

    return stocks


# ============================================================
# [阶段 4 墓碑] save_to_zxg_blk 与 _ZXG_HK_INDEX_MAP/_ZXG_US_INDEX_MAP
# 已迁出至 App/AppData.py（业务数据层，阶段 4 数据层收敛）。
# 调用方式：from App.AppData import app_data
#           app_data.save_to_zxg_blk(codes)
# ============================================================

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
    print("  - get_xdxr_data(): 获取除权除息数据（eltdx -> mootdx -> pytdx）")
    print("  - get_float_shares_from_xdxr(): 从xdxr提取流通股本")
    print("  - 通过 set_tdx_config(forward_adjust_enabled=True) 启用前复权")
    print("")
    print("板块功能：")
    print("  - get_index_stocks(): 获取指数/板块成分股（88x→tdxhy/block, 标准指数→AKShare）")
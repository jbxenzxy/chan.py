"""
缠论分析 - chan.py 版本
基于 https://github.com/Vespa314/chan.py 实现
功能：读取通达信本地K线数据，进行缠论分析，生成K线图网页
"""

import sys
import os
import json
import struct
import re
import threading
import multiprocessing
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
from urllib.parse import urlparse, parse_qs

# ============================================================
# 内存监控工具
# ============================================================
def get_memory_info():
    """获取当前进程内存占用（跨平台）"""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        return mem_info.rss / (1024 * 1024)  # 转换为 MB
    except ImportError:
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS 返回的是字节，Linux 返回的是 KB
            if sys.platform == "darwin":
                return rss / (1024 * 1024)
            else:
                return rss / 1024
        except Exception:
            return None


_memory_print_count = 0
_memory_baseline = None
_freq_order = {'w': 0, 'd': 1, '30m': 2, '5m': 3}


def print_memory(label="当前"):
    """打印内存占用信息（带递增计数器、相对基线增量、缓存统计）"""
    pass  # 调试阶段已结束，关闭内存监控输出

# ============================================================
# 配置区域 - 请根据你的实际环境修改
# ============================================================
VIPDOC_DIR = r"C:\new_tdx_test\vipdoc"  # 通达信vipdoc目录
TDX_HQ_CACHE = r"C:\new_tdx_test\T0002\hq_cache"  # 通达信hq_cache目录（shm.tnf/szm.tnf）
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))  # 输出目录（脚本所在目录）
SYMBOL_CODE = "SH000001"  # 默认股票代码（上证指数）
SYMBOL_DISPLAY = "上证指数"
CHAN_PATH = r"C:\my_chan_project"  # chan.py 仓库解压目录

# ============================================================
# 天勤期货/期指行情配置
# ============================================================
# 请修改为你的快期账号（注册地址: https://account.shinnytech.com）
TQ_ENABLED = True                          # 是否启用期货实时行情（设为 False 则只保留股票功能）
TQ_ACCOUNT = "你的快期账号"                 # 快期注册手机号/用户名/邮箱
TQ_PASSWORD = "你的快期密码"                # 快期登录密码

# 将 chan.py 和当前脚本目录都添加到搜索路径
if CHAN_PATH not in sys.path:
    sys.path.insert(0, CHAN_PATH)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# ============================================================
# 导入 chan.py 核心模块
# ============================================================
try:
    from Chan import CChan
    from ChanConfig import CChanConfig
    from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE, BI_DIR, FX_TYPE, BSP_TYPE
    from Common.CTime import CTime
    from KLine.KLine_Unit import CKLine_Unit
    from KLine.KLine_List import CKLine_List
    from DataAPI.CommonStockAPI import CCommonStockApi
    CHAN_AVAILABLE = True
    #print("\n[stock][信息] https://github.com/Vespa314/chan.py 导入成功！！")
except ImportError as e:
    CHAN_AVAILABLE = False
    print(f"\n[错误] chan.py 导入失败: {e}")
    print(f"[提示] 请确保 CHAN_PATH = r'{CHAN_PATH}' 指向正确的 chan.py 仓库目录")
    sys.exit(1)

# 导入通达信数据源适配器（从 chan.py 的 DataAPI 目录）
from DataAPI.TdxAPI import CTdxAPI

# 导入天勤数据源适配器（期货/期指）
try:
    from DataAPI.TqSdkAPI import CTqSdkAPI, fetch_futures_kline, FREQ_SEC_MAP
    TQ_AVAILABLE = True
except ImportError as e:
    CTqSdkAPI = None
    TQ_AVAILABLE = False
    print(f"[stock][警告] 天勤数据源未安装: {e}，期货功能不可用。pip install tqsdk")


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
            # 打印前3条和最后1条原始数据（已关闭）
            # if idx_offset < 3 or i + record_size >= len(data):
            #     print(f"[stock][调试] 记录{idx_offset}: date={date_int} o={o} h={h} l={l} c={c} amount={amount} vol={vol}")
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
    # 打印转换后的前3条和最后1条（已关闭）
    # if len(records) >= 3:
    #     for i in [0, 1, 2, len(records)-1]:
    #         r = records[i]
    #         print(f"[stock][调试] 转换后记录{i}: date={r['dt'].strftime('%Y-%m-%d')} o={r['open']:.2f} h={r['high']:.2f} l={r['low']:.2f} c={r['close']:.2f} vol={r['vol']} amount={r['amount']}")
    # 检测数据缺口（超过30个交易日无数据视为断层）
    # 如果有缺口，只保留最后一段连续数据
    gap_indices = []
    for i in range(1, len(records)):
        prev_dt = records[i-1]["dt"]
        curr_dt = records[i]["dt"]
        gap_days = (curr_dt - prev_dt).days
        if gap_days > 30:
            gap_indices.append(i)
    if gap_indices:
        # 只保留最后一个缺口之后的数据（最后一段连续行情）
        last_gap_idx = gap_indices[-1]
        skipped = last_gap_idx
        old_start = records[0]["dt"].strftime("%Y-%m-%d")
        old_end = records[-1]["dt"].strftime("%Y-%m-%d")
        new_start = records[last_gap_idx]["dt"].strftime("%Y-%m-%d")
        records = records[last_gap_idx:]
        print(f"[stock][警告] 检测到 {len(gap_indices)} 处数据缺口，已截断:")
        print(f"[stock][警告]   原数据范围: {old_start} ~ {old_end} ({skipped+len(records)}条)")
        print(f"[stock][警告]   截断后范围: {new_start} ~ {old_end} ({len(records)}条)")
        print(f"[stock][警告]   跳过 {skipped} 条旧数据，只分析最后一段连续行情")
    else:
        # 无数据缺口，无需截断
        old_start = records[0]["dt"].strftime("%Y-%m-%d")
        old_end = records[-1]["dt"].strftime("%Y-%m-%d")
        print(f"[stock][警告] 检测到 0 处数据缺口，无需截断:")
        print(f"[stock][警告]   原数据范围: {old_start} ~ {old_end} ({len(records)}条)")
        print(f"[stock][警告]   截断后范围: {old_start} ~ {old_end} ({len(records)}条)")
        print(f"[stock][警告]   跳过 0 条旧数据")
    print(f"[stock][调试] 共解析有效记录: {len(records)}条")
    return records


def read_tdx_min_file(filepath, market="sh", aggregate_30m=True):
    """
    解析通达信5分钟线二进制文件(.lc5) — numpy批量读取优化版
    通达信 .lc5 文件格式（每条记录 32 字节，小端序）：
      H(日期) + H(时间) + f(开) + f(高) + f(低) + f(收) + f(成交额) + I(成交量) + I(保留)
      日期编码: year = num // 2048 + 2004, month = (num % 2048) // 100, day = (num % 2048) % 100
      时间: 从0点开始的分钟数 (HH*60+MM)
      价格字段是 float 类型，直接使用
    aggregate_30m=True: 从5分钟线合成为30分钟线（默认，兼容旧行为）
    aggregate_30m=False: 直接返回5分钟线原始数据
    """
    import numpy as np
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

    from collections import OrderedDict
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




def resample_to_weekly(day_records):
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
    from collections import OrderedDict
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
# MACD 计算
# ============================================================
def ema(data, period):
    """计算EMA"""
    result = []
    k = 2.0 / (period + 1)
    for i, val in enumerate(data):
        if i == 0:
            result.append(val)
        else:
            result.append(val * k + result[-1] * (1 - k))
    return result


def calculate_macd(closes, fast=12, slow=26, signal=9):
    """计算MACD"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema(dif, signal)
    macd = [2 * (d - a) for d, a in zip(dif, dea)]
    return [{"dif": dif[i], "dea": dea[i], "macd": macd[i]} for i in range(len(closes))]


# ============================================================
# 获取股票名称
# ============================================================
def get_stock_name(market, code):
    """获取股票名称。优先从本地缓存文件读取，缓存不存在则返回None。
    港股5位代码（如00700）和A股6位代码（如000700）是不同证券，绝不互相回退。
    """
    _load_stock_names_from_cache_file()
    compound_key = market + code
    info = _stock_names_cache.get(compound_key)
    if info and isinstance(info, dict):
        name = info.get("name", "")
        if name:
            return name
    if info and isinstance(info, str) and info:
        return info
    return None


# 流通股本缓存：按需从通达信本地gbbq文件加载
# key: 股票代码(6位), value: 流通股本(股)
_float_shares_cache = {}
_float_shares_loaded = False

# GBBQ缓存文件路径（保存解密后的全部流通股本数据）
_GBBQ_CACHE_FILE = os.path.join(VIPDOC_DIR, "gbbq_cache.json")

# 股票名称缓存：从通达信行情服务器批量获取后保存到本地JSON
# key: 股票代码(6位), value: {"name": "股票名称", "pinyin": "拼音首字母"}
_stock_names_cache = {}
_stock_names_loaded = False
_STOCK_NAMES_CACHE_FILE = os.path.join(VIPDOC_DIR, "stock_names_cache.json")

# 刷新状态（GBBQ + 股票名称共用）
_gbbq_refresh_status = {"running": False, "progress": 0, "total": 0, "loaded": 0, "error": None, "step": ""}


def _load_stock_names_from_cache_file():
    """
    从 stock_names_cache.json 缓存文件加载股票名称到内存。
    返回加载的记录数，文件不存在则返回0。
    版本迁移：自动将旧版纯数字键转换为 market+code 复合键。
    """
    global _stock_names_loaded
    if _stock_names_loaded:
        return len(_stock_names_cache)
    if not os.path.exists(_STOCK_NAMES_CACHE_FILE):
        _inject_known_indices()
        return 0
    try:
        with open(_STOCK_NAMES_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # 迁移：将旧版纯数字键（如 "000001"）转换为复合键（如 "sh000001"）
            migrated = {}
            for key, info in data.items():
                if isinstance(info, dict) and "market" in info and info["market"]:
                    mkt = info["market"]
                    # 纯数字键（旧格式）→ 复合键
                    if key.isdigit():
                        new_key = mkt + key
                    else:
                        new_key = key
                    migrated[new_key] = info
                else:
                    migrated[key] = info
            _stock_names_cache.update(migrated)
            _stock_names_loaded = True
            print(f"[stock][信息] 从缓存文件加载股票名称: {len(_stock_names_cache)} 只")
            _inject_known_indices()
            return len(_stock_names_cache)
    except Exception as e:
        print(f"[stock][警告] 读取股票名称缓存失败: {e}")
    _inject_known_indices()
    return 0


def _inject_known_indices():
    """将常用指数注入缓存，确保中文名称和拼音首字母始终正确。"""
    _KNOWN_INDICES = [
        ("399001", "深证成指", "sz"),
        ("999999", "上证指数", "sh"),
        ("000001", "上证指数", "sh"),
        ("399006", "创业板指", "sz"),
        ("399005", "中小板指", "sz"),
        ("000016", "上证50", "sh"),
        ("000300", "沪深300", "sh"),
        ("399300", "沪深300", "sz"),
        ("000688", "科创50", "sh"),
        ("000852", "中证1000", "sh"),
        ("000905", "中证500", "sh"),
        ("399905", "中证500", "sz"),
        ("399330", "深证100", "sz"),
        ("399673", "创业板50", "sz"),
    ]
    try:
        from pypinyin import lazy_pinyin
        for code, name, market in _KNOWN_INDICES:
            py = "".join([p[0].upper() for p in lazy_pinyin(name) if p])
            compound_key = market + code
            _stock_names_cache[compound_key] = {"name": name, "pinyin": py, "market": market}
    except ImportError:
        for code, name, market in _KNOWN_INDICES:
            compound_key = market + code
            _stock_names_cache[compound_key] = {"name": name, "pinyin": "", "market": market}


def _read_tdx_tnf_file(tnf_path, market):
    """
    读取通达信本地 tnf 文件（shm.tnf / szm.tnf），返回 {compound_key: {"name": name, "pinyin": pinyin}} 字典。
    tnf 文件格式：50字节文件头 + N条314字节的记录。
    关键字段偏移：
      code:   0-6   (6字节ASCII)
      name:   23-41 (18字节GBK编码)
      pinyin: 285-293 (8字节ASCII)
    compound_key 格式: market + code，如 sh000001、sz000001
    """
    result = {}
    if not os.path.exists(tnf_path):
        return result
    try:
        with open(tnf_path, "rb") as f:
            # 跳过50字节文件头
            f.seek(50)
            while True:
                block = f.read(314)
                if len(block) < 314:
                    break
                code = block[0:6].decode("ascii", errors="ignore").strip("\x00").strip()
                # 名称在偏移23处，长度18字节，GBK编码
                name = block[23:23+18].decode("gbk", errors="ignore").strip("\x00").strip()
                # 拼音在偏移285处，长度8字节
                pinyin = block[285:285+8].decode("ascii", errors="ignore").strip("\x00").strip()
                if code and name:
                    compound_key = market + code
                    result[compound_key] = {"name": name, "pinyin": pinyin}
    except Exception as e:
        print(f"[stock][警告] 读取 {os.path.basename(tnf_path)} 失败: {e}")
    return result


def _collect_codes_from_vipdoc():
    """
    从 vipdoc 目录下的 .day 文件名收集所有股票代码。
    适用于没有 shm.tnf/szm.tnf 的通达信普通版。
    返回 {code: {"name": "", "pinyin": "", "market": "sh/sz/hk"}} 字典。

    包含范围：
      A股: 主板(60xxxx/00xxxx)、创业板(30xxxx)、科创板(68xxxx)、北交所(8xxxxx/4xxxxx)
           指数(000001上证/399001深成指/399006创业板指 等 399xxx/000xxx指数)
           ETF(51xxxx沪市ETF/15xxxx深市ETF/159xxx)
      港股: ds/lday 目录下的 31#XXXXX.day 文件

    排除范围：
      债券(11xxxx/12xxxx/13xxxx沪市债券, 10xxxx/11xxxx/12xxxx深市债券)
      基金(50xxxx沪市封闭基金, 16xxxx/18xxxx深市基金)
      其他(1xxxxx北交所债券, 20xxxx/90xxxx B股, 395xxx通达信内部板块)
    """
    result = {}

    # === A股代码过滤规则 ===
    # 上海市场(sh)：包含
    sh_include_prefixes = ("60", "68")  # 主板60, 科创板68
    # 上海市场(sh)：排除（债券、基金、ETF等）
    sh_exclude_prefixes = ("11", "12", "13", "50", "51", "52", "53", "54", "55", "56", "57", "58", "59", "588", "90", "91", "92", "93", "94", "95", "96", "97", "98", "10", "00", "09")
    # 上海指数：000xxx 和 9xxxxx 是上证系列指数
    sh_index_prefixes = ("000", "9")

    # 深圳市场(sz)：包含
    sz_include_prefixes = ("00", "30", "39")  # 主板00, 创业板30, 指数39
    # 深圳市场(sz)：排除（债券、基金、ETF等）
    sz_exclude_prefixes = ("10", "11", "12", "13", "14", "15", "16", "17", "18", "20", "395")

    # 深圳指数：399xxx 是深市指数（如399001深成指、399006创业板指）
    sz_index_prefixes = ("399",)

    def _is_a_stock_code(code, mkt_dir):
        """判断是否为需要包含的A股代码"""
        if not code.isdigit() or len(code) != 6:
            return False

        if mkt_dir == "sh":
            # 上海指数：000xxx（上证系列指数）、9xxxxx
            if code.startswith(sh_index_prefixes):
                return True
            # 上海包含：主板60、科创板68、ETF 51/56/58/59/588
            if code.startswith(sh_include_prefixes):
                return True
            # 上海排除：债券、基金等
            if code.startswith(sh_exclude_prefixes):
                return False
            # 其他上海代码默认排除
            return False

        elif mkt_dir == "sz":
            # 深圳指数：399xxx
            if code.startswith(sz_index_prefixes):
                return True
            # 深圳包含：主板00、创业板30、ETF 15/16/18
            if code.startswith(sz_include_prefixes):
                # 00开头中排除债券(001xxx-009xxx等)、基金
                if code.startswith("00"):
                    # 000001-000999 中，000001是平安银行，000002-000100通常是债券
                    if code == "000001":
                        return True  # 平安银行(深市主板000001)
                    if int(code) <= 100:
                        return False  # 000002-000100 通常是债券/基金
                return True
            # 深圳排除：债券、基金、通达信内部板块等
            if code.startswith(sz_exclude_prefixes):
                return False
            # 其他深圳代码默认排除
            return False

        return False

    # === 收集A股代码 ===
    for mkt_dir, prefix in [("sh", "sh"), ("sz", "sz")]:
        lday_dir = os.path.join(VIPDOC_DIR, mkt_dir, "lday")
        if not os.path.isdir(lday_dir):
            continue
        for fname in os.listdir(lday_dir):
            if fname.startswith(prefix) and fname.endswith(".day"):
                code = fname[len(prefix):-4]
                if _is_a_stock_code(code, mkt_dir):
                    compound_key = mkt_dir + code
                    if compound_key not in result:
                        result[compound_key] = {"name": "", "pinyin": "", "market": mkt_dir}

    # === 收集港股代码（ds目录）===
    ds_lday_dir = os.path.join(VIPDOC_DIR, "ds", "lday")
    if os.path.isdir(ds_lday_dir):
        for fname in os.listdir(ds_lday_dir):
            if fname.startswith("31#") and fname.endswith(".day"):
                # 港股格式：31#00700.day
                code = fname[3:-4]  # 提取 00700
                if code.isdigit():
                    # 港股代码统一补前导零到5位
                    hk_code = code.zfill(5)
                    compound_key = "hk" + hk_code
                    if compound_key not in result:
                        result[compound_key] = {"name": "", "pinyin": "", "market": "hk"}

    print(f"[stock][调试] 代码收集明细: A股{sum(1 for v in result.values() if v['market'] != 'hk')}只, 港股{sum(1 for v in result.values() if v['market'] == 'hk')}只")
    return result


def _fetch_names_from_sina_once(codes_dict):
    """
    一次性从新浪财经API获取股票名称，用于首次建立缓存。
    参数 codes_dict: {code: {"name": "", ...}} —— 只获取 name 为空的条目。
    返回补充了多少条名称。
    注意：新浪API不支持A股和港股混合请求，必须分开调用。
    """
    import urllib.request
    import time

    # 只获取没有名称的代码
    codes_missing = [compound_key for compound_key, info in codes_dict.items() if not info.get("name")]
    if not codes_missing:
        return 0

    # 按市场分组：A股和港股必须分开请求
    a_stock_codes = []
    hk_codes = []
    compound_key_map = {}  # sh000001 -> 000001
    for compound_key in codes_missing:
        market = codes_dict[compound_key].get("market", "")
        # 从复合键提取纯代码：去掉前缀 sh/sz/hk
        if market and compound_key.startswith(market):
            bare_code = compound_key[len(market):]
        else:
            bare_code = compound_key
        compound_key_map[bare_code] = compound_key
        if market == "hk":
            hk_codes.append(bare_code)
        else:
            a_stock_codes.append((bare_code, market))

    print(f"[stock][信息] 从新浪API获取名称：A股{len(a_stock_codes)}只 + 港股{len(hk_codes)}只")
    filled = 0
    hk_filled = 0
    batch_size = 50

    # === 第一轮：A股 ===
    if a_stock_codes:
        total_batches = (len(a_stock_codes) - 1) // batch_size + 1
        for i in range(0, len(a_stock_codes), batch_size):
            batch = a_stock_codes[i:i+batch_size]
            batch_num = i // batch_size + 1
            codes_str_parts = []
            bare_to_compound = {}
            for bare_code, market in batch:
                codes_str_parts.append(f"{market}{bare_code}")
                bare_to_compound[bare_code] = market + bare_code
            codes_str = ",".join(codes_str_parts)
            url = f"http://hq.sinajs.cn/list={codes_str}"
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.sina.com.cn/"
                })
                resp = urllib.request.urlopen(req, timeout=15)
                content = resp.read().decode("gbk", errors="ignore")
                for line in content.strip().split("\n"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    var_part, val_part = line.split("=", 1)
                    val_part = val_part.strip().strip('"').strip(";").strip('"')
                    if not val_part:
                        continue
                    var_name = var_part.strip().replace("var ", "")
                    for mkt_prefix in ("sh", "sz"):
                        marker = f"hq_str_{mkt_prefix}"
                        if var_name.startswith(marker):
                            bare_code = var_name[len(marker):]
                            compound_key = bare_to_compound.get(bare_code)
                            if not compound_key:
                                continue
                            fields = val_part.split(",")
                            if len(fields) >= 1:
                                name = fields[0].strip()
                                if name and compound_key in codes_dict:
                                    codes_dict[compound_key]["name"] = name
                                    filled += 1
                            break
                if batch_num % 20 == 0 or batch_num == total_batches:
                    print(f"[stock][信息] 新浪API(A股): {batch_num}/{total_batches}, 累计{filled}只")
            except Exception as e:
                print(f"[stock][警告] 新浪API(A股)批次{batch_num}失败: {e}")
            if batch_num < total_batches:
                time.sleep(0.5)

    # === 第二轮：港股（用腾讯财经API，新浪港股接口已失效） ===
    if hk_codes:
        total_batches = (len(hk_codes) - 1) // batch_size + 1
        hk_filled = 0
        for i in range(0, len(hk_codes), batch_size):
            batch = hk_codes[i:i+batch_size]
            batch_num = i // batch_size + 1
            # 腾讯财经API：支持多只股票，用逗号分隔
            codes_str = ",".join([f"hk{code}" for code in batch])
            url = f"https://qt.gtimg.cn/q={codes_str}"
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.qq.com/"
                })
                resp = urllib.request.urlopen(req, timeout=15)
                content = resp.read().decode("gbk", errors="ignore")
                # 解析格式：v_hk00700="1~腾讯控股~00700~...";
                for line in content.strip().split(";"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    var_part, val_part = line.split("=", 1)
                    val_part = val_part.strip().strip('"').strip(";")
                    if not val_part:
                        continue
                    # 提取代码：v_hk00700 -> 00700
                    var_name = var_part.strip().replace("v_", "").replace("hk", "")
                    bare_code = var_name.strip()
                    compound_key = "hk" + bare_code
                    fields = val_part.split("~")
                    if len(fields) >= 2:
                        name = fields[1].strip()  # 股票名称在第2个字段
                        if name and compound_key in codes_dict:
                            codes_dict[compound_key]["name"] = name
                            filled += 1
                            hk_filled += 1
                if batch_num % 10 == 0 or batch_num == total_batches:
                    print(f"[stock][信息] 腾讯API(港股): {batch_num}/{total_batches}, 本轮累计{hk_filled}只")
            except Exception as e:
                print(f"[stock][警告] 腾讯API(港股)批次{batch_num}失败: {e}")
            if batch_num < total_batches:
                time.sleep(0.5)

    print(f"[stock][信息] API补全完成: 共{filled}只 (A股{filled-hk_filled}, 港股{hk_filled})")
    return filled


def _refresh_stock_names():
    """
    从通达信本地文件批量获取全市场股票名称，保存到 stock_names_cache.json。
    数据来源优先级：
      1. T0002/hq_cache/shm.tnf + szm.tnf（专业版/券商版，完全离线）
      2. vipdoc/*.day 文件名（普通版，无tnf文件时的降级方案）
      3. 新浪财经API（仅在缓存为空时调用一次，建立初始缓存）
    """
    global _stock_names_cache, _stock_names_loaded, _gbbq_refresh_status

    # === 先加载已有缓存，新数据合并进去，不覆盖 ===
    raw_names = {}
    _load_stock_names_from_cache_file()
    if _stock_names_cache:
        for code, info in _stock_names_cache.items():
            if isinstance(info, dict):
                raw_names[code] = info
            else:
                raw_names[code] = {"name": info, "pinyin": ""}
        print(f"[stock][信息] 已加载现有缓存 {len(raw_names)} 只，将在此基础上合并新数据")
    else:
        print("[stock][信息] 无现有缓存，将从通达信本地文件全新读取")

    market_stats = {}
    has_tnf = False

    # === 方案1: 读取沪市本地文件 shm.tnf（专业版/券商版）===
    shm_path = os.path.join(TDX_HQ_CACHE, "shm.tnf")
    if os.path.exists(shm_path):
        sh_data = _read_tdx_tnf_file(shm_path, "sh")
        for code, info in sh_data.items():
            raw_names[code] = info
        market_stats["上海"] = {"status": "成功(tnf本地文件)", "count": len(sh_data)}
        print(f"[stock][信息] 上海市场读取完成: {len(sh_data)} 只 (来自 {shm_path})")
        has_tnf = True
    else:
        market_stats["上海"] = {"status": "tnf文件不存在", "count": 0}
        print(f"[stock][信息] 沪市tnf文件不存在: {shm_path}")

    # === 方案1: 读取深市本地文件 szm.tnf（专业版/券商版）===
    szm_path = os.path.join(TDX_HQ_CACHE, "szm.tnf")
    if os.path.exists(szm_path):
        sz_data = _read_tdx_tnf_file(szm_path, "sz")
        for code, info in sz_data.items():
            raw_names[code] = info
        market_stats["深圳"] = {"status": "成功(tnf本地文件)", "count": len(sz_data)}
        print(f"[stock][信息] 深圳市场读取完成: {len(sz_data)} 只 (来自 {szm_path})")
        has_tnf = True
    else:
        market_stats["深圳"] = {"status": "tnf文件不存在", "count": 0}
        print(f"[stock][信息] 深市tnf文件不存在: {szm_path}")

    # === 方案2: 无tnf文件时，从vipdoc的.day文件名收集代码（普通版降级）===
    if not has_tnf:
        print("[stock][信息] 未找到tnf文件，启用普通版降级方案：从vipdoc文件名收集代码...")
        vipdoc_codes = _collect_codes_from_vipdoc()
        print(f"[stock][信息] 从vipdoc文件名收集到 {len(vipdoc_codes)} 只代码")
        # 合并到 raw_names，已有名称的保留
        for code, info in vipdoc_codes.items():
            if code not in raw_names:
                raw_names[code] = info
            elif not raw_names[code].get("name"):
                raw_names[code]["name"] = info.get("name", "")
        market_stats["上海"] = {"status": "降级(vipdoc文件名)", "count": len(vipdoc_codes)}
        market_stats["深圳"] = {"status": "降级(vipdoc文件名)", "count": len(vipdoc_codes)}

    # === 方案3: 补全缺失的名称 ===
    # 即使已有缓存，如果有新发现的代码（如港股）没有名称，也要补全
    codes_without_name = [c for c, info in raw_names.items() if not info.get("name")]
    if codes_without_name:
        print(f"[stock][信息] 有 {len(codes_without_name)} 只代码无名称，尝试从新浪API补全...")
        temp_dict = {c: raw_names[c] for c in codes_without_name}
        filled = _fetch_names_from_sina_once(temp_dict)
        for code, info in temp_dict.items():
            if info.get("name"):
                raw_names[code] = info
        market_stats["新浪API补全"] = {"status": "补全", "count": filled}

    # === 补充通达信板块指数名称（88xxxx系列，如880491半导体）===
    tdxzs_file = os.path.join(TDX_HQ_CACHE, "tdxzs.cfg")
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
                        if name and code and code not in raw_names:
                            # 通达信板块指数代码（88xxxx），使用 sh 市场前缀
                            compound_key = "sh" + code
                            raw_names[compound_key] = {"name": name, "pinyin": "", "market": "sh"}
        except Exception as e:
            print(f"[stock][警告] 读取tdxzs.cfg失败: {e}")

    # === 统一用pypinyin生成拼音首字母（忽略tnf文件中的拼音，确保格式一致） ===
    try:
        from pypinyin import lazy_pinyin
        all_names = {}
        for code, info in raw_names.items():
            if isinstance(info, dict):
                name = info.get("name", "")
                market = info.get("market", "")  # 保留市场字段
            else:
                name = str(info)
                market = ""
            # 始终用pypinyin生成拼音首字母，确保搜索的一致性
            pinyin = ""
            if name:
                try:
                    py_list = lazy_pinyin(name)
                    pinyin = "".join([p[0].upper() for p in py_list if p])
                except Exception:
                    pinyin = ""
            all_names[code] = {"name": name, "pinyin": pinyin, "market": market}
    except ImportError:
        all_names = {}
        for code, info in raw_names.items():
            if isinstance(info, dict):
                name = info.get("name", "")
                market = info.get("market", "")
            else:
                name = str(info)
                market = ""
            all_names[code] = {"name": name, "pinyin": "", "market": market}

    # === 过滤 ST、*ST、退市股票，不写入缓存 ===
    filtered_count = 0
    for code in list(all_names.keys()):
        name = all_names[code].get("name", "")
        if not name or name.startswith("*ST") or name.startswith("ST") or "退" in name:
            del all_names[code]
            filtered_count += 1
    if filtered_count > 0:
        print(f"[stock][信息] 过滤掉 {filtered_count} 只（ST/*ST/退市）")

    if all_names:
        os.makedirs(os.path.dirname(_STOCK_NAMES_CACHE_FILE), exist_ok=True)
        with open(_STOCK_NAMES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_names, f, ensure_ascii=False)
        _stock_names_cache = all_names
        _stock_names_loaded = True
        _inject_known_indices()
        sh_count = sum(1 for c in all_names if all_names[c].get("market") == "sh")
        sz_count = sum(1 for c in all_names if all_names[c].get("market") == "sz")
        print(f"[stock][信息] 股票名称刷新完成: 共{len(all_names)}只 (上海{sh_count}, 深圳{sz_count}), 已保存到 {_STOCK_NAMES_CACHE_FILE}")
    else:
        print("[stock][警告] 股票名称刷新失败: 未获取到任何数据")

    print(f"[stock][信息] 市场拉取汇总: {market_stats}")

    # 全部刷新完成，标记状态
    _gbbq_refresh_status["running"] = False
    _gbbq_refresh_status["step"] = ""


def _load_float_shares_from_cache_file():
    """
    从 gbbq_cache.json 缓存文件加载流通股本到内存。
    返回加载的记录数，文件不存在则返回0。
    """
    global _float_shares_loaded
    if _float_shares_loaded:
        return len(_float_shares_cache)
    if not os.path.exists(_GBBQ_CACHE_FILE):
        return 0
    try:
        with open(_GBBQ_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _float_shares_cache.update(data)
            _float_shares_loaded = True
            print(f"[stock][信息] 从缓存文件加载流通股本: {len(_float_shares_cache)} 只")
            return len(_float_shares_cache)
    except Exception as e:
        print(f"[stock][警告] 读取缓存文件失败: {e}")
    return 0


def _refresh_gbbq_to_file():
    """
    解密全部GBBQ记录，将有效流通股本保存到 gbbq_cache.json。
    在后台线程中运行，通过 _gbbq_refresh_status 报告进度。
    """
    global _float_shares_cache, _float_shares_loaded, _gbbq_refresh_status

    _gbbq_refresh_status = {"running": True, "progress": 0, "total": 0, "loaded": 0, "error": None, "step": "正在刷新GBBQ文件..."}

    gbbq_file = os.path.join(TDX_HQ_CACHE, "gbbq")
    if not os.path.exists(gbbq_file):
        _gbbq_refresh_status["error"] = f"gbbq文件不存在: {gbbq_file}"
        _gbbq_refresh_status["running"] = False
        return

    try:
        from pytdx.reader.gbbq_reader import GbbqReader
        reader = GbbqReader()
        bin_keys = bytes.fromhex(reader.hexdump_keys)
    except ImportError:
        _gbbq_refresh_status["error"] = "pytdx 未安装，无法解密gbbq"
        _gbbq_refresh_status["running"] = False
        return
    except Exception as e:
        _gbbq_refresh_status["error"] = f"读取密钥失败: {e}"
        _gbbq_refresh_status["running"] = False
        return

    try:
        with open(gbbq_file, "rb") as f:
            content = f.read()

        count = struct.unpack("<I", content[0:4])[0]
        data_offset = 4
        _gbbq_refresh_status["total"] = count

        # 收集所有有效记录，按代码分组保留最新日期
        records_by_code = {}
        processed = 0

        for _ in range(count):
            try:
                rec = _decrypt_gbbq_record(content, data_offset, bin_keys)
                data_offset += 29
                code = rec[1].strip()

                # 只保留 category in {2,3,5,9} 且日期最新的
                if rec[3] in (2, 3, 5, 9):
                    if code not in records_by_code or rec[2] > records_by_code[code][2]:
                        records_by_code[code] = rec
            except Exception:
                pass

            processed += 1
            # 每处理1万条更新一次进度
            if processed % 10000 == 0:
                _gbbq_refresh_status["progress"] = processed

        # 提取流通股本，构建 {code: 流通股数} 字典
        result = {}
        loaded = 0
        for code, rec in records_by_code.items():
            shares_wan = rec[4]  # hongli_panqianliutong
            if shares_wan and shares_wan > 0:
                result[code] = float(shares_wan) * 10000
                loaded += 1

        # 保存到缓存文件
        os.makedirs(os.path.dirname(_GBBQ_CACHE_FILE), exist_ok=True)
        with open(_GBBQ_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)

        # 同时更新内存缓存
        _float_shares_cache = result
        _float_shares_loaded = True

        _gbbq_refresh_status["progress"] = count
        _gbbq_refresh_status["loaded"] = loaded
        print(f"[stock][信息] GBBQ刷新完成: 总{count}条, 有效{len(records_by_code)}只, 加载{loaded}只, 已保存到 {_GBBQ_CACHE_FILE}")

        # GBBQ完成后，继续刷新股票名称
        _gbbq_refresh_status["step"] = "正在刷新股票名称..."
        _refresh_stock_names()

    except Exception as e:
        _gbbq_refresh_status["error"] = str(e)
        _gbbq_refresh_status["running"] = False
        _gbbq_refresh_status["step"] = "GBBQ刷新失败"
        print(f"[错误] GBBQ刷新失败: {e}")
        import traceback
        traceback.print_exc()

def _decrypt_gbbq_record(encrypt_data, data_offset, bin_keys):
    """解密单条 gbbq 记录（29字节），返回 (market, code, datetime, category,
    hongli_panqianliutong, peigujia_qianzongguben, songgu_qianzongguben, peigu_houzongguben)"""
    from ctypes import c_uint32
    clear_data = bytearray()
    for i in range(3):
        (eax,) = struct.unpack("<I", bin_keys[0x44: 0x44 + 4])
        (ebx,) = struct.unpack("<I", encrypt_data[data_offset: data_offset + 4])
        num = c_uint32(eax ^ ebx).value
        (numold,) = struct.unpack("<I", encrypt_data[data_offset + 0x4: data_offset + 0x4 + 4])
        for j in reversed(range(4, 0x40 + 4, 4)):
            ebx = (num & 0xff0000) >> 16
            (eax,) = struct.unpack("<I", bin_keys[ebx * 4 + 0x448: ebx * 4 + 0x448 + 4])
            ebx = num >> 24
            (eax_add,) = struct.unpack("<I", bin_keys[ebx * 4 + 0x48: ebx * 4 + 0x48 + 4])
            eax += eax_add
            eax = c_uint32(eax).value
            ebx = (num & 0xff00) >> 8
            (eax_xor,) = struct.unpack("<I", bin_keys[ebx * 4 + 0x848: ebx * 4 + 0x848 + 4])
            eax ^= eax_xor
            eax = c_uint32(eax).value
            ebx = num & 0xff
            (eax_add,) = struct.unpack("<I", bin_keys[ebx * 4 + 0xC48: ebx * 4 + 0xC48 + 4])
            eax += eax_add
            eax = c_uint32(eax).value
            (eax_xor,) = struct.unpack("<I", bin_keys[j: j + 4])
            eax ^= eax_xor
            eax = c_uint32(eax).value
            ebx = num
            num = numold ^ eax
            num = c_uint32(num).value
            numold = ebx
        (numold_op,) = struct.unpack("<I", bin_keys[0: 4])
        numold ^= numold_op
        numold = c_uint32(numold).value
        clear_data.extend(struct.pack("<II", numold, num))
        data_offset += 8
    clear_data.extend(encrypt_data[data_offset: data_offset + 5])
    (v1, v2, v3, v4, v5, v6, v7, v8) = struct.unpack("<B7sIBffff", clear_data)
    return (v1, v2.rstrip(b"\x00").decode("utf-8"), v3, v4, v5, v6, v7, v8)


def _load_float_shares_for_codes(codes):
    """
    从通达信本地 gbbq 文件加载指定股票代码的流通股本。
    优化：直接操作二进制，只解密与自选股匹配的记录，跳过99%无关记录。
    187158条记录中只解密几百条，速度从1分钟降到<1秒。
    """
    if not codes:
        return

    global _float_shares_loaded

    code_set = set(codes)
    gbbq_file = os.path.join(TDX_HQ_CACHE, "gbbq")
    if not os.path.exists(gbbq_file):
        return

    try:
        # 读取 pytdx 的密钥表
        from pytdx.reader.gbbq_reader import GbbqReader
        reader = GbbqReader()
        bin_keys = bytes.fromhex(reader.hexdump_keys)
    except ImportError:
        print("[stock][信息] pytdx 未安装，跳过市值比较")
        return
    except Exception as e:
        print(f"[stock][警告] 读取密钥失败: {e}")
        return

    try:
        with open(gbbq_file, "rb") as f:
            content = f.read()

        count = struct.unpack("<I", content[0:4])[0]
        data_offset = 4

        # 收集所有匹配的记录，按代码分组保留最新日期
        records_by_code = {}
        matched = 0
        skipped = 0

        for _ in range(count):
            # 只解密这条记录
            rec = _decrypt_gbbq_record(content, data_offset, bin_keys)
            data_offset += 29
            code = rec[1].strip()  # rec[1] 已经是 str (decode("utf-8") 后)

            if code not in code_set:
                skipped += 1
                continue

            matched += 1
            # rec: (market, code, datetime, category, hongli, peigu, songgu, houzong)
            # 只保留 category in {2,3,5,9} 且日期最新的
            if rec[3] not in (2, 3, 5, 9):
                continue
            if code not in records_by_code or rec[2] > records_by_code[code][2]:
                records_by_code[code] = rec

        loaded = 0
        for code, rec in records_by_code.items():
            shares_wan = rec[4]  # hongli_panqianliutong
            if shares_wan and shares_wan > 0:
                _float_shares_cache[code] = float(shares_wan) * 10000
                loaded += 1

        _float_shares_loaded = True
        print(f"[stock][信息] 流通股本: 匹配 {matched} 条, 有效 {len(records_by_code)} 只, 加载 {loaded}/{len(code_set)} 只")
    except Exception as e:
        print(f"[stock][警告] 读取 gbbq 失败: {e}")
        import traceback
        traceback.print_exc()


def get_stock_float_mv_local(market, code, last_close):
    """
    从本地缓存计算流通市值（单位：亿元）。
    流通市值 = 最新收盘价 × 流通股本
    港股返回 None（跳过市值比较）。
    优先从缓存文件读取，缓存文件不存在则按需解密。
    """
    if market == "hk":
        return None
    global _float_shares_loaded
    if not _float_shares_loaded:
        # 优先尝试从缓存文件加载
        if _load_float_shares_from_cache_file() == 0:
            # 缓存文件不存在，回退到按需解密
            try:
                _load_float_shares_for_codes([code])
            except Exception:
                pass
    shares = _float_shares_cache.get(code)
    if shares and last_close and last_close > 0:
        return last_close * shares / 100000000  # 元 -> 亿元
    return None


# ============================================================
# 解析股票代码，判断市场
# ============================================================
def parse_stock_code(code):
    """
    解析代码，返回 (market, code)
    market: 'sh' / 'sz' / 'hk' / 'futures'
    """
    code = code.strip().upper()

    # ===== 期货/期指代码识别 =====
    # 格式: EXCHANGE.SYMBOL (如 CFFEX.IM2507, SHFE.rb2505)
    # 或主连: KQ.m@EXCHANGE.SYMBOL, KQ.i@EXCHANGE.SYMBOL
    # 注意：天勤要求 KQ 前缀中的 m/i 小写，所以先 upper() 匹配，再恢复小写
    FUTURE_EXCHANGES = ['CFFEX', 'SHFE', 'DCE', 'CZCE', 'INE', 'GFEX']
    for ex in FUTURE_EXCHANGES:
        if code.startswith(ex + '.'):
            return 'futures', code
        if code.startswith('KQ.M@' + ex + '.'):
            return 'futures', code.replace('KQ.M@', 'KQ.m@', 1)
        if code.startswith('KQ.I@' + ex + '.'):
            return 'futures', code.replace('KQ.I@', 'KQ.i@', 1)
    # ===== 期货代码识别结束 =====

    prefix_match = re.match(r'^(SH|SZ|HK)(\d+)$', code)
    if prefix_match:
        return prefix_match.group(1).lower(), prefix_match.group(2)
    suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK)$', code)
    if suffix_match:
        return suffix_match.group(2).lower(), suffix_match.group(1)
    # 自动判断：5位纯数字优先识别为港股（如 00700）
    if len(code) == 5 and code.isdigit():
        return 'hk', code
    if len(code) == 4 and code.isdigit():
        return 'hk', '0' + code
    # 6位代码：先检查是否是港股（在ds目录下有对应文件）
    if len(code) == 6 and code.isdigit():
        hk_file = os.path.join(VIPDOC_DIR, "ds", "lday", f"31#{code}.day")
        if os.path.exists(hk_file):
            return 'hk', code
    # A股判断
    if code.startswith('6'):
        return 'sh', code
    if code.startswith('5'):
        return 'sh', code  # 5xxxxx: 沪市ETF(51/56/58/59/588)、基金(50)等
    if code.startswith('0') or code.startswith('3'):
        return 'sz', code
    if code.startswith('1'):
        return 'sz', code  # 1xxxxx: 深市ETF(15/16/18)、债券等
    # 搜索
    for m in ['sh', 'sz']:
        f = os.path.join(VIPDOC_DIR, m, "lday", f"{m}{code}.day")
        if os.path.exists(f):
            return m, code
    f = os.path.join(VIPDOC_DIR, "ds", "lday", f"31#{code}.day")
    if os.path.exists(f):
        return 'hk', code
    return None, code


def find_day_file(market, code):
    """查找日线数据文件路径"""
    if market == 'hk':
        return os.path.join(VIPDOC_DIR, "ds", "lday", f"31#{code}.day")
    return os.path.join(VIPDOC_DIR, market, "lday", f"{market}{code}.day")


def _get_kl_type(freq):
    """根据频率字符串返回对应的 KL_TYPE 枚举值"""
    mapping = {
        '15s': KL_TYPE.K_15S, '1m': KL_TYPE.K_1M, '5m': KL_TYPE.K_5M,
        '30m': KL_TYPE.K_30M, '60m': KL_TYPE.K_60M, 'd': KL_TYPE.K_DAY,
        'w': KL_TYPE.K_WEEK,
    }
    return mapping.get(freq, KL_TYPE.K_DAY)

def _get_freq_label(freq):
    """根据频率字符串返回中文标签"""
    labels = {'15s': '15秒', '1m': '1分钟', '5m': '5分钟', '30m': '30分钟', '60m': '60分钟', 'd': '日线', 'w': '周线'}
    return labels.get(freq, '日线')


def _make_chan_config():
    """统一的缠论配置，股票和期货共用"""
    from ChanConfig import CChanConfig
    return CChanConfig({
        "trigger_step": True,
        "bi_fx_check": "loss",
        "bi_allow_sub_peak": True,
        "bi_algo": "normal",
        "bi_strict": True,
        "bi_end_is_peak": False,
        "seg_algo": "chan",
        "zs_algo": "over_seg",
        "zs_combine": False,
        "bs_type": "1,1p,2,2s,3a,3b",
        "min_zs_cnt": 1,
        "bs1_peak": True,
        "divergence_rate": 0.9,
        "bsp2_follow_1": True,
        "max_bs2_rate": 0.9,
        "bsp2s_follow_2": False,
        "max_bsp2s_lv": None,
        "bsp3_follow_1": False,
        "strict_bsp3": False,
        "bsp3_peak": False,
        "bsp3a_max_zs_cnt": 2,
        "macd_algo": "full_area",
    })


def _ctime_to_fmt(ctime, date_fmt):
    """将 CTime 对象转为目标日期格式。
    使用 CTime.ts（Unix时间戳）精确格式化，避免 to_str() 丢失秒信息。"""
    if ctime is None:
        return ""
    if hasattr(ctime, 'ts'):
        return datetime.fromtimestamp(ctime.ts).strftime(date_fmt)
    # 兼容旧调用：传入字符串
    if isinstance(ctime, str):
        s = ctime.replace("/", "-")
        if date_fmt.endswith(":%S") and len(s) == 16:
            s += ":00"
        return s
    return ""


# ============================================================
# 缠论分析（chan.py 版本）
# ============================================================
import collections

_MAX_CACHE_SIZE = 50  # 最多缓存 50 个 (股票, 周期) 组合
_analysis_cache = collections.OrderedDict()
_CACHE_VERSION = "v2"  # 修改分析结果结构时递增，使旧缓存自动失效

# 扫描跳过记录（收集后统一打印）
_scan_skip_log = []

# 扫描锁（防止并发扫描导致内存峰值翻倍）
_scan_lock = threading.Lock()

# 股票分析锁（防止并发请求时 CTdxAPI.set_data 被覆盖导致分析结果串数据）
_stock_analysis_lock = threading.Lock()

# 内存保护阈值
_MEMORY_WARN_THRESHOLD_MB = 1500   # 1.5GB 警告
_MEMORY_LIMIT_MB = 2500            # 2.5GB 强制清理


def _cache_put(key, value):
    """写入缓存，超出上限时淘汰最旧的条目（LRU语义）"""
    if key in _analysis_cache:
        del _analysis_cache[key]  # 移到末尾
    elif len(_analysis_cache) >= _MAX_CACHE_SIZE:
        oldest_key = next(iter(_analysis_cache))
        _analysis_cache.pop(oldest_key)
        import gc
        gc.collect()
        print(f"[内存] 缓存已满({_MAX_CACHE_SIZE})，淘汰: {oldest_key}")
    _analysis_cache[key] = value


def _cache_get(key):
    """读取缓存，命中时移到末尾（LRU语义）"""
    if key not in _analysis_cache:
        return None
    value = _analysis_cache.pop(key)
    _analysis_cache[key] = value
    return value


def _check_memory_and_protect():
    """检查内存，超过阈值时自动清理缓存"""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        rss_mb = process.memory_info().rss / (1024 * 1024)
    except Exception:
        return

    import gc
    if rss_mb > _MEMORY_LIMIT_MB:
        _analysis_cache.clear()
        gc.collect()
        print(f"[内存保护] 内存 {rss_mb:.0f}MB 超过上限 {_MEMORY_LIMIT_MB}MB，已清空全部缓存")
    elif rss_mb > _MEMORY_WARN_THRESHOLD_MB:
        keys_to_remove = list(_analysis_cache.keys())[:len(_analysis_cache) // 2]
        for k in keys_to_remove:
            del _analysis_cache[k]
        gc.collect()
        print(f"[内存保护] 内存 {rss_mb:.0f}MB 超过警告线 {_MEMORY_WARN_THRESHOLD_MB}MB，已淘汰一半缓存")

# ============================================================
# 手选进入段选点保存/恢复
# ============================================================
SAVED_POINT_FILE = r"C:\new_tdx_test\vipdoc\double_click_dt.csv"
# CSV列：股票代码,股票名,年K选点,季K选点,月K选点,周K选点,日K选点,30分选点,15分选点,5分选点,1分选点
SAVED_POINT_COLUMNS = ["code", "name", "y", "q", "m", "w", "d", "60m", "30m", "15m", "5m", "1m", "15s"]
# freq -> CSV列名 的映射
FREQ_TO_COL = {"y": "y", "q": "q", "m": "m", "w": "w", "d": "d", "60m": "60m", "30m": "30m", "15m": "15m", "5m": "5m", "1m": "1m", "15s": "15s"}
# 日内周期集合：这些周期的选点保存/恢复需要精确到时分
INTRADAY_FREQS = {"60m", "30m", "15m", "5m", "1m", "15s"}
# 秒级周期：K线时间含秒，保存需精确到 HH:MM:SS
SECOND_FREQS = {"30s", "15s"}


def _find_left_shoulder_time(kl_list, bi_list, bi_idx, freq):
    """
    找到分型左肩第一根原始K线的时间T。

    用户双击的分型K线是合并K线（分型中间K线），分型由三根合并K线组成：
    左肩 | 中间（分型）| 右肩。左肩合并K线可能由多根原始K线经过包含处理形成，
    需要找到左肩合并K线中最左边（最早）的那根原始K线对应的时间。

    参数:
        kl_list: KLine_List对象
        bi_list: 笔列表
        bi_idx: 前端双击命中的笔索引（该笔的begin_klu就是分型中间K线）
        freq: 周期

    返回:
        str: 格式化的时间字符串，如 "2026-01-09" 或 "2026-01-09 10:00"
        None: 定位失败
    """
    entry_bi = bi_list[bi_idx]
    begin_klu = entry_bi.get_begin_klu()  # 分型中间K线对应的klu

    # 在kl_list.lst中找到包含begin_klu的合并K线索引（分型中间位置）
    mid_idx = None
    for i, klc in enumerate(kl_list.lst):
        if hasattr(klc, 'lst') and klc.lst:
            for klu in klc.lst:
                if klu is begin_klu:
                    mid_idx = i
                    break
        if mid_idx is not None:
            break

    if mid_idx is None or mid_idx <= 0:
        print(f"[stock][警告] 无法定位分型中间K线在kl_list.lst中的位置")
        return None

    # 左肩 = 分型合并K线的前一个合并K线
    left_klc = kl_list.lst[mid_idx - 1]

    # 取左肩原始K线序列的第一根（最左边）
    if hasattr(left_klc, 'lst') and left_klc.lst:
        first_klu = left_klc.lst[0]
    else:
        # 没有包含关系，左肩就是一根原始K线
        first_klu = (left_klc.get_high_peak_klu() or left_klc.get_low_peak_klu())

    if first_klu is None:
        print(f"[stock][警告] 无法获取左肩K线单元")
        return None

    raw = first_klu.time.to_str()
    if freq in SECOND_FREQS:
        if hasattr(first_klu.time, 'ts'):
            return datetime.fromtimestamp(first_klu.time.ts).strftime("%Y-%m-%d %H:%M:%S")
        return raw[:19].replace("/", "-")   # fallback
    elif freq in INTRADAY_FREQS:
        return raw[:16].replace("/", "-")   # "2026-01-09 10:00"
    else:
        return raw[:10].replace("/", "-")    # "2026-01-09"


def _format_bi_sdt(bi, freq):
    """
    从笔(bi)的起始K线单元提取日期字符串，用于选点保存和恢复匹配。

    - 日线及以上周期：精确到日，格式 "YYYY-MM-DD"
    - 日内周期（30m/15m/5m/1m）：精确到时分，格式 "YYYY-MM-DD HH:MM"

    保存和恢复必须使用同一个函数，确保格式一致、精确匹配。
    """
    raw = bi.get_begin_klu().time.to_str()  # e.g. "2026/01/09" or "2026/01/09 10:00"
    if freq in SECOND_FREQS:
        return raw[:19].replace("/", "-")   # "2026-01-09 10:00:30"
    elif freq in INTRADAY_FREQS:
        return raw[:16].replace("/", "-")   # "2026-01-09 10:00"
    else:
        return raw[:10].replace("/", "-")    # "2026-01-09"


def _format_klu_time(klu, date_fmt):
    """把 chan.py 的 KLine_Unit 时间格式化为前端统一日期字符串。"""
    if klu is None:
        return ""
    if hasattr(klu.time, 'ts'):
        return datetime.fromtimestamp(klu.time.ts).strftime(date_fmt)
    raw = klu.time.to_str()
    try:
        return datetime.strptime(raw, "%Y/%m/%d %H:%M").strftime(date_fmt)
    except Exception:
        return raw[:16].replace("/", "-") if len(date_fmt) > 10 else raw[:10].replace("/", "-")


def _format_bi_edt(bi, date_fmt):
    """从笔的结束K线单元提取结束时间。"""
    return _format_klu_time(bi.get_end_klu(), date_fmt)


def _bi_overlap_range(bi, zg, zd):
    """判断笔与中枢区间[zd, zg]是否严格重叠，与 chan.py has_overlap 默认语义一致。"""
    return min(zg, bi._high()) > max(zd, bi._low())


def _calc_zs_confirm_edt_from_bis(zs_obj, all_bi_list, date_fmt):
    """
    计算中枢事实确认结束时间。

    zs.end/edt 表示中枢内部最后一笔的结束时间；confirm_edt 表示第一根
    与中枢区间无重叠、且后面已经有 next 的笔的结束时间。这样可以避免
    用尾部无后继的笔过早确认中枢结束；当 next 是虚笔时，也能符合
    trigger_step=True 的实时语义。
    """
    try:
        end_idx = zs_obj.end_bi.idx
        zg, zd = zs_obj.high, zs_obj.low
    except Exception:
        return ""
    for bi in all_bi_list[end_idx + 1:]:
        if _bi_overlap_range(bi, zg, zd):
            continue
        if getattr(bi, "next", None) is None:
            return ""
        return _format_bi_edt(bi, date_fmt)
    return ""


def _calc_zs_confirm_edt_from_manual(zs_record, start_i, bis, date_fmt):
    """
    手选/保存选点模式下，根据自建中枢记录和后续笔序列计算确认结束时间。
    start_i 是中枢内部最后一笔之后的扫描位置。
    """
    zg, zd = zs_record["zg"], zs_record["zd"]
    i = start_i
    while i < len(bis):
        bi = bis[i]
        if _bi_overlap_range(bi, zg, zd):
            i += 1
            continue
        if getattr(bi, "next", None) is None:
            return ""
        return _format_bi_edt(bi, date_fmt)
    return ""


def _load_saved_point_times():
    """从CSV文件加载所有选点记录，返回 {code: {col: value}} 字典"""
    points = {}
    if not os.path.exists(SAVED_POINT_FILE):
        return points
    try:
        import csv
        with open(SAVED_POINT_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = row.get("code", "").strip()
                if code:
                    points[code] = row
    except Exception as e:
        print(f"[stock][警告] 读取选点文件失败: {e}")
    return points

def _save_point_time(code, name, freq, sdt):
    """保存或更新某只股票某个周期的选点"""
    import csv
    col = FREQ_TO_COL.get(freq)
    if not col:
        return
    # 读取现有数据
    rows = []
    if os.path.exists(SAVED_POINT_FILE):
        try:
            with open(SAVED_POINT_FILE, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    rows.append(row)
        except:
            fieldnames = SAVED_POINT_COLUMNS
    else:
        fieldnames = SAVED_POINT_COLUMNS

    # 查找是否已有该代码的记录
    found = False
    for row in rows:
        if row.get("code", "").strip() == code:
            row["name"] = name
            row[col] = sdt
            found = True
            break
    if not found:
        new_row = {"code": code, "name": name}
        for c in SAVED_POINT_COLUMNS[2:]:
            new_row[c] = ""
        new_row[col] = sdt
        rows.append(new_row)

    # 写回文件
    try:
        with open(SAVED_POINT_FILE, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[stock][信息] 保存选点成功: {code} {freq} {col}={sdt}")
    except Exception as e:
        print(f"[stock][警告] 保存选点文件失败: {e}")


def _clear_saved_point_time(code, freq):
    """清除某只股票某个周期在CSV中的选点，同时更新内存缓存"""
    import csv
    col = FREQ_TO_COL.get(freq)
    if not col:
        return
    # 先清除内存缓存（无论CSV是否存在都要执行）
    if code in _saved_point_times:
        if col in _saved_point_times[code]:
            _saved_point_times[code][col] = ""
    if not os.path.exists(SAVED_POINT_FILE):
        return
    rows = []
    try:
        with open(SAVED_POINT_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                rows.append(row)
    except Exception:
        return
    # 清除该代码对应周期的选点
    for row in rows:
        if row.get("code", "").strip() == code:
            row[col] = ""
            break
    try:
        with open(SAVED_POINT_FILE, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[stock][信息] 清除选点成功: {code} {freq}")
    except Exception as e:
        print(f"[stock][警告] 清除选点失败: {e}")

# 启动时加载一次选点数据
_saved_point_times = _load_saved_point_times()


# ============================================================
# 文字标注持久化存储
# ============================================================
ANNOTATIONS_FILE = os.path.join(VIPDOC_DIR, "annotations.json")
_annotations_cache = {}  # { "code_freq": [ { "date": "2024-01-15", "text": "支撑位", "y_offset": 0 }, ... ] }
_annotations_loaded = False


def _load_annotations():
    """从 annotations.json 加载标注数据到内存"""
    global _annotations_cache, _annotations_loaded
    if _annotations_loaded:
        return
    if os.path.exists(ANNOTATIONS_FILE):
        try:
            with open(ANNOTATIONS_FILE, "r", encoding="utf-8") as f:
                _annotations_cache = json.load(f)
            print(f"[stock][信息] 标注数据已加载: {len(_annotations_cache)} 个条目")
        except Exception as e:
            print(f"[stock][警告] 加载标注数据失败: {e}")
            _annotations_cache = {}
    _annotations_loaded = True


def _save_annotations():
    """保存标注数据到 annotations.json"""
    try:
        with open(ANNOTATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(_annotations_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[stock][警告] 保存标注数据失败: {e}")


def _get_annotation_key(code, freq):
    """生成标注缓存的键: {code}_{freq}"""
    return f"{code}_{freq}"


def _get_annotations_for(code, freq):
    """获取某股票某周期的所有标注"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    return _annotations_cache.get(key, [])


def _add_annotation(code, freq, date_str, text, y_offset=0):
    """添加一条标注（自动去重：同日期同文字不重复添加）"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    if key not in _annotations_cache:
        _annotations_cache[key] = []
    # 去重：同日期同文字已存在则不添加
    for ann in _annotations_cache[key]:
        if ann.get("date") == date_str and ann.get("text") == text:
            return False
    _annotations_cache[key].append({
        "date": date_str,
        "text": text,
        "y_offset": y_offset,
    })
    _save_annotations()
    return True


def _delete_annotation(code, freq, date_str, text):
    """删除一条标注"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    if key not in _annotations_cache:
        return False
    before = len(_annotations_cache[key])
    _annotations_cache[key] = [
        ann for ann in _annotations_cache[key]
        if not (ann.get("date") == date_str and ann.get("text") == text)
    ]
    if len(_annotations_cache[key]) < before:
        if not _annotations_cache[key]:
            del _annotations_cache[key]  # 清理空列表
        _save_annotations()
        return True
    return False


def _delete_annotation_by_date(code, freq, date_str):
    """删除某日期下所有标注"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    if key not in _annotations_cache:
        return False
    before = len(_annotations_cache[key])
    _annotations_cache[key] = [
        ann for ann in _annotations_cache[key]
        if ann.get("date") != date_str
    ]
    if len(_annotations_cache[key]) < before:
        if not _annotations_cache[key]:
            del _annotations_cache[key]
        _save_annotations()
        return True
    return False


def _delete_all_annotations(code, freq):
    """删除某股票某周期下全部标注"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    if key not in _annotations_cache or not _annotations_cache[key]:
        return False
    del _annotations_cache[key]
    _save_annotations()
    return True


def _get_annotated_codes(freq=""):
    """获取所有有标注的股票代码+周期列表，用于自选扫描
    返回 bare_code + market + name，方便前端与自选股列表交叉匹配。
    例如 key "000001.SH_d" → {"code": "000001", "market": "SH", "name": "上证指数", "freq": "d", "count": N}
    期货 key "KQ.m@SHFE.rb_d" → {"code": "KQ.m@SHFE.rb", "market": "", "name": "", "freq": "d", "count": N}
    """
    _load_annotations()
    _load_stock_names_from_cache_file()
    result = []
    for key, anns in _annotations_cache.items():
        if not anns:
            continue
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        code_with_suffix, key_freq = parts
        if freq and key_freq != freq:
            continue

        # 解析市场后缀: 000001.SH → bare_code=000001, market=SH
        # 期货代码（如 KQ.m@SHFE.rb）没有市场后缀，保持不变
        market = ""
        bare_code = code_with_suffix
        for suffix in [".SH", ".SZ", ".HK", ".BJ"]:
            if code_with_suffix.upper().endswith(suffix):
                market = suffix[1:]  # 去掉点号
                bare_code = code_with_suffix[:-len(suffix)]
                break

        # 查询股票名称
        name = ""
        if market and bare_code:
            lookup_key = market.lower() + bare_code
            info = _stock_names_cache.get(lookup_key, {})
            if isinstance(info, dict):
                name = info.get("name", "")
            elif info:
                name = str(info)

        result.append({
            "code": bare_code,
            "market": market,
            "name": name,
            "freq": key_freq,
            "count": len(anns),
            "annotations": [{"date": a.get("date", ""), "text": a.get("text", "")} for a in anns if a.get("text")]
        })
    return result


# 启动时加载标注数据
_load_annotations()


def _analyze_futures_internal(code, freq="d", end_date=None):
    """
    使用天勤数据源 + chan.py 进行期货/期指缠论分析（静态模式，HTTP 请求）
    与 _analyze_stock_internal 输出格式一致，复用后续的 K线/笔/线段/中枢/买卖点提取逻辑。
    """
    import time
    t_start = time.time()

    if not TQ_AVAILABLE or CTqSdkAPI is None:
        return {"error": "天勤数据源未安装，请执行: pip install tqsdk"}

    # 确定周期秒数
    freq_sec = FREQ_SEC_MAP.get(freq, 86400)

    # 0. 尝试复用 SSE 引擎缓存（避免重复连接+拉取+分析）
    cached = None
    try:
        from DataAPI.TqSdkAPI import get_futures_cache
        cached = get_futures_cache(code, freq_sec=freq_sec)
    except Exception:
        pass

    if cached and not end_date and cached.get("freq_sec") == freq_sec:
        # 直接使用引擎缓存的 chan + records，跳过拉取和 chan.py 分析
        print(f"[缓存] ⑴ 复用SSE缓存: 跳过拉取+分析")
        records = cached["records"]
        chan = cached["chan"]
        stock_name = cached["name"]
        kl_list = chan[_get_kl_type(freq)]
        print(f"[缓存] ⑶ 复用缓存chan: 合并K线={len(kl_list.lst)}, 笔={len(kl_list.bi_list)}, 中枢={len(kl_list.zs_list)}")
    else:
        # 1. 拉取历史K线
        t_fetch = time.time()
        full_records = fetch_futures_kline(code, freq_sec=freq_sec)
        print(f"[拉取] ⑴ 天勤拉取K线: {time.time()-t_fetch:.3f}s, {len(full_records)}条")
        if len(full_records) < 5:
            return {"error": f"K线数据不足: 仅{len(full_records)}条"}

        # 2. 截断（end_date 复盘模式）
        if end_date:
            target_dt = None
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                try:
                    target_dt = datetime.strptime(end_date, fmt)
                    break
                except ValueError:
                    continue
            if target_dt is None:
                return {"error": f"无法解析日期: {end_date}"}
            records = [r for r in full_records if r["dt"] <= target_dt]
            if len(records) < 5:
                return {"error": f"截断后K线数据不足: 仅{len(records)}条"}
        else:
            records = full_records

        # 3. 注入数据源
        t_set = time.time()
        CTqSdkAPI.set_data(records, symbol=code)
        print(f"[分析] ⑵ 注入数据源: {time.time()-t_set:.3f}s")

        # 4. 获取品种名称
        stock_name = _get_futures_name(code)

        # 5. 创建 CChan 并消费
        import gc
        t0 = time.time()
        config = _make_chan_config()

        try:
            try:
                from Math.Demark import CDemarkEngine
                CDemarkEngine.DEMARK_LEN = 9
                CDemarkEngine.SETUP_BIAS = 4
                CDemarkEngine.COUNTDOWN_BIAS = 2
                CDemarkEngine.MAX_COUNTDOWN = 13
                CDemarkEngine.TIAOKONG_ST = True
                CDemarkEngine.SETUP_CMP2CLOSE = True
                CDemarkEngine.COUNTDOWN_CMP2CLOSE = True
            except Exception:
                pass
            chan = CChan(
                code=code,
                begin_time=None,
                end_time=None,
                data_src="custom:TqSdkAPI.CTqSdkAPI",
                lv_list=[_get_kl_type(freq)],
                config=config,
                autype=AUTYPE.NONE,
            )
            for _snapshot in chan.step_load():
                pass
        except Exception as e:
            return {"error": f"chan.py 期货分析失败: {e}"}

        kl_list = chan[_get_kl_type(freq)]
        print(f"[分析] ⑶ chan.py分析: {time.time()-t0:.3f}s, 合并K线={len(kl_list.lst)}, 笔={len(kl_list.bi_list)}, 中枢={len(kl_list.zs_list)}")

    # 6. 提取结果（与股票一致的格式，用 records 而非 kl_list）

    t_extract = time.time()
    closes = [r["close"] for r in records]
    macd_list = calculate_macd(closes)
    date_fmt = "%Y-%m-%d %H:%M:%S" if freq in INTRADAY_FREQS else "%Y-%m-%d"

    # K线数据（从 records 构建，与股票代码一致）
    kline_data = []
    for i, row in enumerate(records):
        macd = macd_list[i] if i < len(macd_list) else {"dif": 0, "dea": 0, "macd": 0}
        kline_data.append({
            "date": row["dt"].strftime(date_fmt),
            "timestamp": int(row["dt"].timestamp()) * 1000,
            "open": row["open"], "high": row["high"],
            "low": row["low"], "close": row["close"],
            "vol": row["vol"], "amount": row["amount"],
            "dif": round(macd["dif"], 4),
            "dea": round(macd["dea"], 4),
            "macd": round(macd["macd"], 4),
        })

    # 笔、线段、中枢、买卖点提取（与股票逻辑完全一致）
    bi_data, fx_data, seg_data, zs_data, zs_stars, bsp_data, white_hline = [], [], [], [], [], [], None

    # 提取笔（字段与股票代码完全一致）
    for bi in kl_list.bi_list:
        try:
            direction = "up" if bi.is_up() else "down"
            begin_val = bi.get_begin_val()
            end_val = bi.get_end_val()
            power = abs(end_val - begin_val)
            begin_klu = bi.get_begin_klu()
            end_klu = bi.get_end_klu()
            sdt_str = _ctime_to_fmt(begin_klu.time if begin_klu else None, date_fmt)
            edt_str = _ctime_to_fmt(end_klu.time if end_klu else None, date_fmt)
            try:
                sdt_ts = int(begin_klu.time.ts * 1000) if begin_klu and hasattr(begin_klu.time, 'ts') else 0
            except:
                sdt_ts = 0
            try:
                edt_ts = int(end_klu.time.ts * 1000) if end_klu and hasattr(end_klu.time, 'ts') else 0
            except:
                edt_ts = 0

            # 分型索引（在 kl_list.lst 中定位）
            begin_fx_idx = None
            if hasattr(bi, 'begin_klc') and bi.begin_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.begin_klc:
                        begin_fx_idx = idx
                        break
            end_fx_idx = None
            if hasattr(bi, 'end_klc') and bi.end_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.end_klc:
                        end_fx_idx = idx
                        break

            # 分型肩部原始K线时间（与股票一致的左肩/右肩逻辑）
            fx_a_raw_dt = ""
            fx_b_raw_dt = ""
            try:
                begin_klc = bi.begin_klc
                end_klc = bi.end_klc
                # A: begin分型左肩第一根原始K线时间
                if begin_fx_idx is not None and begin_fx_idx > 0:
                    left_klc = kl_list.lst[begin_fx_idx - 1]
                else:
                    left_klc = None
                a_klu = left_klc.lst[0] if (left_klc and left_klc.lst) else (begin_klc.lst[0] if begin_klc.lst else None)
                if a_klu:
                    fx_a_raw_dt = _ctime_to_fmt(a_klu.time, date_fmt)
                # B: end分型右肩最后一根原始K线时间
                if end_fx_idx is not None:
                    right_klc = kl_list.lst[end_fx_idx + 1] if end_fx_idx + 1 < len(kl_list.lst) else None
                else:
                    right_klc = None
                b_klu = right_klc.lst[-1] if (right_klc and right_klc.lst) else (end_klc.lst[-1] if end_klc.lst else None)
                if b_klu:
                    fx_b_raw_dt = _ctime_to_fmt(b_klu.time, date_fmt)
            except Exception:
                pass

            bi_data.append({
                "sdt": sdt_str, "edt": edt_str,
                "sdt_ts": sdt_ts, "edt_ts": edt_ts,
                "direction": direction,
                "fx_a_price": round(begin_val, 2),
                "fx_b_price": round(end_val, 2),
                "high": round(bi._high(), 2),
                "low": round(bi._low(), 2),
                "power": round(power, 2),
                "is_sure": getattr(bi, 'is_sure', True),
                "end_fx_idx": end_fx_idx,
                "begin_fx_idx": begin_fx_idx,
                "fx_a_raw_dt": fx_a_raw_dt,
                "fx_b_raw_dt": fx_b_raw_dt,
            })
        except Exception:
            pass

    # 提取分型（与股票路径一致）
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            mark = "G"
            price = klc.high
            klu = klc.get_high_peak_klu()
            fx_date = _ctime_to_fmt(klu.time, date_fmt) if klu else ""
            fx_data.append({
                "date": fx_date,
                "timestamp": int(klu.time.ts * 1000) if klu and hasattr(klu.time, 'ts') else 0,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            mark = "D"
            price = klc.low
            klu = klc.get_low_peak_klu()
            fx_date = _ctime_to_fmt(klu.time, date_fmt) if klu else ""
            fx_data.append({
                "date": fx_date,
                "timestamp": int(klu.time.ts * 1000) if klu and hasattr(klu.time, 'ts') else 0,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })

    # 提取线段（与股票代码完全一致）
    for seg in kl_list.seg_list:
        try:
            direction = "up" if seg.is_up() else "down"
            begin_klu = seg.get_begin_klu()
            end_klu = seg.get_end_klu()
            sdt = _ctime_to_fmt(begin_klu.time, date_fmt) if begin_klu else ""
            edt = _ctime_to_fmt(end_klu.time, date_fmt) if end_klu else ""
            if direction == "up":
                begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
                end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
            else:
                begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
                end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
            seg_data.append({
                "sdt": sdt, "edt": edt,
                "direction": direction,
                "begin_price": begin_price,
                "end_price": end_price,
                "high": round(seg._high(), 2),
                "low": round(seg._low(), 2),
                "amp": round(seg.amp(), 2),
            })
        except Exception:
            pass

    # 提取中枢（与股票代码完全一致，使用 _format_klu_time）
    for zs in kl_list.zs_list:
        try:
            zs_data.append({
                "sdt": _format_klu_time(zs.begin, date_fmt),
                "edt": _format_klu_time(zs.end, date_fmt),
                "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, date_fmt),
                "zg": round(zs.high, 2),
                "zd": round(zs.low, 2),
                "gg": round(zs.peak_high, 2),
                "dd": round(zs.peak_low, 2),
                "dir": "up" if zs.bi_in and zs.bi_in.is_up() else "down",
            })
        except Exception as e:
            print(f"[调试] 中枢提取失败: {type(e).__name__}: {e}")

    # 中枢五角星（与股票代码完全一致）
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        star_date = _ctime_to_fmt(begin_klu.time, date_fmt)
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "D",
                "color": "red",
            })
        else:
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "G",
                "color": "green",
            })

    # 提取买卖点（与股票路径一致）
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            bsp_date = _ctime_to_fmt(bsp.klu.time, date_fmt)
            try:
                bsp_ts = int(datetime.strptime(bsp_date, date_fmt).timestamp()) * 1000
            except:
                bsp_ts = 0
            bsp_data.append({
                "date": bsp_date, "timestamp": bsp_ts,
                "type": bsp.type2str(),
                "is_buy": bsp.is_buy,
                "price": bsp.klu.close,
                "high": bsp.klu.high,
                "low": bsp.klu.low,
            })
    except Exception as e:
        print(f"[调试] 期货获取买卖点失败: {e}")

    # 7. 组装结果
    print(f"[分析] ⑷ 提取结果(K线/笔/分型/线段/中枢/买卖点): {time.time()-t_extract:.3f}s")
    date_range = f"{kline_data[0]['date']} ~ {kline_data[-1]['date']}" if kline_data else ""
    result = {
        "meta": {
            "symbol": code,
            "name": stock_name,
            "freq": _get_freq_label(freq),
            "chan_version": "chan.py",
            "kline_count": len(kline_data),
            "bi_count": len(bi_data),
            "fx_count": len(fx_data),
            "zs_count": len(zs_data),
            "seg_count": len(seg_data),
            "bsp_count": len(bsp_data),
            "date_range": date_range,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_replay": bool(end_date),
            "market": "futures",
        },
        "klines": kline_data,
        "bis": bi_data,
        "fxs": fx_data,
        "zs": zs_data,
        "zs_stars": zs_stars,
        "segs": seg_data,
        "bsps": bsp_data,
        "white_hline": white_hline,
    }

    print(f"[信息] 期货查询 {code} 完成({_get_freq_label(freq)}): {len(kline_data)}K线, {len(bi_data)}笔, {len(fx_data)}分型, {len(zs_data)}中枢, {len(seg_data)}线段, {len(bsp_data)}买卖点")
    print(f"[耗时] 总耗时: {time.time()-t_start:.3f}s")
    return result


def _get_futures_name(code):
    """获取期货品种的中文名称"""
    FUTURES_NAMES = {
        "KQ.m@CFFEX.IF": "沪深300主连", "KQ.m@CFFEX.IH": "上证50主连",
        "KQ.m@CFFEX.IC": "中证500主连", "KQ.m@CFFEX.IM": "中证1000主连",
        "KQ.m@CFFEX.T":  "10年国债主连", "KQ.m@CFFEX.TF": "5年国债主连",
        "KQ.m@CFFEX.TL": "30年国债主连", "KQ.m@CFFEX.TS": "2年国债主连",
        "KQ.m@SHFE.rb":  "螺纹钢主连", "KQ.m@SHFE.au": "沪金主连",
        "KQ.m@SHFE.ag":  "沪银主连", "KQ.m@SHFE.cu": "沪铜主连",
        "KQ.m@SHFE.al":  "沪铝主连", "KQ.m@SHFE.zn": "沪锌主连",
        "KQ.m@SHFE.ni":  "沪镍主连", "KQ.m@SHFE.ru": "橡胶主连",
        "KQ.m@SHFE.bu":  "沥青主连", "KQ.m@SHFE.fu": "燃油主连",
        "KQ.m@SHFE.sp":  "纸浆主连", "KQ.m@SHFE.hc": "热卷主连",
        "KQ.m@SHFE.ss":  "不锈钢主连", "KQ.m@SHFE.sn": "沪锡主连",
        "KQ.m@SHFE.pb":  "沪铅主连", "KQ.m@SHFE.wr": "线材主连",
        "KQ.m@SHFE.ao":  "氧化铝主连", "KQ.m@SHFE.br": "丁二烯主连",
        "KQ.m@DCE.m":    "豆粕主连", "KQ.m@DCE.y": "豆油主连",
        "KQ.m@DCE.a":    "豆一主连", "KQ.m@DCE.b": "豆二主连",
        "KQ.m@DCE.p":    "棕榈油主连", "KQ.m@DCE.j": "焦炭主连",
        "KQ.m@DCE.jm":   "焦煤主连", "KQ.m@DCE.i": "铁矿石主连",
        "KQ.m@DCE.c":    "玉米主连", "KQ.m@DCE.cs": "淀粉主连",
        "KQ.m@DCE.l":    "塑料主连", "KQ.m@DCE.v": "PVC主连",
        "KQ.m@DCE.pp":   "PP主连", "KQ.m@DCE.eg": "乙二醇主连",
        "KQ.m@DCE.eb":   "苯乙烯主连", "KQ.m@DCE.pg": "LPG主连",
        "KQ.m@DCE.fb":   "纤维板主连", "KQ.m@DCE.bb": "胶合板主连",
        "KQ.m@DCE.rr":   "粳米主连", "KQ.m@DCE.lh": "生猪主连",
        "KQ.m@DCE.jd":   "鸡蛋主连", "KQ.m@CZCE.TA": "PTA主连",
        "KQ.m@CZCE.MA":  "甲醇主连", "KQ.m@CZCE.FG": "玻璃主连",
        "KQ.m@CZCE.SA":  "纯碱主连", "KQ.m@CZCE.SR": "白糖主连",
        "KQ.m@CZCE.CF":  "棉花主连", "KQ.m@CZCE.CY": "棉纱主连",
        "KQ.m@CZCE.OI":  "菜油主连", "KQ.m@CZCE.RM": "菜粕主连",
        "KQ.m@CZCE.ZC":  "动力煤主连", "KQ.m@CZCE.UR": "尿素主连",
        "KQ.m@CZCE.PF":  "短纤主连", "KQ.m@CZCE.PK": "花生主连",
        "KQ.m@CZCE.AP":  "苹果主连", "KQ.m@CZCE.CJ": "红枣主连",
        "KQ.m@CZCE.SM":  "锰硅主连", "KQ.m@CZCE.SF": "硅铁主连",
        "KQ.m@CZCE.SH":  "烧碱主连", "KQ.m@CZCE.PX": "对二甲苯主连",
        "KQ.m@CZCE.LR":  "晚籼稻主连", "KQ.m@CZCE.RI": "早籼稻主连",
        "KQ.m@CZCE.JR":  "粳稻主连", "KQ.m@CZCE.WH": "强麦主连",
        "KQ.m@CZCE.PM":  "普麦主连", "KQ.m@CZCE.RS": "菜籽主连",
        "KQ.m@INE.sc":   "原油主连", "KQ.m@INE.lu": "低硫燃油主连",
        "KQ.m@INE.nr":   "20号胶主连", "KQ.m@INE.bc": "国际铜主连",
        "KQ.m@INE.ec":   "集运指数主连", "KQ.m@GFEX.si": "工业硅主连",
        "KQ.m@GFEX.lc":  "碳酸锂主连", "KQ.m@GFEX.ps": "多晶硅主连",
    }
    if code in FUTURES_NAMES:
        return FUTURES_NAMES[code]
    # 尝试从具体合约代码中提取品种名
    for ex in ['CFFEX', 'SHFE', 'DCE', 'CZCE', 'INE', 'GFEX']:
        if code.startswith(ex + '.'):
            return code  # 返回原始代码作为名称
    return code


def _analyze_stock_internal(code, freq="d", end_date=None, start_time=None, cache_chan=True):
    """
    使用 chan.py 进行缠论分析（内部实现，不处理多进程）
    返回与 czsc 版本兼容的 JSON 数据结构
    end_date: 复盘截止日期，有值时以该日期为"最新行情"
    start_time: 选点起始时间，有值时只加载该时间之后的K线（不设数量限制）
    cache_chan: 是否缓存CChan对象。扫描模式设为False以节省内存。
    """
    import time
    t_start = time.time()

    market, code = parse_stock_code(code)
    if not market:
        return {"error": f"无法识别股票代码: {code}"}

    # ===== 期货/期指分支：走天勤数据源 =====
    if market == 'futures':
        return _analyze_futures_internal(code, freq=freq, end_date=end_date)

    # 查找数据文件
    if freq in ('30m', '5m'):
        # 30分钟线/5分钟线数据：从通达信5分钟线(.lc5)读取
        if market == 'hk':
            data_file = os.path.join(VIPDOC_DIR, "ds", "fzline", f"31#{code}.lc5")
        else:
            data_file = os.path.join(VIPDOC_DIR, market, "fzline", f"{market}{code}.lc5")
        if not os.path.exists(data_file):
            return {"error": f"找不到5分钟线数据文件: {data_file}"}
    else:
        day_file = find_day_file(market, code)
        if not os.path.exists(day_file):
            return {"error": f"找不到数据文件: {day_file}"}

    cache_key = f"{_CACHE_VERSION}_{market}_{code}_{freq}"

    # 冷启动（无end_date）：命中缓存直接返回
    # 但如果CSV中有保存的选点，且缓存的saved_selection_date与CSV不一致，
    # 说明缓存是从默认时间范围加载的（而非从选点时间），需要跳过缓存重新加载
    cached_result = _cache_get(cache_key)
    if not end_date and cached_result is not None and "result" in cached_result:
        result = cached_result["result"]
        col = FREQ_TO_COL.get(freq, "")
        if col and code in _saved_point_times:
            saved_sdt = _saved_point_times[code].get(col, "").strip() or None
            if saved_sdt:
                cached_saved = result.get("meta", {}).get("saved_selection_date", "")
                if cached_saved != saved_sdt:
                    # 缓存中的选点与CSV不一致，跳过缓存，从start_time重新加载
                    print(f"[stock][信息] 缓存选点({cached_saved})与CSV({saved_sdt})不一致，跳过缓存")
                else:
                    print(f"[stock][耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                    return result
            else:
                print(f"[stock][耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                return result
        else:
            print(f"[stock][耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
            return result

    # 复盘模式：不清空缓存，保留冷启动的 records 和 result

    # 1. 获取K线数据（优先从缓存读取全量数据，避免重复读文件）
    if cached_result is not None and "records" in cached_result:
        full_records = cached_result["records"]
        print(f"[stock][耗时] 从缓存获取K线: {len(full_records)}条")
    else:
        t0 = time.time()
        if freq == '30m':
            full_records = read_tdx_min_file(data_file, market=market, aggregate_30m=True)
        elif freq == '5m':
            full_records = read_tdx_min_file(data_file, market=market, aggregate_30m=False)
        else:
            full_records = read_tdx_day_file(day_file, market=market)
        # 周线：从日线合成
        if freq == 'w':
            full_records = resample_to_weekly(full_records)
        if len(full_records) < 5:
            return {"error": f"K线数据不足: 仅{len(full_records)}条"}
        print(f"[stock][耗时] 读取数据文件: {time.time()-t0:.3f}s, {len(full_records)}条K线")

    # 截断到指定日期（复盘模式：以end_date为"最新行情"）
    # 左边界与冷启动一致（同样按时间范围截取），只有右边界不同
    if end_date:
        target_dt = None
        matched_fmt = None
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
            try:
                target_dt = datetime.strptime(end_date, fmt)
                matched_fmt = fmt
                break
            except ValueError:
                continue
        if target_dt is None:
            return {"error": f"无法解析日期: {end_date}"}
        if matched_fmt == "%Y-%m-%d" and freq in INTRADAY_FREQS:
            target_dt = target_dt.replace(hour=23, minute=59, second=59)

        # 如果选择的日期不早于最新行情，视为正常冷启动而非复盘
        if full_records and target_dt >= full_records[-1]["dt"]:
            print(f"[stock][信息] 选择的日期({end_date})不早于最新行情，按冷启动处理")
            end_date = None

    if end_date:
        # 复盘模式：左右边界截断
        from datetime import timedelta
        if freq == 'w':
            cutoff = target_dt - timedelta(days=365 * 8)
        elif freq == 'd':
            cutoff = target_dt - timedelta(days=365 * 3)
        elif freq == '30m':
            cutoff = target_dt - timedelta(days=90)
        elif freq == '5m':
            cutoff = target_dt - timedelta(days=21)
        else:
            cutoff = None

        before_count = len(full_records)
        if cutoff is not None:
            records = [r for r in full_records if cutoff <= r["dt"] <= target_dt]
        else:
            records = [r for r in full_records if r["dt"] <= target_dt]
        if len(records) < 5:
            return {"error": f"截断后K线数据不足: 仅{len(records)}条，请选择更晚的日期"}
        print(f"[stock][信息] 复盘范围(freq={freq}) {cutoff.strftime('%Y-%m-%d')} ~ {end_date}, "
              f"全量{before_count}条 -> {len(records)}条")
    else:
        records = full_records
        # 确定起始时间：优先使用传入的start_time，其次使用CSV保存的选点
        if start_time is None:
            col = FREQ_TO_COL.get(freq, "")
            if col and code in _saved_point_times:
                _saved = _saved_point_times[code].get(col, "").strip() or None
                if _saved:
                    start_time = _saved

        if start_time is not None:
            # 从选点时间开始过滤，不做数量限制
            from datetime import timedelta
            start_dt = None
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                try:
                    start_dt = datetime.strptime(start_time, fmt)
                    break
                except ValueError:
                    continue
            if start_dt is not None:
                records = [r for r in records if r["dt"] >= start_dt]
                print(f"[stock][信息] 从选点时间 {start_time} 开始，筛选后 {len(records)} 条K线")
        else:
            # 冷启动无选点时：按时间范围截取数据
            # 日线：最近3年；周K：最近8年；30分：最近3个月；5分：最近1个月
            if len(records) > 0:
                from datetime import timedelta
                latest_dt = records[-1]["dt"]
                if freq == 'w':
                    cutoff = latest_dt - timedelta(days=365 * 8)
                elif freq == 'd':
                    cutoff = latest_dt - timedelta(days=365 * 3)
                elif freq == '30m':
                    cutoff = latest_dt - timedelta(days=90)
                elif freq == '5m':
                    cutoff = latest_dt - timedelta(days=21)
                else:
                    cutoff = None
                if cutoff is not None:
                    before_count = len(records)
                    records = [r for r in records if r["dt"] >= cutoff]
                    if before_count != len(records):
                        print(f"[stock][信息] 按时间范围截取(freq={freq}): 从{latest_dt.strftime('%Y-%m-%d')}往前推, "
                              f"{before_count}条 -> {len(records)}条")

    # 2. 获取股票名称
    t0 = time.time()
    stock_name = get_stock_name(market, code)
    if not stock_name:
        stock_name = f"{code}.{market.upper()}"


    # 3. 使用 chan.py 进行缠论分析
    import gc
    # 复盘时：清空缓存恢复原始状态，再重新加载（与选点逻辑一致）
    if end_date and cache_key in _analysis_cache:
        del _analysis_cache[cache_key]
        gc.collect()

    t0 = time.time()
    with _stock_analysis_lock:
        CTdxAPI.set_data(records)
        chan_code = f"{market}.{code}"
        config = _make_chan_config()

        try:
            try:
                from Math.Demark import CDemarkEngine
                CDemarkEngine.DEMARK_LEN = 9
                CDemarkEngine.SETUP_BIAS = 4
                CDemarkEngine.COUNTDOWN_BIAS = 2
                CDemarkEngine.MAX_COUNTDOWN = 13
                CDemarkEngine.TIAOKONG_ST = True
                CDemarkEngine.SETUP_CMP2CLOSE = True
                CDemarkEngine.COUNTDOWN_CMP2CLOSE = True
            except Exception:
                pass
            chan = CChan(
                code=chan_code,
                begin_time=None,
                end_time=None,
                data_src="custom:TdxAPI.CTdxAPI",
                lv_list=[_get_kl_type(freq)],
                config=config,
                autype=AUTYPE.NONE,
            )
            # trigger_step=True 时 CChan.__init__ 不会自动加载；必须逐步消费完整数据流。
            for _snapshot in chan.step_load():
                pass
        except Exception as e:
            return {"error": f"chan.py 分析失败: {e}"}

    print(f"[stock][耗时] chan.py 缠论分析: {time.time()-t0:.3f}s")

    # 4. 提取结果
    t0 = time.time()
    kl_list = chan[_get_kl_type(freq)]

    # 4.1 K线数据（含MACD）
    closes = [r["close"] for r in records]
    macd_list = calculate_macd(closes)
    date_fmt = "%Y-%m-%d %H:%M" if freq in INTRADAY_FREQS else "%Y-%m-%d"
    kline_data = []
    for i, row in enumerate(records):
        macd = macd_list[i] if i < len(macd_list) else {"dif": 0, "dea": 0, "macd": 0}
        kline_data.append({
            "date": row["dt"].strftime(date_fmt),
            "timestamp": int(row["dt"].timestamp()) * 1000,
            "open": row["open"], "high": row["high"],
            "low": row["low"], "close": row["close"],
            "vol": row["vol"], "amount": row["amount"],
            "dif": round(macd["dif"], 4),
            "dea": round(macd["dea"], 4),
            "macd": round(macd["macd"], 4),
        })

    # 4.2 笔数据
    bi_data = []
    _fx_empty_count = 0  # 统计 fx_a_raw_dt / fx_b_raw_dt 为空的笔数
    for bi in kl_list.bi_list:
        direction = "up" if bi.is_up() else "down"
        begin_val = bi.get_begin_val()
        end_val = bi.get_end_val()
        power = abs(end_val - begin_val)
        # 获取起止时间
        begin_klu = bi.get_begin_klu()
        end_klu = bi.get_end_klu()
        sdt = begin_klu.time.to_str() if begin_klu else ""
        edt = end_klu.time.to_str() if end_klu else ""
        # 转换时间格式（chan.py to_str() 返回 "YYYY/MM/DD HH:MM"）
        try:
            sdt_dt = datetime.strptime(sdt, "%Y/%m/%d %H:%M")
            sdt_str = sdt_dt.strftime(date_fmt)
            sdt_ts = int(sdt_dt.timestamp()) * 1000
        except:
            sdt_str = sdt[:16].replace("/", "-") if freq in INTRADAY_FREQS else sdt[:10].replace("/", "-")
            sdt_ts = 0
        try:
            edt_dt = datetime.strptime(edt, "%Y/%m/%d %H:%M")
            edt_str = edt_dt.strftime(date_fmt)
            edt_ts = int(edt_dt.timestamp()) * 1000
        except:
            edt_str = edt[:16].replace("/", "-") if freq in INTRADAY_FREQS else edt[:10].replace("/", "-")
            edt_ts = 0
        # 获取起始分型K线索引（在kl_list.lst中定位，用于左肩反查）
        begin_fx_idx = None
        if hasattr(bi, 'begin_klc') and bi.begin_klc:
            for idx, klc in enumerate(kl_list.lst):
                if klc is bi.begin_klc:
                    begin_fx_idx = idx
                    break
        # 获取结束分型K线索引（用于实笔的左肩定位）
        # 实笔的左肩 = 结束分型左边第一根K线（包含关系处理后的）
        end_fx_idx = None
        if hasattr(bi, 'end_klc') and bi.end_klc:
            # 找到end_klc在kl_list.lst中的索引
            for idx, klc in enumerate(kl_list.lst):
                if klc is bi.end_klc:
                    end_fx_idx = idx
                    break
        # 获取分型肩部原始K线时间（双窗口模式：高亮整笔的外沿区间）
        fx_a_raw_dt = ""
        fx_b_raw_dt = ""
        try:
            begin_klc = bi.begin_klc
            end_klc = bi.end_klc
            # A: begin分型左肩第一根原始K线时间
            # 在 kl_list.lst 中定位 begin 分型，取前一个 KLC 作为左肩（而非 begin_klc.pre 指针）
            if begin_fx_idx is not None and begin_fx_idx > 0:
                left_klc = kl_list.lst[begin_fx_idx - 1]
            else:
                left_klc = None
            a_klu = left_klc.lst[0] if (left_klc and left_klc.lst) else (begin_klc.lst[0] if begin_klc.lst else None)
            if a_klu:
                a_t = a_klu.time.to_str()
                try:
                    fx_a_raw_dt = datetime.strptime(a_t, "%Y/%m/%d %H:%M").strftime(date_fmt)
                except:
                    fx_a_raw_dt = a_t[:16].replace("/", "-") if freq in INTRADAY_FREQS else a_t[:10].replace("/", "-")
            # B: end分型右肩最后一根原始K线时间
            # 注意：使用 end_fx_idx（在 kl_list.lst 中的实际位置），而非 end_klc.idx（内部索引可能不同）
            if end_fx_idx is not None:
                right_klc = kl_list.lst[end_fx_idx + 1] if end_fx_idx + 1 < len(kl_list.lst) else None
            else:
                right_klc = None
            b_klu = right_klc.lst[-1] if (right_klc and right_klc.lst) else (end_klc.lst[-1] if end_klc.lst else None)
            if b_klu:
                b_t = b_klu.time.to_str()
                try:
                    fx_b_raw_dt = datetime.strptime(b_t, "%Y/%m/%d %H:%M").strftime(date_fmt)
                except:
                    fx_b_raw_dt = b_t[:16].replace("/", "-") if freq in INTRADAY_FREQS else b_t[:10].replace("/", "-")
        except Exception as e:
            print(f"[stock][调试] 获取分型肩部原始K线时间失败: {e}")

        # 统计空值
        if not fx_a_raw_dt or not fx_b_raw_dt:
            _fx_empty_count += 1

        bi_data.append({
            "sdt": sdt_str, "edt": edt_str,
            "sdt_ts": sdt_ts, "edt_ts": edt_ts,
            "direction": direction,
            "fx_a_price": round(begin_val, 2),
            "fx_b_price": round(end_val, 2),
            "high": round(bi._high(), 2),
            "low": round(bi._low(), 2),
            "power": round(power, 2),
            "is_sure": getattr(bi, 'is_sure', True),
            "end_fx_idx": end_fx_idx,
            "begin_fx_idx": begin_fx_idx,
            "fx_a_raw_dt": fx_a_raw_dt,
            "fx_b_raw_dt": fx_b_raw_dt,
        })

    # === 笔循环结束后，打印空值统计 ===
    if _fx_empty_count > 0:
        print(f"[stock][调试] 笔 fx_a/fx_b 空值总数: {_fx_empty_count}/{len(bi_data)}")

    # 4.3 分型数据（从合并K线中提取）
    fx_data = []
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            # 顶分型：取合并K线中最高点对应的klu时间
            mark = "G"
            price = klc.high
            klu = klc.get_high_peak_klu()
            fx_date = klu.time.to_str()[:16].replace("/", "-") if freq in INTRADAY_FREQS else klu.time.to_str()[:10].replace("/", "-")
            try:
                fx_dt = datetime.strptime(fx_date, date_fmt)
                fx_ts = int(fx_dt.timestamp()) * 1000
            except:
                fx_ts = 0
            fx_data.append({
                "date": fx_date, "timestamp": fx_ts,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            # 底分型：取合并K线中最低点对应的klu时间
            mark = "D"
            price = klc.low
            klu = klc.get_low_peak_klu()
            fx_date = klu.time.to_str()[:16].replace("/", "-") if freq in INTRADAY_FREQS else klu.time.to_str()[:10].replace("/", "-")
            try:
                fx_dt = datetime.strptime(fx_date, date_fmt)
                fx_ts = int(fx_dt.timestamp()) * 1000
            except:
                fx_ts = 0
            fx_data.append({
                "date": fx_date, "timestamp": fx_ts,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })

    # 4.4 中枢数据（直接从CChan实例获取，选点已在加载时通过start_time处理）
    zs_data = []
    for zs in kl_list.zs_list:
        zs_data.append({
            "sdt": _format_klu_time(zs.begin, date_fmt),
            "edt": _format_klu_time(zs.end, date_fmt),
            "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, date_fmt),
            "zg": round(zs.high, 2),
            "zd": round(zs.low, 2),
            "gg": round(zs.peak_high, 2),
            "dd": round(zs.peak_low, 2),
            "dir": "up" if zs.bi_in and zs.bi_in.is_up() else "down",
        })

    zs_stars = []
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        try:
            sdt_raw = begin_klu.time.to_str()
            sdt_dt = datetime.strptime(sdt_raw, "%Y/%m/%d %H:%M")
            star_date = sdt_dt.strftime(date_fmt)
        except:
            star_date = sdt_raw[:16].replace("/", "-") if freq in INTRADAY_FREQS else sdt_raw[:10].replace("/", "-")
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "D",
                "color": "red",
            })
        else:
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "G",
                "color": "green",
            })

    # 4.5 买卖点数据
    bsp_data = []
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            bsp_date = bsp.klu.time.to_str()[:16].replace("/", "-") if freq in INTRADAY_FREQS else bsp.klu.time.to_str()[:10].replace("/", "-")
            try:
                bsp_dt = datetime.strptime(bsp_date, date_fmt)
                bsp_ts = int(bsp_dt.timestamp()) * 1000
            except:
                bsp_ts = 0
            bsp_data.append({
                "date": bsp_date, "timestamp": bsp_ts,
                "type": bsp.type2str(),
                "is_buy": bsp.is_buy,
                "price": bsp.klu.close,
                "high": bsp.klu.high,
                "low": bsp.klu.low,
            })
    except Exception as e:
        print(f"[stock][调试] 获取买卖点失败: {e}")

    # 4.6 线段数据
    seg_data = []
    for seg in kl_list.seg_list:
        direction = "up" if seg.is_up() else "down"
        begin_klu = seg.get_begin_klu()
        end_klu = seg.get_end_klu()
        sdt = (begin_klu.time.to_str()[:16].replace("/", "-") if freq in INTRADAY_FREQS else begin_klu.time.to_str()[:10].replace("/", "-")) if begin_klu else ""
        edt = (end_klu.time.to_str()[:16].replace("/", "-") if freq in INTRADAY_FREQS else end_klu.time.to_str()[:10].replace("/", "-")) if end_klu else ""
        # 线段的起点/终点价格 = 对应K线的最高/最低价
        # 向上线段：起点连最低价，终点连最高价
        # 向下线段：起点连最高价，终点连最低价
        begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
        end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
        if direction == "down":
            begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
            end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
        seg_data.append({
            "sdt": sdt, "edt": edt,
            "direction": direction,
            "begin_price": begin_price,
            "end_price": end_price,
            "high": round(seg._high(), 2),
            "low": round(seg._low(), 2),
            "amp": round(seg.amp(), 2),
        })

    print(f"[stock][耗时] 分析结果转JSON(K线/分型/笔/线段/中枢/买卖点）：{time.time()-t0:.3f}s")

    # 获取当前周期的保存选点日期（复盘模式下不注入，防止前端误判为有选点）
    _col_meta = FREQ_TO_COL.get(freq, "")
    _saved_sdt_for_meta = ""
    if not end_date and _col_meta and code in _saved_point_times:
        _saved_sdt_for_meta = _saved_point_times[code].get(_col_meta, "").strip() or ""

    # 5. 计算最新笔的白色横虚线数据
    white_hline = None
    if bi_data:
        latest_bi = bi_data[-1]
        # 判断是否为虚笔：is_sure=False 视为虚笔
        is_virtual = not latest_bi.get("is_sure", True)
        direction = latest_bi.get("direction", "")
        if is_virtual:
            # 虚笔：找到端点左边第一根K线（包含关系处理后的）
            # 笔的结束K线索引：通过edt在K线数据中查找
            search_edt = latest_bi["edt"]
            end_klu_idx = None
            for ki, k in enumerate(kline_data):
                if k["date"] == search_edt:
                    end_klu_idx = ki
                    break
            if end_klu_idx is not None and end_klu_idx > 0:
                left_kline = kline_data[end_klu_idx - 1]
                if direction == "down":
                    # 向下虚笔：以左边第一根K线的最高价画线
                    white_hline = {
                        "price": left_kline["high"],
                        "start_date": left_kline["date"],
                    }
                elif direction == "up":
                    # 向上虚笔：以左边第一根K线的最低价画线
                    white_hline = {
                        "price": left_kline["low"],
                        "start_date": left_kline["date"],
                    }
        else:
            # 实笔：找到结束分型的左肩
            end_fx_idx = latest_bi.get("end_fx_idx")
            if end_fx_idx is not None and end_fx_idx > 0:
                # 左肩 = 结束分型左边第一根合并K线
                left_klc = kl_list.lst[end_fx_idx - 1]
                # 获取左肩K线对应的所有原始K线，取其中最高/最低价
                if hasattr(left_klc, 'lst') and left_klc.lst:
                    high = max(klu.high for klu in left_klc.lst)
                    low = min(klu.low for klu in left_klc.lst)
                else:
                    high = left_klc.high
                    low = left_klc.low
                # 获取左肩K线的时间（用于前端定位起始X坐标）
                if hasattr(left_klc, 'get_high_peak_klu') and left_klc.get_high_peak_klu():
                    ls_time = left_klc.get_high_peak_klu().time.to_str()
                elif hasattr(left_klc, 'get_low_peak_klu') and left_klc.get_low_peak_klu():
                    ls_time = left_klc.get_low_peak_klu().time.to_str()
                else:
                    ls_time = ""
                try:
                    ls_dt = datetime.strptime(ls_time, "%Y/%m/%d %H:%M")
                    ls_date = ls_dt.strftime(date_fmt)
                except:
                    ls_date = ls_time[:16].replace("/", "-") if freq in INTRADAY_FREQS else ls_time[:10].replace("/", "-")
                if direction == "down":
                    # 向下实笔：以底分型左肩的最高价画线
                    white_hline = {
                        "price": round(high, 2),
                        "start_date": ls_date,
                    }
                elif direction == "up":
                    # 向上实笔：以顶分型左肩的最低价画线
                    white_hline = {
                        "price": round(low, 2),
                        "start_date": ls_date,
                    }

    # 6. 组装结果
    date_range = f"{kline_data[0]['date']} ~ {kline_data[-1]['date']}" if kline_data else ""
    result = {
        "meta": {
            "symbol": f"{code}.{market.upper()}",
            "name": stock_name,
            "freq": _get_freq_label(freq),
            "chan_version": "chan.py",
            "kline_count": len(kline_data),
            "bi_count": len(bi_data),
            "fx_count": len(fx_data),
            "zs_count": len(zs_data),
            "seg_count": len(seg_data),
            "bsp_count": len(bsp_data),
            "date_range": date_range,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "saved_selection_date": _saved_sdt_for_meta,
            "is_replay": bool(end_date),
        },
        "klines": kline_data,
        "bis": bi_data,
        "fxs": fx_data,
        "zs": zs_data,
        "zs_stars": zs_stars,
        "segs": seg_data,
        "bsps": bsp_data,
        "white_hline": white_hline,
    }

    mode_str = f" [复盘到 {end_date}]" if end_date else ""
    print(f"[stock][信息] 查询 {code}.{market.upper()} 完成{mode_str}: {len(kline_data)}条K线, {len(bi_data)}笔, {len(fx_data)}分型, {len(zs_data)}中枢, {len(seg_data)}线段, {len(bsp_data)}买卖点")
    print(f"[stock][耗时] 总耗时: {time.time()-t_start:.3f}s")

    # 缓存策略：
    # - 冷启动/选点/复盘：统一缓存 records + result + chan
    # - 扫描模式(cache_chan=False)：只缓存 result，不缓存 chan 和 records
    # 复盘 = 在复盘日期那天冷启动，复原历史当天的情况
    cached = _cache_get(cache_key)
    if cached is None:
        cached = {}
    if cache_chan:
        cached["records"] = full_records
        cached["chan"] = chan
    cached["result"] = result
    _cache_put(cache_key, cached)

    # 复盘后触发GC，回收旧的CChan对象，避免内存累积导致下次分析变慢
    if end_date:
        t_gc = time.time()
        gc.collect()
        print(f"[stock][耗时]   复盘后GC回收: {time.time()-t_gc:.3f}s")

    return result


def _build_zs_from_bis(bis, all_bi_list, date_fmt):
    """
    从笔列表中构建中枢，完整模拟 CZSList 的 over_seg 算法（不含合并）。

    流程（对应 ZSList.py）：
      cal_bi_zs(over_seg) → 逐笔调用 update_overseg_zs
        → update_overseg_zs: 处理延伸/跳过 → add_to_free_lst
          → add_to_free_lst: 加入 free_lst → try_construct_zs(over_seg)
            → 取最后3笔 → 跳过进入段 → 检查三笔重叠 → 形成中枢 → 清空 free_lst

    参数:
        bis: 从 start_bi 到 end_bi 的笔列表（含两端）
        all_bi_list: 完整的笔列表（用于查找进入段）
        date_fmt: 日期格式字符串

    返回:
        zs_data: 中枢数据列表
        zs_stars: 五角星数据列表
    """
    zs_data = []
    zs_stars = []
    if len(bis) < 4:  # over_seg 至少需要 1进入段 + 3构成中枢
        return zs_data, zs_stars

    def _fmt(klu):
        if klu is None:
            return ""
        raw = klu.time.to_str()
        try:
            return datetime.strptime(raw, "%Y/%m/%d %H:%M").strftime(date_fmt)
        except:
            return raw[:16].replace("/", "-") if len(date_fmt) > 10 else raw[:10].replace("/", "-")

    def _in_zs_range(bi, zg, zd):
        """笔是否与中枢区间 [zd, zg] 有重叠（模拟 CZS.in_range）"""
        return min(zg, bi._high()) >= max(zd, bi._low())

    free_lst = []       # 模拟 CZSList.free_item_lst
    zs_records = []     # 模拟 CZSList.zs_lst（简化版）

    for bi in bis:
        # ===== update_overseg_zs 逻辑 =====
        if len(zs_records) and len(free_lst) == 0:
            last_zs = zs_records[-1]
            # 检查1: try_add_to_end — bi 本身在 ZS 范围内，且 bi.next 也在范围内，则延伸中枢
            # 对应源码: bi.idx - zs.end_bi.idx <= 1 and zs.in_range(bi.next) and zs.try_add_to_end(bi)
            # try_add_to_end 内部先检查 in_range(bi)，所以这里也要检查 bi 在范围内
            if bi.next is not None:
                if (bi.idx - last_zs['end_bi'].idx <= 1
                        and _in_zs_range(bi.next, last_zs['zg'], last_zs['zd'])
                        and _in_zs_range(bi, last_zs['zg'], last_zs['zd'])):
                    # try_add_to_end 成功 → 延伸中枢（update_zs_end）
                    last_zs['edt'] = _fmt(bi.get_end_klu())
                    last_zs['gg'] = round(max(last_zs['gg'], bi._high()), 2)
                    last_zs['dd'] = round(min(last_zs['dd'], bi._low()), 2)
                    last_zs['end_bi'] = bi
                    zs_data[-1] = {k: v for k, v in last_zs.items() if k != 'end_bi'}
                    continue
            # 检查2: bi 本身在 ZS 范围内且相邻 → 跳过（不加入 free_lst）
            if _in_zs_range(bi, last_zs['zg'], last_zs['zd']) and bi.idx - last_zs['end_bi'].idx <= 1:
                continue
            # 否则：bi 突破中枢，加入 free_lst（继续往下走）
            # 如果 bi 不跟最后一个中枢重叠，且 bi 不是最后一笔（有后续笔确认），设置 confirm_edt
            if not _in_zs_range(bi, last_zs['zg'], last_zs['zd']) and not last_zs.get('confirm_edt'):
                if bi.idx != bis[-1].idx:
                    last_zs['confirm_edt'] = _fmt(bi.get_end_klu())
                    zs_data[-1]['confirm_edt'] = last_zs['confirm_edt']

        # ===== add_to_free_lst 逻辑 =====
        if len(free_lst) != 0 and bi.idx == free_lst[-1].idx:
            free_lst = free_lst[:-1]  # 笔更新，替换最后一项
        free_lst.append(bi)

        # ===== try_construct_zs(over_seg) 逻辑 =====
        if len(free_lst) < 3:
            continue

        lst = list(free_lst[-3:])  # 取最后3笔

        # --- 处理进入段 ---
        if len(zs_records) > 0:
            # 有前序中枢：检查突破方向
            zs = zs_records[-1]
            lst0_low = lst[0]._low()
            lst0_high = lst[0]._high()
            if lst0_low > zs['zg']:
                # 向上突破 → 跳过向上笔（进入段）
                if lst[0].is_up():
                    continue  # return None，笔留在 free_lst 中等下一笔
            elif lst0_high < zs['zd']:
                # 向下突破 → 跳过向下笔（进入段）
                if lst[0].is_down():
                    continue
            # else: lst[0] 在 ZS 内部 → 不应出现（update_overseg_zs 已过滤）
        else:
            # 无前序中枢：free_lst[0] 是进入段
            first_pen = free_lst[0]
            if len(free_lst) == 3:
                # 只有3笔，跳过第一笔（进入段），等第4笔
                continue
            else:
                # 超过3笔，跳过与进入段同向的笔
                if lst[0].dir == first_pen.dir:
                    continue
                # lst[0] 与 first_pen 反向，可以构建

        # 现在 lst 应该包含3笔且第1笔与进入段反向
        if len(lst) < 3:
            continue

        b1, b2, b3 = lst[0], lst[1], lst[2]

        if not getattr(b3, 'is_sure', True):
            continue

        # --- 检查三笔重叠 ---
        min_high = min(b1._high(), b2._high(), b3._high())
        max_low = max(b1._low(), b2._low(), b3._low())
        if min_high <= max_low:
            continue  # 无重叠，继续积累

        # --- 形成中枢 ---
        zg = min_high
        zd = max_low
        gg = max(b1._high(), b2._high(), b3._high())
        dd = min(b1._low(), b2._low(), b3._low())

        # 查找进入段笔（中枢第一笔的前一笔）
        entry_bi = None
        entry_dir = "up"
        for j, bi_ref in enumerate(all_bi_list):
            if bi_ref is b1 and j > 0:
                entry_bi = all_bi_list[j - 1]
                break
        if entry_bi is not None:
            entry_dir = "up" if entry_bi.is_up() else "down"

        sdt = _fmt(b1.get_begin_klu())
        edt = _fmt(b3.get_end_klu())

        zs_rec = {
            'sdt': sdt, 'edt': edt, 'confirm_edt': '',
            'zg': round(zg, 2), 'zd': round(zd, 2),
            'gg': round(gg, 2), 'dd': round(dd, 2),
            'dir': entry_dir,
            'end_bi': b3,
        }
        zs_records.append(zs_rec)
        zs_data.append({k: v for k, v in zs_rec.items() if k != 'end_bi'})

        # 添加五角星（进入段起点分型）
        if entry_bi is not None:
            entry_fx_klu = entry_bi.get_begin_klu()
            if entry_fx_klu is not None:
                try:
                    fx_date = _fmt(entry_fx_klu)
                except:
                    fx_date = ""
                star_price = entry_bi.get_begin_val()
                if entry_bi.is_up():
                    zs_stars.append({"date": fx_date, "price": round(star_price, 2), "mark": "D", "color": "red"})
                else:
                    zs_stars.append({"date": fx_date, "price": round(star_price, 2), "mark": "G", "color": "green"})

        # --- 清空 free_lst（不合并） ---
        free_lst = []

    return zs_data, zs_stars


def compute_dual_zs(code, freq='d', start_bi=0, end_bi=0):
    """
    双窗口新模式：从缓存中取出已计算的笔列表，截取 [start_bi, end_bi] 范围内的笔，
    用 _build_zs_from_bis 重新计算中枢，返回给前端绘制。

    与 _build_zs_from_bis 不同的是：
    - 不依赖完整的 chan.py 分析流程，直接从已缓存的 bi_list 中切片
    - 仅返回中枢数据（zs + zs_stars），不返回完整 chartData
    """
    import re
    normalized_code = code.strip().upper()
    market = None
    prefix_match = re.match(r'^(SH|SZ|HK)(\d+)$', normalized_code)
    suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK)$', normalized_code)
    if prefix_match:
        market = prefix_match.group(1).lower()
        normalized_code = prefix_match.group(2)
    elif suffix_match:
        normalized_code = suffix_match.group(1)
        market = suffix_match.group(2).lower()

    cache_key = f"{_CACHE_VERSION}_{market}_{normalized_code}_{freq}"
    cached = _cache_get(cache_key)
    if cached is None:
        return {"error": "请先在该周期下加载K线数据"}
    if "chan" not in cached:
        # 扫描缓存只有result没有chan，重新分析以获取完整数据
        print(f"[stock][信息] 缓存中无chan对象，重新分析 {normalized_code} {freq}")
        analyze_stock(normalized_code, freq=freq, cache_chan=True)
        cached = _cache_get(cache_key)
        if cached is None or "chan" not in cached:
            return {"error": "缓存中无分析数据，请重新查询"}

    chan = cached["chan"]
    kl_list = chan[_get_kl_type(freq)]
    bi_list = kl_list.bi_list

    if start_bi >= len(bi_list) or end_bi >= len(bi_list):
        return {"error": f"笔索引越界: start_bi={start_bi}, end_bi={end_bi}, 总笔数={len(bi_list)}"}

    # 截取指定范围的笔（含 start_bi, end_bi）
    sliced_bis = bi_list[start_bi:end_bi + 1]

    if freq in INTRADAY_FREQS:
        date_fmt = "%Y-%m-%d %H:%M"  # 与kline date格式一致，确保前端dateToGlobalIdx能匹配
    else:
        date_fmt = "%Y-%m-%d"

    zs_data, zs_stars = _build_zs_from_bis(sliced_bis, bi_list, date_fmt)
    return {"zs": zs_data, "zs_stars": zs_stars}


def manual_select_zs(code, freq='d', bi_idx=-1):
    """
    手选进入段：找到左肩原始K线时间T，销毁旧CChan实例及所有中间状态，
    从T重新加载K线数据并创建全新CChan实例，返回完整chartData给前端渲染。

    流程：
    1. 通过前端传来的笔索引，找到分型左肩第一根原始K线时间T
    2. 保存T到CSV
    3. 销毁旧CChanA及_analysis_cache中的全部中间状态，回收内存
    4. 从T开始重新读取通达信K线，创建CChanB，返回完整结果
    """
    # 标准化代码
    normalized_code = code.strip().upper()
    market = None
    prefix_match = re.match(r'^(SH|SZ|HK)(\d+)$', normalized_code)
    suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK)$', normalized_code)
    if prefix_match:
        market = prefix_match.group(1).lower()
        normalized_code = prefix_match.group(2)
    elif suffix_match:
        normalized_code = suffix_match.group(1)
        market = suffix_match.group(2).lower()
    cache_key = f"{_CACHE_VERSION}_{market}_{normalized_code}_{freq}"
    cached = _cache_get(cache_key)
    if cached is None:
        return {"error": "请先查询该股票"}

    if "chan" not in cached:
        # 扫描缓存只有result没有chan，重新分析以获取完整数据
        print(f"[stock][信息] 缓存中无chan对象，重新分析 {normalized_code} {freq}")
        analyze_stock(normalized_code, freq=freq, cache_chan=True)
        cached = _cache_get(cache_key)
        if cached is None or "chan" not in cached:
            return {"error": "缓存中无分析数据，请重新查询"}

    chan = cached["chan"]
    kl_list = chan[_get_kl_type(freq)]
    bi_list = kl_list.bi_list

    target_bi_idx = int(bi_idx)
    if target_bi_idx < 0 or target_bi_idx >= len(bi_list):
        return {"error": f"笔索引 {bi_idx} 越界，笔总数 {len(bi_list)}"}

    # 检查：选点之后至少需要4笔才能构建中枢（三笔重叠+确认判断）
    remaining_bis = len(bi_list) - target_bi_idx - 1
    if remaining_bis < 4:
        return {"error": f"选点之后仅剩 {remaining_bis} 笔，至少需要4笔才能构建中枢，请重新选点"}

    # Step 1: 找到左肩原始K线时间T
    start_time = _find_left_shoulder_time(kl_list, bi_list, target_bi_idx, freq)
    if start_time is None:
        return {"error": "无法定位左肩K线时间，请重试"}

    # Step 2: 保存选点到CSV（保存的是左肩第一根原始K线的时间T）
    stock_name = cached.get("result", {}).get("meta", {}).get("name", "")
    _save_point_time(normalized_code, stock_name, freq, start_time)
    if normalized_code not in _saved_point_times:
        _saved_point_times[normalized_code] = {}
    _saved_point_times[normalized_code]["name"] = stock_name
    _saved_point_times[normalized_code][FREQ_TO_COL.get(freq, "")] = start_time

    # Step 3: 销毁旧CChanA及所有中间状态，回到冷启动前的干净状态
    import gc
    if cache_key in _analysis_cache:
        del _analysis_cache[cache_key]
    gc.collect()

    # Step 4: 从T开始重新加载K线，创建CChanB，返回完整chartData
    result = _analyze_stock_internal(normalized_code, freq=freq, start_time=start_time)
    return result


def futures_manual_select_zs(symbol, freq="15s", bi_idx="0"):
    """
    期货期指手选进入段：与股票 manual_select_zs 逻辑一致。
    创建临时 TqApi → 拉取全量历史 → 找到左肩时间T → 保存CSV →
    创建新 TqApi → 从T重新拉取 → 创建新CChan → 返回完整快照。
    """
    import time
    from tqsdk import TqApi, TqAuth
    from DataAPI.TqSdkAPI import (
        FREQ_SEC_MAP, FREQ_LABEL_CN, CTqSdkAPI,
        fetch_futures_kline, _extract_realtime_snapshot,
        TQ_ACCOUNT, TQ_PASSWORD,
    )

    freq_sec = FREQ_SEC_MAP.get(freq, 15)
    freq_label = freq
    freq_cn = FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{freq_cn}"
    target_bi_idx = int(bi_idx)

    api = None
    api2 = None
    try:
        t_conn = time.time()
        api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
        print(f"[{display_key}] ⓪ 临时连接天勤(选点): 耗时 {time.time()-t_conn:.1f}s")

        records = fetch_futures_kline(api, symbol, freq_sec=freq_sec, display_key=display_key)
        if len(records) < 5:
            return {"error": f"K线数据不足: 仅{len(records)}条"}

        # 注入数据源 + 创建 CChan
        CTqSdkAPI.set_data(records, symbol=f"{symbol}:{freq_sec}")

        from Common.CEnum import KL_TYPE, AUTYPE
        from Chan import CChan
        from ChanConfig import CChanConfig

        _freq_to_kl = {
            15: KL_TYPE.K_15S, 30: KL_TYPE.K_30S, 60: KL_TYPE.K_1M,
            300: KL_TYPE.K_5M, 900: KL_TYPE.K_15M, 1800: KL_TYPE.K_30M,
            3600: KL_TYPE.K_60M, 86400: KL_TYPE.K_DAY,
            604800: KL_TYPE.K_WEEK, 2592000: KL_TYPE.K_MON,
        }
        kl_type = _freq_to_kl.get(freq_sec, KL_TYPE.K_15S)

        config = CChanConfig({
            "trigger_step": True, "bi_fx_check": "loss", "bi_allow_sub_peak": True,
            "bi_algo": "normal", "bi_strict": True, "bi_end_is_peak": False,
            "seg_algo": "chan", "zs_algo": "over_seg", "zs_combine": False,
            "bs_type": "1,1p,2,2s,3a,3b", "min_zs_cnt": 1, "bs1_peak": True,
            "divergence_rate": 0.9, "bsp2_follow_1": True, "max_bs2_rate": 0.9,
            "bsp2s_follow_2": False, "max_bsp2s_lv": None, "bsp3_follow_1": False,
            "strict_bsp3": False, "bsp3_peak": False, "bsp3a_max_zs_cnt": 2,
            "macd_algo": "full_area",
        })

        try:
            from Math.Demark import CDemarkEngine
            CDemarkEngine.DEMARK_LEN = 9
            CDemarkEngine.SETUP_BIAS = 4
            CDemarkEngine.COUNTDOWN_BIAS = 2
            CDemarkEngine.MAX_COUNTDOWN = 13
            CDemarkEngine.TIAOKONG_ST = True
            CDemarkEngine.SETUP_CMP2CLOSE = True
            CDemarkEngine.COUNTDOWN_CMP2CLOSE = True
        except Exception:
            pass

        chan = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
        )
        for _snapshot in chan.step_load():
            pass

        kl_list = chan[kl_type]
        bi_list = kl_list.bi_list

        if target_bi_idx < 0 or target_bi_idx >= len(bi_list):
            return {"error": f"笔索引 {bi_idx} 越界，笔总数 {len(bi_list)}"}

        # 检查选点后至少需要4笔
        remaining_bis = len(bi_list) - target_bi_idx - 1
        if remaining_bis < 4:
            return {"error": f"选点之后仅剩 {remaining_bis} 笔，至少需要4笔才能构建中枢，请重新选点"}

        # Step 2: 找到左肩时间T
        start_time = _find_left_shoulder_time(kl_list, bi_list, target_bi_idx, freq)
        if start_time is None:
            return {"error": "无法定位左肩K线时间，请重试"}

        print(f"[{display_key}] 选点左肩时间: {start_time}")

        # Step 3: 保存选点到CSV
        name = _get_futures_name(symbol)
        _save_point_time(symbol, name, freq, start_time)
        if symbol not in _saved_point_times:
            _saved_point_times[symbol] = {}
        _saved_point_times[symbol]["name"] = name
        _saved_point_times[symbol][FREQ_TO_COL.get(freq, "")] = start_time

        # Step 4: 关闭旧TqApi，创建新TqApi，从T重新拉取
        if api is not None:
            try:
                api.close()
            except Exception:
                pass
            api = None

        t_conn2 = time.time()
        api2 = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
        print(f"[{display_key}] ⓪ 重新连接天勤(选点后): 耗时 {time.time()-t_conn2:.1f}s")

        records2 = fetch_futures_kline(api2, symbol, freq_sec=freq_sec,
                                       display_key=display_key, start_time=start_time)
        if len(records2) < 5:
            return {"error": f"选点后K线数据不足: 仅{len(records2)}条"}

        # 注入数据源 + 创建新 CChan
        CTqSdkAPI.set_data(records2, symbol=f"{symbol}:{freq_sec}")

        chan2 = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
        )
        for _snapshot in chan2.step_load():
            pass

        # Step 5: 提取快照并返回
        result = _extract_realtime_snapshot(chan2, kl_type, symbol, name, freq_label,
                                            saved_selection_date=start_time)
        print(f"[{display_key}] 选点完成: {len(result['klines'])}K线, {result['meta']['bi_count']}笔, {result['meta']['zs_count']}中枢")
        return result

    except Exception as e:
        import traceback
        print(f"[{display_key}] 选点异常: {e}")
        traceback.print_exc()
        return {"error": f"选点失败: {str(e)}"}
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass
        if api2 is not None:
            try:
                api2.close()
            except Exception:
                pass


def analyze_stock(code, freq="d", end_date=None, cache_chan=True):
    """公开入口：冷启动和复盘均在当前进程运行。
    复盘模式用全量 records 重新创建 CChan 进行真实复盘计算。
    cache_chan=False: 扫描模式，只缓存result不缓存CChan对象，节省内存。
    """
    return _analyze_stock_internal(code, freq=freq, end_date=end_date, cache_chan=cache_chan)


# ============================================================
# 自选股扫描：批量分析自选股中的买点
# ============================================================
ZXG_BLK_PATH = r"C:\new_tdx_test\T0002\blocknew\zxg.blk"


def read_zxg_stocks():
    """
    读取通达信自选股文件 zxg.blk，返回股票代码列表。
    文件格式：GBK编码，每行一个代码。
    A股格式：7位纯数字（1位交易所前缀 + 6位股票代码），如 "0600000"、"1600001"
    港股格式：可能为 "HK00700" 或前缀+5位数字等
    """
    if not os.path.exists(ZXG_BLK_PATH):
        print(f"[stock][警告] 自选股文件不存在: {ZXG_BLK_PATH}")
        return []
    stocks = []
    try:
        with open(ZXG_BLK_PATH, "r", encoding="gbk") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # A股：7位纯数字（前缀0/1/2 + 6位代码）
                if len(line) == 7 and line.isdigit():
                    prefix = line[0]
                    code = line[1:7]
                    stocks.append({"prefix": prefix, "code": code})
                # 港股：通达信内部格式 31#XXXXX（如 31#00700、31#09926）
                elif line.startswith("31#") and len(line) == 8:
                    code = line[3:].strip()
                    if code.isdigit():
                        stocks.append({"prefix": "hk", "code": code})
                # 港股：HK前缀 + 数字代码
                elif line.upper().startswith("HK") and len(line) > 2:
                    code = line[2:].strip()
                    if code.isdigit():
                        stocks.append({"prefix": "hk", "code": code})
                # 其他格式：尝试匹配任何可识别的代码
                elif len(line) >= 4 and line.isdigit():
                    # 可能是港股5位代码（无前缀）
                    if len(line) == 5:
                        stocks.append({"prefix": "hk", "code": line})
                    elif len(line) == 6:
                        stocks.append({"prefix": "hk", "code": "0" + line})
                    else:
                        # 未知格式，跳过
                        pass
    except Exception as e:
        print(f"[错误] 读取自选股文件失败: {e}")
    return stocks


def scan_zxg_buy_points(freq="d"):
    """
    批量扫描自选股，找出当天（最后一根K线）有买点的股票。
    有买点的股票保留缓存（方便后续点击查看），无买点的立即清除缓存释放内存。
    【内存优化】扫描模式下只缓存轻量 result，不缓存 CChan 对象。
    返回列表，每项包含：code, name, market, buy_points(买点列表)
    """
    import gc

    stocks = read_zxg_stocks()
    if not stocks:
        return {"error": "自选股为空或文件不存在", "total": 0, "results": []}

    results = []
    skipped = 0
    total = len(stocks)

    print(f"[自选扫描] 开始扫描 {total} 只自选股，周期={_get_freq_label(freq)}")

    for idx, stk in enumerate(stocks):
        # 内存保护：每10只检查一次
        if (idx + 1) % 10 == 0:
            _check_memory_and_protect()

        code = stk["code"]
        prefix = stk["prefix"]
        # 转换为市场标识
        if prefix == "1":
            market = "sh"
        elif prefix == "0":
            market = "sz"
        elif prefix == "2":
            market = "bj"  # 北交所，通达信可能没有数据
        else:
            continue

        # 跳过明显非股票的代码
        if code.startswith("399") or code.startswith("000") and market == "sz" and len(code) == 6 and int(code) < 1000:
            skipped += 1
            continue

        try:
            # 扫描串行化：加锁防止并发创建多个CChan导致内存峰值
            with _scan_lock:
                result = analyze_stock(code, freq=freq, cache_chan=False)
            if "error" in result:
                # 分析失败，清除可能残留的缓存
                cache_key = f"{_CACHE_VERSION}_{market}_{code}_{freq}"
                if cache_key in _analysis_cache:
                    del _analysis_cache[cache_key]
                skipped += 1
                continue

            bsps = result.get("bsps", [])
            stock_name = result.get("meta", {}).get("name", f"{code}.{market.upper()}")
            # [DEBUG] 打印扫描中meta信息
            print(f"[DEBUG-名称] scan_zxg_buy_points({code}, {freq}) meta.name='{result.get('meta', {}).get('name')}', meta={result.get('meta', {})}")
            print(f"[DEBUG-名称] scan_zxg_buy_points({code}, {freq}) stock_name='{stock_name}'")

            # 筛选最后一根K线上的买点
            klines = result.get("klines", [])
            if not klines:
                cache_key = f"{_CACHE_VERSION}_{market}_{code}_{freq}"
                if cache_key in _analysis_cache:
                    del _analysis_cache[cache_key]
                skipped += 1
                continue
            last_kline_date = klines[-1]["date"]

            today_buy_points = []
            for bsp in bsps:
                if bsp.get("is_buy", False) and bsp.get("date", "") == last_kline_date:
                    today_buy_points.append({
                        "type": bsp.get("type", ""),
                        "price": bsp.get("price", 0),
                        "date": bsp.get("date", ""),
                    })

            cache_key = f"{_CACHE_VERSION}_{market}_{code}_{freq}"
            if today_buy_points:
                # 有买点：只缓存轻量 result（cache_chan=False已处理），后续点击可直接查看
                results.append({
                    "code": code,
                    "name": stock_name,
                    "market": market,
                    "buy_points": today_buy_points,
                    "last_close": klines[-1]["close"],
                })
                print(f"[自选扫描] ({idx+1}/{total}) {stock_name}({code}) 发现 {len(today_buy_points)} 个买点 [缓存保留]")
            else:
                # 无买点：清除缓存释放内存
                if cache_key in _analysis_cache:
                    del _analysis_cache[cache_key]

            # 进度反馈（每10只打印一次）
            if (idx + 1) % 10 == 0 or idx == total - 1:
                print(f"[自选扫描] 进度: {idx+1}/{total}, 已发现 {len(results)} 只买点股")

        except Exception as e:
            print(f"[自选扫描] ({idx+1}/{total}) {code} 分析异常: {e}")
            cache_key = f"{_CACHE_VERSION}_{market}_{code}_{freq}"

    # 扫描完毕，触发一次GC回收
    gc.collect()
    print(f"[自选扫描] 扫描完成: 共{total}只, 扫描{total-skipped}只, 跳过{skipped}只, 发现{len(results)}只有买/卖点")
    return {"error": None, "total": total, "skipped": skipped, "results": results}


# ============================================================
# HTTP 服务器
# ============================================================
class ChartHandler(SimpleHTTPRequestHandler):
    """HTTP请求处理器"""
    def handle_one_request(self):
        """静默处理客户端断开连接，避免 ConnectionAbortedError 日志噪音"""
        try:
            super().handle_one_request()
        except ConnectionAbortedError:
            pass
        except ConnectionResetError:
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/stock":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            end_date = params.get("end_date", [""])[0] or None
            if not code:
                self.send_json_response({"error": "请输入股票代码"}, 400)
                print_memory(f"前端操作(查询股票-{code or '空'})")
                return
            try:
                result = analyze_stock(code, freq=freq, end_date=end_date)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
            except Exception as e:
                import traceback
                print(f"[错误] analyze_stock异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
            print_memory(f"前端操作(查询股票-{code})")
        elif parsed.path == "/api/manual_zs":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            bi_idx = params.get("bi_idx", ["-1"])[0]
            if not code or bi_idx == "-1":
                self.send_json_response({"error": "缺少必要参数 code 或 bi_idx"}, 400)
                return
            try:
                result = manual_select_zs(code, freq=freq, bi_idx=bi_idx)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
            except Exception as e:
                import traceback
                print(f"[错误] manual_select_zs异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
        elif parsed.path == "/api/dual_zs":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            start_bi = int(params.get("start_bi", ["-1"])[0])
            end_bi = int(params.get("end_bi", ["-1"])[0])
            if not code or start_bi < 0 or end_bi < 0 or start_bi > end_bi:
                self.send_json_response({"error": "参数错误"}, 400)
                return
            try:
                result = compute_dual_zs(code, freq=freq, start_bi=start_bi, end_bi=end_bi)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
            except Exception as e:
                import traceback
                print(f"[错误] dual_zs异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
        elif parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            keyword = params.get("q", [""])[0]
            if not keyword:
                self.send_json_response({"error": "请输入搜索关键词"}, 400)
                return
            _load_stock_names_from_cache_file()
            keyword_upper = keyword.upper()
            exact_results = []
            prefix_results = []
            other_results = []

            for compound_key, info in _stock_names_cache.items():
                if isinstance(info, dict):
                    name = info.get("name", "")
                    pinyin = info.get("pinyin", "")
                    market = info.get("market", "")
                else:
                    name = info
                    pinyin = ""
                    market = ""

                if not name:
                    continue

                # 从复合键中提取纯代码（去掉市场前缀）
                if market and compound_key.startswith(market):
                    bare_code = compound_key[len(market):]
                else:
                    bare_code = compound_key

                # 匹配：用户输入匹配纯代码、名称或拼音
                if not (keyword_upper in bare_code or keyword_upper in name.upper() or keyword_upper in pinyin):
                    continue

                if not market:
                    if len(bare_code) == 5 and bare_code.isdigit():
                        market = "hk"
                    elif bare_code.startswith("6") or bare_code.startswith("5") or bare_code.startswith("9"):
                        market = "sh"
                    elif bare_code.startswith("0") or bare_code.startswith("3") or bare_code.startswith("2"):
                        market = "sz"
                    elif bare_code.startswith("88") or bare_code.startswith("99"):
                        market = "sh"
                    else:
                        market = "sz"

                item = {"code": bare_code, "name": name, "pinyin": pinyin, "market": market, "type": ""}
                if bare_code == keyword_upper:
                    exact_results.append(item)
                elif bare_code.startswith(keyword_upper):
                    prefix_results.append(item)
                else:
                    other_results.append(item)

            results = exact_results[:10] + prefix_results[:10] + other_results[:10]
            results = results[:10]

            # 期货/期指搜索：匹配本地品种名称表
            try:
                from DataAPI.TqSdkAPI import DEFAULT_FUTURES_SYMBOLS
                _futures_search = _get_futures_name  # 使用本地函数
                for sym, name, freq_sec, freq_label in DEFAULT_FUTURES_SYMBOLS:
                    if keyword_upper in sym.upper() or keyword_upper in name.upper():
                        # 避免重复（如果主连代码恰好命中）
                        if not any(r["code"] == sym for r in results):
                            results.append({
                                "code": sym, "name": name, "pinyin": "",
                                "market": "futures", "type": freq_label,
                            })
            except Exception:
                pass

            # 本地缓存未找到或不够，再查东方财富API补充（已注释掉，避免频繁请求被拉黑）
            # if len(results) < 10:
            #     try:
            #         import urllib.request
            #         url = f"http://searchapi.eastmoney.com/api/suggest/get?input={urllib.parse.quote(keyword)}&type=14&token=D43BF722C8E33BDC906FB84D85E326E&count=10"
            #         req = urllib.request.Request(url, headers={
            #             "User-Agent": "Mozilla/5.0",
            #             "Referer": "https://quote.eastmoney.com/"
            #         })
            #         with urllib.request.urlopen(req, timeout=5) as resp:
            #             text = resp.read().decode("utf-8", errors="ignore")
            #         data = json.loads(text)
            #         for item in data.get("QuotationCodeTable", {}).get("Data", []):
            #             code = item.get("Code", "")
            #             jys = item.get("JYS", "")
            #             name = item.get("Name", "")
            #             sec_type = item.get("SecurityType", "")
            #             if sec_type not in ("1", "2", "3", "5", "6", "19", "25"):
            #                 continue
            #             if jys not in ("0", "1", "2", "3", "6", "23", "80", "HK"):
            #                 continue
            #             if code in ("002002",):
            #                 continue
            #             if name.startswith("*ST") or name.startswith("ST"):
            #                 continue
            #             if jys == "1" and code == "000001" and "上证" in name:
            #                 code = "999999"
            #             if any(r["code"] == code for r in results):
            #                 continue
            #             if jys == "HK":
            #                 market = "hk"
            #             elif jys in ("1", "2"):
            #                 market = "sh"
            #             elif jys in ("0", "3", "6", "80"):
            #                 market = "sz"
            #             elif jys == "23":
            #                 market = "sh"
            #             else:
            #                 market = "sz"
            #             results.append({
            #                 "code": code,
            #                 "name": name,
            #                 "pinyin": item.get("PinYin", ""),
            #                 "market": market,
            #                 "type": item.get("SecurityTypeName", ""),
            #             })
            #             if len(results) >= 10:
            #                 break
            #     except Exception as e:
            #         import traceback
            #         print(f"[错误] 搜索异常: {e}")
            #         traceback.print_exc()
            self.send_json_response({"results": results}, 200)
        elif parsed.path == "/api/zxg_list":
            # 返回自选股列表（供前端逐只扫描）
            try:
                stocks = read_zxg_stocks()
                self.send_json_response({"stocks": stocks}, 200)
            except Exception as e:
                self.send_json_response({"error": str(e)}, 500)
        elif parsed.path == "/api/scan_one":
            # 扫描单只股票的买卖点（供前端逐只调用，实时显示进度）
            import time
            t_scan_start = time.time()
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            prefix = params.get("prefix", [""])[0]
            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return
            try:
                _check_memory_and_protect()
                t0 = time.time()
                with _scan_lock:
                    result = analyze_stock(code, freq=freq, cache_chan=False)
                t_analyze = time.time() - t0
                if "error" in result:
                    # 分析失败，清除缓存，收集跳过原因（不实时打印）
                    cache_key = f"{_CACHE_VERSION}_{code}_{freq}"
                    if cache_key in _analysis_cache:
                        del _analysis_cache[cache_key]
                    _scan_skip_log.append(f"{code} - {result['error']}")
                    print(f"[耗时-扫描] {code} 分析失败: {result['error']}, 耗时{t_analyze:.3f}s")
                    self.send_json_response({"error": result["error"]}, 200)
                else:
                    t0 = time.time()
                    bsps = result.get("bsps", [])
                    stock_name = result.get("meta", {}).get("name", f"{code}")
                    # [DEBUG] 打印API响应中的名称
                    print(f"[DEBUG-名称] /api/scan_one({code}) meta.name='{result.get('meta', {}).get('name')}', stock_name='{stock_name}'")
                    klines = result.get("klines", [])
                    last_kline_date = klines[-1]["date"] if klines else ""
                    buy_points = []
                    sell_points = []
                    for bsp in bsps:
                        if bsp.get("date", "") == last_kline_date:
                            point = {
                                "type": bsp.get("type", ""),
                                "price": bsp.get("price", 0),
                                "date": bsp.get("date", ""),
                            }
                            if bsp.get("is_buy", False):
                                buy_points.append(point)
                            else:
                                sell_points.append(point)
                    has_points = buy_points or sell_points
                    t_filter = time.time() - t0
                    if has_points:
                        # 有买/卖点：保留缓存
                        # 计算流通市值和MA120标记
                        t0 = time.time()
                        market_val, code_val = parse_stock_code(code)
                        if not market_val:
                            if prefix == "hk":
                                market_val = "hk"
                            elif prefix in ("0", "1"):
                                market_val = "sh" if prefix == "1" else "sz"
                            else:
                                market_val = prefix
                        float_mv = get_stock_float_mv_local(market_val, code_val, klines[-1]["close"] if klines else 0)
                        t_float = time.time() - t0
                        t0 = time.time()
                        below_ma120 = None  # None表示不比较（K线不足120根）
                        if len(klines) >= 120:
                            ma120_sum = sum(k["close"] for k in klines[-120:])
                            ma120_val = ma120_sum / 120
                            below_ma120 = klines[-1]["close"] < ma120_val
                        t_ma120 = time.time() - t0
                        t_total = time.time() - t_scan_start
                        print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s 流值{t_float:.3f}s MA120{t_ma120:.3f}s) 有买卖点")
                        resp_data = {
                            "code": code, "name": stock_name,
                            "buy_points": buy_points,
                            "sell_points": sell_points,
                            "last_close": klines[-1]["close"] if klines else 0,
                            "float_mv": float_mv,
                            "below_ma120": below_ma120,
                            "freq": freq,
                        }
                        print(f"[DEBUG-名称] /api/scan_one({code}) resp_data.name='{resp_data['name']}'")
                    else:
                        # 无买/卖点：清除缓存
                        cache_key = f"{_CACHE_VERSION}_{code}_{freq}"
                        if cache_key in _analysis_cache:
                            del _analysis_cache[cache_key]
                        t_total = time.time() - t_scan_start
                        print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 无买卖点")
                        resp_data = {"code": code, "buy_points": [], "sell_points": []}
                    self.send_json_response(resp_data, 200)
            except Exception as e:
                import traceback
                _scan_skip_log.append(f"{code} - 异常: {e}")
                cache_key = f"{_CACHE_VERSION}_{code}_{freq}"
                if cache_key in _analysis_cache:
                    del _analysis_cache[cache_key]
                t_total = time.time() - t_scan_start
                print(f"[耗时-扫描] {code} 异常: {e}, 总耗时{t_total:.3f}s")
                self.send_json_response({"error": str(e)}, 200)
        elif parsed.path == "/api/scan_start":
            # 新一轮扫描开始：清空跳过记录，优先从缓存文件加载
            _scan_skip_log.clear()
            # 检查缓存文件是否存在
            if not os.path.exists(_GBBQ_CACHE_FILE):
                self.send_json_response({"ok": False, "need_refresh": True, "msg": "未找到缓存数据，请先点击右上角刷新按钮"}, 200)
                return
            try:
                _load_float_shares_from_cache_file()
                _load_stock_names_from_cache_file()
            except Exception:
                pass
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/gbbq_refresh":
            # 启动GBBQ刷新（后台线程解密全部记录并保存到缓存文件）
            if _gbbq_refresh_status["running"]:
                self.send_json_response({"status": "already_running", **_gbbq_refresh_status}, 200)
            else:
                t = threading.Thread(target=_refresh_gbbq_to_file, daemon=True)
                t.start()
                self.send_json_response({"status": "started"}, 200)
        elif parsed.path == "/api/gbbq_status":
            # 查询GBBQ刷新进度
            self.send_json_response(_gbbq_refresh_status, 200)
        elif parsed.path == "/api/refresh_stock_names":
            # 独立刷新股票名称缓存（不依赖GBBQ）
            def _do_refresh_names():
                try:
                    _refresh_stock_names()
                except Exception as e:
                    import traceback
                    print(f"[错误] refresh_stock_names异常: {e}")
                    traceback.print_exc()
            t = threading.Thread(target=_do_refresh_names, daemon=True)
            t.start()
            self.send_json_response({"status": "started", "msg": "股票名称刷新已启动"}, 200)
        elif parsed.path == "/api/scan_end":
            # 扫描结束：统一打印跳过记录
            if _scan_skip_log:
                print("\n========== 扫描跳过股票明细 ==========")
                print(f"共跳过 {len(_scan_skip_log)} 只:")
                for i, item in enumerate(_scan_skip_log, 1):
                    print(f"  {i}. {item}")
                print("========================================\n")
            else:
                print("\n[扫描明细] 无跳过股票\n")
            self.send_json_response({"count": len(_scan_skip_log)}, 200)
        elif parsed.path == "/api/clear_saved_point":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return
            normalized_code = code.strip().upper()
            prefix_match = re.match(r'^(SH|SZ|HK)(\d+)$', normalized_code)
            suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK)$', normalized_code)
            if prefix_match:
                normalized_code = prefix_match.group(2)
            elif suffix_match:
                normalized_code = suffix_match.group(1)
            _clear_saved_point_time(normalized_code, freq)
            # 销毁该周期的缓存
            cache_key = f"{_CACHE_VERSION}_{normalized_code}_{freq}"
            if cache_key in _analysis_cache:
                del _analysis_cache[cache_key]
            import gc
            gc.collect()
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/futures_manual_zs":
            # 期货期指双击选点
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [""])[0]
            freq = params.get("freq", ["15s"])[0]
            bi_idx = params.get("bi_idx", ["-1"])[0]
            if not symbol or bi_idx == "-1":
                self.send_json_response({"error": "缺少必要参数 symbol 或 bi_idx"}, 400)
                return
            try:
                result = futures_manual_select_zs(symbol, freq=freq, bi_idx=bi_idx)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
            except Exception as e:
                import traceback
                print(f"[错误] futures_manual_select_zs异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
        elif parsed.path == "/api/futures_clear_saved_point":
            # 期货期指清除选点
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [""])[0]
            freq = params.get("freq", ["15s"])[0]
            if not symbol:
                self.send_json_response({"error": "缺少symbol参数"}, 400)
                return
            _clear_saved_point_time(symbol, freq)
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/futures_status":
            # 新架构：每个 SSE 连接自包含，无共享引擎，始终返回 ok
            self.send_json_response({"ok": True, "architecture": "self-contained"}, 200)
        elif parsed.path == "/api/futures_config":
            # 前端查询可用周期列表（用于变灰不可用按钮）
            from DataAPI.TqSdkAPI import SUPPORTED_FREQS, DISABLED_FREQS
            self.send_json_response({
                "supported_freqs": SUPPORTED_FREQS,
                "disabled_freqs": DISABLED_FREQS,
            }, 200)
        elif parsed.path == "/api/futures_stream":
            # SSE 实时推送端点
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [""])[0]
            freq = params.get("freq", ["15s"])[0]
            start_time = params.get("start_time", [""])[0] or None
            if not symbol:
                self.send_json_response({"error": "缺少symbol参数"}, 400)
                return
            self._handle_sse_stream(symbol, freq, start_time=start_time)
            return
        elif parsed.path == "/api/annotations":
            # 获取某股票某周期的标注数据
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return
            anns = _get_annotations_for(code, freq)
            self.send_json_response({"annotations": anns, "code": code, "freq": freq}, 200)
        elif parsed.path == "/api/annotations_scan":
            # 自选扫描：返回有标注的股票列表
            params = parse_qs(parsed.query)
            freq = params.get("freq", [""])[0]
            codes = _get_annotated_codes(freq)
            self.send_json_response({"codes": codes, "total": len(codes)}, 200)
        else:
            filepath = os.path.join(OUTPUT_DIR, parsed.path.lstrip("/"))
            if os.path.isfile(filepath):
                with open(filepath, "rb") as f:
                    content = f.read()
                self.send_response(200)
                if filepath.endswith(".html"):
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                elif filepath.endswith(".js"):
                    self.send_header("Content-Type", "application/javascript")
                elif filepath.endswith(".css"):
                    self.send_header("Content-Type", "text/css")
                else:
                    self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404)
            print_memory(f"前端操作(请求文件-{parsed.path})")

    def _handle_sse_stream(self, symbol, freq="15s", start_time=None):
        """Server-Sent Events 推送端：自包含——创建TqApi → 拉取历史 → chan分析 → 快照 → 实时循环。
        每个 SSE 连接独立，互不干扰。
        start_time: 选点起始时间，有值时从该时间拉取K线；无值时自动查询CSV保存的选点。"""
        import logging
        import time
        logging.getLogger("tqsdk").setLevel(logging.WARNING)
        logging.getLogger("tqsdk.tqapi").setLevel(logging.WARNING)
        for h in logging.root.handlers:
            h.setLevel(logging.WARNING)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        from tqsdk import TqApi, TqAuth
        from DataAPI.TqSdkAPI import (init_chan_symbol, _extract_realtime_snapshot,
                                       FREQ_SEC_MAP, FREQ_LABEL_CN, CTqSdkAPI,
                                       TQ_ACCOUNT, TQ_PASSWORD)
        from datetime import datetime

        api = None
        chan = None
        try:
            freq_sec = FREQ_SEC_MAP.get(freq, 15)
            freq_label = freq
            freq_cn = FREQ_LABEL_CN.get(freq_label, freq_label)
            display_key = f"{symbol}:{freq_cn}"

            # 如果没有传入 start_time，查询CSV中是否有保存的选点
            if start_time is None:
                col = FREQ_TO_COL.get(freq, "")
                if col and symbol in _saved_point_times:
                    _saved = _saved_point_times[symbol].get(col, "").strip() or None
                    if _saved:
                        start_time = _saved
                        print(f"[{display_key}] 检测到保存选点: {start_time}")

            saved_selection_date = start_time or ""

            t_conn = time.time()
            api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
            print(f"[{display_key}] ⓪ 连接天勤: 耗时 {time.time()-t_conn:.1f}s")

            t_total = time.time()
            name = _get_futures_name(symbol)  # 品种名称

            # === 1. 拉取历史 + chan 分析 ===
            result = init_chan_symbol(api, symbol, name, freq_sec, freq_label, start_time=start_time)
            if result is None:
                err = json.dumps({"error": "初始化失败（无数据或网络异常）", "symbol": symbol})
                self.wfile.write(f"event: init\ndata: {err}\n\n".encode("utf-8"))
                self.wfile.flush()
                return
            chan, klines, kl_type, records = result

            # === 2. 推送初始快照 ===
            t0 = time.time()
            try:
                init_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                       saved_selection_date=saved_selection_date)
                init_str = json.dumps(init_data, ensure_ascii=False, allow_nan=False)
                self.wfile.write(f"event: init\ndata: {init_str}\n\n".encode("utf-8"))
                self.wfile.flush()
                print(f"[{display_key}] ⑶ 推送初始快照: "
                      f"K线{init_data['meta']['kline_count']}, "
                      f"笔{init_data['meta']['bi_count']}, "
                      f"中枢{init_data['meta']['zs_count']}, "
                      f"耗时 {time.time()-t0:.1f}s")
            except Exception as e:
                err = json.dumps({"error": f"快照提取失败: {e}", "symbol": symbol})
                self.wfile.write(f"event: init\ndata: {err}\n\n".encode("utf-8"))
                self.wfile.flush()
                return

            # === 3. 实时循环：wait_update → 检测新K线 → step_load → 推送 ===
            print(f"[{display_key}] ⑷ 进入实时循环 (总耗时 {time.time()-t_total:.1f}s)")
            last_bar_time = None
            last_realtime_period = 0
            while True:
                try:
                    api.wait_update(deadline=time.time() * 1e9 + 500_000_000)
                except Exception as e:
                    print(f"[{display_key}] wait_update 异常: {e}")
                    time.sleep(0.5)
                    continue

                now = datetime.now()
                if len(klines) == 0:
                    continue

                last_row = klines.iloc[-1]
                dt_ns = last_row.get("datetime")
                if dt_ns is None:
                    continue

                if last_bar_time is None:
                    last_bar_time = dt_ns
                    last_realtime_period = 0
                    continue

                now_ts = now.timestamp()
                realtime_period_start = (now_ts // freq_sec) * freq_sec
                if realtime_period_start > last_realtime_period:
                    last_realtime_period = realtime_period_start

                new_bar_completed = (dt_ns != last_bar_time)

                if new_bar_completed:
                    last_bar_time = dt_ns
                    completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row

                    o = float(completed_row.get("open", 0) or 0)
                    h = float(completed_row.get("high", 0) or 0)
                    l = float(completed_row.get("low", 0) or 0)
                    cl = float(completed_row.get("close", 0) or 0)
                    vol = int(completed_row.get("volume", 0) or 0)
                    oi = float(completed_row.get("open_oi", 0) or 0)
                    h = max(h, o, cl)
                    l = min(l, o, cl)

                    completed_dt_ns = completed_row.get("datetime")
                    dt = datetime.fromtimestamp((completed_dt_ns or dt_ns) / 1e9)

                    code_key = f"{symbol}:{freq_sec}"
                    new_bar = {
                        "dt": dt, "open": round(o, 3), "high": round(h, 3),
                        "low": round(l, 3), "close": round(cl, 3),
                        "vol": vol, "amount": round(oi, 2),
                    }

                    last_records = CTqSdkAPI.get_last_n(1, symbol=code_key)
                    if not last_records or last_records[0]["dt"] != dt:
                        CTqSdkAPI.append_bar(new_bar, symbol=code_key)
                        try:
                            for _snapshot in chan.step_load():
                                pass
                        except Exception as e:
                            print(f"[{display_key}] step_load 异常: {e}")

                    print(f"[{display_key}] 完成新K线: "
                          f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f}")

                # 推送当前快照
                t_push = time.time()
                try:
                    update_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                       saved_selection_date=saved_selection_date)
                    update_str = json.dumps(update_data, ensure_ascii=False, allow_nan=False)
                    self.wfile.write(f"event: update\ndata: {update_str}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    if new_bar_completed:
                        print(f"[{display_key}] 推送更新: 耗时 {time.time()-t_push:.1f}s")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                except Exception as e:
                    print(f"[{display_key}] 推送异常: {e}")

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            print(f"[{display_key}] 连接异常: {e}")
        finally:
            if api is not None:
                try:
                    api.close()
                except Exception:
                    pass

    def do_POST(self):
        """处理 POST 请求（标注增删、扫描等）"""
        parsed = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_length > 0:
            try:
                raw_body = self.rfile.read(content_length)
                body = json.loads(raw_body.decode("utf-8"))
            except Exception:
                body = {}

        if parsed.path == "/api/annotations":
            action = body.get("action", "")
            code = body.get("code", "")
            freq = body.get("freq", "d")
            date_str = body.get("date", "")
            text = body.get("text", "")
            y_offset = body.get("y_offset", 0)

            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return

            if action == "add":
                if not date_str or not text:
                    self.send_json_response({"error": "缺少date或text参数"}, 400)
                    return
                success = _add_annotation(code, freq, date_str, text, y_offset)
                self.send_json_response({"ok": success, "duplicate": not success}, 200)
            elif action == "delete":
                if not date_str or not text:
                    self.send_json_response({"error": "缺少date或text参数"}, 400)
                    return
                success = _delete_annotation(code, freq, date_str, text)
                self.send_json_response({"ok": success}, 200)
            elif action == "delete_by_date":
                if not date_str:
                    self.send_json_response({"error": "缺少date参数"}, 400)
                    return
                success = _delete_annotation_by_date(code, freq, date_str)
                self.send_json_response({"ok": success}, 200)
            elif action == "delete_all":
                success = _delete_all_annotations(code, freq)
                self.send_json_response({"ok": success}, 200)
            elif action == "update":
                old_text = body.get("old_text", "")
                new_text = body.get("text", "")
                if not date_str or not old_text or not new_text:
                    self.send_json_response({"error": "缺少date/old_text/text参数"}, 400)
                    return
                _delete_annotation(code, freq, date_str, old_text)
                success = _add_annotation(code, freq, date_str, new_text, y_offset)
                self.send_json_response({"ok": success}, 200)
            else:
                self.send_json_response({"error": f"未知action: {action}"}, 400)
        else:
            self.send_json_response({"error": "未知路径"}, 404)

    def send_json_response(self, data, status_code):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


# ============================================================
# HTML 模板（完整版，从 czsc 版本适配）
# ============================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>缠论K线分析 - chan.py</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB',
                         'Microsoft YaHei', 'Helvetica Neue', Helvetica, Arial, sans-serif;
            background: #1a1a2e; color: #e0e0e0;
            overflow: hidden; height: 100vh; user-select: none;
        }
        .header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 8px 20px; background: #16213e;
            border-bottom: 1px solid #0f3460; height: 48px;
        }
        .header-left { display: flex; align-items: center; gap: 16px; }
        .stock-input {
            display: flex; align-items: center; gap: 6px;
            position: relative;
        }
        .stock-input label { color: #8892b0; font-size: 12px; white-space: nowrap; }
        .stock-input input {
            width: 150px; padding: 4px 6px; font-size: 12px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #a8b2d1; outline: none; color-scheme: dark;
        }
        .stock-input input:focus { border-color: #e94560; }
        .stock-input-wrap {
            position: relative; display: inline-block;
        }
        .stock-input-wrap input { padding-right: 22px !important; }
        .stock-input-clear {
            position: absolute; right: 5px; top: 50%; transform: translateY(-50%);
            color: #555; font-size: 13px; cursor: pointer;
            user-select: none; line-height: 1; z-index: 1;
        }
        .stock-input-clear:hover { color: #e94560; }
        .stock-input button {
            padding: 4px 12px; font-size: 12px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #a8b2d1; cursor: pointer; transition: all 0.2s;
        }
        .stock-input button:hover { background: #0f3460; color: #e0e0e0; }
        .stock-history {
            position: absolute; top: 100%; left: 0;
            background: #16213e; border: 1px solid #0f3460; border-radius: 4px;
            max-height: 264px; overflow-y: auto; z-index: 200;
            display: none; min-width: 120px;
        }
        .stock-history.show { display: block; }
        .stock-history-item {
            padding: 4px 10px; font-size: 12px; color: #a8b2d1;
            cursor: pointer; white-space: nowrap;
            display: flex; justify-content: space-between; align-items: center;
        }
        .stock-history-item:hover { background: #0f3460; color: #e0e0e0; }
        .stock-history-del {
            color: #555; font-size: 14px; margin-left: 12px; padding: 0 2px;
            cursor: pointer; flex-shrink: 0;
        }
        .stock-history-del:hover { color: #e94560; }
        .stock-history-clear {
            padding: 4px 10px; font-size: 12px; color: #555;
            cursor: pointer; text-align: center;
            border-top: 1px solid #0f3460;
        }
        .stock-history-clear:hover { color: #e94560; }
        .stock-name { font-size: 18px; font-weight: 700; color: #e94560; }
        .stock-code { font-size: 11px; color: #8892b0; }
        .header-right { display: flex; align-items: center; gap: 12px; }
        .btn-icon {
            display: inline-flex; align-items: center; justify-content: center;
            width: 28px; height: 28px; border-radius: 4px;
            border: 1px solid #0f3460; background: #1a1a2e;
            cursor: pointer; transition: all 0.2s; padding: 0;
            flex-shrink: 0;
        }
        .btn-icon:hover { background: #0f3460; }
        .btn-icon.active { background: #e94560; border-color: #e94560; }
        .btn-icon.active svg { fill: #fff; }
        .btn-icon svg { width: 16px; height: 16px; fill: #a8b2d1; }
        .btn-icon:hover svg { fill: #e0e0e0; }
        .btn {
            padding: 4px 12px; border-radius: 4px; border: 1px solid #0f3460;
            background: #1a1a2e; color: #a8b2d1; font-size: 11px;
            cursor: pointer; transition: all 0.2s;
        }
        .btn:hover { background: #0f3460; color: #e0e0e0; }
        .btn.active { background: #e94560; border-color: #e94560; color: #fff; }
        .btn:disabled { opacity: 0.35; cursor: not-allowed; }
        .btn:disabled:hover { background: #1a1a2e; color: #a8b2d1; }
        .freq-btn {
            padding: 2px 8px; border-radius: 3px; border: 1px solid #0f3460;
            background: #1a1a2e; color: #a8b2d1; font-size: 11px;
            cursor: pointer; transition: all 0.2s; margin-left: 4px;
        }
        .freq-btn:hover { background: #0f3460; color: #e0e0e0; }
        .freq-btn.active { background: #e94560; border-color: #e94560; color: #fff; }
        .freq-btn:disabled { opacity: 0.35; cursor: not-allowed; }
        .freq-btn:disabled:hover { background: #1a1a2e; color: #a8b2d1; }
        .realtime-badge {
            display: none; align-items: center; gap: 4px; padding: 2px 8px;
            border-radius: 10px; background: #27ae60; color: #fff; font-size: 11px;
            font-weight: bold; margin-left: 8px; animation: pulse-badge 2s infinite;
        }
        .realtime-badge.visible { display: flex; }
        .realtime-badge.stopped { background: #7f8c8d; animation: none; }
        @keyframes pulse-badge {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }
        #chart-container {
            width: 100%; height: calc(100vh - 80px);
            position: relative; cursor: crosshair;
        }
        #chart-container canvas { display: block; }
        .crosshair-info {
            position: absolute; top: 8px; left: 20px;
            font-size: 12px; color: #a8b2d1;
            pointer-events: none; z-index: 10; line-height: 1.6;
        }
        .crosshair-info .label { color: #8892b0; }
        .crosshair-info .up { color: #FF4444; }
        .crosshair-info .down { color: #00DD00; }
        .legend {
            position: fixed; bottom: 16px; left: 20px;
            display: flex; gap: 16px; padding: 8px 16px;
            background: rgba(22, 33, 62, 0.9);
            border-radius: 6px; border: 1px solid #0f3460;
            font-size: 12px; z-index: 100;
        }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-color { width: 20px; height: 3px; border-radius: 2px; }
        .legend-dot { width: 8px; height: 8px; border-radius: 50%; }
        .loading-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: #1a1a2e; display: flex; flex-direction: column;
            align-items: center; justify-content: center;
            z-index: 1000; transition: opacity 0.5s;
        }
        .loading-overlay.hidden { opacity: 0; pointer-events: none; }
        .loading-spinner {
            width: 40px; height: 40px;
            border: 3px solid #0f3460; border-top-color: #e94560;
            border-radius: 50%; animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loading-text { margin-top: 16px; color: #8892b0; font-size: 14px; }
        .error-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: #1a1a2e; display: flex; flex-direction: column;
            align-items: center; justify-content: center; z-index: 1000;
        }
        .error-overlay.hidden { display: none; }
        .error-icon { font-size: 48px; margin-bottom: 16px; }
        .error-title { font-size: 20px; color: #e94560; margin-bottom: 8px; }
        .error-msg { font-size: 14px; color: #8892b0; max-width: 500px; text-align: center; line-height: 1.6; }
        .stats-panel {
            position: fixed; top: 56px; right: 12px;
            background: rgba(22, 33, 62, 0.95);
            border: 1px solid #0f3460; border-radius: 8px;
            padding: 12px 16px; font-size: 12px;
            z-index: 100; min-width: 180px; display: none;
        }
        .stats-panel.show { display: block; }
        .stats-title { font-size: 13px; font-weight: 600; color: #e94560;
            margin-bottom: 8px; padding-bottom: 6px;
            border-bottom: 1px solid #0f3460;
        }
        .stats-row { display: flex; justify-content: space-between; padding: 3px 0; }
        .stats-label { color: #8892b0; }
        .stats-value { color: #a8b2d1; font-weight: 500; }
        .goto-date {
            display: flex; align-items: center; gap: 6px;
        }
        .goto-date label { color: #8892b0; font-size: 12px; white-space: nowrap; }
        .goto-date input {
            width: 130px; padding: 4px 6px; font-size: 12px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #a8b2d1; outline: none;
            color-scheme: dark;
        }
        .goto-date input:focus { border-color: #e94560; }
        .goto-date button {
            padding: 4px 12px; font-size: 12px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #a8b2d1; cursor: pointer; transition: all 0.2s;
        }
        .goto-date button:hover { background: #0f3460; color: #e0e0e0; }
        .goto-date .date-arrow {
            font-size: 10px; color: #8892b0; cursor: pointer;
            padding: 2px 3px; user-select: none;
            transition: color 0.2s; line-height: 1;
        }
        .goto-date .date-arrow:hover { color: #e94560; }
        .goto-date .date-input-wrap {
            position: relative; display: inline-block;
        }
        .goto-date .date-weekday {
            position: absolute; right: 28px; top: 50%; transform: translateY(-50%);
            font-size: 11px; color: #a8b2d1; white-space: nowrap;
            pointer-events: none; user-select: none;
            padding: 0 2px;
        }
        .help-tip { position: fixed; bottom: 48px; right: 20px;
            font-size: 11px; color: #555; z-index: 100;
        }
        .scan-panel {
            position: fixed; bottom: 40px; left: 10px;
            width: 420px; max-height: calc(100vh - 120px);
            background: rgba(22, 33, 62, 0.97);
            border: 1px solid #0f3460; border-radius: 8px;
            z-index: 200; display: none;
            flex-direction: column; overflow: hidden;
            box-shadow: 0 4px 24px rgba(0,0,0,0.4);
        }
        .scan-panel.show { display: flex; }
        .scan-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 10px 14px; border-bottom: 1px solid #0f3460;
            background: rgba(15, 52, 96, 0.3);
        }
        .scan-title { font-size: 13px; font-weight: 600; color: #e94560; }
        .scan-status { font-size: 11px; color: #8892b0; }
        .scan-close {
            font-size: 18px; color: #8892b0; cursor: pointer;
            padding: 0 4px; line-height: 1;
        }
        .scan-close:hover { color: #e94560; }
        .scan-body {
            flex: 1; overflow-y: auto; padding: 8px;
            font-size: 12px;
        }
        .scan-empty {
            text-align: center; color: #555; padding: 40px 0;
        }
        .scan-loading {
            text-align: center; color: #8892b0; padding: 30px 0;
        }
        .scan-loading .spinner {
            display: inline-block; width: 20px; height: 20px;
            border: 2px solid #0f3460; border-top-color: #e94560;
            border-radius: 50%; animation: spin 0.8s linear infinite;
            margin-bottom: 8px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .scan-summary {
            padding: 6px 10px; margin-bottom: 6px;
            background: rgba(15, 52, 96, 0.3); border-radius: 4px;
            color: #a8b2d1; font-size: 11px;
        }
        .scan-summary b { color: #e94560; }
        .scan-stock-row {
            display: flex !important; align-items: center !important; padding: 4px 8px !important;
            border-radius: 4px !important; cursor: pointer !important;
            border-bottom: 1px solid rgba(15, 52, 96, 0.3) !important;
            flex-wrap: nowrap !important; overflow: hidden !important;
            width: 100% !important; box-sizing: border-box !important;
        }
        .scan-stock-row:hover { background: rgba(233, 69, 96, 0.1); }
        .scan-col-name {
            width: 64px !important; flex-shrink: 0 !important;
            text-align: left !important; overflow: hidden !important;
            text-overflow: ellipsis !important; white-space: nowrap !important;
            color: #8892b0 !important; font-weight: 400 !important;
        }
        .scan-col-code {
            width: 85px !important; flex-shrink: 0 !important;
            text-align: left !important; color: #8892b0 !important;
            overflow: hidden !important; text-overflow: ellipsis !important;
            white-space: nowrap !important; padding-left: 4px !important;
        }
        .scan-col-mv {
            width: 44px !important; flex-shrink: 0 !important;
            text-align: left !important; color: #8892b0 !important;
        }
        .scan-col-ma {
            width: 56px !important; flex-shrink: 0 !important;
            text-align: left !important; color: #8892b0 !important;
        }
        .scan-col-ann {
            flex: 1 1 auto !important; min-width: 0 !important;
            text-align: left !important; color: #a8b2d1 !important;
            overflow: hidden !important; text-overflow: ellipsis !important;
            white-space: nowrap !important; padding: 0 4px 0 4px !important;
        }
        .scan-col-tags {
            flex: 1 1 auto !important; min-width: 0 !important;
            text-align: right !important;
            display: flex !important; justify-content: flex-end !important;
            gap: 2px !important; flex-wrap: nowrap !important;
        }
        .scan-bsp-tags {
            display: inline-flex !important; gap: 2px !important;
            flex-shrink: 0 !important; flex-wrap: nowrap !important;
            white-space: nowrap !important;
            vertical-align: middle !important;
        }
        .scan-bsp-tag {
            display: inline-block !important;
            padding: 1px 3px !important; border-radius: 3px !important;
            font-size: 9px !important; font-weight: 600 !important;
            white-space: nowrap !important; flex-shrink: 0 !important;
            line-height: 1.2 !important;
        }
        .scan-bsp-tag.buy1 { background: rgba(233, 69, 96, 0.3); color: #FF6B8A; }
        .scan-bsp-tag.buy2 { background: rgba(255, 167, 16, 0.3); color: #FFA710; }
        .scan-bsp-tag.buy3 { background: rgba(12, 244, 155, 0.3); color: #0CF49B; }
        .scan-bsp-tag.buy1p { background: rgba(100, 149, 237, 0.3); color: #6495ED; }
        .scan-bsp-tag.buy2s { background: rgba(186, 85, 211, 0.3); color: #BA55D3; }
        .scan-bsp-tag.buy3b { background: rgba(255, 215, 0, 0.3); color: #FFD700; }
        .scan-bsp-tag.buya { background: rgba(233, 69, 96, 0.2); color: #e94560; }
        .scan-bsp-tag.sell1 { background: rgba(0, 180, 80, 0.3); color: #00B450; }
        .scan-bsp-tag.sell2 { background: rgba(0, 150, 136, 0.3); color: #009688; }
        .scan-bsp-tag.sell3 { background: rgba(76, 175, 80, 0.3); color: #4CAF50; }
        .scan-bsp-tag.sell1p { background: rgba(0, 200, 120, 0.3); color: #00C878; }
        .scan-bsp-tag.sell2s { background: rgba(0, 170, 100, 0.3); color: #00AA64; }
        .scan-bsp-tag.sell3b { background: rgba(100, 200, 130, 0.3); color: #64C882; }
        .scan-bsp-tag.sella { background: rgba(0, 200, 80, 0.2); color: #00C850; }
        .scan-no-result {
            text-align: center; color: #555; padding: 30px 0; font-size: 12px;
        }
        .range-slider {
            position: fixed; bottom: 0; left: 0; width: 100%; height: 32px;
            background: rgba(22, 33, 62, 0.92); z-index: 100;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            padding: 0 20px; box-sizing: border-box;
            border-top: 1px solid #0f3460;
        }
        .range-slider-track {
            position: relative; width: 100%; height: 6px;
            background: #2a2a4a; border-radius: 3px; cursor: pointer;
        }
        .range-slider-window {
            position: absolute; top: 0; height: 100%;
            background: rgba(65, 105, 225, 0.4); border-radius: 3px;
            min-width: 10px;
        }
        .range-slider-handle {
            position: absolute; top: -3px; width: 8px; height: 12px;
            background: #4169E1; border-radius: 2px; cursor: ew-resize;
            transition: background 0.15s;
        }
        .range-slider-handle:hover { background: #6495ED; }
        .range-slider-handle.left { left: -4px; }
        .range-slider-handle.right { right: -4px; }
        .range-slider-label {
            font-size: 11px; color: #a8b2d1; font-family: monospace;
            margin-top: 2px; white-space: nowrap; text-align: center;
            line-height: 1.4;
        }
        /* 双窗口模式样式 */
        #chart-top, #chart-bottom {
            width: 100%; position: relative; cursor: crosshair; overflow: hidden;
        }
        #chart-top { height: 50%; border-bottom: 2px solid #0f3460; }
        #chart-bottom { height: 50%; }
        #chart-top canvas, #chart-bottom canvas { display: block; }
        #chart-top.dual-active { outline: 2px solid rgba(233, 69, 96, 0.5); outline-offset: -2px; }
        #chart-bottom.dual-active { outline: 2px solid rgba(233, 69, 96, 0.5); outline-offset: -2px; }
        .dual-separator {
            height: 2px; background: #e94560; width: 100%;
        }
        #btn-dual.active { background: #e94560; border-color: #e94560; color: #fff; }
        /* 文字标注弹出菜单 */
        .annotation-menu {
            position: fixed; z-index: 9999; background: #16213e;
            border: 1px solid #0f3460; border-radius: 6px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.4); padding: 4px 0;
            min-width: 140px; display: none;
        }
        .annotation-menu.show { display: block; }
        .annotation-menu-item {
            padding: 6px 14px; font-size: 12px; color: #a8b2d1;
            cursor: pointer; white-space: nowrap;
        }
        .annotation-menu-item:hover { background: #0f3460; color: #e0e0e0; }
        .annotation-menu-item.danger { color: #e94560; }
        .annotation-menu-item.danger:hover { background: rgba(233,69,96,0.2); }
        .annotation-menu-divider {
            height: 1px; background: #0f3460; margin: 4px 0;
        }
        /* 标注输入对话框 */
        .annotation-dialog {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.5); z-index: 10000;
            display: none; align-items: center; justify-content: center;
        }
        .annotation-dialog.show { display: flex; }
        .annotation-dialog-box {
            background: #16213e; border: 1px solid #0f3460; border-radius: 8px;
            padding: 20px 24px; min-width: 320px; box-shadow: 0 8px 32px rgba(0,0,0,0.5);
        }
        .annotation-dialog-title {
            font-size: 14px; font-weight: 600; color: #e0e0e0; margin-bottom: 12px;
        }
        .annotation-dialog-date {
            font-size: 11px; color: #8892b0; margin-bottom: 12px;
        }
        .annotation-dialog-input {
            width: 100%; padding: 6px 10px; font-size: 13px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #e0e0e0; outline: none; margin-bottom: 14px;
            color-scheme: dark;
        }
        .annotation-dialog-input:focus { border-color: #e94560; }
        .annotation-dialog-btns {
            display: flex; justify-content: flex-end; gap: 8px;
        }
        .annotation-dialog-btn {
            padding: 5px 16px; border-radius: 4px; font-size: 12px;
            cursor: pointer; border: 1px solid #0f3460; background: #1a1a2e;
            color: #a8b2d1; transition: all 0.2s;
        }
        .annotation-dialog-btn:hover { background: #0f3460; color: #e0e0e0; }
        .annotation-dialog-btn.primary {
            background: #e94560; border-color: #e94560; color: #fff;
        }
        .annotation-dialog-btn.primary:hover { background: #d63850; }
        .annotation-list-item {
            font-size: 11px; color: #8892b0; padding: 2px 0;
            cursor: pointer; display: flex; justify-content: space-between;
            align-items: center;
        }
        .annotation-list-item:hover { color: #e94560; }
        .annotation-list-del {
            color: #555; font-size: 13px; cursor: pointer; padding: 0 4px;
        }
        .annotation-list-del:hover { color: #e94560; }
    </style>
</head>
<body>
    <div class="loading-overlay" id="loading">
        <div class="loading-spinner"></div>
        <div class="loading-text">正在加载K线数据...</div>
    </div>
    <div class="error-overlay hidden" id="error">
        <div class="error-icon">&#9888;</div>
        <div class="error-title">数据加载失败</div>
        <div class="error-msg" id="error-msg">
            数据加载失败，请检查网络连接或稍后重试。
        </div>
    </div>
    <div class="header">
        <div class="header-left">
            <div class="stock-input">
                <label>代码:</label>
                <div class="stock-input-wrap">
                    <input type="text" id="stock-code-input" placeholder="如 600519、szzs、贵州茅台" onkeydown="onInputKeydown(event)" onfocus="showHistory()" oninput="onInputChange()" />
                    <span class="stock-input-clear" id="input-clear" onclick="clearInput()" style="display:none">&times;</span>
                </div>
                <button onclick="loadStock()">查询</button>
                <div class="stock-history" id="stock-history"></div>
            </div>
            <span class="stock-name" id="stock-name">--</span>
            <span class="stock-code" id="stock-code">--</span>
            <span id="freq-selector">
                <button class="freq-btn" id="btn-w" onclick="switchFreq('w')">周K</button>
                <button class="freq-btn" id="btn-d" onclick="switchFreq('d')">日K</button>
                <button class="freq-btn" id="btn-30m" onclick="switchFreq('30m')">30分</button>
                <button class="freq-btn active" id="btn-5m" onclick="switchFreq('5m')">5分</button>
                <button class="freq-btn" id="btn-1m" onclick="switchFreq('1m')">1分</button>
                <button class="freq-btn" id="btn-15s" onclick="switchFreq('15s')">15秒</button>
            </span>
            <span class="realtime-badge" id="realtime-badge" title="实时推送中">● 实时</span>
        </div>
        <div class="header-right">
            <div class="goto-date">
                <span class="date-arrow" onclick="dateStep(-1)" title="前一天">&#9664;</span>
                <span class="date-input-wrap">
                    <input type="date" id="goto-date-input" min="1990-01-01" max="2099-12-31" onchange="handleDateChange()" onkeydown="handleDateKeydown(event)" onblur="handleDateBlur()" oninput="handleDateInput(event)" />
                    <span class="date-weekday" id="date-weekday"></span>
                </span>
                <span class="date-arrow" onclick="dateStep(1)" title="后一天">&#9654;</span>
            </div>
            <button class="btn" id="btn-dual" onclick="toggleDualWindow()">双窗口</button>
                <button class="btn" id="btn-fx" onclick="toggleOverlay('fx')">分型</button>
                <button class="btn active" id="btn-bi" onclick="toggleOverlay('bi')">笔</button>
                <button class="btn" id="btn-seg" onclick="toggleOverlay('seg')">线段</button>
                <button class="btn active" id="btn-zs" onclick="toggleOverlay('zs')">中枢</button>
                <button class="btn" id="btn-bsp" onclick="toggleOverlay('bsp')">买卖点</button>
            <button class="btn" id="btn-scan" onclick="startScanZxg()" title="扫描自选股买点">自选扫描</button>
                <button class="btn" id="btn-ma" onclick="toggleOverlay('ma')">均线</button>
            <button class="btn" id="btn-restart" disabled onclick="restartStock()" title="清除选点，按冷启动重新加载">重置</button>
            <button class="btn" id="btn-stats" onclick="toggleStats()">统计</button>
            <button class="btn-icon" id="btn-refresh" title="刷新GBBQ数据" onclick="refreshGbbq()">
                <svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.96 7.96 0 0012 4C7.58 4 4.01 7.58 4.01 12S7.58 20 12 20c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
            </button>
            <span id="refresh-status" style="color:#a8b2d1;font-size:11px;margin-left:4px;display:none;"></span>
            <button class="btn-icon" id="btn-settings" title="设置">
                <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.49.49 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 00-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1115.6 12 3.611 3.611 0 0112 15.6z"/></svg>
            </button>
        </div>
    </div>
    <div id="chart-container">
        <div class="crosshair-info" id="crosshair-info"></div>
    </div>
    <div class="range-slider" id="range-slider">
        <div class="range-slider-track" id="slider-track">
            <div class="range-slider-window" id="slider-window">
                <div class="range-slider-handle left" id="slider-handle-left"></div>
                <div class="range-slider-handle right" id="slider-handle-right"></div>
            </div>
        </div>
        <div class="range-slider-label" id="slider-label"></div>
    </div>
    <div class="help-tip">鼠标滚轮缩放 &middot; 拖拽平移 &middot; 悬停查看数据</div>
    <div class="stats-panel" id="stats-panel">
        <div class="stats-title">缠论统计</div>
        <div id="stats-content"></div>
    </div>
    <div class="scan-panel" id="scan-panel">
        <div class="scan-header">
            <span class="scan-title" id="scan-title">买卖点扫描</span>
            <span class="scan-status" id="scan-status"></span>
            <span class="scan-close" onclick="closeScanPanel()">&times;</span>
        </div>
        <div class="scan-body" id="scan-body">
            <div class="scan-empty">点击上方"自选扫描"按钮开始</div>
        </div>
    </div>
    <div class="redframe-debug" id="redframe-debug" style="position:fixed;bottom:10px;right:10px;background:#1a1a2e;color:#fff;border:1px solid #e94560;border-radius:4px;padding:6px 10px;font-size:11px;font-family:monospace;z-index:9999;display:none;max-width:320px;pointer-events:none;">
        <b style="color:#e94560;">红框调试</b> | <span id="rfdb-state">--</span>
        <span id="rfdb-detail"></span>
    </div>
    <!-- 文字标注右键菜单 -->
    <div class="annotation-menu" id="annotation-menu">
        <div class="annotation-menu-item" id="annotation-menu-edit-one" onclick="annotationEditAnnotation()" style="display:none;">修改标注</div>
        <div class="annotation-menu-item" id="annotation-menu-delete-one" onclick="annotationDeleteAnnotation()" style="display:none;">删除标注</div>
        <div class="annotation-menu-item" id="annotation-menu-add" onclick="annotationAdd()">添加标注</div>
        <div class="annotation-menu-divider" id="annotation-menu-divider"></div>
        <div class="annotation-menu-item danger" id="annotation-menu-del-all" onclick="annotationDeleteAllGlobal()">删除全部</div>
    </div>
    <!-- 文字标注输入对话框 -->
    <div class="annotation-dialog" id="annotation-dialog">
        <div class="annotation-dialog-box">
            <div class="annotation-dialog-title" id="annotation-dialog-title">添加文字标注</div>
            <div class="annotation-dialog-date" id="annotation-dialog-date"></div>
            <input class="annotation-dialog-input" id="annotation-dialog-input" type="text" placeholder="输入标注文字，如：支撑位、减仓点" maxlength="50" onkeydown="annotationDialogKeydown(event)" />
            <div class="annotation-dialog-btns">
                <button class="annotation-dialog-btn" onclick="annotationDialogCancel()">取消</button>
                <button class="annotation-dialog-btn primary" onclick="annotationDialogConfirm()">确定</button>
            </div>
        </div>
    </div>
    <!-- 自选扫描模式选择对话框 -->
    <div class="annotation-dialog" id="scan-mode-dialog">
        <div class="annotation-dialog-box">
            <div class="annotation-dialog-title">自选扫描</div>
            <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px;">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="radio" name="scan-mode" value="ann" checked style="accent-color:#e94560;" />
                    标注
                </label>
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="radio" name="scan-mode" value="bsp" style="accent-color:#e94560;" />
                    买/卖点
                </label>
            </div>
            <div class="annotation-dialog-btns">
                <button class="annotation-dialog-btn" onclick="scanModeDialogCancel()">取消</button>
                <button class="annotation-dialog-btn primary" onclick="scanModeDialogConfirm()">确认</button>
            </div>
        </div>
    </div>
    <script>
    (function() {
        "use strict";
        let chartData = null, canvas, ctx;
        let showBi = true, showFx = false, showMa = false, showZs = true, showSeg = false, showBsp = false;
        const PADDING = { top: 20, right: 22, bottom: 60, left: 10 };
        const VOL_RATIO = 0.2, GAP = 12;
        const MACD_TEXT_HEIGHT = 18;
        let viewOffset = 0, viewCount = 377;
        let isDragging = false, dragStartX = 0, dragStartOffset = 0;
        let mouseX = -1, mouseY = -1;
        let _currentClipText = "";
        let _mouseDownX = 0, _mouseDownY = 0;
        // 区间选择状态机: IDLE(空闲) | SELECTED_A(已选起点)
        let _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
        let _currentGlobalIdx = -1;
        let _overlayData = null;
        let initialized = false;
        let currentFreq = 'd'; // 当前周期: d=日K, 30m=30分钟
        let lastStockFreq = 'd';     // 股票上下文上次使用的周期（同类切换继承）
        let lastFuturesFreq = '15s'; // 期货上下文上次使用的周期（同类切换继承）
        // 双窗口状态
        let isDualWindow = false;
        let dualBottomData = null;
        let dualBottomFreq = '';
        let dualBottomViewOffset = 0, dualBottomViewCount = 377;
        let dualBottomMouseX = -1, dualBottomMouseY = -1;
        let topCanvas, topCtx, bottomCanvas, bottomCtx;
        let dualBottomIsDragging = false, dualBottomDragStartX = 0, dualBottomDragStartOffset = 0;
        let dualBottomMouseDownX = 0, dualBottomMouseDownY = 0; // 底部窗口点击坐标
        let _bottomCurrentGlobalIdx = -1; // 底部窗口当前鼠标指向的全局索引
        let _bottomClipText = ""; // 底部窗口当前K线信息文本
        let dualHighlightRange = null; // {startIdx, endIdx} 下面窗口高亮范围（灰框）
        let dualRedRange = null;     // {beforeStart, beforeEnd, afterStart, afterEnd} 下面窗口红框范围
        let dualOffscreenState = false; // 状态A：当前鼠标指向的K线对应区间在下面窗口视口外
        let dualNewZsData = null;       // 双窗口新模式：红框内笔计算的新中枢数据 {zs: [...], zs_stars: [...]}
        let dualShowNewZs = false;      // 双窗口新模式：是否绘制新中枢（替代原线段/中枢/买卖点）
        let dualNewZsStartBi = -1;      // 双窗口新模式：上次请求的起始笔索引（用于去重）
        let dualNewZsEndBi = -1;        // 双窗口新模式：上次请求的结束笔索引（用于去重）
        let activeDualWindow = 'top';   // 当前激活的窗口：'top' 或 'bottom'，控制底部滚动条作用于哪个窗口

        // 文字标注状态
        let annotations = [];          // 当前标注列表: [{date, text, y_offset}]
        let _annotationTargetDate = ""; // 右键点击的K线日期
        let _annotationTargetY = 0;     // 右键点击的Y坐标（图表内相对坐标，用于标注定位）
        let _annotationTargetX = 0;     // 右键点击的X坐标（用于菜单定位）
        let _annotationClickTarget = null; // 右键点击命中的标注对象 {date, text, y_offset}，null表示未命中
        let _annotationEditOldText = "";   // 编辑模式下被修改的旧文字
        let _annotationDialogMode = "add"; // "add" 或 "edit"

        // 实时模式（期货/期指 SSE 推送）
        let isRealtimeMode = false;       // 是否处于实时模式
        let realtimeSymbol = null;        // 实时模式下当前品种代码
        let realtimeFreq = null;          // 实时模式下当前周期
        let realtimeStartTime = null;     // 实时模式下选点起始时间
        let realtimeEventSource = null;   // SSE EventSource 对象
        let reconnectTimer = null;          // 重连定时器（防止 onerror 多次触发导致重复连接）
        let reconnectCount = 0;            // 重连次数计数
        const MAX_RECONNECT = 3;           // 最大重连次数
        let realtimeStopped = false;       // 彻底放弃重连标志（阻止 onerror 死循环）
        let realtimeConnected = false;    // SSE 是否已连接

        // 辅助函数：30分钟K线显示时间
        function getKlineEndTime(dateStr, showSeconds) {
            const parts = dateStr.split(/[-\s:]/);
            const yy = parts[0].slice(2);
            const mm = parts[1];
            const dd = parts[2];
            const hh = parts[3];
            const min = parts[4];
            const ss = parts[5];
            if (showSeconds && ss !== undefined) {
                return `${yy}-${mm}-${dd} ${hh}:${min}:${ss}`;
            }
            return `${yy}-${mm}-${dd} ${hh}:${min}`;
        }
        // 双窗口：上面周期 -> 下面周期映射
        function getDualBottomFreq(topFreq) {
            if (topFreq === 'w') return 'd';
            if (topFreq === 'd') return '30m';
            if (topFreq === '30m') return '5m';
            return null; // 5m无对应
        }
        // 双窗口：获取上面窗口某根K线对应的时间区间
        function getTopKlineTimeRange(kline, topFreq) {
            // 返回 {start: Date, end: Date}
            const dateStr = kline.date;
            const d = new Date(dateStr.replace(" ", "T"));
            if (topFreq === 'w') {
                // 周K：该周K覆盖周一到周五
                const day = d.getDay();
                const monday = new Date(d);
                monday.setDate(d.getDate() - (day === 0 ? 6 : day - 1));
                monday.setHours(0, 0, 0, 0);
                const friday = new Date(monday);
                friday.setDate(monday.getDate() + 4);
                friday.setHours(23, 59, 59, 999);
                return { start: monday, end: friday };
            } else if (topFreq === 'd') {
                // 日K：整天
                const start = new Date(d); start.setHours(0, 0, 0, 0);
                const end = new Date(d); end.setHours(23, 59, 59, 999);
                return { start, end };
            } else if (topFreq === '30m') {
                // 30分K：该30分钟区间（前后各15分钟作为匹配范围）
                const start = new Date(d); start.setMinutes(d.getMinutes() - 15);
                const end = new Date(d); end.setMinutes(d.getMinutes() + 14, 59, 999);
                return { start, end };
            }
            return null;
        }
        // 双窗口：根据上面窗口鼠标位置计算下面窗口高亮范围
        function calcDualHighlight(topMouseX) {
            if (!isDualWindow || !dualBottomData || !chartData) return null;
            const area = getChartArea();
            const klines = getVisibleKlines();
            if (!klines.length) return null;
            const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
            const barStep = area.w / effectiveCount;
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            const idx = Math.floor((topMouseX - area.x + subPixelOffset) / barStep);
            if (idx < 0 || idx >= klines.length) return null;
            const topKline = klines[idx];
            const timeRange = getTopKlineTimeRange(topKline, currentFreq);
            if (!timeRange) return null;
            // 在下面窗口数据中找到时间范围内的K线
            const bottomKlines = dualBottomData.klines;
            let startIdx = -1, endIdx = -1;
            for (let i = 0; i < bottomKlines.length; i++) {
                const bk = bottomKlines[i];
                const bd = new Date(bk.date.replace(" ", "T"));
                if (bd >= timeRange.start && bd <= timeRange.end) {
                    if (startIdx === -1) startIdx = i;
                    endIdx = i;
                }
            }
            // 下面窗口数据中没有匹配的K线（上面K线日期超出了下面数据范围）
            if (startIdx === -1) {
                // 用上面K线日期与下面数据首尾日期比较来判断方向
                const topDate = new Date(topKline.date.replace(" ", "T"));
                const bottomFirstDate = new Date(bottomKlines[0].date.replace(" ", "T"));
                const bottomLastDate = new Date(bottomKlines[bottomKlines.length - 1].date.replace(" ", "T"));
                if (topDate < bottomFirstDate) {
                    return { startIdx: -1, endIdx: -1, isVisible: false, isLeft: true, isRight: false };
                } else if (topDate > bottomLastDate) {
                    return { startIdx: -1, endIdx: -1, isVisible: false, isLeft: false, isRight: true };
                }
                return null;
            }
            // 判断高亮范围是否在下面窗口当前视口内
            const bottomGlobalStart = Math.max(0, Math.floor(dualBottomViewOffset));
            const bottomGlobalEnd = bottomGlobalStart + dualBottomViewCount;
            const isVisible = (startIdx < bottomGlobalEnd && endIdx >= bottomGlobalStart);
            const isLeft = endIdx < bottomGlobalStart;   // 整个区间在视口左边
            const isRight = startIdx >= bottomGlobalEnd;
            let redRange = null;
            try {
                redRange = calcRedRange(topKline, bottomKlines, startIdx, endIdx);
            } catch (e) {
                console.error("[红框] calcRedRange异常:", e);
                window._lastCalcRedRangeError = String(e);
            }
            return { startIdx, endIdx, isVisible, isLeft, isRight, redRange };
        }

        // 双窗口红框：鼠标指向上面K线所属笔的外沿区间（分型左肩→右肩）
        // 注意：使用 chartData.bis（复数），JSON 字段名是 "bis"
        function calcRedRange(topKline, bottomKlines, grayStart, grayEnd) {
            console.log("[红框] === 进入 calcRedRange ===");
            console.log("[红框] topKline.date=" + topKline.date + " grayStart=" + grayStart + " grayEnd=" + grayEnd);
            console.log("[红框] chartData.bis=" + (chartData && chartData.bis ? "长度" + chartData.bis.length : "null"));
            console.log("[红框] bottomKlines.length=" + bottomKlines.length);
            if (!chartData || !chartData.bis || !chartData.bis.length) {
                console.log("[红框] ❌ chartData 或 chartData.bis 为空，返回null");
                window._lastRedFrameStatus = { state: "SKIP", reason: "chartData或bis为空" };
                updateRedFrameDebug();
                return null;
            }
            const d = topKline.date;
            let bi = null;
            // 找到topKline所属的笔（交界处归属右边）
            for (let i = 0; i < chartData.bis.length; i++) {
                const b = chartData.bis[i];
                if (d >= b.sdt && d < b.edt) { bi = b; console.log("[红框] 找到笔(主循环): idx=" + i + " sdt=" + b.sdt + " edt=" + b.edt + " dir=" + b.direction); break; }
            }
            if (!bi) {
                for (let i = chartData.bis.length - 1; i >= 0; i--) {
                    if (d === chartData.bis[i].edt) { bi = chartData.bis[i]; console.log("[红框] 找到笔(edt匹配): idx=" + i + " sdt=" + chartData.bis[i].sdt + " edt=" + chartData.bis[i].edt); break; }
                }
            }
            if (!bi) {
                console.log("[红框] ❌ 未找到所属笔，topKline.date=" + d + " 不在任何笔的[sdt, edt)范围内");
                window._lastRedFrameStatus = { state: "SKIP", reason: "未找到所属笔", topDate: d, biCount: chartData.bis.length };
                updateRedFrameDebug();
                return null;
            }
            if (!bi.fx_a_raw_dt || !bi.fx_b_raw_dt) {
                console.log("[红框] ❌ bi.fx_a_raw_dt='" + (bi.fx_a_raw_dt || "") + "' fx_b_raw_dt='" + (bi.fx_b_raw_dt || "") + "' 为空");
                console.log("[红框]    bi.sdt=" + bi.sdt + " bi.edt=" + bi.edt + " bi.direction=" + bi.direction);
                window._lastRedFrameStatus = { state: "SKIP", reason: "fx_a或fx_b为空", sdt: bi.sdt, edt: bi.edt };
                updateRedFrameDebug();
                return null;
            }
            const aDt = bi.fx_a_raw_dt, bDt = bi.fx_b_raw_dt;
            // 上面窗口周期可能和下面不同，日期格式长度不同（日K:10位，30分:16位）
            const aLen = aDt.length, bLen = bDt.length;
            let aIdx = -1, bIdx = -1;
            const bottomFirstDate = bottomKlines[0].date.slice(0, aLen);
            const bottomLastDate = bottomKlines[bottomKlines.length - 1].date.slice(0, bLen);
            for (let i = 0; i < bottomKlines.length; i++) {
                const bk = bottomKlines[i];
                // A: 左肩是分型前一根K线，红框从分型开始（即左肩之后），所以用 > 严格大于
                // 这样做自然处理休市：如果 aDt 是周五，> 之后自然跳到下周一
                if (aIdx === -1 && bk.date.slice(0, aLen) > aDt) aIdx = i;
                // B: 红框包含右肩，取 <= bDt 的最后一根
                if (bk.date.slice(0, bLen) <= bDt) bIdx = i;
            }
            // 参照灰框处理：笔区间完全在底部数据范围之外 → 不显示红框，返回null
            if (aIdx === -1 && bIdx === -1) {
                if (aDt > bottomLastDate) {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间在底部数据右侧", aDt: aDt, bottomLast: bottomLastDate };
                } else if (bDt < bottomFirstDate) {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间在底部数据左侧", bDt: bDt, bottomFirst: bottomFirstDate };
                } else {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间无匹配", aDt: aDt, bDt: bDt };
                }
                updateRedFrameDebug();
                return null;
            }
            // 部分重叠：aIdx 或 bIdx 为 -1 时，截断到可见范围
            if (aIdx === -1) aIdx = 0;
            if (bIdx === -1) bIdx = bottomKlines.length - 1;
            if (aIdx > bIdx) {
                window._lastRedFrameStatus = { state: "SKIP", reason: "aIdx>bIdx", aIdx: aIdx, bIdx: bIdx };
                updateRedFrameDebug();
                return null;
            }
            // 红框时间：使用下方K线时间（精确到分钟），确保30m/5m图表显示完整时间
            const leftDate = bottomKlines[aIdx].date;
            const rightDate = bottomKlines[bIdx].date;
            // before: 笔区间在灰框之前的部分 [aIdx, grayStart-1]
            const beforeStart = aIdx, beforeEnd = Math.min(grayStart - 1, bIdx);
            // after: 笔区间在灰框之后的部分 [grayEnd+1, bIdx]
            const afterStart = grayEnd + 1, afterEnd = bIdx;
            const result = {
                beforeStart, beforeEnd,
                afterStart, afterEnd,
                hasBefore: (beforeEnd >= beforeStart),
                hasAfter: (afterEnd >= afterStart),
                leftDate: leftDate,    // 红框左边沿K线时间（下方窗口，精确到分钟）
                rightDate: rightDate,  // 红框右边沿K线时间
                aIdx: aIdx,            // 红框整体左边界（下方窗口全局索引）
                bIdx: bIdx,            // 红框整体右边界（下方窗口全局索引）
            };
            console.log("[红框] ✅ 返回红框范围: before=[" + beforeStart + "," + beforeEnd + "] hasBefore=" + result.hasBefore + " after=[" + afterStart + "," + afterEnd + "] hasAfter=" + result.hasAfter);
            window._lastRedFrameStatus = { state: "OK", reason: "calcRedRange成功", before: result.hasBefore, after: result.hasAfter, aIdx: aIdx, bIdx: bIdx, grayStart: grayStart, grayEnd: grayEnd, leftDate: result.leftDate, rightDate: result.rightDate };
            updateRedFrameDebug();
            return result;
        }
        const COLORS = {
            bg: "#1a1a2e", grid: "rgba(255,255,255,0.04)", text: "#8892b0", textLight: "#a8b2d1",
            up: "#FF4444", down: "#00DD00", bi: "#FFD700",
            crosshair: "rgba(255,255,255,0.3)",
            macdUp: "rgba(253,16,80,0.6)", macdDown: "rgba(12,244,155,0.6)", // 原值: macdUp="rgba(255,68,68,0.6)", macdDown="rgba(0,221,0,0.6)"
            dif: "#FFFFFF", dea: "#ffa710", // 原值: dea="#FFD700"
        };

        // 根据市场类型更新频率按钮的启用/禁用状态
        function updateFreqButtonStates(isFutures) {
            // 股票禁用 1m/15s，期货禁用 d/w
            document.getElementById('btn-d').disabled = isFutures;
            document.getElementById('btn-w').disabled = isFutures;
            document.getElementById('btn-1m').disabled = !isFutures;
            document.getElementById('btn-15s').disabled = !isFutures;
            // 共享周期始终启用
            document.getElementById('btn-30m').disabled = false;
            document.getElementById('btn-5m').disabled = false;
            // 同步 active 状态
            document.querySelectorAll('.freq-btn').forEach(b => b.classList.remove('active'));
            const activeBtn = document.getElementById('btn-' + currentFreq);
            if (activeBtn) activeBtn.classList.add('active');
        }

        async function init() {
            try {
                chartData = %%CHART_DATA%%;
                document.getElementById("stock-name").textContent = chartData.meta.name;
                document.getElementById("stock-code").textContent = chartData.meta.symbol;
                document.title = "缠论分析 - " + chartData.meta.name;
                initCanvas();
                updateSlider();
                // 根据数据中的 freq 自动识别周期
                if (chartData.meta.freq === "5分钟") {
                    currentFreq = "5m";
                } else if (chartData.meta.freq === "30分钟") {
                    currentFreq = "30m";
                } else if (chartData.meta.freq === "周线") {
                    currentFreq = "w";
                } else {
                    currentFreq = "d";
                }
                lastStockFreq = currentFreq; // 记录初始股票周期
                updateFreqButtonStates(false); // 初始页面为股票，禁用 1m/15s
                viewCount = 377;
                  adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                showFx = false;
                showMa = false;
                showZs = true;
                viewOffset = Math.max(0, chartData.klines.length - viewCount);
                // K线不足一屏时右对齐：确保最后一根K线紧贴右纵坐标
                if (chartData.klines.length < viewCount) {
                    viewOffset = 0;
                }
                initialized = true;
                updateRestartBtn();
                updateDualBtn();
                const lastDate = chartData.klines[chartData.klines.length - 1].date.slice(0, 10);
                document.getElementById("goto-date-input").value = lastDate;
                updateWeekday();
                render();
                document.getElementById("loading").classList.add("hidden");
                generateStats();
                loadAnnotations();
            } catch (err) {
                console.error("初始化失败:", err);
                document.getElementById("loading").classList.add("hidden");
                document.getElementById("error").classList.remove("hidden");
            }
        }

        function initCanvas() {
            const container = document.getElementById("chart-container");
            canvas = document.createElement("canvas");
            container.appendChild(canvas); ctx = canvas.getContext("2d");
            topCanvas = canvas; topCtx = ctx;
            resizeCanvas();
            window.addEventListener("resize", () => { resizeCanvas(); render(); });
            // 上面窗口事件
            canvas.addEventListener("wheel", onWheel, { passive: false });
            canvas.addEventListener("mousedown", onMouseDown);
            canvas.addEventListener("mousemove", onMouseMove);
            canvas.addEventListener("mouseup", onMouseUp);
            canvas.addEventListener("mouseleave", onMouseLeave);
            canvas.addEventListener("contextmenu", onContextMenu);
            canvas.addEventListener("dblclick", function(e) {
                if (!chartData) return;
                const rect = canvas.getBoundingClientRect();
                const clickX = e.clientX - rect.left;
                const clickY = e.clientY - rect.top;
                const area = getChartArea();
                // 1. 只在K线主图区域内有效
                if (clickX < area.x || clickX > area.x + area.w ||
                    clickY < area.y || clickY > area.y + area.h) {
                    return;
                }
                // 2. 计算当前可见K线和参数
                const klines = getVisibleKlines();
                if (!klines.length) return;
                const priceRange = getPriceRange(klines);
                const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
                const barStep = area.w / effectiveCount;
                const barWidth = Math.max(1, barStep * 0.7);
                const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
                // 3. 检查是否落在任何K线的[high,low]矩形内，同时检查是否是笔交汇点（分型）
                let clickedOnKline = false;
                let clickedBiIdx = -1;
                for (let i = 0; i < klines.length; i++) {
                    const k = klines[i];
                    const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                    const highY = priceToY(k.high, area, priceRange);
                    const lowY = priceToY(k.low, area, priceRange);
                    const halfW = barWidth / 2;
                    if (clickX >= x - halfW && clickX <= x + halfW &&
                        clickY >= highY && clickY <= lowY) {
                        clickedOnKline = true;
                        // 通过笔数据判断交汇点：双击K线日期 == 某笔edt == 下一笔sdt
                        const globalStart = Math.max(0, Math.floor(viewOffset));
                        const globalIdx = globalStart + i;
                        const kline = chartData.klines[globalIdx];
                        if (kline) {
                            let dateStr = kline.date;
                            for (let j = 0; j < chartData.bis.length - 1; j++) {
                                if (chartData.bis[j].edt === dateStr && chartData.bis[j + 1].sdt === dateStr) {
                                    clickedBiIdx = j + 1;
                                    break;
                                }
                            }
                        }
                        break;
                    }
                }
                // 复盘模式下不支持双击选点（在K线检测之后判断，确保只对K线上的双击弹提示）
                if (chartData.meta && chartData.meta.is_replay && clickedOnKline) {
                    showDualToast("复盘模式，不支持选点");
                    return;
                }
                // 4. 如果双击落在分型K线上且找到对应笔，手选进入段
                if (clickedBiIdx >= 0) {
                    const code = chartData.meta.symbol;
                    const freq = currentFreq;
                    const isFutures = chartData.meta.market === 'futures';
                    document.getElementById("loading").classList.remove("hidden");
                    document.querySelector(".loading-text").textContent = "正在手选进入段...";
                    const apiPath = isFutures
                        ? "/api/futures_manual_zs?symbol=" + encodeURIComponent(code) + "&freq=" + freq + "&bi_idx=" + clickedBiIdx
                        : "/api/manual_zs?code=" + encodeURIComponent(code) + "&freq=" + freq + "&bi_idx=" + clickedBiIdx;
                    fetch(apiPath)
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "手选失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            // 检查后端返回的错误
                            if (data.error) {
                                throw new Error(data.error);
                            }
                            // 期货：断开旧SSE，从选点时间重新连接
                            if (isFutures) {
                                chartData = data;
                                adjustViewForSavedPoint();
                                document.getElementById("stock-name").textContent = chartData.meta.name;
                                document.getElementById("stock-code").textContent = chartData.meta.symbol;
                                document.title = "缠论分析 - " + chartData.meta.name;
                                if (chartData.klines.length > 0) {
                                    const lastDate = chartData.klines[chartData.klines.length - 1].date.slice(0, 10);
                                    document.getElementById("goto-date-input").value = lastDate;
                                }
                                updateWeekday();
                                document.getElementById("loading").classList.add("hidden");
                                document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                                updateRestartBtn();
                                updateDualBtn();
                                resizeCanvas();
                                render();
                                generateStats();
                                loadAnnotations();
                                // 重连SSE，带上选点时间
                                const savedDate = chartData.meta.saved_selection_date;
                                connectRealtimeInit(code, freq, savedDate);
                                return;
                            }
                            // data 现在是完整的 chartData JSON（CChanB 从T重新计算的结果）
                            // 全文替换 chartData
                            chartData = data;
                            // 根据数据中的 freq 自动识别周期
                            if (chartData.meta.freq === "5分钟") {
                                currentFreq = "5m";
                            } else if (chartData.meta.freq === "30分钟") {
                                currentFreq = "30m";
                            } else if (chartData.meta.freq === "周线") {
                                currentFreq = "w";
                            } else {
                                currentFreq = "d";
                            }
                            // 同步按钮状态
                            document.getElementById("btn-d").classList.toggle("active", currentFreq === "d");
                            document.getElementById("btn-w").classList.toggle("active", currentFreq === "w");
                            document.getElementById("btn-30m").classList.toggle("active", currentFreq === "30m");
                            document.getElementById("btn-5m").classList.toggle("active", currentFreq === "5m");
                            // 重置视图：选点后klines只含选点之后的K线，直接全部显示
                            adjustViewForSavedPoint();
                            // 更新DOM
                            document.getElementById("stock-name").textContent = chartData.meta.name;
                            document.getElementById("stock-code").textContent = chartData.meta.symbol;
                            document.title = "缠论分析 - " + chartData.meta.name;
                            const lastDate = chartData.klines[chartData.klines.length - 1].date.slice(0, 10);
                            document.getElementById("goto-date-input").value = lastDate;
                            updateWeekday();
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                            updateRestartBtn();
                            updateDualBtn();
                            resizeCanvas();
                            render();
                            generateStats();
                            loadAnnotations();
                        })
                        .catch(err => {
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                            setTimeout(() => {
                                alert(err.message);
                            }, 50);
                        });
                    return;
                }
                // 5. 如果双击落在K线上但不是分型，无效
                if (clickedOnKline) {
                    return;
                }
                // 6. 双击空白处
                if (isDualWindow && dualOffscreenState && dualHighlightRange && dualBottomData) {
                    // 状态A：让下面窗口平移到对应区间
                    const hr = dualHighlightRange;
                    if (hr.startIdx >= 0 && hr.endIdx >= 0) {
                        const centerIdx = (hr.startIdx + hr.endIdx) / 2;
                        const totalKlines = dualBottomData.klines.length;
                        let newOffset = Math.round(centerIdx - dualBottomViewCount / 2);
                        // 左边不够：左对齐
                        if (newOffset < 0) newOffset = 0;
                        // 右边不够：右对齐（最后一根K线贴右边缘）
                        const maxOffset = Math.max(0, totalKlines - dualBottomViewCount);
                        if (newOffset > maxOffset) newOffset = maxOffset;
                        dualBottomViewOffset = newOffset;
                        // 重新计算高亮范围（区间已移入视口，应该变为isVisible=true）
                        dualHighlightRange = calcDualHighlight(mouseX);
                        dualRedRange = dualHighlightRange ? dualHighlightRange.redRange : null;
                        dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                        renderBottom();
                    } else {
                        // startIdx === -1（下面窗口无对应K线数据）
                        showDualToast("请加载更多K线...");
                    }
                    return;
                }
                // 7. 默认：恢复全视图
                viewCount = 377;
                viewOffset = Math.max(0, chartData.klines.length - viewCount);
                const lastDate = chartData.klines[chartData.klines.length - 1].date.slice(0, 10);
                document.getElementById("goto-date-input").value = lastDate;
                updateWeekday();
                render();
            });
        }

        function resizeCanvas() {
            const container = document.getElementById("chart-container");
            const dpr = window.devicePixelRatio || 1;
            if (isDualWindow) {
                // 双窗口模式：分别调整两个canvas
                const w = container.clientWidth;
                const hTop = container.clientHeight / 2;
                const hBottom = container.clientHeight / 2;
                if (topCanvas) {
                    topCanvas.width = w * dpr; topCanvas.height = hTop * dpr;
                    topCanvas.style.width = w + "px"; topCanvas.style.height = hTop + "px";
                    topCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
                }
                if (bottomCanvas) {
                    bottomCanvas.width = w * dpr; bottomCanvas.height = hBottom * dpr;
                    bottomCanvas.style.width = w + "px"; bottomCanvas.style.height = hBottom + "px";
                    bottomCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
                }
            } else {
                // 单窗口模式
                const w = container.clientWidth, h = container.clientHeight;
                canvas.width = w * dpr; canvas.height = h * dpr;
                canvas.style.width = w + "px"; canvas.style.height = h + "px";
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            }
        }

        function getChartArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top, w: totalW - rightGap, h: chartH };
        }

        function getVolArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalMacdH = (h - PADDING.top - PADDING.bottom - GAP) * VOL_RATIO;
            const macdChartH = totalMacdH - MACD_TEXT_HEIGHT;
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top + chartH + GAP + MACD_TEXT_HEIGHT,
                     w: totalW - rightGap, h: macdChartH };
        }

        function getMacdTextArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top + chartH + GAP,
                     w: totalW - rightGap, h: MACD_TEXT_HEIGHT };
        }

        function getVisibleKlines() {
            if (!chartData) return [];
            const start = Math.max(0, Math.floor(viewOffset));
            const end = Math.min(chartData.klines.length, start + viewCount + 2);
            const result = chartData.klines.slice(start, end);
            // 周K：返回全部K线，确保铺满整个画布
            if (currentFreq === 'w' && result.length < viewCount) {
                return result;
            }
            return result;
        }

        function getPriceRange(klines) {
            if (!klines.length) return { min: 0, max: 100 };
            let min = Infinity, max = -Infinity;
            klines.forEach(k => { if (k.low < min) min = k.low; if (k.high > max) max = k.high; });
            const margin = (max - min) * 0.05;
            return { min: min - margin, max: max + margin };
        }

        function getMacdRange(klines) {
            if (!klines.length) return { min: -1, max: 1 };
            let min = Infinity, max = -Infinity;
            klines.forEach(k => {
                if (k.macd < min) min = k.macd;
                if (k.macd > max) max = k.macd;
                if (k.dif < min) min = k.dif;
                if (k.dif > max) max = k.dif;
                if (k.dea < min) min = k.dea;
                if (k.dea > max) max = k.dea;
            });
            const margin = Math.max(Math.abs(max), Math.abs(min)) * 0.1;
            return { min: min - margin, max: max + margin };
        }

        function priceToY(price, area, priceRange) {
            return area.y + area.h - (price - priceRange.min) / (priceRange.max - priceRange.min) * area.h;
        }

        function yToPrice(y, area, priceRange) {
            return priceRange.min + (area.y + area.h - y) / area.h * (priceRange.max - priceRange.min);
        }

        /**
         * 构建全局日期→全局索引映射（chartData.klines 级别）。
         * 所有需要通过日期查找K线索引的 draw 函数统一使用此映射，
         * 避免因视口滚动导致局部 klines 子数组中找不到日期而丢失绘制。
         */
        function buildGlobalDateMap() {
            const dateToGlobalIdx = {};
            chartData.klines.forEach((k, i) => { dateToGlobalIdx[k.date] = i; });
            return { dateToGlobalIdx };
        }

        /**
         * 通过日期查找全局索引。
         * @param {string} date - 日期字符串
         * @param {object} map - buildGlobalDateMap() 的返回值
         * @returns {number|undefined} 全局索引
         */
        function dateToGlobalIdx(date, map) {
            return map.dateToGlobalIdx[date];
        }

        /**
         * 将全局索引转换为画布上的 X 坐标。
         * @param {number} globalIdx - 在 chartData.klines 中的全局索引
         * @param {number} globalStart - 当前视口起始的全局索引
         * @param {number} areaX - 图表区域左边界
         * @param {number} barStep - 每根K线的像素步长
         * @param {number} subPixelOffset - 亚像素偏移
         * @returns {number} 画布 X 坐标
         */
        function globalIdxToX(globalIdx, globalStart, areaX, barStep, subPixelOffset) {
            const localIdx = globalIdx - globalStart;
            return areaX + barStep * localIdx + barStep / 2 - subPixelOffset;
        }

        function render() {
            if (!chartData) return;
            if (isDualWindow) {
                renderTop(); // renderTop内部会调用updateDualHighlight -> renderBottom
            } else {
                renderSingle();
            }
        }

        function renderSingle() {
            if (!chartData || !ctx) return;
            canvas = topCanvas; ctx = topCtx;
            _renderChart(chartData, currentFreq, viewOffset, viewCount, mouseX, mouseY, null, null);
        }

        function renderTop() {
            if (!chartData || !topCtx) return;
            canvas = topCanvas; ctx = topCtx;
            updateActiveWindowClass();
            _renderChart(chartData, currentFreq, viewOffset, viewCount, mouseX, mouseY, null, null);
            // 上面窗口渲染完后，计算下面窗口高亮并重绘下面窗口
            // 注意：_renderChart 内部会临时覆盖全局变量然后恢复，
            // 所以这里全局变量已恢复为上面窗口的值，calcDualHighlight 可以正确使用
            updateDualHighlight();
        }

        function renderBottom() {
            if (!dualBottomData || !bottomCtx) return;
            updateDualNewZs();  // 双窗口新模式：检查红框完整性，决定是否请求新中枢
            updateActiveWindowClass();
            const _savedCanvas = canvas, _savedCtx = ctx;
            canvas = bottomCanvas; ctx = bottomCtx;
            window._isRenderingBottom = true;  // 标记：下面窗口渲染中，drawCrosshair 不更新 OHLC
            _renderChart(dualBottomData, dualBottomFreq, dualBottomViewOffset, dualBottomViewCount, dualBottomMouseX, dualBottomMouseY, dualHighlightRange, dualRedRange);
            window._isRenderingBottom = false;
            canvas = _savedCanvas; ctx = _savedCtx;
        }

        function _renderChart(data, freq, vOffset, vCount, mX, mY, highlightRange, redRange) {
            if (!data || !ctx) return;
            // 临时覆盖全局变量供绘制函数使用
            const _savedViewOffset = viewOffset, _savedViewCount = viewCount;
            const _savedMouseX = mouseX, _savedMouseY = mouseY;
            const _savedCurrentFreq = currentFreq;
            const _savedChartData = chartData;
            viewOffset = vOffset; viewCount = vCount;
            mouseX = mX; mouseY = mY;
            currentFreq = freq;
            chartData = data;
            const w = canvas.clientWidth, h = canvas.clientHeight;
            ctx.fillStyle = COLORS.bg; ctx.fillRect(0, 0, w, h);
            const klines = getVisibleKlines();
            if (!klines.length) {
                viewOffset = _savedViewOffset; viewCount = _savedViewCount;
                mouseX = _savedMouseX; mouseY = _savedMouseY;
                currentFreq = _savedCurrentFreq;
                chartData = _savedChartData;
                return;
            }
            const area = getChartArea(), volArea = getVolArea();
            const macdTextArea = getMacdTextArea();
            const priceRange = getPriceRange(klines), macdRange = getMacdRange(klines);
            const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
            const barWidth = Math.max(1, (area.w / effectiveCount) * 0.7);
            const barStep = area.w / effectiveCount;
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            // 双窗口红框：笔外沿区间（分型左肩→右肩，跳过中间灰框部分）
            // 调试：记录到全局状态供侧边调试面板读取（保留 calcRedRange 之前设置的原因）
            var _prevReason = window._lastRedFrameStatus ? window._lastRedFrameStatus.reason : undefined;
            window._lastRedFrameStatus = { redRange: !!redRange, highlightRange: !!highlightRange, isVisible: highlightRange ? highlightRange.isVisible : null };
            if (_prevReason) window._lastRedFrameStatus.reason = _prevReason;
            updateRedFrameDebug();
            if (redRange && highlightRange && highlightRange.isVisible) {
                window._lastRedFrameStatus.state = "DRAW";
                window._lastRedFrameStatus.leftDate = redRange.leftDate || "";
                window._lastRedFrameStatus.rightDate = redRange.rightDate || "";
                const globalStart = Math.max(0, Math.floor(viewOffset));
                const rFill = "rgba(220, 50, 50, 0.12)";  // 与红中枢同色
                if (redRange.hasBefore) {
                    const bx1 = globalIdxToX(redRange.beforeStart, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                    const bx2 = globalIdxToX(redRange.beforeEnd, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                    ctx.fillStyle = rFill; ctx.fillRect(bx1, area.y, bx2 - bx1, area.h);
                    window._lastRedFrameStatus.beforeDrawn = true;
                    window._lastRedFrameStatus.beforeRect = [bx1.toFixed(0), bx2.toFixed(0)];
                }
                if (redRange.hasAfter) {
                    const ax1 = globalIdxToX(redRange.afterStart, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                    const ax2 = globalIdxToX(redRange.afterEnd, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                    ctx.fillStyle = rFill; ctx.fillRect(ax1, area.y, ax2 - ax1, area.h);
                    window._lastRedFrameStatus.afterDrawn = true;
                    window._lastRedFrameStatus.afterRect = [ax1.toFixed(0), ax2.toFixed(0)];
                }
                updateRedFrameDebug();
            } else {
                // 保留 calcRedRange 给出的原因（如果有），不覆盖
                if (!window._lastRedFrameStatus || !window._lastRedFrameStatus.reason) {
                    window._lastRedFrameStatus = window._lastRedFrameStatus || {};
                    window._lastRedFrameStatus.reason = "渲染跳过(redRange或visibility)";
                }
                window._lastRedFrameStatus.state = "SKIP";
                window._lastRedFrameStatus.redRange = !!redRange;
                window._lastRedFrameStatus.highlightRange = !!highlightRange;
                window._lastRedFrameStatus.isVisible = highlightRange ? highlightRange.isVisible : null;
                updateRedFrameDebug();
            }
            // 双窗口高亮：在绘制K线之前先画灰色背景
            let offscreenIndicator = null; // {isLeft, isRight} 用于最后画箭头
            let highlightCenterDate = null; // 灰框中间K线的日期
            if (highlightRange && highlightRange.startIdx !== undefined) {
                if (highlightRange.isVisible) {
                    const globalStart = Math.max(0, Math.floor(viewOffset));
                    const hStartX = globalIdxToX(highlightRange.startIdx, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                    const hEndX = globalIdxToX(highlightRange.endIdx, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                    ctx.fillStyle = "rgba(128, 128, 128, 0.35)";
                    ctx.fillRect(hStartX, area.y, hEndX - hStartX, area.h);
                    // 画灰框中间的白色纵线
                    const centerIdx = Math.round((highlightRange.startIdx + highlightRange.endIdx) / 2);
                    const centerKline = data.klines[centerIdx];
                    if (centerKline) {
                        highlightCenterDate = centerKline.date;
                        const centerX = globalIdxToX(centerIdx, globalStart, area.x, barStep, subPixelOffset);
                        ctx.strokeStyle = "rgba(255, 255, 255, 0.5)";
                        ctx.lineWidth = 1;
                        ctx.setLineDash([4, 3]);
                        ctx.beginPath();
                        ctx.moveTo(centerX, area.y);
                        ctx.lineTo(centerX, area.y + area.h);
                        ctx.stroke();
                        ctx.setLineDash([]);
                    }
                } else if (highlightRange.isLeft || highlightRange.isRight) {
                    offscreenIndicator = { isLeft: highlightRange.isLeft, isRight: highlightRange.isRight };
                }
            }
            drawGrid(area, priceRange); drawGrid(volArea, macdRange);
            ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(area.x + area.w, area.y);
            ctx.lineTo(area.x + area.w, volArea.y + volArea.h);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(area.x, area.y);
            ctx.lineTo(area.x, volArea.y + volArea.h);
            ctx.stroke();
            const klinesToDraw = klines.slice(0, viewCount);
            drawMacdLabel(macdTextArea, klinesToDraw, barStep, subPixelOffset);
            drawMacd(klinesToDraw, volArea, macdRange, barStep, barWidth, subPixelOffset);
            // 区间选择高亮：绘制起点A的金色标记
            if (_rangeSelect.mode === 'SELECTED_A' && _rangeSelect.startFreq === currentFreq && chartData && _rangeSelect.startSymbol === chartData.meta.symbol) {
                const selIdx = _rangeSelect.startIdx;
                const globalStart = Math.max(0, Math.floor(viewOffset));
                const selX = globalIdxToX(selIdx, globalStart, area.x, barStep, subPixelOffset);
                if (selX >= area.x - barStep && selX <= area.x + area.w + barStep) {
                    const selX1 = selX - barStep / 2;
                    const selX2 = selX + barStep / 2;
                    ctx.fillStyle = "rgba(255, 215, 0, 0.22)";
                    ctx.fillRect(selX1, area.y, selX2 - selX1, area.h);
                    ctx.strokeStyle = "rgba(255, 215, 0, 0.7)";
                    ctx.lineWidth = 1.5;
                    ctx.strokeRect(selX1, area.y, selX2 - selX1, area.h);
                    // 顶部标签
                    const selK = data.klines[selIdx];
                    if (selK) {
                        const label = "A";
                        ctx.font = "bold 11px monospace";
                        ctx.fillStyle = "rgba(0,0,0,0.75)";
                        ctx.fillRect(selX - 8, area.y - 18, 16, 16);
                        ctx.fillStyle = "#FFD700";
                        ctx.textAlign = "center";
                        ctx.fillText(label, selX, area.y - 6);
                    }
                }
            }
            drawCandles(klinesToDraw, area, priceRange, barStep, barWidth, subPixelOffset);
            if (showMa) {
                try { drawMaLines(klinesToDraw, area, priceRange, barStep, subPixelOffset); }
                catch (e) { console.error("[drawMaLines错误]", e); }
            }
            if (showBi) drawBiLines(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showFx) drawFxMarkers(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            // 双窗口新模式：红框完整时，用新中枢替代原线段/中枢/买卖点
            const isBottomNewZs = (data === dualBottomData && dualShowNewZs && dualNewZsData);
            if (showZs && !isBottomNewZs) drawZs(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showSeg && !isBottomNewZs) drawSegLines(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showBsp && !isBottomNewZs) drawBspMarkers(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (isBottomNewZs) drawDualNewZs(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawWhiteHLine(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawAnnotations(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            _overlayData = null;
            drawCrosshair(klinesToDraw, area, priceRange, volArea, macdRange, barStep, macdTextArea, subPixelOffset);
            drawPriceAxis(area, priceRange); drawMacdAxis(volArea, macdRange);
            drawDateAxis(klinesToDraw, barStep, subPixelOffset);
            if (_overlayData) {
                if (_overlayData.rightPrice !== undefined) {
                    const labelW = 50;
                    ctx.fillStyle = "#dcdcdc"; ctx.fillRect(area.x + area.w + 2, _overlayData.rightY - 10, labelW, 20);
                    ctx.fillStyle = "#333"; ctx.font = "11px monospace"; ctx.textAlign = "left";
                    ctx.fillText(_overlayData.rightPrice, area.x + area.w + 6, _overlayData.rightY + 4);
                }
                if (_overlayData.bottomText) {
                    const d = _overlayData;
                    ctx.fillStyle = "#dcdcdc";
                    ctx.fillRect(d.bottomX, d.bottomY, d.bottomW + d.bottomPad * 2, d.bottomH);
                    ctx.fillStyle = "#333"; ctx.textAlign = "left";
                    ctx.fillText(d.bottomText, d.bottomX + d.bottomPad, d.bottomY + 13);
                }
            }
            // 双窗口：在所有绘制完成后，画视口外指示箭头（确保不被覆盖）
            if (offscreenIndicator) {
                const arrowSize = 10;
                const arrowY = area.y + area.h / 2;
                ctx.fillStyle = "rgba(200, 200, 200, 0.6)";
                ctx.beginPath();
                if (offscreenIndicator.isLeft) {
                    ctx.moveTo(area.x + arrowSize + 4, arrowY - arrowSize);
                    ctx.lineTo(area.x + 4, arrowY);
                    ctx.lineTo(area.x + arrowSize + 4, arrowY + arrowSize);
                } else {
                    ctx.moveTo(area.x + area.w - arrowSize - 4, arrowY - arrowSize);
                    ctx.lineTo(area.x + area.w - 4, arrowY);
                    ctx.lineTo(area.x + area.w - arrowSize - 4, arrowY + arrowSize);
                }
                ctx.closePath();
                ctx.fill();
            }
            // 双窗口高亮：在灰框中间白线下方显示日期标签（同drawCrosshair完整信息）
            if (highlightCenterDate && highlightRange && highlightRange.isVisible) {
                const globalStart = Math.max(0, Math.floor(viewOffset));
                const centerIdx = Math.round((highlightRange.startIdx + highlightRange.endIdx) / 2);
                const centerX = globalIdxToX(centerIdx, globalStart, area.x, barStep, subPixelOffset);
                const centerKline = data.klines[centerIdx];
                if (centerKline) {
                    // 格式化日期
                    let shortDate;
                    if (freq === '15s') {
                        shortDate = getKlineEndTime(highlightCenterDate, true);
                    } else if (freq === '1m' || freq === '30m' || freq === '5m') {
                        shortDate = getKlineEndTime(highlightCenterDate);
                    } else if (freq === 'w') {
                        const dateParts = highlightCenterDate.split("-");
                        shortDate = dateParts[0].slice(2) + "-" + dateParts[1] + "-" + dateParts[2];
                    } else {
                        const dateParts = highlightCenterDate.split("-");
                        shortDate = dateParts[0].slice(2) + "-" + dateParts[1] + "-" + dateParts[2];
                    }
                    const d = new Date(highlightCenterDate.replace(" ", "T"));
                    const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                    const weekDay = "周" + weekDays[d.getDay()];
                    // barsToRight: 从centerIdx到最右边可见K线
                    const rightGlobalIdx = globalStart + klines.length - 1;
                    const barsToRight = Math.max(1, rightGlobalIdx - centerIdx + 1);
                    // 涨跌幅: 从centerIdx到最右边可见K线
                    const prevKLine = centerIdx > 0 ? data.klines[centerIdx - 1] : null;
                    const startPrice = prevKLine ? prevKLine.close : centerKline.open;
                    const rightVisibleK = klines[klines.length - 1];
                    const totalChange = rightVisibleK.close - startPrice;
                    const totalChangePct = startPrice !== 0 ? (totalChange / startPrice * 100).toFixed(2) : "0.00";
                    const tcSign = totalChange >= 0 ? "+" : "";
                    const extraText = ` ${barsToRight}根 ${tcSign}${totalChange.toFixed(2)}(${tcSign}${totalChangePct}%)`;
                    const dateText = shortDate + " " + weekDay + extraText;
                    ctx.font = "11px monospace";
                    const textW = ctx.measureText(dateText).width;
                    const labelH = 18;
                    const labelPad = 4;
                    let labelX = centerX - textW / 2 - labelPad;
                    if (labelX < area.x) labelX = area.x;
                    if (labelX + textW + labelPad * 2 > area.x + area.w) labelX = area.x + area.w - textW - labelPad * 2;
                    const labelY = area.y + area.h - labelH;
                    ctx.fillStyle = "#dcdcdc";
                    ctx.fillRect(labelX, labelY, textW + labelPad * 2, labelH);
                    ctx.fillStyle = "#333"; ctx.textAlign = "left";
                    ctx.fillText(dateText, labelX + labelPad, labelY + 13);
                }
            }
            // 恢复全局变量
            viewOffset = _savedViewOffset; viewCount = _savedViewCount;
            mouseX = _savedMouseX; mouseY = _savedMouseY;
            currentFreq = _savedCurrentFreq;
            chartData = _savedChartData;
            // 只在主窗口（上面窗口或单窗口）更新统计
            if (data === _savedChartData || !isDualWindow) {
                generateStats();
            }
            // 始终更新slider（双窗口下根据激活窗口显示对应数据范围）
            updateSlider();
        }

        // 红框调试面板更新（不依赖console.log，即使F12过滤也能在页面上看到）
        function updateRedFrameDebug() {
            var dbg = document.getElementById("redframe-debug");
            if (!dbg || !isDualWindow) return;
            var st = window._lastRedFrameStatus;
            if (!st) return;
            dbg.style.display = "block";
            var stateEl = document.getElementById("rfdb-state");
            var detailEl = document.getElementById("rfdb-detail");
            // 显示灰色框状态
            var gs = window._lastGrayStatus;
            var grayInfo = "";
            if (gs && gs.startIdx !== undefined) {
                grayInfo = " 灰[" + gs.startIdx + "-" + gs.endIdx + (gs.isVisible ? "✓" : "✗") + "]";
            }
            if (st.state === "SKIP") {
                stateEl.textContent = "跳过";
                stateEl.style.color = "#ffa710";
                var extra = "";
                if (window._lastCalcRedRangeError) extra += " ERR:" + window._lastCalcRedRangeError;
                if (st.aDt) extra += " aDt=" + st.aDt;
                if (st.bDt) extra += " bDt=" + st.bDt;
                if (st.bottomFirst) extra += " btm1st=" + st.bottomFirst;
                if (st.bottomLast) extra += " btmLast=" + st.bottomLast;
                detailEl.textContent = (st.reason||"") + grayInfo + extra + " redRange=" + st.redRange + " hl=" + st.highlightRange + " vis=" + st.isVisible;
            } else if (st.state === "DRAW") {
                stateEl.textContent = "已绘制";
                stateEl.style.color = "#4caf50";
                // 格式化日期：30分钟数据去掉秒和前面多余的
                function fmtDate(d) {
                    if (!d) return "?";
                    if (d.length >= 16) return d.slice(5, 16);  // "MM-DD HH:MM"
                    return d.slice(5, 10);  // "MM-DD"
                }
                detailEl.textContent = "[" + fmtDate(st.leftDate) + ", " + fmtDate(st.rightDate) + "]";
            } else if (st.state === "OK") {
                stateEl.textContent = "计算OK";
                stateEl.style.color = "#2196f3";
                detailEl.textContent = "A/B[" + st.aIdx + "," + st.bIdx + "] 灰[" + st.grayStart + "," + st.grayEnd + "] before=" + st.before + " after=" + st.after + grayInfo;
            } else {
                stateEl.textContent = st.state || "--";
                stateEl.style.color = "#fff";
                detailEl.textContent = "";
            }
        }

        // 上面窗口鼠标移动时更新下面窗口高亮并重绘下面窗口
        function updateDualHighlight() {
            if (!isDualWindow || !dualBottomData) return;
            if (mouseX >= 0) {
                dualHighlightRange = calcDualHighlight(mouseX);
                dualRedRange = dualHighlightRange ? dualHighlightRange.redRange : null;
                // 更新状态A：区间是否在视口外
                dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                // 更新调试面板：显示灰框状态
                if (dualHighlightRange && dualHighlightRange.startIdx !== undefined) {
                    window._lastGrayStatus = {
                        startIdx: dualHighlightRange.startIdx,
                        endIdx: dualHighlightRange.endIdx,
                        isVisible: dualHighlightRange.isVisible,
                        redRange: !!dualRedRange
                    };
                } else {
                    window._lastGrayStatus = { noMatch: true };
                }
            }
            renderBottom();
        }

        // 双窗口新模式：检查红框是否完整，若完整则请求用红框内笔计算新中枢
        function updateDualNewZs() {
            if (!isDualWindow || !dualBottomData || !dualHighlightRange) {
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            const rr = dualHighlightRange.redRange;
            if (!rr) {
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            const aIdx = rr.aIdx;
            const bIdx = rr.bIdx;
            if (aIdx === undefined || bIdx === undefined) return;
            const globalStart = Math.floor(dualBottomViewOffset);
            const globalEnd = globalStart + dualBottomViewCount;
            // 完整红框：左右边界都在视口内
            const isComplete = (aIdx >= globalStart && bIdx < globalEnd);
            if (!isComplete) {
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            // 在下面窗口的笔中，找到第一个和最后一个被红框完全覆盖的完整笔
            const bottomKlines = dualBottomData.klines;
            const leftDate = bottomKlines[aIdx].date;
            const rightDate = bottomKlines[bIdx].date;
            let startBi = -1, endBi = -1;
            const bis = dualBottomData.bis || [];
            for (let i = 0; i < bis.length; i++) {
                const bi = bis[i];
                if (bi.sdt >= leftDate && bi.edt <= rightDate) {
                    if (startBi === -1) startBi = i;
                    endBi = i;
                }
            }
            if (startBi === -1 || endBi === -1 || startBi > endBi) {
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            if (dualShowNewZs && dualNewZsStartBi === startBi && dualNewZsEndBi === endBi) {
                return;
            }
            dualNewZsStartBi = startBi;
            dualNewZsEndBi = endBi;
            const code = dualBottomData.meta.symbol;
            fetch("/api/dual_zs?code=" + encodeURIComponent(code) + "&freq=" + dualBottomFreq + "&start_bi=" + startBi + "&end_bi=" + endBi)
                .then(resp => resp.json())
                .then(data => {
                    if (data.error) {
                        console.error("[dual_zs] 后端错误:", data.error);
                        return;
                    }
                    if (dualNewZsStartBi === startBi && dualNewZsEndBi === endBi) {
                        dualNewZsData = data;
                        dualShowNewZs = true;
                        renderBottom();
                    }
                })
                .catch(err => {
                    console.error("[dual_zs] 请求失败:", err);
                });
        }

        function drawGrid(area, range) {
            ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
            for (let i = 0; i <= 5; i++) {
                const y = area.y + (area.h / 5) * i;
                ctx.beginPath(); ctx.moveTo(area.x, y); ctx.lineTo(area.x + area.w, y); ctx.stroke();
            }
        }

        function drawCandles(klines, area, priceRange, barStep, barWidth, subPixelOffset) {
            klines.forEach((k, i) => {
                const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                const openY = priceToY(k.open, area, priceRange);
                const closeY = priceToY(k.close, area, priceRange);
                const highY = priceToY(k.high, area, priceRange);
                const lowY = priceToY(k.low, area, priceRange);
                const bodyTop = Math.min(openY, closeY);
                const bodyH = Math.max(1, Math.abs(closeY - openY));

                if (k.close === k.open) {
                    // 收盘价等于开盘价，用白色画
                    ctx.fillStyle = "#FFFFFF";
                    ctx.fillRect(x - 0.5, highY, 1, lowY - highY);
                    ctx.strokeStyle = "#FFFFFF"; ctx.lineWidth = 1;
                    ctx.strokeRect(x - barWidth / 2, bodyTop, barWidth, bodyH);
                } else if (k.close > k.open) {
                    ctx.fillStyle = "#FF4444";
                    if (highY < bodyTop) {
                        ctx.fillRect(x - 0.5, highY, 1, bodyTop - highY);
                    }
                    if (bodyTop + bodyH < lowY) {
                        ctx.fillRect(x - 0.5, bodyTop + bodyH, 1, lowY - bodyTop - bodyH);
                    }
                    ctx.strokeStyle = "#FF4444"; ctx.lineWidth = 1;
                    ctx.strokeRect(x - barWidth / 2, bodyTop, barWidth, bodyH);
                } else {
                    ctx.fillStyle = "#54fcfc";
                    ctx.fillRect(x - 0.5, highY, 1, lowY - highY);
                    ctx.fillRect(x - barWidth / 2, bodyTop, barWidth, bodyH);
                    ctx.fillRect(x - 0.5, bodyTop + bodyH, 1, lowY - bodyTop - bodyH);
                }
            });
        }

        function drawMacd(klines, macdArea, macdRange, barStep, barWidth, subPixelOffset) {
            const zeroY = macdArea.y + macdArea.h * (macdRange.max / (macdRange.max - macdRange.min));
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const isUp = k.macd >= 0;
                ctx.fillStyle = isUp ? COLORS.macdUp : COLORS.macdDown;
                const macdH = Math.abs(k.macd) / (macdRange.max - macdRange.min) * macdArea.h;
                const y = k.macd >= 0 ? zeroY - macdH : zeroY;
                ctx.fillRect(x - barWidth / 2, y, barWidth, macdH);
            });
            ctx.strokeStyle = COLORS.dif; ctx.lineWidth = 1;
            ctx.beginPath();
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const y = macdArea.y + macdArea.h - (k.dif - macdRange.min) / (macdRange.max - macdRange.min) * macdArea.h;
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.strokeStyle = COLORS.dea; ctx.lineWidth = 1;
            ctx.beginPath();
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const y = macdArea.y + macdArea.h - (k.dea - macdRange.min) / (macdRange.max - macdRange.min) * macdArea.h;
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.strokeStyle = "rgba(255,255,255,0.2)"; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(macdArea.x, zeroY); ctx.lineTo(macdArea.x + macdArea.w, zeroY); ctx.stroke();
        }

        function drawBiLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.bis.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            ctx.lineWidth = 1;
            chartData.bis.forEach(bi => {
                let s = dateToGlobalIdx(bi.sdt, map), e = dateToGlobalIdx(bi.edt, map);
                if (s === undefined || e === undefined) return;
                // 笔的两端都必须在视口内才显示（确保成笔条件在当前视口内成立）
                if (s < globalStart || s >= globalEnd || e < globalStart || e >= globalEnd) return;
                let x1 = globalIdxToX(s, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(e, globalStart, area.x, barStep, subPixelOffset);
                // 裁剪到图表主区域内
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(bi.fx_a_price, area, priceRange);
                const y2 = priceToY(bi.fx_b_price, area, priceRange);
                // 未确定的笔用虚线绘制，确定的笔用实线
                // 原值: 确定笔="#FFFFFF", 未确定笔="rgba(255, 255, 255, 0.4)"
                if (bi.is_sure === false) {
                    ctx.strokeStyle = "rgba(253, 221, 96, 0.4)";
                    ctx.setLineDash([4, 4]);
                } else {
                    ctx.strokeStyle = "#fddd60";
                    ctx.setLineDash([]);
                }
                ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
            });
            ctx.setLineDash([]);
        }

        function drawMacdLabel(textArea, klines, barStep, subPixelOffset) {
            let targetK = null;
            if (mouseX >= textArea.x && mouseX <= textArea.x + textArea.w) {
                const idx = Math.floor((mouseX - textArea.x + subPixelOffset) / barStep);
                targetK = klines[Math.min(idx, klines.length - 1)];
            }
            if (!targetK) targetK = klines[klines.length - 1];
            if (targetK) {
                ctx.font = "13px monospace"; ctx.textAlign = "left";
                const lineY = textArea.y + 13;
                ctx.fillStyle = COLORS.textLight;
                ctx.fillText("MACD(12,26,9)", textArea.x + 4, lineY);
                let xPos = textArea.x + 4 + ctx.measureText("MACD(12,26,9) ").width;
                ctx.fillStyle = COLORS.dif;
                ctx.fillText("DIF:" + targetK.dif.toFixed(2), xPos, lineY);
                xPos += ctx.measureText("DIF:" + targetK.dif.toFixed(2) + " ").width;
                ctx.fillStyle = COLORS.dea;
                ctx.fillText("DEA:" + targetK.dea.toFixed(2), xPos, lineY);
                xPos += ctx.measureText("DEA:" + targetK.dea.toFixed(2) + " ").width;
                ctx.fillStyle = targetK.macd >= 0 ? COLORS.macdUp : COLORS.macdDown;
                ctx.fillText("BAR:" + targetK.macd.toFixed(2), xPos, lineY);
            }
        }

        function drawFxMarkers(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.fxs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            let fxNum = 0;
            ctx.font = "10px monospace"; ctx.textAlign = "center";
            chartData.fxs.forEach(fx => {
                let idx = dateToGlobalIdx(fx.date, map);
                if (idx === undefined) return;
                if (idx < globalStart || idx >= globalEnd) return;
                fxNum++;
                const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
                const y = priceToY(fx.price, area, priceRange);
                const isTop = fx.mark === "G";
                const color = isTop ? COLORS.up : COLORS.down;
                ctx.fillStyle = color;
                ctx.fillText(String(fxNum), x, isTop ? y - 4 : y + 10);
            });
        }

        function drawZs(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.zs || !chartData.zs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;

            chartData.zs.forEach(zs => {
                let sIdx = dateToGlobalIdx(zs.sdt, map);
                let eIdx = zs.confirm_edt ? dateToGlobalIdx(zs.confirm_edt, map) : undefined;
                if (sIdx === undefined) return;
                if (eIdx === undefined) {
                    // 未确认结束的中枢延伸到当前数据最后一根K线，而不是使用 zs.end/edt 过早收口
                    eIdx = chartData.klines.length - 1;
                }
                // 只绘制与当前视口有交集的中枢
                if (eIdx < globalStart || sIdx >= globalEnd) return;

                // 右边框使用后端给出的“中枢结束事实被确认”的时点；未确认则延伸到最新K线
                let finalEndIdx = eIdx;

                let x1 = globalIdxToX(sIdx, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(finalEndIdx, globalStart, area.x, barStep, subPixelOffset);
                // 裁剪到图表主区域内
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(zs.zg, area, priceRange);
                const y2 = priceToY(zs.zd, area, priceRange);

                const isUp = zs.dir === "up";
                const fillColor = isUp ? "rgba(220, 50, 50, 0.10)" : "rgba(50, 180, 50, 0.10)";
                const strokeColor = isUp ? "rgba(220, 50, 50, 0.6)" : "rgba(50, 180, 50, 0.6)";
                const textColor = isUp ? "rgba(220, 50, 50, 0.8)" : "rgba(50, 180, 50, 0.8)";

                ctx.fillStyle = fillColor;
                ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
                ctx.setLineDash([]);

                ctx.font = "10px monospace";
                ctx.fillStyle = textColor;
                ctx.textAlign = "right";
                ctx.fillText(zs.zg.toFixed(2), x1 - 2, y1 - 2);
                ctx.fillText(zs.zd.toFixed(2), x1 - 2, y2 + 10);
            });

            if (!chartData.zs_stars || !chartData.zs_stars.length) return;
            chartData.zs_stars.forEach(star => {
                let idx = dateToGlobalIdx(star.date, map);
                if (idx === undefined) return;
                if (idx < globalStart || idx >= globalEnd) return;
                const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
                const y = priceToY(star.price, area, priceRange);
                const isTop = star.mark === "G";
                const starY = isTop ? y - 16 : y + 22;
                drawStar(ctx, x, starY, 5, 6, 3, star.color);
            });
        }

        function drawStar(ctx, cx, cy, spikes, outerR, innerR, color) {
            ctx.fillStyle = color;
            ctx.beginPath();
            for (let i = 0; i < spikes * 2; i++) {
                const r = i % 2 === 0 ? outerR : innerR;
                const angle = (Math.PI / spikes) * i - Math.PI / 2;
                const x = cx + r * Math.cos(angle);
                const y = cy + r * Math.sin(angle);
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }
            ctx.closePath();
            ctx.fill();
        }

        // 画线段（与笔同粗细，区分方向颜色）
        function drawSegLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.segs || !chartData.segs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            ctx.lineWidth = 1; ctx.setLineDash([]);
            chartData.segs.forEach(seg => {
                let s = dateToGlobalIdx(seg.sdt, map), e = dateToGlobalIdx(seg.edt, map);
                if (s === undefined || e === undefined) return;
                // 只绘制与当前视口有交集的线段
                if (e < globalStart || s >= globalEnd) return;
                let x1 = globalIdxToX(s, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(e, globalStart, area.x, barStep, subPixelOffset);
                // 裁剪到图表主区域内
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(seg.begin_price, area, priceRange);
                const y2 = priceToY(seg.end_price, area, priceRange);
                ctx.strokeStyle = "#ffa710"; // 原值: up="#FF6666", down="#66FF66"
                ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
            });
        }

        // 画买卖点标记（买点▲红色，卖点▼绿色——绿色与MACD绿柱子同色）
        // 标记画在K线外侧：买点在最低价下方，卖点在最高价上方，与分型标号/五角星错开
        function drawBspMarkers(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.bsps || !chartData.bsps.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            chartData.bsps.forEach(bsp => {
                let idx = dateToGlobalIdx(bsp.date, map);
                if (idx === undefined) return;
                if (idx < globalStart || idx >= globalEnd) return;
                const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
                const isBuy = bsp.is_buy;
                // 用K线外侧价格定位：买点用low，卖点用high
                const anchorPrice = isBuy ? bsp.low : bsp.high;
                const y = priceToY(anchorPrice, area, priceRange);
                const color = isBuy ? COLORS.up : COLORS.down;
                ctx.fillStyle = color;
                ctx.textAlign = "center";
                ctx.textBaseline = "middle";
                ctx.font = "bold 14px monospace";
                // 错开偏移：买点往下放（远离五角星/分型），卖点往上放
                const markerY = isBuy ? y + 22 : y - 22;
                ctx.fillText(isBuy ? "▲" : "▼", x, markerY);
                // 买卖点类型标签再往外错开一点（与三角形同色，fillStyle已设置）
                ctx.font = "11px sans-serif";
                const labelY = isBuy ? markerY + 18 : markerY - 18;
                ctx.fillText(bsp.type, x, labelY);
                ctx.textBaseline = "alphabetic";
            });
        }

        // 双窗口新模式：绘制红框内笔计算的新中枢（替代原中枢/线段/买卖点）
        function drawDualNewZs(klines, area, priceRange, barStep, subPixelOffset) {
            if (!dualNewZsData || !dualNewZsData.zs || !dualNewZsData.zs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;

            dualNewZsData.zs.forEach((zs, zsIdx) => {
                let sIdx = dateToGlobalIdx(zs.sdt, map);
                if (sIdx === undefined) return;
                let eIdx = undefined;
                if (zs.confirm_edt) {
                    // 中枢被确认的笔打破 → 右边界在打破笔的末端
                    eIdx = dateToGlobalIdx(zs.confirm_edt, map);
                }
                if (eIdx === undefined) {
                    // 未被确认打破 → 用 edt（最后重叠笔的末端）
                    eIdx = zs.edt ? dateToGlobalIdx(zs.edt, map) : undefined;
                }
                if (eIdx === undefined) {
                    eIdx = chartData.klines.length - 1;
                }
                // 最后一个中枢，未被确认打破 → 延伸到红框右边界
                if (zsIdx === dualNewZsData.zs.length - 1 && !zs.confirm_edt && dualRedRange && dualRedRange.bIdx !== undefined) {
                    if (dualRedRange.bIdx > eIdx) {
                        eIdx = dualRedRange.bIdx;
                    }
                }
                if (eIdx < globalStart || sIdx >= globalEnd) return;
                let finalEndIdx = eIdx;
                let x1 = globalIdxToX(sIdx, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(finalEndIdx, globalStart, area.x, barStep, subPixelOffset);
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(zs.zg, area, priceRange);
                const y2 = priceToY(zs.zd, area, priceRange);

                const isUp = zs.dir === "up";
                // 新中枢使用更醒目的颜色区分
                const fillColor = isUp ? "rgba(220, 50, 50, 0.10)" : "rgba(50, 180, 50, 0.10)";
                const strokeColor = isUp ? "rgba(255, 80, 80, 0.85)" : "rgba(80, 255, 80, 0.85)";
                const textColor = isUp ? "rgba(220, 50, 50, 0.8)" : "rgba(50, 180, 50, 0.8)";

                ctx.fillStyle = fillColor;
                ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
                ctx.setLineDash([]);

                ctx.font = "10px monospace";
                ctx.fillStyle = textColor;
                ctx.textAlign = "right";
                ctx.fillText(zs.zg.toFixed(2), x1 - 2, y1 - 2);
                ctx.fillText(zs.zd.toFixed(2), x1 - 2, y2 + 10);
            });

            if (!dualNewZsData.zs_stars || !dualNewZsData.zs_stars.length) return;
            dualNewZsData.zs_stars.forEach(star => {
                let idx = dateToGlobalIdx(star.date, map);
                if (idx === undefined) return;
                if (idx < globalStart || idx >= globalEnd) return;
                const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
                const y = priceToY(star.price, area, priceRange);
                const isTop = star.mark === "G";
                const starY = isTop ? y - 16 : y + 22;
                drawStar(ctx, x, starY, 5, 6, 3, star.color);
            });
        }

        function drawMaLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || klines.length < 2) return;
            const start = Math.max(0, Math.floor(viewOffset));
            const allKlines = chartData.klines;
            const n = allKlines.length;
            const ma5 = new Array(n).fill(null);
            const ma120 = new Array(n).fill(null);
            let sum5 = 0;
            for (let i = 0; i < n; i++) {
                sum5 += allKlines[i].close;
                if (i >= 5) sum5 -= allKlines[i - 5].close;
                if (i >= 4) ma5[i] = sum5 / 5;
            }
            let sum120 = 0;
            for (let i = 0; i < n; i++) {
                sum120 += allKlines[i].close;
                if (i >= 120) sum120 -= allKlines[i - 120].close;
                if (i >= 119) ma120[i] = sum120 / 120;
            }
            ctx.strokeStyle = "#FFFFFF"; ctx.lineWidth = 1;
            ctx.beginPath();
            let started = false;
            for (let i = 0; i < klines.length; i++) {
                const globalIdx = start + i;
                if (globalIdx < n && ma5[globalIdx] !== null && !isNaN(ma5[globalIdx])) {
                    const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                    const y = priceToY(ma5[globalIdx], area, priceRange);
                    if (!started) { ctx.moveTo(x, y); started = true; }
                    else ctx.lineTo(x, y);
                }
            }
            ctx.stroke();
            ctx.strokeStyle = "#888888"; ctx.lineWidth = 1;
            ctx.beginPath();
            started = false;
            for (let i = 0; i < klines.length; i++) {
                const globalIdx = start + i;
                if (globalIdx < n && ma120[globalIdx] !== null && !isNaN(ma120[globalIdx])) {
                    const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                    const y = priceToY(ma120[globalIdx], area, priceRange);
                    if (!started) { ctx.moveTo(x, y); started = true; }
                    else ctx.lineTo(x, y);
                }
            }
            ctx.stroke();
        }

        // 画最新笔的白色横虚线（同一时间只有一根）
        function drawWhiteHLine(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.white_hline) return;
            const hline = chartData.white_hline;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            // 找到起始日期对应的全局索引
            let startIdx = dateToGlobalIdx(hline.start_date, map);
            if (startIdx === undefined) return;
            // 如果起始点在视口右边之外，不绘制
            if (startIdx >= globalEnd) return;
            // 计算起始X坐标（如果起始点在视口左边之外，则从area.x开始）
            let x1;
            if (startIdx < globalStart) {
                x1 = area.x;
            } else {
                x1 = globalIdxToX(startIdx, globalStart, area.x, barStep, subPixelOffset);
            }
            // 向右延伸到页面最右边
            const x2 = rightBound;
            const y = priceToY(hline.price, area, priceRange);
            // 白色横虚线
            ctx.strokeStyle = "#FFFFFF";
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 3]);
            ctx.beginPath();
            ctx.moveTo(x1, y);
            ctx.lineTo(x2, y);
            ctx.stroke();
            ctx.setLineDash([]);
            // 在右端显示价格标签
            ctx.fillStyle = "#FFFFFF";
            ctx.font = "11px monospace";
            ctx.textAlign = "left";
            ctx.fillText(hline.price.toFixed(2), x2 + 4, y + 4);
        }

        function drawCrosshair(klines, area, priceRange, volArea, volRange, barStep, macdTextArea, subPixelOffset) {
            let idx, k, cx;
            if (mouseX < area.x || mouseX > area.x + area.w) {
                idx = klines.length - 1;
                k = klines[idx];
                if (!k) return;
                cx = area.x + barStep * idx + barStep / 2 - subPixelOffset;
                // K线不足一屏时右对齐
                if (currentFreq === 'w') {
                    cx = area.x + area.w - barStep / 2;
                }
            } else {
                idx = Math.floor((mouseX - area.x + subPixelOffset) / barStep);
                k = klines[Math.min(idx, klines.length - 1)];
                if (!k) return;
                cx = area.x + barStep * idx + barStep / 2 - subPixelOffset;
                const crosshairEndY = volArea.y + volArea.h;
                ctx.strokeStyle = COLORS.crosshair; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
                ctx.beginPath(); ctx.moveTo(cx, area.y); ctx.lineTo(cx, crosshairEndY); ctx.stroke();
                if (mouseY >= area.y && mouseY <= area.y + area.h) {
                    ctx.beginPath(); ctx.moveTo(area.x, mouseY); ctx.lineTo(area.x + area.w, mouseY); ctx.stroke();
                }
                ctx.setLineDash([]);
                if (mouseY >= area.y && mouseY <= area.y + area.h) {
                    const price = yToPrice(mouseY, area, priceRange);
                    _overlayData = _overlayData || {};
                    _overlayData.rightPrice = price.toFixed(2);
                    _overlayData.rightY = mouseY;
                }
            }

            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalIdx = globalStart + idx;
            if (!window._isRenderingBottom) {
                _currentGlobalIdx = globalIdx;
            }
            const prevK = globalIdx > 0 ? chartData.klines[globalIdx - 1] : null;
            const prevClose = prevK ? prevK.close : k.open;
            const changeVal = k.close - prevClose;
            const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
            const cls = changeVal >= 0 ? "up" : "down";
            const sign = changeVal >= 0 ? "+" : "";

            if (mouseX >= area.x && mouseX <= area.x + area.w) {
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                let shortDate;
                if (currentFreq === '15s') {
                    shortDate = getKlineEndTime(k.date, true);  // 含秒
                } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m') {
                    shortDate = getKlineEndTime(k.date);
                } else if (currentFreq === 'w') {
                    const dateParts = k.date.split("-");
                    shortDate = dateParts[0].slice(2) + "-" + dateParts[1] + "-" + dateParts[2];
                } else {
                    const dateParts = k.date.split("-");
                    shortDate = dateParts[0].slice(2) + "-" + dateParts[1] + "-" + dateParts[2];
                }
                const d = new Date(k.date.replace(" ", "T"));
                const weekDay = "周" + weekDays[d.getDay()];

                const rightVisibleK = klines[klines.length - 1];
                const rightGlobalIdx = globalStart + klines.length - 1;
                const barsToRight = Math.max(1, rightGlobalIdx - globalIdx + 1);
                const prevKLine = globalIdx > 0 ? chartData.klines[globalIdx - 1] : null;
                const startPrice = prevKLine ? prevKLine.close : k.open;
                const totalChange = rightVisibleK.close - startPrice;
                const totalChangePct = startPrice !== 0 ? (totalChange / startPrice * 100).toFixed(2) : "0.00";
                const tcSign = totalChange >= 0 ? "+" : "";

                const extraText = ` ${barsToRight}根 ${tcSign}${totalChange.toFixed(2)}(${tcSign}${totalChangePct}%)`;
                const dateText = shortDate + " " + weekDay + extraText;

                ctx.font = "11px monospace";
                const textW = ctx.measureText(dateText).width;
                const labelH = 18;
                const labelPad = 4;
                let labelX = cx - textW / 2 - labelPad;
                if (labelX < area.x) labelX = area.x;
                if (labelX + textW + labelPad * 2 > area.x + area.w) labelX = area.x + area.w - textW - labelPad * 2;
                const labelY = area.y + area.h - labelH;
                _overlayData = _overlayData || {};
                _overlayData.bottomText = dateText;
                _overlayData.bottomX = labelX;
                _overlayData.bottomY = labelY;
                _overlayData.bottomW = textW;
                _overlayData.bottomH = labelH;
                _overlayData.bottomPad = labelPad;
            }

            // 双窗口：下面窗口渲染时，仅当鼠标不在下面窗口上（mouseX<0）才跳过 OHLC 更新
            // 避免"鼠标在上面窗口时，下面窗口的最后一根K线数据覆盖上面窗口的 OHLC"
            if (!(window._isRenderingBottom && mouseX < 0)) {
                document.getElementById("crosshair-info").innerHTML =
                    `<span class="label">开:</span> <span class="${cls}">${k.open.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">高:</span> <span class="${cls}">${k.high.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">低:</span> <span class="${cls}">${k.low.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">收:</span> <span class="${cls}">${k.close.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">涨跌:</span> <span class="${cls}">${sign}${changeVal.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">涨幅:</span> <span class="${cls}">${sign}${changePct}%</span>`;
            }

            const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
            const weekDayStr = "周" + weekDays[new Date(k.date.replace(" ", "T")).getDay()];
            const clipText = `${k.date} ${weekDayStr} 开:${k.open.toFixed(2)} 高:${k.high.toFixed(2)} 低:${k.low.toFixed(2)} 收:${k.close.toFixed(2)} 涨跌:${sign}${changeVal.toFixed(2)} 涨幅:${sign}${changePct}%`;
            if (window._isRenderingBottom) {
                // 底部窗口：记录底部窗口的全局索引和剪贴板文本
                _bottomCurrentGlobalIdx = globalIdx;
                _bottomClipText = clipText;
            } else {
                // 上面窗口
                _currentGlobalIdx = globalIdx;
                _currentClipText = clipText;
            }
        }

        function drawPriceAxis(area, priceRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textAlign = "left";
            for (let i = 0; i <= 5; i++) {
                const price = priceRange.min + (priceRange.max - priceRange.min) * (1 - i / 5);
                const y = area.y + (area.h / 5) * i;
                ctx.fillText(price.toFixed(2), area.x + area.w + 6, y + 4);
            }
        }

        function drawMacdAxis(macdArea, macdRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textAlign = "left";
            ctx.fillText(macdRange.max.toFixed(2), macdArea.x + macdArea.w + 6, macdArea.y + 12);
            const zeroY = macdArea.y + macdArea.h * (macdRange.max / (macdRange.max - macdRange.min));
            ctx.fillText("0", macdArea.x + macdArea.w + 6, zeroY + 4);
            ctx.fillText(macdRange.min.toFixed(2), macdArea.x + macdArea.w + 6, macdArea.y + macdArea.h - 4);
        }

        function drawDateAxis(klines, barStep, subPixelOffset) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace";
            const area = getChartArea(), volArea = getVolArea();
            const dateY = volArea.y + volArea.h + 16;
            const minPixelGap = currentFreq === '30m' ? 110 : 70;
            const interval = Math.max(1, Math.ceil(minPixelGap / barStep));
            const indices = [];
            indices.push(0);
            for (let i = interval; i < klines.length - 1; i += interval) {
                indices.push(i);
            }
            indices.push(klines.length - 1);
            for (let j = indices.length - 1; j > 0; j--) {
                const x1 = area.x + barStep * indices[j - 1] + barStep / 2 - subPixelOffset;
                const x2 = area.x + barStep * indices[j] + barStep / 2 - subPixelOffset;
                if (x2 - x1 < minPixelGap) {
                    if (indices[j - 1] === 0) indices.splice(j - 1, 1);
                    else if (indices[j] === klines.length - 1) indices.splice(j - 1, 1);
                    else indices.splice(j, 1);
                }
            }
            indices.forEach(i => {
                let shortDate;
                if (currentFreq === '15s') {
                    // 15秒：显示日期+时间（含秒）
                    shortDate = getKlineEndTime(klines[i].date, true);
                } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m') {
                    shortDate = getKlineEndTime(klines[i].date);
                } else if (currentFreq === 'w') {
                    const dateParts = klines[i].date.split("-");
                    shortDate = dateParts[0].slice(2) + "-" + dateParts[1] + "-" + dateParts[2];
                } else {
                    // 日线
                    const dateParts = klines[i].date.split("-");
                    shortDate = dateParts[0].slice(2) + "-" + dateParts[1] + "-" + dateParts[2];
                }
                if (i === 0) {
                    ctx.textAlign = "left";
                    ctx.fillText(shortDate, area.x, dateY);
                } else if (i === klines.length - 1) {
                    ctx.textAlign = "right";
                    // K线不足一屏时：日期标签也右对齐
                    const dateX = currentFreq === 'w' ? area.x + area.w : area.x + area.w;
                    ctx.fillText(shortDate, dateX, dateY);
                } else {
                    ctx.textAlign = "center";
                    const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                    ctx.fillText(shortDate, x, dateY);
                }
            });
        }

        function onWheel(e) {
            e.preventDefault();
            if (isDualWindow) { activeDualWindow = 'top'; updateActiveWindowClass(); updateSlider(); }
            const area = getChartArea();
            const klines = chartData.klines;
            const barStep = area.w / viewCount;
            const ratio = Math.max(0, Math.min(1, (mouseX - area.x) / area.w));
            const mouseKIdx = ratio * viewCount;

            const zoomFactor = 1.15;
            const newViewCount = e.deltaY > 0
                ? Math.min(klines.length, Math.ceil(viewCount * zoomFactor))
                : Math.max(3, Math.round(viewCount / zoomFactor));
            if (newViewCount === viewCount) return;

            const maxOffset = klines.length - newViewCount;

            if (mouseKIdx >= viewCount - 1) {
                const rightGlobalIdx = viewOffset + viewCount - 1;
                viewCount = newViewCount;
                viewOffset = Math.max(0, Math.min(maxOffset, rightGlobalIdx - newViewCount + 1));
                if (isDualWindow) { renderTop(); } else { render(); }
                return;
            }

            const anchorGlobalIdx = viewOffset + mouseKIdx;
            let newViewOffset = anchorGlobalIdx - ratio * newViewCount;
            newViewOffset = Math.max(0, newViewOffset);
            if (newViewOffset > maxOffset) newViewOffset = maxOffset;

            viewCount = newViewCount;
            viewOffset = newViewOffset;
            if (isDualWindow) { renderTop(); } else { render(); }
        }

        function onMouseDown(e) {
            isDragging = true; dragStartX = e.clientX; dragStartOffset = viewOffset; canvas.style.cursor = "grabbing";
            _mouseDownX = e.clientX; _mouseDownY = e.clientY;
            if (isDualWindow) { activeDualWindow = 'top'; updateActiveWindowClass(); updateSlider(); }
        }
        function onMouseMove(e) {
            const rect = canvas.getBoundingClientRect();
            mouseX = e.clientX - rect.left; mouseY = e.clientY - rect.top;
            if (isDragging) { viewOffset = dragStartOffset - (e.clientX - dragStartX) / (getChartArea().w / viewCount); viewOffset = Math.max(0, Math.min(chartData.klines.length - viewCount, viewOffset)); }
            if (isDualWindow) {
                renderTop();
            } else {
                render();
            }
        }
        function onMouseUp(e) {
            isDragging = false; canvas.style.cursor = "crosshair";
            // 只处理左键点击（非拖拽）
            if (e.button !== 0 || Math.abs(e.clientX - _mouseDownX) >= 5 || Math.abs(e.clientY - _mouseDownY) >= 5) return;
            if (_currentGlobalIdx < 0 || !chartData) return;

            // === Ctrl+点击：区间选择模式切换 ===
            if (e.ctrlKey) {
                if (_rangeSelect.mode === 'IDLE') {
                    // 进入选择模式，记录起点A
                    _rangeSelect = {
                        mode: 'SELECTED_A',
                        startIdx: _currentGlobalIdx,
                        startFreq: currentFreq,
                        startSymbol: chartData.meta.symbol
                    };
                    const startDate = chartData.klines[_currentGlobalIdx].date.split(' ')[0];
                    showDualToast("区间起点: " + startDate + "，点击另一根K线完成选择");
                    render();
                } else {
                    // Ctrl+再次点击：取消选择
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("区间选择已取消");
                    render();
                }
                return;
            }

            // === 普通点击：如果在选择模式中，完成区间选择 ===
            if (_rangeSelect.mode === 'SELECTED_A') {
                // 验证：同一股票、同一周期
                if (_rangeSelect.startFreq !== currentFreq || _rangeSelect.startSymbol !== chartData.meta.symbol) {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("股票或周期已变更，区间选择已取消");
                    return;
                }
                const a = Math.min(_rangeSelect.startIdx, _currentGlobalIdx);
                const b = Math.max(_rangeSelect.startIdx, _currentGlobalIdx);
                const klines = chartData.klines;
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                const lines = [];
                for (let i = a; i <= b; i++) {
                    const k = klines[i];
                    const prevK = i > 0 ? klines[i - 1] : null;
                    const prevClose = prevK ? prevK.close : k.open;
                    const changeVal = k.close - prevClose;
                    const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
                    const sign = changeVal >= 0 ? "+" : "";
                    const wd = "周" + weekDays[new Date(k.date.replace(" ", "T")).getDay()];
                    lines.push(`${k.date} ${wd} 开:${k.open.toFixed(2)} 高:${k.high.toFixed(2)} 低:${k.low.toFixed(2)} 收:${k.close.toFixed(2)} 涨跌:${sign}${changeVal.toFixed(2)} 涨幅:${sign}${changePct}%`);
                }
                navigator.clipboard.writeText(lines.join("\n")).catch(() => {});
                showDualToast("已复制 " + (b - a + 1) + " 根K线数据到剪贴板");
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                render();
                return;
            }

            // === 普通模式：复制当前K线信息 ===
            if (_currentClipText) {
                navigator.clipboard.writeText(_currentClipText).catch(() => {});
            }
        }
        function onMouseLeave() { isDragging = false; mouseX = -1; mouseY = -1; canvas.style.cursor = "crosshair"; if (isDualWindow) { dualOffscreenState = false; dualHighlightRange = null; dualRedRange = null; dualNewZsData = null; dualShowNewZs = false; renderTop(); } else { render(); } }

        // 双窗口toast提示
        function showDualToast(msg) {
            let toast = document.getElementById("dual-toast");
            if (!toast) {
                toast = document.createElement("div");
                toast.id = "dual-toast";
                toast.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,0.85);color:#fff;padding:12px 28px;border-radius:8px;font-size:14px;z-index:9999;pointer-events:none;opacity:0;transition:opacity 0.3s;";
                document.body.appendChild(toast);
            }
            toast.textContent = msg;
            toast.style.opacity = "1";
            clearTimeout(toast._timer);
            toast._timer = setTimeout(() => { toast.style.opacity = "0"; }, 1000);
        }

        // Esc键取消区间选择
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape' && _rangeSelect.mode === 'SELECTED_A') {
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                showDualToast("区间选择已取消");
                render();
            }
        });

        // 更新双窗口激活状态视觉提示
        function updateActiveWindowClass() {
            const topDiv = document.getElementById("chart-top");
            const bottomDiv = document.getElementById("chart-bottom");
            if (topDiv) topDiv.classList.toggle("dual-active", activeDualWindow === 'top');
            if (bottomDiv) bottomDiv.classList.toggle("dual-active", activeDualWindow === 'bottom');
        }

        // 双窗口切换
        window.toggleDualWindow = function() {
            if (!chartData) return;
            const btn = document.getElementById("btn-dual");
            if (isDualWindow) {
                // 关闭双窗口
                isDualWindow = false;
                activeDualWindow = 'top';
                dualBottomData = null;
                dualBottomFreq = '';
                dualHighlightRange = null;
                dualRedRange = null;
                dualNewZsData = null;
                dualShowNewZs = false;
                dualNewZsStartBi = -1;
                dualNewZsEndBi = -1;
                btn.classList.remove("active");
                // 恢复单canvas布局
                const container = document.getElementById("chart-container");
                // 移除双窗口子元素
                const topDiv = document.getElementById("chart-top");
                const bottomDiv = document.getElementById("chart-bottom");
                if (topDiv) topDiv.remove();
                if (bottomDiv) bottomDiv.remove();
                // 恢复原始canvas
                canvas = topCanvas; ctx = topCtx;
                container.appendChild(canvas);
                resizeCanvas();
                render();
            } else {
                // 开启双窗口
                const bottomFreq = getDualBottomFreq(currentFreq);
                if (!bottomFreq) {
                    // 5分周期无对应，提示
                    return;
                }
                isDualWindow = true;
                dualBottomFreq = bottomFreq;
                btn.classList.add("active");
                // 创建双窗口布局
                const container = document.getElementById("chart-container");
                // 保存原始canvas引用
                const origCanvas = topCanvas;
                // 清空容器
                container.innerHTML = '';
                // 创建上面窗口
                const topDiv = document.createElement("div");
                topDiv.id = "chart-top";
                topDiv.appendChild(origCanvas);
                container.appendChild(topDiv);
                // 创建下面窗口
                const bottomDiv = document.createElement("div");
                bottomDiv.id = "chart-bottom";
                bottomCanvas = document.createElement("canvas");
                bottomCtx = bottomCanvas.getContext("2d");
                bottomDiv.appendChild(bottomCanvas);
                container.appendChild(bottomDiv);
                // 添加下面窗口事件
                bottomCanvas.addEventListener("wheel", onBottomWheel, { passive: false });
                bottomCanvas.addEventListener("mousedown", onBottomMouseDown);
                bottomCanvas.addEventListener("mousemove", onBottomMouseMove);
                bottomCanvas.addEventListener("mouseup", onBottomMouseUp);
                bottomCanvas.addEventListener("mouseleave", onBottomMouseLeave);
                bottomCanvas.addEventListener("dblclick", function(e) {
                    if (!dualBottomData) return;
                    const rect = bottomCanvas.getBoundingClientRect();
                    const clickX = e.clientX - rect.left;
                    const clickY = e.clientY - rect.top;
                    // 临时切换全局变量以使用 getChartArea 等函数
                    const _savedCanvas = canvas, _savedCtx = ctx;
                    const _savedViewOffset = viewOffset, _savedViewCount = viewCount;
                    const _savedChartData = chartData, _savedFreq = currentFreq;
                    canvas = bottomCanvas; ctx = bottomCtx;
                    viewOffset = dualBottomViewOffset; viewCount = dualBottomViewCount;
                    chartData = dualBottomData; currentFreq = dualBottomFreq;
                    const area = getChartArea();
                    const klines = getVisibleKlines();
                    if (!klines.length) { canvas = _savedCanvas; ctx = _savedCtx; viewOffset = _savedViewOffset; viewCount = _savedViewCount; chartData = _savedChartData; currentFreq = _savedFreq; return; }
                    const priceRange = getPriceRange(klines);
                    const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
                    const barStep = area.w / effectiveCount;
                    const barWidth = Math.max(1, barStep * 0.7);
                    const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
                    // 检查是否落在K线上
                    let clickedOnKline = false;
                    for (let i = 0; i < klines.length; i++) {
                        const k = klines[i];
                        const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                        const highY = priceToY(k.high, area, priceRange);
                        const lowY = priceToY(k.low, area, priceRange);
                        const halfW = barWidth / 2;
                        if (clickX >= x - halfW && clickX <= x + halfW &&
                            clickY >= highY && clickY <= lowY) {
                            clickedOnKline = true;
                            break;
                        }
                    }
                    // 恢复全局变量
                    canvas = _savedCanvas; ctx = _savedCtx;
                    viewOffset = _savedViewOffset; viewCount = _savedViewCount;
                    chartData = _savedChartData; currentFreq = _savedFreq;
                    // 双击K线上无效，双击空白处
                    if (clickedOnKline) return;
                    // 状态A：让下面窗口平移到对应区间
                    if (dualOffscreenState && dualHighlightRange && dualBottomData) {
                        const hr = dualHighlightRange;
                        if (hr.startIdx >= 0 && hr.endIdx >= 0) {
                            const centerIdx = (hr.startIdx + hr.endIdx) / 2;
                            const totalKlines = dualBottomData.klines.length;
                            let newOffset = Math.round(centerIdx - dualBottomViewCount / 2);
                            if (newOffset < 0) newOffset = 0;
                            const maxOffset = Math.max(0, totalKlines - dualBottomViewCount);
                            if (newOffset > maxOffset) newOffset = maxOffset;
                            dualBottomViewOffset = newOffset;
                            dualHighlightRange = calcDualHighlight(mouseX);
                            dualRedRange = dualHighlightRange ? dualHighlightRange.redRange : null;
                            dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                        } else {
                            showDualToast("请加载更多K线...");
                        }
                        renderBottom();
                        return;
                    }
                    // 默认：恢复下面窗口全视图
                    dualBottomViewCount = 377;
                    dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                    if (dualBottomData.klines.length < dualBottomViewCount) {
                        dualBottomViewOffset = 0;
                    }
                    renderBottom();
                });
                // 恢复crosshair-info
                const crosshairInfo = document.createElement("div");
                crosshairInfo.className = "crosshair-info";
                crosshairInfo.id = "crosshair-info";
                container.appendChild(crosshairInfo);
                resizeCanvas();
                // 请求下面窗口数据
                const code = chartData.meta.symbol;
                document.getElementById("loading").classList.remove("hidden");
                document.querySelector(".loading-text").textContent = "正在加载" + (bottomFreq === 'd' ? '日K' : bottomFreq === '30m' ? '30分' : '5分') + "数据...";
                fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + bottomFreq)
                    .then(resp => {
                        if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                        return resp.json();
                    })
                    .then(data => {
                        dualBottomData = data;
                        dualBottomViewCount = 377;
                        dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                        if (dualBottomData.klines.length < dualBottomViewCount) {
                            dualBottomViewOffset = 0;
                        }
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        render();
                    })
                    .catch(err => {
                        alert("加载下面窗口数据失败: " + err.message);
                        // 回退到单窗口
                        isDualWindow = false;
                        activeDualWindow = 'top';
                        dualBottomData = null;
                        dualBottomFreq = '';
                        btn.classList.remove("active");
                        const container2 = document.getElementById("chart-container");
                        container2.innerHTML = '';
                        const ci2 = document.createElement("div");
                        ci2.className = "crosshair-info";
                        ci2.id = "crosshair-info";
                        container2.appendChild(ci2);
                        container2.appendChild(origCanvas);
                        canvas = topCanvas; ctx = topCtx;
                        resizeCanvas();
                        render();
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    });
            }
        };

        // 下面窗口的事件处理
        function onBottomWheel(e) {
            e.preventDefault();
            if (!dualBottomData) return;
            activeDualWindow = 'bottom';
            updateActiveWindowClass();
            updateSlider();
            const savedCanvas = canvas; const savedCtx = ctx;
            const savedViewOffset = viewOffset; const savedViewCount = viewCount;
            canvas = bottomCanvas; ctx = bottomCtx;
            viewOffset = dualBottomViewOffset; viewCount = dualBottomViewCount;
            const rect = bottomCanvas.getBoundingClientRect();
            const bMouseX = e.clientX - rect.left;
            const area = getChartArea();
            const klines = dualBottomData.klines;
            const barStep = area.w / viewCount;
            const ratio = Math.max(0, Math.min(1, (bMouseX - area.x) / area.w));
            const mouseKIdx = ratio * viewCount;
            const zoomFactor = 1.15;
            const newViewCount = e.deltaY > 0
                ? Math.min(klines.length, Math.ceil(viewCount * zoomFactor))
                : Math.max(3, Math.round(viewCount / zoomFactor));
            if (newViewCount === viewCount) { canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount; return; }
            const maxOffset = klines.length - newViewCount;
            if (mouseKIdx >= viewCount - 1) {
                const rightGlobalIdx = viewOffset + viewCount - 1;
                dualBottomViewCount = newViewCount;
                dualBottomViewOffset = Math.max(0, Math.min(maxOffset, rightGlobalIdx - newViewCount + 1));
            } else {
                const anchorGlobalIdx = viewOffset + mouseKIdx;
                let newViewOffset = anchorGlobalIdx - ratio * newViewCount;
                newViewOffset = Math.max(0, newViewOffset);
                if (newViewOffset > maxOffset) newViewOffset = maxOffset;
                dualBottomViewCount = newViewCount;
                dualBottomViewOffset = newViewOffset;
            }
            canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
            renderBottom();
        }

        function onBottomMouseDown(e) {
            dualBottomIsDragging = true;
            dualBottomDragStartX = e.clientX;
            dualBottomDragStartOffset = dualBottomViewOffset;
            dualBottomMouseDownX = e.clientX;
            dualBottomMouseDownY = e.clientY;
            bottomCanvas.style.cursor = "grabbing";
            activeDualWindow = 'bottom';
            updateActiveWindowClass();
            updateSlider();
        }

        function onBottomMouseMove(e) {
            const rect = bottomCanvas.getBoundingClientRect();
            dualBottomMouseX = e.clientX - rect.left;
            dualBottomMouseY = e.clientY - rect.top;
            if (dualBottomIsDragging && dualBottomData) {
                const savedCanvas = canvas; const savedCtx = ctx;
                const savedViewOffset = viewOffset; const savedViewCount = viewCount;
                canvas = bottomCanvas; ctx = bottomCtx;
                viewOffset = dualBottomViewOffset; viewCount = dualBottomViewCount;
                dualBottomViewOffset = dualBottomDragStartOffset - (e.clientX - dualBottomDragStartX) / (getChartArea().w / viewCount);
                dualBottomViewOffset = Math.max(0, Math.min(dualBottomData.klines.length - dualBottomViewCount, dualBottomViewOffset));
                canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
            }
            renderBottom();
        }

        function onBottomMouseUp(e) {
            dualBottomIsDragging = false;
            bottomCanvas.style.cursor = "crosshair";
            // 只处理左键点击（非拖拽）
            if (e.button !== 0 || Math.abs(e.clientX - dualBottomMouseDownX) >= 5 || Math.abs(e.clientY - dualBottomMouseDownY) >= 5) return;
            if (_bottomCurrentGlobalIdx < 0 || !dualBottomData) return;

            // === Ctrl+点击：区间选择模式切换（底部窗口）===
            if (e.ctrlKey) {
                if (_rangeSelect.mode === 'IDLE') {
                    _rangeSelect = {
                        mode: 'SELECTED_A',
                        startIdx: _bottomCurrentGlobalIdx,
                        startFreq: dualBottomFreq,
                        startSymbol: dualBottomData.meta.symbol
                    };
                    const startDate = dualBottomData.klines[_bottomCurrentGlobalIdx].date.split(' ')[0];
                    showDualToast("区间起点: " + startDate + "，点击另一根K线完成选择");
                    renderBottom();
                } else {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("区间选择已取消");
                    renderBottom();
                }
                return;
            }

            // === 普通点击：如果在选择模式中，完成区间选择（底部窗口）===
            if (_rangeSelect.mode === 'SELECTED_A') {
                // 验证：同一股票、同一周期
                if (_rangeSelect.startFreq !== dualBottomFreq || _rangeSelect.startSymbol !== dualBottomData.meta.symbol) {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("股票或周期已变更，区间选择已取消");
                    return;
                }
                const a = Math.min(_rangeSelect.startIdx, _bottomCurrentGlobalIdx);
                const b = Math.max(_rangeSelect.startIdx, _bottomCurrentGlobalIdx);
                const klines = dualBottomData.klines;
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                const lines = [];
                for (let i = a; i <= b; i++) {
                    const k = klines[i];
                    const prevK = i > 0 ? klines[i - 1] : null;
                    const prevClose = prevK ? prevK.close : k.open;
                    const changeVal = k.close - prevClose;
                    const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
                    const sign = changeVal >= 0 ? "+" : "";
                    const wd = "周" + weekDays[new Date(k.date.replace(" ", "T")).getDay()];
                    lines.push(`${k.date} ${wd} 开:${k.open.toFixed(2)} 高:${k.high.toFixed(2)} 低:${k.low.toFixed(2)} 收:${k.close.toFixed(2)} 涨跌:${sign}${changeVal.toFixed(2)} 涨幅:${sign}${changePct}%`);
                }
                navigator.clipboard.writeText(lines.join("\n")).catch(() => {});
                showDualToast("已复制 " + (b - a + 1) + " 根K线数据到剪贴板");
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                renderBottom();
                return;
            }

            // === 普通模式：复制当前K线信息 ===
            if (_bottomClipText) {
                navigator.clipboard.writeText(_bottomClipText).catch(() => {});
            }
        }

        function onBottomMouseLeave() {
            dualBottomIsDragging = false;
            dualBottomMouseX = -1; dualBottomMouseY = -1;
            bottomCanvas.style.cursor = "crosshair";
            renderBottom();
        }

        window.toggleOverlay = function(type) {
            if (type === "bi") { showBi = !showBi; document.getElementById("btn-bi").classList.toggle("active", showBi); }
            else if (type === "fx") { showFx = !showFx; document.getElementById("btn-fx").classList.toggle("active", showFx); }
            else if (type === "ma") { showMa = !showMa; document.getElementById("btn-ma").classList.toggle("active", showMa); }
            else if (type === "zs") { showZs = !showZs; document.getElementById("btn-zs").classList.toggle("active", showZs); }
            else if (type === "seg") { showSeg = !showSeg; document.getElementById("btn-seg").classList.toggle("active", showSeg); }
            else if (type === "bsp") { showBsp = !showBsp; document.getElementById("btn-bsp").classList.toggle("active", showBsp); }
            render();
        };

        window.toggleStats = function() { document.getElementById("stats-panel").classList.toggle("show"); };

        // 辅助：根据chartData中的saved_selection_date恢复重启按钮状态
        function updateRestartBtn() {
            var hasPoint = chartData && chartData.meta && chartData.meta.saved_selection_date;
            document.getElementById("btn-restart").disabled = !hasPoint;
        }

        function updateDualBtn() {
            document.getElementById("btn-dual").disabled = (currentFreq === '5m');
        };

        // ============================================================
        // 重启：清除选点，按冷启动重新加载
        // ============================================================
        window.restartStock = function() {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            const isFutures = chartData.meta.market === 'futures';
            document.getElementById("btn-restart").disabled = true;
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在重置...";

            // 期货：清除选点 + 冷启动重连SSE（无start_time）
            if (isFutures) {
                fetch("/api/futures_clear_saved_point?symbol=" + encodeURIComponent(code) + "&freq=" + freq)
                    .then(resp => resp.json())
                    .then(() => {
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        connectRealtimeInit(code, freq);  // 冷启动，不带start_time
                    })
                    .catch(err => {
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        document.getElementById("btn-restart").disabled = false;
                        alert("重置失败: " + err.message);
                    });
                return;
            }

            // 股票：清除选点 + 冷启动HTTP
            // Step 1: 调用后端清除CSV中该周期选点
            fetch("/api/clear_saved_point?code=" + encodeURIComponent(code) + "&freq=" + freq)
                .then(resp => resp.json())
                .then(() => {
                    // Step 2: 冷启动重新加载
                    return fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq);
                })
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "重置失败"); });
                    return resp.json();
                })
                .then(data => {
                    // 全文替换 chartData
                    chartData = data;
                    if (chartData.meta.freq === "5分钟") {
                        currentFreq = "5m";
                    } else if (chartData.meta.freq === "30分钟") {
                        currentFreq = "30m";
                    } else if (chartData.meta.freq === "周线") {
                        currentFreq = "w";
                    } else {
                        currentFreq = "d";
                    }
                    document.getElementById("btn-d").classList.toggle("active", currentFreq === "d");
                    document.getElementById("btn-w").classList.toggle("active", currentFreq === "w");
                    document.getElementById("btn-30m").classList.toggle("active", currentFreq === "30m");
                    document.getElementById("btn-5m").classList.toggle("active", currentFreq === "5m");
                    viewCount = 377;
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    const lastDate = chartData.klines[chartData.klines.length - 1].date.slice(0, 10);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    resizeCanvas();
                    render();
                    generateStats();
                    updateRestartBtn();
                    updateDualBtn();
                })
                .catch(err => {
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    document.getElementById("btn-restart").disabled = false;
                    alert("重置失败: " + err.message);
                });
        };

        // ============================================================
        // 自选股买卖点扫描（逐只扫描，实时进度，可中断）
        // ============================================================
        let _scanRunning = false;
        let _scanAborted = false;
        let _scanMode = "ann"; // "ann" = 标注扫描, "bsp" = 买卖点扫描

        // 扫描模式对话框：取消
        window.scanModeDialogCancel = function() {
            document.getElementById("scan-mode-dialog").classList.remove("show");
        };

        // 扫描模式对话框：确认
        window.scanModeDialogConfirm = function() {
            var selected = document.querySelector('input[name="scan-mode"]:checked');
            if (!selected) return;
            _scanMode = selected.value;
            document.getElementById("scan-mode-dialog").classList.remove("show");
            // 执行实际扫描
            doStartScan();
        };

        function updateScanTitle() {
            var freq = currentFreq;
            var freqLabels = {"d": "日K", "w": "周K", "30m": "30分", "5m": "5分"};
            var modeLabel = _scanMode === "ann" ? "标注" : "买/卖点";
            document.getElementById("scan-title").textContent = modeLabel + "（" + (freqLabels[freq] || freq) + "）";
        }

        window.startScanZxg = function() {
            if (_scanRunning) {
                // 正在扫描中，再次点击 = 中断扫描
                _scanAborted = true;
                return;
            }
            // 弹出模式选择对话框
            document.getElementById("scan-mode-dialog").classList.add("show");
        };

        // 实际执行扫描（由对话框确认后调用）
        function doStartScan() {
            var panel = document.getElementById("scan-panel");
            var body = document.getElementById("scan-body");
            var status = document.getElementById("scan-status");
            var btn = document.getElementById("btn-scan");

            panel.classList.add("show");
            btn.classList.add("active");
            _scanRunning = true;
            _scanAborted = false;

            var freq = currentFreq;
            updateScanTitle();
            status.textContent = "";

            // 标注扫描模式：直接查询标注缓存
            if (_scanMode === "ann") {
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在查询标注数据...</div>';
                // 获取自选股列表 + 标注缓存
                Promise.all([
                    fetch("/api/zxg_list"),
                    fetch("/api/annotations_scan?freq=" + freq)
                ])
                .then(function(resps) {
                    return Promise.all([resps[0].json(), resps[1].json()]);
                })
                .then(function(dataArr) {
                    var zxgData = dataArr[0];
                    var annData = dataArr[1];
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "自选扫描";

                    if (!zxgData.stocks || zxgData.stocks.length === 0) {
                        body.innerHTML = '<div class="scan-no-result">自选股为空或文件不存在</div>';
                        return;
                    }

                    // 构建标注代码集合（key: "code.market"，如 "000001.SH"）
                    var annotatedCodes = {};
                    (annData.codes || []).forEach(function(c) {
                        annotatedCodes[c.code + "." + c.market] = c;
                    });

                    // 交叉匹配：自选股中有标注的（用复合key匹配，避免000001.SH/000001.SZ冲突）
                    var results = [];
                    zxgData.stocks.forEach(function(stk) {
                        var market = stk.prefix === "1" ? "SH" : stk.prefix === "0" ? "SZ" : stk.prefix === "2" ? "BJ" : stk.prefix.toUpperCase();
                        var lookupKey = stk.code + "." + market;
                        var ann = annotatedCodes[lookupKey];
                        if (ann) {
                            results.push({
                                code: stk.code + "." + market,
                                name: ann.name || (stk.code + "." + market),
                                freq: ann.freq,
                                count: ann.count,
                                annotations: ann.annotations || []
                            });
                        }
                    });

                    var html = '<div class="scan-summary">自选股 <b>' + zxgData.stocks.length + '</b> 只，有标注 <b>' + results.length + '</b> 只</div>';
                    if (results.length === 0) {
                        html += '<div class="scan-no-result">当前周期下未发现标注股票</div>';
                    } else {
                        results.forEach(function(r) {
                            // 取日期最靠近当前日期的标注文字，最多8字
                            var closestText = "";
                            if (r.annotations && r.annotations.length > 0) {
                                var today = new Date();
                                today.setHours(0, 0, 0, 0);
                                var closest = null;
                                var closestDiff = Infinity;
                                r.annotations.forEach(function(a) {
                                    var d = new Date(a.date);
                                    if (isNaN(d.getTime())) return;
                                    var diff = Math.abs(d - today);
                                    if (diff < closestDiff) {
                                        closestDiff = diff;
                                        closest = a.text;
                                    }
                                });
                                if (closest) {
                                    closestText = closest.length > 11 ? closest.substring(0, 11) + "..." : closest;
                                }
                            }
                            html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\')" title="点击查看K线图">';
                            html += '<span class="scan-col-name">' + r.name + '</span>';
                            html += '<span class="scan-col-code">' + r.code + '</span>';
                            html += '<span class="scan-col-ann">' + closestText + '</span>';
                            html += '<span class="scan-col-tags"><span class="scan-bsp-tag buy1">' + r.count + '条</span></span>';
                            html += '</div>';
                        });
                    }
                    body.innerHTML = html;
                })
                .catch(function(err) {
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "自选扫描";
                    body.innerHTML = '<div class="scan-no-result">查询失败: ' + err.message + '</div>';
                });
                return;
            }

            // 买卖点扫描模式（原有逻辑）
            // 第一步：通知后端开始新扫描 + 获取自选股列表
            body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取自选股列表...</div>';
            Promise.all([
                fetch("/api/scan_start"),
                fetch("/api/zxg_list")
            ])
                .then(function(resps) {
                    // 先检查 scan_start 的响应
                    return resps[0].json().then(function(scanStartData) {
                        if (scanStartData.need_refresh) {
                            // 需要刷新GBBQ缓存
                            _scanRunning = false;
                            btn.classList.remove("active");
                            body.innerHTML = '<div class="scan-no-result" style="text-align:center;padding:20px;">' +
                                '<div style="font-size:14px;color:#e94560;margin-bottom:12px;">&#9888; ' + scanStartData.msg + '</div>' +
                                '<button class="btn" onclick="refreshGbbq();closeScanPanel();" style="margin-top:8px;">立即刷新</button>' +
                                '</div>';
                            return null;
                        }
                        // scan_start 正常，继续处理自选股列表
                        return resps[1].json();
                    });
                })
                .then(function(data) {
                    if (data === null) return; // 已处理 need_refresh
                    if (!data.stocks || data.stocks.length === 0) {
                        _scanRunning = false;
                        btn.classList.remove("active");
                        body.innerHTML = '<div class="scan-no-result">自选股为空或文件不存在</div>';
                        return;
                    }
                    var stocks = data.stocks;
                    var total = stocks.length;
                    var results = [];
                    var skipped = 0;
                    var currentIdx = 0;
                    var completed = 0;
                    var hasRenderedAny = false;

                    // 扫描结束统一通知后端打印
                    function finishScan(interrupted) {
                        fetch("/api/scan_end").then(function() {
                            renderScanResults(results, interrupted ? completed : total, skipped, interrupted);
                        });
                    }

                    // 实时更新面板：显示进度 + 已找到的买卖点股票
                    function updatePanel() {
                        var progress = completed + "/" + total;
                        var found = results.length;
                        var html = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + progress + '，已发现 ' + found + ' 只股票</div>';
                        // 如果已经找到一些结果，实时显示出来
                        if (results.length > 0) {
                            hasRenderedAny = true;
                            html += '<div class="scan-summary" style="margin-top:8px;">已发现 <b>' + found + '</b> 只有买/卖点</div>';
                            for (var i = 0; i < results.length; i++) {
                                var r = results[i];
                                var tagsHtml = '<div class="scan-bsp-tags">';
                                for (var j = 0; j < (r.buy_points || []).length; j++) {
                                    var bp = r.buy_points[j];
                                    var tp = bp.type.toLowerCase().replace(/\s/g, "");
                                    var cls = "buya";
                                    if (tp === "1") cls = "buy1";
                                    else if (tp === "2") cls = "buy2";
                                    else if (tp === "3a") cls = "buy3";
                                    else if (tp === "1p") cls = "buy1p";
                                    else if (tp === "2s") cls = "buy2s";
                                    else if (tp === "3b") cls = "buy3b";
                                    tagsHtml += '<span class="scan-bsp-tag ' + cls + '">' + bp.type + '</span>';
                                }
                                for (var j = 0; j < (r.sell_points || []).length; j++) {
                                    var sp = r.sell_points[j];
                                    var tp = sp.type.toLowerCase().replace(/\s/g, "");
                                    var cls = "sella";
                                    if (tp === "1") cls = "sell1";
                                    else if (tp === "2") cls = "sell2";
                                    else if (tp === "3a") cls = "sell3";
                                    else if (tp === "1p") cls = "sell1p";
                                    else if (tp === "2s") cls = "sell2s";
                                    else if (tp === "3b") cls = "sell3b";
                                    tagsHtml += '<span class="scan-bsp-tag ' + cls + '">' + sp.type + '</span>';
                                }
                                tagsHtml += '</div>';
                                var mvText = '';
                                if (r.float_mv !== undefined && r.float_mv !== null && r.float_mv < 50) {
                                    mvText = r.float_mv.toFixed(1) + '亿';
                                }
                                var maText = '';
                                if (r.below_ma120 === true) {
                                    maText = '牛熊线下';
                                }
                                html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\')" title="点击查看K线图">';
                                html += '<span class="scan-col-name">' + r.name + '</span>';
                                html += '<span class="scan-col-code">' + r.code + '</span>';
                                html += '<span class="scan-col-mv">' + mvText + '</span>';
                                html += '<span class="scan-col-ma">' + maText + '</span>';
                                html += '<span class="scan-col-tags">' + tagsHtml + '</span>';
                                html += '</div>';
                            }
                        }
                        body.innerHTML = html;
                    }

                    function checkDone() {
                        if (_scanAborted) {
                            _scanRunning = false;
                            _scanAborted = false;
                            btn.classList.remove("active");
                            btn.textContent = "自选扫描";
                            finishScan(true);
                            return;
                        }
                        if (completed >= total) {
                            _scanRunning = false;
                            btn.classList.remove("active");
                            btn.textContent = "自选扫描";
                            finishScan(false);
                            return;
                        }
                    }

                    // 第二步：并发扫描（同时发送多个请求）
                    var CONCURRENCY = 5;  // 同时扫描5只
                    btn.textContent = "中断扫描";

                    function launchBatch() {
                        if (_scanAborted) return;
                        var batch = [];
                        while (currentIdx < total && batch.length < CONCURRENCY) {
                            batch.push(stocks[currentIdx]);
                            currentIdx++;
                        }
                        if (batch.length === 0) return;
                        batch.forEach(function(stk) {
                            var code = stk.code;
                            var prefix = stk.prefix;
                            fetch("/api/scan_one?code=" + code + "&freq=" + freq + "&prefix=" + prefix + "&_t=" + Date.now())
                                .then(function(resp) { return resp.json(); })
                                .then(function(data) {
                                    completed++;
                                    if (data.error) {
                                        skipped++;
                                    } else if ((data.buy_points && data.buy_points.length > 0) || (data.sell_points && data.sell_points.length > 0)) {
                                        results.push(data);
                                    }
                                    updatePanel();
                                    if (currentIdx < total) {
                                        launchBatch();
                                    } else {
                                        checkDone();
                                    }
                                })
                                .catch(function(err) {
                                    completed++;
                                    skipped++;
                                    updatePanel();
                                    if (currentIdx < total) {
                                        launchBatch();
                                    } else {
                                        checkDone();
                                    }
                                });
                        });
                    }

                    // 启动初始批次
                    for (var i = 0; i < CONCURRENCY; i++) {
                        launchBatch();
                    }
                })
                .catch(function(err) {
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "自选扫描";
                    body.innerHTML = '<div class="scan-no-result">读取自选股失败: ' + err.message + '</div>';
                });
        };

        // ============================================================
        // GBBQ数据刷新（后台线程解密，轮询进度）
        // ============================================================
        window.refreshGbbq = function() {
            var btn = document.getElementById("btn-refresh");
            var status = document.getElementById("refresh-status");
            if (btn.disabled) return;
            btn.disabled = true;
            btn.classList.add("active");

            // 给刷新图标添加旋转动画
            btn.querySelector("svg").style.animation = "spin 1s linear infinite";
            status.style.display = "inline";
            status.textContent = "正在刷新GBBQ文件...";

            fetch("/api/gbbq_refresh")
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.status === "already_running") {
                        pollGbbqStatus(btn, status);
                    } else {
                        pollGbbqStatus(btn, status);
                    }
                })
                .catch(function(err) {
                    btn.disabled = false;
                    btn.classList.remove("active");
                    btn.querySelector("svg").style.animation = "";
                    status.style.display = "none";
                    alert("启动刷新失败: " + err.message);
                });
        };

        function pollGbbqStatus(btn, status) {
            fetch("/api/gbbq_status")
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.running) {
                        status.textContent = data.step || "刷新中...";
                        setTimeout(function() { pollGbbqStatus(btn, status); }, 500);
                    } else {
                        btn.disabled = false;
                        btn.classList.remove("active");
                        btn.querySelector("svg").style.animation = "";
                        if (data.error) {
                            status.textContent = "刷新失败";
                            alert("刷新失败: " + data.error);
                        } else {
                            status.textContent = "刷新完成";
                            setTimeout(function() { status.style.display = "none"; }, 2000);
                        }
                    }
                })
                .catch(function() {
                    btn.disabled = false;
                    btn.classList.remove("active");
                    btn.querySelector("svg").style.animation = "";
                    status.style.display = "none";
                });
        }

        function renderScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var html = '<div class="scan-summary">自选股 <b>' + total + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，跳过 <b>' + skipped + '</b> 只，发现 <b>' + results.length + '</b> 只有买/卖点' + label + '</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现买卖点股票</div>';
            } else {
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    var tagsHtml = '<div class="scan-bsp-tags">';
                    // 买点标签
                    for (var j = 0; j < (r.buy_points || []).length; j++) {
                        var bp = r.buy_points[j];
                        var tp = bp.type.toLowerCase().replace(/\s/g, "");
                        var cls = "buya";
                        if (tp === "1") cls = "buy1";
                        else if (tp === "2") cls = "buy2";
                        else if (tp === "3a") cls = "buy3";
                        else if (tp === "1p") cls = "buy1p";
                        else if (tp === "2s") cls = "buy2s";
                        else if (tp === "3b") cls = "buy3b";
                        tagsHtml += '<span class="scan-bsp-tag ' + cls + '">' + bp.type + '</span>';
                    }
                    // 卖点标签
                    for (var j = 0; j < (r.sell_points || []).length; j++) {
                        var sp = r.sell_points[j];
                        var tp = sp.type.toLowerCase().replace(/\s/g, "");
                        var cls = "sella";
                        if (tp === "1") cls = "sell1";
                        else if (tp === "2") cls = "sell2";
                        else if (tp === "3a") cls = "sell3";
                        else if (tp === "1p") cls = "sell1p";
                        else if (tp === "2s") cls = "sell2s";
                        else if (tp === "3b") cls = "sell3b";
                        tagsHtml += '<span class="scan-bsp-tag ' + cls + '">' + sp.type + '</span>';
                    }
                    tagsHtml += '</div>';
                    // 构建各列内容
                    var mvText2 = '';
                    if (r.float_mv !== undefined && r.float_mv !== null && r.float_mv < 50) {
                        mvText2 = r.float_mv.toFixed(1) + '亿';
                    }
                    var maText2 = '';
                    if (r.below_ma120 === true) {
                        maText2 = '牛熊线下';
                    }
                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\')" title="点击查看K线图">';
                    html += '<span class="scan-col-name">' + r.name + '</span>';
                    html += '<span class="scan-col-code">' + r.code + '</span>';
                    html += '<span class="scan-col-mv">' + mvText2 + '</span>';
                    html += '<span class="scan-col-ma">' + maText2 + '</span>';
                    html += '<span class="scan-col-tags">' + tagsHtml + '</span>';
                    html += '</div>';
                }
            }
            body.innerHTML = html;
        }

        window.closeScanPanel = function() {
            // 扫描中不允许关闭面板，用户需通过"中断扫描"按钮停止
            if (_scanRunning) return;
            document.getElementById("scan-panel").classList.remove("show");
        };

        window.loadScanResult = function(code) {
            // 加载该股票到当前页面，不关闭面板
            document.getElementById("stock-code-input").value = code;
            loadStock();
        };

        window.switchFreq = function(freq) {
            if (!chartData || currentFreq === freq) return;
            // 切换周期时取消区间选择
            if (_rangeSelect.mode === 'SELECTED_A') {
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
            }
            currentFreq = freq;
            updateDualBtn();
            const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            if (isFutures) {
                lastFuturesFreq = freq; // 期货上下文切换周期，记录
            } else {
                lastStockFreq = freq;   // 股票上下文切换周期，记录
            }
            updateFreqButtonStates(isFutures);
            // 双窗口模式下，如果新周期是5m，自动关闭双窗口
            if (isDualWindow && freq === '5m') {
                isDualWindow = false;
                activeDualWindow = 'top';
                dualBottomData = null;
                dualBottomFreq = '';
                dualHighlightRange = null;
                dualRedRange = null;
                dualNewZsData = null;
                dualShowNewZs = false;
                dualNewZsStartBi = -1;
                dualNewZsEndBi = -1;
                document.getElementById("btn-dual").classList.remove("active");
                // 恢复单canvas布局
                const container = document.getElementById("chart-container");
                const topDiv = document.getElementById("chart-top");
                const bottomDiv = document.getElementById("chart-bottom");
                if (topDiv) topDiv.remove();
                if (bottomDiv) bottomDiv.remove();
                canvas = topCanvas; ctx = topCtx;
                container.appendChild(canvas);
                const ci = document.createElement("div");
                ci.className = "crosshair-info"; ci.id = "crosshair-info";
                container.appendChild(ci);
                resizeCanvas();
            }
            // 切换周期后重新加载数据
            const code = document.getElementById("stock-code-input").value.trim() || chartData.meta.symbol;
            if (code) {
                document.getElementById("loading").classList.remove("hidden");
                // 期货：跳过HTTP，直接重连SSE（初始快照+增量合一）
                const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
                if (isFutures) {
                    disconnectRealtime();
                    connectRealtimeInit(code, freq);
                    return;
                }
                fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq)
                    .then(resp => {
                        if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                        return resp.json();
                    })
                    .then(data => {
                        chartData = data;
                        updateRestartBtn();
                        updateDualBtn();
                        viewCount = 377;
                        adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                        viewOffset = Math.max(0, chartData.klines.length - viewCount);
                        // K线不足一屏时右对齐
                        if (chartData.klines.length < viewCount) {
                            viewOffset = 0;
                        }
                        document.getElementById("stock-name").textContent = chartData.meta.name;
                        document.getElementById("stock-code").textContent = chartData.meta.symbol;
                        document.title = "缠论分析 - " + chartData.meta.name;
                        const lastDate = chartData.klines[chartData.klines.length - 1].date.slice(0, 10);
                        document.getElementById("goto-date-input").value = lastDate;
                        updateWeekday();
                        // 双窗口模式下同时更新下面窗口
                        if (isDualWindow) {
                            const newBottomFreq = getDualBottomFreq(freq);
                            if (newBottomFreq) {
                                dualBottomFreq = newBottomFreq;
                                return fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + newBottomFreq)
                                    .then(resp => resp.json())
                                    .then(bottomData => {
                                        dualBottomData = bottomData;
                                        dualBottomViewCount = 377;
                                        dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                                        if (dualBottomData.klines.length < dualBottomViewCount) {
                                            dualBottomViewOffset = 0;
                                        }
                                        document.getElementById("loading").classList.add("hidden");
                                        render();
                                        generateStats();
                                        loadAnnotations();
                                        startRealtimeIfFutures(data);
                                    });
                            } else {
                                // 新周期是5m，双窗口已关闭
                                document.getElementById("loading").classList.add("hidden");
                                render();
                                generateStats();
                                loadAnnotations();
                                startRealtimeIfFutures(data);
                            }
                        } else {
                            document.getElementById("loading").classList.add("hidden");
                            render();
                            generateStats();
                            loadAnnotations();
                            startRealtimeIfFutures(data);
                        }
                    })
                    .catch(err => {
                        alert("切换周期失败: " + err.message);
                        document.getElementById("loading").classList.add("hidden");
                    });
            }
        };

        // 根据保存的选点日期，动态调整 viewCount 和 viewOffset
        // 选点后后端已过滤，klines只包含选点之后的K线，直接全部显示
        function adjustViewForSavedPoint() {
            if (!chartData || !chartData.meta) return;
            if (!chartData.meta.saved_selection_date) return;
            if (!chartData.klines || chartData.klines.length === 0) return;
            viewCount = chartData.klines.length;
            viewOffset = 0;
        }

        window.gotoDate = function() {
            if (!chartData) return;
            // 复盘模式下断开实时连接
            disconnectRealtime();
            const dateStr = document.getElementById("goto-date-input").value.trim();
            if (!dateStr) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            const url = "/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq + "&end_date=" + encodeURIComponent(dateStr);
            document.getElementById("goto-date-input").disabled = true;
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在复盘计算，请稍候...";
            fetch(url)
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "跳转失败"); });
                    return resp.json();
                })
                .then(data => {
                    chartData = data;
                    updateRestartBtn();
                    updateDualBtn();
                    viewCount = 377;
                    adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    resizeCanvas();
                    render();
                    loadAnnotations();
                })
                .catch(err => {
                    alert("跳转失败: " + err.message);
                })
                .finally(() => {
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    document.getElementById("goto-date-input").disabled = false;
                });
        };

        let _dateKeyArrow = false, _dateKeyEnter = false, _dateManualTyping = false, _dateStepIgnore = false;
        window.handleDateKeydown = function(e) {
            if (e.key === 'Enter') { _dateKeyEnter = true; gotoDate(); return; }
            if (e.key.startsWith('Arrow')) { _dateKeyArrow = true; return; }
            if (e.key !== 'Tab' && e.key !== 'Escape') { _dateManualTyping = true; }
        };
        window.handleDateChange = function() {
            if (_dateStepIgnore) return;
            updateWeekday();
            if (_dateKeyEnter) { _dateKeyEnter = false; return; }
            if (_dateKeyArrow) { _dateKeyArrow = false; return; }
            if (_dateManualTyping) { _dateManualTyping = false; return; }
            gotoDate();
        };
        window.handleDateBlur = function() {
            const input = document.getElementById("goto-date-input");
            const v = input.value;
            if (!v) return;
            const parts = v.split('-');
            if (parts.length === 3) {
                const d = parseInt(parts[2], 10);
                if (!isNaN(d) && d > 31) {
                    input.value = parts[0] + '-' + parts[1] + '-31';
                }
            }
            updateWeekday();
        };
        window.handleDateInput = function(e) {
            const input = e.target;
            const val = input.value;
            if (!val) return;
            // 年份部分超过4位时截断到4位，并尝试将焦点移到月份位置
            const firstDash = val.indexOf('-');
            if (firstDash === -1) {
                // 值中还没有 '-'，可能是纯年份数字
                if (val.length > 4) {
                    input.value = val.substring(0, 4);
                    setTimeout(() => { try { input.setSelectionRange(5, 5); } catch(_) {} }, 10);
                }
            } else {
                const yearStr = val.substring(0, firstDash);
                if (yearStr.length > 4) {
                    const rest = val.substring(firstDash);
                    input.value = yearStr.substring(0, 4) + rest;
                    setTimeout(() => { try { input.setSelectionRange(5, 5); } catch(_) {} }, 10);
                }
            }
            updateWeekday();
        };

        window.dateStep = function(delta) {
            if (!chartData || !chartData.klines || chartData.klines.length === 0) return;
            var input = document.getElementById("goto-date-input");
            var baseDate = input.value.trim();
            if (!baseDate) return;
            var parts = baseDate.split('-');
            if (parts.length !== 3) return;
            var d = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
            d.setDate(d.getDate() + delta);
            // 右箭头：不允许超过今天实际日期
            if (delta > 0) {
                var today = new Date();
                var todayD = new Date(today.getFullYear(), today.getMonth(), today.getDate());
                if (d > todayD) return;
            }
            var yyyy = d.getFullYear();
            var mm = String(d.getMonth() + 1).padStart(2, '0');
            var dd = String(d.getDate()).padStart(2, '0');
            _dateStepIgnore = true;
            input.value = yyyy + '-' + mm + '-' + dd;
            updateWeekday();
            gotoDate();
            _dateStepIgnore = false;
        };

        window.updateWeekday = function() {
            var input = document.getElementById("goto-date-input");
            var span = document.getElementById("date-weekday");
            var v = input.value.trim();
            if (!v) { span.textContent = ""; return; }
            var parts = v.split('-');
            if (parts.length !== 3) { span.textContent = ""; return; }
            var d = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
            var weekNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
            span.textContent = weekNames[d.getDay()];
        };

        const HISTORY_KEY = "chan_stock_history";
        const MAX_HISTORY = 10;
        function getHistory() {
            try {
                let list = JSON.parse(localStorage.getItem(HISTORY_KEY)) || [];
                // 兼容旧格式（纯字符串）-> 转换为新格式（{code, name}）
                return list.map(c => typeof c === 'string' ? {code: c, name: ""} : c);
            } catch(e) { return []; }
        }
        function saveHistory(code, name) {
            let list = getHistory();
            list = list.filter(c => c.code !== code);
            list.unshift({code: code, name: name || ""});
            if (list.length > MAX_HISTORY) list = list.slice(0, MAX_HISTORY);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
        }
        function removeHistory(code) {
            let list = getHistory().filter(c => c.code !== code);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
            showHistory();
        }
        window.clearHistory = function() {
            localStorage.removeItem(HISTORY_KEY);
            document.getElementById("stock-history").classList.remove("show");
        };
        window.clearInput = function() {
            const input = document.getElementById("stock-code-input");
            input.value = "";
            input.focus();
            document.getElementById("input-clear").style.display = "none";
        };
        let searchTimer = null;
        let searchResults = [];
        let selectedIndex = -1;
        window.onInputChange = function() {
            const input = document.getElementById("stock-code-input");
            document.getElementById("input-clear").style.display = input.value ? "" : "none";
            selectedIndex = -1;
            const val = input.value.trim();
            if (!val) {
                document.getElementById("stock-history").classList.remove("show");
                return;
            }
            // 带市场前缀+数字（如 sh600519），不搜索；纯拼音（如 SZCZ）正常搜索
            if (/^(sh|sz|bj|hk)\d/i.test(val)) {
                document.getElementById("stock-history").classList.remove("show");
                return;
            }
            // 纯数字（6位）也搜索，可能有同名（如000001=平安银行/上证指数）
            // 拼音或中文，延迟搜索
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => doSearch(val), 300);
        };
        window.onInputKeydown = function(e) {
            const el = document.getElementById("stock-history");
            if (!el.classList.contains("show") || !searchResults.length) {
                if (e.key === "Enter") loadStock();
                return;
            }
            const items = el.querySelectorAll(".stock-history-item");
            if (e.key === "ArrowDown") {
                e.preventDefault();
                selectedIndex = (selectedIndex + 1) % items.length;
                updateSearchSelection(items);
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                selectedIndex = (selectedIndex - 1 + items.length) % items.length;
                updateSearchSelection(items);
            } else if (e.key === "Enter") {
                e.preventDefault();
                if (selectedIndex >= 0 && selectedIndex < searchResults.length) {
                    const item = searchResults[selectedIndex];
                    selectHistory(item.market === 'futures' ? item.code : item.market + item.code);
                } else {
                    loadStock();
                }
            } else if (e.key === "Escape") {
                el.classList.remove("show");
            }
        };
        window.updateSearchSelection = function(items) {
            items.forEach((item, i) => {
                item.style.background = i === selectedIndex ? "#0f3460" : "";
                item.style.color = i === selectedIndex ? "#e0e0e0" : "";
            });
        };
        window.doSearch = function(keyword) {
            fetch("/api/search?q=" + encodeURIComponent(keyword))
                .then(r => r.json())
                .then(data => {
                    const el = document.getElementById("stock-history");
                    searchResults = data.results || [];
                    selectedIndex = -1;
                    if (!searchResults.length) {
                        el.classList.remove("show");
                        return;
                    }
                    el.innerHTML = searchResults.map((item, idx) => {
                        const safeCode = item.code.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                        const safeMarket = item.market.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                        const fullCode = item.market === 'futures' ? safeCode : safeMarket + safeCode;
                        const displayCode = item.market === 'futures' ? item.code : item.market + item.code;
                        const typeMap = {"深A":"深A","沪A":"沪A","深B":"深B","沪B":"沪B","指数":"指数","基金":"基金","场外基金":"场外基金","港股":"港股"};
                        const typeLabel = typeMap[item.type] || item.type;
                        return `<div class="stock-history-item" data-idx="${idx}"><span onclick="selectHistory('${fullCode}')" style="flex:1;display:block">${displayCode} - ${item.name} (${item.pinyin}) <span style="color:#888;font-size:11px;margin-left:8px">${typeLabel}</span></span></div>`;
                    }).join("");
                    el.classList.add("show");
                    // 焦点自动移到第一个候选
                    selectedIndex = 0;
                    updateSearchSelection(el.querySelectorAll(".stock-history-item"));
                })
                .catch(() => {});
        };
        window.toggleInputClear = function() {
            const input = document.getElementById("stock-code-input");
            document.getElementById("input-clear").style.display = input.value ? "" : "none";
        };
        window.removeHistory = removeHistory;
        window.showHistory = function() {
            const list = getHistory();
            const el = document.getElementById("stock-history");
            if (!list.length) { el.classList.remove("show"); return; }
            el.innerHTML = list.map(c => {
                const safe = c.code.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                const label = c.name ? c.code + " - " + c.name : c.code;
                return `<div class="stock-history-item"><span onclick="selectHistory('${safe}')" style="flex:1;display:block">${label}</span><span class="stock-history-del" onclick="event.stopPropagation();removeHistory('${safe}')">&times;</span></div>`;
            }).join("");
            el.innerHTML += `<div class="stock-history-clear" onclick="event.stopPropagation();clearHistory()">清除全部</div>`;
            el.classList.add("show");
        };
        document.addEventListener("click", function(e) {
            if (!e.target.closest(".stock-input")) {
                document.getElementById("stock-history").classList.remove("show");
            }
        });

        window.loadStock = function() {
            const code = document.getElementById("stock-code-input").value.trim();
            if (!code) return;
            // 切换股票时取消区间选择
            if (_rangeSelect.mode === 'SELECTED_A') {
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
            }
            document.getElementById("stock-history").classList.remove("show");
            document.getElementById("loading").classList.remove("hidden");
            const isFuturesCode = code.includes('KQ.m@') || code.includes('KQ.i@') || /^[A-Z]+\.[A-Z]/.test(code);
            // 同类继承上一周期，异类使用默认周期
            const fetchFreq = isFuturesCode ? lastFuturesFreq : lastStockFreq;
            currentFreq = fetchFreq;
            if (isFuturesCode) {
                updateFreqButtonStates(true); // 期货：禁用 d/w，启用 1m/15s
                connectRealtimeInit(code, fetchFreq);
                return;
            }
            updateFreqButtonStates(false); // 股票：禁用 1m/15s，启用 d/w
            fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + fetchFreq)
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                    return resp.json();
                })
                .then(data => {
                    saveHistory(code, data.meta.name);
                    chartData = data;
                    updateRestartBtn();
                    updateDualBtn();
                    // 根据返回数据的周期同步按钮状态
                    let returnedFreq;
                    if (data.meta.freq === "5分钟") {
                        returnedFreq = "5m";
                    } else if (data.meta.freq === "30分钟") {
                        returnedFreq = "30m";
                    } else if (data.meta.freq === "周线") {
                        returnedFreq = "w";
                    } else {
                        returnedFreq = "d";
                    }
                    currentFreq = returnedFreq;
                    lastStockFreq = currentFreq; // 更新股票周期记忆
                    updateFreqButtonStates(false);
                    viewCount = 377;
                    adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    // 保持当前开关状态，不重置（切换个股时继承）
                    document.getElementById("btn-fx").classList.toggle("active", showFx);
                    document.getElementById("btn-bi").classList.toggle("active", showBi);
                    document.getElementById("btn-zs").classList.toggle("active", showZs);
                    document.getElementById("btn-ma").classList.toggle("active", showMa);
                    document.getElementById("btn-seg").classList.toggle("active", showSeg);
                    document.getElementById("btn-bsp").classList.toggle("active", showBsp);
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    const lastDate = chartData.klines[chartData.klines.length - 1].date.slice(0, 10);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    resizeCanvas();
                    // 双窗口模式下同时加载下面窗口数据
                    if (isDualWindow) {
                        const bottomFreq = getDualBottomFreq(currentFreq);
                        if (bottomFreq) {
                            dualBottomFreq = bottomFreq;
                            return fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + bottomFreq)
                                .then(resp => resp.json())
                                .then(bottomData => {
                                    dualBottomData = bottomData;
                                    dualBottomViewCount = 377;
                                    dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                                    if (dualBottomData.klines.length < dualBottomViewCount) {
                                        dualBottomViewOffset = 0;
                                    }
                                    document.getElementById("loading").classList.add("hidden");
                                    render();
                                    generateStats();
                                    loadAnnotations();
                                });
                        }
                    }
                    document.getElementById("loading").classList.add("hidden");
                    render();
                    generateStats();
                    loadAnnotations();
                    // 期货/期指：切换到实时模式
                    startRealtimeIfFutures(data);
                })
                .catch(err => {
                    alert("查询失败: " + err.message);
                    document.getElementById("loading").classList.add("hidden");
                });
        };

        window.selectHistory = function(code) {
            document.getElementById("stock-code-input").value = code;
            document.getElementById("stock-history").classList.remove("show");
            window.loadStock();
        };

        // ========== 期货实时模式 ==========
        function startRealtimeIfFutures(data) {
            // 检查是否是期货/期指品种（股票路径中用于断开SSE，期货路径中用于连SSE）
            const isFutures = data.meta.market === 'futures';
            const badge = document.getElementById('realtime-badge');

            if (isFutures) {
                const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d','周线':'w'};
                if (data.meta.freq) {
                    currentFreq = freqMap[data.meta.freq] || currentFreq;
                }
                lastFuturesFreq = currentFreq; // 记录期货周期
                updateFreqButtonStates(true);
                connectRealtime(data.meta.symbol);
            } else {
                disconnectRealtime();
                updateFreqButtonStates(false);
            }
        }

        // ========== SSE 初始化连接（初始快照 + 增量合一） ==========
        function tryReconnect(callback, delayMs) {
            if (reconnectCount >= MAX_RECONNECT) {
                console.warn('重连已达上限(' + MAX_RECONNECT + '次)，放弃重连');
                realtimeStopped = true;
                isRealtimeMode = false;
                if (realtimeEventSource) {
                    realtimeEventSource.close();
                    realtimeEventSource = null;
                }
                const badge = document.getElementById('realtime-badge');
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
                return;
            }
            reconnectCount++;
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(() => {
                reconnectTimer = null;
                callback();
            }, delayMs);
        }

        function connectRealtimeInit(symbol, freq, startTime) {
            disconnectRealtime();
            realtimeStopped = false;  // 用户主动操作，允许重连
            reconnectCount = 0; // 用户主动操作，重置重连计数
            realtimeSymbol = symbol;
            realtimeFreq = freq;
            realtimeStartTime = startTime || null;
            isRealtimeMode = true;
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';
            // loading 由调用方（loadStock/switchFreq）已设置

            try {
                let sseUrl = '/api/futures_stream?symbol=' + encodeURIComponent(symbol) + '&freq=' + encodeURIComponent(freq || '15s');
                if (startTime) {
                    sseUrl += '&start_time=' + encodeURIComponent(startTime);
                }
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                // init 事件：初始全量快照
                realtimeEventSource.addEventListener('init', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.error) {
                            console.warn('引擎未就绪，2秒后重试:', data.error);
                            disconnectRealtime();
                            tryReconnect(() => {
                                if (realtimeSymbol === symbol) {
                                    connectRealtimeInit(symbol, freq, realtimeStartTime);
                                }
                            }, 2000);
                            return;
                        }
                        // 全量初始数据
                        chartData = data;
                        saveHistory(symbol, data.meta.name);
                        updateRestartBtn();
                        updateDualBtn();
                        // 同步周期
                        const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d','周线':'w'};
                        currentFreq = freqMap[data.meta.freq] || freq;
                        lastFuturesFreq = currentFreq; // 更新期货周期记忆
                        updateFreqButtonStates(true);
                        viewCount = 377;
                        adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                        viewOffset = Math.max(0, data.klines.length - viewCount);
                        if (data.klines.length < viewCount) viewOffset = 0;
                        document.getElementById("stock-name").textContent = data.meta.name;
                        document.getElementById("stock-code").textContent = data.meta.symbol;
                        document.title = "缠论分析 - " + data.meta.name;
                        if (data.klines.length > 0) {
                            const lastDate = data.klines[data.klines.length - 1].date.slice(0, 10);
                            document.getElementById("goto-date-input").value = lastDate;
                        }
                        updateWeekday();
                        document.getElementById("loading").classList.add("hidden");
                        resizeCanvas();
                        render();
                        generateStats();
                    } catch(e) {
                        console.error('初始数据解析失败:', e);
                        document.getElementById("loading").classList.add("hidden");
                    }
                });

                // update 事件：增量更新
                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeData(data);
                    } catch(e) {
                        console.error('实时数据解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    if (realtimeStopped) return; // 已放弃重连，忽略后续事件
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
                    tryReconnect(() => {
                        if (isRealtimeMode && realtimeSymbol) {
                            connectRealtime(realtimeSymbol, realtimeFreq, realtimeStartTime);
                        }
                    }, 5000);
                };

                realtimeEventSource.onopen = function() {
                    realtimeConnected = true;
                    badge.classList.remove('stopped');
                    badge.textContent = '● 实时';
                };
            } catch(e) {
                console.error('SSE连接失败:', e);
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
                document.getElementById("loading").classList.add("hidden");
                // 3秒后重试
                tryReconnect(() => {
                    if (realtimeSymbol === symbol) {
                        document.getElementById("loading").classList.remove("hidden");
                        connectRealtimeInit(symbol, freq, realtimeStartTime);
                    }
                }, 3000);
            }
        }

        function connectRealtime(symbol, freq, startTime) {
            freq = freq || currentFreq || '15s';
            // 断开旧连接
            disconnectRealtime();
            realtimeSymbol = symbol;
            realtimeFreq = freq;
            realtimeStartTime = startTime || null;
            isRealtimeMode = true;
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';

            try {
                let sseUrl = '/api/futures_stream?symbol=' + encodeURIComponent(symbol) + '&freq=' + encodeURIComponent(freq);
                if (startTime) {
                    sseUrl += '&start_time=' + encodeURIComponent(startTime);
                }
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                // 只监听 update 事件（重连不处理 init，避免覆盖已有数据）
                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeData(data);
                    } catch(e) {
                        console.error('实时数据解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    if (realtimeStopped) return; // 已放弃重连，忽略后续事件
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
                    // 5秒后尝试重连
                    tryReconnect(() => {
                        if (isRealtimeMode && realtimeSymbol) {
                            connectRealtime(realtimeSymbol, realtimeFreq, realtimeStartTime);
                        }
                    }, 5000);
                };

                realtimeEventSource.onopen = function() {
                    realtimeConnected = true;
                    badge.classList.remove('stopped');
                    badge.textContent = '● 实时';
                };
            } catch(e) {
                console.error('SSE连接失败:', e);
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
            }
        }

        function disconnectRealtime() {
            isRealtimeMode = false;
            realtimeSymbol = null;
            realtimeFreq = null;
            realtimeStartTime = null;
            if (realtimeEventSource) {
                realtimeEventSource.close();
                realtimeEventSource = null;
            }
            realtimeStopped = false; // 断开时重置标志
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
            realtimeConnected = false;
            const badge = document.getElementById('realtime-badge');
            badge.classList.remove('visible', 'stopped');
        }

        function handleRealtimeData(data) {
            if (!isRealtimeMode || !data || !data.klines) return;
            // 保存当前开关状态
            const savedShowBi = showBi, savedShowFx = showFx, savedShowMa = showMa;
            const savedShowZs = showZs, savedShowSeg = showSeg, savedShowBsp = showBsp;

            // 保存用户当前的缩放和位置
            const oldKlinesCount = chartData && chartData.klines ? chartData.klines.length : 0;
            const savedViewCount = viewCount;
            const savedViewOffset = viewOffset;
            const wasAtRightEdge = (savedViewOffset + savedViewCount >= oldKlinesCount);

            // 更新图表数据
            chartData = data;

            // 更新元信息
            document.getElementById('stock-name').textContent = data.meta.name;
            document.getElementById('stock-code').textContent = data.meta.symbol;
            document.title = "缠论分析 - " + data.meta.name;
            if (data.meta.freq) {
                const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d'};
                currentFreq = freqMap[data.meta.freq] || currentFreq;
            }

            // 同步 freq 按钮状态
            updateFreqButtonStates(true);

            // 保持用户缩放不变：如果在最右端，左减一右加一；否则原地不动
            const newKlinesCount = data.klines.length;
            const delta = newKlinesCount - oldKlinesCount;
            viewCount = savedViewCount;
            if (wasAtRightEdge && delta > 0) {
                viewOffset = Math.max(0, savedViewOffset + delta);
            } else {
                viewOffset = savedViewOffset;
            }

            // 重绘
            updateSlider();
            resizeCanvas();
            render();
            updateRestartBtn();
            updateDualBtn();
        }

        // 页面关闭时清理 SSE
        window.addEventListener('beforeunload', function() {
            disconnectRealtime();
        });

        function generateStats() {
            if (!chartData) return;
            const klines = getVisibleKlines();
            if (!klines.length) return;
            const startDate = klines[0].date, endDate = klines[klines.length - 1].date;
            const visBis = chartData.bis.filter(bi => bi.sdt >= startDate && bi.edt <= endDate);
            const visFxs = chartData.fxs.filter(fx => fx.date >= startDate && fx.date <= endDate);
            const allBis = chartData.bis, allFxs = chartData.fxs;
            let visUp = 0, visDown = 0, totalPower = 0, maxPower = 0;
            visBis.forEach(bi => { if (bi.direction === "up") visUp++; else visDown++; totalPower += bi.power; if (bi.power > maxPower) maxPower = bi.power; });
            const avgPower = visBis.length > 0 ? (totalPower / visBis.length).toFixed(2) : 0;
            let allUp = 0, allDown = 0;
            allBis.forEach(bi => { if (bi.direction === "up") allUp++; else allDown++; });
            document.getElementById("stats-content").innerHTML = `
                <div class="stats-row"><span class="stats-label">可见笔数</span><span class="stats-value">${visBis.length} / ${allBis.length}</span></div>
                <div class="stats-row"><span class="stats-label">向上笔</span><span class="stats-value" style="color:#FF4444">${visUp} / ${allUp}</span></div>
                <div class="stats-row"><span class="stats-label">向下笔</span><span class="stats-value" style="color:#00DD00">${visDown} / ${allDown}</span></div>
                <div class="stats-row"><span class="stats-label">平均力度</span><span class="stats-value">${avgPower}</span></div>
                <div class="stats-row"><span class="stats-label">最大力度</span><span class="stats-value" style="color:#FFD700">${maxPower.toFixed(2)}</span></div>
                <div class="stats-row"><span class="stats-label">顶分型</span><span class="stats-value" style="color:#FF4444">${visFxs.filter(f=>f.mark==="G").length} / ${allFxs.filter(f=>f.mark==="G").length}</span></div>
                <div class="stats-row"><span class="stats-label">底分型</span><span class="stats-value" style="color:#00DD00">${visFxs.filter(f=>f.mark==="D").length} / ${allFxs.filter(f=>f.mark==="D").length}</span></div>`;
        }

        function updateSlider() {
            // 双窗口模式下，使用激活窗口的数据
            const data = (isDualWindow && activeDualWindow === 'bottom' && dualBottomData) ? dualBottomData : chartData;
            const vo = (isDualWindow && activeDualWindow === 'bottom') ? dualBottomViewOffset : viewOffset;
            const vc = (isDualWindow && activeDualWindow === 'bottom') ? dualBottomViewCount : viewCount;
            if (!data || !data.klines.length) return;
            const track = document.getElementById("slider-track");
            const win = document.getElementById("slider-window");
            const label = document.getElementById("slider-label");
            const totalKlines = data.klines.length;
            const trackWidth = track.clientWidth;
            if (trackWidth <= 0) return;

            const windowWidth = Math.max(10, (vc / totalKlines) * trackWidth);
            const maxOffset = Math.max(0, totalKlines - vc);
            const windowLeft = (vo / totalKlines) * trackWidth;

            win.style.width = windowWidth + "px";
            win.style.left = Math.max(0, Math.min(windowLeft, trackWidth - windowWidth)) + "px";

            const displayCount = Math.round(vc);
            const displayOffset = Math.round(vo);
            const startIdx = Math.max(0, displayOffset);
            const endIdx = Math.min(totalKlines - 1, startIdx + displayCount - 1);
            const startDate = data.klines[startIdx].date.slice(0, 10);
            const endDate = data.klines[endIdx].date.slice(0, 10);
            const globalStart = Math.max(0, Math.floor(vo));
            const globalEnd = Math.min(totalKlines, globalStart + vc);
            const visBis = data.bis.filter(bi => {
                const si = data.klines.findIndex(k => k.date === bi.sdt);
                return si >= globalStart && si < globalEnd;
            });
            const visFxs = data.fxs.filter(fx => {
                const fi = data.klines.findIndex(k => k.date === fx.date);
                return fi >= globalStart && fi < globalEnd;
            });
            const visZs = data.zs.filter(zs => {
                const si = data.klines.findIndex(k => k.date === zs.sdt);
                return si >= globalStart && si < globalEnd;
            });
            const winLabel = isDualWindow ? (activeDualWindow === 'bottom' ? '[下窗] ' : '[上窗] ') : '';
            label.textContent = winLabel + startDate + " - " + endDate + "   [K线]: " + displayCount + "/" + totalKlines + "   [分型]: " + visFxs.length + "/" + data.fxs.length + "   [笔]: " + visBis.length + "/" + data.bis.length + "   [中枢]: " + visZs.length + "/" + data.zs.length;
        }

        (function() {
            const slider = document.getElementById("range-slider");
            const track = document.getElementById("slider-track");
            const win = document.getElementById("slider-window");
            const handleLeft = document.getElementById("slider-handle-left");
            const handleRight = document.getElementById("slider-handle-right");
            let sliderDragging = false;
            let dragType = null;
            let dragStartX = 0, dragStartOffset = 0, dragStartCount = 0, dragStartRightEdge = 0;

            // 获取当前激活窗口的 data
            function getActiveData() {
                if (isDualWindow && activeDualWindow === 'bottom' && dualBottomData) {
                    return dualBottomData;
                }
                return chartData;
            }
            // 获取当前激活窗口的 viewOffset
            function getActiveViewOffset() {
                if (isDualWindow && activeDualWindow === 'bottom') {
                    return dualBottomViewOffset;
                }
                return viewOffset;
            }
            // 设置当前激活窗口的 viewOffset
            function setActiveViewOffset(v) {
                if (isDualWindow && activeDualWindow === 'bottom') {
                    dualBottomViewOffset = v;
                } else {
                    viewOffset = v;
                }
            }
            // 获取当前激活窗口的 viewCount
            function getActiveViewCount() {
                if (isDualWindow && activeDualWindow === 'bottom') {
                    return dualBottomViewCount;
                }
                return viewCount;
            }
            // 设置当前激活窗口的 viewCount
            function setActiveViewCount(v) {
                if (isDualWindow && activeDualWindow === 'bottom') {
                    dualBottomViewCount = v;
                } else {
                    viewCount = v;
                }
            }
            // 渲染当前激活窗口
            function renderActive() {
                updateActiveWindowClass();
                if (isDualWindow && activeDualWindow === 'bottom') {
                    // 直接渲染下面窗口，跳过 updateDualNewZs() 避免滑块操作时误清除红框新中枢
                    if (!dualBottomData || !bottomCtx) return;
                    const _savedCanvas = canvas, _savedCtx = ctx;
                    canvas = bottomCanvas; ctx = bottomCtx;
                    window._isRenderingBottom = true;
                    _renderChart(dualBottomData, dualBottomFreq, dualBottomViewOffset, dualBottomViewCount,
                        dualBottomMouseX, dualBottomMouseY, dualHighlightRange, dualRedRange);
                    window._isRenderingBottom = false;
                    canvas = _savedCanvas; ctx = _savedCtx;
                } else if (isDualWindow) {
                    renderTop();
                } else {
                    render();
                }
            }

            function getSliderInfo() {
                const data = getActiveData();
                const totalKlines = data ? data.klines.length : 1;
                const trackWidth = track.clientWidth;
                return { totalKlines, trackWidth };
            }

            handleLeft.addEventListener("mousedown", function(e) {
                e.preventDefault(); e.stopPropagation();
                sliderDragging = true; dragType = "left";
                dragStartX = e.clientX; dragStartCount = getActiveViewCount();
                dragStartRightEdge = getActiveViewOffset() + getActiveViewCount();
            });
            handleRight.addEventListener("mousedown", function(e) {
                e.preventDefault(); e.stopPropagation();
                sliderDragging = true; dragType = "right";
                dragStartX = e.clientX; dragStartCount = getActiveViewCount();
                dragStartOffset = getActiveViewOffset();
            });
            win.addEventListener("mousedown", function(e) {
                e.preventDefault(); e.stopPropagation();
                sliderDragging = true; dragType = "window";
                dragStartX = e.clientX; dragStartOffset = getActiveViewOffset();
            });
            track.addEventListener("mousedown", function(e) {
                const data = getActiveData();
                if (!data) return;
                e.preventDefault();
                const rect = track.getBoundingClientRect();
                const ratio = (e.clientX - rect.left) / rect.width;
                const totalKlines = data.klines.length;
                const vc = getActiveViewCount();
                const newOffset = ratio * totalKlines - vc / 2;
                setActiveViewOffset(Math.max(0, Math.min(totalKlines - vc, newOffset)));
                renderActive();
            });

            document.addEventListener("mousemove", function(e) {
                if (!sliderDragging || !getActiveData()) return;
                const { totalKlines, trackWidth } = getSliderInfo();
                if (trackWidth <= 0) return;
                const dx = e.clientX - dragStartX;
                const dk = (dx / trackWidth) * totalKlines;

                let vc = getActiveViewCount();
                let vo = getActiveViewOffset();
                if (dragType === "left") {
                    const newCount = Math.round(Math.max(3, Math.min(totalKlines, dragStartCount - dk)));
                    vc = newCount;
                    vo = Math.max(0, Math.round(dragStartRightEdge - vc));
                } else if (dragType === "right") {
                    const newCount = Math.round(Math.max(3, Math.min(totalKlines, dragStartCount + dk)));
                    const maxOffset = totalKlines - newCount;
                    vc = newCount;
                    vo = Math.min(vo, Math.max(0, maxOffset));
                } else if (dragType === "window") {
                    const newOffset = dragStartOffset + dk;
                    const maxOffset = totalKlines - vc;
                    vo = Math.max(0, Math.min(newOffset, maxOffset));
                }
                vc = Math.round(vc);
                vo = Math.round(vo);
                setActiveViewCount(vc);
                setActiveViewOffset(vo);
                renderActive();
            });

            document.addEventListener("mouseup", function() {
                sliderDragging = false; dragType = null;
            });
        })();

        // ============================================================
        // 文字标注功能
        // ============================================================

        // 加载标注数据
        function loadAnnotations() {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/annotations?code=" + encodeURIComponent(code) + "&freq=" + freq)
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    annotations = data.annotations || [];
                    render();
                })
                .catch(function() { annotations = []; });
        }

        // 保存标注到后端
        function saveAnnotationToServer(dateStr, text, yOffset) {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/annotations", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action: "add",
                    code: code,
                    freq: freq,
                    date: dateStr,
                    text: text,
                    y_offset: yOffset || 0
                })
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.ok) {
                    // 添加到本地缓存
                    annotations.push({ date: dateStr, text: text, y_offset: yOffset || 0 });
                    render();
                }
            })
            .catch(function(err) { console.error("保存标注失败:", err); });
        }

        // 删除标注
        function deleteAnnotationFromServer(dateStr, text) {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/annotations", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action: "delete",
                    code: code,
                    freq: freq,
                    date: dateStr,
                    text: text
                })
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.ok) {
                    annotations = annotations.filter(function(a) {
                        return !(a.date === dateStr && a.text === text);
                    });
                    render();
                }
            })
            .catch(function(err) { console.error("删除标注失败:", err); });
        }

        // 右键菜单处理
        function onContextMenu(e) {
            e.preventDefault();
            if (!chartData) return;

            // 双窗口模式下，只在上面窗口支持标注
            if (isDualWindow && window._isRenderingBottom) return;

            const rect = canvas.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const clickY = e.clientY - rect.top;

            // 确定点击的K线
            const area = getChartArea();
            const klines = getVisibleKlines();
            if (!klines.length) return;
            const barStep = area.w / (klines.length < viewCount ? klines.length : viewCount);
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            const idx = Math.floor((clickX - area.x + subPixelOffset) / barStep);
            if (idx < 0 || idx >= klines.length) return;
            const k = klines[idx];
            if (!k) return;
            // 检查是否在K线主图区域内
            if (clickY < area.y || clickY > area.y + area.h) return;

            _annotationTargetDate = k.date;
            _annotationTargetX = e.clientX;
            _annotationTargetY = clickY;
            _annotationClickTarget = null;

            // 检测点击是否在某个标注的方框区域内
            const priceRange = getPriceRange(klines);
            const dateToIdx = {};
            for (let i = 0; i < klines.length; i++) { dateToIdx[klines[i].date] = i; }
            for (let i = 0; i < annotations.length; i++) {
                const ann = annotations[i];
                const annIdx = dateToIdx[ann.date];
                if (annIdx === undefined) continue;
                const annK = klines[annIdx];
                const annX = area.x + barStep * annIdx + barStep / 2 - subPixelOffset;
                const annY = ann.y_offset || (priceToY(annK.high, area, priceRange) - 8);
                const layout = getAnnotationLayout(ann, annX, annY, area);
                if (clickX >= layout.boxX && clickX <= layout.boxX + layout.boxW &&
                    clickY >= layout.boxY && clickY <= layout.boxY + layout.boxH) {
                    _annotationClickTarget = ann;
                    break;
                }
            }

            // 显示菜单
            const menu = document.getElementById("annotation-menu");
            const menuDeleteOne = document.getElementById("annotation-menu-delete-one");
            const menuEditOne = document.getElementById("annotation-menu-edit-one");
            const menuAdd = document.getElementById("annotation-menu-add");
            const menuDivider = document.getElementById("annotation-menu-divider");
            const menuDelAll = document.getElementById("annotation-menu-del-all");
            if (_annotationClickTarget) {
                menuDeleteOne.style.display = "block";
                menuEditOne.style.display = "block";
                menuAdd.style.display = "none";
                menuDivider.style.display = "none";
                menuDelAll.style.display = "none";
            } else {
                menuDeleteOne.style.display = "none";
                menuEditOne.style.display = "none";
                menuAdd.style.display = "block";
                menuDivider.style.display = "block";
                menuDelAll.style.display = "block";
            }

            menu.style.left = e.clientX + "px";
            menu.style.top = e.clientY + "px";
            menu.classList.add("show");
        }

        // 关闭右键菜单（点击其他地方）
        document.addEventListener("click", function(e) {
            const menu = document.getElementById("annotation-menu");
            if (!menu.contains(e.target)) {
                menu.classList.remove("show");
            }
        });

        // 添加标注
        window.annotationAdd = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            _annotationDialogMode = "add";
            document.getElementById("annotation-dialog-title").textContent = "添加文字标注";
            document.getElementById("annotation-dialog-date").textContent = "K线日期: " + _annotationTargetDate;
            document.getElementById("annotation-dialog-input").value = "";
            document.getElementById("annotation-dialog").classList.add("show");
            setTimeout(function() {
                document.getElementById("annotation-dialog-input").focus();
            }, 100);
        };

        // 删除右键点击命中的标注
        window.annotationDeleteAnnotation = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!_annotationClickTarget) return;
            deleteAnnotationFromServer(_annotationClickTarget.date, _annotationClickTarget.text);
        };

        // 修改右键点击命中的标注
        window.annotationEditAnnotation = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!_annotationClickTarget) return;
            _annotationDialogMode = "edit";
            _annotationEditOldText = _annotationClickTarget.text;
            _annotationTargetDate = _annotationClickTarget.date;
            _annotationTargetY = _annotationClickTarget.y_offset || 0;
            document.getElementById("annotation-dialog-title").textContent = "修改文字标注";
            document.getElementById("annotation-dialog-date").textContent = "K线日期: " + _annotationClickTarget.date;
            document.getElementById("annotation-dialog-input").value = _annotationClickTarget.text;
            document.getElementById("annotation-dialog").classList.add("show");
            setTimeout(function() {
                var inp = document.getElementById("annotation-dialog-input");
                inp.focus();
                inp.setSelectionRange(inp.value.length, inp.value.length);
            }, 100);
        };

        // 删除当前股票/周期全部标注
        window.annotationDeleteAllGlobal = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            if (confirm("确定删除当前股票 (" + code + ") " + freq + " 周期下的全部标注吗？")) {
                fetch("/api/annotations", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        action: "delete_all",
                        code: code,
                        freq: freq
                    })
                })
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.ok) {
                        annotations = [];
                        render();
                    }
                })
                .catch(function(err) { console.error("删除全部标注失败:", err); });
            }
        };

        // 标注对话框键盘事件
        window.annotationDialogKeydown = function(e) {
            if (e.key === "Enter") {
                annotationDialogConfirm();
            } else if (e.key === "Escape") {
                annotationDialogCancel();
            }
        };

        // 标注对话框确认
        window.annotationDialogConfirm = function() {
            const text = document.getElementById("annotation-dialog-input").value.trim();
            if (!text) {
                alert("请输入标注文字");
                return;
            }
            document.getElementById("annotation-dialog").classList.remove("show");
            if (_annotationDialogMode === "edit" && _annotationEditOldText) {
                updateAnnotationOnServer(_annotationTargetDate, _annotationEditOldText, text, _annotationTargetY);
            } else {
                saveAnnotationToServer(_annotationTargetDate, text, _annotationTargetY);
            }
        };

        // 更新标注（修改模式：删除旧标注+添加新标注）
        function updateAnnotationOnServer(dateStr, oldText, newText, yOffset) {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/annotations", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action: "update",
                    code: code,
                    freq: freq,
                    date: dateStr,
                    old_text: oldText,
                    text: newText,
                    y_offset: yOffset || 0
                })
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.ok) {
                    annotations = annotations.filter(function(a) {
                        return !(a.date === dateStr && a.text === oldText);
                    });
                    annotations.push({ date: dateStr, text: newText, y_offset: yOffset || 0 });
                    render();
                }
            })
            .catch(function(err) { console.error("更新标注失败:", err); });
        }

        // 标注对话框取消
        window.annotationDialogCancel = function() {
            document.getElementById("annotation-dialog").classList.remove("show");
        };

        // 计算标注框的布局信息（供绘制和命中检测共用）
        function getAnnotationLayout(ann, klineX, klineY, area) {
            const font = "bold 11px 'PingFang SC', 'Microsoft YaHei', sans-serif";
            ctx.font = font;
            const lineHeight = 16;
            const padX = 6, padY = 3;
            const maxCharsPerLine = 11;

            // 按每行最多11个字折行
            const lines = [];
            let remaining = ann.text;
            while (remaining.length > 0) {
                lines.push(remaining.substring(0, maxCharsPerLine));
                remaining = remaining.substring(maxCharsPerLine);
            }

            // 计算每行宽度，取最大
            let maxTextW = 0;
            const lineWidths = lines.map(function(line) {
                const w = ctx.measureText(line).width;
                if (w > maxTextW) maxTextW = w;
                return w;
            });

            const boxW = maxTextW + padX * 2;
            const boxH = lines.length * lineHeight + padY * 2;
            const boxY = klineY - boxH; // 框底对齐klineY

            // 居中：以K线X为中心
            let boxX = klineX - boxW / 2;

            // 边界修正：不超出视口
            if (boxX < area.x) {
                boxX = area.x;
            }
            if (boxX + boxW > area.x + area.w) {
                boxX = area.x + area.w - boxW;
            }

            return { lines: lines, lineWidths: lineWidths, maxTextW: maxTextW,
                     boxW: boxW, boxH: boxH, boxX: boxX, boxY: boxY,
                     lineHeight: lineHeight, padX: padX, padY: padY };
        }

        // 绘制标注文字
        function drawAnnotations(klines, area, priceRange, barStep, subPixelOffset) {
            if (!annotations || !annotations.length) return;
            const dateToIdx = {};
            for (let i = 0; i < klines.length; i++) {
                dateToIdx[klines[i].date] = i;
            }

            annotations.forEach(function(ann) {
                const idx = dateToIdx[ann.date];
                if (idx === undefined) return;
                const k = klines[idx];
                const kx = area.x + barStep * idx + barStep / 2 - subPixelOffset;
                const ky = ann.y_offset || (priceToY(k.high, area, priceRange) - 8);

                const layout = getAnnotationLayout(ann, kx, ky, area);

                // 绘制每行文字（无背景框，文字左对齐，白色加阴影）
                ctx.fillStyle = "#ffffff";
                ctx.textAlign = "left";
                ctx.textBaseline = "middle";
                ctx.font = "bold 11px 'PingFang SC', 'Microsoft YaHei', sans-serif";
                ctx.shadowColor = "rgba(0, 0, 0, 0.85)";
                ctx.shadowBlur = 3;
                for (let li = 0; li < layout.lines.length; li++) {
                    const lineX = layout.boxX + layout.padX;
                    const lineY = layout.boxY + layout.padY + layout.lineHeight * li + layout.lineHeight / 2;
                    ctx.fillText(layout.lines[li], lineX, lineY);
                }
                ctx.shadowColor = "transparent";
                ctx.shadowBlur = 0;
            });
        }

        init();
    })();
    </script>
</body>
</html>"""


# ============================================================
# 主函数
# ============================================================
def main():
    print("=" * 60)
    print("  缠论分析 - chan.py 版本")
    print("=" * 60)

    # 1. 默认加载上证指数
    result = analyze_stock(SYMBOL_CODE, freq="d")
    if "error" in result:
        print(f"[错误] {result['error']}")
        return

    # 2. 生成 HTML
    html_path = os.path.join(OUTPUT_DIR, "chan_chart.html")
    chart_data_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
    chart_data_json = chart_data_json.replace("</script>", "<\\/script>")
    html_content = HTML_TEMPLATE.replace("%%CHART_DATA%%", chart_data_json)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    html_size = os.path.getsize(html_path)
    print(f"[stock][信息] HTML页面已生成: {html_path} ({html_size/1024/1024:.1f}MB)")

    # 3. 启动HTTP服务器（流通股本在扫描时按需加载，只加载自选股列表中的股票）
    port = 18081  # 使用18081，避免与czsc版本的18080冲突
    server = ThreadingHTTPServer(("127.0.0.1", port), ChartHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=False)
    server_thread.start()
    url = f"http://127.0.0.1:{port}/chan_chart.html"
    print(f"[stock][信息] HTTP服务器已启动: {url}\n")
    print_memory("程序启动后")

    # 4. 期货实时引擎改为按需启动：用户输入期货代码后通过 SSE 连接自动触发
    print("[提示] 期货实时引擎已设为按需启动（输入期货代码后自动触发）")

    try:
        server_thread.join()
    except KeyboardInterrupt:
        server.shutdown()
        print("\n[stock][信息] 服务器已停止")

    print("=" * 60)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

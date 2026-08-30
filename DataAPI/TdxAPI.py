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
import struct
import pandas as pd
from datetime import datetime, timedelta
import threading as _threading
import logging
log = logging.getLogger(__name__)
from contextlib import contextmanager
from collections import OrderedDict
import numpy as np
from chinese_calendar import is_holiday

from Common.CEnum import AUTYPE, KL_TYPE, DATA_FIELD
from DataAPI.CommonStockAPI import CCommonStockApi
from KLine.KLine_Unit import CKLine_Unit

# 通达信研究行业 X代码↔881代码映射表（从官方PDF 3.6节提取）
# 映射表硬编码内嵌于 App/AppData.py（原独立数据文件 tdxhy_mapping_data.py
# 已合并入 AppData.py），由 App 层单一加载函数 AppData.load_tdxhy_mapping()
# 加载后，经 set_tdx_hy_mapping 注入（与 set_tdx_config 同一注入模式，
# DataAPI 与 App 互不依赖）。
_TDXHY_X_TO_881 = {}
_TDXHY_881_TO_X = {}


def collect_codes_from_vipdoc(vipdoc_dir):
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
                return True
            # 深圳排除：债券、基金、通达信内部板块等
            if code.startswith(sz_exclude_prefixes):
                return False
            # 其他深圳代码默认排除
            return False

        return False

    # === 收集A股代码 ===
    sh_count = 0
    sz_count = 0
    for mkt_dir, prefix in [("sh", "sh"), ("sz", "sz")]:
        lday_dir = os.path.join(vipdoc_dir, mkt_dir, "lday")
        if not os.path.isdir(lday_dir):
            continue
        for fname in os.listdir(lday_dir):
            if fname.startswith(prefix) and fname.endswith(".day"):
                code = fname[len(prefix):-4]
                if _is_a_stock_code(code, mkt_dir):
                    compound_key = mkt_dir + code
                    if compound_key not in result:
                        result[compound_key] = {"name": "", "pinyin": "", "market": mkt_dir}
                        if mkt_dir == "sh":
                            sh_count += 1
                        else:
                            sz_count += 1

    # === 收集港股代码（ds目录）===
    hk_count = 0
    ds_lday_dir = os.path.join(vipdoc_dir, "ds", "lday")
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
                        hk_count += 1

    return result


def set_tdx_hy_mapping(x_to_881=None, to_x=None):
    """由 App 引擎层启动时调用，注入通达信研究行业映射表

    数据文件 App/AppData.py 内嵌映射表由 AppData.load_tdxhy_mapping() 单一
    加载，本函数只收值不寻址；注入对象直存（同一 dict 身份），
    注入前调用方为空表（跨层依赖方向保持 DataAPI → 不 import App）。

    fail-fast 注入模式：任一侧注入后若双表仍存在空表，直接抛 ValueError，
    不以空表继续运行。
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
# 模块级配置（由 App 引擎层调用 set_tdx_config() 设置）
# ============================================================
_tdx_config = {
    "vipdoc_dir": None,
    "forward_adjust_enabled": False,
}

# 港股指数：应用层字母代码（HSTECH/HSIDI 等）→ 通达信 HZ 文件代码。
# 通达信港股指数日线文件为 `27#HZ{代码}.day`（如 27#HZ5017.day），
# 而非港股个股的 31# 前缀。读文件前须用此表把字母代码换算为 HZ 代码。
# （注意：与 App/AppData.py 中仅供「同花顺→通达信自选股同步」脚本使用的
#   ZXG_HK_INDEX_MAP 无关；此处是行情读取路径的本地映射。）
_HK_INDEX_HZ_MAP = {
    "HSTECH": "HZ5017",  # 恒生科技指数（Hang Seng TECH）
    "HSIDI": "HZ5489",   # 恒生创新药指数（Hang Seng Innovative Drug）
}

# 港股指数：应用层字母代码 → 恒生指数公司官方 Factsheet PDF 关键字。
# 这类港股指数由恒生指数公司发布，中证指数(000xxx)/深交所(399xxx)等中港A股
# 权威渠道均不覆盖，故「成分股」扫描来源须走官方 Factsheet PDF（结构化表格，
# 依 ISIN 列判别成分行）解析。
_HK_INDEX_FACT_SHEET = {
    "HSTECH": "hsteche",   # 恒生科技指数（30 只）
    "HSIDI": "hsidie",     # 恒生创新药指数（40 只）
}
# 当前支持成分检索的港股指数集合（大小写不敏感）
_HK_INDEX_CODES = set(_HK_INDEX_FACT_SHEET)

# 成分抓取用 UA（经实际验证，中港数据站点对 UA/Referer 有反爬过滤）
_HK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _hk_index_hz_code(code):
    """港股指数字母代码 → 通达信 HZ 文件代码；非指数原样返回。"""
    return _HK_INDEX_HZ_MAP.get((code or "").upper(), code)


def _hk_day_file(code):
    """港股日线文件路径：指数走 27#HZ{...}.day，个股走 31#{code}.day。"""
    hz = _hk_index_hz_code(code)
    if hz != code:
        return os.path.join(_tdx_config["vipdoc_dir"], "ds", "lday", f"27#{hz}.day")
    return os.path.join(_tdx_config["vipdoc_dir"], "ds", "lday", f"31#{code}.day")


def set_tdx_config(vipdoc_dir=None, forward_adjust_enabled=None):
    """设置通达信数据源所需的全局配置（由 App 引擎层启动时调用）"""
    if vipdoc_dir is not None:
        _tdx_config["vipdoc_dir"] = vipdoc_dir
    if forward_adjust_enabled is not None:
        _tdx_config["forward_adjust_enabled"] = forward_adjust_enabled


# 板块文件（block_*.dat）网络下载/刷新用的通达信行情服务器列表。
# 走 pytdx 的 TdxHq_API 连接拉取板块文件；与除权除息无关（除权除息现走 eltdx）。
TDX_BLOCK_SERVERS = [
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
        log.warning(f"[警告] 检测到 {len(gap_indices)} 处数据缺口（请补全数据）:")
        for idx, (gi, gap_td) in enumerate(gap_indices):
            prev_dt = records[gi-1]["dt"].strftime("%Y-%m-%d")
            curr_dt = records[gi]["dt"].strftime("%Y-%m-%d")
            log.warning(f"[警告]   缺口{idx+1}: {prev_dt} -> {curr_dt} (间隔{gap_td}个交易日)")
    else:
        old_start = records[0]["dt"].strftime("%Y-%m-%d")
        old_end = records[-1]["dt"].strftime("%Y-%m-%d")
        log.info("[信息] 检测到 0 处数据缺口")
        log.info(f"[信息]   数据范围: {old_start} ~ {old_end} ({len(records)}条)")


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
        log.info("[信息] 检测到指数文件，成交量字段不可靠（通达信确认指数分钟线仅有成交额，无成交量），将设为0")

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

    return result


def _resample_5m_to_30m(records, market="sh"):
    """
    将5分钟K线合成为30分钟K线（从 read_tdx_min_file 中提取的独立函数）
    供外部在5分钟前复权后调用，避免对30分钟K线做二次复权
    """
    if not records:
        return []

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
# XDXR 数据获取：由 DataAPI/ElTdxAPI.py 提供（当前仅 eltdx 数据源，
#   mootdx/pytdx 回退已注释保留于 ElTdxAPI，取消注释即可恢复三级回退）。
# ============================================================

from DataAPI.ElTdxAPI import get_xdxr_data
from DataAPI.AkshareAPI import fetch_index_cons


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

    return records, True


def find_day_file(market, code):
    """查找日线数据文件路径"""
    if market == 'hk':
        return _hk_day_file(code)
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
            hz = _hk_index_hz_code(code)
            prefix = f"27#{hz}" if hz != code else f"31#{code}"
            data_file = os.path.join(_tdx_config["vipdoc_dir"], "ds", "fzline", f"{prefix}.lc5")
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
            hz = _hk_index_hz_code(code)
            prefix = f"27#{hz}" if hz != code else f"31#{code}"
            min_file = os.path.join(_tdx_config["vipdoc_dir"], "ds", "fzline", f"{prefix}.lc5")
        elif market == 'ds':
            min_file = os.path.join(_tdx_config["vipdoc_dir"], "ds", "fzline", f"62#{code}.lc5")
        else:
            min_file = os.path.join(_tdx_config["vipdoc_dir"], market, "fzline", f"{market}{code}.lc5")
        if not os.path.exists(min_file):
            log.warning(f"[警告] 子级别数据文件不存在: {min_file}")
            return None
        sub_records = read_tdx_min_file(min_file, market=market, aggregate_30m=False)
        if _tdx_config["forward_adjust_enabled"]:
            sub_records, _ = _forward_adjust(sub_records, market=market, code=code, end_date=end_date)
        if sub_freq == '30m':
            sub_records = _resample_5m_to_30m(sub_records, market=market)
    elif sub_freq == 'd':
        day_file = find_day_file(market, code)
        if not os.path.exists(day_file):
            log.warning(f"[警告] 子级别数据文件不存在: {day_file}")
            return None
        sub_records = read_tdx_day_file(day_file, market=market)
        if _tdx_config["forward_adjust_enabled"]:
            sub_records, _ = _forward_adjust(sub_records, market=market, code=code, end_date=end_date)
    else:
        return None

    if len(sub_records) < 5:
        log.warning(f"[警告] 子级别数据不足({len(sub_records)}条)")
        return None

    # 过滤到与主级别相同的时间范围（略大一点，确保边界包含）
    if records:
        main_start = records[0]["dt"]
        main_end = records[-1]["dt"]
        sub_records = [r for r in sub_records if main_start - timedelta(days=1) <= r["dt"] <= main_end + timedelta(days=1)]

    log.info(f"[信息] 子级别({sub_freq})数据加载: {len(sub_records)}条")
    return sub_records


# ============================================================
# 每请求数据注入（线程局部）
# ============================================================
# 背景：原 CTdxAPI._tdx_data 是**类变量**，set_data() 作为 classmethod 写
# 进程级全局，CChan 内部实例化的 CTdxAPI 再从类上读。数据流经一个进程级
# 共享可变对象，并发请求必然互相覆盖 —— 这正是 AppEngine._stock_analysis_lock
# 存在的唯一根因。
#
# 改为每请求线程局部注入后，数据全程是调用方的局部变量，CChan 构建天然
# 线程安全，该锁随之删除（与期货/SSE 路径的 session_context 同一机制）。
_CURRENT_TDX_DATA = _threading.local()


@contextmanager
def tdx_data_context(data):
    """线程局部注入本请求的 K 线数据，供随后创建的 CTdxAPI 实例读取。

    CChan 经 data_src="custom:TdxAPI.CTdxAPI" 自行实例化数据源（构造参数
    只有 code/k_type，无法携带数据），故用线程局部传递本请求数据。
    必须与 CChan 的 step_load 消费放在同一个 with 内 —— 数据源实例是在
    step_load 内部惰性创建的（见 Chan.py get_load_stock_iter）。

    data 形态（与历史类变量一致）：
      - list[dict]            单级别模式，所有级别共用同一份数据
      - dict{KL_TYPE: [...]}  多级别模式，按 k_type 取

    离开 with 自动还原上一層值：线程池线程复用不会把数据串到别的请求。
    """
    prev = getattr(_CURRENT_TDX_DATA, "data", None)
    _CURRENT_TDX_DATA.data = data
    try:
        yield
    finally:
        _CURRENT_TDX_DATA.data = prev


# ============================================================
# 适配器类
# ============================================================
class CTdxAPI(CCommonStockApi):
    """通达信本地文件数据源适配器

    K 线数据经 tdx_data_context() 每请求线程局部注入，实例只在 __init__
    时绑定一次快照引用（数据本身仍是调用方的局部对象，不跨请求共享）。
    脱离上下文直接实例化（如 DataAPI.get_stock_api 工厂）时数据为空，
    _get_records() 返回 [] —— 显式空结果优于静默读到别人的数据。
    """

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        super().__init__(code, k_type, begin_date, end_date, autype)
        self.is_stock = True
        self._tdx_data = getattr(_CURRENT_TDX_DATA, "data", None)

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass

    # ── 股票数据获取（统一经数据源适配器单轨）────────────────────
    # 引擎层不直连模块级 read_main_level_records / read_sub_level_records，
    # 统一经本适配器（CCommonStockApi 实现）读取；模块级函数为内部实现细节。
    @classmethod
    def fetch_main_level(cls, market, code, freq, return_raw=False, end_date=None):
        """读取主级别K线（委托模块级 read_main_level_records）

        return_raw=True 时返回 (main_records, sub_records, forward_adjust_done)
        三元组（双窗口共用读取优化路径）；否则返回 (records, forward_adjust_done)。
        """
        return read_main_level_records(market, code, freq,
                                       return_raw=return_raw, end_date=end_date)

    @classmethod
    def fetch_sub_level(cls, market, code, freq, sub_freq, records, end_date=None):
        """读取子级别K线（委托模块级 read_sub_level_records）"""
        return read_sub_level_records(market, code, freq, sub_freq, records,
                                      end_date=end_date)

    def SetBasciInfo(self):
        self.is_stock = True

    def _get_records(self):
        """根据 self.k_type 从本请求注入的 _tdx_data 中取出对应级别的 records"""
        if not self._tdx_data:
            return []
        if isinstance(self._tdx_data, dict):
            # 多级别模式：按 k_type 取对应级别的数据
            return self._tdx_data.get(self.k_type, [])
        else:
            # 单级别模式：直接使用
            return self._tdx_data

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

    说明：App/AppData.py 另持一份同名解析（服务其自选股读取），两者
    互不依赖、各自自含为既定职责边界（不引入顶层中立模块强并）。
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
        log.error(f"[错误] 读取板块文件失败 {blk_path}: {e}")
    return stocks


# ============================================================
# 自选股读写（read_zxg_stocks / save_to_zxg_blk / _ZXG_HK_INDEX_MAP /
# _ZXG_US_INDEX_MAP）属业务数据层职责，实现位于 App/AppData.py
# （含路径推导与 blk 解析：AppData.zxg_blk_path / _read_zxg_blk_file，
# 与本模块互不依赖）；本模块的 get_blk_path / read_blk_file 保留为
# 通用 .blk 解析工具（Test/test_blk_parsing.py 守护双解析器一致性）。
# 调用方式：from App.AppData import app_data
#           app_data.read_zxg_stocks() / app_data.save_to_zxg_blk(codes)
# ============================================================


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
            log.warning(f"[板块刷新] {file_name} 服务端 size 无效，保留旧文件")
            return False

        raw = _download_block_file(api, host, port, file_name)
        if not raw or len(raw) != total_size:
            log.warning(f"[板块刷新] {file_name} 下载不完整，保留旧文件: {len(raw) if raw else 0}/{total_size}")
            return False

        if not _validate_downloaded_block_file(file_name, raw):
            log.warning(f"[板块刷新] {file_name} 格式校验失败，保留旧文件")
            return False

        local_path = os.path.join(block_cache_dir, file_name)
        _safe_replace_file(local_path, raw)
        log.info(f"[板块刷新] ✅ {file_name} 刷新成功: {total_size} 字节")
        return True
    except Exception as e:
        log.warning(f"[板块刷新] {file_name} 刷新失败，保留旧文件: {e}")
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
                    log.info(f"[板块成分股] ✅ 从本地缓存读取 {bf}: {len(parsed)} 个板块")
                    result.update(parsed)
            except Exception as e:
                log.warning(f"[板块成分股] ⚠️ 本地缓存 {bf} 读取失败，尝试从网络下载: {e}")
                need_download.append(bf)
    else:
        # 没有配置通达信目录，全部从网络下载
        need_download = candidate_files[:]

    servers = TDX_BLOCK_SERVERS[:]

    if need_download:
        log.info(f"[板块成分股] 需从网络下载: {need_download}")
    else:
        log.info("[板块成分股] 所有板块文件已从本地缓存加载，无需下载")

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
                log.info(f"[板块成分股] 开始下载 {bf}...")
                raw = _download_block_file(api, host, port, bf)
                if raw and len(raw) > 386:
                    parsed = _parse_raw_block_gn(raw, bf)
                    if parsed:
                        result.update(parsed)
                        log.info(f"[板块成分股] ✅ {bf} 下载完成: {len(parsed)} 个板块")
                        # 写入本地缓存文件：先写临时文件，校验成功后原子替换，避免下载失败破坏旧文件
                        if block_cache_dir and _validate_downloaded_block_file(bf, raw):
                            try:
                                local_path = os.path.join(block_cache_dir, bf)
                                _safe_replace_file(local_path, raw)
                            except Exception as e:
                                log.warning(f"[板块成分股] ⚠️ 写入本地缓存 {bf} 失败: {e}")
                        need_download.remove(bf)
                else:
                    log.warning(f"[板块成分股] ⚠️ {bf} 下载失败或数据无效")

            api.disconnect()
            if not need_download:
                break

        except TypeError:
            import traceback
            traceback.print_exc()
            continue
        except Exception:
            import traceback
            traceback.print_exc()
            continue

    if not result:
        log.warning("[板块成分股] 所有服务器均下载失败，板块数据不可用")
        _BLOCK_GN_CACHE_LOADED = True
        return {}

    _BLOCK_GN_CACHE = result
    _BLOCK_GN_CACHE_LOADED = True
    log.info(f"[板块成分股] 解析完成，共 {len(result)} 个板块有成分股数据")
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
        log.warning("[板块刷新] 无法确定 hq_cache 目录，跳过板块文件下载")
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
        log.warning("[板块刷新] pytdx 未安装，无法刷新板块文件；保留旧文件")
        return

    refreshed = 0
    for host, port in TDX_BLOCK_SERVERS[:]:
        try:
            api = TdxHq_API(multithread=True)
            if not api.connect(host, port):
                continue
            log.info(f"[板块刷新] 已连接服务器 {host}:{port}")
            for bf in block_files:
                if _safe_refresh_one_block_file(api, host, port, bf, block_cache_dir, progress_callback=progress_callback):
                    refreshed += 1
            api.disconnect()
            break
        except Exception as e:
            log.warning(f"[板块刷新] 服务器 {host}:{port} 刷新失败: {e}")
            try:
                api.disconnect()
            except Exception:
                pass
            continue

    if refreshed == 0:
        log.warning("[板块刷新] 所有文件均未刷新成功，继续使用旧文件")
    else:
        log.info(f"[板块刷新] 刷新完成: {refreshed}/{len(block_files)} 个文件成功, 已保存到 {block_cache_dir}")

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
                    log.info(f"[板块成分股] ✅ 从 infoharbor_block.dat 读取 {len(result)} 个带代码板块")
            except Exception as e:
                log.warning(f"[板块成分股] ⚠️ 读取 infoharbor_block.dat 失败: {e}")

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
        log.info(f"[板块成分股] ✅ 从 infoharbor_block.dat 找到 '{item.get('name', sector_code)}' 共 {len(stocks)} 只成分股")
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
    log.info(f"[板块成分股] 查询 sector_code={sector_code}")

    # Step 1: 881xxx（研究行业新版）→ 本地 tdxhy.cfg
    if sector_code.startswith("881"):
        return _read_tdxhy_sector_stocks(sector_code)

    # Step 1.5: 港股指数（HSTECH/HSIDI 等）→ 恒指官方 Factsheet PDF。
    # 必须放在「标准指数→AKShare 中证指数」之前：否则 HSTECH 之类字母代码会落入
    # 中证指数接口（index_stock_cons_csindex），把恒指代码当 6 位中证代码请求，
    # 返回非 Excel 内容抛 "Excel file format cannot be determined"。
    if sector_code.upper() in _HK_INDEX_CODES:
        return _read_hk_index_stocks(sector_code.upper())

    # Step 2: 标准指数（000xxx / 399xxx 等）→ 本地或 AKShare 统一获取
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
                log.warning(f"[板块成分股] 读取tdxzs.cfg失败: {e}")

    if not sector_name:
        log.info(f"[板块成分股] 未在tdxzs.cfg中找到板块代码 {sector_code}")
        return []

    # 8803xx-8804xx（旧版行业）无成分股数据
    if sector_code.startswith("8803") or sector_code.startswith("8804"):
        log.warning(f"[板块成分股] 旧版行业代码 {sector_code}，无成分股数据。请使用 881 研究行业代码。")
        return []

    # 从 block_*.dat 缓存中查找
    cache = _download_block_gn_from_network()
    stocks = cache.get(sector_name, [])

    if stocks:
        if len(stocks) >= 400:
            log.warning(f"[板块成分股] ⚠️ 从旧 block_*.dat 找到 '{sector_name}' 共 {len(stocks)} 只，可能受 400 只上限影响")
        else:
            log.info(f"[板块成分股] ✅ 从旧 block_*.dat 找到 '{sector_name}' 共 {len(stocks)} 只成分股")
    else:
        log.error(f"[板块成分股] ❌ 旧 block_*.dat 缓存中未找到板块 '{sector_name}'")

    return stocks


# 权威市场 → 板块前缀（与通达信市场前缀约定一致：1=沪、0=深、2=北交所/新三板）
_MARKET_PREFIX = {"sh": "1", "sz": "0", "bj": "2"}


def _prefix_by_digit(code):
    """无权威市场信息时，按代码首数字兜底推断板块前缀。

    仅作 fallback：6xx→沪(1)、0x/3x→深(0)、8x/4x→京(2)、其余默认沪(1)。
    注意：首数字无法区分 9xx（沪B 900 / 北交所 920），故此兜底不够权威，
    凡能拿到 csindex「交易所」字段的上游必须优先用 _MARKET_PREFIX。
    """
    first = code[0]
    if first in "68":
        return "1"
    elif first in "03":
        return "0"
    elif first in "84":
        return "2"
    else:
        return "1"


def _index_cons_to_stocks(items):
    """将 AkshareAPI.fetch_index_cons 的 {code, market} 结果转为板块成分结构。

    优先采用 csindex「交易所」字段给出的权威市场（_MARKET_PREFIX），
    只有拿不到才退回首数字兜底。此前旧规则 first in "689" 会把北交所
    8xxxxx/920xxx 误判为沪市(prefix=1)——北交所从未被测试故一直潜伏，
    现以权威 market 为准予以修复。
    items: list[dict]，每项 {"code", "market"}。
    """
    stocks = []
    seen = set()
    for it in items:
        code = it.get("code", "")
        if code in seen:
            continue
        seen.add(code)
        prefix = _MARKET_PREFIX.get(it.get("market")) or _prefix_by_digit(code)
        stocks.append({"code": code, "prefix": prefix, "name": code})
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
        log.info(f"[板块成分股] {source_label} 返回未知列名: {list(df.columns)}")
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
            stocks.append({"code": code, "prefix": _prefix_by_digit(code), "name": code})

    return stocks


def _run_with_timeout(fn, timeout):
    """在守护线程中执行阻塞网络抓取，限时返回，避免扫描卡死。

    背景：akshare/requests 等成分抓取可能因网络异常无限阻塞（无超时、原生调用
    不可打断）。扫描「成分股」来源在 API 线程池线程里解析股票清单，若阻塞则
    「中断扫描」也结束不了（见 App/app 单组来源 page_index 卡死历史）。本函数：
      守护线程执行，主线程 join 分片轮询——超过 timeout 直接放弃，返回 None。
    （P1-5 后 abort_check 中止链路已随任务级 cancel 语义下线，参数随之删除。）
    返回 fn 结果；超时返回 None；fn 内部异常原样透出（调用方捕获）。
    """
    import threading
    box = {}

    def _run():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001 —— 集中透出，调用方按需捕获
            box["e"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    waited, step = 0.0, 0.5
    while waited < timeout:
        t.join(timeout=step)
        waited += step
        if not t.is_alive():
            break
    if t.is_alive():
        log.warning(f"[板块成分股] 网络获取超时(>{timeout:.0f}s)，返回空（可重试）")
        return None
    if "e" in box:
        raise box["e"]
    return box.get("r")


def _read_sh_index_stocks_exchange():
    """上证指数(000001)成分 = 沪市全部A股，上交所官网 query.sse.com.cn 直连。

    与深证成指(399001)直连深交所官网一致：从上交所官方查询接口实时获取当前
    「全部A股」清单（主板A股 STOCK_TYPE=1 + 科创板 STOCK_TYPE=8 两段合并去重），
    而不走通达信本地 vipdoc/sh/lday 枚举（该目录会混入其它指数/基金/代码段，
    market+code 整改后不可再当作指数成分来源）。
    返回 [{"code","prefix","name"}, ...]；网络抓取经 _run_with_timeout 限时。
    """
    import requests

    url = "https://query.sse.com.cn/sseQuery/commonQuery.do"
    headers = {
        "Host": "query.sse.com.cn",
        "Pragma": "no-cache",
        "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36",
    }

    def _fetch():
        stocks, seen = [], set()
        for stock_type in ("1", "8"):  # 1=主板A股, 8=科创板
            params = {
                "STOCK_TYPE": stock_type,
                "REG_PROVINCE": "",
                "CSRC_CODE": "",
                "STOCK_CODE": "",
                "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
                "COMPANY_STATUS": "2,4,5,7,8",
                "type": "inParams",
                "isPagination": "true",
                "pageHelp.cacheSize": "1",
                "pageHelp.beginPage": "1",
                "pageHelp.pageSize": "10000",
                "pageHelp.pageNo": "1",
                "pageHelp.endPage": "1",
            }
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            for row in data.get("result") or []:
                code = str(row.get("A_STOCK_CODE", "")).strip()
                if "." in code:
                    code = code.split(".")[0]
                name = str(row.get("SEC_NAME_CN", "")).strip()
                if len(code) != 6 or not code.isdigit():
                    continue
                if not code.startswith("6"):
                    continue  # 仅A股（主板600/601/603 + 科创688/689）
                if code in seen:
                    continue
                seen.add(code)
                stocks.append({"code": code, "prefix": "1", "name": name or code})
        return stocks

    return _run_with_timeout(_fetch, timeout=25)


def _normalize_hk_code(raw):
    """把港股成分代码规整为 5 位零填充（00700 等）；非法返回 None。"""
    if raw is None:
        return None
    m = re.search(r"(\d{1,5})", str(raw).strip())
    if not m:
        return None
    return m.group(1).zfill(5)


def _build_hk_stock_records(codes):
    """codes: 港股代码列表 → [{code,prefix:'hk',name}]（去重保序）"""
    seen, stocks = set(), []
    for c in codes or []:
        c = _normalize_hk_code(c)
        if c is None or c in seen:
            continue
        seen.add(c)
        stocks.append({"code": c, "prefix": "hk", "name": c})
    return stocks


def _extract_hk_codes_from_pdf_text(texts):
    """从恒指 Factsheet 页文本提取成分代码（5 位零填充）。

    因子 1（用作主解析）——**ISIN 锚点**，与 PDF 引擎排版无关：
    港股/中概企业在港上市证券的 ISIN 一律以 KY / CN / HK 开头（12 位），
    每一成分行必含 ISIN。因此只要某行出现这种 ISIN，就取紧贴在它**前方**
    的 1~5 位数字作为股票代码。该法不依赖「代码在第几列」，
    因此无论 pypdf 是按空格分列、粘连成 '0700KYG...'、还是换行位置不同，
    都能命中；同时天然排除指数行/日期/权重等无 ISIN 的噪声。

    因子 2（兜底）——旧「CONSTITUENTS 段 + 代码+ISIN 两 token」严格模式，
    应对个别引擎把行内文本排成不同顺序的场景。
    """

    def _anchor(code_list):
        """以通用 12 位 ISIN 为锚收集紧邻其前的短数字代码。

        证券 ISIN 一律 12 位：2 位国家/地区码 + 9 位字母数字 + 1 位数字校验，
        如 KYG875721634（开曼）/ BMG5984D0714（百慕大）/ HK0000072722（香港）。
        不可只认 KY/CN/HK，否则会漏掉注册地在百慕大(BM)、泽西(JE)等地的
        成分公司（如华润啤酒 03320 即 BM 开头）。逐行锚定 ISIN 后取紧邻前
        方的短数字作为代码，与列序、粘连与否（'0700KYG...'）均无关。
        """
        for text in texts:
            if not text:
                continue
            for line in text.splitlines():
                for m in re.finditer(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", line):
                    pre = line[: m.start()].rstrip()
                    cm = re.search(r"(\d{1,5})$", pre)
                    if cm:
                        code_list.append(cm.group(1).zfill(5))

    def _strict(code_list):
        """兜底：CONSTITUENTS 段之后『首列1-5位数字 + 第2列12位ISIN』。"""
        for text in texts:
            if not text:
                continue
            idx = text.find("CONSTITUENTS")
            seg = text[idx:] if idx >= 0 else text
            for line in seg.splitlines():
                toks = line.split()
                if len(toks) < 2:
                    continue
                if re.fullmatch(r"\d{1,5}", toks[0]) and re.fullmatch(r"[0-9A-Z]{6,15}", toks[1]):
                    code_list.append(toks[0].zfill(5))

    codes = []
    _anchor(codes)
    if len(codes) < 8:
        codes = []
        _strict(codes)
    return codes


def _parse_pdf_hk_codes(pdf_bytes):
    """多引擎解析恒指 Factsheet PDF 成分表，返回 5 位代码列表。

    依序尝试 pdfplumber / PyMuPDF / pypdf / pdfminer.six——任一引擎取到
    >=8 个成分代码即采纳（过滤解析噪声），否则回退下一引擎；全失败返回 []。
    各库均在使用时按需 import，缺哪个就跳过哪个，保证零硬依赖。
    """
    from io import BytesIO

    # 1) pdfplumber（列对齐最好；x_tolerance=2 防止 iso 列被合并、丢失前导0）
    try:
        import pdfplumber
        texts = []
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for p in pdf.pages:
                texts.append(p.extract_text(x_tolerance=2) or "")
        codes = _extract_hk_codes_from_pdf_text(texts)
        if len(codes) >= 8:
            return codes
    except Exception:
        pass

    # 2) PyMuPDF
    try:
        import fitz
        texts = []
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for pg in doc:
                texts.append(pg.get_text() or "")
        codes = _extract_hk_codes_from_pdf_text(texts)
        if len(codes) >= 8:
            return codes
    except Exception:
        pass

    # 3) pypdf（务必优先普通 extract_text()：新版 pypdf(实测 6.16.2) 的
    #    extraction_mode="layout" 会把代码列与 ISIN 列拆到不同行，破坏锚点解析；
    #    普通模式返回 '3690 KYG596691041 MEITUAN' 式逐行文本，跨版本一致）。
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(pdf_bytes))
        texts = []
        for pg in reader.pages:
            try:
                texts.append(pg.extract_text() or "")
            except TypeError:
                # 极旧版 pypdf 无普通模式则回退 layout
                texts.append(pg.extract_text(extraction_mode="layout") or "")
        codes = _extract_hk_codes_from_pdf_text(texts)
        if len(codes) >= 8:
            return codes
    except Exception:
        pass

    # 4) pdfminer.six
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(BytesIO(pdf_bytes))
        codes = _extract_hk_codes_from_pdf_text([text])
        if len(codes) >= 8:
            return codes
    except Exception:
        pass

    return []


def _pdf_parse_diagnostics(pdf_bytes, sector_code):
    """解析失败时输出 PDF 库版本 + 抽取文本片段，便于定位真实排版差异。"""
    from io import BytesIO
    texts, engine, ver = [], None, None
    try:
        import pypdf
        from pypdf import PdfReader
        engine, ver = "pypdf", getattr(pypdf, "__version__", "?")
        try:
            reader = PdfReader(BytesIO(pdf_bytes))
            texts = [(p.extract_text() or "") for p in reader.pages]
        except TypeError:
            reader = PdfReader(BytesIO(pdf_bytes))
            texts = [(p.extract_text(extraction_mode="layout") or "") for p in reader.pages]
    except Exception:
        engine = "pypdf(不可用)"
    joined = "".join(texts)
    idx = joined.find("CONSTITUENTS")
    snippet = (joined[idx: idx + 300]) if idx >= 0 else joined[:300]
    log.info(f"[板块成分股][诊断] {sector_code}: 解析引擎={engine} 版本={ver} 文本长度={len(joined)}")
    log.info(f"[板块成分股][诊断] {sector_code}: 文本片段>>> {snippet!r}")


def _read_hk_index_stocks(sector_code):
    """港股指数（HSTECH/HSIDI 等）成分股：恒生指数公司官方 Factsheet PDF。

    返回 [{code, prefix:'hk', name}, ...]；下载与解析均走 _run_with_timeout 限时。
    sector_code 应为大写字母指数代码。
    """
    import requests

    sheet_key = _HK_INDEX_FACT_SHEET.get(sector_code)

    def _fetch_fact_sheet():
        url = ("https://www.hsi.com.hk/static/uploads/contents/en/"
               f"dl_centre/factsheets/{sheet_key}.pdf")
        r = requests.get(url, headers={"User-Agent": _HK_UA}, timeout=25)
        r.raise_for_status()
        return r.content

    # ── 第一优先级：恒生指数公司官方 Factsheet PDF（权威，覆盖全部支持指数）──
    if sheet_key:
        try:
            pdf_bytes = _run_with_timeout(_fetch_fact_sheet, timeout=30)
            if not pdf_bytes:
                log.info(f"[板块成分股] 恒指官方 Factsheet 下载超时/中断: {sector_code}")
            else:
                codes = _parse_pdf_hk_codes(pdf_bytes)
                stocks = _build_hk_stock_records(codes)
                if stocks:
                    log.info(f"[板块成分股] ✅ 恒生指数公司 Factsheet 获取 '{sector_code}' "
                             f"共 {len(stocks)} 只港股成分股")
                    return stocks
                _pdf_parse_diagnostics(pdf_bytes, sector_code)
                log.warning(f"[板块成分股] 恒指官方 Factsheet 解析无成分: {sector_code}"
                            f"（未安装 pdfplumber/PyMuPDF/pypdf/pdfminer 任一 PDF 解析库？）")
        except Exception as e:
            log.warning(f"[板块成分股] 恒指官方 Factsheet 获取 {sector_code} 失败: {e}")
            import traceback
            traceback.print_exc()

    log.warning(f"[板块成分股] 港股指数 {sector_code} 成分获取失败（官方 Factsheet 下载/解析异常）")
    return []


def _fetch_csi_index_stocks(sector_code):
    """中证指数成分股统一取数（经 AkshareAPI.fetch_index_cons 收口）。

    中证四大指数（000300/000905/000852/000688）与「其他指数（000xxx 非中证、
    932xxx 等）」的 csindex 回退逻辑相同，提取归一复用。限时 25s。
    返回 [{"code","prefix","name"}, ...]；空 / 失败返回 []。
    """
    try:
        items = _run_with_timeout(
            lambda: fetch_index_cons(sector_code),
            timeout=25)
        if not items:
            log.info(f"[板块成分股] 中证指数 返回空数据: {sector_code}")
            return []
        stocks = _index_cons_to_stocks(items)
        log.info(f"[板块成分股] ✅ 中证指数 获取 '{sector_code}' 共 {len(stocks)} 只成分股")
        return stocks
    except Exception as e:
        log.warning(f"[板块成分股] 中证指数 获取 {sector_code} 失败: {e}")
        import traceback
        traceback.print_exc()
        return []


def _read_standard_index_stocks(sector_code):
    """
    根据指数代码获取成分股。

    路由逻辑：
    - 上证指数（000001）：综合指数 = 沪市全部A股（上交所官网直连，与 399xxx 风格一致）
    - 中证指数（000300/000905/000852/000688 等）：中证指数官网（官方直连）
    - 深交所指数（399xxx）：深交所官网 ShowReport XLS（官方直连）

    所有网络抓取均限时（_run_with_timeout），避免扫描因网络阻塞而卡死。
    """
    # ── 上证指数（000001）：综合指数 = 沪市全部A股（上交所官网直连）──
    # 放最前：000001 与 399xxx 同风格从上交所官网取全部A股，不走通达信本地
    # vipdoc/sh/lday 枚举（该目录还混有其它指数，不能作指数成分来源）。
    if sector_code == "000001":
        stocks = _read_sh_index_stocks_exchange()
        log.info(f"[板块成分股] 📈 上证指数(000001) 上交所A股共 {len(stocks or [])} 只")
        return stocks or []

    # ── 中证指数（000300/000905/000852/000688 等）→ csindex（经 AkshareAPI 收口）──
    CSI_INDICES = {"000300", "000905", "000852", "000688"}
    if sector_code in CSI_INDICES:
        return _fetch_csi_index_stocks(sector_code)

    # ── 深交所指数（399xxx）→ 深交所官网 XLS 直连（限时+可中断）──
    if sector_code.startswith("399"):
        try:
            import requests
            import pandas as _pd
            from io import BytesIO

            url = f"https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xls&CATALOGID=1747_zs&ZSDM={sector_code}"

            def _fetch():
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                r.raise_for_status()
                for engine in [None, "xlrd"]:
                    try:
                        return _pd.read_excel(BytesIO(r.content), dtype=str, engine=engine)
                    except Exception:
                        if engine is None:
                            continue
                        raise

            df = _run_with_timeout(_fetch, timeout=25)
            if df is None:
                return []
            if df.empty:
                log.info(f"[板块成分股] 深交所 返回空数据: {sector_code}")
                return []
            stocks = _parse_stocks_from_df(df, f"深交所({sector_code})")
            log.info(f"[板块成分股] ✅ 深交所 获取 '{sector_code}' 共 {len(stocks)} 只成分股")
            return stocks
        except Exception as e:
            log.warning(f"[板块成分股] 深交所 获取 {sector_code} 失败: {e}")
            import traceback
            traceback.print_exc()
            return []

    # ── 其他指数（000xxx 非中证、932xxx 等）→ 尝试 csindex（经 AkshareAPI 收口）──
    return _fetch_csi_index_stocks(sector_code)


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
        log.warning("[板块成分股] hq_cache 目录不存在，无法读取 tdxhy.cfg")
        _TDXHY_CACHE = result
        return result

    tdxhy_path = os.path.join(hq_cache, "tdxhy.cfg")
    if not os.path.exists(tdxhy_path):
        log.warning(f"[板块成分股] tdxhy.cfg 不存在: {tdxhy_path}")
        _TDXHY_CACHE = result
        return result

    try:
        with open(tdxhy_path, "r", encoding="gbk", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        log.warning(f"[板块成分股] 读取 tdxhy.cfg 失败: {e}")
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
    log.info(f"[板块成分股] 解析 tdxhy.cfg 完成: {len(lines)} 行, {len(result)} 个X代码, "
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
        log.info(f"[板块成分股] 881代码 {sector_code} 不在映射表中")
        return []

    x_code, sector_name = _TDXHY_881_TO_X[sector_code]
    log.info(f"[板块成分股] 板块名称: '{sector_name}' (代码={sector_code}, X={x_code})")

    # 解析 tdxhy.cfg
    x_to_stocks = _parse_tdxhy_cfg()
    if not x_to_stocks:
        log.info("[板块成分股] tdxhy.cfg 缓存为空")
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
        log.info(f"[板块成分股] {level}聚合: X={x_code} → 子级={child_codes}{more} → 共 {len(all_stocks)} 只成分股")

    # 去重
    seen = set()
    stocks = []
    for s in all_stocks:
        key = s["code"]
        if key not in seen:
            seen.add(key)
            stocks.append(s)

    if stocks:
        log.info(f"[板块成分股] ✅ tdxhy.cfg 找到 '{sector_name}' 共 {len(stocks)} 只成分股")
    else:
        log.error(f"[板块成分股] ❌ tdxhy.cfg 中未找到 '{sector_name}' 的成分股")

    return stocks


# ============================================================
# save_to_zxg_blk 与 _ZXG_HK_INDEX_MAP/_ZXG_US_INDEX_MAP 属业务数据层
# 职责，实现位于 App/AppData.py。
# 调用方式：from App.AppData import app_data
#           app_data.save_to_zxg_blk(codes)
# ============================================================

# ============================================================
if __name__ == "__main__":
    print("通达信本地文件数据源模块已加载。")
    print("可用功能：")
    print("  - CTdxAPI: 适配器类（P2 已删 CTdxAPI_Sliced 切片适配器）")
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
    print("  - get_xdxr_data(): 获取除权除息数据（DataAPI/ElTdxAPI.py · 当前仅 eltdx）")
    print("  - 通过 set_tdx_config(forward_adjust_enabled=True) 启用前复权")
    print("")
    print("板块功能：")
    print("  - get_index_stocks(): 获取指数/板块成分股（88x→tdxhy/block, 标准指数→AKShare）")
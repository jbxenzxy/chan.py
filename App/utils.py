# -*- coding: utf-8 -*-
"""
App/utils.py —— 代码解析工具（阶段 8 自 AppEngine 下沉）
=========================================================================
设计文档 10.1：完成最后一步拆分，my_chan_main.py 职责被各层完全吸收。

本模块收纳证券代码解析函数（_get_stock_name / _get_stock_market_code /
_get_market_code），自 AppEngine 下沉至此。这些函数依赖配置/数据源
（app_config / app_data / DataAPI），为保持本模块顶层纯净，依赖一律
在函数内 import。

清理记录：原随 my_chan_main.py 迁入的纯算法函数（ema / calculate_macd /
_inherit_macd_for_preview_bar / _get_date_fmt / _get_kl_type /
_get_freq_label / _find_left_shoulder_time / _bi_overlap_range /
_calc_zs_confirm_edt_from_bis / safe_write_json_file /
_send_windows_notification）与 AppEngine.py 内同名完整实现重复且全库
无人引用，已删除；AppEngine.py 内实现为唯一事实源。
"""
import os
import re


def _get_stock_name(market, code):
    """获取股票名称（委托 app_data.get_stock_name）"""
    from App.AppData import app_data
    return app_data.get_stock_name(market, code)


def _get_stock_market_code(code):
    """识别股票/指数代码，返回 (market, code)；无法识别返回 (None, code)。"""
    from App.AppConfig import app_config
    # 通达信扩展市场指数别名：中证2000 本地K线在 ds 目录，文件名为 62#932000
    _DS_INDEX_ALIASES = {
        "ZZ2": ("ds", "932000"),
        "ZZ2000": ("ds", "932000"),
        "中证2000": ("ds", "932000"),
        "932000": ("ds", "932000"),
    }
    if code in _DS_INDEX_ALIASES:
        return _DS_INDEX_ALIASES[code]

    # 港股指数别名映射：将用户输入的指数简称映射到通达信港股数据文件实际代码
    _HK_INDEX_ALIASES = {
        "HSTECH": ("hk", "HSTECH"),   # 恒生科技指数
        "HSI": ("hk", "HSI"),         # 恒生指数
        "HSCEI": ("hk", "HSCEI"),     # 恒生中国企业指数
        "HSCCI": ("hk", "HSCCI"),     # 恒生香港中资企业指数
    }
    if code in _HK_INDEX_ALIASES:
        return _HK_INDEX_ALIASES[code]

    # 港股数字代码规范化：通达信文件统一使用5位（4位需补前导零，如 9926 -> 09926）
    def _norm_hk(c):
        if c.isdigit() and len(c) == 4:
            return '0' + c
        return c

    prefix_match = re.match(r'^(SH|SZ|HK|DS)(\d+)$', code)
    if prefix_match:
        mkt = prefix_match.group(1).lower()
        c = prefix_match.group(2)
        if mkt == 'ds':
            return mkt, c
        return mkt, _norm_hk(c) if mkt == 'hk' else c
    # HK前缀 + 非数字代码（如 HKHSTECH、HKHSI）
    prefix_alpha_match = re.match(r'^HK([A-Z]+)$', code)
    if prefix_alpha_match:
        return 'hk', prefix_alpha_match.group(1)
    suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK|DS)$', code)
    if suffix_match:
        mkt = suffix_match.group(2).lower()
        c = suffix_match.group(1)
        if mkt == 'ds':
            return mkt, c
        return mkt, _norm_hk(c) if mkt == 'hk' else c
    # .HK 后缀 + 非数字代码（如 HSTECH.HK）
    suffix_alpha_match = re.match(r'^([A-Z]+)\.HK$', code)
    if suffix_alpha_match:
        return 'hk', suffix_alpha_match.group(1)
    # 自动判断：5位纯数字优先识别为港股（如 00700）
    if len(code) == 5 and code.isdigit():
        return 'hk', code
    if len(code) == 4 and code.isdigit():
        return 'hk', '0' + code
    # 6位代码：先检查是否是港股（在ds目录下有对应文件）
    if len(code) == 6 and code.isdigit():
        hk_file = os.path.join(app_config.vipdoc_dir, "ds", "lday", f"31#{code}.day")
        if os.path.exists(hk_file):
            return 'hk', code
        ds_file = os.path.join(app_config.vipdoc_dir, "ds", "lday", f"62#{code}.day")
        if os.path.exists(ds_file):
            return 'ds', code
    # A股判断
    if code.startswith('6'):
        return 'sh', code
    if code.startswith('5'):
        return 'sh', code  # 5xxxxx: 沪市ETF(51/56/58/59/588)、基金(50)等
    if code.startswith('88') or code.startswith('99'):
        return 'sh', code  # 88xxxx: 通达信板块指数; 99xxxx: 指数
    if code.startswith('0') or code.startswith('3'):
        return 'sz', code
    if code.startswith('1'):
        return 'sz', code  # 1xxxxx: 深市ETF(15/16/18)、债券等
    # 搜索
    for m in ['sh', 'sz']:
        f = os.path.join(app_config.vipdoc_dir, m, "lday", f"{m}{code}.day")
        if os.path.exists(f):
            return m, code
    f = os.path.join(app_config.vipdoc_dir, "ds", "lday", f"31#{code}.day")
    if os.path.exists(f):
        return 'hk', code
    f = os.path.join(app_config.vipdoc_dir, "ds", "lday", f"62#{code}.day")
    if os.path.exists(f):
        return 'ds', code
    return None, code


def _get_market_code(code):
    """
    解析代码，返回 (market, code)
    market: 'sh' / 'sz' / 'hk' / 'ds' / 'futures'
    """
    try:
        from DataAPI.TqSdkAPI import _get_futures_code
    except ImportError:
        _get_futures_code = None
    code = code.strip().upper()
    if _get_futures_code:
        futures_code = _get_futures_code(code)
        if futures_code:
            return 'futures', futures_code
    return _get_stock_market_code(code)

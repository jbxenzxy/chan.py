# -*- coding: utf-8 -*-
"""
App/AppEngine.py —— 分析引擎层
=========================================================================
承载股票/指数缠论分析核心（分析函数 + 工具函数 + 模块级常量/状态），
由 App/AppOrch.py（业务编排层）和 FrontAPI.py（API 入口层）引用。

文件内按 5 区域划分（见各区域分隔头）：
  - 区域 1 · 模块常量与配置（import + 配置别名 + 业务常量）
  - 区域 2 · 数据委托层（对 AppData 数据访问的薄委托壳 + 选点 schema 常量）
  - 区域 3 · 股票分析（_analyze_stock_internal 编排）
  - 区域 4 · 结果提取（_extract_main_level_data / _extract_sub_level_data）
  - 区域 5 · 公开入口（analyze_stock + _SUB_FREQ_MAP）

依赖方向：AppEngine → App/AppConfig.py / App/AppData.py / DataAPI/（单向）；
App/AppOrch.py → AppEngine（单向）；FrontAPI.py 仅引用常量/纯函数，引擎
入口一律走 orch.* 漏斗。期货分流：analyze_stock 延迟导入 AppSSE（避免循环依赖）。

使用方式：
    from App import AppEngine as _m      # 服务层获取引擎
    from App import AppEngine as m       # API 层读取常量/纯函数
"""
# ═══════════════════════════════════════════════════════════════════════
# 区域 1 · 模块常量与配置
# ═══════════════════════════════════════════════════════════════════════

import sys, os, time, threading, gc
from datetime import datetime, timedelta

# 区间套辅助函数（实现位于 BSPointList.py）：_main_bi_range（选点左肩定位，
# _analyze_stock_internal/_extract_sub_level_data 使用）与 _stocks_red_range
# （股票双窗口红框，_extract_main_level_data 使用）
from BuySellPoint.BSPointList import _main_bi_range, _stocks_red_range

# ============================================================
# 配置区域 —— 中心化于配置层
# 基础设施配置：App/AppConfig.py（环境变量 / 仓库根 .env 优先）
# 算法参数 + 默认代码：ChanConfig.py（SYMBOL_CODE 环境变量可覆盖）
# 引用一律 app_config.<属性> 直读（无路径别名）
# ============================================================
from App.AppConfig import app_config

# 业务数据层（缓存/持久化/标注/自选股的实现单源；本文件同名函数为
# 薄委托壳，状态名为共享同一对象的别名）
from App.AppData import app_data
from App.AppData import SAVED_POINT_COLUMNS as AppData_SAVED_POINT_COLUMNS
from App.AppData import FREQ_TO_COL as AppData_FREQ_TO_COL
# 缓存键结构化键工厂（消除字符串拼接歧义与漂移）
from App.AppData import make_single_key, make_dual_main_key, make_dual_sub_key

from ChanConfig import get_symbol_code
SYMBOL_CODE = get_symbol_code()                                        # 默认股票代码（SYMBOL_CODE，上证指数）

from App.AppLog import get_logger
log = get_logger(__name__)

# 引擎纯函数/常量 + 证券代码解析公共工具（MACD/EMA、周期映射、日期格式、
# 左肩定位、中枢确认、代码解析等，实现位于 App/utils.py；AppSSE/FrontAPI
# 直接从 App.utils 导入；此处仅 import 引擎自身消费的符号）
from App.utils import (
    _get_stock_name, _get_stock_market_code, _get_market_code,
    _get_kl_type, _get_freq_label,
    _make_chan_config, calculate_macd,
    INTRADAY_FREQS, _get_date_fmt,
    _calc_zs_confirm_edt_from_bis, _find_left_shoulder_time,
)

# ============================================================
# 天勤期货/期指行情配置
# ============================================================
# 账户和密码从 vipdoc（即 tdx_install_dir\vipdoc）下 tq_account.json 文件读取
# 文件格式: {"account": "手机号或用户名", "password": "密码"}
# SSE 调试旗读取配置中心 AppConfig（单一事实源 app_config.sse_debug）。

# 将 chan.py 和当前脚本目录都添加到搜索路径
if app_config.chan_path not in sys.path:
    sys.path.insert(0, app_config.chan_path)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# ============================================================
# 导入 chan.py 核心模块
# ============================================================
try:
    from Chan import CChan
    from Common.CEnum import AUTYPE, KL_TYPE, FX_TYPE
except ImportError as e:
    log.error(f"\n[错误] chan.py 导入失败: {e}")
    log.info(f"[提示] 请确保 CHAN_PATH = r'{app_config.chan_path}' 指向正确的 chan.py 仓库目录")
    sys.exit(1)

# 导入通达信数据源适配器（chan.py 的 DataAPI 目录）
# 包含：K线读取、前复权（自选股读写位于 App/AppData.py）
# K线读取统一经 CTdxAPI（CCommonStockApi 实现）访问，引擎层不直连
# 模块级 read_main_level_records / read_sub_level_records。
from DataAPI.TdxAPI import CTdxAPI

# 前复权开关：True=开启前复权（消除分红送股的跳空缺口），False=关闭（不复权，原样输出）
# 读取配置中心 AppConfig（单一事实源 app_config.forward_adjust_enabled，.env/环境变量可覆盖）；
# 保持符号名以兼容消费侧（func_map_check ORCH_E）。
FORWARD_ADJUST_ENABLED = app_config.forward_adjust_enabled

# 调试模式：冷启动只从指定日期开始加载(所有周期有效)，None表示不开启。如果该日期前无通达信数据，则有多少加载多少
DEBUG_COLD_START_START_DATE = app_config.debug_cold_start_start_date

# 调试模式：用于解决冷启动起不来的问题；冷启动加载到此日期(仅日K生效)，None表示不开启
DEBUG_COLD_START_END_DATE = app_config.debug_cold_start_end_date

# 注入通达信数据源配置到 TdxAPI 模块
from DataAPI.TdxAPI import set_tdx_config as _set_tdx_config
_set_tdx_config(
    vipdoc_dir=app_config.vipdoc_dir,
    forward_adjust_enabled=FORWARD_ADJUST_ENABLED,
)

# 行业映射单源注入：tdxhy_mapping_data.py 为 App/ 下独立数据文件，
# 由 AppData.load_tdxhy_mapping 单一函数加载；DataAPI 与 App 互不依赖，
# TdxAPI 侧经 set_tdx_hy_mapping 注入（与 set_tdx_config 同一注入模式）。
# 加载失败硬失败（ValueError），不静默降级空表。
from DataAPI.TdxAPI import set_tdx_hy_mapping as _set_tdx_hy_mapping
_set_tdx_hy_mapping(*app_data.load_tdxhy_mapping())

# 全量数据模式：True=加载全部K线不做时间截断；False=默认模式
# 读取配置中心 AppConfig（单一事实源 app_config.full_data_mode）。
FULL_DATA_MODE = app_config.full_data_mode

# 股票K线回看配置（仅 FULL_DATA_MODE=False 时生效）：{周期: (K线根数, 显示文本)}
# 语义与期货 FUTURES_LOOKBACK_CONFIG 一致：保留最近 N 根K线；N<=0 表示不限制。
# 读取配置中心 AppConfig（单一事实源 app_config.stocks_lookback_config）。
STOCKS_LOOKBACK_CONFIG = app_config.stocks_lookback_config

# 双窗下窗「对齐不足降全量」阈值：下窗按上窗时间区间对齐截断后，
# K线根数低于此值时降为全量（数据源覆盖不足的兜底，见 AppConfig 注释）。
# 单一事实源在 app_config.dual_sub_fallback_min。
DUAL_SUB_FALLBACK_MIN = app_config.dual_sub_fallback_min

# 期货回看根数纯函数（不依赖 tqsdk 安装，单独导入保证恒可用）：
# TqSdkAPI 模块顶层无硬 import tqsdk，resolve_lookback_bars 读取 AppEngine
# 注入的 FUTURES_LOOKBACK_CONFIG，与 CTqSdkAPI/tqsdk 是否可用无关，
# 故放在 tqsdk 的 try/except 之外，避免 tqsdk 未装时被置 None 的边界问题。
from DataAPI.TqSdkAPI import resolve_lookback_bars

# 导入天勤数据源适配器（期货/期指）
# 频率映射/别名/支持列表/fetch_kline 一律经 CTqSdkAPI 元数据接口访问
# （CommonStockAPI 抽象层），不直接 import。
try:
    from DataAPI.TqSdkAPI import (CTqSdkAPI, _get_futures_name, load_tq_account,
                                  set_futures_lookback_config)
    load_tq_account(app_config.vipdoc_dir)
    # 注入期货历史回看配置（AppConfig 单一事实源 → DataAPI 层，单向）
    set_futures_lookback_config(app_config.futures_lookback_config)
    TQ_AVAILABLE = True
except ImportError as e:
    CTqSdkAPI = None
    _get_futures_name = None
    load_tq_account = None
    set_futures_lookback_config = None
    TQ_AVAILABLE = False
    log.warning(f"[警告] 天勤数据源未安装: {e}，期货功能不可用。pip install tqsdk")


# ============================================================
# 同花顺自选股同步（云端 API）
# ============================================================
try:
    from Script.ths_cloud_api import save_scan_to_ths_cloud   # 同花顺云端扫描工具（Script/）
    _THS_CLOUD_AVAILABLE = True
except ImportError:
    save_scan_to_ths_cloud = None
    _THS_CLOUD_AVAILABLE = False


# ============================================================
# 通达信数据读取
# ============================================================

# ============================================================
# 盘后数据下载引擎（基于 eltdx）—— 实现位于 DataAPI/ElTdxAPI.py
# 下载函数族与下载状态单一事实源在 ElTdxAPI，本模块共享其引用。
# ============================================================
from DataAPI import ElTdxAPI as _ElTdx
_ELTDX_AVAILABLE = _ElTdx._ELTDX_AVAILABLE
TdxClient = _ElTdx.TdxClient

# 下载状态管理（单一事实源：ElTdxAPI 模块级对象，本模块共享同一引用）
_download_state = _ElTdx._download_state
_download_lock = _ElTdx._download_lock

# TDX 市场代码映射
TDX_MARKET_MAP = {
    "sh": 1,   # 上海
    "sz": 0,   # 深圳
    "bj": 2,   # 北京
}

# 扩展市场（港股）代码前缀
HK_CODE_PREFIX = "31"  # 港股在 ds/lday 下的文件名前缀
DS_CODE_PREFIX = "62"  # 扩展市场指数在 ds/lday 下的文件名前缀


# ============================================================
# 获取股票名称（实现位于 App/utils.py，顶部统一 re-import）
# ============================================================


# 股票名称/PE/市值缓存状态：别名 = app_data 实例字段（共享同一对象）
# key: 股票代码(6位), value: {"name": "股票名称", "pinyin": "拼音首字母"}
# ═══════════════════════════════════════════════════════════════════════
# 区域 2 · 数据委托层
# ═══════════════════════════════════════════════════════════════════════

_stock_names_cache = app_data.names_cache

def _load_stock_names_from_cache_file():
    """从 stock_names.json 加载股票名称到内存（委托 app_data）"""
    return app_data.load_stock_names_from_cache_file()



# ============================================================
# 原子 JSON 写（持久化底座；实现位于 App/AppData.safe_write_json_file）
# ============================================================
def _safe_write_json_file(path, data, *, ensure_ascii=False, indent=None):
    """先写临时文件并校验 JSON 可读，再原子覆盖（委托 app_data）"""
    from App.AppData import safe_write_json_file
    return safe_write_json_file(path, data, ensure_ascii=ensure_ascii, indent=indent)


# ============================================================
# PE-TTM 缓存（腾讯接口，增量刷新；实现位于 App/AppData.py）
# ============================================================
_pe_ttm_cache = app_data.pe_cache        # {market+code: float}  PE-TTM值（共享对象）

# 指数归属缓存（AKShare在线获取，与PE-TTM一起保存到stock_pettm_index.json）
# key: market+code（如 "sh600519"）, value: "沪深300"|"中证500"|"中证1000"
_index_belong_cache = app_data.belong_cache


def _load_pe_ttm_cache():
    """从 stock_pettm_index.json 加载 PE-TTM 与指数归属（委托 app_data）"""
    return app_data.load_pe_ttm_cache()


def _get_pe_ttm(market, code):
    """获取单只股票的 PE-TTM 值（委托 app_data）"""
    return app_data.get_pe_ttm(market, code)


def _get_index_belong(market, code):
    """获取单只股票的指数归属（委托 app_data）"""
    return app_data.get_index_belong(market, code)


def _collect_codes_from_vipdoc(vipdoc_dir):
    """委托 DataAPI/ElTdxAPI 收集 vipdoc 目录证券代码（vipdoc_dir 由调用方注入）"""
    return _ElTdx.collect_codes_from_vipdoc(vipdoc_dir)


# ============================================================




# ============================================================
# 缠论分析（chan.py 版本）
# ============================================================


# 统一缓存（实现与状态在 App/AppData.py；别名共享同一对象）
_stocks_analysis_cache = app_data.stocks_analysis_cache   # 分析结果 LRU
_cache_lock = app_data.cache_lock                        # 保护缓存的并发读写

# 全市场流通市值缓存（通过腾讯接口获取，本地JSON兜底；实现位于 app_data）
def _load_float_mc_cache():
    """从本地JSON加载流通市值缓存（委托 app_data）"""
    return app_data.load_float_mc_cache()

def _update_float_mc_cache(mv_dict):
    """将外部获取的流通市值字典合并到全局缓存并落盘（委托 app_data）。
    调用方应确保 _load_float_mc_cache() 已先执行。"""
    return app_data.update_float_mc_cache(mv_dict)

def _get_float_mc_from_cache(code):
    """从缓存获取流通市值（亿元），未命中返回None（委托 app_data）。"""
    return app_data.get_float_mc_from_cache(code)

# 扫描与冷启动共用同一个 _stocks_analysis_cache，由 LRU 50 条统一管理
# 扫描时：有买点才保留缓存，否则释放

# 股票分析锁（防止并发请求时 CTdxAPI.set_data 被覆盖导致分析结果串数据）
_stock_analysis_lock = threading.Lock()


def _cache_put(key, value):
    """写入缓存，超出上限时淘汰最旧的条目（LRU语义；委托 app_data）。
    内存由 LRU 50 条上限 + 扫描时逐只释放非买点缓存共同控制。
    """
    return app_data.cache_put(key, value)


def _cache_get(key):
    """读取缓存，命中时移到末尾（LRU语义；委托 app_data）"""
    return app_data.cache_get(key)


def _cache_remove(key):
    """从缓存中删除指定条目（不触发 GC，由调用方在适当时机统一回收；委托 app_data）"""
    return app_data.cache_remove(key)




# ============================================================
# 手选进入段选点保存/恢复（实现与路径均单源于 app_data / app_config；
# 此处仅留 schema 常量供本文件消费）
# ============================================================
# CSV列：股票代码,股票名,年K选点,季K选点,月K选点,周K选点,日K选点,30分选点,15分选点,5分选点,1分选点
SAVED_POINT_COLUMNS = AppData_SAVED_POINT_COLUMNS
# freq -> CSV列名 的映射
FREQ_TO_COL = AppData_FREQ_TO_COL


def _save_point_time(code, name, freq, sdt):
    """保存或更新某只股票某个周期的选点（委托 app_data）"""
    return app_data.save_point_time(code, name, freq, sdt)


# 选点内存缓存：别名 = app_data 实例字段（共享同一对象；启动加载由
# app_data 实例化完成，本文件多处直接读该字典保持零漂移）
_saved_point_times = app_data.saved_point_times


# ═══════════════════════════════════════════════════════════════════════
# 区域 3 · 股票分析
# ═══════════════════════════════════════════════════════════════════════

def _analyze_stock_internal(code, freq="d", end_date=None, start_time=None, cache_chan=True, dual=False, step=None, sub_freq=None):
    """
    使用通达信数据源 + chan.py 进行股票/指数缠论分析（内部实现，不处理期货分流）
    返回与 czsc 版本兼容的 JSON 数据结构
    end_date: 复盘截止日期，有值时以该日期为"最新行情"
    start_time: 选点起始时间，有值时只加载该时间之后的K线（不设数量限制）
    step: 箭头步进，在 full_records 中从 end_date 位置偏移 step 根K线作为新的截断日期
    cache_chan: 是否缓存CChan对象。扫描模式设为False以节省内存。
    sub_freq: 双窗口下窗周期（显式透传；缺省按 _SUB_FREQ_MAP 回退）。
    """
    import time
    t_start = time.time()

    market, code = _get_stock_market_code(code.strip().upper())
    if not market:
        return {"error": f"无法识别股票代码: {code}"}

    qualified_code = f"{code}.{market.upper()}"  # 区分沪市深市同号股票

    # 调试模式：冷启动注入截止日期（仅日K生效）
    if not end_date and DEBUG_COLD_START_END_DATE and freq == 'd':
        end_date = DEBUG_COLD_START_END_DATE
        log.info(f"[调试] 冷启动使用截止日期: {end_date}")

    # ===== 双窗口模式：独立缓存系统 =====
    # 双窗口与单窗口完全独立，各自拥有独立的 CChan 对象和缓存 key
    # 双窗口内部主级别和子级别也分开存储
    if dual:
        # 未显式传 sub_freq 时按缺省配对回退
        if not sub_freq:
            sub_freq = _SUB_FREQ_MAP.get(freq)
        pair_err = _validate_stock_dual_pair(freq, sub_freq)
        if pair_err:
            return {"error": pair_err}
        # 实现开关（CHAN_STOCK_DUAL_IMPL）：
        #   independent = 独立下窗路径（默认）
        #   legacy      = 多级别联立路径（快照基线/回滚通道）
        dual_impl = _stock_dual_impl()
        # 缓存 key 约定（结构化键，见 AppData.make_*_key）：
        #   dual_main  — 主级别缓存（含 CChan 对象）
        #   dual_sub   — 子级别缓存（独立存储）
        #   date_suffix = end_date（复盘）或 "live"（非复盘）
        date_suffix = end_date if end_date else "live"
        main_cache_key = make_dual_main_key(market, code, freq, date_suffix)
        sub_cache_key = make_dual_sub_key(market, code, sub_freq, date_suffix)
        cache_key = None  # 双窗口不使用单窗口的 cache_key，初始化为 None 防止意外引用

        # 查双窗口缓存（主级别和子级别必须同时存在，不会出现一个存在一个不存在）
        # 复盘模式(end_date)不命中缓存，强制重新加载
        main_cached = _cache_get(main_cache_key)
        sub_cached = _cache_get(sub_cache_key)
        if not end_date and main_cached is not None and sub_cached is not None \
                and "result" in main_cached and "result" in sub_cached:
            result = main_cached["result"]
            result["sub"] = sub_cached["result"]
            log.info(f"[耗时] 命中双窗口缓存(freq={freq}+{sub_freq})，总耗时: 0.001s")
            return result

        # 复盘模式：清除旧的双窗口缓存，强制重新加载主级别和子级别
        if end_date:
            with _cache_lock:
                if main_cache_key in _stocks_analysis_cache:
                    del _stocks_analysis_cache[main_cache_key]
                if sub_cache_key in _stocks_analysis_cache:
                    del _stocks_analysis_cache[sub_cache_key]
            gc.collect()
            log.info(f"[信息] 复盘模式：已清除双窗口缓存，重新加载主级别({freq})和子级别({sub_freq})")

        # 未命中缓存：冷启动从文件加载双级别数据，cached_result=None 强制走文件读取
        cached_result = None
    else:
        # ===== 单窗口模式 =====
        date_suffix = end_date if end_date else "live"
        cache_key = make_single_key(market, code, freq, date_suffix)
        cached_result = _cache_get(cache_key)
        if not end_date and cached_result is not None and "result" in cached_result:
            result = cached_result["result"]
            col = FREQ_TO_COL.get(freq, "")
            if col and qualified_code in _saved_point_times:
                saved_sdt = _saved_point_times[qualified_code].get(col, "").strip() or None
                if saved_sdt:
                    cached_saved = result.get("meta", {}).get("saved_selection_date", "")
                    if cached_saved != saved_sdt:
                        log.info(f"[信息] 缓存选点({cached_saved})与CSV({saved_sdt})不一致，跳过缓存")
                    else:
                        log.info(f"[耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                        return result
                else:
                    log.info(f"[耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                    return result
            else:
                log.info(f"[耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                return result

    # ===== 0. 提前解析 end_date（复盘模式需要先知道 target_dt，以便传给前复权） =====
    target_dt = None
    if end_date:
        for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
            try:
                target_dt = datetime.strptime(end_date, fmt)
                break
            except ValueError:
                continue
        if target_dt is None:
            return {"error": f"无法解析日期: {end_date}"}

    # ===== 1. 加载K线数据 =====
    forward_adjust_done = False
    sub_records = None  # 子级别数据，仅双窗口使用

    if dual:
        # ────────────────────────────────────
        # 双窗口分支：独立加载主级别和子级别数据
        # ────────────────────────────────────
        t0 = time.time()
        if freq == '30m' and sub_freq == '5m':
            # 优化：30m+5m 共用同一次5m文件读取和前复权，避免重复读取和二次复权
            full_records, sub_records, forward_adjust_done = CTdxAPI.fetch_main_level(market, code, freq, return_raw=True, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            log.info(f"[耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            log.info(f"[信息] 子级别({sub_freq})数据加载: {len(sub_records)}条 (复用前复权)")
        elif freq == 'w' and sub_freq == 'd':
            # 优化：w+d 共用同一次日线文件读取和前复权，避免重复读取和二次复权
            full_records, sub_records, forward_adjust_done = CTdxAPI.fetch_main_level(market, code, freq, return_raw=True, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            log.info(f"[耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            log.info(f"[信息] 子级别({sub_freq})数据加载: {len(sub_records)}条 (复用前复权)")
        else:
            full_records, forward_adjust_done = CTdxAPI.fetch_main_level(market, code, freq, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            log.info(f"[耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            sub_records = CTdxAPI.fetch_sub_level(market, code, freq, sub_freq, full_records, end_date=target_dt)
        if sub_records is None or len(sub_records) < 5:
            log.warning("[警告] 子级别数据不足，退化为单级别模式")
            sub_freq = None
    else:
        # ────────────────────────────────────
        # 单窗口分支：只加载主级别数据
        # ────────────────────────────────────
        if not end_date and cached_result is not None and "records" in cached_result:
            full_records = cached_result["records"]
            forward_adjust_done = cached_result.get("result", {}).get("meta", {}).get("forward_adjust", False)
            log.info(f"[耗时] 从缓存获取K线: {len(full_records)}条")
        else:
            t0 = time.time()
            full_records, forward_adjust_done = CTdxAPI.fetch_main_level(market, code, freq, end_date=target_dt)
            if len(full_records) < 5:
                log.info(f"[调试-K线不足] code={code}, market={market}, freq={freq}, target_dt={target_dt}, records={len(full_records)}")
                return {"error": f"K线数据不足: 仅{len(full_records)}条"}
            log.info(f"[耗时] 读取数据文件: {time.time()-t0:.3f}s, {len(full_records)}条K线")

    # 调试模式：数据加载后立即截断起始日期（所有周期生效），后续流程对此无感知
    # 等于在数据源层面"只加载了指定日期之后的数据"
    if DEBUG_COLD_START_START_DATE:
        try:
            start_cutoff = datetime.strptime(DEBUG_COLD_START_START_DATE, "%Y-%m-%d")
            before = len(full_records)
            filtered = [r for r in full_records if r["dt"] >= start_cutoff]
            if filtered:
                full_records = filtered
                log.info(f"[调试] 起始日期截断({DEBUG_COLD_START_START_DATE}): {before}条 -> {len(full_records)}条")
            else:
                log.info(f"[调试] 起始日期 {DEBUG_COLD_START_START_DATE} 之前无数据，保留全部{before}条")
        except ValueError:
            log.warning(f"[警告] DEBUG_COLD_START_START_DATE 格式错误: {DEBUG_COLD_START_START_DATE}，应为 YYYY-MM-DD")

    # 截断到指定日期（复盘模式：以end_date为"最新行情"）
    # 左边界与冷启动一致（同样按时间范围截取），只有右边界不同
    if end_date:
        # 确定日期格式（用于校正日内周期的时分秒），target_dt 已在前面解析
        matched_fmt = None
        for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
            try:
                datetime.strptime(end_date, fmt)
                matched_fmt = fmt
                break
            except ValueError:
                continue
        if matched_fmt == "%Y-%m-%d" and freq in INTRADAY_FREQS:
            target_dt = target_dt.replace(hour=23, minute=59, second=59)

        # === 箭头步进：在 full_records 中从 end_date 位置偏移 step 根K线 ===
        if step is not None:
            step = int(step)
            if step != 0:
                # 找到 end_date 对应的K线索引（精确匹配或 ≤ target_dt 的最后一根）
                anchor_idx = None
                for i in range(len(full_records) - 1, -1, -1):
                    if full_records[i]["dt"] <= target_dt:
                        anchor_idx = i
                        break
                if anchor_idx is not None:
                    new_idx = anchor_idx + step
                    if 0 <= new_idx < len(full_records):
                        target_dt = full_records[new_idx]["dt"]
                        end_date = target_dt.strftime("%Y-%m-%d %H:%M:%S")
                        log.info(f"[箭头] step={step}: {full_records[anchor_idx]['dt']} → {target_dt} (idx {anchor_idx} → {new_idx})")
                    else:
                        log.info(f"[箭头] step={step} 越界: idx {anchor_idx} → {new_idx}, 共{len(full_records)}条")

    if end_date:
        # 复盘模式：右边界 = target_dt，左边界与冷启动一致
        records = [r for r in full_records if r["dt"] <= target_dt]
        if len(records) < 5:
            return {"error": f"截断后K线数据不足: 仅{len(records)}条，请选择更晚的日期"}

        # 复盘（C 操作）不加载双击选点：不读取 CSV 保存的选点。
        # 左边界按 AppConfig 从「复盘结束时间」往前保留最近 N 根K线——日K/周K 不限量，
        # 30m/5m 按 STOCKS_LOOKBACK_CONFIG（仅当显式传入 start_time 才应用选点）。
        if start_time is not None:
            start_dt = None
            for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
                try:
                    start_dt = datetime.strptime(start_time, fmt)
                    break
                except ValueError:
                    continue
            if start_dt is not None and start_dt <= target_dt:
                before_count = len(records)
                records = [r for r in records if r["dt"] >= start_dt]
                log.info(f"[信息] 复盘选点: 从选点时间 {start_time} 开始，筛选后 {before_count}条 -> {len(records)}条")
        else:
            # 与冷启动一致，对30分/5分做根数截断，日K/周K不截断
            if not FULL_DATA_MODE and len(records) > 0 and freq in STOCKS_LOOKBACK_CONFIG:
                trunc_bars, trunc_text = STOCKS_LOOKBACK_CONFIG[freq]
                # bars<=0 表示不限制（如 w 显式配 (0,"不限制")），跳过截断
                if trunc_bars > 0 and len(records) > trunc_bars:
                    before_count = len(records)
                    records = records[-trunc_bars:]
                    log.info(f"[信息] 复盘截断(freq={freq}): 保留{trunc_text}, "
                          f"{before_count}条 -> {len(records)}条")

        if len(records) < 5:
            return {"error": f"截断后K线数据不足: 仅{len(records)}条，请选择更晚的日期"}
        log.info(f"[信息] 复盘范围(freq={freq}) {records[0]['dt'].strftime('%Y-%m-%d')} ~ {records[-1]['dt'].strftime('%Y-%m-%d')}, "
              f"全量{len(full_records)}条 -> {len(records)}条")
    else:
        records = full_records
        # 确定起始时间：优先使用传入的start_time，其次使用CSV保存的选点
        # 双窗例外（用户逻辑⑵）：双窗口不加载 CSV 保存的选点——CSV 里只有
        # 单窗口的选点，不与双窗混用（双窗 A 操作上窗按周期配置加载，
        # 下窗对齐上窗区间；双窗选点本身不保存，见 AppChart）。
        if start_time is None and not dual:
            col = FREQ_TO_COL.get(freq, "")
            if col and qualified_code in _saved_point_times:
                _saved = _saved_point_times[qualified_code].get(col, "").strip() or None
                if _saved:
                    start_time = _saved

        if start_time is not None:
            # 从选点时间开始过滤，不做数量限制
            start_dt = None
            for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
                try:
                    start_dt = datetime.strptime(start_time, fmt)
                    break
                except ValueError:
                    continue
            if start_dt is not None:
                records = [r for r in records if r["dt"] >= start_dt]
                log.info(f"[信息] 从选点时间 {start_time} 开始，筛选后 {len(records)} 条K线")
        else:
            # 冷启动无选点：默认模式下对30分和5分做根数截断（全量模式跳过）
            if not FULL_DATA_MODE and len(records) > 0 and freq in STOCKS_LOOKBACK_CONFIG:
                trunc_bars, trunc_text = STOCKS_LOOKBACK_CONFIG[freq]
                # bars<=0 表示不限制（如 w 显式配 (0,"不限制")），跳过截断
                if trunc_bars > 0 and len(records) > trunc_bars:
                    before_count = len(records)
                    records = records[-trunc_bars:]
                    log.info(f"[信息] 按K线根数截取(freq={freq}): 保留{trunc_text}, "
                          f"{before_count}条 -> {len(records)}条")

    # 双窗口：子级别数据同步截断到主级别时间范围
    # 避免 chan.py 分析不必要的全量子级别数据（如 30m+5m 时 5m 有 25152 条）
    # 对齐语义（用户逻辑⑵）：下窗后端加载的K线跟上窗对齐——上窗区间 =
    # 上窗后端实际加载的K线范围（A 操作按周期配置、B 操作选点→最新、
    # C 操作复盘结束时间往前推，主级别 records 已先按各操作规则截断）。
    # 下窗不读取 CSV 保存的选点卡界（单窗选点不与双窗混用，双窗选点不保存）。
    if dual and sub_freq and sub_records is not None and len(records) > 0:
        main_start = records[0]["dt"]
        main_end = records[-1]["dt"]
        sub_full_backup = list(sub_records)  # 对齐不足降全量的回退基准
        if dual_impl == "independent":
            # 结束时间语义精确截断：
            #   left_dt < sub_dt <= right_dt 恰为「完全落入上窗范围」的下窗K线
            left_dt, right_dt = _stocks_sub_dt_algo(main_start, main_end, freq, sub_freq)
            if left_dt is not None:
                sub_before = len(sub_records)
                sub_records = [r for r in sub_records if left_dt < r["dt"] <= right_dt]
                if sub_before != len(sub_records):
                    if sub_records:
                        log.info(f"[信息] 子级别({sub_freq})精确截断(P0): {sub_before}条 -> {len(sub_records)}条 "
                                 f"[{sub_records[0]['dt']} ~ {sub_records[-1]['dt']}]")
                    else:
                        log.info(f"[信息] 子级别({sub_freq})精确截断(P0): {sub_before}条 -> 0条")
            else:
                log.warning(f"[警告] 子级别({sub_freq})无法计算截断边界(主级别={freq})，保留全量")
        else:
            # legacy：±1 天 padding（联立路径基线，行为冻结）
            sub_before = len(sub_records)
            sub_records = [r for r in sub_records if main_start - timedelta(days=1) <= r["dt"] <= main_end + timedelta(days=1)]
            if sub_before != len(sub_records):
                log.info(f"[信息] 子级别({sub_freq})同步截断: {sub_before}条 -> {len(sub_records)}条")

        # 对齐不足降全量（用户逻辑⑵⓵）：按对齐区间截断后下窗K线过少
        # （下窗数据源覆盖不足，如上市较晚、分时文件只存近期）时，
        # 降为全量加载兜底——下窗笔结构要撑得起区间套/红框中枢分析。
        # 全量本身也不足时维持现状（后续 <5 检查退化为单级别提示）。
        if len(sub_records) < DUAL_SUB_FALLBACK_MIN and len(sub_full_backup) > len(sub_records):
            log.warning(f"[信息] 子级别({sub_freq})对齐区间[{main_start.strftime('%Y/%m/%d')} ~ "
                        f"{main_end.strftime('%Y/%m/%d')}]内仅{len(sub_records)}条"
                        f"(< {DUAL_SUB_FALLBACK_MIN})，降为全量{len(sub_full_backup)}条")
            sub_records = sub_full_backup

    # 2. 使用 chan.py 进行缠论分析
    # 复盘时：清空缓存恢复原始状态，再重新加载（与选点逻辑一致）
    if end_date and cache_key in _stocks_analysis_cache:
        with _cache_lock:
            if cache_key in _stocks_analysis_cache:
                del _stocks_analysis_cache[cache_key]
        gc.collect()

    t0 = time.time()
    with _stock_analysis_lock:
        chan_code = f"{market}.{code}"
        config = _make_chan_config()

        # 每次请求重置复盘标记，避免残留前一次状态
        from BuySellPoint.BSPointList import CMyBSPointList
        CMyBSPointList.REPLAY_MODE = False

        try:
            # CChan 创建：数据加载已在前面完成，此处只做数据注入和缠论分析
            if end_date:
                from BuySellPoint.BSPointList import CMyBSPointList
                CMyBSPointList.REPLAY_MODE = True
            _DUAL_LV_LIST = {
                'w': [KL_TYPE.K_WEEK, KL_TYPE.K_DAY],
                'd': [KL_TYPE.K_DAY, KL_TYPE.K_30M],
                '30m': [KL_TYPE.K_30M, KL_TYPE.K_5M],
            }
            sub_chan = None
            if dual and sub_freq and dual_impl == "independent":
                # ── 独立双窗：先下后上 ──────────────────────────
                # ① 先建下窗独立 CChan 并整读入运行时缓存——上窗 bsp 计算的
                #    区间套（check_nested_diver）从缓存读完整下窗笔结构，
                #    消除联立模式下「主K线先到、子级别笔未跟上」的时序退化
                #    （对齐期货 SSE 双窗时序约定）。
                CTdxAPI.set_data(sub_records)
                sub_config = _make_chan_config()
                sub_config.kl_data_check = False
                sub_chan = CChan(
                    code=chan_code,
                    begin_time=None,
                    end_time=None,
                    data_src="custom:TdxAPI.CTdxAPI",
                    lv_list=[_get_kl_type(sub_freq)],
                    config=sub_config,
                    autype=AUTYPE.NONE,
                    market_type="stock",
                )
                for _snapshot in sub_chan.step_load():
                    pass
                app_data.stocks_sub_cache_put(chan_code, sub_freq, sub_chan)

                # ② 再建上窗（单级别注入），携带显式下窗周期供区间套取数
                CTdxAPI.set_data(records)
                lv_list = [_get_kl_type(freq)]
                config.kl_data_check = False
                chan = CChan(
                    code=chan_code,
                    begin_time=None,
                    end_time=None,
                    data_src="custom:TdxAPI.CTdxAPI",
                    lv_list=lv_list,
                    config=config,
                    autype=AUTYPE.NONE,
                    market_type="stock",
                )
                # 显式下窗周期：优先于 BSPointList 固定映射
                chan._stocks_dual_sub_freq = sub_freq
                for _snapshot in chan.step_load():
                    pass
            elif dual and sub_freq:
                # legacy：多级别联立注入（基线路径，行为冻结）
                lv_list = _DUAL_LV_LIST[freq]
                CTdxAPI.set_data({
                    _get_kl_type(freq): records,
                    _get_kl_type(sub_freq): sub_records,
                })
                config.kl_data_check = False
                chan = CChan(
                    code=chan_code,
                    begin_time=None,
                    end_time=None,
                    data_src="custom:TdxAPI.CTdxAPI",
                    lv_list=lv_list,
                    config=config,
                    autype=AUTYPE.NONE,
                    market_type="stock",
                )
                for _snapshot in chan.step_load():
                    pass
            else:
                # 单窗口（或双窗口降级）：只注入主级别数据
                lv_list = [_get_kl_type(freq)]
                CTdxAPI.set_data(records)
                chan = CChan(
                    code=chan_code,
                    begin_time=None,
                    end_time=None,
                    data_src="custom:TdxAPI.CTdxAPI",
                    lv_list=lv_list,
                    config=config,
                    autype=AUTYPE.NONE,
                    market_type="stock",
                )
                for _snapshot in chan.step_load():
                    pass
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            records_info = ""
            if records:
                records_info = f" records={len(records)}条 [{records[0]['dt']} ~ {records[-1]['dt']}]"
            log.error(f"[错误] chan.py 分析失败: code={chan_code} freq={freq}{records_info} 耗时={time.time()-t0:.3f}s")
            log.error(f"[错误] 异常类型: {type(e).__name__}, 异常信息: {e}")
            log.error(f"[错误] 完整堆栈:\n{tb}")
            return {"error": f"chan.py 分析失败: {type(e).__name__}: {e}"}
        finally:
            if end_date:
                CMyBSPointList.REPLAY_MODE = False

    log.info(f"[耗时] chan.py 缠论分析: {time.time()-t0:.3f}s")

    # 4. 提取主级别结果
    # 独立双窗的灰框 sub_kl_times 由时间分桶合成（sub_records），
    # legacy 联立路径走 KLU.sub_kl_list 取数（sub_records=None 区分）
    result = _extract_main_level_data(chan, freq, records, market, code,
                                       dual=dual, sub_freq=sub_freq,
                                       qualified_code=qualified_code,
                                       end_date=end_date,
                                       forward_adjust_done=forward_adjust_done,
                                       sub_records=(sub_records if (dual and sub_freq and dual_impl == "independent") else None),
                                       start_time=start_time)

    # 双窗口模式：提取子级别数据
    # 独立双窗从下窗独立 CChan 提取；legacy 从联立 CChan 提取
    sub_result = None
    if dual and sub_freq:
        log.info(f"[调试] 双窗口模式: dual={dual}, sub_freq={sub_freq}, impl={dual_impl}, "
                 f"chan类型={type(chan).__name__}")
        try:
            sub_src_chan = sub_chan if (dual_impl == "independent" and sub_chan is not None) else chan
            sub_result = _extract_sub_level_data(sub_src_chan, sub_freq, code, market)
        except Exception as e:
            import traceback
            log.error(f"[错误] 提取子级别数据失败: {type(e).__name__}: {e}")
            log.error(f"[错误] 堆栈:\n{traceback.format_exc()}")

    if sub_result:
        result["sub"] = sub_result

    mode_str = f" [复盘到 {end_date}]" if end_date else ""
    log.info(f"[信息] 查询 {code}.{market.upper()} 完成{mode_str}: {result['meta']['kline_count']}条K线, {result['meta']['bi_count']}笔, {result['meta']['fx_count']}分型, {result['meta']['zs_count']}中枢, {result['meta']['seg_count']}线段, {result['meta']['bsp_count']}买卖点")
    log.info(f"[耗时] 总耗时: {time.time()-t_start:.3f}s")

    # 缓存策略（结构化键，见 AppData.make_*_key）：
    # - 单窗口：缓存到 single 键
    # - 双窗口：主级别缓存到 dual_main 键，子级别缓存到 dual_sub 键
    # - 冷启动/选点/复盘/扫描：统一缓存 records + result + chan，由 LRU 20 条 + 内存保护管理
    if dual and sub_freq and sub_records is not None:
        # 双窗口：主级别缓存（不含 sub 字段，sub 独立存储）
        main_result = {k: v for k, v in result.items() if k != "sub"}
        main_cached = _cache_get(main_cache_key)
        if main_cached is None:
            main_cached = {}
        if cache_chan:
            main_cached["records"] = full_records
            main_cached["chan"] = chan       # CChan 只存一份在主级别缓存
        main_cached["result"] = main_result
        _cache_put(main_cache_key, main_cached)

        # 双窗口：子级别缓存（独立存储，下次切回双窗口直接命中）
        sub_cached = _cache_get(sub_cache_key)
        if sub_cached is None:
            sub_cached = {}
        if cache_chan:
            sub_cached["records"] = sub_records
            if dual_impl == "independent" and sub_chan is not None:
                # 独立双窗下窗 CChan 一并落 dual_sub 缓存（供排查/离线整读）
                sub_cached["chan"] = sub_chan
        sub_cached["result"] = sub_result
        _cache_put(sub_cache_key, sub_cached)
    elif dual:
        # 双窗口降级为单级别（子级别数据不足）：不缓存，下次重试
        pass
    else:
        # 单窗口
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
        log.info(f"[耗时]   复盘后GC回收: {time.time()-t_gc:.3f}s")

    return result


# ============================================================
# ═══════════════════════════════════════════════════════════════════════
# 区域 4 · 结果提取
# ═══════════════════════════════════════════════════════════════════════

# 提取主级别和子级别数据
# ============================================================
def _extract_main_level_data(chan, freq, records, market, code, dual=False, sub_freq=None,
                              qualified_code="", end_date=None, forward_adjust_done=False,
                              sub_records=None, start_time=None):
    """
    从 CChan 中提取主级别的 K线、笔、分型、中枢、线段、买卖点数据。
    返回与 czsc 版本兼容的 JSON 数据结构（不含 sub 字段）。
    sub_records: 独立双窗传入下窗记录列表——灰框 sub_kl_times 按
    时间分桶合成（行为与联立取数一致）；None 时走联立
    KLU.sub_kl_list 取数（legacy/单窗口）。
    start_time: 显式选点时间（B 操作重建传入）。meta.saved_selection_date
    回显规则：显式选点直接回显（双窗选点不落 CSV，仅会话内回显供前端
    全量显示）；否则单窗回显 CSV 保存的选点，双窗不读 CSV（不混用）。
    """
    t0 = time.time()
    kl_list = chan[_get_kl_type(freq)]

    # 1. K线数据（含MACD）
    closes = [r["close"] for r in records]
    macd_list = calculate_macd(closes)
    date_fmt = _get_date_fmt(freq)
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

    def _parse_klu_dt(klu):
        """将 KLU 的时间转为 datetime 对象，用于范围比较。"""
        return datetime.fromtimestamp(klu.time.ts)

    def _format_klu_dt(klu, out_freq):
        """将 KLU 的时间格式化为目标频率对应的日期字符串。"""
        out_fmt = _get_date_fmt(out_freq)
        return klu.time.toFmtStr(out_fmt)

    def _get_sub_klus(main_klu, main_freq):
        """获取一根主级别K线真正覆盖的子级别K线序列。"""
        if not main_klu or not hasattr(main_klu, 'sub_kl_list') or not main_klu.sub_kl_list:
            return []
        main_dt = _parse_klu_dt(main_klu)
        if main_dt is None:
            return []

        if main_freq == 'w':
            start = main_dt - timedelta(days=main_dt.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=6, hours=23, minutes=59, seconds=59, microseconds=999999)
        elif main_freq == 'd':
            start = main_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            end = main_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif main_freq == '30m':
            end = main_dt
            start = main_dt - timedelta(minutes=30) + timedelta(microseconds=1)
        else:
            return list(main_klu.sub_kl_list)

        valid = []
        for sub_klu in main_klu.sub_kl_list:
            sub_dt = _parse_klu_dt(sub_klu)
            if sub_dt is not None and start <= sub_dt <= end:
                valid.append(sub_klu)
        return valid

    # 双窗口模式：为每根K线添加 sub_kl_times（灰框定位用）
    if dual and sub_freq:
        if sub_records is not None:
            # 独立双窗——时间分桶合成（上窗KLU无 sub_kl_list）
            binned = _build_sub_kl_times(records, sub_records, freq, sub_freq)
            for k, sub_times in zip(kline_data, binned):
                k["sub_kl_times"] = sub_times
        else:
            # legacy 联立：从 KLU.sub_kl_list 取数（行为冻结）
            date_to_klu = {}
            for klc in kl_list.lst:
                for klu in klc.lst:
                    key = klu.time.toFmtStr(date_fmt)
                    date_to_klu[key] = klu
            for k in kline_data:
                klu = date_to_klu.get(k["date"])
                if klu and klu.sub_kl_list:
                    sub_times = []
                    for sub_klu in _get_sub_klus(klu, freq):
                        sub_times.append(_format_klu_dt(sub_klu, sub_freq))
                    k["sub_kl_times"] = sub_times
                else:
                    k["sub_kl_times"] = []

    # 2. 笔数据
    bi_data = []
    _fx_empty_count = 0
    for bi in kl_list.bi_list:
        direction = "up" if bi.is_up() else "down"
        begin_val = bi.get_begin_val()
        end_val = bi.get_end_val()
        power = abs(end_val - begin_val)
        begin_klu = bi.get_begin_klu()
        end_klu = bi.get_end_klu()
        sdt = begin_klu.time.to_str() if begin_klu else ""
        edt = end_klu.time.to_str() if end_klu else ""
        try:
            sdt_dt = datetime.strptime(sdt, "%Y/%m/%d %H:%M")
            sdt_str = sdt_dt.strftime(date_fmt)
            sdt_ts = int(sdt_dt.timestamp()) * 1000
        except:
            sdt_str = sdt
            sdt_ts = 0
        try:
            edt_dt = datetime.strptime(edt, "%Y/%m/%d %H:%M")
            edt_str = edt_dt.strftime(date_fmt)
            edt_ts = int(edt_dt.timestamp()) * 1000
        except:
            edt_str = edt
            edt_ts = 0
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
        fx_a_raw_dt = ""
        fx_b_raw_dt = ""
        a_klu = None
        b_klu = None
        date_fmt = _get_date_fmt(freq)
        shoulder_times = _main_bi_range(bi, date_fmt)
        if shoulder_times:
            fx_a_raw_dt, fx_b_raw_dt, a_klu, b_klu = shoulder_times

        if not fx_a_raw_dt or not fx_b_raw_dt:
            _fx_empty_count += 1

        # 红框边界（双窗口）：独立双窗用数学换算（主KLU 无联立 sub_kl_list，
        # 以 sub_records 非空为独立实现标志）；legacy 联立路径从左右肩 KLU 的
        # sub_kl_list 取真实子级别边界
        if dual and sub_freq:
            if sub_records is not None:
                from BuySellPoint.BSPointList import _stocks_red_range_algo
                fx_a_sub_dt, fx_b_sub_dt = _stocks_red_range_algo(
                    fx_a_raw_dt, fx_b_raw_dt, freq, sub_freq)
            else:
                fx_a_sub_dt, fx_b_sub_dt = _stocks_red_range(a_klu, b_klu, sub_freq, bi)
        else:
            fx_a_sub_dt, fx_b_sub_dt = "", ""

        from BuySellPoint.BSPointList import CMyBSPointList
        fx_strength = CMyBSPointList._is_strong_fx(bi) if hasattr(bi, 'end_klc') and bi.end_klc else 0
        bi_data.append({
            "idx": bi.idx,
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
            "fx_a_sub_dt": fx_a_sub_dt,
            "fx_b_sub_dt": fx_b_sub_dt,
            "fx_strength": fx_strength,
        })

    # 3. 分型数据
    fx_data = []
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            mark = "G"
            price = klc.high
            klu = klc.get_high_peak_klu()
            fx_date = klu.time.toFmtStr(_get_date_fmt(freq))
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
            mark = "D"
            price = klc.low
            klu = klc.get_low_peak_klu()
            fx_date = klu.time.toFmtStr(_get_date_fmt(freq))
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

    # 4. 中枢数据
    zs_data = []
    for zs in kl_list.zs_list:
        zs_data.append({
            "sdt": zs.begin.time.toFmtStr(date_fmt),
            "edt": zs.end.time.toFmtStr(date_fmt),
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
            sdt_raw = begin_klu.time.to_str()  # type: ignore[union-attr]
            sdt_dt = datetime.strptime(sdt_raw, "%Y/%m/%d %H:%M")
            star_date = sdt_dt.strftime(date_fmt)
        except Exception:
            star_date = ""
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

    # 5. 买卖点数据
    bsp_data = []
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            klu = bsp.klu
            bsp_date = klu.time.toFmtStr(_get_date_fmt(freq))
            try:
                bsp_dt = datetime.strptime(bsp_date, date_fmt)
                bsp_ts = int(bsp_dt.timestamp()) * 1000
            except:
                bsp_ts = 0
            bsp_data.append({
                "date": bsp_date, "timestamp": bsp_ts,
                "type": bsp.type2str(),
                "is_buy": bsp.is_buy,
                "price": klu.close,
                "high": klu.high,
                "low": klu.low,
            })
    except Exception as e:
        log.info(f"[调试] 获取买卖点失败: {e}")

    # 6. 线段数据
    seg_data = []
    for seg in kl_list.seg_list:
        direction = "up" if seg.is_up() else "down"
        begin_klu = seg.get_begin_klu()
        end_klu = seg.get_end_klu()
        sdt = (begin_klu.time.toFmtStr(_get_date_fmt(freq))) if begin_klu else ""
        edt = (end_klu.time.toFmtStr(_get_date_fmt(freq))) if end_klu else ""
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

    log.info(f"[耗时] 分析结果转JSON(K线/分型/笔/线段/中枢/买卖点）: {time.time()-t0:.3f}s")

    # 获取当前周期的保存选点日期（meta.saved_selection_date 回显规则）：
    #   · 复盘（end_date）不回显（复盘不加载/不支持选点）；
    #   · 显式选点（B 操作重建，start_time 传入）直接回显——单窗选点已落
    #     CSV（内存同步），双窗选点不落 CSV、仅会话内回显供前端全量显示；
    #   · 其余：单窗 A 操作回显 CSV 保存的选点（重启加载选点）；双窗
    #     不读 CSV（单窗选点不与双窗混用，用户逻辑⑵注）。
    _col_meta = FREQ_TO_COL.get(freq, "")
    _saved_sdt_for_meta = ""
    if not end_date:
        if start_time:
            _saved_sdt_for_meta = start_time
        elif not dual and _col_meta and qualified_code in _saved_point_times:
            _saved_sdt_for_meta = _saved_point_times[qualified_code].get(_col_meta, "").strip() or ""

    # 7. 计算最新笔的白色横虚线数据
    white_hline = None
    if bi_data:
        latest_bi = bi_data[-1]
        direction = latest_bi.get("direction", "")
        end_fx_idx = latest_bi.get("end_fx_idx")
        if end_fx_idx is not None and end_fx_idx > 0:
            left_klc = kl_list.lst[end_fx_idx - 1]  # type: ignore[union-attr]
            klc_high = left_klc.high  # type: ignore[union-attr]
            klc_low = left_klc.low  # type: ignore[union-attr]
            tgt_klu = None
            if hasattr(left_klc, 'lst') and left_klc.lst:  # type: ignore[union-attr]
                for _klu in left_klc.lst:  # type: ignore[union-attr]
                    if direction == "down" and _klu.high == klc_high:
                        tgt_klu = _klu
                        break
                    elif direction == "up" and _klu.low == klc_low:
                        tgt_klu = _klu
                        break
                if tgt_klu is None:
                    tgt_klu = left_klc.lst[0]  # type: ignore[union-attr]
            if tgt_klu:
                ls_date = tgt_klu.time.toFmtStr(date_fmt)
            else:
                ls_date = ""
            if direction == "down":
                white_hline = {"price": round(klc_high, 2), "start_date": ls_date}
            elif direction == "up":
                white_hline = {"price": round(klc_low, 2), "start_date": ls_date}

    # 8. 组装结果
    date_range = f"{kline_data[0]['date']} ~ {kline_data[-1]['date']}" if kline_data else ""

    log.info(f"[耗时] 主级别提取结果: {time.time()-t0:.3f}s (K线={len(kline_data)} 笔={len(bi_data)} 中枢={len(zs_data)} 线段={len(seg_data)} 买卖点={len(bsp_data)})")

    stock_name = _get_stock_name(market, code)
    if not stock_name:
        stock_name = f"{code}.{market.upper()}"

    result = {
        "meta": {
            "symbol": f"{code}.{market.upper()}",
            "market": market,
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
            "forward_adjust": forward_adjust_done,
            "pe_ttm": _get_pe_ttm(market, code),
            "index_belong": _get_index_belong(market, code),
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
    return result


def _extract_sub_level_data(chan, sub_freq, code, market):
    """
    从多级别 CChan 中提取子级别的 K线、笔、分型、中枢、线段、买卖点数据。
    用于双窗口模式，前端无需再发独立的 API 请求加载下面窗口数据。

    返回格式与主级别 result 一致，前端可直接用作 dualSubData。
    """
    t_start = time.time()
    sub_kl_type = _get_kl_type(sub_freq)
    sub_kl_list = chan[sub_kl_type]

    date_fmt = _get_date_fmt(sub_freq)

    # 1. 提取子级别原始K线数据（从 KLU 对象中提取，用于 MACD 计算）
    raw_records = []
    for klc in sub_kl_list.lst:  # type: ignore[union-attr]
        for klu in klc.lst:  # type: ignore[union-attr]
            try:
                dt = datetime.strptime(klu.time.to_str(), "%Y/%m/%d %H:%M")
            except:
                dt = datetime.strptime(klu.time.to_str() + " 00:00", "%Y/%m/%d %H:%M")
            raw_records.append({
                "dt": dt,
                "open": klu.open,
                "high": klu.high,
                "low": klu.low,
                "close": klu.close,
                "vol": int(klu.trade_info.metric.get("volume", 0) or 0),
                "amount": round(klu.trade_info.metric.get("turnover", 0) or 0, 2),
            })

    # 2. 计算 MACD
    closes = [r["close"] for r in raw_records]
    macd_list = calculate_macd(closes)

    # 3. 构建 kline_data
    kline_data = []
    for i, row in enumerate(raw_records):
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

    # 4. 构建 bi_data
    bi_data = []
    for bi in sub_kl_list.bi_list:
        direction = "up" if bi.is_up() else "down"
        begin_val = bi.get_begin_val()
        end_val = bi.get_end_val()
        power = abs(end_val - begin_val)
        begin_klu = bi.get_begin_klu()
        end_klu = bi.get_end_klu()
        sdt = begin_klu.time.to_str() if begin_klu else ""
        edt = end_klu.time.to_str() if end_klu else ""
        try:
            sdt_dt = datetime.strptime(sdt, "%Y/%m/%d %H:%M")
            sdt_str = sdt_dt.strftime(date_fmt)
            sdt_ts = int(sdt_dt.timestamp()) * 1000
        except:
            sdt_str = sdt
            sdt_ts = 0
        try:
            edt_dt = datetime.strptime(edt, "%Y/%m/%d %H:%M")
            edt_str = edt_dt.strftime(date_fmt)
            edt_ts = int(edt_dt.timestamp()) * 1000
        except:
            edt_str = edt
            edt_ts = 0

        # 子级别的 fx_a_raw_dt/fx_b_raw_dt（分型肩部时间，用于红框定位）
        # 和主级别共用同一个函数，逻辑完全一致
        begin_fx_idx = getattr(bi, 'begin_fx_idx', -1)
        end_fx_idx = getattr(bi, 'end_fx_idx', -1)
        fx_a_raw_dt = ""
        fx_b_raw_dt = ""
        date_fmt_sub = _get_date_fmt(sub_freq)
        shoulder_times = _main_bi_range(bi, date_fmt_sub)
        if shoulder_times:
            fx_a_raw_dt, fx_b_raw_dt, _, _ = shoulder_times

        bi_data.append({
            "idx": bi.idx,
            "sdt": sdt_str, "edt": edt_str,
            "sdt_ts": sdt_ts, "edt_ts": edt_ts,
            "direction": direction,
            "fx_a_price": round(begin_val, 2),
            "fx_b_price": round(end_val, 2),
            "high": round(bi._high(), 2),
            "low": round(bi._low(), 2),
            "power": round(power, 2),
            "is_sure": getattr(bi, 'is_sure', True),
            "end_fx_idx": end_fx_idx if end_fx_idx is not None else -1,
            "begin_fx_idx": begin_fx_idx if begin_fx_idx is not None else -1,
            "fx_a_raw_dt": fx_a_raw_dt,
            "fx_b_raw_dt": fx_b_raw_dt,
            "fx_a_sub_dt": "",
            "fx_b_sub_dt": "",
        })

    # 5. 构建 fx_data
    fx_data = []
    for klc in sub_kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            mark = "G"
            price = klc.high
            klu = klc.get_high_peak_klu()
            fx_date = klu.time.toFmtStr(_get_date_fmt(sub_freq))
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
            mark = "D"
            price = klc.low
            klu = klc.get_low_peak_klu()
            fx_date = klu.time.toFmtStr(_get_date_fmt(sub_freq))
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

    # 6. 构建 zs_data
    zs_data = []
    for zs in sub_kl_list.zs_list:
        zs_data.append({
            "sdt": zs.begin.time.toFmtStr(date_fmt),
            "edt": zs.end.time.toFmtStr(date_fmt),
            "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, sub_kl_list.bi_list, date_fmt),
            "zg": round(zs.high, 2),
            "zd": round(zs.low, 2),
            "gg": round(zs.peak_high, 2),
            "dd": round(zs.peak_low, 2),
            "dir": "up" if zs.bi_in and zs.bi_in.is_up() else "down",
        })

    # 7. 构建 zs_stars
    zs_stars = []
    for zs in sub_kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        try:
            sdt_raw = begin_klu.time.to_str()  # type: ignore[union-attr]
            sdt_dt = datetime.strptime(sdt_raw, "%Y/%m/%d %H:%M")
            star_date = sdt_dt.strftime(date_fmt)
        except Exception:
            star_date = ""
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({
                "date": star_date, "price": round(star_price, 2),
                "mark": "D", "color": "red",
            })
        else:
            zs_stars.append({
                "date": star_date, "price": round(star_price, 2),
                "mark": "G", "color": "green",
            })

    # 8. 构建 bsp_data（从子级别 bs_point_lst 中提取）
    bsp_data = []
    try:
        for bsp in sub_kl_list.bs_point_lst.bsp_iter():
            klu = bsp.klu
            bsp_date = klu.time.toFmtStr(_get_date_fmt(sub_freq))
            try:
                bsp_dt = datetime.strptime(bsp_date, date_fmt)
                bsp_ts = int(bsp_dt.timestamp()) * 1000
            except:
                bsp_ts = 0
            bsp_data.append({
                "date": bsp_date, "timestamp": bsp_ts,
                "type": bsp.type2str(),
                "is_buy": bsp.is_buy,
                "price": klu.close,
                "high": klu.high,
                "low": klu.low,
            })
    except Exception as e:
        log.info(f"[调试] 子级别获取买卖点失败: {e}")

    # 9. 构建 seg_data
    seg_data = []
    for seg in sub_kl_list.seg_list:
        direction = "up" if seg.is_up() else "down"
        begin_klu = seg.get_begin_klu()
        end_klu = seg.get_end_klu()
        sdt = (begin_klu.time.toFmtStr(_get_date_fmt(sub_freq))) if begin_klu else ""
        edt = (end_klu.time.toFmtStr(_get_date_fmt(sub_freq))) if end_klu else ""
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

    # 10. 组装结果
    sub_result = {
        "meta": {
            "symbol": f"{code}.{market.upper()}",
            "market": market,
            "name": "",
            "freq": _get_freq_label(sub_freq),
            "chan_version": "chan.py",
            "kline_count": len(kline_data),
            "bi_count": len(bi_data),
            "fx_count": len(fx_data),
            "zs_count": len(zs_data),
            "seg_count": len(seg_data),
            "bsp_count": len(bsp_data),
            "date_range": f"{kline_data[0]['date']} ~ {kline_data[-1]['date']}" if kline_data else "",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "klines": kline_data,
        "bis": bi_data,
        "fxs": fx_data,
        "zs": zs_data,
        "zs_stars": zs_stars,
        "segs": seg_data,
        "bsps": bsp_data,
    }

    log.info(f"[耗时] 子级别({sub_freq})提取: {time.time()-t_start:.3f}s (K线={len(kline_data)} 笔={len(bi_data)} 分型={len(fx_data)} 中枢={len(zs_data)} 线段={len(seg_data)} 买卖点={len(bsp_data)})")
    return sub_result


# ============================================================
# ═══════════════════════════════════════════════════════════════════════
# 区域 5 · 公开入口
# ═══════════════════════════════════════════════════════════════════════

# 区间套背驰判断
# ============================================================
# 高级别→低级别周期映射（缺省配对；独立双窗显式配对共 6 对，
# 未显式传 sub_freq 时按此缺省回退，保证不传参调用行为不变）
_SUB_FREQ_MAP = {'w': 'd', 'd': '30m', '30m': '5m'}



# ============================================================
# ═══════════════════════════════════════════════════════════════════════
# 股票双窗口独立实现
# ═══════════════════════════════════════════════════════════════════════
# 设计要点：
#   · 下窗独立拉取独立建 CChan（先下后上），区间套/红框读运行时缓存；
#   · 下窗截断按「结束时间语义」精确截断；
#   · 配对空间 6 对，sub_freq 全链路显式透传；
#   · 灰框 sub_kl_times 后端时间分桶合成；
#   · 红框中枢读独立下窗，缓存 miss 抛错；
#   · 实现开关 CHAN_STOCK_DUAL_IMPL=independent|legacy。
# ============================================================

# A/B 实现开关（读取时机=每次分析，便于运行期切换与测试打桩）
_STOCKS_DUAL_IMPL_ENV = "CHAN_STOCK_DUAL_IMPL"


def _stock_dual_impl():
    """股票双窗实现选择（A/B 开关）。

    返回 "independent"（默认，独立下窗路径）或 "legacy"
    （多级别联立路径，快照基线与回滚通道）。
    非法取值一律回退 independent。
    """
    v = os.environ.get(_STOCKS_DUAL_IMPL_ENV, "independent").strip().lower()
    return v if v in ("independent", "legacy") else "independent"


# 股票周期种类（w/d/30m/5m），双窗配对空间 6 对
_STOCKS_DUAL_PAIRS = {
    "w": {"d", "30m", "5m"},
    "d": {"30m", "5m"},
    "30m": {"5m"},
    # 5m 为股票最小周期，无下窗可选（与期货 15s 同语义）
}


def _validate_stock_dual_pair(freq, sub_freq):
    """校验股票双窗配对（显式守卫，不做静默降级）。

    合法返回 None；否则返回错误信息（调用方以 {"error": ...} 4xx 拒绝）。
    """
    subs = _STOCKS_DUAL_PAIRS.get(freq)
    if subs is None:
        return f"双窗口不支持当前上窗周期: {freq}（可选 w/d/30m）"
    if not sub_freq:
        return f"双窗口缺少下窗周期 sub_freq（{freq} 可选 {sorted(subs)}）"
    if sub_freq not in subs:
        return f"双窗口周期配对无效: {freq}+{sub_freq}（上窗须严格大于下窗；{freq} 可选 {sorted(subs)}）"
    return None


# 主级别单根 K 线的覆盖时长（结束时间语义：K 线 dt=结束时刻，
# 覆盖区间 (dt-period, dt]）
_STOCKS_MAIN_PERIOD = {
    "w": timedelta(days=7),
    "d": timedelta(days=1),
    "30m": timedelta(minutes=30),
}

# 日期型 K 线（w/d）dt 为当日 00:00，语义补齐到「当日结束时刻」
_STOCKS_EOD = timedelta(hours=23, minutes=59, seconds=59, microseconds=999999)


def _stocks_sub_dt_algo(main_first_dt, main_last_dt, main_freq, sub_freq=None):
    """股票双窗独立下窗截断边界 —— 结束时间语义纯函数。

    股票 K 线 dt = 结束时间（期货=开始时间，两者相反）。日期型 K 线
    （w/d）dt 为当日 00:00，语义上按「当日结束时刻」（收盘）处理，
    与前端 getMainKlineTimeRange 的日期型解析口径（当日 23:59:59）一致。

    返回 (left_dt, right_dt)，满足  left_dt < sub_dt <= right_dt  的下窗
    K 线恰为「完全落入上窗时间范围」的下窗 K 线：
      right_dt = 上窗末根的结束时刻（日期型=当日收盘）
      left_dt  = 上窗首根的开始时刻 = 首根结束时刻 - 主级别周期
    主级别周期无法识别时返回 (None, None)，调用方退化为不过滤。
    sub_freq 仅为签名完整性保留（边界与下窗周期无关：下窗右端=上窗右端，
    左端=上窗左端，恰好结束于边界的下窗K线按结束时刻归属）。
    """
    period = _STOCKS_MAIN_PERIOD.get(main_freq)
    if period is None or main_first_dt is None or main_last_dt is None:
        return None, None
    first, last = main_first_dt, main_last_dt
    if main_freq in ("w", "d"):
        # 日期型：dt(00:00) → 当日结束时刻，保证日内下窗K线正确落入
        first = first + _STOCKS_EOD
        last = last + _STOCKS_EOD
    return first - period, last


def _build_sub_kl_times(main_records, sub_records, main_freq, sub_freq):
    """灰框对照表合成：每根上窗 K 线 → 其覆盖的下窗 K 线时间列表。

    独立实现下上窗 KLU 不携带 sub_kl_list（联立数据），按时间分桶合成：
    每根上窗 K 线的覆盖区间与 _stocks_sub_dt_algo 同口径。输出与联立
    取数（_get_sub_klus 过滤后逐根格式化）一致，灰框行为不变。

    双指针 O(n+m)：主级别桶按时间递增且互不重叠，下窗记录升序消费。
    """
    sub_fmt = _get_date_fmt(sub_freq)
    times = [[] for _ in main_records]
    if not sub_records:
        return times
    i = j = 0
    n, m = len(main_records), len(sub_records)
    while i < n and j < m:
        left, right = _stocks_sub_dt_algo(main_records[i]["dt"], main_records[i]["dt"],
                                          main_freq, sub_freq)
        if left is None:
            break
        dt = sub_records[j]["dt"]
        if dt <= left:
            j += 1          # 早于当前桶（只可能属于已越过的桶）→ 丢弃
        elif dt <= right:
            times[i].append(dt.strftime(sub_fmt))
            j += 1
        else:
            i += 1          # 晚于当前桶 → 归属后续主级别K线
    return times


def analyze_stock(code, freq="d", end_date=None, cache_chan=True, dual=False, step=None, sub_freq=None):
    """公开分析入口：仅处理股票/指数（通达信数据源），支持 cache_chan 和 dual 双窗口。

    期货的一切拉流（实时/选点/复盘软断开）统一走 AppSSE 的 SSE 通道
    （sse_futures_stream_*），均不经本函数。此处对期货代码明确拒绝，
    防止误传落到股票路径产生静默错误。
    sub_freq: 双窗口下窗周期（全链路显式透传：FrontAPI → 本入口 →
    _analyze_stock_internal；未传时按 _SUB_FREQ_MAP 缺省配对回退）。
    """
    market, normalized_code = _get_market_code(code)
    if not market:
        return {"error": f"无法识别股票代码: {code}"}
    if market == 'futures':
        return {"error": f"期货代码 {code} 请走期货分析接口（/api/futures/read/stream 或复盘/选点），不支持下挂股票路由"}
    stock_code = f"{normalized_code}.{market.upper()}"
    return _analyze_stock_internal(stock_code, freq=freq, end_date=end_date, cache_chan=cache_chan,
                                   dual=dual, step=step, sub_freq=sub_freq)



# -*- coding: utf-8 -*-
"""
App/AppEngine.py —— 分析引擎层（自 my_chan_main.py 迁入）
=========================================================================
设计文档 10.1：迁移完成后，my_chan_main.py 职责被各层完全吸收。
本文件承载分析引擎核心（分析函数 + 工具函数 + 模块级常量/状态），
由 App/AppOrch.py（业务编排层）和 FrontAPI.py（API 入口层）引用。

依赖方向：AppEngine → App/AppConfig.py / App/AppData.py / DataAPI/（单向）
          App/AppOrch.py → AppEngine（单向）
          FrontAPI.py → AppEngine（仅常量/纯函数，引擎入口一律走 orch.* 漏斗）

本文件不包含任何 HTTP 服务器/路由/墓碑代码（ChartHandler、main() 等
已于 3b-2 后统一拆除）。

使用方式：
    from App import AppEngine as _m      # 服务层获取引擎
    from App import AppEngine as m       # API 层读取常量/纯函数
"""
import sys, os, json, time, re, struct, threading, multiprocessing, gc
from datetime import datetime, timedelta
from chinese_calendar import is_holiday

# 区间套辅助函数（已搬迁至 BSPointList.py，红框功能复用）
from BuySellPoint.BSPointList import _main_bi_range, _stocks_red_range, _futures_red_range, _red_range_bi_sequence, _red_range_amp

# ============================================================
# 配置区域 —— 已中心化（阶段 2，V10 方案 7.1/7.2）
# 基础设施配置：App/AppConfig.py（环境变量 / 仓库根 .env 优先）
# 算法参数 + 默认代码：ChanConfig.py（SYMBOL_CODE 环境变量可覆盖）
# 阶段 4：7 个路径别名已删除，全部引用改为 app_config.<属性> 直读
# ============================================================
from App.AppConfig import app_config

# 业务数据层（阶段 4：缓存/持久化/标注/自选股收敛至此，
# 本文件同名函数降级为兼容壳，状态名为共享同一对象的别名）
from App.AppData import app_data
from App.AppData import SAVED_POINT_COLUMNS as AppData_SAVED_POINT_COLUMNS
from App.AppData import FREQ_TO_COL as AppData_FREQ_TO_COL

from ChanConfig import get_symbol_code
SYMBOL_CODE = get_symbol_code()                                        # 默认股票代码（SYMBOL_CODE，上证指数）

# ============================================================
# 天勤期货/期指行情配置
# ============================================================
# 账户和密码从 vipdoc（即 tdx_install_dir\vipdoc）下 tq_account.json 文件读取
# 文件格式: {"account": "手机号或用户名", "password": "密码"}
_SSE_DEBUG  = False # SSE 推送详细调试日志开关（设为 True 可恢复调试输出）
_SSE_DIAG   = True  # 双窗口进度条诊断日志开关：追踪每窗处理耗时 + 快照lastK滞后，验证"00:00+灰色"根因

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
    from ChanConfig import CChanConfig
    from Common.CEnum import AUTYPE, KL_TYPE, FX_TYPE
except ImportError as e:
    print(f"\n[错误] chan.py 导入失败: {e}")
    print(f"[提示] 请确保 CHAN_PATH = r'{app_config.chan_path}' 指向正确的 chan.py 仓库目录")
    sys.exit(1)

# 导入通达信数据源适配器（从 chan.py 的 DataAPI 目录）
# 包含：K线读取、前复权（自选股读写已收敛 App/AppData.py，阶段 4）
from DataAPI.TdxAPI import CTdxAPI, \
    read_main_level_records, read_sub_level_records, \
    get_index_stocks, refresh_block_files

# 前复权开关：True=开启前复权（消除分红送股的跳空缺口），False=关闭（不复权，原样输出）
FORWARD_ADJUST_ENABLED = True

# 调试模式：冷启动只从指定日期开始加载(所有周期有效)，None表示不开启。如果该日期前无通达信数据，则有多少加载多少
DEBUG_COLD_START_START_DATE = None # "2024-09-10"

# 调试模式：用于解决冷启动起不来的问题；冷启动加载到此日期(仅日K生效)，None表示不开启
DEBUG_COLD_START_END_DATE = None   # 示例: "2026-06-29" 北方国际

# 注入通达信数据源配置到 TdxAPI 模块
from DataAPI.TdxAPI import set_tdx_config as _set_tdx_config
_set_tdx_config(
    vipdoc_dir=app_config.vipdoc_dir,
    forward_adjust_enabled=FORWARD_ADJUST_ENABLED,
)

# 阶段 5：行业映射单源注入（设计 8.8/4.1）
# tdxhy_mapping_data.py 已整体迁入 App/（独立数据文件），两处硬编码寻址
# 收敛为 AppData.load_tdxhy_mapping 单一加载函数；DataAPI 与 App 互不依赖
# （设计 4.4），TdxAPI 侧经 set_tdx_hy_mapping 注入（与 set_tdx_config
# 同一注入模式）。加载失败硬失败（ValueError），不静默降级空表。
from DataAPI.TdxAPI import set_tdx_hy_mapping as _set_tdx_hy_mapping
_set_tdx_hy_mapping(*app_data.load_tdxhy_mapping())

# 全量数据模式：True=加载全部K线不做时间截断；False=默认模式
FULL_DATA_MODE = False

# 时间截断配置（仅 FULL_DATA_MODE=False 时生效）：{周期: (天数, 显示文本)}
TIME_TRUNCATE_CONFIG = {
    '30m': (180, "6个月"),
    '5m': (90, "3个月"),
}

# 导入天勤数据源适配器（期货/期指）
# 阶段 5（设计 8.8）：非标准导入收敛 —— 频率映射/别名/支持列表/fetch_kline
# 一律经 CTqSdkAPI 元数据接口访问（CommonStockAPI 抽象层），不再直接 import。
try:
    from DataAPI.TqSdkAPI import CTqSdkAPI, _get_futures_code, _get_futures_name, load_tq_account
    load_tq_account(app_config.vipdoc_dir)
    TQ_AVAILABLE = True
except ImportError as e:
    CTqSdkAPI = None
    _get_futures_code = None
    _get_futures_name = None
    load_tq_account = None
    TQ_AVAILABLE = False
    print(f"[警告] 天勤数据源未安装: {e}，期货功能不可用。pip install tqsdk")


# ============================================================
# 同花顺自选股同步（云端 API）
# ============================================================
try:
    from App.ths_cloud_api import save_scan_to_ths_cloud   # 阶段 2：同花顺工具链迁入 App/
    _THS_CLOUD_AVAILABLE = True
except ImportError:
    save_scan_to_ths_cloud = None
    _THS_CLOUD_AVAILABLE = False


# ============================================================
# 通达信数据读取
# ============================================================

# ============================================================
# 盘后数据下载引擎（基于 eltdx）—— 阶段 5 收敛至 DataAPI/ElTdxAPI.py
# 职责内聚：下载函数族 + 状态单一事实源迁入 ElTdxAPI，本模块保留兼容壳。
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


def _start_download(vipdoc_dir, categories, day_start=None, min_start=None):
    """兼容壳（阶段 5）：委托 DataAPI/ElTdxAPI"""
    return _ElTdx._start_download(vipdoc_dir, categories, day_start=day_start, min_start=min_start)


def _stop_download():
    """兼容壳（阶段 5）：委托 DataAPI/ElTdxAPI"""
    return _ElTdx._stop_download()


def _get_download_status():
    """兼容壳（阶段 5）：委托 DataAPI/ElTdxAPI"""
    return _ElTdx._get_download_status()# ============================================================
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


def _inherit_macd_for_preview_bar(klines_list):
    """让预览K线（列表最后一根）继承前一根已确认K线的MACD值，避免跳变。
    预览K线的close是假数据（壁钟触发时用冻结K线的close填充），
    重算全序列MACD反而引入误差，不如直接继承前一根的值，
    等后续真实tick到来时再由tick路径用真实数据重算覆盖。"""
    if len(klines_list) < 2:
        return
    prev = klines_list[-2]
    klines_list[-1]['dif'] = prev.get('dif', 0)
    klines_list[-1]['dea'] = prev.get('dea', 0)
    klines_list[-1]['macd'] = prev.get('macd', 0)


# ============================================================
# 获取股票名称（阶段 4：实现已收敛 App/AppData.py，此处为兼容壳）
# ============================================================
def _get_stock_name(market, code):
    """获取股票名称（委托 app_data.get_stock_name）"""
    return app_data.get_stock_name(market, code)


# 股票名称/PE/市值缓存状态：别名 = app_data 实例字段（共享同一对象，阶段 4）
# key: 股票代码(6位), value: {"name": "股票名称", "pinyin": "拼音首字母"}
# （惰性标志 _names_loaded/_pe_loaded/_belong_loaded 已随实现收敛 app_data，不再留别名）
_stock_names_cache = app_data.names_cache

# 刷新状态（股票名称刷新用；获取侧状态，阶段 5 前保留于此）
_refresh_status = {"running": False, "progress": 0, "total": 0, "loaded": 0, "error": None, "step": ""}



def _load_stock_names_from_cache_file():
    """从 stock_names.json 加载股票名称到内存（委托 app_data）"""
    return app_data.load_stock_names_from_cache_file()



# ============================================================
# 原子 JSON 写（持久化底座；实现收敛 App/AppData.safe_write_json_file）
# ============================================================
def _safe_write_json_file(path, data, *, ensure_ascii=False, indent=None):
    """先写临时文件并校验 JSON 可读，再原子覆盖（委托 app_data）"""
    from App.AppData import safe_write_json_file
    return safe_write_json_file(path, data, ensure_ascii=ensure_ascii, indent=indent)


# ============================================================
# PE-TTM 缓存（腾讯接口，增量刷新；实现收敛 App/AppData.py）
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


# AKShare 指数代码 → 市场前缀映射
_AKSHARE_EXCHANGE_MAP = {
    "上海证券交易所": "sh",
    "深圳证券交易所": "sz",
}
# AKShare 指数代码 → 归属名称
_AKSHARE_INDEX_MAP = {
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "000688": "科创50",
}


def _fetch_index_belong_from_akshare(timeout=30):
    """
    通过 AKShare index_stock_cons_csindex 接口在线获取沪深300/中证500/中证1000 最新成分股，
    构建 stock→指数归属 反向映射。返回 {market+code: "沪深300"|"中证500"|"中证1000"}。
    如果 AKShare 不可用或网络异常，返回空字典。每个指数单独设置超时。
    （阶段 4：归属缓存由 app_data 持有，经 replace_index_belong 同对象替换）
    """
    try:
        import akshare as ak
    except ImportError:
        print("[指数归属] akshare 未安装，跳过在线获取（pip install akshare）")
        return app_data.belong_cache

    def _fetch_one(_idx_code, _idx_name):
        try:
            _refresh_status["step"] = f"刷新指数归属: {_idx_name}..."
            print(f"[指数归属] 开始获取 {_idx_name}({_idx_code})...")
            df = ak.index_stock_cons_csindex(symbol=_idx_code)
            count = 0
            for _, row in df.iterrows():
                stock_code = str(row["成分券代码"]).zfill(6)
                exchange = str(row["交易所"])
                mkt = _AKSHARE_EXCHANGE_MAP.get(exchange, "")
                if mkt and stock_code.isdigit() and len(stock_code) == 6:
                    result[mkt + stock_code] = _idx_name
                    count += 1
            print(f"[指数归属] {_idx_name}({_idx_code}): 已成功获取 {count}只 成分股")
        except Exception as e:
            print(f"[指数归属] {_idx_name}({_idx_code}) 获取失败: {e}")

    import concurrent.futures
    result = {}
    for index_code, index_name in _AKSHARE_INDEX_MAP.items():
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_fetch_one, index_code, index_name)
            try:
                future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                print(f"[指数归属] {index_name}({index_code}) 获取超时({timeout}s)，跳过")
        finally:
            executor.shutdown(wait=False)  # 不等待卡住的线程，直接进入下一个指数

    app_data.replace_index_belong(result)
    return result


def _refresh_pe_ttm():
    """
    通过腾讯行情接口批量获取 PE-TTM，增量更新 stock_pettm_index.json。
    从 stock_names.json 中读取所有股票代码，分批请求腾讯接口。
    """
    import requests as req
    _refresh_status["step"] = "刷新PE-TTM..."
    _load_pe_ttm_cache()  # 先加载已有缓存

    # 从 stock_names.json 收集所有纯数字股票代码（路径：AppConfig 派生属性）
    if not os.path.exists(app_config.stock_names_cache_file):
        print("[PE-TTM] stock_names.json 不存在，无法刷新")
        _refresh_status["error"] = "stock_names.json 不存在，请先刷新股票名称"
        return

    try:
        with open(app_config.stock_names_cache_file, "r", encoding="utf-8") as f:
            names_data = json.load(f)
    except Exception as e:
        print(f"[PE-TTM] 读取 stock_names.json 失败: {e}")
        _refresh_status["error"] = f"读取 stock_names.json 失败: {e}"
        return

    if not isinstance(names_data, dict):
        _refresh_status["error"] = "stock_names.json 格式错误"
        return

    # 收集股票代码并构建腾讯代码列表
    codes = []
    for key, info in names_data.items():
        if not isinstance(info, dict):
            continue
        mkt = info.get("market", "")
        # 提取纯数字代码
        code = key
        if not code.isdigit() and len(key) > 1:
            # 复合键如 sh000001 → 提取数字部分
            code = key[2:] if key[:2] in ("sh", "sz", "bj", "hk") else key
        # A股6位，港股5位
        code_len = len(code) if code.isdigit() else 0
        if mkt == "hk" and code_len == 5:
            codes.append((mkt, code))
        elif mkt in ("sh", "sz", "bj") and code_len == 6:
            codes.append((mkt, code))

    total = len(codes)
    _refresh_status["total"] = total
    _refresh_status["loaded"] = 0
    print(f"[PE-TTM] 开始刷新 {total} 只股票的 PE-TTM...")

    batch_size = 300
    new_count = 0
    got_set = set()  # 本次成功获取到PE-TTM的代码
    for i in range(0, total, batch_size):
        batch = codes[i:i + batch_size]
        q_codes = [f"{mkt}{code}" for mkt, code in batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(q_codes)
        try:
            resp = req.get(url, timeout=10)
            for line in resp.text.strip().split("\n"):
                if "v_" not in line:
                    continue
                try:
                    # 腾讯接口格式: v_sh600519="1~贵州茅台~600519~...~[39]市盈率~..."
                    parts = line.split('="')[1].strip().strip('";')
                    fields = parts.split("~")
                    # 从行前缀提取市场: v_sh... → sh, v_sz... → sz, v_hk... → hk
                    line_mkt = line[2:4] if len(line) > 4 else ""
                    if len(fields) > 39:
                        stock_code = fields[2]
                        pe_str = fields[39]  # 市盈率(动态)
                        if stock_code and stock_code.isdigit() and pe_str and pe_str.replace(".", "").replace("-", "").isdigit():
                            pe_val = float(pe_str)
                            if pe_val != 0:
                                cache_key = line_mkt + stock_code if line_mkt else stock_code
                                got_set.add(cache_key)
                                if cache_key not in _pe_ttm_cache or _pe_ttm_cache[cache_key] != pe_val:
                                    _pe_ttm_cache[cache_key] = pe_val
                                    new_count += 1
                except (ValueError, TypeError, IndexError):
                    pass
        except Exception as e:
            print(f"[PE-TTM] 第{i//batch_size+1}批失败: {e}")

        _refresh_status["loaded"] = min(i + batch_size, total)
        print(f"[PE-TTM] 进度: {_refresh_status['loaded']}/{total}, 新增/更新 {new_count} 条")

    # 统计未获取到的股票
    all_queried = {mkt + code for mkt, code in codes}
    missed = all_queried - got_set
    if missed:
        missed_list = sorted(missed)[:20]
        print(f"[PE-TTM] 未获取到PE-TTM: {len(missed)} 只 (如: {', '.join(missed_list)}{'...' if len(missed) > 20 else ''})")

    # 刷新指数归属（AKShare在线获取，与PE-TTM一起保存）
    _refresh_status["step"] = "刷新指数归属..."
    print("[指数归属] ========== 开始刷新指数归属 ==========")
    _fetch_index_belong_from_akshare()

    # 保存到文件（合并PE-TTM和指数归属，过滤掉旧格式的纯数字key）
    try:
        os.makedirs(os.path.dirname(app_config.stock_pe_ttm_file), exist_ok=True)
        # 找出所有有PE-TTM或指数归属的股票代码
        all_keys = set(_pe_ttm_cache.keys()) | set(_index_belong_cache.keys())
        combined = {}
        for k in all_keys:
            if k.isdigit() and len(k) == 6:
                continue  # 过滤旧格式纯数字key
            entry = {}
            pe_val = _pe_ttm_cache.get(k)
            idx_val = _index_belong_cache.get(k)
            if isinstance(pe_val, (int, float)) and pe_val != 0:
                entry["pe_ttm"] = pe_val
            if idx_val:
                entry["index"] = idx_val
            if entry:
                combined[k] = entry
        _safe_write_json_file(app_config.stock_pe_ttm_file, combined, ensure_ascii=False)
        print(f"刷新完成: 共 {len(combined)} 条 (PE-TTM: {sum(1 for v in combined.values() if 'pe_ttm' in v)} 条, 指数归属: {sum(1 for v in combined.values() if 'index' in v)} 条), 已保存到 {app_config.stock_pe_ttm_file}")
    except Exception as e:
        print(f"[PE-TTM] 保存失败: {e}")
        _refresh_status["error"] = f"保存 PE-TTM 失败: {e}"


def _collect_codes_from_vipdoc(vipdoc_dir):
    """兼容壳（阶段 5）：委托 DataAPI/ElTdxAPI（vipdoc_dir 由调用方注入，设计 4.4）"""
    return _ElTdx.collect_codes_from_vipdoc(vipdoc_dir)


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

    filled = 0
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
            except Exception as e:
                print(f"[股名刷新]   新浪A股批次{batch_num}失败: {e}")
            if batch_num < total_batches:
                time.sleep(0.5)

    # === 第二轮：港股（用腾讯财经API，新浪港股接口已失效） ===
    if hk_codes:
        total_batches = (len(hk_codes) - 1) // batch_size + 1
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
            except Exception as e:
                print(f"[股名刷新]   腾讯港股批次{batch_num}失败: {e}")
            if batch_num < total_batches:
                time.sleep(0.5)

    return filled


def _refresh_stock_names():
    """
    从本地文件批量获取全市场股票名称，保存到 stock_names.json。
    数据来源优先级：
      1. vipdoc/*.day 文件名（收集所有已下载过数据的股票代码）
      2. 新浪财经API（为无名称的代码批量查询名称）
    （阶段 4：名称缓存由 app_data 持有；本函数只做获取与合并，
     最终经 replace_names 同对象替换，_stock_names_cache 别名全程可见）
    """
    global _refresh_status

    if _refresh_status["running"]:
        return
    _refresh_status["running"] = True
    _refresh_status["step"] = "刷新股票名..."
    _refresh_status["error"] = None
    print("[股名刷新] ========== 开始刷新股票名称 ==========")

    # === 先加载已有缓存，新数据合并进去，不覆盖 ===
    raw_names = {}
    _load_stock_names_from_cache_file()
    if _stock_names_cache:
        for code, info in _stock_names_cache.items():
            if isinstance(info, dict):
                raw_names[code] = info
            else:
                raw_names[code] = {"name": info, "pinyin": ""}
        print(f"[股名刷新] 步骤1/5 加载缓存: 已加载 {len(raw_names)} 只")
    else:
        print("[股名刷新] 步骤1/5 加载缓存: 无缓存，全新读取")

    # === 方案1: vipdoc .day文件名收集代码 ===
    # .day 文件覆盖所有已下载过K线数据的股票
    vipdoc_codes = _collect_codes_from_vipdoc(app_config.vipdoc_dir)
    # 统计扫描结果
    v_sh = sum(1 for v in vipdoc_codes.values() if v.get("market") == "sh")
    v_sz = sum(1 for v in vipdoc_codes.values() if v.get("market") == "sz")
    v_hk = sum(1 for v in vipdoc_codes.values() if v.get("market") == "hk")
    v_total = v_sh + v_sz + v_hk
    cache_before = len(raw_names)
    vipdoc_new = 0   # 缓存中没有的新代码
    vipdoc_filled = 0  # 缓存中有但无名称，从vipdoc补全
    for code, info in vipdoc_codes.items():
        if code not in raw_names:
            raw_names[code] = info
            vipdoc_new += 1
        elif not raw_names[code].get("name"):
            raw_names[code]["name"] = info.get("name", "")
            vipdoc_filled += 1
    print(f"[股名刷新] 步骤2/5 合并扫描: vipdoc共{v_total}只 (sh{v_sh}+sz{v_sz}+ds{v_hk}), 缓存{cache_before}只, 合并后{len(raw_names)}只 (新增{vipdoc_new}只)")

    # === 方案2: 新浪API补全缺失的名称 ===
    # 即使已有缓存，如果有新发现的代码（如港股）没有名称，也要补全
    codes_without_name = [c for c, info in raw_names.items() if not info.get("name")]
    if codes_without_name:
        a_no = sum(1 for c in codes_without_name if raw_names[c].get("market") != "hk")
        hk_no = sum(1 for c in codes_without_name if raw_names[c].get("market") == "hk")
        print(f"[股名刷新] 步骤3/5 补全名称: {len(codes_without_name)} 只无名称 (A股{a_no}, 港股{hk_no})")
        temp_dict = {c: raw_names[c] for c in codes_without_name}
        filled = _fetch_names_from_sina_once(temp_dict)
        for code, info in temp_dict.items():
            if info.get("name"):
                raw_names[code] = info
        failed = len(codes_without_name) - filled
        if failed > 0:
            print(f"[股名刷新] 步骤3/5 补全名称: 成功 {filled} 只, 失败 {failed} 只")
        else:
            print(f"[股名刷新] 步骤3/5 补全名称: 全部成功 {filled} 只")
    else:
        print("[股名刷新] 步骤3/5 补全名称: 无需补全")

    # === 补充通达信板块指数名称（88xxxx系列，如880491半导体、881319半导体）===
    # 88xxxx代码不以标准A股格式开头，_is_a_stock_code() 会过滤掉，所以不在 raw_names 中。
    # 来源: tdxzs.cfg（通达信配置文件）和 tdxhy_mapping_data.py（本地映射表）
    tdxzs_filled = 0
    tdxzs_file = os.path.join(app_config.tdx_hq_cache, "tdxzs.cfg")
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
                        if not name or not code:
                            continue
                        # 跳过 8803xx-8804xx（旧版行业），由 881 研究行业替代
                        if code.startswith("8803") or code.startswith("8804"):
                            continue
                        compound_key = "sh" + code
                        if compound_key not in raw_names:
                            raw_names[compound_key] = {"name": name, "pinyin": "", "market": "sh"}
                            tdxzs_filled += 1
                        elif not raw_names[compound_key].get("name"):
                            raw_names[compound_key]["name"] = name
                            tdxzs_filled += 1
        except Exception as e:
            print(f"[股名刷新]   读取tdxzs.cfg失败: {e}")

    # 新版研究行业(881xxx)从 tdxhy_mapping_data 读取（阶段 5：单一加载函数 app_data.load_tdxhy_mapping，设计 8.8）
    tdxhy_filled = 0
    try:
        _TDXHY_881_TO_X = app_data.load_tdxhy_mapping()[1]
        for code_881, (x_code, name) in _TDXHY_881_TO_X.items():
            compound_key = "sh" + code_881
            if compound_key not in raw_names:
                raw_names[compound_key] = {"name": name, "pinyin": "", "market": "sh"}
                tdxhy_filled += 1
            elif not raw_names[compound_key].get("name"):
                raw_names[compound_key]["name"] = name
                tdxhy_filled += 1
    except Exception as e:
        print(f"[股名刷新]   加载tdxhy_mapping_data失败: {e}")

    block_filled = tdxzs_filled + tdxhy_filled
    print(f"[股名刷新] 步骤4/5 补充板块: tdxzs.cfg +{tdxzs_filled}条, tdxhy +{tdxhy_filled}条, 共补全 {block_filled} 条板块")

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
            # 通达信/新浪API中名称可能含空格（如"五 粮 液"）或全角字母（如"鲁泰Ａ"）→ 统一清理
            name_clean = name.replace(" ", "")
            # 全角ASCII → 半角: U+FF01-U+FF5E → U+0021-U+007E
            name_clean = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in name_clean)
            pinyin = ""
            if name_clean:
                try:
                    py_list = lazy_pinyin(name_clean)
                    pinyin = "".join([p[0].upper() for p in py_list if p])
                except Exception:
                    pinyin = ""
            all_names[code] = {"name": name_clean, "pinyin": pinyin, "market": market}
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
    filtered_empty = 0
    filtered_st = 0
    filtered_delist = 0
    for _code in list(all_names.keys()):
        name = all_names[_code].get("name", "")
        if not name:
            del all_names[_code]
            filtered_count += 1
            filtered_empty += 1
        elif name.startswith("*ST") or name.startswith("ST"):
            del all_names[_code]
            filtered_count += 1
            filtered_st += 1
        elif "退" in name:
            del all_names[_code]
            filtered_count += 1
            filtered_delist += 1
    if all_names:
        os.makedirs(os.path.dirname(app_config.stock_names_cache_file), exist_ok=True)
        _safe_write_json_file(app_config.stock_names_cache_file, all_names, ensure_ascii=False)
        app_data.replace_names(all_names)  # 同对象替换：别名 _stock_names_cache 即时可见
        sh_count = sum(1 for c in all_names if all_names[c].get("market") == "sh")
        sz_count = sum(1 for c in all_names if all_names[c].get("market") == "sz")
        hk_count = sum(1 for c in all_names if all_names[c].get("market") == "hk")
        if filtered_count > 0:
            parts = []
            if filtered_st: parts.append(f"ST/*ST {filtered_st}只")
            if filtered_delist: parts.append(f"退市 {filtered_delist}只")
            if filtered_empty: parts.append(f"无名 {filtered_empty}只")
            print(f"[股名刷新] 步骤5/5 过滤保存: 过滤 {filtered_count} 只 ({', '.join(parts)}), 最终 {len(all_names)} 只 (上海{sh_count}, 深圳{sz_count}, 港股{hk_count}) → {os.path.basename(app_config.stock_names_cache_file)}")
        else:
            print(f"[股名刷新] 步骤5/5 过滤保存: 最终 {len(all_names)} 只 (上海{sh_count}, 深圳{sz_count}, 港股{hk_count}) → {os.path.basename(app_config.stock_names_cache_file)}")
    else:
        print("[股名刷新] 步骤5/5 过滤保存: 失败，未获取到任何数据")

    # 刷新板块文件（block_zs.dat / block_gn.dat / block_fg.dat / block.dat）
    print("[板块刷新] ========== 开始刷新板块文件 ==========")
    _refresh_status["step"] = "刷新成分股..."
    try:
        def _set_step(msg):
            _refresh_status["step"] = msg
        refresh_block_files(progress_callback=_set_step)
    except Exception as e:
        print(f"[板块刷新] 板块文件刷新失败: {e}")

    # 刷新 PE-TTM（增量更新 stock_pettm_index.json）
    print("[PE-TTM] ========== 开始刷新PE-TTM ==========")
    try:
        _refresh_pe_ttm()
    except Exception as e:
        print(f"[PE-TTM] PE-TTM 刷新失败: {e}")
        _refresh_status["error"] = f"PE-TTM 刷新失败: {e}"

    # 全部刷新完成，标记状态
    _refresh_status["running"] = False
    _refresh_status["step"] = ""



# ============================================================
# 解析证券代码，判断市场
# ============================================================



def _get_stock_market_code(code):
    """识别股票/指数代码，返回 (market, code)；无法识别返回 (None, code)。"""
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
    code = code.strip().upper()
    futures_code = _get_futures_code(code)
    if futures_code:
        return 'futures', futures_code
    return _get_stock_market_code(code)



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
    """统一的缠论配置，股票和期货共用。配置值已迁移到 ChanConfig.CChanConfig 默认值
    """
    from ChanConfig import CChanConfig
    return CChanConfig()


# ============================================================
# 缠论分析（chan.py 版本）
# ============================================================
import collections

# 统一缓存（阶段 4：实现与状态收敛 App/AppData.py；别名共享同一对象）
_stocks_analysis_cache = app_data.stocks_analysis_cache   # 分析结果 LRU
_cache_lock = app_data.cache_lock                        # 保护缓存的并发读写

# 扫描跳过记录（收集后统一打印）
_scan_skip_log = []

# 全市场流通市值缓存（通过腾讯接口获取，本地JSON兜底；阶段 4 收敛 app_data）
def _load_float_mc_cache():
    """从本地JSON加载流通市值缓存（委托 app_data）"""
    return app_data.load_float_mc_cache()

def _fetch_float_mc_from_tencent(stock_list):
    """通过腾讯行情接口批量获取流通市值（毫秒级，极其稳定）。
    stock_list: [{"code": "600519", "prefix": "1"}, ...]
    返回: {code: float_mc(亿元)}，失败返回空字典。
    """
    if not stock_list:
        return {}
    import requests as req
    # 构造腾讯代码：prefix 0→sz, 1→sh, 2→bj
    _PFX = {"0": "sz", "1": "sh", "2": "bj"}
    codes = []
    for stk in stock_list:
        code = stk.get("code", "")
        prefix = stk.get("prefix", "")
        mkt = _PFX.get(prefix, "")
        if mkt and code:
            codes.append(mkt + code)
    if not codes:
        return {}
    # 腾讯接口限制每次约200-300只，超过则分批
    batch_size = 300
    all_mv = {}
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            resp = req.get(url, timeout=5)
            for line in resp.text.strip().split("\n"):
                if "v_" not in line:
                    continue
                try:
                    # 格式: v_sh600519="1~贵州茅台~600519~...~[44]流通市值~..."
                    parts = line.split('="')[1].strip().strip('";')
                    fields = parts.split("~")
                    if len(fields) > 44:
                        stock_code = fields[2]  # 纯数字代码
                        nmc = fields[44]  # 流通市值(亿元，腾讯接口直接返回亿元)
                        if stock_code and nmc:
                            all_mv[stock_code] = float(nmc)  # 已经是亿元，无需转换
                except (ValueError, TypeError, IndexError):
                    pass
        except Exception as e:
            print(f"[流通市值] 腾讯接口第{i//batch_size+1}批失败: {type(e).__name__}: {e}")
    return all_mv

def _update_float_mc_cache(mv_dict):
    """将外部获取的流通市值字典合并到全局缓存并落盘（委托 app_data）。
    调用方应确保 _load_float_mc_cache() 已先执行。"""
    return app_data.update_float_mc_cache(mv_dict)

def _get_float_mc_from_cache(code):
    """从缓存获取流通市值（亿元），未命中返回None（委托 app_data）。"""
    return app_data.get_float_mc_from_cache(code)

# 扫描与冷启动共用同一个 _stocks_analysis_cache，由 LRU 50 条统一管理
# 扫描时：有买点才保留缓存，否则释放

# 扫描锁（防止并发扫描导致内存峰值翻倍）
_scan_lock = threading.Lock()

# 扫描终止标志：前端点击中断时设True，后端检查后跳过后续请求
_scan_aborted = False

# 扫描开始时间：用于计算扫描耗时，在通知中显示
_scan_start_time = None

# 页面指数代码：当前页面正在查看的通达信板块指数代码（如 880491），用于"成分股"扫描来源
_page_index_code = None

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


def _send_windows_notification(title, message):
    """发送 Windows 10/11 Toast 通知（右下角弹出）。
    winotify 零依赖，仅需 PowerShell（Windows 内置），无需额外安装任何包。
    只需在 venv 中执行一次: pip install winotify
    在新线程中执行，不阻塞主流程。
    """
    def _notify():
        try:
            from winotify import Notification
            toast = Notification(app_id="缠论扫描", title=title, msg=message, duration="short")
            toast.show()
        except ImportError:
            print("[通知] 未安装 winotify，请在 venv 中执行: pip install winotify")
        except Exception as e:
            print(f"[通知] 发送失败: {type(e).__name__}: {e}")

    t = threading.Thread(target=_notify, daemon=True)
    t.start()


# ============================================================
# 手选进入段选点保存/恢复（阶段 4：实现收敛 App/AppData.py，此处仅留 schema 常量供本文件消费；
# 路径 SAVED_POINT_FILE / 持久化实现均单源于 app_data / app_config）
# ============================================================
# CSV列：股票代码,股票名,年K选点,季K选点,月K选点,周K选点,日K选点,30分选点,15分选点,5分选点,1分选点
SAVED_POINT_COLUMNS = AppData_SAVED_POINT_COLUMNS
# freq -> CSV列名 的映射
FREQ_TO_COL = AppData_FREQ_TO_COL
# 日内周期集合：分钟级
INTRADAY_FREQS = {"30m", "5m", "1m"}
# 秒级周期：K线时间含秒
SUBSECOND_FREQS = {"15s"}


def _get_date_fmt(freq):
    """根据周期返回统一日期格式（使用斜杠 / 分隔符，与 CChan 输出格式一致）。

    - 秒级（15s）→ "%Y/%m/%d %H:%M:%S"
    - 分钟级（30m, 5m, 1m）→ "%Y/%m/%d %H:%M"
    - 日线及以上 → "%Y/%m/%d"
    """
    if freq in SUBSECOND_FREQS:
        return "%Y/%m/%d %H:%M:%S"
    if freq in INTRADAY_FREQS:
        return "%Y/%m/%d %H:%M"
    return "%Y/%m/%d"


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
        print(f"[警告] 无法定位分型中间K线在kl_list.lst中的位置")
        return None

    # 左肩 = 分型合并K线的前一个合并K线
    left_klc = kl_list.lst[mid_idx - 1]  # type: ignore[union-attr]

    # 取左肩原始K线序列的第一根（最左边）
    if hasattr(left_klc, 'lst') and left_klc.lst:  # type: ignore[union-attr]
        first_klu = left_klc.lst[0]  # type: ignore[union-attr]
    else:
        # 没有包含关系，左肩就是一根原始K线
        first_klu = (left_klc.get_high_peak_klu() or left_klc.get_low_peak_klu())

    if first_klu is None:
        print(f"[警告] 无法获取左肩K线单元")
        return None

    return first_klu.time.toFmtStr(_get_date_fmt(freq))





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
        return bi.get_end_klu().time.toFmtStr(date_fmt)
    return ""


def _load_saved_point_times():
    """从CSV文件加载所有选点记录（委托 app_data；启动加载已在 app_data 实例化时完成）"""
    return app_data.load_saved_point_times()

def _save_point_time(code, name, freq, sdt):
    """保存或更新某只股票某个周期的选点（委托 app_data）"""
    return app_data.save_point_time(code, name, freq, sdt)


def _clear_saved_point_time(code, freq):
    """清除某只股票某个周期在CSV中的选点，同时更新内存缓存（委托 app_data）"""
    return app_data.clear_saved_point_time(code, freq)


def _cleanup_all_futures_data():
    """期货切到股票时彻底清理所有期货数据：K线缓存、分析缓存、选点记录

    注意：本函数**不得**调用 gc.collect()。SSE 期货流关闭时 TqApi 内部
    协程可能仍处于挂起态（tqsdk 在 Python 3.12+ 的已知问题，见官方
    issue #442），若在此强制 GC 会触发「Task was destroyed but it is
    pending!」+「Event loop is closed」错误级联（实测日志）。TqApi 的
    生命周期由 SSE 流 finally 块 src.close()→api.close() 负责，内存由
    Python 引用计数 + 自动 GC 回收即可。
    """
    # 1. 清空 CTqSdkAPI 的K线缓存
    if CTqSdkAPI is not None:
        CTqSdkAPI.clear_all_cache()
        print("[清理] 已清空期货K线缓存")

    # 2. 清空选点记录中的期货条目（key以KQ.开头）
    pts_to_del = [k for k in list(_saved_point_times.keys()) if k.startswith("KQ.")]
    for k in pts_to_del:
        del _saved_point_times[k]
    if pts_to_del:
        print(f"[清理] 已清除 {len(pts_to_del)} 条期货选点记录")


# 选点内存缓存：别名 = app_data 实例字段（共享同一对象；
# 启动加载已随 app_data 实例化完成，本文件多处直接读该字典保持零漂移）
_saved_point_times = app_data.saved_point_times


def _save_last_code_freq(code, freq="d"):
    """持久化上次查看的代码和周期到JSON文件（委托 app_data；股票和期货通用）"""
    return app_data.save_last_code_freq(code, freq)


def _load_last_code_freq():
    """从JSON文件加载上次查看的代码和周期（委托 app_data）"""
    return app_data.load_last_code_freq()


# ============================================================
# 文字标注持久化存储（阶段 4：实现收敛 App/AppData.py）
# ============================================================
# 标注缓存：别名 = app_data 实例字段（共享同一对象；启动加载已随 app_data 实例化完成）
_annotations_cache = app_data.annotations_cache


def _load_annotations():
    """从 text_annotation.json 加载标注数据到内存（委托 app_data；幂等）"""
    return app_data.load_annotations()


def _save_annotations():
    """保存标注数据到 text_annotation.json（委托 app_data）"""
    return app_data.save_annotations()


def _get_annotation_key(code, freq):
    """生成标注缓存的键: {code}_{freq}（实现收敛 App/AppData.get_annotation_key）"""
    from App.AppData import get_annotation_key
    return get_annotation_key(code, freq)


def _get_annotations_for(code, freq):
    """获取某股票某周期的所有标注（委托 app_data）"""
    return app_data.get_annotations_for(code, freq)


def _add_annotation(code, freq, date_str, text, y_offset=0):
    """添加一条标注，同日期同文字去重（委托 app_data）"""
    return app_data.add_annotation(code, freq, date_str, text, y_offset)


def _delete_annotation(code, freq, date_str, text):
    """删除一条标注（委托 app_data）"""
    return app_data.delete_annotation(code, freq, date_str, text)


def _delete_annotation_by_date(code, freq, date_str):
    """删除某日期下所有标注（委托 app_data）"""
    return app_data.delete_annotation_by_date(code, freq, date_str)


def _delete_all_annotations(code, freq):
    """删除某股票某周期下全部标注（委托 app_data）"""
    return app_data.delete_all_annotations(code, freq)


def _get_annotated_codes(freq=""):
    """获取所有有标注的股票代码+周期列表，用于自选扫描（委托 app_data）"""
    return app_data.get_annotated_codes(freq)


# 启动时加载标注数据（已随 app_data 实例化完成；此处保留调用幂等无副作用）
_load_annotations()


def _calc_futures_white_hline(kl_list, _freq, date_fmt):
    """计算期货最新笔的白色横虚线数据（与股票逻辑一致）。
    返回 {"price": float, "start_date": str} 或 None。"""
    white_hline = None
    if not kl_list or not kl_list.bi_list:
        return white_hline
    latest_bi = kl_list.bi_list[-1]
    direction = "up" if latest_bi.is_up() else "down"
    end_klc = getattr(latest_bi, 'end_klc', None)
    if end_klc is None:
        return white_hline
    end_fx_idx = None
    for idx, klc in enumerate(kl_list.lst):  # type: ignore[union-attr]
        if klc is end_klc:
            end_fx_idx = idx
            break
    if end_fx_idx is None or end_fx_idx <= 0:
        return white_hline
    left_klc = kl_list.lst[end_fx_idx - 1]  # type: ignore[union-attr]
    klc_high = left_klc.high  # type: ignore[union-attr]
    klc_low = left_klc.low  # type: ignore[union-attr]
    tgt_klu = None
    if hasattr(left_klc, 'lst') and left_klc.lst:  # type: ignore[union-attr]
        for klu in left_klc.lst:  # type: ignore[union-attr]
            if direction == "down" and klu.high == klc_high:
                tgt_klu = klu
                break
            elif direction == "up" and klu.low == klc_low:
                tgt_klu = klu
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
    return white_hline

def init_chan_symbol(api, symbol, _name, freq_sec, freq_label, start_time=None):
    """拉取历史K线 + 运行 chan.py 分析，返回 (chan, klines, kl_type, records) 或 None。
    由 SSE handler 调用，每个 SSE 连接自包含。
    start_time: 选点起始时间，有值时只拉取该时间之后的K线"""
    import time as _time
    from Common.CEnum import KL_TYPE, AUTYPE
    from Chan import CChan
    from ChanConfig import CChanConfig

    display_label = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{display_label}"

    try:
        records = CTqSdkAPI.fetch_kline(api, symbol, freq_sec=freq_sec, display_key=display_key, start_time=start_time)
        if len(records) > 1:
            now = datetime.now()
            if (now - records[-1]["dt"]).total_seconds() < freq_sec:
                records = records[:-1]
        CTqSdkAPI.set_data(records, symbol=f"{symbol}:{freq_sec}")

        if len(records) == 0:
            print(f"[{display_key}] ⑵ 无有效数据，跳过")
            return None

        t_chan = _time.time()

        _freq_to_kl = {
            15: KL_TYPE.K_15S, 30: KL_TYPE.K_30S, 60: KL_TYPE.K_1M,
            300: KL_TYPE.K_5M, 900: KL_TYPE.K_15M, 1800: KL_TYPE.K_30M,
            3600: KL_TYPE.K_60M, 86400: KL_TYPE.K_DAY,
            604800: KL_TYPE.K_WEEK, 2592000: KL_TYPE.K_MON,
        }
        kl_type = _freq_to_kl.get(freq_sec, KL_TYPE.K_15S)

        config = CChanConfig()

        chan = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )

        for _snapshot in chan.step_load():
            pass

        klines = api.get_kline_serial(symbol, freq_sec)

        if _SSE_DEBUG:
            print(f"[{display_key}] ⑵ 缠论分析: 消费 {len(records)}根K线, 耗时 {_time.time()-t_chan:.1f}s")
        return (chan, klines, kl_type, records)

    except Exception as e:
        import traceback
        print(f"[{display_key}] ⑵ 失败: {e}")
        traceback.print_exc()
        return None

def _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None):
    """从 CChan 对象中提取缠论结构快照，格式与 /api/stock 一致。
    lightweight=True: 仅返回最后一根K线的OHLC变化（周期内tick更新用），不遍历全量结构。
    klines: 天勤实时K线DataFrame（lightweight=True时优先使用，避免chan框架kl_list滞后）"""
    from Common.CEnum import FX_TYPE
    kl_list = chan[kl_type]
    _date_fmt = _get_date_fmt(freq_label)
    _meta_freq_label = _get_freq_label(freq_label)

    if lightweight:
        # ★ 优先从天勤实时 klines 读取当前形成中K线的OHLC，避免 chan 框架 kl_list 滞后
        if klines is not None and len(klines) > 0:
            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            kline_dt = "?"
            if dt_ns is not None:
                try:
                    kline_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime(_date_fmt)
                except Exception as e:
                    print(f"[警告] 异常: {type(e).__name__}: {e}")
            o = float(last_row.get("open", 0) or 0)
            h = float(last_row.get("high", 0) or 0)
            l = float(last_row.get("low", 0) or 0)
            c = float(last_row.get("close", 0) or 0)
            return {
                "type": "tick",
                "kline": {
                    "date": kline_dt,
                    "open": round(o, 3),
                    "high": round(h, 3),
                    "low": round(l, 3),
                    "close": round(c, 3),
                },
                "meta": {
                    "symbol": symbol, "name": name, "freq": _meta_freq_label,
                    "generated_at": datetime.now().strftime(_date_fmt),
                    "is_realtime": True, "market": "futures",
                },
            }
        # 回退：无 klines 时从 chan 框架读取
        if len(kl_list.lst) == 0:  # type: ignore[union-attr]
            return None
        last_klc = kl_list.lst[-1]  # type: ignore[union-attr]
        if len(last_klc.lst) == 0:  # type: ignore[union-attr]
            return None
        last_klu = last_klc.lst[-1]  # type: ignore[union-attr]
        return {
            "type": "tick",
            "kline": {
                "date": last_klu.time.toFmtStr(_date_fmt),
                "open": round(last_klu.open, 3),
                "high": round(last_klu.high, 3),
                "low": round(last_klu.low, 3),
                "close": round(last_klu.close, 3),
            },
            "meta": {
                "symbol": symbol, "name": name, "freq": _meta_freq_label,
                "generated_at": datetime.now().strftime(_date_fmt),
                "is_realtime": True, "market": "futures",
            },
        }

    klines_out = []
    for klc in kl_list.lst:  # type: ignore[union-attr]
        for klu in klc.lst:  # type: ignore[union-attr]
            t = klu.time
            klines_out.append({
                "date": t.toFmtStr(_date_fmt),
                "timestamp": int(t.ts * 1000),
                "open": round(klu.open, 3),
                "high": round(klu.high, 3),
                "low": round(klu.low, 3),
                "close": round(klu.close, 3),
                "vol": int(klu.trade_info.metric.get("volume", 0) or 0),
                "amount": round(klu.trade_info.metric.get("turnover", 0) or 0, 2),
            })

    closes = [k["close"] for k in klines_out]
    if len(closes) >= 26:
        ema12 = ema(closes, 12)
        ema26 = ema(closes, 26)
        dif = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        dea = ema(dif, 9)
        macd_vals = [2 * (d - a) for d, a in zip(dif, dea)]
        for i in range(len(klines_out)):
            if i < len(dif):
                klines_out[i]["dif"] = round(dif[i], 4)
                klines_out[i]["dea"] = round(dea[i], 4)
                klines_out[i]["macd"] = round(macd_vals[i], 4)
            else:
                klines_out[i]["dif"] = 0; klines_out[i]["dea"] = 0; klines_out[i]["macd"] = 0
    else:
        for k in klines_out:
            k["dif"] = 0; k["dea"] = 0; k["macd"] = 0

    bis = []
    for bi in kl_list.bi_list:
        try:
            direction = "up" if bi.is_up() else "down"
            begin_klu = bi.get_begin_klu()
            end_klu = bi.get_end_klu()
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
            # 左肩/右肩原始K线时间（用于双窗口红框定位）
            fx_a_raw_dt = ""
            fx_b_raw_dt = ""
            shoulder_times = _main_bi_range(bi, _date_fmt)
            if shoulder_times:
                fx_a_raw_dt, fx_b_raw_dt, _, _ = shoulder_times
            bis.append({
                "sdt": begin_klu.time.toFmtStr(_date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(_date_fmt) if end_klu else "",
                "sdt_ts": int(begin_klu.time.ts * 1000) if begin_klu else 0,
                "edt_ts": int(end_klu.time.ts * 1000) if end_klu else 0,
                "direction": direction,
                "fx_a_price": round(bi.get_begin_val(), 2),
                "fx_b_price": round(bi.get_end_val(), 2),
                "high": round(bi._high(), 2),
                "low": round(bi._low(), 2),
                "power": round(abs(bi.get_end_val() - bi.get_begin_val()), 2),
                "is_sure": getattr(bi, 'is_sure', True),
                "end_fx_idx": end_fx_idx,
                "begin_fx_idx": begin_fx_idx,
                "fx_a_raw_dt": fx_a_raw_dt,
                "fx_b_raw_dt": fx_b_raw_dt,
                "fx_a_sub_dt": "",
                "fx_b_sub_dt": "",
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    fxs = []
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            peak_klu = klc.get_high_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(_date_fmt) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu else 0,
                "mark": "G", "price": klc.high, "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            peak_klu = klc.get_low_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(_date_fmt) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu else 0,
                "mark": "D", "price": klc.low, "high": klc.high, "low": klc.low,
            })

    segs = []
    for seg in kl_list.seg_list:
        try:
            direction = "up" if seg.is_up() else "down"
            begin_klu = seg.get_begin_klu()
            end_klu = seg.get_end_klu()
            if direction == "up":
                begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
                end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
            else:
                begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
                end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
            segs.append({
                "sdt": begin_klu.time.toFmtStr(_date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(_date_fmt) if end_klu else "",
                "direction": direction,
                "begin_price": begin_price, "end_price": end_price,
                "high": round(seg._high(), 2), "low": round(seg._low(), 2),
                "amp": round(seg.amp(), 2),
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_list = []
    for zs in kl_list.zs_list:
        try:
            zs_list.append({
                "sdt": zs.begin.time.toFmtStr(_date_fmt) if zs.begin and hasattr(zs.begin, 'time') else "",
                "edt": zs.end.time.toFmtStr(_date_fmt) if zs.end and hasattr(zs.end, 'time') else "",
                "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, _date_fmt),
                "zg": round(zs.high, 2), "zd": round(zs.low, 2),
                "gg": round(zs.peak_high, 2), "dd": round(zs.peak_low, 2),
                "dir": "up" if (zs.bi_in and zs.bi_in.is_up()) else "down",
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_stars = []
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        star_date = begin_klu.time.toFmtStr(_date_fmt)
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "D", "color": "red"})
        else:
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "G", "color": "green"})

    bsps = []
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            bsps.append({
                "date": bsp.klu.time.toFmtStr(_date_fmt),
                "timestamp": int(bsp.klu.time.ts * 1000),
                "type": bsp.type2str(), "is_buy": bsp.is_buy,
                "price": round(bsp.klu.close, 3),
                "high": round(bsp.klu.high, 3),
                "low": round(bsp.klu.low, 3),
            })
    except Exception as e:
        print(f"[警告] 异常: {type(e).__name__}: {e}")

    return {
        "meta": {
            "symbol": symbol, "name": name, "freq": _meta_freq_label,
            "kline_count": len(klines_out), "bi_count": len(bis),
            "fx_count": len(fxs), "zs_count": len(zs_list),
            "seg_count": len(segs), "bsp_count": len(bsps),
            "generated_at": datetime.now().strftime(_date_fmt),
            "is_realtime": True, "is_replay": False, "market": "futures",
            "saved_selection_date": saved_selection_date,
        },
        "klines": klines_out, "bis": bis, "fxs": fxs, "segs": segs,
        "zs": zs_list, "zs_stars": zs_stars, "bsps": bsps, "white_hline": None,
    }



def _analyze_futures_internal(code, freq="1m", end_date=None, dual=False, existing_chan=None, existing_records=None, step=None, sub_freq=None):
    """
    使用天勤数据源 + chan.py 进行期货/期指缠论分析（静态模式，HTTP 请求）
    与股票分析输出格式一致，便于前端复用同一套图表渲染逻辑。

    dual=True: 双窗口模式，返回 result 含 sub 字段（两个独立 CChan 对象）。
    existing_chan: 双窗口模式下，复用已有的单窗口 CChan 对象（匹配周期则复用）。
    existing_records: 对应 existing_chan 的 records。
    step: 箭头步进，在 full_records 中从 end_date 位置偏移 step 根K线作为新的截断日期。
    sub_freq: 双窗口下窗周期。None 时使用默认映射 _FUTURES_DUAL_FREQ_MAP。
    """
    import time
    t_start = time.time()

    if not TQ_AVAILABLE or CTqSdkAPI is None:
        return {"error": "天勤数据源未安装，请执行: pip install tqsdk"}

    # 确定周期秒数
    freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 86400)

    # 1. 拉取历史K线（每次冷启动重新拉取天勤数据）
    t_fetch = time.time()
    from tqsdk import TqApi, TqAuth
    from DataAPI.TqSdkAPI import TQ_ACCOUNT, TQ_PASSWORD
    _display_key = f"{code}:{CTqSdkAPI.FREQ_LABEL_CN.get(freq, freq)}"
    _api = None
    full_records = []
    try:
        _api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
        full_records = CTqSdkAPI.fetch_kline(_api, code, freq_sec=freq_sec, display_key=_display_key)
    except Exception as _e:
        print(f"[期货][错误] 天勤拉取K线失败: {type(_e).__name__}: {_e}")
        return {"error": f"天勤拉取K线失败: {type(_e).__name__}: {_e}"}
    finally:
        if _api is not None:
            try:
                _api.close()
            except Exception as _e:
                print(f"[警告] 关闭天勤连接异常: {type(_e).__name__}: {_e}")
    print(f"[拉取] ⑴ 天勤拉取K线: {time.time()-t_fetch:.3f}s, {len(full_records)}条")
    if len(full_records) < 5:
        return {"error": f"K线数据不足: 仅{len(full_records)}条"}

    # 2. 截断（end_date 复盘模式）
    if end_date:
        target_dt = None
        for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
            try:
                target_dt = datetime.strptime(end_date, fmt)
                break
            except ValueError:
                continue
        if target_dt is None:
            return {"error": f"无法解析日期: {end_date}"}
        # === 箭头步进：在 full_records 中从 end_date 位置偏移 step 根K线 ===
        if step is not None:
            step = int(step)
            if step != 0:
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
                        print(f"[futures][箭头] step={step}: {full_records[anchor_idx]['dt']} → {target_dt} (idx {anchor_idx} → {new_idx})")
                    else:
                        print(f"[futures][箭头] step={step} 越界: idx {anchor_idx} → {new_idx}, 共{len(full_records)}条")

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
    t0 = time.time()
    config = _make_chan_config()

    # 每次请求重置复盘标记，避免残留前一次状态
    from BuySellPoint.BSPointList import CMyBSPointList
    CMyBSPointList.REPLAY_MODE = False

    try:
        if end_date:
            from BuySellPoint.BSPointList import CMyBSPointList
            CMyBSPointList.REPLAY_MODE = True
        chan = CChan(
            code=code,
            begin_time=None,
            end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[_get_kl_type(freq)],
            config=config,
            autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan.step_load():
            pass
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        records_info = ""
        if records:
            records_info = f" records={len(records)}条 [{records[0]['dt']} ~ {records[-1]['dt']}]"
        print(f"[期货][错误] chan.py 分析失败: code={code} freq={freq}{records_info}")
        print(f"[期货][错误] 异常类型: {type(e).__name__}, 异常信息: {e}")
        print(f"[期货][错误] 完整堆栈:\n{tb}")
        return {"error": f"chan.py 期货分析失败: {type(e).__name__}: {e}"}
    finally:
        if end_date:
            CMyBSPointList.REPLAY_MODE = False

    kl_list = chan[_get_kl_type(freq)]
    print(f"[分析] ⑶ chan.py分析: {time.time()-t0:.3f}s, 合并K线={len(kl_list.lst)}, 笔={len(kl_list.bi_list)}, 中枢={len(kl_list.zs_list)}")

    # 6. 提取结果（与股票一致的格式，用 records 而非 kl_list）

    t_extract = time.time()
    closes = [r["close"] for r in records]
    macd_list = calculate_macd(closes)
    date_fmt = _get_date_fmt(freq)

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
            sdt_str = begin_klu.time.toFmtStr(date_fmt) if begin_klu else ""
            edt_str = end_klu.time.toFmtStr(date_fmt) if end_klu else ""
            try:
                sdt_ts = int(begin_klu.time.ts * 1000) if begin_klu else 0
            except:
                sdt_ts = 0
            try:
                edt_ts = int(end_klu.time.ts * 1000) if end_klu else 0
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

            # 分型肩部原始K线时间
            # chan.py: begin_klc 是分型中间KLC（2），begin_klc.pre 是左肩KLC（1），begin_klc.next 是右肩KLC（3）
            # 左肩KLC可能合并了多根原始K线，取第一根 = A
            # 右肩KLC可能合并了多根原始K线，取最后一根 = B
            fx_a_raw_dt = ""
            fx_b_raw_dt = ""
            a_klu = None
            b_klu = None
            try:
                begin_klc = bi.begin_klc
                end_klc = bi.end_klc
                # A: 左肩第一根原始K线 = begin_klc.pre.lst[0]
                left_shoulder_klc = begin_klc.pre if begin_klc else None
                if left_shoulder_klc and left_shoulder_klc.lst:
                    a_klu = left_shoulder_klc.lst[0]
                if a_klu:
                    fx_a_raw_dt = a_klu.time.toFmtStr(date_fmt)
                # B: 右肩最后一根原始K线 = end_klc.next.lst[-1]
                right_shoulder_klc = end_klc.next if end_klc else None
                if right_shoulder_klc and right_shoulder_klc.lst:
                    b_klu = right_shoulder_klc.lst[-1]
                if b_klu:
                    fx_b_raw_dt = b_klu.time.toFmtStr(date_fmt)
            except Exception as _e:
                print(f"[警告] 异常: {type(_e).__name__}: {_e}")

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
                "fx_a_sub_dt": "",
                "fx_b_sub_dt": "",
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    # 提取分型（与股票路径一致）
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            mark = "G"
            price = klc.high
            klu = klc.get_high_peak_klu()
            fx_date = klu.time.toFmtStr(date_fmt) if klu else ""
            fx_data.append({
                "date": fx_date,
                "timestamp": int(klu.time.ts * 1000) if klu else 0,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            mark = "D"
            price = klc.low
            klu = klc.get_low_peak_klu()
            fx_date = klu.time.toFmtStr(date_fmt) if klu else ""
            fx_data.append({
                "date": fx_date,
                "timestamp": int(klu.time.ts * 1000) if klu else 0,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })

    # 提取线段（与股票代码完全一致）
    for seg in kl_list.seg_list:
        try:
            direction = "up" if seg.is_up() else "down"
            begin_klu = seg.get_begin_klu()
            end_klu = seg.get_end_klu()
            sdt = begin_klu.time.toFmtStr(date_fmt) if begin_klu else ""
            edt = end_klu.time.toFmtStr(date_fmt) if end_klu else ""
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
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    for zs in kl_list.zs_list:
        try:
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
        star_date = begin_klu.time.toFmtStr(date_fmt)
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
            klu = bsp.klu
            bsp_date = klu.time.toFmtStr(date_fmt)
            try:
                bsp_ts = int(datetime.strptime(bsp_date, date_fmt).timestamp()) * 1000
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
        print(f"[调试] 期货获取买卖点失败: {e}")

    # 计算白色横虚线（最新笔分型上下沿，K线确认后才有意义）
    white_hline = _calc_futures_white_hline(kl_list, freq, date_fmt)

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
            "forward_adjust": False,
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

    # 双窗口模式：提取子级别数据（独立 CChan 对象）
    if dual:
        # 优先使用传入的 sub_freq（双窗口周期独立），否则用默认映射
        if not sub_freq:
            sub_freq = _FUTURES_DUAL_FREQ_MAP.get(freq)
        if not sub_freq:
            result["error"] = f"双窗口不支持当前周期: {freq}"
            return result
        sub_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(sub_freq, 15)
        print(f"[双窗口] 开始提取子级别({sub_freq})数据...")

        # 检查 existing_chan 是否匹配 sub_freq，匹配则复用
        if existing_chan is not None and existing_records is not None and freq == sub_freq:
            # existing_chan 匹配的是主周期，子周期需要新建
            pass
        elif existing_chan is not None and existing_records is not None and freq != sub_freq:
            # 如果 existing_chan 正好匹配 sub_freq，复用它
            # 这个情况发生在上窗周期!=单窗口周期时（暂不涉及，保留接口）
            pass

        # 拉取子级别历史K线
        t_sub_fetch = time.time()
        _sub_display_key = f"{code}:{CTqSdkAPI.FREQ_LABEL_CN.get(sub_freq, sub_freq)}"
        _api_sub = None
        sub_full_records = []
        try:
            _api_sub = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
            sub_full_records = CTqSdkAPI.fetch_kline(_api_sub, code, freq_sec=sub_freq_sec, display_key=_sub_display_key)
        except Exception as _e:
            print(f"[双窗口][错误] 子级别天勤拉取K线失败: {type(_e).__name__}: {_e}")
            return result
        finally:
            if _api_sub is not None:
                try:
                    _api_sub.close()
                except Exception as _e:
                    print(f"[警告] 关闭天勤子级别连接异常: {type(_e).__name__}: {_e}")
        print(f"[双窗口] 子级别({sub_freq})拉取K线: {time.time()-t_sub_fetch:.3f}s, {len(sub_full_records)}条")

        if len(sub_full_records) < 5:
            print(f"[双窗口] 子级别({sub_freq})数据不足，仅{len(sub_full_records)}条，跳过")
            return result

        # 截断到主级别时间范围（同步）
        # 期货K线时间=开始时间（不同于股票=结束时间），用数学换算精确截断：
        #   下窗右边界 = 上窗最后一根K线开始时间 + (上窗周期 - 下窗周期)
        if len(sub_full_records) > 0 and records:
            main_start = records[0]["dt"]
            main_end = records[-1]["dt"]
            offset_sec = freq_sec - sub_freq_sec
            sub_end = main_end + timedelta(seconds=offset_sec)
            sub_before = len(sub_full_records)
            sub_full_records = [r for r in sub_full_records
                                if main_start <= r["dt"] <= sub_end]
            if sub_before != len(sub_full_records):
                print(f"[双窗口] 子级别({sub_freq})同步截断: {sub_before}条 -> {len(sub_full_records)}条")

        sub_records = sub_full_records

        # 注入子级别数据源
        sub_code = f"{code}:{sub_freq_sec}"
        CTqSdkAPI.set_data(sub_records, symbol=sub_code)

        # 创建子级别 CChan
        t_sub_chan = time.time()
        sub_config = _make_chan_config()
        try:
            sub_chan = CChan(
                code=sub_code,
                begin_time=None, end_time=None,
                data_src="custom:TqSdkAPI.CTqSdkAPI",
                lv_list=[_get_kl_type(sub_freq)],
                config=sub_config,
                autype=AUTYPE.NONE,
                market_type="futures",
            )
            for _snapshot in sub_chan.step_load():
                pass
        except Exception as e:
            print(f"[双窗口] 子级别({sub_freq}) chan.py 分析失败: {e}")
            return result

        _futures_analysis_cache[f"{code.upper()}:{sub_freq}"] = sub_chan
        sub_kl_list = sub_chan[_get_kl_type(sub_freq)]
        print(f"[双窗口] 子级别({sub_freq}) chan.py分析: {time.time()-t_sub_chan:.3f}s, "
              f"合并K线={len(sub_kl_list.lst)}, 笔={len(sub_kl_list.bi_list)}, 中枢={len(sub_kl_list.zs_list)}")

        # 提取子级别结果
        sub_name = _get_futures_name(code)
        sub_result = _extract_realtime_snapshot(
            sub_chan, _get_kl_type(sub_freq), code, sub_name,
            sub_freq, klines=None
        )
        result["sub"] = sub_result
        # 将 fx_a_raw_dt/fx_b_raw_dt（天勤K线开始时间）换算为子级别时间
        main_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 60)
        _futures_red_range(result, main_freq_sec, sub_freq_sec, sub_freq)
        print(f"[双窗口] 子级别({sub_freq})提取完成: K线={sub_result['meta']['kline_count']}, "
              f"笔={sub_result['meta']['bi_count']}, 中枢={sub_result['meta']['zs_count']}")

    return result


def _analyze_stock_internal(code, freq="d", end_date=None, start_time=None, cache_chan=True, dual=False, step=None):
    """
    使用通达信数据源 + chan.py 进行股票/指数缠论分析（内部实现，不处理期货分流）
    返回与 czsc 版本兼容的 JSON 数据结构
    end_date: 复盘截止日期，有值时以该日期为"最新行情"
    start_time: 选点起始时间，有值时只加载该时间之后的K线（不设数量限制）
    step: 箭头步进，在 full_records 中从 end_date 位置偏移 step 根K线作为新的截断日期
    cache_chan: 是否缓存CChan对象。扫描模式设为False以节省内存。
    """
    import time
    t_start = time.time()

    market, code = _get_stock_market_code(code.strip().upper())
    # print(f"[调试-_analyze_stock_internal] 解析后market={market}, code={code}, freq={freq}")
    if not market:
        return {"error": f"无法识别股票代码: {code}"}

    qualified_code = f"{code}.{market.upper()}"  # 区分沪市深市同号股票

    # 调试模式：冷启动注入截止日期（仅日K生效）
    if not end_date and DEBUG_COLD_START_END_DATE and freq == 'd':
        end_date = DEBUG_COLD_START_END_DATE
        print(f"[调试] 冷启动使用截止日期: {end_date}")

    # ===== 双窗口模式：独立缓存系统 =====
    # 双窗口与单窗口完全独立，各自拥有独立的 CChan 对象和缓存 key
    # 双窗口内部主级别和子级别也分开存储
    sub_freq = None  # 单窗口模式下为 None，双窗口模式下由 _SUB_FREQ_MAP 赋值
    if dual:
        sub_freq = _SUB_FREQ_MAP.get(freq)
        if not sub_freq:
            return {"error": f"双窗口不支持当前周期: {freq}"}
        # 缓存 key 约定：
        #   dual_main_{market}_{code}_{freq}_{date_suffix}  — 主级别缓存（含 CChan 对象）
        #   dual_sub_{market}_{code}_{sub_freq}_{date_suffix}  — 子级别缓存（独立存储）
        #   date_suffix = end_date（复盘）或 "live"（非复盘）
        date_suffix = end_date if end_date else "live"
        main_cache_key = f"dual_main_{market}_{code}_{freq}_{date_suffix}"
        sub_cache_key = f"dual_sub_{market}_{code}_{sub_freq}_{date_suffix}"
        cache_key = None  # 双窗口不使用单窗口的 cache_key，初始化为 None 防止意外引用

        # 查双窗口缓存（主级别和子级别必须同时存在，不会出现一个存在一个不存在）
        # 复盘模式(end_date)不命中缓存，强制重新加载
        main_cached = _cache_get(main_cache_key)
        sub_cached = _cache_get(sub_cache_key)
        if not end_date and main_cached is not None and sub_cached is not None \
                and "result" in main_cached and "result" in sub_cached:
            result = main_cached["result"]
            result["sub"] = sub_cached["result"]
            print(f"[耗时] 命中双窗口缓存(freq={freq}+{sub_freq})，总耗时: 0.001s")
            return result

        # 复盘模式：清除旧的双窗口缓存，强制重新加载主级别和子级别
        if end_date:
            with _cache_lock:
                if main_cache_key in _stocks_analysis_cache:
                    del _stocks_analysis_cache[main_cache_key]
                if sub_cache_key in _stocks_analysis_cache:
                    del _stocks_analysis_cache[sub_cache_key]
            gc.collect()
            print(f"[信息] 复盘模式：已清除双窗口缓存，重新加载主级别({freq})和子级别({sub_freq})")

        # 未命中缓存：冷启动从文件加载双级别数据，cached_result=None 强制走文件读取
        cached_result = None
    else:
        # ===== 单窗口模式 =====
        date_suffix = end_date if end_date else "live"
        cache_key = f"single_{market}_{code}_{freq}_{date_suffix}"
        cached_result = _cache_get(cache_key)
        if not end_date and cached_result is not None and "result" in cached_result:
            result = cached_result["result"]
            col = FREQ_TO_COL.get(freq, "")
            if col and qualified_code in _saved_point_times:
                saved_sdt = _saved_point_times[qualified_code].get(col, "").strip() or None
                if saved_sdt:
                    cached_saved = result.get("meta", {}).get("saved_selection_date", "")
                    if cached_saved != saved_sdt:
                        print(f"[信息] 缓存选点({cached_saved})与CSV({saved_sdt})不一致，跳过缓存")
                    else:
                        print(f"[耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                        return result
                else:
                    print(f"[耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                    return result
            else:
                print(f"[耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
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
            full_records, sub_records, forward_adjust_done = read_main_level_records(market, code, freq, return_raw=True, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            print(f"[耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            print(f"[信息] 子级别({sub_freq})数据加载: {len(sub_records)}条 (复用前复权)")
        elif freq == 'w' and sub_freq == 'd':
            # 优化：w+d 共用同一次日线文件读取和前复权，避免重复读取和二次复权
            full_records, sub_records, forward_adjust_done = read_main_level_records(market, code, freq, return_raw=True, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            print(f"[耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            print(f"[信息] 子级别({sub_freq})数据加载: {len(sub_records)}条 (复用前复权)")
        else:
            full_records, forward_adjust_done = read_main_level_records(market, code, freq, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            print(f"[耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            sub_records = read_sub_level_records(market, code, freq, sub_freq, full_records, end_date=target_dt)
        if sub_records is None or len(sub_records) < 5:
            print(f"[警告] 子级别数据不足，退化为单级别模式")
            sub_freq = None
    else:
        # ────────────────────────────────────
        # 单窗口分支：只加载主级别数据
        # ────────────────────────────────────
        if not end_date and cached_result is not None and "records" in cached_result:
            full_records = cached_result["records"]
            forward_adjust_done = cached_result.get("result", {}).get("meta", {}).get("forward_adjust", False)
            print(f"[耗时] 从缓存获取K线: {len(full_records)}条")
        else:
            t0 = time.time()
            full_records, forward_adjust_done = read_main_level_records(market, code, freq, end_date=target_dt)
            if len(full_records) < 5:
                print(f"[调试-K线不足] code={code}, market={market}, freq={freq}, target_dt={target_dt}, records={len(full_records)}")
                return {"error": f"K线数据不足: 仅{len(full_records)}条"}
            print(f"[耗时] 读取数据文件: {time.time()-t0:.3f}s, {len(full_records)}条K线")

    # 调试模式：数据加载后立即截断起始日期（所有周期生效），后续流程对此无感知
    # 等于在数据源层面"只加载了指定日期之后的数据"
    if DEBUG_COLD_START_START_DATE:
        try:
            start_cutoff = datetime.strptime(DEBUG_COLD_START_START_DATE, "%Y-%m-%d")
            before = len(full_records)
            filtered = [r for r in full_records if r["dt"] >= start_cutoff]
            if filtered:
                full_records = filtered
                print(f"[调试] 起始日期截断({DEBUG_COLD_START_START_DATE}): {before}条 -> {len(full_records)}条")
            else:
                print(f"[调试] 起始日期 {DEBUG_COLD_START_START_DATE} 之前无数据，保留全部{before}条")
        except ValueError:
            print(f"[警告] DEBUG_COLD_START_START_DATE 格式错误: {DEBUG_COLD_START_START_DATE}，应为 YYYY-MM-DD")

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
                        print(f"[箭头] step={step}: {full_records[anchor_idx]['dt']} → {target_dt} (idx {anchor_idx} → {new_idx})")
                    else:
                        print(f"[箭头] step={step} 越界: idx {anchor_idx} → {new_idx}, 共{len(full_records)}条")

    if end_date:
        # 复盘模式：右边界 = target_dt，左边界与冷启动一致
        records = [r for r in full_records if r["dt"] <= target_dt]
        if len(records) < 5:
            return {"error": f"截断后K线数据不足: 仅{len(records)}条，请选择更晚的日期"}

        # 读取CSV保存的选点，如果选点日期 ≤ 复盘日期，则左边界 = 选点
        if start_time is None:
            col = FREQ_TO_COL.get(freq, "")
            if col and qualified_code in _saved_point_times:
                _saved = _saved_point_times[qualified_code].get(col, "").strip() or None
                if _saved:
                    start_time = _saved

        from datetime import timedelta
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
                print(f"[信息] 复盘选点: 从选点时间 {start_time} 开始，筛选后 {before_count}条 -> {len(records)}条")
        else:
            # 无选点：与冷启动一致，对30分/5分做时间截断，日K/周K不截断
            if not FULL_DATA_MODE and len(records) > 0 and freq in TIME_TRUNCATE_CONFIG:
                trunc_days, trunc_text = TIME_TRUNCATE_CONFIG[freq]
                cutoff = target_dt - timedelta(days=trunc_days)
                before_count = len(records)
                records = [r for r in records if r["dt"] >= cutoff]
                if before_count != len(records):
                    print(f"[信息] 复盘截断(freq={freq}): 从{target_dt.strftime('%Y-%m-%d')}往前推{trunc_text}, "
                          f"{before_count}条 -> {len(records)}条")

        if len(records) < 5:
            return {"error": f"截断后K线数据不足: 仅{len(records)}条，请选择更晚的日期"}
        print(f"[信息] 复盘范围(freq={freq}) {records[0]['dt'].strftime('%Y-%m-%d')} ~ {records[-1]['dt'].strftime('%Y-%m-%d')}, "
              f"全量{len(full_records)}条 -> {len(records)}条")
    else:
        records = full_records
        # 确定起始时间：优先使用传入的start_time，其次使用CSV保存的选点
        if start_time is None:
            col = FREQ_TO_COL.get(freq, "")
            if col and qualified_code in _saved_point_times:
                _saved = _saved_point_times[qualified_code].get(col, "").strip() or None
                if _saved:
                    start_time = _saved

        if start_time is not None:
            # 从选点时间开始过滤，不做数量限制
            from datetime import timedelta
            start_dt = None
            for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
                try:
                    start_dt = datetime.strptime(start_time, fmt)
                    break
                except ValueError:
                    continue
            if start_dt is not None:
                records = [r for r in records if r["dt"] >= start_dt]
                print(f"[信息] 从选点时间 {start_time} 开始，筛选后 {len(records)} 条K线")
        else:
            # 冷启动无选点：默认模式下对30分和5分做时间截断（全量模式跳过）
            if not FULL_DATA_MODE and len(records) > 0 and freq in TIME_TRUNCATE_CONFIG:
                from datetime import timedelta
                latest_dt = records[-1]["dt"]
                trunc_days, trunc_text = TIME_TRUNCATE_CONFIG[freq]
                cutoff = latest_dt - timedelta(days=trunc_days)
                before_count = len(records)
                records = [r for r in records if r["dt"] >= cutoff]
                if before_count != len(records):
                    print(f"[信息] 按时间范围截取(freq={freq}): 从{latest_dt.strftime('%Y-%m-%d')}往前推{trunc_text}, "
                          f"{before_count}条 -> {len(records)}条")

    # 双窗口：子级别数据同步截断到主级别时间范围
    # 避免 chan.py 分析不必要的全量子级别数据（如 30m+5m 时 5m 有 25152 条）
    # 下窗起始 = max(上窗起始, 下窗选点)
    if dual and sub_freq and sub_records is not None and len(records) > 0:
        from datetime import timedelta
        main_start = records[0]["dt"]
        main_end = records[-1]["dt"]
        # 读取下窗周期的选点，如果晚于上窗起始则使用选点
        sub_col = FREQ_TO_COL.get(sub_freq, "")
        if sub_col and qualified_code in _saved_point_times:
            sub_saved = _saved_point_times[qualified_code].get(sub_col, "").strip() or None
            if sub_saved:
                sub_saved_dt = None
                for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
                    try:
                        sub_saved_dt = datetime.strptime(sub_saved, fmt)
                        break
                    except ValueError:
                        continue
                if sub_saved_dt is not None and sub_saved_dt > main_start:
                    print(f"[信息] 子级别({sub_freq})选点晚于上窗起始: {sub_saved} > {main_start.strftime('%Y/%m/%d')}, 使用选点")
                    main_start = sub_saved_dt
        sub_before = len(sub_records)
        sub_records = [r for r in sub_records if main_start - timedelta(days=1) <= r["dt"] <= main_end + timedelta(days=1)]
        if sub_before != len(sub_records):
            print(f"[信息] 子级别({sub_freq})同步截断: {sub_before}条 -> {len(sub_records)}条")

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
            if dual and sub_freq:
                # 双窗口：注入主级别和子级别数据
                lv_list = _DUAL_LV_LIST[freq]
                CTdxAPI.set_data({
                    _get_kl_type(freq): records,
                    _get_kl_type(sub_freq): sub_records,
                })
                config.kl_data_check = False
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
            print(f"[错误] chan.py 分析失败: code={chan_code} freq={freq}{records_info}")
            print(f"[错误] 异常类型: {type(e).__name__}, 异常信息: {e}")
            print(f"[错误] 完整堆栈:\n{tb}")
            return {"error": f"chan.py 分析失败: {type(e).__name__}: {e}"}
        finally:
            if end_date:
                CMyBSPointList.REPLAY_MODE = False

    print(f"[耗时] chan.py 缠论分析: {time.time()-t0:.3f}s")

    # 4. 提取主级别结果
    result = _extract_main_level_data(chan, freq, records, market, code, 
                                       dual=dual, sub_freq=sub_freq,
                                       qualified_code=qualified_code, 
                                       end_date=end_date,
                                       forward_adjust_done=forward_adjust_done)

    # 双窗口模式：提取子级别数据
    sub_result = None
    if dual and sub_freq:
        print(f"[调试] 双窗口模式: dual={dual}, sub_freq={sub_freq}, chan类型={type(chan).__name__}")
        try:
            sub_result = _extract_sub_level_data(chan, sub_freq, code, market)
        except Exception as e:
            import traceback
            print(f"[错误] 提取子级别数据失败: {type(e).__name__}: {e}")
            print(f"[错误] 堆栈:\n{traceback.format_exc()}")

    if sub_result:
        result["sub"] = sub_result

    mode_str = f" [复盘到 {end_date}]" if end_date else ""
    print(f"[信息] 查询 {code}.{market.upper()} 完成{mode_str}: {result['meta']['kline_count']}条K线, {result['meta']['bi_count']}笔, {result['meta']['fx_count']}分型, {result['meta']['zs_count']}中枢, {result['meta']['seg_count']}线段, {result['meta']['bsp_count']}买卖点")
    print(f"[耗时] 总耗时: {time.time()-t_start:.3f}s")

    # 缓存策略：
    # - 单窗口：缓存到 single_{market}_{code}_{freq}
    # - 双窗口：主级别缓存到 dual_main_{market}_{code}_{freq}，子级别缓存到 dual_sub_{market}_{code}_{sub_freq}
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
        print(f"[耗时]   复盘后GC回收: {time.time()-t_gc:.3f}s")

    return result


# ============================================================
# 提取主级别和子级别数据
# ============================================================
def _extract_main_level_data(chan, freq, records, market, code, dual=False, sub_freq=None,
                              qualified_code="", end_date=None, forward_adjust_done=False):
    """
    从 CChan 中提取主级别的 K线、笔、分型、中枢、线段、买卖点数据。
    返回与 czsc 版本兼容的 JSON 数据结构（不含 sub 字段）。
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

        fx_a_sub_dt, fx_b_sub_dt = _stocks_red_range(a_klu, b_klu, sub_freq, bi) if dual and sub_freq else ("", "")

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
        print(f"[调试] 获取买卖点失败: {e}")

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

    print(f"[耗时] 分析结果转JSON(K线/分型/笔/线段/中枢/买卖点）: {time.time()-t0:.3f}s")

    # 获取当前周期的保存选点日期
    _col_meta = FREQ_TO_COL.get(freq, "")
    _saved_sdt_for_meta = ""
    if not end_date and _col_meta and qualified_code in _saved_point_times:
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

    print(f"[耗时] 主级别提取结果: {time.time()-t0:.3f}s (K线={len(kline_data)} 笔={len(bi_data)} 中枢={len(zs_data)} 线段={len(seg_data)} 买卖点={len(bsp_data)})")

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
        print(f"[调试] 子级别获取买卖点失败: {e}")

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

    print(f"[耗时] 子级别({sub_freq})提取: {time.time()-t_start:.3f}s (K线={len(kline_data)} 笔={len(bi_data)} 分型={len(fx_data)} 中枢={len(zs_data)} 线段={len(seg_data)} 买卖点={len(bsp_data)})")
    return sub_result


# ============================================================
# 区间套背驰判断
# ============================================================
# 高级别→低级别周期映射（与双窗口 getDualSubFreq 一致）
_SUB_FREQ_MAP = {'w': 'd', 'd': '30m', '30m': '5m'}

# 期货双窗口周期映射：上窗周期 → 下窗周期
_FUTURES_DUAL_FREQ_MAP = {
    "30m": "5m",
    "5m": "1m",
    "1m": "15s",
}

# 期货分析缓存（供 /api/red_range_zs 等访问）
# key: "symbol:freq"  (如 "KQ.m@CFFEX.IM:5m")，当前 value: CChan 对象
# 后续可扩展为 {records, chan, result} 三元组，key可加前缀区分（single_/dual_main_/dual_sub_）
# （阶段 4：实现与状态收敛 App/AppData.py；此处别名共享同一对象）
_futures_analysis_cache = app_data.futures_analysis_cache


def compute_red_range_zs(code, sub_freq='d', left_date='', right_date='', end_date=None):
    """
    双窗口红框中枢计算：前端传来红框的左右边界时间 [left_date, right_date]，
    后端内部调用 _red_range_bi_sequence 找到被红框完全覆盖的子级别笔，再
    用 _red_range_amp 重新计算中枢，返回给前端绘制。

    参数:
        code:       股票代码（如 "SH000001" 或 "000001.SH"）
        sub_freq:   子级别周期（如 "30m", "5m"）
        left_date:  红框左边界时间字符串（子级别K线格式，如 "2025-06-15 10:00"）
        right_date: 红框右边界时间字符串
        end_date:   复盘日期（None=实时模式，有值=复盘模式），用于精确匹配缓存 key

    返回: {"zs": [...]} 或 {"error": "..."}
    """
    import re
    normalized_code = code.strip().upper()

    # ── 期货双窗口 ──
    if normalized_code.startswith("KQ."):
        cache_key = f"{normalized_code}:{sub_freq}"
        cached = _futures_analysis_cache.get(cache_key)
        if cached is None:
            return {"error": "双窗口下窗缓存已过期，请重新打开双窗口"}
        chan = cached
        kl_list = chan[_get_kl_type(sub_freq)]
        bi_list = kl_list.bi_list
        date_fmt = _get_date_fmt(sub_freq)
        start_bi, end_bi = _red_range_bi_sequence(left_date, right_date, bi_list, sub_freq)
        if start_bi is None:
            return {"error": f"红框内无完整笔: [{left_date}, {right_date}]"}
        sliced_bis = bi_list[start_bi:end_bi + 1]
        zs_data = _red_range_amp(sliced_bis, bi_list, date_fmt)
        return {"zs": zs_data, "start_bi": start_bi, "end_bi": end_bi}

    # ── 股票双窗口 ──
    market = None
    prefix_match = re.match(r'^(SH|SZ|HK|DS)(\d+)$', normalized_code)
    suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK|DS)$', normalized_code)
    if prefix_match:
        market = prefix_match.group(1).lower()
        normalized_code = prefix_match.group(2)
    elif suffix_match:
        normalized_code = suffix_match.group(1)
        market = suffix_match.group(2).lower()

    if not market:
        return {"error": f"无法识别股票代码: {code}"}

    date_suffix = end_date if end_date else "live"
    cache_key = f"single_{market}_{normalized_code}_{sub_freq}_{date_suffix}"
    cached = _cache_get(cache_key)

    # 双窗口新模式：当前 sub_freq 通常是下面窗口频率，优先从 dual_main 主级别缓存中的多级别 CChan 取子级别笔列表。
    # dual_sub 缓存只存 result/records，不存 chan；真正可用于重算中枢的 CChan 在 dual_main 缓存里。
    if (cached is None or "chan" not in cached) and sub_freq in _SUB_FREQ_MAP.values():
        for main_freq, _sub in _SUB_FREQ_MAP.items():
            if _sub == sub_freq:
                dual_main_cache_key = f"dual_main_{market}_{normalized_code}_{main_freq}_{date_suffix}"
                main_cached = _cache_get(dual_main_cache_key)
                if main_cached and "chan" in main_cached:
                    main_chan = main_cached["chan"]
                    try:
                        _ = main_chan[_get_kl_type(sub_freq)]
                        cached = {"chan": main_chan}
                        break
                    except Exception as e:
                        print(f"[警告] 异常: {type(e).__name__}: {e}")
                if cached is None or "chan" not in cached:
                    single_main_cache_key = f"single_{market}_{normalized_code}_{main_freq}_{date_suffix}"
                    main_cached = _cache_get(single_main_cache_key)
                    if main_cached and "chan" in main_cached:
                        main_chan = main_cached["chan"]
                        try:
                            _ = main_chan[_get_kl_type(sub_freq)]
                            cached = {"chan": main_chan}
                            print(f"[信息] compute_red_range_zs 从单窗口主级别缓存({main_freq})获取子级别({sub_freq})数据")
                            break
                        except Exception as e:
                            print(f"[警告] 异常: {type(e).__name__}: {e}")

    if cached is None:
        return {"error": "请先在该周期下加载K线数据"}
    if "chan" not in cached:
        print(f"[信息] 缓存中无chan对象，重新分析 {normalized_code} {sub_freq}")
        analyze_stock(f"{normalized_code}.{market.upper()}", freq=sub_freq, cache_chan=True)
        cached = _cache_get(cache_key)
        if cached is None or "chan" not in cached:
            return {"error": "缓存中无分析数据，请重新查询"}

    chan = cached["chan"]
    kl_list = chan[_get_kl_type(sub_freq)]
    bi_list = kl_list.bi_list

    date_fmt = _get_date_fmt(sub_freq)

    # ── 步骤③：后端找被红框完全覆盖的笔 ──
    start_bi, end_bi = _red_range_bi_sequence(left_date, right_date, bi_list, sub_freq)
    if start_bi is None:
        return {"error": f"红框内无完整笔: [{left_date}, {right_date}]"}

    sliced_bis = bi_list[start_bi:end_bi + 1]
    zs_data = _red_range_amp(sliced_bis, bi_list, date_fmt)
    return {"zs": zs_data, "start_bi": start_bi, "end_bi": end_bi}


def stock_manual_select_point(code, freq='d', bi_idx=-1):
    """
    手选进入段：找到左肩原始K线时间T，销毁旧CChan实例及所有中间状态，
    从T重新加载K线数据并创建全新CChan实例，返回完整chartData给前端渲染。

    流程：
    1. 通过前端传来的笔索引，找到分型左肩第一根原始K线时间T
    2. 保存T到CSV
    3. 销毁旧CChanA及_stocks_analysis_cache中的全部中间状态，回收内存
    4. 从T开始重新读取通达信K线，创建CChanB，返回完整结果
    """
    # 标准化代码
    normalized_code = code.strip().upper()
    market = None
    prefix_match = re.match(r'^(SH|SZ|HK|DS)(\d+)$', normalized_code)
    suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK|DS)$', normalized_code)
    if prefix_match:
        market = prefix_match.group(1).lower()
        normalized_code = prefix_match.group(2)
    elif suffix_match:
        normalized_code = suffix_match.group(1)
        market = suffix_match.group(2).lower()
    date_suffix = "live"
    cache_key = f"single_{market}_{normalized_code}_{freq}_{date_suffix}"
    qualified_code = f"{normalized_code}.{market.upper()}"  # 区分沪市深市同号股票
    cached = _cache_get(cache_key)
    if cached is None:
        return {"error": "请先查询该股票"}

    if "chan" not in cached:
        # 扫描缓存只有result没有chan，重新分析以获取完整数据
        print(f"[信息] 缓存中无chan对象，重新分析 {normalized_code} {freq}")
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
    _save_point_time(qualified_code, stock_name, freq, start_time)
    if qualified_code not in _saved_point_times:
        _saved_point_times[qualified_code] = {}
    _saved_point_times[qualified_code]["name"] = stock_name
    _saved_point_times[qualified_code][FREQ_TO_COL.get(freq, "")] = start_time

    # Step 3: 销毁旧CChanA及所有中间状态，回到冷启动前的干净状态
    if cache_key in _stocks_analysis_cache:
        with _cache_lock:
            if cache_key in _stocks_analysis_cache:
                del _stocks_analysis_cache[cache_key]
    gc.collect()

    # Step 4: 从T开始重新加载K线，创建CChanB，返回完整chartData
    result = _analyze_stock_internal(f"{normalized_code}.{market.upper()}", freq=freq, start_time=start_time)
    return result


def futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """
    期货期指手选进入段：与股票 stock_manual_select_point 逻辑一致。
    创建临时 TqApi → 拉取全量历史 → 找到左肩时间T → 保存CSV →
    创建新 TqApi → 从T重新拉取 → 创建新CChan → 返回完整快照。
    """
    import time
    from tqsdk import TqApi, TqAuth
    from DataAPI.TqSdkAPI import CTqSdkAPI, TQ_ACCOUNT, TQ_PASSWORD

    # 别名解析
    symbol_upper = symbol.upper()
    if symbol_upper in CTqSdkAPI.FUTURES_ALIASES:
        symbol = CTqSdkAPI.FUTURES_ALIASES[symbol_upper]

    freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 15)
    freq_label = freq
    freq_cn = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{freq_cn}"
    target_bi_idx = int(bi_idx)

    api = None
    api2 = None
    try:
        t_conn = time.time()
        api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
        print(f"[{display_key}] ⓪ 临时连接天勤(选点): 耗时 {time.time()-t_conn:.1f}s")

        records = CTqSdkAPI.fetch_kline(api, symbol, freq_sec=freq_sec, display_key=display_key)
        if len(records) < 5:
            return {"error": f"K线数据不足: 仅{len(records)}条"}

        # 注入数据源 + 创建 CChan
        CTqSdkAPI.set_data(records, symbol=f"{symbol}:{freq_sec}")

        from Common.CEnum import KL_TYPE, AUTYPE
        from Chan import CChan

        _freq_to_kl = {
            15: KL_TYPE.K_15S, 30: KL_TYPE.K_30S, 60: KL_TYPE.K_1M,
            300: KL_TYPE.K_5M, 900: KL_TYPE.K_15M, 1800: KL_TYPE.K_30M,
            3600: KL_TYPE.K_60M, 86400: KL_TYPE.K_DAY,
            604800: KL_TYPE.K_WEEK, 2592000: KL_TYPE.K_MON,
        }
        kl_type = _freq_to_kl.get(freq_sec, KL_TYPE.K_15S)

        config = _make_chan_config()

        chan = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
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
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
            api = None

        t_conn2 = time.time()
        api2 = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
        print(f"[{display_key}] ⓪ 重新连接天勤(选点后): 耗时 {time.time()-t_conn2:.1f}s")

        records2 = CTqSdkAPI.fetch_kline(api2, symbol, freq_sec=freq_sec,
                                         display_key=display_key, start_time=start_time)
        if len(records2) < 5:
            return {"error": f"选点后K线数据不足: 仅{len(records2)}条"}

        # 注入数据源 + 创建新 CChan
        CTqSdkAPI.set_data(records2, symbol=f"{symbol}:{freq_sec}")

        chan2 = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan2.step_load():
            pass

        # Step 5: 提取快照并返回
        result = _extract_realtime_snapshot(chan2, kl_type, symbol, name, freq_label,
                                            saved_selection_date=start_time)
        # 计算白色横虚线
        _kl_list = chan2[kl_type]
        _date_fmt = _get_date_fmt(freq)
        result['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _date_fmt)
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
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
        if api2 is not None:
            try:
                api2.close()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")


def analyze_stock(code, freq="d", end_date=None, cache_chan=True, dual=False, step=None, sub_freq=None):
    """公开分析入口：先识别市场，再分流到股票或期货的并列内部流程。
    股票/指数：走通达信数据源，支持 cache_chan 和 dual 双窗口。
    期货/期指：走天勤数据源，dual=True 时使用两个独立 CChan 对象。
    sub_freq: 双窗口下窗周期，仅期货路径使用（None 时用默认映射）。
    """
    market, normalized_code = _get_market_code(code)
    # print(f"[调试-analyze_stock] 输入code={code}, 识别market={market}, normalized_code={normalized_code}")
    if not market:
        return {"error": f"无法识别股票代码: {code}"}
    if market == 'futures':
        return _analyze_futures_internal(normalized_code, freq=freq, end_date=end_date, dual=dual, step=step, sub_freq=sub_freq)
    stock_code = f"{normalized_code}.{market.upper()}"
    return _analyze_stock_internal(stock_code, freq=freq, end_date=end_date, cache_chan=cache_chan, dual=dual, step=step)


# ============================================================
# 扫描预过滤（ST + 流通市值）
# ============================================================
def _quick_prefilter_pass(market, code):
    """
    快速预过滤：检查ST/*ST/退市、流通市值条件。
    用于中证1000等大范围扫描时提前跳过不满足条件的股票。
    返回 (pass_filter, float_mc, skip_reason)：
      - pass_filter=True 表示通过过滤，可以继续分析
      - pass_filter=False 表示应跳过
      - skip_reason 为跳过原因字符串（如 "ST" / "流通市值<50亿"）
    """
    try:
        # 1. 过滤 ST/*ST/退市股票（通过名称缓存判断）
        try:
            compound_key = ("sh" if market == "1" else "sz" if market == "0" else "bj") + code
            info = _stock_names_cache.get(compound_key, {})
            name = info.get("name", "") if isinstance(info, dict) else str(info) if info else ""
            if name and (name.startswith("*ST") or name.startswith("ST") or "退" in name):
                return (False, None, "ST")
        except Exception:
            pass  # 名称查找失败不跳过

        # 2. 流通市值过滤：从缓存获取（阶段一已确保缓存有数据）
        float_mc = _get_float_mc_from_cache(code)
        if float_mc is not None:
            if float_mc < 50:
                return (False, float_mc, "流通市值<50亿")
        else:
            print(f"[预过滤] {code} 流通市值未知")

        return (True, float_mc, None)
    except Exception as e:
        import traceback
        print(f"[预过滤] {code} 异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        return (True, None, None)


def _debug_read_page_index_stocks(sector_code):
    """获取当前页面指数的成分股"""
    if not sector_code:
        return []
    return get_index_stocks(sector_code)


def read_tdxhy_l2_indices():
    """返回所有二级行业板块指数列表（X+4位代码对应的881yyy），共125个
    （阶段 5 兼容壳 → app_data.tdxhy_l2_indices；映射读取随数据文件迁 App/，设计 8.8）"""
    return app_data.tdxhy_l2_indices()


def read_tdxhy_l3_indices():
    """返回所有三级行业板块指数列表（X+6位代码对应的881yyy），共315个
    （阶段 5 兼容壳 → app_data.tdxhy_l3_indices；同上）"""
    return app_data.tdxhy_l3_indices()


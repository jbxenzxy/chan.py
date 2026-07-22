"""
缠论分析 - chan.py 版本
基于 https://github.com/Vespa314/chan.py 实现
功能：读取通达信本地K线数据，进行缠论分析，生成K线图网页
"""

import sys
import os
import json
import time
import re
import threading
import multiprocessing
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
from urllib.parse import urlparse, parse_qs

# 区间套辅助函数（已搬迁至 BSPointList.py，红框功能复用）
from BuySellPoint.BSPointList import _main_bi_range, _stocks_red_range, _futures_red_range, _red_range_bi_sequence, _red_range_amp

# ============================================================
# 配置区域 - 请根据你的实际环境修改
# ============================================================
VIPDOC_DIR = r"C:\new_tdx_test\vipdoc"  # 通达信vipdoc目录
TDX_HQ_CACHE = r"C:\new_tdx_test\T0002\hq_cache"  # 通达信hq_cache目录（shm.tnf/szm.tnf）
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))  # 输出目录（脚本所在目录）
SYMBOL_CODE = "SH000001"  # 默认股票代码（上证指数）
CHAN_PATH = r"C:\my_chan_project"  # chan.py 仓库解压目录
_LAST_CODE_FREQ_FILE = os.path.join(VIPDOC_DIR, "last_code_freq.json")  # 持久化上次查看的代码和周期

# ============================================================
# 天勤期货/期指行情配置
# ============================================================
# 账户和密码从 C:\new_tdx_test\vipdoc\tq_account.json 文件读取
# 文件格式: {"account": "手机号或用户名", "password": "密码"}
_SSE_DEBUG  = False         # SSE 推送详细调试日志开关（设为 True 可恢复调试输出）

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
    from Common.CEnum import AUTYPE, KL_TYPE, FX_TYPE
except ImportError as e:
    print(f"\n[错误] chan.py 导入失败: {e}")
    print(f"[提示] 请确保 CHAN_PATH = r'{CHAN_PATH}' 指向正确的 chan.py 仓库目录")
    sys.exit(1)

# 导入通达信数据源适配器（从 chan.py 的 DataAPI 目录）
# 包含：K线读取、前复权
from DataAPI.TdxAPI import CTdxAPI, \
    read_main_level_records, read_sub_level_records, \
    read_zxg_stocks, read_zz1000_stocks, save_to_zxg_blk, \
    read_sz50_stocks, read_hs300_stocks, read_zz500_stocks, \
    get_index_stocks, refresh_block_files

# 前复权开关：True=开启前复权（消除分红送股的跳空缺口），False=关闭（不复权，原样输出）
FORWARD_ADJUST_ENABLED = True

# 调试模式：冷启动只从指定日期开始加载(所有周期有效)，None表示不开启。如果该日期前无通达信数据，则有多少加载多少
DEBUG_COLD_START_START_DATE = None # "2024-09-10"

# 调试模式：用于解决冷启动起不来的问题；冷启动加载到此日期(仅日K生效)，None表示不开启
DEBUG_COLD_START_END_DATE = None # 示例: "2026-06-29" 北方国际

# 注入通达信数据源配置到 TdxAPI 模块
from DataAPI.TdxAPI import set_tdx_config as _set_tdx_config
_set_tdx_config(
    vipdoc_dir=VIPDOC_DIR,
    forward_adjust_enabled=FORWARD_ADJUST_ENABLED,
)

# 全量数据模式：True=加载全部K线不做时间截断；False=默认模式
FULL_DATA_MODE = False

# 时间截断配置（仅 FULL_DATA_MODE=False 时生效）：{周期: (天数, 显示文本)}
TIME_TRUNCATE_CONFIG = {
    '30m': (180, "6个月"),
    '5m': (90, "3个月"),
}

# 导入天勤数据源适配器（期货/期指）
try:
    from DataAPI.TqSdkAPI import CTqSdkAPI, fetch_futures_kline, FREQ_SEC_MAP, FUTURES_ALIASES, \
        _get_futures_code, _get_futures_name, load_tq_account
    load_tq_account(VIPDOC_DIR)
    TQ_AVAILABLE = True
except ImportError as e:
    CTqSdkAPI = None
    FUTURES_ALIASES = {}
    TQ_AVAILABLE = False
    print(f"[警告] 天勤数据源未安装: {e}，期货功能不可用。pip install tqsdk")


# ============================================================
# 同花顺自选股同步（云端 API）
# ============================================================
try:
    from ths_cloud_api import save_scan_to_ths_cloud
    _THS_CLOUD_AVAILABLE = True
except ImportError:
    _THS_CLOUD_AVAILABLE = False


# ============================================================
# 通达信数据读取
# ============================================================





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
# 获取股票名称
# ============================================================
def _get_stock_name(market, code):
    """获取股票名称。从本地缓存文件读取，缓存不存在则返回None。
    港股5位代码（如00700）和A股6位代码（如000700）是不同证券，绝不互相回退。
    """
    if market == "ds" and code == "932000":
        return "中证2000"
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


# 股票名称缓存：从通达信行情服务器批量获取后保存到本地JSON
# key: 股票代码(6位), value: {"name": "股票名称", "pinyin": "拼音首字母"}
_stock_names_cache = {}
_stock_names_loaded = False
_STOCK_NAMES_CACHE_FILE = os.path.join(VIPDOC_DIR, "stock_names.json")
_STOCK_PE_TTM_FILE = os.path.join(VIPDOC_DIR, "stock_pettm.json")

# 刷新状态（股票名称刷新用）
_refresh_status = {"running": False, "progress": 0, "total": 0, "loaded": 0, "error": None, "step": ""}



def _load_stock_names_from_cache_file():
    """
    从 stock_names.json 缓存文件加载股票名称到内存。
    返回加载的记录数，文件不存在则返回0。
    版本迁移：自动将旧版纯数字键转换为 market+code 复合键。
    """
    global _stock_names_loaded
    if _stock_names_loaded:
        return len(_stock_names_cache)
    if not os.path.exists(_STOCK_NAMES_CACHE_FILE):
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
            print(f"[信息] 从缓存文件加载股票名称: {len(_stock_names_cache)} 只")
            return len(_stock_names_cache)
    except Exception as e:
        print(f"[警告] 读取股票名称缓存失败: {e}")
    return 0




def _safe_write_json_file(path, data, *, ensure_ascii=False, indent=None):
    """先写临时文件并校验 JSON 可读，再用 os.replace 覆盖正式文件；失败时保留旧文件。"""
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
        with open(tmp_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, type(data)):
            raise ValueError("临时 JSON 文件类型校验失败")
        os.replace(tmp_path, path)
        return True
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ============================================================
# PE-TTM 缓存（腾讯接口，增量刷新）
# ============================================================
_pe_ttm_cache = {}       # {code: float}  纯数字代码 → PE-TTM值
_pe_ttm_loaded = False

# 指数归属缓存（AKShare在线获取，与PE-TTM一起保存到stock_pettm.json）
# key: market+code（如 "sh600519"）, value: "沪深300"|"中证500"|"中证1000"
_index_belong_cache = {}
_index_belong_loaded = False


def _load_pe_ttm_cache():
    """从 stock_pettm.json 加载 PE-TTM 和指数归属缓存到内存。文件不存在则返回空。
    向后兼容旧格式 {"sh600519": 25.3}，新格式为 {"sh600519": {"pe_ttm": 25.3, "index": "沪深300"}}"""
    global _pe_ttm_cache, _pe_ttm_loaded, _index_belong_cache, _index_belong_loaded
    if _pe_ttm_loaded:
        return _pe_ttm_cache
    _pe_ttm_loaded = True
    _index_belong_loaded = True
    if not os.path.exists(_STOCK_PE_TTM_FILE):
        return _pe_ttm_cache
    try:
        with open(_STOCK_PE_TTM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            pe_count = 0
            idx_count = 0
            for k, v in data.items():
                if isinstance(v, dict):
                    # 新格式：{"pe_ttm": float, "index": str}
                    pe_val = v.get("pe_ttm")
                    idx_val = v.get("index")
                    if isinstance(pe_val, (int, float)) and pe_val != 0:
                        _pe_ttm_cache[k] = pe_val
                        pe_count += 1
                    if isinstance(idx_val, str) and idx_val:
                        _index_belong_cache[k] = idx_val
                        idx_count += 1
                elif isinstance(v, (int, float)) and v != 0:
                    # 旧格式：直接是数字
                    _pe_ttm_cache[k] = v
                    pe_count += 1
        print(f"[PE-TTM] 从缓存加载 PE-TTM {pe_count} 条, 指数归属 {idx_count} 条")
    except Exception as e:
        print(f"[PE-TTM] 加载缓存失败: {e}")
    return _pe_ttm_cache


def _get_pe_ttm(market, code):
    """获取单只股票的 PE-TTM 值，未缓存则返回 None。key 为 market+code 避免沪市深市同号冲突。"""
    _load_pe_ttm_cache()
    return _pe_ttm_cache.get(market + code)


def _get_index_belong(market, code):
    """获取单只股票的指数归属（沪深300/中证500/中证1000），未缓存则返回 None。"""
    _load_pe_ttm_cache()
    return _index_belong_cache.get(market + code)


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
}


def _fetch_index_belong_from_akshare(timeout=30):
    """
    通过 AKShare index_stock_cons_csindex 接口在线获取沪深300/中证500/中证1000 最新成分股，
    构建 stock→指数归属 反向映射。返回 {market+code: "沪深300"|"中证500"|"中证1000"}。
    如果 AKShare 不可用或网络异常，返回空字典。每个指数单独设置超时。
    """
    global _index_belong_cache
    try:
        import akshare as ak
    except ImportError:
        print("[指数归属] akshare 未安装，跳过在线获取（pip install akshare）")
        return _index_belong_cache

    def _fetch_one(index_code, index_name):
        try:
            _refresh_status["step"] = f"刷新指数归属: {index_name}..."
            print(f"[指数归属] 开始获取 {index_name}({index_code})...")
            df = ak.index_stock_cons_csindex(symbol=index_code)
            count = 0
            for _, row in df.iterrows():
                stock_code = str(row["成分券代码"]).zfill(6)
                exchange = str(row["交易所"])
                mkt = _AKSHARE_EXCHANGE_MAP.get(exchange, "")
                if mkt and stock_code.isdigit() and len(stock_code) == 6:
                    result[mkt + stock_code] = index_name
                    count += 1
            print(f"[指数归属] {index_name}({index_code}): 获取 {count} 只成分股")
        except Exception as e:
            print(f"[指数归属] {index_name}({index_code}) 获取失败: {e}")

    import concurrent.futures
    result = {}
    for index_code, index_name in _AKSHARE_INDEX_MAP.items():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch_one, index_code, index_name)
            try:
                future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                print(f"[指数归属] {index_name}({index_code}) 获取超时({timeout}s)，跳过")

    _index_belong_cache = result
    print(f"[指数归属] 共获取 {len(result)} 只股票的指数归属")
    return result


def _refresh_pe_ttm():
    """
    通过腾讯行情接口批量获取 PE-TTM，增量更新 stock_pettm.json。
    从 stock_names.json 中读取所有股票代码，分批请求腾讯接口。
    """
    global _pe_ttm_cache
    import requests as req
    _refresh_status["step"] = "刷新PE-TTM..."
    _load_pe_ttm_cache()  # 先加载已有缓存

    # 从 stock_names.json 收集所有纯数字股票代码
    if not os.path.exists(_STOCK_NAMES_CACHE_FILE):
        print("[PE-TTM] stock_names.json 不存在，无法刷新")
        _refresh_status["error"] = "stock_names.json 不存在，请先刷新股票名称"
        return

    try:
        with open(_STOCK_NAMES_CACHE_FILE, "r", encoding="utf-8") as f:
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

    BATCH_SIZE = 300
    new_count = 0
    got_set = set()  # 本次成功获取到PE-TTM的代码
    for i in range(0, total, BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
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
            print(f"[PE-TTM] 第{i//BATCH_SIZE+1}批失败: {e}")

        _refresh_status["loaded"] = min(i + BATCH_SIZE, total)
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
        os.makedirs(os.path.dirname(_STOCK_PE_TTM_FILE), exist_ok=True)
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
        _safe_write_json_file(_STOCK_PE_TTM_FILE, combined, ensure_ascii=False)
        print(f"[PE-TTM] 刷新完成: 共 {len(combined)} 条 (PE-TTM: {sum(1 for v in combined.values() if 'pe_ttm' in v)} 条, 指数归属: {sum(1 for v in combined.values() if 'index' in v)} 条), 已安全保存到 {_STOCK_PE_TTM_FILE}")
    except Exception as e:
        print(f"[PE-TTM] 保存失败: {e}")
        _refresh_status["error"] = f"保存 PE-TTM 失败: {e}"


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
                        if mkt_dir == "sh":
                            sh_count += 1
                        else:
                            sz_count += 1

    # === 收集港股代码（ds目录）===
    hk_count = 0
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
                        hk_count += 1

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
            except Exception as e:
                print(f"[刷新]   新浪A股批次{batch_num}失败: {e}")
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
            except Exception as e:
                print(f"[刷新]   腾讯港股批次{batch_num}失败: {e}")
            if batch_num < total_batches:
                time.sleep(0.5)

    return filled


def _refresh_stock_names():
    """
    从本地文件批量获取全市场股票名称，保存到 stock_names.json。
    数据来源优先级：
      1. vipdoc/*.day 文件名（收集所有已下载过数据的股票代码）
      2. 新浪财经API（为无名称的代码批量查询名称）
    """
    global _stock_names_cache, _stock_names_loaded, _refresh_status

    if _refresh_status["running"]:
        return
    _refresh_status["running"] = True
    _refresh_status["step"] = "刷新股票名..."
    _refresh_status["error"] = None
    print("[刷新] ========== 开始刷新股票名称 ==========")

    # === 先加载已有缓存，新数据合并进去，不覆盖 ===
    raw_names = {}
    _load_stock_names_from_cache_file()
    if _stock_names_cache:
        for code, info in _stock_names_cache.items():
            if isinstance(info, dict):
                raw_names[code] = info
            else:
                raw_names[code] = {"name": info, "pinyin": ""}
        print(f"[刷新] 步骤1/5 加载缓存: 已加载 {len(raw_names)} 只")
    else:
        print("[刷新] 步骤1/5 加载缓存: 无缓存，全新读取")

    # === 方案1: vipdoc .day文件名收集代码 ===
    # .day 文件覆盖所有已下载过K线数据的股票
    vipdoc_codes = _collect_codes_from_vipdoc()
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
    print(f"[刷新] 步骤2/5 合并扫描: vipdoc共{v_total}只 (sh{v_sh}+sz{v_sz}+ds{v_hk}), 缓存{cache_before}只, 合并后{len(raw_names)}只 (新增{vipdoc_new}只)")

    # === 方案2: 新浪API补全缺失的名称 ===
    # 即使已有缓存，如果有新发现的代码（如港股）没有名称，也要补全
    codes_without_name = [c for c, info in raw_names.items() if not info.get("name")]
    if codes_without_name:
        a_no = sum(1 for c in codes_without_name if raw_names[c].get("market") != "hk")
        hk_no = sum(1 for c in codes_without_name if raw_names[c].get("market") == "hk")
        print(f"[刷新] 步骤3/5 补全名称: {len(codes_without_name)} 只无名称 (A股{a_no}, 港股{hk_no})")
        temp_dict = {c: raw_names[c] for c in codes_without_name}
        filled = _fetch_names_from_sina_once(temp_dict)
        for code, info in temp_dict.items():
            if info.get("name"):
                raw_names[code] = info
        failed = len(codes_without_name) - filled
        if failed > 0:
            print(f"[刷新] 步骤3/5 补全名称: 成功 {filled} 只, 失败 {failed} 只")
        else:
            print(f"[刷新] 步骤3/5 补全名称: 全部成功 {filled} 只")
    else:
        print("[刷新] 步骤3/5 补全名称: 无需补全")

    # === 补充通达信板块指数名称（88xxxx系列，如880491半导体、881319半导体）===
    # 88xxxx代码不以标准A股格式开头，_is_a_stock_code() 会过滤掉，所以不在 raw_names 中。
    # 来源: tdxzs.cfg（通达信配置文件）和 tdxhy_mapping_data.py（本地映射表）
    tdxzs_filled = 0
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
            print(f"[刷新]   读取tdxzs.cfg失败: {e}")

    # 新版研究行业(881xxx)从 tdxhy_mapping_data 读取
    tdxhy_filled = 0
    _mapping_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DataAPI", "tdxhy_mapping_data.py")
    if os.path.exists(_mapping_path):
        try:
            _mapping_ns = {}
            exec(open(_mapping_path, encoding="utf-8").read(), _mapping_ns)
            _TDXHY_881_TO_X = _mapping_ns.get("_TDXHY_881_TO_X", {})
            for code_881, (x_code, name) in _TDXHY_881_TO_X.items():
                compound_key = "sh" + code_881
                if compound_key not in raw_names:
                    raw_names[compound_key] = {"name": name, "pinyin": "", "market": "sh"}
                    tdxhy_filled += 1
                elif not raw_names[compound_key].get("name"):
                    raw_names[compound_key]["name"] = name
                    tdxhy_filled += 1
        except Exception as e:
            print(f"[刷新]   加载tdxhy_mapping_data失败: {e}")
    else:
        print(f"[刷新]   tdxhy_mapping_data.py不存在: {_mapping_path}")

    block_filled = tdxzs_filled + tdxhy_filled
    print(f"[刷新] 步骤4/5 补充板块: tdxzs.cfg +{tdxzs_filled}条, tdxhy +{tdxhy_filled}条, 共补全 {block_filled} 条板块")

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
    for code in list(all_names.keys()):
        name = all_names[code].get("name", "")
        if not name:
            del all_names[code]
            filtered_count += 1
            filtered_empty += 1
        elif name.startswith("*ST") or name.startswith("ST"):
            del all_names[code]
            filtered_count += 1
            filtered_st += 1
        elif "退" in name:
            del all_names[code]
            filtered_count += 1
            filtered_delist += 1
    if all_names:
        os.makedirs(os.path.dirname(_STOCK_NAMES_CACHE_FILE), exist_ok=True)
        _safe_write_json_file(_STOCK_NAMES_CACHE_FILE, all_names, ensure_ascii=False)
        _stock_names_cache = all_names
        _stock_names_loaded = True
        sh_count = sum(1 for c in all_names if all_names[c].get("market") == "sh")
        sz_count = sum(1 for c in all_names if all_names[c].get("market") == "sz")
        hk_count = sum(1 for c in all_names if all_names[c].get("market") == "hk")
        if filtered_count > 0:
            parts = []
            if filtered_st: parts.append(f"ST/*ST {filtered_st}只")
            if filtered_delist: parts.append(f"退市 {filtered_delist}只")
            if filtered_empty: parts.append(f"无名 {filtered_empty}只")
            print(f"[刷新] 步骤5/5 过滤保存: 过滤 {filtered_count} 只 ({', '.join(parts)}), 最终 {len(all_names)} 只 (上海{sh_count}, 深圳{sz_count}, 港股{hk_count}) → {os.path.basename(_STOCK_NAMES_CACHE_FILE)}")
        else:
            print(f"[刷新] 步骤5/5 过滤保存: 最终 {len(all_names)} 只 (上海{sh_count}, 深圳{sz_count}, 港股{hk_count}) → {os.path.basename(_STOCK_NAMES_CACHE_FILE)}")
    else:
        print("[刷新] 步骤5/5 过滤保存: 失败，未获取到任何数据")

    # 刷新板块文件（block_zs.dat / block_gn.dat / block_fg.dat / block.dat）
    print("[刷新] ========== 开始刷新板块文件 ==========")
    _refresh_status["step"] = "刷新成分股..."
    try:
        def _set_step(msg):
            _refresh_status["step"] = msg
        refresh_block_files(progress_callback=_set_step)
    except Exception as e:
        print(f"[刷新] 板块文件刷新失败: {e}")

    # 刷新 PE-TTM（增量更新 stock_pettm.json）
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
        hk_file = os.path.join(VIPDOC_DIR, "ds", "lday", f"31#{code}.day")
        if os.path.exists(hk_file):
            return 'hk', code
        ds_file = os.path.join(VIPDOC_DIR, "ds", "lday", f"62#{code}.day")
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
        f = os.path.join(VIPDOC_DIR, m, "lday", f"{m}{code}.day")
        if os.path.exists(f):
            return m, code
    f = os.path.join(VIPDOC_DIR, "ds", "lday", f"31#{code}.day")
    if os.path.exists(f):
        return 'hk', code
    f = os.path.join(VIPDOC_DIR, "ds", "lday", f"62#{code}.day")
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

_MAX_CACHE_SIZE = 50  # 最多缓存 50 个 (股票, 周期) 组合
_stocks_analysis_cache = collections.OrderedDict()
_cache_lock = threading.RLock()  # 保护 _stocks_analysis_cache 的并发读写

# 扫描跳过记录（收集后统一打印）
_scan_skip_log = []

# 全市场流通市值缓存（通过AKShare东财接口一次获取，保存到本地JSON）
# key: 股票代码(6位), value: 流通市值(亿元, float)
_float_mc_cache = {}
_float_mc_loaded = False
_FLOAT_MC_CACHE_FILE = os.path.join(VIPDOC_DIR, "stock_float_mc.json")

def _load_float_mc_cache():
    """从本地JSON加载流通市值缓存（无日期限制，作为腾讯接口失败时的兜底）。"""
    global _float_mc_loaded, _float_mc_cache
    if _float_mc_loaded:
        return
    if not os.path.exists(_FLOAT_MC_CACHE_FILE):
        return
    try:
        with open(_FLOAT_MC_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "data" in data:
            _float_mc_cache = data["data"]
            _float_mc_loaded = True
            print(f"[流通市值] 从本地缓存加载 {len(_float_mc_cache)} 只股票")
    except Exception as e:
        print(f"[流通市值] 读取缓存失败: {e}")

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
    BATCH_SIZE = 300
    all_mv = {}
    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
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
            print(f"[流通市值] 腾讯接口第{i//BATCH_SIZE+1}批失败: {type(e).__name__}: {e}")
    return all_mv

def _update_float_mc_cache(mv_dict):
    """将外部获取的流通市值字典合并到全局缓存，并保存到本地JSON。
    调用方应确保 _load_float_mc_cache() 已先执行。"""
    global _float_mc_cache, _float_mc_loaded
    _float_mc_cache.update(mv_dict)
    _float_mc_loaded = True
    # 保存到本地JSON（无日期限制，作为下次腾讯接口失败时的兜底）
    try:
        with open(_FLOAT_MC_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"data": _float_mc_cache}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[流通市值] 保存缓存文件失败: {e}")

def _get_float_mc_from_cache(code):
    """从缓存获取流通市值（亿元），未命中返回None。"""
    return _float_mc_cache.get(code)

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
    """写入缓存，超出上限时淘汰最旧的条目（LRU语义）。
    内存由 LRU 50 条上限 + 扫描时逐只释放非买点缓存共同控制。
    """
    with _cache_lock:
        if key in _stocks_analysis_cache:
            del _stocks_analysis_cache[key]  # 移到末尾
        elif len(_stocks_analysis_cache) >= _MAX_CACHE_SIZE:
            oldest_key = next(iter(_stocks_analysis_cache))
            _stocks_analysis_cache.pop(oldest_key)
            import gc
            gc.collect()
            print(f"[内存] 缓存已满({_MAX_CACHE_SIZE})，淘汰: {oldest_key}")
        _stocks_analysis_cache[key] = value


def _cache_get(key):
    """读取缓存，命中时移到末尾（LRU语义）"""
    with _cache_lock:
        if key not in _stocks_analysis_cache:
            return None
        value = _stocks_analysis_cache.pop(key)
        _stocks_analysis_cache[key] = value
    return value


def _cache_remove(key):
    """从缓存中删除指定条目（不触发 GC，由调用方在适当时机统一回收）"""
    with _cache_lock:
        if key in _stocks_analysis_cache:
            del _stocks_analysis_cache[key]


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
# 手选进入段选点保存/恢复
# ============================================================
SAVED_POINT_FILE = r"C:\new_tdx_test\vipdoc\double_click_dt.csv"
# CSV列：股票代码,股票名,年K选点,季K选点,月K选点,周K选点,日K选点,30分选点,15分选点,5分选点,1分选点
SAVED_POINT_COLUMNS = ["code", "name", "y", "q", "m", "w", "d", "60m", "30m", "15m", "5m", "1m", "15s"]
# freq -> CSV列名 的映射
FREQ_TO_COL = {"y": "y", "q": "q", "m": "m", "w": "w", "d": "d", "60m": "60m", "30m": "30m", "15m": "15m", "5m": "5m", "1m": "1m", "15s": "15s"}
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
    left_klc = kl_list.lst[mid_idx - 1]

    # 取左肩原始K线序列的第一根（最左边）
    if hasattr(left_klc, 'lst') and left_klc.lst:
        first_klu = left_klc.lst[0]
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
        print(f"[警告] 读取选点文件失败: {e}")
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
        print(f"[信息] 保存选点成功: {code} {freq} {col}={sdt}")
    except Exception as e:
        print(f"[警告] 保存选点文件失败: {e}")


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
        print(f"[信息] 清除选点成功: {code} {freq}")
    except Exception as e:
        print(f"[警告] 清除选点失败: {e}")


def _cleanup_all_futures_data():
    """期货切到股票时彻底清理所有期货数据：K线缓存、分析缓存、选点记录"""
    import gc
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

    gc.collect()


# 启动时加载一次选点数据
_saved_point_times = _load_saved_point_times()


def _save_last_code_freq(code, freq="d"):
    """持久化上次查看的代码和周期到JSON文件（股票和期货通用）"""
    try:
        data = {"code": code, "freq": freq}
        with open(_LAST_CODE_FREQ_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        pass  # 静默失败，不影响主流程


def _load_last_code_freq():
    """从JSON文件加载上次查看的代码和周期，返回 (code, freq) 或 (None, None)"""
    try:
        if not os.path.exists(_LAST_CODE_FREQ_FILE):
            return None, None
        with open(_LAST_CODE_FREQ_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        code = data.get("code", "").strip()
        freq = data.get("freq", "d")
        if code:
            return code, freq
    except Exception as e:
        print(f"[警告] 异常: {type(e).__name__}: {e}")
    return None, None


# ============================================================
# 文字标注持久化存储
# ============================================================
ANNOTATIONS_FILE = os.path.join(VIPDOC_DIR, "text_annotation.json")
_annotations_cache = {}  # { "code_freq": [ { "date": "2024-01-15", "text": "支撑位", "y_offset": 0 }, ... ] }
_annotations_loaded = False


def _load_annotations():
    """从 text_annotation.json 加载标注数据到内存"""
    global _annotations_cache, _annotations_loaded
    if _annotations_loaded:
        return
    if os.path.exists(ANNOTATIONS_FILE):
        try:
            with open(ANNOTATIONS_FILE, "r", encoding="utf-8") as f:
                _annotations_cache = json.load(f)
            print(f"[信息] 标注数据已加载: {len(_annotations_cache)} 个条目")
        except Exception as e:
            print(f"[警告] 加载标注数据失败: {e}")
            _annotations_cache = {}
    _annotations_loaded = True


def _save_annotations():
    """保存标注数据到 text_annotation.json"""
    try:
        with open(ANNOTATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(_annotations_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[警告] 保存标注数据失败: {e}")


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
        for suffix in [".SH", ".SZ", ".HK", ".BJ", ".DS"]:
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


def _calc_futures_white_hline(kl_list, freq, date_fmt):
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
    for idx, klc in enumerate(kl_list.lst):
        if klc is end_klc:
            end_fx_idx = idx
            break
    if end_fx_idx is None or end_fx_idx <= 0:
        return white_hline
    left_klc = kl_list.lst[end_fx_idx - 1]
    klc_high = left_klc.high
    klc_low = left_klc.low
    tgt_klu = None
    if hasattr(left_klc, 'lst') and left_klc.lst:
        for klu in left_klc.lst:
            if direction == "down" and klu.high == klc_high:
                tgt_klu = klu
                break
            elif direction == "up" and klu.low == klc_low:
                tgt_klu = klu
                break
        if tgt_klu is None:
            tgt_klu = left_klc.lst[0]
    if tgt_klu:
        ls_date = tgt_klu.time.toFmtStr(date_fmt)
    else:
        ls_date = ""
    if direction == "down":
        white_hline = {"price": round(klc_high, 2), "start_date": ls_date}
    elif direction == "up":
        white_hline = {"price": round(klc_low, 2), "start_date": ls_date}
    return white_hline

def init_chan_symbol(api, symbol, name, freq_sec, freq_label, start_time=None):
    """拉取历史K线 + 运行 chan.py 分析，返回 (chan, klines, kl_type, records) 或 None。
    由 SSE handler 调用，每个 SSE 连接自包含。
    start_time: 选点起始时间，有值时只拉取该时间之后的K线"""
    import time as _time
    from Common.CEnum import KL_TYPE, AUTYPE
    from Chan import CChan
    from ChanConfig import CChanConfig
    from DataAPI.TqSdkAPI import FREQ_LABEL_CN

    display_label = FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{display_label}"

    try:
        records = fetch_futures_kline(api, symbol, freq_sec=freq_sec, display_key=display_key, start_time=start_time)
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
        if len(kl_list.lst) == 0:
            return None
        last_klc = kl_list.lst[-1]
        if len(last_klc.lst) == 0:
            return None
        last_klu = last_klc.lst[-1]
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
    for klc in kl_list.lst:
        for klu in klc.lst:
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



def _analyze_futures_internal(code, freq="1m", end_date=None, dual=False, existing_chan=None, existing_records=None, step=None):
    """
    使用天勤数据源 + chan.py 进行期货/期指缠论分析（静态模式，HTTP 请求）
    与股票分析输出格式一致，便于前端复用同一套图表渲染逻辑。

    dual=True: 双窗口模式，返回 result 含 sub 字段（两个独立 CChan 对象）。
    existing_chan: 双窗口模式下，复用已有的单窗口 CChan 对象（匹配周期则复用）。
    existing_records: 对应 existing_chan 的 records。
    step: 箭头步进，在 full_records 中从 end_date 位置偏移 step 根K线作为新的截断日期。
    """
    import time
    t_start = time.time()

    if not TQ_AVAILABLE or CTqSdkAPI is None:
        return {"error": "天勤数据源未安装，请执行: pip install tqsdk"}

    # 确定周期秒数
    freq_sec = FREQ_SEC_MAP.get(freq, 86400)

    # 1. 拉取历史K线（每次冷启动重新拉取天勤数据）
    t_fetch = time.time()
    from tqsdk import TqApi, TqAuth
    from DataAPI.TqSdkAPI import TQ_ACCOUNT, TQ_PASSWORD, FREQ_LABEL_CN
    _display_key = f"{code}:{FREQ_LABEL_CN.get(freq, freq)}"
    _api = None
    try:
        _api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
        full_records = fetch_futures_kline(_api, code, freq_sec=freq_sec, display_key=_display_key)
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
    import gc
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
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

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
        sub_freq = _FUTURES_DUAL_FREQ_MAP.get(freq)
        if not sub_freq:
            result["error"] = f"双窗口不支持当前周期: {freq}"
            return result
        sub_freq_sec = FREQ_SEC_MAP.get(sub_freq, 15)
        print(f"[双窗口] 开始提取子级别({sub_freq})数据...")

        # 检查 existing_chan 是否匹配 sub_freq，匹配则复用
        sub_chan = None
        sub_records = None
        if existing_chan is not None and existing_records is not None and freq == sub_freq:
            # existing_chan 匹配的是主周期，子周期需要新建
            pass
        elif existing_chan is not None and existing_records is not None and freq != sub_freq:
            # 如果 existing_chan 正好匹配 sub_freq，复用它
            # 这个情况发生在上窗周期!=单窗口周期时（暂不涉及，保留接口）
            pass

        # 拉取子级别历史K线
        t_sub_fetch = time.time()
        _sub_display_key = f"{code}:{FREQ_LABEL_CN.get(sub_freq, sub_freq)}"
        _api_sub = None
        try:
            _api_sub = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
            sub_full_records = fetch_futures_kline(_api_sub, code, freq_sec=sub_freq_sec, display_key=_sub_display_key)
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
        main_freq_sec = FREQ_SEC_MAP.get(freq, 60)
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
            import gc
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
            full_records, sub_records = read_main_level_records(market, code, freq, return_raw=True, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            print(f"[耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            print(f"[信息] 子级别({sub_freq})数据加载: {len(sub_records)}条 (复用前复权)")
        elif freq == 'w' and sub_freq == 'd':
            # 优化：w+d 共用同一次日线文件读取和前复权，避免重复读取和二次复权
            full_records, sub_records = read_main_level_records(market, code, freq, return_raw=True, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            print(f"[耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            print(f"[信息] 子级别({sub_freq})数据加载: {len(sub_records)}条 (复用前复权)")
        else:
            full_records = read_main_level_records(market, code, freq, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            print(f"[耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            sub_records = read_sub_level_records(market, code, freq, sub_freq, full_records, end_date=target_dt)
        forward_adjust_done = FORWARD_ADJUST_ENABLED
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
            full_records = read_main_level_records(market, code, freq, end_date=target_dt)
            if len(full_records) < 5:
                print(f"[调试-K线不足] code={code}, market={market}, freq={freq}, target_dt={target_dt}, records={len(full_records)}")
                return {"error": f"K线数据不足: 仅{len(full_records)}条"}
            forward_adjust_done = FORWARD_ADJUST_ENABLED
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
    import gc
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
            sdt_raw = begin_klu.time.to_str()
            sdt_dt = datetime.strptime(sdt_raw, "%Y/%m/%d %H:%M")
            star_date = sdt_dt.strftime(date_fmt)
        except:
            star_date = sdt_raw
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
        is_virtual = not latest_bi.get("is_sure", True)
        direction = latest_bi.get("direction", "")
        end_fx_idx = latest_bi.get("end_fx_idx")
        if end_fx_idx is not None and end_fx_idx > 0:
            left_klc = kl_list.lst[end_fx_idx - 1]
            klc_high = left_klc.high
            klc_low = left_klc.low
            tgt_klu = None
            if hasattr(left_klc, 'lst') and left_klc.lst:
                for klu in left_klc.lst:
                    if direction == "down" and klu.high == klc_high:
                        tgt_klu = klu
                        break
                    elif direction == "up" and klu.low == klc_low:
                        tgt_klu = klu
                        break
                if tgt_klu is None:
                    tgt_klu = left_klc.lst[0]
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
    for klc in sub_kl_list.lst:
        for klu in klc.lst:
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
                "vol": getattr(klu, 'vol', 0),
                "amount": getattr(klu, 'amount', 0),
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
            sdt_raw = begin_klu.time.to_str()
            sdt_dt = datetime.strptime(sdt_raw, "%Y/%m/%d %H:%M")
            star_date = sdt_dt.strftime(date_fmt)
        except:
            star_date = sdt_raw
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
_futures_analysis_cache = {}


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
    import gc
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
    from DataAPI.TqSdkAPI import (
        FREQ_SEC_MAP, FREQ_LABEL_CN, CTqSdkAPI,
        fetch_futures_kline,
        TQ_ACCOUNT, TQ_PASSWORD, FUTURES_ALIASES,
    )

    # 别名解析
    symbol_upper = symbol.upper()
    if symbol_upper in FUTURES_ALIASES:
        symbol = FUTURES_ALIASES[symbol_upper]

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


def analyze_stock(code, freq="d", end_date=None, cache_chan=True, dual=False, step=None):
    """公开分析入口：先识别市场，再分流到股票或期货的并列内部流程。
    股票/指数：走通达信数据源，支持 cache_chan 和 dual 双窗口。
    期货/期指：走天勤数据源，dual=True 时使用两个独立 CChan 对象。
    """
    market, normalized_code = _get_market_code(code)
    # print(f"[调试-analyze_stock] 输入code={code}, 识别market={market}, normalized_code={normalized_code}")
    if not market:
        return {"error": f"无法识别股票代码: {code}"}
    if market == 'futures':
        return _analyze_futures_internal(normalized_code, freq=freq, end_date=end_date, dual=dual, step=step)
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
            info = _STOCK_NAMES_CACHE.get(compound_key, {})
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
    """调试包装：打印 _page_index_code 后调用 get_index_stocks"""
    print(f"\n[成分股-DEBUG] ========== _debug_read_page_index_stocks 被调用 ==========")
    print(f"[成分股-DEBUG] _page_index_code = '{sector_code}'")
    if not sector_code:
        print(f"[成分股-DEBUG] ❌ _page_index_code 为空，返回空列表")
        return []
    print(f"[成分股-DEBUG] 调用 get_index_stocks('{sector_code}')...")
    print(f"[成分股-DEBUG] 开始时间: {time.strftime('%H:%M:%S')}")
    result = get_index_stocks(sector_code)
    print(f"[成分股-DEBUG] 结束时间: {time.strftime('%H:%M:%S')}")
    print(f"[成分股-DEBUG] get_index_stocks 返回 {len(result)} 只股票")
    return result


def read_tdxhy_l2_indices():
    """返回所有二级行业板块指数列表（X+4位代码对应的881yyy），共125个"""
    from DataAPI.TdxAPI import _TDXHY_X_TO_881 as _x_to_881
    result = []
    for x_code, (name, code_881) in _x_to_881.items():
        digits = x_code[1:]  # 去掉X
        if len(digits) == 4:  # X+4位 = 二级行业
            result.append({"code": code_881, "prefix": "1", "name": name})
    return result


def read_tdxhy_l3_indices():
    """返回所有三级行业板块指数列表（X+6位代码对应的881yyy），共315个"""
    from DataAPI.TdxAPI import _TDXHY_X_TO_881 as _x_to_881
    result = []
    for x_code, (name, code_881) in _x_to_881.items():
        digits = x_code[1:]  # 去掉X
        if len(digits) == 6:  # X+6位 = 三级行业
            result.append({"code": code_881, "prefix": "1", "name": name})
    return result


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
        global _scan_aborted, _scan_start_time
        parsed = urlparse(self.path)
        if parsed.path == "/api/stock":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            end_date = params.get("end_date", [""])[0] or None
            step = params.get("step", [None])[0]
            dual = params.get("dual", ["0"])[0] == "1"
            if not code:
                self.send_json_response({"error": "请输入股票代码"}, 400)
                return
            try:
                result = analyze_stock(code, freq=freq, end_date=end_date, dual=dual, step=step)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
                    # 非复盘模式：持久化当前代码和周期，下次冷启动自动恢复
                    # 双窗口/期货不保存，只保存上面窗口的周期
                    if not end_date and not params.get("dual_bottom", [""])[0] and result.get("meta", {}).get("market") != "futures":
                        _save_last_code_freq(code, freq)
            except Exception as e:
                import traceback
                print(f"[错误] analyze_stock异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
        elif parsed.path == "/api/stocks_manual_select_point":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            bi_idx = params.get("bi_idx", ["-1"])[0]
            if not code or bi_idx == "-1":
                self.send_json_response({"error": "缺少必要参数 code 或 bi_idx"}, 400)
                return
            try:
                result = stock_manual_select_point(code, freq=freq, bi_idx=bi_idx)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
            except Exception as e:
                import traceback
                print(f"[错误] stock_manual_select_point异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
        elif parsed.path == "/api/red_range_zs":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            left_date = params.get("left_date", [""])[0]
            right_date = params.get("right_date", [""])[0]
            end_date = params.get("end_date", [""])[0] or None
            if not code or not left_date or not right_date:
                self.send_json_response({"error": "参数错误: code/left_date/right_date 不能为空"}, 400)
                return
            try:
                result = compute_red_range_zs(code, sub_freq=freq, left_date=left_date, right_date=right_date, end_date=end_date)
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
            if not os.path.exists(_STOCK_NAMES_CACHE_FILE):
                self.send_json_response({"need_refresh": True, "msg": "请先刷新股票名缓存"}, 200)
                return
            keyword_upper = keyword.upper()
            exact_results = []
            exact_pinyin_results = []  # 拼音或名称完全匹配（如输入"LG"精确匹配"柳工"）
            prefix_results = []
            other_results = []

            # 手工补充通达信扩展市场指数，后续仍走统一的搜索结果选择与 analyze_stock 流程
            manual_items = [
                {"code": "932000", "name": "中证2000", "pinyin": "ZZ2", "market": "ds", "type": "指数"},
            ]
            for item in manual_items:
                bare_code = item["code"]
                name = item["name"]
                pinyin = item.get("pinyin", "")
                if not (keyword_upper in bare_code or keyword_upper in name.upper() or keyword_upper in pinyin):
                    continue
                if bare_code == keyword_upper:
                    exact_results.append(item)
                elif pinyin == keyword_upper or name.upper() == keyword_upper or keyword_upper in pinyin:
                    exact_pinyin_results.append(item)
                elif bare_code.startswith(keyword_upper):
                    prefix_results.append(item)
                else:
                    other_results.append(item)

            for compound_key, info in _stock_names_cache.items():
                if isinstance(info, dict):
                    name = info.get("name", "")
                    pinyin = info.get("pinyin", "")
                    market = info.get("market", "")
                else:
                    name = info
                    pinyin = ""
                    market = ""
                # 归一化：全角ASCII → 半角（兼容旧缓存中"鲁泰Ａ"→"鲁泰A"）
                name = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in name)
                pinyin = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in pinyin)

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
                elif pinyin == keyword_upper or name.upper() == keyword_upper:
                    exact_pinyin_results.append(item)
                elif bare_code.startswith(keyword_upper):
                    prefix_results.append(item)
                else:
                    other_results.append(item)

            results = exact_results[:10] + exact_pinyin_results[:10] + prefix_results[:10] + other_results[:10]
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
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

            # 期货别名搜索：支持用户输入短名称搜索（如 PTA、IF、rb 等）
            for alias, full_code in FUTURES_ALIASES.items():
                if keyword_upper in alias.upper():
                    name = _get_futures_name(full_code)
                    # 避免重复
                    if not any(r["code"] == full_code for r in results):
                        results.append({
                            "code": full_code, "name": name, "pinyin": alias,
                            "market": "futures", "type": "",
                        })
            self.send_json_response({"results": results}, 200)
        elif parsed.path == "/api/zxg_list":
            # 返回自选股列表（供前端逐只扫描）
            try:
                stocks = read_zxg_stocks()
                self.send_json_response({"stocks": stocks}, 200)
            except Exception as e:
                self.send_json_response({"error": str(e)}, 500)
        elif parsed.path == "/api/scan_stock_list":
            # 返回股票列表（支持逗号分隔多来源：zxg,sz50,hs300,zz500,zz1000 → 合并去重后返回）
            params = parse_qs(parsed.query)
            raw = params.get("source", ["zxg"])[0]
            sources = [s.strip() for s in raw.split(",") if s.strip()]
            _SOURCE_READERS = {
                "zxg": (read_zxg_stocks, "自选股"),
                "sz50": (read_sz50_stocks, "上证50"),
                "hs300": (read_hs300_stocks, "沪深300"),
                "zz500": (read_zz500_stocks, "中证500"),
                "zz1000": (read_zz1000_stocks, "中证1000"),
                "page_index": (
                    lambda: _debug_read_page_index_stocks(_page_index_code),
                    "成分股",
                ),
                "tdxhy2": (read_tdxhy_l2_indices, "板块指数2"),
                "tdxhy3": (read_tdxhy_l3_indices, "板块指数3"),
            }
            # 先读取所有来源的股票列表
            src_stocks = {}  # src → [(code, prefix, stock_dict), ...]
            errors = []
            for src in sources:
                reader = _SOURCE_READERS.get(src)
                if reader is None:
                    errors.append(f"未知来源: {src}")
                    continue
                read_fn, label = reader
                stocks = read_fn()
                if not stocks and src not in ("zxg", "page_index", "tdxhy2", "tdxhy3"):
                    errors.append(f"{label}板块文件不存在，请先在通达信中创建/下载{label}板块")
                    continue
                src_stocks[src] = stocks
            # 合并去重：按出现顺序，非zxg来源优先（zxg先出现 → 后续非zxg覆盖并升级_source）
            merged = []
            seen = {}  # (code, prefix) → index in merged
            for src in sources:
                stocks = src_stocks.get(src)
                if not stocks:
                    continue
                for stk in stocks:
                    key = (stk["code"], stk["prefix"])
                    if key not in seen:
                        stk["_source"] = src
                        seen[key] = len(merged)
                        merged.append(stk)
                    else:
                        exist_idx = seen[key]
                        exist_src = merged[exist_idx].get("_source", "")
                        if exist_src == "zxg" and src != "zxg":
                            # 升级：自选股 → 板块来源，预过滤生效
                            merged[exist_idx]["_source"] = src
            # 批量获取流通市值：仅当包含非板块指数来源时才调腾讯接口
            _need_float_mc = any(s not in ("tdxhy2", "tdxhy3") for s in sources)
            if _need_float_mc:
                # 先加载本地缓存，让内存中总有旧数据可用（即使腾讯接口失败）
                _load_float_mc_cache()
                if _float_mc_loaded:
                    print(f"[流通市值] 本地缓存已加载 {len(_float_mc_cache)} 只")
                try:
                    t_mc = time.time()
                    mv_dict = _fetch_float_mc_from_tencent(merged)
                    total_stocks = len(merged)
                    got_count = len(mv_dict)
                    miss_count = total_stocks - got_count
                    if mv_dict:
                        _update_float_mc_cache(mv_dict)
                        if miss_count == 0:
                            print(f"[流通市值] ✅ AKShare(腾讯接口) 获取全部 {got_count} 只 (耗时{time.time()-t_mc:.1f}s)")
                        else:
                            print(f"[流通市值] ⚠️ AKShare(腾讯接口) 获取 {got_count}/{total_stocks} 只，{miss_count} 只未获取到 (耗时{time.time()-t_mc:.1f}s)")
                    else:
                        print(f"[流通市值] 腾讯接口未返回数据，使用本地缓存")
                except Exception as e:
                    print(f"[流通市值] 腾讯接口异常: {type(e).__name__}: {e}，使用本地缓存")
            # 后端预过滤：对非自选股来源，提前剔除ST/流通市值<50亿
            pre_filtered = merged
            pre_skip_count = 0
            pre_skip_log = []  # 收集跳过原因，最后打印
            try:
                t_pre_all = time.time()
                filtered = []
                for stk in merged:
                    src = stk.get("_source", "zxg")
                    # 自选股和板块指数不做预过滤
                    if src in ("zxg", "tdxhy2", "tdxhy3"):
                        filtered.append(stk)
                        continue
                    code = stk.get("code", "")
                    prefix = stk.get("prefix", "")
                    _PFX_MAP = {"0": "sz", "1": "sh", "2": "bj"}
                    market = _PFX_MAP.get(prefix, "")
                    if not market or not code:
                        filtered.append(stk)
                        continue
                    pass_ok, pre_mc, skip_reason = _quick_prefilter_pass(market, code)
                    if not pass_ok:
                        pre_skip_count += 1
                        pre_skip_log.append(f"[预过滤] {code} 跳过 ({skip_reason})")
                    else:
                        filtered.append(stk)
                pre_filtered = filtered
                elapsed = time.time() - t_pre_all
                if pre_skip_count > 0:
                    print(f"[预过滤] 批量预过滤完成: 跳过 {pre_skip_count} 只，剩余 {len(pre_filtered)} 只 (耗时 {elapsed:.1f}s)")
                    for line in pre_skip_log:
                        print(line)
                else:
                    print(f"[预过滤] 批量预过滤完成: 全部通过 {len(pre_filtered)} 只 (耗时 {elapsed:.1f}s)")
            except Exception as e:
                print(f"[预过滤] 批量预过滤异常: {type(e).__name__}: {e}")
            self.send_json_response({
                "stocks": pre_filtered,
                "sources": sources,
                "total": len(pre_filtered),
                "pre_skipped": pre_skip_count,
                "errors": errors if errors else None
            }, 200)
        elif parsed.path == "/api/scan_one":
            # 扫描单只股票的买卖点（供前端逐只调用，实时显示进度）
            t_scan_start = time.time()
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            prefix = params.get("prefix", [""])[0]
            recent_str = params.get("recent", ["1"])[0]
            source = params.get("source", ["zxg"])[0]  # zxg=自选股, sz50=上证50, hs300=沪深300, zz500=中证500, zz1000=中证1000, page_index=成分股
            scan_mode = params.get("mode", [""])[0]  # "" = 买卖点扫描, "fx_d" = 底分型扫描
            try:
                recent_days = max(1, int(recent_str))
            except ValueError:
                recent_days = 1
            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return
            try:
                t0 = time.time()
                # 自选股扫描：用 prefix 拼出带市场前缀的代码（如 SH000852）
                # 港股：prefix="hk"，拼出 HK03690，让 _get_stock_market_code 通过 HK 前缀精确识别
                _PREFIX_MAP = {"0": "SZ", "1": "SH", "2": "BJ", "hk": "HK"}
                market_prefix = _PREFIX_MAP.get(prefix, "")
                qualified_code = (market_prefix + code) if market_prefix else code
                market = market_prefix.lower() if market_prefix else ""
                # 检查终止标志：在预过滤之前先检查，避免做无用功
                if _scan_aborted:
                    self.send_json_response({"error": "扫描已终止", "aborted": True}, 200)
                    return
                # 板块扫描的预过滤已在 /api/scan_stock_list 中批量完成，此处不再重复
                # 检查终止标志：在获取锁之前先检查，避免排队等待
                if _scan_aborted:
                    self.send_json_response({"error": "扫描已终止", "aborted": True}, 200)
                    return
                with _scan_lock:
                    if _scan_aborted:
                        self.send_json_response({"error": "扫描已终止", "aborted": True}, 200)
                        return
                    result = analyze_stock(qualified_code, freq=freq, cache_chan=True)
                t_analyze = time.time() - t0
                if "error" in result:
                    _scan_skip_log.append(f"{code} - {result['error']}")
                    print(f"[耗时-扫描] {code} 分析失败: {result['error']}, 耗时{t_analyze:.3f}s")
                    self.send_json_response({"error": result["error"]}, 200)
                else:
                    t0 = time.time()
                    bsps = result.get("bsps", [])
                    stock_name = result.get("meta", {}).get("name", f"{code}")
                    # [DEBUG] 打印API响应中的名称
                    # print(f"[DEBUG-名称] /api/scan_one({code}) meta.name='{result.get('meta', {}).get('name')}', stock_name='{stock_name}'")
                    klines = result.get("klines", [])
                    t_filter = 0

                    # 底分型扫描模式：最后一笔是向下实笔（即最后一个分型是底分型）
                    # 不能直接用 fxs[-1] 判断，因为最后可能有一个待定分型尚未形成笔
                    if scan_mode == "fx_d":
                        bis = result.get("bis", [])
                        is_fx_d = False
                        fx_strength = 0
                        if bis:
                            last_bi = bis[-1]
                            # 最后一笔必须是已确认的向下实笔
                            if last_bi.get("is_sure", True) and last_bi.get("direction") == "down":
                                is_fx_d = True
                                fx_strength = last_bi.get("fx_strength", 0)
                        t_filter = time.time() - t0

                        if is_fx_d:
                            t_total = time.time() - t_scan_start
                            print(f"[耗时-扫描-底分型] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 是底分型")
                            resp_data = {
                                "code": code + "." + market.upper(), "name": stock_name,
                                "is_fx_d": True,
                                "last_close": klines[-1]["close"] if klines else 0,
                                "freq": freq,
                                "fx_strength": fx_strength,
                            }
                        else:
                            # 不是底分型：释放缓存
                            mkt, cd = _get_market_code(qualified_code)
                            if mkt and cd:
                                cache_key = f"single_{mkt}_{cd}_{freq}_live"
                                _cache_remove(cache_key)
                            t_total = time.time() - t_scan_start
                            print(f"[耗时-扫描-底分型] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 不是底分型")
                            resp_data = {"code": code, "is_fx_d": False}
                        self.send_json_response(resp_data, 200)
                        return

                    # 均线分类扫描模式：按最新收盘价未攻克的最小周期均线分类
                    # 8条均线 → 9类：类9=全部在均线下(最差)，类1=全部在均线上(最强)
                    if scan_mode == "ma":
                        MA_PERIODS = [5, 13, 21, 34, 55, 89, 144, 233]
                        closes = [k.get("close", 0) for k in klines]
                        last_close = closes[-1] if closes else 0
                        ma_category = -1  # -1 表示无法计算（数据不足）
                        if last_close > 0 and len(closes) >= max(MA_PERIODS):
                            # 统计收盘价攻克的均线数：对每根均线，若 close >= MA 则攻克
                            conquered = 0
                            for p in MA_PERIODS:
                                ma_val = sum(closes[-p:]) / p
                                if last_close >= ma_val:
                                    conquered += 1
                            # 类别 = 未攻克的均线数 (0=全部攻克最强, 8=全部未攻克最弱)
                            ma_category = 8 - conquered
                        t_filter = time.time() - t0

                        t_total = time.time() - t_scan_start
                        print(f"[耗时-扫描-均线] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 分类:{ma_category}")
                        resp_data = {
                            "code": code + "." + market.upper(),
                            "name": stock_name,
                            "ma_category": ma_category,
                            "last_close": round(last_close, 2),
                            "freq": freq,
                        }
                        # 类0~2 保留缓存（类似买点扫描），类3~8 释放缓存
                        if ma_category > 2:
                            mkt, cd = _get_market_code(qualified_code)
                            if mkt and cd:
                                cache_key = f"single_{mkt}_{cd}_{freq}_live"
                                _cache_remove(cache_key)
                        self.send_json_response(resp_data, 200)
                        return

                    # 买卖点扫描模式（原有逻辑）
                    # 最近N根K线的日期集合
                    recent_dates = set()
                    for k in klines[-recent_days:]:
                        recent_dates.add(k.get("date", ""))
                    buy_points = []
                    sell_points = []
                    for bsp in bsps:
                        if bsp.get("date", "") in recent_dates:
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

                    # 缓存策略：与扫描展示一致——最近N根K线有买点才保留缓存
                    # 不能用 bsps[-1] 判断，因为整个历史的最后一个买卖点
                    # 可能是很久以前的买点，导致大量无意义缓存挤占空间
                    # 用 _get_market_code 解析 qualified_code，确保与 _analyze_stock_internal
                    # 内部构造 cache_key 时使用的 market/code 完全一致
                    if not buy_points:
                        mkt, cd = _get_market_code(qualified_code)
                        if mkt and cd:
                            cache_key = f"single_{mkt}_{cd}_{freq}_live"
                            _cache_remove(cache_key)

                    if has_points:
                        # 有买/卖点：缓存已由 analyze_stock 写入（完整三项），后续点击可直接查看
                        t_total = time.time() - t_scan_start
                        print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 有买卖点")
                        resp_data = {
                            "code": code + "." + market.upper(), "name": stock_name,
                            "buy_points": buy_points,
                            "sell_points": sell_points,
                            "last_close": klines[-1]["close"] if klines else 0,
                            "freq": freq,
                        }
                        # print(f"[DEBUG-名称] /api/scan_one({code}) resp_data.name='{resp_data['name']}'")
                    else:
                        # 无买卖点：已释放缓存，不保留
                        t_total = time.time() - t_scan_start
                        print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 无买卖点")
                        resp_data = {"code": code, "buy_points": [], "sell_points": []}
                    self.send_json_response(resp_data, 200)
            except Exception as e:
                import traceback
                _scan_skip_log.append(f"{code} - 异常: {e}")
                t_total = time.time() - t_scan_start
                print(f"[耗时-扫描] {code} 异常: {e}, 总耗时{t_total:.3f}s")
                self.send_json_response({"error": str(e)}, 200)
        elif parsed.path == "/api/scan_page_index_code":
            # 前端传入当前页面正在查看的板块指数代码，后端存储供"成分股"扫描来源使用
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0].strip()
            global _page_index_code
            if code:
                # 去除市场后缀（如 880491.SH → 880491）
                if "." in code:
                    code = code.split(".")[0]
                _page_index_code = code
                print(f"[成分股] 已设置板块指数代码: {code}")
                self.send_json_response({"ok": True, "code": code}, 200)
            else:
                self.send_json_response({"error": "缺少code参数"}, 400)
        elif parsed.path == "/api/scan_start":
            # 新一轮扫描开始：清空跳过记录和终止标志，记录开始时间
            # 扫描过程中逐只判断：有买点才保留缓存，否则释放
            _scan_aborted = False
            _scan_skip_log.clear()
            _scan_start_time = time.time()
            try:
                _load_stock_names_from_cache_file()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/refresh_stock_names":
            # 启动股票名称刷新
            if _refresh_status["running"]:
                self.send_json_response({"status": "already_running", **_refresh_status}, 200)
            else:
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
        elif parsed.path == "/api/refresh_status":
            # 查询刷新状态（前端轮询用）
            self.send_json_response(_refresh_status, 200)
        elif parsed.path == "/api/scan_end":
            # 扫描结束：统一打印跳过记录，发送 Windows 通知
            if _scan_skip_log:
                print("\n========== 扫描异常/失败股票明细 ==========")
                print(f"共 {len(_scan_skip_log)} 只:")
                for i, item in enumerate(_scan_skip_log, 1):
                    print(f"  {i}. {item}")
                print("============================================\n")
            else:
                print("\n[扫描明细] 全部扫描成功，无异常股票\n")
            # 发送 Windows 通知
            if _scan_start_time is not None:
                elapsed = time.time() - _scan_start_time
                minutes = int(elapsed // 60)
                seconds = int(elapsed % 60)
                if minutes > 0:
                    time_str = f"{minutes}分{seconds}秒"
                else:
                    time_str = f"{seconds}秒"
                skip_count = len(_scan_skip_log)
                msg = f"耗时 {time_str}"
                if skip_count > 0:
                    msg += f"，跳过 {skip_count} 只"
                _send_windows_notification("扫描完成", msg)
                _scan_start_time = None  # 重置，避免重复通知
            self.send_json_response({"count": len(_scan_skip_log)}, 200)
        elif parsed.path == "/api/scan_clear_cache":
            # 关闭扫描面板：缓存由扫描时逐只判断管理，此处不再做全量清除
            print("[扫描缓存] 面板关闭，缓存由 LRU 自然淘汰")
            self.send_json_response({"cleared": 0}, 200)
        elif parsed.path == "/api/scan_abort":
            # 前端点击中断扫描：设置后端全局终止标志
            _scan_aborted = True
            print("[扫描] 收到中断请求，设置终止标志")
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/zxg_save":
            # 保存勾选的股票到通达信 && 同花顺自选股
            try:
                params = parse_qs(parsed.query)
                codes_str = params.get("codes", [""])[0]
                codes = codes_str.split(",") if codes_str else []
                if not codes:
                    self.send_json_response({"error": "codes为空"}, 400)
                    return
                # 原始代码（带后缀，如 600150.SH），给通达信用
                codes_raw = [c.strip() for c in codes]
                # 完整代码（带后缀），给同花顺用——保留后缀避免 000001.SZ/000001.SH 冲突
                codes_ths = list(dict.fromkeys(codes_raw))  # 去重保序
                # 通达信：本地自选股文件写入
                print(f"[保存] 通达信: 输入 {len(codes_raw)} 只, 代码={codes_raw}")
                tdx_added = save_to_zxg_blk(codes_raw)
                print(f"[保存] 通达信: 实际写入 {tdx_added} 只")

                # 同花顺：云端 API 同步
                ths_added = 0
                ths_msg = ""
                print(f"[保存] 同花顺: 输入 {len(codes_ths)} 只, 代码={codes_ths}")
                if _THS_CLOUD_AVAILABLE:
                    try:
                        cloud_result = save_scan_to_ths_cloud(codes_ths)
                        if "error" in cloud_result:
                            raise Exception(cloud_result["error"])
                        ths_added = len(cloud_result.get("added", []))
                        ths_msg = "ok"
                        print(f"[保存] 同花顺: 新增{ths_added}, 跳过{len(cloud_result.get('skipped',[]))}, 失败{len(cloud_result.get('failed',[]))}")
                    except Exception as e:
                        err_str = str(e)
                        if "登录状态失效" in err_str or "Cookie" in err_str:
                            ths_msg = "Cookie过期，请运行 ths_capture_cookie.py 重新获取"
                        else:
                            ths_msg = f"云端同步失败: {err_str}"
                        print(f"[保存] 同花顺: {ths_msg}")
                else:
                    ths_msg = "ths_cloud_api.py 未找到，请确保该文件在脚本同目录"
                    print(f"[保存] 同花顺: {ths_msg}")
                print(f"[保存] 汇总: 通达信={tdx_added}, 同花顺={ths_added}, msg={ths_msg}")
                self.send_json_response({
                    "ok": True,
                    "tdx_saved": tdx_added,
                    "ths_saved": ths_added,
                    "ths_msg": ths_msg,
                }, 200)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_json_response({"error": str(e)}, 500)
        elif parsed.path == "/api/clear_saved_point":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return
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
            # 用 market-qualified code 区分沪市深市同号股票
            qualified_code = f"{normalized_code}.{market.upper()}" if market else normalized_code
            _clear_saved_point_time(qualified_code, freq)
            cache_key = f"single_{market}_{normalized_code}_{freq}_live"
            with _cache_lock:
                if cache_key in _stocks_analysis_cache:
                    del _stocks_analysis_cache[cache_key]
            import gc
            gc.collect()
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/futures_manual_select_point":
            # 期货期指双击选点
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [""])[0]
            freq = params.get("freq", ["15s"])[0]
            bi_idx = params.get("bi_idx", ["-1"])[0]
            if not symbol or bi_idx == "-1":
                self.send_json_response({"error": "缺少必要参数 symbol 或 bi_idx"}, 400)
                return
            try:
                result = futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
            except Exception as e:
                import traceback
                print(f"[错误] futures_manual_select_point异常: {e}")
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
            # 别名解析
            symbol_upper = symbol.upper()
            if symbol_upper in FUTURES_ALIASES:
                symbol = FUTURES_ALIASES[symbol_upper]
            _clear_saved_point_time(symbol, freq)
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/futures_cleanup":
            # 期货切到股票：彻底清理所有期货数据
            _cleanup_all_futures_data()
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
            # SSE 实时推送端点（支持单窗口和双窗口模式）
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [""])[0]
            freq = params.get("freq", ["15s"])[0]
            start_time = params.get("start_time", [""])[0] or None
            dual = params.get("dual", ["0"])[0] == "1"
            if not symbol:
                self.send_json_response({"error": "缺少symbol参数"}, 400)
                return
            if dual:
                sub_freq_var = params.get("sub_freq", [""])[0] or None
                self._handle_sse_stream_dual(symbol, freq, sub_freq=sub_freq_var, start_time=start_time)
            else:
                self._handle_sse_stream_single(symbol, freq, start_time=start_time)
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

    def _handle_sse_stream_dual(self, symbol, main_freq="1m", sub_freq=None, start_time=None):
        """Server-Sent Events 双窗口推送端：两个独立 CChan 对象，一次 SSE 连接推送两个周期数据。
        与股票双窗口解耦，使用独立的 TqApi 连接和独立的 CChan 对象。
        """
        if not TQ_AVAILABLE:
            self.send_json_response({"error": "天勤数据源不可用"}, 503)
            return

        from DataAPI.TqSdkAPI import (FREQ_SEC_MAP, FREQ_LABEL_CN, CTqSdkAPI,
                                       TQ_ACCOUNT, TQ_PASSWORD, FUTURES_ALIASES)
        from tqsdk import TqApi, TqAuth
        from datetime import datetime

        # 确定周期
        if not sub_freq:
            sub_freq = _FUTURES_DUAL_FREQ_MAP.get(main_freq, "15s")
        main_freq_sec = FREQ_SEC_MAP.get(main_freq, 60)
        sub_freq_sec = FREQ_SEC_MAP.get(sub_freq, 15)

        display_key = f"{symbol} 双窗口({main_freq}/{sub_freq})"
        if _SSE_DEBUG:
            print(f"\n[{display_key}] ═══ SSE双窗口连接建立 ═══")

        # 发送 SSE 头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        api = None
        main_chan = None
        sub_chan = None
        try:
            # 别名解析
            symbol_upper = symbol.upper()
            if symbol_upper in FUTURES_ALIASES:
                symbol = FUTURES_ALIASES[symbol_upper]

            api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
            name = _get_futures_name(symbol)
            main_freq_label = main_freq
            sub_freq_label = sub_freq

            # 1. 查询选点状态
            saved_selection_date = ""
            main_start_time = start_time
            sub_start_time = start_time
            try:
                qualified_code = symbol
                col_meta = FREQ_TO_COL.get(main_freq, "")
                if col_meta and qualified_code in _saved_point_times:
                    saved_selection_date = _saved_point_times[qualified_code].get(col_meta, "").strip() or ""
                    # 如果外部没传start_time，从CSV读取选点
                    if main_start_time is None and saved_selection_date:
                        main_start_time = saved_selection_date
                # 下窗也查询选点
                sub_col_meta = FREQ_TO_COL.get(sub_freq, "")
                if sub_col_meta and qualified_code in _saved_point_times:
                    sub_saved = _saved_point_times[qualified_code].get(sub_col_meta, "").strip() or ""
                    if sub_start_time is None and sub_saved:
                        sub_start_time = sub_saved
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

            # 2. 拉取上窗历史 + chan分析
            if _SSE_DEBUG:
                print(f"[{display_key}] 拉取上窗({main_freq})历史K线...")
            main_result = init_chan_symbol(api, symbol, name, main_freq_sec, main_freq_label, start_time=main_start_time)
            main_chan, main_records, main_kl_type, _ = main_result
            main_kl_type = _get_kl_type(main_freq)
            if _SSE_DEBUG:
                print(f"[{display_key}] 上窗({main_freq}) chan.py: 合并K线={len(main_chan[main_kl_type].lst)}, "
                      f"笔={len(main_chan[main_kl_type].bi_list)}, 中枢={len(main_chan[main_kl_type].zs_list)}")

            # 3. 拉取下窗历史 + chan分析
            if _SSE_DEBUG:
                print(f"[{display_key}] 拉取下窗({sub_freq})历史K线...")
            sub_result = init_chan_symbol(api, symbol, name, sub_freq_sec, sub_freq_label, start_time=sub_start_time)
            sub_chan, sub_records, sub_kl_type, _ = sub_result
            sub_kl_type = _get_kl_type(sub_freq)
            # 缓存下窗 CChan 供 /api/dual_zs 访问（key 统一大写）
            _futures_analysis_cache[f"{symbol.upper()}:{sub_freq}"] = sub_chan
            if _SSE_DEBUG:
                print(f"[{display_key}] 下窗({sub_freq}) chan.py: 合并K线={len(sub_chan[sub_kl_type].lst)}, "
                      f"笔={len(sub_chan[sub_kl_type].bi_list)}, 中枢={len(sub_chan[sub_kl_type].zs_list)}")

            # 7. 提取初始快照
            t_snap = time.time()
            main_snapshot = _extract_realtime_snapshot(
                main_chan, main_kl_type, symbol, name, main_freq_label,
                saved_selection_date=saved_selection_date
            )
            sub_snapshot = _extract_realtime_snapshot(
                sub_chan, sub_kl_type, symbol, name, sub_freq_label,
                klines=None
            )
            # 期货双窗口：上窗 bis 的 fx_a_raw_dt/fx_b_raw_dt 是上层K线时间，
            # 需要换算成子级别K线时间，前端 calcRedRange 才能正确匹配
            _futures_red_range(main_snapshot, main_freq_sec, sub_freq_sec, sub_freq)

            # ★ 追加上下窗当前形成中的K线（与单窗口一致），让前端立即看到，且 tick 更新正确的 K 线
            _main_klines_for_init = api.get_kline_serial(symbol, main_freq_sec)
            _sub_klines_for_init = api.get_kline_serial(symbol, sub_freq_sec)
            # 上窗
            if _main_klines_for_init is not None and len(_main_klines_for_init) > 0:
                _lr = _main_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
                if _dns is not None:
                    _bdt = datetime.fromtimestamp(_dns / 1e9)
                    _bds = _bdt.strftime(_get_date_fmt(main_freq))
                    _ex = main_snapshot.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _bds:
                        _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                            'open': round(float(_lr.get('open', 0) or 0), 3),
                            'high': round(float(_lr.get('high', 0) or 0), 3),
                            'low': round(float(_lr.get('low', 0) or 0), 3),
                            'close': round(float(_lr.get('close', 0) or 0), 3),
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        _inherit_macd_for_preview_bar(_ex)
                        main_snapshot['meta']['kline_count'] = len(_ex)
            # 下窗
            if _sub_klines_for_init is not None and len(_sub_klines_for_init) > 0:
                _lr = _sub_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
                if _dns is not None:
                    _bdt = datetime.fromtimestamp(_dns / 1e9)
                    _bds = _bdt.strftime(_get_date_fmt(sub_freq))
                    _ex = sub_snapshot.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _bds:
                        _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                            'open': round(float(_lr.get('open', 0) or 0), 3),
                            'high': round(float(_lr.get('high', 0) or 0), 3),
                            'low': round(float(_lr.get('low', 0) or 0), 3),
                            'close': round(float(_lr.get('close', 0) or 0), 3),
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        _inherit_macd_for_preview_bar(_ex)
                        sub_snapshot['meta']['kline_count'] = len(_ex)
            if _SSE_DEBUG:
                print(f"[{display_key}] 初始快照提取: {time.time()-t_snap:.3f}s")

            # 8. 推送双窗口 init 事件
            init_data = {
                "main": main_snapshot,
                "sub": sub_snapshot,
            }
            init_str = json.dumps(init_data, ensure_ascii=False, allow_nan=False)
            self.wfile.write(f"event: init\ndata: {init_str}\n\n".encode("utf-8"))
            self.wfile.flush()
            if _SSE_DEBUG:
                print(f"[{display_key}] 推送init")

            # 缓存快照用于 tick 路径
            main_cached_snapshot = main_snapshot
            sub_cached_snapshot = sub_snapshot

            # 9. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照
            #
            # 策略：用系统壁钟（datetime.now()）判断K线周期是否结束，不再等天勤的
            # klines 序列推进信号。天勤免费版 klines 推进延迟约 7 秒，但 klines[-1]
            # 的 OHLC 在周期结束后已冻结，可以直接用于缠论计算。
            #
            # 流程：
            #   1. 壁钟确认 N-1 周期结束 → 立即取 klines[-1]/[-2] 做缠论
            #   2. 缠论计算完成后，N 周期的第一笔 tick 已到达 → 快照中直接显示 N
            #
            BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
            t_total = time.time()  # 总耗时起点（用于日志输出）
            if _SSE_DEBUG:
                print(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

            # 保存两个窗口的 klines 引用供实时更新使用
            main_klines = api.get_kline_serial(symbol, main_freq_sec)
            sub_klines = api.get_kline_serial(symbol, sub_freq_sec)

            # last_bar_dt_ns: klines[-1] 的时间戳，用于检测 klines 是否推进
            # last_processed_dt_ns: 已处理过的K线时间戳，防止同一根K线被重复处理
            main_last_bar_dt_ns = None
            main_last_processed_dt_ns = None
            sub_last_bar_dt_ns = None
            sub_last_processed_dt_ns = None
            last_debug_print = time.time()

            # 性能统计
            t_wait_total = 0.0
            t_tick_total = 0.0
            t_step_total = 0.0
            t_snapshot_total = 0.0
            t_push_total = 0.0
            loop_count = 0
            tick_count = 0
            step_count = 0
            last_perf_print = time.time()

            # ---- 定义单窗口K线处理函数（避免 continue 跳过另一个窗口） ----
            def _process_one_window(klines, chan, kl_type, freq_sec, freq_label,
                                     cached_snapshot, last_bar_dt_ns, last_processed_dt_ns,
                                     is_main, window_label):
                """处理单个窗口的K线检测，返回 (updated, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, need_tick)"""
                nonlocal last_debug_print, last_perf_print, loop_count, tick_count, step_count
                nonlocal t_wait_total, t_tick_total, t_step_total, t_snapshot_total, t_push_total
                if len(klines) == 0:
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
                last_row = klines.iloc[-1]
                dt_ns = last_row.get("datetime")
                if dt_ns is None:
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

                # 诊断
                if loop_count == 1 or (loop_count % 50 == 0):
                    chan_last_klu = None
                    try:
                        chan_kl_list = chan[kl_type]
                        if chan_kl_list.lst:
                            last_klc = chan_kl_list.lst[-1]
                            if last_klc.lst:
                                chan_last_klu = last_klc.lst[-1]
                    except Exception as e:
                        print(f"[警告] 异常: {type(e).__name__}: {e}")
                    tqsdk_last_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime('%H:%M:%S') if dt_ns else "None"
                    chan_last_dt = chan_last_klu.time.to_str()[:16] if chan_last_klu and hasattr(chan_last_klu, 'time') else "None"
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG-{window_label}] 循环#{loop_count} | "
                          f"tqsdk klines[-1]={tqsdk_last_dt} | "
                          f"chan kl_list[-1]={chan_last_dt} | "
                          f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

                # 初始化
                if last_bar_dt_ns is None:
                    last_bar_dt_ns = dt_ns
                    last_debug_print = now_ts
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DEBUG] 初始化 [{window_label}]: "
                          f"klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

                # 每60秒性能统计
                if now_ts - last_perf_print >= 60.0:
                    last_perf_print = now_ts
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [PERF] 循环{loop_count}次 | "
                          f"wait_update总计={t_wait_total:.1f}s | "
                          f"tick推送{tick_count}次总计={t_tick_total:.1f}s | "
                          f"step_load{step_count}次总计={t_step_total:.1f}s | "
                          f"快照提取总计={t_snapshot_total:.1f}s | "
                          f"SSE推送总计={t_push_total:.1f}s")
                    t_wait_total = 0.0; t_tick_total = 0.0; t_step_total = 0.0
                    t_snapshot_total = 0.0; t_push_total = 0.0
                    loop_count = 0; tick_count = 0; step_count = 0

                # 每2秒 DEBUG
                if now_ts - last_debug_print >= 2.0:
                    last_debug_print = now_ts
                    bar_dt = datetime.fromtimestamp(dt_ns / 1e9)
                    lag = now_ts - (dt_ns / 1e9 + freq_sec)
                    pushed = (dt_ns != last_bar_dt_ns)
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DEBUG-{window_label}] 壁钟={now.strftime('%H:%M:%S')} "
                          f"klines[-1]={bar_dt.strftime('%H:%M:%S')} "
                          f"过期={lag:+.1f}s 推进={pushed} "
                          f"O={last_row.get('open'):.1f} H={last_row.get('high'):.1f} "
                          f"L={last_row.get('low'):.1f} C={last_row.get('close'):.1f}")

                # --- K线完成检测 ---
                klines_pushed = (dt_ns != last_bar_dt_ns)

                if klines_pushed:
                    completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row
                    last_bar_dt_ns = dt_ns
                    bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] K线完成(klines推进) [{window_label}] "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")
                else:
                    bar_end_ts = (dt_ns / 1e9) + freq_sec + BAR_COMPLETION_BUFFER
                    if now_ts < bar_end_ts:
                        # 周期未结束 → tick更新（更新快照OHLC，稍后统一推送）
                        if cached_snapshot is not None:
                            try:
                                ex = cached_snapshot.get('klines', [])
                                if ex:
                                    o = round(float(last_row.get('open', 0) or 0), 3)
                                    h = round(float(last_row.get('high', 0) or 0), 3)
                                    l = round(float(last_row.get('low', 0) or 0), 3)
                                    c = round(float(last_row.get('close', 0) or 0), 3)
                                    ex[-1]['open'] = o; ex[-1]['high'] = h
                                    ex[-1]['low'] = l; ex[-1]['close'] = c
                                    closes = [k['close'] for k in ex]
                                    if len(closes) >= 26:
                                        ema12 = ema(closes, 12); ema26 = ema(closes, 26)
                                        for i in range(len(ex)):
                                            if i < len(ema12):
                                                ex[i]['dif'] = round(ema12[i] - ema26[i], 4)
                                        difs = [ex[i]['dif'] for i in range(len(ex))]
                                        dea = ema(difs, 9)
                                        for i in range(len(ex)):
                                            if i < len(dea):
                                                ex[i]['dea'] = round(dea[i], 4)
                                                ex[i]['macd'] = round(2 * (ex[i]['dif'] - dea[i]), 4)
                                    cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                            except Exception as e:
                                print(f"[警告] 异常: {type(e).__name__}: {e}")
                        return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, True
                    # 壁钟到期
                    completed_row = last_row
                    bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] K线完成(壁钟) [{window_label}]: "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")

                completed_dt_ns = completed_row.get("datetime")
                if completed_dt_ns is None:
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
                if completed_dt_ns == last_processed_dt_ns:
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
                last_processed_dt_ns = completed_dt_ns

                # 提取 OHLC
                o = float(completed_row.get("open", 0) or 0)
                h = float(completed_row.get("high", 0) or 0)
                l = float(completed_row.get("low", 0) or 0)
                cl = float(completed_row.get("close", 0) or 0)
                vol = int(completed_row.get("volume", 0) or 0)
                oi = float(completed_row.get("open_oi", 0) or 0)
                h = max(h, o, cl); l = min(l, o, cl)

                dt = datetime.fromtimestamp(completed_dt_ns / 1e9)
                bar_expected_end = (completed_dt_ns / 1e9) + freq_sec
                delay = now_ts - bar_expected_end
                source = "klines推进" if klines_pushed else "壁钟"

                code_key = f"{symbol}:{freq_sec}"
                new_bar = {"dt": dt, "open": round(o, 3), "high": round(h, 3),
                           "low": round(l, 3), "close": round(cl, 3),
                           "vol": vol, "amount": round(oi, 2)}

                last_records = CTqSdkAPI.get_last_n(1, symbol=code_key)
                updated = False
                t_step = 0.0
                if not last_records or last_records[0]["dt"] != dt:
                    CTqSdkAPI.append_bar(new_bar, symbol=code_key)
                    t_step_start = time.time()
                    try:
                        for _snapshot in chan.step_load():
                            pass
                    except Exception as e:
                        print(f"[{display_key}] {window_label} step_load 异常: {e}")
                    t_step = time.time() - t_step_start
                    t_step_total += t_step; step_count += 1
                    updated = True
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 完成新K线[{source}] [{window_label}]: "
                          f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                          f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                          f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

                # 提取完整快照
                if updated:
                    snapshot = _extract_realtime_snapshot(
                        chan, kl_type, symbol, name, freq_label,
                        saved_selection_date=saved_selection_date
                    )
                    if is_main:
                        _futures_red_range(snapshot, freq_sec, sub_freq_sec, sub_freq)
                    _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                    _next_ds = _next_dt.strftime(_get_date_fmt(freq_label))
                    _ex = snapshot.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _next_ds:
                        _next_c = round(cl, 3)
                        _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                            'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        _inherit_macd_for_preview_bar(_ex)
                        snapshot['meta']['kline_count'] = len(_ex)
                    if is_main:
                        _kl_list = chan[kl_type]
                        _date_fmt = _get_date_fmt(main_freq)
                        snapshot['white_hline'] = _calc_futures_white_hline(_kl_list, main_freq, _date_fmt)
                    cached_snapshot = snapshot

                return updated, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

            # ---- 主循环 ----
            while True:
                try:
                    t_wait_start = time.time()
                    api.wait_update(deadline=time.time() * 1e9 + 100_000_000)
                    t_wait = time.time() - t_wait_start
                    t_wait_total += t_wait
                except Exception as e:
                    print(f"[{display_key}] wait_update 异常: {e}")
                    time.sleep(0.5)
                    continue

                # 心跳检测
                try:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 客户端断开，退出循环")
                    break

                loop_count += 1
                now = datetime.now()
                now_ts = now.timestamp()

                # 处理上窗
                main_updated, main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns, main_need_tick = \
                    _process_one_window(main_klines, main_chan, main_kl_type, main_freq_sec, main_freq_label,
                                        main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns,
                                        is_main=True, window_label="上窗")

                # 处理下窗
                sub_updated, sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns, sub_need_tick = \
                    _process_one_window(sub_klines, sub_chan, sub_kl_type, sub_freq_sec, sub_freq_label,
                                        sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns,
                                        is_main=False, window_label="下窗")

                # 推送：tick模式或K线完成模式
                if main_need_tick or sub_need_tick:
                    # tick推送：统一发送双窗口数据
                    t_tick_start = time.time()
                    tick_data = {"main": main_cached_snapshot, "sub": sub_cached_snapshot}
                    tick_str = json.dumps(tick_data, ensure_ascii=False, allow_nan=False)
                    try:
                        self.wfile.write(f"event: update\ndata: {tick_str}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    t_tick_total += time.time() - t_tick_start
                    tick_count += 1
                    if tick_count == 1:
                        if _SSE_DEBUG:
                            print(f"[{display_key}] [DIAG] 首次tick推送: "
                              f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

                if main_updated or sub_updated:
                    t_snap_start = time.time()
                    update_data = {"main": main_cached_snapshot, "sub": sub_cached_snapshot}
                    update_str = json.dumps(update_data, ensure_ascii=False, allow_nan=False)
                    t_push_start = time.time()
                    try:
                        self.wfile.write(f"event: update\ndata: {update_str}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    t_push_total += time.time() - t_push_start
                    t_snapshot_total += time.time() - t_snap_start
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 推送更新: 快照提取={t_snapshot_total:.3f}s JSON序列化={(time.time()-t_snap_start)-t_push_total:.3f}s SSE写入={t_push_total:.3f}s")

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            import traceback
            print(f"[{display_key}] 连接异常: {e}")
            traceback.print_exc()
        finally:
            if api is not None:
                try:
                    api.close()
                except Exception as e:
                    print(f"[警告] 异常: {type(e).__name__}: {e}")
            try:
                CTqSdkAPI._records_by_symbol.pop(f"{symbol}:{main_freq_sec}", None)
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
            try:
                CTqSdkAPI._records_by_symbol.pop(f"{symbol}:{sub_freq_sec}", None)
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
            try:
                _futures_analysis_cache.pop(f"{symbol.upper()}:{sub_freq}", None)
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

    def _handle_sse_stream_single(self, symbol, freq="15s", start_time=None):
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
        from DataAPI.TqSdkAPI import (FREQ_SEC_MAP, FREQ_LABEL_CN, CTqSdkAPI,
                                       TQ_ACCOUNT, TQ_PASSWORD, FUTURES_ALIASES)
        from datetime import datetime

        api = None
        chan = None
        try:
            # 别名解析：支持 PTA→KQ.m@CZCE.TA 等短名称
            symbol_upper = symbol.upper()
            if symbol_upper in FUTURES_ALIASES:
                symbol = FUTURES_ALIASES[symbol_upper]

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
                # ★ 追加当前形成中的K线（klines[-1]），让前端立即看到新K线
                if klines is not None and len(klines) > 0:
                    _lr = klines.iloc[-1]; _dns = _lr.get('datetime')
                    if _dns is not None:
                        _bdt = datetime.fromtimestamp(_dns / 1e9)
                        _bds = _bdt.strftime(_get_date_fmt(freq))
                        _ex = init_data.get('klines', [])
                        if not _ex or _ex[-1]['date'] != _bds:
                            _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                                'open': round(float(_lr.get('open', 0) or 0), 3),
                                'high': round(float(_lr.get('high', 0) or 0), 3),
                                'low': round(float(_lr.get('low', 0) or 0), 3),
                                'close': round(float(_lr.get('close', 0) or 0), 3),
                                'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                            _inherit_macd_for_preview_bar(_ex)
                            init_data['meta']['kline_count'] = len(_ex)
                # 计算白色横虚线（初始快照，K线已确认状态）
                _kl_list = chan[kl_type]
                _date_fmt = _get_date_fmt(freq)
                init_data['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _date_fmt)
                init_str = json.dumps(init_data, ensure_ascii=False, allow_nan=False)
                self.wfile.write(f"event: init\ndata: {init_str}\n\n".encode("utf-8"))
                self.wfile.flush()
                cached_snapshot = init_data  # ★ 缓存完整快照，tick推送时更新最后一根K线OHLC
                if _SSE_DEBUG:
                    print(f"[{display_key}] ⑶ 推送init: "
                          f"K线{init_data['meta']['kline_count']}, "
                          f"笔{init_data['meta']['bi_count']}, "
                          f"中枢{init_data['meta']['zs_count']}, "
                          f"耗时 {time.time()-t0:.1f}s")
            except Exception as e:
                err = json.dumps({"error": f"快照提取失败: {e}", "symbol": symbol})
                self.wfile.write(f"event: init\ndata: {err}\n\n".encode("utf-8"))
                self.wfile.flush()
                return

            # === 3. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照 ===
            #
            # 策略：用系统壁钟（datetime.now()）判断K线周期是否结束，不再等天勤的
            # klines 序列推进信号。天勤免费版 klines 推进延迟约 7 秒，但 klines[-1]
            # 的 OHLC 在周期结束后已冻结，可以直接用于缠论计算。
            #
            # 流程：
            #   1. 壁钟确认 N-1 周期结束 → 立即取 klines[-1]/[-2] 做缠论
            #   2. 缠论计算完成后，N 周期的第一笔 tick 已到达 → 快照中直接显示 N
            #
            BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
            if _SSE_DEBUG:
                print(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

            # last_bar_dt_ns: klines[-1] 的时间戳，用于检测 klines 是否推进
            # last_processed_dt_ns: 已处理过的K线时间戳，防止同一根K线被重复处理
            last_bar_dt_ns = None
            last_processed_dt_ns = None
            last_debug_print = time.time()

            # 性能统计
            t_wait_total = 0.0
            t_tick_total = 0.0
            t_step_total = 0.0
            t_snapshot_total = 0.0
            t_push_total = 0.0
            loop_count = 0
            tick_count = 0
            step_count = 0
            last_perf_print = time.time()

            while True:
                try:
                    t_wait_start = time.time()
                    api.wait_update(deadline=time.time() * 1e9 + 100_000_000)
                    t_wait = time.time() - t_wait_start
                    t_wait_total += t_wait
                except Exception as e:
                    print(f"[{display_key}] wait_update 异常: {e}")
                    time.sleep(0.5)
                    continue

                # 心跳检测：前端断开后及时退出
                try:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 客户端断开，退出循环")
                    break

                loop_count += 1

                now = datetime.now()
                now_ts = now.timestamp()

                if len(klines) == 0:
                    continue

                last_row = klines.iloc[-1]
                dt_ns = last_row.get("datetime")
                if dt_ns is None:
                    continue

                # ★ 诊断：对比 tqsdk 实时 K 线和 chan 框架内部 K 线的时间差
                if loop_count == 1 or (loop_count % 50 == 0):
                    chan_last_klu = None
                    try:
                        chan_kl_list = chan[kl_type]
                        if chan_kl_list.lst:
                            last_klc = chan_kl_list.lst[-1]
                            if last_klc.lst:
                                chan_last_klu = last_klc.lst[-1]
                    except Exception as e:
                        print(f"[警告] 异常: {type(e).__name__}: {e}")
                    tqsdk_last_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime('%H:%M:%S') if dt_ns else "None"
                    chan_last_dt = chan_last_klu.time.to_str()[:16] if chan_last_klu and hasattr(chan_last_klu, 'time') else "None"
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] 循环#{loop_count} | "
                          f"tqsdk klines[-1]={tqsdk_last_dt} | "
                          f"chan kl_list[-1]={chan_last_dt} | "
                          f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

                # 初始化
                if last_bar_dt_ns is None:
                    last_bar_dt_ns = dt_ns
                    last_debug_print = now_ts
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DEBUG] 初始化: klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                    continue

                # 每60秒打印一次性能统计
                if now_ts - last_perf_print >= 60.0:
                    last_perf_print = now_ts
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [PERF] 循环{loop_count}次 | "
                          f"wait_update总计={t_wait_total:.1f}s | "
                          f"tick推送{tick_count}次总计={t_tick_total:.1f}s | "
                          f"step_load{step_count}次总计={t_step_total:.1f}s | "
                          f"快照提取总计={t_snapshot_total:.1f}s | "
                          f"SSE推送总计={t_push_total:.1f}s")
                    t_wait_total = 0.0
                    t_tick_total = 0.0
                    t_step_total = 0.0
                    t_snapshot_total = 0.0
                    t_push_total = 0.0
                    loop_count = 0
                    tick_count = 0
                    step_count = 0

                # ★ DEBUG: 每2秒打印一次当前状态
                if now_ts - last_debug_print >= 2.0:
                    last_debug_print = now_ts
                    bar_dt = datetime.fromtimestamp(dt_ns / 1e9)
                    lag = now_ts - (dt_ns / 1e9 + freq_sec)
                    pushed = (dt_ns != last_bar_dt_ns)
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DEBUG] 壁钟={now.strftime('%H:%M:%S')} "
                          f"klines[-1]={bar_dt.strftime('%H:%M:%S')} "
                          f"过期={lag:+.1f}s 推进={pushed} "
                          f"O={last_row.get('open'):.1f} H={last_row.get('high'):.1f} "
                          f"L={last_row.get('low'):.1f} C={last_row.get('close'):.1f}")

                # --- 检测上一根K线是否已完成 ---
                klines_pushed = (dt_ns != last_bar_dt_ns)

                if klines_pushed:
                    # klines 已推进 → 上一根K线（klines[-2]）已冻结，立即处理
                    completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row
                    last_bar_dt_ns = dt_ns
                    bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] K线完成(klines推进): "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")
                else:
                    # klines 未推进 → 用壁钟判断当前K线周期是否已结束
                    bar_end_ts = (dt_ns / 1e9) + freq_sec + BAR_COMPLETION_BUFFER
                    if now_ts < bar_end_ts:
                        # 周期未结束，更新缓存快照的最后一根K线OHLC后推送完整格式
                        t_tick_start = time.time()
                        try:
                            if cached_snapshot is not None:
                                ex = cached_snapshot.get('klines', [])
                                if ex:
                                    o = round(float(last_row.get('open', 0) or 0), 3)
                                    h = round(float(last_row.get('high', 0) or 0), 3)
                                    l = round(float(last_row.get('low', 0) or 0), 3)
                                    c = round(float(last_row.get('close', 0) or 0), 3)
                                    ex[-1]['open'] = o
                                    ex[-1]['high'] = h
                                    ex[-1]['low'] = l
                                    ex[-1]['close'] = c
                                    # ★ 实时计算最后一根K线的MACD，避免前端跳变
                                    closes = [k['close'] for k in ex]
                                    if len(closes) >= 26:
                                        ema12 = ema(closes, 12)
                                        ema26 = ema(closes, 26)
                                        for i in range(len(ex)):
                                            if i < len(ema12):
                                                ex[i]['dif'] = round(ema12[i] - ema26[i], 4)
                                        difs = [ex[i]['dif'] for i in range(len(ex))]
                                        dea = ema(difs, 9)
                                        for i in range(len(ex)):
                                            if i < len(dea):
                                                ex[i]['dea'] = round(dea[i], 4)
                                                ex[i]['macd'] = round(2 * (ex[i]['dif'] - ex[i]['dea']), 4)
                                    cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                                    tick_str = json.dumps(cached_snapshot, ensure_ascii=False, allow_nan=False)
                                    self.wfile.write(f"event: update\ndata: {tick_str}\n\n".encode("utf-8"))
                                    self.wfile.flush()
                                    if tick_count == 0:
                                        if _SSE_DEBUG:
                                            print(f"[{display_key}] [DIAG] 首次tick推送: "
                                              f"tqsdk klines[-1]={now.strftime('%H:%M:%S')} | "
                                              f"更新最后一根K线OHLC O={o} H={h} L={l} C={c} | "
                                              f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")
                                    t_tick_total += time.time() - t_tick_start
                                    tick_count += 1
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            return
                        except Exception as e:
                            print(f"[警告] 异常: {type(e).__name__}: {e}")
                        continue
                    # 壁钟到期，当前K线（klines[-1]）已冻结
                    completed_row = last_row
                    bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] K线完成(壁钟): "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")

                completed_dt_ns = completed_row.get("datetime")
                if completed_dt_ns is None:
                    continue

                # 防止重复处理同一根K线
                if completed_dt_ns == last_processed_dt_ns:
                    continue
                last_processed_dt_ns = completed_dt_ns

                # 提取 OHLC
                o = float(completed_row.get("open", 0) or 0)
                h = float(completed_row.get("high", 0) or 0)
                l = float(completed_row.get("low", 0) or 0)
                cl = float(completed_row.get("close", 0) or 0)
                vol = int(completed_row.get("volume", 0) or 0)
                oi = float(completed_row.get("open_oi", 0) or 0)
                h = max(h, o, cl)
                l = min(l, o, cl)

                dt = datetime.fromtimestamp(completed_dt_ns / 1e9)
                bar_expected_end = (completed_dt_ns / 1e9) + freq_sec
                delay = now_ts - bar_expected_end
                source = "klines推进" if klines_pushed else "壁钟"

                code_key = f"{symbol}:{freq_sec}"
                new_bar = {
                    "dt": dt, "open": round(o, 3), "high": round(h, 3),
                    "low": round(l, 3), "close": round(cl, 3),
                    "vol": vol, "amount": round(oi, 2),
                }
                t_append = time.time()

                last_records = CTqSdkAPI.get_last_n(1, symbol=code_key)
                t_step = 0.0
                if not last_records or last_records[0]["dt"] != dt:
                    CTqSdkAPI.append_bar(new_bar, symbol=code_key)
                    t_step_start = time.time()
                    try:
                        for _snapshot in chan.step_load():
                            pass
                    except Exception as e:
                        print(f"[{display_key}] step_load 异常: {e}")
                    t_step = time.time() - t_step_start
                    t_step_total += t_step
                    step_count += 1

                if _SSE_DEBUG:
                    print(f"[{display_key}] 完成新K线[{source}]: "
                      f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                      f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                      f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                      f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

                # 推送快照（此时 klines[-1] 已推进到 N 周期，快照中自然包含 N 的实时OHLC）
                t_snap_start = time.time()
                try:
                    update_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                       saved_selection_date=saved_selection_date)
                    # ★ 用 completed_time + freq_sec 计算下一根K线时间（不用klines[-1]，因为壁钟触发时klines未推进）
                    _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                    _next_ds = _next_dt.strftime(_get_date_fmt(freq_label))
                    _ex = update_data.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _next_ds:
                        _next_c = round(cl, 3)
                        _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                            'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        _inherit_macd_for_preview_bar(_ex)
                        update_data['meta']['kline_count'] = len(_ex)
                    # K线确认后，计算白色横虚线（不在tick推送路径计算）
                    _kl_list = chan[kl_type]
                    _date_fmt = _get_date_fmt(freq)
                    update_data['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _date_fmt)
                    cached_snapshot = update_data  # ★ 更新缓存
                    t_snap = time.time() - t_snap_start
                    t_snapshot_total += t_snap
                    update_str = json.dumps(update_data, ensure_ascii=False, allow_nan=False)
                    t_serialize = time.time() - t_snap_start - t_snap
                    t_push_start = time.time()
                    self.wfile.write(f"event: update\ndata: {update_str}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    t_push = time.time() - t_push_start
                    t_push_total += t_push
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 推送更新: 快照提取={t_snap:.3f}s "
                          f"JSON序列化={t_serialize:.3f}s SSE写入={t_push:.3f}s "
                          f"(append+step_load={time.time()-t_append:.3f}s)")
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
                except Exception as e:
                    print(f"[警告] 异常: {type(e).__name__}: {e}")
            # 清理该连接的K线缓存
            try:
                CTqSdkAPI._records_by_symbol.pop(f"{symbol}:{freq_sec}", None)
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

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
        t_json = time.time()
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        json_time = time.time() - t_json
        body_kb = len(body) / 1024
        # 细粒度计时：序列化 / 发送HTTP头 / 写入socket 三个环节分别计时
        if body_kb > 100 or json_time > 0.1:
            print(f"[耗时] JSON序列化: {json_time:.3f}s ({body_kb:.0f}KB)")
        t_header = time.time()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        header_time = time.time() - t_header
        t_write = time.time()
        self.wfile.write(body)
        write_time = time.time() - t_write
        # 汇总：任一环节超 50ms 就打印明细，方便定位瓶颈
        if json_time > 0.05 or header_time > 0.05 or write_time > 0.05:
            print(f"[耗时] 响应明细 序列化={json_time:.3f}s 头部={header_time:.3f}s 写入={write_time:.3f}s ({body_kb:.0f}KB)")

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
        .scan-header-right {
            display: flex; align-items: center; gap: 0;
            flex-shrink: 0;
        }
        .scan-minimize {
            font-size: 16px; color: #8892b0; cursor: pointer;
            display: inline-block; width: 22px; text-align: center;
            line-height: 1; margin-right: 2px;
            font-family: monospace;
        }
        .scan-minimize:hover { color: #64ffda; }
        .scan-panel.minimized .scan-body { display: none; }
        .scan-panel.minimized {
            width: auto; min-width: 200px;
            max-height: none;
        }
        .scan-save-btn {
            font-size: 11px; color: #8892b0; cursor: pointer;
            padding: 2px 8px; border: 1px solid #0f3460; border-radius: 3px;
            background: transparent; margin-right: 8px;
        }
        .scan-save-btn:hover { color: #e94560; border-color: #e94560; }
        .scan-save-btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .scan-col-chk {
            width: 18px !important; flex-shrink: 0 !important;
            text-align: center !important;
        }
        .scan-col-chk input[type="checkbox"] {
            width: 12px; height: 12px; cursor: pointer;
            accent-color: #e94560; vertical-align: middle;
            filter: grayscale(0.5) brightness(0.8);
        }
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
        .scan-col-freq {
            width: 40px !important; flex-shrink: 0 !important;
            text-align: left !important; color: #e94560 !important;
            font-size: 11px !important; white-space: nowrap !important;
            padding-left: 6px !important;
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
            overflow: hidden !important;
        }
        .scan-bsp-tags {
            display: inline-flex !important; gap: 2px !important;
            flex-shrink: 0 !important; flex-wrap: wrap !important;
            vertical-align: middle !important;
            max-height: 28px !important; overflow: hidden !important;
        }
        .scan-bsp-tag {
            display: inline-block !important;
            padding: 1px 3px !important; border-radius: 3px !important;
            font-size: 9px !important; font-weight: 600 !important;
            white-space: nowrap !important; flex-shrink: 0 !important;
            line-height: 1.2 !important;
        }
        .scan-bsp-tag.buy { background: rgba(255, 68, 68, 0.25); color: #FF4444; }
        .scan-bsp-tag.sell { background: rgba(0, 221, 0, 0.2); color: #00DD00; }
        .scan-bsp-tag.fx-d { background: rgba(255, 120, 120, 0.2); color: #ff7878; }
        .scan-bsp-tag.fx-weak { background: rgba(180, 180, 180, 0.2); color: #b0b0b0; }
        .scan-bsp-tag.fx-strong { background: rgba(255, 165, 0, 0.2); color: #ffa500; }
        .scan-bsp-tag.fx-strongest { background: rgba(255, 68, 68, 0.3); color: #ff4444; font-weight: 600; }
        .scan-bsp-tag.scan-bsp-more { background: rgba(255,255,255,0.1); color: #8892b0; font-weight: 400; }
        /* 均线分类标签：类0(最强,红) → 类8(最弱,绿) */
        .scan-bsp-tag.ma-cat0 { background: rgba(244, 67, 54, 0.25); color: #F44336; font-weight: 600; }
        .scan-bsp-tag.ma-cat1 { background: rgba(255, 87, 34, 0.22); color: #FF5722; }
        .scan-bsp-tag.ma-cat2 { background: rgba(255, 152, 0, 0.2); color: #FF9800; }
        .scan-bsp-tag.ma-cat3 { background: rgba(255, 193, 7, 0.2); color: #FFC107; }
        .scan-bsp-tag.ma-cat4 { background: rgba(255, 235, 59, 0.2); color: #FFEB3B; }
        .scan-bsp-tag.ma-cat5 { background: rgba(198, 255, 0, 0.2); color: #C6FF00; }
        .scan-bsp-tag.ma-cat6 { background: rgba(118, 255, 3, 0.2); color: #76FF03; }
        .scan-bsp-tag.ma-cat7 { background: rgba(0, 230, 118, 0.22); color: #00E676; }
        .scan-bsp-tag.ma-cat8 { background: rgba(0, 200, 83, 0.25); color: #00C853; font-weight: 600; }
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
        #chart-main, #chart-sub {
            width: 100%; position: relative; cursor: crosshair; overflow: hidden;
        }
        #chart-main { height: 50%; border-bottom: 2px solid #0f3460; }
        #chart-sub { height: 50%; }
        #chart-main canvas, #chart-sub canvas { display: block; }
        #chart-main.dual-active { outline: 2px solid rgba(233, 69, 96, 0.5); outline-offset: -2px; }
        #chart-sub.dual-active { outline: 2px solid rgba(233, 69, 96, 0.5); outline-offset: -2px; }
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
        /* BSP过滤设置对话框 */
        .bsp-filter-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 14px;
        }
        .bsp-filter-label {
            display: flex; align-items: center; gap: 8px; cursor: pointer;
            font-size: 13px; color: #a8b2d1; padding: 6px 10px; border-radius: 4px;
            background: #1a1a2e; transition: background 0.2s;
            white-space: nowrap; overflow: visible;
        }
        .bsp-filter-label:hover { background: #0f3460; }
        .bsp-filter-label input[type="checkbox"] { accent-color: #e94560; }
        /* 设置抽屉（右侧滑入） */
        .settings-drawer-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.35); z-index: 9999;
            opacity: 0; visibility: hidden; transition: opacity 0.25s, visibility 0.25s;
        }
        .settings-drawer-overlay.show { opacity: 1; visibility: visible; }
        .settings-drawer {
            position: fixed; top: 0; right: 0; bottom: 0;
            width: 360px; max-width: 92vw;
            background: #16213e; border-left: 1px solid #0f3460;
            box-shadow: -8px 0 32px rgba(0,0,0,0.4);
            z-index: 10000;
            transform: translateX(100%); transition: transform 0.28s ease;
            display: flex; flex-direction: column;
        }
        .settings-drawer.show { transform: translateX(0); }
        .settings-drawer-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 14px 18px; border-bottom: 1px solid #0f3460;
        }
        .settings-drawer-title { font-size: 14px; font-weight: 600; color: #e0e0e0; }
        .settings-drawer-close {
            font-size: 20px; color: #8892b0; cursor: pointer; line-height: 1;
            padding: 0 4px;
        }
        .settings-drawer-close:hover { color: #e94560; }
        .settings-drawer-body {
            flex: 1; overflow-y: auto; padding: 16px 18px;
        }
        .settings-drawer-footer {
            padding: 12px 18px; border-top: 1px solid #0f3460;
            display: flex; justify-content: flex-end; gap: 8px;
        }
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
                    <input type="text" id="stock-code-input" placeholder="如 SZZS、GZMT、600519" onkeydown="onInputKeydown(event)" onfocus="clearInput();showHistory()" oninput="onInputChange()" />
                    <span class="stock-input-clear" id="input-clear" onclick="clearInput();document.getElementById('stock-code-input').focus()" style="display:none">&times;</span>
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
                <span class="date-arrow" id="date-arrow-left" onclick="dateStep(-1)" title="前一天">&#9664;</span>
                <span class="date-input-wrap">
                    <input type="date" id="goto-date-input" onchange="handleDateChange()" onkeydown="handleDateKeydown(event)" onblur="handleDateBlur()" oninput="handleDateInput(event)" />
                    <span class="date-weekday" id="date-weekday"></span>
                </span>
                <span class="date-arrow" id="date-arrow-right" onclick="dateStep(1)" title="后一天">&#9654;</span>
            </div>
            <button class="btn" id="btn-dual" onclick="toggleDualWindow()">双窗口</button>
                <button class="btn" id="btn-fx" onclick="toggleOverlay('fx')">分型</button>
                <button class="btn active" id="btn-bi" onclick="toggleOverlay('bi')">笔</button>
                <button class="btn" id="btn-seg" onclick="toggleOverlay('seg')">线段</button>
                <button class="btn active" id="btn-zs" onclick="toggleOverlay('zs')">中枢</button>
                <button class="btn active" id="btn-bsp" onclick="toggleOverlay('bsp')">买卖点</button>
            <button class="btn" id="btn-scan" onclick="startScanZxg()" title="扫描股票买卖点">股票扫描</button>
            <button class="btn" id="btn-stats" onclick="toggleStats()">统计</button>
            <button class="btn-icon" id="btn-refresh" title="刷新股票名称、板块文件、PE-TTM和指数归属" onclick="refreshStockNames()">
                <svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.96 7.96 0 0012 4C7.58 4 4.01 7.58 4.01 12S7.58 20 12 20c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
            </button>
            <span id="refresh-status" style="color:#a8b2d1;font-size:11px;margin-left:4px;display:none;"></span>
            <button class="btn-icon" id="btn-settings" title="设置" onclick="openBspSettings()">
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

    <div class="stats-panel" id="stats-panel">
        <div class="stats-title">缠论统计</div>
        <div id="stats-content"></div>
    </div>
    <div class="scan-panel" id="scan-panel">
        <div class="scan-header">
            <span class="scan-title" id="scan-title">买卖点扫描</span>
            <button class="scan-save-btn" id="scan-save-btn" onclick="saveScanToZxg()" disabled title="保存勾选到通达信+同花顺自选股">保存到自选</button>
            <span class="scan-status" id="scan-status"></span>
            <span class="scan-header-right">
            <span class="scan-minimize" onclick="toggleScanMinimize()" title="最小化面板">-</span>
            <span class="scan-close" onclick="closeScanPanel()">&times;</span>
            </span>
        </div>
        <div class="scan-body" id="scan-body">
            <div class="scan-empty">点击上方"股票扫描"按钮开始</div>
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
        <div class="annotation-menu-item danger" id="annotation-menu-del-all" onclick="annotationDeleteAllGlobal()">删除全部</div>
        <div class="annotation-menu-divider" id="annotation-menu-divider"></div>
        <div class="annotation-menu-item" id="annotation-menu-restart" onclick="cancelSelectedPoint()" style="display:none;">取消选点</div>
        <div class="annotation-menu-item" id="annotation-menu-replay" onclick="annotationReplayToHere()">复盘至此</div>
        <div class="annotation-menu-divider" id="annotation-menu-divider2"></div>
        <div class="annotation-menu-item" id="annotation-menu-mirror" onclick="toggleMirrorMode()">反转视图</div>
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
    <!-- 股票扫描模式选择对话框 -->
    <div class="annotation-dialog" id="scan-mode-dialog">
        <div class="annotation-dialog-box">
            <div class="annotation-dialog-title">股票扫描</div>
            <div id="scan-source-section">
            <div style="margin-bottom:10px;font-size:12px;color:#8892b0;">扫描来源（可多选）</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="checkbox" name="scan-source" value="zxg" checked style="accent-color:#e94560;" />
                    自选股
                </label>
                <label id="label-page-index" style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="checkbox" name="scan-source" value="page_index" style="accent-color:#e94560;" />
                    成分股
                </label>
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="checkbox" name="scan-source" value="tdxhy2" style="accent-color:#e94560;" />
                    板块指数2
                </label>
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="checkbox" name="scan-source" value="tdxhy3" style="accent-color:#e94560;" />
                    板块指数3
                </label>
            </div>
            <div style="display:flex;gap:6px;margin-bottom:14px;">
                <button onclick="scanSourceSelectAll()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">全选</button>
                <button onclick="scanSourceSelectNone()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">取消</button>
            </div>
            </div>
            <div id="scan-recent-row" style="display:flex;align-items:center;gap:8px;margin-bottom:14px;font-size:12px;color:#8892b0;">
                <span>最近</span>
                <input type="number" id="scan-recent-days" value="1" min="1" max="100" style="width:50px;height:24px;background:#1a1a2e;border:1px solid #2a2a3e;color:#e0e0e0;border-radius:4px;text-align:center;font-size:13px;padding:0 4px;" />
                <span>根</span>
            </div>
            <div id="scan-freq-row" style="margin-bottom:14px;">
                <div style="margin-bottom:6px;font-size:12px;color:#8892b0;">扫描周期</div>
                <div style="display:flex;gap:16px;font-size:13px;color:#a8b2d1;">
                    <label style="cursor:pointer;"><input type="radio" name="scan-freq" value="w" style="accent-color:#e94560;margin-right:4px;" />周K</label>
                    <label style="cursor:pointer;"><input type="radio" name="scan-freq" value="d" checked style="accent-color:#e94560;margin-right:4px;" />日K</label>
                    <label style="cursor:pointer;"><input type="radio" name="scan-freq" value="30m" style="accent-color:#e94560;margin-right:4px;" />30分</label>
                    <label style="cursor:pointer;"><input type="radio" name="scan-freq" value="5m" style="accent-color:#e94560;margin-right:4px;" />5分</label>
                </div>
            </div>
            <div style="margin-bottom:10px;font-size:12px;color:#8892b0;">扫描模式</div>
            <div style="display:flex;gap:16px;font-size:13px;color:#a8b2d1;margin-bottom:14px;">
                <label style="cursor:pointer;"><input type="radio" name="scan-mode" value="ann" checked onchange="updateScanRecentDisabled()" style="accent-color:#e94560;margin-right:4px;" />标注</label>
                <label style="cursor:pointer;"><input type="radio" name="scan-mode" value="ma" onchange="updateScanRecentDisabled()" style="accent-color:#e94560;margin-right:4px;" />均线</label>
                <label style="cursor:pointer;"><input type="radio" name="scan-mode" value="fx_d" onchange="updateScanRecentDisabled()" style="accent-color:#e94560;margin-right:4px;" />底分型</label>
                <label style="cursor:pointer;"><input type="radio" name="scan-mode" value="bsp" onchange="updateScanRecentDisabled()" style="accent-color:#e94560;margin-right:4px;" />买/卖点</label>
            </div>
            <div class="annotation-dialog-btns">
                <button class="annotation-dialog-btn" onclick="scanModeDialogCancel()">取消</button>
                <button class="annotation-dialog-btn primary" onclick="scanModeDialogConfirm()">确认</button>
            </div>
        </div>
    </div>
    <!-- 设置抽屉（买卖点过滤 + 均线周期） -->
    <div class="settings-drawer-overlay" id="bsp-filter-overlay" onclick="closeBspSettings()"></div>
    <aside class="settings-drawer" id="bsp-filter-dialog">
        <div class="settings-drawer-header">
            <span class="settings-drawer-title">显示设置</span>
            <span class="settings-drawer-close" onclick="closeBspSettings()">&times;</span>
        </div>
        <div class="settings-drawer-body">
            <div style="margin-bottom:8px;font-size:12px;color:#8892b0;">买卖点类型（可多选）</div>
            <div class="bsp-filter-grid">
                <label class="bsp-filter-label">
                    <input type="checkbox" name="bsp-filter" value="0" checked onchange="onBspFilterChange(this)" />
                    0类（中枢震荡）
                </label>
                <label class="bsp-filter-label">
                    <input type="checkbox" name="bsp-filter" value="1" checked onchange="onBspFilterChange(this)" />
                    1类（趋势背驰）
                </label>
                <label class="bsp-filter-label">
                    <input type="checkbox" name="bsp-filter" value="2" checked onchange="onBspFilterChange(this)" />
                    2类（二买/二卖）
                </label>
                <label class="bsp-filter-label">
                    <input type="checkbox" name="bsp-filter" value="3" checked onchange="onBspFilterChange(this)" />
                    3类（三买/三卖）
                </label>
            </div>
            <div style="display:flex;gap:6px;margin-bottom:18px;">
                <button onclick="bspFilterSelectAll()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">全选</button>
                <button onclick="bspFilterSelectNone()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">取消</button>
            </div>

            <div style="margin-bottom:8px;font-size:12px;color:#8892b0;">均线周期（可多选，斐波那契数列）</div>
            <div class="bsp-filter-grid" id="ma-periods-grid">
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="5" onchange="onMaPeriodChange(this)" /><span style="color:#FFFFFF">●</span> MA5</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="13" onchange="onMaPeriodChange(this)" /><span style="color:#F77F00">●</span> MA13</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="21" onchange="onMaPeriodChange(this)" /><span style="color:#FCBF49">●</span> MA21</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="34" onchange="onMaPeriodChange(this)" /><span style="color:#90BE6D">●</span> MA34</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="55" onchange="onMaPeriodChange(this)" /><span style="color:#22D3EE">●</span> MA55</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="89" onchange="onMaPeriodChange(this)" /><span style="color:#3B82F6">●</span> MA89</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="144" onchange="onMaPeriodChange(this)" /><span style="color:#A8A8A8">●</span> MA144</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="233" onchange="onMaPeriodChange(this)" /><span style="color:#8822DD">●</span> MA233</label>
            </div>
            <div style="display:flex;gap:6px;margin-bottom:14px;">
                <button onclick="maPeriodsSelectAll()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">全选</button>
                <button onclick="maPeriodsSelectNone()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">取消</button>
            </div>

            <div style="margin-bottom:8px;font-size:12px;color:#8892b0;">其他</div>
            <div class="bsp-filter-grid">
                <label class="bsp-filter-label">
                    <input type="checkbox" name="show-bi-idx" onchange="onShowBiIdxChange(this)" />
                    显示笔索引编号
                </label>
            </div>
        </div>
    </aside>
    <script>
    (function() {
        "use strict";
        let chartData = null, canvas, ctx;
        let showBi = true, showFx = false, showZs = true, showSeg = false, showBsp = true, showBiIdx = false;
        // BSP买卖点类型过滤：默认全部显示（0,1,2,3 对应 bs_type 配置）
        let bspFilter = { '0': true, '1': true, '2': true, '3': true };
        // 均线周期：选中的周期集合，默认空（不显示均线）
        const MA_PERIODS = [5, 13, 21, 34, 55, 89, 144, 233];
        const MA_COLORS = { 5:'#FFFFFF', 13:'#F77F00', 21:'#FCBF49', 34:'#90BE6D', 55:'#22D3EE', 89:'#3B82F6', 144:'#A8A8A8', 233:'#8822DD' };
        let maPeriods = {};  // {5: true, 13: true, ...}
        // 从 localStorage 恢复叠加层开关状态
        function loadOverlaySettings() {
            try {
                const raw = localStorage.getItem('chan_overlay_settings');
                if (!raw) return;
                const s = JSON.parse(raw);
                if (typeof s.showBi === 'boolean') showBi = s.showBi;
                if (typeof s.showFx === 'boolean') showFx = s.showFx;
                if (typeof s.showZs === 'boolean') showZs = s.showZs;
                if (typeof s.showSeg === 'boolean') showSeg = s.showSeg;
                if (typeof s.showBsp === 'boolean') showBsp = s.showBsp;
                if (typeof s.showBiIdx === 'boolean') showBiIdx = s.showBiIdx;
                if (s.bspFilter && typeof s.bspFilter === 'object') {
                    for (var k in s.bspFilter) { bspFilter[k] = s.bspFilter[k]; }
                }
                if (s.maPeriods && typeof s.maPeriods === 'object') {
                    for (var p in s.maPeriods) { maPeriods[p] = s.maPeriods[p]; }
                }
            } catch(e) {}
        }
        // 保存叠加层开关状态到 localStorage
        function saveOverlaySettings() {
            try {
                const s = {
                    showBi: showBi, showFx: showFx,
                    showZs: showZs, showSeg: showSeg, showBsp: showBsp, showBiIdx: showBiIdx,
                    bspFilter: bspFilter,
                    maPeriods: maPeriods
                };
                localStorage.setItem('chan_overlay_settings', JSON.stringify(s));
            } catch(e) {}
        }
        function getShowMa() { return Object.keys(maPeriods).some(function(p){ return maPeriods[p]; }); }
        // 根据保存的设置更新按钮 UI 状态
        function applyOverlayButtonStates() {
            document.getElementById("btn-bi").classList.toggle("active", showBi);
            document.getElementById("btn-fx").classList.toggle("active", showFx);
            document.getElementById("btn-zs").classList.toggle("active", showZs);
            document.getElementById("btn-seg").classList.toggle("active", showSeg);
            document.getElementById("btn-bsp").classList.toggle("active", showBsp);
        }
        // 启动时加载保存的设置
        loadOverlaySettings();
        // 频率→秒数映射
        const FREQ_SEC_MAP_JS = { 'w': 604800, 'd': 86400, '30m': 1800, '5m': 300, '1m': 60, '15s': 15 };
        const PADDING = { top: 20, right: 22, bottom: 36, left: 10 };
        const VOL_RATIO = 0.2, GAP = 12;
        const MACD_TEXT_HEIGHT = 14;
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
        let lastFuturesFreq = '1m'; // 期货上下文上次使用的周期（同类切换继承）
        // 双窗口状态
        let isDualWindow = false;
        let dualSubData = null;
        let dualSubFreq = '';
        let dualSubViewOffset = 0, dualSubViewCount = 377;
        let dualSubMouseX = -1, dualSubMouseY = -1;
        let mainCanvas, mainCtx, subCanvas, subCtx;
        // 反转视图模式：将上涨行情反转为下跌、下跌反转为上涨（缠论做空视角）
        let _isMirrorMode = false;
        // 取消选点菜单项是否可用（有选点且非双窗口/非复盘模式）
        let _restartEnabled = false;
        let dualSubIsDragging = false, dualSubDragStartX = 0, dualSubDragStartOffset = 0;
        let dualSubMouseDownX = 0, dualSubMouseDownY = 0; // 底部窗口点击坐标
        let _subCurrentGlobalIdx = -1; // 底部窗口当前鼠标指向的全局索引
        let _subClipText = ""; // 底部窗口当前K线信息文本
        let dualHighlightRange = null; // {startIdx, endIdx} 下面窗口高亮范围（灰框）
        let dualRedRange = null;     // {beforeStart, beforeEnd, afterStart, afterEnd} 下面窗口红框范围
        let dualOffscreenState = false; // 状态A：当前鼠标指向的K线对应区间在下面窗口视口外
        let dualNewZsData = null;       // 双窗口新模式：红框内笔计算的新中枢数据 {zs: [...], zs_stars: [...]}
        let dualShowNewZs = false;      // 双窗口新模式：是否绘制新中枢（替代原线段/中枢/买卖点）
        let dualNewZsLeftDate = "";     // 双窗口新模式：上次请求的红框左边界日期（用于去重）
        let dualNewZsRightDate = "";    // 双窗口新模式：上次请求的红框右边界日期（用于去重）
        let dualNewZsFailedKey = "";    // 双窗口新模式：失败请求去重，避免同一红框反复请求
        let activeDualWindow = 'main';   // 当前激活的窗口：'top' 或 'bottom'，控制底部滚动条作用于哪个窗口
        let _ctrlPressed = false;         // Ctrl键是否按下（用于红框计算优化）

        // 文字标注状态
        let annotations = [];          // 当前标注列表: [{date, text, y_offset}]
        let _annotationTargetDate = ""; // 右键点击的K线日期
        let _annotationTargetY = 0;     // 右键点击的Y坐标（图表内相对坐标，用于标注定位）
        let _annotationTargetX = 0;     // 右键点击的X坐标（用于菜单定位）
        let _annotationClickTarget = null; // 右键点击命中的标注对象 {date, text, y_offset}，null表示未命中
        let _annotationEditOldText = "";   // 编辑模式下被修改的旧文字
        let _annotationDialogMode = "add"; // "add" 或 "edit"

        // ===== 日期输入框：按周期切换 date / datetime-local =====
        const INTRADAY_FREQS_JS = ["30m", "5m", "1m", "15s"];
        function isIntradayFreq(freq) { return INTRADAY_FREQS_JS.indexOf(freq) >= 0; }

        // K线日期 → 输入框格式
        // K线日期: "2026/07/02" / "2026/07/02 10:35" / "2026/07/02 10:35:00"
        // date: "2026-07-02"  /  datetime-local: "2026-07-02T10:35"
        function klineDateToInput(klineDate, freq) {
            if (!klineDate) return "";
            var d = klineDate.replace(/\//g, "-");
            if (isIntradayFreq(freq)) {
                var dt = d.slice(0, 19);       // "YYYY-MM-DD HH:MM:SS"（15秒含秒，分钟级不越界）
                return dt.replace(" ", "T");
            }
            return d.slice(0, 10);
        }

        // 输入框值 → 后端API格式
        // date: "2026-07-02" / datetime-local: "2026-07-02T10:35"
        // API: "2026-07-02" / "2026-07-02 10:35"
        function inputDateToApi(inputVal, freq) {
            if (!inputVal) return "";
            if (isIntradayFreq(freq)) return inputVal.replace("T", " ").replace(/-/g, "/");
            return inputVal.slice(0, 10).replace(/-/g, "/");
        }

        // 切换输入框 type 属性（date ↔ datetime-local）
        function updateDateInputType() {
            var input = document.getElementById("goto-date-input");
            var weekday = document.getElementById("date-weekday");
            var isIntra = isIntradayFreq(currentFreq);
            var oldVal = input.value;
            if (isIntra) {
                input.type = "datetime-local";
                input.step = (currentFreq === "15s") ? "15" : "60";
                // 股票：限定盘中时间 09:00-15:59；期货：全天
                var isStock = chartData && chartData.meta && chartData.meta.symbol && !isFuturesCode(chartData.meta.symbol);
                if (isStock) {
                    input.min = "1990-01-01T09:00";
                    input.max = "2099-12-31T15:59";
                } else {
                    input.min = "1990-01-01T00:00";
                    input.max = "2099-12-31T23:59";
                }
                if (currentFreq === "15s") {
                    input.style.width = "190px";
                    if (weekday) weekday.style.right = "28px";
                } else {
                    input.style.width = "170px";
                    if (weekday) weekday.style.right = "28px";
                }
                if (oldVal && oldVal.indexOf("T") < 0) oldVal = oldVal + "T09:30";
            } else {
                input.type = "date";
                input.step = "1";
                input.min = "1990-01-01";
                input.max = "2099-12-31";
                input.style.width = "130px";
                if (oldVal && oldVal.indexOf("T") >= 0) oldVal = oldVal.slice(0, 10);
                if (weekday) weekday.style.right = "28px";
            }
            input.value = oldVal;
            // datetime-local：picker 打开时记录原始值
            if (isIntra) {
                input.onfocus = function() {
                    var v = input.value;
                    if (!v) return;
                    _datePickerInteracted = false;
                    _datePickerInputCount = 0;
                    _dateFocusOriginal = v;
                };
            } else {
                input.onfocus = null;
            }
            // 箭头提示
            var la = document.getElementById("date-arrow-left");
            var ra = document.getElementById("date-arrow-right");
            if (la) la.title = (currentFreq === "d") ? "前一天" : "前一根";
            if (ra) ra.title = (currentFreq === "d") ? "后一天" : "后一根";
        }

        // 实时模式（期货/期指 SSE 推送）
        let isRealtimeMode = false;       // 是否处于实时模式
        let realtimeSymbol = null;        // 实时模式下当前品种代码
        let realtimeFreq = null;          // 实时模式下当前周期
        let realtimeStartTime = null;     // 实时模式下选点起始时间
        let realtimeEventSource = null;   // SSE EventSource 对象
        let realtimeConnected = false;    // SSE 是否已连接

        // 辅助函数：30分钟K线显示时间
        function getKlineEndTime(dateStr, showSeconds) {
            const parts = dateStr.split(/[-\/\s:]/);
            const yy = parts[0].slice(2);
            const mm = parts[1];
            const dd = parts[2];
            const hh = parts[3];
            const min = parts[4];
            const ss = parts[5];
            if (showSeconds && ss !== undefined) {
                return `${yy}/${mm}/${dd} ${hh}:${min}:${ss}`;
            }
            return `${yy}/${mm}/${dd} ${hh}:${min}`;
        }
        // 双窗口：上面周期 -> 下面周期映射
        function getDualSubFreq(mainFreq) {
            // 股票周期映射
            if (mainFreq === 'w') return 'd';
            if (mainFreq === 'd') return '30m';
            if (mainFreq === '30m') return '5m';
            // 期货周期映射（股票5m无对应，期货5m→1m）
            if (mainFreq === '5m') return '1m';
            if (mainFreq === '1m') return '15s';
            return null; // 5m(股票)/15s(期货)无对应
        }
        // 双窗口：获取上面窗口某根K线对应的灰框边界（子级别K线时间字符串）
        // 通用方案：利用相邻K线时间，不依赖周期长度假设
        //   期货：K线时间=开始时间。左边界=当前时间X，右边界=(下一根时间Y - bottom_sec)
        //   股票：K线时间=结束时间。左边界=(上一根时间Y + bottom_sec)，右边界=当前时间X
        //         日期型K线（如d/w）无时分秒，解析时视为当日结束时刻(23:59:59)
        // 返回 {start: string|null, end: string|null}，null 表示边界在数据范围外
        function getMainKlineTimeRange(kline, idx, klines, isFutures, subFreq) {
            const subSec = FREQ_SEC_MAP_JS[subFreq];
            if (!subSec) return null;
            const dateLen = kline.date.length;  // 19=含秒, 16=含分, 10=仅日期
            function fmt(d) {
                const y = d.getFullYear();
                const mo = String(d.getMonth() + 1).padStart(2, '0');
                const da = String(d.getDate()).padStart(2, '0');
                if (dateLen >= 19) {
                    const h = String(d.getHours()).padStart(2, '0');
                    const mi = String(d.getMinutes()).padStart(2, '0');
                    const s = String(d.getSeconds()).padStart(2, '0');
                    return `${y}/${mo}/${da} ${h}:${mi}:${s}`;
                } else if (dateLen >= 16) {
                    const h = String(d.getHours()).padStart(2, '0');
                    const mi = String(d.getMinutes()).padStart(2, '0');
                    return `${y}/${mo}/${da} ${h}:${mi}`;
                }
                return `${y}/${mo}/${da}`;
            }
            function parse(ds) {
                // 日期型K线（仅日期）→ 视为当日结束时刻 23:59:59.999
                if (ds.length === 10) return new Date(ds.replace(/\//g, "-") + "T23:59:59");
                return new Date(ds.replace(/\//g, "-").replace(" ", "T"));
            }
            if (isFutures) {
                // 期货：左边界 = 当前K线时间X（精确匹配）
                const start = kline.date;
                // 右边界 = (下一根K线时间Y - sub_sec) 记为Z
                let end = null;
                if (idx + 1 < klines.length) {
                    const nextD = parse(klines[idx + 1].date);
                    const endD = new Date(nextD.getTime() - subSec * 1000);
                    end = fmt(endD);
                }
                return { start, end };
            } else {
                // 股票：左边界 = (上一根K线时间Y + sub_sec) 记为Z
                let start = null;
                if (idx > 0) {
                    const prevD = parse(klines[idx - 1].date);
                    const startD = new Date(prevD.getTime() + subSec * 1000);
                    start = fmt(startD);
                }
                // 右边界 = 当前K线时间X（精确匹配）
                const end = kline.date;
                return { start, end };
            }
        }
        // 双窗口：根据上面窗口鼠标位置计算下面窗口高亮范围
        function calcGrayRange(topMouseX) {
            if (!isDualWindow || !dualSubData || !chartData) return null;
            const area = getChartArea();
            const klines = getVisibleKlines();
            if (!klines.length) return null;
            const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
            const barStep = area.w / effectiveCount;
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            const idx = Math.floor((topMouseX - area.x + subPixelOffset) / barStep);
            if (idx < 0 || idx >= klines.length) return null;
            const mainKline = klines[idx];
            const subKlines = dualSubData.klines;
            let startIdx = -1, endIdx = -1;
            // 方案B：优先使用 sub_kl_times（后端多级别CChan返回的子级别K线时间列表）
            if (mainKline.sub_kl_times && mainKline.sub_kl_times.length > 0) {
                const subTimes = mainKline.sub_kl_times;
                const firstTime = subTimes[0];
                const lastTime = subTimes[subTimes.length - 1];
                for (let i = 0; i < subKlines.length; i++) {
                    const bk = subKlines[i];
                    if (bk.date >= firstTime && startIdx === -1) startIdx = i;
                    if (bk.date <= lastTime) endIdx = i;
                }
            } else {
                // 通用方案：利用相邻K线时间精确计算灰框边界
                const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
                const timeRange = getMainKlineTimeRange(mainKline, idx, klines, isFutures, dualSubFreq);
                if (!timeRange) return null;
                // 左边界：用 >= 匹配（字符串比较对 ISO 日期天然正确）
                if (timeRange.start) {
                    for (let i = 0; i < subKlines.length; i++) {
                        if (subKlines[i].date >= timeRange.start) { startIdx = i; break; }
                    }
                }
                // 右边界：用前缀匹配（兼容 d→30m 等跨格式场景），回退 <=
                if (timeRange.end) {
                    const endLen = timeRange.end.length;
                    for (let i = subKlines.length - 1; i >= 0; i--) {
                        if (subKlines[i].date.slice(0, endLen) === timeRange.end) { endIdx = i; break; }
                    }
                    if (endIdx === -1) {
                        for (let i = 0; i < subKlines.length; i++) {
                            if (subKlines[i].date <= timeRange.end) endIdx = i;
                        }
                    }
                }
                // 边界在数据范围外：用首/尾替代
                if (timeRange.start === null && startIdx === -1) startIdx = 0;
                if (timeRange.end === null && endIdx === -1) endIdx = subKlines.length - 1;
            }
            // 下面窗口数据中没有匹配的K线（上面K线日期超出了下面数据范围）
            if (startIdx === -1) {
                // 用上面K线日期与下面数据首尾日期比较来判断方向
                const topDate = new Date(mainKline.date.replace(/\//g, "-").replace(" ", "T"));
                const subFirstDate = new Date(subKlines[0].date.replace(/\//g, "-").replace(" ", "T"));
                const subLastDate = new Date(subKlines[subKlines.length - 1].date.replace(/\//g, "-").replace(" ", "T"));
                if (topDate < subFirstDate) {
                    return { startIdx: -1, endIdx: -1, isVisible: false, isLeft: true, isRight: false };
                } else if (topDate > subLastDate) {
                    return { startIdx: -1, endIdx: -1, isVisible: false, isLeft: false, isRight: true };
                }
                return null;
            }
            // 判断高亮范围是否在下面窗口当前视口内
            const subGlobalStart = Math.max(0, Math.floor(dualSubViewOffset));
            const subGlobalEnd = subGlobalStart + dualSubViewCount;
            const isVisible = (startIdx < subGlobalEnd && endIdx >= subGlobalStart);
            const isLeft = endIdx < subGlobalStart;   // 整个区间在视口左边
            const isRight = startIdx >= subGlobalEnd;
            let redRange = null;
            if (_ctrlPressed) {
                try {
                    redRange = calcRedRange(mainKline, subKlines, startIdx, endIdx);
                } catch (e) {
                    console.error("[红框] calcRedRange异常:", e);
                    window._lastCalcRedRangeError = String(e);
                }
            }
            return { startIdx, endIdx, isVisible, isLeft, isRight, redRange };
        }

        // 双窗口红框：鼠标指向上面K线所属笔的外沿区间（分型左肩→右肩）
        // 注意：使用 chartData.bis（复数），JSON 字段名是 "bis"
        function calcRedRange(mainKline, subKlines, grayStart, grayEnd) {
            console.log("[红框] === 进入 calcRedRange ===");
            console.log("[红框] mainKline.date=" + mainKline.date + " grayStart=" + grayStart + " grayEnd=" + grayEnd);
            console.log("[红框] chartData.bis=" + (chartData && chartData.bis ? "长度" + chartData.bis.length : "null"));
            console.log("[红框] subKlines.length=" + subKlines.length);
            if (!chartData || !chartData.bis || !chartData.bis.length) {
                console.log("[红框] ❌ chartData 或 chartData.bis 为空，返回null");
                window._lastRedFrameStatus = { state: "SKIP", reason: "chartData或bis为空" };
                updateRedFrameDebug();
                return null;
            }
            const d = mainKline.date;
            let bi = null;
            // 找到mainKline所属的笔（交界处归属右边）
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
                console.log("[红框] ❌ 未找到所属笔，mainKline.date=" + d + " 不在任何笔的[sdt, edt)范围内");
                window._lastRedFrameStatus = { state: "SKIP", reason: "未找到所属笔", topDate: d, biCount: chartData.bis.length };
                updateRedFrameDebug();
                return null;
            }
            const aDt = bi.fx_a_sub_dt || bi.fx_a_raw_dt, bDt = bi.fx_b_sub_dt || bi.fx_b_raw_dt;
            console.log("[红框] 边界值: fx_a_sub_dt='" + (bi.fx_a_sub_dt || "(空)") + "' fx_b_sub_dt='" + (bi.fx_b_sub_dt || "(空)") + "' fx_a_raw_dt='" + (bi.fx_a_raw_dt || "(空)") + "' fx_b_raw_dt='" + (bi.fx_b_raw_dt || "(空)") + "'");
            console.log("[红框] 最终使用: aDt='" + aDt + "' bDt='" + bDt + "'");
            if (!aDt || !bDt) {
                console.log("[红框] ❌ aDt='" + (aDt || "") + "' bDt='" + (bDt || "") + "' 为空");
                console.log("[红框]    bi.sdt=" + bi.sdt + " bi.edt=" + bi.edt + " bi.direction=" + bi.direction);
                window._lastRedFrameStatus = { state: "SKIP", reason: "fx_a或fx_b为空", sdt: bi.sdt, edt: bi.edt };
                updateRedFrameDebug();
                return null;
            }
            // fx_a_sub_dt / fx_b_sub_dt 是后端从分型原始K线对应的次级别序列边界直接算出的
            // 双窗口下直接就是次级别K线时间，>= / <= 精确匹配即可
            const aLen = aDt.length, bLen = bDt.length;
            let aIdx = -1, bIdx = -1;
            const subFirstDate = subKlines[0].date.slice(0, aLen);
            const subLastDate = subKlines[subKlines.length - 1].date.slice(0, bLen);
            for (let i = 0; i < subKlines.length; i++) {
                const bk = subKlines[i];
                // A: 红框左边界（次级别第一根）
                if (aIdx === -1 && bk.date.slice(0, aLen) >= aDt) aIdx = i;
                // B: 红框右边界（次级别最后一根）
                if (bk.date.slice(0, bLen) <= bDt) bIdx = i;
            }
            // 参照灰框处理：笔区间完全在底部数据范围之外 → 不显示红框，返回null
            if (aIdx === -1 && bIdx === -1) {
                if (aDt > subLastDate) {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间在底部数据右侧", aDt: aDt, bottomLast: subLastDate };
                } else if (bDt < subFirstDate) {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间在底部数据左侧", bDt: bDt, bottomFirst: subFirstDate };
                } else {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间无匹配", aDt: aDt, bDt: bDt };
                }
                updateRedFrameDebug();
                return null;
            }
            // 部分重叠：aIdx 或 bIdx 为 -1 时，截断到可见范围
            if (aIdx === -1) aIdx = 0;
            if (bIdx === -1) bIdx = subKlines.length - 1;
            if (aIdx > bIdx) {
                window._lastRedFrameStatus = { state: "SKIP", reason: "aIdx>bIdx", aIdx: aIdx, bIdx: bIdx };
                updateRedFrameDebug();
                return null;
            }
            // 红框时间：使用下方K线时间（精确到分钟），确保30m/5m图表显示完整时间
            const leftDate = subKlines[aIdx].date;
            const rightDate = subKlines[bIdx].date;
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
            // 共享周期：30m 始终启用
            document.getElementById('btn-30m').disabled = false;
            // 5m: 股票双窗口→禁用(无下级)，期货双窗口→启用(5m→1m)
            if (isDualWindow && isFutures) {
                document.getElementById('btn-5m').disabled = false;
                document.getElementById('btn-15s').disabled = true;  // 期货双窗口：15s永远禁用
            } else if (isDualWindow && !isFutures) {
                document.getElementById('btn-5m').disabled = true;
            } else {
                document.getElementById('btn-5m').disabled = false;
            }
            // 同步 active 状态
            document.querySelectorAll('.freq-btn').forEach(b => b.classList.remove('active'));
            const activeBtn = document.getElementById('btn-' + currentFreq);
            if (activeBtn) activeBtn.classList.add('active');
        }

        // 判断是否为期货/期指代码
        function isFuturesCode(code) {
            return code.includes('KQ.m@') || code.includes('KQ.i@') || /^[A-Z]+\.[A-Z]/.test(code);
        }
        // 保存当前状态到 localStorage（仅股票，仅单窗口非复盘模式）
        function saveLastState() {
            if (!chartData || !chartData.meta) return;
            if (isDualWindow) return;  // 双窗口不保存
            if (chartData.meta.is_replay) return;  // 复盘模式不保存
            if (chartData.meta.market === 'futures') return;  // 期货不保存
            const state = {
                code: chartData.meta.symbol,
                freq: currentFreq,
                name: chartData.meta.name
            };
            try { localStorage.setItem('lastCodeFreq', JSON.stringify(state)); } catch(e) {}
        }
        // 从 localStorage 加载上次状态，仅股票有效
        function loadLastCodeFreq() {
            try {
                const raw = localStorage.getItem('lastCodeFreq');
                if (!raw) return null;
                const state = JSON.parse(raw);
                if (!state.code || !state.freq) return null;
                if (isFuturesCode(state.code)) return null;  // 排除期货残留
                return state;
            } catch(e) { return null; }
        }

        async function init() {
            try {
                // 先尝试从 localStorage 恢复上次状态
                const savedState = loadLastCodeFreq();
                if (savedState) {
                    // 有保存的股票状态，用初始数据占位后立即异步加载
                    chartData = %%CHART_DATA%%;
                    document.getElementById("stock-code-input").value = savedState.code;
                    // 设置初始周期
                    if (savedState.freq) {
                        currentFreq = savedState.freq;
                        lastStockFreq = savedState.freq;
                    }
                    initCanvas();
                    updateSlider();
                    updateFreqButtonStates(false);
                    updateRestartBtn();
                    updateDualBtn();
                    // 异步加载保存的股票数据
                    document.getElementById("loading").classList.remove("hidden");
                    fetch("/api/stock?code=" + encodeURIComponent(savedState.code) + "&freq=" + savedState.freq)
                        .then(resp => {
                            if (!resp.ok) throw new Error("恢复失败");
                            return resp.json();
                        })
                        .then(data => {
                            chartData = data;
                            saveHistory(savedState.code, data.meta.name);
                            document.getElementById("stock-name").textContent = chartData.meta.name;
                            document.getElementById("stock-code").textContent = chartData.meta.symbol;
                            document.title = "缠论分析 - " + chartData.meta.name;
                            let returnedFreq;
                            if (data.meta.freq === "5分钟") returnedFreq = "5m";
                            else if (data.meta.freq === "30分钟") returnedFreq = "30m";
                            else if (data.meta.freq === "周线") returnedFreq = "w";
                            else returnedFreq = "d";
                            currentFreq = returnedFreq;
                            lastStockFreq = currentFreq;
                            updateDateInputType();
                            updateFreqButtonStates(false);
                            viewCount = 377;
                            adjustViewForSavedPoint();
                            viewOffset = Math.max(0, chartData.klines.length - viewCount);
                            if (chartData.klines.length < viewCount) viewOffset = 0;
                            applyOverlayButtonStates();
                            initialized = true;
                            updateRestartBtn();
                            updateDualBtn();
                            const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                            document.getElementById("goto-date-input").value = lastDate;
                            updateWeekday();
                            render();
                            document.getElementById("loading").classList.add("hidden");
                            generateStats();
                            loadAnnotations();
                            // 断开期货SSE（如果有）
                            disconnectRealtime();
                        })
                        .catch(err => {
                            console.error("恢复上次状态失败，回退到默认:", err);
                            // 回退到默认上证指数
                            document.getElementById("stock-code-input").value = "";
                            initDefault();
                        });
                    return;
                }
                // 无保存状态，默认加载上证指数
                initDefault();
            } catch (err) {
                console.error("初始化失败:", err);
                document.getElementById("loading").classList.add("hidden");
                document.getElementById("error").classList.remove("hidden");
            }
        }

        function initDefault() {
            chartData = %%CHART_DATA%%;
            document.getElementById("stock-name").textContent = chartData.meta.name;
            document.getElementById("stock-code").textContent = chartData.meta.symbol;
            document.title = "缠论分析 - " + chartData.meta.name;
            initCanvas();
            updateSlider();
            if (chartData.meta.freq === "5分钟") currentFreq = "5m";
            else if (chartData.meta.freq === "30分钟") currentFreq = "30m";
            else if (chartData.meta.freq === "周线") currentFreq = "w";
            else currentFreq = "d";
            updateDateInputType();
            lastStockFreq = currentFreq;
            updateFreqButtonStates(false);
            viewCount = 377;
            adjustViewForSavedPoint();
            applyOverlayButtonStates();
            viewOffset = Math.max(0, chartData.klines.length - viewCount);
            if (chartData.klines.length < viewCount) viewOffset = 0;
            initialized = true;
            updateRestartBtn();
            updateDualBtn();
            const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
            document.getElementById("goto-date-input").value = lastDate;
            updateWeekday();
            render();
            document.getElementById("loading").classList.add("hidden");
            generateStats();
            loadAnnotations();
        }

        function initCanvas() {
            const container = document.getElementById("chart-container");
            canvas = document.createElement("canvas");
            container.appendChild(canvas); ctx = canvas.getContext("2d");
            mainCanvas = canvas; mainCtx = ctx;
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
                // 双窗口模式下不支持双击选点
                if (isDualWindow && clickedOnKline) {
                    showDualToast("双窗口模式，不支持选点");
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
                        ? "/api/futures_manual_select_point?symbol=" + encodeURIComponent(code) + "&freq=" + freq + "&bi_idx=" + clickedBiIdx
                : "/api/stocks_manual_select_point?code=" + encodeURIComponent(code) + "&freq=" + freq + "&bi_idx=" + clickedBiIdx;
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
                                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
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
                            updateDateInputType();
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
                            const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
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
                if (isDualWindow && dualOffscreenState && dualHighlightRange && dualSubData) {
                    // 状态A：让下面窗口平移到对应区间
                    const hr = dualHighlightRange;
                    if (hr.startIdx >= 0 && hr.endIdx >= 0) {
                        const centerIdx = (hr.startIdx + hr.endIdx) / 2;
                        const totalKlines = dualSubData.klines.length;
                        let newOffset = Math.round(centerIdx - dualSubViewCount / 2);
                        // 左边不够：左对齐
                        if (newOffset < 0) newOffset = 0;
                        // 右边不够：右对齐（最后一根K线贴右边缘）
                        const maxOffset = Math.max(0, totalKlines - dualSubViewCount);
                        if (newOffset > maxOffset) newOffset = maxOffset;
                        dualSubViewOffset = newOffset;
                        // 重新计算高亮范围（区间已移入视口，应该变为isVisible=true）
                        dualHighlightRange = calcGrayRange(mouseX);
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
                const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
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
                if (mainCanvas) {
                    mainCanvas.width = w * dpr; mainCanvas.height = hTop * dpr;
                    mainCanvas.style.width = w + "px"; mainCanvas.style.height = hTop + "px";
                    mainCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
                }
                if (subCanvas) {
                    subCanvas.width = w * dpr; subCanvas.height = hBottom * dpr;
                    subCanvas.style.width = w + "px"; subCanvas.style.height = hBottom + "px";
                    subCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
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
            return { x: PADDING.left, y: PADDING.top + chartH + MACD_TEXT_HEIGHT,
                     w: totalW - rightGap, h: macdChartH };
        }

        function getMacdTextArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top + chartH,
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

        // 反转视图：对数据做镜像变换（价格取负 + high/low互换 + 方向取反）
        // 变换后所有绘制函数自动正确：涨K线变跌K线、MACD红柱变绿柱、笔/中枢方向自动镜像
        function _mirrorChartData(data) {
            if (!data) return data;
            // 浅拷贝顶层，klines/bis等数组深拷贝（不能修改原始缓存）
            var m = Object.assign({}, data);
            // K线: open→-open, close→-close, high↔low互换取负, macd/dif/dea取负
            m.klines = (data.klines || []).map(function(k) {
                var nk = Object.assign({}, k);
                nk.open = -k.open;
                nk.close = -k.close;
                nk.high = -k.low;   // 原low取负 → 新high
                nk.low = -k.high;   // 原high取负 → 新low
                if (k.dif !== undefined) nk.dif = -k.dif;
                if (k.dea !== undefined) nk.dea = -k.dea;
                if (k.macd !== undefined) nk.macd = -k.macd;
                return nk;
            });
            // 笔: direction up↔down, 价格取负, high↔low互换
            m.bis = (data.bis || []).map(function(b) {
                var nb = Object.assign({}, b);
                nb.direction = b.direction === 'up' ? 'down' : 'up';
                nb.fx_a_price = -b.fx_a_price;
                nb.fx_b_price = -b.fx_b_price;
                nb.high = -b.low;
                nb.low = -b.high;
                return nb;
            });
            // 分型: mark G↔D, price取负, high↔low互换
            m.fxs = (data.fxs || []).map(function(f) {
                var nf = Object.assign({}, f);
                nf.mark = f.mark === 'G' ? 'D' : 'G';
                nf.price = -f.price;
                nf.high = -f.low;
                nf.low = -f.high;
                return nf;
            });
            // 中枢: zg↔zd互换取负, gg↔dd互换取负, dir up↔down
            m.zs = (data.zs || []).map(function(z) {
                var nz = Object.assign({}, z);
                nz.zg = -z.zd;
                nz.zd = -z.zg;
                nz.gg = -z.dd;
                nz.dd = -z.gg;
                nz.dir = z.dir === 'up' ? 'down' : 'up';
                return nz;
            });
            // 线段: direction up↔down, 价格取负, high↔low互换
            m.segs = (data.segs || []).map(function(s) {
                var ns = Object.assign({}, s);
                ns.direction = s.direction === 'up' ? 'down' : 'up';
                ns.begin_price = -s.begin_price;
                ns.end_price = -s.end_price;
                ns.high = -s.low;
                ns.low = -s.high;
                return ns;
            });
            // 买卖点: is_buy取反, 价格取负, high↔low互换
            m.bsps = (data.bsps || []).map(function(b) {
                var nb = Object.assign({}, b);
                nb.is_buy = !b.is_buy;
                nb.price = -b.price;
                nb.high = -b.low;
                nb.low = -b.high;
                return nb;
            });
            // 中枢星标: price取负, mark G↔D, color red↔green
            m.zs_stars = (data.zs_stars || []).map(function(s) {
                var ns = Object.assign({}, s);
                ns.price = -s.price;
                ns.mark = s.mark === 'G' ? 'D' : 'G';
                ns.color = s.color === 'red' ? 'green' : 'red';
                return ns;
            });
            // 白色水平线: price取负
            if (data.white_hline) {
                m.white_hline = Object.assign({}, data.white_hline);
                m.white_hline.price = -data.white_hline.price;
            }
            return m;
        }

        // 反转模式下的价格显示：取绝对值
        function _fmtPrice(p) {
            return (_isMirrorMode ? Math.abs(p) : p).toFixed(2);
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
            const result = map.dateToGlobalIdx[date];
            if (result === undefined && window._dualZsDebugCount === undefined) {
                window._dualZsDebugCount = 0;
            }
            if (result === undefined && window._dualZsDebugCount < 3) {
                console.log("[dateToGlobalIdx] 未匹配日期: '" + date + "', 可用日期样本: " + Object.keys(map.dateToGlobalIdx).slice(0, 3).join(", "));
                window._dualZsDebugCount++;
            }
            return result;
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
            canvas = mainCanvas; ctx = mainCtx;
            _renderChart(chartData, currentFreq, viewOffset, viewCount, mouseX, mouseY, null, null);
        }

        function renderTop() {
            if (!chartData || !mainCtx) return;
            canvas = mainCanvas; ctx = mainCtx;
            updateActiveWindowClass();
            _renderChart(chartData, currentFreq, viewOffset, viewCount, mouseX, mouseY, null, null);
            // 上面窗口渲染完后，计算下面窗口高亮并重绘下面窗口
            // 注意：_renderChart 内部会临时覆盖全局变量然后恢复，
            // 所以这里全局变量已恢复为上面窗口的值，calcGrayRange 可以正确使用
            updateDualHighlight();
        }

        function renderBottom() {
            if (!dualSubData || !subCtx) return;
            updateDualNewZs();  // 双窗口新模式：检查红框完整性，决定是否请求新中枢
            updateActiveWindowClass();
            const _savedCanvas = canvas, _savedCtx = ctx;
            canvas = subCanvas; ctx = subCtx;
            window._isRenderingBottom = true;  // 标记：下面窗口渲染中，drawCrosshair 不更新 OHLC
            _renderChart(dualSubData, dualSubFreq, dualSubViewOffset, dualSubViewCount, dualSubMouseX, dualSubMouseY, dualHighlightRange, dualRedRange);
            window._isRenderingBottom = false;
            canvas = _savedCanvas; ctx = _savedCtx;
        }

        function _renderChart(data, freq, vOffset, vCount, mX, mY, highlightRange, redRange) {
            if (!data || !ctx) return;
            // 反转视图模式：对数据做镜像变换（不修改原始缓存，仅影响渲染）
            if (_isMirrorMode) {
                data = _mirrorChartData(data);
            }
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
            drawGrid(area, priceRange);
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
            drawMacd(klinesToDraw, volArea, macdRange, barStep, barWidth / 2, subPixelOffset);
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
            if (getShowMa()) {
                try { drawMaLines(klinesToDraw, area, priceRange, barStep, subPixelOffset); }
                catch (e) { console.error("[drawMaLines错误]", e); }
            }
            if (showBi) drawBiLines(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showFx) drawFxMarkers(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            // 双窗口新模式：红框出现后立即进入新中枢模式。
            // 请求返回前也先隐藏原中枢/线段/买卖点，避免红框出现后仍显示旧结构。
            const isSubNewZs = (data === dualSubData && dualShowNewZs);
            if (showZs && !isSubNewZs) drawZs(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showSeg && !isSubNewZs) drawSegLines(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showBsp && !isSubNewZs) drawBspMarkers(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (isSubNewZs) drawDualNewZs(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawWhiteHLine(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawAnnotations(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawViewportHighLow(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            _overlayData = null;
            drawCrosshair(klinesToDraw, area, priceRange, volArea, macdRange, barStep, macdTextArea, subPixelOffset);
            drawPriceAxis(area, priceRange); drawMacdAxis(volArea, macdRange);
            drawDateAxis(klinesToDraw, barStep, subPixelOffset);
            if (_overlayData) {
                if (_overlayData.rightPrice !== undefined) {
                    const labelW = 50;
                    ctx.fillStyle = "#dcdcdc"; ctx.fillRect(area.x + area.w + 2, _overlayData.rightY - 10, labelW, 20);
                    ctx.fillStyle = "#333"; ctx.font = "11px monospace"; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
                    ctx.fillText(_overlayData.rightPrice, area.x + area.w + 6, _overlayData.rightY + 4);
                }
                if (_overlayData.bottomText) {
                    const d = _overlayData;
                    ctx.fillStyle = "#dcdcdc";
                    ctx.fillRect(d.bottomX, d.bottomY, d.bottomW + d.bottomPad * 2, d.bottomH);
                    ctx.fillStyle = "#333"; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
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
                        const dateParts = highlightCenterDate.split(/[-\/]/);
                        shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                    } else {
                        const dateParts = highlightCenterDate.split(/[-\/]/);
                        shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                    }
                    const d = new Date(highlightCenterDate.replace(/\//g, "-").replace(" ", "T"));
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
            if (!isDualWindow || !dualSubData) return;
            if (mouseX >= 0) {
                dualHighlightRange = calcGrayRange(mouseX);
                // 只有按住 Ctrl 键时才计算红框（耗资源操作），否则只显示灰框
                dualRedRange = (_ctrlPressed && dualHighlightRange) ? dualHighlightRange.redRange : null;
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

        // 双窗口新模式：红框出现后，请求用红框内笔计算新中枢
        function updateDualNewZs() {
            console.log("[updateDualNewZs] 进入: isDualWindow=" + isDualWindow + " dualSubData=" + !!dualSubData + " dualHighlightRange=" + !!dualHighlightRange);
            if (!isDualWindow || !dualSubData || !dualHighlightRange) {
                console.log("[updateDualNewZs] 条件1失败: isDualWindow/dualSubData/dualHighlightRange 为空");
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            const rr = dualHighlightRange.redRange;
            console.log("[updateDualNewZs] redRange=" + !!rr + " highlightRange keys=" + Object.keys(dualHighlightRange).join(","));
            if (!rr) {
                console.log("[updateDualNewZs] 条件2失败: redRange为空");
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            const aIdx = rr.aIdx;
            const bIdx = rr.bIdx;
            console.log("[updateDualNewZs] aIdx=" + aIdx + " bIdx=" + bIdx);
            if (aIdx === undefined || bIdx === undefined) {
                console.log("[updateDualNewZs] 条件3失败: aIdx或bIdx为undefined");
                return;
            }
            // 红框对应的灰框区间不在当前下面窗口视口内时，不切换新中枢
            console.log("[updateDualNewZs] isVisible=" + dualHighlightRange.isVisible);
            if (!dualHighlightRange.isVisible) {
                console.log("[updateDualNewZs] 条件4失败: 不在视口内");
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            // 红框左右边界时间（子级别K线格式，传给后端由 _red_range_bi_sequence 找笔）
            const subKlines = dualSubData.klines;
            const leftDate = subKlines[aIdx].date;
            const rightDate = subKlines[bIdx].date;
            console.log("[updateDualNewZs] leftDate=" + leftDate + " rightDate=" + rightDate + " freq=" + dualSubFreq);
            const requestKey = dualSubFreq + ":" + leftDate + ":" + rightDate;
            if (dualNewZsFailedKey === requestKey) {
                console.log("[updateDualNewZs] 条件5失败: 请求已失败过, key=" + requestKey);
                return;
            }
            if (dualShowNewZs && dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                console.log("[updateDualNewZs] 条件6: 已缓存相同请求, 跳过");
                return;
            }
            console.log("[updateDualNewZs] >>> 发送fetch请求到 /api/red_range_zs");
            dualNewZsLeftDate = leftDate;
            dualNewZsRightDate = rightDate;
            dualShowNewZs = true;
            dualNewZsData = null;
            const code = dualSubData.meta.symbol;
            const isReplay = dualSubData.meta && dualSubData.meta.is_replay;
            let url = "/api/red_range_zs?code=" + encodeURIComponent(code) + "&freq=" + dualSubFreq + "&left_date=" + encodeURIComponent(leftDate) + "&right_date=" + encodeURIComponent(rightDate);
            if (isReplay) {
                const endDate = document.getElementById("goto-date-input").value;
                url += "&end_date=" + encodeURIComponent(endDate);
            }
            fetch(url)
                .then(resp => resp.json())
                .then(data => {
                    if (data.error) {
                        console.error("[dual_zs] 后端错误:", data.error);
                        if (dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                            dualNewZsFailedKey = requestKey;
                            dualShowNewZs = false;
                            dualNewZsData = null;
                            renderBottom();
                        }
                        return;
                    }
                    if (dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                        dualNewZsFailedKey = "";
                        dualNewZsData = data;
                        dualShowNewZs = true;
                        renderBottom();
                    }
                })
                .catch(err => {
                    console.error("[dual_zs] 请求失败:", err);
                    if (dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                        dualNewZsFailedKey = requestKey;
                        dualShowNewZs = false;
                        dualNewZsData = null;
                        renderBottom();
                    }
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
                    // 收盘价等于开盘价，画十字线（竖线+横线，宽度一致）
                    ctx.fillStyle = "#FFFFFF";
                    ctx.fillRect(x - 0.5, highY, 1, lowY - highY);          // 竖线：上影线到下影线
                    ctx.fillRect(x - barWidth / 2, closeY - 0.5, barWidth, 1); // 横线：在收盘价位置，与竖线同宽
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
                // 显示笔索引编号
                if (showBiIdx && bi.idx != null) {
                    ctx.font = "10px monospace"; ctx.textAlign = "center";
                    ctx.fillStyle = "#fddd60";
                    const midX = (x1 + x2) / 2;
                    const midY = (y1 + y2) / 2;
                    const labelY = bi.direction === "up" ? midY - 6 : midY + 12;
                    ctx.fillText(String(bi.idx), midX, labelY);
                }
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
                ctx.font = "11px monospace"; ctx.textAlign = "left";
                const lineY = textArea.y + 11;
                ctx.fillStyle = COLORS.textLight;
                ctx.fillText("MACD(12,26,9)", textArea.x + 4, lineY);
                let xPos = textArea.x + 4 + ctx.measureText("MACD(12,26,9) ").width;
                ctx.fillStyle = COLORS.dif;
                ctx.fillText("DIF:" + targetK.dif.toFixed(2), xPos, lineY);
                xPos += ctx.measureText("DIF:" + targetK.dif.toFixed(2) + " ").width;
                ctx.fillStyle = COLORS.dea;
                ctx.fillText("DEA:" + targetK.dea.toFixed(2), xPos, lineY);
                xPos += ctx.measureText("DEA:" + targetK.dea.toFixed(2) + " ").width;
                ctx.fillStyle = targetK.macd >= 0 ? "#FF4444" : "#00DD00";
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
            const isReplay = chartData.meta && chartData.meta.is_replay;

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
                ctx.fillText(_fmtPrice(zs.zg), x1 - 2, y1 - 2);
                ctx.fillText(_fmtPrice(zs.zd), x1 - 2, y2 + 10);
                // 中枢高度，标在上下沿中间位置
                const zsHeight = zs.zg - zs.zd;
                ctx.fillText(_fmtPrice(zsHeight), x1 - 2, (y1 + y2) / 2 + 3);
            });

            // 进入段和离开段红绿五角星 - 已注释掉
            // if (!chartData.zs_stars || !chartData.zs_stars.length) return;
            // chartData.zs_stars.forEach(star => {
            //     let idx = dateToGlobalIdx(star.date, map);
            //     if (idx === undefined) return;
            //     if (idx < globalStart || idx >= globalEnd) return;
            //     const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
            //     const y = priceToY(star.price, area, priceRange);
            //     const isTop = star.mark === "G";
            //     const starY = isTop ? y - 16 : y + 22;
            //     drawStar(ctx, x, starY, 5, 6, 3, star.color);
            // });
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
                if (bspFilter && !bspFilter[bsp.type]) return;  // 按用户设置过滤类型
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
            if (!dualNewZsData || !dualNewZsData.zs || !dualNewZsData.zs.length) {
                console.log("[drawDualNewZs] 跳过: dualNewZsData=", dualNewZsData);
                return;
            }
            const map = buildGlobalDateMap();
            console.log("[drawDualNewZs] ZS数量=" + dualNewZsData.zs.length + ", start_bi=" + dualNewZsData.start_bi + ", end_bi=" + dualNewZsData.end_bi);
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
                    eIdx = dualSubData.klines.length - 1;
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
                ctx.fillText(_fmtPrice(zs.zg), x1 - 2, y1 - 2);
                ctx.fillText(_fmtPrice(zs.zd), x1 - 2, y2 + 10);
                // 中枢高度，标在上下沿中间位置
                const zsHeight = zs.zg - zs.zd;
                ctx.fillText(_fmtPrice(zsHeight), x1 - 2, (y1 + y2) / 2 + 3);
            });

            // 进入段和离开段红绿五角星 - 已注释掉
            // if (!dualNewZsData.zs_stars || !dualNewZsData.zs_stars.length) return;
            // dualNewZsData.zs_stars.forEach(star => {
            //     let idx = dateToGlobalIdx(star.date, map);
            //     if (idx === undefined) return;
            //     if (idx < globalStart || idx >= globalEnd) return;
            //     const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
            //     const y = priceToY(star.price, area, priceRange);
            //     const isTop = star.mark === "G";
            //     const starY = isTop ? y - 16 : y + 22;
            //     drawStar(ctx, x, starY, 5, 6, 3, star.color);
            // });
        }

        function drawMaLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || klines.length < 2) return;
            const start = Math.max(0, Math.floor(viewOffset));
            const allKlines = chartData.klines;
            const n = allKlines.length;
            // 收集已选中的均线周期（按周期升序，短周期画在上层）
            const periods = [];
            for (var p in maPeriods) {
                if (maPeriods[p]) periods.push(parseInt(p, 10));
            }
            periods.sort(function(a, b) { return a - b; });
            if (periods.length === 0) return;
            ctx.lineWidth = 1;
            for (let pi = 0; pi < periods.length; pi++) {
                const period = periods[pi];
                if (period <= 0 || period > n) continue;
                // 滑动窗口计算该周期均线
                const ma = new Array(n).fill(null);
                let sum = 0;
                for (let i = 0; i < n; i++) {
                    sum += allKlines[i].close;
                    if (i >= period) sum -= allKlines[i - period].close;
                    if (i >= period - 1) ma[i] = sum / period;
                }
                ctx.strokeStyle = MA_COLORS[period] || "#FFFFFF";
                ctx.beginPath();
                let started = false;
                for (let i = 0; i < klines.length; i++) {
                    const globalIdx = start + i;
                    if (globalIdx < n && ma[globalIdx] !== null && !isNaN(ma[globalIdx])) {
                        const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                        const y = priceToY(ma[globalIdx], area, priceRange);
                        if (!started) { ctx.moveTo(x, y); started = true; }
                        else ctx.lineTo(x, y);
                    }
                }
                ctx.stroke();
            }
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
            ctx.fillText(_fmtPrice(hline.price), x2 + 4, y + 4);
        }

        /**
         * 同花顺风格：在视口内标注最高价和最低价的极值K线
         * - 数值和箭头纯白色 #FFFFFF
         * - 高点：下边沿贴合极值线，数值 ↘；低点：上边沿贴合极值线，数值 ↗
         * - 左侧空间不足时：高点 ↙ 数值，低点 ↖ 数值（数值显示在右侧）
         */
        function drawViewportHighLow(klines, area, priceRange, barStep, subPixelOffset) {
            if (!klines.length) return;

            // 找到视口内最高价和最低价的K线
            let maxHigh = -Infinity, minLow = Infinity;
            let maxHighIdx = -1, minLowIdx = -1;

            for (let i = 0; i < klines.length; i++) {
                const k = klines[i];
                if (k.high > maxHigh) { maxHigh = k.high; maxHighIdx = i; }
                if (k.low < minLow) { minLow = k.low; minLowIdx = i; }
            }

            if (maxHighIdx === -1 || minLowIdx === -1) return;

            const gap = 4; // 数值与箭头间距

            ctx.font = "11px monospace";
            ctx.fillStyle = "#FFFFFF";

            const arrowR = "\u2192"; // →  用于计算箭头宽度（所有箭头等宽）
            const arrowW = ctx.measureText(arrowR).width;

            /**
             * 绘制单个极值标注
             * @param {number} price - 极值价格
             * @param {number} klineIdx - K线在视口内的索引
             * @param {boolean} isHigh - 是否为高点（true=下边沿贴合, false=上边沿贴合）
             */
            function drawOne(price, klineIdx, isHigh) {
                const kx = area.x + barStep * klineIdx + barStep / 2 - subPixelOffset;
                const ky = priceToY(price, area, priceRange);
                const text = _fmtPrice(price);
                const textW = ctx.measureText(text).width;

                const needLeft = textW + gap + arrowW;
                const canLeft = (kx - needLeft) >= area.x;

                const textHeight = 11 * 1.2;  // fontSize * 行高系数
                const a = isHigh ? (canLeft ? "\u2198" : "\u2199") : (canLeft ? "\u2197" : "\u2196");

                ctx.textAlign = "left";

                if (canLeft) {
                    // 数值 ↘(高) / ↗(低)
                    const arrowX = kx - arrowW;
                    const textX = arrowX - gap - textW;
                    // 箭头：保持原位置（高点bottom贴合ky，低点top贴合ky）
                    ctx.textBaseline = isHigh ? "bottom" : "top";
                    ctx.fillText(a, textX + textW + gap, ky);
                    // 数值：中间对齐箭头尾端，高点往上移，低点往下移
                    ctx.textBaseline = "middle";
                    ctx.fillText(text, textX, isHigh ? ky - textHeight / 2 : ky + textHeight / 2);
                } else {
                    // ↙(高) / ↖(低) 数值
                    const arrowX = kx;
                    const textX = arrowX + arrowW + gap;
                    if (textX + textW > area.x + area.w) return;
                    // 箭头：保持原位置
                    ctx.textBaseline = isHigh ? "bottom" : "top";
                    ctx.fillText(a, arrowX, ky);
                    // 数值：中间对齐箭头尾端
                    ctx.textBaseline = "middle";
                    ctx.fillText(text, textX, isHigh ? ky - textHeight / 2 : ky + textHeight / 2);
                }
            }

            drawOne(maxHigh, maxHighIdx, true);   // 高点：下边沿贴合
            drawOne(minLow, minLowIdx, false);    // 低点：上边沿贴合
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
                if (mouseY >= area.y && mouseY <= crosshairEndY) {
                    ctx.beginPath(); ctx.moveTo(area.x, mouseY); ctx.lineTo(area.x + area.w, mouseY); ctx.stroke();
                }
                ctx.setLineDash([]);
                if (mouseY >= area.y && mouseY <= area.y + area.h) {
                    const price = yToPrice(mouseY, area, priceRange);
                    _overlayData = _overlayData || {};
                    _overlayData.rightPrice = _fmtPrice(price);
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
                    const dateParts = k.date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                } else {
                    const dateParts = k.date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                }
                const d = new Date(k.date.replace(/\//g, "-").replace(" ", "T"));
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
                // 反转模式下显示原始价格：open/close取绝对值，high/low互换后取绝对值
                const dispOpen = _isMirrorMode ? Math.abs(k.open) : k.open;
                const dispHigh = _isMirrorMode ? Math.abs(k.low) : k.high;   // k.low=-orig_high → |k.low|=orig_high
                const dispLow = _isMirrorMode ? Math.abs(k.high) : k.low;    // k.high=-orig_low → |k.high|=orig_low
                const dispClose = _isMirrorMode ? Math.abs(k.close) : k.close;
                document.getElementById("crosshair-info").innerHTML =
                    `<span class="label">开:</span> <span class="${cls}">${dispOpen.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">高:</span> <span class="${cls}">${dispHigh.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">低:</span> <span class="${cls}">${dispLow.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">收:</span> <span class="${cls}">${dispClose.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">涨跌:</span> <span class="${cls}">${sign}${changeVal.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">涨幅:</span> <span class="${cls}">${sign}${changePct}%</span> &nbsp; ` +
                    `<span class="label">复权:</span> <span class="label">${chartData.meta.forward_adjust ? "前复权" : "不复权"}</span>` +
                    (chartData.meta.pe_ttm != null ? ` &nbsp; <span class="label">PE-TTM:</span> <span class="label">${chartData.meta.pe_ttm > 0 ? chartData.meta.pe_ttm.toFixed(2) : "亏损"}</span>` : "") +
                    (chartData.meta.index_belong ? ` &nbsp; <span class="label">归属:</span> <span class="label">${chartData.meta.index_belong}</span>` : "");
            }

            const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
            const weekDayStr = "周" + weekDays[new Date(k.date.replace(/\//g, "-").replace(" ", "T")).getDay()];
            // 剪贴板文本同样显示原始价格
            const clipOpen = _isMirrorMode ? Math.abs(k.open) : k.open;
            const clipHigh = _isMirrorMode ? Math.abs(k.low) : k.high;
            const clipLow = _isMirrorMode ? Math.abs(k.high) : k.low;
            const clipClose = _isMirrorMode ? Math.abs(k.close) : k.close;
            const clipText = `${k.date} ${weekDayStr} 开:${clipOpen.toFixed(2)} 高:${clipHigh.toFixed(2)} 低:${clipLow.toFixed(2)} 收:${clipClose.toFixed(2)}`;
            if (window._isRenderingBottom) {
                // 底部窗口：记录底部窗口的全局索引和剪贴板文本
                _subCurrentGlobalIdx = globalIdx;
                _subClipText = clipText;
            } else {
                // 上面窗口
                _currentGlobalIdx = globalIdx;
                _currentClipText = clipText;
            }
        }

        function drawPriceAxis(area, priceRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
            for (let i = 0; i <= 5; i++) {
                const price = priceRange.min + (priceRange.max - priceRange.min) * (1 - i / 5);
                const y = area.y + (area.h / 5) * i;
                ctx.fillText(_fmtPrice(price), area.x + area.w + 6, y + 4);
            }
        }

        function drawMacdAxis(macdArea, macdRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
            ctx.fillText(macdRange.max.toFixed(2), macdArea.x + macdArea.w + 6, macdArea.y + 12);
            const zeroY = macdArea.y + macdArea.h * (macdRange.max / (macdRange.max - macdRange.min));
            ctx.fillText("0", macdArea.x + macdArea.w + 6, zeroY + 4);
            ctx.fillText(macdRange.min.toFixed(2), macdArea.x + macdArea.w + 6, macdArea.y + macdArea.h - 4);
        }

        function drawDateAxis(klines, barStep, subPixelOffset) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace";
            const area = getChartArea(), volArea = getVolArea();
            const dateY = volArea.y + volArea.h + 28;

            // 测量样本日期文本宽度，用于计算最小像素间距
            let sampleDate;
            if (currentFreq === '15s') {
                sampleDate = getKlineEndTime(klines[0].date, true);
            } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m') {
                sampleDate = getKlineEndTime(klines[0].date);
            } else {
                const dateParts = klines[0].date.split(/[-\/]/);
                sampleDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
            }
            const textWidth = ctx.measureText(sampleDate).width;
            const gap = 10;  // 标签文本边缘之间的最小像素间距
            const n = klines.length;
            const lastIdx = n - 1;

            // 始终包含首尾标签
            const indices = [0];

            if (n > 1) {
                // 首标签左对齐，尾标签右对齐，中间标签居中
                // 首标签右边缘 = area.x + textWidth
                // 第一个中间标签左边缘 = centerX - textWidth/2，要求 centerX >= area.x + textWidth + gap + textWidth/2
                // 即 centerX >= area.x + 1.5*textWidth + gap
                // centerX = area.x + barStep * idx + barStep/2 - subPixelOffset
                // => idx >= (1.5*textWidth + gap - barStep/2 + subPixelOffset) / barStep
                const firstMiddleIdx = Math.max(1, Math.round((1.5 * textWidth + gap - barStep / 2 + subPixelOffset) / barStep));

                // 尾标签左边缘 = area.x + area.w - textWidth
                // 最后一个中间标签右边缘 = centerX + textWidth/2，要求 centerX <= area.x + area.w - textWidth - gap - textWidth/2
                // => idx <= (area.w - 1.5*textWidth - gap - barStep/2 + subPixelOffset) / barStep
                const lastMiddleIdx = Math.min(lastIdx - 1, Math.round((area.w - 1.5 * textWidth - gap - barStep / 2 + subPixelOffset) / barStep));

                if (firstMiddleIdx <= lastMiddleIdx) {
                    // 中间标签之间的最小K线间隔（保证居中标签不重叠）
                    const minIdxGap = Math.ceil((textWidth + gap) / barStep);
                    const available = lastMiddleIdx - firstMiddleIdx;
                    const k = Math.floor(available / minIdxGap) + 1;  // 中间标签个数
                    if (k >= 1 && k === 1) {
                        // 只有一个中间标签：放在安全区间中点
                        indices.push(Math.round((firstMiddleIdx + lastMiddleIdx) / 2));
                    } else if (k >= 2) {
                        // 多个中间标签：均匀分布
                        const step = available / (k - 1);
                        for (let i = 0; i < k; i++) {
                            indices.push(Math.round(firstMiddleIdx + i * step));
                        }
                    }
                }

                indices.push(lastIdx);
            }

            // 绘制标签
            indices.forEach(i => {
                let shortDate;
                if (currentFreq === '15s') {
                    shortDate = getKlineEndTime(klines[i].date, true);
                } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m') {
                    shortDate = getKlineEndTime(klines[i].date);
                } else if (currentFreq === 'w') {
                    const dateParts = klines[i].date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                } else {
                    // 日线
                    const dateParts = klines[i].date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                }
                if (i === 0) {
                    ctx.textAlign = "left";
                    ctx.fillText(shortDate, area.x, dateY);
                } else if (i === lastIdx) {
                    ctx.textAlign = "right";
                    ctx.fillText(shortDate, area.x + area.w, dateY);
                } else {
                    ctx.textAlign = "center";
                    const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                    ctx.fillText(shortDate, x, dateY);
                }
            });
        }

        function onWheel(e) {
            e.preventDefault();
            if (isDualWindow) { activeDualWindow = 'main'; updateActiveWindowClass(); updateSlider(); }
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
            if (isDualWindow) { activeDualWindow = 'main'; updateActiveWindowClass(); updateSlider(); }
        }
        function onMouseMove(e) {
            const rect = canvas.getBoundingClientRect();
            mouseX = e.clientX - rect.left; mouseY = e.clientY - rect.top;
            if (isDragging) { viewOffset = dragStartOffset - (e.clientX - dragStartX) / (getChartArea().w / viewCount); viewOffset = Math.max(0, Math.min(chartData.klines.length - viewCount, viewOffset)); }
            // 双窗口红框：直接用 MouseEvent.ctrlKey 检测，比 keydown/keyup 跟踪更可靠
            if (isDualWindow) {
                const prevCtrl = _ctrlPressed;
                _ctrlPressed = e.ctrlKey;
                // Ctrl 状态变化时强制重绘（松开Ctrl立即清除红框/新中枢）
                if (_ctrlPressed !== prevCtrl) {
                    if (!_ctrlPressed) {
                        dualRedRange = null;
                        dualShowNewZs = false;
                        dualNewZsData = null;
                    }
                }
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
                    const wd = "周" + weekDays[new Date(k.date.replace(/\//g, "-").replace(" ", "T")).getDay()];
                    lines.push(`${k.date} ${wd} 开:${k.open.toFixed(2)} 高:${k.high.toFixed(2)} 低:${k.low.toFixed(2)} 收:${k.close.toFixed(2)}`);
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

        // Esc键取消区间选择 / 关闭设置抽屉
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                var drawer = document.getElementById("bsp-filter-dialog");
                if (drawer.classList.contains("show")) {
                    closeBspSettings();
                    return;
                }
                if (_rangeSelect.mode === 'SELECTED_A') {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("区间选择已取消");
                    render();
                }
            }
        });
        document.addEventListener('keyup', function(e) {
            // 兜底：松开Ctrl时清除红框（onMouseMove用e.ctrlKey是主要检测路径）
            if (e.key === 'Control' && isDualWindow) {
                _ctrlPressed = false;
                dualRedRange = null;
                dualShowNewZs = false;
                dualNewZsData = null;
                renderTop();
            }
        });

        // 更新双窗口激活状态视觉提示
        function updateActiveWindowClass() {
            const mainDiv = document.getElementById("chart-main");
            const subDiv = document.getElementById("chart-sub");
            if (mainDiv) mainDiv.classList.toggle("dual-active", activeDualWindow === 'main');
            if (subDiv) subDiv.classList.toggle("dual-active", activeDualWindow === 'sub');
        }

        // 双窗口切换
        window.toggleDualWindow = function() {
            if (!chartData) return;
            const btn = document.getElementById("btn-dual");
            if (isDualWindow) {
                // 关闭双窗口
                isDualWindow = false;
                activeDualWindow = 'main';
                dualSubData = null;
                dualSubFreq = '';
                dualHighlightRange = null;
                dualRedRange = null;
                dualNewZsData = null;
                dualShowNewZs = false;
                dualNewZsLeftDate = "";
                dualNewZsRightDate = "";
                btn.classList.remove("active");
                const isFuturesClose = chartData && chartData.meta && chartData.meta.market === 'futures';
                updateFreqButtonStates(isFuturesClose);
                // 恢复单canvas布局
                const container = document.getElementById("chart-container");
                const mainDiv = document.getElementById("chart-main");
                const subDiv = document.getElementById("chart-sub");
                if (mainDiv) mainDiv.remove();
                if (subDiv) subDiv.remove();
                canvas = mainCanvas; ctx = mainCtx;
                container.appendChild(canvas);
                resizeCanvas();
                // 期货：关闭双窗口后重连单SSE
                if (isFuturesClose) {
                    disconnectRealtime();
                    connectRealtimeInit(chartData.meta.symbol, currentFreq);
                } else {
                    render();
                }
            } else {
                // 开启双窗口
                const subFreq = getDualSubFreq(currentFreq);
                if (!subFreq) {
                    // 5分周期无对应，提示
                    return;
                }
                isDualWindow = true;
                dualSubFreq = subFreq;
                btn.classList.add("active");
                // 创建双窗口布局
                const container = document.getElementById("chart-container");
                // 保存原始canvas引用
                const origCanvas = mainCanvas;
                // 清空容器
                container.innerHTML = '';
                // 创建上面窗口
                const mainDiv = document.createElement("div");
                mainDiv.id = "chart-main";
                mainDiv.appendChild(origCanvas);
                container.appendChild(mainDiv);
                // 创建下面窗口
                const subDiv = document.createElement("div");
                subDiv.id = "chart-sub";
                subCanvas = document.createElement("canvas");
                subCtx = subCanvas.getContext("2d");
                subDiv.appendChild(subCanvas);
                container.appendChild(subDiv);
                // 添加下面窗口事件
                subCanvas.addEventListener("wheel", onSubWheel, { passive: false });
                subCanvas.addEventListener("mousedown", onSubMouseDown);
                subCanvas.addEventListener("mousemove", onSubMouseMove);
                subCanvas.addEventListener("mouseup", onSubMouseUp);
                subCanvas.addEventListener("mouseleave", onSubMouseLeave);
                subCanvas.addEventListener("dblclick", function(e) {
                    if (!dualSubData) return;
                    const rect = subCanvas.getBoundingClientRect();
                    const clickX = e.clientX - rect.left;
                    const clickY = e.clientY - rect.top;
                    // 临时切换全局变量以使用 getChartArea 等函数
                    const _savedCanvas = canvas, _savedCtx = ctx;
                    const _savedViewOffset = viewOffset, _savedViewCount = viewCount;
                    const _savedChartData = chartData, _savedFreq = currentFreq;
                    canvas = subCanvas; ctx = subCtx;
                    viewOffset = dualSubViewOffset; viewCount = dualSubViewCount;
                    chartData = dualSubData; currentFreq = dualSubFreq;
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
                    if (dualOffscreenState && dualHighlightRange && dualSubData) {
                        const hr = dualHighlightRange;
                        if (hr.startIdx >= 0 && hr.endIdx >= 0) {
                            const centerIdx = (hr.startIdx + hr.endIdx) / 2;
                            const totalKlines = dualSubData.klines.length;
                            let newOffset = Math.round(centerIdx - dualSubViewCount / 2);
                            if (newOffset < 0) newOffset = 0;
                            const maxOffset = Math.max(0, totalKlines - dualSubViewCount);
                            if (newOffset > maxOffset) newOffset = maxOffset;
                            dualSubViewOffset = newOffset;
                            dualHighlightRange = calcGrayRange(mouseX);
                            dualRedRange = dualHighlightRange ? dualHighlightRange.redRange : null;
                            dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                        } else {
                            showDualToast("请加载更多K线...");
                        }
                        renderBottom();
                        return;
                    }
                    // 默认：恢复下面窗口全视图
                    dualSubViewCount = 377;
                    dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                    if (dualSubData.klines.length < dualSubViewCount) {
                        dualSubViewOffset = 0;
                    }
                    renderBottom();
                });
                // 恢复crosshair-info
                const crosshairInfo = document.createElement("div");
                crosshairInfo.className = "crosshair-info";
                crosshairInfo.id = "crosshair-info";
                container.appendChild(crosshairInfo);
                resizeCanvas();
                const code = chartData.meta.symbol;
                const isFutures = chartData.meta.market === 'futures';
                document.getElementById("loading").classList.remove("hidden");
                document.querySelector(".loading-text").textContent = "正在加载双窗口数据...";

                if (isFutures) {
                    // 期货双窗口：使用 connectRealtimeDual，自带完整的 init/update/error 处理与自动跟随逻辑
                    connectRealtimeDual(code, currentFreq, subFreq);
                } else {
                    // 股票双窗口：HTTP 请求
                    fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + currentFreq + "&dual=1")
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            if (data.sub) {
                                chartData = data;
                                dualSubData = data.sub;
                                dualSubViewCount = 377;
                                dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                                if (dualSubData.klines.length < dualSubViewCount) {
                                    dualSubViewOffset = 0;
                                }
                                document.getElementById("loading").classList.add("hidden");
                                document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                                updateFreqButtonStates(false);
                                render();
                            } else {
                                throw new Error("服务端未返回子级别数据");
                            }
                        })
                        .catch(err => {
                            alert("加载下面窗口数据失败: " + err.message);
                            isDualWindow = false;
                            activeDualWindow = 'main';
                            dualSubData = null;
                            dualSubFreq = '';
                            btn.classList.remove("active");
                            updateFreqButtonStates(false);
                            const container2 = document.getElementById("chart-container");
                            container2.innerHTML = '';
                            const ci2 = document.createElement("div");
                            ci2.className = "crosshair-info";
                            ci2.id = "crosshair-info";
                            container2.appendChild(ci2);
                            container2.appendChild(origCanvas);
                            canvas = mainCanvas; ctx = mainCtx;
                            resizeCanvas();
                            render();
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        });
                }
            }
        };

        // 下面窗口的事件处理
        function onSubWheel(e) {
            e.preventDefault();
            if (!dualSubData) return;
            activeDualWindow = 'sub';
            updateActiveWindowClass();
            updateSlider();
            const savedCanvas = canvas; const savedCtx = ctx;
            const savedViewOffset = viewOffset; const savedViewCount = viewCount;
            canvas = subCanvas; ctx = subCtx;
            viewOffset = dualSubViewOffset; viewCount = dualSubViewCount;
            const rect = subCanvas.getBoundingClientRect();
            const bMouseX = e.clientX - rect.left;
            const area = getChartArea();
            const klines = dualSubData.klines;
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
                dualSubViewCount = newViewCount;
                dualSubViewOffset = Math.max(0, Math.min(maxOffset, rightGlobalIdx - newViewCount + 1));
            } else {
                const anchorGlobalIdx = viewOffset + mouseKIdx;
                let newViewOffset = anchorGlobalIdx - ratio * newViewCount;
                newViewOffset = Math.max(0, newViewOffset);
                if (newViewOffset > maxOffset) newViewOffset = maxOffset;
                dualSubViewCount = newViewCount;
                dualSubViewOffset = newViewOffset;
            }
            canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
            renderBottom();
        }

        function onSubMouseDown(e) {
            dualSubIsDragging = true;
            dualSubDragStartX = e.clientX;
            dualSubDragStartOffset = dualSubViewOffset;
            dualSubMouseDownX = e.clientX;
            dualSubMouseDownY = e.clientY;
            subCanvas.style.cursor = "grabbing";
            activeDualWindow = 'sub';
            updateActiveWindowClass();
            updateSlider();
        }

        function onSubMouseMove(e) {
            const rect = subCanvas.getBoundingClientRect();
            dualSubMouseX = e.clientX - rect.left;
            dualSubMouseY = e.clientY - rect.top;
            if (dualSubIsDragging && dualSubData) {
                const savedCanvas = canvas; const savedCtx = ctx;
                const savedViewOffset = viewOffset; const savedViewCount = viewCount;
                canvas = subCanvas; ctx = subCtx;
                viewOffset = dualSubViewOffset; viewCount = dualSubViewCount;
                dualSubViewOffset = dualSubDragStartOffset - (e.clientX - dualSubDragStartX) / (getChartArea().w / viewCount);
                dualSubViewOffset = Math.max(0, Math.min(dualSubData.klines.length - dualSubViewCount, dualSubViewOffset));
                canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
            }
            renderBottom();
        }

        function onSubMouseUp(e) {
            dualSubIsDragging = false;
            subCanvas.style.cursor = "crosshair";
            // 只处理左键点击（非拖拽）
            if (e.button !== 0 || Math.abs(e.clientX - dualSubMouseDownX) >= 5 || Math.abs(e.clientY - dualSubMouseDownY) >= 5) return;
            if (_subCurrentGlobalIdx < 0 || !dualSubData) return;

            // === Ctrl+点击：区间选择模式切换（底部窗口）===
            if (e.ctrlKey) {
                if (_rangeSelect.mode === 'IDLE') {
                    _rangeSelect = {
                        mode: 'SELECTED_A',
                        startIdx: _subCurrentGlobalIdx,
                        startFreq: dualSubFreq,
                        startSymbol: dualSubData.meta.symbol
                    };
                    const startDate = dualSubData.klines[_subCurrentGlobalIdx].date.split(' ')[0];
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
                if (_rangeSelect.startFreq !== dualSubFreq || _rangeSelect.startSymbol !== dualSubData.meta.symbol) {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("股票或周期已变更，区间选择已取消");
                    return;
                }
                const a = Math.min(_rangeSelect.startIdx, _subCurrentGlobalIdx);
                const b = Math.max(_rangeSelect.startIdx, _subCurrentGlobalIdx);
                const klines = dualSubData.klines;
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                const lines = [];
                for (let i = a; i <= b; i++) {
                    const k = klines[i];
                    const prevK = i > 0 ? klines[i - 1] : null;
                    const prevClose = prevK ? prevK.close : k.open;
                    const changeVal = k.close - prevClose;
                    const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
                    const sign = changeVal >= 0 ? "+" : "";
                    const wd = "周" + weekDays[new Date(k.date.replace(/\//g, "-").replace(" ", "T")).getDay()];
                    lines.push(`${k.date} ${wd} 开:${k.open.toFixed(2)} 高:${k.high.toFixed(2)} 低:${k.low.toFixed(2)} 收:${k.close.toFixed(2)}`);
                }
                navigator.clipboard.writeText(lines.join("\n")).catch(() => {});
                showDualToast("已复制 " + (b - a + 1) + " 根K线数据到剪贴板");
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                renderBottom();
                return;
            }

            // === 普通模式：复制当前K线信息 ===
            if (_subClipText) {
                navigator.clipboard.writeText(_subClipText).catch(() => {});
            }
        }

        function onSubMouseLeave() {
            dualSubIsDragging = false;
            dualSubMouseX = -1; dualSubMouseY = -1;
            subCanvas.style.cursor = "crosshair";
            renderBottom();
        }

        window.toggleOverlay = function(type) {
            if (type === "bi") { showBi = !showBi; document.getElementById("btn-bi").classList.toggle("active", showBi); }
            else if (type === "fx") { showFx = !showFx; document.getElementById("btn-fx").classList.toggle("active", showFx); }
            else if (type === "zs") { showZs = !showZs; document.getElementById("btn-zs").classList.toggle("active", showZs); }
            else if (type === "seg") { showSeg = !showSeg; document.getElementById("btn-seg").classList.toggle("active", showSeg); }
            else if (type === "bsp") { showBsp = !showBsp; document.getElementById("btn-bsp").classList.toggle("active", showBsp); }
            saveOverlaySettings();
            render();
        };

        window.toggleStats = function() {
            var panel = document.getElementById("stats-panel");
            var btn = document.getElementById("btn-stats");
            if (panel.classList.contains("show")) {
                panel.classList.remove("show");
                document.removeEventListener("click", _onClickOutsideStats);
            } else {
                panel.classList.add("show");
                // 延迟绑定，避免当前点击冒泡立即触发关闭
                setTimeout(function() {
                    document.addEventListener("click", _onClickOutsideStats);
                }, 0);
            }
        };
        function _onClickOutsideStats(e) {
            var panel = document.getElementById("stats-panel");
            var btn = document.getElementById("btn-stats");
            if (!panel.contains(e.target) && !btn.contains(e.target)) {
                panel.classList.remove("show");
                document.removeEventListener("click", _onClickOutsideStats);
            }
        }

        // ── BSP买卖点类型过滤 + 均线周期设置 ──
        window.openBspSettings = function() {
            // 打开前同步当前过滤状态到复选框
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = bspFilter[cbs[i].value];
            }
            // 同步均线周期复选框
            var macbs = document.querySelectorAll('#bsp-filter-dialog input[name="ma-period"]');
            for (var i = 0; i < macbs.length; i++) {
                macbs[i].checked = !!maPeriods[macbs[i].value];
            }
            // 同步笔索引复选框
            var biIdxCb = document.querySelector('#bsp-filter-dialog input[name="show-bi-idx"]');
            if (biIdxCb) biIdxCb.checked = showBiIdx;
            document.getElementById("bsp-filter-dialog").classList.add("show");
            document.getElementById("bsp-filter-overlay").classList.add("show");
        };
        window.closeBspSettings = function() {
            document.getElementById("bsp-filter-dialog").classList.remove("show");
            document.getElementById("bsp-filter-overlay").classList.remove("show");
        };
        // 即时生效：单个买卖点复选框变化
        window.onBspFilterChange = function(cb) {
            bspFilter[cb.value] = cb.checked;
            saveOverlaySettings();
            render();
        };
        // 即时生效：单个均线周期复选框变化
        window.onMaPeriodChange = function(cb) {
            if (cb.checked) maPeriods[cb.value] = true;
            else delete maPeriods[cb.value];
            saveOverlaySettings();
            render();
        };
        window.onShowBiIdxChange = function(cb) {
            showBiIdx = cb.checked;
            saveOverlaySettings();
            render();
        };
        window.bspFilterSelectAll = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = true;
                bspFilter[cbs[i].value] = true;
            }
            saveOverlaySettings();
            render();
        };
        window.bspFilterSelectNone = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = false;
                bspFilter[cbs[i].value] = false;
            }
            saveOverlaySettings();
            render();
        };
        window.maPeriodsSelectAll = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="ma-period"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = true;
                maPeriods[cbs[i].value] = true;
            }
            saveOverlaySettings();
            render();
        };
        window.maPeriodsSelectNone = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="ma-period"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = false;
            }
            maPeriods = {};
            saveOverlaySettings();
            render();
        };

        // 辅助：根据chartData中的saved_selection_date恢复「取消选点」菜单项状态
        function updateRestartBtn() {
            var hasPoint = chartData && chartData.meta && chartData.meta.saved_selection_date;
            var isReplay = chartData && chartData.meta && chartData.meta.is_replay;
            _restartEnabled = hasPoint && !isDualWindow && !isReplay;
        }

        function updateDualBtn() {
            const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            if (isFutures) {
                // 期货：30m/5m/1m 可双窗口，15s 不可
                document.getElementById("btn-dual").disabled = (currentFreq === '15s');
            } else {
                // 股票：w/d/30m 可双窗口，5m 不可
                document.getElementById("btn-dual").disabled = (currentFreq === '5m');
            }
        };

        // ============================================================
        // 重启：清除选点，按冷启动重新加载
        // ============================================================
        window.cancelSelectedPoint = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!chartData || !chartData.meta) return;
            // 双窗口模式和复盘模式不允许重置
            if (isDualWindow) { showDualToast("双窗口模式，不支持重置"); return; }
            if (chartData.meta.is_replay) { showDualToast("复盘模式，不支持重置"); return; }
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            const isFutures = chartData.meta.market === 'futures';
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在重置...";

            // 期货：清除选点 + 冷启动重连SSE（无start_time）
            if (isFutures) {
                fetch("/api/futures_clear_saved_point?symbol=" + encodeURIComponent(code) + "&freq=" + freq)
                    .then(resp => resp.json())
                    .then(() => {
                        // 不隐藏loading，交给connectRealtimeInit的init事件来隐藏
                        // 如果提前隐藏loading，会导致SSE重连失败时没有任何加载反馈
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        connectRealtimeInit(code, freq);  // 冷启动，不带start_time
                    })
                    .catch(err => {
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
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
                    return fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq + (isDualWindow && getDualSubFreq(freq) ? "&dual=1" : ""));
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
                    updateDateInputType();
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
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    resizeCanvas();
                    render();
                    generateStats();
                    updateRestartBtn();
                    updateDualBtn();
                    // 双窗口模式：从 data.sub 恢复子级别数据
                    if (isDualWindow && data.sub) {
                        dualSubData = data.sub;
                        dualSubViewCount = 377;
                        dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                        if (dualSubData.klines.length < dualSubViewCount) {
                            dualSubViewOffset = 0;
                        }
                    }
                })
                .catch(err => {
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    alert("重置失败: " + err.message);
                });
        };

        // ============================================================
        // 股票买卖点扫描（逐只扫描，实时进度，可中断）
        // ============================================================
        let _scanRunning = false;
        let _scanAborted = false;
        let _scanMode = "ann"; // "ann" = 标注扫描, "ma" = 均线分类扫描, "fx_d" = 底分型扫描, "bsp" = 买卖点扫描
        let _scanRecentDays = 1; // 最近N根K线，默认1
        let _scanSources = ["zxg"]; // 多选：["zxg", "sz50", "hs300", "zz500", "zz1000"]
        let _scanFreq = "d"; // 扫描周期，默认日K

        // 扫描模式切换时，控制"最近N根"输入框的灰化状态
        // 标注扫描：只要有标注就命中，与日期无关，输入框置灰；扫描来源也置灰
        // 底分型扫描：找最后一个分型是底分型的个股，与日期无关，输入框置灰；扫描来源可用
        // 买卖点扫描：需要按最近N根K线过滤，输入框可用
        function updateScanRecentDisabled() {
            var row = document.getElementById("scan-recent-row");
            var input = document.getElementById("scan-recent-days");
            var freqRow = document.getElementById("scan-freq-row");
            var selected = document.querySelector('input[name="scan-mode"]:checked');
            var isAnn = selected && selected.value === "ann";
            var isMa = selected && selected.value === "ma";
            var isFxD = selected && selected.value === "fx_d";
            if (row && input) {
                if (isAnn || isMa || isFxD) {
                    row.style.opacity = "0.35";
                    row.style.pointerEvents = "none";
                    input.disabled = true;
                } else {
                    row.style.opacity = "1";
                    row.style.pointerEvents = "";
                    input.disabled = false;
                }
            }
            if (freqRow) {
                if (isAnn) {
                    // 标注扫描：周期也置灰
                    freqRow.style.opacity = "0.35";
                    freqRow.style.pointerEvents = "none";
                } else {
                    // 底分型/买卖点扫描：周期可用
                    freqRow.style.opacity = "1";
                    freqRow.style.pointerEvents = "";
                }
            }
            // 标注模式下灰化"扫描来源"区域（标注扫描与来源无关）
            var srcSection = document.getElementById("scan-source-section");
            if (srcSection) {
                if (isAnn) {
                    srcSection.style.opacity = "0.35";
                    srcSection.style.pointerEvents = "none";
                } else {
                    srcSection.style.opacity = "1";
                    srcSection.style.pointerEvents = "";
                }
            }
        }
        window.updateScanRecentDisabled = updateScanRecentDisabled;

        // 扫描来源→中文标签（多选时用顿号连接）
        function _scanSourceLabel() {
            var map = {"zxg": "自选股", "sz50": "上证50", "hs300": "沪深300", "zz500": "中证500", "zz1000": "中证1000", "page_index": "成分股", "tdxhy2": "板块指数2", "tdxhy3": "板块指数3"};
            var labels = [];
            for (var i = 0; i < _scanSources.length; i++) {
                labels.push(map[_scanSources[i]] || _scanSources[i]);
            }
            return labels.join("、");
        }

        // 全选 / 取消 扫描来源
        window.scanSourceSelectAll = function() {
            var cbs = document.querySelectorAll('input[name="scan-source"]');
            for (var i = 0; i < cbs.length; i++) { cbs[i].checked = true; }
        };
        window.scanSourceSelectNone = function() {
            var cbs = document.querySelectorAll('input[name="scan-source"]');
            for (var i = 0; i < cbs.length; i++) { cbs[i].checked = false; }
        };

        console.log("[扫描模块] v2-多选版 已加载 OK");

        // 生成买卖点标签HTML（最多显示6个，超出显示+N）
        function buildBspTagsHtml(buyPoints, sellPoints) {
            var MAX_TAGS = 6;
            var allTags = [];
            (buyPoints || []).forEach(function(bp) {
                var tp = bp.type.replace(/\s/g, "");
                if (tp === "0" || tp === "1" || tp === "2" || tp === "3") {
                    allTags.push('<span class="scan-bsp-tag buy">' + bp.type + '</span>');
                }
            });
            (sellPoints || []).forEach(function(sp) {
                var tp = sp.type.replace(/\s/g, "");
                if (tp === "0" || tp === "1" || tp === "2" || tp === "3") {
                    allTags.push('<span class="scan-bsp-tag sell">' + sp.type + '</span>');
                }
            });
            var html = '<div class="scan-bsp-tags">';
            if (allTags.length <= MAX_TAGS) {
                html += allTags.join('');
            } else {
                html += allTags.slice(0, MAX_TAGS).join('');
                html += '<span class="scan-bsp-tag scan-bsp-more">+' + (allTags.length - MAX_TAGS) + '</span>';
            }
            html += '</div>';
            return html;
        }

        // 扫描模式对话框：取消
        window.scanModeDialogCancel = function() {
            document.getElementById("scan-mode-dialog").classList.remove("show");
        };

        // 扫描模式对话框：确认
        window.scanModeDialogConfirm = function() {
            var selected = document.querySelector('input[name="scan-mode"]:checked');
            if (!selected) return;
            _scanMode = selected.value;
            // 多选：读取所有勾选的 checkbox
            var sourceCbs = document.querySelectorAll('input[name="scan-source"]:checked');
            _scanSources = [];
            for (var i = 0; i < sourceCbs.length; i++) {
                _scanSources.push(sourceCbs[i].value);
            }
            if (_scanSources.length === 0) {
                _scanSources = ["zxg"];
                document.querySelector('input[name="scan-source"][value="zxg"]').checked = true;
            }
            var daysInput = document.getElementById("scan-recent-days");
            _scanRecentDays = parseInt(daysInput.value) || 1;
            if (_scanRecentDays < 1) _scanRecentDays = 1;
            // 读取扫描周期
            var freqRadio = document.querySelector('input[name="scan-freq"]:checked');
            if (freqRadio) {
                _scanFreq = freqRadio.value;
            }
            // 持久化到 localStorage，下次打开保持上次选择
            try {
                localStorage.setItem("scan_mode", _scanMode);
                localStorage.setItem("scan_recent_days", String(_scanRecentDays));
                localStorage.setItem("scan_sources", _scanSources.join(","));
                localStorage.setItem("scan_freq", _scanFreq);
            } catch(e) {}
            console.log("[扫描对话框] 用户选择来源: " + _scanSources.join(",") + " 模式: " + _scanMode + " 最近: " + _scanRecentDays + " 周期: " + _scanFreq);
            document.getElementById("scan-mode-dialog").classList.remove("show");
            // 如果勾选了"成分股"，通知后端当前页面指数代码
            if (_scanSources.indexOf("page_index") >= 0 && chartData && chartData.meta && chartData.meta.symbol) {
                fetch("/api/scan_page_index_code?code=" + encodeURIComponent(chartData.meta.symbol)).catch(function(){});
            }
            // 执行实际扫描
            doStartScan();
        };

        function updateScanTitle() {
            var freqLabels = {"d": "日K", "w": "周K", "30m": "30分", "5m": "5分"};
            var freqLabel = freqLabels[_scanFreq] || _scanFreq;
            if (_scanMode === "bsp") {
                document.getElementById("scan-title").innerHTML = freqLabel + ' <span style="font-size:11px;font-weight:400;color:#a8b2d1">[最近</span><b style="font-size:11px;color:#e94560"> ' + _scanRecentDays + ' </b><span style="font-size:11px;font-weight:400;color:#a8b2d1">根]</span>';
            } else if (_scanMode === "ma") {
                document.getElementById("scan-title").textContent = freqLabel + " 均线分类";
            } else if (_scanMode === "fx_d") {
                document.getElementById("scan-title").textContent = freqLabel + " 底分型";
            } else {
                // 标注扫描：显示全周期，不再显示当前周期
                document.getElementById("scan-title").textContent = "扫描全周期";
            }
        }

        window.startScanZxg = function() {
            if (_scanRunning) {
                // 正在扫描中，再次点击 = 中断扫描
                _scanAborted = true;
                var btn = document.getElementById("btn-scan");
                btn.textContent = "正在中断...";
                btn.disabled = true;
                // 通知后端立即终止
                fetch("/api/scan_abort").catch(function(){});
                return;
            }
            // 弹出模式选择对话框
            // 从 localStorage 恢复上次的选择
            try {
                var savedMode = localStorage.getItem("scan_mode");
                if (savedMode === "bsp" || savedMode === "ann" || savedMode === "ma" || savedMode === "fx_d") {
                    _scanMode = savedMode;
                    var radio = document.querySelector('input[name="scan-mode"][value="' + savedMode + '"]');
                    if (radio) radio.checked = true;
                }
                var savedDays = localStorage.getItem("scan_recent_days");
                if (savedDays) {
                    _scanRecentDays = parseInt(savedDays) || 1;
                    document.getElementById("scan-recent-days").value = _scanRecentDays;
                }
                var savedSources = localStorage.getItem("scan_sources");
                if (savedSources) {
                    var arr = savedSources.split(",");
                    var valid = [];
                    for (var i = 0; i < arr.length; i++) {
                        var v = arr[i].trim();
                        if (v === "zxg" || v === "sz50" || v === "hs300" || v === "zz500" || v === "zz1000" || v === "page_index" || v === "tdxhy2" || v === "tdxhy3") {
                            valid.push(v);
                        }
                    }
                    if (valid.length > 0) {
                        _scanSources = valid;
                        // 先全部取消，再勾选保存的
                        var allCbs = document.querySelectorAll('input[name="scan-source"]');
                        for (var i = 0; i < allCbs.length; i++) { allCbs[i].checked = false; }
                        for (var i = 0; i < valid.length; i++) {
                            var cb = document.querySelector('input[name="scan-source"][value="' + valid[i] + '"]');
                            if (cb) cb.checked = true;
                        }
                    }
                }
                var savedFreq = localStorage.getItem("scan_freq");
                if (savedFreq && ["w", "d", "30m", "5m"].indexOf(savedFreq) >= 0) {
                    _scanFreq = savedFreq;
                    var radio = document.querySelector('input[name="scan-freq"][value="' + savedFreq + '"]');
                    if (radio) radio.checked = true;
                }
            } catch(e) {}
            // "成分股"选项一直可见：当前页面是可获取成分股的指数时可用，否则灰化禁用
            var pageIndexLabel = document.getElementById("label-page-index");
            var pageIndexCb = document.querySelector('input[name="scan-source"][value="page_index"]');
            if (pageIndexLabel && pageIndexCb) {
                var sym = chartData && chartData.meta && chartData.meta.symbol;
                // 可获取成分股的指数：通达信板块(88xxxx)、深市指数(399xxx)、中证系列(932xxx)、
                // 上海指数(000xxx)。注意排除深市主板股票(000xxx.SZ)的误判。
                var isSectorIndex = sym && (
                    /^88\d{4}/.test(sym) || /^399\d{3}/.test(sym) || /^932\d{3}/.test(sym) ||
                    (/^000\d{3}/.test(sym) && !/\.[Ss][Zz]$/.test(sym))
                );
                if (isSectorIndex) {
                    pageIndexLabel.style.opacity = "1";
                    pageIndexLabel.style.pointerEvents = "";
                    pageIndexCb.disabled = false;
                } else {
                    pageIndexLabel.style.opacity = "0.35";
                    pageIndexLabel.style.pointerEvents = "none";
                    pageIndexCb.checked = false;
                    pageIndexCb.disabled = true;
                }
            }
            // 根据当前模式设置"最近N根"输入框的灰化状态
            updateScanRecentDisabled();
            document.getElementById("scan-mode-dialog").classList.add("show");
        };

        // 多来源合并：后端统一合并去重，前端只需传逗号分隔的来源列表
        function _fetchMergedStocks(sources, freq) {
            var url = "/api/scan_stock_list?source=" + sources.join(",");
            if (freq) url += "&freq=" + freq;
            return fetch(url)
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.errors && data.errors.length > 0) {
                        console.warn("[扫描] 后端合并警告:", data.errors.join("; "));
                    }
                    return { stocks: data.stocks || [], pre_skipped: data.pre_skipped || 0 };
                });
        }

        // 实际执行扫描（由对话框确认后调用）
        function doStartScan() {
            var panel = document.getElementById("scan-panel");
            var body = document.getElementById("scan-body");
            var status = document.getElementById("scan-status");
            var btn = document.getElementById("btn-scan");

            panel.classList.add("show");
            panel.classList.remove("minimized");
            btn.classList.add("active");
            _scanRunning = true;
            _scanAborted = false;

            var freq = _scanFreq;
            updateScanTitle();
            status.textContent = "";

            // 标注扫描模式：直接查询标注缓存（全周期，不按 freq 过滤，与扫描来源无关）
            if (_scanMode === "ann") {
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在查询标注数据...</div>';
                fetch("/api/annotations_scan")
                .then(function(resp) { return resp.json(); })
                .then(function(annData) {
                    console.log("[标注扫描] 完成，共 " + (annData.codes ? annData.codes.length : 0) + " 条记录");
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "股票扫描";

                    var codes = annData.codes || [];

                    var html = '<div class="scan-summary">全周期标注 <b>' + codes.length + '</b> 条</div>';
                    if (codes.length === 0) {
                        html += '<div class="scan-no-result">未发现标注股票</div>';
                    } else {
                        var freqLabelMap = {"d": "日K", "w": "周K", "30m": "30分", "5m": "5分", "60m": "60分", "1m": "1分", "15s": "15秒"};
                        codes.forEach(function(c) {
                            var rCode = c.code + "." + c.market;
                            var rFreqLabel = freqLabelMap[c.freq] || c.freq;
                            // 取日期最靠近当前日期的标注文字，最多11字
                            var closestText = "";
                            if (c.annotations && c.annotations.length > 0) {
                                var today = new Date();
                                today.setHours(0, 0, 0, 0);
                                var closest = null;
                                var closestDiff = Infinity;
                                c.annotations.forEach(function(a) {
                                    var d = new Date(a.date.replace(/\//g, "-"));
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
                            html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + rCode + '\', \'' + c.freq + '\')" title="点击查看K线图">';
                            html += chkBox(rCode, false);
                            html += '<span class="scan-col-name">' + (c.name || rCode) + '</span>';
                            html += '<span class="scan-col-code">' + rCode + '</span>';
                            html += '<span class="scan-col-freq">' + rFreqLabel + '</span>';
                            html += '<span class="scan-col-ann">' + closestText + '</span>';
                            html += '<span class="scan-col-tags"><span class="scan-bsp-tag buy">' + c.count + '条</span></span>';
                            html += '</div>';
                        });
                    }
                    body.innerHTML = html;
                    updateScanSaveBtn();
                })
                .catch(function(err) {
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "股票扫描";
                    body.innerHTML = '<div class="scan-no-result">查询失败: ' + err.message + '</div>';
                });
                return;
            }

            // 底分型扫描模式：找到指定周期中最后一个分型是底分型的个股
            if (_scanMode === "fx_d") {
                var sourceLabel = _scanSourceLabel();
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取：' + sourceLabel + '...</div>';
                var freq = _scanFreq;
                Promise.all([
                    fetch("/api/scan_start"),
                    _fetchMergedStocks(_scanSources, freq)
                ])
                    .then(function(resps) {
                        return resps[0].json().then(function(scanStartData) {
                            if (scanStartData.need_refresh) {
                                _scanRunning = false;
                                btn.classList.remove("active");
                                body.innerHTML = '<div class="scan-no-result" style="text-align:center;padding:20px;">' +
                                    '<div style="font-size:14px;color:#e94560;margin-bottom:12px;">&#9888; ' + scanStartData.msg + '</div>' +
                                    '<button class="btn" onclick="refreshStockNames();closeScanPanel();" style="margin-top:8px;">立即刷新</button>' +
                                    '</div>';
                                return null;
                            }
                            return resps[1];
                        });
                    })
                    .then(function(data) {
                        if (data === null) return;
                        if (!data || !data.stocks || data.stocks.length === 0) {
                            _scanRunning = false;
                            btn.classList.remove("active");
                            body.innerHTML = '<div class="scan-no-result">' + sourceLabel + '列表为空或文件不存在</div>';
                            return;
                        }
                        var stocks = data.stocks;
                        var total = stocks.length;
                        var preSkipped = data.pre_skipped || 0;
                        console.log("[底分型扫描] 合并后股票总数: " + total + " 只, 来源: " + _scanSources.join(","));
                        var results = [];
                        var skipped = 0;
                        var currentIdx = 0;
                        var completed = 0;
                        var hasRenderedAny = false;

                        body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，底分型 0 只（0 / 0 / 0）</div>';

                        function finishScan(interrupted) {
                            fetch("/api/scan_end").then(function() {
                                renderFxDScanResults(results, total + preSkipped, preSkipped + skipped, interrupted);
                            });
                        }

                        var _updateTimer = null;
                        var _pendingUpdate = false;
                        function updatePanel() {
                            if (_updateTimer) {
                                _pendingUpdate = true;
                                return;
                            }
                            _doUpdatePanel();
                            _updateTimer = setInterval(function() {
                                if (_pendingUpdate) {
                                    _pendingUpdate = false;
                                    _doUpdatePanel();
                                } else {
                                    clearInterval(_updateTimer);
                                    _updateTimer = null;
                                }
                            }, 500);
                        }
                        function _doUpdatePanel() {
                            var progress = completed + "/" + total;
                            var totalSkipped = preSkipped + skipped;
                            var strongest = 0, strong = 0, weak = 0;
                            for (var i = 0; i < results.length; i++) {
                                var s = results[i].fx_strength;
                                if (s === 2) { strongest++; }
                                else if (s === 1) { strong++; }
                                else { weak++; }
                            }
                            var fxSummary = results.length + ' 只（' + strongest + ' / ' + strong + ' / ' + weak + '）';
                            var html = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + progress + '，跳过 ' + totalSkipped + ' 只，底分型 ' + fxSummary + '</div>';
                            if (results.length > 0) {
                                hasRenderedAny = true;
                                var shCount = 0, szCount = 0, bjCount = 0, hkCount = 0;
                                for (var i = 0; i < results.length; i++) {
                                    var parts = results[i].code.split(".");
                                    var mkt = parts.length > 1 ? parts[1] : "";
                                    if (mkt === "SH") { shCount++; }
                                    else if (mkt === "SZ") { szCount++; }
                                    else if (mkt === "BJ") { bjCount++; }
                                    else if (mkt === "HK") { hkCount++; }
                                }
                                var marketParts = [];
                                if (shCount > 0) marketParts.push("上海 <b>" + shCount + "</b> 只");
                                if (szCount > 0) marketParts.push("深圳 <b>" + szCount + "</b> 只");
                                if (bjCount > 0) marketParts.push("北京 <b>" + bjCount + "</b> 只");
                                if (hkCount > 0) marketParts.push("香港 <b>" + hkCount + "</b> 只");
                                html += '<div class="scan-summary" style="margin-top:8px;">' + marketParts.join("，") + '</div>';
                                // 按分型强度降序排序（最强分型→强分型→弱分型）
                                results.sort(function(a, b) { return b.fx_strength - a.fx_strength; });
                                for (var i = 0; i < results.length; i++) {
                                    var r = results[i];
                                    var fxLabel = '底分型';
                                    var fxClass = 'fx-d';
                                    var checked = false;
                                    if (r.fx_strength === 2) { fxLabel = '最强分型'; fxClass = 'fx-strongest'; checked = true; }
                                    else if (r.fx_strength === 1) { fxLabel = '强分型'; fxClass = 'fx-strong'; checked = true; }
                                    else { fxLabel = '弱分型'; fxClass = 'fx-weak'; }
                                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                                    html += chkBox(r.code, checked);
                                    html += '<span class="scan-col-name">' + r.name + '</span>';
                                    html += '<span class="scan-col-code">' + r.code + '</span>';
                                    html += '<span class="scan-col-tags"><span class="scan-bsp-tag ' + fxClass + '">' + fxLabel + '</span></span>';
                                    html += '</div>';
                                }
                            }
                            body.innerHTML = html;
                            updateScanSaveBtn();
                        }

                        function checkDone() {
                            if (_updateTimer) { clearInterval(_updateTimer); _updateTimer = null; }
                            if (_scanAborted) {
                                _scanRunning = false;
                                _scanAborted = false;
                                btn.classList.remove("active");
                                btn.disabled = false;
                                btn.textContent = "股票扫描";
                                finishScan(true);
                                return;
                            }
                            if (completed >= total) {
                                _scanRunning = false;
                                btn.classList.remove("active");
                                btn.disabled = false;
                                btn.textContent = "股票扫描";
                                finishScan(false);
                                return;
                            }
                        }

                        var CONCURRENCY = 5;
                        btn.textContent = "中断扫描";

                        function launchBatch() {
                            if (_scanAborted) return;
                            var batch = [];
                            while (currentIdx < total && batch.length < CONCURRENCY) {
                                batch.push(stocks[currentIdx]);
                                currentIdx++;
                            }
                            if (batch.length === 0) return;
                            var batchDone = 0;
                            var batchSize = batch.length;
                            batch.forEach(function(stk) {
                                var code = stk.code;
                                var prefix = stk.prefix;
                                fetch("/api/scan_one?code=" + code + "&freq=" + freq + "&prefix=" + prefix + "&mode=fx_d&source=" + (stk._source || "zxg") + "&_t=" + Date.now())
                                    .then(function(resp) { return resp.json(); })
                                    .then(function(data) {
                                        completed++;
                                        if (data.skipped) {
                                            skipped++;
                                        } else if (data.error) {
                                            skipped++;
                                        } else if (data.is_fx_d) {
                                            results.push(data);
                                        }
                                        batchDone++;
                                        if (batchDone >= batchSize) {
                                            setTimeout(function() {
                                                updatePanel();
                                                if (currentIdx < total) {
                                                    launchBatch();
                                                    if (_scanAborted) { checkDone(); }
                                                } else {
                                                    checkDone();
                                                }
                                            }, 0);
                                        }
                                    })
                                    .catch(function(err) {
                                        completed++;
                                        skipped++;
                                        batchDone++;
                                        if (batchDone >= batchSize) {
                                            setTimeout(function() {
                                                updatePanel();
                                                if (currentIdx < total) {
                                                    launchBatch();
                                                    if (_scanAborted) { checkDone(); }
                                                } else {
                                                    checkDone();
                                                }
                                            }, 0);
                                        }
                                    });
                            });
                        }

                        launchBatch();
                    })
                    .catch(function(err) {
                        _scanRunning = false;
                        btn.classList.remove("active");
                        btn.textContent = "股票扫描";
                        body.innerHTML = '<div class="scan-no-result">读取' + sourceLabel + '失败: ' + err.message + '</div>';
                    });
                return;
            }

            // 均线分类扫描模式：按最新收盘价未攻克的最小周期均线分类
            if (_scanMode === "ma") {
                var sourceLabel = _scanSourceLabel();
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取：' + sourceLabel + '...</div>';
                var freq = _scanFreq;
                Promise.all([
                    fetch("/api/scan_start"),
                    _fetchMergedStocks(_scanSources, freq)
                ])
                    .then(function(resps) {
                        return resps[0].json().then(function(scanStartData) {
                            if (scanStartData.need_refresh) {
                                _scanRunning = false;
                                btn.classList.remove("active");
                                body.innerHTML = '<div class="scan-no-result" style="text-align:center;padding:20px;">' +
                                    '<div style="font-size:14px;color:#e94560;margin-bottom:12px;">&#9888; ' + scanStartData.msg + '</div>' +
                                    '<button class="btn" onclick="refreshStockNames();closeScanPanel();" style="margin-top:8px;">立即刷新</button>' +
                                    '</div>';
                                return null;
                            }
                            return resps[1];
                        });
                    })
                    .then(function(data) {
                        if (data === null) return;
                        if (!data || !data.stocks || data.stocks.length === 0) {
                            _scanRunning = false;
                            btn.classList.remove("active");
                            body.innerHTML = '<div class="scan-no-result">' + sourceLabel + '列表为空或文件不存在</div>';
                            return;
                        }
                        var stocks = data.stocks;
                        var total = stocks.length;
                        var preSkipped = data.pre_skipped || 0;
                        console.log("[均线分类扫描] 合并后股票总数: " + total + " 只, 来源: " + _scanSources.join(","));
                        var results = [];
                        var skipped = 0;
                        var currentIdx = 0;
                        var completed = 0;
                        var hasRenderedAny = false;

                        body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，命中 0 只</div>';

                        function finishScan(interrupted) {
                            fetch("/api/scan_end").then(function() {
                                renderMaScanResults(results, total + preSkipped, preSkipped + skipped, interrupted);
                            });
                        }

                        var _updateTimer = null;
                        var _pendingUpdate = false;
                        function updatePanel() {
                            if (_updateTimer) {
                                _pendingUpdate = true;
                                return;
                            }
                            _doUpdatePanel();
                            _updateTimer = setInterval(function() {
                                if (_pendingUpdate) {
                                    _pendingUpdate = false;
                                    _doUpdatePanel();
                                } else {
                                    clearInterval(_updateTimer);
                                    _updateTimer = null;
                                }
                            }, 500);
                        }
                        function _doUpdatePanel() {
                            var progress = completed + "/" + total;
                            var totalSkipped = preSkipped + skipped;
                            var html = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + progress + '，跳过 ' + totalSkipped + ' 只，命中 ' + results.length + ' 只</div>';
                            if (results.length > 0) {
                                hasRenderedAny = true;
                                var catCounts = {};
                                for (var i = 0; i < results.length; i++) {
                                    var c = results[i].ma_category;
                                    catCounts[c] = (catCounts[c] || 0) + 1;
                                }
                                var catParts = [];
                                for (var cat = 0; cat <= 8; cat++) {
                                    if (catCounts[cat]) catParts.push("类" + cat + " <b>" + catCounts[cat] + "</b> 只");
                                }
                                html += '<div class="scan-summary" style="margin-top:8px;">' + catParts.join("，") + '</div>';
                                // 按类别升序排序（类1→类9，最强→最弱）
                                results.sort(function(a, b) { return a.ma_category - b.ma_category; });
                                for (var i = 0; i < results.length; i++) {
                                    var r = results[i];
                                    var cat = r.ma_category;
                                    var catClass = 'ma-cat' + cat;
                                    var catLabel = '类' + cat;
                                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                                    html += chkBox(r.code, cat <= 2);
                                    html += '<span class="scan-col-name">' + r.name + '</span>';
                                    html += '<span class="scan-col-code">' + r.code + '</span>';
                                    html += '<span class="scan-col-tags"><span class="scan-bsp-tag ' + catClass + '">' + catLabel + '</span></span>';
                                    html += '</div>';
                                }
                            }
                            body.innerHTML = html;
                            updateScanSaveBtn();
                        }

                        function checkDone() {
                            if (_updateTimer) { clearInterval(_updateTimer); _updateTimer = null; }
                            if (_scanAborted) {
                                _scanRunning = false;
                                _scanAborted = false;
                                btn.classList.remove("active");
                                btn.disabled = false;
                                btn.textContent = "股票扫描";
                                finishScan(true);
                                return;
                            }
                            if (completed >= total) {
                                _scanRunning = false;
                                btn.classList.remove("active");
                                btn.disabled = false;
                                btn.textContent = "股票扫描";
                                finishScan(false);
                                return;
                            }
                        }

                        var CONCURRENCY = 5;
                        btn.textContent = "中断扫描";

                        function launchBatch() {
                            if (_scanAborted) return;
                            var batch = [];
                            while (currentIdx < total && batch.length < CONCURRENCY) {
                                batch.push(stocks[currentIdx]);
                                currentIdx++;
                            }
                            if (batch.length === 0) return;
                            var batchDone = 0;
                            var batchSize = batch.length;
                            batch.forEach(function(stk) {
                                var code = stk.code;
                                var prefix = stk.prefix;
                                fetch("/api/scan_one?code=" + code + "&freq=" + freq + "&prefix=" + prefix + "&mode=ma&source=" + (stk._source || "zxg") + "&_t=" + Date.now())
                                    .then(function(resp) { return resp.json(); })
                                    .then(function(data) {
                                        completed++;
                                        if (data.skipped) {
                                            skipped++;
                                        } else if (data.ma_category !== undefined && data.ma_category >= 0) {
                                            results.push({
                                                code: data.code,
                                                name: data.name,
                                                ma_category: data.ma_category,
                                                last_close: data.last_close
                                            });
                                        }
                                        batchDone++;
                                        if (batchDone >= batchSize) {
                                            setTimeout(function() {
                                                updatePanel();
                                                if (currentIdx < total) {
                                                    launchBatch();
                                                    if (_scanAborted) { checkDone(); }
                                                } else {
                                                    checkDone();
                                                }
                                            }, 0);
                                        }
                                    })
                                    .catch(function(err) {
                                        completed++;
                                        skipped++;
                                        batchDone++;
                                        if (batchDone >= batchSize) {
                                            setTimeout(function() {
                                                updatePanel();
                                                if (currentIdx < total) {
                                                    launchBatch();
                                                    if (_scanAborted) { checkDone(); }
                                                } else {
                                                    checkDone();
                                                }
                                            }, 0);
                                        }
                                    });
                            });
                        }

                        launchBatch();
                    })
                    .catch(function(err) {
                        _scanRunning = false;
                        btn.classList.remove("active");
                        btn.textContent = "股票扫描";
                        body.innerHTML = '<div class="scan-no-result">读取' + sourceLabel + '失败: ' + err.message + '</div>';
                    });
                return;
            }

            // 买卖点扫描模式（原有逻辑）
            // 第一步：通知后端开始新扫描 + 合并多来源股票列表
            var sourceLabel = _scanSourceLabel();
            body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取：' + sourceLabel + '...</div>';
            Promise.all([
                fetch("/api/scan_start"),
                _fetchMergedStocks(_scanSources, freq)
            ])
                .then(function(resps) {
                    // 先检查 scan_start 的响应
                    return resps[0].json().then(function(scanStartData) {
                        if (scanStartData.need_refresh) {
                            // 需要刷新缓存数据
                            _scanRunning = false;
                            btn.classList.remove("active");
                            body.innerHTML = '<div class="scan-no-result" style="text-align:center;padding:20px;">' +
                                '<div style="font-size:14px;color:#e94560;margin-bottom:12px;">&#9888; ' + scanStartData.msg + '</div>' +
                                '<button class="btn" onclick="refreshStockNames();closeScanPanel();" style="margin-top:8px;">立即刷新</button>' +
                                '</div>';
                            return null;
                        }
                        // scan_start 正常，继续处理股票列表（_fetchMergedStocks 已返回去重数组）
                        return resps[1];
                    });
                })
                .then(function(data) {
                    if (data === null) return; // 已处理 need_refresh
                    if (!data || !data.stocks || data.stocks.length === 0) {
                        _scanRunning = false;
                        btn.classList.remove("active");
                        body.innerHTML = '<div class="scan-no-result">' + sourceLabel + '列表为空或文件不存在</div>';
                        return;
                    }
                    var stocks = data.stocks;
                    var total = stocks.length;
                    var preSkipped = data.pre_skipped || 0;
                    console.log("[买卖点扫描] 合并后股票总数: " + total + " 只, 来源: " + _scanSources.join(","));
                    var results = [];
                    var skipped = 0;
                    var currentIdx = 0;
                    var completed = 0;
                    var hasRenderedAny = false;

                    // 立即更新面板为扫描进度，不等第一批请求返回
                    body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，买点 0 只，卖点 0 只</div>';

                    // 扫描结束统一通知后端打印
                    function finishScan(interrupted) {
                        fetch("/api/scan_end").then(function() {
                            renderScanResults(results, total + preSkipped, preSkipped + skipped, interrupted);
                        });
                    }

                    // 实时更新面板：显示进度 + 已找到的买卖点股票
                    var _updateTimer = null;
                    var _pendingUpdate = false;
                    function updatePanel() {
                        // 节流：最多500ms更新一次，避免阻塞主线程导致"中断扫描"按钮无响应
                        if (_updateTimer) {
                            _pendingUpdate = true;
                            return;
                        }
                        _doUpdatePanel();
                        _updateTimer = setInterval(function() {
                            if (_pendingUpdate) {
                                _pendingUpdate = false;
                                _doUpdatePanel();
                            } else {
                                clearInterval(_updateTimer);
                                _updateTimer = null;
                            }
                        }, 500);
                    }
                    function _doUpdatePanel() {
                        var progress = completed + "/" + total;
                        var totalSkipped = preSkipped + skipped;
                        var buyCount = 0, sellCount = 0;
                        for (var i = 0; i < results.length; i++) {
                            if (isLatestBspBuy(results[i])) { buyCount++; } else { sellCount++; }
                        }
                        var html = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + progress + '，跳过 ' + totalSkipped + ' 只，买点 ' + buyCount + ' 只，卖点 ' + sellCount + ' 只</div>';
                        // 如果已经找到一些结果，实时显示出来
                        if (results.length > 0) {
                            hasRenderedAny = true;
                            var shCount = 0, szCount = 0, bjCount = 0, hkCount = 0;
                            for (var i = 0; i < results.length; i++) {
                                var parts = results[i].code.split(".");
                                var mkt = parts.length > 1 ? parts[1] : "";
                                if (mkt === "SH") { shCount++; }
                                else if (mkt === "SZ") { szCount++; }
                                else if (mkt === "BJ") { bjCount++; }
                                else if (mkt === "HK") { hkCount++; }
                            }
                            var marketParts = [];
                            if (shCount > 0) marketParts.push("上海 <b>" + shCount + "</b> 只");
                            if (szCount > 0) marketParts.push("深圳 <b>" + szCount + "</b> 只");
                            if (bjCount > 0) marketParts.push("北京 <b>" + bjCount + "</b> 只");
                            if (hkCount > 0) marketParts.push("香港 <b>" + hkCount + "</b> 只");
                            html += '<div class="scan-summary" style="margin-top:8px;">' + marketParts.join("，") + '</div>';
                            // 按最新买卖点类型排序：先买点后卖点，内部 1→2→3→0
                            results.sort(function(a, b) { return getLatestBspSortKey(a) - getLatestBspSortKey(b); });
                            for (var i = 0; i < results.length; i++) {
                                var r = results[i];
                                var tagsHtml = buildBspTagsHtml(r.buy_points, r.sell_points);
                                html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                                html += chkBox(r.code, isLatestBspBuy(r));
                                html += '<span class="scan-col-name">' + r.name + '</span>';
                                html += '<span class="scan-col-code">' + r.code + '</span>';
                                html += '<span class="scan-col-tags">' + tagsHtml + '</span>';
                                html += '</div>';
                            }
                        }
                        body.innerHTML = html;
                        updateScanSaveBtn();
                    }

                    function checkDone() {
                        if (_updateTimer) { clearInterval(_updateTimer); _updateTimer = null; }
                        if (_scanAborted) {
                            _scanRunning = false;
                            _scanAborted = false;
                            btn.classList.remove("active");
                            btn.disabled = false;
                            btn.textContent = "股票扫描";
                            finishScan(true);
                            return;
                        }
                        if (completed >= total) {
                            _scanRunning = false;
                            btn.classList.remove("active");
                            btn.disabled = false;
                            btn.textContent = "股票扫描";
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
                        var batchDone = 0;
                        var batchSize = batch.length;
                        batch.forEach(function(stk) {
                            var code = stk.code;
                            var prefix = stk.prefix;
                            fetch("/api/scan_one?code=" + code + "&freq=" + freq + "&prefix=" + prefix + "&recent=" + _scanRecentDays + "&source=" + (stk._source || "zxg") + "&_t=" + Date.now())
                                .then(function(resp) { return resp.json(); })
                                .then(function(data) {
                                    completed++;
                                    if (data.skipped) {
                                        // 预过滤跳过，不计入 error
                                        skipped++;
                                    } else if (data.error) {
                                        skipped++;
                                    } else if ((data.buy_points && data.buy_points.length > 0) || (data.sell_points && data.sell_points.length > 0)) {
                                        results.push(data);
                                    }
                                    batchDone++;
                                    if (batchDone >= batchSize) {
                                        // 整批完成后只产生一个 setTimeout，点击事件有机会插入
                                        setTimeout(function() {
                                            updatePanel();
                                            if (currentIdx < total) {
                                                launchBatch();
                                                // launchBatch 可能因 _scanAborted 提前返回，此时需手动触发 checkDone
                                                if (_scanAborted) { checkDone(); }
                                            } else {
                                                checkDone();
                                            }
                                        }, 0);
                                    }
                                })
                                .catch(function(err) {
                                    completed++;
                                    skipped++;
                                    batchDone++;
                                    if (batchDone >= batchSize) {
                                        setTimeout(function() {
                                            updatePanel();
                                            if (currentIdx < total) {
                                                launchBatch();
                                                if (_scanAborted) { checkDone(); }
                                            } else {
                                                checkDone();
                                            }
                                        }, 0);
                                    }
                                });
                        });
                    }

                    // 启动初始批次（单链递归，每次发5个请求，完成后通过setTimeout推迟下一批，
                    // 让浏览器有机会处理点击"中断扫描"按钮）
                    launchBatch();
                })
                .catch(function(err) {
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "股票扫描";
                    body.innerHTML = '<div class="scan-no-result">读取' + sourceLabel + '失败: ' + err.message + '</div>';
                });
        };

        // ============================================================
        // 股票名称刷新（原 GBBQ 刷新，现在仅刷新股票名称缓存）
        // ============================================================
        window.refreshStockNames = function() {
            var btn = document.getElementById("btn-refresh");
            var status = document.getElementById("refresh-status");
            if (btn.disabled) return;
            btn.disabled = true;
            btn.classList.add("active");
            btn.querySelector("svg").style.animation = "spin 1s linear infinite";
            status.style.display = "inline";
            status.textContent = "正在刷新股票名称...";

            fetch("/api/refresh_stock_names")
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.status === "already_running") {
                        pollRefreshStatus(btn, status);
                    } else {
                        pollRefreshStatus(btn, status);
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

        function pollRefreshStatus(btn, status) {
            fetch("/api/refresh_status")
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.running) {
                        status.textContent = data.step || "刷新中...";
                        setTimeout(function() { pollRefreshStatus(btn, status); }, 500);
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
            var sourceLabel = _scanSourceLabel();
            var buyCount = 0, sellCount = 0;
            for (var i = 0; i < results.length; i++) {
                if (isLatestBspBuy(results[i])) { buyCount++; } else { sellCount++; }
            }
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total + '</b> 只，跳过 <b>' + skipped + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，买点 <b>' + buyCount + '</b> 只，卖点 <b>' + sellCount + '</b> 只' + label + '</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现买卖点股票</div>';
            } else {
                // 按最新买卖点类型排序：一类(1)→二类(2)→三类(3)→0类(0)
                results.sort(function(a, b) { return getLatestBspSortKey(a) - getLatestBspSortKey(b); });
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    var tagsHtml = buildBspTagsHtml(r.buy_points, r.sell_points);
                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                    html += chkBox(r.code, isLatestBspBuy(r));
                    html += '<span class="scan-col-name">' + r.name + '</span>';
                    html += '<span class="scan-col-code">' + r.code + '</span>';
                    html += '<span class="scan-col-tags">' + tagsHtml + '</span>';
                    html += '</div>';
                }
            }
            body.innerHTML = html;
            updateScanSaveBtn();
        }

        // 底分型扫描结果渲染
        function renderFxDScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var strongest = 0, strong = 0, weak = 0;
            for (var i = 0; i < results.length; i++) {
                var s = results[i].fx_strength;
                if (s === 2) { strongest++; }
                else if (s === 1) { strong++; }
                else { weak++; }
            }
            var fxSummary = results.length + ' 只（' + strongest + ' / ' + strong + ' / ' + weak + '）';
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total + '</b> 只，跳过 <b>' + skipped + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，底分型 <b>' + fxSummary + '</b>' + label + '</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现底分型股票</div>';
            } else {
                // 按分型强度降序排序（最强分型→强分型→弱分型）
                results.sort(function(a, b) { return b.fx_strength - a.fx_strength; });
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    var fxLabel = '底分型';
                    var fxClass = 'fx-d';
                    var checked = false;
                    if (r.fx_strength === 2) { fxLabel = '最强分型'; fxClass = 'fx-strongest'; checked = true; }
                    else if (r.fx_strength === 1) { fxLabel = '强分型'; fxClass = 'fx-strong'; checked = true; }
                    else { fxLabel = '弱分型'; fxClass = 'fx-weak'; }
                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                    html += chkBox(r.code, checked);
                    html += '<span class="scan-col-name">' + r.name + '</span>';
                    html += '<span class="scan-col-code">' + r.code + '</span>';
                    html += '<span class="scan-col-tags"><span class="scan-bsp-tag ' + fxClass + '">' + fxLabel + '</span></span>';
                    html += '</div>';
                }
            }
            body.innerHTML = html;
            updateScanSaveBtn();
        }

        // 均线分类扫描结果渲染
        function renderMaScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var catCounts = {};
            for (var i = 0; i < results.length; i++) {
                var c = results[i].ma_category;
                catCounts[c] = (catCounts[c] || 0) + 1;
            }
            var catParts = [];
            for (var cat = 0; cat <= 8; cat++) {
                if (catCounts[cat]) catParts.push("类" + cat + " <b>" + catCounts[cat] + "</b> 只");
            }
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total + '</b> 只，跳过 <b>' + skipped + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，' + (catParts.length > 0 ? catParts.join("，") : '无') + label + '</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现均线分类结果</div>';
            } else {
                // 按类别升序排序（类1→类9，最强→最弱）
                results.sort(function(a, b) { return a.ma_category - b.ma_category; });
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    var cat = r.ma_category;
                    var catClass = 'ma-cat' + cat;
                    var catLabel = '类' + cat;
                    var checked = cat <= 2;
                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                    html += chkBox(r.code, checked);
                    html += '<span class="scan-col-name">' + r.name + '</span>';
                    html += '<span class="scan-col-code">' + r.code + '</span>';
                    html += '<span class="scan-col-tags"><span class="scan-bsp-tag ' + catClass + '">' + catLabel + '</span></span>';
                    html += '</div>';
                }
            }
            body.innerHTML = html;
            updateScanSaveBtn();
        }

        // 生成复选框HTML
        function isLatestBspBuy(r) {
            var buyPoints = r.buy_points || [];
            var sellPoints = r.sell_points || [];
            if (buyPoints.length === 0 && sellPoints.length === 0) return false;
            var lastBuyDate = buyPoints.length > 0 ? buyPoints[buyPoints.length - 1].date : "";
            var lastSellDate = sellPoints.length > 0 ? sellPoints[sellPoints.length - 1].date : "";
            // 最近的是买点
            if (!lastBuyDate && !lastSellDate) return false;
            if (!lastSellDate) return true;
            if (!lastBuyDate) return false;
            return lastBuyDate >= lastSellDate;
        }

        // 获取最新买卖点的两层排序键：
        // 第一层：先买点(0) 后卖点(1)；第二层：1→2→3→0
        // 返回数值越小排越前：买点一类→1, 买点二类→2, 买点三类→3, 买点0类→4,
        //                       卖点一类→11, 卖点二类→12, 卖点三类→13, 卖点0类→14, 无买卖点→99
        function getLatestBspSortKey(r) {
            var buyPoints = r.buy_points || [];
            var sellPoints = r.sell_points || [];
            if (buyPoints.length === 0 && sellPoints.length === 0) return 99;
            var lastBuyDate = buyPoints.length > 0 ? buyPoints[buyPoints.length - 1].date : "";
            var lastSellDate = sellPoints.length > 0 ? sellPoints[sellPoints.length - 1].date : "";
            var latestPoint = null;
            var isBuy = false;
            if (!lastSellDate) { latestPoint = buyPoints[buyPoints.length - 1]; isBuy = true; }
            else if (!lastBuyDate) { latestPoint = sellPoints[sellPoints.length - 1]; isBuy = false; }
            else if (lastBuyDate >= lastSellDate) { latestPoint = buyPoints[buyPoints.length - 1]; isBuy = true; }
            else { latestPoint = sellPoints[sellPoints.length - 1]; isBuy = false; }
            var tp = (latestPoint.type || "").replace(/\s/g, "");
            var typeKey = 99;
            if (tp === "1") typeKey = 1;
            else if (tp === "2") typeKey = 2;
            else if (tp === "3") typeKey = 3;
            else if (tp === "0") typeKey = 4;
            return (isBuy ? 0 : 10) + typeKey;
        }

        function chkBox(code, checked) {
            return '<span class="scan-col-chk" onclick="event.stopPropagation()"><input type="checkbox" value="' + code + '" onchange="updateScanSaveBtn()" ' + (checked ? 'checked' : '') + '/></span>';
        }

        // 收集勾选的代码并更新按钮状态
        window.updateScanSaveBtn = function() {
            var checks = document.querySelectorAll("#scan-body .scan-col-chk input[type=checkbox]:checked");
            var allCbs = document.querySelectorAll("#scan-body .scan-col-chk input[type=checkbox]");
            var btn = document.getElementById("scan-save-btn");
            btn.disabled = allCbs.length === 0;
            btn.textContent = checks.length > 0 ? "保存到自选(" + checks.length + ")" : "保存到自选";
        };

        // 保存勾选到自选股（通达信+同花顺）
        window.saveScanToZxg = function() {
            var checks = document.querySelectorAll("#scan-body .scan-col-chk input[type=checkbox]:checked");
            if (checks.length === 0) return;
            var codes = [];
            checks.forEach(function(cb) { codes.push(cb.value); });
            var btn = document.getElementById("scan-save-btn");
            btn.disabled = true;
            btn.textContent = "保存中...";
            fetch("/api/zxg_save?codes=" + encodeURIComponent(codes.join(",")))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                console.log("[THS] 保存响应:", data);
                // 保存结果用与扫描面板汇总行一致的亮度：普通文字 #a8b2d1，高亮数字/状态 #e94560
                btn.style.opacity = "1";
                var parts = [];
                if (data.tdx_saved > 0) {
                    parts.push("<span style='color:#a8b2d1'>通达信：</span><span style='color:#e94560'>" + data.tdx_saved + "</span><span style='color:#a8b2d1'> 只</span>");
                } else {
                    parts.push("<span style='color:#a8b2d1'>通达信：</span><span style='color:#e94560'> 已保存</span>");
                }
                // 同花顺状态：区分成功/已存在/失败/未配置
                if (data.ths_saved > 0) {
                    parts.push("<span style='color:#a8b2d1'>同花顺：</span><span style='color:#e94560'>" + data.ths_saved + "</span><span style='color:#a8b2d1'> 只</span>");
                } else if (!data.ths_msg || data.ths_msg === "THS_DIR 未配置") {
                    // 未配置同花顺目录，静默不显示
                } else if (data.ths_msg === "ok") {
                    parts.push("<span style='color:#a8b2d1'>同花顺：</span><span style='color:#e94560'> 已保存</span>");
                } else {
                    parts.push("<span style='color:#a8b2d1'>同花顺：失败</span>");
                    console.warn("[THS] 保存失败:", data.ths_msg);
                }
                btn.innerHTML = parts.join("&nbsp;&nbsp;&nbsp;");
                setTimeout(function() {
                    btn.textContent = "保存到自选";
                    btn.disabled = false;
                    btn.style.opacity = "";
                    updateScanSaveBtn();
                }, 2000);
            })
            .catch(function() {
                btn.textContent = "保存失败";
                btn.disabled = false;
                btn.style.opacity = "";
            });
        };

        window.closeScanPanel = function() {
            // 扫描中不允许关闭面板，用户需通过"中断扫描"按钮停止
            if (_scanRunning) return;
            document.getElementById("scan-panel").classList.remove("show");
            // 关闭面板时清除扫描缓存，释放内存
            fetch("/api/scan_clear_cache").catch(function() {});
        };

        window.toggleScanMinimize = function() {
            // 最小化/恢复扫描面板，扫描可后台继续不中断
            var panel = document.getElementById("scan-panel");
            var btn = document.querySelector(".scan-minimize");
            panel.classList.toggle("minimized");
            if (btn) {
                btn.innerHTML = panel.classList.contains("minimized") ? "+" : "-";
                btn.title = panel.classList.contains("minimized") ? "恢复面板" : "最小化面板";
            }
        };

        window.loadScanResult = function(code, freq) {
            // 加载该股票到当前页面，不关闭面板
            // 传入 freq（标注所在周期），确保用正确的周期加载K线，避免标注因周期不匹配而不显示
            document.getElementById("stock-code-input").value = code;
            if (freq) {
                lastStockFreq = freq; // 让 loadStock 使用标注所在的周期
            }
            loadStock();
        };

        window.switchFreq = function(freq) {
            if (!chartData || currentFreq === freq) return;
            // 切换周期时取消区间选择
            if (_rangeSelect.mode === 'SELECTED_A') {
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
            }
            currentFreq = freq;
            updateDateInputType();
            updateDualBtn();
            const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            if (isFutures) {
                lastFuturesFreq = freq; // 期货上下文切换周期，记录
            } else {
                lastStockFreq = freq;   // 股票上下文切换周期，记录
            }
            updateFreqButtonStates(isFutures);
            // 切换周期后重新加载数据
            const code = document.getElementById("stock-code-input").value.trim() || chartData.meta.symbol;
            if (code) {
                document.getElementById("loading").classList.remove("hidden");
                // 期货：跳过HTTP，直接重连SSE（初始快照+增量合一）
                const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
                if (isFutures) {
                    disconnectRealtime();
                    if (isDualWindow) {
                        // 双窗口模式：用双窗口SSE
                        const newSubFreq = getDualSubFreq(freq);
                        dualSubFreq = newSubFreq;
                        connectRealtimeDual(code, freq, newSubFreq);
                    } else {
                        connectRealtimeInit(code, freq);
                    }
                    return;
                }
                fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq + (isDualWindow && getDualSubFreq(freq) ? "&dual=1" : ""))
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
                        const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, freq);
                        document.getElementById("goto-date-input").value = lastDate;
                        updateWeekday();
                        // 双窗口模式：从 data.sub 获取子级别数据（方案B）
                        if (isDualWindow) {
                            const newSubFreq = getDualSubFreq(freq);
                            if (newSubFreq) {
                                dualSubFreq = newSubFreq;
                                if (data.sub) {
                                    dualSubData = data.sub;
                                    dualSubViewCount = 377;
                                    dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                                    if (dualSubData.klines.length < dualSubViewCount) {
                                        dualSubViewOffset = 0;
                                    }
                                }
                            } else {
                                // 新周期是5m，双窗口已关闭
                            }
                        }
                        document.getElementById("loading").classList.add("hidden");
                        render();
                        generateStats();
                        loadAnnotations();
                        saveLastState(); // 保存状态
                        startRealtimeIfFutures(data);
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
            // 键盘Enter提供了精确日期，应在重置前捕获，用于跳过 isToday 安全网
            const keyEnter = _dateKeyEnter;
            // 重置所有日期输入标志位，避免上次手动输入/键盘操作阻塞后续日历点击
            _dateKeyEnter = false;
            _dateKeyArrow = false;
            _dateManualTyping = false;
            _datePickerInteracted = false;
            _datePickerInputCount = 0;
            if (!chartData) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            const dateStr = document.getElementById("goto-date-input").value.trim();
            if (!dateStr) return;
            const apiDate = inputDateToApi(dateStr, freq);
            // 日期是今天 → 冷启动（不传 end_date，加载全部K线）
            // 用本地日期避免 UTC 时区偏移（如 UTC+8 凌晨 0-8 点 toISOString 会返回昨天）
            const now = new Date();
            const todayStr = now.getFullYear() + '-' + String(now.getMonth()+1).padStart(2,'0') + '-' + String(now.getDate()).padStart(2,'0');
            const isToday = dateStr.startsWith(todayStr);
            // 期货：判断是否"回到最新/实时"——请求时间 ≥ 最后一根K线时间才算
            // （日内期货所有K线都是今天，不能用 isToday 判断，否则所有日内复盘都被拦截）
            const isFutures = chartData.meta.market === 'futures';
            const lastKlineInput = (chartData.klines && chartData.klines.length > 0)
                ? klineDateToInput(chartData.klines[chartData.klines.length - 1].date, freq)
                : "";
            const wantLive = isFutures && dateStr >= lastKlineInput;
            if (wantLive) {
                document.getElementById("goto-date-input").disabled = true;
                document.getElementById("loading").classList.remove("hidden");
                document.querySelector(".loading-text").textContent = "正在恢复实时行情...";
                if (isDualWindow && getDualSubFreq(freq)) {
                    disconnectRealtime();
                    connectRealtimeDual(code, freq, getDualSubFreq(freq));
                } else {
                    // 保留选点起始时间（若有），与手选后的SSE重连逻辑一致
                    const savedDate = chartData.meta.saved_selection_date || null;
                    connectRealtimeInit(code, freq, savedDate);
                }
                // 不在这里隐藏loading，SSE的init事件回调会处理loading隐藏和input恢复
                return;
            }
            // 复盘模式下断开实时连接（请求时间早于最新K线才走到这里）
            disconnectRealtime();
            // 股票：isToday安全网只给日历"今天"用（Edge时间未变时兜底）
            // 键盘Enter/右键复盘至此有精确日期 → 跳过isToday安全网，始终传end_date
            // 期货：wantLive已判断"回实时"，走到这里说明wantLive=false，始终传end_date
            const needEndDate = isFutures ? true : (!isToday || keyEnter);
            const url = "/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq
                + (needEndDate ? "&end_date=" + encodeURIComponent(apiDate) : "")
                + (isDualWindow && getDualSubFreq(freq) ? "&dual=1" : "");
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
                    // 双窗口模式：从 data.sub 恢复子级别数据
                    if (isDualWindow && data.sub) {
                        dualSubData = data.sub;
                        dualSubViewCount = 377;
                        dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                        if (dualSubData.klines.length < dualSubViewCount) {
                            dualSubViewOffset = 0;
                        }
                    }
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
                    // 复盘后输入框显示实际最后一根K线日期
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
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
        let _dateInputTriggered = false;   // input 已触发 gotoDate，change 跳过
        let _dateFocusOriginal = "";       // onfocus 保存的原始值，用于 blur 恢复
        let _datePickerInteracted = false; // datetime-local picker 中用户有过交互，blur 时不恢复原始值
        let _datePickerInputCount = 0;     // datetime-local picker 打开后真实交互次数
        window.handleDateKeydown = function(e) {
            if (e.key === 'Enter') { _dateKeyEnter = true; gotoDate(); return; }
            if (e.key.startsWith('Arrow')) { _dateKeyArrow = true; return; }
            if (e.key !== 'Tab' && e.key !== 'Escape') { _dateManualTyping = true; }
        };
        window.handleDateChange = function() {
            if (_dateStepIgnore) return;
            updateWeekday();
            // 键盘/手动输入 → 不触发（Enter 已在 handleDateKeydown 中处理）
            if (_dateKeyEnter) { _dateKeyEnter = false; return; }
            if (_dateKeyArrow) { _dateKeyArrow = false; return; }
            if (_dateManualTyping) { _dateManualTyping = false; return; }
            // input 已处理（datetime-local "今天"），change 跳过避免重复
            if (_dateInputTriggered) { _dateInputTriggered = false; return; }
            // datetime-local 正常完成（用户选完日期+小时+分钟，picker关闭）→ 触发
            _dateFocusOriginal = "";
            _datePickerInteracted = false;
            _datePickerInputCount = 0;
            // 期货兜底：Edge点击日历"今天"时handleDateInput的检测可能未触发，
            // 此时dateStr的日期=今天但时间未变，wantLive可能为false，应强制设为23:59再判断
            if (chartData && chartData.meta && chartData.meta.market === 'futures') {
                var input2 = document.getElementById("goto-date-input");
                if (input2.type === "datetime-local") {
                    var now3 = new Date();
                    var ts3 = now3.getFullYear() + '-' + String(now3.getMonth()+1).padStart(2,'0') + '-' + String(now3.getDate()).padStart(2,'0');
                    if (input2.value.startsWith(ts3)) {
                        input2.value = ts3 + 'T23:59';
                    }
                }
            }
            gotoDate();
        };
        window.handleDateBlur = function() {
            const input = document.getElementById("goto-date-input");
            var v = input.value;
            // 期货兜底：复盘后点击日历"今天"，Edge的step="15"输入框可能不触发input/change事件
            // 在blur时检测：如果当前处于复盘状态(chartData.meta.is_replay)且日期=今天 → 强制设23:59并触发gotoDate
            if (chartData && chartData.meta && chartData.meta.market === 'futures'
                && chartData.meta.is_replay && input.type === "datetime-local") {
                var nowB = new Date();
                var tsB = nowB.getFullYear() + '-' + String(nowB.getMonth()+1).padStart(2,'0') + '-' + String(nowB.getDate()).padStart(2,'0');
                var datePart = v.split('T')[0] || "";
                if (datePart === tsB) {
                    // 用户点了"今天"但input/change未触发 → 直接恢复实时
                    input.value = tsB + 'T23:59';
                    _dateFocusOriginal = "";
                    _datePickerInteracted = false;
                    _datePickerInputCount = 0;
                    gotoDate();
                    return;
                }
            }
            // picker 打开后用户未交互 → 恢复原始值
            if (_dateFocusOriginal && !_datePickerInteracted) {
                input.value = _dateFocusOriginal;
                _dateFocusOriginal = "";
            }
            _dateFocusOriginal = "";
            _datePickerInteracted = false;
            _datePickerInputCount = 0;
            v = input.value;
            if (!v) return;
            const parts = v.split('-');
            if (parts.length === 3) {
                const d = parseInt(parts[2], 10);
                if (!isNaN(d) && d > 31) {
                    input.value = parts[0] + '-' + parts[1] + '-31';
                }
            }
            // 股票 datetime-local：小时超出盘中范围(09-15)则自动修正
            if (input.type === "datetime-local" && chartData && chartData.meta && !isFuturesCode(chartData.meta.symbol)) {
                var p = input.value.split('T');
                if (p.length === 2) {
                    var tp = p[1].split(':');
                    var hh = parseInt(tp[0], 10);
                    if (hh < 9) input.value = p[0] + 'T09:' + tp[1];
                    else if (hh > 15) input.value = p[0] + 'T15:' + tp[1];
                }
            }
            updateWeekday();
        };
        window.handleDateInput = function(e) {
            const input = e.target;
            const val = input.value;
            if (!val) return;
            // 年份部分超过4位时截断到4位
            const firstDash = val.indexOf('-');
            if (firstDash === -1) {
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
            // 键盘输入 → 不在此处理（等待 Enter 或 change）
            if (_dateManualTyping || _dateKeyEnter || _dateKeyArrow) return;
            // datetime-local 日历交互：
            // - 用户选了日期/时间 → 标记 _datePickerInteracted，blur 时不再恢复原始值
            // - 第1次交互，日期=今天 且 时间≠原始时间 → "今天"按钮，立即触发
            // - 其他情况：不触发，等 change（正常完成选日期+小时+分钟后触发）
            if (input.type === "datetime-local" && _dateFocusOriginal) {
                _datePickerInteracted = true;
                _datePickerInputCount++;
                if (_datePickerInputCount === 1) {
                    var curParts = val.split('T');
                    var origParts = _dateFocusOriginal.split('T');
                    var now2 = new Date();
                    var todayStr = now2.getFullYear() + '-' + String(now2.getMonth()+1).padStart(2,'0') + '-' + String(now2.getDate()).padStart(2,'0');
                    if (curParts.length === 2 && origParts.length === 2 && curParts[0] === todayStr && curParts[1] !== origParts[1]) {
                        // "今天"按钮：日期=今天 且 时间变了 → 设为 23:59，立即触发
                        input.value = curParts[0] + 'T23:59';
                        _dateFocusOriginal = "";
                        _datePickerInteracted = false;
                        _datePickerInputCount = 0;
                        _dateInputTriggered = true;
                        gotoDate();
                        return;
                    }
                }
            }
        };

        // 期货：记录实时进入复盘时的边界日期（最后一根K线日期），右箭头至此自动重连
        let _futuresRealtimeBorderDate = null;

        window.dateStep = function(delta) {
            if (!chartData || !chartData.klines || chartData.klines.length === 0) return;
            var input = document.getElementById("goto-date-input");
            var isFutures = chartData.meta && chartData.meta.market === 'futures';

            // === 期货实时模式 ===
            if (isFutures && isRealtimeMode) {
                if (delta > 0) return; // 右箭头：已在最新，无需操作
                // 左箭头：先记录进入复盘前的最后一根K线日期，再断开SSE进入复盘
                if (chartData.klines.length > 0) {
                    _futuresRealtimeBorderDate = chartData.klines[chartData.klines.length - 1].date;
                    input.value = klineDateToInput(_futuresRealtimeBorderDate, currentFreq);
                }
                disconnectRealtime();
                isRealtimeMode = false;
            }

            var currentEndDate = inputDateToApi(input.value.trim(), currentFreq);
            if (!currentEndDate) return;

            _dateStepIgnore = true;
            fetchStep(currentEndDate, delta);
            _dateStepIgnore = false;
        };

        // 发送 step 请求，后端在 full_records 中偏移定位，全自动处理节假日/调休/跨周
        function fetchStep(endDate, delta) {
            var code = chartData.meta.symbol;
            var freq = currentFreq;
            var url = "/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq
                + "&end_date=" + encodeURIComponent(endDate) + "&step=" + delta
                + (isDualWindow && getDualSubFreq(freq) ? "&dual=1" : "");
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
                    if (isDualWindow && data.sub) {
                        dualSubData = data.sub;
                        dualSubViewCount = 377;
                        dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                        if (dualSubData.klines.length < dualSubViewCount) dualSubViewOffset = 0;
                    }
                    viewCount = 377;
                    adjustViewForSavedPoint();
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    if (chartData.klines.length < viewCount) viewOffset = 0;
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    var lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    resizeCanvas();
                    render();
                    loadAnnotations();
                    // 期货复盘模式：右箭头返回的最后一根K线 > 进入复盘时的边界 → 重连实时
                    var isFutures = chartData.meta && chartData.meta.market === 'futures';
                    if (isFutures && delta > 0 && _futuresRealtimeBorderDate) {
                        if (chartData.klines[chartData.klines.length - 1].date >= _futuresRealtimeBorderDate) {
                            var savedDate = chartData.meta.saved_selection_date || null;
                            connectRealtimeInit(chartData.meta.symbol, freq, savedDate);
                        }
                    }
                })
                .catch(err => { alert("箭头跳转失败: " + err.message); })
                .finally(() => {
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    document.getElementById("goto-date-input").disabled = false;
                });
        }

        window.updateWeekday = function() {
            var input = document.getElementById("goto-date-input");
            var span = document.getElementById("date-weekday");
            var v = input.value.trim();
            if (!v) { span.textContent = ""; return; }
            // 提取日期部分（兼容 datetime-local 的 T 分隔符）
            var datePart = v.split("T")[0];
            var parts = datePart.split('-');
            if (parts.length !== 3) { span.textContent = ""; return; }
            var d = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
            var weekNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
            span.textContent = weekNames[d.getDay()];
        };

        const HISTORY_KEY = "chan_stock_history";
        const MAX_HISTORY = 20;
        // 归一化股票代码：将 880974.SH / SH880974 / sh880974 统一为 sh880974
        function normalizeCode(code) {
            if (!code) return "";
            const c = code.trim();
            // 期货/期指代码（含 KQ.m@、KQ.i@、大写.大写 格式）不做转换
            if (c.includes('KQ.m@') || c.includes('KQ.i@') || /^[A-Z]+\.[A-Z]+$/.test(c)) return c;
            // 匹配 880974.SH 或 880974.sh 格式 → 转为 sh880974
            const dotMatch = c.match(/^(\d+)\.(SH|SZ|HK|BJ|DS)$/i);
            if (dotMatch) return dotMatch[2].toLowerCase() + dotMatch[1];
            // 匹配 SH880974 / sz000001 格式 → 转为小写前缀
            const prefixMatch = c.match(/^(SH|SZ|HK|BJ|DS)(\d+)$/i);
            if (prefixMatch) return prefixMatch[1].toLowerCase() + prefixMatch[2];
            return c;
        }
        // 固定快捷入口：五大核心指数，常驻历史列表顶部，不参与保存/删除/清除
        const FIXED_INDICES = [
            {code: "sh000001", name: "上证指数"},
            {code: "sz399001", name: "深证成指"},
            {code: "sh000300", name: "沪深300"},
            {code: "sh000905", name: "中证500"},
            {code: "sh000852", name: "中证1000"},
            {code: "sz399006", name: "创业板指"},
            {code: "sh000688", name: "科创50"},
        ];
        const FIXED_CODES = new Set(FIXED_INDICES.map(x => normalizeCode(x.code)));
        function isFixedCode(code) { return FIXED_CODES.has(normalizeCode(code)); }
        function getHistory() {
            try {
                let list = JSON.parse(localStorage.getItem(HISTORY_KEY)) || [];
                // 兼容旧格式（纯字符串）-> 转换为新格式（{code, name}）
                return list.map(c => typeof c === 'string' ? {code: c, name: ""} : c);
            } catch(e) { return []; }
        }
        function saveHistory(code, name) {
            const normCode = normalizeCode(code);
            // 固定快捷入口不写入历史，避免与顶部固定区重复
            if (isFixedCode(normCode)) return;
            let list = getHistory();
            list = list.filter(c => normalizeCode(c.code) !== normCode);
            list.unshift({code: normCode, name: name || ""});
            if (list.length > MAX_HISTORY) list = list.slice(0, MAX_HISTORY);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
        }
        function removeHistory(code) {
            const normCode = normalizeCode(code);
            let list = getHistory().filter(c => normalizeCode(c.code) !== normCode);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
            showHistory();
        }
        window.clearHistory = function() {
            localStorage.removeItem(HISTORY_KEY);
            // 仅清除用户历史，固定快捷入口保留并重新渲染
            showHistory();
        };
        window.clearInput = function() {
            const input = document.getElementById("stock-code-input");
            input.value = "";
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
            // 带市场前缀+完整代码（如 sh600519、sz000001、hk00700），不搜索
            // 要求前缀后紧跟5~6位数字且为完整输入，避免 SZ5 这样的拼音缩写被误拦截
            if (/^(sh|sz|bj|hk)\d{5,6}$/i.test(val)) {
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
                    if (data.need_refresh) {
                        el.innerHTML = '<div class="stock-history-item" style="color:#e94560;cursor:default;padding:10px;">' + (data.msg || '') + '</div>';
                        el.classList.add("show");
                        return;
                    }
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
            // 重置搜索态，确保历史视图下键盘导航不会误用旧的搜索结果
            searchResults = [];
            selectedIndex = -1;
            // 顶部固定快捷入口区（不可删除）
            let html = FIXED_INDICES.map(item => {
                const safe = item.code.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                return `<div class="stock-history-item"><span onclick="selectHistory('${safe}')" style="flex:1;display:block">${item.code} - ${item.name}</span></div>`;
            }).join("");
            // 用户浏览历史区（可单条删除）；首项顶部加分割线，与底部"清除全部"风格一致
            html += list.map((c, i) => {
                const safe = c.code.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                const label = c.name ? c.code + " - " + c.name : c.code;
                const sepStyle = i === 0 ? 'border-top:1px solid #0f3460;' : '';
                return `<div class="stock-history-item" style="${sepStyle}"><span onclick="selectHistory('${safe}')" style="flex:1;display:block">${label}</span><span class="stock-history-del" onclick="event.stopPropagation();removeHistory('${safe}')">&times;</span></div>`;
            }).join("");
            // 有用户历史时才显示"清除全部"（仅清用户历史，不影响固定项）
            if (list.length) {
                html += `<div class="stock-history-clear" onclick="event.stopPropagation();clearHistory()">清除全部</div>`;
            }
            el.innerHTML = html;
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
            const FUTURES_ALIAS_KEYS = new Set(["IF","IH","IC","IM","T","TF","TL","TS","CU","AL","ZN","PB","NI","SN","AO","AU","AG","RB","WR","HC","SS","BU","RU","FU","SP","BR","M","Y","A","B","P","J","JM","I","C","CS","L","V","PP","EG","EB","PG","FB","BB","RR","LH","JD","TA","PTA","MA","FG","SA","SR","CF","CY","OI","RM","ZC","UR","PF","PK","AP","CJ","SM","SF","SH","PX","LR","RI","JR","WH","PM","RS","SC","LU","NR","BC","EC","SI","LC","PS"]);
            const isFuturesCode = code.includes('KQ.m@') || code.includes('KQ.i@') || /^[A-Z]+\.[A-Z]/.test(code) || FUTURES_ALIAS_KEYS.has(code.toUpperCase());
            // 判断切换前是否为期指
            const wasFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            // 同类继承上一周期，异类使用默认周期
            let fetchFreq;
            if (wasFutures && isFuturesCode) {
                // 期指C → 期指D：保持C的周期
                fetchFreq = lastFuturesFreq;
            } else if (!wasFutures && !isFuturesCode) {
                // 股票A → 股票B：保持A的周期
                fetchFreq = lastStockFreq;
            } else if (wasFutures && !isFuturesCode) {
                // 期指 → 股票：默认日K，同时彻底清理所有期货数据
                disconnectRealtime();
                fetch("/api/futures_cleanup").catch(() => {});
                fetchFreq = 'd';
            } else {
                // 股票 → 期指：默认1分钟
                fetchFreq = '1m';
            }
            currentFreq = fetchFreq;
            updateDateInputType();
            if (isFuturesCode) {
                updateFreqButtonStates(true); // 期货：禁用 d/w，启用 1m/15s
                if (isDualWindow) {
                    const subFreq = getDualSubFreq(fetchFreq);
                    if (subFreq) {
                        connectRealtimeDual(code, fetchFreq, subFreq);
                        return;
                    }
                }
                connectRealtimeInit(code, fetchFreq);
                return;
            }
            updateFreqButtonStates(false); // 股票：禁用 1m/15s，启用 d/w
            fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + fetchFreq + (isDualWindow && getDualSubFreq(fetchFreq) ? "&dual=1" : ""))
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
                    updateDateInputType();
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
                    document.getElementById("btn-seg").classList.toggle("active", showSeg);
                    document.getElementById("btn-bsp").classList.toggle("active", showBsp);
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    resizeCanvas();
                    // 双窗口模式下同时加载下面窗口数据
                    if (isDualWindow) {
                        const subFreq = getDualSubFreq(currentFreq);
                        if (subFreq) {
                            dualSubFreq = subFreq;
                            if (data.sub) {
                                dualSubData = data.sub;
                                dualSubViewCount = 377;
                                dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                                if (dualSubData.klines.length < dualSubViewCount) {
                                    dualSubViewOffset = 0;
                                }
                            }
                        }
                    }
                    document.getElementById("loading").classList.add("hidden");
                    render();
                    generateStats();
                    loadAnnotations();
                    saveLastState(); // 保存状态
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
        function connectRealtimeInit(symbol, freq, startTime) {
            disconnectRealtime();
            _futuresRealtimeBorderDate = null; // 清除期货复盘边界
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
                let sseUrl = '/api/futures_stream?symbol=' + encodeURIComponent(symbol) + '&freq=' + encodeURIComponent(freq || '1m');
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
                            console.warn('引擎未就绪:', data.error);
                            disconnectRealtime();
                            document.getElementById("loading").classList.add("hidden");
                            return;
                        }
                        // 全量初始数据
                        chartData = data;
                        // 用后端解析后的完整代码保存历史，避免别名导致历史记录不一致
                        const resolvedSymbol = data.meta.symbol || symbol;
                        saveHistory(resolvedSymbol, data.meta.name);
                        // 同步 realtimeSymbol 为解析后的完整代码
                        realtimeSymbol = resolvedSymbol;
                        // 更新输入框为解析后的完整代码
                        document.getElementById("stock-code-input").value = resolvedSymbol;
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
                            const lastDate = klineDateToInput(data.klines[data.klines.length - 1].date, currentFreq);
                            document.getElementById("goto-date-input").value = lastDate;
                        }
                        updateWeekday();
                        document.getElementById("loading").classList.add("hidden");
                        document.getElementById("goto-date-input").disabled = false;
                        resizeCanvas();
                        render();
                        generateStats();
                    } catch(e) {
                        console.error('初始数据解析失败:', e);
                        document.getElementById("loading").classList.add("hidden");
                        document.getElementById("goto-date-input").disabled = false;
                    }
                });

                // update 事件：增量更新
                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeDataSingle(data);
                    } catch(e) {
                        console.error('实时数据解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    // 立即关闭EventSource，阻止浏览器自带重连
                    realtimeEventSource.close();
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
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
            }
        }

        // 期货双窗口SSE连接（独立于 connectRealtimeInit，与股票双窗口解耦）
        function connectRealtimeDual(symbol, mainFreq, subFreq) {
            disconnectRealtime();
            realtimeSymbol = symbol;
            realtimeFreq = mainFreq;
            dualSubFreq = subFreq;
            isRealtimeMode = true;
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';

            try {
                let sseUrl = '/api/futures_stream?symbol=' + encodeURIComponent(symbol)
                    + '&freq=' + mainFreq + '&dual=1&sub_freq=' + subFreq;
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                realtimeEventSource.addEventListener('init', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.main) {
                            chartData = data.main;
                            const resolvedSymbol = chartData.meta.symbol || symbol;
                            saveHistory(resolvedSymbol, chartData.meta.name);
                            realtimeSymbol = resolvedSymbol;
                            updateRestartBtn();
                            updateDualBtn();
                            const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d','周线':'w'};
                            currentFreq = freqMap[chartData.meta.freq] || currentFreq;
                            lastFuturesFreq = currentFreq;
                            viewCount = 377;
                            adjustViewForSavedPoint();
                            viewOffset = Math.max(0, chartData.klines.length - viewCount);
                            if (chartData.klines.length < viewCount) { viewOffset = 0; viewCount = chartData.klines.length; }
                            document.getElementById("stock-name").textContent = chartData.meta.name;
                            document.getElementById("stock-code").textContent = chartData.meta.symbol;
                            document.title = "缠论分析 - " + chartData.meta.name;
                            if (chartData.klines.length > 0) {
                                const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                                document.getElementById("goto-date-input").value = lastDate;
                            }
                            updateWeekday();
                        }
                        if (data.sub) {
                            dualSubData = data.sub;
                            dualSubViewCount = 377;
                            dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                            if (dualSubData.klines.length < dualSubViewCount) {
                                dualSubViewOffset = 0;
                            }
                        }
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        document.getElementById("goto-date-input").disabled = false;
                        updateFreqButtonStates(true);
                        render();
                    } catch (e) {
                        console.error('双窗口init解析失败:', e);
                        document.getElementById("goto-date-input").disabled = false;
                    }
                });

                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeDataDual(data);
                    } catch (e) {
                        console.error('双窗口update解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    // 立即关闭EventSource，阻止浏览器自带重连
                    realtimeEventSource.close();
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
                };

                realtimeEventSource.onopen = function() {
                    realtimeConnected = true;
                    badge.classList.remove('stopped');
                    badge.textContent = '● 实时';
                };
            } catch (e) {
                console.error('双窗口SSE连接失败:', e);
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
                document.getElementById("loading").classList.add("hidden");
            }
        }

        function connectRealtime(symbol, freq, startTime) {
            freq = freq || currentFreq || '1m';
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
                        handleRealtimeDataSingle(data);
                    } catch(e) {
                        console.error('实时数据解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    // 立即关闭EventSource，阻止浏览器自带重连
                    realtimeEventSource.close();
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
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
            realtimeConnected = false;
            const badge = document.getElementById('realtime-badge');
            badge.classList.remove('visible', 'stopped');
        }

        function handleRealtimeDataSingle(data) {
            if (!isRealtimeMode || !data || !data.klines) return;
            // 保存当前开关状态
            const savedShowBi = showBi, savedShowFx = showFx;
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

        function handleRealtimeDataDual(data) {
            if (!isRealtimeMode || !data) return;
            // 保存当前开关状态
            const savedShowBi = showBi, savedShowFx = showFx;
            const savedShowZs = showZs, savedShowSeg = showSeg, savedShowBsp = showBsp;

            if (data.main) {
                // 保存用户当前的缩放和位置
                const oldMainCount = chartData && chartData.klines ? chartData.klines.length : 0;
                const savedViewCount = viewCount;
                const savedViewOffset = viewOffset;
                const wasAtRightEdge = (savedViewOffset + savedViewCount >= oldMainCount);

                chartData = data.main;

                // 更新元信息
                if (data.main.meta) {
                    document.getElementById('stock-name').textContent = data.main.meta.name || '';
                    document.getElementById('stock-code').textContent = data.main.meta.symbol || '';
                    if (data.main.meta.freq) {
                        const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d'};
                        currentFreq = freqMap[data.main.meta.freq] || currentFreq;
                    }
                }

                // 保持用户缩放不变：如果在最右端，左减一右加一
                const newMainCount = data.main.klines ? data.main.klines.length : 0;
                const delta = newMainCount - oldMainCount;
                viewCount = savedViewCount;
                if (wasAtRightEdge && delta > 0) {
                    viewOffset = Math.max(0, savedViewOffset + delta);
                } else {
                    viewOffset = savedViewOffset;
                }
            }
            if (data.sub) {
                // 保存子窗口的缩放和位置
                const oldSubCount = dualSubData && dualSubData.klines ? dualSubData.klines.length : 0;
                const savedSubCount = dualSubViewCount || 377;
                const savedSubOffset = dualSubViewOffset || 0;
                const wasSubAtRightEdge = (savedSubOffset + savedSubCount >= oldSubCount);

                dualSubData = data.sub;

                const newSubCount = data.sub.klines ? data.sub.klines.length : 0;
                const subDelta = newSubCount - oldSubCount;
                dualSubViewCount = savedSubCount;
                if (wasSubAtRightEdge && subDelta > 0) {
                    dualSubViewOffset = Math.max(0, savedSubOffset + subDelta);
                } else {
                    dualSubViewOffset = savedSubOffset;
                }
            }
            updateSlider();
            resizeCanvas();
            render();
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
            const data = (isDualWindow && activeDualWindow === 'sub' && dualSubData) ? dualSubData : chartData;
            const vo = (isDualWindow && activeDualWindow === 'sub') ? dualSubViewOffset : viewOffset;
            const vc = (isDualWindow && activeDualWindow === 'sub') ? dualSubViewCount : viewCount;
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
            const winLabel = isDualWindow ? (activeDualWindow === 'sub' ? '[下窗] ' : '[上窗] ') : '';
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
                if (isDualWindow && activeDualWindow === 'sub' && dualSubData) {
                    return dualSubData;
                }
                return chartData;
            }
            // 获取当前激活窗口的 viewOffset
            function getActiveViewOffset() {
                if (isDualWindow && activeDualWindow === 'sub') {
                    return dualSubViewOffset;
                }
                return viewOffset;
            }
            // 设置当前激活窗口的 viewOffset
            function setActiveViewOffset(v) {
                if (isDualWindow && activeDualWindow === 'sub') {
                    dualSubViewOffset = v;
                } else {
                    viewOffset = v;
                }
            }
            // 获取当前激活窗口的 viewCount
            function getActiveViewCount() {
                if (isDualWindow && activeDualWindow === 'sub') {
                    return dualSubViewCount;
                }
                return viewCount;
            }
            // 设置当前激活窗口的 viewCount
            function setActiveViewCount(v) {
                if (isDualWindow && activeDualWindow === 'sub') {
                    dualSubViewCount = v;
                } else {
                    viewCount = v;
                }
            }
            // 渲染当前激活窗口
            function renderActive() {
                updateActiveWindowClass();
                if (isDualWindow && activeDualWindow === 'sub') {
                    // 直接渲染下面窗口，跳过 updateDualNewZs() 避免滑块操作时误清除红框新中枢
                    if (!dualSubData || !subCtx) return;
                    const _savedCanvas = canvas, _savedCtx = ctx;
                    canvas = subCanvas; ctx = subCtx;
                    window._isRenderingBottom = true;
                    _renderChart(dualSubData, dualSubFreq, dualSubViewOffset, dualSubViewCount,
                        dualSubMouseX, dualSubMouseY, dualHighlightRange, dualRedRange);
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
                    // 先本地移除，立即刷新界面
                    annotations = annotations.filter(function(a) {
                        return !(a.date === dateStr && a.text === text);
                    });
                    render();
                    // 再从服务器重新加载，确保与后端真实状态完全一致
                    loadAnnotations();
                } else {
                    // 后端未找到匹配标注：标注可能存在于其他周期下
                    // 重新加载以同步本地状态，并提示用户
                    console.warn("[标注] 后端未找到匹配标注(code=" + code + ", freq=" + freq + ")，重新加载标注数据");
                    loadAnnotations();
                    alert("未找到该标注，可能标注存在于其他周期下。\n当前周期: " + freq + "\n请切换到添加标注时使用的周期再试。");
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
            const menuRestart = document.getElementById("annotation-menu-restart");
            const menuReplay = document.getElementById("annotation-menu-replay");
            const menuDivider = document.getElementById("annotation-menu-divider");
            const menuDelAll = document.getElementById("annotation-menu-del-all");
            const menuDivider2 = document.getElementById("annotation-menu-divider2");
            const menuMirror = document.getElementById("annotation-menu-mirror");
            // 更新反转视图菜单项文字（显示当前状态）
            menuMirror.textContent = _isMirrorMode ? "取消反转" : "反转视图";
            if (_annotationClickTarget) {
                menuDeleteOne.style.display = "block";
                menuEditOne.style.display = "block";
                menuAdd.style.display = "none";
                menuRestart.style.display = "none";
                menuReplay.style.display = "none";
                menuDivider.style.display = "none";
                menuDelAll.style.display = "none";
            } else {
                menuDeleteOne.style.display = "none";
                menuEditOne.style.display = "none";
                menuAdd.style.display = "block";
                menuRestart.style.display = _restartEnabled ? "block" : "none";
                menuReplay.style.display = "block";
                menuDivider.style.display = "block";
                menuDelAll.style.display = "block";
            }
            // 反转视图始终显示（与标注操作无关，是全局视图模式）
            menuDivider2.style.display = "block";
            menuMirror.style.display = "block";

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

        // 复盘到右键点击的K线日期（等价于在复盘日期输入框中输入该日期）
        window.annotationReplayToHere = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!_annotationTargetDate) return;
            var input = document.getElementById("goto-date-input");
            var dateStr;
            if (isIntradayFreq(currentFreq)) {
                // 日内周期：保留完整时间，转成 datetime-local 格式
                dateStr = klineDateToInput(_annotationTargetDate, currentFreq);
            } else {
                // 日K/周K：只取日期部分
                dateStr = _annotationTargetDate.slice(0, 10).replace(/\//g, "-");
                if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
                    alert("无法识别该K线日期: " + _annotationTargetDate);
                    return;
                }
            }
            // 若与当前日期相同，仍强制重新复盘（避免用户改了其他条件后无响应）
            input.value = dateStr;
            if (typeof updateWeekday === "function") updateWeekday();
            _dateKeyEnter = true;  // 复用keyEnter标志，gotoDate中跳过isToday安全网，始终传end_date
            gotoDate();
        };

        // 切换反转视图模式（K线涨跌互换、MACD红绿互换、缠论结构镜像）
        // 保底策略：如果反图渲染出错，自动切回正图并从后端重新加载，确保正图永远正确
        window.toggleMirrorMode = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            var prevMode = _isMirrorMode;
            _isMirrorMode = !_isMirrorMode;
            try {
                render();
            } catch(e) {
                console.error("[反转视图] 渲染出错，自动恢复正图:", e);
                _isMirrorMode = false;
                // chartData 可能被镜像数据污染（_renderChart 中途异常未恢复），
                // 从后端重新加载（命中缓存仅 0.001s），彻底恢复正图
                try {
                    loadStock();
                } catch(e2) {
                    console.error("[反转视图] 恢复失败:", e2);
                }
            }
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
            const font = "bold 12px 'PingFang SC', 'Microsoft YaHei', sans-serif";
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
                ctx.font = "bold 12px 'PingFang SC', 'Microsoft YaHei', sans-serif";
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
            ctx.textBaseline = "alphabetic"; // 恢复默认基线
        }

        init();

        // 关闭/刷新页面时保存状态（仅股票，期货不保存）
        window.addEventListener('beforeunload', function() { saveLastState(); });
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
    
    # 1. 加载上次查看的代码和周期（持久化恢复），若不存在则使用默认值
    last_code, last_freq = _load_last_code_freq()
    if last_code:
        start_code = last_code
        start_freq = last_freq
        print(f"[信息] 恢复上次: {last_code} (周期: {last_freq})")
    else:
        start_code = SYMBOL_CODE
        start_freq = "d"
        print(f"[信息] 使用默认股票: {start_code}")

    result = analyze_stock(start_code, freq=start_freq)
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
    print(f"[信息] HTML页面已生成: {html_path} ({html_size/1024/1024:.1f}MB)")

    # 3. 启动HTTP服务器
    port = 18081  # 使用18081，避免与czsc版本的18080冲突
    server = ThreadingHTTPServer(("127.0.0.1", port), ChartHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=False)
    server_thread.start()
    url = f"http://127.0.0.1:{port}/chan_chart.html"
    print(f"[信息] HTTP服务器已启动: {url}\n")

    try:
        server_thread.join()
    except KeyboardInterrupt:
        server.shutdown()
        print("\n[信息] 服务器已停止")

    print("=" * 60)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

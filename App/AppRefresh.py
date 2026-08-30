# -*- coding: utf-8 -*-
"""
App/AppRefresh.py —— 刷新功能域
=========================================================================
点击页面右上角「刷新」按钮后的操作，刷新股票名、指数归属、PE-TTM、
板块文件等。

本模块收纳：
  - 股票名称刷新（refresh_stock_names / refresh_stock_names_async / refresh_status）
  - 名称 / PE / 流通市值 / 指数归属 缓存读写（AppData 直连）
  - 刷新实现（_refresh_stock_names / _refresh_pe_ttm /
      _fetch_index_belong_from_akshare / _collect_codes_from_vipdoc /
      _fetch_names_from_sina_once / _fetch_float_mc_from_tencent 等）

依赖方向：AppRefresh.py → AppConfig / AppData / DataAPI（单向）
"""
import json
import os
import threading
import time
import traceback

from App.AppConfig import app_config
from App.AppData import app_data
from App.AppLog import get_logger
from DataAPI.TdxAPI import collect_codes_from_vipdoc, refresh_block_files

log = get_logger(__name__)


# 股票名称/PE/市值缓存状态：别名 = app_data 实例字段（共享同一对象）
# key: 股票代码(6位), value: {"name": "股票名称", "pinyin": "拼音首字母"}
_stock_names_cache = app_data.names_cache

# PE-TTM 缓存（腾讯接口，增量刷新；共享对象）
_pe_ttm_cache = app_data.pe_cache        # {market+code: float}  PE-TTM值

# 指数归属缓存（AKShare在线获取，与PE-TTM一起保存到stock_pettm_index.json）
# key: market+code（如 "sh600519"）, value: "沪深300"|"中证500"|"中证1000"
_index_belong_cache = app_data.belong_cache

# 刷新状态（股票名称刷新用；获取侧状态）
# 访问者：刷新工作线程（写）+ /api/stocks/refresh/read|POST 的 REST 线程
# （读）。running 的「检查后置位」必须原子，否则两个并发 POST 会同时通过
# 检查起两条刷新线程 —— 故配 _refresh_state_lock 做 CAS。
_refresh_status = {"running": False, "progress": 0, "total": 0, "loaded": 0, "error": None, "step": ""}
_refresh_state_lock = threading.Lock()

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


# ═══════════════════════════════════════════════════════════════════════
# 名称 / PE / 流通市值 / 指数归属 缓存
# ═══════════════════════════════════════════════════════════════════════

def load_stock_names_from_cache_file():
    """加载股票名称缓存（AppData 直连）"""
    return app_data.load_stock_names_from_cache_file()


def load_float_mc_cache():
    """加载流通市值缓存（AppData 直连）"""
    return app_data.load_float_mc_cache()


def update_float_mc_cache(mv_dict):
    """更新流通市值缓存（AppData 直连）"""
    return app_data.update_float_mc_cache(mv_dict)


def load_pe_ttm_cache():
    """加载 PE-TTM 缓存（AppData 直连）"""
    return app_data.load_pe_ttm_cache()


def get_pe_ttm(market, code):
    """获取 PE-TTM（AppData 直连）"""
    return app_data.get_pe_ttm(market, code)


def get_index_belong(market, code):
    """获取指数归属（AppData 直连）"""
    return app_data.get_index_belong(market, code)


# ═══════════════════════════════════════════════════════════════════════
# 刷新实现
# ═══════════════════════════════════════════════════════════════════════

def _safe_write_json_file(path, data, *, ensure_ascii=False, indent=None):
    """先写临时文件并校验 JSON 可读，再原子覆盖（委托 app_data）"""
    from App.AppData import safe_write_json_file
    return safe_write_json_file(path, data, ensure_ascii=ensure_ascii, indent=indent)


def _collect_codes_from_vipdoc(vipdoc_dir):
    """委托 DataAPI/TdxAPI（vipdoc_dir 由调用方注入）"""
    return collect_codes_from_vipdoc(vipdoc_dir)


def _fetch_names_from_sina_once(codes_dict):
    """
    一次性从新浪财经API获取股票名称，用于首次建立缓存。
    参数 codes_dict: {code: {"name": "", ...}} —— 只获取 name 为空的条目。
    返回补充了多少条名称。
    注意：新浪API不支持A股和港股混合请求，必须分开调用。
    """
    import urllib.request

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
                log.info(f"[股名刷新]   新浪A股批次{batch_num}失败: {e}")
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
                log.info(f"[股名刷新]   腾讯港股批次{batch_num}失败: {e}")
            if batch_num < total_batches:
                time.sleep(0.5)

    return filled


def _fetch_index_belong_from_akshare(timeout=30):
    """
    通过 AKShare index_stock_cons_csindex 接口在线获取沪深300/中证500/中证1000 最新成分股，
    构建 stock→指数归属 反向映射。返回 {market+code: "沪深300"|"中证500"|"中证1000"}。
    如果 AKShare 不可用或网络异常，返回空字典。每个指数单独设置超时。
    （归属缓存由 app_data 持有，经 replace_index_belong 同对象替换）
    """
    try:
        import akshare as ak
    except ImportError:
        log.info("[指数归属] akshare 未安装，跳过在线获取（pip install akshare）")
        return app_data.belong_cache

    def _fetch_one(_idx_code, _idx_name):
        try:
            _refresh_status["step"] = f"刷新指数归属: {_idx_name}..."
            log.info(f"[指数归属] 开始获取 {_idx_name}({_idx_code})...")
            df = ak.index_stock_cons_csindex(symbol=_idx_code)
            count = 0
            for _, row in df.iterrows():
                stock_code = str(row["成分券代码"]).zfill(6)
                exchange = str(row["交易所"])
                mkt = _AKSHARE_EXCHANGE_MAP.get(exchange, "")
                if mkt and stock_code.isdigit() and len(stock_code) == 6:
                    result[mkt + stock_code] = _idx_name
                    count += 1
            log.info(f"[指数归属] {_idx_name}({_idx_code}): 已成功获取 {count}只 成分股")
        except Exception as e:
            log.info(f"[指数归属] {_idx_name}({_idx_code}) 获取失败: {e}")

    import concurrent.futures
    result = {}
    for index_code, index_name in _AKSHARE_INDEX_MAP.items():
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_fetch_one, index_code, index_name)
            try:
                future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                log.info(f"[指数归属] {index_name}({index_code}) 获取超时({timeout}s)，跳过")
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
    load_pe_ttm_cache()  # 先加载已有缓存

    # 从 stock_names.json 收集所有纯数字股票代码（路径：AppConfig 派生属性）
    if not os.path.exists(app_config.stock_names_cache_file):
        log.info("[PE-TTM] stock_names.json 不存在，无法刷新")
        _refresh_status["error"] = "stock_names.json 不存在，请先刷新股票名称"
        return

    try:
        with open(app_config.stock_names_cache_file, "r", encoding="utf-8") as f:
            names_data = json.load(f)
    except Exception as e:
        log.info(f"[PE-TTM] 读取 stock_names.json 失败: {e}")
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
    log.info(f"[PE-TTM] 开始刷新 {total} 只股票的 PE-TTM...")

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
            log.info(f"[PE-TTM] 第{i//batch_size+1}批失败: {e}")

        _refresh_status["loaded"] = min(i + batch_size, total)
        log.info(f"[PE-TTM] 进度: {_refresh_status['loaded']}/{total}, 新增/更新 {new_count} 条")

    # 统计未获取到的股票
    all_queried = {mkt + code for mkt, code in codes}
    missed = all_queried - got_set
    if missed:
        missed_list = sorted(missed)[:20]
        log.info(f"[PE-TTM] 未获取到PE-TTM: {len(missed)} 只 (如: {', '.join(missed_list)}{'...' if len(missed) > 20 else ''})")

    # 刷新指数归属（AKShare在线获取，与PE-TTM一起保存）
    _refresh_status["step"] = "刷新指数归属..."
    log.info("[指数归属] ========== 开始刷新指数归属 ==========")
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
        log.info(f"刷新完成: 共 {len(combined)} 条 (PE-TTM: {sum(1 for v in combined.values() if 'pe_ttm' in v)} 条, 指数归属: {sum(1 for v in combined.values() if 'index' in v)} 条), 已保存到 {app_config.stock_pe_ttm_file}")
    except Exception as e:
        log.info(f"[PE-TTM] 保存失败: {e}")
        _refresh_status["error"] = f"保存 PE-TTM 失败: {e}"


def _reset_refresh_running(fn):
    """装饰器：统一管理 running 旗（守卫 + 置位 + try/finally 复位）。

    原实现 running=True 只在函数末尾（617）复位，中途异常会卡死运行旗，
    后续所有刷新请求直接返回 already_running；后改为无条件 finally 复位，
    又破坏了重入守卫——撞守卫的调用（running 为 True 时的早退）也会把旗标
    误抹掉、允许第二个刷新并发进入。现收敛为：守卫只判不写（running 时
    直接返回，不进 finally 复位）；真正进入才置位，无论正常完成还是异常
    逃逸都复位。三种路径（守卫早退/正常/异常）互不串扰。
    """
    import functools

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        # 检查 + 置位在同一把锁内（CAS）：并发调用只有一个能进入
        with _refresh_state_lock:
            if _refresh_status["running"]:
                return                  # 撞守卫：直接返回，不触碰 flag
            _refresh_status["running"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            with _refresh_state_lock:
                _refresh_status["running"] = False
    return _wrapper


@_reset_refresh_running
def _refresh_stock_names():
    """
    从本地文件批量获取全市场股票名称，保存到 stock_names.json。
    数据来源优先级：
      1. vipdoc/*.day 文件名（收集所有已下载过数据的股票代码）
      2. 新浪财经API（为无名称的代码批量查询名称）
    （名称缓存由 app_data 持有；本函数只做获取与合并，
     最终经 replace_names 同对象替换，_stock_names_cache 别名全程可见）
    """
    # running 守卫与置位由装饰器 _reset_refresh_running 统一管理，此处不再重复
    _refresh_status["step"] = "刷新股票名..."
    _refresh_status["error"] = None
    log.info("[股名刷新] ========== 开始刷新股票名称 ==========")

    # === 先加载已有缓存，新数据合并进去，不覆盖 ===
    raw_names = {}
    load_stock_names_from_cache_file()
    if _stock_names_cache:
        for code, info in _stock_names_cache.items():
            if isinstance(info, dict):
                raw_names[code] = info
            else:
                raw_names[code] = {"name": info, "pinyin": ""}
        log.info(f"[股名刷新] 步骤1/5 加载缓存: 已加载 {len(raw_names)} 只")
    else:
        log.info("[股名刷新] 步骤1/5 加载缓存: 无缓存，全新读取")

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
    log.info(f"[股名刷新] 步骤2/5 合并扫描: vipdoc共{v_total}只 (sh{v_sh}+sz{v_sz}+ds{v_hk}), 缓存{cache_before}只, 合并后{len(raw_names)}只 (新增{vipdoc_new}只)")

    # === 方案2: 新浪API补全缺失的名称 ===
    # 即使已有缓存，如果有新发现的代码（如港股）没有名称，也要补全
    codes_without_name = [c for c, info in raw_names.items() if not info.get("name")]
    if codes_without_name:
        a_no = sum(1 for c in codes_without_name if raw_names[c].get("market") != "hk")
        hk_no = sum(1 for c in codes_without_name if raw_names[c].get("market") == "hk")
        log.info(f"[股名刷新] 步骤3/5 补全名称: {len(codes_without_name)} 只无名称 (A股{a_no}, 港股{hk_no})")
        temp_dict = {c: raw_names[c] for c in codes_without_name}
        filled = _fetch_names_from_sina_once(temp_dict)
        for code, info in temp_dict.items():
            if info.get("name"):
                raw_names[code] = info
        failed = len(codes_without_name) - filled
        if failed > 0:
            log.info(f"[股名刷新] 步骤3/5 补全名称: 成功 {filled} 只, 失败 {failed} 只")
        else:
            log.info(f"[股名刷新] 步骤3/5 补全名称: 全部成功 {filled} 只")
    else:
        log.info("[股名刷新] 步骤3/5 补全名称: 无需补全")

    # === 补充通达信板块指数名称（88xxxx系列，如880491半导体、881319半导体）===
    # 88xxxx代码不以标准A股格式开头，_is_a_stock_code() 会过滤掉，所以不在 raw_names 中。
    # 来源: tdxzs.cfg（通达信配置文件）和 AppData 内嵌映射表（本地映射）
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
            log.info(f"[股名刷新]   读取tdxzs.cfg失败: {e}")

    # 研究行业(881xxx)从 AppData 内嵌映射表读取（单一加载函数 app_data.load_tdxhy_mapping）
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
        log.info(f"[股名刷新]   加载行业映射失败: {e}")

    block_filled = tdxzs_filled + tdxhy_filled
    log.info(f"[股名刷新] 步骤4/5 补充板块: tdxzs.cfg +{tdxzs_filled}条, tdxhy +{tdxhy_filled}条, 共补全 {block_filled} 条板块")

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
            log.info(f"[股名刷新] 步骤5/5 过滤保存: 过滤 {filtered_count} 只 ({', '.join(parts)}), 最终 {len(all_names)} 只 (上海{sh_count}, 深圳{sz_count}, 港股{hk_count})")
        else:
            log.info(f"[股名刷新] 步骤5/5 过滤保存: 最终 {len(all_names)} 只 (上海{sh_count}, 深圳{sz_count}, 港股{hk_count})")
        log.info(f"[股名刷新] 刷新完成: 共 {len(all_names)} 只股票名称, 已保存到 {app_config.stock_names_cache_file}")
    else:
        log.info("[股名刷新] 步骤5/5 过滤保存: 失败，未获取到任何数据")

    # 刷新板块文件（block_zs.dat / block_gn.dat / block_fg.dat / block.dat）
    log.info("[板块刷新] ========== 开始刷新板块文件 ==========")
    _refresh_status["step"] = "刷新成分股..."
    try:
        def _set_step(msg):
            _refresh_status["step"] = msg
        refresh_block_files(progress_callback=_set_step)
    except Exception as e:
        log.info(f"[板块刷新] 板块文件刷新失败: {e}")

    # 刷新 PE-TTM（增量更新 stock_pettm_index.json）
    log.info("[PE-TTM] ========== 开始刷新PE-TTM ==========")
    try:
        _refresh_pe_ttm()
    except Exception as e:
        log.info(f"[PE-TTM] PE-TTM 刷新失败: {e}")
        _refresh_status["error"] = f"PE-TTM 刷新失败: {e}"

    # 全部刷新完成，标记状态
    _refresh_status["running"] = False
    _refresh_status["step"] = ""


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
            log.info(f"[流通市值] 腾讯接口第{i//batch_size+1}批失败: {type(e).__name__}: {e}")
    return all_mv


# ═══════════════════════════════════════════════════════════════════════
# 股票名称刷新（异步）
# ═══════════════════════════════════════════════════════════════════════

def refresh_status():
    """股票名称刷新状态（快照副本，避免调用方拿到可变的内部字典）"""
    with _refresh_state_lock:
        return dict(_refresh_status)


def refresh_stock_names():
    """刷新股票名称（阻塞）"""
    return _refresh_stock_names()


def refresh_stock_names_async():
    """异步启动股票名称刷新（不阻塞请求线程）

    仅做「快速预检查」：running 已为 True 时直接返回 already_running，避免再起一条
    注定被守卫拦截的线程。真正的 CAS（检查 + 置位）由装饰器 _reset_refresh_running
    包裹的 _refresh_stock_names 在子线程内完成，finally 中无条件复位，保证 running
    不会卡死。注意：此处【不得】提前把 running 旗置为 True —— 否则子线程进入被装饰函数
    时守卫已见 running=True 而早退，刷新正文永不执行且 running 永久卡死。
    """
    with _refresh_state_lock:
        if _refresh_status["running"]:
            return {"status": "already_running", **_refresh_status}

    def _do_refresh():
        try:
            _refresh_stock_names()
        except Exception as e:
            traceback.print_exc()
            log.error(f"[错误] refresh_stock_names异常: {e}")

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()
    return {"status": "started", "msg": "股票名称刷新已启动"}


def fetch_float_mc_from_tencent(stock_list):
    """从腾讯接口获取流通市值（获取侧）"""
    return _fetch_float_mc_from_tencent(stock_list)

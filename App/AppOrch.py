# -*- coding: utf-8 -*-
"""
App/AppOrch.py —— 业务编排层（服务层）
=========================================================================
按职责划分为三个分区（见设计文档 6.1 节）：
  - 消费侧（分析引擎）：analyze_stock / call_analysis / run_analysis
  - 获取侧（数据拉取）：fetch_and_inject、盘后下载、股票名称刷新
  - 扫描（批量扫描）：ScannerService（状态收敛到类内部）

合并说明（阶段 2 双版本合并）：
  - 底座采用第三方版：引擎全局串行锁 _ENGINE_LOCK + call_analysis（同步持锁）
    + run_analysis（线程池执行 + 持锁，不阻塞事件循环）
  - 吸收本版接口面：40+ 函数全量锁定（分析/搜索/缓存/同花顺/下载/期货/扫描）
    + ScannerService 状态收敛 + 领域异常层级

依赖方向（设计文档 6.2 节）：
  FrontAPI.py → App/AppOrch.py → App/AppData.py（单向，禁止反向）

使用方式：
    from App.AppOrch import analyze_stock, ScannerService, call_analysis
    result = call_analysis("000001.SH", freq="d")
"""
import os
import time
import threading
import traceback

# my_chan_main 作为底层引擎（阶段 3 起逐步拆分吸收）
import my_chan_main as _m


# ═══════════════════════════════════════════════════════════════════════
# 引擎全局串行锁（第三方底座）
# 引擎全局缓存（_stocks_analysis_cache 等）非线程安全 → 全局串行锁保护；
# 阶段 2 仅 call_analysis/run_analysis 持锁，阶段 3a 其余路由迁入后全覆盖。
# ═══════════════════════════════════════════════════════════════════════
_ENGINE_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════
# 领域异常层级（见设计文档 7.7 节）
# 服务层只抛领域异常，API 层通过统一中间件捕获。
# ═══════════════════════════════════════════════════════════════════════

class AppError(Exception):
    """领域异常基类 · status_code 默认 500"""
    status_code = 500


class DataFetchError(AppError):
    """数据源获取失败 · 502"""
    status_code = 502


class AnalysisError(AppError):
    """缠论分析失败 · 500"""
    status_code = 500


class ConfigError(AppError):
    """配置错误 · 500"""
    status_code = 500


class NotFoundError(AppError):
    """股票 / 期货不存在 · 404"""
    status_code = 404


class PersistenceError(AppError):
    """持久化失败 · 503"""
    status_code = 503


# ═══════════════════════════════════════════════════════════════════════
# 消费侧：分析引擎
# ═══════════════════════════════════════════════════════════════════════

def call_analysis(code, freq="d", end_date=None, dual=False, step=None, sub_freq=None):
    """单标的缠论分析（同步入口，REST 路由当前直接调用）

    - 引擎全局缓存非线程安全 → 全局串行锁保护
    - 开始/完成即时打印：uvicorn 仅在请求完成后记日志，挂起时控制台零输出，
      此日志是排障第一现场（阶段 1 Hotfix 教训）。
    """
    print(f"[api] /api/stock 开始分析: code={code!r} freq={freq!r} "
          f"end_date={end_date!r} dual={dual}", flush=True)
    t0 = time.time()
    with _ENGINE_LOCK:
        result = _m.analyze_stock(code, freq=freq, end_date=end_date,
                                  dual=dual, step=step, sub_freq=sub_freq)
    print(f"[api] /api/stock 完成: code={code!r} 耗时 {time.time() - t0:.2f}s", flush=True)
    return result


async def run_analysis(code, freq="d", end_date=None, dual=False, step=None, sub_freq=None):
    """单标的缠论分析（异步入口，阶段 3a SSE/REST 统一走此通道）

    线程池执行 + 串行锁：不阻塞事件循环（静态资源/健康检查保持可响应），
    同时保证同一时刻只有一个线程进入引擎。
    """
    import asyncio
    loop = asyncio.get_event_loop()
    print(f"[api] run_analysis 开始: code={code!r} freq={freq!r} "
          f"end_date={end_date!r} dual={dual}", flush=True)
    t0 = time.time()

    def _job():
        with _ENGINE_LOCK:
            return _m.analyze_stock(code, freq=freq, end_date=end_date,
                                    dual=dual, step=step, sub_freq=sub_freq)

    try:
        return await loop.run_in_executor(None, _job)
    finally:
        print(f"[api] run_analysis 完成: code={code!r} 耗时 {time.time() - t0:.2f}s", flush=True)


def analyze_stock(code, freq="d", end_date=None, cache_chan=True, dual=False, step=None, sub_freq=None):
    """统一的缠论分析入口（无状态，可在线程池 / ProcessPool 复用）"""
    return _m.analyze_stock(code, freq=freq, end_date=end_date,
                            cache_chan=cache_chan, dual=dual, step=step, sub_freq=sub_freq)


def stock_manual_select_point(code, freq="d", bi_idx=-1):
    """股票手动选点"""
    return _m.stock_manual_select_point(code, freq=freq, bi_idx=bi_idx)


def futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """期货手动选点"""
    return _m.futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)


def compute_red_range_zs(code, sub_freq="d", left_date="", right_date="", end_date=None):
    """红框中枢计算"""
    return _m.compute_red_range_zs(code, sub_freq=sub_freq,
                                   left_date=left_date, right_date=right_date,
                                   end_date=end_date)


def extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None):
    """从实时行情快照中提取分析所需字段（供 SSE 路径使用）"""
    return _m._extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                         saved_selection_date, lightweight, klines)


# ═══════════════════════════════════════════════════════════════════════
# 获取侧：数据拉取与注入
# ═══════════════════════════════════════════════════════════════════════

def fetch_and_inject(code, freq="d", source="tdx", end_date=None, dual=False, step=None, sub_freq=None):
    """
    判断股票 / 期货 → 拉取 K 线 → 注入分析引擎。
    第一版委托 analyze_stock（其内部完成数据拉取 + 分析），
    阶段 5 后统一走 DataAPI 抽象层。
    """
    return _m.analyze_stock(code, freq=freq, end_date=end_date,
                            dual=dual, step=step, sub_freq=sub_freq)


# ═══════════════════════════════════════════════════════════════════════
# 搜索
# ═══════════════════════════════════════════════════════════════════════

def search_stocks(q):
    """股票代码 / 名称 / 拼音搜索（委托 my_chan_main 的缓存与别名）"""
    _m._load_stock_names_from_cache_file()
    if not os.path.exists(_m._STOCK_NAMES_CACHE_FILE):
        return {"need_refresh": True, "msg": "请先刷新股票名缓存"}

    keyword_upper = q.upper()
    exact_results = []
    exact_pinyin_results = []
    prefix_results = []
    other_results = []

    # 手工补充扩展市场指数
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
        elif pinyin == keyword_upper or name.upper() == keyword_upper:
            exact_pinyin_results.append(item)
        elif bare_code.startswith(keyword_upper):
            prefix_results.append(item)
        else:
            other_results.append(item)

    for compound_key, info in _m._stock_names_cache.items():
        if isinstance(info, dict):
            name = info.get("name", "")
            pinyin = info.get("pinyin", "")
            market = info.get("market", "")
        else:
            name = info
            pinyin = ""
            market = ""
        name = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in name)
        pinyin = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in pinyin)
        if not name:
            continue

        if market and compound_key.startswith(market):
            bare_code = compound_key[len(market):]
        else:
            bare_code = compound_key

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

    # 期货/期指别名搜索
    for alias, full_code in _m.FUTURES_ALIASES.items():
        if keyword_upper in alias.upper():
            name = _m._get_futures_name(full_code) if _m._get_futures_name else alias
            if not any(r["code"] == full_code for r in results):
                results.append({
                    "code": full_code, "name": name, "pinyin": alias,
                    "market": "futures", "type": "",
                })

    return {"results": results}


# ═══════════════════════════════════════════════════════════════════════
# 名称 / PE / 流通市值 缓存
# ═══════════════════════════════════════════════════════════════════════

def load_stock_names_from_cache_file():
    """加载股票名称缓存"""
    return _m._load_stock_names_from_cache_file()


def refresh_stock_names():
    """刷新股票名称（阻塞）"""
    return _m._refresh_stock_names()


def load_float_mc_cache():
    """加载流通市值缓存"""
    return _m._load_float_mc_cache()


def fetch_float_mc_from_tencent(stock_list):
    """从腾讯接口获取流通市值"""
    return _m._fetch_float_mc_from_tencent(stock_list)


def update_float_mc_cache(mv_dict):
    """更新流通市值缓存"""
    return _m._update_float_mc_cache(mv_dict)


def load_pe_ttm_cache():
    """加载 PE-TTM 缓存"""
    return _m._load_pe_ttm_cache()


def get_pe_ttm(market, code):
    """获取 PE-TTM"""
    return _m._get_pe_ttm(market, code)


def get_index_belong(market, code):
    """获取指数归属"""
    return _m._get_index_belong(market, code)


# ═══════════════════════════════════════════════════════════════════════
# 同花顺云端自选股
# ═══════════════════════════════════════════════════════════════════════

def ths_cloud_available():
    """同花顺云端 API 是否可用"""
    return _m._THS_CLOUD_AVAILABLE


def save_scan_to_ths_cloud(codes):
    """保存扫描结果到同花顺云端自选股"""
    if not _m._THS_CLOUD_AVAILABLE or _m.save_scan_to_ths_cloud is None:
        return {"error": "ths_cloud_api.py 未找到，请确保该文件在 App/ 目录"}
    return _m.save_scan_to_ths_cloud(codes)


# ═══════════════════════════════════════════════════════════════════════
# 股票名称刷新（异步）
# ═══════════════════════════════════════════════════════════════════════

def refresh_status():
    """股票名称刷新状态"""
    return _m._refresh_status


def refresh_stock_names_async():
    """异步启动股票名称刷新（不阻塞请求线程）"""
    if _m._refresh_status["running"]:
        return {"status": "already_running", **_m._refresh_status}

    def _do_refresh():
        try:
            _m._refresh_stock_names()
        except Exception as e:
            traceback.print_exc()
            print(f"[错误] refresh_stock_names异常: {e}")

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()
    return {"status": "started", "msg": "股票名称刷新已启动"}


# ═══════════════════════════════════════════════════════════════════════
# 盘后下载（页面右上角「盘后下载」按钮）
# 阶段 5 起收纳进 DataAPI/ElTdxAPI.py，此处先薄封装。
# ═══════════════════════════════════════════════════════════════════════

def eltdx_available():
    """eltdx 盘后下载引擎是否可用"""
    return _m._ELTDX_AVAILABLE


def download_dir():
    """盘后下载数据保存目录"""
    return _m.DOWNLOAD_DIR


def start_download(categories, day_start=None, min_start=None):
    """启动盘后下载"""
    return _m._start_download(_m.DOWNLOAD_DIR, categories,
                              day_start=day_start or None,
                              min_start=min_start or None)


def get_download_status():
    """盘后下载进度"""
    return _m._get_download_status()


def stop_download():
    """停止盘后下载"""
    return _m._stop_download()


# ═══════════════════════════════════════════════════════════════════════
# 期货
# ═══════════════════════════════════════════════════════════════════════

def futures_cleanup():
    """清理所有期货数据"""
    return _m._cleanup_all_futures_data()


def get_futures_aliases():
    """期货别名映射"""
    return _m.FUTURES_ALIASES


def get_futures_name(full_code):
    """期货名称"""
    if _m._get_futures_name:
        return _m._get_futures_name(full_code)
    return full_code


def tq_available():
    """天勤数据源是否可用"""
    return _m.TQ_AVAILABLE


def futures_config():
    """期货可用周期列表"""
    try:
        from DataAPI.TqSdkAPI import SUPPORTED_FREQS, DISABLED_FREQS
        return {"supported_freqs": SUPPORTED_FREQS, "disabled_freqs": DISABLED_FREQS}
    except ImportError:
        return {"supported_freqs": [], "disabled_freqs": []}


def get_sse_handler(kind):
    """返回 ChartHandler 的 SSE 处理方法（供 SSE Mock 桥接使用）

    kind: "dual" → _handle_sse_stream_dual；"single" → _handle_sse_stream_single
    阶段 3 起改写为原生异步生成器后，本接口随之移除。
    """
    handler = getattr(_m.ChartHandler, f"_handle_sse_stream_{kind}", None)
    if handler is None:
        raise ConfigError(f"未知 SSE 处理类型: {kind}")
    return handler


def get_stock_names_cache_file():
    """股票名称缓存文件路径"""
    return _m._STOCK_NAMES_CACHE_FILE


# ═══════════════════════════════════════════════════════════════════════
# 辅助：代码解析
# ═══════════════════════════════════════════════════════════════════════

def get_stock_market_code(code):
    """解析股票代码 → (market, bare_code)"""
    return _m._get_stock_market_code(code)


def get_market_code(code):
    """解析市场代码"""
    return _m._get_market_code(code)


def get_stock_name(market, code):
    """获取股票名称"""
    return _m._get_stock_name(market, code)


# ═══════════════════════════════════════════════════════════════════════
# 扫描：批量扫描服务（状态收敛到类内部）
# ═══════════════════════════════════════════════════════════════════════

class ScannerService:
    """批量扫描服务：遍历代码列表、逐票调用 analyze_stock、汇总结果、追踪进度。

    第一版为薄封装：委托 my_chan_main 的扫描函数与全局状态，
    对外通过本类方法访问，避免路由层直接操作模块级状态。
    """

    # ── 状态访问器（收敛到类内部，见设计文档 6.3 节）────────────────
    @property
    def aborted(self):
        return _m._scan_aborted

    @aborted.setter
    def aborted(self, value):
        _m._scan_aborted = value

    @property
    def skip_log(self):
        return _m._scan_skip_log

    @property
    def start_time(self):
        return _m._scan_start_time

    @start_time.setter
    def start_time(self, value):
        _m._scan_start_time = value

    @property
    def page_index_code(self):
        return _m._page_index_code

    @page_index_code.setter
    def page_index_code(self, value):
        _m._page_index_code = value

    @property
    def lock(self):
        return _m._scan_lock

    # ── 股票列表 ─────────────────────────────────────────────────────
    def stock_list(self, source="zxg"):
        """返回股票列表（支持逗号分隔多来源）"""
        sources = [s.strip() for s in source.split(",") if s.strip()]

        _SOURCE_READERS = {
            "zxg": (_m.read_zxg_stocks, "自选股"),
            "page_index": (lambda: _m._debug_read_page_index_stocks(_m._page_index_code), "成分股"),
            "tdxhy2": (_m.read_tdxhy_l2_indices, "板块指数2"),
            "tdxhy3": (_m.read_tdxhy_l3_indices, "板块指数3"),
        }

        src_stocks = {}
        errors = []
        for src in sources:
            reader = _SOURCE_READERS.get(src)
            if reader is None:
                errors.append(f"未知来源: {src}")
                continue
            read_fn, _ = reader
            src_stocks[src] = read_fn()

        # 合并去重
        merged = []
        seen = {}
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
                        merged[exist_idx]["_source"] = src

        # 批量获取流通市值
        _need_float_mc = any(s not in ("tdxhy2", "tdxhy3") for s in sources)
        if _need_float_mc:
            _m._load_float_mc_cache()
            try:
                t_mc = time.time()
                mv_dict = _m._fetch_float_mc_from_tencent(merged)
                if mv_dict:
                    _m._update_float_mc_cache(mv_dict)
            except Exception as e:
                print(f"[流通市值] 腾讯接口异常: {type(e).__name__}: {e}，使用本地缓存")

        # 后端预过滤
        pre_filtered = merged
        pre_skip_count = 0
        try:
            _PFX_MAP = {"0": "sz", "1": "sh", "2": "bj"}
            filtered = []
            for stk in merged:
                src = stk.get("_source", "zxg")
                if src in ("zxg", "tdxhy2", "tdxhy3"):
                    filtered.append(stk)
                    continue
                code = stk.get("code", "")
                prefix = stk.get("prefix", "")
                market = _PFX_MAP.get(prefix, "")
                if not market or not code:
                    filtered.append(stk)
                    continue
                pass_ok, pre_mc, skip_reason = _m._quick_prefilter_pass(market, code)
                if not pass_ok:
                    pre_skip_count += 1
                else:
                    filtered.append(stk)
            pre_filtered = filtered
        except Exception as e:
            print(f"[预过滤] 批量预过滤异常: {type(e).__name__}: {e}")

        return {
            "stocks": pre_filtered,
            "sources": sources,
            "total": len(pre_filtered),
            "pre_skipped": pre_skip_count,
            "errors": errors if errors else None,
        }

    # ── 单只扫描 ─────────────────────────────────────────────────────
    def scan_one(self, code, freq="d", prefix="", recent="1", source="zxg", mode=""):
        """扫描单只股票"""
        t_scan_start = time.time()
        try:
            recent_days = max(1, int(recent))
        except ValueError:
            recent_days = 1

        if not code:
            return {"error": "缺少code参数"}

        try:
            t0 = time.time()
            _PREFIX_MAP = {"0": "SZ", "1": "SH", "2": "BJ", "hk": "HK"}
            market_prefix = _PREFIX_MAP.get(prefix, "")
            qualified_code = (market_prefix + code) if market_prefix else code
            market = market_prefix.lower() if market_prefix else ""

            if _m._scan_aborted:
                return {"error": "扫描已终止", "aborted": True}

            with _m._scan_lock:
                if _m._scan_aborted:
                    return {"error": "扫描已终止", "aborted": True}
                result = _m.analyze_stock(qualified_code, freq=freq, cache_chan=True)

            t_analyze = time.time() - t0
            if "error" in result:
                _m._scan_skip_log.append(f"{code} - {result['error']}")
                return {"error": result["error"]}

            t0 = time.time()
            bsps = result.get("bsps", [])
            stock_name = result.get("meta", {}).get("name", f"{code}")
            klines = result.get("klines", [])
            t_filter = 0

            # ── 底分型扫描模式 ──
            if mode == "fx_d":
                bis = result.get("bis", [])
                is_fx_d = False
                fx_strength = 0
                if bis:
                    last_bi = bis[-1]
                    if last_bi.get("is_sure", True) and last_bi.get("direction") == "down":
                        is_fx_d = True
                        fx_strength = last_bi.get("fx_strength", 0)
                t_filter = time.time() - t0
                if is_fx_d:
                    return {
                        "code": code + "." + market.upper(), "name": stock_name,
                        "is_fx_d": True,
                        "last_close": klines[-1]["close"] if klines else 0,
                        "freq": freq,
                        "fx_strength": fx_strength,
                    }
                else:
                    mkt, cd = _m._get_market_code(qualified_code)
                    if mkt and cd:
                        _m._cache_remove(f"single_{mkt}_{cd}_{freq}_live")
                    return {"code": code, "is_fx_d": False}

            # ── 均线分类扫描模式 ──
            if mode == "ma":
                ma_periods = [5, 13, 21, 34, 55, 89, 144, 233]
                closes = [k.get("close", 0) for k in klines]
                last_close = closes[-1] if closes else 0
                ma_category = -1
                if last_close > 0 and len(closes) >= max(ma_periods):
                    conquered = 0
                    for p in ma_periods:
                        ma_val = sum(closes[-p:]) / p
                        if last_close >= ma_val:
                            conquered += 1
                    ma_category = 8 - conquered
                t_filter = time.time() - t0
                resp_data = {
                    "code": code + "." + market.upper(),
                    "name": stock_name,
                    "ma_category": ma_category,
                    "last_close": round(last_close, 2),
                    "freq": freq,
                }
                if ma_category > 3:
                    mkt, cd = _m._get_market_code(qualified_code)
                    if mkt and cd:
                        _m._cache_remove(f"single_{mkt}_{cd}_{freq}_live")
                return resp_data

            # ── 买卖点扫描模式 ──
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

            below_ma120 = False
            ma120_val = 0
            closes = [k.get("close", 0) for k in klines]
            last_close = klines[-1]["close"] if klines else 0
            if last_close > 0 and len(closes) >= 120:
                ma120_val = round(sum(closes[-120:]) / 120, 2)
                below_ma120 = last_close < ma120_val

            t_filter = time.time() - t0

            if not buy_points:
                mkt, cd = _m._get_market_code(qualified_code)
                if mkt and cd:
                    _m._cache_remove(f"single_{mkt}_{cd}_{freq}_live")

            if has_points:
                return {
                    "code": code + "." + market.upper(), "name": stock_name,
                    "buy_points": buy_points,
                    "sell_points": sell_points,
                    "last_close": klines[-1]["close"] if klines else 0,
                    "freq": freq,
                    "below_ma120": below_ma120,
                    "ma120_val": ma120_val,
                }
            else:
                return {"code": code, "buy_points": [], "sell_points": []}

        except Exception as exc:
            _m._scan_skip_log.append(f"{code} - 异常: {exc}")
            return {"error": str(exc)}

    # ── 扫描生命周期 ─────────────────────────────────────────────────
    def start(self):
        """新一轮扫描开始"""
        _m._scan_aborted = False
        _m._scan_skip_log.clear()
        _m._scan_start_time = time.time()
        try:
            _m._load_stock_names_from_cache_file()
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")
        return {"ok": True}

    def end(self):
        """扫描结束"""
        if _m._scan_skip_log:
            print(f"\n========== 扫描异常/失败股票明细 ==========")
            print(f"共 {len(_m._scan_skip_log)} 只:")
            for i, item in enumerate(_m._scan_skip_log, 1):
                print(f"  {i}. {item}")
            print("============================================\n")
        else:
            print("\n[扫描明细] 全部扫描成功，无异常股票\n")

        if _m._scan_start_time is not None:
            elapsed = time.time() - _m._scan_start_time
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            time_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"
            skip_count = len(_m._scan_skip_log)
            msg = f"耗时 {time_str}"
            if skip_count > 0:
                msg += f"，跳过 {skip_count} 只"
            _m._send_windows_notification("扫描完成", msg)
            _m._scan_start_time = None
        return {"count": len(_m._scan_skip_log)}

    def abort(self):
        """中断扫描"""
        _m._scan_aborted = True
        print("[扫描] 收到中断请求，设置终止标志")
        return {"ok": True}

    def clear_cache(self):
        """关闭扫描面板"""
        print("[扫描缓存] 面板关闭，缓存由 LRU 自然淘汰")
        return {"cleared": 0}

    def set_page_index_code(self, code):
        """设置当前板块指数代码"""
        code = code.strip()
        if code:
            if "." in code:
                code = code.split(".")[0]
            _m._page_index_code = code
            print(f"[成分股] 已设置板块指数代码: {code}")
            return {"ok": True, "code": code}
        else:
            return {"error": "缺少code参数"}


# 全局单例
scanner = ScannerService()

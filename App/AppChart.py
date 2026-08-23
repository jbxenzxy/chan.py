# -*- coding: utf-8 -*-
"""
App/AppChart.py —— 图表交互功能域
=========================================================================
按业务能力拆分（阶段 8 重设计）：本文件收纳「图表交互」触发的全部动作。
所谓图表交互，指用户在页面上与图表发生的一切操作：

  - 左上角输入股票/期货代码、切换周期、点击双窗口按钮、选择复盘日期
    → 触发缠论引擎核心去工作（call_analysis / run_analysis / analyze_stock）
  - 图表上手动选点（stock_manual_select_point：左肩定位 → 从 T 重拉 →
    新 CChan → 完整 chartData）
  - 图表上框选红框中枢（compute_red_range_zs：红框内笔序列 → 中枢重算）
  - 搜索（search_stocks）、代码解析（get_stock_market_code 等）
  - 选点 / 上次查看 / 期货子窗缓存漏斗
  - 市场/代码/周期查询漏斗（futures_cleanup / get_futures_aliases /
    get_futures_freqs 等，实现已迁 App/AppSSE.py，此处为图表交互入口的薄封装）

本模块收纳：
  - 分析漏斗（call_analysis / run_analysis / analyze_stock 等，持 _ENGINE_LOCK）
  - 手动选点 / 红框中枢（call_* 持锁漏斗 + RAW 原始实现）
  - 数据拉取与注入（fetch_and_inject / _get_data_source）
  - 审核标注（get_annotations / handle_annotation_action / delete_by_date
    等，原 App/AppAnnotate.py，阶段随本版合入：图表右键标注属图表交互）
  - 搜索（search_stocks）
  - 选点 / 上次查看 / 期货子窗缓存漏斗
  - 市场/代码/周期查询漏斗（futures_cleanup / get_futures_aliases /
    get_futures_freqs 等，实现已迁 App/AppSSE.py，此处为图表交互入口的薄封装）
  - 代码解析（get_stock_market_code / get_market_code / get_stock_name）

依赖方向：AppChart.py → AppEngine / AppSSE / AppData / DataAPI（单向）
锁定义：_ENGINE_LOCK 为引擎调用全局串行锁，本模块 call_* 漏斗持锁；
LOCK_POLICY 登记表在 AppOrch.py（聚合入口）统一维护。
"""
import os
import json
import time
import threading
import traceback

# 分析引擎层（阶段 10.1：my_chan_main.py 职责被各层完全吸收，引擎迁入 App/AppEngine.py）
from App import AppEngine as _m
# P1-5 缓存键规范化：结构化键工厂（消除字符串拼接歧义与漂移）
from App.AppData import app_data, make_single_key, make_dual_main_key, make_futures_sub_key
# SSE 实时流 / 期货功能域（阶段 8：期货选点/退出清理/市场代码周期查询实现已迁 AppSSE，此处仅漏斗）
from App import AppSSE as _sse
# 领域异常（P2-3：红框期货分支 error-dict → 领域异常，定义独立 App/AppErrors.py）
from App.AppErrors import DataFetchError, AnalysisError
# 区间套辅助（红框中枢重算：compute_red_range_zs 使用，与 AppEngine 同源）
from BuySellPoint.BSPointList import _red_range_bi_sequence, _red_range_amp
from App.AppLog import get_logger
log = get_logger(__name__)



# 引擎调用全局串行锁（锁分类 SERIAL 共用；LOCK_POLICY 登记见 AppOrch.py）
_ENGINE_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════
# 消费侧：分析引擎
# ═══════════════════════════════════════════════════════════════════════

def call_analysis(code, freq="d", end_date=None, dual=False, step=None, sub_freq=None):
    """单标的缠论分析（同步入口，REST 路由当前直接调用）

    - 引擎全局缓存非线程安全 → 全局串行锁保护
    - 开始/完成即时打印：uvicorn 仅在请求完成后记日志，挂起时控制台零输出，
      此日志是排障第一现场（阶段 1 Hotfix 教训）。
    """
    log.info(f"[api] /api/stock 开始分析: code={code!r} freq={freq!r} "
          f"end_date={end_date!r} dual={dual}")
    t0 = time.time()
    with _ENGINE_LOCK:
        result = _m.analyze_stock(code, freq=freq, end_date=end_date,
                                  dual=dual, step=step, sub_freq=sub_freq)
    log.info(f"[api] /api/stock 完成: code={code!r} 耗时 {time.time() - t0:.2f}s")
    return result


async def run_analysis(code, freq="d", end_date=None, dual=False, step=None, sub_freq=None):
    """单标的缠论分析（异步入口，阶段 3a SSE/REST 统一走此通道）

    线程池执行 + 串行锁：不阻塞事件循环（静态资源/健康检查保持可响应），
    同时保证同一时刻只有一个线程进入引擎。
    """
    import asyncio
    loop = asyncio.get_event_loop()
    log.info(f"[api] run_analysis 开始: code={code!r} freq={freq!r} "
          f"end_date={end_date!r} dual={dual}")
    t0 = time.time()

    def _job():
        with _ENGINE_LOCK:
            return _m.analyze_stock(code, freq=freq, end_date=end_date,
                                    dual=dual, step=step, sub_freq=sub_freq)

    try:
        return await loop.run_in_executor(None, _job)
    finally:
        log.info(f"[api] run_analysis 完成: code={code!r} 耗时 {time.time() - t0:.2f}s")


def analyze_stock(code, freq="d", end_date=None, cache_chan=True, dual=False, step=None, sub_freq=None):
    """统一的缠论分析入口 · 锁分类 RAW（无锁）

    ⚠ 本函数是引擎原始入口的薄封装，**并非无状态**：引擎内部维护模块级
    LRU 缓存（_stocks_analysis_cache）与名称/PE/市值等共享缓存，均非线程
    安全。此前的「无状态，可在线程池 / ProcessPool 复用」表述有误导。

    调用约定（LOCK_POLICY，见 AppOrch.py 文件头）：
      - 串行分析路径（REST 交互式）→ 必须走 call_analysis / run_analysis
        （持 _ENGINE_LOCK），不得直调本函数；
      - 扫描路径（SCAN）→ AppScan.Scanner.scan_one 内部调用（全局
        _scan_lock 内串行引擎调用，锁外保留并发）；
      - SSE 期货路径（SELF_CONTAINED）→ 独立 CChan 会话，不触共享缓存。
    """
    return _m.analyze_stock(code, freq=freq, end_date=end_date,
                            cache_chan=cache_chan, dual=dual, step=step, sub_freq=sub_freq)


# ═══════════════════════════════════════════════════════════════════════
# 图表标注（由原 App/AppAnnotate.py 合入：图表右键标注属图表交互域）
# 依赖方向：本小节 → AppData（单向，app_data 单例）
# 路由见 FrontAPI /api/annotations GET、/api/stocks/{code}/save|read/annotation
# ═══════════════════════════════════════════════════════════════════════

def get_annotations(code, freq):
    """获取标注数据（/api/annotations GET）"""
    from App.AppData import app_data
    return app_data.get_annotations_for(code, freq)


def handle_annotation_action(body):
    """标注增删改统一入口（/api/annotations POST，40 行校验逻辑下沉）

    body: {action, code, freq, date, text, y_offset, old_text}
    返回 (result_dict, status_code)，语义与原路由逐分支一致。
    """
    from App.AppData import app_data

    action = body.get("action", "")
    code = body.get("code", "")
    freq = body.get("freq", "d")
    date_str = body.get("date", "")
    text = body.get("text", "")
    y_offset = body.get("y_offset", 0)

    if not code:
        return {"error": "缺少code参数"}, 400

    if action == "add":
        if not date_str or not text:
            return {"error": "缺少date或text参数"}, 400
        success = app_data.add_annotation(code, freq, date_str, text, y_offset)
        return {"ok": success, "duplicate": not success}, 200
    elif action == "delete":
        if not date_str or not text:
            return {"error": "缺少date或text参数"}, 400
        success = app_data.delete_annotation(code, freq, date_str, text)
        return {"ok": success}, 200
    elif action == "delete_by_date":
        if not date_str:
            return {"error": "缺少date参数"}, 400
        success = app_data.delete_annotation_by_date(code, freq, date_str)
        return {"ok": success}, 200
    elif action == "delete_all":
        success = app_data.delete_all_annotations(code, freq)
        return {"ok": success}, 200
    elif action == "update":
        old_text = body.get("old_text", "")
        new_text = body.get("text", "")
        if not date_str or not old_text or not new_text:
            return {"error": "缺少date/old_text/text参数"}, 400
        app_data.delete_annotation(code, freq, date_str, old_text)
        success = app_data.add_annotation(code, freq, date_str, new_text, y_offset)
        return {"ok": success}, 200
    else:
        return {"error": f"未知action: {action}"}, 400


def call_manual_select_point(code, freq="d", bi_idx=-1):
    """股票手动选点 · SERIAL（持 _ENGINE_LOCK）

    原路由直连 m.stock_manual_select_point 绕锁（阶段 2 遗留问题 L185），
    阶段 3a 起统一走本漏斗：内部链路复用 analyze_stock 引擎与共享缓存。
    P0-2：直调本域本地实现（原经 AppEngine 兼容壳回跳，已删除）。
    """
    with _ENGINE_LOCK:
        return stock_manual_select_point(code, freq=freq, bi_idx=bi_idx)


def call_futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """期货手动选点 · SERIAL（持 _ENGINE_LOCK）

    内部走期货分析链路（_analyze_futures_internal，含期货缓存读写），
    归入串行分类，与股票侧共用引擎锁。
    P0-2：直调本域本地实现（原经 AppEngine 兼容壳回跳，已删除）。
    """
    with _ENGINE_LOCK:
        return futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)


def call_compute_red_range_zs(code, sub_freq="d", left_date="", right_date="", end_date=None):
    """红框中枢计算 · SERIAL（持 _ENGINE_LOCK）

    原路由直连 m.compute_red_range_zs 绕锁（阶段 2 遗留问题 L206），
    阶段 3a 起统一走本漏斗：内部复用 analyze_stock 引擎与共享缓存。
    P0-2：直调本域本地实现（原经 AppEngine 兼容壳回跳，已删除）。
    """
    with _ENGINE_LOCK:
        return compute_red_range_zs(code, sub_freq=sub_freq,
                                    left_date=left_date, right_date=right_date,
                                    end_date=end_date)


def stock_manual_select_point(code, freq="d", bi_idx=-1):
    """股票手动选点 · RAW（无锁原始入口）

    ⚠ 与 analyze_stock 同理并非无状态：内部走 analyze_stock 引擎链路与
    共享缓存。REST 调用方必须走 call_manual_select_point（持锁漏斗）；
    本签名保留供已按 SELF_CONTAINED 分类并自带会话隔离的路径使用。

    流程：通过前端传来的笔索引找到分型左肩第一根原始K线时间T → 保存T到
    CSV → 销毁旧CChan及_stocks_analysis_cache 中间状态 → 从T重新加载K线
    创建全新CChan，返回完整 chartData。
    """
    import re
    import gc
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
    cache_key = make_single_key(market, normalized_code, freq, date_suffix)
    qualified_code = f"{normalized_code}.{market.upper()}"  # 区分沪市深市同号股票
    cached = app_data.cache_get(cache_key)
    if cached is None:
        return {"error": "请先查询该股票"}

    if "chan" not in cached:
        # 扫描缓存只有result没有chan，重新分析以获取完整数据
        log.info(f"[信息] 缓存中无chan对象，重新分析 {normalized_code} {freq}")
        analyze_stock(normalized_code, freq=freq, cache_chan=True)
        cached = app_data.cache_get(cache_key)
        if cached is None or "chan" not in cached:
            return {"error": "缓存中无分析数据，请重新查询"}

    chan = cached["chan"]
    kl_list = chan[_m._get_kl_type(freq)]
    bi_list = kl_list.bi_list

    target_bi_idx = int(bi_idx)
    if target_bi_idx < 0 or target_bi_idx >= len(bi_list):
        return {"error": f"笔索引 {bi_idx} 越界，笔总数 {len(bi_list)}"}

    # 检查：选点之后至少需要4笔才能构建中枢（三笔重叠+确认判断）
    remaining_bis = len(bi_list) - target_bi_idx - 1
    if remaining_bis < 4:
        return {"error": f"选点之后仅剩 {remaining_bis} 笔，至少需要4笔才能构建中枢，请重新选点"}

    # Step 1: 找到左肩原始K线时间T
    start_time = _m._find_left_shoulder_time(kl_list, bi_list, target_bi_idx, freq)
    if start_time is None:
        return {"error": "无法定位左肩K线时间，请重试"}

    # Step 2: 保存选点到CSV（保存的是左肩第一根原始K线的时间T）
    stock_name = cached.get("result", {}).get("meta", {}).get("name", "")
    app_data.save_point_time(qualified_code, stock_name, freq, start_time)
    if qualified_code not in app_data.saved_point_times:
        app_data.saved_point_times[qualified_code] = {}
    app_data.saved_point_times[qualified_code]["name"] = stock_name
    app_data.saved_point_times[qualified_code][app_data.freq_to_col(freq) or ""] = start_time

    # Step 3: 销毁旧CChanA及所有中间状态，回到冷启动前的干净状态
    # P1-2：缓存删除统一经 app_data.cache_remove（内部持锁，消除手工锁+直改双路径）
    app_data.cache_remove(cache_key)
    gc.collect()

    # Step 4: 从T开始重新加载K线，创建CChanB，返回完整chartData
    result = _m._analyze_stock_internal(f"{normalized_code}.{market.upper()}", freq=freq, start_time=start_time)
    return result


def futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """期货手动选点 · RAW（无锁原始入口）

    ⚠ 内部读写期货共享缓存，非线程安全。REST 调用方必须走
    call_futures_manual_select_point（持锁漏斗）。实现已迁 App/AppSSE.py。
    """
    return _sse.futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)


def compute_red_range_zs(code, sub_freq="d", left_date="", right_date="", end_date=None):
    """红框中枢计算 · RAW（无锁原始入口）

    ⚠ 内部复用 analyze_stock 引擎与共享缓存。REST 调用方必须走
    call_compute_red_range_zs（持锁漏斗）。

    双窗口红框中枢计算：前端传来红框的左右边界时间 [left_date, right_date]，
    后端内部调用 _red_range_bi_sequence 找到被红框完全覆盖的子级别笔，再
    用 _red_range_amp 重新计算中枢，返回给前端绘制。
    """
    import re
    normalized_code = code.strip().upper()

    # ── 期货双窗口 ──
    if normalized_code.startswith("KQ."):
        cache_key = make_futures_sub_key(normalized_code, sub_freq)
        cached = app_data.futures_cache_get(cache_key)
        if cached is None:
            raise DataFetchError("双窗口下窗缓存已过期，请重新打开双窗口")
        chan = cached
        kl_list = chan[_m._get_kl_type(sub_freq)]
        bi_list = kl_list.bi_list
        date_fmt = _m._get_date_fmt(sub_freq)
        start_bi, end_bi = _red_range_bi_sequence(left_date, right_date, bi_list, sub_freq)
        if start_bi is None:
            raise AnalysisError(f"红框内无完整笔: [{left_date}, {right_date}]")
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
    cache_key = make_single_key(market, normalized_code, sub_freq, date_suffix)
    cached = app_data.cache_get(cache_key)

    # 双窗口新模式：当前 sub_freq 通常是下面窗口频率，优先从 dual_main 主级别缓存中的多级别 CChan 取子级别笔列表。
    # dual_sub 缓存只存 result/records，不存 chan；真正可用于重算中枢的 CChan 在 dual_main 缓存里。
    if (cached is None or "chan" not in cached) and sub_freq in _m._SUB_FREQ_MAP.values():
        for main_freq, _sub in _m._SUB_FREQ_MAP.items():
            if _sub == sub_freq:
                dual_main_cache_key = make_dual_main_key(market, normalized_code, main_freq, date_suffix)
                main_cached = app_data.cache_get(dual_main_cache_key)
                if main_cached and "chan" in main_cached:
                    main_chan = main_cached["chan"]
                    try:
                        _ = main_chan[_m._get_kl_type(sub_freq)]
                        cached = {"chan": main_chan}
                        break
                    except Exception as e:
                        log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
                if cached is None or "chan" not in cached:
                    single_main_cache_key = make_single_key(market, normalized_code, main_freq, date_suffix)
                    main_cached = app_data.cache_get(single_main_cache_key)
                    if main_cached and "chan" in main_cached:
                        main_chan = main_cached["chan"]
                        try:
                            _ = main_chan[_m._get_kl_type(sub_freq)]
                            cached = {"chan": main_chan}
                            log.info(f"[信息] compute_red_range_zs 从单窗口主级别缓存({main_freq})获取子级别({sub_freq})数据")
                            break
                        except Exception as e:
                            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")

    if cached is None:
        return {"error": "请先在该周期下加载K线数据"}
    if "chan" not in cached:
        log.info(f"[信息] 缓存中无chan对象，重新分析 {normalized_code} {sub_freq}")
        analyze_stock(f"{normalized_code}.{market.upper()}", freq=sub_freq, cache_chan=True)
        cached = app_data.cache_get(cache_key)
        if cached is None or "chan" not in cached:
            return {"error": "缓存中无分析数据，请重新查询"}

    chan = cached["chan"]
    kl_list = chan[_m._get_kl_type(sub_freq)]
    bi_list = kl_list.bi_list

    date_fmt = _m._get_date_fmt(sub_freq)

    # ── 步骤③：后端找被红框完全覆盖的笔 ──
    start_bi, end_bi = _red_range_bi_sequence(left_date, right_date, bi_list, sub_freq)
    if start_bi is None:
        return {"error": f"红框内无完整笔: [{left_date}, {right_date}]"}

    sliced_bis = bi_list[start_bi:end_bi + 1]
    zs_data = _red_range_amp(sliced_bis, bi_list, date_fmt)
    return {"zs": zs_data, "start_bi": start_bi, "end_bi": end_bi}


# ═══════════════════════════════════════════════════════════════════════
# 获取侧：数据拉取与注入（阶段 5：统一走 DataAPI 抽象层）
# ═══════════════════════════════════════════════════════════════════════

def _get_data_source(code, source="tdx"):
    """阶段 5：根据 code 类型和 source 参数选择数据源，返回 DataAPI 类引用。

    数据源选择规则：
      - tdx: 通达信本地数据（股票/指数/板块），使用 DataAPI.TdxAPI
      - tqsdk: 天勤期货数据（期货/期指），使用 DataAPI.TqSdkAPI
      - 自动检测: code 包含期货特征时自动选择 tqsdk

    返回 (api_module, is_futures) 元组。
    """
    from DataAPI.TqSdkAPI import _get_futures_code

    if source == "tqsdk" or _get_futures_code(code):
        from DataAPI import TqSdkAPI
        return TqSdkAPI, True

    from DataAPI import TdxAPI
    return TdxAPI, False


def fetch_and_inject(code, freq="d", end_date=None, dual=False, step=None, sub_freq=None):
    """
    阶段 5：判断股票 / 期货 → 拉取 K 线 → 注入分析引擎。

    fetch 统一走 DataAPI 抽象层（替换阶段 4 前的直连模式）：
      - 数据源选择由 analyze_stock 内部自动检测（股票走 TdxAPI / 期货走 TqSdkAPI）
      - 本函数为薄封装，直接委托 analyze_stock（其内部已通过 DataAPI 读取数据）
      - source 死参数已清理（P0-2）：原参数解析后未生效，一律委托 analyze_stock

    锁分类 RAW（无锁）：委托 analyze_stock，共享引擎缓存，非线程安全。
    串行调用方须走 call_analysis / run_analysis。
    """
    return _m.analyze_stock(code, freq=freq, end_date=end_date,
                            dual=dual, step=step, sub_freq=sub_freq)


# ═══════════════════════════════════════════════════════════════════════
# 搜索
# ═══════════════════════════════════════════════════════════════════════

def search_stocks(q):
    """股票代码 / 名称 / 拼音搜索（委托 my_chan_main 的缓存与别名）"""
    from App.AppData import app_data
    _m._load_stock_names_from_cache_file()
    if not os.path.exists(app_data.stock_names_cache_file):
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

    # 期货/期指别名搜索（阶段 5：经 CTqSdkAPI 市场/代码/周期查询接口）
    from DataAPI.TqSdkAPI import CTqSdkAPI
    for alias, full_code in CTqSdkAPI.FUTURES_ALIASES.items():
        if keyword_upper in alias.upper():
            name = _m._get_futures_name(full_code) if _m._get_futures_name else alias
            if not any(r["code"] == full_code for r in results):
                results.append({
                    "code": full_code, "name": name, "pinyin": alias,
                    "market": "futures", "type": "",
                })

    return {"results": results}


# ═══════════════════════════════════════════════════════════════════════
# 选点 / 上次查看 / 期货子窗缓存漏斗
# ═══════════════════════════════════════════════════════════════════════

def clear_saved_point(code, freq="d"):
    """清除选点并同步清缓存（/api/clear_saved_point）"""
    from App.AppData import app_data
    return app_data.clear_saved_point(code, freq)


def futures_clear_saved_point(symbol, freq="15s"):
    """期货清除选点（/api/futures_clear_saved_point）：别名解析 + 清 CSV"""
    from App.AppData import app_data

    symbol_upper = symbol.upper()
    from DataAPI.TqSdkAPI import CTqSdkAPI
    if symbol_upper in CTqSdkAPI.FUTURES_ALIASES:
        symbol = CTqSdkAPI.FUTURES_ALIASES[symbol_upper]
    app_data.clear_saved_point_time(symbol, freq)
    return {"ok": True}


def save_last_code_freq(code, freq="d"):
    """持久化上次查看代码/周期（/api/stock 成功后的副作用）"""
    from App.AppData import app_data
    return app_data.save_last_code_freq(code, freq)


def load_last_code_freq():
    """加载上次查看代码/周期（启动恢复）"""
    from App.AppData import app_data
    return app_data.load_last_code_freq()


def get_saved_point_times():
    """选点内存表（阶段 4：FrontAPI 经此只读访问，禁直连 my_chan_main 状态）"""
    from App.AppData import app_data
    return app_data.saved_point_times


def futures_cache_get(key):
    """期货分析缓存读（阶段 4 漏斗）"""
    from App.AppData import app_data
    return app_data.futures_cache_get(key)


def futures_cache_put(key, value):
    """期货分析缓存写（阶段 4 漏斗；SSE 双窗口下窗 chan 入缓存）"""
    from App.AppData import app_data
    return app_data.futures_cache_put(key, value)


def futures_cache_pop(key, default=None):
    """期货分析缓存失效（阶段 4 漏斗；连接关闭时释放）"""
    from App.AppData import app_data
    return app_data.futures_cache_pop(key, default)


def futures_set_sub_chan(symbol, sub_freq, chan):
    """写期货子窗 CChan（阶段 4 吸收评审：语义化漏斗，key 规则内聚数据层）"""
    from App.AppData import app_data
    return app_data.set_futures_sub_chan(symbol, sub_freq, chan)


def futures_get_sub_chan(symbol, sub_freq):
    """读期货子窗 CChan（语义化漏斗；symbol 大小写不敏感）"""
    from App.AppData import app_data
    return app_data.get_futures_sub_chan(symbol, sub_freq)


def futures_pop_sub_chan(symbol, sub_freq):
    """失效期货子窗 CChan（语义化漏斗；连接关闭时释放）"""
    from App.AppData import app_data
    return app_data.pop_futures_sub_chan(symbol, sub_freq)


def get_saved_point(code, freq):
    """查询单个选点：返回该 (code, freq) 已保存的选点时间或空串（阶段 4）"""
    from App.AppData import app_data
    col = app_data.freq_to_col(freq)
    if not col:
        return ""
    return app_data.saved_point_times.get(code, {}).get(col, "").strip()


# ═══════════════════════════════════════════════════════════════════════
# 期货
# ═══════════════════════════════════════════════════════════════════════

def futures_cleanup():
    """清理所有期货数据（实现迁 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse._cleanup_all_futures_data()


def get_futures_aliases():
    """期货别名映射（实现迁 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse.get_futures_aliases()


def get_futures_name(full_code):
    """期货名称（实现迁 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse.get_futures_name(full_code)


def tq_available():
    """天勤数据源是否可用（实现迁 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse.tq_available()


def get_futures_freqs():
    """期货可用周期列表（实现迁 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse.get_futures_freqs()


def get_stock_names_cache_file():
    """股票名称缓存文件路径"""
    from App.AppData import app_data
    return app_data.stock_names_cache_file


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

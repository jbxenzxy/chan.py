# -*- coding: utf-8 -*-
"""
App/AppChart.py —— 图表交互功能域
=========================================================================
收纳用户在页面上与图表交互触发的全部动作：
  - 分析漏斗（call_analysis / run_analysis / analyze_stock，持 _ENGINE_LOCK）
  - 手动选点 / 红框中枢（call_* 持锁漏斗 + RAW 原始实现）
  - 数据拉取与注入（fetch_and_inject，薄委托引擎 analyze_stock）
  - 审核标注（get_annotations / handle_annotation_action，图表右键标注）
  - 搜索（search_stocks）
  - 选点 / 上次查看 / 期货子窗缓存漏斗
  - 市场/代码/周期查询漏斗（futures_cleanup / get_futures_aliases 等，
    实现在 App/AppSSE.py，此处为图表交互入口的薄封装）
  - 代码解析（get_stock_market_code / get_market_code / get_stock_name）

依赖方向：AppChart.py → AppEngine / AppSSE / AppData（单向；
期货元数据一律经 AppSSE 出口，不直连 DataAPI）
锁定义：_ENGINE_LOCK 为引擎调用全局串行锁，本模块 call_* 漏斗持锁；
LOCK_POLICY 登记表在 AppOrch.py（聚合入口）统一维护。
"""
import os
import json
import time
import threading
import traceback

# 分析引擎层（App/AppEngine.py）
from App import AppEngine as _m
# 结构化缓存键工厂（消除字符串拼接歧义与漂移）
# compute_red_range_zs 独立双窗分支用 make_dual_sub_key 读 dual_sub 缓存的下窗 CChan
from App.AppData import (app_data, make_single_key, make_dual_main_key,
                         make_dual_sub_key, make_futures_sub_key)
# SSE 实时流 / 期货功能域（期货选点/退出清理/市场代码周期查询实现在 AppSSE，此处仅漏斗）
from App import AppSSE as _sse
# 领域异常（定义于 App/AppErrors.py）
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
      此日志是排障第一现场。
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
    """单标的缠论分析（异步入口，SSE/REST 统一走此通道）

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
    安全。

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
# 图表标注（图表右键标注属图表交互域）
# 依赖方向：本小节 → AppData（单向，app_data 单例）
# 路由见 FrontAPI /api/annotations GET、/api/stocks/{code}/save|read/annotation
# ═══════════════════════════════════════════════════════════════════════

def get_annotations(code, freq):
    """获取标注数据（/api/annotations GET）"""
    from App.AppData import app_data
    return app_data.get_annotations_for(code, freq)


def handle_annotation_action(body):
    """标注增删改统一入口（/api/annotations POST）

    body: {action, code, freq, date, text, y_offset, old_text}
    返回 (result_dict, status_code)。
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


def call_manual_select_point(code, freq="d", bi_idx=-1, dual=False, sub_freq=None, main_freq=None):
    """股票手动选点 · SERIAL（持 _ENGINE_LOCK）

    统一走本漏斗：内部链路复用 analyze_stock 引擎与共享缓存。
    透传双窗上下文（dual/sub_freq/main_freq），支持双窗选点。
    """
    with _ENGINE_LOCK:
        return stock_manual_select_point(code, freq=freq, bi_idx=bi_idx,
                                         dual=dual, sub_freq=sub_freq,
                                         main_freq=main_freq)


def call_futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """期货手动选点 · SERIAL（持 _ENGINE_LOCK）

    内部自建链路（CTqSdkSession + _build_futures_chan +
    _extract_realtime_snapshot，期货生产链路统一走 SSE），
    归入串行分类，与股票侧共用引擎锁。
    """
    with _ENGINE_LOCK:
        return futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)


def call_compute_red_range_zs(code, sub_freq="d", left_date="", right_date="", end_date=None):
    """红框中枢计算 · SERIAL（持 _ENGINE_LOCK）

    统一走本漏斗：内部复用 analyze_stock 引擎与共享缓存。
    """
    with _ENGINE_LOCK:
        return compute_red_range_zs(code, sub_freq=sub_freq,
                                    left_date=left_date, right_date=right_date,
                                    end_date=end_date)


def stock_manual_select_point(code, freq="d", bi_idx=-1, dual=False, sub_freq=None, main_freq=None):
    """股票手动选点 · RAW（无锁原始入口）

    ⚠ 与 analyze_stock 同理并非无状态：内部走 analyze_stock 引擎链路与
    共享缓存。REST 调用方必须走 call_manual_select_point（持锁漏斗）；
    本签名保留供已按 SELF_CONTAINED 分类并自带会话隔离的路径使用。

    流程：通过前端传来的笔索引找到分型左肩第一根原始K线时间T → 保存T到
    CSV → 销毁旧CChan及_stocks_analysis_cache 中间状态 → 从T重新加载K线
    创建全新CChan，返回完整 chartData。

    双窗选点：
      · dual=True 时 freq 为「双击所在窗口」周期（上窗或下窗），
        main_freq 为上窗周期（下窗选点时必传），sub_freq 为下窗周期；
      · CChan 取数按窗口定位：上窗选点读 dual_main 缓存；下窗选点读
        独立下窗运行时缓存/dual_sub 缓存（independent）或 dual_main
        多级别联立（legacy），miss 时回退单窗口缓存链；
      · 选点保存后同步销毁 dual_main+dual_sub 两键（双窗缓存命中不比对
        saved_selection_date，必须显式失效），重建走双窗路径（响应含
        data.sub），下窗重建半径自选点起算（CSV sub 列 → sub_saved_dt）。
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

    # ── 双窗上下文：配对校验 + 按窗口定位 CChan ──────────────
    dual_main_cache_key = dual_sub_cache_key = None
    if dual:
        if not main_freq:
            main_freq = freq           # 未显式传上窗周期 → 双击发生在上窗
        if not sub_freq:
            sub_freq = _m._SUB_FREQ_MAP.get(main_freq)
        pair_err = _m._validate_stock_dual_pair(main_freq, sub_freq)
        if pair_err:
            return {"error": pair_err}
        if freq != main_freq and freq != sub_freq:
            return {"error": f"选点周期 {freq} 不在双窗配对 {main_freq}+{sub_freq} 内"}
        dual_main_cache_key = make_dual_main_key(market, normalized_code, main_freq, date_suffix)
        dual_sub_cache_key = make_dual_sub_key(market, normalized_code, sub_freq, date_suffix)

    def _fetch_cached_chan():
        """选点目标窗口的 (chan, meta_name) 取数：双窗优先，回退单窗链"""
        if dual:
            dual_main_cached = app_data.cache_get(dual_main_cache_key)
            if freq == main_freq:
                # 上窗选点：主级别 CChan（legacy 联立含多级别，取主级别同源）
                if dual_main_cached is not None and "chan" in dual_main_cached:
                    name = dual_main_cached.get("result", {}).get("meta", {}).get("name", "")
                    return dual_main_cached["chan"], name
            else:
                # 下窗选点：independent 读独立下窗（运行时缓存 → dual_sub），
                # legacy 读 dual_main 多级别联立的子级别
                if _m._stock_dual_impl() == "independent":
                    chan_obj = app_data.stocks_sub_cache_get(f"{market}.{normalized_code}", freq)
                    name = ""
                    if chan_obj is None:
                        dual_sub_cached = app_data.cache_get(dual_sub_cache_key)
                        if dual_sub_cached is not None:
                            chan_obj = dual_sub_cached.get("chan")
                            name = dual_sub_cached.get("result", {}).get("meta", {}).get("name", "")
                    if chan_obj is not None:
                        return chan_obj, name
                elif dual_main_cached is not None and "chan" in dual_main_cached:
                    try:
                        main_chan = dual_main_cached["chan"]
                        _ = main_chan[_m._get_kl_type(sub_freq)]
                        name = dual_main_cached.get("result", {}).get("meta", {}).get("name", "")
                        return main_chan, name
                    except Exception as e:
                        log.warning(f"[选点] legacy 联立取下窗失败: {type(e).__name__}: {e}")
        # 单窗口（或双窗缓存缺失回退）：单窗缓存链
        cached = app_data.cache_get(cache_key)
        if cached is None:
            return None, ""
        if "chan" not in cached:
            # 扫描缓存只有result没有chan，重新分析以获取完整数据
            log.info(f"[信息] 缓存中无chan对象，重新分析 {normalized_code} {freq}")
            analyze_stock(normalized_code, freq=freq, cache_chan=True)
            cached = app_data.cache_get(cache_key)
            if cached is None or "chan" not in cached:
                return None, ""
        return cached["chan"], cached.get("result", {}).get("meta", {}).get("name", "")

    chan, stock_name = _fetch_cached_chan()
    if chan is None:
        return {"error": "请先在该周期窗口加载K线数据"}

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
    # 双窗例外（用户逻辑⑵⓶注1）：双窗口模式下双击选点【不保存】——
    # CSV 只承载单窗口选点（单窗 A 操作重启加载），双窗选点为会话内
    # 一次性视图（响应 meta.saved_selection_date 由引擎按显式
    # start_time 回显，供前端全量显示），不落 CSV、不进内存选点表。
    if not stock_name:
        single_cached = app_data.cache_get(cache_key)
        stock_name = (single_cached or {}).get("result", {}).get("meta", {}).get("name", "")
    if not dual:
        app_data.save_point_time(qualified_code, stock_name, freq, start_time)
        if qualified_code not in app_data.saved_point_times:
            app_data.saved_point_times[qualified_code] = {}
        app_data.saved_point_times[qualified_code]["name"] = stock_name
        app_data.saved_point_times[qualified_code][app_data.freq_to_col(freq) or ""] = start_time

    # Step 3: 销毁旧CChanA及所有中间状态，回到冷启动前的干净状态
    # 缓存删除统一经 app_data.cache_remove（内部持锁）
    app_data.cache_remove(cache_key)
    if dual:
        # 双窗两键一并失效（双窗缓存命中不比对 saved_selection_date，
        # 不删除会命中旧 result，选点重建失效）
        app_data.cache_remove(dual_main_cache_key)
        app_data.cache_remove(dual_sub_cache_key)
        app_data.stocks_sub_cache_pop(f"{market}.{normalized_code}", sub_freq)
    gc.collect()

    # Step 4: 从T开始重新加载K线，创建CChanB，返回完整chartData
    # 双窗：重建走双窗路径（响应含 data.sub）——
    #   上窗选点 start_time=T 作用于上窗（freq==main_freq 时双击发生在上窗，
    #   前端限制下窗选点，freq!=main_freq 分支为防御路径）；
    #   下窗无选点概念（双窗选点不保存、不读 CSV），纯对齐上窗
    #   [T, 最新] 区间加载（对齐不足时引擎降全量兜底）。
    rebuild_start_time = start_time if freq == main_freq else None
    result = _m._analyze_stock_internal(
        f"{normalized_code}.{market.upper()}",
        freq=(main_freq if dual else freq),
        start_time=rebuild_start_time,
        dual=dual,
        sub_freq=(sub_freq if dual else None))
    return result


def futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """期货手动选点 · RAW（无锁原始入口）

    ⚠ 内部读写期货共享缓存，非线程安全。REST 调用方必须走
    call_futures_manual_select_point（持锁漏斗）。实现在 App/AppSSE.py。
    """
    return _sse.futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)


def compute_red_range_zs(code, sub_freq="d", left_date="", right_date="", end_date=None):
    """红框中枢计算 · RAW（无锁原始入口）

    ⚠ 内部复用 analyze_stock 引擎与共享缓存。REST 调用方必须走
    call_compute_red_range_zs（持锁漏斗）。

    双窗口红框中枢计算：前端传来红框的左右边界时间 [left_date, right_date]，
    后端内部调用 _red_range_bi_sequence 找到被红框完全覆盖的子级别笔，再
    用 _red_range_amp 重新计算中枢，返回给前端绘制。

    股票双窗实现（A/B 开关 CHAN_STOCK_DUAL_IMPL）：
      · independent（默认）：读独立下窗 CChan（运行时缓存 → dual_sub
        结构化缓存），miss 抛 DataFetchError（对齐期货语义）；
      · legacy：联立缓存回退链（single → dual_main → single 主级别，
        miss 时回退重算），行为冻结作 A/B 基线。
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

    # ── 独立双窗实现：红框中枢读「独立下窗 CChan」──
    # 读取顺序：运行时缓存（stocks_sub_cache，双窗分析先下后上写入，最新鲜）
    #         → dual_sub 结构化缓存（独立实现随分析落 chan，复盘态亦可整读）。
    # 两者皆 miss 抛领域异常（对齐期货语义：红框依赖下窗笔结构，
    # 服务重启/双窗重建间隙等异常态不静默回退，交前端提示重开双窗口）。
    if _m._stock_dual_impl() == "independent":
        chan_code = f"{market}.{normalized_code}"
        chan_obj = app_data.stocks_sub_cache_get(chan_code, sub_freq)
        if chan_obj is None:
            dual_sub_cached = app_data.cache_get(
                make_dual_sub_key(market, normalized_code, sub_freq, date_suffix))
            if dual_sub_cached is not None:
                chan_obj = dual_sub_cached.get("chan")
        if chan_obj is None:
            log.warning(f"[red_range_zs] 独立双窗下窗缓存缺失: {chan_code} {sub_freq} "
                        f"(date_suffix={date_suffix})")
            raise DataFetchError("双窗口下窗缓存已过期，请重新打开双窗口")
        kl_list = chan_obj[_m._get_kl_type(sub_freq)]
        bi_list = kl_list.bi_list
        date_fmt = _m._get_date_fmt(sub_freq)
        start_bi, end_bi = _red_range_bi_sequence(left_date, right_date, bi_list, sub_freq)
        if start_bi is None:
            raise AnalysisError(f"红框内无完整笔: [{left_date}, {right_date}]")
        sliced_bis = bi_list[start_bi:end_bi + 1]
        zs_data = _red_range_amp(sliced_bis, bi_list, date_fmt)
        return {"zs": zs_data, "start_bi": start_bi, "end_bi": end_bi}

    # ── legacy 联立实现（A/B 基线，行为冻结）：缓存回退链 ──
    cache_key = make_single_key(market, normalized_code, sub_freq, date_suffix)
    cached = app_data.cache_get(cache_key)

    # 双窗口：当前 sub_freq 通常是下面窗口频率，优先从 dual_main 主级别缓存中的多级别 CChan 取子级别笔列表。
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
# 获取侧：数据拉取与注入（统一走 DataAPI 抽象层）
# ═══════════════════════════════════════════════════════════════════════

def fetch_and_inject(code, freq="d", end_date=None, dual=False, step=None, sub_freq=None):
    """
    判断股票 / 期货 → 拉取 K 线 → 注入分析引擎。

    fetch 统一走 DataAPI 抽象层：
      - 数据源选择由 analyze_stock 内部自动检测（股票走 TdxAPI / 期货走 TqSdkAPI）
      - 本函数为薄封装，直接委托 analyze_stock（其内部已通过 DataAPI 读取数据）

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

    # 期货/期指别名搜索（经期货域元数据出口，图表层不直连 CTqSdkAPI）
    _futures_aliases = _sse.get_futures_aliases()
    for alias, full_code in _futures_aliases.items():
        if keyword_upper in alias.upper():
            name = _sse.get_futures_name(full_code)
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

    # 别名解析经期货域元数据出口（图表层不直连 CTqSdkAPI）
    symbol_upper = symbol.upper()
    _futures_aliases = _sse.get_futures_aliases()
    if symbol_upper in _futures_aliases:
        symbol = _futures_aliases[symbol_upper]
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
    """选点内存表（FrontAPI 经此只读访问）"""
    from App.AppData import app_data
    return app_data.saved_point_times


def futures_cache_get(key):
    """期货分析缓存读"""
    from App.AppData import app_data
    return app_data.futures_cache_get(key)


def futures_cache_put(key, value):
    """期货分析缓存写（SSE 双窗口下窗 chan 入缓存）"""
    from App.AppData import app_data
    return app_data.futures_cache_put(key, value)


def futures_cache_pop(key, default=None):
    """期货分析缓存失效（连接关闭时释放）"""
    from App.AppData import app_data
    return app_data.futures_cache_pop(key, default)


def futures_set_sub_chan(symbol, sub_freq, chan):
    """写期货子窗 CChan（语义化漏斗，key 规则内聚数据层）"""
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
    """查询单个选点：返回该 (code, freq) 已保存的选点时间或空串"""
    from App.AppData import app_data
    col = app_data.freq_to_col(freq)
    if not col:
        return ""
    return app_data.saved_point_times.get(code, {}).get(col, "").strip()


# ═══════════════════════════════════════════════════════════════════════
# 期货
# ═══════════════════════════════════════════════════════════════════════

def futures_cleanup():
    """清理所有期货数据（实现在 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse._cleanup_all_futures_data()


def get_futures_aliases():
    """期货别名映射（实现在 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse.get_futures_aliases()


def get_futures_name(full_code):
    """期货名称（实现在 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse.get_futures_name(full_code)


def tq_available():
    """天勤数据源是否可用（实现在 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse.tq_available()


def get_futures_freqs():
    """期货可用周期列表（实现在 App/AppSSE.py，此处为图表交互入口漏斗）"""
    return _sse.get_futures_freqs()


def get_futures_freq_sec_map():
    """期货周期→秒数映射（/api/health 单一事实源出口，实现 App/AppSSE.py）"""
    return _sse.get_futures_freq_sec_map()


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

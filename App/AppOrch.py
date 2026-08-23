# -*- coding: utf-8 -*-
"""
App/AppOrch.py —— 业务编排层（服务层）聚合入口
=========================================================================
阶段 8 重设计：按业务能力拆分后，本文件降级为聚合入口（re-export）。

拆分后各功能文件（App + 动词命名，与 AppConfig/AppData/AppEngine 平铺）：
  - AppChart.py      图表交互（左上角输入代码、切换周期、双窗口、复盘、手动选点、红框中枢）
  - AppSSE.py        SSE 实时流（期货实时行情推送 / 期货静态分析 / 期货选点 / 期货元数据）
  - AppScan.py       股票扫描（右上角「股票扫描」按钮）
  - AppDownload.py   盘后下载（右上角「盘后下载」按钮）
  - AppRefresh.py    刷新（右上角「刷新」按钮：股票名/指数归属/PE-TTM/板块）

标注归 AppChart（图表右键标注属图表交互域，原 AppAnnotate 已合并删除）。

本文件保留：
  - 领域异常层级 re-export（AppError 等 6 类，定义已独立 App/AppErrors.py，
    P2-3 为消除双轨并存并避免 AppSSE→AppOrch→AppChart→AppSSE 循环依赖；
    Test/test_phase2_guards.py 引用）
  - LOCK_POLICY 锁分类登记表（Test/test_phase3_guards.py 守护）
  - 全部业务函数 re-export（FrontAPI 的 orch.xxx 调用零改动）

依赖方向（设计文档 6.2 节）：
  FrontAPI.py → App/AppOrch.py → 各功能文件 → AppEngine / AppData（单向）

使用方式：
    from App.AppOrch import analyze_stock, Scanner, call_analysis
    result = call_analysis("000001.SH", freq="d")
"""
import threading

# 分析引擎层（阶段 10.1：my_chan_main.py 职责被各层完全吸收，引擎迁入 App/AppEngine.py）
from App import AppEngine as _m

# ── 各功能域 re-export ─────────────────────────────────────────────────
from App.AppChart import (
    _ENGINE_LOCK,
    call_analysis, run_analysis, analyze_stock,
    call_manual_select_point, call_futures_manual_select_point,
    call_compute_red_range_zs,
    stock_manual_select_point, futures_manual_select_point, compute_red_range_zs,
    fetch_and_inject,
    search_stocks,
    get_annotations, handle_annotation_action,
    clear_saved_point, futures_clear_saved_point,
    save_last_code_freq, load_last_code_freq,
    get_saved_point_times,
    futures_cache_get, futures_cache_put, futures_cache_pop,
    futures_set_sub_chan, futures_get_sub_chan, futures_pop_sub_chan,
    get_saved_point,
    futures_cleanup, get_futures_aliases, get_futures_name,
    tq_available, get_futures_freqs, get_futures_freq_sec_map,
    get_stock_names_cache_file,
    get_stock_market_code, get_market_code, get_stock_name,
)
from App.AppScan import (
    Scanner, scanner,
    get_annotated_codes, read_zxg_stocks, zxg_save,
    save_scan_to_ths_cloud, ths_cloud_available,
)
from App.AppDownload import (
    start_download_checked, start_download, stop_download, get_download_status,
    eltdx_available, download_dir,
)
from App.AppRefresh import (
    load_stock_names_from_cache_file, refresh_stock_names,
    load_float_mc_cache, fetch_float_mc_from_tencent, update_float_mc_cache,
    load_pe_ttm_cache, get_pe_ttm, get_index_belong,
    refresh_status, refresh_stock_names_async,
)



# ═══════════════════════════════════════════════════════════════════════
# 引擎调用锁分类建档（阶段 3a）
# =====================================================================
# 背景（阶段 2 遗留问题）：引擎全局缓存（_stocks_analysis_cache、
# _futures_analysis_cache、名称/PE/市值缓存等）非线程安全，但阶段 2 仅
# call_analysis / run_analysis 持锁，api_server 有 3 处直连引擎绕锁。
#
# 解法（非全面串行化）：给引擎调用按「是否触碰共享状态」分类建档——
#
#   SERIAL（串行分析）  REST 交互式分析路径（单标的分析 / 手动选点 /
#                      红框中枢计算 / 期货选点）。共用 _ENGINE_LOCK：
#                      同一时刻只有一个线程进入引擎，交互延迟可接受。
#
#   SCAN（并行扫描）    批量扫描路径。前端按 SCAN_CONCURRENCY 并发发起
#                      /api/scan_one。_scan_lock 为全局锁（单实例，非按
#                      票）：锁内串行化引擎调用 analyze_stock（保护非线程
#                      安全的引擎缓存不被并发写），锁外的预处理/结果过滤
#                      保留并发——即并发体现在非引擎阶段，引擎阶段全局
#                      串行（阶段 2.6 基线继承语义，本阶段零改动，收敛
#                      计划随阶段 5 数据层拆分一并处理）。
#
#   SELF_CONTAINED     SSE 期货实时流。每连接独立 TqApi + CChan 对象，
#                      不触碰 _stocks_analysis_cache（_futures_analysis_
#                      cache 仅按 symbol:freq 键存放下窗 CChan 供
#                      /api/dual_zs 读取，启动写入/收尾弹出，无跨连接
#                      读改写竞争）。连接间天然隔离，不加锁。
#
# 约定：路由层（FrontAPI）禁止直连 m.analyze_stock 等引擎函数，
# 一律经本层 call_* 漏斗（锁策略集中在 LOCK_POLICY 登记，
# Test/test_phase3_guards.py 守护）。
# ═══════════════════════════════════════════════════════════════════════

# 锁策略登记表：入口 → (类别, 说明)。守护用例校验完备性与一致性。
LOCK_POLICY = {
    "call_analysis":                ("SERIAL", "REST 单标的分析：共享分析缓存 → _ENGINE_LOCK"),
    "run_analysis":                 ("SERIAL", "call_analysis 异步版：线程池执行 + _ENGINE_LOCK，不阻塞事件循环"),
    "call_manual_select_point":     ("SERIAL", "股票手动选点：内部走 analyze_stock 引擎链路 → _ENGINE_LOCK"),
    "call_futures_manual_select_point": ("SERIAL", "期货手动选点：内部走期货分析链路（含期货缓存）→ _ENGINE_LOCK"),
    "call_compute_red_range_zs":    ("SERIAL", "红框中枢计算：内部走 analyze_stock 引擎链路 → _ENGINE_LOCK"),
    "analyze_stock":                ("RAW", "引擎原始入口（无锁）：仅供 SCAN/SELF_CONTAINED 分类路径内部使用；"
                                      "串行调用方必须改走 call_analysis / run_analysis"),
    "fetch_and_inject":             ("RAW", "引擎原始入口薄封装（无锁）：同 analyze_stock，阶段 5 拆分时收敛"),
    "Scanner.scan_one":             ("SCAN", "扫描路径（同步旧径）：引擎调用在全局 _scan_lock 内串行（基线继承，保护引擎缓存），锁外预处理/过滤保留 SCAN_CONCURRENCY 并发；不加 _ENGINE_LOCK；阶段 7 起前端批量扫描改走 SCAN_ASYNC，本径保留兼容"),
    "Scanner.submit_batch_scan":    ("SCAN_ASYNC", "批量扫描提交（阶段 7）：股票清单派发至执行池（ProcessPool spawn 优先，受限环境降级 ThreadPool），引擎调用在 worker 内走 scan_one（每 worker 独立 _scan_lock），API 进程零持锁；结果经 SQLite 扫描库回流供前端轮询"),
    "sse_futures_stream_single":    ("SELF_CONTAINED", "SSE 单窗口（FrontAPI）：每连接独立 TqApi+CChan，不触共享分析缓存"),
    "sse_futures_stream_dual":      ("SELF_CONTAINED", "SSE 双窗口（FrontAPI）：独立 TqApi+双 CChan，连接间隔离"),
}


# ═══════════════════════════════════════════════════════════════════════
# 领域异常层级（定义独立 App/AppErrors.py，P2-3 起；见设计文档 7.7 节）
# 服务层只抛领域异常，API 层通过统一中间件捕获。
# ═══════════════════════════════════════════════════════════════════════

from App.AppErrors import (
    AppError,
    DataFetchError,
    AnalysisError,
    ConfigError,
    NotFoundError,
    PersistenceError,
)


# ═══════════════════════════════════════════════════════════════════════
# 兼容别名（阶段 8 拆分后，历史 import 路径仍可用）
# ═══════════════════════════════════════════════════════════════════════

# ScannerService 旧名兼容（Test/test_phase3_guards.py 等历史引用）
ScannerService = Scanner

__all__ = [
    # 异常
    "AppError", "DataFetchError", "AnalysisError", "ConfigError",
    "NotFoundError", "PersistenceError",
    # 锁
    "_ENGINE_LOCK", "LOCK_POLICY",
    # 分析漏斗（AppChart）
    "call_analysis", "run_analysis", "analyze_stock",
    "call_manual_select_point", "call_futures_manual_select_point",
    "call_compute_red_range_zs",
    "stock_manual_select_point", "futures_manual_select_point",
    "compute_red_range_zs",
    "fetch_and_inject", "search_stocks",
    "clear_saved_point", "futures_clear_saved_point",
    "save_last_code_freq", "load_last_code_freq", "get_saved_point_times",
    "futures_cache_get", "futures_cache_put", "futures_cache_pop",
    "futures_set_sub_chan", "futures_get_sub_chan", "futures_pop_sub_chan",
    "get_saved_point",
    "futures_cleanup", "get_futures_aliases", "get_futures_name",
    "tq_available", "get_futures_freqs", "get_futures_freq_sec_map",
    "get_stock_names_cache_file",
    "get_stock_market_code", "get_market_code", "get_stock_name",
    # 扫描（AppScan）
    "Scanner", "ScannerService", "scanner",
    "get_annotated_codes", "read_zxg_stocks", "zxg_save",
    "save_scan_to_ths_cloud", "ths_cloud_available",
    # 下载（AppDownload）
    "start_download_checked", "start_download", "stop_download",
    "get_download_status", "eltdx_available", "download_dir",
    # 刷新（AppRefresh）
    "load_stock_names_from_cache_file", "refresh_stock_names",
    "load_float_mc_cache", "fetch_float_mc_from_tencent",
    "update_float_mc_cache", "load_pe_ttm_cache", "get_pe_ttm",
    "get_index_belong", "refresh_status", "refresh_stock_names_async",
    # 标注（AppChart）
    "get_annotations", "handle_annotation_action",
]

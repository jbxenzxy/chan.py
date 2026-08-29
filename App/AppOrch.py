# -*- coding: utf-8 -*-
"""
App/AppOrch.py —— 业务编排层（服务层）聚合入口
=========================================================================
本文件为聚合入口（re-export），各功能文件按业务能力拆分
（App + 动词命名，与 AppConfig/AppData/AppEngine 平铺）：
  - AppChart.py      图表交互（左上角输入代码、切换周期、双窗口、复盘、手动选点、红框中枢）
  - AppSSE.py        SSE 实时流（期货实时行情推送 / 期货复盘 / 期货选点 / 期货元数据）
  - AppScan.py       股票扫描（右上角「股票扫描」按钮）
  - AppDownload.py   盘后下载（右上角「盘后下载」按钮）
  - AppRefresh.py    刷新（右上角「刷新」按钮：股票名/指数归属/PE-TTM/板块）

标注归 AppChart（图表右键标注属图表交互域）。

本文件持有：
  - 领域异常层级 re-export（AppError 等 6 类，定义在 App/AppErrors.py；
    Test/test_phase2_guards.py 引用）
  - LOCK_POLICY 锁分类登记表（Test/test_phase3_guards.py 守护）
  - 全部业务函数 re-export（FrontAPI 的 orch.xxx 调用零改动）

依赖方向：
  FrontAPI.py → App/AppOrch.py → 各功能文件 → AppEngine / AppData（单向）

使用方式：
    from App.AppOrch import analyze_stock, Scanner, call_analysis
    result = call_analysis("sh600519", freq="d")
"""
# ── 各功能域 re-export ─────────────────────────────────────────────────
from App.AppChart import (
    _ENGINE_LOCK,
    engine_section,
    call_analysis, analyze_stock,
    call_manual_select_point, call_futures_manual_select_point,
    call_compute_red_range_zs,
    stock_manual_select_point, futures_manual_select_point, compute_red_range_zs,
    search_stocks,
    get_annotations, handle_annotation_action,
    clear_saved_point, futures_clear_saved_point,
    save_last_code_freq, load_last_code_freq,
    futures_cleanup, get_futures_aliases, get_futures_name,
    tq_available, get_futures_freqs, get_futures_freq_sec_map,
    get_stock_names_cache_file,
)
from App.AppScan import (
    Scanner, scanner,
    get_annotated_codes, read_zxg_stocks, zxg_save,
    save_scan_to_ths_cloud,
)
from App.AppDownload import (
    start_download_checked, stop_download, get_download_status,
    download_dir,
)
from App.AppRefresh import (
    load_stock_names_from_cache_file, refresh_stock_names,
    load_float_mc_cache, fetch_float_mc_from_tencent, update_float_mc_cache,
    load_pe_ttm_cache, get_pe_ttm, get_index_belong,
    refresh_status, refresh_stock_names_async,
)
from App.AppAMO import call_amo



# ═══════════════════════════════════════════════════════════════════════
# 引擎调用锁分类建档
# =====================================================================
# 背景：引擎全局缓存（_stocks_analysis_cache、
# _futures_analysis_cache、名称/PE/市值缓存等）非线程安全，
# 引擎调用必须按「是否触碰共享状态」分类，防止绕锁并发写坏缓存。
#
#   SERIAL（串行分析）  REST 交互式分析路径（单标的分析 / 手动选点 /
#                      红框中枢计算 / 期货选点）。共用 _ENGINE_LOCK：
#                      同一时刻只有一个线程进入引擎，交互延迟可接受。
#
#   SCAN（并行扫描）    批量扫描路径。前端并发发起 /api/scan_one，任务派发
#                      至执行池（SCAN_ASYNC，worker 数由 SCAN_POOL_WORKERS
#                      决定）。_scan_lock 为全局锁（单实例，非按票）：锁内串行
#                      化引擎调用 analyze_stock（保护非线程安全的引擎缓存不被
#                      并发写），锁外的预处理/结果过滤保留并发——即并发体现在
#                      非引擎阶段，引擎阶段全局串行。
#
#   SELF_CONTAINED     SSE 期货实时流。每连接独立 TqApi + CChan 对象，
#                      不触碰 _stocks_analysis_cache（_futures_analysis_
#                      cache 仅按 symbol:freq 键存放下窗 CChan 供
#                      /api/dual_zs 读取，启动写入/收尾弹出，无跨连接
#                      读改写竞争）。连接间天然隔离，不加锁。
#
# 约定：路由层（FrontAPI）禁止直连 m.analyze_stock 等引擎函数，
# 一律经本层 call_* 漏斗（锁策略集中在 LOCK_POLICY 登记，
# P1-2 起经 engine_section 可执行化：SERIAL 漏斗实现用
# `with engine_section("<入口名>"):` 按登记分类实际取锁，
# Test/test_phase3_guards.py 守护）。
# ═══════════════════════════════════════════════════════════════════════

# 锁策略登记表：入口 → (类别, 说明)。P1-2 起该表不再只是文档，
# engine_section 按类别实际取锁（SERIAL→_ENGINE_LOCK，其余不加锁），
# 守护用例校验「登记即执行」。
LOCK_POLICY = {
    "call_analysis":                ("SERIAL", "REST 单标的分析：共享分析缓存 → _ENGINE_LOCK"),
    "call_manual_select_point":     ("SERIAL", "股票手动选点：内部走 analyze_stock 引擎链路 → _ENGINE_LOCK；"
                                      "双窗选点含 dual_main/dual_sub/下窗运行时缓存读写，同锁覆盖"),
    "call_futures_manual_select_point": ("SERIAL", "期货手动选点：内部走期货分析链路（含期货缓存）→ _ENGINE_LOCK"),
    "call_compute_red_range_zs":    ("SERIAL", "红框中枢计算：内部走 analyze_stock 引擎链路 → _ENGINE_LOCK；"
                                      "独立双窗分支读下窗运行时缓存/dual_sub 缓存，miss 抛 DataFetchError"),
    "analyze_stock":                ("RAW", "引擎原始入口（无锁）：仅供 SCAN/SELF_CONTAINED 分类路径内部使用；"
                                      "串行调用方必须改走 call_analysis"),
    "Scanner.scan_one":             ("SCAN", "扫描路径（同步旧径）：引擎调用在全局 _scan_lock 内串行（基线继承，保护引擎缓存），锁外预处理/过滤保留前端并发请求；不加 _ENGINE_LOCK；前端批量扫描走 SCAN_ASYNC，本径保留兼容"),
    "Scanner.submit_batch_scan":    ("SCAN_ASYNC", "批量扫描提交：股票清单派发至执行池（ProcessPool spawn 优先，受限环境降级 ThreadPool），引擎调用在 worker 内走 scan_one（每 worker 独立 _scan_lock），API 进程零持锁；结果经 SQLite 扫描库回流供前端轮询"),
    "sse_futures_stream_single":    ("SELF_CONTAINED", "SSE 单窗口（FrontAPI）：每连接独立 TqApi+CChan，不触共享分析缓存"),
    "sse_futures_stream_dual":      ("SELF_CONTAINED", "SSE 双窗口（FrontAPI）：独立 TqApi+双 CChan，连接间隔离"),
    "call_amo":                     ("SERIAL", "市场量能：读 TDX 本地指数日线成交额（sh000001+sz399106），不触引擎共享缓存；持 _ENGINE_LOCK 与引擎调用/下载写盘串行"),
}


# ═══════════════════════════════════════════════════════════════════════
# 领域异常层级（定义在 App/AppErrors.py）
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
# 兼容别名：旧 import 路径仍可用
# ═══════════════════════════════════════════════════════════════════════

# ScannerService 旧名兼容（Test/test_phase3_guards.py 等引用）
ScannerService = Scanner

__all__ = [
    # 异常
    "AppError", "DataFetchError", "AnalysisError", "ConfigError",
    "NotFoundError", "PersistenceError",
    # 锁
    "_ENGINE_LOCK", "LOCK_POLICY",
    # 分析漏斗（AppChart）
    "call_analysis", "analyze_stock",
    "call_manual_select_point", "call_futures_manual_select_point",
    "call_compute_red_range_zs",
    "stock_manual_select_point", "futures_manual_select_point",
    "compute_red_range_zs",
    "search_stocks",
    "clear_saved_point", "futures_clear_saved_point",
    "save_last_code_freq", "load_last_code_freq",
    "futures_cleanup", "get_futures_aliases", "get_futures_name",
    "tq_available", "get_futures_freqs", "get_futures_freq_sec_map",
    "get_stock_names_cache_file",
    # 扫描（AppScan）
    "Scanner", "ScannerService", "scanner",
    "get_annotated_codes", "read_zxg_stocks", "zxg_save",
    "save_scan_to_ths_cloud",
    # 下载（AppDownload）
    "start_download_checked", "stop_download",
    "get_download_status", "download_dir",
    # 刷新（AppRefresh）
    "load_stock_names_from_cache_file", "refresh_stock_names",
    "load_float_mc_cache", "fetch_float_mc_from_tencent",
    "update_float_mc_cache", "load_pe_ttm_cache", "get_pe_ttm",
    "get_index_belong", "refresh_status", "refresh_stock_names_async",
    # 市场量能（AppAMO）
    "call_amo",
    # 标注（AppChart）
    "get_annotations", "handle_annotation_action",
]

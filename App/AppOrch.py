# -*- coding: utf-8 -*-
"""
App/AppOrch.py —— 业务编排层（服务层）聚合入口
=========================================================================
本文件为聚合入口（re-export），各功能文件按业务能力拆分
（App + 动词命名，与 AppConfig/AppData/AppEngine 平铺）：
  - AppChart.py      图表交互（左上角输入代码、切换周期、双窗口、复盘、手动选点、红框中枢）
  - AppSSE.py        SSE 实时流（期货实时行情推送 / 期货复盘 / 期货选点 / 期货元数据）
  - AppScan.py       股票扫描（右上角「股票扫描」按钮）
  - AppRefresh.py    刷新（右上角「刷新」按钮：股票名/指数归属/PE-TTM/板块）

标注归 AppChart（图表右键标注属图表交互域）。

本文件持有：
  - 领域异常层级 re-export（AppError 等 6 类，定义在 App/AppErrors.py；
    Test/test_phase2_guards.py 引用）
  - SHARED_RESOURCE_REGISTRY 共享资源登记表（按资源索引，取代原
    LOCK_POLICY；Test/test_phase3_guards.py 守护）
  - 全部业务函数 re-export（FrontAPI 的 orch.xxx 调用零改动）

依赖方向：
  FrontAPI.py → App/AppOrch.py → 各功能文件 → AppEngine / AppData（单向）

锁：本层不持有锁。共享资源的锁由各持有者（AppData / AppScanPool /
AppRefresh / DataAPI）按资源持有，登记表见 SHARED_RESOURCE_REGISTRY。

使用方式：
    from App.AppOrch import analyze_stock, Scanner, call_analysis
    result = call_analysis("sh600519", freq="d")
"""
# ── 各功能域 re-export ─────────────────────────────────────────────────
from App.AppChart import (
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
    load_float_mc_cache, fetch_float_mc_from_tencent, update_float_mc_cache,
)
from App.AppRefresh import (
    load_stock_names_from_cache_file, refresh_stock_names,
    load_pe_ttm_cache, get_pe_ttm, get_index_belong,
    refresh_status, refresh_stock_names_async,
)
from App.AppAMO import call_amo



# ═══════════════════════════════════════════════════════════════════════
# 共享资源登记表（按「资源」索引，不是按「入口」索引）
# =====================================================================
# 为什么改主键：原 LOCK_POLICY 以「入口函数名」为主键、以 SERIAL/SCAN/…
# 为类别，表里没有一列写着「护哪个资源」。结果锁与资源对不上号——同一份
# 缓存被两把不同的外层锁「恰好」覆盖，新增一条访问路径就会静默漏锁。
#
# 判断顺序（对每个数据问三句，再决定要不要锁）：
#   ① 被哪种执行体承载？（事件循环 / 线程池 / 进程池 worker / SSE 常驻线程）
#   ② 在哪个作用域共享？（进程内堆 / 跨进程文件 / 每请求局部 / 只读）
#   ③ 存在跨执行体的**写-写**或**读-改-写**吗？（纯只读共享不需要锁）
#
# 锁的作用域必须与资源的作用域同级：
#   进程内堆对象 → threading.Lock / RLock
#   跨进程       → OS 层（SQLite WAL / fcntl / multiprocessing.Lock）
#                  ⚠ threading.Lock 跨进程**无效但看起来有效**，是最大陷阱
#   事件循环内   → asyncio.Lock，且临界区不得跨 await
#
# 消除共享优先于加锁：CTdxAPI._tdx_data 原是类变量（进程级共享），改为
# 每请求线程局部注入后，AppEngine._stock_analysis_lock 与 AppChart.
# _ENGINE_LOCK 一并删除——两把锁护的是同一个根因。
# 完整论述见 Docs/chan_lock_design_v5.md。
# ═══════════════════════════════════════════════════════════════════════

# 资源名 → (作用域, 保护手段, 访问者, 说明)
SHARED_RESOURCE_REGISTRY = {
    "stocks_analysis_cache": (
        "进程内", "AppData._stocks_cache_lock (RLock)", "REST / SSE",
        "股票分析结果 LRU（含 dual_main/dual_sub 结构化键）+ 股票下窗 CChan "
        "运行时缓存。读写/淘汰/失效全部经 app_data.cache_* / "
        "stocks_sub_cache_*，每个入口各自持锁。"),
    "futures_analysis_cache": (
        "进程内", "AppData._futures_cache_lock (RLock)", "SSE 写 / REST 读、清",
        "期货下窗 CChan。独立成锁：访问者是 SSE 常驻线程（高频写、生命周期"
        "以分钟计），不应与 REST 的毫秒级缓存操作抢同一把锁。"),
    "user_store_files": (
        "进程内 + 文件", "AppData._user_store_lock (RLock) + 原子落盘",
        "REST / worker（只读加载）",
        "标注 text_annotation.json、选点 saved_point.csv、last_code_freq.json、"
        "float_mc_cache.json、zxg.blk。都是短耗时读-改-写，合并为一把锁消除"
        "跨锁顺序死锁；统一走 safe_write_json_file / _atomic_write_text。"),
    "scan_pool": (
        "进程内", "AppScanPool._scan_pool_lock", "REST",
        "ProcessPool 单例 + _active_scans 引用计数。护的是「父进程的池对象」，"
        "不是 worker 之间的共享——后者归 OS 管。"),
    "scan_tasks.db": (
        "跨进程", "SQLite WAL + busy_timeout（OS 文件锁）",
        "REST / ProcessPool worker",
        "批量扫描结果。跨进程资源**不能**用 threading.Lock 保护，真正生效的"
        "是 SQLite 自身的 WAL 与写重试。"),
    "refresh_status": (
        "进程内", "AppRefresh._refresh_state_lock", "REST / 刷新工作线程",
        "刷新进度字典；running 的「检查 + 置位」必须在锁内完成（CAS）。"),
    "xdxr_cache": (
        "进程内", "DataAPI.TdxAPI._xdxr_lock", "REST / worker（各进程独立）",
        "除权除息缓存，DataAPI 内部，与本层正交。"),
    "tq_session_registry": (
        "进程内", "DataAPI.TqSdkCSSESource._ACTIVE_SOURCES_LOCK / 实例 _lock",
        "SSE / REST（close_all）",
        "TqApi 活跃源注册表与单源记录缓存，DataAPI 内部，与本层正交。"),
    # ── 已消除的共享（无需锁；登记在此以防回潮）──────────────────
    "CTdxAPI._tdx_data": (
        "每请求局部", "无需锁（tdx_data_context 线程局部注入）",
        "REST / worker",
        "原为类变量、进程级共享，是 _stock_analysis_lock + _ENGINE_LOCK 的"
        "唯一根因。改为每请求注入后数据全程是调用方局部变量，CChan 构建"
        "天然线程安全。"),
    "CChan / TqApi / CTqSdkSession": (
        "每连接局部", "无需锁", "SSE",
        "SSE 每连接独立 TqApi + CChan + 记录缓存（session_context 绑定）。"
        "注意这不代表 SSE 线程与 REST 线程隔离——app_data 仍然共享。"),
    "app_config / vipdoc .day / FREQ_SEC_MAP": (
        "只读共享", "无需锁（无写者）", "全部",
        "启动后只读。判断标准是「有没有写者」，不是「是不是共享」。"),
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
    # 共享资源登记表（按资源索引）
    "SHARED_RESOURCE_REGISTRY",
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
    "load_float_mc_cache", "fetch_float_mc_from_tencent",
    "update_float_mc_cache",
    # 刷新（AppRefresh）
    "load_stock_names_from_cache_file", "refresh_stock_names",
    "load_pe_ttm_cache", "get_pe_ttm",
    "get_index_belong", "refresh_status", "refresh_stock_names_async",
    # 市场量能（AppAMO）
    "call_amo",
    # 标注（AppChart）
    "get_annotations", "handle_annotation_action",
]

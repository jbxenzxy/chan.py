# -*- coding: utf-8 -*-
"""
阶段 2.6：函数级迁移映射 —— 目标归属定义 + 同步校验器
=====================================================================
本文件是「函数级映射」的单一事实源（机器可读部分）：

  TARGET_FUNCS   App/AppEngine.py 全部顶层函数 → 目标层
  TARGET_STATES  全部模块级状态 → 收敛目标
  TARGET_CLASSES 顶层类 → 处置
  TARGET_ROUTES  api_server.py 路由 → 目标（阶段 3a 输入）

校验（默认模式）：
  ① 完备性：代码中每个顶层函数/状态都有归属（无孤儿）
  ② 无幽灵：每个归属项在代码中存在
  ③ 行号漂移：JSON 中的行区间与当前代码一致（代码改了映射未更新→FAIL）
  ④ 一致性：Test/function_map.json 与本文件+当前代码同步

用法：
  python Test/func_map_check.py            # 校验（run_all 注册组件）
  python Test/func_map_check.py --update   # 重新生成 Test/function_map.json
"""
import json
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

from Test.func_map_analyzer import analyze

# 映射 JSON 与校验脚本同目录（Test/），均为测试相关交付件
MAP_JSON = os.path.join(TEST_DIR, "function_map.json")

# ═══════════════════════════════════════════════════════════════════
# 目标层代号（与设计文档 4.5 / 6.1 / 8.3-8.11 对应）
# ═══════════════════════════════════════════════════════════════════
LAYERS = {
    "FE":       ("FrontAPI.py",            "API 层：SSE 原生异步生成器（阶段 3b）+ 路由（阶段 3a）"),
    "ORCH_E":   ("App/AppOrch.py 消费侧",   "分析引擎：analyze_stock 编排 / 结果提取 / 实时快照"),
    "ORCH_F":   ("App/AppOrch.py 获取侧",   "数据拉取与注入：外部源刷新 / 期货 TqApi 初始化"),
    "ORCH_S":   ("App/AppOrch.py 扫描",     "ScannerService：批量扫描 / 预筛 / 进度"),
    "ORCH_C":   ("App/AppOrch.py 公共",     "跨分区公共工具"),
    "DATA":     ("App/AppData.py",          "业务数据层：缓存 / 持久化 / 标注 / 自选股"),
    "ELTDX":    ("DataAPI/ElTdxAPI.py",     "盘后下载（设计 8.8 明确的函数族）"),
    "DAPI":     ("DataAPI/ 抽象层",         "数据源抽象 / 元数据（阶段 5 提升）"),
    "TDXHY":    ("App/tdxhy_mapping_data.py", "行业映射读取接口（阶段 5 随数据文件同迁 App/，设计 8.8）"),
    "CFG":      ("App/AppConfig + ChanConfig", "配置中心（路径常量 / 算法参数构造）"),
    "RETIRE":   ("下线（阶段 9/10）",       "ChartHandler/遗留入口/死代码，验收通过后一次性删除"),
}

# 处置阶段（迁移发生在哪个阶段）
PHASE_OF = {
    "FE": "3a/3b", "ORCH_E": "3", "ORCH_F": "3/5", "ORCH_S": "3/7",
    "ORCH_C": "3", "DATA": "4", "ELTDX": "5", "DAPI": "5", "TDXHY": "5",
    "CFG": "2→4", "RETIRE": "9/10",
}

# ═══════════════════════════════════════════════════════════════════
# ① 函数 → 目标（61 项，须与代码一一对应）
#    值：(层代号, 说明)
# ═══════════════════════════════════════════════════════════════════
TARGET_FUNCS = {
    # ── 盘后下载族 → DataAPI/ElTdxAPI.py（设计 8.8 点名 8 函数 + 下载专用工具，
    #    阶段 5 已物理迁入 ElTdxAPI，check/build_map 扫描 ElTdxAPI.py 合并）──
    "_tdx_day_record":        ("ELTDX", "日线 .day 记录构造（纯）"),
    "_tdx_min_record":        ("ELTDX", "分钟 .lc1/.lc5 记录构造（纯）"),
    "_date_to_int":           ("ELTDX", "日期→pytdx int（仅下载族调用）"),
    "_date_to_min_packed":    ("ELTDX", "分钟打包 int（仅 _download_min_kline 调用）"),
    "_ensure_dir":            ("ELTDX", "安全 mkdir（仅下载族调用）"),
    "_download_day_kline":    ("ELTDX", "日线下载（纯，client 注入）"),
    "_download_min_kline":    ("ELTDX", "分钟下载（纯，client 注入）"),
    "_download_task":         ("ELTDX", "下载任务体（读 _download_state/_download_lock）"),
    "_start_download":        ("ELTDX", "启动下载线程（ElTdxAPI 实现；AppEngine 兼容壳已随阶段 8 瘦身删除）"),
    "_stop_download":         ("ELTDX", "中止下载（ElTdxAPI 实现；AppEngine 兼容壳已随阶段 8 瘦身删除）"),
    "_get_download_status":   ("ELTDX", "下载进度查询（ElTdxAPI 实现；AppEngine 兼容壳已随阶段 8 瘦身删除）"),
    "collect_codes_from_vipdoc": ("ELTDX", "从 vipdoc 收集代码（公开API，不含下划线前缀）"),
    "_collect_codes_from_vipdoc": ("ELTDX", "✓5 兼容壳 → ElTdxAPI.collect_codes_from_vipdoc"),

    # ── 消费侧：指标计算（纯函数，波次 1 先行）──
    "ema":                        ("ORCH_E", "EMA 指标（纯）"),
    "calculate_macd":             ("ORCH_E", "MACD 指标（纯，3 处调用）"),
    "_inherit_macd_for_preview_bar": ("ORCH_E", "预览 K 线 MACD 继承（纯）"),

    # ── 消费侧：周期/日期/代码 工具 ──
    "_get_kl_type":               ("ORCH_E", "freq→KL_TYPE（纯，6 处调用，波次 1）"),
    "_get_freq_label":            ("ORCH_E", "freq→中文标签（纯，4 处调用；阶段 5 可提升 DataAPI 元数据）"),
    "_get_date_fmt":              ("ORCH_E", "freq→日期格式（7 处调用，被依赖最多；读 INTRADAY_FREQS/SUBSECOND_FREQS）"),

    # ── 消费侧：缠论结构计算 ──
    "_find_left_shoulder_time":   ("ORCH_E", "左肩时间查找（手动选点用）"),
    "_bi_overlap_range":          ("ORCH_E", "笔与中枢区间重叠（纯）"),
    "_calc_zs_confirm_edt_from_bis": ("ORCH_E", "中枢确认时间（纯，4 处调用）"),
    "_calc_futures_white_hline":  ("ORCH_E", "期货白线（阶段 8 兼容壳 → App/AppSSE.py）"),
    "_make_chan_config":          ("CFG",    "CChanConfig 构造（纯；算法参数归 ChanConfig.py）"),

    # ── 消费侧：核心分析链 ──
    "_analyze_stock_internal":    ("ORCH_E", "股票分析核心（440 行：拉取→注入→CChan→提取）"),
    "_analyze_futures_internal":  ("ORCH_E", "期货分析核心（阶段 8 兼容壳 → App/AppSSE.py）"),
    "_extract_main_level_data":   ("ORCH_E", "主级别结果提取（357 行）"),
    "_extract_sub_level_data":    ("ORCH_E", "子级别结果提取（254 行）"),
    "_extract_realtime_snapshot": ("ORCH_E", "实时快照提取（阶段 8 兼容壳 → App/AppSSE.py）"),
    "analyze_stock":              ("ORCH_E", "统一分析入口（AppOrch 已有壳；无状态可双通道复用）"),
    "compute_red_range_zs":       ("ORCH_E", "红框中枢计算（阶段 8 兼容壳 → App/AppChart.py）"),
    "stock_manual_select_point":  ("ORCH_E", "股票手动选点（阶段 8 兼容壳 → App/AppChart.py）"),
    "futures_manual_select_point": ("ORCH_E", "期货手动选点（阶段 8 兼容壳 → App/AppSSE.py）"),

    # ── 获取侧 ──
    "_fetch_names_from_sina_once":  ("ORCH_F", "新浪批量拉名（纯，HTTP 注入点）"),
    "_refresh_stock_names":         ("ORCH_F", "全市场名称刷新（220 行；写 _stock_names_cache；AppOrch 已有壳）"),
    "_refresh_pe_ttm":              ("ORCH_F", "PE 刷新（akshare；写缓存文件）"),
    "_fetch_index_belong_from_akshare": ("ORCH_F", "行业归属拉取（写 _index_belong_cache）"),
    "_fetch_float_mc_from_tencent": ("ORCH_F", "腾讯流通市值拉取（纯）"),
    "init_chan_symbol":             ("ORCH_F", "TqApi 合约初始化（阶段 8 兼容壳 → App/AppSSE.py）"),

    # ── 扫描 ──
    "_quick_prefilter_pass":        ("ORCH_S", "扫描预筛（读 _stock_names_cache + 流通市值）"),
    "_debug_read_page_index_stocks": ("ORCH_S", "板块成分读取（扫描调试/页面索引用）"),

    # ── 公共工具 ──
    "_send_windows_notification":   ("ORCH_C", "Windows 通知（ChartHandler 下载/扫描完成用）"),

    # ── AppData：名称/PE/归属/市值 缓存族 ──
    "_load_stock_names_from_cache_file": ("DATA", "✓4 兼容壳 → app_data（AppOrch 已直连）"),
    "_safe_write_json_file":        ("DATA", "✓4 兼容壳 → App/AppData.safe_write_json_file"),
    "_load_pe_ttm_cache":           ("DATA", "✓4 兼容壳 → app_data（AppOrch 已直连）"),
    "_get_pe_ttm":                  ("DATA", "✓4 兼容壳 → app_data（AppOrch 已直连）"),
    "_get_index_belong":            ("DATA", "✓4 兼容壳 → app_data（AppOrch 已直连）"),
    "_load_float_mc_cache":         ("DATA", "✓4 兼容壳 → app_data（AppOrch 已直连）"),
    "_update_float_mc_cache":       ("DATA", "✓4 兼容壳 → app_data（AppOrch 已直连）"),
    "_get_float_mc_from_cache":     ("DATA", "✓4 兼容壳 → app_data"),

    # ── AppData：统一缓存三件套 ──
    "_cache_put":                   ("DATA", "✓4 兼容壳 → app_data.cache_put（LRU）"),
    "_cache_get":                   ("DATA", "✓4 兼容壳 → app_data.cache_get"),
    "_cache_remove":                ("DATA", "✓4 兼容壳 → app_data.cache_remove"),

    # ── AppData：选点持久化 ──
    "_save_point_time":             ("DATA", "✓4 兼容壳 → app_data（引擎内部手动选点仍调用）"),

    # ── AppData：上次代码/周期 + 标注族（阶段 8 瘦身：随功能域迁移删除）──

    # ── DataAPI / 行业映射 ──
    "read_tdxhy_l2_indices":        ("TDXHY", "二级行业指数读取（阶段 5 随数据文件迁 App/）"),
    "read_tdxhy_l3_indices":        ("TDXHY", "三级行业指数读取（同上）"),

    # ── 配置 ──
    # _verify_config_consistency 已随 my_chan_main 下线（阶段 10.1 删除）

    # ── 下线（阶段 10.1：my_chan_main.py 已删除，RETIRE 项全部物理移除）──
}

# ═══════════════════════════════════════════════════════════════════
# ② 状态 → 收敛目标（56 项）
# ═══════════════════════════════════════════════════════════════════
TARGET_STATES = {
    # 阶段 2 已中心化的别名（常量=app_config 派生；保留只读直至下线，_verify_config_consistency 守护）

    # 文件路径常量 → AppConfig
    # 配置路径别名：阶段 4 已全部删除（CFG 单源直读 app_config.<属性>）
    # _STOCK_NAMES_CACHE_FILE/_STOCK_PE_TTM_FILE/_FLOAT_MC_CACHE_FILE/
    # SAVED_POINT_FILE/ANNOTATIONS_FILE 等 12 个别名随阶段 4 清零，
    # 由 test_phase4_guards 的 _FORBIDDEN_PATH_ALIASES 守卫「不得复活」。
    "SAVED_POINT_COLUMNS":     ("CFG", "选点表列定义（= App/AppData.SAVED_POINT_COLUMNS 同值别名）"),

    # 业务缓存 → ✓4 已收敛：别名 = app_data 实例字段（共享同一对象，身份校验见 phase4 守护）
    "_stock_names_cache":  ("DATA", "✓4 名称缓存（= app_data._names）"),
    "_pe_ttm_cache":       ("DATA", "✓4 PE 缓存（= app_data._pe）"),
    "_index_belong_cache": ("DATA", "✓4 归属缓存（= app_data._belong）"),
    "_stocks_analysis_cache": ("DATA", "✓4 分析结果 LRU（= app_data._stocks_analysis_cache）"),
    "_futures_analysis_cache": ("DATA", "✓4 期货分析缓存（= app_data._futures_analysis_cache）"),
    "_cache_lock":         ("DATA", "✓4 缓存锁（= app_data._cache_lock）"),
    "_saved_point_times":  ("DATA", "✓4 选点表内存态（= app_data._saved_point_times）"),

    # 获取侧状态
    "_refresh_status":        ("ORCH_F", "刷新进度（3 读）→ 获取侧状态对象"),
    "_AKSHARE_EXCHANGE_MAP":  ("ORCH_F", "akshare 交易所映射（获取侧常量）"),
    "_AKSHARE_INDEX_MAP":     ("ORCH_F", "akshare 指数映射（获取侧常量）"),

    # 扫描状态 → ScannerService 类字段（api_server 跨模块直写，迁移时一并改路由调用）
    "_scan_lock":       ("ORCH_S", "扫描锁（api_server:491 直取；AppOrch:488 已暴露）→ ScannerService"),
    "_scan_aborted":    ("ORCH_S", "中止旗（api_server 直写）→ ScannerService"),
    "_scan_start_time": ("ORCH_S", "扫描起点时间（api_server 直写）→ ScannerService"),
    "_page_index_code": ("ORCH_S", "页面索引代码 → ScannerService"),
    "_scan_skip_log":   ("ORCH_S", "跳过日志（api_server:498 直写）→ ScannerService"),

    # 盘后下载状态 → ElTdxAPI 类字段
    "_download_state": ("ELTDX", "下载状态 dict（4 函数共用）→ ElTdxAPI 类字段"),
    "_download_lock":  ("ELTDX", "下载锁 → ElTdxAPI 类字段"),
    "_ELTDX_AVAILABLE": ("ELTDX", "eltdx 可用旗（阶段 5 兼容壳别名 → ElTdxAPI 模块级）"),
    "TdxClient":        ("ELTDX", "eltdx 客户端类（阶段 5 兼容壳别名 → ElTdxAPI 模块级）"),

    # 消费侧常量
    "FREQ_TO_COL":     ("ORCH_E", "freq→选点列（6 读）→ 消费侧常量"),
    "INTRADAY_FREQS":  ("ORCH_E", "日内周期集（2 读）"),
    "SUBSECOND_FREQS": ("ORCH_E", "秒级周期集"),
    "_SUB_FREQ_MAP":   ("ORCH_E", "子级别映射（2 读）"),
    "_FUTURES_DUAL_FREQ_MAP": ("ORCH_E", "期货双窗口映射"),
    "TIME_TRUNCATE_CONFIG":   ("ORCH_E", "数据截断配置（→ ChanConfig/参数化）"),
    "FULL_DATA_MODE":  ("ORCH_E", "全量模式开关（→ ChanConfig）"),
    "FORWARD_ADJUST_ENABLED": ("ORCH_E", "前复权开关（ChartHandler 2 处；→ ChanConfig）"),
    "DEBUG_COLD_START_START_DATE": ("ORCH_E", "调试冷启动起（→ ChanConfig 调试参数）"),
    "DEBUG_COLD_START_END_DATE":   ("ORCH_E", "调试冷启动止（→ ChanConfig 调试参数）"),
    "HK_CODE_PREFIX":  ("ORCH_E", "港股前缀（_get_stock_market_code 用）"),
    "DS_CODE_PREFIX":  ("ORCH_E", "科创板前缀（同上）"),
    "TDX_MARKET_MAP":  ("ORCH_E", "市场代码映射（ChartHandler 1 处；阶段 5 提升 DataAPI 元数据候选）"),

    # 锁与引擎
    "_stock_analysis_lock": ("RETIRE", "旧引擎锁（ChartHandler 2 处；AppOrch._ENGINE_LOCK 已接替 → 随 3b 下线）"),

    # SSE 调试旗 → FrontAPI（随 3b SSE 生成器迁移）
    "_SSE_DEBUG": ("FE", "SSE 调试旗（ChartHandler 32 处 / init_chan_symbol 1 处）→ FrontAPI 常量"),

    # 下线（阶段 10.1：my_chan_main.py 已删除，RETIRE 项全部物理移除）
    "SCRIPT_DIR":    ("RETIRE", "ChartHandler 静态服务用（随 do_GET 3a 删除）"),
    "SYMBOL_CODE":   ("RETIRE", "main() 默认代码（随 main 下线；api_server:1074 启动引用先参数化）"),
}

# ═══════════════════════════════════════════════════════════════════
# ③ 顶层类 → 处置
# ═══════════════════════════════════════════════════════════════════
TARGET_CLASSES = {
    "ThreadingHTTPServer": ("RETIRE", "旧 HTTP 服务器别名（2 行；随 ChartHandler 3b 后删）"),
    "ChartHandler": ("FE", "遗留服务器 · 阶段 3a 已墓碑化：do_GET/do_POST 删除（844 行）改回 410 Gone；"
                          "_handle_sse_stream_dual/single 已于 3b-2 拆除（灰度通过后随 legacy 桥接下线）；"
                          "send_json_response/log_message/handle_one_request 随类保留"),
}

# ═══════════════════════════════════════════════════════════════════
# ④ api_server.py → FrontAPI/AppOrch（阶段 3a 已完成 · 状态记录）
# 31 条路由已迁入 FrontAPI.py（单一路由源）；api_server.py 已于 3b-2 后
# 删除（原兼容壳退役）；历史绕锁 3 处（L185/L206/L472）改走持锁漏斗
# （AppOrch.LOCK_POLICY · SERIAL）。
# ═══════════════════════════════════════════════════════════════════
TARGET_ROUTES = {
    "_sse_generator":  ("RETIRE", "✓3a 已迁 FrontAPI，3b-2 已拆除（legacy 桥接下线）"),
    "_json_response":  ("FE", "✓3a 已迁 FrontAPI"),
    "_SSEMockWfile":   ("RETIRE", "✓3a 已迁 FrontAPI，3b-2 已拆除"),
    "_SSEMockHandler": ("RETIRE", "✓3a 已迁 FrontAPI，3b-2 已拆除"),
    "app":             ("FE", "✓3a 单一 app 实例 → FrontAPI"),
    "router":          ("FE", "✓3a 单一 router → FrontAPI"),
    # 29 条 REST/SSE 路由：✓3a 已全部迁入 FrontAPI.py，命名经 RESTful 整理统一
    # （历史绕锁 3 处改走持锁漏斗；tdx 双入口合并为 download/start 单 POST；
    #  chan_chart.html 书签兼容重定向已删除；v5 定稿：read/save 统一、scan 归 stocks）
    "api_stocks_analyze": ("FE", "✓REST GET /api/stocks/{code}/analyze → AppOrch.call_analysis（SERIAL 持锁）"),
    "api_stocks_select_point": ("FE", "✓REST POST /api/stocks/{code}/select/point → AppOrch.call_manual_select_point（SERIAL 持锁）"),
    "api_stocks_red_range": ("FE", "✓REST GET /api/stocks/{code}/red-range → AppOrch.call_compute_red_range_zs（SERIAL 持锁）"),
    "api_stocks_delete_point": ("FE", "✓REST DELETE /api/stocks/{code}/delete/point → AppOrch.clear_saved_point"),
    "api_search": ("FE", "✓REST GET /api/search → FrontAPI + AppOrch.search_stocks"),
    "api_stocks_scan_save_zxg": ("FE", "✓REST POST /api/stocks/scan/save/zxg → FrontAPI + AppData.save_to_zxg_blk"),
    "api_stocks_scan_read_candidates": ("FE", "✓REST GET /api/stocks/scan/read/candidates → FrontAPI + AppOrch 扫描"),
    "api_stocks_scan_set_index": ("FE", "✓REST PUT /api/stocks/scan/set/index → FrontAPI + AppOrch 扫描"),
    "api_stocks_scan_start": ("FE", "✓REST POST /api/stocks/scan/start → FrontAPI + ScannerService"),
    "api_stocks_scan_end": ("FE", "✓REST POST /api/stocks/scan/end → FrontAPI + ScannerService"),
    "api_stocks_scan_close": ("FE", "✓REST POST /api/stocks/scan/close → FrontAPI + AppData.cache_remove"),
    "api_stocks_scan_submit": ("FE", "✓REST POST /api/stocks/scan/submit → FrontAPI + ScannerService"),
    "api_stocks_scan_read_status": ("FE", "✓REST GET /api/stocks/scan/{task_id}/read/status → FrontAPI + ScannerService"),
    "api_stocks_scan_cancel_task": ("FE", "✓REST POST /api/stocks/scan/{task_id}/cancel → FrontAPI + ScannerService"),
    "api_futures_select_point": ("FE", "✓REST POST /api/futures/{symbol}/select/point → AppOrch.call_futures_manual_select_point（SERIAL 持锁）"),
    "api_futures_delete_point": ("FE", "✓REST DELETE /api/futures/{symbol}/delete/point → FrontAPI + AppData"),
    "api_futures_cleanup": ("FE", "✓REST POST /api/futures/cleanup → FrontAPI + AppOrch._cleanup_all_futures_data"),
    "api_futures_read_status": ("FE", "✓REST GET /api/futures/read/status → FrontAPI + AppOrch 获取侧状态"),
    "api_futures_read_config": ("FE", "✓REST GET /api/futures/read/config → FrontAPI + AppOrch"),
    "api_futures_read_stream": ("FE", "✓SSE GET /api/futures/read/stream → FrontAPI 原生异步生成器"),
    "api_stocks_refresh": ("FE", "✓REST POST /api/stocks/refresh → FrontAPI + AppOrch.refresh_stock_names_async"),
    "api_stocks_refresh_read_status": ("FE", "✓REST GET /api/stocks/refresh/read/status → FrontAPI + AppOrch.refresh_status"),
    "api_stocks_read_annotation": ("FE", "✓REST GET /api/stocks/{code}/read/annotation → FrontAPI + AppOrch.get_annotations"),
    "api_stocks_save_annotation": ("FE", "✓REST POST /api/stocks/{code}/save/annotation → FrontAPI + AppOrch.handle_annotation_action"),
    "api_stocks_scan_annotation": ("FE", "✓REST GET /api/stocks/scan/annotation → FrontAPI + AppOrch.get_annotated_codes"),
    "api_stocks_download_start": ("FE", "✓REST POST /api/stocks/download/start → FrontAPI + AppOrch.start_download_checked（GET+POST 双入口已合并）"),
    "api_stocks_download_read_status": ("FE", "✓REST GET /api/stocks/download/read/status → FrontAPI + AppOrch.get_download_status"),
    "api_stocks_download_cancel": ("FE", "✓REST POST /api/stocks/download/cancel → FrontAPI + AppOrch.stop_download"),
    "api_health": ("FE", "✓REST GET /api/health → FrontAPI 健康探活（meta 路由，v5 定稿补 api_ 前缀）"),
    "app_error_handler": ("FE", "✓3a AppError 统一处理器已迁 FrontAPI（兼容壳经别名共享）"),
}


# ═══════════════════════════════════════════════════════════════════
# 迁移波次：按调用拓扑分层（叶=1），同波次内被依赖多者优先
# ═══════════════════════════════════════════════════════════════════
def compute_waves(funcs):
    """wave(f) = 1 + max(wave(callee))。返回 {name: wave}"""
    waves = {}

    def w(name, stack=()):
        if name in waves:
            return waves[name]
        if name in stack:            # 环（含自递归）：按已访问最深+1
            return 0
        calls = [c for c in funcs[name]["calls"] if c in funcs]
        waves[name] = 1 + max((w(c, stack + (name,)) for c in calls), default=0)
        return waves[name]

    for n in funcs:
        w(n)
    return waves


def build_map():
    auto = analyze()
    funcs, states = auto["funcs"], auto["states"]

    # 阶段 5：同时扫描 DataAPI/ElTdxAPI.py，合并其函数与状态
    eltdx_path = os.path.join(REPO_ROOT, "DataAPI", "ElTdxAPI.py")
    if os.path.exists(eltdx_path):
        auto_eltdx = analyze(eltdx_path)
        funcs.update(auto_eltdx["funcs"])
        states.update(auto_eltdx["states"])
    waves = compute_waves(funcs)

    # 波次内排序：被依赖多者优先
    for name, info in funcs.items():
        info["wave"] = waves[name]
        info["target"] = TARGET_FUNCS[name][0] if name in TARGET_FUNCS else None
        info["note"] = TARGET_FUNCS[name][1] if name in TARGET_FUNCS else ""

    ordered = sorted(funcs.items(),
                     key=lambda kv: (kv[1]["wave"], -len(kv[1]["depended_by"]), kv[0]))

    states_out = {}
    for name, info in states.items():
        states_out[name] = {**info,
                            "target": TARGET_STATES[name][0] if name in TARGET_STATES else None,
                            "note": TARGET_STATES[name][1] if name in TARGET_STATES else "",
                            "readers": [n for n, i in funcs.items() if name in i["reads"]],
                            "writers": [n for n, i in funcs.items() if name in i["writes"]]}

    return {
        "phase": "2.6",
        "source_file": "App/AppEngine.py（api_server.py 与 my_chan_main.py 已删除，路由单源于 FrontAPI）",
        "layers": {k: {"home": v[0], "desc": v[1], "phase": PHASE_OF[k]}
                   for k, v in LAYERS.items()},
        "summary": {
            "n_funcs": len(funcs),
            "n_states": len(states),
            "n_classes": len(TARGET_CLASSES),
            "by_layer": _count_by(funcs, TARGET_FUNCS),
            "states_by_layer": _count_by(states, TARGET_STATES),
        },
        "migration_order": [{"order": i, "name": n, **{
            k: info[k] for k in ("wave", "target", "depended_by", "calls",
                                 "reads", "writes", "pure", "line_start", "line_end",
                                 "n_lines", "note")}}
                            for i, (n, info) in enumerate(ordered, 1)],
        "states": states_out,
        "classes": TARGET_CLASSES,
        "api_server_routes": TARGET_ROUTES,
    }


def _count_by(items, targets):
    from collections import Counter
    c = Counter(targets.get(n, ("?",))[0] for n in items)
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


# ═══════════════════════════════════════════════════════════════════
# 校验
# ═══════════════════════════════════════════════════════════════════
def check(update=False):
    failures = []
    auto = analyze()
    funcs, states = auto["funcs"], auto["states"]

    # 阶段 5：同时扫描 DataAPI/ElTdxAPI.py，合并其函数与状态
    eltdx_path = os.path.join(REPO_ROOT, "DataAPI", "ElTdxAPI.py")
    if os.path.exists(eltdx_path):
        auto_eltdx = analyze(eltdx_path)
        funcs.update(auto_eltdx["funcs"])
        states.update(auto_eltdx["states"])

    # ① 完备性：无孤儿
    orphan_f = sorted(set(funcs) - set(TARGET_FUNCS))
    orphan_s = sorted(set(states) - set(TARGET_STATES))
    if orphan_f:
        failures.append(f"函数未归属({len(orphan_f)}): {orphan_f}")
    if orphan_s:
        failures.append(f"状态未归属({len(orphan_s)}): {orphan_s}")

    # ② 无幽灵
    ghost_f = sorted(set(TARGET_FUNCS) - set(funcs))
    ghost_s = sorted(set(TARGET_STATES) - set(states))
    if ghost_f:
        failures.append(f"归属项已不存在于代码({len(ghost_f)}): {ghost_f}")
    if ghost_s:
        failures.append(f"状态归属项不存在({len(ghost_s)}): {ghost_s}")

    # ③ 与冻结 JSON 同步（--update 时先重生成再校验）
    if update:
        os.makedirs(TEST_DIR, exist_ok=True)
        with open(MAP_JSON, "w", encoding="utf-8") as f:
            json.dump(build_map(), f, ensure_ascii=False, indent=1)
        print(f"[UPDATED] {MAP_JSON}（{len(funcs)} 函数 / {len(states)} 状态）")

    if os.path.exists(MAP_JSON):
        frozen = json.load(open(MAP_JSON, encoding="utf-8"))
        drift = []
        for item in frozen.get("migration_order", []):
            f = funcs.get(item["name"])
            if f and (f["line_start"] != item["line_start"]
                      or f["line_end"] != item["line_end"]):
                drift.append(f"{item['name']}: JSON {item['line_start']}-{item['line_end']}"
                             f" vs 代码 {f['line_start']}-{f['line_end']}")
        if drift:
            failures.append(f"行号漂移({len(drift)}): {drift[:3]}… 请 --update 重新生成")
        if len(frozen.get("migration_order", [])) != len(funcs):
            failures.append("JSON 函数数与代码不一致（新增/删除函数未映射？）")
    else:
        failures.append(f"缺少 {MAP_JSON}（首次请 --update 生成）")

    # 输出
    if failures:
        print(f"[FAIL] 映射校验 {len(failures)} 项问题:")
        for x in failures:
            print("  -", x)
        return False
    print(f"[PASS] 映射校验: {len(funcs)} 函数 / {len(states)} 状态 / "
          f"{len(TARGET_CLASSES)} 类 / {len(TARGET_ROUTES)} 路由 全部归属且与代码同步")
    return True


if __name__ == "__main__":
    sys.exit(0 if check(update="--update" in sys.argv) else 1)

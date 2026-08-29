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
  （行号漂移与函数总数比对校验已下线：P2 清理后行号频繁变动，
   Test/function_map.json 仅作归档快照，不再做行号级比对）

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
    "DAPI":     ("DataAPI/ 抽象层",         "数据源抽象 / 元数据（阶段 5 提升）"),
    "TDXHY":    ("App/tdxhy_mapping_data.py", "行业映射读取接口（阶段 5 随数据文件同迁 App/，设计 8.8）"),
    "CFG":      ("App/AppConfig + ChanConfig", "配置中心（路径常量 / 算法参数构造）"),
    "RETIRE":   ("下线（阶段 9/10）",       "ChartHandler/遗留入口/死代码，验收通过后一次性删除"),
}

# 处置阶段（迁移发生在哪个阶段）
PHASE_OF = {
    "FE": "3a/3b", "ORCH_E": "3", "ORCH_F": "3/5", "ORCH_S": "3/7",
    "ORCH_C": "3", "DATA": "4", "DAPI": "5", "TDXHY": "5",
    "CFG": "2→4", "RETIRE": "9/10",
}

# ═══════════════════════════════════════════════════════════════════
# ① 函数 → 目标（48 项，须与代码一一对应）
#    值：(层代号, 说明)
# ═══════════════════════════════════════════════════════════════════
TARGET_FUNCS = {

    # vipdoc 代码收集（「盘后下载」功能已移除，collect_codes_from_vipdoc 归位
    # DataAPI/TdxAPI.py；AppEngine 兼容壳仍转发之）
    "_collect_codes_from_vipdoc": ("DAPI", "✓5 兼容壳 → DataAPI/TdxAPI.collect_codes_from_vipdoc"),

    # ── 消费侧：指标计算（P0-1c 已物理迁入 App/utils.py，不再属 AppEngine 映射）──

    # ── 消费侧：周期/日期/代码 工具（P0-1c 已物理迁入 App/utils.py，不再属 AppEngine 映射）──

    # ── 消费侧：缠论结构计算（P0-1c 已物理迁入 App/utils.py，不再属 AppEngine 映射）──

    # ── 消费侧：核心分析链 ──
    "_analyze_stock_internal":    ("ORCH_E", "股票分析核心（440 行：拉取→注入→CChan→提取）"),
    "_extract_main_level_data":   ("ORCH_E", "主级别结果提取（357 行）"),
    "_extract_sub_level_data":    ("ORCH_E", "子级别结果提取（254 行）"),
    "analyze_stock":              ("ORCH_E", "统一分析入口（AppOrch 已有壳；无状态可双通道复用）"),

    # ── 消费侧：股票双窗口（独立下窗） ──
    "_validate_stock_dual_pair":  ("ORCH_E", "双窗周期配对校验（非法返回错误串，调用方 4xx 拒绝）"),
    "_stock_dual_impl":           ("ORCH_E", "双窗 A/B 实现开关（环境变量，默认 independent）"),
    "_stocks_sub_dt_algo":        ("ORCH_E", "独立下窗截断边界（结束时间语义纯函数）"),
    "_build_sub_kl_times":        ("ORCH_E", "灰框对照表合成（上窗K线→下窗K线时间分桶，双指针）"),

    # ── 获取侧（P0-1a 已物理迁入 App/AppRefresh.py，不再属 AppEngine 映射）──

    # ── 扫描（P0-1b 已物理迁入 App/AppScan.py，不再属 AppEngine 映射）──

    # ── 公共工具（P1-3：_send_windows_notification 已迁 App/AppScan.py，不再属 AppEngine 映射）──

    # ── AppData：名称/PE/归属/市值 缓存族 ──
    "_load_stock_names_from_cache_file": ("DATA", "✓4 兼容壳 → app_data（AppOrch 已直连）"),
    "_safe_write_json_file":        ("DATA", "✓4 兼容壳 → App/AppData.safe_write_json_file"),
    "_get_pe_ttm":                  ("DATA", "✓4 兼容壳 → app_data（AppOrch 已直连）"),
    "_get_index_belong":            ("DATA", "✓4 兼容壳 → app_data（AppOrch 已直连）"),
    # P2：_load_float_mc_cache 兼容壳已随 AppRefresh 物理迁入删除，不再属 AppEngine 映射

    # ── AppData：统一缓存三件套 ──
    "_cache_put":                   ("DATA", "✓4 兼容壳 → app_data.cache_put（LRU）"),
    "_cache_get":                   ("DATA", "✓4 兼容壳 → app_data.cache_get"),

    # ── AppData：选点持久化（P2：_save_point_time 兼容壳已删除，AppData 直连）──

    # ── AppData：上次代码/周期 + 标注族（阶段 8 瘦身：随功能域迁移删除）──

    # ── DataAPI / 行业映射（P0-1b 已物理迁入 App/AppScan.py，不再属 AppEngine 映射）──

    # ── 配置 ──
    # _verify_config_consistency 已随 my_chan_main 下线（阶段 10.1 删除）

    # ── 下线（阶段 10.1：my_chan_main.py 已删除，RETIRE 项全部物理移除）──
}

# ═══════════════════════════════════════════════════════════════════
# ② 状态 → 收敛目标（51 项）
# ═══════════════════════════════════════════════════════════════════
TARGET_STATES = {
    # 阶段 2 已中心化的别名（常量=app_config 派生；保留只读直至下线，_verify_config_consistency 守护）

    # 文件路径常量 → AppConfig
    # 配置路径别名：阶段 4 已全部删除（CFG 单源直读 app_config.<属性>）
    # _STOCK_NAMES_CACHE_FILE/_STOCK_PE_TTM_FILE/_FLOAT_MC_CACHE_FILE/
    # SAVED_POINT_FILE/ANNOTATIONS_FILE 等 12 个别名随阶段 4 清零，
    # 由 test_phase4_guards 的 _FORBIDDEN_PATH_ALIASES 守卫「不得复活」。
    "SAVED_POINT_COLUMNS":     ("CFG", "选点表列定义（= App/AppData.SAVED_POINT_COLUMNS 同值别名）"),

    # 统一日志（P0-3：App/AppLog.py 框架，全项目共享）
    "log":                     ("ORCH_C", "统一日志 logger（P0-3 App/AppLog.py；get_logger(__name__)）"),

    # 业务缓存 → ✓4 已收敛：别名 = app_data 实例字段（共享同一对象，身份校验见 phase4 守护）
    "_stock_names_cache":  ("DATA", "✓4 名称缓存（= app_data._names）"),
    "_pe_ttm_cache":       ("DATA", "✓4 PE 缓存（= app_data._pe）"),
    "_index_belong_cache": ("DATA", "✓4 归属缓存（= app_data._belong）"),
    "_stocks_analysis_cache": ("DATA", "✓4 分析结果 LRU（= app_data._stocks_analysis_cache）"),
    # P1-3：_futures_analysis_cache 死别名已删除（AppEngine 不再持有，期货缓存仅经 app_data.futures_cache_*）
    "_cache_lock":         ("DATA", "✓4 缓存锁（= app_data._cache_lock）"),
    "_saved_point_times":  ("DATA", "✓4 选点表内存态（= app_data._saved_point_times）"),

    # 获取侧状态（P0-1a 已物理迁入 App/AppRefresh.py，不再属 AppEngine 映射）

    # 扫描状态（P0-1b 已物理迁入 App/AppScan.py，不再属 AppEngine 映射）

    # 消费侧常量
    "FREQ_TO_COL":     ("ORCH_E", "freq→选点列（6 读）→ 消费侧常量"),
    "_SUB_FREQ_MAP":   ("ORCH_E", "子级别映射（2 读）"),
    # INTRADAY_FREQS / SUBSECOND_FREQS / _FREQ_SEC_TO_KL / _FUTURES_DUAL_FREQ_MAP
    # （P0-1c 已物理迁入 App/utils.py，不再属 AppEngine 映射）
    "STOCKS_LOOKBACK_CONFIG": ("ORCH_E", "股票K线回看条数配置（→ ChanConfig/参数化）"),
    "FULL_DATA_MODE":  ("ORCH_E", "全量模式开关（→ ChanConfig）"),
    "FORWARD_ADJUST_ENABLED": ("ORCH_E", "前复权开关（ChartHandler 2 处；→ ChanConfig）"),
    "DEBUG_COLD_START_START_DATE": ("ORCH_E", "调试冷启动起（→ ChanConfig 调试参数）"),
    "DEBUG_COLD_START_END_DATE":   ("ORCH_E", "调试冷启动止（→ ChanConfig 调试参数）"),
    # P2：HK_CODE_PREFIX / DS_CODE_PREFIX / TDX_MARKET_MAP 死状态已删除
    #     （市场代码统一由 App/AppData.get_market_code 判定，不再属 AppEngine）
    "DUAL_SUB_FALLBACK_MIN": ("ORCH_E", "双窗下窗对齐不足降全量阈值（= app_config.dual_sub_fallback_min）"),
    "_STOCKS_DUAL_IMPL_ENV": ("ORCH_E", "双窗 A/B 实现开关环境变量名"),
    "_STOCKS_DUAL_PAIRS":    ("ORCH_E", "股票双窗配对空间（上窗周期→可选下窗周期集合）"),
    "_STOCKS_MAIN_PERIOD":   ("ORCH_E", "主级别单根K线覆盖时长映射"),
    "_STOCKS_EOD":           ("ORCH_E", "日期型K线当日结束时刻补齐偏移"),

    # 锁与引擎
    "_stock_analysis_lock": ("RETIRE", "旧引擎锁（ChartHandler 2 处；AppOrch._ENGINE_LOCK 已接替 → 随 3b 下线）"),

    # SSE 调试旗（P0-1c 已物理迁入 App/utils.py，不再属 AppEngine 映射）

    # 启动基础设施（活状态，P2 由 RETIRE 更正为活登记）
    "SCRIPT_DIR":    ("CFG", "引擎目录 sys.path 引导（AppEngine 顶部 import 前置），活状态"),
    "SYMBOL_CODE":   ("ORCH_E", "默认股票代码（FrontAPI /api/health 与启动日志实际读取），活状态"),
}

# ═══════════════════════════════════════════════════════════════════
# ③ 顶层类 → 处置
# ═══════════════════════════════════════════════════════════════════
TARGET_CLASSES = {
    # 旧 HTTP 服务器层（ChartHandler / ThreadingHTTPServer）已随迁移完成整体删除。
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
    "api_stocks_scan_close": ("FE", "✓REST POST /api/stocks/scan/close → FrontAPI + Scanner.clear_cache（仅关面板，P0-3 后不再清缓存）"),
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

    # ③ 归档快照（--update 时重生成 Test/function_map.json；
    #    行号漂移与函数总数比对校验已下线，见顶部 docstring）
    if update:
        os.makedirs(TEST_DIR, exist_ok=True)
        with open(MAP_JSON, "w", encoding="utf-8") as f:
            json.dump(build_map(), f, ensure_ascii=False, indent=1)
        print(f"[UPDATED] {MAP_JSON}（{len(funcs)} 函数 / {len(states)} 状态）")

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

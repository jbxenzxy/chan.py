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
import json
import time
import threading
import traceback

# my_chan_main 作为底层引擎（阶段 3 起逐步拆分吸收）
import my_chan_main as _m


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
_ENGINE_LOCK = threading.Lock()

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
    "ScannerService.scan_one":      ("SCAN", "扫描路径：引擎调用在全局 _scan_lock 内串行（基线继承，保护引擎缓存），锁外预处理/过滤保留 SCAN_CONCURRENCY 并发；不加 _ENGINE_LOCK"),
    "sse_futures_stream_single":    ("SELF_CONTAINED", "SSE 单窗口（FrontAPI）：每连接独立 TqApi+CChan，不触共享分析缓存"),
    "sse_futures_stream_dual":      ("SELF_CONTAINED", "SSE 双窗口（FrontAPI）：独立 TqApi+双 CChan，连接间隔离"),
}


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
    """统一的缠论分析入口 · 锁分类 RAW（无锁）

    ⚠ 本函数是引擎原始入口的薄封装，**并非无状态**：引擎内部维护模块级
    LRU 缓存（_stocks_analysis_cache）与名称/PE/市值等共享缓存，均非线程
    安全。此前的「无状态，可在线程池 / ProcessPool 复用」表述有误导。

    调用约定（LOCK_POLICY，见文件头）：
      - 串行分析路径（REST 交互式）→ 必须走 call_analysis / run_analysis
        （持 _ENGINE_LOCK），不得直调本函数；
      - 扫描路径（SCAN）→ ScannerService.scan_one 内部调用（全局
        _scan_lock 内串行引擎调用，锁外保留并发）；
      - SSE 期货路径（SELF_CONTAINED）→ 独立 CChan 会话，不触共享缓存。
    """
    return _m.analyze_stock(code, freq=freq, end_date=end_date,
                            cache_chan=cache_chan, dual=dual, step=step, sub_freq=sub_freq)


def call_manual_select_point(code, freq="d", bi_idx=-1):
    """股票手动选点 · SERIAL（持 _ENGINE_LOCK）

    原路由直连 m.stock_manual_select_point 绕锁（阶段 2 遗留问题 L185），
    阶段 3a 起统一走本漏斗：内部链路复用 analyze_stock 引擎与共享缓存。
    """
    with _ENGINE_LOCK:
        return _m.stock_manual_select_point(code, freq=freq, bi_idx=bi_idx)


def call_futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """期货手动选点 · SERIAL（持 _ENGINE_LOCK）

    内部走期货分析链路（_analyze_futures_internal，含期货缓存读写），
    归入串行分类，与股票侧共用引擎锁。
    """
    with _ENGINE_LOCK:
        return _m.futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)


def call_compute_red_range_zs(code, sub_freq="d", left_date="", right_date="", end_date=None):
    """红框中枢计算 · SERIAL（持 _ENGINE_LOCK）

    原路由直连 m.compute_red_range_zs 绕锁（阶段 2 遗留问题 L206），
    阶段 3a 起统一走本漏斗：内部复用 analyze_stock 引擎与共享缓存。
    """
    with _ENGINE_LOCK:
        return _m.compute_red_range_zs(code, sub_freq=sub_freq,
                                       left_date=left_date, right_date=right_date,
                                       end_date=end_date)


def stock_manual_select_point(code, freq="d", bi_idx=-1):
    """股票手动选点 · RAW（无锁原始入口）

    ⚠ 与 analyze_stock 同理并非无状态：内部走 analyze_stock 引擎链路与
    共享缓存。REST 调用方必须走 call_manual_select_point（持锁漏斗）；
    本签名保留供已按 SELF_CONTAINED 分类并自带会话隔离的路径使用。
    """
    return _m.stock_manual_select_point(code, freq=freq, bi_idx=bi_idx)


def futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """期货手动选点 · RAW（无锁原始入口）

    ⚠ 内部读写期货共享缓存，非线程安全。REST 调用方必须走
    call_futures_manual_select_point（持锁漏斗）。
    """
    return _m.futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)


def compute_red_range_zs(code, sub_freq="d", left_date="", right_date="", end_date=None):
    """红框中枢计算 · RAW（无锁原始入口）

    ⚠ 内部复用 analyze_stock 引擎与共享缓存。REST 调用方必须走
    call_compute_red_range_zs（持锁漏斗）。
    """
    return _m.compute_red_range_zs(code, sub_freq=sub_freq,
                                   left_date=left_date, right_date=right_date,
                                   end_date=end_date)


def extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None):
    """从实时行情快照中提取分析所需字段（供 SSE 路径使用）"""
    return _m._extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                         saved_selection_date, lightweight, klines)


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


def fetch_and_inject(code, freq="d", source="tdx", end_date=None, dual=False, step=None, sub_freq=None):
    """
    阶段 5：判断股票 / 期货 → 拉取 K 线 → 注入分析引擎。

    fetch 统一走 DataAPI 抽象层（替换阶段 4 前的直连模式）：
      - 数据源选择经 _get_data_source() 路由到对应 DataAPI 实现
      - 实际拉取仍委托 analyze_stock（其内部已通过 DataAPI 读取数据）
      - source 参数显式选择数据源（tdx / tqsdk），缺省自动检测

    锁分类 RAW（无锁）：委托 analyze_stock，共享引擎缓存，非线程安全。
    串行调用方须走 call_analysis / run_analysis。
    """
    api_module, is_futures = _get_data_source(code, source)
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

    # 期货/期指别名搜索（阶段 5：经 CTqSdkAPI 元数据接口）
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
# REST 服务函数（阶段 3a）
# 业务段从 api_server 路由下沉至此，路由层（FrontAPI）保持薄：
# 参数校验 + 调本层 + 响应组装。数据访问一律经 AppData（单向依赖）。
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


def get_annotated_codes(freq=""):
    """自选扫描：返回有标注的股票列表（/api/annotations_scan）"""
    from App.AppData import app_data
    return app_data.get_annotated_codes(freq)


def read_zxg_stocks():
    """读取自选股列表（/api/zxg_list）"""
    from App.AppData import app_data
    return app_data.read_zxg_stocks()


def zxg_save(codes):
    """保存勾选股票到通达信 + 同花顺自选股（/api/zxg_save，业务段下沉）

    codes: 逗号分隔字符串（与原路由入参一致）
    返回 (result_dict, status_code)。
    """
    from App.AppData import app_data

    codes_list = codes.split(",") if codes else []
    if not codes_list:
        return {"error": "codes为空"}, 400

    try:
        codes_raw = [c.strip() for c in codes_list]
        codes_ths = list(dict.fromkeys(codes_raw))

        # 通达信
        print(f"[保存] 通达信: 输入 {len(codes_raw)} 只, 代码={codes_raw}")
        tdx_added = app_data.save_to_zxg_blk(codes_raw)
        print(f"[保存] 通达信: 实际写入 {tdx_added} 只")

        # 同花顺
        ths_added = 0
        ths_msg = ""
        print(f"[保存] 同花顺: 输入 {len(codes_ths)} 只, 代码={codes_ths}")
        if _m._THS_CLOUD_AVAILABLE:
            try:
                cloud_result = _m.save_scan_to_ths_cloud(codes_ths)
                if "error" in cloud_result:
                    raise Exception(cloud_result["error"])
                ths_added = len(cloud_result.get("added", []))
                ths_msg = "ok"
                print(f"[保存] 同花顺: 新增{ths_added}, "
                      f"跳过{len(cloud_result.get('skipped',[]))}, "
                      f"失败{len(cloud_result.get('failed',[]))}")
            except Exception as e:
                err_str = str(e)
                if "登录状态失效" in err_str or "Cookie" in err_str:
                    ths_msg = "Cookie过期，请运行 ths_capture_cookie.py 重新获取"
                else:
                    ths_msg = f"云端同步失败: {err_str}"
                print(f"[保存] 同花顺: {ths_msg}")
        else:
            ths_msg = "App/ths_cloud_api.py 未找到，请确保 App/ 目录完整（阶段 2 已迁入 App/）"
            print(f"[保存] 同花顺: {ths_msg}")

        print(f"[保存] 汇总: 通达信={tdx_added}, 同花顺={ths_added}, msg={ths_msg}")
        return {
            "ok": True,
            "tdx_saved": tdx_added,
            "ths_saved": ths_added,
            "ths_msg": ths_msg,
        }, 200
    except Exception as exc:
        traceback.print_exc()
        return {"error": str(exc)}, 500


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


def start_download_checked(categories, day_start=None, min_start=None):
    """盘后下载启动 · 带前置检查（/api/tdx_download_start GET/POST 共用）

    categories: GET 传 JSON 字符串，POST 传 list（两形态统一在此归一）。
    返回 (result_dict, status_code)，语义与原路由一致（含 409 冲突码）。
    阶段 5：委托 DataAPI/ElTdxAPI（下载目录经 app_config.download_dir）。
    """
    from DataAPI import ElTdxAPI as _eltdx
    from App.AppConfig import app_config
    if not _eltdx._ELTDX_AVAILABLE:
        return {"error": "eltdx 未安装，请先 pip install eltdx"}, 400
    if isinstance(categories, str):
        try:
            categories = json.loads(categories)
        except Exception:
            return {"error": "categories 参数格式错误"}, 400
    if not categories:
        return {"error": "请选择要下载的数据类型"}, 400
    ok, msg = _eltdx._start_download(app_config.download_dir, categories,
                                     day_start=day_start or None,
                                     min_start=min_start or None)
    return {"ok": ok, "message": msg}, (200 if ok else 409)


# ═══════════════════════════════════════════════════════════════════════
# 名称 / PE / 流通市值 / 选点 / 上次查看 缓存
# （阶段 4：纯数据读写一律直连 AppData；获取侧刷新函数仍走引擎壳）
# ═══════════════════════════════════════════════════════════════════════

def load_stock_names_from_cache_file():
    """加载股票名称缓存（AppData 直连，阶段 4）"""
    from App.AppData import app_data
    return app_data.load_stock_names_from_cache_file()


def refresh_stock_names():
    """刷新股票名称（阻塞）—— 获取侧逻辑，阶段 5 前保留引擎实现"""
    return _m._refresh_stock_names()


def load_float_mc_cache():
    """加载流通市值缓存（AppData 直连，阶段 4）"""
    from App.AppData import app_data
    return app_data.load_float_mc_cache()


def fetch_float_mc_from_tencent(stock_list):
    """从腾讯接口获取流通市值（获取侧）"""
    return _m._fetch_float_mc_from_tencent(stock_list)


def update_float_mc_cache(mv_dict):
    """更新流通市值缓存（AppData 直连，阶段 4）"""
    from App.AppData import app_data
    return app_data.update_float_mc_cache(mv_dict)


def load_pe_ttm_cache():
    """加载 PE-TTM 缓存（AppData 直连，阶段 4）"""
    from App.AppData import app_data
    return app_data.load_pe_ttm_cache()


def get_pe_ttm(market, code):
    """获取 PE-TTM（AppData 直连，阶段 4）"""
    from App.AppData import app_data
    return app_data.get_pe_ttm(market, code)


def get_index_belong(market, code):
    """获取指数归属（AppData 直连，阶段 4）"""
    from App.AppData import app_data
    return app_data.get_index_belong(market, code)


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
# 阶段 5：职责内聚 DataAPI/ElTdxAPI.py，此处为薄封装（委托目标 ElTdxAPI）。
# ═══════════════════════════════════════════════════════════════════════

def eltdx_available():
    """eltdx 盘后下载引擎是否可用"""
    from DataAPI import ElTdxAPI as _eltdx
    return _eltdx._ELTDX_AVAILABLE


def download_dir():
    """盘后下载数据保存目录（阶段 4 配置中心化：app_config.download_dir）"""
    from App.AppConfig import app_config
    return app_config.download_dir


def start_download(categories, day_start=None, min_start=None):
    """启动盘后下载"""
    from DataAPI import ElTdxAPI as _eltdx
    from App.AppConfig import app_config
    return _eltdx._start_download(app_config.download_dir, categories,
                                  day_start=day_start or None,
                                  min_start=min_start or None)


def get_download_status():
    """盘后下载进度"""
    from DataAPI import ElTdxAPI as _eltdx
    return _eltdx._get_download_status()


def stop_download():
    """停止盘后下载"""
    from DataAPI import ElTdxAPI as _eltdx
    return _eltdx._stop_download()


# ═══════════════════════════════════════════════════════════════════════
# 期货
# ═══════════════════════════════════════════════════════════════════════

def futures_cleanup():
    """清理所有期货数据"""
    return _m._cleanup_all_futures_data()


def get_futures_aliases():
    """期货别名映射（阶段 5：经 CTqSdkAPI 元数据接口）"""
    from DataAPI.TqSdkAPI import CTqSdkAPI
    return CTqSdkAPI.FUTURES_ALIASES


def get_futures_name(full_code):
    """期货名称"""
    if _m._get_futures_name:
        return _m._get_futures_name(full_code)
    return full_code


def tq_available():
    """天勤数据源是否可用"""
    return _m.TQ_AVAILABLE


def futures_config():
    """期货可用周期列表（阶段 5：经 CTqSdkAPI 元数据接口）"""
    try:
        from DataAPI.TqSdkAPI import CTqSdkAPI
        return {"supported_freqs": CTqSdkAPI.SUPPORTED_FREQS,
                "disabled_freqs": CTqSdkAPI.DISABLED_FREQS}
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
            if _m._float_mc_loaded:
                print(f"[流通市值] 本地缓存已加载 {len(_m._float_mc_cache)} 只")
            try:
                t_mc = time.time()
                mv_dict = _m._fetch_float_mc_from_tencent(merged)
                if mv_dict:
                    total_stocks = len(merged)
                    got_count = len(mv_dict)
                    miss_count = total_stocks - got_count
                    _m._update_float_mc_cache(mv_dict)
                    if miss_count == 0:
                        print(f"[流通市值] 腾讯接口 获取全部 {got_count} 只 (耗时{time.time()-t_mc:.1f}s)")
                    else:
                        print(f"[流通市值] 腾讯接口 获取 {got_count}/{total_stocks} 只，{miss_count} 只未获取到 (耗时{time.time()-t_mc:.1f}s)")
                else:
                    print("[流通市值] 腾讯接口未返回数据，使用本地缓存")
            except Exception as e:
                print(f"[流通市值] 腾讯接口异常: {type(e).__name__}: {e}，使用本地缓存")

        # 后端预过滤
        pre_filtered = merged
        pre_skip_count = 0
        pre_skip_log = []
        try:
            t_pre_all = time.time()
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

        return {
            "stocks": pre_filtered,
            "sources": sources,
            "total": len(pre_filtered),
            "pre_skipped": pre_skip_count,
            "errors": errors if errors else None,
        }

    # ── 单只扫描 ─────────────────────────────────────────────────────
    def scan_one(self, code, freq="d", prefix="", recent="1", source="zxg", mode=""):
        """扫描单只股票 · 锁分类 SCAN

        锁语义（阶段 2.6 基线继承，本阶段零改动）：引擎调用 analyze_stock
        在全局 _scan_lock（单实例、非按票）内串行执行——保护非线程安全
        的引擎缓存不被并发写；锁外的预处理/结果过滤仍按 SCAN_CONCURRENCY
        并发，故扫描吞吐主要依赖非引擎阶段的并行。
        """
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
                print(f"[耗时-扫描] {code} 分析失败: {result['error']}, 耗时{t_analyze:.3f}s")
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
                    t_total = time.time() - t_scan_start
                    print(f"[耗时-扫描-底分型] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 是底分型")
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
                    t_total = time.time() - t_scan_start
                    print(f"[耗时-扫描-底分型] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 不是底分型")
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
                t_total = time.time() - t_scan_start
                print(f"[耗时-扫描-均线] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 分类:{ma_category}")
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
                t_total = time.time() - t_scan_start
                print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 有买卖点")
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
                t_total = time.time() - t_scan_start
                print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 无买卖点")
                return {"code": code, "buy_points": [], "sell_points": []}

        except Exception as exc:
            _m._scan_skip_log.append(f"{code} - 异常: {exc}")
            t_total = time.time() - t_scan_start
            print(f"[耗时-扫描] {code} 异常: {exc}, 总耗时{t_total:.3f}s")
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

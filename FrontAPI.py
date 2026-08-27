# -*- coding: utf-8 -*-
"""
FrontAPI.py —— FastAPI 统一入口 · 面向前端 · REST + SSE
=========================================================================
单一路由源：全部 REST 路由与 SSE 流端点收敛于本文件，路由保持薄
（参数校验 + run_in_threadpool(orch.*) + 响应组装），业务段在 App/AppOrch.py。
引擎调用一律经 AppOrch 的 call_* 漏斗（锁分类见 AppOrch.LOCK_POLICY），
本文件不直连引擎分析函数（Test/test_phase3_guards.py G3 守护）。
SSE 实时流为同步生成器（每连接一条常驻线程，Starlette 在线程池中迭代，
阻塞调用不占事件循环）；生成器实现位于 App/AppSSE.py，数据源抽象
CSSESource/CTqSdkSession 位于 DataAPI/TqSdkCSSESource.py，本文件仅 re-export。

锁分类（AppOrch.LOCK_POLICY）：
    SERIAL         串行分析（call_* 漏斗持 _ENGINE_LOCK）
    SCAN           并行扫描（Scanner.scan_one，_scan_lock 防同票重入）
    SELF_CONTAINED SSE 流（每连接独立会话，不加锁）

启动：
    python FrontAPI.py                     # 推荐入口（端口/地址走 App/AppConfig.py）
    uvicorn FrontAPI:app --port 18081      # 等效
"""
import sys
import os
import time
import traceback
from contextlib import asynccontextmanager

# ── 仓库根目录引导（App/ 包位于仓库根）────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Python 3.10 兼容垫片（原项目使用 typing.Self，需 3.11+；3.11+ 下自动跳过）
import typing
if not hasattr(typing, "Self"):
    try:
        from typing_extensions import Self
        typing.Self = Self
    except ImportError:
        pass

from fastapi import FastAPI, Query, HTTPException, Body, APIRouter, Request, Path
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.concurrency import run_in_threadpool

from App.AppConfig import app_config
from App import AppOrch as orch
from App.AppOrch import AppError  # 领域异常统一在服务层定义

from App.AppLog import get_logger
# 统一 logger 名（AppLog 格式已不含 [%(name)s]，name 不再显示于日志）
log = get_logger("FrontAPI")

# 分析引擎层：引擎入口一律走 orch.* 漏斗，此处仅读取常量/纯函数/SSE 调试旗
from App import AppEngine as m  # noqa: F401
# SSE 实时流 / 期货功能域：SSE 内部实现位于 App/AppSSE.py
from App import AppSSE as _sse  # noqa: F401
# SSE 数据源抽象：tqsdk 仅在 DataAPI 层可见；API 层经 AppSSE re-export 消费，
# 不直连 DataAPI.TqSdkCSSESource（CSSESource 供注入用例，close_all 供退出回收）
from App.AppSSE import (  # noqa: F401
    CSSESource,
    close_all,
)


# ═══════════════════════════════════════════════════════════════════════
# 应用组装
# ═══════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app):
    """应用生命周期：关闭时优雅回收 ProcessPool 与活跃 CTqSdkSession。

    未回收时 Ctrl+C 会让 worker 进程在 call_queue.get() 阻塞处收到
    KeyboardInterrupt 并打印 traceback（实测 SpawnProcess-1）。
    CTqSdkSession 同理：服务器退出时事件循环即将关闭，必须在此显式
    _close_all_sources() 关闭所有 TqApi（同步生成器 finally 虽可靠，
    但服务器退出时生成器可能仍在 wait_update 阻塞，主动回收更确定），
    否则进程退出时 TqApi 内部挂起任务被销毁，触发
    「Task was destroyed but it is pending!」级联。
    """
    yield
    try:
        from App.AppScanPool import shutdown as scan_pool_shutdown
        scan_pool_shutdown()
    except Exception as exc:  # noqa: BLE001 —— 关闭兜底不阻断退出
        log.info(f"[FrontAPI] 关闭 ProcessPool 异常: {type(exc).__name__}: {exc}")
    try:
        # run_in_threadpool：close_all 等待各生成器线程完成
        # api.close()（最迟 0.1s），不能阻塞事件循环
        await run_in_threadpool(close_all)
    except Exception as exc:  # noqa: BLE001 —— 关闭兜底不阻断退出
        log.info(f"[FrontAPI] 关闭 CTqSdkSession 异常: {type(exc).__name__}: {exc}")


app = FastAPI(title="缠论分析 API", version="1.2.0", docs_url="/docs",
              lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_request(request: Request, call_next):
    """仅记录请求异常；正常请求不再打印 → / ← 起止日志（避免刷屏）。"""
    try:
        return await call_next(request)
    except Exception:
        log.error("%s %s 异常", request.method, request.url.path)
        raise


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """领域异常 → 统一日志 + 统一 JSON（状态码读取异常自身）"""
    log.info(f"领域异常 {exc.__class__.__name__}: {exc} ({request.url.path})")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.__class__.__name__, "detail": str(exc)},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """未预期异常兜底：500 + 日志，不向客户端泄漏内部细节"""
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"error": "InternalError", "detail": "服务器内部错误"},
    )


@app.get("/api/health", tags=["meta"])
async def api_health():
    """健康检查：入口标识 + 配置摘要（凭据打码）+ 周期映射（前端共享）"""
    # 周期映射以后端为单一事实源（CTqSdkAPI.FREQ_SEC_MAP 接口属性），
    # 前端经 /api/health 拉取；取数经期货域元数据出口，API 层不直连
    # DataAPI.TqSdkAPI。
    _freq_sec_map = _sse.get_futures_freq_sec_map()
    return {
        "status": "ok",
        "entry": "FrontAPI",
        "version": app.version,
        "symbol_code": m.SYMBOL_CODE,
        "config": app_config.as_dict(redact=True),
        "freq_sec_map": _freq_sec_map,
    }


# ═══════════════════════════════════════════════════════════════════════
# 辅助：统一 JSON 响应
# ═══════════════════════════════════════════════════════════════════════

def _json_response(data, status_code: int = 200):
    """统一 JSON 返回（带无缓存响应头）"""
    return JSONResponse(
        content=data,
        status_code=status_code,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# SSE · 数据源抽象
# CSSESourceClosed / CSSESource / CTqSdkSession / 注册表的实现位于
# DataAPI/TqSdkCSSESource.py（tqsdk 仅在 DataAPI 层可见）；
# FrontAPI 仅引用自身消费的符号（CSSESource 注入用例 / close_all 退出回收），
# CSSESourceClosed 与 CTqSdkSession 由 AppSSE/测试直连原模块，不在此转口。
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
# SSE · 同步生成器（每连接一条常驻线程）
# 生成器实现位于 App/AppSSE.py，此处仅 re-export，
# 保持 FrontAPI.sse_futures_stream_single/dual 对测试（test_sse_gray /
# test_sse_sequence / test_phase3_guards）与路由可见，入口保持「薄」。
# 数据源抽象（CSSESource/CTqSdkSession）在 DataAPI/TqSdkCSSESource.py；
# 业务函数（init_chan_symbol/_extract_realtime_snapshot/_sse_frame 等）
# 在 App/AppSSE.py —— 生成器按「src 协议 + 服务层业务函数」消费，不触碰 tqsdk。
# ═══════════════════════════════════════════════════════════════════════
from App.AppSSE import (  # noqa: F401
    sse_futures_stream_single,
    sse_futures_stream_dual,
    _sse_frame,
)



# ═══════════════════════════════════════════════════════════════════════
# REST 路由（单一路由源）
# 路由保持薄：参数校验 + run_in_threadpool(orch.*) + 响应组装。
# 引擎调用全部走 AppOrch 漏斗（锁分类见 AppOrch.LOCK_POLICY）。
# ═══════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════
# API 命名规范（定稿 · 新增接口请遵守）
# ═══════════════════════════════════════════════════════════════════════
# 一、函数/路径统一结构：api + [stocks|futures] + 动词 + 名字
#     股票独有带 stocks；期货独有带 futures；共用不带前缀。
# 二、动词统一约定：
#     读取只读数据      →  read        （不用 get）
#     写入/持久化       →  save        （不用 write）
#     触发类动作        →  start/end/close/cancel/submit/refresh/cleanup
#     选点/删点         →  select / delete
#     设置              →  set
# 三、动作动词精确语义：
#     end     正常结束一个进行中的过程/会话（如扫描正常收尾）
#     close   关闭面板/界面、清理缓存（UI 层）
#     cancel  中途终止一个进行中的任务（协作式，worker 检查标志后停止）
#     abort   强制/紧急中断（立即停止、丢弃未完成工作；当前无使用场景，不保留端点）
# ═══════════════════════════════════════════════════════════════════════
router = APIRouter()


# ── 路由 — 核心数据 ───────────────────────────────────────────────────

@router.get("/api/stocks/{code}/analyze")
async def api_stocks_analyze(
    code: str = Path(...),
    freq: str = Query("d"),
    end_date: str = Query(None),
    step: str = Query(None),
    dual: bool = Query(False),
    sub_freq: str = Query(None),
):
    """获取股票缠论分析数据（orch.call_analysis · SERIAL 持锁）"""
    if not code:
        raise HTTPException(status_code=400, detail="请输入股票代码")
    try:
        result = await run_in_threadpool(orch.call_analysis, code, freq=freq,
                                         end_date=end_date, dual=dual,
                                         step=step, sub_freq=sub_freq)
    except AppError:
        raise  # 领域异常：交给统一异常处理器
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")

    if "error" in result:
        return _json_response(result, 400)

    # 持久化（与原有逻辑一致：非复盘、非双窗口下窗、非期货）
    if not end_date and result.get("meta", {}).get("market") != "futures":
        await run_in_threadpool(orch.save_last_code_freq, code, freq)

    return _json_response(result)


@router.post("/api/stocks/{code}/select/point")
async def api_stocks_select_point(
    code: str = Path(...),
    freq: str = Query("d"),
    bi_idx: str = Query("-1"),
    dual: bool = Query(False),
    sub_freq: str = Query(None),
    main_freq: str = Query(None),
):
    """股票手动选点（orch.call_manual_select_point · SERIAL 持锁）

    双窗选点：dual=1 时 freq=双击所在窗口周期，
    main_freq=上窗周期（下窗选点必传），sub_freq=下窗周期。
    """
    if not code or bi_idx == "-1":
        raise HTTPException(status_code=400, detail="缺少必要参数 code 或 bi_idx")
    try:
        result = await run_in_threadpool(orch.call_manual_select_point, code,
                                         freq=freq, bi_idx=bi_idx,
                                         dual=dual, sub_freq=sub_freq,
                                         main_freq=main_freq)
    except AppError:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


@router.get("/api/stocks/{code}/red-range")
async def api_stocks_red_range(
    code: str = Path(...),
    freq: str = Query("d"),
    left_date: str = Query(...),
    right_date: str = Query(...),
    end_date: str = Query(None),
):
    """红框中枢计算（orch.call_compute_red_range_zs · SERIAL 持锁）"""
    if not code or not left_date or not right_date:
        raise HTTPException(status_code=400, detail="参数错误: code/left_date/right_date 不能为空")
    try:
        result = await run_in_threadpool(orch.call_compute_red_range_zs, code,
                                         sub_freq=freq, left_date=left_date,
                                         right_date=right_date, end_date=end_date)
    except AppError:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


# ── 路由 — 搜索 ───────────────────────────────────────────────────────

@router.get("/api/search")
async def api_search(q: str = Query(...)):
    """股票代码/名称/拼音搜索（orch.search_stocks，业务段已下沉）"""
    if not q:
        raise HTTPException(status_code=400, detail="请输入搜索关键词")
    result = await run_in_threadpool(orch.search_stocks, q)
    return _json_response(result)


# ── 路由 — 扫描 ───────────────────────────────────────────────────────

@router.get("/api/stocks/scan/read/candidates")
async def api_stocks_scan_read_candidates(source: str = Query("zxg")):
    """返回股票列表（支持逗号分隔多来源；orch.Scanner.stock_list）"""
    result = await run_in_threadpool(orch.scanner.stock_list, source)
    return _json_response(result)


@router.put("/api/stocks/scan/set/index")
async def api_stocks_scan_set_index(code: str = Query("")):
    """设置当前板块指数代码"""
    result = await run_in_threadpool(orch.scanner.set_page_index_code, code)
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


@router.post("/api/stocks/scan/start")
async def api_stocks_scan_start():
    """新一轮扫描开始"""
    result = await run_in_threadpool(orch.scanner.start)
    return _json_response(result)


@router.post("/api/stocks/scan/end")
async def api_stocks_scan_end():
    """扫描结束"""
    result = await run_in_threadpool(orch.scanner.end)
    return _json_response(result)


@router.post("/api/stocks/scan/close")
async def api_stocks_scan_close():
    """关闭扫描面板"""
    result = await run_in_threadpool(orch.scanner.clear_cache)
    return _json_response(result)


# ── 路由 — 批量扫描异步化（ProcessPool）───────────────────────────────
# 批量扫描统一走 ProcessPool：任务提交返回 task_id，前端轮询
# /api/stocks/scan/{task_id}/read/status 获取进度与结果（扫描结果经
# SQLite AppScanStore 跨进程共享）。
# RESTful 分层路由：/api/stocks/scan/submit ·
# /api/stocks/scan/{task_id}/read/status · /api/stocks/scan/{task_id}/cancel；
# 请求体平铺字段 → Swagger 自动生成字段 schema 与默认值。

@router.post("/api/stocks/scan/submit")
async def api_stocks_scan_submit(stocks: list = Body(...),
                               freq: str = Body("d"),
                               mode: str = Body(""),
                               recent: str = Body("1"),
                               source: str = Body("zxg")):
    """提交批量扫描 → {task_id, total}（ProcessPool 异步执行）

    body: {stocks: [{code, prefix, _source}], freq, mode, recent, source}
    返回 task_id；进度经 /api/stocks/scan/{task_id}/read/status 轮询。
    """
    if not stocks:
        return _json_response({"error": "股票列表为空"}, 400)
    result = await run_in_threadpool(
        orch.scanner.submit_batch_scan, stocks, freq, mode, recent, source)
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


@router.get("/api/stocks/scan/{task_id}/read/status")
async def api_stocks_scan_read_status(task_id: str = Path(...),
                               since: int = Query(0, ge=0)):
    """批量扫描状态轮询（前端每 1-2s 调用，增量读取）

    since: 游标，返回 seq >= since 的结果行（含首行）；前端按
    row.seq + 1 推进，避免全量回传 O(n²)。
    返回 {task_id, status, total, completed, results, error}；
    任务不存在返回 404。
    """
    result = await run_in_threadpool(
        orch.scanner.get_batch_scan_status, task_id, since)
    if result is None:
        return _json_response({"error": f"任务不存在: {task_id}"}, 404)
    return _json_response(result)


@router.post("/api/stocks/scan/{task_id}/cancel")
async def api_stocks_scan_cancel_task(task_id: str = Path(...)):
    """中止批量扫描任务（worker 每票前检查中止标志）"""
    result = await run_in_threadpool(orch.scanner.abort_batch_scan, task_id)
    if "error" in result:
        return _json_response(result, 404)
    return _json_response(result)


# ── 路由 — 市场量能 ──────────────────────────────────────────────────

@router.get("/api/amo/read")
async def api_amo_read(
    start_date: str = Query(...),
    end_date: str = Query(...),
):
    """获取市场量能数据（orch.call_amo · SERIAL 持锁）

    数据源仅 TDX 本地指数日线（sh000001 + sz399106 成交额相加），
    无任何兜底；start_date/end_date 为 K 线页面「视口」左右边界日期。
    """
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="参数错误: start_date/end_date 不能为空")
    try:
        result = await run_in_threadpool(orch.call_amo, start_date, end_date)
    except AppError:
        raise  # 领域异常：交给统一异常处理器
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


# ── 路由 — 自选股保存 ─────────────────────────────────────────────────

@router.post("/api/stocks/scan/save/zxg")
async def api_stocks_scan_save_zxg(codes: str = Query("")):
    """保存勾选的股票到通达信+同花顺自选股（orch.zxg_save，业务段已下沉）"""
    data, status = await run_in_threadpool(orch.zxg_save, codes)
    return _json_response(data, status)


# ── 路由 — 选点管理 ───────────────────────────────────────────────────

@router.delete("/api/stocks/{code}/delete/point")
async def api_stocks_delete_point(
    code: str = Path(...),
    freq: str = Query("d"),
):
    """清除股票选点"""
    if not code:
        return _json_response({"error": "缺少code参数"}, 400)
    result = await run_in_threadpool(orch.clear_saved_point, code, freq)
    return _json_response(result)


# ── 路由 — 期货/期指 ──────────────────────────────────────────────────

@router.post("/api/futures/{symbol}/select/point")
async def api_futures_select_point(
    symbol: str = Path(...),
    freq: str = Query("15s"),
    bi_idx: str = Query("-1"),
):
    """期货手动选点（orch.call_futures_manual_select_point · SERIAL 持锁）
    期货选点统一走领域异常，AppError 由统一异常处理器捕获"""
    if not symbol or bi_idx == "-1":
        return _json_response({"error": "缺少必要参数 symbol 或 bi_idx"}, 400)
    try:
        result = await run_in_threadpool(orch.call_futures_manual_select_point, symbol,
                                         freq=freq, bi_idx=bi_idx)
    except AppError:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    return _json_response(result)


@router.delete("/api/futures/{symbol}/delete/point")
async def api_futures_delete_point(symbol: str = Path(...), freq: str = Query("15s")):
    """期货清除选点"""
    if not symbol:
        return _json_response({"error": "缺少symbol参数"}, 400)
    result = await run_in_threadpool(orch.futures_clear_saved_point, symbol, freq)
    return _json_response(result)


@router.post("/api/futures/cleanup")
async def api_futures_cleanup():
    """清理所有期货数据（期指切股票：先回收 TqApi 连接，再清空缓存）

    顺序关键：先 close_all（经 AppSSE re-export，实现在 DataAPI.TqSdkCSSESource）
    设置 _closed 旗并等待各生成器线程完成 api.close()（天勤要求 close 在
    wait_update 返回后由生成器线程调用），再清空期货 K 线缓存/选点记录，
    避免残留连接继续写缓存。
    run_in_threadpool 包裹：close_all 等待各生成器线程完成
    api.close()（最迟 0.1s），不能阻塞事件循环。
    """
    await run_in_threadpool(close_all)
    await run_in_threadpool(orch.futures_cleanup)
    return _json_response({"ok": True})


@router.get("/api/futures/read/status")
async def api_futures_read_status():
    """期货状态"""
    return _json_response({"ok": True, "architecture": "self-contained"})


@router.get("/api/futures/read/config")
async def api_futures_read_config():
    """期货可用周期列表"""
    result = await run_in_threadpool(orch.get_futures_freqs)
    return _json_response(result)


# ── 路由 — SSE 实时推送（期货） ────────────────────────────────────────

@router.get("/api/futures/read/stream")
def api_futures_read_stream(
    symbol: str = Query(...),
    freq: str = Query("15s"),
    start_time: str = Query(None),
    dual: bool = Query(False),
    sub_freq: str = Query(None),
    end_time: str = Query(None),
):
    """SSE 实时推送（期货单/双窗口）· 同步生成器

    每个 SSE 连接 = 1 条常驻线程（同步生成器 + StreamingResponse）。
    本端点返回同步生成器 sse_futures_stream_single/dual，Starlette 检测到
    同步迭代器后自动在线程池中迭代（iterate_in_threadpool），阻塞调用
    （connect/wait_update/step_load 等）天然发生在线程内，不占事件循环。
    事件协议基线见 Test/test_sse_gray.py。

    end_time: 复盘终点（软断开）。传入时建 chan 截断到该边界，end_time
    之后不再推进（复盘看历史不被实时拉最新）；不传为实时流/选点起点流。
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少symbol参数")

    if not orch.tq_available():
        raise HTTPException(status_code=503, detail="天勤数据源不可用")

    if dual:
        gen = sse_futures_stream_dual(symbol, freq, sub_freq, start_time, end_time)
    else:
        gen = sse_futures_stream_single(symbol, freq, start_time, end_time)

    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── 路由 — 股票名称刷新 ───────────────────────────────────────────────

@router.post("/api/stocks/refresh")
async def api_stocks_refresh():
    """启动股票名称刷新"""
    result = await run_in_threadpool(orch.refresh_stock_names_async)
    return _json_response(result)


@router.get("/api/stocks/refresh/read/status")
async def api_stocks_refresh_read_status():
    """查询刷新状态"""
    result = await run_in_threadpool(orch.refresh_status)
    return _json_response(result)


# ── 路由 — 标注 ───────────────────────────────────────────────────────

@router.get("/api/stocks/{code}/read/annotation")
async def api_stocks_read_annotation(code: str = Path(...), freq: str = Query("d")):
    """获取标注数据"""
    if not code:
        return _json_response({"error": "缺少code参数"}, 400)
    anns = await run_in_threadpool(orch.get_annotations, code, freq)
    return _json_response({"annotations": anns, "code": code, "freq": freq})


@router.post("/api/stocks/{code}/save/annotation")
async def api_stocks_save_annotation(code: str = Path(...), body: dict = Body(...)):
    """标注增删改（orch.handle_annotation_action，40 行校验逻辑已下沉）"""
    data, status = await run_in_threadpool(orch.handle_annotation_action, body)
    return _json_response(data, status)


@router.get("/api/stocks/scan/annotation")
async def api_stocks_scan_annotation(freq: str = Query("")):
    """自选扫描：返回有标注的股票列表"""
    codes = await run_in_threadpool(orch.get_annotated_codes, freq)
    return _json_response({"codes": codes, "total": len(codes)})


# ── 路由 — 盘后下载 ───────────────────────────────────────────────────

@router.post("/api/stocks/download/start")
async def api_stocks_download_start(body: dict = Body(...)):
    """盘后下载启动（POST 入口）"""
    categories = body.get("categories") or []
    day_start = body.get("day_start") or ""
    min_start = body.get("min_start") or ""
    data, status = await run_in_threadpool(orch.start_download_checked,
                                           categories, day_start, min_start)
    return _json_response(data, status)


@router.get("/api/stocks/download/read/status")
async def api_stocks_download_read_status():
    """盘后下载进度"""
    status = await run_in_threadpool(orch.get_download_status)
    return _json_response(status)


@router.post("/api/stocks/download/cancel")
async def api_stocks_download_cancel():
    """盘后下载停止"""
    ok, msg = await run_in_threadpool(orch.stop_download)
    return _json_response({"ok": ok, "message": msg})


# ── 路由挂载（单一路由源）────────────────────────────────────────────
app.include_router(router)


# ── 静态资源：Frontend/ ──────────────────────────────────────────────
_frontend_dir = app_config.frontend_dir
if os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
else:
    log.warning(f"[警告] Frontend/ 目录不存在 ({_frontend_dir})，回退到 OUTPUT_DIR 静态挂载")
    app.mount("/", StaticFiles(directory=m.OUTPUT_DIR, html=True), name="static")


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import socket
    import uvicorn

    HOST = app_config.host
    PORT = app_config.port

    # ── 端口占用检测 ──────────────────────────────────────────────────
    try:
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _probe.bind((HOST, PORT))
        _probe.close()
    except OSError:
        log.error(f"[错误] 端口 {PORT} 已被占用！")
        log.error("[错误] 可能原因：FrontAPI.py 或其他服务仍在运行。")
        log.info(f"[解决] Windows 可执行: netstat -ano | findstr {PORT} 查看占用 PID，")
        log.info("[解决] 然后执行: taskkill /PID <PID> /F 结束旧进程。")
        sys.exit(1)

    last_code, last_freq = orch.load_last_code_freq()  # AppOrch 漏斗读 AppData
    if last_code:
        log.info(f"[信息] 恢复上次: {last_code} (周期: {last_freq})")
    else:
        log.info(f"[信息] 使用默认股票: {m.SYMBOL_CODE}")

    log.info(f"[信息] FastAPI 服务器启动: http://{HOST}:{PORT}")
    log.info(f"[信息] API 文档:   http://{HOST}:{PORT}/docs")
    log.info(f"[信息] K线图表页:  http://{HOST}:{PORT}/")
    log.info(f"[信息] 健康检查:   http://{HOST}:{PORT}/api/health")
    log.info("[信息] SSE:       /api/futures_stream（同步生成器，每连接一条常驻线程）")
    # Uvicorn 默认把日志写 stderr（终端标红）。传 log_config=None 让其
    # 复用 AppLog.py 已配置的 root (stdout)，startup/error 日志改为 stdout，
    # 与应用日志同色；access 日志已由 access_log=False 关闭。
    uvicorn.run(app, host=HOST, port=PORT, log_config=None,
                log_level="info", access_log=False)


# -*- coding: utf-8 -*-
"""
FrontAPI.py —— FastAPI 统一入口 · 面向前端 · REST + SSE
=========================================================================
单一路由源：全部 REST 路由与 SSE 流端点收敛于本文件，路由保持薄
（参数校验 + run_in_threadpool(orch.*) + 响应组装），业务段在 App/AppOrch.py。
引擎调用一律经 AppOrch 的 call_* 漏斗，本文件不直连引擎分析函数
（Test/test_phase3_guards.py G3 守护）。
SSE 实时流为同步生成器（每连接一条常驻线程，Starlette 在线程池中迭代，
阻塞调用不占事件循环）；生成器实现位于 App/AppSSE.py，数据源抽象
CSSESource/CTqSdkSession 位于 DataAPI/TqSdkCSSESource.py，本文件仅 re-export。

执行体（决定了哪些资源是共享的）：
    事件循环线程   async def 且内部无阻塞的路由（/api/health 等）
    线程池        绝大多数 REST（run_in_threadpool，默认 40 线程）
    进程池 worker  批量扫描（/scan/submit → spawn worker，内存隔离）
    SSE 常驻线程   /api/futures/read/stream（同步生成器，每连接 1 条）

锁：路由层不持锁。共享资源的锁由各持有者按资源持有，登记表见
AppOrch.SHARED_RESOURCE_REGISTRY（按资源索引，不是按入口索引）。
分析路径已免锁——CChan 构建经 tdx_data_context 每请求线程局部注入。

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
# 引擎调用全部走 AppOrch 漏斗（共享资源登记表见 AppOrch.SHARED_RESOURCE_REGISTRY）。
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
    """获取股票缠论分析数据（orch.call_analysis）"""
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

    # 持久化最近代码/周期（仅非复盘路径持久化；本路由 analyze 到不了期货——
    # analyze_stock 入口对期货代码已明确拒绝，market!="futures" 属历史防御）
    #
    # 审计 X6（多页语义，非并发缺陷）：这是**进程级单例**，多页浏览不同标
    # 的时互相覆盖，最终只记住最后 analyze 的那一个。
    #   · 并发安全性：已达标。save_last_code_freq 持 _user_store_lock +
    #     原子落盘，多页同时写不会写坏文件——**不需要再加锁**。
    #   · 语义：名称是「上次查看」，多页下"最后看的那个"胜出**符合**该语义，
    #     故不改行为。用户若感知为"服务端记住了我看的股票"，那是对单页时代
    #     的习惯推断，不是本功能的承诺。
    #   · 若要改成"每个标签页各记各的"，正确位置是**前端 localStorage**
    #     （它本来就是标签页级状态），而不是在后端加任何锁或分片——把会话级
    #     状态放在进程级才是 X6 与 X1/X3 共同的根因（指导书 §0.2 模式 A）。
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
    """股票手动选点（orch.call_manual_select_point）

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
    """红框中枢计算（orch.call_compute_red_range_zs）"""
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
async def api_stocks_scan_read_candidates(source: str = Query("zxg"),
                                          page_index_code: str = Query(""),
                                          scan_token: str = Query("")):
    """返回股票列表（支持逗号分隔多来源；orch.Scanner.stock_list）

    page_index_code：**本次请求**要用的板块指数代码（成分股来源）。
    审计 X3：必须随请求传入，不能让后端去读进程级全局——多网页下
    A 页选沪深300、B 页选中证500 时，读全局会让两页都按最后设置的
    那个扫，静默扫错一整批成分股。
    """
    result = await run_in_threadpool(orch.scanner.stock_list, source,
                                     page_index_code or None, scan_token or None)
    return _json_response(result)


@router.put("/api/stocks/scan/set/index")
async def api_stocks_scan_set_index(code: str = Query("")):
    """【已废弃】设置板块指数代码（全局兜底，仅供旧客户端兼容）

    新路径：把代码作为 page_index_code 参数传给
    GET /api/stocks/scan/read/candidates，随请求走、不落全局（审计 X3）。
    """
    result = await run_in_threadpool(orch.scanner.set_page_index_code, code)
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


@router.post("/api/stocks/scan/start")
async def api_stocks_scan_start(page_index_code: str = Query("")):
    """新一轮扫描开始 → {ok, scan_token}

    scan_token 是本次扫描的私有上下文标识，后续 end / submit 需带上它，
    两个页面同时扫描才不会互相覆盖（审计 X3）。
    """
    result = await run_in_threadpool(orch.scanner.start, page_index_code or None)
    return _json_response(result)


@router.post("/api/stocks/scan/end")
async def api_stocks_scan_end(scan_token: str = Query("")):
    """扫描结束（按 scan_token 结算那一次扫描）"""
    result = await run_in_threadpool(orch.scanner.end, scan_token or None)
    return _json_response(result)


@router.post("/api/stocks/scan/close")
async def api_stocks_scan_close():
    """关闭扫描面板（仅回收面板自身状态，不清空共享分析缓存）"""
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
                               source: str = Body("zxg"),
                               scan_token: str = Body("")):
    """提交批量扫描 → {task_id, total}（ProcessPool 异步执行）

    body: {stocks: [{code, prefix, _source}], freq, mode, recent, source, scan_token}
    返回 task_id；进度经 /api/stocks/scan/{task_id}/read/status 轮询。
    scan_token 用于把收割线程的跳过记录写回发起它的那次扫描（审计 X3）。
    """
    if not stocks:
        return _json_response({"error": "股票列表为空"}, 400)
    result = await run_in_threadpool(
        orch.scanner.submit_batch_scan, stocks, freq, mode, recent, source,
        scan_token or None)
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
    """获取市场量能数据（orch.call_amo）

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
    """期货手动选点（orch.call_futures_manual_select_point）
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
    """清理期货残留数据（期指切股票时由前端 fire-and-forget 调用）

    【审计 X1 · P0 修复】**不再**调用 close_all()。

    原实现先 close_all() 关闭**所有**活跃 CTqSdkSession——那是主机级操作，
    一个页面切走期货会把其余所有期货页面的 SSE 流一并掐断，并清空它们的
    K线缓存、下窗 chan 与选点。多网页下这是**每次必错**的确定性故障。

    修复后的职责划分（作用域纪律，指导书维度 0.2 模式 B）：
      · **本页面的会话**（TqApi 连接、本连接 K线记录、本连接下窗 chan）
        → 由本页面 SSE 生成器的 finally 自行回收（单窗 :660 / 双窗 :1181-1185）。
        前端在调用本端点前已 disconnectRealtime()，生成器随即走 finally。
      · **本端点**只做孤儿清扫：若此刻已无任何活跃期货会话，清掉残留缓存；
        若仍有会话（别的页面在看，或本页 SSE 尚未走完 finally），**跳过**
        以免误伤——那些会话会自行清理。

    close_all() 现仅供 lifespan 服务器退出钩子使用。
    run_in_threadpool 包裹：清扫涉及文件/锁等待，不能阻塞事件循环。
    """
    result = await run_in_threadpool(orch.futures_cleanup)
    return _json_response({
        "ok": True,
        # swept=False 表示"有页面仍在看期货，已跳过清扫"——属正常，非错误
        "swept": result.get("swept", False),
        "active_sessions": result.get("active_sessions", 0),
    })


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


# ── 路由挂载（单一路由源）────────────────────────────────────────────
app.include_router(router)


# ── 静态资源：Frontend/ ──────────────────────────────────────────────
# P0-4 修复：删除 OUTPUT_DIR 回退分支。AppEngine 无 OUTPUT_DIR（该符号
# 仅存于历史注释与守护测试），Frontend/ 目录缺失时原回退分支必然抛
# AttributeError；前端静态资源缺失即部署错误，改为显式 fail fast。
_frontend_dir = app_config.frontend_dir
if not os.path.isdir(_frontend_dir):
    raise RuntimeError(
        f"[错误] Frontend/ 目录不存在 ({_frontend_dir})：前端静态资源缺失，"
        f"请检查部署（原回退分支引用不存在的 AppEngine.OUTPUT_DIR）")
app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")


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
    log.info("[信息] SSE:       /api/futures/read/stream（同步生成器，每连接一条常驻线程）")
    # Uvicorn 默认把日志写 stderr（终端标红）。传 log_config=None 让其
    # 复用 AppLog.py 已配置的 root (stdout)，startup/error 日志改为 stdout，
    # 与应用日志同色；access 日志已由 access_log=False 关闭。
    uvicorn.run(app, host=HOST, port=PORT, log_config=None,
                log_level="info", access_log=False)


# -*- coding: utf-8 -*-
"""
FrontAPI.py —— FastAPI 统一入口 · 面向前端 · REST + SSE（阶段 2 骨架）
=========================================================================
V10 方案 8.3 / 7.7：
  - 路由收敛、保持薄：本文件不写业务逻辑，仅做 入口组装 / 异常中间件 / 静态资源；
    REST 端点现阶段仍由 api_server.py 的 router 聚合挂载（阶段 3a 逐步迁入本文件）。
  - 统一错误处理：领域异常层级 + 全局 exception_handler，替代逐路由 try/except
    （api_server.py 现状 33 个路由重复同一模式）。
  - SSE 流式路径的专属错误规则（事件流内发 error 事件，不 raise HTTPException）
    于阶段 3b SSE 生成器迁入时启用。

合并说明（阶段 2 双版本合并）：
  - 底座采用第三方版：薄入口 + /api/health + 路由聚合（include_router api_server.router）
  - 领域异常统一定义于 App/AppOrch.py（服务层契约），本文件仅导入并注册处理器，
    保证服务层抛出的异常能被统一捕获（单向依赖 FrontAPI → AppOrch）。

启动：
    python FrontAPI.py                     # 推荐入口（端口/地址走 App/AppConfig.py）
    uvicorn FrontAPI:app --port 18081      # 等效
兼容：
    python api_server.py                   # 旧入口保留（阶段 3a 完成前不拆除）
"""
import sys
import os

# ── 仓库根目录引导（App/ 包、api_server、my_chan_main 均位于仓库根）────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from App.AppConfig import app_config
from App.AppOrch import AppError  # 领域异常统一在服务层定义（方案 7.7）

# 引擎模块（import my_chan_main 触发其模块级初始化，与 api_server 行为一致）
import my_chan_main as m  # noqa: F401  （FrontAPI 与 api_server 共享同一模块实例）


# ═══════════════════════════════════════════════════════════════════════
# 应用组装
# ═══════════════════════════════════════════════════════════════════════

app = FastAPI(title="缠论分析 API", version="1.1.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """领域异常 → 统一日志 + 统一 JSON（状态码读取异常自身）"""
    print(f"[FrontAPI] 领域异常 {exc.__class__.__name__}: {exc} ({request.url.path})", flush=True)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.__class__.__name__, "detail": str(exc)},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """未预期异常兜底：500 + 日志，不向客户端泄漏内部细节（7.7 规则）"""
    import traceback
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"error": "InternalError", "detail": "服务器内部错误"},
    )


@app.get("/api/health", tags=["meta"])
async def health():
    """健康检查：入口标识 + 配置摘要（凭据打码，方案 7.3）"""
    return {
        "status": "ok",
        "entry": "FrontAPI",
        "version": app.version,
        "symbol_code": m.SYMBOL_CODE,
        "config": app_config.as_dict(redact=True),
    }


# ── 路由聚合：现阶段全部 REST 端点仍在 api_server.router（阶段 3a 迁入）──
import api_server as _routes  # noqa: E402  （路由源模块）

app.include_router(_routes.router)

# ── 静态资源：Frontend/（与 api_server 兼容入口同规则）────────────────
_frontend_dir = app_config.frontend_dir
if os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
else:
    print(f"[警告] Frontend/ 目录不存在 ({_frontend_dir})，回退到 OUTPUT_DIR 静态挂载")
    app.mount("/", StaticFiles(directory=m.OUTPUT_DIR, html=True), name="static")


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import socket
    import uvicorn

    HOST = app_config.host
    PORT = app_config.port

    # ── 端口占用检测（与 api_server 兼容入口同策略）────────────────────
    try:
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _probe.bind((HOST, PORT))
        _probe.close()
    except OSError:
        print(f"[错误] 端口 {PORT} 已被占用！")
        print(f"[错误] 可能原因：api_server.py 旧入口或其他服务仍在运行。")
        print(f"[解决] Windows 可执行: netstat -ano | findstr {PORT} 查看占用 PID，")
        print(f"[解决] 然后执行: taskkill /PID <PID> /F 结束旧进程。")
        sys.exit(1)

    last_code, last_freq = m._load_last_code_freq()
    if last_code:
        print(f"[信息] 恢复上次: {last_code} (周期: {last_freq})")
    else:
        print(f"[信息] 使用默认股票: {m.SYMBOL_CODE}")

    print(f"[信息] FastAPI 服务器启动: http://{HOST}:{PORT}")
    print(f"[信息] API 文档:   http://{HOST}:{PORT}/docs")
    print(f"[信息] K线图表页:  http://{HOST}:{PORT}/")
    print(f"[信息] 健康检查:   http://{HOST}:{PORT}/api/health")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")

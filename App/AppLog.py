# -*- coding: utf-8 -*-
"""
App/AppLog.py —— 统一日志框架
=====================================================================
以 logging 封装替换散落 print；请求级 trace-id；错误日志带
code/freq/耗时上下文。

设计要点：
  - 单一事实源：全项目统一格式（时间 / 级别 / 模块 / 消息），stderr 输出。
  - 请求级 trace-id：contextvar 实现，线程 / 异步隔离；请求入口设置一次，
    链路内任意位置可读，用于跨函数 / 跨线程关联同一请求。
  - 零依赖：不 import 任何 App 业务模块，可被任意层（含 DataAPI）安全引用。

用法：
    from App.AppLog import get_logger, trace_id, current_trace_id
    log = get_logger(__name__)
    log.info("...")
    tid = trace_id()                 # 请求入口设置一次
    log.info("trace=%s", current_trace_id())
    log.error("code=%s freq=%s 耗时=%.3fs", code, freq, elapsed)
"""
import logging
import sys
import uuid
from contextvars import ContextVar

_TRACE_ID: ContextVar = ContextVar("trace_id", default="-")

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def _configure_root() -> None:
    """幂等配置根 logger：统一格式 + stderr 输出（重复 import 不叠加 handler）。"""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_configure_root()


def get_logger(name: str) -> logging.Logger:
    """按模块名取 logger（统一格式，无需逐文件配置）。"""
    return logging.getLogger(name)


def trace_id() -> str:
    """生成并设置新的请求级 trace-id，返回该 id（contextvar，线程/异步隔离）。"""
    tid = uuid.uuid4().hex[:12]
    _TRACE_ID.set(tid)
    return tid


def current_trace_id() -> str:
    """读取当前请求级 trace-id（无则 '-'）。"""
    return _TRACE_ID.get()

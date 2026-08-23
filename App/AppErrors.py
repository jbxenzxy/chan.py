# -*- coding: utf-8 -*-
"""
App/AppErrors.py —— 领域异常层级（P2-3 自 AppOrch.py 独立）
=================================================================
服务层统一异常体系：服务层只抛领域异常，API 层通过统一中间件捕获
（FrontAPI @app.exception_handler(AppError) → 结构化 JSON）。

原定义于 App/AppOrch.py（聚合入口），P2-3 为消除「error-dict 返回 vs
领域异常抛出」双轨并存、并让 AppSSE/AppChart 等下层功能模块可直接
引用（避免 AppSSE → AppOrch → AppChart → AppSSE 循环依赖），
将异常层级独立为本模块；AppOrch 继续 re-export，历史 import 路径
（from App.AppOrch import AppError）零改动。

状态码约定（设计文档 7.7）：
  AppError          500  领域异常基类
  DataFetchError    502  数据源获取失败
  AnalysisError     500  缠论分析失败
  ConfigError       500  配置 / 参数错误
  NotFoundError     404  股票 / 期货不存在
  PersistenceError  503  持久化失败
"""


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

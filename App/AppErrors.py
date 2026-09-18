# -*- coding: utf-8 -*-
"""
App/AppErrors.py —— 领域异常层级
=================================================================
服务层统一异常体系：服务层只抛领域异常，API 层通过统一中间件捕获
（FrontAPI @app.exception_handler(AppError) → 结构化 JSON）。

独立成模块使 AppSSE/AppChart 等下层功能模块可直接引用（避免
AppSSE → AppOrch → AppChart → AppSSE 循环依赖）；AppOrch 继续
re-export（from App.AppOrch import AppError 路径仍可用）。

状态码约定：
  AppError          500  领域异常基类
  BadRequestError   400  请求参数缺失 / 不合法（客户端输入问题）
  DataFetchError    502  数据源获取失败
  AnalysisError     500  缠论分析失败
  ConfigError       500  配置 / 参数错误
  NotFoundError     404  股票 / 期货不存在
  PersistenceError  503  持久化失败
"""


class AppError(Exception):
    """领域异常基类 · status_code 默认 500"""
    status_code = 500


class BadRequestError(AppError):
    """请求参数缺失 / 不合法 · 400

    「客户端输入问题」专用：请求缺字段 / 取值不被允许 —— 前端该据此提示
    "还差什么、哪个值不行"，监控也不该把它计成服务端故障。
    服务端自身故障（配置读不出、子进程起不来、磁盘写失败）继续走基类
    AppError(500)：两类混用会让用户把"自己少传一个字段"读成"系统坏了"。
    """
    status_code = 400


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

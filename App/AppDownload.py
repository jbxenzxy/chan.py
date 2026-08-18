# -*- coding: utf-8 -*-
"""
App/AppDownload.py —— 盘后下载功能域
=========================================================================
按业务能力拆分（阶段 8 重设计）：点击页面右上角「盘后下载」按钮后的操作。

本模块收纳：
  - start_download_checked（带前置检查，GET/POST 共用）
  - start_download / stop_download / get_download_status
  - eltdx_available / download_dir

阶段 5：职责内聚 DataAPI/ElTdxAPI.py，此处为薄封装（委托目标 ElTdxAPI）。
"""
import json


# ═══════════════════════════════════════════════════════════════════════
# 盘后下载（页面右上角「盘后下载」按钮）
# ═══════════════════════════════════════════════════════════════════════

def eltdx_available():
    """eltdx 盘后下载引擎是否可用"""
    from DataAPI import ElTdxAPI as _eltdx
    return _eltdx._ELTDX_AVAILABLE


def download_dir():
    """盘后下载数据保存目录（阶段 4 配置中心化：app_config.download_dir）"""
    from App.AppConfig import app_config
    return app_config.download_dir


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

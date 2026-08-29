# -*- coding: utf-8 -*-
"""
App/AppDownload.py —— 盘后下载功能域
=========================================================================
点击页面右上角「盘后下载」按钮后的操作。

本模块收纳：
  - start_download_checked（带前置检查，盘后下载唯一启动入口）
  - stop_download / get_download_status
  - download_dir

下载职责内聚 DataAPI/ElTdxAPI.py，此处为薄封装（委托目标 ElTdxAPI）。
"""
import json


# ═══════════════════════════════════════════════════════════════════════
# 盘后下载（页面右上角「盘后下载」按钮）
# ═══════════════════════════════════════════════════════════════════════

def download_dir():
    """盘后下载数据保存目录（app_config.download_dir）"""
    from App.AppConfig import app_config
    return app_config.download_dir


def start_download_checked(categories, day_start=None, min_start=None):
    """盘后下载启动 · 带前置检查（仅 POST /api/stocks/download/start，GET 入口已删除）

    categories: POST 传 list（与删除前 GET 传 JSON 字符串的形态差异已归一）。
    返回 (result_dict, status_code)（含 409 冲突码）。
    委托 DataAPI/ElTdxAPI（下载目录经 app_config.download_dir）。
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


def get_download_status():
    """盘后下载进度"""
    from DataAPI import ElTdxAPI as _eltdx
    return _eltdx._get_download_status()


def stop_download():
    """停止盘后下载"""
    from DataAPI import ElTdxAPI as _eltdx
    return _eltdx._stop_download()

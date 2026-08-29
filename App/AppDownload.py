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
import logging

log = logging.getLogger(__name__)


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

    # P0-3：下载完成 → 失效股票分析缓存（下载写入了新数据，旧的
    # 分析结果已过期；共享 LRU 会在下次请求时按需重建）。回调由
    # AppDownload（App 层）注入，ElTdxAPI 保持零 App 依赖。
    ok, msg = _eltdx._start_download(app_config.download_dir, categories,
                                     day_start=day_start or None,
                                     min_start=min_start or None,
                                     on_finish=stocks_cache_clear)
    return {"ok": ok, "message": msg}, (200 if ok else 409)


def stocks_cache_clear():
    """失效股票分析缓存（P0-3 唯一失效漏斗）。

    下载完成回调（on_finish）与手动入口（POST /api/stocks/cleanup，经
    AppOrch 再导出）均汇聚至此；App 层持 AppData 依赖，DataAPI 层零依赖。
    """
    from App.AppData import app_data
    cleared = app_data.stocks_cache_clear()
    log.info(f"[缓存] 失效股票分析缓存 {cleared} 条")
    return cleared


def get_download_status():
    """盘后下载进度"""
    from DataAPI import ElTdxAPI as _eltdx
    return _eltdx._get_download_status()


def stop_download():
    """停止盘后下载"""
    from DataAPI import ElTdxAPI as _eltdx
    return _eltdx._stop_download()

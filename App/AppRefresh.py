# -*- coding: utf-8 -*-
"""
App/AppRefresh.py —— 刷新功能域
=========================================================================
按业务能力拆分（阶段 8 重设计）：点击页面右上角「刷新」按钮后的操作，
刷新股票名、指数归属、PE-TTM、板块文件等。

本模块收纳：
  - 股票名称刷新（refresh_stock_names / refresh_stock_names_async / refresh_status）
  - 名称 / PE / 流通市值 / 指数归属 缓存读写（AppData 直连 + 引擎壳）
  - load_stock_names_from_cache_file / load_float_mc_cache /
    fetch_float_mc_from_tencent / update_float_mc_cache /
    load_pe_ttm_cache / get_pe_ttm / get_index_belong

依赖方向：AppRefresh.py → AppEngine / AppData（单向）
"""
import threading
import traceback

# 分析引擎层（阶段 10.1：my_chan_main.py 职责被各层完全吸收，引擎迁入 App/AppEngine.py）
from App import AppEngine as _m
from App.AppLog import get_logger
log = get_logger(__name__)



# ═══════════════════════════════════════════════════════════════════════
# 名称 / PE / 流通市值 / 指数归属 缓存
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
            log.error(f"[错误] refresh_stock_names异常: {e}")

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()
    return {"status": "started", "msg": "股票名称刷新已启动"}

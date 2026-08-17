# -*- coding: utf-8 -*-
"""
DataAPI — 数据源抽象层（阶段 5：获取侧抽象完善）
==================================================
对外提供统一的数据源选择与数据获取接口，屏蔽底层 TDX / TqSdk / 其他
数据源的细节差异。消费侧（AppOrch）通过本层获取数据，不再直连底层实现。

导出的公共接口：
  - get_stock_api(code, k_type, begin_date, end_date, autype)
      根据代码类型自动选择数据源，返回 CCommonStockApi 子类实例
  - CCommonStockApi
      数据源抽象基类（重新导出，方便消费侧引用）
  - _ELTDX_AVAILABLE
      盘后下载引擎是否可用
"""
from DataAPI.CommonStockAPI import CCommonStockApi  # noqa: F401


def get_stock_api(code, k_type="d", begin_date=None, end_date=None, autype=None):
    """数据源工厂：根据代码类型自动选择数据源，返回 CCommonStockApi 子类实例。

    选择规则：
      - 期货/期指代码（包含 CFFEX/SHFE/DCE/CZCE/INE/GFEX/SGX 等交易所前缀）
        或 KQ.m@ / KQ.i@ / KQD.m@ 前缀 → TqSdkAPI
      - 其他（A股/指数/板块/港股）→ TdxAPI

    返回的实例可直接调用 get_kl_data() 获取 K 线数据。
    """
    from DataAPI.TqSdkAPI import _get_futures_code

    is_futures = _get_futures_code(code) is not None
    if is_futures:
        from DataAPI.TqSdkAPI import CTqSdkAPI
        return CTqSdkAPI(code, k_type, begin_date, end_date, autype)
    else:
        from DataAPI.TdxAPI import CTdxAPI
        return CTdxAPI(code, k_type, begin_date, end_date, autype)

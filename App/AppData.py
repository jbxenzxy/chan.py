"""
App/AppData.py — 业务数据层
============================
负责缓存 / 持久化 / 标注 / 自选股（见设计文档 6.1 节）。

第一版为薄封装：接口先锁定，实现委托给 my_chan_main 的既有函数，
保证零行为漂移。后续阶段可逐步将实现搬入本层。

依赖方向（设计文档 6.2 节）：
  FrontAPI.py → App/AppOrch.py → App/AppData.py（单向，禁止反向）

使用方式：
    from App.AppData import app_data
    app_data.cache_put("key", value)
    app_data.get_annotations_for("000001.SH", "d")
"""

import os
import gc
import re


class AppData:
    """业务数据层：缓存 / 持久化 / 标注 / 自选股"""

    # ── 缓存（LRU 语义，委托 my_chan_main）───────────────────────────
    def cache_put(self, key, value):
        """写入缓存，超出上限时淘汰最旧条目"""
        from my_chan_main import _cache_put
        return _cache_put(key, value)

    def cache_get(self, key):
        """读取缓存，命中时移到末尾（LRU 语义）"""
        from my_chan_main import _cache_get
        return _cache_get(key)

    def cache_remove(self, key):
        """从缓存中删除指定条目"""
        from my_chan_main import _cache_remove
        return _cache_remove(key)

    # ── 上次查看代码/周期持久化 ──────────────────────────────────────
    def save_last_code_freq(self, code, freq="d"):
        """持久化上次查看的代码和周期（股票和期货通用）"""
        from my_chan_main import _save_last_code_freq
        return _save_last_code_freq(code, freq)

    def load_last_code_freq(self):
        """加载上次查看的代码和周期，返回 (code, freq) 或 (None, None)"""
        from my_chan_main import _load_last_code_freq
        return _load_last_code_freq()

    # ── 手动选点持久化 ───────────────────────────────────────────────
    def load_saved_point_times(self):
        """从 CSV 加载所有选点记录，返回 {code: {col: value}}"""
        from my_chan_main import _load_saved_point_times
        return _load_saved_point_times()

    def save_point_time(self, code, name, freq, sdt):
        """保存或更新某只股票某个周期的选点"""
        from my_chan_main import _save_point_time
        return _save_point_time(code, name, freq, sdt)

    def clear_saved_point_time(self, code, freq):
        """清除某只股票某个周期在 CSV 中的选点"""
        from my_chan_main import _clear_saved_point_time
        return _clear_saved_point_time(code, freq)

    def clear_saved_point(self, code, freq="d"):
        """清除选点并同步清理分析缓存（对应 /api/clear_saved_point）"""
        from my_chan_main import _clear_saved_point_time, _stocks_analysis_cache, _cache_lock

        normalized_code = code.strip().upper()
        market = None
        prefix_match = re.match(r'^(SH|SZ|HK|DS)(\d+)$', normalized_code)
        suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK|DS)$', normalized_code)
        if prefix_match:
            market = prefix_match.group(1).lower()
            normalized_code = prefix_match.group(2)
        elif suffix_match:
            normalized_code = suffix_match.group(1)
            market = suffix_match.group(2).lower()

        qualified_code = f"{normalized_code}.{market.upper()}" if market else normalized_code
        _clear_saved_point_time(qualified_code, freq)
        cache_key = f"single_{market}_{normalized_code}_{freq}_live"
        with _cache_lock:
            if cache_key in _stocks_analysis_cache:
                del _stocks_analysis_cache[cache_key]
        gc.collect()
        return {"ok": True}

    # ── 文字标注 ─────────────────────────────────────────────────────
    def get_annotations_for(self, code, freq):
        """获取某股票某周期的所有标注"""
        from my_chan_main import _get_annotations_for
        return _get_annotations_for(code, freq)

    def add_annotation(self, code, freq, date_str, text, y_offset=0):
        """添加一条标注（同日期同文字自动去重）"""
        from my_chan_main import _add_annotation
        return _add_annotation(code, freq, date_str, text, y_offset)

    def delete_annotation(self, code, freq, date_str, text):
        """删除一条标注"""
        from my_chan_main import _delete_annotation
        return _delete_annotation(code, freq, date_str, text)

    def delete_annotation_by_date(self, code, freq, date_str):
        """删除某日期下所有标注"""
        from my_chan_main import _delete_annotation_by_date
        return _delete_annotation_by_date(code, freq, date_str)

    def delete_all_annotations(self, code, freq):
        """删除某股票某周期下全部标注"""
        from my_chan_main import _delete_all_annotations
        return _delete_all_annotations(code, freq)

    def get_annotated_codes(self, freq=""):
        """获取所有有标注的股票代码+周期列表，用于自选扫描"""
        from my_chan_main import _get_annotated_codes
        return _get_annotated_codes(freq)

    # ── 自选股（委托 my_chan_main，其内部复用 DataAPI.TdxAPI）────────
    def read_zxg_stocks(self):
        """读取自选股列表"""
        from my_chan_main import read_zxg_stocks
        return read_zxg_stocks()

    def save_to_zxg_blk(self, codes):
        """保存股票代码列表到自选股板块（codes: list[str]，如 ["000852.SH", ...]）"""
        from my_chan_main import save_to_zxg_blk
        return save_to_zxg_blk(codes)


# 全局单例
app_data = AppData()

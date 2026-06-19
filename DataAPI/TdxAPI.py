"""
通达信本地文件数据源适配器（供 chan.py 的 custom 数据源使用）
使用方法：将此文件放到 chan.py 仓库的 DataAPI/ 目录下
"""

from Common.CEnum import AUTYPE, KL_TYPE, DATA_FIELD
from DataAPI.CommonStockAPI import CCommonStockApi
from KLine.KLine_Unit import CKLine_Unit


class CTdxAPI(CCommonStockApi):
    """通达信本地文件数据源适配器"""

    # 类变量，由 my_chan_main.py 外部设置
    _tdx_data = None  # list of dict: [{dt, open, high, low, close, vol, amount}, ...]

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        super().__init__(code, k_type, begin_date, end_date, autype)
        self.is_stock = True

    @classmethod
    def set_data(cls, data):
        """设置K线数据（由 my_chan_main.py 调用）"""
        cls._tdx_data = data

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass

    def SetBasciInfo(self):
        self.is_stock = True

    def get_kl_data(self):
        """逐根 yield 返回 CKLine_Unit"""
        if not CTdxAPI._tdx_data:
            return
        from Common.CTime import CTime
        for row in CTdxAPI._tdx_data:
            dt = row["dt"]
            time = CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, auto=False)
            klu_dict = {
                DATA_FIELD.FIELD_TIME: time,
                DATA_FIELD.FIELD_OPEN: row["open"],
                DATA_FIELD.FIELD_CLOSE: row["close"],
                DATA_FIELD.FIELD_HIGH: row["high"],
                DATA_FIELD.FIELD_LOW: row["low"],
                DATA_FIELD.FIELD_VOLUME: row["vol"],
                DATA_FIELD.FIELD_TURNOVER: row["amount"],
            }
            yield CKLine_Unit(klu_dict)


class CTdxAPI_Sliced(CTdxAPI):
    """
    切片数据源适配器：只返回从指定日期开始的K线数据。
    用于双击选点后，基于 chan.py 内部算法重新计算中枢/线段/买卖点。
    """
    _sliced_data = []

    @classmethod
    def set_sliced_data(cls, records, start_dt):
        """
        设置切片数据：只保留从 start_dt 开始的记录（含等于）

        Args:
            records: 完整的K线记录列表
            start_dt: 开始日期时间（datetime对象）
        """
        cls._sliced_data = [r for r in records if r["dt"] >= start_dt]
        print(f"[信息] 切片数据源: 从 {start_dt} 开始，共 {len(cls._sliced_data)} 条K线")

    @classmethod
    def clear_sliced_data(cls):
        """清空切片数据"""
        cls._sliced_data = []

    def get_kl_data(self):
        """逐根 yield 返回切片后的 CKLine_Unit"""
        if not CTdxAPI_Sliced._sliced_data:
            return
        from Common.CTime import CTime
        for row in CTdxAPI_Sliced._sliced_data:
            dt = row["dt"]
            time = CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, auto=False)
            klu_dict = {
                DATA_FIELD.FIELD_TIME: time,
                DATA_FIELD.FIELD_OPEN: row["open"],
                DATA_FIELD.FIELD_CLOSE: row["close"],
                DATA_FIELD.FIELD_HIGH: row["high"],
                DATA_FIELD.FIELD_LOW: row["low"],
                DATA_FIELD.FIELD_VOLUME: row["vol"],
                DATA_FIELD.FIELD_TURNOVER: row["amount"],
            }
            yield CKLine_Unit(klu_dict)

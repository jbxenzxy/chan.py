from enum import Enum, auto
from typing import Literal


class DATA_SRC(Enum):
    BAO_STOCK = auto()
    CCXT = auto()
    CSV = auto()
    AKSHARE = auto()


class KL_TYPE(Enum):
    K_1S = 1
    K_3S = 2
    K_5S = 3
    K_10S = 4
    K_15S = 5
    K_20S = 6
    K_30S = 7
    K_1M = 8
    K_3M = 9
    K_5M = 10
    K_10M = 11
    K_15M = 12
    K_30M = 13
    K_60M = 14
    K_DAY = 15
    K_WEEK = 16
    K_MON = 17
    K_QUARTER = 18
    K_YEAR = 19


class KLINE_DIR(Enum):
    UP = auto()
    DOWN = auto()
    COMBINE = auto()
    INCLUDED = auto()


class FX_TYPE(Enum):
    BOTTOM = auto()
    TOP = auto()
    UNKNOWN = auto()


class BI_DIR(Enum):
    UP = auto()
    DOWN = auto()


class BI_TYPE(Enum):
    UNKNOWN = auto()
    STRICT = auto()
    SUB_VALUE = auto()  # 次高低点成笔
    TIAOKONG_THRED = auto()
    DAHENG = auto()
    TUIBI = auto()
    UNSTRICT = auto()
    TIAOKONG_VALUE = auto()


BSP_MAIN_TYPE = Literal['0', '1', '2', '3', '11', '22', '33']


class BSP_TYPE(Enum):
    T0 = '0'      # 0类买卖点（神之一笔，不依赖中枢/背驰）
    T1 = '1'      # 1类买卖点（对应缠论一类买卖点）
    T2 = '2'      # 2类买卖点（待实现）
    T3 = '3'      # 3类买卖点（对应缠论三类买卖点）
    T11 = '11'  # 11类买卖点（原1类，趋势背驰）
    T11P = '11p'  # 11p类买卖点（原1p类，盘整背驰）
    T22 = '22'  # 22类买卖点（原2类，回踩/回抽）
    T22S = '22s'  # 22s类买卖点（原2s类，类二）
    T33A = '33a'  # 33a类买卖点（原3a类，中枢在11类后面）
    T33B = '33b'  # 33b类买卖点（原3b类，中枢在11类前面）

    def main_type(self) -> BSP_MAIN_TYPE:
        return self.value.rstrip('psab')  # type: ignore


class AUTYPE(Enum):
    QFQ = auto()
    HFQ = auto()
    NONE = auto()


class TREND_TYPE(Enum):
    MEAN = "mean"
    MAX = "max"
    MIN = "min"


class TREND_LINE_SIDE(Enum):
    INSIDE = auto()
    OUTSIDE = auto()


class LEFT_SEG_METHOD(Enum):
    ALL = auto()
    PEAK = auto()


class FX_CHECK_METHOD(Enum):
    STRICT = auto()
    LOSS = auto()
    HALF = auto()
    TOTALLY = auto()


class SEG_TYPE(Enum):
    BI = auto()
    SEG = auto()


class MACD_ALGO(Enum):
    AREA = auto()
    PEAK = auto()
    FULL_AREA = auto()
    FULL_AREA_EXT = auto()
    DIF = auto()
    DEA = auto()
    DIFF = auto()
    SLOPE = auto()
    AMP = auto()
    VOLUMN = auto()
    AMOUNT = auto()
    VOLUMN_AVG = auto()
    AMOUNT_AVG = auto()
    TURNRATE_AVG = auto()
    RSI = auto()


class DATA_FIELD:
    FIELD_TIME = "time_key"
    FIELD_OPEN = "open"
    FIELD_HIGH = "high"
    FIELD_LOW = "low"
    FIELD_CLOSE = "close"
    FIELD_VOLUME = "volume"  # 成交量
    FIELD_TURNOVER = "turnover"  # 成交额
    FIELD_TURNRATE = "turnover_rate"  # 换手率


TRADE_INFO_LST = [DATA_FIELD.FIELD_VOLUME, DATA_FIELD.FIELD_TURNOVER, DATA_FIELD.FIELD_TURNRATE]

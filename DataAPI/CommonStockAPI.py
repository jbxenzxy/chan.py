import abc
from typing import Iterable

from KLine.KLine_Unit import CKLine_Unit


class CCommonStockApi:
    """
    数据源抽象基类（阶段 5 起承载「数据源元数据接口」，设计文档 8.8）。

    收敛原则：数据源特有元数据（频率映射 / 别名 / 支持列表）提升为抽象层
    元数据接口，业务代码只依赖本抽象层，不再直接 import 具体数据源实现；
    跨数据源差异由各实现类各自提供（无期货能力的数据源保持默认空值）。
    """

    def __init__(self, code, k_type, begin_date, end_date, autype):
        self.code = code
        self.name = None
        self.is_stock = None
        self.k_type = k_type
        self.begin_date = begin_date
        self.end_date = end_date
        self.autype = autype
        self.SetBasciInfo()

    @abc.abstractmethod
    def get_kl_data(self) -> Iterable[CKLine_Unit]:
        pass

    @abc.abstractmethod
    def SetBasciInfo(self):
        pass

    @classmethod
    @abc.abstractmethod
    def do_init(cls):
        pass

    @classmethod
    @abc.abstractmethod
    def do_close(cls):
        pass

    # ═══════════════════════════════════════════════════════════════
    # 数据源元数据接口（阶段 5 提升，设计文档 8.8 非标准导入收敛表）
    # 类属性访问（Cls.FREQ_SEC_MAP），默认空值；具体数据源各自覆盖。
    #
    # 实现说明：用「普通类属性」而非「@classmethod @property」——
    # 后者在 Python 3.11+ 下 classmethod 包装 property 会返回 method
    # 对象而非属性值（TypeError: argument of type 'method' is not a
    # container or iterable），普通类属性全 Python 版本行为一致。
    # ═══════════════════════════════════════════════════════════════
    FREQ_SEC_MAP = {}
    """周期标签 → 秒数 映射（默认空，由具体数据源提供）"""

    FREQ_LABEL_CN = {}
    """周期标签 → 中文名 映射（默认空）"""

    FUTURES_ALIASES = {}
    """期货别名 → 完整代码 映射（默认空）"""

    SUPPORTED_FREQS = []
    """支持的周期列表（默认空）"""

    DISABLED_FREQS = []
    """禁用的周期列表（默认空）"""

    @classmethod
    def fetch_kline(cls, api, symbol, freq_sec=15, num_bars=None,
                    display_key=None, start_time=None):
        """拉取历史 K 线（数据源特有实现；未实现时抛错）"""
        raise NotImplementedError(f"{cls.__name__} 未实现 fetch_kline")

from typing import List, Optional

from Common.cache import make_cache
from Common.CEnum import BI_DIR, BI_TYPE, DATA_FIELD, FX_TYPE, MACD_ALGO
from Common.ChanException import CChanException, ErrCode
from KLine.KLine import CKLine
from KLine.KLine_Unit import CKLine_Unit


class CBi:
    def __init__(self, begin_klc: CKLine, end_klc: CKLine, idx: int, is_sure: bool):
        # self.__begin_klc = begin_klc
        # self.__end_klc = end_klc
        self.__dir = None
        self.__idx = idx
        self.__type = BI_TYPE.STRICT

        self.set(begin_klc, end_klc)

        self.__is_sure = is_sure
        self.__used_to_be_sure = is_sure
        self.__sure_end: List[CKLine] = []

        self.__seg_idx: Optional[int] = None

        from Seg.Seg import CSeg
        self.parent_seg: Optional[CSeg[CBi]] = None  # 在哪个线段里面

        from BuySellPoint.BS_Point import CBS_Point
        self.bsp: Optional[CBS_Point] = None  # 尾部是不是买卖点

        self.next: Optional[CBi] = None
        self.pre: Optional[CBi] = None

    def clean_cache(self):
        self._memoize_cache = {}

    @property
    def begin_klc(self): return self.__begin_klc

    @property
    def end_klc(self): return self.__end_klc

    @property
    def dir(self): return self.__dir

    @property
    def idx(self): return self.__idx

    @property
    def type(self): return self.__type

    @property
    def is_sure(self): return self.__is_sure

    @property
    def used_to_be_sure(self): return self.__used_to_be_sure

    @property
    def is_used_to_be_sure(self): return self.is_sure or self.used_to_be_sure

    @property
    def sure_end(self): return self.__sure_end

    @property
    def klc_lst(self):
        klc = self.begin_klc
        while True:
            yield klc
            klc = klc.next
            if not klc or klc.idx > self.end_klc.idx:
                break

    @property
    def klc_lst_re(self):
        klc = self.end_klc
        while True:
            yield klc
            klc = klc.pre
            if not klc or klc.idx < self.begin_klc.idx:
                break

    @property
    def seg_idx(self): return self.__seg_idx

    def set_seg_idx(self, idx):
        self.__seg_idx = idx

    def __str__(self):
        return f"{self.dir}|{self.begin_klc} ~ {self.end_klc}"

    def check(self):
        try:
            if self.is_down():
                assert self.begin_klc.high > self.end_klc.low
            else:
                assert self.begin_klc.low < self.end_klc.high
        except Exception as e:
            raise CChanException(f"{self.idx}:{self.begin_klc[0].time}~{self.end_klc[-1].time}笔的方向和收尾位置不一致!", ErrCode.BI_ERR) from e

    def set(self, begin_klc: CKLine, end_klc: CKLine):
        self.__begin_klc: CKLine = begin_klc
        self.__end_klc: CKLine = end_klc
        if begin_klc.fx == FX_TYPE.BOTTOM:
            self.__dir = BI_DIR.UP
        elif begin_klc.fx == FX_TYPE.TOP:
            self.__dir = BI_DIR.DOWN
        else:
            raise CChanException("ERROR DIRECTION when creating bi", ErrCode.BI_ERR)
        self.check()
        self.clean_cache()

    @make_cache
    def get_begin_val(self):
        return self.begin_klc.low if self.is_up() else self.begin_klc.high

    @make_cache
    def get_end_val(self):
        return self.end_klc.high if self.is_up() else self.end_klc.low

    @make_cache
    def get_begin_klu(self) -> CKLine_Unit:
        if self.is_up():
            return self.begin_klc.get_peak_klu(is_high=False)
        else:
            return self.begin_klc.get_peak_klu(is_high=True)

    @make_cache
    def get_end_klu(self) -> CKLine_Unit:
        if self.is_up():
            return self.end_klc.get_peak_klu(is_high=True)
        else:
            return self.end_klc.get_peak_klu(is_high=False)

    @make_cache
    def amp(self):
        return abs(self.get_end_val() - self.get_begin_val())

    @make_cache
    def get_klu_cnt(self):
        return self.get_end_klu().idx - self.get_begin_klu().idx + 1

    @make_cache
    def get_klc_cnt(self):
        assert self.end_klc.idx == self.get_end_klu().klc.idx
        assert self.begin_klc.idx == self.get_begin_klu().klc.idx
        return self.end_klc.idx - self.begin_klc.idx + 1

    @make_cache
    def _high(self):
        return self.end_klc.high if self.is_up() else self.begin_klc.high

    @make_cache
    def _low(self):
        return self.begin_klc.low if self.is_up() else self.end_klc.low

    @make_cache
    def _mid(self):
        return (self._high() + self._low()) / 2  # 笔的中位价

    @make_cache
    def is_down(self):
        return self.dir == BI_DIR.DOWN

    @make_cache
    def is_up(self):
        return self.dir == BI_DIR.UP

    def update_virtual_end(self, new_klc: CKLine):
        self.append_sure_end(self.end_klc)
        self.update_new_end(new_klc)
        self.__used_to_be_sure = self.__is_sure
        self.__is_sure = False

    def restore_from_virtual_end(self, sure_end: CKLine):
        self.__is_sure = True
        self.__used_to_be_sure = True
        self.update_new_end(new_klc=sure_end)
        self.__sure_end = []

    def append_sure_end(self, klc: CKLine):
        self.__sure_end.append(klc)

    def update_new_end(self, new_klc: CKLine):
        self.__end_klc = new_klc
        self.check()
        self.clean_cache()

    def cal_macd_metric(self, macd_algo, is_reverse):
        if macd_algo == MACD_ALGO.AREA:
            return self.Cal_MACD_half(is_reverse)
        elif macd_algo == MACD_ALGO.PEAK:
            return self.Cal_MACD_peak()
        elif macd_algo == MACD_ALGO.FULL_AREA:
            return self.Cal_MACD_area()
        elif macd_algo == MACD_ALGO.FULL_AREA_EXT:
            return self.Cal_MACD_area_ext()
        elif macd_algo == MACD_ALGO.DIF:
            return self.Cal_MACD_dif()
        elif macd_algo == MACD_ALGO.DEA:
            return self.Cal_MACD_dea()
        elif macd_algo == MACD_ALGO.DIFF:
            return self.Cal_MACD_diff()
        elif macd_algo == MACD_ALGO.SLOPE:
            return self.Cal_MACD_slope()
        elif macd_algo == MACD_ALGO.AMP:
            return self.Cal_MACD_amp()
        elif macd_algo == MACD_ALGO.AMOUNT:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_TURNOVER, cal_avg=False)
        elif macd_algo == MACD_ALGO.VOLUMN:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_VOLUME, cal_avg=False)
        elif macd_algo == MACD_ALGO.VOLUMN_AVG:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_VOLUME, cal_avg=True)
        elif macd_algo == MACD_ALGO.AMOUNT_AVG:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_TURNOVER, cal_avg=True)
        elif macd_algo == MACD_ALGO.TURNRATE_AVG:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_TURNRATE, cal_avg=True)
        elif macd_algo == MACD_ALGO.RSI:
            return self.Cal_Rsi()
        else:
            raise CChanException(f"unsupport macd_algo={macd_algo}, should be one of area/full_area/peak/diff/slope/amp", ErrCode.PARA_ERROR)

    @make_cache
    def Cal_Rsi(self):
        rsi_lst: List[float] = []
        for klc in self.klc_lst:
            rsi_lst.extend(klu.rsi for klu in klc.lst)
        return 10000.0/(min(rsi_lst)+1e-7) if self.is_down() else max(rsi_lst)

    @make_cache
    def Cal_MACD_area(self):
        _s = 1e-7
        begin_klu = self.get_begin_klu()
        end_klu = self.get_end_klu()
        for klc in self.klc_lst:
            for klu in klc.lst:
                if klu.idx < begin_klu.idx or klu.idx > end_klu.idx:
                    continue
                if (self.is_down() and klu.macd.macd < 0) or (self.is_up() and klu.macd.macd > 0):
                    _s += abs(klu.macd.macd)
        return _s

    @make_cache
    def Cal_MACD_area_ext(self):
        """
        扩展的 MACD 全程面积算法：
        - 向下笔：G(绿柱面积) + (红柱最高峰至末尾的矩形面积 - 红柱峰到末尾的实际面积)
        - 向上笔：G(红柱面积) + (绿柱最低峰至末尾的矩形面积 - 绿柱峰到末尾的实际面积)
        多个相等峰值时取最后一个。无反向柱子时退化为 Cal_MACD_area。
        """
        _s = 1e-7
        begin_klu = self.get_begin_klu()
        end_klu = self.get_end_klu()

        same_dir_sum = 0.0             # G: 同向柱子绝对值之和
        counter_bars: List[float] = []  # 反向柱子，按时间顺序排列

        for klc in self.klc_lst:
            for klu in klc.lst:
                if klu.idx < begin_klu.idx or klu.idx > end_klu.idx:
                    continue
                if self.is_down():
                    if klu.macd.macd < 0:       # 绿柱，同向
                        same_dir_sum += abs(klu.macd.macd)
                    else:                         # 红柱，反向
                        counter_bars.append(klu.macd.macd)
                else:  # 向上笔
                    if klu.macd.macd > 0:        # 红柱，同向
                        same_dir_sum += abs(klu.macd.macd)
                    else:                         # 绿柱，反向
                        counter_bars.append(klu.macd.macd)

        if not counter_bars:
            return same_dir_sum + _s

        # 找峰值：向下笔取最大值，向上笔取最小值（绝对值最大）
        peak_val = max(counter_bars) if self.is_down() else min(counter_bars)

        # 从后往前找最后一个等于峰值的索引
        peak_idx = len(counter_bars) - 1
        for i in range(len(counter_bars) - 1, -1, -1):
            if counter_bars[i] == peak_val:
                peak_idx = i
                break

        count = len(counter_bars) - peak_idx   # 峰到末尾的柱子数
        Y = sum(abs(counter_bars[j]) for j in range(peak_idx, len(counter_bars)))
        X = abs(peak_val) * count

        return same_dir_sum + (X - Y) + _s

    @make_cache
    def Cal_MACD_peak(self):
        peak = 1e-7
        for klc in self.klc_lst:
            for klu in klc.lst:
                if abs(klu.macd.macd) > peak:
                    if self.is_down() and klu.macd.macd < 0:
                        peak = abs(klu.macd.macd)
                    elif self.is_up() and klu.macd.macd > 0:
                        peak = abs(klu.macd.macd)
        return peak

    @make_cache
    def Cal_MACD_dif(self):
        peak = 1e-7
        for klc in self.klc_lst:
            for klu in klc.lst:
                if abs(klu.macd.DIF) > peak:
                    if self.is_down() and klu.macd.DIF < 0:
                        peak = abs(klu.macd.DIF)
                    elif self.is_up() and klu.macd.DIF > 0:
                        peak = abs(klu.macd.DIF)
        return peak

    @make_cache
    def Cal_MACD_dea(self):
        peak = 1e-7
        for klc in self.klc_lst:
            for klu in klc.lst:
                if abs(klu.macd.DEA) > peak:
                    if self.is_down() and klu.macd.DEA < 0:
                        peak = abs(klu.macd.DEA)
                    elif self.is_up() and klu.macd.DEA > 0:
                        peak = abs(klu.macd.DEA)
        return peak

    def Cal_MACD_half(self, is_reverse):
        if is_reverse:
            return self.Cal_MACD_half_reverse()
        else:
            return self.Cal_MACD_half_obverse()

    @make_cache
    def Cal_MACD_half_obverse(self):
        _s = 1e-7
        begin_klu = self.get_begin_klu()
        peak_macd = begin_klu.macd.macd
        for klc in self.klc_lst:
            for klu in klc.lst:
                if klu.idx < begin_klu.idx:
                    continue
                if klu.macd.macd*peak_macd > 0:
                    _s += abs(klu.macd.macd)
                else:
                    break
            else:  # 没有被break，继续找写一个KLC
                continue
            break
        return _s

    @make_cache
    def Cal_MACD_half_reverse(self):
        _s = 1e-7
        begin_klu = self.get_end_klu()
        peak_macd = begin_klu.macd.macd
        for klc in self.klc_lst_re:
            for klu in klc[::-1]:
                if klu.idx > begin_klu.idx:
                    continue
                if klu.macd.macd*peak_macd > 0:
                    _s += abs(klu.macd.macd)
                else:
                    break
            else:  # 没有被break，继续找写一个KLC
                continue
            break
        return _s

    @make_cache
    def Cal_MACD_diff(self):
        """
        macd红绿柱最大值最小值之差
        """
        _max, _min = float("-inf"), float("inf")
        for klc in self.klc_lst:
            for klu in klc.lst:
                macd = klu.macd.macd
                if macd > _max:
                    _max = macd
                if macd < _min:
                    _min = macd
        return _max-_min

    @make_cache
    def Cal_MACD_slope(self):
        begin_klu = self.get_begin_klu()
        end_klu = self.get_end_klu()
        if self.is_up():
            return (end_klu.high - begin_klu.low)/end_klu.high/(end_klu.idx - begin_klu.idx + 1)
        else:
            return (begin_klu.high - end_klu.low)/begin_klu.high/(end_klu.idx - begin_klu.idx + 1)

    @make_cache
    def Cal_MACD_amp(self):
        begin_klu = self.get_begin_klu()
        end_klu = self.get_end_klu()
        if self.is_down():
            return (begin_klu.high-end_klu.low)/begin_klu.high
        else:
            return (end_klu.high-begin_klu.low)/begin_klu.low

    def Cal_MACD_trade_metric(self, metric: str, cal_avg=False) -> float:
        _s = 0
        for klc in self.klc_lst:
            for klu in klc.lst:
                metric_res = klu.trade_info.metric[metric]
                if metric_res is None:
                    return 0.0
                _s += metric_res
        return _s / self.get_klu_cnt() if cal_avg else _s

    # def set_klc_lst(self, lst):
    #     self.__klc_lst = lst

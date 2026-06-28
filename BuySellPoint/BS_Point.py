from typing import Dict, Generic, List, Optional, TypeVar, Union

from Bi.Bi import CBi
from ChanModel.Features import CFeatures
from Common.CEnum import BSP_TYPE
from Seg.Seg import CSeg

LINE_TYPE = TypeVar('LINE_TYPE', CBi, CSeg)


class CBS_Point(Generic[LINE_TYPE]):
    def __init__(self, bi: LINE_TYPE, is_buy, bs_type: BSP_TYPE, relate_bsp1: Optional['CBS_Point'], feature_dict=None):
        self.bi: LINE_TYPE = bi
        end_klu = bi.get_end_klu()
        # 尝试取右肩K线：从分型合并K线（CKLine_Combined）往后找，取右肩最后一根原始K线
        # 用 lst[-1] 而非 lst[0]，因为右肩KLC在包含合并后可能包含多根原始K线，
        # 而 MACD 比较用的是 lst[-1] 的值，买卖点应标记在触发该MACD值的K线上
        end_klc = getattr(bi, 'end_klc', None)
        if end_klc is not None:
            right_klc = getattr(end_klc, 'next', None)
            if right_klc is not None and hasattr(right_klc, 'lst') and right_klc.lst:
                self.klu = right_klc.lst[-1]
            else:
                self.klu = end_klu
        else:
            self.klu = end_klu
        self.is_buy = is_buy
        self.type: List[BSP_TYPE] = [bs_type]
        self.relate_bsp1 = relate_bsp1

        self.bi.bsp = self  # type: ignore
        self.features = CFeatures(feature_dict)

        self.is_segbsp = False

        self.init_common_feature()

    def add_type(self, bs_type: BSP_TYPE):
        self.type.append(bs_type)

    def type2str(self):
        return ",".join([x.value for x in self.type])

    def add_another_bsp_prop(self, bs_type: BSP_TYPE, relate_bsp1):
        self.add_type(bs_type)
        if self.relate_bsp1 is None:
            self.relate_bsp1 = relate_bsp1
        elif relate_bsp1 is not None:
            assert self.relate_bsp1.klu.idx == relate_bsp1.klu.idx

    def add_feat(self, inp1: Union[str, Dict[str, float], Dict[str, Optional[float]], 'CFeatures'], inp2: Optional[float] = None):
        self.features.add_feat(inp1, inp2)

    def init_common_feature(self):
        # 用于配置适用所有买卖点的特征
        self.add_feat({
            'bsp_bi_amp': self.bi.amp(),
        })

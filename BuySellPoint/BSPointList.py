from datetime import datetime, timedelta
from typing import Dict, Generic, Iterable, List, Optional, Tuple, TypeVar

from Bi.Bi import CBi
from Bi.BiList import CBiList
from Common.CEnum import BSP_TYPE, KL_TYPE, MACD_ALGO
from Common.func_util import has_overlap
from Seg.Seg import CSeg
from Seg.SegListComm import CSegListComm
from ZS.ZS import CZS

from .BS_Point import CBS_Point
from .BSPointConfig import CBSPointConfig, CPointConfig

LINE_TYPE = TypeVar('LINE_TYPE', CBi, CSeg[CBi])
LINE_LIST_TYPE = TypeVar('LINE_LIST_TYPE', CBiList, CSegListComm[CBi])

# 区间套背驰判断（由 my_chan_main 在双窗口模式下调用）


class CBSPointList(Generic[LINE_TYPE, LINE_LIST_TYPE]):
    def __init__(self, bs_point_config: CBSPointConfig):
        self.bsp_store_dict: Dict[BSP_TYPE, Tuple[List[CBS_Point[LINE_TYPE]], List[CBS_Point[LINE_TYPE]]]] = {}
        self.bsp_store_flat_dict: Dict[Tuple[int, int], CBS_Point[LINE_TYPE]] = {}

        self.bsp11_list: List[CBS_Point[LINE_TYPE]] = []
        self.bsp11_dict: Dict[int, CBS_Point[LINE_TYPE]] = {}

        self.config = bs_point_config
        self.last_sure_pos = -1
        self.last_sure_seg_idx = 0

    def store_add_bsp(self, bsp_type: BSP_TYPE, bsp: CBS_Point[LINE_TYPE]):
        if bsp_type not in self.bsp_store_dict:
            self.bsp_store_dict[bsp_type] = ([], [])
        if len(self.bsp_store_dict[bsp_type][bsp.is_buy]) > 0:
            assert self.bsp_store_dict[bsp_type][bsp.is_buy][-1].bi.idx <= bsp.bi.idx, f"{bsp_type}, {bsp.is_buy} {self.bsp_store_dict[bsp_type][bsp.is_buy][-1].bi.idx} {bsp.bi.idx}"
        self.bsp_store_dict[bsp_type][bsp.is_buy].append(bsp)
        self.bsp_store_flat_dict[(bsp.bi.idx, bsp.klu.idx)] = bsp

    def add_bsp11(self, bsp: CBS_Point[LINE_TYPE]):
        if len(self.bsp11_list) > 0:
            assert self.bsp11_list[-1].bi.idx < bsp.bi.idx
        self.bsp11_list.append(bsp)
        self.bsp11_dict[bsp.bi.idx] = bsp

    def clear_store_end(self):
        for bsp_list in self.bsp_store_dict.values():
            for is_buy in [True, False]:
                while len(bsp_list[is_buy]) > 0:
                    if bsp_list[is_buy][-1].bi.get_end_klu().idx <= self.last_sure_pos:
                        break
                    bi_idx = bsp_list[is_buy][-1].bi.idx
                    flat_list = self.bsp_store_flat_dict.get(bi_idx, [])
                    if flat_list and flat_list[-1] is bsp_list[is_buy][-1]:
                        flat_list.pop()
                        if not flat_list:
                            del self.bsp_store_flat_dict[bi_idx]
                    # 同时把失效买卖点从Bi删除
                    bsp_list[is_buy][-1].bi.bsp = None
                    bsp_list[is_buy].pop()

    def clear_bsp11_end(self):
        while len(self.bsp11_list) > 0:
            if self.bsp11_list[-1].bi.get_end_klu().idx <= self.last_sure_pos:
                break
            del self.bsp11_dict[self.bsp11_list[-1].bi.idx]
            self.bsp11_list.pop()

    def bsp_iter(self) -> Iterable[CBS_Point[LINE_TYPE]]:
        for bsp_list in self.bsp_store_dict.values():
            yield from bsp_list[True]
            yield from bsp_list[False]

    def bsp_iter_v2(self) -> Iterable[CBS_Point[LINE_TYPE]]:
        list_indices = []
        for bsp_type, bsp_list in self.bsp_store_dict.items():
            if bsp_list[True]:
                list_indices.append([bsp_type, True, len(bsp_list[True]) - 1])
            if bsp_list[False]:
                list_indices.append([bsp_type, False, len(bsp_list[False]) - 1])

        while list_indices:
            max_idx = -1
            max_bi_idx = -1
            max_bsp = None

            for i, (bsp_type, is_buy, idx) in enumerate(list_indices):
                if idx >= 0:
                    bsp = self.bsp_store_dict[bsp_type][is_buy][idx]
                    if bsp.bi.idx > max_bi_idx:
                        max_bi_idx = bsp.bi.idx
                        max_idx = i
                        max_bsp = bsp

            if max_bsp is None:
                break

            yield max_bsp

            list_indices[max_idx][2] -= 1
            if list_indices[max_idx][2] < 0:
                list_indices.pop(max_idx)

    def __len__(self):
        return len(self.bsp_store_flat_dict)

    def _has_bsp_for_bi(self, bi_idx: int) -> bool:
        """检查是否存在以 bi_idx 为键的买卖点（flat_dict 键为 (bi.idx, klu.idx)）。"""
        return any(k[0] == bi_idx for k in self.bsp_store_flat_dict)

    def cal(self, bi_list: LINE_LIST_TYPE, seg_list: CSegListComm[LINE_TYPE]):
        self.clear_store_end()
        self.clear_bsp11_end()
        self.cal_seg_bs11point(seg_list, bi_list)
        self.cal_seg_bs22point(seg_list, bi_list)
        self.cal_seg_bs33point(seg_list, bi_list)

        self.update_last_pos(seg_list)

    def update_last_pos(self, seg_list: CSegListComm):
        self.last_sure_pos = -1
        self.last_sure_seg_idx = 0
        seg_idx = len(seg_list)-1
        while seg_idx >= 0:
            seg = seg_list[seg_idx]
            if seg.is_sure:
                self.last_sure_pos = seg.end_bi.get_begin_klu().idx
                self.last_sure_seg_idx = seg.idx
                return
            seg_idx -= 1

    def seg_need_cal(self, seg: CSeg):
        return seg.end_bi.get_end_klu().idx > self.last_sure_pos

    def add_bs(
        self,
        bs_type: BSP_TYPE,
        bi: LINE_TYPE,
        relate_bsp11: Optional[CBS_Point],
        is_target_bsp: bool = True,
        feature_dict=None,
    ):
        is_buy = bi.is_down()
        # 计算当前右肩K线位置，作为查找键的一部分
        end_klc = getattr(bi, 'end_klc', None)
        right_klc = getattr(end_klc, 'next', None) if end_klc else None
        cur_klu = right_klc.lst[-1] if right_klc and right_klc.lst else bi.get_end_klu()
        # 按 (bi.idx, klu.idx) 查找：同一笔同一K线位置 → 追加类型；否则 → 新建
        if exist_bsp := self.bsp_store_flat_dict.get((bi.idx, cur_klu.idx)):
            assert exist_bsp.is_buy == is_buy
            exist_bsp.add_another_bsp_prop(bs_type, relate_bsp11)
            if feature_dict is not None:
                exist_bsp.add_feat(feature_dict)
            return
        if bs_type not in self.config.GetBSConfig(is_buy).target_types:
            is_target_bsp = False

        if is_target_bsp or bs_type in [BSP_TYPE.T11, BSP_TYPE.T11P]:
            bsp = CBS_Point[LINE_TYPE](
                bi=bi,
                is_buy=is_buy,
                bs_type=bs_type,
                relate_bsp11=relate_bsp11,
                feature_dict=feature_dict,
            )
        else:
            return
        if is_target_bsp:
            self.store_add_bsp(bs_type, bsp)
        else:
            bsp.bi.bsp = None
        if bs_type in [BSP_TYPE.T11, BSP_TYPE.T11P]:
            self.add_bsp11(bsp)

    def cal_seg_bs11point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        for seg in seg_list[self.last_sure_seg_idx:]:
            if not self.seg_need_cal(seg):
                continue
            self.cal_single_bs11point(seg, bi_list)

    def cal_single_bs11point(self, seg: CSeg[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        BSP_CONF = self.config.GetBSConfig(seg.is_down())
        zs_cnt = seg.get_multi_bi_zs_cnt() if BSP_CONF.bsp11_only_multibi_zs else len(seg.zs_lst)
        is_target_bsp = (BSP_CONF.min_zs_cnt <= 0 or zs_cnt >= BSP_CONF.min_zs_cnt)
        if len(seg.zs_lst) > 0 and \
           not seg.zs_lst[-1].is_one_bi_zs() and \
           ((seg.zs_lst[-1].bi_out and seg.zs_lst[-1].bi_out.idx >= seg.end_bi.idx) or seg.zs_lst[-1].bi_lst[-1].idx >= seg.end_bi.idx) \
           and seg.end_bi.idx - seg.zs_lst[-1].get_bi_in().idx > 2:
            self.treat_bsp11(seg, BSP_CONF, is_target_bsp)
        else:
            self.treat_pz_bsp11(seg, BSP_CONF, bi_list, is_target_bsp)

    def treat_bsp11(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, is_target_bsp: bool):
        last_zs = seg.zs_lst[-1]
        break_peak, _ = last_zs.out_bi_is_peak(seg.end_bi.idx)
        if BSP_CONF.bs11_peak and not break_peak:
            is_target_bsp = False
        is_diver, divergence_rate = last_zs.is_divergence(BSP_CONF, out_bi=seg.end_bi)
        if not is_diver:
            is_target_bsp = False
        feature_dict = {
            'divergence_rate': divergence_rate,
            'zs_cnt': len(seg.zs_lst),
        }
        self.add_bs(bs_type=BSP_TYPE.T11, bi=seg.end_bi, relate_bsp11=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict)

    def treat_pz_bsp11(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, bi_list: LINE_LIST_TYPE, is_target_bsp):
        last_bi = seg.end_bi
        pre_bi = bi_list[last_bi.idx-2]
        if last_bi.seg_idx != pre_bi.seg_idx:
            return
        if last_bi.dir != seg.dir:
            return
        if last_bi.is_down() and last_bi._low() > pre_bi._low():  # 创新低
            return
        if last_bi.is_up() and last_bi._high() < pre_bi._high():  # 创新高
            return
        in_metric = pre_bi.cal_macd_metric(BSP_CONF.macd_algo, is_reverse=False)
        out_metric = last_bi.cal_macd_metric(BSP_CONF.macd_algo, is_reverse=True)
        is_diver, divergence_rate = out_metric <= BSP_CONF.divergence_rate*in_metric, out_metric/(in_metric+1e-7)
        if not is_diver:
            is_target_bsp = False
        if isinstance(bi_list, CBiList):
            assert isinstance(last_bi, CBi) and isinstance(pre_bi, CBi)
        feature_dict = {
            'divergence_rate': divergence_rate,
            'bsp11_bi_amp': last_bi.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T11P, bi=last_bi, relate_bsp11=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict)

    def cal_seg_bs22point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        for seg in seg_list[self.last_sure_seg_idx:]:
            config = self.config.GetBSConfig(seg.is_down())
            if BSP_TYPE.T22 not in config.target_types and BSP_TYPE.T22S not in config.target_types:
                continue
            if not self.seg_need_cal(seg):
                continue
            self.treat_bsp22(seg, seg_list, bi_list)

    def treat_bsp22(self, seg: CSeg, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        if len(seg_list) > 1:
            BSP_CONF = self.config.GetBSConfig(seg.is_down())
            bsp11_bi = seg.end_bi
            real_bsp11 = self.bsp11_dict.get(bsp11_bi.idx)
            if bsp11_bi.idx + 2 >= len(bi_list):
                return
            break_bi = bi_list[bsp11_bi.idx + 1]
            bsp22_bi = bi_list[bsp11_bi.idx + 2]
        else:
            BSP_CONF = self.config.GetBSConfig(seg.is_up())
            bsp11_bi, real_bsp11 = None, None
            if len(bi_list) == 1:
                return
            bsp22_bi = bi_list[1]
            break_bi = bi_list[0]
        if BSP_CONF.bsp22_follow_11 and (not bsp11_bi or not self._has_bsp_for_bi(bsp11_bi.idx)):
            return
        retrace_rate = bsp22_bi.amp()/break_bi.amp()
        bsp22_flag = retrace_rate <= BSP_CONF.max_bs22_rate
        if bsp22_flag:
            feature_dict = {
                'bsp22_retrace_rate': retrace_rate,
                'bsp22_break_bi_amp': break_bi.amp(),
                'bsp22_bi_amp': bsp22_bi.amp(),
            }
            self.add_bs(bs_type=BSP_TYPE.T22, bi=bsp22_bi, relate_bsp11=real_bsp11, feature_dict=feature_dict)  # type: ignore
        elif BSP_CONF.bsp22s_follow_22:
            return
        if BSP_TYPE.T22S not in self.config.GetBSConfig(seg.is_down()).target_types:
            return
        self.treat_bsp22s(seg_list, bi_list, bsp22_bi, break_bi, real_bsp11, BSP_CONF)  # type: ignore

    def treat_bsp22s(
        self,
        seg_list: CSegListComm,
        bi_list: LINE_TYPE,
        bsp22_bi: LINE_TYPE,
        break_bi: LINE_TYPE,
        real_bsp11: Optional[CBS_Point],
        BSP_CONF: CPointConfig,
    ):
        bias = 2
        _low, _high = None, None
        while bsp22_bi.idx + bias < len(bi_list):  # 计算类22
            bsp22s_bi = bi_list[bsp22_bi.idx + bias]
            assert bsp22s_bi.seg_idx is not None and bsp22_bi.seg_idx is not None
            if BSP_CONF.max_bsp22s_lv is not None and bias/2 > BSP_CONF.max_bsp22s_lv:
                break
            if bsp22s_bi.seg_idx != bsp22_bi.seg_idx and (bsp22s_bi.seg_idx < len(seg_list)-1 or bsp22s_bi.seg_idx - bsp22_bi.seg_idx >= 2 or seg_list[bsp22_bi.seg_idx].is_sure):
                break
            if bias == 2:
                if not has_overlap(bsp22_bi._low(), bsp22_bi._high(), bsp22s_bi._low(), bsp22s_bi._high()):
                    break
                _low = max([bsp22_bi._low(), bsp22s_bi._low()])
                _high = min([bsp22_bi._high(), bsp22s_bi._high()])
            elif not has_overlap(_low, _high, bsp22s_bi._low(), bsp22s_bi._high()):
                break

            if bsp22s_break_bsp11(bsp22s_bi, break_bi):
                break
            retrace_rate = abs(bsp22s_bi.get_end_val()-break_bi.get_end_val())/break_bi.amp()
            if retrace_rate > BSP_CONF.max_bs22_rate:
                break
            feature_dict = {
                'bsp22s_retrace_rate': retrace_rate,
                'bsp22s_break_bi_amp': break_bi.amp(),
                'bsp22s_bi_amp': bsp22s_bi.amp(),
                'bsp22s_lv': bias/2,
            }
            self.add_bs(bs_type=BSP_TYPE.T22S, bi=bsp22s_bi, relate_bsp11=real_bsp11, feature_dict=feature_dict)  # type: ignore
            bias += 2

    def cal_seg_bs33point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        for seg in seg_list[self.last_sure_seg_idx:]:
            if not self.seg_need_cal(seg):
                continue
            config = self.config.GetBSConfig(seg.is_down())
            if BSP_TYPE.T33A not in config.target_types and BSP_TYPE.T33B not in config.target_types:
                continue
            if len(seg_list) > 1:
                bsp11_bi = seg.end_bi
                bsp11_bi_idx = bsp11_bi.idx
                BSP_CONF = self.config.GetBSConfig(seg.is_down())
                real_bsp11 = self.bsp11_dict.get(bsp11_bi.idx)
                next_seg_idx = seg.idx+1
                next_seg = seg.next  # 可能为None, 所以并不一定可以保证next_seg_idx == next_seg.idx
            else:
                next_seg = seg
                next_seg_idx = seg.idx
                bsp11_bi, real_bsp11 = None, None
                bsp11_bi_idx = -1
                BSP_CONF = self.config.GetBSConfig(seg.is_up())
            if BSP_CONF.bsp33_follow_11 and (not bsp11_bi or not self._has_bsp_for_bi(bsp11_bi.idx)):
                continue
            if next_seg:
                self.treat_bsp33_after(seg_list, next_seg, BSP_CONF, bi_list, real_bsp11, bsp11_bi_idx, next_seg_idx)
            self.treat_bsp33_before(seg_list, seg, next_seg, bsp11_bi, BSP_CONF, bi_list, real_bsp11, next_seg_idx)

    def treat_bsp33_after(
        self,
        seg_list: CSegListComm[LINE_TYPE],
        next_seg: CSeg[LINE_TYPE],
        BSP_CONF: CPointConfig,
        bi_list: LINE_LIST_TYPE,
        real_bsp11,
        bsp11_bi_idx,
        next_seg_idx
    ):
        first_zs = next_seg.get_first_multi_bi_zs()
        if first_zs is None:
            return
        if BSP_CONF.strict_bsp33 and first_zs.get_bi_in().idx != bsp11_bi_idx+1:
            return

        config = self.config.GetBSConfig(next_seg.is_down())
        bsp3a_max_zs_cnt = config.bsp33a_max_zs_cnt
        for zs_idx, zs in enumerate(next_seg.get_multi_bi_zs_lst()):
            if zs_idx >= bsp3a_max_zs_cnt:
                break
            if zs.bi_out is None or zs.bi_out.idx+1 >= len(bi_list):
                break
            bsp33_bi = bi_list[zs.bi_out.idx+1]
            if bsp33_bi.parent_seg is None:
                if next_seg.idx != len(seg_list)-1:
                    break
            elif bsp33_bi.parent_seg.idx != next_seg.idx:
                if len(bsp33_bi.parent_seg.bi_list) >= 3:
                    break
            if bsp33_bi.dir == next_seg.dir:
                break
            if bsp33_bi.seg_idx != next_seg_idx and next_seg_idx < len(seg_list)-2:
                break
            if bsp33_back2zs(bsp33_bi, zs):
                continue
            bsp3_peak_zs = bsp33_break_zspeak(bsp33_bi, zs)
            if BSP_CONF.bsp33_peak and not bsp3_peak_zs:
                continue
            feature_dict = {
                'bsp33_zs_height': (zs.high - zs.low)/zs.low,
                'bsp33_bi_amp': bsp33_bi.amp(),
            }
            self.add_bs(bs_type=BSP_TYPE.T33A, bi=bsp33_bi, relate_bsp11=real_bsp11, feature_dict=feature_dict)  # type: ignore

    def treat_bsp33_before(
        self,
        seg_list: CSegListComm[LINE_TYPE],
        seg: CSeg[LINE_TYPE],
        next_seg: Optional[CSeg[LINE_TYPE]],
        bsp11_bi: Optional[LINE_TYPE],
        BSP_CONF: CPointConfig,
        bi_list: LINE_LIST_TYPE,
        real_bsp11,
        next_seg_idx
    ):
        cmp_zs = seg.get_final_multi_bi_zs()
        if cmp_zs is None:
            return
        if not bsp11_bi:
            return
        if BSP_CONF.strict_bsp33 and (cmp_zs.bi_out is None or cmp_zs.bi_out.idx != bsp11_bi.idx):
            return
        end_bi_idx = cal_bsp33_bi_end_idx(next_seg)
        for bsp33_bi in bi_list[bsp11_bi.idx+2::2]:
            if bsp33_bi.idx > end_bi_idx:
                break
            assert bsp33_bi.seg_idx is not None
            if bsp33_bi.seg_idx != next_seg_idx and bsp33_bi.seg_idx < len(seg_list)-1:
                break
            if bsp33_back2zs(bsp33_bi, cmp_zs):  # type: ignore
                continue
            feature_dict = {
                'bsp33_zs_height': (cmp_zs.high - cmp_zs.low)/cmp_zs.low,
                'bsp33_bi_amp': bsp33_bi.amp(),
            }
            self.add_bs(bs_type=BSP_TYPE.T33B, bi=bsp33_bi, relate_bsp11=real_bsp11, feature_dict=feature_dict)  # type: ignore
            break

    def getSortedBspList(self) -> List[CBS_Point[LINE_TYPE]]:
        return sorted(self.bsp_iter(), key=lambda bsp: bsp.bi.idx)

    def get_latest_bsp(self, number: int) -> List[CBS_Point[LINE_TYPE]]:
        res = []
        for bsp in self.bsp_iter_v2():
            res.append(bsp)
            if number != 0 and len(res) >= number:
                break
        return res


def bsp22s_break_bsp11(bsp22s_bi: LINE_TYPE, bsp22_break_bi: LINE_TYPE) -> bool:
    return (bsp22s_bi.is_down() and bsp22s_bi._low() < bsp22_break_bi._low()) or \
           (bsp22s_bi.is_up() and bsp22s_bi._high() > bsp22_break_bi._high())


def bsp33_back2zs(bsp33_bi: LINE_TYPE, zs: CZS) -> bool:
    return (bsp33_bi.is_down() and bsp33_bi._low() < zs.high) or (bsp33_bi.is_up() and bsp33_bi._high() > zs.low)


def bsp33_break_zspeak(bsp33_bi: LINE_TYPE, zs: CZS) -> bool:
    return (bsp33_bi.is_down() and bsp33_bi._high() >= zs.peak_high) or (bsp33_bi.is_up() and bsp33_bi._low() <= zs.peak_low)


def cal_bsp33_bi_end_idx(seg: Optional[CSeg[LINE_TYPE]]):
    if not seg:
        return float("inf")
    if seg.get_multi_bi_zs_cnt() == 0 and seg.next is None:
        return float("inf")
    end_bi_idx = seg.end_bi.idx-1
    for zs in seg.zs_lst:
        if zs.is_one_bi_zs():
            continue
        if zs.bi_out is not None:
            end_bi_idx = zs.bi_out.idx
            break
    return end_bi_idx


# ============================================================
# 自定义买卖点列表（继承 CBSPointList，替换 cal 内部调用）
# ============================================================
# ═══════════════════════════════════════════════════════════
# 区间套背驰判断
# 上下文通过 self.parent（CKLine_List 反向引用）获取，
# code 和 kl_type 是 CKLine_List 创建时就有的固有属性。
# sub_freq 由 freq 自动推导（双窗口配对固定）。
# ═══════════════════════════════════════════════════════════

# KL_TYPE → freq 字符串映射
_KL_TYPE_TO_FREQ = {
    KL_TYPE.K_15S: '15s', KL_TYPE.K_1M: '1m', KL_TYPE.K_5M: '5m',
    KL_TYPE.K_30M: '30m', KL_TYPE.K_60M: '60m', KL_TYPE.K_DAY: 'd',
    KL_TYPE.K_WEEK: 'w',
}

# freq 字符串 → KL_TYPE 枚举反向映射
_FREQ_TO_KL_TYPE = {
    '15s': KL_TYPE.K_15S, '1m': KL_TYPE.K_1M, '5m': KL_TYPE.K_5M,
    '30m': KL_TYPE.K_30M, '60m': KL_TYPE.K_60M, 'd': KL_TYPE.K_DAY,
    'w': KL_TYPE.K_WEEK,
}

def _get_kl_type(freq):
    """根据频率字符串返回对应的 KL_TYPE 枚举值"""
    return _FREQ_TO_KL_TYPE.get(freq, KL_TYPE.K_DAY)

# 双窗口 freq 配对（上窗→下窗）
_STOCK_SUB_FREQ_MAP = {'w': 'd', 'd': '30m', '30m': '5m'}
_FUTURES_SUB_FREQ_MAP = {'30m': '5m', '5m': '1m', '1m': '15s'}


class MyBSPointList(CBSPointList[LINE_TYPE, LINE_LIST_TYPE]):
    """自定义买卖点计算类。

    继承 CBSPointList，将 cal() 中的调用替换为 cal_bs0point ~ cal_bs3point。
    你只需修改下面四个方法即可实现自己的买卖点逻辑。

    与线段（seg_list）完全解耦，只依赖笔列表（bi_list）和笔中枢列表（zs_list）。
    0类买卖点已通过 BSP_TYPE.T0 枚举正规化，无需额外 hack。

    self.parent 是父级 CKLine_List 的反向引用，从父级获取 code 和 kl_type。
    """

    def __init__(self, bs_point_config):
        super().__init__(bs_point_config)
        # 反向引用父级 CKLine_List，由 CKLine_List.__init__ 设置
        self.parent = None  # 指向 CKLine_List

    # ── 入口 ──
    def cal(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        self.check_nested_diver(bi_list, zs_list)
        self.cal_bs0point(bi_list, zs_list)
        self.cal_bs3point(bi_list, zs_list)
        self.cal_bs1point(bi_list, zs_list)
        self.cal_bs2point(bi_list, zs_list)

    def check_nested_diver(self, bi_list, zs_list):
        """
        区间套背驰判断：分析主级别一笔在子级别是否段背(无中枢) 或 有买/卖点(有中枢)
        code、freq 从 self.parent 获取（CKLine_List 创建时就有的固有属性）
        sub_freq 由 freq 自动推导（双窗口配对固定）
        """
        parent = self.parent # 指向 CKLine_List
        if parent is None:
            raise RuntimeError("[check_nested_diver] 严重Bug：指向 CKLine_List 的指针未设置！")

        kl_type = parent.kl_type  # KL_TYPE 枚举
        main_freq = _KL_TYPE_TO_FREQ.get(kl_type)
        if main_freq is None:
            raise RuntimeError("[check_nested_diver] 严重Bug：主级别映射缺失")

        # 获取 market_type
        market_type = getattr(parent, 'market_type', None)
        if market_type is None:
            raise RuntimeError("[check_nested_diver] 严重Bug：market_type 未设置！")

        is_stocks = (market_type == "stock")
        # 双窗口 freq 配对：上窗 freq 确定下窗 freq
        if is_stocks:
            sub_freq = _STOCK_SUB_FREQ_MAP.get(main_freq)
        else:
            sub_freq = _FUTURES_SUB_FREQ_MAP.get(main_freq)

        # 从同一 CChan 中直接获取子级别数据
        if parent.chan is None:
            raise RuntimeError("[check_nested_diver] 严重Bug：指针 CChan 的指针未设置！")

        chan = parent.chan
        sub_kl_type = _get_kl_type(sub_freq)
        try:
            # 此处 sub_kl_list 指某级别整个K线列表对象，包含该级别的所有 合并K线、bi_list、zs_list等
            sub_kl_list = chan[sub_kl_type]
        except Exception:
            # print(f"[check_nested_diver] 单窗口无子 or 双窗口无孙: {sub_kl_type} → 按背驰处理！！！")
            return True

        # print(f"[check_nested_diver] 市场类型={market_type}, code={parent.code}, 主周期={main_freq}, 子周期={sub_freq}")

        sub_bi_list = sub_kl_list.bi_list
        if len(sub_bi_list) == 0:
            # 上/下窗，历史K线不对齐(如：日K加载多于30分)
            # print(f"[check_nested_diver] 双窗口-无子级别 → 按背驰处理！！！")
            return True


        # 1. 确定主级别一笔的左右边界 [A,B] 及 KLU 对象
        main_bi = bi_list[-1]
        main_date_fmt = _get_date_fmt(main_freq)
        shoulder_result = _get_main_bi_time_range(main_bi, main_date_fmt)
        if shoulder_result is None:
            raise RuntimeError(f"[check_nested_diver] 严重Bug：无法确定主级别[A,B]: main_bi.idx={main_bi.idx}")
        fx_a_raw_dt, fx_b_raw_dt, a_klu, b_klu = shoulder_result

        # 2. 确定子级别红框边界 [C,D]
        fx_a_sub_dt, fx_b_sub_dt = None, None
        if is_stocks:
            fx_a_sub_dt, fx_b_sub_dt = _stocks_red_range(a_klu, b_klu, sub_freq, main_bi)
        else:
            from DataAPI.TqSdkAPI import FREQ_SEC_MAP
            top_freq_sec = FREQ_SEC_MAP[main_freq]
            sub_freq_sec = FREQ_SEC_MAP[sub_freq]
            snapshot = {'bis': [{'fx_a_raw_dt': fx_a_raw_dt, 'fx_b_raw_dt': fx_b_raw_dt}]}
            _futures_red_range(snapshot, top_freq_sec, sub_freq_sec, sub_freq)
            fx_a_sub_dt = snapshot['bis'][0].get('fx_a_sub_dt', '')
            fx_b_sub_dt = snapshot['bis'][0].get('fx_b_sub_dt', '')

        if not fx_a_sub_dt or not fx_b_sub_dt:
            raise RuntimeError(f"[check_nested_diver] 严重Bug：无法确定子级别[C,D]: fx_a='{fx_a_sub_dt}', fx_b='{fx_b_sub_dt}'")

        # 3. 在子级别笔列表中找被红框完全覆盖的笔
        start_bi_idx, end_bi_idx = _find_sub_bi_sequence(fx_a_sub_dt, fx_b_sub_dt, sub_bi_list, sub_freq)
        if start_bi_idx is None or end_bi_idx is None:
            raise RuntimeError("[check_nested_diver] 严重Bug：找不到被红框[C,D]覆盖的笔")

        sub_bi_sliced = list(sub_bi_list[start_bi_idx:end_bi_idx + 1])
        bi_count = len(sub_bi_sliced)
        print(f"[check_nested_diver] 子级别笔范围: 索引[{start_bi_idx} ~ {end_bi_idx}], 共{bi_count}笔")

        # 4. 判断子级别笔序列是否形成中枢
        sub_date_fmt = _get_date_fmt(sub_freq)
        zs_data = _check_red_range_zs(sub_bi_sliced, sub_bi_list, sub_date_fmt)
        has_zs = len(zs_data) > 0

        if has_zs:
            # 场景二：笔序列形成中枢（1个或多个）
            # 从sub_bi_sliced末尾往前找，最多找2笔与主级别笔同向的笔
            # 如果其中任何一笔有对应的买卖点，则认为背驰
            main_dir_up = main_bi.is_up()
            same_dir_count = 0
            for bi in reversed(sub_bi_sliced):
                if bi.is_up() == main_dir_up:
                    same_dir_count += 1
                    bsp = getattr(bi, 'bsp', None)
                    if bsp is not None:
                        # 向上主笔→子级别卖点(is_buy=False)；向下主笔→子级别买点(is_buy=True)
                        if bsp.is_buy == (not main_dir_up):
                            detail = f"子级别{len(zs_data)}个中枢，同向笔bi[{bi.idx}]有{'买' if bsp.is_buy else '卖'}点"
                            print(f"[check_nested_diver] {detail} → 背驰")
                            return True
                    if same_dir_count >= 2:
                        break
            # 同向笔无对应买卖点 → 不背驰
            print(f"[check_nested_diver] 子级别{len(zs_data)}个中枢，同向笔无对应买卖点 → 不背驰")
            return False

        # 场景一：笔序列不形成任何中枢
        if bi_count == 1:
            # 子场景⑴：只有一笔
            result = _check_sub_single_bi_diver(sub_bi_sliced[0])
            print(f"[check_nested_diver] 单笔背驰判断: {result['detail']}")
            return result['diverged']
        else:
            # 子场景⑵：有多笔但不形成中枢
            result = _check_sub_multi_bi_diver(sub_bi_sliced)
            print(f"[check_nested_diver] 多笔无中枢背驰判断: {result['detail']}")
            return result['diverged']

    # ═══════════════════════════════════════════════════════════
    # ── 0类买卖点(中枢震荡) ──
    # ═══════════════════════════════════════════════════════════
    def cal_bs0point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        # ── 共用检查 ──
        result = self._get_pivot_and_cur_bi(bi_list, zs_list)
        if result is None:
            return
        pivot_a, stroke_n = result
        if stroke_n.idx < pivot_a.begin_bi.idx + 2:
            return

        # ── 第3笔(C点) or 第n笔（n≥4）──
        if stroke_n.idx == pivot_a.begin_bi.idx + 2:
            self._cal_bs0point_3rd(bi_list, pivot_a, stroke_n)
        else:
            self._cal_bs0point_nth(bi_list, pivot_a, stroke_n)

    # ── 第3笔（中枢A形成笔）──
    def _cal_bs0point_3rd(self, bi_list, pivot_a, stroke_n):
        # 分型MACD拐头判断（右肩 vs 中间），MACD值取自合并K线内最后一根原始K线（klc.lst[-1].macd）
        if not self._check_fx_macd_inflection_point(stroke_n, check_zero_axis=False):
            return

        # MACD峰值背驰（PEAK）
        # 向下笔只取绿柱(MACD<0)的绝对值最大值
        # 向上笔只取红柱(MACD>0)的绝对值最大值
        # 没有对应颜色柱子时返回 1e-7（≈0）
        stroke_1 = bi_list[pivot_a.begin_bi.idx]  # 中枢A的第1笔
        in_metric = stroke_1.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=False) # is_reverse 仅在 MACD_ALGO.AREA 时有用
        out_metric = stroke_n.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=True)
        divergence_rate = out_metric / (in_metric + 1e-7)

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        is_diver = out_metric <= config.divergence_rate * in_metric

        if not is_diver:
            return

        feature_dict = {
            'divergence_rate': divergence_rate,
            'bsp0_bi_amp': stroke_n.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T0, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    # ── 第n笔（n≥4）──
    def _cal_bs0point_nth(self, bi_list, pivot_a, stroke_n):
        # 笔n与中枢A有重叠
        if not has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high):
            return

        # 向上笔高点 >= 中枢A上沿
        # 向下笔低点 <= 中枢A下沿
        if stroke_n.is_up():
            if stroke_n._high() < pivot_a.high:
                return
        else:
            if stroke_n._low() > pivot_a.low:
                return

        # 分型MACD拐头判断（右肩 vs 中间），MACD值取自合并K线内最后一根原始K线（klc.lst[-1].macd）
        if not self._check_fx_macd_inflection_point(stroke_n):
            return

        # ⑴ 主分析：第n笔（n≥4）分析是否有买卖点
        bsp_found = self._cal_bs0point_nth_1st(bi_list, pivot_a, stroke_n)

        # ⑵ 如果主分析没找到买卖点，且n=6或8，用中枢A进入段做MACD面积分析
        if not bsp_found:
            nth_in_pivot = stroke_n.idx - pivot_a.begin_bi.idx + 1
            if nth_in_pivot == 6 or nth_in_pivot == 8:
                self._cal_bs0point_nth_2nd(bi_list, pivot_a, stroke_n)

    # ── 第n笔主分析逻辑（返回是否找到买卖点）──
    def _cal_bs0point_nth_1st(self, bi_list, pivot_a, stroke_n):
        """
        分析逻辑：基于中枢B（n-3, n-2, n-1构成）做背驰判断
        返回 True 表示找到买卖点，False 表示未找到
        """
        # n-3, n-2, n-1 重叠 -> 中枢B
        n_idx = stroke_n.idx
        s_nm1 = bi_list[n_idx - 1]  # 笔n-1
        s_nm2 = bi_list[n_idx - 2]  # 笔n-2
        s_nm3 = bi_list[n_idx - 3]  # 笔n-3
        s_nm4 = bi_list[n_idx - 4]  # 笔n-4（中枢B的进入笔）

        # 三笔有无重叠，是否构成中枢B
        zs_b_low = max(s_nm1._low(), s_nm2._low(), s_nm3._low())
        zs_b_high = min(s_nm1._high(), s_nm2._high(), s_nm3._high())
        if zs_b_low > zs_b_high:
            return False

        # 笔n末端 和 笔n-4始端 要在中枢B外
        if stroke_n.is_up():
            # 向上笔n：末端高点 > 中枢B上沿
            if stroke_n._high() <= zs_b_high:
                return False
            # n-4（进入笔）：末端低点 < 中枢B下沿
            if s_nm4._low() >= zs_b_low:
                return False
        else:
            # 向下笔n：末端低点 < 中枢B下沿
            if stroke_n._low() >= zs_b_low:
                return False
            # n-4（进入笔）：末端高点 > 中枢B上沿
            if s_nm4._high() <= zs_b_high:
                return False

        # MACD背驰判断（full_area）
        # 进入笔：n-4，离开笔：n
        in_metric = s_nm4.cal_macd_metric(MACD_ALGO.FULL_AREA, is_reverse=False) # is_reverse 仅在 MACD_ALGO.AREA 时有用
        out_metric = stroke_n.cal_macd_metric(MACD_ALGO.FULL_AREA, is_reverse=True)
        divergence_rate = out_metric / (in_metric + 1e-7)

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        is_diver = out_metric <= config.divergence_rate * in_metric

        if not is_diver:
            return False

        # ── 生成0类买卖点 ──
        feature_dict = {
            'divergence_rate': divergence_rate,
            'bsp0_bi_amp': stroke_n.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T0, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)
        return True

    # ── 第n笔再次分析：MACD全面积比较（中枢A进入段 vs 笔n）──
    def _cal_bs0point_nth_2nd(self, bi_list, pivot_a, stroke_n):
        """
        分析逻辑：当n=6或8时，主分析未找到买卖点，改用中枢A进入段与笔n做MACD全面积比较
        进入段为 pivot_a.begin_bi 的前一笔，离开段为 stroke_n
        """

        # MACD全面积比较
        entry_bi = bi_list[pivot_a.begin_bi.idx - 1]
        in_metric = entry_bi.cal_macd_metric(MACD_ALGO.FULL_AREA, is_reverse=False) # is_reverse 仅在 MACD_ALGO.AREA 时有用
        out_metric = stroke_n.cal_macd_metric(MACD_ALGO.FULL_AREA, is_reverse=True)
        divergence_rate = out_metric / (in_metric + 1e-7)

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        is_diver = out_metric <= config.divergence_rate * in_metric

        if not is_diver:
            return

        # ── 生成0类买卖点 ──
        feature_dict = {
            'divergence_rate': divergence_rate,
            'bsp0_bi_amp': stroke_n.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T0, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    # ═══════════════════════════════════════════════════════════
    # ── 1类买卖点（对应缠论一类买卖点）──
    # ═══════════════════════════════════════════════════════════
    def cal_bs1point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        # ── 共用检查 ──
        result = self._get_pivot_and_cur_bi(bi_list, zs_list)
        if result is None:
            return
        pivot_a, stroke_n = result

        # 笔N和N-1不跟中枢重叠，但笔N-2重叠
        stroke_nm1 = bi_list[stroke_n.idx - 1]  # 前一笔N-1
        stroke_nm2 = bi_list[stroke_n.idx - 2]  # 前二笔N-2
        n_overlap = has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high)
        nm1_overlap = has_overlap(stroke_nm1._low(), stroke_nm1._high(), pivot_a.low, pivot_a.high)
        nm2_overlap = has_overlap(stroke_nm2._low(), stroke_nm2._high(), pivot_a.low, pivot_a.high)

        if n_overlap or nm1_overlap or not nm2_overlap:
            return

        # 笔N的极值必须突破笔N-2的极值
        # 向下笔（买点）：笔N低点 <= 笔N-2低点（创新低）
        # 向上笔（卖点）：笔N高点 >= 笔N-2高点（创新高）
        if stroke_n.is_down() and stroke_n._low() > stroke_nm2._low():
            return
        if stroke_n.is_up() and stroke_n._high() < stroke_nm2._high():
            return

        # 笔N两端的高度差 >= 笔N-2两端的高度差
        if stroke_n.amp() < stroke_nm2.amp():
            return

        # 分型MACD拐头判断（右肩 vs 中间），MACD值取自合并K线内最后一根原始K线（klc.lst[-1].macd）
        if not self._check_fx_macd_inflection_point(stroke_n):
            return

        # 笔N-1上没有相反方向的买卖点
        # 笔N向下（买点）→ 笔N-1向上 → 笔N-1上不能有卖点
        # 笔N向上（卖点）→ 笔N-1向下 → 笔N-1上不能有买点
        if self._has_bsp_for_bi(stroke_nm1.idx):
            return

        # 笔N与笔N-2的MACD峰值（PEAK）背驰判断
        # 向下笔只取绿柱(MACD<0)的绝对值最大值
        # 向上笔只取红柱(MACD>0)的绝对值最大值
        # 没有对应颜色柱子时返回 1e-7（≈0）
        in_metric = stroke_nm2.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=False) # is_reverse 仅在 MACD_ALGO.AREA 时有用
        out_metric = stroke_n.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=True)
        divergence_rate = out_metric / (in_metric + 1e-7)

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        is_diver = out_metric <= config.divergence_rate * in_metric

        if not is_diver:
            return

        # ── 生成1类买卖点 ──
        feature_dict = {
            'bsp1_bi_amp': stroke_n.amp(),
            'divergence_rate': divergence_rate,
        }
        self.add_bs(bs_type=BSP_TYPE.T1, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    # ═══════════════════════════════════════════════════════════
    # ── 2类买卖点 ──
    # ═══════════════════════════════════════════════════════════
    def cal_bs2point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        # ── 共用检查 ──
        result = self._get_pivot_and_cur_bi(bi_list, zs_list)
        if result is None:
            return
        pivot_a, stroke_n = result

        # 保护性检查：访问 stroke_n.idx - 6 需要至少6笔
        if stroke_n.idx < 6:
            return

        # 笔N，N-1，N-2，N-3，N-4，N-5都不跟中枢重叠，笔N-6跟中枢重叠
        stroke_nm1 = bi_list[stroke_n.idx - 1]  # 前一笔N-1
        stroke_nm2 = bi_list[stroke_n.idx - 2]  # 前二笔N-2
        stroke_nm3 = bi_list[stroke_n.idx - 3]  # 前三笔N-3
        stroke_nm4 = bi_list[stroke_n.idx - 4]  # 前四笔N-4
        stroke_nm5 = bi_list[stroke_n.idx - 5]  # 前五笔N-5
        stroke_nm6 = bi_list[stroke_n.idx - 6]  # 前六笔N-6
        n_overlap = has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high)
        nm1_overlap = has_overlap(stroke_nm1._low(), stroke_nm1._high(), pivot_a.low, pivot_a.high)
        nm2_overlap = has_overlap(stroke_nm2._low(), stroke_nm2._high(), pivot_a.low, pivot_a.high)
        nm3_overlap = has_overlap(stroke_nm3._low(), stroke_nm3._high(), pivot_a.low, pivot_a.high)
        nm4_overlap = has_overlap(stroke_nm4._low(), stroke_nm4._high(), pivot_a.low, pivot_a.high)
        nm5_overlap = has_overlap(stroke_nm5._low(), stroke_nm5._high(), pivot_a.low, pivot_a.high)
        nm6_overlap = has_overlap(stroke_nm6._low(), stroke_nm6._high(), pivot_a.low, pivot_a.high)

        if n_overlap or nm1_overlap or nm2_overlap or nm3_overlap or nm4_overlap or nm5_overlap or not nm6_overlap:
            return

        # 笔N的末端极值不破笔N-2的极值（二买不创新低 / 二卖不创新高）
        # 向下笔（二买）：笔N低点 >= 笔N-2低点
        # 向上笔（二卖）：笔N高点 <= 笔N-2高点
        if stroke_n.is_down() and stroke_n._low() < stroke_nm2._low():
            return
        if stroke_n.is_up() and stroke_n._high() > stroke_nm2._high():
            return

        # 分型MACD拐头判断（右肩 vs 中间），MACD值取自合并K线内最后一根原始K线（klc.lst[-1].macd）
        if not self._check_fx_macd_inflection_point(stroke_n):
            return

        # 笔N-2上有买卖点
        # 笔N向下（二买）→ 笔N-2向下 → 笔N-2上要有一买点
        # 笔N向上（二卖）→ 笔N-2向上 → 笔N-2上要有一卖点
        if not self._has_bsp_for_bi(stroke_nm2.idx):
            return

        # 笔N与笔N-2的MACD峰值（PEAK）背驰判断
        # 向下笔只取绿柱(MACD<0)的绝对值最大值
        # 向上笔只取红柱(MACD>0)的绝对值最大值
        # 没有对应颜色柱子时返回 1e-7（≈0）
        in_metric = stroke_nm2.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=False)
        out_metric = stroke_n.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=True)
        divergence_rate = out_metric / (in_metric + 1e-7)

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        is_diver = out_metric <= config.divergence_rate * in_metric

        if not is_diver:
            return

        # ── 生成2类买卖点 ──
        feature_dict = {
            'bsp2_bi_amp': stroke_n.amp(),
            'divergence_rate': divergence_rate,
        }
        self.add_bs(bs_type=BSP_TYPE.T2, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    # ═══════════════════════════════════════════════════════════
    # ── 3类买卖点 ──
    # ═══════════════════════════════════════════════════════════
    def cal_bs3point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        # ── 共用检查 ──
        result = self._get_pivot_and_cur_bi(bi_list, zs_list)
        if result is None:
            return
        pivot_a, stroke_n = result

        # 笔N不跟最后一个中枢重叠，但笔N-1重叠
        stroke_nm1 = bi_list[stroke_n.idx - 1]  # 前一笔N-1
        n_overlap = has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high)
        nm1_overlap = has_overlap(stroke_nm1._low(), stroke_nm1._high(), pivot_a.low, pivot_a.high)

        if n_overlap or not nm1_overlap:
            return

        # 分型MACD拐头判断（右肩 vs 中间），MACD值取自合并K线内最后一根原始K线（klc.lst[-1].macd）
        if not self._check_fx_macd_inflection_point(stroke_n):
            return

        # 笔N-1上没有相反方向的买卖点
        # 笔N向下（买点）→ 笔N-1向上 → 笔N-1上不能有卖点
        # 笔N向上（卖点）→ 笔N-1向下 → 笔N-1上不能有买点
        if self._has_bsp_for_bi(stroke_nm1.idx):
            return

        # ── 生成3类买卖点 ──
        feature_dict = {
            'bsp3_bi_amp': stroke_n.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T3, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    @staticmethod
    def _get_pivot_and_cur_bi(bi_list, zs_list):
        """
        ⑴ zs_list 为空？→ None
        ⑵ 找不到多笔中枢（最后一个中枢）？→ None
        ⑶ 当下笔是虚笔？→ None
        返回值: (pivot_a, stroke_n) 或 None
        """
        if zs_list is None or len(zs_list) == 0:
            return None
        pivot_a = None
        for zs in reversed(zs_list):
            if not zs.is_one_bi_zs():
                pivot_a = zs
                break
        if pivot_a is None:
            return None

        stroke_n = bi_list[-1]
        if not getattr(stroke_n, 'is_sure', True):
            return None

        return (pivot_a, stroke_n)

    @staticmethod
    def _check_fx_macd_inflection_point(stroke_n, check_zero_axis=True):
        """
        检查分型MACD是否满足拐点条件（右肩K线 vs 中间K线）。
        前置条件（check_zero_axis=True时生效）：黄白线必须在0轴正确一侧：
          向上笔（卖点）：中间K线 DIF > 0 且 DEA > 0
          向下笔（买点）：中间K线 DIF < 0 且 DEA < 0
        拐点判断：右肩K线的 DIF/DEA/macd 至少有两个弱于中间K线
          向上笔：右肩 < 中间
          向下笔：右肩 > 中间
        Returns: True 表示满足拐点条件，False 表示不满足
        """
        end_klc = stroke_n.end_klc
        right_klc = getattr(end_klc, 'next', None) if end_klc else None
        mid_macd = end_klc.lst[-1].macd
        right_macd = right_klc.lst[-1].macd if right_klc and right_klc.lst else None
        if right_macd is None:
            return False

        if stroke_n.is_up():
            # 向上笔（卖点）：黄白线必须在0轴以上
            if check_zero_axis and not (mid_macd.DIF > 0 and mid_macd.DEA > 0):
                return False   # 没涨透，暂不做空
            cnt = (right_macd.DIF < mid_macd.DIF) + (right_macd.DEA < mid_macd.DEA) + (right_macd.macd < mid_macd.macd)
        else:
            # 向下笔（买点）：黄白线必须在0轴以下
            if check_zero_axis and not (mid_macd.DIF < 0 and mid_macd.DEA < 0):
                return False   # 没跌透，暂不做多
            cnt = (right_macd.DIF > mid_macd.DIF) + (right_macd.DEA > mid_macd.DEA) + (right_macd.macd > mid_macd.macd)
        return cnt >= 2


# ═══════════════════════════════════════════════════════════
# 区间套辅助函数（从 my_chan_main.py 搬迁至此）
# 主要用于 check_nested_diver 计算背驰，
# 红框功能（my_chan_main.py）通过 import 复用
# ═══════════════════════════════════════════════════════════

INTRADAY_FREQS = {"30m", "5m", "1m"}
SUBSECOND_FREQS = {"15s"}


def _get_date_fmt(freq):
    """根据频率返回统一的日期格式字符串（/ 分隔符）
    - 15s           → %Y/%m/%d %H:%M:%S
    - 30m, 5m, 1m   → %Y/%m/%d %H:%M
    - d, w, m, q, y → %Y/%m/%d
    """
    if freq in SUBSECOND_FREQS:
        return "%Y/%m/%d %H:%M:%S"
    if freq in INTRADAY_FREQS:
        return "%Y/%m/%d %H:%M"
    return "%Y/%m/%d"


def _get_main_bi_time_range(bi, date_fmt):
    """获取主笔两端原始K线时间及KLU对象

    股票和期货统一走此函数，通过 date_fmt 指定格式：
    - date_fmt = "%Y/%m/%d %H:%M:%S"（含秒，长度19）
    - date_fmt = "%Y/%m/%d %H:%M"（含时分，长度16）
    - date_fmt = "%Y/%m/%d"（仅日期，长度10）

    返回: (fx_a_raw_dt, fx_b_raw_dt, a_klu, b_klu) 或 None
      a_klu / b_klu 为左右肩 KLU 对象，供 _stocks_red_range 直接消费，
      避免重复从 bi 提取 KLU。
    """
    fx_a_raw_dt = ""
    fx_b_raw_dt = ""
    a_klu = None
    b_klu = None

    try:
        begin_klc = bi.begin_klc
        end_klc = bi.end_klc
        left_shoulder_klc = begin_klc.pre if begin_klc else None
        if left_shoulder_klc and left_shoulder_klc.lst:
            a_klu = left_shoulder_klc.lst[0]
            fx_a_raw_dt = a_klu.time.toFmtStr(date_fmt)

        right_shoulder_klc = end_klc.next if end_klc else None
        if right_shoulder_klc and right_shoulder_klc.lst:
            b_klu = right_shoulder_klc.lst[-1]
            fx_b_raw_dt = b_klu.time.toFmtStr(date_fmt)
    except Exception as e:
        print(f"[警告] 异常: {type(e).__name__}: {e}")

    if not fx_a_raw_dt or not fx_b_raw_dt:
        return None
    return fx_a_raw_dt, fx_b_raw_dt, a_klu, b_klu


def _stocks_red_range(a_klu, b_klu, sub_freq, bi=None):
    """股票双窗口：从主级别一笔的左右肩 KLU 中提取子级别边界时间 [C,D]。

    a_klu / b_klu 由 _get_main_bi_time_range 返回，避免重复提取。
    多级别CChan联立模式下，KLU 带有 sub_kl_list（真实子级别K线序列），
    直接取左肩第一根 / 右肩最后一根子级别K线的时间。

    实时场景下，最新 K 线（右肩）的 sub_kl_list 可能尚未建立，此时自动
    回退到笔的结束 K 线（end_klc.lst[-1]），该 K 线在上一轮已处理完毕。

    参数:
        a_klu:    左肩 KLU 对象（来自 _get_main_bi_time_range）
        b_klu:    右肩 KLU 对象（来自 _get_main_bi_time_range）
        sub_freq: 子级别周期（如 "30m", "5m"）
        bi:       主级别一笔（CBi 对象），仅在 b_klu sub_kl_list 为空时用于 fallback

    与期货 _futures_red_range 不同：股票有真实子级别数据，无需数学换算。
    """
    # ── 右边界 fallback：若右肩 sub_kl_list 为空，回退到笔结束K线 ──
    if b_klu is None or not hasattr(b_klu, 'sub_kl_list') or not b_klu.sub_kl_list:
        if bi is not None and bi.end_klc and bi.end_klc.lst:
            b_klu = bi.end_klc.lst[-1]

    # ── 读取子级别边界时间 ──
    out_fmt = _get_date_fmt(sub_freq)
    fx_a_sub_dt = ""
    fx_b_sub_dt = ""
    try:
        if a_klu and hasattr(a_klu, 'sub_kl_list') and a_klu.sub_kl_list:
            fx_a_sub_dt = a_klu.sub_kl_list[0].time.toFmtStr(out_fmt)
        if b_klu and hasattr(b_klu, 'sub_kl_list') and b_klu.sub_kl_list:
            fx_b_sub_dt = b_klu.sub_kl_list[-1].time.toFmtStr(out_fmt)
    except Exception as e:
        print(f"[stocks_red_range] 异常: {type(e).__name__}: {e}")
    return fx_a_sub_dt, fx_b_sub_dt


def _futures_red_range(snapshot, top_freq_sec, bottom_freq_sec, sub_freq=None):
    """期货双窗口：将上窗笔的原始分型时间换算为子级别 K 线时间。

    天勤 K 线时间 = 开始时间（不同于股票 = 结束时间），因此：
      fx_a_sub_dt = fx_a_raw_dt
        → 上层 K 线开始时间 = 第一根子级别 K 线时间
      fx_b_sub_dt = fx_b_raw_dt + (top_freq_sec - bottom_freq_sec)
        → 上层 K 线开始时间 + (上层周期 - 子周期) = 最后一根子级别 K 线时间
        → 例：30m 线 11:00 + 30m - 5m = 11:25（最后一根 5m 线）

    参数:
        snapshot:         上窗快照（含 bis 列表），直接修改其 fx_a_sub_dt / fx_b_sub_dt
        top_freq_sec:     上窗周期秒数
        bottom_freq_sec:  下窗周期秒数
        sub_freq:         子级别周期字符串（如 "5m", "1m"），用于确定输出日期格式；
                          为 None 时默认使用 "%Y/%m/%d %H:%M:%S"

    与 _stocks_red_range 不同：期货无真实子级别 K 线序列，需数学换算。
    """
    # ── 构建解析格式列表：优先 / 格式，兼容 - 格式 ──
    _PARSE_DATE_FORMATS = [
        "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
        "%Y/%m/%d", "%Y-%m-%d",
    ]
    out_fmt = _get_date_fmt(sub_freq) if sub_freq else "%Y/%m/%d %H:%M:%S"

    if not snapshot:
        print("[futures_red_range] 快照为空，跳过")
        return
    bis = snapshot.get('bis')
    if not bis:
        print("[futures_red_range] 快照中无 bis 数据，跳过")
        return

    if top_freq_sec <= bottom_freq_sec:
        print(f"[futures_red_range] 周期不合法: top={top_freq_sec}s <= bottom={bottom_freq_sec}s，跳过")
        return

    offset_sec = top_freq_sec - bottom_freq_sec
    fail_count = 0
    skip_count = 0

    for bi in bis:
        raw_a = bi.get('fx_a_raw_dt', '') or ''
        raw_b = bi.get('fx_b_raw_dt', '') or ''

        # 左边界：第一根子级别 K 线 = 上层 K 线开始时间
        bi['fx_a_sub_dt'] = raw_a

        # 右边界：最后一根子级别 K 线 = 上层 K 线开始 + offset
        if not raw_b:
            bi['fx_b_sub_dt'] = ''
            skip_count += 1
            continue

        dt = None
        for fmt in _PARSE_DATE_FORMATS:
            try:
                dt = datetime.strptime(raw_b, fmt)
                break
            except ValueError:
                continue

        if dt is None:
            fail_count += 1
            if fail_count <= 3:
                print(f"[futures_red_range] fx_b_raw_dt 日期解析失败: '{raw_b}'")
            bi['fx_b_sub_dt'] = raw_b  # 回退：保留原始值
            continue

        try:
            dt = dt + timedelta(seconds=offset_sec)
            bi['fx_b_sub_dt'] = dt.strftime(out_fmt)
        except OverflowError:
            fail_count += 1
            if fail_count <= 3:
                print(f"[futures_red_range] timedelta 溢出: raw_b='{raw_b}', offset={offset_sec}s")
            bi['fx_b_sub_dt'] = raw_b

    if skip_count > 0:
        print(f"[futures_red_range] {skip_count}/{len(bis)} 笔 fx_b_raw_dt 为空，fx_b_sub_dt 置空")
    if fail_count > 0:
        print(f"[futures_red_range] {fail_count}/{len(bis)} 笔 fx_b_sub_dt 换算失败，回退到原始值")


def _find_sub_bi_sequence(fx_a_sub_dt, fx_b_sub_dt, sub_bi_list, sub_freq):
    """在子级别笔列表中找被红框 [fx_a_sub_dt, fx_b_sub_dt] 完全覆盖的笔。

    逻辑与前端 updateDualNewZs() 完全一致：
      bi 的 sdt >= fx_a_sub_dt 且 bi 的 edt <= fx_b_sub_dt → 该笔被红框完全覆盖。
    取第一个和最后一个被覆盖的笔索引。

    参数:
        fx_a_sub_dt: 红框左边界时间字符串（来自 _stocks_red_range 的 fx_a_sub_dt）
        fx_b_sub_dt: 红框右边界时间字符串（来自 _stocks_red_range 的 fx_b_sub_dt）
        sub_bi_list: 子级别完整笔列表（CBiList）
        sub_freq:   子级别周期（如 "30m", "5m"），用于确定日期格式

    返回: (start_bi_idx, end_bi_idx) 或 (None, None)
    """
    if not fx_a_sub_dt or not fx_b_sub_dt:
        print(f"[find_sub_bi] 红框边界为空: fx_a='{fx_a_sub_dt}', fx_b='{fx_b_sub_dt}'")
        return None, None

    sub_date_fmt = _get_date_fmt(sub_freq)
    start_bi_idx = None
    end_bi_idx = None
    skipped_count = 0

    for i, bi in enumerate(sub_bi_list):
        begin_klu = bi.get_begin_klu()
        end_klu = bi.get_end_klu()
        if begin_klu is None or end_klu is None:
            skipped_count += 1
            continue
        try:
            s_dt = begin_klu.time.toFmtStr(sub_date_fmt)
            e_dt = end_klu.time.toFmtStr(sub_date_fmt)
        except (AttributeError, ValueError, TypeError) as e:
            skipped_count += 1
            if skipped_count <= 3:
                print(f"[find_sub_bi] 笔{i}时间格式化失败: {type(e).__name__}: {e}")
            continue

        if s_dt >= fx_a_sub_dt and e_dt <= fx_b_sub_dt:
            if start_bi_idx is None:
                start_bi_idx = i
            end_bi_idx = i

    if skipped_count > 0:
        print(f"[find_sub_bi] 跳过 {skipped_count}/{len(sub_bi_list)} 笔（缺少K线或时间格式化失败）")

    if start_bi_idx is not None:
        print(f"[find_sub_bi] 红框内笔范围: [{start_bi_idx}, {end_bi_idx}], 共 {end_bi_idx - start_bi_idx + 1} 笔")
    else:
        print(f"[find_sub_bi] 红框内无完整笔: fx_a='{fx_a_sub_dt}', fx_b='{fx_b_sub_dt}'")

    return start_bi_idx, end_bi_idx


def _check_red_range_zs(bis, all_bi_list, date_fmt):
    """
    从笔列表中构建中枢，完整模拟 CZSList 的 over_seg 算法（不含合并）。

    流程（对应 ZSList.py）：
      cal_bi_zs(over_seg) → 逐笔调用 update_overseg_zs
        → update_overseg_zs: 处理延伸/跳过 → add_to_free_lst
          → add_to_free_lst: 加入 free_lst → try_construct_zs(over_seg)
            → 取最后3笔 → 跳过进入段 → 检查三笔重叠 → 形成中枢 → 清空 free_lst

    参数:
        bis: 从 start_bi 到 end_bi 的笔列表（含两端）
        all_bi_list: 完整的笔列表（用于查找进入段）
        date_fmt: 日期格式字符串

    返回:
        zs_data: 中枢数据列表
    """
    zs_data = []
    print(f"[build_zs] 输入bis数量={len(bis)}, date_fmt={date_fmt}")
    if len(bis) < 4:  # over_seg 至少需要 1进入段 + 3构成中枢
        print(f"[build_zs] bis不足4根，无法形成中枢，返回空")
        return zs_data

    def _in_zs_range(bi, zg, zd):
        """笔是否与中枢区间 [zd, zg] 有重叠（模拟 CZS.in_range）"""
        return min(zg, bi._high()) >= max(zd, bi._low())

    free_lst = []       # 模拟 CZSList.free_item_lst
    zs_records = []     # 模拟 CZSList.zs_lst（简化版）

    for bi in bis:
        # ===== update_overseg_zs 逻辑 =====
        if len(zs_records) and len(free_lst) == 0:
            last_zs = zs_records[-1]
            if bi.next is not None:
                if (bi.idx - last_zs['end_bi'].idx <= 1
                        and _in_zs_range(bi.next, last_zs['zg'], last_zs['zd'])
                        and _in_zs_range(bi, last_zs['zg'], last_zs['zd'])):
                    last_zs['edt'] = bi.get_end_klu().time.toFmtStr(date_fmt) if bi.get_end_klu() is not None else ""
                    last_zs['gg'] = round(max(last_zs['gg'], bi._high()), 2)
                    last_zs['dd'] = round(min(last_zs['dd'], bi._low()), 2)
                    last_zs['end_bi'] = bi
                    zs_data[-1] = {k: v for k, v in last_zs.items() if k != 'end_bi'}
                    continue
            if _in_zs_range(bi, last_zs['zg'], last_zs['zd']) and bi.idx - last_zs['end_bi'].idx <= 1:
                continue
            if not _in_zs_range(bi, last_zs['zg'], last_zs['zd']) and not last_zs.get('confirm_edt'):
                if bi.idx != bis[-1].idx:
                    last_zs['confirm_edt'] = bi.get_end_klu().time.toFmtStr(date_fmt) if bi.get_end_klu() is not None else ""
                    zs_data[-1]['confirm_edt'] = last_zs['confirm_edt']

        # ===== add_to_free_lst 逻辑 =====
        if len(free_lst) != 0 and bi.idx == free_lst[-1].idx:
            free_lst = free_lst[:-1]
        free_lst.append(bi)

        # ===== try_construct_zs(over_seg) 逻辑 =====
        if len(free_lst) < 3:
            continue

        lst = list(free_lst[-3:])

        # --- 处理进入段 ---
        if len(zs_records) > 0:
            zs = zs_records[-1]
            lst0_low = lst[0]._low()
            lst0_high = lst[0]._high()
            if lst0_low > zs['zg']:
                if lst[0].is_up():
                    continue
            elif lst0_high < zs['zd']:
                if lst[0].is_down():
                    continue
        else:
            first_pen = free_lst[0]
            if len(free_lst) == 3:
                continue
            else:
                if lst[0].dir == first_pen.dir:
                    continue

        if len(lst) < 3:
            continue

        b1, b2, b3 = lst[0], lst[1], lst[2]

        if not getattr(b3, 'is_sure', True):
            continue

        # --- 检查三笔重叠 ---
        min_high = min(b1._high(), b2._high(), b3._high())
        max_low = max(b1._low(), b2._low(), b3._low())
        if min_high <= max_low:
            continue

        # --- 形成中枢 ---
        zg = min_high
        zd = max_low
        gg = max(b1._high(), b2._high(), b3._high())
        dd = min(b1._low(), b2._low(), b3._low())

        entry_bi = None
        entry_dir = "up"
        for j, bi_ref in enumerate(all_bi_list):
            if bi_ref is b1 and j > 0:
                entry_bi = all_bi_list[j - 1]
                break
        if entry_bi is not None:
            entry_dir = "up" if entry_bi.is_up() else "down"

        sdt = b1.get_begin_klu().time.toFmtStr(date_fmt) if b1.get_begin_klu() is not None else ""
        edt = b3.get_end_klu().time.toFmtStr(date_fmt) if b3.get_end_klu() is not None else ""

        zs_rec = {
            'sdt': sdt, 'edt': edt, 'confirm_edt': '',
            'zg': round(zg, 2), 'zd': round(zd, 2),
            'gg': round(gg, 2), 'dd': round(dd, 2),
            'dir': entry_dir,
            'end_bi': b3,
        }
        zs_records.append(zs_rec)
        zs_data.append({k: v for k, v in zs_rec.items() if k != 'end_bi'})

        free_lst = []

    print(f"[build_zs] 完成: zs_data长度={len(zs_data)}")
    return zs_data


def _check_sub_single_bi_diver(bi):
    """单笔背驰判断：检查笔内MACD柱状图是否走弱"""
    end_klu = bi.get_end_klu()
    end_macd = abs(end_klu.macd.macd) if end_klu and hasattr(end_klu, 'macd') else 0
    peak = bi.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=True)  # is_reverse 参数仅在 MACD_ALGO.AREA 才有意义
    if peak < 1e-7:
        return {"diverged": False, "detail": "笔内无有效MACD柱"}
    ratio = end_macd / peak
    is_diver = ratio <= 0.7  # 末端MACD不足峰值的70%
    detail = f"单笔，{'MACD走弱' if is_diver else '未走弱'}（峰值={peak:.2f}，末端={end_macd:.2f}）"
    return {"diverged": is_diver, "detail": detail}


def _check_sub_multi_bi_diver(bi_list):
    """多笔无中枢背驰判断：比较最后两个同向笔的MACD峰值"""
    last_bi = bi_list[-1]
    prev_same_dir = None
    for bi in reversed(bi_list[:-1]):
        if bi.is_up() == last_bi.is_up():
            prev_same_dir = bi
            break
    if prev_same_dir is None:
        return {"diverged": False, "detail": "未找到前一个同向笔"}

    prev_peak = prev_same_dir.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=False) # is_reverse 参数仅在 MACD_ALGO.AREA 才有意义
    curr_peak = last_bi.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=True)
    is_diver = curr_peak <= prev_peak * 0.7
    detail = (
        f"多笔无中枢，{'MACD背驰' if is_diver else '未背驰'}"
        f"（前笔峰值={prev_peak:.2f}，后笔峰值={curr_peak:.2f}）"
    )
    return {"diverged": is_diver, "detail": detail}
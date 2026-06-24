from typing import Dict, Generic, Iterable, List, Optional, Tuple, TypeVar

from Bi.Bi import CBi
from Bi.BiList import CBiList
from Common.CEnum import BSP_TYPE, MACD_ALGO
from Common.func_util import has_overlap
from Seg.Seg import CSeg
from Seg.SegListComm import CSegListComm
from ZS.ZS import CZS

from .BS_Point import CBS_Point
from .BSPointConfig import CBSPointConfig, CPointConfig

LINE_TYPE = TypeVar('LINE_TYPE', CBi, CSeg[CBi])
LINE_LIST_TYPE = TypeVar('LINE_LIST_TYPE', CBiList, CSegListComm[CBi])

# 区间套背驰判断回调（由 my_chan_main 注入）
_sub_divergence_check = None


class CBSPointList(Generic[LINE_TYPE, LINE_LIST_TYPE]):
    def __init__(self, bs_point_config: CBSPointConfig):
        self.bsp_store_dict: Dict[BSP_TYPE, Tuple[List[CBS_Point[LINE_TYPE]], List[CBS_Point[LINE_TYPE]]]] = {}
        self.bsp_store_flat_dict: Dict[int, CBS_Point[LINE_TYPE]] = {}

        self.bsp1_list: List[CBS_Point[LINE_TYPE]] = []
        self.bsp1_dict: Dict[int, CBS_Point[LINE_TYPE]] = {}

        self.config = bs_point_config
        self.last_sure_pos = -1
        self.last_sure_seg_idx = 0

    def store_add_bsp(self, bsp_type: BSP_TYPE, bsp: CBS_Point[LINE_TYPE]):
        if bsp_type not in self.bsp_store_dict:
            self.bsp_store_dict[bsp_type] = ([], [])
        if len(self.bsp_store_dict[bsp_type][bsp.is_buy]) > 0:
            assert self.bsp_store_dict[bsp_type][bsp.is_buy][-1].bi.idx < bsp.bi.idx, f"{bsp_type}, {bsp.is_buy} {self.bsp_store_dict[bsp_type][bsp.is_buy][-1].bi.idx} {bsp.bi.idx}"
        self.bsp_store_dict[bsp_type][bsp.is_buy].append(bsp)
        self.bsp_store_flat_dict[bsp.bi.idx] = bsp

    def add_bsp1(self, bsp: CBS_Point[LINE_TYPE]):
        if len(self.bsp1_list) > 0:
            assert self.bsp1_list[-1].bi.idx < bsp.bi.idx
        self.bsp1_list.append(bsp)
        self.bsp1_dict[bsp.bi.idx] = bsp

    def clear_store_end(self):
        for bsp_list in self.bsp_store_dict.values():
            for is_buy in [True, False]:
                while len(bsp_list[is_buy]) > 0:
                    if bsp_list[is_buy][-1].bi.get_end_klu().idx <= self.last_sure_pos:
                        break
                    del self.bsp_store_flat_dict[bsp_list[is_buy][-1].bi.idx]
                    # 同时把失效买卖点从Bi删除
                    bsp_list[is_buy][-1].bi.bsp = None
                    bsp_list[is_buy].pop()

    def clear_bsp1_end(self):
        while len(self.bsp1_list) > 0:
            if self.bsp1_list[-1].bi.get_end_klu().idx <= self.last_sure_pos:
                break
            del self.bsp1_dict[self.bsp1_list[-1].bi.idx]
            self.bsp1_list.pop()

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

    def cal(self, bi_list: LINE_LIST_TYPE, seg_list: CSegListComm[LINE_TYPE]):
        self.clear_store_end()
        self.clear_bsp1_end()
        self.cal_seg_bs1point(seg_list, bi_list)
        self.cal_seg_bs2point(seg_list, bi_list)
        self.cal_seg_bs3point(seg_list, bi_list)

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
        relate_bsp1: Optional[CBS_Point],
        is_target_bsp: bool = True,
        feature_dict=None,
    ):
        is_buy = bi.is_down()
        if exist_bsp := self.bsp_store_flat_dict.get(bi.idx):
            assert exist_bsp.is_buy == is_buy
            exist_bsp.add_another_bsp_prop(bs_type, relate_bsp1)
            if feature_dict is not None:
                exist_bsp.add_feat(feature_dict)
            return
        if bs_type not in self.config.GetBSConfig(is_buy).target_types:
            is_target_bsp = False

        if is_target_bsp or bs_type in [BSP_TYPE.T1, BSP_TYPE.T1P]:
            bsp = CBS_Point[LINE_TYPE](
                bi=bi,
                is_buy=is_buy,
                bs_type=bs_type,
                relate_bsp1=relate_bsp1,
                feature_dict=feature_dict,
            )
        else:
            return
        if is_target_bsp:
            self.store_add_bsp(bs_type, bsp)
        else:
            bsp.bi.bsp = None
        if bs_type in [BSP_TYPE.T1, BSP_TYPE.T1P]:
            self.add_bsp1(bsp)

    def cal_seg_bs1point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        for seg in seg_list[self.last_sure_seg_idx:]:
            if not self.seg_need_cal(seg):
                continue
            self.cal_single_bs1point(seg, bi_list)

    def cal_single_bs1point(self, seg: CSeg[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        BSP_CONF = self.config.GetBSConfig(seg.is_down())
        zs_cnt = seg.get_multi_bi_zs_cnt() if BSP_CONF.bsp1_only_multibi_zs else len(seg.zs_lst)
        is_target_bsp = (BSP_CONF.min_zs_cnt <= 0 or zs_cnt >= BSP_CONF.min_zs_cnt)
        if len(seg.zs_lst) > 0 and \
           not seg.zs_lst[-1].is_one_bi_zs() and \
           ((seg.zs_lst[-1].bi_out and seg.zs_lst[-1].bi_out.idx >= seg.end_bi.idx) or seg.zs_lst[-1].bi_lst[-1].idx >= seg.end_bi.idx) \
           and seg.end_bi.idx - seg.zs_lst[-1].get_bi_in().idx > 2:
            self.treat_bsp1(seg, BSP_CONF, is_target_bsp)
        else:
            self.treat_pz_bsp1(seg, BSP_CONF, bi_list, is_target_bsp)

    def treat_bsp1(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, is_target_bsp: bool):
        last_zs = seg.zs_lst[-1]
        break_peak, _ = last_zs.out_bi_is_peak(seg.end_bi.idx)
        if BSP_CONF.bs1_peak and not break_peak:
            is_target_bsp = False
        is_diver, divergence_rate = last_zs.is_divergence(BSP_CONF, out_bi=seg.end_bi)
        if not is_diver:
            is_target_bsp = False
        feature_dict = {
            'divergence_rate': divergence_rate,
            'zs_cnt': len(seg.zs_lst),
        }
        self.add_bs(bs_type=BSP_TYPE.T1, bi=seg.end_bi, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict)

    def treat_pz_bsp1(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, bi_list: LINE_LIST_TYPE, is_target_bsp):
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
            'bsp1_bi_amp': last_bi.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T1P, bi=last_bi, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict)

    def cal_seg_bs2point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        for seg in seg_list[self.last_sure_seg_idx:]:
            config = self.config.GetBSConfig(seg.is_down())
            if BSP_TYPE.T2 not in config.target_types and BSP_TYPE.T2S not in config.target_types:
                continue
            if not self.seg_need_cal(seg):
                continue
            self.treat_bsp2(seg, seg_list, bi_list)

    def treat_bsp2(self, seg: CSeg, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        if len(seg_list) > 1:
            BSP_CONF = self.config.GetBSConfig(seg.is_down())
            bsp1_bi = seg.end_bi
            real_bsp1 = self.bsp1_dict.get(bsp1_bi.idx)
            if bsp1_bi.idx + 2 >= len(bi_list):
                return
            break_bi = bi_list[bsp1_bi.idx + 1]
            bsp2_bi = bi_list[bsp1_bi.idx + 2]
        else:
            BSP_CONF = self.config.GetBSConfig(seg.is_up())
            bsp1_bi, real_bsp1 = None, None
            if len(bi_list) == 1:
                return
            bsp2_bi = bi_list[1]
            break_bi = bi_list[0]
        if BSP_CONF.bsp2_follow_1 and (not bsp1_bi or bsp1_bi.idx not in self.bsp_store_flat_dict):
            return
        retrace_rate = bsp2_bi.amp()/break_bi.amp()
        bsp2_flag = retrace_rate <= BSP_CONF.max_bs2_rate
        if bsp2_flag:
            feature_dict = {
                'bsp2_retrace_rate': retrace_rate,
                'bsp2_break_bi_amp': break_bi.amp(),
                'bsp2_bi_amp': bsp2_bi.amp(),
            }
            self.add_bs(bs_type=BSP_TYPE.T2, bi=bsp2_bi, relate_bsp1=real_bsp1, feature_dict=feature_dict)  # type: ignore
        elif BSP_CONF.bsp2s_follow_2:
            return
        if BSP_TYPE.T2S not in self.config.GetBSConfig(seg.is_down()).target_types:
            return
        self.treat_bsp2s(seg_list, bi_list, bsp2_bi, break_bi, real_bsp1, BSP_CONF)  # type: ignore

    def treat_bsp2s(
        self,
        seg_list: CSegListComm,
        bi_list: LINE_LIST_TYPE,
        bsp2_bi: LINE_TYPE,
        break_bi: LINE_TYPE,
        real_bsp1: Optional[CBS_Point],
        BSP_CONF: CPointConfig,
    ):
        bias = 2
        _low, _high = None, None
        while bsp2_bi.idx + bias < len(bi_list):  # 计算类二
            bsp2s_bi = bi_list[bsp2_bi.idx + bias]
            assert bsp2s_bi.seg_idx is not None and bsp2_bi.seg_idx is not None
            if BSP_CONF.max_bsp2s_lv is not None and bias/2 > BSP_CONF.max_bsp2s_lv:
                break
            if bsp2s_bi.seg_idx != bsp2_bi.seg_idx and (bsp2s_bi.seg_idx < len(seg_list)-1 or bsp2s_bi.seg_idx - bsp2_bi.seg_idx >= 2 or seg_list[bsp2_bi.seg_idx].is_sure):
                break
            if bias == 2:
                if not has_overlap(bsp2_bi._low(), bsp2_bi._high(), bsp2s_bi._low(), bsp2s_bi._high()):
                    break
                _low = max([bsp2_bi._low(), bsp2s_bi._low()])
                _high = min([bsp2_bi._high(), bsp2s_bi._high()])
            elif not has_overlap(_low, _high, bsp2s_bi._low(), bsp2s_bi._high()):
                break

            if bsp2s_break_bsp1(bsp2s_bi, break_bi):
                break
            retrace_rate = abs(bsp2s_bi.get_end_val()-break_bi.get_end_val())/break_bi.amp()
            if retrace_rate > BSP_CONF.max_bs2_rate:
                break
            feature_dict = {
                'bsp2s_retrace_rate': retrace_rate,
                'bsp2s_break_bi_amp': break_bi.amp(),
                'bsp2s_bi_amp': bsp2s_bi.amp(),
                'bsp2s_lv': bias/2,
            }
            self.add_bs(bs_type=BSP_TYPE.T2S, bi=bsp2s_bi, relate_bsp1=real_bsp1, feature_dict=feature_dict)  # type: ignore
            bias += 2

    def cal_seg_bs3point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        for seg in seg_list[self.last_sure_seg_idx:]:
            if not self.seg_need_cal(seg):
                continue
            config = self.config.GetBSConfig(seg.is_down())
            if BSP_TYPE.T3A not in config.target_types and BSP_TYPE.T3B not in config.target_types:
                continue
            if len(seg_list) > 1:
                bsp1_bi = seg.end_bi
                bsp1_bi_idx = bsp1_bi.idx
                BSP_CONF = self.config.GetBSConfig(seg.is_down())
                real_bsp1 = self.bsp1_dict.get(bsp1_bi.idx)
                next_seg_idx = seg.idx+1
                next_seg = seg.next  # 可能为None, 所以并不一定可以保证next_seg_idx == next_seg.idx
            else:
                next_seg = seg
                next_seg_idx = seg.idx
                bsp1_bi, real_bsp1 = None, None
                bsp1_bi_idx = -1
                BSP_CONF = self.config.GetBSConfig(seg.is_up())
            if BSP_CONF.bsp3_follow_1 and (not bsp1_bi or bsp1_bi.idx not in self.bsp_store_flat_dict):
                continue
            if next_seg:
                self.treat_bsp3_after(seg_list, next_seg, BSP_CONF, bi_list, real_bsp1, bsp1_bi_idx, next_seg_idx)
            self.treat_bsp3_before(seg_list, seg, next_seg, bsp1_bi, BSP_CONF, bi_list, real_bsp1, next_seg_idx)

    def treat_bsp3_after(
        self,
        seg_list: CSegListComm[LINE_TYPE],
        next_seg: CSeg[LINE_TYPE],
        BSP_CONF: CPointConfig,
        bi_list: LINE_LIST_TYPE,
        real_bsp1,
        bsp1_bi_idx,
        next_seg_idx
    ):
        first_zs = next_seg.get_first_multi_bi_zs()
        if first_zs is None:
            return
        if BSP_CONF.strict_bsp3 and first_zs.get_bi_in().idx != bsp1_bi_idx+1:
            return

        config = self.config.GetBSConfig(next_seg.is_down())
        bsp3a_max_zs_cnt = config.bsp3a_max_zs_cnt
        for zs_idx, zs in enumerate(next_seg.get_multi_bi_zs_lst()):
            if zs_idx >= bsp3a_max_zs_cnt:
                break
            if zs.bi_out is None or zs.bi_out.idx+1 >= len(bi_list):
                break
            bsp3_bi = bi_list[zs.bi_out.idx+1]
            if bsp3_bi.parent_seg is None:
                if next_seg.idx != len(seg_list)-1:
                    break
            elif bsp3_bi.parent_seg.idx != next_seg.idx:
                if len(bsp3_bi.parent_seg.bi_list) >= 3:
                    break
            if bsp3_bi.dir == next_seg.dir:
                break
            if bsp3_bi.seg_idx != next_seg_idx and next_seg_idx < len(seg_list)-2:
                break
            if bsp3_back2zs(bsp3_bi, zs):
                continue
            bsp3_peak_zs = bsp3_break_zspeak(bsp3_bi, zs)
            if BSP_CONF.bsp3_peak and not bsp3_peak_zs:
                continue
            feature_dict = {
                'bsp3_zs_height': (zs.high - zs.low)/zs.low,
                'bsp3_bi_amp': bsp3_bi.amp(),
            }
            self.add_bs(bs_type=BSP_TYPE.T3A, bi=bsp3_bi, relate_bsp1=real_bsp1, feature_dict=feature_dict)  # type: ignore

    def treat_bsp3_before(
        self,
        seg_list: CSegListComm[LINE_TYPE],
        seg: CSeg[LINE_TYPE],
        next_seg: Optional[CSeg[LINE_TYPE]],
        bsp1_bi: Optional[LINE_TYPE],
        BSP_CONF: CPointConfig,
        bi_list: LINE_LIST_TYPE,
        real_bsp1,
        next_seg_idx
    ):
        cmp_zs = seg.get_final_multi_bi_zs()
        if cmp_zs is None:
            return
        if not bsp1_bi:
            return
        if BSP_CONF.strict_bsp3 and (cmp_zs.bi_out is None or cmp_zs.bi_out.idx != bsp1_bi.idx):
            return
        end_bi_idx = cal_bsp3_bi_end_idx(next_seg)
        for bsp3_bi in bi_list[bsp1_bi.idx+2::2]:
            if bsp3_bi.idx > end_bi_idx:
                break
            assert bsp3_bi.seg_idx is not None
            if bsp3_bi.seg_idx != next_seg_idx and bsp3_bi.seg_idx < len(seg_list)-1:
                break
            if bsp3_back2zs(bsp3_bi, cmp_zs):  # type: ignore
                continue
            feature_dict = {
                'bsp3_zs_height': (cmp_zs.high - cmp_zs.low)/cmp_zs.low,
                'bsp3_bi_amp': bsp3_bi.amp(),
            }
            self.add_bs(bs_type=BSP_TYPE.T3B, bi=bsp3_bi, relate_bsp1=real_bsp1, feature_dict=feature_dict)  # type: ignore
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


def bsp2s_break_bsp1(bsp2s_bi: LINE_TYPE, bsp2_break_bi: LINE_TYPE) -> bool:
    return (bsp2s_bi.is_down() and bsp2s_bi._low() < bsp2_break_bi._low()) or \
           (bsp2s_bi.is_up() and bsp2s_bi._high() > bsp2_break_bi._high())


def bsp3_back2zs(bsp3_bi: LINE_TYPE, zs: CZS) -> bool:
    return (bsp3_bi.is_down() and bsp3_bi._low() < zs.high) or (bsp3_bi.is_up() and bsp3_bi._high() > zs.low)


def bsp3_break_zspeak(bsp3_bi: LINE_TYPE, zs: CZS) -> bool:
    return (bsp3_bi.is_down() and bsp3_bi._high() >= zs.peak_high) or (bsp3_bi.is_up() and bsp3_bi._low() <= zs.peak_low)


def cal_bsp3_bi_end_idx(seg: Optional[CSeg[LINE_TYPE]]):
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
class MyBSPointList(CBSPointList[LINE_TYPE, LINE_LIST_TYPE]):
    """自定义买卖点计算类。

    继承 CBSPointList，将 cal() 中的调用替换为 cal_bs0point ~ cal_bs3point。
    你只需修改下面四个方法即可实现自己的买卖点逻辑。

    与线段（seg_list）完全解耦，只依赖笔列表（bi_list）和笔中枢列表（zs_list）。
    0类买卖点已通过 BSP_TYPE.T0 枚举正规化，无需额外 hack。
    """

    # ── 入口 ──
    def cal(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        self.cal_bs0point(bi_list, zs_list)
        self.cal_bs1point(bi_list, zs_list)
        self.cal_bs2point(bi_list, zs_list)
        self.cal_bs3point(bi_list, zs_list)

    # ── 0类买卖点 ──
    def cal_bs0point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        """0类买卖点计算。

        算法：
        1. 从框架的笔中枢列表 zs_list 中找到最后一个多笔中枢，即中枢A
           中枢A由前三笔（笔1, 笔2, 笔3）重叠构成
        2. 取最后一笔n（n≥4，即笔1.idx+3 起）作为当下笔：
           - 笔n必须属于中枢A（与A有重叠）
           - 向上笔n：末端高点 > 中枢A上沿
           - 向下笔n：末端低点 < 中枢A下沿
        3. 从笔n往回数5笔（n-4, n-3, n-2, n-1, n）
           n-3, n-2, n-1 重叠 → 中枢B
        4. 笔n突破中枢B（向上突破上沿，向下突破下沿）
           且 n-4 也突破中枢B（向上：n-4低点<下沿；向下：n-4高点>上沿）
        5. n-4为进入笔，n为离开笔，MACD full_area 背驰 → 0类买卖点
        """
        # ── 第一步：从笔中枢列表 zs_list 中找到最后一个多笔中枢 → 中枢A ──
        if zs_list is None or len(zs_list) == 0:
            return

        pivot_a = None
        for zs in reversed(zs_list):
            if not zs.is_one_bi_zs():
                pivot_a = zs
                break

        if pivot_a is None:
            return

        # ── 第二步：只检查最后一笔（当下笔）──
        # 中枢A由前三笔（笔1, 笔2, 笔3）构成，笔n（n≥4）从笔1.idx + 3 开始
        n_idx = len(bi_list) - 1
        if n_idx < pivot_a.begin_bi.idx + 3 or n_idx < 4:
            return

        stroke_n = bi_list[n_idx]

        # 虚笔不计算0类买卖点
        if not getattr(stroke_n, 'is_sure', True):
            return

        # 已存在T0买卖点，跳过重复计算
        if exist_bsp := self.bsp_store_flat_dict.get(stroke_n.idx):
            if BSP_TYPE.T0 in exist_bsp.type:
                return

        # ── 条件〇：笔n属于中枢A（与中枢A有重叠）──
        if not has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high):
            return

        # ── 区间套背驰判断：高级别一笔在次级别是否背驰，不背驰则跳过 ──
        """
        if _sub_divergence_check is not None:
            result = _sub_divergence_check(bi_list)
            if not result.get("diverged"):
                print(f"[区间套] 跳过0类买卖点: {result.get('detail', '未知原因')}")
                return
        """
        # ── 条件一：笔n突破中枢A ──
        end_klc = stroke_n.end_klc
        right_klc = getattr(end_klc, 'next', None) if end_klc else None

        if stroke_n.is_up():
            # 向上笔n：
            # 条件A：末端高点 >= 中枢A波动区间高点
            # 条件B：(末端高点 >= 中枢A上沿) AND (顶分型右肩DEA < 前一根K线DEA)
            cond_a = stroke_n._high() >= getattr(pivot_a, 'peak_high', pivot_a.high)
            cond_b = False
            if stroke_n._high() >= pivot_a.high:
                if right_klc is not None and hasattr(right_klc, 'dea') and hasattr(end_klc, 'dea'):
                    cond_b = right_klc.dea < end_klc.dea
            if not (cond_a or cond_b):
                return
        else:
            # 向下笔n：
            # 条件A：末端低点 <= 中枢A波动区间低点
            # 条件B：(末端低点 <= 中枢A下沿) AND (底分型右肩DEA > 前一根K线DEA)
            cond_a = stroke_n._low() <= getattr(pivot_a, 'peak_low', pivot_a.low)
            cond_b = False
            if stroke_n._low() <= pivot_a.low:
                if right_klc is not None and hasattr(right_klc, 'dea') and hasattr(end_klc, 'dea'):
                    cond_b = right_klc.dea > end_klc.dea
            if not (cond_a or cond_b):
                return

        # ── 条件二：n-3, n-2, n-1 重叠 → 中枢B ──
        s_nm1 = bi_list[n_idx - 1]  # 笔n-1
        s_nm2 = bi_list[n_idx - 2]  # 笔n-2
        s_nm3 = bi_list[n_idx - 3]  # 笔n-3
        s_nm4 = bi_list[n_idx - 4]  # 笔n-4（中枢B的进入笔）

        zs_b_low = max(s_nm1._low(), s_nm2._low(), s_nm3._low())
        zs_b_high = min(s_nm1._high(), s_nm2._high(), s_nm3._high())
        if zs_b_low > zs_b_high:  # 三笔无重叠，不构成中枢B
            return

        # ── 条件三：笔n突破中枢B，且 n-4 也突破中枢B ──
        if stroke_n.is_up():
            # 向上笔n：末端高点 > 中枢B上沿
            if stroke_n._high() <= zs_b_high:
                return
            # n-4（进入笔）：末端低点 < 中枢B下沿
            if s_nm4._low() >= zs_b_low:
                return
        else:
            # 向下笔n：末端低点 < 中枢B下沿
            if stroke_n._low() >= zs_b_low:
                return
            # n-4（进入笔）：末端高点 > 中枢B上沿
            if s_nm4._high() <= zs_b_high:
                return

        # ── 条件四：MACD背驰判断（full_area）──
        # 进入笔：n-4，离开笔：n
        in_metric = s_nm4.cal_macd_metric(MACD_ALGO.FULL_AREA, is_reverse=False)
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
        self.add_bs(bs_type=BSP_TYPE.T0, bi=stroke_n, relate_bsp1=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    # ── 1类/1p 买卖点 ──
    def cal_bs1point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        """TODO: 在此实现自定义的 1 类（趋势背驰）和 1p 类（盘整背驰）买卖点计算逻辑。

        参数:
            bi_list: 笔列表，包含所有笔对象
            zs_list: 笔中枢列表，可直接遍历取多笔中枢

        生成买卖点请调用:
            self.add_bs(bs_type=BSP_TYPE.T1,  bi=..., relate_bsp1=None, feature_dict={...})
            self.add_bs(bs_type=BSP_TYPE.T1P, bi=..., relate_bsp1=None, feature_dict={...})
        """
        pass

    def cal_bs2point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        """TODO: 在此实现自定义的 2 类（回踩/回抽确认）和 2s 类（类二）买卖点计算逻辑。

        参数:
            bi_list: 笔列表，包含所有笔对象
            zs_list: 笔中枢列表，可直接遍历取多笔中枢

        生成买卖点请调用:
            self.add_bs(bs_type=BSP_TYPE.T2,  bi=..., relate_bsp1=..., feature_dict={...})
            self.add_bs(bs_type=BSP_TYPE.T2S, bi=..., relate_bsp1=..., feature_dict={...})
        """
        pass

    def cal_bs3point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        """TODO: 在此实现自定义的 3a 类（中枢在1类之后）和 3b 类（中枢在1类之前）买卖点计算逻辑。

        参数:
            bi_list: 笔列表，包含所有笔对象
            zs_list: 笔中枢列表，可直接遍历取多笔中枢

        生成买卖点请调用:
            self.add_bs(bs_type=BSP_TYPE.T3A, bi=..., relate_bsp1=..., feature_dict={...})
            self.add_bs(bs_type=BSP_TYPE.T3B, bi=..., relate_bsp1=..., feature_dict={...})
        """
        pass

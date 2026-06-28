from datetime import datetime
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

# 区间套背驰判断回调（由 my_chan_main 注入）
_sub_divergence_check = None

# 区间套背驰判断上下文（由 my_chan_main 注入）
_sub_divergence_code = None
_sub_divergence_market = None


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
        self.cal_bs3point(bi_list, zs_list)
        self.cal_bs1point(bi_list, zs_list)
        self.cal_bs2point(bi_list, zs_list)

    # ── 0类买卖点 ──
    def cal_bs0point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        # ═══════════════════════════════════════════════════════════
        # ── 共用检查 ──
        # ═══════════════════════════════════════════════════════════
        result = self._get_pivot_and_cur_bi(bi_list, zs_list)
        if result is None:
            return
        pivot_a, stroke_n = result
        if stroke_n.idx < pivot_a.begin_bi.idx + 2:
            return

        # ═══════════════════════════════════════════════════════════
        # ── 分支判断：第3笔 or 第n笔（n≥4）──
        # ═══════════════════════════════════════════════════════════
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
        # Cal_MACD_peak() 内部已按方向过滤
        #   向下笔只取绿柱(MACD<0)的绝对值最大值
        #   向上笔只取红柱(MACD>0)的绝对值最大值
        #   没有对应颜色柱子时返回 1e-7（≈0）
        stroke_1 = bi_list[pivot_a.begin_bi.idx]  # 中枢A的第1笔
        in_metric = stroke_1.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=False) # is_reverse 仅在 MACD_ALGO.AREA 时有用，对于 PEAK 和 FULL_AREA 无用
        out_metric = stroke_n.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=True) # is_reverse 仅在 MACD_ALGO.AREA 时有用，对于 PEAK 和 FULL_AREA 无用
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
        # 笔n属于中枢A（与中枢A有重叠）
        if not has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high):
            return

        # 笔n突破中枢A
        # 向上笔高点 >= 中枢A中间位
        # 向下笔低点 <= 中枢A中间位
        pivot_a_mid = (pivot_a.high + pivot_a.low) / 2  # 中枢A中间位
        if stroke_n.is_up():
            # 向上笔：末端高点 >= 中枢A中间位
            if stroke_n._high() < pivot_a_mid:
                return
        else:
            # 向下笔：末端低点 <= 中枢A中间位
            if stroke_n._low() > pivot_a_mid:
                return

        # 分型MACD拐头判断（右肩 vs 中间），MACD值取自合并K线内最后一根原始K线（klc.lst[-1].macd）
        if not self._check_fx_macd_inflection_point(stroke_n):
            return

        # 条件：n-3, n-2, n-1 重叠 -> 中枢B
        n_idx = stroke_n.idx
        s_nm1 = bi_list[n_idx - 1]  # 笔n-1
        s_nm2 = bi_list[n_idx - 2]  # 笔n-2
        s_nm3 = bi_list[n_idx - 3]  # 笔n-3
        s_nm4 = bi_list[n_idx - 4]  # 笔n-4（中枢B的进入笔）

        zs_b_low = max(s_nm1._low(), s_nm2._low(), s_nm3._low())
        zs_b_high = min(s_nm1._high(), s_nm2._high(), s_nm3._high())
        if zs_b_low > zs_b_high:  # 三笔无重叠，不构成中枢B
            return

        # 条件：笔n突破中枢B，且 n-4 也突破中枢B
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

        # 条件：MACD背驰判断（full_area）
        # 进入笔：n-4，离开笔：n
        in_metric = s_nm4.cal_macd_metric(MACD_ALGO.FULL_AREA, is_reverse=False)    # is_reverse 仅在 MACD_ALGO.AREA 时有用，对于 PEAK 和 FULL_AREA 无用
        out_metric = stroke_n.cal_macd_metric(MACD_ALGO.FULL_AREA, is_reverse=True) # is_reverse 仅在 MACD_ALGO.AREA 时有用，对于 PEAK 和 FULL_AREA 无用
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

    # ── 1类买卖点（对应缠论一类买卖点）──
    def cal_bs1point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        # ═══════════════════════════════════════════════════════════
        # ── 共用检查 ──
        # ═══════════════════════════════════════════════════════════
        result = self._get_pivot_and_cur_bi(bi_list, zs_list)
        if result is None:
            return
        pivot_a, stroke_n = result

        stroke_nm1 = bi_list[stroke_n.idx - 1]  # 前一笔N-1
        stroke_nm2 = bi_list[stroke_n.idx - 2]  # 前二笔N-2
        # ═══════════════════════════════════════════════════════════
        # 条件：笔N和N-1不跟中枢重叠，但笔N-2重叠
        # ═══════════════════════════════════════════════════════════
        n_overlap = has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high)
        nm1_overlap = has_overlap(stroke_nm1._low(), stroke_nm1._high(), pivot_a.low, pivot_a.high)
        nm2_overlap = has_overlap(stroke_nm2._low(), stroke_nm2._high(), pivot_a.low, pivot_a.high)

        if n_overlap or nm1_overlap or not nm2_overlap:
            return  # N或N-1与中枢重叠 或 N-2不与中枢重叠 → 不满足条件

        # ═══════════════════════════════════════════════════════════
        # 条件：笔N的极值必须突破笔N-2的极值
        # ═══════════════════════════════════════════════════════════
        # 向下笔（买点）：笔N低点 <= 笔N-2低点（创新低）
        # 向上笔（卖点）：笔N高点 >= 笔N-2高点（创新高）
        if stroke_n.is_down() and stroke_n._low() > stroke_nm2._low():
            return
        if stroke_n.is_up() and stroke_n._high() < stroke_nm2._high():
            return

        # ═══════════════════════════════════════════════════════════
        # 条件：分型MACD走弱判断（右肩 vs 中间）
        # ═══════════════════════════════════════════════════════════
        if not self._check_fx_macd_inflection_point(stroke_n):
            return

        # ═══════════════════════════════════════════════════════════
        # 条件：笔N-1上没有相反方向的买卖点
        # ═══════════════════════════════════════════════════════════
        # 笔N向下（买点）→ 笔N-1向上 → 笔N-1上不能有卖点
        # 笔N向上（卖点）→ 笔N-1向下 → 笔N-1上不能有买点
        if self._has_bsp_for_bi(stroke_nm1.idx):
            return

        # ═══════════════════════════════════════════════════════════
        # 条件：笔N与笔N-2的MACD峰值（PEAK）背驰判断
        # ═══════════════════════════════════════════════════════════
        # 注意：Cal_MACD_peak() 内部已按方向过滤——
        #   向下笔只取绿柱(MACD<0)的绝对值最大值；
        #   向上笔只取红柱(MACD>0)的绝对值最大值。
        #   没有对应颜色柱子时返回 1e-7（≈0）。
        in_metric = stroke_nm2.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=False) # is_reverse 仅在 MACD_ALGO.AREA 时有用，对于 PEAK 和 FULL_AREA 无用
        out_metric = stroke_n.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=True)   # is_reverse 仅在 MACD_ALGO.AREA 时有用，对于 PEAK 和 FULL_AREA 无用
        divergence_rate = out_metric / (in_metric + 1e-7)

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        is_diver = out_metric <= config.divergence_rate * in_metric

        if not is_diver:
            return

        # ═══════════════════════════════════════════════════════════
        # ── 生成1类买卖点 ──
        # ═══════════════════════════════════════════════════════════
        feature_dict = {
            'bsp1_bi_amp': stroke_n.amp(),
            'divergence_rate': divergence_rate,
        }
        self.add_bs(bs_type=BSP_TYPE.T1, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    # ── 2类买卖点 ──
    def cal_bs2point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        """TODO: 在此实现自定义的 2 类买卖点计算逻辑
        """
        pass

    # ── 3类买卖点（对应缠论三类买卖点）──
    def cal_bs3point(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        # ═══════════════════════════════════════════════════════════
        # ── 共用检查 ──
        # ═══════════════════════════════════════════════════════════
        result = self._get_pivot_and_cur_bi(bi_list, zs_list)
        if result is None:
            return
        pivot_a, stroke_n = result

        # ═══════════════════════════════════════════════════════════
        # 条件：笔N不跟最后一个中枢重叠，但笔N-1重叠
        # ═══════════════════════════════════════════════════════════
        stroke_nm1 = bi_list[stroke_n.idx - 1]  # 前一笔N-1
        n_overlap = has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high)
        nm1_overlap = has_overlap(stroke_nm1._low(), stroke_nm1._high(), pivot_a.low, pivot_a.high)

        if n_overlap or not nm1_overlap:
            return  # N与中枢重叠 或 N-1不与中枢重叠 → 不满足条件

        # ═══════════════════════════════════════════════════════════
        # 条件：分型MACD走弱判断（右肩 vs 中间）
        # ═══════════════════════════════════════════════════════════
        if not self._check_fx_macd_inflection_point(stroke_n):
            return

        # ═══════════════════════════════════════════════════════════
        # 条件：笔N-1上没有相反方向的买卖点
        # ═══════════════════════════════════════════════════════════
        # 笔N向下（买点）→ 笔N-1向上 → 笔N-1上不能有卖点
        # 笔N向上（卖点）→ 笔N-1向下 → 笔N-1上不能有买点
        if self._has_bsp_for_bi(stroke_nm1.idx):
            return

        # ═══════════════════════════════════════════════════════════
        # ── 生成3类买卖点 ──
        # ═══════════════════════════════════════════════════════════
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
        """检查分型MACD是否满足拐点条件（右肩K线 vs 中间K线）。

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


def _analyze_sub_level_divergence(bi_list):
    """
    区间套背驰判断：分析高级别最后一笔在低级别是否MACD背驰。

    code/market 从模块级变量 _sub_divergence_code / _sub_divergence_market 读取，
    由 my_chan_main 在 step_load 前设置。

    参数:
        bi_list: 高级别笔列表（由 cal_bs0point 直接传入）

    返回:
        {"diverged": True/False, "detail": "..."}
    """
    # 局部导入避免循环依赖
    import my_chan_main as _main

    code = _sub_divergence_code
    market = _sub_divergence_market

    # ── 1. 前置检查 ──
    high_bi = bi_list[-1]

    high_kl_type = bi_list[0].get_begin_klu().kl_type
    _KL_TYPE_TO_FREQ = {KL_TYPE.K_WEEK: 'w', KL_TYPE.K_DAY: 'd', KL_TYPE.K_30M: '30m', KL_TYPE.K_5M: '5m'}
    high_freq = _KL_TYPE_TO_FREQ.get(high_kl_type)
    if high_freq is None:
        return {"diverged": False, "detail": f"不支持的K线类型: {high_kl_type}"}
    if high_freq not in _main._SUB_FREQ_MAP:
        return {"diverged": False, "detail": f"周期 {high_freq} 无对应的低级别，跳过背驰判断"}
    sub_freq = _main._SUB_FREQ_MAP[high_freq]

    # ── 2. 确定 A（左边界）和 B（右边界）时间 ──
    # 逻辑与双窗口红框完全一致：左肩第一根K线之后 → 右肩最后一根K线
    shoulder_times = _main._get_bi_fx_shoulder_times(high_bi, high_freq)
    if shoulder_times is None:
        return {"diverged": False, "detail": "无法确定笔的分型肩部时间"}
    fx_a_dt, fx_b_dt = shoulder_times

    high_bi_direction = _main._get_bi_direction_str(high_bi)
    print(f"[区间套] 红框边界: 左肩={fx_a_dt}, 右肩={fx_b_dt}, 低级别={sub_freq}, 方向={high_bi_direction}")

    # ── 3. 从缓存获取次级别已计算好的笔列表 ──
    if not code or not market:
        return {"diverged": False, "detail": "当前无分析上下文（_sub_divergence_code/_sub_divergence_market 未设置）"}

    # 尝试从缓存获取次级别完整数据（第一次读不到则冷启动加载）
    cache_key = f"single_{market}_{code}_{sub_freq}"
    cached = _main._cache_get(cache_key)
    if cached is None or "chan" not in cached:
        print(f"[区间套] 缓存未命中: {cache_key}，触发冷启动加载次级别 {sub_freq}")
        # 冷启动：暂时释放 _stock_analysis_lock 避免死锁，
        # 同时保存/恢复上下文防止被覆盖
        saved_code = _sub_divergence_code
        saved_market = _sub_divergence_market
        _main._stock_analysis_lock.release()
        try:
            _main.analyze_stock(f"{code}.{market.upper()}", freq=sub_freq, cache_chan=True)
        finally:
            _main._stock_analysis_lock.acquire()
            _sub_divergence_code = saved_code
            _sub_divergence_market = saved_market
        cached = _main._cache_get(cache_key)
        if cached is None or "chan" not in cached:
            print(f"[区间套] 冷启动后仍找不到低级别数据: {cache_key}，按不背驰处理")
            return {"diverged": False, "detail": f"次级别{sub_freq}冷启动加载失败，无法分析"}

    # 获取次级别完整chan对象和笔列表 L1
    sub_date_fmt = "%Y-%m-%d %H:%M" if sub_freq in _main.INTRADAY_FREQS else "%Y-%m-%d"
    sub_kl_list = cached["chan"][_main._get_kl_type(sub_freq)]
    sub_bi_list_full = sub_kl_list.bi_list
    full_records = cached["records"]

    if len(sub_bi_list_full) == 0:
        return {"diverged": False, "detail": f"次级别{sub_freq}笔列表为空，无法分析"}

    print(f"[区间套] 从缓存获取次级别数据: {len(full_records)}条K线, {len(sub_bi_list_full)}笔")

    # ── 4. 在已有笔列表 L1 中找到 [start_bi, end_bi] ──
    # start_bi: 第一个被 [A, B] 完全覆盖的笔
    # end_bi:   最后一个被 [A, B] 完全覆盖的笔
    # "完全覆盖" = 笔.start_time >= A 且 笔.end_time <= B
    def _bi_in_range(bi, a_dt_str, b_dt_str):
        """检查笔是否完全在 [A, B] 时间区间内"""
        try:
            s_time_str = bi.get_begin_klu().time.to_str()
            e_time_str = bi.get_end_klu().time.to_str()
            s_dt = s_time_str[:len(a_dt_str)].replace("/", "-")
            e_dt = e_time_str[:len(b_dt_str)].replace("/", "-")
            # 字符串比较在 YYYY-MM-DD 格式下直接可用
            return s_dt >= a_dt_str and e_dt <= b_dt_str
        except Exception:
            return False

    start_bi_idx = None
    end_bi_idx = None

    for i, bi in enumerate(sub_bi_list_full):
        if _bi_in_range(bi, fx_a_dt, fx_b_dt):
            if start_bi_idx is None:
                start_bi_idx = i
            end_bi_idx = i

    if start_bi_idx is None or end_bi_idx is None:
        print(f"[区间套] 在次级别笔列表中找不到完全被[A, B]覆盖的笔")
        return {"diverged": False, "detail": f"次级别中找不到完全覆盖区间的笔，A={fx_a_dt}, B={fx_b_dt}"}

    if end_bi_idx - start_bi_idx + 1 < 5:
        # 至少需要5笔：1进入段 + 4构成中枢（含确认）
        print(f"[区间套] 覆盖范围内仅{end_bi_idx - start_bi_idx + 1}笔，至少需要5笔才能构建中枢")
        return {"diverged": False, "detail": f"区间内仅{end_bi_idx - start_bi_idx + 1}笔，至少需要5笔才能分析"}

    # 截取笔列表 L2 = [start_bi_idx, end_bi_idx]（包含两端）
    sub_bi_list = list(sub_bi_list_full[start_bi_idx:end_bi_idx + 1])
    print(f"[区间套] 找到次级别笔范围: 索引[{start_bi_idx} ~ {end_bi_idx}], 共{len(sub_bi_list)}笔")

    # ── 5. 基于 L2 重新计算中枢 ──
    zs_data, zs_stars = _main._build_zs_from_bis(sub_bi_list, sub_bi_list_full, sub_date_fmt)

    # 将 zs_data 转换为简易中枢对象，供后续背驰判断使用
    class _DummyZS:
        __slots__ = ('high', 'low')
        def __init__(self, zs_info):
            self.high = zs_info['zg']
            self.low = zs_info['zd']
    sub_zs_list = [_DummyZS(zs_info) for zs_info in zs_data]

    # ── 6. 提取分析区间（仅用于日志），MACD 用全量数据计算以保证准确 ──
    # 左边界：在次级别找 <= X 的最后一根K线，其下一根即为左边界
    a_idx = -1
    for i, r in enumerate(full_records):
        r_date_str = r["dt"].strftime(sub_date_fmt)
        if r_date_str <= fx_a_dt:
            a_idx = i + 1
    # 右边界：在次级别找 <= Y 的最后一根K线，即为右边界
    # 注意：fx_b_dt 可能比 r_date_str 短（如日K vs 30m），需截断到相同长度后比较
    b_len = len(fx_b_dt)
    b_idx = -1
    for i, r in enumerate(full_records):
        r_date_str = r["dt"].strftime(sub_date_fmt)
        if r_date_str[:b_len] <= fx_b_dt:
            b_idx = i

    if a_idx == -1:
        a_idx = 0
    if b_idx == -1:
        b_idx = len(full_records) - 1
    if a_idx > b_idx:
        return {"diverged": False, "detail": f"低级别数据中无法找到有效K线区间: a_idx={a_idx}, b_idx={b_idx}"}

    analysis_records = full_records[a_idx:b_idx + 1]
    if len(analysis_records) < 5:
        return {"diverged": False, "detail": f"分析区间内K线数据不足: 仅{len(analysis_records)}条"}

    a_time = analysis_records[0]["dt"].strftime(sub_date_fmt)
    b_time = analysis_records[-1]["dt"].strftime(sub_date_fmt)
    print(f"[区间套] 分析区间: {a_time} → {b_time}, {len(analysis_records)}条K线, {len(sub_bi_list)}笔, {len(sub_zs_list)}中枢")

    # MACD 使用全量 full_records 计算，不截取（截取K线太少会导致MACD不准）
    full_closes = [r["close"] for r in full_records]
    sub_macd_list = _main.calculate_macd(full_closes)
    print(f"[区间套] MACD基于全量 {len(full_records)} 条K线计算")

    # ── 7. 分析次级别走势结构（沿用原有逻辑） ──
    bi_count = len(sub_bi_list)
    zs_count = len(sub_zs_list)

    # 提取笔简要信息
    sub_bis_info = []
    for i, bi in enumerate(sub_bi_list):
        try:
            s = bi.get_begin_klu().time.to_str()
            e = bi.get_end_klu().time.to_str()
            s_dt = datetime.strptime(s, "%Y/%m/%d %H:%M").strftime(sub_date_fmt)
            e_dt = datetime.strptime(e, "%Y/%m/%d %H:%M").strftime(sub_date_fmt)
        except:
            s_dt = s[:16].replace("/", "-") if sub_freq in _main.INTRADAY_FREQS else s[:10].replace("/", "-")
            e_dt = e[:16].replace("/", "-") if sub_freq in _main.INTRADAY_FREQS else e[:10].replace("/", "-")
        sub_bis_info.append({
            "idx": i,
            "sdt": s_dt,
            "edt": e_dt,
            "direction": "up" if bi.is_up() else "down",
            "high": round(bi._high(), 2),
            "low": round(bi._low(), 2),
        })

    # 提取中枢简要信息
    sub_zs_info = []
    for zs_info in zs_data:
        sub_zs_info.append({
            "sdt": zs_info.get("sdt", ""),
            "edt": zs_info.get("edt", ""),
            "zg": zs_info["zg"],
            "zd": zs_info["zd"],
        })

    # ── 8. 判断走势类型（沿用原有逻辑） ──
    result_type = None
    detail = ""
    divergence = None  # 背驰信息

    if bi_count == 0:
        result_type = "single_bi"
        detail = "次级别无笔（数据不足）"
    elif bi_count == 1:
        result_type = "single_bi"
        detail = f"次级别仅一笔（{sub_bis_info[0]['direction']}），未形成中枢"
        # 单笔背驰判断
        divergence = _main._check_single_bi_divergence(
            sub_bi_list[0], full_records, sub_macd_list, sub_date_fmt
        )
        if divergence is not None:
            if divergence["diverged"]:
                detail += f"，但MACD背驰（笔内最高MACD柱={divergence['max_macd']:.2f}，末端={divergence['end_macd']:.2f}）"
            else:
                detail += f"，未背驰（笔内最高MACD柱={divergence['max_macd']:.2f}，末端={divergence['end_macd']:.2f}）"
    elif zs_count == 0:
        is_trend = _main._check_trend_maintained(sub_bis_info, high_bi_direction)
        if is_trend:
            result_type = "multi_bi_trend"
            detail = f"次级别由{bi_count}笔构成，保持{high_bi_direction}趋势（反向笔未破前笔极值）"
        else:
            result_type = "multi_bi_trend"
            detail = f"次级别由{bi_count}笔构成，但趋势已被破坏（反向笔破了前一笔极值）"
        # 多笔背驰判断：比较最后两个同向笔的MACD柱子
        if bi_count >= 3:
            divergence = _main._check_multi_bi_divergence(
                sub_bi_list, full_records, sub_macd_list, sub_date_fmt, high_bi_direction
            )
            if divergence is not None:
                if divergence["diverged"]:
                    detail += f"，MACD背驰（前笔峰值={divergence['prev_macd']:.2f}，后笔峰值={divergence['curr_macd']:.2f}）"
                else:
                    detail += f"，未背驰（前笔峰值={divergence['prev_macd']:.2f}，后笔峰值={divergence['curr_macd']:.2f}）"
    elif zs_count >= 1:
        result_type = "one_zs"
        zs = sub_zs_info[0]
        detail = f"次级别由{bi_count}笔构成，形成{zs_count}个中枢（ZG={zs['zg']}, ZD={zs['zd']}）"
        # 单中枢背驰判断：进入段 vs 离开段 MACD面积比较
        divergence = _main._check_one_zs_divergence(
            sub_bi_list, sub_zs_list, full_records, sub_macd_list, high_bi_direction
        )
        if divergence is not None:
            if divergence["diverged"]:
                detail += f"，MACD面积背驰（进入段面积={divergence['in_area']:.2f}，离开段面积={divergence['out_area']:.2f}）"
            else:
                detail += f"，未背驰（进入段面积={divergence['in_area']:.2f}，离开段面积={divergence['out_area']:.2f}）"
    else:
        result_type = "multi_zs"
        detail = f"次级别由{bi_count}笔构成，形成{zs_count}个中枢"

    # 汇总背驰结论
    diverged = divergence is not None and divergence.get("diverged", False)

    # 清理局部变量（缓存对象保留）
    import gc
    del zs_data, zs_stars
    gc.collect()
    return {"diverged": diverged, "detail": detail}
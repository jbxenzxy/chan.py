import threading
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

# ── 复盘调试标志（线程局部）──────────────────────────────────────────
# 原 CMyBSPointList.REPLAY_MODE 是进程级类变量，被每条分析链路在入口改写、
# 出口复原。它只用于 _dbg_bs* 的调试打印开关（不影响计算结果），但作为
# 进程级可变全局，一旦分析路径不再被串行锁保护，并发请求会互相串改调试
# 标志。改为线程局部后，调试打印按线程正确归属，且无需任何锁。
_REPLAY_LOCAL = threading.local()


def set_replay_mode(on: bool) -> bool:
    """设置本线程复盘调试标志，返回设置前的原值（供 finally 恢复）。"""
    prev = getattr(_REPLAY_LOCAL, "on", False)
    _REPLAY_LOCAL.on = bool(on)
    return prev


def in_replay_mode() -> bool:
    """本线程当前是否处于复盘调试模式。"""
    return bool(getattr(_REPLAY_LOCAL, "on", False))

# 区间套背驰判断（双窗口模式下由引擎层调用）


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
_STOCKS_SUB_FREQ_MAP = {'w': 'd', 'd': '30m', '30m': '5m'}
_FUTURES_SUB_FREQ_MAP = {'30m': '5m', '5m': '1m', '1m': '15s'}


class CMyBSPointList(CBSPointList[LINE_TYPE, LINE_LIST_TYPE]):
    """
    自定义买卖点计算类

    继承 CBSPointList，将 cal() 中的调用替换为 cal_bs0point ~ cal_bs3point
    你只需修改下面四个方法即可实现自己的买卖点逻辑。

    与线段（seg_list）完全解耦，只依赖笔列表（bi_list）和笔中枢列表（zs_list）
    0类买卖点已通过 BSP_TYPE.T0 枚举正规化，无需额外 hack。

    self.parent 是父级 CKLine_List 的反向引用，从父级获取 code 和 kl_type
    """

    # ── 买卖点调试开关 ──
    DEBUG_BS0 = True               # 0类调试总开关
    DEBUG_BS1 = True               # 1类调试总开关
    DEBUG_BS2 = True               # 2类调试总开关
    DEBUG_BS3 = True               # 3类调试总开关
    DEBUG_BS = True                # 区间套背驰调试总开关
    # REPLAY_MODE 已移除：原为进程级类变量，现由模块级线程局部函数
    # set_replay_mode() / in_replay_mode() 承担（见本文件顶部）。
    BS0_ZS_BREAK_RATIO = 0.8       # 离开笔有效突破中枢的比例阈值
    BS0_OUT_IN_RATIO = 0.8         # 离开笔振幅 >= 进入笔振幅的比例阈值
    NESTED_MACD_DIVER_RATIO = 0.8  # 次级别单笔或多笔，MACD背驰判断阈值
    ZS_ENTRY_AMP_RATIO = 1.2       # 中枢进入笔振幅比例阈值（进入笔振幅 >= 构成中枢第三笔振幅 × 该比例）

    def __init__(self, bs_point_config):
        super().__init__(bs_point_config)
        # 反向引用父级 CKLine_List，由 CKLine_List.__init__ 设置
        self.parent = None  # 指向 CKLine_List

        # 进入段与离开段 MACD 面积背驰比较开关
        # True  = 正向时比较进入段与离开段 MACD 面积背驰，反向时跳过（原始逻辑）
        # False = 无论正向/反向，始终跳过面积背驰比较（实验方案）
        self.ENABLE_ENTRY_EXIT_AREA_DIVERGENCE = False

    def _get_freq(self) -> str:
        """从 self.parent.kl_type 推导周期字符串，用于调试输出前缀区分上下窗。"""
        parent = self.parent
        if parent is None:
            return '?'
        return _KL_TYPE_TO_FREQ.get(parent.kl_type, '?')

    def _dbg_bs0(self, func, msg, **kwargs):
        """0类买卖点调试输出（仅在复盘模式下生效）。
        Args:
            func: 函数名
            msg: 调试信息
            **kwargs: 附加键值对，自动格式化
        """
        if not self.DEBUG_BS0 or not in_replay_mode():
            return
        extra = ' | '.join(f'{k}={v:.2f}' if isinstance(v, float) else f'{k}={v}' for k, v in kwargs.items()) if kwargs else ''
        freq = self._get_freq()
        line = f'[BS0][{freq}] {func}: {msg}'
        if extra:
            line += f' | {extra}'
        print(line)

    def _dbg_bs1(self, func, msg, **kwargs):
        """1类买卖点调试输出（仅在复盘模式下生效）。
        Args:
            func: 函数名
            msg: 调试信息
            **kwargs: 附加键值对，自动格式化
        """
        if not self.DEBUG_BS1 or not in_replay_mode():
            return
        extra = ' | '.join(f'{k}={v:.2f}' if isinstance(v, float) else f'{k}={v}' for k, v in kwargs.items()) if kwargs else ''
        freq = self._get_freq()
        line = f'[BS1][{freq}] {func}: {msg}'
        if extra:
            line += f' | {extra}'
        print(line)

    def _dbg_bs2(self, func, msg, **kwargs):
        """2类买卖点调试输出（仅在复盘模式下生效）。
        Args:
            func: 函数名
            msg: 调试信息
            **kwargs: 附加键值对，自动格式化
        """
        if not self.DEBUG_BS2 or not in_replay_mode():
            return
        extra = ' | '.join(f'{k}={v:.2f}' if isinstance(v, float) else f'{k}={v}' for k, v in kwargs.items()) if kwargs else ''
        freq = self._get_freq()
        line = f'[BS2][{freq}] {func}: {msg}'
        if extra:
            line += f' | {extra}'
        print(line)

    def _dbg_bs3(self, func, msg, **kwargs):
        """3类买卖点调试输出（仅在复盘模式下生效）。
        Args:
            func: 函数名
            msg: 调试信息
            **kwargs: 附加键值对，自动格式化
        """
        if not self.DEBUG_BS3 or not in_replay_mode():
            return
        extra = ' | '.join(f'{k}={v:.2f}' if isinstance(v, float) else f'{k}={v}' for k, v in kwargs.items()) if kwargs else ''
        freq = self._get_freq()
        line = f'[BS3][{freq}] {func}: {msg}'
        if extra:
            line += f' | {extra}'
        print(line)

    def _dbg_bs(self, func, msg, **kwargs):
        """区间套背驰调试输出（仅在复盘模式下生效）。
        Args:
            func: 函数名
            msg: 调试信息
            **kwargs: 附加键值对，自动格式化
        """
        if not self.DEBUG_BS or not in_replay_mode():
            return
        extra = ' | '.join(f'{k}={v:.2f}' if isinstance(v, float) else f'{k}={v}' for k, v in kwargs.items()) if kwargs else ''
        freq = self._get_freq()
        line = f'[BS][{freq}] {func}: {msg}'
        if extra:
            line += f' | {extra}'
        print(line)

    # ── 入口 ──
    def cal(self, bi_list: LINE_LIST_TYPE, zs_list=None):
        # ① 中枢/实笔/分型 前置检查
        result = self.cal_bsp_precondition(bi_list, zs_list)
        if result is None:
            return
        pivot_a, stroke_n = result

        # ② 区间套不背 → 静默返回
        is_diver = self.check_nested_diver(bi_list, zs_list)
        if not is_diver:
            return
        
        self.cal_bs0point(bi_list, zs_list, pivot_a, stroke_n)
        self.cal_bs1point(bi_list, zs_list, pivot_a, stroke_n)
        self.cal_bs2point(bi_list, zs_list, pivot_a, stroke_n)
        self.cal_bs3point(bi_list, zs_list, pivot_a, stroke_n)

    @classmethod
    def cal_bsp_precondition(cls, bi_list, zs_list):
        """买卖点前置检查：中枢存在 + 实笔 + 强势分型，缺一即返回 None"""
        # ⑴ 中枢检查
        if zs_list is None or len(zs_list) == 0:
            return None
        pivot_a = None
        for zs in reversed(zs_list):
            if not zs.is_one_bi_zs():
                pivot_a = zs
                break
        if pivot_a is None:
            return None

        # ⑵ 实笔检查（过滤虚笔）
        stroke_n = bi_list[-1]
        if cls._is_virtual_bi(bi_list):
            return None

        # ⑶ 分型检查（过滤弱分型/中继）
        if cls._is_strong_fx(stroke_n) == 0:
            return None

        return (pivot_a, stroke_n)

    def check_nested_diver(self, bi_list, zs_list):
        """
        区间套背驰判断：分析主级别一笔在子级别是否段背(无中枢) 或 有买/卖点(有中枢)
        code、freq 从 self.parent 获取(CKLine_List 创建时就有的固有属性)
        sub_freq 由 freq 自动推导(双窗口配对固定)
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
        # 双窗口 freq 配对：上窗 main_freq 确定下窗 sub_freq
        if is_stocks:
            sub_freq = _STOCKS_SUB_FREQ_MAP.get(main_freq)
        else:
            sub_freq = _FUTURES_SUB_FREQ_MAP.get(main_freq)

        sub_kl_type = _get_kl_type(sub_freq)

        if is_stocks:
            # ── 股票（双窗双路径）─────────────────────
            # 独立双窗：上窗 CChan 携带显式下窗周期（chan._stocks_dual_sub_freq，
            # AppEngine 建上窗前设置），区间套改读「下窗 CChan 运行时缓存」——
            # 下窗先建整读（先下后上时序），主K线计算时下窗笔结构已完整。
            _explicit_sub_freq = None
            if parent.chan is not None:
                _explicit_sub_freq = getattr(parent.chan, "_stocks_dual_sub_freq", None)
            if _explicit_sub_freq:
                sub_freq = _explicit_sub_freq       # 三套映射统一：显式传入优先
                sub_kl_type = _get_kl_type(sub_freq)
                from App.AppData import app_data
                # 审计 P0-2：缓存里的 CChan 是**活对象**，取对象与读 kl_datas
                # 必须在同一把锁内完成（写侧每次分析会整条替换缓存条目）。
                with app_data.stocks_sub_chan_guarded(parent.code, sub_freq) as sub_chan:
                    if sub_chan is None:
                        # 先下后上时序下不应发生（下窗先建缓存再建上窗）；
                        # 仅服务重启/缓存被清等异常态，语义=初始化竞态而非配置错误
                        self._dbg_bs('check_nested_diver', '独立双窗-下窗运行时缓存缺失'
                                     '（先下后上时序被破坏或缓存被清理）→ 按子级别背驰处理',
                                     code=parent.code, sub_freq=sub_freq)
                        return True  # 按子级别背驰处理
                    sub_kl_list = sub_chan[sub_kl_type]
                    # 审计 U2（P1）：快照必须在**锁内**做。缓存条目会被分析
                    # 线程整条替换，出锁后再 list(...) 就可能与替换交错——
                    # 期货侧同款问题已修（见下方 else 分支 :771），股票侧漏了
                    # 同一处。临界区压到最小：锁内只做浅拷贝，随即出锁遍历。
                    sub_bi_list = list(sub_kl_list.bi_list)
            else:
                # 单窗口 / 独立双窗下窗 / legacy 联立：显式判 kl_datas 是否含次级别
                # （替代原 try/KeyError 异常控制流，行为等价）：
                #   单窗、独立双窗下窗 lv_list 仅本级 → None → 按子级别背驰处理；
                #   legacy 联立上窗 lv_list 两级 → 取到联立子级别，走区间套。
                if parent.chan is None:
                    raise RuntimeError("[check_nested_diver] 严重Bug：CChan 指针未设置！")
                sub_kl_list = parent.chan.kl_datas.get(sub_kl_type)
                if sub_kl_list is None:
                    self._dbg_bs('check_nested_diver', '单窗口无子 or 双窗口无孙 → 按子级别背驰处理',
                                 sub_kl_type=sub_kl_type)
                    return True  # 按子级别背驰处理
                # parent.chan 为本次分析自有的 CChan（不跨连接共享，风险低于
                # 缓存路径），仍统一在取到后立即快照，保持与双窗路径同一口径，
                # 避免指针被带出作用域后再遍历。
                sub_bi_list = list(sub_kl_list.bi_list)
        else:
            # 期货：上下窗是独立的 CChan 对象，下窗缓存在 _futures_analysis_cache 中
            # 时序约定（区间套）：实时循环先处理下窗(次级别)再处理上窗(主级别)，
            # 保证此处取到的下窗 sub_chan 是已更新到最新的笔结构。
            # （下窗缓存经 AppData 公共 API 读取）
            from App.AppData import app_data, make_futures_sub_key_from_code
            # P0-2 修复：parent.code 形如 "SYMBOL:freq_sec"（CChan.code 含周期后缀），
            # 经 make_futures_sub_key_from_code 去掉 ":freq_sec" 还原纯 symbol 后
            # 委托 make_futures_sub_key 工厂生成下窗缓存 key（大小写/分隔符单一
            # 事实源）。此前直接以 parent.code 拼接 "SYMBOL:freq_sec:sub_freq"，
            # 与写侧 set_futures_sub_chan 的 "SYMBOL:sub_freq" 永不相等，导致
            # 期货区间套 100% 静默失效。
            cache_key = make_futures_sub_key_from_code(parent.code, sub_freq)
            # 审计 P0-2：缓存里存的是**活着的 CChan**，SSE 每根K线
            # _drain_chan → step_load → do_init 会**就地清空重建** kl_datas
            # （Chan.py do_init 把 self.kl_datas 整个换成新的空 CKLine_List）。
            # 原先 `futures_cache_get` 只在**取指针**这一瞬间持容器锁，随后
            # `sub_kl_list.bi_list` 的遍历完全在锁外——正好撞在 do_init 之后、
            # 回填之前就会读到空列表。故取对象与遍历须在同一把对象图锁内。
            with app_data.futures_sub_chan_guarded_by_key(cache_key) as sub_chan:
                if sub_chan is None:
                    self._dbg_bs('check_nested_diver', '期货下窗暂无缓存 → 按子级别背驰处理',
                                 cache_key=cache_key, sub_freq=sub_freq) # 上/下窗分开加载，必然有前有后，所以存在“上窗有，下窗无”的情况
                    return True # 按子级别背驰处理
                sub_kl_list = sub_chan[sub_kl_type]
                # 临界区压到最小：锁内只做浅拷贝，随即出锁。
                # do_init 是**整体替换** kl_datas，旧的 CBi 对象不再被引擎
                # 引用、不会被就地改写，故 list(...) 即等价于不可变快照。
                sub_bi_list = list(sub_kl_list.bi_list)
            # 以下对 sub_bi_list 的使用均在锁外（快照已与引擎内部状态解耦）
        # 注：股票、期货两条路径均已在其**各自的临界区内**完成 sub_bi_list
        # 快照（股票 :730-736 / :738-744，期货 :771），此处不再存在
        # 「锁外取指针后遍历」的窗口（审计 U2 / P0-2 双侧闭合）。
        if len(sub_bi_list) == 0:
            # 上/下窗，历史K线不对齐(如：日K加载多于30分)
            self._dbg_bs('check_nested_diver', '双窗口-无子级别 → 按子级别背驰处理')
            return True  # 按子级别背驰处理
        
        # 1. 确定主级别一笔的左右边界 [A,B]
        main_bi = bi_list[-1]
        main_date_fmt = _get_date_fmt(main_freq)
        shoulder_result = _main_bi_range(main_bi, main_date_fmt)
        if shoulder_result is None:
            raise RuntimeError(f"[check_nested_diver] 严重Bug：无法确定主级别[A,B]: main_bi.idx={main_bi.idx}")
        fx_a_raw_dt, fx_b_raw_dt, a_klu, b_klu = shoulder_result

        # 2. 确定子级别红框边界 [C,D]
        fx_a_sub_dt, fx_b_sub_dt = None, None
        if is_stocks:
            if _explicit_sub_freq:
                # P3：独立双窗——上窗 KLU 无联立 sub_kl_list，改数学换算
                # （结束时间语义，与期货公式镜像对称）
                fx_a_sub_dt, fx_b_sub_dt = _stocks_red_range_algo(
                    fx_a_raw_dt, fx_b_raw_dt, main_freq, _explicit_sub_freq)
            else:
                # legacy 联立：从左右肩 KLU 的 sub_kl_list 取真实子级别边界
                fx_a_sub_dt, fx_b_sub_dt = _stocks_red_range(a_klu, b_klu, sub_freq, main_bi)
        else:
            from DataAPI.TqSdkAPI import CTqSdkAPI
            main_freq_sec = CTqSdkAPI.FREQ_SEC_MAP[main_freq]
            sub_freq_sec = CTqSdkAPI.FREQ_SEC_MAP[sub_freq]
            snapshot = {'bis': [{'fx_a_raw_dt': fx_a_raw_dt, 'fx_b_raw_dt': fx_b_raw_dt}]}
            _futures_red_range(snapshot, main_freq_sec, sub_freq_sec, sub_freq)
            fx_a_sub_dt = snapshot['bis'][0].get('fx_a_sub_dt', '')
            fx_b_sub_dt = snapshot['bis'][0].get('fx_b_sub_dt', '')

        if not fx_a_sub_dt or not fx_b_sub_dt:
            self._dbg_bs('check_nested_diver', '无法确定子级别[C,D] → 可能子级别K线不够',
                         fx_a=fx_a_sub_dt, fx_b=fx_b_sub_dt)
            return True  # 按子级别背驰处理

        # 3. 在子级别笔列表中找被红框完全覆盖的笔
        start_bi_idx, end_bi_idx = _red_range_bi_sequence(fx_a_sub_dt, fx_b_sub_dt, sub_bi_list, sub_freq)
        if start_bi_idx is None or end_bi_idx is None:
            self._dbg_bs('check_nested_diver', '找不到被红框[C,D]完全覆盖的笔',
                         fx_a=fx_a_sub_dt, fx_b=fx_b_sub_dt)
            return True  # 按子级别背驰处理

        sub_bi_sliced = list(sub_bi_list[start_bi_idx:end_bi_idx + 1])
        bi_count = len(sub_bi_sliced)
        self._dbg_bs('check_nested_diver', '子级别笔范围',
                     start_bi_idx=start_bi_idx, end_bi_idx=end_bi_idx,
                     bi_count=bi_count)

        # 4. 判断子级别笔序列是否形成中枢
        sub_date_fmt = _get_date_fmt(sub_freq)
        zs_data = _red_range_amp(sub_bi_sliced, sub_bi_list, sub_date_fmt)
        has_zs = len(zs_data) > 0
        # 场景一：笔序列形成中枢(1个或多个)
        if has_zs:
            result = _red_range_zs_diver(sub_bi_sliced, main_bi, zs_data)
            self._dbg_bs('check_nested_diver', '有中枢背驰判断',
                         detail=result['detail'], diverged=result['diverged'])
            return result['diverged']

        # 场景二：笔序列不形成中枢
        if bi_count == 1:
            # 子场景⑴：仅一笔
            result = _red_range_single_bi_diver(sub_bi_sliced[0])
            self._dbg_bs('check_nested_diver', '无中枢单笔背驰判断',
                         detail=result['detail'], diverged=result['diverged'])
            return result['diverged']
        else:
            # 子场景⑵：有多笔
            result = _red_range_multi_bi_diver(sub_bi_sliced)
            self._dbg_bs('check_nested_diver', '无中枢多笔背驰判断',
                         detail=result['detail'], diverged=result['diverged'])
            return result['diverged']

    # ═══════════════════════════════════════════════════════════
    # ── 0类买卖点(中枢震荡) ──
    # ═══════════════════════════════════════════════════════════
    def cal_bs0point(self, bi_list: LINE_LIST_TYPE, zs_list=None, pivot_a=None, stroke_n=None):
        self._dbg_bs0('cal_bs0point', '进入......', bi_idx=len(bi_list)-1)

        # 笔N与中枢A要有重叠
        if not has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high):
            self._dbg_bs0('cal_bs0point', '跳过: 重叠条件不满足',
                          stroke_high=stroke_n._high(), stroke_low=stroke_n._low(),
                          zs_high=pivot_a.high, zs_low=pivot_a.low)
            return

        # 分流：第3笔 / 第4笔 / 第n笔（n≥5）
        nth_in_pivot = stroke_n.idx - pivot_a.begin_bi.idx + 1
        if stroke_n.idx == pivot_a.begin_bi.idx + 2:
            self._dbg_bs0('cal_bs0point', '走第3笔分支', stroke_n_idx=stroke_n.idx,
                          zs_high=pivot_a.high, zs_low=pivot_a.low)
            self._cal_bs0point_3rd(bi_list, pivot_a, stroke_n)
        elif stroke_n.idx == pivot_a.begin_bi.idx + 3:
            self._dbg_bs0('cal_bs0point', '走第4笔分支', stroke_n_idx=stroke_n.idx,
                          nth_in_pivot=nth_in_pivot, zs_high=pivot_a.high, zs_low=pivot_a.low)
            self._cal_bs0point_4th(bi_list, pivot_a, stroke_n)
        else:
            self._dbg_bs0('cal_bs0point', '走第n笔分支', stroke_n_idx=stroke_n.idx,
                          nth_in_pivot=nth_in_pivot, zs_high=pivot_a.high, zs_low=pivot_a.low)
            self._cal_bs0point_nth(bi_list, pivot_a, stroke_n)

    # ── 第3笔（中枢A形成笔）──
    def _cal_bs0point_3rd(self, bi_list, pivot_a, stroke_n):
        self._dbg_bs0('_cal_bs0point_3rd', '进入', stroke_n_idx=stroke_n.idx,
                      stroke_dir='up' if stroke_n.is_up() else 'down')

        # 笔C振幅不足(相比笔A)，直接跳过
        # stroke_1 = bi_list[pivot_a.begin_bi.idx]
        # if not self._is_valid_out_in_amp(stroke_n, stroke_1):
        #     self._dbg_bs0('_cal_bs0point_3rd', '跳过: C笔振幅不足',
        #                   stroke_n_amp=round(stroke_n.amp(), 2),
        #                   stroke_1_amp=round(stroke_1.amp(), 2),
        #                   threshold=round(stroke_1.amp() * CMyBSPointList.BS0_OUT_IN_RATIO, 2))
        #     return

        # 笔C的极值，必须突破笔A的极值
        # 向下笔(买点)：笔C低点 < 笔A低点(创新低)
        # 向上笔(卖点)：笔C高点 > 笔A高点(创新高)
        stroke_a = bi_list[pivot_a.begin_bi.idx]
        if stroke_n.is_down() and stroke_n._low() >= stroke_a._low():
            self._dbg_bs0('_cal_bs0point_3rd', '跳过: 笔C未创新低(相比笔A)',
                          c_idx=stroke_n.idx,
                          a_idx=stroke_a.idx,
                          c_low=stroke_n._low(), a_low=stroke_a._low())
            return
        if stroke_n.is_up() and stroke_n._high() <= stroke_a._high():
            self._dbg_bs0('_cal_bs0point_3rd', '跳过: 笔C未创新高(相比笔A)',
                          c_idx=stroke_n.idx,
                          a_idx=stroke_a.idx,
                          c_high=stroke_n._high(), a_high=stroke_a._high())
            return

        # 确保A、B、C三笔为标准🗲走势
        # 向下笔C(买点)：笔B(向上)的高点 < 笔A的高点
        # 向上笔C(卖点)：笔B(向下)的低点 > 笔A的低点
        stroke_b = bi_list[stroke_n.idx - 1]
        if stroke_n.is_down() and stroke_b._high() >= stroke_a._high():
            self._dbg_bs0('_cal_bs0point_3rd', '跳过: 笔A、B、C 非闪电走势',
                          b_idx=stroke_b.idx,
                          a_idx=stroke_a.idx,
                          b_high=stroke_b._high(), a_high=stroke_a._high())
            return
        if stroke_n.is_up() and stroke_b._low() <= stroke_a._low():
            self._dbg_bs0('_cal_bs0point_3rd', '跳过: 笔A、B、C 非闪电走势',
                          b_idx=stroke_b.idx,
                          a_idx=stroke_a.idx,
                          b_low=stroke_b._low(), a_low=stroke_a._low())
            return

        # 笔C结束K线，MACD黄白线 与 柱子值 的关系
        # 向下笔(买点)：DIF和DEA要同时 ≥ 柱子值(黄白线在柱子上方)
        # 向上笔(卖点)：DIF和DEA要同时 ≤ 柱子值(黄白线在柱子下方)
        end_klu = stroke_n.get_end_klu()
        if stroke_n.is_down() and (end_klu.macd.DIF < end_klu.macd.macd or end_klu.macd.DEA < end_klu.macd.macd):
            self._dbg_bs0('_cal_bs0point_3rd', '跳过: 向下笔C，黄白线未同时≥柱子值',
                          c_idx=stroke_n.idx,
                          dif=round(end_klu.macd.DIF, 4),
                          dea=round(end_klu.macd.DEA, 4),
                          macd_bar=round(end_klu.macd.macd, 4))
            return
        if stroke_n.is_up() and (end_klu.macd.DIF > end_klu.macd.macd or end_klu.macd.DEA > end_klu.macd.macd):
            self._dbg_bs0('_cal_bs0point_3rd', '跳过: 向上笔C，黄白线未同时≤柱子值',
                          c_idx=stroke_n.idx,
                          dif=round(end_klu.macd.DIF, 4),
                          dea=round(end_klu.macd.DEA, 4),
                          macd_bar=round(end_klu.macd.macd, 4))
            return

        # 笔C与笔A的MACD峰值(PEAK)背驰
        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        stroke_a = bi_list[pivot_a.begin_bi.idx]
        is_diver, n_metric, nm2_metric = self._is_nearest_same_direction_diver(stroke_n, stroke_a, config)
        divergence_rate = n_metric / (nm2_metric + 1e-7)
        if not is_diver:
            self._dbg_bs0('_cal_bs0point_3rd', '跳过: 最近同向，MACD峰值未背驰',
                          nm2_metric=round(nm2_metric, 2), n_metric=round(n_metric, 2),
                          divergence_rate=round(divergence_rate, 2),
                          threshold=round(config.divergence_rate, 2))
            return

        feature_dict = {
            'divergence_rate': divergence_rate,
            'bsp0_bi_amp': stroke_n.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T0, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)
        self._dbg_bs0('_cal_bs0point_3rd', 'OK 生成0类买/卖点',
                      is_buy=is_buy, divergence_rate=round(divergence_rate, 2))

    # ── 第4笔 ──
    def _cal_bs0point_4th(self, bi_list, pivot_a, stroke_n):
        nth_in_pivot = stroke_n.idx - pivot_a.begin_bi.idx + 1
        self._dbg_bs0('_cal_bs0point_4th', '进入', stroke_n_idx=stroke_n.idx,
                      nth_in_pivot=nth_in_pivot,
                      stroke_dir='up' if stroke_n.is_up() else 'down')

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        entry_bi = bi_list[pivot_a.begin_bi.idx - 1]

        # 离开笔4有效突破(中枢A)
        if not self._is_valid_out_bi(stroke_n, pivot_a):
            base_range, ratio, _ = self._compute_base_range(pivot_a)
            r = self.BS0_ZS_BREAK_RATIO
            if stroke_n.is_up():
                threshold = pivot_a.low + base_range * r
                actual = stroke_n._high()
                cond = f'need high(={actual:.2f}) >= zs_low + base_range*{r}(={threshold:.2f})'
            else:
                threshold = pivot_a.high - base_range * r
                actual = stroke_n._low()
                cond = f'need low(={actual:.2f}) <= zs_high - base_range*{r}(={threshold:.2f})'
            self._dbg_bs0('_cal_bs0point_4th', '跳过: 离开笔未有效突破中枢A',
                          stroke_high=stroke_n._high(), stroke_low=stroke_n._low(),
                          zs_high=pivot_a.high, zs_low=pivot_a.low,
                          zs_range=round(pivot_a.high - pivot_a.low, 2),
                          peak_range=round(pivot_a.peak_high - pivot_a.peak_low, 2),
                          ratio=round(ratio, 2),
                          base_range=round(base_range, 2),
                          threshold=round(threshold, 2), actual=actual,
                          condition=cond)
            return

        # 离开笔振幅不足(相比进入笔)，直接跳过
        if not self._is_valid_out_in_amp(stroke_n, entry_bi):
            self._dbg_bs0('_cal_bs0point_4th', '跳过: 离开笔振幅不足',
                          stroke_n_amp=round(stroke_n.amp(), 2),
                          entry_bi_amp=round(entry_bi.amp(), 2),
                          threshold=round(entry_bi.amp() * CMyBSPointList.BS0_OUT_IN_RATIO, 2))
            return

        '''
        # 中枢A，离开笔和进入笔，MACD DIF背驰
        in_dif = entry_bi.cal_macd_metric(MACD_ALGO.DIF, is_reverse=False) # is_reverse 仅对 MACD_ALGO.AREA 有意义
        out_dif = stroke_n.cal_macd_metric(MACD_ALGO.DIF, is_reverse=True)
        if out_dif >= in_dif:
            self._dbg_bs0('_cal_bs0point_4th', '跳过: MACD DIF未背驰',
                          in_dif=round(in_dif, 2), out_dif=round(out_dif, 2))
            return
        '''

        # 中枢A，离开笔和进入笔，MACD面积背驰
        in_metric = entry_bi.cal_macd_metric(config.macd_algo, is_reverse=False) # is_reverse 仅对 MACD_ALGO.AREA 有意义
        out_metric = stroke_n.cal_macd_metric(config.macd_algo, is_reverse=True)
        divergence_rate = out_metric / (in_metric + 1e-7)
        is_diver = out_metric < config.divergence_rate * in_metric
        if not is_diver:
            self._dbg_bs0('_cal_bs0point_4th', '跳过: MACD面积未背驰',
                          in_metric=round(in_metric, 2), out_metric=round(out_metric, 2),
                          divergence_rate=round(divergence_rate, 2),
                          threshold=round(config.divergence_rate, 2))
            return

        # ── 生成0类买卖点 ──
        feature_dict = {
            'divergence_rate': divergence_rate,
            'bsp0_bi_amp': stroke_n.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T0, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)
        self._dbg_bs0('_cal_bs0point_4th', 'OK 生成0类买/卖点',
                      is_buy=is_buy, divergence_rate=round(divergence_rate, 2))

    # ── 第n笔（n≥5）──
    def _cal_bs0point_nth(self, bi_list, pivot_a, stroke_n):
        nth_in_pivot = stroke_n.idx - pivot_a.begin_bi.idx + 1
        self._dbg_bs0('_cal_bs0point_nth', '进入', stroke_n_idx=stroke_n.idx,
                      nth_in_pivot=nth_in_pivot,
                      stroke_dir='up' if stroke_n.is_up() else 'down')

        bsp_found = self._cal_bs0point_nth_nzs(bi_list, pivot_a, stroke_n)
        if not bsp_found:
            if nth_in_pivot == 6 or nth_in_pivot == 8:
                self._dbg_bs0('_cal_bs0point_nth', '主分析未找到, 走第2次分析',
                              nth_in_pivot=nth_in_pivot)
                self._cal_bs0point_nth_ozs(bi_list, pivot_a, stroke_n)
            else:
                self._dbg_bs0('_cal_bs0point_nth', '跳过: 主分析未找到，且n不是6或8',
                              nth_in_pivot=nth_in_pivot)

    # ── 第n笔主分析逻辑（返回是否找到买卖点）──
    def _cal_bs0point_nth_nzs(self, bi_list, pivot_a, stroke_n):
        # n-3, n-2, n-1 重叠 -> 中枢B
        n_idx = stroke_n.idx
        s_nm1 = bi_list[n_idx - 1]  # 笔n-1
        s_nm2 = bi_list[n_idx - 2]  # 笔n-2
        s_nm3 = bi_list[n_idx - 3]  # 笔n-3
        s_nm4 = bi_list[n_idx - 4]  # 笔n-4(中枢进入笔)

        self._dbg_bs0('_cal_bs0point_nth_nzs', '进入', n_idx=n_idx,
                      nm1_high=s_nm1._high(), nm1_low=s_nm1._low(),
                      nm2_high=s_nm2._high(), nm2_low=s_nm2._low(),
                      nm3_high=s_nm3._high(), nm3_low=s_nm3._low(),
                      nm4_high=s_nm4._high(), nm4_low=s_nm4._low(),
                      nm4_dir='up' if s_nm4.is_up() else 'down')

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        entry_bi = s_nm4

        # 中枢B有效(三笔重叠 + 进入笔有效)
        is_valid, zs_b = self._is_valid_zs(s_nm4, s_nm3, s_nm2, s_nm1)
        if not is_valid:
            self._dbg_bs0('_cal_bs0point_nth_nzs', '跳过: 中枢B无效')
            return False
        # self._dbg_bs0('_cal_bs0point_nth_nzs', '中枢B有效', zs_b_high=zs_b.high, zs_b_low=zs_b.low)

        # 离开笔有效突破(中枢B)
        if not self._is_valid_out_bi(stroke_n, zs_b):
            base_range, ratio, _ = self._compute_base_range(zs_b)
            r = self.BS0_ZS_BREAK_RATIO
            if stroke_n.is_up():
                threshold = zs_b.low + base_range * r
                actual = stroke_n._high()
                cond = f'need high(={actual:.2f}) >= zs_low + base_range*{r}(={threshold:.2f})'
            else:
                threshold = zs_b.high - base_range * r
                actual = stroke_n._low()
                cond = f'need low(={actual:.2f}) <= zs_high - base_range*{r}(={threshold:.2f})'
            self._dbg_bs0('_cal_bs0point_nth_nzs', '跳过: 离开笔未有效突破中枢B',
                          stroke_high=stroke_n._high(), stroke_low=stroke_n._low(),
                          zs_b_high=zs_b.high, zs_b_low=zs_b.low,
                          zs_range=round(zs_b.high - zs_b.low, 2),
                          peak_range=round(zs_b.peak_high - zs_b.peak_low, 2),
                          ratio=round(ratio, 2),
                          base_range=round(base_range, 2),
                          threshold=round(threshold, 2), actual=actual,
                          condition=cond)
            return False

        # 离开笔振幅不足(相比进入笔)，直接跳过
        if not self._is_valid_out_in_amp(stroke_n, entry_bi):
            self._dbg_bs0('_cal_bs0point_nth_nzs', '跳过: 离开笔振幅不足',
                          stroke_n_amp=round(stroke_n.amp(), 2),
                          entry_bi_amp=round(entry_bi.amp(), 2),
                          threshold=round(entry_bi.amp() * CMyBSPointList.BS0_OUT_IN_RATIO, 2))
            return False

        '''
        # ⑶ 中枢B，离开笔和进入笔，MACD DIF背驰
        in_dif = entry_bi.cal_macd_metric(MACD_ALGO.DIF, is_reverse=False) # is_reverse 仅对 MACD_ALGO.AREA 有意义
        out_dif = stroke_n.cal_macd_metric(MACD_ALGO.DIF, is_reverse=True)
        if out_dif >= in_dif:
            self._dbg_bs0('_cal_bs0point_nth_nzs', '跳过: MACD DIF未背驰',
                          in_dif=round(in_dif, 2), out_dif=round(out_dif, 2))
            return False
        '''

        # 中枢B，离开笔和进入笔，MACD面积背驰
        in_metric = entry_bi.cal_macd_metric(config.macd_algo, is_reverse=False) # is_reverse 仅对 MACD_ALGO.AREA 有意义
        out_metric = stroke_n.cal_macd_metric(config.macd_algo, is_reverse=True)
        divergence_rate = out_metric / (in_metric + 1e-7)
        is_diver = out_metric < config.divergence_rate * in_metric
        if not is_diver:
            self._dbg_bs0('_cal_bs0point_nth_nzs', '跳过: MACD面积未背驰',
                          in_metric=round(in_metric, 2), out_metric=round(out_metric, 2),
                          divergence_rate=round(divergence_rate, 2),
                          threshold=round(config.divergence_rate, 2))
            return False

        # ── 生成0类买卖点 ──
        feature_dict = {
            'divergence_rate': divergence_rate,
            'bsp0_bi_amp': stroke_n.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T0, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)
        self._dbg_bs0('_cal_bs0point_nth_nzs', 'OK 生成0类买/卖点',
                      is_buy=is_buy, divergence_rate=round(divergence_rate, 2))
        return True

    # ── 第n笔再次分析：MACD全面积比较（中枢A进入段 vs 笔n）──
    def _cal_bs0point_nth_ozs(self, bi_list, pivot_a, stroke_n):
        """
        分析逻辑：当n=6或8时，主分析未找到买卖点，改用中枢A进入段与笔n做MACD全面积比较
        进入段为 pivot_a.begin_bi 的前一笔，离开段为 stroke_n
        """

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        entry_bi = bi_list[pivot_a.begin_bi.idx - 1]

        self._dbg_bs0('_cal_bs0point_nth_ozs', '进入', stroke_n_idx=stroke_n.idx,
                      entry_bi_idx=entry_bi.idx,
                      stroke_dir='up' if stroke_n.is_up() else 'down')

        # 离开笔6/8有效突破(中枢A)
        if not self._is_valid_out_bi(stroke_n, pivot_a):
            base_range, ratio, _ = self._compute_base_range(pivot_a)
            r = self.BS0_ZS_BREAK_RATIO
            if stroke_n.is_up():
                threshold = pivot_a.low + base_range * r
                actual = stroke_n._high()
                cond = f'need high(={actual:.2f}) >= zs_low + base_range*{r}(={threshold:.2f})'
            else:
                threshold = pivot_a.high - base_range * r
                actual = stroke_n._low()
                cond = f'need low(={actual:.2f}) <= zs_high - base_range*{r}(={threshold:.2f})'
            self._dbg_bs0('_cal_bs0point_nth_ozs', '跳过: 离开笔未有效突破中枢A',
                          stroke_high=stroke_n._high(), stroke_low=stroke_n._low(),
                          zs_high=pivot_a.high, zs_low=pivot_a.low,
                          zs_range=round(pivot_a.high - pivot_a.low, 2),
                          peak_range=round(pivot_a.peak_high - pivot_a.peak_low, 2),
                          ratio=round(ratio, 2),
                          base_range=round(base_range, 2),
                          threshold=round(threshold, 2), actual=actual,
                          condition=cond)
            return

        # 离开笔振幅不足(相比进入笔)，直接跳过
        if not self._is_valid_out_in_amp(stroke_n, entry_bi):
            self._dbg_bs0('_cal_bs0point_nth_ozs', '跳过: 离开笔振幅不足',
                          stroke_n_amp=round(stroke_n.amp(), 2),
                          entry_bi_amp=round(entry_bi.amp(), 2),
                          threshold=round(entry_bi.amp() * CMyBSPointList.BS0_OUT_IN_RATIO, 2))
            return

        '''
        # 中枢A，离开笔6/8和进入笔，MACD DIF背驰
        in_dif = entry_bi.cal_macd_metric(MACD_ALGO.DIF, is_reverse=False) # is_reverse 仅对 MACD_ALGO.AREA 有意义
        out_dif = stroke_n.cal_macd_metric(MACD_ALGO.DIF, is_reverse=True)
        if out_dif >= in_dif:
            self._dbg_bs0('_cal_bs0point_nth_ozs', '跳过: MACD DIF未背驰',
                          in_dif=round(in_dif, 2), out_dif=round(out_dif, 2))
            return
        '''

        # 中枢A，离开笔6/8和进入笔，MACD面积背驰
        in_metric = entry_bi.cal_macd_metric(config.macd_algo, is_reverse=False) # is_reverse 仅对 MACD_ALGO.AREA 有意义
        out_metric = stroke_n.cal_macd_metric(config.macd_algo, is_reverse=True)
        divergence_rate = out_metric / (in_metric + 1e-7)
        is_diver = out_metric < config.divergence_rate * in_metric
        if not is_diver:
            self._dbg_bs0('_cal_bs0point_nth_ozs', '跳过: MACD面积未背驰',
                          in_metric=round(in_metric, 2), out_metric=round(out_metric, 2),
                          divergence_rate=round(divergence_rate, 2),
                          threshold=round(config.divergence_rate, 2))
            return

        # ── 生成0类买卖点 ──
        feature_dict = {
            'divergence_rate': divergence_rate,
            'bsp0_bi_amp': stroke_n.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T0, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)
        self._dbg_bs0('_cal_bs0point_nth_ozs', 'OK 生成0类买/卖点',
                      is_buy=is_buy, divergence_rate=round(divergence_rate, 2))

    # ═══════════════════════════════════════════════════════════
    # ── 1类买卖点 ──
    # ═══════════════════════════════════════════════════════════
    @staticmethod
    def _check_bs1_overlap(bi_list, stroke_n, pivot_a):
        """
        检查1类买卖点重叠模式，返回匹配的场景名或None
        A: N,N-1不重叠, N-2重叠
        B: N,N-1,N-2,N-3不重叠, N-4重叠
        C: N,N-1,N-2,N-3,N-4,N-5不重叠, N-6重叠
        """

        patterns = [
            ('A', 2),
            ('B', 4),
            ('C', 6),
        ]
        for name, n_before in patterns:
            if stroke_n.idx < n_before:
                continue
            # 前 n_before 笔(N ~ N-(n_before-1))都不能与中枢重叠
            ok = True
            for i in range(n_before):
                s = bi_list[stroke_n.idx - i]
                if has_overlap(s._low(), s._high(), pivot_a.low, pivot_a.high):
                    ok = False
                    break
            if not ok:
                continue
            # 第 n_before 笔(N-n_before)必须与中枢重叠
            s_last = bi_list[stroke_n.idx - n_before]
            if has_overlap(s_last._low(), s_last._high(), pivot_a.low, pivot_a.high):
                return name
        return None

    # ═══════════════════════════════════════════════════════════
    # ── 1类买卖点 ──
    # ═══════════════════════════════════════════════════════════
    # ── 模式→c段起点偏移量映射 ──
    # A: N,N-1不重叠,                 N-2重叠 → c段起点 = N-2, 偏移2
    # B: N,N-1,N-2,N-3不重叠,         N-4重叠 → c段起点 = N-4, 偏移4
    # C: N,N-1,N-2,N-3,N-4,N-5不重叠, N-6重叠 → c段起点 = N-6, 偏移6
    _BS1_C_OFFSET = {'A': 2, 'B': 4, 'C': 6}

    def cal_bs1point(self, bi_list: LINE_LIST_TYPE, zs_list=None, pivot_a=None, stroke_n=None):
        self._dbg_bs1('cal_bs1point', '进入......', bi_idx=len(bi_list)-1)

        is_buy = stroke_n.is_down()
        config = self.config.GetBSConfig(is_buy)
        entry_bi = bi_list[pivot_a.begin_bi.idx - 1]

        # 重叠条件: A/B/C 三种模式任一满足即可
        # A: N,N-1不重叠,                 N-2重叠(三买卖点后，走一笔)
        # B: N,N-1,N-2,N-3不重叠,         N-4重叠(三买卖点后，走ABC)
        # C: N,N-1,N-2,N-3,N-4,N-5不重叠, N-6重叠(三买卖点后，走ABCDE)
        matched_pattern = self._check_bs1_overlap(bi_list, stroke_n, pivot_a)
        if matched_pattern is None:
            self._dbg_bs1('cal_bs1point', '跳过: 重叠条件不满足(A/B/C)',
                          stroke_n_idx=stroke_n.idx)
            return
        self._dbg_bs1('cal_bs1point', f'重叠条件匹配: {matched_pattern}',
                      stroke_n_idx=stroke_n.idx)

        # 笔N的极值，必须突破笔N-2的极值
        # 向下笔(买点)：笔N低点 < 笔N-2低点(创新低)
        # 向上笔(卖点)：笔N高点 > 笔N-2高点(创新高)
        stroke_nm2 = bi_list[stroke_n.idx - 2]
        if stroke_n.is_down() and stroke_n._low() >= stroke_nm2._low():
            self._dbg_bs1('cal_bs1point', '跳过: 向下笔未创新低',
                          n_idx=stroke_n.idx,
                          n_2_idx=stroke_nm2.idx,
                          n_low=stroke_n._low(), nm2_low=stroke_nm2._low())
            return
        if stroke_n.is_up() and stroke_n._high() <= stroke_nm2._high():
            self._dbg_bs1('cal_bs1point', '跳过: 向上笔未创新高',
                          n_idx=stroke_n.idx,
                          n_2_idx=stroke_nm2.idx,
                          n_high=stroke_n._high(), nm2_high=stroke_nm2._high())
            return

        # 笔N的极值，必须突破中枢A的波动区间
        # 向下笔(买点)：笔N低点 < 中枢A波动区间最低点(peak_low)
        # 向上笔(卖点)：笔N高点 > 中枢A波动区间最高点(peak_high)
        if stroke_n.is_down() and stroke_n._low() >= pivot_a.peak_low:
            self._dbg_bs1('cal_bs1point', '跳过: 向下笔未跌破中枢波动区间最低点',
                          n_idx=stroke_n.idx,
                          n_low=stroke_n._low(), peak_low=pivot_a.peak_low)
            return
        if stroke_n.is_up() and stroke_n._high() <= pivot_a.peak_high:
            self._dbg_bs1('cal_bs1point', '跳过: 向上笔未突破中枢波动区间最高点',
                          n_idx=stroke_n.idx,
                          n_high=stroke_n._high(), peak_high=pivot_a.peak_high)
            return

        # 笔N与 笔N-2的MACD峰值(PEAK)背驰
        is_diver, n_metric, nm2_metric = self._is_nearest_same_direction_diver(stroke_n, stroke_nm2, config)
        divergence_rate = n_metric / (nm2_metric + 1e-7)
        if not is_diver:
            self._dbg_bs1('cal_bs1point', '跳过: 最近同向，MACD峰值未背驰',
                          c1_idx=stroke_nm2.idx,
                          c2_idx=stroke_n.idx,
                          nm2_metric=nm2_metric, n_metric=n_metric,
                          divergence_rate=divergence_rate,
                          threshold=config.divergence_rate)
            return

        # 进入段与离开段，MACD面积背驰判断
        # 由 ENABLE_ENTRY_EXIT_AREA_DIVERGENCE 开关控制：
        #   False（实验方案）：无论正向/反向，始终跳过面积背驰比较
        #   True （原始逻辑）：正向时比较，反向时跳过
        if self.ENABLE_ENTRY_EXIT_AREA_DIVERGENCE:
            is_opposite_dir = (stroke_n.is_down() and entry_bi.is_up()) or (stroke_n.is_up() and entry_bi.is_down())
            if not is_opposite_dir:
                # 本级别：c段全体(c₁+c₂+...) vs 中枢进入段，MACD面积比较
                # c段 = 最后重叠笔的顶分型 → 笔N底分型，包含所有同向笔
                c_offset = self._BS1_C_OFFSET[matched_pattern]
                c_metric = 0.0
                c_bi_idxs = []  # c段同向笔的索引（c₁+c₂+...）
                for i in range(c_offset, -1, -1):  # N-c_offset 到 N（含）
                    s = bi_list[stroke_n.idx - i]
                    if (is_buy and s.is_down()) or (not is_buy and s.is_up()):
                        c_metric += s.cal_macd_metric(config.macd_algo, is_reverse=True)
                        c_bi_idxs.append(s.idx)

                entry_metric = entry_bi.cal_macd_metric(config.macd_algo, is_reverse=False)
                c_divergence_rate = c_metric / (entry_metric + 1e-7)
                is_c_diver = c_metric < config.divergence_rate * entry_metric
                if not is_c_diver:
                    self._dbg_bs1('cal_bs1point', '跳过: c段全体与进入段，MACD面积未背驰',
                                  stroke_n_idx=stroke_n.idx,
                                  pattern=matched_pattern,
                                  c_bi_idxs=c_bi_idxs,
                                  c_metric=round(c_metric, 2),
                                  entry_bi_idx=entry_bi.idx,
                                  entry_metric=round(entry_metric, 2),
                                  c_divergence_rate=round(c_divergence_rate, 2),
                                  threshold=round(config.divergence_rate, 2))
                    return
            else:
                self._dbg_bs1('cal_bs1point', '进入段与离开段反向，跳过中枢两端MACD面积背驰判断',
                              entry_bi_idx=entry_bi.idx,
                              c2_idx=stroke_n.idx)
                # 方向相反时，给后续调试输出和feature_dict设置默认值
                c_bi_idxs = []
                c_divergence_rate = 0.0
        else:
            self._dbg_bs1('cal_bs1point', '实验方案: 关闭进入段与离开段MACD面积背驰判断',
                          entry_bi_idx=entry_bi.idx,
                          c2_idx=stroke_n.idx)
            c_bi_idxs = []
            c_divergence_rate = 0.0

        # ── 生成1类买卖点 ──
        self._dbg_bs1('cal_bs1point', 'OK 生成1类买/卖点',
                      stroke_n_idx=stroke_n.idx, is_buy=is_buy,
                      divergence_rate=divergence_rate,
                      c_divergence_rate=c_divergence_rate,
                      pattern=matched_pattern,
                      c_bi_idxs=c_bi_idxs,
                      entry_bi_idx=entry_bi.idx)
        feature_dict = {
            'bsp1_bi_amp': stroke_n.amp(),
            'divergence_rate': divergence_rate,
            'c_divergence_rate': c_divergence_rate,
            'pattern': matched_pattern,
        }
        self.add_bs(bs_type=BSP_TYPE.T1, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    # ═══════════════════════════════════════════════════════════
    # ── 2类买卖点 ──
    # ═══════════════════════════════════════════════════════════
    def cal_bs2point(self, bi_list: LINE_LIST_TYPE, zs_list=None, pivot_a=None, stroke_n=None):
        self._dbg_bs2('cal_bs2point', '进入......', bi_idx=len(bi_list)-1)

        # 重叠条件: 仅 B/C 两种模式
        # B: N,N-1,N-2,N-3不重叠,         N-4重叠
        # C: N,N-1,N-2,N-3,N-4,N-5不重叠, N-6重叠
        matched_pattern = self._check_bs1_overlap(bi_list, stroke_n, pivot_a)
        if matched_pattern not in ('B', 'C'):
            self._dbg_bs2('cal_bs2point', '跳过: 重叠条件不满足(B/C均不匹配)',
                          stroke_n_idx=stroke_n.idx,
                          matched_pattern=matched_pattern)
            return
        self._dbg_bs2('cal_bs2point', f'重叠模式匹配: {matched_pattern}',
                      stroke_n_idx=stroke_n.idx)

        # 笔N不创新低/不创新高（与1类相反，2类核心特征）
        # 向下笔(二买)：笔N低点 >= 笔N-2低点
        # 向上笔(二卖)：笔N高点 <= 笔N-2高点
        stroke_nm2 = bi_list[stroke_n.idx - 2]
        if stroke_n.is_down() and stroke_n._low() < stroke_nm2._low():
            self._dbg_bs2('cal_bs2point', '跳过: 向下笔创新低，不符合2买',
                          n_idx=stroke_n.idx,
                          n_2_idx=stroke_nm2.idx,
                          n_low=stroke_n._low(), nm2_low=stroke_nm2._low())
            return
        if stroke_n.is_up() and stroke_n._high() > stroke_nm2._high():
            self._dbg_bs2('cal_bs2point', '跳过: 向上笔创新高，不符合2卖',
                          n_idx=stroke_n.idx,
                          n_2_idx=stroke_nm2.idx,
                          n_high=stroke_n._high(), nm2_high=stroke_nm2._high())
            return

        # 笔N-2上需有买/卖点(一买确认后才有二买)
        if not self._has_bsp_for_bi(stroke_nm2.idx):
            self._dbg_bs2('cal_bs2point', '跳过: 笔N-2上没有买/卖点',
                          n_2_idx=stroke_nm2.idx)
            return

        # ── 生成2类买卖点 ──
        is_buy = stroke_n.is_down()
        self._dbg_bs2('cal_bs2point', 'OK 生成2类买/卖点',
                      stroke_n_idx=stroke_n.idx, is_buy=is_buy,
                      pattern=matched_pattern)
        feature_dict = {
            'bsp2_bi_amp': stroke_n.amp(),
            'pattern': matched_pattern,
        }
        self.add_bs(bs_type=BSP_TYPE.T2, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    # ═══════════════════════════════════════════════════════════
    # ── 3类买卖点 ──
    # ═══════════════════════════════════════════════════════════
    def cal_bs3point(self, bi_list: LINE_LIST_TYPE, zs_list=None, pivot_a=None, stroke_n=None):
        self._dbg_bs3('cal_bs3point', '进入......', bi_idx=len(bi_list)-1)

        # 笔N不跟最后一个中枢重叠，但笔N-1重叠
        stroke_nm1 = bi_list[stroke_n.idx - 1]  # 前一笔N-1
        n_overlap = has_overlap(stroke_n._low(), stroke_n._high(), pivot_a.low, pivot_a.high)
        nm1_overlap = has_overlap(stroke_nm1._low(), stroke_nm1._high(), pivot_a.low, pivot_a.high)
        if n_overlap or not nm1_overlap:
            self._dbg_bs3('cal_bs3point', '跳过: 重叠条件不满足',
                          stroke_n_idx=stroke_n.idx,
                          n_overlap=n_overlap,
                          n_1_idx=stroke_nm1.idx,
                          n_1_overlap=nm1_overlap,
                          n_high=stroke_n._high(), n_low=stroke_n._low(),
                          n_1_high=stroke_nm1._high(), n_1_low=stroke_nm1._low(),
                          zs_high=pivot_a.high, zs_low=pivot_a.low)
            return

        # ── 生成3类买卖点 ──
        is_buy = stroke_n.is_down()
        self._dbg_bs3('cal_bs3point', 'OK 生成3类买/卖点',
                      stroke_n_idx=stroke_n.idx, is_buy=is_buy)
        feature_dict = {
            'bsp3_bi_amp': stroke_n.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T3, bi=stroke_n, relate_bsp11=None,
                    is_target_bsp=True, feature_dict=feature_dict)

    @staticmethod
    def _is_virtual_bi(bi_list):
        """判断当下笔是否为虚笔。"""
        if bi_list is None or len(bi_list) == 0:
            return False
        return not getattr(bi_list[-1], 'is_sure', True)

    @staticmethod
    def _is_strong_fx(bi):
        """
        判断当下笔的结束分型是否为强势分型
        参考缠论原文：
          - 第82课《分型结构的心理因素》：第三根K线强弱判断
          - 第79课《分型的辅助操作》：5周期均线辅助过滤
        简化说明：
          - 忽略第二根K线形态（长上/下影、长阴/阳）的判断，降低参数复杂度
          - 第三根K线"区间"指整根K线的最高~最低（high~low），非实体部分
        返回:
            int: 0=弱分型（中继概率大）, 1=强势分型, 2=最强分型
        """

        # 虚笔无右肩，直接返回弱分型
        if not getattr(bi, 'is_sure', True):
            return 0

        # 1. 提取构成分型的三根K线
        # k2 = bi.end_klc（分型中间一根，笔的结束点）
        k2_klc = bi.end_klc
        if k2_klc is None:
            raise ValueError(f"_is_strong_fx: k2_klc (bi.end_klc) 为 None, bi.idx={getattr(bi, 'idx', 'unknown')}")
        # k1 = 左肩（笔内倒数第二根合并K线）
        k1_klc = getattr(k2_klc, 'pre', None)
        if k1_klc is None:
            raise ValueError(f"_is_strong_fx: k1_klc (k2.pre) 为 None, bi.idx={getattr(bi, 'idx', 'unknown')}")
        # k3 = 右肩（笔外紧接的下一根合并K线，不在 bi.klc_lst 中）
        k3_klc = getattr(k2_klc, 'next', None)
        if k3_klc is None:
            raise ValueError(f"_is_strong_fx: k3_klc (k2.next) 为 None, bi.idx={getattr(bi, 'idx', 'unknown')}")

        # 从 klc 提取 OHLC
        # CKLine 合并K线有 .high / .low 属性，.open / .close 需从 .lst 取
        def _ohlc(klc):
            lst = klc.lst
            o = lst[0].open
            c = lst[-1].close
            return o, klc.high, klc.low, c

        k1_o, k1_h, k1_l, k1_c = _ohlc(k1_klc)
        k2_o, k2_h, k2_l, k2_c = _ohlc(k2_klc)
        k3_o, k3_h, k3_l, k3_c = _ohlc(k3_klc)

        # 2. 计算5周期均线（原始K线级别）
        # MA5 = 最近5根原始K线（klu）收盘价的均值，以 k3 为最新
        # k3 是合并K线（klc），可能包含多根原始K线；需从 k3 的 .lst
        # 向前回溯，通过 .pre 链跨 klc 获取，直到凑够5根原始K线
        klu_close_5 = []
        cur = k3_klc
        while len(klu_close_5) < 5:
            # 从当前合并K线的原始K线列表中，从后往前取（最新优先）
            for klu in reversed(cur.lst):
                if len(klu_close_5) >= 5:
                    break
                klu_close_5.append(klu.close)
            if len(klu_close_5) >= 5:
                break
            cur = getattr(cur, 'pre', None)
            if cur is None:
                return 0
        ma5 = sum(klu_close_5) / 5.0

        # 3. 分型方向：向下笔 → 结束于底分型，向上笔 → 结束于顶分型
        is_bottom = bi.is_down()

        # 4. 计算区间中点（区间 = high ~ low，非实体）
        k2_mid = (k2_h + k2_l) / 2.0
        k1_mid = (k1_h + k1_l) / 2.0

        if is_bottom:
            # ── 底分型 ──
            # 第82课镜像：第三根以阳线收在第二根区间一半之上
            cond1 = k3_c > k3_o and k3_c > k2_mid
            # 第79课：收盘价站上5周期均线
            cond2 = k3_c > ma5

            if not (cond1 and cond2):
                return 0  # 弱分型

            # 第82课镜像：第三根最高价突破第一根最高价，且收盘收在第一根区间一半之上，且收盘价站上MA5
            if k3_h > k1_h and k3_c > k1_mid and cond2:
                return 2  # 最强分型
            return 1      # 强势分型

        else:
            # ── 顶分型 ──
            # 第82课原文：第三根不能以阳线收在第二根区间一半之上
            cond1 = k3_c < k3_o and k3_c < k2_mid
            # 第79课：收盘价跌破5周期均线
            cond2 = k3_c < ma5

            if not (cond1 and cond2):
                return 0  # 弱分型

            # 第82课原文：第三根跌破第一根低点，且不能高收到第一根区间一半之上，且收盘价跌破MA5
            if k3_l < k1_l and k3_c < k1_mid and cond2:
                return 2  # 最强分型
            return 1      # 强势分型

    @staticmethod
    def _is_valid_zs(entry_bi, bi3, bi2, bi1):
        """
        有效中枢需要同时满足：
        1. 三笔（bi1, bi2, bi3）有重叠区间，形成中枢
        2. 进入笔有效：不被下一反向笔吃掉
           - 向上进入笔：起始端低点 < 下一反向笔末端低点
           - 向下进入笔：起始端高点 > 下一反向笔末端高点

        Args:
            entry_bi: 进入笔
            bi3, bi2, bi1: 构成中枢的三笔（按顺序）

        Returns:
            (True, zs_obj): 有效中枢，zs_obj 有 .high 和 .low 属性
            (False, None): 无效中枢
        """
        zs_low = max(bi1._low(), bi2._low(), bi3._low())
        zs_high = min(bi1._high(), bi2._high(), bi3._high())
        if zs_low > zs_high:
            return False, None

        # 进入笔振幅需 >= bi3振幅 × ZS_ENTRY_AMP_RATIO
        if entry_bi.amp() < bi3.amp() * CMyBSPointList.ZS_ENTRY_AMP_RATIO:
            return False, None

        # peak_high/peak_low：中枢内所有笔（bi1/bi2/bi3）的极值，即波动区间
        zs_peak_high = max(bi1._high(), bi2._high(), bi3._high())
        zs_peak_low = min(bi1._low(), bi2._low(), bi3._low())
        zs = type('_ZS', (), {
            'high': zs_high, 'low': zs_low,
            'peak_high': zs_peak_high, 'peak_low': zs_peak_low,
        })()
        return True, zs

    @staticmethod
    def _is_valid_out_bi(stroke_n, zs):
        """检查离开笔是否有效突破中枢

        向上笔高点 >= 中枢下沿 + 基准区间 × BS0_ZS_BREAK_RATIO
        向下笔低点 <= 中枢上沿 - 基准区间 × BS0_ZS_BREAK_RATIO

        基准区间由 _compute_base_range 计算（smoothstep 平滑过渡）。
        """
        base_range, _, _ = CMyBSPointList._compute_base_range(zs)
        if base_range < 0:
            return False

        r = CMyBSPointList.BS0_ZS_BREAK_RATIO
        if stroke_n.is_up():
            return stroke_n._high() >= zs.low + base_range * r
        else:
            return stroke_n._low() <= zs.high - base_range * r

    @staticmethod
    def _is_valid_out_in_amp(stroke_n, entry_bi):
        """
        检查离开笔振幅是否达到进入笔振幅的比例阈值
        离开笔振幅 >= 进入笔振幅 × BS0_OUT_IN_RATIO
        """
        return stroke_n.amp() >= entry_bi.amp() * CMyBSPointList.BS0_OUT_IN_RATIO

    @staticmethod
    def _compute_base_range(zs):
        """计算有效突破判断的基准区间（smoothstep 平滑过渡）

        - zs_range = zs.high - zs.low（中枢重叠区间高度）
        - peak_range = zs.peak_high - zs.peak_low（中枢波动区间高度）
        - 当 peak_range / zs_range 在 [1.0, 2.0] 之间时，用 smoothstep
          在 zs_range 和 peak_range 之间平滑过渡。
        - ratio <= 1.0 → 返回 zs_range
        - ratio >= 2.0 → 返回 peak_range

        Returns:
            (base_range, ratio, used_zs_range): 基准区间、波动比例、是否仅用重叠区间
        """
        zs_range = zs.high - zs.low
        if zs_range <= 0:
            return zs_range, 0.0, True

        peak_range = zs.peak_high - zs.peak_low
        ratio = peak_range / zs_range
        if ratio <= 1.0:
            return zs_range, ratio, True
        elif ratio >= 2.0:
            return peak_range, ratio, False
        else:
            # smoothstep: 3t² - 2t³, 在两端一阶导数为0，过渡自然
            t = ratio - 1.0          # 将 [1.0, 2.0] 映射到 [0, 1]
            weight = t * t * (3 - 2 * t)
            return zs_range + (peak_range - zs_range) * weight, ratio, False

    '''
    @staticmethod
    def _is_macd_diver(stroke_n):
        """
        MACD模拟背驰判断
          向上笔(卖点)：右肩 macd < 当前这笔 macd 峰值(红柱最大值)
          向下笔(买点)：右肩 macd > 当前这笔 macd 峰值(绿柱最小值)
        """
        # 计算当前笔MACD柱子峰值
        # 向上笔取最大红柱，向下笔取最小绿柱(最负)
        peak_macd = 1e-7
        for klc in stroke_n.klc_lst:
            for klu in klc.lst:
                if stroke_n.is_up():
                    # peak_macd 要么是某个正值，要么是 1e-7（笔内全是负柱的极端情况）
                    if klu.macd.macd > peak_macd:
                        peak_macd = klu.macd.macd
                else:
                    # peak_macd 要么是某个负值，要么是 1e-7（笔内全是正柱的极端情况）
                    if klu.macd.macd < peak_macd:
                        peak_macd = klu.macd.macd

        end_klc = stroke_n.end_klc
        right_klc = getattr(end_klc, 'next', None)
        if right_klc is None:
            return False
        right_macd = right_klc.lst[-1].macd.macd
        if stroke_n.is_up():
            return right_macd < peak_macd
        else:
            return right_macd > peak_macd
    '''

    @staticmethod
    def _is_nearest_same_direction_diver(n, nm2, config):
        """
        最近同向笔MACD背驰比较
        比较当下笔N与其最近同向笔N-2的MACD峰值(PEAK)，判断当下笔力度是否不足
        """
        n_metric = n.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=False) # is_reverse 仅对 MACD_ALGO.AREA 有意义
        nm2_metric = nm2.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=True)
        is_diver = n_metric < config.divergence_rate * nm2_metric
        return is_diver, n_metric, nm2_metric

    '''
    @staticmethod
    def _is_return_zero_axis(bi_list, pivot_a, stroke_n, dbg_func=None):
        """判断中枢内C笔相对于A笔的MACD黄白线(DIF)是否回0轴。
        - C笔末端DIF < 0：直接视为回0轴
        - C笔末端DIF >= 0：使用衰减率判断，dif / dif_peak < 0.1 视为回0轴
        """
        stroke_a = bi_list[pivot_a.begin_bi.idx]
        stroke_c = stroke_n
        def _get_dif_peak(stroke):
            """取笔内所有K线DIF绝对值的峰值"""
            peak = 1e-7
            for klc in stroke.klc_lst:
                for klu in klc.lst:
                    if abs(klu.macd.DIF) > peak:
                        peak = abs(klu.macd.DIF)
            return peak

        # C笔DIF在0轴下，认为回0轴了
        dif = stroke_c.get_end_klu().macd.DIF
        if dif < 0:
            if dbg_func:
                dbg_func('_is_return_zero_axis', 'DIF回0轴: DIF在0轴下',
                         dif=round(dif, 2))
            return True

        dif_peak = _get_dif_peak(stroke_a)
        # A笔DIF峰值 < A笔振幅的1%，视为无力度
        if dif_peak < stroke_a.amp() * 0.01:
            if dbg_func:
                dbg_func('_is_return_zero_axis', 'A笔无力度',
                         dif_peak=round(dif_peak, 2), amp=round(stroke_a.amp(), 2))
            return False

        dif_ratio = dif / (dif_peak + 1e-7)
        if dif_ratio >= 0.1:
            if dbg_func:
                dbg_func('_is_return_zero_axis', 'DIF未回0轴: 偏离度不足',
                         dif_peak=round(dif_peak, 2), dif=round(dif, 2),
                         dif_ratio=round(dif_ratio, 2))
            return False

        if dbg_func:
            dbg_func('_is_return_zero_axis', 'DIF回0轴',
                     dif_peak=round(dif_peak, 2), dif=round(dif, 2),
                     dif_ratio=round(dif_ratio, 2))
        return True
    '''

# ═══════════════════════════════════════════════════════════
# 区间套辅助函数
# 主要用于 check_nested_diver 计算背驰，
# 红框功能（App 引擎层）通过 import 复用
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


def _main_bi_range(bi, date_fmt, allow_partial=False):
    """获取主笔两端原始K线时间及KLU对象

    股票和期货统一走此函数，通过 date_fmt 指定格式：
    - date_fmt = "%Y/%m/%d %H:%M:%S"（含秒，长度19）
    - date_fmt = "%Y/%m/%d %H:%M"（含时分，长度16）
    - date_fmt = "%Y/%m/%d"（仅日期，长度10）

    返回: (fx_a_raw_dt, fx_b_raw_dt, a_klu, b_klu) 或 None
      a_klu / b_klu 为左右边界 KLU 对象，供 _stocks_red_range 直接消费，
      避免重复从 bi 提取 KLU。

    边界取值说明（2026-09-02 由「分型肩部外沿」改为「分型自身极值（峰/谷）」）：
      - 新逻辑：左边界 = 起始分型的极值原始K线（向上笔=谷底，向下笔=峰顶），
        即 bi.get_begin_klu()；右边界 = 结束分型的极值原始K线（向上笔=峰顶，
        向下笔=谷底），即 bi.get_end_klu()。红框边界与笔的 sdt/edt 端点一致
        （sdt/edt 即用这两个函数生成）。
      - 旧逻辑（已注释保留）：左边界 = 起始分型左肩分型的第一根原始K线，
        右边界 = 结束分型右肩分型的最后一根原始K线（外沿区间，区间更宽）。

    allow_partial: 是否允许返回单侧结果。默认 False（任一侧为空即整体返回 None，
      保持历史行为）；True 时保留已有单侧结果（期货分析路径依赖此行为）。
    """
    fx_a_raw_dt = ""
    fx_b_raw_dt = ""
    a_klu = None
    b_klu = None

    try:
        # ── 新逻辑（2026-09-02）：取分型自身极值（峰/谷）所在原始K线 ──
        # 左边界 = 起始分型极值K线（向上笔=谷底，向下笔=峰顶）
        a_klu = bi.get_begin_klu()
        if a_klu:
            fx_a_raw_dt = a_klu.time.toFmtStr(date_fmt)
        # 右边界 = 结束分型极值K线（向上笔=峰顶，向下笔=谷底）
        b_klu = bi.get_end_klu()
        if b_klu:
            fx_b_raw_dt = b_klu.time.toFmtStr(date_fmt)

        # ── 旧逻辑（2026-09-02 注释保留，如需恢复取消注释即可）──
        # 左边界 = 起始分型左肩分型的第一根原始K线
        # 右边界 = 结束分型右肩分型的最后一根原始K线
        # begin_klc = bi.begin_klc
        # end_klc = bi.end_klc
        # left_shoulder_klc = begin_klc.pre if begin_klc else None
        # if left_shoulder_klc and left_shoulder_klc.lst:
        #     a_klu = left_shoulder_klc.lst[0]
        #     fx_a_raw_dt = a_klu.time.toFmtStr(date_fmt)
        # right_shoulder_klc = end_klc.next if end_klc else None
        # if right_shoulder_klc and right_shoulder_klc.lst:
        #     b_klu = right_shoulder_klc.lst[-1]
        #     fx_b_raw_dt = b_klu.time.toFmtStr(date_fmt)
    except Exception as e:
        print(f"[警告] 异常: {type(e).__name__}: {e}")

    if allow_partial:
        # 保留单侧结果（期货分析路径依赖：任一侧为空不整体丢弃）
        if not fx_a_raw_dt and not fx_b_raw_dt:
            return None
    else:
        if not fx_a_raw_dt or not fx_b_raw_dt:
            return None
    return fx_a_raw_dt, fx_b_raw_dt, a_klu, b_klu


def _stocks_red_range(a_klu, b_klu, sub_freq, bi=None):
    """股票双窗口：从主级别一笔的左右肩 KLU 中提取子级别边界时间 [C,D]。

    a_klu / b_klu 由 _main_bi_range 返回，避免重复提取。
    多级别CChan联立模式下，KLU 带有 sub_kl_list（真实子级别K线序列），
    直接取左肩第一根 / 右肩最后一根子级别K线的时间。

    实时场景下，最新 K 线（右肩）的 sub_kl_list 可能尚未建立，此时自动
    回退到笔的结束 K 线（end_klc.lst[-1]），该 K 线在上一轮已处理完毕。

    参数:
        a_klu:    左肩 KLU 对象（来自 _main_bi_range）
        b_klu:    右肩 KLU 对象（来自 _main_bi_range）
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


# 主/子级别周期秒数（股票双窗独立化数学换算用；与 AppEngine._STOCKS_MAIN_PERIOD 同源口径）
_STOCKS_FREQ_SEC = {
    "w": 7 * 86400,
    "d": 86400,
    "30m": 1800,
    "5m": 300,
}

# 日期解析格式（与 _futures_red_range 一致：优先 / 格式，兼容 - 格式）
_STOCKS_RANGE_PARSE_FORMATS = [
    "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
    "%Y/%m/%d", "%Y-%m-%d",
]


def _stocks_red_range_algo(fx_a_raw_dt, fx_b_raw_dt, main_freq, sub_freq):
    """（P3）股票红框边界数学换算 —— 独立双窗纯函数（无联立 sub_kl_list）。

    股票 K 线 dt = 结束时间（与期货=开始时间相反），公式与
    _futures_red_range 镜像对称（期货 offset 加在右端，股票减在左端）：
      · 日内主级别（30m）：
          C = A - (main_sec - sub_sec)   左肩主K线覆盖区间内第一根下窗K线
          D = B                          右肩主K线覆盖区间内最后一根下窗K线
          （例：30m 线 A=11:00 + 5m 子 → C=10:35，恰为 10:31~11:00 内首根 5m 线）
      · 日期型主级别（w/d）：主K线 dt 为当日 00:00，覆盖全日/全周，
        无法定位到日内时刻，按「当日边界」取整：
          C = A 当日（w：所在周周一）00:00
          D = B 当日 23:59:59
        按下窗日期格式输出后，字符串比较语义仍正确覆盖当日全部下窗K线。
    参数:
        fx_a_raw_dt / fx_b_raw_dt: 主级别笔左右肩原始分型时间（_main_bi_range 产出）
        main_freq / sub_freq:      主/子级别周期（如 "30m"/"5m"、"d"/"30m"）
    返回:
        (fx_a_sub_dt, fx_b_sub_dt)；解析失败返回 ("", "")。
    """
    main_sec = _STOCKS_FREQ_SEC.get(main_freq)
    sub_sec = _STOCKS_FREQ_SEC.get(sub_freq)
    if not fx_a_raw_dt or not fx_b_raw_dt or main_sec is None or sub_sec is None:
        return "", ""

    out_fmt = _get_date_fmt(sub_freq)

    def _parse(s):
        for fmt in _STOCKS_RANGE_PARSE_FORMATS:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    dt_a = _parse(fx_a_raw_dt)
    dt_b = _parse(fx_b_raw_dt)
    if dt_a is None or dt_b is None:
        return "", ""

    try:
        if main_freq in ("w", "d"):
            # 日期型主K线覆盖全日（w=全周）：左界取覆盖期首日 00:00，
            # 右界取右肩当日 23:59:59（字符串比较覆盖当日全部下窗K线）
            if main_freq == "w":
                c = (dt_a - timedelta(days=dt_a.weekday())).replace(
                    hour=0, minute=0, second=0, microsecond=0)
            else:
                c = dt_a.replace(hour=0, minute=0, second=0, microsecond=0)
            d = dt_b.replace(hour=23, minute=59, second=59, microsecond=0)
        else:
            # 日内主K线：dt=结束时刻，覆盖 (dt-main_sec, dt]
            c = dt_a - timedelta(seconds=(main_sec - sub_sec))
            d = dt_b
        return c.strftime(out_fmt), d.strftime(out_fmt)
    except Exception as e:
        print(f"[stocks_red_range_algo] 异常: {type(e).__name__}: {e}")
        return "", ""


def _futures_red_range(snapshot, main_freq_sec, sub_freq_sec, sub_freq=None):
    """期货双窗口：将主级别笔的原始分型时间换算为子级别 K 线时间。

    天勤 K 线时间 = 开始时间（不同于股票 = 结束时间），因此：
      fx_a_sub_dt = fx_a_raw_dt
        → 上层 K 线开始时间 = 第一根子级别 K 线时间
      fx_b_sub_dt = fx_b_raw_dt + (main_freq_sec - sub_freq_sec)
        → 上层 K 线开始时间 + (主级别周期 - 子周期) = 最后一根子级别 K 线时间
        → 例：30m 线 11:00 + 30m - 5m = 11:25（最后一根 5m 线）

    参数:
        snapshot:         上窗快照（含 bis 列表），直接修改其 fx_a_sub_dt / fx_b_sub_dt
        main_freq_sec:    主级别周期秒数
        sub_freq_sec:     子级别周期秒数
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
        return
    bis = snapshot.get('bis')
    if not bis:
        return

    if main_freq_sec <= sub_freq_sec:
        return

    offset_sec = main_freq_sec - sub_freq_sec
    fail_count = 0
    skip_count = 0

    for bi in bis:
        raw_a = bi.get('fx_a_raw_dt', '') or ''
        raw_b = bi.get('fx_b_raw_dt', '') or ''

        # 左边界：第一根子级别 K 线 = 上层 K 线开始时间
        if raw_a:
            dt_a = None
            for fmt in _PARSE_DATE_FORMATS:
                try:
                    dt_a = datetime.strptime(raw_a, fmt)
                    break
                except ValueError:
                    continue
            bi['fx_a_sub_dt'] = dt_a.strftime(out_fmt) if dt_a else raw_a
        else:
            bi['fx_a_sub_dt'] = ''

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
            bi['fx_b_sub_dt'] = raw_b  # 回退：保留原始值
            continue

        try:
            dt = dt + timedelta(seconds=offset_sec)
            bi['fx_b_sub_dt'] = dt.strftime(out_fmt)
        except OverflowError:
            fail_count += 1
            bi['fx_b_sub_dt'] = raw_b

    


def _red_range_bi_sequence(fx_a_sub_dt, fx_b_sub_dt, sub_bi_list, sub_freq):
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
        return None, None

    sub_date_fmt = _get_date_fmt(sub_freq)
    start_bi_idx = None
    end_bi_idx = None

    for i, bi in enumerate(sub_bi_list):
        begin_klu = bi.get_begin_klu()
        end_klu = bi.get_end_klu()
        if begin_klu is None or end_klu is None:
            continue
        try:
            s_dt = begin_klu.time.toFmtStr(sub_date_fmt)
            e_dt = end_klu.time.toFmtStr(sub_date_fmt)
        except (AttributeError, ValueError, TypeError):
            continue

        if s_dt >= fx_a_sub_dt and e_dt <= fx_b_sub_dt:
            if start_bi_idx is None:
                start_bi_idx = i
            end_bi_idx = i

    return start_bi_idx, end_bi_idx


def _red_range_amp(bis, all_bi_list, date_fmt):
    """
    从红框覆盖的笔列表中构建中枢。

    策略（先校验进入笔，再跑原始chan.py）：
      红框覆盖 bis = [P1, P2, ..., Pn]
      1. 取 P_k(当前首笔) 和 P_{k+1}(下一笔)，判断 P_k 是否满足作为 P_{k+1} 进入笔的 amp 条件
      2. 不满足 → 去掉 P_k，用 P_{k+1} 和 P_{k+2} 判断，以此类推
      3. 找到满足的一对 P_k, P_{k+1} 后，用 [P_k..Pn] 交给原始 chan.py over_seg 跑一遍
      4. 剩余笔不足4笔 → 返回空

    参数:
        bis: 红框完全覆盖的笔列表
        all_bi_list: 完整的笔列表（用于查找进入笔）
        date_fmt: 日期格式字符串

    返回:
        zs_data: 中枢数据列表
    """
    skip = 0
    while len(bis) - skip >= 4:
        pk = bis[skip]           # 当前候选进入笔
        pk1 = bis[skip + 1]      # 下一笔（中枢第一笔候选）

        # amp 校验: pk.amp() >= pk1.amp() * ZS_ENTRY_AMP_RATIO
        pk_amp = pk.amp()
        pk1_amp = pk1.amp()
        threshold = pk1_amp * CMyBSPointList.ZS_ENTRY_AMP_RATIO
        if pk_amp >= threshold:
            sub_bis = bis[skip:]
            return _red_range_try_construct_zs(sub_bis, all_bi_list, date_fmt)
        skip += 1

    return []


def _red_range_try_construct_zs(bis, all_bi_list, date_fmt):
    """
    纯原始 chan.py over_seg 算法（不含任何 amp 校验）。
    严格对照 ZS/ZSList.py 的 cal_bi_zs(over_seg) + update_overseg_zs + add_to_free_lst + try_construct_zs。

    参数:
        bis: 笔列表（已经是排除首笔后的子集）
        all_bi_list: 完整的笔列表（用于查找进入笔信息）
        date_fmt: 日期格式字符串

    返回:
        zs_data: 中枢数据列表
    """
    zs_data = []

    def _in_zs_range(bi, zg, zd):
        return min(zg, bi._high()) >= max(zd, bi._low())

    free_lst = []
    zs_records = []

    for bi in bis:
        # ===== update_overseg_zs（严格对照 chan.py）=====
        if len(zs_records) and len(free_lst) == 0:
            last_zs = zs_records[-1]

            # 关卡1: bi.next在中枢内 + bi在中枢内 + 紧邻 → try_add_to_end
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

            # 关卡2: bi本身在中枢内 + 紧邻 → return
            if (_in_zs_range(bi, last_zs['zg'], last_zs['zd'])
                    and bi.idx - last_zs['end_bi'].idx <= 1):
                continue

            # 脱离中枢
            if not _in_zs_range(bi, last_zs['zg'], last_zs['zd']) and not last_zs.get('confirm_edt'):
                if bi.idx != bis[-1].idx:
                    last_zs['confirm_edt'] = bi.get_end_klu().time.toFmtStr(date_fmt) if bi.get_end_klu() is not None else ""
                    zs_data[-1]['confirm_edt'] = last_zs['confirm_edt']

        # ===== add_to_free_lst =====
        if len(free_lst) != 0 and bi.idx == free_lst[-1].idx:
            free_lst = free_lst[:-1]
        free_lst.append(bi)

        # ===== try_construct_zs(over_seg) =====
        if len(free_lst) < 3:
            continue

        lst = list(free_lst[-3:])

        # --- 进入段跳过（原始chan.py逻辑）---
        skip_entry = False
        if len(zs_records) > 0:
            zs = zs_records[-1]
            lst0_low = lst[0]._low()
            lst0_high = lst[0]._high()
            if lst0_low > zs['zg']:
                if lst[0].is_up():
                    skip_entry = True
            elif lst0_high < zs['zd']:
                if lst[0].is_down():
                    skip_entry = True
            else:
                skip_entry = True  # lst0与中枢重叠
        else:
            first_pen = free_lst[0]
            if len(free_lst) == 3:
                skip_entry = True  # 前3笔，跳过首笔当进入段
            else:
                if lst[0].dir == first_pen.dir:
                    skip_entry = True  # 与first_pen同向

        if skip_entry:
            continue

        # --- 三笔重叠 ---
        b1, b2, b3 = lst[0], lst[1], lst[2]
        if not getattr(b3, 'is_sure', True):
            continue

        min_high = min(b1._high(), b2._high(), b3._high())
        max_low = max(b1._low(), b2._low(), b3._low())
        if min_high < max_low:
            continue

        # --- 找进入笔 ---
        entry_bi = None
        entry_dir = "up"
        for j, bi_ref in enumerate(all_bi_list):
            if bi_ref is b1 and j > 0:
                entry_bi = all_bi_list[j - 1]
                break
        if entry_bi is not None:
            entry_dir = "up" if entry_bi.is_up() else "down"

        # --- 形成中枢 ---
        zg = min_high
        zd = max_low
        gg = max(b1._high(), b2._high(), b3._high())
        dd = min(b1._low(), b2._low(), b3._low())

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

    return zs_data


def _red_range_zs_diver(sub_bi_sliced, main_bi, zs_data):
    """
    有中枢背驰判断：从子级别笔序列末尾往前找同向笔，检查是否有买卖点
    从 sub_bi_sliced 末尾往前找，最多找 2 笔与主级别笔同向的笔
    如果其中任何一笔有对应的买卖点，则认为背驰
    参数:
        sub_bi_sliced: 红框内子级别笔切片列表
        main_bi:      主级别当前笔
        zs_data:      中枢数据列表（仅用于诊断信息）
    """
    main_dir_up = main_bi.is_up()
    same_dir_count = 0
    for bi in reversed(sub_bi_sliced):
        if bi.is_up() == main_dir_up:
            same_dir_count += 1
            bsp = getattr(bi, 'bsp', None)
            if bsp is not None:
                assert bsp.is_buy == (not main_dir_up), f"BUG: bi[{bi.idx}] bsp方向不匹配"
                detail = f"子级别{len(zs_data)}个中枢，同向笔bi[{bi.idx}]有{'买' if bsp.is_buy else '卖'}点"
                return {"diverged": True, "detail": detail}
            if same_dir_count >= 2:
                break
    detail = f"子级别{len(zs_data)}个中枢，同向笔无买/卖点"
    return {"diverged": False, "detail": detail}


def _red_range_single_bi_diver(bi):
    # 单笔背驰判断：与 _is_macd_diver 算法一致，区别：比较对象是末端K线而非右肩
    peak_macd = 1e-7
    for klc in bi.klc_lst:
        for klu in klc.lst:
            if bi.is_up():
                if klu.macd.macd > peak_macd:
                    peak_macd = klu.macd.macd
            else:
                if klu.macd.macd < peak_macd:
                    peak_macd = klu.macd.macd

    end_klu = bi.get_end_klu()
    end_macd = end_klu.macd.macd
    if bi.is_up():
        is_diver = end_macd < peak_macd * CMyBSPointList.NESTED_MACD_DIVER_RATIO
    else:
        is_diver = end_macd > peak_macd * CMyBSPointList.NESTED_MACD_DIVER_RATIO

    detail = f"单笔，{'MACD走弱' if is_diver else '未走弱'}（峰值={peak_macd:.2f}，末端={end_macd:.2f}）"
    return {"diverged": is_diver, "detail": detail}


def _red_range_multi_bi_diver(bi_list):
    """多笔无中枢背驰判断：比较最后两个同向笔的MACD峰值"""
    last_bi = bi_list[-1]
    prev_same_dir = None
    for bi in reversed(bi_list[:-1]):
        if bi.is_up() == last_bi.is_up():
            prev_same_dir = bi
            break
    if prev_same_dir is None:
        return {"diverged": False, "detail": "未找到前一个同向笔"}

    prev_peak = prev_same_dir.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=False) # is_reverse 仅对 MACD_ALGO.AREA 有意义
    curr_peak = last_bi.cal_macd_metric(MACD_ALGO.PEAK, is_reverse=True)
    is_diver = curr_peak <= prev_peak * CMyBSPointList.NESTED_MACD_DIVER_RATIO
    detail = (
        f"多笔无中枢，{'MACD背驰' if is_diver else '未背驰'}"
        f"（前笔峰值={prev_peak:.2f}，后笔峰值={curr_peak:.2f}）"
    )
    return {"diverged": is_diver, "detail": detail}
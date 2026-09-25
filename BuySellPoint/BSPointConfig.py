from typing import Dict, List, Optional

from Common.CEnum import BSP_TYPE, MACD_ALGO
from Common.ChanException import CChanException, ErrCode
from Common.func_util import _parse_inf


class CBSPointConfig:
    def __init__(self, **args):
        self.b_conf = CPointConfig(**args)
        self.s_conf = CPointConfig(**args)

    def GetBSConfig(self, is_buy):
        return self.b_conf if is_buy else self.s_conf


class CPointConfig:
    def __init__(self,
                 divergence_rate,
                 min_zs_cnt,
                 bsp11_only_multibi_zs,
                 max_bs22_rate,
                 macd_algo,
                 bs11_peak,
                 bs_type,
                 bsp22_follow_11,
                 bsp33_follow_11,
                 bsp33_peak,
                 bsp22s_follow_22,
                 max_bsp22s_lv,
                 strict_bsp33,
                 bsp33a_max_zs_cnt,
                 ):
        self.divergence_rate = divergence_rate
        self.min_zs_cnt = min_zs_cnt
        self.bsp11_only_multibi_zs = bsp11_only_multibi_zs
        self.max_bs22_rate = max_bs22_rate
        assert self.max_bs22_rate <= 1
        self.SetMacdAlgo(macd_algo)
        self.bs11_peak = bs11_peak
        self.tmp_target_types = bs_type
        self.target_types: List[BSP_TYPE] = []
        self.bsp22_follow_11 = bsp22_follow_11
        self.bsp33_follow_11 = bsp33_follow_11
        self.bsp33_peak = bsp33_peak
        self.bsp22s_follow_22 = bsp22s_follow_22
        self.max_bsp22s_lv: Optional[int] = max_bsp22s_lv
        self.strict_bsp33 = strict_bsp33
        self.bsp33a_max_zs_cnt = bsp33a_max_zs_cnt
        assert self.bsp33a_max_zs_cnt >= 1

    def parse_target_type(self):
        _d: Dict[str, BSP_TYPE] = {x.value: x for x in BSP_TYPE}
        if isinstance(self.tmp_target_types, str):
            self.tmp_target_types = [t.strip() for t in self.tmp_target_types.split(",")]
        for target_t in self.tmp_target_types:
            assert target_t in ['0', '1', '2', '3', '11', '11p', '22', '22s', '33a', '33b'], \
                f"unsupported bs_type: {target_t}, valid: 0,1,2,3,11,11p,22,22s,33a,33b"
        self.target_types = [_d[_type] for _type in self.tmp_target_types]

    def SetMacdAlgo(self, macd_algo):
        _d = {
            # ===== MACD 面积族（2026-09-25 由 area/full_area/full_area_ext 改名，统一 AREA_* 前缀，函数 Cal_MACD_area_* 对齐）=====
            "area_half": MACD_ALGO.AREA_HALF,        # 原 AREA / Cal_MACD_half：笔首(尾)连续同向柱"半段"面积
            "area_full": MACD_ALGO.AREA_FULL,        # 原 FULL_AREA / Cal_MACD_area：整笔同向柱面积之和
            "area_full_ext": MACD_ALGO.AREA_FULL_EXT, # 原 FULL_AREA_EXT / Cal_MACD_area_ext：area_full + 反向柱峰值修正
            # ===== MACD 指标族 =====
            # 2026-09-25 重命名：MACD_ALGO.PEAK → MACD_ALGO.BAR（与 DIF/DEA 对齐，统一用「指标名」）；
            # 配置关键字唯一为 "bar"，"peak" 历史别名已彻底移除，旧配置须改为 "bar"。
            "bar": MACD_ALGO.BAR,           # 规范名（对齐 DIF/DEA）
            "diff": MACD_ALGO.DIFF,
            "slope": MACD_ALGO.SLOPE,
            "amp": MACD_ALGO.AMP,
            "amount": MACD_ALGO.AMOUNT,
            "volumn": MACD_ALGO.VOLUMN,
            "amount_avg": MACD_ALGO.AMOUNT_AVG,
            "volumn_avg": MACD_ALGO.VOLUMN_AVG,
            "turnrate_avg": MACD_ALGO.TURNRATE_AVG,
            "rsi": MACD_ALGO.RSI,
        }
        if macd_algo not in _d:
            _legacy = {"peak": "bar", "area": "area_half", "full_area": "area_full", "full_area_ext": "area_full_ext"}
            _hint = f'；注意："{macd_algo}" 已于 2026-09-25 更名为 "{_legacy[macd_algo]}"，请更新你的配置' if macd_algo in _legacy else ""
            raise CChanException(
                f"unsupport macd_algo={macd_algo!r}, valid: {sorted(_d)}{_hint}",
                ErrCode.PARA_ERROR)
        self.macd_algo = _d[macd_algo]

    def set(self, k, v):
        v = _parse_inf(v)
        if k == "macd_algo":
            self.SetMacdAlgo(v)
        else:
            exec(f"self.{k} = {v}")

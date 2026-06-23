from typing import List

from Bi.BiConfig import CBiConfig
from BuySellPoint.BSPointConfig import CBSPointConfig
from Common.CEnum import TREND_TYPE
from Common.ChanException import CChanException, ErrCode
from Common.func_util import _parse_inf
from Math.BOLL import BollModel
from Math.Demark import CDemarkEngine
from Math.KDJ import KDJ
from Math.MACD import CMACD
from Math.RSI import RSI
from Math.TrendModel import CTrendModel
from Seg.SegConfig import CSegConfig
from ZS.ZSConfig import CZSConfig


class CChanConfig:
    def __init__(self, conf=None):
        if conf is None:
            conf = {}
        conf = ConfigWithCheck(conf)
        self.bi_conf = CBiConfig(
            bi_algo=conf.get("bi_algo", "normal"),                 # 按缠论笔定义，非顶/底分型即成笔
            is_strict=conf.get("bi_strict", True),                 # bi_algo=normal时有效；顶/底分型间，至少隔1根K线
            bi_fx_check=conf.get("bi_fx_check", "loss"),           # 分型检查：loss模式（宽松）
            gap_as_kl=conf.get("gap_as_kl", False),
            bi_end_is_peak=conf.get('bi_end_is_peak', False),      # 关闭后，允许一笔之间有"不成笔的"更高/低分型；参见 上证指数 日K 25/09/03~25/10/31
            bi_allow_sub_peak=conf.get("bi_allow_sub_peak", True), # 打开后，允许笔端点的分型非局部最高/低分型；参见23/07/21~23/08/07
        )
        self.seg_conf = CSegConfig(
            seg_algo=conf.get("seg_algo", "chan"),                 # 利用特征序列来计算
            left_method=conf.get("left_seg_method", "peak"),
        )
        self.zs_conf = CZSConfig(
            need_combine=conf.get("zs_combine", False),            # 是否中枢合并，默认为 True
            zs_combine_mode=conf.get("zs_combine_mode", "zs"),
            one_bi_zs=conf.get("one_bi_zs", False),
            zs_algo=conf.get("zs_algo", "over_seg"),               # 中枢算法：跨段
        )

        self.trigger_step = conf.get("trigger_step", True)         # 实时/回放语义：逐根推进，启用尾部虚笔和未确认结构处理
        self.skip_step = conf.get("skip_step", 0)

        self.kl_data_check = conf.get("kl_data_check", True)
        self.max_kl_misalgin_cnt = conf.get("max_kl_misalgin_cnt", 2)
        self.max_kl_inconsistent_cnt = conf.get("max_kl_inconsistent_cnt", 5)
        self.auto_skip_illegal_sub_lv = conf.get("auto_skip_illegal_sub_lv", False)
        self.print_warning = conf.get("print_warning", True)
        self.print_err_time = conf.get("print_err_time", True)

        self.mean_metrics: List[int] = conf.get("mean_metrics", [])
        self.trend_metrics: List[int] = conf.get("trend_metrics", [])
        self.macd_config = conf.get("macd", {"fast": 12, "slow": 26, "signal": 9})
        self.cal_demark = conf.get("cal_demark", False)
        self.cal_rsi = conf.get("cal_rsi", False)
        self.cal_kdj = conf.get("cal_kdj", False)
        self.rsi_cycle = conf.get("rsi_cycle", 14)
        self.kdj_cycle = conf.get("kdj_cycle", 9)
        self.demark_config = conf.get("demark", {
            'demark_len': 9,
            'setup_bias': 4,
            'countdown_bias': 2,
            'max_countdown': 13,
            'tiaokong_st': True,
            'setup_cmp2close': True,
            'countdown_cmp2close': True,
        })
        self.boll_n = conf.get("boll_n", 20)

        self.set_bsp_config(conf)

        conf.check()

    def GetMetricModel(self):
        res: List[CMACD | CTrendModel | BollModel | CDemarkEngine | RSI | KDJ] = [
            CMACD(
                fastperiod=self.macd_config['fast'],
                slowperiod=self.macd_config['slow'],
                signalperiod=self.macd_config['signal'],
            )
        ]
        res.extend(CTrendModel(TREND_TYPE.MEAN, mean_T) for mean_T in self.mean_metrics)

        for trend_T in self.trend_metrics:
            res.append(CTrendModel(TREND_TYPE.MAX, trend_T))
            res.append(CTrendModel(TREND_TYPE.MIN, trend_T))
        res.append(BollModel(self.boll_n))
        if self.cal_demark:
            res.append(CDemarkEngine(
                demark_len=self.demark_config['demark_len'],
                setup_bias=self.demark_config['setup_bias'],
                countdown_bias=self.demark_config['countdown_bias'],
                max_countdown=self.demark_config['max_countdown'],
                tiaokong_st=self.demark_config['tiaokong_st'],
                setup_cmp2close=self.demark_config['setup_cmp2close'],
                countdown_cmp2close=self.demark_config['countdown_cmp2close'],
            ))
        if self.cal_rsi:
            res.append(RSI(self.rsi_cycle))
        if self.cal_kdj:
            res.append(KDJ(self.kdj_cycle))
        return res

    def set_bsp_config(self, conf):
        para_dict = {
            "divergence_rate": 0.9,         # 1类（和1p类）买卖点MACD背驰力度，默认为 0.9（1：出中枢笔 vs 进中枢笔；1p：相邻同向笔；出中枢笔(后笔)MACD面积 ≤ 0.9×进中枢笔(前笔)MACD面积）
            "min_zs_cnt": 1,                # 1类（和1p类）买卖点至少要经历几个中枢，默认为 1
            "bsp1_only_multibi_zs": True,
            "max_bs2_rate": 0.9,            # 2类买卖点那一笔回撤最大比例，默认为 0.9999；如果是 1.0，相当于允许回测到1类买卖点的位置
            "macd_algo": "full_area",       # MACD背驰计算；整根笔对应的MACD同向面积累加（上涨笔取正柱，下跌笔取负柱）
            "bs1_peak": False,              # 1类（非1p类）买卖点位置是否必须是整个中枢范围内所有笔中的最高点(上涨)或最低点(下跌)，默认为 True
            "bs_type": "0",                 # 买卖点类型：0震荡；1趋背/盘背；1p实为段背；3a 中枢在1类后面；3b 中枢在1类前面
            "bsp2_follow_1": True,          # 2类买卖点是否必须跟在1类买卖点后面（用于小转大时1类买卖点因为背驰度不足没生成），默认为 True
            "bsp3_follow_1": False,         # 3类买卖点是否必须跟在1类买卖点后面，默认为 True（没有1类点就不算3类点，忽视了有“小转大”的可能）
            "bsp3_peak": False,             # 3类买卖点突破笔是不是必须突破中枢里面最高/最低的，默认为 False
            "bsp2s_follow_2": False,        # 类2买卖点是否必须跟在2类买卖点后面（2类买卖点可能由于不满足 max_bs2_rate），默认为 False
            "max_bsp2s_lv": None,           # 类2买卖点最大层级（距离2类买卖点的笔的距离/2），默认为None，不做限制
            "strict_bsp3": False,           # 3类买卖点对应的中枢，是否要求中枢进入笔"紧邻"1类点笔，默认为 False（允许1类点笔后走个ABC，ABC是后面中枢的进入段）
            "bsp3a_max_zs_cnt": 2,          # 3类买卖点最多可以跨越多少个中枢，默认为1的设计意图：只关注离1类点最近的那个中枢回拉产生的3a类点，越远的中枢越不可靠
        }
        args = {para: conf.get(para, default_value) for para, default_value in para_dict.items()}
        self.bs_point_conf = CBSPointConfig(**args)

        self.seg_bs_point_conf = CBSPointConfig(**args)
        self.seg_bs_point_conf.b_conf.set("macd_algo", "slope")
        self.seg_bs_point_conf.s_conf.set("macd_algo", "slope")
        self.seg_bs_point_conf.b_conf.set("bsp1_only_multibi_zs", False)
        self.seg_bs_point_conf.s_conf.set("bsp1_only_multibi_zs", False)

        for k, v in conf.items():
            if isinstance(v, str):
                v = f'"{v}"'
            v = _parse_inf(v)
            if k.endswith("-buy"):
                prop = k.replace("-buy", "")
                exec(f"self.bs_point_conf.b_conf.set('{prop}', {v})")
            elif k.endswith("-sell"):
                prop = k.replace("-sell", "")
                exec(f"self.bs_point_conf.s_conf.set('{prop}', {v})")
            elif k.endswith("-segbuy"):
                prop = k.replace("-segbuy", "")
                exec(f"self.seg_bs_point_conf.b_conf.set('{prop}', {v})")
            elif k.endswith("-segsell"):
                prop = k.replace("-segsell", "")
                exec(f"self.seg_bs_point_conf.s_conf.set('{prop}', {v})")
            elif k.endswith("-seg"):
                prop = k.replace("-seg", "")
                exec(f"self.seg_bs_point_conf.b_conf.set('{prop}', {v})")
                exec(f"self.seg_bs_point_conf.s_conf.set('{prop}', {v})")
            elif k in args:
                exec(f"self.bs_point_conf.b_conf.set({k}, {v})")
                exec(f"self.bs_point_conf.s_conf.set({k}, {v})")
            else:
                raise CChanException(f"unknown para = {k}", ErrCode.PARA_ERROR)
        self.bs_point_conf.b_conf.parse_target_type()
        self.bs_point_conf.s_conf.parse_target_type()
        self.seg_bs_point_conf.b_conf.parse_target_type()
        self.seg_bs_point_conf.s_conf.parse_target_type()


class ConfigWithCheck:
    def __init__(self, conf):
        self.conf = conf

    def get(self, k, default_value=None):
        res = self.conf.get(k, default_value)
        if k in self.conf:
            del self.conf[k]
        return res

    def items(self):
        visit_keys = set()
        for k, v in self.conf.items():
            yield k, v
            visit_keys.add(k)
        for k in visit_keys:
            del self.conf[k]

    def check(self):
        if len(self.conf) > 0:
            invalid_key_lst = ",".join(list(self.conf.keys()))
            raise CChanException(f"invalid CChanConfig: {invalid_key_lst}", ErrCode.PARA_ERROR)

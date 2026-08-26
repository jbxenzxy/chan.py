import os
from functools import lru_cache
from typing import List, Optional

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

try:
    from pydantic import field_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAVE_PYDANTIC_SETTINGS = True
except ImportError:  # 未安装 pydantic-settings 时降级为内置环境变量解析
    _HAVE_PYDANTIC_SETTINGS = False


# ═══════════════════════════════════════════════════════════════════════
# 阶段 2：算法参数配置层（V10 方案 7.1/7.2 —— 与 App/AppConfig.py 并行的双文件）
#   环境变量 / 仓库根 .env  →  覆盖引擎默认值；显式传入 conf 优先级最高。
#   未设置的项不进入覆盖字典，引擎行为与历史版本完全一致。
# ═══════════════════════════════════════════════════════════════════════

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(_REPO_ROOT, ".env")

# 环境变量名 → CChanConfig conf 键（均为引擎已知键，ConfigWithCheck 校验可通过）
_ENV_KEYS = {
    "BI_ALGO": "bi_algo",
    "BI_STRICT": "bi_strict",
    "BI_FX_CHECK": "bi_fx_check",
    "GAP_AS_KL": "gap_as_kl",
    "BI_END_IS_PEAK": "bi_end_is_peak",
    "BI_ALLOW_SUB_PEAK": "bi_allow_sub_peak",
    "SEG_ALGO": "seg_algo",
    "LEFT_SEG_METHOD": "left_seg_method",
    "ZS_COMBINE": "zs_combine",
    "ZS_COMBINE_MODE": "zs_combine_mode",
    "ONE_BI_ZS": "one_bi_zs",
    "ZS_ALGO": "zs_algo",
    "TRIGGER_STEP": "trigger_step",
    "SKIP_STEP": "skip_step",
    "MEAN_METRICS": "mean_metrics",
    "TREND_METRICS": "trend_metrics",
    "RSI_CYCLE": "rsi_cycle",
    "KDJ_CYCLE": "kdj_cycle",
    "CAL_DEMARK": "cal_demark",
    "CAL_RSI": "cal_rsi",
    "CAL_KDJ": "cal_kdj",
    "BOLL_N": "boll_n",
}


def _parse_env_file(path):
    """极简 .env 解析（与 App/AppConfig.py 同规则）：KEY=VALUE，忽略注释，去引号。"""
    result = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                result[key] = value
    except OSError:
        pass
    return result


def _to_bool(raw) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _to_int_list(raw):
    text = str(raw).strip()
    if text.startswith("["):
        text = text.strip("[]")
    return [int(x.strip()) for x in text.split(",") if x.strip()]


if _HAVE_PYDANTIC_SETTINGS:

    class ChanEnvSettings(BaseSettings):
        """算法参数环境面（pydantic-settings）：未设置 = None = 用引擎默认"""

        model_config = SettingsConfigDict(
            env_file=_ENV_FILE if os.path.exists(_ENV_FILE) else None,
            env_file_encoding="utf-8",
            extra="ignore",
        )

        symbol_code: str = "sh000001"       # 默认分析代码（V10 7.2：归 ChanConfig 管；标准格式 market(小写)+code）
        bi_algo: Optional[str] = None
        bi_strict: Optional[bool] = None
        bi_fx_check: Optional[str] = None
        gap_as_kl: Optional[bool] = None
        bi_end_is_peak: Optional[bool] = None
        bi_allow_sub_peak: Optional[bool] = None
        seg_algo: Optional[str] = None
        left_seg_method: Optional[str] = None
        zs_combine: Optional[bool] = None
        zs_combine_mode: Optional[str] = None
        one_bi_zs: Optional[bool] = None
        zs_algo: Optional[str] = None
        trigger_step: Optional[bool] = None
        skip_step: Optional[int] = None
        mean_metrics: Optional[List[int]] = None
        trend_metrics: Optional[List[int]] = None
        rsi_cycle: Optional[int] = None
        kdj_cycle: Optional[int] = None
        cal_demark: Optional[bool] = None
        cal_rsi: Optional[bool] = None
        cal_kdj: Optional[bool] = None
        boll_n: Optional[int] = None

        @field_validator("mean_metrics", "trend_metrics", mode="before")
        @classmethod
        def _parse_metric_list(cls, v):
            """同时接受 JSON 数组与逗号分隔两种环境变量写法（与降级路径行为一致）"""
            if isinstance(v, str):
                text = v.strip()
                if text.startswith("["):
                    import json
                    return json.loads(text)
                return [int(x.strip()) for x in text.split(",") if x.strip()]
            return v

else:

    class ChanEnvSettings:  # 降级实现：仅标量 + 逗号列表
        def __init__(self):
            merged = dict(_parse_env_file(_ENV_FILE))
            merged.update(os.environ)
            for env_key, conf_key in _ENV_KEYS.items():
                raw = merged.get(env_key)
                if raw is None or raw == "":
                    continue
                try:
                    if conf_key in ("mean_metrics", "trend_metrics"):
                        setattr(self, conf_key, _to_int_list(raw))
                    elif conf_key in ("skip_step", "rsi_cycle", "kdj_cycle", "boll_n"):
                        setattr(self, conf_key, int(raw))
                    elif isinstance(raw, bool):
                        setattr(self, conf_key, raw)
                    elif conf_key in ("bi_strict", "gap_as_kl", "bi_end_is_peak", "bi_allow_sub_peak",
                                      "zs_combine", "one_bi_zs", "trigger_step",
                                      "cal_demark", "cal_rsi", "cal_kdj"):
                        setattr(self, conf_key, _to_bool(raw))
                    else:
                        setattr(self, conf_key, raw)
                except (TypeError, ValueError):
                    print(f"[ChanConfig] 环境变量 {env_key}={raw!r} 解析失败，忽略")
            self.symbol_code = merged.get("SYMBOL_CODE", "").strip() or "sh000001"


@lru_cache(maxsize=1)
def _chan_env_settings():
    return ChanEnvSettings()


def reload_chan_env():
    """重新加载环境配置（测试 / 运行期改 .env 后手动刷新用）。"""
    _chan_env_settings.cache_clear()


def chan_env_overrides() -> dict:
    """返回已被环境变量/.env 显式设置的算法参数 {conf键: 值}。

    合并进 CChanConfig 的优先级：显式 conf > 环境变量 > 引擎内置默认。
    """
    s = _chan_env_settings()
    overrides = {}
    for env_key, conf_key in _ENV_KEYS.items():
        value = getattr(s, conf_key, None)
        if value is not None:
            overrides[conf_key] = value
    return overrides


def get_symbol_code() -> str:
    """默认分析代码（V10 7.2：SYMBOL_CODE 归 ChanConfig.py / .env 管）"""
    return _chan_env_settings().symbol_code


class CChanConfig:
    def __init__(self, conf=None):
        if conf is None:
            conf = {}
        # 环境变量/.env 覆盖引擎默认；显式传入的 conf 优先级最高（保持既有 API 语义）
        conf = {**chan_env_overrides(), **conf}
        conf = ConfigWithCheck(conf)
        self.bi_conf = CBiConfig(
            bi_algo=conf.get("bi_algo", "normal"),                 # 按缠论笔定义，非顶/底分型即成笔
            is_strict=conf.get("bi_strict", True),                 # bi_algo=normal时有效；顶/底分型间，至少隔1根K线
            bi_fx_check=conf.get("bi_fx_check", "loss"),           # 分型检查：loss模式（宽松）
            gap_as_kl=conf.get("gap_as_kl", False),
            bi_end_is_peak=conf.get('bi_end_is_peak', False),      # 关闭后，允许一笔之间有"不成笔的"更高/低分型；参见 上证指数 日K 25/09/03~25/10/31
            bi_allow_sub_peak=conf.get("bi_allow_sub_peak", True), # 打开后，允许笔端点的分型非局部最高/低分型；  参见 上证指数 日K 23/07/21~23/08/07
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
            "divergence_rate": 1,           # 11类（和11p类）买卖点MACD背驰力度，默认为 0.9（1：出中枢笔 vs 进中枢笔；1p：相邻同向笔；出中枢笔(后笔)MACD面积 ≤ 0.9×进中枢笔(前笔)MACD面积）
            "min_zs_cnt": 1,                # 11类（和11p类）买卖点至少要经历几个中枢，默认为 1
            "bsp11_only_multibi_zs": True,
            "max_bs22_rate": 0.9,           # 22类买卖点那一笔回撤最大比例，默认为 0.9999；如果是 1.0，相当于允许回测到11类买卖点的位置
            "macd_algo": "full_area_ext",   # MACD背驰计算；整根笔对应的MACD同向面积累加（上涨笔取正柱，下跌笔取负柱）
            "bs11_peak": False,             # 11类（非11p类）买卖点位置是否必须是整个中枢范围内所有笔中的最高点(上涨)或最低点(下跌)，默认为 True
            "bs_type": "0,1,2,3",           # 买卖点类型：0震荡；11趋背/盘背；11p段背；22回踩/回抽；22s类22；33a中枢在11类后面；33b中枢在11类前面；1一类买卖点；2待实现；3三类买卖点
            "bsp22_follow_11": True,        # 22类买卖点是否必须跟在11类买卖点后面（用于小转大时11类买卖点因为背驰度不足没生成），默认为 True
            "bsp33_follow_11": False,       # 33类买卖点是否必须跟在11类买卖点后面，默认为 True（没有11类点就不算33类点，忽视了有"小转大"的可能）
            "bsp33_peak": False,            # 33类买卖点突破笔是不是必须突破中枢里面最高/最低的，默认为 False
            "bsp22s_follow_22": False,      # 类22买卖点是否必须跟在22类买卖点后面（22类买卖点可能由于不满足 max_bs22_rate），默认为 False
            "max_bsp22s_lv": None,          # 类22买卖点最大层级（距离22类买卖点的笔的距离/2），默认为None，不做限制
            "strict_bsp33": False,          # 33类买卖点对应的中枢，是否要求中枢进入笔"紧邻"11类点笔，默认为 False（允许11类点笔后走个ABC，ABC是后面中枢的进入段）
            "bsp33a_max_zs_cnt": 2,         # 33类买卖点最多可以跨越多少个中枢，默认为1的设计意图：只关注离11类点最近的那个中枢回拉产生的33a类点，越远的中枢越不可靠
        }
        args = {para: conf.get(para, default_value) for para, default_value in para_dict.items()}
        self.bs_point_conf = CBSPointConfig(**args)

        self.seg_bs_point_conf = CBSPointConfig(**args)
        self.seg_bs_point_conf.b_conf.set("macd_algo", "slope")
        self.seg_bs_point_conf.s_conf.set("macd_algo", "slope")
        self.seg_bs_point_conf.b_conf.set("bsp11_only_multibi_zs", False)
        self.seg_bs_point_conf.s_conf.set("bsp11_only_multibi_zs", False)

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

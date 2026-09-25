from enum import Enum, auto
from typing import Literal


class DATA_SRC(Enum):
    BAO_STOCK = auto()
    CCXT = auto()
    CSV = auto()
    AKSHARE = auto()


class KL_TYPE(Enum):
    K_1S = 1
    K_3S = 2
    K_5S = 3
    K_10S = 4
    K_15S = 5
    K_20S = 6
    K_30S = 7
    K_1M = 8
    K_3M = 9
    K_5M = 10
    K_10M = 11
    K_15M = 12
    K_30M = 13
    K_60M = 14
    K_DAY = 15
    K_WEEK = 16
    K_MON = 17
    K_QUARTER = 18
    K_YEAR = 19


# ═══════════════════════════════════════════════════════════════════
# 频率注册表（单一事实源）：集中管理各频率的不变属性，新增周期只需在此
# 加一行，以下派生映射自动同步，杜绝多副本漏改。
# 行格式：(KL_TYPE, 秒数, 长中文标签, 是否日内分钟级, 是否秒级)
#   长标签为 None 时 _get_freq_label 回退默认 '日线'。
# 消费方（均在顶部 import 本文件派生视图）：
#   - Common/func_util   INTRADAY_FREQS / SUBSECOND_FREQS（_get_date_fmt 用）
#   - BuySellPoint/BSPointList  KL_TYPE_TO_FREQ / FREQ_TO_KL_TYPE
#   - App/AppUtils          FREQ_SEC_TO_KL / FREQ_TABLE（标签）
#   - DataAPI/TqSdkAPI   FREQ_SEC_MAP
#   - App/AppEngine      _STOCKS_MAIN_PERIOD
# ═══════════════════════════════════════════════════════════════════
FREQ_TABLE = {
    "15s": (KL_TYPE.K_15S, 15,      "15秒",  False, True),
    "30s": (KL_TYPE.K_30S, 30,      None,    False, False),
    "1m":  (KL_TYPE.K_1M,  60,      "1分钟", True,  False),
    "3m":  (KL_TYPE.K_3M,  180,     None,    False, False),
    "5m":  (KL_TYPE.K_5M,  300,     "5分钟", True,  False),
    "15m": (KL_TYPE.K_15M, 900,     "15分钟", True,  False),
    "30m": (KL_TYPE.K_30M, 1800,    "30分钟", True,  False),
    "60m": (KL_TYPE.K_60M, 3600,    "60分钟", False, False),
    "d":   (KL_TYPE.K_DAY, 86400,   "日线",   False, False),
    "w":   (KL_TYPE.K_WEEK, 604800, "周线",   False, False),
    "M":   (KL_TYPE.K_MON, 2592000, "月线",   False, False),
}

FREQ_SEC_MAP      = {f: r[1] for f, r in FREQ_TABLE.items()}       # freq → 秒数
KL_TYPE_TO_FREQ   = {r[0]: f for f, r in FREQ_TABLE.items()}       # KL → freq
FREQ_TO_KL_TYPE   = {f: r[0] for f, r in FREQ_TABLE.items()}       # freq → KL
FREQ_SEC_TO_KL    = {r[1]: r[0] for f, r in FREQ_TABLE.items()}    # 秒数 → KL
INTRADAY_FREQS    = {f for f, r in FREQ_TABLE.items() if r[3]}     # 日内分钟级
SUBSECOND_FREQS   = {f for f, r in FREQ_TABLE.items() if r[4]}     # 秒级


# ═══════════════════════════════════════════════════════════════════
# 市场周期支持集（单一事实源）——按市场界定「开放哪些周期」，股票/期货的周期
# 差异（股票 w/d/30m/15m/5m，期货 30m/5m/1m/15s）在此一次划分清楚，各自的变更
# 互不牵连（如股票新增 60m 只动 STOCKS_FREQS，不影响期货；反之亦然）。
# ── 为什么拆开 ──────────────────────────────────────────────
#   股票与期货的周期表本质不同（股票含 w/d 周月，期货含 15s 秒级），若共用一份
#   「支持周期」常量，那么任一方新增/删除周期都会连带波及另一方，长期维护时极易
#   误改对方。拆成两份后，市场支持的周期在各自集内独立演进。
# ── 为什么客观属性不拆（仍共享） ─────────────────────────────
#   周期「客观属性」（FREQ_TABLE 及派生映射：秒数、KL_TYPE、中文标签、是否日内
#   分钟级等）是周期本身的固有属性——一根 5m K 无论股票期货都是同一周期，客观
#   属性没有「市场」维度，按其定义不应随市场复制。所以这里只界定「该市场开放
#   哪些周期」，属性仍统一查 FREQ_TABLE；若把属性表也按市场复制一份，反而
#   重新制造「多副本漏改」的隐患（正是 FREQ_TABLE 设计要消灭的问题）。
# ── 消费方 ────────────────────────────────────────────────
#   · DataAPI/TqSdkAPI   SUPPORTED_FREQS（期货）
#   · App/AppConfig      回看配置 keys 守卫（STOCKS/FUTURES_LOOKBACK_CONFIG）
#   · Test/test_15m_period 前端 levels/FREQ_SEC_MAP_JS 跨语言镜像守卫
#   · _ORDERED 列表按 FREQ_TABLE 顺序稳定展开，供需要稳定序的场景（如前端按钮序）。
# ═══════════════════════════════════════════════════════════════════
STOCKS_FREQS          = frozenset({"w", "d", "30m", "15m", "5m"})
FUTURES_FREQS         = frozenset({"30m", "5m", "1m", "15s"})
STOCKS_FREQS_ORDERED   = [f for f in FREQ_TABLE if f in STOCKS_FREQS]
FUTURES_FREQS_ORDERED  = [f for f in FREQ_TABLE if f in FUTURES_FREQS]


class KLINE_DIR(Enum):
    UP = auto()
    DOWN = auto()
    COMBINE = auto()
    INCLUDED = auto()


class FX_TYPE(Enum):
    BOTTOM = auto()
    TOP = auto()
    UNKNOWN = auto()


class BI_DIR(Enum):
    UP = auto()
    DOWN = auto()


class BI_TYPE(Enum):
    UNKNOWN = auto()
    STRICT = auto()
    SUB_VALUE = auto()  # 次高低点成笔
    TIAOKONG_THRED = auto()
    DAHENG = auto()
    TUIBI = auto()
    UNSTRICT = auto()
    TIAOKONG_VALUE = auto()


BSP_MAIN_TYPE = Literal['0', '1', '2', '3', '11', '22', '33']


class BSP_TYPE(Enum):
    T0 = '0'      # 0类买卖点（神之一笔，不依赖中枢/背驰）
    T1 = '1'      # 1类买卖点（对应缠论一类买卖点）
    T2 = '2'      # 2类买卖点（待实现）
    T3 = '3'      # 3类买卖点（对应缠论三类买卖点）
    T11 = '11'  # 11类买卖点（原1类，趋势背驰）
    T11P = '11p'  # 11p类买卖点（原1p类，盘整背驰）
    T22 = '22'  # 22类买卖点（原2类，回踩/回抽）
    T22S = '22s'  # 22s类买卖点（原2s类，类二）
    T33A = '33a'  # 33a类买卖点（原3a类，中枢在11类后面）
    T33B = '33b'  # 33b类买卖点（原3b类，中枢在11类前面）

    def main_type(self) -> BSP_MAIN_TYPE:
        return self.value.rstrip('psab')  # type: ignore


class AUTYPE(Enum):
    QFQ = auto()
    HFQ = auto()
    NONE = auto()


class TREND_TYPE(Enum):
    MEAN = "mean"
    MAX = "max"
    MIN = "min"


class TREND_LINE_SIDE(Enum):
    INSIDE = auto()
    OUTSIDE = auto()


class LEFT_SEG_METHOD(Enum):
    ALL = auto()
    PEAK = auto()


class FX_CHECK_METHOD(Enum):
    STRICT = auto()
    LOSS = auto()
    HALF = auto()
    TOTALLY = auto()


class SEG_TYPE(Enum):
    BI = auto()
    SEG = auto()


class MACD_ALGO(Enum):
    # ===== MACD 面积族：三者均基于 MACD 柱(|macd|)的面积累加，区别在覆盖区间与反向柱处理 =====
    # 2026-09-25 由 AREA/FULL_AREA/FULL_AREA_EXT 改名，统一为 AREA_* 前缀，函数名对齐 Cal_MACD_area_*：
    # - AREA_HALF：仅取笔首/尾连续同向柱的"半段"面积（遇第一根反向柱即停），受 is_reverse 控制取首还是尾；
    #   原 Cal_MACD_half，改名为 area_half 以表达"半段/边界段"，不再误导为整笔面积。
    # - AREA_FULL：整笔所有同向柱 |macd| 之和，反向柱直接忽略（不计也不减）；原 Cal_MACD_area。
    # - AREA_FULL_EXT：AREA_FULL + 反向柱峰值修正(X−Y)；无反向柱时退化为 AREA_FULL；原 Cal_MACD_area_ext。
    AREA_HALF = auto()        # 2026-09-25 由 AREA 改名（原 Cal_MACD_half）
    AREA_FULL = auto()        # 2026-09-25 由 FULL_AREA 改名（原 Cal_MACD_area）
    AREA_FULL_EXT = auto()    # 2026-09-25 由 FULL_AREA_EXT 改名（原 Cal_MACD_area_ext）
    # ===== MACD 指标族：BAR/DIF/DEA 均为 MACD 衍生指标的整笔峰值 =====
    # 2026-09-25 重命名：原 MACD_ALGO.PEAK → MACD_ALGO.BAR（与 DIF/DEA 对齐，统一用「指标名」）。
    # 改名原因：原命名用「操作名」(PEAK=峰值) 而非「指标名」，与 MACD_ALGO.DIF/Cal_MACD_dif()、
    # MACD_ALGO.DEA/Cal_MACD_dea() 的「指标名」风格不一致，极易误解为「这是一个峰值模式/开关」。
    # 语义未变：本成员计算的是整笔(BAR = MACD 柱/直方图)的峰值(极值)，只是把名称统一为「指标名」。
    # 配置关键字：唯一规范名为 "bar"。"peak" 历史别名已移除，旧配置须改为 "bar"。
    BAR = auto()
    DIF = auto()
    DEA = auto()
    DIFF = auto()             # MACD 柱最大−最小之差（与 DIF 不同，注意区分）
    SLOPE = auto()
    AMP = auto()
    VOLUMN = auto()
    AMOUNT = auto()
    VOLUMN_AVG = auto()
    AMOUNT_AVG = auto()
    TURNRATE_AVG = auto()
    RSI = auto()


class DATA_FIELD:
    FIELD_TIME = "time_key"
    FIELD_OPEN = "open"
    FIELD_HIGH = "high"
    FIELD_LOW = "low"
    FIELD_CLOSE = "close"
    FIELD_VOLUME = "volume"  # 成交量
    FIELD_TURNOVER = "turnover"  # 成交额
    FIELD_TURNRATE = "turnover_rate"  # 换手率


TRADE_INFO_LST = [DATA_FIELD.FIELD_VOLUME, DATA_FIELD.FIELD_TURNOVER, DATA_FIELD.FIELD_TURNRATE]

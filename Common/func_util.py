from .CEnum import BI_DIR, KL_TYPE


def kltype_lt_day(_type: KL_TYPE):
    return _type.value < KL_TYPE.K_DAY.value


def kltype_lte_day(_type: KL_TYPE):
    return _type.value <= KL_TYPE.K_DAY.value


def check_kltype_order(type_list):
    last_lv = type_list[0].value
    for kl_type in type_list[1:]:
        assert kl_type.value < last_lv, "lv_list的顺序必须从大级别到小级别"
        last_lv = kl_type.value


def revert_bi_dir(dir):
    return BI_DIR.DOWN if dir == BI_DIR.UP else BI_DIR.UP


def has_overlap(l1, h1, l2, h2, equal=False):
    return h2 >= l1 and h1 >= l2 if equal else h2 > l1 and h1 > l2


def str2float(s):
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_inf(v):
    if isinstance(v, float):
        if v == float("inf"):
            v = 'float("inf")'
        if v == float("-inf"):
            v = 'float("-inf")'
    return v


# ═══════════════════════════════════════════════════════════════════
# 周期分类（单一事实源）：供 App/utils 与 BuySellPoint/BSPointList 等多层
# 共同消费，避免在高层复制成双副本（任一层的 _get_date_fmt / 红框时机
# 判断都依赖这三项，若分散会导致新增周期时漏改）。
#   App/utils.py / BSPointList.py 均改为 from Common.func_util import ...
#   单向依赖方向：App → Common、BuySellPoint → Common（不产生反向边）。
# ═══════════════════════════════════════════════════════════════════
# 日内分钟级周期集合（红框/买卖点日期格式按此判定）
INTRADAY_FREQS = {"30m", "15m", "5m", "1m"}
# 秒级周期（K线时间含秒）
SUBSECOND_FREQS = {"15s"}


def _get_date_fmt(freq):
    """根据周期返回统一日期格式（使用斜杠 / 分隔符，与 CChan 输出格式一致）。

    - 秒级（15s）→ "%Y/%m/%d %H:%M:%S"
    - 分钟级（30m, 15m, 5m, 1m）→ "%Y/%m/%d %H:%M"
    - 日线及以上（d, w, m, q, y）→ "%Y/%m/%d"
    """
    if freq in SUBSECOND_FREQS:
        return "%Y/%m/%d %H:%M:%S"
    if freq in INTRADAY_FREQS:
        return "%Y/%m/%d %H:%M"
    return "%Y/%m/%d"

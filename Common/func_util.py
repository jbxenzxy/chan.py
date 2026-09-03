from .CEnum import BI_DIR, INTRADAY_FREQS, KL_TYPE, SUBSECOND_FREQS


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
# 周期分类：INTRADAY_FREQS / SUBSECOND_FREQS 的单一事实源是 Common.CEnum
# 的 FREQ_TABLE（见 CEnum.py，顶部 import），此处仅是再导出，供
# _get_date_fmt 与本仓既有消费方（App/utils、BSPointList、AppEngine）沿用，
# 不再内联复制。
# ═══════════════════════════════════════════════════════════════════


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

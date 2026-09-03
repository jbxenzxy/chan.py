# -*- coding: utf-8 -*-
"""
App/utils.py —— App 层公共工具（代码解析 + 引擎纯函数/常量）
=========================================================================
本模块收纳两类无业务状态的公共工具，供 AppEngine / AppSSE 等统一导入：

  A. 证券代码解析
     _get_stock_name / _get_stock_market_code / _get_market_code
     依赖配置/数据源（app_config / app_data / DataAPI）。为最小化模块
     加载耦合，这些函数保持函数内 import（惰性依赖）。

  B. 引擎纯函数/常量
     MACD/EMA、周期映射、日期格式、左肩定位、中枢确认、期货双窗口映射、
     SSE 调试旗等。无状态纯函数/常量，以本模块为唯一事实源，
     AppEngine 与 AppSSE 统一从本模块导入（AppEngine 顶部 re-import 兼容）。

顶层 import 说明：引擎纯函数需要 app_config / KL_TYPE / CChanConfig /
CTqSdkAPI 顶层导入（均不反向 import App，无循环依赖）；代码解析函数
保持函数内 import。
"""
import os
import re

# ── 顶层依赖（引擎纯函数需要；均不反向 import App，无循环依赖）──
from App.AppConfig import app_config
# chan.py 核心（KL_TYPE 枚举）
from Common.CEnum import KL_TYPE
# 缠论配置（_make_chan_config）
from ChanConfig import CChanConfig
# 天勤数据源（_get_kl_type 经 FREQ_SEC_MAP 换算；缺失时降级）
try:
    from DataAPI.TqSdkAPI import CTqSdkAPI
except ImportError:
    CTqSdkAPI = None
from App.AppLog import get_logger
log = get_logger(__name__)


# SSE 推送详细调试日志开关（设为 True 可恢复调试输出；单一事实源 app_config.sse_debug）
_SSE_DEBUG = app_config.sse_debug


# ═══════════════════════════════════════════════════════════════════
# A. 证券代码解析
# ═══════════════════════════════════════════════════════════════════

# 港股指数 HZ 文件代码 → 字母代码（与 DataAPI/TdxAPI.py 的 _HK_INDEX_HZ_MAP
# 互为镜像：数据层持正向表换算 27#HZxxxx 文件路径，应用层持反向表做代码归一。
# App/utils 不 import 数据源（数据源门禁：仅 TqSdkAPI），故此处自含小表。）
_HK_HZ_TO_LETTER = {
    "HZ5017": "HSTECH",  # 恒生科技指数（Hang Seng TECH）
    "HZ5489": "HSIDI",   # 恒生创新药指数（Hang Seng Innovative Drug）
}


def _get_stock_name(market, code):
    """获取股票名称（委托 app_data.get_stock_name）"""
    from App.AppData import app_data
    return app_data.get_stock_name(market, code)


def _get_stock_market_code(code):
    """识别股票/指数代码，返回 (market, code)；无法识别返回 (None, code)。

    写法统一契约（长痛不如短痛，数字代码只认一种标准）：
      标准格式 = market(小写) + code(数字)，market 在前、无任何连接符。
      例：sh600519 / sz000001 / hk00700 / ds932000 / bj430047。
    其它一切历史写法（点号连接、code 在前、market 大写、后缀、.HK 等）一律
      拒绝并返回 (None, 原样)——绝不静默兼容，杜绝新功能又照着旧做法写。
    保留的非写法便利：
      · 港股指数的「hk+指数名」：hkHSTECH → ('hk','HSTECH')
      · 港股指数的「hk+HZ 文件代码」：hkHZ5489 → ('hk','HSIDI')（反向查表归一）
      · 别名/简称关键词：HSI / HSTECH / 中证2000 / ZZ2000（大小写不敏感）
      · 裸数字（无 market 限定）仍按首位自动推断默认市场
    """
    code = code.strip()

    # ── 便捷别名与关键词（非 market+code 写法）──
    _DS_INDEX_ALIASES = {
        "ZZ2": ("ds", "932000"), "ZZ2000": ("ds", "932000"),
        "中证2000": ("ds", "932000"), "932000": ("ds", "932000"),
    }
    _HK_INDEX_ALIASES = {
        "HSTECH": ("hk", "HSTECH"), "HSI": ("hk", "HSI"),
        "HSCEI": ("hk", "HSCEI"), "HSCCI": ("hk", "HSCCI"),
        "HSIDI": ("hk", "HSIDI"),           # 恒生创新药指数
        "HSKJ": ("hk", "HSTECH"),           # 恒生科技（拼音首字母）
        "HSCXY": ("hk", "HSIDI"),           # 恒生创新药（拼音首字母）
    }
    _up = code.upper()
    if _up in _DS_INDEX_ALIASES:
        return _DS_INDEX_ALIASES[_up]
    if _up in _HK_INDEX_ALIASES:
        return _HK_INDEX_ALIASES[_up]
    # 港股指数的 hk+指数名 便捷写法
    if code[:2].lower() == 'hk':
        suffix = code[2:].upper()
        if suffix.isalpha():
            return 'hk', suffix
        # hk+HZ 通达信港股指数文件代码（如 hkHZ5489 恒生创新药 → HSIDI）：
        # 反向查 _HK_HZ_TO_LETTER 归一为字母代码，使数据层 _hk_day_file
        # 命中 27#HZxxxx.day（若直接透传 HZ 代码会走 31# 个股路径而失配）。
        if re.match(r'^HZ\d+$', suffix):
            letter = _HK_HZ_TO_LETTER.get(suffix)
            if letter:
                return 'hk', letter

    # ── 唯一标准数字写法：market(小写) + code(4~6位数字)，无连接符 ──
    m = re.match(r'^([a-z]{2})(\d{4,6})$', code)
    if m and m.group(1) in ('sh', 'sz', 'hk', 'bj', 'ds'):
        mkt, c = m.group(1), m.group(2)
        if mkt == 'hk' and len(c) == 4:
            return mkt, '0' + c   # 港股 4 位补零（通达信文件统一 5 位）
        return mkt, c

    # ── 裸数字默认市场推断（与写法正交：无 market 限定）──
    if len(code) == 5 and code.isdigit():
        return 'hk', code
    if len(code) == 4 and code.isdigit():
        return 'hk', '0' + code
    if len(code) == 6 and code.isdigit():
        hk_file = os.path.join(app_config.vipdoc_dir, "ds", "lday", f"31#{code}.day")
        if os.path.exists(hk_file):
            return 'hk', code
        ds_file = os.path.join(app_config.vipdoc_dir, "ds", "lday", f"62#{code}.day")
        if os.path.exists(ds_file):
            return 'ds', code
    mkt = _infer_bare_code_market(code)
    if mkt:
        return mkt, code
    for _m in ['sh', 'sz']:
        f = os.path.join(app_config.vipdoc_dir, _m, "lday", f"{_m}{code}.day")
        if os.path.exists(f):
            return _m, code
    f = os.path.join(app_config.vipdoc_dir, "ds", "lday", f"31#{code}.day")
    if os.path.exists(f):
        return 'hk', code
    f = os.path.join(app_config.vipdoc_dir, "ds", "lday", f"62#{code}.day")
    if os.path.exists(f):
        return 'ds', code
    return None, code


def _infer_bare_code_market(bare_code):
    """单一事实源：裸代码首位 → 市场（沪深同号消歧的地面规则）。

    _get_stock_market_code 的 A 股判断段与 AppChart.search_stocks 的市场兜底
    共用此函数，避免两份首位规则各自演化又互相打架（历史上「每次加功能就改坏
    沪深重名」的根源之一）。规则覆盖 A 股与 B 股：

      - hk: 4/5 位纯数字（历史港股写法，如 00700 / 9926）
      - sh: 6xx(沪A) / 5xx(沪ETF) / 9xx(沪B) / 88xx(板块指数) / 99xx(指数)
      - sz: 0xx(深A) / 3xx(创业板) / 1xx(深ETF) / 2xx(深B)
    无法判定返回 None（交由上层文件探测/兜底决定）。
    """
    if not isinstance(bare_code, str) or not bare_code.isdigit():
        return None
    if len(bare_code) == 4 or len(bare_code) == 5:
        return "hk"
    if bare_code[:1] in ("6", "5", "9"):
        return "sh"
    if bare_code.startswith(("88", "99")):
        return "sh"
    if bare_code[:1] in ("0", "3", "1", "2"):
        return "sz"
    return None


def is_index(market, code):
    """是否为指数（单一事实源；注入 meta.is_index / 前端「成分股」置灰判定）。

    与 _infer_bare_code_market / _get_stock_market_code 的指数分段保持一致：
      - sh: 88xx 板块指数 / 99xx 指数 / 000xxx 上证·中证指数（如 sh000001 上证指数）
      - sz: 399xxx 深市指数（sz000xxx 为深市股票，如 sz000001 平安银行，非指数）
      - ds: 中证/扩展指数（932xxx 等）
      - hk: hk+指数名（HSTECH/HSI/HSCEI/HSCCI 等字母代码；数字代码为个股）
      - bj: 北交所股票，非指数
    前端灰化「成分股」只认此结果，不再靠 code 正则自行推断。
    """
    if not market or not isinstance(code, str):
        return False
    if market == "sh":
        return code.startswith(("88", "99")) or code.startswith("000")
    if market == "sz":
        return code.startswith("399")
    if market == "ds":
        return True
    if market == "hk":
        return not code.isdigit()  # hk 字母代码=指数名
    return False


def _get_market_code(code):
    """
    解析代码，返回 (market, code)
    market: 'sh' / 'sz' / 'hk' / 'ds' / 'futures'
    """
    try:
        from DataAPI.TqSdkAPI import _get_futures_code
    except ImportError:
        _get_futures_code = None
    code = code.strip()
    if _get_futures_code:
        futures_code = _get_futures_code(code.upper())
        if futures_code:
            return 'futures', futures_code
    return _get_stock_market_code(code)


# ═══════════════════════════════════════════════════════════════════
# B. 引擎纯函数/常量
# ═══════════════════════════════════════════════════════════════════
# 依赖方向：本部分 → AppConfig / DataAPI / ChanConfig / Common（单向）
#   - 纯函数/常量零业务状态，不触碰 app_data 实例字段；
#   - 周期/日期/配置相关常量单源于此，消除 AppEngine/AppSSE 双来源。

# ── MACD 计算（纯函数）──
def ema(data, period):
    """计算EMA"""
    result = []
    k = 2.0 / (period + 1)
    for i, val in enumerate(data):
        if i == 0:
            result.append(val)
        else:
            result.append(val * k + result[-1] * (1 - k))
    return result


def calculate_macd(closes, fast=12, slow=26, signal=9):
    """计算MACD"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema(dif, signal)
    macd = [2 * (d - a) for d, a in zip(dif, dea)]
    return [{"dif": dif[i], "dea": dea[i], "macd": macd[i]} for i in range(len(closes))]


def _inherit_macd_for_preview_bar(klines_list):
    """让预览K线（列表最后一根）继承前一根已确认K线的MACD值，避免跳变。
    预览K线的close是假数据（壁钟触发时用冻结K线的close填充），
    重算全序列MACD反而引入误差，不如直接继承前一根的值，
    等后续真实tick到来时再由tick路径用真实数据重算覆盖。"""
    if len(klines_list) < 2:
        return
    prev = klines_list[-2]
    klines_list[-1]['dif'] = prev.get('dif', 0)
    klines_list[-1]['dea'] = prev.get('dea', 0)
    klines_list[-1]['macd'] = prev.get('macd', 0)


# ── 周期映射（秒数 → KL_TYPE；取数唯一来源）──
_FREQ_SEC_TO_KL = {
    15: KL_TYPE.K_15S, 30: KL_TYPE.K_30S, 60: KL_TYPE.K_1M,
    180: KL_TYPE.K_3M, 300: KL_TYPE.K_5M, 900: KL_TYPE.K_15M,
    1800: KL_TYPE.K_30M, 3600: KL_TYPE.K_60M, 86400: KL_TYPE.K_DAY,
    604800: KL_TYPE.K_WEEK, 2592000: KL_TYPE.K_MON,
}


def _get_kl_type_by_sec(freq_sec):
    """秒数 → KL_TYPE（唯一来源；AppSSE._build_futures_chan 使用）"""
    return _FREQ_SEC_TO_KL.get(freq_sec, KL_TYPE.K_15S)


def _get_kl_type(freq):
    """根据频率字符串返回对应的 KL_TYPE 枚举值（委托秒数映射，经 FREQ_SEC_MAP 换算）"""
    freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 86400) if CTqSdkAPI else 86400
    return _get_kl_type_by_sec(freq_sec)


def _get_freq_label(freq):
    """根据频率字符串返回中文标签"""
    labels = {'15s': '15秒', '1m': '1分钟', '5m': '5分钟', '15m': '15分钟', '30m': '30分钟', '60m': '60分钟', 'd': '日线', 'w': '周线'}
    return labels.get(freq, '日线')


# ── 缠论配置（统一构造；配置值单源于 ChanConfig.CChanConfig 默认值）──
def _make_chan_config():
    """统一的缠论配置，股票和期货共用。配置值单源于 ChanConfig.CChanConfig 默认值"""
    return CChanConfig()


# ── 日期格式（freq → 统一日期格式，与 CChan 输出格式一致）──
# CSV列：股票代码,股票名,年K选点,季K选点,月K选点,周K选点,日K选点,30分选点,15分选点,5分选点,1分选点
# 日内周期集合：分钟级
INTRADAY_FREQS = {"30m", "15m", "5m", "1m"}
# 秒级周期：K线时间含秒
SUBSECOND_FREQS = {"15s"}


def _get_date_fmt(freq):
    """根据周期返回统一日期格式（使用斜杠 / 分隔符，与 CChan 输出格式一致）。

    - 秒级（15s）→ "%Y/%m/%d %H:%M:%S"
    - 分钟级（30m, 5m, 1m）→ "%Y/%m/%d %H:%M"
    - 日线及以上 → "%Y/%m/%d"
    """
    if freq in SUBSECOND_FREQS:
        return "%Y/%m/%d %H:%M:%S"
    if freq in INTRADAY_FREQS:
        return "%Y/%m/%d %H:%M"
    return "%Y/%m/%d"


# ── 中枢/左肩辅助（纯函数）──
def _bi_overlap_range(bi, zg, zd):
    """判断笔与中枢区间[zd, zg]是否严格重叠，与 chan.py has_overlap 默认语义一致。"""
    return min(zg, bi._high()) > max(zd, bi._low())


def _calc_zs_confirm_edt_from_bis(zs_obj, all_bi_list, date_fmt):
    """
    计算中枢事实确认结束时间。

    zs.end/edt 表示中枢内部最后一笔的结束时间；confirm_edt 表示第一根
    与中枢区间无重叠、且后面已经有 next 的笔的结束时间。这样可以避免
    用尾部无后继的笔过早确认中枢结束；当 next 是虚笔时，也能符合
    trigger_step=True 的实时语义。
    """
    try:
        end_idx = zs_obj.end_bi.idx
        zg, zd = zs_obj.high, zs_obj.low
    except Exception:
        return ""
    for bi in all_bi_list[end_idx + 1:]:
        if _bi_overlap_range(bi, zg, zd):
            continue
        if getattr(bi, "next", None) is None:
            return ""
        return bi.get_end_klu().time.toFmtStr(date_fmt)
    return ""


def _find_left_shoulder_time(kl_list, bi_list, bi_idx, freq):
    """
    找到分型左肩第一根原始K线的时间T。

    用户双击的分型K线是合并K线（分型中间K线），分型由三根合并K线组成：
    左肩 | 中间（分型）| 右肩。左肩合并K线可能由多根原始K线经过包含处理形成，
    需要找到左肩合并K线中最左边（最早）的那根原始K线对应的时间。

    参数:
        kl_list: KLine_List对象
        bi_list: 笔列表
        bi_idx: 前端双击命中的笔索引（该笔的begin_klu就是分型中间K线）
        freq: 周期

    返回:
        str: 格式化的时间字符串，如 "2026-01-09" 或 "2026-01-09 10:00"
        None: 定位失败
    """
    entry_bi = bi_list[bi_idx]
    begin_klu = entry_bi.get_begin_klu()  # 分型中间K线对应的klu

    # 在kl_list.lst中找到包含begin_klu的合并K线索引（分型中间位置）
    mid_idx = None
    for i, klc in enumerate(kl_list.lst):
        if hasattr(klc, 'lst') and klc.lst:
            for klu in klc.lst:
                if klu is begin_klu:
                    mid_idx = i
                    break
        if mid_idx is not None:
            break

    if mid_idx is None or mid_idx <= 0:
        log.warning(f"[警告] 无法定位分型中间K线在kl_list.lst中的位置")
        return None

    # 左肩 = 分型合并K线的前一个合并K线
    left_klc = kl_list.lst[mid_idx - 1]  # type: ignore[union-attr]

    # 取左肩原始K线序列的第一根（最左边）
    if hasattr(left_klc, 'lst') and left_klc.lst:  # type: ignore[union-attr]
        first_klu = left_klc.lst[0]  # type: ignore[union-attr]
    else:
        # 没有包含关系，左肩就是一根原始K线
        first_klu = (left_klc.get_high_peak_klu() or left_klc.get_low_peak_klu())

    if first_klu is None:
        log.warning(f"[警告] 无法获取左肩K线单元")
        return None

    return first_klu.time.toFmtStr(_get_date_fmt(freq))


# ── 期货双窗口周期映射（上窗周期 → 下窗周期）──
_FUTURES_DUAL_FREQ_MAP = {
    "30m": "5m",
    "5m": "1m",
    "1m": "15s",
}

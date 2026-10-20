# -*- coding: utf-8 -*-
"""
App/AppSSE.py —— SSE 实时流功能域
=========================================================================
期货实时行情通过 SSE 长连接持续推送到前端图表（/api/futures/read/stream）。
当前期货是唯一 SSE 实时流消费者；未来若增加股票实时，同样收纳于此
（命名取 SSE 而非 Futures 的原因）。

文件内按 5 区域划分（见各区域分隔头）：SSE 实时流 / 期货分析 / 期货选点 /
市场/代码/周期查询 / 期货退出清理。

依赖方向：AppSSE → AppEngine / App/AppUtils / AppData / DataAPI（单向）；
纯函数/常量与 AppEngine 统一从 App/AppUtils 导入；共享状态（选点/期货缓存）
一律经 app_data.* 公共 API（同一对象，零漂移）。
锁：SSE 路径每连接独立 TqApi + CChan + 记录缓存（session_context 绑定），
CChan 构建免锁。但 SSE 线程与 REST 线程共享 app_data —— 期货下窗 CChan
经 app_data.set_futures_sub_chan 写入，由 AppData._futures_cache_lock 保护。
"""
import json
import re
import time
import traceback
from datetime import datetime, date, timedelta

# 引擎侧依赖（仅引擎私有实现：天勤数据源 / 期货名称 / 回看根数折算；
# resolve_lookback_bars 与 CTqSdkAPI 同路线经 AppEngine 显式名，不直连 DataAPI）
from App.AppEngine import (
    TQ_AVAILABLE, CTqSdkAPI, _get_futures_name, resolve_lookback_bars,
)
# 会话上下文：缓存实例化到连接会话，绑定 CChan 内部 CTqSdkAPI 实例
from DataAPI.TqSdkAPI import session_context, session_set, session_clear
# 领域异常（期货路径使用领域异常，定义于 App/AppErrors.py）
from App.AppErrors import AppError, DataFetchError, AnalysisError
# 引擎纯函数/常量公共工具（与 AppEngine 统一从 App/AppUtils 导入）
from App.AppUtils import (
    _make_chan_config, _get_kl_type, _get_kl_type_by_sec, _get_freq_label, _get_date_fmt,
    ema,
    _calc_zs_confirm_edt_from_bis, _find_left_shoulder_time,
    _FUTURES_DUAL_FREQ_MAP, _SSE_DEBUG, _inherit_metrics_for_preview_bar,
    calculate_rsi, RsiStream, RSI_CYCLE,
    MacdStream, MACD_MIN_BARS,
)
# 业务数据层（选点/期货子窗缓存；与 AppEngine 同一 app_data 单例）
from App.AppData import app_data
# 区间套辅助（红框/双窗口共用，与 AppEngine 同源）
from BuySellPoint.BSPointList import _main_bi_range, _futures_red_range, CMyBSPointList, set_replay_mode
# chan.py 核心（与 AppEngine 同源；期货分析链路统一走 SSE init_chan_symbol）
from Chan import CChan
from Common.CEnum import AUTYPE, FX_TYPE
# SSE 数据源抽象（tqsdk 仅在 DataAPI 可见；生成器消费 src.* 协议；
# close_all/CSSESource 一并 re-export，API 层经本模块消费，不直连 DataAPI）
from DataAPI.TqSdkCSSESource import (  # noqa: F401
    CSSESource, CSSESourceClosed, CTqSdkSession, close_all,
    active_session_count,
)
from App.AppLog import get_logger
log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 区域 1 · SSE 实时流
# ═══════════════════════════════════════════════════════════════════════

# 期货窗口取数的公共参数（对齐股票 A/B/C 语义；见 _futures_window_fetch_bars）
_TQ_MAX_BARS = 10000      # 天勤单序列上限（官方 docstring「每个序列最大支持请求 10000 个数据」）
_BAR_ESTIMATE_MARGIN = 50 # 墙钟估算根数的固定余量（覆盖会话边界的不完整K线/估算误差）


def _parse_flex_time(s):
    """解析多格式时间字符串（%Y/%m/%d %H:%M:%S / %H:%M / %d），失败返回 None。"""
    if not s:
        return None
    for _fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, _fmt)
        except ValueError:
            continue
    return None


def _estimate_bars_between(t_from, t_to, freq_sec):
    """按墙钟估算 [t_from, t_to] 区间内 freq_sec 周期的K线根数上界。

    墙钟含非交易时段（夜盘休市/周末/节假日），结果只会偏大不会偏小——
    用于「至少拉多少根才能覆盖到 t_from」的取数上界（宁多勿少），
    多拉的部分由调用方的时间过滤（start_time/end_time）截掉。
    """
    if t_from is None or t_to is None or freq_sec <= 0:
        return 0
    secs = (t_to - t_from).total_seconds()
    if secs <= 0:
        return 0
    return int(secs // freq_sec) + 1 + _BAR_ESTIMATE_MARGIN


def _same_minute(dt, s):
    """dt 与时间字符串 s 是否同一分钟（选点判定用；15s 周期的秒位差异不算）。"""
    p = _parse_flex_time(s) if s else None
    if p is None or dt is None:
        return False
    return p.replace(second=0, microsecond=0) == dt.replace(second=0, microsecond=0)


def _futures_window_fetch_bars(freq_sec, start_time=None, end_time=None, base_bars=None):
    """计算一个期货窗口的取数根数（对齐股票 A/B/C 三种操作的左边界语义）。

    A（默认，无 start/end）：取 base_bars（缺省=该周期配置的回看条数）。
    B（start_time=T，选点）：[T, 最新] 全量 → 墙钟估算(T→now)+余量。
        对齐股票「选点后从T加载到最新」：左边界=T，不受配置根数限制。
    C（end_time，复盘）：[end-N, end] → N + 墙钟估算(end→now)。
        对齐股票「从结束时间往前推N根」：fetch 多拉 end→now 流逝的部分，
        拉回后先截到 ≤end 再取末 N 根（截断在 init_chan_symbol 内完成）。
    全部封顶天勤上限 10000（=期货数据源的「全量」；所需根数超限时自然
    降级为「最近 10000 根再过滤」，等价股票「对齐不足降全量」）。

    base_bars: 显式基准根数（双窗下窗传「上窗区间折算根数」实现对齐；None=按配置）。
    返回 (fetch_bars, base_bars_out)：fetch_bars 传给 fetch_kline 的 num_bars；
    base_bars_out 为 C 模式截断到末 N 根的目标根数（A/B 模式不额外截断）。
    """
    n = base_bars if base_bars and base_bars > 0 else resolve_lookback_bars(freq_sec)
    now = datetime.now()
    if end_time:
        _end_dt = _parse_flex_time(end_time)
        if _end_dt is not None:
            if start_time:
                # 组合窗口（复盘继承选点，2026-10-03 二期）：[start, end] 全量——
                # 墙钟估算(start→now) 覆盖选点起全部K线（含 end→now 流逝部分，
                # 拉回后由 init_chan_symbol 的 end 截断去掉），base_bars_out=None
                # → 调用方跳过「末 N 根」截断（对齐股票选点路径不做根数截断）。
                # start ≥ end（倒挂）不应到达此处（两个调用方均已报错中止）；
                # 纯函数无处报错，防御性走 C 分支（默认窗口）保底。
                _start_dt = _parse_flex_time(start_time)
                if _start_dt is not None and _start_dt < _end_dt:
                    fetch = _estimate_bars_between(_start_dt, now, freq_sec)
                    return (min(fetch, _TQ_MAX_BARS), None)
            fetch = n + _estimate_bars_between(_end_dt, now, freq_sec)
            return (min(fetch, _TQ_MAX_BARS), n)
    elif start_time:
        _start_dt = _parse_flex_time(start_time)
        if _start_dt is not None:
            fetch = _estimate_bars_between(_start_dt, now, freq_sec)
            return (min(fetch, _TQ_MAX_BARS), n)
    return (n, n)


# ═══════════════════════════════════════════════════════════════════════
# 期货复盘「目标不可达」的四类诊断（2026-10-17，方案 v1.18 §3.3）
# ═══════════════════════════════════════════════════════════════════════
# 复盘输入日期 D 早于窗口左界 L 时**不得静默改 L**（方案 §2 规则⑥「改 R 不改 L」
# —— L 冻结是复盘前后结构可比的前提）。但 L 可能由四种原因之一钉住，提示与出路
# 各不相同，必须分类而不是笼统「请扩大数据范围」：
#   contract_range    具体合约：D 不在该合约存续期（上市日 ~ 最后交易日）→ 换合约
#   tq_10000          天勤单序列上限：D 早于该周期「最近 10000 根」首根 → 换合约（该日主力）
#   before_selection  方式D：窗口以选点为界，D 早于选点 → 先取消选点
#   way_c             方式C：窗口 = 最近 N 根（N<10000），D 在可取范围内
#                     → **仅提示**（出路 = 调大 FUTURES_LOOKBACK_CONFIG 后重启；不由程序改 L）
# 判定顺序 = 硬约束优先：合约存续期 → 10000 根 → 选点 → 方式C。若 D 早于选点
# 但同时超出 10000 根，先报硬约束（取消选点也解决不了）。
# 事实来源全部取数据真值，不维护交易日历表：
#   · 合约存续期 = 该合约 30m 的 10000 根首/末根（30m 10000 根 ≈ 800 个交易日，
#     远大于任一 CFFEX 合约存续期 ⇒ 首根即上市日、末根即最后交易日）；
#   · 10000 根边界 = 当前周期 10000 根的首根（get_kline_serial 的硬上限）；
#   · 选点 = CSV 选点（与 L 同一事实源）。
_FUTURES_LIFE_FREQ = 1800   # 30m：探合约存续期（10000 根跨度 ≫ 合约存续期）
_FREQ_CN_BY_SEC = {15: "15秒", 60: "1分钟", 300: "5分钟", 1800: "30分钟"}
_CFFEX_ROLL_WINDOW_DAYS = 15   # 交割前 15 个日历天内视为换月窗口（保守值；窗口内才与次月比 vol）
# 非 CFFEX 无法按合约规则推算交割日，「序列是否已停更」只能用时限兜底：
# 末根距今超过该天数 ⇒ 判定为停更（已交割 / 长期停牌），而非撞上休市。
# 取值依据：A 股/期货最长连续休市 ≈ 9~10 个日历天（春节、国庆），取 15 留足余量；
# 且必须 > 休市上限，否则「查上周六」会被误报成 A′。CFFEX 走交割日精确判定，不用它。
_FUTURES_STALE_DAYS = 15


class FuturesReplayBlocked(Exception):
    """期货复盘目标不可达（四类之一）；携带 code / 文案 / 建议合约，供 SSE 错误帧透传。

    ⚠️ 契约：suggest 恒为 str——前端直接拼串并赋给 stock-code-input.value，
    改成任何非 str 类型都会让「切换」在前端抛 TypeError（评审第三轮 §1）。
    """

    def __init__(self, code, message, suggest=""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggest = suggest


def _third_friday(year, month):
    """当月第三个周五：CFFEX **股指**期货（IF/IH/IC/IM）的最后交易日/交割日。

    国债是**第二个**周五，别拿本函数算国债（见 `_cffex_expiry`）。
    """
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _split_product(symbol):
    """`CFFEX.IF2409` / `KQ.m@CFFEX.IF` → (交易所, 品种根)；解析不出 → ("", "")。"""
    if not symbol or "." not in symbol:
        return "", ""
    body = symbol.split("@")[-1]
    if "." not in body:
        return "", ""
    ex, prod = body.split(".", 1)
    m = re.match(r"^([A-Za-z]{1,3})(\d{3,4})?$", prod)
    if not m:
        return "", ""
    return ex.upper(), m.group(1)      # 品种根保留原大小写（rb2405 / IF2405）


_CFFEX_QUARTERS = (3, 6, 9, 12)   # 股指期货季月：3/6/9/12
# CFFEX 内部交割规则分两套，**混用必错**（2026-10-18 修）：
#   · 股指 IF/IH/IC/IM：最后交易日 = 到期月**第三个**周五，挂牌 = 当月、下月、
#     随后两个季月（共 4 个）；
#   · 国债 T/TF/TS/TL：最后交易日 = 到期月**第二个**周五，挂牌 = **最近的三个季月**
#     （共 3 个；2403 摘牌的同时 2412 挂牌，滚动保持三个）。
# 混用的后果（实测 CFFEX.T2409、D=2024-03-10）：候选落成 T2403/T2404/T2406/T2409
# （2404 根本不挂牌），交割日 03-15（真值 03-08）→ 建议 T2403 → 切过去又被判
# 「不在存续期」，再建议 T2403 ⇒ **同一个建议反复出现，切不出去**。
_CFFEX_BOND_ROOTS = ("T", "TF", "TS", "TL")   # 国债期货品种根


def _cffex_expiry(year, month, is_bond):
    """CFFEX 某合约月份的最后交易日：国债（is_bond）= 第二周五，股指 = 第三周五。

    节假日顺延 ±1 交易日，与文案「最后交易日约」口径一致（不维护交易日历表）。
    """
    if not is_bond:
        return _third_friday(year, month)
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 7)


def _cffex_contract_months(year, month, is_bond=False):
    """CFFEX 在市的合约月份（**股指 / 国债两套**，见 `_CFFEX_BOND_ROOTS` 注释）。

    · 股指：当月、下月、下季月、下下季月（4 个）。不能按自然月连续枚举：
      7/8 月、10/11 月 CFFEX 根本不挂牌（`IF2407` 不存在），既浪费两次取数，
      又会**漏掉季月**（4 月在市的是 2405/2406/2409/2412，自然月枚举只到 2408
      就停了，拿不到 2409/2412）。
    · 国债：从给定月起的最近三个季月（3 个）。同理不能枚举 4/7/10 月。
    """
    if is_bond:
        y, m = year, month
        while m not in _CFFEX_QUARTERS:          # 先走到当前（或下一个）季月
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        seq = []
        for _ in range(3):
            seq.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            while m not in _CFFEX_QUARTERS:
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return seq

    seq = [(year, month)]
    y2, m2 = (year + 1, 1) if month == 12 else (year, month + 1)
    seq.append((y2, m2))                       # 下月
    yy, mm, found = y2, m2, 0
    while found < 2:                           # 再取两个季月（跳过已是下月的那个）
        yy, mm = (yy + 1, 1) if mm == 12 else (yy, mm + 1)
        if mm in _CFFEX_QUARTERS and (yy, mm) not in seq:
            seq.append((yy, mm))
            found += 1
    return seq


def _contract_candidates_with_expiry(symbol, target_dt, months=5):
    """目标日期可能存在的同品种合约候选 + 交割日，形如 (code, expiry_or_None)。

    · CFFEX：按品种的真实挂牌规则枚举（股指 = 当月/下月/两个季月共 4 个；
      国债 = 最近三个季月共 3 个）= 该日全部在市合约，所以 IF/IH/IC/IM 的
      「该日主力合约」必定在候选里，不会漏；交割日在**生成处**随附
      （股指第三周五 / 国债第二周五，(y, mo) 本就在手——零解析、零失败路径），
      节假日顺延偏差 ±1 个交易日，与文案口径一致；
    · 其它交易所：交割规则各异（每月 10/15 日等），按月枚举、交割日 = None，
      **一律由 `_contract_covers` 用数据真值验证**，不维护交割日历表。
    CFFEX 先按最后交易日剔掉目标日**已交割**的月份（4/22 不给 IF2404）。
    """
    ex, root = _split_product(symbol)
    if not ex:
        return []
    d = target_dt.date() if isinstance(target_dt, datetime) else target_dt

    if ex == "CFFEX":
        is_bond = root.upper() in _CFFEX_BOND_ROOTS
        y, mo = d.year, d.month
        if _cffex_expiry(y, mo, is_bond) < d:    # 当月已交割 → 从下月起算
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        return [(f"{ex}.{root}{yy % 100:02d}{mm:02d}", _cffex_expiry(yy, mm, is_bond))
                for yy, mm in _cffex_contract_months(y, mo, is_bond)]

    out = []
    for k in range(0, months):
        y = d.year + (d.month - 1 + k) // 12
        mo = (d.month - 1 + k) % 12 + 1
        if ex == "CZCE":      # 郑商所三位月份：MA405
            out.append((f"{ex}.{root}{y % 10}{mo:02d}", None))
        else:                 # 其它交易所四位：rb2405
            out.append((f"{ex}.{root}{y % 100:02d}{mo:02d}", None))
    return out


def _contract_candidates(symbol, target_dt, months=5):
    """对外兼容形状（纯 code 列表）：现有断言与调用方零改动。
    内部需要交割日请用 `_contract_candidates_with_expiry`，勿再从 code 反解。"""
    return [code for code, _ in _contract_candidates_with_expiry(symbol, target_dt, months)]


def _ensure_futures_src_connected(src):
    """诊断探针需要数据源已连接；未连接则补一次（connect 已幂等）。

    为什么必须补：A/B 两类的判据全来自真实取数（合约存续期、10000 根首根），而
    `_sse_single_gen` / `_sse_dual_gen` 的**倒挂分类发生在 `src.connect()` 之前**。
    不补就会出现：`api is None` → `AttributeError: 'NoneType' has no attribute
    'get_kline_serial'` → 两个探针都失败 → A/B 全部跳过 → 误报成方式C
    「请加载更多数据」（配置已用满 10000 根时尤其自相矛盾）。
    """
    if getattr(src, "api", None) is not None:
        return True
    if not hasattr(src, "connect"):
        return True          # 测试桩 / 无连接概念的数据源：直接放行
    try:
        src.connect()
    except Exception as e:   # noqa: BLE001 —— 连不上就退化为无探针，由调用方降级
        log.warning(f"[诊断] 数据源连接失败，探针不可用: {type(e).__name__}: {e}")
        return False
    return getattr(src, "api", None) is not None


def _futures_contract_life(src, symbol):
    """具体合约存续期 (首根, 末根)；主连/指数或取不到 → None。

    ⚠️ 缓存：本函数**不再自持缓存**，直接复用 `_futures_series` 的 (symbol, freq)
    缓存 ⇒ 当前周期就是 30m 时，窗口探针取过的那一份被原样复用，同 freq 的
    「存续期探针」退化为零取数（此前两套缓存互不共享：30m 场景每个候选都要
    取两遍相同的 10000 根，5 个候选中 5 次是纯重复）。
    """
    if not symbol or "." not in symbol or symbol.upper().startswith("KQ"):
        return None
    recs = _futures_series(src, symbol, _FUTURES_LIFE_FREQ)
    return (recs[0]["dt"], recs[-1]["dt"]) if recs else None


def _cffex_contract_month(symbol):
    """`CFFEX.IF2409` → (2024, 9)；非 CFFEX / 主连 / 解析不出 → None。

    CFFEX 两个品种根（股指 IF/IH/IC/IM、国债 T/TF/TS/TL）合约代码都是四位 YYMM，
    故只做形状解析、不区分套别（套别由 `_CFFEX_BOND_ROOTS` 另行判定）。
    主连 `KQ.m@CFFEX.IF` 经 `split("@")[-1]` 后仍带交易所前缀，但 `_split_product`
    拿不到月份数字（它只返回字母根），故此处从原始代码重新取数字段。
    """
    ex, _root = _split_product(symbol)
    if ex != "CFFEX":
        return None
    body = symbol.split("@")[-1]
    if "." not in body:
        return None
    _ex, prod = body.split(".", 1)
    m = re.match(r"^[A-Za-z]{1,3}(\d{2})(\d{2})$", prod)
    if not m:
        return None
    return (2000 + int(m.group(1)), int(m.group(2)))


def _futures_series_stale(symbol, last_dt, end_dt):
    """序列在 end_dt 之后是否**已停更**——区别于「只是撞上休市日」。

    晚向（`D > 序列末根 且 D < now`）必须分开两件事，否则「查上周六」会被误报成 A′：
      · **休市**：序列仍在市，末根 = 上一交易日 ⇒ 不得报 A′（按正常截断处理）；
      · **停更**：合约已交割 / 长期停牌 ⇒ 报 A′（晚于最后交易日 + 建议切换）。
    判据按「合约规则优先、时限兜底」取，不维护交易日历表：
      · CFFEX：由合约代码月份算交割日（`_cffex_expiry`，股指第三周五 / 国债第二周五，
        节假日 ±1 交易日），`end_dt` 越过它 ⇒ 已交割 ——「刚交割几天」同样命中，
        不受 `_FUTURES_STALE_DAYS` 时限限制（那是给没有交割日规则的交易所用的）；
      · 其它交易所：退回时限兜底（末根距今 > `_FUTURES_STALE_DAYS`）。
    """
    ex, root = _split_product(symbol)
    if ex == "CFFEX":
        ym = _cffex_contract_month(symbol)
        if ym is not None:
            return end_dt.date() > _cffex_expiry(
                ym[0], ym[1], root.upper() in _CFFEX_BOND_ROOTS)
    return (datetime.now() - last_dt) > timedelta(days=_FUTURES_STALE_DAYS)


def _series_last_dt(klines):
    """未截断序列（get_kline_serial 的 klines DataFrame）末根时间；取不到 → None。

    datetime 列是纳秒时间戳（可能 None），转换口径与「追加当前形成中K线」处
    （`_lr = klines.iloc[-1]`）同款。判 `_late_boundary_is_live` 必须用它：
    init 返回的 records 已被截断到复盘点，拿它自比会把「D=历史休市日」
    误判成实时（2026-10-19 P1 回归）。
    """
    if klines is None or len(klines) == 0:
        return None
    _dns = klines.iloc[-1].get("datetime")
    return datetime.fromtimestamp(_dns / 1e9) if _dns is not None else None


def _late_boundary_is_live(end_time, end_dt, series_last_dt):
    """复盘边界落在**数据源**末根之后（且是历史日期）⇒ 本次连接按「回最新」服务。

    只处理「晚向休市」：D 晚于真实末根、但不是未来（末根=周五、盘前输入上周六）——
    该日没有 K 线，「截至该日」与「最新」是同一张图；仍冻结会得到一张徽标显示
    ● 实时、实际永不更新的图（页面不关跨过开盘即命中）。故按 §2 规则③
    「输入 ≥ 末根 ⇒ 回最新」服务：`is_replay=False`、更新循环不冻结。

    ⚠️ 第三参必须是**未截断**的序列末根（`_series_last_dt(klines)`）：
    2026-10-19 P1 回归的根因就是把 init 返回的**已截断** records 传进来自我比较——
    「D=历史休市日」截断后末根=D 前一交易日，`D > 它` 恒真 ⇒ 普通复盘（D 早于
    真实末根，该日之后还有 K 线）被静默改成实时 + 追加边界外最新根，图变成
    「截断历史 + 孤立一根」且中间不回补。传真实末根后：D 早于末根 ⇒ False
    （冻结复盘，回到 v1.18 行为）；只有 D 晚于末根才 True。

    series_last_dt 取不到（klines 空 / datetime 列 None）⇒ 保守判 False（冻结）：
    误冻结可由前端「回实时」按钮纠正，误实时是错乱图，两害取其轻。

    ⚠️ 今天的边界不走此路（保持冻结）：用户可能就想看「今天早些时候」。
    前端 `isToday` 已把它导向回实时，本条只对 API 直调生效。
    """
    if not end_time or end_dt is None or series_last_dt is None:
        return False
    if end_dt.date() >= datetime.now().date():
        return False
    return end_dt.date() > series_last_dt.date()


def _futures_series(src, symbol, freq_sec):
    """该周期「最近 10000 根」的 records（按会话缓存）。

    一份取数同时服务两件事：首根 = 天勤可取范围左边界；目标日的 vol = 主力判定。
    同一会话内同一 (合约, 周期) 只取一次。
    """
    cache = getattr(src, "_futures_series_cache", None)
    if cache is None:
        cache = {}
        src._futures_series_cache = cache
    key = (symbol, freq_sec)
    if key not in cache:
        recs = []
        try:
            _ensure_futures_src_connected(src)
            recs = src.fetch_kline(symbol, freq_sec=freq_sec, num_bars=_TQ_MAX_BARS) or []
        except Exception as e:  # noqa: BLE001
            log.warning(f"[诊断] 10000 根探针失败 {symbol}/{freq_sec}: {type(e).__name__}: {e}")
        cache[key] = recs
    return cache[key]


def _futures_earliest_at(src, symbol, freq_sec):
    """该周期「最近 10000 根」的首根时间 = 天勤可取范围的左边界。"""
    recs = _futures_series(src, symbol, freq_sec)
    return recs[0]["dt"] if recs else None


def _contract_volume_on(src, cand, freq_sec, end_dt):
    """候选合约在目标日的成交量（主力判定口径：当日成交量最大者为主力）。

    ⚠️ 时点口径：按**目标日全天**累计（该日所有 K 线的 vol 相加，含 D 时刻之后），
    与「按日复盘」粒度一致——不要误用作「截至 D 时刻」的累计量。"""
    day = end_dt.date()
    total = 0
    for r in _futures_series(src, cand, freq_sec):
        if r["dt"].date() == day:
            total += int(r.get("vol", 0) or 0)
    return total


def _contract_covers(src, cand, freq_sec, end_dt):
    """该合约在**当前周期**能否取到 end_dt：10000 根窗口 ≤ end_dt 且 末根 ≥ end_dt。

    判据只取**当前周期**的 10000 根（1 次取数），不再先探 30m 存续期：
      · D 早于窗口首根 → 不覆盖（与旧判据同结果：30m 窗口更宽，卡住的总是本周期）；
      · D 晚于序列末根（合约已交割，末根即最后交易日）→ 不覆盖（旧判据用 30m
        末根判定，日期级等价；对未交割合约末根即最新 K 线，也落在区间内）。
    省下的那 5 次 30m 取数**占原开销一半**，且这份记录本来就是 vol 仲裁要用的。
    """
    recs = _futures_series(src, cand, freq_sec)
    if not recs:
        return False
    first, last = recs[0]["dt"], recs[-1]["dt"]
    if end_dt.date() < first.date() or end_dt.date() > last.date():
        return False
    return end_dt >= first


def _contract_window_covers(src, cand, freq_sec, end_dt):
    """只验「当前周期 10000 根窗口覆盖」（1 次取数）。

    供 CFFEX 最近候选使用：其存续期覆盖由候选生成规则结构保证
    （cands[0] = 交割日 ≥ D 的最近月份，D 当天必在市），不需要实测；
    数据异常（未挂牌/取数失败）时 first=None → 判不覆盖 → 走回退全扫。
    """
    first = _futures_earliest_at(src, cand, freq_sec)
    return first is not None and end_dt >= first


def _suggest_contract_with_expiry(src, symbol, freq_sec, target_dt):
    """找一个「在当前周期能取到该日期」的同品种合约，返回 (code, expiry_or_None)。

    **最近交割合约优先**（v1.18 §3.6）：
      ⓪ 未来日期早退——SSE 是公开接口，直接调 end_time=2099 可绕过前端；未来无可
        建议合约，省掉全部候选探针；
      ① 最近候选（cands[0]，交割日最近且未交割）的**存续期覆盖由生成规则结构保证**，
        只验窗口覆盖（1 次取数）；
      ② 换月窗口内（D 距其交割日 ≤ _CFFEX_ROLL_WINDOW_DAYS 日历天）与次月比
        目标日 vol，胜者即真主力（股指主力只在当月↔次月间切换，季月从不做主力）；
        窗口外直接当月（当月为量头是经验规律，窗口 15 天已覆盖绝大多数移仓）；
      ③ 最近候选窗口不覆盖（数据异常 / 15s 等极短周期）→ **回退原全扫 + vol 仲裁**
        （逐字保留现状，异常兜底不制造死路）。
    为什么最近候选几乎总覆盖：其交割日距目标日 ≤ 约 1 个月，而 1m / 5m / 30m 的
    10000 根分别覆盖约 2 个月 / 20 个月 / 5 年 ⇒ 必落在窗口内。15s（10000 根 ≈
    7 个交易日）等极短周期可能全部候选都不行 → 返回 ("", None)，由调用方退化为
    「改用更大周期」。
    """
    if target_dt > datetime.now():
        return ("", None)
    cands = _contract_candidates_with_expiry(symbol, target_dt)
    if not cands:
        return ("", None)
    ex, _ = _split_product(symbol)

    if ex == "CFFEX":
        (c0, exp0), rest = cands[0], cands[1:]
        if _contract_window_covers(src, c0, freq_sec, target_dt):
            if (rest and (exp0 - target_dt.date()).days <= _CFFEX_ROLL_WINDOW_DAYS):
                c1, exp1 = rest[0]
                if _contract_window_covers(src, c1, freq_sec, target_dt):
                    # vol 读 _futures_series 缓存（窗口探针已取），零额外取数
                    if (_contract_volume_on(src, c1, freq_sec, target_dt)
                            > _contract_volume_on(src, c0, freq_sec, target_dt)):
                        return (c1, exp1)
            return (c0, exp0)

    best, best_vol, best_exp = "", -1, None
    for cand, exp in cands:
        if not _contract_covers(src, cand, freq_sec, target_dt):
            continue
        vol = _contract_volume_on(src, cand, freq_sec, target_dt)
        if vol > best_vol:      # 并列时保留先出现的（交割日最近者）
            best, best_vol, best_exp = cand, vol, exp
    return (best, best_exp)


def _suggest_contract_for_date(src, symbol, freq_sec, target_dt):
    """对外形态保持 str：错误帧 / 前端 / FuturesReplayBlocked.suggest / 现有测试零改动。
    （suggest 恒为 str——前端直接拼串并赋给 stock-code-input.value。）
    内部需要建议合约的交割日请用 `_suggest_contract_with_expiry`。"""
    return _suggest_contract_with_expiry(src, symbol, freq_sec, target_dt)[0]


def _classify_futures_replay(src, symbol, freq_sec, end_dt,
                             start_dt=None, start_is_selection=False,
                             window_bars=None, raw_first=None):
    """判定「复盘目标不可达」的类别；可达（D >= 窗口左界）返回 None。

    判定顺序 = **硬约束优先**（方案 v1.18 §3.6）：
      ① 合约存续期（仅具体合约）→ `contract_range`（A）：提示 + 切换按钮
      ② 天勤 10000 根           → `tq_10000`（B 具体合约 / E 主连）：提示 + 切换按钮
      ③ 选点钉住 L（方式D）     → `before_selection`（D / G）：**仅提示**
      ④ 配置根数钉住 L（方式C） → `way_c`（C / F）：**仅提示**
    「切换」= 前端换到建议合约（最近交割优先，换月窗口内按目标日 vol 仲裁）并自动
    复盘到该日期，不改当前窗口的 L。
    raw_first：调用方已持有「本周期 10000 根首根」时直接传入，省一次取数。
    """
    d_label = f"{end_dt:%Y/%m/%d %H:%M}"

    # ① 合约存续期：只对具体合约成立（主连无存续期概念，直接落到 ②）
    life = _futures_contract_life(src, symbol)
    if life is not None:
        first_bar, last_bar = life
        day = end_dt.date()
        out_early = day < first_bar.date()
        out_late = day > last_bar.date()
        if out_early or out_late:
            sug, sug_exp = _suggest_contract_with_expiry(src, symbol, freq_sec, end_dt)
            span = f"{first_bar:%Y/%m/%d} ~ {last_bar:%Y/%m/%d}"
            if out_early:
                why = f"不在 {symbol} 的存续期内（{span}，早于上市日）"
            else:
                why = f"晚于 {symbol} 的最后交易日（{span}）"
            if sug:
                if sug_exp is not None:
                    # CFFEX：交割日在候选生成处随附（推算值，节假日顺延 ±1 交易日近似）
                    exp_hint = f"最后交易日约 {sug_exp:%Y/%m/%d}，"
                else:
                    # 非 CFFEX：存续期走 30m 探针（_FUTURES_LIFE_FREQ）。当前周期就是 30m 时
                    # 命中窗口探针缓存（零额外取数）；**非同频时是一次新的 30m 取数**，
                    # 可能失败 → _sl 可为 None，下一行按 None 兜底（v1.18 后 _contract_covers
                    # 只取当前周期，不再预热 30m 缓存，「必非 None」的旧前提不成立）。
                    _sl = _futures_contract_life(src, sug)
                    # 值是实测末根 = 最后交易日（非 CFFEX 的交割日在最后交易日之后，
                    # 本应用不推算；标「交割日」会输出错误信息）
                    exp_hint = (f"最后交易日 {_sl[1]:%Y/%m/%d}，" if _sl else "该合约存续期未取到，")
                tail = f"请切换到该日主力合约 {sug}（{exp_hint}当前周期可取到该日期）。"
            elif out_late:
                # 未来日期：后续合约还没上市/无数据，换周期也解决不了，不能给
                # 「改用更大周期」这种无效指路
                tail = "该日期之后的合约尚未上市或无数据，请确认复盘日期。"
            else:
                tail = "同品种暂无可取到该日期的合约，请改用更大的周期。"
            return FuturesReplayBlocked(
                "contract_range", f"复盘日期 {d_label} {why}；{tail}", suggest=sug)

    # ② 天勤 10000 根：数据源单序列硬上限（换合约才能绕过）
    first = raw_first if raw_first is not None else _futures_earliest_at(src, symbol, freq_sec)
    if first is not None and end_dt < first:
        label = _FREQ_CN_BY_SEC.get(int(freq_sec), f"{int(freq_sec)}秒")
        sug, sug_exp = _suggest_contract_with_expiry(src, symbol, freq_sec, end_dt)
        head = (f"复盘日期 {d_label} 早于 {symbol} 在{label}周期的 10000 根下限"
                f"（最早只到 {first:%Y/%m/%d %H:%M}）")
        if sug:
            if sug_exp is not None:
                # CFFEX：删「存续期 a~b」（上市日不可推算），只给最后交易日约（推算值）
                exp_hint = f"最后交易日约 {sug_exp:%Y/%m/%d}"
            else:
                # 非 CFFEX：保留实测存续期。30m 探针在当前周期非 30m 时是**新的 30m 取数**
                # （可能失败 → _sl 可为 None，下一行按 None 兜底；30m 仍保留，因其给的是
                # 真实存续期，当前周期窗口更短会给错区间）。
                _sl = _futures_contract_life(src, sug)
                exp_hint = (f"存续期 {_sl[0]:%Y/%m/%d} ~ {_sl[1]:%Y/%m/%d}" if _sl else "存续期未取到")
            tail = f"；请切换到该日主力合约 {sug}（{exp_hint}）。"
        else:
            tail = ("；同品种暂无在该周期可取到该日期的合约，"
                    "请改用 5分钟 / 30分钟（同样 10000 根，覆盖的时间跨度更长）。")
        return FuturesReplayBlocked("tq_10000", head + tail, suggest=sug)

    # ③④ 目标早于窗口左界：L 是选点 → 方式D；L 是配置根数 → 方式C。两类都**只提示**
    #     （方式C 的「加载更多」是用户侧的显式操作，不由程序替他改 L —— 方案 §2 规则⑥）
    if start_dt is not None and end_dt < start_dt:
        if start_is_selection:
            return FuturesReplayBlocked(
                "before_selection",
                f"复盘日期 {d_label} 在选点 {start_dt:%Y/%m/%d %H:%M} 左侧"
                f"（方式D：复盘窗口 = [选点, 复盘点]，起点不能早于选点）；"
                f"如需查看，请先「取消选点」。")
        n = window_bars if window_bars else resolve_lookback_bars(freq_sec)
        if n >= _TQ_MAX_BARS:
            # 配置已经用满 10000 根：再说「请加载更多数据」自相矛盾（无更多可加载）。
            # 真正的边界在 A/B 两类（合约存续期 / 单序列上限），探针可用时不会走到这里。
            return FuturesReplayBlocked(
                "way_c",
                f"复盘日期 {d_label} 早于已加载数据起点 {start_dt:%Y/%m/%d %H:%M}；"
                f"当前窗口已是{_FREQ_CN_BY_SEC.get(int(freq_sec), str(int(freq_sec)) + '秒')}"
                f"周期的取数上限（10000 根），无法再往前加载。")
        reach = f"；10000 根内最早可到 {first:%Y/%m/%d %H:%M}" if first else ""
        # 方式C **只提示**：出路是把 FUTURES_LOOKBACK_CONFIG 调大并重启服务
        # （用户明确要求走配置、不在界面给一次性入口）⇒ 错误帧只带 code，不给按钮。
        return FuturesReplayBlocked(
            "way_c",
            f"请加载更多数据：复盘日期 {d_label} 早于已加载数据起点 "
            f"{start_dt:%Y/%m/%d %H:%M}（当前窗口 = 最近 {n} 根{reach}）。")
    return None


def init_chan_symbol(src, symbol, _name, freq_sec, freq_label, start_time=None, end_time=None, num_bars=None):
    """拉取历史K线 + 运行 chan.py 分析，返回 (chan, klines, kl_type, records) 或 None。
    由 SSE handler 调用，每个 SSE 连接自包含。
    start_time: 选点起始时间，有值时只拉取该时间之后的K线（B 操作：左边界=T，全量到最新）；
                与 end_time 同传时为组合窗口（复盘继承选点，左边界=T 不做根数截断）
    end_time: 复盘终点（软断开边界）：有值时只截取该时间之前（含）的K线建 chan，
              且取数为 N+复盘点到当前的流逝根数、建 chan 前截断到末 N 根
              （C 操作：左边界=从 end 往前推 N 根）；
              与 start_time 同传时窗口 = [start, end]（复盘继承选点）；
              update 循环停在此边界，不被实时拉新。None 为实时流。
    num_bars: 显式取数根数（双窗下窗传「上窗区间折算根数」实现对齐；None=按窗口语义计算）。
    首参为数据源对象 src（CTqSdkSession），服务层只消费
    src.fetch_kline / src.get_kline_serial 协议，不触碰 src.api 原始对象。"""
    import time as _time

    display_label = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{display_label}"

    try:
        # 窗口取数根数：A=配置回看 / B=[T,最新]全量 / C=N+流逝根数（见 _futures_window_fetch_bars）
        fetch_bars, base_bars = _futures_window_fetch_bars(
            freq_sec, start_time=start_time, end_time=end_time, base_bars=num_bars)
        # 取数前确保已连接（幂等）：SSE 主体已连时是空操作；直接调用 init 的场景
        # （定位 / 测试 / 未来新增入口）不会因 api=None 被下面的通用 except 吞成「初始化失败」。
        _ensure_futures_src_connected(src)
        records = src.fetch_kline(symbol, freq_sec=freq_sec, display_key=display_key,
                                  start_time=start_time, num_bars=fetch_bars)
        # 复盘终点截断：先滤出 <= end_time 的历史，再取末 N 根（C 操作左边界=从 end 往前推 N 根），
        # 再扣除"最新未完成K线"首根保护
        if end_time:
            _end_dt = _parse_flex_time(end_time)
            if records and _end_dt is not None:
                if fetch_bars >= _TQ_MAX_BARS and _end_dt < records[0]["dt"]:
                    # 目标早于本周期「最近 10000 根」首根 → 四类诊断（合约范围 / 10000 根）；
                    # 可达（判定为 None）则照常截断，不在这里拦
                    _blk = _classify_futures_replay(
                        src, symbol, freq_sec, _end_dt, raw_first=records[0]["dt"])
                    if _blk is not None:
                        raise _blk
                elif (_end_dt < datetime.now()
                        and _end_dt.date() > records[-1]["dt"].date()
                        and _futures_series_stale(symbol, records[-1]["dt"], _end_dt)):
                    # **晚向**：D 晚于该序列末根、且不是未来。
                    # v1.19 起前端已放行「D ≥ 末根 但 D < 今天」这一组合（app.js wantLive
                    # 加了 `D ≥ 今天` 合取项）——该组合只可能是「已交割 / 停牌的序列」，
                    # 因为仍在市的序列末根就是最新，不可能落后于一个历史日期。
                    # 但放行后「查上周六」（末根 = 周五）也会进到这里，所以必须用
                    # `_futures_series_stale` 区分「休市」与「停更」：休市不报（按正常截断
                    # 处理，等价于截至该休市日），停更才报 A′。
                    # 只认 contract_range：晚向不可能命中 10000 根/选点/方式C。
                    _blk = _classify_futures_replay(
                        src, symbol, freq_sec, _end_dt, raw_first=records[0]["dt"])
                    if _blk is not None and _blk.code == "contract_range":
                        raise _blk
            records = _truncate_records_by_end(records, end_time, freq_sec)
            if base_bars and len(records) > base_bars:
                records = records[-base_bars:]
        if len(records) > 1:
            now = datetime.now()
            if (now - records[-1]["dt"]).total_seconds() < freq_sec:
                records = records[:-1]

        if len(records) == 0:
            log.info(f"[{display_key}] ⑵ 无有效数据，跳过")
            return None

        t_chan = _time.time()

        # 统一构造：set_data + 周期映射 + 建 CChan + step_load（见 _build_futures_chan）
        # 数据注入经 src.set_data（Session 协议），不落类级缓存
        chan, kl_type = _build_futures_chan(records, symbol, freq_sec, src=src)

        klines = src.get_kline_serial(symbol, freq_sec)

        if _SSE_DEBUG:
            log.info(f"[{display_key}] ⑵ 缠论分析: 消费 {len(records)}根K线, 耗时 {_time.time()-t_chan:.1f}s")
        return (chan, klines, kl_type, records)

    except FuturesReplayBlocked:
        # 四类诊断结果直接上抛给 SSE 生成器（由它转成带 code 的 init 错误帧）
        raise
    except Exception as e:
        import traceback
        log.info(f"[{display_key}] ⑵ 失败: {e}")
        traceback.print_exc()
        return None


def _drain_chan(chan):
    """驱动 chan 增量计算（耗尽 step_load 生成器）。

    服务层纯业务函数：生成器经本函数消耗引擎，
    数据源不关心缠论计算。
    """
    for _snapshot in chan.step_load():
        pass


# ═══════════════════════════════════════════════════════════════════
# 增量快照：每根 K 线完成不全量 O(n) 重建 klines/MACD，
# 复用缓存快照仅追加新确认K线 + EMA 状态续算，结构元素仍重建。
# ═══════════════════════════════════════════════════════════════════


def _get_saved_point(code, freq):
    """查询单个选点（数据层漏斗；与 AppChart.get_saved_point 语义一致）。"""
    col = app_data.freq_to_col(freq)
    if not col:
        return ""
    # N2 收口：原为 `saved_point_times.get(code, {}).get(col, "")` 的**无锁
    # 两步读**（get_saved_point_time 的 docstring 里记的 check-then-act 窗口
    # 是同型缺陷），写者 save_point_time / clear_saved_point_time 持
    # _user_store_lock，可在两步之间改表。get_saved_point_time 是加锁单步
    # 出口、语义等价（无条目返回 ""），故此处不再引用裸出口。
    return app_data.get_saved_point_time(code, col)


def _sse_frame(event, payload) -> bytes:
    """构造一帧 SSE 事件（帧格式固定：event 行 + data 行 + 空行，字节级稳定）"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, allow_nan=False)}\n\n".encode("utf-8")


def _truncate_records_by_end(records, end_time, freq_sec, step=None, fmt_list=None):
    """按复盘终点截断 K 线序列（复盘软断开的数据边界）。

    语义：找到 <= end_time
    的最后一个锚点切出前缀；支持箭头步进 step 做偏移。返回截断后的 records；
    不足 5 根返回空列表（由调用方判定）。
    """
    if not records or not end_time:
        return records
    fmt_list = fmt_list or ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]
    target_dt = None
    for fmt in fmt_list:
        try:
            target_dt = datetime.strptime(end_time, fmt)
            break
        except ValueError:
            continue
    if target_dt is None:
        return records
    if step is not None:
        step = int(step)
        if step != 0:
            anchor_idx = None
            for i in range(len(records) - 1, -1, -1):
                if records[i]["dt"] <= target_dt:
                    anchor_idx = i
                    break
            if anchor_idx is not None:
                new_idx = anchor_idx + step
                if 0 <= new_idx < len(records):
                    target_dt = records[new_idx]["dt"]
    recs = [r for r in records if r["dt"] <= target_dt]
    return recs


def _per_frame_session(gen, src):
    """SSE 生成器的「逐帧重绑定会话」包装（见
    Docs/chan_lock_audit_v6.md 前提 A）。

    为什么需要它：starlette 对**同步生成器**是「每帧一次独立的
    `anyio.to_thread.run_sync`」（starlette/concurrency.py:57）——线程与
    连接之间**没有任何绑定关系**。因此「生成器入口 `session_set(src)` 一次」
    只能覆盖第一帧。实测 6 条并发连接 × 12 帧：4/6 发生跨线程迭代，
    28 帧读到**别的连接**的线程局部会话。后果分两种：
      · 落在没绑定过的线程 → CTqSdkAPI 回退到自己的空缓存 → 建链无数据；
      · 落在别的连接刚跑过的线程 → 读到别人的记录缓存 → **串品种**。
    实时循环每根K线的 `_drain_chan → step_load` 每次都会新建 CTqSdkAPI
    并读线程局部，且它位于 heartbeat 帧 `yield` 之后，是命中率最高的位置。

    本包装在**每一次 next() 派发**上重新绑定本连接会话，并用
    `session_context` 在帧体结束后还原该线程的旧值——既不丢绑定，也不把
    会话残留在线程池的公共线程上。

    ⚠ 不可用 contextvars 替代 threading.local：`run_sync` 在调用方上下文
    的**副本**中执行，帧内 `set()` 不会回传，同一条线程第 2 帧即丢值
    （实测），会把「偶发失配」变成「必然失配」。
    """
    while True:
        with session_context(src):
            try:
                item = next(gen)
            except StopIteration:
                return
        # 帧体跑完、会话已还原后才把这一帧交给框架推送：
        # 推送过程不需要会话，且避免在公共线程上留下会话残留。
        yield item


def sse_futures_stream_single(symbol, freq="15s", start_time=None, end_time=None,
                              reset_selection=False, source=None):
    """期货 SSE 单窗口 · 建会话并返回「逐帧重绑定会话」的生成器。

    reset_selection：**切到建议合约**的这次连接不继承选点（详见 `_sse_single_gen`
    同名参数）。默认 False = 原语义（缺失时从 CSV 恢复）。

    注意：本函数是**普通函数**（不是生成器）——会话必须在开始迭代之前就
    建好，`_per_frame_session` 才能在每一帧上重新绑定它。`CTqSdkSession()`
    构造只做实例级初始化（TqApi 在 `src.connect()` 内才创建，此处无网络
    I/O），故提前创建没有副作用。
    """
    src = source if source is not None else CTqSdkSession()
    return _per_frame_session(
        _sse_single_gen(symbol, freq, start_time, end_time, src,
                        reset_selection=reset_selection), src)


def _sse_single_gen(symbol, freq="15s", start_time=None, end_time=None, source=None,
                    reset_selection=False):
    """期货 SSE 单窗口 · 同步生成器（本体；对外经 _per_frame_session 包装）

    事件协议：
    init（初始快照/失败载荷）→ 实时循环（heartbeat 注释帧 + update 事件：
    tick 路径更新末根 K 线 OHLC/MACD，K线完成路径全量快照）→ 收尾清理。
    source 可注入（默认 CTqSdkSession）；Test/test_sse_gray.py 用 MockSource
    驱动确定性比对。锁：每连接独立会话，共享缓存经 AppData 按资源加锁。

    reset_selection：复盘连接**不继承 CSV 选点**（切到建议合约的那一次用它）。
    空 start_time 在协议里同时表示「前端没带 L」与「前端不要 L」两种意思，
    而后端对空值的语义是「从 CSV 恢复」——切到建议合约（月份合约）时，
    若该合约在 CSV 里存过晚于 D 的选点，复盘窗口 L 会被这个选点钉住，
    随即又报 before_selection（D/G），而 D/G 是**仅提示**、无「切换」按钮，
    用户切过去等于白切一次。切过去的目标是「看那一天」，不该继承选点。
    """
    # 修复：删除全局日志级别污染（原在此处将 tqsdk 及 root 全部 handler
    # 设 WARNING，永不恢复，导致整个进程 log.info 消失）。tqsdk 抑制已在
    # DataAPI/TqSdkAPI.py 顶层完成（只抑制自身 logger，绝不设 root）。
    log.info("SSE 单窗口连接: symbol=%s freq=%s", symbol, freq)

    src = source if source is not None else CTqSdkSession()
    # 入口这次绑定保留：覆盖第一帧，以及「本生成器被直接调用、未经
    # _per_frame_session 包装」的场景。**跨帧保障由 _per_frame_session
    # 负责**—— starlette 每帧独立线程派发，入口一次绑定覆盖
    # 不到后续帧（实测 4/6 连接跨线程迭代）。
    session_set(src)

    display_key = None
    freq_sec = None
    try:
        # 期货符号规范化（唯一入口）：短名 PTA→KQ.m@CZCE.TA，大小写
        # KQ.m@CFFEX.Im→KQ.m@CFFEX.IM。天勤 instrument_id **大小写敏感**，
        # 且各所品种根大小写不统一（CFFEX.IF 大写 / SHFE.rb 小写），
        # 所以不能在这里简单 upper()——那会把能用的改成不能用的。
        symbol = CTqSdkAPI.normalize_futures_symbol(symbol) or symbol

        freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 15)
        freq_label = freq
        freq_cn = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
        display_key = f"{symbol}:{freq_cn}"

        # 选点继承（2026-10-03 二期，对齐股票复盘窗口 X 语义）：
        #   · 复盘（end_time）继承选点：显式 start_time 优先生效（前端带
        #     当前窗口首根 = 窗口左边界 L），缺失时从 CSV 恢复（F5 刷新后
        #     前端内存丢失，CSV 是 SSOT）→ 窗口 = [start, end]（组合模式，
        #     不做根数截断）；
        #   · 起止倒挂（start ≥ end）→ 报错中止本次连接，**不静默回退**：
        #     与股票 `_analyze_stock_internal` 同判据（`>=`）、同文案，前端
        #     收到 init 错误帧后弹窗告知用户（需求⑼/⑽ 的兜底归一，见
        #     Docs/选点&复盘方案v1.18.md §3.5）；
        #   · 非复盘：start_time 缺失时从 CSV 恢复（B 操作/冷启动，原语义）。
        # meta.saved_selection_date 恒回显 CSV 真值（与股票同规则）：
        # A 复盘的 start=A左 不会冒充选点，复盘态选点/取消选点驱动前端菜单。
        if end_time:
            if start_time is None and not reset_selection:
                _saved = _get_saved_point(symbol, freq) or None
                if _saved:
                    start_time = _saved
                    log.info(f"[{display_key}] 复盘恢复选点: {start_time}")
            elif reset_selection:
                # 只述机制不述场景：本分支在「start 非空 + reset」组合（前端不发、
                # API 直调可达）也会进入，旧措辞「切合约连接」在那个组合下不属实。
                log.info(f"[{display_key}] reset_selection=1：跳过 CSV 选点恢复（不继承选点）")
            if start_time:
                _s_dt = _parse_flex_time(start_time)
                _e_dt = _parse_flex_time(end_time)
                if _s_dt is not None and _e_dt is not None and _s_dt >= _e_dt:
                    # 起止倒挂 ≠ 一律「请扩大数据范围」：按 L 被谁钉住分四类
                    # （合约范围 / 天勤10000根 / 选点 / 方式C，见方案 v1.18 §3.6）
                    _saved_pt = _get_saved_point(symbol, freq) or ""
                    _is_sel = _same_minute(_s_dt, _saved_pt)
                    _blk = _classify_futures_replay(
                        src, symbol, freq_sec, _e_dt, start_dt=_s_dt,
                        start_is_selection=_is_sel,
                        window_bars=resolve_lookback_bars(freq_sec))
                    if _blk is not None:
                        log.info(f"[{display_key}] {_blk.code}: {_blk.message}")
                        yield _sse_frame("init", {
                            "error": _blk.message, "code": _blk.code,
                            "suggest": _blk.suggest, "symbol": symbol})
                        return
                    # 兜底（探针全失败且不属四类）：保留原判据文案
                    _msg = f"复盘起始时间 {start_time} 不早于复盘截止时间 {end_time}"
                    log.info(f"[{display_key}] {_msg}")
                    yield _sse_frame("init", {"error": _msg, "symbol": symbol})
                    return
        elif start_time is None:
            col = app_data.freq_to_col(freq) or ""
            if col:
                _saved = _get_saved_point(symbol, freq) or None
                if _saved:
                    start_time = _saved
                    log.info(f"[{display_key}] 检测到保存选点: {start_time}")

        saved_selection_date = _get_saved_point(symbol, freq) or ""

        t_conn = time.time()
        src.connect()
        log.info(f"[{display_key}] ⓪ 连接天勤: 耗时 {time.time()-t_conn:.1f}s")

        t_total = time.time()
        name = _get_futures_name(symbol)  # 品种名称

        # 复盘软断开边界（墙钟比较基准；选点/实时流无 end_time 恒为 None）
        _end_dt = None
        if end_time:
            for _fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                try:
                    _end_dt = datetime.strptime(end_time, _fmt)
                    break
                except ValueError:
                    continue

        # === 1. 拉取历史 + chan 分析 ===
        # 历史拉取/chan 分析在服务层 AppSSE.init_chan_symbol 完成；
        # 首参传数据源对象 src（只消费 src.* 协议，不触碰 src.api 原始对象）
        # 复盘(end_time)时由 init 截断建 chan；复盘调试标志为**线程局部**
        # （set_replay_mode 存原值 + finally 恢复），SSE 每连接独占一条线程，
        # 天然不跨连接污染（原为进程级类变量，并发下会互相串改调试日志）。
        _replay_prev_s = set_replay_mode(bool(end_time))
        try:
            result = init_chan_symbol(src, symbol, name, freq_sec, freq_label, start_time, end_time)
        except FuturesReplayBlocked as _blk:
            # 目标早于本周期 10000 根 / 超出合约存续期：init 内部已判定出类别，
            # 这里必须转成带 code 的诊断帧。不接就会变成「连接异常中断」——
            # 前端既看不到原因，也拿不到「切换」按钮（冷启动无 start 时唯一出口）。
            log.info(f"[{display_key}] {_blk.code}: {_blk.message}")
            yield _sse_frame("init", {"error": _blk.message, "code": _blk.code,
                                      "suggest": _blk.suggest, "symbol": symbol})
            return
        finally:
            set_replay_mode(_replay_prev_s)
        if result is None:
            yield _sse_frame("init", {"error": "初始化失败（无数据或网络异常）", "symbol": symbol})
            return
        chan, klines, kl_type, records = result
        # 复盘边界落在**数据源**末根之后且非停更（休市日/长假）⇒ 按「回最新」服务：
        # is_replay 与更新循环的冻结判据都由它派生，避免出现「徽标 ● 实时但永不更新」。
        # ⚠️ 必须传未截断末根（klines 末根，_series_last_dt）：init 返回的 records
        # 已截断到复盘点，拿它自比会把「D=历史休市日」误判成实时（2026-10-19 P1 回归）。
        _is_replay_eff = bool(end_time) and not _late_boundary_is_live(
            end_time, _end_dt, _series_last_dt(klines))

        # === 2. 推送初始快照 ===
        t0 = time.time()
        try:
            init_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                            saved_selection_date=saved_selection_date,
                                            is_replay=_is_replay_eff)
            # ★ 追加当前形成中的K线（klines[-1]），让前端立即看到新K线
            # ⚠️ 复盘态（end_time 非空）不追加：复盘语义 = 末根冻结在复盘点、
            #   不被实时拉最新（本函数 update 循环的「复盘软断开」注释同款口径）。
            #   klines[-1] 是**数据源最新一根**（复盘边界之外），追加会让快照
            #   末根越界（实测：复盘 IF2409 到 2024/09/02 → 快照末根变
            #   2024/09/20）；且前端复盘态重连以末根当复盘点（app.js 的
            #   _replayEndMain/replayEnd3）→ 复盘点被悄悄改写为边界外时刻。
            #   复盘快照的边界末根由 records 截断保证（已含），无需此追加。
            #   v1.19 起判据改用 `_is_replay_eff`：边界落在末根之后但合约仍在市
            #   （休市日）时按实时服务，形成中的K线照常追加。
            if (not _is_replay_eff) and klines is not None and len(klines) > 0:
                _lr = klines.iloc[-1]; _dns = _lr.get('datetime')
                if _dns is not None:
                    _bdt = datetime.fromtimestamp(_dns / 1e9)
                    _bds = _bdt.strftime(_get_date_fmt(freq))
                    _ex = init_data.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _bds:
                        _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                            'open': round(float(_lr.get('open', 0) or 0), 3),
                            'high': round(float(_lr.get('high', 0) or 0), 3),
                            'low': round(float(_lr.get('low', 0) or 0), 3),
                            'close': round(float(_lr.get('close', 0) or 0), 3),
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0, 'rsi': 0})
                        _inherit_metrics_for_preview_bar(_ex)
                        init_data['meta']['kline_count'] = len(_ex)
            # 计算白色横虚线（初始快照，K线已确认状态）
            _kl_list = chan[kl_type]
            init_data['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _get_date_fmt(freq))
            yield _sse_frame("init", init_data)
            cached_snapshot = init_data  # ★ 缓存完整快照，tick推送时更新最后一根K线OHLC
            if _SSE_DEBUG:
                log.info(f"[{display_key}] ⑶ 推送init: "
                      f"K线{init_data['meta']['kline_count']}, "
                      f"笔{init_data['meta']['bi_count']}, "
                      f"中枢{init_data['meta']['zs_count']}, "
                      f"耗时 {time.time()-t0:.1f}s")
        except Exception as _e:
            yield _sse_frame("init", {"error": f"快照提取失败: {_e}", "symbol": symbol})
            return

        # === 3. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照 ===
        # 策略：壁钟（datetime.now()）判断K线周期结束，不等天勤
        # klines 推进信号；周期结束后 klines[-1] OHLC 已冻结可直接入缠论。
        BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
        if _SSE_DEBUG:
            log.info(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

        # last_bar_dt_ns: klines[-1] 的时间戳，用于检测 klines 是否推进
        # last_processed_dt_ns: 已处理过的K线时间戳，防止同一根K线被重复处理
        last_bar_dt_ns = None
        last_processed_dt_ns = None
        last_debug_print = time.time()

        # 性能统计
        t_wait_total = 0.0
        t_tick_total = 0.0
        t_step_total = 0.0
        t_snapshot_total = 0.0
        t_push_total = 0.0
        loop_count = 0
        tick_count = 0
        step_count = 0
        last_perf_print = time.time()
        # tick 增量状态：只缓存「到倒数第二根为止」的状态，供每 tick 续算末根（O(1)）。
        # 形如 {"main": {"n": 已喂根数, "s": 状态对象}}；长度变了（新根推进）或首次
        # 进入 ⇒ 重建（重建即全量喂一遍，正确性兜底由「n 必须等于 len(ex)-1」保证）。
        # 两个 dict 分别装 MacdStream 与 RsiStream —— 见 _tick_update_metrics。
        macd_tail_state = {}
        rsi_tail_state = {}

        while True:
            try:
                t_wait_start = time.time()
                src.wait_update(time.time() * 1e9 + 100_000_000)
                t_wait = time.time() - t_wait_start
                t_wait_total += t_wait
            except CSSESourceClosed:
                # 数据源正常关闭（仅 Mock/回放源）：等价客户端断开的自然结束
                return
            except Exception as _e:
                log.info(f"[{display_key}] wait_update 异常: {_e}")
                time.sleep(0.5)
                continue

            # 心跳注释帧（客户端断开由生成器线程退出触发 finally 清理）
            yield b": heartbeat\n\n"

            loop_count += 1

            now = datetime.now()
            now_ts = now.timestamp()

            # 复盘软断开：end_time 模式下，一但墙钟越过复盘边界所属周期，
            # 即停止推进 K 线/chan/快照（update 不把图"拉最新"），只保活连接。
            # 判据用 `_is_replay_eff`：边界落在末根之后但仍在市（休市日/长假）时
            # 本次连接按实时服务，不冻结（否则开盘后图不再更新）。
            if _is_replay_eff and _end_dt is not None and now > _end_dt:
                continue

            if len(klines) == 0:
                continue

            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            if dt_ns is None:
                continue

            # 初始化
            if last_bar_dt_ns is None:
                last_bar_dt_ns = dt_ns
                last_debug_print = now_ts
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DEBUG] 初始化: klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                continue

            # 每60秒打印一次性能统计
            if now_ts - last_perf_print >= 60.0:
                last_perf_print = now_ts
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [PERF] 循环{loop_count}次 | "
                          f"wait_update总计={t_wait_total:.1f}s | "
                          f"tick推送{tick_count}次总计={t_tick_total:.1f}s | "
                          f"step_load{step_count}次总计={t_step_total:.1f}s | "
                          f"快照提取总计={t_snapshot_total:.1f}s | "
                          f"SSE推送总计={t_push_total:.1f}s")
                t_wait_total = 0.0
                t_tick_total = 0.0
                t_step_total = 0.0
                t_snapshot_total = 0.0
                t_push_total = 0.0
                loop_count = 0
                tick_count = 0
                step_count = 0

            # ★ DEBUG: 每2秒打印一次当前状态
            if now_ts - last_debug_print >= 2.0:
                last_debug_print = now_ts
                bar_dt = datetime.fromtimestamp(dt_ns / 1e9)
                lag = now_ts - (dt_ns / 1e9 + freq_sec)
                pushed = (dt_ns != last_bar_dt_ns)
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DEBUG] 壁钟={now.strftime('%H:%M:%S')} "
                          f"klines[-1]={bar_dt.strftime('%H:%M:%S')} "
                          f"过期={lag:+.1f}s 推进={pushed} "
                          f"O={last_row.get('open'):.1f} H={last_row.get('high'):.1f} "
                          f"L={last_row.get('low'):.1f} C={last_row.get('close'):.1f}")

            # --- 检测上一根K线是否已完成 ---
            klines_pushed = (dt_ns != last_bar_dt_ns)

            if klines_pushed:
                # klines 已推进 → 上一根K线（klines[-2]）已冻结，立即处理
                completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row
                last_bar_dt_ns = dt_ns
            else:
                # klines 未推进 → 用壁钟判断当前K线周期是否已结束
                bar_end_ts = (dt_ns / 1e9) + freq_sec + BAR_COMPLETION_BUFFER
                if now_ts < bar_end_ts:
                    # 周期未结束，更新缓存快照的最后一根K线OHLC后推送完整格式
                    t_tick_start = time.time()
                    try:
                        if cached_snapshot is not None:
                            ex = cached_snapshot.get('klines', [])
                            if ex:
                                o = round(float(last_row.get('open', 0) or 0), 3)
                                h = round(float(last_row.get('high', 0) or 0), 3)
                                l = round(float(last_row.get('low', 0) or 0), 3)
                                c = round(float(last_row.get('close', 0) or 0), 3)
                                ex[-1]['open'] = o
                                ex[-1]['high'] = h
                                ex[-1]['low'] = l
                                ex[-1]['close'] = c
                                # ★ 实时续算末根（预览bar）的 MACD / RSI，避免前端跳变。
                                #   tick 只改末根 OHLC ⇒ 只续算一根（O(1)），
                                #   状态按窗口键隔离，见 _tick_update_metrics。
                                _tick_update_metrics(ex, macd_tail_state, rsi_tail_state, "main")
                                cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                                yield _sse_frame("update", cached_snapshot)
                                t_tick_total += time.time() - t_tick_start
                                tick_count += 1
                    except Exception as _e:
                        log.warning(f"[警告] 异常: {type(_e).__name__}: {_e}")
                    continue
                # 壁钟到期，当前K线（klines[-1]）已冻结
                completed_row = last_row

            completed_dt_ns = completed_row.get("datetime")
            if completed_dt_ns is None:
                continue

            # 防止重复处理同一根K线
            if completed_dt_ns == last_processed_dt_ns:
                continue
            last_processed_dt_ns = completed_dt_ns

            # 提取 OHLC
            o = float(completed_row.get("open", 0) or 0)
            h = float(completed_row.get("high", 0) or 0)
            l = float(completed_row.get("low", 0) or 0)
            cl = float(completed_row.get("close", 0) or 0)
            vol = int(completed_row.get("volume", 0) or 0)
            h = max(h, o, cl)
            l = min(l, o, cl)

            dt = datetime.fromtimestamp(completed_dt_ns / 1e9)
            bar_expected_end = (completed_dt_ns / 1e9) + freq_sec
            delay = now_ts - bar_expected_end
            source_tag = "klines推进" if klines_pushed else "壁钟"

            code_key = f"{symbol}:{freq_sec}"
            new_bar = {
                "dt": dt, "open": round(o, 3), "high": round(h, 3),
                "low": round(l, 3), "close": round(cl, 3),
                "vol": vol, "amount": 0,  # 天勤K线无成交额，amount置0（前端期货显成交量vol）
            }
            t_append = time.time()

            last_records = src.last_records(code_key)
            t_step = 0.0
            if not last_records or last_records[0]["dt"] != dt:
                src.append_bar(new_bar, code_key)
                t_step_start = time.time()
                try:
                    _drain_chan(chan)
                except Exception as _e:
                    log.info(f"[{display_key}] step_load 异常: {_e}")
                t_step = time.time() - t_step_start
                t_step_total += t_step
                step_count += 1

            if _SSE_DEBUG:
                log.info(f"[{display_key}] 完成新K线[{source_tag}]: "
                      f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                      f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                      f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                      f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

            # 推送快照（此时 klines[-1] 已推进到 N 周期，快照中自然包含 N 的实时OHLC）
            t_snap_start = time.time()
            try:
                # 增量快照：复用缓存 klines + EMA 状态续算 MACD，避免每根K线全量 O(n) 重建
                update_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                saved_selection_date=saved_selection_date,
                                                prev_klines=(cached_snapshot or {}).get("klines"),
                                                prev_ema_state=(cached_snapshot or {}).get("meta", {}).get("_ema_state"),
                                                is_replay=_is_replay_eff)
                # ★ 用 completed_time + freq_sec 计算下一根K线时间（不用klines[-1]，因为壁钟触发时klines未推进）
                _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                _next_ds = _next_dt.strftime(_get_date_fmt(freq_label))
                _ex = update_data.get('klines', [])
                if not _ex or _ex[-1]['date'] != _next_ds:
                    _next_c = round(cl, 3)
                    _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                        'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0, 'rsi': 0})
                    _inherit_metrics_for_preview_bar(_ex)
                    update_data['meta']['kline_count'] = len(_ex)
                # K线确认后，计算白色横虚线（不在tick推送路径计算）
                _kl_list = chan[kl_type]
                update_data['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _get_date_fmt(freq))
                cached_snapshot = update_data  # ★ 更新缓存
                t_snap = time.time() - t_snap_start
                t_snapshot_total += t_snap
                t_push_start = time.time()
                yield _sse_frame("update", update_data)
                t_push = time.time() - t_push_start
                t_push_total += t_push
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] 推送更新: 快照提取={t_snap:.3f}s "
                          f"SSE写入={t_push:.3f}s "
                          f"(append+step_load={time.time()-t_append:.3f}s)")
            except Exception as _e:
                log.info(f"[{display_key}] 推送异常: {_e}")

    except Exception as e:
        # 打印连接异常后静默结束（错误已在 init 事件载荷中表达）
        log.info(f"[{display_key}] 连接异常: {e}")
    finally:
        # 生成器线程收尾：close() 设置关闭旗（幂等），close_api() 由生成器
        # 线程调用 api.close()——wait_update 已返回、_loop 已停止，串行安全。
        # 顺序：先 close()（通知外部回收已生效）再 close_api()（真正关连接）。
        src.close()
        src.close_api()
        # 清理该连接的K线缓存
        if symbol is not None and freq_sec is not None:
            src.cleanup_records(f"{symbol}:{freq_sec}")
        # 修复：清除线程局部会话（线程池复用前必须清，防串连其它连接缓存）
        session_clear()


def sse_futures_stream_dual(symbol, main_freq="1m", sub_freq=None, start_time=None, sub_start_time=None, end_time=None, reset_selection=False, source=None):
    """期货 SSE 双窗口 · 建会话并返回「逐帧重绑定会话」的生成器。

    与 sse_futures_stream_single 同构：普通函数（非生成器），会话提前建好
    后交给 _per_frame_session 逐帧重绑定。
    reset_selection：切到建议合约的这次连接，上下窗都不继承 CSV 选点（同单窗）。
    """
    src = source if source is not None else CTqSdkSession()
    return _per_frame_session(
        _sse_dual_gen(symbol, main_freq, sub_freq, start_time, sub_start_time, end_time, src,
                      reset_selection=reset_selection), src)


def _sse_dual_gen(symbol, main_freq="1m", sub_freq=None, start_time=None, sub_start_time=None, end_time=None, source=None, reset_selection=False):
    """期货 SSE 双窗口 · 同步生成器（本体；对外经 _per_frame_session 包装）

    事件协议：
    两个独立 CChan 对象、一次连接推送两个周期（下窗先处理——区间套分析
    需先分析次级别）。source 可注入（默认 CTqSdkSession）。
    end_time: 复盘终点（软断开边界），有值时双窗建 chan 均截断到该边界，
              update 循环停在后不推进，也不被实时拉新。
    锁：每连接独立会话；共享缓存经 AppData 按资源加锁
    （见 AppOrch.SHARED_RESOURCE_REGISTRY）。
    """
    log.info("SSE 双窗口连接: symbol=%s main=%s sub=%s",
             symbol, main_freq, sub_freq)

    src = source if source is not None else CTqSdkSession()
    # 入口这次绑定保留：覆盖第一帧与「直接调用本体」场景；跨帧保障由
    # _per_frame_session 负责（starlette 每帧独立线程派发）。
    session_set(src)

    from datetime import datetime

    # 确定周期
    if not sub_freq:
        sub_freq = _FUTURES_DUAL_FREQ_MAP.get(main_freq, "15s")
    main_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(main_freq, 60)
    sub_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(sub_freq, 15)

    display_key = f"{symbol} 双窗口({main_freq}/{sub_freq})"
    if _SSE_DEBUG:
        log.info(f"\n[{display_key}] ═══ SSE双窗口连接建立 ═══")

    try:
        # 期货符号规范化（与单窗同一入口，见 _sse_single_gen 处说明）
        symbol = CTqSdkAPI.normalize_futures_symbol(symbol) or symbol

        src.connect()
        name = _get_futures_name(symbol)
        main_freq_label = main_freq
        sub_freq_label = sub_freq

        # 复盘软断开边界（墙钟比较基准；选点/实时流无 end_time 恒为 None）
        _end_dt = None
        if end_time:
            for _fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                try:
                    _end_dt = datetime.strptime(end_time, _fmt)
                    break
                except ValueError:
                    continue

        # 1. 查询选点状态（选点经 app_data 公共 API 读取）
        # 四期（2026-10-03，对齐股票双窗 §4/§5 一致性原则）：上下窗选点独立——
        #   · 上窗 L = 显式 start_time > CSV(main_freq 列) > 方式A（自身配置根数）；
        #   · 下窗 L = 显式 sub_start_time > CSV(sub_freq 列) > 方式A（自身配置根数）；
        #   · 复盘（end_time）：两窗 L 同规则（前端复盘显式传双 start=各自冻结，
        #     缺失时 CSV 恢复=继承选点，与期货单窗 gen 同构）；
        #   · meta 双字段恒回显 CSV 真值：main_snapshot.saved_selection_date=
        #     CSV(main 列)、sub_snapshot.saved_selection_date=CSV(sub 列)。
        saved_selection_date = ""
        sub_saved_selection_date = ""
        main_start_time = start_time
        _inv_msg = None   # 起止倒挂错误文案（非 None ⇒ 报错中止本次连接，见下）
        # 四类诊断上下文 (freq_sec, start_dt, start_is_selection, end_dt)：
        # 倒挂不等于「请扩大数据范围」，按 L 被谁钉住分别提示（方案 v1.18 §3.6）
        _inv_ctx = None
        try:
            qualified_code = symbol
            col_main = app_data.freq_to_col(main_freq) or ""
            if col_main:
                saved_selection_date = _get_saved_point(qualified_code, main_freq)
            col_sub = app_data.freq_to_col(sub_freq) or ""
            if col_sub:
                sub_saved_selection_date = _get_saved_point(qualified_code, sub_freq)
            # 切到建议合约时两窗都不继承选点：空 start 在协议里同时表示「前端没带
            # L」和「前端不要 L」，而默认语义是「从 CSV 恢复」——继承会让刚切过去
            # 的窗口 L 被该合约旧选点钉住，立刻又报 before_selection（仅提示、无出路）。
            if main_start_time is None and saved_selection_date and not reset_selection:
                main_start_time = saved_selection_date
                log.info(f"[{display_key}] 上窗恢复选点: {saved_selection_date}")
            if sub_start_time is None and sub_saved_selection_date and not reset_selection:
                sub_start_time = sub_saved_selection_date
                log.info(f"[{display_key}] 下窗恢复选点: {sub_saved_selection_date}")
            if reset_selection:
                log.info(f"[{display_key}] 切合约连接：本次上下窗均不继承选点（reset_selection）")
            # 复盘起止倒挂（start ≥ end）→ 记错误文案，稍后报错中止本次连接：
            # **任一窗倒挂即中止**（对齐越界判定口径「任一窗越界即拦/回」），不再
            # 静默丢弃该窗 start、回退默认窗口——与 `_sse_single_gen`、股票
            # `_analyze_stock_internal` 同判据（`>=`）、同文案前缀
            # （需求⑼/⑽ 的兜底归一，见 Docs/选点&复盘方案v1.18.md §3.5）。
            if end_time:
                _e_dt2 = _parse_flex_time(end_time)
                if _e_dt2 is not None:
                    _m_dt = _parse_flex_time(main_start_time) if main_start_time else None
                    if _m_dt is not None and _m_dt >= _e_dt2:
                        _inv_msg = f"上窗复盘起始时间 {main_start_time} 不早于复盘截止时间 {end_time}"
                        _inv_ctx = (main_freq_sec, _m_dt,
                                    _same_minute(_m_dt, saved_selection_date), _e_dt2, "上窗")
                    else:
                        _sb_dt = _parse_flex_time(sub_start_time) if sub_start_time else None
                        if _sb_dt is not None and _sb_dt >= _e_dt2:
                            _inv_msg = f"下窗复盘起始时间 {sub_start_time} 不早于复盘截止时间 {end_time}"
                            _inv_ctx = (sub_freq_sec, _sb_dt,
                                        _same_minute(_sb_dt, sub_saved_selection_date), _e_dt2, "下窗")
        except Exception as _e:
            log.warning(f"[警告] 异常: {type(_e).__name__}: {_e}")
        if _inv_msg:
            _blk = None
            _msg = _inv_msg
            if _inv_ctx is not None:
                _f_sec, _s_dt2, _is_sel2, _e_dt3, _loc = _inv_ctx
                _blk = _classify_futures_replay(
                    src, symbol, _f_sec, _e_dt3, start_dt=_s_dt2,
                    start_is_selection=_is_sel2,
                    window_bars=resolve_lookback_bars(_f_sec))
                if _blk is not None:
                    # 双窗两列各有独立选点：选点类提示必须带「上窗/下窗」定位，
                    # 否则用户不知道清哪一个（合约/10000 根两窗同因，不加前缀）
                    _msg = (f"{_loc}：{_blk.message}"
                            if _blk.code == "before_selection" else _blk.message)
            if _blk is not None:
                log.info(f"[{display_key}] {_blk.code}: {_msg}")
                yield _sse_frame("init", {"error": _msg, "code": _blk.code,
                                          "suggest": _blk.suggest, "symbol": symbol})
                return
            log.info(f"[{display_key}] {_inv_msg}")
            yield _sse_frame("init", {"error": _inv_msg, "symbol": symbol})
            return

        # 下窗取数（四期一致性原则）：下窗窗口独立，方式A = 下窗自己的配置根数
        # （FUTURES_LOOKBACK_CONFIG[sub_freq]，原「上窗配置 N_main 折算」废除）。
        # 下窗 L 已在上方选点状态段解析（显式 > CSV > None=方式A），num_bars
        # 交由 init_chan_symbol 按窗口分支自算（B=墙钟估算[start→now]、A=自身配置），
        # 本层不再传折算基准。
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 下窗独立窗口: start={sub_start_time}, replay={bool(end_time)}")

        # 2. 拉取下窗历史 + chan分析（次级别优先：区间套分析需先分析次级别）
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 拉取下窗({sub_freq})历史K线...")
        # 复盘软断开：end_time 模式下建 chan 前启用买卖点调试，建后复原
        _replay_prev = set_replay_mode(bool(end_time))
        try:
            sub_result = init_chan_symbol(src, symbol, name, sub_freq_sec, sub_freq_label, sub_start_time, end_time)
        except FuturesReplayBlocked as _blk:
            # 同单窗：诊断必须落成错误帧，否则双窗连接直接断流、前端无从提示
            _msg = f"下窗：{_blk.message}" if _blk.code == "before_selection" else _blk.message
            log.info(f"[{display_key}] {_blk.code}: {_msg}")
            yield _sse_frame("init", {"error": _msg, "code": _blk.code,
                                      "suggest": _blk.suggest, "symbol": symbol})
            return
        finally:
            set_replay_mode(_replay_prev)
        if sub_result is None:
            yield _sse_frame("init", {"error": "下窗初始化失败（无数据或网络异常）", "symbol": symbol})
            return
        # init_chan_symbol 返回 (chan, klines, kl_type, records)——第二项是
        # get_kline_serial 的 klines DataFrame 而非 records（本分支未使用）
        sub_chan, _sub_klines, sub_kl_type, _ = sub_result
        sub_kl_type = _get_kl_type(sub_freq)
        # 缓存下窗 CChan 供 /api/dual_zs 访问（语义化漏斗：key 规则内聚数据层）
        app_data.set_futures_sub_chan(symbol, sub_freq, sub_chan)
        # 下窗「对象图锁」：缓存里存的是**活着的 CChan**，实时
        # 循环每根K线 _drain_chan → step_load → do_init 会就地清空重建
        # kl_datas，而 REST 侧读取方拿到指针后在锁外遍历 bi_list。容器锁只
        # 护「取指针」护不住对象图，故取一把按 key 稳定的锁，让重建与遍历
        # 互斥（读取方见 BSPointList.check_nesting_divergence）。
        _sub_chan_lock = app_data.futures_sub_chan_lock(symbol, sub_freq)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 下窗({sub_freq}) chan.py: 合并K线={len(sub_chan[sub_kl_type].lst)}, "
                  f"笔={len(sub_chan[sub_kl_type].bi_list)}, 中枢={len(sub_chan[sub_kl_type].zs_list)}")

        # 3. 拉取上窗历史 + chan分析
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 拉取上窗({main_freq})历史K线...")
        _replay_prev2 = set_replay_mode(bool(end_time))
        try:
            main_result = init_chan_symbol(src, symbol, name, main_freq_sec, main_freq_label, main_start_time, end_time)
        except FuturesReplayBlocked as _blk:
            # 同上/下窗：诊断落成错误帧（上窗被钉住同样要能提示 + 给「切换」）
            _msg = f"上窗：{_blk.message}" if _blk.code == "before_selection" else _blk.message
            log.info(f"[{display_key}] {_blk.code}: {_msg}")
            yield _sse_frame("init", {"error": _msg, "code": _blk.code,
                                      "suggest": _blk.suggest, "symbol": symbol})
            return
        finally:
            set_replay_mode(_replay_prev2)
        if main_result is None:
            yield _sse_frame("init", {"error": "上窗初始化失败（无数据或网络异常）", "symbol": symbol})
            return
        # init_chan_symbol 返回 (chan, klines, kl_type, records)——第二项是
        # get_kline_serial 的 klines DataFrame 而非 records（本分支未使用）
        main_chan, _main_klines, main_kl_type, _main_records = main_result
        # 同单窗：边界落在**数据源**末根之后且非停更（休市日/长假）⇒ 两窗都按「回最新」服务。
        # ⚠️ 判据只取上窗**未截断**末根（_series_last_dt；同一 symbol、同一 end_time，
        # 两窗必然同时命中）。传已截断的 _main_records 自比会把「D=历史休市日」
        # 误判成实时（2026-10-19 P1 回归）。
        _is_replay_eff = bool(end_time) and not _late_boundary_is_live(
            end_time, _end_dt, _series_last_dt(_main_klines))
        main_kl_type = _get_kl_type(main_freq)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 上窗({main_freq}) chan.py: 合并K线={len(main_chan[main_kl_type].lst)}, "
                  f"笔={len(main_chan[main_kl_type].bi_list)}, 中枢={len(main_chan[main_kl_type].zs_list)}")

        # 7. 提取初始快照
        t_snap = time.time()
        main_snapshot = _extract_realtime_snapshot(main_chan, main_kl_type, symbol, name, main_freq_label,
                                                 saved_selection_date=saved_selection_date,
                                                 is_replay=_is_replay_eff)
        sub_snapshot = _extract_realtime_snapshot(sub_chan, sub_kl_type, symbol, name, sub_freq_label,
                                                       saved_selection_date=sub_saved_selection_date,
                                                       klines=None,
                                                       is_replay=_is_replay_eff)
        # 期货双窗口：上窗 bis 的 fx_a_raw_dt/fx_b_raw_dt 是上层K线时间，
        # 需要换算成子级别K线时间，前端 calcRedRange 才能正确匹配
        _futures_red_range(main_snapshot, main_freq_sec, sub_freq_sec, sub_freq)

        # ★ 追加上下窗当前形成中的K线（与单窗口一致），让前端立即看到，且 tick 更新正确的 K 线
        _main_klines_for_init = src.get_kline_serial(symbol, main_freq_sec)
        _sub_klines_for_init = src.get_kline_serial(symbol, sub_freq_sec)
        # 上窗
        # ⚠️ 复盘态（end_time 非空）不追加「数据源最新一根」——同单窗口径：
        #   复盘末根冻结在复盘点，_main_klines_for_init.iloc[-1] 在边界外。
        if (not _is_replay_eff) and _main_klines_for_init is not None and len(_main_klines_for_init) > 0:
            _lr = _main_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
            if _dns is not None:
                _bdt = datetime.fromtimestamp(_dns / 1e9)
                _bds = _bdt.strftime(_get_date_fmt(main_freq))
                _ex = main_snapshot.get('klines', [])
                if not _ex or _ex[-1]['date'] != _bds:
                    _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                        'open': round(float(_lr.get('open', 0) or 0), 3),
                        'high': round(float(_lr.get('high', 0) or 0), 3),
                        'low': round(float(_lr.get('low', 0) or 0), 3),
                        'close': round(float(_lr.get('close', 0) or 0), 3),
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0, 'rsi': 0})
                    _inherit_metrics_for_preview_bar(_ex)
                    main_snapshot['meta']['kline_count'] = len(_ex)
        # 下窗
        # ⚠️ 复盘态（end_time 非空）不追加——同上窗（复盘末根冻结在复盘点）。
        if (not _is_replay_eff) and _sub_klines_for_init is not None and len(_sub_klines_for_init) > 0:
            _lr = _sub_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
            if _dns is not None:
                _bdt = datetime.fromtimestamp(_dns / 1e9)
                _bds = _bdt.strftime(_get_date_fmt(sub_freq))
                _ex = sub_snapshot.get('klines', [])
                if not _ex or _ex[-1]['date'] != _bds:
                    _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                        'open': round(float(_lr.get('open', 0) or 0), 3),
                        'high': round(float(_lr.get('high', 0) or 0), 3),
                        'low': round(float(_lr.get('low', 0) or 0), 3),
                        'close': round(float(_lr.get('close', 0) or 0), 3),
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0, 'rsi': 0})
                    _inherit_metrics_for_preview_bar(_ex)
                    sub_snapshot['meta']['kline_count'] = len(_ex)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 初始快照提取: {time.time()-t_snap:.3f}s")

        # 8. 推送双窗口 init 事件
        init_data = {
            "main": main_snapshot,
            "sub": sub_snapshot,
        }
        yield _sse_frame("init", init_data)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 推送init")

        # 缓存快照用于 tick 路径
        main_cached_snapshot = main_snapshot
        sub_cached_snapshot = sub_snapshot

        # 9. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照（策略同单窗口）
        BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
        t_total = time.time()  # 总耗时起点（用于日志输出）
        if _SSE_DEBUG:
            log.info(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

        # 保存两个窗口的 klines 引用供实时更新使用
        main_klines = src.get_kline_serial(symbol, main_freq_sec)
        sub_klines = src.get_kline_serial(symbol, sub_freq_sec)

        # last_bar_dt_ns: klines[-1] 的时间戳，用于检测 klines 是否推进
        # last_processed_dt_ns: 已处理过的K线时间戳，防止同一根K线被重复处理
        main_last_bar_dt_ns = None
        main_last_processed_dt_ns = None
        sub_last_bar_dt_ns = None
        sub_last_processed_dt_ns = None
        last_debug_print = time.time()

        # 性能统计
        t_wait_total = 0.0
        t_tick_total = 0.0
        t_step_total = 0.0
        t_snapshot_total = 0.0
        t_push_total = 0.0
        loop_count = 0
        tick_count = 0
        step_count = 0
        last_perf_print = time.time()

        # tick 增量状态（按窗口键隔离）：只缓存「到倒数第二根为止」的状态，
        # 供每 tick 续算末根（O(1)）。长度变了或首次进入 ⇒ 重建（正确性兜底）。
        # 两个 dict 分别装 MacdStream 与 RsiStream —— 见 _tick_update_metrics。
        macd_tail_state = {}
        rsi_tail_state = {}

        # ---- 定义单窗口K线处理函数（避免 continue 跳过另一个窗口） ----
        def _process_one_window(klines, chan, kl_type, freq_sec, freq_label,
                                      cached_snapshot, last_bar_dt_ns, last_processed_dt_ns,
                                      is_main, window_label, chan_lock=None):
            """处理单个窗口的K线检测，返回 (updated, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, need_tick)

            chan_lock：该窗口 CChan **对象图**的锁。
            下窗 CChan 经 set_futures_sub_chan 发布进共享缓存供 REST 读取，
            而 `_drain_chan → step_load → do_init` 会**就地清空重建**
            kl_datas；缓存 dict 的锁（_futures_cache_lock）只护住「取指针」
            这一瞬间，护不住读取方拿到指针之后的遍历——正好撞在 do_init
            之后、回填之前就读到空 bi_list。故重建必须与读取方互斥。
            上窗 CChan 不进共享缓存（连接私有），传 None 即可。
            """
            nonlocal last_debug_print, last_perf_print, loop_count, tick_count, step_count
            nonlocal t_wait_total, t_tick_total, t_step_total, t_snapshot_total, t_push_total
            if len(klines) == 0:
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            if dt_ns is None:
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

            # 诊断
            if loop_count == 1 or (loop_count % 50 == 0):
                chan_last_klu = None
                try:
                    chan_kl_list = chan[kl_type]
                    if chan_kl_list.lst:
                        last_klc = chan_kl_list.lst[-1]
                        if last_klc.lst:
                            chan_last_klu = last_klc.lst[-1]
                except Exception as e:
                    log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
                tqsdk_last_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime('%H:%M:%S') if dt_ns else "None"
                chan_last_dt = chan_last_klu.time.to_str()[:16] if chan_last_klu and hasattr(chan_last_klu, 'time') else "None"
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DIAG-{window_label}] 循环#{loop_count} | "
                          f"tqsdk klines[-1]={tqsdk_last_dt} | "
                          f"chan kl_list[-1]={chan_last_dt} | "
                          f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

            # 初始化
            if last_bar_dt_ns is None:
                last_bar_dt_ns = dt_ns
                last_debug_print = now_ts
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DEBUG] 初始化 [{window_label}]: "
                          f"klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

            # 每60秒性能统计
            if now_ts - last_perf_print >= 60.0:
                last_perf_print = now_ts
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [PERF] 循环{loop_count}次 | "
                          f"wait_update总计={t_wait_total:.1f}s | "
                          f"tick推送{tick_count}次总计={t_tick_total:.1f}s | "
                          f"step_load{step_count}次总计={t_step_total:.1f}s | "
                          f"快照提取总计={t_snapshot_total:.1f}s | "
                          f"SSE推送总计={t_push_total:.1f}s")
                t_wait_total = 0.0; t_tick_total = 0.0; t_step_total = 0.0
                t_snapshot_total = 0.0; t_push_total = 0.0
                loop_count = 0; tick_count = 0; step_count = 0

            # 每2秒 DEBUG
            if now_ts - last_debug_print >= 2.0:
                last_debug_print = now_ts
                bar_dt = datetime.fromtimestamp(dt_ns / 1e9)
                lag = now_ts - (dt_ns / 1e9 + freq_sec)
                pushed = (dt_ns != last_bar_dt_ns)
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DEBUG-{window_label}] 壁钟={now.strftime('%H:%M:%S')} "
                          f"klines[-1]={bar_dt.strftime('%H:%M:%S')} "
                          f"过期={lag:+.1f}s 推进={pushed} "
                          f"O={last_row.get('open'):.1f} H={last_row.get('high'):.1f} "
                          f"L={last_row.get('low'):.1f} C={last_row.get('close'):.1f}")

            # --- K线完成检测 ---
            klines_pushed = (dt_ns != last_bar_dt_ns)

            if klines_pushed:
                completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row
                last_bar_dt_ns = dt_ns
            else:
                bar_end_ts = (dt_ns / 1e9) + freq_sec + BAR_COMPLETION_BUFFER
                if now_ts < bar_end_ts:
                    # 周期未结束 → tick更新（更新快照OHLC，稍后统一推送）
                    if cached_snapshot is not None:
                        try:
                            ex = cached_snapshot.get('klines', [])
                            if ex:
                                o = round(float(last_row.get('open', 0) or 0), 3)
                                h = round(float(last_row.get('high', 0) or 0), 3)
                                l = round(float(last_row.get('low', 0) or 0), 3)
                                c = round(float(last_row.get('close', 0) or 0), 3)
                                ex[-1]['open'] = o; ex[-1]['high'] = h
                                ex[-1]['low'] = l; ex[-1]['close'] = c
                                # ★ 实时续算末根（预览bar）的 MACD / RSI（同单窗路径）。
                                #   状态按窗口键隔离：本嵌套函数对上/下窗各调一次，
                                #   共用一份状态会互相覆盖（根数恰相同时还会算错）。
                                _tick_update_metrics(ex, macd_tail_state, rsi_tail_state, freq_label)
                                cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                        except Exception as e:
                            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, True
                # 壁钟到期
                completed_row = last_row

            completed_dt_ns = completed_row.get("datetime")
            if completed_dt_ns is None:
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
            if completed_dt_ns == last_processed_dt_ns:
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
            last_processed_dt_ns = completed_dt_ns

            # 提取 OHLC
            o = float(completed_row.get("open", 0) or 0)
            h = float(completed_row.get("high", 0) or 0)
            l = float(completed_row.get("low", 0) or 0)
            cl = float(completed_row.get("close", 0) or 0)
            vol = int(completed_row.get("volume", 0) or 0)
            h = max(h, o, cl); l = min(l, o, cl)

            dt = datetime.fromtimestamp(completed_dt_ns / 1e9)
            bar_expected_end = (completed_dt_ns / 1e9) + freq_sec
            delay = now_ts - bar_expected_end
            source_tag = "klines推进" if klines_pushed else "壁钟"

            code_key = f"{symbol}:{freq_sec}"
            new_bar = {"dt": dt, "open": round(o, 3), "high": round(h, 3),
                       "low": round(l, 3), "close": round(cl, 3),
                       "vol": vol, "amount": 0}  # 天勤K线无成交额，amount置0（前端期货显成交量vol）

            last_records = src.last_records(code_key)
            updated = False
            t_step = 0.0
            if not last_records or last_records[0]["dt"] != dt:
                src.append_bar(new_bar, code_key)
                t_step_start = time.time()
                try:
                    if chan_lock is not None:
                        # 与 REST 侧读取方的遍历互斥
                        with chan_lock:
                            _drain_chan(chan)
                    else:
                        _drain_chan(chan)
                except Exception as e:
                    log.info(f"[{display_key}] {window_label} step_load 异常: {e}")
                t_step = time.time() - t_step_start
                t_step_total += t_step; step_count += 1
                updated = True
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] 完成新K线[{source_tag}] [{window_label}]: "
                          f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                          f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                          f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

            # 提取完整快照（增量：复用缓存 klines + EMA 状态续算 MACD）
            if updated:
                snapshot = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                           saved_selection_date=saved_selection_date,
                                                           prev_klines=(cached_snapshot or {}).get("klines"),
                                                           prev_ema_state=(cached_snapshot or {}).get("meta", {}).get("_ema_state"),
                                                           is_replay=_is_replay_eff)
                if is_main:
                    _futures_red_range(snapshot, freq_sec, sub_freq_sec, sub_freq)
                _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                _next_ds = _next_dt.strftime(_get_date_fmt(freq_label))
                _ex = snapshot.get('klines', [])
                if not _ex or _ex[-1]['date'] != _next_ds:
                    _next_c = round(cl, 3)
                    _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                        'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0, 'rsi': 0})
                    _inherit_metrics_for_preview_bar(_ex)
                    snapshot['meta']['kline_count'] = len(_ex)
                if is_main:
                    _kl_list = chan[kl_type]
                    snapshot['white_hline'] = _calc_futures_white_hline(_kl_list, main_freq, _get_date_fmt(main_freq))
                cached_snapshot = snapshot

            return updated, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

        # ---- 主循环 ----
        while True:
            try:
                t_wait_start = time.time()
                src.wait_update(time.time() * 1e9 + 100_000_000)
                t_wait = time.time() - t_wait_start
                t_wait_total += t_wait
            except CSSESourceClosed:
                # 数据源正常关闭（仅 Mock/回放源）：等价客户端断开的自然结束
                return
            except Exception as _e:
                log.info(f"[{display_key}] wait_update 异常: {_e}")
                time.sleep(0.5)
                continue

            # 心跳注释帧（客户端断开由生成器线程退出触发 finally 清理）
            yield b": heartbeat\n\n"

            loop_count += 1
            now = datetime.now()
            now_ts = now.timestamp()

            # 复盘软断开：end_time 模式下，一但墙钟越过复盘边界，
            # 即停止推进双窗 K 线/chan/快照（不把图"拉最新"），只保活连接。
            # 判据用 `_is_replay_eff`（同单窗：休市日按实时服务，不冻结）。
            if _is_replay_eff and _end_dt is not None and now > _end_dt:
                continue

            # 处理下窗（次级别优先：区间套分析需先分析次级别）
            # 传下窗对象图锁：该 CChan 已发布进共享缓存，重建须与 REST 读取互斥
            sub_updated, sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns, sub_need_tick = \
                _process_one_window(sub_klines, sub_chan, sub_kl_type, sub_freq_sec, sub_freq_label,
                                          sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns,
                                          is_main=False, window_label="下窗",
                                          chan_lock=_sub_chan_lock)

            # 处理上窗（连接私有，不进共享缓存，无需对象图锁）
            main_updated, main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns, main_need_tick = \
                _process_one_window(main_klines, main_chan, main_kl_type, main_freq_sec, main_freq_label,
                                          main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns,
                                          is_main=True, window_label="上窗")

            # 推送：tick模式或K线完成模式
            if main_need_tick or sub_need_tick:
                # tick推送：统一发送双窗口数据
                t_tick_start = time.time()
                tick_data = {"main": main_cached_snapshot, "sub": sub_cached_snapshot}
                yield _sse_frame("update", tick_data)
                t_tick_total += time.time() - t_tick_start
                tick_count += 1

            if main_updated or sub_updated:
                t_snap_start = time.time()
                update_data = {"main": main_cached_snapshot, "sub": sub_cached_snapshot}
                t_push_start = time.time()
                yield _sse_frame("update", update_data)
                t_push_total += time.time() - t_push_start
                t_snapshot_total += time.time() - t_snap_start
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] 推送更新: 快照提取={t_snapshot_total:.3f}s "
                          f"JSON序列化={(time.time()-t_snap_start)-t_push_total:.3f}s "
                          f"SSE写入={t_push_total:.3f}s")

    except Exception as e:
        # 打印连接异常后静默结束（错误已在 init 事件载荷中表达）
        log.info(f"[{display_key}] 连接异常: {e}")
        traceback.print_exc()
    finally:
        # 生成器线程收尾：close() 设置关闭旗（幂等），close_api() 由生成器
        # 线程调用 api.close()——wait_update 已返回、_loop 已停止，串行安全。
        src.close()
        src.close_api()
        # 清理两个窗口的K线缓存与期货下窗缓存
        if symbol is not None and main_freq_sec is not None:
            src.cleanup_records(f"{symbol}:{main_freq_sec}")
        if symbol is not None and sub_freq_sec is not None:
            src.cleanup_records(f"{symbol}:{sub_freq_sec}")
        try:
            app_data.pop_futures_sub_chan(symbol, sub_freq)  # 语义化漏斗失效（key 规则内聚数据层）
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
        # 修复：清除线程局部会话（线程池复用前必须清，防串连其它连接缓存）
        session_clear()


# ═══════════════════════════════════════════════════════════════════════
# 区域 2 · 期货分析
# ═══════════════════════════════════════════════════════════════════════

def _build_futures_chan(records, symbol, freq_sec, config=None, code=None, src=None):
    """统一期货 CChan 构造：注入数据源 → 周期映射 → 建链 → 全量消费。

    期货各链路唯一的「数据 → CChan」构造入口。单一定义：
      - 周期映射：_get_kl_type_by_sec(freq_sec)
      - 配置：_make_chan_config()
    数据注入经 src.set_data（Session 协议）完成；
    src 为必填（修复：缓存实例化到连接会话，不再回退类级缓存）。
    返回 (chan, kl_type)。缓存逻辑留在各调用方，本函数只负责「数据 → CChan」。
    """
    if src is None:
        raise RuntimeError(
            "[TqSdkAPI] _build_futures_chan 需要数据源 src（SSE 每连接自包含）")
    chan_code = code or f"{symbol}:{freq_sec}"
    src.set_data(records, symbol=chan_code)
    kl_type = _get_kl_type_by_sec(freq_sec)
    config = config or _make_chan_config()
    # 修复：CChan 内部自行实例化 CTqSdkAPI（data_src="custom:..."），
    # 经线程局部绑定本会话缓存，使 set_data 写入与 get_kl_data 读取同源。
    with session_context(src):
        chan = CChan(
            code=chan_code, begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan.step_load():
            pass
    return chan, kl_type


# ═══════════════════════════════════════════════════════════════════════
# 期货手动选点
# ═══════════════════════════════════════════════════════════════════════


def _extract_chan_structure(kl_list, chan, date_fmt):
    """统一缠论结构元素提取：bis / fxs / segs / zs / zs_stars / bsps。

    SSE 实时快照（_extract_realtime_snapshot）与期货分析
    共用同一提取逻辑，保证两条路径字段一致。边界（峰/谷）原始K线时间统一走
    _main_bi_range（与股票路径同源）。

    返回 (bis, fxs, segs, zs_list, zs_stars, bsps)。
    """
    bis = []
    for bi in kl_list.bi_list:
        try:
            direction = "up" if bi.is_up() else "down"
            begin_klu = bi.get_begin_klu()
            end_klu = bi.get_end_klu()
            begin_fx_idx = None
            if hasattr(bi, 'begin_klc') and bi.begin_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.begin_klc:
                        begin_fx_idx = idx
                        break
            end_fx_idx = None
            if hasattr(bi, 'end_klc') and bi.end_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.end_klc:
                        end_fx_idx = idx
                        break
            # 左/右边界原始K线时间（双窗口红框定位）
            # 归一化：与股票/区间套统一走 _main_bi_range（取分型自身
            # 极值（峰/谷）原始K线，allow_partial=True 保留单侧结果——期货分析
            # 路径依赖此行为）。旧内联逻辑（分型左肩/右肩外沿）已注释保留，
            # 如需恢复取消注释即可。
            fx_a_raw_dt = ""
            fx_b_raw_dt = ""
            try:
                shoulder_times = _main_bi_range(bi, date_fmt, allow_partial=True)
                if shoulder_times:
                    fx_a_raw_dt, fx_b_raw_dt, _, _ = shoulder_times
                # ── 旧逻辑（2026-09-02 注释保留，如需恢复取消注释即可）──
                # 左边界 = 起始分型左肩分型的第一根原始K线
                # 右边界 = 结束分型右肩分型的最后一根原始K线
                # begin_klc = getattr(bi, 'begin_klc', None)
                # end_klc = getattr(bi, 'end_klc', None)
                # left_shoulder_klc = begin_klc.pre if begin_klc else None
                # if left_shoulder_klc and left_shoulder_klc.lst:
                #     fx_a_raw_dt = left_shoulder_klc.lst[0].time.toFmtStr(date_fmt)
                # right_shoulder_klc = end_klc.next if end_klc else None
                # if right_shoulder_klc and right_shoulder_klc.lst:
                #     fx_b_raw_dt = right_shoulder_klc.lst[-1].time.toFmtStr(date_fmt)
            except Exception as _e:
                log.warning(f"[警告] 异常: {type(_e).__name__}: {_e}")
            bis.append({
                "idx": getattr(bi, "idx", None),
                "sdt": begin_klu.time.toFmtStr(date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(date_fmt) if end_klu else "",
                "sdt_ts": int(begin_klu.time.ts * 1000) if begin_klu else 0,
                "edt_ts": int(end_klu.time.ts * 1000) if end_klu else 0,
                "direction": direction,
                "fx_a_price": round(bi.get_begin_val(), 2),
                "fx_b_price": round(bi.get_end_val(), 2),
                "high": round(bi._high(), 2),
                "low": round(bi._low(), 2),
                "power": round(abs(bi.get_end_val() - bi.get_begin_val()), 2),
                "is_sure": getattr(bi, 'is_sure', True),
                "end_fx_idx": end_fx_idx,
                "begin_fx_idx": begin_fx_idx,
                "fx_a_raw_dt": fx_a_raw_dt,
                "fx_b_raw_dt": fx_b_raw_dt,
                "fx_a_sub_dt": "",
                "fx_b_sub_dt": "",
            })
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")

    fxs = []
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            peak_klu = klc.get_high_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(date_fmt) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu else 0,
                "mark": "G", "price": klc.high, "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            peak_klu = klc.get_low_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(date_fmt) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu else 0,
                "mark": "D", "price": klc.low, "high": klc.high, "low": klc.low,
            })

    segs = []
    for seg in kl_list.seg_list:
        try:
            direction = "up" if seg.is_up() else "down"
            begin_klu = seg.get_begin_klu()
            end_klu = seg.get_end_klu()
            if direction == "up":
                begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
                end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
            else:
                begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
                end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
            segs.append({
                "sdt": begin_klu.time.toFmtStr(date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(date_fmt) if end_klu else "",
                "direction": direction,
                "begin_price": begin_price, "end_price": end_price,
                "high": round(seg._high(), 2), "low": round(seg._low(), 2),
                "amp": round(seg.amp(), 2),
            })
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_list = []
    for zs in kl_list.zs_list:
        try:
            zs_list.append({
                "sdt": zs.begin.time.toFmtStr(date_fmt) if zs.begin and hasattr(zs.begin, 'time') else "",
                "edt": zs.end.time.toFmtStr(date_fmt) if zs.end and hasattr(zs.end, 'time') else "",
                "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, date_fmt),
                "zg": round(zs.high, 2), "zd": round(zs.low, 2),
                "gg": round(zs.peak_high, 2), "dd": round(zs.peak_low, 2),
                "dir": "up" if (zs.bi_in and zs.bi_in.is_up()) else "down",
            })
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_stars = []
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        star_date = begin_klu.time.toFmtStr(date_fmt)
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "D", "color": "red"})
        else:
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "G", "color": "green"})

    bsps = []
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            klu = bsp.klu
            bsp_ts = 0
            try:
                bsp_ts = int(klu.time.ts * 1000)
            except Exception:
                try:
                    bsp_ts = int(datetime.strptime(klu.time.toFmtStr(date_fmt), date_fmt).timestamp()) * 1000
                except Exception:
                    bsp_ts = 0
            # 分型极值（结构止损参考）：bsp.bi 是终结该买卖点的那一笔，
            # get_end_klu() 返回该笔终点分型的极值 K 线：
            #   底分型（买点）→ 该 K 线.low 为分型最低点；顶分型（卖点）→ .high 为分型最高点。
            f_klu = bsp.bi.get_end_klu()
            bsps.append({
                "date": klu.time.toFmtStr(date_fmt),
                "timestamp": bsp_ts,
                "type": bsp.type2str(), "is_buy": bsp.is_buy,
                "price": round(klu.close, 3),
                "high": round(klu.high, 3),
                "low": round(klu.low, 3),
                "fractal_low": round(f_klu.low, 3),
                "fractal_high": round(f_klu.high, 3),
            })
    except Exception as e:
        log.warning(f"[警告] 异常: {type(e).__name__}: {e}")

    return bis, fxs, segs, zs_list, zs_stars, bsps


def _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None, prev_klines=None, prev_ema_state=None, is_replay=False):
    """从 CChan 对象中提取缠论结构快照，格式与 /api/stock 一致。
    lightweight=True: 仅返回最后一根K线的OHLC变化（周期内tick更新用），不遍历全量结构。
    klines: 天勤实时K线DataFrame（lightweight=True时优先使用，避免chan框架kl_list滞后）。
    prev_klines/prev_ema_state: 增量快照 —— 复用缓存 klines 仅追加新确认K线，
    EMA 状态续算 MACD，避免每根K线全量 O(n) 重建；None 时走原始全量路径。
    is_replay: 复盘模式（软断开 end_time）标记，前端
    「复盘禁选点/禁重置/注记不保存」守卫依赖此标记。
    """
    kl_list = chan[kl_type]
    _date_fmt = _get_date_fmt(freq_label)
    _meta_freq_label = _get_freq_label(freq_label)

    if lightweight:
        # ★ 优先从天勤实时 klines 读取当前形成中K线的OHLC，避免 chan 框架 kl_list 滞后
        if klines is not None and len(klines) > 0:
            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            kline_dt = "?"
            if dt_ns is not None:
                try:
                    kline_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime(_date_fmt)
                except Exception as e:
                    log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
            o = float(last_row.get("open", 0) or 0)
            h = float(last_row.get("high", 0) or 0)
            l = float(last_row.get("low", 0) or 0)
            c = float(last_row.get("close", 0) or 0)
            return {
                "type": "tick",
                "kline": {
                    "date": kline_dt,
                    "open": round(o, 3),
                    "high": round(h, 3),
                    "low": round(l, 3),
                    "close": round(c, 3),
                },
                "meta": {
                    "symbol": symbol, "name": name, "freq": _meta_freq_label,
                    "generated_at": datetime.now().strftime(_date_fmt),
                    "is_realtime": True, "market": "futures",
                },
            }
        # 回退：无 klines 时从 chan 框架读取
        if len(kl_list.lst) == 0:  # type: ignore[union-attr]
            return None
        last_klc = kl_list.lst[-1]  # type: ignore[union-attr]
        if len(last_klc.lst) == 0:  # type: ignore[union-attr]
            return None
        last_klu = last_klc.lst[-1]  # type: ignore[union-attr]
        return {
            "type": "tick",
            "kline": {
                "date": last_klu.time.toFmtStr(_date_fmt),
                "open": round(last_klu.open, 3),
                "high": round(last_klu.high, 3),
                "low": round(last_klu.low, 3),
                "close": round(last_klu.close, 3),
            },
            "meta": {
                "symbol": symbol, "name": name, "freq": _meta_freq_label,
                "generated_at": datetime.now().strftime(_date_fmt),
                "is_realtime": True, "market": "futures",
            },
        }

    # ── klines：增量或全量 ──
    ema_state = None
    if prev_klines is not None:
        klines_out, _changed_idx = _incremental_klines(prev_klines, kl_list, _date_fmt)
        ema_state = _apply_macd_incremental(klines_out, _changed_idx, prev_ema_state)
    else:
        klines_out = []
        for klc in kl_list.lst:  # type: ignore[union-attr]
            for klu in klc.lst:  # type: ignore[union-attr]
                t = klu.time
                klines_out.append({
                    "date": t.toFmtStr(_date_fmt),
                    "timestamp": int(t.ts * 1000),
                    "open": round(klu.open, 3),
                    "high": round(klu.high, 3),
                    "low": round(klu.low, 3),
                    "close": round(klu.close, 3),
                    "vol": int(klu.trade_info.metric.get("volume", 0) or 0),
                    "amount": round(klu.trade_info.metric.get("turnover", 0) or 0, 2),
                })
        ema_state = _apply_macd_full(klines_out)

    # RSI：**快照**路径两条分支（klines 全量 / 增量）都全量重算（见 _apply_rsi_full 的说明）。
    # tick 路径走 `RsiStream` 增量，见 `_tick_update_metrics`（tick 只改末根，不必 O(n)）。
    _apply_rsi_full(klines_out)

    # 统一结构元素提取（bis/fxs/segs/zs/zs_stars/bsps，与期货分析共用）
    bis, fxs, segs, zs_list, zs_stars, bsps = _extract_chan_structure(kl_list, chan, _date_fmt)

    return {
        "meta": {
            "symbol": symbol, "name": name, "freq": _meta_freq_label,
            "kline_count": len(klines_out), "bi_count": len(bis),
            "fx_count": len(fxs), "zs_count": len(zs_list),
            "seg_count": len(segs), "bsp_count": len(bsps),
            "generated_at": datetime.now().strftime(_date_fmt),
            "is_realtime": True, "is_replay": bool(is_replay), "market": "futures",
            "saved_selection_date": saved_selection_date,
            # 增量快照：EMA 状态续算 MACD（内部使用，前端忽略）
            "_ema_state": ema_state,
        },
        "klines": klines_out, "bis": bis, "fxs": fxs, "segs": segs,
        "zs": zs_list, "zs_stars": zs_stars, "bsps": bsps, "white_hline": None,
    }


def _calc_futures_white_hline(kl_list, _freq, date_fmt):
    """计算期货最新笔的白色横虚线数据（与股票逻辑一致）。
    返回 {"price": float, "start_date": str} 或 None。"""
    white_hline = None
    if not kl_list or not kl_list.bi_list:
        return white_hline
    latest_bi = kl_list.bi_list[-1]
    direction = "up" if latest_bi.is_up() else "down"
    end_klc = getattr(latest_bi, 'end_klc', None)
    if end_klc is None:
        return white_hline
    end_fx_idx = None
    for idx, klc in enumerate(kl_list.lst):  # type: ignore[union-attr]
        if klc is end_klc:
            end_fx_idx = idx
            break
    if end_fx_idx is None or end_fx_idx <= 0:
        return white_hline
    left_klc = kl_list.lst[end_fx_idx - 1]  # type: ignore[union-attr]
    klc_high = left_klc.high  # type: ignore[union-attr]
    klc_low = left_klc.low  # type: ignore[union-attr]
    tgt_klu = None
    if hasattr(left_klc, 'lst') and left_klc.lst:  # type: ignore[union-attr]
        for klu in left_klc.lst:  # type: ignore[union-attr]
            if direction == "down" and klu.high == klc_high:
                tgt_klu = klu
                break
            elif direction == "up" and klu.low == klc_low:
                tgt_klu = klu
                break
        if tgt_klu is None:
            tgt_klu = left_klc.lst[0]  # type: ignore[union-attr]
    if tgt_klu:
        ls_date = tgt_klu.time.toFmtStr(date_fmt)
    else:
        ls_date = ""
    if direction == "down":
        white_hline = {"price": round(klc_high, 2), "start_date": ls_date}
    elif direction == "up":
        white_hline = {"price": round(klc_low, 2), "start_date": ls_date}
    return white_hline


# ═══════════════════════════════════════════════════════════════════════
# 期货分析（HTTP 请求模式；与 SSE 实时流共用天勤数据源 + CChan 链路）
# ═══════════════════════════════════════════════════════════════════════


def _apply_macd_full(klines_out):
    """全量重算 MACD（原始路径），返回最后一根K线的 EMA 状态（增量续算用）。"""
    closes = [k["close"] for k in klines_out]
    if len(closes) >= MACD_MIN_BARS:
        ema12 = ema(closes, 12)
        ema26 = ema(closes, 26)
        dif = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        dea = ema(dif, 9)
        macd_vals = [2 * (d - a) for d, a in zip(dif, dea)]
        for i in range(len(klines_out)):
            if i < len(dif):
                klines_out[i]["dif"] = round(dif[i], 4)
                klines_out[i]["dea"] = round(dea[i], 4)
                klines_out[i]["macd"] = round(macd_vals[i], 4)
            else:
                klines_out[i]["dif"] = 0; klines_out[i]["dea"] = 0; klines_out[i]["macd"] = 0
        return {"ema12": ema12[-1], "ema26": ema26[-1], "dea": dea[-1]}
    for k in klines_out:
        k["dif"] = 0; k["dea"] = 0; k["macd"] = 0
    return None


def _apply_rsi_full(klines_out):
    """全量重算 RSI，就地写入每根K线的 "rsi" 字段（口径同 AppUtils.calculate_rsi）。

    2026-10-10 更正（原结论有误，保留痕迹）：这里原先写「⚠ 刻意不做增量版 ——
    RSI 是 Wilder 递推、状态含 up/down 两条**逐根全长**序列，收益不值」。
    **这个判断是错的**：`AppUtils.calculate_rsi` 里那三个 `diff` / `ups` / `downs`
    列表都只是**局部变量**（播种期过后 `diff` 永不回看，`ups`/`downs` 每次只取 `[-1]`），
    递推真正依赖的只有 4 组状态量（`diff_len` + `up_sum` + `down_sum` + 上一根的
    `up`/`down`）—— 与 MACD 增量的 3 个标量（ema12/ema26/dea）同量级，都不随 n 增长。
    故已新增 `AppUtils.RsiStream`（O(1) 增量，与全量**逐位相等**），
    **tick 路径已切过去**（每 tick 只续算末根，不再 O(n) 全量）。

    本函数仍是**快照路径**的全量入口：快照本来就是整窗重建，省不掉那一次 O(n)，
    走全量反而少一条状态存取路径。两条路径的口径仍只有 `calculate_rsi` 一份 ——
    由 `Test/test_bottom_slots.py` 的「RSI 增量 ≡ 全量」护栏逐位钉住。
    """
    closes = [k["close"] for k in klines_out]
    rsi_vals = calculate_rsi(closes)
    for i in range(len(klines_out)):
        klines_out[i]["rsi"] = round(rsi_vals[i], 4) if i < len(rsi_vals) else 0


def _apply_macd_incremental(klines_out, changed_idx, prev_ema_state):
    """从 changed_idx 起增量重算 MACD（EMA 状态续算，O(1) 每根K线）。

    prev_ema_state 为 changed_idx-1 处K线的 EMA 状态（由上次快照 meta 携带）。
    返回更新后的 EMA 状态；状态缺失时回退全量重算。
    """
    if not klines_out:
        return prev_ema_state
    if prev_ema_state is None:
        return _apply_macd_full(klines_out)
    # 从 changed_idx 起逐根续算
    k12 = 2.0 / 13.0; k26 = 2.0 / 27.0; k9 = 2.0 / 10.0
    state = dict(prev_ema_state)
    for k in klines_out[changed_idx:]:
        c = k["close"]
        ema12 = c * k12 + state["ema12"] * (1 - k12)
        ema26 = c * k26 + state["ema26"] * (1 - k26)
        dif = ema12 - ema26
        dea = dif * k9 + state["dea"] * (1 - k9)
        k["dif"] = round(dif, 4)
        k["dea"] = round(dea, 4)
        k["macd"] = round(2 * (dif - dea), 4)
        state = {"ema12": ema12, "ema26": ema26, "dea": dea}
    return state


def _tick_update_metrics(ex, macd_states, rsi_states, key):
    """tick 路径：续算**末根**（预览bar）的 dif/dea/macd/rsi —— 末根 O(1)，前 n-1 根不动。

    为什么末根可以只算一根：tick 只改末根的 OHLC，前面各根的 close 没变 ⇒ 它们的指标值
    也不会变。早前这里每 tick 把 n 根重算一遍（MACD 还是 `ema(closes,12)` +
    `ema(closes,26)` + `ema(difs,9)` **三趟** O(n)），实测 n=6000 时 tick 单次 1.41 ms
    里 MACD 占 ~100% —— RSI 改增量后 MACD 成了唯一热点，故一并按同一套路切增量。

    **例外（重建分支，2026-10-10 补）**：状态需要重建时（`"n" != len(ex)-1`，一根K线周期
    只发生一次），顺带把**前 n-1 根**的 MACD 写成与 `_apply_macd_full` 同一批值。原因是
    快照可能生成于「整窗不足门槛」的时刻（那时整窗被置 0），而此刻窗口可能已越过门槛
    （典型：25 根确认K线 + 1 根预览bar = 26）⇒ 若只写末根，就会留下「前 25 根全 0 +
    末根有值」的孤立点。旧 tick 因**循环重写全窗**而没有这个现象，切增量后才会显出来。
    重建分支本来就是 O(n)，顺带写一遍不改变量级；且稳态下这些根的值与流式算出的逐位
    相同（`Test/test_sse_incremental.py` ⑤a），故对已填好的窗口等价于幂等重写。
    门槛未开时同分支同步置 0，令「tick 后整窗 ≡ `_apply_macd_full`」**无条件**成立
    （⑤f 用 `==` 钉住）。

    状态缓存：两个 dict 形如 {key: {"n": 状态已喂根数, "s": 状态对象}}
      · `key` 由调用方给定（单窗 `"main"` / 双窗 `freq_label`）—— **上/下窗必须分开**，
        否则两个窗口根数不同时会互相覆盖，根数恰相同时还会算错；
      · `"n"` 必须等于 `len(ex) - 1`，否则说明序列已经变了（新根推进 / 首次进入），
        重建一次（O(n)，一根K线周期内只发生一次）。

    **取入口径（本轮统一）**：全程用**未取整**的 `dif` 递推 `dea`，最后才 `round` 落到
    字段上 —— 与 `_apply_macd_full` / `_apply_macd_incremental` 同源。早前 tick 路径先把
    `dif` round 到 4 位再喂给 `dea`（`difs = [ex[i]['dif'] ...]`），导致 tick 推的值与
    快照推的值在末位差 1e-4（600 根实测 `macd` 有 253 根不相等）—— 已统一，由
    `Test/test_sse_incremental.py` 的「MACD tick ≡ 快照」护栏逐位钉住。
    """
    if not ex:
        return
    n = len(ex)
    last_close = ex[-1]["close"]
    # 注意：**不要**在这里 `[k["close"] for k in ex]` 建全表 —— 那是 O(n)，
    # 每 tick 建一次就把增量的收益全吃回去（实测 n=6000 会从 0.0002 ms 涨到 0.18 ms）。
    # 只有状态需要重建时才遍历 ex[:-1]（一根K线周期内只发生一次）。

    # ── MACD（3 标量：ema12 / ema26 / dea）
    # 整窗门槛：与 `_apply_macd_full` 同源同义 —— 整窗不足 MACD_MIN_BARS 根 ⇒ 整窗置 0。
    # 只判一次、重建分支与末根分支**共用**，避免门槛在两处各写一遍（改一处漏一处）。
    gate_open = n >= MACD_MIN_BARS
    ms = macd_states.get(key)
    if ms is None or ms["n"] != n - 1:
        # 重建分支（每根K线周期一次，本来就是 O(n)）：顺带把前 n-1 根写成与
        # `_apply_macd_full` 同一批值 —— 否则「快照在整窗不足门槛时置的 0」会与
        # 「此刻已越过门槛的末根值」并存（窗口恰好 26 根时 = 一条孤立点）。
        # 稳态下这些根的值与流式算出的逐位相同 ⇒ 等价于幂等重写（见函数 docstring）。
        s = MacdStream()
        for k in ex[:-1]:
            dif, dea, macd = s.feed(k["close"])
            if gate_open:
                k["dif"] = round(dif, 4)
                k["dea"] = round(dea, 4)
                k["macd"] = round(macd, 4)
            else:
                k["dif"] = 0
                k["dea"] = 0
                k["macd"] = 0
        ms = {"n": n - 1, "s": s.copy()}
        macd_states[key] = ms
    if not gate_open:
        ex[-1]["dif"] = 0
        ex[-1]["dea"] = 0
        ex[-1]["macd"] = 0
    else:
        dif, dea, macd = ms["s"].copy().feed(last_close)
        ex[-1]["dif"] = round(dif, 4)
        ex[-1]["dea"] = round(dea, 4)
        ex[-1]["macd"] = round(macd, 4)

    # ── RSI（4 组状态量：见 AppUtils.RsiStream）
    rs = rsi_states.get(key)
    if rs is None or rs["n"] != n - 1:
        s = RsiStream(RSI_CYCLE)
        for k in ex[:-1]:
            s.feed(k["close"])
        rs = {"n": n - 1, "s": s.copy()}
        rsi_states[key] = rs
    ex[-1]["rsi"] = round(rs["s"].copy().feed(last_close), 4)


def _incremental_klines(prev_klines, kl_list, date_fmt):
    """增量更新 klines：复用缓存，仅追加新确认K线 / 修正尾部。

    prev_klines 可含末尾预览bar（调用方追加，date 为下一根形成K线），
    本函数剥离预览bar后返回纯确认K线，调用方再追加新预览bar。

    返回 (klines_out, changed_idx)：
      changed_idx = 需重算 MACD 的起始下标（新追加/合并修正的最后一根）。
    """
    if not kl_list.lst or not kl_list.lst[-1].lst:
        return list(prev_klines), len(prev_klines)
    last_klu = kl_list.lst[-1].lst[-1]
    last_dt_str = last_klu.time.toFmtStr(date_fmt)

    out = list(prev_klines)
    # 剥离预览bar（末尾条目日期 ≠ chan 最后一根确认K线日期）
    if out and out[-1]["date"] != last_dt_str:
        out.pop()
    if out and out[-1]["date"] == last_dt_str:
        # 尾部K线未变：仅更新 OHLC（合并场景）
        out[-1].update({
            "open": round(last_klu.open, 3),
            "high": round(last_klu.high, 3),
            "low": round(last_klu.low, 3),
            "close": round(last_klu.close, 3),
            "vol": int(last_klu.trade_info.metric.get("volume", 0) or 0),
            "amount": round(last_klu.trade_info.metric.get("turnover", 0) or 0, 2),
        })
        return out, len(out) - 1
    # 新确认K线：追加
    out.append({
        "date": last_dt_str,
        "timestamp": int(last_klu.time.ts * 1000),
        "open": round(last_klu.open, 3),
        "high": round(last_klu.high, 3),
        "low": round(last_klu.low, 3),
        "close": round(last_klu.close, 3),
        "vol": int(last_klu.trade_info.metric.get("volume", 0) or 0),
        "amount": round(last_klu.trade_info.metric.get("turnover", 0) or 0, 2),
    })
    return out, len(out) - 1


# ═══════════════════════════════════════════════════════════════════════
# 区域 3 · 期货选点
# ═══════════════════════════════════════════════════════════════════════

def futures_manual_select_point(symbol, freq="15s", bi_idx="0", end_date=None, start_time=None):
    """
    期货期指手选进入段：与股票 stock_manual_select_point 逻辑一致。
    创建临时 TqApi → 拉取全量历史 → 找到左肩时间T → 保存CSV →
    创建新 TqApi → 从T重新拉取 → 创建新CChan → 返回完整快照。

    end_date（复盘态选点，2026-10-03 二期）：当前复盘点——重建拉取按
    [T, end_date] 截断（改L不改R），响应 is_replay=True 保持复盘态。
    start_time：前端**当前视图的左边界 L**，定位窗口的唯一可靠来源
    （前端视图的 L 是冻结值，§2.1 改 R 不改 L；后端自己推导在「复盘态且
    该周期无选点」时会落到另一个窗口）。缺省时退回 CSV → 方式C。
    """
    import time

    # 期货符号规范化（与单窗/双窗同一入口）
    symbol = CTqSdkAPI.normalize_futures_symbol(symbol) or symbol

    freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 15)
    freq_label = freq
    freq_cn = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{freq_cn}"
    name = _get_futures_name(symbol)
    target_bi_idx = int(bi_idx)

    src = None
    src2 = None
    try:
        t_conn = time.time()
        src = CTqSdkSession()
        src.connect()
        log.info(f"[{display_key}] ⓪ 临时连接天勤(选点): 耗时 {time.time()-t_conn:.1f}s")

        # 定位窗口必须与前端当前视图**同源**：前端发来的 bi_idx 属于**当前视图**
        # 的笔列表，后端只能在同一个窗口（同一个 L）上取 bi_list[bi_idx]。
        # L 的唯一可靠来源 = 前端显式传入的 start_time（= 前端 chartData.klines[0]）：
        #   · 它是**冻结**值（§2.1 改 R 不改 L）——冷启动 = 方式C 的实时窗口 L，
        #     复盘原样带过来，选点后又变成新选点 T；后端自己推导只能对上两种情形
        #     （CSV 有值 / 非复盘的方式C），复盘态且无 CSV 时「从复盘点往前推 N 根」
        #     ≠ 视图 L。实测 2026-10-09 IM 1m 复盘到 10/08：视图 L=09/24 10:50
        #     （80 笔）vs 推导窗口 L=09/23 14:56（86 笔），双击 10/08 14:28
        #     （前端 bi_idx=79）左肩错到 10/08 11:24（应得 10/08 14:27）。
        # 取数优先级与 SSE 生成器（_sse_single_gen / _sse_dual_gen）同构：
        # 显式 start_time > CSV（F5 刷新后前端内存丢失，CSV 是 SSOT）> 方式C 默认；
        # start 传值时 init 内部按墙钟估算拉取（B 分支 / start+end 组合分支）。
        locate_start = start_time or None
        if locate_start is None:
            _col_loc = app_data.freq_to_col(freq) or ""
            if _col_loc:
                locate_start = _get_saved_point(symbol, freq) or None
        loc_result = init_chan_symbol(src, symbol, name, freq_sec, freq_label, locate_start, end_date)
        if loc_result is None:
            raise DataFetchError("选点定位失败（无数据或网络异常）")
        chan, kl_type = loc_result[0], loc_result[2]

        kl_list = chan[kl_type]
        bi_list = kl_list.bi_list

        if target_bi_idx < 0 or target_bi_idx >= len(bi_list):
            raise AnalysisError(f"笔索引 {bi_idx} 越界，笔总数 {len(bi_list)}")

        # 检查选点后至少需要4笔
        remaining_bis = len(bi_list) - target_bi_idx - 1
        if remaining_bis < 4:
            raise AnalysisError(f"选点之后仅剩 {remaining_bis} 笔，至少需要4笔才能构建中枢，请重新选点")

        # Step 2: 找到左肩时间T
        start_time = _find_left_shoulder_time(kl_list, bi_list, target_bi_idx, freq)
        if start_time is None:
            raise AnalysisError("无法定位左肩K线时间，请重试")

        log.info(f"[{display_key}] 选点左肩时间: {start_time}")

        # Step 3: 保存选点到CSV（save_point_time 内部已在 _saved_point_lock 内
        # 同步更新内存态与落盘，调用方不再锁外直写内存态）
        app_data.save_point_time(symbol, name, freq, start_time)

        # Step 4: 关闭旧TqApi，创建新TqApi，从T重新拉取
        if src is not None:
            try:
                src.close()
                src.close_api()
            except Exception as e:
                log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
            src = None

        t_conn2 = time.time()
        src2 = CTqSdkSession()
        src2.connect()
        log.info(f"[{display_key}] ⓪ 重新连接天勤(选点后): 耗时 {time.time()-t_conn2:.1f}s")

        # 选点后重建走窗口根数计算（对齐单窗 gen）：B 模式 [T, 最新] 全量不截断、
        # 复盘态组合模式 [T, end] 亦不截断。不传 num_bars 会退回默认配置根数并在
        # fetch_kline 内被截成末 N 根，与「选点不截断」语义相反。
        _sel_fetch, _sel_base = _futures_window_fetch_bars(
            freq_sec, start_time=start_time, end_time=end_date)
        records2 = src2.fetch_kline(symbol, freq_sec=freq_sec,
                                    display_key=display_key, start_time=start_time,
                                    num_bars=_sel_fetch)
        if end_date:
            # 复盘态选点：R 保持复盘点——截去 end_date 之后的K线（改L不改R）
            records2 = _truncate_records_by_end(records2, end_date, freq_sec)
            # 组合模式 base_bars=None（不做根数截断）；base_bars 有值时才截末 N 根。
            if _sel_base and len(records2) > _sel_base:
                records2 = records2[-_sel_base:]
        if len(records2) < 5:
            raise DataFetchError(f"选点后K线数据不足: 仅{len(records2)}条")

        # 注入数据源 + 创建新 CChan（统一走 _build_futures_chan；config 省略即由
        # 其内部按 _make_chan_config() 取默认——不再向上游要一个已不存在的局部变量）
        # 数据注入经 src2.set_data（Session 协议），不落类级缓存
        chan2, _ = _build_futures_chan(records2, symbol, freq_sec, src=src2)

        # Step 5: 提取快照并返回（复盘态选点 is_replay=True，前端保持复盘 UI）
        result = _extract_realtime_snapshot(chan2, kl_type, symbol, name, freq_label,
                                            saved_selection_date=start_time,
                                            is_replay=bool(end_date))
        # 计算白色横虚线
        _kl_list = chan2[kl_type]
        _date_fmt = _get_date_fmt(freq)
        result['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _date_fmt)
        log.info(f"[{display_key}] 选点完成: {len(result['klines'])}K线, {result['meta']['bi_count']}笔, {result['meta']['zs_count']}中枢")
        return result

    except AppError:
        # 领域异常原样上抛（API 层统一中间件捕获，不二次包装）
        raise
    except Exception as e:
        import traceback
        log.info(f"[{display_key}] 选点异常: {e}")
        traceback.print_exc()
        raise AnalysisError(f"选点失败: {str(e)}") from e
    finally:
        if src is not None:
            try:
                src.close()
                src.close_api()
            except Exception as e:
                log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
        if src2 is not None:
            try:
                src2.close()
                src2.close_api()
            except Exception as e:
                log.warning(f"[警告] 异常: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 区域 4 · 市场/代码/周期查询
# ═══════════════════════════════════════════════════════════════════════

def get_futures_aliases():
    """期货别名映射**全表**（经 CTqSdkAPI 查询）。

    单一事实源 = `DataAPI/TqSdkAPI.FUTURES_ALIASES`（手写维护，**16 个品种 /
    17 条别名** = 16 个唯一合约 + 1 组重复写法：`TA`/`PTA` 同指 `KQ.m@CZCE.TA`）。

    用户拍板（口径变更，见实施计划 附录 D.8）：
      · 本表是**"能看行情"的口径**（输入能解析出一个代码），**不等于"可交易范围"**；
      · 用户点名把本表**收窄到 16 个品种**（原为 83 条「人工累加」的全表）——
        收窄后 RB/M/SC 等**仍在**表内，而 T/TF/NI/SR/PP/Y/PG… 等**移出**者
        不再可搜可看。这是预期效果，不是 bug。

    两处消费（解耦后各自独立）：
      · 搜索漏斗 / SSE 别名解析 —— 走本表，**不做品种过滤**（看行情不设限）；
      · 自动下单开关 —— 由 `AppTrader.check_symbol_allowed` 单独拦品种（白名单 8 个），
        引擎侧 `Product.assert_product_allowed` 是权威闸门。

    因此**不要再**在搜索/解析侧做品种过滤：那会让"看行情"被"能不能下单"牵连
    （反例见 2026-09-14 当天先做后撤的 `get_tradable_futures_aliases`）。
    要缩"能看什么"，改的是 `TqSdkAPI.FUTURES_ALIASES` 本身（+ 同步前端
    `FUTURES_ALIAS_KEYS`），**不是**在这里加过滤。
    """
    if CTqSdkAPI is None:
        return {}
    return CTqSdkAPI.FUTURES_ALIASES


def get_futures_name(full_code):
    """期货名称"""
    if _get_futures_name:
        return _get_futures_name(full_code)
    return full_code


def tq_available():
    """天勤数据源是否可用"""
    return TQ_AVAILABLE


def get_futures_freqs():
    """期货可用周期列表（经 CTqSdkAPI 查询）"""
    try:
        if CTqSdkAPI is None:
            return {"supported_freqs": [], "disabled_freqs": []}
        return {"supported_freqs": CTqSdkAPI.SUPPORTED_FREQS,
                "disabled_freqs": CTqSdkAPI.DISABLED_FREQS}
    except ImportError:
        return {"supported_freqs": [], "disabled_freqs": []}


def get_futures_freq_sec_map():
    """期货周期→秒数映射（/api/health 的单一事实源出口，不直连 CTqSdkAPI）"""
    try:
        if CTqSdkAPI is None:
            return {}
        return dict(CTqSdkAPI.FREQ_SEC_MAP)
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════
# 区域 5 · 期货退出清理
# ═══════════════════════════════════════════════════════════════════════

def _cleanup_all_futures_data():
    """期货切到股票时清理期货数据：K线缓存、分析缓存、选点记录

    【· P0 修复】作用域纪律：本函数是**全局清扫**，只在
    「没有任何页面正在看期货」时才成立。

    原实现无条件清扫，导致一个页面切走期货时，其余期货页面的 SSE 会话
    被 close_all() 掐断、K线记录缓存被清空、下窗 chan 与选点被抹掉——
    这是**确定性**故障（每次必错），不是概率性竞争。

    修复思路不是加锁（锁解决不了作用域错配，见指导书维度 0.2 模式 B），
    而是**收窄作用域**：
      · 会话自有的资源（TqApi 连接、本连接 K线记录、本连接下窗 chan）
        由**该会话自己的生成器 finally** 回收——单窗 :660、双窗 :1181-1185
        已具备，无需外部代劳；
      · 本函数只负责**无人持有的孤儿状态**清扫，并以此处的
        active_session_count() 为守卫。

    因此守卫命中（仍有活跃会话）时**直接返回**是正确的：那些会话会在
    各自 finally 中自我清理，全局清扫只会误伤它们。

    返回：(是否执行了清扫, 活跃会话数)  —— 供 API 层观测与前端提示。

    注意：本函数**不得**调用 gc.collect()。SSE 期货流关闭时 TqApi 内部
    协程可能仍处于挂起态（tqsdk 在 Python 3.12+ 的已知问题，见官方
    issue #442），若在此强制 GC 会触发「Task was destroyed but it is
    pending!」+「Event loop is closed」错误级联（实测日志）。TqApi 的
    生命周期由 SSE 流 finally 块 src.close()→api.close() 负责，内存由
    Python 引用计数 + 自动 GC 回收即可。
    """
    active = active_session_count()
    if active > 0:
        # 仍有页面在看期货（含刚发起本请求、其 SSE 尚未走完 finally 的那一页）：
        # 全局清扫会误伤它们，跳过。它们各自的 finally 会自行回收。
        log.info(f"[清理] 跳过期货全局清扫：仍有 {active} 个活跃期货 SSE 会话，"
                 f"其资源由各生成器 finally 自行回收")
        return False, active

    # 1. 清空 CTqSdkAPI 的K线缓存（已改为遍历活跃会话；此处无活跃会话，仅兜底）
    if CTqSdkAPI is not None:
        CTqSdkAPI.clear_all_cache()
        log.info("[清理] 已清空期货K线缓存")

    # 2. 清空选点记录中的期货条目（key以KQ.开头，经 app_data 加锁删除）
    #    注：只清内存态缓存，CSV 持久化不受影响，下次访问自动重载
    removed = app_data.clear_saved_points_by_prefix("KQ.")
    if removed:
        log.info(f"[清理] 已清除 {removed} 条期货选点记录")

    # 3. 清空期货分析缓存（双窗下窗 chan 残留：不清空则切回后
    #    check_nesting_divergence 经 futures_cache_get 读到过期中间状态）
    app_data.futures_cache_clear()
    log.info("[清理] 已清空期货分析缓存")
    return True, 0


def futures_cleanup():
    """清理期货数据（孤儿清扫；有活跃会话时自动跳过）

    返回 dict：{"swept": bool, "active_sessions": int}
      swept=True  —— 已执行全局清扫（当时无活跃期货会话）
      swept=False —— 跳过清扫（仍有页面在看期货，避免误伤，见）
    """
    swept, active = _cleanup_all_futures_data()
    return {"swept": swept, "active_sessions": active}





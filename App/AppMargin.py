# -*- coding: utf-8 -*-
"""
App/AppMargin.py —— 两融余额功能域
=========================================================================
点击页面右上角「两融余额」按钮后的操作：读取全 A 股（沪市 + 深市 + 北交所）
每日融资融券余额，按 K 线页面「视口」日期区间绘制余额曲线。

数据源（在线，无本地兜底）：
  - eltdx F10 通道（7615 TQLEX，HTTP POST），项目已要求 eltdx>=3.3.2。
  - F10Client.market_report("000001", "market_margin_summary")：
    Entry CWServ.skef10_dpzx_rzrq，Params ["002", "000001"]，
    一次调用返回全历史「大盘两融日序列汇总」（实测 8733 行、2010-03-31 起）。
  - 每行是一个「分市场 × 交易日」：N001 交易日、N010 市场标记、
    N005 该市场两融余额（元）= N003 融资余额 + N004 融券余额。
  - 三市标记：012001 沪 / 012002 深 / 012046 北。上游文档另称有 total
    汇总行，实测 0 条；本模块只认上述三个标记并**按日期求和**，即便日后
    服务端补了 total 也不会被重复计入。
  - 北交所自 2023-02-13 才有数据，更早的日期只有沪深两行，按「有则加」处理。
  - 三市均为 T+1 存量（次一交易日开市前披露），无盘中实时值。
  - 单位：接口为「元」，本模块对外统一换算为「亿元」。

口径交叉验证（2026-11-10 实测）：三市自算合计与第三方同口径的
三市合计报表在 2026-11-02 ~ 11-09 六个交易日逐日零差
（11-09 均为 22,957.84 亿元）。该第三方报表仅作对照，本模块不再请求它。

视口边界映射：前端把 K 线页面「视口」最左/最右 K 线日期作为
start_date / end_date 传入；数据源是全历史，本模块在内存里按闭区间收口。

指标计算（区间内）：
  - 峰值余额（最大）
  - 当前余额（最右）
  - 缩至峰值占比 = 当前/峰值 * 100（与市场量能同口径）

无持久化：面板关闭即释放数据，本模块不存任何状态、不落盘、不缓存，
也不复用 F10Client 实例（每次请求新建，避免引入共享连接状态）。

锁：本模块**无锁**。call_margin 只读在线接口并算统计，不进 CChan 缠论引擎、
不写任何共享状态。
"""
import math
import time

# 领域异常（数据源失败 → DataFetchError）
from App.AppErrors import DataFetchError
from App.AppLog import get_logger

log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════════════

_MARGIN_SECTION = "market_margin_summary"
_MARGIN_CODE = "000001"
_F10_TIMEOUT_SEC = 15

_DATE_FIELD = "N001"
# N005 = 两融余额（元，融资 N003 + 融券 N004）；绝不取 N003（仅融资余额）
_BALANCE_FIELD = "N005"
_FINANCING_ONLY_FIELD = "N003"
_MARKET_FIELD = "N010"
# 沪 / 深 / 北；只认这三个标记，其它（含文档提到的 total）一律不计入
_MARKET_TAGS = ("012001", "012002", "012046")


def _norm_date(s: str) -> str:
    """把前端传入的日期统一为连字符格式（YYYY/MM/DD → YYYY-MM-DD）。"""
    return (s or "").replace("/", "-")


def _fmt_yi(amount_yuan: float) -> float:
    """元 → 亿元（保留 2 位小数，前端展示用）"""
    return round(amount_yuan / 1e8, 2)


def _fetch_f10_rows():
    """单次拉取全历史「分市场 × 交易日」行；失败一律抛 DataFetchError。

    eltdx 采用函数内延迟导入：模块顶层不依赖 eltdx，缺依赖时也能给出
    可读的 DataFetchError，而不是让整个 FrontAPI 在 import 阶段崩掉。
    """
    try:
        from eltdx import F10Client
        client = F10Client(timeout=_F10_TIMEOUT_SEC)
        resp = client.market_report(_MARGIN_CODE, _MARGIN_SECTION)
        rows = list(getattr(resp, "rows", None) or [])
    except Exception as exc:
        raise DataFetchError(f"两融余额数据源请求失败: {exc}") from exc
    if not rows:
        raise DataFetchError("两融余额数据源返回空序列")
    return rows


def _parse_rows(rows):
    """分市场行 → [(YYYY-MM-DD, 三市合计元), ...]，按日期升序。

    - 同一 (日期, 市场) 重复行只保留最后一条，避免重复累加；
    - 坏值（None / 字符串 / 负数 / NaN）、未知市场标记只丢该行；
    - 缺失的市场按「有则加」，不补零、不丢弃该交易日。
    """
    per_market = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_raw = str(row.get(_DATE_FIELD) or "")[:10].replace("/", "-")
        tag = str(row.get(_MARKET_FIELD) or "")
        value = row.get(_BALANCE_FIELD)
        if len(date_raw) != 10 or tag not in _MARKET_TAGS:
            continue
        if not isinstance(value, (int, float)):
            continue
        fv = float(value)
        if not math.isfinite(fv) or fv <= 0:
            continue
        per_market[(date_raw, tag)] = fv
    per_day = {}
    for (date_raw, _tag), fv in per_market.items():
        per_day[date_raw] = per_day.get(date_raw, 0.0) + fv
    return sorted(per_day.items())


def fetch_margin_rows(start_date: str, end_date: str):
    """按日期区间取全 A 股两融余额，返回 [(YYYY-MM-DD, 余额元), ...]（日期升序）。

    单次 F10 调用拿全历史，在内存里按闭区间过滤；不缓存、不落盘、不做任何兜底。
    """
    rows = _parse_rows(_fetch_f10_rows())
    return [(d, v) for d, v in rows if start_date <= d <= end_date]


def call_margin(start_date: str, end_date: str):
    """两融余额数据（同步入口，REST 路由经 run_in_threadpool 调用）

    - 数据源为 eltdx F10「大盘两融日序列汇总」，按日期把沪深北三市求和，
      无本地兜底
    - 无锁：只读在线接口、不进缠论引擎、不写共享状态
    - 入参前端日期为斜杠格式（%Y/%m/%d，全站 K 线契约）；内部转连字符比较，
      返回的 dates/peak_date/current_date 也统一为斜杠格式以便前端横轴对齐
    - 返回 {dates, balances(亿元), stats:{peak, peak_date, current,
      current_date, peak_ratio}}；数据源失败抛 DataFetchError
    """
    start_date = _norm_date(start_date)
    end_date = _norm_date(end_date)
    log.debug(f"[api] /api/margin/read 开始: start_date={start_date!r} end_date={end_date!r}")
    t0 = time.time()
    rows = fetch_margin_rows(start_date, end_date)

    # fetch_margin_rows 已按区间收口，此处再做一次闭区间过滤（防口径漂移）
    dates = sorted(d for d, _v in rows if start_date <= d <= end_date)
    by_date = dict(rows)
    balances_yuan = [by_date[d] for d in dates]

    if not dates:
        log.debug(f"[api] /api/margin/read 完成: 区间无数据 "
                  f"({start_date}~{end_date}) 耗时 {time.time() - t0:.2f}s")
        return {
            "dates": [], "balances": [],
            "stats": {"peak": None, "peak_date": None,
                      "current": None, "current_date": None,
                      "peak_ratio": None},
        }

    peak_idx = max(range(len(balances_yuan)), key=lambda i: balances_yuan[i])
    peak = balances_yuan[peak_idx]
    current = balances_yuan[-1]
    peak_ratio = round(current / peak * 100.0, 2) if peak > 0 else None

    # 对外统一斜杠格式（%Y/%m/%d），与全站 K 线日期契约一致
    out_dates = [d.replace("-", "/") for d in dates]
    result = {
        "dates": out_dates,
        "balances": [_fmt_yi(v) for v in balances_yuan],
        "stats": {
            "peak": _fmt_yi(peak),
            "peak_date": out_dates[peak_idx],
            "current": _fmt_yi(current),
            "current_date": out_dates[-1],
            "peak_ratio": peak_ratio,
        },
    }
    log.debug(f"[api] /api/margin/read 完成: {len(dates)} 天 "
              f"耗时 {time.time() - t0:.2f}s")
    return result

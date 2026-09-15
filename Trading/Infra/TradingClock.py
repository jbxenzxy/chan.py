# -*- coding: utf-8 -*-
"""
时间与交易日语义工具（Trading/Infra/TradingClock.py）
=====================================================
本模块是 Trading 侧所有「时钟 / 时间戳单位 / 交易日归属」语义的唯一事实源。

内容（2026-09-15 P-C 改名拆分 · Infra划分治理 §5.1）：
  · 墙钟：now_cn / now_ms（CN_TZ 北京时间）
  · 交易日归属：trading_day_of_ms / trading_day_from_clock / date_of_ms
    （夜盘归属次一交易日，规则 ⑸ 的权威口径，SSOT 2026-09-10 立）
  · 时间戳单位嗅探：ts_scale / norm_delta_sec / parse_hhmmss + 两阈值常量
    （原 Period.py 的时间工具，P-C 搭车迁入）
  · 交易日墙钟近似：SESSION_SECS（仅展示/粗算，原 Period.py）

本模块只装时间语义：不装周期登记（freq→秒 在 Infra/Period.py），
不装业务记录（枚举/数据类 在 Infra/Records.py）。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Optional

CN_TZ = timezone(timedelta(hours=8))

# ── 建仓日 / 交易日（SSOT，2026-09-10 立）─────────────────────────────
# 规则 ⑸「今仓 → 反向开仓锁仓 / 昨仓 → 平仓」以及平今•平昨费率口径，
# 全部只认 trading_day_of_ms() 一个函数。权威输入是**毫秒时间戳**
# （bar.timestamp / Signal.timestamp），不是格式化字符串 —— 字符串是展示产物，
# 一旦为空就会被下游解释成一个合法业务语义（"很久以前" = 昨仓），错误无法暴露。
#
# 夜盘起点（北京时间整点）：>= 该时刻的成交归属【次一交易日】。
# 期货夜盘最早 21:00 开盘，取 20:00 留边界余量（20:00-21:00 是休市静默段）。
NIGHT_SESSION_START_HOUR = 20
# 派生日期可信下限：低于它的派生结果说明该"时间戳"不是真实毫秒
# （如测试夹具用 4000 = 序号），必须拒绝采用，避免算成 1970-01-01。
PLAUSIBLE_DATE_MIN = "2020-01-01"


def now_cn() -> str:
    """北京时间 ISO 字符串（秒精度）。"""
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def now_ms() -> int:
    """当前毫秒时间戳（墙钟）。"""
    return int(datetime.now(CN_TZ).timestamp() * 1000)


def _ms_to_dt(ts_ms: int) -> Optional[datetime]:
    """毫秒时间戳 → 北京时间 datetime。无效（None/0/负/越界）→ None。"""
    try:
        ts = int(ts_ms)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000.0, CN_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def date_of_ms(ts_ms: int) -> str:
    """毫秒时间戳 → 自然日 'YYYY-MM-DD'（北京时间）。无效 → ''。"""
    dt = _ms_to_dt(ts_ms)
    return dt.strftime("%Y-%m-%d") if dt is not None else ""


def trading_day_of_ms(ts_ms: int,
                      night_start_hour: int = NIGHT_SESSION_START_HOUR) -> str:
    """毫秒时间戳 → 所属【交易日】'YYYY-MM-DD'（北京时间）。无效 → ''。

    为什么不能直接用自然日
      夜盘的成交属于**次一交易日**。用自然日会漏判：夜盘 21:00 建的仓，
      自然日是 9/9，而 9/10 日盘平它实为【平今】，自然日口径却判成"昨仓"
      → 规则 ⑸ 发 CLOSE 平昨 → 中金所拒单（平今/平昨报错）→ 连锁 phantom 清仓。
      日盘品种（如 IF）两者恒等，所以此坑只在夜盘品种（au/ag/螺纹/原油）暴露。

    规则
      北京时间 hour >= night_start_hour（默认 20）→ 归属次一自然日；否则归属当日。
      周末/节假日不做精细处理：**所有调用方共用本函数**，只要口径一致，
      "是否同一交易日"的判定就正确 —— 例如周五夜盘 → 周六，与下周一的日盘
      不等，判为跨交易日，符合事实（周五夜盘的仓到下周一确实是昨仓）。
      真正需要"精确交易日历"的场景（如对账按日切片）不在本函数职责内。
    """
    dt = _ms_to_dt(ts_ms)
    if dt is None:
        return ""
    if dt.hour >= night_start_hour:
        dt = dt + timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def trading_day_from_clock(clock: str) -> str:
    """墙钟字符串（`entry_at` / `now_cn()` 产物）→ 交易日 'YYYY-MM-DD'。无效 → ''。

    仅作**第三兜底**：既无 entry_date 又无有效 entry_bar_ts 时才用。
    刻意不做夜盘偏移 —— 墙钟在回放/补录场景下未必等于 K 线的交易日，
    把它当权威会引入比"缺失"更隐蔽的错误。这里只负责"能读出个像样的日期"。
    """
    if not clock or len(clock) < 10:
        return ""
    head = clock[:10]
    if head[4] != "-" or head[7] != "-":
        return ""
    if not (head[:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit()):
        return ""
    if head < PLAUSIBLE_DATE_MIN:
        return ""
    return head



# ── 交易日墙钟近似（Step 2.2 归一，原 main.py 硬编码 4.5）────────────
# 仅用于"根/交易日"这类**展示/粗算**（main.py 启动日志）；不是精确的交易时段
# （中金所 9:30–11:30 + 13:00–15:00 = 4h，14:55 强平阈值另见 profile.session_end_hhmm）。
# 收口到这里是为了 SSOT：要改近似值只改这一处。
SESSION_SECS: float = 4.5 * 3600

# ── 时间戳单位判定 ────────────────────────────────────────────────
# 毫秒时间戳 ~1.7e12，秒时间戳 ~1.7e9，中间空 3 个数量级，1e11 可完美分离。
_TS_MS_THRESHOLD = 1e11
# 差值（相邻 bar 间隔）单位嗅探阈值：
#   秒单位最大值 = 30m × 30 根持仓 = 54_000 < 100_000
#   毫秒最小值 = 1m = 60_000 < 100_000（按秒处理也得到 60_000，会超上限——但
#   1m 的真实值 60 秒来自秒单位源，一致）
# 取 100_000 能把"秒单位的任何合理周期/持仓"与"毫秒单位"分开。
_DELTA_MS_THRESHOLD = 100_000


def ts_scale(ts: Any) -> float:
    """返回把该时间戳归一到「秒」所需的除数（毫秒源=1000，秒源=1）。

    用**绝对值**判定，比用差值判定可靠：时间戳量级是稳定的全局属性，
    而差值会随"持仓多久/相邻 bar 间隔多少"变化。
    """
    try:
        return 1000.0 if abs(float(ts)) > _TS_MS_THRESHOLD else 1.0
    except (TypeError, ValueError):
        return 1.0


def norm_delta_sec(later_ts: Any, earlier_ts: Any) -> float:
    """两个时间戳之差 → 秒（自动嗅探毫秒/秒）。

    仅在拿不到绝对量级（如只有差值）时使用；能拿到原值时优先 `ts_scale`。
    """
    try:
        d = float(later_ts) - float(earlier_ts)
    except (TypeError, ValueError):
        return 0.0
    if d <= 0:
        return 0.0
    if d > _DELTA_MS_THRESHOLD:
        d /= 1000.0
    return d


# ══════════════════════════════════════════════════════════════════
# 当日秒数解析（支持 "14:55" 与 "14:54:45" 两种粒度）
# ══════════════════════════════════════════════════════════════════
def parse_hhmmss(s: Any) -> Optional[int]:
    """把 "14:55" / "14:54:45" / "2026-09-01 14:54:45" 解析为当日秒数。

    15s 周期的 date 带秒（"2026-09-01 14:54:45"），旧代码 `s[:5]` 会把
    ":45" 截掉 → 收盘判定最多偏 59 秒，15s 下相当于 4 根 bar。
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    if " " in s:
        s = s.split(" ", 1)[1]
    if "T" in s:                      # ISO 8601: 2026-09-01T14:54:45
        s = s.split("T", 1)[1]
    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        sec = int(float(parts[2])) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None
    return h * 3600 + m * 60 + sec



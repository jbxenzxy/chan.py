# -*- coding: utf-8 -*-
"""
周期档案 / 时间语义工具（Trading/Infra/PeriodProfile.py）
=========================================================
本模块是 Trading 侧**所有"时间与周期"语义的唯一事实源**。

背景（Step 1 四周期审计的产物）
--------------------------------
Trading 原实现里有多处"周期耦合"逻辑是隐式或错误的：

  · `Bar.timestamp` 单位不统一（SSE 源是毫秒，回放源可能是秒），
    `Exit.py` 却把它当秒去和 60~14400 的阈值比 → `_bar_secs` 恒 None；
  · `max_hold_bars=30` 是"根数"，在 30m 下 = 15 小时、在 15s 下 = 7.5 分钟，
    同一个数字语义漂移 120 倍；
  · `_close_retry_bars=5` 名为根数、实则拿毫秒时间戳差值比较 → 5 毫秒；
  · 收盘判定用"bar 起点"，30m 的 bar 起点只有 :00/:30，永远取不到 14:55
    → 收盘强平对 30m 完全失效。

本模块把上述语义集中、显式化，供 Exit/Engine/Source 共用。

为什么 Trading 自持一份 freq→秒，而不 import 主程序 Common.CEnum.FREQ_SEC_MAP
---------------------------------------------------------------------------
  Trading/ 的架构约定是对 chan.py **零侵入、零 import** —— 它是独立部署的自动
  下单网关，只通过 HTTP SSE 端点取数。一旦 import Common.CEnum，就会把整个
  chan.py 依赖树（Common → KLine → BuySellPoint → DataAPI → tqsdk）拖进来，
  网关的启动环境被绑死。

  代价是两表可能漂移 → 由 `Test/test_period_consistency.py` 在 CI 里逐项对账，
  主程序新增/修改周期而这里没跟上时，测试直接红。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# ══════════════════════════════════════════════════════════════════
# 周期 → 秒（Trading 自持的一份；与主程序 Common.CEnum.FREQ_TABLE 对账）
# ══════════════════════════════════════════════════════════════════
FREQ_SEC: Dict[str, int] = {
    "15s": 15,
    "1m": 60,
    "5m": 300,
    "30m": 1800,
}

# 与主程序同名的别名，方便对账测试直接比对两个 dict
FREQ_SEC_MAP = FREQ_SEC

# 期货自动下单支持的周期（按粗细升序）。对账测试 / 参数化测试遍历这份列表。
SUPPORTED_FREQS: Tuple[str, ...] = tuple(
    sorted(FREQ_SEC, key=lambda f: FREQ_SEC[f]))

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


def bar_secs_for(freq: Any, default: Optional[int] = None,
                 strict: bool = False) -> Optional[int]:
    """freq 字符串 → 该周期一根 bar 的秒数。

    strict=True 时未知周期抛 ValueError（配置校验用，fail-fast 不留静默兜底）；
    否则返回 default（None 表示"未知"，调用方自行降级）。
    """
    f = str(freq or "").strip()
    if f in FREQ_SEC:
        return FREQ_SEC[f]
    if strict:
        raise ValueError(
            "不支持的 K 线周期: {!r}（Trading 支持: {}）".format(
                freq, ", ".join(SUPPORTED_FREQS)))
    return default


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


def bar_sec_of_day(x: Any) -> Optional[int]:
    """取 Bar（或 date 字符串）的当日秒数。"""
    d = getattr(x, "date", None)
    if d is None and isinstance(x, str):
        d = x
    return parse_hhmmss(d)


def eod_triggered(bar_start_sec: Optional[int], bar_secs: Optional[int],
                  session_end_sec: Optional[int],
                  lead_bars: int = 1) -> bool:
    """收盘前强平的**统一判定**：再持一根 bar 就赶不上收盘 → 现在就平。

    为什么不是"bar 结束时刻 ≥ 收盘"就平
        30m 的 bar 起点只有 :00 / :30，最后一根是 14:30-15:00，
        它闭合推送时已经是 15:00（收盘后）——按旧口径等于永远平不掉。
        真正的语义应该是"下一根 bar 会跨过收盘，这根就是最后机会"。

    判定式：bar 结束时刻 + lead_bars × bar_secs ≥ 收盘时刻

        lead_bars=1（默认）：
            30m  14:00 起点的 bar → 结束 14:30，+30min = 15:00 ≥ 15:00 ✓
                 触发于 14:30，留 30 分钟缓冲
            5m   14:45 起点 → 结束 14:50，+5min = 14:55 ✓  留 10 分钟
            1m   14:53 起点 → 结束 14:54，+1min = 14:55 ✓  留 6 分钟
            15s  14:54:30 起点 → 结束 14:54:45，+15s = 14:55 ✓ 留 1 分钟

        lead_bars=0 退回旧的"bar 结束时刻 ≥ 收盘"（30m 失效，不推荐）。

    bar_secs 未知时降级为"bar 起点 ≥ 收盘"（保守：宁可不平也不能误判），
    绝不再静默使用错误单位。
    """
    if bar_start_sec is None or session_end_sec is None:
        return False
    if not bar_secs or bar_secs <= 0:
        return bar_start_sec >= session_end_sec
    lead = max(0, int(lead_bars)) * int(bar_secs)
    return (bar_start_sec + bar_secs + lead) >= session_end_sec


def bars_per_day(bar_secs: Optional[int], session_secs: float) -> Optional[int]:
    """一个交易日能有多少根 bar（用于 ATR 样本充足性检查）。"""
    if not bar_secs or bar_secs <= 0 or session_secs <= 0:
        return None
    return int(session_secs // bar_secs)


# ══════════════════════════════════════════════════════════════════
# 周期档案（Step 1 只填时间语义；Step 2 再补盈利相关参数）
# ══════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PeriodProfile:
    """一个 K 线周期的全部周期相关设定。

    Step 1（2026-09-08）只装时间语义（freq / bar_secs）——目标是"任何周期能正确跑通"。
    Step 2.1（2026-09-08）加入 6 项周期敏感参数 + note：信号新鲜度 / 持仓根数 /
      墙钟硬顶 / 收盘提前量 / 收盘时刻 / 日笔数。这 6 项的默认值 = BASELINE
      （当前 5m 默认值），4 个周期先统一占位；Step 2.7+ 拿到真实数据后再按周期
      差异化重标（届时只改对应 profile 的值 + 改 note）。
    """
    freq: str
    bar_secs: int
    note: str = ""                          # 调参记录 / 数据来源 / 标定状态
    signal_max_age_minutes: float = 60.0    # 信号新鲜度过滤（分钟），0=不过滤
    max_hold_bars: int = 30                 # 最长持仓 K 线根数（量纲=bar，与周期无关）
    max_hold_seconds: float = 0.0           # 可选墙钟硬顶（秒），0=不启用
    eod_lead_bars: int = 1                  # 提前 N 根 bar 判定收盘强平
    session_end_hhmm: str = "14:55"         # 收盘前强平阈值时刻（""=不启用）
    max_trades_per_day: int = 20            # 每日最大往返笔数

    def __post_init__(self) -> None:
        if self.freq not in FREQ_SEC:
            raise ValueError(
                "未登记的周期: {!r}（已登记: {}）".format(
                    self.freq, ", ".join(SUPPORTED_FREQS)))
        if FREQ_SEC[self.freq] != int(self.bar_secs):
            raise ValueError(
                "周期档案与 FREQ_SEC 不一致: {}={} vs bar_secs={}".format(
                    self.freq, FREQ_SEC[self.freq], self.bar_secs))

    @property
    def label(self) -> str:
        return self.freq


# 4 个周期的档案。bar_secs 显式给真值；6 项周期敏感参数默认 = BASELINE（dataclass 默认）。
# note 显式标「占位」，Step 2.7+ 拿到真实数据后改成「已标定 + 数据来源 + 日期」。
PERIOD_PROFILES: Dict[str, PeriodProfile] = {
    "15s": PeriodProfile(
        freq="15s", bar_secs=15,
        note="占位=BASELINE；待 2.7+ 真实 15s 数据重标（信号新鲜度/持仓根数/日笔数均需重标）"),
    "1m": PeriodProfile(
        freq="1m", bar_secs=60,
        note="占位=BASELINE；待 2.7+ 真实 1m 数据重标"),
    "5m": PeriodProfile(
        freq="5m", bar_secs=300,
        note="占位=BASELINE（5m 基线尚未经真实数据标定，当前=2.0 默认值）"),
    "30m": PeriodProfile(
        freq="30m", bar_secs=1800,
        note="占位=BASELINE；待 2.7+ 真实 30m 数据重标（30 根≈3.75 交易日，实由收盘强平接管）"),
}


def profile_for(freq: Any) -> Optional[PeriodProfile]:
    return PERIOD_PROFILES.get(str(freq or "").strip())

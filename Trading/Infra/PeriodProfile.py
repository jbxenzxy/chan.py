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


def bars_per_day(bar_secs: Optional[int], session_secs: float) -> Optional[int]:
    """一个交易日能有多少根 bar（用于 ATR 样本充足性检查）。"""
    if not bar_secs or bar_secs <= 0 or session_secs <= 0:
        return None
    return int(session_secs // bar_secs)


# ══════════════════════════════════════════════════════════════════
# 周期档案（时间语义 SSOT：freq / bar_secs）
# ══════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PeriodProfile:
    """一个 K 线周期的时间语义设定（freq / bar_secs）。

    Step 1（2026-09-08）只装时间语义——目标是"任何周期能正确跑通"。
    2026-09-08 精简：原 L4 时间/收盘兜底（max_hold_bars / max_hold_seconds /
      eod_lead_bars / session_end_hhmm）、风控五道硬闸门（max_trades_per_day）
      以及 signal_max_age_minutes（改为 K 线相对容差 signal_k_tol_bars，非周期敏感、
      不再放本档案）随功能一并删除。
    止盈止损等盈利参数（R 地板 / 盈亏比）**不随周期变、随品种变**，归口在
      Infra/ProductProfile.py，不在此档案。
    """
    freq: str
    bar_secs: int

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


# 4 个周期的档案：仅时间语义（freq / bar_secs），与 FREQ_SEC 一一对应。
# 盈利参数（R 地板 / 盈亏比）随品种变，见 Infra/ProductProfile.py。
PERIOD_PROFILES: Dict[str, PeriodProfile] = {
    "15s": PeriodProfile(freq="15s", bar_secs=15),
    "1m": PeriodProfile(freq="1m", bar_secs=60),
    "5m": PeriodProfile(freq="5m", bar_secs=300),
    "30m": PeriodProfile(freq="30m", bar_secs=1800),
}


def profile_for(freq: Any) -> Optional[PeriodProfile]:
    return PERIOD_PROFILES.get(str(freq or "").strip())

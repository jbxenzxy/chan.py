# -*- coding: utf-8 -*-
"""
周期档案（Trading/Infra/Period.py）
===================================
本模块是「周期轴」的唯一登记处：freq → 秒 的单一事实源 + 4 个周期的档案。

⚠️ 周期特有参数当前为空（诚实答案，2026-09-15 P-C 改名时重申 · §5.2）：
  周期相关的行为参数（max_hold_bars / eod_lead_bars / session_end_hhmm /
  max_trades_per_day / signal_max_age_minutes 等）已于 2026-09-08 删除；
  止盈止损等盈利参数**不随周期变、随品种变**，归口在 Infra/Product.py。
  **若未来周期行为参数回归，家在这里。**
  本模块的价值不在「装参数」，而在两件事：
    1. 周期↔秒的单一事实源：FREQ_SEC 一处定义，SUPPORTED_FREQS / bar_secs_for /
       bars_per_day 都从这里长出去；main.py 启动期周期 fail-fast 也读它。
    2. 与 chan.py 主程序的对账护栏：test_period_consistency.py 把 FREQ_SEC 与
       主程序 Common.CEnum 的表逐项比对——主程序加周期而这里没跟上，测试直接红。
       这条护栏是跨仓一致性的唯一保险，删不掉。

时间语义工具（ts_scale / norm_delta_sec / parse_hhmmss / 两阈值常量 /
SESSION_SECS）已于 2026-09-15 P-C 迁往 Infra/TradingClock.py，本模块不再定义。

角色定位（2026-09-14 双轴声明）：本档案按**变异维度（随周期变）**分区，
是领域注册表（凭经验标定的代码资产，git 评审 + 对账测试守护），
不是部署配置入口 —— **不按消费层挪入 Trading/Config.py**；
两轴关系见 Config.py 模块 docstring 的「双轴声明」。

为什么 Trading 自持一份 freq→秒，而不 import 主程序 Common.CEnum.FREQ_SEC_MAP
---------------------------------------------------------------------------
  Trading/ 的架构约定是对 chan.py **零侵入、零 import** —— 它是独立部署的自动
  下单网关，只通过 HTTP SSE 端点取数。一旦 import Common.CEnum，就会把整个
  chan.py 依赖树（Common → KLine → BuySellPoint → DataAPI → tqsdk）拖进来，
  网关的启动环境被绑死。

  代价是两表可能漂移 → 由 `Test/test_period_consistency.py` 在 CI 里逐项对账，
  主程序新增/修改周期而这里没跟上时，测试直接红。

背景（Step 1 四周期审计的产物）
--------------------------------
Trading 原实现里有多处「周期耦合」逻辑是隐式或错误的：
Bar.timestamp 单位不统一（SSE 源毫秒 / 回放源可能秒）被当秒比、
max_hold_bars=30 在 30m 与 15s 下语义漂移 120 倍、
_close_retry_bars 拿毫秒差比阈值、收盘判定取不到 14:55 ——
本轴把上述语义集中、显式化，供 Exit/Engine/Source 共用。
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


def bars_per_day(bar_secs: Optional[int], session_secs: float) -> Optional[int]:
    """一个交易日能有多少根 bar（用于 ATR 样本充足性检查）。"""
    if not bar_secs or bar_secs <= 0 or session_secs <= 0:
        return None
    return int(session_secs // bar_secs)


# ══════════════════════════════════════════════════════════════════
# 周期档案（时间语义 SSOT：freq / bar_secs）
# ══════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Period:
    """一个 K 线周期的时间语义设定（freq / bar_secs）。

    Step 1（2026-09-08）只装时间语义——目标是"任何周期能正确跑通"。
    2026-09-08 精简：原 L4 时间/收盘兜底（max_hold_bars / max_hold_seconds /
      eod_lead_bars / session_end_hhmm）、风控五道硬闸门（max_trades_per_day）
      以及 signal_max_age_minutes（改为 K 线相对容差 signal_k_tol_bars，非周期敏感、
      不再放本档案）随功能一并删除。
    止盈止损等盈利参数（R 地板 / 盈亏比）**不随周期变、随品种变**，归口在
      Infra/Product.py，不在此档案。
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
# 盈利参数（R 地板 / 盈亏比）随品种变，见 Infra/Product.py。
PERIOD_PROFILES: Dict[str, Period] = {
    "15s": Period(freq="15s", bar_secs=15),
    "1m": Period(freq="1m", bar_secs=60),
    "5m": Period(freq="5m", bar_secs=300),
    "30m": Period(freq="30m", bar_secs=1800),
}


def profile_for(freq: Any) -> Optional[Period]:
    return PERIOD_PROFILES.get(str(freq or "").strip())

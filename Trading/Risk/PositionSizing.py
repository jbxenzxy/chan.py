# -*- coding: utf-8 -*-
"""
仓位管理（手数定档）
====================
回答"这一笔开几手"。

2026-09-08 精简：只保留**固定手数**功能。
    · 开几手 = `fixed_volume`（0=沿用 risk.max_volume，默认 2）。
    · 动态模式（capital_pct 按保证金占比 / atr_risk 按风险敞口）已删除；
    · 资金闸门（capital_gate / risk_unit_pct / initial_cash）已整体删除；
    · 不再查账户权益/联网，纯固定手数 + 上下限截断。

数据源
------
sizing.enabled=False 时即传统固定手数（下称 fixed_volume）；enabled=True 也仍
是 fixed_volume，只是同样走一遍 min/max 截断语义。本模块保持"默认关闭"的
历史行为：不查账户、不联网、dry_run 无需任何改动 —— 零风险引入。

术语（与 CTP 对齐，避免歧义）
    开多 (BUY, OPEN) / 开空 (SELL, OPEN) / 平多 (SELL, CLOSE) / 平空 (BUY, CLOSE)
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..Infra.InstrumentSpec import InstrumentSpec
from ..Config import SizingConfig

# 中金所限价单每次最大下单手数（IF/IH/IC/IM 同，交易所交易细则）。
# SSOT（Step 2.4 合并）： sizing.max_volume 未显式配置（0）时的默认截断上限，
# 也是引擎开仓前的交易所限单检查（Engine._do_open）的上限——两处共用本常量。
CFFEX_LIMIT_MAX = 20


class PositionSizer:
    """把"要不要开"翻译成"开几手"（固定手数）。

    不持有状态、不碰 IO、不查账户。开几手完全由配置决定。
    """

    def __init__(self, params: "SizingConfig",
                 spec: Optional[InstrumentSpec] = None,
                 risk_max_volume: int = 2):
        # 严格模式（2026-09-07）：只接受 SizingConfig 配置模型，不接受裸 dict。
        # 目的：拼错/缺失的键在 Trading/Config.py 构造时就抛异常（fail-fast）。
        if isinstance(params, dict):
            raise TypeError(
                "PositionSizer 只接受 SizingConfig（严格模式不接受裸 dict）："
                "请用 SizingConfig(**d) 构造，缺键/拼错会在配置阶段暴露")
        if params is None:
            raise TypeError("PositionSizer 需要 SizingConfig 参数（传 None 无效）")
        p = params
        self.cfg = p
        self.spec = spec or InstrumentSpec()
        self.enabled = bool(p.enabled)

        # fixed_volume=0 表示"沿用 risk.max_volume"（默认 2）
        self.fixed_volume = int(p.fixed_volume or 0)

        # 固定手数（非仓位管理 / fixed 模式的回落值）：
        # fixed_volume 显式 >0 用它，否则回落 risk.max_volume（默认 2）。
        self._fixed_volume = self.fixed_volume if self.fixed_volume > 0 \
            else max(0, int(risk_max_volume or 0))

        # 硬上限（固定手数的截断上限）：
        # 未显式配置（0）时默认中金所单笔上限 20；显式配置则用配置值。
        cfg_max = int(p.max_volume or 0)
        self.max_volume = cfg_max if cfg_max > 0 else CFFEX_LIMIT_MAX
        # 下限：算出来小于它时提升到它（默认 1，保证"信号来了就交易"的历史行为）
        self.min_volume = int(p.min_volume or 0)
        # 手数取不到时的回退值（固定手数下无权益查询，通常不会走回退）
        self.fallback_volume = int(p.fallback_volume or 0)

        # ── 解锁后是否补开今仓 ──
        #   True  = 解锁昨仓后绝不新开今仓（默认，规避金融期货"平今高手续费"）。
        #           例：昨日锁 3 手、今日信号算 5 手 → 只解锁 3 手，缺的 2 手不补开。
        #   False = 解锁后按缺口补开（N - 已解锁手数），把净敞口补到目标手数（需显式关闭）。
        #   无论开关如何，解锁本身照常执行（解锁是减风险动作）。
        self.unlock_no_new_open = bool(p.unlock_no_new_open)

    # ---------------- 对外主入口 ----------------
    def size(self, **kwargs) -> Tuple[int, str]:
        """返回 (手数, 原因)。手数 ≤ 0 表示不要开仓（调用方应拦下）。

        （2026-09-08 精简：固定手数不再需要 equity/price/stop/atr 输入，
         多余关键字参数一律忽略，签名保持兼容。）
        """
        raw = float(self._fixed_volume)
        why = "sizing:fixed"

        # ① 硬上限截断
        if self.max_volume > 0 and raw > self.max_volume:
            return self.max_volume, why + "+capped(max_volume=%d)" % self.max_volume

        vol = int(raw)

        # ② 下限提升（算出来少于 min_volume 时，若允许最少 1 手则提升）
        if vol < self.min_volume:
            if self.min_volume > 0 and self.max_volume >= self.min_volume:
                return self.min_volume, why + "+raised(min_volume=%d)" % self.min_volume
            return 0, why + "+below_min(no_open)"

        return vol, why

    # ---------------- 观测 ----------------
    def describe(self) -> Dict[str, Any]:
        """给日志/事件流用的快照，便于事后复盘"为什么开这么多手"。"""
        return {
            "enabled": self.enabled,
            "mode": "fixed",
            "fixed_volume": self.fixed_volume,
            "min_volume": self.min_volume,
            "max_volume": self.max_volume,
            "fallback_volume": self.fallback_volume,
            "unlock_no_new_open": self.unlock_no_new_open,
        }
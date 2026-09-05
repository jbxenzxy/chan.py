# -*- coding: utf-8 -*-
"""
持仓簿（PositionBook）
======================
设计动机
--------
v1 只支持"一个实例一手"，全部代码假定 `engine.position` 是单个 `Position` 或 None。
v2 引入"同 K 线连开 N 单 (N≥1)"与"昨日锁仓 UNLOCK_FIRST 入场"两条路径后，
引擎需要同时跟踪多个 Position（同方向拆批 / 跨日双边锁仓），单变量已不够用。

E1 的目标（已交付）
  ① 引入 `PositionBook` 容器（薄封装 List[Position]，配 max=1 的强约束）
  ② 通过 `engine.position` 的 property 兼容层，**让所有现有调用点不需改一行**
  ③ 序列化同时写新键 "positions" 与旧键 "position"，老数据库零侵入迁移
  ④ 零行为变化 —— P5..P11 必须仍然全绿

E2 已交付
  进场点：`on_signal` 入口检查 `book.has_opposite(side)` 决定走 OPEN 还是 UNLOCK。

E3 计划（本次 E3.1 子步：cfg 化容器容量）
  E3.1（本次）：`cfg.risk.max_open_positions` 配置化 + `book.set_max(N)` 动态调整 +
                PositionBook 真支持多仓（add 不再 throw，只要总数 ≤ max）；legacy_single
                在多仓时**仍抛守护错**（真正的多仓 API 由 E3.3 接入）
  E3.2：PositionSizer 加 batch 拆分（total/batch），`_open_position` → `_open_positions` 循环
  E3.3：settle_position / _close_position 改 for-each + FIFO 出场 + _reconcile_position 多仓

设计纪律
  · max_open_positions 默认 = 1 → 所有现存测试（P5..P13）零行为变化
  · 测试显式构造 `cfg = GatewayConfig(risk=RiskConfig(max_open_positions=N))` 才能进入多仓路径
  · legacy_single 在多仓抛错是有意的早期守护：E3.3 之前不允许"单仓 API 操作多仓"
  · add 抛错改为按 cfg max 限制（max=1 仍等同 E1）；add 多仓时仍按 FIFO 顺序追加
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .types import Position, Side


class PositionBookError(Exception):
    """PositionBook 容量 / 不变量违反时抛出。E1 阶段主要是超 max 报错；E3 阶段用于多仓守护。"""


class PositionBook:
    """持仓簿 —— 持有若干 Position 的最简容器。

    关键不变量
      `len(self._positions) <= max_positions`

    关键约定
      * 容器按"添加顺序"持有 —— 决定离场优先级 / 结算顺序（FIFO）。
      * 内部 List 不直接暴露给外部，避免被偷偷 mutate；统一通过 `positions` property 拷贝访问。
      * `legacy_single()` 是 E1/E2 阶段的主入口（v1 单仓语义）：
          · 空 → None
          · 1 个 → 那个 Position
          · 多仓（max>1 时可能发生）→ 抛 PositionBookError
        E3.3 之前不允许用单仓 API 操作多仓 —— 引擎 settle/close 必须显式 for-each
        `engine.positions.positions`，否则视为状态不自洽。
    """
    DEFAULT_MAX = 1   # E1 默认：单实例最多 1 仓；E3 由 cfg.risk.max_open_positions 覆盖

    def __init__(self, max_positions: int = DEFAULT_MAX):
        if max_positions < 1:
            raise PositionBookError(
                "max_positions must be >= 1 (got {})".format(max_positions))
        self._max = int(max_positions)
        self._positions: List[Position] = []
        self._truncated: List[Position] = []   # replace_with 截断时丢弃的仓，供调用方记录

    # ─── 容量管理（E3.1 新增）───────────────────────
    def set_max(self, n: int) -> None:
        """动态调整容量上限（cfg 化场景：引擎重启 / 改 cfg 后调用）。
        只能"放大"或"等量"，**不能缩小到现存数以下**（不允许隐式丢弃持仓）。
        """
        n = int(n)
        if n < 1:
            raise PositionBookError(
                "max_positions must be >= 1 (got {})".format(n))
        if n < len(self._positions):
            raise PositionBookError(
                "Cannot shrink max_positions from {} to {}: "
                "{} position(s) currently held".format(
                    self._max, n, len(self._positions)))
        self._max = n

    # ─── 容器 CRUD ─────────────────────────────────────
    def add(self, p: Position) -> None:
        """添加一个 Position（FIFO 追加）。
        超过 max 立即报错 —— 不允许隐式合并/覆盖。
        E1: max=1 即触发；E3.1: max=N 时第 N+1 个报错。
        """
        if len(self._positions) >= self._max:
            raise PositionBookError(
                "PositionBook full (max={}, present={})".format(
                    self._max, len(self._positions)))
        self._positions.append(p)

    def remove(self, p: Position) -> None:
        """移除第一个身份相等的 Position；找不到不抛错（幂等）。"""
        try:
            self._positions.remove(p)
        except ValueError:
            pass

    def clear(self) -> None:
        self._positions.clear()

    def replace_with(self, other: "PositionBook") -> None:
        """引擎内部用：把整个簿替换成另一簿。
        仅用于 _restore 从持久化恢复的场景 —— 调用方要保证 other 内容合法。

        兼容"持久化数据上限 > 当前 cfg 上限"的场景（典型：max 后续缩小，
        或历史数据来自更宽容的版本）—— 此时截断到 self._max 并打 warning，
        不抛错。这是 restore 路径的"宽松恢复"语义。
        """
        if not isinstance(other, PositionBook):
            raise PositionBookError(
                "replace_with requires PositionBook, got {}".format(type(other).__name__))
        n_other = len(other._positions)
        if n_other > self._max:
            # E3.1 兼容：cfg.max 后续缩小时，已持久化的多仓不应让引擎启动失败。
            # 取前 max 个 FIFO 截断（与离场优先级一致），并返回被丢弃的 Positions
            # 让调用方可以写 warning。
            self._positions = list(other._positions[:self._max])
            self._truncated = other._positions[self._max:]
        else:
            self._positions = list(other._positions)
            self._truncated = []

    def is_empty(self) -> bool:
        return not self._positions

    def __len__(self) -> int:
        return len(self._positions)

    def __iter__(self):
        return iter(list(self._positions))   # 拷一份，外部修改不会影响迭代

    def __bool__(self) -> bool:
        # 明确：bool(book) 表示"有没有持仓"
        return bool(self._positions)

    @property
    def positions(self) -> List[Position]:
        """浅拷贝 list，避免外部偷偷 append/clear 改坏内部状态。"""
        return list(self._positions)

    @property
    def max_positions(self) -> int:
        return self._max

    @property
    def truncated_on_restore(self) -> List[Position]:
        """replace_with 触发截断时被丢弃的 Position（最近一次）。
        引擎 _restore 在 cfg 上限 < persisted 数据时引用本字段写 warning。
        """
        return list(self._truncated)

    # ─── 兼容层：单仓 API（v1 主路径）─────────────────
    def legacy_single(self) -> Optional[Position]:
        """E1/E2 阶段主入口。语义 = 旧版 `engine.position`：
            · 空 → None
            · 1 个 → 那个 Position
            · 多仓（max>1 时可能发生）→ 抛 PositionBookError
              —— E3.3 之前不允许用单仓 API 操作多仓。
              调用方应当循环 .positions 显式处理。
        """
        if not self._positions:
            return None
        if len(self._positions) > 1:
            # E3.1 仍守护：引擎主路径未 for-each 之前，多仓视为状态不自洽。
            # 真正多仓接入是 E3.3（settle/close loop + FIFO）。
            raise PositionBookError(
                "PositionBook holds {} positions; legacy_single() requires 0 or 1. "
                "E3.3 will iterate .positions instead.".format(len(self._positions)))
        return self._positions[0]

    def set_legacy(self, p: Optional[Position]) -> None:
        """兼容层 setter：把整簿 reset 成只有 p 一个（或清空）。

        语义 —— 不论 max 是多少，set_legacy 都做"整簿替换"：
          · max=1：完全替换（v1 语义，旧引擎 `self.position = new_pos`）
          · max>1：整簿替换为单笔 p —— 其他仓被丢弃 ⚠️

        多仓下丢仓风险：引擎主路径应避免在多仓状态下调用 set_legacy。
        推荐：多仓场景下用 `book.clear()` + `book.add(p)`，或者 `book.remove(p)` 增量操作。
        legacy_single() 多仓抛错仍是守护 —— E3.3 之前不允许用单仓 API 操作多仓。

        此保留 E1 行为不收紧的原因是：测试 P12 line 208-211 锁死了"多仓 set_legacy 替换整簿"
        语义，引擎 _restore 路径也走 set_legacy（虽然 E3.1 已改为走 replace_with 内部方法）。
        """
        if p is None:
            self._positions.clear()
            return
        self._positions = [p]

    # ─── E2 准备：跨方向识别 ──────────────────────────
    def has_opposite(self, side: Side) -> bool:
        """是否存在与给定 side 相反方向的持仓。

        E2 (UNLOCK_FIRST 入场路径) 的判定依据：
          收到新信号时，若本簿已有反向 Position → 这是一笔"昨仓解锁"，
          不应再走开仓，应走 UNLOCK_FIRST（只平昨）。
        """
        for p in self._positions:
            if p.side is not side:
                return True
        return False

    def opposite_positions(self, side: Side) -> List[Position]:
        """取出所有与给定 side 相反方向的 Position（E2 用）。"""
        return [p for p in self._positions if p.side is not side]

    def same_side_positions(self, side: Side) -> List[Position]:
        """取出与给定 side 同方向的 Position（E3 N≥1 拆批后用于合并 / 批量离场）。"""
        return [p for p in self._positions if p.side is side]

    # ─── 序列化 ───────────────────────────────────────
    def to_dict(self) -> List[Dict[str, Any]]:
        """序列化成 list[dict]。空簿 → []（不是 None，便于回放空状态）。"""
        return [p.to_dict() for p in self._positions]

    @classmethod
    def from_dict(cls, data: Any, max_positions: int = DEFAULT_MAX) -> "PositionBook":
        """反序列化。

        兼容两种输入形态：
          · 新版 list（"positions" 键）
          · 旧版 dict（"position" 键 —— v1 单字段格式）

        任一形态都把有效 Position 装入新簿。识别不出（空 dict / 空 list）返回空簿。
        """
        book = cls(max_positions=max_positions)
        items: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            # 旧 v1 单字段格式
            if data.get("symbol"):
                items = [data]
        elif isinstance(data, list):
            items = [x for x in data if isinstance(x, dict) and x.get("symbol")]
        for item in items:
            try:
                book._positions.append(Position.from_dict(item))
            except Exception:
                # 单条坏数据不影响整体恢复（典型场景：未持久化的新字段被旧 schema 解析）
                continue
        return book

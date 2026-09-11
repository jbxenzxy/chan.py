"""
持仓簿（PositionBook）
======================
职责
----
持有一个合约下的若干笔 `Position`，并提供**三态判定与对冲选仓**所需的查询。
它是"容器 + 查询"，不含任何下单 / 判定逻辑 —— 决策一律在 Engine。

核心约定（需求附录，2026-09-11 重构确立）
------------------------------------------
1. **仓单之间没有配对关系。** 簿只是一个按建仓时间先后（FIFO）排列的序列。
   平仓时与"序列中反向最早的一笔"对冲（见 `oldest_opposite`），
   不需要、也不存在"这两笔是一对"的记录。

2. **三态只看净敞口。** 空仓 / 锁仓 / 运行的区别完全由 `net_volume()` 决定，
   与"这笔仓是怎么来的"无关（历史上曾按来源标记判定，已废弃）。

3. **不设笔数上限。** 容量不是本层的职责，资金才是唯一闸门
   （钱不够自然开不成功，由柜台拒单兜底）。历史上曾有 `max_open_positions`
   笔数上限，2026-09-11 按 D2 删除 —— 它会把正常的连续开仓静默挡掉。

序列化形态
----------
`to_dict` -> list[dict]；`from_dict` 同时接受新版 list（"positions" 键）
与旧版 dict（"position" 键，v1 单字段格式）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..Infra.Types import Position, Side


class PositionBookError(Exception):
    """PositionBook 容量 / 不变量违反时抛出。E1 阶段主要是超 max 报错；E3 阶段用于多仓守护。"""


class PositionBook:
    """持仓簿 —— 持有若干 Position 的最简容器。

    关键约定
      * 容器按"添加顺序"持有 —— 这个顺序就是 FIFO 出场顺序，
        也是"哪一笔是最近建的"的依据（见 `latest` / `oldest_opposite`）。
      * 内部 List 不直接暴露给外部，避免被偷偷 mutate；
        统一通过 `positions` property 拷贝访问。
      * `legacy_single()` 仅在**确知簿内至多一笔**的场景使用（如恢复后的兼容路径）：
        空 → None，1 个 → 该 Position，多笔 → 抛 PositionBookError。
        引擎的批量路径一律显式 for-each `engine.positions.positions`。
    """
    DEFAULT_MAX = None   # 默认不限容量（D2：笔数上限已删，资金是唯一闸门）

    def __init__(self, max_positions: Optional[int] = DEFAULT_MAX):
        # max_positions=None 表示"不限容量"：add 不校验、replace_with 不截断。
        # 参数本身保留仅因为部分调用点仍显式传值；新代码直接 `PositionBook()`。
        if max_positions is None:
            self._max: Optional[int] = None
        else:
            if int(max_positions) < 1:
                raise PositionBookError(
                    "max_positions must be >= 1 (got {})".format(max_positions))
            self._max = int(max_positions)
        self._positions: List[Position] = []
        self._truncated: List[Position] = []   # replace_with 截断时丢弃的仓，供调用方记录

    # ─── 容量管理 ───────────────────────────────────
    def set_max(self, n: Optional[int]) -> None:
        """动态调整容量上限（cfg 化场景：引擎重启 / 改 cfg 后调用）。
        只能"放大"或"等量"，**不能缩小到现存数以下**（不允许隐式丢弃持仓）。
        n=None 表示不限容量。
        """
        if n is None:
            self._max = None
            return
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
        """添加一个 Position（FIFO 追加）。超过 max 立即报错 —— 不允许隐式合并/覆盖。"""
        if self._max is not None and len(self._positions) >= self._max:
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

        兼容"持久化数据笔数 > 当前 max"的场景 —— 此时截断到 self._max 并把
        被丢弃的仓记入 `truncated_on_restore`，不抛错（restore 路径的宽松语义）。
        """
        if not isinstance(other, PositionBook):
            raise PositionBookError(
                "replace_with requires PositionBook, got {}".format(type(other).__name__))
        n_other = len(other._positions)
        if self._max is not None and n_other > self._max:
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
        """语义 = 旧版 `engine.position`：空 → None，1 笔 → 该 Position，
        多笔 → 抛 PositionBookError（调用方应循环 `.positions` 显式处理）。"""
        if not self._positions:
            return None
        if len(self._positions) > 1:
            raise PositionBookError(
                "PositionBook holds {} positions; legacy_single() requires 0 or 1. "
                "Iterate .positions instead.".format(len(self._positions)))
        return self._positions[0]

    def set_legacy(self, p: Optional[Position]) -> None:
        """兼容层 setter：把整簿 reset 成只有 p 一笔（或清空）。

        ⚠️ 多仓下会把其他仓**整簿丢弃**。引擎主路径应避免在多仓状态下调用它；
        需要精确控制时用 `book.clear()` + `book.add(p)`，或 `book.remove(p)` 增量操作。
        """
        if p is None:
            self._positions.clear()
            return
        self._positions = [p]

    # ─── E2 准备：跨方向识别 ──────────────────────────
    def has_opposite(self, side: Side) -> bool:
        """是否存在与给定 side 相反方向的持仓。

        ⚠️ 本方法**只做方向筛选，不判日期**。锁仓态下同样存在反向仓，
        此时该 OPEN 还是 CLOSE 取决于 `latest().entry_date` 是否等于今日
        （规则 ⑹/⑺），与本方法无关。
        """
        for p in self._positions:
            if p.side is not side:
                return True
        return False

    def opposite_positions(self, side: Side) -> List[Position]:
        """取出所有与给定 side 相反方向的 Position（保持 FIFO 顺序）。"""
        return [p for p in self._positions if p.side is not side]

    def same_side_positions(self, side: Side) -> List[Position]:
        """取出与给定 side 同方向的 Position（保持 FIFO 顺序）。"""
        return [p for p in self._positions if p.side is side]

    # ─── 三态判定 / 选仓（2026-09-11 重构新增）─────────────────────────
    def net_volume(self) -> int:
        """净敞口（手，带符号）：Σ(side.sign × volume)。>0 净多，<0 净空。

        **三态判定的唯一来源**（需求 ⑴，架构约束 A1）：
          net == 0 且簿空 → FLAT（空仓态）
          net == 0 且簿非空 → LOCKED（锁仓态）
          net != 0 → RUNNING（运行态）
        除 `TradingEngine.account_state()` 外，任何地方都不得再写第二处三态判定。
        """
        return sum(p.side.sign * int(p.volume) for p in self._positions)

    def oldest_opposite(self, side: Side) -> Optional[Position]:
        """反向仓中最早建仓的一笔（`entry_bar_seq` 最小）—— CLOSE 的对冲目标。

        为什么是"反向最早"：需求附录规定，平仓就是跟持仓序列中反向最早的仓单
        对冲掉。簿内仓单**没有配对概念**，只有时间先后，所以选仓规则是纯 FIFO。
        平掉最早的一笔不会改变"最近一笔"是谁 —— 故拆锁全程 `D_last` 无需重算。
        """
        cands = self.opposite_positions(side)
        if not cands:
            return None
        return min(cands, key=lambda p: p.entry_bar_seq)

    def latest(self) -> Optional[Position]:
        """最近建仓的一笔（`entry_bar_seq` 最大）—— 规则 ⑷⑸⑹⑺ 的 D_last 取值点。

        为什么只看最近一笔就够：运行态 / 锁仓态下，簿内仓单要么全是今仓、
        要么全是跨日仓，**不可能混合**（附录 A.3 归纳证明）。故"最近一笔的
        建仓交易日"等价于"任一笔的建仓交易日"，看一笔即可判定当日 / 跨日。
        """
        if not self._positions:
            return None
        best = max(p.entry_bar_seq for p in self._positions)
        # 同一序号（同一根 K 线内多笔）时取**最后追加**的一笔 —— 它才是时间上最新的
        return [p for p in self._positions if p.entry_bar_seq == best][-1]

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

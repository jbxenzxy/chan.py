# -*- coding: utf-8 -*-
"""
P5 修复验证脚本（不需要真 tqsdk，用 mock 验证逻辑）

测试覆盖：
  ① _verify_position_delta 精确容差匹配：cur==target 通过；cur 跳到 target+2（被 CTP
     重发回报污染）应被拒为 False
  ② _verify_position_delta 兼容 ±1 帧同步漂移：cur=target±1 通过
  ③ _verify_position_delta close 方向：cur 减到 baseline-volume 通过；cur 减到
     baseline-volume-2 拒为 False
  ④ baseline<0 立即返回 False
"""
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    """从本文件位置向上找包含 tg/ 包的目录，兼容多种摆放方式。"""
    d = _HERE
    for _ in range(5):
        for cand in (os.path.join(d, "tg"), os.path.join(d, "trader_gateway", "tg")):
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "__init__.py")):
                return os.path.dirname(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 tg 包。请把本文件放在 trader_gateway/ 或 trader_gateway/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 trader_gateway 目录。")
    raise SystemExit(2)
sys.path.insert(0, _TG_ROOT)

from tg.brokers.simnow import _verify_position_delta, _position_total  # noqa: E402


class FakeApi:
    """mock tqsdk API，支持 time.time() 模拟超时。"""

    def __init__(self):
        self._wait_update_calls = 0
        self._position = {}
        self._should_fail = False

    def set_position(self, sym, side, total):
        """注入一个持仓快照。side='LONG' 或 'SHORT'。"""
        item = type("FakePos", (), {})()
        item.pos_long_today = total if side == "LONG" else 0
        item.pos_long_his = 0
        item.pos_short_today = 0 if side == "LONG" else total
        item.pos_short_his = 0
        self._position[sym] = item

    def get_position(self, symbol=None):
        if symbol is None:
            return self._position
        return self._position.get(symbol)

    def wait_update(self, deadline=None):
        self._wait_update_calls += 1
        # 模拟同步耗尽，返回 None 即可


def test_open_exact_match():
    """open 1 手：baseline=0, cur=1 应通过"""
    api = FakeApi()
    api.set_position("CFFEX.IF2609", "LONG", 1)
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=0, expected_delta=1, timeout_s=2.0)
    assert ok is True, "baseline=0, cur=1（open 1 手到位）应通过"
    print("✓ test_open_exact_match passed")


def test_open_ghost_rejected():
    """open 1 手但被 CTP 重发污染到 cur=3：应拒为 False（之前 P4 `>=` 错判通过，P5 精确匹配拒绝）"""
    api = FakeApi()
    api.set_position("CFFEX.IF2609", "LONG", 3)
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=0, expected_delta=1, timeout_s=2.0)
    assert ok is False, "baseline=0, cur=3（幽灵+3）应被 P5 精确匹配拒"
    print("✓ test_open_ghost_rejected passed (P5 关键修复点)")


def test_open_tolerance_one_frame():
    """open 1 手但 cur=2（同步漂移 1 帧）：应通过（±1 容差）"""
    api = FakeApi()
    api.set_position("CFFEX.IF2609", "LONG", 2)
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=0, expected_delta=1, timeout_s=2.0)
    assert ok is True, "baseline=0, cur=2（漂移 1 帧）应通过"
    print("✓ test_open_tolerance_one_frame passed")


def test_open_tolerance_two_frames_rejected():
    """open 1 手但 cur=3（漂移 2 帧，超容差）：应拒为 False"""
    api = FakeApi()
    api.set_position("CFFEX.IF2609", "LONG", 3)
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=0, expected_delta=1, timeout_s=2.0)
    assert ok is False, "cur=3 漂移超容差应拒"
    print("✓ test_open_tolerance_two_frames_rejected passed")


def test_close_exact_match():
    """close 1 手：baseline=2, cur=1 应通过"""
    api = FakeApi()
    api.set_position("CFFEX.IF2609", "LONG", 1)
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=2, expected_delta=-1, timeout_s=2.0)
    assert ok is True, "baseline=2, cur=1（close 1 手到位）应通过"
    print("✓ test_close_exact_match passed")


def test_close_ghost_rejected():
    """close 1 手但被重发污染到 cur=-1（反向减仓）：应拒为 False"""
    api = FakeApi()
    api.set_position("CFFEX.IF2609", "LONG", -1)
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=2, expected_delta=-1, timeout_s=2.0)
    assert ok is False, "baseline=2, cur=-1（幽灵 -1）应被 P5 拒"
    print("✓ test_close_ghost_rejected passed (P5 关键修复点)")


def test_baseline_negative():
    """baseline<0 立即返回 False（_position_total 读失败时）"""
    api = FakeApi()
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=-1, expected_delta=1, timeout_s=2.0)
    assert ok is False, "baseline<0 应立即返回 False"
    print("✓ test_baseline_negative passed")


def test_position_total_zero():
    """_position_total 在空持仓时返回 0"""
    api = FakeApi()
    n = _position_total(api, "CFFEX.IF2609", "LONG")
    assert n == 0, f"空持仓应返回 0，实际 {n}"
    print("✓ test_position_total_zero passed")


def test_position_total_reads():
    """_position_total 正确读取今+昨"""
    api = FakeApi()
    item = type("P", (), {})()
    item.pos_long_today = 2
    item.pos_long_his = 1
    item.pos_short_today = 0
    item.pos_short_his = 0
    api._position["CFFEX.IF2609"] = item
    n = _position_total(api, "CFFEX.IF2609", "LONG")
    assert n == 3, f"今2+昨1 应=3，实际 {n}"
    print("✓ test_position_total_reads passed")


if __name__ == "__main__":
    test_open_exact_match()
    test_open_ghost_rejected()
    test_open_tolerance_one_frame()
    test_open_tolerance_two_frames_rejected()
    test_close_exact_match()
    test_close_ghost_rejected()
    test_baseline_negative()
    test_position_total_zero()
    test_position_total_reads()
    print()
    print("=" * 60)
    print("✓ P5 修复全部 9 个单元测试通过")
    print("=" * 60)
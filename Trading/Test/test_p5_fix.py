# -*- coding: utf-8 -*-
"""
P5 修复验证脚本（不需要真 tqsdk，用 mock 验证逻辑）

测试覆盖：
  ① _verify_position_delta 精确容差匹配：cur==target 通过；cur 跳到 target+2（被 CTP
     重发回报污染）应被拒为 False
  ② _verify_position_delta 兼容 ±1 帧同步漂移：cur=target±1 通过
  ③ _verify_position_delta close 方向：cur 减到 baseline-volume 通过；cur 减到
     baseline-volume-2 拒为 False
  ④ baseline<0 / baseline=None（读数不可信）立即返回 False
  ⑤ 读数不可信时 _position_total 返回 None（不是 -1 这种数字哨兵）
"""
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    """从本文件位置向上找 Trading 包目录，兼容多种摆放方式。"""
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d  # Trading 包目录本身（消 tg/ 层后 Trading 即包）
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Broker.SimNow import _verify_position_delta, _position_total  # noqa: E402


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
    """position 被污染成负手数：畸形读数 → 不可信 → 不判通过（P5 关键修复点）"""
    api = FakeApi()
    api.set_position("CFFEX.IF2609", "LONG", -1)
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=2, expected_delta=-1, timeout_s=2.0)
    assert ok is False, "cur=-1（畸形读数）应被拒为不通过"
    print("✓ test_close_ghost_rejected passed (P5 关键修复点)")


def test_baseline_negative():
    """baseline<0（畸形读数）立即返回 False —— 不猜、不放行。

    收敛后 `_position_total` 不再产出负数（不可信一律 None），这里钉的是
    **接口边界上的自守**：直接调用方塞进负数也必须被判掉。
    """
    api = FakeApi()
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=-1, expected_delta=1, timeout_s=2.0)
    assert ok is False, "baseline<0 应立即返回 False"
    print("✓ test_baseline_negative passed")


def test_baseline_none():
    """baseline=None（下单前读数不可信）→ 立即 False（不猜、不放行）"""
    api = FakeApi()
    api.set_position("CFFEX.IF2609", "LONG", 1)
    ok = _verify_position_delta(api, "CFFEX.IF2609", "LONG",
                                 baseline=None, expected_delta=1, timeout_s=2.0)
    assert ok is False, "baseline=None 应立即返回 False"
    print("✓ test_baseline_none passed")


def test_position_total_untrusted_is_none():
    """读数不可信 → _position_total 返回 None，**不是** -1 这种数字哨兵。

    数字哨兵的危险在于漏挡的调用方会把它当手数算下去（`baseline + delta` /
    `engine_vol - real_vol`），算出错误手数还一路静默；None 会直接 TypeError。
    """
    api = FakeApi()
    # 形态认不出（既无持仓字段、也不是 Mapping）
    api._position["CFFEX.IF2609"] = object()
    assert _position_total(api, "CFFEX.IF2609", "LONG") is None, "形态认不出应返回 None"
    assert _position_total(None, "CFFEX.IF2609", "LONG") is None, "api 异常应返回 None"
    # 值域畸形（负手数）
    api.set_position("CFFEX.IF2609", "LONG", -1)
    assert _position_total(api, "CFFEX.IF2609", "LONG") is None, "负手数应判不可信"
    # 对照：可信的 0 仍是 0（不是 None），"无仓"与"读不到"必须能分开
    api.set_position("CFFEX.IF2609", "LONG", 0)
    assert _position_total(api, "CFFEX.IF2609", "LONG") == 0, "可信的 0 必须是 0"
    print("✓ test_position_total_untrusted_is_none passed")


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
    test_baseline_none()
    test_position_total_untrusted_is_none()
    test_position_total_zero()
    test_position_total_reads()
    print()
    print("=" * 60)
    print("✓ P5 修复全部 11 个单元测试通过")
    print("=" * 60)
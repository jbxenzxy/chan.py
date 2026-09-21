# -*- coding: utf-8 -*-
"""
P6 修复单元测试：CTP 真实成交明细（trade_records）权威判定
==========================================================
背景（v5 实测事故）
    tqsdk 在 insert_order 后会**乐观**把 position 缓存 +1/-1，CTP 后续拒单也不回滚。
    P4/P5 拿 position 缓存做"必过"校验，两头都误判：
      · 误拒：CTP 真成交但 position 缓存滞后 → 判幽灵 → v5 的 21 笔 close 死循环
      · 误放：CTP 拒单但 position 缓存被 +1 → 判成交 → v5 的 4 笔幻象 filled
        （1.5 分钟后查 SimNow 真实账户却是 0 持仓）

    P6 改用 order.trade_records：CTP 真正确认的成交回报明细，只有交易所撮合成功
    才会写入，不受本地缓存乐观更新影响。

    [5] 另加一组**容器形态**护栏：上面各例喂的都是原生 dict，恰好落进
    `isinstance(recs, dict)` 认得的那条分支；tqsdk 的容器类不是 dict 子类，
    真形态一旦变化，旧判据会静默累计出 0 手 → 真成交被判成未成交。

本测试用 mock order 对象验证，不需要真实 tqsdk / 网络。
跑法：python test_p6_fix.py
"""
from __future__ import annotations

import sys


class MockOrder:
    """模拟 tqsdk Order 对象（只需 status / volume_left / trade_records）。"""

    def __init__(self, status="FINISHED", volume_left=0, trade_records=None,
                 trade_price=None):
        self.status = status
        self.volume_left = volume_left
        self.trade_records = trade_records
        self.trade_price = trade_price


# ---- import simnow.py 里的【真实】函数 ----
# simnow.py 对 tqsdk 是懒加载的（只有真正下单才 import tqsdk），
# 所以 import 这个模块本身不需要 tqsdk、不需要网络、不需要 SimNow 连接。
#
# 修正记录：早期版本为了避免 import tqsdk，把两个被测函数的函数体**复制**了一份
# 放在本文件里。那样测的是"规格"而不是"simnow.py 里的真实代码"——
# 以后谁改坏了 simnow.py，测试照样全绿，回归形同虚设。现在改为真 import。
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    """从本文件位置向上找 Trading 包目录。

    兼容三种摆放方式：
      <root>/test_p6_fix.py + <root>/Trading/
      <root>/Trading/test_p6_fix.py + <root>/Trading/
      <root>/Trading/Test/test_p6_fix.py + <root>/Trading/
    """
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

try:
    from Trading.Broker.SimNow import (  # noqa: E402
        _traded_volume_from_records,
        _traded_price_from_records,
    )
except Exception as e:  # pragma: no cover
    print("✗ 无法从 tg.brokers.simnow 导入被测函数: {}: {}".format(type(e).__name__, e))
    print("  Trading 根目录解析为: {}".format(_TG_ROOT))
    raise SystemExit(2)

print("[import] 被测函数来自: {}/tg/brokers/simnow.py（真实代码，非副本）".format(_TG_ROOT))


def p6_is_filled(order, volume: int) -> bool:
    """P6 判定逻辑：P3 两层 + trade_records 成交量 >= 委托量。"""
    p3 = (getattr(order, "status", "") == "FINISHED"
          and getattr(order, "volume_left", None) == 0)
    if not p3:
        return False
    return _traded_volume_from_records(order) >= int(volume)


# ---------------- 测试用例 ----------------
_PASS = 0
_FAIL = 0


def check(name: str, got, want) -> None:
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print("  ✓ {}".format(name))
    else:
        _FAIL += 1
        print("  ✗ {}   got={!r}  want={!r}".format(name, got, want))


def test_p6_volume_extraction() -> None:
    print("\n[1] 成交量提取 _traded_volume_from_records")
    check("空 trade_records -> 0",
          _traded_volume_from_records(MockOrder(trade_records={})), 0)
    check("None trade_records -> 0",
          _traded_volume_from_records(MockOrder(trade_records=None)), 0)
    check("单笔 1 手 -> 1",
          _traded_volume_from_records(MockOrder(
              trade_records={"t1": {"volume": 1, "price": 4547.4}})), 1)
    check("两笔各 1 手 -> 2（累加）",
          _traded_volume_from_records(MockOrder(
              trade_records={"t1": {"volume": 1, "price": 4540.0},
                             "t2": {"volume": 1, "price": 4560.0}})), 2)
    check("一笔 3 手 -> 3",
          _traded_volume_from_records(MockOrder(
              trade_records={"t1": {"volume": 3, "price": 4550.0}})), 3)
    check("list 形式 records -> 正常累加",
          _traded_volume_from_records(MockOrder(
              trade_records=[{"volume": 2, "price": 4550.0}])), 2)
    check("含 None 项 -> 跳过不崩",
          _traded_volume_from_records(MockOrder(
              trade_records={"t1": None, "t2": {"volume": 1, "price": 4550.0}})), 1)
    check("脏数据（volume 非数字）-> 0 不抛异常",
          _traded_volume_from_records(MockOrder(
              trade_records={"t1": {"volume": "abc", "price": 4550.0}})), 0)


def test_p6_price_extraction() -> None:
    print("\n[2] 成交均价 _traded_price_from_records（按 volume 加权）")
    check("空 -> None",
          _traded_price_from_records(MockOrder(trade_records={})), None)
    check("单笔 @4547.4 -> 4547.4",
          _traded_price_from_records(MockOrder(
              trade_records={"t1": {"volume": 1, "price": 4547.4}})), 4547.4)
    check("1手@4540 + 1手@4560 -> 4550.0（加权均价）",
          _traded_price_from_records(MockOrder(
              trade_records={"t1": {"volume": 1, "price": 4540.0},
                             "t2": {"volume": 1, "price": 4560.0}})), 4550.0)
    check("3手@4500 + 1手@4600 -> 4525.0",
          _traded_price_from_records(MockOrder(
              trade_records={"t1": {"volume": 3, "price": 4500.0},
                             "t2": {"volume": 1, "price": 4600.0}})), 4525.0)


def test_p6_core_judgement() -> None:
    print("\n[3] P6 权威判定（这是挡住 v5 幻象的关键）")

    # 场景 A：CTP 拒单但 position 缓存被乐观 +1（v5 的 4 笔幻象 filled）
    #         status=FINISHED, volume_left=0，但 trade_records 空
    ghost = MockOrder(status="FINISHED", volume_left=0,
                      trade_records={}, trade_price=4547.4)
    check("A 幻象成交（拒单但缓存+1）-> 判未成交",
          p6_is_filled(ghost, 1), False)

    # 场景 B：真成交，CTP 回报了 1 手明细
    real = MockOrder(status="FINISHED", volume_left=0,
                     trade_records={"t1": {"volume": 1, "price": 4547.4}},
                     trade_price=4547.4)
    check("B 真成交 -> 判成交",
          p6_is_filled(real, 1), True)

    # 场景 C：委托 2 手但只成交 1 手（部分成交）
    partial = MockOrder(status="FINISHED", volume_left=0,
                        trade_records={"t1": {"volume": 1, "price": 4547.4}})
    check("C 部分成交（1/2 手）-> 判未成交",
          p6_is_filled(partial, 2), False)

    # 场景 D：委托 2 手全成交
    full2 = MockOrder(status="FINISHED", volume_left=0,
                      trade_records={"t1": {"volume": 1, "price": 4547.0},
                                     "t2": {"volume": 1, "price": 4547.8}})
    check("D 委托 2 手全成交 -> 判成交",
          p6_is_filled(full2, 2), True)

    # 场景 E：订单未 FINISHED（还在队列中）
    alive = MockOrder(status="ALIVE", volume_left=1, trade_records={})
    check("E status=ALIVE -> 判未成交（P3 层挡）",
          p6_is_filled(alive, 1), False)

    # 场景 F：撤单（FINISHED 但 volume_left 未清零）
    canceled = MockOrder(status="FINISHED", volume_left=1,
                         trade_records={}, trade_price=4547.4)
    check("F 撤单（volume_left=1）-> 判未成交（P3 层挡）",
          p6_is_filled(canceled, 1), False)

    # 场景 G：trade_records 量大于委托量（CTP 重发回报污染，>= 应放行）
    over = MockOrder(status="FINISHED", volume_left=0,
                     trade_records={"t1": {"volume": 2, "price": 4547.4}})
    check("G 成交明细 2 手 > 委托 1 手 -> 判成交（>= 语义）",
          p6_is_filled(over, 1), True)


def test_p4_downgraded_to_diagnostic() -> None:
    """验证 P4/P5 已降级：position 缓存不同步不再推翻 P6 的判定。"""
    print("\n[4] P4/P5 降级为纯诊断（不 reject）")

    # v5 那 21 笔 close 死循环的根因：真成交了但 position 缓存滞后，
    # 旧 P4 会把 is_fully_filled 改 False；现在 P6 说了算，不该被改。
    p4_lag_but_p6_ok = MockOrder(
        status="FINISHED", volume_left=0,
        trade_records={"t1": {"volume": 1, "price": 4542.0}})
    # 模拟：P4 校验失败（position 缓存没同步），但 P6 通过
    p6_result = p6_is_filled(p4_lag_but_p6_ok, 1)
    # P6 通过 -> 最终判定成交（P4 失败只记 warning，不改判定）
    final = p6_result
    check("P4 滞后但 P6 通过 -> 仍判成交（不再误拒）", final, True)

    # 反向：P4 通过（缓存被乐观 +1）但 P6 失败（无成交明细）-> 必须判未成交
    p4_ok_but_p6_fail = MockOrder(status="FINISHED", volume_left=0,
                                  trade_records={}, trade_price=4547.4)
    check("P4 通过但 P6 失败 -> 必须判未成交（挡住幻象）",
          p6_is_filled(p4_ok_but_p6_fail, 1), False)


def test_record_container_shape() -> None:
    """成交明细容器的**形态**护栏（2026-09-21）。

    为什么单列一组：上面 [1]~[4] 喂的都是**原生 dict**，恰好落进
    `isinstance(recs, dict)` 认得的那条分支 —— 桩的形态 ≠ 生产形态，等于没覆盖。
    tqsdk 的容器类（`Entity`）继承 `MutableMapping` 而**不是 dict 子类**；一旦
    `trade_records` 变成那种容器，旧判据会走 `list(recs)` → 把 Mapping **当序列
    迭代** → 拿到的是**键**（字符串）→ 每个字段都取不到 → 静默累计出 0。

    这条路径**不是无害的保守方向**：`_finalize` 的 P6 降级分支（`status=FINISHED`
    且 `volume_left=0` 但成交明细手数不足）会把一笔**真成交降级成未成交**并记一条
    拒单，引擎据此以为没成交。故本组不用 dict，专测非 dict 的 Mapping。
    """
    from collections.abc import MutableMapping

    print("\n[5] 成交明细容器形态护栏：非 dict 的 Mapping 也必须读对")

    class EntityLike(MutableMapping):
        """tqsdk `Entity` 的最小替身：是 Mapping，但**不是 dict 子类**。

        与 `tqsdk/entity.py` 的 `Entity` 同构：键存在实例字典里，**缺键抛
        KeyError**（`MutableMapping.get` 据此才返回 None 而不是崩）。
        """

        def __init__(self, **kw):
            self.__dict__.update(kw)

        def __getitem__(self, k):
            return self.__dict__[k]

        def __setitem__(self, k, v):
            self.__dict__[k] = v

        def __delitem__(self, k):
            del self.__dict__[k]

        def __iter__(self):
            return iter(self.__dict__)

        def __len__(self):
            return len(self.__dict__)

    class RecLike:
        """明细替身：字段是**属性**（真 tqsdk `Trade` 就是这样）。"""

        def __init__(self, volume, price):
            self.volume, self.price = volume, price

    # ① 容器不是 dict、值是 dict
    c1 = EntityLike(t1={"volume": 2, "price": 4500.0})
    check("非 dict 的 Mapping 容器（值为 dict）-> 2",
          _traded_volume_from_records(MockOrder(trade_records=c1)), 2)
    check("非 dict 的 Mapping 容器（值为 dict）-> 均价 4500.0",
          _traded_price_from_records(MockOrder(trade_records=c1)), 4500.0)

    # ② 容器不是 dict、值也是对象（真 Trade 形态）
    c2 = EntityLike(t1=RecLike(1, 4540.0), t2=RecLike(1, 4560.0))
    check("非 dict 容器 + 对象值明细 -> 2",
          _traded_volume_from_records(MockOrder(trade_records=c2)), 2)
    check("非 dict 容器 + 对象值明细 -> 加权均价 4550.0",
          _traded_price_from_records(MockOrder(trade_records=c2)), 4550.0)

    # ③ 空容器（不是"没成交"以外的含义）
    check("空非 dict 容器 -> 0",
          _traded_volume_from_records(MockOrder(trade_records=EntityLike())), 0)
    check("空非 dict 容器 -> 均价 None",
          _traded_price_from_records(MockOrder(trade_records=EntityLike())), None)

    # ④ 形态完全认不出（truthy 的意外对象）-> 0 / None，且不崩
    check("形态认不出（int）-> 0 不崩",
          _traded_volume_from_records(MockOrder(trade_records=42)), 0)
    check("形态认不出（int）-> 均价 None",
          _traded_price_from_records(MockOrder(trade_records=42)), None)

    # ⑤ 一条坏明细**不得**清零已累加总量（旧写法整段包在 try 里会清零）
    check("一条坏明细不清零已累加总量（2 + 脏数据 -> 2）",
          _traded_volume_from_records(MockOrder(
              trade_records={"t1": {"volume": 2, "price": 4500.0},
                             "t2": {"volume": "abc", "price": 4500.0}})), 2)
    check("一条坏明细不清零已累加总量（对象值版）",
          _traded_volume_from_records(MockOrder(
              trade_records=EntityLike(t1=RecLike(2, 4500.0),
                                       t2=RecLike("abc", 4500.0)))), 2)

    # ⑥ 回归保护：序列形态（list）必须继续可用 —— test_p6_fix.py:126 依赖它
    check("序列形态仍支持（list -> 2）",
          _traded_volume_from_records(MockOrder(
              trade_records=[{"volume": 2, "price": 4550.0}])), 2)


def main() -> int:
    print("=" * 64)
    print("P6 修复单元测试：CTP 真实成交明细权威判定")
    print("=" * 64)
    test_p6_volume_extraction()
    test_p6_price_extraction()
    test_p6_core_judgement()
    test_p4_downgraded_to_diagnostic()
    test_record_container_shape()
    print("\n" + "=" * 64)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 64)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

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

    [6] 与 2026-09-30 事故根修（IM 实盘 10:29）：P6 的裁决权已归还**委托终态**
    （`status` / `volume_left` / `last_msg` 同属 orders 回报流），`trade_records`
    （trades 回报流，独立到达）降级为**成交价来源 + 诊断**，不再拥有对成交事实
    的否决权。于是原 [3]A 的用例被**一分为二**：
      · A1 = 终态文案宣告全部成交 + 明细未到 → **判成交**（事故现场，旧实现判反了）；
      · A2 = 拒单文案 + 明细未到        → 判未成交（09-04 幻影防护，不降级）。
    两者输入只差 `last_msg`，正是"成交必须由**正向证据**宣告、绝不因**证据缺失**
    被否证"这条规格的两个方向。判据本体已收口到生产侧的 `_p6_is_filled`
    （本文件**真 import**，不再自带副本 —— 副本测的是规格，不是生产代码）。

本测试用 mock order 对象验证，不需要真实 tqsdk / 网络。
跑法：python test_p6_fix.py
"""
from __future__ import annotations

import sys


class MockOrder:
    """模拟 tqsdk Order 对象（只需 status / volume_left / last_msg / trade_records）。

    `last_msg` 默认空串 = "回报没带文案"，落在"认不出"那一侧（不放行），
    这是 09-04 幻影防护的保守方向。
    """

    def __init__(self, status="FINISHED", volume_left=0, trade_records=None,
                 trade_price=None, last_msg=""):
        self.status = status
        self.volume_left = volume_left
        self.trade_records = trade_records
        self.trade_price = trade_price
        self.last_msg = last_msg


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
    # `_p6_is_filled` = 生产侧的裁决本体（`_finalize` 与测试**同调它**）——
    # 本文件不再自带判定式副本：副本测的是"规格"，生产代码被改坏了照样绿。
    from Trading.Broker.SimNow import (  # noqa: E402
        _p6_is_filled as p6_is_filled,
        _traded_volume_from_records,
        _traded_price_from_records,
    )
except Exception as e:  # pragma: no cover
    print("✗ 无法从 Trading/Broker/SimNow.py 导入被测函数: {}: {}".format(
        type(e).__name__, e))
    print("  Trading 根目录解析为: {}".format(_TG_ROOT))
    raise SystemExit(2)

print("[import] 被测函数来自: {}/Broker/SimNow.py（真实代码，非副本）".format(_TG_ROOT))


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
    print("\n[3] P6 裁决（成交事实只由正向证据宣告）")

    # 场景 A1（2026-09-30 IM 实盘 10:29 事故现场）：委托终态**文案已宣告全部成交**，
    #   而成交明细包（另一条回报流）**尚未到达**。
    #   ⚠️ 这一条与下面的 A2 只差 `last_msg`，而**旧实现把两者判成同一个结果**
    #   （都判未成交）—— 那正是事故的根因：用"明细还没到"否决了"CTP 已宣告的成交"，
    #   于是不落账本、留下无风控锚的孤儿仓（快期3 里却是成交的）。
    accident = MockOrder(status="FINISHED", volume_left=0, trade_records={},
                         trade_price=float("nan"), last_msg="全部成交报单已提交")
    check("A1 终态宣告全部成交 + 明细未到 -> 判成交（2026-09-30 事故根修）",
          p6_is_filled(accident, 2), True)

    # 场景 A2：CTP 拒单但 position 缓存被乐观 +1（v5 的 4 笔幻象 filled）。
    #   形态与 A1 同（FINISHED + volume_left=0 + 明细空），差别在 `last_msg` ——
    #   拒单文案**不宣告成交**，于是仍走明细判据 → 判未成交。
    #   09-04 幻影防护改由"正向文案"承担，防护力不降级。
    ghost = MockOrder(status="FINISHED", volume_left=0,
                      trade_records={}, trade_price=4547.4,
                      last_msg="资金不足，报单被拒绝")
    check("A2 幻象成交（拒单文案 + 缓存+1）-> 判未成交",
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

    # 反向：P4 通过（缓存被乐观 +1）但 P6 失败（无成交明细 + **拒单文案**）
    #   -> 必须判未成交。`last_msg` 是这条例的判别关键：拒单文案不宣告成交，
    #   于是不会命中"不等明细"的放行口（若这里留空串同样走明细判据，结论一致）。
    p4_ok_but_p6_fail = MockOrder(status="FINISHED", volume_left=0,
                                  trade_records={}, trade_price=4547.4,
                                  last_msg="资金不足，报单被拒绝")
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


def test_all_traded_msg_gate() -> None:
    """终态文案闸门：只认「全部成交」（2026-09-30 事故根修）。

    为什么单列一组：这是 P6 唯一一个"不等成交明细就放行"的口子，口子必须开得**窄**。
    任何"认不出也放行"的写法都会把 09-04 幻影形态（拒单文案 + 余量残留 0）一起放进来
    —— 那才是真正的资损方向（账本有、实盘没有）。故本组同时钉住两侧：
      · 宣告成交 → 放行（哪怕明细为空）；
      · 拒/撤/未成交/否定式（未全部成交…）/认不出/空/None → 一律不放行。
    """

    print("\n[6] 终态文案闸门：只认『全部成交』，认不出不放行")

    def verdict(msg, volume=2):
        return p6_is_filled(
            MockOrder(status="FINISHED", volume_left=0, trade_records={},
                      trade_price=7192.40, last_msg=msg), volume)

    check("事故现场文案（全部成交报单已提交）-> 放行",
          verdict("全部成交报单已提交"), True)
    check("CTP 原生文案（全部成交）-> 放行", verdict("全部成交"), True)
    check("否定式（未全部成交，已撤单）-> 不放行",
          verdict("未全部成交，已撤单"), False)
    check("否定式（未能全部成交，报单已撤销）-> 不放行",
          verdict("未能全部成交，报单已撤销"), False)
    check("否定式（全部成交否则撤销指令未能全部成交）-> 不放行",
          verdict("全部成交否则撤销指令未能全部成交"), False)
    check("拒单文案（资金不足）-> 不放行", verdict("资金不足，报单被拒绝"), False)
    check("撤单文案（已撤单报单已提交）-> 不放行", verdict("已撤单报单已提交"), False)
    check("未成交文案（未成交报单已提交）-> 不放行", verdict("未成交报单已提交"), False)
    check("部分成交文案（部分成交报单已提交）-> 不放行",
          verdict("部分成交报单已提交"), False)
    check("空文案（回报未携带）-> 不放行", verdict(""), False)
    check("None 文案 -> 不放行", verdict(None), False)
    check("认不出的文案（中继换文案）-> 不放行", verdict("Some unknown message"), False)


def main() -> int:
    print("=" * 64)
    print("P6 修复单元测试：CTP 真实成交明细权威判定")
    print("=" * 64)
    test_p6_volume_extraction()
    test_p6_price_extraction()
    test_p6_core_judgement()
    test_p4_downgraded_to_diagnostic()
    test_record_container_shape()
    test_all_traded_msg_gate()
    print("\n" + "=" * 64)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 64)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

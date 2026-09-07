# -*- coding: utf-8 -*-
"""
test_simnow_guards：SimNow broker 读仓守卫纯 mock 单测（不连网）
=================================================================
覆盖 2026-09-07 对账加固后的 `_channel_unstable` / `_quote_stale` /
`real_position` / `_position_total` 各分支：

  [1] api 未初始化            → 不稳定，real_position=None
  [2] wait_update 抛异常（断连）→ 不稳定，real_position=None
  [3] 行情新鲜 + 持仓正常      → 稳定，real_position 精确读数
  [4] 行情陈旧（停滞 10 分钟）  → 不稳定，real_position=None（本测试核心场景：
      断连重连窗口读仓，修复前会读到 0 导致引擎误清持仓）
  [5] 行情 datetime 为空（首帧未到）→ 不稳定
  [6] _position_total 跨品种收窄：dict 缺本合约但其他合约有仓 → 返回 0（修复前
      会把别的合约误当本合约读）
  [7] _position_total 精确匹配正常读数

运行方式（独立脚本，非 pytest）：
    /c/my_chan_project/.venv/Scripts/python.exe test_simnow_guards.py
退出码 0 = 全部通过；非 0 = 有断言失败。

说明：被测对象是**同包的 Broker/SimNow.py**（本测试随包放在 Trading/Test/ 下，
与被测文件同仓库），通过 exec 加载其源码、复用 Trading 包解析相对导入；
注册装饰器临时 no-op，避免与仓库已注册的同名 broker 冲突。
"""
import os
import sys
import time
import types

# ---- 定位仓库根（Trading 包所在目录）：从本文件向上找，可用 CHAN_REPO 覆盖 ----
_REPO = os.environ.get("CHAN_REPO") or ""
if not _REPO:
    _p = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(_p, "Trading")):
            _REPO = _p
            break
        _p = os.path.dirname(_p)
if _REPO:
    sys.path.insert(0, _REPO)

import Trading.Broker.Base as _base_mod          # noqa: E402  (触发包初始化)
from Trading.Infra.Types import Side              # noqa: E402

# ---- 以 Trading.Broker 的包上下文 exec 加载同仓库的加固版 SimNow.py ----
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SN_PATH = os.path.join(_PKG_DIR, "Broker", "SimNow.py")

_orig_register = _base_mod.register_broker
_base_mod.register_broker = lambda cls: cls       # 测试加载期 no-op，防注册冲突
try:
    _mod = types.ModuleType("Trading.Broker.SimNowUnderTest")
    _mod.__package__ = "Trading.Broker"
    _mod.__name__ = "Trading.Broker.SimNowUnderTest"
    _mod.__file__ = _SN_PATH
    with open(_SN_PATH, encoding="utf-8") as f:
        _src = f.read()
    exec(compile(_src, _SN_PATH, "exec"), _mod.__dict__)
finally:
    _base_mod.register_broker = _orig_register

SimNowBroker = _mod.SimNowBroker
_position_total = _mod._position_total

_PASS, _FAIL = 0, 0


def check(cond: bool, label: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  PASS  " + label)
    else:
        _FAIL += 1
        print("  FAIL  " + label)


def _dt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class FakeQuote:
    def __init__(self, dt: str):
        self.datetime = dt


class FakePos:
    def __init__(self, lt=0, lh=0, st=0, sh=0):
        self.pos_long_today, self.pos_long_his = lt, lh
        self.pos_short_today, self.pos_short_his = st, sh


class FakeApi:
    """模拟 tqsdk TqApi 的最小接口（wait_update / get_quote / get_position）。"""

    def __init__(self, quote, fail_wait=False, positions=None):
        self._quote = quote
        self._fail_wait = fail_wait
        self._positions = positions or {}

    def wait_update(self, deadline=None):
        if self._fail_wait:
            raise RuntimeError("websocket connection lost")
        return True

    def get_quote(self, symbol):
        return self._quote

    def get_position(self, symbol=None):
        if symbol:
            return self._positions.get(symbol)
        return dict(self._positions)


def _make(api: FakeApi, quote) -> SimNowBroker:
    b = SimNowBroker.__new__(SimNowBroker)   # 跳过 __init__（不连网、不查凭据）
    b._api = api
    b._quote = quote
    b._trade_symbol = "CFFEX.IF2509"
    return b


NOW = time.time()
FRESH = FakeQuote(_dt(NOW - 2))          # 2 秒前的 tick → 新鲜
STALE = FakeQuote(_dt(NOW - 600))        # 10 分钟前的 tick → 陈旧
EMPTY = FakeQuote("")                    # 首帧未到
BROKEN = FakeQuote("not-a-date")         # 格式异常


def main() -> None:
    print("== test_simnow_guards ==")

    print("[1] api 未初始化")
    b = _make(None, None)
    check(b._channel_unstable() is True, "api=None → 不稳定")
    check(b.real_position(Side.LONG) is None, "api=None → real_position=None")

    print("[2] wait_update 抛异常（断连）")
    b = _make(FakeApi(FRESH, fail_wait=True), FRESH)
    check(b._channel_unstable() is True, "wait_update 异常 → 不稳定")
    check(b.real_position(Side.LONG) is None, "断连 → real_position=None")

    print("[3] 行情新鲜 + 持仓正常")
    pos = {"CFFEX.IF2509": FakePos(lt=2, sh=1)}
    b = _make(FakeApi(FRESH, positions=pos), FRESH)
    check(b._channel_unstable() is False, "行情新鲜 → 稳定")
    check(b.real_position(Side.LONG) == 2, "多仓精确读数 = 2")
    check(b.real_position(Side.SHORT) == 1, "空仓精确读数 = 1")

    print("[4] 行情陈旧（停滞 10 分钟，误清事故的核心场景）")
    b = _make(FakeApi(STALE, positions=pos), STALE)
    check(b._channel_unstable() is True, "行情停滞 >30s → 不稳定")
    check(b.real_position(Side.LONG) is None,
          "陈旧行情下 real_position=None（引擎对账跳过，不误清）")

    print("[5] 行情 datetime 为空 / 格式异常")
    b = _make(FakeApi(EMPTY, positions=pos), EMPTY)
    check(b._channel_unstable() is True, "datetime 空（首帧未到）→ 不稳定")
    b = _make(FakeApi(BROKEN, positions=pos), BROKEN)
    check(b._channel_unstable() is True, "datetime 格式异常 → 不稳定")

    print("[6] _position_total 跨品种收窄")
    cross = {"CFFEX.IM2509": FakePos(lt=5)}          # 只有别的品种有仓
    check(_position_total(None, "CFFEX.IF2509", "LONG") == -1,
          "api 异常 → -1")
    api = FakeApi(FRESH, positions=cross)
    check(_position_total(api, "CFFEX.IF2509", "LONG") == 0,
          "dict 缺本合约（其他品种有仓）→ 返回 0，不跨品种误读")

    print("[7] _position_total 精确匹配")
    api = FakeApi(FRESH, positions={"CFFEX.IF2509": FakePos(lt=2, lh=1, st=0, sh=3)})
    check(_position_total(api, "CFFEX.IF2509", "LONG") == 3, "今+昨多仓 = 3")
    check(_position_total(api, "CFFEX.IF2509", "SHORT") == 3, "今+昨空仓 = 3")

    print("== {} pass / {} fail ==".format(_PASS, _FAIL))
    sys.exit(0 if _FAIL == 0 else 1)


if __name__ == "__main__":
    main()

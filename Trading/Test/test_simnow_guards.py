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
  [8] 帧级空闲泵积压观测（P3 可观测性，实现见 Broker/SimNow._observe_pump_backlog）
      → api 缺 `_recv_chan` / 取值为 None / qsize 抛异常，三种取数失败一律不抛、
        不计数；队列恒空 → 恒零告警；连续积压达阈值 → 恰好一条 WARNING 且计数归零
        （按阈值节流不刷屏）；积压清零 → 计数归零；`__init__` 未跑的实例
        （`__new__` 构造，本文件 _make 的造法）不因缺字段崩。

运行方式（独立脚本，非 pytest）：
    /c/my_chan_project/.venv/Scripts/python.exe test_simnow_guards.py
退出码 0 = 全部通过；非 0 = 有断言失败。

说明：被测对象是**同包的 Broker/SimNow.py**（本测试随包放在 Trading/Test/ 下，
与被测文件同仓库），通过 exec 加载其源码、复用 Trading 包解析相对导入；
注册装饰器临时 no-op，避免与仓库已注册的同名 broker 冲突。
"""
import logging
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
from Trading.Config import BrokerConfig           # noqa: E402  Step 2.3: _make 注入 params
from Trading.Infra.Records import Side  # noqa: E402


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
    """模拟 tqsdk TqApi 的最小接口（wait_update / get_quote / get_position）。

    recv_chan / has_recv_chan 仅供 [8] 积压观测用：前者是 `_recv_chan` 的替身，
    后者模拟「该属性压根不存在」的 api 实现（tqsdk 在 _setup_connection 之前
    也没有可用队列）。
    """

    def __init__(self, quote, fail_wait=False, positions=None, recv_chan=None,
                 has_recv_chan=True):
        self._quote = quote
        self._fail_wait = fail_wait
        self._positions = positions or {}
        if has_recv_chan:
            self._recv_chan = recv_chan

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


class FakeChan:
    """`_recv_chan` 替身：真实现是 asyncio.Queue 的子类，qsize 即队列 len，O(1)。"""

    def __init__(self, size=0):
        self.size = size

    def qsize(self):
        return self.size


class BadChan:
    """qsize 抛异常的替身（内部状态被并发破坏等）——观测必须吞掉它。"""

    def qsize(self):
        raise RuntimeError("recv_chan qsize exploded")


class _CapHandler(logging.Handler):
    """捕获 tg.brokers.simnow 的日志记录，供 [8] 断言「恰好打了几条」。"""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def count(self, keyword):
        return sum(1 for m in self.messages if keyword in m)


def _make(api: FakeApi, quote) -> SimNowBroker:
    b = SimNowBroker.__new__(SimNowBroker)   # 跳过 __init__（不连网、不查凭据）
    b._api = api
    b._quote = quote
    b._trade_symbol = "CFFEX.IF2509"
    # Step 2.3：_timing() 严格读取 channel 时序参数（如 quote_stale_seconds），
    # __new__ 跳过 __init__ 后 params 缺失 → 注入完整 BrokerConfig 快照。
    b.params = BrokerConfig().model_dump()
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

    print("[8] 帧级空闲泵积压观测（纯读：不改行为、不抛、按阈值节流）")
    _STREAK = _mod._PUMP_BACKLOG_WARN_STREAK
    _lg = logging.getLogger("tg.brokers.simnow")
    _cap = _CapHandler()
    _old_level, _old_propagate = _lg.level, _lg.propagate
    _lg.addHandler(_cap)
    _lg.setLevel(logging.WARNING)
    _lg.propagate = False          # 断言期间不回灌 root，输出保持干净
    try:
        # (a) 取不到队列（属性缺失 / 值为 None）→ 不观测、不抛、不计数
        b = _make(FakeApi(FRESH, has_recv_chan=False), FRESH)
        b.pulse(0)
        check(True, "api 无 _recv_chan → pulse 不抛")
        b = _make(FakeApi(FRESH, recv_chan=None), FRESH)
        b.pulse(0)
        check(True, "_recv_chan=None → pulse 不抛")
        check(getattr(b, "_pump_backlog_streak", 0) == 0, "取不到队列 → 不计入积压")

        # (b) qsize 抛异常 → 吞掉（观测绝不允许反过来影响保活）
        b = _make(FakeApi(FRESH, recv_chan=BadChan()), FRESH)
        b.pulse(0)
        b.pulse(0)
        check(True, "qsize 抛异常 → pulse 不抛")
        check(getattr(b, "_pump_backlog_streak", 0) == 0, "取数失败 → 不计入积压")

        # (c) 队列恒空（日常态）→ 计数恒 0、零告警
        b = _make(FakeApi(FRESH, recv_chan=FakeChan(0)), FRESH)
        for _ in range(_STREAK * 2):
            b.pulse(0)
        check(getattr(b, "_pump_backlog_streak", 0) == 0, "队列恒空 → 计数恒 0")
        check(_cap.count("回报泵") == 0, "队列恒空 → 零告警（不刷屏）")

        # (d) 连续积压：阈值前静默 → 达阈值恰好一条 → 计数归零（节流）
        b = _make(FakeApi(FRESH, recv_chan=FakeChan(3)), FRESH)
        for _ in range(_STREAK - 1):
            b.pulse(0)
        check(_cap.count("回报泵") == 0,
              "连续 {} 帧积压 → 尚未告警".format(_STREAK - 1))
        check(b._pump_backlog_streak == _STREAK - 1,
              "计数 = {}".format(_STREAK - 1))
        b.pulse(0)
        check(_cap.count("回报泵") == 1, "第 {} 帧 → 恰好一条 WARNING".format(_STREAK))
        check(b._pump_backlog_streak == 0, "告警后计数归零")
        b.pulse(0)
        b.pulse(0)
        check(_cap.count("回报泵") == 1, "后续 2 帧仍只有 1 条（节流，未刷屏）")

        # (e) 积压清零 → 计数归零（不残留历史）
        ch = FakeChan(5)
        b = _make(FakeApi(FRESH, recv_chan=ch), FRESH)
        for _ in range(3):
            b.pulse(0)
        check(b._pump_backlog_streak == 3, "积压 3 帧 → 计数 3")
        ch.size = 0
        b.pulse(0)
        check(b._pump_backlog_streak == 0, "积压清零 → 计数归零")

        # (f) __init__ 未跑的实例（本文件 _make 的造法）本就没有计数字段 → 兜底不抛
        b = _make(FakeApi(FRESH, recv_chan=FakeChan(1)), FRESH)
        check(not hasattr(b, "_pump_backlog_streak"), "前置：_make 实例无计数字段")
        b.pulse(0)
        check(getattr(b, "_pump_backlog_streak", None) == 1, "缺字段 → 兜底计为 1")
    finally:
        _lg.removeHandler(_cap)
        _lg.setLevel(_old_level)
        _lg.propagate = _old_propagate

    print("== {} pass / {} fail ==".format(_PASS, _FAIL))
    sys.exit(0 if _FAIL == 0 else 1)


if __name__ == "__main__":
    main()

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
  [9] gateway 日志配置（`Trading/main._setup_logging`）：配好之后 `tg` 必须放行
      INFO、handler 只装一份（幂等）、且 INFO 埋点真的写进当前 stdout
      （子进程里即 gateway.log）。此前全树无 logging 配置，"仪器没通电"。
  [10] 冒烟脚本的读数纪律（静态护栏）：`Trading/Test/smoke_simnow_phase_g.py`
      里不得出现 `real_position(...) or 0` —— `None` 是 falsy，会被抹成 0，
      于是"开仓前读失败"与"平仓后读失败"互相抵消，断言退化成空断言，
      冒烟打绿而实际一手指仓都没读到。必须走 `_read_real_position` 带上"可信"标志。
  [11] 读数不可信的**唯一出口** = None（2026-09-21 收敛）：`real_position` 的
      四个失败成因（未连接 / 通道不稳 / 读数抛异常 / 形态或取值不可识别）
      必须全部返回 None，且 `_position_total` / `_take_baseline` /
      `_take_baseline_split` 不得再返回 -1 / 0 / (-1,-1) 这类数字哨兵
      （数字哨兵会被漏挡的调用方当手数算下去）。

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
    check(_position_total(None, "CFFEX.IF2509", "LONG") is None,
          "api 异常 → None（不可信，不是 -1）")
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

    print("[9] gateway 日志配置：INFO 埋点必须真的落盘（2026-09-21）")
    # 事故：交易子进程**全树没有 logging 配置** → root 停在 WARNING、handlers
    # 为空 → 所有 `logger.info(...)` 被静默丢弃。专为诊断写的埋点一行都没出现过
    # （`otg_latency:` 回报链路时延 / `在线通道已连接` / `登录后持仓镜像初读`），
    # 「回报滞后 25~55s 还在不在」因此一直拿不出数据收口。
    # 本组把"埋点落盘"变成可执行断言：配好之后 INFO 必须真的写进当前 stdout
    # （子进程里 = {out}/gateway.log）。
    import io
    from contextlib import redirect_stdout
    from Trading import main as _gw_main

    _gw_main._setup_logging()
    _tg = logging.getLogger("tg")
    check(_tg.isEnabledFor(logging.INFO) is True, "配置后 tg 放行 INFO")
    check(any(getattr(h, "_gw_log_handler", False) for h in _tg.handlers) is True,
          "tg 已装落盘 handler")
    _n_handlers = len(_tg.handlers)
    _gw_main._setup_logging()                      # 幂等：不叠加（build_runtime 会多次调）
    check(len(_tg.handlers) == _n_handlers, "重复调用不叠加 handler")

    _buf = io.StringIO()
    with redirect_stdout(_buf):
        logging.getLogger("tg.brokers.simnow").info("otg_latency: probe 探针")
    check("otg_latency: probe 探针" in _buf.getvalue(),
          "INFO 埋点确实写进当前 stdout（子进程里 = gateway.log）")

    print("[10] 冒烟脚本读数纪律：失败值不得被抹成 0（AST 静态护栏）")
    # 事故形态（2026-09-21）：`real_position` 失败时返回 None（未连接 / 通道不稳）
    # 或 -1（形态认不出）。`None or 0` → 0 —— 于是 --trade 路径里 149/150 与
    # 170/171 两侧四次读数全失败时互相抵消，172 行断言 `(lp2,sp2)==(bl_l,bl_s)`
    # 恒成立（实测两种失败都过），冒烟报绿而实际一手指仓都没读到。
    #
    # 护栏走 **AST** 而不是正则：注释与 docstring 对 AST 不可见，所以
    #   ① 解释性文字（包括本护栏自己的说明）不会误伤；
    #   ② 只有真正会被执行的 `... or 0` 才算违规 —— 与「注释不算证据」同一口径。
    import ast
    _smoke = os.path.join(_PKG_DIR, "Test", "smoke_simnow_phase_g.py")
    with open(_smoke, encoding="utf-8") as f:
        _smoke_tree = ast.parse(f.read(), _smoke)

    def _masked_calls(tree):
        """返回 `SomeObj.real_position(...) or 0` 这类表达式的行号列表。"""
        hits = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.BoolOp) or not isinstance(n.op, ast.Or):
                continue
            if len(n.values) != 2:
                continue
            left, right = n.values
            if not (isinstance(left, ast.Call)
                    and isinstance(left.func, ast.Attribute)
                    and left.func.attr == "real_position"):
                continue
            if isinstance(right, ast.Constant) and right.value == 0:
                hits.append(n.lineno)
        return hits

    check(_masked_calls(_smoke_tree) == [],
          "冒烟脚本没有可执行代码把 real_position 的失败值抹成 0")
    # 正向对照：同一条检测对事故写法必须命中，否则这条护栏只是恒真的摆设
    check(_masked_calls(ast.parse("lp = b.real_position(Side.LONG) or 0")) == [1],
          "护栏有判别力（命中 `real_position(...) or 0`）")
    check(_masked_calls(ast.parse(
        '"""说明：real_position(Side.LONG) or 0 会抹掉 None"""\nx = 1')) == [],
          "注释/docstring 不算证据（AST 看不见，不误伤）")

    _defs = {n.name for n in ast.walk(_smoke_tree)
             if isinstance(n, ast.FunctionDef)}
    _called = {n.func.id for n in ast.walk(_smoke_tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("_read_real_position" in _defs,
          "冒烟脚本定义了 `_read_real_position`（把「读数是否可信」带出来）")
    check("_read_real_position" in _called, "并且 --trade 路径真的调用了它")

    print("[11] 读数不可信的**唯一**出口 = None（收敛掉 -1 数字哨兵）")
    # 病根：同一个概念"读数不可信"曾有两种表示 —— None（未连接/通道不稳/抛异常）
    #   与 -1（形态或取值认不出）。只挡 None 的调用方把 -1 当数字算
    #   （`engine_vol - (-1)` = 比账本多平一手 → 整侧仓单被删 + 补记虚构盈亏）；
    #   只挡 <0 的调用方把 None 放进算术（TypeError 或静默跳过）。
    #   本组把"非负 int = 可信 / None = 不可信"钉成契约，并禁止数字哨兵回流。
    class _BadShape:
        """形态认不出：既没有持仓字段，也不是 Mapping（真异常形态的替身）。"""

    class RaiseApi(FakeApi):
        def get_position(self, symbol=None):
            raise RuntimeError("get_position exploded")

    class ShapeApi(FakeApi):
        def __init__(self, bad):
            super().__init__(FRESH)
            self._bad = bad

        def get_position(self, symbol=None):
            return self._bad

    # (a) _position_total：三种不可信 → 一律 None
    check(_position_total(None, "CFFEX.IF2509", "LONG") is None,
          "api 异常 → None")
    check(_position_total(RaiseApi(FRESH), "CFFEX.IF2509", "LONG") is None,
          "get_position 抛异常 → None")
    check(_position_total(ShapeApi(_BadShape()), "CFFEX.IF2509", "LONG") is None,
          "返回值形态认不出 → None")
    check(_position_total(ShapeApi({"CFFEX.IF2509": FakePos(lt=-1)}),
                          "CFFEX.IF2509", "LONG") is None,
          "取值畸形（负手数）→ None")
    # 对照：可信读数必须原样给数字，且"无仓"的 0 不能被混成 None
    check(_position_total(FakeApi(FRESH, positions={"CFFEX.IF2509": FakePos(lt=2)}),
                          "CFFEX.IF2509", "LONG") == 2, "可信读数 → 原样 2")
    check(_position_total(FakeApi(FRESH, positions={}), "CFFEX.IF2509", "LONG") == 0,
          "缺本合约（可信的『无仓』）→ 0，不是 None")

    # (b) real_position：四个失败成因 → 全 None（同一个信号）
    check(_make(None, None).real_position(Side.LONG) is None,
          "成因①未连接 → None")
    check(_make(FakeApi(STALE, positions={"CFFEX.IF2509": FakePos(lt=2)}),
                STALE).real_position(Side.LONG) is None,
          "成因②通道不稳（行情停滞）→ None")
    check(_make(RaiseApi(FRESH), FRESH).real_position(Side.LONG) is None,
          "成因③读数抛异常 → None")
    check(_make(ShapeApi(_BadShape()), FRESH).real_position(Side.LONG) is None,
          "成因④形态或取值认不出 → None（旧实现这里是 -1）")
    _rb = _make(FakeApi(FRESH, positions={"CFFEX.IF2509": FakePos(lt=2, lh=1)}), FRESH)
    check(_rb.real_position(Side.LONG) == 3, "★ 可信读数不受影响")
    check(_rb.real_position(Side.LONG) >= 0, "★ 非 None 的返回值恒为非负")

    # (c) 基线出口：异常时必须是 None，不能是 0（0 是"该侧确实无仓"的可信语义）
    _bb = _make(FakeApi(FRESH, fail_wait=True), FRESH)
    check(_bb._take_baseline("LONG") is None,
          "★ _take_baseline 异常 → None（旧实现返回 0，等于断言『该侧 0 手』）")
    check(_bb._take_baseline_split("LONG") is None,
          "★ _take_baseline_split 异常 → None（旧实现返回 (-1, -1)）")
    _ok_api = FakeApi(FRESH, positions={"CFFEX.IF2509": FakePos(lt=2, lh=1)})
    _okb = _make(_ok_api, FRESH)
    check(_okb._take_baseline("LONG") == 3, "_take_baseline 正常路径 = 3")
    check(_okb._take_baseline_split("LONG") == (2, 1),
          "_take_baseline_split 正常路径 = (2, 1)")

    # (d) 防回流（AST 静态护栏）：持仓取数路径不得再出现数字哨兵返回值。
    #     走 AST 而不是正则 —— 注释/docstring 里提到 "-1" 不算违规。
    import ast
    _sn_tree = ast.parse(_src, _SN_PATH)

    def _sentinel_returns(tree):
        hits = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Return) or n.value is None:
                continue
            v = n.value
            neg = (isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.USub)
                   and isinstance(v.operand, ast.Constant) and v.operand.value == 1)
            neg_tuple = (isinstance(v, ast.Tuple) and v.elts and all(
                isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.USub)
                and isinstance(e.operand, ast.Constant) and e.operand.value == 1
                for e in v.elts))
            if neg or neg_tuple:
                hits.append(n.lineno)
        return hits

    _sentinels = _sentinel_returns(_sn_tree)
    check(_sentinels == [],
          "SimNow.py 无 `return -1` / `return (-1, ...)` 数字哨兵（命中行 {}）"
          .format(_sentinels))
    # 正向对照：这条检测对老写法必须命中，否则只是摆设
    check(len(_sentinel_returns(ast.parse("def f():\n    return -1\n"))) == 1,
          "护栏有判别力（命中 `return -1`）")
    check(_sentinel_returns(ast.parse("def f():\n    return None\n")) == [],
          "对 `return None` 不误伤")

    # (e) 同源入口（Tool 脚本）：负数不得被当成"持仓已归零"。
    #     `SimNowForceClose` 的成交校验原是 `cur == 0 or (cur is not None and
    #     cur <= 0)`，恒等于 `cur == 0 or cur < 0` —— 负读数被当成"归零成功"，
    #     工具会报"成交+持仓校验通过"。本护栏禁止该比较再次出现。
    _fc = os.path.join(_PKG_DIR, "Tool", "SimNow", "SimNowForceClose.py")
    with open(_fc, encoding="utf-8") as f:
        _fc_tree = ast.parse(f.read(), _fc)

    def _lt_zero_on_cur(tree):
        hits = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Compare) or len(n.ops) != 1:
                continue
            if not isinstance(n.ops[0], (ast.Lt, ast.LtE)):
                continue
            if isinstance(n.left, ast.Name) and n.left.id == "cur":
                hits.append(n.lineno)
        return hits

    check(_lt_zero_on_cur(_fc_tree) == [],
          "ForceClose 无 `cur < 0` / `cur <= 0`（负数不得当『归零成功』）")
    check(len(_lt_zero_on_cur(ast.parse("if cur <= 0:\n    pass\n"))) == 1,
          "护栏有判别力（命中 `cur <= 0`）")

    print("== {} pass / {} fail ==".format(_PASS, _FAIL))
    sys.exit(0 if _FAIL == 0 else 1)


if __name__ == "__main__":
    main()

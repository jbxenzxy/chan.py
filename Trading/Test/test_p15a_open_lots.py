# -*- coding: utf-8 -*-
"""
P15a 一笔报单开仓测试（2026-09-16 第三轮改写）
================================================
本文件原来测的是两样**已被重构删除**的东西：
  · `cfg.risk.max_open_positions`（同时持仓笔数上限）—— D2 判定删除：
    "资金是唯一闸门"，同向笔数门连同 `open_silenced` 事件一起消失；
  · `eng._open_position(sig, side, N)` 直接调 —— Phase 4 删除，开仓唯一路径改为
    `on_signal` → `_decide_action` → `_pre_trade_check` → `_execute` → `_book_open`。
  另 `RiskConfig.unlock_no_new_open` 随"解锁"概念一并删除（D17 丢弃旧键但不静默）。

⚠️ 2026-09-16（第三轮）**再改写一次**：用户拍板 —— **"删掉 `risk.max_volume`，
   表第 3 列就是用来替换这个的"**。故本文件里所有"改风控手数"的手法全部换成
   **"改品种执行策略表第 3 列"**（用 `dataclasses.replace` 造一份改过表的品种档案）。
   手数旋钮从此只有一个，**本文件的写法本身就是"唯一来源"的示范**。

新口径（本测试锁死）
    [1] 术语纪律：config 无 sizing 键、**风控层无任何手数旋钮**；`RiskConfig`
        字段集**受控**（无已删键、也不得出现预期外的新字段）；三个已删键
        （`max_open_positions` / `unlock_no_new_open` / **`max_volume`**）
        按 D17 丢弃 + 可观测。
    [2] 一笔报单挂 N 手：**表第 3 列 = N** → broker **恰好 1 单 N 手**、簿
        **1 笔 N 手**、事件里恰好 1 条 order + 1 条 open（不是 N 单，也不是
        1 笔拆 N 笔）。
    [2b] **改表即生效**（用户 ⑵ 的核心诉求）：表值 3/7/20 手时实开就是那么多
        —— 不再被任何风控上限"取小"压住（旧口径 `min(risk.max_volume, 表值)`
        会让"改表 N"不生效，而启动横幅只显示表值 → 唯一可见处反而误导）。
        另锁：`lots_per_order` **每次实时读表**（会话中换表立即可见）+ 无品种档案 → 1 手。
    [3] 手数 1..20 的构造期 fail-fast：**原 `RiskConfig` 的校验已迁到
        `ExecPolicy.__post_init__`**（手数真值源既然变成表，约束就必须长在表上，
        dry_run 与实盘不能出现"超限照常成交" vs "被 CTP 拒单"的行为分歧）。
    [4] 拒单路径：全场拒 → 簿空 / `account_state()==FLAT` / `_state==IDLE` /
        signal_action=rejected，且**不留幻影持仓**。
    [5] 无分仓残留 + 唯一报单出口存在性（A3）+ **手数只剩一条通道**：
        `lots_per_signal` / `max_volume` / `CZCE` 在**可执行代码**里零残留
        （token 级判定 —— 注释与文档串里的历史说明不算残留，那是给后人读的）。

不需要真实 tqsdk / 网络；纯单测 + RejectDryBroker mock 测拒单路径。
跑法：python Trading/Test/test_p15a_open_lots.py
"""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
import sys
import tempfile
import tokenize
from contextlib import contextmanager
from dataclasses import replace as _dc_replace

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
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
    print("\u2717 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/Test/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p15a_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from Trading import Broker  # noqa: E402  注册 dry_run
from Trading.Broker.Base import OrderIntent  # noqa: E402
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, RiskConfig, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.StateDB import Store  # noqa: E402

from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
from Trading.Infra.Instrument import Instrument  # noqa: E402
from Trading.Infra.Product import (  # noqa: E402
    EXEC_POLICY, FAK, FOK, PRODUCT_PROFILES, R_OPEN, ExecPolicy,
)

_IF = PRODUCT_PROFILES["IF"]
from Trading.Infra.Records import AccountState, Bar, EngineState, Signal  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


class RejectDryBroker(DryRunBroker):
    """DryRunBroker 子类，可指定拒单次数。0=全过、1=首笔拒、-1=全拒。

    2026-09-11：`submit` 签名已加到 8 参（多出 entry_date / is_exit，D12/D13），
    子类必须同步，否则 TypeError 会被引擎当成 channel 故障。
    """
    def __init__(self, spec, params=None, *, reject_first_n=0):
        super().__init__(spec, params)
        self.reject_first_n = reject_first_n
        self._calls = 0

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        self._calls += 1
        if self.reject_first_n == -1 or self._calls <= self.reject_first_n:
            from Trading.Infra.Records import Order
            o = Order(
                order_id="reject-{:06d}".format(self._calls),
                signal_key=signal_key, symbol=self.state.trade_symbol,
                side=side,
                action="open" if intent is OrderIntent.OPEN else "close",
                volume=int(volume),
                price=0.0, req_price=float(ref_price),
                filled_price=None, status="rejected",
                created_at="2026-09-01 09:30", broker=self.name, note=note,
                meta={"intent": intent.value if hasattr(intent, "value") else str(intent),
                      "entry_date": entry_date,
                      "reject_reason": "test_reject"})
            self.orders.append(o)
            return o
        return super().submit(intent, side, volume, ref_price, signal_key, note,
                              entry_date, is_exit)


def profile_with_lots(code="IF", lots=None):
    """品种档案，可把**执行策略表第 3 列**换成 `lots`。

    2026-09-16：这就是单测里"改表"的等价物 —— `ExecPolicy` / `Product` 都是
    `frozen=True`，改表 = 换一份档案对象（生产路径是改 `Infra/Product.py` 的
    `EXEC_POLICY` 源码或重跑生成器，运行期不可就地改，见 [3k]）。
    `lots=None` = 直接用**真实表值**（不打任何补丁）。
    """
    prof = PRODUCT_PROFILES[code]
    if lots is None:
        return prof
    return _dc_replace(prof, exec_policy=_dc_replace(prof.exec_policy,
                                                     lots_per_order=int(lots)))


_EXCHANGE_PREFIX = {"IF": "CFFEX", "IH": "CFFEX", "IC": "CFFEX", "IM": "CFFEX",
                    "AU": "SHFE", "AG": "SHFE", "CU": "SHFE", "TA": "CZCE"}


def _symbol_of(code):
    """品种键 → 主连符号（只服务本测试构造 cfg，不参与任何判据）。"""
    return "KQ.m@{}.{}".format(_EXCHANGE_PREFIX.get(code, "CFFEX"), code)


def make_engine(tmpdir, *, code="IF", lots=None, broker_cls=None, reject_first_n=0):
    """构造引擎。

    2026-09-11：不再设 `max_open_positions` / `unlock_no_new_open`（D2/D17 已删）。
    2026-09-16：不再设 `risk.max_volume`（该旋钮已删）—— **开仓手数的唯一来源
    = 品种执行策略表第 3 列**，要改手数就 `lots=N` 改表（见 `profile_with_lots`）。
    ⚠️ broker 与 engine 必须共用**同一份** `spec`：引擎的 `self.state` 默认沿用
    `broker.state`，手数就是从这里实时读表取到的。

    🆕 2026-09-16 A 批 ⑶-b：**cfg 的品种必须跟着 `code` 走**。
      原来这里固定 `TradingConfig.from_dict(DEFAULT_CONFIG)`（signal_symbol = IF），
      却在 `spec` 上传别的品种档案（如 TA）来测"手数来自表" —— 这个状态在生产
      路径上**不可能出现**（main.py 把同一份 cfg 同时交给 Broker 与引擎），
      而引擎构造期现在会断言「白名单放行的品种 == 运行时档案的品种」
      （`_assert_product_ssot`）→ "cfg 说 IF、档案说 TA"的构造被当场拒绝启动。
      故让 cfg 同源：要测"手数来自表"，改的是**档案里的表行**（`lots`），
      而不是让 cfg 与档案互相矛盾 —— 后者本来就在测一个不存在的状态。
    """
    _base = copy.deepcopy(DEFAULT_CONFIG)
    _base["instrument"]["signal_symbol"] = _symbol_of(code)
    cfg = TradingConfig.from_dict(_base)

    spec = Instrument(None, profile_with_lots(code, lots))
    if broker_cls is None:
        broker = DryRunBroker(spec, {"sim_equity": 10_000_000.0})
    else:
        broker = broker_cls(spec, {"sim_equity": 10_000_000.0},
                            reject_first_n=reject_first_n)
    entry = EntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def read_events(eng, tail_n=400):
    """读事件日志尾部 → [(kind, 整个 dict), ...]（flush 后再读，保证不漏）。"""
    eng.ev.flush()
    out = []
    try:
        with open(eng.ev.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return out
    for line in lines[-tail_n:]:
        try:
            d = json.loads(line)
        except Exception:
            continue
        out.append((d.get("kind"), d))
    return out


def kinds_of(eng, tail_n=400):
    return [k for k, _d in read_events(eng, tail_n)]


def make_sig(key="P15A-TEST|0|0", is_buy=True, price=4550.0, low=4540.0, high=4560.0):
    return Signal(
        key=key, symbol="KQ.m@CFFEX.IF", freq="5m",
        date="2026-09-01 09:30", timestamp=4000,
        bsp_type="B" if is_buy else "S", is_buy=is_buy,
        price=price, high=high, low=low, extra={})


def make_bar(date="2026-09-01 09:30", close=4550.0):
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=4000, vol=0)


def _mk_risk(**kw):
    """构造 RiskConfig，返回 (实例 or None, 异常串 or None)。"""
    try:
        return RiskConfig(**kw), None
    except Exception as e:
        return None, "{}: {}".format(type(e).__name__, str(e).replace("\n", " ")[:200])


def _mk_pol(**kw):
    """构造 ExecPolicy，返回 (实例 or None, 异常串 or None)。"""
    kw.setdefault("today_exit", R_OPEN)
    kw.setdefault("order_advanced", FOK)
    kw.setdefault("lots_per_order", 2)
    try:
        return ExecPolicy(**kw), None
    except Exception as e:
        return None, "{}: {}".format(type(e).__name__, str(e).replace("\n", " ")[:200])


def _code_tokens(path):
    """Python 文件的**可执行 token** 序列（剔除注释 / 文档串 / 空白）。

    用途：判定"某个旧符号在代码里是否还有残留"。直接用 `in src` 会把
    **注释与 docstring 里的历史说明**也算成残留（那些是刻意保留给后人读的
    变更记录），token 级判定才反映"代码是否还依赖它"。
    """
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    skip = {tokenize.COMMENT, tokenize.STRING, tokenize.NL,
            tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}
    return [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in skip]


# ════════════════════════════════════════════════════════════════
# [1] 术语纪律：仓位管理已删 + D2/D17/第三轮已删键
# ════════════════════════════════════════════════════════════════
print("\n[1] 术语纪律：仓位管理已删 + 已删键（含 max_volume）")
check("[1a] config 无 sizing 键（PositionSizing 整体删除）",
      "sizing" in DEFAULT_CONFIG, False)
# Phase 11 新增 delivery_guard_days（交割月护栏阈值）；2026-09-16 又删掉 max_volume。
# 原断言 "字段集 == {...}" 等于把字段清单写死 —— 任何**有意**增删都会误报。
# 拆成三条：保住原意（无已删键 + 字段体积不失控 + 手数旋钮为零），
# 新增字段只需同步 _RISK_KNOWN 一行，属"有意确认"而非漏改。
_RISK_KNOWN = {"delivery_guard_days"}
_DROPPED = ["max_open_positions", "max_volume", "unlock_no_new_open"]
_risk_fields = set(RiskConfig.model_fields)
check("[1b] RiskConfig 无已删键（D2 max_open_positions / D17 unlock_no_new_open）",
      sorted(_risk_fields & {"max_open_positions", "unlock_no_new_open"}), [])
check("[1b2] RiskConfig 字段集不超出已知清单（新增字段须同步本行）",
      sorted(_risk_fields - _RISK_KNOWN), [])
check("[1b3] RiskConfig 已无 max_volume（手数旋钮已删 → 真值源 = 品种执行策略表第 3 列）",
      "max_volume" in _risk_fields, False)
check("[1c] DEFAULT_CONFIG.risk 无 max_open_positions",
      "max_open_positions" in (DEFAULT_CONFIG.get("risk") or {}), False)
check("[1d] DEFAULT_CONFIG.risk 无 unlock_no_new_open",
      "unlock_no_new_open" in (DEFAULT_CONFIG.get("risk") or {}), False)
check("[1e] DEFAULT_CONFIG.risk 无 max_volume（默认配置里也没有第二个手数旋钮）",
      "max_volume" in (DEFAULT_CONFIG.get("risk") or {}), False)

# D17：旧键按"丢弃 + 可观测"处理（不静默、也不 fail-fast）
RiskConfig.dropped_legacy_keys.clear()
legacy_cfg, legacy_err = _mk_risk(delivery_guard_days=5, max_open_positions=3,
                                  unlock_no_new_open=True, max_volume=2)
check("[1f] 带三个已删键的配置仍能构造（不 fail-fast）", legacy_err, None)
check("[1g] 其余合法键原样保留（delivery_guard_days=5）",
      (legacy_cfg.delivery_guard_days if legacy_cfg else None), 5)
check("[1h] 丢弃动作**可观测**（dropped_legacy_keys 记账，含 max_volume）",
      sorted(set(RiskConfig.dropped_legacy_keys)), _DROPPED)
# 但 extra="forbid" 仍在：真正不认识的键必须报错（否则拼错键名会被静默吞掉）
_bogus, _bogus_err = _mk_risk(delivery_guard_days=1, max_open_position=3)
check_true("[1i] 未列入白名单的未知键仍 fail-fast（extra=forbid）",
           _bogus_err is not None and "ValidationError" in _bogus_err)

check("[1j] config.broker_params 无 open_advanced 键",
      "open_advanced" in (DEFAULT_CONFIG.get("broker_params") or {}), False)
check("[1k] config.broker_params 无 overprice_points_fok 键",
      "overprice_points_fok" in (DEFAULT_CONFIG.get("broker_params") or {}), False)
check("[1l] 超价合并为单参数 overprice_ticks=5（IF=1.0 点）",
      (DEFAULT_CONFIG.get("broker_params") or {}).get("overprice_ticks"), 5)


# ════════════════════════════════════════════════════════════════
# [2] 一笔报单挂 N 手（唯一开仓路径：on_signal）
# ════════════════════════════════════════════════════════════════
print("\n[2] 一笔报单挂 N 手（表第 3 列 = N → 1 单 N 手）")
for _n in (1, 2):
    with tmp_dir() as td:
        eng = make_engine(td, lots=_n)
        eng.on_bar(make_bar())
        sig = make_sig(key="P15A-2-{}".format(_n))
        eng.on_signal(sig)

        check("[2] N={}：broker 恰好 1 单（不是 N 单）".format(_n),
              len(eng.broker.orders), 1)
        check("[2] N={}：该单 {} 手".format(_n, _n), eng.broker.orders[0].volume, _n)
        check("[2] N={}：簿 1 笔（不是拆成 N 笔）".format(_n), len(eng.positions), 1)
        check("[2] N={}：该笔 {} 手".format(_n, _n),
              eng.positions.positions[0].volume, _n)
        check("[2] N={}：signal_key 无 #idx 后缀".format(_n),
              eng.positions.positions[0].signal_key, sig.key)
        check("[2] N={}：lots_per_order 就取自品种执行策略表第 3 列".format(_n),
              eng.lots_per_order, _n)
        check("[2] N={}：signal_action=opened".format(_n),
              eng.store.signal_action(sig.key), "opened")
        check("[2] N={}：account_state=RUNNING".format(_n),
              eng.account_state(), AccountState.RUNNING)
        check("[2] N={}：净敞口 = {}".format(_n, _n),
              eng.positions.net_volume(), _n)
        check("[2] N={}：_state=IN_TRADE".format(_n), eng._state, EngineState.IN_TRADE)

        # 事件账：1 条 order + 1 条 open（"一笔报单"在事件层同样成立）
        evs = read_events(eng)
        ks = [k for k, _d in evs]
        check("[2] N={}：事件里恰好 1 条 order".format(_n), ks.count("order"), 1)
        check("[2] N={}：事件里恰好 1 条 open".format(_n), ks.count("open"), 1)
        _o = [d for k, d in evs if k == "order"][0]
        check("[2] N={}：order.volume={}".format(_n, _n), _o.get("volume"), _n)
        check("[2] N={}：order.transition=1（空仓开新仓）".format(_n),
              _o.get("transition"), 1)

# ════════════════════════════════════════════════════════════════
# [2b] 「改表即生效」—— 表第 3 列是**唯一**手数来源，不再被风控上限取小
# ════════════════════════════════════════════════════════════════
# 2026-09-16（用户第 3 轮 ⑵）：原实现 = min(risk.max_volume, 表第 3 列)，
# 于是"改表 N"不生效、而启动横幅只打表值 —— 唯一可见处反而误导。删掉
# risk.max_volume 后，表值就是实开手数。下面用 3/7/20 手（> 旧默认 2 手）
# 直接验证"表说了算"。
print("\n[2b] 改表即生效：表值 1/3/7/20 手 → 实开就是那么多手")
for _n in (1, 3, 7, 20):
    with tmp_dir() as td:
        eng = make_engine(td, lots=_n)
        check("[2b] 表值 {} 手：lots_per_order == {}".format(_n, _n),
              eng.lots_per_order, _n)
        check("[2b] 表值 {} 手：_open_volume() == {}".format(_n, _n),
              eng._open_volume(), _n)
        eng.on_bar(make_bar())
        eng.on_signal(make_sig(key="P15A-2b-{}".format(_n)))
        check("[2b] ★ 表 {} 手 → 实报 1 单 {} 手（没有被压到 2 手）".format(_n, _n),
              eng.broker.orders[0].volume, _n)
        check("[2b] ★ 表 {} 手 → 簿 1 笔 {} 手".format(_n, _n),
              eng.positions.positions[0].volume, _n)

# 每次实时读表（无缓存）：会话中换表 → 立刻可见
with tmp_dir() as td:
    eng = make_engine(td, lots=2)
    check("[2c] 初始：表 2 手 → lots_per_order=2", eng.lots_per_order, 2)
    # 模拟"会话中改表"：整份换掉引擎持有的品种档案（ExecPolicy 是 frozen，
    # 换表 = 换对象）。若手数像旧实现那样在 __init__ 里算成 self.lots_per_signal，
    # 这里读到的还会是 2。
    eng.state._product = profile_with_lots("IF", 5)
    check("[2c] ★ 换表后立刻读到 5（property 每次实时读表，无缓存镜像）",
          eng.lots_per_order, 5)
    check("[2c] _open_volume() 同步为 5", eng._open_volume(), 5)

# 无品种档案 → 保守 1 手（宁可少开，不按猜出来的手数下单）
#
# 2026-09-16 A 批 ⑵：原版这里是"引擎侧刻意不给品种档案"
#   （`state=Instrument(None, None)`）—— 这条路在**引擎层已不可达**：品种既然
#   过了白名单（IF 在册），运行时对象就必须拿到档案，否则 `_assert_product_ssot`
#   在引擎构造期直接拒绝启动（见下面 [2d2]）。
#   兜底逻辑本身**仍在代码里**（`lots_per_order` 的 `pol is None → 1`），
#   故改为**运行期摘档案**来验证（与上面 [2c] 换档案同一手法）。
with tmp_dir() as td:
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    _bk = DryRunBroker(Instrument(None, _IF), {"sim_equity": 10_000_000.0})
    eng = TradingEngine(
        cfg, _bk, EntryPolicy({"reverse_on_opposite_signal": False}),
        LayeredExitPolicy(), Store(os.path.join(td, "state.db")),
        EventLog(os.path.join(td, "events.jsonl"), echo=False, echo_kinds=None),
        state=Instrument(None, _IF))
    eng.state._product = None            # ← 运行期摘掉档案（模拟未标定品种）
    check("[2d] 兜底仍在：无品种档案 → 保守 1 手"
          "（与「档案缺失时派生值一律取保守侧」同一约定；"
          "原举的 exchange 未标定 → '' 一例随该字段于 B 批删除而不再存在）",
          eng.lots_per_order, 1)
    check("[2d] _open_volume() 同为 1", eng._open_volume(), 1)

# 启动期硬失败（A 批 ⑵）：品种在册却没拿到档案 → 拒绝启动，不再静默走保守侧
with tmp_dir() as td:
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    _bk = DryRunBroker(Instrument(None, None), {"sim_equity": 10_000_000.0})
    _err = ""
    try:
        TradingEngine(
            cfg, _bk, EntryPolicy({"reverse_on_opposite_signal": False}),
            LayeredExitPolicy(), Store(os.path.join(td, "state.db")),
            EventLog(os.path.join(td, "events.jsonl"), echo=False, echo_kinds=None),
            state=Instrument(None, None))
    except ValueError as e:
        _err = str(e)
    check("[2d2] ★ 引擎侧刻意不给档案 → 拒绝启动（而非静默按 1 手下真单）",
          "品种档案缺失" in _err, True)

# 代码不看交易所名字：CZCE(TA) 的 1 手来自**表**，不是"按交易所硬编码"
with tmp_dir() as td:
    eng = make_engine(td, code="TA", lots=None)
    check("[2e] TA 用**真实表值**：FAK 品种 → 1 手（不是按 CZCE 名字钉的）",
          eng.lots_per_order, 1)
    check("[2e] TA 的手数 == 表第 3 列",
          eng.lots_per_order, EXEC_POLICY["TA"].lots_per_order)

# 同向第二信号：D2 删掉的是"笔数静默门"，但**规则 ⑶（运行态不响应信号）仍在**
#   —— 已持仓（net≠0 → RUNNING）时第二信号被整条忽略，不是被笔数上限挡掉。
#   两者的可观测区别：旧口径写 open_silenced，新口径写 signal_skip/running_ignore_signal。
with tmp_dir() as td:
    eng = make_engine(td, lots=2)
    eng.on_bar(make_bar())
    s1 = make_sig(key="P15A-2-c")
    eng.on_signal(s1)
    check("[2m] 首信号：簿 1 笔 2 手", len(eng.positions), 1)
    s2 = make_sig(key="P15A-2-d")
    eng.on_signal(s2)
    check("[2n] 运行态第二信号（规则 ⑶）→ 不开新仓（仍 1 笔）",
          len(eng.positions), 1)
    check("[2o] 运行态第二信号 → 净敞口不变（仍 2）",
          eng.positions.net_volume(), 2)
    check("[2p] 运行态第二信号 → signal_action=skip",
          eng.store.signal_action(s2.key), "skip")
    check("[2q] 运行态第二信号 → 簿内 signal_key 仍只有第一笔",
          sorted(p.signal_key for p in eng.positions.positions), ["P15A-2-c"])
    _ks2 = kinds_of(eng)
    check_true("[2r] 运行态第二信号 → 写 signal_skip 事件",
               "signal_skip" in _ks2)
    _skip = [d for k, d in read_events(eng) if k == "signal_skip"]
    check("[2s] 跳过原因 = running_ignore_signal（不是笔数上限）",
          (_skip[-1].get("reason") if _skip else None), "running_ignore_signal")
    check_true("[2t] 事件里无 open_silenced（D2 已删该事件）",
               "open_silenced" not in _ks2)


# ════════════════════════════════════════════════════════════════
# [3] 手数 1..20 构造期校验（原 RiskConfig 的职责已迁 ExecPolicy）
# ════════════════════════════════════════════════════════════════
print("\n[3] 手数 1..20 构造期 fail-fast（原 risk.max_volume 校验已迁品种执行策略表）")
_r1, _e1 = _mk_pol(lots_per_order=1)
check("[3a] lots_per_order=1 合法（下界）", _e1, None)
_r20, _e20 = _mk_pol(lots_per_order=20)
check("[3b] lots_per_order=20 合法（上界 = 中金所限价单单笔上限）", _e20, None)
_r0, _e0 = _mk_pol(lots_per_order=0)
check_true("[3c] lots_per_order=0 → 构造期报错", _e0 is not None)
_r21, _e21 = _mk_pol(lots_per_order=21)
check_true("[3d] lots_per_order=21 → 构造期报错（取代已删的运行期拦截）",
           _e21 is not None)
check_true("[3e] 报错信息点明 1..20 区间",
           _e21 is not None and "1..20" in _e21)
# FAK ⟹ N == 1（放开 N 会破坏「待报 / 全成 / 全撤」三态不变量）
_rf, _ef = _mk_pol(order_advanced=FAK, lots_per_order=2)
check_true("[3f] FAK + 2 手 → 构造期报错（FAK 一笔必须 1 手）", _ef is not None)
_rf1, _ef1 = _mk_pol(order_advanced=FAK, lots_per_order=1)
check("[3g] FAK + 1 手 合法", _ef1, None)
# 真实表逐行复核（表值本身也是数据，同样要过约束）
check("[3h] 真实 EXEC_POLICY 全表手数均落在 1..20",
      sorted(c for c, p in EXEC_POLICY.items() if not 1 <= p.lots_per_order <= 20), [])
check("[3i] 真实表里 FAK 品种手数恒 1",
      sorted(c for c, p in EXEC_POLICY.items()
             if p.order_advanced == FAK and p.lots_per_order != 1), [])
# 表行 frozen：运行期不能就地改手数 —— "改表"是源码/生成器层面的事
try:
    EXEC_POLICY["IF"].lots_per_order = 99
    _froze = "MUTATED"
except Exception:
    _froze = "FROZEN"
check("[3j] ExecPolicy 行 frozen：运行期不可就地改手数", _froze, "FROZEN")
# 诚实记录：pydantic 未开 validate_assignment，**构造之后**的属性赋值绕过校验。
# 生产路径恒走 `TradingConfig.from_dict`（即构造期），故不构成实际风险；
# 这里钉住现状，避免"以为赋值也会被拦"的错觉。
_c = TradingConfig.from_dict(DEFAULT_CONFIG)
try:
    _c.risk.delivery_guard_days = 99
    _post = _c.risk.delivery_guard_days
except Exception:
    _post = "RAISED"
check("[3k] 现状记录：属性赋值不经校验（仅构造期生效）", _post, 99)


# ════════════════════════════════════════════════════════════════
# [4] 拒单路径（不留幻影持仓）
# ════════════════════════════════════════════════════════════════
print("\n[4] 拒单路径")
with tmp_dir() as td:
    eng = make_engine(td, lots=3, broker_cls=RejectDryBroker, reject_first_n=-1)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-r")
    eng.on_signal(sig)
    check("[4a] 全场拒单：簿空（无幻影持仓）", eng.positions.is_empty(), True)
    check("[4b] 全场拒单：净敞口 0", eng.positions.net_volume(), 0)
    check("[4c] 全场拒单：account_state=FLAT", eng.account_state(), AccountState.FLAT)
    check("[4d] 全场拒单：_state=IDLE", eng._state, EngineState.IDLE)
    check("[4e] 全场拒单：signal_action=rejected",
          eng.store.signal_action(sig.key), "rejected")
    _ks = kinds_of(eng)
    check_true("[4f] 全场拒单：写 order_rejected 事件", "order_rejected" in _ks)
    check("[4g] 全场拒单：不写 open 事件", _ks.count("open"), 0)

# 首笔拒 + 第二笔过：拒单不污染后续
with tmp_dir() as td:
    eng = make_engine(td, lots=2, broker_cls=RejectDryBroker, reject_first_n=1)
    eng.on_bar(make_bar())
    s1 = make_sig(key="P15A-4-s1")
    eng.on_signal(s1)
    check("[4h] 首笔拒：簿空", eng.positions.is_empty(), True)
    s2 = make_sig(key="P15A-4-s2")
    eng.on_signal(s2)
    check("[4i] 第二笔过：簿 1 笔 2 手", len(eng.positions), 1)
    check("[4j] 第二笔过：净敞口 2", eng.positions.net_volume(), 2)
    check("[4k] 第二笔过：signal_action=opened",
          eng.store.signal_action(s2.key), "opened")


# ════════════════════════════════════════════════════════════════
# [5] 无分仓残留 + 唯一报单出口（A3）+ 手数只剩一条通道
# ════════════════════════════════════════════════════════════════
print("\n[5] 无分仓残留 + 唯一报单出口 + 手数单通道")
import Trading.Engine.Engine as _engine_mod  # noqa: E402
_ENGINE_PY = os.path.join(_TG_ROOT, "Engine", "Engine.py")
_src = open(_ENGINE_PY, encoding="utf-8").read()
_TE = _engine_mod.TradingEngine
check("[5a] engine 无 _open_position（Phase 4 已删，开仓唯一路径走 _execute）",
      hasattr(_TE, "_open_position"), False)
check("[5b] engine 无 _close_position（Phase 4 已删）",
      hasattr(_TE, "_close_position"), False)
check("[5c] engine 无 _open_positions（复数，E3.2 批次开仓已删）",
      hasattr(_TE, "_open_positions"), False)
check("[5d] engine 无 _book_positions 方法", hasattr(_TE, "_book_positions"), False)
check("[5e] engine 无 _unlock_position（「解锁」概念已删）",
      hasattr(_TE, "_unlock_position"), False)
for _m in ("_unlock_round_entry", "_check_unlock_round", "_unlock_round_settle"):
    check("[5f] engine 无 {} 方法".format(_m), hasattr(_TE, _m), False)
check("[5g] engine 源码无 unlock_round 残留", "unlock_round" in _src, False)
check("[5h] engine 源码无 open_silenced 残留", "open_silenced" in _src, False)

# A3：唯一报单出口 + 决策/落账分层
for _m in ("_execute", "_book_open", "_book_close",
           "_decide_action", "_decide_exit", "_pre_trade_check", "account_state"):
    check("[5i] engine 有 {}".format(_m), hasattr(_TE, _m), True)
check("[5j] engine 有 _sync_state（_state 派生镜像刷新点）",
      hasattr(_TE, "_sync_state"), True)

# ── 手数只剩一条通道（用户第 3 轮 ⑵ 的护栏）────────────────────
check("[5k] engine 无 lots_per_signal 镜像属性（手数不再有第二来源）",
      hasattr(_TE, "lots_per_signal"), False)
check("[5l] engine 有 lots_per_order property（表第 3 列的实时只读转发）",
      isinstance(getattr(_TE, "lots_per_order", None), property), True)
_toks = _code_tokens(_ENGINE_PY)
for _sym in ("lots_per_signal", "max_volume", "CZCE"):
    check("[5m] ★ 引擎**可执行代码**（token 级，剔注释/文档串）无 {} 残留".format(_sym),
          _sym in _toks, False)


print("\n" + "=" * 60)
print("P15a 一笔报单开仓 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

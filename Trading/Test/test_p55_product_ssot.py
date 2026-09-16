# -*- coding: utf-8 -*-
"""
P55 品种档案 SSOT + exec_policy 类型收紧（2026-09-16 · A 批 ⑵+⑶-b）
=====================================================================
背景（用户 2026-09-16 提出的两条结构清理，逐字保留原始主张）
--------------------------------------------------------------
⑵ 「`exec_policy` 在构造期解析一次并 fail-fast：现在 `Instrument.exec_policy`
   返回 `Optional[Any]`，引擎里 `getattr(self.state, "exec_policy", None)` 是
   不可达的静默降级 —— 正是禁止的跨文件 fallback 味道。收敛成一个普通属性，
   `Any` 也一并收紧。」

⑶-b「会计侧 Product 来源收敛到 `self.state.product` 一处（`cfg.product_profile`
   只在启动期用，否则会出现「按 AU 决策、按 IF 记账」）。」

为什么这不是"洁癖"，而是可发生的账错
------------------------------------
`cfg.product_profile` 是**实时查表的 property**（`Config.py`：
`PRODUCT_PROFILES.get(parse_product_key(cfg.instrument.signal_symbol))`，
每次调用都重新解析）；`Instrument._product` 是**构造期冻结**的那一份。
两者恒等只靠一个约定 ——「main.py 的 `--symbol` 配置重建发生在 Instrument
构造之前」。约定一破，症状就是**决策按 A 品种、记账按 B 品种**：
乘数错 → PnL 错，费率档错 → 成本错，且**完全静默**（没有任何告警）。

覆盖
--------------------------------------------------------------------------
  [1] `Instrument.cost_cash` 签名：**没有** product 形参（结构上消灭分叉入口）
  [2] `Instrument.exec_policy`：类型收紧（注解含 ExecPolicy、不含 Any）；
      已标定 → ExecPolicy 实例；未标定 → None
  [3] 启动期 fail-fast 三条（都**拒绝启动**，不是告警）：
        a. 品种在册但 `state.product is None` → 「品种档案缺失」
        b. cfg 与 state **品种分叉**（cfg=IF / 档案=AU）→ 「品种来源分叉」
        c. 品种在册但 `exec_policy is None`（EXEC_POLICY 漏行 / 字段被改名）
           → 「缺少执行策略行」
  [4] 正面路径：正常 cfg + 档案 → 启动成功，且 `_assert_product_ssot` 已执行
  [5] ★ 运行期零次读 `cfg.product_profile`（AST 静态证明：Engine 只允许
      `__init__` 出现一次，Reconcile 一次都不许）—— 这是"只在启动期用"的
      **可执行定义**，不是注释里的一句话
  [6] `cost_cash` 口径不变（公式锚点）：对 IF/AU/TA × 平今/平昨 × 多手数，
      与 `Fee.cash` 显式组合逐值相等；未标定品种 → 0.0（等价旧调用点的
      `… if p is not None else 0.0`）
  [7] 旧符号零残留（防回潮）：`getattr(…, "exec_policy"…)`、
      `cost_cash(p,` / `cost_cash(_p,` 全仓零命中

跑法：python Trading/Test/test_p55_product_ssot.py
"""
from __future__ import annotations

import ast
import copy
import inspect
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace as _dc_replace

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(
                os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
_REPO = os.path.dirname(_TG_ROOT)
sys.path.insert(0, _REPO)

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Instrument import Instrument  # noqa: E402
from Trading.Infra.Product import (  # noqa: E402
    EXEC_POLICY, PRODUCT_PROFILES, ExecPolicy)
from Trading.Infra.StateDB import Store  # noqa: E402
from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_IF = PRODUCT_PROFILES["IF"]
_AU = PRODUCT_PROFILES["AU"]

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="tg_p55_%s_" % tag)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_cfg(signal_symbol="KQ.m@CFFEX.IF"):
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["instrument"]["signal_symbol"] = signal_symbol
    base["exit_params"].update({"use_atr": False, "use_trailing": False})
    return TradingConfig.from_dict(base)


def build_engine(tmpdir, cfg, spec, tag="a", store=None):
    return TradingEngine(
        cfg, DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        EntryPolicy({}), LayeredExitPolicy(),
        store or Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                 echo=False, echo_kinds=None))


def expect_startup_error(tmpdir, cfg, spec, tag, must_contain):
    """构造引擎并捕获启动期异常；返回异常消息（没抛则返回 "<no-raise>"）。"""
    try:
        build_engine(tmpdir, cfg, spec, tag)
    except ValueError as e:
        return ("OK" if must_contain in str(e) else "WRONG_MSG: " + str(e)[:200])
    except Exception as e:  # noqa: BLE001
        return "WRONG_TYPE {}: {}".format(type(e).__name__, str(e)[:160])
    return "<no-raise>"


# ══════════════════════════════════════════════════════════════
print("\n[1] cost_cash 签名：没有 product 形参（分叉入口在结构上不存在）")
# ══════════════════════════════════════════════════════════════
_sig = inspect.signature(Instrument.cost_cash)
_params = list(_sig.parameters)
check("[1a] 形参名与顺序", _params,
      ["self", "entry_price", "exit_price", "closetoday", "volume"])
check_true("[1b] ★ 形参里没有 product（旧签名 cost_cash(product, …) 已消失）",
           "product" not in _params, _params)
check("[1c] 默认值：volume=1（closetoday 无默认 = 必须显式声明今/昨）",
      (_sig.parameters["volume"].default,
       _sig.parameters["closetoday"].default), (1, inspect.Parameter.empty))


# ══════════════════════════════════════════════════════════════
print("\n[2] Instrument.exec_policy：类型收紧 + 取值路径")
# ══════════════════════════════════════════════════════════════
_ann = str(Instrument.exec_policy.fget.__annotations__.get("return", ""))
check_true("[2a] ★ 返回注解含 ExecPolicy", "ExecPolicy" in _ann, _ann)
check_true("[2b] ★ 返回注解不含 Any（Optional[Any] 已收紧）",
           "Any" not in _ann, _ann)
check_true("[2c] 已标定品种 → ExecPolicy 实例（不是 Any 包裹的鸭子）",
           isinstance(Instrument(None, _IF).exec_policy, ExecPolicy))
check("[2d] 未标定品种（离线探针）→ None", Instrument(None, None).exec_policy, None)
check("[2e] 表与档案同源（取到的就是 EXEC_POLICY 那一行对象）",
      Instrument(None, _IF).exec_policy is EXEC_POLICY["IF"], True)


# ══════════════════════════════════════════════════════════════
print("\n[3] 启动期 fail-fast 三条（拒绝启动，不是告警、不是静默降级）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("ff") as td:
    # a. 品种在册（IF 过白名单）但运行时对象没拿到档案
    cfg_a = make_cfg("KQ.m@CFFEX.IF")
    spec_a = Instrument(cfg_a.instrument, None)
    check("[3a] state.product is None → 品种档案缺失",
          expect_startup_error(td, cfg_a, spec_a, "a", "品种档案缺失"), "OK")

    # b. cfg 按 IF 放行，运行时对象却持 AU 档案（真实的「按 AU 决策、按 IF 记账」入口）
    cfg_b = make_cfg("KQ.m@CFFEX.IF")
    spec_b = Instrument(cfg_b.instrument, _AU)
    check("[3b] ★ cfg=IF / 档案=AU → 品种来源分叉（这是账错入口，必须拦死在启动期）",
          expect_startup_error(td, cfg_b, spec_b, "b", "品种来源分叉"), "OK")

    # c. 品种在册但 EXEC_POLICY 漏了这一行（用 replace 模拟"表漏行/字段被改名"）
    cfg_c = make_cfg("KQ.m@CFFEX.IF")
    prof_no_pol = _dc_replace(_IF, exec_policy=None)
    spec_c = Instrument(cfg_c.instrument, prof_no_pol)
    check("[3c] exec_policy is None → 缺少执行策略行",
          expect_startup_error(td, cfg_c, spec_c, "c", "缺少执行策略行"), "OK")

    # d. 正面路径：正常的 cfg + 档案 → 启动成功
    cfg_d = make_cfg("KQ.m@CFFEX.IF")
    spec_d = Instrument(cfg_d.instrument, _IF)
    eng = build_engine(td, cfg_d, spec_d, "d")
    check_true("[3d] 正常路径：启动成功", eng is not None)
    check("[3e] 引擎的档案就是传入那一份（不是按 cfg 另查一份）",
          eng.state.product is _IF, True)
    check("[3f] 启动后 _exec_policy() 非 None（已标定品种恒有策略行）",
          eng._exec_policy() is EXEC_POLICY["IF"], True)


# ══════════════════════════════════════════════════════════════
print("\n[4] ★ 运行期零次读 cfg.product_profile（AST 静态证明）")
# ══════════════════════════════════════════════════════════════
def _cfg_profile_readers(rel_path):
    """返回 [(函数名, 行号)]：源码里所有读取「cfg 的 product_profile」的点。

    ⚠️ 两种写法**必须都抓**（只抓一种 = 这条护栏其实没在验东西）：
      · `cfg.product_profile`        —— `Engine.__init__` 的 Instrument 回落构造
        （形参名就叫 `cfg`，不是 `self.cfg`：第一次写这条检查时只匹配了
        `self.cfg`，结果命中 0 处、断言"想当然地绿"——本文件自己的反面教材）
      · `self.cfg.product_profile`   —— 将来出现在别的方法里的写法
    """
    src = open(os.path.join(_REPO, rel_path), encoding="utf-8").read()
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Attribute) or sub.attr != "product_profile":
                continue
            v = sub.value
            is_bare = isinstance(v, ast.Name) and v.id == "cfg"
            is_self = (isinstance(v, ast.Attribute) and v.attr == "cfg"
                       and isinstance(v.value, ast.Name)
                       and v.value.id == "self")
            if is_bare or is_self:
                hits.append((node.name, sub.lineno))
    return hits


_eng_hits = _cfg_profile_readers("Trading/Engine/Engine.py")
_rec_hits = _cfg_profile_readers("Trading/Engine/Reconcile.py")
check("[4a] Engine 里的读取点（函数名）", sorted({h[0] for h in _eng_hits}),
      ["__init__"])
check("[4b] ★ Engine 的读取次数 = 1（仅 __init__ 的 Instrument 回落构造）",
      len(_eng_hits), 1)
check("[4c] ★ Reconcile 一次都不读（对账也走 state.product）",
      _rec_hits, [])
check_true("[4d] Engine 的读取点带行号（抓法未过窄 —— 这条防的是「护栏自己看错」）",
           all(h[1] > 0 for h in _eng_hits) and len(_eng_hits) == 1,
           _eng_hits)


# ══════════════════════════════════════════════════════════════
print("\n[5] cost_cash 口径不变（公式锚点：与 Fee.cash 显式组合逐值相等）")
# ══════════════════════════════════════════════════════════════
def _formula(prof, entry, exit_, closetoday, volume):
    """按 Fee 两档显式展开 —— 独立于 cost_cash 的实现路径。"""
    of, cf = prof.fee_pair()
    ef = cf if closetoday else of
    return (of.cash(entry, prof.multiplier)
            + ef.cash(exit_, prof.multiplier)) * volume


for _code, _e, _x in (("IF", 4500.0, 4510.0), ("AU", 560.0, 566.0),
                      ("TA", 5200.0, 5230.0), ("CU", 78000.0, 78500.0)):
    _p = PRODUCT_PROFILES[_code]
    _inst = Instrument(None, _p)
    for _ct in (True, False):
        for _vol in (1, 2):
            check("[5] {} closetoday={} vol={}".format(_code, _ct, _vol),
                  _inst.cost_cash(_e, _x, closetoday=_ct, volume=_vol),
                  _formula(_p, _e, _x, _ct, _vol))

check("[5x] 未标定品种 → 0.0（等价旧调用点的 … if p is not None else 0.0）",
      Instrument(None, None).cost_cash(4500.0, 4510.0, closetoday=True), 0.0)


# ══════════════════════════════════════════════════════════════
print("\n[6] 旧符号零残留（防回潮：有人加回去就红）")
# ══════════════════════════════════════════════════════════════
def _legacy_hits(rel_path):
    """AST 扫描旧写法 —— **只看真实代码**。

    为什么不用纯文本 grep：本轮被改的两个文件里，注释与 docstring 会**引用**
    旧写法（"原实现是 `getattr(self.state, "exec_policy", None)` …"）——纯文本
    扫描会把这些"说明性引用"当成命中，于是这条护栏只会逼着人删掉解释、留下一段
    没有来由的代码。AST 只看 Call 节点，注释天然不在其中。
    """
    src = open(rel_path, encoding="utf-8").read()
    out = []
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        # ① getattr(<obj>, "exec_policy", …) —— exec_policy 的静默降级
        if isinstance(f, ast.Name) and f.id == "getattr":
            names = [a.value for a in n.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if any("exec_policy" in s for s in names):
                out.append("L{}: getattr(…, 'exec_policy', …) 静默降级".format(n.lineno))
            continue
        # ② cost_cash 的**位置参数个数**：旧签名 3 个（product, entry, exit），
        #    新签名 2 个（entry, exit）。数量一变就说明签名被改回去了。
        if isinstance(f, ast.Attribute) and f.attr == "cost_cash":
            if len(n.args) != 2:
                out.append("L{}: cost_cash 位置参数 {} 个（应为 2）".format(
                    n.lineno, len(n.args)))
    return out


_hits = []
for _root, _dirs, _files in os.walk(os.path.join(_REPO, "Trading")):
    _dirs[:] = [d for d in _dirs if d != "__pycache__"]
    for _f in sorted(_files):
        if not _f.endswith(".py"):
            continue
        _fp = os.path.join(_root, _f)
        for _h in _legacy_hits(_fp):
            _hits.append("{}: {}".format(
                os.path.relpath(_fp, _REPO).replace("\\", "/"), _h))
check("[6a] ★ 旧符号全仓零命中（AST 口径：注释里的引用不算命中）", _hits, [])

_src_eng = open(os.path.join(_REPO, "Trading/Engine/Engine.py"),
                encoding="utf-8").read()
check_true("[6b] _restore 里确实调了 _assert_product_ssot（断言没被摘掉）",
           "_assert_product_ssot(_key)" in _src_eng)
check_true("[6c] _exec_policy 的实现体是纯属性访问（return self.state.exec_policy）",
           "return self.state.exec_policy" in _src_eng)


print("\n============================================================")
print("P55 品种档案 SSOT 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("============================================================")
raise SystemExit(1 if _FAIL else 0)

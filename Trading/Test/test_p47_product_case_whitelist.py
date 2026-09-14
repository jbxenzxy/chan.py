# -*- coding: utf-8 -*-
"""
P47 品种代码大小写归一 + 白名单硬约束 契约测试
================================================
背景（2026-09-13，用户拍板）：
  ① 真 bug 修复：前端别名表（DataAPI/TqSdkAPI.py FUTURES_ALIASES）把
    SHFE/DCE 品种解析成 tqsdk 惯例的**小写**主连（AU → "KQ.m@SHFE.au"、
    RB → "KQ.m@SHFE.rb"），而 parse_product 原先大小写敏感 →
    PRODUCT_PROFILES.get("au") 返回 None —— 用户输入 AU/AG/CU/TA 这
    4 个**已支持**品种时档案也匹配不上（档案键为大写）。修复 = 在
    parse_product 内统一归一为大写，调用方无需各自 upper()。
  ② 品种白名单硬约束：品种不在 PRODUCT_PROFILES → **交易引擎拒绝启动**
    （Engine._restore 抛 ValueError），不发告警。实盘入口在
    App/AppTrader.start 前置拦截（AppError → 400 → 前端 alert）。

本测试锁死的断言：
  [1] parse_product 大小写归一：小写主连 → 大写品种代码；无点串/缺失 → ""
  [2] TradingConfig 以**前端真实形态**（别名解析后的小写主连）构造时，
      AU/AG/CU/TA/IF 档案全部命中、五字段正确注入（回归 ①）
  [3] 白名单硬约束（回归 ②）：未知品种（RB/ZZ）构造引擎抛 ValueError；
      异常 msg 含支持清单与"禁止启动"；已知品种（小写 au 主连）正常启动
  [4] 【2026-09-14 新增】别名表 ⇔ 前端硬编码表 **同源契约**（防"两表不同源"回潮）：
      · `TqSdkAPI.FUTURES_ALIASES` = 16 品种 / 17 条别名（用户点名收窄，原 83 条）；
      · `Frontend/app.js` 的 `FUTURES_ALIAS_KEYS` 键集与之一致（逐键比对）；
      · **每个可下单品种（PRODUCT_PROFILES）都必须能在别名表里搜到**
        —— 否则会出现"能下单却画不出 K 线"的死角；
      · 已裁掉的品种（T/TF/NI/SR/PP/Y/A50…）确实不在表内（负向断言）。
  [5] 【2026-09-14 第 6 批新增】「未标定品种置灰」契约（防"置灰越界去限制搜索"回潮）：
      · 开关容器有置灰类名 + 提示位（index.html / app.css / app.js 三处齐备）；
      · 置灰判定**复用** /api/trader/product-check（与开启路径同一来源，不另写白名单）；
      · 引擎运行中**不置灰**（否则用户关不掉正在跑的引擎）；
      · 接口失败 = **放行**（置灰不能把开关卡死在灰态）；
      · 置灰**只作用于下单开关** —— 搜索/解析侧不得出现任何品种过滤（负向断言）。

不需要真实 tqsdk / 网络。
跑法：python Trading/Test/test_p47_product_case_whitelist.py
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))

# 自定位仓库根（Test/ → Trading/ → 仓库根），与本目录其他 test_p* 一致
_RROOT = os.path.dirname(os.path.dirname(_HERE))
if _RROOT not in sys.path:
    sys.path.insert(0, _RROOT)

from Trading.Infra.ProductProfile import (  # noqa: E402
    PRODUCT_PROFILES, parse_product, parse_product_key,
)
from Trading.Config import TradingConfig  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name +
          ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("✓" if ok else "✗") + " " + name +
          ("" if ok else "  -> got={!r}".format(got)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="p47_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def build_engine(tmpdir, signal_symbol):
    """构造引擎（与本目录 test_p46 同款最小装配）。未知品种应抛 ValueError。"""
    from Trading import Broker  # noqa: F401  # 注册 dry_run broker
    from Trading.Broker.DryRun import DryRunBroker
    from Trading.Engine.Engine import TradingEngine
    from Trading.Infra.EventLog import EventLog
    from Trading.Infra.Store import Store
    from Trading.Strategy.Entry import EntryPolicy
    from Trading.Strategy.Exit import LayeredExitPolicy

    cfg = TradingConfig(instrument={"signal_symbol": signal_symbol})
    entry = EntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    broker = DryRunBroker(cfg.instrument, {"sim_equity": 10_000_000.0})
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def main():
    print("\n[1] parse_product 大小写归一（回归：别名表产出小写主连）")
    check("parse KQ.m@SHFE.au（前端别名形态）→ AU", parse_product("KQ.m@SHFE.au"), "AU")
    check("parse KQ.m@SHFE.rb → RB", parse_product("KQ.m@SHFE.rb"), "RB")
    check("parse KQ.m@SHFE.AU（大写）→ AU", parse_product("KQ.m@SHFE.AU"), "AU")
    check("parse KQ.m@CZCE.TA → TA", parse_product("KQ.m@CZCE.TA"), "TA")
    check("parse KQ.m@CFFEX.if → IF", parse_product("KQ.m@CFFEX.if"), "IF")
    check("parse 无点串 → ''", parse_product("IF2609"), "")
    check("parse 空串 → ''", parse_product(""), "")
    check("parse None → ''", parse_product(None), "")

    print("\n[2] 前端真实形态（小写主连）构造 TradingConfig：档案全部命中")
    for sym, product, mult, tick in [
            ("KQ.m@SHFE.au", "AU", 1000.0, 0.02),
            ("KQ.m@SHFE.ag", "AG", 15.0, 1.0),
            ("KQ.m@SHFE.cu", "CU", 5.0, 10.0),
            ("KQ.m@CZCE.ta", "TA", 5.0, 2.0),
            ("KQ.m@CFFEX.if", "IF", 300.0, 0.2)]:
        c = TradingConfig(instrument={"signal_symbol": sym})
        check("{} product_profile 命中 {}".format(sym, product),
              (c.product_profile.product if c.product_profile else None), product)
        check("{} multiplier 注入 {}".format(product, mult),
              c.instrument.multiplier, mult)
        check("{} price_tick 注入 {}".format(product, tick),
              c.instrument.price_tick, tick)
        check("{} R 下限 = 默认 3 点（2026-09-13 拍板）".format(product),
              c.exit_params.min_r_points, 3.0)

    print("\n[3] 白名单硬约束：未知品种拒绝启动、已知品种正常启动")
    for sym in ["KQ.m@SHFE.rb", "KQ.m@SHFE.ZZ"]:
        with tmp_dir() as td:
            try:
                build_engine(td, sym)
                raised = None
            except ValueError as e:
                raised = str(e)
            check_true("{} 构造引擎抛 ValueError（拒绝启动）".format(sym),
                       raised is not None)
            if raised:
                check("{} 异常列出支持清单(含 AU)".format(sym), "AU" in raised, True)
                check("{} 异常说明'禁止启动'".format(sym), "禁止启动" in raised, True)
    for sym in ["KQ.m@SHFE.au", "KQ.m@CFFEX.IF"]:
        with tmp_dir() as td:
            eng = build_engine(td, sym)
            check("{}（小写/大写主连）正常启动".format(sym), eng is not None, True)

    print("\n[4] 别名表 ⇔ 前端硬编码表 同源契约（16 品种 / 17 别名）")
    from DataAPI.TqSdkAPI import FUTURES_ALIASES

    # 4a 表本身的口径（2026-09-14 第二轮用户点名收窄，原 83 条全表）
    check("别名表条数 = 17", len(FUTURES_ALIASES), 17)
    contracts = sorted({parse_product_key(v) for v in FUTURES_ALIASES.values()})
    check("别名表覆盖品种数 = 16", len(contracts), 16)
    all_values = list(FUTURES_ALIASES.values())
    check("重复写法只允许 TA/PTA（同指 CZCE.TA）",
          sorted(a for a, v in FUTURES_ALIASES.items()
                 if all_values.count(v) > 1),
          ["PTA", "TA"])
    check("16 品种清单逐项", contracts,
          ["AG", "AU", "CU", "IC", "IF", "IH", "IM", "JM", "LC", "LH",
           "M", "MA", "P", "RB", "SC", "TA"])

    # 4b 前端硬编码表必须与后端同源
    #    （"两表不同源"是 2026-09-14 实测过的真实漏洞：下拉挡得住、回车挡不住 ——
    #      根因就是前端硬编码表与后端别名表各写一份。此断言把两边钉在一起。）
    _app_js = os.path.join(_RROOT, "Frontend", "app.js")
    with open(_app_js, encoding="utf-8") as fh:
        js = fh.read()
    m = re.search(r"const FUTURES_ALIAS_KEYS = new Set\(\[(.*?)\]\);", js, re.S)
    check_true("app.js 中抓到 FUTURES_ALIAS_KEYS", m is not None)
    fe_keys = set()
    if m:
        fe_keys = {k.strip().strip('"').strip("'")
                   for k in m.group(1).split(",") if k.strip()}
    check("前端键集 == 后端别名表键集", sorted(fe_keys), sorted(FUTURES_ALIASES))

    # 4c 每个**可下单**品种都必须能在别名表里搜到
    #    （否则出现"能下单却画不出 K 线"的死角：引擎允许跑，但界面上翻不到行情）
    reachable = {parse_product_key(v) for v in FUTURES_ALIASES.values()}
    check("可下单品种全部可在别名表搜到（无死角）",
          sorted(set(PRODUCT_PROFILES) - reachable), [])
    check("可下单品种数仍为 8（本轮未动白名单）", len(PRODUCT_PROFILES), 8)

    # 4d 负向断言：本轮裁掉的品种确实不在表内（防止"以为删了其实没删"）
    for gone in ["T", "TF", "TL", "TS", "NI", "AL", "ZN", "SR", "I",
                 "PP", "Y", "A", "SI", "PS", "LU", "NR", "A50", "CN"]:
        check_true("已裁掉 {!r}（不在别名表）".format(gone),
                   gone not in FUTURES_ALIASES)

    print("\n[5] 未标定品种置灰契约（只置灰下单开关，不限制行情搜索）")
    _html_p = os.path.join(_RROOT, "Frontend", "index.html")
    _css_p = os.path.join(_RROOT, "Frontend", "app.css")
    with open(_html_p, encoding="utf-8") as fh:
        html = fh.read()
    with open(_css_p, encoding="utf-8") as fh:
        css = fh.read()

    # 5a 三处落点齐备（缺一处 = 置灰不生效或看不到原因）
    check_true("index.html 有置灰提示位 auto-order-hint",
               'id="auto-order-hint"' in html)
    check_true("提示位在 auto-order-wrap 容器内",
               re.search(r'id="auto-order-wrap".*?id="auto-order-hint"', html, re.S)
               is not None)
    check_true("app.css 有 .auto-order-wrap.disabled 样式",
               ".auto-order-wrap.disabled" in css)
    check_true("app.js 有置灰渲染函数 applyAutoOrderTradableUI",
               "function applyAutoOrderTradableUI(" in js)
    check_true("app.js 有置灰查询函数 refreshAutoOrderTradable",
               "function refreshAutoOrderTradable(" in js)

    # 5b 判定来源与开启路径同一份（复用 checkSymbolTradable → /product-check），
    #    且**没有**在前端另抄一份白名单
    _fn = re.search(r"async function refreshAutoOrderTradable\(\)\s*\{(.*?)\n        \}",
                    js, re.S)
    check_true("置灰查询函数已抓到", _fn is not None)
    if _fn:
        check_true("置灰判定复用 checkSymbolTradable（同一来源）",
                   "checkSymbolTradable(" in _fn.group(1))
    check_true("前端没有另抄白名单常量（不得出现 PRODUCT_PROFILES 硬编码）",
               "PRODUCT_PROFILES" not in js)

    # 5c 引擎运行中不置灰（否则关不掉正在跑的引擎）
    _ui = re.search(r"function applyAutoOrderTradableUI\(\)\s*\{(.*?)\n        \}",
                    js, re.S)
    check_true("置灰渲染函数已抓到", _ui is not None)
    if _ui:
        check_true("置灰条件排除「引擎运行中」",
                   "!autoOrderRunning" in _ui.group(1))
    # 5d 接口失败 = 放行（allowed: true）—— 置灰不能把开关卡死
    _ck = re.search(r"async function checkSymbolTradable\(symbol\)\s*\{(.*?)\n        \}",
                    js, re.S)
    check_true("checkSymbolTradable 已抓到", _ck is not None)
    if _ck:
        body = _ck.group(1)
        check("异常降级为放行的分支数 = 2（HTTP 非 2xx + 抛异常）",
              body.count("return { allowed: true, message: '' };"), 2)
    # 5e 开关复位走置灰重算，而不是无脑 disabled=false
    check_true("onAutoOrderToggle 的 finally 用置灰重算复位",
               re.search(r"finally\s*\{\s*autoOrderBusy = false;.*?applyAutoOrderTradableUI\(\);",
                         js, re.S) is not None)
    check_true("不再无脑 checkbox.disabled = false（防解除灰态）",
               "checkbox.disabled = false;" not in js)

    # 5f ⚠️ 负向：置灰**不得**越界去限制行情搜索
    #    （搜索/解析侧一旦出现品种过滤，就退回"消费端加过滤"的旧做法 ——
    #      实测有 4 条绕过点，2026-09-14 当天即被用户撤销）
    _chart = os.path.join(_RROOT, "App", "AppChart.py")
    with open(_chart, encoding="utf-8") as fh:
        chart_py = fh.read()
    _search = re.search(r"def search_stocks\(.*?\n(?=def |\Z)", chart_py, re.S)
    check_true("AppChart.search_stocks 已抓到", _search is not None)
    if _search:
        sb = _search.group(0)
        check_true("搜索侧不出现置灰/白名单过滤符号",
                   not any(t in sb for t in ("autoOrderTradable", "disabled",
                                             "PRODUCT_PROFILES")))

    print("\n============================================================")
    print("P47 大小写归一 + 白名单硬约束 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("============================================================")
    raise SystemExit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

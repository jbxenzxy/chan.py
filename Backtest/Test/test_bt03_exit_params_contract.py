# -*- coding: utf-8 -*-
"""
股票出场参数 / 费率 SSOT 契约（Backtest/Test/test_bt03_exit_params_contract.py）
==============================================================================
设计文档 §5.9 #1 的落点（v1.15 用户裁定走**护栏式**，不做"AppTPSL 反向 import"）。

背景：同一口径在迁移期有**两份常量**
---------------------------------------------------------------------
    `App/AppTPSL.py:45`          `_STOCK_EXIT_OVERRIDES = {"win_loss_ratio": 2.0,
                                                          "trailing_trigger_r": 1.0}`
    `Backtest/ExitParams.py`     `STOCK_EXIT_PARAMS`（同一组数值）

`_STOCK_EXIT_OVERRIDES` 要等 TPSL **被删**才消失 ⇒ 中间期两份并存。本用例钉住
"存在则必须相等"。

为什么不做根治（让 `AppTPSL` 反向 import `Backtest.ExitParams`）
---------------------------------------------------------------------
那会改动 `App/` 一行，与 §5.1「不碰 `App/`」冲突。护栏式 = 最小改动、零 `App/` 触碰。

三态判定（刻意区分，不许"一律 SKIP"糊过去）
---------------------------------------------------------------------
    `App.AppTPSL` 模块**不存在**   → SKIP。这是 TPSL 已退役的**预期终态**，
                                     本用例随之自动退役。
    模块在、符号**不在**            → **FAIL**。那是改名 / 误删符号，不是退役 ——
                                     静默 SKIP 会让"口径悄悄没人守了"。
    两者都在                        → 键集 + 逐键值必须完全相等。

覆盖
---------------------------------------------------------------------
  ①（核心）`STOCK_EXIT_OVERRIDES` 与 `STOCK_EXIT_PARAMS` 键集相等 + 逐键值精确相等
  ② `LayeredExitPolicy(STOCK_EXIT_PARAMS)` 可构造 —— 键名拼错会**当场报错**
     （`ExitPolicyParams` 是 `extra="forbid"` 的校验模型），这是"比静默用默认安全"的
     设计，本用例把"当前这份确实能构造"钉住
  ③ 构造后 `params` 里 SSOT 各键与 SSOT 同值（防止中途被平台默认值覆盖）
  ④ 费率口径冻结（取数日期 2026-10-05）+ 推导式自洽：
     `NOMINAL_COST_RATE == 2k + s`、`TARGET_AMOUNT == m / k`。
     费率是**会变的**（券商改佣金、政策改印花税）⇒ 改的时候改两处并在交付说明里写明，
     不许"数字悄悄漂了但没人知道"。
  ⑤ 股票侧出场口径（2026-10-06 统一轮）：`atr_sl_multiple` 显式在表里 + 值冻结 +
     **与全局模型默认相等**。⚠ 这条本轮**改判**：上一版钉的是"刻意分叉（股票 1.0 /
     期货 2.0）"，用户裁定「要改就股票和期货两个品种都改，回测和实盘要一致，否则回测
     得出的结论跟实盘不一致，那做回测干嘛」⇒ 全局默认（`Trading/Config.py::ExitConfig`）
     同步改为 1.0，本组改为钉**相等性**。两个失效方式仍都无声，都被守住：
     「删掉键 ⇒ 显式声明消失」与「只改一边 ⇒ 回测与实盘分叉」。

跑法：`python Backtest/Test/test_bt03_exit_params_contract.py`（退出码 0/1 即判决）
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKTEST_DIR)
for _p in (ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PASS = 0
FAIL = 0
SKIP = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[PASS] %s" % name)
    else:
        FAIL += 1
        print("[FAIL] %s" % name)
        if detail:
            print("        " + str(detail).replace("\n", "\n        "))


def skip(name, why):
    global SKIP
    SKIP += 1
    print("[SKIP] %s（%s）" % (name, why))


PAGE_MODULE = "App.AppTPSL"
PAGE_SYMBOL = "_STOCK_EXIT_OVERRIDES"


def _load_page_overrides():
    """三态：("gone", None) / ("renamed", None) / ("ok", dict)。"""
    try:
        spec = importlib.util.find_spec(PAGE_MODULE)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return "gone", None
    mod = importlib.import_module(PAGE_MODULE)
    if not hasattr(mod, PAGE_SYMBOL):
        return "renamed", None
    return "ok", dict(getattr(mod, PAGE_SYMBOL))


def main():
    print("=" * 68)
    print("出场参数 / 费率 SSOT 契约（Backtest/Test/test_bt03_exit_params_contract.py）")
    print("=" * 68)

    import Backtest.ExitParams as EP

    ssot = dict(EP.STOCK_EXIT_PARAMS)

    # ── ① 双源常量相等（核心）────────────────────────────────────
    state, page = _load_page_overrides()
    if state == "gone":
        skip("① %s.%s ≡ Backtest.ExitParams.STOCK_EXIT_PARAMS" % (PAGE_MODULE, PAGE_SYMBOL),
             "模块不存在 ⇒ TPSL 已退役，本用例随之自动退出（预期终态）")
    elif state == "renamed":
        check("① %s.%s ≡ Backtest.ExitParams.STOCK_EXIT_PARAMS" % (PAGE_MODULE, PAGE_SYMBOL),
              False,
              "模块仍在但符号 %r 不见了 —— 这是改名/误删符号，不是 TPSL 退役。\n"
              "请核对：若确为改名，同步本测试的 PAGE_SYMBOL 与 ExitParams.py 的注释引用。"
              % PAGE_SYMBOL)
    else:
        d = []
        if set(page) != set(ssot):
            d.append("键集不同：页面=%s 回测=%s" % (sorted(page), sorted(ssot)))
        for k in sorted(set(page) & set(ssot)):
            if page[k] != ssot[k]:
                d.append("键 %r：页面=%r 回测=%r" % (k, page[k], ssot[k]))
        check("① %s.%s ≡ Backtest.ExitParams.STOCK_EXIT_PARAMS（键集 + 逐键值）"
              % (PAGE_MODULE, PAGE_SYMBOL), not d, "\n".join(d))

    # ── ② 可构造（键名拼错会当场报错）────────────────────────────
    from Trading.Strategy.Exit import ExitPolicyParams, LayeredExitPolicy
    pol = None
    try:
        pol = LayeredExitPolicy(ssot)
        err = ""
    except Exception as e:                                     # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, e)
    check("② LayeredExitPolicy(STOCK_EXIT_PARAMS) 可构造（extra=forbid 校验通过）",
          pol is not None, err)

    # ── ③ 构造后各键仍与 SSOT 同值 ───────────────────────────────
    if pol is None:
        check("③ 构造后 params 里 SSOT 各键同值", False, "② 未通过，跳过比对")
    else:
        p = getattr(pol, "params", None)
        if not isinstance(p, dict):
            p = dict(p) if p is not None else {}
        d = [(k, p.get(k), ssot[k]) for k in sorted(ssot) if p.get(k) != ssot[k]]
        check("③ 构造后 params 里 SSOT 各键同值", not d,
              "; ".join("%r: 策略内=%r SSOT=%r" % t for t in d))

    # ── ④ 费率口径冻结 + 推导自洽 ────────────────────────────────
    FROZEN = {
        "COMMISSION_RATE": 0.0001,      # k（含规费与过户费）
        "MIN_COMMISSION_CASH": 5.0,     # m
        "STAMP_DUTY_RATE": 0.0005,      # s（仅卖出）
        "TRANSFER_FEE_RATE": 0.0,       # t（已并入 k）
    }
    d = []
    for name, want in FROZEN.items():
        got = getattr(EP, name, None)
        if got != want:
            d.append("%s：现值 %r ≠ 冻结值 %r（改费率须同步本测试 + ExitParams.py）"
                     % (name, got, want))
    check("④a 费率常量与冻结值一致（取数日期 2026-10-05）", not d, "\n".join(d))

    d = []
    if EP.NOMINAL_COST_RATE != 2 * EP.COMMISSION_RATE + EP.STAMP_DUTY_RATE:
        d.append("NOMINAL_COST_RATE=%r ≠ 2k+s=%r"
                 % (EP.NOMINAL_COST_RATE, 2 * EP.COMMISSION_RATE + EP.STAMP_DUTY_RATE))
    if EP.TARGET_AMOUNT != EP.MIN_COMMISSION_CASH / EP.COMMISSION_RATE:
        d.append("TARGET_AMOUNT=%r ≠ m/k=%r"
                 % (EP.TARGET_AMOUNT, EP.MIN_COMMISSION_CASH / EP.COMMISSION_RATE))
    check("④b 推导式自洽（c=2k+s、target_amount=m/k）", not d, "\n".join(d))

    # 最小申报单位表（板块规则）—— 逐条对照
    d = []
    for code, want in (("sh600519", (100, 100)), ("sz002190", (100, 100)),
                       ("sz300015", (100, 100)), ("sh688981", (200, 100)),
                       ("bj430047", (100, 1)), ("sz430047", (100, 1)),
                       ("bj839680", (100, 1))):
        got = EP.lot_rule(code)
        if got != want:
            d.append("%s: %r ≠ %r" % (code, got, want))
    check("④c lot_rule 板块最小申报单位（主板/科创/北交所）", not d, "\n".join(d))

    # ── ⑤ 股票侧出场口径：显式冻结 + 与全局默认**必须相等**（2026-10-06）──
    # 为什么单独钉这两个键：它们**必须显式列在 STOCK_EXIT_PARAMS 里**，不能靠
    # 「未列出 ⇒ 吃模型默认」这条捷径 —— 显式列出才读得出来比对，也才有一份
    # 可评审的"股票口径声明"。
    # ⚠ 口径统一（2026-10-06 用户二次拍板）：`atr_sl_multiple` 由 2.0 收紧到 1.0，
    # 且 `Trading/Config.py::ExitConfig` 的**全局默认同步改成 1.0** —— 用户原话
    # 「要改就股票和期货两个品种都改，而且回测和实盘要一致，否则回测得出的结论跟
    #  实盘不一致，那做回测干嘛」。故 ⑤c 钉的不再是"刻意分叉"，而是**相等性**：
    # 股票侧 == 全局默认 == `ExitPolicyParams` 默认（后者继承 `ExitConfig`）。
    # 两个失效方式都无声，本组同时守住：
    #   · 有人把键从 dict 删掉 ⇒ 股票口径改成靠默认兜底，"显式声明"消失（⑤a）；
    #   · 有人**只改一边**（改股票没改全局，或反过来）⇒ 回测与实盘口径分叉（⑤c/⑤d）。
    _model = ExitPolicyParams()
    check("⑤a atr_sl_multiple 显式存在于 STOCK_EXIT_PARAMS（不靠模型默认兜底）",
          "atr_sl_multiple" in ssot, "keys=%s" % sorted(ssot))
    check("⑤b atr_sl_multiple == 1.0（2026-10-06 用户拍板，R = max(A, 1×ATR)）",
          ssot.get("atr_sl_multiple") == 1.0, "got=%r" % ssot.get("atr_sl_multiple"))
    check("⑤c 股票侧 == 全局默认（回测/实盘与股票/期货同一套 R 几何；只改一边即红）",
          float(_model.atr_sl_multiple) == 1.0
          and ssot.get("atr_sl_multiple") == float(_model.atr_sl_multiple),
          "股票=%r 全局默认=%r" % (ssot.get("atr_sl_multiple"),
                                   float(_model.atr_sl_multiple)))
    check("⑤d win_loss_ratio == 2.0 且 == 全局默认（L3 启动阈值同规格）",
          ssot.get("win_loss_ratio") == 2.0
          and ssot.get("win_loss_ratio") == float(_model.win_loss_ratio),
          "股票=%r 全局默认=%r" % (ssot.get("win_loss_ratio"),
                                   float(_model.win_loss_ratio)))

    print("-" * 68)
    print("合计 %d 项，通过 %d，失败 %d，跳过 %d" % (PASS + FAIL + SKIP, PASS, FAIL, SKIP))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
前端「关闭自动下单」请求契约 —— 静态护栏
=====================================================================
背景（2026-09-30 评审）：多实例改造后，后端 `AppTrader.stop()` 支持
**按品种停**（`stop(symbol)`），不带 symbol 才走"停全部"兼容分支。但前端
`onAutoOrderToggle` 一开始只给**开启**分支构造 body，**关闭**请求不带 body
→ 服务端 `symbol=None` → 走"停全部"。后果：多品种并跑时在 A 品种页面点关闭，
会把 B 品种的实例一起停掉，其运行态持仓要走锁仓/平仓离场（有资金影响）。

这条不变量依赖人工评审才发现一次 —— 前端是零构建原生 JS、仓库里没有
运行时用例，`Test/run_all.py` 也不会跑它，改动丢一次就没人知道。故用
**读源码的静态断言**把契约钉住（与 `test_p59` [14i]~[14k]、
`test_frontend_smoke` 同一手法）。

钉住的四条：
  ① 关闭分支**必须**构造 body（不能是无 body 的裸 POST）；
  ② body 里**必须**带 symbol（不带 = 退化为停全部）；
  ③ 关闭请求打的是 off 路由（不是 on）；
  ④ 后端 off 路由确实把 body.symbol 透传给 `call_trader_stop`
     （前端带了对面不读，等于白带）。

运行：python Test/test_frontend_ao_off_symbol.py [--update]
"""
import argparse
import os
import re
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")
FRONT_API = os.path.join(REPO_ROOT, "FrontAPI.py")


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _check(failures, label, got, want):
    ok = (got == want)
    print(("  ✓ " if ok else "  ✗ ") + label
          + ("" if ok else " -> got {!r}, want {!r}".format(got, want)))
    if not ok:
        failures.append(label)


def _slice_toggle(js):
    """取 `onAutoOrderToggle` 函数体（从定义到下一个顶层 function 之前）。"""
    start = js.find("async function onAutoOrderToggle(")
    if start < 0:
        return ""
    nxt = js.find("\n        async function ", start + 1)
    if nxt < 0:
        nxt = js.find("\n        function ", start + 1)
    return js[start:(nxt if nxt > 0 else len(js))]


def test_off_branch_body(failures):
    """① ② ③ —— 关闭分支的 body / symbol / 路由。"""
    js = _read(APP_JS)
    body = _slice_toggle(js)
    _check(failures, "[1] onAutoOrderToggle 可定位（源码锚点仍在）",
           bool(body), True)
    if not body:
        return

    # else 分支 = 关闭分支（on 为真假由 checkbox 决定，if (on) 之后即 else）
    else_pos = body.find("} else {")
    _check(failures, "[2] 存在独立的关闭（else）分支", else_pos > 0, True)
    if else_pos < 0:
        return
    off = body[else_pos:]

    # ① 关闭分支构造 body
    _check(failures, "[3] 关闭分支构造了请求 body",
           "opts.body = JSON.stringify(" in off, True)
    # ② body 带 symbol —— 只看 JSON.stringify 的**实参**：else 分支的注释里
    #    本来就有"symbol"字样（解释为什么必须带），整段做子串包含会被注释
    #    命中而恒绿，护栏就白钉了（变异实测：body 改成 {} 仍绿）。
    m = re.search(r"opts\.body\s*=\s*JSON\.stringify\((.*?)\);", off, re.S)
    arg = m.group(1) if m else ""
    _check(failures, "[4] 关闭请求的 body 实参含 symbol（不带 = 退化成停全部）",
           "symbol" in arg, True)
    # ③ 路由按 on/off 分流，且 off 分支确实存在
    _check(failures, "[5] 关闭走 off 路由",
           "'/api/trader/auto-order/off'" in js
           or '"/api/trader/auto-order/off"' in js, True)
    # 附带：Content-Type 提到分支外（两分支共用，开启也仍带）
    _check(failures, "[6] Content-Type 在分支外设置（开启/关闭都带）",
           body.count("opts.headers = { 'Content-Type': 'application/json' }"), 1)


def test_backend_reads_symbol(failures):
    """④ 后端 off 路由把 body.symbol 透传给 stop()。"""
    api = _read(FRONT_API)
    # 锚点用路由装饰器字符串（行号会漂，字符串不会），向后取一段函数体
    pos = api.find('@router.post("/api/trader/auto-order/off"')
    seg = api[pos:pos + 1600] if pos >= 0 else ""
    _check(failures, "[7] off 路由可定位（源码锚点仍在）", bool(seg), True)
    if not seg:
        return
    _check(failures, "[8] off 路由读取 body.symbol",
           "get(\"symbol\")" in seg, True)
    _check(failures, "[9] off 路由把 symbol 透传给 call_trader_stop",
           "call_trader_stop" in seg and "symbol=" in seg, True)


def main():
    ap = argparse.ArgumentParser(
        description="前端「关闭自动下单」请求契约静态护栏")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    ap.parse_args()

    print("===== 前端「关闭自动下单」请求契约（静态护栏） =====")
    failures = []
    test_off_branch_body(failures)
    test_backend_reads_symbol(failures)

    print()
    if failures:
        print("===== 结果: 失败 {} 项 =====".format(len(failures)))
        for x in failures:
            print(" -", x)
        return False
    print("===== 结果: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

# -*- coding: utf-8 -*-
"""
账本面板展示契约护栏（2026-09-22 用户拍板；2026-09-24 成交节删除改版）
====================================================================
钉住 Frontend/app.html + app.js renderAutoOrderLedger + App/AppTrader.py
投影三处的展示契约：

  ① 面板节标题：只剩「持仓」一节（2026-09-24 用户拍板：成交节删除）——
     旧长标题（含「账本」字样）与「成交」节全文件零残留；
  ② 持仓行：**序号**（1. / 2. / …，按后端 positions 原序编号，前端不重排）
     + 多/空 + 手数 + @ 入场价 + 时间，**无止损列**（renderAutoOrderLedger
     持仓分支内「止损」零命中；时间戳走 fmtAolTime）；
  ③ 成交行（2026-09-24 删除）：前端成交分支 / t.symbol / 净额 / aol-trades
     消费全部零残留；AppTrader.py 的 trades_recent 投影**保留**（API 数据
     完整性不受展示删减影响，只是前端不再消费）；
  ④ 成交列表排序（后端投影保留，契约随之保留）：App/AppTrader.py 用
     `_all_trades[-10:]`（库内 exit_at 升序原序截尾），最新一条排在**最后**；
     `reversed(_all_trades)` 不得回潮；
  ⑤ 资源版本号：app.html 引 app.js?v=37（改前端必须抬版本号，防缓存假象；
     版本号是**单调递增**的，每次改前端都要同时抬这里的期望值与残留断言）；
  ⑥ 账本与开关解耦（2026-09-22 二次拍板）：空态文案「（暂无账本数据）」，
     「（自动下单未运行）」零残留；AppTrader._read_engine_switch 收 out_dir、
     status() 回退 上次运行目录 → 默认目录；进程不在时 alerts/toasts 置空；
  ⑦ DryRun 离场按触发 K 线收盘价落账（2026-09-22 拍板）：ExitCheck 有
     fill_price 字段、触发两分支（多 / 空）都带 fill_price=close、Engine 透传并优先作
     ref_price；
  ⑧ 持仓行显示合约（p.symbol，2026-09-22 三次拍板：
     多合约并行时行内必须能看出是哪个合约，如 IM2612）；
  ⑨ 顶栏 chip 几何（2026-09-24 二次拍板）：开关宽度 34px = 账本 chip 宽度、
     滑块位移 15px（34-13-3-3）；实时徽标 / 自动下单文字 / 账本 三件套
     white-space: nowrap —— 三者高度本就都是 19px，不一致只在头部被挤压
     折行时出现（vw≤1700 实测 34 / 38 / 38），守 nowrap 即守"始终同高"。

行为层：fmtAolTime 抽到 node 里跑真函数，逐样本比对输出（node 不在位
则 SKIP，只跑静态层）。判别力自证（护栏不恒真）：把 AppTrader.py 的
`_all_trades[-10:]` 临时变异回 `list(reversed(_all_trades))[:10]` → 本用例
必须变红（变异记录见交付说明）；app.html 标题改名 / app.js 恢复「止损」
列同理。

跑法：python Test/test_aol_ledger_display.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {}".format(name))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TEST_DIR)

with open(os.path.join(REPO, "Frontend", "app.html"), encoding="utf-8") as f:
    HTML = f.read()
with open(os.path.join(REPO, "Frontend", "app.js"), encoding="utf-8") as f:
    JS = f.read()
with open(os.path.join(REPO, "App", "AppTrader.py"), encoding="utf-8") as f:
    PY = f.read()

# ═══ ① 标题 ═══
print("\n[1] 面板节标题（2026-09-24：只剩「持仓」，成交节已删）")
titles = re.findall(r'class="aol-title">([^<]+)</div>', HTML)
check("标题序列 == ['持仓']（成交节删除）", titles, ["持仓"])
check("旧标题「交易引擎账本持仓」零残留", HTML.count("交易引擎账本持仓"), 0)
check("旧标题「最近成交（交易引擎账本口径）」零残留（整串）",
      HTML.count("最近成交（交易引擎账本口径）"), 0)
check("标题内不再含「账本」字样",
      any("账本" in t for t in titles), False)
check("成交节容器 aol-trades 已从 app.html 删除", "aol-trades" in HTML, False)

# ═══ ② 持仓行：无止损列 ═══
print("\n[2] 持仓行去止损列 + 时间格式化")
i0 = JS.index("function renderAutoOrderLedger(data) {")
i1 = JS.index("// 价格显示", i0)          # 下一区块横幅 = 区块结束锚
BLOCK = JS[i0:i1]
check("renderAutoOrderLedger 区块长度 > 300（防锚点抓半截）",
      len(BLOCK) > 300, True)
check("区块内「止损」零命中", BLOCK.count("止损"), 0)
check("时间只剩持仓行走 fmtAolTime", BLOCK.count("fmtAolTime("), 1)
check("持仓行保留 @ 入场价（一位小数）", BLOCK.count("fmtAolPx1(p.entry_price)"), 1)
check("持仓行显示合约（p.symbol）",
      BLOCK.count("(p.symbol ? '<span class=\"aol-dim\">' + p.symbol + '</span>' : '')"), 1)
# ── 持仓序号（2026-09-24 二次拍板：1. 空 2 手 … / 2. 多 2 手 …）──
#   序号按后端 positions **原序**编号（引擎 append 顺序 = 建仓先后，
#   PositionBook.to_dict 原序输出），前端不重排 —— 重排会让序号在每次
#   刷新后跳位，失去"1 号仓"这样的指代能力。
check("持仓 map 带下标（function (p, i)）",
      BLOCK.count("ps.map(function (p, i) {"), 1)
check("序号 span（aol-idx）恰好一处",
      BLOCK.count("'<span class=\"aol-idx\">' + (i + 1) + '.</span>'"), 1)
check("序号从 1 起（i + 1，不是 i）", BLOCK.count("(i + 1)"), 1)
# ── ③ 前端成交消费零残留（2026-09-24 删除；AppTrader 投影保留见 [4]）──
check("成交行拼接（t.entry_price/exit_price/净额）零残留",
      (("fmtAolPx1(t.entry_price)" in BLOCK)
       or ("fmtAolPx1(t.exit_price)" in BLOCK) or ("'净 '" in BLOCK)), False)
check("成交行合约（t.symbol）零残留",
      "(t.symbol ? '<span class=\"aol-dim\">' + t.symbol + '</span>' : '')" in BLOCK,
      False)
check("空态「（无成交）」零残留",
      ("（无成交）" in JS) or ("（无成交）" in HTML), False)

# ═══ ③ fmtAolTime 存在且被 app.html 版本号护栏配套 ═══
print("\n[3] 资源版本号")
# 版本号是**单调递增**的守卫（防缓存假象）：本次改前端（账本持仓序号 +
# 顶栏开关宽度对齐账本 + 三件套禁止折行 + Worker 轮询 URL 根因修复 +
# 统计弹窗类型胜负行回退修复，2026-09-24 第三轮）已抬到 v=37。
#   本组断言刻意保留"写死当前值"的形态 —— 它的作用正是强迫每次改前端的人意识到
#   要抬版本号；放宽成"任意 v=\d+"就等于把这条守卫拆掉。
check("app.html 引 app.js?v=37", 'app.js?v=37' in HTML, True)
check("旧版本号 v=36 零残留", 'app.js?v=36' in HTML, False)

# ═══ ④ 成交列表排序（后端投影保留：展示删减不动数据完整性） ═══
print("\n[4] 成交列表：升序原序截尾，最新在最后（投影保留，前端不消费）")
check("AppTrader.py 用 _all_trades[-10:]",
      "_all_trades[-10:]" in PY, True)
check("reversed(_all_trades) 零残留", "reversed(_all_trades)" in PY, False)
check("旧注释「倒序截取」零残留", "倒序截取" in PY, False)
# 排序语义自证：StateDB.trades() 升序（ORDER BY exit_at），尾部 10 条
# 原序 = 时间升序 = 最新在最后。用表达式本身在样本上复核一遍。
_statedb_path = os.path.join(REPO, "Trading", "Infra", "StateDB.py")
if os.path.exists(_statedb_path):
    # 前置事实（部分树里没有该文件 → 跳过，不算失败）
    _statedb = open(_statedb_path, encoding="utf-8").read()
    check("StateDB.trades() 仍按 exit_at 升序（前置事实成立）",
          "ORDER BY exit_at" in _statedb, True)
else:
    print("  [SKIP] StateDB.py 不在场（部分树），跳过前置事实核对")
_all = list(range(12))                     # 模拟升序 trades（数字大 = 时间晚）
_recent = _all[-10:]
check("尾部 10 条原序 = 升序且含最新", (_recent[0], _recent[-1]), (2, 11))

# ═══ [6] 账本与自动下单开关解耦（2026-09-22 二次拍板）═══
print("\n[6] 账本与开关解耦：关着引擎也能看，数据全来自 state.db")
check("旧空态「（自动下单未运行）」零残留",
      ('（自动下单未运行）' in HTML) or ('（自动下单未运行）' in JS), False)
check("新空态「（暂无账本数据）」两处就位",
      (HTML.count('（暂无账本数据）') >= 1) and (JS.count('（暂无账本数据）') >= 1), True)
check("_read_engine_switch 签名改为收 out_dir",
      "def _read_engine_switch(out_dir: Optional[str])" in PY, True)
check("status() 回退上次运行目录（状态文件）",
      '_read_state_file().get("out_dir")' in PY, True)
check("回退默认目录（Trading/State）",
      'os.path.join(_DEFAULT_OUT, "state.db")' in PY, True)
check("进程不在时置空运行时队列（防陈旧告警重放）",
      ('_ao["alerts"] = []' in PY) and ('_ao["toasts"] = []' in PY), True)

# ═══ [7] DryRun 离场按触发 K 线收盘价落账（2026-09-22 拍板）═══
print("\n[7] 离场成交参考价 = 触发那根 K 线收盘价")
_ex_path = os.path.join(REPO, "Trading", "Strategy", "Exit.py")
_en_path = os.path.join(REPO, "Trading", "Engine", "Engine.py")
if os.path.exists(_ex_path) and os.path.exists(_en_path):
    EX = open(_ex_path, encoding="utf-8").read().replace("\r\n", "\n")
    EN = open(_en_path, encoding="utf-8").read().replace("\r\n", "\n")
    check("ExitCheck 新增 fill_price 字段",
          "fill_price: Optional[float] = None" in EX, True)
    # ⚠️ 2026-09-22 时序改版后，触发返回多带一个 `plan=updated`（同一根可以先按极值抬
    #   保护价、再按收盘价判出跌破，计划要随离场一并交回引擎）—— 故断言用**前缀正则**计数，
    #   而不是整条字面量：将来再追加关键字参数不会假红，但"把 fill_price=close 拿掉"或
    #   "只改多 / 空一侧"仍会变红。
    check("触发价仍是保护价本身（stop，**不被收盘价替换**）",
          len(re.findall(r"ExitCheck\(stop_reason, stop, fill_price=close", EX)), 2)
    check("触发两分支（多 / 空）全部带 fill_price=close", EX.count("fill_price=close"), 2)
    # 2026-09-22：止损侧 reason 不再是字面量 "sl"，改走 stop_reason（按 `_phase` 细分出
    #   breakeven / trailing / sl —— 离场原因细分，统计按规则身份分组）。
    #   新时序下 ExitCheck 返回共 3 处：触发两支（多 / 空）+ only_update 支（只抬价不离场），
    #   三处都复用同一变量 —— 故断言"全部走变量"（== 3）且"无一写死字面量"（== 0）。
    check("止损侧 reason 全走变量 stop_reason（多 / 空 / 只更新计划共 3 处），无一写死字面量",
          (len(re.findall(r"ExitCheck\(stop_reason,", EX)) == 3
           and len(re.findall(r'ExitCheck\(\s*"sl"', EX)) == 0), True)
    check("Engine 透传 fill_price", "fill_price=check.fill_price" in EN, True)
    check("_force_exit 优先用 fill_price 作成交参考价",
          "ref = float(fill_price) if fill_price else float(price or 0.0)" in EN, True)
else:
    print("  [SKIP] Exit.py / Engine.py 不在场（部分树），跳过离场价护栏")

# ═══ ⑨ 顶栏 chip 几何（2026-09-24 二次拍板）═══
print("\n[8] 顶栏三件套几何：开关宽度 = 账本 chip 宽度；三件套禁止折行")
CSS = open(os.path.join(REPO, "Frontend", "app.css"), encoding="utf-8").read()


def _rule(sel):
    """取某条 CSS 规则的声明体（按选择器精确匹配，不含其 .xxx 派生规则）"""
    m = re.search(re.escape(sel) + r"\s*\{(.*?)\}", CSS, re.S)
    return m.group(1) if m else ""


# 实测基线（无头 Edge 量 getBoundingClientRect，视口未被挤压时）：
#   实时 47.69×19 / 自动下单文字 54×19 / 开关 40×19 / 账本 34×19
# → 高度本来就一致（都 19px）。真正会不一致的是**头部被挤到折行**时
#   （vw≤1700 实测：实时 34 / 文字 38 / 账本 38 / 开关 19），
#   所以这里守的是 nowrap（禁止折行）+ 开关宽度，不是死守高度数值。
check("开关宽度 = 34px（= 账本 chip 实测宽度，用户二次拍板）",
      len(re.findall(r"\.auto-order-switch \{[^}]*width: 34px;", CSS)), 1)
check("滑块位移随宽度重算 = 15px（34 - 13 - 3 - 3）",
      CSS.count("translateX(15px)"), 1)
check("旧位移 21px 零残留", CSS.count("translateX(21px)"), 0)
check("实时徽标 nowrap（防折行撑到 34/49px）",
      "white-space: nowrap" in _rule(".realtime-badge"), True)
check("自动下单文字 chip nowrap（防折行撑到 38px）",
      "white-space: nowrap" in _rule(".auto-order-label"), True)
check("账本 chip nowrap（防折行撑到 38px）",
      "white-space: nowrap" in _rule(".auto-order-ledger-btn"), True)
check("账本 chip 仍是 10px 字号 + 0 7px padding（34px 宽度的来路）",
      (_rule(".auto-order-ledger-btn").count("font-size: 10px") == 1
       and _rule(".auto-order-ledger-btn").count("padding: 0 7px") == 1), True)
check("持仓序号样式 .aol-idx 就位", ".aol-idx" in CSS, True)

# ═══ 行为层：fmtAolTime 真函数逐样本比对（node） ═══
print("\n[5] fmtAolTime 行为层（node 真函数）")
m = re.search(r"(        // 账本面板时间[\s\S]*?        function fmtAolTime\(s\) \{[\s\S]*?\n        \})", JS)
check("fmtAolTime 函数源码可抽取", m is not None, True)
if m:
    node = shutil.which("node")
    if not node:
        print("  [SKIP] node 不在位，只跑静态层")
    else:
        fn_src = re.sub(r"^        ", "", m.group(1), flags=re.M)
        cases = [
            ("2026-09-22T13:32:03+08:00", "26/09/22 13:32:03"),
            ("2026-09-22 14:00:00", "26/09/22 14:00:00"),
            ("2026-09-22", "26/09/22"),
            ("", ""),
            ("garbage", "garbage"),
        ]
        script = (fn_src
                  + "\nconst cases = " + json.dumps(cases)
                  + ";\nconsole.log(JSON.stringify(cases.map(([s]) => fmtAolTime(s))));")
        tmp = os.path.join(TEST_DIR, "_aol_fmt_tmp.js")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(script)
        try:
            r = subprocess.run([node, tmp], capture_output=True,
                               text=True, timeout=30)
            if r.returncode != 0:
                check("node 执行 fmtAolTime", r.stderr.strip()[-120:], "")
            else:
                got = __import__("json").loads(r.stdout.strip())
                check("fmtAolTime 五样本输出逐字一致",
                      got, [want for _, want in cases])
        finally:
            os.remove(tmp)


# ═══ 行为层：fmtAolPx1 真函数逐样本比对（node） ═══
print("\n[6] fmtAolPx1 行为层（node 真函数）")
m1 = re.search(r"(        function fmtAolPx1\(v\) \{[\s\S]*?\n        \})", JS)
check("fmtAolPx1 函数源码可抽取", m1 is not None, True)
if m1:
    node = shutil.which("node")
    if not node:
        print("  [SKIP] node 不在位，只跑静态层")
    else:
        fn1 = re.sub(r"^        ", "", m1.group(1), flags=re.M)
        cases1 = [
            (7618, "7618.0"),
            (7594.2, "7594.2"),
            (7580, "7580.0"),
            ("7568.6", "7568.6"),
            (None, "--"),
        ]
        script1 = (fn1
                   + "\nconst cases = " + json.dumps(cases1)
                   + ";\nconsole.log(JSON.stringify(cases.map(([s]) => fmtAolPx1(s))));")
        tmp1 = os.path.join(TEST_DIR, "_aol_px1_tmp.js")
        with open(tmp1, "w", encoding="utf-8") as f:
            f.write(script1)
        try:
            r1 = subprocess.run([node, tmp1], capture_output=True,
                                text=True, timeout=30)
            if r1.returncode != 0:
                check("node 执行 fmtAolPx1", r1.stderr.strip()[-120:], "")
            else:
                got1 = __import__("json").loads(r1.stdout.strip())
                check("fmtAolPx1 五样本输出逐字一致",
                      got1, [want for _, want in cases1])
        finally:
            os.remove(tmp1)

print("\n" + "=" * 60)
print("aol_ledger_display: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

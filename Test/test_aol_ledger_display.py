# -*- coding: utf-8 -*-
"""
账本面板展示契约护栏（2026-09-22 用户拍板）
====================================================================
钉住 Frontend/index.html + app.js renderAutoOrderLedger + App/AppTrader.py
投影三处的展示契约：

  ① 面板两节标题：「持仓」「成交」——旧长标题（含「账本」字样的两节
     标题）全文件零残留；
  ② 持仓行：多/空 + 手数 + @ 入场价 + 时间，**无止损列**（renderAutoOrderLedger
     持仓分支内「止损」零命中；时间戳走 fmtAolTime）；
  ③ 成交行：时间走 fmtAolTime（YY/MM/DD HH:MM:SS）；
  ④ 成交列表排序：App/AppTrader.py 用 `_all_trades[-10:]`（库内 exit_at
     升序原序截尾），最新一条排在**最后**；`reversed(_all_trades)` 不得回潮；
  ⑤ 资源版本号：index.html 引 app.js?v=26（改前端必须抬版本号，防缓存假象）。

行为层：fmtAolTime 抽到 node 里跑真函数，逐样本比对输出（node 不在位
则 SKIP，只跑静态层）。判别力自证（护栏不恒真）：把 AppTrader.py 的
`_all_trades[-10:]` 临时变异回 `list(reversed(_all_trades))[:10]` → 本用例
必须变红（变异记录见交付说明）；index.html 标题改名 / app.js 恢复「止损」
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

with open(os.path.join(REPO, "Frontend", "index.html"), encoding="utf-8") as f:
    HTML = f.read()
with open(os.path.join(REPO, "Frontend", "app.js"), encoding="utf-8") as f:
    JS = f.read()
with open(os.path.join(REPO, "App", "AppTrader.py"), encoding="utf-8") as f:
    PY = f.read()

# ═══ ① 标题 ═══
print("\n[1] 面板两节标题")
titles = re.findall(r'class="aol-title">([^<]+)</div>', HTML)
check("标题序列 == ['持仓', '成交']", titles, ["持仓", "成交"])
check("旧标题「交易引擎账本持仓」零残留", HTML.count("交易引擎账本持仓"), 0)
check("旧标题「最近成交（交易引擎账本口径）」零残留（整串）",
      HTML.count("最近成交（交易引擎账本口径）"), 0)
check("两节标题内不再含「账本」字样",
      any("账本" in t for t in titles), False)

# ═══ ② 持仓行：无止损列 ═══
print("\n[2] 持仓行去止损列 + 时间格式化")
i0 = JS.index("function renderAutoOrderLedger(data) {")
i1 = JS.index("// 价格显示", i0)          # 下一区块横幅 = 区块结束锚
BLOCK = JS[i0:i1]
check("renderAutoOrderLedger 区块长度 > 800（防锚点抓半截）",
      len(BLOCK) > 800, True)
check("持仓/成交分支内「止损」零命中", BLOCK.count("止损"), 0)
check("时间统一走 fmtAolTime（两处）", BLOCK.count("fmtAolTime("), 2)
check("持仓行保留 @ 入场价", BLOCK.count("fmtAolPx(p.entry_price)"), 1)
check("成交行保留 入场→出场 与 净额",
      ("fmtAolPx(t.entry_price) + ' → '" in BLOCK) and ('净 ' in BLOCK), True)

# ═══ ③ fmtAolTime 存在且被 index.html 版本号护栏配套 ═══
print("\n[3] 资源版本号")
check("index.html 引 app.js?v=26", 'app.js?v=26' in HTML, True)
check("旧版本号 v=25 零残留", 'app.js?v=25' in HTML, False)

# ═══ ④ 成交列表排序（后端投影） ═══
print("\n[4] 成交列表：升序原序截尾，最新在最后")
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

print("\n" + "=" * 60)
print("aol_ledger_display: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_aol_auto_open_on_fill.py —— 报单成交后自动打开账本（2026-10-08 需求 ⑴⑵）

需求原文（用户 2026-10-08）：
  ⑴ 买卖点报单成功后，页面自动打开"账本"，类似用户手工点了一次"账本"按钮
  ⑵ 止盈止损报单成功后，页面也自动打开"账本"
  有了 ⑴ 和 ⑵，更方便用户及时获知有开仓或平仓事件发生。
用户拍板（同日）：任意实例成交都打开；打开后常驻、点页面别处关（与手工一致）；
  平仓后面板只剩"空仓" ⇒ 顶部临时补一行「刚刚：…」事件文案。

为什么必须有这一层：引擎侧 code 早就分好了类（open_filled / close_filled），
前端 `handleAutoOrderToasts` 也早就在跑 —— 缺的只是"命中成交 code → 打开面板"
这一句。而这类接线最典型的失效形态是**静默半残**：字符串改了、函数没调，静态
grep 全绿但用户永远等不到账本自己弹开。所以本用例分两层：

  静态层（[1]~[4]）：常量集合 / 函数存在 / app.html 提示行元素 / CSS 样式 /
      版本号已抬 / **跨端一致性**（前端常量 ↔ 引擎 code 字面量）。
  行为层（[5]~[8]）：把 `handleAutoOrderToasts` / `openAutoOrderLedger` /
      `setAolFlash` / `renderAutoOrderLedger` 四个**真函数**从 app.js 抽出来，
      在 node 里配 stub DOM 跑真实调用，逐样本比对面板 display / 提示行文本 /
      刷新次数。静态断言证不了渲染结果，这层才是判据。

跑法：python Test/test_aol_auto_open_on_fill.py
（node 不在位时行为层整组 SKIP，只跑静态层 —— 与 test_aol_ledger_display 同口径）
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
APPJS = os.path.join(REPO, "Frontend", "app.js")
APPHTML = os.path.join(REPO, "Frontend", "app.html")
APPCSS = os.path.join(REPO, "Frontend", "app.css")
ENGINE = os.path.join(REPO, "Trading", "Engine", "Engine.py")

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print("  [PASS] {}".format(name))
    else:
        _FAIL += 1
        print("  [FAIL] {}\n         got = {!r}\n        want = {!r}"
              .format(name, got, want))


def _read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


JS = _read(APPJS)
HTML = _read(APPHTML)
CSS = _read(APPCSS)
ENG = _read(ENGINE)

print("=" * 60)
print("报单成交自动打开账本（账本自动弹出 + 一次性事件提示行）")
print("=" * 60)

# ═══ [1] 成交 code 集合：只认报单结果，不认阶段跃迁 / 账单同步 ═══
print("\n[1] 自动打开只认成交类 code")
m_codes = re.search(r"const AOL_AUTOPEN_CODES\s*=\s*\{([^}]*)\}", JS)
check("AOL_AUTOPEN_CODES 常量存在", m_codes is not None, True)
if m_codes:
    body = m_codes.group(1)
    check("[1a] 含 open_filled（买卖点开仓成交）", "open_filled" in body, True)
    check("[1b] 含 close_filled（止盈止损离场成交）", "close_filled" in body, True)
    check("[1c] 不含 run_breakeven（阶段跃迁不是报单）",
          "run_breakeven" in body, False)
    check("[1d] 不含 run_trailing（阶段跃迁不是报单）",
          "run_trailing" in body, False)
    check("[1e] 不含 reconcile_sync（账单同步不是报单）",
          "reconcile_sync" in body, False)

# ═══ [2] 函数存在性与"幂等打开"语义（绝不能是 toggle） ═══
print("\n[2] 打开动作 = 幂等 open，不是 toggle")
check("[2a] openAutoOrderLedger 已定义",
      "function openAutoOrderLedger(" in JS, True)
check("[2b] setAolFlash 已定义", "function setAolFlash(" in JS, True)
check("[2c] bindLedgerOutsideClose 已定义（自动弹出也能点外面关）",
      "function bindLedgerOutsideClose(" in JS, True)
# 打开分支里必须是无条件置 block，不能是 willShow 那种取反切换
m_open = re.search(r"function openAutoOrderLedger\(flashMsg\) \{[\s\S]*?\n        \}", JS)
check("[2d] openAutoOrderLedger 源码可抽取", m_open is not None, True)
if m_open:
    src = m_open.group(0)
    check("[2e] 面板置为 block（无条件，不是 toggle 取反）",
          "panel.style.display = 'block'" in src, True)
    check("[2f] 不含 willShow 取反切换语义", "willShow" in src, False)
    check("[2g] 提示行在渲染之后设置（渲染会清提示行）",
          src.index("refreshLedgerPanel()") < src.index("setAolFlash("), True)
    check("[2h] 自动打开也注册「点外面关」监听",
          "bindLedgerOutsideClose()" in src, True)

# ═══ [3] app.html 提示行元素 + CSS 样式 ═══
print("\n[3] 面板顶部的事件提示行（一次性）")
check("[3a] app.html 有 #aol-flash 元素", 'id="aol-flash"' in HTML, True)
check("[3b] 默认不显示、不占位",
      bool(re.search(r'id="aol-flash"\s+style="display:none"', HTML)), True)
check("[3c] 元素在账本面板内（面板 div 之后）",
      HTML.index('id="auto-order-ledger-panel"')
      < HTML.index('id="aol-flash"'), True)
check("[3d] CSS 有 .aol-flash 样式", ".aol-flash" in CSS, True)
check("[3e] 提示行文案以「刚刚：」起头（JS 侧）",
      "el.textContent = '刚刚：' + msg" in JS, True)

# ═══ [4] 跨端一致性：前端常量 ↔ 引擎 code 字面量（改名即红） ═══
print("\n[4] 跨端一致性：引擎确实还在发这两个 code")
check("[4a] Engine.py 仍写 code=\"open_filled\"",
      'code="open_filled"' in ENG, True)
check("[4b] Engine.py 仍写 code=\"close_filled\"",
      'code="close_filled"' in ENG, True)
check("[4c] AppTrader.py 投影未过滤 code（整条 dict 透传）",
      '"toasts": [t for t in raw_toasts if isinstance(t, dict)]'
      in _read(os.path.join(REPO, "App", "AppTrader.py")), True)
check("[4d] 前端取的是 t.code（不是靠文案猜）",
      "code: String(t.code || '')" in JS, True)

# ═══ [5]~[8] 行为层：node 真函数 + stub DOM ═══
print("\n[5] 行为层：handleAutoOrderToasts 真函数逐场景（node）")
node = shutil.which("node")
if not node:
    print("  [SKIP] node 不在位，只跑静态层")
else:
    def _grab(pat):
        m = re.search(pat, JS)
        return re.sub(r"^        ", "", m.group(0), flags=re.M) if m else None

    parts = [
        _grab(r"const AOL_AUTOPEN_CODES\s*=\s*\{[^}]*\};"),
        _grab(r"function bindLedgerOutsideClose\(\) \{[\s\S]*?\n        \}"),
        _grab(r"function setAolFlash\(msg\) \{[\s\S]*?\n        \}"),
        _grab(r"function openAutoOrderLedger\(flashMsg\) \{[\s\S]*?\n        \}"),
        _grab(r"function fmtAolPx1\(v\) \{[\s\S]*?\n        \}"),
        _grab(r"function fmtAolTime\(s\) \{[\s\S]*?\n        \}"),
        _grab(r"function renderAutoOrderLedger\(led\) \{[\s\S]*?\n        \}"),
        _grab(r"function handleAutoOrderToasts\(toasts\) \{[\s\S]*?\n        \}"),
    ]
    check("[5a] 八段源码全部可抽取",
          [i for i, p in enumerate(parts) if p is None], [])

    if all(parts):
        STUB = r"""
const els = {};
let DOC_LISTENERS = 0;
function mkEl(id) {
    if (!els[id]) {
        els[id] = {
            id: id,
            style: { display: 'none' },
            textContent: '', innerHTML: '',
            contains: function () { return false; }
        };
    }
    return els[id];
}
const document = {
    getElementById: mkEl,
    addEventListener: function () { DOC_LISTENERS += 1; }
};
let autoOrderLedgerData = null;
let autoOrderSeenToastTs = {};
const SHOWN = [];
function showToast(m) { SHOWN.push(m); }
function aoSysNotify() {}
let refreshCalls = 0;
function refreshLedgerPanel() { refreshCalls += 1; }
"""
        HARNESS = r"""
const OUT = {};
function reset(panelDisplay) {
    mkEl('auto-order-ledger-panel').style.display = panelDisplay;
    mkEl('aol-flash').style.display = 'none';
    mkEl('aol-flash').textContent = '';
    mkEl('aol-positions').innerHTML = '';
    autoOrderSeenToastTs = {};
    autoOrderLedgerData = null;
    SHOWN.length = 0;
    refreshCalls = 0;
}
function snap() {
    return {
        panel: String(mkEl('auto-order-ledger-panel').style.display),
        flash: String(mkEl('aol-flash').style.display),
        flashText: String(mkEl('aol-flash').textContent),
        shown: SHOWN.slice(),
        refresh: refreshCalls
    };
}
const T = function (ts, code, msg, inst) {
    return { ts: ts, code: code, msg: msg, _instLabel: inst || '仿真 IF' };
};

// ── 场景 1：冷启动首次拉取只定水位，不回放历史（页面晚开不该补弹账本） ──
reset('none');
handleAutoOrderToasts([T(1000, 'open_filled', '开仓成交：多 2手 @ 7584.6')]);
OUT.c1_first_pull = snap();

// ── 场景 2：第二轮新成交（开仓）→ 面板打开 + 提示行带原文 ──
handleAutoOrderToasts([T(1001, 'open_filled', '开仓成交：多 2手 @ 7584.6')]);
OUT.c2_open = snap();

// ── 场景 3：面板已开着又来一条离场成交 → 仍开着（幂等，不被 toggle 关掉）──
handleAutoOrderToasts([T(1002, 'close_filled', '平仓成交·移动止盈（跟踪止损触发）：空 2手 @ 7607.8')]);
OUT.c3_close_while_open = snap();

// ── 场景 4：非成交 code（阶段跃迁）不触发 ──
reset('none');
handleAutoOrderToasts([T(2000, 'run_breakeven', '盈利达到 1R（= 5 点），进入保本策略')]);
handleAutoOrderToasts([T(2001, 'run_breakeven', '盈利达到 2R（= 10 点），进入保本策略')]);
OUT.c4_phase_no_open = snap();

// ── 场景 5：账单同步不触发 ──
reset('none');
handleAutoOrderToasts([T(3000, 'reconcile_sync', '账单已同步：本地柜台镜像 多 持仓 2 手')]);
handleAutoOrderToasts([T(3001, 'reconcile_sync', '账单已同步：本地柜台镜像 多 持仓 2 手')]);
OUT.c5_reconcile_no_open = snap();

// ── 场景 6：缺 code 的旧格式 toast 不触发（向后兼容，不误弹）──
reset('none');
handleAutoOrderToasts([T(4000, '', '开仓成交：多 2手 @ 7584.6')]);
handleAutoOrderToasts([T(4001, '', '开仓成交：多 2手 @ 7584.6')]);
OUT.c6_no_code = snap();

// ── 场景 7：提示行一次性 —— 账本数据下次刷新即清 ──
reset('none');
handleAutoOrderToasts([T(5000, 'open_filled', '开仓成交：多 2手 @ 7584.6')]);
handleAutoOrderToasts([T(5001, 'open_filled', '开仓成交：多 2手 @ 7584.6')]);
OUT.c7_flash_before = snap();
renderAutoOrderLedger({ groups: [] });
OUT.c7_flash_after = snap();

// ── 场景 8：同一轮多条成交 → 提示行取最后一条（最新）──
reset('none');
handleAutoOrderToasts([T(6000, 'open_filled', '开仓成交：多 2手 @ 7584.6')]);
handleAutoOrderToasts([
    T(6001, 'open_filled', '开仓成交：多 2手 @ 7584.6'),
    T(6002, 'close_filled', '平仓成交·止损：空 2手 @ 7576.8')
]);
OUT.c8_multi = snap();

OUT.docListeners = DOC_LISTENERS;
console.log(JSON.stringify(OUT));
"""
        script = STUB + "\n".join(parts) + "\n" + HARNESS
        tmp = os.path.join(TEST_DIR, "_aol_autopen_tmp.js")
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(script)
        try:
            r = subprocess.run([node, tmp], capture_output=True,
                               text=True, encoding="utf-8", timeout=60)
            if r.returncode != 0:
                check("[5b] node 执行自动打开链路", r.stderr.strip()[-200:], "")
            else:
                o = json.loads(r.stdout.strip())
                print("\n[6] 逐场景断言")
                check("[6a] 首次拉取不打开面板（不回放历史）",
                      o["c1_first_pull"]["panel"], "none")
                # 首次拉取连 toast 都不弹（只定水位、不回放历史，需求 ⑷ 原语义）
                check("[6b] 首次拉取也不弹 toast（定水位不回放，原行为不变）",
                      o["c1_first_pull"]["shown"], [])

                check("[6c] 开仓成交 → 面板打开",
                      o["c2_open"]["panel"], "block")
                check("[6d] 提示行可见",
                      o["c2_open"]["flash"], "block")
                check("[6e] 提示行 = 「刚刚：」+ toast 原文",
                      o["c2_open"]["flashText"],
                      "刚刚：开仓成交：多 2手 @ 7584.6")
                check("[6f] 打开即拉一次聚合账本",
                      o["c2_open"]["refresh"], 1)

                check("[6g] 面板已开时再来离场成交 → 仍开着（幂等）",
                      o["c3_close_while_open"]["panel"], "block")
                check("[6h] 提示行更新为离场文案",
                      o["c3_close_while_open"]["flashText"],
                      "刚刚：平仓成交·移动止盈（跟踪止损触发）：空 2手 @ 7607.8")

                check("[6i] 阶段跃迁 toast 不打开面板",
                      o["c4_phase_no_open"]["panel"], "none")
                check("[6j] 账单同步 toast 不打开面板",
                      o["c5_reconcile_no_open"]["panel"], "none")
                check("[6k] 缺 code 的旧格式 toast 不打开面板",
                      o["c6_no_code"]["panel"], "none")

                check("[6l] 提示行刷新前可见",
                      o["c7_flash_before"]["flash"], "block")
                check("[6m] 账本数据刷新后提示行消失（一次性）",
                      o["c7_flash_after"]["flash"], "none")
                check("[6n] 刷新后面板仍开着（没被顺手关掉）",
                      o["c7_flash_after"]["panel"], "block")

                check("[6o] 同一轮多条成交 → 提示行取最后一条",
                      o["c8_multi"]["flashText"],
                      "刚刚：平仓成交·止损：空 2手 @ 7576.8")

                print("\n[7] 点外面关（自动弹出也必须能关掉）")
                check("[7a] 自动打开时注册了一次 document 监听",
                      o["docListeners"] >= 1, True)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

        # ── [8] 手工点击仍是 toggle（自动打开没把手工语义改坏） ──
        print("\n[8] 手工点击语义未被改动")
        m_tog = re.search(
            r"function toggleAutoOrderLedger\(ev\) \{[\s\S]*?\n        \}", JS)
        check("[8a] toggleAutoOrderLedger 源码可抽取", m_tog is not None, True)
        if m_tog:
            tsrc = m_tog.group(0)
            check("[8b] 仍是 willShow 取反切换", "willShow" in tsrc, True)
            check("[8c] 手工打开也走 bindLedgerOutsideClose",
                  "bindLedgerOutsideClose()" in tsrc, True)
            check("[8d] 不再内联重复注册监听（已抽成函数）",
                  "toggleAutoOrderLedger._outside" in tsrc, False)

print("\n" + "=" * 60)
print("aol_auto_open_on_fill: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)

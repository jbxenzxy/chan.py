# -*- coding: utf-8 -*-
"""
前端多实例契约 —— 行为护栏（源码抽取 + node 真执行）
=====================================================================
背景（2026-09-30 评审 P2-1 / P2-3 / P3-5）：多实例改造后前端仍按"合约全等"
绑实例，且告警水位/冷却只有一份全局单值。三条后果都可复现（探针实测）：

  · P3-5：另一标签页把同品种写成 `CFFEX.IF2609` / `IF2609` / 小写主连时，
    `bound=null` → `autoOrderRunning` 假 false → **切合约/切周期守卫放行**
    （引擎还在跑，保护失效）；开关显示"关"、accepted 被丢弃。
  · P2-1：A 实例已确认的高 ts 会把 B 实例（上一轮后端读库瞬时失败、没被
    读到）较低 ts 的新告警判成"旧闻" → 不弹框且被随后的 ack 广播清出库，
    **不可恢复**；冷却键只带 code 时"同 code 的另一个实例"5 分钟内被静默
    `continue`（连 console 都没有）。
  · P2-3：退出弹窗读顶层 `log_tail`，退出的不是"最近一次操作"的那个实例时
    正文退化成"（日志文件不存在或为空）"+"完整日志：（未知）"。

补充（2026-10-09 用户反馈：「自动下单开着时选复盘，弹出『交易引擎已退出!』，
日志却说『文件不存在或为空』，可引擎其实还在跑、刷新页面又显示开启」）：
复盘重连的 SSE init 会重解析 `realtimeSymbol`，而**品种键**只在
`refreshAutoOrderTradable()` 里更新 ⇒ 头几拍 `autoOrderPageKey()` 为空 ⇒
`matchAutoOrderInstance` 兜底失效 ⇒ `bound=null` ⇒ `running` 假 false ⇒
既假弹退出框（实例投影缺失 → 正文退化），又让 §3.3「引擎运行中禁止复盘」
守卫静默放行。故退出探测抽出为 `_detectAutoOrderExit()`，加两条防线：
① 品种键为空的一拍不作结论（不计数也不撤膛）；②「上膛（本页绑定曾是运行中）
→ 连续两拍不在跑」才提示，报后撤膛只报一次。用户主动起停（onAutoOrderToggle
成功路径）把上膛标志复位为 null ⇒ 主动关闭绝不被报成异常退出。

前端是零构建原生 JS、仓库里没有浏览器运行时用例，故本文件用
**源码抽取 + node 真执行** 钉住这三条行为 —— 不做子串包含断言：这些词在
注释里就有，子串断言会恒绿（`test_frontend_ao_off_symbol` 已踩过这个坑）。
抽取靠"签名锚点 + 花括号配对"；锚点消失/花括号不配对即判失败（防漂移）。

运行：python Test/test_ao_multi_instance_frontend.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _check(failures, label, got, want):
    ok = (got == want)
    print(("  ✓ " if ok else "  ✗ ") + label
          + ("" if ok else " -> got {!r}, want {!r}".format(got, want)))
    if not ok:
        failures.append(label)


def _extract(js, anchor):
    """按锚点定位代码块，返回从锚点到花括号配平处为止的源码片段。

    只用花括号深度计数 —— 这批函数体里没有含 `{`/`}` 的字符串或模板字面量
    （有的话配对会错，测试会以"锚点不可定位"形式失败，不会静默通过）。
    """
    start = js.find(anchor)
    if start < 0:
        return ""
    i = js.find("{", start)
    if i < 0:
        return ""
    depth = 0
    while i < len(js):
        c = js[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
        i += 1
    return ""


def _node_run(js_source, timeout=120):
    """跑一段自证脚本，返回 (ok, stdout)。node 不在位 → (None, 原因)。"""
    node = shutil.which("node")
    if not node:
        return None, "node 不可用"
    fd, path = tempfile.mkstemp(prefix="ao_fe_guard_", suffix=".js")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(js_source)
        p = subprocess.run([node, path], capture_output=True, timeout=timeout)
        out = p.stdout.decode("utf-8", "replace")
        if p.returncode != 0:
            return False, (out + p.stderr.decode("utf-8", "replace"))
        return True, out
    except subprocess.TimeoutExpired:
        return False, "node 执行超时"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════
# 抽取锚点（改前端时这些签名就是契约）
# ══════════════════════════════════════════════════════════════════
ANCHOR_PAGE_KEY = "function autoOrderPageKey("
ANCHOR_MATCH = "function matchAutoOrderInstance("
ANCHOR_RC = "function _rcHint("
ANCHOR_ALERTS = "function handleAutoOrderAlerts("
ANCHOR_TOASTS = "function handleAutoOrderToasts("
ANCHOR_EXIT = "function _detectAutoOrderExit("
# ⑤ 本页绑定重算 + §3.3「引擎运行中」拦截（2026-10-09 假弹退出框的另一半根因）
ANCHOR_INIT = "function connectRealtimeInit("
ANCHOR_DUAL = "function connectRealtimeDual("
ANCHOR_GOTO = "window.gotoDate = function()"


def _collect(failures):
    js = _read(APP_JS)
    got = {
        "page_key": _extract(js, ANCHOR_PAGE_KEY),
        "match": _extract(js, ANCHOR_MATCH),
        "rc": _extract(js, ANCHOR_RC),
        "alerts": _extract(js, ANCHOR_ALERTS),
        "toasts": _extract(js, ANCHOR_TOASTS),
        "exit": _extract(js, ANCHOR_EXIT),
    }
    for k, v in got.items():
        _check(failures, "[0] 抽取可定位 + 花括号配平: " + k, bool(v), True)
    return got


# ══════════════════════════════════════════════════════════════════
# ① 实例绑定按**品种键**（P3-5）
# ══════════════════════════════════════════════════════════════════
def test_binding(src, failures):
    if not src["page_key"] or not src["match"]:
        return
    harness = """
'use strict';
const SRC_KEY = %s;
const SRC_MATCH = %s;
function build(rt, prod, forSym) {
  const tradable = prod ? { product: prod } : {};
  return new Function('realtimeSymbol', 'autoOrderTradable', 'autoOrderTradableFor',
    SRC_KEY + '\\n' + SRC_MATCH + '\\nreturn matchAutoOrderInstance;'
  )(rt, tradable, forSym);
}
const INST = { running: true, instance_key: 'IF', symbol: 'KQ.m@CFFEX.IF',
               log_file: '/s/gateway.log', log_tail: 'BOOM' };
const INST_RB = { running: true, instance_key: 'RB', symbol: 'KQ.m@SHFE.rb' };
const INST_OLD = { running: true, instance_key: 'KQ.m@CFFEX.IF', symbol: 'KQ.m@CFFEX.IF' };
const OUT = [];
function case_(name, rt, prod, forSym, insts, runningOnly) {
  const f = build(rt, prod, forSym);
  const r = f(insts, runningOnly);
  OUT.push({ name: name, hit: (r ? (r.instance_key || r.symbol) : null) });
}
// 同标签页（写法完全相同）—— 改造前后都该命中
case_('same-tab', 'KQ.m@CFFEX.IF', 'IF', 'KQ.m@CFFEX.IF', [INST], true);
// 另一标签页：同品种三种别的写法（改造前 bound=false，守卫放行）
case_('other-tab-month', 'CFFEX.IF2609', 'IF', 'CFFEX.IF2609', [INST], true);
case_('other-tab-bare', 'IF2609', 'IF', 'IF2609', [INST], true);
case_('other-tab-lower', 'kq.m@cffex.if', 'IF', 'kq.m@cffex.if', [INST], true);
// 别的品种不得误绑
case_('other-product', 'KQ.m@SHFE.rb', 'RB', 'KQ.m@SHFE.rb', [INST], true);
// 品种键还没到手（首拍轮询早于 /product-check 返回）→ 退回合约全等
case_('no-key-same', 'KQ.m@CFFEX.IF', '', 'KQ.m@CFFEX.IF', [INST], true);
case_('no-key-other', 'CFFEX.IF2609', '', 'CFFEX.IF2609', [INST], true);
// 旧后端（无 instances[] 时合成的实例键 = 原样 symbol）
case_('legacy-backend', 'KQ.m@CFFEX.IF', 'IF', 'KQ.m@CFFEX.IF', [INST_OLD], true);
// runningOnly：未在跑的实例不算绑定
case_('not-running', 'CFFEX.IF2609', 'IF', 'CFFEX.IF2609',
      [{ running: false, instance_key: 'IF', symbol: 'KQ.m@CFFEX.IF' }], true);
// runningOnly=false：本页品种那个"已退出"实例仍能取到（退出弹窗要它）
case_('stopped-inst', 'CFFEX.IF2609', 'IF', 'CFFEX.IF2609',
      [{ running: false, instance_key: 'IF', symbol: 'KQ.m@CFFEX.IF' }], false);
console.log(JSON.stringify(OUT));
""" % (json.dumps(src["page_key"]), json.dumps(src["match"]))

    ok, out = _node_run(harness)
    if ok is None:
        print("  [SKIP] ① 实例绑定: " + out)
        return
    if not ok:
        failures.append("① node 执行失败")
        print("[FAIL] ① node 执行失败:\n" + out[:800])
        return
    got = {r["name"]: r["hit"] for r in json.loads(out.strip().splitlines()[-1])}
    want = {
        "same-tab": "IF", "other-tab-month": "IF", "other-tab-bare": "IF",
        "other-tab-lower": "IF", "other-product": None, "no-key-same": "IF",
        "no-key-other": None, "legacy-backend": "KQ.m@CFFEX.IF",
        "not-running": None, "stopped-inst": "IF",
    }
    for k in want:
        _check(failures, "[1] 绑定: " + k, got.get(k), want[k])


# ══════════════════════════════════════════════════════════════════
# ② 告警水位/冷却 per 实例（P2-1）
# ══════════════════════════════════════════════════════════════════
def test_alerts(src, failures):
    if not src["alerts"]:
        return
    harness = """
'use strict';
const SRC = %s;
const state = { seen: {}, cool: {}, ack: [] };
function run(alerts) {
  const shown = [];
  const acked = [];
  const fn = new Function(
    'alerts', 'autoOrderSeenAlertTs', 'autoOrderAlertCool',
    'AUTO_ORDER_ALERT_COOL_MS', 'autoOrderAlertAckHold',
    'ackAutoOrderAlerts', 'ackIfAlertsSeen', 'showToast', 'aoSysNotify',
    'showAlert', 'console',
    SRC + '\\nreturn handleAutoOrderAlerts;'
  )(alerts, state.seen, state.cool, 5 * 60 * 1000, state.ackHold || 0,
    function (ts) { acked.push(ts); }, function () {},
    function (m) { shown.push(m); }, function () {},
    function (m) { shown.push(m); return { then: function () {} }; },
    { warn: function () {}, error: function () {}, log: function () {} });
  fn(alerts);
  return { shown: shown, acked: acked };
}
function alertOf(inst, ts, code, lvl) {
  return { ts: ts, code: code, level: lvl || 'severe', msg: code + '@' + inst,
           _instLabel: inst };
}
const OUT = {};
// 批 1：只有 A 实例的告警可见（B 实例上一轮后端读库失败 → 没被读到）
run([alertOf('仿真 IF', 1000, 'fund')]);
OUT.seen_after_1 = JSON.parse(JSON.stringify(state.seen));
// 批 2：B 实例较低 ts 的新告警出现 —— 单值水位会把它判成"旧闻"（不弹且被 ack）
const r2 = run([alertOf('仿真 IF', 1000, 'fund'), alertOf('仿真 AU', 900, 'fund')]);
OUT.batch2_shown = r2.shown.slice();
// 批 3：同实例同 code 的第二条（冷却内）不该连弹
const r3 = run([alertOf('仿真 AU', 950, 'fund')]);
OUT.batch3_shown = r3.shown.slice();
// 批 4：同一批里两个实例同 code —— 冷却键不带实例时 B 会被静默吞掉
const r4 = run([alertOf('仿真 IF', 1001, 'boom'), alertOf('仿真 AU', 1002, 'boom')]);
OUT.batch4_shown = r4.shown.slice();
OUT.seen_final = JSON.parse(JSON.stringify(state.seen));
console.log(JSON.stringify(OUT));
""" % (json.dumps(src["alerts"]))

    ok, out = _node_run(harness)
    if ok is None:
        print("  [SKIP] ② 告警水位: " + out)
        return
    if not ok:
        failures.append("② node 执行失败")
        print("[FAIL] ② node 执行失败:\n" + out[:800])
        return
    d = json.loads(out.strip().splitlines()[-1])
    _check(failures, "[2] 首批只记本实例水位（A=1000）",
           d["seen_after_1"].get("仿真 IF"), 1000)
    _check(failures, "[3] B 实例较低 ts 的新告警仍要弹（不被 A 的高 ts 掩盖）",
           len(d["batch2_shown"]), 1)
    _check(failures, "[4] 弹的正是 B 实例那条",
           "仿真 AU" in (d["batch2_shown"][0] if d["batch2_shown"] else ""), True)
    _check(failures, "[5] 同实例同 code 冷却内不连弹", d["batch3_shown"], [])
    # severe 是"合并成一条弹框"，所以按合并正文里两个实例都在来判（不是弹框次数）
    _merged = "\n".join(d["batch4_shown"])
    _check(failures, "[6] 同批两实例同 code 都要弹（冷却键=实例×code）",
           (len(d["batch4_shown"]), "boom@仿真 IF" in _merged,
            "boom@仿真 AU" in _merged),
           (1, True, True))
    _check(failures, "[7] 水位逐实例推进（AU=1002、IF=1001）",
           (d["seen_final"].get("仿真 AU"), d["seen_final"].get("仿真 IF")),
           (1002, 1001))


# ══════════════════════════════════════════════════════════════════
# ③ 轻提示水位 per 实例（P2-1）
# ══════════════════════════════════════════════════════════════════
def test_toasts(src, failures):
    if not src["toasts"]:
        return
    harness = """
'use strict';
const SRC = %s;
const state = { seen: {} };
// handleAutoOrderToasts 的两个外部依赖（2026-10-08 起新增）：
//   AOL_AUTOPEN_CODES —— 命中成交 code 的清单；openAutoOrderLedger —— 打开账本。
// 本组件只钉「轻提示水位 per 实例」，故前者传空集合（永不命中）、后者空实现：
// 自动打开的真实判据由 Test/test_aol_auto_open_on_fill.py 独占覆盖，
// 这里不再复制一份命中集合（两处各写一遍必然漂移）。
function run(toasts) {
  const shown = [];
  const fn = new Function(
    'toasts', 'autoOrderSeenToastTs', 'showToast', 'aoSysNotify',
    'AOL_AUTOPEN_CODES', 'openAutoOrderLedger',
    SRC + '\\nreturn handleAutoOrderToasts;'
  )(toasts, state.seen,
    function (m) { shown.push(m); }, function () {},
    {}, function () {});
  fn(toasts);
  return shown;
}
function t(inst, ts, msg) { return { ts: ts, msg: msg, _instLabel: inst }; }
const OUT = {};
// 首见实例只定水位、不回放历史（页面晚开不该把半小时前的开仓弹一遍）
OUT.b1 = run([t('仿真 IF', 1000, 'A-开仓')]);
// B 实例首次出现 —— 同样只定它自己的水位（逐实例的"不回放历史"）
OUT.b2 = run([t('仿真 AU', 900, 'B-历史')]);
// A 已见 1000；B 已见 900；现在 B 来个 950 的**新**提示
// 单值水位（=1000）会把它一起吞掉 → 改造前这条永远弹不出来
OUT.b3 = run([t('仿真 IF', 1000, 'A-开仓'), t('仿真 AU', 950, 'B-平仓')]);
OUT.seen = JSON.parse(JSON.stringify(state.seen));
console.log(JSON.stringify(OUT));
""" % (json.dumps(src["toasts"]))

    ok, out = _node_run(harness)
    if ok is None:
        print("  [SKIP] ③ 轻提示水位: " + out)
        return
    if not ok:
        failures.append("③ node 执行失败")
        print("[FAIL] ③ node 执行失败:\n" + out[:800])
        return
    d = json.loads(out.strip().splitlines()[-1])
    _check(failures, "[8] 首见实例不回放历史", d["b1"], [])
    _check(failures, "[8b] 另一实例首见也只定水位（逐实例语义一致）",
           d["b2"], [])
    _check(failures, "[9] B 实例较低 ts 的新提示不被吞", len(d["b3"]), 1)
    _check(failures, "[10] 弹的正是 B 实例那条",
           "B-平仓" in (d["b3"][0] if d["b3"] else ""), True)
    _check(failures, "[11] 轻提示水位逐实例记",
           (d["seen"].get("仿真 IF"), d["seen"].get("仿真 AU")), (1000, 950))


# ══════════════════════════════════════════════════════════════════
# ④ 异常退出探测：误报防线 + 复核两拍 + 只报一次 + 日志取本页实例
#    （P2-3 / 2026-10-09 用户反馈「选复盘时假弹『交易引擎已退出!』」）
# ══════════════════════════════════════════════════════════════════
def _exit_harness(rc_src, exit_src):
    """构造 ④ 的 node 脚本。rc_src/exit_src 分开传 ⇒ 变异自证可只换 exit 源码。"""
    return """
'use strict';
const SRC_RC = %s;
const SRC_EXIT = %s;
// 每个 mkr() = 一个"页面"（模块级状态 autoOrderPrevRunning /
// autoOrderNotRunningStreak / autoOrderBusy 都是页面私有的）。返回的
// fire/armed/streak 闭包住这些状态 ⇒ 能真跑多拍、且能看内部状态迁移。
function mkr() {
  const shown = [];
  const api = new Function(
    'showAlert', 'console',
    'let autoOrderPrevRunning = null;'
    + 'let autoOrderNotRunningStreak = 0;'
    + 'let autoOrderBusy = false;'
    + SRC_RC + '\\n' + SRC_EXIT + '\\n'
    + 'return {'
    + '  fire: function (r, pi, b, d, k) { return _detectAutoOrderExit(r, pi, b, d, k); },'
    + '  busy: function (v) { autoOrderBusy = v; },'
    + '  armed: function () { return autoOrderPrevRunning; },'
    + '  streak: function () { return autoOrderNotRunningStreak; }'
    + '};'
  )(function (m) { shown.push(m); },
    { warn: function () {}, error: function () {}, log: function () {} });
  api.shown = shown;
  return api;
}
const IF = { running: true, instance_key: 'IF', symbol: 'KQ.m@CFFEX.IF',
             log_file: '/s/IF/gateway.log', log_tail: 'RuntimeError: boom',
             exit_rc: 3221225477 };
const NODATA = { log_tail: null, log_file: null };
const TOP = { log_tail: 'top-tail', log_file: '/s/AU/gateway.log' };
const OUT = {};

// a) 误报防线：上膛后品种键未就位（key=''）→ 连续三拍都不得作结论，
//    且不得撤膛（否则键恢复后真崩溃会漏报）。若删掉 !pageKey 短路，
//    第 2 拍起就会假弹 ⇒ 本组转红。
const ma = mkr();
ma.fire(true, IF, IF, NODATA, 'IF');
OUT.a_armed = ma.armed();
ma.fire(false, null, null, NODATA, '');
ma.fire(false, null, null, NODATA, '');
ma.fire(false, null, null, NODATA, '');
OUT.a_shown = ma.shown.length;
OUT.a_armed_after = ma.armed();
OUT.a_streak = ma.streak();

// b) 复核两拍：单拍瞬态（后端读库瞬时失败 / 绑定空窗）不报；第 2 拍才报，
//    正文取本页实例的 log_tail / exit_rc / log_file。
const mb = mkr();
mb.fire(true, IF, IF, NODATA, 'IF');
mb.fire(false, IF, null, NODATA, 'IF');
OUT.b_shown1 = mb.shown.length;
mb.fire(false, IF, null, NODATA, 'IF');
OUT.b_shown2 = mb.shown.length;
OUT.b_text = mb.shown.join('\\n');
OUT.b_armed_after = mb.armed();

// c) 只报一次：已报过（撤膛）后继续不在跑，不再弹。
mb.fire(false, IF, null, NODATA, 'IF');
mb.fire(false, IF, null, NODATA, 'IF');
OUT.c_shown = mb.shown.length;

// d) 未上膛（= 用户主动关闭后 onAutoOrderToggle 复位为 null）→ 连续不在跑
//    也不许报成"异常退出"。
const md = mkr();
md.fire(false, IF, null, NODATA, 'IF');
md.fire(false, IF, null, NODATA, 'IF');
md.fire(false, IF, null, NODATA, 'IF');
OUT.d_shown = md.shown.length;

// e) 运行 → 回落 → 又运行 → 再回落：回到运行必须清零复核计数，
//    重启后的第一次回落仍要重新攒满两拍。
const me = mkr();
me.fire(true, IF, IF, NODATA, 'IF');
me.fire(false, IF, null, NODATA, 'IF');
me.fire(true, IF, IF, NODATA, 'IF');
OUT.e_streak_reset = me.streak();
me.fire(false, IF, null, NODATA, 'IF');
OUT.e_shown1 = me.shown.length;
me.fire(false, IF, null, NODATA, 'IF');
OUT.e_shown2 = me.shown.length;

// f) busy（用户正在起停，状态切换中）→ 一拍也不报。
const mf = mkr();
mf.fire(true, IF, IF, NODATA, 'IF');
mf.busy(true);
mf.fire(false, IF, null, NODATA, 'IF');
mf.fire(false, IF, null, NODATA, 'IF');
mf.fire(false, IF, null, NODATA, 'IF');
OUT.f_shown = mf.shown.length;

// g) 实例投影缺失（旧后端 / 已出注册表）→ 退回顶层，保持改造前行为。
const mg = mkr();
mg.fire(true, null, null, TOP, 'IF');
mg.fire(false, null, null, TOP, 'IF');
mg.fire(false, null, null, TOP, 'IF');
OUT.g_text = mg.shown.join('\\n');

// rc 定性（人话映射不受本次改造影响）
OUT.rc0 = (function () {
  const f = new Function(SRC_RC + '\\nreturn _rcHint;')();
  return [f(0), f(3221225477), f(3221225781), f(-1073741819), f(null), f('')];
})();
console.log(JSON.stringify(OUT));
""" % (json.dumps(rc_src), json.dumps(exit_src))


def test_exit_popup(src, failures):
    if not src["exit"] or not src["rc"]:
        return
    ok, out = _node_run(_exit_harness(src["rc"], src["exit"]))
    if ok is None:
        print("  [SKIP] ④ 退出探测: " + out)
        return
    if not ok:
        failures.append("④ node 执行失败")
        print("[FAIL] ④ node 执行失败:\n" + out[:800])
        return
    d = json.loads(out.strip().splitlines()[-1])

    print("-- 误报防线 / 复核两拍 / 只报一次 --")
    _check(failures, "[12] 上膛后 armed=true", d["a_armed"], True)
    _check(failures, "[13] 品种键未就位：连续三拍都不作结论（不弹）",
           d["a_shown"], 0)
    _check(failures, "[14] 品种键未就位：不得撤膛（键恢复后仍能报警）",
           d["a_armed_after"], True)
    _check(failures, "[15] 品种键未就位：不计数复核", d["a_streak"], 0)
    _check(failures, "[16] 单拍不在跑（瞬态）→ 不弹", d["b_shown1"], 0)
    _check(failures, "[17] 第 2 拍不在跑 → 弹（真崩溃照常报）",
           d["b_shown2"], 1)
    _check(failures, "[18] 弹后撤膛（防每拍重复弹）", d["b_armed_after"], False)
    _check(failures, "[19] 已报过再继续不在跑 → 不再弹", d["c_shown"], 1)
    _check(failures, "[20] 未上膛（用户主动关闭）→ 不报异常退出",
           d["d_shown"], 0)
    _check(failures, "[21] 回到运行 → 复核计数清零", d["e_streak_reset"], 0)
    _check(failures, "[22] 重启后第一次回落仍要重新两拍（1 拍不弹）",
           d["e_shown1"], 0)
    _check(failures, "[23] 重启后第二次回落 → 弹", d["e_shown2"], 1)
    _check(failures, "[24] busy（起停切换中）→ 不弹", d["f_shown"], 0)

    print("-- 弹窗正文取材（P2-3）--")
    _check(failures, "[25] 正文用本页实例的日志尾部",
           "RuntimeError: boom" in d["b_text"], True)
    _check(failures, "[26] 正文带出退出码定性",
           "0xC0000005" in d["b_text"], True)
    _check(failures, "[27] 用本页实例的完整日志路径",
           "/s/IF/gateway.log" in d["b_text"], True)
    _check(failures, "[28] 实例投影缺失时退回顶层（不回归）",
           ("top-tail" in d["g_text"], "/s/AU/gateway.log" in d["g_text"]),
           (True, True))

    print("-- 退出码人话 --")
    rc = d["rc0"]
    _check(failures, "[29] rc=0 → 干净返回", "干净返回" in rc[0], True)
    _check(failures, "[30] rc=0xC0000005 能定性", "0xC0000005" in rc[1], True)
    _check(failures, "[31] rc=0xC0000135 能定性", "0xC0000135" in rc[2], True)
    _check(failures, "[32] 负号形态的 0xC0000005 同样能定性",
           "0xC0000005" in rc[3], True)
    _check(failures, "[33] rc 未知 → 空串，不猜", (rc[4], rc[5]), ("", ""))


def test_exit_guard_discriminating_power(src, failures):
    """判别力自证：两条防线各做一次单点变异，对应用例必须转红。

    否则「全都通过」可能只是因为用例根本没踩到分支（恒绿）。
      · 变异 A：`autoOrderNotRunningStreak < 2` → `< 1`（取消复核两拍）
        ⇒ 单拍瞬态也会弹，[16] 必转红。
      · 变异 B：`if (!pageKey) {` → `if (false) {`（取消"绑定未知不作结论"）
        ⇒ 品种键空窗第 2 拍就假弹，[13] 必转红。
    """
    if not src["exit"] or not src["rc"]:
        return
    print("-- 判别力自证（变异：单点拆掉一条防线）--")

    cases = [
        ("streak", "autoOrderNotRunningStreak < 2", "autoOrderNotRunningStreak < 1",
         "取消复核两拍", "b_shown1", 1),
        ("pagekey", "if (!pageKey) {", "if (false) {",
         "取消绑定未知防线", "a_shown", 1),
    ]
    for name, needle, repl, label, key, want in cases:
        if needle not in src["exit"]:
            failures.append("⑨-" + name + " 未找到变异锚点: " + needle)
            print("[FAIL] 未找到变异锚点（防漂移）: " + needle)
            continue
        mutant = src["exit"].replace(needle, repl, 1)
        ok, out = _node_run(_exit_harness(src["rc"], mutant))
        if ok is None:
            print("  [SKIP] 判别力自证 " + name + ": " + out)
            return
        if not ok:
            failures.append("⑨-" + name + " 变异版 node 执行失败")
            print("[FAIL] 变异版执行失败:\n" + out[:800])
            continue
        d = json.loads(out.strip().splitlines()[-1])
        _check(failures, "[⑨-" + name + "] 变异（" + label + "）→ " + key,
               d[key], want)


# ══════════════════════════════════════════════════════════════════
# ⑤ 本页绑定在 init 后必须重算 + §3.3「引擎运行中」拦截可达
#    （2026-10-09 用户反馈：假弹「交易引擎已退出!」且守卫静默放行）
# ══════════════════════════════════════════════════════════════════
def _ordered(blk, first, second):
    """blk 中 first 出现在 second 之前（两者都必须在）。second 取**最后一次**出现。"""
    i, j = blk.find(first), blk.rfind(second)
    return i >= 0 and j >= 0 and i < j


def test_binding_recompute_and_replay_guard(failures):
    """两条接线断言（源码抽取，按函数名锚点，不锚行号）。

    ① `connectRealtimeInit`/`connectRealtimeDual` 的 SSE init 处理器改写
       `realtimeSymbol` 之后，必须**跟着**重算品种键（`syncAutoOrderWrap()`）。
       init 重解析出的完整代码与页面变量 `autoOrderTradableFor` 一开始不等 ⇒
       `autoOrderPageKey()` 空串 ⇒ `matchAutoOrderInstance` 兜底失效 ⇒
       `bound=null` ⇒ `running` 假 false。漏了这一拍，既假弹退出框、又让
       §3.3「引擎运行中禁止选点/复盘/取消」四类守卫静默放行（引擎仍在跑）。
    ② `window.gotoDate`（选点/复盘写入点）：引擎运行中必须先 showAlert 拦下，
       且**不得**顺手调用关引擎的接口/开关 —— 用户明确要求「提示让用户自己去关，
       无需自动关闭交易引擎」。
    """
    js = _read(APP_JS)
    checks = {
        "init": _extract(js, ANCHOR_INIT),
        "dual": _extract(js, ANCHOR_DUAL),
        "goto": _extract(js, ANCHOR_GOTO),
    }
    for k, blk in checks.items():
        if not blk:
            failures.append("[34] 抽取失败（锚点不可定位）: " + k)
            print("[FAIL] 抽取失败（锚点不可定位）: " + k)
            return

    print("-- 本页绑定重算接线（init 改写 symbol 后必须补一拍）--")
    _check(failures, "[34] 单窗 init：改写 realtimeSymbol 后重算品种键",
           _ordered(checks["init"], "realtimeSymbol = resolvedSymbol",
                    "syncAutoOrderWrap()"), True)
    _check(failures, "[35] 双窗 init：同上（下窗也要能绑回本页实例）",
           _ordered(checks["dual"], "realtimeSymbol = resolvedSymbol",
                    "syncAutoOrderWrap()"), True)

    print("-- §3.3 拦截：提示而非自动关引擎 --")
    _check(failures, "[36] 复盘分支：引擎运行中先拦截",
           "'交易引擎运行中，请先关闭，再复盘'" in checks["goto"], True)
    _check(failures, "[37] 取消复盘分支：同样拦截",
           "'交易引擎运行中，请先关闭，再取消复盘'" in checks["goto"], True)
    _check(failures, "[38] 拦截在复盘 SSE 连接之前（不是连完再提示）",
           _ordered(checks["goto"], "'交易引擎运行中，请先关闭，再复盘'",
                    "connectRealtimeInit("), True)
    _check(failures, "[39] 双窗复盘分支同样在连接之前",
           _ordered(checks["goto"], "'交易引擎运行中，请先关闭，再复盘'",
                    "connectRealtimeDual("), True)
    _off = [s for s in ("/api/trader/auto-order/off", "onAutoOrderToggle(",
                        "autoOrderToggle(") if s in checks["goto"]]
    _check(failures, "[40] 选点/复盘路径不得自动关闭交易引擎", _off, [])


def main():
    print("===== 前端多实例契约（源码抽取 + node 真执行） =====")
    failures = []
    src = _collect(failures)
    test_binding(src, failures)
    test_alerts(src, failures)
    test_toasts(src, failures)
    test_exit_popup(src, failures)
    test_exit_guard_discriminating_power(src, failures)
    test_binding_recompute_and_replay_guard(failures)
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

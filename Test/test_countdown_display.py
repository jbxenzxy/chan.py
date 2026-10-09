# -*- coding: utf-8 -*-
"""倒计时（K线区右上角「流逝时间」）显示判据 —— 行为护栏（源码抽取 + node 真执行）
=====================================================================
背景（2026-10-09）：用户报「期货 IM 1分钟图，右上角原先的流逝时间不见了」。
排查结论 = 判据用错了量：`_calcCountdownState` 首行是
    if (!isRealtimeMode || realtimeStartTime) return null;
它把 `realtimeStartTime`（= **是否设了视图左边界 start**）当成了
「是否是实时态」。两者的区别正是选点：

  · 选点 = 改 L 不改 R（需求⑹），选点态 R 仍是最新 ⇒ 末根K线就是
    **当前正在走**的那根，倒计时应当照常显示；
  · 只有复盘态末根冻结在复盘点（已走完）才该隐藏。

正确的判据只有一个：**末根K线是否正在走**（末根区间是否覆盖「现在」）。

本用例用「源码抽取 + node 真执行」钉住这条判据（不做子串断言——这些词
在注释里也有，子串断言会恒绿）：
  ① 实时态 + 末根正在走            → 显示
  ② 选点态（realtimeStartTime 非空）+ 末根正在走 → 显示（本次事故）
  ③ 复盘态（末根已走完）           → 不显示
  ④ 非实时态                       → 不显示
  ⑤ 日线/周线（freqSec ≥ 86400）   → 不显示
  ⑥ 无数据 / 缺字段                → 不显示
  ⑦ 每秒定时器：实时态重绘；复盘态零空转；末根刚走完那一秒重绘一次（擦除）
  ⑧ drawCountdownBar（真实实现）：状态为 null 时把边界归 null
  ⑨ 判别力自证：把判据改回旧写法（realtimeStartTime 短路）→ ② 必须转红

抽取靠「签名锚点 + 花括号配对」；锚点消失/花括号不配对即判失败（防漂移）。
node 不在位 → 整体 SKIP（返回 0，不假装通过）。

运行：python Test/test_countdown_display.py
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

# 抽取锚点（改前端时这些签名就是契约）
ANCHOR_CALC = "function _calcCountdownState("
ANCHOR_DRAW = "function drawCountdownBar("
ANCHOR_REDRAW = "function _redrawCountdown("
ANCHOR_INIT = "function connectRealtimeInit("
ANCHOR_DUAL = "function connectRealtimeDual("
ANCHOR_DISCONNECT = "function disconnectRealtime()"

# 变更前判据（判别力自证用）：把新判据改回旧写法，② 必须转红
JUDGE_NOW = "if (!isRealtimeMode) return null;"
JUDGE_PRE_FIX = "if (!isRealtimeMode || realtimeStartTime) return null;"
# 复盘挂起护条（判别力自证用）：抽掉它，⑩b 必须转红（复现「把剩余秒数走完」）
JUDGE_PENDING = "if (replayPending) return null;"


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _check(failures, label, got, want):
    ok = (got == want)
    print(("  ✓ " if ok else "  ✗ ") + label
          + ("" if ok else " -> got {!r}, want {!r}".format(got, want)))
    if not ok:
        failures.append(label)
    return ok


def _extract(js, anchor):
    """按锚点定位代码块，返回从锚点到花括号配平处为止的源码片段。

    只用花括号深度计数 —— 这批函数体里没有含 `{`/`}` 的字符串或模板字面量
    （有的话配对会错，测试会以「锚点不可定位」形式失败，不会静默通过）。
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
    fd, path = tempfile.mkstemp(prefix="countdown_guard_", suffix=".js")
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
# node 自证脚本：桩住模块级状态，真跑抽取出来的三个函数
# ══════════════════════════════════════════════════════════════════
HARNESS = r"""
'use strict';
// ── 桩：app.js 的模块级状态（被测函数闭包读取）──
var isRealtimeMode = false;
var realtimeStartTime = null;
var replayPending = false;   // 复盘挂起（点下复盘 → 复盘数据落地之间）
var currentFreq = '1m';
var chartData = null;
var FREQ_SEC_MAP_JS = {'15s':15,'1m':60,'5m':300,'15m':900,'30m':1800,'d':86400,'w':604800};
var canvas = {};          // 与 subCanvas 不同对象 ⇒ 走「上窗」边界
var subCanvas = {};
var _countdownBounds = null;
var _subCountdownBounds = null;
var renderCount = 0;
function render() { renderCount++; _countdownBounds = null; _subCountdownBounds = null; }
function _drawCountdownImpl(area, state) { return {x: 1, y: 2, w: 3, h: 4}; }

// ── 被测源码（从 Frontend/app.js 抽取，逐字）──
@@CALC@@

@@DRAW@@

@@REDRAW@@

function _pad(n) { return String(n).padStart(2, '0'); }
function _fmt(d) {
    return d.getFullYear() + '/' + _pad(d.getMonth() + 1) + '/' + _pad(d.getDate())
         + ' ' + _pad(d.getHours()) + ':' + _pad(d.getMinutes());
}
// 末根K线起点 = 「现在」所在的那一分钟（含秒被格式化截掉）⇒ 该根正在走
function _klineRunning() { return [{date: _fmt(new Date(Date.now()))}]; }
// 末根K线起点 = 1 小时前 ⇒ 该根早已走完（复盘态/休市）
function _klineFinished() { return [{date: _fmt(new Date(Date.now() - 3600 * 1000))}]; }
function _shown(st) { return st !== null && st !== undefined; }

var OUT = {};

// ① 实时态 + 末根正在走
isRealtimeMode = true; realtimeStartTime = null; currentFreq = '1m';
chartData = {klines: _klineRunning()};
OUT.c1 = _shown(_calcCountdownState());

// ② 选点态（有 start）+ 末根正在走 —— 本次事故：旧判据在此返回 null
realtimeStartTime = '2026/09/29 13:37';
chartData = {klines: _klineRunning()};
OUT.c2 = _shown(_calcCountdownState());

// ③ 复盘态：末根冻结在复盘点（已走完）⇒ 不显示
realtimeStartTime = null;
chartData = {klines: _klineFinished()};
OUT.c3 = _shown(_calcCountdownState());

// ④ 非实时态 ⇒ 不显示
isRealtimeMode = false; realtimeStartTime = null;
chartData = {klines: _klineRunning()};
OUT.c4 = _shown(_calcCountdownState());

// ⑤ 日线（freqSec ≥ 86400）⇒ 不显示
isRealtimeMode = true; currentFreq = 'd';
chartData = {klines: _klineRunning()};
OUT.c5 = _shown(_calcCountdownState());

// ⑥ 无数据 / 无 klines ⇒ 不显示
currentFreq = '1m'; chartData = null;
OUT.c6a = _shown(_calcCountdownState());
chartData = {klines: []};
OUT.c6b = _shown(_calcCountdownState());

// ⑦ 每秒定时器（真实 _redrawCountdown + 真实 render 调用计数）
//   ⑦a 实时态：每次 tick 都要重绘（进度条随时间走）
isRealtimeMode = true; realtimeStartTime = null;
chartData = {klines: _klineRunning()};
_countdownBounds = null; _subCountdownBounds = null; renderCount = 0;
_redrawCountdown(); _redrawCountdown(); _redrawCountdown();
OUT.t_live = renderCount;
//   ⑦b 复盘态（末根已走完）+ 无残留边界：零空转（不重绘）
realtimeStartTime = null;
chartData = {klines: _klineFinished()};
_countdownBounds = null; _subCountdownBounds = null; renderCount = 0;
_redrawCountdown(); _redrawCountdown(); _redrawCountdown();
OUT.t_replay = renderCount;
//   ⑦c 末根刚走完那一秒：留有一秒前的边界 ⇒ 重绘一次擦除，之后停
_countdownBounds = {x: 1, y: 2, w: 3, h: 4}; renderCount = 0;
_redrawCountdown(); _redrawCountdown(); _redrawCountdown();
OUT.t_erase = renderCount;

// ⑧ 真实 drawCountdownBar：状态为 null 时只把**焦点窗**边界归 null
//   （另一窗由它自己那次绘制负责 —— 状态是全局的，边界是每画布独立的）
isRealtimeMode = true; realtimeStartTime = null;
chartData = {klines: _klineFinished()};
canvas = {}; subCanvas = {};                  // 焦点窗 = 上窗
_countdownBounds = {x: 9}; _subCountdownBounds = {x: 9};
drawCountdownBar({x: 0, y: 0, w: 100, h: 100});
OUT.b_null_main = (_countdownBounds === null && _subCountdownBounds !== null);
subCanvas = canvas;                           // 焦点窗 = 下窗（canvas === subCanvas）
_countdownBounds = {x: 9}; _subCountdownBounds = {x: 9};
drawCountdownBar({x: 0, y: 0, w: 100, h: 100});
OUT.b_null_sub = (_subCountdownBounds === null && _countdownBounds !== null);
canvas = {}; subCanvas = {};                  // 焦点窗 = 上窗，状态非 null
chartData = {klines: _klineRunning()};
_countdownBounds = null; _subCountdownBounds = null;
drawCountdownBar({x: 0, y: 0, w: 100, h: 100});
OUT.b_live = (_countdownBounds !== null && _subCountdownBounds === null
              && _countdownBounds.w === 3);

// ⑩ 复盘挂起（点下复盘 → 复盘数据落地之间，2026-10-09 用户反馈）
//   此时 chartData 还是**实时那份**、末根正在走，但 R 已指向复盘点 ⇒ 必须立即消失
//   （报障原状：倒计时照样把剩余 15 秒走完才不见）
isRealtimeMode = true; realtimeStartTime = null; currentFreq = '1m';
chartData = {klines: _klineRunning()};
replayPending = false;
OUT.c10_live = _shown(_calcCountdownState());
replayPending = true;
OUT.c10_pending = _shown(_calcCountdownState());
//   ⑩b 挂起期间每秒定时器零空转（不会把残留进度条继续画）
_countdownBounds = null; _subCountdownBounds = null; renderCount = 0;
_redrawCountdown(); _redrawCountdown();
OUT.t_pending = renderCount;
//   ⑩c 挂起解除（数据落地）→ 判据回到「末根是否正在走」
replayPending = false;
OUT.c10_resumed = _shown(_calcCountdownState());

console.log(JSON.stringify(OUT));
"""


def _collect(failures):
    js = _read(APP_JS)
    got = {
        "calc": _extract(js, ANCHOR_CALC),
        "draw": _extract(js, ANCHOR_DRAW),
        "redraw": _extract(js, ANCHOR_REDRAW),
    }
    for k, v in got.items():
        _check(failures, "[0] 抽取可定位 + 花括号配平: " + k, bool(v), True)
    return got


def _run(src, calc_src):
    harness = (HARNESS
               .replace("@@CALC@@", calc_src)
               .replace("@@DRAW@@", src["draw"])
               .replace("@@REDRAW@@", src["redraw"]))
    ok, out = _node_run(harness)
    if ok is None:
        return None, out
    if not ok:
        return False, out
    return json.loads(out.strip().splitlines()[-1]), ""


def test_display_judgement(src, failures):
    res, err = _run(src, src["calc"])
    if res is None:
        print("  … node 不在位，跳过倒计时判据用例（{}）".format(err))
        return None
    if res is False:
        failures.append("[① ] node 执行失败")
        print("[FAIL] node 执行失败:\n" + err[:1200])
        return None

    print("-- 显示判据（只有「末根K线正在走」才画）--")
    _check(failures, "[① ] 实时态(无 start) + 末根正在走 → 显示", res["c1"], True)
    _check(failures, "[② ] 选点态(有 start) + 末根正在走 → 显示", res["c2"], True)
    _check(failures, "[③ ] 复盘态：末根已走完 → 不显示", res["c3"], False)
    _check(failures, "[④ ] 非实时态 → 不显示", res["c4"], False)
    _check(failures, "[⑤ ] 日线(freqSec>=86400) → 不显示", res["c5"], False)
    _check(failures, "[⑥ ] 无数据 / klines 为空 → 不显示",
           (res["c6a"], res["c6b"]), (False, False))

    print("-- 每秒定时器（真实 _redrawCountdown）--")
    _check(failures, "[⑦a] 实时态：3 次 tick 重绘 3 次", res["t_live"], 3)
    _check(failures, "[⑦b] 复盘态：零空转（3 次 tick 不重绘）", res["t_replay"], 0)
    _check(failures, "[⑦c] 末根刚走完那一秒：重绘 1 次擦除后停", res["t_erase"], 1)

    print("-- drawCountdownBar（真实实现）--")
    _check(failures, "[⑧a] 上窗焦点 + 状态为 null → 只归上窗边界",
           res["b_null_main"], True)
    _check(failures, "[⑧b] 下窗焦点 + 状态为 null → 只归下窗边界",
           res["b_null_sub"], True)
    _check(failures, "[⑧c] 状态非 null → 只写焦点窗边界", res["b_live"], True)

    print("-- 复盘挂起（2026-10-09 用户反馈：点下复盘必须立即消失）--")
    _check(failures, "[⑩a] 挂起前（实时态、末根在走）→ 显示", res["c10_live"], True)
    _check(failures, "[⑩b] 复盘挂起（数据未落地）→ 立即不显示",
           res["c10_pending"], False)
    _check(failures, "[⑩c] 挂起期间每秒定时器零空转", res["t_pending"], 0)
    _check(failures, "[⑩d] 挂起解除（数据落地）→ 判据恢复", res["c10_resumed"], True)
    return res


def test_replay_pending_reset_paths(failures):
    """replayPending 的置位/复位路径（缺一条 = 倒计时永久隐藏或挂起不生效）。

    置位：两个 connect（单窗/双窗）在带 end_time（复盘）时置位；
    复位：数据落地（两个 init 处理器）、disconnectRealtime、SSE onerror。
    抽取 connect*/disconnectRealtime 整块来断言（不锚行号）。
    """
    js = _read(APP_JS)
    checks = [
        (ANCHOR_INIT, "单窗复盘置位（connectRealtimeInit 带 end_time）",
         "replayPending = !!endTime"),
        (ANCHOR_DUAL, "双窗复盘置位（connectRealtimeDual 带 end_time）",
         "replayPending = !!endTime"),
        (ANCHOR_INIT, "单窗数据落地复位（init 处理器）", "replayPending = false"),
        (ANCHOR_DUAL, "双窗数据落地复位（init 处理器）", "replayPending = false"),
        (ANCHOR_DISCONNECT, "离开实时链复位（disconnectRealtime）",
         "replayPending = false"),
    ]
    for anchor, label, needle in checks:
        blk = _extract(js, anchor)
        if not blk:
            failures.append("[⑪ ] 抽取失败: " + anchor)
            print("[FAIL] 抽取失败（锚点不可定位）: " + anchor)
            continue
        _check(failures, "[⑪ ] " + label, needle in blk, True)


def test_discriminating_power(src, failures, real):
    """判别力自证：判据改回旧写法（realtimeStartTime 短路）⇒ ② 必须转红。"""
    if real is None:
        return
    print("-- 判别力自证（变异：判据改回 realtimeStartTime 短路）--")
    if JUDGE_NOW not in src["calc"]:
        failures.append("[⑨ ] 未在抽取源码中找到新判据锚点: " + JUDGE_NOW)
        print("[FAIL] 未找到新判据锚点，无法做变异自证")
        return
    mutant = src["calc"].replace(JUDGE_NOW, JUDGE_PRE_FIX, 1)
    res, err = _run(src, mutant)
    if res is None or res is False:
        failures.append("[⑨ ] 变异版本 node 执行失败")
        print("[FAIL] 变异版本执行失败:\n" + str(err)[:800])
        return
    _check(failures, "[⑨a] 变异版：② 选点态被隐藏（复现事故原状）", res["c2"], False)
    _check(failures, "[⑨b] 变异版：① 实时态仍显示（变异只碰选点态）", res["c1"], True)

    # 第二处判别力自证：抽掉复盘挂起护条 ⇒ ⑩b 必须转红（复现「走完剩余秒数」）
    if JUDGE_PENDING not in src["calc"]:
        failures.append("[⑫ ] 未在抽取源码中找到复盘挂起锚点: " + JUDGE_PENDING)
        print("[FAIL] 未找到复盘挂起锚点，无法做变异自证")
        return
    mutant2 = src["calc"].replace(JUDGE_PENDING, "", 1)
    res2, err2 = _run(src, mutant2)
    if res2 is None or res2 is False:
        failures.append("[⑫ ] 挂起变异版本 node 执行失败")
        print("[FAIL] 挂起变异版本执行失败:\n" + str(err2)[:800])
        return
    _check(failures, "[⑫a] 变异版：挂起期间倒计时照样显示（复现报障原状）",
           res2["c10_pending"], True)
    _check(failures, "[⑫b] 变异版：挂起前后判据不受影响",
           (res2["c10_live"], res2["c10_resumed"]), (True, True))


def main():
    print("===== 倒计时显示判据（源码抽取 + node 真执行） =====")
    failures = []
    src = _collect(failures)
    if not all(src.values()):
        print("\n===== 结果: 失败 {} 项（抽取锚点不可定位）=====".format(len(failures)))
        for x in failures:
            print(" -", x)
        return False
    real = test_display_judgement(src, failures)
    print()
    print("-- 复盘挂起置位/复位路径（缺一条 = 永久隐藏 或 挂起不生效）--")
    test_replay_pending_reset_paths(failures)
    test_discriminating_power(src, failures, real)
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

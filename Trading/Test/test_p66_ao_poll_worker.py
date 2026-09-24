# -*- coding: utf-8 -*-
"""
P66 自动下单轮询移 Web Worker（后台通知延迟修复）
================================================
背景（2026-09-23 实盘现象）：开仓成交后快期3 秒级显示仓单，Win11 右下角
系统通知却延迟一分钟级别才出。events.jsonl 铁证：order filled 与 toast
事件**同一秒**（交易引擎侧零延迟），App 侧 `_read_engine_switch` 每次新开连接
实时读 WAL —— 延迟全部在「前端轮询」环节：主线程 setInterval 在页面后台
≥5 分钟后被 Chrome intensive throttling 节流到 ~1 次/分钟。

修复：轮询本体移到 Web Worker —— r7 为独立文件 ao-poll-worker.js，r8 应用户
「不想多一个文件」的要求把 Worker 源码内嵌进 app.js（const AO_WORKER_SRC 模板
字符串 + Blob URL 创建；Worker 独立事件循环的定时器不受节流，与创建方式无关）。
Worker 每 5s 拉一次 status 并 postMessage
回主线程，主线程收到消息**立即**应用（消息任务是普通任务，不被节流）。
Worker 创建失败（老浏览器）回退主线程 setInterval（旧行为）。

2026-09-24 根因修复（本轮改判据的真正原因）：
  Worker 由 **Blob URL** 创建 → self.location.href = blob:http://host/<uuid>；
  blob: 是 cannot-be-a-base scheme，**无法解析相对路径**。无头 Edge/Chromium 实测：
      new URL('/ping', self.location.href) → throw Invalid URL
      fetch('/ping')                       → TypeError: Failed to parse URL from /ping
      fetch(location.origin + '/ping')     → 200 ✅
  于是每一次 _poll 都在 fetch 那一行抛 TypeError，被 .catch 静默吞掉 —— 轮询
  "活着"但永远没有数据：开平仓弹窗不弹、账本不刷、保护价线不画，只有手动开关
  自动下单（onAutoOrderToggle 里那一次主线程 poll）才刷新一拍（实盘表现即
  "重开后连弹 5 条积压 + 账本同时刷新"）。
  ⚠️ 更糟的是：旧判据 [1c]/[3-T6] 把「根相对 URL」**钉成了契约**（断言
  fetchUrls[0] === '/api/trader/auto-order/status'）—— 缺陷被护栏保护着，
  怎么改都绿。故本轮把这两条改成「绝对 URL」，并加 [1h]/[1i]/[3-T8]/[4-U8]：
  根相对 fetch 零残留、start 必带 base、失败不再静默。

本测试钉死四件事：
  [1] Worker 源码段：5s 周期唯一来源（POLL_MS）、status **绝对** URL（_statusUrl，
      根相对零残留）+ no-store、start/stop 消息协议（start 收 base + 启动即拉
      一次；stop 清定时器）、postMessage 消息形状（type:'status' + data）、
      失败回传（type:'poll-error'，不再静默）；
  [2] app.js 接线：Blob URL 创建 Worker、onmessage →
      applyAutoOrderStatus(d.data)、onerror → fallback（回退定时器）、
      pollAutoOrderStatus 与 Worker 共用 applyAutoOrderStatus、
      主线程周期 setInterval 只存在于 fallback 分支；
  [3] 行为矩阵：从 app.js 抽出 Worker 源码段，用 **node 跑真代码**（模拟
      self/定时器/fetch）—— start 后立即拉一次 + 周期 5000 + 每 tick 一次
      fetch → 一条 status 消息 + stop 清定时器 + 根相对 URL + no-store；
  [4] **接线级**探针：抽出 startAutoOrderPolling IIFE 真片段用 node 跑，
      断言主线程**真的** postMessage({type:'start'}) —— 创建侧与驱动侧
      分离，只审创建侧的静态判据对「建了 Worker 却从没发 start」恒绿。

判别力（护栏不恒真）：
  status 写回根相对 URL（2026-09-24 根因） → [1c]/[1c3]/[3-T6]
  start 去掉 base                          → [1h]/[2g]/[4-U8]
  失败改回静默 catch（删 poll-error）      → [1i]/[2h]/[3-T8]
  Worker 回退成主线程定时器     → [2a]/[3-*]/[4-U2]（周期行为变）
  删掉「启动即拉一次」          → [3-T1]
  周期改大/写死第二处           → [1a]/[3-T2]
  消息形状变化（丢 data）       → [1d]/[3-T4]/[2b]
  建了 Worker 但没发 start      → [4-U1]（[1][2][3] 全绿，只有 [4] 能抓：
                                  2026-09-24 实测，缺这一行时前三组
                                  20 passed / 0 failed 而轮询归零）

资源版本号不在本文件断言 —— 由 Test/test_aol_ledger_display.py ⑤ 组独占守卫。

跑法：python Trading/Test/test_p66_ao_poll_worker.py
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or os.path.dirname(_HERE)
_REPO = os.path.dirname(_TG_ROOT)

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


_JS_P = os.path.join(_REPO, "Frontend", "app.js")
if not os.path.isfile(_JS_P):
    print("✗ 找不到 {}".format(_JS_P))
    sys.exit(2)
JS = io.open(_JS_P, encoding="utf-8").read()

# r8：Worker 源码已内嵌进 app.js（const AO_WORKER_SRC = `...`;，经 Blob URL
# 创建，独立文件 ao-poll-worker.js 已取消）—— 判据对象改为「从 app.js 抽取
# 的 Worker 源码段」，判据本身与文件时代完全一致。
_m = re.search(r"const AO_WORKER_SRC = `([^`]*)`;", JS, re.S)
if not _m:
    print("✗ app.js 中找不到 AO_WORKER_SRC 模板字符串（内嵌 Worker 源码）")
    sys.exit(2)
WK = _m.group(1)


# ════════════════════════════════════════════════════════════════
# [1] Worker 源码段判据
# ════════════════════════════════════════════════════════════════
print("\n[1] Worker 源码段（app.js 内嵌 AO_WORKER_SRC）")
check("[1a] 周期唯一来源 POLL_MS = 5000", "var POLL_MS = 5000;" in WK, True)
check("[1b] setInterval 用 POLL_MS 变量（不写死第二处）",
      WK.count("setInterval(_poll, POLL_MS)") == 1
      and "setInterval(_poll, 5000)" not in WK, True)
# ⚠️ 2026-09-24 根因位：这里**曾经**断言根相对 URL —— 那正是"轮询永不成功"的
#    缺陷本身被护栏保护着。现在守的是"绝对 URL（base + 路径）"，并反向钉死
#    根相对写法零残留（谁再写回 _timeoutedFetch('/api/...') 立刻红）。
check("[1c] status 走 _statusUrl()（绝对 URL），no-store 在 helper 内",
      "_timeoutedFetch(_statusUrl())" in WK and "cache: 'no-store'" in WK, True)
check("[1c2] _statusUrl 用 API_BASE/origin 拼绝对 URL",
      WK.count("function _statusUrl()") == 1
      and "(API_BASE || self.location.origin || '')" in WK
      and "+ '/api/trader/auto-order/status'" in WK, True)
check("[1c3] 根相对 fetch 零残留（blob: Worker 解析不出来 → 每轮必抛 TypeError）",
      "_timeoutedFetch('/api/" in WK, False)
check("[1d] 消息形状 type:'status' + data",
      "postMessage({ type: 'status', data: j })" in WK, True)
check("[1e] start 协议：建定时器 + 启动即拉一次",
      "d.type === 'start'" in WK
      and "_timer = setInterval(_poll, POLL_MS);" in WK
      and WK.count("_poll();") == 1, True)
check("[1f] stop 协议：clearInterval 清理",
      "d.type === 'stop'" in WK and "clearInterval(_timer)" in WK, True)
check("[1g] _poll 只定义一次", WK.count("function _poll(") == 1, True)
# 2026-09-24 根因配套：base 由主线程下发（blob: Worker 无法自己解析相对路径）
check("[1h] start 收 base 并落到 API_BASE",
      "if (d.base) API_BASE = d.base;" in WK, True)
# 静默 catch 是这次根因潜伏一整天的直接原因 —— 失败必须回传主线程
check("[1i] 拉取失败回传 poll-error（不再静默吞掉）",
      "type: 'poll-error'" in WK, True)

# ════════════════════════════════════════════════════════════════
# [2] app.js 接线判据
# ════════════════════════════════════════════════════════════════
print("\n[2] app.js 接线")
check("[2a] 创建 Blob Worker（AO_WORKER_SRC + createObjectURL）",
      "const _wblob = new Blob([AO_WORKER_SRC]," in JS
      and "new Worker(URL.createObjectURL(_wblob))" in JS, True)
check("[2b] onmessage → applyAutoOrderStatus(d.data)",
      "if (d.type === 'status' && d.data) applyAutoOrderStatus(d.data);" in JS, True)
check("[2c] onerror → fallback 回退主线程定时器",
      "w.onerror = function() { fallback(); };" in JS, True)
check("[2d] applyAutoOrderStatus 定义恰好一次",
      JS.count("function applyAutoOrderStatus(data)") == 1, True)
check("[2e] pollAutoOrderStatus 复用 applyAutoOrderStatus（不再自带处理体）",
      "applyAutoOrderStatus(await resp.json());" in JS, True)
check("[2f] 主线程周期 setInterval 只剩 fallback 分支一处",
      JS.count("autoOrderPollTimer = setInterval") == 1
      and JS.index("autoOrderPollTimer = setInterval")
      > JS.index("function fallback()"), True)
# 2026-09-24 根因位：start **必须带 base**（= location.origin）。少了 base，
# Worker 侧只能退到 self.location.origin —— 在 blob: 下那也是 blob: 串，
# 拼出来的 URL 照样请求不出去（行为由 [3-T6]/[4-U8] 钉死）。
check("[2g] 创建 Worker 后立即发 start，且带 base: location.origin",
      JS.count("w.postMessage({ type: 'start', base: location.origin });") == 1,
      True)
check("[2h] 主线程接收 poll-error 并打 console.error（失败可见）",
      "d.type === 'poll-error'" in JS and "[auto-order] Worker 轮询失败" in JS,
      True)

# ════════════════════════════════════════════════════════════════
# [3] node 跑真 Worker 代码（模拟 self / 定时器 / fetch）
# ════════════════════════════════════════════════════════════════
print("\n[3] Worker 行为矩阵（node 跑真代码）")

_PROBE = """
const posted = [];
const timers = [];
const fetchUrls = [];
const fetchOpts = [];
let timerId = 0;
let fetchCalls = 0;

function fakeSetInterval(fn, ms) {
    const id = ++timerId;
    timers.push({ id: id, fn: fn, ms: ms, cleared: false });
    return id;
}
function fakeClearInterval(id) {
    for (let i = 0; i < timers.length; i++) {
        if (timers[i].id === id) timers[i].cleared = true;
    }
}
function fakeFetch(url, opts) {
    fetchCalls += 1;
    fetchUrls.push(url);
    fetchOpts.push(opts || {});
    return Promise.resolve({
        ok: true,
        json: function () {
            return Promise.resolve({ running: true, auto_order: { enabled: true } });
        }
    });
}

const selfObj = { postMessage: function (m) { posted.push(m); } };
const fn = new Function('self', 'setInterval', 'clearInterval', 'fetch', WORKER_SRC);
fn(selfObj, fakeSetInterval, fakeClearInterval, fakeFetch);

// start：应立即拉一次 + 建 5s 周期定时器（base 由主线程下发 —— 2026-09-24 根因位）
selfObj.onmessage({ data: { type: 'start', base: 'http://probe-origin' } });
console.log('T1=' + (fetchCalls === 1 ? 'ok' : 'FAIL') + ' fetch_after_start');
const t = timers[timers.length - 1];
console.log('T2=' + (t && t.ms === 5000 ? 'ok' : 'FAIL') + ' interval_ms_5000');

// 触发一个周期：fetch 再一次 → 消息一条（status + data）
t.fn();
setTimeout(function () {
    console.log('T3=' + (fetchCalls === 2 ? 'ok' : 'FAIL') + ' fetch_per_tick');
    console.log('T4=' +
        (posted.length === 2 && posted.every(function (m) {
            return m.type === 'status' && m.data && m.data.running === true;
        }) ? 'ok' : 'FAIL')
        + ' one_status_msg_per_fetch');

    // stop：定时器被清
    selfObj.onmessage({ data: { type: 'stop' } });
    console.log('T5=' + (t.cleared === true ? 'ok' : 'FAIL') + ' stop_clears_timer');

    // ⚠️ 2026-09-24：这里**曾经**断言根相对 URL —— 那正是缺陷本身。
    //    blob: Worker 里根相对路径解析不出来（实测 TypeError），必须绝对 URL。
    console.log('T6=' +
        (fetchUrls[0] === 'http://probe-origin/api/trader/auto-order/status'
            ? 'ok' : 'FAIL')
        + ' url_absolute_from_base');
    console.log('T7=' +
        (fetchOpts[0].cache === 'no-store' ? 'ok' : 'FAIL') + ' cache_no_store');

    // T8：拉取失败必须回传 poll-error（静默 catch 是这次根因潜伏一整天的直接原因）
    const posted2 = [];
    const selfObj2 = { postMessage: function (m) { posted2.push(m); } };
    function fakeFetchReject() {
        return Promise.reject(new TypeError('Failed to parse URL from /api/x'));
    }
    const fn2 = new Function('self', 'setInterval', 'clearInterval', 'fetch', WORKER_SRC);
    fn2(selfObj2, fakeSetInterval, fakeClearInterval, fakeFetchReject);
    selfObj2.onmessage({ data: { type: 'start', base: 'http://probe-origin' } });
    setTimeout(function () {
        console.log('T8=' +
            (posted2.length >= 1 && posted2[0].type === 'poll-error'
                ? 'ok' : 'FAIL')
            + ' poll_error_posted_on_failure');
    }, 30);
}, 50);
"""

_probe_js = ("const WORKER_SRC = " + json.dumps(WK) + ";\n" + _PROBE)
_tmp_js = os.path.join(_HERE, "_p66_worker_probe.js")
io.open(_tmp_js, "w", encoding="utf-8").write(_probe_js)
try:
    r = subprocess.run(["node", _tmp_js], capture_output=True, text=True,
                       errors="replace", timeout=60)
    out = r.stdout + r.stderr
    for tag in ("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"):
        want_line = tag + "=ok"
        check("[3-{}] {}".format(tag, {
            "T1": "start 后立即拉一次",
            "T2": "周期 5000ms",
            "T3": "每 tick 恰一次 fetch",
            "T4": "postMessage 一条 status（含 data.running）",
            "T5": "stop 清定时器",
            "T6": "status 是绝对 URL（base + 路径；根相对在 blob: 下必抛）",
            "T7": "no-store",
            "T8": "拉取失败回传 poll-error（不再静默）"}[tag]),
            want_line in out, True)
finally:
    if os.path.isfile(_tmp_js):
        os.remove(_tmp_js)

# ════════════════════════════════════════════════════════════════
# [4] 接线级探针：node 跑真 startAutoOrderPolling 片段
#
#     为什么必须加这一组：创建侧与驱动侧是分离的 —— Worker 侧的定时器只
#     在收到 {type:'start'} 时才建立。只审创建侧的静态判据（[1][2][3]）
#     对「建了 Worker 但从没发 start」恒绿：2026-09-24 实测，把缺的那一行
#     补上前后，[1][2][3] 三组都是 20 passed / 0 failed，而运行时两条轮询
#     路径同时归零（新的 Worker 定时器没建、旧的主线程 setInterval 已删，
#     fallback 只在无 Worker / 构造抛错 / onerror 三条路径触发）。
#     故这里抽出 IIFE 真片段注入桩执行，断言「消息真的发出去了」。
# ════════════════════════════════════════════════════════════════
print("\n[4] 接线级探针（node 跑真 startAutoOrderPolling）")

_m2 = re.search(r"\(function startAutoOrderPolling\(\) \{.*?\n        \}\)\(\);",
                JS, re.S)
if not _m2:
    print("✗ app.js 中找不到 startAutoOrderPolling IIFE")
    sys.exit(2)
WIRE = _m2.group(0)

_WIRE_PROBE = """
function run(opts) {
    let pollCalls = 0;
    const workers = [];
    const timers = [];
    const posted = [];
    const ctx = {
        // 片段里引用的外层常量：不注入会在 new Worker 那一行 ReferenceError
        // → 落进 catch → 静默回退主线程（正是 [4] 组要防的"看着建了其实没建"）
        AO_WORKER_SRC: 'probe-worker-src',
        autoOrderPollTimer: null,
        autoOrderWorker: null,
        pollAutoOrderStatus: function () { pollCalls += 1; },
        Worker: opts.noWorker ? undefined : function (url) {
            this.url = url;
            this.postMessage = function (m) { posted.push(m); };
            workers.push(this);
        },
        Blob: function (parts, o) { this.parts = parts; this.type = o && o.type; },
        URL: { createObjectURL: function () { return 'blob:probe'; } },
        // 2026-09-24：片段现在要读 location.origin 下发 base —— 不注入会在
        // postMessage 那一行 ReferenceError → 落进 catch → 静默回退主线程，
        // 正是 [4] 组要防的"看着建了其实没建/没发"。
        location: { origin: 'http://probe-origin' },
        document: {
            getElementById: function () {
                return { classList: { contains: function () {
                    return opts.visible !== false;
                } } };
            }
        },
        setInterval: function (fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; },
        clearInterval: function () {},
        console: console
    };
    // with：让片段里对 autoOrderPollTimer / autoOrderWorker 的赋值落到 ctx 上
    const fn = new Function('ctx', 'with (ctx) {' + WIRE_SRC + '}');
    fn(ctx);
    return {
        ctx: ctx, workers: workers, timers: timers, posted: posted,
        getPollCalls: function () { return pollCalls; }
    };
}

// 主路径：Worker 可用
const a = run({});
console.log('U1=' + (a.posted.length === 1 && a.posted[0].type === 'start'
    ? 'ok' : 'FAIL') + ' start_msg_sent_to_worker');
console.log('U2=' + (a.workers.length === 1 ? 'ok' : 'FAIL') + ' worker_created');
console.log('U3=' + (a.timers.length === 0 && a.ctx.autoOrderPollTimer === null
    ? 'ok' : 'FAIL') + ' no_main_thread_timer_when_worker_ok');
console.log('U4=' + (a.ctx.autoOrderWorker !== null ? 'ok' : 'FAIL')
    + ' worker_handle_kept');
// U8（2026-09-24 根因位）：start 消息必须带 base —— 不带的话 Worker 侧
// 只能退到 self.location.origin（blob: 串），拼出的 URL 请求不出去，
// 症状与"没发 start"完全一致（轮询永不成功），故单独钉一条。
console.log('U8=' + (a.posted.length === 1
    && a.posted[0].base === 'http://probe-origin' ? 'ok' : 'FAIL')
    + ' start_msg_carries_origin_base');

// 回退路径：无 Worker 环境 → 主线程 5s 定时器（旧行为健在）
const b = run({ noWorker: true });
console.log('U5=' + (b.timers.length === 1 && b.timers[0].ms === 5000
    ? 'ok' : 'FAIL') + ' fallback_timer_5000_without_worker');
b.timers[0].fn();
console.log('U6=' + (b.getPollCalls() === 1 ? 'ok' : 'FAIL')
    + ' fallback_polls_when_visible');

// 回退路径的可见性门控仍在（非实时页不发无谓请求）
const c = run({ noWorker: true, visible: false });
c.timers[0].fn();
console.log('U7=' + (c.getPollCalls() === 0 ? 'ok' : 'FAIL')
    + ' fallback_gated_by_visibility');
"""

_probe2_js = ("const WIRE_SRC = " + json.dumps(WIRE) + ";\n" + _WIRE_PROBE)
_tmp_js2 = os.path.join(_HERE, "_p66_wire_probe.js")
io.open(_tmp_js2, "w", encoding="utf-8").write(_probe2_js)
try:
    r2 = subprocess.run(["node", _tmp_js2], capture_output=True, text=True,
                        errors="replace", timeout=60)
    out2 = r2.stdout + r2.stderr
    for tag in ("U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8"):
        check("[4-{}] {}".format(tag, {
            "U1": "主线程真的向 Worker 发了 {type:'start'}（不发 = 轮询永不建立）",
            "U2": "Worker 真的被创建",
            "U3": "Worker 可用时不建主线程定时器（无双轮询）",
            "U4": "Worker 句柄被保存（供后续 stop/回收）",
            "U8": "start 带 base = location.origin（不带 = Worker 拼不出绝对 URL）",
            "U5": "无 Worker 环境回退主线程 5s 定时器",
            "U6": "回退定时器在可见时真的调 pollAutoOrderStatus",
            "U7": "回退定时器保留 wrap.visible 门控"}[tag]),
            tag + "=ok" in out2, True)
finally:
    if os.path.isfile(_tmp_js2):
        os.remove(_tmp_js2)

print("\n==== P66：{} passed, {} failed ====".format(_PASS, _FAIL))
sys.exit(0 if _FAIL == 0 else 1)

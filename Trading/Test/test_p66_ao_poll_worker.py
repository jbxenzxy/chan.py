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

本测试钉死三件事：
  [1] Worker 源码段：5s 周期唯一来源（POLL_MS）、status 端点 + no-store、
      start/stop 消息协议（start 启动即拉一次；stop 清定时器）、postMessage
      消息形状（type:'status' + data）；
  [2] app.js 接线：Blob URL 创建 Worker、onmessage →
      applyAutoOrderStatus(d.data)、onerror → fallback（回退定时器）、
      pollAutoOrderStatus 与 Worker 共用 applyAutoOrderStatus、
      主线程周期 setInterval 只存在于 fallback 分支；
  [3] 行为矩阵：从 app.js 抽出 Worker 源码段，用 **node 跑真代码**（模拟
      self/定时器/fetch）—— start 后立即拉一次 + 周期 5000 + 每 tick 一次
      fetch → 一条 status 消息 + stop 清定时器 + 根相对 URL + no-store。

判别力（护栏不恒真）：
  Worker 回退成主线程定时器     → [2a]/[3-*]（周期行为变）
  删掉「启动即拉一次」          → [3-T1]
  周期改大/写死第二处           → [1a]/[3-T2]
  消息形状变化（丢 data）       → [1d]/[3-T4]/[2b]

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
check("[1c] status 端点 + no-store",
      "fetch('/api/trader/auto-order/status', { cache: 'no-store' })" in WK, True)
check("[1d] 消息形状 type:'status' + data",
      "postMessage({ type: 'status', data: j })" in WK, True)
check("[1e] start 协议：建定时器 + 启动即拉一次",
      "d.type === 'start'" in WK
      and "_timer = setInterval(_poll, POLL_MS);" in WK
      and WK.count("_poll();") == 1, True)
check("[1f] stop 协议：clearInterval 清理",
      "d.type === 'stop'" in WK and "clearInterval(_timer)" in WK, True)
check("[1g] _poll 只定义一次", WK.count("function _poll(") == 1, True)

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

// start：应立即拉一次 + 建 5s 周期定时器
selfObj.onmessage({ data: { type: 'start' } });
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

    console.log('T6=' +
        (fetchUrls[0] === '/api/trader/auto-order/status' ? 'ok' : 'FAIL')
        + ' url_root_relative');
    console.log('T7=' +
        (fetchOpts[0].cache === 'no-store' ? 'ok' : 'FAIL') + ' cache_no_store');
}, 50);
"""

_probe_js = ("const WORKER_SRC = " + json.dumps(WK) + ";\n" + _PROBE)
_tmp_js = os.path.join(_HERE, "_p66_worker_probe.js")
io.open(_tmp_js, "w", encoding="utf-8").write(_probe_js)
try:
    r = subprocess.run(["node", _tmp_js], capture_output=True, text=True,
                       errors="replace", timeout=60)
    out = r.stdout + r.stderr
    for tag in ("T1", "T2", "T3", "T4", "T5", "T6", "T7"):
        want_line = tag + "=ok"
        check("[3-{}] {}".format(tag, {
            "T1": "start 后立即拉一次",
            "T2": "周期 5000ms",
            "T3": "每 tick 恰一次 fetch",
            "T4": "postMessage 一条 status（含 data.running）",
            "T5": "stop 清定时器",
            "T6": "根相对 URL",
            "T7": "no-store"}[tag]),
            want_line in out, True)
finally:
    if os.path.isfile(_tmp_js):
        os.remove(_tmp_js)

print("\n==== P66：{} passed, {} failed ====".format(_PASS, _FAIL))
sys.exit(0 if _FAIL == 0 else 1)

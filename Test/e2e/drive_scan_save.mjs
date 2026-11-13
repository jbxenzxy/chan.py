// 「保存到自选」真浏览器 E2E 驱动（零 npm 依赖：只用 Node 内置 fetch + WebSocket + CDP）
//
// 为什么要真浏览器：`Test/test_scan_panel_collapse.py` 的 node 桩把
// setTimeout 换成假时钟、把 DOM 换成手写对象，能覆盖状态机**分支**，但覆盖不了
// **渲染时序**——真 4000ms 才触发的那一帧提示、`disabled` 元素在真浏览器里
// 根本不派发 click、`innerHTML` 里的 `&nbsp;` 在 textContent 里变成 \u00a0。
//
// 用法：node drive_scan_save.mjs <baseUrl> <edgePath>
// 输出：一行 JSON（checks / console 异常 / 请求记录），由 Python 侧解析。

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BASE = process.argv[2];
const EDGE = process.argv[3];
if (!BASE || !EDGE) {
  console.log(JSON.stringify({ fatal: 'usage: drive_scan_save.mjs <baseUrl> <edgePath>' }));
  process.exit(2);
}

const checks = [];
const consoleErrors = [];
const record = (id, ok, detail) => checks.push({ id, ok: !!ok, detail: String(detail) });

// ── 起浏览器 ────────────────────────────────────────────────────────
const udd = fs.mkdtempSync(path.join(os.tmpdir(), 'wb-e2e-'));
const proc = spawn(EDGE, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--disable-background-networking', '--disable-sync', '--disable-features=Translate',
  '--remote-debugging-port=0', `--user-data-dir=${udd}`, 'about:blank',
], { stdio: ['ignore', 'pipe', 'pipe'] });
let browserErr = '';
proc.stderr.on('data', (d) => { browserErr += d.toString(); });

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function cleanup(code) {
  try { proc.kill(); } catch { /* ignore */ }
  setTimeout(() => {
    try { fs.rmSync(udd, { recursive: true, force: true, maxRetries: 6 }); } catch { /* ignore */ }
    process.exit(code);
  }, 400);
}

// 等 DevToolsActivePort（--remote-debugging-port=0 时端口由浏览器写文件告知）
const portFile = path.join(udd, 'DevToolsActivePort');
let port = 0;
for (let i = 0; i < 150 && !port; i++) {
  if (fs.existsSync(portFile)) {
    const txt = fs.readFileSync(portFile, 'utf8').trim().split(/\r?\n/);
    port = parseInt(txt[0], 10) || 0;
  }
  if (!port) await sleep(100);
}
if (!port) {
  console.log(JSON.stringify({ fatal: 'CDP 端口未就绪', stderr: browserErr.slice(0, 800) }));
  cleanup(1);
}

// 连 page target
let targets = [];
for (let i = 0; i < 50; i++) {
  try {
    targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    if (targets.some((t) => t.type === 'page')) break;
  } catch { /* 浏览器还没起稳 */ }
  await sleep(100);
}
const page = targets.find((t) => t.type === 'page');
if (!page) {
  console.log(JSON.stringify({ fatal: '没有 page target', targets: targets.map((t) => t.type) }));
  cleanup(1);
}

const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map();
let seq = 0;
ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  if (msg.method === 'Runtime.exceptionThrown') {
    const d = msg.params?.exceptionDetails;
    consoleErrors.push(String(d?.exception?.description || d?.text || 'exception').slice(0, 300));
  }
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params?.type === 'error') {
    consoleErrors.push(msg.params.args.map((a) => a.value ?? a.description ?? '').join(' ').slice(0, 300));
  }
});
await new Promise((res, rej) => {
  ws.addEventListener('open', res);
  ws.addEventListener('error', () => rej(new Error('ws error')));
});

const send = (method, params = {}) => new Promise((res) => {
  const i = ++seq;
  pending.set(i, res);
  ws.send(JSON.stringify({ id: i, method, params }));
});

async function evalJS(expression, awaitPromise = false) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise });
  const ex = r.result?.exceptionDetails;
  if (ex) throw new Error(String(ex.exception?.description || ex.text).slice(0, 400));
  return r.result?.result?.value;
}

await send('Runtime.enable');
await send('Page.enable');

// ── 打开真页面 ──────────────────────────────────────────────────────
await send('Page.navigate', { url: BASE + '/' });
for (let i = 0; i < 200; i++) {
  const ready = await evalJS('document.readyState === "complete" && typeof window.updateScanSaveBtn === "function" && !!document.getElementById("scan-save-btn")').catch(() => false);
  if (ready) break;
  await sleep(100);
}

const boot = await evalJS(`(() => ({
  ready: document.readyState,
  hasFn: typeof window.updateScanSaveBtn === 'function',
  hasSaveFn: typeof window.saveScanToZxg === 'function',
  hasBtn: !!document.getElementById('scan-save-btn'),
  hasBody: !!document.getElementById('scan-body'),
}))()`);
record('T0', boot.hasFn && boot.hasSaveFn && boot.hasBtn && boot.hasBody,
  `真 app.js 执行后：ready=${boot.ready} updateScanSaveBtn=${boot.hasFn} saveScanToZxg=${boot.hasSaveFn} btn=${boot.hasBtn} #scan-body=${boot.hasBody}`);
if (!(boot.hasFn && boot.hasBtn && boot.hasBody)) {
  console.log(JSON.stringify({ fatal: '页面未完成初始化', boot, consoleErrors }));
  ws.close(); cleanup(1);
}

// 页面内探针：只读状态 + 真 DOM 造行，不改 app.js 任何逻辑
await evalJS(`window.__probe = {
  state() {
    var b = document.getElementById('scan-save-btn');
    return {
      text: b.textContent,
      norm: b.textContent.replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim(),
      disabled: b.disabled,
      innerHTML: b.innerHTML,
      spans: b.querySelectorAll('span').length,
      title: b.title,
      cursor: b.style.cursor,
      isDefaultHandler: b.onclick === window.saveScanToZxg,
      hasHandler: typeof b.onclick === 'function',
      cbTotal: document.querySelectorAll('#scan-body input[type=checkbox]').length,
      cbChecked: document.querySelectorAll('#scan-body input[type=checkbox]:checked').length,
    };
  },
  rows(values) {
    var body = document.getElementById('scan-body');
    body.innerHTML = values.map(function (v) {
      return '<div class="scan-row"><span class="scan-col-chk">' +
        '<input type="checkbox" value="' + v + '" onchange="updateScanSaveBtn()" />' +
        '</span><span>' + v + '</span></div>';
    }).join('');
    return document.querySelectorAll('#scan-body input[type=checkbox]').length;
  },
  tick(n) {
    var boxes = document.querySelectorAll('#scan-body input[type=checkbox]');
    for (var i = 0; i < n && i < boxes.length; i++) boxes[i].checked = true;
    window.updateScanSaveBtn();
    return boxes.length;
  },
  click() { document.getElementById('scan-save-btn').click(); },
}; 1`);

// 等按钮真正回到「可再保存」态：文案是裸标题 + onclick 已交回 saveScanToZxg。
// 只看文案不够——结果展示期 onclick 被替换成空函数，文案却仍是上一次结果。
async function waitIdle(ms = 4000) {
  const t0 = Date.now();
  let st = null;
  while (Date.now() - t0 < ms) {
    st = await evalJS('window.__probe.state()');
    if (st.isDefaultHandler && /^保存到自选(\(\d+\))?$/.test(st.text)) return st;
    await sleep(80);
  }
  return st || await evalJS('window.__probe.state()');
}

// ── T1 初始态：无复选框 ⇒ disabled ──────────────────────────────────
let s = await evalJS('window.__probe.state()');
record('T1', s.disabled === true && s.text === '保存到自选',
  `无行时 disabled=${s.disabled} text=${JSON.stringify(s.text)}`);

// ── T2 勾选 2 只 ⇒ 文案带计数、可点 ─────────────────────────────────
await evalJS('window.__probe.rows(["sz002523","SLOW001"])');
await evalJS('window.__probe.tick(2)');
s = await evalJS('window.__probe.state()');
record('T2', s.disabled === false && s.text === '保存到自选(2)',
  `勾 2/2 只：disabled=${s.disabled} text=${JSON.stringify(s.text)}`);

// ── T3 点击保存 ⇒ 进入取消态，且**不置 disabled**（置了就点不动取消）──
await evalJS('window.__probe.click()');
s = await evalJS('window.__probe.state()');
record('T3', s.text === '保存中...（点此取消）' && s.disabled === false && s.cursor === 'pointer',
  `保存中文案=${JSON.stringify(s.text)} disabled=${s.disabled} cursor=${JSON.stringify(s.cursor)}` +
  ` 复选框=${s.cbChecked}/${s.cbTotal}`);

// ── T9 保存中 onchange 不得改写文案（_scanSaving 守卫）───────────────
// 桩后端让这两个代码慢 5.2s 返回，才有稳定的「保存中」窗口可断言。
await sleep(250);
await evalJS('window.updateScanSaveBtn()');
const guard = await evalJS('window.__probe.state()');
record('T9', guard.text === '保存中...（点此取消）',
  `保存中调用 updateScanSaveBtn() 后文案=${JSON.stringify(guard.text)}`);

// ── T7 真 4000ms 定时器：2.0s 不该出现登录提示，4.4s 必须出现 ────────
await sleep(1750);                       // 累计 ≈2.0s
const at2s = await evalJS('window.__probe.state()');
await sleep(2400);                       // 累计 ≈4.4s > 4000ms
const at44 = await evalJS('window.__probe.state()');
record('T7', !at2s.text.includes('可能需要登录') && at44.text.includes('同花顺可能需要登录'),
  `2.0s=${JSON.stringify(at2s.text)}；4.4s=${JSON.stringify(at44.text)}`);

// ── T4 慢响应返回 ⇒ 真 innerHTML 渲染成 3 组 6 个 span ──────────────
let done = null;
for (let i = 0; i < 80; i++) {
  const st = await evalJS('window.__probe.state()');
  if (st.spans > 0) { done = st; break; }
  await sleep(100);
}
record('T4', done && done.spans === 6 && done.norm === '通达信：2 只 同花顺：2 只',
  done ? `spans=${done.spans} textContent(norm)=${JSON.stringify(done.norm)} disabled=${done.disabled}`
       : '慢响应后 8s 仍未见渲染结果（spans=0）');

// ── T10 结果展示期间点击无效（onclick 已被换成 preventDefault）──────
const beforeClick = await evalJS('window.__probe.state()');
await evalJS('window.__probe.click()');
await sleep(150);
const afterClick = await evalJS('window.__probe.state()');
record('T10', beforeClick.norm === afterClick.norm && afterClick.title === beforeClick.title,
  `点击前后文案一致=${beforeClick.norm === afterClick.norm}`);

// ── T5 finish(2000) 后复位成可再保存 ────────────────────────────────
const idle5 = await waitIdle(3500);
record('T5', idle5.text === '保存到自选(2)' && idle5.disabled === false && idle5.isDefaultHandler,
  `复位后 text=${JSON.stringify(idle5.text)} disabled=${idle5.disabled} onclick 已交回=${idle5.isDefaultHandler}`);

// ── T6 取消：300ms 后点取消 ⇒ 只发 1 次请求，1.2s 后复位 ────────────
await evalJS('window.__probe.rows(["sz002523","SLOW001"])');
await evalJS('window.__probe.tick(2)');
const reqBefore = (await (await fetch(BASE + '/__requests')).json()).length;
await evalJS('window.__probe.click()');
await sleep(300);
await evalJS('window.__probe.click()');
await sleep(150);
s = await evalJS('window.__probe.state()');
const cancelOk = s.text === '已取消保存' && s.disabled === true && (s.title || '').includes('已取消');
const idle6 = await waitIdle(3000);
const reqAfter = (await (await fetch(BASE + '/__requests')).json()).length;
record('T6', cancelOk && idle6.text === '保存到自选(2)' && idle6.isDefaultHandler && reqAfter - reqBefore === 1,
  `取消态 text=${JSON.stringify(s.text)} disabled=${s.disabled}；复位 text=${JSON.stringify(idle6.text)}；` +
  `新增请求 ${reqAfter - reqBefore} 次（取消不该重发）`);
await sleep(3200);   // 等被取消的那次慢请求彻底落地，避免污染 T8

// ── T8 失败分支 ⇒ 「保存失败」+ title 带原因，1.5s 后复位 ────────────
await evalJS('window.__probe.rows(["FAILME01"])');
await evalJS('window.__probe.tick(1)');
await evalJS('window.__probe.click()');
let fail = null;
for (let i = 0; i < 50; i++) {
  const st = await evalJS('window.__probe.state()');
  if (st.text === '保存失败') { fail = st; break; }
  await sleep(100);
}
const idle8 = await waitIdle(3000);
record('T8', fail && fail.title.length > 0 && fail.disabled === false
              && idle8.text === '保存到自选(1)' && idle8.isDefaultHandler,
  fail ? `失败态 title=${JSON.stringify(fail.title.slice(0, 80))}；复位 text=${JSON.stringify(idle8.text)}`
       : '等 5s 仍未出现「保存失败」');

// ── T11 全程无 JS 异常（真浏览器才暴露 ReferenceError / 类型错误）────
record('T11', consoleErrors.length === 0,
  consoleErrors.length ? consoleErrors.slice(0, 3).join(' | ') : '无 exceptionThrown / console.error');

ws.close();
console.log(JSON.stringify({ checks, consoleErrors }));
cleanup(0);

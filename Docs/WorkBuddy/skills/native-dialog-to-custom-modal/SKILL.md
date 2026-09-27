---
name: native-dialog-to-custom-modal
description: 把原生的 `alert()` / `confirm()` / `prompt()` 换成**自实现的可关闭模态框**（alert / confirm 两形态共用一条队列），并做"同步阻塞 → 非阻塞"的调用点复核与双轨验证。当用户说「点弹窗之外的区域也能关」「点框外＝点确定」「确认框点框外到底该是取消还是确定」「弹窗挡着操作」「弹窗只能点确定太烦」「alert 太丑/不能复制文字」「弹窗挡不住页面」时使用。产出：模态组件（HTML/CSS/JS）+ 全部调用点替换 + 阻塞性语义复核表 + 双轨验证证据（node DOM 桩 逻辑矩阵 + 真浏览器点击/计时）。
agent_created: true
---

# 原生对话框 → 自定义模态框

## 0. 第一条铁律：native `alert()` **做不到**"点框外关闭"

原生 `alert()`/`confirm()` 是浏览器自己画的**系统级**对话框：页面**拿不到它的 DOM**，
没有任何 API 能监听它的关闭或外部点击。所以需求「点框外＝点确定」**只有一条路**：
**自己实现模态框**，然后把调用点全部换过去。**加监听、改 CSS、包一层 div 都是无效方案** ——
遇到这类需求别在原生框上想办法，直接进入替换流程。

先数一遍调用点（决定工作量与回归面）：

```bash
grep -nE '(?<![\w.$])(alert|confirm|prompt)\s*\(' <前端文件> | grep -v '^\s*//'
```
注释里出现的 `alert()` 字样也要一并改（否则全文替换会把注释改成 `showXxx()` 的怪句）。

---

## 1. 组件实现契约（这四条缺一条就会被用户抓）

| 契约 | 为什么 |
|---|---|
| **出口至少三个、语义等价**：点「确定」按钮 / 点**遮罩**（框之外区域）/ 按 <kbd>Esc</kbd> <kbd>Enter</kbd> | 「点框外＝点确定」是用户原话；Esc/Enter 是模态的通用预期，Enter 与原生 alert（默认焦点在确定）一致 |
| **点框内不关** | 否则误触关闭；遮罩点击判据用 `e.target === overlay`（不是 `closest`） |
| **一次只弹一个 + 队列** | 原生 alert 是**串行排队**的；直接 `appendChild` 会几层叠在一起分不清。用队列复刻同一语义 |
| **关闭幂等** | 三条出口可能同时到达（例如 Enter 既触发 keydown 又触发按钮 click），`closed` 标志保证只收尾一次 |

返回值用 **Promise，在关闭那一刻 resolve** —— 这是后面「需要人确认才继续」的调用方唯一的挂载点。

CSS 三件套（少一条就会在真实场景翻车）：

```css
.mydlg      { position:fixed; inset:0; z-index:<比既有弹层+toast 都高>; display:none; }
.mydlg.show { display:flex; align-items:center; justify-content:center; }
.mydlg-box  { max-height:80vh; display:flex; flex-direction:column; }   /* 长文不撑破窗口 */
.mydlg-msg  { flex:1 1 auto; min-height:0; overflow-y:auto;
              white-space:pre-wrap; word-break:break-word; user-select:text; }
```

- `min-height:0` **不能省**：flex 子项默认 `min-height:auto`，不加这条 `overflow-y:auto` 不生效（内容照样把框撑长）。
- `user-select:text`：告警/日志类正文要能选中复制出去查。
- 遮罩要**真的盖住整屏**（`inset:0`），它同时承担"拦住页面点击"的模态职责。

---

## 1.5 alert 与 confirm 两种形态：出口语义怎么定

同一个组件支撑两种形态最省事，但**必须共用同一条队列与同一个 `_alertShowing`** ——
各起一套队列会让"一次只弹一个"失效（alert 和 confirm 会同时挂在屏上）。

| 形态 | 按钮 | 点「确定」/ <kbd>Enter</kbd> | 点「取消」/ 点遮罩 / <kbd>Esc</kbd> | Promise 值 |
|---|---|---|---|---|
| alert（提示） | 只有确定 | 关闭 | 关闭（没有"取消"可言） | 恒 `true` |
| confirm（问答） | 确定 + 取消 | **执行** | **不执行** | `true` / `false` |

规则可以压成一句话：**点遮罩与按 <kbd>Esc</kbd> 同义**（照抄浏览器原生行为）——
alert 关掉即"确定"，confirm 关掉即"取消"。这样只有一条规则要记。

**两条必须照抄既有代码的地方**（别按平台惯例自创）：

1. **点框外的语义** —— 先 grep 仓里既有的问答式弹窗怎么写的。chan.py 仓 `index.html` 的两个
   问答弹窗（文字标注 / 股票扫描）都写着 `onclick="if(event.target===this)XxxCancel()"`：
   **这就是"点框外＝取消"的可执行证据**，也是回答用户"到底哪个功能是点框外＝取消"时要引用的东西
   （别凭印象答，他记得的可能只是你新做的提示框）。
2. **按钮顺序** —— 同仓既有弹窗是「**确定在左、取消在右**」。我第一版按 Web/macOS 惯例写成
   「取消在左」，与仓内既有约定相反，白跑一轮验证。判据永远是**同仓既有文件的多数派**，
   不是通用惯例；先在按钮串里对齐，再验证。

实现细节：`job.kind` 决定按钮个数，**每次弹框重建 DOM**（alert 一个按钮、confirm 两个 ——
复用同一个节点会让上次的按钮残留）。按钮定位用 `data-act="ok"|"cancel"`，**不要用 `.primary`**
（那是样式 class，样式一改断言就失联）。

---

## 2. 最容易漏的一步：**非阻塞化会改变调用点语义**

原生 `alert()` 是**同步阻塞**的：它后面的语句要等人点掉才执行。换成 Promise 模态后，
后面的语句**立刻**执行 —— 必须逐个调用点复核，判断有没有人依赖那个"等一下"。

2026-09-22 在 chan.py 实例上 23 个调用点里只挖出 **1 处**真依赖：

```js
alert('自动下单需要人工介入！…');   // 阻塞 → 人点确定之后才往下走
ackAutoOrderAlerts(maxTs);           // 把水位写回后端（语义＝「确认＝人已看到」）
```
换成模态后 ack **改挂到关闭之后**，并加一个"暂缓水位"标志 —— 否则每 5 秒一轮询就把后端队列清了、人还没看：

```js
autoOrderAlertAckHold = Math.max(autoOrderAlertAckHold, maxTs);
showAlert(msg).then(ackIfAlertsSeen);     // 关掉才 ack
return;                                   // 本轮不 ack
...
function ackIfAlertsSeen() {
    if (_alertShowing || _alertQueue.length) return;   // 还有框排队 → 继续等
    const ts = autoOrderAlertAckHold; autoOrderAlertAckHold = 0;
    ackAutoOrderAlerts(ts);
}
```
**注意**：暂缓标志必须是**模块级**的。用局部变量不行 —— 轮询会让 `fresh` 变空、下一轮直接走到 ack 分支，
弹框还没关就把队列清了（实测踩过）。

复核清单（逐个 `alert(` 调用点过一遍）：
1. 后面有没有"必须等人看完"的副作用（回执/ack/状态推进/后续请求）？
2. 是否在 `.catch()` 里、后面还有 `.finally()`（非阻塞化会让 finally 提前跑）？
3. 是否在 `alert(...); return;` 形态（安全 —— return 照旧立即执行）？
4. 是否在循环里（会不会一次排很多条）？

---

## 3. 双轨验证（都要做，各覆盖一半）

### 3.1 node + 最小 DOM 桩：逻辑矩阵（快、断言密）
**从真实源文件抽代码段**，不要手抄一份 —— 抽出来才是"测的就是要交付的东西"：

```js
// 按 [COMPONENT] 横幅切片；或按"缩进 8 空格的独立 }"截顶层函数
function fnText(name) {
  const s = lineNo("function " + name + "(") - 1;
  for (let i = s + 1; i < LINES.length; i++)
    if (LINES[i] === "        }") return LINES.slice(s, i + 1).join("\n");
}
```
桩只需实现：`getElementById` / `createElement` / `appendChild` / `classList` / `querySelector` /
`addEventListener` + 一个能解析组件自己那段 `innerHTML` 的**极简标签解析器**（正则扫 `<tag class="…">`）。
断言矩阵：点框外关 / 点框内不关 / 三出口 / 队列 / 幂等 / 非阻塞 / 回执时序。

### 3.2 真浏览器：CSS 与层叠上下文（桩测不出来）
用 `frontend-pixel-verify` §9 的 playwright 流程。**必须量/点的四件事**：
- `getComputedStyle` 的 `position/display/z-index` + 遮罩 `getBoundingClientRect()` **是否等于视口**；
- `document.elementFromPoint(5,5)` **是不是遮罩本体** —— 这一条才真正证明"框外区域被遮罩占住"
  （z-index 被别的弹层压住 / 祖先有 `transform` 导致 `fixed` 失效，都只会在这里露出来）；
- **真鼠标** `page.mouse.click(5,5)` 关掉、点框内不关、**底层元素收到点击 0 次**；
- 时长类需求按**绝对时刻**采样（如 0.4/1.2/4.6/6.2s），取"前一帧仍在、后一帧已消失"两点定区间。

---

## 4. 坑

- **`confirm()` 的收编：先问，再定语义**。一问一答的弹窗，"点框外"应当映射成**取消**，
  映射成确定＝误操作（删除确认类尤甚）。要不要收进来先问用户；收进来的话，点框外 / Esc 的语义
  照 §1.5 抄仓里既有问答弹窗，**别顺手一起换成"＝确定"** —— 那样等于把危险默认值调得更危险。
- **验证脚本自身最容易假死**：上一个用例的框忘了关 → 下一个 `showXxx()` 只是**排队**、不进屏 →
  那条 `await p` 永远等不到 → node 事件循环空转、进程**静默退出**（连 `FATAL` 都不打印）。
  每个用例结束都要 drain（循环点框外直到 `!shown`）；看到"日志停在某段、没有总结行"先怀疑这个。
- **断言集合里要有一条「与既有约定一致」**：把既有弹窗那两行 `onclick` 与按钮顺序一起做成断言，
  否则以后有人改了既有约定，这个新组件就成了孤儿，而且没人会发现。
- **`innerHTML` 里的按钮别自己造样式**：复用仓里既有弹窗的按钮 class，避免两套观感漂移。
- **新增组件区块/文件会动到结构护栏**：chan.py 仓里 `Test/test_phase6_guards.py` 会数
  `[COMPONENT] X —— …` 区块、并断言"**区块外不得有游离的顶层函数**"（新函数必须落在某个区块之内），
  且 `window.*` 绑定面是**冻结基线**（新组件别往 window 上挂）。改完必跑 `node --check` + 该护栏。
- **别在 UI 文案上裸写"引擎"**（chan.py 术语铁律）：**交易引擎** = `Trading/`；**缠论引擎** = 其余部分。
  新增文案/注释/日志一律用全称；改完跑 `Trading/Test/test_p26_terminology_guard.py`（它会扫 `Frontend/*.js`）。

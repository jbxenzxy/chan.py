---
name: frontend-pixel-verify
description: 前端**像素级 / 时序级**验收与量测：用真实无头 Chromium 量出元素的真实盒模型（不是读 CSS 猜），把候选方案**真实渲染**成图给用户挑，出改前/改后同尺度对比图，并在多个视口宽度下做回归。当用户说「改为跟 XX 一样大 / 一样的宽度和高度」「对齐一下」「间距不对」「按钮/徽标尺寸不一致」「这里看着别扭」这类**外观尺寸**诉求，或「弹窗时间太短没看清就消失」「同时来好几条只看到一条」「一闪而过」「点不掉」这类**时序/交互**诉求，或改完 CSS 需要证明效果时使用。也用于**交付型 SVG / 内联图的文本边界**检查（防超 viewBox 被静默裁切，见 §8）与**前端时序缺陷实测**（playwright + fetch 打桩驱动真实页面，见 §9）。产出：像素级量测报告 + 方案对比图 + 改前改后对比图 + 多宽度回归表 + 时序实测表 + 可回退的样式补丁。
agent_created: true
---

# 前端像素级验收

外观类需求（"一样大""对齐""间距"）**不能靠读 CSS 推断** —— 真实尺寸由字体度量、
flex 收缩、`box-sizing`、过渡状态共同决定，逐项算容易差 1~2px 而看不出来。
**先量，再改，改完再用同一个量测脚本证明。**

## 何时用

- 用户提出**尺寸/对齐**诉求：「改为跟 XX 一样大」「一样的宽度和高度」「对齐」「差一点」「间距不对」
- 改完 CSS 要交差，需要**证据**而不是"看着行"
- 一个尺寸需求**有多种合理解释**，且改法差别很大（→ 先渲染候选给用户挑，别猜）

**不要用**：纯逻辑/数据类改动；纯文案调整；对面元素根本不在同一视觉行的情况。

---

## 0. 铁律

1. **先量真实尺寸，再动手**。读 CSS 得出的数字**不算**证据。
2. **"一样大"有歧义时，先渲染候选方案 → 截图 → 让用户选**。别猜 —— 猜错的代价是一整轮返工。
   用户说"一样大"时至少要问清：是**整体**一样大、还是**里面某个子元素**一样大？
3. **改前 / 改后必须用同一尺度、同一脚本、同一窗口宽**各出一张图，叠参考线标出目标尺寸。
4. **多宽度回归**：至少测 1 个宽视口（正常）+ 1~2 个窄视口（布局会被挤压时）。
   只测一个宽度，很容易在窄窗口引入"文字被压出框"这类新问题。
5. **尺寸不变量优先用结构保证，而不是靠断言盯着**。例：两个控件必须同高 →
   定义一个共享的 CSS 变量（`--hdr-chip-h`），双方都引用它，改一处两者一起变，
   **结构上不可能漂移**。这样也不必新增测试文件。
6. **别用 `height` 钉死会随内容长高的盒子**。窗口变窄 → 文字折行 → 盒子本来会长高；
   钉死会**把文字压出框**（新增 bug）。要"至少这么高"就用 `min-height`。

## 1. 工具链：无头 Chromium

**先探测再决定**（2026-09-17 实测更新）：本机**其实装了 playwright 且浏览器已下载**
（`~/AppData/Local/ms-playwright/chromium-*`，Python 包在托管 Python 与项目 venv 里都有）——
那就直接用 playwright，能拿到 `--dump-dom` 拿不到的东西：**点击、计时序列、逐帧采样**。
探测一行：

```bash
python -c "import playwright; print(playwright.__file__)"; ls ~/AppData/Local/ms-playwright
```

**没装也能跑**：**Windows 自带 Edge，macOS 自带 Chrome**，headless 就够用：

```bash
EDGE="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"   # 或 chrome.exe
"$EDGE" --headless=new --disable-gpu --no-first-run --no-default-browser-check \
        --hide-scrollbars --user-data-dir=<临时目录> --virtual-time-budget=4000 \
        --window-size=1920,140 \
        --dump-dom file:///<绝对路径>/index.html > dump.html
```

要量数据就 `--dump-dom`（把量测结果写进 DOM 再正则取出来）；
要图就 `--force-device-scale-factor=4 --screenshot=<png>`（4 倍缩放，肉眼看得到 1px）。

## 2. 用 `scripts/probe.py` 量测（推荐入口）

```bash
PY=<你的 Python>            # 需 PIL（出图用）
WINW=1920 "$PY" scripts/probe.py <前端目录（含 index.html+app.css）> <输出目录> <标签>
```

它做三件事：

1. 复制 `index.html`，把 `<script src="app.js...">` **换成量测脚本**
   （真实 app.js 会去连后端并报错，没必要引入）；
2. `--dump-dom` 取出每个元素的 `getBoundingClientRect()` + computed style，
   以及 **`::before` 伪元素**在**开/关两种状态**下的几何；
3. 以 4 倍缩放截图，末行打印**对齐判定**（如 `徽标高 19  控件高 19  →  ✓ 同高`）。

`probe.py` 里已经内置了下节全部坑点的规避，直接跑即可。

## 3. 用 `scripts/variants.py` 渲染候选方案

当"一样大"有歧义：**在同一张图里并排渲染 3 个候选**（复用项目真实 CSS + 真实 DOM 片段，
所以像素与线上一致），再让用户指一个。流程：

```bash
PY <你的 Python> scripts/variants.py <前端目录> <输出目录>
# → variants.png（含"参照行"= 目标元素本身 + 各方案行），再用 PIL 裁掉空白
```

关键做法：**先建一个"参照行"单独放目标元素并量它**，把这个尺寸当作"目标尺寸"
喂给各方案 —— 这样"等宽等高"是**精确**的，而不是手写一个近似值。

## 4. 用 `scripts/compose_compare.py` 出改前/改后对比图

同一窗口宽下各截一张，裁同一区域，**叠参考线**（青 = 目标元素上下沿，橙 = 被改元素下沿），
堆成上下两行 + 文字标注实测数字。参考线让 0.5~1px 的差异**肉眼可见**。

```bash
PY scripts/compose_compare.py <probe 输出根目录> <输出 png>
```

出图注意：**PIL 默认字体没有中文，也没有 ✓/✗ 字形**（会渲染成方框）。
用系统字体（`C:\Windows\Fonts\msyh.ttc`），标注里别用 ✓/✗，改写成"已对齐 / 未对齐"。

## 5. 六个必踩坑（都实测踩过）

1. **`--window-size` 太窄会挤压布局，量出来的"参照尺寸"全错。**
   实测：1400 宽时徽标被挤成折行方块，量到 `35.75 × 49`（真实是 `47.69 × 19`）。
   修法二选一：① 窗口给够宽（1920）；② **把真实容器（如 `.header`）`display:none` 藏掉，
   把目标元素克隆到一条没有宽度约束的自建行里量**（更稳，推荐）。
2. **参考元素不要放在会被挤压的容器里量**。被测对象若和右侧一堆按钮共享一行，
   窗口变窄时它会被 flex 收缩 —— 必须量"自由状态"下的尺寸。
3. **量 `::before` 的 `transform` 必须临时关掉 `transition`**，
   否则同步 `getComputedStyle` 读到的是过渡**起点**（永远 `matrix(1,0,0,1,0,0)`）。
   注入 `.sel, .sel::before { transition: none !important; }` 再读。
4. **变量遮蔽**：`for tag, k in (...)` 会覆盖函数参数 `tag`，
   把产物写成 `measure_开.json` 这种怪名字。自查：产物名对不上就查同名局部变量。
5. **正则别锚在注入脚本自身的字面量上**。若注入的 `<script>` 里含 `PROBE_BEGIN`，
   在 `--dump-dom` 输出里它会**先**被匹配到（拿到的是 JS 源码而不是结果）。
   锚到结果容器上，如 `<pre id="__PROBE__">(.*?)</pre>`。
6. **`<style>` 块放在 `<body>` 里可能整块不生效**（实测：行布局退化成块级、
   克隆体被拉满整行宽度）。关键布局**用内联样式**设置，别赌 `<style>`。
   另：页面里若有 `height: calc(100vh - Npx)` 的大元素，追加的行会被顶出首屏 →
   截图全黑；把对比容器设成 `position: fixed; top: 0`。

## 6. 交付格式

沿用项目里既有的"最小可替换单元"约定：

```
<date>_<topic>.zip
├── <改动的文件>                     ← 完整文件，覆盖即用
├── _baseline/<文件>.before_rN        ← 改前原件，覆盖回去即回退
├── <Topic>_roundN.diff              ← unified diff（忽略 CRLF）
├── README.md                        ← 改了什么/为什么/怎么验证/怎么回退
├── evidence/before_after.png        ← 改前改后对比（带参考线）
├── evidence/options.png             ← 当初给用户挑的候选方案图
├── evidence/measure_*.json          ← 量测原始数据
└── _verify/probe.py 等              ← 量测脚本（用户可自证，不进项目）
```

交付说明里必须写清：**实测数字表（改前 vs 改后）** + **多宽度回归表** + **回退方法**。
纯外观改动**不必**新增测试文件 —— 用共享变量把不变量结构化即可，并**明确说明这一点**
（否则用户以为你漏了护栏）。

## 7. 收尾必做

- **洁净室复验**：把交付 zip **解压到用户项目的副本**上，再跑一次 `probe.py`，
  确认量测结果达标、且解压后的文件与验证树**逐字节一致**。
- 改动若触及功能逻辑（不只是样式），回归跑该项目的既有测试。

## 8. 场景扩展：交付型 SVG / 内联图的**文本边界**（防静默裁切）

不只 UI 元素要量 —— **交付给用户的 `.svg` 或内联 `<svg viewBox="...">` 文档**同样要量。
它的失败模式更阴险：**超出 viewBox 的文字被静默裁掉**（根 `svg` 的 `overflow` 初始值即 `hidden`），
不报错、不换行、不加省略号 —— 你以为发出去了，实际少了半句话。

### 8.1 判据：`getBBox()` 直接和 viewBox 比，**绝不靠数汉字估算**

`getBBox()` 返回 **SVG 用户坐标**，与 `width="100%"` 的缩放无关 ⇒ 可直接比：

```
右侧越界 ⇔ getBBox().x + width  > viewBox 宽
底部越界 ⇔ getBBox().y + height > viewBox 的 y + 高
```

实测教训（2026-09-16 chan.py「账户三态」图）：一行判据文案，我按"全角 11px / 半角 6px"
估算右端 ≈748px，**真实渲染 771.1px** —— 差 23px，且字体回退会让误差方向不定。
**估算只能用来粗排，判据必须实测。**

### 8.2 零依赖量测片段（Edge headless + 包装 HTML）

```python
WRAP = """<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0"><div style="width:680px">__SVG__</div>
<pre id="out" style="display:none">PENDING</pre>
<script>
window.addEventListener('load', function () {
  var rows = [];
  document.querySelectorAll('svg text').forEach(function (t) {
    var b = t.getBBox();
    rows.push({y:+b.y.toFixed(1), right:+(b.x+b.width).toFixed(1),
               bottom:+(b.y+b.height).toFixed(1), txt:t.textContent});
  });
  document.getElementById('out').textContent = '@@' + JSON.stringify(rows) + '@@';
});
</script></body></html>"""
```

用 `--dump-dom` 取回 DOM，正则抠 `@@...@@` 即得结构化数据。
（`<pre>` 必须 `display:none`，否则它会出现在截图里。）

### 8.3 截图对照要 **1:1 + 同尺度**

```bash
"$EDGE" --headless=new --window-size=680,600 --force-device-scale-factor=1.5 \
        --screenshot=after.png file:///.../wrap_after.html
```

- **窗口宽取成恰好等于 viewBox 宽** ⇒ 1:1，右边裁没裁一眼可见；
- 改前 / 改后各截一张，同窗口尺寸 ⇒ 可直接叠看；
- 截图能抓到你量不到的**版式事故**（行距挤压、文字压线）——
  **量测 + 截图两条腿都要走**，别只交数字。

### 8.4 改文案 = 三处连带，一个都不能少

1. **行数变 → 调 `viewBox` 高度**：不加高，新加的行直接落在画布外被裁，而且**静默**；
2. **每行 `<text>` 的 `y` 要整体重排**：只在中间插一行就交差，下面的行会重叠；
3. **同文档的 `<desc>` / 图内注释一起同步**，否则图纸自相矛盾
   （实例：图内边标注已改叫"今日仓 / 跨日仓"，`<desc>` 却还写着"按最新一笔仓单的建仓交易日"）。

### 8.5 交付行尾要匹配**用户工作区**，不是仓库

交付前先看用户手上那份文件的真实行尾：仓库基线可能是 LF，而用户 Windows 工作区是 CRLF
（实测同一文件：仓库 6267B/LF、用户本地 6322B/CRLF，**差 55 = 55 个 CRLF，内容零差异**）。
给 CRLF ⇒ 覆盖后 `git diff` 只显示你真正改的几行；给 LF ⇒ 整文件翻天，用户没法 review。

**而且要以"用户手上那份"为基线，不是以"我上一轮交付的版本"为基线**：实测
`app.js` 用户现状 445268B 与我留的基线副本 445260B 差 8B —— 用户已经覆盖了我上一批交付，
我又拿旧副本当"改前"，对比表就掺了假。**动手前先 diff 一次用户现状 vs 基线副本**，有差异就重建基线。

---

## 9. 场景扩展：**时序/交互类**前端缺陷（"一闪而过""只有最后一条生效"）

用户说「弹窗时间太短，没看清就消失」「同时来好几条只看到一条」时，**读代码够用但对不上数**：
必须真跑、真计时、真点。playwright 是唯一能同时做这三件事的工具。

### 9.1 IIFE 闭包里的函数，`page.evaluate` 调不到 —— 别想着直接调

零构建原生 JS 常把一切裹在一个大 IIFE 里，`showToast` / `pollXxx` 这些**在全局作用域取不到**
（`typeof showToast === 'undefined'`）。两个正确姿势：

1. **走用户那条真实入口**（首选）：脚本往 `window` 上挂过的东西能直接调
   （本仓实例：`window.onAutoOrderToggle(checkbox)` 就是开关的 `onchange` 入口）——
   这一条路径把「点开关 → 请求 → 轮询 → 告警 → 弹提示」整条链跑通，比单独调 `showToast` 更有说服力。
   **要触发"弹窗/提示"这类分支时，先在本仓找有没有零依赖入口**：实测
   `window.annotationDialogConfirm()`（先把 `annotation-dialog-input` 的 `value` 清空）会直接走到
   `showAlert('请输入标注文字')` 那条分支 —— **不需要任何后端数据**，就能在真 `index.html` + 真 `app.js`
   里把模态框弹出来，用来验证 z-index / 层叠上下文 / 真鼠标点击是否落在遮罩上（2026-09-22 实测）。
2. **打桩 `fetch` 喂数据**，而不是起后端：`context.add_init_script(...)` 里替换 `window.fetch`
   （脚本执行前注入，早于 app.js），按 URL 片段返回罐头 JSON。这样**页面本身是真的**
   （真 index.html / 真 app.js / 真 app.css），只有数据源是假的。
   注意打桩里少写一个常量就会让整个 IIFE 抛错、`fetch` **根本没被替换** ——
   症状是控制台出现 `Fetch API cannot load file:///...`，一看就知道打桩没生效。

### 9.2 环境会在收尾时 SIGTERM 掉带浏览器的进程 → 结果必须**即时落盘**

本机实测：`playwright` 能正常 `launch`（Chromium 151）并跑完断言，但**进程收尾时被 SIGTERM**，
表现是 `rc=1`、**stdout 全丢**（连 traceback 都可能没有）。别把结论打到 stdout：

```python
_LOG_PATH = os.path.join(outdir, "run_%s.log" % label)   # 每步 append + flush
...
if __name__ == "__main__":
    main()
    os._exit(0)     # 硬退出：跳过 playwright 的清理，保证文件都已落盘
```

另外**"改前/改后"两次运行必须写进不同目录**：截图/JSON 同名会互相覆盖（实测把改后的图整个盖掉，
拿到的对比图两边都是同一份）。

**收尾时 `br.close()` 也会挂死**（本机复现多次：主流程断言全部跑完、日志也写完了，但进程不退出，
于是"结果: N passed"那行**永远写不出来** —— 看起来像卡在断言上，其实卡在收尾）。两条一起用：

```python
    log("结果: %d passed, %d failed" % (PASS[0], FAIL[0]))   # ← 统计必须在 close 之前
    def _close_browser():
        try:
            br.close()
        except Exception as e:
            log("[WARN] br.close() 异常（忽略）: %r" % (e,))
    th = threading.Thread(target=_close_browser, daemon=True)
    th.start(); th.join(timeout=5)
    if th.is_alive():
        log("[WARN] br.close() 5s 未返回 → 放弃等待，直接落盘退出")
```

⚠️ playwright 的 sync API **不能在别的线程里用**：守护线程里调 `br.close()` 会抛
`Cannot switch to a different thread`（greenlet 错）。把它当成"放弃等待"的信号即可，
**必须 catch 掉** —— 线程里未捕获的异常会把收尾路径一起带崩。`join(timeout=5)` 之后照常
`os._exit(0)`，**别为了"优雅收尾"去等它**。

对应的清进程姿势（挂起后要手动收）：`ps -W` 输出的**第 1 列是 Cygwin PID、第 4 列才是 WINPID**，
`taskkill /F /PID` 必须用第 4 列；用第 1 列只会得到"没有找到进程"，白折腾好几轮。
在 Git Bash 里还要 `MSYS_NO_PATHCONV=1 taskkill /F /PID <winpid>`（否则 `//F` 会被转义坏）。

### 9.3 采样要按**绝对时刻**排，别用"间隔"堆

先定"打开后 0.4s / 1.2s / 4.6s / 6.2s"这类**目标时刻**，再换算成间隔
（0.4 / +0.8 / +3.4 / +1.6），否则累计漂移，"4.6s 时还在"这句话就不成立。
要在目标时刻画结论线（比如"5 秒后消失"），至少取**前一帧仍在、后一帧已消失**两点：
本仓实测 4.6s 有 3 条、6.2s 归零 ⇒ 时长落在 (4.6, 6.2]，与代码里的 5000ms 一致。

### 9.4 量出来的东西要包含"看不到"的那些

同一个函数往往**既**测"能看到几条"，**也**测"点得掉吗""会不会溢出""上限多少"：
本仓一次 3 条告警的实测里，改前 `pointer-events` 就是 `none`（点不掉、选不中），
而改后是 `auto` —— 这一列比"时长"更能说明原设计的问题。
`scrollWidth > clientWidth + 1` 是判断"文字溢出容器"的稳定判据（`getBoundingClientRect()` 看不出来）。

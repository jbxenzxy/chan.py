# -*- coding: utf-8 -*-
"""
成交额/量「类MACD」显示模式 —— 设置项契约 + 数值对齐 + 真渲染对照
=====================================================================
被守护的功能（2026-09-18 新增）：

  底部指标区是**多槽位**（单窗 2 槽 / 双窗 1 槽；2026-10-09 改造），每槽由标签行
  最左的 chip 点击在 成交额/量 → MACD → RSI(12) 之间循环（BOTTOM_ORDER）。其中
  「成交额（股票）/ 成交量（期货）」槽除柱状图外，可切为「类MACD」——借用传统
  MACD(12,26,9) 算法，把收盘价换成成交额/量，得到黄白线（DIF/DEA）与红绿柱
  （BAR）。该「柱状图 / 类MACD」仍由右上角「显示设置」抽屉决定，默认柱状图
  （与既有行为一致）。

本用例四层守护：

  ① 设置项契约：抽屉里有该项、两个单选项值恰为 bar/macd、默认 bar 语义、
     且**不带内联 onchange**（内联处理器必须挂 window.*，而 window API 面
     已冻结，见 test_phase6_guards ③）。
  ② 状态与持久化：`_volDisplayMode` 声明 / localStorage 读写 / AppState
     访问器在位（刷新后仍生效）。
  ③ 数值对齐（真跑）：把 app.js 里 VOL_MACD_CORE 标记区间的**真实代码**
     抽到 node 执行，用真实快照（股票 amount / 期货 vol）与后端
     App/AppUtils.calculate_macd 逐点比对——不是"看起来像 MACD"，是逐值相同。
     附：尾部占位K线（未形成预览bar，量恒为 0）必须**不参与 EMA**（否则末根
     出现假的深坑），与后端 _inherit_macd_for_preview_bar 同口径继承。
  ④ 真渲染对照（无头 Chrome，浏览器不在位时降级 SKIP）：起本地静态服务 +
     路由打桩喂真实快照 → 点槽 0 的 chip 循环到 成交额/量 → 逐项量像素：
        · 柱状图模式：底部无 DIF 白线 / DEA 橙线（≈0），有红/青柱
        · 类MACD模式：白线 + 橙线显著出现，正文变成「MACD(12,26,9)」（与「价格MACD」
          逐字同构）；品种口径由标签行**右端 chip**（成交额 / 成交量）标识，不再另画
        且标签里的 DIF 数值 ≡ Python 侧 calculate_macd 在同一根K线上的结果；
        切回柱状图白线/橙线消失（双向实时）；翻转视图下白线/橙线照常在。
        另：槽位交互（默认双槽 / chip 单击循环 + 落盘 / 双击不切换且不重置视图）
        与 RSI 槽的自适应纵轴（上下档 = 可见窗口 rsi 的 min/max 各留 5%，再夹回
        指标定义域 [0,100]；中间档 "50" 仅在值域含 50 时画）也在本节覆盖；
        几何不变式与 RSI 两份后端实现的数值对齐另有专门用例
        Test/test_bottom_slots.py。

运行：python Test/test_vol_macd_mode.py            # 校验（run_all 组件）
      python Test/test_vol_macd_mode.py --update   # 兼容参数（无冻结基线）
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")
ENTRY_HTML = os.path.join(REPO_ROOT, "Frontend", "app.html")
FRONTEND_DIR = os.path.join(REPO_ROOT, "Frontend")
SNAP_STOCK = os.path.join(TEST_DIR, "snapshots", "stock_d_full.json")
SNAP_FUTURES = os.path.join(TEST_DIR, "snapshots", "futures_15s_full.json")

CORE_BEGIN = "// >>> VOL_MACD_CORE"
CORE_END = "// <<< VOL_MACD_CORE"


# ═══════════════════════════════════════════════════════════════════
# 公共工具
# ═══════════════════════════════════════════════════════════════════
def read(path):
    """读文本并归一换行（本仓是 CRLF 检出，正则/切片一律按 \\n 处理）。"""
    with open(path, encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


def check(failures, cond, desc, detail=""):
    if cond:
        print(f"  [ok] {desc}")
    else:
        failures.append(desc + (f" | {detail}" if detail else ""))
        print(f"  [FAIL] {desc}" + (f" | {detail}" if detail else ""))
    return bool(cond)


def find_node():
    cand = [os.environ.get("NODE_EXE"), shutil.which("node")]
    cand += glob.glob(os.path.expanduser(
        "~/.workbuddy/binaries/node/versions/*/node.exe"))
    cand += [r"C:\Program Files\nodejs\node.exe"]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def find_playwright():
    """返回 (playwright 模块, 失败原因)。不可用时原因非空。"""
    try:
        import playwright.sync_api as _pl  # noqa: F401
        import playwright
        return playwright, ""
    except Exception as e:  # pragma: no cover
        return None, f"playwright 未安装: {type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════════
# ① 设置项契约（app.html）
# ═══════════════════════════════════════════════════════════════════
def test_setting_ui(failures):
    print("\n① 设置项契约（抽屉：成交额/量显示）")
    html = read(ENTRY_HTML)
    group = re.search(r'<div id="vol-display-mode-group"[\s\S]*?</div>', html)
    check(failures, group is not None,
          "抽屉内存在 #vol-display-mode-group 分组")
    if not group:
        return
    body = group.group(0)
    vals = re.findall(r'name="vol-display-mode"\s+value="([^"]+)"', body)
    check(failures, vals == ["bar", "macd"],
          "两个单选项取值恰为 [bar, macd]", f"实际 {vals}")
    check(failures, "柱状图" in body and "类MACD" in body,
          "两个选项文案含「柱状图」「类MACD」")
    check(failures, "onchange" not in body and "onclick" not in body,
          "单选项不带内联事件（window API 面冻结，须走 addEventListener）",
          "内联处理器必须挂 window.*，会顶破 phase6 ③ 冻结基线")
    check(failures, bool(re.search(r'value="bar">\s*\n\s*柱状图（默认）', body)),
          "明确标注柱状图为默认值")
    tip = html[group.end():group.end() + 400]
    check(failures, "借用传统" not in tip and "得到黄白线" not in tip and "双击" not in tip,
          "分组下方不再有长篇说明文案（含义与双击语义不写进抽屉）",
          repr(tip[:80]))
    # 缓存契约（2026-10-09 拍板）：?v= 人工版本号废除，缓存由 FrontAPI no-cache 头接管
    check(failures, "app.js?v=" not in html and 'src="app.js"' in html,
          "app.html 裸引 app.js（?v= 已废除）",
          "仍带 ?v= 版本号或缺 app.js 引用")


# ═══════════════════════════════════════════════════════════════════
# ② 状态与持久化
# ═══════════════════════════════════════════════════════════════════
def test_state_and_persist(failures):
    print("\n② 状态与持久化（默认柱状图 / 刷新后仍生效）")
    js = read(APP_JS)
    check(failures, re.search(r"let _volDisplayMode = 'bar';", js) is not None,
          "声明 _volDisplayMode，默认 'bar'（= 既有柱状图行为，升级不改变观感）")
    check(failures,
          re.search(r"if \(s\.volDisplayMode === 'bar' \|\| s\.volDisplayMode === 'macd'\) "
                    r"_volDisplayMode = s\.volDisplayMode;", js) is not None,
          "loadOverlaySettings 校验取值后回填（非法值不回填）")
    check(failures, re.search(r"^\s+volDisplayMode: _volDisplayMode,$", js, re.M) is not None,
          "saveOverlaySettings 落盘 volDisplayMode")
    check(failures,
          re.search(r"_volDisplayMode: \{ get: function\(\)\{ return _volDisplayMode; \}, "
                    r"set: function\(v\)\{ _volDisplayMode = v; \} \},", js) is not None,
          "AppState 访问层暴露 _volDisplayMode（getter/setter 同源）")
    # 单选项同步 + 监听挂载（幂等）
    check(failures, "function initVolDisplayModeRadio()" in js
          and "function onVolDisplayModeRadioChange(ev)" in js,
          "存在抽屉单选项的同步/回调实现")
    check(failures, "radios[i].addEventListener('change', onVolDisplayModeRadioChange);" in js
          and "getAttribute('data-bound')" in js,
          "监听用 addEventListener 且 data-bound 幂等（重复开抽屉不叠加）")
    check(failures, re.search(r"initCoordSystemRadio\(\);\s*\n\s*initVolDisplayModeRadio\(\);", js)
          is not None,
          "openBspSettings 里随抽屉打开同步选中态")
    # 快捷键之外的入口：默认值必须与"未设置"一致
    check(failures, "volDisplayMode" not in read(ENTRY_HTML).split("vol-display-mode-group")[0],
          "HTML 未预置 checked（选中态由 JS 依 _volDisplayMode 同步，单一事实源）")


# ═══════════════════════════════════════════════════════════════════
# ③ 数值对齐（node 跑 app.js 的真实代码段）
# ═══════════════════════════════════════════════════════════════════
NODE_DRIVER = r"""
const fs = require('fs');
const snapPath = process.argv[2];
const isFutures = process.argv[3] === 'futures';
const snap = JSON.parse(fs.readFileSync(snapPath, 'utf8'));
const klines = snap.klines;

function series(list, fut) {
  const m = calcVolMacdMap(list, fut);
  return list.map(k => { const v = m.get(k); return [v.dif, v.dea, v.macd]; });
}
const out = {};
out.full = series(klines, isFutures);

// 尾部 3 根改成占位预览bar（量/额=0，仅 OHLC 被填入，后端就是这种形态）
const tail = klines.map(k => Object.assign({}, k));
for (let i = tail.length - 3; i < tail.length; i++) { tail[i].vol = 0; tail[i].amount = 0; }
out.withTail = series(tail, isFutures);

// 样本不足 26 根 → 与后端同规则：全 0
out.short = series(klines.slice(0, 25).map(k => Object.assign({}, k)), isFutures);

// 首根必须恒等于首个样本（EMA 以首值为种子，不做 SMA 预热——与后端 ema() 一致）
out.emaSeed = _volMacdEma([5, 7, 9], 12).map(v => Math.round(v * 1e9) / 1e9);
process.stdout.write(JSON.stringify(out));
"""


def test_numeric_alignment(failures):
    print("\n③ 数值对齐：前端类MACD ≡ 后端 calculate_macd（真实快照，逐点）")
    from App.AppUtils import calculate_macd

    js = read(APP_JS)
    begin = js.find(CORE_BEGIN)
    end = js.find(CORE_END)
    if not check(failures, begin >= 0 and end > begin,
                 f"app.js 内存在 {CORE_BEGIN} … {CORE_END} 标记区间（供离线抽代码）"):
        return
    core = js[begin:end]

    node = find_node()
    if not node:
        print("  [SKIP] 未找到 node（离线数值对齐无法执行；静态契约已过）")
        return

    tmp = tempfile.mkdtemp(prefix="vol_macd_")
    drv = os.path.join(tmp, "driver.js")
    with open(drv, "w", encoding="utf-8") as f:
        f.write(core + "\n" + NODE_DRIVER)

    cases = [("股票 amount", SNAP_STOCK, "stock"),
             ("期货 vol", SNAP_FUTURES, "futures")]
    for name, snap_path, kind in cases:
        if not os.path.isfile(snap_path):
            check(failures, False, f"{name}: 快照存在", snap_path)
            continue
        proc = subprocess.run([node, drv, snap_path, kind], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            check(failures, False, f"{name}: node 执行成功",
                  (proc.stderr or "").strip()[:200])
            continue
        got = json.loads(proc.stdout)
        snap = json.load(open(snap_path, encoding="utf-8"))
        klines = snap["klines"]
        vals = [(k["vol"] if kind == "futures" else k["amount"]) for k in klines]
        ref = calculate_macd(vals) if len(vals) >= 26 else None

        full = got["full"]
        if ref is None:
            check(failures, all(abs(x) < 1e-12 for tri in full for x in tri),
                  f"{name}: 样本不足 26 根 → 全 0")
            continue
        worst = 0.0
        for i, tri in enumerate(full):
            for a, b in zip(tri, (ref[i]["dif"], ref[i]["dea"], ref[i]["macd"])):
                worst = max(worst, abs(a - b))
        check(failures, worst < 1e-9,
              f"{name}: {len(full)} 根 dif/dea/macd 逐点 ≡ calculate_macd",
              f"最大偏差 {worst:.3e}")

    # 预览bar：尾部 3 根量=0 → 不参与 EMA，末根继承前一根已算出的结果
    full, tail = got["full"], got["withTail"]
    n = len(full)
    head_ok = all(abs(a - b) < 1e-9
                  for i in range(n - 3) for a, b in zip(full[i], tail[i]))
    inherit_ok = all(abs(tail[i][j] - full[n - 4][j]) < 1e-9
                     for i in range(n - 3, n) for j in range(3))
    check(failures, head_ok,
          "尾部预览bar（量=0）不影响此前任何一根的结果（EMA 未被 0 拉低）")
    check(failures, inherit_ok,
          "末根（预览bar）继承前一根结果，与后端 _inherit_macd_for_preview_bar 同口径")
    check(failures, all(abs(x) < 1e-12 for tri in got["short"] for x in tri),
          "样本不足 26 根 → 全 0（与后端 _apply_macd_full 同规则）")
    check(failures, abs(got["emaSeed"][0] - 5) < 1e-12,
          "EMA 以首个样本为种子（不做 SMA 预热，与后端 ema() 一致）")
    shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
# ⑤ 渲染分派与画法复用（源码契约）
# ═══════════════════════════════════════════════════════════════════
def test_render_dispatch(failures):
    print("\n⑤ 渲染分派（底部指标区：注册表四钩子 + vol 槽内 柱状图/类MACD 双分派）")
    js = read(APP_JS)
    check(failures, "_volMacdMap = (_hasBottomSlot('vol') && _volDisplayMode === 'macd')\n"
                    "                ? calcVolMacdMap(data.klines, "
                    "!!(data.meta && data.meta.market === 'futures'))" in js,
          "_renderChart 里按「存在 vol 槽 且 模式=macd」算一次类MACD（全序列 EMA 预热）")
    # ⚠ 赋值必须在槽位值域循环之前：vol 槽的 range() 在 macd 模式下会读 _volMacdMap，
    #   放到循环之后会读到上一帧的陈旧 Map（vol 槽 y 值域滞后一帧）。
    _i_map = js.find("_volMacdMap = (_hasBottomSlot('vol')")
    _i_loop = js.find("slotRanges.push(BOTTOM_INDICATORS[_slotAt(si)].range(klines));")
    check(failures, _i_map != -1 and _i_loop != -1 and _i_map < _i_loop,
          "类MACD Map 先于槽位值域循环赋值（否则 vol 槽值域滞后一帧）")
    check(failures, "range: (klines) => (_volDisplayMode === 'macd' ? getVolumeMacdRange(klines) : getVolumeRange(klines))," in js,
          "vol 槽值域按显示模式二选一（类MACD 口径同 getMacdRange，全 0 兜底 ±1）")
    check(failures, "drawVolumeMacd(c.klines, area, range, c.barStep, c.macdBarWidth, c.subPixelOffset);" in js
          and "else drawVolumeAxis(area, range);" in js,
          "vol 槽的绘制与纵轴都按 _volDisplayMode 分派到类MACD 分支")
    check(failures, "drawMacd(klines, volArea, macdRange, barStep, barWidth, subPixelOffset, volMacdOf);" in js,
          "drawVolumeMacd 复用 drawMacd 画法（翻转视图/零线/柱宽口径不漂移）")
    check(failures, "function drawMacd(klines, macdArea, macdRange, barStep, barWidth, subPixelOffset, valOf)" in js
          and "const getVals = valOf || function(k) { return k; };" in js,
          "drawMacd 支持可选取值函数，缺省仍取价格MACD（价格MACD行为不变）")
    check(failures, "if (_volDisplayMode === 'macd') {" in js
          and '"MACD(12,26,9)"' in js
          and "tabLabel: () => (isFuturesMode() ? '成交量' : '成交额')" in js
          and '(isFuturesMode() ? "成交量MACD" : "成交额MACD")' not in js
          and "textArea.x + textArea.w - 4" not in js,
          "类MACD 正文为 MACD(12,26,9)（与价格MACD同构、去掉成交额/量前缀）；"
          "品种口径由标签行右端 chip（tabLabel 的成交额/成交量）承载，"
          "右上角重复绘制已移除")
    # 参数写法与「价格MACD」逐字同构（本轮明确要求：两处 12 26 9 的间隔一致）
    check(failures, 'MACD(12,26,9)' in js and '(12, 26, 9)' not in js,
          "参数写法统一为「12,26,9」（逗号后无空格），无带空格旧写法残留")
    check(failures, "function formatVolMacdVal(v)" in js and "return (v < 0 ? \"-\" : \"\") + formatVolume(Math.abs(v));" in js,
          "类MACD 数值带符号格式化（单位随成交额/量：万/亿 或 手）")
    check(failures, "calcVolMacdMap(data.klines" in js and
                    js.count("_volMacdMap = (_hasBottomSlot('vol') && _volDisplayMode === 'macd')") == 1,
          "类MACD 只在启用该模式时计算（柱状图模式零额外开销）")
    # 双击切换已废除：切换入口改为标签行 chip 单击循环（BOTTOM_ORDER）
    check(failures, "_showVolume = !_showVolume" not in js
          and "_subShowVolume = !_subShowVolume" not in js,
          "双击切换已移除（不得残留 _showVolume / _subShowVolume 的翻转赋值）")
    check(failures, "const BOTTOM_ORDER = ['vol', 'macd', 'rsi'];" in js
          and "const next = BOTTOM_ORDER[(BOTTOM_ORDER.indexOf(_slotAt(i)) + 1) % BOTTOM_ORDER.length];" in js,
          "chip 单击按 BOTTOM_ORDER 循环切换该槽指标（替代双击）")


# ═══════════════════════════════════════════════════════════════════
# ④ 真渲染对照（无头 Chrome）
# ═══════════════════════════════════════════════════════════════════
TEXT_RECORDER_JS = r"""
window.__drawnTexts = [];
window.__drawnTextPos = [];
// 测试环境不建立实时流（EventSource）：用例只验证渲染与显示模式，不依赖实时数据；
// 长连接会让被 page.route 打桩的请求一直挂着，给收尾平添不确定性。
window.EventSource = function () {
  this.readyState = 0; this.url = ''; this.withCredentials = false;
  this.close = function () {};
  this.addEventListener = function () {};
  this.removeEventListener = function () {};
  this.dispatchEvent = function () { return false; };
};
(function () {
  var orig = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type) {
    var ctx = orig.apply(this, arguments);
    if (type === '2d' && ctx && !ctx.__textWrapped) {
      ctx.__textWrapped = true;
      var of = ctx.fillText;
      ctx.fillText = function (t, x, y) {
        try {
          window.__drawnTexts.push(String(t));
          window.__drawnTextPos.push([String(t), x, y]);
        } catch (e) {}
        return of.apply(this, arguments);
      };
    }
    return ctx;
  };
})();
"""

PIXEL_STATS_JS = r"""
(args) => {
  const c = document.querySelector('#chart-container canvas');
  if (!c) return { error: 'no canvas' };
  const dpr = window.devicePixelRatio || 1;
  const W = c.clientWidth, H = c.clientHeight;
  const L = args.layout;
  const netH = H - L.top - L.bottom - L.gap;
  const chartH = netH * (1 - L.volRatio);
  // 槽位改造后底部区有多个槽：允许外部显式指定要统计的**绘图窗**（y0/y1），
  // 未指定时回落到"整个底部区"（仅用于不需要区分槽的粗统计）。
  const y0 = (args.y0 !== undefined) ? args.y0 : L.top + chartH + L.textH;
  const y1 = (args.y1 !== undefined) ? args.y1 : L.top + chartH + netH * L.volRatio;
  const x0 = L.left, x1 = L.left + (W - L.left - L.right - L.rightGap);
  const g = c.getContext('2d');
  const w = Math.max(1, Math.round((x1 - x0) * dpr));
  const h = Math.max(1, Math.round((y1 - y0) * dpr));
  const img = g.getImageData(Math.round(x0 * dpr), Math.round(y0 * dpr), w, h).data;
  let white = 0, orange = 0, red = 0, cyan = 0, ink = 0;
  for (let i = 0; i < img.length; i += 4) {
    const r = img[i], gg = img[i + 1], b = img[i + 2], a = img[i + 3];
    if (a < 8) continue;
    if (r > 240 && gg > 240 && b > 240) white++;              // DIF 线 #FFFFFF
    else if (r > 200 && gg > 90 && gg < 170 && b < 60) orange++; // DEA 线 #F77F00
    else if (r > 120 && gg < 90 && b < 90) red++;              // 红柱/涨柱
    else if (r < 90 && gg > 120 && b > 120) cyan++;            // 青柱/跌柱
    if (r > 60 || gg > 60 || b > 60) ink++;
  }
  // 零线探测：drawMacd 的零线为 rgba(255,255,255,0.2)，叠在 #1a1a2e 底上混合 ≈ (72,72,88)。
  // 底部指标区不画网格线（drawGrid 只作用于价格区），所以"整行多数像素为这种灰"的行
  // 只可能是零线 —— 用它来量 0 轴的实际屏幕位置。
  let zeroLineY = null, zeroRatio = 0;
  for (let yy = 0; yy < h; yy++) {
    let cnt = 0;
    for (let xx = 0; xx < w; xx++) {
      const i = (yy * w + xx) * 4;
      const r = img[i], gg = img[i + 1], b = img[i + 2], a = img[i + 3];
      if (a > 200 && Math.abs(r - gg) <= 8 && (b - r) >= 4 && (b - r) <= 30
          && r >= 45 && r <= 105) cnt++;
    }
    const ratio = cnt / w;
    if (ratio > zeroRatio) { zeroRatio = ratio; zeroLineY = yy; }
  }
  return { white, orange, red, cyan, ink, zeroLineY, zeroRatio,
           area: { x0, y0, x1, y1, w, h }, dpr, W, H };
}
"""


# RSI 槽的水平线探针（**不复用** MACD 零线那套「全宽灰线 + zeroRatio ≥ 0.35」启发式：
# 设计 §2.4.3-4 明确要求 —— RSI 的参考线是虚线、中轴另色，同一探针会误判/漏判）。
# 这里按**颜色分类**统计每行的覆盖比例：底色 #1a1a2e 上叠 rgba(255,255,255,0.15~0.2)
# 的灰（r≈g、b 略大）；RSI 折线 #22D3EE 是青色（g、b 远大于 r），被 |r-g|<=6 排除。
RSI_LINE_PROBE_JS = r"""
(args) => {
  const c = document.querySelector('#chart-container canvas');
  if (!c) return { error: 'no canvas' };
  const dpr = window.devicePixelRatio || 1;
  const g = c.getContext('2d');
  const x0 = args.x0, x1 = args.x1, y0 = args.y0, y1 = args.y1;
  const w = Math.max(1, Math.round((x1 - x0) * dpr));
  const h = Math.max(1, Math.round((y1 - y0) * dpr));
  const img = g.getImageData(Math.round(x0 * dpr), Math.round(y0 * dpr), w, h).data;
  const rows = [];
  let whiteMin = null, whiteMax = null, whiteRows = 0, whitePix = 0, cyanPix = 0;
  for (let yy = 0; yy < h; yy++) {
    let cnt = 0, wy = 0, cy = 0;
    for (let xx = 0; xx < w; xx++) {
      const i = (yy * w + xx) * 4;
      const r = img[i], gg = img[i + 1], b = img[i + 2], a = img[i + 3];
      if (a < 8) continue;
      // 水平参考线 / 中轴：底色 #1a1a2e(r=26) 上叠 rgba(255,255,255,0.15~0.2)。
      // ⚠ 下界必须低到 33：1px 线落在半像素处时抗锯齿会把行覆盖拆成 ~0.4 / 0.6，
      //   峰值 r 只有 40~46 —— 用 48 会把这种行整条滤掉。
      // ⚠ 参考线现在统一是「细点虚线」（1px 点 + 3px 空隙）⇒ 同行覆盖率只有实线的
      //   ~1/4，判据不能再沿用实线口径的 0.20，已下调并由调用方另行量证「点线 vs 实线」。
      if (Math.abs(r - gg) <= 6 && (b - r) >= 4 && (b - r) <= 26
          && r >= 33 && r <= 115) cnt++;
      // RSI 折线：与 MACD 白线同色 #FFFFFF（r=g=b≈255）—— 靠「近白」与上面那类灰线区分
      if (r >= 200 && gg >= 200 && b >= 200) wy++;
      // 旧青蓝 #22D3EE 残留探测（改白之后应为 0）
      if (gg > 100 && b > 100 && r + 40 < gg && r + 40 < b) cy++;
    }
    rows.push(cnt / w);
    if (wy > 0) {
      whiteRows++; whitePix += wy;
      const y = y0 + yy / dpr;
      if (whiteMin === null || y < whiteMin) whiteMin = y;
      if (whiteMax === null || y > whiteMax) whiteMax = y;
    }
    cyanPix += cy;
  }
  return { rows, dpr, w, h, whiteMinY: whiteMin, whiteMaxY: whiteMax,
           whiteRows, whitePix, cyanPix, area: { x0, y0, x1, y1 } };
}
"""


# 槽位分割线探针（要求 ④）：判定「某 css y 处是否有一条**横贯全宽**的分割线」。
#
# 实现侧已保证分割线画在**槽内容之后**（见 _renderChart 里的顺序注释）—— 它的 y 同时是
#   上一槽绘图窗的下沿、柱体基线所在行，画在前面会被柱体盖成断续。所以这里可以只量
#   「整行覆盖率」：不必再按「上方几行是背景」做位掩码去绕开柱体（那套只能拿到 0.399，
#   且会把矮柱的描边溢出当成合格列，无法自证）。
#
# 线判据 r ∈ [30, 115]（灰阶、比底色 #1a1a2e 的 r=26 亮、远暗于文字/柱体）：
#   1px 线落在半像素处会把墨迹劈成 0.9 / 0.1 两行 ⇒ 对该 css y 的 floor / ceil 两个
#   device row 取较优者。
SLOT_DIVIDER_PROBE_JS = r"""
(args) => {
  const c = document.querySelector('#chart-container canvas');
  if (!c) return { error: 'no canvas' };
  const dpr = window.devicePixelRatio || 1;
  const g = c.getContext('2d');
  const x0 = Math.round(args.x0 * dpr), x1 = Math.round(args.x1 * dpr);
  const w = Math.max(1, x1 - x0);
  const isLine = (r, gg, b, a) => a >= 8 && r >= 30 && r <= 115
        && Math.abs(r - gg) <= 6 && (b - r) >= 4 && (b - r) <= 26;
  const res = { w: w, dpr: dpr, rows: {} };
  for (const [tag, yCss] of args.rows) {
    const rf = yCss * dpr;
    const cand = Array.from(new Set([Math.floor(rf), Math.ceil(rf)]));
    let best = { row: null, n: 0, ratio: 0 };
    for (const row of cand) {
      if (row < 0 || row >= c.height) continue;
      const d = g.getImageData(x0, row, w, 1).data;
      let n = 0;
      for (let xx = 0; xx < w; xx++) {
        const k = xx * 4;
        if (isLine(d[k], d[k + 1], d[k + 2], d[k + 3])) n++;
      }
      const ratio = n / w;
      if (ratio > best.ratio) best = { row: row, n: n, ratio: ratio };
    }
    res.rows[tag] = best;
  }
  return res;
}
"""





def _launch_browser(pw):
    """按 本机 Chrome → 捆绑 chromium → ms-playwright 缓存 依次尝试。

    本机 Chrome 优先：本机的「捆绑 chromium」在 browser.close() 时会挂住
    Playwright driver（报 Connection closed while reading from the driver），
    令整个用例收尾卡死；本机 Chrome 的 launch / close 均正常。
    """
    notes = []
    try:
        return pw.chromium.launch(channel="chrome"), "本机 Chrome", notes
    except Exception as e:
        notes.append("channel=chrome: " + str(e).splitlines()[0][:90])
    try:
        return pw.chromium.launch(), "捆绑 chromium", notes
    except Exception as e:
        notes.append("捆绑 chromium: " + str(e).splitlines()[0][:90])
    pats = ["~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe",
            "~/AppData/Local/ms-playwright/chromium-*/chrome-win/chrome.exe",
            "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"]
    for pat in pats:
        for cand in glob.glob(os.path.expanduser(pat)):
            try:
                return pw.chromium.launch(executable_path=cand), os.path.basename(os.path.dirname(cand)), notes
            except Exception as e:
                notes.append(os.path.basename(cand) + ": " + str(e).splitlines()[0][:90])
    return None, "", notes


def _fmt_volume(v, is_futures):
    """formatVolume 的 Python 对照实现（测试侧独立写一遍，避免"两边同错"）。"""
    if is_futures:
        return (f"{v / 10000:.2f}万") if v >= 10000 else str(round(v))
    if v >= 100000000:
        return f"{v / 100000000:.2f}亿"
    if v >= 10000:
        return f"{v / 10000:.2f}万"
    return f"{v:.0f}"


def test_real_render(failures):
    print("\n④ 真渲染对照（无头 Chrome：双击切块 + 逐项量像素 + 标签数值对基准）")
    pl, why = find_playwright()
    if pl is None:
        print(f"  [SKIP] {why}（渲染层无法执行；①②③⑤ 已覆盖契约与数值）")
        return
    from playwright.sync_api import sync_playwright

    if not os.path.isfile(SNAP_STOCK):
        check(failures, False, "股票快照存在", SNAP_STOCK)
        return
    snap_text = open(SNAP_STOCK, encoding="utf-8").read()
    snap = json.loads(snap_text)
    is_futures = snap["meta"].get("market") == "futures"
    # RSI 判据不该依赖 fixture 内容（冻结基线会随输出字段演进被重冻）：这里给
    # **喂给页面的副本**按后端同口径（calculate_rsi）显式算一遍 rsi，即 fixture
    # 缺该字段也能验「折线已画 + 随翻转镜像」（**不动**冻结文件）。
    from App.AppUtils import calculate_rsi as _calc_rsi
    served = json.loads(snap_text)
    rsi_vals = _calc_rsi([k["close"] for k in served["klines"]])
    for _i, _k in enumerate(served["klines"]):
        _k["rsi"] = round(rsi_vals[_i], 4) if _i < len(rsi_vals) else 0
    served_text = json.dumps(served)
    shot_dir = os.environ.get("VOL_MACD_SHOT_DIR") or tempfile.mkdtemp(prefix="vol_macd_shots_")
    os.makedirs(shot_dir, exist_ok=True)

    # 布局常量：从源码读，防止测试与实现的魔法数字各自漂移
    js = read(APP_JS)
    m_pad = re.search(r"const PADDING = \{ top: (\d+), right: (\d+), bottom: (\d+), left: (\d+) \};", js)
    m_vol = re.search(r"const VOL_RATIO = ([\d.]+), GAP = (\d+);", js)
    m_txt = re.search(r"const MACD_TEXT_HEIGHT = (\d+);", js)
    m_gap = re.search(r"const rightGap = (\d+);", js)
    layout = {}
    if m_pad:
        layout.update(top=int(m_pad.group(1)), right=int(m_pad.group(2)),
                      bottom=int(m_pad.group(3)), left=int(m_pad.group(4)))
    if m_vol:
        layout.update(volRatio=float(m_vol.group(1)), gap=int(m_vol.group(2)))
    if m_txt:
        layout["textH"] = int(m_txt.group(1))
    if m_gap:
        layout["rightGap"] = int(m_gap.group(1))
    m_slot = re.search(r"const SLOT_GAP = (\d+);", js)
    if m_slot:
        layout["slotGap"] = int(m_slot.group(1))
    # 单窗 = 2 槽 ⇒ 实际底部区占比 = volRatioFor(2) = VOL_RATIO × 2（设计不变式）。
    # 基线常量名与值都不动，只是**消费方式**从"直接用 VOL_RATIO"变成"×槽数"。
    slot_count = 2
    layout["volRatioBase"] = layout.get("volRatio", 0.2)
    layout["volRatio"] = layout["volRatioBase"] * slot_count
    check(failures,
          all(k in layout for k in ("top", "bottom", "volRatio", "gap", "left",
                                    "textH", "rightGap", "slotGap")),
          "布局常量可从源码解析（测试与实现同源，不各写各的魔法数字）", str(layout))

    import functools
    import http.server
    import threading

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=FRONTEND_DIR))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    checks = []
    with sync_playwright() as pw:
        browser, which, notes = _launch_browser(pw)
        if browser is None:
            print("  [SKIP] 无可用无头浏览器：" + " / ".join(notes[:3]))
            httpd.shutdown()
            return
        print(f"  （无头浏览器：{which}）")
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        page.add_init_script(TEXT_RECORDER_JS)

        def route_api(route):
            url = route.request.url
            if "/api/health" in url:
                body = json.dumps({"config": {"view_count": 233}})
            elif "analyze" in url:
                body = served_text
            else:
                body = "{}"
            route.fulfill(status=200, content_type="application/json", body=body)

        page.route("**/api/**", route_api)
        page.goto(f"http://127.0.0.1:{port}/app.html", wait_until="load")
        page.wait_for_function(
            "() => { const s = window.ChanApp && window.ChanApp.state;"
            " return !!(s && s.chartData && s.chartData.klines && s.chartData.klines.length > 0); }",
            timeout=20000)
        page.wait_for_timeout(400)

        box = page.locator("#chart-container canvas").first.bounding_box()
        checks.append(("页面加载真实快照并完成首屏渲染", box is not None and box["width"] > 200,
                       f"canvas box={box}"))
        if box is None:
            page.unroute_all(behavior="ignore")
            browser.close()
            httpd.shutdown()
            check(failures, False, "canvas 存在", "首屏未渲染出 canvas")
            return

        W, H = box["width"], box["height"]
        netH = H - layout["top"] - layout["bottom"] - layout["gap"]
        chartH = netH * (1 - layout["volRatio"])
        total = netH * layout["volRatio"]
        slotH = (total - slot_count * layout["textH"]
                 - (slot_count - 1) * layout["slotGap"]) / slot_count
        b_top = layout["top"] + chartH          # 底部区上沿
        b_bot = layout["top"] + netH            # 底部区下沿（≡ 改造前 volArea 下沿）
        # 槽 0 = vol、槽 1 = rsi；每槽 = 14px 标签行 + slotH 绘图窗（SLOT_GAP = 0）
        s0_plot_top = b_top + layout["textH"]
        s0_plot_bot = s0_plot_top + slotH
        s1_plot_top = s0_plot_bot + layout["textH"]
        s1_plot_bot = s1_plot_top + slotH
        area_x = layout["left"]
        area_w = W - layout["left"] - layout["right"] - layout["rightGap"]

        # ── 0) 槽几何不变式（与实现同源，先钉住再谈像素） ──
        checks.append(("槽几何：每槽绘图窗 ≡ netH×VOL_RATIO − 标签高（与槽数无关）",
                       abs(slotH - (netH * layout["volRatioBase"] - layout["textH"])) < 1e-6,
                       f"slotH={slotH:.2f} 期望={netH * layout['volRatioBase'] - layout['textH']:.2f}"))
        checks.append(("槽几何：四段（label0/plot0/label1/plot1）恰好铺满底部区、无重叠无空隙",
                       abs(s0_plot_bot - (b_top + layout["textH"] + slotH)) < 1e-6
                       and abs(s1_plot_bot - b_bot) < 1e-6,
                       f"s1_plot_bot={s1_plot_bot:.2f} b_bot={b_bot:.2f}"))

        # ── 1) 默认槽位：单窗 2 槽 ['macd','rsi']、双窗下窗 1 槽 ['macd'] ──
        st0 = page.evaluate("() => ({ slots: window.ChanApp.state._bottomSlots,"
                            " sub: window.ChanApp.state._subBottomSlots })")
        checks.append(("单窗默认双槽 ['macd','rsi']（验收 1）",
                       st0["slots"] == ["macd", "rsi"], str(st0["slots"])))
        checks.append(("双窗下窗默认单槽 ['macd']",
                       st0["sub"] == ["macd"], str(st0["sub"])))

        # ── 2) chip 单击循环：槽 0 macd → rsi →（双击只算一次）→ vol ──
        # chip 几何与实现同源：chip 在标签行**右端**，右边界 = label.x + label.w − 2
        # （= area_x + area_w − 2），高 = MACD_TEXT_HEIGHT − 2 ⇒ 取「右边界内缩 6px」/
        # 标签行竖直中心，落点必在 chip 内（chip 宽 = measureText(name) + 12 ≥ 24px）。
        chip_x = box["x"] + layout["left"] + area_w - 6
        chip0_y = box["y"] + b_top + 7
        page.mouse.click(chip_x, chip0_y)
        page.wait_for_timeout(250)
        s1 = page.evaluate("() => window.ChanApp.state._bottomSlots")
        checks.append(("chip 单击：槽 0 macd → rsi（BOTTOM_ORDER 顺序）",
                       list(s1)[0] == "rsi", str(s1)))
        store = page.evaluate("() => JSON.parse(localStorage.getItem('chan_overlay_settings') || '{}')")
        checks.append(("chip 切换后落盘 localStorage.bottomSlots",
                       list(store.get("bottomSlots") or [])[:1] == ["rsi"],
                       str(store.get("bottomSlots"))))

        # 槽 1 标签行上沿的分割线（要求 ④）：在**槽 0 = rsi** 的这一刻量。
        # ⚠ COLORS.grid = rgba(255,255,255,0.04)（4% 不透明）：分隔线只有在**底色之上**才叠出
        #   可测的 r≈34；压在柱体（深≈10 / 亮≈233）上会落到判据 [30,115] 之外。而该 y 恰好是
        #   槽 0 绘图窗下沿、柱体基线所在行 ⇒ 槽 0 画成交额柱时只有 ~10% 像素可测（0.104），
        #   那是量测口径与被测物的相互作用，不是「没画线」。此刻槽 0 = rsi（单线，不落基线）。
        # x 限定在「无文字区」避免误计：正文自左端起约 300px 内、chip 贴右端（宽 ≤40px），
        # 故取 [x0=area_x+400, x1=area_x+area_w−70] 两头都让开。
        _div_y = s1_plot_top - layout["textH"]        # 标签行上沿 = 上一槽绘图窗下沿
        div_probe = page.evaluate(SLOT_DIVIDER_PROBE_JS,
                                  dict(x0=area_x + 400, x1=area_x + area_w - 70,
                                       rows=[["div", _div_y],
                                             ["above", _div_y - 3.0],
                                             ["up2", _div_y - 6.0],
                                             ["below", _div_y + 3.0]]))

        # 双击 chip：不重置视图（§1.5 的核心）；且只前进一位（浏览器先发两次 click，
        # detail>1 的那次被忽略）—— 不得连切两位。
        view_before = page.evaluate("() => ({ off: window.ChanApp.state.viewOffset,"
                                    " cnt: window.ChanApp.state.viewCount })")
        page.mouse.dblclick(chip_x, chip0_y)
        page.wait_for_timeout(250)
        s2 = page.evaluate("() => window.ChanApp.state._bottomSlots")
        view_after = page.evaluate("() => ({ off: window.ChanApp.state.viewOffset,"
                                   " cnt: window.ChanApp.state.viewCount })")
        checks.append(("双击 chip：只前进一位 rsi → vol（不因双击连切两位）",
                       list(s2)[0] == "vol", str(s2)))
        checks.append(("双击 chip：不触发「恢复全视图」（viewOffset/viewCount 不变）",
                       view_after == view_before, f"{view_before} → {view_after}"))
        checks.append(("双击 chip 不影响槽 1（仍为 rsi）", list(s2)[1] == "rsi", str(s2)))

        # 鼠标移出画布后重渲染，让标签取"最后一根"（否则标签跟随 hover 的K线）
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] + 60)
        page.wait_for_timeout(200)
        region0 = dict(layout=layout, y0=s0_plot_top, y1=s0_plot_bot)
        bar_stats = page.evaluate(PIXEL_STATS_JS, region0)
        page.locator("#chart-container canvas").screenshot(
            path=os.path.join(shot_dir, "1_bar.png"))
        # ── 2) 打开设置抽屉（走真实齿轮按钮）→ 选「类MACD」 ──
        page.click("#btn-settings")
        page.wait_for_timeout(150)
        radios = page.evaluate("() => Array.from("
                               "document.querySelectorAll('input[name=\"vol-display-mode\"]'))"
                               ".map(el => [el.value, el.checked])")
        checks.append(("抽屉打开后单选项与当前模式同步（bar 选中）",
                       radios == [["bar", True], ["macd", False]], str(radios)))
        # 真实点击（走 addEventListener 的生产路径）→ 立即生效
        page.evaluate("() => { window.__drawnTexts.length = 0; window.__drawnTextPos.length = 0; }")
        page.click('input[name="vol-display-mode"][value="macd"]')
        page.wait_for_timeout(250)
        store = page.evaluate("() => JSON.parse(localStorage.getItem('chan_overlay_settings') || '{}')")
        checks.append(("选中类MACD 后立即落盘 localStorage.volDisplayMode",
                       store.get("volDisplayMode") == "macd", str(store.get("volDisplayMode"))))
        checks.append(("选中类MACD 后内存态同步",
                       page.evaluate("() => window.ChanApp.state._volDisplayMode") == "macd", ""))

        page.wait_for_timeout(100)
        macd_texts = page.evaluate("() => window.__drawnTexts.slice()")
        macd_pos = page.evaluate("() => window.__drawnTextPos.slice()")
        macd_stats = page.evaluate(PIXEL_STATS_JS, region0)
        rsi_probe_n = page.evaluate(RSI_LINE_PROBE_JS,
                                    dict(x0=area_x, x1=area_x + area_w,
                                         y0=s1_plot_top, y1=s1_plot_bot))
        page.locator("#chart-container canvas").screenshot(
            path=os.path.join(shot_dir, "2_vol_macd.png"))

        # ── 2b) 翻转视图下同样成立（类MACD 复用 drawMacd 的镜像路径） ──
        # 先清空文字记录：mirror_pos 必须是**翻转后重绘**的那一批 —— 否则非翻转的
        # 文字会一起留下（RSI 纵轴三档同名，会出现两次 ⇒ 三档判据全红）。
        page.evaluate("() => { window.__drawnTexts.length = 0; window.__drawnTextPos.length = 0; }")
        page.evaluate("() => window.toggleMirrorMode()")
        page.wait_for_timeout(250)
        mirror_pos = page.evaluate("() => window.__drawnTextPos.slice()")
        mirror_stats = page.evaluate(PIXEL_STATS_JS, region0)
        rsi_probe_m = page.evaluate(RSI_LINE_PROBE_JS,
                                    dict(x0=area_x, x1=area_x + area_w,
                                         y0=s1_plot_top, y1=s1_plot_bot))
        page.locator("#chart-container canvas").screenshot(
            path=os.path.join(shot_dir, "2b_vol_macd_mirror.png"))
        page.evaluate("() => window.toggleMirrorMode()")
        page.wait_for_timeout(150)

        # ── 3) 切回柱状图，证明是"实时双向生效"（不是单向覆盖） ──
        page.evaluate("() => { window.__drawnTexts.length = 0; window.__drawnTextPos.length = 0; }")
        page.click('input[name="vol-display-mode"][value="bar"]')
        page.wait_for_timeout(250)
        bar_texts = page.evaluate("() => window.__drawnTexts.slice()")
        back_stats = page.evaluate(PIXEL_STATS_JS, region0)
        page.locator("#chart-container canvas").screenshot(
            path=os.path.join(shot_dir, "3_back_to_bar.png"))

        view = page.evaluate("() => ({ off: window.ChanApp.state.viewOffset,"
                             " cnt: window.ChanApp.state.viewCount })")

        # ── 4) 真实 wheel 缩放（要求 ⑥）：放大后 RSI 纵轴值域必须跟着可见窗口变 ──
        # 走生产路径（canvas 的 wheel 监听 → onWheel → 改 viewCount → render），
        # 不用 state 直接赋值，免得绕开缩放分支本身。
        # ⚠ 先关设置抽屉：其背板 #bsp-filter-overlay（z-index 高于画布）会覆盖整屏，
        #   不关的话 wheel 落在背板上、canvas 的监听一次都收不到（viewCount 纹丝不动）。
        #   走生产路径关（抽屉右上角 × 就是 closeBspSettings）—— 不用 evaluate 直接调函数。
        page.click("#bsp-filter-dialog .settings-drawer-close")
        page.wait_for_timeout(200)
        _ovl = page.evaluate("() => { const o = document.getElementById('bsp-filter-overlay');"
                            " return o ? o.classList.contains('show') : null; }")
        checks.append(("抽屉关闭后背板不再拦截画布（wheel 能落到 canvas 上）",
                       _ovl is False, "#bsp-filter-overlay.show = %s" % _ovl))
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.3)
        page.wait_for_timeout(120)
        for _ in range(5):
            page.mouse.wheel(0, -600)      # deltaY < 0 ⇒ 放大（onWheel 每事件一档 1.15×）
        page.wait_for_timeout(150)
        # ⚠ 每个 wheel 事件都会触发一次重绘 ⇒ 文字记录会累积 6 份，z_ax[0] 会取到中间态。
        #   先清空、再单独走最后一次缩放 —— zoom_pos 恰好只含**末次重绘**的刻度。
        page.evaluate("() => { window.__drawnTexts.length = 0; window.__drawnTextPos.length = 0; }")
        page.mouse.wheel(0, -600)
        page.wait_for_timeout(300)
        zoom_view = page.evaluate("() => ({ off: window.ChanApp.state.viewOffset,"
                                  " cnt: window.ChanApp.state.viewCount })")
        zoom_pos = page.evaluate("() => window.__drawnTextPos.slice()")
        zoom_probe = page.evaluate(RSI_LINE_PROBE_JS,
                                   dict(x0=area_x, x1=area_x + area_w,
                                        y0=s1_plot_top, y1=s1_plot_bot))
        # 收尾：先解除 API 打桩再关浏览器 —— 关闭时若恰有被拦截的请求在飞，
        # sync 路由处理器会与 close 互等（详见 TEXT_RECORDER_JS 处的说明）。
        page.unroute_all(behavior="ignore")
        browser.close()
    httpd.shutdown()

    # ── 像素判据 ──
    print("  像素统计：柱状图=%s" % {k: bar_stats.get(k) for k in ("white", "orange", "red", "cyan")})
    print("            类MACD=%s" % {k: macd_stats.get(k) for k in ("white", "orange", "red", "cyan")})
    print("            切回后=%s" % {k: back_stats.get(k) for k in ("white", "orange", "red", "cyan")})
    checks.append(("柱状图模式：底部区域无 DIF 白线 / DEA 橙线（white<30 且 orange<30）",
                   bar_stats.get("white", 999) < 30 and bar_stats.get("orange", 999) < 30,
                   str({k: bar_stats.get(k) for k in ("white", "orange")})))
    checks.append(("柱状图模式：确实有红/青柱（区域非空白）",
                   (bar_stats.get("red", 0) + bar_stats.get("cyan", 0)) > 50,
                   str({k: bar_stats.get(k) for k in ("red", "cyan")})))
    checks.append(("类MACD模式：底部出现 DIF 白线（white>100）",
                   macd_stats.get("white", 0) > 100, str(macd_stats.get("white"))))
    checks.append(("类MACD模式：底部出现 DEA 橙线（orange>100）",
                   macd_stats.get("orange", 0) > 100, str(macd_stats.get("orange"))))
    checks.append(("切回柱状图后白线/橙线消失（双向实时生效，非单向缓存）",
                   back_stats.get("white", 999) < 30 and back_stats.get("orange", 999) < 30,
                   str({k: back_stats.get(k) for k in ("white", "orange")})))
    checks.append(("翻转视图下类MACD 照常绘制（镜像分支不会静默失效）",
                   mirror_stats.get("white", 0) > 100 and mirror_stats.get("orange", 0) > 100,
                   str({k: mirror_stats.get(k) for k in ("white", "orange")})))

    # ── ⑵ 翻转视图：0 轴（零线）镜像正确，且纵轴「0」标签与零线同源对齐 ──
    # drawMacd 的零线 y = macdToY(0)：未翻转 = area.y + h*max/range；
    # 翻转 = area.y + h*(-min)/range。两者之和 = 2*area.y + h*(max-min)/range
    # = 2*area.y + h —— 与 range 内 max/min 取值无关，恒等于指标区上下沿之和。
    def _zero_css(stats):
        zy = stats.get("zeroLineY")
        ratio = stats.get("zeroRatio", 0) or 0
        if zy is None or ratio < 0.35:
            return None
        return s0_plot_top + zy / (stats.get("dpr") or 1)

    zn, zf = _zero_css(macd_stats), _zero_css(mirror_stats)
    checks.append(("0 轴零线可从像素探测（底部指标区的全宽灰线）",
                   zn is not None and zf is not None,
                   "非翻转=%s 翻转=%s ratio=%.2f/%.2f" % (
                       zn, zf, macd_stats.get("zeroRatio", 0) or 0,
                       mirror_stats.get("zeroRatio", 0) or 0)))
    if zn is not None and zf is not None:
        checks.append(("翻转视图下 0 轴镜像正确（翻转前后零线 y 之和 ≡ 槽 0 绘图窗上下沿之和）",
                       abs((zn + zf) - (s0_plot_top + s0_plot_bot)) <= 4.0,
                       "非翻转=%.1f 翻转=%.1f 和=%.1f 期望=%.1f" % (
                           zn, zf, zn + zf, s0_plot_top + s0_plot_bot)))
        for tag, pos, zz in (("未翻转", macd_pos, zn), ("翻转", mirror_pos, zf)):
            zeros = [round(p[2], 1) for p in pos if p[0] == "0"]
            checks.append(("%s时纵轴「0」标签与零线同位（同一根 0 轴，不是两套口径）" % tag,
                           any(abs((z - 4) - zz) <= 4.0 for z in zeros),
                           "标签y=%s 零线=%.1f" % (zeros, zz)))

    # ── 标签文本判据 ──
    def has(prefix, texts):
        return [t for t in texts if t.startswith(prefix)]

    checks.append(("柱状图模式标签为「成交额: …」（数值随成交额）",
                   bool(has("成交额:", bar_texts)) or bool(has("成交量(手):", bar_texts)),
                   str([t for t in bar_texts if ":" in t][:6])))
    exp_vlabel = "MACD(12,26,9)"
    vmacd_label = has(exp_vlabel, macd_texts)
    checks.append((f"类MACD模式正文为「{exp_vlabel}」+ DIF/DEA/BAR（品种口径移到右端 chip）",
                   len(vmacd_label) == 1
                   and len(has("DIF:", macd_texts)) == 1
                   and len(has("DEA:", macd_texts)) == 1
                   and len(has("BAR:", macd_texts)) == 1,
                   str([t for t in macd_texts if ":" in t or "MACD" in t][:8])))
    # ⑴ 品种口径由 chip 承载：'成交额/成交量' 恰好一处（就是 chip 文本），且贴在标签行右端；
    #    改造前是「正文前缀 + 右上角右对齐」各画一遍。
    _chip_pos = [p for p in macd_pos if p[0] in ("成交额", "成交量")]
    _body_x = [p[1] for p in macd_pos if p[0].startswith(exp_vlabel)]
    checks.append(("类MACD：品种口径由标签行**右端**的 chip 唯一承载（'成交额/成交量' 恰一处且 x 在正文右侧）",
                   len(_chip_pos) == 1 and bool(_body_x)
                   and _chip_pos[0][1] > max(_body_x)
                   and _chip_pos[0][1] > area_x + area_w * 0.8,
                   "chip=%s 正文 x=%s 行右端=%.0f" % (
                       [(p[0], round(p[1], 1)) for p in _chip_pos],
                       [round(v, 1) for v in _body_x], area_x + area_w)))
    checks.append(("类MACD 标签的参数写法与价格MACD 逐字同构（12,26,9，逗号后无空格）",
                   bool(vmacd_label) and all("(12,26,9)" in t for t in vmacd_label)
                   and not any("(12, 26, 9)" in t for t in macd_texts), ""))

    # ── 标签数值 ≡ Python 侧 calculate_macd（同源比对，跨语言闭环） ──
    from App.AppUtils import calculate_macd
    klines = snap["klines"]
    total = len(klines)
    start = max(0, int(view["off"]))
    if total and start >= total:
        start = max(0, total - int(view["cnt"]))
    last_idx = min(total, start + int(view["cnt"]) + 2) - 1
    vals = [(k["vol"] if is_futures else k["amount"]) for k in klines]
    ref = calculate_macd(vals)
    exp_dif = _fmt_volume(abs(ref[last_idx]["dif"]), is_futures)
    exp_dif = ("-" if ref[last_idx]["dif"] < 0 else "") + exp_dif
    got_dif = (has("DIF:", macd_texts) or [""])[0]
    checks.append((f"标签 DIF ≡ calculate_macd 在末根可见K线(#{last_idx})的值",
                   got_dif == "DIF:" + exp_dif,
                   f"前端标签 {got_dif!r} vs 基准 {'DIF:' + exp_dif!r}"))

    # ── ⑶ RSI 纵轴：上下档 = 当前可见窗口的**自适应**值域，中间档 = 语义中位 50 ──
    # 与实现同一条公式：可见窗口 = klines[start : start+viewCount+2]（getVisibleKlines 的 +2），
    # 值域 = 该窗 rsi 的 min/max 各留 5%。这里用**喂给页面的那份** served 副本现算。
    def _exp_rsi_span(off, cnt):
        s = max(0, int(off))
        e = min(total, s + int(cnt) + 2)
        rv = [k["rsi"] for k in klines[s:e] if isinstance(k.get("rsi"), (int, float))]
        return min(rv), max(rv)

    def _rsi_range(off, cnt):
        """与 getRsiRange 同式：min/max 各留 5%，再夹回指标定义域 [0,100]。

        夹回是必需项 —— 窗口里出现 RSI = 0（本实现的暖机口径会给 0）时，
        留白会把下沿算成负值印在纵轴上（用户实测上证指数日K 的 −4.76）。
        """
        m, M = _exp_rsi_span(off, cnt)
        margin = (M - m) * 0.05
        return max(0.0, m - margin), min(100.0, M + margin)

    def _rsi_y_in(v, lo, hi, mirror):
        """与 rsiToY 同式。值域自适应后不能再按 v/100 折算槽高。"""
        frac = (v - lo) / (hi - lo)
        return (s1_plot_top + frac * slotH) if mirror \
            else (s1_plot_top + slotH - frac * slotH)

    def _axis_texts(pos):
        """槽 1 绘图窗内的纵轴刻度文字，按 y 升序（上→下）。

        纵轴刻度由 drawRsiAxis 以 fillText(text, x, rsiToY(v) + 4) 绘制 ⇒ 基线 −4 就是刻度 y。
        必须按「基线 −4 落在绘图窗内」筛、且文本必须是纯数字：标签行的 'RSI(12):' /
        chip 的 'RSI' / 槽值 '51.40' 画在 y ≈ 槽上沿 − 3（基线只差 3px），只按原始 y 取
        会把它们混进来（曾因此在本函数下游 _as_num 拿到 'RSI(12):' 而 TypeError）。
        """
        out = []
        for p in pos:
            if _as_num(p[0]) is None:
                continue
            if s1_plot_top - 2.0 <= (p[2] - 4.0) <= s1_plot_bot + 2.0:
                out.append(p)
        return sorted(out, key=lambda p: p[2])

    def _as_num(t):
        try:
            return float(t)
        except ValueError:
            return None

    d_lo, d_hi = _exp_rsi_span(view["off"], view["cnt"])
    lo_n, hi_n = _rsi_range(view["off"], view["cnt"])
    _clamped = lo_n <= 0.0 or hi_n >= 100.0
    has50 = lo_n <= 50 <= hi_n
    ax_n, ax_m = _axis_texts(macd_pos), _axis_texts(mirror_pos)


    checks.append(("RSI 值域已改为自适应：跨度 %.2f（固定口径恒为 100）；未触定义域时 ≡ 数据跨度 ×1.1"
                   % (hi_n - lo_n),
                   (hi_n - lo_n) < 100
                   and (_clamped or abs((hi_n - lo_n) - (d_hi - d_lo) * 1.1) < 1e-9),
                   "值域=[%.2f, %.2f] 数据跨度=%.2f 触边界=%s"
                   % (lo_n, hi_n, d_hi - d_lo, _clamped)))
    checks.append(("RSI 值域恒落在指标定义域内（0 ≤ min < max ≤ 100，留白不得推出界外）",
                   0 <= lo_n < hi_n <= 100,
                   "值域=[%.4f, %.4f]" % (lo_n, hi_n)))
    checks.append(("RSI 纵轴档数 = 3（含中位 50）/ 2（50 落在值域外时省掉中间档）",
                   len(ax_n) == (3 if has50 else 2) and len(ax_m) == (3 if has50 else 2),
                   "非翻转 %s / 翻转 %s；50 在窗内=%s" % (
                       [p[0] for p in ax_n], [p[0] for p in ax_m], has50)))
    top_n = _as_num(ax_n[0][0]) if ax_n else None
    bot_n = _as_num(ax_n[-1][0]) if ax_n else None
    top_m = _as_num(ax_m[0][0]) if ax_m else None
    bot_m = _as_num(ax_m[-1][0]) if ax_m else None
    checks.append(("RSI 纵轴上下档 = 可见窗口值域两端（≈min/max ±5%，不再是写死的 0/100）",
                   None not in (top_n, bot_n, top_m, bot_m)
                   and abs(top_n - hi_n) <= 0.011 and abs(bot_n - lo_n) <= 0.011
                   and abs(top_m - lo_n) <= 0.011 and abs(bot_m - hi_n) <= 0.011,
                   "非翻转 [%s, %s] / 翻转 [%s, %s] vs 期望 [%.2f, %.2f]" % (
                       top_n, bot_n, top_m, bot_m, lo_n, hi_n)))
    if has50 and len(ax_n) == 3:
        checks.append(("RSI 纵轴中间那档恒为 \"50\"（不是 MACD 的零线 \"0\"；"
                       "照抄 drawMacdAxis 会画成两遍 \"0\"）",
                       ax_n[1][0] == "50" and ax_m[1][0] == "50",
                       "%s / %s" % ([p[0] for p in ax_n], [p[0] for p in ax_m])))
    else:
        checks.append(("RSI 纵轴中间那档恒为 \"50\"（本样本 50 出窗，本条按档数判据覆盖）",
                       len(ax_n) == 2 and "50" not in [p[0] for p in ax_n],
                       "%s / %s" % ([p[0] for p in ax_n], [p[0] for p in ax_m])))

    _pos_ok, _pos_det = True, []
    for _tag, _axs, _mir in (("未翻转", ax_n, False), ("翻转", ax_m, True)):
        for _p in _axs:
            _ey = _rsi_y_in(_as_num(_p[0]), lo_n, hi_n, _mir)
            _pos_det.append("%s:%s@%.1f(期望%.1f)" % (_tag, _p[0], _p[2] - 4, _ey))
            if abs((_p[2] - 4) - _ey) > 2.5:
                _pos_ok = False
    checks.append(("RSI 每档刻度的 y 都 ≡ rsiToY(该档值)（与折线 / 参考线同一套 y 口径）",
                   _pos_ok, "; ".join(_pos_det)))

    n_by = {p[0]: p[2] - 4 for p in ax_n}
    m_by = {p[0]: p[2] - 4 for p in ax_m}
    _common = sorted(set(n_by) & set(m_by), key=float)
    checks.append(("翻转是 Y 轴镜像：共有刻度 y 前后之和 ≡ 2×槽上沿 + 槽高",
                   bool(_common) and all(
                       abs((n_by[k] + m_by[k]) - (2 * s1_plot_top + slotH)) <= 4.0
                       for k in _common),
                   "共有 %d 档 %s；非翻转=%s 翻转=%s" % (
                       len(_common), _common,
                       {k: round(v, 1) for k, v in n_by.items()},
                       {k: round(v, 1) for k, v in m_by.items()})))
    tick50 = {"未翻转": n_by.get("50"), "翻转": m_by.get("50")}

    # ── ⑷ RSI 三条参考线（50 中轴 + 80/20 超买超卖，细点虚线）真的画在 rsiToY(v) 上 ──
    def _line_hit(stats, expect_css, tol=2.5):
        rows = stats.get("rows") or []
        d = stats.get("dpr") or 1
        best, best_ratio = None, 0.0
        for yy, ratio in enumerate(rows):
            y_css = stats["area"]["y0"] + yy / d
            if abs(y_css - expect_css) <= tol and ratio > best_ratio:
                best, best_ratio = y_css, ratio
        return best, best_ratio

    _duty = []
    for tag, probe, mirror in (("未翻转", rsi_probe_n, False),
                               ("翻转", rsi_probe_m, True)):
        expect = [v for v in (50, 80, 20) if lo_n <= v <= hi_n]
        hits = {v: _line_hit(probe, _rsi_y_in(v, lo_n, hi_n, mirror)) for v in expect}
        checks.append(("RSI %s：落在值域内的 20 / 50 / 80 参考线都画在 rsiToY(v) 上" % tag,
                       bool(expect) and all(h[0] is not None and h[1] >= 0.05
                                            for h in hits.values()),
                       str({k: (None if h[0] is None else round(h[0], 1), round(h[1], 3))
                            for k, h in hits.items()})))
        if expect and hits.get(50, (None,))[0] is not None and tick50[tag] is not None:
            checks.append(("RSI %s：纵轴 \"50\" 刻度与其水平线同位（同一套 y 口径）" % tag,
                           abs(tick50[tag] - hits[50][0]) <= 2.5,
                           "刻度=%.1f 线=%.1f" % (tick50[tag], hits[50][0])))
        _duty += [h[1] for h in hits.values()]
    checks.append(("RSI 参考线是「细点虚线」而非实线：实测同行覆盖率 %.3f（实线 ≥0.5）"
                   % (max(_duty) if _duty else 0.0),
                   bool(_duty) and max(_duty) <= 0.45,
                   "各参考线覆盖率=%s" % [round(x, 3) for x in _duty]))

    # ── ⑸ RSI 折线（后端下发的 k.rsi）：白色、真的画出来了，且随翻转镜像 ──
    checks.append(("RSI 折线已绘制（槽 1 绘图窗内有白色像素，与 MACD 白线同色）",
                   (rsi_probe_n.get("whitePix") or 0) > 200
                   and (rsi_probe_m.get("whitePix") or 0) > 200,
                   "白像素数：非翻转=%s 翻转=%s" % (rsi_probe_n.get("whitePix"),
                                                   rsi_probe_m.get("whitePix"))))
    checks.append(("RSI 折线不再用旧青蓝 #22D3EE（槽 1 内青像素 = 0）",
                   (rsi_probe_n.get("cyanPix") or 0) == 0
                   and (rsi_probe_m.get("cyanPix") or 0) == 0,
                   "青像素数：非翻转=%s 翻转=%s" % (rsi_probe_n.get("cyanPix"),
                                                   rsi_probe_m.get("cyanPix"))))
    if rsi_probe_n.get("whiteMinY") is not None and rsi_probe_m.get("whiteMinY") is not None:
        _flip = 2 * s1_plot_top + slotH      # Y 轴镜像：y ↦ flip − y
        checks.append(("RSI 折线随翻转镜像：翻转前后 y 的 {min,max} 互换（各自之和 ≡ 2×槽上沿＋槽高）",
                       abs((rsi_probe_n["whiteMinY"] + rsi_probe_m["whiteMaxY"]) - _flip) <= 3.0
                       and abs((rsi_probe_n["whiteMaxY"] + rsi_probe_m["whiteMinY"]) - _flip) <= 3.0,
                       "非翻转 min/max=%.1f/%.1f 翻转=%.1f/%.1f 期望和=%.1f" % (
                           rsi_probe_n["whiteMinY"], rsi_probe_n["whiteMaxY"],
                           rsi_probe_m["whiteMinY"], rsi_probe_m["whiteMaxY"], _flip)))

    # ── ⑺ 槽 1 标签行上沿的分割线（要求 ④）与 chip 短名（要求 ③） ──
    # 实现侧已把分割线画在槽内容之后（见 _renderChart 顺序注释）⇒ 恒横贯全宽，
    #   可直接量整行覆盖率；上下 3px / 6px 三条反证行都必须干净。
    _dr = div_probe.get("rows") or {}
    _div = (_dr.get("div") or {}).get("ratio") or 0.0
    _above = (_dr.get("above") or {}).get("ratio") or 0.0
    _up2 = (_dr.get("up2") or {}).get("ratio") or 0.0
    _below = (_dr.get("below") or {}).get("ratio") or 0.0
    checks.append(("槽 1 标签行上沿有分割线（横贯全宽，与槽 0 那条同源），上下 3px 都无横线",
                   _div >= 0.95 and max(_above, _up2, _below) <= 0.05,
                   "上沿=%.3f 上方3px=%.3f 上方6px=%.3f 下方3px=%.3f" % (
                       _div, _above, _up2, _below)))
    checks.append(("chip 短名为 'RSI'（不再是 'RSI(12)'）；标签行仍带参数 'RSI(12):'",
                   "RSI" in macd_texts and "RSI(12)" not in macd_texts
                   and bool([t for t in macd_texts if t.startswith("RSI(12):")]),
                   "chip 命中=%s 旧串命中=%s" % ("RSI" in macd_texts,
                                              "RSI(12)" in macd_texts)))

    # ── ⑻ 真实 wheel 缩放：RSI 纵轴值域必须随可见窗口变化（要求 ⑥ 的端到端证明） ──
    z_lo, z_hi = _rsi_range(zoom_view["off"], zoom_view["cnt"])
    z_ax = _axis_texts(zoom_pos)
    z_top = _as_num(z_ax[0][0]) if z_ax else None
    z_bot = _as_num(z_ax[-1][0]) if z_ax else None
    checks.append(("wheel 缩放真的生效（viewCount 由 %s 缩到 %s）" % (view["cnt"], zoom_view["cnt"]),
                   int(zoom_view["cnt"]) < int(view["cnt"]),
                   "offset=%s→%s" % (view["off"], zoom_view["off"])))
    checks.append(("放大后 RSI 纵轴值域随新窗口收窄（不再是恒定 0~100 ⇒ 曲线被压平）",
                   None not in (z_top, z_bot)
                   and abs(z_top - z_hi) <= 0.011 and abs(z_bot - z_lo) <= 0.011
                   and (z_hi - z_lo) < (hi_n - lo_n),
                   "缩放前 [%.2f, %.2f]（跨度 %.2f）→ 缩放后 [%.2f, %.2f]（跨度 %.2f）" % (
                       lo_n, hi_n, hi_n - lo_n, z_lo, z_hi, z_hi - z_lo)))
    checks.append(("放大后 RSI 折线仍在槽 1 绘图窗内（值域切换不会静默丢线）",
                   (zoom_probe.get("whitePix") or 0) > 200,
                   "缩放后白像素数=%s" % zoom_probe.get("whitePix")))
    # ── ⑹ 标签 RSI(12) 的数值 ≡ calculate_rsi（与上面 DIF 那条同款跨语言闭环） ──
    exp_rsi = "%.2f" % (round(rsi_vals[last_idx], 4) if last_idx < len(rsi_vals) else 0)
    checks.append((f"标签 RSI(12) ≡ calculate_rsi 在末根可见K线(#{last_idx})的值",
                   bool(has("RSI(12):", macd_texts)) and exp_rsi in macd_texts,
                   "期望 %s，画面里 RSI 文本 %s" % (
                       exp_rsi, [t for t in macd_texts if t.startswith("RSI")][:4])))

    for desc, ok, detail in checks:
        check(failures, ok, desc, detail)
    print("  截图（人工复核）：" + shot_dir)


# ═══════════════════════════════════════════════════════════════════
def _start_watchdog(seconds):
    """总时长看门狗：浏览器收尾一类的挂起不该把整个用例拖死（正常跑约 10s）。"""
    import threading
    import time

    def _fire():
        time.sleep(seconds)
        print("\n[看门狗] 用例超过 %ds 未结束（疑似浏览器收尾挂起），强制退出。" % seconds,
              flush=True)
        os._exit(1)

    threading.Thread(target=_fire, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="兼容参数（本用例无冻结基线，等价校验）")
    ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    _start_watchdog(180)

    print("===== 成交额/量「类MACD」显示模式 =====")
    failures = []
    test_setting_ui(failures)
    test_state_and_persist(failures)
    test_numeric_alignment(failures)
    test_render_dispatch(failures)
    test_real_render(failures)
    print("-" * 64)
    if failures:
        for f in failures:
            print("[FAIL]", f)
        print(f"===== 失败 =====（{len(failures)} 项）")
        return 1
    print("===== 全部通过 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())

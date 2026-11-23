# -*- coding: utf-8 -*-
"""画线工具护栏（同花顺式：线段 / 水平线，2026-11-18）。

钉住两类回归面：
  ① 结构与接线（静态）：按钮在「股票扫描」之后（统计之前）、工具条 DOM/样式存在、render 流
     调用绘制、mousedown/move/up 分流、Del 键删除、click 守卫。
  ② 算法与手势（node 抽真实源码执行，桩只覆盖 DOM/坐标壳）：
     · 坐标换算 drawGlobalIdxToX / drawXToGlobalIdx 互为逆运算；
     · 几何同源：drawGlobalIdxToX 与生产 globalIdxToX 逐值相等
       （只验互逆是不够的——两份副本同时写错照样互逆）；
     · 点到线段距离、命中测试（线段/水平线/未命中/按 symbol|freq 分图隔离）；
     · 水平线单击生成、线段 Down→Move→Up 落定、短草稿丢弃；
     · 选中整体拖动（两端同增量 / 水平线改价）；删除与按图清空；
     · 数据坐标锚定：视口 globalStart 变化后同一 gx 仍指同一批 K 线；
     · 日期锚定：换数据窗口（下标整体平移）后线仍贴住同一批 K 线；
     · 手势边界：主绘图区外不接管（不吞底部 chip）、跨图残留选中不误删、
       拖动被取消时回滚不落盘、点空白立刻重绘。
  ③ 工具条停靠（2026-11-23 用户口径）：默认贴绘图区右上角（不再顶端居中）、
     右偏移与窗口宽度无关、**真拖**（位移 ≥ 阈值）后位置归用户 —— 单击手柄
     不算拖、不能把自动停靠关掉、也不能改写停靠写下的定位；倒计时（流逝时间）
     在画时让到它左边并与之同一行（不钻到它下面、也不压它）；
     双窗重建 #chart-container 后工具条节点必须归位。

抽取手法与 test_measure_gesture.py 同型：注释感知花括号配平抽真实函数，
零网络、零浏览器、秒级。
跑法：python Test/test_draw_tool.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")
APP_HTML = os.path.join(REPO_ROOT, "Frontend", "app.html")
APP_CSS = os.path.join(REPO_ROOT, "Frontend", "app.css")

_passed = 0
_failed = []


def rec(no, title, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
        print("[PASS] %s %s" % (no, title))
    else:
        _failed.append("%s %s | %s" % (no, title, detail))
        print("[FAIL] %s %s" % (no, title) + (" | " + detail if detail else ""))


def read(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


def _to_matching_brace(src, b):
    i, n, depth = b + 1, len(src), 1
    while i < n:
        c = src[i]
        if c in "\"'`":
            q = c
            i += 1
            while i < n and src[i] != q:
                if src[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError("花括号不配对")


def fn_src(src, name):
    m = re.search(r"^[ \t]*function\s+%s\s*\(" % re.escape(name), src, re.M)
    if not m:
        raise AssertionError("未找到 function %s" % name)
    b = src.index("{", m.end())
    return src[m.start():_to_matching_brace(src, b)]


def find_node():
    cand = [os.environ.get("NODE_EXE"), shutil.which("node")]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def run_node(src, tag):
    node = find_node()
    if not node:
        rec(tag, "node 可用", False, "未找到 node")
        return None
    tmp = tempfile.mkdtemp(prefix="draw_tool_")
    p = os.path.join(tmp, "drv.js")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        r = subprocess.run([node, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode != 0:
        rec(tag, "node 执行成功", False, (r.stderr or "").strip()[:600])
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        rec(tag, "node 输出 JSON", False, r.stdout[:600])
        return None


# ══════════════════ 静态结构断言 ══════════════════
js = read(APP_JS)
html = read(APP_HTML)
css = read(APP_CSS)


def static_checks():
    btn = 'id="btn-draw"'
    scan = 'id="btn-scan"'
    stats = 'id="btn-stats"'
    rec("S1", "画线按钮在「股票扫描」之后、「统计」之前（2026-11-23 用户口径）",
        all(x in html for x in (btn, scan, stats))
        and html.index(scan) < html.index(btn) < html.index(stats))
    rec("S2", "工具条 DOM 含三种模式 + 删除/清空/退出",
        all(x in html for x in ("draw-toolbar", "draw-tool-select", "draw-tool-segment",
                                "draw-tool-horizontal", "draw-tool-delete",
                                "draw-tool-clear", "draw-tool-close")))
    rec("S3", "工具条样式存在（含选中态）",
        all(x in css for x in (".draw-toolbar", ".draw-tool.active", "#btn-draw.active")))
    rec("S4", "render 流调用 drawDrawings 且只画主图",
        "drawDrawings(area, priceRange, barStep, subPixelOffset);" in js and
        "if (data !== dualSubData) drawDrawings" in js)
    rec("S5", "mousedown/move/up 三处均分流画线手势",
        all(x in js for x in ("if (_drawMode && _drawHandleDown(e))",
                              "if (_drawMode && _drawHandleMove(e))",
                              "if (_drawMode && _drawHandleUp(e))")))
    rec("S6", "Del/Backspace 删除选中且排除输入框",
        "e.key !== 'Delete' && e.key !== 'Backspace'" in js and
        "t.tagName === 'INPUT'" in js and "deleteSelectedDraw(); e.preventDefault();" in js)
    rec("S7", "画线手势 click 守卫吞掉尾巴 click",
        "if (_drawClickGuardArmed) { _drawClickGuardArmed = false; return; }" in js)
    rec("S8", "导出工具条入口供内联 onclick 调用",
        all(x in js for x in ("window.toggleDrawToolbar", "window.setDrawMode",
                              "window.deleteSelectedDraw", "window.clearAllDraws")))
    rec("S9", "画布事件注册齐全（down/move/up/leave）",
        all(x in js for x in ('canvas.addEventListener("mousedown", onMouseDown)',
                              'canvas.addEventListener("mousemove", onMouseMove)',
                              'canvas.addEventListener("mouseup", onMouseUp)',
                              'canvas.addEventListener("mouseleave", onMouseLeave)')))

    rec("S10", "工具条标题兼拖拽手柄（id + move 光标 + 防选中）",
        'id="draw-toolbar-handle"' in html and
        '.draw-toolbar-label' in css and 'cursor: move' in css and
        'user-select: none' in css and
        '_drawBindToolbarDrag' in js)
    rec("S11", "选择按钮为十字准星 SVG 图标（保留 id/onclick/aria-label）",
        'id="draw-tool-select"' in html and 'draw-tool-icon' in html and
        'draw-tool-icon svg' in css and 'aria-label="选择"' in html and
        'setDrawMode(\'select\')' in html and
        '<path d="M11 2h2v9h9v2h-9v9h-2v-9H2v-2h9V2z"' in html)

    rec("S12", "画线持久化：localStorage 键 + 5 处落盘钩子 + 启动恢复",
        "const DRAW_STORAGE_KEY = 'chan_draw_lines'" in js and
        "localStorage.setItem(DRAW_STORAGE_KEY" in js and
        js.count("_drawPersist();") >= 5 and
        "_drawRestore();   // 恢复上次重启前的画线" in js and
        js.index("function _drawRestore") > js.index("function _drawSerialize"))
    # 「显示设置」抽屉的图层复选框也写「线段」（2026-11-20 从顶栏 btn-seg 搬来），故 S14 必须限定在工具条片段内检查
    _bar_html = html[html.index('id="draw-toolbar"'):html.index('id="range-slider"')]
    # 本段片段：否定断言只对本段生效（别处用虚线不该让画线的断言变红）
    _draw_fn = fn_src(js, "drawDrawings")
    rec("S15", "倾斜线草稿为红色实线（拉动中与选中态一致）",
        "ctx.strokeStyle = '#ff5252'; ctx.lineWidth = 2; ctx.setLineDash([]);" in js and
        "// 绘制中的草稿线（红色实线，与选中态一致）" in js and
        "setLineDash([5, 4])" not in _draw_fn)
    rec("S16", "草稿线只有倾斜线一种（无 horizontal 死分支）",
        "_drawDraft && _drawDraft.key === _drawCurrentKey()" in _draw_fn and
        "d.type === 'horizontal'" not in _draw_fn)

    rec("S14", "工具条顺序：水平线在倾斜线左 + 倾斜线文案",
        _bar_html.index('id="draw-tool-horizontal"') < _bar_html.index('id="draw-tool-segment"') and
        '>水平线</button>' in _bar_html and '>倾斜线</button>' in _bar_html and
        'title="倾斜线：按住鼠标拖动画斜线"' in _bar_html and
        '>线段</button>' not in _bar_html)
    rec("S13", "持久化健壮性：反序列化脏数据校验 + id 续号",
        all(x in js for x in ("function _drawDeserialize", "catch(e) { return []; }",
                              "if (!Array.isArray(arr)) return [];",
                              "function _drawNextIdFrom", "return m + 1;")))

    # ── 工具条停靠（2026-11-23：右上角 + 避开「流逝时间」）──
    # 片段限定：app.css 里别处也有 left:50%（居中遮罩），全局搜会把那条误算进来
    _dock_css = css[css.index(".draw-toolbar {"):css.index(".draw-toolbar-label {")]
    _dock_fn = fn_src(js, "_drawToolbarDock")
    _dock_sync = fn_src(js, "_drawSyncToolbar")
    _dock_cd = fn_src(js, "drawCountdownBar")
    _bind_fn = fn_src(js, "_drawBindToolbarDrag")
    _arm_fn = fn_src(js, "_drawToolbarDragArmed")
    rec("S17", "工具条默认停靠右上角（顶端居中定位零残留）",
        "right: 6px" in _dock_css and
        "left: 50%" not in _dock_css and "translateX(-50%)" not in _dock_css)
    rec("S18", "停靠接线：打开前就位 + 倒计时两分支都回写 + 拖拽改 left 必须清 right",
        "_drawToolbarDock();" in _dock_sync and
        _dock_cd.count("_drawToolbarDock(area)") == 2 and
        _dock_fn.count("bar.style.right") == 2 and
        'bar.style.right = "auto";' in js and
        "_drawToolbarDragged = true;" in js)
    rec("S19", "拖过后不再自动停靠；避让只认上一帧真盒子（_countdownBounds）",
        "if (!bar || _drawToolbarDragged) return;" in _dock_fn and
        "hostW - cd.x + DRAW_TOOLBAR_DOCK_GAP" in _dock_fn and
        "cd.h - barH" in _dock_fn and
        "_calcCountdownState" not in _dock_fn)
    rec("S20", "双窗重建 #chart-container 后画线工具条归位（innerHTML='' 不丢节点）",
        js.count("innerHTML = ''") == 2 and
        'const keepDrawBar = document.getElementById("draw-toolbar");' in js and
        js.count("if (keepDrawBar) container.appendChild(keepDrawBar);") == 1 and
        js.count("if (keepDrawBar) container2.appendChild(keepDrawBar);") == 1)
    # 2026-11-23 评审⑴：单击手柄（按下即抬起）曾经在 mousedown 就置 _drawToolbarDragged，
    # 于是「点一下标题」= 自动停靠永久关闭直到刷新。落地必须推迟到越过阈值那一帧。
    _drag_down = _bind_fn[_bind_fn.index('addEventListener("mousedown"'):
                           _bind_fn.index('addEventListener("mousemove"')]
    _drag_move = _bind_fn[_bind_fn.index('addEventListener("mousemove"'):]
    rec("S21", "拖拽起手：mousedown 只记起手点，越过阈值才写样式/置「位置归用户」",
        "_drawToolbarDragged = true;" not in _drag_down and
        "bar.style" not in _drag_down and
        "_drawToolbarDragged = true;" in _drag_move and
        "_drawToolbarDragArmed(d, e.clientX, e.clientY)" in _drag_move and
        "DRAW_TOOLBAR_DRAG_MIN" in _arm_fn and
        "d.live = true;" in _drag_move)


# ══════════════════ node 行为断言 ══════════════════
FNS = ["_drawCurrentKey", "_drawVisibleLines", "buildGlobalDateMap", "globalIdxToX",
       "_drawPruneSelection", "_drawDateMap", "_drawAnchorGx", "_drawMaterialize",
       "_drawReanchor", "drawGlobalIdxToX", "drawXToGlobalIdx",
       "drawPointSegDist", "drawHitTest", "drawDrawings", "_drawMousePos",
       "_drawHandleDown", "_drawHandleMove", "_drawHandleUp", "_drawCancelGesture",
       "deleteSelectedDraw", "clearAllDraws", "_drawToolbarPos", "_drawToolbarDock",
       "_drawBindToolbarDrag", "_drawToolbarDragArmed",
       "_drawSerialize", "_drawDeserialize", "_drawNextIdFrom", "priceToY", "yToPrice"]

PREAMBLE = r"""
let _drawLines=[], _drawMode=null, _drawSelected=null, _drawDraft=null, _drawDrag=null;
let _drawGeom=null, _drawClickGuardArmed=false, _drawNextId=1, _drawDateMapCache=null;
const DRAW_HIT_PX=6, DRAW_MIN_PX=4;
let chartData=null, currentFreq='5m', _logScale=false, _isMirrorMode=false;
const renderCalls=[]; function render(){ renderCalls.push(1); }
// 持久化写入桩：抽取的真实函数（_drawHandleDown/Up、deleteSelectedDraw、
// clearAllDraws）会调 _drawPersist；落盘行为由静态 S12 钩子断言覆盖，这里只吞调用。
const persistCalls=[]; function _drawPersist(){ persistCalls.push(1); }
const canvas={ getBoundingClientRect(){ return {left:0,top:0}; } };
const _stroke=[];
const ctx={ save(){},restore(){},beginPath(){},rect(){},clip(){},setLineDash(){},
  moveTo(x,y){ this._m=[x,y]; }, lineTo(x,y){ _stroke.push([this._m[0],this._m[1],x,y]); },
  stroke(){}, arc(){}, fill(){}, fillStyle:'',strokeStyle:'',lineWidth:1 };
const AREA={x:0,y:0,w:800,h:400}, PR={min:3000,max:4000};
function G(globalStart,subPx){ return {area:AREA,priceRange:PR,barStep:80,subPixelOffset:(subPx||0),globalStart:globalStart||0}; }
function mkChart(sym,klines){ return {meta:{symbol:sym||'IF2606',freq:'5m'},klines:klines||[]}; }
// 造 klines：日期锚点只关心 date（下标 = 数组下标，换窗口时整体平移）
function mkDates(from,n){ const a=[];
  for(let i=0;i<n;i++){ a.push({date:'d'+String(from+i).padStart(3,'0')}); } return a; }
function reset(){ _drawLines=[]; _drawMode='segment'; _drawSelected=null; _drawDraft=null;
  _drawDrag=null; _drawGeom=G(0); _drawClickGuardArmed=false; _drawNextId=1;
  _drawDateMapCache=null; persistCalls.length=0;
  chartData=mkChart(); currentFreq='5m'; renderCalls.length=0; _stroke.length=0; }
const ev=(x,y,btn)=>({clientX:x,clientY:y,button:btn===undefined?0:btn,preventDefault(){}});
const res=[];
function check(no,actual,expected){
  res.push({no:no, ok:JSON.stringify(actual)===JSON.stringify(expected),
            got:actual===undefined?null:actual, want:expected===undefined?null:expected});
}
function near(a,b,eps){ return Math.abs(a-b)<=(eps||1e-6); }
function arrNear(a,b,eps){ return a.length===b.length && a.every((v,i)=>near(v,b[i],eps)); }
// ── 工具条停靠桩：style 用 Proxy 记写入次数（验「只在变化时写」）、
//    容器宽可变（验「右偏移与窗口宽无关」）。DRAW_TOOLBAR_DOCK_* 由抽取脚本
//    从 app.js 正则取真值注入，这里不写死，避免两份默认值各说各话。
let _dockWrites = 0;
const _dockStyle = new Proxy({}, {
  set(t, k, v) { _dockWrites++; t[k] = v; return true; },
  get(t, k) { return t[k]; }
});
const _dockHost = { clientWidth: 1500 };
// 拖拽起手要量 bar 的视觉矩形（B43 用 _barRect 摆一个「已停靠在右上角」的位置）
const _barRect = { left: 900, top: 6, w: 216, h: 33 };
// offsetHeight 给真机工具条高度（真机 ~33px）；隐藏态量到 0 的分支由 B39f 单独覆盖
const _dockBar = { style: _dockStyle, parentNode: _dockHost, offsetHeight: 33,
  getBoundingClientRect() { return { left: _barRect.left, top: _barRect.top,
                                     width: _barRect.w, height: _barRect.h }; } };
// ── DOM 桩（B43 用真源码驱动「单击 / 真拖」两条路径，不重写一遍判定逻辑）──
const _handleEl = { _h: [], addEventListener(k, f) { this._h.push(f); } };
const _chartContainer = { clientWidth: 1200, clientHeight: 700,
  getBoundingClientRect() { return { left: 0, top: 0 }; } };
const _docHandlers = {};
const document = {
  getElementById(id) {
    if (id === "draw-toolbar") return _dockBar;
    if (id === "draw-toolbar-handle") return _handleEl;
    if (id === "chart-container") return _chartContainer;
    return null;
  },
  addEventListener(k, f) { (_docHandlers[k] = _docHandlers[k] || []).push(f); }
};
const _winHandlers = {};
const window = { addEventListener(k, f) { (_winHandlers[k] = _winHandlers[k] || []).push(f); } };
let _drawToolbarDragged = false;
let _drawToolbarDrag = null;
let _countdownBounds = null;
// 绘图区宽度跟着容器宽走（真机：w = clientWidth − PADDING.left/right − rightGap(55)）；
// 写死 1413 会让「右偏移与窗口宽无关」那条断言假红/假绿
function getChartArea() { return { x: 10, y: 20, w: _dockHost.clientWidth - 87, h: 700 }; }
"""

SCENARIOS = r"""
// ── B1 坐标换算互为逆运算 ──
reset();
let g=G(0), gxList=[0,2.5,7,19.75];
let round=gxList.map(gx=>drawXToGlobalIdx(drawGlobalIdxToX(gx,g),g));
check('B1',arrNear(round,gxList),true);
// 视口右移（globalStart=5）后换算仍自洽
g=G(5); round=gxList.map(gx=>drawXToGlobalIdx(drawGlobalIdxToX(gx,g),g));
check('B1b',arrNear(round,gxList),true);
// 非零 subPixelOffset：钉住逆运算里的 + 号（符号写反必红）
g=G(3,17.5); round=gxList.map(gx=>drawXToGlobalIdx(drawGlobalIdxToX(gx,g),g));
check('B1c',arrNear(round,gxList),true);

// ── B2 点到线段距离 ──
check('B2a',near(drawPointSegDist(10,5,0,0,20,0),5),true);
check('B2b',near(drawPointSegDist(-3,0,0,0,20,0),3),true);
check('B2c',near(drawPointSegDist(10,0,5,5,5,5),Math.sqrt(50)),true);

// ── B3 命中测试：水平线 / 线段 / 未命中 / 分图隔离 ──
reset();
_drawLines.push({id:1,key:'IF2606|5m',type:'horizontal',py:3500}); // y=200
check('B3a',!!drawHitTest(400,200,G(0)),true);
check('B3b',drawHitTest(400,260,G(0)),null);
_drawLines.push({id:2,key:'IF2606|5m',type:'segment',gx0:2,py0:3500,gx1:6,py1:3600});
// gx2→x200,y200 ; gx6→x520,y160 ; 中点≈(360,180)
check('B3c',(drawHitTest(360,180,G(0))||{}).id,2);
// 换图（当前 key 不匹配）→ 任何线都不命中
chartData=mkChart('rb'); currentFreq='d';
check('B3d',drawHitTest(400,200,G(0)),null);
chartData=mkChart('IF2606'); currentFreq='5m';

// ── B4 水平线单击生成 ──
reset(); _drawMode='horizontal';
let ok= _drawHandleDown(ev(400,200));
check('B4a',ok,true);
check('B4b',_drawLines.length,1);
check('B4c',_drawLines[0].type,'horizontal');
check('B4d',near(_drawLines[0].py,3500,1e-6),true);
check('B4e',_drawClickGuardArmed,true);

// ── B5 线段 Down→Move→Up 落定 ──
reset(); _drawMode='segment';
_drawHandleDown(ev(200,200));       // gx=2,py=3500
_drawHandleMove(ev(520,160));       // gx=6,py=3600
check('B5a',!!_drawDraft,true);
_drawHandleUp(ev(520,160));
check('B5b',_drawLines.length,1);
let seg=_drawLines[0];
check('B5c',seg.type,'segment');
check('B5d',near(seg.gx0,2,1e-6)&&near(seg.gx1,6,1e-6),true);
check('B5e',near(seg.py0,3500,1e-4)&&near(seg.py1,3600,1e-4),true);

// ── B6 短草稿丢弃 ──
reset(); _drawMode='segment';
_drawHandleDown(ev(200,200));
_drawHandleMove(ev(201,200));       // 1px < DRAW_MIN_PX
_drawHandleUp(ev(201,200));
check('B6',_drawLines.length,0);

// ── B7 选中整体拖动 ──
reset();
_drawLines.push({id:1,key:'IF2606|5m',type:'segment',gx0:2,py0:3500,gx1:6,py1:3600});
_drawMode='select';
_drawHandleDown(ev(360,180));        // 命中线段中点
check('B7a',!!_drawDrag,true);
_drawHandleMove(ev(440,180));        // x+80 => gx+1
_drawHandleUp(ev(440,180));
let moved=_drawLines[0];
check('B7b',near(moved.gx0,3,1e-6)&&near(moved.gx1,7,1e-6),true);
check('B7c',near(moved.py0,3500,1e-4)&&near(moved.py1,3600,1e-4),true);
// 水平线拖动只改价
reset(); _drawLines.push({id:2,key:'IF2606|5m',type:'horizontal',py:3500});
_drawMode='select'; _drawHandleDown(ev(400,200));
_drawHandleMove(ev(400,160));        // y-40 => 价 +100
_drawHandleUp(ev(400,160));
check('B7d',near(_drawLines[0].py,3600,1e-2),true);

// ── B8 删除 / 按图清空 ──
reset();
_drawLines.push({id:1,key:'IF2606|5m',type:'horizontal',py:3500});
_drawLines.push({id:2,key:'rb|d',type:'horizontal',py:3400});
_drawSelected=_drawLines[0];
deleteSelectedDraw();
check('B8a',_drawLines.length,1);
check('B8b',_drawLines[0].key,'rb|d');
clearAllDraws(); // 清当前图 IF2606|5m —— 已无该图线，rb|d 应保留
check('B8c',_drawLines.length,1);
check('B8d',_drawLines[0].key,'rb|d');

// ── B9 数据坐标锚定：视口右移后同一 gx 仍指同一 K 线 ──
reset();
let g0=G(0), g5=G(5);
let gxAnchor=10;
let x0=drawGlobalIdxToX(gxAnchor,g0);   // 视口起点0时屏幕X
// 视口右移5根后，同一 globalIdx 的屏幕X应左移 5*barStep=400
check('B9a',near(drawGlobalIdxToX(gxAnchor,g5),x0-400,1e-6),true);
check('B9b',near(drawXToGlobalIdx(x0,g0),gxAnchor,1e-6),true);

// ── B10 绘制真的产出线段（非空断言，防静默空壳）──
reset();
_drawLines.push({id:1,key:'IF2606|5m',type:'segment',gx0:2,py0:3500,gx1:6,py1:3600});
drawDrawings(AREA,PR,80,0);
check('B10',_stroke.length>=1,true);

// ── B11 select 点空白交回默认手势 ──
reset(); _drawMode='select';
check('B11',_drawHandleDown(ev(10,380)),false);

// ── B12 拖动/绘制武装 click 守卫 ──
reset(); _drawMode='horizontal'; _drawClickGuardArmed=false;
_drawHandleDown(ev(400,200));
check('B12',_drawClickGuardArmed,true);

// ── B13-B18 工具条拖拽纯几何（含右/下边界钳制）──
// 容器 800x400，工具条 200x30 ⇒ 可移动范围 x∈[0,600] y∈[0,370]
check('B13',JSON.stringify(_drawToolbarPos(100,50,110,60,210,160,800,400,200,30)),
      JSON.stringify({x:200,y:150}));
check('B14',JSON.stringify(_drawToolbarPos(100,50,110,60,0,0,800,400,200,30)),
      JSON.stringify({x:0,y:0}));                // 左上越界（nx=-10,ny=-10）钳到 0
check('B15',JSON.stringify(_drawToolbarPos(100,50,110,60,2000,2000,800,400,200,30)),
      JSON.stringify({x:600,y:370}));             // 右下越界钳到 cw-bw / ch-bh
check('B16',_drawToolbarPos(100,50,110,60,110,60,800,400,200,30).x,100); // 原地不动
check('B17',_drawToolbarPos(100,50,110,60,610,360,800,400,200,30).x,600);
check('B18',_drawToolbarPos(100,50,110,60,610,360,800,400,200,30).y,350); // y未触底不钳

// ── B19-B27 画线持久化：序列化 / 脏数据 / id 续号 ──
const two=[{id:1,key:'IF2606|5m',type:'horizontal',py:3500},
           {id:3,key:'IF2606|5m',type:'segment',gx0:2,py0:3,gx1:6,py1:9}];
check('B19',_drawSerialize(two),JSON.stringify(two));          // 往返稳定
check('B20',JSON.stringify(_drawDeserialize(JSON.stringify(two))),JSON.stringify(two));
check('B21',_drawDeserialize('not-json').length,0);            // 坏 JSON 不崩
check('B22',_drawDeserialize('{}').length,0);                  // 非数组不崩
check('B23',_drawDeserialize('null').length,0);                // null 不崩
check('B24',_drawDeserialize('').length,0);                    // 空串
const dirty=JSON.stringify([null, 42, {key:'x'}, {key:'k',type:'horizontal',py:'abc'},
  {key:'k',type:'horizontal',py:1}, {key:'k',type:'segment',gx0:1,py0:2,gx1:3,py1:4},
  {key:'k',type:'bogus',py:5},
  // 未知 type + 完整数值：数值校验兜不住它，type 白名单是唯一防线（去掉必红）
  {key:'k',type:'bogus',gx0:1,py0:2,gx1:3,py1:4},
  // null 会被 Number() 转成 0，num() 的 null 拦截是唯一防线（去掉必红）
  {key:'k',type:'horizontal',py:null},
  // 非 number（false/''/[]）同理：类型守卫是唯一防线（去掉必红）
  {key:'k',type:'horizontal',py:false}]);
check('B25',_drawDeserialize(dirty).length,2);                // 只留 2 条合法线
check('B26',_drawNextIdFrom(_drawDeserialize(JSON.stringify(two))),4); // 续号 max(1,3)+1
check('B27',_drawNextIdFrom([]),1);                           // 空则 1

// ── B28 跨图残留选中不得误删（切标的/周期后 Del 只作废选中）──
reset();
_drawLines.push({id:1,key:'IF2606|5m',type:'horizontal',py:3500});
_drawSelected=_drawLines[0];
chartData=mkChart('rb'); currentFreq='d';      // 切图：选中仍指向上一张图
deleteSelectedDraw();
check('B28a',_drawLines.length,1);             // 看不见的线没被删
check('B28b',_drawSelected,null);              // 选中已作废
chartData=mkChart('IF2606'); currentFreq='5m'; _drawMode='select';
check('B28c',_drawHandleDown(ev(400,200)),true);  // 回原图仍点得中（线还在原位）
check('B28d',_drawLines.length,1);

// ── B29 日期锚点：换数据窗口（下标整体平移）后线跟着 K 线走 ──
reset();
chartData=mkChart('IF2606',mkDates(0,200));    // d000..d199
_drawGeom=G(100);                              // 视口从 d100 起 ⇒ 屏内可见 d100..
_drawMode='segment';
_drawHandleDown(ev(200,200));                  // gx=102
_drawHandleMove(ev(520,160));                  // gx=106
_drawHandleUp(ev(520,160));
let segD=_drawLines[0];
check('B29a',segD.gx0,102);
check('B29b',segD.t0,'d102');
check('B29c',segD.t1,'d106');
// 重取数：只剩最后 100 根（d100..d199），d102 的下标由 102 变成 2
chartData=mkChart('IF2606',mkDates(100,100));
check('B29d',_drawAnchorGx(segD,0),2);
check('B29e',_drawAnchorGx(segD,1),6);
check('B29f',drawGlobalIdxToX(_drawAnchorGx(segD,0),G(0)),200);  // 画在 2 号 K 线上
// 该日期已不在窗口内 ⇒ 退回落线时的下标（不丢线、不崩）
chartData=mkChart('IF2606',mkDates(0,100));
check('B29g',_drawAnchorGx(segD,0),102);
// 空 klines（早期/加载中）⇒ 同样退回下标
chartData=mkChart('IF2606');
check('B29h',_drawAnchorGx(segD,1),106);

// ── B30 手势只在主绘图区内接管（底部 chip / 右轴不被吞）──
reset(); _drawMode='horizontal';
check('B30a',_drawHandleDown(ev(400,420)),false);   // 主图下沿之外（底部指标带）
check('B30b',_drawLines.length,0);                  // 不再生成看不见的线
check('B30c',_drawClickGuardArmed,false);           // 没武装守卫 ⇒ chip 的 click 不被吞
_drawMode='segment';
check('B30d',_drawHandleDown(ev(850,200)),false);   // 右轴之外
check('B30e',_drawDraft,null);

// ── B31 几何同源：drawGlobalIdxToX 与生产 globalIdxToX 逐值相等 ──
let geoBad=0, geoCases=0;
[0,1,5,37,500].forEach(gs=>[0,0.5,17.5,79.9].forEach(sp=>[8,24.37,80].forEach(bs=>{
  const gg={area:{x:13,y:0,w:800,h:400},priceRange:PR,barStep:bs,
            subPixelOffset:sp,globalStart:gs};
  [0,2.5,7,19.75,gs+3].forEach(gx=>{
    geoCases++;
    if(!near(drawGlobalIdxToX(gx,gg), globalIdxToX(gx,gs,13,bs,sp), 1e-12)) geoBad++;
  });
})));
check('B31',[geoCases,geoBad],[300,0]);

// ── B37 极性钉死：subPixelOffset 在 X 公式里是「减」──
//    B31 验的是委托关系（恒等式），把正向 drawGlobalIdxToX 与逆向
//    drawXToGlobalIdx 的 subPixelOffset 符号「成对对调」后，两边仍自洽：
//    B1 互逆组绿、B31 绿、整棵树 97 项全绿 —— 全图（K 线+画线）一起平移，
//    相对关系不变，所以画线工具本身没坏，但公式极性没人守。
//    这里用绝对像素值把极性钉住，让这类"两侧同时翻"的自洽变异无法蒙混。
check('B37a', near(globalIdxToX(3, 0, 100, 80, 1.5), 378.5, 1e-9), true);
check('B37b', near(drawGlobalIdxToX(3, {area:{x:100,y:0,w:800,h:400}, priceRange:PR,
        barStep:80, subPixelOffset:1.5, globalStart:0}), 378.5, 1e-9), true);
check('B37c', near(globalIdxToX(103, 100, 13, 24.37, 79.9), 18.395, 1e-9), true);

// ── B32 拖动中被取消（移出画布）⇒ 回滚到按下时快照且不落盘 ──
reset();
_drawLines.push({id:1,key:'IF2606|5m',type:'segment',gx0:2,py0:3500,gx1:6,py1:3600});
_drawMode='select';
_drawHandleDown(ev(360,180));
_drawHandleMove(ev(440,180));       // 就地改成 gx 3..7
_drawCancelGesture();
let back=_drawLines[0];
check('B32a',near(back.gx0,2,1e-6)&&near(back.gx1,6,1e-6),true);
check('B32b',near(back.py0,3500,1e-4)&&near(back.py1,3600,1e-4),true);
check('B32c',persistCalls.length,0);   // 取消 = 本次拖动作废，不落盘
check('B32d',_drawDrag,null);

// ── B33 select 点空白：清选中并立刻重绘（不留残影）──
reset();
_drawLines.push({id:1,key:'IF2606|5m',type:'segment',gx0:2,py0:3500,gx1:6,py1:3600});
_drawMode='select'; _drawSelected=_drawLines[0]; renderCalls.length=0;
check('B33a',_drawHandleDown(ev(10,380)),false);
check('B33b',_drawSelected,null);
check('B33c',renderCalls.length,1);

// ── B34 水平线只锚价格（与窗口无关，不挂日期）──
reset(); chartData=mkChart('IF2606',mkDates(0,50));
_drawMode='horizontal'; _drawHandleDown(ev(400,200));
check('B34a',_drawLines[0].t0===undefined,true);
check('B34b',near(_drawLines[0].py,3500,1e-6),true);

// ── B35 拖动定稿后重写日期锚点（否则换窗口会跳回原位）──
reset(); chartData=mkChart('IF2606',mkDates(0,50));
_drawLines.push({id:1,key:'IF2606|5m',type:'segment',gx0:2,py0:3500,gx1:6,py1:3600,
                 t0:'d002',t1:'d006'});
_drawMode='select';
_drawHandleDown(ev(360,180));
_drawHandleMove(ev(440,180));       // +1 根
_drawHandleUp(ev(440,180));
check('B35a',_drawLines[0].t0,'d003');
check('B35b',_drawLines[0].t1,'d007');

// ── B36 拖动起点用「解析后的下标」：存着旧 gx、日期有效时也不许跳 ──
reset(); chartData=mkChart('IF2606',mkDates(100,100));   // d100..d199
_drawLines.push({id:1,key:'IF2606|5m',type:'segment',gx0:102,py0:3500,gx1:106,py1:3600,
                 t0:'d102',t1:'d106'});
_drawMode='select';
check('B36a',_drawHandleDown(ev(360,180)),true);   // 按解析后的位置(2..6)命中的
check('B36b',_drawDrag&&_drawDrag.ln.gx0,2);        // 按下即兑现成当前下标
_drawHandleUp(ev(360,180));
check('B36c',_drawLines[0].t0,'d102');              // 原位不动 ⇒ 锚点不变

// ── B38-B42 工具条停靠（右上角 + 避开流逝时间）──
function resetDock() { _dockWrites = 0; _drawToolbarDragged = false; _countdownBounds = null;
  delete _dockStyle.right; delete _dockStyle.top; delete _dockStyle.left; }
// B38 无倒计时：贴右上角，右沿对齐绘图区右沿（1500-(10+1413)=77）
resetDock(); _drawToolbarDock();
check('B38a', _dockStyle.right, '77px');
check('B38b', _dockStyle.top, DRAW_TOOLBAR_DOCK_TOP + 'px');
check('B38c', _dockStyle.left, 'auto');
check('B38d', _dockWrites, 3);
// B39 流逝时间在画：让到它**左边**、跟它**同一行**（用户口径：左工具、右流逝时间）
_dockWrites = 0;
_countdownBounds = { x: 1350, y: 26, w: 33, h: 18 };
_drawToolbarDock();
check('B39a', _dockStyle.right, (1500 - 1350 + DRAW_TOOLBAR_DOCK_GAP) + 'px');
check('B39b', 1500 - parseInt(_dockStyle.right, 10) <= 1350, true);   // 右沿停在倒计时左沿之外
check('B39c', parseInt(_dockStyle.top, 10) < 26 + 18, true);          // 没掉到流逝时间下面
check('B39d', Math.abs((parseInt(_dockStyle.top, 10) + 33 / 2) - (26 + 18 / 2)) <= 1, true);
check('B39e', _dockWrites, 2);                                        // right + top 各写一次
// B39f 隐藏态量不到高度（offsetHeight = 0）⇒ 退回贴顶，不抛（下次显示再对齐）
resetDock(); _dockBar.offsetHeight = 0;
_countdownBounds = { x: 1350, y: 26, w: 33, h: 18 };
_drawToolbarDock();
check('B39f', _dockStyle.top, DRAW_TOOLBAR_DOCK_TOP + 'px');
_dockBar.offsetHeight = 33;
// B40 手动拖过 ⇒ 位置归用户，dock 一个字都不写
_dockStyle.top = '200px'; _dockStyle.right = 'auto'; _dockWrites = 0;
_drawToolbarDragged = true;
_drawToolbarDock();
check('B40a', [_dockStyle.top, _dockStyle.right], ['200px', 'auto']);
check('B40b', _dockWrites, 0);
// B41 倒计时消失 ⇒ 回贴顶；同状态重复调用不重复写（幂等）
resetDock(); _drawToolbarDock();
check('B41a', _dockStyle.top, DRAW_TOOLBAR_DOCK_TOP + 'px');
check('B41b', _dockWrites, 3);
_dockWrites = 0; _drawToolbarDock();
check('B41c', _dockWrites, 0);
// B42 右偏移与窗口宽度无关（resize 后无需重算停靠）
_dockHost.clientWidth = 1000; resetDock(); _drawToolbarDock();
check('B42', _dockStyle.right, '77px');
_dockHost.clientWidth = 1500;

// ── B43 拖拽起手语义：单击不关自动停靠、真拖才接管（2026-11-23 评审⑴）──
// 驱动 app.js 真源码：mousedown 只记起手点，位移越过 DRAW_TOOLBAR_DRAG_MIN 的那一帧
// 才写样式 + 置 _drawToolbarDragged ⇒「点一下标题」不会把停靠永久关掉。
_drawBindToolbarDrag();
const _fireDoc = function(k, e) { (_docHandlers[k] || []).forEach(function(f) { f(e); }); };
const _pressHandle = function(x, y) {
  _handleEl._h.forEach(function(f) {
    f({ clientX: x, clientY: y, preventDefault: function() {}, stopPropagation: function() {} });
  });
};
const _mv = function(x, y) { return { clientX: x, clientY: y }; };
const _resetDragState = function() {
  for (const k in _dockStyle) delete _dockStyle[k];
  _dockWrites = 0; _drawToolbarDragged = false; _drawToolbarDrag = null;
};
_resetDragState();
_pressHandle(900, 20);                        // 按下手柄（bar 视觉左上角 900,6）
check('B43a', _drawToolbarDragged, false);    // 按下 ≠ 拖过
check('B43b', _dockWrites, 0);                // 且没改写停靠写下的 right/top
check('B43c', !!_drawToolbarDrag, true);      // 只记了起手点
_fireDoc('mousemove', _mv(902, 21));          // 阈值内抖动（2px）
check('B43d', _drawToolbarDragged, false);
check('B43e', _dockWrites, 0);
_fireDoc('mousemove', _mv(906, 20));          // 越过阈值（6px）⇒ 这一刻才接管
check('B43f', _drawToolbarDragged, true);
check('B43g', _dockStyle.right, 'auto');      // right 必须清掉（left+right 并存会拉成整行宽）
check('B43h', _dockStyle.left, '906px');
check('B43i', _dockStyle.top, '6px');
_fireDoc('mouseup', _mv(906, 20));
check('B43j', _drawToolbarDrag, null);        // 松手复位：不按键移动鼠标不再拖
// 纯单击（按下 → 抬起，中间无 mousemove）必须一个样式都不改，停靠照旧有效
_resetDragState();
_pressHandle(900, 20);
_fireDoc('mouseup', _mv(900, 20));
check('B43k', _drawToolbarDragged, false);
check('B43l', _dockWrites, 0);

console.log(JSON.stringify(res));
"""


def behavior_checks():
    try:
        parts = [PREAMBLE]
        # 停靠常量取 app.js 真值（测试不另写一份默认值，避免两边各说各话）
        consts = re.findall(r"^\s*const (DRAW_TOOLBAR_\w+) = (\d+);", js, re.M)
        need = ("DRAW_TOOLBAR_DOCK_TOP", "DRAW_TOOLBAR_DOCK_GAP", "DRAW_TOOLBAR_DRAG_MIN")
        if not set(need) <= set(k for k, _ in consts):
            rec("B0b", "抽取工具条常量", False, repr(consts))
            return
        parts.append("".join("const %s = %s;\n" % (k, v) for k, v in consts))
        for name in FNS:
            parts.append(fn_src(js, name) + "\n")
        parts.append(SCENARIOS)
        src = "\n".join(parts)
    except AssertionError as e:
        rec("B0", "抽取画线真实函数", False, str(e))
        return
    data = run_node(src, "B0")
    if data is None:
        return
    for item in data:
        rec(item["no"], "行为 %s" % item["no"], item["ok"],
            "got=%s want=%s" % (json.dumps(item["got"]), json.dumps(item["want"])))


try:
    static_checks()
except AssertionError as e:
    # 抽取锚点缺失（典型场景：拿旧树跑新护栏）=== 记一条红、照常出合计与失败清单。
    # 直接抛栈会把「失败清单」一起吞掉，只剩一个 traceback，看不出断在哪一条。
    rec("S0", "静态断言段跑完（抽取锚点齐全）", False, str(e))
try:
    behavior_checks()
except AssertionError as e:
    rec("B0z", "行为断言段跑完（抽取锚点齐全）", False, str(e))

print("\n合计 %d 项，通过 %d，失败 %d" % (_passed + len(_failed), _passed, len(_failed)))
if _failed:
    print("失败清单：")
    for f in _failed:
        print("  - " + f)
    sys.exit(1)

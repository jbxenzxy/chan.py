# -*- coding: utf-8 -*-
"""画线工具护栏（同花顺式：线段 / 水平线，2026-11-18）。

钉住两类回归面：
  ① 结构与接线（静态）：按钮在「统计」之后、工具条 DOM/样式存在、render 流
     调用绘制、mousedown/move/up 分流、Del 键删除、click 守卫。
  ② 算法与手势（node 抽真实源码执行，桩只覆盖 DOM/坐标壳）：
     · 坐标换算 drawGlobalIdxToX / drawXToGlobalIdx 互为逆运算；
     · 点到线段距离、命中测试（线段/水平线/未命中/按 symbol|freq 分图隔离）；
     · 水平线单击生成、线段 Down→Move→Up 落定、短草稿丢弃；
     · 选中整体拖动（两端同增量 / 水平线改价）；删除与按图清空；
     · 数据坐标锚定：视口 globalStart 变化后同一 gx 仍指同一批 K 线。

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
    stats = 'id="btn-stats"'
    rec("S1", "画线按钮存在且在「统计」之后",
        btn in html and stats in html and html.index(btn) > html.index(stats))
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
    rec("S11", "选择按钮改鼠标指针 SVG 图标（保留 id/onclick/aria-label）",
        'id="draw-tool-select"' in html and 'draw-tool-icon' in html and
        'draw-tool-icon svg' in css and 'aria-label="选择"' in html and
        'setDrawMode(\'select\')' in html and
        '<path d="M4 2.2v16.1l4.3-4.1 3 6.6 2.7-1.2-3-6.4h5.9L4 2.2z"' in html)

    rec("S12", "画线持久化：localStorage 键 + 5 处落盘钩子 + 启动恢复",
        "const DRAW_STORAGE_KEY = 'chan_draw_lines'" in js and
        "localStorage.setItem(DRAW_STORAGE_KEY" in js and
        js.count("_drawPersist();") >= 5 and
        "_drawRestore();   // 恢复上次重启前的画线" in js and
        js.index("function _drawRestore") > js.index("function _drawSerialize"))
    # 顶栏 btn-seg 也写「线段」，故 S14 必须限定在工具条片段内检查
    _bar_html = html[html.index('id="draw-toolbar"'):html.index('id="range-slider"')]
    rec("S14", "工具条顺序：水平线在倾斜线左 + 倾斜线文案",
        _bar_html.index('id="draw-tool-horizontal"') < _bar_html.index('id="draw-tool-segment"') and
        '>水平线</button>' in _bar_html and '>倾斜线</button>' in _bar_html and
        'title="倾斜线：按住鼠标拖动画斜线"' in _bar_html and
        '>线段</button>' not in _bar_html)
    rec("S13", "持久化健壮性：反序列化脏数据校验 + id 续号",
        all(x in js for x in ("function _drawDeserialize", "catch(e) { return []; }",
                              "if (!Array.isArray(arr)) return [];",
                              "function _drawNextIdFrom", "return m + 1;")))


# ══════════════════ node 行为断言 ══════════════════
FNS = ["_drawCurrentKey", "_drawVisibleLines", "drawGlobalIdxToX", "drawXToGlobalIdx",
       "drawPointSegDist", "drawHitTest", "drawDrawings", "_drawMousePos",
       "_drawHandleDown", "_drawHandleMove", "_drawHandleUp", "_drawCancelGesture",
       "deleteSelectedDraw", "clearAllDraws", "_drawToolbarPos",
       "_drawSerialize", "_drawDeserialize", "_drawNextIdFrom", "priceToY", "yToPrice"]

PREAMBLE = r"""
let _drawLines=[], _drawMode=null, _drawSelected=null, _drawDraft=null, _drawDrag=null;
let _drawGeom=null, _drawClickGuardArmed=false, _drawNextId=1;
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
function mkChart(sym){ return {meta:{symbol:sym||'IF2606',freq:'5m'},klines:[]}; }
function reset(){ _drawLines=[]; _drawMode='segment'; _drawSelected=null; _drawDraft=null;
  _drawDrag=null; _drawGeom=G(0); _drawClickGuardArmed=false; _drawNextId=1;
  chartData=mkChart(); currentFreq='5m'; renderCalls.length=0; _stroke.length=0; }
const ev=(x,y,btn)=>({clientX:x,clientY:y,button:btn===undefined?0:btn,preventDefault(){}});
const res=[];
function check(no,actual,expected){
  res.push({no:no, ok:JSON.stringify(actual)===JSON.stringify(expected),
            got:actual===undefined?null:actual, want:expected===undefined?null:expected});
}
function near(a,b,eps){ return Math.abs(a-b)<=(eps||1e-6); }
function arrNear(a,b,eps){ return a.length===b.length && a.every((v,i)=>near(v,b[i],eps)); }
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
  {key:'k',type:'bogus',py:5}]);
check('B25',_drawDeserialize(dirty).length,2);                // 只留 2 条合法线
check('B26',_drawNextIdFrom(_drawDeserialize(JSON.stringify(two))),4); // 续号 max(1,3)+1
check('B27',_drawNextIdFrom([]),1);                           // 空则 1

console.log(JSON.stringify(res));
"""


def behavior_checks():
    try:
        parts = [PREAMBLE]
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


static_checks()
behavior_checks()

print("\n合计 %d 项，通过 %d，失败 %d" % (_passed + len(_failed), _passed, len(_failed)))
if _failed:
    print("失败清单：")
    for f in _failed:
        print("  - " + f)
    sys.exit(1)

    (function() {
        "use strict";

// ══════════════════════════════════════════════════════════════════
        // ChanApp 组件注册表
        // 单文件内 10 个组件区块；注册表登记各组件对外接口，
        // 跨组件调用走注册表或 window.* 绑定，闭包内部实现不改。
        // 控制台可经 ChanApp.components 调试各组件入口。
// ══════════════════════════════════════════════════════════════════
        const ChanApp = {
            version: "6.0",
            phase: 6,
            components: {}
        };

// ══════════════════════════════════════════════════════════════════
        // [STATE] SharedState —— 组件共享状态（声明顺序与原文件一致，
        // 保证初始化语义零漂移；各状态按主要使用方就近注释归属）
// ══════════════════════════════════════════════════════════════════

        let chartData = null, canvas, ctx;

        let showBi = true, showFx = false, showZs = true, showSeg = false, showBsp = true, showBiIdx = false;

        // BSP买卖点类型过滤：默认全部显示（0,1,2,3 对应 bs_type 配置）
        let bspFilter = { '0': true, '1': true, '2': true, '3': true };

        // 均线周期：选中的周期集合，默认空（不显示均线）
        const MA_PERIODS = [5, 13, 21, 34, 55, 89, 144, 233];

        const MA_COLORS = { 5:'#FFFFFF', 13:'#FCBF49', 21:'#F77F00', 34:'#90BE6D', 55:'#22D3EE', 89:'#3B82F6', 144:'#A8A8A8', 233:'#8822DD' };

        let maPeriods = {};  // {5: true, 13: true, ...}

        let _logScale = false; // 坐标系模式：false=普通坐标系+等差网格, true=对数坐标系+等比网格

        let _showVolume = false; // 上窗/单窗 底部区域显示模式：false=MACD, true=成交额（双击切换）

        let _subShowVolume = false; // 双窗口下窗 底部区域显示模式（独立，不与上窗联动）

        // 频率→秒数映射（后端单一事实源 /api/health 下发，本地常量仅作离线兜底）
        let FREQ_SEC_MAP_JS = { 'w': 604800, 'd': 86400, '30m': 1800, '5m': 300, '1m': 60, '15s': 15 };

        // 前端视口默认显示的K线根数（所有周期相同）——后端经 /api/health 下发
        // （config.view_count，见 App/AppConfig.py 的 VIEW_COUNT），此默认值仅作离线兜底。
        let VIEW_COUNT = 377;

        const PADDING = { top: 20, right: 22, bottom: 36, left: 10 };

        const VOL_RATIO = 0.2, GAP = 12;

        const MACD_TEXT_HEIGHT = 14;

        let viewOffset = 0, viewCount = VIEW_COUNT;

        let isDragging = false, dragStartX = 0, dragStartOffset = 0;

        let mouseX = -1, mouseY = -1;

        let _currentClipText = "";

        let _mouseDownX = 0, _mouseDownY = 0;

        // 区间选择状态机: IDLE(空闲) | SELECTED_A(已选起点)
        let _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };

        let _currentGlobalIdx = -1;

        let _overlayData = null;

        let initialized = false;

        let currentFreq = 'd'; // 当前周期: d=日K, 30m=30分钟

        let lastStockFreq = 'd';     // 股票上下文上次使用的周期（同类切换继承）

        let lastFuturesFreq = '5m'; // 期货上下文上次使用的周期（同类切换继承）

        // 双窗口状态
        let isDualWindow = false;

        let dualSubData = null;

        let dualSubFreq = '';

        let dualSubViewOffset = 0, dualSubViewCount = VIEW_COUNT;

        let dualSubMouseX = -1, dualSubMouseY = -1;

        let mainCanvas, mainCtx, subCanvas, subCtx;

        // 翻转视图模式：将上涨行情反转为下跌、下跌反转为上涨（缠论做空视角）
        let _isMirrorMode = false;

        // 取消选点菜单项是否可用（有选点且非双窗口/非复盘模式）
        let _restartEnabled = false;

        // K线倒计时进度条（快期3风格：右上角红色进度条+剩余时间）
        let _countdownTimer = null;

        let dualSubIsDragging = false, dualSubDragStartX = 0, dualSubDragStartOffset = 0;

        let dualSubMouseDownX = 0, dualSubMouseDownY = 0; // 底部窗口点击坐标

        let _subCurrentGlobalIdx = -1; // 底部窗口当前鼠标指向的全局索引

        let _subClipText = ""; // 底部窗口当前K线信息文本

        let dualHighlightRange = null; // {startIdx, endIdx} 下面窗口高亮范围（灰框）

        let dualRedRange = null;     // {beforeStart, beforeEnd, afterStart, afterEnd} 下面窗口红框范围

        let dualOffscreenState = false; // 状态A：当前鼠标指向的K线对应区间在下面窗口视口外

        let dualNewZsData = null;       // 双窗口新模式：红框内笔计算的新中枢数据 {zs: [...], zs_stars: [...]}

        let dualShowNewZs = false;      // 双窗口新模式：是否绘制新中枢（替代原线段/中枢/买卖点）

        let dualNewZsLeftDate = "";     // 双窗口新模式：上次请求的红框左边界日期（用于去重）

        let dualNewZsRightDate = "";    // 双窗口新模式：上次请求的红框右边界日期（用于去重）

        let dualNewZsFailedKey = "";    // 双窗口新模式：失败请求去重，避免同一红框反复请求

        let activeDualWindow = 'main';   // 当前激活的窗口：'top' 或 'bottom'，控制底部滚动条作用于哪个窗口

        let _ctrlPressed = false;         // Ctrl键是否按下（用于红框计算优化）

        // 文字标注状态
        let annotations = [];          // 当前标注列表: [{date, text, y_offset}]

        let _annotationTargetDate = ""; // 右键点击的K线日期

        let _annotationTargetY = 0;     // 右键点击的Y坐标（图表内相对坐标，用于标注定位）

        let _annotationTargetX = 0;     // 右键点击的X坐标（用于菜单定位）

        let _annotationClickTarget = null; // 右键点击命中的标注对象 {date, text, y_offset}，null表示未命中

        let _annotationEditOldText = "";   // 编辑模式下被修改的旧文字

        let _annotationDialogMode = "add"; // "add" 或 "edit"

        // ===== 日期输入框：按周期切换 date / datetime-local =====
        const INTRADAY_FREQS_JS = ["30m", "5m", "1m", "15s"];

        // 实时模式（期货/期指 SSE 推送）
        let isRealtimeMode = false;       // 是否处于实时模式

        let realtimeSymbol = null;        // 实时模式下当前品种代码

        let realtimeFreq = null;          // 实时模式下当前周期

        let realtimeStartTime = null;     // 实时模式下选点起始时间

        let realtimeEndTime = null;       // 复盘软断开边界（end_time）

        let realtimeEventSource = null;   // SSE EventSource 对象

        let realtimeConnected = false;    // SSE 是否已连接

        const COLORS = {
            bg: "#1a1a2e", grid: "rgba(255,255,255,0.04)", text: "#8892b0", textLight: "#a8b2d1",
            up: "#FF3C3C", down: "#00F0F0", bi: "#FFD700",
            crosshair: "rgba(255,255,255,0.3)",
            macdUp: "rgba(255,60,60,0.6)", macdDown: "rgba(0,240,240,0.6)", // 原值: macdUp="rgba(255,68,68,0.6)", macdDown="rgba(0,221,0,0.6)"
            dif: "#FFFFFF", dea: "#F77F00", // 原值: dea="#FFD700"
        };

        // ===== K线倒计时进度条（快期3风格） =====
        let _countdownBounds = null; // 上窗/单窗倒计时区域边界，用于增量更新

        let _subCountdownBounds = null; // 下窗倒计时区域边界，用于增量更新

        // ── 盘后数据下载 ──
        var _downloadTimer = null;

        var _downloadRunning = false;

        // ============================================================
        // 股票买卖点扫描（逐只扫描，实时进度，可中断）
        // ============================================================
        let _scanRunning = false;

        let _scanAborted = false;

        let _scanTaskId = null; // 当前批量扫描 task_id（中止时立即经 /api/stocks/scan/{task_id}/cancel 传播）

        let _scanMode = "ann"; // "ann" = 标注扫描, "ma" = 均线分类扫描, "fangliang" = 放量扫描, "fx_d" = 底分型扫描, "bsp" = 买卖点扫描

        let _scanRecentDays = 1; // 最近N根K线，默认1

        let _scanSources = ["zxg"]; // 多选：["zxg", "page_index", "tdxhy2", "tdxhy3"]

        let _scanFreq = "d"; // 扫描周期，默认日K

        let _dateKeyArrow = false, _dateKeyEnter = false, _dateManualTyping = false;

        let _dateInputTriggered = false;   // input 已触发 gotoDate，change 跳过

        let _dateFocusOriginal = "";       // onfocus 保存的原始值，用于 blur 恢复

        let _datePickerInteracted = false; // datetime-local picker 中用户有过交互，blur 时不恢复原始值

        let _datePickerInputCount = 0;     // datetime-local picker 打开后真实交互次数

        // (期货复盘边界 _futuresRealtimeBorderDate 已随左右箭头删除：复盘统一由 gotoDate/SSE 承载)

        const HISTORY_KEY = "chan_stock_history";

        const MAX_HISTORY = 20;

        // 固定快捷入口：常驻历史列表顶部，不参与保存/删除/清除
        // 前 7 项为五大核心指数+创业板+科创50；后 4 项为 CFFEX 四大期指主连（排序按用户要求）
        const FIXED_INDICES = [
            {code: "sh000001", name: "上证指数"},
            {code: "sz399001", name: "深证成指"},
            {code: "sh000300", name: "沪深300"},
            {code: "sh000905", name: "中证500"},
            {code: "sh000852", name: "中证1000"},
            {code: "sz399006", name: "创业板指"},
            {code: "sh000688", name: "科创50"},
            {code: "KQ.m@CFFEX.IF", name: "沪深300主连"},
            {code: "KQ.m@CFFEX.IH", name: "上证50主连"},
            {code: "KQ.m@CFFEX.IC", name: "中证500主连"},
            {code: "KQ.m@CFFEX.IM", name: "中证1000主连"},
        ];

        const FIXED_CODES = new Set(FIXED_INDICES.map(x => normalizeCode(x.code)));

        let searchTimer = null;

        let searchResults = [];

        let selectedIndex = -1;

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] KLineChart —— K线图表组件（渲染引擎 / 坐标系 / 交互 / 倒计时）
        // 对外接口（ChanApp.components.KLineChart）: render, renderSingle, renderTop, renderBottom, resizeCanvas, priceToY, yToPrice, getChartArea, getVisibleKlines, getPriceRange, drawCandles, drawBiLines, drawZs, drawBspMarkers, drawMaLines, drawCrosshair, drawDateAxis, onWheel, toggleOverlay, toggleDualWindow, applyOverlayButtonStates, cancelSelectedPoint
// ══════════════════════════════════════════════════════════════════

        // 股票双窗：下窗视口对齐上窗视口「首末K线时间范围」：
        //   - _parseViewDate：日期型(仅日期)按当日 00:00(左)/23:59:59(右) 归一，日内按原时刻；
        //   - alignDualSubViewport：把下窗视口 [dualSubViewOffset, +dualSubViewCount]
        //     重算为上窗当前视口时间范围内对应下窗K线；下窗数据不足则降为「全量加载与显示」；
        //   - key 守卫：仅上窗视口(viewOffset/viewCount)或下窗数据变化时重算，
        //     避免悬停/常规重绘把下窗独立缩放/平移打回。
        let _alignedSubRef = null, _alignedSubTs = null, _lastAlignKey = '';
        function _parseViewDate(ds, forEnd) {
            const d = ds.length === 10
                ? new Date(ds.replace(/\//g, "-") + (forEnd ? "T23:59:59.999" : "T00:00:00.000"))
                : new Date(ds.replace(/\//g, "-").replace(" ", "T"));
            return d.getTime();
        }
        function alignDualSubViewport() {
            if (!isDualWindow || !dualSubData || !dualSubData.klines
                || !chartData || !chartData.klines) return;
            // 期货双窗不参与对齐（保持独立视口）
            if (chartData.meta && chartData.meta.market === 'futures') return;
            const sK = dualSubData.klines, mK = chartData.klines;
            if (!mK.length || !sK.length) return;
            if (dualSubData !== _alignedSubRef) {
                _alignedSubRef = dualSubData;
                _alignedSubTs = sK.map(k => _parseViewDate(k.date, false));
                _lastAlignKey = ''; // 新数据必须先重算一次
            }
            const key = viewOffset + ':' + viewCount;
            if (key === _lastAlignKey) return;
            _lastAlignKey = key;
            let firstIdx = Math.max(0, Math.floor(viewOffset));
            let lastIdx = firstIdx + Math.floor(viewCount) - 1;
            if (lastIdx >= mK.length) lastIdx = mK.length - 1;
            if (firstIdx > lastIdx) return;
            const startD = _parseViewDate(mK[firstIdx].date, false);
            const endD = _parseViewDate(mK[lastIdx].date, true);
            const ts = _alignedSubTs;
            let lo = 0, hi = ts.length;
            while (lo < hi) { const mid = (lo + hi) >> 1; if (ts[mid] < startD) lo = mid + 1; else hi = mid; }
            const sFirst = (lo < ts.length) ? lo : -1;
            lo = 0; hi = ts.length;
            while (lo < hi) { const mid = (lo + hi) >> 1; if (ts[mid] <= endD) lo = mid + 1; else hi = mid; }
            const sLast = lo - 1;
            if (sFirst < 0 || sLast < 0 || sFirst > sLast) {
                dualSubViewOffset = 0;
                dualSubViewCount = sK.length; // 下窗无法对齐上窗范围 -> 降为全量
                return;
            }
            dualSubViewOffset = sFirst;
            dualSubViewCount = sLast - sFirst + 1;
        }

        // 从 localStorage 恢复叠加层开关状态
        function loadOverlaySettings() {
            try {
                const raw = localStorage.getItem('chan_overlay_settings');
                if (!raw) return;
                const s = JSON.parse(raw);
                if (typeof s.showBi === 'boolean') showBi = s.showBi;
                if (typeof s.showFx === 'boolean') showFx = s.showFx;
                if (typeof s.showZs === 'boolean') showZs = s.showZs;
                if (typeof s.showSeg === 'boolean') showSeg = s.showSeg;
                if (typeof s.showBsp === 'boolean') showBsp = s.showBsp;
                if (typeof s.showBiIdx === 'boolean') showBiIdx = s.showBiIdx;
                if (typeof s.showVolume === 'boolean') _showVolume = s.showVolume;
                if (typeof s.showSubVolume === 'boolean') _subShowVolume = s.showSubVolume;
                if (s.bspFilter && typeof s.bspFilter === 'object') {
                    for (var k in s.bspFilter) { bspFilter[k] = s.bspFilter[k]; }
                }
                if (s.maPeriods && typeof s.maPeriods === 'object') {
                    for (var p in s.maPeriods) { maPeriods[p] = s.maPeriods[p]; }
                }
                if (typeof s.logScale === 'boolean') _logScale = s.logScale;
            } catch(e) {}
        }

        // 保存叠加层开关状态到 localStorage
        function saveOverlaySettings() {
            try {
                const s = {
                    showBi: showBi, showFx: showFx,
                    showZs: showZs, showSeg: showSeg, showBsp: showBsp, showBiIdx: showBiIdx,
                    showVolume: _showVolume,
                    showSubVolume: _subShowVolume,
                    bspFilter: bspFilter,
                    maPeriods: maPeriods,
                    logScale: _logScale
                };
                localStorage.setItem('chan_overlay_settings', JSON.stringify(s));
            } catch(e) {}
        }

        function getShowMa() { return Object.keys(maPeriods).some(function(p){ return maPeriods[p]; }); }

        // 根据保存的设置更新按钮 UI 状态
        function applyOverlayButtonStates() {
            document.getElementById("btn-bi").classList.toggle("active", showBi);
            document.getElementById("btn-fx").classList.toggle("active", showFx);
            document.getElementById("btn-zs").classList.toggle("active", showZs);
            document.getElementById("btn-seg").classList.toggle("active", showSeg);
            document.getElementById("btn-bsp").classList.toggle("active", showBsp);
        }

        // 辅助函数：30分钟K线显示时间
        function getKlineEndTime(dateStr, showSeconds) {
            const parts = dateStr.split(/[-\/\s:]/);
            const yy = parts[0].slice(2);
            const mm = parts[1];
            const dd = parts[2];
            const hh = parts[3];
            const min = parts[4];
            const ss = parts[5];
            if (showSeconds && ss !== undefined) {
                return `${yy}/${mm}/${dd} ${hh}:${min}:${ss}`;
            }
            return `${yy}/${mm}/${dd} ${hh}:${min}`;
        }

        // 双窗口：上面周期 -> 下面周期映射（默认配对）
        function getDualSubFreq(mainFreq) {
            // 股票周期映射
            if (mainFreq === 'w') return 'd';
            if (mainFreq === 'd') return '30m';
            if (mainFreq === '30m') return '5m';
            // 期货周期映射（股票5m无对应，期货5m→1m）
            if (mainFreq === '5m') return '1m';
            if (mainFreq === '1m') return '15s';
            return null; // 5m(股票)/15s(期货)无对应
        }

        // 股票双窗口配对空间（配对放宽至 6 对，与后端 _STOCKS_DUAL_PAIRS 同口径）
        // 上窗周期 → 可选下窗周期集合；getDualSubFreq 返回其中的默认配对
        const STOCKS_DUAL_PAIRS_JS = {
            'w':   ['d', '30m', '5m'],
            'd':   ['30m', '5m'],
            '30m': ['5m'],
            // 5m 为股票最小周期，无下窗可选（与期货 15s 同语义）
        };

        // 校验股票双窗配对（P2：上窗须严格大于下窗且在配对空间内）
        function isValidStockDualPair(mainFreq, subFreq) {
            const subs = STOCKS_DUAL_PAIRS_JS[mainFreq];
            return !!(subs && subFreq && subs.indexOf(subFreq) >= 0);
        }

        // 双窗口：获取上面窗口某根K线对应的灰框边界（子级别K线时间字符串）
        // 通用方案：利用相邻K线时间，不依赖周期长度假设
        //   期货：K线时间=开始时间。左边界=当前时间X，右边界=(下一根时间Y - bottom_sec)
        //   股票：K线时间=结束时间。左边界=(上一根时间Y + bottom_sec)，右边界=当前时间X
        //         日期型K线（如d/w）无时分秒，解析时视为当日结束时刻(23:59:59)
        // 返回 {start: string|null, end: string|null}，null 表示边界在数据范围外
        function getMainKlineTimeRange(kline, idx, klines, isFutures, subFreq) {
            const subSec = FREQ_SEC_MAP_JS[subFreq];
            if (!subSec) return null;
            const dateLen = kline.date.length;  // 19=含秒, 16=含分, 10=仅日期
            function fmt(d) {
                const y = d.getFullYear();
                const mo = String(d.getMonth() + 1).padStart(2, '0');
                const da = String(d.getDate()).padStart(2, '0');
                if (dateLen >= 19) {
                    const h = String(d.getHours()).padStart(2, '0');
                    const mi = String(d.getMinutes()).padStart(2, '0');
                    const s = String(d.getSeconds()).padStart(2, '0');
                    return `${y}/${mo}/${da} ${h}:${mi}:${s}`;
                } else if (dateLen >= 16) {
                    const h = String(d.getHours()).padStart(2, '0');
                    const mi = String(d.getMinutes()).padStart(2, '0');
                    return `${y}/${mo}/${da} ${h}:${mi}`;
                }
                return `${y}/${mo}/${da}`;
            }
            function parse(ds) {
                // 日期型K线（仅日期）→ 视为当日结束时刻 23:59:59.999
                if (ds.length === 10) return new Date(ds.replace(/\//g, "-") + "T23:59:59");
                return new Date(ds.replace(/\//g, "-").replace(" ", "T"));
            }
            if (isFutures) {
                // 期货：左边界 = 当前K线时间X（精确匹配）
                const start = kline.date;
                // 右边界 = (下一根K线时间Y - sub_sec) 记为Z
                let end = null;
                if (idx + 1 < klines.length) {
                    const nextD = parse(klines[idx + 1].date);
                    const endD = new Date(nextD.getTime() - subSec * 1000);
                    end = fmt(endD);
                }
                return { start, end };
            } else {
                // 股票：左边界 = (上一根K线时间Y + sub_sec) 记为Z
                let start = null;
                if (idx > 0) {
                    const prevD = parse(klines[idx - 1].date);
                    const startD = new Date(prevD.getTime() + subSec * 1000);
                    start = fmt(startD);
                }
                // 右边界 = 当前K线时间X（精确匹配）
                const end = kline.date;
                return { start, end };
            }
        }

        // 双窗口：根据上面窗口鼠标位置计算下面窗口高亮范围
        function calcGrayRange(topMouseX) {
            if (!isDualWindow || !dualSubData || !chartData) return null;
            const area = getChartArea();
            const klines = getVisibleKlines();
            if (!klines.length) return null;
            const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
            const barStep = area.w / effectiveCount;
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            const idx = Math.floor((topMouseX - area.x + subPixelOffset) / barStep);
            if (idx < 0 || idx >= klines.length) return null;
            const mainKline = klines[idx];
            const subKlines = dualSubData.klines;
            let startIdx = -1, endIdx = -1;
            // 优先使用 sub_kl_times（后端多级别CChan返回的子级别K线时间列表）
            if (mainKline.sub_kl_times && mainKline.sub_kl_times.length > 0) {
                const subTimes = mainKline.sub_kl_times;
                const firstTime = subTimes[0];
                const lastTime = subTimes[subTimes.length - 1];
                for (let i = 0; i < subKlines.length; i++) {
                    const bk = subKlines[i];
                    if (bk.date >= firstTime && startIdx === -1) startIdx = i;
                    if (bk.date <= lastTime) endIdx = i;
                }
            } else {
                // 通用方案：利用相邻K线时间精确计算灰框边界
                const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
                const timeRange = getMainKlineTimeRange(mainKline, idx, klines, isFutures, dualSubFreq);
                if (!timeRange) return null;
                // 左边界：用 >= 匹配（字符串比较对 ISO 日期天然正确）
                if (timeRange.start) {
                    for (let i = 0; i < subKlines.length; i++) {
                        if (subKlines[i].date >= timeRange.start) { startIdx = i; break; }
                    }
                }
                // 右边界：用前缀匹配（兼容 d→30m 等跨格式场景），回退 <=
                if (timeRange.end) {
                    const endLen = timeRange.end.length;
                    for (let i = subKlines.length - 1; i >= 0; i--) {
                        if (subKlines[i].date.slice(0, endLen) === timeRange.end) { endIdx = i; break; }
                    }
                    if (endIdx === -1) {
                        for (let i = 0; i < subKlines.length; i++) {
                            if (subKlines[i].date <= timeRange.end) endIdx = i;
                        }
                    }
                }
                // 边界在数据范围外：用首/尾替代
                if (timeRange.start === null && startIdx === -1) startIdx = 0;
                if (timeRange.end === null && endIdx === -1) endIdx = subKlines.length - 1;
            }
            // 下面窗口数据中没有匹配的K线（上面K线日期超出了下面数据范围）
            if (startIdx === -1) {
                // 用上面K线日期与下面数据首尾日期比较来判断方向
                const topDate = new Date(mainKline.date.replace(/\//g, "-").replace(" ", "T"));
                const subFirstDate = new Date(subKlines[0].date.replace(/\//g, "-").replace(" ", "T"));
                const subLastDate = new Date(subKlines[subKlines.length - 1].date.replace(/\//g, "-").replace(" ", "T"));
                if (topDate < subFirstDate) {
                    return { startIdx: -1, endIdx: -1, isVisible: false, isLeft: true, isRight: false };
                } else if (topDate > subLastDate) {
                    return { startIdx: -1, endIdx: -1, isVisible: false, isLeft: false, isRight: true };
                }
                return null;
            }
            // 判断高亮范围是否在下面窗口当前视口内
            const subGlobalStart = Math.max(0, Math.floor(dualSubViewOffset));
            const subGlobalEnd = subGlobalStart + dualSubViewCount;
            const isVisible = (startIdx < subGlobalEnd && endIdx >= subGlobalStart);
            const isLeft = endIdx < subGlobalStart;   // 整个区间在视口左边
            const isRight = startIdx >= subGlobalEnd;
            let redRange = null;
            if (_ctrlPressed) {
                try {
                    redRange = calcRedRange(mainKline, subKlines, startIdx, endIdx);
                } catch (e) {
                    console.error("[红框] calcRedRange异常:", e);
                    window._lastCalcRedRangeError = String(e);
                }
            }
            return { startIdx, endIdx, isVisible, isLeft, isRight, redRange };
        }

        // 双窗口红框：鼠标指向上面K线所属笔的外沿区间（分型左肩→右肩）
        // 注意：使用 chartData.bis（复数），JSON 字段名是 "bis"
        function calcRedRange(mainKline, subKlines, grayStart, grayEnd) {
            if (!chartData || !chartData.bis || !chartData.bis.length) {
                window._lastRedFrameStatus = { state: "SKIP", reason: "chartData或bis为空" };
                updateRedFrameDebug();
                return null;
            }
            const d = mainKline.date;
            let bi = null;
            // 找到mainKline所属的笔（交界处归属右边）
            for (let i = 0; i < chartData.bis.length; i++) {
                const b = chartData.bis[i];
            if (d >= b.sdt && d < b.edt) { bi = b; break; }
            }
            if (!bi) {
                for (let i = chartData.bis.length - 1; i >= 0; i--) {
            if (d === chartData.bis[i].edt) { bi = chartData.bis[i]; break; }
                }
            }
            if (!bi) {
                window._lastRedFrameStatus = { state: "SKIP", reason: "未找到所属笔", topDate: d, biCount: chartData.bis.length };
                updateRedFrameDebug();
                return null;
            }
            const aDt = bi.fx_a_sub_dt || bi.fx_a_raw_dt, bDt = bi.fx_b_sub_dt || bi.fx_b_raw_dt;
            if (!aDt || !bDt) {
                window._lastRedFrameStatus = { state: "SKIP", reason: "fx_a或fx_b为空", sdt: bi.sdt, edt: bi.edt };
                updateRedFrameDebug();
                return null;
            }
            // fx_a_sub_dt / fx_b_sub_dt 是后端从分型原始K线对应的次级别序列边界直接算出的
            // 双窗口下直接就是次级别K线时间，>= / <= 精确匹配即可
            const aLen = aDt.length, bLen = bDt.length;
            let aIdx = -1, bIdx = -1;
            const subFirstDate = subKlines[0].date.slice(0, aLen);
            const subLastDate = subKlines[subKlines.length - 1].date.slice(0, bLen);
            for (let i = 0; i < subKlines.length; i++) {
                const bk = subKlines[i];
                // A: 红框左边界（次级别第一根）
                if (aIdx === -1 && bk.date.slice(0, aLen) >= aDt) aIdx = i;
                // B: 红框右边界（次级别最后一根）
                if (bk.date.slice(0, bLen) <= bDt) bIdx = i;
            }
            // 参照灰框处理：笔区间完全在底部数据范围之外 → 不显示红框，返回null
            if (aIdx === -1 && bIdx === -1) {
                if (aDt > subLastDate) {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间在底部数据右侧", aDt: aDt, bottomLast: subLastDate };
                } else if (bDt < subFirstDate) {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间在底部数据左侧", bDt: bDt, bottomFirst: subFirstDate };
                } else {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间无匹配", aDt: aDt, bDt: bDt };
                }
                updateRedFrameDebug();
                return null;
            }
            // 部分重叠：aIdx 或 bIdx 为 -1 时，截断到可见范围
            if (aIdx === -1) aIdx = 0;
            if (bIdx === -1) bIdx = subKlines.length - 1;
            if (aIdx > bIdx) {
                window._lastRedFrameStatus = { state: "SKIP", reason: "aIdx>bIdx", aIdx: aIdx, bIdx: bIdx };
                updateRedFrameDebug();
                return null;
            }
            // 红框时间：使用下方K线时间（精确到分钟），确保30m/5m图表显示完整时间
            const leftDate = subKlines[aIdx].date;
            const rightDate = subKlines[bIdx].date;
            // before: 笔区间在灰框之前的部分 [aIdx, grayStart-1]
            const beforeStart = aIdx, beforeEnd = Math.min(grayStart - 1, bIdx);
            // after: 笔区间在灰框之后的部分 [grayEnd+1, bIdx]
            const afterStart = grayEnd + 1, afterEnd = bIdx;
            const result = {
                beforeStart, beforeEnd,
                afterStart, afterEnd,
                hasBefore: (beforeEnd >= beforeStart),
                hasAfter: (afterEnd >= afterStart),
                leftDate: leftDate,    // 红框左边沿K线时间（下方窗口，精确到分钟）
                rightDate: rightDate,  // 红框右边沿K线时间
                aIdx: aIdx,            // 红框整体左边界（下方窗口全局索引）
                bIdx: bIdx,            // 红框整体右边界（下方窗口全局索引）
            };
            window._lastRedFrameStatus = { state: "OK", reason: "calcRedRange成功", before: result.hasBefore, after: result.hasAfter, aIdx: aIdx, bIdx: bIdx, grayStart: grayStart, grayEnd: grayEnd, leftDate: result.leftDate, rightDate: result.rightDate };
            updateRedFrameDebug();
            return result;
        }

        function initCanvas() {
            const container = document.getElementById("chart-container");
            canvas = document.createElement("canvas");
            container.appendChild(canvas); ctx = canvas.getContext("2d");
            mainCanvas = canvas; mainCtx = ctx;
            resizeCanvas();
            // 立即用背景色填充 canvas，防止加载期间透出浏览器默认白底
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const dpr = window.devicePixelRatio || 1;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.fillStyle = COLORS.bg;
            ctx.fillRect(0, 0, w, h);
            window.addEventListener("resize", () => { resizeCanvas(); render(); });
            // 上面窗口事件
            canvas.addEventListener("wheel", onWheel, { passive: false });
            canvas.addEventListener("mousedown", onMouseDown);
            canvas.addEventListener("mousemove", onMouseMove);
            canvas.addEventListener("mouseup", onMouseUp);
            canvas.addEventListener("mouseleave", onMouseLeave);
            canvas.addEventListener("contextmenu", onContextMenu);
            canvas.addEventListener("dblclick", function(e) {
                if (!chartData) return;
                const rect = canvas.getBoundingClientRect();
                const clickX = e.clientX - rect.left;
                const clickY = e.clientY - rect.top;
                const area = getChartArea();
                const volArea = getVolArea();
                const macdTextArea = getMacdTextArea();
                // 0. 底部区域（MACD/成交额标签+图表区）双击切换显示模式
                const bottomTop = macdTextArea.y;
                const bottomBottom = volArea.y + volArea.h;
                if (clickX >= area.x && clickX <= area.x + area.w &&
                    clickY >= bottomTop && clickY <= bottomBottom) {
                    _showVolume = !_showVolume;
                    saveOverlaySettings();
                    render();
                    return;
                }
                // 1. 只在K线主图区域内有效
                if (clickX < area.x || clickX > area.x + area.w ||
                    clickY < area.y || clickY > area.y + area.h) {
                    return;
                }
                // 2. 计算当前可见K线和参数
                const klines = getVisibleKlines();
                if (!klines.length) return;
                const priceRange = getPriceRange(klines);
                const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
                const barStep = area.w / effectiveCount;
                const barWidth = Math.max(1, barStep * 0.7);
                const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
                // 3. 检查是否落在任何K线的[high,low]矩形内，同时检查是否是笔交汇点（分型）
                let clickedOnKline = false;
                let clickedBiIdx = -1;
                for (let i = 0; i < klines.length; i++) {
                    const k = klines[i];
                    const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                    const highY = priceToY(k.high, area, priceRange);
                    const lowY = priceToY(k.low, area, priceRange);
                    const halfW = barWidth / 2;
                    if (clickX >= x - halfW && clickX <= x + halfW &&
                        clickY >= highY && clickY <= lowY) {
                        clickedOnKline = true;
                        // 通过笔数据判断交汇点：双击K线日期 == 某笔edt == 下一笔sdt
                        const globalStart = Math.max(0, Math.floor(viewOffset));
                        const globalIdx = globalStart + i;
                        const kline = chartData.klines[globalIdx];
                        if (kline) {
                            let dateStr = kline.date;
                            for (let j = 0; j < chartData.bis.length - 1; j++) {
                                if (chartData.bis[j].edt === dateStr && chartData.bis[j + 1].sdt === dateStr) {
                                    clickedBiIdx = j + 1;
                                    break;
                                }
                            }
                        }
                        break;
                    }
                }
                // 复盘模式下不支持双击选点（在K线检测之后判断，确保只对K线上的双击弹提示）
                if (chartData.meta && chartData.meta.is_replay && clickedOnKline) {
                    showDualToast("复盘模式，不支持选点");
                    return;
                }
                // 双窗选点规则（股票/期货一致）：仅上窗可选点，下窗只对齐展示。
                // 上窗选点 → 后端保存T → 重连双窗SSE带 start_time=T（下窗自动对齐 [T, 最新]）
                // 4. 如果双击落在分型K线上且找到对应笔，手选进入段
                if (clickedBiIdx >= 0) {
                    const code = chartData.meta.symbol;
                    const freq = currentFreq;
                    const isFutures = chartData.meta.market === 'futures';
                    document.getElementById("loading").classList.remove("hidden");
                    document.querySelector(".loading-text").textContent = "正在手选进入段...";
                    // 股票双窗选点：上窗选点带双窗上下文，
                    // 后端销毁双窗两键缓存并按双窗路径重建（响应含 data.sub）
                    const dualQuery = (isDualWindow && !isFutures)
                        ? "&dual=1&main_freq=" + currentFreq + "&sub_freq=" + dualSubFreq : "";
                    const apiPath = isFutures
                        ? "/api/futures/" + encodeURIComponent(code) + "/select/point?freq=" + freq + "&bi_idx=" + clickedBiIdx
                : "/api/stocks/" + encodeURIComponent(code) + "/select/point?freq=" + freq + "&bi_idx=" + clickedBiIdx + dualQuery;
                    fetch(apiPath, { method: "POST" })
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "手选失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            // 检查后端返回的错误
                            if (data.error) {
                                throw new Error(data.error);
                            }
                            // 期货：断开旧SSE，从选点时间重新连接
                            if (isFutures) {
                                const savedDate = data.meta && data.meta.saved_selection_date;
                                // 双窗模式：上窗选点已保存 → 重连双窗SSE带 start_time=T，
                                // 下窗由后端自动对齐 [T, 最新]（下窗对齐上窗语义），
                                // 初始快照（含上下窗）由 SSE init 事件统一推送，
                                // 不在此处用单窗响应覆盖 chartData/dualSubData
                                if (isDualWindow && dualSubFreq) {
                                    document.querySelector(".loading-text").textContent = "正在加载双窗口数据...";
                                    connectRealtimeDual(code, freq, dualSubFreq, null, savedDate);
                                    return;
                                }
                                chartData = data;
                                adjustViewForSavedPoint();
                                document.getElementById("stock-name").textContent = chartData.meta.name;
                                document.getElementById("stock-code").textContent = chartData.meta.symbol;
                                document.title = "缠论分析 - " + chartData.meta.name;
                                if (chartData.klines.length > 0) {
                                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                                    document.getElementById("goto-date-input").value = lastDate;
                                }
                                updateWeekday();
                                document.getElementById("loading").classList.add("hidden");
                                document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                                updateRestartBtn();
                                updateDualBtn();
                                resizeCanvas();
                                render();
                                generateStats();
                                loadAnnotations();
                                // 重连SSE，带上选点时间（savedDate 已在上方取自 data.meta）
                                connectRealtimeInit(code, freq, savedDate);
                                return;
                            }
                            // data 现在是完整的 chartData JSON（CChanB 从T重新计算的结果）
                            // 全文替换 chartData
                            chartData = data;
                            // 根据数据中的 freq 自动识别周期
                            if (chartData.meta.freq === "5分钟") {
                                currentFreq = "5m";
                            } else if (chartData.meta.freq === "30分钟") {
                                currentFreq = "30m";
                            } else if (chartData.meta.freq === "周线") {
                                currentFreq = "w";
                            } else {
                                currentFreq = "d";
                            }
                            updateDateInputType();
                            // 同步按钮状态
                            document.getElementById("btn-d").classList.toggle("active", currentFreq === "d");
                            document.getElementById("btn-w").classList.toggle("active", currentFreq === "w");
                            document.getElementById("btn-30m").classList.toggle("active", currentFreq === "30m");
                            document.getElementById("btn-5m").classList.toggle("active", currentFreq === "5m");
                            // 重置视图：选点后klines只含选点之后的K线，直接全部显示
                            adjustViewForSavedPoint();
                            // 双窗（用户逻辑⑵⓶）：同步下窗数据与视图——
                            // 下窗对齐上窗 [选点, 最新] 区间加载，视口无 377
                            // 限制：下窗后端加载多少根，前端视口就显示多少根
                            // （与上窗 adjustViewForSavedPoint 全量显示规则一致；
                            //   A/C 操作的下窗仍走 VIEW_COUNT 377 视口，见别处）
                            if (isDualWindow && data.sub) {
                                dualSubData = data.sub;
                                dualSubViewCount = dualSubData.klines.length;
                                dualSubViewOffset = 0;
                                updateFreqButtonStates(false);
                            }
                            // 更新DOM
                            document.getElementById("stock-name").textContent = chartData.meta.name;
                            document.getElementById("stock-code").textContent = chartData.meta.symbol;
                            document.title = "缠论分析 - " + chartData.meta.name;
                            const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                            document.getElementById("goto-date-input").value = lastDate;
                            updateWeekday();
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                            updateRestartBtn();
                            updateDualBtn();
                            resizeCanvas();
                            render();
                            generateStats();
                            loadAnnotations();
                        })
                        .catch(err => {
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                            setTimeout(() => {
                                alert(err.message);
                            }, 50);
                        });
                    return;
                }
                // 5. 如果双击落在K线上但不是分型，无效
                if (clickedOnKline) {
                    return;
                }
                // 6. 双击空白处
                if (isDualWindow && dualOffscreenState && dualHighlightRange && dualSubData) {
                    // 状态A：让下面窗口平移到对应区间
                    const hr = dualHighlightRange;
                    if (hr.startIdx >= 0 && hr.endIdx >= 0) {
                        const centerIdx = (hr.startIdx + hr.endIdx) / 2;
                        const totalKlines = dualSubData.klines.length;
                        let newOffset = Math.round(centerIdx - dualSubViewCount / 2);
                        // 左边不够：左对齐
                        if (newOffset < 0) newOffset = 0;
                        // 右边不够：右对齐（最后一根K线贴右边缘）
                        const maxOffset = Math.max(0, totalKlines - dualSubViewCount);
                        if (newOffset > maxOffset) newOffset = maxOffset;
                        dualSubViewOffset = newOffset;
                        // 重新计算高亮范围（区间已移入视口，应该变为isVisible=true）
                        dualHighlightRange = calcGrayRange(mouseX);
                        dualRedRange = dualHighlightRange ? dualHighlightRange.redRange : null;
                        dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                        renderBottom();
                    } else {
                        // startIdx === -1（下面窗口无对应K线数据）
                        showDualToast("请加载更多K线...");
                    }
                    return;
                }
                // 7. 默认：恢复全视图
                viewCount = VIEW_COUNT;
                viewOffset = Math.max(0, chartData.klines.length - viewCount);
                const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                document.getElementById("goto-date-input").value = lastDate;
                updateWeekday();
                render();
            });
        }

        function resizeCanvas() {
            const container = document.getElementById("chart-container");
            const dpr = window.devicePixelRatio || 1;
            if (isDualWindow) {
                // 双窗口模式：分别调整两个canvas
                const w = container.clientWidth;
                const hTop = container.clientHeight / 2;
                const hBottom = container.clientHeight / 2;
                if (mainCanvas) {
                    mainCanvas.width = w * dpr; mainCanvas.height = hTop * dpr;
                    mainCanvas.style.width = w + "px"; mainCanvas.style.height = hTop + "px";
                    mainCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
                    mainCtx.fillStyle = COLORS.bg; mainCtx.fillRect(0, 0, w, hTop);
                }
                if (subCanvas) {
                    subCanvas.width = w * dpr; subCanvas.height = hBottom * dpr;
                    subCanvas.style.width = w + "px"; subCanvas.style.height = hBottom + "px";
                    subCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
                    subCtx.fillStyle = COLORS.bg; subCtx.fillRect(0, 0, w, hBottom);
                }
            } else {
                // 单窗口模式
                const w = container.clientWidth, h = container.clientHeight;
                canvas.width = w * dpr; canvas.height = h * dpr;
                canvas.style.width = w + "px"; canvas.style.height = h + "px";
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
                ctx.fillStyle = COLORS.bg; ctx.fillRect(0, 0, w, h);
            }
        }

        function getChartArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top, w: totalW - rightGap, h: chartH };
        }

        function getVolArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalMacdH = (h - PADDING.top - PADDING.bottom - GAP) * VOL_RATIO;
            const macdChartH = totalMacdH - MACD_TEXT_HEIGHT;
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top + chartH + MACD_TEXT_HEIGHT,
                     w: totalW - rightGap, h: macdChartH };
        }

        function getMacdTextArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top + chartH,
                     w: totalW - rightGap, h: MACD_TEXT_HEIGHT };
        }

        function getVisibleKlines() {
            if (!chartData) return [];
            const start = Math.max(0, Math.floor(viewOffset));
            const end = Math.min(chartData.klines.length, start + viewCount + 2);
            const result = chartData.klines.slice(start, end);
            // 周K：返回全部K线，确保铺满整个画布
            if (currentFreq === 'w' && result.length < viewCount) {
                return result;
            }
            return result;
        }

        function getPriceRange(klines) {
            if (!klines.length) return { min: 0, max: 100 };
            let min = Infinity, max = -Infinity;
            klines.forEach(k => { if (k.low < min) min = k.low; if (k.high > max) max = k.high; });
            // 对数坐标系（翻转视图不改变价格范围，镜像与非镜像共用同一逻辑）：
            //   全正 → log空间计算margin；存在非正价（前复权）→ 回退线性，避免log(<=0)无定义
            if (_logScale) {
                if (min <= 0) {
                    const margin = (max - min) * 0.05;
                    return { min: min - margin, max: max + margin };
                }
                const logMin = Math.log(min);
                const logMax = Math.log(max);
                const logMargin = (logMax - logMin) * 0.05;
                return { min: Math.exp(logMin - logMargin), max: Math.exp(logMax + logMargin) };
            }
            const margin = (max - min) * 0.05;
            return { min: min - margin, max: max + margin };
        }

        function getMacdRange(klines) {
            if (!klines.length) return { min: -1, max: 1 };
            let min = Infinity, max = -Infinity;
            klines.forEach(k => {
                if (k.macd < min) min = k.macd;
                if (k.macd > max) max = k.macd;
                if (k.dif < min) min = k.dif;
                if (k.dif > max) max = k.dif;
                if (k.dea < min) min = k.dea;
                if (k.dea > max) max = k.dea;
            });
            // 全为0时兜底，避免下游 drawMacd/drawMacdAxis 除以零
            if (min === 0 && max === 0) return { min: -1, max: 1 };
            const margin = Math.max(Math.abs(max), Math.abs(min)) * 0.1;
            return { min: min - margin, max: max + margin };
        }

        // 期货显示成交量(vol)，股票显示成交额(amount)——天勤K线无成交额字段，故期货改用成交量
        function isFuturesMode() {
            return !!(chartData && chartData.meta && chartData.meta.market === 'futures');
        }

        // 取底部柱状指标值：期货=成交量(vol)，股票=成交额(amount)
        function getVolMetric(k) {
            if (!k) return 0;
            return isFuturesMode() ? (k.vol || 0) : (k.amount || 0);
        }

        // 底部指标标签：期货"成交量(手)"，股票"成交额"
        function getVolLabel() {
            return isFuturesMode() ? "成交量(手)" : "成交额";
        }

        function getVolumeRange(klines) {
            if (!klines.length) return { min: 0, max: 1 };
            let max = 0;
            klines.forEach(k => { const v = getVolMetric(k); if (v > max) max = v; });
            // 全为0时兜底，避免底部柱状区域空白
            if (max === 0) return { min: 0, max: 1 };
            return { min: 0, max: max * 1.05 };
        }

        // _mirrorChartData 已废弃：翻转视图改为纯视图变换（priceToY 翻转Y轴），
        // 不再对数据取负，前复权负价原样保留显示。颜色/MACD/方向翻转由各 draw 函数显式处理。

        // 价格显示：原样输出（含负号），翻转模式下不取绝对值
        function _fmtPrice(p) {
            return p.toFixed(2);
        }

        function priceToY(price, area, priceRange) {
            // 翻转视图：仅翻转Y轴方向，价格原值参与计算（含前复权负价）。
            // 对数模式要求 priceRange.min>0（由 getPriceRange 保证：有非正价时回退线性）。
            if (_logScale && priceRange.min > 0) {
                const logMin = Math.log(priceRange.min);
                const logMax = Math.log(priceRange.max);
                const logPrice = Math.log(price);
                const ratio = (logPrice - logMin) / (logMax - logMin);
                return _isMirrorMode ? area.y + ratio * area.h : area.y + area.h - ratio * area.h;
            }
            const ratio = (price - priceRange.min) / (priceRange.max - priceRange.min);
            return _isMirrorMode ? area.y + ratio * area.h : area.y + area.h - ratio * area.h;
        }

        function yToPrice(y, area, priceRange) {
            if (_logScale && priceRange.min > 0) {
                const logMin = Math.log(priceRange.min);
                const logMax = Math.log(priceRange.max);
                const ratio = _isMirrorMode ? (y - area.y) / area.h : (area.y + area.h - y) / area.h;
                return Math.exp(logMin + ratio * (logMax - logMin));
            }
            const ratio = _isMirrorMode ? (y - area.y) / area.h : (area.y + area.h - y) / area.h;
            return priceRange.min + ratio * (priceRange.max - priceRange.min);
        }

        /**
         * 构建全局日期→全局索引映射（chartData.klines 级别）。
         * 所有需要通过日期查找K线索引的 draw 函数统一使用此映射，
         * 避免因视口滚动导致局部 klines 子数组中找不到日期而丢失绘制。
         */
        function buildGlobalDateMap() {
            const dateToGlobalIdx = {};
            chartData.klines.forEach((k, i) => { dateToGlobalIdx[k.date] = i; });
            return { dateToGlobalIdx };
        }

        /**
         * 通过日期查找全局索引。
         * @param {string} date - 日期字符串
         * @param {object} map - buildGlobalDateMap() 的返回值
         * @returns {number|undefined} 全局索引
         */
        function dateToGlobalIdx(date, map) {
            const result = map.dateToGlobalIdx[date];
            if (result === undefined && window._dualZsDebugCount === undefined) {
                window._dualZsDebugCount = 0;
            }
            if (result === undefined && window._dualZsDebugCount < 3) {
                console.log("[dateToGlobalIdx] 未匹配日期: '" + date + "', 可用日期样本: " + Object.keys(map.dateToGlobalIdx).slice(0, 3).join(", "));
                window._dualZsDebugCount++;
            }
            return result;
        }

        /**
         * 将全局索引转换为画布上的 X 坐标。
         * @param {number} globalIdx - 在 chartData.klines 中的全局索引
         * @param {number} globalStart - 当前视口起始的全局索引
         * @param {number} areaX - 图表区域左边界
         * @param {number} barStep - 每根K线的像素步长
         * @param {number} subPixelOffset - 亚像素偏移
         * @returns {number} 画布 X 坐标
         */
        function globalIdxToX(globalIdx, globalStart, areaX, barStep, subPixelOffset) {
            const localIdx = globalIdx - globalStart;
            return areaX + barStep * localIdx + barStep / 2 - subPixelOffset;
        }

        function render() {
            if (!chartData) return;
            if (isDualWindow) {
                alignDualSubViewport(); // 双窗：下窗视口对齐上窗当前视口时间范围
                renderTop(); // renderTop内部会调用updateDualHighlight -> renderBottom
            } else {
                renderSingle();
            }
        }

        function renderSingle() {
            if (!chartData || !ctx) return;
            canvas = mainCanvas; ctx = mainCtx;
            _renderChart(chartData, currentFreq, viewOffset, viewCount, mouseX, mouseY, null, null);
        }

        function renderTop() {
            if (!chartData || !mainCtx) return;
            canvas = mainCanvas; ctx = mainCtx;
            updateActiveWindowClass();
            _renderChart(chartData, currentFreq, viewOffset, viewCount, mouseX, mouseY, null, null);
            // 上面窗口渲染完后，计算下面窗口高亮并重绘下面窗口
            // 注意：_renderChart 内部会临时覆盖全局变量然后恢复，
            // 所以这里全局变量已恢复为上面窗口的值，calcGrayRange 可以正确使用
            updateDualHighlight();
        }

        function renderBottom() {
            if (!dualSubData || !subCtx) return;
            updateDualNewZs();  // 双窗口新模式：检查红框完整性，决定是否请求新中枢
            updateActiveWindowClass();
            const _savedCanvas = canvas, _savedCtx = ctx;
            canvas = subCanvas; ctx = subCtx;
            window._isRenderingBottom = true;  // 标记：下面窗口渲染中，drawCrosshair 不更新 OHLC
            _renderChart(dualSubData, dualSubFreq, dualSubViewOffset, dualSubViewCount, dualSubMouseX, dualSubMouseY, dualHighlightRange, dualRedRange);
            window._isRenderingBottom = false;
            canvas = _savedCanvas; ctx = _savedCtx;
        }

        function _renderChart(data, freq, vOffset, vCount, mX, mY, highlightRange, redRange) {
            if (!data || !ctx) return;
            // 翻转视图：纯视图变换（Y轴方向翻转），不修改数据。
            // 负价（前复权）原样保留；K线/成交量/MACD/中枢/买卖点的颜色与方向翻转
            // 由各 draw 函数依据 _isMirrorMode 显式处理。
            // 临时覆盖全局变量供绘制函数使用
            const _savedViewOffset = viewOffset, _savedViewCount = viewCount;
            const _savedMouseX = mouseX, _savedMouseY = mouseY;
            const _savedCurrentFreq = currentFreq;
            const _savedChartData = chartData;
            const _savedShowVolume = _showVolume;
            viewOffset = vOffset; viewCount = vCount;
            mouseX = mX; mouseY = mY;
            currentFreq = freq;
            chartData = data;
            // 双窗口模式：下窗使用独立的 _subShowVolume，不与上窗联动
            if (data === dualSubData) _showVolume = _subShowVolume;
            const w = canvas.clientWidth, h = canvas.clientHeight;
            ctx.fillStyle = COLORS.bg; ctx.fillRect(0, 0, w, h);
            const klines = getVisibleKlines();
            if (!klines.length) {
                viewOffset = _savedViewOffset; viewCount = _savedViewCount;
                mouseX = _savedMouseX; mouseY = _savedMouseY;
                currentFreq = _savedCurrentFreq;
                chartData = _savedChartData;
                _showVolume = _savedShowVolume;
                return;
            }
            const area = getChartArea(), volArea = getVolArea();
            const macdTextArea = getMacdTextArea();
            const priceRange = getPriceRange(klines), macdRange = getMacdRange(klines), volRange = getVolumeRange(klines);
            const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
            const barWidth = Math.max(1, (area.w / effectiveCount) * 0.7);
            const barStep = area.w / effectiveCount;
            const MACD_BAR_WIDTH = Math.max(3, barStep * 0.5);  // MACD红绿柱宽度随K线间距缩放（原固定2px）
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            // 双窗口红框：笔外沿区间（分型左肩→右肩，跳过中间灰框部分）
            // 调试：记录到全局状态供侧边调试面板读取（保留 calcRedRange 之前设置的原因）
            var _prevReason = window._lastRedFrameStatus ? window._lastRedFrameStatus.reason : undefined;
            window._lastRedFrameStatus = { redRange: !!redRange, highlightRange: !!highlightRange, isVisible: highlightRange ? highlightRange.isVisible : null };
            if (_prevReason) window._lastRedFrameStatus.reason = _prevReason;
            updateRedFrameDebug();
            if (redRange && highlightRange && highlightRange.isVisible) {
                window._lastRedFrameStatus.state = "DRAW";
                window._lastRedFrameStatus.leftDate = redRange.leftDate || "";
                window._lastRedFrameStatus.rightDate = redRange.rightDate || "";
                const globalStart = Math.max(0, Math.floor(viewOffset));
                const rFill = "rgba(220, 50, 50, 0.12)";  // 与红中枢同色
                if (redRange.hasBefore) {
                    const bx1 = globalIdxToX(redRange.beforeStart, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                    const bx2 = globalIdxToX(redRange.beforeEnd, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                    ctx.fillStyle = rFill; ctx.fillRect(bx1, area.y, bx2 - bx1, area.h);
                    window._lastRedFrameStatus.beforeDrawn = true;
                    window._lastRedFrameStatus.beforeRect = [bx1.toFixed(0), bx2.toFixed(0)];
                }
                if (redRange.hasAfter) {
                    const ax1 = globalIdxToX(redRange.afterStart, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                    const ax2 = globalIdxToX(redRange.afterEnd, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                    ctx.fillStyle = rFill; ctx.fillRect(ax1, area.y, ax2 - ax1, area.h);
                    window._lastRedFrameStatus.afterDrawn = true;
                    window._lastRedFrameStatus.afterRect = [ax1.toFixed(0), ax2.toFixed(0)];
                }
                updateRedFrameDebug();
            } else {
                // 保留 calcRedRange 给出的原因（如果有），不覆盖
                if (!window._lastRedFrameStatus || !window._lastRedFrameStatus.reason) {
                    window._lastRedFrameStatus = window._lastRedFrameStatus || {};
                    window._lastRedFrameStatus.reason = "渲染跳过(redRange或visibility)";
                }
                window._lastRedFrameStatus.state = "SKIP";
                window._lastRedFrameStatus.redRange = !!redRange;
                window._lastRedFrameStatus.highlightRange = !!highlightRange;
                window._lastRedFrameStatus.isVisible = highlightRange ? highlightRange.isVisible : null;
                updateRedFrameDebug();
            }
            // 双窗口高亮：在绘制K线之前先画灰色背景
            let offscreenIndicator = null; // {isLeft, isRight} 用于最后画箭头
            let highlightCenterDate = null; // 灰框中间K线的日期
            if (highlightRange && highlightRange.startIdx !== undefined) {
                if (highlightRange.isVisible) {
                    const globalStart = Math.max(0, Math.floor(viewOffset));
                    const hStartX = globalIdxToX(highlightRange.startIdx, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                    const hEndX = globalIdxToX(highlightRange.endIdx, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                    ctx.fillStyle = "rgba(128, 128, 128, 0.35)";
                    ctx.fillRect(hStartX, area.y, hEndX - hStartX, area.h);
                    // 画灰框中间的白色纵线
                    const centerIdx = Math.round((highlightRange.startIdx + highlightRange.endIdx) / 2);
                    const centerKline = data.klines[centerIdx];
                    if (centerKline) {
                        highlightCenterDate = centerKline.date;
                        const centerX = globalIdxToX(centerIdx, globalStart, area.x, barStep, subPixelOffset);
                        ctx.strokeStyle = "rgba(255, 255, 255, 0.5)";
                        ctx.lineWidth = 1;
                        ctx.setLineDash([4, 3]);
                        ctx.beginPath();
                        ctx.moveTo(centerX, area.y);
                        ctx.lineTo(centerX, area.y + area.h);
                        ctx.stroke();
                        ctx.setLineDash([]);
                    }
                } else if (highlightRange.isLeft || highlightRange.isRight) {
                    offscreenIndicator = { isLeft: highlightRange.isLeft, isRight: highlightRange.isRight };
                }
            }
            drawGrid(area, priceRange);
            ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(area.x + area.w, area.y);
            ctx.lineTo(area.x + area.w, volArea.y + volArea.h);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(area.x, area.y);
            ctx.lineTo(area.x, volArea.y + volArea.h);
            ctx.stroke();
            const klinesToDraw = klines.slice(0, viewCount);
            drawMacdLabel(macdTextArea, klinesToDraw, barStep, subPixelOffset);
            if (_showVolume) {
                drawVolume(klinesToDraw, volArea, volRange, barStep, barWidth, subPixelOffset);
            } else {
                drawMacd(klinesToDraw, volArea, macdRange, barStep, MACD_BAR_WIDTH, subPixelOffset);
            }
            // 区间选择高亮：绘制起点A的金色标记
            if (_rangeSelect.mode === 'SELECTED_A' && _rangeSelect.startFreq === currentFreq && chartData && _rangeSelect.startSymbol === chartData.meta.symbol) {
                const selIdx = _rangeSelect.startIdx;
                const globalStart = Math.max(0, Math.floor(viewOffset));
                const selX = globalIdxToX(selIdx, globalStart, area.x, barStep, subPixelOffset);
                if (selX >= area.x - barStep && selX <= area.x + area.w + barStep) {
                    const selX1 = selX - barStep / 2;
                    const selX2 = selX + barStep / 2;
                    ctx.fillStyle = "rgba(255, 215, 0, 0.22)";
                    ctx.fillRect(selX1, area.y, selX2 - selX1, area.h);
                    ctx.strokeStyle = "rgba(255, 215, 0, 0.7)";
                    ctx.lineWidth = 1.5;
                    ctx.strokeRect(selX1, area.y, selX2 - selX1, area.h);
                    // 顶部标签
                    const selK = data.klines[selIdx];
                    if (selK) {
                        const label = "A";
                        ctx.font = "bold 11px monospace";
                        ctx.fillStyle = "rgba(0,0,0,0.75)";
                        ctx.fillRect(selX - 8, area.y - 18, 16, 16);
                        ctx.fillStyle = "#FFD700";
                        ctx.textAlign = "center";
                        ctx.fillText(label, selX, area.y - 6);
                    }
                }
            }
            drawCandles(klinesToDraw, area, priceRange, barStep, barWidth, subPixelOffset);
            if (getShowMa()) {
                try { drawMaLines(klinesToDraw, area, priceRange, barStep, subPixelOffset); }
                catch (e) { console.error("[drawMaLines错误]", e); }
            }
            if (showBi) drawBiLines(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showFx) drawFxMarkers(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            // 双窗口新模式：红框出现后立即进入新中枢模式。
            // 请求返回前也先隐藏原中枢/线段/买卖点，避免红框出现后仍显示旧结构。
            const isSubNewZs = (data === dualSubData && dualShowNewZs);
            if (showZs && !isSubNewZs) drawZs(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showSeg && !isSubNewZs) drawSegLines(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showBsp && !isSubNewZs) drawBspMarkers(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (isSubNewZs) drawDualNewZs(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawWhiteHLine(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawAnnotations(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawViewportHighLow(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            _overlayData = null;
            drawCrosshair(klinesToDraw, area, priceRange, volArea, _showVolume ? volRange : macdRange, barStep, macdTextArea, subPixelOffset);
            drawPriceAxis(area, priceRange);
            if (_showVolume) {
                drawVolumeAxis(volArea, volRange);
            } else {
                drawMacdAxis(volArea, macdRange);
            }
            drawDateAxis(klinesToDraw, barStep, subPixelOffset);
            drawCountdownBar(area);
            _drawOverlayIfNeeded(_overlayData, area);
            // 双窗口：在所有绘制完成后，画视口外指示箭头（确保不被覆盖）
            if (offscreenIndicator) {
                const arrowSize = 10;
                const arrowY = area.y + area.h / 2;
                ctx.fillStyle = "rgba(200, 200, 200, 0.6)";
                ctx.beginPath();
                if (offscreenIndicator.isLeft) {
                    ctx.moveTo(area.x + arrowSize + 4, arrowY - arrowSize);
                    ctx.lineTo(area.x + 4, arrowY);
                    ctx.lineTo(area.x + arrowSize + 4, arrowY + arrowSize);
                } else {
                    ctx.moveTo(area.x + area.w - arrowSize - 4, arrowY - arrowSize);
                    ctx.lineTo(area.x + area.w - 4, arrowY);
                    ctx.lineTo(area.x + area.w - arrowSize - 4, arrowY + arrowSize);
                }
                ctx.closePath();
                ctx.fill();
            }
            // 双窗口高亮：在灰框中间白线下方显示日期标签（同drawCrosshair完整信息）
            if (highlightCenterDate && highlightRange && highlightRange.isVisible) {
                const globalStart = Math.max(0, Math.floor(viewOffset));
                const centerIdx = Math.round((highlightRange.startIdx + highlightRange.endIdx) / 2);
                const centerX = globalIdxToX(centerIdx, globalStart, area.x, barStep, subPixelOffset);
                const centerKline = data.klines[centerIdx];
                if (centerKline) {
                    // 格式化日期
                    let shortDate;
                    if (freq === '15s') {
                        shortDate = getKlineEndTime(highlightCenterDate, true);
                    } else if (freq === '1m' || freq === '30m' || freq === '5m') {
                        shortDate = getKlineEndTime(highlightCenterDate);
                    } else if (freq === 'w') {
                        const dateParts = highlightCenterDate.split(/[-\/]/);
                        shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                    } else {
                        const dateParts = highlightCenterDate.split(/[-\/]/);
                        shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                    }
                    const d = new Date(highlightCenterDate.replace(/\//g, "-").replace(" ", "T"));
                    const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                    const weekDay = "周" + weekDays[d.getDay()];
                    // barsToRight: 从centerIdx到最右边可见K线
                    const rightGlobalIdx = globalStart + klines.length - 1;
                    const barsToRight = Math.max(1, rightGlobalIdx - centerIdx + 1);
                    // 涨跌幅: 从centerIdx到最右边可见K线
                    const prevKLine = centerIdx > 0 ? data.klines[centerIdx - 1] : null;
                    const startPrice = prevKLine ? prevKLine.close : centerKline.open;
                    const rightVisibleK = klines[klines.length - 1];
                    const totalChange = rightVisibleK.close - startPrice;
                    const totalChangePct = startPrice !== 0 ? (totalChange / startPrice * 100).toFixed(2) : "0.00";
                    const tcSign = totalChange >= 0 ? "+" : "";
                    // 跌时在括号内追加回本所需涨幅
                    let pctText = `${tcSign}${totalChangePct}%`;
                    if (totalChange < 0) {
                        const absPct = Math.abs(parseFloat(totalChangePct));
                        if (absPct > 0 && absPct < 100) {
                            const recoverPct = (absPct / (100 - absPct) * 100).toFixed(2);
                            pctText += `/+${recoverPct}%`;
                        }
                    }
                    const extraText = ` ${barsToRight}根 ${tcSign}${totalChange.toFixed(2)}(${pctText})`;
                    const dateText = shortDate + " " + weekDay + extraText;
                    ctx.font = "11px monospace";
                    const textW = ctx.measureText(dateText).width;
                    const labelH = 18;
                    const labelPad = 4;
                    let labelX = centerX - textW / 2 - labelPad;
                    if (labelX < area.x) labelX = area.x;
                    if (labelX + textW + labelPad * 2 > area.x + area.w) labelX = area.x + area.w - textW - labelPad * 2;
                    const labelY = area.y + area.h - labelH;
                    ctx.fillStyle = "#dcdcdc";
                    ctx.fillRect(labelX, labelY, textW + labelPad * 2, labelH);
                    ctx.fillStyle = "#333"; ctx.textAlign = "left";
                    if (totalChange < 0) {
                        // 分段绘制：前半部分黑色，回本百分比红色
                        const recoverSuffix = `/+${(Math.abs(parseFloat(totalChangePct)) / (100 - Math.abs(parseFloat(totalChangePct))) * 100).toFixed(2)}%`;
                        const recoverPart = `/${recoverSuffix})`;
                        const splitIdx = dateText.lastIndexOf(recoverPart);
                        if (splitIdx > 0) {
                            ctx.fillText(dateText.substring(0, splitIdx), labelX + labelPad, labelY + 13);
                            const prefixW = ctx.measureText(dateText.substring(0, splitIdx)).width;
                            ctx.fillStyle = "#fd1050";
                            ctx.fillText(dateText.substring(splitIdx), labelX + labelPad + prefixW, labelY + 13);
                        } else {
                            ctx.fillText(dateText, labelX + labelPad, labelY + 13);
                        }
                    } else {
                        ctx.fillText(dateText, labelX + labelPad, labelY + 13);
                    }
                }
            }
            // 恢复全局变量
            viewOffset = _savedViewOffset; viewCount = _savedViewCount;
            mouseX = _savedMouseX; mouseY = _savedMouseY;
            currentFreq = _savedCurrentFreq;
            chartData = _savedChartData;
            _showVolume = _savedShowVolume;
            // 只在主窗口（上面窗口或单窗口）更新统计
            if (data === _savedChartData || !isDualWindow) {
                generateStats();
            }
            // 始终更新slider（双窗口下根据激活窗口显示对应数据范围）
            updateSlider();
        }

        // ============================================================
        // 红框调试面板已暂时禁用（2026-08-26，按需注释而非删除）。
        // 原实现保存在下方 /* ... */ 块内；恢复时解开注释并删除 no-op 占位即可。
        // 注意：红框本身（Ctrl 选中 / 新中枢计算）不受影响，仅面板不显示。
        // ============================================================
        /*
        // 红框调试面板更新（不依赖console.log，即使F12过滤也能在页面上看到）
        function updateRedFrameDebug() {
            var dbg = document.getElementById("redframe-debug");
            if (!dbg || !isDualWindow) return;
            var st = window._lastRedFrameStatus;
            if (!st) return;
            dbg.style.display = "block";
            var stateEl = document.getElementById("rfdb-state");
            var detailEl = document.getElementById("rfdb-detail");
            // 显示灰色框状态
            var gs = window._lastGrayStatus;
            var grayInfo = "";
            if (gs && gs.startIdx !== undefined) {
                grayInfo = " 灰[" + gs.startIdx + "-" + gs.endIdx + (gs.isVisible ? "✓" : "✗") + "]";
            }
            if (st.state === "SKIP") {
                stateEl.textContent = "跳过";
                stateEl.style.color = "#ffa710";
                var extra = "";
                if (window._lastCalcRedRangeError) extra += " ERR:" + window._lastCalcRedRangeError;
                if (st.aDt) extra += " aDt=" + st.aDt;
                if (st.bDt) extra += " bDt=" + st.bDt;
                if (st.bottomFirst) extra += " btm1st=" + st.bottomFirst;
                if (st.bottomLast) extra += " btmLast=" + st.bottomLast;
                detailEl.textContent = (st.reason||"") + grayInfo + extra + " redRange=" + st.redRange + " hl=" + st.highlightRange + " vis=" + st.isVisible;
            } else if (st.state === "DRAW") {
                stateEl.textContent = "已绘制";
                stateEl.style.color = "#4caf50";
                function fmtDate(d) {
                    if (!d) return "?";
                    if (d.length >= 16) return d.slice(5, 16);
                    return d.slice(5, 10);
                }
                detailEl.textContent = "[" + fmtDate(st.leftDate) + ", " + fmtDate(st.rightDate) + "]";
            } else if (st.state === "OK") {
                stateEl.textContent = "计算OK";
                stateEl.style.color = "#2196f3";
                detailEl.textContent = "A/B[" + st.aIdx + "," + st.bIdx + "] 灰[" + st.grayStart + "," + st.grayEnd + "] before=" + st.before + " after=" + st.after + grayInfo;
            } else {
                stateEl.textContent = st.state || "--";
                stateEl.style.color = "#fff";
                detailEl.textContent = "";
            }
        }
        */
        // no-op 占位：保留各处 updateRedFrameDebug() 调用不致报错，面板不再显示
        function updateRedFrameDebug() {}

        // 上面窗口鼠标移动时更新下面窗口高亮并重绘下面窗口
        function updateDualHighlight() {
            if (!isDualWindow || !dualSubData) return;
            if (mouseX >= 0) {
                dualHighlightRange = calcGrayRange(mouseX);
                // 只有按住 Ctrl 键时才计算红框（耗资源操作），否则只显示灰框
                dualRedRange = (_ctrlPressed && dualHighlightRange) ? dualHighlightRange.redRange : null;
                // 更新状态A：区间是否在视口外
                dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                // 更新调试面板：显示灰框状态
                if (dualHighlightRange && dualHighlightRange.startIdx !== undefined) {
                    window._lastGrayStatus = {
                        startIdx: dualHighlightRange.startIdx,
                        endIdx: dualHighlightRange.endIdx,
                        isVisible: dualHighlightRange.isVisible,
                        redRange: !!dualRedRange
                    };
                } else {
                    window._lastGrayStatus = { noMatch: true };
                }
            }
            renderBottom();
        }

        // 双窗口新模式：红框出现后，请求用红框内笔计算新中枢
        function updateDualNewZs() {
            if (!isDualWindow || !dualSubData || !dualHighlightRange) {
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            const rr = dualHighlightRange.redRange;
            if (!rr) {
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            const aIdx = rr.aIdx;
            const bIdx = rr.bIdx;
            if (aIdx === undefined || bIdx === undefined) {
                return;
            }
            // 红框对应的灰框区间不在当前下面窗口视口内时，不切换新中枢
            if (!dualHighlightRange.isVisible) {
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            // 红框左右边界时间（子级别K线格式，传给后端由 _red_range_bi_sequence 找笔）
            const subKlines = dualSubData.klines;
            const leftDate = subKlines[aIdx].date;
            const rightDate = subKlines[bIdx].date;
            const requestKey = dualSubFreq + ":" + leftDate + ":" + rightDate;
            if (dualNewZsFailedKey === requestKey) {
                return;
            }
            if (dualShowNewZs && dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                return;
            }
            dualNewZsLeftDate = leftDate;
            dualNewZsRightDate = rightDate;
            dualShowNewZs = true;
            dualNewZsData = null;
            const code = dualSubData.meta.symbol;
            const isReplay = dualSubData.meta && dualSubData.meta.is_replay;
            let url = "/api/stocks/" + encodeURIComponent(code) + "/red-range?freq=" + dualSubFreq + "&left_date=" + encodeURIComponent(leftDate) + "&right_date=" + encodeURIComponent(rightDate);
            if (isReplay) {
                const endDate = document.getElementById("goto-date-input").value;
                url += "&end_date=" + encodeURIComponent(endDate);
            }
            fetch(url)
                .then(resp => resp.json())
                .then(data => {
                    if (data.error) {
                        console.error("[dual_zs] 后端错误:", data.error);
                        if (dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                            dualNewZsFailedKey = requestKey;
                            dualShowNewZs = false;
                            dualNewZsData = null;
                            renderBottom();
                        }
                        return;
                    }
                    if (dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                        dualNewZsFailedKey = "";
                        dualNewZsData = data;
                        dualShowNewZs = true;
                        renderBottom();
                    }
                })
                .catch(err => {
                    console.error("[dual_zs] 请求失败:", err);
                    if (dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                        dualNewZsFailedKey = requestKey;
                        dualShowNewZs = false;
                        dualNewZsData = null;
                        renderBottom();
                    }
                });
        }

        function drawGrid(area, range) {
            ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
            if (_logScale && range.min > 0) {
                // 等比坐标：网格线按对数均匀分布，视觉上"下密上疏"
                const logMin = Math.log(range.min);
                const logMax = Math.log(range.max);
                for (let i = 0; i <= 5; i++) {
                    const logPrice = logMin + (logMax - logMin) * (i / 5);
                    const price = Math.exp(logPrice);
                    const y = priceToY(price, area, range);
                    ctx.beginPath(); ctx.moveTo(area.x, y); ctx.lineTo(area.x + area.w, y); ctx.stroke();
                }
            } else {
                // 等差坐标：网格线按像素等距分布
                for (let i = 0; i <= 5; i++) {
                    const y = area.y + (area.h / 5) * i;
                    ctx.beginPath(); ctx.moveTo(area.x, y); ctx.lineTo(area.x + area.w, y); ctx.stroke();
                }
            }
        }

        function drawCandles(klines, area, priceRange, barStep, barWidth, subPixelOffset) {
            klines.forEach((k, i) => {
                const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                const openY = priceToY(k.open, area, priceRange);
                const closeY = priceToY(k.close, area, priceRange);
                const highY = priceToY(k.high, area, priceRange);
                const lowY = priceToY(k.low, area, priceRange);
                // 翻转视图下Y轴反转，highY和lowY视觉位置互换：visTop始终是视觉顶部
                const visTop = _isMirrorMode ? lowY : highY;
                const visBot = _isMirrorMode ? highY : lowY;
                const bodyTop = Math.min(openY, closeY);
                const bodyH = Math.max(1, Math.abs(closeY - openY));

                if (k.close === k.open) {
                    // 收盘价等于开盘价，画十字线（竖线+横线，宽度一致）
                    ctx.fillStyle = "#FFFFFF";
                    ctx.fillRect(x - 0.5, visTop, 1, visBot - visTop);          // 竖线：上影线到下影线
                    ctx.fillRect(x - barWidth / 2, closeY - 0.5, barWidth, 1); // 横线：在收盘价位置，与竖线同宽
                } else {
                    // 翻转视图下颜色与空心/实心样式对调：原阳线(涨)显示为阴线样式，原阴线(跌)显示为阳线样式
                    const drawAsRise = _isMirrorMode ? (k.close < k.open) : (k.close > k.open);
                    if (drawAsRise) {
                        // 阳线样式：空心红
                        ctx.fillStyle = "#FF3C3C";
                        if (visTop < bodyTop) {
                            ctx.fillRect(x - 0.5, visTop, 1, bodyTop - visTop);
                        }
                        if (bodyTop + bodyH < visBot) {
                            ctx.fillRect(x - 0.5, bodyTop + bodyH, 1, visBot - bodyTop - bodyH);
                        }
                        ctx.strokeStyle = "#FF3C3C"; ctx.lineWidth = 1;
                        ctx.strokeRect(x - barWidth / 2, bodyTop, barWidth, bodyH);
                    } else {
                        // 阴线样式：实心青
                        ctx.fillStyle = "#00F0F0";
                        ctx.fillRect(x - 0.5, visTop, 1, visBot - visTop);
                        ctx.fillRect(x - barWidth / 2, bodyTop, barWidth, bodyH);
                        ctx.fillRect(x - 0.5, bodyTop + bodyH, 1, visBot - bodyTop - bodyH);
                    }
                }
            });
        }

        function drawMacd(klines, macdArea, macdRange, barStep, barWidth, subPixelOffset) {
            // 翻转视图：MACD柱绕零线翻转（正柱→零线下方），颜色对调；dif/dea线Y轴翻转
            const range = macdRange.max - macdRange.min;
            const macdToY = (v) => _isMirrorMode
                ? macdArea.y + (v - macdRange.min) / range * macdArea.h
                : macdArea.y + macdArea.h - (v - macdRange.min) / range * macdArea.h;
            // 零线Y坐标：用 macdToY(0) 统一计算，翻转模式下自动翻转（与dif/dea线一致）
            const zeroY = macdToY(0);
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const isUp = k.macd >= 0;
                ctx.fillStyle = (_isMirrorMode ? !isUp : isUp) ? COLORS.macdUp : COLORS.macdDown;
                const macdH = Math.abs(k.macd) / range * macdArea.h;
                const y = isUp ? (_isMirrorMode ? zeroY : zeroY - macdH)
                               : (_isMirrorMode ? zeroY - macdH : zeroY);
                ctx.fillRect(x - barWidth / 2, y, barWidth, macdH);
            });
            ctx.strokeStyle = COLORS.dif; ctx.lineWidth = 1;
            ctx.beginPath();
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const y = macdToY(k.dif);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.strokeStyle = COLORS.dea; ctx.lineWidth = 1;
            ctx.beginPath();
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const y = macdToY(k.dea);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.strokeStyle = "rgba(255,255,255,0.2)"; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(macdArea.x, zeroY); ctx.lineTo(macdArea.x + macdArea.w, zeroY); ctx.stroke();
        }

        function drawVolume(klines, volArea, volRange, barStep, barWidth, subPixelOffset) {
            // 底部柱状图（股票=成交额，期货=成交量）：与K线风格一致
            //   红柱（涨）= 空心，颜色 #FF3C3C，与阳K线一致
            //   绿柱（跌）= 实心，颜色 #00F0F0，与阴K线一致
            klines.forEach((k, i) => {
                const x = volArea.x + barStep * i + barStep / 2 - subPixelOffset;
                // 翻转视图：颜色与空心/实心样式对调（与drawCandles一致）
                const drawAsRise = _isMirrorMode ? (k.close < k.open) : (k.close > k.open);
                const volH = (getVolMetric(k) / volRange.max) * volArea.h;
                // 成交量(额)恒为正值，柱子始终从底部向上生长，翻转视图下不翻转柱方向（仅翻转颜色）
                const y = volArea.y + volArea.h - volH;
                if (drawAsRise) {
                    // 红柱：空心，只画边框
                    ctx.strokeStyle = "#FF3C3C"; ctx.lineWidth = 1;
                    ctx.strokeRect(x - barWidth / 2, y, barWidth, volH);
                } else {
                    // 绿柱：实心
                    ctx.fillStyle = "#00F0F0";
                    ctx.fillRect(x - barWidth / 2, y, barWidth, volH);
                }
            });
        }

        function drawBiLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.bis.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            ctx.lineWidth = 1;
            chartData.bis.forEach(bi => {
                let s = dateToGlobalIdx(bi.sdt, map), e = dateToGlobalIdx(bi.edt, map);
                if (s === undefined || e === undefined) return;
                // 笔的两端都必须在视口内才显示（确保成笔条件在当前视口内成立）
                if (s < globalStart || s >= globalEnd || e < globalStart || e >= globalEnd) return;
                let x1 = globalIdxToX(s, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(e, globalStart, area.x, barStep, subPixelOffset);
                // 裁剪到图表主区域内
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(bi.fx_a_price, area, priceRange);
                const y2 = priceToY(bi.fx_b_price, area, priceRange);
                // 未确定的笔用虚线绘制，确定的笔用实线
                // 原值: 确定笔="#FFFFFF", 未确定笔="rgba(255, 255, 255, 0.4)"
                if (bi.is_sure === false) {
                    ctx.strokeStyle = "rgba(253, 221, 96, 0.4)";
                    ctx.setLineDash([4, 4]);
                } else {
                    ctx.strokeStyle = "#fddd60";
                    ctx.setLineDash([]);
                }
                ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
                // 显示笔索引编号
                if (showBiIdx && bi.idx != null) {
                    ctx.font = "10px monospace"; ctx.textAlign = "center";
                    ctx.fillStyle = "#fddd60";
                    const midX = (x1 + x2) / 2;
                    const midY = (y1 + y2) / 2;
                    // 翻转视图：笔方向视觉反转，标签上下位置随之翻转
                    const labelAbove = _isMirrorMode ? (bi.direction !== "up") : (bi.direction === "up");
                    const labelY = labelAbove ? midY - 6 : midY + 12;
                    ctx.fillText(String(bi.idx), midX, labelY);
                }
            });
            ctx.setLineDash([]);
        }

        function drawMacdLabel(textArea, klines, barStep, subPixelOffset) {
            let targetK = null;
            if (mouseX >= textArea.x && mouseX <= textArea.x + textArea.w) {
                const idx = Math.floor((mouseX - textArea.x + subPixelOffset) / barStep);
                targetK = klines[Math.min(idx, klines.length - 1)];
            }
            if (!targetK) targetK = klines[klines.length - 1];
            if (targetK) {
                ctx.font = "11px monospace"; ctx.textAlign = "left";
                const lineY = textArea.y + 11;
                if (_showVolume) {
                    // 底部柱状指标模式：股票显示成交额，期货显示成交量（文字灰色，数字红/绿）
                    // 翻转视图：颜色对调，与翻转后的成交量柱一致
                    const volIsRise = _isMirrorMode ? (targetK.close < targetK.open) : (targetK.close > targetK.open);
                    const volColor = volIsRise ? "#FF3C3C" : "#00F0F0";
                    const vLabel = getVolLabel();
                    ctx.fillStyle = "#a8b2d1";
                    ctx.fillText(vLabel + ":", textArea.x + 4, lineY);
                    let xPos = textArea.x + 4 + ctx.measureText(vLabel + ":").width;
                    ctx.fillStyle = volColor;
                    const val = getVolMetric(targetK);
                    const valLabel = isFuturesMode()
                        ? (val >= 10000 ? (val / 10000).toFixed(2) + "万" : Math.round(val).toString())
                        : (val >= 100000000 ? (val / 100000000).toFixed(2) + "亿" :
                           val >= 10000 ? (val / 10000).toFixed(2) + "万" : val.toFixed(2));
                    ctx.fillText(valLabel, xPos, lineY);
                } else {
                    ctx.fillStyle = COLORS.textLight;
                    ctx.fillText("MACD(12,26,9)", textArea.x + 4, lineY);
                    let xPos = textArea.x + 4 + ctx.measureText("MACD(12,26,9) ").width;
                    // 防御：K线数据可能缺少MACD字段（dif/dea/macd），缺失时跳过标签避免 toFixed 崩溃
                    if (targetK.dif !== undefined && targetK.dea !== undefined && targetK.macd !== undefined) {
                        ctx.fillStyle = COLORS.dif;
                        ctx.fillText("DIF:" + targetK.dif.toFixed(2), xPos, lineY);
                        xPos += ctx.measureText("DIF:" + targetK.dif.toFixed(2) + " ").width;
                        ctx.fillStyle = COLORS.dea;
                        ctx.fillText("DEA:" + targetK.dea.toFixed(2), xPos, lineY);
                        xPos += ctx.measureText("DEA:" + targetK.dea.toFixed(2) + " ").width;
                        // 翻转视图：BAR颜色对调，与翻转后的MACD柱一致
                        const barIsUp = _isMirrorMode ? (targetK.macd < 0) : (targetK.macd >= 0);
                        ctx.fillStyle = barIsUp ? "#FF3C3C" : "#00F0F0";
                        ctx.fillText("BAR:" + targetK.macd.toFixed(2), xPos, lineY);
                    } else {
                        ctx.fillStyle = "#888";
                        ctx.fillText("MACD数据缺失", xPos, lineY);
                    }
                }
            }
        }

        function drawFxMarkers(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.fxs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            let fxNum = 0;
            ctx.font = "10px monospace"; ctx.textAlign = "center";
            chartData.fxs.forEach(fx => {
                let idx = dateToGlobalIdx(fx.date, map);
                if (idx === undefined) return;
                if (idx < globalStart || idx >= globalEnd) return;
                fxNum++;
                const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
                const y = priceToY(fx.price, area, priceRange);
                // 翻转视图：顶分型视觉变底分型，颜色与标签位置随之翻转
                const showAsTop = _isMirrorMode ? (fx.mark !== "G") : (fx.mark === "G");
                const color = showAsTop ? COLORS.up : COLORS.down;
                ctx.fillStyle = color;
                ctx.fillText(String(fxNum), x, showAsTop ? y - 4 : y + 10);
            });
        }

        function drawZs(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.zs || !chartData.zs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            const isReplay = chartData.meta && chartData.meta.is_replay;

            chartData.zs.forEach(zs => {
                let sIdx = dateToGlobalIdx(zs.sdt, map);
                let eIdx = zs.confirm_edt ? dateToGlobalIdx(zs.confirm_edt, map) : undefined;
                if (sIdx === undefined) return;
                if (eIdx === undefined) {
                    // 未确认结束的中枢延伸到当前数据最后一根K线，而不是使用 zs.end/edt 过早收口
                    eIdx = chartData.klines.length - 1;
                }
                // 只绘制与当前视口有交集的中枢
                if (eIdx < globalStart || sIdx >= globalEnd) return;

                // 右边框使用后端给出的“中枢结束事实被确认”的时点；未确认则延伸到最新K线
                let finalEndIdx = eIdx;

                let x1 = globalIdxToX(sIdx, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(finalEndIdx, globalStart, area.x, barStep, subPixelOffset);
                // 裁剪到图表主区域内
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(zs.zg, area, priceRange);
                const y2 = priceToY(zs.zd, area, priceRange);

                // 翻转视图：向上中枢视觉变向下，颜色随之翻转
                const isUp = _isMirrorMode ? (zs.dir !== "up") : (zs.dir === "up");
                const fillColor = isUp ? "rgba(220, 50, 50, 0.10)" : "rgba(50, 180, 50, 0.10)";
                const strokeColor = isUp ? "rgba(220, 50, 50, 0.6)" : "rgba(50, 180, 50, 0.6)";
                const textColor = isUp ? "rgba(220, 50, 50, 0.8)" : "rgba(50, 180, 50, 0.8)";

                ctx.fillStyle = fillColor;
                ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
                ctx.setLineDash([]);

                ctx.font = "10px monospace";
                ctx.fillStyle = textColor;
                ctx.textAlign = "right";
                ctx.fillText(_fmtPrice(zs.zg), x1 - 2, y1 - 2);
                ctx.fillText(_fmtPrice(zs.zd), x1 - 2, y2 + 10);
                // 中枢高度，标在上下沿中间位置
                const zsHeight = zs.zg - zs.zd;
                ctx.fillText(_fmtPrice(zsHeight), x1 - 2, (y1 + y2) / 2 + 3);
            });
        }

        // 画线段（与笔同粗细，区分方向颜色）
        function drawSegLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.segs || !chartData.segs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            ctx.lineWidth = 1; ctx.setLineDash([]);
            chartData.segs.forEach(seg => {
                let s = dateToGlobalIdx(seg.sdt, map), e = dateToGlobalIdx(seg.edt, map);
                if (s === undefined || e === undefined) return;
                // 只绘制与当前视口有交集的线段
                if (e < globalStart || s >= globalEnd) return;
                let x1 = globalIdxToX(s, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(e, globalStart, area.x, barStep, subPixelOffset);
                // 裁剪到图表主区域内
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(seg.begin_price, area, priceRange);
                const y2 = priceToY(seg.end_price, area, priceRange);
                ctx.strokeStyle = "#ffa710"; // 原值: up="#FF6666", down="#66FF66"
                ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
            });
        }

        // 画买卖点标记（买点▲红色，卖点▼绿色——绿色与MACD绿柱子同色）
        // 标记画在K线外侧：买点在最低价下方，卖点在最高价上方，与分型标号/五角星错开
        function drawBspMarkers(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.bsps || !chartData.bsps.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            chartData.bsps.forEach(bsp => {
                let idx = dateToGlobalIdx(bsp.date, map);
                if (idx === undefined) return;
                if (idx < globalStart || idx >= globalEnd) return;
                if (bspFilter && !bspFilter[bsp.type]) return;  // 按用户设置过滤类型
                const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
                const isBuy = bsp.is_buy;
                // 用K线外侧价格定位：买点用low，卖点用high（锚点价格不随翻转改变）
                const anchorPrice = isBuy ? bsp.low : bsp.high;
                const y = priceToY(anchorPrice, area, priceRange);
                // 翻转视图：买/卖视觉互换——颜色、符号、上下偏移随之翻转
                const showAsBuy = _isMirrorMode ? !isBuy : isBuy;
                const color = showAsBuy ? COLORS.up : COLORS.down;
                ctx.fillStyle = color;
                ctx.textAlign = "center";
                ctx.textBaseline = "middle";
                ctx.font = "bold 14px monospace";
                // 错开偏移：买点往下放（远离五角星/分型），卖点往上放
                const markerY = showAsBuy ? y + 22 : y - 22;
                ctx.fillText(showAsBuy ? "▲" : "▼", x, markerY);
                // 买卖点类型标签再往外错开一点（与三角形同色，fillStyle已设置）
                ctx.font = "11px sans-serif";
                const labelY = showAsBuy ? markerY + 18 : markerY - 18;
                ctx.fillText(bsp.type, x, labelY);
                ctx.textBaseline = "alphabetic";
            });
        }

        // 双窗口新模式：绘制红框内笔计算的新中枢（替代原中枢/线段/买卖点）
        function drawDualNewZs(klines, area, priceRange, barStep, subPixelOffset) {
            if (!dualNewZsData || !dualNewZsData.zs || !dualNewZsData.zs.length) {
                return;
            }
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;

            dualNewZsData.zs.forEach((zs, zsIdx) => {
                let sIdx = dateToGlobalIdx(zs.sdt, map);
                if (sIdx === undefined) return;
                let eIdx = undefined;
                if (zs.confirm_edt) {
                    // 中枢被确认的笔打破 → 右边界在打破笔的末端
                    eIdx = dateToGlobalIdx(zs.confirm_edt, map);
                }
                if (eIdx === undefined) {
                    // 未被确认打破 → 用 edt（最后重叠笔的末端）
                    eIdx = zs.edt ? dateToGlobalIdx(zs.edt, map) : undefined;
                }
                if (eIdx === undefined) {
                    eIdx = dualSubData.klines.length - 1;
                }
                // 最后一个中枢，未被确认打破 → 延伸到红框右边界
                if (zsIdx === dualNewZsData.zs.length - 1 && !zs.confirm_edt && dualRedRange && dualRedRange.bIdx !== undefined) {
                    if (dualRedRange.bIdx > eIdx) {
                        eIdx = dualRedRange.bIdx;
                    }
                }
                if (eIdx < globalStart || sIdx >= globalEnd) return;
                let finalEndIdx = eIdx;
                let x1 = globalIdxToX(sIdx, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(finalEndIdx, globalStart, area.x, barStep, subPixelOffset);
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(zs.zg, area, priceRange);
                const y2 = priceToY(zs.zd, area, priceRange);

                const isUp = _isMirrorMode ? (zs.dir !== "up") : (zs.dir === "up");
                // 新中枢使用更醒目的颜色区分
                const fillColor = isUp ? "rgba(220, 50, 50, 0.10)" : "rgba(50, 180, 50, 0.10)";
                const strokeColor = isUp ? "rgba(255, 80, 80, 0.85)" : "rgba(80, 255, 80, 0.85)";
                const textColor = isUp ? "rgba(220, 50, 50, 0.8)" : "rgba(50, 180, 50, 0.8)";

                ctx.fillStyle = fillColor;
                ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
                ctx.setLineDash([]);

                ctx.font = "10px monospace";
                ctx.fillStyle = textColor;
                ctx.textAlign = "right";
                ctx.fillText(_fmtPrice(zs.zg), x1 - 2, y1 - 2);
                ctx.fillText(_fmtPrice(zs.zd), x1 - 2, y2 + 10);
                // 中枢高度，标在上下沿中间位置
                const zsHeight = zs.zg - zs.zd;
                ctx.fillText(_fmtPrice(zsHeight), x1 - 2, (y1 + y2) / 2 + 3);
            });
        }

        function drawMaLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || klines.length < 2) return;
            const start = Math.max(0, Math.floor(viewOffset));
            const allKlines = chartData.klines;
            const n = allKlines.length;
            // 收集已选中的均线周期（按周期升序，短周期画在上层）
            const periods = [];
            for (var p in maPeriods) {
                if (maPeriods[p]) periods.push(parseInt(p, 10));
            }
            periods.sort(function(a, b) { return a - b; });
            if (periods.length === 0) return;
            ctx.lineWidth = 1;
            for (let pi = 0; pi < periods.length; pi++) {
                const period = periods[pi];
                if (period <= 0 || period > n) continue;
                // 滑动窗口计算该周期均线
                const ma = new Array(n).fill(null);
                let sum = 0;
                for (let i = 0; i < n; i++) {
                    sum += allKlines[i].close;
                    if (i >= period) sum -= allKlines[i - period].close;
                    if (i >= period - 1) ma[i] = sum / period;
                }
                ctx.strokeStyle = MA_COLORS[period] || "#FFFFFF";
                ctx.beginPath();
                let started = false;
                for (let i = 0; i < klines.length; i++) {
                    const globalIdx = start + i;
                    if (globalIdx < n && ma[globalIdx] !== null && !isNaN(ma[globalIdx])) {
                        const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                        const y = priceToY(ma[globalIdx], area, priceRange);
                        if (!started) { ctx.moveTo(x, y); started = true; }
                        else ctx.lineTo(x, y);
                    }
                }
                ctx.stroke();
            }
        }

        // 画最新笔的白色横虚线（同一时间只有一根）
        function drawWhiteHLine(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.white_hline) return;
            const hline = chartData.white_hline;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            // 找到起始日期对应的全局索引
            let startIdx = dateToGlobalIdx(hline.start_date, map);
            if (startIdx === undefined) return;
            // 如果起始点在视口右边之外，不绘制
            if (startIdx >= globalEnd) return;
            // 计算起始X坐标（如果起始点在视口左边之外，则从area.x开始）
            let x1;
            if (startIdx < globalStart) {
                x1 = area.x;
            } else {
                x1 = globalIdxToX(startIdx, globalStart, area.x, barStep, subPixelOffset);
            }
            // 向右延伸到页面最右边
            const x2 = rightBound;
            const y = priceToY(hline.price, area, priceRange);
            // 白色横虚线
            ctx.strokeStyle = "#FFFFFF";
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 3]);
            ctx.beginPath();
            ctx.moveTo(x1, y);
            ctx.lineTo(x2, y);
            ctx.stroke();
            ctx.setLineDash([]);
            // 在右端显示价格标签
            ctx.fillStyle = "#FFFFFF";
            ctx.font = "11px monospace";
            ctx.textAlign = "left";
            ctx.fillText(_fmtPrice(hline.price), x2 + 4, y + 4);
        }

        /**
         * 同花顺风格：在视口内标注最高价和最低价的极值K线
         * - 数值和箭头纯白色 #FFFFFF
         * - 高点：下边沿贴合极值线，数值 ↘；低点：上边沿贴合极值线，数值 ↗
         * - 左侧空间不足时：高点 ↙ 数值，低点 ↖ 数值（数值显示在右侧）
         */
        function drawViewportHighLow(klines, area, priceRange, barStep, subPixelOffset) {
            if (!klines.length) return;

            // 找到视口内最高价和最低价的K线
            let maxHigh = -Infinity, minLow = Infinity;
            let maxHighIdx = -1, minLowIdx = -1;

            for (let i = 0; i < klines.length; i++) {
                const k = klines[i];
                if (k.high > maxHigh) { maxHigh = k.high; maxHighIdx = i; }
                if (k.low < minLow) { minLow = k.low; minLowIdx = i; }
            }

            if (maxHighIdx === -1 || minLowIdx === -1) return;

            const gap = 4; // 数值与箭头间距

            ctx.font = "11px monospace";
            ctx.fillStyle = "#FFFFFF";

            const arrowR = "\u2192"; // →  用于计算箭头宽度（所有箭头等宽）
            const arrowW = ctx.measureText(arrowR).width;

            /**
             * 绘制单个极值标注
             * @param {number} price - 极值价格
             * @param {number} klineIdx - K线在视口内的索引
             * @param {boolean} isHigh - 是否为高点（true=下边沿贴合, false=上边沿贴合）
             */
            function drawOne(price, klineIdx, isHigh) {
                const kx = area.x + barStep * klineIdx + barStep / 2 - subPixelOffset;
                const ky = priceToY(price, area, priceRange);
                const text = _fmtPrice(price);
                const textW = ctx.measureText(text).width;

                const needLeft = textW + gap + arrowW;
                const canLeft = (kx - needLeft) >= area.x;

                const textHeight = 11 * 1.2;  // fontSize * 行高系数
                const a = isHigh ? (canLeft ? "\u2198" : "\u2199") : (canLeft ? "\u2197" : "\u2196");

                ctx.textAlign = "left";

                if (canLeft) {
                    // 数值 ↘(高) / ↗(低)
                    const arrowX = kx - arrowW;
                    const textX = arrowX - gap - textW;
                    // 箭头：保持原位置（高点bottom贴合ky，低点top贴合ky）
                    ctx.textBaseline = isHigh ? "bottom" : "top";
                    ctx.fillText(a, textX + textW + gap, ky);
                    // 数值：中间对齐箭头尾端，高点往上移，低点往下移
                    ctx.textBaseline = "middle";
                    ctx.fillText(text, textX, isHigh ? ky - textHeight / 2 : ky + textHeight / 2);
                } else {
                    // ↙(高) / ↖(低) 数值
                    const arrowX = kx;
                    const textX = arrowX + arrowW + gap;
                    if (textX + textW > area.x + area.w) return;
                    // 箭头：保持原位置
                    ctx.textBaseline = isHigh ? "bottom" : "top";
                    ctx.fillText(a, arrowX, ky);
                    // 数值：中间对齐箭头尾端
                    ctx.textBaseline = "middle";
                    ctx.fillText(text, textX, isHigh ? ky - textHeight / 2 : ky + textHeight / 2);
                }
            }

            // 翻转视图：高低点视觉互换，isHigh随之翻转（箭头方向与文字对齐跟随Y轴翻转）
            drawOne(maxHigh, maxHighIdx, _isMirrorMode ? false : true);   // 高点：下边沿贴合
            drawOne(minLow, minLowIdx, _isMirrorMode ? true : false);    // 低点：上边沿贴合
        }

        function drawCrosshair(klines, area, priceRange, volArea, volRange, barStep, macdTextArea, subPixelOffset) {
            let idx, k, cx;
            if (mouseX < area.x || mouseX > area.x + area.w) {
                idx = klines.length - 1;
                k = klines[idx];
                if (!k) return;
                cx = area.x + barStep * idx + barStep / 2 - subPixelOffset;
                // K线不足一屏时右对齐
                if (currentFreq === 'w') {
                    cx = area.x + area.w - barStep / 2;
                }
            } else {
                idx = Math.floor((mouseX - area.x + subPixelOffset) / barStep);
                k = klines[Math.min(idx, klines.length - 1)];
                if (!k) return;
                cx = area.x + barStep * idx + barStep / 2 - subPixelOffset;
                const crosshairEndY = volArea.y + volArea.h;
                ctx.strokeStyle = COLORS.crosshair; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
                ctx.beginPath(); ctx.moveTo(cx, area.y); ctx.lineTo(cx, crosshairEndY); ctx.stroke();
                if (mouseY >= area.y && mouseY <= crosshairEndY) {
                    ctx.beginPath(); ctx.moveTo(area.x, mouseY); ctx.lineTo(area.x + area.w, mouseY); ctx.stroke();
                }
                ctx.setLineDash([]);
                if (mouseY >= area.y && mouseY <= area.y + area.h) {
                    const price = yToPrice(mouseY, area, priceRange);
                    _overlayData = _overlayData || {};
                    _overlayData.rightPrice = _fmtPrice(price);
                    _overlayData.rightY = mouseY;
                }
            }

            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalIdx = globalStart + idx;
            if (!window._isRenderingBottom) {
                _currentGlobalIdx = globalIdx;
            }
            const prevK = globalIdx > 0 ? chartData.klines[globalIdx - 1] : null;
            const prevClose = prevK ? prevK.close : k.open;
            const changeVal = k.close - prevClose;
            const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
            const cls = changeVal >= 0 ? "up" : "down";
            const sign = changeVal >= 0 ? "+" : "";

            if (mouseX >= area.x && mouseX <= area.x + area.w) {
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                let shortDate;
                if (currentFreq === '15s') {
                    shortDate = getKlineEndTime(k.date, true);  // 含秒
                } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m') {
                    shortDate = getKlineEndTime(k.date);
                } else if (currentFreq === 'w') {
                    const dateParts = k.date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                } else {
                    const dateParts = k.date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                }
                const d = new Date(k.date.replace(/\//g, "-").replace(" ", "T"));
                const weekDay = "周" + weekDays[d.getDay()];

                const rightVisibleK = klines[klines.length - 1];
                const rightGlobalIdx = globalStart + klines.length - 1;
                const barsToRight = Math.max(1, rightGlobalIdx - globalIdx + 1);
                const prevKLine = globalIdx > 0 ? chartData.klines[globalIdx - 1] : null;
                const startPrice = prevKLine ? prevKLine.close : k.open;
                const totalChange = rightVisibleK.close - startPrice;
                const totalChangePct = startPrice !== 0 ? (totalChange / startPrice * 100).toFixed(2) : "0.00";
                const tcSign = totalChange >= 0 ? "+" : "";

                // 跌时在括号内追加回本所需涨幅
                let pctText = `${tcSign}${totalChangePct}%`;
                if (totalChange < 0) {
                    const absPct = Math.abs(parseFloat(totalChangePct));
                    if (absPct > 0 && absPct < 100) {
                        const recoverPct = (absPct / (100 - absPct) * 100).toFixed(2);
                        pctText += `/+${recoverPct}%`;
                    }
                }
                const extraText = ` ${barsToRight}根 ${tcSign}${totalChange.toFixed(2)}(${pctText})`;
                const dateText = shortDate + " " + weekDay + extraText;

                ctx.font = "11px monospace";
                const textW = ctx.measureText(dateText).width;
                const labelH = 18;
                const labelPad = 4;
                let labelX = cx - textW / 2 - labelPad;
                if (labelX < area.x) labelX = area.x;
                if (labelX + textW + labelPad * 2 > area.x + area.w) labelX = area.x + area.w - textW - labelPad * 2;
                const labelY = area.y + area.h - labelH;
                _overlayData = _overlayData || {};
                _overlayData.bottomText = dateText;
                _overlayData.bottomIsDown = totalChange < 0;
                _overlayData.bottomX = labelX;
                _overlayData.bottomY = labelY;
                _overlayData.bottomW = textW;
                _overlayData.bottomH = labelH;
                _overlayData.bottomPad = labelPad;
            }

            // 双窗口：下面窗口渲染时，仅当鼠标不在下面窗口上（mouseX<0）才跳过 OHLC 更新
            // 避免"鼠标在上面窗口时，下面窗口的最后一根K线数据覆盖上面窗口的 OHLC"
            if (!(window._isRenderingBottom && mouseX < 0)) {
                // 显示真实OHLC（翻转视图不改数值，前复权负价原样显示含负号）
                const dispOpen = k.open;
                const dispHigh = k.high;
                const dispLow = k.low;
                const dispClose = k.close;
                document.getElementById("crosshair-info").innerHTML =
                    `<span class="label">开:</span> <span class="${cls}">${dispOpen.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">高:</span> <span class="${cls}">${dispHigh.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">低:</span> <span class="${cls}">${dispLow.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">收:</span> <span class="${cls}">${dispClose.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">涨跌:</span> <span class="${cls}">${sign}${changeVal.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">涨幅:</span> <span class="${cls}">${sign}${changePct}%</span> &nbsp; ` +
                    `<span class="label">复权:</span> <span class="label">${chartData.meta.forward_adjust ? "前复权" : "不复权"}</span>` +
                    (chartData.meta.pe_ttm != null ? ` &nbsp; <span class="label">PE-TTM:</span> <span class="label">${chartData.meta.pe_ttm > 0 ? chartData.meta.pe_ttm.toFixed(2) : "亏损"}</span>` : "") +
                    (chartData.meta.index_belong ? ` &nbsp; <span class="label">归属:</span> <span class="label">${chartData.meta.index_belong}</span>` : "");
            }

            // 均线浮动提示：检测鼠标是否靠近某条均线，若在阈值内则显示tooltip
            const maTooltip = document.getElementById("ma-tooltip");
            if (mouseX >= area.x && mouseX <= area.x + area.w && mouseY >= area.y && mouseY <= area.y + area.h) {
                const maPeriodsKeys = [];
                for (var mp in maPeriods) { if (maPeriods[mp]) maPeriodsKeys.push(parseInt(mp, 10)); }
                maPeriodsKeys.sort(function(a, b) { return a - b; });
                const allN = chartData.klines.length;
                const MA_HOVER_THRESHOLD = 10;
                let bestPeriod = null, bestDist = Infinity;
                for (let pi = 0; pi < maPeriodsKeys.length; pi++) {
                    const p = maPeriodsKeys[pi];
                    if (p <= 0 || p > allN || globalIdx < p - 1) continue;
                    let s = 0;
                    for (let i = globalIdx - p + 1; i <= globalIdx; i++) s += chartData.klines[i].close;
                    const maVal = s / p;
                    const maY = priceToY(maVal, area, priceRange);
                    const d = Math.abs(maY - mouseY);
                    if (d < MA_HOVER_THRESHOLD && d < bestDist) {
                        bestDist = d;
                        bestPeriod = p;
                    }
                }
                if (bestPeriod !== null) {
                    const bestColor = MA_COLORS[bestPeriod] || "#FFFFFF";
                    maTooltip.innerHTML = `MA${bestPeriod}`;
                    const containerRect = document.getElementById("chart-container").getBoundingClientRect();
                    const canvasRect = canvas.getBoundingClientRect();
                    const offX = canvasRect.left - containerRect.left;
                    const offY = canvasRect.top - containerRect.top;
                    let tx = mouseX + offX + 14;
                    let ty = mouseY + offY - 22;
                    maTooltip.style.display = "block";
                    // 防止超出右边界
                    if (tx + maTooltip.offsetWidth > containerRect.width - 4) tx = mouseX + offX - maTooltip.offsetWidth - 14;
                    if (ty < 0) ty = mouseY + offY + 14;
                    maTooltip.style.left = tx + "px";
                    maTooltip.style.top = ty + "px";
                } else {
                    maTooltip.style.display = "none";
                }
            } else {
                maTooltip.style.display = "none";
            }

            const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
            const weekDayStr = "周" + weekDays[new Date(k.date.replace(/\//g, "-").replace(" ", "T")).getDay()];
            // 剪贴板文本：真实OHLC（翻转视图不改数值）
            const clipOpen = k.open;
            const clipHigh = k.high;
            const clipLow = k.low;
            const clipClose = k.close;
            const clipText = `${k.date} ${weekDayStr} 开:${clipOpen.toFixed(2)} 高:${clipHigh.toFixed(2)} 低:${clipLow.toFixed(2)} 收:${clipClose.toFixed(2)}`;
            if (window._isRenderingBottom) {
                // 底部窗口：记录底部窗口的全局索引和剪贴板文本
                _subCurrentGlobalIdx = globalIdx;
                _subClipText = clipText;
            } else {
                // 上面窗口
                _currentGlobalIdx = globalIdx;
                _currentClipText = clipText;
            }
        }

        function drawPriceAxis(area, priceRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
            if (_logScale && priceRange.min > 0) {
                // 对数坐标系：价格标签按对数均匀分布
                const logMin = Math.log(priceRange.min);
                const logMax = Math.log(priceRange.max);
                for (let i = 0; i <= 5; i++) {
                    const logPrice = logMin + (logMax - logMin) * (1 - i / 5);
                    const price = Math.exp(logPrice);
                    const y = priceToY(price, area, priceRange);
                    ctx.fillText(_fmtPrice(price), area.x + area.w + 6, y + 4);
                }
            } else {
                // 普通坐标系：价格标签按算术均匀分布（Y坐标经priceToY，翻转视图下自动翻转）
                for (let i = 0; i <= 5; i++) {
                    const price = priceRange.min + (priceRange.max - priceRange.min) * (1 - i / 5);
                    const y = priceToY(price, area, priceRange);
                    ctx.fillText(_fmtPrice(price), area.x + area.w + 6, y + 4);
                }
            }
        }

        function drawMacdAxis(macdArea, macdRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
            const range = macdRange.max - macdRange.min;
            // 翻转视图：max/min标签位置互换（Y轴翻转），0标签位置也随Y轴翻转
            const zeroY = _isMirrorMode
                ? macdArea.y + (0 - macdRange.min) / range * macdArea.h
                : macdArea.y + macdArea.h * (macdRange.max / range);
            const topVal = _isMirrorMode ? macdRange.min : macdRange.max;
            const botVal = _isMirrorMode ? macdRange.max : macdRange.min;
            ctx.fillText(topVal.toFixed(2), macdArea.x + macdArea.w + 6, macdArea.y + 12);
            ctx.fillText("0", macdArea.x + macdArea.w + 6, zeroY + 4);
            ctx.fillText(botVal.toFixed(2), macdArea.x + macdArea.w + 6, macdArea.y + macdArea.h - 4);
        }

        function drawVolumeAxis(volArea, volRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
            // 成交量(额)恒为正值，轴标签不随翻转视图改变（0在底部、max在顶部）
            const maxLabel = formatVolume(volRange.max);
            ctx.fillText(maxLabel, volArea.x + volArea.w + 6, volArea.y + 12);
            const midLabel = formatVolume(volRange.max / 2);
            ctx.fillText(midLabel, volArea.x + volArea.w + 6, volArea.y + volArea.h / 2 + 4);
            ctx.fillText("0", volArea.x + volArea.w + 6, volArea.y + volArea.h - 4);
        }

        // 格式化底部柱状指标数字（股票成交额：万/亿；期货成交量：手，万级用万）
        function formatVolume(vol) {
            if (isFuturesMode()) {
                if (vol >= 10000) return (vol / 10000).toFixed(2) + "万";
                return Math.round(vol).toString();
            }
            if (vol >= 100000000) return (vol / 100000000).toFixed(2) + "亿";
            if (vol >= 10000) return (vol / 10000).toFixed(2) + "万";
            return vol.toFixed(0);
        }

        function drawDateAxis(klines, barStep, subPixelOffset) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace";
            const area = getChartArea(), volArea = getVolArea();
            const dateY = volArea.y + volArea.h + 28;

            // 测量样本日期文本宽度，用于计算最小像素间距
            let sampleDate;
            if (currentFreq === '15s') {
                sampleDate = getKlineEndTime(klines[0].date, true);
            } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m') {
                sampleDate = getKlineEndTime(klines[0].date);
            } else {
                const dateParts = klines[0].date.split(/[-\/]/);
                sampleDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
            }
            const textWidth = ctx.measureText(sampleDate).width;
            const gap = 10;  // 标签文本边缘之间的最小像素间距
            const n = klines.length;
            const lastIdx = n - 1;

            // 始终包含首尾标签
            const indices = [0];

            if (n > 1) {
                // 首标签左对齐，尾标签右对齐，中间标签居中
                // 首标签右边缘 = area.x + textWidth
                // 第一个中间标签左边缘 = centerX - textWidth/2，要求 centerX >= area.x + textWidth + gap + textWidth/2
                // 即 centerX >= area.x + 1.5*textWidth + gap
                // centerX = area.x + barStep * idx + barStep/2 - subPixelOffset
                // => idx >= (1.5*textWidth + gap - barStep/2 + subPixelOffset) / barStep
                const firstMiddleIdx = Math.max(1, Math.round((1.5 * textWidth + gap - barStep / 2 + subPixelOffset) / barStep));

                // 尾标签左边缘 = area.x + area.w - textWidth
                // 最后一个中间标签右边缘 = centerX + textWidth/2，要求 centerX <= area.x + area.w - textWidth - gap - textWidth/2
                // => idx <= (area.w - 1.5*textWidth - gap - barStep/2 + subPixelOffset) / barStep
                const lastMiddleIdx = Math.min(lastIdx - 1, Math.round((area.w - 1.5 * textWidth - gap - barStep / 2 + subPixelOffset) / barStep));

                if (firstMiddleIdx <= lastMiddleIdx) {
                    // 中间标签之间的最小K线间隔（保证居中标签不重叠）
                    const minIdxGap = Math.ceil((textWidth + gap) / barStep);
                    const available = lastMiddleIdx - firstMiddleIdx;
                    const k = Math.floor(available / minIdxGap) + 1;  // 中间标签个数
                    if (k >= 1 && k === 1) {
                        // 只有一个中间标签：放在安全区间中点
                        indices.push(Math.round((firstMiddleIdx + lastMiddleIdx) / 2));
                    } else if (k >= 2) {
                        // 多个中间标签：均匀分布
                        const step = available / (k - 1);
                        for (let i = 0; i < k; i++) {
                            indices.push(Math.round(firstMiddleIdx + i * step));
                        }
                    }
                }

                indices.push(lastIdx);
            }

            // 绘制标签
            indices.forEach(i => {
                let shortDate;
                if (currentFreq === '15s') {
                    shortDate = getKlineEndTime(klines[i].date, true);
                } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m') {
                    shortDate = getKlineEndTime(klines[i].date);
                } else if (currentFreq === 'w') {
                    const dateParts = klines[i].date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                } else {
                    // 日线
                    const dateParts = klines[i].date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                }
                if (i === 0) {
                    ctx.textAlign = "left";
                    ctx.fillText(shortDate, area.x, dateY);
                } else if (i === lastIdx) {
                    ctx.textAlign = "right";
                    ctx.fillText(shortDate, area.x + area.w, dateY);
                } else {
                    ctx.textAlign = "center";
                    const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                    ctx.fillText(shortDate, x, dateY);
                }
            });
        }

        // ===== 白框覆盖层重绘（供倒计时动画帧使用） =====
        function _drawOverlayIfNeeded(overlayData, area) {
            if (!overlayData) return;
            if (overlayData.rightPrice !== undefined) {
                const labelW = 50;
                ctx.fillStyle = "#dcdcdc"; ctx.fillRect(area.x + area.w + 2, overlayData.rightY - 10, labelW, 20);
                ctx.fillStyle = "#333"; ctx.font = "11px monospace"; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
                ctx.fillText(overlayData.rightPrice, area.x + area.w + 6, overlayData.rightY + 4);
            }
            if (overlayData.bottomText) {
                const d = overlayData;
                ctx.fillStyle = "#dcdcdc";
                ctx.fillRect(d.bottomX, d.bottomY, d.bottomW + d.bottomPad * 2, d.bottomH);
                ctx.fillStyle = "#333"; ctx.font = "11px monospace"; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
                if (d.bottomIsDown) {
                    const txt = d.bottomText;
                    const lastParen = txt.lastIndexOf(")");
                    if (lastParen > 0) {
                        const before = txt.substring(0, lastParen);
                        const after = txt.substring(lastParen);
                        const slashIdx = before.lastIndexOf("/+");
                        if (slashIdx > 0) {
                            ctx.fillText(before.substring(0, slashIdx), d.bottomX + d.bottomPad, d.bottomY + 13);
                            const prefixW = ctx.measureText(before.substring(0, slashIdx)).width;
                            ctx.fillStyle = "#fd1050";
                            ctx.fillText(before.substring(slashIdx) + after, d.bottomX + d.bottomPad + prefixW, d.bottomY + 13);
                        } else {
                            ctx.fillText(txt, d.bottomX + d.bottomPad, d.bottomY + 13);
                        }
                    } else {
                        ctx.fillText(txt, d.bottomX + d.bottomPad, d.bottomY + 13);
                    }
                } else {
                    ctx.fillText(d.bottomText, d.bottomX + d.bottomPad, d.bottomY + 13);
                }
            }
        }

        function _calcCountdownState(freq, data) {
            // 返回倒计时计算状态，或 null（不显示）
            // freq/data 缺省时使用全局 currentFreq/chartData（上窗/单窗）
            freq = freq || currentFreq;
            data = data || chartData;
            if (!isRealtimeMode || realtimeStartTime) return null;
            const freqSec = FREQ_SEC_MAP_JS[freq];
            if (!freqSec || freqSec >= 86400) return null;
            if (!data || !data.klines || data.klines.length === 0) return null;

            const lastK = data.klines[data.klines.length - 1];
            const dateStr = lastK.date;
            const parts = dateStr.split(/[-\/\s:]/);
            if (parts.length < 5) return null;
            const yy = parseInt(parts[0]), mm = parseInt(parts[1]) - 1, dd = parseInt(parts[2]);
            const hh = parseInt(parts[3]), min = parseInt(parts[4]);
            const ss = parts.length >= 6 ? parseInt(parts[5]) : 0;
            const klineStart = new Date(yy, mm, dd, hh, min, ss);
            const klineEnd = new Date(klineStart.getTime() + freqSec * 1000);
            const now = new Date();
            const remaining = Math.max(0, (klineEnd.getTime() - now.getTime()) / 1000);

            const remMin = Math.floor(remaining / 60);
            const remSec = Math.floor(remaining % 60);
            const timeStr = String(remMin).padStart(2, '0') + ':' + String(remSec).padStart(2, '0');
            const ratio = Math.min(1, Math.max(0, remaining / freqSec));
            return { remaining, timeStr, ratio };
        }

        function _drawCountdownImpl(area, state) {
            // 纯绘制，不计算状态（供 render 和动画帧复用）
            const timeStr = state.timeStr;
            const ratio = state.ratio;
            // 确保进度条高度在物理像素层面为整数，杜绝亚像素抗锯齿导致的灰/红色段视觉错位
            const dpr = window.devicePixelRatio || 1;
            const barHeight = Math.round(2 * dpr) / dpr;

            let barX = 0, barY = 0, barWidth = 0;

            // 保存并恢复上下文状态，避免影响后续绘制
            const savedFont = ctx.font;
            const savedTextAlign = ctx.textAlign;
            const savedTextBaseline = ctx.textBaseline;
            const savedFillStyle = ctx.fillStyle;
            try {
                ctx.font = '11px monospace';
                // 所有尺寸取整，避免浮点坐标导致抗锯齿差异
                barWidth = Math.round(ctx.measureText(timeStr).width);
                barX = Math.round(area.x + area.w - barWidth - 6);
                // 倒计时放在K线区域右上角，与底部白框永不重叠
                barY = Math.round(area.y + 6);
                const elapsedW = Math.round(barWidth * (1 - ratio));
                const redW = barWidth - elapsedW;

                // 先画整条灰色底（已流逝），再叠加红色（剩余），
                // 确保两色段共享同一矩形像素区域，杜绝拼接处亚像素错位
                ctx.fillStyle = '#9d9da0';
                ctx.fillRect(barX, barY, barWidth, barHeight);
                if (redW > 0) {
                    ctx.fillStyle = '#dd373a';
                    ctx.fillRect(barX + elapsedW, barY, redW, barHeight);
                }

                // 时间文本
                ctx.textAlign = 'right';
                ctx.textBaseline = 'top';
                ctx.fillStyle = COLORS.text;
                ctx.fillText(timeStr, barX + barWidth, barY + barHeight + 2);
            } finally {
                // 恢复上下文状态（即使绘制抛异常也保证恢复）
                ctx.font = savedFont;
                ctx.textAlign = savedTextAlign;
                ctx.textBaseline = savedTextBaseline;
                ctx.fillStyle = savedFillStyle;
            }

            // 返回边界供调用方存储（上窗/下窗各自独立跟踪）
            return { x: barX, y: barY, w: barWidth, h: barHeight + 2 + 14 };
        }

        function drawCountdownBar(area) {
            const state = _calcCountdownState();
            if (!state) {
                if (canvas === subCanvas) _subCountdownBounds = null;
                else _countdownBounds = null;
                return;
            }
            const bounds = _drawCountdownImpl(area, state);
            if (canvas === subCanvas) _subCountdownBounds = bounds;
            else _countdownBounds = bounds;
        }

        function _redrawCountdown() {
            if (!isRealtimeMode || realtimeStartTime) return;
            // 全量重绘：render() 内部的 drawCountdownBar 会用最新时间重绘进度条，
            // 避免增量擦除导致K线被擦除后不恢复（双窗口下窗焦点时上窗不重绘的问题）。
            render();
        }

        function startCountdownTimer() {
            stopCountdownTimer();
            _redrawCountdown(); // 立即绘制一次
            _countdownTimer = setInterval(_redrawCountdown, 1000);
        }

        function stopCountdownTimer() {
            if (_countdownTimer) {
                clearInterval(_countdownTimer);
                _countdownTimer = null;
            }
            _countdownBounds = null;
            _subCountdownBounds = null;
        }

        function onWheel(e) {
            e.preventDefault();
            if (isDualWindow) { activeDualWindow = 'main'; updateActiveWindowClass(); updateSlider(); updateFreqButtonStates(chartData && chartData.meta && chartData.meta.market === 'futures'); }
            const area = getChartArea();
            const klines = chartData.klines;
            const barStep = area.w / viewCount;
            const ratio = Math.max(0, Math.min(1, (mouseX - area.x) / area.w));
            const mouseKIdx = ratio * viewCount;

            const zoomFactor = 1.15;
            const newViewCount = e.deltaY > 0
                ? Math.min(klines.length, Math.ceil(viewCount * zoomFactor))
                : Math.max(3, Math.round(viewCount / zoomFactor));
            if (newViewCount === viewCount) return;

            const maxOffset = klines.length - newViewCount;

            if (mouseKIdx >= viewCount - 1) {
                const rightGlobalIdx = viewOffset + viewCount - 1;
                viewCount = newViewCount;
                viewOffset = Math.max(0, Math.min(maxOffset, rightGlobalIdx - newViewCount + 1));
                if (isDualWindow) { renderTop(); } else { render(); }
                return;
            }

            const anchorGlobalIdx = viewOffset + mouseKIdx;
            let newViewOffset = anchorGlobalIdx - ratio * newViewCount;
            newViewOffset = Math.max(0, newViewOffset);
            if (newViewOffset > maxOffset) newViewOffset = maxOffset;

            viewCount = newViewCount;
            viewOffset = newViewOffset;
            if (isDualWindow) { renderTop(); } else { render(); }
        }

        function onMouseDown(e) {
            isDragging = true; dragStartX = e.clientX; dragStartOffset = viewOffset; canvas.style.cursor = "grabbing";
            _mouseDownX = e.clientX; _mouseDownY = e.clientY;
            if (isDualWindow) { activeDualWindow = 'main'; updateActiveWindowClass(); updateSlider(); updateFreqButtonStates(chartData && chartData.meta && chartData.meta.market === 'futures'); }
        }

        function onMouseMove(e) {
            const rect = canvas.getBoundingClientRect();
            mouseX = e.clientX - rect.left; mouseY = e.clientY - rect.top;
            if (isDragging) { viewOffset = dragStartOffset - (e.clientX - dragStartX) / (getChartArea().w / viewCount); viewOffset = Math.max(0, Math.min(chartData.klines.length - viewCount, viewOffset)); }
            // 双窗口红框：直接用 MouseEvent.ctrlKey 检测，比 keydown/keyup 跟踪更可靠
            if (isDualWindow) {
                const prevCtrl = _ctrlPressed;
                _ctrlPressed = e.ctrlKey;
                // Ctrl 状态变化时强制重绘（松开Ctrl立即清除红框/新中枢）
                if (_ctrlPressed !== prevCtrl) {
                    if (!_ctrlPressed) {
                        dualRedRange = null;
                        dualShowNewZs = false;
                        dualNewZsData = null;
                    }
                }
                renderTop();
            } else {
                render();
            }
        }

        function onMouseUp(e) {
            isDragging = false; canvas.style.cursor = "crosshair";
            // 只处理左键点击（非拖拽）
            if (e.button !== 0 || Math.abs(e.clientX - _mouseDownX) >= 5 || Math.abs(e.clientY - _mouseDownY) >= 5) return;
            if (_currentGlobalIdx < 0 || !chartData) return;

            // === Ctrl+点击：区间选择模式切换 ===
            if (e.ctrlKey) {
                if (_rangeSelect.mode === 'IDLE') {
                    // 进入选择模式，记录起点A
                    _rangeSelect = {
                        mode: 'SELECTED_A',
                        startIdx: _currentGlobalIdx,
                        startFreq: currentFreq,
                        startSymbol: chartData.meta.symbol
                    };
                    const startDate = chartData.klines[_currentGlobalIdx].date.split(' ')[0];
                    showDualToast("区间起点: " + startDate + "，点击另一根K线完成选择");
                    render();
                } else {
                    // Ctrl+再次点击：取消选择
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("区间选择已取消");
                    render();
                }
                return;
            }

            // === 普通点击：如果在选择模式中，完成区间选择 ===
            if (_rangeSelect.mode === 'SELECTED_A') {
                // 验证：同一股票、同一周期
                if (_rangeSelect.startFreq !== currentFreq || _rangeSelect.startSymbol !== chartData.meta.symbol) {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("股票或周期已变更，区间选择已取消");
                    return;
                }
                const a = Math.min(_rangeSelect.startIdx, _currentGlobalIdx);
                const b = Math.max(_rangeSelect.startIdx, _currentGlobalIdx);
                const klines = chartData.klines;
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                const lines = [];
                for (let i = a; i <= b; i++) {
                    const k = klines[i];
                    const prevK = i > 0 ? klines[i - 1] : null;
                    const prevClose = prevK ? prevK.close : k.open;
                    const changeVal = k.close - prevClose;
                    const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
                    const sign = changeVal >= 0 ? "+" : "";
                    const wd = "周" + weekDays[new Date(k.date.replace(/\//g, "-").replace(" ", "T")).getDay()];
                    lines.push(`${k.date} ${wd} 开:${k.open.toFixed(2)} 高:${k.high.toFixed(2)} 低:${k.low.toFixed(2)} 收:${k.close.toFixed(2)}`);
                }
                navigator.clipboard.writeText(lines.join("\n")).catch(() => {});
                showDualToast("已复制 " + (b - a + 1) + " 根K线数据到剪贴板");
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                render();
                return;
            }

            // === 普通模式：复制当前K线信息 ===
            if (_currentClipText) {
                navigator.clipboard.writeText(_currentClipText).catch(() => {});
            }
        }

        function onMouseLeave() { isDragging = false; mouseX = -1; mouseY = -1; canvas.style.cursor = "crosshair"; if (isDualWindow) { dualOffscreenState = false; dualHighlightRange = null; dualRedRange = null; dualNewZsData = null; dualShowNewZs = false; renderTop(); } else { render(); } }

        // 双窗口toast提示
        function showDualToast(msg) {
            let toast = document.getElementById("dual-toast");
            if (!toast) {
                toast = document.createElement("div");
                toast.id = "dual-toast";
                toast.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,0.85);color:#fff;padding:12px 28px;border-radius:8px;font-size:14px;z-index:9999;pointer-events:none;opacity:0;transition:opacity 0.3s;";
                document.body.appendChild(toast);
            }
            toast.textContent = msg;
            toast.style.opacity = "1";
            clearTimeout(toast._timer);
            toast._timer = setTimeout(() => { toast.style.opacity = "0"; }, 1000);
        }

        // 更新双窗口激活状态视觉提示
        function updateActiveWindowClass() {
            const mainDiv = document.getElementById("chart-main");
            const subDiv = document.getElementById("chart-sub");
            if (mainDiv) mainDiv.classList.toggle("dual-active", activeDualWindow === 'main');
            if (subDiv) subDiv.classList.toggle("dual-active", activeDualWindow === 'sub');
        }

        // 双窗口切换
        window.toggleDualWindow = function() {
            if (!chartData) return;
            const btn = document.getElementById("btn-dual");
            if (isDualWindow) {
                // 关闭双窗口
                isDualWindow = false;
                activeDualWindow = 'main';
                dualSubData = null;
                dualSubFreq = '';
                dualHighlightRange = null;
                dualRedRange = null;
                dualNewZsData = null;
                dualShowNewZs = false;
                dualNewZsLeftDate = "";
                dualNewZsRightDate = "";
                // 隐藏红框调试面板（该面板已暂时禁用，行内清理保留注释待恢复）
                // const dbg = document.getElementById("redframe-debug");
                // if (dbg) dbg.style.display = "none";
                btn.classList.remove("active");
                const isFuturesClose = chartData && chartData.meta && chartData.meta.market === 'futures';
                updateFreqButtonStates(isFuturesClose);
                // 恢复单canvas布局
                const container = document.getElementById("chart-container");
                const mainDiv = document.getElementById("chart-main");
                const subDiv = document.getElementById("chart-sub");
                if (mainDiv) mainDiv.remove();
                if (subDiv) subDiv.remove();
                canvas = mainCanvas; ctx = mainCtx;
                container.appendChild(canvas);
                resizeCanvas();
                // 期货：关闭双窗口后重连单SSE（后端单窗流从CSV恢复保存的选点）
                if (isFuturesClose) {
                    disconnectRealtime();
                    connectRealtimeInit(chartData.meta.symbol, currentFreq);
                    return;
                }
                // 股票：重新加载单窗口数据。双窗口请求(dual=1)按设计不加载CSV保存的
                // 选点，且双窗上窗选点时间会残留在 chartData.meta.saved_selection_date。
                // 这里重新请求不带 dual 的单窗口冷启动数据，让后端从 CSV 恢复选点
                // （AppEngine 585-590），并借 adjustViewForSavedPoint() 丢弃双窗残留、
                // 按恢复的选点全量显示。
                const code = chartData.meta.symbol;
                const freq = currentFreq;
                document.getElementById("loading").classList.remove("hidden");
                document.querySelector(".loading-text").textContent = "正在恢复单窗口数据...";
                fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + freq, { cache: "no-store" })
                    .then(resp => {
                        if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                        return resp.json();
                    })
                    .then(data => {
                        if (!data || !data.meta) {
                            throw new Error(data && data.error ? data.error : "API 返回数据缺少 meta 字段");
                        }
                        chartData = data;
                        document.getElementById("stock-name").textContent = chartData.meta.name;
                        document.getElementById("stock-code").textContent = chartData.meta.symbol;
                        document.title = "缠论分析 - " + chartData.meta.name;
                        viewCount = VIEW_COUNT;
                        adjustViewForSavedPoint();
                        viewOffset = Math.max(0, chartData.klines.length - viewCount);
                        if (chartData.klines.length < viewCount) viewOffset = 0;
                        const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                        document.getElementById("goto-date-input").value = lastDate;
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        updateFreqButtonStates(false);
                        updateRestartBtn();
                        updateDualBtn();
                        render();
                        generateStats();
                        loadAnnotations();
                        saveLastState();
                    })
                    .catch(err => {
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        console.error("恢复单窗口数据失败:", err);
                        render();
                    });
            } else {
                // 开启双窗口
                const subFreq = getDualSubFreq(currentFreq);
                if (!subFreq) {
                    // 5分周期无对应，提示
                    return;
                }
                isDualWindow = true;
                // 配对放宽：上次下窗周期对当前上窗仍合法则保持，否则默认配对
                dualSubFreq = (dualSubFreq && isValidStockDualPair(currentFreq, dualSubFreq))
                    ? dualSubFreq : subFreq;
                btn.classList.add("active");
                // 创建双窗口布局
                const container = document.getElementById("chart-container");
                // 保存原始canvas引用
                const origCanvas = mainCanvas;
                // 清空容器
                container.innerHTML = '';
                // 创建上面窗口
                const mainDiv = document.createElement("div");
                mainDiv.id = "chart-main";
                mainDiv.appendChild(origCanvas);
                container.appendChild(mainDiv);
                // 创建下面窗口
                const subDiv = document.createElement("div");
                subDiv.id = "chart-sub";
                subCanvas = document.createElement("canvas");
                subCtx = subCanvas.getContext("2d");
                subDiv.appendChild(subCanvas);
                container.appendChild(subDiv);
                // 添加下面窗口事件
                subCanvas.addEventListener("wheel", onSubWheel, { passive: false });
                subCanvas.addEventListener("mousedown", onSubMouseDown);
                subCanvas.addEventListener("mousemove", onSubMouseMove);
                subCanvas.addEventListener("mouseup", onSubMouseUp);
                subCanvas.addEventListener("mouseleave", onSubMouseLeave);
                subCanvas.addEventListener("dblclick", function(e) {
                    if (!dualSubData) return;
                    const rect = subCanvas.getBoundingClientRect();
                    const clickX = e.clientX - rect.left;
                    const clickY = e.clientY - rect.top;
                    // 临时切换全局变量以使用 getChartArea 等函数
                    const _savedCanvas = canvas, _savedCtx = ctx;
                    const _savedViewOffset = viewOffset, _savedViewCount = viewCount;
                    const _savedChartData = chartData, _savedFreq = currentFreq;
                    canvas = subCanvas; ctx = subCtx;
                    viewOffset = dualSubViewOffset; viewCount = dualSubViewCount;
                    chartData = dualSubData; currentFreq = dualSubFreq;
                    const area = getChartArea();
                    const volArea = getVolArea();
                    const macdTextArea = getMacdTextArea();
                    // 底部区域双击切换显示模式
                    const bottomTop = macdTextArea.y;
                    const bottomBottom = volArea.y + volArea.h;
                    if (clickX >= area.x && clickX <= area.x + area.w &&
                        clickY >= bottomTop && clickY <= bottomBottom) {
                        _subShowVolume = !_subShowVolume;
                        saveOverlaySettings();
                        canvas = _savedCanvas; ctx = _savedCtx;
                        viewOffset = _savedViewOffset; viewCount = _savedViewCount;
                        chartData = _savedChartData; currentFreq = _savedFreq;
                        renderBottom();
                        return;
                    }
                    const klines = getVisibleKlines();
                    if (!klines.length) { canvas = _savedCanvas; ctx = _savedCtx; viewOffset = _savedViewOffset; viewCount = _savedViewCount; chartData = _savedChartData; currentFreq = _savedFreq; return; }
                    const priceRange = getPriceRange(klines);
                    const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
                    const barStep = area.w / effectiveCount;
                    const barWidth = Math.max(1, barStep * 0.7);
                    const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
                    // 检查是否落在K线上
                    let clickedOnKline = false;
                    let clickedGlobalIdx = -1;
                    for (let i = 0; i < klines.length; i++) {
                        const k = klines[i];
                        const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                        const highY = priceToY(k.high, area, priceRange);
                        const lowY = priceToY(k.low, area, priceRange);
                        const halfW = barWidth / 2;
                        if (clickX >= x - halfW && clickX <= x + halfW &&
                            clickY >= highY && clickY <= lowY) {
                            clickedOnKline = true;
                            clickedGlobalIdx = Math.max(0, Math.floor(dualSubViewOffset)) + i;
                            break;
                        }
                    }
                    // 恢复全局变量
                    canvas = _savedCanvas; ctx = _savedCtx;
                    viewOffset = _savedViewOffset; viewCount = _savedViewCount;
                    chartData = _savedChartData; currentFreq = _savedFreq;
                    // 下窗双击选点限制：双窗采用「下窗对齐上窗」，
                    // 仅允许在上窗双击选点，前端限制下窗选点操作。
                    if (clickedOnKline) {
                        if (_savedChartData && _savedChartData.meta && _savedChartData.meta.is_replay) {
                            showDualToast("复盘模式，不支持选点");
                            return;
                        }
                        // 股票/期货双窗统一：仅上窗可选点，下窗只对齐展示
                        showDualToast("双窗口模式下仅支持在上窗选点");
                        return;
                    }
                    // 状态A：让下面窗口平移到对应区间
                    if (dualOffscreenState && dualHighlightRange && dualSubData) {
                        const hr = dualHighlightRange;
                        if (hr.startIdx >= 0 && hr.endIdx >= 0) {
                            const centerIdx = (hr.startIdx + hr.endIdx) / 2;
                            const totalKlines = dualSubData.klines.length;
                            let newOffset = Math.round(centerIdx - dualSubViewCount / 2);
                            if (newOffset < 0) newOffset = 0;
                            const maxOffset = Math.max(0, totalKlines - dualSubViewCount);
                            if (newOffset > maxOffset) newOffset = maxOffset;
                            dualSubViewOffset = newOffset;
                            dualHighlightRange = calcGrayRange(mouseX);
                            dualRedRange = dualHighlightRange ? dualHighlightRange.redRange : null;
                            dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                        } else {
                            showDualToast("请加载更多K线...");
                        }
                        renderBottom();
                        return;
                    }
                    // 默认：恢复下面窗口全视图
                    dualSubViewCount = VIEW_COUNT;
                    dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                    if (dualSubData.klines.length < dualSubViewCount) {
                        dualSubViewOffset = 0;
                    }
                    renderBottom();
                });
                // 恢复crosshair-info和ma-tooltip（container.innerHTML='' 已删除这两个元素）
                const crosshairInfo = document.createElement("div");
                crosshairInfo.className = "crosshair-info";
                crosshairInfo.id = "crosshair-info";
                container.appendChild(crosshairInfo);
                const maTooltip = document.createElement("div");
                maTooltip.className = "ma-tooltip";
                maTooltip.id = "ma-tooltip";
                container.appendChild(maTooltip);
                resizeCanvas();
                const code = chartData.meta.symbol;
                const isFutures = chartData.meta.market === 'futures';
                document.getElementById("loading").classList.remove("hidden");
                document.querySelector(".loading-text").textContent = "正在加载双窗口数据...";

                if (isFutures) {
                    // 期货双窗口：使用 connectRealtimeDual，自带完整的 init/update/error 处理与自动跟随逻辑
                    connectRealtimeDual(code, currentFreq, subFreq);
                } else {
                    // 股票双窗口：HTTP 请求（P2：显式透传下窗周期 dualSubFreq，
                    // 保持开启时校验过的配对，不依赖后端缺省映射）
                    fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + currentFreq
                        + "&dual=1&sub_freq=" + dualSubFreq)
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            if (data.sub) {
                                chartData = data;
                                dualSubData = data.sub;
                                dualSubViewCount = VIEW_COUNT;
                                dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                                if (dualSubData.klines.length < dualSubViewCount) {
                                    dualSubViewOffset = 0;
                                }
                                document.getElementById("loading").classList.add("hidden");
                                document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                                updateFreqButtonStates(false);
                                render();
                            } else {
                                throw new Error("服务端未返回子级别数据");
                            }
                        })
                        .catch(err => {
                            alert("加载下面窗口数据失败: " + err.message);
                            isDualWindow = false;
                            activeDualWindow = 'main';
                            dualSubData = null;
                            dualSubFreq = '';
                            btn.classList.remove("active");
                            updateFreqButtonStates(false);
                            const container2 = document.getElementById("chart-container");
                            container2.innerHTML = '';
                            const ci2 = document.createElement("div");
                            ci2.className = "crosshair-info";
                            ci2.id = "crosshair-info";
                            container2.appendChild(ci2);
                            const mt2 = document.createElement("div");
                            mt2.className = "ma-tooltip";
                            mt2.id = "ma-tooltip";
                            container2.appendChild(mt2);
                            container2.appendChild(origCanvas);
                            canvas = mainCanvas; ctx = mainCtx;
                            resizeCanvas();
                            render();
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        });
                }
            }
        };

        // 下面窗口的事件处理
        function onSubWheel(e) {
            e.preventDefault();
            if (!dualSubData) return;
            activeDualWindow = 'sub';
            updateActiveWindowClass();
            updateSlider();
            updateFreqButtonStates(chartData && chartData.meta && chartData.meta.market === 'futures');
            const savedCanvas = canvas; const savedCtx = ctx;
            const savedViewOffset = viewOffset; const savedViewCount = viewCount;
            canvas = subCanvas; ctx = subCtx;
            viewOffset = dualSubViewOffset; viewCount = dualSubViewCount;
            const rect = subCanvas.getBoundingClientRect();
            const bMouseX = e.clientX - rect.left;
            const area = getChartArea();
            const klines = dualSubData.klines;
            const barStep = area.w / viewCount;
            const ratio = Math.max(0, Math.min(1, (bMouseX - area.x) / area.w));
            const mouseKIdx = ratio * viewCount;
            const zoomFactor = 1.15;
            const newViewCount = e.deltaY > 0
                ? Math.min(klines.length, Math.ceil(viewCount * zoomFactor))
                : Math.max(3, Math.round(viewCount / zoomFactor));
            if (newViewCount === viewCount) { canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount; return; }
            const maxOffset = klines.length - newViewCount;
            if (mouseKIdx >= viewCount - 1) {
                const rightGlobalIdx = viewOffset + viewCount - 1;
                dualSubViewCount = newViewCount;
                dualSubViewOffset = Math.max(0, Math.min(maxOffset, rightGlobalIdx - newViewCount + 1));
            } else {
                const anchorGlobalIdx = viewOffset + mouseKIdx;
                let newViewOffset = anchorGlobalIdx - ratio * newViewCount;
                newViewOffset = Math.max(0, newViewOffset);
                if (newViewOffset > maxOffset) newViewOffset = maxOffset;
                dualSubViewCount = newViewCount;
                dualSubViewOffset = newViewOffset;
            }
            canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
            renderBottom();
        }

        function onSubMouseDown(e) {
            dualSubIsDragging = true;
            dualSubDragStartX = e.clientX;
            dualSubDragStartOffset = dualSubViewOffset;
            dualSubMouseDownX = e.clientX;
            dualSubMouseDownY = e.clientY;
            subCanvas.style.cursor = "grabbing";
            activeDualWindow = 'sub';
            updateActiveWindowClass();
            updateSlider();
            updateFreqButtonStates(chartData && chartData.meta && chartData.meta.market === 'futures');
        }

        function onSubMouseMove(e) {
            const rect = subCanvas.getBoundingClientRect();
            dualSubMouseX = e.clientX - rect.left;
            dualSubMouseY = e.clientY - rect.top;
            if (dualSubIsDragging && dualSubData) {
                const savedCanvas = canvas; const savedCtx = ctx;
                const savedViewOffset = viewOffset; const savedViewCount = viewCount;
                canvas = subCanvas; ctx = subCtx;
                viewOffset = dualSubViewOffset; viewCount = dualSubViewCount;
                dualSubViewOffset = dualSubDragStartOffset - (e.clientX - dualSubDragStartX) / (getChartArea().w / viewCount);
                dualSubViewOffset = Math.max(0, Math.min(dualSubData.klines.length - dualSubViewCount, dualSubViewOffset));
                canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
            }
            renderBottom();
        }

        function onSubMouseUp(e) {
            dualSubIsDragging = false;
            subCanvas.style.cursor = "crosshair";
            // 只处理左键点击（非拖拽）
            if (e.button !== 0 || Math.abs(e.clientX - dualSubMouseDownX) >= 5 || Math.abs(e.clientY - dualSubMouseDownY) >= 5) return;
            if (_subCurrentGlobalIdx < 0 || !dualSubData) return;

            // === Ctrl+点击：区间选择模式切换（底部窗口）===
            if (e.ctrlKey) {
                if (_rangeSelect.mode === 'IDLE') {
                    _rangeSelect = {
                        mode: 'SELECTED_A',
                        startIdx: _subCurrentGlobalIdx,
                        startFreq: dualSubFreq,
                        startSymbol: dualSubData.meta.symbol
                    };
                    const startDate = dualSubData.klines[_subCurrentGlobalIdx].date.split(' ')[0];
                    showDualToast("区间起点: " + startDate + "，点击另一根K线完成选择");
                    renderBottom();
                } else {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("区间选择已取消");
                    renderBottom();
                }
                return;
            }

            // === 普通点击：如果在选择模式中，完成区间选择（底部窗口）===
            if (_rangeSelect.mode === 'SELECTED_A') {
                // 验证：同一股票、同一周期
                if (_rangeSelect.startFreq !== dualSubFreq || _rangeSelect.startSymbol !== dualSubData.meta.symbol) {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("股票或周期已变更，区间选择已取消");
                    return;
                }
                const a = Math.min(_rangeSelect.startIdx, _subCurrentGlobalIdx);
                const b = Math.max(_rangeSelect.startIdx, _subCurrentGlobalIdx);
                const klines = dualSubData.klines;
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                const lines = [];
                for (let i = a; i <= b; i++) {
                    const k = klines[i];
                    const prevK = i > 0 ? klines[i - 1] : null;
                    const prevClose = prevK ? prevK.close : k.open;
                    const changeVal = k.close - prevClose;
                    const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
                    const sign = changeVal >= 0 ? "+" : "";
                    const wd = "周" + weekDays[new Date(k.date.replace(/\//g, "-").replace(" ", "T")).getDay()];
                    lines.push(`${k.date} ${wd} 开:${k.open.toFixed(2)} 高:${k.high.toFixed(2)} 低:${k.low.toFixed(2)} 收:${k.close.toFixed(2)}`);
                }
                navigator.clipboard.writeText(lines.join("\n")).catch(() => {});
                showDualToast("已复制 " + (b - a + 1) + " 根K线数据到剪贴板");
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                renderBottom();
                return;
            }

            // === 普通模式：复制当前K线信息 ===
            if (_subClipText) {
                navigator.clipboard.writeText(_subClipText).catch(() => {});
            }
        }

        function onSubMouseLeave() {
            dualSubIsDragging = false;
            dualSubMouseX = -1; dualSubMouseY = -1;
            subCanvas.style.cursor = "crosshair";
            renderBottom();
        }

        window.toggleOverlay = function(type) {
            if (type === "bi") { showBi = !showBi; document.getElementById("btn-bi").classList.toggle("active", showBi); }
            else if (type === "fx") { showFx = !showFx; document.getElementById("btn-fx").classList.toggle("active", showFx); }
            else if (type === "zs") { showZs = !showZs; document.getElementById("btn-zs").classList.toggle("active", showZs); }
            else if (type === "seg") { showSeg = !showSeg; document.getElementById("btn-seg").classList.toggle("active", showSeg); }
            else if (type === "bsp") { showBsp = !showBsp; document.getElementById("btn-bsp").classList.toggle("active", showBsp); }
            saveOverlaySettings();
            render();
        };

        // 辅助：根据chartData中的saved_selection_date恢复「取消选点」菜单项状态
        function updateRestartBtn() {
            var hasPoint = chartData && chartData.meta && chartData.meta.saved_selection_date;
            var isReplay = chartData && chartData.meta && chartData.meta.is_replay;
            _restartEnabled = hasPoint && !isDualWindow && !isReplay;
        }

        function updateDualBtn() {
            // 双窗中始终可点（用于退出），仅在非双窗时按入口规则约束
            if (isDualWindow) {
                document.getElementById("btn-dual").disabled = false;
                return;
            }
            const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            if (isFutures) {
                // 期货：30m/5m/1m 可双窗口，15s 不可
                document.getElementById("btn-dual").disabled = (currentFreq === '15s');
            } else {
                // 股票：w/d/30m 可双窗口，5m 不可
                document.getElementById("btn-dual").disabled = (currentFreq === '5m');
            }
        }

        // ============================================================
        // 重启：清除选点，按冷启动重新加载
        // ============================================================
        window.cancelSelectedPoint = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!chartData || !chartData.meta) return;
            // 双窗口模式和复盘模式不允许重置
            if (isDualWindow) { showDualToast("双窗口模式，不支持重置"); return; }
            if (chartData.meta.is_replay) { showDualToast("复盘模式，不支持重置"); return; }
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            const isFutures = chartData.meta.market === 'futures';
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在重置...";

            // 期货：清除选点 + 冷启动重连SSE（无start_time）
            if (isFutures) {
                fetch("/api/futures/" + encodeURIComponent(code) + "/delete/point?freq=" + freq, { method: "DELETE" })
                    .then(resp => resp.json())
                    .then(() => {
                        // 不隐藏loading，交给connectRealtimeInit的init事件来隐藏
                        // 如果提前隐藏loading，会导致SSE重连失败时没有任何加载反馈
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        connectRealtimeInit(code, freq);  // 冷启动，不带start_time
                    })
                    .catch(err => {
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        alert("重置失败: " + err.message);
                    });
                return;
            }

            // 股票：清除选点 + 冷启动HTTP
            // Step 1: 调用后端清除CSV中该周期选点
            fetch("/api/stocks/" + encodeURIComponent(code) + "/delete/point?freq=" + freq, { method: "DELETE" })
                .then(resp => resp.json())
                .then(() => {
                    // Step 2: 冷启动重新加载（P2：股票双窗也显式透传 sub_freq）
                    return fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + freq + (isDualWindow && getDualSubFreq(freq) ? "&dual=1" : "") + (isDualWindow && dualSubFreq && freqLevel(freq) > freqLevel(dualSubFreq) ? "&sub_freq=" + dualSubFreq : ""));
                })
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "重置失败"); });
                    return resp.json();
                })
                .then(data => {
                    // 全文替换 chartData
                    chartData = data;
                    if (chartData.meta.freq === "5分钟") {
                        currentFreq = "5m";
                    } else if (chartData.meta.freq === "30分钟") {
                        currentFreq = "30m";
                    } else if (chartData.meta.freq === "周线") {
                        currentFreq = "w";
                    } else {
                        currentFreq = "d";
                    }
                    updateDateInputType();
                    document.getElementById("btn-d").classList.toggle("active", currentFreq === "d");
                    document.getElementById("btn-w").classList.toggle("active", currentFreq === "w");
                    document.getElementById("btn-30m").classList.toggle("active", currentFreq === "30m");
                    document.getElementById("btn-5m").classList.toggle("active", currentFreq === "5m");
                    viewCount = VIEW_COUNT;
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    resizeCanvas();
                    render();
                    generateStats();
                    updateRestartBtn();
                    updateDualBtn();
                    // 双窗口模式：从 data.sub 恢复子级别数据
                    if (isDualWindow && data.sub) {
                        dualSubData = data.sub;
                        dualSubViewCount = VIEW_COUNT;
                        dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                        if (dualSubData.klines.length < dualSubViewCount) {
                            dualSubViewOffset = 0;
                        }
                    }
                })
                .catch(err => {
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    alert("重置失败: " + err.message);
                });
        };


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.KLineChart = {
            render, renderSingle, renderTop, renderBottom, resizeCanvas,
        priceToY, yToPrice, getChartArea, getVisibleKlines, getPriceRange,
        drawCandles, drawBiLines, drawZs, drawBspMarkers, drawMaLines,
        drawCrosshair, drawDateAxis, onWheel, toggleOverlay, toggleDualWindow,
        applyOverlayButtonStates, cancelSelectedPoint
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] NavToolbar —— 导航工具栏组件（频率切换 / 日期跳转 / 坐标系统）
        // 对外接口（ChanApp.components.NavToolbar）: switchFreq, gotoDate, handleDateChange, handleDateInput, onCoordSystemChange, initCoordSystemRadio, updateFreqButtonStates, updateWeekday, adjustViewForSavedPoint
// ══════════════════════════════════════════════════════════════════

        // 坐标系切换（设置抽屉内 radio 触发）
        window.onCoordSystemChange = function(el) {
            if (el.value === 'log') {
                _logScale = true;
            } else {
                _logScale = false;
            }
            saveOverlaySettings();
            render();
        };

        window.initCoordSystemRadio = function() {
            var radios = document.getElementsByName('coord-system');
            for (var i = 0; i < radios.length; i++) {
                radios[i].checked = (_logScale && radios[i].value === 'log') || (!_logScale && radios[i].value === 'linear');
            }
        };

        function isIntradayFreq(freq) { return INTRADAY_FREQS_JS.indexOf(freq) >= 0; }

        // K线日期 → 输入框格式
        // K线日期: "2026/07/02" / "2026/07/02 10:35" / "2026/07/02 10:35:00"
        // date: "2026-07-02"  /  datetime-local: "2026-07-02T10:35"
        function klineDateToInput(klineDate, freq) {
            if (!klineDate) return "";
            var d = klineDate.replace(/\//g, "-");
            if (isIntradayFreq(freq)) {
                var dt = d.slice(0, 19);       // "YYYY-MM-DD HH:MM:SS"（15秒含秒，分钟级不越界）
                return dt.replace(" ", "T");
            }
            return d.slice(0, 10);
        }

        // 输入框值 → 后端API格式
        // date: "2026-07-02" / datetime-local: "2026-07-02T10:35"
        // API: "2026-07-02" / "2026-07-02 10:35"
        function inputDateToApi(inputVal, freq) {
            if (!inputVal) return "";
            if (isIntradayFreq(freq)) return inputVal.replace("T", " ").replace(/-/g, "/");
            return inputVal.slice(0, 10).replace(/-/g, "/");
        }

        // 切换输入框 type 属性（date ↔ datetime-local）
        function updateDateInputType() {
            var input = document.getElementById("goto-date-input");
            var weekday = document.getElementById("date-weekday");
            var isIntra = isIntradayFreq(currentFreq);
            var oldVal = input.value;
            if (isIntra) {
                input.type = "datetime-local";
                input.step = (currentFreq === "15s") ? "15" : "60";
                // 股票：限定盘中时间 09:00-15:59；期货：全天
                var isStock = chartData && chartData.meta && chartData.meta.symbol && !isFuturesCode(chartData.meta.symbol);
                if (isStock) {
                    input.min = "1990-01-01T09:00";
                    input.max = "2099-12-31T15:59";
                } else {
                    input.min = "1990-01-01T00:00";
                    input.max = "2099-12-31T23:59";
                }
                if (currentFreq === "15s") {
                    input.style.width = "190px";
                    if (weekday) weekday.style.right = "28px";
                } else {
                    input.style.width = "170px";
                    if (weekday) weekday.style.right = "28px";
                }
                if (oldVal && oldVal.indexOf("T") < 0) oldVal = oldVal + "T09:30";
            } else {
                input.type = "date";
                input.step = "1";
                input.min = "1990-01-01";
                input.max = "2099-12-31";
                input.style.width = "130px";
                if (oldVal && oldVal.indexOf("T") >= 0) oldVal = oldVal.slice(0, 10);
                if (weekday) weekday.style.right = "28px";
            }
            input.value = oldVal;
            // datetime-local：picker 打开时记录原始值
            if (isIntra) {
                input.onfocus = function() {
                    var v = input.value;
                    if (!v) return;
                    _datePickerInteracted = false;
                    _datePickerInputCount = 0;
                    _dateFocusOriginal = v;
                };
            } else {
                input.onfocus = null;
            }

            // 箭头提示（仅 title 文案，不触发任何跳转；左右箭头已停用点击无响应）
            var la = document.getElementById("date-arrow-left");
            var ra = document.getElementById("date-arrow-right");
            if (la) la.title = (currentFreq === "d") ? "前一天" : "前一根";
            if (ra) ra.title = (currentFreq === "d") ? "后一天" : "后一根";
        }

        // 周期级别：数值越大级别越高（w=6 > d=5 > 30m=4 > 5m=3 > 1m=2 > 15s=1）
        // 用于双窗口校验：下窗周期级别必须严格小于上窗周期级别
        function freqLevel(freq) {
            const levels = {'w': 6, 'd': 5, '30m': 4, '5m': 3, '1m': 2, '15s': 1};
            return levels[freq] || 0;
        }

        // 周期中文标签（用于弹窗提示）
        function freqLabel(freq) {
            const labels = {'w': '周线', 'd': '日线', '30m': '30分钟', '5m': '5分钟', '1m': '1分钟', '15s': '15秒'};
            return labels[freq] || freq;
        }

        // 根据市场类型更新频率按钮的启用/禁用状态
        function updateFreqButtonStates(isFutures) {
            // 股票禁用 1m/15s，期货禁用 d/w
            document.getElementById('btn-d').disabled = isFutures;
            document.getElementById('btn-w').disabled = isFutures;
            document.getElementById('btn-1m').disabled = !isFutures;
            document.getElementById('btn-15s').disabled = !isFutures;
            // 共享周期：30m 始终启用
            document.getElementById('btn-30m').disabled = false;
            // 5m: 期货双窗口→全部启用（上下窗解耦）；股票双窗口→按焦点窗口（配对放宽）
            if (isDualWindow && isFutures) {
                document.getElementById('btn-5m').disabled = false;
                // 期货双窗口：上下窗独立切换，15s不再禁用
            } else if (isDualWindow && !isFutures) {
                if (activeDualWindow === 'sub') {
                    // 股票下窗焦点：仅启用当前上窗配对空间内的周期（w 不可作下窗）
                    const subs = STOCKS_DUAL_PAIRS_JS[currentFreq] || [];
                    document.getElementById('btn-w').disabled = true;
                    document.getElementById('btn-d').disabled = subs.indexOf('d') < 0;
                    document.getElementById('btn-30m').disabled = subs.indexOf('30m') < 0;
                    document.getElementById('btn-5m').disabled = subs.indexOf('5m') < 0;
                } else {
                    // 股票上窗焦点：5m 为最小周期无下窗，禁用
                    document.getElementById('btn-5m').disabled = true;
                }
            } else {
                document.getElementById('btn-5m').disabled = false;
            }
            // 同步 active 状态：双窗口下根据焦点窗口决定高亮
            document.querySelectorAll('.freq-btn').forEach(b => b.classList.remove('active'));
            const highlightFreq = (isDualWindow && activeDualWindow === 'sub') ? dualSubFreq : currentFreq;
            const activeBtn = document.getElementById('btn-' + highlightFreq);
            if (activeBtn) activeBtn.classList.add('active');
            // 市场量能按钮可用性（仅上证指数日K）
            updateAmoButtonState();
        }

        window.switchFreq = function(freq) {
            if (!chartData) return;
            const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            // 期货双窗口下窗焦点：独立切换下窗周期，不联动上窗
            if (isDualWindow && activeDualWindow === 'sub' && isFutures) {
                if (dualSubFreq === freq) return;
                // 校验：下窗周期必须严格小于上窗周期，否则弹窗提示并取消
                if (freqLevel(freq) >= freqLevel(currentFreq)) {
                    alert("下窗周期必须小于上窗周期，当前上窗周期为" + freqLabel(currentFreq)
                        + "，无法切换到" + freqLabel(freq));
                    return;
                }
                // 切换周期时取消区间选择
                if (_rangeSelect.mode === 'SELECTED_A') {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                }
                dualSubFreq = freq;
                updateDateInputType();
                updateDualBtn();
                updateFreqButtonStates(isFutures);
                const code = document.getElementById("stock-code-input").value.trim() || chartData.meta.symbol;
                if (code) {
                    document.getElementById("loading").classList.remove("hidden");
                    disconnectRealtime();
                    connectRealtimeDual(code, currentFreq, freq);
                }
                return;
            }
            // 股票双窗口下窗焦点：独立切换下窗周期（配对放宽），不联动上窗
            // （与期货同交互；重走 analyze 双窗接口，响应 data.sub 即新下窗数据）
            if (isDualWindow && activeDualWindow === 'sub' && !isFutures) {
                if (dualSubFreq === freq) return;
                // 校验：新下窗周期须在当前上窗的配对空间内（P2：3对 → 6对）
                if (!isValidStockDualPair(currentFreq, freq)) {
                    const subs = (STOCKS_DUAL_PAIRS_JS[currentFreq] || []).map(freqLabel).join("、");
                    alert("下窗周期配对无效: " + freqLabel(currentFreq) + "+" + freqLabel(freq)
                        + (subs ? "（" + freqLabel(currentFreq) + " 可选 " + subs + "）" : "（当前上窗无下窗可选）"));
                    return;
                }
                // 切换周期时取消区间选择
                if (_rangeSelect.mode === 'SELECTED_A') {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                }
                dualSubFreq = freq;
                updateDateInputType();
                updateDualBtn();
                updateFreqButtonStates(isFutures);
                const code2 = document.getElementById("stock-code-input").value.trim() || chartData.meta.symbol;
                if (code2) {
                    document.getElementById("loading").classList.remove("hidden");
                    fetch("/api/stocks/" + encodeURIComponent(code2) + "/analyze?freq=" + currentFreq
                        + "&dual=1&sub_freq=" + dualSubFreq)
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            chartData = data;
                            updateRestartBtn();
                            updateDualBtn();
                            if (data.sub) {
                                dualSubData = data.sub;
                                dualSubViewCount = VIEW_COUNT;
                                dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                                if (dualSubData.klines.length < dualSubViewCount) {
                                    dualSubViewOffset = 0;
                                }
                            }
                            document.getElementById("loading").classList.add("hidden");
                            render();
                            generateStats();
                            loadAnnotations();
                            saveLastState();
                        })
                        .catch(err => {
                            alert("切换下窗周期失败: " + err.message);
                            document.getElementById("loading").classList.add("hidden");
                        });
                }
                return;
            }
            if (currentFreq === freq) return;
            // 期货双窗口校验：上窗周期必须严格大于下窗周期，否则弹窗提示并取消
            if (isDualWindow && isFutures && freqLevel(freq) <= freqLevel(dualSubFreq)) {
                alert("上窗周期必须大于下窗周期，当前下窗周期为" + freqLabel(dualSubFreq)
                    + "，无法切换到" + freqLabel(freq));
                return;
            }
            // 股票双窗口（配对放宽）：新上窗周期无任何下窗可选 → 拒绝切换
            // （5m 为股票最小周期）；当前下窗仍为合法配对则保持，否则回退默认配对
            if (isDualWindow && !isFutures) {
                if (!STOCKS_DUAL_PAIRS_JS[freq]) {
                    alert("上窗周期必须大于下窗周期，" + freqLabel(freq) + "为股票最小周期，双窗口下不可选");
                    return;
                }
                if (!isValidStockDualPair(freq, dualSubFreq)) {
                    dualSubFreq = getDualSubFreq(freq);
                }
            }
            // 切换周期时取消区间选择
            if (_rangeSelect.mode === 'SELECTED_A') {
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
            }
            currentFreq = freq;
            updateDateInputType();
            updateDualBtn();
            if (isFutures) {
                lastFuturesFreq = freq; // 期货上下文切换周期，记录
            } else {
                lastStockFreq = freq;   // 股票上下文切换周期，记录
            }
            updateFreqButtonStates(isFutures);
            // 股票双窗口：切换周期始终作用于上窗，切换后焦点回到上窗
            if (isDualWindow && !isFutures) {
                activeDualWindow = 'main';
                updateActiveWindowClass();
                updateSlider();
            }
            // 切换周期后重新加载数据
            const code = document.getElementById("stock-code-input").value.trim() || chartData.meta.symbol;
            if (code) {
                document.getElementById("loading").classList.remove("hidden");
                // 期货：跳过HTTP，直接重连SSE（初始快照+增量合一）
                if (isFutures) {
                    disconnectRealtime();
                    if (isDualWindow) {
                        // 双窗口模式：上窗周期变，下窗保持不变
                        connectRealtimeDual(code, freq, dualSubFreq);
                    } else {
                        connectRealtimeInit(code, freq);
                    }
                    return;
                }
                fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + freq
                    + (isDualWindow && getDualSubFreq(freq) ? "&dual=1" : "")
                    + (isDualWindow && dualSubFreq && freqLevel(freq) > freqLevel(dualSubFreq) ? "&sub_freq=" + dualSubFreq : ""))
                    .then(resp => {
                        if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                        return resp.json();
                    })
                    .then(data => {
                        chartData = data;
                        updateRestartBtn();
                        updateDualBtn();
                        viewCount = VIEW_COUNT;
                        adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                        viewOffset = Math.max(0, chartData.klines.length - viewCount);
                        // K线不足一屏时右对齐
                        if (chartData.klines.length < viewCount) {
                            viewOffset = 0;
                        }
                        document.getElementById("stock-name").textContent = chartData.meta.name;
                        document.getElementById("stock-code").textContent = chartData.meta.symbol;
                        document.title = "缠论分析 - " + chartData.meta.name;
                        const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, freq);
                        document.getElementById("goto-date-input").value = lastDate;
                        updateWeekday();
                        // 双窗口模式：从 data.sub 获取子级别数据
                        if (isDualWindow) {
                            // 下窗周期已在切换前校验/回退（保持合法配对不变），
                            // 此处不再重置为默认配对；响应 data.sub 即该配对的下窗数据
                            if (data.sub) {
                                dualSubData = data.sub;
                                dualSubViewCount = VIEW_COUNT;
                                dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                                if (dualSubData.klines.length < dualSubViewCount) {
                                    dualSubViewOffset = 0;
                                }
                            } else {
                                // 响应缺 sub（异常态）：保持旧下窗数据，仅提示
                                console.warn("[switchFreq] 双窗口响应缺少 data.sub");
                            }
                        }
                        document.getElementById("loading").classList.add("hidden");
                        render();
                        generateStats();
                        loadAnnotations();
                        saveLastState(); // 保存状态
                        startRealtimeIfFutures(data);
                    })
                    .catch(err => {
                        alert("切换周期失败: " + err.message);
                        document.getElementById("loading").classList.add("hidden");
                    });
            }
        };

        // 根据保存的选点日期，动态调整 viewCount 和 viewOffset
        // 选点后后端已过滤，klines只包含选点之后的K线，直接全部显示
        function adjustViewForSavedPoint() {
            if (!chartData || !chartData.meta) return;
            if (!chartData.meta.saved_selection_date) return;
            if (!chartData.klines || chartData.klines.length === 0) return;
            viewCount = chartData.klines.length;
            viewOffset = 0;
        }

        window.gotoDate = function() {
            // 键盘Enter提供了精确日期，应在重置前捕获，用于跳过 isToday 安全网
            const keyEnter = _dateKeyEnter;
            // 重置所有日期输入标志位，避免上次手动输入/键盘操作阻塞后续日历点击
            _dateKeyEnter = false;
            _dateKeyArrow = false;
            _dateManualTyping = false;
            _datePickerInteracted = false;
            _datePickerInputCount = 0;
            if (!chartData) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            const dateStr = document.getElementById("goto-date-input").value.trim();
            if (!dateStr) return;
            const apiDate = inputDateToApi(dateStr, freq);
            // 日期是今天 → 冷启动（不传 end_date，加载全部K线）
            // 用本地日期避免 UTC 时区偏移（如 UTC+8 凌晨 0-8 点 toISOString 会返回昨天）
            const now = new Date();
            const todayStr = now.getFullYear() + '-' + String(now.getMonth()+1).padStart(2,'0') + '-' + String(now.getDate()).padStart(2,'0');
            const isToday = dateStr.startsWith(todayStr);
            // 期货：判断是否"回到最新/实时"——请求时间 ≥ 最后一根K线时间才算
            // （日内期货所有K线都是今天，不能用 isToday 判断，否则所有日内复盘都被拦截）
            const isFutures = chartData.meta.market === 'futures';
            const lastKlineInput = (chartData.klines && chartData.klines.length > 0)
                ? klineDateToInput(chartData.klines[chartData.klines.length - 1].date, freq)
                : "";
            const wantLive = isFutures && dateStr >= lastKlineInput;
            if (wantLive) {
                document.getElementById("goto-date-input").disabled = true;
                document.getElementById("loading").classList.remove("hidden");
                document.querySelector(".loading-text").textContent = "正在恢复实时行情...";
                if (isDualWindow && dualSubFreq) {
                    disconnectRealtime();
                    // 双窗口模式：保持用户独立选择的下窗周期
                    connectRealtimeDual(code, freq, dualSubFreq);
                } else {
                    // 保留选点起始时间（若有），与手选后的SSE重连逻辑一致
                    const savedDate = chartData.meta.saved_selection_date || null;
                    connectRealtimeInit(code, freq, savedDate);
                }
                // 不在这里隐藏loading，SSE的init事件回调会处理loading隐藏和input恢复
                return;
            }
            // 复盘模式下断开实时连接（请求时间早于最新K线才走到这里）
            disconnectRealtime();
            // ── 期货：复盘到过去 → 走 SSE 软断开（AppSSE end_time），不复用股票路由 ──
            // end_time 软断开承载期货复盘：连接保持存活、K线冻结在边界。
            // 复盘选日期/复盘至此统一由 gotoDate 并入 SSE，不走股票路由。
            if (isFutures) {
                document.getElementById("goto-date-input").disabled = true;
                document.getElementById("loading").classList.remove("hidden");
                document.querySelector(".loading-text").textContent = "正在复盘计算，请稍候...";
                if (isDualWindow && dualSubFreq) {
                    connectRealtimeDual(chartData.meta.symbol, freq, dualSubFreq, apiDate);
                } else {
                    connectRealtimeInit(chartData.meta.symbol, freq, realtimeStartTime, apiDate);
                }
                return;
            }
            // 股票：isToday安全网只给日历"今天"用（Edge时间未变时兜底）
            // 键盘Enter/右键复盘至此有精确日期 → 跳过isToday安全网，始终传end_date
            const needEndDate = (!isToday || keyEnter);
            const url = "/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + freq
                + (needEndDate ? "&end_date=" + encodeURIComponent(apiDate) : "")
                + (isDualWindow && getDualSubFreq(freq) ? "&dual=1" : "")
                + (isDualWindow && dualSubFreq && freqLevel(freq) > freqLevel(dualSubFreq) ? "&sub_freq=" + dualSubFreq : "");
            document.getElementById("goto-date-input").disabled = true;
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在复盘计算，请稍候...";
            fetch(url)
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "跳转失败"); });
                    return resp.json();
                })
                .then(data => {
                    chartData = data;
                    updateRestartBtn();
                    updateDualBtn();
                    // 双窗口模式：从 data.sub 恢复子级别数据
                    if (isDualWindow && data.sub) {
                        dualSubData = data.sub;
                        dualSubViewCount = VIEW_COUNT;
                        dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                        if (dualSubData.klines.length < dualSubViewCount) {
                            dualSubViewOffset = 0;
                        }
                    }
                    viewCount = VIEW_COUNT;
                    adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    // 复盘后输入框显示实际最后一根K线日期
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    resizeCanvas();
                    render();
                    loadAnnotations();
                })
                .catch(err => {
                    alert("跳转失败: " + err.message);
                })
                .finally(() => {
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    document.getElementById("goto-date-input").disabled = false;
                });
        };

        window.handleDateKeydown = function(e) {
            if (e.key === 'Enter') { _dateKeyEnter = true; gotoDate(); return; }
            if (e.key.startsWith('Arrow')) { _dateKeyArrow = true; return; }
            if (e.key !== 'Tab' && e.key !== 'Escape') { _dateManualTyping = true; }
        };

        window.handleDateChange = function() {
            updateWeekday();
            // 键盘/手动输入 → 不触发（Enter 已在 handleDateKeydown 中处理）
            if (_dateKeyEnter) { _dateKeyEnter = false; return; }
            if (_dateKeyArrow) { _dateKeyArrow = false; return; }
            if (_dateManualTyping) { _dateManualTyping = false; return; }
            // input 已处理（datetime-local "今天"），change 跳过避免重复
            if (_dateInputTriggered) { _dateInputTriggered = false; return; }
            // datetime-local 正常完成（用户选完日期+小时+分钟，picker关闭）→ 触发
            _dateFocusOriginal = "";
            _datePickerInteracted = false;
            _datePickerInputCount = 0;
            // 期货兜底：Edge点击日历"今天"时handleDateInput的检测可能未触发，
            // 此时dateStr的日期=今天但时间未变，wantLive可能为false，应强制设为23:59再判断
            if (chartData && chartData.meta && chartData.meta.market === 'futures') {
                var input2 = document.getElementById("goto-date-input");
                if (input2.type === "datetime-local") {
                    var now3 = new Date();
                    var ts3 = now3.getFullYear() + '-' + String(now3.getMonth()+1).padStart(2,'0') + '-' + String(now3.getDate()).padStart(2,'0');
                    if (input2.value.startsWith(ts3)) {
                        input2.value = ts3 + 'T23:59';
                    }
                }
            }
            gotoDate();
        };

        window.handleDateBlur = function() {
            const input = document.getElementById("goto-date-input");
            var v = input.value;
            // 期货兜底：复盘后点击日历"今天"，Edge的step="15"输入框可能不触发input/change事件
            // 在blur时检测：如果当前处于复盘状态(chartData.meta.is_replay)且日期=今天 → 强制设23:59并触发gotoDate
            if (chartData && chartData.meta && chartData.meta.market === 'futures'
                && chartData.meta.is_replay && input.type === "datetime-local") {
                var nowB = new Date();
                var tsB = nowB.getFullYear() + '-' + String(nowB.getMonth()+1).padStart(2,'0') + '-' + String(nowB.getDate()).padStart(2,'0');
                var datePart = v.split('T')[0] || "";
                if (datePart === tsB) {
                    // 用户点了"今天"但input/change未触发 → 直接恢复实时
                    input.value = tsB + 'T23:59';
                    _dateFocusOriginal = "";
                    _datePickerInteracted = false;
                    _datePickerInputCount = 0;
                    gotoDate();
                    return;
                }
            }
            // picker 打开后用户未交互 → 恢复原始值
            if (_dateFocusOriginal && !_datePickerInteracted) {
                input.value = _dateFocusOriginal;
                _dateFocusOriginal = "";
            }
            _dateFocusOriginal = "";
            _datePickerInteracted = false;
            _datePickerInputCount = 0;
            v = input.value;
            if (!v) return;
            const parts = v.split('-');
            if (parts.length === 3) {
                const d = parseInt(parts[2], 10);
                if (!isNaN(d) && d > 31) {
                    input.value = parts[0] + '-' + parts[1] + '-31';
                }
            }
            // 股票 datetime-local：小时超出盘中范围(09-15)则自动修正
            if (input.type === "datetime-local" && chartData && chartData.meta && !isFuturesCode(chartData.meta.symbol)) {
                var p = input.value.split('T');
                if (p.length === 2) {
                    var tp = p[1].split(':');
                    var hh = parseInt(tp[0], 10);
                    if (hh < 9) input.value = p[0] + 'T09:' + tp[1];
                    else if (hh > 15) input.value = p[0] + 'T15:' + tp[1];
                }
            }
            updateWeekday();
        };

        window.handleDateInput = function(e) {
            const input = e.target;
            const val = input.value;
            if (!val) return;
            // 年份部分超过4位时截断到4位
            const firstDash = val.indexOf('-');
            if (firstDash === -1) {
                if (val.length > 4) {
                    input.value = val.substring(0, 4);
                    setTimeout(() => { try { input.setSelectionRange(5, 5); } catch(_) {} }, 10);
                }
            } else {
                const yearStr = val.substring(0, firstDash);
                if (yearStr.length > 4) {
                    const rest = val.substring(firstDash);
                    input.value = yearStr.substring(0, 4) + rest;
                    setTimeout(() => { try { input.setSelectionRange(5, 5); } catch(_) {} }, 10);
                }
            }
            updateWeekday();
            // 键盘输入 → 不在此处理（等待 Enter 或 change）
            if (_dateManualTyping || _dateKeyEnter || _dateKeyArrow) return;
            // datetime-local 日历交互：
            // - 用户选了日期/时间 → 标记 _datePickerInteracted，blur 时不再恢复原始值
            // - 第1次交互，日期=今天 且 时间≠原始时间 → "今天"按钮，立即触发
            // - 其他情况：不触发，等 change（正常完成选日期+小时+分钟后触发）
            if (input.type === "datetime-local" && _dateFocusOriginal) {
                _datePickerInteracted = true;
                _datePickerInputCount++;
                if (_datePickerInputCount === 1) {
                    var curParts = val.split('T');
                    var origParts = _dateFocusOriginal.split('T');
                    var now2 = new Date();
                    var todayStr = now2.getFullYear() + '-' + String(now2.getMonth()+1).padStart(2,'0') + '-' + String(now2.getDate()).padStart(2,'0');
                    if (curParts.length === 2 && origParts.length === 2 && curParts[0] === todayStr && curParts[1] !== origParts[1]) {
                        // "今天"按钮：日期=今天 且 时间变了 → 设为 23:59，立即触发
                        input.value = curParts[0] + 'T23:59';
                        _dateFocusOriginal = "";
                        _datePickerInteracted = false;
                        _datePickerInputCount = 0;
                        _dateInputTriggered = true;
                        gotoDate();
                        return;
                    }
                }
            }
        };

        // 左右箭头步进（dateStep / fetchStep）已按需求删除：
        // 复盘由 gotoDate 的 SSE 软断开（end_time）统一承载，箭头逐根步进不再需要。

        window.updateWeekday = function() {
            var input = document.getElementById("goto-date-input");
            var span = document.getElementById("date-weekday");
            var v = input.value.trim();
            if (!v) { span.textContent = ""; return; }
            // 提取日期部分（兼容 datetime-local 的 T 分隔符）
            var datePart = v.split("T")[0];
            var parts = datePart.split('-');
            if (parts.length !== 3) { span.textContent = ""; return; }
            var d = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
            var weekNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
            span.textContent = weekNames[d.getDay()];
        };


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.NavToolbar = {
            switchFreq, gotoDate, handleDateChange,
        handleDateInput, onCoordSystemChange, initCoordSystemRadio,
        updateFreqButtonStates, updateWeekday, adjustViewForSavedPoint
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] SymbolSearch —— 证券搜索组件（搜索 / 历史 / 代码加载）
        // 对外接口（ChanApp.components.SymbolSearch）: loadStock, doSearch, showHistory, selectHistory, clearHistory, normalizeCode, isFuturesCode, getHistory, saveHistory, removeHistory
// ══════════════════════════════════════════════════════════════════

        // 判断是否为期货/期指代码
        function isFuturesCode(code) {
            return code.includes('KQ.m@') || code.includes('KQ.i@') || code.includes('KQD.m@') || /^[A-Z]+\.[A-Z]/.test(code);
        }

        // 归一化股票代码：标准写法唯一 = market(小写)+code(数字)，market 在前、无连接符。
        // 不再归一化任何历史写法（大写/带点/code在前一律保持原样回传，交后端严格解析拒绝）。
        // 全体调用方传至此处的都应是标准格式（search 结果 market+code / 固定入口 / 历史）。
        function normalizeCode(code) {
            if (!code) return "";
            return code.trim();
        }

        function isFixedCode(code) { return FIXED_CODES.has(normalizeCode(code)); }

        function getHistory() {
            try {
                let list = JSON.parse(localStorage.getItem(HISTORY_KEY)) || [];
                // 兼容旧格式（纯字符串）-> 转换为新格式（{code, name}）
                return list.map(c => typeof c === 'string' ? {code: c, name: ""} : c);
            } catch(e) { return []; }
        }

        function saveHistory(code, name) {
            const normCode = normalizeCode(code);
            // 固定快捷入口不写入历史，避免与顶部固定区重复
            if (isFixedCode(normCode)) return;
            let list = getHistory();
            list = list.filter(c => normalizeCode(c.code) !== normCode);
            list.unshift({code: normCode, name: name || ""});
            if (list.length > MAX_HISTORY) list = list.slice(0, MAX_HISTORY);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
        }

        function removeHistory(code) {
            const normCode = normalizeCode(code);
            let list = getHistory().filter(c => normalizeCode(c.code) !== normCode);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
            showHistory();
        }

        window.clearHistory = function() {
            localStorage.removeItem(HISTORY_KEY);
            // 仅清除用户历史，固定快捷入口保留并重新渲染
            showHistory();
        };

        window.clearInput = function() {
            const input = document.getElementById("stock-code-input");
            input.value = "";
            document.getElementById("input-clear").style.display = "none";
        };

        window.onInputChange = function() {
            const input = document.getElementById("stock-code-input");
            document.getElementById("input-clear").style.display = input.value ? "" : "none";
            selectedIndex = -1;
            const val = input.value.trim();
            if (!val) {
                document.getElementById("stock-history").classList.remove("show");
                return;
            }
            // 带市场限定的完整代码（market 前或后、可带点、大小写不限）：一律不再走搜索，
            // 直接交后端统一严格解析。标准 market(小写)+code 会被正确加载，
            // 旧写法（带点/大写/code在前）会被严格解析拒绝并给出明确错误。
            if (/^(sh|sz|bj|hk)[.]?\d+$/i.test(val)
                || /^\d+[.]?(sh|sz|bj|hk)$/i.test(val)) {
                document.getElementById("stock-history").classList.remove("show");
                return;
            }
            // 纯数字（6位）也搜索，可能有同名（如000001=平安银行/上证指数）
            // 拼音或中文，延迟搜索
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => doSearch(val), 300);
        };

        window.onInputKeydown = function(e) {
            const el = document.getElementById("stock-history");
            if (!el.classList.contains("show") || !searchResults.length) {
                if (e.key === "Enter") loadStock();
                return;
            }
            const items = el.querySelectorAll(".stock-history-item");
            if (e.key === "ArrowDown") {
                e.preventDefault();
                selectedIndex = (selectedIndex + 1) % items.length;
                updateSearchSelection(items);
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                selectedIndex = (selectedIndex - 1 + items.length) % items.length;
                updateSearchSelection(items);
            } else if (e.key === "Enter") {
                e.preventDefault();
                if (selectedIndex >= 0 && selectedIndex < searchResults.length) {
                    const item = searchResults[selectedIndex];
                    selectHistory(item.market === 'futures' ? item.code : item.market + item.code);
                } else {
                    loadStock();
                }
            } else if (e.key === "Escape") {
                el.classList.remove("show");
            }
        };

        window.updateSearchSelection = function(items) {
            items.forEach((item, i) => {
                item.style.background = i === selectedIndex ? "#0f3460" : "";
                item.style.color = i === selectedIndex ? "#e0e0e0" : "";
            });
        };

        window.doSearch = function(keyword) {
            fetch("/api/search?q=" + encodeURIComponent(keyword))
                .then(r => r.json())
                .then(data => {
                    const el = document.getElementById("stock-history");
                    if (data.need_refresh) {
                        el.innerHTML = '<div class="stock-history-item" style="color:#e94560;cursor:default;padding:10px;">' + (data.msg || '') + '</div>';
                        el.classList.add("show");
                        return;
                    }
                    searchResults = data.results || [];
                    selectedIndex = -1;
                    if (!searchResults.length) {
                        el.classList.remove("show");
                        return;
                    }
                    el.innerHTML = searchResults.map((item, idx) => {
                        const safeCode = item.code.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                        const safeMarket = item.market.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                        const fullCode = item.market === 'futures' ? safeCode : safeMarket + safeCode;
                        const displayCode = item.market === 'futures' ? item.code : item.market + item.code;
                        const typeMap = {"深A":"深A","沪A":"沪A","深B":"深B","沪B":"沪B","指数":"指数","基金":"基金","场外基金":"场外基金","港股":"港股"};
                        const typeLabel = typeMap[item.type] || item.type;
                        return `<div class="stock-history-item" data-idx="${idx}"><span onclick="selectHistory('${fullCode}')" style="flex:1;display:block">${displayCode} - ${item.name} (${item.pinyin}) <span style="color:#888;font-size:11px;margin-left:8px">${typeLabel}</span></span></div>`;
                    }).join("");
                    el.classList.add("show");
                    // 焦点自动移到第一个候选
                    selectedIndex = 0;
                    updateSearchSelection(el.querySelectorAll(".stock-history-item"));
                })
                .catch(() => {});
        };

        window.toggleInputClear = function() {
            const input = document.getElementById("stock-code-input");
            document.getElementById("input-clear").style.display = input.value ? "" : "none";
        };

        window.removeHistory = removeHistory;

        window.showHistory = function() {
            const list = getHistory();
            const el = document.getElementById("stock-history");
            // 重置搜索态，确保历史视图下键盘导航不会误用旧的搜索结果
            searchResults = [];
            selectedIndex = -1;
            // 顶部固定快捷入口区（不可删除）
            let html = FIXED_INDICES.map(item => {
                const safe = item.code.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                return `<div class="stock-history-item"><span onclick="selectHistory('${safe}')" style="flex:1;display:block">${item.code} - ${item.name}</span></div>`;
            }).join("");
            // 用户浏览历史区（可单条删除）；首项顶部加分割线，与底部"清除全部"风格一致
            html += list.map((c, i) => {
                const safe = c.code.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                const label = c.name ? c.code + " - " + c.name : c.code;
                const sepStyle = i === 0 ? 'border-top:1px solid #0f3460;' : '';
                return `<div class="stock-history-item" style="${sepStyle}"><span onclick="selectHistory('${safe}')" style="flex:1;display:block">${label}</span><span class="stock-history-del" onclick="event.stopPropagation();removeHistory('${safe}')">&times;</span></div>`;
            }).join("");
            // 有用户历史时才显示"清除全部"（仅清用户历史，不影响固定项）
            if (list.length) {
                html += `<div class="stock-history-clear" onclick="event.stopPropagation();clearHistory()">清除全部</div>`;
            }
            el.innerHTML = html;
            el.classList.add("show");
        };

        window.loadStock = function() {
            const code = document.getElementById("stock-code-input").value.trim();
            if (!code) return;
            // 切换股票时取消区间选择
            if (_rangeSelect.mode === 'SELECTED_A') {
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
            }
            document.getElementById("stock-history").classList.remove("show");
            document.getElementById("loading").classList.remove("hidden");
            const FUTURES_ALIAS_KEYS = new Set(["IF","IH","IC","IM","T","TF","TL","TS","CU","AL","ZN","PB","NI","SN","AO","AU","AG","RB","WR","HC","SS","BU","RU","FU","SP","BR","M","Y","A","B","P","J","JM","I","C","CS","L","V","PP","EG","EB","PG","FB","BB","RR","LH","JD","TA","PTA","MA","FG","SA","SR","CF","CY","OI","RM","ZC","UR","PF","PK","AP","CJ","SM","SF","SH","PX","LR","RI","JR","WH","PM","RS","SC","LU","NR","BC","EC","SI","LC","PS","A50","CN"]);
            const isFuturesCode = code.includes('KQ.m@') || code.includes('KQ.i@') || code.includes('KQD.m@') || /^[A-Z]+\.[A-Z]/.test(code) || FUTURES_ALIAS_KEYS.has(code.toUpperCase());
            // 判断切换前是否为期指
            const wasFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            // 同类继承上一周期，异类使用默认周期
            let fetchFreq;
            if (wasFutures && isFuturesCode) {
                // 期指C → 期指D：保持C的周期
                fetchFreq = lastFuturesFreq;
            } else if (!wasFutures && !isFuturesCode) {
                // 股票A → 股票B：保持A的周期
                fetchFreq = lastStockFreq;
            } else if (wasFutures && !isFuturesCode) {
                // 期指 → 股票：默认日K，重置为单窗口，同时彻底清理所有期货数据
                disconnectRealtime();
                fetch("/api/futures/cleanup", { method: "POST" }).catch(() => {});
                fetchFreq = 'd';
                if (isDualWindow) {
                    isDualWindow = false;
                    activeDualWindow = 'main';
                    dualSubData = null;
                    dualSubFreq = '';
                    dualHighlightRange = null;
                    dualRedRange = null;
                    dualNewZsData = null;
                    dualShowNewZs = false;
                    dualNewZsLeftDate = "";
                    dualNewZsRightDate = "";
                    document.getElementById("btn-dual").classList.remove("active");
                    // 红框调试面板已禁用（行内清理保留注释待恢复）
                    // const dbg = document.getElementById("redframe-debug");
                    // if (dbg) dbg.style.display = "none";
                    // 恢复单canvas布局
                    const container = document.getElementById("chart-container");
                    const mainDiv = document.getElementById("chart-main");
                    const subDiv = document.getElementById("chart-sub");
                    if (mainDiv) mainDiv.remove();
                    if (subDiv) subDiv.remove();
                    canvas = mainCanvas; ctx = mainCtx;
                    container.appendChild(canvas);
                    resizeCanvas();
                }
            } else {
                // 股票 → 期指：默认5分钟，重置为单窗口
                // 股票和期货周期体系不同（股票: w/d/30m/5m，期货: 30m/5m/1m/15s），
                // 下窗周期无法跨市场继承，强行继承会导致下窗周期>上窗周期
                fetchFreq = '5m';
                if (isDualWindow) {
                    isDualWindow = false;
                    activeDualWindow = 'main';
                    dualSubData = null;
                    dualSubFreq = '';
                    dualHighlightRange = null;
                    dualRedRange = null;
                    dualNewZsData = null;
                    dualShowNewZs = false;
                    dualNewZsLeftDate = "";
                    dualNewZsRightDate = "";
                    document.getElementById("btn-dual").classList.remove("active");
                    // 红框调试面板已禁用（行内清理保留注释待恢复）
                    // const dbg = document.getElementById("redframe-debug");
                    // if (dbg) dbg.style.display = "none";
                    // 恢复单canvas布局
                    const container = document.getElementById("chart-container");
                    const mainDiv = document.getElementById("chart-main");
                    const subDiv = document.getElementById("chart-sub");
                    if (mainDiv) mainDiv.remove();
                    if (subDiv) subDiv.remove();
                    canvas = mainCanvas; ctx = mainCtx;
                    container.appendChild(canvas);
                    resizeCanvas();
                }
            }
            currentFreq = fetchFreq;
            updateDateInputType();
            if (isFuturesCode) {
                updateFreqButtonStates(true); // 期货：禁用 d/w，启用 1m/15s
                if (isDualWindow) {
                    // 双窗口换期货合约：走双窗 SSE，两窗同步重连
                    // （wasFutures→isFuturesCode 路径保持周期不变，dualSubFreq 仍然有效；
                    //   空/失效时按映射回退，期货周期全为日内，getDualSubFreq 必有解或回退 1m）
                    const subFreq = dualSubFreq || getDualSubFreq(fetchFreq) || '1m';
                    connectRealtimeDual(code, fetchFreq, subFreq);
                } else {
                    connectRealtimeInit(code, fetchFreq);
                }
                return;
            }
            updateFreqButtonStates(false); // 股票：禁用 1m/15s，启用 d/w
            // P2：双窗换标的保持当前下窗配对（对新周期仍合法则不变，否则回退默认）
            let reqSubFreq = '';
            if (isDualWindow) {
                if (dualSubFreq && isValidStockDualPair(fetchFreq, dualSubFreq)) {
                    reqSubFreq = dualSubFreq;
                } else {
                    reqSubFreq = getDualSubFreq(fetchFreq) || '';
                    dualSubFreq = reqSubFreq;
                }
            }
            fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + fetchFreq
                + (isDualWindow && getDualSubFreq(fetchFreq) ? "&dual=1" : "")
                + (isDualWindow && reqSubFreq ? "&sub_freq=" + reqSubFreq : ""))
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                    return resp.json();
                })
                .then(data => {
                    // 防御：检查 API 返回数据是否完整（缺少 meta 时后续 data.meta.name 会崩溃）
                    if (!data || !data.meta) {
                        const errMsg = data && data.error ? data.error : "API 返回数据缺少 meta 字段";
                        throw new Error("查询失败: " + errMsg);
                    }
                    saveHistory(code, data.meta.name);
                    chartData = data;
                    updateRestartBtn();
                    updateDualBtn();
                    // 根据返回数据的周期同步按钮状态
                    let returnedFreq;
                    if (data.meta.freq === "5分钟") {
                        returnedFreq = "5m";
                    } else if (data.meta.freq === "30分钟") {
                        returnedFreq = "30m";
                    } else if (data.meta.freq === "周线") {
                        returnedFreq = "w";
                    } else {
                        returnedFreq = "d";
                    }
                    currentFreq = returnedFreq;
                    lastStockFreq = currentFreq; // 更新股票周期记忆
                    updateDateInputType();
                    updateFreqButtonStates(false);
                    viewCount = VIEW_COUNT;
                    adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    // 保持当前开关状态，不重置（切换个股时继承）
                    document.getElementById("btn-fx").classList.toggle("active", showFx);
                    document.getElementById("btn-bi").classList.toggle("active", showBi);
                    document.getElementById("btn-zs").classList.toggle("active", showZs);
                    document.getElementById("btn-seg").classList.toggle("active", showSeg);
                    document.getElementById("btn-bsp").classList.toggle("active", showBsp);
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    resizeCanvas();
                    // 双窗口模式下同时加载下面窗口数据
                    if (isDualWindow) {
                        // P2：dualSubFreq 已在请求前按新周期校验/回退，
                        // 响应 data.sub 即该配对的下窗数据，不再重置为默认配对
                        if (data.sub) {
                            dualSubData = data.sub;
                            // B 操作双窗选点（上窗有选点）：下窗对齐上窗 [选点, 最新] 区间加载，
                            // 视口无 377 限制——下窗后端加载多少根，前端视口就显示多少根
                            // （与股票双窗选点后下窗全显规则一致；A/C 操作仍走 VIEW_COUNT 视口）
                            if (chartData && chartData.meta && chartData.meta.saved_selection_date) {
                                dualSubViewCount = dualSubData.klines.length;
                                dualSubViewOffset = 0;
                            } else {
                                dualSubViewCount = VIEW_COUNT;
                                dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                                if (dualSubData.klines.length < dualSubViewCount) {
                                    dualSubViewOffset = 0;
                                }
                            }
                        }
                    }
                    document.getElementById("loading").classList.add("hidden");
                    render();
                    generateStats();
                    loadAnnotations();
                    saveLastState(); // 保存状态
                    // 期货/期指：切换到实时模式
                    startRealtimeIfFutures(data);
                })
                .catch(err => {
                    alert("查询失败: " + err.message);
                    document.getElementById("loading").classList.add("hidden");
                });
        };

        window.selectHistory = function(code) {
            document.getElementById("stock-code-input").value = code;
            document.getElementById("stock-history").classList.remove("show");
            window.loadStock();
        };


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.SymbolSearch = {
            loadStock, doSearch, showHistory, selectHistory, clearHistory,
        normalizeCode, isFuturesCode, getHistory, saveHistory, removeHistory
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] StatsPanel —— 统计面板组件（左侧信息 / 复盘滑块联动）
        // 对外接口（ChanApp.components.StatsPanel）: toggleStats, generateStats, updateSlider, _onClickOutsideStats
// ══════════════════════════════════════════════════════════════════

        window.toggleStats = function() {
            var panel = document.getElementById("stats-panel");
            var btn = document.getElementById("btn-stats");
            if (panel.classList.contains("show")) {
                panel.classList.remove("show");
                document.removeEventListener("click", _onClickOutsideStats);
            } else {
                panel.classList.add("show");
                // 延迟绑定，避免当前点击冒泡立即触发关闭
                setTimeout(function() {
                    document.addEventListener("click", _onClickOutsideStats);
                }, 0);
            }
        };

        function _onClickOutsideStats(e) {
            var panel = document.getElementById("stats-panel");
            var btn = document.getElementById("btn-stats");
            if (!panel.contains(e.target) && !btn.contains(e.target)) {
                panel.classList.remove("show");
                document.removeEventListener("click", _onClickOutsideStats);
            }
        }

        function generateStats() {
            if (!chartData) return;
            const klines = getVisibleKlines();
            if (!klines.length) return;
            const startDate = klines[0].date, endDate = klines[klines.length - 1].date;
            const visBis = chartData.bis.filter(bi => bi.sdt >= startDate && bi.edt <= endDate);
            const visFxs = chartData.fxs.filter(fx => fx.date >= startDate && fx.date <= endDate);
            const allBis = chartData.bis, allFxs = chartData.fxs;
            let visUp = 0, visDown = 0, totalPower = 0, maxPower = 0;
            visBis.forEach(bi => { if (bi.direction === "up") visUp++; else visDown++; totalPower += bi.power; if (bi.power > maxPower) maxPower = bi.power; });
            const avgPower = visBis.length > 0 ? (totalPower / visBis.length).toFixed(2) : 0;
            let allUp = 0, allDown = 0;
            allBis.forEach(bi => { if (bi.direction === "up") allUp++; else allDown++; });
            document.getElementById("stats-content").innerHTML = `
                <div class="stats-row"><span class="stats-label">可见笔数</span><span class="stats-value">${visBis.length} / ${allBis.length}</span></div>
                <div class="stats-row"><span class="stats-label">向上笔</span><span class="stats-value" style="color:#FF3C3C">${visUp} / ${allUp}</span></div>
                <div class="stats-row"><span class="stats-label">向下笔</span><span class="stats-value" style="color:#00F0F0">${visDown} / ${allDown}</span></div>
                <div class="stats-row"><span class="stats-label">平均力度</span><span class="stats-value">${avgPower}</span></div>
                <div class="stats-row"><span class="stats-label">最大力度</span><span class="stats-value" style="color:#FFD700">${maxPower.toFixed(2)}</span></div>
                <div class="stats-row"><span class="stats-label">顶分型</span><span class="stats-value" style="color:#FF3C3C">${visFxs.filter(f=>f.mark==="G").length} / ${allFxs.filter(f=>f.mark==="G").length}</span></div>
                <div class="stats-row"><span class="stats-label">底分型</span><span class="stats-value" style="color:#00F0F0">${visFxs.filter(f=>f.mark==="D").length} / ${allFxs.filter(f=>f.mark==="D").length}</span></div>`;
        }

        function updateSlider() {
            // 双窗口模式下，使用激活窗口的数据
            const data = (isDualWindow && activeDualWindow === 'sub' && dualSubData) ? dualSubData : chartData;
            const vo = (isDualWindow && activeDualWindow === 'sub') ? dualSubViewOffset : viewOffset;
            const vc = (isDualWindow && activeDualWindow === 'sub') ? dualSubViewCount : viewCount;
            if (!data || !data.klines.length) return;
            const track = document.getElementById("slider-track");
            const win = document.getElementById("slider-window");
            const label = document.getElementById("slider-label");
            const totalKlines = data.klines.length;
            const trackWidth = track.clientWidth;
            if (trackWidth <= 0) return;

            const windowWidth = Math.max(10, (vc / totalKlines) * trackWidth);
            const maxOffset = Math.max(0, totalKlines - vc);
            const windowLeft = (vo / totalKlines) * trackWidth;

            win.style.width = windowWidth + "px";
            win.style.left = Math.max(0, Math.min(windowLeft, trackWidth - windowWidth)) + "px";

            const displayCount = Math.round(vc);
            const displayOffset = Math.round(vo);
            const startIdx = Math.max(0, displayOffset);
            const endIdx = Math.min(totalKlines - 1, startIdx + displayCount - 1);
            const startDate = data.klines[startIdx].date.slice(0, 10);
            const endDate = data.klines[endIdx].date.slice(0, 10);
            const globalStart = Math.max(0, Math.floor(vo));
            const globalEnd = Math.min(totalKlines, globalStart + vc);
            const visBis = data.bis.filter(bi => {
                const si = data.klines.findIndex(k => k.date === bi.sdt);
                return si >= globalStart && si < globalEnd;
            });
            const visFxs = data.fxs.filter(fx => {
                const fi = data.klines.findIndex(k => k.date === fx.date);
                return fi >= globalStart && fi < globalEnd;
            });
            const visZs = data.zs.filter(zs => {
                const si = data.klines.findIndex(k => k.date === zs.sdt);
                return si >= globalStart && si < globalEnd;
            });
            const winLabel = isDualWindow ? (activeDualWindow === 'sub' ? '[下窗] ' : '[上窗] ') : '';
            label.textContent = winLabel + startDate + " - " + endDate + "   [K线]: " + displayCount + "/" + totalKlines + "   [分型]: " + visFxs.length + "/" + data.fxs.length + "   [笔]: " + visBis.length + "/" + data.bis.length + "   [中枢]: " + visZs.length + "/" + data.zs.length;
        }


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.StatsPanel = {
            toggleStats, generateStats, updateSlider, _onClickOutsideStats
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] DownloadPanel —— 盘后下载面板组件（分类勾选 / 进度轮询）
        // 对外接口（ChanApp.components.DownloadPanel）: toggleDownloadPanel, closeDownloadPanel, startDownload, stopDownload, _pollDownloadStatus, _buildCategories
// ══════════════════════════════════════════════════════════════════

        window.toggleDownloadPanel = function() {
            var panel = document.getElementById("download-panel");
            var overlay = document.getElementById("download-panel-overlay");
            if (panel.classList.contains("show")) {
                closeDownloadPanel();
            } else {
                // 动态计算默认日期：日线=5年前，分钟线=6个月前
                var today = new Date();
                var y5 = new Date(today.getFullYear() - 5, today.getMonth(), today.getDate());
                var m6 = new Date(today.getFullYear(), today.getMonth() - 6, today.getDate());
                var defaultDay = y5.toISOString().slice(0, 10);
                var defaultMin = m6.toISOString().slice(0, 10);
                document.getElementById("dl-day-start").value = defaultDay;
                document.getElementById("dl-min-start").value = defaultMin;

                panel.classList.add("show");
                overlay.classList.add("show");
                // 如果正在下载中，恢复进度显示
                if (_downloadRunning) {
                    _startPolling();
                }
            }
        };

        window.closeDownloadPanel = function() {
            document.getElementById("download-panel").classList.remove("show");
            document.getElementById("download-panel-overlay").classList.remove("show");
        };

        function _buildCategories() {
            var cats = [];
            // 沪深京市场：日线勾选 → sh/sz/bj
            if (document.getElementById("dl-hsj-day") && document.getElementById("dl-hsj-day").checked) {
                cats.push({type: "day", market: "sh"}, {type: "day", market: "sz"}, {type: "day", market: "bj"});
            }
            // 沪深京市场：5分钟勾选 → sh/sz/bj
            if (document.getElementById("dl-hsj-5m") && document.getElementById("dl-hsj-5m").checked) {
                cats.push({type: "5m", market: "sh"}, {type: "5m", market: "sz"}, {type: "5m", market: "bj"});
            }
            return cats;
        }

        window.startDownload = function() {
            var cats = _buildCategories();
            if (cats.length === 0) {
                alert("请至少选择一项要下载的数据类型");
                return;
            }
            var dayStart = document.getElementById("dl-day-start").value || "";
            var minStart = document.getElementById("dl-min-start").value || "";
            _downloadRunning = true;
            document.getElementById("dl-btn-start").style.display = "none";
            document.getElementById("dl-btn-stop").style.display = "";
            document.getElementById("download-progress-wrap").style.display = "";
            document.getElementById("download-status").textContent = "正在连接服务器...";

            fetch("/api/stocks/download/start", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({categories: cats, day_start: dayStart, min_start: minStart})
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.ok) {
                    document.getElementById("download-status").textContent = data.message;
                    _startPolling();
                } else {
                    _downloadRunning = false;
                    document.getElementById("download-status").textContent = "启动失败: " + (data.message || data.error || "未知错误");
                    _resetDownloadUI();
                }
            })
            .catch(function(err) {
                _downloadRunning = false;
                document.getElementById("download-status").textContent = "启动失败: " + err.message;
                _resetDownloadUI();
            });
        };

        window.stopDownload = function() {
            fetch("/api/stocks/download/cancel", { method: "POST" })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                document.getElementById("download-status").textContent = data.message;
                _downloadRunning = false;
                _resetDownloadUI();
                if (_downloadTimer) { clearInterval(_downloadTimer); _downloadTimer = null; }
            });
        };

        function _startPolling() {
            if (_downloadTimer) clearInterval(_downloadTimer);
            _downloadTimer = setInterval(_pollDownloadStatus, 1000);
        }

        function _pollDownloadStatus() {
            fetch("/api/stocks/download/read/status")
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var pct = data.progress || 0;
                document.getElementById("download-progress-fill").style.width = pct + "%";
                document.getElementById("download-progress-text").textContent =
                    pct + "% (" + (data.completed_stocks || 0) + "/" + (data.total_stocks || 0) + ")";
                var statusText = "";
                if (data.current_category) {
                    statusText += "[" + data.current_category + "] ";
                }
                if (data.current_stock) {
                    statusText += data.current_stock;
                }
                if (data.errors && data.errors.length > 0) {
                    statusText += " | 错误: " + data.errors.length + " 条";
                    // 显示前三条错误信息
                    var firstErrors = data.errors.slice(0, 3).join(" ; ");
                    statusText += " [" + firstErrors + "]";
                }
                document.getElementById("download-status").textContent = statusText || "下载中...";

                if (!data.running) {
                    // 下载完成
                    _downloadRunning = false;
                    _resetDownloadUI();
                    if (_downloadTimer) { clearInterval(_downloadTimer); _downloadTimer = null; }
                    var errorCount = data.errors ? data.errors.length : 0;
                    var msg = "下载完成！共 " + (data.completed_stocks || 0) + " 只股票";
                    if (errorCount > 0) {
                        msg += "，" + errorCount + " 个错误";
                    }
                    document.getElementById("download-status").textContent = msg;
                    document.getElementById("download-progress-text").textContent = "100%";
                    document.getElementById("download-progress-fill").style.width = "100%";
                    // 检测数据是否最新交易日
                    if (data.data_is_latest === false) {
                        var dataDate = data.latest_data_date_str || "未知";
                        var tdDate = data.latest_trading_day_str || "未知";
                        setTimeout(function() {
                            alert("⚠ 注意：TDX 服务器数据尚未更新到最新交易日\n\n本地数据最新日期: " + dataDate + "\n最新交易日: " + tdDate + "\n\n建议在收盘后再次点击下载获取最新数据。");
                        }, 300);
                    }
                }
            })
            .catch(function(err) {
                console.error("轮询下载状态失败:", err);
            });
        }

        function _resetDownloadUI() {
            document.getElementById("dl-btn-start").style.display = "";
            document.getElementById("dl-btn-stop").style.display = "none";
        }


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.DownloadPanel = {
            toggleDownloadPanel, closeDownloadPanel, startDownload,
        stopDownload, _pollDownloadStatus, _buildCategories
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] BspSettingsPanel —— 买卖点设置弹窗组件（BSP 过滤 / 均线周期）
        // 对外接口（ChanApp.components.BspSettingsPanel）: openBspSettings, closeBspSettings, onBspFilterChange, onMaPeriodChange, onShowBiIdxChange, bspFilterSelectAll, bspFilterSelectNone, maPeriodsSelectAll, maPeriodsSelectNone
// ══════════════════════════════════════════════════════════════════

        // ── BSP买卖点类型过滤 + 均线周期设置 ──
        window.openBspSettings = function() {
            // 打开前同步当前过滤状态到复选框
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = bspFilter[cbs[i].value];
            }
            // 同步均线周期复选框
            var macbs = document.querySelectorAll('#bsp-filter-dialog input[name="ma-period"]');
            for (var i = 0; i < macbs.length; i++) {
                macbs[i].checked = !!maPeriods[macbs[i].value];
            }
            // 同步笔索引复选框
            var biIdxCb = document.querySelector('#bsp-filter-dialog input[name="show-bi-idx"]');
            if (biIdxCb) biIdxCb.checked = showBiIdx;
            initCoordSystemRadio();
            document.getElementById("bsp-filter-dialog").classList.add("show");
            document.getElementById("bsp-filter-overlay").classList.add("show");
        };

        window.closeBspSettings = function() {
            document.getElementById("bsp-filter-dialog").classList.remove("show");
            document.getElementById("bsp-filter-overlay").classList.remove("show");
        };

        // 即时生效：单个买卖点复选框变化
        window.onBspFilterChange = function(cb) {
            bspFilter[cb.value] = cb.checked;
            saveOverlaySettings();
            render();
        };

        // 即时生效：单个均线周期复选框变化
        window.onMaPeriodChange = function(cb) {
            if (cb.checked) maPeriods[cb.value] = true;
            else delete maPeriods[cb.value];
            saveOverlaySettings();
            render();
        };

        window.onShowBiIdxChange = function(cb) {
            showBiIdx = cb.checked;
            saveOverlaySettings();
            render();
        };

        window.bspFilterSelectAll = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = true;
                bspFilter[cbs[i].value] = true;
            }
            saveOverlaySettings();
            render();
        };

        window.bspFilterSelectNone = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = false;
                bspFilter[cbs[i].value] = false;
            }
            saveOverlaySettings();
            render();
        };

        window.maPeriodsSelectAll = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="ma-period"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = true;
                maPeriods[cbs[i].value] = true;
            }
            saveOverlaySettings();
            render();
        };

        window.maPeriodsSelectNone = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="ma-period"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = false;
            }
            maPeriods = {};
            saveOverlaySettings();
            render();
        };


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.BspSettingsPanel = {
            openBspSettings, closeBspSettings, onBspFilterChange,
        onMaPeriodChange, onShowBiIdxChange, bspFilterSelectAll, bspFilterSelectNone,
        maPeriodsSelectAll, maPeriodsSelectNone
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] ScanPanel —— 扫描面板组件（模式对话框 / 逐只扫描 / 结果渲染 / 保存自选）
        // 对外接口（ChanApp.components.ScanPanel）: startScanZxg, doStartScan, scanModeDialogConfirm, scanModeDialogCancel, renderScanResults, renderFxDScanResults, renderMaScanResults, renderFangliangScanResults, saveScanToZxg, closeScanPanel, toggleScanMinimize, loadScanResult, refreshStockNames, updateScanSaveBtn, scanSourceSelectAll, scanSourceSelectNone
// ══════════════════════════════════════════════════════════════════

        // 扫描模式切换时，控制"最近N根"输入框的灰化状态
        // 标注扫描：只要有标注就命中，与日期无关，输入框置灰；扫描来源也置灰
        // 底分型扫描：找最后一个分型是底分型的个股，与日期无关，输入框置灰；扫描来源可用
        // 买卖点扫描：需要按最近N根K线过滤，输入框可用
        function updateScanRecentDisabled() {
            var row = document.getElementById("scan-recent-row");
            var input = document.getElementById("scan-recent-days");
            var freqRow = document.getElementById("scan-freq-row");
            var selected = document.querySelector('input[name="scan-mode"]:checked');
            var isAnn = selected && selected.value === "ann";
            var isMa = selected && selected.value === "ma";
            var isFxD = selected && selected.value === "fx_d";
            if (row && input) {
                if (isAnn || isMa || isFxD) {
                    row.style.opacity = "0.35";
                    row.style.pointerEvents = "none";
                    input.disabled = true;
                } else {
                    row.style.opacity = "1";
                    row.style.pointerEvents = "";
                    input.disabled = false;
                }
            }
            if (freqRow) {
                if (isAnn) {
                    // 标注扫描：周期也置灰
                    freqRow.style.opacity = "0.35";
                    freqRow.style.pointerEvents = "none";
                } else {
                    // 底分型/买卖点扫描：周期可用
                    freqRow.style.opacity = "1";
                    freqRow.style.pointerEvents = "";
                }
            }
            // 标注模式下灰化"扫描来源"区域（标注扫描与来源无关）
            var srcSection = document.getElementById("scan-source-section");
            if (srcSection) {
                if (isAnn) {
                    srcSection.style.opacity = "0.35";
                    srcSection.style.pointerEvents = "none";
                } else {
                    srcSection.style.opacity = "1";
                    srcSection.style.pointerEvents = "";
                }
            }
        }

        window.updateScanRecentDisabled = updateScanRecentDisabled;

        // 扫描来源→中文标签（多选时用顿号连接）
        function _scanSourceLabel() {
            var map = {"zxg": "自选股", "page_index": "成分股", "tdxhy2": "板块指数2", "tdxhy3": "板块指数3"};
            var labels = [];
            for (var i = 0; i < _scanSources.length; i++) {
                labels.push(map[_scanSources[i]] || _scanSources[i]);
            }
            return labels.join("、");
        }

        // 全选 / 取消 扫描来源
        window.scanSourceSelectAll = function() {
            var cbs = document.querySelectorAll('input[name="scan-source"]');
            for (var i = 0; i < cbs.length; i++) { cbs[i].checked = true; }
        };

        window.scanSourceSelectNone = function() {
            var cbs = document.querySelectorAll('input[name="scan-source"]');
            for (var i = 0; i < cbs.length; i++) { cbs[i].checked = false; }
        };

        // 生成买卖点标签HTML（最多显示6个，超出显示+N）
        function buildBspTagsHtml(buyPoints, sellPoints) {
            var MAX_TAGS = 6;
            var allTags = [];
            (buyPoints || []).forEach(function(bp) {
                var tp = bp.type.replace(/\s/g, "");
                if (tp === "0" || tp === "1" || tp === "2" || tp === "3") {
                    allTags.push('<span class="scan-bsp-tag buy">' + bp.type + '</span>');
                }
            });
            (sellPoints || []).forEach(function(sp) {
                var tp = sp.type.replace(/\s/g, "");
                if (tp === "0" || tp === "1" || tp === "2" || tp === "3") {
                    allTags.push('<span class="scan-bsp-tag sell">' + sp.type + '</span>');
                }
            });
            var html = '<div class="scan-bsp-tags">';
            if (allTags.length <= MAX_TAGS) {
                html += allTags.join('');
            } else {
                html += allTags.slice(0, MAX_TAGS).join('');
                html += '<span class="scan-bsp-tag scan-bsp-more">+' + (allTags.length - MAX_TAGS) + '</span>';
            }
            html += '</div>';
            return html;
        }

        // 生成120均线列HTML
        function buildMa120Html(data) {
            if (data.below_ma120 === undefined || data.ma120_val === undefined) return '<span class="scan-col-ma">--</span>';
            if (data.ma120_val === 0) return '<span class="scan-col-ma">--</span>';
            if (data.below_ma120) {
                return '<span class="scan-col-ma warn">↓' + data.ma120_val + '</span>';
            } else {
                return '<span class="scan-col-ma">↑' + data.ma120_val + '</span>';
            }
        }

        // 放量标签HTML：颜色对齐K线图中 A 那根"成交额柱"的颜色
        //   成交额柱：收阳(close>open) → 红柱 #FF3C3C；收阴 → 青绿柱 #00F0F0（见 drawVolume）
        function buildFangliangTagHtml(data) {
            var isRise = !!(data.a_is_rise);
            var cls = isRise ? "fl-rise" : "fl-fall";
            return '<span class="scan-bsp-tag ' + cls + '">放量</span>';
        }

        // 扫描模式对话框：取消
        window.scanModeDialogCancel = function() {
            document.getElementById("scan-mode-dialog").classList.remove("show");
        };

        // 扫描模式对话框：确认
        window.scanModeDialogConfirm = function() {
            var selected = document.querySelector('input[name="scan-mode"]:checked');
            if (!selected) return;
            _scanMode = selected.value;
            // 多选：读取所有勾选的 checkbox
            var sourceCbs = document.querySelectorAll('input[name="scan-source"]:checked');
            _scanSources = [];
            for (var i = 0; i < sourceCbs.length; i++) {
                _scanSources.push(sourceCbs[i].value);
            }
            if (_scanSources.length === 0) {
                _scanSources = ["zxg"];
                document.querySelector('input[name="scan-source"][value="zxg"]').checked = true;
            }
            var daysInput = document.getElementById("scan-recent-days");
            _scanRecentDays = parseInt(daysInput.value) || 1;
            if (_scanRecentDays < 1) _scanRecentDays = 1;
            // 读取扫描周期
            var freqRadio = document.querySelector('input[name="scan-freq"]:checked');
            if (freqRadio) {
                _scanFreq = freqRadio.value;
            }
            // 持久化到 localStorage，下次打开保持上次选择
            try {
                localStorage.setItem("scan_mode", _scanMode);
                localStorage.setItem("scan_recent_days", String(_scanRecentDays));
                localStorage.setItem("scan_sources", _scanSources.join(","));
                localStorage.setItem("scan_freq", _scanFreq);
            } catch(e) {}
            document.getElementById("scan-mode-dialog").classList.remove("show");
            // 如果勾选了"成分股"，通知后端当前页面指数代码
            if (_scanSources.indexOf("page_index") >= 0 && chartData && chartData.meta && chartData.meta.symbol) {
                fetch("/api/stocks/scan/set/index?code=" + encodeURIComponent(chartData.meta.symbol), { method: "PUT" }).catch(function(){});
            }
            // 执行实际扫描
            doStartScan();
        };

        function updateScanTitle() {
            var freqLabels = {"d": "日K", "w": "周K", "30m": "30分", "5m": "5分"};
            var freqLabel = freqLabels[_scanFreq] || _scanFreq;
            if (_scanMode === "bsp") {
                document.getElementById("scan-title").innerHTML = freqLabel + '<span style="font-size:11px;font-weight:400;color:#a8b2d1">[最近</span><b style="font-size:11px;color:#e94560">' + _scanRecentDays + '</b><span style="font-size:11px;font-weight:400;color:#a8b2d1">根]</span> 买/卖点';
            } else if (_scanMode === "ma") {
                document.getElementById("scan-title").textContent = freqLabel + " 均线";
            } else if (_scanMode === "fangliang") {
                document.getElementById("scan-title").textContent = freqLabel + " 放量";
            } else if (_scanMode === "fx_d") {
                document.getElementById("scan-title").textContent = freqLabel + " 底分型";
            } else {
                // 标注扫描：显示全周期，不再显示当前周期
                document.getElementById("scan-title").textContent = "全周期 标注";
            }
        }

        window.startScanZxg = function() {
            if (_scanRunning) {
                // 正在扫描中，再次点击 = 中断扫描
                _scanAborted = true;
                var btn = document.getElementById("btn-scan");
                btn.textContent = "正在中断...";
                btn.disabled = true;
                // 通知后端立即终止：
                // 中止：精确中止当前 task（worker 每票前检查中止标志）
                if (_scanTaskId) {
                    fetch("/api/stocks/scan/" + _scanTaskId + "/cancel", { method: "POST" }).catch(function(){});
                    _scanTaskId = null;
                }
                return;
            }
            // 弹出模式选择对话框
            // 从 localStorage 恢复上次的选择
            try {
                var savedMode = localStorage.getItem("scan_mode");
                if (savedMode === "bsp" || savedMode === "ann" || savedMode === "ma" || savedMode === "fx_d" || savedMode === "fangliang") {
                    _scanMode = savedMode;
                    var radio = document.querySelector('input[name="scan-mode"][value="' + savedMode + '"]');
                    if (radio) radio.checked = true;
                }
                var savedDays = localStorage.getItem("scan_recent_days");
                if (savedDays) {
                    _scanRecentDays = parseInt(savedDays) || 1;
                    document.getElementById("scan-recent-days").value = _scanRecentDays;
                }
                var savedSources = localStorage.getItem("scan_sources");
                if (savedSources) {
                    var arr = savedSources.split(",");
                    var valid = [];
                    for (var i = 0; i < arr.length; i++) {
                        var v = arr[i].trim();
                        if (v === "zxg" || v === "page_index" || v === "tdxhy2" || v === "tdxhy3") {
                            valid.push(v);
                        }
                    }
                    if (valid.length > 0) {
                        _scanSources = valid;
                        // 先全部取消，再勾选保存的
                        var allCbs = document.querySelectorAll('input[name="scan-source"]');
                        for (var i = 0; i < allCbs.length; i++) { allCbs[i].checked = false; }
                        for (var i = 0; i < valid.length; i++) {
                            var cb = document.querySelector('input[name="scan-source"][value="' + valid[i] + '"]');
                            if (cb) cb.checked = true;
                        }
                    }
                }
                var savedFreq = localStorage.getItem("scan_freq");
                if (savedFreq && ["w", "d", "30m", "5m"].indexOf(savedFreq) >= 0) {
                    _scanFreq = savedFreq;
                    var radio = document.querySelector('input[name="scan-freq"][value="' + savedFreq + '"]');
                    if (radio) radio.checked = true;
                }
            } catch(e) {}
            // "成分股"选项一直可见：当前页面是可获取成分股的指数时可用，否则灰化禁用
            var pageIndexLabel = document.getElementById("label-page-index");
            var pageIndexCb = document.querySelector('input[name="scan-source"][value="page_index"]');
            if (pageIndexLabel && pageIndexCb) {
                // 是否灰化只看「当前打开的是不是指数」：以后端 meta.is_index 权威字段
                // 为准，唯一事实源。前端不做任何 code 正则推断——后端契约保证该字段必下发。
                var isSectorIndex = !!(chartData && chartData.meta && chartData.meta.is_index);
                if (isSectorIndex) {
                    pageIndexLabel.style.opacity = "1";
                    pageIndexLabel.style.pointerEvents = "";
                    pageIndexCb.disabled = false;
                } else {
                    pageIndexLabel.style.opacity = "0.35";
                    pageIndexLabel.style.pointerEvents = "none";
                    pageIndexCb.checked = false;
                    pageIndexCb.disabled = true;
                }
            }
            // 根据当前模式设置"最近N根"输入框的灰化状态
            updateScanRecentDisabled();
            document.getElementById("scan-mode-dialog").classList.add("show");
        };

        // 多来源合并：后端统一合并去重，前端只需传逗号分隔的来源列表
        function _fetchMergedStocks(sources, freq) {
            var url = "/api/stocks/scan/read/candidates?source=" + sources.join(",");
            if (freq) url += "&freq=" + freq;
            return fetch(url)
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.errors && data.errors.length > 0) {
                        console.warn("[扫描] 后端合并警告:", data.errors.join("; "));
                    }
                    return { stocks: data.stocks || [], pre_skipped: data.pre_skipped || 0 };
                });
        }

        // 批量扫描异步化（ProcessPool）
        // 提交全部股票到后端执行池（/api/stocks/scan/submit → task_id），轮询
        // /api/stocks/scan/{task_id}/read/status?since=N 增量获取结果；中止经 /api/stocks/scan/{task_id}/cancel。
        // 回调契约：onData(单票结果) 逐票增量喂入、onDone(err, interrupted) 终态
        // ——各模式渲染/过滤逻辑零改动，前端守护用例直接复用。
        // 增量游标：since 按 row.seq + 1 推进（>= 语义含首行），
        // 避免全量回传 O(n²)；轮询失败退避重试：连续 3 次熔断，
        // 不因单次网络抖动丢弃已扫描结果。
        function _asyncScanAll(stocks, opts, onData, onDone) {
            var freq = opts.freq || "d";
            var mode = opts.mode || "";
            var recent = (opts.recent != null) ? String(opts.recent) : "1";
            var source = opts.source || "zxg";
            var pollTimer = null;
            var stopped = false;
            var failCount = 0;
            var interrupted = false;

            function finish(err) {
                if (stopped) return;
                stopped = true;
                if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
                _scanTaskId = null;
                onDone(err || null, interrupted);
            }

            fetch("/api/stocks/scan/submit", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    stocks: stocks, freq: freq, mode: mode,
                    recent: recent, source: source
                })
            })
            .then(function(r) { return r.json(); })
            .then(function(sub) {
                if (!sub || sub.error || !sub.task_id) {
                    finish((sub && sub.error) ? sub.error : "提交批量扫描失败");
                    return;
                }
                var taskId = sub.task_id;
                _scanTaskId = taskId;
                var since = 0;  // 下次期望的 seq（后端按 seq >= since 增量返回）
                var _seenSeq = {};   // 已收到的 seq 去重（防并发下重复 onData）

                // 稳健游标推进：since 只在「连续已见」时可前进。
                // 并发 worker 完成顺序随机，若用 since=max(seen)+1 快进，会把
                // 落库慢、尚未出现在快照的中间 seq 永久跳过（进度停在~50、
                // 扫到数量随完成顺序漂移）。正确做法：since 只推进到连续区块
                // 边界，缺口未填前保持不动，使所有 seq 最终都能被读到。
                function _advanceSince() {
                    while (_seenSeq[since]) { since++; }
                }

                function poll() {
                    if (stopped) return;
                    if (_scanAborted) {
                        interrupted = true;
                        if (taskId) fetch("/api/stocks/scan/" + taskId + "/cancel", { method: "POST" }).catch(function(){});
                        // 中止后继续轮询：worker 快速落库中止行，completed 收敛 total
                        // （保持原设计：手工终止时已扫描结果正常显示）
                    }
                    fetch("/api/stocks/scan/" + encodeURIComponent(taskId) + "/read/status" +
                          "?since=" + since + "&_t=" + Date.now())
                    .then(function(r) { return r.json(); })
                    .then(function(st) {
                        if (stopped) return;
                        failCount = 0;
                        if (!st || st.error) {
                            finish(st && st.error ? st.error : "查询扫描进度失败");
                            return;
                        }
                        var rows = st.results || [];
                        for (var i = 0; i < rows.length; i++) {
                            var seq = rows[i].seq;
                            if (_seenSeq[seq]) continue;      // 已处理，防重复
                            _seenSeq[seq] = true;
                            _advanceSince();                  // 连续区块边界推进
                            onData(rows[i].data || rows[i]);
                        }
                        if (st.status === "done" || st.status === "aborted" || st.status === "error") {
                            if (st.status === "aborted") interrupted = true;
                            finish(null);
                            return;
                        }
                        pollTimer = setTimeout(poll, 700);
                    })
                    .catch(function() {
                        if (stopped) return;
                        failCount++;
                        if (failCount >= 3) { finish("轮询扫描进度连续失败"); return; }
                        pollTimer = setTimeout(poll, 1500);
                    });
                }
                poll();
            })
            .catch(function(err) {
                finish(err && err.message ? err.message : String(err));
            });
        }

        // 实际执行扫描（由对话框确认后调用）
        function doStartScan() {
            var panel = document.getElementById("scan-panel");
            var body = document.getElementById("scan-body");
            var status = document.getElementById("scan-status");
            var btn = document.getElementById("btn-scan");

            panel.classList.add("show");
            panel.classList.remove("minimized");
            btn.classList.add("active");
            _scanRunning = true;
            _scanAborted = false;

            var freq = _scanFreq;
            updateScanTitle();
            status.textContent = "";

            // 标注扫描模式：直接查询标注缓存（全周期，不按 freq 过滤，与扫描来源无关）
            if (_scanMode === "ann") {
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在查询标注数据...</div>';
                fetch("/api/stocks/scan/annotation")
                .then(function(resp) { return resp.json(); })
                .then(function(annData) {
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "股票扫描";

                    var codes = annData.codes || [];

                    var html = '<div class="scan-summary">全周期 标注 <b>' + codes.length + '</b> 条</div>';
                    if (codes.length === 0) {
                        html += '<div class="scan-no-result">未发现标注股票</div>';
                    } else {
                        var freqLabelMap = {"d": "日K", "w": "周K", "30m": "30分", "5m": "5分", "60m": "60分", "1m": "1分", "15s": "15秒"};
                        codes.forEach(function(c) {
                            var rCode = c.code + "." + c.market;
                            var rFreqLabel = freqLabelMap[c.freq] || c.freq;
                            // 取日期最靠近当前日期的标注文字，最多11字
                            var closestText = "";
                            if (c.annotations && c.annotations.length > 0) {
                                var today = new Date();
                                today.setHours(0, 0, 0, 0);
                                var closest = null;
                                var closestDiff = Infinity;
                                c.annotations.forEach(function(a) {
                                    var d = new Date(a.date.replace(/\//g, "-"));
                                    if (isNaN(d.getTime())) return;
                                    var diff = Math.abs(d - today);
                                    if (diff < closestDiff) {
                                        closestDiff = diff;
                                        closest = a.text;
                                    }
                                });
                                if (closest) {
                                    closestText = closest.length > 11 ? closest.substring(0, 11) + "..." : closest;
                                }
                            }
                            html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + rCode + '\', \'' + c.freq + '\')" title="点击查看K线图">';
                            html += chkBox(rCode, false);
                            html += '<span class="scan-col-name">' + (c.name || rCode) + '</span>';
                            html += '<span class="scan-col-code">' + rCode + '</span>';
                            html += '<span class="scan-col-freq">' + rFreqLabel + '</span>';
                            html += '<span class="scan-col-ann">' + closestText + '</span>';
                            html += '<span class="scan-col-tags"><span class="scan-bsp-tag buy">' + c.count + '条</span></span>';
                            html += '</div>';
                        });
                    }
                    body.innerHTML = html;
                    updateScanSaveBtn();
                })
                .catch(function(err) {
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "股票扫描";
                    body.innerHTML = '<div class="scan-no-result">查询失败: ' + err.message + '</div>';
                });
                return;
            }

            // 底分型扫描模式：找到指定周期中最后一个分型是底分型的个股
            if (_scanMode === "fx_d") {
                var sourceLabel = _scanSourceLabel();
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取：' + sourceLabel + '...</div>';
                var freq = _scanFreq;
                Promise.all([
                    fetch("/api/stocks/scan/start", { method: "POST" }),
                    _fetchMergedStocks(_scanSources, freq)
                ])
                    .then(function(resps) {
                        return resps[0].json().then(function(scanStartData) {
                            if (scanStartData.need_refresh) {
                                _scanRunning = false;
                                btn.classList.remove("active");
                                body.innerHTML = '<div class="scan-no-result" style="text-align:center;padding:20px;">' +
                                    '<div style="font-size:14px;color:#e94560;margin-bottom:12px;">&#9888; ' + scanStartData.msg + '</div>' +
                                    '<button class="btn" onclick="refreshStockNames();closeScanPanel();" style="margin-top:8px;">立即刷新</button>' +
                                    '</div>';
                                return null;
                            }
                            return resps[1];
                        });
                    })
                    .then(function(data) {
                        if (data === null) return;
                        if (!data || !data.stocks || data.stocks.length === 0) {
                            _scanRunning = false;
                            btn.classList.remove("active");
                            body.innerHTML = '<div class="scan-no-result">' + sourceLabel + '列表为空或文件不存在</div>';
                            return;
                        }
                        var stocks = data.stocks;
                        var total = stocks.length;
                        var preSkipped = data.pre_skipped || 0;
                        var results = [];
                        var skipped = 0;
                        var completed = 0;
                        var hasRenderedAny = false;

                        body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，底分型 0 只（0 / 0 / 0）</div>';

                        function finishScan(interrupted) {
                            // 先清掉进度节流定时器，避免其 500ms 内的最后一次 _doUpdatePanel
                            // 把"正在扫描"写回 body，覆盖 renderScanResults 的结果（spinner 残留）
                            if (_updateTimer) { clearInterval(_updateTimer); _updateTimer = null; }
                            _pendingUpdate = false;
                            fetch("/api/stocks/scan/end", { method: "POST" }).then(function() {
                                renderFxDScanResults(results, total + preSkipped, preSkipped + skipped, interrupted);
                            });
                        }

                        var _updateTimer = null;
                        var _pendingUpdate = false;
                        function updatePanel() {
                            if (_updateTimer) {
                                _pendingUpdate = true;
                                return;
                            }
                            _doUpdatePanel();
                            _updateTimer = setInterval(function() {
                                if (_pendingUpdate) {
                                    _pendingUpdate = false;
                                    _doUpdatePanel();
                                } else {
                                    clearInterval(_updateTimer);
                                    _updateTimer = null;
                                }
                            }, 500);
                        }
                        function _doUpdatePanel() {
                            var progress = completed + "/" + total;
                            var totalSkipped = preSkipped + skipped;
                            var strongest = 0, strong = 0, weak = 0;
                            for (var i = 0; i < results.length; i++) {
                                var s = results[i].fx_strength;
                                if (s === 2) { strongest++; }
                                else if (s === 1) { strong++; }
                                else { weak++; }
                            }
                            var fxSummary = results.length + ' 只（' + strongest + ' / ' + strong + ' / ' + weak + '）';
                            var html = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + progress + '，跳过 ' + totalSkipped + ' 只，底分型 ' + fxSummary + '</div>';
                            if (results.length > 0) {
                                hasRenderedAny = true;
                                var shCount = 0, szCount = 0, bjCount = 0, hkCount = 0;
                                for (var i = 0; i < results.length; i++) {
                                    var parts = results[i].code.split(".");
                                    var mkt = parts.length > 1 ? parts[1] : "";
                                    if (mkt === "SH") { shCount++; }
                                    else if (mkt === "SZ") { szCount++; }
                                    else if (mkt === "BJ") { bjCount++; }
                                    else if (mkt === "HK") { hkCount++; }
                                }
                                var marketParts = [];
                                if (shCount > 0) marketParts.push("上海 <b>" + shCount + "</b> 只");
                                if (szCount > 0) marketParts.push("深圳 <b>" + szCount + "</b> 只");
                                if (bjCount > 0) marketParts.push("北京 <b>" + bjCount + "</b> 只");
                                if (hkCount > 0) marketParts.push("香港 <b>" + hkCount + "</b> 只");
                                html += '<div class="scan-summary" style="margin-top:8px;">' + marketParts.join("，") + '</div>';
                                // 按分型强度降序排序（最强分型→强分型→弱分型）
                                results.sort(function(a, b) { return b.fx_strength - a.fx_strength; });
                                for (var i = 0; i < results.length; i++) {
                                    var r = results[i];
                                    var fxLabel = '底分型';
                                    var fxClass = 'fx-d';
                                    var checked = false;
                                    if (r.fx_strength === 2) { fxLabel = '最强分型'; fxClass = 'fx-strongest'; checked = true; }
                                    else if (r.fx_strength === 1) { fxLabel = '强分型'; fxClass = 'fx-strong'; checked = true; }
                                    else { fxLabel = '弱分型'; fxClass = 'fx-weak'; }
                                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                                    html += chkBox(r.code, checked);
                                    html += '<span class="scan-col-name">' + r.name + '</span>';
                                    html += '<span class="scan-col-code">' + r.code + '</span>';
                                    html += '<span class="scan-col-tags"><span class="scan-bsp-tag ' + fxClass + '">' + fxLabel + '</span></span>';
                                    html += '</div>';
                                }
                            }
                            body.innerHTML = html;
                            updateScanSaveBtn();
                        }

                        // 提交到后端执行池，轮询增量结果
                        // （单票响应同形，模式过滤/渲染逻辑零改动）
                        btn.textContent = "中断扫描";
                        _asyncScanAll(stocks, {freq: freq, mode: "fx_d", recent: _scanRecentDays, source: _scanSources.join(",")}, function(data) {
                            completed++;
                            if (data.skipped) { skipped++; }
                            else if (data.error) { skipped++; }
                            else if (data.is_fx_d) { results.push(data); }
                            updatePanel();
                        }, function(err, interrupted) {
                            if (err) {
                                console.error("[底分型扫描] " + err);
                                _scanRunning = false;
                                _scanAborted = false;
                                btn.classList.remove("active");
                                btn.disabled = false;
                                btn.textContent = "股票扫描";
                                body.innerHTML = '<div class="scan-no-result">扫描失败: ' + err + '</div>';
                                return;
                            }
                            _scanRunning = false;
                            _scanAborted = false;
                            btn.classList.remove("active");
                            btn.disabled = false;
                            btn.textContent = "股票扫描";
                            finishScan(interrupted);
                        });
                    })
                    .catch(function(err) {
                        _scanRunning = false;
                        btn.classList.remove("active");
                        btn.textContent = "股票扫描";
                        body.innerHTML = '<div class="scan-no-result">读取' + sourceLabel + '失败: ' + err.message + '</div>';
                    });
                return;
            }

            // 均线分类扫描模式：按最新收盘价未攻克的最小周期均线分类
            if (_scanMode === "ma") {
                var sourceLabel = _scanSourceLabel();
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取：' + sourceLabel + '...</div>';
                var freq = _scanFreq;
                Promise.all([
                    fetch("/api/stocks/scan/start", { method: "POST" }),
                    _fetchMergedStocks(_scanSources, freq)
                ])
                    .then(function(resps) {
                        return resps[0].json().then(function(scanStartData) {
                            if (scanStartData.need_refresh) {
                                _scanRunning = false;
                                btn.classList.remove("active");
                                body.innerHTML = '<div class="scan-no-result" style="text-align:center;padding:20px;">' +
                                    '<div style="font-size:14px;color:#e94560;margin-bottom:12px;">&#9888; ' + scanStartData.msg + '</div>' +
                                    '<button class="btn" onclick="refreshStockNames();closeScanPanel();" style="margin-top:8px;">立即刷新</button>' +
                                    '</div>';
                                return null;
                            }
                            return resps[1];
                        });
                    })
                    .then(function(data) {
                        if (data === null) return;
                        if (!data || !data.stocks || data.stocks.length === 0) {
                            _scanRunning = false;
                            btn.classList.remove("active");
                            body.innerHTML = '<div class="scan-no-result">' + sourceLabel + '列表为空或文件不存在</div>';
                            return;
                        }
                        var stocks = data.stocks;
                        var total = stocks.length;
                        var preSkipped = data.pre_skipped || 0;
                        var results = [];
                        var skipped = 0;
                        var currentIdx = 0;
                        var completed = 0;
                        var hasRenderedAny = false;

                        body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，命中 0 只</div>';

                        function finishScan(interrupted) {
                            // 先清掉进度节流定时器，避免其 500ms 内的最后一次 _doUpdatePanel
                            // 把"正在扫描"写回 body，覆盖 renderScanResults 的结果（spinner 残留）
                            if (_updateTimer) { clearInterval(_updateTimer); _updateTimer = null; }
                            _pendingUpdate = false;
                            fetch("/api/stocks/scan/end", { method: "POST" }).then(function() {
                                renderMaScanResults(results, total + preSkipped, preSkipped + skipped, interrupted);
                            });
                        }

                        var _updateTimer = null;
                        var _pendingUpdate = false;
                        function updatePanel() {
                            if (_updateTimer) {
                                _pendingUpdate = true;
                                return;
                            }
                            _doUpdatePanel();
                            _updateTimer = setInterval(function() {
                                if (_pendingUpdate) {
                                    _pendingUpdate = false;
                                    _doUpdatePanel();
                                } else {
                                    clearInterval(_updateTimer);
                                    _updateTimer = null;
                                }
                            }, 500);
                        }
                        function _doUpdatePanel() {
                            var progress = completed + "/" + total;
                            var totalSkipped = preSkipped + skipped;
                            var html = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + progress + '，跳过 ' + totalSkipped + ' 只，命中 ' + results.length + ' 只</div>';
                            if (results.length > 0) {
                                hasRenderedAny = true;
                                var catCounts = {};
                                for (var i = 0; i < results.length; i++) {
                                    var c = results[i].ma_category;
                                    catCounts[c] = (catCounts[c] || 0) + 1;
                                }
                                var catParts = [];
                                for (var cat = 0; cat <= 8; cat++) {
                                    if (catCounts[cat]) catParts.push("类" + cat + " <b>" + catCounts[cat] + "</b> 只");
                                }
                                html += '<div class="scan-summary" style="margin-top:8px;">' + catParts.join("，") + '</div>';
                                // 按类别升序排序（类1→类9，最强→最弱）
                                results.sort(function(a, b) { return a.ma_category - b.ma_category; });
                                for (var i = 0; i < results.length; i++) {
                                    var r = results[i];
                                    var cat = r.ma_category;
                                    var catClass = 'ma-cat' + cat;
                                    var catLabel = '类' + cat;
                                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                                    html += chkBox(r.code, cat <= 3);
                                    html += '<span class="scan-col-name">' + r.name + '</span>';
                                    html += '<span class="scan-col-code">' + r.code + '</span>';
                                    html += '<span class="scan-col-tags"><span class="scan-bsp-tag ' + catClass + '">' + catLabel + '</span></span>';
                                    html += '</div>';
                                }
                            }
                            body.innerHTML = html;
                            updateScanSaveBtn();
                        }

                        // 提交到后端执行池，轮询增量结果
                        // （单票响应同形，模式过滤/渲染逻辑零改动）
                        btn.textContent = "中断扫描";
                        _asyncScanAll(stocks, {freq: freq, mode: "ma", recent: "1", source: _scanSources.join(",")}, function(data) {
                            completed++;
                            if (data.skipped) { skipped++; }
                            else if (data.error) { skipped++; }
                            else if (data.ma_category !== undefined && data.ma_category >= 0) {
                                results.push({
                                    code: data.code,
                                    name: data.name,
                                    ma_category: data.ma_category,
                                    last_close: data.last_close
                                });
                            }
                            updatePanel();
                        }, function(err, interrupted) {
                            if (err) {
                                console.error("[均线分类扫描] " + err);
                                _scanRunning = false;
                                _scanAborted = false;
                                btn.classList.remove("active");
                                btn.disabled = false;
                                btn.textContent = "股票扫描";
                                body.innerHTML = '<div class="scan-no-result">扫描失败: ' + err + '</div>';
                                return;
                            }
                            _scanRunning = false;
                            _scanAborted = false;
                            btn.classList.remove("active");
                            btn.disabled = false;
                            btn.textContent = "股票扫描";
                            finishScan(interrupted);
                        });
                    })
                    .catch(function(err) {
                        _scanRunning = false;
                        btn.classList.remove("active");
                        btn.textContent = "股票扫描";
                        body.innerHTML = '<div class="scan-no-result">读取' + sourceLabel + '失败: ' + err.message + '</div>';
                    });
                return;
            }

            // 放量扫描模式：找最近 N 根内成交额最大者为 A，且 A 大于其前 120 根的成交额峰值
            if (_scanMode === "fangliang") {
                var sourceLabel = _scanSourceLabel();
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取：' + sourceLabel + '...</div>';
                var freq = _scanFreq;
                Promise.all([
                    fetch("/api/stocks/scan/start", { method: "POST" }),
                    _fetchMergedStocks(_scanSources, freq)
                ])
                    .then(function(resps) {
                        return resps[0].json().then(function(scanStartData) {
                            if (scanStartData.need_refresh) {
                                _scanRunning = false;
                                btn.classList.remove("active");
                                body.innerHTML = '<div class="scan-no-result" style="text-align:center;padding:20px;">' +
                                    '<div style="font-size:14px;color:#e94560;margin-bottom:12px;">&#9888; ' + scanStartData.msg + '</div>' +
                                    '<button class="btn" onclick="refreshStockNames();closeScanPanel();" style="margin-top:8px;">立即刷新</button>' +
                                    '</div>';
                                return null;
                            }
                            return resps[1];
                        });
                    })
                    .then(function(data) {
                        if (data === null) return;
                        if (!data || !data.stocks || data.stocks.length === 0) {
                            _scanRunning = false;
                            btn.classList.remove("active");
                            body.innerHTML = '<div class="scan-no-result">' + sourceLabel + '列表为空或文件不存在</div>';
                            return;
                        }
                        var stocks = data.stocks;
                        var total = stocks.length;
                        var preSkipped = data.pre_skipped || 0;
                        var results = [];
                        var skipped = 0;
                        var currentIdx = 0;
                        var completed = 0;
                        var hasRenderedAny = false;

                        body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，放量 0 只</div>';

                        function finishScan(interrupted) {
                            if (_updateTimer) { clearInterval(_updateTimer); _updateTimer = null; }
                            _pendingUpdate = false;
                            fetch("/api/stocks/scan/end", { method: "POST" }).then(function() {
                                renderFangliangScanResults(results, total + preSkipped, preSkipped + skipped, interrupted);
                            });
                        }

                        var _updateTimer = null;
                        var _pendingUpdate = false;
                        function updatePanel() {
                            if (_updateTimer) {
                                _pendingUpdate = true;
                                return;
                            }
                            _doUpdatePanel();
                            _updateTimer = setInterval(function() {
                                if (_pendingUpdate) {
                                    _pendingUpdate = false;
                                    _doUpdatePanel();
                                } else {
                                    clearInterval(_updateTimer);
                                    _updateTimer = null;
                                }
                            }, 500);
                        }
                        function _doUpdatePanel() {
                            var progress = completed + "/" + total;
                            var totalSkipped = preSkipped + skipped;
                            var html = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + progress + '，跳过 ' + totalSkipped + ' 只，放量 ' + results.length + ' 只</div>';
                            if (results.length > 0) {
                                hasRenderedAny = true;
                                var shCount = 0, szCount = 0, bjCount = 0, hkCount = 0;
                                for (var i = 0; i < results.length; i++) {
                                    var parts = results[i].code.split(".");
                                    var mkt = parts.length > 1 ? parts[1] : "";
                                    if (mkt === "SH") { shCount++; }
                                    else if (mkt === "SZ") { szCount++; }
                                    else if (mkt === "BJ") { bjCount++; }
                                    else if (mkt === "HK") { hkCount++; }
                                }
                                var marketParts = [];
                                if (shCount > 0) marketParts.push("上海 <b>" + shCount + "</b> 只");
                                if (szCount > 0) marketParts.push("深圳 <b>" + szCount + "</b> 只");
                                if (bjCount > 0) marketParts.push("北京 <b>" + bjCount + "</b> 只");
                                if (hkCount > 0) marketParts.push("香港 <b>" + hkCount + "</b> 只");
                                html += '<div class="scan-summary" style="margin-top:8px;">' + marketParts.join("，") + '</div>';
                                // 动态排序：红色（A为阳线，a_is_rise=true）排前面，绿色排后面；仅红色勾选
                                results.sort(function(a, b) {
                                    return ((b.a_is_rise ? 1 : 0) - (a.a_is_rise ? 1 : 0));
                                });
                                for (var i = 0; i < results.length; i++) {
                                    var r = results[i];
                                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                                    html += chkBox(r.code, !!r.a_is_rise);
                                    html += '<span class="scan-col-name">' + r.name + '</span>';
                                    html += '<span class="scan-col-code">' + r.code + '</span>';
                                    html += '<span class="scan-col-tags">' + buildFangliangTagHtml(r) + '</span>';
                                    html += '</div>';
                                }
                            }
                            body.innerHTML = html;
                            updateScanSaveBtn();
                        }

                        // 提交到后端执行池，轮询增量结果
                        btn.textContent = "中断扫描";
                        _asyncScanAll(stocks, {freq: freq, mode: "fangliang", recent: _scanRecentDays, source: _scanSources.join(",")}, function(data) {
                            completed++;
                            if (data.skipped) { skipped++; }
                            else if (data.error) { skipped++; }
                            else if (data.is_fangliang) { results.push(data); }
                            updatePanel();
                        }, function(err, interrupted) {
                            if (err) {
                                console.error("[放量扫描] " + err);
                                _scanRunning = false;
                                _scanAborted = false;
                                btn.classList.remove("active");
                                btn.disabled = false;
                                btn.textContent = "股票扫描";
                                body.innerHTML = '<div class="scan-no-result">扫描失败: ' + err + '</div>';
                                return;
                            }
                            _scanRunning = false;
                            _scanAborted = false;
                            btn.classList.remove("active");
                            btn.disabled = false;
                            btn.textContent = "股票扫描";
                            finishScan(interrupted);
                        });
                    })
                    .catch(function(err) {
                        _scanRunning = false;
                        btn.classList.remove("active");
                        btn.textContent = "股票扫描";
                        body.innerHTML = '<div class="scan-no-result">读取' + sourceLabel + '失败: ' + err.message + '</div>';
                    });
                return;
            }

            // 买卖点扫描模式（原有逻辑）
            // 第一步：通知后端开始新扫描 + 合并多来源股票列表
            var sourceLabel = _scanSourceLabel();
            body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取：' + sourceLabel + '...</div>';
            Promise.all([
                fetch("/api/stocks/scan/start", { method: "POST" }),
                _fetchMergedStocks(_scanSources, freq)
            ])
                .then(function(resps) {
                    // 先检查 scan_start 的响应
                    return resps[0].json().then(function(scanStartData) {
                        if (scanStartData.need_refresh) {
                            // 需要刷新缓存数据
                            _scanRunning = false;
                            btn.classList.remove("active");
                            body.innerHTML = '<div class="scan-no-result" style="text-align:center;padding:20px;">' +
                                '<div style="font-size:14px;color:#e94560;margin-bottom:12px;">&#9888; ' + scanStartData.msg + '</div>' +
                                '<button class="btn" onclick="refreshStockNames();closeScanPanel();" style="margin-top:8px;">立即刷新</button>' +
                                '</div>';
                            return null;
                        }
                        // scan_start 正常，继续处理股票列表（_fetchMergedStocks 已返回去重数组）
                        return resps[1];
                    });
                })
                .then(function(data) {
                    if (data === null) return; // 已处理 need_refresh
                    if (!data || !data.stocks || data.stocks.length === 0) {
                        _scanRunning = false;
                        btn.classList.remove("active");
                        body.innerHTML = '<div class="scan-no-result">' + sourceLabel + '列表为空或文件不存在</div>';
                        return;
                    }
                    var stocks = data.stocks;
                    var total = stocks.length;
                    var preSkipped = data.pre_skipped || 0;
                    var results = [];
                    var skipped = 0;
                    var currentIdx = 0;
                    var completed = 0;
                    var hasRenderedAny = false;

                    // 立即更新面板为扫描进度，不等第一批请求返回
                    body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，买点 0 只，卖点 0 只</div>';

                    // 扫描结束统一通知后端打印
                    function finishScan(interrupted) {
                        // 先清掉进度节流定时器，避免其 500ms 内的最后一次 _doUpdatePanel
                        // 把"正在扫描"写回 body，覆盖 renderScanResults 的结果（spinner 残留）
                        if (_updateTimer) { clearInterval(_updateTimer); _updateTimer = null; }
                        _pendingUpdate = false;
                        fetch("/api/stocks/scan/end", { method: "POST" }).then(function() {
                            renderScanResults(results, total + preSkipped, preSkipped + skipped, interrupted);
                        });
                    }

                    // 实时更新面板：显示进度 + 已找到的买卖点股票
                    var _updateTimer = null;
                    var _pendingUpdate = false;
                    function updatePanel() {
                        // 节流：最多500ms更新一次，避免阻塞主线程导致"中断扫描"按钮无响应
                        if (_updateTimer) {
                            _pendingUpdate = true;
                            return;
                        }
                        _doUpdatePanel();
                        _updateTimer = setInterval(function() {
                            if (_pendingUpdate) {
                                _pendingUpdate = false;
                                _doUpdatePanel();
                            } else {
                                clearInterval(_updateTimer);
                                _updateTimer = null;
                            }
                        }, 500);
                    }
                    function _doUpdatePanel() {
                        var progress = completed + "/" + total;
                        var totalSkipped = preSkipped + skipped;
                        var buyCount = 0, sellCount = 0;
                        for (var i = 0; i < results.length; i++) {
                            if (isLatestBspBuy(results[i])) { buyCount++; } else { sellCount++; }
                        }
                        var html = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + progress + '，跳过 ' + totalSkipped + ' 只，买点 ' + buyCount + ' 只，卖点 ' + sellCount + ' 只</div>';
                        // 如果已经找到一些结果，实时显示出来
                        if (results.length > 0) {
                            hasRenderedAny = true;
                            var shCount = 0, szCount = 0, bjCount = 0, hkCount = 0;
                            for (var i = 0; i < results.length; i++) {
                                var parts = results[i].code.split(".");
                                var mkt = parts.length > 1 ? parts[1] : "";
                                if (mkt === "SH") { shCount++; }
                                else if (mkt === "SZ") { szCount++; }
                                else if (mkt === "BJ") { bjCount++; }
                                else if (mkt === "HK") { hkCount++; }
                            }
                            var marketParts = [];
                            if (shCount > 0) marketParts.push("上海 <b>" + shCount + "</b> 只");
                            if (szCount > 0) marketParts.push("深圳 <b>" + szCount + "</b> 只");
                            if (bjCount > 0) marketParts.push("北京 <b>" + bjCount + "</b> 只");
                            if (hkCount > 0) marketParts.push("香港 <b>" + hkCount + "</b> 只");
                            html += '<div class="scan-summary" style="margin-top:8px;">' + marketParts.join("，") + '</div>';
                            // 按最新买卖点类型排序：先买点后卖点，内部 1→2→3→0
                            results.sort(function(a, b) { return getLatestBspSortKey(a) - getLatestBspSortKey(b); });
                            for (var i = 0; i < results.length; i++) {
                                var r = results[i];
                                var tagsHtml = buildBspTagsHtml(r.buy_points, r.sell_points);
                                html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                                html += chkBox(r.code, isLatestBspBuy(r));
                                html += '<span class="scan-col-name">' + r.name + '</span>';
                                html += '<span class="scan-col-code">' + r.code + '</span>';
                                html += buildMa120Html(r);
                                html += '<span class="scan-col-tags">' + tagsHtml + '</span>';
                                html += '</div>';
                            }
                        }
                        body.innerHTML = html;
                        updateScanSaveBtn();
                    }

                    // 提交到后端执行池，轮询增量结果
                    // （单票响应同形，模式过滤/渲染逻辑零改动）
                    // mode=""（空）即买卖点扫描；recent 传最近 N 根过滤
                    btn.textContent = "中断扫描";
                    _asyncScanAll(stocks, {freq: freq, mode: "", recent: _scanRecentDays, source: _scanSources.join(",")}, function(data) {
                        completed++;
                        if (data.skipped) { skipped++; }
                        else if (data.error) { skipped++; }
                        else if ((data.buy_points && data.buy_points.length > 0) || (data.sell_points && data.sell_points.length > 0)) {
                            results.push(data);
                        }
                        updatePanel();
                    }, function(err, interrupted) {
                        if (err) {
                            console.error("[买卖点扫描] " + err);
                            _scanRunning = false;
                            _scanAborted = false;
                            btn.classList.remove("active");
                            btn.disabled = false;
                            btn.textContent = "股票扫描";
                            body.innerHTML = '<div class="scan-no-result">扫描失败: ' + err + '</div>';
                            return;
                        }
                        _scanRunning = false;
                        _scanAborted = false;
                        btn.classList.remove("active");
                        btn.disabled = false;
                        btn.textContent = "股票扫描";
                        finishScan(interrupted);
                    });
                })
                .catch(function(err) {
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "股票扫描";
                    body.innerHTML = '<div class="scan-no-result">读取' + sourceLabel + '失败: ' + err.message + '</div>';
                });
        }

        // ============================================================
        // 股票名称刷新（原 GBBQ 刷新，现在仅刷新股票名称缓存）
        // ============================================================
        window.refreshStockNames = function() {
            var btn = document.getElementById("btn-refresh");
            var status = document.getElementById("refresh-status");
            if (btn.disabled) return;
            btn.disabled = true;
            btn.classList.add("active");
            btn.querySelector("svg").style.animation = "spin 1s linear infinite";
            status.style.display = "inline";
            status.textContent = "正在刷新股票名称...";

            fetch("/api/stocks/refresh", { method: "POST" })
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.status === "already_running") {
                        pollRefreshStatus(btn, status);
                    } else {
                        pollRefreshStatus(btn, status);
                    }
                })
                .catch(function(err) {
                    btn.disabled = false;
                    btn.classList.remove("active");
                    btn.querySelector("svg").style.animation = "";
                    status.style.display = "none";
                    alert("启动刷新失败: " + err.message);
                });
        };

        function pollRefreshStatus(btn, status) {
            fetch("/api/stocks/refresh/read/status")
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.running) {
                        status.textContent = data.step || "刷新中...";
                        setTimeout(function() { pollRefreshStatus(btn, status); }, 500);
                    } else {
                        btn.disabled = false;
                        btn.classList.remove("active");
                        btn.querySelector("svg").style.animation = "";
                        if (data.error) {
                            status.textContent = "刷新失败";
                            alert("刷新失败: " + data.error);
                        } else {
                            status.textContent = "刷新完成";
                            setTimeout(function() { status.style.display = "none"; }, 2000);
                        }
                    }
                })
                .catch(function() {
                    btn.disabled = false;
                    btn.classList.remove("active");
                    btn.querySelector("svg").style.animation = "";
                    status.style.display = "none";
                });
        }

        function renderScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var buyCount = 0, sellCount = 0;
            for (var i = 0; i < results.length; i++) {
                if (isLatestBspBuy(results[i])) { buyCount++; } else { sellCount++; }
            }
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total + '</b> 只，跳过 <b>' + skipped + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，买点 <b>' + buyCount + '</b> 只，卖点 <b>' + sellCount + '</b> 只' + label + '</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现买卖点股票</div>';
            } else {
                // 按最新买卖点类型排序：一类(1)→二类(2)→三类(3)→0类(0)
                results.sort(function(a, b) { return getLatestBspSortKey(a) - getLatestBspSortKey(b); });
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    var tagsHtml = buildBspTagsHtml(r.buy_points, r.sell_points);
                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                    html += chkBox(r.code, isLatestBspBuy(r));
                    html += '<span class="scan-col-name">' + r.name + '</span>';
                    html += '<span class="scan-col-code">' + r.code + '</span>';
                    html += buildMa120Html(r);
                    html += '<span class="scan-col-tags">' + tagsHtml + '</span>';
                    html += '</div>';
                }
            }
            body.innerHTML = html;
            updateScanSaveBtn();
        }

        // 底分型扫描结果渲染
        function renderFxDScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var strongest = 0, strong = 0, weak = 0;
            for (var i = 0; i < results.length; i++) {
                var s = results[i].fx_strength;
                if (s === 2) { strongest++; }
                else if (s === 1) { strong++; }
                else { weak++; }
            }
            var fxSummary = results.length + ' 只（' + strongest + ' / ' + strong + ' / ' + weak + '）';
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total + '</b> 只，跳过 <b>' + skipped + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，底分型 <b>' + fxSummary + '</b>' + label + '</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现底分型股票</div>';
            } else {
                // 按分型强度降序排序（最强分型→强分型→弱分型）
                results.sort(function(a, b) { return b.fx_strength - a.fx_strength; });
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    var fxLabel = '底分型';
                    var fxClass = 'fx-d';
                    var checked = false;
                    if (r.fx_strength === 2) { fxLabel = '最强分型'; fxClass = 'fx-strongest'; checked = true; }
                    else if (r.fx_strength === 1) { fxLabel = '强分型'; fxClass = 'fx-strong'; checked = true; }
                    else { fxLabel = '弱分型'; fxClass = 'fx-weak'; }
                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                    html += chkBox(r.code, checked);
                    html += '<span class="scan-col-name">' + r.name + '</span>';
                    html += '<span class="scan-col-code">' + r.code + '</span>';
                    html += '<span class="scan-col-tags"><span class="scan-bsp-tag ' + fxClass + '">' + fxLabel + '</span></span>';
                    html += '</div>';
                }
            }
            body.innerHTML = html;
            updateScanSaveBtn();
        }

        // 均线分类扫描结果渲染
        function renderMaScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var catCounts = {};
            for (var i = 0; i < results.length; i++) {
                var c = results[i].ma_category;
                catCounts[c] = (catCounts[c] || 0) + 1;
            }
            var catParts = [];
            for (var cat = 0; cat <= 8; cat++) {
                if (catCounts[cat]) catParts.push("类" + cat + " <b>" + catCounts[cat] + "</b> 只");
            }
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total + '</b> 只，跳过 <b>' + skipped + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，' + (catParts.length > 0 ? catParts.join("，") : '无') + label + '</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现均线分类结果</div>';
            } else {
                // 按类别升序排序（类1→类9，最强→最弱）
                results.sort(function(a, b) { return a.ma_category - b.ma_category; });
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    var cat = r.ma_category;
                    var catClass = 'ma-cat' + cat;
                    var catLabel = '类' + cat;
                    var checked = cat <= 3;
                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                    html += chkBox(r.code, checked);
                    html += '<span class="scan-col-name">' + r.name + '</span>';
                    html += '<span class="scan-col-code">' + r.code + '</span>';
                    html += '<span class="scan-col-tags"><span class="scan-bsp-tag ' + catClass + '">' + catLabel + '</span></span>';
                    html += '</div>';
                }
            }
            body.innerHTML = html;
            updateScanSaveBtn();
        }

        // 放量扫描结果渲染
        function renderFangliangScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total + '</b> 只，跳过 <b>' + skipped + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，放量 <b>' + results.length + '</b> 只' + label + '</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现放量标的</div>';
            } else {
                // 动态排序：红色（A为阳线，a_is_rise=true）排前面，绿色排后面；仅红色勾选
                results.sort(function(a, b) {
                    return ((b.a_is_rise ? 1 : 0) - (a.a_is_rise ? 1 : 0));
                });
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                    html += chkBox(r.code, !!r.a_is_rise);
                    html += '<span class="scan-col-name">' + r.name + '</span>';
                    html += '<span class="scan-col-code">' + r.code + '</span>';
                    html += '<span class="scan-col-tags">' + buildFangliangTagHtml(r) + '</span>';
                    html += '</div>';
                }
            }
            body.innerHTML = html;
            updateScanSaveBtn();
        }

        // 生成复选框HTML
        function isLatestBspBuy(r) {
            var buyPoints = r.buy_points || [];
            var sellPoints = r.sell_points || [];
            if (buyPoints.length === 0 && sellPoints.length === 0) return false;
            var lastBuyDate = buyPoints.length > 0 ? buyPoints[buyPoints.length - 1].date : "";
            var lastSellDate = sellPoints.length > 0 ? sellPoints[sellPoints.length - 1].date : "";
            // 最近的是买点
            if (!lastBuyDate && !lastSellDate) return false;
            if (!lastSellDate) return true;
            if (!lastBuyDate) return false;
            return lastBuyDate >= lastSellDate;
        }

        // 获取最新买卖点的两层排序键：
        // 第一层：先买点(0) 后卖点(1)；第二层：1→2→3→0
        // 返回数值越小排越前：买点一类→1, 买点二类→2, 买点三类→3, 买点0类→4,
        //                       卖点一类→11, 卖点二类→12, 卖点三类→13, 卖点0类→14, 无买卖点→99
        function getLatestBspSortKey(r) {
            var buyPoints = r.buy_points || [];
            var sellPoints = r.sell_points || [];
            if (buyPoints.length === 0 && sellPoints.length === 0) return 99;
            var lastBuyDate = buyPoints.length > 0 ? buyPoints[buyPoints.length - 1].date : "";
            var lastSellDate = sellPoints.length > 0 ? sellPoints[sellPoints.length - 1].date : "";
            var latestPoint = null;
            var isBuy = false;
            if (!lastSellDate) { latestPoint = buyPoints[buyPoints.length - 1]; isBuy = true; }
            else if (!lastBuyDate) { latestPoint = sellPoints[sellPoints.length - 1]; isBuy = false; }
            else if (lastBuyDate >= lastSellDate) { latestPoint = buyPoints[buyPoints.length - 1]; isBuy = true; }
            else { latestPoint = sellPoints[sellPoints.length - 1]; isBuy = false; }
            var tp = (latestPoint.type || "").replace(/\s/g, "");
            var typeKey = 99;
            if (tp === "1") typeKey = 1;
            else if (tp === "2") typeKey = 2;
            else if (tp === "3") typeKey = 3;
            else if (tp === "0") typeKey = 4;
            return (isBuy ? 0 : 10) + typeKey;
        }

        function chkBox(code, checked) {
            return '<span class="scan-col-chk" onclick="event.stopPropagation()"><input type="checkbox" value="' + code + '" onchange="updateScanSaveBtn()" ' + (checked ? 'checked' : '') + '/></span>';
        }

        // 收集勾选的代码并更新按钮状态
        window.updateScanSaveBtn = function() {
            var checks = document.querySelectorAll("#scan-body .scan-col-chk input[type=checkbox]:checked");
            var allCbs = document.querySelectorAll("#scan-body .scan-col-chk input[type=checkbox]");
            var btn = document.getElementById("scan-save-btn");
            btn.disabled = allCbs.length === 0;
            btn.textContent = checks.length > 0 ? "保存到自选(" + checks.length + ")" : "保存到自选";
        };

        // 保存勾选到自选股（通达信+同花顺）
        window.saveScanToZxg = function() {
            var checks = document.querySelectorAll("#scan-body .scan-col-chk input[type=checkbox]:checked");
            if (checks.length === 0) return;
            var codes = [];
            checks.forEach(function(cb) { codes.push(cb.value); });
            var btn = document.getElementById("scan-save-btn");
            btn.disabled = true;
            btn.textContent = "保存中...";
            fetch("/api/stocks/scan/save/zxg?codes=" + encodeURIComponent(codes.join(",")), { method: "POST" })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                // 保存结果用与扫描面板汇总行一致的亮度：普通文字 #a8b2d1，高亮数字/状态 #e94560
                btn.style.opacity = "1";
                var parts = [];
                if (data.tdx_saved > 0) {
                    parts.push("<span style='color:#a8b2d1'>通达信：</span><span style='color:#e94560'>" + data.tdx_saved + "</span><span style='color:#a8b2d1'> 只</span>");
                } else {
                    parts.push("<span style='color:#a8b2d1'>通达信：</span><span style='color:#e94560'> 已保存</span>");
                }
                // 同花顺状态：区分成功/已存在/失败/未配置
                if (data.ths_saved > 0) {
                    parts.push("<span style='color:#a8b2d1'>同花顺：</span><span style='color:#e94560'>" + data.ths_saved + "</span><span style='color:#a8b2d1'> 只</span>");
                } else if (!data.ths_msg || data.ths_msg === "THS_DIR 未配置") {
                    // 未配置同花顺目录，静默不显示
                } else if (data.ths_msg === "ok") {
                    parts.push("<span style='color:#a8b2d1'>同花顺：</span><span style='color:#e94560'> 已保存</span>");
                } else {
                    parts.push("<span style='color:#a8b2d1'>同花顺：失败</span>");
                    console.warn("[THS] 保存失败:", data.ths_msg);
                }
                btn.innerHTML = parts.join("&nbsp;&nbsp;&nbsp;");
                setTimeout(function() {
                    btn.textContent = "保存到自选";
                    btn.disabled = false;
                    btn.style.opacity = "";
                    updateScanSaveBtn();
                }, 2000);
            })
            .catch(function() {
                btn.textContent = "保存失败";
                btn.disabled = false;
                btn.style.opacity = "";
            });
        };

        window.closeScanPanel = function() {
            // 扫描中不允许关闭面板，用户需通过"中断扫描"按钮停止
            if (_scanRunning) return;
            document.getElementById("scan-panel").classList.remove("show");
            // 关闭面板时清除扫描缓存，释放内存
            fetch("/api/stocks/scan/close", { method: "POST" }).catch(function() {});
        };

        window.toggleScanMinimize = function() {
            // 最小化/恢复扫描面板，扫描可后台继续不中断
            var panel = document.getElementById("scan-panel");
            var btn = document.querySelector(".scan-minimize");
            panel.classList.toggle("minimized");
            if (btn) {
                btn.innerHTML = panel.classList.contains("minimized") ? "+" : "-";
                btn.title = panel.classList.contains("minimized") ? "恢复面板" : "最小化面板";
            }
        };

        window.loadScanResult = function(code, freq) {
            // 加载该股票到当前页面，不关闭面板
            // 传入 freq（标注所在周期），确保用正确的周期加载K线，避免标注因周期不匹配而不显示
            document.getElementById("stock-code-input").value = code;
            if (freq) {
                lastStockFreq = freq; // 让 loadStock 使用标注所在的周期
            }
            loadStock();
        };


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.ScanPanel = {
            startScanZxg, doStartScan, scanModeDialogConfirm, scanModeDialogCancel,
        renderScanResults, renderFxDScanResults, renderMaScanResults,
        saveScanToZxg, closeScanPanel, toggleScanMinimize, loadScanResult,
        refreshStockNames, updateScanSaveBtn, scanSourceSelectAll, scanSourceSelectNone
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] AmoPanel —— 市场量能面板（右上角「市场量能」按钮）
        // 数据源仅 TDX 本地指数日线（sh000001 + sz399106 成交额相加），无兜底
        // 仅上证指数(sh000001)日K图可用，其它周期/标的按钮灰化
// ══════════════════════════════════════════════════════════════════

        function amoIsAvailable() {
            return !!(chartData && chartData.meta
                && chartData.meta.symbol === 'sh000001'
                && currentFreq === 'd');
        }

        function updateAmoButtonState() {
            var btn = document.getElementById("btn-amo");
            if (!btn) return;
            var available = amoIsAvailable();
            btn.disabled = !available;
            btn.title = available ? "市场量能" : "市场量能（仅上证指数日K可用）";
            // 面板打开时若变为不可用（切股票/切周期）→ 关闭面板释放数据
            if (!available) {
                var panel = document.getElementById("amo-panel");
                if (panel && panel.classList.contains("show")) {
                    closeAmoPanel();
                }
            }
        }

        window.toggleAmoPanel = function() {
            if (!amoIsAvailable()) return;
            var panel = document.getElementById("amo-panel");
            if (panel.classList.contains("show")) {
                closeAmoPanel();
            } else {
                panel.classList.add("show");
                loadAmoData();
            }
        };

        window.closeAmoPanel = function() {
            var panel = document.getElementById("amo-panel");
            if (panel) panel.classList.remove("show");
            // 关闭面板即释放数据（无持久化）
        };

        // 打开面板后点击面板之外区域 → 自动关闭面板并释放数据
        document.addEventListener("mousedown", function(e) {
            var panel = document.getElementById("amo-panel");
            if (!panel || !panel.classList.contains("show")) return;
            // 点击面板内部 或「市场量能」按钮本身（避免与按钮 toggle 抢）→ 不关闭
            if (panel.contains(e.target)) return;
            var btn = document.getElementById("btn-amo");
            if (btn && btn.contains(e.target)) return;
            closeAmoPanel();
        });

        function getViewportDateRange() {
            // 与 K 线可见视口「严格对齐」的日期区间：取最左/最右「落在画布内」的 K 线日期。
            // 不能直接用 getVisibleKlines() 的首/末元素——其末尾多取 viewCount+2 根
            // overscan，会把「视口右缘之外、屏幕上看不到」的 K 线也算进来，导致面板
            // 右边界比 K 线偏晚（如 K 线 1.13 而面板 1.15）。故右缘用 viewOffset+viewCount-1。
            if (!chartData || !chartData.klines || !chartData.klines.length) return null;
            var kl = chartData.klines;
            var start = Math.max(0, Math.floor(viewOffset));
            var end = Math.max(start, Math.min(kl.length - 1, Math.floor(viewOffset + viewCount) - 1));
            if (end < start) return null;
            return {
                startDate: kl[start].date.slice(0, 10),
                endDate: kl[end].date.slice(0, 10),
            };
        }

        function loadAmoData() {
            var range = getViewportDateRange();
            if (!range) return;
            // 视口左右边界日期（K线页面「视口」最左/最右可见 K 线）
            var startDate = range.startDate;
            var endDate = range.endDate;
            fetch("/api/amo/read?start_date=" + encodeURIComponent(startDate)
                + "&end_date=" + encodeURIComponent(endDate))
                .then(function(resp) {
                    if (!resp.ok) return resp.json().then(function(e) { throw new Error(e.detail || e.error || "查询失败"); });
                    return resp.json();
                })
                .then(function(data) {
                    renderAmoChart(data);
                })
                .catch(function(err) {
                    var empty = document.getElementById("amo-empty");
                    var canvas = document.getElementById("amo-chart");
                    if (empty) { empty.style.display = "block"; empty.textContent = "加载失败: " + err.message; }
                    if (canvas) { var ctx = canvas.getContext("2d"); ctx.clearRect(0, 0, canvas.width, canvas.height); }
                });
        }

        function fmtAmt(amtYi) {
            // 零售额/峰值金额友好显示：amtYi 单位为「亿元」。
            // >=10000亿（即1万亿）按「万亿」显示，否则按「亿」显示。
            if (amtYi == null) return "--";
            var a = Number(amtYi);
            if (Math.abs(a) >= 10000) return (a / 10000).toFixed(2) + " 万亿";
            return a + " 亿";
        }

        function fmtAxisDate(d) {
            // 全站日期契约 %Y/%m/%d；横轴仅展示 YY/MM/DD，标签更紧凑不至于裁切
            return d && d.length >= 10 ? d.slice(2) : d;
        }

        function renderAmoChart(data) {
            var canvas = document.getElementById("amo-chart");
            var empty = document.getElementById("amo-empty");
            if (!canvas) return;
            var ctx = canvas.getContext("2d");
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            var dates = data.dates || [];
            var amounts = data.amounts || [];
            var stats = data.stats || {};

            // 统计栏
            document.getElementById("amo-current").textContent = fmtAmt(stats.current);
            document.getElementById("amo-peak").textContent = fmtAmt(stats.peak);
            var shrinkEl = document.getElementById("amo-shrink");
            // 缩至峰值占比 = 当前成交额 / 峰值成交额 ×100，口径同市场量能文章
            // （"成交额缩至峰值的百分之几"，例子 3.45万亿→0.97万亿=28%）。
            // 越低越接近地量底部：按文章规律，"回落至约50%及以下"进入接近阈值区(高亮)。
            if (stats.peak_ratio != null) {
                shrinkEl.textContent = stats.peak_ratio + "%";
                shrinkEl.classList.add("shrink");
                shrinkEl.classList.toggle("warn", stats.peak_ratio <= 50);
            } else {
                shrinkEl.textContent = "--";
                shrinkEl.classList.remove("shrink", "warn");
            }

            if (!dates.length) {
                if (empty) { empty.style.display = "block"; empty.textContent = "当前视口区间无成交额数据"; }
                return;
            }
            if (empty) empty.style.display = "none";

            var W = canvas.width, H = canvas.height;
            var padL = 8, padR = 8, padT = 12, padB = 24;
            var plotW = W - padL - padR;
            var plotH = H - padT - padB;
            var maxA = Math.max.apply(null, amounts);
            var minA = Math.min.apply(null, amounts);
            if (maxA === minA) maxA = minA + 1;
            var range = maxA - minA;
            var padRange = range * 0.1;
            var yMax = maxA + padRange;
            var yMin = Math.max(0, minA - padRange);
            var yRange = (yMax - yMin) || 1;

            function x(i) { return padL + (dates.length === 1 ? plotW / 2 : (i / (dates.length - 1)) * plotW); }
            function y(v) { return padT + (1 - (v - yMin) / yRange) * plotH; }

            // 网格线
            ctx.strokeStyle = "rgba(15,52,96,0.4)";
            ctx.lineWidth = 1;
            for (var g = 0; g <= 4; g++) {
                var gy = padT + (g / 4) * plotH;
                ctx.beginPath();
                ctx.moveTo(padL, gy);
                ctx.lineTo(W - padR, gy);
                ctx.stroke();
            }

            // 面积填充
            ctx.beginPath();
            ctx.moveTo(x(0), y(amounts[0]));
            for (var i = 1; i < amounts.length; i++) ctx.lineTo(x(i), y(amounts[i]));
            ctx.lineTo(x(amounts.length - 1), padT + plotH);
            ctx.lineTo(x(0), padT + plotH);
            ctx.closePath();
            var grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
            grad.addColorStop(0, "rgba(233,69,96,0.35)");
            grad.addColorStop(1, "rgba(233,69,96,0.02)");
            ctx.fillStyle = grad;
            ctx.fill();

            // 曲线
            ctx.beginPath();
            ctx.moveTo(x(0), y(amounts[0]));
            for (var j = 1; j < amounts.length; j++) ctx.lineTo(x(j), y(amounts[j]));
            ctx.strokeStyle = "#e94560";
            ctx.lineWidth = 1.6;
            ctx.stroke();

            // 峰值点标记（金色圆点 + 数值）
            var peakIdx = 0;
            for (var p = 1; p < amounts.length; p++) if (amounts[p] > amounts[peakIdx]) peakIdx = p;
            ctx.beginPath();
            ctx.arc(x(peakIdx), y(amounts[peakIdx]), 3.5, 0, Math.PI * 2);
            ctx.fillStyle = "#ffd700";
            ctx.fill();
            ctx.strokeStyle = "#fff";
            ctx.lineWidth = 1;
            ctx.stroke();
            ctx.fillStyle = "#ffd700";
            // 与统计栏「峰值成交额」数值（.amo-value 13px）同字号
            ctx.font = "12px sans-serif";
            ctx.fillText("峰值 " + fmtAmt(amounts[peakIdx]), x(peakIdx) + 6, y(amounts[peakIdx]) - 6);

            // 当前点标记（最右，青色圆点）
            var lastIdx = amounts.length - 1;
            ctx.beginPath();
            ctx.arc(x(lastIdx), y(amounts[lastIdx]), 3, 0, Math.PI * 2);
            ctx.fillStyle = "#64ffda";
            ctx.fill();

            // 日期轴（首/中/尾）：YY/MM/DD 紧凑格式；首左对齐、尾右对齐避免被画布裁掉
            ctx.fillStyle = "#8892b0";
            // 日期轴：比峰值文字再小一号 → 12px（原 10px 放大一号）
            ctx.font = "12px sans-serif";
            var midIdx = Math.floor((dates.length - 1) / 2);
            ctx.textAlign = "left";
            ctx.fillText(fmtAxisDate(dates[0]), padL, H - 8);
            ctx.textAlign = "center";
            ctx.fillText(fmtAxisDate(dates[midIdx]), x(midIdx), H - 8);
            ctx.textAlign = "right";
            ctx.fillText(fmtAxisDate(dates[lastIdx]), W - padR, H - 8);
            ctx.textAlign = "left";
        }

        // 注册组件对外接口
        ChanApp.components.AmoPanel = {
            toggleAmoPanel, closeAmoPanel, updateAmoButtonState
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] RealtimeService —— 实时行情服务组件（期货 SSE 连接 / 增量上屏）
        // 对外接口（ChanApp.components.RealtimeService）: startRealtimeIfFutures, connectRealtimeInit, connectRealtimeDual, connectRealtime, disconnectRealtime, handleRealtimeDataSingle, handleRealtimeDataDual
// ══════════════════════════════════════════════════════════════════

        // ========== 期货实时模式 ==========
        function startRealtimeIfFutures(data) {
            // 检查是否是期货/期指品种（股票路径中用于断开SSE，期货路径中用于连SSE）
            const isFutures = data.meta.market === 'futures';
            const badge = document.getElementById('realtime-badge');

            if (isFutures) {
                const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d','周线':'w'};
                if (data.meta.freq) {
                    currentFreq = freqMap[data.meta.freq] || currentFreq;
                }
                lastFuturesFreq = currentFreq; // 记录期货周期
                updateFreqButtonStates(true);
                connectRealtime(data.meta.symbol);
            } else {
                disconnectRealtime();
                updateFreqButtonStates(false);
            }
        }

        // ========== SSE 初始化连接（初始快照 + 增量合一） ==========
        function connectRealtimeInit(symbol, freq, startTime, endTime) {
            disconnectRealtime();
            realtimeSymbol = symbol;
            realtimeFreq = freq;
            realtimeStartTime = startTime || null;
            realtimeEndTime = endTime || null; // 复盘软断开边界
            isRealtimeMode = true;
            startCountdownTimer();
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';
            // loading 由调用方（loadStock/switchFreq）已设置

            try {
                let sseUrl = '/api/futures/read/stream?symbol=' + encodeURIComponent(symbol) + '&freq=' + encodeURIComponent(freq || '1m');
                if (startTime) {
                    sseUrl += '&start_time=' + encodeURIComponent(startTime);
                }
                if (endTime) {
                    // 复盘软断开：把终点传入前端，后端把更新停在该边界，不拉最新
                    sseUrl += '&end_time=' + encodeURIComponent(endTime);
                }
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                // init 事件：初始全量快照
                realtimeEventSource.addEventListener('init', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.error) {
                            console.warn('引擎未就绪:', data.error);
                            disconnectRealtime();
                            document.getElementById("loading").classList.add("hidden");
                            return;
                        }
                        // 全量初始数据
                        chartData = data;
                        // 用后端解析后的完整代码保存历史，避免别名导致历史记录不一致
                        const resolvedSymbol = data.meta.symbol || symbol;
                        saveHistory(resolvedSymbol, data.meta.name);
                        // 同步 realtimeSymbol 为解析后的完整代码
                        realtimeSymbol = resolvedSymbol;
                        // 更新输入框为解析后的完整代码
                        document.getElementById("stock-code-input").value = resolvedSymbol;
                        updateRestartBtn();
                        updateDualBtn();
                        // 同步周期
                        const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d','周线':'w'};
                        currentFreq = freqMap[data.meta.freq] || freq;
                        lastFuturesFreq = currentFreq; // 更新期货周期记忆
                        updateFreqButtonStates(true);
                        viewCount = VIEW_COUNT;
                        adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                        viewOffset = Math.max(0, data.klines.length - viewCount);
                        if (data.klines.length < viewCount) viewOffset = 0;
                        document.getElementById("stock-name").textContent = data.meta.name;
                        document.getElementById("stock-code").textContent = data.meta.symbol;
                        document.title = "缠论分析 - " + data.meta.name;
                        if (data.klines.length > 0) {
                            const lastDate = klineDateToInput(data.klines[data.klines.length - 1].date, currentFreq);
                            document.getElementById("goto-date-input").value = lastDate;
                        }
                        updateWeekday();
                        document.getElementById("loading").classList.add("hidden");
                        document.getElementById("goto-date-input").disabled = false;
                        resizeCanvas();
                        render();
                        generateStats();
                    } catch(e) {
                        console.error('初始数据解析失败:', e);
                        document.getElementById("loading").classList.add("hidden");
                        document.getElementById("goto-date-input").disabled = false;
                    }
                });

                // update 事件：增量更新
                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeDataSingle(data);
                    } catch(e) {
                        console.error('实时数据解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    // 立即关闭EventSource，阻止浏览器自带重连
                    realtimeEventSource.close();
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
                };

                realtimeEventSource.onopen = function() {
                    realtimeConnected = true;
                    badge.classList.remove('stopped');
                    badge.textContent = '● 实时';
                };
            } catch(e) {
                console.error('SSE连接失败:', e);
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
                document.getElementById("loading").classList.add("hidden");
            }
        }

        // 期货双窗口SSE连接（独立于 connectRealtimeInit，与股票双窗口解耦）
        // startTime: 上窗选点时间 T（B 操作双窗：上窗 [T, 最新]、下窗自动对齐同一区间）
        // endTime: 复盘终点（软断开；复盘模式下后端忽略 startTime——复盘不加载选点）
        function connectRealtimeDual(symbol, mainFreq, subFreq, endTime, startTime) {
            disconnectRealtime();
            realtimeSymbol = symbol;
            realtimeFreq = mainFreq;
            dualSubFreq = subFreq;
            realtimeStartTime = startTime || null;
            realtimeEndTime = endTime || null; // 复盘软断开边界
            isRealtimeMode = true;
            startCountdownTimer();
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';

            try {
                let sseUrl = '/api/futures/read/stream?symbol=' + encodeURIComponent(symbol)
                    + '&freq=' + mainFreq + '&dual=1&sub_freq=' + subFreq;
                if (startTime) {
                    // B 操作双窗选点：上窗从 T 加载到最新，下窗由后端对齐同一区间
                    sseUrl += '&start_time=' + encodeURIComponent(startTime);
                }
                if (endTime) {
                    sseUrl += '&end_time=' + encodeURIComponent(endTime);
                }
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                realtimeEventSource.addEventListener('init', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.main) {
                            chartData = data.main;
                            const resolvedSymbol = chartData.meta.symbol || symbol;
                            saveHistory(resolvedSymbol, chartData.meta.name);
                            realtimeSymbol = resolvedSymbol;
                            updateRestartBtn();
                            updateDualBtn();
                            const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d','周线':'w'};
                            currentFreq = freqMap[chartData.meta.freq] || currentFreq;
                            lastFuturesFreq = currentFreq;
                            viewCount = VIEW_COUNT;
                            adjustViewForSavedPoint();
                            viewOffset = Math.max(0, chartData.klines.length - viewCount);
                            if (chartData.klines.length < viewCount) { viewOffset = 0; viewCount = chartData.klines.length; }
                            document.getElementById("stock-name").textContent = chartData.meta.name;
                            document.getElementById("stock-code").textContent = chartData.meta.symbol;
                            document.title = "缠论分析 - " + chartData.meta.name;
                            if (chartData.klines.length > 0) {
                                const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                                document.getElementById("goto-date-input").value = lastDate;
                            }
                            updateWeekday();
                        }
                        if (data.sub) {
                            dualSubData = data.sub;
                            // B 操作双窗选点（上窗有选点）：下窗由后端对齐上窗 [选点, 最新] 区间加载，
                            // 视口无 377 限制——下窗后端加载多少根，前端视口就显示多少根
                            // （与股票双窗选点后下窗全显规则一致；A/C 操作仍走 VIEW_COUNT 视口）
                            if (chartData && chartData.meta && chartData.meta.saved_selection_date) {
                                dualSubViewCount = dualSubData.klines.length;
                                dualSubViewOffset = 0;
                            } else {
                                dualSubViewCount = VIEW_COUNT;
                                dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                                if (dualSubData.klines.length < dualSubViewCount) {
                                    dualSubViewOffset = 0;
                                }
                            }
                        }
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        document.getElementById("goto-date-input").disabled = false;
                        updateFreqButtonStates(true);
                        render();
                    } catch (e) {
                        console.error('双窗口init解析失败:', e);
                        document.getElementById("goto-date-input").disabled = false;
                    }
                });

                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeDataDual(data);
                    } catch (e) {
                        console.error('双窗口update解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    // 立即关闭EventSource，阻止浏览器自带重连
                    realtimeEventSource.close();
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
                };

                realtimeEventSource.onopen = function() {
                    realtimeConnected = true;
                    badge.classList.remove('stopped');
                    badge.textContent = '● 实时';
                };
            } catch (e) {
                console.error('双窗口SSE连接失败:', e);
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
                document.getElementById("loading").classList.add("hidden");
            }
        }

        function connectRealtime(symbol, freq, startTime) {
            freq = freq || currentFreq || '1m';
            // 断开旧连接
            disconnectRealtime();
            realtimeSymbol = symbol;
            realtimeFreq = freq;
            realtimeStartTime = startTime || null;
            isRealtimeMode = true;
            startCountdownTimer();
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';

            try {
                let sseUrl = '/api/futures/read/stream?symbol=' + encodeURIComponent(symbol) + '&freq=' + encodeURIComponent(freq);
                if (startTime) {
                    sseUrl += '&start_time=' + encodeURIComponent(startTime);
                }
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                // 只监听 update 事件（重连不处理 init，避免覆盖已有数据）
                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeDataSingle(data);
                    } catch(e) {
                        console.error('实时数据解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    // 立即关闭EventSource，阻止浏览器自带重连
                    realtimeEventSource.close();
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
                };

                realtimeEventSource.onopen = function() {
                    realtimeConnected = true;
                    badge.classList.remove('stopped');
                    badge.textContent = '● 实时';
                };
            } catch(e) {
                console.error('SSE连接失败:', e);
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
            }
        }

        function disconnectRealtime() {
            stopCountdownTimer();
            isRealtimeMode = false;
            realtimeSymbol = null;
            realtimeFreq = null;
            realtimeStartTime = null;
            if (realtimeEventSource) {
                realtimeEventSource.close();
                realtimeEventSource = null;
            }
            realtimeConnected = false;
            const badge = document.getElementById('realtime-badge');
            badge.classList.remove('visible', 'stopped');
        }

        function handleRealtimeDataSingle(data) {
            if (!isRealtimeMode || !data || !data.klines) return;
            // 保存当前开关状态
            const savedShowBi = showBi, savedShowFx = showFx;
            const savedShowZs = showZs, savedShowSeg = showSeg, savedShowBsp = showBsp;

            // 保存用户当前的缩放和位置
            const oldKlinesCount = chartData && chartData.klines ? chartData.klines.length : 0;
            const savedViewCount = viewCount;
            const savedViewOffset = viewOffset;
            const wasAtRightEdge = (savedViewOffset + savedViewCount >= oldKlinesCount);

            // 更新图表数据
            chartData = data;

            // 更新元信息
            document.getElementById('stock-name').textContent = data.meta.name;
            document.getElementById('stock-code').textContent = data.meta.symbol;
            document.title = "缠论分析 - " + data.meta.name;
            if (data.meta.freq) {
                const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d'};
                currentFreq = freqMap[data.meta.freq] || currentFreq;
            }

            // 同步 freq 按钮状态
            updateFreqButtonStates(true);

            // 保持用户缩放不变：如果在最右端，左减一右加一；否则原地不动
            const newKlinesCount = data.klines.length;
            const delta = newKlinesCount - oldKlinesCount;
            viewCount = savedViewCount;
            if (wasAtRightEdge && delta > 0) {
                viewOffset = Math.max(0, savedViewOffset + delta);
            } else {
                viewOffset = savedViewOffset;
            }

            // 重绘
            updateSlider();
            resizeCanvas();
            render();
            updateRestartBtn();
            updateDualBtn();
        }

        function handleRealtimeDataDual(data) {
            if (!isRealtimeMode || !data) return;
            // 保存当前开关状态
            const savedShowBi = showBi, savedShowFx = showFx;
            const savedShowZs = showZs, savedShowSeg = showSeg, savedShowBsp = showBsp;

            if (data.main) {
                // 保存用户当前的缩放和位置
                const oldMainCount = chartData && chartData.klines ? chartData.klines.length : 0;
                const savedViewCount = viewCount;
                const savedViewOffset = viewOffset;
                const wasAtRightEdge = (savedViewOffset + savedViewCount >= oldMainCount);

                chartData = data.main;

                // 更新元信息
                if (data.main.meta) {
                    document.getElementById('stock-name').textContent = data.main.meta.name || '';
                    document.getElementById('stock-code').textContent = data.main.meta.symbol || '';
                    if (data.main.meta.freq) {
                        const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d'};
                        currentFreq = freqMap[data.main.meta.freq] || currentFreq;
                    }
                }

                // 保持用户缩放不变：如果在最右端，左减一右加一
                const newMainCount = data.main.klines ? data.main.klines.length : 0;
                const delta = newMainCount - oldMainCount;
                viewCount = savedViewCount;
                if (wasAtRightEdge && delta > 0) {
                    viewOffset = Math.max(0, savedViewOffset + delta);
                } else {
                    viewOffset = savedViewOffset;
                }
            }
            if (data.sub) {
                // 保存子窗口的缩放和位置
                const oldSubCount = dualSubData && dualSubData.klines ? dualSubData.klines.length : 0;
                const savedSubCount = dualSubViewCount || VIEW_COUNT;
                const savedSubOffset = dualSubViewOffset || 0;
                const wasSubAtRightEdge = (savedSubOffset + savedSubCount >= oldSubCount);

                dualSubData = data.sub;

                const newSubCount = data.sub.klines ? data.sub.klines.length : 0;
                const subDelta = newSubCount - oldSubCount;
                dualSubViewCount = savedSubCount;
                if (wasSubAtRightEdge && subDelta > 0) {
                    dualSubViewOffset = Math.max(0, savedSubOffset + subDelta);
                } else {
                    dualSubViewOffset = savedSubOffset;
                }
            }
            updateSlider();
            render();
        }


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.RealtimeService = {
            startRealtimeIfFutures, connectRealtimeInit, connectRealtimeDual,
        connectRealtime, disconnectRealtime, handleRealtimeDataSingle,
        handleRealtimeDataDual
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] AnnotationPanel —— 文字标注组件（标注 CRUD / 右键菜单 / 弹窗）
        // 对外接口（ChanApp.components.AnnotationPanel）: loadAnnotations, onContextMenu, annotationAdd, annotationDialogConfirm, annotationDialogCancel, annotationEditAnnotation, annotationDeleteAnnotation, annotationDeleteAllGlobal, annotationReplayToHere, toggleMirrorMode, drawAnnotations
// ══════════════════════════════════════════════════════════════════

        // ============================================================
        // 文字标注功能
        // ============================================================

        // 加载标注数据
        function loadAnnotations() {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/stocks/" + encodeURIComponent(code) + "/read/annotation?freq=" + freq)
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    annotations = data.annotations || [];
                    render();
                })
                .catch(function() { annotations = []; });
        }

        // 保存标注到后端
        function saveAnnotationToServer(dateStr, text, yOffset) {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/stocks/" + encodeURIComponent(code) + "/save/annotation", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action: "add",
                    code: code,
                    freq: freq,
                    date: dateStr,
                    text: text,
                    y_offset: yOffset || 0
                })
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.ok) {
                    // 添加到本地缓存
                    annotations.push({ date: dateStr, text: text, y_offset: yOffset || 0 });
                    render();
                }
            })
            .catch(function(err) { console.error("保存标注失败:", err); });
        }

        // 删除标注
        function deleteAnnotationFromServer(dateStr, text) {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/stocks/" + encodeURIComponent(code) + "/save/annotation", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action: "delete",
                    code: code,
                    freq: freq,
                    date: dateStr,
                    text: text
                })
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.ok) {
                    // 先本地移除，立即刷新界面
                    annotations = annotations.filter(function(a) {
                        return !(a.date === dateStr && a.text === text);
                    });
                    render();
                    // 再从服务器重新加载，确保与后端真实状态完全一致
                    loadAnnotations();
                } else {
                    // 后端未找到匹配标注：标注可能存在于其他周期下
                    // 重新加载以同步本地状态，并提示用户
                    console.warn("[标注] 后端未找到匹配标注(code=" + code + ", freq=" + freq + ")，重新加载标注数据");
                    loadAnnotations();
                    alert("未找到该标注，可能标注存在于其他周期下。\n当前周期: " + freq + "\n请切换到添加标注时使用的周期再试。");
                }
            })
            .catch(function(err) { console.error("删除标注失败:", err); });
        }

        // 右键菜单处理
        function onContextMenu(e) {
            e.preventDefault();
            if (!chartData) return;

            // 双窗口模式下，只在上面窗口支持标注
            if (isDualWindow && window._isRenderingBottom) return;

            const rect = canvas.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const clickY = e.clientY - rect.top;

            // 确定点击的K线
            const area = getChartArea();
            const klines = getVisibleKlines();
            if (!klines.length) return;
            const barStep = area.w / (klines.length < viewCount ? klines.length : viewCount);
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            const idx = Math.floor((clickX - area.x + subPixelOffset) / barStep);
            if (idx < 0 || idx >= klines.length) return;
            const k = klines[idx];
            if (!k) return;
            // 检查是否在K线主图区域内
            if (clickY < area.y || clickY > area.y + area.h) return;

            _annotationTargetDate = k.date;
            _annotationTargetX = e.clientX;
            _annotationTargetY = clickY;
            _annotationClickTarget = null;

            // 检测点击是否在某个标注的方框区域内
            const priceRange = getPriceRange(klines);
            const dateToIdx = {};
            for (let i = 0; i < klines.length; i++) { dateToIdx[klines[i].date] = i; }
            for (let i = 0; i < annotations.length; i++) {
                const ann = annotations[i];
                const annIdx = dateToIdx[ann.date];
                if (annIdx === undefined) continue;
                const annK = klines[annIdx];
                const annX = area.x + barStep * annIdx + barStep / 2 - subPixelOffset;
                const annY = ann.y_offset || (priceToY(annK.high, area, priceRange) - 8);
                const layout = getAnnotationLayout(ann, annX, annY, area);
                if (clickX >= layout.boxX && clickX <= layout.boxX + layout.boxW &&
                    clickY >= layout.boxY && clickY <= layout.boxY + layout.boxH) {
                    _annotationClickTarget = ann;
                    break;
                }
            }

            // 显示菜单
            const menu = document.getElementById("annotation-menu");
            const menuDeleteOne = document.getElementById("annotation-menu-delete-one");
            const menuEditOne = document.getElementById("annotation-menu-edit-one");
            const menuAdd = document.getElementById("annotation-menu-add");
            const menuRestart = document.getElementById("annotation-menu-restart");
            const menuReplay = document.getElementById("annotation-menu-replay");
            const menuDivider = document.getElementById("annotation-menu-divider");
            const menuDelAll = document.getElementById("annotation-menu-del-all");
            const menuDivider2 = document.getElementById("annotation-menu-divider2");
            const menuMirror = document.getElementById("annotation-menu-mirror");
            // 更新翻转视图菜单项文字（显示当前状态）
            menuMirror.textContent = _isMirrorMode ? "取消翻转" : "翻转视图";
            if (_annotationClickTarget) {
                menuDeleteOne.style.display = "block";
                menuEditOne.style.display = "block";
                menuAdd.style.display = "none";
                menuRestart.style.display = "none";
                menuReplay.style.display = "none";
                menuDivider.style.display = "none";
                menuDelAll.style.display = "none";
            } else {
                menuDeleteOne.style.display = "none";
                menuEditOne.style.display = "none";
                menuAdd.style.display = "block";
                menuRestart.style.display = _restartEnabled ? "block" : "none";
                menuReplay.style.display = "block";
                menuDivider.style.display = "block";
                menuDelAll.style.display = "block";
            }
            // 翻转视图始终显示（与标注操作无关，是全局视图模式）
            menuDivider2.style.display = "block";
            menuMirror.style.display = "block";

            menu.style.left = e.clientX + "px";
            menu.style.top = e.clientY + "px";
            menu.classList.add("show");
        }

        // 添加标注
        window.annotationAdd = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            _annotationDialogMode = "add";
            document.getElementById("annotation-dialog-title").textContent = "添加文字标注";
            document.getElementById("annotation-dialog-date").textContent = "K线日期: " + _annotationTargetDate;
            document.getElementById("annotation-dialog-input").value = "";
            document.getElementById("annotation-dialog").classList.add("show");
            setTimeout(function() {
                document.getElementById("annotation-dialog-input").focus();
            }, 100);
        };

        // 复盘到右键点击的K线日期（等价于在复盘日期输入框中输入该日期）
        window.annotationReplayToHere = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!_annotationTargetDate) return;
            var input = document.getElementById("goto-date-input");
            var dateStr;
            if (isIntradayFreq(currentFreq)) {
                // 日内周期：保留完整时间，转成 datetime-local 格式
                dateStr = klineDateToInput(_annotationTargetDate, currentFreq);
            } else {
                // 日K/周K：只取日期部分
                dateStr = _annotationTargetDate.slice(0, 10).replace(/\//g, "-");
                if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
                    alert("无法识别该K线日期: " + _annotationTargetDate);
                    return;
                }
            }
            // 若与当前日期相同，仍强制重新复盘（避免用户改了其他条件后无响应）
            input.value = dateStr;
            if (typeof updateWeekday === "function") updateWeekday();
            _dateKeyEnter = true;  // 复用keyEnter标志，gotoDate中跳过isToday安全网，始终传end_date
            gotoDate();
        };

        // 切换翻转视图模式（K线涨跌互换、MACD红绿互换、缠论结构镜像）
        // 保底策略：如果反图渲染出错，自动切回正图并从后端重新加载，确保正图永远正确
        window.toggleMirrorMode = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            var prevMode = _isMirrorMode;
            _isMirrorMode = !_isMirrorMode;
            try {
                render();
            } catch(e) {
                console.error("[翻转视图] 渲染出错，自动恢复正图:", e);
                _isMirrorMode = false;
                // chartData 可能被镜像数据污染（_renderChart 中途异常未恢复），
                // 从后端重新加载（命中缓存仅 0.001s），彻底恢复正图
                try {
                    loadStock();
                } catch(e2) {
                    console.error("[翻转视图] 恢复失败:", e2);
                }
            }
        };

        // 删除右键点击命中的标注
        window.annotationDeleteAnnotation = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!_annotationClickTarget) return;
            deleteAnnotationFromServer(_annotationClickTarget.date, _annotationClickTarget.text);
        };

        // 修改右键点击命中的标注
        window.annotationEditAnnotation = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!_annotationClickTarget) return;
            _annotationDialogMode = "edit";
            _annotationEditOldText = _annotationClickTarget.text;
            _annotationTargetDate = _annotationClickTarget.date;
            _annotationTargetY = _annotationClickTarget.y_offset || 0;
            document.getElementById("annotation-dialog-title").textContent = "修改文字标注";
            document.getElementById("annotation-dialog-date").textContent = "K线日期: " + _annotationClickTarget.date;
            document.getElementById("annotation-dialog-input").value = _annotationClickTarget.text;
            document.getElementById("annotation-dialog").classList.add("show");
            setTimeout(function() {
                var inp = document.getElementById("annotation-dialog-input");
                inp.focus();
                inp.setSelectionRange(inp.value.length, inp.value.length);
            }, 100);
        };

        // 删除当前股票/周期全部标注
        window.annotationDeleteAllGlobal = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            if (confirm("确定删除当前股票 (" + code + ") " + freq + " 周期下的全部标注吗？")) {
                fetch("/api/stocks/" + encodeURIComponent(code) + "/save/annotation", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        action: "delete_all",
                        code: code,
                        freq: freq
                    })
                })
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.ok) {
                        annotations = [];
                        render();
                    }
                })
                .catch(function(err) { console.error("删除全部标注失败:", err); });
            }
        };

        // 标注对话框键盘事件
        window.annotationDialogKeydown = function(e) {
            if (e.key === "Enter") {
                annotationDialogConfirm();
            } else if (e.key === "Escape") {
                annotationDialogCancel();
            }
        };

        // 标注对话框确认
        window.annotationDialogConfirm = function() {
            const text = document.getElementById("annotation-dialog-input").value.trim();
            if (!text) {
                alert("请输入标注文字");
                return;
            }
            document.getElementById("annotation-dialog").classList.remove("show");
            if (_annotationDialogMode === "edit" && _annotationEditOldText) {
                updateAnnotationOnServer(_annotationTargetDate, _annotationEditOldText, text, _annotationTargetY);
            } else {
                saveAnnotationToServer(_annotationTargetDate, text, _annotationTargetY);
            }
        };

        // 更新标注（修改模式：删除旧标注+添加新标注）
        function updateAnnotationOnServer(dateStr, oldText, newText, yOffset) {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/stocks/" + encodeURIComponent(code) + "/save/annotation", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action: "update",
                    code: code,
                    freq: freq,
                    date: dateStr,
                    old_text: oldText,
                    text: newText,
                    y_offset: yOffset || 0
                })
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.ok) {
                    annotations = annotations.filter(function(a) {
                        return !(a.date === dateStr && a.text === oldText);
                    });
                    annotations.push({ date: dateStr, text: newText, y_offset: yOffset || 0 });
                    render();
                }
            })
            .catch(function(err) { console.error("更新标注失败:", err); });
        }

        // 标注对话框取消
        window.annotationDialogCancel = function() {
            document.getElementById("annotation-dialog").classList.remove("show");
        };

        // 计算标注框的布局信息（供绘制和命中检测共用）
        function getAnnotationLayout(ann, klineX, klineY, area) {
            const font = "bold 12px 'PingFang SC', 'Microsoft YaHei', sans-serif";
            ctx.font = font;
            const lineHeight = 16;
            const padX = 6, padY = 3;
            const maxCharsPerLine = 11;

            // 按每行最多11个字折行
            const lines = [];
            let remaining = ann.text;
            while (remaining.length > 0) {
                lines.push(remaining.substring(0, maxCharsPerLine));
                remaining = remaining.substring(maxCharsPerLine);
            }

            // 计算每行宽度，取最大
            let maxTextW = 0;
            const lineWidths = lines.map(function(line) {
                const w = ctx.measureText(line).width;
                if (w > maxTextW) maxTextW = w;
                return w;
            });

            const boxW = maxTextW + padX * 2;
            const boxH = lines.length * lineHeight + padY * 2;
            const boxY = klineY - boxH; // 框底对齐klineY

            // 居中：以K线X为中心
            let boxX = klineX - boxW / 2;

            // 边界修正：不超出视口
            if (boxX < area.x) {
                boxX = area.x;
            }
            if (boxX + boxW > area.x + area.w) {
                boxX = area.x + area.w - boxW;
            }

            return { lines: lines, lineWidths: lineWidths, maxTextW: maxTextW,
                     boxW: boxW, boxH: boxH, boxX: boxX, boxY: boxY,
                     lineHeight: lineHeight, padX: padX, padY: padY };
        }

        // 绘制标注文字
        function drawAnnotations(klines, area, priceRange, barStep, subPixelOffset) {
            if (!annotations || !annotations.length) return;
            const dateToIdx = {};
            for (let i = 0; i < klines.length; i++) {
                dateToIdx[klines[i].date] = i;
            }

            annotations.forEach(function(ann) {
                const idx = dateToIdx[ann.date];
                if (idx === undefined) return;
                const k = klines[idx];
                const kx = area.x + barStep * idx + barStep / 2 - subPixelOffset;
                const ky = ann.y_offset || (priceToY(k.high, area, priceRange) - 8);

                const layout = getAnnotationLayout(ann, kx, ky, area);

                // 绘制每行文字（无背景框，文字左对齐，白色加阴影）
                ctx.fillStyle = "#ffffff";
                ctx.textAlign = "left";
                ctx.textBaseline = "middle";
                ctx.font = "bold 12px 'PingFang SC', 'Microsoft YaHei', sans-serif";
                ctx.shadowColor = "rgba(0, 0, 0, 0.85)";
                ctx.shadowBlur = 3;
                for (let li = 0; li < layout.lines.length; li++) {
                    const lineX = layout.boxX + layout.padX;
                    const lineY = layout.boxY + layout.padY + layout.lineHeight * li + layout.lineHeight / 2;
                    ctx.fillText(layout.lines[li], lineX, lineY);
                }
                ctx.shadowColor = "transparent";
                ctx.shadowBlur = 0;
            });
            ctx.textBaseline = "alphabetic"; // 恢复默认基线
        }


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.AnnotationPanel = {
            loadAnnotations, onContextMenu, annotationAdd,
        annotationDialogConfirm, annotationDialogCancel, annotationEditAnnotation,
        annotationDeleteAnnotation, annotationDeleteAllGlobal,
        annotationReplayToHere, toggleMirrorMode, drawAnnotations
        };

// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] Bootstrap —— 应用引导（首屏 init / 状态持久化 / 全局监听注册）
        // 对外接口（ChanApp.components.Bootstrap）: init, initDefault, saveLastState, loadLastCodeFreq
// ══════════════════════════════════════════════════════════════════

        // 保存当前状态到 localStorage（仅股票，仅单窗口非复盘模式）
        function saveLastState() {
            if (!chartData || !chartData.meta) return;
            if (isDualWindow) return;  // 双窗口不保存
            if (chartData.meta.is_replay) return;  // 复盘模式不保存
            if (chartData.meta.market === 'futures') return;  // 期货不保存
            const state = {
                code: chartData.meta.symbol,
                freq: currentFreq,
                name: chartData.meta.name
            };
            try { localStorage.setItem('lastCodeFreq', JSON.stringify(state)); } catch(e) {}
        }

        // 从 localStorage 加载上次状态，仅股票有效
        function loadLastCodeFreq() {
            try {
                const raw = localStorage.getItem('lastCodeFreq');
                if (!raw) return null;
                const state = JSON.parse(raw);
                if (!state.code || !state.freq) return null;
                if (isFuturesCode(state.code)) return null;  // 排除期货残留
                return state;
            } catch(e) { return null; }
        }

        async function init() {
            try {
                // 先尝试从 localStorage 恢复上次状态
                const savedState = loadLastCodeFreq();
                if (savedState) {
                    // 有保存的股票状态，先置空，立即异步加载
                    chartData = null;
                    document.getElementById("stock-code-input").value = savedState.code;
                    // 设置初始周期
                    if (savedState.freq) {
                        currentFreq = savedState.freq;
                        lastStockFreq = savedState.freq;
                    }
                    initCanvas();
                    updateSlider();
                    updateFreqButtonStates(false);
                    updateRestartBtn();
                    updateDualBtn();
                    // 异步加载保存的股票数据
                    document.getElementById("loading").classList.remove("hidden");
                    fetch("/api/stocks/" + encodeURIComponent(savedState.code) + "/analyze?freq=" + savedState.freq, { cache: "no-store" })
                        .then(resp => {
                            if (!resp.ok) throw new Error("恢复失败");
                            return resp.json();
                        })
                        .then(data => {
                            // 防御：检查 API 返回数据是否完整
                            if (!data || !data.meta) {
                                const errMsg = data && data.error ? data.error : "API 返回数据缺少 meta 字段";
                                throw new Error("恢复失败: " + errMsg);
                            }
                            chartData = data;
                            saveHistory(savedState.code, data.meta.name);
                            document.getElementById("stock-name").textContent = chartData.meta.name;
                            document.getElementById("stock-code").textContent = chartData.meta.symbol;
                            document.title = "缠论分析 - " + chartData.meta.name;
                            let returnedFreq;
                            if (data.meta.freq === "5分钟") returnedFreq = "5m";
                            else if (data.meta.freq === "30分钟") returnedFreq = "30m";
                            else if (data.meta.freq === "周线") returnedFreq = "w";
                            else returnedFreq = "d";
                            currentFreq = returnedFreq;
                            lastStockFreq = currentFreq;
                            updateDateInputType();
                            updateFreqButtonStates(false);
                            viewCount = VIEW_COUNT;
                            adjustViewForSavedPoint();
                            viewOffset = Math.max(0, chartData.klines.length - viewCount);
                            if (chartData.klines.length < viewCount) viewOffset = 0;
                            applyOverlayButtonStates();
                            initialized = true;
                            updateRestartBtn();
                            updateDualBtn();
                            const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                            document.getElementById("goto-date-input").value = lastDate;
                            updateWeekday();
                            render();
                            document.getElementById("loading").classList.add("hidden");
                            document.getElementById("error").classList.add("hidden");
                            generateStats();
                            loadAnnotations();
                            // 断开期货SSE（如果有）
                            disconnectRealtime();
                        })
                        .catch(err => {
                            console.error("恢复上次状态失败，回退到默认:", err);
                            try {
                                let errMsgEl2 = document.getElementById("error-msg");
                                if (errMsgEl2) {
                                    errMsgEl2.innerHTML = "恢复错误: " + (err && err.message ? String(err.message).replace(/</g, "&lt;") : String(err))
                                        + "<br><br><span style='font-size:12px;color:#aaa'>name=" + (err && err.name) + "<br>stack=" + (err && err.stack ? err.stack.replace(/</g, "&lt;") : "无") + "</span>";
                                }
                            } catch (e3) {}
                            // 回退到默认上证指数
                            document.getElementById("stock-code-input").value = "";
                            initDefault();
                        });
                    return;
                }
                // 无保存状态，默认加载上证指数
                initDefault();
            } catch (err) {
                console.error("初始化失败:", err);
                document.getElementById("loading").classList.add("hidden");
                document.getElementById("error").classList.remove("hidden");
            }
        }

        async function initDefault() {
            document.getElementById("loading").classList.remove("hidden");
            try {
                const resp = await fetch("/api/stocks/" + encodeURIComponent("sh000001") + "/analyze?freq=d", { cache: "no-store" });
                if (!resp.ok) throw new Error("默认加载失败");
                const data = await resp.json();
                // 防御：检查 API 返回数据是否完整（缺少 meta 时后续 chartData.meta.symbol 会崩溃）
                if (!data || !data.meta) {
                    const errMsg = data && data.error ? data.error : "API 返回数据缺少 meta 字段";
                    throw new Error("首屏数据加载失败: " + errMsg);
                }
                chartData = data;
                document.getElementById("stock-name").textContent = chartData.meta.name;
                document.getElementById("stock-code").textContent = chartData.meta.symbol;
                document.title = "缠论分析 - " + chartData.meta.name;
                initCanvas();
                updateSlider();
                if (chartData.meta.freq === "5分钟") currentFreq = "5m";
                else if (chartData.meta.freq === "30分钟") currentFreq = "30m";
                else if (chartData.meta.freq === "周线") currentFreq = "w";
                else currentFreq = "d";
                updateDateInputType();
                lastStockFreq = currentFreq;
                updateFreqButtonStates(false);
                viewCount = VIEW_COUNT;
                adjustViewForSavedPoint();
                applyOverlayButtonStates();
                viewOffset = Math.max(0, chartData.klines.length - viewCount);
                if (chartData.klines.length < viewCount) viewOffset = 0;
                initialized = true;
                updateRestartBtn();
                updateDualBtn();
                const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                document.getElementById("goto-date-input").value = lastDate;
                updateWeekday();
                render();
                document.getElementById("loading").classList.add("hidden");
                document.getElementById("error").classList.add("hidden");
                generateStats();
                loadAnnotations();
            } catch (err) {
                console.error("initDefault 失败:", err);
                // 把错误详情显示到页面上，方便用户直接查看（无需F12）
                try {
                    let errMsgEl = document.getElementById("error-msg");
                    if (errMsgEl) {
                        errMsgEl.innerHTML = "错误: " + (err && err.message ? String(err.message).replace(/</g, "&lt;") : String(err))
                            + "<br><br><span style='font-size:12px;color:#aaa'>name=" + (err && err.name) + "<br>stack=" + (err && err.stack ? err.stack.replace(/</g, "&lt;") : "无") + "</span>";
                    }
                } catch (e2) {}
                document.getElementById("loading").classList.add("hidden");
                document.getElementById("error").classList.remove("hidden");
            }
        }


        // 注册组件对外接口（引用上方闭包内实现）
        ChanApp.components.Bootstrap = {
            init, initDefault, saveLastState, loadLastCodeFreq
        };


// ══════════════════════════════════════════════════════════════════
        // [MERGED] AppState 状态访问层
        // 30 个共享状态变量的 getter/setter 访问器 + 8 个引导方法别名。
        // 闭包变量仍为唯一数据源（访问器同源读写，行为零漂移）；本层不进
        // components 注册表、不新增任何 window.* 绑定（window API 面冻结），
        // 仅供控制台调试（ChanApp.state.<变量>）。
// ══════════════════════════════════════════════════════════════════
        ChanApp.state = (function() {
            const s = {};
            Object.defineProperties(s, {
                chartData: { get: function(){ return chartData; }, set: function(v){ chartData = v; } },
                showBi: { get: function(){ return showBi; }, set: function(v){ showBi = v; } },
                showFx: { get: function(){ return showFx; }, set: function(v){ showFx = v; } },
                showZs: { get: function(){ return showZs; }, set: function(v){ showZs = v; } },
                showSeg: { get: function(){ return showSeg; }, set: function(v){ showSeg = v; } },
                showBsp: { get: function(){ return showBsp; }, set: function(v){ showBsp = v; } },
                showBiIdx: { get: function(){ return showBiIdx; }, set: function(v){ showBiIdx = v; } },
                bspFilter: { get: function(){ return bspFilter; }, set: function(v){ bspFilter = v; } },
                maPeriods: { get: function(){ return maPeriods; }, set: function(v){ maPeriods = v; } },
                _logScale: { get: function(){ return _logScale; }, set: function(v){ _logScale = v; } },
                _showVolume: { get: function(){ return _showVolume; }, set: function(v){ _showVolume = v; } },
                _subShowVolume: { get: function(){ return _subShowVolume; }, set: function(v){ _subShowVolume = v; } },
                currentFreq: { get: function(){ return currentFreq; }, set: function(v){ currentFreq = v; } },
                lastStockFreq: { get: function(){ return lastStockFreq; }, set: function(v){ lastStockFreq = v; } },
                lastFuturesFreq: { get: function(){ return lastFuturesFreq; }, set: function(v){ lastFuturesFreq = v; } },
                isDualWindow: { get: function(){ return isDualWindow; }, set: function(v){ isDualWindow = v; } },
                dualSubData: { get: function(){ return dualSubData; }, set: function(v){ dualSubData = v; } },
                dualSubFreq: { get: function(){ return dualSubFreq; }, set: function(v){ dualSubFreq = v; } },
                viewOffset: { get: function(){ return viewOffset; }, set: function(v){ viewOffset = v; } },
                viewCount: { get: function(){ return viewCount; }, set: function(v){ viewCount = v; } },
                isRealtimeMode: { get: function(){ return isRealtimeMode; }, set: function(v){ isRealtimeMode = v; } },
                realtimeSymbol: { get: function(){ return realtimeSymbol; }, set: function(v){ realtimeSymbol = v; } },
                realtimeFreq: { get: function(){ return realtimeFreq; }, set: function(v){ realtimeFreq = v; } },
                realtimeStartTime: { get: function(){ return realtimeStartTime; }, set: function(v){ realtimeStartTime = v; } },
                realtimeConnected: { get: function(){ return realtimeConnected; }, set: function(v){ realtimeConnected = v; } },
                annotations: { get: function(){ return annotations; }, set: function(v){ annotations = v; } },
                initialized: { get: function(){ return initialized; }, set: function(v){ initialized = v; } },
                _isMirrorMode: { get: function(){ return _isMirrorMode; }, set: function(v){ _isMirrorMode = v; } },
                activeDualWindow: { get: function(){ return activeDualWindow; }, set: function(v){ activeDualWindow = v; } },
                _ctrlPressed: { get: function(){ return _ctrlPressed; }, set: function(v){ _ctrlPressed = v; } },
            });
            s.loadOverlaySettings = loadOverlaySettings;
            s.saveOverlaySettings = saveOverlaySettings;
            s.applyOverlayButtonStates = applyOverlayButtonStates;
            s.getShowMa = getShowMa;
            s.saveLastState = saveLastState;
            s.loadLastCodeFreq = loadLastCodeFreq;
            s.init = init;
            s.initDefault = initDefault;
            return s;
        })();


// ══════════════════════════════════════════════════════════════════
        // [EXEC] 引导执行序列 —— 顶层执行语句保持原文件相对顺序
        //（事件监听注册顺序影响同名事件派发序，禁止重排）
// ══════════════════════════════════════════════════════════════════

        // 启动时加载保存的设置
        loadOverlaySettings();

        initCoordSystemRadio();

        // Esc键取消区间选择 / 关闭设置抽屉
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                var drawer = document.getElementById("bsp-filter-dialog");
                if (drawer.classList.contains("show")) {
                    closeBspSettings();
                    return;
                }
                if (_rangeSelect.mode === 'SELECTED_A') {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("区间选择已取消");
                    render();
                }
            }
        });

        document.addEventListener('keyup', function(e) {
            // 兜底：松开Ctrl时清除红框（onMouseMove用e.ctrlKey是主要检测路径）
            if (e.key === 'Control' && isDualWindow) {
                _ctrlPressed = false;
                dualRedRange = null;
                dualShowNewZs = false;
                dualNewZsData = null;
                renderTop();
            }
        });


        document.addEventListener("click", function(e) {
            if (!e.target.closest(".stock-input")) {
                document.getElementById("stock-history").classList.remove("show");
            }
        });

        // 页面关闭时清理 SSE
        window.addEventListener('beforeunload', function() {
            disconnectRealtime();
        });

        (function() {
            const slider = document.getElementById("range-slider");
            const track = document.getElementById("slider-track");
            const win = document.getElementById("slider-window");
            const handleLeft = document.getElementById("slider-handle-left");
            const handleRight = document.getElementById("slider-handle-right");
            let sliderDragging = false;
            let dragType = null;
            let dragStartX = 0, dragStartOffset = 0, dragStartCount = 0, dragStartRightEdge = 0;

            // 获取当前激活窗口的 data
            function getActiveData() {
                if (isDualWindow && activeDualWindow === 'sub' && dualSubData) {
                    return dualSubData;
                }
                return chartData;
            }
            // 获取当前激活窗口的 viewOffset
            function getActiveViewOffset() {
                if (isDualWindow && activeDualWindow === 'sub') {
                    return dualSubViewOffset;
                }
                return viewOffset;
            }
            // 设置当前激活窗口的 viewOffset
            function setActiveViewOffset(v) {
                if (isDualWindow && activeDualWindow === 'sub') {
                    dualSubViewOffset = v;
                } else {
                    viewOffset = v;
                }
            }
            // 获取当前激活窗口的 viewCount
            function getActiveViewCount() {
                if (isDualWindow && activeDualWindow === 'sub') {
                    return dualSubViewCount;
                }
                return viewCount;
            }
            // 设置当前激活窗口的 viewCount
            function setActiveViewCount(v) {
                if (isDualWindow && activeDualWindow === 'sub') {
                    dualSubViewCount = v;
                } else {
                    viewCount = v;
                }
            }
            // 渲染当前激活窗口
            function renderActive() {
                updateActiveWindowClass();
                if (isDualWindow && activeDualWindow === 'sub') {
                    // 直接渲染下面窗口，跳过 updateDualNewZs() 避免滑块操作时误清除红框新中枢
                    if (!dualSubData || !subCtx) return;
                    const _savedCanvas = canvas, _savedCtx = ctx;
                    canvas = subCanvas; ctx = subCtx;
                    window._isRenderingBottom = true;
                    _renderChart(dualSubData, dualSubFreq, dualSubViewOffset, dualSubViewCount,
                        dualSubMouseX, dualSubMouseY, dualHighlightRange, dualRedRange);
                    window._isRenderingBottom = false;
                    canvas = _savedCanvas; ctx = _savedCtx;
                } else if (isDualWindow) {
                    renderTop();
                } else {
                    render();
                }
            }

            function getSliderInfo() {
                const data = getActiveData();
                const totalKlines = data ? data.klines.length : 1;
                const trackWidth = track.clientWidth;
                return { totalKlines, trackWidth };
            }

            handleLeft.addEventListener("mousedown", function(e) {
                e.preventDefault(); e.stopPropagation();
                sliderDragging = true; dragType = "left";
                dragStartX = e.clientX; dragStartCount = getActiveViewCount();
                dragStartRightEdge = getActiveViewOffset() + getActiveViewCount();
            });
            handleRight.addEventListener("mousedown", function(e) {
                e.preventDefault(); e.stopPropagation();
                sliderDragging = true; dragType = "right";
                dragStartX = e.clientX; dragStartCount = getActiveViewCount();
                dragStartOffset = getActiveViewOffset();
            });
            win.addEventListener("mousedown", function(e) {
                e.preventDefault(); e.stopPropagation();
                sliderDragging = true; dragType = "window";
                dragStartX = e.clientX; dragStartOffset = getActiveViewOffset();
            });
            track.addEventListener("mousedown", function(e) {
                const data = getActiveData();
                if (!data) return;
                e.preventDefault();
                const rect = track.getBoundingClientRect();
                const ratio = (e.clientX - rect.left) / rect.width;
                const totalKlines = data.klines.length;
                const vc = getActiveViewCount();
                const newOffset = ratio * totalKlines - vc / 2;
                setActiveViewOffset(Math.max(0, Math.min(totalKlines - vc, newOffset)));
                renderActive();
            });

            document.addEventListener("mousemove", function(e) {
                if (!sliderDragging || !getActiveData()) return;
                const { totalKlines, trackWidth } = getSliderInfo();
                if (trackWidth <= 0) return;
                const dx = e.clientX - dragStartX;
                const dk = (dx / trackWidth) * totalKlines;

                let vc = getActiveViewCount();
                let vo = getActiveViewOffset();
                if (dragType === "left") {
                    const newCount = Math.round(Math.max(3, Math.min(totalKlines, dragStartCount - dk)));
                    vc = newCount;
                    vo = Math.max(0, Math.round(dragStartRightEdge - vc));
                } else if (dragType === "right") {
                    const newCount = Math.round(Math.max(3, Math.min(totalKlines, dragStartCount + dk)));
                    const maxOffset = totalKlines - newCount;
                    vc = newCount;
                    vo = Math.min(vo, Math.max(0, maxOffset));
                } else if (dragType === "window") {
                    const newOffset = dragStartOffset + dk;
                    const maxOffset = totalKlines - vc;
                    vo = Math.max(0, Math.min(newOffset, maxOffset));
                }
                vc = Math.round(vc);
                vo = Math.round(vo);
                setActiveViewCount(vc);
                setActiveViewOffset(vo);
                renderActive();
            });

            document.addEventListener("mouseup", function() {
                sliderDragging = false; dragType = null;
            });
        })();

        // 关闭右键菜单（点击其他地方）
        document.addEventListener("click", function(e) {
            const menu = document.getElementById("annotation-menu");
            if (!menu.contains(e.target)) {
                menu.classList.remove("show");
            }
        });

        init();

        // 周期映射以后端为单一事实源：启动时从 /api/health 拉取周期映射，覆盖本地兜底常量
        (async function loadFreqMapFromBackend() {
            try {
                const resp = await fetch("/api/health", { cache: "no-store" });
                if (!resp.ok) return;
                const data = await resp.json();
                if (data && data.freq_sec_map && Object.keys(data.freq_sec_map).length > 0) {
                    FREQ_SEC_MAP_JS = data.freq_sec_map;
                }
                // 前端视口默认K线根数：优先用后端配置 VIEW_COUNT（校验为正整数，否则保留默认）
                if (data && data.config && typeof data.config.view_count === 'number'
                    && data.config.view_count > 0) {
                    VIEW_COUNT = data.config.view_count;
                }
            } catch (e) { /* 离线兜底：保留本地常量 */ }
        })();

        // 关闭/刷新页面时保存状态（仅股票，期货不保存）
        window.addEventListener('beforeunload', function() { saveLastState(); });


        // 组件注册表暴露至全局（控制台调试入口；内部仍走闭包）
        window.ChanApp = ChanApp;

    })();
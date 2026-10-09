    (function() {
        "use strict";

        const ChanApp = {
            version: "6.0",
            phase: 6
        };

// ══════════════════════════════════════════════════════════════════
        // [STATE] SharedState —— 组件共享状态（声明顺序与原文件一致，
        // 保证初始化语义零漂移；各状态按主要使用方就近注释归属）
// ══════════════════════════════════════════════════════════════════

        let chartData = null, canvas, ctx;

        // ══════════════════════════════════════════════════════════════════
        // [N1 修复] 图表请求序号守卫 —— 防"后到响应覆盖先到"的图表闪回
        // （指导书 v1.3 附录 N1 / 维度 3.3：竞态在前端共享变量，后端加锁无效）
        // 机制：每次用户发起会改图表的操作（查询/切代码/切周期/复盘跳转/
        // 重置/开关双窗/恢复加载）序号 +1；请求发出前捕获序号，响应回来时
        // 若序号已过期（期间用户又发了新操作）则丢弃该响应——不写 chartData、
        // 不渲染、不更新 UI。对后端缓存命中路径同样生效：响应即时返回时
        // 序号不会过期，只有确实被更新的操作抢先才丢弃。
        // ══════════════════════════════════════════════════════════════════
        let _chartActionSeq = 0;
        function _bumpChartActionSeq() { _chartActionSeq += 1; return _chartActionSeq; }
        function _isChartActionStale(seq) { return seq !== _chartActionSeq; }

        let showBi = true, showFx = false, showZs = true, showSeg = false, showBsp = true, showBiIdx = false;

        // BSP买卖点类型过滤：默认全部显示（0,1,2,3 对应 bs_type 配置）
        let bspFilter = { '0': true, '1': true, '2': true, '3': true };

        // 均线周期：选中的周期集合，默认空（不显示均线）
        const MA_PERIODS = [5, 13, 21, 34, 55, 89, 144, 233];

        const MA_COLORS = { 5:'#FFFFFF', 13:'#FCBF49', 21:'#F77F00', 34:'#90BE6D', 55:'#22D3EE', 89:'#3B82F6', 144:'#A8A8A8', 233:'#8822DD' };

        let maPeriods = {};  // {5: true, 13: true, ...}

        let _logScale = false; // 坐标系模式：false=普通坐标系+等差网格, true=对数坐标系+等比网格

        // 底部指标区槽位（单窗 2 槽 / 双窗 1 槽）；元素为 BOTTOM_INDICATORS 的 id。
        // 用数组而不是单个数：数组天然就是"多槽位"的输入，槽数只决定取前几个 ——
        // 单窗配好的 ['macd','rsi']，切双窗时只渲染第 0 个，切回单窗自动恢复第 2 槽。
        // 槽位次序固定：[0] 在上、[1] 在下（不做拖拽调序）。两槽允许选同一指标（不做互斥判断）。
        let _bottomSlots = ['macd', 'rsi'];   // 上窗 / 单窗

        let _subBottomSlots = ['macd'];       // 双窗口下窗（独立，不与上窗联动）

        // 成交额/量的显示模式：'bar'=柱状图（默认，与既有行为一致）；
        // 'macd'=类MACD（把成交额/量代入传统 MACD(12,26,9)，替代收盘价）。
        // 由右上角「显示设置」抽屉的单选项切换；只作用于「选中了 vol 的那个槽」，
        // 与同屏其他槽（macd / rsi）无关。
        let _volDisplayMode = 'bar';

        // 频率→秒数映射（后端单一事实源 /api/health 下发，本地常量仅作离线兜底）
        let FREQ_SEC_MAP_JS = { 'w': 604800, 'd': 86400, '30m': 1800, '15m': 900, '5m': 300, '1m': 60, '15s': 15 };

        // 前端视口默认显示的K线根数（所有周期相同）——后端经 /api/health 下发
        // （config.view_count，见 App/AppConfig.py 的 VIEW_COUNT），此默认值仅作离线兜底。
        // P2 对齐：与后端 app_config.view_count 默认 233 保持一致（原 377 与后端不同源）。
        let VIEW_COUNT = 233;

        const PADDING = { top: 20, right: 22, bottom: 36, left: 10 };

        const VOL_RATIO = 0.2, GAP = 12;

        const MACD_TEXT_HEIGHT = 14;

        // ══ 底部指标区槽位（单窗 2 槽 / 双窗 1 槽）══════════════════════════
        // 候选指标注册表：每个指标四个钩子，渲染层按 id 分派、不再写 if/else 链。
        //   range(klines)         → 值域 {min, max}
        //   draw(area, range, c)  → 绘制（c = 渲染上下文，见 _renderChart 的 bottomCtx）
        //   axis(area, range)     → 纵轴刻度
        //   label(textArea, c)    → 标签行正文（chip 由渲染层统一画）
        // 加一个指标 = 加一条记录 + 三个函数，几何与分派都不用再动。
        // chip（标签行**最右**那个可点的指标名）用短名：成交额 / 成交量 ——
        // 数值区前缀仍走既有 getVolLabel()（含 "(手)"），不动它的既有口径。
        const BOTTOM_INDICATORS = {
            vol: {
                id: 'vol',
                tabLabel: () => (isFuturesMode() ? '成交量' : '成交额'),
                range: (klines) => (_volDisplayMode === 'macd' ? getVolumeMacdRange(klines) : getVolumeRange(klines)),
                draw: (area, range, c) => {
                    if (_volDisplayMode === 'macd') {
                        drawVolumeMacd(c.klines, area, range, c.barStep, c.macdBarWidth, c.subPixelOffset);
                    } else {
                        drawVolume(c.klines, area, range, c.barStep, c.barWidth, c.subPixelOffset);
                    }
                },
                axis: (area, range) => {
                    if (_volDisplayMode === 'macd') drawVolMacdAxis(area, range);
                    else drawVolumeAxis(area, range);
                },
                label: (textArea, c) => drawVolSlotLabel(textArea, c),
            },
            macd: {
                id: 'macd',
                tabLabel: () => 'MACD',
                range: (klines) => getMacdRange(klines),
                draw: (area, range, c) => drawMacd(c.klines, area, range, c.barStep, c.macdBarWidth, c.subPixelOffset),
                axis: (area, range) => drawMacdAxis(area, range),
                label: (textArea, c) => drawMacdSlotLabel(textArea, c),
            },
            rsi: {
                id: 'rsi',
                tabLabel: () => 'RSI',   // chip 用短名（参数写在标签行里，与 MACD 槽同构）
                range: (klines) => getRsiRange(klines),
                draw: (area, range, c) => drawRsi(c.klines, area, range, c.barStep, c.subPixelOffset),
                axis: (area, range) => drawRsiAxis(area, range),
                label: (textArea, c) => drawRsiSlotLabel(textArea, c),
            },
        };

        const BOTTOM_ORDER = ['vol', 'macd', 'rsi'];   // chip 点击的循环顺序

        // 槽数随窗口模式：单窗 2 槽、双窗 1 槽（双窗每窗高度已砍半，放不下第 2 槽）。
        const SLOT_COUNT = () => (isDualWindow ? 1 : 2);

        const SLOT_GAP = 0;                            // 槽间像素（0 = 靠 14px 标签行做视觉分隔）

        const CHIP_PAD_X = 6;                          // chip 内左右留白
        const CHIP_H = MACD_TEXT_HEIGHT;               // chip 高度（与标签行同高）
        const RSI_REF_VALUES = [50, 80, 20];           // RSI 参考线：中轴 50、超买 80、超卖 20
        const RSI_REF_DASH = [1, 3];                   // 三条线统一「细点虚线」：1px 点 + 3px 空隙

        // 双窗口模式逐窗布局参数（fix#1+#2）：
        // 双窗时每个 canvas 高度被砍半，若沿用单窗的 PADDING/GAP 绝对值与 0.2 占比，
        // 固定开销占比放大、MACD 带被严重压扁，波峰波谷难以分辨。
        // 因此双窗时：压缩冗余上下留白与间隔、并把指标区(MACD)占比从 0.2 提到 0.3。
        // 单窗完整沿用原始 PADDING/GAP/VOL_RATIO，避免回归。
        const DUAL_LAYOUT = { top: 10, bottom: 30, gap: 4, volRatio: 0.30 };

        function getLayoutParams() {
            return isDualWindow
                ? DUAL_LAYOUT
                : { top: PADDING.top, bottom: PADDING.bottom, gap: GAP, volRatio: volRatioFor(SLOT_COUNT()) };
        }

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

        let currentFreq = 'd';       // 当前周期: d=日K, 30m=30分钟

        let lastStockFreq = 'd';     // 股票上下文上次使用的周期（同类切换继承）

        // 2026-10-02：默认由 '5m' 改为 '1m'（产品行为变更，此前无单独评审记录）—— 期货页默认周期。
        let lastFuturesFreq = '1m';  // 期货上下文上次使用的周期（同类切换继承）；默认 1m

        // 双窗口状态
        let isDualWindow = false;

        let dualSubData = null;

        let dualSubFreq = '';

        let dualSubViewOffset = 0, dualSubViewCount = VIEW_COUNT;

        let dualSubMouseX = -1, dualSubMouseY = -1;

        let mainCanvas, mainCtx, subCanvas, subCtx;

        // 翻转视图模式：将上涨行情反转为下跌、下跌反转为上涨（缠论做空视角）
        let _isMirrorMode = false;

        // 股票「止盈止损」图上推演（v1.5 §4）：激活标志 + 后端推演计划
        let _tpslActive = false;
        let _tpslPlan = null;

        // 取消选点菜单项是否可用（有选点且非双窗口；股票复盘态已放开——改L，R保持复盘点）
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
        const INTRADAY_FREQS_JS = ["30m", "15m", "5m", "1m", "15s"];

        // 实时模式（期货/期指 SSE 推送）
        let isRealtimeMode = false;       // 是否处于实时模式

        let realtimeSymbol = null;        // 实时模式下当前品种代码

        let realtimeFreq = null;          // 实时模式下当前周期

        let realtimeStartTime = null;     // 实时模式下选点起始时间

        let realtimeEndTime = null;       // 复盘软断开边界（end_time）
        // 复盘挂起（2026-10-09 用户反馈）：点下复盘 → SSE init 落地之前，chartData 还是
        // **上一份（实时）数据**，末根仍覆盖「现在」⇒ 按数据算出来的倒计时会继续把剩余
        // 秒数走完才消失（实测「还剩 15 秒时选复盘，倒计时照样走到 0 才不见」）。
        // 置 true 期间 `_calcCountdownState` 直接给 null ⇒ **点下复盘即消失**。
        // 复位路径（三条，缺一条就会永久隐藏）：数据落地（两个 init 处理器）、
        // disconnectRealtime（含 init 报错分支）、SSE onerror。
        let replayPending = false;

        let realtimeEventSource = null;   // SSE EventSource 对象

        let realtimeConnected = false;    // SSE 是否已连接

        const COLORS = {
            bg: "#1a1a2e", grid: "rgba(255,255,255,0.04)", text: "#8892b0", textLight: "#a8b2d1",
            up: "#FF3C3C", down: "#00F0F0", bi: "#FFD700",
            crosshair: "rgba(255,255,255,0.3)",
            macdUp: "rgba(255,60,60,0.6)", macdDown: "rgba(0,240,240,0.6)", // 原值: macdUp="rgba(255,68,68,0.6)", macdDown="rgba(0,221,0,0.6)"
            dif: "#FFFFFF", dea: "#F77F00", // 原值: dea="#FFD700"
            rsi: "#FFFFFF", // RSI 折线——与 MACD 白线（COLORS.dif）同色；守卫用例钉住两者相等
        };

        // ===== K线倒计时进度条（快期3风格） =====
        let _countdownBounds = null; // 上窗/单窗倒计时区域边界，用于增量更新

        let _subCountdownBounds = null; // 下窗倒计时区域边界，用于增量更新

        // ============================================================
        // 股票买卖点扫描（逐只扫描，实时进度，可中断）
        // ============================================================
        let _scanRunning = false;

        let _scanAborted = false;

        let _scanTaskId = null; // 当前批量扫描 task_id（中止时立即经 /api/stocks/scan/{task_id}/cancel 传播）

        let _scanMode = "ann"; // "ann" = 标注扫描, "ma" = 均线分类扫描, "fangliang" = 放量扫描, "lianzhang" = 连涨扫描, "fx_d" = 底分型扫描, "bsp" = 买卖点扫描, "backtest" = 回测扫描

        let _scanRecentDays = 1; // 最近N根K线，默认1

        let _scanSources = ["zxg"]; // 多选：["zxg", "page_index", "tdxhy2", "tdxhy3"]

        let _scanFreq = "d"; // 扫描周期，默认日K

        // 流通市值过滤下限（亿）。null = 未配置：请求不带该参数，阈值由后端
        // app_config.SCAN_MIN_FLOAT_MC 兜底（单一事实源在后端，前端不再写死默认值）；
        // 0 = 不过滤。用户在设置抽屉显式设置后存 localStorage 并随请求传入。
        let _scanMinFloatMc = null;
        // /api/health 下发的后端默认值（SCAN_MIN_FLOAT_MC），仅作输入框 placeholder 与「未配置」语义，
        // 不覆盖用户显式设置值。
        let _scanMinFloatMcServer = null;

        // 放量扫描的比较窗口根数：SSOT 在后端 app_config.SCAN_FANGLIANG_WINDOW_BARS
        // （App/AppScan.py 的 fangliang 分支据此取前窗峰值）。前端**只在口径披露
        // 文案里引用它**，绝不参与判定；经 /api/health 的 config 下发（同
        // _scanMinFloatMcServer 的手法）。null = 尚未拉到 ⇒ 文案回落中性的
        // 「前述比较窗口」，不编造 120 —— 写死数字等于在前端复制一份口径。
        let _scanFangliangWindowBars = null;

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
                // 槽位：新键优先；旧键（布尔 showVolume / showSubVolume）保留迁移 ——
                // 第 1 槽沿用旧选择（false → 'macd'、true → 'vol'），第 2 槽补 'rsi'，
                // 使升级后"原来在看的那个指标还在原位"，同时直接获得同屏 RSI 的能力。
                if (Array.isArray(s.bottomSlots)) {
                    const validSlots = s.bottomSlots.filter(id => BOTTOM_INDICATORS[id]).slice(0, 2);
                    if (validSlots.length) _bottomSlots = validSlots;
                } else if (typeof s.showVolume === 'boolean') {
                    _bottomSlots = [s.showVolume ? 'vol' : 'macd', 'rsi'];
                }
                if (Array.isArray(s.subBottomSlots)) {
                    const validSub = s.subBottomSlots.filter(id => BOTTOM_INDICATORS[id]).slice(0, 2);
                    if (validSub.length) _subBottomSlots = validSub;
                } else if (typeof s.showSubVolume === 'boolean') {
                    _subBottomSlots = [s.showSubVolume ? 'vol' : 'macd'];
                }
                if (s.volDisplayMode === 'bar' || s.volDisplayMode === 'macd') _volDisplayMode = s.volDisplayMode;
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
                    bottomSlots: _bottomSlots,
                    subBottomSlots: _subBottomSlots,
                    volDisplayMode: _volDisplayMode,
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
            if (mainFreq === '15m') return '5m';
            // 期货周期映射（股票5m无对应，期货5m→1m）
            if (mainFreq === '5m') return '1m';
            if (mainFreq === '1m') return '15s';
            return null; // 5m(股票)/15s(期货)无对应
        }

        // 股票双窗口配对空间（配对放宽至 9 对，与后端 _STOCKS_DUAL_PAIRS 同口径）
        // 上窗周期 → 可选下窗周期集合；getDualSubFreq 返回其中的默认配对
        const STOCKS_DUAL_PAIRS_JS = {
            'w':   ['d', '30m', '15m', '5m'],
            'd':   ['30m', '15m', '5m'],
            '30m': ['15m', '5m'],
            '15m': ['5m'],
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
            // 底部指标区槽位 chip：单击沿 BOTTOM_ORDER 切换该槽的指标
            canvas.addEventListener("click", function(e) {
                if (!chartData) return;
                // 双击的第二次点击（click.detail > 1）不重复切换 —— 浏览器必然先发
                // 两次 click 才发 dblclick；不看 detail 会让双击 chip 连切两位。
                if (e.detail > 1) return;
                const rect = canvas.getBoundingClientRect();
                const slot = hitBottomSlotChip(e.clientX - rect.left, e.clientY - rect.top);
                if (slot >= 0) cycleBottomSlot(slot);
            });
            canvas.addEventListener("dblclick", function(e) {
                if (!chartData) return;
                const rect = canvas.getBoundingClientRect();
                const clickX = e.clientX - rect.left;
                const clickY = e.clientY - rect.top;
                const area = getChartArea();
                // 0. 底部指标区（各槽标签行 + 绘图窗）只做**命中拦截**：
                //    切换入口已改为标签行 chip（cycleBottomSlot），双击不再切换指标，
                //    但这一层拦截必须留着 —— 否则会落到底下的"双击空白处 → 恢复全视图"。
                const bottomTop = getBottomSlotLabelArea(0).y;
                const bottomBottom = getBottomAreaBottomY();
                if (clickX >= area.x && clickX <= area.x + area.w &&
                    clickY >= bottomTop && clickY <= bottomBottom) {
                    return;
                }
                // 1. 只在K线主图区域内有效（下沿用 >= ：主图下沿恰好等于底带上沿，
                //    交给上面那段处理，不让它去参与K线命中测试）
                if (clickX < area.x || clickX > area.x + area.w ||
                    clickY < area.y || clickY >= area.y + area.h) {
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
                // 复盘态选点：四场景全放开（股票/期货 × 单窗/双窗，一致原则）——
                // 选点=改焦点窗 L，R 保持复盘点（请求带 end_date）
                // 双窗选点规则（股票/期货一致）：上下窗各自可选点，按各自周期列
                // 存 CSV；选点 = 改焦点窗 L，另一窗 L 不受牵动（各自冻结）。
                // 上窗选点 → 后端保存T → 重连双窗SSE带 start_time=T（下窗 L 取
                // CSV(sub 列)，不跟随上窗）
                // 4. 如果双击落在分型K线上且找到对应笔，手选进入段
                if (clickedBiIdx >= 0) {
                    // 焦点窗跟随双击所在窗（与下窗分支对称）：上窗选点即以
                    // 上窗为焦点，供「取消选点」按焦点窗清列——焦点与选点不
                    // 同源时会清错列（选了下窗列却清上窗列）。
                    if (isDualWindow) { activeDualWindow = 'main'; updateActiveWindowClass(); }
                    // 引擎运行中拦截：选点写 CSV，影响引擎下次重连的行情窗口（四类拦截之一）
                    if (autoOrderRunning && isFuturesMode()) {
                        showAlert('交易引擎运行中，请先关闭，再选点');
                        return;
                    }
                    const code = chartData.meta.symbol;
                    const freq = currentFreq;
                    const isFutures = chartData.meta.market === 'futures';
                    document.getElementById("loading").classList.remove("hidden");
                    document.querySelector(".loading-text").textContent = "正在手选进入段...";
                    // 股票双窗选点：上窗选点带双窗上下文，
                    // 后端销毁双窗两键缓存并按双窗路径重建（响应含 data.sub）
                    const dualQuery = (isDualWindow && !isFutures)
                        ? "&dual=1&main_freq=" + currentFreq + "&sub_freq=" + dualSubFreq : "";
                    // 复盘态选点（四场景全放开）：带当前复盘点，后端重建 [选点, 复盘点]（改L不改R）
                    const _replayEndMain = (chartData.meta && chartData.meta.is_replay && chartData.klines && chartData.klines.length > 0)
                        ? inputDateToApi(klineDateToInput(chartData.klines[chartData.klines.length - 1].date, freq), freq)
                        : null;
                    const replayEndQuery = _replayEndMain ? "&end_date=" + encodeURIComponent(_replayEndMain) : "";
                    // 视图左边界 L（期货专用）：定位窗口必须与前端**当前视图**同源。
                    // 选点 = 改 L，前端发来的 bi_idx 属于当前视图的笔列表，后端必须
                    // 在同一个窗口（同一个 L）上取 bi_list[bi_idx]。L 只能由前端提供：
                    // 它是冻结值（方案 §2.1「改 R 不改 L」）——冷启动 = 方式C 的实时
                    // 窗口 L、复盘原样带过来、选点后 = 新选点 T；后端自己推导在
                    // 「复盘态且该周期无选点」时必然落到另一个窗口（从复盘点往前 N 根）。
                    const _viewStart = (isFutures && chartData.klines && chartData.klines.length > 0)
                        ? "&start_time=" + encodeURIComponent(inputDateToApi(klineDateToInput(chartData.klines[0].date, freq), freq))
                        : "";
                    const apiPath = isFutures
                        ? "/api/futures/" + encodeURIComponent(code) + "/select/point?freq=" + freq + "&bi_idx=" + clickedBiIdx + replayEndQuery + _viewStart
                : "/api/stocks/" + encodeURIComponent(code) + "/select/point?freq=" + freq + "&bi_idx=" + clickedBiIdx + replayEndQuery + dualQuery;
                    const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号
                    fetch(apiPath, { method: "POST" })
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "手选失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
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
                                // 四期：重连不带 start——后端从 CSV 恢复两窗选点（单双窗同构）；
                                // 复盘态必须带 end（R 保持复盘点）：漏传会让上窗从复盘态
                                // 掉回实时态（下窗 :3260 / 取消选点 :3627 均已带 end）。
                                connectRealtimeDual(code, freq, dualSubFreq, _replayEndMain);
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
                                // 重连SSE，带上选点时间（savedDate 已在上方取自 data.meta）；
                                // 复盘态选点：同时带复盘点，保持 [选点, 复盘点] 复盘窗口
                                const replayEnd2 = (chartData.meta && chartData.meta.is_replay && chartData.klines && chartData.klines.length > 0)
                                    ? inputDateToApi(klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq), currentFreq)
                                    : null;
                                connectRealtimeInit(code, freq, savedDate, replayEnd2);
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
                            } else if (chartData.meta.freq === "15分钟") {
                                currentFreq = "15m";
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
                            document.getElementById("btn-15m").classList.toggle("active", currentFreq === "15m");
                            document.getElementById("btn-5m").classList.toggle("active", currentFreq === "5m");
                            // 重置视图：选点后klines只含选点之后的K线，直接全部显示
                            adjustViewForSavedPoint();
                            // 双窗：同步下窗数据与视图。四期语义——下窗 L 不受上窗
                            // 选点牵动（后端按 CSV(sub 列)/方式A 自算），本次响应里的
                            // 下窗即其自身窗口，前端全量显示（视口无 VIEW_COUNT 限制：
                            // 后端加载多少根就显示多少根，与上窗
                            // adjustViewForSavedPoint 规则一致）。
                            // A/C 操作的下窗仍走 VIEW_COUNT 视口，见别处。
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
                            if (_isChartActionStale(_seq)) return; // [N1] 过期请求的失败不得弹窗打断新状态
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                            setTimeout(() => {
                                showAlert(err.message);
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
                        showToast("请加载更多K线...");
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
            const L = getLayoutParams();
            const netH = h - L.top - L.bottom - L.gap;
            const chartH = netH * (1 - L.volRatio);
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: L.top, w: totalW - rightGap, h: chartH };
        }

        // ══ 底部指标区槽位几何（单窗 2 槽 / 双窗 1 槽）══════════════════════
        // 硬不变式：slotH(n) = netH × VOL_RATIO − MACD_TEXT_HEIGHT，**与槽数 n 无关**
        //   ⇒ 单窗双槽的每个槽，与改造前的单窗单槽逐像素相同（H=820 时 136.4px）；
        //   ⇒ n = 1 时两个几何函数与改造前的 getVolArea() / getMacdTextArea() 逐像素等价
        //      —— 这就是"双窗零回归"的来源。
        // n = 2 时 label(0) ∪ plot(0) ∪ label(1) ∪ plot(1) 恰好铺满
        //   [L.top + chartH, L.top + chartH + total]，无重叠无空隙。
        function _bottomSlotMetrics() {
            const L = getLayoutParams();
            const netH = canvas.clientHeight - L.top - L.bottom - L.gap;
            const chartH = netH * (1 - L.volRatio);
            const total = netH * L.volRatio;          // 底部区总高（volRatioFor 已按槽数翻倍）
            const n = SLOT_COUNT();
            return { L: L, netH: netH, chartH: chartH, total: total, n: n,
                     slotH: (total - n * MACD_TEXT_HEIGHT - (n - 1) * SLOT_GAP) / n };
        }

        function getBottomSlotLabelArea(i) {       // 第 i 槽的标签行
            const m = _bottomSlotMetrics();
            const totalW = canvas.clientWidth - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left,
                     y: m.L.top + m.chartH + i * (MACD_TEXT_HEIGHT + m.slotH + SLOT_GAP),
                     w: totalW - rightGap, h: MACD_TEXT_HEIGHT };
        }

        function getBottomSlotPlotArea(i) {        // 第 i 槽的绘图窗
            const label = getBottomSlotLabelArea(i);
            const m = _bottomSlotMetrics();
            return { x: label.x, y: label.y + MACD_TEXT_HEIGHT, w: label.w, h: m.slotH };
        }

        // 整个底部区的下沿（十字光标竖线 / 日期轴的定位基准）。
        // ≡ L.top + chartH + total，与改造前的 volArea.y + volArea.h 同值。
        function getBottomAreaBottomY() {
            const m = _bottomSlotMetrics();
            return m.L.top + m.netH;
        }

        // 第 i 槽标签行上沿的分割线。槽 0 的标签行上沿与主图下沿**重合**（同一条
        // y = L.top + chartH），那条线已由 drawGrid 的最后一条网格线给出；所以只对
        // i > 0 画，免得同一条线被叠画两遍而比其它槽亮一倍。
        function drawBottomSlotDivider(i) {
            const label = getBottomSlotLabelArea(i);
            ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(label.x, label.y);
            ctx.lineTo(label.x + label.w, label.y);
            ctx.stroke();
        }

        // 底部区占比**按槽数翻倍**：底部区总高 = 槽数 × 现在的底部区。
        // 取 n × VOL_RATIO 是唯一能让"每槽绘图窗 = netH×VOL_RATIO − MACD_TEXT_HEIGHT"
        //   与 n 无关的式子 ⇒ 每槽与今天单窗单槽逐像素相同；n = 1 时恒等于 VOL_RATIO。
        // 别写成"更大的常数"（0.30 / 0.36 / 0.40 硬编码都算）：那会让每槽高度随窗口模式
        //   与槽数漂移，就没法再用"与现单槽逐像素相同"来验收了。
        function volRatioFor(slotCount) { return VOL_RATIO * slotCount; }

        // 当前渲染目标用的槽位数组：双窗下窗用 _subBottomSlots，其余（单窗 / 双窗上窗）
        // 用 _bottomSlots。_renderChart 会在渲染下窗时把 chartData 换成 dualSubData。
        function _bottomSlotList() {
            return (chartData && chartData === dualSubData) ? _subBottomSlots : _bottomSlots;
        }

        // 第 i 槽当前选中的指标 id；越界 / 数组被清空时回落到 'macd'，
        // 避免"标签行空了、指标窗没内容"的空状态（槽数本身由窗口模式决定，不随内容变）。
        function _slotAt(i) {
            const list = _bottomSlotList();
            return (list && BOTTOM_INDICATORS[list[i]]) ? list[i] : 'macd';
        }

        function _setSlotAt(i, id) {
            const list = _bottomSlotList();
            if (!list || list[i] === undefined || !BOTTOM_INDICATORS[id]) return;
            list[i] = id;
        }

        // 当前渲染的槽里是否有某个指标（vol 槽的类MACD 只在此时才算，柱状图模式零开销）
        function _hasBottomSlot(id) {
            const n = SLOT_COUNT();
            for (let i = 0; i < n; i++) { if (_slotAt(i) === id) return true; }
            return false;
        }

        // chip 点击：该槽在 BOTTOM_ORDER 内往后跳一位（macd → rsi → vol → macd）
        function cycleBottomSlot(i) {
            const next = BOTTOM_ORDER[(BOTTOM_ORDER.indexOf(_slotAt(i)) + 1) % BOTTOM_ORDER.length];
            _setSlotAt(i, next);
            saveOverlaySettings();
            if (window._isRenderingBottom) renderBottom(); else render();
        }

        // 第 i 槽 chip 的矩形（标签行**最右**）。只依赖几何与文本宽度，不读鼠标状态。
        // 靠右放：chip 上写的就是该槽的指标短名（成交额 / 成交量 / MACD / RSI），
        // 正好替代改造前在正文右侧又画一遍的「成交额 / 成交量」文字。
        function getBottomSlotChipRect(i) {
            const label = getBottomSlotLabelArea(i);
            const name = BOTTOM_INDICATORS[_slotAt(i)].tabLabel();
            ctx.save();
            ctx.font = "11px monospace";
            const tw = ctx.measureText(name).width;
            ctx.restore();
            return { x: label.x + label.w - 2 - (tw + CHIP_PAD_X * 2), y: label.y + 1,
                     w: tw + CHIP_PAD_X * 2, h: CHIP_H - 2, text: name };
        }

        // chip 命中测试（返回槽号，未命中 -1）。命中区仅限标签行左侧，
        // 不触碰 K 线区单击语义（选点走双击）。
        function hitBottomSlotChip(x, y) {
            const n = SLOT_COUNT();
            for (let i = 0; i < n; i++) {
                const r = getBottomSlotChipRect(i);
                if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) return i;
            }
            return -1;
        }

        function getVisibleKlines() {
            if (!chartData) return [];
            const total = chartData.klines.length;
            let start = Math.max(0, Math.floor(viewOffset));
            // 安全网：视口偏移越界（数据少于偏移，常见于多窗共享全局 viewOffset 后切到较短序列）
            // 会令切片为空 → 整图上窗/下窗静默空白。越界时回退到末尾，保证切片非空。
            if (total > 0 && start >= total) {
                start = Math.max(0, total - viewCount);
            }
            const end = Math.min(total, start + viewCount + 2);
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

        // ══ 成交额/量 的类MACD（只在前端算，不动后端 SSE 快照）══════════════
        // 需求：底部指标区的「成交额/量」除柱状图外，可切换为「类MACD」——
        // 借用传统 MACD(12,26,9) 算法，把收盘价换成成交额（股票）/成交量（期货），
        // 算出黄白线（DIF/DEA）与红绿柱（BAR）。
        // 为什么放在前端算：后端只下发「价格 MACD」的 dif/dea/macd，那条链路围着
        // AppSSE 的增量 EMA 状态机与「预览bar继承」口径转；把成交额/量也塞进去，
        // 改的是实时流热路径。类MACD 只是一种显示模式，前端算波及面最小，
        // 且实时刷新天然跟随每次 render（无需重连 SSE、无需落盘）。
        // 算法逐字对齐后端 App/AppUtils.py 的 ema() / calculate_macd()：
        //   ema 以首个样本为种子（不做 SMA 预热），k = 2/(N+1)；
        //   样本不足 26 根时全 0（与后端同规则）。
        // 后端把结果 round 到 4 位小数后再下发；本模式不走那条链路，保留全精度，
        // 显示端再按成交额/量的单位格式化（见 formatVolMacdVal）。
        // >>> VOL_MACD_CORE（Test/test_vol_macd_mode.py 按此标记抽取本段到 node 做数值对齐）
        const VOL_MACD_PARAMS = { fast: 12, slow: 26, signal: 9 };

        const ZERO_MACD_VALS = { dif: 0, dea: 0, macd: 0 };

        function _volMacdEma(values, period) {
            const k = 2.0 / (period + 1);
            const out = new Array(values.length);
            for (let i = 0; i < values.length; i++) {
                out[i] = (i === 0) ? values[i] : values[i] * k + out[i - 1] * (1 - k);
            }
            return out;
        }

        // 成交额/量 类MACD —— 返回 Map<K线对象, {dif, dea, macd}>
        // 尾部占位K线（未形成的预览bar：成交量/成交额恒为 0，只有 OHLC 被填入）
        // **不参与 EMA**：0 会把 EMA 一路拉向 0，末根出现假的深坑；与后端
        // _inherit_macd_for_preview_bar 同口径，末根继承前一根已算出的结果。
        function calcVolMacdMap(klines, isFutures) {
            const out = new Map();
            if (!klines || !klines.length) return out;
            const metricOf = isFutures
                ? function(k) { return k.vol || 0; }
                : function(k) { return k.amount || 0; };
            let realN = klines.length;
            while (realN > 0 && metricOf(klines[realN - 1]) <= 0) realN--;
            if (realN < VOL_MACD_PARAMS.slow) {
                for (let i = 0; i < klines.length; i++) out.set(klines[i], ZERO_MACD_VALS);
                return out;
            }
            const vals = [];
            for (let i = 0; i < realN; i++) vals.push(metricOf(klines[i]));
            const emaFast = _volMacdEma(vals, VOL_MACD_PARAMS.fast);
            const emaSlow = _volMacdEma(vals, VOL_MACD_PARAMS.slow);
            const dif = [], dea = [];
            for (let i = 0; i < realN; i++) dif.push(emaFast[i] - emaSlow[i]);
            const deaRaw = _volMacdEma(dif, VOL_MACD_PARAMS.signal);
            for (let i = 0; i < realN; i++) dea.push(deaRaw[i]);
            let last = ZERO_MACD_VALS;
            for (let i = 0; i < realN; i++) {
                last = { dif: dif[i], dea: dea[i], macd: 2 * (dif[i] - dea[i]) };
                out.set(klines[i], last);
            }
            for (let i = realN; i < klines.length; i++) out.set(klines[i], last);
            return out;
        }
        // <<< VOL_MACD_CORE

        // 最近一次渲染算出的成交额/量类MACD：键=K线对象（视口切片与全序列共享同一批
        // 对象），绘制 / 标签 / 纵轴三处都从这里取值，保证同源、不会各算各的。
        let _volMacdMap = null;

        // 取某根K线的成交额/量类MACD值；未算（非本显示模式）时按 0 处理
        function volMacdOf(k) {
            return (_volMacdMap && _volMacdMap.get(k)) || ZERO_MACD_VALS;
        }

        // 成交额/量 类MACD 的纵轴取值范围（口径与 getMacdRange 一致：
        // dif/dea/macd 三者同域，全 0 时兜底 ±1 避免除以零）
        function getVolumeMacdRange(klines) {
            if (!klines.length) return { min: -1, max: 1 };
            let min = Infinity, max = -Infinity;
            klines.forEach(k => {
                const v = volMacdOf(k);
                if (v.macd < min) min = v.macd;
                if (v.macd > max) max = v.macd;
                if (v.dif < min) min = v.dif;
                if (v.dif > max) max = v.dif;
                if (v.dea < min) min = v.dea;
                if (v.dea > max) max = v.dea;
            });
            if (min === 0 && max === 0) return { min: -1, max: 1 };
            const margin = Math.max(Math.abs(max), Math.abs(min)) * 0.1;
            return { min: min - margin, max: max + margin };
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
            // 顶栏「统计」按钮：股票态文案改成「回测」+ 双窗态禁用（§4.1 / §4.4）。
            // 放这里而不是各加载回调里：市场态随标的/周期/复盘切换而变，散在多个
            // 调用点必漏；render() 是这些路径的公共下游。内部有文案缓存，改动为零。
            syncStatsButtonLabel();
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
            const _savedBottomSlots = _bottomSlots;
            viewOffset = vOffset; viewCount = vCount;
            mouseX = mX; mouseY = mY;
            currentFreq = freq;
            chartData = data;
            // 双窗口模式：下窗使用独立的槽位数组，不与上窗联动
            if (data === dualSubData) _bottomSlots = _subBottomSlots;
            const w = canvas.clientWidth, h = canvas.clientHeight;
            ctx.fillStyle = COLORS.bg; ctx.fillRect(0, 0, w, h);
            const klines = getVisibleKlines();
            if (!klines.length) {
                viewOffset = _savedViewOffset; viewCount = _savedViewCount;
                mouseX = _savedMouseX; mouseY = _savedMouseY;
                currentFreq = _savedCurrentFreq;
                chartData = _savedChartData;
                _bottomSlots = _savedBottomSlots;
                return;
            }
            const area = getChartArea();
            const priceRange = getPriceRange(klines);
            // 成交额/量 类MACD：只在「某个槽选中了 vol 且显示模式为 macd」时计算
            // （全序列参与 EMA 预热），结果按K线对象索引，供绘制 / 标签 / 纵轴同源取值。
            // 必须先于下面的槽位值域循环赋值：vol.range() 在 macd 模式下会读 _volMacdMap。
            _volMacdMap = (_hasBottomSlot('vol') && _volDisplayMode === 'macd')
                ? calcVolMacdMap(data.klines, !!(data.meta && data.meta.market === 'futures'))
                : null;
            // 槽位：按槽数取绘图窗与值域。值域一律走各指标的 range() 钩子
            // （rsi 固定 [0,100]、vol 与 macd 从数据推），渲染层不再 per-指标 分支。
            const slotCount = SLOT_COUNT();
            const slotAreas = [], slotRanges = [];
            for (let si = 0; si < slotCount; si++) {
                slotAreas.push(getBottomSlotPlotArea(si));
                slotRanges.push(BOTTOM_INDICATORS[_slotAt(si)].range(klines));
            }
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
            const bottomBottomY = getBottomAreaBottomY();
            ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(area.x + area.w, area.y);
            ctx.lineTo(area.x + area.w, bottomBottomY);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(area.x, area.y);
            ctx.lineTo(area.x, bottomBottomY);
            ctx.stroke();
            const klinesToDraw = klines.slice(0, viewCount);
            // 底部指标区绘制顺序：① 各槽画进自己的绘图窗 → ② 槽间分割线 → ③ 标签行（含 chip）。
            // ② 必须在 ① 之后：分割线的 y 同时是**上一槽绘图窗的下沿**，而柱状图的柱体基线
            //   就压在那一行 —— 空心柱用 strokeRect 描边，描边还会往下溢出约半像素。
            //   实测该行 1093 px 中 736 px 是柱体像素；画在 ① 之前会被盖成断续锯齿。
            //   放在最后 ⇒ 分隔线恒横贯全宽，且「标签行上沿有分割线」与上一槽画什么无关。
            // ③ 也放在 ① 之后：标签行的 chip / 文本同样不该被上一槽的半像素溢出盖住。
            // bottomCtx 供各 draw / label 钩子取值，避免它们各自重算一遍。
            const bottomCtx = {
                klines: klinesToDraw, barStep: barStep, subPixelOffset: subPixelOffset,
                barWidth: barWidth, macdBarWidth: MACD_BAR_WIDTH, targetK: null, textX: 0,
            };
            for (let si = 0; si < slotCount; si++) {
                BOTTOM_INDICATORS[_slotAt(si)].draw(slotAreas[si], slotRanges[si], bottomCtx);
            }
            for (let si = 1; si < slotCount; si++) {   // 槽 0 的上沿分割线由主图末条网格线给出
                drawBottomSlotDivider(si);
            }
            for (let si = 0; si < slotCount; si++) {
                drawBottomSlotLabel(si, bottomCtx);
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
            // 保护价线只画主图（dualSubData 是双窗副图；单窗模式它为 null，恒不等 → 恒画）
            if (data !== dualSubData) drawRunSegments(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            // 股票「止盈止损」分段横虚线：只画主图（与保护价线同一约定）
            if (data !== dualSubData) drawTpslLines(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawAnnotations(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawViewportHighLow(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            _overlayData = null;
            drawCrosshair(klinesToDraw, area, priceRange, bottomBottomY, barStep, subPixelOffset);
            drawPriceAxis(area, priceRange);
            for (let si = 0; si < slotCount; si++) {
                BOTTOM_INDICATORS[_slotAt(si)].axis(slotAreas[si], slotRanges[si]);
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
                    } else if (freq === '1m' || freq === '30m' || freq === '15m' || freq === '5m') {
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
            _bottomSlots = _savedBottomSlots;
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

        function drawMacd(klines, macdArea, macdRange, barStep, barWidth, subPixelOffset, valOf) {
            // valOf（可选）：返回该K线的 {dif, dea, macd}；缺省用K线自身的价格MACD字段。
            // 传入取值函数即复用同一套画法绘制「成交额/量 类MACD」（见 drawVolumeMacd）——
            // 翻转视图/零线/柱宽等口径天然一致，不会出现两套画法各自漂移。
            const getVals = valOf || function(k) { return k; };
            // 翻转视图：MACD柱绕零线翻转（正柱→零线下方），颜色对调；dif/dea线Y轴翻转
            const range = macdRange.max - macdRange.min;
            const macdToY = (v) => _isMirrorMode
                ? macdArea.y + (v - macdRange.min) / range * macdArea.h
                : macdArea.y + macdArea.h - (v - macdRange.min) / range * macdArea.h;
            // 零线Y坐标：用 macdToY(0) 统一计算，翻转模式下自动翻转（与dif/dea线一致）
            const zeroY = macdToY(0);
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const v = getVals(k) || ZERO_MACD_VALS;
                const isUp = v.macd >= 0;
                ctx.fillStyle = (_isMirrorMode ? !isUp : isUp) ? COLORS.macdUp : COLORS.macdDown;
                const macdH = Math.abs(v.macd) / range * macdArea.h;
                const y = isUp ? (_isMirrorMode ? zeroY : zeroY - macdH)
                               : (_isMirrorMode ? zeroY - macdH : zeroY);
                ctx.fillRect(x - barWidth / 2, y, barWidth, macdH);
            });
            ctx.strokeStyle = COLORS.dif; ctx.lineWidth = 1;
            ctx.beginPath();
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const y = macdToY((getVals(k) || ZERO_MACD_VALS).dif);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.strokeStyle = COLORS.dea; ctx.lineWidth = 1;
            ctx.beginPath();
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const y = macdToY((getVals(k) || ZERO_MACD_VALS).dea);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.strokeStyle = "rgba(255,255,255,0.2)"; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(macdArea.x, zeroY); ctx.lineTo(macdArea.x + macdArea.w, zeroY); ctx.stroke();
        }

        // 成交额/量 的类MACD绘制：画法完全复用 drawMacd，只换取值来源
        function drawVolumeMacd(klines, volArea, macdRange, barStep, barWidth, subPixelOffset) {
            drawMacd(klines, volArea, macdRange, barStep, barWidth, subPixelOffset, volMacdOf);
        }

        // ══ RSI 槽（RSI(12)）════════════════════════════════════════════════
        // 数据来自后端：App/AppUtils.calculate_rsi → SSE 逐根下发 k.rsi。
        // 前端**零计算** —— 与 MACD 读 k.dif/dea/macd 完全同构；显示处理也照前端
        // MACD 指标那一套（值域 / 绘制 / 纵轴 / 标签 / 翻转），唯一差别是指标定义
        // 本身：单线，且值域随可见窗口自适应（见 getRsiRange）。
        function rsiOf(k) {
            const v = k ? k.rsi : undefined;
            return (typeof v === 'number' && isFinite(v)) ? v : null;
        }

        // RSI 值域**随可见窗口自适应**（与 getMacdRange 同机制：只吃 getVisibleKlines()
        // 那一刀切出来的 klines），不再固定 [0,100]。固定值域会让缩放/滚动对 RSI 曲线
        // 毫无影响 —— 放大后被压成一条直线（通达信副图是「自动」纵轴：按当前显示区间
        // 的最大/最小值铺刻度，缩放时范围跟着变；另有「固定」模式才手填上下边界）。
        // 上下各留 5% 余量（与 getPriceRange 同口径）；整窗无 rsi（全缺值）回落 [0,100]。
        // ★ 留白后必须**夹回 [0,100]**：RSI 是有界量，刻度落在定义域外一定是错的 ——
        //   实测（上证指数日K）窗口里有 RSI = 0 的K线时，下沿被留白算成 −4.76 印在
        //   纵轴上；反向（窗口含 RSI = 100，暖机段给过）上沿会越过 100。
        //   RSI = 0 / 100 不是占位值，是本实现的**暖机口径**产物：见
        //   App/AppUtils.calculate_rsi（前端只读 k.rsi）—— 前 period−1 根的 up/down
        //   走简单平均，这批 diff 一根上涨都没有 ⇒ ups = 0 ⇒ rs = 0 ⇒ RSI = 0.0。
        function getRsiRange(klines) {
            if (!klines || !klines.length) return { min: 0, max: 100 };
            let min = Infinity, max = -Infinity;
            klines.forEach(k => {
                const v = rsiOf(k);
                if (v === null) return;
                if (v < min) min = v;
                if (v > max) max = v;
            });
            if (min === Infinity) return { min: 0, max: 100 };
            let lo, hi;
            if (max - min < 1e-9) {          // 全等（含整窗恒 50）：给个不塌陷的窗口
                const d = Math.max(1, Math.abs(max) * 0.05);
                lo = min - d; hi = max + d;
            } else {
                const margin = (max - min) * 0.05;
                lo = min - margin; hi = max + margin;
            }
            return { min: Math.max(0, lo), max: Math.min(100, hi) };
        }

        // 值 → Y。翻转视图只翻 Y 方向，值原样参与计算（与 drawMacd 的 macdToY 同式）。
        function rsiToY(v, area, range) {
            const span = range.max - range.min;
            return _isMirrorMode
                ? area.y + (v - range.min) / span * area.h
                : area.y + area.h - (v - range.min) / span * area.h;
        }

        // RSI 折线（单线）+ 50 中轴 + 80/20 超买超卖参考线。
        // 三条水平线的 y **全部走同一个 rsiToY(v)**，禁止手写 area.h * 0.2 / * 0.8 之类
        // 相对比例 —— MACD 那条 0 轴踩过的坑正是"同一条线用两个式子算"。
        // 值域自适应后参考线可能整体出界（放大到趋势段时 20/80 都在窗外）：出界那条
        //   直接不画 —— 照画会落到相邻区域上（MACD 的 0 轴就有这个隐患）。
        // 翻转视图：三条线整体镜像（80 ↔ 20 互换、50 是不动点）；单线单色
        //   ⇒ 只翻 Y 位置，不改颜色、不改数值。
        function drawRsi(klines, area, range, barStep, subPixelOffset) {
            // 三条线**同一套线型**（细点虚线），只靠明度区分中轴与超买超卖：
            // 50 中轴略亮（0.2），80/20 略暗（0.15）。
            ctx.lineWidth = 1;
            ctx.setLineDash(RSI_REF_DASH);
            RSI_REF_VALUES.forEach(v => {
                if (v < range.min || v > range.max) return;   // 出界不画（见函数头说明）
                const y = rsiToY(v, area, range);
                ctx.strokeStyle = (v === 50) ? "rgba(255,255,255,0.2)" : "rgba(255,255,255,0.15)";
                ctx.beginPath(); ctx.moveTo(area.x, y); ctx.lineTo(area.x + area.w, y); ctx.stroke();
            });
            ctx.setLineDash([]);
            // 折线：k.rsi 缺失的点不画（与 k.dif 缺失时 ZERO_MACD_VALS 兜底同思路）
            ctx.strokeStyle = COLORS.rsi; ctx.lineWidth = 1;
            ctx.beginPath();
            let started = false;
            klines.forEach((k, i) => {
                const v = rsiOf(k);
                if (v === null) { started = false; return; }
                const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                const y = rsiToY(v, area, range);
                if (started) ctx.lineTo(x, y);
                else { ctx.moveTo(x, y); started = true; }
            });
            ctx.stroke();
            ctx.setLineDash([]);
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

        // ══ 底部指标区标签行（chip + 正文）══════════════════════════════════
        // chip：标签行最右那个显示当前指标名的小方块，可点击切换（见 cycleBottomSlot）。
        // 形态 = 文字 + 底色，**不画边框**。
        function drawBottomSlotChip(chip) {
            ctx.font = "11px monospace"; ctx.textAlign = "left";
            ctx.fillStyle = "rgba(255,255,255,0.08)";
            ctx.fillRect(chip.x, chip.y, chip.w, chip.h);
            ctx.fillStyle = COLORS.textLight;
            ctx.fillText(chip.text, chip.x + CHIP_PAD_X, chip.y + chip.h - 2);
        }

        // 画第 i 槽的标签行：该指标的正文（自左端起）+ 右端的 chip。
        // hover 命中该标签行 → 取指向的K线；否则末根兜底（与改造前的 drawMacdLabel 同规则）。
        function drawBottomSlotLabel(i, c) {
            const textArea = getBottomSlotLabelArea(i);
            const chip = getBottomSlotChipRect(i);
            drawBottomSlotChip(chip);
            let targetK = null;
            if (mouseX >= textArea.x && mouseX <= textArea.x + textArea.w) {
                const idx = Math.floor((mouseX - textArea.x + c.subPixelOffset) / c.barStep);
                targetK = c.klines[Math.min(idx, c.klines.length - 1)];
            }
            c.targetK = targetK || c.klines[c.klines.length - 1] || null;
            // 正文自标签行左端起（chip 已移到右端，正文不再接在 chip 后面）。
            // 与 chip 的基线同源：正文 = textArea.y + 11，chip = label.y + 1 + (CHIP_H-2) - 2 = 同值。
            c.textX = textArea.x + 4;
            BOTTOM_INDICATORS[_slotAt(i)].label(textArea, c);
        }

        // 成交额/量 槽的标签行：柱状图模式显示数值；类MACD 模式与价格MACD同构
        // （黄白线 + 红绿柱）。品种口径由标签行**右端的 chip**（成交额 / 成交量）标识。
        function drawVolSlotLabel(textArea, c) {
            const targetK = c.targetK;
            if (!targetK) return;
            ctx.font = "11px monospace"; ctx.textAlign = "left";
            const lineY = textArea.y + 11;
            if (_volDisplayMode === 'macd') {
                // 成交额/量 类MACD：正文与价格MACD同构（黄白线 + 红绿柱），
                // 不带「成交额MACD/成交量MACD」前缀，只留 MACD(12,26,9)；
                // 品种口径由标签行右端的 chip 标识（见 getBottomSlotChipRect）。
                // 数值单位随成交额/量。
                const vmacd = volMacdOf(targetK);
                const vlabel = "MACD(12,26,9)";
                ctx.fillStyle = COLORS.textLight;
                ctx.fillText(vlabel, c.textX, lineY);
                let vxPos = c.textX + ctx.measureText(vlabel + " ").width;
                ctx.fillStyle = COLORS.dif;
                ctx.fillText("DIF:" + formatVolMacdVal(vmacd.dif), vxPos, lineY);
                vxPos += ctx.measureText("DIF:" + formatVolMacdVal(vmacd.dif) + " ").width;
                ctx.fillStyle = COLORS.dea;
                ctx.fillText("DEA:" + formatVolMacdVal(vmacd.dea), vxPos, lineY);
                vxPos += ctx.measureText("DEA:" + formatVolMacdVal(vmacd.dea) + " ").width;
                // 翻转视图：BAR颜色对调，与翻转后的类MACD柱一致
                const vBarIsUp = _isMirrorMode ? (vmacd.macd < 0) : (vmacd.macd >= 0);
                ctx.fillStyle = vBarIsUp ? "#FF3C3C" : "#00F0F0";
                ctx.fillText("BAR:" + formatVolMacdVal(vmacd.macd), vxPos, lineY);
                // 品种口径不再在此重复右对齐画一遍：标签行右端的 chip 就是
                // 「成交额 / 成交量」本身（tabLabel()），重复绘制纯属冗余。
            } else {
                // 柱状指标模式：股票显示成交额，期货显示成交量（文字灰色，数字红/绿）
                // 翻转视图：颜色对调，与翻转后的成交量柱一致
                const volIsRise = _isMirrorMode ? (targetK.close < targetK.open) : (targetK.close > targetK.open);
                const volColor = volIsRise ? "#FF3C3C" : "#00F0F0";
                const vLabel = getVolLabel();
                ctx.fillStyle = "#a8b2d1";
                ctx.fillText(vLabel + ":", c.textX, lineY);
                let xPos = c.textX + ctx.measureText(vLabel + ":").width;
                ctx.fillStyle = volColor;
                const val = getVolMetric(targetK);
                const valLabel = isFuturesMode()
                    ? (val >= 10000 ? (val / 10000).toFixed(2) + "万" : Math.round(val).toString())
                    : (val >= 100000000 ? (val / 100000000).toFixed(2) + "亿" :
                       val >= 10000 ? (val / 10000).toFixed(2) + "万" : val.toFixed(2));
                ctx.fillText(valLabel, xPos, lineY);
            }
        }

        // 价格 MACD 槽的标签行：MACD(12,26,9) + DIF/DEA/BAR（BAR 颜色随翻转对调）
        function drawMacdSlotLabel(textArea, c) {
            const targetK = c.targetK;
            if (!targetK) return;
            ctx.font = "11px monospace"; ctx.textAlign = "left";
            const lineY = textArea.y + 11;
            ctx.fillStyle = COLORS.textLight;
            ctx.fillText("MACD(12,26,9)", c.textX, lineY);
            let xPos = c.textX + ctx.measureText("MACD(12,26,9) ").width;
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

        // RSI 槽的标签行：单值。数值用 MACD 的 DIF 白（COLORS.dif）—— 与「RSI 曲线取
        // MACD 白线色」同源；前缀仍是常规标签色（与 MACD 槽「名字浅色 + 数值亮色」同构）。
        // 无 BAR 换色分支 —— 数值含义不随视角变。
        function drawRsiSlotLabel(textArea, c) {
            const targetK = c.targetK;
            if (!targetK) return;
            ctx.font = "11px monospace"; ctx.textAlign = "left";
            const lineY = textArea.y + 11;
            const v = rsiOf(targetK);
            ctx.fillStyle = COLORS.textLight;
            ctx.fillText("RSI(12):", c.textX, lineY);
            ctx.fillStyle = COLORS.dif;
            ctx.fillText(v === null ? "--" : v.toFixed(2),
                         c.textX + ctx.measureText("RSI(12): ").width, lineY);
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
                // 与引擎口径一致：type2str() 可能是逗号串（同位置合并类型），
                // 任一段被勾选就显示 —— 否则这类点会"图上不画"且"单也不下"，
                // 两处同时静默漏掉，用户根本不知道有过这个买卖点。
                // 先判 bspFilter 是否在位：它被外部置空时按"全放行"处理
                //（与引擎"kv 缺失 = 全部放行"同口径），而不是在循环里抛
                // TypeError 把整段买卖点绘制打断（连不过滤的那些点也一起没了）。
                if (bspFilter) {
                    let _bspSegHit = false;
                    String(bsp.type).split(",").forEach(function (seg) {
                        if (bspFilter[seg.trim()]) _bspSegHit = true;
                    });
                    if (!_bspSegHit) return;
                }
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

        function drawCrosshair(klines, area, priceRange, bottomBottomY, barStep, subPixelOffset) {
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
                const crosshairEndY = bottomBottomY;
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
                } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '15m' || currentFreq === '5m') {
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
                // 「减持计划」徽标：命中窗口时显示「减持:√ 08-04~11-03」（多窗口取前 2 条）
                let reductionBadge = "";
                const reduction = chartData.meta.shareholder_reduction;
                if (reduction && reduction.active) {
                    const ws = (reduction.windows || []).slice(0, 2)
                        .map(w => (w && w.start && w.end) ? w.start + "~" + w.end : null)
                        .filter(Boolean).join(",");
                    reductionBadge = ` &nbsp; <span class="label" style="color:#e74c3c">减持:</span> <span class="label" style="color:#e74c3c">√${ws ? " " + ws : ""}</span>`;
                }
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
                    (chartData.meta.index_belong ? ` &nbsp; <span class="label">归属:</span> <span class="label">${chartData.meta.index_belong}</span>` : "") +
                    reductionBadge;
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

        // 成交额/量 类MACD 的纵轴：数值带符号（DIF/DEA/BAR 可正可负），
        // 单位跟随成交额/量（股票 万/亿，期货 手/万），与柱状模式纵轴同源格式化。
        function drawVolMacdAxis(volArea, macdRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
            const range = macdRange.max - macdRange.min;
            const zeroY = _isMirrorMode
                ? volArea.y + (0 - macdRange.min) / range * volArea.h
                : volArea.y + volArea.h * (macdRange.max / range);
            const topVal = _isMirrorMode ? macdRange.min : macdRange.max;
            const botVal = _isMirrorMode ? macdRange.max : macdRange.min;
            ctx.fillText(formatVolMacdVal(topVal), volArea.x + volArea.w + 6, volArea.y + 12);
            ctx.fillText("0", volArea.x + volArea.w + 6, zeroY + 4);
            ctx.fillText(formatVolMacdVal(botVal), volArea.x + volArea.w + 6, volArea.y + volArea.h - 4);
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

        // RSI 纵轴：上下两档 = 当前值域的 max / min（随可见窗口自适应，与 drawMacdAxis
        //   的 topVal / botVal 同机制，数值带两位小数）；中间那档是语义中位 **50**。
        // ⚠ 不能照抄 drawMacdAxis 的三段式再把中间写成值域中点：MACD 的 0 是值域
        //   **内插的零线**（位置随数据变），而 RSI 的 0 是值域**端点**、中位是 **50**。
        //   照抄会把中间那档写成 "0"（与下沿重合）⇒ "0" 被画两遍、中间缺 "50"。
        //   三档的 y 一样全部走 rsiToY，与折线 / 参考线 / 中轴同源；变量也不叫 zeroY。
        // 50 落在值域外时**不画**那一档：位置会算到绘图窗外，压上相邻区域。
        function drawRsiAxis(area, range) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace";
            ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
            const x = area.x + area.w + 6;
            const topVal = _isMirrorMode ? range.min : range.max;
            const botVal = _isMirrorMode ? range.max : range.min;
            ctx.fillText(topVal.toFixed(2), x, rsiToY(topVal, area, range) + 4);
            if (range.min <= 50 && 50 <= range.max) {
                ctx.fillText("50", x, rsiToY(50, area, range) + 4);
            }
            ctx.fillText(botVal.toFixed(2), x, rsiToY(botVal, area, range) + 4);
        }


        // 成交额/量 类MACD 的数值格式化：带符号，绝对值交给 formatVolume
        // （股票 万/亿、期货 手/万），与柱状模式的纵轴/标签同一套单位口径。
        function formatVolMacdVal(v) {
            if (!isFinite(v)) return "-";
            return (v < 0 ? "-" : "") + formatVolume(Math.abs(v));
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
            const area = getChartArea();
            const dateY = getBottomAreaBottomY() + 28;

            // 测量样本日期文本宽度，用于计算最小像素间距
            let sampleDate;
            if (currentFreq === '15s') {
                sampleDate = getKlineEndTime(klines[0].date, true);
            } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '15m' || currentFreq === '5m') {
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
                } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '15m' || currentFreq === '5m') {
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
            // 显示判据只有一个：**末根K线是否正在走**（末根区间覆盖「现在」，
            // 见下方 klineEnd 判定）。不能用 realtimeStartTime（= 是否设了
            // start）代替它：选点是改 L 不改 R（需求⑹），选点态 R 仍是最新、
            // 末根就是当前正在走的K线，倒计时应当照常显示；只有复盘态末根
            // 冻结在复盘点、末根已走完，才不显示。
            freq = freq || currentFreq;
            data = data || chartData;
            if (!isRealtimeMode) return null;
            // 复盘挂起（replayPending）：块注释见声明处 —— 点下复盘到复盘数据落地之间，
            // R 已指向复盘点，倒计时必须立刻消失，而不是拿还在走的旧末根算完剩余秒数。
            if (replayPending) return null;
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
            // 末根已走完（复盘态末根冻结在复盘点、或休市时最后一根已收盘）
            // ⇒ 没有「正在走」的K线，不画倒计时。
            if (now.getTime() >= klineEnd.getTime()) return null;
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
            if (!isRealtimeMode) return;
            // 显示判据唯一来源 = _calcCountdownState（末根K线是否正在走）。
            // 末根刚走完的那一秒仍需重绘一次，把上一秒画的进度条擦掉
            // （render 内 drawCountdownBar 见到 null 会把 _countdownBounds
            // 归 null），此后每秒直接返回——复盘态/休市零空转。
            if (!_calcCountdownState() && !_countdownBounds && !_subCountdownBounds) return;
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
                    showToast("区间起点: " + startDate + "，点击另一根K线完成选择");
                    render();
                } else {
                    // Ctrl+再次点击：取消选择
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showToast("区间选择已取消");
                    render();
                }
                return;
            }

            // === 普通点击：如果在选择模式中，完成区间选择 ===
            if (_rangeSelect.mode === 'SELECTED_A') {
                // 验证：同一股票、同一周期
                if (_rangeSelect.startFreq !== currentFreq || _rangeSelect.startSymbol !== chartData.meta.symbol) {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showToast("股票或周期已变更，区间选择已取消");
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
                showToast("已复制 " + (b - a + 1) + " 根K线数据到剪贴板");
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

        // ══════════════════════════════════════════════════════════
        // 轻提示（K线交互反馈 / 自动下单告警共用）
        //   · 时长 5s：正文是整句中文（告警最长约 72 字），1s 读不完；
        //     不等它自己淡出也可以 —— 点一下就立刻关。
        //   · 多条堆叠：调用方会一次连发多条（告警在 for 循环里逐条发），
        //     单例 div 会把先发的直接覆盖 → 5 条只看到最后 1 条，等于没提示。
        //   · 限宽换行 / 可选中：样式在 app.css 的 #toast-stack 与 .toast-item。
        // ══════════════════════════════════════════════════════════
        const TOAST_MS = 5000;      // 自动淡出时长
        const TOAST_MAX = 5;        // 同屏上限：再多也只是刷屏，丢掉最早的

        function showToast(msg, ms) {
            let stack = document.getElementById("toast-stack");
            if (!stack) {
                stack = document.createElement("div");
                stack.id = "toast-stack";
                document.body.appendChild(stack);
            }
            while (stack.children.length >= TOAST_MAX) {
                stack.removeChild(stack.firstChild);
            }
            const item = document.createElement("div");
            item.className = "toast-item";
            item.textContent = msg;
            item.title = "点击关闭";
            stack.appendChild(item);
            // 下一帧再置 opacity：新建节点在同一帧里从 0 置 1 不触发 transition，
            // 等于没有淡入（旧实现只有"已存在节点"那条路径才有淡入）。
            requestAnimationFrame(function () { item.style.opacity = "1"; });
            const timer = setTimeout(close, ms || TOAST_MS);
            item.addEventListener("click", close);
            function close() {
                clearTimeout(timer);
                item.style.opacity = "0";
                setTimeout(function () {
                    if (item.parentNode) item.parentNode.removeChild(item);
                }, 320);
            }
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
                _tpslReset();  // S3：关双窗清空止盈止损推演
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
                const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号
                fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + freq, { cache: "no-store" })
                    .then(resp => {
                        if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                        return resp.json();
                    })
                    .then(data => {
                        if (_isChartActionStale(_seq)) return; // [N1] 已被更新的操作抢先，丢弃过期响应
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
                        if (_isChartActionStale(_seq)) return; // [N1] 过期请求的失败不得覆盖新状态
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
                // 底部指标区槽位 chip：单击切指标（与双击同一套上下文切换）
                subCanvas.addEventListener("click", function(e) {
                    if (!dualSubData) return;
                    // 同单窗：双击的第二次点击不重复切换
                    if (e.detail > 1) return;
                    const rect = subCanvas.getBoundingClientRect();
                    const _scCanvas = canvas, _scCtx = ctx, _scChartData = chartData;
                    canvas = subCanvas; ctx = subCtx; chartData = dualSubData;
                    try {
                        const slot = hitBottomSlotChip(e.clientX - rect.left, e.clientY - rect.top);
                        if (slot >= 0) cycleBottomSlot(slot);
                    } finally {
                        canvas = _scCanvas; ctx = _scCtx; chartData = _scChartData;
                    }
                });
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
                    try {
                    const area = getChartArea();
                    // 底部指标区：切换入口是标签行 chip，这里只做命中拦截
                    const bottomTop = getBottomSlotLabelArea(0).y;
                    const bottomBottom = getBottomAreaBottomY();
                    if (clickX >= area.x && clickX <= area.x + area.w &&
                        clickY >= bottomTop && clickY <= bottomBottom) {
                        return;
                    }
                    const klines = getVisibleKlines();
                    if (!klines.length) { return; }
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
                    // 下窗双击选点（三期独立选点）：命中K线 → 笔定位 → 选点请求
                    // （freq=下窗周期，后端按焦点窗落列并双窗重建；响应含 data.sub，
                    //  上窗重载=区间套基于新下窗笔重算，见方案 §4.4）
                    if (clickedOnKline) {
                        // 焦点窗跟随双击所在窗（与上窗分支对称）：下窗双击选点
                        // 即以下窗为焦点，供「取消选点」按焦点窗清列——此前只有
                        // 滚轮/拖拽会置焦点，双击下窗选点后直接取消会清上窗列。
                        activeDualWindow = 'sub';
                        updateActiveWindowClass();
                        // 复盘态选点四场景全放开：下窗选点=改下窗 L，R 保持复盘点
                        // 笔定位：双击K线日期 == 某笔edt == 下一笔sdt（与上窗同款匹配）
                        const subKline = (clickedGlobalIdx >= 0 && chartData.klines && clickedGlobalIdx < chartData.klines.length)
                            ? chartData.klines[clickedGlobalIdx] : null;
                        let subBiIdx = -1;
                        if (subKline && chartData.bis && chartData.bis.length > 1) {
                            const dStr = subKline.date;
                            for (let j = 0; j < chartData.bis.length - 1; j++) {
                                if (chartData.bis[j].edt === dStr && chartData.bis[j + 1].sdt === dStr) {
                                    subBiIdx = j + 1;
                                    break;
                                }
                            }
                        }
                        if (subBiIdx < 0) return;  // 非笔边界K线：无效双击，静默
                        const _subCode = chartData.meta.symbol;
                        const _subFreq = dualSubFreq;
                        // 上窗周期必须取 _savedFreq：本块处于「下窗替换态」
                        // （chartData=dualSubData、currentFreq=下窗周期），取 currentFreq
                        // 会把下窗周期当上窗周期传给后端；后端按 main_freq 重建上窗，
                        // 同周期配对会被 _validate_stock_dual_pair 拒绝 ⇒ 选点必失败
                        const _mainFreq = _savedFreq;
                        const _subIsFutures = chartData.meta.market === 'futures';
                        // 引擎运行中拦截（期货域）：选点写 CSV，影响引擎窗口
                        if (_subIsFutures && autoOrderRunning && isFuturesMode()) {
                            showAlert('交易引擎运行中，请先关闭，再选点');
                            return;
                        }
                        document.getElementById("loading").classList.remove("hidden");
                        document.querySelector(".loading-text").textContent = "正在手选进入段...";
                        const _seq = _bumpChartActionSeq();
                        // 复盘态选点：带当前复盘点（重建窗口 [新选点, 复盘点]，R 不变）
                        const _subReplayEnd = (chartData.meta && chartData.meta.is_replay && chartData.klines && chartData.klines.length > 0)
                            ? "&end_date=" + encodeURIComponent(inputDateToApi(klineDateToInput(chartData.klines[chartData.klines.length - 1].date, _subFreq), _subFreq))
                            : "";
                        // 视图左边界 L（期货专用，与上窗同款）：本块处于下窗替换态
                        // （chartData = dualSubData），首根即下窗视图 L。
                        const _subViewStart = (_subIsFutures && chartData.klines && chartData.klines.length > 0)
                            ? "&start_time=" + encodeURIComponent(inputDateToApi(klineDateToInput(chartData.klines[0].date, _subFreq), _subFreq))
                            : "";
                        const _selectUrl = _subIsFutures
                            ? "/api/futures/" + encodeURIComponent(_subCode) + "/select/point?freq=" + _subFreq + "&bi_idx=" + subBiIdx + _subReplayEnd + _subViewStart
                            : "/api/stocks/" + encodeURIComponent(_subCode) + "/select/point?freq=" + _subFreq
                              + "&bi_idx=" + subBiIdx + _subReplayEnd + "&dual=1&main_freq=" + _mainFreq + "&sub_freq=" + _subFreq;
                        fetch(_selectUrl, { method: "POST" })
                            .then(resp => {
                                if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "手选失败"); });
                                return resp.json();
                            })
                            .then(data => {
                                if (_isChartActionStale(_seq)) return;
                                if (data.error) throw new Error(data.error);
                                if (_subIsFutures) {
                                    // 期货下窗选点：响应为下窗单窗快照（校验用），
                                    // 双窗数据靠重连拉取（后端 CSV 恢复两窗选点）。
                                    // 注意：本块处于下窗替换态（chartData=dualSubData、
                                    // currentFreq=下窗周期），上窗周期用捕获的 _savedFreq；
                                    // 复盘态重连带 end_time（R 保持复盘点）
                                    const _subReplayEnd2 = (chartData.meta && chartData.meta.is_replay)
                                        ? inputDateToApi(klineDateToInput(chartData.klines[chartData.klines.length - 1].date, _subFreq), _subFreq)
                                        : null;
                                    connectRealtimeDual(_subCode, _savedFreq, dualSubFreq, _subReplayEnd2);
                                    return;
                                }
                                chartData = data;                      // 上窗新数据（区间套基于新下窗重算）
                                if (data.sub) { dualSubData = data.sub; }  // 下窗 = [新选点, 最新]
                                adjustViewForSavedPoint();
                                document.getElementById("stock-name").textContent = chartData.meta.name;
                                document.getElementById("stock-code").textContent = chartData.meta.symbol;
                                document.title = "缠论分析 - " + chartData.meta.name;
                                if (chartData.klines.length > 0) {
                                    document.getElementById("goto-date-input").value = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                                }
                                updateWeekday();
                                document.getElementById("loading").classList.add("hidden");
                                updateRestartBtn();
                                updateDualBtn();
                                resizeCanvas();
                                render();
                                renderBottom();
                                generateStats();
                                loadAnnotations();
                            })
                            .catch(err => {
                                if (_isChartActionStale(_seq)) return;
                                document.getElementById("loading").classList.add("hidden");
                                showAlert(err.message);
                            });
                        return;
                    }
                    } finally {
                        // 无论正常结束还是提前 return / 异常，全局视口状态必定还原，
                        // 杜绝"漏写 restore 导致上窗 viewOffset/viewCount 被下窗值污染"的脆弱点
                        canvas = _savedCanvas; ctx = _savedCtx;
                        viewOffset = _savedViewOffset; viewCount = _savedViewCount;
                        chartData = _savedChartData; currentFreq = _savedFreq;
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
                            showToast("请加载更多K线...");
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
                    const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号
                    fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + currentFreq
                        + "&dual=1&sub_freq=" + dualSubFreq)
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
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
                            if (_isChartActionStale(_seq)) return; // [N1] 过期请求的失败不得回滚当前状态
                            showAlert("加载下面窗口数据失败: " + err.message);
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
            try {
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
                if (newViewCount === viewCount) { return; }
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
            } finally {
                canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
            }
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
                try {
                dualSubViewOffset = dualSubDragStartOffset - (e.clientX - dualSubDragStartX) / (getChartArea().w / viewCount);
                dualSubViewOffset = Math.max(0, Math.min(dualSubData.klines.length - dualSubViewCount, dualSubViewOffset));
                } finally {
                    canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
                }
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
                    showToast("区间起点: " + startDate + "，点击另一根K线完成选择");
                    renderBottom();
                } else {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showToast("区间选择已取消");
                    renderBottom();
                }
                return;
            }

            // === 普通点击：如果在选择模式中，完成区间选择（底部窗口）===
            if (_rangeSelect.mode === 'SELECTED_A') {
                // 验证：同一股票、同一周期
                if (_rangeSelect.startFreq !== dualSubFreq || _rangeSelect.startSymbol !== dualSubData.meta.symbol) {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showToast("股票或周期已变更，区间选择已取消");
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
                showToast("已复制 " + (b - a + 1) + " 根K线数据到剪贴板");
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
        // （复盘态已放开：meta 恒回显 CSV 真值，复盘态选点后菜单点亮；
        //   取消 = 改L回方式A左边界，R保持复盘点——请求带 end_date。
        //   三期双窗：meta 双字段（上窗 saved_selection_date / 下窗
        //   sub_saved_selection_date），任一窗有选点即亮，点击清焦点窗列）
        function updateRestartBtn() {
            var hasPoint = chartData && chartData.meta &&
                (chartData.meta.saved_selection_date || chartData.meta.sub_saved_selection_date);
            _restartEnabled = hasPoint;
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
                // 股票：w/d/30m 可双窗口；15m/5m 不可（15m/5m 不单独作为主窗切双窗）
                document.getElementById("btn-dual").disabled = (currentFreq === '15m' || currentFreq === '5m');
            }
        }

        // ============================================================
        // 重启：清除选点，按冷启动重新加载
        // ============================================================
        window.cancelSelectedPoint = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!chartData || !chartData.meta) return;
            // 双窗取消选点（三期/四期独立选点）：清焦点窗周期列（对齐单窗语义）；
            // 复盘态全放开（重载后端按 CSV 恢复各窗 L，焦点列已清=方式A）
            const isFutures = chartData.meta.market === 'futures';
            const code = chartData.meta.symbol;
            // 焦点窗周期：双窗下窗焦点=下窗周期，其余=上窗周期（只用于清列）
            const freq = (isDualWindow && activeDualWindow === 'sub' && dualSubFreq) ? dualSubFreq : currentFreq;
            // 重载周期：双窗恒用上窗周期——清列与重载是两个动作，重载若按焦点窗
            // 周期（下窗周期）请求，analyze 会返回下窗单窗快照、currentFreq 被改写
            // 成下窗周期，双窗整体降一级（getDualSubFreq(下窗周期) 无配对可取）。
            const _loadFreq = isDualWindow ? currentFreq : freq;
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在重置...";

            // 期货：清除选点 + 冷启动重连SSE（无start_time）
            const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号（期货/股票两分支共用）
            if (isFutures) {
                // 引擎运行中拦截：取消选点清 CSV，影响引擎下次重连的行情窗口（四类拦截之一）
                if (autoOrderRunning && isFuturesMode()) {
                    showAlert('交易引擎运行中，请先关闭，再取消选点');
                    return;
                }
                fetch("/api/futures/" + encodeURIComponent(code) + "/delete/point?freq=" + freq, { method: "DELETE" })  // 四期：双窗下 freq=焦点窗周期
                    .then(resp => resp.json())
                    .then(() => {
                        if (_isChartActionStale(_seq)) return; // [N1] 过期重置不得重连SSE（会掐断新操作的实时流）
                        // 不隐藏loading，交给connectRealtimeInit的init事件来隐藏
                        // 如果提前隐藏loading，会导致SSE重连失败时没有任何加载反馈
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        // 复盘态取消选点：保持复盘态（无选点窗口 [复盘点-N, 复盘点]）；
                        // 实时态：冷启动重连（原语义）
                        // 复盘点取上窗末根（chartData 恒为上窗数据）：按下窗周期
                        // 换算会把上窗日期截断成错粒度。
                        const replayEnd3 = (chartData.meta.is_replay && chartData.klines && chartData.klines.length > 0)
                            ? inputDateToApi(klineDateToInput(chartData.klines[chartData.klines.length - 1].date, _loadFreq), _loadFreq)
                            : null;
                        // 双窗：重连双窗 SSE（主窗周期 + 下窗周期），焦点列已清由后端
                        // 从 CSV 恢复；按焦点窗周期重连会把双窗降成下窗单窗。
                        if (isDualWindow && dualSubFreq) {
                            connectRealtimeDual(code, _loadFreq, dualSubFreq, replayEnd3);
                        } else {
                            connectRealtimeInit(code, _loadFreq, null, replayEnd3);
                        }
                    })
                    .catch(err => {
                        if (_isChartActionStale(_seq)) return; // [N1] 过期请求的失败不得弹窗打断新状态
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        showAlert("重置失败: " + err.message);
                    });
                return;
            }

            // 股票：清除选点 + 冷启动HTTP（复盘态：带 end_date 保持复盘态，R=复盘点不变）
            // Step 1: 调用后端清除CSV中该周期选点
            // （_seq 已在期货分支前声明，两分支共用）
            fetch("/api/stocks/" + encodeURIComponent(code) + "/delete/point?freq=" + freq, { method: "DELETE" })
                .then(resp => resp.json())
                .then(() => {
                    // Step 2: 冷启动重新加载（P2：股票双窗也显式透传 sub_freq）
                    const replayEndQuery = (chartData.meta.is_replay && chartData.klines && chartData.klines.length > 0)
                        ? "&end_date=" + encodeURIComponent(inputDateToApi(klineDateToInput(chartData.klines[chartData.klines.length - 1].date, _loadFreq), _loadFreq))
                        : "";
                    return fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + _loadFreq + replayEndQuery + (isDualWindow && getDualSubFreq(_loadFreq) ? "&dual=1" : "") + (isDualWindow && dualSubFreq && freqLevel(_loadFreq) > freqLevel(dualSubFreq) ? "&sub_freq=" + dualSubFreq : ""));
                })
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "重置失败"); });
                    return resp.json();
                })
                .then(data => {
                    if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
                    // 全文替换 chartData
                    chartData = data;
                    if (chartData.meta.freq === "5分钟") {
                        currentFreq = "5m";
                    } else if (chartData.meta.freq === "30分钟") {
                        currentFreq = "30m";
                    } else if (chartData.meta.freq === "15分钟") {
                        currentFreq = "15m";
                    } else if (chartData.meta.freq === "周线") {
                        currentFreq = "w";
                    } else {
                        currentFreq = "d";
                    }
                    updateDateInputType();
                    document.getElementById("btn-d").classList.toggle("active", currentFreq === "d");
                    document.getElementById("btn-w").classList.toggle("active", currentFreq === "w");
                    document.getElementById("btn-30m").classList.toggle("active", currentFreq === "30m");
                    document.getElementById("btn-15m").classList.toggle("active", currentFreq === "15m");
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
                    // （三期：下窗有选点时全量显示——sub.meta.saved_selection_date）
                    if (isDualWindow && data.sub) {
                        dualSubData = data.sub;
                        dualSubViewCount = (dualSubData.meta && dualSubData.meta.saved_selection_date)
                            ? dualSubData.klines.length : VIEW_COUNT;
                        dualSubViewOffset = Math.max(0, dualSubData.klines.length - dualSubViewCount);
                        if (dualSubData.klines.length < dualSubViewCount) {
                            dualSubViewOffset = 0;
                        }
                    }
                })
                .catch(err => {
                    if (_isChartActionStale(_seq)) return; // [N1] 过期请求的失败不得弹窗打断新状态
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    showAlert("重置失败: " + err.message);
                });
        };




// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] NavToolbar —— 导航工具栏组件（频率切换 / 日期跳转 / 坐标系统）

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

        // 周期级别：数值越大级别越高（w=7 > d=6 > 30m=5 > 15m=4 > 5m=3 > 1m=2 > 15s=1）
        // 用于双窗口校验：下窗周期级别必须严格小于上窗周期级别
        function freqLevel(freq) {
            const levels = {'w': 7, 'd': 6, '30m': 5, '15m': 4, '5m': 3, '1m': 2, '15s': 1};
            return levels[freq] || 0;
        }

        // 周期中文标签（用于弹窗提示）
        function freqLabel(freq) {
            const labels = {'w': '周线', 'd': '日线', '30m': '30分钟', '15m': '15分钟', '5m': '5分钟', '1m': '1分钟', '15s': '15秒'};
            return labels[freq] || freq;
        }

        // 根据市场类型更新频率按钮的启用/禁用状态
        function updateFreqButtonStates(isFutures) {
            // 按市场折叠按钮（腾出顶部空间，用户要求）：
            //   股票: 周K/日K/30分/15分/5分；期货: 30分/5分/1分/15秒
            const SHOW = {'w': !isFutures, 'd': !isFutures, '30m': true,
                          '15m': !isFutures, '5m': true, '1m': isFutures,
                          '15s': isFutures};
            for (const f in SHOW) {
                const el = document.getElementById('btn-' + f);
                if (el) el.style.display = SHOW[f] ? '' : 'none';
            }
            // 股票禁用 1m/15s，期货禁用 d/w
            document.getElementById('btn-d').disabled = isFutures;
            document.getElementById('btn-w').disabled = isFutures;
            document.getElementById('btn-1m').disabled = !isFutures;
            document.getElementById('btn-15s').disabled = !isFutures;
            // 共享周期：30m 始终启用
            document.getElementById('btn-30m').disabled = false;
            // 15m：股票周期（股票单窗/双窗可用；期货无 15m，禁用）
            document.getElementById('btn-15m').disabled = isFutures;
            // 5m: 期货双窗口→全部启用（上下窗解耦）；股票双窗口→按焦点窗口（配对放宽）
            if (isDualWindow && isFutures) {
                document.getElementById('btn-5m').disabled = false;
                document.getElementById('btn-15m').disabled = true;
                // 期货双窗口：上下窗独立切换，15s不再禁用
            } else if (isDualWindow && !isFutures) {
                if (activeDualWindow === 'sub') {
                    // 股票下窗焦点：仅启用当前上窗配对空间内的周期（w 不可作下窗）
                    const subs = STOCKS_DUAL_PAIRS_JS[currentFreq] || [];
                    document.getElementById('btn-w').disabled = true;
                    document.getElementById('btn-d').disabled = subs.indexOf('d') < 0;
                    document.getElementById('btn-30m').disabled = subs.indexOf('30m') < 0;
                    document.getElementById('btn-15m').disabled = subs.indexOf('15m') < 0;
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
            // 交易引擎运行中禁止切换周期（仅期货页）：运行中的引擎绑定旧周期的 SSE 流，
            // 直接切换会与引擎状态错配，须先关闭自动下单。
            // 交易引擎是期货域功能，股票页切周期与引擎无关，不受此限制。
            if (autoOrderRunning && isFuturesMode()) {
                showAlert('交易引擎运行中，请先关闭，再切周期');
                return;
            }
            const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            // 期货双窗口下窗焦点：独立切换下窗周期，不联动上窗
            if (isDualWindow && activeDualWindow === 'sub' && isFutures) {
                if (dualSubFreq === freq) return;
                // 校验：下窗周期必须严格小于上窗周期，否则弹窗提示并取消
                if (freqLevel(freq) >= freqLevel(currentFreq)) {
                    showAlert("下窗周期必须小于上窗周期，当前上窗周期为" + freqLabel(currentFreq)
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
                    showAlert("下窗周期配对无效: " + freqLabel(currentFreq) + "+" + freqLabel(freq)
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
                    const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号
                    fetch("/api/stocks/" + encodeURIComponent(code2) + "/analyze?freq=" + currentFreq
                        + "&dual=1&sub_freq=" + dualSubFreq)
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
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
                            if (_isChartActionStale(_seq)) return; // [N1] 过期请求的失败不得弹窗打断新状态
                            showAlert("切换下窗周期失败: " + err.message);
                            document.getElementById("loading").classList.add("hidden");
                        });
                }
                return;
            }
            if (currentFreq === freq) return;
            // 期货双窗口校验：上窗周期必须严格大于下窗周期，否则弹窗提示并取消
            if (isDualWindow && isFutures && freqLevel(freq) <= freqLevel(dualSubFreq)) {
                showAlert("上窗周期必须大于下窗周期，当前下窗周期为" + freqLabel(dualSubFreq)
                    + "，无法切换到" + freqLabel(freq));
                return;
            }
            // 股票双窗口（配对放宽）：新上窗周期无任何下窗可选 → 拒绝切换
            // （5m 为股票最小周期）；当前下窗仍为合法配对则保持，否则回退默认配对
            if (isDualWindow && !isFutures) {
                if (!STOCKS_DUAL_PAIRS_JS[freq]) {
                    showAlert("上窗周期必须大于下窗周期，" + freqLabel(freq) + "为股票最小周期，双窗口下不可选");
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
                const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号
                fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + freq
                    + (isDualWindow && getDualSubFreq(freq) ? "&dual=1" : "")
                    + (isDualWindow && dualSubFreq && freqLevel(freq) > freqLevel(dualSubFreq) ? "&sub_freq=" + dualSubFreq : ""))
                    .then(resp => {
                        if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                        return resp.json();
                    })
                    .then(data => {
                        if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
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
                        if (_isChartActionStale(_seq)) return; // [N1] 过期请求的失败不得弹窗打断新状态
                        showAlert("切换周期失败: " + err.message);
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
            // ═══ 复盘窗口 [L, R] 锚点（期货/股票共用，2026-10-03 二期扩展到期货）═══
            const isDualCtx = isDualWindow && getDualSubFreq(freq);
            const hasKlines = chartData.klines && chartData.klines.length > 0;
            const replayMode = !!(chartData.meta && chartData.meta.is_replay);
            const apiFirst = hasKlines ? inputDateToApi(klineDateToInput(chartData.klines[0].date, freq), freq) : "";
            const apiLast = hasKlines ? inputDateToApi(klineDateToInput(chartData.klines[chartData.klines.length - 1].date, freq), freq) : "";
            // 双窗下窗首根（三期独立选点）：下窗复盘窗口左边界
            const apiFirstSub = (isDualWindow && dualSubData && dualSubData.klines && dualSubData.klines.length > 0)
                ? inputDateToApi(klineDateToInput(dualSubData.klines[0].date, dualSubFreq), dualSubFreq) : "";
            const min15 = function(s) { return s.slice(0, 16); }; // 分钟粒度：15s周期首根含秒，避免秒位差异误判
            const isFutureDate = dateStr.slice(0, 10) > todayStr; // 输入框格式YYYY-MM-DD前缀，字典序即时间序
            // 回实时/回最新钳位：非复盘态末根=数据源最新，≥末根即回；
            // 复盘态末根=复盘点<最新，只拦今天/未来（回实时），(复盘点,最新) 内放行往右复盘
            const wantLive = isFutures && (!replayMode ? dateStr >= lastKlineInput
                : (isToday || isFutureDate));
            if (wantLive) {
                // 复盘态回实时 = 取消复盘：引擎运行中拦截（四类拦截之一）
                if (autoOrderRunning && isFuturesMode()) {
                    showAlert('交易引擎运行中，请先关闭，再取消复盘');
                    return;
                }
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
                // 引擎运行中拦截：复盘态选点/取消选点写 CSV，影响引擎窗口（四类拦截之一）
                if (autoOrderRunning && isFuturesMode()) {
                    showAlert('交易引擎运行中，请先关闭，再复盘');
                    return;
                }
                // 复盘越界：日历输入早于窗口左边界 L → 弹窗（右键复盘至此天然在区间内）
                // 双窗（四期）：两窗 L 各自冻结，任一窗越界即拦——判定基准 = max(L_main, L_sub)
                const _futReplayFloor = (isDualWindow && apiFirstSub && apiFirstSub > apiFirst) ? apiFirstSub : apiFirst;
                if (apiFirst && min15(apiDate) < min15(_futReplayFloor)) {
                    showAlert("复盘日期 " + apiDate + " 早于已加载数据起点 " + _futReplayFloor + "，请扩大数据范围。");
                    return;
                }
                document.getElementById("goto-date-input").disabled = true;
                document.getElementById("loading").classList.remove("hidden");
                document.querySelector(".loading-text").textContent = "正在复盘计算，请稍候...";
                if (isDualWindow && dualSubFreq) {
                    // 双窗复盘：两窗 L 各自冻结（双 start），R 共享=复盘点
                    connectRealtimeDual(chartData.meta.symbol, freq, dualSubFreq, apiDate, apiFirst, apiFirstSub);
                } else {
                    // 复盘继承选点：start=当前窗口首根（左边界L），后端 CSV 恢复兜底
                    connectRealtimeInit(chartData.meta.symbol, freq, apiFirst || realtimeStartTime, apiDate);
                }
                return;
            }
            // 股票：isToday安全网只给日历"今天"用（Edge时间未变时兜底）
            // 键盘Enter/右键复盘至此有精确日期 → 跳过isToday安全网，始终传end_date
            // ═══ 股票复盘窗口：atLatest 钳位（锚点已提前，期货/股票共用）═══
            // ≥最新K线（非复盘态末根=数据源最新）或今天/未来 → 钳到「回最新」：不带end_date冷启动，
            // 与「选今天」同路；复盘态末根=复盘点<最新，往右复盘必须放行走end_date
            const atLatest = hasKlines && (isToday || isFutureDate
                || (!replayMode && min15(apiDate) >= min15(apiLast)));
            const needEndDate = (!isToday || keyEnter) && !atLatest;
            // 复盘日期早于窗口左边界 → 弹窗拦截（右键只能命中可见K线，此拦截只对日历输入可达）
            // 双窗（三期）：两窗 L 各自冻结，任一窗越界即拦——判定基准 = max(L_main, L_sub)
            const _replayFloor = (isDualCtx && apiFirstSub && apiFirstSub > apiFirst) ? apiFirstSub : apiFirst;
            if (needEndDate && apiFirst && min15(apiDate) < min15(_replayFloor)) {
                showAlert("复盘日期 " + apiDate + " 早于已加载数据起点 " + _replayFloor + "，请扩大数据范围。");
                return;
            }
            const url = "/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + freq
                + (needEndDate ? "&end_date=" + encodeURIComponent(apiDate) : "")
                + (needEndDate && apiFirst ? "&start_time=" + encodeURIComponent(apiFirst) : "")
                + (needEndDate && isDualCtx && apiFirstSub ? "&sub_start_time=" + encodeURIComponent(apiFirstSub) : "")
                + (isDualWindow && getDualSubFreq(freq) ? "&dual=1" : "")
                + (isDualWindow && dualSubFreq && freqLevel(freq) > freqLevel(dualSubFreq) ? "&sub_freq=" + dualSubFreq : "");
            document.getElementById("goto-date-input").disabled = true;
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在复盘计算，请稍候...";
            const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号
            fetch(url)
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "跳转失败"); });
                    return resp.json();
                })
                .then(data => {
                    if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
                    chartData = data;
                    updateRestartBtn();
                    updateDualBtn();
                    // 双窗口模式：从 data.sub 恢复子级别数据
                    // （三期：下窗有选点时全量显示——sub.meta.saved_selection_date）
                    if (isDualWindow && data.sub) {
                        dualSubData = data.sub;
                        dualSubViewCount = (dualSubData.meta && dualSubData.meta.saved_selection_date)
                            ? dualSubData.klines.length : VIEW_COUNT;
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
                    if (_isChartActionStale(_seq)) return; // [N1] 过期请求的失败不得弹窗打断新状态
                    showAlert("跳转失败: " + err.message);
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




// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] SymbolSearch —— 证券搜索组件（搜索 / 历史 / 代码加载）

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
                        positionStockHistory();
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
                    positionStockHistory();
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

        // 搜索历史/搜索结果下拉：改为 fixed 后必须按输入框视口坐标定位，
        // 否则 .header 的 overflow 会裁掉它（见 app.css .stock-history 注释）。
        function positionStockHistory() {
            const input = document.getElementById("stock-code-input");
            const el = document.getElementById("stock-history");
            if (!input || !el) return;
            const r = input.getBoundingClientRect();
            el.style.left = r.left + "px";
            el.style.top = (r.bottom + 2) + "px";   // 紧贴输入框下方
        }
        // 窗口尺寸变化（含窄屏响应式断点切换）时，若下拉已展开则重新对齐
        window.addEventListener("resize", function() {
            const el = document.getElementById("stock-history");
            if (el && el.classList.contains("show")) positionStockHistory();
        });

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
            positionStockHistory();
            el.classList.add("show");
        };

        window.loadStock = function() {
            const code = document.getElementById("stock-code-input").value.trim();
            if (!code) return;
            _tpslReset();  // S3：切代码/切周期/复盘/市场切换清空止盈止损推演
            // 交易引擎运行中禁止切换合约（仅期货页）：运行中的引擎绑定旧合约的持仓/SSE 流，
            // 直接切换会与引擎状态错配（切合约对账），须先关闭自动下单。
            // 交易引擎是期货域功能，股票页切合约与引擎无关，不受此限制。
            if (autoOrderRunning && isFuturesMode()) {
                showAlert('交易引擎运行中，请先关闭，再切合约');
                return;
            }
            // 切换股票时取消区间选择
            if (_rangeSelect.mode === 'SELECTED_A') {
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
            }
            document.getElementById("stock-history").classList.remove("show");
            document.getElementById("loading").classList.remove("hidden");
            // ⚠️ 本表必须与后端 DataAPI/TqSdkAPI.FUTURES_ALIASES **键集一致**
            //   （收窄到 16 品种；原为 83 条全表）。
            //   用途：判定"用户敲的短代码是不是期货"。只改一边会出现
            //   「本表命中 → 认作期货 → connectRealtimeInit → 后端解析不出代码」的空转。
            //   契约测试：Trading/Test/test_p47_product_case_whitelist.py [4] 段
            //   （逐键比对本表 ⇔ FUTURES_ALIASES，改漏即打红）。
            //   注意：这不是"可交易清单"—— 可自动下单的只有 8 个品种，
            //   由后端 /api/trader/product-check 判定，前端不复制白名单。
            const FUTURES_ALIAS_KEYS = new Set(["IF","IH","IC","IM","AU","AG","CU","RB","M","P","JM","LH","TA","PTA","MA","SC","LC"]);
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
            const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号
            fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + fetchFreq
                + (isDualWindow && getDualSubFreq(fetchFreq) ? "&dual=1" : "")
                + (isDualWindow && reqSubFreq ? "&sub_freq=" + reqSubFreq : ""))
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                    return resp.json();
                })
                .then(data => {
                    if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
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
                    } else if (data.meta.freq === "15分钟") {
                        returnedFreq = "15m";
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
                    if (_isChartActionStale(_seq)) return; // [N1] 过期请求的失败不得弹窗打断新状态
                    showAlert("查询失败: " + err.message);
                    document.getElementById("loading").classList.add("hidden");
                });
        };

        window.selectHistory = function(code) {
            document.getElementById("stock-code-input").value = code;
            document.getElementById("stock-history").classList.remove("show");
            window.loadStock();
        };




// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] StatsPanel —— 统计面板组件（左侧信息 / 复盘滑块联动）

// ══════════════════════════════════════════════════════════════════
        window.toggleStats = function() {
            // 股票态：同一颗按钮的语义是「回测」（§4.1）—— 统计面板读的是期货自动
            // 下单的成交账本（state.db），股票态本就没有可看的东西（§4.2 / Q7）。
            if (!isFuturesMode()) { toggleBacktestPanel(); return; }
            var panel = document.getElementById("stats-panel");
            if (!panel) return;
            if (panel.classList.contains("show")) {
                closeStatsPanel();
            } else {
                panel.classList.add("show");
                generateStats(true);  // 打开时强制刷新一次（要最新数据）；render() 热路径保持 force=false 走缓存
            }
        };

        window.closeStatsPanel = function() {
            var panel = document.getElementById("stats-panel");
            if (panel) panel.classList.remove("show");
        };

        // 打开面板后点击面板之外区域 → 自动关闭（与「市场量能」面板同款 mousedown 语义）
        // 两个面板共用同一条监听：它们占同一屏位、且同一时刻只可能有一个是 show
        // （市场态一变 syncStatsButtonLabel 就把另一个收掉）。
        document.addEventListener("mousedown", function(e) {
            var btn = document.getElementById("btn-stats");
            if (btn && btn.contains(e.target)) return;   // 点击按钮本身 → 交给 toggleStats 处理
            var pairs = [["stats-panel", closeStatsPanel], ["bt-panel", closeBacktestPanel]];
            for (var i = 0; i < pairs.length; i++) {
                var panel = document.getElementById(pairs[i][0]);
                if (!panel || !panel.classList.contains("show")) continue;
                if (panel.contains(e.target)) continue;  // 点击面板内部 → 不关闭
                pairs[i][1]();
            }
        });

        // ── 成交统计：与图表重绘解耦 ────────────────────────────────────
        // 旧「缠论统计」依赖可见区间(getVisibleKlines)，必须每次重绘重算，故挂在 render() 热路径上；
        // 新「成交统计」只依赖 symbol + state.db，与视图无关。若仍留在热路径里「先清空 → 再请求 → 再写回」，
        // 则每次重绘（鼠标移动 app.js:2699 / 1 秒倒计时 2628 / 缩放）都会制造一次可见闪断 → 面板「一闪一闪」。
        // 三道护栏：①结果按品种缓存 ②在途请求去重 + 过期响应丢弃 ③内容不变则零 DOM 写。
        var _tradeStatsCache = { symbol: null, data: null, html: null, seq: 0, inflight: false };
        var _TRADE_STATS_LOADING = '<div class="stats-loading" style="padding:8px;color:#a8b2d1;">加载成交统计…</div>';
        var _TRADE_STATS_NO_SYMBOL = '<div class="stats-row"><span class="stats-value">无品种上下文</span></div>';

        // 内容未变则不触碰 DOM —— 这是消除闪烁的关键。
        function _writeStatsHtml(html) {
            var box = document.getElementById("stats-content");
            if (!box) return false;
            if (_tradeStatsCache.html === html) return false;
            box.innerHTML = html;
            _tradeStatsCache.html = html;
            return true;
        }

        // force=true 只在「打开面板」时用（要最新数据）；render() 热路径一律 force=false → 命中缓存，零请求零闪烁。
        function generateStats(force) {
            var panel = document.getElementById("stats-panel");
            if (!panel || !panel.classList.contains("show")) return;
            if (!chartData || !chartData.meta || !chartData.meta.symbol) {
                _writeStatsHtml(_TRADE_STATS_NO_SYMBOL);
                return;
            }
            var symbol = chartData.meta.symbol;
            var cached = (_tradeStatsCache.symbol === symbol) ? _tradeStatsCache.data : null;
            if (!force && cached) {          // 命中缓存：同步渲染，不发请求、不显示「加载中」
                renderTradeStats(cached);
                return;
            }
            if (_tradeStatsCache.inflight && _tradeStatsCache.symbol === symbol) return;  // 在途请求去重
            var seq = ++_tradeStatsCache.seq;
            _tradeStatsCache.inflight = true;
            _tradeStatsCache.symbol = symbol;
            if (!cached) _writeStatsHtml(_TRADE_STATS_LOADING);   // 仅「该品种首屏无缓存」才显示占位
            var url = "/api/trader/trades?symbol=" + encodeURIComponent(symbol) + "&union=true";
            fetch(url, { cache: "no-store" })
                .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)); })
                .then(function (d) {
                    if (seq !== _tradeStatsCache.seq) return;     // 过期响应丢弃，防旧结果覆盖新品种
                    _tradeStatsCache.symbol = symbol;
                    _tradeStatsCache.data = d;
                    _tradeStatsCache.inflight = false;
                    renderTradeStats(d);
                })
                .catch(function (err) {
                    if (seq !== _tradeStatsCache.seq) return;
                    _tradeStatsCache.inflight = false;
                    _writeStatsHtml('<div class="stats-row"><span class="stats-value">统计加载失败：'
                        + (err && err.message ? err.message : err) + '</span></div>');
                });
        }

        // 读库可见性：把「真的没有成交」与「库读不出来」分开显示。
        // 后端 read_errors 只含真故障（库损坏 / 旧 schema 无 trades 表 / 打不开），
        // 不含「文件还没生成」—— 后者是正常空态，dbs_scanned 会体现。
        function statsReadErrors(d) { return (d && d.read_errors) || []; }

        function statsEsc(s) {
            return String(s == null ? "" : s)
                .replace(/&/g, "&amp;").replace(/</g, "&lt;")
                .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
        }

        function statsReadWarningHtml(d) {
            var errs = statsReadErrors(d);
            if (!errs.length) return "";
            return '<div class="stats-row"><span class="stats-label" style="color:#FFB020">读取警告</span>'
                + '<span class="stats-value" style="color:#FFB020;font-size:11px">'
                + errs.length + ' 个库读取失败，汇总不完整</span></div>';
        }

        // 统计口径标识：统计与自动下单共用同一份「买卖点类型过滤」勾选，
        // 用户取消勾选的某类其历史成交不计入统计。这里显式标出当前汇总数字
        // 是基于哪几类算的，避免「勾一下就把历史胜率静默重算」而无人察觉。
        function statsCaliberHtml(d) {
            var incl = d.bsp_types_included || ["0", "1", "2", "3"];
            var excluded = ["0", "1", "2", "3"].filter(function (t) {
                return incl.indexOf(t) < 0;
            });
            if (excluded.length === 0) {
                return '<div class="stats-row" style="font-size:11px;color:#a8b2d1;'
                    + 'padding-left:2px;">统计口径：全部 0/1/2/3 类'
                    + '（未启用买卖点类型过滤）</div>';
            }
            return '<div class="stats-row" style="background:rgba(255,176,32,0.10);'
                + 'border-left:3px solid #FFB020;padding:4px 6px;margin-bottom:6px;'
                + 'font-size:11px;color:#FFB020;">统计口径：'
                + incl.join("/") + '类</div>';
        }

        // 数据来源（§3.10.5）：多实例下「合了哪几个库」必须可见，否则
        // 统计数字的构成无法解释。sources[].path 形如
        // ...\Trading\State\SimNow\IF\state.db —— 取 state.db 前两段
        // （登录方式/品种）作为显示名；解析不出就显示倒数第二段。
        function statsSourcesHtml(d) {
            var srcs = (d && Array.isArray(d.sources)) ? d.sources : [];
            if (!srcs.length) return '';
            var names = srcs.map(function (s) {
                var segs = String(s.path || '').split(/[\\/]/);
                var dbi = -1;
                for (var i = segs.length - 1; i >= 0; i--) {
                    if (segs[i] === 'state.db') { dbi = i; break; }
                }
                if (dbi >= 2) return segs[dbi - 2] + '/' + segs[dbi - 1];
                return segs.length >= 2 ? segs[segs.length - 2] : (segs[0] || '?');
            });
            var rows = srcs.map(function (s) { return Number(s.rows || 0); });
            var line = names.map(function (n, i) {
                return n + '（' + rows[i] + '笔）';
            }).join('、');
            return '<div class="stats-row" style="font-size:11px;color:#a8b2d1;'
                + 'padding-left:2px;">数据来源：' + line + '</div>';
        }

        function renderTradeStats(d) {
            var count = (d && d.count) || 0;
            var readErrs = statsReadErrors(d);
            if (!count) {
                // 品种键解析失败优先说 —— 此时"没有匹配"是因为请求本身不可解释，
                // 而不是"库里没成交"，两者混在一起会把真问题藏掉。
                if (d && d.symbol_raw && !d.symbol_key) {
                    _writeStatsHtml('<div class="stats-empty">无法从「'
                        + statsEsc(d.symbol_raw) + '」解析品种键，未做任何匹配</div>');
                    return;
                }
                if (readErrs.length) {
                    var lines = "";
                    for (var i = 0; i < readErrs.length; i++) {
                        lines += '<div class="stats-row"><span class="stats-label" style="font-size:11px">'
                            + statsEsc(readErrs[i].path) + '</span>'
                            + '<span class="stats-value" style="color:#FFB020;font-size:11px">'
                            + statsEsc(readErrs[i].error || readErrs[i].status) + '</span></div>';
                    }
                    _writeStatsHtml('<div class="stats-empty">该品种暂无历史成交，'
                        + '但以下 ' + readErrs.length + ' 个库读取失败 —— 此结果不可信：</div>'
                        + '<div class="stats-rows">' + lines + '</div>');
                    return;
                }
                if (d && d.dbs_scanned === 0) {
                    _writeStatsHtml('<div class="stats-empty">未找到 state.db（尚无自动下单记录）</div>');
                    return;
                }
                _writeStatsHtml(statsCaliberHtml(d)
                    + '<div class="stats-empty">该品种暂无历史成交</div>');
                return;
            }
            var pct = function (x) { return (x * 100).toFixed(1) + "%"; };
            var yuan = function (x) { return Number(x).toFixed(2) + " 元"; };
            // 期望值的量纲是「元/笔」：= 总净盈亏 ÷ 总交易笔数，除完剩下的就是
            // 每笔。与 yuan() 分开写，是为了让「存量」与「每笔」两类字段在字面上
            // 就能区分开 —— 光看 "516.00 元" 分不出是这一笔还是平均每笔。
            var yuanPer = function (x) { return Number(x).toFixed(2) + " 元/笔"; };
            // 总净盈亏是存量口径，能到 7 位数；核心区一行四格、每格只有 ~100px 宽，
            // 直接排 "3400000.00 元" 会把格子撑破。过万进「万元」、过百万进
            // 「百万元」，两级各缩 4 个数量级，最长也只 11 个字符。
            // 缩位规则**不在这里实现**：money() / moneyScale() 全局只有一份
            // （见下方「金额缩位」一节），盈亏曲线纵轴调的是同一个 ——
            // 各写一份必然漂移，同一个数会在面板与轴上各给一种单位。
            // 只有总净盈亏走这套：期望值 / 平均每笔 / 最大单笔都是「每笔」量级，
            // 把 5000 元写成 "0.50 万元" 反而读不出数。
            var col = function (x) { return x >= 0 ? "#FF3C3C" : "#00F0F0"; };  // 涨红跌绿
            var num = function (x) { return x != null ? Number(x).toFixed(2) : "—"; };
            var html = "";
            // 统计口径标识（置于最顶，先于曲线与核心数）：一眼看出当前汇总数字
            // 是基于哪几类买卖点算的（用户取消勾选的某类其历史成交已被排除）。
            html += statsCaliberHtml(d);
            // 数据来源行（§3.10.5）：多实例下合并了哪些实例目录必须可见。
            html += statsSourcesHtml(d);
            // ① 曲线在上（对齐「市场量能」的 amo-chart 位置）
            html += '<canvas id="trade-equity-canvas"></canvas>';
            // ② 一行四格核心数（对齐「市场量能」的 amo-stats）
            // 盈利因子（总盈 ÷ 总亏）与盈亏比同属「策略整体质量」口径，
            // 提到核心区与总净盈亏 / 实际胜率 / 盈亏比并列；明细区不再重复这一行。
            html += '<div class="stats-hero">';
            html += '<div class="stats-cell"><span class="stats-label">总净盈亏</span><span class="stats-value" style="color:' + col(d.total_net) + '">' + money(d.total_net) + '</span></div>';
            html += '<div class="stats-cell"><span class="stats-label">实际胜率</span><span class="stats-value">' + pct(d.win_rate) + '</span></div>';
            html += '<div class="stats-cell"><span class="stats-label">盈亏比(赔率)</span><span class="stats-value">' + num(d.pl_ratio) + '</span></div>';
            html += '<div class="stats-cell"><span class="stats-label">盈利因子</span><span class="stats-value">' + num(d.profit_factor) + '</span></div>';
            html += '</div>';
            // ③ 明细行
            html += '<div class="stats-rows">';
            // 品种：统计口径是「整个期货品种」（同品种多月份合并），所以这里只给
            // 品种键（IF / IH / AU…）。不列合约名 —— 一旦列出 CFFEX.IF2612 这种写法，
            // 「品种合计」就会被读成「某一个合约的战绩」。
            if (d.symbol_key) {
                html += '<div class="stats-row"><span class="stats-label">期货品种</span><span class="stats-value" style="font-size:11px;text-align:right">' + statsEsc(d.symbol_key) + '</span></div>';
            }
            html += '<div class="stats-row"><span class="stats-label">成交笔数</span><span class="stats-value">' + count + '（胜 ' + d.wins + ' / 亏 ' + d.losses + (d.flat ? ' / 平 ' + d.flat : '') + '）</span></div>';
            // 类型胜负：按买卖点类型（0/1/2/3 类）拆胜/亏笔数，紧跟「成交笔数」之后、
            // 期望值之前（用户拍板：一眼看出每类买卖点的盈亏数量分布）。类型取自后端
            // compute_trade_stats 算好的 by_bsp_type（signal_key 中段=类型）；组里只有
            // 胜/亏（平手不计入）。净方向标：胜>亏 标「正」、亏>胜 标「负」、
            // 持平不标 —— 方便一眼看出哪类是净亏来源。
            // 布局：四段各写成一个独立的 <span class="stats-bsp-seg">，父行
            // .stats-bsp-row 用 flex + justify-content:space-between ——
            // 0类贴左、3类贴右、中间 1类/2类 等间距分布（用户拍板：不要挤在
            // 左边）。**不要**退回"一个 span + 段间空格"的老写法：面板一窄，
            // 那几个空格就撑不住间距，四段会连成一串读不出来；靠 flex 均分则
            // 与面板宽度无关。弹窗也因此**不加宽**，保持 440px。
            var bbs = d.by_bsp_type || {};
            html += '<div class="stats-row stats-bsp-row">';
            for (var bt = 0; bt <= 3; bt++) {
                var bg = bbs[String(bt)] || {wins: 0, losses: 0};
                var btag = bg.wins > bg.losses ? "正" : (bg.losses > bg.wins ? "负" : "");
                html += '<span class="stats-bsp-seg">' + bt + '类' + btag + '(胜' + bg.wins + '/亏' + bg.losses + ')</span>';
            }
            html += '</div>';
            // 期望值 = win_rate*avg_win + loss_rate*avg_loss = 总净盈亏 ÷ 总笔数，
            // 量纲就是「元/笔」，所以数值后面必须缀上「/笔」—— 否则它与上面的
            // 总净盈亏只差一个数字，读的人无从判断哪个是总量、哪个是每笔。
            html += '<div class="stats-row"><span class="stats-label">期望值</span><span class="stats-value" style="color:' + col(d.expectancy) + '">' + yuanPer(d.expectancy) + '</span></div>';
            // 这两个数就是「盈亏比(赔率)」的两个分量（pl_ratio = avg_win / |avg_loss|），
            // 标签必须写明「每笔」—— 光写「平均盈利」会被读成总量口径，与盈利因子混淆。
            // 后半截只写「亏损」不重复「平均每笔」：主语已由前半截给出，再写一遍
            // 只是把标签撑长 —— 明细区一格放不下就折行，右侧的值会被挤走。
            html += '<div class="stats-row"><span class="stats-label">平均每笔盈利/亏损</span><span class="stats-value"><span style="color:#FF3C3C">' + yuan(d.avg_win) + '</span> / <span style="color:#00F0F0">' + yuan(d.avg_loss) + '</span></span></div>';
            // 最大单笔盈亏合成一行：这两个数本来就是一对（最好的单笔 / 最坏的单笔），
            // 合成一行后与上面「平均每笔盈利/亏损」同构，也省下一行高度。
            // 同样不给发生时间 —— 这一格回答的是"最好/最坏会到多少"，时间是复盘表格里的事。
            html += '<div class="stats-row"><span class="stats-label">最大单笔盈利/亏损</span><span class="stats-value"><span style="color:#FF3C3C">' + yuan(d.max_win.net_cash) + '</span> / <span style="color:#00F0F0">' + yuan(d.max_loss.net_cash) + '</span></span></div>';
            html += statsReadWarningHtml(d);
            html += '</div>';
            if (_writeStatsHtml(html)) drawEquityCurve(d.equity_curve || []);
        }

        // ── 金额缩位：不过万 → 元，过万 → 万元，过百万 → 百万元 ──
        // 唯一实现。统计面板的「总净盈亏」与盈亏曲线**纵轴刻度**都调这里：
        // 两处各写一份必然漂移，同一个数会在面板写"3.40 万元"、在轴上写"340万"，
        // 读的人会当成两个口径。
        function moneyScale(v) {
            var a = Math.abs(Number(v));
            if (!isFinite(a)) a = 0;
            if (a >= 1e6) return { div: 1e6, unit: "百万元" };
            if (a >= 1e4) return { div: 1e4, unit: "万元" };
            return { div: 1, unit: "元" };
        }

        // 单个金额 → 字面量（固定 2 位小数），面板字段用。
        // 纵轴刻度**不用**它：轴的小数位数要跟着刻度步长走 —— 步长 5000 时
        // "0.00 / 0.50 / 1.00 万元"多出来的那两位是纯噪声，见 eqAxisNum。
        function money(x) {
            var v = Number(x), sc = moneyScale(v);
            return (v / sc.div).toFixed(2) + " " + sc.unit;
        }

        // ── 盈亏曲线坐标：刻度 / 数字 / 日期 ──
        // 纵轴"好看刻度"：步长取 1/2/5×10^k，一来刻度值都是整数好读，二来
        // 0 必然落在刻度上 —— 0 是盈与亏的分界，必须能一眼对上。
        // want = 期望条数；实际会多出 ≤2 条，因为首尾要凑到整步长上。
        function eqAxisTicks(minV, maxV, want) {
            var span = maxV - minV;
            if (!(span > 0)) return [minV];
            var raw = span / Math.max(2, want);
            var mag = Math.pow(10, Math.floor(Math.log10(raw)));
            var k = raw / mag;
            var step = (k <= 1 ? 1 : k <= 2 ? 2 : k <= 5 ? 5 : 10) * mag;
            var lo = Math.floor(minV / step) * step;
            var hi = Math.ceil(maxV / step) * step;
            var out = [];
            // 上限 9 条兜底：want 与 span 都异常时也不至于把画布画满
            for (var i = 0; i <= 9; i++) {
                var v = lo + i * step;
                if (v > hi + step * 1e-6) break;
                out.push(Math.abs(v) < step * 1e-9 ? 0 : v);   // 消掉 -0
            }
            return out;
        }

        // 纵轴刻度数字：带单位，单位与「总净盈亏」同一套缩位（元 → 万元 →
        // 百万元，见 moneyScale）。sc 由**整条轴的量级**选定、全轴共用一个单位 ——
        // 若逐条各自判档，一条轴上会出现"0 元 / 50.00 万元 / 1.00 百万元"
        // 这种同轴混单位，刻度之间反而没法直接比大小。
        // step 决定小数位数：位数按"能把这个步长写准"来定 —— 步长 0.25 百万元
        // 若只给 1 位小数，0.25/0.5/0.75 会被写成 0.3/0.5/0.8（刻度值全是错的）。
        function eqAxisNum(v, step, sc) {
            var div = (sc && sc.div) || 1, unit = (sc && sc.unit) || "元";
            var q = Math.abs(step || 0) / div, dec = 0;
            while (dec < 6
                   && Math.abs(q * Math.pow(10, dec) - Math.round(q * Math.pow(10, dec))) > 1e-6) {
                dec++;
            }
            var nv = v / div;
            if (Math.abs(nv) < 1e-9) nv = 0;   // 消掉 -0（"-0 元"）
            var s = nv.toFixed(dec);
            // 步长带小数时取到的刻度仍可能是整数（0 / 0.5 / 1）—— 整数别拖
            // ".0"，否则 0 会写成 "0.0"。
            if (dec > 0) s = s.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
            return s + " " + unit;
        }

        // 横轴标签：与「市场量能」面板同源 —— 日期串直接交给它的 fmtAxisDate
        // （"YYYY-MM-DD" → "YY-MM-DD"），两个面板的日期写法因此不会各走各的。
        // wantTime=true（整条曲线都在同一天）时改给 "HH:MM" —— 同一天里并排
        // 三个 "26-09-18" 位置是有了、信息量为零；日内数据真正能区分的是时刻。
        // 解析不出来就返回空串 → 那根刻度只画线不画字（曲线本身照画，
        // 不能因为一行坏数据让整条轴变空白）。
        function eqAxisDate(s, wantTime) {
            var m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/
                .exec(String(s == null ? "" : s));
            if (!m) return "";
            if (wantTime && m[4]) return m[4] + ":" + m[5];
            return fmtAxisDate(m[1] + "-" + m[2] + "-" + m[3]);
        }

        function drawEquityCurve(curve) {
            var cv = document.getElementById("trade-equity-canvas");
            if (!cv || !curve.length) return;
            // 用 body（含 14px 左右内边距）反推可用宽度，避免 440px 卡片里出现横向滚动条。
            var box = cv.parentNode || document.getElementById("stats-panel");
            var w = Math.max(240, ((box ? box.clientWidth : 0) || 300) - 28);
            var h = 240;
            var dpr = window.devicePixelRatio || 1;
            var n = curve.length;
            // 尺寸与数据都未变则跳过重绘：给 canvas.width 赋值会清空画布，重复调用会让曲线闪动。
            // key 存在 canvas 自身 dataset 上，故每次重建 DOM（新 canvas 无 key）仍会正常绘制一次。
            var key = w + "x" + h + "@" + dpr + "#" + n + ":"
                + curve[0].cumulative + ">" + curve[n - 1].cumulative;
            if (cv.dataset && cv.dataset.drawnKey === key) return;
            if (cv.dataset) cv.dataset.drawnKey = key;
            cv.width = w * dpr; cv.height = h * dpr;
            cv.style.width = w + "px"; cv.style.height = h + "px";
            var ctx = cv.getContext("2d");
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);

            // 只收有限值：某一笔的 cumulative 若是 NaN/undefined，丢掉该点即可；
            // 放它进 max/min 会把整条轴与曲线一起算成 NaN —— 那样画布全空白且不报错。
            var seq = [];
            for (var i = 0; i < n; i++) {
                var v = Number(curve[i].cumulative);
                if (isFinite(v)) seq.push({ i: i, v: v });
            }
            if (!seq.length) return;
            var vals = seq.map(function (p) { return p.v; }).concat([0]);
            var maxV = Math.max.apply(null, vals), minV = Math.min.apply(null, vals);
            if (maxV === minV) { maxV += 1; minV -= 1; }

            // 纵轴刻度：轴范围直接用刻度边界（曲线因此永远贴轴走，不会顶出可视区）
            ctx.font = "10px system-ui";
            var ticks = eqAxisTicks(minV, maxV, 4);
            var lo = ticks[0], hi = ticks[ticks.length - 1];
            // 刻度步长：小数位数由它决定（ticks 至少两条，见 eqAxisTicks）
            var step = ticks.length > 1 ? ticks[1] - ticks[0] : 0;
            var range = hi - lo;
            // 纵轴单位：按**整条轴的量级**选一档（元 / 万元 / 百万元），全轴统一。
            // 取首尾刻度的绝对值里大的那个 —— 刻度边界已经把数据包住了，
            // 所以这就是这条轴上会出现的最大的数。
            var sc = moneyScale(Math.max(Math.abs(lo), Math.abs(hi)));
            // 左留白按最宽的那个刻度值**实测**宽度算：写死 46px 时
            // 7 位数（"-1200000" 宽 45.4px，右对齐贴 x=42）会把负号切掉。
            // 现在标签还带单位（"3.40 百万元"约 60px），更得实测。
            var labW = 0;
            for (var t = 0; t < ticks.length; t++) {
                labW = Math.max(labW, ctx.measureText(eqAxisNum(ticks[t], step, sc)).width);
            }
            var padL = Math.ceil(labW) + 12, padR = 10, padT = 12, padB = 30;
            var plotW = Math.max(40, w - padL - padR), plotH = h - padT - padB;
            function yOf(v) { return padT + plotH * (1 - (v - lo) / range); }
            function xOf(idx) { return padL + (n <= 1 ? plotW / 2 : plotW * idx / (n - 1)); }
            var y0 = yOf(0);

            // ① 网格 + 纵轴刻度值 + 纵轴线
            ctx.lineWidth = 1; ctx.textBaseline = "middle"; ctx.textAlign = "right";
            ctx.strokeStyle = "#122a52";
            for (var g = 0; g < ticks.length; g++) {
                var gy = yOf(ticks[g]);
                ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(w - padR, gy); ctx.stroke();
            }
            ctx.beginPath(); ctx.moveTo(padL, padT); ctx.lineTo(padL, padT + plotH); ctx.stroke();
            ctx.fillStyle = "#8892b0";
            for (var g2 = 0; g2 < ticks.length; g2++) {
                ctx.fillText(eqAxisNum(ticks[g2], step, sc), padL - 6, yOf(ticks[g2]));
            }

            // ② 0 轴：曲线跨 0 时单独标出；单边行情下 0 就是最外侧那条网格线
            //    （旧版无论何时都再画一条 0 虚线 + 一个"0"文字，于是 0 被画两遍）
            if (lo < 0 && hi > 0) {
                ctx.strokeStyle = "#888"; ctx.setLineDash([4, 3]);
                ctx.beginPath(); ctx.moveTo(padL, y0); ctx.lineTo(w - padR, y0); ctx.stroke();
                ctx.setLineDash([]);
            }

            // ③ 曲线与 0 轴之间的面积，再压曲线本身
            var j;
            ctx.beginPath();
            ctx.moveTo(xOf(seq[0].i), y0);
            for (j = 0; j < seq.length; j++) ctx.lineTo(xOf(seq[j].i), yOf(seq[j].v));
            ctx.lineTo(xOf(seq[seq.length - 1].i), y0);
            ctx.closePath();
            ctx.fillStyle = "rgba(255,215,0,0.10)"; ctx.fill();
            ctx.beginPath();
            for (j = 0; j < seq.length; j++) {
                var cx = xOf(seq[j].i), cy = yOf(seq[j].v);
                if (j === 0) ctx.moveTo(cx, cy); else ctx.lineTo(cx, cy);
            }
            ctx.strokeStyle = "#FFD700"; ctx.lineWidth = 1.5; ctx.stroke();

            // ④ 横轴：轴线 + 3 根刻度线 + 3 个日期（首 / 中 / 尾）
            //    条数固定 3，与「市场量能」面板同一口径 —— 横轴是「第几笔」，
            //    笔数只决定点与点的间距、不决定标签条数。1000 笔时落点仍是
            //    index 0 / 499 / 999，与 5 笔时的 0 / 2 / 4 完全同构，
            //    不会随笔数增长把横轴挤成一团。
            var baseY = padT + plotH;
            ctx.strokeStyle = "#2a3f6b"; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(padL, baseY); ctx.lineTo(w - padR, baseY); ctx.stroke();
            // 整条曲线落在同一个自然日 → 横轴给时刻而不是日期
            var day0 = String(curve[0].exit_at || "").slice(0, 10);
            var sameDay = !!day0 && day0 === String(curve[n - 1].exit_at || "").slice(0, 10);
            var mid = Math.floor((n - 1) / 2);
            var marks = (n <= 1) ? [0] : [0, mid, n - 1];
            var seen = {}, lastTxt = null;
            for (var li = 0; li < marks.length; li++) {
                var idx = marks[li];
                if (seen[idx]) continue;   // n=2 时 mid=0 与首刻度重合，去掉
                seen[idx] = 1;
                var lx = xOf(idx);
                ctx.strokeStyle = "#2a3f6b";
                ctx.beginPath(); ctx.moveTo(lx, baseY); ctx.lineTo(lx, baseY + 4); ctx.stroke();
                var dTxt = eqAxisDate(curve[idx].exit_at, sameDay);
                if (!dTxt) continue;
                if (dTxt === lastTxt) continue;   // 与前一个刻度同字 → 不重复画
                lastTxt = dTxt;
                // 首标签左对齐、尾标签右对齐（贴着绘图区边缘走，因此不会越界），
                // 中间的居中；只画一笔时居中。
                ctx.textAlign = (n === 1) ? "center"
                    : (idx === 0 ? "left" : (idx === n - 1 ? "right" : "center"));
                ctx.fillStyle = "#8892b0";
                ctx.fillText(dTxt, lx, baseY + 15);
            }

            // ⑤ 末端数值：回答"现在累计到多少"。靠右时就翻到点的左侧
            //    —— 旧版固定向右排，7 位数会被画布右缘裁掉（实测裁 7.4px）。
            var lastV = seq[seq.length - 1].v;
            var lastX = xOf(seq[seq.length - 1].i), lastY = yOf(lastV);
            // 末端值就是当前的累计净盈亏，与面板「总净盈亏」是同一个数
            // （TradeStats.py:197 total_net = Σ净盈亏；:213-222 cumulative 逐笔累加
            // 全部成交）—— 所以这里调的就是面板那个 money()，两处字面必然一致：
            // 曲线末端写"3.40 百万元"、面板也写"3.40 百万元"，不会一边缩位一边不缩。
            var tag = money(lastV);
            var tagW = ctx.measureText(tag).width;
            var tagLeft = (lastX + 8 + tagW <= w - padR);
            ctx.textAlign = tagLeft ? "left" : "right";
            ctx.fillStyle = lastV >= 0 ? "#FF3C3C" : "#00F0F0";
            ctx.beginPath(); ctx.arc(lastX, lastY, 2.5, 0, Math.PI * 2); ctx.fill();
            ctx.fillText(tag, tagLeft ? lastX + 8 : lastX - 8,
                Math.max(padT + 7, Math.min(padT + plotH - 7, lastY)));
            ctx.textBaseline = "alphabetic";
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




// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] BacktestPanel —— 股票态「回测」面板（设计文档 §4）
//   · 按钮复用 #btn-stats：股票态文案「回测」、期货态「统计」（§4.1 —— 股票/期货
//     是**同一个按钮的两种市场态**，不是两个 DOM）。统计面板的数据源是期货自动下单
//     的成交账本（state.db），股票态本来就没有可看的东西 ⇒ 改名零功能损失（§4.2/Q7）。
//   · 数据来源 = 当前页面**加载序列** chartData.klines（**不是**视口
//     getVisibleKlines()）：后端按它跑一遍 ⇒ 区间天然「所见即所测」，
//     不需要另传 [L, R]（§4.4）。
//   · 不落盘（Q8 定案）：纯请求-响应；报告 / CSV 导出留给 CLI 批跑。
//   · 双窗态护栏（§4.4 前端护栏 ①）：回测只复刻**单窗口径**，双窗时禁用按钮并说明
//     原因 —— 不加护栏的话，用户会拿到与图上不一致的数字，且没有任何东西告诉他
//     那是「本次未启用区间套」的配置差异。
// ══════════════════════════════════════════════════════════════════

        var _btSeq = 0;          // 过期响应丢弃：切标的/周期后，在途的旧结果不得覆盖新结果
        var _btBtnLabel = null;  // 按钮文案缓存：render() 是热路径，文案未变就不碰 DOM

        window.toggleBacktestPanel = function() {
            var panel = document.getElementById("bt-panel");
            if (!panel) return;
            if (panel.classList.contains("show")) { closeBacktestPanel(); return; }
            panel.classList.add("show");
            runBacktest();
        };

        window.closeBacktestPanel = function() {
            var panel = document.getElementById("bt-panel");
            if (panel) panel.classList.remove("show");
        };

        // 按钮文案 + 双窗护栏同步（在 render() 里调；文案未变则不写 DOM）。
        // 放在 render() 而不是"加载完成回调"：市场态还随 freq 切换 / 复盘进出而变，
        // 散在多个调用点必然漏一处，而 render() 是所有这些路径的公共下游。
        function syncStatsButtonLabel() {
            var btn = document.getElementById("btn-stats");
            if (!btn || typeof chartData === "undefined" || !chartData || !chartData.meta) return;
            var futures = isFuturesMode();
            var label = futures ? "统计" : "回测";
            if (_btBtnLabel !== label) {
                // 市场态切换：两面板占同一屏位，同时 show 会叠在一起 ⇒ 收掉不适用的那个。
                // 首次同步（_btBtnLabel === null）跳过关闭动作：此时两面板本来就是初始
                // 隐藏态，且首屏 render() 可能早于 window.close*Panel 的赋值执行。
                if (_btBtnLabel !== null) {
                    if (futures) closeBacktestPanel(); else closeStatsPanel();
                }
                btn.textContent = label;
                _btBtnLabel = label;
            }
            var dual = (!futures && isDualWindow);
            btn.disabled = dual;
            btn.title = dual ? "当前为双窗态，回测只支持单窗口径 —— 请先切回单窗"
                             : (futures ? "" : "回测当前页面标的与周期（区间所见即所测）");
        }

        // 买卖点类型勾选（与自动下单 / 成交统计 / 股票扫描共用同一份 bspFilter，
        //   §4.7.3）：
        //   全勾 = 未启用过滤 → 不传 bsp_types（后端 None = 全放行）；
        //   四类全不勾 → 传空串（后端 "" = 全部过滤掉）。二者是**不同语义**，
        //   合并会让"全不勾"静默变成"全放行"，恰好相反。
        //   消费方 = 单页「回测」+ 扫描的「买/卖点」与「回测」两个模式；三者
        //   取的是同一个函数的返回值，故过滤口径不会漂移。
        function _btBspTypes() {
            var on = ["0", "1", "2", "3"].filter(function (t) { return !!bspFilter[t]; });
            return on.length === 4 ? null : on.join(",");
        }

        function _btWrite(html) {
            var box = document.getElementById("bt-content");
            if (box) box.innerHTML = html;
        }

        function _btCol(x) { return Number(x) >= 0 ? "#FF3C3C" : "#00F0F0"; }   // 涨红跌绿（与统计面板同款）
        // `_btNA`（指数页「不适用」徽章）已随 2026-10-06「指数当个股」拍板删除：
        //   字段与个股完全一致地下发与渲染，不再有任何「不适用」分流；指数的假设
        //   由口径披露行说明（前端横幅 + Report.caliber_lines；面板底部 disclosures 不追加）。
        // 2026-10-06 补充裁定：正值**不带 + 号**（红/灰都不带）—— 正负由颜色表达，
        //   符号再表达一遍是冗余；负数自带的 `-` 号保留。
        function _btPct(x, nd) {
            if (x === null || x === undefined) return "—";
            var v = Number(x);
            return v.toFixed(nd === undefined ? 2 : nd) + "%";
        }
        function _btNum(x, nd) {
            if (x === null || x === undefined) return "—";
            var v = Number(x);
            return v.toFixed(nd === undefined ? 3 : nd);
        }
        // 金额 → 万元（保留 2 位、去尾零：50000 → "5 万元"，53396 → "5.34 万元"）。
        //   面板上的金额只有两处（目标成交额 / 最大成交额），量级都在 5 万上下 ——
        //   用「元」得数位数、用「亿元」又全变成 0.0x，万元是唯一读得顺的档。
        //   不复用 `_btNum`：金额与倍数/R 是两种量纲，格式各写各的。
        function _btWan(x) {
            if (x === null || x === undefined) return "—";
            var s = (Number(x) / 10000).toFixed(2);
            s = s.replace(/\.?0+$/, "");
            return s + " 万元";
        }

        function runBacktest() {
            if (!chartData || !chartData.meta || !chartData.meta.symbol) {
                _btWrite('<div class="stats-row"><span class="stats-value">无标的上下文</span></div>');
                return;
            }
            var klines = chartData.klines || [];
            if (!klines.length) {
                _btWrite('<div class="stats-row"><span class="stats-value">当前页面无 K 线数据</span></div>');
                return;
            }
            var symbol = chartData.meta.symbol;
            var body = { code: symbol, freq: currentFreq, klines: klines };
            var types = _btBspTypes();
            if (types !== null) body.bsp_types = types;

            var seq = ++_btSeq;
            _btWrite('<div class="stats-loading" style="padding:8px;color:#a8b2d1;">回测中…（'
                + klines.length + ' 根 ' + statsEsc(String(currentFreq)) + '）</div>');
            fetch("/api/stocks/" + encodeURIComponent(symbol) + "/backtest", {
                method: "POST",
                cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            })
                .then(function (r) {
                    return r.json().then(function (d) {
                        if (!r.ok) throw new Error((d && d.detail) || ("HTTP " + r.status));
                        return d;
                    });
                })
                .then(function (d) {
                    if (seq !== _btSeq) return;      // 过期响应丢弃
                    renderBacktest(d);
                })
                .catch(function (err) {
                    if (seq !== _btSeq) return;
                    _btWrite('<div class="stats-row"><span class="stats-value">回测失败：'
                        + statsEsc(err && err.message ? err.message : String(err)) + '</span></div>');
                });
        }

        function renderBacktest(d) {
            var s = d.summary || {}, run = d.run || {}, cal = d.caliber || {}, tgt = d.target || {};
            // 出场原因三选一（止损 / 保本 / 跟踪止盈）的文案由**后端下发**
            //   （SSOT = Backtest/Report.py::exit_reason_labels，与 `python -m Backtest.Runner`
            //   的控制台摘要同一份）。前端不硬编码任何 reason 文案；查不到的 key **原样显示**
            //   —— 宁可露出一个英文标识符，也不要把没见过的原因静默吞掉。
            var reasonLabels = d.exit_reason_labels || {};
            var reasonLegend = d.exit_reason_legend || {};
            var html = "";

            // ① 口径标识置顶（口径先行：先说清这几个数字是在什么口径下算的）
            if (cal.bsp_types !== null && cal.bsp_types !== undefined) {
                html += '<div class="stats-row" style="background:rgba(255,176,32,0.10);'
                    + 'border-left:3px solid #FFB020;padding:4px 6px;margin-bottom:6px;'
                    + 'font-size:11px;color:#FFB020;">已按「买卖点类型」勾选做事前过滤：'
                    + statsEsc(cal.bsp_types || "（全不勾）") + ' 类</div>';
            }

            // ①b 指数标识（2026-10-06 拍板「指数当个股」）：字段与个股完全一致，
            //     不再有任何「不适用」分流；横幅只披露假想假设本身。
            //     判定不在前端做（前端只看后端 `is_index`）—— 页面级 SSOT 是
            //     `App.AppUtils.is_index`（含 88xx 板块指数 / ds 扩展指数 / hk 字母
            //     代码），**不是**取数层的 `DataAPI.TdxAPI._is_index_code`（那只认
            //     A 股指数段，漏 88xxxx）。前端复制一份必然漂移。
            var isIndex = !!(tgt.is_index || cal.is_index);
            if (isIndex) {
                html += '<div class="stats-row" style="background:rgba(0,240,240,0.10);'
                    + 'border-left:3px solid #00F0F0;padding:4px 6px;margin-bottom:6px;'
                    + 'font-size:11px;color:#5fd7de;">指数按个股假想：1点=1元、'
                    + '1手=100股、费率同个股（示意值，看信号质量）</div>';
            }

            // ② 核心区：一行五格（对齐「成交统计」面板的 .stats-hero）
            //   2026-10-06 统计口径统一轮（v2.1 定案）：六格 → 五格 —— 「净收益率(均)」
            //   （含浮口径）删除、期望值(R) 改为「期望值(%/笔)」：四指标（胜率 / 盈亏比 /
            //   盈利因子 / 期望值）一律**已实现**口径、未平仓笔不进任何分子分母，与期货
            //   「期望值(元/笔)」同构不同单位。期望值(%/笔) = 每笔净收益率% 的等权平均
            //   （`avg_net_return_pct`），保留 2 位小数。
            //   `bt-hero` 是给 CSS 的钩子：五格仍比期货统计面板的四格挤一格，
            //   保留这一处降一号标签字号（见 app.css 的 `.bt-hero` 规则）。
            html += '<div class="stats-hero bt-hero">';
            html += '<div class="stats-cell"><span class="stats-label">交易笔数</span><span class="stats-value">'
                + run.filled + '</span></div>';
            html += '<div class="stats-cell"><span class="stats-label">胜率</span><span class="stats-value">'
                + (s.win_rate === null || s.win_rate === undefined
                    ? "—" : (Number(s.win_rate) * 100).toFixed(1) + "%") + '</span></div>';
            html += '<div class="stats-cell"><span class="stats-label">盈亏比</span><span class="stats-value">'
                + _btNum(s.profit_loss_ratio, 3) + '</span></div>';
            html += '<div class="stats-cell"><span class="stats-label">盈利因子</span><span class="stats-value">'
                + _btNum(s.profit_factor, 3) + '</span></div>';
            html += '<div class="stats-cell"><span class="stats-label">期望值(%/笔)</span><span class="stats-value" style="color:'
                + _btCol(s.avg_net_return_pct) + '">' + _btPct(s.avg_net_return_pct) + '</span></div>';
            html += '</div>';

            // ③ 明细行
            //   标的显示**股票名**（2026-10-06 用户裁定）：`chartData.meta.name` 就是页面
            //   标题一直在用的那份；仅当它与本次响应的标的**同一个**时才替换（防串标的）。
            //   取不到名字就回落代码 —— 宁可显示 sh600036，也不要留一个空标签。
            var _dispName = statsEsc(tgt.code || "");
            if (typeof chartData !== "undefined" && chartData && chartData.meta
                && chartData.meta.name && chartData.meta.symbol === tgt.code) {
                _dispName = statsEsc(chartData.meta.name);
            }
            html += '<div class="stats-rows">';
            html += '<div class="stats-row"><span class="stats-label">标的 / 周期</span><span class="stats-value">'
                + _dispName + ' · ' + statsEsc(tgt.freq_label || "") + '</span></div>';
            html += '<div class="stats-row"><span class="stats-label">区间 / K线</span><span class="stats-value">'
                + statsEsc((tgt.date_from || "") + " ~ " + (tgt.date_to || "")) + '（' + tgt.bars + ' 根）</span></div>';
            // 仓位口径两项（§5.4d-quater：报告必须披露放大倍数）
            //   2026-10-06 用户裁定：金额一律用**万元** —— `50000 元` 要数位数，
            //   `5 万元` 一眼读出来。行序 = 目标成交额 → 最大成交额。
            //   同日补充裁定：①「最小申报 / 借道笔数」行删除（`min_lot` /
            //   `min_lot_derived_trades` 照常下发，CLI 摘要照印）；②「最大单笔放大」
            //   10-06 改名「实际成交额」、10-07 再改「最大成交额」—— 值格式不变
            //   （倍数（万元））；语义 = N 笔里实际成交额最大的那一笔（各用各的入场价）。
            //   v2.2（指数当个股）：指数不再「不适用」，照常显示（上证 3842 点的
            //   放大倍数 ≈ 7.7 倍，如实披露 —— 假设本身由口径行说明）。
            html += '<div class="stats-row"><span class="stats-label">目标成交额</span><span class="stats-value">'
                + (cal.target_amount === undefined || cal.target_amount === null
                    ? "—" : _btWan(cal.target_amount)) + '</span></div>';
            html += '<div class="stats-row"><span class="stats-label">最大成交额</span><span class="stats-value">'
                + ((cal.max_notional_multiple === null || cal.max_notional_multiple === undefined
                    ? "—" : Number(cal.max_notional_multiple).toFixed(2) + " 倍")
                   + '（' + _btWan(cal.max_notional) + '）') + '</span></div>';
            html += '<div class="stats-row"><span class="stats-label">胜负平</span><span class="stats-value">'
                + '胜 ' + s.w + ' / 亏 ' + s.l + ' / 平 ' + s.e + (s.u ? ' / 未平 ' + s.u : '') + '</span></div>';
            // 「平均持仓 / 最好·最差 R」行 2026-10-06 用户裁定**移除**：样本少时
            //   "仅 1 笔已平仓，最好＝最差"这类说明比信息本身还长。数据仍在
            //   summary（avg_bars_held / max_win_r / max_loss_r）里照常下发，
            //   CLI 摘要照印 —— 只是面板不再占一行版面。
            // 「首见信号 / 拒收 / 过滤」行同日裁定移除：这三个计数是
            //   引擎内部口径（`首见 = 过滤 + 拒收 + 开仓笔数`），与页面上画出来的买卖点
            //   对不上（图上只画勾选的类型），摆在面板里只会引出"为什么你说是 5 个、
            //   我只看得到 3 个"这类问题。数据仍在 `run` 里照常下发（CLI 摘要照印），
            //   谁要谁取，不再占用面板版面。

            // 「类型拆解（0类 1笔·均R +2.1）」行同日裁定**移除**：逐笔明细已逐行
            //   给出类型与 R 倍数，分组均值在单类型样本下是同义反复。
            //   `by_bsp_type` 照常下发，CLI 摘要照印 —— 只是面板不再占一行。
            // 出场原因（三选一）：三个枚举**恒显示**，即使某类 0 笔 —— 面板要看的是
            //   "这三条路各走了几次"，没触发过的那条不出现，就看不出来"这轮压根没走过保本"。
            var byReason = d.by_reason || {};
            var reasonKeys = Object.keys(reasonLabels);
            // 后端映射里没有、但桶里真出现过的 key 追加在后（不静默丢）
            Object.keys(byReason).sort().forEach(function (k) {
                if (reasonKeys.indexOf(k) < 0) reasonKeys.push(k);
            });
            if (reasonKeys.length) {
                // 计数行用**短名**（2026-10-06 用户裁定）：跟踪止盈 → 跟踪。
                //   只缩这一行 —— 逐笔明细与后端 `exit_reason_labels` 仍是全名
                //   （那里"跟踪止盈"是规则身份，缩成"跟踪"反而不知道在跟踪什么）。
                var _rShort = { "跟踪止盈": "跟踪" };
                var reasonTxt = reasonKeys.map(function (k) {
                    var n = (byReason[k] || {}).n || 0;
                    var lg = reasonLegend[k];
                    var _nm = _rShort[reasonLabels[k] || k] || reasonLabels[k] || k;
                    return '<span' + (lg ? ' title="' + statsEsc(lg) + '"' : '')
                        + ' style="cursor:' + (lg ? 'help' : 'default') + '">'
                        + statsEsc(_nm) + ' ' + n + '</span>';
                }).join('<span style="color:#4a5165"> · </span>');
                html += '<div class="stats-row"><span class="stats-label">出场原因</span><span class="stats-value" style="font-size:11px">'
                    + reasonTxt + '</span></div>';
            }

            html += '</div>';

            // ④ 逐笔明细 —— **最新在上**（2026-10-06 补充裁定）：倒序渲染。
            //   编号按**显示位置**从 1 递增（2026-10-06 同日裁定：首行必须是 1，
            //   往下依次增大；trade_id 仍随数据下发，只作跨轮次稳定标识，
            //   不再直接当序号 —— 倒序显示时它会让首行顶着最大号，违反直觉）。
            var trades = (d.trades || []).slice().reverse();
            html += '<div class="stats-rows" style="margin-top:6px;">';
            html += '<div class="stats-row"><span class="stats-label">逐笔明细</span>'
                + '<span class="stats-value" style="font-size:11px;color:#a8b2d1;">' + trades.length + ' 笔</span></div>';
            html += trades.map(function (t, i) {
                var side = t.side === "long" ? "多" : "空";
                // 出场原因走**后端下发的映射**；未平仓笔没有原因（它还没出场）
                var reason = (t.exit_reason === null || t.exit_reason === undefined)
                    ? "" : (reasonLabels[t.exit_reason] || t.exit_reason);
                var metric;
                if (t.open) {
                    // 持仓中：盈亏 = 截止**最后一根 K 线收盘价**的浮动（后端按"假如以该价
                    //   平掉"估的，成本与已平仓笔同一套函数 ⇒ 两族数字可直接比）。
                    //   末尾标个「浮」—— 免得把还没落袋的浮动当成成交结果。
                    var tip = "截止 " + (tgt.date_to || "最新")
                        + " 收盘价 " + _btNum(t.unrealized_price, 3)
                        + " 的浮动盈亏（未平仓；成本按该价平仓估算）";
                    if (t.unrealized_net_return_pct === null
                        || t.unrealized_net_return_pct === undefined) {
                        metric = '<span title="' + tip + '" style="color:'
                            + _btCol(t.unrealized_r) + '">' + _btNum(t.unrealized_r, 2) + 'R</span>'
                            + '<span style="color:#8b93a7;font-size:10px;"> 浮</span>';
                    } else {
                        metric = '<span title="' + tip + '" style="color:'
                            + _btCol(t.unrealized_net_return_pct) + '">'
                            + _btPct(t.unrealized_net_return_pct) + ' ('
                            + _btNum(t.unrealized_r, 2) + 'R)</span>'
                            + '<span style="color:#8b93a7;font-size:10px;"> 浮</span>';
                    }
                } else if (t.net_return_pct === null || t.net_return_pct === undefined) {
                    // 净收益率缺失（notional 为 0 的病态样本）时只留 R —— 纯兜底分支：
                    //   v2.2 后指数与个股同形、净收益率族恒有值，此分支正常不再触发。
                    metric = '<span style="color:' + _btCol(t.r_multiple) + '">'
                        + _btNum(t.r_multiple, 2) + 'R</span>';
                } else {
                    metric = '<span style="color:' + _btCol(t.net_return_pct) + '">'
                        + _btPct(t.net_return_pct) + ' (' + _btNum(t.r_multiple, 2) + 'R)</span>';
                }
                // 行首 2026-10-06 用户裁定：`#1` → `1.`；序号 = **显示位置**（i+1，
                //   首行必为 1）；日期后插 `R=xx` =
                //   该笔**入场时冻结的风险距离**（`r_distance`，单位元，
                //   `max(结构距离, atr_sl_multiple×ATR)`）—— 右端的 (+2.19R) 是"赚了几个 R"，
                //   这里的 R=0.66 是"1R 有多大"，一个是分母一个是商，缺一读不懂。
                //   未平仓笔同样有（入场那一刻就定了），故两支都显示。
                var _rd = (t.r_distance === null || t.r_distance === undefined)
                    ? "—" : Number(t.r_distance).toFixed(2);
                return '<div class="stats-row" style="font-size:11px;">'
                    + '<span class="stats-label">' + (i + 1) + '. ' + side + ' ' + statsEsc(t.bsp_type) + '类 '
                    + statsEsc(t.entry_date) + ' → ' + statsEsc(t.exit_date || "持仓中")
                    + ' R=' + _rd
                    + (reason ? ' ' + statsEsc(reason) : '') + '</span>'
                    + '<span class="stats-value">' + metric + '</span></div>';
            }).join("");
            html += '</div>';

            // ⑤ 口径披露（三条偏离，与「止盈止损」推演同款）
            html += '<div class="stats-rows" style="margin-top:6px;">';
            html += '<div class="stats-row"><span class="stats-label" style="font-size:11px;color:#a8b2d1;">口径披露</span>'
                + '<span class="stats-value" style="font-size:11px;color:#a8b2d1;text-align:right;">'
                + statsEsc((d.disclosures || []).join("；")) + '</span></div>';
            html += '</div>';

            _btWrite(html);
        }



// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] BspSettingsPanel —— 买卖点设置弹窗组件（BSP 过滤 / 均线周期）

// ══════════════════════════════════════════════════════════════════

        // ── BSP买卖点类型过滤 + 均线周期设置 ──

        // 把「买卖点类型」勾选同步给自动下单（交易引擎子进程）：
        //   与图上画哪些买卖点是同一份勾选 —— 未勾选的类型，自动下单忽略其信号
        //   （四个全不勾 = 不再有新的开仓 / 拆锁报单；运行态已有持仓的止损止盈
        //    走 L1-L3、不经信号，不受影响）。
        //   通道：POST /api/trader/signal-filter → 写引擎 state.db，引擎每个信号
        //   现读 → 盘中改勾选即时生效，无需重启自动下单子进程、与账户三态无关。
        //   失败只告警：显示过滤是本地行为，不能因为下单侧接口故障就挡住看图。
        // 后端 state.db 是「哪几类会被自动下单执行」的唯一真值源：打开设置面板
        // 时 GET 回填，换浏览器 / 多标签页 / 别处改过都能看到实际生效的那一份。
        // bspFilterLocalVer：本地勾选变更计数 —— GET 是异步的，回填返回前用户
        // 可能已经改了勾选，此时丢弃回填结果，绝不覆盖用户刚做的改动；
        // 推送成功时也自增一次，让"推送前发出、推送后才返回"的旧回填一并作废。
        var bspFilterLocalVer = 0;
        // bspFilterPushInFlight：在飞的推送数 —— 推送还没落地时到达的回填必然是
        // 改前的旧值，套用会把用户刚勾的改回去（显示与引擎再次相反）。
        var bspFilterPushInFlight = 0;

        function _bspPushDone() {
            bspFilterPushInFlight = Math.max(0, bspFilterPushInFlight - 1);
        }

        // 写入抽屉内的「自动下单状态」提示行。抽屉中现在不提供该元素，
        // 这里判空即返回；保留函数与全部调用点，是为了让「接口不可达 / 写入被拒 /
        // 推送完成」三条判定路径的调用位置保持完整，静态护栏可直接引用这些文本。
        function renderBspFilterEngineState(filt, unreachable) {
            var el = document.getElementById("bsp-filter-engine-state");
            if (!el) return;
            if (unreachable) {
                el.textContent = "自动下单状态未同步（接口不可达或写入被拒）：勾选只影响图上显示";
                return;
            }
            if (!filt) {
                el.textContent = "自动下单：未设置类型过滤 → 当前全部放行";
                return;
            }
            var on = ["0", "1", "2", "3"].filter(function (t) { return filt[t]; });
            el.textContent = on.length
                ? "自动下单当前认 " + on.join("/") + " 类（未勾选的类型不产生新的信号单）"
                : "自动下单当前不认任何类型：不再有信号驱动的新报单；已有持仓的止损止盈仍会照常离场";
        }

        function applyBspFilterFromTrader(filt) {
            var types = ["0", "1", "2", "3"];   // 与后端 BSP_TYPE_CHOICES 一一对应
            for (var i = 0; i < types.length; i++) {
                // filt 为空 = 引擎侧从未设置过滤 = 全部放行 → 回填为全勾，
                // 让面板显示与引擎真实行为一致
                bspFilter[types[i]] = filt ? !!filt[types[i]] : true;
            }
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var j = 0; j < cbs.length; j++) cbs[j].checked = !!bspFilter[cbs[j].value];
            saveOverlaySettings();
            renderBspFilterEngineState(filt);
            render();
        }

        function syncBspFilterFromTrader() {
            var v = bspFilterLocalVer;
            try {
                fetch("/api/trader/signal-filter", { cache: "no-store" })
                    .then(function (resp) {
                        // 非 2xx（老后端没这条路由 / 接口故障）也要落到"未同步"：
                        // 直接 return null 会被下面当成"没数据"静默丢掉，状态行
                        // 永久停在"读取中…"，比不显示还糟。
                        return resp.ok ? resp.json() : null;
                    })
                    .then(function (data) {
                        if (bspFilterLocalVer !== v || bspFilterPushInFlight > 0) return;
                        if (!data) { renderBspFilterEngineState(null, true); return; }
                        applyBspFilterFromTrader(data.bsp_type_filter);
                    })
                    .catch(function () {
                        if (bspFilterLocalVer !== v) return;
                        renderBspFilterEngineState(null, true);
                    });
            } catch (e) { /* 后端不可达：保留本地显示，不挡看图 */ }
        }

        function pushBspFilterToTrader() {
            bspFilterLocalVer++;
            bspFilterPushInFlight++;
            try {
                fetch("/api/trader/signal-filter", {
                    method: "POST",
                    cache: "no-store",
                    headers: { "Content-Type": "application/json" },
                    // 固定发满四键：后端把"键不全"当参数错误拒掉（400），
                    // 完整性责任放在**唯一生产者**这一侧，别让一份被手工改过的
                    // localStorage 变成"少一个键 = 一次 400 提示"。
                    body: JSON.stringify({ bsp_types: {
                        "0": !!bspFilter["0"], "1": !!bspFilter["1"],
                        "2": !!bspFilter["2"], "3": !!bspFilter["3"]
                    } })
                }).then(function (resp) {
                    _bspPushDone();
                    if (!resp.ok) {
                        showToast("买卖点过滤未同步到自动下单（HTTP " + resp.status + "）");
                        renderBspFilterEngineState(null, true);
                        return;
                    }
                    // 推送已落地：版本号再自增一次（作废"改前发出"的回填），
                    // 并就地刷新状态行 —— 盘中改完立刻能核对"引擎现在认哪几类"，
                    // 不必靠重开面板。
                    bspFilterLocalVer++;
                    renderBspFilterEngineState(bspFilter);
                }).catch(function (e) {
                    _bspPushDone();
                    showToast("买卖点过滤未同步到自动下单：" + e.message);
                    renderBspFilterEngineState(null, true);
                });
            } catch (e) {
                _bspPushDone();
                showToast("买卖点过滤未同步到自动下单：" + e.message);
            }
        }

        window.openBspSettings = function() {
            // 以自动下单侧为准回填一次（异步；期间用户已手动改过则丢弃回填）
            var _stEl = document.getElementById("bsp-filter-engine-state");
            if (_stEl) _stEl.textContent = "自动下单状态读取中…";
            syncBspFilterFromTrader();
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
            initVolDisplayModeRadio();
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
            pushBspFilterToTrader();
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

        // 成交额/量显示模式（柱状图 / 类MACD）：即时生效，无需重开页面或重连。
        // 刻意**不用内联 onchange** —— 内联处理器必须挂到 window.*，而 window API 面
        // 已冻结（Test/test_phase6_guards.py ③ 逐名比对冻结基线），故改挂 addEventListener。
        function onVolDisplayModeRadioChange(ev) {
            var el = ev && ev.target ? ev.target : ev;
            if (!el || !el.value) return;
            _volDisplayMode = (el.value === 'macd') ? 'macd' : 'bar';
            saveOverlaySettings();
            render();
        }

        // 打开抽屉时调用：同步选中态 + 首次挂监听（data-bound 幂等，重复打开不叠加）
        function initVolDisplayModeRadio() {
            var radios = document.querySelectorAll('#bsp-filter-dialog input[name="vol-display-mode"]');
            for (var i = 0; i < radios.length; i++) {
                radios[i].checked = (radios[i].value === _volDisplayMode);
                if (!radios[i].getAttribute('data-bound')) {
                    radios[i].setAttribute('data-bound', '1');
                    radios[i].addEventListener('change', onVolDisplayModeRadioChange);
                }
            }
        }

        window.bspFilterSelectAll = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = true;
                bspFilter[cbs[i].value] = true;
            }
            saveOverlaySettings();
            pushBspFilterToTrader();
            render();
        };

        window.bspFilterSelectNone = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = false;
                bspFilter[cbs[i].value] = false;
            }
            saveOverlaySettings();
            pushBspFilterToTrader();
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




// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] ScanPanel —— 扫描面板组件（模式对话框 / 逐只扫描 / 结果渲染 / 保存自选）

// ══════════════════════════════════════════════════════════════════

        // 扫描模式切换时，控制"最近N根"输入框的灰化状态
        // 标注扫描：只要有标注就命中，与日期无关，输入框置灰；扫描来源也置灰
        // 底分型扫描：找最后一个分型是底分型的个股，与日期无关，输入框置灰；扫描来源可用
        // 均线分类扫描：按最新收盘价分类，与日期无关，输入框置灰；扫描来源可用
        // 回测扫描：整条加载序列参与回测，与"最近N根"无关，输入框置灰；
        //           扫描来源与扫描周期**都可用**（回测必须知道扫谁、按什么周期扫）
        // 买卖点/放量/连涨扫描：需要按最近N根K线过滤，输入框可用
        function updateScanRecentDisabled() {
            var row = document.getElementById("scan-recent-row");
            var input = document.getElementById("scan-recent-days");
            var freqRow = document.getElementById("scan-freq-row");
            var selected = document.querySelector('input[name="scan-mode"]:checked');
            var isAnn = selected && selected.value === "ann";
            var isMa = selected && selected.value === "ma";
            var isFxD = selected && selected.value === "fx_d";
            var isBacktest = selected && selected.value === "backtest";
            var isLianzhang = selected && selected.value === "lianzhang";
            if (row && input) {
                if (isAnn || isMa || isFxD || isBacktest) {
                    row.style.opacity = "0.35";
                    row.style.pointerEvents = "none";
                    input.disabled = true;
                } else {
                    row.style.opacity = "1";
                    row.style.pointerEvents = "";
                    input.disabled = false;
                    // 连涨：「最近N根」是核心参数，N=1（单根收红）几乎无意义
                    //   ⇒ 进入本模式时若当前值 < 2 自动填 3（用户仍可改）。
                    if (isLianzhang && (parseInt(input.value, 10) || 0) < 2) {
                        input.value = 3;
                    }
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
            var map = {"zxg": "自选股", "page_index": "成分股", "tdxhy2": "板块指数2", "tdxhy3": "板块指数3", "all_a": "全A股"};
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
            // 注：原先勾选"成分股"时会先 PUT /scan/set/index 把指数代码写进
            // 后端**全局**，后端读全局来决定扫哪些成分股。多网页下后设置的
            // 页面会覆盖先设置的，两页会按同一指数扫。
            // 现改为在 read/candidates 与 scan/start 上随请求传
            // page_index_code（见 runScan），故不再需要这次写全局的调用。
            // 执行实际扫描
            doStartScan();
        };

        function updateScanTitle() {
            var freqLabels = {"d": "日K", "w": "周K", "30m": "30分", "15m": "15分", "5m": "5分"};
            var freqLabel = freqLabels[_scanFreq] || _scanFreq;
            if (_scanMode === "bsp") {
                document.getElementById("scan-title").innerHTML = freqLabel + '<span style="font-size:11px;font-weight:400;color:#a8b2d1">[最近</span><b style="font-size:11px;color:#e94560">' + _scanRecentDays + '</b><span style="font-size:11px;font-weight:400;color:#a8b2d1">根]</span> 买/卖点';
            } else if (_scanMode === "ma") {
                document.getElementById("scan-title").textContent = freqLabel + " 均线";
            } else if (_scanMode === "fangliang") {
                document.getElementById("scan-title").textContent = freqLabel + " 放量";
            } else if (_scanMode === "fx_d") {
                document.getElementById("scan-title").textContent = freqLabel + " 底分型";
            } else if (_scanMode === "backtest") {
                document.getElementById("scan-title").textContent = freqLabel + " 回测";
            } else if (_scanMode === "lianzhang") {
                document.getElementById("scan-title").textContent = freqLabel + " " + _scanRecentDays + "连涨";
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
                if (savedMode === "bsp" || savedMode === "ann" || savedMode === "ma" || savedMode === "fx_d" || savedMode === "fangliang" || savedMode === "lianzhang" || savedMode === "backtest") {
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
                        if (v === "zxg" || v === "page_index" || v === "tdxhy2" || v === "tdxhy3" || v === "all_a") {
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
                if (savedFreq && ["w", "d", "30m", "15m", "5m"].indexOf(savedFreq) >= 0) {
                    _scanFreq = savedFreq;
                    var radio = document.querySelector('input[name="scan-freq"][value="' + savedFreq + '"]');
                    if (radio) radio.checked = true;
                }
                // 未显式配置过 → 保持 null（请求不带参，后端用 app_config 默认）；
                // 输入框留空，placeholder 展示后端下发的默认值。
                var savedMc = parseFloat(localStorage.getItem("scan_min_float_mc"));
                if (!isNaN(savedMc) && savedMc >= 0) _scanMinFloatMc = savedMc;
                var mcInput = document.getElementById("scan-min-float-mc");
                if (mcInput) {
                    mcInput.value = _scanMinFloatMc === null ? "" : String(_scanMinFloatMc);
                    if (_scanMinFloatMcServer !== null) mcInput.placeholder = String(_scanMinFloatMcServer);
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
        // 成分股来源的板块指数代码**随本请求传入**（page_index_code），
        // 不再依赖「先 PUT /scan/set/index 写全局、后端再读全局」——多网页下
        // 后设置的页面会覆盖先设置的，导致两页都按同一指数扫错一整批成分股。
        function _fetchMergedStocks(sources, freq, pageIndexCode, scanToken) {
            var url = "/api/stocks/scan/read/candidates?source=" + sources.join(",");
            if (freq) url += "&freq=" + freq;
            if (pageIndexCode) url += "&page_index_code=" + encodeURIComponent(pageIndexCode);
            if (scanToken) url += "&scan_token=" + encodeURIComponent(scanToken);
            // 流通市值过滤阈值随请求走（与 page_index_code 同一多页契约）；0 = 后端关闭过滤且不取市值。
            // 未配置（null）时**不带参**：后端回退 app_config.scan_min_float_mc
            // ——前端一旦写死默认值，后端配置改动就被架空（2026-10-02 修复）。
            if (_scanMinFloatMc !== null) url += "&scan_min_float_mc=" + _scanMinFloatMc;
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
        // 提交体构造（独立成函数，让 bsp_types 的"带 / 不带"规则可被单独断言）：
        //   · `null` / `undefined`（页面四类全勾 = 未启用过滤）→ **不带该字段**，
        //     后端收到 None = 全放行；
        //   · `""`（四类全不勾）→ 如实带上，后端收到 "" = 全部过滤掉
        //     （买/卖点扫描零命中、回测扫描零成交）。
        //   两者语义**相反**，用 `if (opts.bsp_types)` 合并会把"全不勾"静默
        //   变成"全放行" —— 与单页回测 `_btBspTypes()` 是同一条坑（该函数的
        //   口径说明见 App/AppBacktest.py）。买/卖点扫描与回测扫描都消费它。
        function _scanSubmitBody(stocks, opts) {
            var body = {
                stocks: stocks,
                freq: opts.freq || "d",
                mode: opts.mode || "",
                recent: (opts.recent != null) ? String(opts.recent) : "1",
                source: opts.source || "zxg",
                scan_token: opts.scan_token || ""
            };
            if (opts.bsp_types !== undefined && opts.bsp_types !== null) {
                body.bsp_types = opts.bsp_types;
            }
            return body;
        }

        function _asyncScanAll(stocks, opts, onData, onDone) {
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
                body: JSON.stringify(_scanSubmitBody(stocks, opts))
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
        // 辅助：市场分布摘要（上海/深圳/北京/香港 计数），fx_d / fangliang / bsp 共用
        function _scanMarketSummaryHtml(results) {
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
            return '<div class="scan-summary" style="margin-top:8px;">' + marketParts.join("，") + '</div>';
        }

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
                        var freqLabelMap = {"d": "日K", "w": "周K", "30m": "30分", "15m": "15分", "5m": "5分", "60m": "60分", "1m": "1分", "15s": "15秒"};
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

            // fx_d / ma / fangliang / bsp 四种扫描模式共用同一条执行管线
            // （原 4 个近复制分支，各含 3 个重复闭包，共 12 份近似副本）。
            // 差异收敛为 spec：mode 字符串 + 结果分类谓词 + 进度/结果渲染函数。
            var runScan = function(spec) {
                var sourceLabel = _scanSourceLabel();
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取：' + sourceLabel + '...</div>';
                // ① 板块指数代码**随请求传入**（page_index_code），
                // 不再「先 PUT /scan/set/index 写后端全局、后端再读全局」——
                // 多网页下后设置的页面会覆盖先设置的，两页会按同一指数扫，
                // 静默扫错一整批成分股；② start() 返回本次扫描私有的
                // scan_token，后端按它隔离开始时间与跳过记录，本页的扫描
                // 不会被另一页覆盖。
                var pageIndexCode = (_scanSources.indexOf("page_index") >= 0
                        && chartData && chartData.meta && chartData.meta.symbol)
                        ? chartData.meta.symbol : "";
                var scanToken = "";
                var startUrl = "/api/stocks/scan/start";
                if (pageIndexCode) startUrl += "?page_index_code=" + encodeURIComponent(pageIndexCode);
                Promise.all([
                    fetch(startUrl, { method: "POST" }),
                    _fetchMergedStocks(_scanSources, freq, pageIndexCode)
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
                            // 记住本次扫描的会话标识，供 submit / end 携带
                            scanToken = scanStartData.scan_token || "";
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

                        body.innerHTML = spec.initialSummary(preSkipped, total);

                        function finishScan(interrupted) {
                            // 先清掉进度节流定时器，避免其 500ms 内的最后一次 _doUpdatePanel
                            // 把"正在扫描"写回 body，覆盖 render* 的结果（spinner 残留）
                            if (_updateTimer) { clearInterval(_updateTimer); _updateTimer = null; }
                            _pendingUpdate = false;
                            // 带 scan_token：结算本页这一次扫描
                            var endUrl = "/api/stocks/scan/end"
                                + (scanToken ? "?scan_token=" + encodeURIComponent(scanToken) : "");
                            fetch(endUrl, { method: "POST" }).then(function() {
                                spec.renderFinal(results, total + preSkipped, preSkipped + skipped, interrupted);
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
                            var html = spec.progressLine(completed, total, preSkipped, skipped, results);
                            if (results.length > 0) {
                                html += spec.renderRows(results);
                            }
                            body.innerHTML = html;
                            updateScanSaveBtn();
                        }

                        // 提交到后端执行池，轮询增量结果
                        // （单票响应同形，模式过滤/渲染逻辑零改动）
                        btn.textContent = "中断扫描";
                        _asyncScanAll(stocks, {freq: freq, mode: spec.mode, recent: spec.recent, source: _scanSources.join(","), bsp_types: spec.bsp_types, scan_token: scanToken}, function(data) {
                            completed++;
                            if (data.skipped) { skipped++; }
                            else if (data.error) { skipped++; }
                            else if (spec.classify(data)) {
                                results.push(spec.normalize ? spec.normalize(data) : data);
                            }
                            updatePanel();
                        }, function(err, interrupted) {
                            if (err) {
                                console.error("[" + spec.errLabel + "] " + err);
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
            };

            // fx_d：底分型扫描（最后一个分型是底分型的个股）
            if (_scanMode === "fx_d") {
                runScan({
                    mode: "fx_d",
                    recent: _scanRecentDays,
                    errLabel: "底分型扫描",
                    initialSummary: function(preSkipped, total) {
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，底分型 0 只（0 / 0 / 0）</div>';
                    },
                    progressLine: function(completed, total, preSkipped, skipped, results) {
                        var strongest = 0, strong = 0, weak = 0;
                        for (var i = 0; i < results.length; i++) {
                            var s = results[i].fx_strength;
                            if (s === 2) { strongest++; }
                            else if (s === 1) { strong++; }
                            else { weak++; }
                        }
                        var fxSummary = results.length + ' 只（' + strongest + ' / ' + strong + ' / ' + weak + '）';
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + (completed + "/" + total) + '，跳过 ' + (preSkipped + skipped) + ' 只，底分型 ' + fxSummary + '</div>';
                    },
                    classify: function(data) { return !!data.is_fx_d; },
                    renderRows: function(results) {
                        var html = _scanMarketSummaryHtml(results);
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
                        return html;
                    },
                    renderFinal: function(results, total, skipped, interrupted) {
                        renderFxDScanResults(results, total, skipped, interrupted);
                    }
                });
                return;
            }

            // ma：均线分类扫描（按最新收盘价未攻克的最小周期均线分类）
            if (_scanMode === "ma") {
                runScan({
                    mode: "ma",
                    recent: "1",
                    errLabel: "均线分类扫描",
                    initialSummary: function(preSkipped, total) {
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，命中 0 只</div>';
                    },
                    progressLine: function(completed, total, preSkipped, skipped, results) {
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + (completed + "/" + total) + '，跳过 ' + (preSkipped + skipped) + ' 只，命中 ' + results.length + ' 只</div>';
                    },
                    classify: function(data) { return data.ma_category !== undefined && data.ma_category >= 0; },
                    normalize: function(data) {
                        return { code: data.code, name: data.name, ma_category: data.ma_category, last_close: data.last_close };
                    },
                    renderRows: function(results) {
                        var html = '';
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
                        return html;
                    },
                    renderFinal: function(results, total, skipped, interrupted) {
                        renderMaScanResults(results, total, skipped, interrupted);
                    }
                });
                return;
            }

            // backtest：回测扫描 —— 对「扫描来源」内每票，按「扫描周期」跑一遍与
            // 页面「回测」按钮**同一套内核**（后端 scan_one 复用同一份 klines 加载
            // 序列调 AppBacktest），结果按期望值(%/笔)从大到小排。
            if (_scanMode === "backtest") {
                runScan({
                    mode: "backtest",
                    // recent 对本模式无意义（弹窗里该输入框已置灰）：整条加载序列
                    // 参与回测。这里传 "1" 只是满足提交体的字段形态，后端忽略它。
                    recent: "1",
                    errLabel: "回测扫描",
                    // 买卖点类型过滤：与页面「回测」按钮同一口径（§4.7.3 共用
                    // bspFilter）。`null`（四类全勾 = 未启用过滤）不带该字段，
                    // `""`（全不勾）如实带 —— 二者语义相反，见 _scanSubmitBody。
                    bsp_types: _btBspTypes(),
                    initialSummary: function(preSkipped, total) {
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在回测 0/' + total + '，跳过 ' + preSkipped + ' 只，有成交 0 只</div>';
                    },
                    progressLine: function(completed, total, preSkipped, skipped, results) {
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在回测 ' + (completed + "/" + total) + '，跳过 ' + (preSkipped + skipped) + ' 只，有成交 ' + results.length + ' 只</div>';
                    },
                    // 只收「至少 1 笔成交」的票（用户裁定）：0 笔的票没有期望值可读，
                    // 混在列表里只会稀释信噪比。"跳过"仍按既有口径单列。
                    classify: function(data) { return (data.filled || 0) >= 1; },
                    renderRows: function(results) { return _renderBacktestRows(results); },
                    renderFinal: function(results, total, skipped, interrupted) {
                        renderBacktestScanResults(results, total, skipped, interrupted);
                    }
                });
                return;
            }

            // fangliang：放量扫描（最近 N 根内成交额最大者为 A，且 A 大于前 W 根峰值）
            //   W = 后端 app_config.SCAN_FANGLIANG_WINDOW_BARS（默认 120），经
            //   _scanFangliangWindowBars 下发给面板的**披露文案**；判定在后端，
            //   前端不重算 —— 两处口径必须同一句话（见 _fangliangCaliberHtml）。
            if (_scanMode === "fangliang") {
                runScan({
                    mode: "fangliang",
                    recent: _scanRecentDays,
                    errLabel: "放量扫描",
                    initialSummary: function(preSkipped, total) {
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，放量 0 只</div>';
                    },
                    progressLine: function(completed, total, preSkipped, skipped, results) {
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + (completed + "/" + total) + '，跳过 ' + (preSkipped + skipped) + ' 只，放量 ' + results.length + ' 只</div>';
                    },
                    classify: function(data) { return !!data.is_fangliang; },
                    renderRows: function(results) {
                        var html = _scanMarketSummaryHtml(results);
                        // 进度期同样挂口径披露：与终态共用 _fangliangCaliberHtml()，
                        // 两处措辞只可能同源（进度期就能读到判据，不必等扫完）。
                        html += _fangliangCaliberHtml();
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
                        return html;
                    },
                    renderFinal: function(results, total, skipped, interrupted) {
                        renderFangliangScanResults(results, total, skipped, interrupted);
                    }
                });
                return;
            }

            // lianzhang：连涨扫描（最近 N 根 K 线逐根收红；口径 = K 线图红色，见后端
            //   AppScan.scan_one 的 lianzhang 分支，两处判据必须同一句话）
            if (_scanMode === "lianzhang") {
                runScan({
                    mode: "lianzhang",
                    recent: _scanRecentDays,
                    errLabel: "连涨扫描",
                    initialSummary: function(preSkipped, total) {
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，连涨 0 只</div>';
                    },
                    progressLine: function(completed, total, preSkipped, skipped, results) {
                        return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + (completed + "/" + total) + '，跳过 ' + (preSkipped + skipped) + ' 只，连涨 ' + results.length + ' 只</div>';
                    },
                    classify: function(data) { return !!data.is_lianzhang; },
                    renderRows: function(results) { return _renderLianzhangRows(results); },
                    renderFinal: function(results, total, skipped, interrupted) {
                        renderLianzhangScanResults(results, total, skipped, interrupted);
                    }
                });
                return;
            }

            // 买卖点扫描模式（默认）：按最新买卖点类型排序，先买点后卖点
            runScan({
                mode: "",
                recent: _scanRecentDays,
                errLabel: "买卖点扫描",
                // 买卖点类型过滤：与「设置 → 买卖点类型」同口径（2026-10-06 用户
                // 拍板）—— 判据在后端复用 Backtest.Filter.bsp_type_allowed（与
                // Trading 引擎 `_bsp_type_allowed` 逐字同源），故**图上画的 =
                // 扫描扫的 = 自动下单用的**。与「回测扫描」走同一个 _btBspTypes()，
                // 两个模式因此行为一致。四态语义见 _scanSubmitBody。
                bsp_types: _btBspTypes(),
                initialSummary: function(preSkipped, total) {
                    return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，跳过 ' + preSkipped + ' 只，买点 0 只，卖点 0 只</div>';
                },
                progressLine: function(completed, total, preSkipped, skipped, results) {
                    var buyCount = 0, sellCount = 0;
                    for (var i = 0; i < results.length; i++) {
                        if (isLatestBspBuy(results[i])) { buyCount++; } else { sellCount++; }
                    }
                    return '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + (completed + "/" + total) + '，跳过 ' + (preSkipped + skipped) + ' 只，买点 ' + buyCount + ' 只，卖点 ' + sellCount + ' 只</div>';
                },
                classify: function(data) {
                    return (data.buy_points && data.buy_points.length > 0) || (data.sell_points && data.sell_points.length > 0);
                },
                renderRows: function(results) {
                    var html = _scanMarketSummaryHtml(results);
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
                    return html;
                },
                renderFinal: function(results, total, skipped, interrupted) {
                    renderScanResults(results, total, skipped, interrupted);
                }
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
                    showAlert("启动刷新失败: " + err.message);
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
                            showAlert("刷新失败: " + data.error);
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
            // 买卖点类型口径披露：扫描按它做**事前过滤**（后端同一门）。
            //   不写出来的话，勾选被改过 / 全不勾时用户只会看到"图上有买卖点、
            //   扫描却没扫到"，然后当成 bug 来查。
            //   措辞必须点明「事前」：被拒类型**不参与**"最新买卖点是买还是卖"的
            //   判定（§4.7.3 定案）—— 说成"结果里过滤掉"是错的，会误导用户以为
            //   放行类型的命中不受勾选影响。
            html += '<div class="scan-summary" style="font-size:10px;color:#7a8399;">'
                + '买卖点类型：' + statsEsc(_btBspTypesLabel(_btBspTypes()))
                + ' · 未勾选的类型不参与判定（事前过滤，非事后过滤）</div>';
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

        // 放量扫描口径披露行 —— 与连涨披露行（renderLianzhangScanResults）**同构**：
        //   同一 class（scan-summary）+ 同一字号/颜色，面板上两种模式的解释读起来是一套。
        //
        //   为什么必须写：放量判据不是「最近 N 根天天放量」，而是「最近 N 根里**最猛
        //   的那一根**，比它前面的比较窗口内任何一根都猛」—— 这条不看代码推不出来。
        //   不写清楚，用户看到图上某一根明明是巨量柱却没被扫出来（那根不在最近 N 根
        //   内 / 或没超过前窗峰值），只会当成 bug 来查。
        //
        //   窗口根数取自 _scanFangliangWindowBars（SSOT = 后端 SCAN_FANGLIANG_WINDOW_BARS），
        //   **不在文案里硬编码 120**；未拉到（离线/旧缓存）时回落中性措辞，宁可不给数字
        //   也不给错的数字。窗口根数、比较基准、数据不足的跳过语义三者都要说全。
        function _fangliangCaliberHtml() {
            var w = _scanFangliangWindowBars;
            var windowTxt = (typeof w === "number" && w >= 1)
                ? ('其前 ' + w + ' 根')
                : '其前一段比较窗口';
            return '<div class="scan-summary" style="font-size:10px;color:#7a8399;">'
                + '最近 ' + _scanRecentDays + ' 根 · 判据：其中成交额最大的一根'
                + ' > ' + windowTxt + '的最高成交额（成交额创新高即放量，比较窗口不足则不参评）'
                + '</div>';
        }

        // 放量扫描结果渲染
        function renderFangliangScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total + '</b> 只，跳过 <b>' + skipped + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，放量 <b>' + results.length + '</b> 只' + label + '</div>';
            // 口径披露：判据 + 比较窗口根数（SSOT 在后端配置）。
            //   同连涨披露行一样，把「凭什么算放量」写在脸上，防「明明是巨量柱却没扫到」被当 bug。
            html += _fangliangCaliberHtml();
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

        // 连涨标签HTML：口径 = K 线红色（close > open），与 K 线图红柱同色
        function buildLianzhangTagHtml(data) {
            var n = data.recent_days || 0;
            return '<span class="scan-bsp-tag fl-rise">' + n + '连涨</span>';
        }

        // 连涨结果行渲染（进度期与终态共用同一份，避免两处漂移）
        //   列：股票名 · 代码 ·「N连涨」标签 · 区间涨幅（降序，涨红跌绿）
        //   涨幅口径 = K 线图底部**十字白框**：悬停窗口首根时的读数（基期 = 首根
        //   前收，终点 = 末根收盘）—— 与后端 gain_pct 同一口径，**不是**首根开盘。
        //   勾选：命中即勾 —— 本模式的每一行都已满足判据，不存在「命中但有强弱之分」。
        function _renderLianzhangRows(results) {
            var html = _scanMarketSummaryHtml(results);
            // 按区间累计涨幅降序：涨幅最大的排最前
            results.sort(function(a, b) { return (b.gain_pct || 0) - (a.gain_pct || 0); });
            for (var i = 0; i < results.length; i++) {
                var r = results[i];
                var g = Number(r.gain_pct || 0);
                html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="点击查看K线图">';
                html += chkBox(r.code, true);
                html += '<span class="scan-col-name">' + r.name + '</span>';
                html += '<span class="scan-col-code">' + r.code + '</span>';
                html += '<span class="scan-col-tags">' + buildLianzhangTagHtml(r) + '</span>';
                html += '<span class="scan-col-expect" style="color:' + _btCol(g) + '">' + _btPct(g) + '</span>';
                html += '</div>';
            }
            return html;
        }

        // 连涨扫描结果渲染（终态）
        function renderLianzhangScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total + '</b> 只，跳过 <b>' + skipped + '</b> 只，扫描 <b>' + (total - skipped) + '</b> 只，连涨 <b>' + results.length + '</b> 只' + label + '</div>';
            // 口径披露：判据与 K 线红色同源（收盘 > 开盘，平盘白线不算）；
            //   涨幅与 K 线图底部**十字白框**同源 —— 悬停窗口首根时的读数
            //   （基期 = 首根的前一根收盘，终点 = 末根收盘），**不是**首根开盘。
            //   不写清楚必被当成「图上明明是红的却没扫到」或「涨幅跟白框对不上」来查。
            html += '<div class="scan-summary" style="font-size:10px;color:#7a8399;">'
                + '最近 ' + _scanRecentDays + ' 根 · 判据：逐根收红（收盘 > 开盘，平盘不算）'
                + ' · 涨幅 = 首根的前一根收盘 → 末根收盘（同 K 线图底部白框读数，非首根开盘）</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现连涨标的</div>';
            } else {
                html += _renderLianzhangRows(results);
            }
            body.innerHTML = html;
            updateScanSaveBtn();
        }

        // ── 回测扫描（backtest 模式）────────────────────────────────────
        // 买卖点类型过滤的中文披露：取值直接来自页面的 _btBspTypes()，**不重算** ——
        //   重算一份必然与提交给后端的那个串漂移（那里才是真口径）。
        function _btBspTypesLabel(types) {
            if (types === null || types === undefined) return "全部（未启用过滤）";
            if (types === "") return "全不勾（不会有任何成交）";
            return String(types).split(",").map(function(t) {
                return t.trim() + "类";
            }).join("、");
        }

        // 期望值取值：后端 `avg_net_return_pct` 已是百分数（已 ×100，见
        //   App/AppBacktest._pct），前端不再乘。区分 `null`（一笔都没平仓 ⇒
        //   期望值不可算）与 0（可算且为零）—— 排序与配色都要分开处理。
        function _btExpect(r) {
            var v = (r || {}).avg_net_return_pct;
            return (v === null || v === undefined) ? null : Number(v);
        }

        // 结果行渲染（进度期与终态**共用同一份**，避免两处漂移）
        //   列：股票名 · 代码 · 笔数 · 期望值(%/笔)
        //   排序：期望值从大到小；`null`（一笔都没平仓）沉底，不混在有值中间。
        function _renderBacktestRows(results) {
            var html = _scanMarketSummaryHtml(results);
            results.sort(function(a, b) {
                var x = _btExpect(a), y = _btExpect(b);
                if (x === null && y === null) return 0;
                if (x === null) return 1;                 // 无值一律排在有值之后
                if (y === null) return -1;
                return y - x;
            });
            for (var i = 0; i < results.length; i++) {
                var r = results[i];
                var exp = _btExpect(r);
                // 区间 / 笔数拆解放悬停：列宽有限，塞进行里会挤掉期望值
                var tip = (r.date_from && r.date_to)
                    ? ("回测区间 " + r.date_from + " ~ " + r.date_to + "（" + r.bars + " 根）"
                       + "，成交 " + r.filled + " 笔（已平 " + r.closed + " / 持仓 " + r.still_open + "）")
                    : "点击查看K线图";
                html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + _scanFreq + '\')" title="' + statsEsc(tip) + '">';
                // 默认勾选规则（用户裁定）：期望值 > 0 —— 跑完一键把正期望的票存进自选。
                //   无值（还有未平仓笔）**不勾**：那是"还没结果"，不是"结果为正"。
                html += chkBox(r.code, exp !== null && exp > 0);
                html += '<span class="scan-col-name">' + statsEsc(r.name || r.code) + '</span>';
                html += '<span class="scan-col-code">' + statsEsc(r.code) + '</span>';
                html += '<span class="scan-col-btcount">' + r.filled + ' 笔</span>';
                html += '<span class="scan-col-expect" style="color:'
                    + (exp === null ? "#8b93a7" : _btCol(exp)) + '">'
                    + (exp === null ? "—" : _btPct(exp)) + '</span>';
                html += '</div>';
            }
            return html;
        }

        // 回测扫描结果渲染（终态）
        function renderBacktestScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var freqLabels = {"d": "日K", "w": "周K", "30m": "30分", "15m": "15分", "5m": "5分"};
            var freqLabel = freqLabels[_scanFreq] || _scanFreq;
            var posCount = 0, naCount = 0;
            for (var i = 0; i < results.length; i++) {
                var e = _btExpect(results[i]);
                if (e === null) { naCount++; }
                else if (e > 0) { posCount++; }
            }
            var html = '<div class="scan-summary">' + sourceLabel + ' <b>' + total
                + '</b> 只，跳过 <b>' + skipped + '</b> 只，回测 <b>' + (total - skipped)
                + '</b> 只，有成交 <b>' + results.length + '</b> 只（期望 > 0 <b>' + posCount + '</b> 只'
                + (naCount > 0 ? '，未平仓 ' + naCount + ' 只' : '') + '）' + label + '</div>';
            // 口径披露行：**必须披露** —— 扫描回测取的是后端 lookback 的最新 N 根，
            //   而页面若处「复盘 / 选点」态，单页回测的加载序列会被截断，同一票两处
            //   数字会不一样；不写清楚，日后必然被当成"回测有 bug"来查。
            if (results.length > 0) {
                var r0 = results[0];
                html += '<div class="scan-summary" style="font-size:10px;color:#7a8399;">'
                    + freqLabel + ' · 区间：最新 ' + r0.bars + ' 根（截至 '
                    + statsEsc(r0.date_to || "—") + '）'
                    + ' · 买卖点类型：' + statsEsc(_btBspTypesLabel(_btBspTypes()))
                    + '</div>';
            }
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下没有股票产生成交'
                    + (skipped > 0 ? '（另有 ' + skipped + ' 只被跳过）' : '') + '</div>';
            } else {
                html += _renderBacktestRows(results);
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



// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] RealtimeService —— 实时行情服务组件（期货 SSE 连接 / 增量上屏）

// ══════════════════════════════════════════════════════════════════

        // ========== 期货实时模式 ==========
        function startRealtimeIfFutures(data) {
            // 检查是否是期货/期指品种（股票路径中用于断开SSE，期货路径中用于连SSE）
            const isFutures = data.meta.market === 'futures';
            const badge = document.getElementById('realtime-badge');

            if (isFutures) {
                const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','15分钟':'15m','日线':'d','周线':'w'};
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
            replayPending = !!endTime;         // 复盘挂起（声明处有完整说明）
            isRealtimeMode = true;
            startCountdownTimer();
            // 复盘挂起要**立即**消失：stopCountdownTimer（disconnectRealtime 内）只把
            // 边界归 null，上一秒画在画布上的进度条像素还在 —— 补一次整图重绘擦掉它；
            // 此后每秒零空转（判据 null + 边界 null ⇒ _redrawCountdown 直接 return）。
            if (replayPending) render();
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';
            // loading 由调用方（loadStock/switchFreq）已设置
            syncAutoOrderWrap();

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
                            // 后端 init 错误必须让用户看见：如「复盘起始时间…不早于
                            // 复盘截止时间」与股票同款弹窗（原 console.warn 只让
                            // loading 消失，用户看不出发生了什么）
                            disconnectRealtime();
                            document.getElementById("loading").classList.add("hidden");
                            showAlert(data.error);
                            return;
                        }
                        // 全量初始数据
                        chartData = data;
                        // 复盘数据已落地 ⇒ 解除挂起（判据回到「末根是否正在走」：
                        // 复盘态末根冻结在复盘点 ⇒ 仍不显示；回实时/取消复盘 ⇒ 正常显示）
                        replayPending = false;
                        // 用后端解析后的完整代码保存历史，避免别名导致历史记录不一致
                        const resolvedSymbol = data.meta.symbol || symbol;
                        saveHistory(resolvedSymbol, data.meta.name);
                        // 同步 realtimeSymbol 为解析后的完整代码
                        realtimeSymbol = resolvedSymbol;
                        // ⚠️ realtimeSymbol 变了必须跟着重算品种键（2026-10-09 用户反馈）：
                        //   autoOrderTradableFor 只在 syncAutoOrderWrap →
                        //   refreshAutoOrderTradable 里随 realtimeSymbol 更新。init 改写
                        //   symbol 后不补这一拍，autoOrderPageKey() 会一直返回空串 ⇒ 本页
                        //   绑定失效 ⇒ running 假 false：① 弹假的「交易引擎已退出!」；
                        //   ② §3.3「请先关闭，再复盘/选点」守卫静默放行（引擎仍在跑）。
                        syncAutoOrderWrap();
                        // 更新输入框为解析后的完整代码
                        document.getElementById("stock-code-input").value = resolvedSymbol;
                        updateRestartBtn();
                        updateDualBtn();
                        // 同步周期
                        const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','15分钟':'15m','日线':'d','周线':'w'};
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
                    replayPending = false;   // 断线 ⇒ 解除复盘挂起，绝不永久隐藏倒计时
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
        function connectRealtimeDual(symbol, mainFreq, subFreq, endTime, startTime, subStartTime) {
            disconnectRealtime();
            realtimeSymbol = symbol;
            realtimeFreq = mainFreq;
            dualSubFreq = subFreq;
            realtimeStartTime = startTime || null;
            realtimeEndTime = endTime || null; // 复盘软断开边界
            replayPending = !!endTime;         // 复盘挂起（声明处有完整说明）
            isRealtimeMode = true;
            startCountdownTimer();
            if (replayPending) render();       // 立即擦掉上一秒画的进度条（同单窗）
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';

            try {
                let sseUrl = '/api/futures/read/stream?symbol=' + encodeURIComponent(symbol)
                    + '&freq=' + mainFreq + '&dual=1&sub_freq=' + subFreq;
                if (startTime) {
                    // B 操作双窗选点（四期独立选点）：上窗/下窗各自的窗口左边界
                    sseUrl += '&start_time=' + encodeURIComponent(startTime);
                }
                if (subStartTime) {
                    sseUrl += '&sub_start_time=' + encodeURIComponent(subStartTime);
                }
                if (endTime) {
                    sseUrl += '&end_time=' + encodeURIComponent(endTime);
                }
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                realtimeEventSource.addEventListener('init', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.error) {
                            // 双窗同单窗：init 错误（含任一窗复盘起止倒挂）必须弹窗
                            disconnectRealtime();
                            document.getElementById("loading").classList.add("hidden");
                            showAlert(data.error);
                            return;
                        }
                        if (data.main) {
                            chartData = data.main;
                            replayPending = false;   // 复盘数据已落地 ⇒ 解除挂起（同单窗）
                            const resolvedSymbol = chartData.meta.symbol || symbol;
                            saveHistory(resolvedSymbol, chartData.meta.name);
                            realtimeSymbol = resolvedSymbol;
                            // 品种键随 realtimeSymbol 重算（同单窗：不补这一拍会让
                            // autoOrderPageKey() 恒空 ⇒ 绑丢 ⇒ 假「已退出」+ 守卫放行）
                            syncAutoOrderWrap();
                            updateRestartBtn();
                            updateDualBtn();
                            const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','15分钟':'15m','日线':'d','周线':'w'};
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
                    replayPending = false;   // 断线 ⇒ 解除复盘挂起，绝不永久隐藏倒计时
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
            syncAutoOrderWrap();

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
                    replayPending = false;   // 断线 ⇒ 解除复盘挂起，绝不永久隐藏倒计时
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
            replayPending = false;   // 离开实时链 ⇒ 解除复盘挂起（复位路径之一）
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
            syncAutoOrderWrap();
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
                const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','15分钟':'15m','日线':'d'};
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
                        const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','15分钟':'15m','日线':'d'};
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
                // 修复上窗空白：多窗共享全局 viewOffset，旧序列比新序列长时会令偏移越界，
                // 越界使 getVisibleKlines 切出空切片 → 上窗静默空白。按新 main 长度夹紧。
                if (newMainCount > 0) {
                    viewOffset = (newMainCount < viewCount)
                        ? 0
                        : Math.max(0, Math.min(newMainCount - viewCount, viewOffset));
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
                // 修复下窗空白：同主窗，按新 sub 长度夹紧视口偏移
                if (newSubCount > 0) {
                    dualSubViewOffset = (newSubCount < dualSubViewCount)
                        ? 0
                        : Math.max(0, Math.min(newSubCount - dualSubViewCount, dualSubViewOffset));
                }
            }
            updateSlider();
            render();
        }




// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] AnnotationPanel —— 文字标注组件（标注 CRUD / 右键菜单 / 弹窗）

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
                    showAlert("未找到该标注，可能标注存在于其他周期下。\n当前周期: " + freq + "\n请切换到添加标注时使用的周期再试。");
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
            const menuTpsl = document.getElementById("annotation-menu-tpsl");
            const menuTpslCancel = document.getElementById("annotation-menu-tpsl-cancel");
            const menuDivider3 = document.getElementById("annotation-menu-divider3");
            // 更新翻转视图菜单项文字（显示当前状态）
            menuMirror.textContent = _isMirrorMode ? "取消翻转" : "翻转视图";
            // 更新复盘菜单项文字：复盘激活期间变「取消复盘」（meta.is_replay：
            // 股票=请求带 end_date，期货=SSE end_time 软断开，两种复盘入场共用）
            const _replayActive = !!(chartData && chartData.meta && chartData.meta.is_replay);
            menuReplay.textContent = _replayActive ? "取消复盘" : "复盘至此";
            if (_annotationClickTarget) {
                menuDeleteOne.style.display = "block";
                menuEditOne.style.display = "block";
                menuAdd.style.display = "none";
                menuRestart.style.display = "none";
                menuReplay.style.display = "none";
                // 复盘组已移到菜单末尾：div1 下面恒接翻转视图，恒显示（不再与 div2 相邻成双线）
                menuDivider.style.display = "block";
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
            // 翻转视图始终显示（与标注操作无关，是全局视图模式）；
            // 复盘激活时「取消复盘」同样始终显示（全局状态，点中标注也要能退出复盘）
            menuMirror.style.display = "block";
            if (_replayActive) menuReplay.style.display = "block";

            // 「止盈止损」仅股票页 + 命中 0123 类买卖点的 K 线显示（P2-1）；
            // 激活期间同根右键换成「取消盈损」（N1 方案 b：归一/取首/warn 在前端）
            const _tpslHit = !isFuturesMode() &&
                _tpslBspCandidates(_annotationTargetDate).length > 0;
            const _replayShown = menuReplay.style.display !== "none";
            // 菜单序（复盘组在末尾）：… 翻转视图 |div2| 止盈止损 |div3| 复盘组；
            // 分隔线只在两侧都有可见项时显示，避免悬空线
            menuDivider2.style.display = (_tpslHit || _replayShown) ? "block" : "none";
            menuDivider3.style.display = (_tpslHit && _replayShown) ? "block" : "none";
            menuTpsl.style.display = (!_tpslActive && _tpslHit) ? "block" : "none";
            menuTpslCancel.style.display = (_tpslActive && _tpslHit) ? "block" : "none";
            if (_tpslHit) menuTpsl.textContent = _tpslActive ? "取消盈损" : "止盈止损";

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
        // 复盘激活期间（meta.is_replay）本项显示为「取消复盘」：填今天走 gotoDate 回到
        // 正常状态（股票=isToday 冷启动不带 end_date；期货=wantLive 恢复实时SSE），
        // 与日历选「今天」同一条路径
        window.annotationReplayToHere = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (chartData && chartData.meta && chartData.meta.is_replay) {
                var nowC = new Date();
                var todayC = nowC.getFullYear() + '-' + String(nowC.getMonth()+1).padStart(2,'0') + '-' + String(nowC.getDate()).padStart(2,'0');
                var isFutC = chartData.meta.market === 'futures';
                var inpC = document.getElementById("goto-date-input");
                // datetime-local 补时间：期货 23:59 保证 wantLive（与 handleDateBlur「今天」兜底同款），股票 15:59 为盘中上限
                inpC.value = isIntradayFreq(currentFreq) ? (todayC + (isFutC ? 'T23:59' : 'T15:59')) : todayC;
                if (typeof updateWeekday === "function") updateWeekday();
                gotoDate();  // 不设 _dateKeyEnter：让 isToday/wantLive 判定走「回最新」分支
                return;
            }
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
                    showAlert("无法识别该K线日期: " + _annotationTargetDate);
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
        //   确认走 showConfirm（自实现模态，与全站同一个框）：点框外 / Esc 都＝取消，
        //   与原生 confirm 的语义一致 —— 换自实现只改观感与样式，不改「怎样才算删」。
        window.annotationDeleteAllGlobal = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            showConfirm("确定删除当前股票 (" + code + ") " + freq + " 周期下的全部标注吗？")
            .then(function(ok) {
                if (!ok) return;
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
            });
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
                showAlert("请输入标注文字");
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




// ══════════════════════════════════════════════════════════════════
        // [COMPONENT] Bootstrap —— 应用引导（首屏 init / 状态持久化 / 全局监听注册）

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

        // ════════════════════════════════════════════════════════════════
        // [会话级视图恢复 fix#1-4] 仅作用于"刷新当前页"；冷启动保持现状（股票单窗口）
        // 用 sessionStorage（只在当前标签会话存活）保存"上次看到的状态"，
        // 从而：同一标签刷新（含睡眠→唤醒后刷新）→ 精确恢复到 股票/期货、
        // 单/双窗口、品种、周期、下窗周期；全新打开软件（新会话）→ sessionStorage
        // 为空 → 走原逻辑（localStorage 股票 / initDefault 上证指数）。复盘不保存。
        // ════════════════════════════════════════════════════════════════

        function saveLastView() {
            if (!chartData || !chartData.meta) return;
            if (chartData.meta.is_replay) return; // 复盘模式不保存（跨刷新不恢复）
            const view = {
                market: chartData.meta.market === 'futures' ? 'futures' : 'stock',
                symbol: chartData.meta.symbol,
                name: chartData.meta.name || '',
                freq: currentFreq,
                dual: !!isDualWindow,
                subFreq: dualSubFreq || ''
            };
            try { sessionStorage.setItem('chan_last_view', JSON.stringify(view)); } catch(e) {}
        }

        function loadLastView() {
            try {
                const raw = sessionStorage.getItem('chan_last_view');
                if (!raw) return null;
                const v = JSON.parse(raw);
                if (!v || !v.market || !v.symbol || !v.freq) return null;
                return v;
            } catch(e) { return null; }
        }

        // 恢复失败/超时的统一兜底：回退到默认股票单窗口（initDefault）
        function _resumeFallback() {
            console.error("会话恢复失败，回退到默认加载（股票单窗口）");
            try { disconnectRealtime(); } catch(e) {}
            initDefault();
        }

        // 全局看门狗：恢复启动后若长时间拿不到 chartData（网络异常/代码失效），
        // 兜底回退到默认，避免页面长期空白或停在"断开"。
        const _resumeWatchdog = (function(){
            let started = false;
            return function() {
                if (started) return;
                started = true;
                const deadline = Date.now() + 25000;
                (function tick() {
                    if (chartData) return;                    // 已成功，交给各自流程
                    if (Date.now() > deadline) {              // 超时兜底
                        started = false;
                        _resumeFallback();
                        return;
                    }
                    setTimeout(tick, 600);
                })();
            };
        })();

        // 双窗恢复：等单窗 chartData 就绪后，预置下窗周期，复用 toggleDualWindow 进入双窗
        // （toggleDualWindow 内部完成双窗 DOM 创建、期货双窗 SSE / 股票双窗请求的全部既有逻辑）
        function _scheduleResumeDual(view) {
            const deadline = Date.now() + 15000;
            (function poll() {
                if (!chartData || !chartData.meta) {
                    if (Date.now() > deadline) _resumeFallback();
                    else setTimeout(poll, 250);
                    return;
                }
                try {
                    const wantSub = view.subFreq || getDualSubFreq(view.freq) || '';
                    if (wantSub) dualSubFreq = wantSub;
                    if (window.toggleDualWindow) window.toggleDualWindow();
                } catch(e) {
                    console.error("恢复双窗口失败:", e);
                    _resumeFallback();
                }
            })();
        }

        // 股票单窗恢复加载（含渲染/历史/统计，与冷启动股票路径同参同语义）
        function _loadStockForResume(view) {
            const code = view.symbol, freq = view.freq;
            document.getElementById("loading").classList.remove("hidden");
            updateFreqButtonStates(false);
            updateDateInputType();
            const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号（恢复加载与用户操作竞争时让位）
            fetch("/api/stocks/" + encodeURIComponent(code) + "/analyze?freq=" + freq, { cache: "no-store" })
                .then(resp => { if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); }); return resp.json(); })
                .then(data => {
                    if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
                    if (!data || !data.meta) throw new Error(data && data.error ? data.error : "API 返回数据缺少 meta 字段");
                    chartData = data;
                    document.getElementById("stock-code-input").value = chartData.meta.symbol;
                    saveHistory(chartData.meta.symbol, chartData.meta.name);
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    let rf = "d";
                    if (data.meta.freq === "5分钟") rf = "5m";
                    else if (data.meta.freq === "30分钟") rf = "30m";
                    else if (data.meta.freq === "15分钟") rf = "15m";
                    else if (data.meta.freq === "周线") rf = "w";
                    currentFreq = rf; lastStockFreq = rf;
                    updateDateInputType(); updateFreqButtonStates(false);
                    viewCount = VIEW_COUNT; adjustViewForSavedPoint();
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    if (chartData.klines.length < viewCount) viewOffset = 0;
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday(); initialized = true;
                    applyOverlayButtonStates(); updateRestartBtn(); updateDualBtn();
                    render();
                    document.getElementById("loading").classList.add("hidden");
                    document.getElementById("error").classList.add("hidden");
                    generateStats(); loadAnnotations();
                    disconnectRealtime();
                    if (view.dual) _scheduleResumeDual(view);   // 数据就绪后进入双窗
                    saveLastView();
                })
                .catch(err => {
                    if (_isChartActionStale(_seq)) return; // [N1] 过期恢复请求不得触发回退
                    console.error("恢复上次状态失败，回退默认:", err);
                    _resumeFallback();
                });
        }

        // 会话视图入口：按 market + dual 分派到对应既有加载/连接流
        function _restoreView(view) {
            initCanvas(); // 建立图表 canvas 与事件（与冷启动一致，供后续 render 使用）
            document.getElementById("stock-code-input").value = view.symbol;
            document.getElementById("loading").classList.remove("hidden");
            if (view.market === 'futures') {
                // 期货：init 回调内置 chartData/周期/按钮/渲染全套；需双窗时单窗就绪后进入
                lastFuturesFreq = view.freq;
                currentFreq = view.freq;
                updateFreqButtonStates(true);
                updateDateInputType();
                connectRealtimeInit(view.symbol, view.freq);
                if (view.dual) _scheduleResumeDual(view);
            } else {
                // 股票：单窗加载（内部按 view.dual 决定是否进入双窗）
                lastStockFreq = view.freq;
                currentFreq = view.freq;
                _loadStockForResume(view);
            }
            _resumeWatchdog(); // 全局兜底：长时间拿不到数据 → initDefault
        }

        async function init() {
            try {
                // 会话级恢复：仅"刷新当前页"生效（sessionStorage 只在当前标签会话存活）。
                // 冷启动（全新会话）sessionStorage 为空 → 不进入此分支，保持原逻辑（股票单窗口）。
                const sessView = loadLastView();
                if (sessView) {
                    _restoreView(sessView);
                    return;
                }
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
                    const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号（恢复加载与用户操作竞争时让位）
                    fetch("/api/stocks/" + encodeURIComponent(savedState.code) + "/analyze?freq=" + savedState.freq, { cache: "no-store" })
                        .then(resp => {
                            if (!resp.ok) throw new Error("恢复失败");
                            return resp.json();
                        })
                        .then(data => {
                            if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
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
                            else if (data.meta.freq === "15分钟") returnedFreq = "15m";
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
                            if (_isChartActionStale(_seq)) return; // [N1] 过期恢复请求不得触发回退
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
                const _seq = _bumpChartActionSeq(); // [N1] 捕获本次操作序号（默认加载与用户操作竞争时让位）
                const resp = await fetch("/api/stocks/" + encodeURIComponent("sh000001") + "/analyze?freq=d", { cache: "no-store" });
                if (!resp.ok) throw new Error("默认加载失败");
                const data = await resp.json();
                if (_isChartActionStale(_seq)) return; // [N1] 丢弃过期响应
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
                else if (chartData.meta.freq === "15分钟") currentFreq = "15m";
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





// ══════════════════════════════════════════════════════════════════
        // [MERGED] AppState 状态访问层
        // 31 个共享状态变量的 getter/setter 访问器 + 8 个引导方法别名。
        // 闭包变量仍为唯一数据源（访问器同源读写，行为零漂移）；本层不新增
        // 任何 window.* 绑定（window API 面冻结），
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
                _bottomSlots: { get: function(){ return _bottomSlots; }, set: function(v){ _bottomSlots = v; } },
                _volDisplayMode: { get: function(){ return _volDisplayMode; }, set: function(v){ _volDisplayMode = v; } },
                _subBottomSlots: { get: function(){ return _subBottomSlots; }, set: function(v){ _subBottomSlots = v; } },
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
        // 首屏即与自动下单侧的生效值对齐：GET 只读回填，**不推送**（打开页面不该
        // 改动引擎策略）。缺失后端/接口不可达时保留本地显示，不阻断看图。
        syncBspFilterFromTrader();

        initCoordSystemRadio();

        // 流通市值过滤输入框（设置抽屉）：change 即持久化，不设 window 绑定（window 面冻结）
        (function() {
            var mcInput = document.getElementById("scan-min-float-mc");
            if (!mcInput) return;
            if (_scanMinFloatMcServer !== null) mcInput.placeholder = String(_scanMinFloatMcServer);
            if (!mcInput.value && _scanMinFloatMc !== null) mcInput.value = String(_scanMinFloatMc);
            mcInput.addEventListener("change", function() {
                // 清空输入框 = 回到「未配置」：删掉持久值，后续请求不带参（后端默认值生效）
                var raw = (mcInput.value || "").trim();
                if (raw === "") {
                    _scanMinFloatMc = null;
                    try { localStorage.removeItem("scan_min_float_mc"); } catch(e) {}
                    return;
                }
                var v = parseFloat(raw);
                if (isNaN(v) || v < 0) v = 0;
                _scanMinFloatMc = v;
                try { localStorage.setItem("scan_min_float_mc", String(v)); } catch(e) {}
            });
        })();

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
                    showToast("区间选择已取消");
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

        // ══════════════════════════════════════════════════════════════
        // [COMPONENT] AutoOrderService —— 自动下单开关（期货实时页顶部）
        // 状态源：GET  /api/trader/auto-order/status（轮询 5s）
        //   开：POST  /api/trader/auto-order/on   → 拉起引擎子进程
        //   关：POST  /api/trader/auto-order/off  → 停信号 + 运行态持仓离场（平今/平昨→空仓）、锁仓态保持
        // 仅在期货实时模式下显示（与"实时"徽标同步显隐）。
        // ══════════════════════════════════════════════════════════════
        let autoOrderBusy = false;        // 请求进行中（防连点）
        let autoOrderPollTimer = null;
        let autoOrderWorker = null;       // 轮询 Worker（后台标签不被节流；创建失败回退主线程定时器）
        // 异常退出探测的「上膛」标志（2026-10-09 明确语义）：true = 本页绑定**曾是**
        // 运行中 ⇒ 之后连续两拍不在跑才提示；null = 撤膛（用户主动起停、或已提示过一次）。
        // 由 _detectAutoOrderExit 与 onAutoOrderToggle 维护；调用方不要再用 running 覆盖。
        let autoOrderPrevRunning = null;
        // 连续「不在跑」拍数（2026-10-09 加）：异常退出提示的复核计数，防单拍瞬态假报
        let autoOrderNotRunningStreak = 0;
        let autoOrderLastLog = null;      // 引擎日志路径（异常退出提示用）
        let autoOrderLastOn = null;       // 上次轮询的开关态（状态变化时打控制台）
        let autoOrderRunning = false;     // 引擎进程是否运行中（切换合约/周期的 guard 依据）
        // 水位 / 冷却**按实例键控**（§3.10.4；实例键 = 登录方式 + 品种键，
        // 由 applyAutoOrderStatus 打进合并元素的 _instLabel，品种互斥 ⇒ 唯一）。
        // 为什么不能是单值：合并队列会把 A、B 两实例的告警按 ts 混在一起，单值
        // 水位一旦被 A 的高 ts 推到 100，B 实例稍后（后端瞬时读库失败那一轮没被
        // 读到）才出现的低 ts 新告警就被判成"旧闻"→ 既不弹框，又被随后的
        // ack 广播从库里清掉，**不可恢复**。冷却同理：键只带 code 会让"同 code
        // 的另一个实例"在 5 分钟内被静默 continue（连 console 都没有）。
        const autoOrderSeenAlertTs = {};  // 实例键 → 告警水位（<= 它的一律不再弹）
        const autoOrderSeenToastTs = {};  // 实例键 → 轻提示水位
        const autoOrderAlertCool = {};    // "实例键|code" → 上次弹框时刻（同因防连弹）
        const AUTO_ORDER_ALERT_COOL_MS = 5 * 60 * 1000;
        let autoOrderAlertAckHold = 0;    // 未确认的严重告警水位：>0 = 弹框还没关，暂缓 ack
        // 运行态保护价分段线（2026-09-28 改版：单线 → 分段阶梯）：
        //   runSegs = null（空仓/锁仓/引擎未下发段）→ 不画；非空 = segments 数组
        //   （每段 {phase, price, start_date, end_date}），每次轮询由 calcRunSegments
        //   重算，签名变化才触发整图重绘；aoRunSnap 缓存标签所需的 run 字段
        //   （r / anchor / side / wlr），与段列表同帧更新防错位。
        let runSegs = null;
        let runSegSig = null;
        let aoRunSnap = null;

        // ══════════════════════════════════════════════════════════════
        // 未标定品种置灰（2026-09-14 第 6 批）
        //   看行情**不设**品种限制（搜索/解析走全表别名表，见 AppChart.search_stocks）；
        //   品种约束只落在**下单侧**：不在下单白名单的品种把开关置灰 + 一行说明，
        //   让用户在"点之前"就知道不能下单，而不是点一下被弹框。
        //   ⚠️ 这是**体验优化，不是安全前置** —— 安全由 /auto-order/on 的启动拦截 +
        //      Engine 权威闸门覆盖。判定口径与开启路径**同一来源**
        //      （/api/trader/product-check），所以两边永不漂移。
        //   ⚠️ 只置灰**下单开关**，绝不用它去限制行情搜索 —— 那会退回
        //      "消费端加过滤"的旧做法（实测有 4 条绕过点，当天即被撤销）。
        // ══════════════════════════════════════════════════════════════
        let autoOrderTradable = { allowed: true, message: '', products: [] };
        let autoOrderTradableFor = null;   // 上面的状态对应哪个品种（防异步竞态贴错标签）

        function applyAutoOrderTradableUI() {
            const wrap = document.getElementById('auto-order-wrap');
            if (!wrap) return;
            const checkbox = document.getElementById('auto-order-checkbox');
            const hint = document.getElementById('auto-order-hint');
            // 引擎运行中**不置灰**：否则用户点不动开关、关不掉正在跑的引擎。
            const off = !autoOrderTradable.allowed && !autoOrderRunning;
            wrap.classList.toggle('disabled', off);
            // 请求进行中时开关的 disabled 由 onAutoOrderToggle 管，别抢
            if (checkbox && !autoOrderBusy) checkbox.disabled = off;
            if (hint) hint.textContent = off ? '该品种不支持自动下单' : '';
            if (off) {
                const list = autoOrderTradable.products.join(' / ');
                wrap.title = '该品种不支持自动下单\n\n' + (autoOrderTradable.message || '')
                    + (list ? '\n\n当前支持：' + list : '');
            }
        }

        async function refreshAutoOrderTradable() {
            const sym = realtimeSymbol;
            if (!sym) return;
            const chk = await checkSymbolTradable(sym);
            // 竞态：等待期间用户又换了品种 → 本次结果作废
            if (sym !== realtimeSymbol) return;
            autoOrderTradable = chk;
            autoOrderTradableFor = sym;
            applyAutoOrderTradableUI();
            // 品种键到位 → 立刻补一拍状态。本页绑定按品种键匹配
            // （matchAutoOrderInstance），而换品种后的首拍 status 轮询可能早于
            // 本次解析返回 —— 不补这一拍就会出现一个轮询周期（5s）的 bound=null，
            // autoOrderRunning 假 false → 切合约/切周期的守卫放行。
            pollAutoOrderStatus();
            if (!chk.allowed) {
                console.info('[auto-order] 品种未标定，开关已置灰: ' + sym
                    + '  ' + chk.message);
            }
        }

        // 开关随"实时"徽标显隐：仅期货实时模式展示
        function syncAutoOrderWrap() {
            const wrap = document.getElementById('auto-order-wrap');
            if (!wrap) return;
            const show = !!(isRealtimeMode && realtimeSymbol);
            wrap.classList.toggle('visible', show);
            if (show) {
                if (autoOrderTradableFor !== realtimeSymbol) {
                    // 换品种 → 先按"放行"渲染（不让旧品种的灰态残留），
                    // 再异步问后端纠偏。接口挂了 = 放行，与开启路径同一降级方向：
                    // 置灰只是提示，**绝不能因为查询失败就把开关卡死在灰态**。
                    autoOrderTradable = { allowed: true, message: '', products: [] };
                    applyAutoOrderTradableUI();
                    refreshAutoOrderTradable();
                }
                pollAutoOrderStatus();
            } else {
                // 隐藏时复位，避免灰态/说明残留在下一个品种上
                autoOrderTradable = { allowed: true, message: '', products: [] };
                autoOrderTradableFor = null;
                applyAutoOrderTradableUI();
            }
        }

        // 单次请求超时（主线程版；Worker 源码字符串内另有一份同名实现 ——
        // 两个作用域无法共享代码，只能各写一份，改时必须同步）。
        // 为什么必须有：fetch 默认**永不超时**，服务端万一 hang 住，上一轮
        // fetch 永远 pending 也不会自己结束 —— 轮询活着但永远没有下一拍。
        // ⚠️ 它只治"请求挂起"这一种失效，对 2026-09-24 那个"弹窗不弹/账本不刷"
        // **无效**：那次的根因在 Worker 里的相对 URL（见 AO_WORKER_SRC._statusUrl），
        // 请求根本没发出去，超时器永远等不到要等的东西。
        function _timeoutedFetch(url) {
            if (typeof AbortController === 'undefined') {
                return fetch(url, { cache: 'no-store' });
            }
            const ctl = new AbortController();
            const killer = setTimeout(function () { ctl.abort(); }, 8000);
            return fetch(url, { cache: 'no-store', signal: ctl.signal })
                .finally(function () { clearTimeout(killer); });
        }

        async function pollAutoOrderStatus() {
            try {
                const resp = await _timeoutedFetch('/api/trader/auto-order/status');
                if (!resp.ok) {
                    console.warn('[auto-order] status HTTP ' + resp.status);
                    return;
                }
                applyAutoOrderStatus(await resp.json());
            } catch (e) {
                console.warn('[auto-order] 轮询失败: ' + e.message);
            }
        }

        // 单段隔离执行器（2026-09-24）：状态应用里的每一段（开关 / 提示 /
        // 账本 / 保护价线）各自独立 —— 一段抛错只丢那一段，**不再连环停摆**。
        // 背景：原来各段裸跑在同一个回调里，任何一段抛异常，排在后面的段
        // 全部不执行且页面无提示（静默失效）；分段 + console.error 后，谁挂了
        // 一眼可见，其余段照常工作。
        // ⚠️ 定位边界：它保证的是「拿到数据之后」各段不互相拖累，属**可观测性**
        // 改造；「弹窗不弹 / 账本不刷」那次的根因在更上游 —— Worker 里的相对
        // URL 让轮询根本拿不到数据（见 AO_WORKER_SRC._statusUrl）。分段隔离
        // 对拿不到数据这一层无能为力，别把它当那次问题的修复。
        function _aoSafe(segName, fn) {
            try {
                fn();
            } catch (e) {
                console.error('[auto-order] ' + segName + ' 段执行失败: ', e);
            }
        }

        // 状态应用（poll 与后台 Worker 共用）。轮询本体已移到 Web Worker
        // （ao-poll-worker.js）：后台标签的主线程 setInterval 会被 Chrome
        // intensive throttling 节流到 ~1 次/分钟（页面隐藏 ≥5 分钟），Worker
        // 内定时器不受该节流，收到的消息任务也不被节流 —— 右下角系统通知
        // 因此恢复秒级（2026-09-23）。
        // 顶部「当前登录方式」徽标（2026-09-28）：跑起来之后一眼能看出连的是
        //   SimNow 还是实盘。此前完全看不出来 —— 只能翻 gateway.log 或去猜 .env，
        //   比"切换不方便"更容易出事（以为在仿真，其实在真钱上跑）。
        function renderAutoOrderLink(view, running) {
            const el = document.getElementById('auto-order-label');
            if (!el) return;
            const cur = (view && view.current) ? String(view.current) : '';
            const mkt = (view && view.market) ? String(view.market) : '';
            if (!cur || !running) {
                el.textContent = '自动下单';
                el.className = 'auto-order-label';
                el.title = '';
                return;
            }
            const isLive = (cur === 'live');
            if (isLive) {
                el.textContent = mkt || '实盘';
                el.className = 'auto-order-label live';
                el.title = '当前登录方式：' + (mkt || '实盘') + ' 实盘 —— 真实资金，成交即扣款';
            } else {
                el.textContent = 'SimNow';
                el.className = 'auto-order-label simnow';
                el.title = '当前登录方式：SimNow 仿真 —— 资金与成交均为模拟';
            }
        }

        // 子进程退出码 → 一句人话。只解释能一眼定性的机器级原因，其余原样给码：
        //   0xC0000005 访问违例（硬崩溃，Traceback 都来不及写）→ 日志尾部为空时
        //              这正是"为什么连日志都没有"的答案；
        //   0xC0000135 DLL 加载失败（Python / 依赖启动即挂）。
        // 后端逐实例带出 exit_rc（§3.2）；取不到就返回空串，不猜。
        function _rcHint(rc) {
            if (rc === null || rc === undefined || rc === '') return '';
            const n = Number(rc);
            if (isNaN(n)) return '';
            const u = n >>> 0;
            if (u === 0xC0000005) return '0xC0000005（访问违例：硬崩溃，异常栈都来不及写）';
            if (u === 0xC0000135) return '0xC0000135（DLL 加载失败：Python / 依赖启动即挂）';
            return (n === 0) ? '0（干净返回，非崩溃）'
                : ('0x' + u.toString(16).toUpperCase());
        }

        // 本页品种键（"KQ.m@CFFEX.IF" / "IF2609" / 别名 → "IF"）。
        // 前端**不复制**归一规则（剥合约月份、大小写）：那份规则的唯一来源是
        // Trading/Infra/Product.parse_product_key，出口是 /api/trader/product-check
        // 回带的 product（与"开启自动下单"的启动闸门同一实现）。前端再写一份
        // 必然与后端漂移 —— 品种白名单两处各写一遍就是这么出的事。
        // 取不到时返回 ''（首次轮询早于该请求返回 / 非白名单品种 → 后端给空串），
        // 调用方退回"合约全等"的老口径，绝不因为拿不到键而丢掉绑定。
        function autoOrderPageKey() {
            if (autoOrderTradableFor !== realtimeSymbol) return '';
            return (autoOrderTradable && autoOrderTradable.product) || '';
        }

        // 本页品种对应的实例（品种键优先、合约全等兜底）。
        // runningOnly=true → 本页绑定实例（开关/守卫依据，§3.7）；
        // runningOnly=false → 本页品种那个实例，不论在跑（退出弹窗要取它自己的
        //   逐实例投影 log_tail / exit_rc，见 §3.2 instances[]）。
        // 为什么必须按**品种键**绑：实例键就是品种键，而本页代码是"合约写法"。
        // 另一标签页把同一品种写成 CFFEX.IF2609 / IF2609 / 小写主连时，
        // 合约全等找不着实例 → bound=null → running 假 false → 切合约/切周期
        // 守卫放行（引擎还在跑，保护失效）；开关也会显示"关"、accepted 被丢弃。
        function matchAutoOrderInstance(insts, runningOnly) {
            const list = insts || [];
            const key = autoOrderPageKey();
            for (let i = 0; i < list.length; i++) {
                const it = list[i] || {};
                if (runningOnly && !it.running) continue;
                if (key && it.instance_key === key) return it;
                // 兜底：品种键未知（旧后端 / 本轮 product-check 未回 / 解析不出），
                // 或后端合成的实例键就是原样 symbol（旧后端无 instances[] 时）。
                if (realtimeSymbol && it.symbol === realtimeSymbol) return it;
            }
            return null;
        }

        // 异常退出探测（2026-10-09 从 applyAutoOrderStatus 内联块抽成独立函数）：
        //   ① 误报防线（用户反馈「选复盘时弹『交易引擎已退出!』，可引擎还在跑、
        //      刷新页面又显示开启」）：本页绑定靠**品种键**（pageKey 入参）。
        //      init 重解析 symbol / 换代码后的头几拍，autoOrderTradableFor !==
        //      realtimeSymbol ⇒ 键为空串 ⇒ bound 必为 null ⇒ running 假 false。
        //      故：键为空的一拍**不作结论**（只 console 记一笔）；且必须
        //      「本页绑定曾是运行中（上膛）→ 连续两拍不在跑」才提示 —— 单拍瞬态
        //      （绑定空窗 / 后端读库瞬时失败）不再假报。真崩溃会持续「不在跑」，
        //      照常提示，判别力未削弱。
        //   ② 状态语义（随之明确）：autoOrderPrevRunning = **上膛**（本页绑定曾是
        //      运行中），不再是「上一拍的原值」—— 后者每拍被覆盖，复核计数永远到不了
        //      2，真崩溃会被漏报。用户主动起停（onAutoOrderToggle 成功路径）复位为
        //      null = 撤膛，所以主动关闭绝不会被报成异常退出。
        //   ③ 抽成函数后门禁可按**函数名**锚点抽取 + node 真执行（见
        //      Test/test_ao_multi_instance_frontend.py），不再锚在一行内联条件上。
        function _detectAutoOrderExit(running, pageInst, bound, data, pageKey) {
            if (running) {                    // 本页绑定运行中 → 上膛
                autoOrderPrevRunning = true;
                autoOrderNotRunningStreak = 0;
                return false;
            }
            if (!pageKey) {                   // 绑定未知 ⇒ 结论不可信，不计数也不撤膛
                console.warn('[auto-order] 本页绑定未知（品种键未就位），本轮不作退出判断');
                return false;
            }
            autoOrderNotRunningStreak += 1;
            if (autoOrderBusy || autoOrderPrevRunning !== true
                    || autoOrderNotRunningStreak < 2) {
                return false;
            }
            autoOrderPrevRunning = false;     // 只报一次，避免每拍重复弹
            // 日志来源 = **本页品种那个实例**自己的逐实例投影（§3.2 instances[]）。
            // 不能只用顶层 log_tail：顶层那份只覆盖"最近一次操作"的那个实例，
            // 退出的不是它时（多实例：后启的 AU 在跑、IF 崩了）正文会退化成
            // "（日志文件不存在或为空）"+"完整日志：（未知）"，等于没提示。
            const _pi = pageInst || bound || null;
            const tail = (_pi && _pi.log_tail) || data.log_tail || '';
            const lf = (_pi && _pi.log_file) || data.log_file || null;
            const rcTxt = _rcHint(_pi ? _pi.exit_rc : undefined);
            console.warn('[auto-order] 交易引擎已退出，日志尾部:\n' + tail);
            showAlert('交易引擎已退出！\n\n交易引擎日志尾部（末 12 行）：\n'
                + (tail || '（本轮未取到日志内容：逐实例投影缺失，日志文件本身见下方路径）')
                + (rcTxt ? '\n\n子进程退出码：' + rcTxt : '')
                + '\n\n完整日志：' + (lf || '（未知）'));
            return true;
        }

        function applyAutoOrderStatus(data) {
            const checkbox = document.getElementById('auto-order-checkbox');
            if (!checkbox) return;
            // 多实例（§3.7）：instances[] = 全部托管实例；本页绑定 = 当前页面
            // 品种的运行中实例（品种互斥保证至多一个，§3.10.2）。旧后端无
            // instances[] 时，用顶层字段合成一个实例（渐进兼容）。
            const insts = Array.isArray(data.instances) ? data.instances
                : (data.symbol ? [{
                    symbol: data.symbol, freq: data.freq, running: !!data.running,
                    link: data.link || '', pid: data.pid,
                    log_file: data.log_file || null,
                    instance_key: data.symbol,
                    auto_order: data.auto_order || null,
                }] : []);
            const bound = matchAutoOrderInstance(insts, true);
            // 本页品种那个实例（含已退出的）：退出弹窗按它取日志尾部/退出码
            const pageInst = bound || matchAutoOrderInstance(insts, false);
            const running = !!bound;   // 本页品种有运行中实例（切换守卫依据，§3.7）
            // 链路视图顺手缓存：开关点击时不必为拿选项再发一次状态请求
            if (data.link_view) autoOrderLinkInfo = data.link_view;
            // 徽标跟随本页绑定的实例：current 覆写为该实例的登录方式
            const boundView = bound
                ? Object.assign({}, data.link_view || {}, { current: bound.link })
                : data.link_view;
            renderAutoOrderLink(boundView, running);
            const ao = bound ? (bound.auto_order || null) : null;
            const enabled = !!(ao && ao.enabled);
            const on = running && enabled;
            autoOrderRunning = running;   // 本页品种有运行中实例（切换守卫依据）
            // 状态变化 → 控制台输出完整信息（定位"自动关闭"问题）
            if (on !== autoOrderLastOn) {
                console.info('[auto-order] 状态: ' + (on ? '开' : '关')
                    + '  running=' + running + '  enabled=' + enabled
                    + '  pid=' + (bound ? bound.pid : null)
                    + '  link=' + (bound ? bound.link : '-')
                    + '  symbol=' + (bound ? bound.symbol + '/' + bound.freq : '-'));
                autoOrderLastOn = on;
            }
            const dot = document.getElementById('auto-order-dot');
            if (dot) { dot.classList.toggle('on', on); dot.classList.toggle('off', !on); }
            if (!autoOrderBusy) checkbox.checked = on;
            const posN = (ao && typeof ao.positions_n === 'number') ? ao.positions_n : 0;
            const aoState = (ao && ao.account_state) || '';
            const lockedN = (aoState === 'locked' && ao && ao.positions_n)
                ? ao.positions_n : 0;
            const aoRun = (ao && ao.run) || null;
            // 告警/轻提示合并（§3.10.4）：全部运行中实例的队列合并弹出，
            // 每条带实例前缀（"[仿真 IF]"）——多实例下只看本页会漏另一引擎的风险。
            const aoAlerts = [];
            const aoToasts = [];
            insts.forEach(function (i) {
                if (!i.running || !i.auto_order) return;
                const lb = (i.link === 'live' ? '实盘' : '仿真') + ' '
                    + (i.instance_key || i.symbol || '');
                (Array.isArray(i.auto_order.alerts) ? i.auto_order.alerts : [])
                    .forEach(function (a) {
                        aoAlerts.push(Object.assign({}, a, {
                            msg: (a.msg ? '[' + lb + '] ' + a.msg : a.msg),
                            _instLabel: lb }));
                    });
                (Array.isArray(i.auto_order.toasts) ? i.auto_order.toasts : [])
                    .forEach(function (t) {
                        aoToasts.push(Object.assign({}, t, {
                            msg: (t.msg ? '[' + lb + '] ' + t.msg : t.msg),
                            _instLabel: lb }));
                    });
            });
            const wrap = document.getElementById('auto-order-wrap');
            if (wrap && !wrap.classList.contains('disabled')) {
                const stateLabel = { flat: '空仓', locked: '锁仓', running: '运行' }[aoState]
                    || '未知';
                const netV = (ao && typeof ao.net_volume === 'number') ? ao.net_volume : 0;
                let tip;
                if (!bound) {
                    tip = '交易引擎：本页品种（' + (realtimeSymbol || '-') + '）无运行中实例';
                } else {
                    tip = '交易引擎：运行中（' + (bound.link === 'live' ? '实盘' : 'SimNow')
                        + ' · ' + (bound.instance_key || bound.symbol) + '）';
                    tip += '，账户状态：' + stateLabel
                        + (aoState === 'running' ? '（净敞口 ' + (netV > 0 ? '+' : '') + netV + ' 手）' : '');
                    if (bound.symbol) tip += '，' + bound.symbol + '/' + (bound.freq || '5m');
                    if (posN) tip += '，持仓 ' + posN + ' 手（已锁仓 ' + lockedN + '）';
                    // run = 当前这段敞口的风控锚与出场计划（后端 auto_order.run）：
                    // 与图上保护价线**同口径**：run 属于另一张图（切了品种 / 周期）
                    // 时，这些价对当前品种毫无意义 —— 线上不画，文字也不该报
                    // （2026-09-29 用户拍板）。
                    const _tipMeta = chartData && chartData.meta ? chartData.meta : null;
                    if (aoRun && _tipMeta && aoRun.symbol === _tipMeta.symbol
                        && aoRun.freq === currentFreq) {
                        tip += '；本段风控锚 ' + fmtPx(aoRun.anchor)
                            + '，止损 ' + fmtPx(aoRun.stop)
                            + '（' + aoRun.name + '）';
                    }
                    if (aoAlerts.length) tip += '；未确认告警 ' + aoAlerts.length + ' 条';
                    if (bound.log_file) tip += '，日志=' + bound.log_file;
                }
                tip += '；关闭时，锁仓或平仓（平今/昨）';
                wrap.title = tip;
            }
            // 半残数据防线（2026-09-24）：绑定实例在跑但 auto_order 投影缺失
            //   （后端读库瞬时失败被吞成 null）时，enabled=false 会把**用户开关
            //   重置成关**、账本被刷成空态 —— 比跳过这一轮糟糕得多。
            //   打 warn 跳过本轮，下轮轮询自然重试。
            if (running && !ao) {
                console.warn('[auto-order] 本轮绑定实例 auto_order 投影缺失（后端读库瞬时失败？），跳过本轮状态应用');
                return;
            }
            // 四段各自隔离（_aoSafe）：一段抛错不再连环停摆（弹窗/账本/画线
            // 全停且无提示的静默失效，2026-09-24 实盘教训），谁挂 console 可见。
            _aoSafe('告警', function () { handleAutoOrderAlerts(aoAlerts); });
            _aoSafe('轻提示', function () { handleAutoOrderToasts(aoToasts); });
            _aoSafe('账本', function () { refreshLedgerPanel(); });   // 账本面板（§3.10.1）：/ledger 聚合，面板关着时不拉
            // 运行态保护价分段线（2026-09-28：单线 → 分段阶梯）。为什么放主图：
            //   保护价是持仓期间**最需要盯着**的数，tooltip/徽标都要"找"才看得见
            //   —— 2026-09-23 实盘多仓浮盈 2.6R 回撤到 0.77R，全程不知道会在哪离场。
            //   分段阶梯把"现在在哪层、从哪根 bar 起生效、锁了多少 R"直接画在
            //   价格轴上；引擎每次抬价/新高都出新段，段签名变化才整图重绘
            //   （轮询 5s 一次，值没变别白画）。
            _aoSafe('保护价线', function () {
                const _rs = calcRunSegments(aoRun, runSegSig);
                if (_rs.changed) {
                    runSegs = _rs.segs;
                    runSegSig = _rs.sig;
                    aoRunSnap = aoRun ? { r: aoRun.r, anchor: aoRun.anchor,
                                          side: aoRun.side, wlr: aoRun.wlr,
                                          symbol: aoRun.symbol,
                                          freq: aoRun.freq } : null;
                    render();
                }
            });
            // 异常退出探测：独立函数（含「绑定未知不作结论 + 连续两拍复核」防线，
            // 见 _detectAutoOrderExit 定义处）。用 _aoSafe 包住：探测内部抛错不能
            // 拖垮后面几段（账本/保护价线）。⚠️ autoOrderPrevRunning 由该函数自己
            // 维护（上膛/撤膛语义），调用方**不要**再用 running 覆盖它。
            _aoSafe('退出探测', function () {
                _detectAutoOrderExit(running, pageInst, bound, data,
                    autoOrderPageKey());
            });
            if (running) autoOrderLastLog = (bound && bound.log_file) || null;
        }

        // ── 引擎账本面板（C，2026-09-18）：持仓，随轮询刷新 ──
        // 数据 = /api/trader/status 的 auto_order.positions。
        // 展示的是**交易引擎账本**（策略/止损止盈只认它），不是柜台真值 ——
        // 镜像可能滞后甚至整场为空（对账证据门的由来），柜台以快期3 为准。
        // 2026-09-24 用户拍板：成交节删除，面板只保留持仓（后端 trades_recent
        // 投影保留不动，只是前端不再消费）；同日二次拍板：持仓行加序号
        // （1. 空 2 手 … / 2. 多 2 手 …），按后端 positions 原序编号。
        let autoOrderLedgerData = null;
        // 点面板与按钮之外任意处即收起 —— 全局只挂一次监听。
        // 自动打开也走它（2026-10-08）：否则自动弹出的账本点哪儿都关不掉。
        function bindLedgerOutsideClose() {
            if (bindLedgerOutsideClose._bound) return;
            bindLedgerOutsideClose._bound = true;
            document.addEventListener('click', function (e) {
                const p = document.getElementById('auto-order-ledger-panel');
                const b = document.getElementById('auto-order-ledger-btn');
                if (p && b && !p.contains(e.target) && !b.contains(e.target)) {
                    p.style.display = 'none';
                }
            });
        }

        // 事件提示行（2026-10-08）：自动打开账本时把"刚才发生了什么"贴在面板顶。
        //   平仓后面板只剩"空仓"（成交节已于 2026-09-24 拍板删除），光看持仓
        //   说不出发生了什么 —— 这一行补的就是这段信息。账本数据每刷新一次
        //   即清（renderAutoOrderLedger 开头清）：一次性、不常驻，也不进成交
        //   列表（与"成交节已删"的拍板不冲突）。
        function setAolFlash(msg) {
            const el = document.getElementById('aol-flash');
            if (!el) return;
            if (!msg) { el.style.display = 'none'; el.textContent = ''; return; }
            el.textContent = '刚刚：' + msg;
            el.style.display = 'block';
        }

        // 报单成交后自动打开账本（2026-10-08 需求 ⑴⑵）：与手工点击的**切换**
        //   语义不同 —— 这里是幂等"确保打开"，面板已开着时再触发仍是开着，
        //   绝不能被 toggle 关掉（连续开平仓会闪成筛子）。
        function openAutoOrderLedger(flashMsg) {
            const panel = document.getElementById('auto-order-ledger-panel');
            if (!panel) return;
            panel.style.display = 'block';
            bindLedgerOutsideClose();
            renderAutoOrderLedger(autoOrderLedgerData);
            refreshLedgerPanel(true);   // 打开即拉一次聚合账本（§3.10.1）；本次刷新不覆盖提示行
            setAolFlash(flashMsg || '');   // 必须在渲染之后：同步渲染会清掉提示行
        }

        function toggleAutoOrderLedger(ev) {
            if (ev) ev.stopPropagation();
            const panel = document.getElementById('auto-order-ledger-panel');
            if (!panel) return;
            // 用内联 style 切换显隐，不依赖 app.css —— 即使样式表没更新，
            // 面板也绝不会以"漏出来的文字块"形式渲染在按钮旁边
            const willShow = (panel.style.display === 'none');
            panel.style.display = willShow ? 'block' : 'none';
            if (willShow) {
                bindLedgerOutsideClose();
                renderAutoOrderLedger(autoOrderLedgerData);
                refreshLedgerPanel();   // 打开即拉一次聚合账本（§3.10.1）
            }
        }
        function fmtAolPx(v) {
            const n = Number(v);
            return (v === null || v === undefined || isNaN(n)) ? '--' : String(n);
        }

        // 账本面板价格（统一一位小数）：7618 → 7618.0；7594.2 → 7594.2。
        // 仅展示层格式化，不做品种 tick 推断（2026-09-22 四次拍板）。
        function fmtAolPx1(v) {
            const n = Number(v);
            return (v === null || v === undefined || isNaN(n)) ? '--' : n.toFixed(1);
        }

        // 账本面板时间：ISO（2026-09-22T13:32:03+08:00）→ 26/09/22 13:32:03；
        // 纯日期（2026-09-22）→ 26/09/22；解析不动就原样返回，不猜。
        function fmtAolTime(s) {
            const str = String(s || '');
            let m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2}:\d{2})/.exec(str);
            if (m) return m[1].slice(2) + '/' + m[2] + '/' + m[3] + ' ' + m[4];
            m = /^(\d{4})-(\d{2})-(\d{2})/.exec(str);
            return m ? m[1].slice(2) + '/' + m[2] + '/' + m[3] : str;
        }
        function renderAutoOrderLedger(led, keepFlash) {
            autoOrderLedgerData = led;
            const panel = document.getElementById('auto-order-ledger-panel');
            if (!panel || panel.style.display === 'none') return;   // 关着不渲染
            // 一次性提示行的清除时机 = **别的**刷新带来的数据到达。
            // openAutoOrderLedger 会先拉一次账本、再设提示行；那一次刷新属于
            // 「同一事件」，必须由调用方显式传 keepFlash=true 豁免 —— 否则提示行
            // 活不过一个 fetch 往返，用户看不到自己当初为什么被弹开面板。
            if (!keepFlash) setAolFlash('');
            const posEl = document.getElementById('aol-positions');
            if (!posEl) return;
            const groups = (led && Array.isArray(led.groups)) ? led.groups : [];
            if (!groups.length) {
                posEl.textContent = '（暂无账本数据）';
                return;
            }
            // 两级分区（§3.10.1）：顶层 = 登录方式（SimNow / 实盘），区内每
            // 实例一个小节（(n) 品种（周期 · 运行中/已停止）），小节之下是该
            // 实例的持仓行。持仓序号保持**实例内**语义（"1 号仓" = 该实例的
            // 第一笔建仓，标题带实例标识后无歧义）。
            let html = '';
            groups.forEach(function (g) {
                const items = Array.isArray(g.items) ? g.items : [];
                if (!items.length) return;   // 空分区整区不显示
                html += '<div class="aol-group" style="font-weight:600;'
                    + 'margin:6px 0 2px;">' + (g.label || g.link || '?') + '</div>';
                items.forEach(function (it, gi) {
                    const st = it.running ? '运行中' : '已停止';
                    html += '<div class="aol-inst" style="margin:4px 0 2px;color:'
                        + (it.running ? '#8ab4ff' : '#8a8f98') + ';">(' + (gi + 1) + ') '
                        + (it.product_key || it.symbol || '?')
                        + '（' + (it.freq ? it.freq + ' · ' : '') + st + '）</div>';
                    const ps = Array.isArray(it.positions) ? it.positions : [];
                    if (!ps.length) {
                        html += '<div class="aol-row" style="color:#8a8f98;">空仓</div>';
                        return;
                    }
                    // 持仓序号按后端 positions **原序**编号（2026-09-24 拍板）：
                    // 不在前端重排，序号即"第几笔建仓"，刷新前后稳定可指代。
                    html += ps.map(function (p, i) {
                        const long = (p.side === 'LONG');
                        return '<div class="aol-row">'
                            + '<span class="aol-idx">' + (i + 1) + '.</span>'
                            + '<span class="aol-side ' + (long ? 'long' : 'short') + '">'
                            + (long ? '多' : '空') + ' ' + p.volume + '手</span>'
                            + (p.symbol ? '<span class="aol-dim">' + p.symbol + '</span>' : '')
                            + '<span>@ ' + fmtAolPx1(p.entry_price) + '</span>'
                            + '<span class="aol-dim">' + fmtAolTime(p.entry_at || p.entry_date || '')
                            + '</span></div>';
                    }).join('');
                });
            });
            posEl.innerHTML = html || '（暂无账本数据）';
        }

        // 聚合账本拉取（§3.10.1）：面板打开期间随轮询刷新；关着不拉。
        let _ledgerInFlight = false;
        function refreshLedgerPanel(keepFlash) {
            const panel = document.getElementById('auto-order-ledger-panel');
            if (!panel || panel.style.display === 'none') return;
            if (_ledgerInFlight) return;
            _ledgerInFlight = true;
            _timeoutedFetch('/api/trader/auto-order/ledger')
                .then(function (r) {
                    return r.ok ? r.json()
                                : Promise.reject(new Error('HTTP ' + r.status));
                })
                .then(function (led) {
                    _ledgerInFlight = false;
                    renderAutoOrderLedger(led, keepFlash);
                })
                .catch(function (e) {
                    _ledgerInFlight = false;
                    console.warn('[auto-order] 账本聚合失败: ' + e.message);
                });
        }

        // 价格显示：只去掉浮点尾巴，不做品种 tick 推断（tick 是后端的事）
        function fmtPx(v) {
            if (typeof v !== 'number' || !isFinite(v)) return '-';
            return String(Math.round(v * 1000) / 1000);
        }

        // ══════════════════════════════════════════════════════════════
        // [COMPONENT] 运行态保护价**分段**线（2026-09-28 改版：单线 → 分段阶梯）
        //   数据源 = /api/trader/auto-order/status → auto_order.run.segments
        //   （交易引擎把本段 run 的每次保护价生效区间记成一段：初始止损段自
        //   开仓 bar 起；浮盈过保本阈值 → 保本段；浮盈过盈亏比阈值 → 初始
        //   跟踪段；此后每根新高 bar → 移动跟踪段。引擎记录**实际发生**的
        //   抬价，入场价 = 实际成交价 —— 与股票页右键推演（信号 K 线收盘价
        //   的假设口径）本质不同；分段/标签口径照 v1.7 文档对齐）。
        //   三态语义（沿 2026-09-24 拍板）：空仓 / 锁仓 → run 为 None → 线不画；
        //   运行态 → 分段阶梯橙虚线，抬价/新高即出新段（左端=那根 bar）。
        //   结构（逻辑与绘制分离，纯函数可在 node 里单测）：
        //     calcRunSegments(aoRun, prevSig) → {segs, sig, changed}
        //     drawRunSegments(...)             → 主图渲染管线里的分段阶梯线 + 层标签
        // ══════════════════════════════════════════════════════════════
        function calcRunSegments(aoRun, prevSig) {
            const segs = (aoRun && Array.isArray(aoRun.segments)
                          && aoRun.segments.length) ? aoRun.segments : null;
            // 签名含 side：方向翻转必然开新 run，即使段值巧合相同也要重画。
            // symbol / freq 一并进签名：切合约 / 切周期时即便段值巧合相同，
            //   快照也要跟着换 —— 否则 {symbol, freq} 还停在上一段 run 上，
            //   绘制函数的品种校验拿旧值比新图，判定结果不可预期。
            // 无段时签名归一为 null —— 空仓 → 空仓的连续轮询不误判"变化"。
            const sig = segs === null ? null : JSON.stringify({
                side: String(aoRun.side || ""),
                symbol: String(aoRun.symbol || ""),
                freq: String(aoRun.freq || ""),
                segs: segs
            });
            let changed;
            if (segs === null || prevSig === null) {
                changed = (segs === null) !== (prevSig === null);
            } else {
                changed = sig !== prevSig;
            }
            return { segs: segs, sig: sig, changed: changed };
        }

        // 主图绘制管线里的保护价分段线：橙色横虚线阶梯 + 层末段标签。
        //   画法照股票页推演 drawTpslLines 的 v1.7 口径（同一份 tpslSegLabel 标签）：
        //   分段线按 bar 边缘衔接（起止同日的段靠边缘衔接才可见）；每层只在
        //   「该层最后一段」画标签；末段（仍在持仓）延伸到图右缘。
        //   颜色沿用橙（图上唯一无语义冲突的醒目色），多空不换色。
        //   非末段的右端 = 下一段左端（end_date 由引擎投影：下一段 start_date）。
        function drawRunSegments(klines, area, priceRange, barStep, subPixelOffset) {
            if (!runSegs || !runSegs.length) return;
            // 品种 / 周期不匹配（这段 run 属于另一张图）→ 不画，与股票页推演
            //   drawTpslLines 同口径：切了合约或周期，上一段 run 的保护价线
            //   留在新图上就是一条**价位完全无关**的橙线，比没有更误导。
            //   没有降级分支：kv run 的**唯一写入点**是引擎 `_persist_run`
            //   （必带 symbol / freq），run 结束即 `delete_key`、恢复时净敞口为
            //   0 也删 —— "有 run 却没品种周期"不可达。放行分支只会把真的缺
            //   失掩盖成"正常画线"，反而看不出投影链路断了。
            const meta = chartData && chartData.meta ? chartData.meta : null;
            if (aoRunSnap && (!meta || aoRunSnap.symbol !== meta.symbol
                              || aoRunSnap.freq !== currentFreq)) return;
            // 伪 plan：tpslSegLabel 只读 r / entry.price / entry.side /
            //   params.win_loss_ratio —— 期货 run 投影按同名字段下发
            //   （anchor = 实际成交价 = 风控锚；wlr 缺失时标签退化为不带倍数）。
            const plan = {
                r: aoRunSnap ? aoRunSnap.r : null,
                entry: { price: aoRunSnap ? aoRunSnap.anchor : null,
                         side: aoRunSnap ? String(aoRunSnap.side || "").toLowerCase() : "" },
                params: { win_loss_ratio: aoRunSnap ? aoRunSnap.wlr : null }
            };
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const segs = runSegs;
            let firstTrailing = -1;
            segs.forEach(function (seg, i) {
                if (firstTrailing < 0 && seg.phase === "trailing") firstTrailing = i;
            });
            // 裁剪区就是同一个 area —— save/clip 提到循环外，一次配对到底。
            //   旧写法把 save 放在循环内、restore 放在循环末：循环里的早退
            //   （`!isLayerLast` / 标签越界）会跳过 restore，同层多段时每帧
            //   泄漏一次状态栈 + clip 残留，把同帧后续绘制裁进主图矩形。
            //   提到循环外后，循环内的 return（= continue）不可能绕过 restore。
            ctx.save();
            ctx.beginPath();
            ctx.rect(area.x, area.y, area.w, area.h);
            ctx.clip();
            segs.forEach(function (seg, i) {
                const isLast = (i === segs.length - 1);
                const isLayerLast = isLast ||
                    (segs[i + 1] && segs[i + 1].phase !== seg.phase);
                const g1 = dateToGlobalIdx(seg.start_date, map);
                if (g1 === undefined) return;
                const g2 = dateToGlobalIdx(seg.end_date, map);
                if (g2 !== undefined && g2 < globalStart) return;
                if (g1 >= globalEnd) return;
                let x1 = (g1 < globalStart) ? area.x
                    : globalIdxToX(g1, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                let x2 = (g2 === undefined || g2 >= globalEnd)
                    ? area.x + area.w
                    : globalIdxToX(g2, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                x1 = Math.max(x1, area.x);
                x2 = Math.min(x2, area.x + area.w);
                // 末段且仍在持仓：保护价当前仍在生效 → 延伸到图右缘。
                if (isLast) x2 = area.x + area.w;
                if (x2 - x1 < 0.5) return;
                const y = priceToY(seg.price, area, priceRange);
                ctx.strokeStyle = "#FF9800";
                ctx.lineWidth = 1.5;
                ctx.setLineDash([6, 4]);
                ctx.beginPath();
                ctx.moveTo(x1, y);
                ctx.lineTo(x2, y);
                ctx.stroke();
                ctx.setLineDash([]);
                if (!isLayerLast) return;
                if (y < area.y + 12 || y > area.y + area.h - 4) return;
                const kind = (seg.phase === "trailing")
                    ? (i === firstTrailing ? "initial" : "moved") : null;
                const label = tpslSegLabel(seg, plan, kind);
                ctx.font = "11px monospace";
                const tw = ctx.measureText(label).width;
                let tx = Math.min(x2, area.x + area.w) - 6;
                tx = Math.max(tx, area.x + tw + 4);
                tx = Math.min(tx, area.x + area.w - 4);
                const ty = (y - 6 < area.y + 12) ? y + 15 : y - 6;
                ctx.fillStyle = "#FF9800";
                ctx.textAlign = "right";
                ctx.fillText(label, tx, ty);
            });
            ctx.restore();
        }

        // ══════════════════════════════════════════════════════════════
        // 股票页「止盈止损」图上推演（v1.5 §4）
        //   归一 + 同根取首 + console.warn 全在前端（N1 方案 b）；
        //   后端 /api/stocks/{code}/tpsl 只校验送去的单个 bsp。
        // ══════════════════════════════════════════════════════════════
        // type 复合串归一：任一分量去尾部 psab 变体后 ∈ {0,1,2,3} 即命中
        //（与后端 AppTPSL._type_hits_0123 / CEnum.BSP_TYPE.main_type 同口径）
        function _tpslTypeHit(typeStr) {
            return String(typeStr || "").split(",").some(function (seg) {
                return ["0", "1", "2", "3"].indexOf(seg.trim().replace(/[psab]+$/, "")) >= 0;
            });
        }
        // 同根候选（不在此处判市场：调用方已用 isFuturesMode 拦截）
        function _tpslBspCandidates(date) {
            if (!chartData || !chartData.bsps) return [];
            return chartData.bsps.filter(function (b) {
                return b.date === date && _tpslTypeHit(b.type);
            });
        }
        // 取首 + 可观测告警（N1：多于一个时 console.warn，不静默）
        function _tpslBspPick(date) {
            const cands = _tpslBspCandidates(date);
            if (!cands.length) return null;
            if (cands.length > 1) {
                console.warn("[止盈止损] 同根多个买卖点，取首个:", date,
                    cands.map(function (b) { return b.type; }).join(" / "));
            }
            return cands[0];
        }
        function _tpslReset() {
            if (!_tpslActive && !_tpslPlan) return;
            _tpslActive = false;
            _tpslPlan = null;
        }
        // 右键菜单 → 组装 payload → 后端推演 → 存计划 → 重绘
        window.stockTpslFromMenu = function () {
            document.getElementById("annotation-menu").classList.remove("show");
            if (_tpslActive || isFuturesMode() || !chartData || !chartData.klines) return;
            const bsp = _tpslBspPick(_annotationTargetDate);
            if (!bsp) return;
            const klines = chartData.klines;
            const entryIdx = klines.findIndex(function (k) { return k.date === bsp.date; });
            if (entryIdx < 0) return;
            const code = chartData.meta && chartData.meta.symbol ? chartData.meta.symbol : "";
            if (!code) return;
            const payload = {
                code: code,
                freq: currentFreq,
                klines: klines.slice(Math.max(0, entryIdx - 60)),   // 预热 60 根 + 推演到末根
                bsp: bsp
            };
            fetch("/api/stocks/" + encodeURIComponent(code) + "/tpsl", {
                method: "POST",
                cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            }).then(function (resp) {
                if (!resp.ok) {
                    resp.json().catch(function () { return {}; }).then(function (err) {
                        showToast("止盈止损推演失败（HTTP " + resp.status + "）" +
                            (err && err.detail ? "：" + err.detail : ""));
                    });
                    return null;
                }
                return resp.json();
            }).then(function (data) {
                if (!data) return;
                _tpslPlan = data;
                _tpslPlan.code = code;
                _tpslPlan.freq = currentFreq;
                _tpslActive = true;
                render();
            }).catch(function (e) {
                showToast("止盈止损推演请求异常：" + e);
            });
        };
        // 取消盈损：清计划 + 重绘（Q4：无提示、无结束弹窗）
        window.stockTpslCancel = function () {
            document.getElementById("annotation-menu").classList.remove("show");
            _tpslReset();
            render();
        };
        // 分段标签（纯函数，可在 node 里单测；2026-09-27 用户三改+四改+五改：
        //   ⑴ 标签画进图内右对齐（不再画到右侧价格轴留白——长标签会超出视口）；
        //   ⑵ 各段层名前置、空格分隔、无「保护」前缀；数值用全角括号 R（…）：
        //     止损/保本段显示 R 点数，跟踪段显示 3R 点数（win_loss_ratio×R，
        //     前缀动态取 params.win_loss_ratio）；终态段追加 已止损/已止盈(±盈亏R)）
        // kind：trailing 段用 —— "initial"=初次达盈亏比阈值的那段（3R 阈值线），
        //   "moved"=其后逐级上移的段（标签带锁盈 R 数与点数）；sl/breakeven 段忽略。
        function tpslSegLabel(seg, plan, kind) {
            const phaseLabel = { breakeven: "保本", trailing: "跟踪" }[seg.phase] || "止损";
            let label = phaseLabel + " " + _fmtPrice(seg.price);
            if (!plan) return label;
            const r = isFinite(plan.r) ? Math.round(plan.r * 100) / 100 : null;
            const wlr = plan.params ? Number(plan.params.win_loss_ratio) : NaN;
            const sign = (plan.entry && plan.entry.side === "short") ? -1 : 1;
            if (seg.phase === "trailing" && kind === "initial") {
                label = "初始跟踪 " + _fmtPrice(seg.price);
                if (r && isFinite(wlr) && wlr > 0) {
                    label += " " + wlr + "R（" + Math.round(wlr * r * 100) / 100 + "）";
                }
            } else if (seg.phase === "trailing" && kind === "moved" && r && r > 0) {
                const locked = Math.round((seg.price - plan.entry.price) * sign
                    / plan.r * 100) / 100;
                label = "移动跟踪 " + _fmtPrice(seg.price) + " " + locked
                    + "R（" + Math.round(locked * plan.r * 100) / 100 + "）";
            } else {
                if (r && seg.phase !== "trailing") {
                    label += " R（" + r + "）";
                }
            }
            const term = plan.terminal;
            if (seg.terminal && term && term.outcome) {
                label += " " + ({ sl_exit: "已止损", be_exit: "已止盈",
                    trail_exit: "已止盈" }[term.outcome] || "");
            }
            return label;
        }
        // 分段横虚线（v1.5 §4.6-⑤）：橙虚线与层标签口径与期货运行态分段线
        //   （drawRunSegments）同源同款（v1.7 标签/边缘衔接，2026-09-28 起
        //   期货侧单线已升级为分段线，两者互为镜像）；
        //   segments 分段、末段（含 terminal）画到图最右、标签加「已」与实际 R 数（含负，Q11）。
        function drawTpslLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!_tpslActive || !_tpslPlan) return;
            // 代码/周期不匹配（切视图后未显式取消）→ 不画（防旧线残留误导，S3）
            const meta = chartData && chartData.meta ? chartData.meta : null;
            if (!meta || _tpslPlan.code !== meta.symbol || _tpslPlan.freq !== currentFreq) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const segs = _tpslPlan.segments || [];
            const term = _tpslPlan.terminal;
            let firstTrailing = -1;
            segs.forEach(function (seg, i) {
                if (firstTrailing < 0 && seg.phase === "trailing") firstTrailing = i;
            });
            // 与 drawRunSegments 同款：save/clip 提到循环外一次配对（循环内
            //   的早退跳过 restore 会漏状态栈 + 留 clip，两函数互为镜像，同修）。
            ctx.save();
            ctx.beginPath();
            ctx.rect(area.x, area.y, area.w, area.h);
            ctx.clip();
            segs.forEach(function (seg, i) {
                // 每层只在「该层最后一段」画标签（层名+R/2R+终态）；段宽不够也画，
                //   文字在保护线上方向左延伸（2026-09-27 实测：宽度门槛会把止损/保本
                //   标签整段吞掉）。逐段标注会糊成一片，故按层取末段。
                const isLast = (i === segs.length - 1);
                const isLayerLast = isLast ||
                    (segs[i + 1] && segs[i + 1].phase !== seg.phase);
                const g1 = dateToGlobalIdx(seg.start_date, map);
                if (g1 === undefined) return;
                const g2 = dateToGlobalIdx(seg.end_date, map);
                if (g2 !== undefined && g2 < globalStart) return;
                if (g1 >= globalEnd) return;
                // 段线按 bar 边缘衔接（X(g)±barStep/2）：跟踪段每根新高 bar 一段
                //   （起=止=同一天），按中心点画会得到零长度线段而不可见 —— 阶梯
                //   上升线靠边缘衔接才能逐级显示（2026-09-27 潍柴日K 实测修复）。
                let x1 = (g1 < globalStart) ? area.x
                    : globalIdxToX(g1, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                let x2 = (g2 === undefined || g2 >= globalEnd)
                    ? area.x + area.w
                    : globalIdxToX(g2, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                x1 = Math.max(x1, area.x);
                x2 = Math.min(x2, area.x + area.w);
                // 末段且未离场（仍在监控）：延伸到图右缘 —— 保护价当前仍在生效；
                //   已离场的段右端=触发 bar（六改⑴），历史段右端=下一段衔接处。
                if (isLast && !term) x2 = area.x + area.w;
                if (x2 - x1 < 0.5) return;
                const y = priceToY(seg.price, area, priceRange);
                ctx.strokeStyle = "#FF9800";
                ctx.lineWidth = 1.5;
                ctx.setLineDash([6, 4]);
                ctx.beginPath();
                ctx.moveTo(x1, y);
                ctx.lineTo(x2, y);
                ctx.stroke();
                ctx.setLineDash([]);
                // ⑴ 标签画进图内右对齐（层末段=通常也是图右缘），不再画到轴留白；
                //   左缘兜底不越出图区（完整可见）。文字不加粗（2026-09-27 用户要求）。
                if (!isLayerLast) return;
                if (y < area.y + 12 || y > area.y + area.h - 4) return;
                const kind = (seg.phase === "trailing")
                    ? (i === firstTrailing ? "initial" : "moved") : null;
                const label = tpslSegLabel(seg, _tpslPlan, kind);
                ctx.font = "11px monospace";
                const tw = ctx.measureText(label).width;
                let tx = Math.min(x2, area.x + area.w) - 6;
                tx = Math.max(tx, area.x + tw + 4);
                tx = Math.min(tx, area.x + area.w - 4);
                const ty = (y - 6 < area.y + 12) ? y + 15 : y - 6;
                ctx.fillStyle = "#FF9800";
                ctx.textAlign = "right";
                ctx.fillText(label, tx, ty);
            });
            ctx.restore();
        }

        // ══════════════════════════════════════════════════════════════
        // [COMPONENT] AlertDialog —— 模态提示框（替代原生 alert / confirm）
        //   全站「看完点确定」的提示统一走这里（原来是各调用点直接敲原生 alert）。
        //   不用原生弹窗的原因：
        //     ① 出口只有一个 —— 必须先点「确定」才能继续操作页面；
        //     ② 点框外区域关不掉（原生 alert 压根没有遮罩层可点）。
        //   关闭出口：
        //     alert（只有确定）—— 点「确定」/ 点遮罩 / 按 Esc / 按 Enter 都关；
        //     confirm（取消 + 确定，破坏性操作前用）—— 点「取消」/ 点遮罩 / 按 Esc
        //       都表示「不执行」，只有点「确定」或按 Enter 才返回 true。
        //   点遮罩与按 Esc 同义（照抄浏览器原生的这条）：alert 关掉即「确定」，
        //   confirm 关掉即「取消」—— 破坏性操作不该被一次误点框外触发。
        //   一次只弹一个：原生弹窗会排队串行，这里用队列复刻同一语义 —— 后到的
        //   消息等前一个关掉再出现，不会几层叠起来分不清哪条是哪条。
        //   返回值是 Promise，在关掉那一刻 resolve（alert 恒 true；confirm 真/假）：
        //   需要「人已看到才往下走」的调用方（如严重告警的 ack 回执，见
        //   ackIfAlertsSeen）挂 .then() 即可。
        // ══════════════════════════════════════════════════════════════
        const _alertQueue = [];       // 待弹消息（各带自己的 resolve）
        let _alertShowing = false;    // 当前屏幕上是否有框

        // kind: "alert"（只有确定）| "confirm"（取消 + 确定）| "choice"（选项 + 取消 + 确定）
        //   choice 的 opts = {options: [{value,label,hint,enabled,reason}], default: <value>}
        //   resolve 值：确定 → 选中项的 value；取消 / 点框外 / Esc → null
        function _pushDialog(msg, kind, opts) {
            return new Promise(function (resolve) {
                _alertQueue.push({
                    msg: String(msg === null || msg === undefined ? "" : msg),
                    kind: kind,
                    opts: opts || null,
                    resolve: resolve
                });
                if (!_alertShowing) _pumpAlertQueue();
            });
        }

        function showAlert(msg) { return _pushDialog(msg, "alert"); }

        // 破坏性操作前的二次确认 —— 返回值 Promise<boolean>，替代原生 confirm。
        function showConfirm(msg) { return _pushDialog(msg, "confirm"); }

        // 单选式确认（自动下单的登录链路选择）：返回值是选中项的 value，取消为 null。
        function showChoice(msg, opts) { return _pushDialog(msg, "choice", opts); }

        function _escHtml(s) {
            return String(s === null || s === undefined ? "" : s)
                .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        }
        function _escAttr(s) {
            return _escHtml(s).replace(/"/g, "&quot;");
        }

        function _pumpAlertQueue() {
            const job = _alertQueue.shift();
            if (!job) { _alertShowing = false; return; }
            _alertShowing = true;
            const stale = document.getElementById("alert-dialog");
            if (stale && stale.parentNode) stale.parentNode.removeChild(stale);
            const overlay = _createAlertDialog(job.kind, job.opts);   // 按钮数随 kind 变，故每次重建
            const isConfirm = (job.kind === "confirm");
            const isChoice = (job.kind === "choice");
            // 点遮罩（框之外的区域）：alert ＝确定；confirm / choice ＝取消（防误删、防误选）
            const outsideResult = !isConfirm && !isChoice;
            const msgEl = overlay.querySelector(".alert-dialog-msg");
            const okBtn = overlay.querySelector('[data-act="ok"]');
            const cancelBtn = overlay.querySelector('[data-act="cancel"]');
            msgEl.textContent = job.msg;
            msgEl.scrollTop = 0;
            let closed = false;
            const close = function (result) {
                if (closed) return;      // 「确定」/「取消」/遮罩/Esc 几条出口可能同时到达
                closed = true;
                overlay.classList.remove("show");
                document.removeEventListener("keydown", onKey, true);
                // choice：确定 → 选中项的 value；取消 → null（调用方据此不启动）
                job.resolve(isChoice ? _choiceValue(overlay, result) : result);
                _pumpAlertQueue();       // resolve 是微任务，此刻队列已推进完
            };
            // Esc 与点遮罩同义；Enter 只认「确定」—— 确认框里回车是「我要执行」，
            // 不该被当成取消（原生 confirm 的 Enter 也是「确定」）。
            const onKey = function (e) {
                if (e.key === "Escape") { e.preventDefault(); close(outsideResult); }
                else if (e.key === "Enter") { e.preventDefault(); close(true); }
            };
            // 点框内不关（只有点遮罩才走 outsideResult）
            overlay.onclick = function (e) {
                if (e.target === overlay) close(outsideResult);
            };
            okBtn.onclick = function () { close(true); };
            if (cancelBtn) cancelBtn.onclick = function () { close(false); };
            document.addEventListener("keydown", onKey, true);
            overlay.classList.add("show");
            okBtn.focus();               // 焦点给「确定」，与原生 confirm 同一默认键
        }

        function _createAlertDialog(kind, opts) {
            const overlay = document.createElement("div");
            overlay.id = "alert-dialog";
            overlay.className = "alert-dialog";
            // 按钮沿用既有弹层的按钮样式与排列：app.html 里两个问答弹窗
            // （文字标注 / 股票扫描）都是「确定在左、取消在右」，这里照抄同一顺序，
            // 不给用户两套肌肉记忆。
            overlay.innerHTML = '<div class="alert-dialog-box">'
                + '<div class="alert-dialog-msg"></div>'
                + (kind === "choice" ? _buildChoiceOpts(opts) : "")
                + '<div class="annotation-dialog-btns">'
                + '<button class="annotation-dialog-btn primary" type="button" data-act="ok">确定</button>'
                + (kind === "confirm" || kind === "choice"
                    ? '<button class="annotation-dialog-btn" type="button" data-act="cancel">取消</button>'
                    : '')
                + '</div></div>';
            document.body.appendChild(overlay);
            return overlay;
        }

        // 选项区（登录链路选择）：不可用项**保留可见**并给出原因，而不是隐藏 ——
        // 用户该知道"为什么点不了实盘"，而不是看着只有一项发懵。
        function _buildChoiceOpts(opts) {
            const list = (opts && Array.isArray(opts.options)) ? opts.options : [];
            let firstOn = "";
            list.forEach(function (o) {
                if (o && o.enabled !== false && !firstOn) firstOn = String(o.value || "");
            });
            const def = String((opts && opts.default) || "");
            // 默认项（上次选择）不可用或压根没有 → 退到第一个可用项，
            // 绝不让一个禁用的单选框成为默认选中（那样"确定"会传出一个被拒的值）。
            const usable = list.some(function (o) {
                return o && String(o.value || "") === def && o.enabled !== false;
            });
            const want = usable ? def : firstOn;
            const rows = list.map(function (o) {
                const v = String((o && o.value) || "");
                const on = !!(o && o.enabled !== false);
                const sub = on ? String((o && o.hint) || "") : String((o && o.reason) || "");
                return '<label class="alert-dialog-opt' + (on ? '' : ' disabled') + '"'
                    + ' title="' + _escAttr(sub) + '">'
                    + '<input type="radio" name="alert-dialog-opt" value="' + _escAttr(v) + '"'
                    + (on && v === want ? ' checked' : '') + (on ? '' : ' disabled') + '>'
                    + '<span><span class="alert-dialog-opt-name">'
                    + _escHtml((o && o.label) || v) + '</span>'
                    + '<span class="alert-dialog-opt-hint">' + _escHtml(sub) + '</span>'
                    + '</span></label>';
            }).join("");
            return '<div class="alert-dialog-opts">' + rows + '</div>';
        }

        function _choiceValue(overlay, result) {
            if (!result) return null;
            const el = overlay.querySelector('input[name="alert-dialog-opt"]:checked');
            return el ? el.value : null;
        }

        // ══════════════════════════════════════════════════════════════
        // [COMPONENT] 自动下单告警弹窗（D11）
        // 后端把「资金不足 / 非交易时段 / 追价跑满 / 平仓连续被拒」这类需要人工
        // 介入的事件写成 alerts 队列（严重告警落盘，重启不丢），随状态轮询下发。
        // 本函数只做三件事：
        //   ① 按 ts 水位挑出新告警（同 code 5 分钟冷却，防一次故障连弹几十个框）
        //   ② severe → showAlert 模态框（点确定 / 点框外 / 按 Esc 都关）；warn → showToast 轻提示
        //   ③ 回 ack 把水位写回 state.db —— 不 ack 的话后端队列不清理，
        //      同一批告警每次轮询都会重来；ack 挂在弹框关掉之后（确认＝人已看到）
        // ⚠️ 弹框不阻塞交易引擎：交易引擎跑在独立子进程、行情走自己的 SSE 连接，
        //    页面这边弹框关不关得掉都影响不到它下单；
        //    真正要防的是"一次弹几十个" —— 所以冷却与"合并成一条"缺一不可。
        // ══════════════════════════════════════════════════════════════
        function handleAutoOrderAlerts(alerts) {
            // alerts = 合并后的全部运行中实例告警（applyAutoOrderStatus 已带实例
            // 前缀 _instLabel）。水位与冷却都按**实例键**分开记（§3.10.4）：
            // 单值水位会被另一实例的高 ts 推高，把本实例（上一轮后端读库瞬时失败
            // 没被读到）较低 ts 的新告警判成"旧闻"—— 既不弹框，又被随后的 ack
            // 广播清出库，**不可恢复**。冷却键不带实例则"同 code 的另一个实例"
            // 会在 5 分钟内被静默 continue（原来连 console 都没有）。
            if (!alerts || !alerts.length) return;
            const now = Date.now();
            const fresh = [];
            const edges = {};      // 实例键 → 本批见到的最大 ts（循环后并入各实例水位）
            let maxTs = 0;         // ack 水位：后端是全局水位（广播写全部实例库）
            for (let i = 0; i < alerts.length; i++) {
                const a = alerts[i] || {};
                const ts = Number(a.ts) || 0;
                if (!ts) continue;
                const ik = String(a._instLabel || '');
                if (ts > (edges[ik] || 0)) edges[ik] = ts;
                if (ts > maxTs) maxTs = ts;
                if (ts <= (autoOrderSeenAlertTs[ik] || 0)) continue;  // 本实例已处理过
                const ck = ik + '|' + String(a.code || 'unknown');
                if (now - (autoOrderAlertCool[ck] || 0) < AUTO_ORDER_ALERT_COOL_MS) {
                    continue;                     // 同实例同 code 冷却中，跳过弹框
                }
                autoOrderAlertCool[ck] = now;
                fresh.push(a);
            }
            // 水位推进：只推进到"本实例本批见过"的位置，实例之间互不掩盖
            Object.keys(edges).forEach(function (ik) {
                if (edges[ik] > (autoOrderSeenAlertTs[ik] || 0)) {
                    autoOrderSeenAlertTs[ik] = edges[ik];
                }
            });
            if (fresh.length) {
                const severe = [];
                const warn = [];
                for (let i = 0; i < fresh.length; i++) {
                    (fresh[i].level === 'severe' ? severe : warn).push(fresh[i]);
                }
                for (let i = 0; i < warn.length; i++) {
                    console.warn('[auto-order] 告警(' + warn[i].code + '): ' + warn[i].msg);
                    showToast('自动下单提醒：' + warn[i].msg);
                    aoSysNotify('自动下单提醒', warn[i].msg, 'ao-warn');
                }
                if (severe.length) {
                    console.error('[auto-order] 严重告警: ' + JSON.stringify(severe));
                    // 水位不在这里写回：确认＝人已看到，等弹框关掉再 ack
                    autoOrderAlertAckHold = Math.max(autoOrderAlertAckHold, maxTs);
                    const severeMsg = severe.map(function (a, i) {
                        return (i + 1) + '. ' + a.msg
                            + (a.n > 1 ? '（已重复 ' + a.n + ' 次）' : '');
                    }).join('\n\n');
                    aoSysNotify('自动下单：需人工介入', severeMsg, 'ao-alert');
                    showAlert('需人工介入！\n\n' + severeMsg).then(function () {
                        ackIfAlertsSeen();
                    });
                    return;                      // 本轮 ack 交给 ackIfAlertsSeen
                }
            }
            if (autoOrderAlertAckHold) return;   // 严重告警框还没关 → 水位先不写回
            ackAutoOrderAlerts(maxTs);
        }

        // 严重告警框关掉之后才回 ack：还有框在排队（含后续轮询新弹的）就继续等，
        // 避免「人还没看完、后端队列已被清空」。水位取见过的最大值。
        function ackIfAlertsSeen() {
            if (_alertShowing || _alertQueue.length) return;
            const ts = autoOrderAlertAckHold;
            autoOrderAlertAckHold = 0;
            ackAutoOrderAlerts(ts);
        }

        function ackAutoOrderAlerts(ts) {
            if (!ts || ts <= 0) return;
            fetch('/api/trader/auto-order/ack', {
                method: 'POST',
                cache: 'no-store',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ts: ts })
            }).then(function (resp) {
                if (!resp.ok) console.warn('[auto-order] 告警 ack HTTP ' + resp.status);
            }).catch(function (e) {
                console.warn('[auto-order] 告警 ack 失败: ' + e.message);
            });
        }

        // ══════════════════════════════════════════════════════════════
        // [COMPONENT] 自动下单后台系统通知（2026-09-23 需求 ⑶）
        //   弹窗 / toast 都是页面内的 DOM：页面切到后台（用户去干别的）就看不见
        //   —— 严重告警回前台还能补看（没 ack 就一直挂着），轻提示 5 秒自动消失、
        //   错过即丢失。这里用浏览器 Notification API 补一条 Win11 右下角系统
        //   通知。与股票扫描那条 winotify 是两条独立通道：那条由 App 进程发、
        //   分不清用户此刻在不在看页面；本条由页面自己发 —— 「页面在不在前台」
        //   只有页面自己知道，这正是"只在后台时才提醒"的判定来源。
        //   三条纪律：
        //     ① 只在页面不在前台时发（hidden = 切了标签页/最小化；!focused =
        //        被别的程序盖住）。前台时弹窗本来就看得见，再发系统通知是噪音；
        //     ② 权限只在用户手势里要（开启自动下单那一刻），平时不搞授权弹窗；
        //     ③ 同 tag 通知互相覆盖（Action Center 不堆一摞），点击把页面拉回前台。
        //   已知边界：浏览器整个关掉就没有这条通道（页面都退了没人发）—— 服务端
        //     通道（winotify）是另一个待决项，见交付说明。非安全上下文（如用
        //     http://局域网IP 访问）无 Notification API，同样静默降级为只弹页内层。
        // ══════════════════════════════════════════════════════════════
        // 纯判据函数（不碰 DOM / Notification，可抽到 node 单测）：
        // supported=环境有无 Notification；permission=授权态；hidden=document.hidden；
        // focused=document.hasFocus()。授权且不在前台才提醒。
        function aoNotifyEligible(supported, permission, hidden, focused) {
            if (!supported || permission !== 'granted') return false;
            return hidden || !focused;
        }

        function aoSysNotify(title, body, tag) {
            const supported = (typeof Notification !== 'undefined');
            const perm = supported ? Notification.permission : 'denied';
            if (!aoNotifyEligible(supported, perm, document.hidden,
                                  document.hasFocus())) return;
            try {
                const n = new Notification(title, {
                    body: String(body || ''),
                    tag: String(tag || 'ao-notify')
                });
                n.onclick = function () { window.focus(); n.close(); };
            } catch (e) {
                console.warn('[auto-order] 系统通知失败: ' + e.message);
            }
        }

        // 只在「开启自动下单」的用户手势里调用（浏览器要求授权请求挂在手势上，
        // 放到 await 之后手势上下文就丢了）。已授权/已拒绝都不再打扰。
        function requestAoNotifyPermission() {
            if (typeof Notification === 'undefined') return;
            if (Notification.permission !== 'default') return;
            Notification.requestPermission().then(function (p) {
                if (p !== 'granted') {
                    showToast('浏览器未授权系统通知：页面在后台时将收不到自动下单提醒');
                }
            }).catch(function () { /* 老式回调实现：静默 */ });
        }

        // ══════════════════════════════════════════════════════════════
        // [COMPONENT] 自动下单轻提示 toast（2026-09-18 需求 ⑷）
        //   引擎把关键动作（开仓/平仓/保本/移动止盈/账单同步）写进 state.db 的
        //   toasts 队列，随状态轮询下发。与告警的分界：轻提示是「刚才发生了什么」
        //   —— 5 秒自动消失、不需要确认、不合并（两次开仓是两个独立事件都要弹）。
        //   首次拉取只定水位不回放历史：页面晚开不该把半小时前的开仓弹一遍。
        // ══════════════════════════════════════════════════════════════
        // 自动打开账本只认**成交**类 code（2026-10-08）：开仓成交 open_filled
        // （买卖点驱动的建仓，含翻仓 / 拆锁入场）与离场成交 close_filled
        // （平仓 / 锁仓离场，含移动止盈、保本止损、初始止损）。阶段跃迁
        // （run_breakeven / run_trailing）与账单同步（reconcile_sync）都不是
        // 报单结果，不打扰看图。
        const AOL_AUTOPEN_CODES = { open_filled: 1, close_filled: 1 };

        function handleAutoOrderToasts(toasts) {
            // toasts = 合并后的全部运行中实例轻提示（已带实例前缀 _instLabel）。
            // 水位同样按实例键控（§3.10.4）：单值水位下 A 实例的高 ts 会把
            // B 实例（本轮才被读到）较低 ts 的新提示一并判成"历史"，直接吞掉。
            if (!toasts || !toasts.length) return;
            const fresh = [];
            const edges = {};
            for (let i = 0; i < toasts.length; i++) {
                const t = toasts[i] || {};
                const ts = Number(t.ts) || 0;
                if (!ts) continue;
                const ik = String(t._instLabel || '');
                if (ts > (edges[ik] || 0)) edges[ik] = ts;
                const prev = autoOrderSeenToastTs[ik] || 0;
                // 首次拉到该实例的队列只定水位、不回放历史（页面晚开不该把
                // 半小时前的开仓弹一遍）—— 与原 prev>0 门同语义，只是逐实例。
                if (prev > 0 && ts > prev) {
                    fresh.push({ msg: String(t.msg || ''),
                                 code: String(t.code || '') });
                }
            }
            Object.keys(edges).forEach(function (ik) {
                if (edges[ik] > (autoOrderSeenToastTs[ik] || 0)) {
                    autoOrderSeenToastTs[ik] = edges[ik];
                }
            });
            // 同一轮多条成交取**最后一条**（最新）作为账本提示行文案
            let autoOpenMsg = '';
            for (let i = 0; i < fresh.length; i++) {
                if (!fresh[i].msg) continue;
                showToast('自动下单：' + fresh[i].msg);
                // toast 5 秒即逝，页面在后台时用户根本看不见 —— 同步补一条
                // 系统通知（同 tag 覆盖，多条合并成 Action Center 里一条）
                aoSysNotify('自动下单', fresh[i].msg, 'ao-toast');
                if (AOL_AUTOPEN_CODES[fresh[i].code]) {
                    autoOpenMsg = fresh[i].msg;
                }
            }
            if (autoOpenMsg) openAutoOrderLedger(autoOpenMsg);
        }

        // ══════════════════════════════════════════════════════════════
        // 品种白名单前置检查（2026-09-14「K线图 vs 自动下单」解耦配套）
        //   看行情不设品种限制（搜索走别名全表，第 5 批已收窄为 16 品种 / 17 别名，
        //   见 AppChart.search_stocks）；
        //   品种约束**只在下单侧**生效：开启前先问后端，闸门实现与
        //   /auto-order/on 的启动拦截同源（Product.assert_product_allowed），
        //   故"前端说能开"与"引擎允许开"永远一致。
        //   同一接口还驱动**开关置灰**（未标定品种开关变灰不可点，见
        //   applyAutoOrderTradableUI），一个来源两处用途，不会各写一份白名单。
        //   接口异常时**放行**（allowed=true）：宁可让引擎启动闸门兜底报错，
        //   也不能因为一个查询接口挂了就彻底开不了自动下单（置灰也不能因此卡死）。
        // ══════════════════════════════════════════════════════════════
        async function checkSymbolTradable(symbol) {
            try {
                const resp = await fetch('/api/trader/product-check?symbol='
                    + encodeURIComponent(symbol || ''), { cache: 'no-store' });
                if (!resp.ok) {
                    console.warn('[auto-order] 品种检查 HTTP ' + resp.status + '，按放行处理');
                    return { allowed: true, message: '' };
                }
                const j = await resp.json();
                return {
                    allowed: j.allowed !== false,
                    message: j.message || '',
                    product: j.product || '',
                    products: Array.isArray(j.products) ? j.products : []
                };
            } catch (e) {
                console.warn('[auto-order] 品种检查失败，按放行处理: ' + e.message);
                return { allowed: true, message: '' };
            }
        }

        // ── 登录链路选择（2026-09-28）：点开自动下单 → 先选 SimNow / 实盘 ──
        //   后端把选项与可用性随状态接口下发（link_view），这里不做任何本地判断：
        //   "实盘能不能选"只有一个判据来源（AppTrader.link_options），前端复制
        //   一份判据必然漂移（品种白名单一度两处各写一遍的教训）。
        let autoOrderLinkInfo = null;   // 最近一次轮询拿到的 link_view

        async function ensureAutoOrderLinkInfo() {
            if (autoOrderLinkInfo) return autoOrderLinkInfo;
            try {
                const resp = await _timeoutedFetch('/api/trader/auto-order/status');
                if (resp.ok) autoOrderLinkInfo = (await resp.json()).link_view || null;
            } catch (e) {
                console.warn('[auto-order] 读取登录链路失败: ' + e.message);
            }
            return autoOrderLinkInfo;
        }

        async function pickTradeLink() {
            const info = await ensureAutoOrderLinkInfo();
            const opts = (info && Array.isArray(info.options)) ? info.options : null;
            if (!opts || !opts.length) {
                showAlert('无法获取登录方式：'
                    + ((info && info.error) || '状态接口未返回选项，请检查交易网关配置'));
                return null;
            }
            const picked = await showChoice(
                '登录方式',
                { options: opts, default: String((info && info.last) || '') });
            return picked || null;
        }

        async function onAutoOrderToggle(checkbox) {
            const on = checkbox.checked;
            if (autoOrderBusy) { checkbox.checked = !on; return; } // 防连点
            if (on) requestAoNotifyPermission();  // 权限要挂在用户手势上，须在 await 之前
            autoOrderBusy = true;      // 选择期间就上锁：否则弹框期间再点会叠出第二个框
            checkbox.disabled = true;
            let link = null;
            const label = document.getElementById('auto-order-label');
            try {
                if (on && realtimeSymbol) {
                    // 只在"开启"路径检查；"关闭"永远允许 —— 不能因为页面品种变了就关不掉。
                    // 置灰已把这条挡在"点之前"，这里保留为**兜底**（接口降级为放行时，
                    // 用户仍可能点到；且升级/多标签页场景下前端状态可能过期）。
                    const chk = await checkSymbolTradable(realtimeSymbol);
                    if (!chk.allowed) {
                        checkbox.checked = false;   // 回弹开关，且**不发启动请求**
                        console.warn('[auto-order] 品种不支持自动下单，已取消开启: '
                            + realtimeSymbol + '  ' + chk.message);
                        showAlert('不支持自动下单\n\n' + chk.message);
                        return;
                    }
                }
                if (on) {
                    // 登录链路：确认 → 选中值；取消 / 点框外 / Esc → null = 不启动
                    link = await pickTradeLink();
                    if (!link) { checkbox.checked = false; return; }
                }
                if (label) label.textContent = on ? '启动中…' : '关闭中…';
                const opts = { method: 'POST', cache: 'no-store' };
                opts.headers = { 'Content-Type': 'application/json' };
                if (on) {
                    // 把当前页面品种/周期/服务地址带给引擎（--source sse 订阅该行情流）
                    opts.body = JSON.stringify({
                        symbol: realtimeSymbol || null,
                        freq: currentFreq || null,
                        sse_base: location.origin,
                        link: link            // 登录链路（simnow / live）
                    });
                } else {
                    // 关闭也带品种（§3.10.2）：只停本页品种的运行中实例——
                    // 多实例下不带 symbol 会走「停全部」，把别的品种一起停掉
                    opts.body = JSON.stringify({ symbol: realtimeSymbol || null });
                }
                console.info('[auto-order] ' + (on ? '开启' : '关闭') + ' 请求: '
                    + (opts.body || '(无 body)') + '  url=/api/trader/auto-order/'
                    + (on ? 'on' : 'off'));
                const resp = await fetch(on ? '/api/trader/auto-order/on' : '/api/trader/auto-order/off', opts);
                let detail = null;
                try {
                    const j = await resp.json();
                    detail = (j && j.detail) || null;
                } catch (e) { /* 非 JSON 响应 */ }
                console.info('[auto-order] 响应: HTTP ' + resp.status
                    + (detail ? '  detail=' + detail : ''));
                if (!resp.ok) {
                    throw new Error(detail || ('HTTP ' + resp.status));
                }
                // 起停成功后复位探测基线（2026-10-09 用户反馈）：开启 → 首次轮询即
                // 运行中；**关闭** → 用户主动关闭绝不能被判成「异常退出」而弹
                // 「交易引擎已退出!」（原实现只在 on 分支复位，关闭路径一旦那一拍
                // poll 没落地，下一轮 5s 轮询就会把主动关闭报成异常退出）。
                autoOrderPrevRunning = null;
                await pollAutoOrderStatus();
            } catch (err) {
                // 失败回弹 + 提示（实盘安全闸门 / 配置缺失等 AppError → detail）
                checkbox.checked = !on;
                console.error('[auto-order] ' + (on ? '开启' : '关闭') + '失败: '
                    + (err && err.message ? err.message : err));
                showAlert('交易引擎' + (on ? '开启' : '关闭') + '失败：' + (err && err.message ? err.message : err));
            } finally {
                autoOrderBusy = false;
                // 用置灰态重算 disabled，而不是无脑置 false ——
                // 否则一次开启/关闭请求就会把"未标定品种"的灰态解除
                applyAutoOrderTradableUI();
                // 仅在未成功起停时复位标签：开启成功路径已在 pollAutoOrderStatus()
                // 里把标签渲染成彩色链路（SimNow / 期货公司名），若这里无脑复位会
                // 让标签闪一下"自动下单"再等下一轮 5s 轮询才恢复（纯视觉瑕疵）。
                // 成功起停后 autoOrderRunning 已是目标态，跳过复位即可保持彩色标签。
                if (label && !autoOrderRunning) {
                    label.textContent = '自动下单';
                    label.className = 'auto-order-label';
                }
            }
        }

        // 关键修复：开关控件用内联 onchange="onAutoOrderToggle(this)"，
        // 内联事件在全局作用域执行。而本函数在 IIFE 闭包内，原本不可见，
        // 导致每次点开关都抛 ReferenceError、从不发请求
        // （症状：开关视觉上开了又自动关、后端无任何日志/state 目录）。
        // 挂到全局后内联 onchange 才能触达。
        window.onAutoOrderToggle = onAutoOrderToggle;

        // 同理：账本按钮用内联 onclick="toggleAutoOrderLedger(event)"，
        // 内联事件在全局作用域执行 —— 不挂 window 的话点击直接抛
        // ReferenceError，按钮看起来毫无反应（P65 复刻了上面开关的坑）。
        window.toggleAutoOrderLedger = toggleAutoOrderLedger;

        // 自动下单轮询 Worker 源码（r8 由独立文件 ao-poll-worker.js 合并内嵌：
        // 用户不想多一个文件。用 Blob URL 创建 —— Worker 独立事件循环的定时器
        // 不受 Chrome intensive throttling 节流，这与 Worker 的创建方式无关，
        // 源码内嵌不影响该性质。注意：本模板字符串内不得出现反引号与 ${。
        const AO_WORKER_SRC = `        // -*- coding: utf-8 -*-
        // 自动下单状态轮询 Worker
        // =======================
        // 为什么轮询要在 Worker 里跑：后台标签页的主线程 setInterval 会被 Chrome
        // intensive throttling 节流到 ~1 次/分钟（页面隐藏 ≥5 分钟后生效），自动下单
        // 的系统通知（Notification）因此延迟可达 60s+ —— 2026-09-23 实盘：快期3
        // 秒级显示仓单，右下角通知一分钟级别才出，而 events.jsonl 证明交易引擎在同一秒
        // 就写好了 toast，延迟全部在「前端轮询」这一环。Worker 内的全局作用域独立，
        // 定时器不受该节流；每 5s 拉一次状态 postMessage 回主线程，主线程收到消息
        // 立即应用（消息任务是普通任务，同样不被节流）。
        //
        // 协议：
        //   主线程 → Worker：{type: 'start', base}  开始轮询（启动即拉一次，不等首周期）
        //                    base = location.origin（**必带**：Worker 的 base 是
        //                    blob: URL，根相对路径解析不出来，见 _statusUrl）
        //                    {type: 'stop'}   停止轮询
        //   Worker → 主线程：{type: 'status', data: <status 响应 JSON>}
        //                    {type: 'poll-error', message}  本轮拉取失败（不再静默）
        //
        // fetch 走 _statusUrl() 拼出的**绝对** URL（base + '/api/...'），与本页面同源。

        var _timer = null;
        var POLL_MS = 5000;
        var API_BASE = '';        // 主线程 {type:'start', base} 下发，见 _statusUrl

        // ⚠️ status 必须拼成**绝对 URL**（origin + 路径）：根相对路径 '/api/...'
        // 在本 Worker 里**永远解析不出来** —— 这是 2026-09-24 定位到的根因。
        //   Worker 由 Blob URL 创建 → self.location.href = blob:http://host/<uuid>；
        //   blob: 是 cannot-be-a-base scheme（没有可解析的基路径），实测（无头
        //   Edge/Chromium，同源码对照）：
        //     new URL('/ping', self.location.href)  → throw Invalid URL
        //     fetch('/ping')                        → TypeError: Failed to parse
        //                                             URL from /ping
        //     fetch(location.origin + '/ping')      → HTTP 200 ✅
        //   于是 2026-09-23 把轮询搬进 Worker 后，每一次 _poll 都在这一行抛
        //   TypeError 并被 .catch 静默吞掉 —— 轮询"活着"但永远没有数据：
        //   开平仓弹窗不弹、账本不刷、保护价线不画、后台通知延迟，只有手动
        //   开关自动下单（onAutoOrderToggle 里那一次主线程 poll）才刷新一拍
        //   —— 实盘表现即"重开后连弹 5 条积压 + 账本同时刷新"。
        //   主线程下发的 base 优先，self.location.origin 只作兜底（Worker 的
        //   origin 与页面同源，blob: 继承之）。
        function _statusUrl() {
            return (API_BASE || self.location.origin || '')
                + '/api/trader/auto-order/status';
        }

        // 单次请求超时（AbortController 手写版，不用 AbortSignal.timeout ——
        // 那是 Chrome 103+ 才有）。为什么必须有：fetch 默认**永不超时**，
        // 服务端万一 hang 住（线程池占满 / 锁等待），上一轮 fetch 永远 pending
        // 也不会自己结束 —— 表现就是"轮询活着但永远没有下一拍"。8s = 2 倍
        // 正常轮询间隔。
        function _timeoutedFetch(url) {
            if (typeof AbortController === 'undefined') {
                return fetch(url, { cache: 'no-store' });
            }
            var ctl = new AbortController();
            var killer = setTimeout(function () { ctl.abort(); }, 8000);
            return fetch(url, { cache: 'no-store', signal: ctl.signal })
                .finally(function () { clearTimeout(killer); });
        }

        function _poll() {
            _timeoutedFetch(_statusUrl())
                .then(function (r) {
                    if (!r.ok) return null;      // HTTP 4xx/5xx：本轮放弃，下轮再试
                    return r.json();
                })
                .then(function (j) {
                    if (j) self.postMessage({ type: 'status', data: j });
                })
                .catch(function (e) {
                    // 不再静默：把失败回传主线程打 console.error。
                    //   静默 catch 正是这次根因潜伏一整天的直接原因 —— 同样的
                    //   失败若再发生，F12 里能立刻看到，而不是靠"现象猜"。
                    self.postMessage({ type: 'poll-error',
                        message: String((e && e.message) || e) });
                });
        }

        self.onmessage = function (e) {
            var d = e.data || {};
            if (d.type === 'start') {
                if (d.base) API_BASE = d.base;
                if (_timer) clearInterval(_timer);
                _timer = setInterval(_poll, POLL_MS);
                _poll();
            } else if (d.type === 'stop') {
                if (_timer) { clearInterval(_timer); _timer = null; }
            }
        };
`;

        // 轮询：实时模式下每 5s 刷新一次状态。
        // 轮询本体在上方 AO_WORKER_SRC（Web Worker）：后台标签的主线程
        // setInterval 会被 Chrome intensive throttling 节流到 ~1 次/分钟
        //（页面隐藏 ≥5 分钟后生效），Worker 内定时器不受该节流 —— 这正是
        // 2026-09-23「右下角系统通知延迟一分钟」的根因。Worker 创建失败
        //（无 Worker 环境 / 老浏览器）回退主线程 setInterval（旧行为，含
        // 实时页可见性判断；后台节流的延迟边界在该回退路径下仍然存在）。
        //
        // 可见性门控的取舍（与旧版**唯一**的行为差异，刻意保留，勿"对齐"回去）：
        //   旧版两条路径都有 `wrap.visible` 门控 —— 只有实时页可见才拉 status。
        //   Worker 路径必须去掉它，因为 p64 后台系统通知正是"页面隐藏时也要
        //   及时弹"的需求本身：加回门控 = 后台标签永不轮询 = 本次改动动机归零。
        //   代价只是页面不在实时页时仍每 5s 一次轻量 GET（status 是纯内存读）。
        //   回退路径保留门控：那条路径本就跑在老浏览器上、且已被节流到分钟级，
        //   它拿不到及时通知，留门控只为少发无谓请求 —— 两条路径的语义差是
        //   能力差导致的，不是漏写。
        (function startAutoOrderPolling() {
            let fellBack = false;
            function fallback() {
                if (fellBack) return;
                fellBack = true;
                autoOrderPollTimer = setInterval(function() {
                    const wrap = document.getElementById('auto-order-wrap');
                    if (wrap && wrap.classList.contains('visible')) {
                        pollAutoOrderStatus();
                    }
                }, 5000);
            }
            try {
                if (typeof Worker === 'undefined') { fallback(); return; }
                const _wblob = new Blob([AO_WORKER_SRC],
                    { type: 'application/javascript' });
                const w = new Worker(URL.createObjectURL(_wblob));
                w.onmessage = function(e) {
                    const d = e.data || {};
                    if (d.type === 'status' && d.data) applyAutoOrderStatus(d.data);
                    else if (d.type === 'poll-error') console.error('[auto-order] Worker 轮询失败: ' + d.message);
                };
                w.onerror = function() { fallback(); };
                autoOrderWorker = w;
                // 启动轮询：Worker 侧定时器只在收到 {type:'start'} 时才建立
                //（协议见 AO_WORKER_SRC 注释）。少了这一行 = Worker 建了却
                // 永不轮询，而 fallback 只在「无 Worker / 构造抛错 / onerror」
                // 三条路径触发 —— Blob Worker 语法正确不会走 onerror，于是
                // 新旧两条轮询路径同时归零：开关态、账户三态、未确认告警、
                // p64 系统通知、引擎账本、保护价徽标全部停摆，且页面无任何
                // 报错（静默失效）。接线由 test_p66 [4] 组用 node 跑真片段
                // 守卫：删掉这一行该组必红。
                //
                // base: location.origin —— **必带**。Worker 的 base 是 blob: URL
                // （cannot-be-a-base），根相对路径在那里解析不出来（2026-09-24
                // 根因，实测 fetch('/api/...') 抛 TypeError: Failed to parse URL
                // from /api/...），status 必须由主线程把 origin 送进去拼成绝对
                // URL。接线同样由 test_p66 [4-U8] 守卫：把 base 去掉该组必红。
                w.postMessage({ type: 'start', base: location.origin });
            } catch (e) { fallback(); }
        })();

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
                // 流通市值过滤下限：后端 SCAN_MIN_FLOAT_MC 为单一事实源，仅用于输入框
                // placeholder 与「未配置」语义，不覆盖用户显式设置值。
                if (data && data.config && typeof data.config.scan_min_float_mc === 'number'
                    && data.config.scan_min_float_mc >= 0) {
                    _scanMinFloatMcServer = data.config.scan_min_float_mc;
                    var _mcInput2 = document.getElementById("scan-min-float-mc");
                    if (_mcInput2) _mcInput2.placeholder = String(_scanMinFloatMcServer);
                }
                // 放量扫描比较窗口根数：同样以后端 SCAN_FANGLIANG_WINDOW_BARS 为
                // 单一事实源，仅用于结果面板的口径披露文案（不参与判定）。
                if (data && data.config
                    && typeof data.config.scan_fangliang_window_bars === 'number'
                    && data.config.scan_fangliang_window_bars >= 1) {
                    _scanFangliangWindowBars = data.config.scan_fangliang_window_bars;
                }
            } catch (e) { /* 离线兜底：保留本地常量 */ }
        })();

        // 关闭/刷新页面时保存状态（仅股票，期货不保存）
        window.addEventListener('beforeunload', function() { saveLastState(); saveLastView(); });


        // ChanApp 暴露至全局（控制台调试 ChanApp.state 入口；内部仍走闭包）
        window.ChanApp = ChanApp;

    })();
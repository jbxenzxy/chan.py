# -*- coding: utf-8 -*-
"""
FrontAPI.py —— FastAPI 统一入口 · 面向前端 · REST + SSE（阶段 3 落地）
=========================================================================
V10 方案 8.6（3a REST 路由迁移 / 3b-1 SSE 原生异步生成器）：

  3a · REST 路由迁移：
    - 30 条 REST 路由收敛到本文件（单一路由源），路由保持薄：
      参数校验 + run_in_threadpool(orch.*) + 响应组装，业务段在 App/AppOrch.py；
    - 引擎调用一律经 AppOrch 的 call_* 漏斗（锁分类见 AppOrch.LOCK_POLICY），
      本文件不直连 m.analyze_stock / m.compute_red_range_zs /
      m.stock_manual_select_point / m.futures_manual_select_point
      （Test/test_phase3_guards.py G3 守护）；
    - api_server.py 退役为兼容壳：app/router 为本文件别名，
      SSE Mock 桥接层（_SSEMockWfile/_SSEMockHandler/_sse_generator）
      亦迁至本文件（3b-2 灰度通过后随旧路径一并拆除）。

  3b-1 · SSE 原生异步生成器（本阶段上线，旧路径保持可用）：
    - ChartHandler._handle_sse_stream_dual/single（write() 式，约 1042 行）
      忠实移植为原生异步生成器 sse_futures_stream_dual/single；
    - 数据源抽象 SSESource：生产 TqSdkSource（每连接独立 TqApi+CChan，
      SELF_CONTAINED 锁分类，不加引擎锁）/ 灰度测试注入 MockSource
      （Test/test_sse_gray.py 驱动确定性比对）；
    - /api/futures_stream 双实现并存：impl=legacy（默认，零行为漂移）|
      impl=native。灰度 7 天（高风险档）比对无差异后翻转默认并进入
      3b-2：拆除 ChartHandler SSE 方法与 Mock 桥接层。

  忠实移植口径（3b-1 与遗留实现的事件协议逐项一致）：
    - 事件名/载荷构造/错误事件：init（含失败载荷）→ update（tick 路径与
      K线完成路径）→ 心跳注释帧 ": heartbeat"，首事件类型与序列不变；
    - 阻塞引擎调用（wait_update/init_chan_symbol/step_load/快照提取）
      经 run_in_threadpool 执行，事件循环不再被单连接独占（遗留实现
      每连接占一线程，语义等价）；
    - 客户端断开：遗留靠 heartbeat 写失败（BrokenPipe）退出；原生靠
      生成器取消（GeneratorExit/CancelledError）触发 finally 清理，
      清理动作（关 TqApi、弹 records/期货缓存）逐项一致。

锁分类（AppOrch.LOCK_POLICY，阶段 2 遗留问题的解法）：
    SERIAL         串行分析（call_* 漏斗持 _ENGINE_LOCK）
    SCAN           并行扫描（ScannerService.scan_one，_scan_lock 防同票重入）
    SELF_CONTAINED SSE 流（每连接独立会话，不加锁）

启动：
    python FrontAPI.py                     # 推荐入口（端口/地址走 App/AppConfig.py）
    uvicorn FrontAPI:app --port 18081      # 等效
兼容：
    python api_server.py                   # 兼容入口（app 为本文件实例别名）
"""
import sys
import os
import json
import time
import queue
import asyncio
import threading
import traceback

# ── 仓库根目录引导（App/ 包、my_chan_main 均位于仓库根）────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Python 3.10 兼容垫片（原项目使用 typing.Self，需 3.11+；3.11+ 下自动跳过）
import typing
if not hasattr(typing, "Self"):
    try:
        from typing_extensions import Self
        typing.Self = Self
    except ImportError:
        pass

from fastapi import FastAPI, Query, HTTPException, Body, APIRouter, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.concurrency import run_in_threadpool

from App.AppConfig import app_config
from App import AppOrch as orch
from App.AppOrch import AppError  # 领域异常统一在服务层定义（方案 7.7）

# 引擎模块（import my_chan_main 触发其模块级初始化，与 api_server 行为一致）
import my_chan_main as m  # noqa: F401  （仅读取常量/纯函数/SSE 调试旗，引擎入口一律走 orch.* 漏斗）


# ═══════════════════════════════════════════════════════════════════════
# 应用组装
# ═══════════════════════════════════════════════════════════════════════

app = FastAPI(title="缠论分析 API", version="1.2.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """领域异常 → 统一日志 + 统一 JSON（状态码读取异常自身）"""
    print(f"[FrontAPI] 领域异常 {exc.__class__.__name__}: {exc} ({request.url.path})", flush=True)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.__class__.__name__, "detail": str(exc)},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """未预期异常兜底：500 + 日志，不向客户端泄漏内部细节（7.7 规则）"""
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"error": "InternalError", "detail": "服务器内部错误"},
    )


@app.get("/api/health", tags=["meta"])
async def health():
    """健康检查：入口标识 + 配置摘要（凭据打码，方案 7.3）"""
    return {
        "status": "ok",
        "entry": "FrontAPI",
        "version": app.version,
        "symbol_code": m.SYMBOL_CODE,
        "config": app_config.as_dict(redact=True),
    }


# ═══════════════════════════════════════════════════════════════════════
# 辅助：统一 JSON 响应（自 api_server 迁入，兼容壳再导出）
# ═══════════════════════════════════════════════════════════════════════

def _json_response(data, status_code: int = 200):
    """统一 JSON 返回（兼容原有 send_json_response 行为）"""
    return JSONResponse(
        content=data,
        status_code=status_code,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# SSE · Legacy 桥接层（自 api_server 迁入；3b-2 灰度通过后拆除）
# 将 ChartHandler 的 write() 式 SSE 方法桥接到 StreamingResponse
# ═══════════════════════════════════════════════════════════════════════

class _SSEMockWfile:
    """模拟 socket.SocketIO，将 write() 转为 enqueue"""
    def __init__(self):
        self.q = queue.Queue()

    def write(self, data: bytes):
        self.q.put(data)

    def flush(self):
        pass


class _SSEMockHandler:
    """模拟 ChartHandler，提供 SSE handler 所需的最小接口"""
    def __init__(self):
        self.wfile = _SSEMockWfile()

    def send_response(self, code: int):
        pass

    def send_header(self, keyword: str, value: str):
        pass

    def end_headers(self):
        pass

    def send_json_response(self, data, status_code: int):
        err = json.dumps(data, ensure_ascii=False, allow_nan=False)
        self.wfile.write(f"event: error\ndata: {err}\n\n".encode("utf-8"))


def _sse_generator(handler_method, *args):
    """在独立线程中运行 ChartHandler 的 SSE 方法，yield 其输出"""
    mock = _SSEMockHandler()

    def _run():
        try:
            handler_method(mock, *args)
        except Exception as exc:
            traceback.print_exc()
            err = json.dumps({"error": str(exc)}, ensure_ascii=False)
            mock.wfile.q.put(f"event: error\ndata: {err}\n\n".encode("utf-8"))
        finally:
            mock.wfile.q.put(None)          # 哨兵

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    while True:
        data = mock.wfile.q.get()
        if data is None:
            break
        yield data


# ═══════════════════════════════════════════════════════════════════════
# SSE · 数据源抽象（3b-1 灰度注入点，设计 8.6-3b）
# ═══════════════════════════════════════════════════════════════════════

class SSESourceClosed(Exception):
    """数据源正常关闭信号（仅 Mock/回放数据源使用；生产 TqSdk 源永不抛出）

    原生生成器收到本信号即正常收尾（等价于客户端断开后的自然结束），
    供 Test/test_sse_gray.py 以有限脚本驱动无限循环协议。
    """


class SSESource:
    """SSE 流数据源抽象 · 锁分类 SELF_CONTAINED

    生产实现 TqSdkSource：真实天勤连接，每 SSE 连接独立 TqApi + CChan，
    不触碰共享分析缓存（_futures_analysis_cache 仅按下窗键存放供
    /api/dual_zs 读取，启动写入/收尾弹出，无跨连接竞争）。
    灰度/测试注入 MockSource：脚本化 K 线序列驱动确定性事件流。

    全部方法为同步阻塞调用——原生生成器经 run_in_threadpool 调用，
    不阻塞事件循环（与遗留实现「每连接一线程」语义等价）。
    """

    def connect(self):
        """建立数据源连接（每 SSE 连接一次）"""
        raise NotImplementedError

    def init_chan(self, symbol, name, freq_sec, freq_label, start_time=None):
        """拉取历史 K 线 + chan 分析，返回 (chan, klines, kl_type, records) 或 None"""
        raise NotImplementedError

    def get_kline_serial(self, symbol, freq_sec):
        """获取实时 K 线序列引用（同 (symbol, freq_sec) 返回同一对象，天勤语义）"""
        raise NotImplementedError

    def wait_update(self, deadline_ns):
        """阻塞等待行情更新（deadline 纳秒壁钟）"""
        raise NotImplementedError

    def last_records(self, code_key):
        """读取最近 N 条已注入记录（去重判断用）"""
        raise NotImplementedError

    def append_bar(self, bar, code_key):
        """向引擎注入一根已完成 K 线"""
        raise NotImplementedError

    def step_load(self, chan):
        """驱动 chan 增量计算（耗尽生成器）"""
        raise NotImplementedError

    def extract_snapshot(self, chan, kl_type, symbol, name, freq_label,
                         saved_selection_date="", klines=None):
        """从 chan 提取实时快照 dict（遗留 _extract_realtime_snapshot）"""
        raise NotImplementedError

    def white_hline(self, kl_list, freq):
        """计算白色横虚线（遗留 _calc_futures_white_hline）"""
        raise NotImplementedError

    def close(self):
        """关闭数据源连接（异常自吞并打印，遗留行为）"""
        raise NotImplementedError

    def cleanup_records(self, code_key):
        """清理该连接的 K 线注入缓存（异常自吞并打印，遗留行为）"""
        raise NotImplementedError


class TqSdkSource(SSESource):
    """生产数据源：天勤 TqApi + my_chan_main 引擎链路（忠实于遗留 ChartHandler）"""

    def __init__(self):
        self.api = None
        self._serials = {}      # (symbol, freq_sec) → kline serial（天勤同参同对象语义）

    def connect(self):
        from tqsdk import TqApi, TqAuth
        from DataAPI.TqSdkAPI import TQ_ACCOUNT, TQ_PASSWORD
        self.api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))

    def init_chan(self, symbol, name, freq_sec, freq_label, start_time=None):
        return m.init_chan_symbol(self.api, symbol, name, freq_sec, freq_label,
                                  start_time=start_time)

    def get_kline_serial(self, symbol, freq_sec):
        key = (symbol, freq_sec)
        if key not in self._serials:
            self._serials[key] = self.api.get_kline_serial(symbol, freq_sec)
        return self._serials[key]

    def wait_update(self, deadline_ns):
        return self.api.wait_update(deadline=deadline_ns)

    def last_records(self, code_key):
        from DataAPI.TqSdkAPI import CTqSdkAPI
        return CTqSdkAPI.get_last_n(1, symbol=code_key)

    def append_bar(self, bar, code_key):
        from DataAPI.TqSdkAPI import CTqSdkAPI
        CTqSdkAPI.append_bar(bar, symbol=code_key)

    def step_load(self, chan):
        for _snapshot in chan.step_load():
            pass

    def extract_snapshot(self, chan, kl_type, symbol, name, freq_label,
                         saved_selection_date="", klines=None):
        return m._extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                            saved_selection_date=saved_selection_date,
                                            klines=klines)

    def white_hline(self, kl_list, freq):
        return m._calc_futures_white_hline(kl_list, freq, m._get_date_fmt(freq))

    def close(self):
        if self.api is not None:
            try:
                self.api.close()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

    def cleanup_records(self, code_key):
        try:
            from DataAPI.TqSdkAPI import CTqSdkAPI
            CTqSdkAPI._records_by_symbol.pop(code_key, None)
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# SSE · 原生异步生成器（3b-1，忠实移植自 ChartHandler._handle_sse_stream_*）
# ═══════════════════════════════════════════════════════════════════════

def _sse_frame(event, payload) -> bytes:
    """构造一帧 SSE 事件（与遗留实现的字节格式逐字一致）"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, allow_nan=False)}\n\n".encode("utf-8")


async def sse_futures_stream_single(symbol, freq="15s", start_time=None, source=None):
    """期货 SSE 单窗口 · 原生异步生成器（3b-1）

    忠实移植 ChartHandler._handle_sse_stream_single 的事件协议：
    init（初始快照/失败载荷）→ 实时循环（heartbeat 注释帧 + update 事件：
    tick 路径更新末根 K 线 OHLC/MACD，K线完成路径全量快照）→ 收尾清理。
    source 可注入（默认 TqSdkSource）；Test/test_sse_gray.py 用 MockSource
    驱动确定性比对。锁分类 SELF_CONTAINED（见 AppOrch.LOCK_POLICY）。
    """
    import logging
    from datetime import datetime
    logging.getLogger("tqsdk").setLevel(logging.WARNING)
    logging.getLogger("tqsdk.tqapi").setLevel(logging.WARNING)
    for h in logging.root.handlers:
        h.setLevel(logging.WARNING)

    src = source if source is not None else TqSdkSource()

    from DataAPI.TqSdkAPI import FREQ_SEC_MAP, FREQ_LABEL_CN, FUTURES_ALIASES

    display_key = None
    freq_sec = None
    try:
        # 别名解析：支持 PTA→KQ.m@CZCE.TA 等短名称
        symbol_upper = symbol.upper()
        if symbol_upper in FUTURES_ALIASES:
            symbol = FUTURES_ALIASES[symbol_upper]

        freq_sec = FREQ_SEC_MAP.get(freq, 15)
        freq_label = freq
        freq_cn = FREQ_LABEL_CN.get(freq_label, freq_label)
        display_key = f"{symbol}:{freq_cn}"

        # 如果没有传入 start_time，查询CSV中是否有保存的选点
        # （阶段 4：经 AppOrch 漏斗读 AppData，不再直连 my_chan_main 状态）
        if start_time is None:
            col = m.FREQ_TO_COL.get(freq, "")
            if col:
                _saved = orch.get_saved_point(symbol, freq) or None
                if _saved:
                    start_time = _saved
                    print(f"[{display_key}] 检测到保存选点: {start_time}")

        saved_selection_date = start_time or ""

        t_conn = time.time()
        await run_in_threadpool(src.connect)
        print(f"[{display_key}] ⓪ 连接天勤: 耗时 {time.time()-t_conn:.1f}s")

        t_total = time.time()
        name = m._get_futures_name(symbol)  # 品种名称

        # === 1. 拉取历史 + chan 分析 ===
        result = await run_in_threadpool(src.init_chan, symbol, name,
                                         freq_sec, freq_label, start_time)
        if result is None:
            yield _sse_frame("init", {"error": "初始化失败（无数据或网络异常）", "symbol": symbol})
            return
        chan, klines, kl_type, records = result

        # === 2. 推送初始快照 ===
        t0 = time.time()
        try:
            init_data = await run_in_threadpool(
                src.extract_snapshot, chan, kl_type, symbol, name, freq_label,
                saved_selection_date)
            # ★ 追加当前形成中的K线（klines[-1]），让前端立即看到新K线
            if klines is not None and len(klines) > 0:
                _lr = klines.iloc[-1]; _dns = _lr.get('datetime')
                if _dns is not None:
                    _bdt = datetime.fromtimestamp(_dns / 1e9)
                    _bds = _bdt.strftime(m._get_date_fmt(freq))
                    _ex = init_data.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _bds:
                        _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                            'open': round(float(_lr.get('open', 0) or 0), 3),
                            'high': round(float(_lr.get('high', 0) or 0), 3),
                            'low': round(float(_lr.get('low', 0) or 0), 3),
                            'close': round(float(_lr.get('close', 0) or 0), 3),
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        m._inherit_macd_for_preview_bar(_ex)
                        init_data['meta']['kline_count'] = len(_ex)
            # 计算白色横虚线（初始快照，K线已确认状态）
            _kl_list = chan[kl_type]
            init_data['white_hline'] = await run_in_threadpool(src.white_hline, _kl_list, freq)
            yield _sse_frame("init", init_data)
            cached_snapshot = init_data  # ★ 缓存完整快照，tick推送时更新最后一根K线OHLC
            if m._SSE_DEBUG:
                print(f"[{display_key}] ⑶ 推送init: "
                      f"K线{init_data['meta']['kline_count']}, "
                      f"笔{init_data['meta']['bi_count']}, "
                      f"中枢{init_data['meta']['zs_count']}, "
                      f"耗时 {time.time()-t0:.1f}s")
        except Exception as _e:
            yield _sse_frame("init", {"error": f"快照提取失败: {_e}", "symbol": symbol})
            return

        # === 3. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照 ===
        # 策略与遗留实现一致：壁钟（datetime.now()）判断K线周期结束，不等天勤
        # klines 推进信号；周期结束后 klines[-1] OHLC 已冻结可直接入缠论。
        BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
        if m._SSE_DEBUG:
            print(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

        # last_bar_dt_ns: klines[-1] 的时间戳，用于检测 klines 是否推进
        # last_processed_dt_ns: 已处理过的K线时间戳，防止同一根K线被重复处理
        last_bar_dt_ns = None
        last_processed_dt_ns = None
        last_debug_print = time.time()

        # 性能统计
        t_wait_total = 0.0
        t_tick_total = 0.0
        t_step_total = 0.0
        t_snapshot_total = 0.0
        t_push_total = 0.0
        loop_count = 0
        tick_count = 0
        step_count = 0
        last_perf_print = time.time()

        while True:
            try:
                t_wait_start = time.time()
                await run_in_threadpool(src.wait_update, time.time() * 1e9 + 100_000_000)
                t_wait = time.time() - t_wait_start
                t_wait_total += t_wait
            except SSESourceClosed:
                # 数据源正常关闭（仅 Mock/回放源）：等价客户端断开的自然结束
                return
            except Exception as _e:
                print(f"[{display_key}] wait_update 异常: {_e}")
                await asyncio.sleep(0.5)
                continue

            # 心跳注释帧（客户端断开由生成器取消机制触发 finally 清理）
            yield b": heartbeat\n\n"

            loop_count += 1

            now = datetime.now()
            now_ts = now.timestamp()

            if len(klines) == 0:
                continue

            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            if dt_ns is None:
                continue

            # ★ 诊断：对比 tqsdk 实时 K 线和 chan 框架内部 K 线的时间差
            if loop_count == 1 or (loop_count % 50 == 0):
                chan_last_klu = None
                try:
                    chan_kl_list = chan[kl_type]
                    if chan_kl_list.lst:
                        last_klc = chan_kl_list.lst[-1]
                        if last_klc.lst:
                            chan_last_klu = last_klc.lst[-1]
                except Exception as _e:
                    print(f"[警告] 异常: {type(_e).__name__}: {_e}")
                tqsdk_last_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime('%H:%M:%S') if dt_ns else "None"
                chan_last_dt = chan_last_klu.time.to_str()[:16] if chan_last_klu and hasattr(chan_last_klu, 'time') else "None"
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DIAG] 循环#{loop_count} | "
                          f"tqsdk klines[-1]={tqsdk_last_dt} | "
                          f"chan kl_list[-1]={chan_last_dt} | "
                          f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

            # 初始化
            if last_bar_dt_ns is None:
                last_bar_dt_ns = dt_ns
                last_debug_print = now_ts
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DEBUG] 初始化: klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                continue

            # 每60秒打印一次性能统计
            if now_ts - last_perf_print >= 60.0:
                last_perf_print = now_ts
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [PERF] 循环{loop_count}次 | "
                          f"wait_update总计={t_wait_total:.1f}s | "
                          f"tick推送{tick_count}次总计={t_tick_total:.1f}s | "
                          f"step_load{step_count}次总计={t_step_total:.1f}s | "
                          f"快照提取总计={t_snapshot_total:.1f}s | "
                          f"SSE推送总计={t_push_total:.1f}s")
                t_wait_total = 0.0
                t_tick_total = 0.0
                t_step_total = 0.0
                t_snapshot_total = 0.0
                t_push_total = 0.0
                loop_count = 0
                tick_count = 0
                step_count = 0

            # ★ DEBUG: 每2秒打印一次当前状态
            if now_ts - last_debug_print >= 2.0:
                last_debug_print = now_ts
                bar_dt = datetime.fromtimestamp(dt_ns / 1e9)
                lag = now_ts - (dt_ns / 1e9 + freq_sec)
                pushed = (dt_ns != last_bar_dt_ns)
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DEBUG] 壁钟={now.strftime('%H:%M:%S')} "
                          f"klines[-1]={bar_dt.strftime('%H:%M:%S')} "
                          f"过期={lag:+.1f}s 推进={pushed} "
                          f"O={last_row.get('open'):.1f} H={last_row.get('high'):.1f} "
                          f"L={last_row.get('low'):.1f} C={last_row.get('close'):.1f}")

            # --- 检测上一根K线是否已完成 ---
            klines_pushed = (dt_ns != last_bar_dt_ns)

            if klines_pushed:
                # klines 已推进 → 上一根K线（klines[-2]）已冻结，立即处理
                completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row
                last_bar_dt_ns = dt_ns
                bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DIAG] K线完成(klines推进): "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")
            else:
                # klines 未推进 → 用壁钟判断当前K线周期是否已结束
                bar_end_ts = (dt_ns / 1e9) + freq_sec + BAR_COMPLETION_BUFFER
                if now_ts < bar_end_ts:
                    # 周期未结束，更新缓存快照的最后一根K线OHLC后推送完整格式
                    t_tick_start = time.time()
                    try:
                        if cached_snapshot is not None:
                            ex = cached_snapshot.get('klines', [])
                            if ex:
                                o = round(float(last_row.get('open', 0) or 0), 3)
                                h = round(float(last_row.get('high', 0) or 0), 3)
                                l = round(float(last_row.get('low', 0) or 0), 3)
                                c = round(float(last_row.get('close', 0) or 0), 3)
                                ex[-1]['open'] = o
                                ex[-1]['high'] = h
                                ex[-1]['low'] = l
                                ex[-1]['close'] = c
                                # ★ 实时计算最后一根K线的MACD，避免前端跳变
                                closes = [k['close'] for k in ex]
                                if len(closes) >= 26:
                                    ema12 = m.ema(closes, 12)
                                    ema26 = m.ema(closes, 26)
                                    for i in range(len(ex)):
                                        if i < len(ema12):
                                            ex[i]['dif'] = round(ema12[i] - ema26[i], 4)
                                    difs = [ex[i]['dif'] for i in range(len(ex))]
                                    dea = m.ema(difs, 9)
                                    for i in range(len(ex)):
                                        if i < len(dea):
                                            ex[i]['dea'] = round(dea[i], 4)
                                            ex[i]['macd'] = round(2 * (ex[i]['dif'] - ex[i]['dea']), 4)
                                cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                                yield _sse_frame("update", cached_snapshot)
                                if tick_count == 0:
                                    if m._SSE_DEBUG:
                                        print(f"[{display_key}] [DIAG] 首次tick推送: "
                                              f"tqsdk klines[-1]={now.strftime('%H:%M:%S')} | "
                                              f"更新最后一根K线OHLC O={o} H={h} L={l} C={c} | "
                                              f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")
                                t_tick_total += time.time() - t_tick_start
                                tick_count += 1
                    except Exception as _e:
                        print(f"[警告] 异常: {type(_e).__name__}: {_e}")
                    continue
                # 壁钟到期，当前K线（klines[-1]）已冻结
                completed_row = last_row
                bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DIAG] K线完成(壁钟): "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")

            completed_dt_ns = completed_row.get("datetime")
            if completed_dt_ns is None:
                continue

            # 防止重复处理同一根K线
            if completed_dt_ns == last_processed_dt_ns:
                continue
            last_processed_dt_ns = completed_dt_ns

            # 提取 OHLC
            o = float(completed_row.get("open", 0) or 0)
            h = float(completed_row.get("high", 0) or 0)
            l = float(completed_row.get("low", 0) or 0)
            cl = float(completed_row.get("close", 0) or 0)
            vol = int(completed_row.get("volume", 0) or 0)
            h = max(h, o, cl)
            l = min(l, o, cl)

            dt = datetime.fromtimestamp(completed_dt_ns / 1e9)
            bar_expected_end = (completed_dt_ns / 1e9) + freq_sec
            delay = now_ts - bar_expected_end
            source_tag = "klines推进" if klines_pushed else "壁钟"

            code_key = f"{symbol}:{freq_sec}"
            new_bar = {
                "dt": dt, "open": round(o, 3), "high": round(h, 3),
                "low": round(l, 3), "close": round(cl, 3),
                "vol": vol, "amount": 0,  # 天勤K线无成交额，amount置0（前端期货显成交量vol）
            }
            t_append = time.time()

            last_records = await run_in_threadpool(src.last_records, code_key)
            t_step = 0.0
            if not last_records or last_records[0]["dt"] != dt:
                await run_in_threadpool(src.append_bar, new_bar, code_key)
                t_step_start = time.time()
                try:
                    await run_in_threadpool(src.step_load, chan)
                except Exception as _e:
                    print(f"[{display_key}] step_load 异常: {_e}")
                t_step = time.time() - t_step_start
                t_step_total += t_step
                step_count += 1

            if m._SSE_DEBUG:
                print(f"[{display_key}] 完成新K线[{source_tag}]: "
                      f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                      f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                      f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                      f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

            # 推送快照（此时 klines[-1] 已推进到 N 周期，快照中自然包含 N 的实时OHLC）
            t_snap_start = time.time()
            try:
                update_data = await run_in_threadpool(
                    src.extract_snapshot, chan, kl_type, symbol, name, freq_label,
                    saved_selection_date)
                # ★ 用 completed_time + freq_sec 计算下一根K线时间（不用klines[-1]，因为壁钟触发时klines未推进）
                _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                _next_ds = _next_dt.strftime(m._get_date_fmt(freq_label))
                _ex = update_data.get('klines', [])
                if not _ex or _ex[-1]['date'] != _next_ds:
                    _next_c = round(cl, 3)
                    _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                        'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                    m._inherit_macd_for_preview_bar(_ex)
                    update_data['meta']['kline_count'] = len(_ex)
                # K线确认后，计算白色横虚线（不在tick推送路径计算）
                _kl_list = chan[kl_type]
                update_data['white_hline'] = await run_in_threadpool(src.white_hline, _kl_list, freq)
                cached_snapshot = update_data  # ★ 更新缓存
                t_snap = time.time() - t_snap_start
                t_snapshot_total += t_snap
                t_push_start = time.time()
                yield _sse_frame("update", update_data)
                t_push = time.time() - t_push_start
                t_push_total += t_push
                if m._SSE_DEBUG:
                    print(f"[{display_key}] 推送更新: 快照提取={t_snap:.3f}s "
                          f"SSE写入={t_push:.3f}s "
                          f"(append+step_load={time.time()-t_append:.3f}s)")
            except Exception as _e:
                print(f"[{display_key}] 推送异常: {_e}")

    except asyncio.CancelledError:
        # 客户端断开（生成器被取消）：与遗留 BrokenPipe 分支等价，直接收尾
        raise
    except Exception as e:
        # 与遗留实现一致：打印连接异常后静默结束（错误已在 init 事件载荷中表达）
        print(f"[{display_key}] 连接异常: {e}")
    finally:
        await run_in_threadpool(src.close)
        # 清理该连接的K线缓存
        if symbol is not None and freq_sec is not None:
            await run_in_threadpool(src.cleanup_records, f"{symbol}:{freq_sec}")


async def sse_futures_stream_dual(symbol, main_freq="1m", sub_freq=None, start_time=None, source=None):
    """期货 SSE 双窗口 · 原生异步生成器（3b-1）

    忠实移植 ChartHandler._handle_sse_stream_dual 的事件协议：
    两个独立 CChan 对象、一次连接推送两个周期（下窗先处理——区间套分析
    需先分析次级别）。source 可注入（默认 TqSdkSource）。
    锁分类 SELF_CONTAINED（见 AppOrch.LOCK_POLICY）。
    """
    src = source if source is not None else TqSdkSource()

    from DataAPI.TqSdkAPI import FREQ_SEC_MAP, CTqSdkAPI, FUTURES_ALIASES
    from datetime import datetime

    # 确定周期
    if not sub_freq:
        sub_freq = m._FUTURES_DUAL_FREQ_MAP.get(main_freq, "15s")
    main_freq_sec = FREQ_SEC_MAP.get(main_freq, 60)
    sub_freq_sec = FREQ_SEC_MAP.get(sub_freq, 15)

    display_key = f"{symbol} 双窗口({main_freq}/{sub_freq})"
    if m._SSE_DEBUG:
        print(f"\n[{display_key}] ═══ SSE双窗口连接建立 ═══")

    try:
        # 别名解析
        symbol_upper = symbol.upper()
        if symbol_upper in FUTURES_ALIASES:
            symbol = FUTURES_ALIASES[symbol_upper]

        await run_in_threadpool(src.connect)
        name = m._get_futures_name(symbol)
        main_freq_label = main_freq
        sub_freq_label = sub_freq

        # 1. 查询选点状态（阶段 4：经 AppOrch 漏斗读 AppData）
        saved_selection_date = ""
        main_start_time = start_time
        sub_start_time = start_time
        try:
            qualified_code = symbol
            col_meta = m.FREQ_TO_COL.get(main_freq, "")
            if col_meta:
                saved_selection_date = orch.get_saved_point(qualified_code, main_freq)
                # 如果外部没传start_time，从CSV读取选点
                if main_start_time is None and saved_selection_date:
                    main_start_time = saved_selection_date
            # 下窗也查询选点
            sub_col_meta = m.FREQ_TO_COL.get(sub_freq, "")
            if sub_col_meta:
                sub_saved = orch.get_saved_point(qualified_code, sub_freq)
                if sub_start_time is None and sub_saved:
                    sub_start_time = sub_saved
        except Exception as _e:
            print(f"[警告] 异常: {type(_e).__name__}: {_e}")

        # 2. 拉取上窗历史 + chan分析
        if m._SSE_DEBUG:
            print(f"[{display_key}] 拉取上窗({main_freq})历史K线...")
        main_result = await run_in_threadpool(src.init_chan, symbol, name,
                                              main_freq_sec, main_freq_label, main_start_time)
        main_chan, main_records, main_kl_type, _ = main_result
        main_kl_type = m._get_kl_type(main_freq)
        if m._SSE_DEBUG:
            print(f"[{display_key}] 上窗({main_freq}) chan.py: 合并K线={len(main_chan[main_kl_type].lst)}, "
                  f"笔={len(main_chan[main_kl_type].bi_list)}, 中枢={len(main_chan[main_kl_type].zs_list)}")

        # 3. 拉取下窗历史 + chan分析
        if m._SSE_DEBUG:
            print(f"[{display_key}] 拉取下窗({sub_freq})历史K线...")
        sub_result = await run_in_threadpool(src.init_chan, symbol, name,
                                             sub_freq_sec, sub_freq_label, sub_start_time)
        sub_chan, sub_records, sub_kl_type, _ = sub_result
        sub_kl_type = m._get_kl_type(sub_freq)
        # 缓存下窗 CChan 供 /api/dual_zs 访问（语义化漏斗：key 规则内聚数据层）
        orch.futures_set_sub_chan(symbol, sub_freq, sub_chan)
        if m._SSE_DEBUG:
            print(f"[{display_key}] 下窗({sub_freq}) chan.py: 合并K线={len(sub_chan[sub_kl_type].lst)}, "
                  f"笔={len(sub_chan[sub_kl_type].bi_list)}, 中枢={len(sub_chan[sub_kl_type].zs_list)}")

        # 7. 提取初始快照
        t_snap = time.time()
        main_snapshot = await run_in_threadpool(
            src.extract_snapshot, main_chan, main_kl_type, symbol, name, main_freq_label,
            saved_selection_date=saved_selection_date)
        sub_snapshot = await run_in_threadpool(
            src.extract_snapshot, sub_chan, sub_kl_type, symbol, name, sub_freq_label,
            klines=None)
        # 期货双窗口：上窗 bis 的 fx_a_raw_dt/fx_b_raw_dt 是上层K线时间，
        # 需要换算成子级别K线时间，前端 calcRedRange 才能正确匹配
        m._futures_red_range(main_snapshot, main_freq_sec, sub_freq_sec, sub_freq)

        # ★ 追加上下窗当前形成中的K线（与单窗口一致），让前端立即看到，且 tick 更新正确的 K 线
        _main_klines_for_init = await run_in_threadpool(src.get_kline_serial, symbol, main_freq_sec)
        _sub_klines_for_init = await run_in_threadpool(src.get_kline_serial, symbol, sub_freq_sec)
        # 上窗
        if _main_klines_for_init is not None and len(_main_klines_for_init) > 0:
            _lr = _main_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
            if _dns is not None:
                _bdt = datetime.fromtimestamp(_dns / 1e9)
                _bds = _bdt.strftime(m._get_date_fmt(main_freq))
                _ex = main_snapshot.get('klines', [])
                if not _ex or _ex[-1]['date'] != _bds:
                    _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                        'open': round(float(_lr.get('open', 0) or 0), 3),
                        'high': round(float(_lr.get('high', 0) or 0), 3),
                        'low': round(float(_lr.get('low', 0) or 0), 3),
                        'close': round(float(_lr.get('close', 0) or 0), 3),
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                    m._inherit_macd_for_preview_bar(_ex)
                    main_snapshot['meta']['kline_count'] = len(_ex)
        # 下窗
        if _sub_klines_for_init is not None and len(_sub_klines_for_init) > 0:
            _lr = _sub_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
            if _dns is not None:
                _bdt = datetime.fromtimestamp(_dns / 1e9)
                _bds = _bdt.strftime(m._get_date_fmt(sub_freq))
                _ex = sub_snapshot.get('klines', [])
                if not _ex or _ex[-1]['date'] != _bds:
                    _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                        'open': round(float(_lr.get('open', 0) or 0), 3),
                        'high': round(float(_lr.get('high', 0) or 0), 3),
                        'low': round(float(_lr.get('low', 0) or 0), 3),
                        'close': round(float(_lr.get('close', 0) or 0), 3),
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                    m._inherit_macd_for_preview_bar(_ex)
                    sub_snapshot['meta']['kline_count'] = len(_ex)
        if m._SSE_DEBUG:
            print(f"[{display_key}] 初始快照提取: {time.time()-t_snap:.3f}s")

        # 8. 推送双窗口 init 事件
        init_data = {
            "main": main_snapshot,
            "sub": sub_snapshot,
        }
        yield _sse_frame("init", init_data)
        if m._SSE_DEBUG:
            print(f"[{display_key}] 推送init")

        # 缓存快照用于 tick 路径
        main_cached_snapshot = main_snapshot
        sub_cached_snapshot = sub_snapshot

        # 9. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照（策略同单窗口）
        BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
        t_total = time.time()  # 总耗时起点（用于日志输出）
        if m._SSE_DEBUG:
            print(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

        # 保存两个窗口的 klines 引用供实时更新使用
        main_klines = await run_in_threadpool(src.get_kline_serial, symbol, main_freq_sec)
        sub_klines = await run_in_threadpool(src.get_kline_serial, symbol, sub_freq_sec)

        # last_bar_dt_ns: klines[-1] 的时间戳，用于检测 klines 是否推进
        # last_processed_dt_ns: 已处理过的K线时间戳，防止同一根K线被重复处理
        main_last_bar_dt_ns = None
        main_last_processed_dt_ns = None
        sub_last_bar_dt_ns = None
        sub_last_processed_dt_ns = None
        last_debug_print = time.time()
        _last_diag_print = time.time()  # 双窗口诊断日志节流时间戳

        # 性能统计
        t_wait_total = 0.0
        t_tick_total = 0.0
        t_step_total = 0.0
        t_snapshot_total = 0.0
        t_push_total = 0.0
        loop_count = 0
        tick_count = 0
        step_count = 0
        last_perf_print = time.time()

        # ---- 定义单窗口K线处理协程（避免 continue 跳过另一个窗口） ----
        async def _process_one_window(klines, chan, kl_type, freq_sec, freq_label,
                                      cached_snapshot, last_bar_dt_ns, last_processed_dt_ns,
                                      is_main, window_label):
            """处理单个窗口的K线检测，返回 (updated, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, need_tick)"""
            nonlocal last_debug_print, last_perf_print, loop_count, tick_count, step_count
            nonlocal t_wait_total, t_tick_total, t_step_total, t_snapshot_total, t_push_total
            if len(klines) == 0:
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            if dt_ns is None:
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

            # 诊断
            if loop_count == 1 or (loop_count % 50 == 0):
                chan_last_klu = None
                try:
                    chan_kl_list = chan[kl_type]
                    if chan_kl_list.lst:
                        last_klc = chan_kl_list.lst[-1]
                        if last_klc.lst:
                            chan_last_klu = last_klc.lst[-1]
                except Exception as e:
                    print(f"[警告] 异常: {type(e).__name__}: {e}")
                tqsdk_last_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime('%H:%M:%S') if dt_ns else "None"
                chan_last_dt = chan_last_klu.time.to_str()[:16] if chan_last_klu and hasattr(chan_last_klu, 'time') else "None"
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DIAG-{window_label}] 循环#{loop_count} | "
                          f"tqsdk klines[-1]={tqsdk_last_dt} | "
                          f"chan kl_list[-1]={chan_last_dt} | "
                          f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

            # 初始化
            if last_bar_dt_ns is None:
                last_bar_dt_ns = dt_ns
                last_debug_print = now_ts
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DEBUG] 初始化 [{window_label}]: "
                          f"klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

            # 每60秒性能统计
            if now_ts - last_perf_print >= 60.0:
                last_perf_print = now_ts
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [PERF] 循环{loop_count}次 | "
                          f"wait_update总计={t_wait_total:.1f}s | "
                          f"tick推送{tick_count}次总计={t_tick_total:.1f}s | "
                          f"step_load{step_count}次总计={t_step_total:.1f}s | "
                          f"快照提取总计={t_snapshot_total:.1f}s | "
                          f"SSE推送总计={t_push_total:.1f}s")
                t_wait_total = 0.0; t_tick_total = 0.0; t_step_total = 0.0
                t_snapshot_total = 0.0; t_push_total = 0.0
                loop_count = 0; tick_count = 0; step_count = 0

            # 每2秒 DEBUG
            if now_ts - last_debug_print >= 2.0:
                last_debug_print = now_ts
                bar_dt = datetime.fromtimestamp(dt_ns / 1e9)
                lag = now_ts - (dt_ns / 1e9 + freq_sec)
                pushed = (dt_ns != last_bar_dt_ns)
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DEBUG-{window_label}] 壁钟={now.strftime('%H:%M:%S')} "
                          f"klines[-1]={bar_dt.strftime('%H:%M:%S')} "
                          f"过期={lag:+.1f}s 推进={pushed} "
                          f"O={last_row.get('open'):.1f} H={last_row.get('high'):.1f} "
                          f"L={last_row.get('low'):.1f} C={last_row.get('close'):.1f}")

            # --- K线完成检测 ---
            klines_pushed = (dt_ns != last_bar_dt_ns)

            if klines_pushed:
                completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row
                last_bar_dt_ns = dt_ns
                bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                if m._SSE_DIAG:
                    print(f"[{display_key}] [DIAG][{window_label}] K线完成(klines推进) "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测={now.strftime('%H:%M:%S.%f')[:-3]} 滞后{now_ts-bar_theoretical_end:+.2f}s "
                          f"klines行数={len(klines)}")
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DIAG] K线完成(klines推进) [{window_label}] "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")
            else:
                bar_end_ts = (dt_ns / 1e9) + freq_sec + BAR_COMPLETION_BUFFER
                if now_ts < bar_end_ts:
                    # 周期未结束 → tick更新（更新快照OHLC，稍后统一推送）
                    if cached_snapshot is not None:
                        try:
                            ex = cached_snapshot.get('klines', [])
                            if ex:
                                o = round(float(last_row.get('open', 0) or 0), 3)
                                h = round(float(last_row.get('high', 0) or 0), 3)
                                l = round(float(last_row.get('low', 0) or 0), 3)
                                c = round(float(last_row.get('close', 0) or 0), 3)
                                ex[-1]['open'] = o; ex[-1]['high'] = h
                                ex[-1]['low'] = l; ex[-1]['close'] = c
                                closes = [k['close'] for k in ex]
                                if len(closes) >= 26:
                                    ema12 = m.ema(closes, 12); ema26 = m.ema(closes, 26)
                                    for i in range(len(ex)):
                                        if i < len(ema12):
                                            ex[i]['dif'] = round(ema12[i] - ema26[i], 4)
                                    difs = [ex[i]['dif'] for i in range(len(ex))]
                                    dea = m.ema(difs, 9)
                                    for i in range(len(ex)):
                                        if i < len(dea):
                                            ex[i]['dea'] = round(dea[i], 4)
                                            ex[i]['macd'] = round(2 * (ex[i]['dif'] - dea[i]), 4)
                                cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                        except Exception as e:
                            print(f"[警告] 异常: {type(e).__name__}: {e}")
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, True
                # 壁钟到期
                completed_row = last_row
                bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                if m._SSE_DIAG:
                    print(f"[{display_key}] [DIAG][{window_label}] K线完成(壁钟) "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测={now.strftime('%H:%M:%S.%f')[:-3]} 滞后{now_ts-bar_theoretical_end:+.2f}s "
                          f"klines行数={len(klines)} dt_ns={dt_ns} last_bar={last_bar_dt_ns}")
                if m._SSE_DEBUG:
                    print(f"[{display_key}] [DIAG] K线完成(壁钟) [{window_label}]: "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")

            completed_dt_ns = completed_row.get("datetime")
            if completed_dt_ns is None:
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
            if completed_dt_ns == last_processed_dt_ns:
                if m._SSE_DIAG:
                    print(f"[{display_key}] [DIAG][{window_label}] 防重拦截: "
                          f"completed={datetime.fromtimestamp(completed_dt_ns/1e9).strftime('%H:%M:%S')} "
                          f"== last_processed={datetime.fromtimestamp(last_processed_dt_ns/1e9).strftime('%H:%M:%S')} "
                          f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]} 快照lastK未更新")
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
            last_processed_dt_ns = completed_dt_ns

            # 提取 OHLC
            o = float(completed_row.get("open", 0) or 0)
            h = float(completed_row.get("high", 0) or 0)
            l = float(completed_row.get("low", 0) or 0)
            cl = float(completed_row.get("close", 0) or 0)
            vol = int(completed_row.get("volume", 0) or 0)
            h = max(h, o, cl); l = min(l, o, cl)

            dt = datetime.fromtimestamp(completed_dt_ns / 1e9)
            bar_expected_end = (completed_dt_ns / 1e9) + freq_sec
            delay = now_ts - bar_expected_end
            source_tag = "klines推进" if klines_pushed else "壁钟"

            code_key = f"{symbol}:{freq_sec}"
            new_bar = {"dt": dt, "open": round(o, 3), "high": round(h, 3),
                       "low": round(l, 3), "close": round(cl, 3),
                       "vol": vol, "amount": 0}  # 天勤K线无成交额，amount置0（前端期货显成交量vol）

            last_records = await run_in_threadpool(src.last_records, code_key)
            updated = False
            t_step = 0.0
            if not last_records or last_records[0]["dt"] != dt:
                await run_in_threadpool(src.append_bar, new_bar, code_key)
                t_step_start = time.time()
                try:
                    await run_in_threadpool(src.step_load, chan)
                except Exception as e:
                    print(f"[{display_key}] {window_label} step_load 异常: {e}")
                t_step = time.time() - t_step_start
                t_step_total += t_step; step_count += 1
                updated = True
                if m._SSE_DIAG:
                    print(f"[{display_key}] [DIAG][{window_label}] step_load耗时={t_step:.3f}s "
                          f"bar={dt.strftime('%Y-%m-%d %H:%M:%S')}")
                if m._SSE_DEBUG:
                    print(f"[{display_key}] 完成新K线[{source_tag}] [{window_label}]: "
                          f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                          f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                          f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

            # 提取完整快照
            if updated:
                snapshot = await run_in_threadpool(
                    src.extract_snapshot, chan, kl_type, symbol, name, freq_label,
                    saved_selection_date)
                if is_main:
                    m._futures_red_range(snapshot, freq_sec, sub_freq_sec, sub_freq)
                _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                _next_ds = _next_dt.strftime(m._get_date_fmt(freq_label))
                _ex = snapshot.get('klines', [])
                if not _ex or _ex[-1]['date'] != _next_ds:
                    _next_c = round(cl, 3)
                    _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                        'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                    m._inherit_macd_for_preview_bar(_ex)
                    snapshot['meta']['kline_count'] = len(_ex)
                if is_main:
                    _kl_list = chan[kl_type]
                    snapshot['white_hline'] = await run_in_threadpool(src.white_hline, _kl_list, main_freq)
                cached_snapshot = snapshot
                if m._SSE_DIAG:
                    _exk = cached_snapshot.get('klines', [])
                    _lastd = _exk[-1]['date'] if _exk else "无"
                    print(f"[{display_key}] [DIAG][{window_label}] updated分支完成: "
                          f"completed={dt.strftime('%H:%M:%S')} 追加预览bar={_next_ds} "
                          f"快照lastK.date={_lastd} kline_count={cached_snapshot.get('meta',{}).get('kline_count')}")

            return updated, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

        # ---- 主循环 ----
        while True:
            try:
                t_wait_start = time.time()
                await run_in_threadpool(src.wait_update, time.time() * 1e9 + 100_000_000)
                t_wait = time.time() - t_wait_start
                t_wait_total += t_wait
            except SSESourceClosed:
                # 数据源正常关闭（仅 Mock/回放源）：等价客户端断开的自然结束
                return
            except Exception as _e:
                print(f"[{display_key}] wait_update 异常: {_e}")
                await asyncio.sleep(0.5)
                continue

            # 心跳注释帧（客户端断开由生成器取消机制触发 finally 清理）
            yield b": heartbeat\n\n"

            loop_count += 1
            now = datetime.now()
            now_ts = now.timestamp()

            # 处理下窗（次级别优先：区间套分析需先分析次级别）
            _t_sub0 = time.time()
            sub_updated, sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns, sub_need_tick = \
                await _process_one_window(sub_klines, sub_chan, sub_kl_type, sub_freq_sec, sub_freq_label,
                                          sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns,
                                          is_main=False, window_label="下窗")
            _t_sub = time.time() - _t_sub0

            # 处理上窗
            _t_main0 = time.time()
            main_updated, main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns, main_need_tick = \
                await _process_one_window(main_klines, main_chan, main_kl_type, main_freq_sec, main_freq_label,
                                          main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns,
                                          is_main=True, window_label="上窗")
            _t_main = time.time() - _t_main0

            if m._SSE_DIAG and now_ts - _last_diag_print >= 2.0:
                _last_diag_print = now_ts
                # 诊断：复现前端"00:00+灰色"判定——快照klines[-1]是否已过期(理论结束时刻<现在)
                def _diag_lastk_lag(snap, label, freq_sec):
                    try:
                        if not snap or not snap.get('klines'):
                            return
                        lastK = snap['klines'][-1]
                        dstr = lastK.get('date', '')
                        if not dstr:
                            return
                        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                            try:
                                kstart = datetime.strptime(dstr, fmt).timestamp()
                                break
                            except ValueError:
                                continue
                        else:
                            return
                        kend = kstart + freq_sec
                        lag = now_ts - kend
                        flag = "<== 已过期(前端将显示00:00+灰色)" if lag > 0 else ""
                        print(f"[{display_key}] [DIAG][{label}] lastK={dstr} 剩余{(-lag):.1f}s "
                              f"过期({lag:+.1f}s) {flag}")
                    except Exception as _e:
                        print(f"[{display_key}] [DIAG][{label}] 解析异常: {_e}")
                print(f"[{display_key}] [DIAG] 单轮耗时: 下窗={_t_sub:.3f}s 上窗={_t_main:.3f}s 总={_t_sub+_t_main:.3f}s")
                _diag_lastk_lag(sub_cached_snapshot, "下窗", sub_freq_sec)
                _diag_lastk_lag(main_cached_snapshot, "上窗", main_freq_sec)
                # 诊断：天勤 klines[-1] 相对 last_bar_dt_ns 是否推进（推进=True 才会触发K线完成updated）
                try:
                    for _kl, _lbd, _lab in ((sub_klines, sub_last_bar_dt_ns, "下窗"),
                                            (main_klines, main_last_bar_dt_ns, "上窗")):
                        if len(_kl) == 0:
                            continue
                        _ldt = _kl.iloc[-1].get("datetime")
                        if _ldt is None:
                            continue
                        _ldt_s = datetime.fromtimestamp(_ldt/1e9).strftime('%H:%M:%S')
                        _lbd_s = datetime.fromtimestamp(_lbd/1e9).strftime('%H:%M:%S') if _lbd else "None"
                        _pushed = (_ldt != _lbd)
                        print(f"[{display_key}] [DIAG][{_lab}] tqsdk klines[-1]={_ldt_s} "
                              f"last_bar_dt_ns={_lbd_s} 推进={_pushed}")
                except Exception as _e:
                    print(f"[{display_key}] [DIAG] klines推进解析异常: {_e}")

            # 推送：tick模式或K线完成模式
            if main_need_tick or sub_need_tick:
                # tick推送：统一发送双窗口数据
                t_tick_start = time.time()
                tick_data = {"main": main_cached_snapshot, "sub": sub_cached_snapshot}
                yield _sse_frame("update", tick_data)
                t_tick_total += time.time() - t_tick_start
                tick_count += 1
                if tick_count == 1:
                    if m._SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] 首次tick推送: "
                              f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

            if main_updated or sub_updated:
                t_snap_start = time.time()
                update_data = {"main": main_cached_snapshot, "sub": sub_cached_snapshot}
                t_push_start = time.time()
                yield _sse_frame("update", update_data)
                t_push_total += time.time() - t_push_start
                t_snapshot_total += time.time() - t_snap_start
                if m._SSE_DEBUG:
                    print(f"[{display_key}] 推送更新: 快照提取={t_snapshot_total:.3f}s "
                          f"JSON序列化={(time.time()-t_snap_start)-t_push_total:.3f}s "
                          f"SSE写入={t_push_total:.3f}s")

    except asyncio.CancelledError:
        # 客户端断开（生成器被取消）：与遗留 BrokenPipe 分支等价，直接收尾
        raise
    except Exception as e:
        # 与遗留实现一致：打印连接异常后静默结束
        print(f"[{display_key}] 连接异常: {e}")
        traceback.print_exc()
    finally:
        await run_in_threadpool(src.close)
        # 清理两个窗口的K线缓存与期货下窗缓存
        if symbol is not None and main_freq_sec is not None:
            await run_in_threadpool(src.cleanup_records, f"{symbol}:{main_freq_sec}")
        if symbol is not None and sub_freq_sec is not None:
            await run_in_threadpool(src.cleanup_records, f"{symbol}:{sub_freq_sec}")
        try:
            orch.futures_pop_sub_chan(symbol, sub_freq)  # 语义化漏斗失效（key 规则内聚数据层）
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# REST 路由（阶段 3a：自 api_server.py 迁入，单一路由源）
# 路由保持薄：参数校验 + run_in_threadpool(orch.*) + 响应组装。
# 引擎调用全部走 AppOrch 漏斗（锁分类见 AppOrch.LOCK_POLICY）。
# ═══════════════════════════════════════════════════════════════════════

router = APIRouter()


# ── 路由 — 核心数据 ───────────────────────────────────────────────────

@router.get("/api/stock")
async def api_stock(
    code: str = Query(...),
    freq: str = Query("d"),
    end_date: str = Query(None),
    step: str = Query(None),
    dual: bool = Query(False),
    sub_freq: str = Query(None),
):
    """获取股票缠论分析数据（orch.call_analysis · SERIAL 持锁）"""
    if not code:
        raise HTTPException(status_code=400, detail="请输入股票代码")
    try:
        result = await run_in_threadpool(orch.call_analysis, code, freq=freq,
                                         end_date=end_date, dual=dual,
                                         step=step, sub_freq=sub_freq)
    except AppError:
        raise  # 领域异常：交给统一异常处理器
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")

    if "error" in result:
        return _json_response(result, 400)

    # 持久化（与原有逻辑一致：非复盘、非双窗口下窗、非期货）
    if not end_date and result.get("meta", {}).get("market") != "futures":
        await run_in_threadpool(orch.save_last_code_freq, code, freq)

    return _json_response(result)


@router.get("/api/stocks_manual_select_point")
async def api_stocks_manual_select_point(
    code: str = Query(...),
    freq: str = Query("d"),
    bi_idx: str = Query("-1"),
):
    """股票手动选点（orch.call_manual_select_point · SERIAL 持锁，阶段 3a 补锁）"""
    if not code or bi_idx == "-1":
        raise HTTPException(status_code=400, detail="缺少必要参数 code 或 bi_idx")
    try:
        result = await run_in_threadpool(orch.call_manual_select_point, code,
                                         freq=freq, bi_idx=bi_idx)
    except AppError:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


@router.get("/api/red_range_zs")
async def api_red_range_zs(
    code: str = Query(...),
    freq: str = Query("d"),
    left_date: str = Query(...),
    right_date: str = Query(...),
    end_date: str = Query(None),
):
    """红框中枢计算（orch.call_compute_red_range_zs · SERIAL 持锁，阶段 3a 补锁）"""
    if not code or not left_date or not right_date:
        raise HTTPException(status_code=400, detail="参数错误: code/left_date/right_date 不能为空")
    try:
        result = await run_in_threadpool(orch.call_compute_red_range_zs, code,
                                         sub_freq=freq, left_date=left_date,
                                         right_date=right_date, end_date=end_date)
    except AppError:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


# ── 路由 — 搜索 ───────────────────────────────────────────────────────

@router.get("/api/search")
async def api_search(q: str = Query(...)):
    """股票代码/名称/拼音搜索（orch.search_stocks，业务段已下沉）"""
    if not q:
        raise HTTPException(status_code=400, detail="请输入搜索关键词")
    result = await run_in_threadpool(orch.search_stocks, q)
    return _json_response(result)


# ── 路由 — 扫描 ───────────────────────────────────────────────────────

@router.get("/api/zxg_list")
async def api_zxg_list():
    """返回自选股列表"""
    try:
        stocks = await run_in_threadpool(orch.read_zxg_stocks)
        return _json_response({"stocks": stocks})
    except Exception as exc:
        return _json_response({"error": str(exc)}, 500)


@router.get("/api/scan_stock_list")
async def api_scan_stock_list(source: str = Query("zxg")):
    """返回股票列表（支持逗号分隔多来源；orch.ScannerService.stock_list）"""
    result = await run_in_threadpool(orch.scanner.stock_list, source)
    return _json_response(result)


@router.get("/api/scan_one")
async def api_scan_one(
    code: str = Query(...),
    freq: str = Query("d"),
    prefix: str = Query(""),
    recent: str = Query("1"),
    source: str = Query("zxg"),
    mode: str = Query(""),
):
    """扫描单只股票（orch.ScannerService.scan_one · SCAN 保留并发）"""
    if not code:
        return _json_response({"error": "缺少code参数"}, 400)
    result = await run_in_threadpool(orch.scanner.scan_one, code, freq, prefix,
                                     recent, source, mode)
    return _json_response(result)


@router.get("/api/scan_page_index_code")
async def api_scan_page_index_code(code: str = Query("")):
    """设置当前板块指数代码"""
    result = await run_in_threadpool(orch.scanner.set_page_index_code, code)
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


@router.get("/api/scan_start")
async def api_scan_start():
    """新一轮扫描开始"""
    result = await run_in_threadpool(orch.scanner.start)
    return _json_response(result)


@router.get("/api/scan_end")
async def api_scan_end():
    """扫描结束"""
    result = await run_in_threadpool(orch.scanner.end)
    return _json_response(result)


@router.get("/api/scan_clear_cache")
async def api_scan_clear_cache():
    """关闭扫描面板"""
    result = await run_in_threadpool(orch.scanner.clear_cache)
    return _json_response(result)


@router.get("/api/scan_abort")
async def api_scan_abort():
    """中断扫描"""
    result = await run_in_threadpool(orch.scanner.abort)
    return _json_response(result)


# ── 路由 — 自选股保存 ─────────────────────────────────────────────────

@router.get("/api/zxg_save")
async def api_zxg_save(codes: str = Query("")):
    """保存勾选的股票到通达信+同花顺自选股（orch.zxg_save，业务段已下沉）"""
    data, status = await run_in_threadpool(orch.zxg_save, codes)
    return _json_response(data, status)


# ── 路由 — 选点管理 ───────────────────────────────────────────────────

@router.get("/api/clear_saved_point")
async def api_clear_saved_point(code: str = Query(...), freq: str = Query("d")):
    """清除选点"""
    if not code:
        return _json_response({"error": "缺少code参数"}, 400)
    result = await run_in_threadpool(orch.clear_saved_point, code, freq)
    return _json_response(result)


# ── 路由 — 期货/期指 ──────────────────────────────────────────────────

@router.get("/api/futures_manual_select_point")
async def api_futures_manual_select_point(
    symbol: str = Query(...),
    freq: str = Query("15s"),
    bi_idx: str = Query("-1"),
):
    """期货手动选点（orch.call_futures_manual_select_point · SERIAL 持锁，阶段 3a 补锁）"""
    if not symbol or bi_idx == "-1":
        return _json_response({"error": "缺少必要参数 symbol 或 bi_idx"}, 400)
    try:
        result = await run_in_threadpool(orch.call_futures_manual_select_point, symbol,
                                         freq=freq, bi_idx=bi_idx)
    except AppError:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


@router.get("/api/futures_clear_saved_point")
async def api_futures_clear_saved_point(symbol: str = Query(...), freq: str = Query("15s")):
    """期货清除选点"""
    if not symbol:
        return _json_response({"error": "缺少symbol参数"}, 400)
    result = await run_in_threadpool(orch.futures_clear_saved_point, symbol, freq)
    return _json_response(result)


@router.get("/api/futures_cleanup")
async def api_futures_cleanup():
    """清理所有期货数据"""
    await run_in_threadpool(orch.futures_cleanup)
    return _json_response({"ok": True})


@router.get("/api/futures_status")
async def api_futures_status():
    """期货状态"""
    return _json_response({"ok": True, "architecture": "self-contained"})


@router.get("/api/futures_config")
async def api_futures_config():
    """期货可用周期列表"""
    result = await run_in_threadpool(orch.futures_config)
    return _json_response(result)


# ── 路由 — SSE 实时推送（期货） ────────────────────────────────────────

@router.get("/api/futures_stream")
async def api_futures_stream(
    symbol: str = Query(...),
    freq: str = Query("15s"),
    start_time: str = Query(None),
    dual: bool = Query(False),
    sub_freq: str = Query(None),
    impl: str = Query("legacy"),
):
    """SSE 实时推送（期货单/双窗口）· 双实现并存（3b-1 灰度开关）

    impl=legacy（默认）：ChartHandler._handle_sse_stream_* + _sse_generator
                         桥接（阶段 3a 前的既有路径，零行为漂移）
    impl=native        ：FrontAPI 原生异步生成器（3b-1 忠实移植，
                         事件协议逐项一致，见 sse_futures_stream_*）
    灰度策略（设计 8.6-3b / 9 章）：高风险档 7 天——两实现并行响应真实
    流量，比对事件序列无差异后翻转默认为 native，再进入 3b-2 拆除旧路径。
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少symbol参数")

    if not orch.tq_available():
        raise HTTPException(status_code=503, detail="天勤数据源不可用")

    if impl not in ("legacy", "native"):
        raise HTTPException(status_code=400, detail="impl 参数仅支持 legacy|native")

    if impl == "native":
        if dual:
            gen = sse_futures_stream_dual(symbol, freq, sub_freq, start_time)
        else:
            gen = sse_futures_stream_single(symbol, freq, start_time)
    else:
        if dual:
            handler = orch.get_sse_handler("dual")
            gen = _sse_generator(handler, symbol, freq, sub_freq, start_time)
        else:
            handler = orch.get_sse_handler("single")
            gen = _sse_generator(handler, symbol, freq, start_time)

    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── 路由 — 股票名称刷新 ───────────────────────────────────────────────

@router.get("/api/refresh_stock_names")
async def api_refresh_stock_names():
    """启动股票名称刷新"""
    result = await run_in_threadpool(orch.refresh_stock_names_async)
    return _json_response(result)


@router.get("/api/refresh_status")
async def api_refresh_status():
    """查询刷新状态"""
    result = await run_in_threadpool(orch.refresh_status)
    return _json_response(result)


# ── 路由 — 标注 ───────────────────────────────────────────────────────

@router.get("/api/annotations")
async def api_annotations_get(code: str = Query(...), freq: str = Query("d")):
    """获取标注数据"""
    if not code:
        return _json_response({"error": "缺少code参数"}, 400)
    anns = await run_in_threadpool(orch.get_annotations, code, freq)
    return _json_response({"annotations": anns, "code": code, "freq": freq})


@router.post("/api/annotations")
async def api_annotations_post(body: dict = Body(...)):
    """标注增删改（orch.handle_annotation_action，40 行校验逻辑已下沉）"""
    data, status = await run_in_threadpool(orch.handle_annotation_action, body)
    return _json_response(data, status)


@router.get("/api/annotations_scan")
async def api_annotations_scan(freq: str = Query("")):
    """自选扫描：返回有标注的股票列表"""
    codes = await run_in_threadpool(orch.get_annotated_codes, freq)
    return _json_response({"codes": codes, "total": len(codes)})


# ── 路由 — 盘后下载 ───────────────────────────────────────────────────

@router.get("/api/tdx_download_start")
async def api_tdx_download_start_get(
    categories: str = Query("[]"),
    day_start: str = Query(""),
    min_start: str = Query(""),
):
    """盘后下载启动 (GET)"""
    data, status = await run_in_threadpool(orch.start_download_checked,
                                           categories, day_start, min_start)
    return _json_response(data, status)


@router.post("/api/tdx_download_start")
async def api_tdx_download_start_post(body: dict = Body(...)):
    """盘后下载启动 (POST)"""
    categories = body.get("categories") or []
    day_start = body.get("day_start") or ""
    min_start = body.get("min_start") or ""
    data, status = await run_in_threadpool(orch.start_download_checked,
                                           categories, day_start, min_start)
    return _json_response(data, status)


@router.get("/api/tdx_download_status")
async def api_tdx_download_status():
    """盘后下载进度"""
    status = await run_in_threadpool(orch.get_download_status)
    return _json_response(status)


@router.get("/api/tdx_download_stop")
async def api_tdx_download_stop():
    """盘后下载停止"""
    ok, msg = await run_in_threadpool(orch.stop_download)
    return _json_response({"ok": ok, "message": msg})


# ── 兼容重定向：旧版页面路径 chan_chart.html → 新首页（自 api_server 迁入）──
from fastapi.responses import RedirectResponse


@router.get("/chan_chart.html", include_in_schema=False)
async def chan_chart_redirect():
    """旧书签兼容：chan_chart.html → /（api_server 遗留路由，行为不变）"""
    return RedirectResponse(url="/")


# ── 路由挂载（单一路由源；api_server 兼容壳 re-export 本 router）──────
app.include_router(router)


# ── 静态资源：Frontend/（与 api_server 兼容入口同规则）────────────────
_frontend_dir = app_config.frontend_dir
if os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
else:
    print(f"[警告] Frontend/ 目录不存在 ({_frontend_dir})，回退到 OUTPUT_DIR 静态挂载")
    app.mount("/", StaticFiles(directory=m.OUTPUT_DIR, html=True), name="static")


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import socket
    import uvicorn

    HOST = app_config.host
    PORT = app_config.port

    # ── 端口占用检测（与 api_server 兼容入口同策略）────────────────────
    try:
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _probe.bind((HOST, PORT))
        _probe.close()
    except OSError:
        print(f"[错误] 端口 {PORT} 已被占用！")
        print(f"[错误] 可能原因：api_server.py 旧入口或其他服务仍在运行。")
        print(f"[解决] Windows 可执行: netstat -ano | findstr {PORT} 查看占用 PID，")
        print(f"[解决] 然后执行: taskkill /PID <PID> /F 结束旧进程。")
        sys.exit(1)

    last_code, last_freq = orch.load_last_code_freq()  # 阶段 4：AppOrch 漏斗读 AppData
    if last_code:
        print(f"[信息] 恢复上次: {last_code} (周期: {last_freq})")
    else:
        print(f"[信息] 使用默认股票: {m.SYMBOL_CODE}")

    print(f"[信息] FastAPI 服务器启动: http://{HOST}:{PORT}")
    print(f"[信息] API 文档:   http://{HOST}:{PORT}/docs")
    print(f"[信息] K线图表页:  http://{HOST}:{PORT}/")
    print(f"[信息] 健康检查:   http://{HOST}:{PORT}/api/health")
    print(f"[信息] SSE 灰度:   /api/futures_stream?impl=legacy|native（默认 legacy，3b-1）")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")

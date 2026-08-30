# -*- coding: utf-8 -*-
"""
App/AppSSE.py —— SSE 实时流功能域
=========================================================================
期货实时行情通过 SSE 长连接持续推送到前端图表（/api/futures/read/stream）。
当前期货是唯一 SSE 实时流消费者；未来若增加股票实时，同样收纳于此
（命名取 SSE 而非 Futures 的原因）。

文件内按 5 区域划分（见各区域分隔头）：SSE 实时流 / 期货分析 / 期货选点 /
市场/代码/周期查询 / 期货退出清理。

依赖方向：AppSSE → AppEngine / App/utils / AppData / DataAPI（单向）；
纯函数/常量与 AppEngine 统一从 App/utils 导入；共享状态（选点/期货缓存）
一律经 app_data.* 公共 API（同一对象，零漂移）。
锁：SSE 路径每连接独立 TqApi + CChan + 记录缓存（session_context 绑定），
CChan 构建免锁。但 SSE 线程与 REST 线程共享 app_data —— 期货下窗 CChan
经 app_data.set_futures_sub_chan 写入，由 AppData._futures_cache_lock 保护。
"""
import json
import time
import traceback
from datetime import datetime

# 引擎侧依赖（仅引擎私有实现：天勤数据源 / 期货名称 / 回看根数折算；
# resolve_lookback_bars 与 CTqSdkAPI 同路线经 AppEngine 显式名，不直连 DataAPI）
from App.AppEngine import (
    TQ_AVAILABLE, CTqSdkAPI, _get_futures_name, resolve_lookback_bars,
)
# P0-1 会话上下文：缓存实例化到连接会话，绑定 CChan 内部 CTqSdkAPI 实例
from DataAPI.TqSdkAPI import session_context, session_set, session_clear
# 领域异常（期货路径使用领域异常，定义于 App/AppErrors.py）
from App.AppErrors import AppError, DataFetchError, AnalysisError
# 引擎纯函数/常量公共工具（与 AppEngine 统一从 App/utils 导入）
from App.utils import (
    _make_chan_config, _get_kl_type, _get_kl_type_by_sec, _get_freq_label, _get_date_fmt,
    ema,
    _calc_zs_confirm_edt_from_bis, _find_left_shoulder_time,
    _FUTURES_DUAL_FREQ_MAP, _SSE_DEBUG, _inherit_macd_for_preview_bar,
)
# 业务数据层（选点/期货子窗缓存；与 AppEngine 同一 app_data 单例）
from App.AppData import app_data
# 区间套辅助（红框/双窗口共用，与 AppEngine 同源）
from BuySellPoint.BSPointList import _futures_red_range, CMyBSPointList, set_replay_mode
# chan.py 核心（与 AppEngine 同源；期货分析链路统一走 SSE init_chan_symbol）
from Chan import CChan
from Common.CEnum import AUTYPE, FX_TYPE
# SSE 数据源抽象（tqsdk 仅在 DataAPI 可见；生成器消费 src.* 协议；
# close_all/CSSESource 一并 re-export，API 层经本模块消费，不直连 DataAPI）
from DataAPI.TqSdkCSSESource import (  # noqa: F401
    CSSESource, CSSESourceClosed, CTqSdkSession, close_all,
)
from App.AppLog import get_logger
log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 区域 1 · SSE 实时流
# ═══════════════════════════════════════════════════════════════════════

# 期货窗口取数的公共参数（对齐股票 A/B/C 语义；见 _futures_window_fetch_bars）
_TQ_MAX_BARS = 10000      # 天勤单序列上限（官方 docstring「每个序列最大支持请求 10000 个数据」）
_BAR_ESTIMATE_MARGIN = 50 # 墙钟估算根数的固定余量（覆盖会话边界的不完整K线/估算误差）


def _parse_flex_time(s):
    """解析多格式时间字符串（%Y/%m/%d %H:%M:%S / %H:%M / %d），失败返回 None。"""
    if not s:
        return None
    for _fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, _fmt)
        except ValueError:
            continue
    return None


def _estimate_bars_between(t_from, t_to, freq_sec):
    """按墙钟估算 [t_from, t_to] 区间内 freq_sec 周期的K线根数上界。

    墙钟含非交易时段（夜盘休市/周末/节假日），结果只会偏大不会偏小——
    用于「至少拉多少根才能覆盖到 t_from」的取数上界（宁多勿少），
    多拉的部分由调用方的时间过滤（start_time/end_time）截掉。
    """
    if t_from is None or t_to is None or freq_sec <= 0:
        return 0
    secs = (t_to - t_from).total_seconds()
    if secs <= 0:
        return 0
    return int(secs // freq_sec) + 1 + _BAR_ESTIMATE_MARGIN


def _futures_window_fetch_bars(freq_sec, start_time=None, end_time=None, base_bars=None):
    """计算一个期货窗口的取数根数（对齐股票 A/B/C 三种操作的左边界语义）。

    A（默认，无 start/end）：取 base_bars（缺省=该周期配置的回看条数）。
    B（start_time=T，选点）：[T, 最新] 全量 → 墙钟估算(T→now)+余量。
        对齐股票「选点后从T加载到最新」：左边界=T，不受配置根数限制。
    C（end_time，复盘）：[end-N, end] → N + 墙钟估算(end→now)。
        对齐股票「从结束时间往前推N根」：fetch 多拉 end→now 流逝的部分，
        拉回后先截到 ≤end 再取末 N 根（截断在 init_chan_symbol 内完成）。
    全部封顶天勤上限 10000（=期货数据源的「全量」；所需根数超限时自然
    降级为「最近 10000 根再过滤」，等价股票「对齐不足降全量」）。

    base_bars: 显式基准根数（双窗下窗传「上窗区间折算根数」实现对齐；None=按配置）。
    返回 (fetch_bars, base_bars_out)：fetch_bars 传给 fetch_kline 的 num_bars；
    base_bars_out 为 C 模式截断到末 N 根的目标根数（A/B 模式不额外截断）。
    """
    n = base_bars if base_bars and base_bars > 0 else resolve_lookback_bars(freq_sec)
    now = datetime.now()
    if end_time:
        _end_dt = _parse_flex_time(end_time)
        if _end_dt is not None:
            fetch = n + _estimate_bars_between(_end_dt, now, freq_sec)
            return (min(fetch, _TQ_MAX_BARS), n)
    elif start_time:
        _start_dt = _parse_flex_time(start_time)
        if _start_dt is not None:
            fetch = _estimate_bars_between(_start_dt, now, freq_sec)
            return (min(fetch, _TQ_MAX_BARS), n)
    return (n, n)


def init_chan_symbol(src, symbol, _name, freq_sec, freq_label, start_time=None, end_time=None, num_bars=None):
    """拉取历史K线 + 运行 chan.py 分析，返回 (chan, klines, kl_type, records) 或 None。
    由 SSE handler 调用，每个 SSE 连接自包含。
    start_time: 选点起始时间，有值时只拉取该时间之后的K线（B 操作：左边界=T，全量到最新）
    end_time: 复盘终点（软断开边界）：有值时只截取该时间之前（含）的K线建 chan，
              且取数为 N+复盘点到当前的流逝根数、建 chan 前截断到末 N 根
              （C 操作：左边界=从 end 往前推 N 根，与股票复盘语义一致），
              update 循环停在此边界，不被实时拉新。None 为实时流。
    num_bars: 显式取数根数（双窗下窗传「上窗区间折算根数」实现对齐；None=按窗口语义计算）。
    首参为数据源对象 src（CTqSdkSession），服务层只消费
    src.fetch_kline / src.get_kline_serial 协议，不触碰 src.api 原始对象。"""
    import time as _time

    display_label = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{display_label}"

    try:
        # 窗口取数根数：A=配置回看 / B=[T,最新]全量 / C=N+流逝根数（见 _futures_window_fetch_bars）
        fetch_bars, base_bars = _futures_window_fetch_bars(
            freq_sec, start_time=start_time, end_time=end_time, base_bars=num_bars)
        records = src.fetch_kline(symbol, freq_sec=freq_sec, display_key=display_key,
                                  start_time=start_time, num_bars=fetch_bars)
        # 复盘终点截断：先滤出 <= end_time 的历史，再取末 N 根（C 操作左边界=从 end 往前推 N 根），
        # 再扣除"最新未完成K线"首根保护
        if end_time:
            records = _truncate_records_by_end(records, end_time, freq_sec)
            if base_bars and len(records) > base_bars:
                records = records[-base_bars:]
        if len(records) > 1:
            now = datetime.now()
            if (now - records[-1]["dt"]).total_seconds() < freq_sec:
                records = records[:-1]

        if len(records) == 0:
            log.info(f"[{display_key}] ⑵ 无有效数据，跳过")
            return None

        t_chan = _time.time()

        # 统一构造：set_data + 周期映射 + 建 CChan + step_load（见 _build_futures_chan）
        # 数据注入经 src.set_data（Session 协议），不落类级缓存
        chan, kl_type = _build_futures_chan(records, symbol, freq_sec, src=src)

        klines = src.get_kline_serial(symbol, freq_sec)

        if _SSE_DEBUG:
            log.info(f"[{display_key}] ⑵ 缠论分析: 消费 {len(records)}根K线, 耗时 {_time.time()-t_chan:.1f}s")
        return (chan, klines, kl_type, records)

    except Exception as e:
        import traceback
        log.info(f"[{display_key}] ⑵ 失败: {e}")
        traceback.print_exc()
        return None


def _drain_chan(chan):
    """驱动 chan 增量计算（耗尽 step_load 生成器）。

    服务层纯业务函数：生成器经本函数消耗引擎，
    数据源不关心缠论计算。
    """
    for _snapshot in chan.step_load():
        pass


# ═══════════════════════════════════════════════════════════════════
# 增量快照：每根 K 线完成不全量 O(n) 重建 klines/MACD，
# 复用缓存快照仅追加新确认K线 + EMA 状态续算，结构元素仍重建。
# ═══════════════════════════════════════════════════════════════════


def _get_saved_point(code, freq):
    """查询单个选点（数据层漏斗；与 AppChart.get_saved_point 语义一致）。"""
    col = app_data.freq_to_col(freq)
    if not col:
        return ""
    return app_data.saved_point_times.get(code, {}).get(col, "").strip()


def _sse_frame(event, payload) -> bytes:
    """构造一帧 SSE 事件（帧格式固定：event 行 + data 行 + 空行，字节级稳定）"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, allow_nan=False)}\n\n".encode("utf-8")


def _truncate_records_by_end(records, end_time, freq_sec, step=None, fmt_list=None):
    """按复盘终点截断 K 线序列（复盘软断开的数据边界）。

    语义：找到 <= end_time
    的最后一个锚点切出前缀；支持箭头步进 step 做偏移。返回截断后的 records；
    不足 5 根返回空列表（由调用方判定）。
    """
    if not records or not end_time:
        return records
    fmt_list = fmt_list or ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]
    target_dt = None
    for fmt in fmt_list:
        try:
            target_dt = datetime.strptime(end_time, fmt)
            break
        except ValueError:
            continue
    if target_dt is None:
        return records
    if step is not None:
        step = int(step)
        if step != 0:
            anchor_idx = None
            for i in range(len(records) - 1, -1, -1):
                if records[i]["dt"] <= target_dt:
                    anchor_idx = i
                    break
            if anchor_idx is not None:
                new_idx = anchor_idx + step
                if 0 <= new_idx < len(records):
                    target_dt = records[new_idx]["dt"]
    recs = [r for r in records if r["dt"] <= target_dt]
    return recs


def sse_futures_stream_single(symbol, freq="15s", start_time=None, end_time=None, source=None):
    """期货 SSE 单窗口 · 同步生成器

    事件协议：
    init（初始快照/失败载荷）→ 实时循环（heartbeat 注释帧 + update 事件：
    tick 路径更新末根 K 线 OHLC/MACD，K线完成路径全量快照）→ 收尾清理。
    source 可注入（默认 CTqSdkSession）；Test/test_sse_gray.py 用 MockSource
    驱动确定性比对。锁：每连接独立会话，共享缓存经 AppData 按资源加锁。
    """
    # P0-5 修复：删除全局日志级别污染（原在此处将 tqsdk 及 root 全部 handler
    # 设 WARNING，永不恢复，导致整个进程 log.info 消失）。tqsdk 抑制已在
    # DataAPI/TqSdkAPI.py 顶层完成（只抑制自身 logger，绝不设 root）。
    log.info("SSE 单窗口连接: symbol=%s freq=%s", symbol, freq)

    src = source if source is not None else CTqSdkSession()
    # P0-1 修复：会话上下文覆盖整个生成器——实时循环每根K线 step_load
    # 会重建 CChan 数据源（CTqSdkAPI 实例），须经线程局部绑定本连接缓存。
    session_set(src)

    display_key = None
    freq_sec = None
    try:
        # 别名解析：支持 PTA→KQ.m@CZCE.TA 等短名称
        symbol_upper = symbol.upper()
        if symbol_upper in CTqSdkAPI.FUTURES_ALIASES:
            symbol = CTqSdkAPI.FUTURES_ALIASES[symbol_upper]

        freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 15)
        freq_label = freq
        freq_cn = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
        display_key = f"{symbol}:{freq_cn}"

        # 如果没有传入 start_time，查询CSV中是否有保存的选点
        # （选点经 app_data 公共 API 读取，不直连引擎内部状态）
        # C 复盘对齐股票语义「复盘不加载选点」：end_time 模式下跳过选点加载，
        # 同时忽略传入的 start_time（复盘窗口固定为 [end-N, end]，与选点无关；
        # 回到最新时前端不带 start_time 重连，此处再从 CSV 恢复选点）。
        if end_time:
            if start_time:
                log.info(f"[{display_key}] 复盘模式：忽略选点 start_time={start_time}（复盘不加载选点）")
            start_time = None
        elif start_time is None:
            col = app_data.freq_to_col(freq) or ""
            if col:
                _saved = _get_saved_point(symbol, freq) or None
                if _saved:
                    start_time = _saved
                    log.info(f"[{display_key}] 检测到保存选点: {start_time}")

        saved_selection_date = start_time or ""

        t_conn = time.time()
        src.connect()
        log.info(f"[{display_key}] ⓪ 连接天勤: 耗时 {time.time()-t_conn:.1f}s")

        t_total = time.time()
        name = _get_futures_name(symbol)  # 品种名称

        # 复盘软断开边界（墙钟比较基准；选点/实时流无 end_time 恒为 None）
        _end_dt = None
        if end_time:
            for _fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                try:
                    _end_dt = datetime.strptime(end_time, _fmt)
                    break
                except ValueError:
                    continue

        # === 1. 拉取历史 + chan 分析 ===
        # 历史拉取/chan 分析在服务层 AppSSE.init_chan_symbol 完成；
        # 首参传数据源对象 src（只消费 src.* 协议，不触碰 src.api 原始对象）
        # 复盘(end_time)时由 init 截断建 chan；复盘调试标志为**线程局部**
        # （set_replay_mode 存原值 + finally 恢复），SSE 每连接独占一条线程，
        # 天然不跨连接污染（原为进程级类变量，并发下会互相串改调试日志）。
        _replay_prev_s = set_replay_mode(bool(end_time))
        try:
            result = init_chan_symbol(src, symbol, name, freq_sec, freq_label, start_time, end_time)
        finally:
            set_replay_mode(_replay_prev_s)
        if result is None:
            yield _sse_frame("init", {"error": "初始化失败（无数据或网络异常）", "symbol": symbol})
            return
        chan, klines, kl_type, records = result

        # === 2. 推送初始快照 ===
        t0 = time.time()
        try:
            init_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                            saved_selection_date=saved_selection_date,
                                            is_replay=bool(end_time))
            # ★ 追加当前形成中的K线（klines[-1]），让前端立即看到新K线
            if klines is not None and len(klines) > 0:
                _lr = klines.iloc[-1]; _dns = _lr.get('datetime')
                if _dns is not None:
                    _bdt = datetime.fromtimestamp(_dns / 1e9)
                    _bds = _bdt.strftime(_get_date_fmt(freq))
                    _ex = init_data.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _bds:
                        _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                            'open': round(float(_lr.get('open', 0) or 0), 3),
                            'high': round(float(_lr.get('high', 0) or 0), 3),
                            'low': round(float(_lr.get('low', 0) or 0), 3),
                            'close': round(float(_lr.get('close', 0) or 0), 3),
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        _inherit_macd_for_preview_bar(_ex)
                        init_data['meta']['kline_count'] = len(_ex)
            # 计算白色横虚线（初始快照，K线已确认状态）
            _kl_list = chan[kl_type]
            init_data['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _get_date_fmt(freq))
            yield _sse_frame("init", init_data)
            cached_snapshot = init_data  # ★ 缓存完整快照，tick推送时更新最后一根K线OHLC
            if _SSE_DEBUG:
                log.info(f"[{display_key}] ⑶ 推送init: "
                      f"K线{init_data['meta']['kline_count']}, "
                      f"笔{init_data['meta']['bi_count']}, "
                      f"中枢{init_data['meta']['zs_count']}, "
                      f"耗时 {time.time()-t0:.1f}s")
        except Exception as _e:
            yield _sse_frame("init", {"error": f"快照提取失败: {_e}", "symbol": symbol})
            return

        # === 3. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照 ===
        # 策略：壁钟（datetime.now()）判断K线周期结束，不等天勤
        # klines 推进信号；周期结束后 klines[-1] OHLC 已冻结可直接入缠论。
        BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
        if _SSE_DEBUG:
            log.info(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

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
                src.wait_update(time.time() * 1e9 + 100_000_000)
                t_wait = time.time() - t_wait_start
                t_wait_total += t_wait
            except CSSESourceClosed:
                # 数据源正常关闭（仅 Mock/回放源）：等价客户端断开的自然结束
                return
            except Exception as _e:
                log.info(f"[{display_key}] wait_update 异常: {_e}")
                time.sleep(0.5)
                continue

            # 心跳注释帧（客户端断开由生成器线程退出触发 finally 清理）
            yield b": heartbeat\n\n"

            loop_count += 1

            now = datetime.now()
            now_ts = now.timestamp()

            # 复盘软断开：end_time 模式下，一但墙钟越过复盘边界所属周期，
            # 即停止推进 K 线/chan/快照（update 不把图"拉最新"），只保活连接。
            if end_time and _end_dt is not None and now > _end_dt:
                continue

            if len(klines) == 0:
                continue

            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            if dt_ns is None:
                continue

            # 初始化
            if last_bar_dt_ns is None:
                last_bar_dt_ns = dt_ns
                last_debug_print = now_ts
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DEBUG] 初始化: klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                continue

            # 每60秒打印一次性能统计
            if now_ts - last_perf_print >= 60.0:
                last_perf_print = now_ts
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [PERF] 循环{loop_count}次 | "
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
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DEBUG] 壁钟={now.strftime('%H:%M:%S')} "
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
                                    ema12 = ema(closes, 12)
                                    ema26 = ema(closes, 26)
                                    for i in range(len(ex)):
                                        if i < len(ema12):
                                            ex[i]['dif'] = round(ema12[i] - ema26[i], 4)
                                    difs = [ex[i]['dif'] for i in range(len(ex))]
                                    dea = ema(difs, 9)
                                    for i in range(len(ex)):
                                        if i < len(dea):
                                            ex[i]['dea'] = round(dea[i], 4)
                                            ex[i]['macd'] = round(2 * (ex[i]['dif'] - ex[i]['dea']), 4)
                                cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                                yield _sse_frame("update", cached_snapshot)
                                t_tick_total += time.time() - t_tick_start
                                tick_count += 1
                    except Exception as _e:
                        log.warning(f"[警告] 异常: {type(_e).__name__}: {_e}")
                    continue
                # 壁钟到期，当前K线（klines[-1]）已冻结
                completed_row = last_row

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

            last_records = src.last_records(code_key)
            t_step = 0.0
            if not last_records or last_records[0]["dt"] != dt:
                src.append_bar(new_bar, code_key)
                t_step_start = time.time()
                try:
                    _drain_chan(chan)
                except Exception as _e:
                    log.info(f"[{display_key}] step_load 异常: {_e}")
                t_step = time.time() - t_step_start
                t_step_total += t_step
                step_count += 1

            if _SSE_DEBUG:
                log.info(f"[{display_key}] 完成新K线[{source_tag}]: "
                      f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                      f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                      f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                      f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

            # 推送快照（此时 klines[-1] 已推进到 N 周期，快照中自然包含 N 的实时OHLC）
            t_snap_start = time.time()
            try:
                # 增量快照：复用缓存 klines + EMA 状态续算 MACD，避免每根K线全量 O(n) 重建
                update_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                saved_selection_date=saved_selection_date,
                                                prev_klines=(cached_snapshot or {}).get("klines"),
                                                prev_ema_state=(cached_snapshot or {}).get("meta", {}).get("_ema_state"),
                                                is_replay=bool(end_time))
                # ★ 用 completed_time + freq_sec 计算下一根K线时间（不用klines[-1]，因为壁钟触发时klines未推进）
                _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                _next_ds = _next_dt.strftime(_get_date_fmt(freq_label))
                _ex = update_data.get('klines', [])
                if not _ex or _ex[-1]['date'] != _next_ds:
                    _next_c = round(cl, 3)
                    _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                        'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                    _inherit_macd_for_preview_bar(_ex)
                    update_data['meta']['kline_count'] = len(_ex)
                # K线确认后，计算白色横虚线（不在tick推送路径计算）
                _kl_list = chan[kl_type]
                update_data['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _get_date_fmt(freq))
                cached_snapshot = update_data  # ★ 更新缓存
                t_snap = time.time() - t_snap_start
                t_snapshot_total += t_snap
                t_push_start = time.time()
                yield _sse_frame("update", update_data)
                t_push = time.time() - t_push_start
                t_push_total += t_push
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] 推送更新: 快照提取={t_snap:.3f}s "
                          f"SSE写入={t_push:.3f}s "
                          f"(append+step_load={time.time()-t_append:.3f}s)")
            except Exception as _e:
                log.info(f"[{display_key}] 推送异常: {_e}")

    except Exception as e:
        # 打印连接异常后静默结束（错误已在 init 事件载荷中表达）
        log.info(f"[{display_key}] 连接异常: {e}")
    finally:
        # 生成器线程收尾：close() 设置关闭旗（幂等），close_api() 由生成器
        # 线程调用 api.close()——wait_update 已返回、_loop 已停止，串行安全。
        # 顺序：先 close()（通知外部回收已生效）再 close_api()（真正关连接）。
        src.close()
        src.close_api()
        # 清理该连接的K线缓存
        if symbol is not None and freq_sec is not None:
            src.cleanup_records(f"{symbol}:{freq_sec}")
        # P0-1 修复：清除线程局部会话（线程池复用前必须清，防串连其它连接缓存）
        session_clear()


def sse_futures_stream_dual(symbol, main_freq="1m", sub_freq=None, start_time=None, end_time=None, source=None):
    """期货 SSE 双窗口 · 同步生成器

    事件协议：
    两个独立 CChan 对象、一次连接推送两个周期（下窗先处理——区间套分析
    需先分析次级别）。source 可注入（默认 CTqSdkSession）。
    end_time: 复盘终点（软断开边界），有值时双窗建 chan 均截断到该边界，
              update 循环停在后不推进，也不被实时拉新。
    锁：每连接独立会话；共享缓存经 AppData 按资源加锁
    （见 AppOrch.SHARED_RESOURCE_REGISTRY）。
    """
    log.info("SSE 双窗口连接: symbol=%s main=%s sub=%s",
             symbol, main_freq, sub_freq)

    src = source if source is not None else CTqSdkSession()
    # P0-1 修复：会话上下文覆盖整个双窗生成器（上下窗共用同一 src，
    # 实时循环两窗 step_load 重建数据源均须绑定本连接缓存）。
    session_set(src)

    from datetime import datetime

    # 确定周期
    if not sub_freq:
        sub_freq = _FUTURES_DUAL_FREQ_MAP.get(main_freq, "15s")
    main_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(main_freq, 60)
    sub_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(sub_freq, 15)

    display_key = f"{symbol} 双窗口({main_freq}/{sub_freq})"
    if _SSE_DEBUG:
        log.info(f"\n[{display_key}] ═══ SSE双窗口连接建立 ═══")

    try:
        # 别名解析
        symbol_upper = symbol.upper()
        if symbol_upper in CTqSdkAPI.FUTURES_ALIASES:
            symbol = CTqSdkAPI.FUTURES_ALIASES[symbol_upper]

        src.connect()
        name = _get_futures_name(symbol)
        main_freq_label = main_freq
        sub_freq_label = sub_freq

        # 复盘软断开边界（墙钟比较基准；选点/实时流无 end_time 恒为 None）
        _end_dt = None
        if end_time:
            for _fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                try:
                    _end_dt = datetime.strptime(end_time, _fmt)
                    break
                except ValueError:
                    continue

        # 1. 查询选点状态（选点经 app_data 公共 API 读取）
        # 对齐股票双窗语义（下窗对齐上窗 + C 复盘规则）：
        #   · 双窗只认上窗选点：下窗不读自己周期的选点，跟随上窗区间对齐取数；
        #   · C 复盘不加载选点（对齐股票）：end_time 模式下忽略 start_time 与 CSV 选点，
        #     复盘窗口固定 [end-N, end]；回到最新时不带 start_time 重连，此处再恢复。
        saved_selection_date = ""
        main_start_time = start_time
        if end_time:
            if main_start_time:
                log.info(f"[{display_key}] 复盘模式：忽略选点 start_time={main_start_time}（复盘不加载选点）")
            main_start_time = None
        else:
            try:
                qualified_code = symbol
                col_meta = app_data.freq_to_col(main_freq) or ""
                if col_meta:
                    saved_selection_date = _get_saved_point(qualified_code, main_freq)
                    # 如果外部没传start_time，从CSV读取选点
                    if main_start_time is None and saved_selection_date:
                        main_start_time = saved_selection_date
                        log.info(f"[{display_key}] 检测到保存选点: {saved_selection_date}")
            except Exception as _e:
                log.warning(f"[警告] 异常: {type(_e).__name__}: {_e}")

        # 下窗取数根数：对齐上窗实际加载区间（股票双窗「下窗对齐上窗」的期货对齐实现）
        #   · A（无选点/非复盘）：上窗配置 N_main 根 → 下窗 = N_main×(上窗周期/下窗周期)+余量
        #     （K线根数按交易时长等比折算：N_main 根上窗K线的交易时长 = N_main×主周期 秒，
        #       对应下窗 N_main×(主周期/次周期) 根；余量覆盖会话边界的不完整K线）
        #   · B（上窗选点 T）：跟随上窗 [T, 最新]，按墙钟估算下窗根数（init_chan_symbol 内处理）
        #   · C（复盘）：跟随上窗 [end-N_main, end]，下窗折算根数 + end→now 流逝根数
        #   · 全部封顶天勤上限 10000；超限自然降级「对齐不足 → 全量（最近10000根）」
        _main_base_bars = resolve_lookback_bars(main_freq_sec)
        _sub_align_ratio = main_freq_sec / sub_freq_sec if sub_freq_sec > 0 else 1.0
        _sub_aligned_bars = min(int(_main_base_bars * _sub_align_ratio) + _BAR_ESTIMATE_MARGIN, _TQ_MAX_BARS)
        if main_start_time:
            # B 模式：下窗跟随上窗选点 T，按墙钟估算（fetch_kline 内再按 T 过滤）
            sub_start_time = main_start_time
            sub_num_bars = None
        else:
            # A/C 模式：下窗按上窗区间折算根数对齐（C 的流逝根数由 init_chan_symbol 补充）
            sub_start_time = None
            sub_num_bars = _sub_aligned_bars
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 下窗对齐: 上窗基准={_main_base_bars}根, 折算比={_sub_align_ratio:.1f}, "
                     f"下窗对齐={_sub_aligned_bars}根 (start={sub_start_time}, replay={bool(end_time)})")

        # 2. 拉取下窗历史 + chan分析（次级别优先：区间套分析需先分析次级别）
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 拉取下窗({sub_freq})历史K线...")
        # 复盘软断开：end_time 模式下建 chan 前启用买卖点调试，建后复原
        _replay_prev = set_replay_mode(bool(end_time))
        try:
            sub_result = init_chan_symbol(src, symbol, name, sub_freq_sec, sub_freq_label, sub_start_time, end_time,
                                          num_bars=sub_num_bars)
        finally:
            set_replay_mode(_replay_prev)
        if sub_result is None:
            yield _sse_frame("init", {"error": "下窗初始化失败（无数据或网络异常）", "symbol": symbol})
            return
        # init_chan_symbol 返回 (chan, klines, kl_type, records)——第二项是
        # get_kline_serial 的 klines DataFrame 而非 records（本分支未使用）
        sub_chan, _sub_klines, sub_kl_type, _ = sub_result
        sub_kl_type = _get_kl_type(sub_freq)
        # 缓存下窗 CChan 供 /api/dual_zs 访问（语义化漏斗：key 规则内聚数据层）
        app_data.set_futures_sub_chan(symbol, sub_freq, sub_chan)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 下窗({sub_freq}) chan.py: 合并K线={len(sub_chan[sub_kl_type].lst)}, "
                  f"笔={len(sub_chan[sub_kl_type].bi_list)}, 中枢={len(sub_chan[sub_kl_type].zs_list)}")

        # 3. 拉取上窗历史 + chan分析
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 拉取上窗({main_freq})历史K线...")
        _replay_prev2 = set_replay_mode(bool(end_time))
        try:
            main_result = init_chan_symbol(src, symbol, name, main_freq_sec, main_freq_label, main_start_time, end_time)
        finally:
            set_replay_mode(_replay_prev2)
        if main_result is None:
            yield _sse_frame("init", {"error": "上窗初始化失败（无数据或网络异常）", "symbol": symbol})
            return
        # init_chan_symbol 返回 (chan, klines, kl_type, records)——第二项是
        # get_kline_serial 的 klines DataFrame 而非 records（本分支未使用）
        main_chan, _main_klines, main_kl_type, _ = main_result
        main_kl_type = _get_kl_type(main_freq)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 上窗({main_freq}) chan.py: 合并K线={len(main_chan[main_kl_type].lst)}, "
                  f"笔={len(main_chan[main_kl_type].bi_list)}, 中枢={len(main_chan[main_kl_type].zs_list)}")

        # 7. 提取初始快照
        t_snap = time.time()
        main_snapshot = _extract_realtime_snapshot(main_chan, main_kl_type, symbol, name, main_freq_label,
                                                 saved_selection_date=saved_selection_date,
                                                 is_replay=bool(end_time))
        sub_snapshot = _extract_realtime_snapshot(sub_chan, sub_kl_type, symbol, name, sub_freq_label,
                                                       klines=None,
                                                       is_replay=bool(end_time))
        # 期货双窗口：上窗 bis 的 fx_a_raw_dt/fx_b_raw_dt 是上层K线时间，
        # 需要换算成子级别K线时间，前端 calcRedRange 才能正确匹配
        _futures_red_range(main_snapshot, main_freq_sec, sub_freq_sec, sub_freq)

        # ★ 追加上下窗当前形成中的K线（与单窗口一致），让前端立即看到，且 tick 更新正确的 K 线
        _main_klines_for_init = src.get_kline_serial(symbol, main_freq_sec)
        _sub_klines_for_init = src.get_kline_serial(symbol, sub_freq_sec)
        # 上窗
        if _main_klines_for_init is not None and len(_main_klines_for_init) > 0:
            _lr = _main_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
            if _dns is not None:
                _bdt = datetime.fromtimestamp(_dns / 1e9)
                _bds = _bdt.strftime(_get_date_fmt(main_freq))
                _ex = main_snapshot.get('klines', [])
                if not _ex or _ex[-1]['date'] != _bds:
                    _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                        'open': round(float(_lr.get('open', 0) or 0), 3),
                        'high': round(float(_lr.get('high', 0) or 0), 3),
                        'low': round(float(_lr.get('low', 0) or 0), 3),
                        'close': round(float(_lr.get('close', 0) or 0), 3),
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                    _inherit_macd_for_preview_bar(_ex)
                    main_snapshot['meta']['kline_count'] = len(_ex)
        # 下窗
        if _sub_klines_for_init is not None and len(_sub_klines_for_init) > 0:
            _lr = _sub_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
            if _dns is not None:
                _bdt = datetime.fromtimestamp(_dns / 1e9)
                _bds = _bdt.strftime(_get_date_fmt(sub_freq))
                _ex = sub_snapshot.get('klines', [])
                if not _ex or _ex[-1]['date'] != _bds:
                    _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                        'open': round(float(_lr.get('open', 0) or 0), 3),
                        'high': round(float(_lr.get('high', 0) or 0), 3),
                        'low': round(float(_lr.get('low', 0) or 0), 3),
                        'close': round(float(_lr.get('close', 0) or 0), 3),
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                    _inherit_macd_for_preview_bar(_ex)
                    sub_snapshot['meta']['kline_count'] = len(_ex)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 初始快照提取: {time.time()-t_snap:.3f}s")

        # 8. 推送双窗口 init 事件
        init_data = {
            "main": main_snapshot,
            "sub": sub_snapshot,
        }
        yield _sse_frame("init", init_data)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 推送init")

        # 缓存快照用于 tick 路径
        main_cached_snapshot = main_snapshot
        sub_cached_snapshot = sub_snapshot

        # 9. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照（策略同单窗口）
        BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
        t_total = time.time()  # 总耗时起点（用于日志输出）
        if _SSE_DEBUG:
            log.info(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

        # 保存两个窗口的 klines 引用供实时更新使用
        main_klines = src.get_kline_serial(symbol, main_freq_sec)
        sub_klines = src.get_kline_serial(symbol, sub_freq_sec)

        # last_bar_dt_ns: klines[-1] 的时间戳，用于检测 klines 是否推进
        # last_processed_dt_ns: 已处理过的K线时间戳，防止同一根K线被重复处理
        main_last_bar_dt_ns = None
        main_last_processed_dt_ns = None
        sub_last_bar_dt_ns = None
        sub_last_processed_dt_ns = None
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

        # ---- 定义单窗口K线处理函数（避免 continue 跳过另一个窗口） ----
        def _process_one_window(klines, chan, kl_type, freq_sec, freq_label,
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
                    log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
                tqsdk_last_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime('%H:%M:%S') if dt_ns else "None"
                chan_last_dt = chan_last_klu.time.to_str()[:16] if chan_last_klu and hasattr(chan_last_klu, 'time') else "None"
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DIAG-{window_label}] 循环#{loop_count} | "
                          f"tqsdk klines[-1]={tqsdk_last_dt} | "
                          f"chan kl_list[-1]={chan_last_dt} | "
                          f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

            # 初始化
            if last_bar_dt_ns is None:
                last_bar_dt_ns = dt_ns
                last_debug_print = now_ts
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DEBUG] 初始化 [{window_label}]: "
                          f"klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

            # 每60秒性能统计
            if now_ts - last_perf_print >= 60.0:
                last_perf_print = now_ts
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [PERF] 循环{loop_count}次 | "
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
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] [DEBUG-{window_label}] 壁钟={now.strftime('%H:%M:%S')} "
                          f"klines[-1]={bar_dt.strftime('%H:%M:%S')} "
                          f"过期={lag:+.1f}s 推进={pushed} "
                          f"O={last_row.get('open'):.1f} H={last_row.get('high'):.1f} "
                          f"L={last_row.get('low'):.1f} C={last_row.get('close'):.1f}")

            # --- K线完成检测 ---
            klines_pushed = (dt_ns != last_bar_dt_ns)

            if klines_pushed:
                completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row
                last_bar_dt_ns = dt_ns
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
                                    ema12 = ema(closes, 12); ema26 = ema(closes, 26)
                                    for i in range(len(ex)):
                                        if i < len(ema12):
                                            ex[i]['dif'] = round(ema12[i] - ema26[i], 4)
                                    difs = [ex[i]['dif'] for i in range(len(ex))]
                                    dea = ema(difs, 9)
                                    for i in range(len(ex)):
                                        if i < len(dea):
                                            ex[i]['dea'] = round(dea[i], 4)
                                            ex[i]['macd'] = round(2 * (ex[i]['dif'] - dea[i]), 4)
                                cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                        except Exception as e:
                            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, True
                # 壁钟到期
                completed_row = last_row

            completed_dt_ns = completed_row.get("datetime")
            if completed_dt_ns is None:
                return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
            if completed_dt_ns == last_processed_dt_ns:
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

            last_records = src.last_records(code_key)
            updated = False
            t_step = 0.0
            if not last_records or last_records[0]["dt"] != dt:
                src.append_bar(new_bar, code_key)
                t_step_start = time.time()
                try:
                    _drain_chan(chan)
                except Exception as e:
                    log.info(f"[{display_key}] {window_label} step_load 异常: {e}")
                t_step = time.time() - t_step_start
                t_step_total += t_step; step_count += 1
                updated = True
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] 完成新K线[{source_tag}] [{window_label}]: "
                          f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                          f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                          f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

            # 提取完整快照（增量：复用缓存 klines + EMA 状态续算 MACD）
            if updated:
                snapshot = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                           saved_selection_date=saved_selection_date,
                                                           prev_klines=(cached_snapshot or {}).get("klines"),
                                                           prev_ema_state=(cached_snapshot or {}).get("meta", {}).get("_ema_state"),
                                                           is_replay=bool(end_time))
                if is_main:
                    _futures_red_range(snapshot, freq_sec, sub_freq_sec, sub_freq)
                _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                _next_ds = _next_dt.strftime(_get_date_fmt(freq_label))
                _ex = snapshot.get('klines', [])
                if not _ex or _ex[-1]['date'] != _next_ds:
                    _next_c = round(cl, 3)
                    _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                        'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                        'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                    _inherit_macd_for_preview_bar(_ex)
                    snapshot['meta']['kline_count'] = len(_ex)
                if is_main:
                    _kl_list = chan[kl_type]
                    snapshot['white_hline'] = _calc_futures_white_hline(_kl_list, main_freq, _get_date_fmt(main_freq))
                cached_snapshot = snapshot

            return updated, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

        # ---- 主循环 ----
        while True:
            try:
                t_wait_start = time.time()
                src.wait_update(time.time() * 1e9 + 100_000_000)
                t_wait = time.time() - t_wait_start
                t_wait_total += t_wait
            except CSSESourceClosed:
                # 数据源正常关闭（仅 Mock/回放源）：等价客户端断开的自然结束
                return
            except Exception as _e:
                log.info(f"[{display_key}] wait_update 异常: {_e}")
                time.sleep(0.5)
                continue

            # 心跳注释帧（客户端断开由生成器线程退出触发 finally 清理）
            yield b": heartbeat\n\n"

            loop_count += 1
            now = datetime.now()
            now_ts = now.timestamp()

            # 复盘软断开：end_time 模式下，一但墙钟越过复盘边界，
            # 即停止推进双窗 K 线/chan/快照（不把图"拉最新"），只保活连接。
            if end_time and _end_dt is not None and now > _end_dt:
                continue

            # 处理下窗（次级别优先：区间套分析需先分析次级别）
            sub_updated, sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns, sub_need_tick = \
                _process_one_window(sub_klines, sub_chan, sub_kl_type, sub_freq_sec, sub_freq_label,
                                          sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns,
                                          is_main=False, window_label="下窗")

            # 处理上窗
            main_updated, main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns, main_need_tick = \
                _process_one_window(main_klines, main_chan, main_kl_type, main_freq_sec, main_freq_label,
                                          main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns,
                                          is_main=True, window_label="上窗")

            # 推送：tick模式或K线完成模式
            if main_need_tick or sub_need_tick:
                # tick推送：统一发送双窗口数据
                t_tick_start = time.time()
                tick_data = {"main": main_cached_snapshot, "sub": sub_cached_snapshot}
                yield _sse_frame("update", tick_data)
                t_tick_total += time.time() - t_tick_start
                tick_count += 1

            if main_updated or sub_updated:
                t_snap_start = time.time()
                update_data = {"main": main_cached_snapshot, "sub": sub_cached_snapshot}
                t_push_start = time.time()
                yield _sse_frame("update", update_data)
                t_push_total += time.time() - t_push_start
                t_snapshot_total += time.time() - t_snap_start
                if _SSE_DEBUG:
                    log.info(f"[{display_key}] 推送更新: 快照提取={t_snapshot_total:.3f}s "
                          f"JSON序列化={(time.time()-t_snap_start)-t_push_total:.3f}s "
                          f"SSE写入={t_push_total:.3f}s")

    except Exception as e:
        # 打印连接异常后静默结束（错误已在 init 事件载荷中表达）
        log.info(f"[{display_key}] 连接异常: {e}")
        traceback.print_exc()
    finally:
        # 生成器线程收尾：close() 设置关闭旗（幂等），close_api() 由生成器
        # 线程调用 api.close()——wait_update 已返回、_loop 已停止，串行安全。
        src.close()
        src.close_api()
        # 清理两个窗口的K线缓存与期货下窗缓存
        if symbol is not None and main_freq_sec is not None:
            src.cleanup_records(f"{symbol}:{main_freq_sec}")
        if symbol is not None and sub_freq_sec is not None:
            src.cleanup_records(f"{symbol}:{sub_freq_sec}")
        try:
            app_data.pop_futures_sub_chan(symbol, sub_freq)  # 语义化漏斗失效（key 规则内聚数据层）
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
        # P0-1 修复：清除线程局部会话（线程池复用前必须清，防串连其它连接缓存）
        session_clear()


# ═══════════════════════════════════════════════════════════════════════
# 区域 2 · 期货分析
# ═══════════════════════════════════════════════════════════════════════

def _build_futures_chan(records, symbol, freq_sec, config=None, code=None, src=None):
    """统一期货 CChan 构造：注入数据源 → 周期映射 → 建链 → 全量消费。

    期货各链路唯一的「数据 → CChan」构造入口。单一定义：
      - 周期映射：_get_kl_type_by_sec(freq_sec)
      - 配置：_make_chan_config()
    数据注入经 src.set_data（Session 协议）完成；
    src 为必填（P0-1 修复：缓存实例化到连接会话，不再回退类级缓存）。
    返回 (chan, kl_type)。缓存逻辑留在各调用方，本函数只负责「数据 → CChan」。
    """
    if src is None:
        raise RuntimeError(
            "[TqSdkAPI] _build_futures_chan 需要数据源 src（SSE 每连接自包含）")
    chan_code = code or f"{symbol}:{freq_sec}"
    src.set_data(records, symbol=chan_code)
    kl_type = _get_kl_type_by_sec(freq_sec)
    config = config or _make_chan_config()
    # P0-1 修复：CChan 内部自行实例化 CTqSdkAPI（data_src="custom:..."），
    # 经线程局部绑定本会话缓存，使 set_data 写入与 get_kl_data 读取同源。
    with session_context(src):
        chan = CChan(
            code=chan_code, begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan.step_load():
            pass
    return chan, kl_type


# ═══════════════════════════════════════════════════════════════════════
# 期货手动选点
# ═══════════════════════════════════════════════════════════════════════


def _extract_chan_structure(kl_list, chan, date_fmt):
    """统一缠论结构元素提取：bis / fxs / segs / zs / zs_stars / bsps。

    SSE 实时快照（_extract_realtime_snapshot）与期货分析
    共用同一提取逻辑，保证两条路径字段一致。肩部原始K线时间统一走
    _main_bi_range（与股票路径同源）。

    返回 (bis, fxs, segs, zs_list, zs_stars, bsps)。
    """
    bis = []
    for bi in kl_list.bi_list:
        try:
            direction = "up" if bi.is_up() else "down"
            begin_klu = bi.get_begin_klu()
            end_klu = bi.get_end_klu()
            begin_fx_idx = None
            if hasattr(bi, 'begin_klc') and bi.begin_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.begin_klc:
                        begin_fx_idx = idx
                        break
            end_fx_idx = None
            if hasattr(bi, 'end_klc') and bi.end_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.end_klc:
                        end_fx_idx = idx
                        break
            # 左肩/右肩原始K线时间（双窗口红框定位；与 _main_bi_range 同源，
            # 但独立计算保留单侧结果——_main_bi_range 任一侧为空即整体返回
            # None，会丢失另一侧已有的肩部时间，期货分析路径依赖此行为）
            fx_a_raw_dt = ""
            fx_b_raw_dt = ""
            try:
                begin_klc = getattr(bi, 'begin_klc', None)
                end_klc = getattr(bi, 'end_klc', None)
                left_shoulder_klc = begin_klc.pre if begin_klc else None
                if left_shoulder_klc and left_shoulder_klc.lst:
                    fx_a_raw_dt = left_shoulder_klc.lst[0].time.toFmtStr(date_fmt)
                right_shoulder_klc = end_klc.next if end_klc else None
                if right_shoulder_klc and right_shoulder_klc.lst:
                    fx_b_raw_dt = right_shoulder_klc.lst[-1].time.toFmtStr(date_fmt)
            except Exception as _e:
                log.warning(f"[警告] 异常: {type(_e).__name__}: {_e}")
            bis.append({
                "idx": getattr(bi, "idx", None),
                "sdt": begin_klu.time.toFmtStr(date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(date_fmt) if end_klu else "",
                "sdt_ts": int(begin_klu.time.ts * 1000) if begin_klu else 0,
                "edt_ts": int(end_klu.time.ts * 1000) if end_klu else 0,
                "direction": direction,
                "fx_a_price": round(bi.get_begin_val(), 2),
                "fx_b_price": round(bi.get_end_val(), 2),
                "high": round(bi._high(), 2),
                "low": round(bi._low(), 2),
                "power": round(abs(bi.get_end_val() - bi.get_begin_val()), 2),
                "is_sure": getattr(bi, 'is_sure', True),
                "end_fx_idx": end_fx_idx,
                "begin_fx_idx": begin_fx_idx,
                "fx_a_raw_dt": fx_a_raw_dt,
                "fx_b_raw_dt": fx_b_raw_dt,
                "fx_a_sub_dt": "",
                "fx_b_sub_dt": "",
            })
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")

    fxs = []
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            peak_klu = klc.get_high_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(date_fmt) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu else 0,
                "mark": "G", "price": klc.high, "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            peak_klu = klc.get_low_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(date_fmt) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu else 0,
                "mark": "D", "price": klc.low, "high": klc.high, "low": klc.low,
            })

    segs = []
    for seg in kl_list.seg_list:
        try:
            direction = "up" if seg.is_up() else "down"
            begin_klu = seg.get_begin_klu()
            end_klu = seg.get_end_klu()
            if direction == "up":
                begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
                end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
            else:
                begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
                end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
            segs.append({
                "sdt": begin_klu.time.toFmtStr(date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(date_fmt) if end_klu else "",
                "direction": direction,
                "begin_price": begin_price, "end_price": end_price,
                "high": round(seg._high(), 2), "low": round(seg._low(), 2),
                "amp": round(seg.amp(), 2),
            })
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_list = []
    for zs in kl_list.zs_list:
        try:
            zs_list.append({
                "sdt": zs.begin.time.toFmtStr(date_fmt) if zs.begin and hasattr(zs.begin, 'time') else "",
                "edt": zs.end.time.toFmtStr(date_fmt) if zs.end and hasattr(zs.end, 'time') else "",
                "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, date_fmt),
                "zg": round(zs.high, 2), "zd": round(zs.low, 2),
                "gg": round(zs.peak_high, 2), "dd": round(zs.peak_low, 2),
                "dir": "up" if (zs.bi_in and zs.bi_in.is_up()) else "down",
            })
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_stars = []
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        star_date = begin_klu.time.toFmtStr(date_fmt)
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "D", "color": "red"})
        else:
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "G", "color": "green"})

    bsps = []
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            klu = bsp.klu
            bsp_ts = 0
            try:
                bsp_ts = int(klu.time.ts * 1000)
            except Exception:
                try:
                    bsp_ts = int(datetime.strptime(klu.time.toFmtStr(date_fmt), date_fmt).timestamp()) * 1000
                except Exception:
                    bsp_ts = 0
            bsps.append({
                "date": klu.time.toFmtStr(date_fmt),
                "timestamp": bsp_ts,
                "type": bsp.type2str(), "is_buy": bsp.is_buy,
                "price": round(klu.close, 3),
                "high": round(klu.high, 3),
                "low": round(klu.low, 3),
            })
    except Exception as e:
        log.warning(f"[警告] 异常: {type(e).__name__}: {e}")

    return bis, fxs, segs, zs_list, zs_stars, bsps


def _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None, prev_klines=None, prev_ema_state=None, is_replay=False):
    """从 CChan 对象中提取缠论结构快照，格式与 /api/stock 一致。
    lightweight=True: 仅返回最后一根K线的OHLC变化（周期内tick更新用），不遍历全量结构。
    klines: 天勤实时K线DataFrame（lightweight=True时优先使用，避免chan框架kl_list滞后）。
    prev_klines/prev_ema_state: 增量快照 —— 复用缓存 klines 仅追加新确认K线，
    EMA 状态续算 MACD，避免每根K线全量 O(n) 重建；None 时走原始全量路径。
    is_replay: 复盘模式（软断开 end_time）标记，前端
    「复盘禁选点/禁重置/注记不保存」守卫依赖此标记。
    """
    kl_list = chan[kl_type]
    _date_fmt = _get_date_fmt(freq_label)
    _meta_freq_label = _get_freq_label(freq_label)

    if lightweight:
        # ★ 优先从天勤实时 klines 读取当前形成中K线的OHLC，避免 chan 框架 kl_list 滞后
        if klines is not None and len(klines) > 0:
            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            kline_dt = "?"
            if dt_ns is not None:
                try:
                    kline_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime(_date_fmt)
                except Exception as e:
                    log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
            o = float(last_row.get("open", 0) or 0)
            h = float(last_row.get("high", 0) or 0)
            l = float(last_row.get("low", 0) or 0)
            c = float(last_row.get("close", 0) or 0)
            return {
                "type": "tick",
                "kline": {
                    "date": kline_dt,
                    "open": round(o, 3),
                    "high": round(h, 3),
                    "low": round(l, 3),
                    "close": round(c, 3),
                },
                "meta": {
                    "symbol": symbol, "name": name, "freq": _meta_freq_label,
                    "generated_at": datetime.now().strftime(_date_fmt),
                    "is_realtime": True, "market": "futures",
                },
            }
        # 回退：无 klines 时从 chan 框架读取
        if len(kl_list.lst) == 0:  # type: ignore[union-attr]
            return None
        last_klc = kl_list.lst[-1]  # type: ignore[union-attr]
        if len(last_klc.lst) == 0:  # type: ignore[union-attr]
            return None
        last_klu = last_klc.lst[-1]  # type: ignore[union-attr]
        return {
            "type": "tick",
            "kline": {
                "date": last_klu.time.toFmtStr(_date_fmt),
                "open": round(last_klu.open, 3),
                "high": round(last_klu.high, 3),
                "low": round(last_klu.low, 3),
                "close": round(last_klu.close, 3),
            },
            "meta": {
                "symbol": symbol, "name": name, "freq": _meta_freq_label,
                "generated_at": datetime.now().strftime(_date_fmt),
                "is_realtime": True, "market": "futures",
            },
        }

    # ── klines：增量或全量 ──
    ema_state = None
    if prev_klines is not None:
        klines_out, _changed_idx = _incremental_klines(prev_klines, kl_list, _date_fmt)
        ema_state = _apply_macd_incremental(klines_out, _changed_idx, prev_ema_state)
    else:
        klines_out = []
        for klc in kl_list.lst:  # type: ignore[union-attr]
            for klu in klc.lst:  # type: ignore[union-attr]
                t = klu.time
                klines_out.append({
                    "date": t.toFmtStr(_date_fmt),
                    "timestamp": int(t.ts * 1000),
                    "open": round(klu.open, 3),
                    "high": round(klu.high, 3),
                    "low": round(klu.low, 3),
                    "close": round(klu.close, 3),
                    "vol": int(klu.trade_info.metric.get("volume", 0) or 0),
                    "amount": round(klu.trade_info.metric.get("turnover", 0) or 0, 2),
                })
        ema_state = _apply_macd_full(klines_out)

    # 统一结构元素提取（bis/fxs/segs/zs/zs_stars/bsps，与期货分析共用）
    bis, fxs, segs, zs_list, zs_stars, bsps = _extract_chan_structure(kl_list, chan, _date_fmt)

    return {
        "meta": {
            "symbol": symbol, "name": name, "freq": _meta_freq_label,
            "kline_count": len(klines_out), "bi_count": len(bis),
            "fx_count": len(fxs), "zs_count": len(zs_list),
            "seg_count": len(segs), "bsp_count": len(bsps),
            "generated_at": datetime.now().strftime(_date_fmt),
            "is_realtime": True, "is_replay": bool(is_replay), "market": "futures",
            "saved_selection_date": saved_selection_date,
            # 增量快照：EMA 状态续算 MACD（内部使用，前端忽略）
            "_ema_state": ema_state,
        },
        "klines": klines_out, "bis": bis, "fxs": fxs, "segs": segs,
        "zs": zs_list, "zs_stars": zs_stars, "bsps": bsps, "white_hline": None,
    }


def _calc_futures_white_hline(kl_list, _freq, date_fmt):
    """计算期货最新笔的白色横虚线数据（与股票逻辑一致）。
    返回 {"price": float, "start_date": str} 或 None。"""
    white_hline = None
    if not kl_list or not kl_list.bi_list:
        return white_hline
    latest_bi = kl_list.bi_list[-1]
    direction = "up" if latest_bi.is_up() else "down"
    end_klc = getattr(latest_bi, 'end_klc', None)
    if end_klc is None:
        return white_hline
    end_fx_idx = None
    for idx, klc in enumerate(kl_list.lst):  # type: ignore[union-attr]
        if klc is end_klc:
            end_fx_idx = idx
            break
    if end_fx_idx is None or end_fx_idx <= 0:
        return white_hline
    left_klc = kl_list.lst[end_fx_idx - 1]  # type: ignore[union-attr]
    klc_high = left_klc.high  # type: ignore[union-attr]
    klc_low = left_klc.low  # type: ignore[union-attr]
    tgt_klu = None
    if hasattr(left_klc, 'lst') and left_klc.lst:  # type: ignore[union-attr]
        for klu in left_klc.lst:  # type: ignore[union-attr]
            if direction == "down" and klu.high == klc_high:
                tgt_klu = klu
                break
            elif direction == "up" and klu.low == klc_low:
                tgt_klu = klu
                break
        if tgt_klu is None:
            tgt_klu = left_klc.lst[0]  # type: ignore[union-attr]
    if tgt_klu:
        ls_date = tgt_klu.time.toFmtStr(date_fmt)
    else:
        ls_date = ""
    if direction == "down":
        white_hline = {"price": round(klc_high, 2), "start_date": ls_date}
    elif direction == "up":
        white_hline = {"price": round(klc_low, 2), "start_date": ls_date}
    return white_hline


# ═══════════════════════════════════════════════════════════════════════
# 期货分析（HTTP 请求模式；与 SSE 实时流共用天勤数据源 + CChan 链路）
# ═══════════════════════════════════════════════════════════════════════


def _apply_macd_full(klines_out):
    """全量重算 MACD（原始路径），返回最后一根K线的 EMA 状态（增量续算用）。"""
    closes = [k["close"] for k in klines_out]
    if len(closes) >= 26:
        ema12 = ema(closes, 12)
        ema26 = ema(closes, 26)
        dif = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        dea = ema(dif, 9)
        macd_vals = [2 * (d - a) for d, a in zip(dif, dea)]
        for i in range(len(klines_out)):
            if i < len(dif):
                klines_out[i]["dif"] = round(dif[i], 4)
                klines_out[i]["dea"] = round(dea[i], 4)
                klines_out[i]["macd"] = round(macd_vals[i], 4)
            else:
                klines_out[i]["dif"] = 0; klines_out[i]["dea"] = 0; klines_out[i]["macd"] = 0
        return {"ema12": ema12[-1], "ema26": ema26[-1], "dea": dea[-1]}
    for k in klines_out:
        k["dif"] = 0; k["dea"] = 0; k["macd"] = 0
    return None


def _apply_macd_incremental(klines_out, changed_idx, prev_ema_state):
    """从 changed_idx 起增量重算 MACD（EMA 状态续算，O(1) 每根K线）。

    prev_ema_state 为 changed_idx-1 处K线的 EMA 状态（由上次快照 meta 携带）。
    返回更新后的 EMA 状态；状态缺失时回退全量重算。
    """
    if not klines_out:
        return prev_ema_state
    if prev_ema_state is None:
        return _apply_macd_full(klines_out)
    # 从 changed_idx 起逐根续算
    k12 = 2.0 / 13.0; k26 = 2.0 / 27.0; k9 = 2.0 / 10.0
    state = dict(prev_ema_state)
    for k in klines_out[changed_idx:]:
        c = k["close"]
        ema12 = c * k12 + state["ema12"] * (1 - k12)
        ema26 = c * k26 + state["ema26"] * (1 - k26)
        dif = ema12 - ema26
        dea = dif * k9 + state["dea"] * (1 - k9)
        k["dif"] = round(dif, 4)
        k["dea"] = round(dea, 4)
        k["macd"] = round(2 * (dif - dea), 4)
        state = {"ema12": ema12, "ema26": ema26, "dea": dea}
    return state


def _incremental_klines(prev_klines, kl_list, date_fmt):
    """增量更新 klines：复用缓存，仅追加新确认K线 / 修正尾部。

    prev_klines 可含末尾预览bar（调用方追加，date 为下一根形成K线），
    本函数剥离预览bar后返回纯确认K线，调用方再追加新预览bar。

    返回 (klines_out, changed_idx)：
      changed_idx = 需重算 MACD 的起始下标（新追加/合并修正的最后一根）。
    """
    if not kl_list.lst or not kl_list.lst[-1].lst:
        return list(prev_klines), len(prev_klines)
    last_klu = kl_list.lst[-1].lst[-1]
    last_dt_str = last_klu.time.toFmtStr(date_fmt)

    out = list(prev_klines)
    # 剥离预览bar（末尾条目日期 ≠ chan 最后一根确认K线日期）
    if out and out[-1]["date"] != last_dt_str:
        out.pop()
    if out and out[-1]["date"] == last_dt_str:
        # 尾部K线未变：仅更新 OHLC（合并场景）
        out[-1].update({
            "open": round(last_klu.open, 3),
            "high": round(last_klu.high, 3),
            "low": round(last_klu.low, 3),
            "close": round(last_klu.close, 3),
            "vol": int(last_klu.trade_info.metric.get("volume", 0) or 0),
            "amount": round(last_klu.trade_info.metric.get("turnover", 0) or 0, 2),
        })
        return out, len(out) - 1
    # 新确认K线：追加
    out.append({
        "date": last_dt_str,
        "timestamp": int(last_klu.time.ts * 1000),
        "open": round(last_klu.open, 3),
        "high": round(last_klu.high, 3),
        "low": round(last_klu.low, 3),
        "close": round(last_klu.close, 3),
        "vol": int(last_klu.trade_info.metric.get("volume", 0) or 0),
        "amount": round(last_klu.trade_info.metric.get("turnover", 0) or 0, 2),
    })
    return out, len(out) - 1


# ═══════════════════════════════════════════════════════════════════════
# 区域 3 · 期货选点
# ═══════════════════════════════════════════════════════════════════════

def futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """
    期货期指手选进入段：与股票 stock_manual_select_point 逻辑一致。
    创建临时 TqApi → 拉取全量历史 → 找到左肩时间T → 保存CSV →
    创建新 TqApi → 从T重新拉取 → 创建新CChan → 返回完整快照。
    """
    import time

    # 别名解析
    symbol_upper = symbol.upper()
    if symbol_upper in CTqSdkAPI.FUTURES_ALIASES:
        symbol = CTqSdkAPI.FUTURES_ALIASES[symbol_upper]

    freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 15)
    freq_label = freq
    freq_cn = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{freq_cn}"
    target_bi_idx = int(bi_idx)

    src = None
    src2 = None
    try:
        t_conn = time.time()
        src = CTqSdkSession()
        src.connect()
        log.info(f"[{display_key}] ⓪ 临时连接天勤(选点): 耗时 {time.time()-t_conn:.1f}s")

        records = src.fetch_kline(symbol, freq_sec=freq_sec, display_key=display_key)
        if len(records) < 5:
            raise DataFetchError(f"K线数据不足: 仅{len(records)}条")

        # 注入数据源 + 创建 CChan（统一走 _build_futures_chan；config 供 chan2 复用）
        # 数据注入经 src.set_data（Session 协议），不落类级缓存
        config = _make_chan_config()
        chan, kl_type = _build_futures_chan(records, symbol, freq_sec, config=config, src=src)

        kl_list = chan[kl_type]
        bi_list = kl_list.bi_list

        if target_bi_idx < 0 or target_bi_idx >= len(bi_list):
            raise AnalysisError(f"笔索引 {bi_idx} 越界，笔总数 {len(bi_list)}")

        # 检查选点后至少需要4笔
        remaining_bis = len(bi_list) - target_bi_idx - 1
        if remaining_bis < 4:
            raise AnalysisError(f"选点之后仅剩 {remaining_bis} 笔，至少需要4笔才能构建中枢，请重新选点")

        # Step 2: 找到左肩时间T
        start_time = _find_left_shoulder_time(kl_list, bi_list, target_bi_idx, freq)
        if start_time is None:
            raise AnalysisError("无法定位左肩K线时间，请重试")

        log.info(f"[{display_key}] 选点左肩时间: {start_time}")

        # Step 3: 保存选点到CSV（save_point_time 内部已在 _saved_point_lock 内
        # 同步更新内存态与落盘，调用方不再锁外直写内存态）
        name = _get_futures_name(symbol)
        app_data.save_point_time(symbol, name, freq, start_time)

        # Step 4: 关闭旧TqApi，创建新TqApi，从T重新拉取
        if src is not None:
            try:
                src.close()
                src.close_api()
            except Exception as e:
                log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
            src = None

        t_conn2 = time.time()
        src2 = CTqSdkSession()
        src2.connect()
        log.info(f"[{display_key}] ⓪ 重新连接天勤(选点后): 耗时 {time.time()-t_conn2:.1f}s")

        records2 = src2.fetch_kline(symbol, freq_sec=freq_sec,
                                    display_key=display_key, start_time=start_time)
        if len(records2) < 5:
            raise DataFetchError(f"选点后K线数据不足: 仅{len(records2)}条")

        # 注入数据源 + 创建新 CChan（统一走 _build_futures_chan，复用 config）
        # 数据注入经 src2.set_data（Session 协议），不落类级缓存
        chan2, _ = _build_futures_chan(records2, symbol, freq_sec, config=config, src=src2)

        # Step 5: 提取快照并返回
        result = _extract_realtime_snapshot(chan2, kl_type, symbol, name, freq_label,
                                            saved_selection_date=start_time)
        # 计算白色横虚线
        _kl_list = chan2[kl_type]
        _date_fmt = _get_date_fmt(freq)
        result['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _date_fmt)
        log.info(f"[{display_key}] 选点完成: {len(result['klines'])}K线, {result['meta']['bi_count']}笔, {result['meta']['zs_count']}中枢")
        return result

    except AppError:
        # 领域异常原样上抛（API 层统一中间件捕获，不二次包装）
        raise
    except Exception as e:
        import traceback
        log.info(f"[{display_key}] 选点异常: {e}")
        traceback.print_exc()
        raise AnalysisError(f"选点失败: {str(e)}") from e
    finally:
        if src is not None:
            try:
                src.close()
                src.close_api()
            except Exception as e:
                log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
        if src2 is not None:
            try:
                src2.close()
                src2.close_api()
            except Exception as e:
                log.warning(f"[警告] 异常: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 区域 4 · 市场/代码/周期查询
# ═══════════════════════════════════════════════════════════════════════

def get_futures_aliases():
    """期货别名映射（经 CTqSdkAPI 查询）"""
    if CTqSdkAPI is None:
        return {}
    return CTqSdkAPI.FUTURES_ALIASES


def get_futures_name(full_code):
    """期货名称"""
    if _get_futures_name:
        return _get_futures_name(full_code)
    return full_code


def tq_available():
    """天勤数据源是否可用"""
    return TQ_AVAILABLE


def get_futures_freqs():
    """期货可用周期列表（经 CTqSdkAPI 查询）"""
    try:
        if CTqSdkAPI is None:
            return {"supported_freqs": [], "disabled_freqs": []}
        return {"supported_freqs": CTqSdkAPI.SUPPORTED_FREQS,
                "disabled_freqs": CTqSdkAPI.DISABLED_FREQS}
    except ImportError:
        return {"supported_freqs": [], "disabled_freqs": []}


def get_futures_freq_sec_map():
    """期货周期→秒数映射（/api/health 的单一事实源出口，不直连 CTqSdkAPI）"""
    try:
        if CTqSdkAPI is None:
            return {}
        return dict(CTqSdkAPI.FREQ_SEC_MAP)
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════
# 区域 5 · 期货退出清理
# ═══════════════════════════════════════════════════════════════════════

def _cleanup_all_futures_data():
    """期货切到股票时彻底清理所有期货数据：K线缓存、分析缓存、选点记录

    注意：本函数**不得**调用 gc.collect()。SSE 期货流关闭时 TqApi 内部
    协程可能仍处于挂起态（tqsdk 在 Python 3.12+ 的已知问题，见官方
    issue #442），若在此强制 GC 会触发「Task was destroyed but it is
    pending!」+「Event loop is closed」错误级联（实测日志）。TqApi 的
    生命周期由 SSE 流 finally 块 src.close()→api.close() 负责，内存由
    Python 引用计数 + 自动 GC 回收即可。
    """
    # 1. 清空 CTqSdkAPI 的K线缓存
    if CTqSdkAPI is not None:
        CTqSdkAPI.clear_all_cache()
        log.info("[清理] 已清空期货K线缓存")

    # 2. 清空选点记录中的期货条目（key以KQ.开头，经 app_data 加锁删除）
    removed = app_data.clear_saved_points_by_prefix("KQ.")
    if removed:
        log.info(f"[清理] 已清除 {removed} 条期货选点记录")

    # 3. 清空期货分析缓存（双窗下窗 chan 残留：不清空则切回后
    #    check_nested_diver 经 futures_cache_get 读到过期中间状态）
    app_data.futures_cache_clear()
    log.info("[清理] 已清空期货分析缓存")


def futures_cleanup():
    """清理所有期货数据"""
    return _cleanup_all_futures_data()





# -*- coding: utf-8 -*-
"""
App/AppSSE.py —— SSE 实时流功能域
=========================================================================
按业务能力拆分（阶段 8 重设计）：期货实时行情通过 SSE 长连接持续推送到
前端图表（/api/futures_stream），本文件收纳该链路的核心机制与期货侧
配套能力。当前期货是唯一 SSE 实时流消费者；未来若增加股票实时，同样
收纳于此（命名取 SSE 而非 Futures 的原因）。

本模块收纳：
  - SSE 实时流支持（init_chan_symbol / _extract_realtime_snapshot /
    _calc_futures_white_hline，供 FrontAPI.CSSESource 调用）
  - SSE 同步生成器（sse_futures_stream_single/dual，V10 复审 P1-3 自
    FrontAPI.py 下沉；FrontAPI 仅 re-export，入口保持「薄」）
  - 期货静态分析（_analyze_futures_internal，HTTP 请求模式，与 SSE
    实时流共用天勤数据源 + CChan 分析链路）
  - 期货手动选点（futures_manual_select_point，临时 TqApi 拉全量 →
    定位左肩 → 从 T 重拉 → 新 CChan → 快照）
  - 期货数据清理（_cleanup_all_futures_data / futures_cleanup）
  - 期货元数据（get_futures_aliases / get_futures_name / tq_available /
    futures_config）
  - 实时快照公共包装（extract_realtime_snapshot，AppOrch re-export）

依赖方向：AppSSE.py → AppEngine / utils / AppData / DataAPI（单向）
  - 引擎侧私有实现（TQ_AVAILABLE / CTqSdkAPI / _get_futures_name）经
    AppEngine 导入；
  - 纯函数/常量（_get_date_fmt / _make_chan_config / ema / 周期映射 /
    中枢/左肩辅助 / _FUTURES_DUAL_FREQ_MAP / _SSE_DEBUG 等）自
    App/utils.py 导入（与 AppEngine 统一来源，P0-1c 显式化）；
  - 共享状态（saved_point_times / futures_analysis_cache / 选点保存）
    一律经 app_data.* 公共 API（同一对象，零漂移）；
  - 区间套辅助（_main_bi_range / _futures_red_range / CMyBSPointList）
    来自 BuySellPoint.BSPointList（与 AppEngine 同源）。
锁分类：SSE 路径 SELF_CONTAINED（每连接独立 TqApi+CChan，不加引擎锁）；
期货选点/静态分析由 AppChart 的 call_* 漏斗持 _ENGINE_LOCK 串行调用。
"""
import json
import time
import traceback
from datetime import datetime, timedelta

# 引擎侧依赖（仅引擎私有实现：天勤数据源 / 期货名称）
from App.AppEngine import (
    TQ_AVAILABLE, CTqSdkAPI, _get_futures_name,
)
# 引擎纯函数/常量公共工具（P0-1c：与 AppEngine 统一从 App/utils 导入）
from App.utils import (
    _make_chan_config, _get_kl_type, _get_kl_type_by_sec, _get_freq_label, _get_date_fmt,
    ema, calculate_macd,
    _calc_zs_confirm_edt_from_bis, _find_left_shoulder_time,
    _FUTURES_DUAL_FREQ_MAP, _SSE_DEBUG, _inherit_macd_for_preview_bar,
)
# 业务数据层（选点/期货子窗缓存；与 AppEngine 同一 app_data 单例）
from App.AppData import app_data
# P1-5 缓存键规范化：期货子窗键统一经规范化工厂（消除大小写漂移）
from App.AppData import make_futures_sub_key
# 区间套辅助（红框/双窗口共用，与 AppEngine 同源）
from BuySellPoint.BSPointList import _main_bi_range, _futures_red_range, CMyBSPointList
# chan.py 核心（与 AppEngine 同源；_analyze_futures_internal 直接使用）
from Chan import CChan
from Common.CEnum import AUTYPE, KL_TYPE, FX_TYPE
# SSE 数据源抽象（tqsdk 仅在 DataAPI 可见；生成器消费 src.* 协议）
from DataAPI.TqSdkCSSESource import CTqSdkSession, CSSESourceClosed
from App.AppLog import get_logger, trace_id
log = get_logger(__name__)



# ═══════════════════════════════════════════════════════════════════════
# SSE 实时流支持
# ═══════════════════════════════════════════════════════════════════════

def _build_futures_chan(records, symbol, freq_sec, config=None, code=None, src=None):
    """统一期货 CChan 构造：注入数据源 → 周期映射 → 建链 → 全量消费。

    取数唯一性收敛 P0-1（阶段 A/B）：收敛 AppSSE 内 5 处「set_data → 周期映射 →
    建 CChan → step_load」重复序列为单一构造函数。唯一来源：
      - 周期映射：AppEngine._get_kl_type_by_sec(freq_sec)（消除三份 _freq_to_kl）
      - 配置：_make_chan_config()（消除 CChanConfig() 与 _make_chan_config() 双来源）
    P1-1 数据源抽象单轨化：数据注入经 src.set_data（Session 协议）完成，
    不再直连 CTqSdkAPI.set_data；src 缺省时回退类级缓存（兼容旧调用）。
    返回 (chan, kl_type)。缓存逻辑留在各调用方，本函数只负责「数据 → CChan」。
    """
    chan_code = code or f"{symbol}:{freq_sec}"
    if src is not None:
        src.set_data(records, symbol=chan_code)
    else:
        CTqSdkAPI.set_data(records, symbol=chan_code)
    kl_type = _get_kl_type_by_sec(freq_sec)
    config = config or _make_chan_config()
    chan = CChan(
        code=chan_code, begin_time=None, end_time=None,
        data_src="custom:TqSdkAPI.CTqSdkAPI",
        lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
        market_type="futures",
    )
    for _snapshot in chan.step_load():
        pass
    return chan, kl_type


def init_chan_symbol(src, symbol, _name, freq_sec, freq_label, start_time=None):
    """拉取历史K线 + 运行 chan.py 分析，返回 (chan, klines, kl_type, records) 或 None。
    由 SSE handler 调用，每个 SSE 连接自包含。
    start_time: 选点起始时间，有值时只拉取该时间之后的K线
    V10 复审 P1-2：首参改为数据源对象 src（CTqSdkSession），服务层只消费
    src.fetch_kline / src.get_kline_serial 协议，不再触碰 src.api 原始对象。"""
    import time as _time

    display_label = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{display_label}"

    try:
        records = src.fetch_kline(symbol, freq_sec=freq_sec, display_key=display_key, start_time=start_time)
        if len(records) > 1:
            now = datetime.now()
            if (now - records[-1]["dt"]).total_seconds() < freq_sec:
                records = records[:-1]

        if len(records) == 0:
            log.info(f"[{display_key}] ⑵ 无有效数据，跳过")
            return None

        t_chan = _time.time()

        # 统一构造（P0-1 取数唯一性收敛）：set_data + 周期映射 + 建 CChan + step_load
        # P1-1 数据源抽象单轨化：数据注入经 src.set_data（Session 协议），不落类级缓存
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

    原为数据源方法 CSSESource.step_load；彻底解耦业务后提升为服务层
    纯业务函数，生成器经本函数消耗引擎，数据源不再关心缠论计算。
    """
    for _snapshot in chan.step_load():
        pass


# ═══════════════════════════════════════════════════════════════════
# P2-4 增量快照：每根 K 线完成不再全量 O(n) 重建 klines/MACD，
# 复用缓存快照仅追加新确认K线 + EMA 状态续算，结构元素仍重建。
# ═══════════════════════════════════════════════════════════════════
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


def _extract_chan_structure(kl_list, chan, date_fmt):
    """统一缠论结构元素提取（P2-7）：bis / fxs / segs / zs / zs_stars / bsps。

    SSE 实时快照（_extract_realtime_snapshot）与期货静态分析
    （_analyze_futures_internal）共用同一提取逻辑，消除两份几乎相同的
    内联实现（约 200 行重复），避免字段漂移。肩部原始K线时间统一走
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
            # None，会丢失另一侧已有的肩部时间，期货静态路径依赖此行为）
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


def _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None, prev_klines=None, prev_ema_state=None):
    """从 CChan 对象中提取缠论结构快照，格式与 /api/stock 一致。
    lightweight=True: 仅返回最后一根K线的OHLC变化（周期内tick更新用），不遍历全量结构。
    klines: 天勤实时K线DataFrame（lightweight=True时优先使用，避免chan框架kl_list滞后）。
    prev_klines/prev_ema_state: P2-4 增量快照 —— 复用缓存 klines 仅追加新确认K线，
    EMA 状态续算 MACD，避免每根K线全量 O(n) 重建；None 时走原始全量路径。
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

    # ── klines：增量（P2-4）或全量 ──
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

    # P2-7 统一结构元素提取（bis/fxs/segs/zs/zs_stars/bsps，与静态分析共用）
    bis, fxs, segs, zs_list, zs_stars, bsps = _extract_chan_structure(kl_list, chan, _date_fmt)

    return {
        "meta": {
            "symbol": symbol, "name": name, "freq": _meta_freq_label,
            "kline_count": len(klines_out), "bi_count": len(bis),
            "fx_count": len(fxs), "zs_count": len(zs_list),
            "seg_count": len(segs), "bsp_count": len(bsps),
            "generated_at": datetime.now().strftime(_date_fmt),
            "is_realtime": True, "is_replay": False, "market": "futures",
            "saved_selection_date": saved_selection_date,
            # P2-4 增量快照：EMA 状态续算 MACD（内部使用，前端忽略）
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
# 期货静态分析（HTTP 请求模式；与 SSE 实时流共用天勤数据源 + CChan 链路）
# ═══════════════════════════════════════════════════════════════════════

def _analyze_futures_internal(code, freq="1m", end_date=None, dual=False, existing_chan=None, existing_records=None, step=None, sub_freq=None):
    """
    使用天勤数据源 + chan.py 进行期货/期指缠论分析（静态模式，HTTP 请求）
    与股票分析输出格式一致，便于前端复用同一套图表渲染逻辑。

    dual=True: 双窗口模式，返回 result 含 sub 字段（两个独立 CChan 对象）。
    existing_chan: 双窗口模式下，复用已有的单窗口 CChan 对象（匹配周期则复用）。
    existing_records: 对应 existing_chan 的 records。
    step: 箭头步进，在 full_records 中从 end_date 位置偏移 step 根K线作为新的截断日期。
    sub_freq: 双窗口下窗周期。None 时使用默认映射 _FUTURES_DUAL_FREQ_MAP。
    """
    import time
    t_start = time.time()

    if not TQ_AVAILABLE or CTqSdkAPI is None:
        return {"error": "天勤数据源未安装，请执行: pip install tqsdk"}

    # 确定周期秒数
    freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 86400)

    # 1. 拉取历史K线（每次冷启动重新拉取天勤数据）
    t_fetch = time.time()
    from DataAPI.TqSdkCSSESource import CTqSdkSession
    _display_key = f"{code}:{CTqSdkAPI.FREQ_LABEL_CN.get(freq, freq)}"
    _src = None
    full_records = []
    try:
        _src = CTqSdkSession()
        _src.connect()
        full_records = _src.fetch_kline(code, freq_sec=freq_sec, display_key=_display_key)
    except Exception as _e:
        log.error(f"[期货][错误] 天勤拉取K线失败: {type(_e).__name__}: {_e}")
        return {"error": f"天勤拉取K线失败: {type(_e).__name__}: {_e}"}
    finally:
        if _src is not None:
            try:
                _src.close()
                _src.close_api()
            except Exception as _e:
                log.warning(f"[警告] 关闭天勤连接异常: {type(_e).__name__}: {_e}")
    log.info(f"[拉取] ⑴ 天勤拉取K线: {time.time()-t_fetch:.3f}s, {len(full_records)}条")
    if len(full_records) < 5:
        return {"error": f"K线数据不足: 仅{len(full_records)}条"}

    # 2. 截断（end_date 复盘模式）
    if end_date:
        target_dt = None
        for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
            try:
                target_dt = datetime.strptime(end_date, fmt)
                break
            except ValueError:
                continue
        if target_dt is None:
            return {"error": f"无法解析日期: {end_date}"}
        # === 箭头步进：在 full_records 中从 end_date 位置偏移 step 根K线 ===
        if step is not None:
            step = int(step)
            if step != 0:
                anchor_idx = None
                for i in range(len(full_records) - 1, -1, -1):
                    if full_records[i]["dt"] <= target_dt:
                        anchor_idx = i
                        break
                if anchor_idx is not None:
                    new_idx = anchor_idx + step
                    if 0 <= new_idx < len(full_records):
                        target_dt = full_records[new_idx]["dt"]
                        end_date = target_dt.strftime("%Y-%m-%d %H:%M:%S")
                        log.info(f"[futures][箭头] step={step}: {full_records[anchor_idx]['dt']} → {target_dt} (idx {anchor_idx} → {new_idx})")
                    else:
                        log.info(f"[futures][箭头] step={step} 越界: idx {anchor_idx} → {new_idx}, 共{len(full_records)}条")

        records = [r for r in full_records if r["dt"] <= target_dt]
        if len(records) < 5:
            return {"error": f"截断后K线数据不足: 仅{len(records)}条"}
    else:
        records = full_records

    # 3+5. 注入数据源 + 创建 CChan 并消费（P0-1 统一 _build_futures_chan）
    t0 = time.time()

    # 获取品种名称（结果提取使用）
    stock_name = _get_futures_name(code)

    # 每次请求重置复盘标记，避免残留前一次状态
    CMyBSPointList.REPLAY_MODE = False

    try:
        if end_date:
            CMyBSPointList.REPLAY_MODE = True
        # P1-1 数据源抽象单轨化：数据注入经 _src.set_data（Session 协议），不落类级缓存
        chan, kl_type = _build_futures_chan(records, symbol=code, freq_sec=freq_sec, code=code, src=_src)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        records_info = ""
        if records:
            records_info = f" records={len(records)}条 [{records[0]['dt']} ~ {records[-1]['dt']}]"
        log.error(f"[期货][错误] chan.py 分析失败: code={code} freq={freq}{records_info} 耗时={time.time()-t0:.3f}s")
        log.error(f"[期货][错误] 异常类型: {type(e).__name__}, 异常信息: {e}")
        log.error(f"[期货][错误] 完整堆栈:\n{tb}")
        return {"error": f"chan.py 期货分析失败: {type(e).__name__}: {e}"}
    finally:
        if end_date:
            CMyBSPointList.REPLAY_MODE = False

    kl_list = chan[kl_type]
    log.info(f"[分析] ⑶ chan.py分析: {time.time()-t0:.3f}s, 合并K线={len(kl_list.lst)}, 笔={len(kl_list.bi_list)}, 中枢={len(kl_list.zs_list)}")

    # 6. 提取结果（与股票一致的格式，用 records 而非 kl_list）

    t_extract = time.time()
    closes = [r["close"] for r in records]
    macd_list = calculate_macd(closes)
    date_fmt = _get_date_fmt(freq)

    # K线数据（从 records 构建，与股票代码一致）
    kline_data = []
    for i, row in enumerate(records):
        macd = macd_list[i] if i < len(macd_list) else {"dif": 0, "dea": 0, "macd": 0}
        kline_data.append({
            "date": row["dt"].strftime(date_fmt),
            "timestamp": int(row["dt"].timestamp()) * 1000,
            "open": row["open"], "high": row["high"],
            "low": row["low"], "close": row["close"],
            "vol": row["vol"], "amount": row["amount"],
            "dif": round(macd["dif"], 4),
            "dea": round(macd["dea"], 4),
            "macd": round(macd["macd"], 4),
        })

    # 笔、线段、中枢、买卖点提取（P2-7 统一 _extract_chan_structure，与 SSE 共用）
    bi_data, fx_data, seg_data, zs_data, zs_stars, bsp_data = _extract_chan_structure(kl_list, chan, date_fmt)

    # 计算白色横虚线（最新笔分型上下沿，K线确认后才有意义）
    white_hline = _calc_futures_white_hline(kl_list, freq, date_fmt)

    # 7. 组装结果
    log.info(f"[分析] ⑷ 提取结果(K线/笔/分型/线段/中枢/买卖点): {time.time()-t_extract:.3f}s")
    date_range = f"{kline_data[0]['date']} ~ {kline_data[-1]['date']}" if kline_data else ""
    result = {
        "meta": {
            "symbol": code,
            "name": stock_name,
            "freq": _get_freq_label(freq),
            "chan_version": "chan.py",
            "kline_count": len(kline_data),
            "bi_count": len(bi_data),
            "fx_count": len(fx_data),
            "zs_count": len(zs_data),
            "seg_count": len(seg_data),
            "bsp_count": len(bsp_data),
            "date_range": date_range,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_replay": bool(end_date),
            "forward_adjust": False,
            "market": "futures",
        },
        "klines": kline_data,
        "bis": bi_data,
        "fxs": fx_data,
        "zs": zs_data,
        "zs_stars": zs_stars,
        "segs": seg_data,
        "bsps": bsp_data,
        "white_hline": white_hline,
    }

    log.info(f"[信息] 期货查询 {code} 完成({_get_freq_label(freq)}): {len(kline_data)}K线, {len(bi_data)}笔, {len(fx_data)}分型, {len(zs_data)}中枢, {len(seg_data)}线段, {len(bsp_data)}买卖点")
    log.info(f"[耗时] 总耗时: {time.time()-t_start:.3f}s")

    # 双窗口模式：提取子级别数据（独立 CChan 对象）
    if dual:
        # 优先使用传入的 sub_freq（双窗口周期独立），否则用默认映射
        if not sub_freq:
            sub_freq = _FUTURES_DUAL_FREQ_MAP.get(freq)
        if not sub_freq:
            result["error"] = f"双窗口不支持当前周期: {freq}"
            return result
        sub_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(sub_freq, 15)
        log.info(f"[双窗口] 开始提取子级别({sub_freq})数据...")

        # 检查 existing_chan 是否匹配 sub_freq，匹配则复用
        if existing_chan is not None and existing_records is not None and freq == sub_freq:
            # existing_chan 匹配的是主周期，子周期需要新建
            pass
        elif existing_chan is not None and existing_records is not None and freq != sub_freq:
            # 如果 existing_chan 正好匹配 sub_freq，复用它
            # 这个情况发生在上窗周期!=单窗口周期时（暂不涉及，保留接口）
            pass

        # 拉取子级别历史K线（与主级别一致：经 CTqSdkSession 数据源抽象建连，
        # tqsdk 仅 DataAPI 可见；修复 V10 复审 P0-1：原直接构造 TqApi 的
        # 悬空引用 NameError）
        t_sub_fetch = time.time()
        _sub_display_key = f"{code}:{CTqSdkAPI.FREQ_LABEL_CN.get(sub_freq, sub_freq)}"
        _src_sub = None
        sub_full_records = []
        try:
            _src_sub = CTqSdkSession()
            _src_sub.connect()
            sub_full_records = _src_sub.fetch_kline(code, freq_sec=sub_freq_sec, display_key=_sub_display_key)
        except Exception as _e:
            log.error(f"[双窗口][错误] 子级别天勤拉取K线失败: {type(_e).__name__}: {_e}")
            return result
        finally:
            if _src_sub is not None:
                try:
                    _src_sub.close()
                    _src_sub.close_api()
                except Exception as _e:
                    log.warning(f"[警告] 关闭天勤子级别连接异常: {type(_e).__name__}: {_e}")
        log.info(f"[双窗口] 子级别({sub_freq})拉取K线: {time.time()-t_sub_fetch:.3f}s, {len(sub_full_records)}条")

        if len(sub_full_records) < 5:
            log.info(f"[双窗口] 子级别({sub_freq})数据不足，仅{len(sub_full_records)}条，跳过")
            return result

        # 截断到主级别时间范围（同步）
        # 期货K线时间=开始时间（不同于股票=结束时间），用数学换算精确截断：
        #   下窗右边界 = 上窗最后一根K线开始时间 + (上窗周期 - 下窗周期)
        if len(sub_full_records) > 0 and records:
            main_start = records[0]["dt"]
            main_end = records[-1]["dt"]
            offset_sec = freq_sec - sub_freq_sec
            sub_end = main_end + timedelta(seconds=offset_sec)
            sub_before = len(sub_full_records)
            sub_full_records = [r for r in sub_full_records
                                if main_start <= r["dt"] <= sub_end]
            if sub_before != len(sub_full_records):
                log.info(f"[双窗口] 子级别({sub_freq})同步截断: {sub_before}条 -> {len(sub_full_records)}条")

        sub_records = sub_full_records

        # 注入子级别数据源 + 创建子级别 CChan（P0-1 统一 _build_futures_chan）
        # P1-1 数据源抽象单轨化：数据注入经 _src_sub.set_data（Session 协议），不落类级缓存
        sub_code = f"{code}:{sub_freq_sec}"
        t_sub_chan = time.time()
        try:
            sub_chan, sub_kl_type = _build_futures_chan(sub_records, symbol=code, freq_sec=sub_freq_sec, code=sub_code, src=_src_sub)
        except Exception as e:
            log.info(f"[双窗口] 子级别({sub_freq}) chan.py 分析失败: {e}")
            return result

        app_data.futures_analysis_cache[make_futures_sub_key(code, sub_freq)] = sub_chan
        sub_kl_list = sub_chan[sub_kl_type]
        log.info(f"[双窗口] 子级别({sub_freq}) chan.py分析: {time.time()-t_sub_chan:.3f}s, "
              f"合并K线={len(sub_kl_list.lst)}, 笔={len(sub_kl_list.bi_list)}, 中枢={len(sub_kl_list.zs_list)}")

        # 提取子级别结果
        sub_name = _get_futures_name(code)
        sub_result = _extract_realtime_snapshot(
            sub_chan, sub_kl_type, code, sub_name,
            sub_freq, klines=None
        )
        result["sub"] = sub_result
        # 将 fx_a_raw_dt/fx_b_raw_dt（天勤K线开始时间）换算为子级别时间
        main_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 60)
        _futures_red_range(result, main_freq_sec, sub_freq_sec, sub_freq)
        log.info(f"[双窗口] 子级别({sub_freq})提取完成: K线={sub_result['meta']['kline_count']}, "
              f"笔={sub_result['meta']['bi_count']}, 中枢={sub_result['meta']['zs_count']}")

    return result


# ═══════════════════════════════════════════════════════════════════════
# 期货手动选点
# ═══════════════════════════════════════════════════════════════════════

def futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """
    期货期指手选进入段：与股票 stock_manual_select_point 逻辑一致。
    创建临时 TqApi → 拉取全量历史 → 找到左肩时间T → 保存CSV →
    创建新 TqApi → 从T重新拉取 → 创建新CChan → 返回完整快照。
    """
    import time
    from DataAPI.TqSdkCSSESource import CTqSdkSession
    from DataAPI.TqSdkAPI import CTqSdkAPI

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
            return {"error": f"K线数据不足: 仅{len(records)}条"}

        # 注入数据源 + 创建 CChan（P0-1 统一 _build_futures_chan；config 供 chan2 复用）
        # P1-1 数据源抽象单轨化：数据注入经 src.set_data（Session 协议），不落类级缓存
        config = _make_chan_config()
        chan, kl_type = _build_futures_chan(records, symbol, freq_sec, config=config, src=src)

        kl_list = chan[kl_type]
        bi_list = kl_list.bi_list

        if target_bi_idx < 0 or target_bi_idx >= len(bi_list):
            return {"error": f"笔索引 {bi_idx} 越界，笔总数 {len(bi_list)}"}

        # 检查选点后至少需要4笔
        remaining_bis = len(bi_list) - target_bi_idx - 1
        if remaining_bis < 4:
            return {"error": f"选点之后仅剩 {remaining_bis} 笔，至少需要4笔才能构建中枢，请重新选点"}

        # Step 2: 找到左肩时间T
        start_time = _find_left_shoulder_time(kl_list, bi_list, target_bi_idx, freq)
        if start_time is None:
            return {"error": "无法定位左肩K线时间，请重试"}

        log.info(f"[{display_key}] 选点左肩时间: {start_time}")

        # Step 3: 保存选点到CSV
        name = _get_futures_name(symbol)
        app_data.save_point_time(symbol, name, freq, start_time)
        if symbol not in app_data.saved_point_times:
            app_data.saved_point_times[symbol] = {}
        app_data.saved_point_times[symbol]["name"] = name
        app_data.saved_point_times[symbol][app_data.freq_to_col(freq) or ""] = start_time

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
            return {"error": f"选点后K线数据不足: 仅{len(records2)}条"}

        # 注入数据源 + 创建新 CChan（P0-1 统一 _build_futures_chan，复用 config）
        # P1-1 数据源抽象单轨化：数据注入经 src2.set_data（Session 协议），不落类级缓存
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

    except Exception as e:
        import traceback
        log.info(f"[{display_key}] 选点异常: {e}")
        traceback.print_exc()
        return {"error": f"选点失败: {str(e)}"}
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
# 期货数据清理
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

    # 2. 清空选点记录中的期货条目（key以KQ.开头）
    pts_to_del = [k for k in list(app_data.saved_point_times.keys()) if k.startswith("KQ.")]
    for k in pts_to_del:
        del app_data.saved_point_times[k]
    if pts_to_del:
        log.info(f"[清理] 已清除 {len(pts_to_del)} 条期货选点记录")


def futures_cleanup():
    """清理所有期货数据"""
    return _cleanup_all_futures_data()


# ═══════════════════════════════════════════════════════════════════════
# 期货元数据
# ═══════════════════════════════════════════════════════════════════════

def get_futures_aliases():
    """期货别名映射（阶段 5：经 CTqSdkAPI 元数据接口）"""
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


def futures_config():
    """期货可用周期列表（阶段 5：经 CTqSdkAPI 元数据接口）"""
    try:
        if CTqSdkAPI is None:
            return {"supported_freqs": [], "disabled_freqs": []}
        return {"supported_freqs": CTqSdkAPI.SUPPORTED_FREQS,
                "disabled_freqs": CTqSdkAPI.DISABLED_FREQS}
    except ImportError:
        return {"supported_freqs": [], "disabled_freqs": []}


# ═══════════════════════════════════════════════════════════════════════
# 实时快照公共包装（AppOrch re-export 用）
# ═══════════════════════════════════════════════════════════════════════

def extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None):
    """从实时行情快照中提取分析所需字段（供 SSE 路径使用）"""
    return _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                      saved_selection_date, lightweight, klines)

# ═══════════════════════════════════════════════════════════════════════
# SSE 同步生成器（方案A · V10 复审 P1-3：自 FrontAPI.py 下沉）
# ═══════════════════════════════════════════════════════════════════════
# 事件协议与遗留实现逐字一致：init（初始快照/失败载荷）→ update（tick 路径
# 与 K 线完成路径）→ 心跳注释帧 ": heartbeat"。source 可注入（默认
# CTqSdkSession）；Test/test_sse_gray.py 用 MockSource 驱动确定性比对。
# 锁分类 SELF_CONTAINED（见 AppOrch.LOCK_POLICY）：每连接独立 TqApi+CChan，
# 不加引擎锁。选点/期货子窗缓存经 app_data 单例（与 AppEngine 同一对象）。


def _get_saved_point(code, freq):
    """查询单个选点（数据层漏斗；与 AppChart.get_saved_point 语义一致）。"""
    col = app_data.freq_to_col(freq)
    if not col:
        return ""
    return app_data.saved_point_times.get(code, {}).get(col, "").strip()


def _sse_frame(event, payload) -> bytes:
    """构造一帧 SSE 事件（与遗留实现的字节格式逐字一致）"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, allow_nan=False)}\n\n".encode("utf-8")


def sse_futures_stream_single(symbol, freq="15s", start_time=None, source=None):
    """期货 SSE 单窗口 · 同步生成器（方案A）

    忠实移植 ChartHandler._handle_sse_stream_single 的事件协议：
    init（初始快照/失败载荷）→ 实时循环（heartbeat 注释帧 + update 事件：
    tick 路径更新末根 K 线 OHLC/MACD，K线完成路径全量快照）→ 收尾清理。
    source 可注入（默认 CTqSdkSession）；Test/test_sse_gray.py 用 MockSource
    驱动确定性比对。锁分类 SELF_CONTAINED（见 AppOrch.LOCK_POLICY）。
    """
    import logging
    from datetime import datetime
    logging.getLogger("tqsdk").setLevel(logging.WARNING)
    logging.getLogger("tqsdk.tqapi").setLevel(logging.WARNING)
    for h in logging.root.handlers:
        h.setLevel(logging.WARNING)

    _tid = trace_id()  # 每连接专属 trace-id（P0-3），连接生命周期内稳定
    log.info("[%s] SSE 单窗口连接: symbol=%s freq=%s", _tid, symbol, freq)

    src = source if source is not None else CTqSdkSession()

    from DataAPI.TqSdkAPI import CTqSdkAPI

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
        # （阶段 4：经 AppOrch 漏斗读 AppData，不再直连 my_chan_main 状态）
        if start_time is None:
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

        # === 1. 拉取历史 + chan 分析 ===
        # 彻底解耦业务：历史拉取/chan 分析在服务层 AppSSE.init_chan_symbol
        # V10 复审 P1-2：首参传数据源对象 src（不再触碰 src.api 原始对象）
        result = init_chan_symbol(src, symbol, name, freq_sec, freq_label, start_time)
        if result is None:
            yield _sse_frame("init", {"error": "初始化失败（无数据或网络异常）", "symbol": symbol})
            return
        chan, klines, kl_type, records = result

        # === 2. 推送初始快照 ===
        t0 = time.time()
        try:
            init_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                            saved_selection_date=saved_selection_date)
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
        # 策略与遗留实现一致：壁钟（datetime.now()）判断K线周期结束，不等天勤
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
                    log.warning(f"[警告] 异常: {type(_e).__name__}: {_e}")
                tqsdk_last_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime('%H:%M:%S') if dt_ns else "None"
                chan_last_dt = chan_last_klu.time.to_str()[:16] if chan_last_klu and hasattr(chan_last_klu, 'time') else "None"

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
                bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
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
                bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec

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
                # P2-4 增量快照：复用缓存 klines + EMA 状态续算 MACD，避免每根K线全量 O(n) 重建
                update_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                saved_selection_date=saved_selection_date,
                                                prev_klines=(cached_snapshot or {}).get("klines"),
                                                prev_ema_state=(cached_snapshot or {}).get("meta", {}).get("_ema_state"))
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
        # 与遗留实现一致：打印连接异常后静默结束（错误已在 init 事件载荷中表达）
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


def sse_futures_stream_dual(symbol, main_freq="1m", sub_freq=None, start_time=None, source=None):
    """期货 SSE 双窗口 · 同步生成器（方案A）

    忠实移植 ChartHandler._handle_sse_stream_dual 的事件协议：
    两个独立 CChan 对象、一次连接推送两个周期（下窗先处理——区间套分析
    需先分析次级别）。source 可注入（默认 CTqSdkSession）。
    锁分类 SELF_CONTAINED（见 AppOrch.LOCK_POLICY）。
    """
    _tid = trace_id()  # 每连接专属 trace-id（P0-3），连接生命周期内稳定
    log.info("[%s] SSE 双窗口连接: symbol=%s main=%s sub=%s",
             _tid, symbol, main_freq, sub_freq)

    src = source if source is not None else CTqSdkSession()

    from DataAPI.TqSdkAPI import CTqSdkAPI
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

        # 1. 查询选点状态（阶段 4：经 AppOrch 漏斗读 AppData）
        saved_selection_date = ""
        main_start_time = start_time
        sub_start_time = start_time
        try:
            qualified_code = symbol
            col_meta = app_data.freq_to_col(main_freq) or ""
            if col_meta:
                saved_selection_date = _get_saved_point(qualified_code, main_freq)
                # 如果外部没传start_time，从CSV读取选点
                if main_start_time is None and saved_selection_date:
                    main_start_time = saved_selection_date
            # 下窗也查询选点
            sub_col_meta = app_data.freq_to_col(sub_freq) or ""
            if sub_col_meta:
                sub_saved = _get_saved_point(qualified_code, sub_freq)
                if sub_start_time is None and sub_saved:
                    sub_start_time = sub_saved
        except Exception as _e:
            log.warning(f"[警告] 异常: {type(_e).__name__}: {_e}")

        # 2. 拉取下窗历史 + chan分析（次级别优先：区间套分析需先分析次级别）
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 拉取下窗({sub_freq})历史K线...")
        sub_result = init_chan_symbol(src, symbol, name, sub_freq_sec, sub_freq_label, sub_start_time)
        sub_chan, sub_records, sub_kl_type, _ = sub_result
        sub_kl_type = _get_kl_type(sub_freq)
        # 缓存下窗 CChan 供 /api/dual_zs 访问（语义化漏斗：key 规则内聚数据层）
        app_data.set_futures_sub_chan(symbol, sub_freq, sub_chan)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 下窗({sub_freq}) chan.py: 合并K线={len(sub_chan[sub_kl_type].lst)}, "
                  f"笔={len(sub_chan[sub_kl_type].bi_list)}, 中枢={len(sub_chan[sub_kl_type].zs_list)}")

        # 3. 拉取上窗历史 + chan分析
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 拉取上窗({main_freq})历史K线...")
        main_result = init_chan_symbol(src, symbol, name, main_freq_sec, main_freq_label, main_start_time)
        main_chan, main_records, main_kl_type, _ = main_result
        main_kl_type = _get_kl_type(main_freq)
        if _SSE_DEBUG:
            log.info(f"[{display_key}] 上窗({main_freq}) chan.py: 合并K线={len(main_chan[main_kl_type].lst)}, "
                  f"笔={len(main_chan[main_kl_type].bi_list)}, 中枢={len(main_chan[main_kl_type].zs_list)}")

        # 7. 提取初始快照
        t_snap = time.time()
        main_snapshot = _extract_realtime_snapshot(main_chan, main_kl_type, symbol, name, main_freq_label,
                                                 saved_selection_date=saved_selection_date)
        sub_snapshot = _extract_realtime_snapshot(sub_chan, sub_kl_type, symbol, name, sub_freq_label,
                                                       klines=None)
        # 期货双窗口：上窗 bis 的 fx_a_raw_dt/fx_b_raw_dt 是上层K线时间，
        # 需要换算成子级别K线时间，前端 calcRedRange 才能正确匹配
        # （阶段 8：_futures_red_range 已随期货功能域迁 App/AppSSE.py）
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
                bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
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
                bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec

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

            # 提取完整快照（P2-4 增量：复用缓存 klines + EMA 状态续算 MACD）
            if updated:
                snapshot = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                           saved_selection_date=saved_selection_date,
                                                           prev_klines=(cached_snapshot or {}).get("klines"),
                                                           prev_ema_state=(cached_snapshot or {}).get("meta", {}).get("_ema_state"))
                if is_main:
                    # 阶段 8：_futures_red_range 已随期货功能域迁 App/AppSSE.py
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

            # 处理下窗（次级别优先：区间套分析需先分析次级别）
            _t_sub0 = time.time()
            sub_updated, sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns, sub_need_tick = \
                _process_one_window(sub_klines, sub_chan, sub_kl_type, sub_freq_sec, sub_freq_label,
                                          sub_cached_snapshot, sub_last_bar_dt_ns, sub_last_processed_dt_ns,
                                          is_main=False, window_label="下窗")
            _t_sub = time.time() - _t_sub0

            # 处理上窗
            _t_main0 = time.time()
            main_updated, main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns, main_need_tick = \
                _process_one_window(main_klines, main_chan, main_kl_type, main_freq_sec, main_freq_label,
                                          main_cached_snapshot, main_last_bar_dt_ns, main_last_processed_dt_ns,
                                          is_main=True, window_label="上窗")
            _t_main = time.time() - _t_main0

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
        # 与遗留实现一致：打印连接异常后静默结束
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

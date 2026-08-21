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
  - 期货静态分析（_analyze_futures_internal，HTTP 请求模式，与 SSE
    实时流共用天勤数据源 + CChan 分析链路）
  - 期货手动选点（futures_manual_select_point，临时 TqApi 拉全量 →
    定位左肩 → 从 T 重拉 → 新 CChan → 快照）
  - 期货数据清理（_cleanup_all_futures_data / futures_cleanup）
  - 期货元数据（get_futures_aliases / get_futures_name / tq_available /
    futures_config）
  - 实时快照公共包装（extract_realtime_snapshot，AppOrch re-export）

依赖方向：AppSSE.py → AppEngine / AppData / DataAPI（单向）
  - 引擎侧纯函数/常量（_get_date_fmt / _make_chan_config / ema 等）与
    共享状态（_saved_point_times / _futures_analysis_cache）经 AppEngine
    导入（同一对象，零漂移）；
  - 区间套辅助（_main_bi_range / _futures_red_range / CMyBSPointList）
    来自 BuySellPoint.BSPointList（与 AppEngine 同源）。
锁分类：SSE 路径 SELF_CONTAINED（每连接独立 TqApi+CChan，不加引擎锁）；
期货选点/静态分析由 AppChart 的 call_* 漏斗持 _ENGINE_LOCK 串行调用。
"""
from datetime import datetime, timedelta

# 引擎侧依赖（纯函数/常量/共享状态，与 AppEngine 同一对象）
from App.AppEngine import (
    TQ_AVAILABLE, CTqSdkAPI, _get_futures_name,
    _make_chan_config, _get_kl_type, _get_freq_label, _get_date_fmt,
    ema, calculate_macd,
    _calc_zs_confirm_edt_from_bis, _find_left_shoulder_time, _save_point_time,
    _futures_analysis_cache, _FUTURES_DUAL_FREQ_MAP, _saved_point_times,
    FREQ_TO_COL, _SSE_DEBUG,
)
# 区间套辅助（红框/双窗口共用，与 AppEngine 同源）
from BuySellPoint.BSPointList import _main_bi_range, _futures_red_range, CMyBSPointList
# chan.py 核心（与 AppEngine 同源；_analyze_futures_internal 直接使用）
from Chan import CChan
from Common.CEnum import AUTYPE, KL_TYPE, FX_TYPE


# ═══════════════════════════════════════════════════════════════════════
# SSE 实时流支持
# ═══════════════════════════════════════════════════════════════════════

def init_chan_symbol(api, symbol, _name, freq_sec, freq_label, start_time=None):
    """拉取历史K线 + 运行 chan.py 分析，返回 (chan, klines, kl_type, records) 或 None。
    由 SSE handler 调用，每个 SSE 连接自包含。
    start_time: 选点起始时间，有值时只拉取该时间之后的K线"""
    import time as _time
    from Common.CEnum import KL_TYPE, AUTYPE
    from Chan import CChan
    from ChanConfig import CChanConfig

    display_label = CTqSdkAPI.FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{display_label}"

    try:
        records = CTqSdkAPI.fetch_kline(api, symbol, freq_sec=freq_sec, display_key=display_key, start_time=start_time)
        if len(records) > 1:
            now = datetime.now()
            if (now - records[-1]["dt"]).total_seconds() < freq_sec:
                records = records[:-1]
        CTqSdkAPI.set_data(records, symbol=f"{symbol}:{freq_sec}")

        if len(records) == 0:
            print(f"[{display_key}] ⑵ 无有效数据，跳过")
            return None

        t_chan = _time.time()

        _freq_to_kl = {
            15: KL_TYPE.K_15S, 30: KL_TYPE.K_30S, 60: KL_TYPE.K_1M,
            300: KL_TYPE.K_5M, 900: KL_TYPE.K_15M, 1800: KL_TYPE.K_30M,
            3600: KL_TYPE.K_60M, 86400: KL_TYPE.K_DAY,
            604800: KL_TYPE.K_WEEK, 2592000: KL_TYPE.K_MON,
        }
        kl_type = _freq_to_kl.get(freq_sec, KL_TYPE.K_15S)

        config = CChanConfig()

        chan = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )

        for _snapshot in chan.step_load():
            pass

        klines = api.get_kline_serial(symbol, freq_sec)

        if _SSE_DEBUG:
            print(f"[{display_key}] ⑵ 缠论分析: 消费 {len(records)}根K线, 耗时 {_time.time()-t_chan:.1f}s")
        return (chan, klines, kl_type, records)

    except Exception as e:
        import traceback
        print(f"[{display_key}] ⑵ 失败: {e}")
        traceback.print_exc()
        return None


def _drain_chan(chan):
    """驱动 chan 增量计算（耗尽 step_load 生成器）。

    原为数据源方法 CSSESource.step_load；彻底解耦业务后提升为服务层
    纯业务函数，生成器经本函数消耗引擎，数据源不再关心缠论计算。
    """
    for _snapshot in chan.step_load():
        pass


def _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None):
    """从 CChan 对象中提取缠论结构快照，格式与 /api/stock 一致。
    lightweight=True: 仅返回最后一根K线的OHLC变化（周期内tick更新用），不遍历全量结构。
    klines: 天勤实时K线DataFrame（lightweight=True时优先使用，避免chan框架kl_list滞后）"""
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
                    print(f"[警告] 异常: {type(e).__name__}: {e}")
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
    else:
        for k in klines_out:
            k["dif"] = 0; k["dea"] = 0; k["macd"] = 0

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
            # 左肩/右肩原始K线时间（用于双窗口红框定位）
            fx_a_raw_dt = ""
            fx_b_raw_dt = ""
            shoulder_times = _main_bi_range(bi, _date_fmt)
            if shoulder_times:
                fx_a_raw_dt, fx_b_raw_dt, _, _ = shoulder_times
            bis.append({
                "sdt": begin_klu.time.toFmtStr(_date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(_date_fmt) if end_klu else "",
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
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    fxs = []
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            peak_klu = klc.get_high_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(_date_fmt) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu else 0,
                "mark": "G", "price": klc.high, "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            peak_klu = klc.get_low_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(_date_fmt) if peak_klu else "",
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
                "sdt": begin_klu.time.toFmtStr(_date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(_date_fmt) if end_klu else "",
                "direction": direction,
                "begin_price": begin_price, "end_price": end_price,
                "high": round(seg._high(), 2), "low": round(seg._low(), 2),
                "amp": round(seg.amp(), 2),
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_list = []
    for zs in kl_list.zs_list:
        try:
            zs_list.append({
                "sdt": zs.begin.time.toFmtStr(_date_fmt) if zs.begin and hasattr(zs.begin, 'time') else "",
                "edt": zs.end.time.toFmtStr(_date_fmt) if zs.end and hasattr(zs.end, 'time') else "",
                "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, _date_fmt),
                "zg": round(zs.high, 2), "zd": round(zs.low, 2),
                "gg": round(zs.peak_high, 2), "dd": round(zs.peak_low, 2),
                "dir": "up" if (zs.bi_in and zs.bi_in.is_up()) else "down",
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_stars = []
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        star_date = begin_klu.time.toFmtStr(_date_fmt)
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "D", "color": "red"})
        else:
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "G", "color": "green"})

    bsps = []
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            bsps.append({
                "date": bsp.klu.time.toFmtStr(_date_fmt),
                "timestamp": int(bsp.klu.time.ts * 1000),
                "type": bsp.type2str(), "is_buy": bsp.is_buy,
                "price": round(bsp.klu.close, 3),
                "high": round(bsp.klu.high, 3),
                "low": round(bsp.klu.low, 3),
            })
    except Exception as e:
        print(f"[警告] 异常: {type(e).__name__}: {e}")

    return {
        "meta": {
            "symbol": symbol, "name": name, "freq": _meta_freq_label,
            "kline_count": len(klines_out), "bi_count": len(bis),
            "fx_count": len(fxs), "zs_count": len(zs_list),
            "seg_count": len(segs), "bsp_count": len(bsps),
            "generated_at": datetime.now().strftime(_date_fmt),
            "is_realtime": True, "is_replay": False, "market": "futures",
            "saved_selection_date": saved_selection_date,
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
        full_records = CTqSdkAPI.fetch_kline(_src.api, code, freq_sec=freq_sec, display_key=_display_key)
    except Exception as _e:
        print(f"[期货][错误] 天勤拉取K线失败: {type(_e).__name__}: {_e}")
        return {"error": f"天勤拉取K线失败: {type(_e).__name__}: {_e}"}
    finally:
        if _src is not None:
            try:
                _src.close()
                _src.close_api()
            except Exception as _e:
                print(f"[警告] 关闭天勤连接异常: {type(_e).__name__}: {_e}")
    print(f"[拉取] ⑴ 天勤拉取K线: {time.time()-t_fetch:.3f}s, {len(full_records)}条")
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
                        print(f"[futures][箭头] step={step}: {full_records[anchor_idx]['dt']} → {target_dt} (idx {anchor_idx} → {new_idx})")
                    else:
                        print(f"[futures][箭头] step={step} 越界: idx {anchor_idx} → {new_idx}, 共{len(full_records)}条")

        records = [r for r in full_records if r["dt"] <= target_dt]
        if len(records) < 5:
            return {"error": f"截断后K线数据不足: 仅{len(records)}条"}
    else:
        records = full_records

    # 3. 注入数据源
    t_set = time.time()
    CTqSdkAPI.set_data(records, symbol=code)
    print(f"[分析] ⑵ 注入数据源: {time.time()-t_set:.3f}s")

    # 4. 获取品种名称
    stock_name = _get_futures_name(code)

    # 5. 创建 CChan 并消费
    t0 = time.time()
    config = _make_chan_config()

    # 每次请求重置复盘标记，避免残留前一次状态
    CMyBSPointList.REPLAY_MODE = False

    try:
        if end_date:
            CMyBSPointList.REPLAY_MODE = True
        chan = CChan(
            code=code,
            begin_time=None,
            end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[_get_kl_type(freq)],
            config=config,
            autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan.step_load():
            pass
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        records_info = ""
        if records:
            records_info = f" records={len(records)}条 [{records[0]['dt']} ~ {records[-1]['dt']}]"
        print(f"[期货][错误] chan.py 分析失败: code={code} freq={freq}{records_info}")
        print(f"[期货][错误] 异常类型: {type(e).__name__}, 异常信息: {e}")
        print(f"[期货][错误] 完整堆栈:\n{tb}")
        return {"error": f"chan.py 期货分析失败: {type(e).__name__}: {e}"}
    finally:
        if end_date:
            CMyBSPointList.REPLAY_MODE = False

    kl_list = chan[_get_kl_type(freq)]
    print(f"[分析] ⑶ chan.py分析: {time.time()-t0:.3f}s, 合并K线={len(kl_list.lst)}, 笔={len(kl_list.bi_list)}, 中枢={len(kl_list.zs_list)}")

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

    # 笔、线段、中枢、买卖点提取（与股票逻辑完全一致）
    bi_data, fx_data, seg_data, zs_data, zs_stars, bsp_data, white_hline = [], [], [], [], [], [], None

    # 提取笔（字段与股票代码完全一致）
    for bi in kl_list.bi_list:
        try:
            direction = "up" if bi.is_up() else "down"
            begin_val = bi.get_begin_val()
            end_val = bi.get_end_val()
            power = abs(end_val - begin_val)
            begin_klu = bi.get_begin_klu()
            end_klu = bi.get_end_klu()
            sdt_str = begin_klu.time.toFmtStr(date_fmt) if begin_klu else ""
            edt_str = end_klu.time.toFmtStr(date_fmt) if end_klu else ""
            try:
                sdt_ts = int(begin_klu.time.ts * 1000) if begin_klu else 0
            except:
                sdt_ts = 0
            try:
                edt_ts = int(end_klu.time.ts * 1000) if end_klu else 0
            except:
                edt_ts = 0

            # 分型索引（在 kl_list.lst 中定位）
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

            # 分型肩部原始K线时间
            # chan.py: begin_klc 是分型中间KLC（2），begin_klc.pre 是左肩KLC（1），begin_klc.next 是右肩KLC（3）
            # 左肩KLC可能合并了多根原始K线，取第一根 = A
            # 右肩KLC可能合并了多根原始K线，取最后一根 = B
            fx_a_raw_dt = ""
            fx_b_raw_dt = ""
            a_klu = None
            b_klu = None
            try:
                begin_klc = bi.begin_klc
                end_klc = bi.end_klc
                # A: 左肩第一根原始K线 = begin_klc.pre.lst[0]
                left_shoulder_klc = begin_klc.pre if begin_klc else None
                if left_shoulder_klc and left_shoulder_klc.lst:
                    a_klu = left_shoulder_klc.lst[0]
                if a_klu:
                    fx_a_raw_dt = a_klu.time.toFmtStr(date_fmt)
                # B: 右肩最后一根原始K线 = end_klc.next.lst[-1]
                right_shoulder_klc = end_klc.next if end_klc else None
                if right_shoulder_klc and right_shoulder_klc.lst:
                    b_klu = right_shoulder_klc.lst[-1]
                if b_klu:
                    fx_b_raw_dt = b_klu.time.toFmtStr(date_fmt)
            except Exception as _e:
                print(f"[警告] 异常: {type(_e).__name__}: {_e}")

            bi_data.append({
                "idx": bi.idx,
                "sdt": sdt_str, "edt": edt_str,
                "sdt_ts": sdt_ts, "edt_ts": edt_ts,
                "direction": direction,
                "fx_a_price": round(begin_val, 2),
                "fx_b_price": round(end_val, 2),
                "high": round(bi._high(), 2),
                "low": round(bi._low(), 2),
                "power": round(power, 2),
                "is_sure": getattr(bi, 'is_sure', True),
                "end_fx_idx": end_fx_idx,
                "begin_fx_idx": begin_fx_idx,
                "fx_a_raw_dt": fx_a_raw_dt,
                "fx_b_raw_dt": fx_b_raw_dt,
                "fx_a_sub_dt": "",
                "fx_b_sub_dt": "",
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    # 提取分型（与股票路径一致）
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            mark = "G"
            price = klc.high
            klu = klc.get_high_peak_klu()
            fx_date = klu.time.toFmtStr(date_fmt) if klu else ""
            fx_data.append({
                "date": fx_date,
                "timestamp": int(klu.time.ts * 1000) if klu else 0,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            mark = "D"
            price = klc.low
            klu = klc.get_low_peak_klu()
            fx_date = klu.time.toFmtStr(date_fmt) if klu else ""
            fx_data.append({
                "date": fx_date,
                "timestamp": int(klu.time.ts * 1000) if klu else 0,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })

    # 提取线段（与股票代码完全一致）
    for seg in kl_list.seg_list:
        try:
            direction = "up" if seg.is_up() else "down"
            begin_klu = seg.get_begin_klu()
            end_klu = seg.get_end_klu()
            sdt = begin_klu.time.toFmtStr(date_fmt) if begin_klu else ""
            edt = end_klu.time.toFmtStr(date_fmt) if end_klu else ""
            if direction == "up":
                begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
                end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
            else:
                begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
                end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
            seg_data.append({
                "sdt": sdt, "edt": edt,
                "direction": direction,
                "begin_price": begin_price,
                "end_price": end_price,
                "high": round(seg._high(), 2),
                "low": round(seg._low(), 2),
                "amp": round(seg.amp(), 2),
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    for zs in kl_list.zs_list:
        try:
            zs_data.append({
                "sdt": zs.begin.time.toFmtStr(date_fmt),
                "edt": zs.end.time.toFmtStr(date_fmt),
                "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, date_fmt),
                "zg": round(zs.high, 2),
                "zd": round(zs.low, 2),
                "gg": round(zs.peak_high, 2),
                "dd": round(zs.peak_low, 2),
                "dir": "up" if zs.bi_in and zs.bi_in.is_up() else "down",
            })
        except Exception as e:
            print(f"[调试] 中枢提取失败: {type(e).__name__}: {e}")

    # 中枢五角星（与股票代码完全一致）
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
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "D",
                "color": "red",
            })
        else:
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "G",
                "color": "green",
            })

    # 提取买卖点（与股票路径一致）
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            klu = bsp.klu
            bsp_date = klu.time.toFmtStr(date_fmt)
            try:
                bsp_ts = int(datetime.strptime(bsp_date, date_fmt).timestamp()) * 1000
            except:
                bsp_ts = 0
            bsp_data.append({
                "date": bsp_date, "timestamp": bsp_ts,
                "type": bsp.type2str(),
                "is_buy": bsp.is_buy,
                "price": klu.close,
                "high": klu.high,
                "low": klu.low,
            })
    except Exception as e:
        print(f"[调试] 期货获取买卖点失败: {e}")

    # 计算白色横虚线（最新笔分型上下沿，K线确认后才有意义）
    white_hline = _calc_futures_white_hline(kl_list, freq, date_fmt)

    # 7. 组装结果
    print(f"[分析] ⑷ 提取结果(K线/笔/分型/线段/中枢/买卖点): {time.time()-t_extract:.3f}s")
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

    print(f"[信息] 期货查询 {code} 完成({_get_freq_label(freq)}): {len(kline_data)}K线, {len(bi_data)}笔, {len(fx_data)}分型, {len(zs_data)}中枢, {len(seg_data)}线段, {len(bsp_data)}买卖点")
    print(f"[耗时] 总耗时: {time.time()-t_start:.3f}s")

    # 双窗口模式：提取子级别数据（独立 CChan 对象）
    if dual:
        # 优先使用传入的 sub_freq（双窗口周期独立），否则用默认映射
        if not sub_freq:
            sub_freq = _FUTURES_DUAL_FREQ_MAP.get(freq)
        if not sub_freq:
            result["error"] = f"双窗口不支持当前周期: {freq}"
            return result
        sub_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(sub_freq, 15)
        print(f"[双窗口] 开始提取子级别({sub_freq})数据...")

        # 检查 existing_chan 是否匹配 sub_freq，匹配则复用
        if existing_chan is not None and existing_records is not None and freq == sub_freq:
            # existing_chan 匹配的是主周期，子周期需要新建
            pass
        elif existing_chan is not None and existing_records is not None and freq != sub_freq:
            # 如果 existing_chan 正好匹配 sub_freq，复用它
            # 这个情况发生在上窗周期!=单窗口周期时（暂不涉及，保留接口）
            pass

        # 拉取子级别历史K线
        t_sub_fetch = time.time()
        _sub_display_key = f"{code}:{CTqSdkAPI.FREQ_LABEL_CN.get(sub_freq, sub_freq)}"
        _api_sub = None
        sub_full_records = []
        try:
            _api_sub = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
            sub_full_records = CTqSdkAPI.fetch_kline(_api_sub, code, freq_sec=sub_freq_sec, display_key=_sub_display_key)
        except Exception as _e:
            print(f"[双窗口][错误] 子级别天勤拉取K线失败: {type(_e).__name__}: {_e}")
            return result
        finally:
            if _api_sub is not None:
                try:
                    _api_sub.close()
                except Exception as _e:
                    print(f"[警告] 关闭天勤子级别连接异常: {type(_e).__name__}: {_e}")
        print(f"[双窗口] 子级别({sub_freq})拉取K线: {time.time()-t_sub_fetch:.3f}s, {len(sub_full_records)}条")

        if len(sub_full_records) < 5:
            print(f"[双窗口] 子级别({sub_freq})数据不足，仅{len(sub_full_records)}条，跳过")
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
                print(f"[双窗口] 子级别({sub_freq})同步截断: {sub_before}条 -> {len(sub_full_records)}条")

        sub_records = sub_full_records

        # 注入子级别数据源
        sub_code = f"{code}:{sub_freq_sec}"
        CTqSdkAPI.set_data(sub_records, symbol=sub_code)

        # 创建子级别 CChan
        t_sub_chan = time.time()
        sub_config = _make_chan_config()
        try:
            sub_chan = CChan(
                code=sub_code,
                begin_time=None, end_time=None,
                data_src="custom:TqSdkAPI.CTqSdkAPI",
                lv_list=[_get_kl_type(sub_freq)],
                config=sub_config,
                autype=AUTYPE.NONE,
                market_type="futures",
            )
            for _snapshot in sub_chan.step_load():
                pass
        except Exception as e:
            print(f"[双窗口] 子级别({sub_freq}) chan.py 分析失败: {e}")
            return result

        _futures_analysis_cache[f"{code.upper()}:{sub_freq}"] = sub_chan
        sub_kl_list = sub_chan[_get_kl_type(sub_freq)]
        print(f"[双窗口] 子级别({sub_freq}) chan.py分析: {time.time()-t_sub_chan:.3f}s, "
              f"合并K线={len(sub_kl_list.lst)}, 笔={len(sub_kl_list.bi_list)}, 中枢={len(sub_kl_list.zs_list)}")

        # 提取子级别结果
        sub_name = _get_futures_name(code)
        sub_result = _extract_realtime_snapshot(
            sub_chan, _get_kl_type(sub_freq), code, sub_name,
            sub_freq, klines=None
        )
        result["sub"] = sub_result
        # 将 fx_a_raw_dt/fx_b_raw_dt（天勤K线开始时间）换算为子级别时间
        main_freq_sec = CTqSdkAPI.FREQ_SEC_MAP.get(freq, 60)
        _futures_red_range(result, main_freq_sec, sub_freq_sec, sub_freq)
        print(f"[双窗口] 子级别({sub_freq})提取完成: K线={sub_result['meta']['kline_count']}, "
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
        api = src.api
        print(f"[{display_key}] ⓪ 临时连接天勤(选点): 耗时 {time.time()-t_conn:.1f}s")

        records = CTqSdkAPI.fetch_kline(api, symbol, freq_sec=freq_sec, display_key=display_key)
        if len(records) < 5:
            return {"error": f"K线数据不足: 仅{len(records)}条"}

        # 注入数据源 + 创建 CChan
        CTqSdkAPI.set_data(records, symbol=f"{symbol}:{freq_sec}")

        from Common.CEnum import KL_TYPE, AUTYPE
        from Chan import CChan

        _freq_to_kl = {
            15: KL_TYPE.K_15S, 30: KL_TYPE.K_30S, 60: KL_TYPE.K_1M,
            300: KL_TYPE.K_5M, 900: KL_TYPE.K_15M, 1800: KL_TYPE.K_30M,
            3600: KL_TYPE.K_60M, 86400: KL_TYPE.K_DAY,
            604800: KL_TYPE.K_WEEK, 2592000: KL_TYPE.K_MON,
        }
        kl_type = _freq_to_kl.get(freq_sec, KL_TYPE.K_15S)

        config = _make_chan_config()

        chan = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan.step_load():
            pass

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

        print(f"[{display_key}] 选点左肩时间: {start_time}")

        # Step 3: 保存选点到CSV
        name = _get_futures_name(symbol)
        _save_point_time(symbol, name, freq, start_time)
        if symbol not in _saved_point_times:
            _saved_point_times[symbol] = {}
        _saved_point_times[symbol]["name"] = name
        _saved_point_times[symbol][FREQ_TO_COL.get(freq, "")] = start_time

        # Step 4: 关闭旧TqApi，创建新TqApi，从T重新拉取
        if src is not None:
            try:
                src.close()
                src.close_api()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
            src = None
            api = None

        t_conn2 = time.time()
        src2 = CTqSdkSession()
        src2.connect()
        api2 = src2.api
        print(f"[{display_key}] ⓪ 重新连接天勤(选点后): 耗时 {time.time()-t_conn2:.1f}s")

        records2 = CTqSdkAPI.fetch_kline(api2, symbol, freq_sec=freq_sec,
                                         display_key=display_key, start_time=start_time)
        if len(records2) < 5:
            return {"error": f"选点后K线数据不足: 仅{len(records2)}条"}

        # 注入数据源 + 创建新 CChan
        CTqSdkAPI.set_data(records2, symbol=f"{symbol}:{freq_sec}")

        chan2 = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan2.step_load():
            pass

        # Step 5: 提取快照并返回
        result = _extract_realtime_snapshot(chan2, kl_type, symbol, name, freq_label,
                                            saved_selection_date=start_time)
        # 计算白色横虚线
        _kl_list = chan2[kl_type]
        _date_fmt = _get_date_fmt(freq)
        result['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _date_fmt)
        print(f"[{display_key}] 选点完成: {len(result['klines'])}K线, {result['meta']['bi_count']}笔, {result['meta']['zs_count']}中枢")
        return result

    except Exception as e:
        import traceback
        print(f"[{display_key}] 选点异常: {e}")
        traceback.print_exc()
        return {"error": f"选点失败: {str(e)}"}
    finally:
        if src is not None:
            try:
                src.close()
                src.close_api()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
        if src2 is not None:
            try:
                src2.close()
                src2.close_api()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")


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
        print("[清理] 已清空期货K线缓存")

    # 2. 清空选点记录中的期货条目（key以KQ.开头）
    pts_to_del = [k for k in list(_saved_point_times.keys()) if k.startswith("KQ.")]
    for k in pts_to_del:
        del _saved_point_times[k]
    if pts_to_del:
        print(f"[清理] 已清除 {len(pts_to_del)} 条期货选点记录")


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

"""
TqSdkAPI - 天勤期货/期指行情数据源适配器
仿照 CTdxAPI 模式，使用 set_data() / append_bar() 注入数据供 chan.py 调用

v5: 每个 SSE 连接自包含——创建 TqApi → 拉历史 → chan分析 → 推送快照 → 实时循环。
d/w 周期因天勤主连合约返回垃圾数据，已排除并在前端禁用。
"""
import sys
import os
import json
import logging
import threading
import time as _time
from datetime import datetime, timedelta

# 抑制 tqsdk 内部 INFO 日志（如 WebSocket 连接通知）
# tqsdk 在 import 时会配置自己的 handler，需同时抑制 root logger
logging.root.setLevel(logging.WARNING)
logging.getLogger("tqsdk").setLevel(logging.WARNING)
logging.getLogger("shinny").setLevel(logging.WARNING)
logging.getLogger("tqsdk.tqapi").setLevel(logging.WARNING)

from DataAPI.CommonStockAPI import CCommonStockApi

# ============================================================
# 天勤配置
# ============================================================
TQ_ACCOUNT = "13521579214"
TQ_PASSWORD = "87654321"
_SSE_DEBUG = False  # SSE 推送详细调试日志开关（设为 True 可恢复调试输出）

# 默认监控的期货品种（引擎启动时初始化 15s/1m/5m，30m 延迟按需初始化）
# 注意：天勤主连合约不支持 d/w 周期（返回垃圾数据），已排除。
# 格式: (天勤合约代码, 显示名称, 周期秒数, 周期标签)
DEFAULT_FUTURES_SYMBOLS = [
    ("KQ.m@CFFEX.IM", "中证1000主连", 15, "15s"),
    ("KQ.m@CFFEX.IM", "中证1000主连", 60, "1m"),
    ("KQ.m@CFFEX.IM", "中证1000主连", 300, "5m"),
]

# ===== 期货品种别名映射表 =====
# 支持用户直接输入短名称（如 PTA、IF、rb、TA 等），自动映射到完整的主连代码
FUTURES_ALIASES = {
    # ===== 中金所 CFFEX =====
    "IF": "KQ.m@CFFEX.IF", "IH": "KQ.m@CFFEX.IH", "IC": "KQ.m@CFFEX.IC",
    "IM": "KQ.m@CFFEX.IM", "T": "KQ.m@CFFEX.T", "TF": "KQ.m@CFFEX.TF",
    "TL": "KQ.m@CFFEX.TL", "TS": "KQ.m@CFFEX.TS",
    # ===== 上期所 SHFE =====
    "CU": "KQ.m@SHFE.cu", "AL": "KQ.m@SHFE.al", "ZN": "KQ.m@SHFE.zn",
    "PB": "KQ.m@SHFE.pb", "NI": "KQ.m@SHFE.ni", "SN": "KQ.m@SHFE.sn",
    "AO": "KQ.m@SHFE.ao", "AU": "KQ.m@SHFE.au", "AG": "KQ.m@SHFE.ag",
    "RB": "KQ.m@SHFE.rb", "WR": "KQ.m@SHFE.wr", "HC": "KQ.m@SHFE.hc",
    "SS": "KQ.m@SHFE.ss", "BU": "KQ.m@SHFE.bu", "RU": "KQ.m@SHFE.ru",
    "FU": "KQ.m@SHFE.fu", "SP": "KQ.m@SHFE.sp", "BR": "KQ.m@SHFE.br",
    # ===== 大商所 DCE =====
    "M": "KQ.m@DCE.m", "Y": "KQ.m@DCE.y", "A": "KQ.m@DCE.a",
    "B": "KQ.m@DCE.b", "P": "KQ.m@DCE.p", "J": "KQ.m@DCE.j",
    "JM": "KQ.m@DCE.jm", "I": "KQ.m@DCE.i", "C": "KQ.m@DCE.c",
    "CS": "KQ.m@DCE.cs", "L": "KQ.m@DCE.l", "V": "KQ.m@DCE.v",
    "PP": "KQ.m@DCE.pp", "EG": "KQ.m@DCE.eg", "EB": "KQ.m@DCE.eb",
    "PG": "KQ.m@DCE.pg", "FB": "KQ.m@DCE.fb", "BB": "KQ.m@DCE.bb",
    "RR": "KQ.m@DCE.rr", "LH": "KQ.m@DCE.lh", "JD": "KQ.m@DCE.jd",
    # ===== 郑商所 CZCE =====
    "TA": "KQ.m@CZCE.TA", "PTA": "KQ.m@CZCE.TA", "MA": "KQ.m@CZCE.MA",
    "FG": "KQ.m@CZCE.FG", "SA": "KQ.m@CZCE.SA", "SR": "KQ.m@CZCE.SR",
    "CF": "KQ.m@CZCE.CF", "CY": "KQ.m@CZCE.CY", "OI": "KQ.m@CZCE.OI",
    "RM": "KQ.m@CZCE.RM", "ZC": "KQ.m@CZCE.ZC", "UR": "KQ.m@CZCE.UR",
    "PF": "KQ.m@CZCE.PF", "PK": "KQ.m@CZCE.PK", "AP": "KQ.m@CZCE.AP",
    "CJ": "KQ.m@CZCE.CJ", "SM": "KQ.m@CZCE.SM", "SF": "KQ.m@CZCE.SF",
    "SH": "KQ.m@CZCE.SH", "PX": "KQ.m@CZCE.PX", "LR": "KQ.m@CZCE.LR",
    "RI": "KQ.m@CZCE.RI", "JR": "KQ.m@CZCE.JR", "WH": "KQ.m@CZCE.WH",
    "PM": "KQ.m@CZCE.PM", "RS": "KQ.m@CZCE.RS",
    # ===== 上海国际能源交易中心 INE =====
    "SC": "KQ.m@INE.sc", "LU": "KQ.m@INE.lu", "NR": "KQ.m@INE.nr",
    "BC": "KQ.m@INE.bc", "EC": "KQ.m@INE.ec",
    # ===== 广期所 GFEX =====
    "SI": "KQ.m@GFEX.si", "LC": "KQ.m@GFEX.lc", "PS": "KQ.m@GFEX.ps",
}

# 前端可用的周期列表（用于变灰不可用按钮）
SUPPORTED_FREQS = ["15s", "1m", "5m", "30m"]
DISABLED_FREQS = ["d", "w"]

# 天勤K线周期映射：标签 -> 秒数
FREQ_SEC_MAP = {
    "15s": 15, "30s": 30, "1m": 60, "3m": 180, "5m": 300,
    "15m": 900, "30m": 1800, "60m": 3600, "d": 86400,
    "w": 604800, "M": 2592000,
}

SEC_TO_LABEL = {v: k for k, v in FREQ_SEC_MAP.items()}

# 周期标签中文名（用于打印日志）
FREQ_LABEL_CN = {
    "15s": "15秒", "30s": "30秒", "1m": "1分", "3m": "3分", "5m": "5分",
    "15m": "15分", "30m": "30分", "60m": "60分", "d": "日线", "w": "周线", "M": "月线",
}

# 历史数据回看条数
HISTORY_LOOKBACK_BARS = {
    15: 2000,      # 15秒   2个交易日
    60: 1000,      # 1分钟   4个交易日
    300: 500,      # 5分钟   10个交易日
    1800: 300,     # 30分钟  37个交易日
}

# 期货双窗口周期映射：上窗周期 → 下窗周期
FUTURES_DUAL_FREQ_MAP = {
    "30m": "5m",
    "5m": "1m",
    "1m": "15s",
}

# 期货双窗口反向映射：下窗周期 → 上窗周期
FUTURES_DUAL_REVERSE_MAP = {
    "5m": "30m",
    "1m": "5m",
    "15s": "1m",
}


class CTqSdkAPI(CCommonStockApi):
    """
    天勤数据源适配器，继承 CCommonStockApi 实现完整接口。
    缓存键为 "symbol:freq_sec" 格式，同品种不同周期各自独立。
    """
    _records_by_symbol = {}
    _lock = threading.Lock()

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass

    @classmethod
    def clear_all_cache(cls):
        """期货切股票时清空所有K线缓存"""
        with cls._lock:
            cls._records_by_symbol.clear()

    @classmethod
    def set_data(cls, records, symbol=None):
        key = symbol or "__default__"
        with cls._lock:
            cls._records_by_symbol[key] = list(records)

    @classmethod
    def append_bar(cls, bar, symbol=None):
        key = symbol or "__default__"
        with cls._lock:
            if key not in cls._records_by_symbol:
                cls._records_by_symbol[key] = []
            cls._records_by_symbol[key].append(bar)

    @classmethod
    def get_data(cls, symbol=None, **kwargs):
        key = symbol or "__default__"
        with cls._lock:
            return cls._records_by_symbol.get(key, []).copy()

    @classmethod
    def get_last_n(cls, n=1, symbol=None):
        key = symbol or "__default__"
        with cls._lock:
            records = cls._records_by_symbol.get(key, [])
            return records[-n:] if len(records) >= n else records.copy()

    def __init__(self, code, k_type, begin_date, end_date, autype):
        self.code = code
        self.name = code
        self.is_stock = False
        self.k_type = k_type
        self.begin_date = begin_date
        self.begin_time = None
        self.end_date = end_date
        self.end_time = None
        self.autype = autype

    def SetBasciInfo(self):
        self.name = self.code
        self.is_stock = False

    def get_kl_data(self):
        from KLine.KLine_Unit import CKLine_Unit
        from Common.CEnum import DATA_FIELD
        from Common.CTime import CTime

        with self._lock:
            records = list(self._records_by_symbol.get(self.code, []))
            if not records:
                records = list(self._records_by_symbol.get("__default__", []))

        for r in records:
            dt = r.get("dt")
            if dt is None:
                continue
            try:
                ct = CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
            except OSError:
                continue

            kl_dict = {
                DATA_FIELD.FIELD_TIME: ct,
                DATA_FIELD.FIELD_OPEN: r.get("open", 0),
                DATA_FIELD.FIELD_HIGH: r.get("high", 0),
                DATA_FIELD.FIELD_LOW: r.get("low", 0),
                DATA_FIELD.FIELD_CLOSE: r.get("close", 0),
                DATA_FIELD.FIELD_VOLUME: r.get("vol", 0),
                DATA_FIELD.FIELD_TURNOVER: r.get("amount", 0),
            }
            try:
                yield CKLine_Unit(kl_dict, autofix=True)
            except Exception:
                continue


# ============================================================
# 天勤行情获取
# ============================================================


def fetch_futures_kline(api, symbol, freq_sec=15, num_bars=None, display_key=None, start_time=None):
    """
    从天勤拉取历史 K 线数据，转换为 records 格式。
    api: TqApi 实例（由调用方创建和传入）
    start_time: 选点起始时间字符串（如 "2026-01-09 10:00"），有值时只返回该时间之后的K线
    """
    t_start = _time.time()
    if num_bars is None:
        num_bars = HISTORY_LOOKBACK_BARS.get(freq_sec, 300)

    klines = api.get_kline_serial(symbol, freq_sec, num_bars)

    # 等待数据加载（主动调用 wait_update 拉取数据）
    waited = 0
    while len(klines) < min(50, num_bars // 5) and waited < 30:
        api.wait_update(deadline=_time.time() * 1e9 + 500_000_000)
        waited += 1

    records = []
    import pandas as pd
    for _, row in klines.iterrows():
        ts = row.get("datetime")
        if ts is None or pd.isna(ts):
            continue
        if isinstance(ts, (int, float)):
            # 跳过无效时间戳：<=0 或 NaN（天勤未返回数据时填充为 0）
            if ts <= 0 or pd.isna(ts):
                continue
            try:
                dt = datetime.fromtimestamp(ts / 1e9)
            except (ValueError, OSError):
                continue
        elif hasattr(ts, 'to_pydatetime'):
            dt = ts.to_pydatetime()
        else:
            continue

        o = float(row.get("open", 0) or 0)
        h = float(row.get("high", 0) or 0)
        l = float(row.get("low", 0) or 0)
        c = float(row.get("close", 0) or 0)
        vol = int(row.get("volume", 0) or 0)
        oi = float(row.get("open_oi", 0) or 0)

        # 跳过全零 OHLC（天勤未返回有效数据）
        if o == 0 and h == 0 and l == 0 and c == 0:
            continue

        h = max(h, o, c)
        l = min(l, o, c)

        records.append({
            "dt": dt,
            "open": round(o, 3),
            "high": round(h, 3),
            "low": round(l, 3),
            "close": round(c, 3),
            "vol": vol,
            "amount": round(oi, 2),
        })

    # 如果有选点时间，过滤记录
    if start_time is not None and records:
        start_dt = None
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
            try:
                start_dt = datetime.strptime(start_time, fmt)
                break
            except ValueError:
                continue
        if start_dt is not None:
            before_count = len(records)
            records = [r for r in records if r["dt"] >= start_dt]
            if before_count != len(records):
                print(f"[{display_key or symbol}] 选点过滤: {start_time} -> {before_count}条 → {len(records)}条")

    # ★ 天勤免费API可能返回超过num_bars的数据，强制截断以控制step_load耗时
    if len(records) > num_bars:
        records = records[-num_bars:]

    label = SEC_TO_LABEL.get(freq_sec, f"{freq_sec}s")
    prefix = display_key if display_key else f"{symbol} {label}"
    elapsed = _time.time() - t_start
    if _SSE_DEBUG:
        if records:
            print(f"[{prefix}] ⑴ 拉取历史: {len(records)}根, "
                  f"{records[0]['dt'].strftime('%Y-%m-%d %H:%M:%S')} ~ "
                  f"{records[-1]['dt'].strftime('%Y-%m-%d %H:%M:%S')}, "
                  f"耗时 {elapsed:.1f}s")
        else:
            print(f"[{prefix}] ⑴ 拉取历史: 0条 (无有效数据), 耗时 {elapsed:.1f}s")
    return records


# ============================================================
# chan 初始化（拉取历史 + 缠论分析，每个 SSE 连接自包含）
# ============================================================


def init_chan_symbol(api, symbol, name, freq_sec, freq_label, start_time=None):
    """拉取历史K线 + 运行 chan.py 分析，返回 (chan, klines, kl_type, records) 或 None。
    由 SSE handler 调用，每个 SSE 连接自包含。
    start_time: 选点起始时间，有值时只拉取该时间之后的K线"""
    from Common.CEnum import KL_TYPE, AUTYPE
    from Chan import CChan
    from ChanConfig import CChanConfig

    display_label = FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{display_label}"

    try:
        records = fetch_futures_kline(api, symbol, freq_sec=freq_sec, display_key=display_key, start_time=start_time)
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


# ============================================================
# 工具函数（chan 实体提取）
# ============================================================

def _fmt_date(ctime):
    if ctime is None:
        return ""
    return datetime.fromtimestamp(ctime.ts).strftime("%Y-%m-%d %H:%M:%S")


def _bi_overlap_range(bi, zg, zd):
    return min(zg, bi._high()) > max(zd, bi._low())


def _format_bi_edt(bi):
    klu = bi.get_end_klu()
    return _fmt_date(klu.time) if klu else ""


def _calc_zs_confirm_edt_from_bis(zs_obj, all_bi_list):
    try:
        end_idx = zs_obj.end_bi.idx
        zg, zd = zs_obj.high, zs_obj.low
    except Exception:
        return ""
    for bi in all_bi_list[end_idx + 1:]:
        if _bi_overlap_range(bi, zg, zd):
            continue
        if getattr(bi, "next", None) is None:
            return ""
        return _format_bi_edt(bi)
    return ""


def _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None):
    """从 CChan 对象中提取缠论结构快照，格式与 /api/stock 一致。
    lightweight=True: 仅返回最后一根K线的OHLC变化（周期内tick更新用），不遍历全量结构。
    klines: 天勤实时K线DataFrame（lightweight=True时优先使用，避免chan框架kl_list滞后）"""
    from Common.CEnum import FX_TYPE
    kl_list = chan[kl_type]

    if lightweight:
        # ★ 优先从天勤实时 klines 读取当前形成中K线的OHLC，避免 chan 框架 kl_list 滞后
        if klines is not None and len(klines) > 0:
            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            kline_dt = "?"
            if dt_ns is not None:
                try:
                    kline_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
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
                    "symbol": symbol, "name": name, "freq": freq_label,
                    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "is_realtime": True, "market": "futures",
                },
            }
        # 回退：无 klines 时从 chan 框架读取
        if len(kl_list.lst) == 0:
            return None
        last_klc = kl_list.lst[-1]
        if len(last_klc.lst) == 0:
            return None
        last_klu = last_klc.lst[-1]
        return {
            "type": "tick",
            "kline": {
                "date": _fmt_date(last_klu.time),
                "open": round(last_klu.open, 3),
                "high": round(last_klu.high, 3),
                "low": round(last_klu.low, 3),
                "close": round(last_klu.close, 3),
            },
            "meta": {
                "symbol": symbol, "name": name, "freq": freq_label,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "is_realtime": True, "market": "futures",
            },
        }

    klines = []
    for klc in kl_list.lst:
        for klu in klc.lst:
            t = klu.time
            klines.append({
                "date": _fmt_date(t),
                "timestamp": int(t.ts * 1000) if hasattr(t, 'ts') else 0,
                "open": round(klu.open, 3),
                "high": round(klu.high, 3),
                "low": round(klu.low, 3),
                "close": round(klu.close, 3),
                "vol": int(klu.trade_info.metric.get("volume", 0) or 0),
                "amount": round(klu.trade_info.metric.get("turnover", 0) or 0, 2),
            })

    closes = [k["close"] for k in klines]
    if len(closes) >= 26:
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        dif = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        dea = _ema(dif, 9)
        macd_vals = [2 * (d - a) for d, a in zip(dif, dea)]
        for i in range(len(klines)):
            if i < len(dif):
                klines[i]["dif"] = round(dif[i], 4)
                klines[i]["dea"] = round(dea[i], 4)
                klines[i]["macd"] = round(macd_vals[i], 4)
            else:
                klines[i]["dif"] = 0; klines[i]["dea"] = 0; klines[i]["macd"] = 0
    else:
        for k in klines:
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
            try:
                begin_klc = bi.begin_klc
                end_klc = bi.end_klc
                left_shoulder_klc = begin_klc.pre if begin_klc else None
                if left_shoulder_klc and left_shoulder_klc.lst:
                    a_klu = left_shoulder_klc.lst[0]
                    fx_a_raw_dt = _fmt_date(a_klu.time)
                right_shoulder_klc = end_klc.next if end_klc else None
                if right_shoulder_klc and right_shoulder_klc.lst:
                    b_klu = right_shoulder_klc.lst[-1]
                    fx_b_raw_dt = _fmt_date(b_klu.time)
            except Exception:
                pass
            bis.append({
                "sdt": _fmt_date(begin_klu.time) if begin_klu else "",
                "edt": _fmt_date(end_klu.time) if end_klu else "",
                "sdt_ts": int(begin_klu.time.ts * 1000) if begin_klu and hasattr(begin_klu.time, 'ts') else 0,
                "edt_ts": int(end_klu.time.ts * 1000) if end_klu and hasattr(end_klu.time, 'ts') else 0,
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
        except Exception:
            pass

    fxs = []
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            peak_klu = klc.get_high_peak_klu()
            fxs.append({
                "date": _fmt_date(peak_klu.time) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu and hasattr(peak_klu.time, 'ts') else 0,
                "mark": "G", "price": klc.high, "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            peak_klu = klc.get_low_peak_klu()
            fxs.append({
                "date": _fmt_date(peak_klu.time) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu and hasattr(peak_klu.time, 'ts') else 0,
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
                "sdt": _fmt_date(begin_klu.time) if begin_klu else "",
                "edt": _fmt_date(end_klu.time) if end_klu else "",
                "direction": direction,
                "begin_price": begin_price, "end_price": end_price,
                "high": round(seg._high(), 2), "low": round(seg._low(), 2),
                "amp": round(seg.amp(), 2),
            })
        except Exception:
            pass

    zs_list = []
    for zs in kl_list.zs_list:
        try:
            zs_list.append({
                "sdt": _fmt_date(zs.begin.time) if zs.begin and hasattr(zs.begin, 'time') else "",
                "edt": _fmt_date(zs.end.time) if zs.end and hasattr(zs.end, 'time') else "",
                "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list),
                "zg": round(zs.high, 2), "zd": round(zs.low, 2),
                "gg": round(zs.peak_high, 2), "dd": round(zs.peak_low, 2),
                "dir": "up" if (zs.bi_in and zs.bi_in.is_up()) else "down",
            })
        except Exception:
            pass

    zs_stars = []
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        star_date = _fmt_date(begin_klu.time)
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
                "date": _fmt_date(bsp.klu.time),
                "timestamp": int(bsp.klu.time.ts * 1000) if hasattr(bsp.klu.time, 'ts') else 0,
                "type": bsp.type2str(), "is_buy": bsp.is_buy,
                "price": round(bsp.klu.close, 3),
                "high": round(bsp.klu.high, 3),
                "low": round(bsp.klu.low, 3),
            })
    except Exception:
        pass

    return {
        "meta": {
            "symbol": symbol, "name": name, "freq": freq_label,
            "kline_count": len(klines), "bi_count": len(bis),
            "fx_count": len(fxs), "zs_count": len(zs_list),
            "seg_count": len(segs), "bsp_count": len(bsps),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_realtime": True, "market": "futures",
            "saved_selection_date": saved_selection_date,
        },
        "klines": klines, "bis": bis, "fxs": fxs, "segs": segs,
        "zs": zs_list, "zs_stars": zs_stars, "bsps": bsps, "white_hline": None,
    }


def _ema(data, period):
    result = []
    k = 2.0 / (period + 1)
    for i, val in enumerate(data):
        if i == 0:
            result.append(val)
        else:
            result.append(val * k + result[-1] * (1 - k))
    return result

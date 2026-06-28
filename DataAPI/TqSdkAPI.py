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
TQ_ACCOUNT = ""
TQ_PASSWORD = ""
_SSE_DEBUG = False  # SSE 推送详细调试日志开关（设为 True 可恢复调试输出）


def load_tq_account(config_dir):
    """
    从 {config_dir}/tq_account.json 文件读取天勤账户和密码。
    文件格式: {"account": "手机号或用户名", "password": "密码"}

    读取成功则更新模块级 TQ_ACCOUNT / TQ_PASSWORD；
    文件不存在或格式错误则保持默认空值，由调用方决定是否回退到硬编码。
    """
    global TQ_ACCOUNT, TQ_PASSWORD
    account_file = os.path.join(config_dir, "tq_account.json")
    if not os.path.exists(account_file):
        print(f"[TqSdkAPI] 账户文件不存在: {account_file}，使用默认空值")
        return False
    try:
        with open(account_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            TQ_ACCOUNT = str(data.get("account", "")).strip()
            TQ_PASSWORD = str(data.get("password", "")).strip()
            if TQ_ACCOUNT and TQ_PASSWORD:
                print(f"[TqSdkAPI] 已从文件加载天勤账户: {account_file}")
                return True
            else:
                print(f"[TqSdkAPI] 账户文件内容不完整，请检查 account 和 password 字段: {account_file}")
                return False
        else:
            print(f"[TqSdkAPI] 账户文件格式错误，应为 JSON 对象: {account_file}")
            return False
    except json.JSONDecodeError as e:
        print(f"[TqSdkAPI] 账户文件 JSON 解析失败: {account_file}, 错误: {e}")
        return False
    except Exception as e:
        print(f"[TqSdkAPI] 读取账户文件失败: {account_file}, 错误: {e}")
        return False

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
# 期货代码/名称工具函数
# ============================================================


def _get_futures_code(code):
    """识别期货/期指代码，返回天勤标准代码；非期货返回 None。"""
    # 格式: EXCHANGE.SYMBOL (如 CFFEX.IM2507, SHFE.rb2505)
    # 或主连: KQ.m@EXCHANGE.SYMBOL, KQ.i@EXCHANGE.SYMBOL
    # 注意：天勤要求 KQ 前缀中的 m/i 小写，所以先 upper() 匹配，再恢复小写
    FUTURE_EXCHANGES = ['CFFEX', 'SHFE', 'DCE', 'CZCE', 'INE', 'GFEX']
    for ex in FUTURE_EXCHANGES:
        if code.startswith(ex + '.'):
            return code
        if code.startswith('KQ.M@' + ex + '.'):
            return code.replace('KQ.M@', 'KQ.m@', 1)
        if code.startswith('KQ.I@' + ex + '.'):
            return code.replace('KQ.I@', 'KQ.i@', 1)

    # 期货别名映射：支持直接输入短名称（如 PTA、IF、rb、TA 等）
    if code in FUTURES_ALIASES:
        return FUTURES_ALIASES[code]
    return None


def _get_futures_name(code):
    """获取期货品种的中文名称"""
    FUTURES_NAMES = {
        "KQ.m@CFFEX.IF": "沪深300主连", "KQ.m@CFFEX.IH": "上证50主连",
        "KQ.m@CFFEX.IC": "中证500主连", "KQ.m@CFFEX.IM": "中证1000主连",
        "KQ.m@CFFEX.T":  "10年国债主连", "KQ.m@CFFEX.TF": "5年国债主连",
        "KQ.m@CFFEX.TL": "30年国债主连", "KQ.m@CFFEX.TS": "2年国债主连",
        "KQ.m@SHFE.rb":  "螺纹钢主连", "KQ.m@SHFE.au": "沪金主连",
        "KQ.m@SHFE.ag":  "沪银主连", "KQ.m@SHFE.cu": "沪铜主连",
        "KQ.m@SHFE.al":  "沪铝主连", "KQ.m@SHFE.zn": "沪锌主连",
        "KQ.m@SHFE.ni":  "沪镍主连", "KQ.m@SHFE.ru": "橡胶主连",
        "KQ.m@SHFE.bu":  "沥青主连", "KQ.m@SHFE.fu": "燃油主连",
        "KQ.m@SHFE.sp":  "纸浆主连", "KQ.m@SHFE.hc": "热卷主连",
        "KQ.m@SHFE.ss":  "不锈钢主连", "KQ.m@SHFE.sn": "沪锡主连",
        "KQ.m@SHFE.pb":  "沪铅主连", "KQ.m@SHFE.wr": "线材主连",
        "KQ.m@SHFE.ao":  "氧化铝主连", "KQ.m@SHFE.br": "丁二烯主连",
        "KQ.m@DCE.m":    "豆粕主连", "KQ.m@DCE.y": "豆油主连",
        "KQ.m@DCE.a":    "豆一主连", "KQ.m@DCE.b": "豆二主连",
        "KQ.m@DCE.p":    "棕榈油主连", "KQ.m@DCE.j": "焦炭主连",
        "KQ.m@DCE.jm":   "焦煤主连", "KQ.m@DCE.i": "铁矿石主连",
        "KQ.m@DCE.c":    "玉米主连", "KQ.m@DCE.cs": "淀粉主连",
        "KQ.m@DCE.l":    "塑料主连", "KQ.m@DCE.v": "PVC主连",
        "KQ.m@DCE.pp":   "PP主连", "KQ.m@DCE.eg": "乙二醇主连",
        "KQ.m@DCE.eb":   "苯乙烯主连", "KQ.m@DCE.pg": "LPG主连",
        "KQ.m@DCE.fb":   "纤维板主连", "KQ.m@DCE.bb": "胶合板主连",
        "KQ.m@DCE.rr":   "粳米主连", "KQ.m@DCE.lh": "生猪主连",
        "KQ.m@DCE.jd":   "鸡蛋主连", "KQ.m@CZCE.TA": "PTA主连",
        "KQ.m@CZCE.MA":  "甲醇主连", "KQ.m@CZCE.FG": "玻璃主连",
        "KQ.m@CZCE.SA":  "纯碱主连", "KQ.m@CZCE.SR": "白糖主连",
        "KQ.m@CZCE.CF":  "棉花主连", "KQ.m@CZCE.CY": "棉纱主连",
        "KQ.m@CZCE.OI":  "菜油主连", "KQ.m@CZCE.RM": "菜粕主连",
        "KQ.m@CZCE.ZC":  "动力煤主连", "KQ.m@CZCE.UR": "尿素主连",
        "KQ.m@CZCE.PF":  "短纤主连", "KQ.m@CZCE.PK": "花生主连",
        "KQ.m@CZCE.AP":  "苹果主连", "KQ.m@CZCE.CJ": "红枣主连",
        "KQ.m@CZCE.SM":  "锰硅主连", "KQ.m@CZCE.SF": "硅铁主连",
        "KQ.m@CZCE.SH":  "烧碱主连", "KQ.m@CZCE.PX": "对二甲苯主连",
        "KQ.m@CZCE.LR":  "晚籼稻主连", "KQ.m@CZCE.RI": "早籼稻主连",
        "KQ.m@CZCE.JR":  "粳稻主连", "KQ.m@CZCE.WH": "强麦主连",
        "KQ.m@CZCE.PM":  "普麦主连", "KQ.m@CZCE.RS": "菜籽主连",
        "KQ.m@INE.sc":   "原油主连", "KQ.m@INE.lu": "低硫燃油主连",
        "KQ.m@INE.nr":   "20号胶主连", "KQ.m@INE.bc": "国际铜主连",
        "KQ.m@INE.ec":   "集运指数主连", "KQ.m@GFEX.si": "工业硅主连",
        "KQ.m@GFEX.lc":  "碳酸锂主连", "KQ.m@GFEX.ps": "多晶硅主连",
    }
    if code in FUTURES_NAMES:
        return FUTURES_NAMES[code]
    # 尝试从具体合约代码中提取品种名
    for ex in ['CFFEX', 'SHFE', 'DCE', 'CZCE', 'INE', 'GFEX']:
        if code.startswith(ex + '.'):
            return code  # 返回原始代码作为名称
    return code


if __name__ == "__main__":
    print("TqSdkAPI loaded successfully")
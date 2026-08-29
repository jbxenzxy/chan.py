"""
TqSdkAPI - 天勤期货/期指行情数据源适配器
仿照 CTdxAPI 模式，使用 set_data() / append_bar() 注入数据供 chan.py 调用

每个 SSE 连接自包含：创建 TqApi → 拉历史 → chan分析 → 推送快照 → 实时循环。
d/w 周期因天勤主连合约返回垃圾数据，已排除并在前端禁用。
"""
import os
import json
import logging
import threading
import time as _time
from contextlib import contextmanager
from datetime import datetime

# 抑制 tqsdk 内部 INFO 日志（如 WebSocket 连接通知）
# 注意：只抑制 tqsdk 自身 logger，绝不设置 root 级别——
# 若设置 root.setLevel(WARNING) 会覆盖 App/AppLog.py 的全局 INFO，
# 导致股票名/PE-TTM/指数归属等 log.info 进度被静默抑制（历史根因）。
logging.getLogger("tqsdk").setLevel(logging.WARNING)
logging.getLogger("shinny").setLevel(logging.WARNING)
logging.getLogger("tqsdk.tqapi").setLevel(logging.WARNING)

from DataAPI.CommonStockAPI import CCommonStockApi

log = logging.getLogger(__name__)

# ── 当前线程会话（P0-1 修复：缓存实例化到各连接会话）────────────
# CChan 内部经 data_src="custom:TqSdkAPI.CTqSdkAPI" 自行实例化数据源
# （code/k_type 等构造参数由引擎传入），无法直接注入会话。SSE 生成器
# 为同步单线程（StreamingResponse 线程池），故用线程局部保存「本线程当前
# 会话」，CTqSdkAPI.__init__ 据此绑定该连接 CTqSdkSession 的记录缓存，
# 实现「每连接自包含」；脱离会话直接实例化（如工具脚本）回退自有缓存。
_CURRENT_SESSION = threading.local()


@contextmanager
def session_context(session):
    """会话上下文：让本线程随后创建的 CTqSdkAPI 实例绑定 session 的记录缓存。

    session 需具备 _records_by_symbol/_lock（CTqSdkSession 实例）；
    MockSource 等无记录缓存的对象不绑定。线程局部离开后自动还原，
    避免线程池复用串连其它连接缓存。
    """
    prev = getattr(_CURRENT_SESSION, "session", None)
    _CURRENT_SESSION.session = session
    try:
        yield
    finally:
        _CURRENT_SESSION.session = prev


def session_set(session):
    """在生成器入口设置当前会话（覆盖整个生成器生命周期，供实时 step_load 重建数据源）。"""
    _CURRENT_SESSION.session = session


def session_clear():
    """在生成器 finally 清除当前会话（线程池复用前必须清，防串连）。"""
    _CURRENT_SESSION.session = None

# ============================================================
# 天勤配置
# ============================================================
TQ_ACCOUNT = ""
TQ_PASSWORD = ""
_SSE_DEBUG = False  # SSE 推送详细调试日志开关（设为 True 可恢复调试输出）


def load_tq_account(config_dir):
    """
    加载天勤账户和密码，写入模块级 TQ_ACCOUNT / TQ_PASSWORD。

    优先级：环境变量 TQ_ACCOUNT / TQ_PASSWORD > tq_account.json 文件
      ① 环境变量 TQ_ACCOUNT / TQ_PASSWORD（win11 可用 `setx` 写入，无需本地文件，
         最优先；需成对非空才生效）；
      ② {config_dir}/tq_account.json（文件格式 {"account": "...", "password": "..."}）。

    启动时即调用以预先准备凭据（TqSdkCSSESource 建连直接读模块级变量）。
    仅当两个来源都取不到有效凭据时才打印提示（避免启动噪音）。

    :return: True=已成功加载有效凭据，False=未取到有效凭据
    """
    global TQ_ACCOUNT, TQ_PASSWORD

    # ① 优先环境变量 TQ_ACCOUNT / TQ_PASSWORD
    env_account = os.environ.get("TQ_ACCOUNT", "").strip()
    env_password = os.environ.get("TQ_PASSWORD", "").strip()
    if env_account and env_password:
        TQ_ACCOUNT = env_account
        TQ_PASSWORD = env_password
        return True

    # ② 回退 {config_dir}/tq_account.json 文件
    file_failure = ""  # 记录文件侧的问题，仅在最终无凭据时一并提示
    account_file = os.path.join(config_dir, "tq_account.json")
    if os.path.exists(account_file):
        try:
            with open(account_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                TQ_ACCOUNT = str(data.get("account", "")).strip()
                TQ_PASSWORD = str(data.get("password", "")).strip()
                if TQ_ACCOUNT and TQ_PASSWORD:
                    return True
                file_failure = f"账户文件内容不完整（account/password 字段）：{account_file}"
            else:
                file_failure = f"账户文件格式错误（应为 JSON 对象）：{account_file}"
        except json.JSONDecodeError as e:
            file_failure = f"账户文件 JSON 解析失败：{account_file}（{e}）"
        except Exception as e:
            file_failure = f"读取账户文件失败：{account_file}（{e}）"
    else:
        file_failure = f"账户文件不存在：{account_file}"

    # ③ 两来源皆无 → 汇总打印（默认空值）
    TQ_ACCOUNT = ""
    TQ_PASSWORD = ""
    reason = file_failure
    if env_account or env_password:
        reason += "；环境变量 TQ_ACCOUNT / TQ_PASSWORD 未成对设置"
    log.warning(f"[TqSdkAPI] 未取到有效凭据（{reason}），使用默认空值")
    return False

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
    # ===== 新加坡 SGX（外盘延时） =====
    "A50": "KQD.m@SGX.CN", "CN": "KQD.m@SGX.CN",
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

# 期货历史数据回看条数
# 单一事实源在 App/AppConfig.py 的 FUTURES_LOOKBACK_CONFIG；因本文件属
# DataAPI 层（DataAPI 不反向依赖 App），配置由 AppEngine 启动时
# 经 set_futures_lookback_config() 注入到模块级 _futures_lookback_config。
# 不再维护本地 HISTORY_LOOKBACK_BARS 兜底常量：缺失周期一律回退默认 300 根。
# bars 语义与股票 STOCKS_LOOKBACK_CONFIG 完全对齐：>0=保留最近 N 根；<=0=不限制
# （「不限制」在 fetch_futures_kline 内统一落天勤上限 10000 根，绝不把 <=0 传给天勤——
#   天勤 get_kline_serial 要求数据长度为正整数，<=0 会直接抛异常）。

# 期货历史回看配置（标签键 → (bars, label)）；由 AppEngine 注入，默认空。
_futures_lookback_config: dict = {}


class CTqSdkAPI(CCommonStockApi):
    """
    天勤数据源适配器，继承 CCommonStockApi 实现完整接口。
    缓存键为 "symbol:freq_sec" 格式，同品种不同周期各自独立。

    P0-1 修复：K 线记录缓存由类级全局改为「每连接会话实例级」——
    SSE 各连接持有独立 CTqSdkSession，其 _records_by_symbol 即本类
    实例读取的缓存（经线程局部绑定，见 session_context / __init__）。
    类级接口保留作兼容（fetch_kline 等元数据/取数方法不变）。

    实现 CommonStockAPI 元数据接口：
    频率映射 / 别名 / 支持列表 为抽象层元数据属性（类属性访问），
    fetch_kline 走基类 get_kline 家族（委托模块级 fetch_futures_kline）。
    """

    # ── 数据源元数据接口（覆盖 CommonStockAPI 默认空值）──────────
    # 普通类属性（全 Python 版本兼容；@classmethod @property 在 3.11+
    # 下 classmethod 包装 property 返回 method 对象，见 CommonStockAPI 说明）
    FREQ_SEC_MAP = FREQ_SEC_MAP
    FREQ_LABEL_CN = FREQ_LABEL_CN
    FUTURES_ALIASES = FUTURES_ALIASES
    SUPPORTED_FREQS = SUPPORTED_FREQS
    DISABLED_FREQS = DISABLED_FREQS

    @classmethod
    def fetch_kline(cls, api, symbol, freq_sec=15, num_bars=None,
                    display_key=None, start_time=None):
        """拉取历史 K 线（委托模块级 fetch_futures_kline）"""
        return fetch_futures_kline(api, symbol, freq_sec=freq_sec,
                                   num_bars=num_bars, display_key=display_key,
                                   start_time=start_time)

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass

    @classmethod
    def clear_all_cache(cls):
        """清空全部期货K线缓存（期货切股票时调用）。

        P0-1 修复：缓存已实例化到各连接 CTqSdkSession，类级统一清空改为
        遍历活跃会话注册表逐个清空；无活跃会话（纯工具场景）自动无操作。
        """
        from DataAPI.TqSdkCSSESource import _ACTIVE_SOURCES, _ACTIVE_SOURCES_LOCK
        with _ACTIVE_SOURCES_LOCK:
            sources = list(_ACTIVE_SOURCES)
        for src in sources:
            try:
                src.clear_all_cache()
            except Exception:
                pass

    def set_data(self, records, symbol=None):
        key = symbol or "__default__"
        with self._lock:
            self._records_by_symbol[key] = list(records)

    def append_bar(self, bar, symbol=None):
        key = symbol or "__default__"
        with self._lock:
            if key not in self._records_by_symbol:
                self._records_by_symbol[key] = []
            self._records_by_symbol[key].append(bar)

    def get_data(self, symbol=None, **kwargs):
        key = symbol or "__default__"
        with self._lock:
            return self._records_by_symbol.get(key, []).copy()

    def get_last_n(self, n=1, symbol=None):
        key = symbol or "__default__"
        with self._lock:
            records = self._records_by_symbol.get(key, [])
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
        # P0-1 修复：缓存实例级。SSE 单线程生成器内经 session_context /
        # session_set 绑定的会话缓存共享同一份记录；脱离会话（工具脚本/
        # 测试直实例化）回退自有空缓存。
        _session = getattr(_CURRENT_SESSION, "session", None)
        if _session is not None and hasattr(_session, "_records_by_symbol"):
            self._records_by_symbol = _session._records_by_symbol
            self._lock = _session._lock
        else:
            self._records_by_symbol = {}
            self._lock = threading.Lock()

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
                # auto=False: 不自动将 00:00 调整为 23:59
                # SGX A50 等外盘品种有凌晨 00:00 的分钟K线，auto=True 会导致
                # 00:00 的 ts 被改为 23:59，使后续 00:05 的 K线时间非单调递增
                ct = CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, auto=False)
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


def set_futures_lookback_config(config: dict):
    """注入期货历史回看配置（标签键 → (bars, label)）。

    由 App/AppEngine.py 启动时调用，把 AppConfig.FUTURES_LOOKBACK_CONFIG
    注入到本模块（DataAPI 层不反向依赖 App）。
    传空 dict 可复位为空；缺失周期在 fetch_futures_kline 中回退默认 300 根。
    值语义与股票 STOCKS_LOOKBACK_CONFIG 对齐：bars>0=保留最近 bars 根；
    bars<=0=「不限制」——fetch_futures_kline 内统一落天勤上限 10000 根
    （实际返回以账户权限为准），不会把 <=0 传给天勤。
    """
    global _futures_lookback_config
    _futures_lookback_config = dict(config) if config else {}


def resolve_lookback_bars(freq_sec):
    """解析期货某周期的回看条数（A 操作「最新K线」模式的取数根数）。

    单一数据源：AppEngine 注入的 FUTURES_LOOKBACK_CONFIG（标签键 → (bars,label)）。
    周期缺失或未注入时回退默认 300 根；bars<=0（不限制）统一返回天勤上限 10000。
    返回恒为正整数，可直接作为 get_kline_serial 的 data_length。
    供 AppSSE 计算窗口取数根数（对齐上窗区间/复盘回推）复用。
    """
    num_bars = 300
    label = SEC_TO_LABEL.get(freq_sec)
    if label is not None:
        _cfg = _futures_lookback_config.get(label)
        if _cfg is not None:
            num_bars = _cfg[0]
    if num_bars <= 0:
        num_bars = 10000
    return num_bars


def fetch_futures_kline(api, symbol, freq_sec=15, num_bars=None, display_key=None, start_time=None):
    """
    从天勤拉取历史 K 线数据，转换为 records 格式。
    api: TqApi 实例（由调用方创建和传入）
    start_time: 选点起始时间字符串（如 "2026-01-09 10:00"），有值时只返回该时间之后的K线
    num_bars: 显式回看条数；None 时按注入的 FUTURES_LOOKBACK_CONFIG 解析。
              <=0 一律视为「不限制」（与股票 STOCKS_LOOKBACK_CONFIG 语义对齐，
              不会传给天勤触发其「数据长度非法」异常）。
    """
    t_start = _time.time()
    if num_bars is None:
        num_bars = resolve_lookback_bars(freq_sec)
    # 「不限制」统一兜底（配置值 bars<=0，或调用方显式传入 num_bars<=0）：
    # 天勤 get_kline_serial 要求数据长度为正整数（<=0 直接抛异常），客户端上限
    # 10000（官方 docstring「每个序列最大支持请求 10000 个数据」，超出自动截断）。
    # 与股票「bars<=0=不限制」语义对齐：不限制 → 取天勤上限，实际返回以账户权限为准。
    if num_bars <= 0:
        num_bars = 10000

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
        # 天勤K线无成交额字段（仅tick/quote有），前端期货改显成交量(vol)；amount置0占位以保持record结构
        # 原代码误用持仓量(open_oi)冒充成交额，已清除

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
            "amount": 0,  # 天勤K线无成交额，置0占位（前端期货显示成交量vol）
        })

    # 如果有选点时间，过滤记录
    if start_time is not None and records:
        start_dt = None
        for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
            try:
                start_dt = datetime.strptime(start_time, fmt)
                break
            except ValueError:
                continue
        if start_dt is not None:
            before_count = len(records)
            records = [r for r in records if r["dt"] >= start_dt]
            if before_count != len(records):
                log.info(f"[{display_key or symbol}] 选点过滤: {start_time} -> {before_count}条 → {len(records)}条")

    # ★ 天勤免费API可能返回超过num_bars的数据，强制截断以控制step_load耗时
    if len(records) > num_bars:
        records = records[-num_bars:]

    label = SEC_TO_LABEL.get(freq_sec, f"{freq_sec}s")
    prefix = display_key if display_key else f"{symbol} {label}"
    elapsed = _time.time() - t_start
    if _SSE_DEBUG:
        if records:
            log.info(f"[{prefix}] ⑴ 拉取历史: {len(records)}根, "
                  f"{records[0]['dt'].strftime('%Y-%m-%d %H:%M:%S')} ~ "
                  f"{records[-1]['dt'].strftime('%Y-%m-%d %H:%M:%S')}, "
                  f"耗时 {elapsed:.1f}s")
        else:
            log.warning(f"[{prefix}] ⑴ 拉取历史: 0条 (无有效数据), 耗时 {elapsed:.1f}s")
    return records


# ============================================================
# 期货代码/名称工具函数
# ============================================================


def _get_futures_code(code):
    """识别期货/期指代码，返回天勤标准代码；非期货返回 None。"""
    # 格式: EXCHANGE.SYMBOL (如 CFFEX.IM2507, SHFE.rb2505)
    # 或主连: KQ.m@EXCHANGE.SYMBOL, KQ.i@EXCHANGE.SYMBOL
    # 注意：天勤要求 KQ 前缀中的 m/i 小写，所以先 upper() 匹配，再恢复小写
    FUTURE_EXCHANGES = ['CFFEX', 'SHFE', 'DCE', 'CZCE', 'INE', 'GFEX', 'SGX']
    for ex in FUTURE_EXCHANGES:
        if code.startswith(ex + '.'):
            return code
        if code.startswith('KQ.M@' + ex + '.'):
            return code.replace('KQ.M@', 'KQ.m@', 1)
        if code.startswith('KQ.I@' + ex + '.'):
            return code.replace('KQ.I@', 'KQ.i@', 1)
        # 外盘延时行情: KQD.m@ 前缀
        if code.startswith('KQD.M@' + ex + '.'):
            return code.replace('KQD.M@', 'KQD.m@', 1)

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
        "KQD.m@SGX.CN": "A50主连",
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
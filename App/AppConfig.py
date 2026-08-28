# -*- coding: utf-8 -*-
"""
App/AppConfig.py —— 基础设施配置（配置中心 · 双文件之一）
=========================================================================
职责：与运行环境相关的配置集中于此；算法参数在顶层 ChanConfig.py
（双文件并行，职责分离，symbol_code 归 ChanConfig.py 管理）。

加载优先级（高 → 低）：
  1. 环境变量（PORT / HOST / CHAN_PATH / TDX_INSTALL_DIR /
     SCAN_POOL_WORKERS / TQ_ACCOUNT / TQ_PASSWORD）
  2. 仓库根目录 .env 文件（格式见 .env.example，凭据永不进 git）
  3. 本文件内的默认值（保证零配置可跑）

实现：优先 pydantic-settings（类型安全、可校验）；未安装时降级为
内置解析器（.env + 环境变量，标量类型）。两套实现共享同名底座
_AppConfigBase（派生路径 + 方法），字段默认值收于 _FIELD_DEFAULTS
单一事实源，任何字段变更只需改一处。

凭据安全：TQ 账号/密码仅进程启动时读取、内存使用，不落缓存、不写日志、
不随任务序列化；对外展示必须走 as_dict(redact=True)。

跨平台：TDX_INSTALL_DIR 默认值平台适应 —— Windows 用 C:\\new_tdx_hd_test，
其它平台用 ~/tdx；显式 .env / 环境变量仍可覆盖默认值。
"""
import json
import os
from App.AppLog import get_logger
log = get_logger(__name__)


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_FILE = os.path.join(_REPO_ROOT, ".env")

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAVE_PYDANTIC_SETTINGS = True
except ImportError:  # 优雅降级：未安装 pydantic-settings 时用内置解析
    _HAVE_PYDANTIC_SETTINGS = False


def _parse_env_file(path):
    """极简 .env 解析（降级模式专用）：KEY=VALUE，忽略注释与空行，去引号。"""
    result = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                result[key] = value
    except OSError:
        pass
    return result


# ═══════════════════════════════════════════════════════════════════
# 跨平台默认安装目录
# ═══════════════════════════════════════════════════════════════════
def _default_tdx_install_dir() -> str:
    """平台适应的 TDX_INSTALL_DIR 默认值。

    Windows：默认 C:\\new_tdx_hd_test；
    其它平台：~/tdx（避免 Windows 路径在 *nix 上必然不可用）。
    显式 .env / 环境变量仍可覆盖该默认值。
    """
    if os.name == "nt":
        return r"C:\new_tdx_hd_test"
    return os.path.join(os.path.expanduser("~"), "tdx")


# ═══════════════════════════════════════════════════════════════════
# 字段默认值 · 单一事实源
# pydantic 版（类型标注）与降级版（env 解析）均从此取默认值，
# 改字段只改这一处，杜绝两套实现各自维护导致的漂移。
# ═══════════════════════════════════════════════════════════════════
_FIELD_DEFAULTS = {
    "HOST": "127.0.0.1",
    "PORT": 18081,
    "CHAN_PATH": _REPO_ROOT,
    "TDX_INSTALL_DIR": _default_tdx_install_dir(),
    "SCAN_POOL_WORKERS": 0,      # 0=自动按 CPU 核数适配；>0 显式覆盖（对齐 .env.example）
    "SCAN_MIN_FLOAT_MC": 50.0,   # 扫描预过滤流通市值下限（亿）
    "SCAN_FANGLIANG_WINDOW_BARS": 120,  # 放量扫描：以 A 那根往前数 N 根K线作比较窗口，
                                        # 即「A > 该窗口内任一天成交额」视为放量命中
    "TQ_ACCOUNT": "",
    "TQ_PASSWORD": "",
    "FORWARD_ADJUST_ENABLED": True,   # 前复权开关
    "FULL_DATA_MODE": False,          # 全量数据模式
    # ── 各标的K线「回看条数」配置 ──────────────────────────────
    # 键统一为 标的类型前缀 + LOOKBACK_CONFIG：STOCKS_ 股票 / FUTURES_ 期货。
    # 值格式统一：(bars, label)，bars=保留最近 N 根K线（<=0 表示不限制）。
    # bars 只决定「后端加载多少根K线」，取值完全由用户按需权衡，随时可调：
    #     缩小 → 后端加载更快、可回看区间更短；放大 → 可回看区间更长；0 → 不限制。
    # 下游全部按「后端实际加载的K线范围/根数」自适应，不依赖本配置的具体数值：
    #     双窗下窗对齐用主级别实际加载区间、对齐不足降全量看实际根数、
    #     前端视口 VIEW_COUNT 与后端加载根数比较后取小、缠论分析按实际数据计算。
    # 因此调整 bars（无论缩小还是放大）都不需要改任何代码，改本配置即可。
    # ── 股票 STOCKS_LOOKBACK_CONFIG ─────────────────────────────
    # 【键】必须是程序内部周期键名，不是中文：
    #     w=周K  d=日K  60m/30m/15m/5m/1m=分时  15s=15秒
    #     股票实际仅 w/d/30m/5m 四个周期（前端禁用 1m/15s，数据源仅 .day/.lc5）。
    #     若键缺失，则该周期「不限制」，全量加载（当前 w 即走此路径）。
    # 【值】二元组 (bars, label)：
    #     bars —— 真正生效的字段：保留最近多少根K线（整数，单位=根）。
    #             从「最新K线(A操作)」或「复盘结束时间(C操作)」往前保留 bars 根。
    #             bars<=0 表示「不限制」（w 即用 0）；bars>0 表示限制，
    #             具体根数按各周期数据量与回看需求自行配置（可随时放大或缩小）。
    #     label——简体中文说明，仅用于日志显示，【不参与截断计算】。
    #             bars 调整后请同步改 label 保持语义一致，避免日志误导；
    #             写相对时长（如"近3个月"）或绝对起点日期（如"24年09月10日"）均可。
    # 【合法性】bars 填任意整数根数：<=0=不限制，正数=保留最近 N 根。
    # 生效前提：FULL_DATA_MODE=False（全量数据模式跳过截断）。
    "STOCKS_LOOKBACK_CONFIG": {"w": (0, "不限制"), "d": (472, "24年09月10日"), "30m": (500, "近3个月"), "5m": (1000, "近1个月")},
    # ── 期货 FUTURES_LOOKBACK_CONFIG ──
    # 与股票 STOCKS_LOOKBACK_CONFIG 值格式及 bars 语义一致：(bars, label)，<=0=不限制；
    # 但【键】不同：期货为 15s/1m/5m/30m（天勤主连不支持 d/w，已排除）。
    #   bars —— 真正生效的字段：初始快照向天勤回看多少根K线（整数，单位=根）。
    #           作为 api.get_kline_serial(..., num_bars) 的第三参传给 tqsdk，
    #           fetch_futures_kline 末尾还会强制截断到 num_bars 以控制 step_load 耗时。
    #           bars>0：保留最近 bars 根（随时可放大或缩小，与股票一致按需配置）；
    #           bars<=0：「不限制」——天勤接口要求数据长度必须为正整数（传 0 会
    #           直接抛异常），fetch_futures_kline 内已统一兜底为天勤上限 10000 根
    #           （实际返回以账户权限为准），故填 0 与股票习惯一致、亦安全。
    #   label——简体中文说明，仅用于日志显示，【不参与条数计算】（bars 变更请同步改）。
    # 单一事实源：经 AppEngine 启动注入 DataAPI/TqSdkAPI.py。
    "FUTURES_LOOKBACK_CONFIG": {"30m": (120, "15个交易日"), "5m": (240, "5个交易日"), "1m": (480, "2个交易日"), "15s": (960, "1个交易日")},
    # 「不限制」写法示例（两类标的通用：bars 填 0 即不限制，按需复制取消注释）：
    # "STOCKS_LOOKBACK_CONFIG": {"w": (0, "不限制"), "d": (0, "不限制"), "30m": (0, "不限制"), "5m": (0, "不限制")},
    # "FUTURES_LOOKBACK_CONFIG": {"30m": (0, "不限制"), "5m": (0, "不限制"), "1m": (0, "不限制"), "15s": (0, "不限制")},
    # 前端视口默认显示的K线根数（所有周期相同，与后端加载根数解耦）：
    #   当后端加载的K线根数 > VIEW_COUNT，前端视口显示 VIEW_COUNT 根（右对齐）；
    #   当后端加载的K线根数 < VIEW_COUNT，前端视口降为「后端加载多少显示多少」（填满宽度）。
    #   经 /api/health 下发命令前端 app.js。
    #   例外：双击选点（B操作）后端已按「选点→最新」过滤，前端全量显示，
    #   不受 VIEW_COUNT 限制（单窗上窗、双窗上窗、双窗下窗同此规则）。
    # 【与 DUAL_SUB_FALLBACK_MIN 的关系】两者相互独立，互不联动：
    #   双窗下窗「降全量」只看 DUAL_SUB_FALLBACK_MIN（默认100），与本值无关——
    #   它保护的是后端缠论分析所需最小K线数（下窗笔结构），不是视口填充数。
    #   故本值【不建议】配置为小于 DUAL_SUB_FALLBACK_MIN 的值：如本值配 50，
    #   下窗对齐后不足 100 仍会降全量（不会等到不足 50 才降），视口却只显示
    #   50 根——功能可用（可滚动查看），但容易误解为「不足视口数才降全量」。
    "VIEW_COUNT": 233,
    # ── 双窗口下窗「对齐不足降全量」阈值 DUAL_SUB_FALLBACK_MIN ─────
    # 双窗下窗加载与上窗时间区间对齐（上窗区间=上窗后端实际加载的K线范围）。
    # 当按对齐区间截断后下窗K线根数 < DUAL_SUB_FALLBACK_MIN 时（下窗数据源
    # 覆盖不足，如上市较晚、分时文件只存近期），下窗降为全量加载，保证下窗
    # 笔结构可支撑缠论分析（区间套/红框中枢依赖完整下窗笔）。
    # 全量仍 < 5 根时按既有逻辑退化为单级别（双窗降级提示）。
    # 建议值 >= 下窗缠论分析所需最小K线数（几笔结构），过小起不到兜底作用。
    # 注意：本阈值与 VIEW_COUNT（前端视口显示根数）无关——即使 VIEW_COUNT
    # 配置为 50，仍按本阈值（<100）降全量，不会随 VIEW_COUNT 缩小。
    "DUAL_SUB_FALLBACK_MIN": 100,
    # ── 双窗口缓存限额（与单窗口共用同一 LRU 池，但单独限额）──────
    # 单窗口是常用操作，缓存条目（single 键）用满 LRU 池（MAX_CACHE_SIZE=50）；
    # 双窗口非常用且条目更重（dual_main + dual_sub 两键，各含 CChan），
    # 故在共享池内对 dual_* 键单独限额：超限时优先淘汰最旧的 dual 键，
    # 不挤占常用单窗口缓存。MAX_DUAL_CACHE_KEYS 按「组」计（1组=2键）。
    "MAX_DUAL_CACHE_KEYS": 10,   # 双窗结构化缓存键上限（10键 = 5组双窗）
    # 双窗运行时下窗 CChan 缓存（stocks_sub_cache）：与上面的 dual_*
    # 结构化缓存【不是同一个缓存】——是两个独立的 dict，各有各的键和消费方：
    #   · dual_main/dual_sub（结构化缓存）：键=(kind,市场,代码,周期,日期,实现)，
    #     消费方=API 层缓存命中（重复请求免重算），MAX_DUAL_CACHE_KEYS 管它；
    #   · stocks_sub_cache（运行时缓存）：键=代码:下窗周期（无日期/实现维度），
    #     消费方=区间套 check_nested_diver / 红框中枢重算 / 双窗选点重建——
    #     这些消费方只知道「代码+下窗周期」，构不出结构化键，故必须有独立缓存。
    # 两个 dict 各存各的引用（同一个下窗 CChan 会被两处同时引用）：
    # 只限额 dual_* 键管不到本缓存，反之亦然——两个上限合起来才框住双窗内存。
    # 本缓存采用 LRU 限额，超限淘汰最旧；切回单窗不主动清（保留快速切回能力）。
    "MAX_STOCKS_SUB_CHAN": 5,    # 运行时下窗 CChan 条目上限
    "DEBUG_COLD_START_START_DATE": None,  # 冷启动起始日期（None=不开启）
    "DEBUG_COLD_START_END_DATE": None,    # 冷启动结束日期（None=不开启）
    "SSE_DEBUG": False,                   # SSE 推送详细调试日志开关
}

# 降级版 env 变量 → 类型转换表（仅覆盖可经 env/.env 注入的标量字段）
def _env_bool(raw):
    """严格字符串→布尔解析：仅 1/true/yes/on 视作 True，其余（含 "False"）为 False。

    修复：原实现 caster=bool 直接 bool(raw)，非空字符串恒真，导致
    env 写 "SSE_DEBUG=False" 反而变成 True（字符串布尔陷阱）。
    """
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


_FIELD_TYPES = {
    "HOST": str,
    "PORT": int,
    "CHAN_PATH": str,
    "TDX_INSTALL_DIR": str,
    "SCAN_POOL_WORKERS": int,
    "SCAN_MIN_FLOAT_MC": float,
    "SCAN_FANGLIANG_WINDOW_BARS": int,
    "VIEW_COUNT": int,
    "DUAL_SUB_FALLBACK_MIN": int,
    "MAX_DUAL_CACHE_KEYS": int,
    "MAX_STOCKS_SUB_CHAN": int,
    "DEBUG_COLD_START_START_DATE": str,
    "DEBUG_COLD_START_END_DATE": str,
    "TQ_ACCOUNT": str,
    "TQ_PASSWORD": str,
    "SSE_DEBUG": _env_bool,
    "FORWARD_ADJUST_ENABLED": _env_bool,
    "FULL_DATA_MODE": _env_bool,
}


class _AppConfigBase:
    """共享底座：派生路径 + 方法。

    不含字段声明（由子类按各自机制声明，值均来自 _FIELD_DEFAULTS），
    只承载「由字段推导」的只读逻辑 —— 派生路径、凭据加载、展示摘要。
    """

    # ── 配置来源标记（随运行时实现自动判定，两套实现共用）──
    @property
    def _config_source(self) -> str:
        return "pydantic-settings" if _HAVE_PYDANTIC_SETTINGS else "builtin-fallback"

    # ── 派生路径（均由 tdx_install_dir / 仓库根推导）──
    @property
    def repo_root(self) -> str:
        return _REPO_ROOT

    @property
    def vipdoc_dir(self) -> str:
        return os.path.join(self.tdx_install_dir, "vipdoc")

    @property
    def download_dir(self) -> str:
        return os.path.join(self.vipdoc_dir, "eltdx")

    @property
    def tdx_hq_cache(self) -> str:
        return os.path.join(self.tdx_install_dir, "T0002", "hq_cache")

    @property
    def output_dir(self) -> str:
        return _REPO_ROOT                           # 输出目录（= 仓库根）

    @property
    def frontend_dir(self) -> str:
        return os.path.join(_REPO_ROOT, "Frontend")

    @property
    def app_data_dir(self) -> str:
        """应用持久化数据目录（= App/ 包目录）。

        应用自生成的缓存 / 状态类文件统一落此目录，不再散落到 vipdoc
        数据目录：股票名、PE-TTM/指数归属、流通市值、手动选点、文本标注、
        上次查看代码周期、扫描任务 DB。App/ 目录随仓库存取，天然随部署迁移。
        """
        return os.path.join(_REPO_ROOT, "App")

    @property
    def last_code_freq_file(self) -> str:
        return os.path.join(self.app_data_dir, "last_code_freq.json")

    @property
    def stock_names_cache_file(self) -> str:
        """股票名称缓存文件"""
        return os.path.join(self.app_data_dir, "stock_names.json")

    @property
    def stock_pe_ttm_file(self) -> str:
        """PE-TTM / 指数归属缓存文件"""
        return os.path.join(self.app_data_dir, "stock_pettm_index.json")

    @property
    def float_mc_cache_file(self) -> str:
        """流通市值缓存文件"""
        return os.path.join(self.app_data_dir, "stock_float_mc.json")

    @property
    def saved_point_file(self) -> str:
        """手动选点持久化文件"""
        return os.path.join(self.app_data_dir, "double_click_dt.csv")

    @property
    def annotations_file(self) -> str:
        """文本标注持久化文件"""
        return os.path.join(self.app_data_dir, "text_annotation.json")

    @property
    def scan_task_db_file(self) -> str:
        """批量扫描任务 SQLite 数据库文件"""
        return os.path.join(self.app_data_dir, "scan_tasks.db")

    # ── TQ 凭据（本地文件 / 环境变量，不进缓存）────────────────────
    def load_tq_account(self) -> dict:
        """
        返回天勤账号凭据：env/.env 注入字段优先，回退 {VIPDOC_DIR}/tq_account.json。

        优先级：env/.env 注入的 tq_account/tq_password > {VIPDOC_DIR}/tq_account.json。
        环境变量优先（win11 可用 `setx TQ_ACCOUNT ...` 无需本地文件）；
        文件缺失/内容无效时才回退文件。文件格式: {"account": "...", "password": "..."}
        凭据仅服务进程启动时读取，不落缓存、不写日志、不随任务序列化。
        """
        # ① 优先 env/.env 注入字段
        env_account = (self.tq_account or "").strip()
        env_password = (self.tq_password or "").strip()
        if env_account and env_password:
            return {"account": env_account, "password": env_password}

        # ② 回退 {VIPDOC_DIR}/tq_account.json
        path = os.path.join(self.vipdoc_dir, "tq_account.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                a = str(data.get("account", "")).strip()
                p = str(data.get("password", "")).strip()
                if a and p:
                    return {"account": a, "password": p}
        except Exception:
            pass
        return {}

    def as_dict(self, redact: bool = True) -> dict:
        """对外展示用摘要；redact=True 时凭据打码。"""
        return {
            "host": self.host,
            "port": self.port,
            "chan_path": self.chan_path,
            "tdx_install_dir": self.tdx_install_dir,
            "scan_pool_workers": self.scan_pool_workers,
            "scan_min_float_mc": self.scan_min_float_mc,
            "full_data_mode": self.full_data_mode,
            "view_count": self.view_count,
            "tq_account": (self.tq_account[:2] + "***") if (redact and self.tq_account) else self.tq_account,
            "tq_password": "***" if (redact and self.tq_password) else self.tq_password,
            "config_source": self._config_source,
        }


if _HAVE_PYDANTIC_SETTINGS:

    class AppConfig(_AppConfigBase, BaseSettings):
        """基础设施配置（与算法参数 ChanConfig.py 并行）"""

        model_config = SettingsConfigDict(
            env_file=_ENV_FILE if os.path.exists(_ENV_FILE) else None,
            env_file_encoding="utf-8",
            extra="ignore",
        )

        # ── 服务 ──
        host: str = _FIELD_DEFAULTS["HOST"]                     # API 监听地址
        port: int = _FIELD_DEFAULTS["PORT"]                     # API 服务端口

        # ── 路径 ──
        chan_path: str = _FIELD_DEFAULTS["CHAN_PATH"]           # chan.py 仓库根目录
        tdx_install_dir: str = _FIELD_DEFAULTS["TDX_INSTALL_DIR"]  # 通达信安装目录（改路径只改这一处/.env）

        # ── 并发 ──
        scan_pool_workers: int = _FIELD_DEFAULTS["SCAN_POOL_WORKERS"]  # ProcessPool worker 数（0=按核数）
        scan_min_float_mc: float = _FIELD_DEFAULTS["SCAN_MIN_FLOAT_MC"]  # 扫描预过滤流通市值下限（亿）
        scan_fangliang_window_bars: int = _FIELD_DEFAULTS["SCAN_FANGLIANG_WINDOW_BARS"]  # 放量扫描比较窗口K线根数（默认120）

        # ── 凭据（.env 或环境变量注入；不落缓存不写日志）──
        tq_account: str = _FIELD_DEFAULTS["TQ_ACCOUNT"]         # 天勤账号（空 = 回退 vipdoc/tq_account.json）
        tq_password: str = _FIELD_DEFAULTS["TQ_PASSWORD"]       # 天勤密码

        # ── 数据模式（引擎行为开关）────────────────────────────
        forward_adjust_enabled: bool = _FIELD_DEFAULTS["FORWARD_ADJUST_ENABLED"]  # 前复权开关
        full_data_mode: bool = _FIELD_DEFAULTS["FULL_DATA_MODE"]                   # 全量数据模式
        stocks_lookback_config: dict = _FIELD_DEFAULTS["STOCKS_LOOKBACK_CONFIG"]  # 股票K线回看条数配置
        futures_lookback_config: dict = _FIELD_DEFAULTS["FUTURES_LOOKBACK_CONFIG"]  # 期货K线回看条数配置
        view_count: int = _FIELD_DEFAULTS["VIEW_COUNT"]                           # 前端视口默认显示K线根数（所有周期相同）
        dual_sub_fallback_min: int = _FIELD_DEFAULTS["DUAL_SUB_FALLBACK_MIN"]     # 双窗下窗对齐截断后不足此数→降全量
        max_dual_cache_keys: int = _FIELD_DEFAULTS["MAX_DUAL_CACHE_KEYS"]         # 双窗结构化缓存键上限（共享池内单独限额）
        max_stocks_sub_chan: int = _FIELD_DEFAULTS["MAX_STOCKS_SUB_CHAN"]         # 双窗运行时下窗 CChan 缓存上限
        debug_cold_start_start_date: str | None = _FIELD_DEFAULTS["DEBUG_COLD_START_START_DATE"]
        debug_cold_start_end_date: str | None = _FIELD_DEFAULTS["DEBUG_COLD_START_END_DATE"]
        sse_debug: bool = _FIELD_DEFAULTS["SSE_DEBUG"]          # SSE 推送详细调试日志开关

else:

    class AppConfig(_AppConfigBase):
        """降级实现：字段默认值来自 _FIELD_DEFAULTS，env/.env 标量覆盖默认值。"""

        def __init__(self):
            # 先用 _FIELD_DEFAULTS 铺默认值（单一事实源），键大写、赋值小写属性名
            self.__dict__.update({k.lower(): v for k, v in _FIELD_DEFAULTS.items()})
            # 键统一大写（与 pydantic-settings 大小写不敏感行为对齐），
            # 赋值目标为小写属性名（env 键大写 / 属性名小写，两者不可
            # 混用，否则环境变量覆盖会静默失效）。
            merged = {k.upper(): v for k, v in _parse_env_file(_ENV_FILE).items()}
            merged.update({k.upper(): v for k, v in os.environ.items() if k.upper() in _FIELD_TYPES})
            for key, caster in _FIELD_TYPES.items():
                raw = merged.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    setattr(self, key.lower(), caster(raw))
                except (TypeError, ValueError):
                    log.info(f"[AppConfig] 环境变量 {key}={raw!r} 类型非法，使用默认值")


# 全局单例：一处定义、全局引用
app_config = AppConfig()

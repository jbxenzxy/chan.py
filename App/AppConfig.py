# -*- coding: utf-8 -*-
"""
App/AppConfig.py —— 基础设施配置（配置中心 · 双文件之一）
=========================================================================
职责（V10 方案 7.1/7.2）：与运行环境相关的配置集中于此；
算法参数在顶层 ChanConfig.py（双文件并行，职责分离）。

加载优先级（高 → 低）：
  1. 环境变量（PORT / HOST / CHAN_PATH / TDX_INSTALL_DIR /
     SCAN_POOL_WORKERS / TQ_ACCOUNT / TQ_PASSWORD）
  2. 仓库根目录 .env 文件（格式见 .env.example，凭据永不进 git）
  3. 本文件内的默认值（与历史硬编码行为完全一致，保证零配置可跑）

实现：优先 pydantic-settings（类型安全、可校验）；未安装时降级为
内置解析器（.env + 环境变量，标量类型），保证引擎链路不被配置层卡死。
两套实现共享同名底座 _AppConfigBase（派生路径 + 方法），字段默认值
收于 _FIELD_DEFAULTS 单一事实源 —— P2-1 消除「字段/派生路径/as_dict
双份重复」，任何字段变更只需改一处。

凭据安全（方案 7.3）：TQ 账号/密码仅进程启动时读取、内存使用，
不落缓存、不写日志、不随任务序列化；对外展示必须走 as_dict(redact=True)。

跨平台（P2-2）：TDX_INSTALL_DIR 默认值平台适应 —— Windows 沿用
历史默认 C:\\new_tdx_hd_test，其它平台用 ~/tdx；显式 .env / 环境变量
仍覆盖默认值。

合并说明（阶段 2 双版本合并）：
  - 底座采用第三方版：绝对路径 .env（_REPO_ROOT/.env）+ pydantic-settings 降级
  - 吸收本版补充：数据模式字段（forward_adjust / full_data_mode / sse 调试）
    与派生路径（stock_names / pe_ttm / float_mc / saved_point / annotations）
  - symbol_code 归 ChanConfig.py 管理（方案 7.2），本文件不持有
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
# 跨平台默认安装目录（P2-2）
# ═══════════════════════════════════════════════════════════════════
def _default_tdx_install_dir() -> str:
    """平台适应的 TDX_INSTALL_DIR 默认值。

    Windows：沿用历史默认 C:\\new_tdx_hd_test；
    其它平台：~/tdx（避免 Windows 路径在 *nix 上必然不可用）。
    显式 .env / 环境变量仍可覆盖该默认值。
    """
    if os.name == "nt":
        return r"C:\new_tdx_hd_test"
    return os.path.join(os.path.expanduser("~"), "tdx")


# ═══════════════════════════════════════════════════════════════════
# 字段默认值 · 单一事实源（P2-1）
# pydantic 版（类型标注）与降级版（env 解析）均从此取默认值，
# 改字段只改这一处，杜绝两套实现各自维护导致的漂移。
# ═══════════════════════════════════════════════════════════════════
_FIELD_DEFAULTS = {
    "HOST": "127.0.0.1",
    "PORT": 18081,
    "CHAN_PATH": _REPO_ROOT,
    "TDX_INSTALL_DIR": _default_tdx_install_dir(),
    "SCAN_POOL_WORKERS": 0,      # 0=自动按 CPU 核数适配；>0 显式覆盖（对齐 .env.example）
    "SCAN_MIN_FLOAT_MC": 50.0,   # P2-8：扫描预过滤流通市值下限（亿），原硬编码 float_mc < 50
    "SCAN_FANGLIANG_WINDOW_BARS": 120,  # 放量扫描：以 A 那根往前数 N 根K线作比较窗口（原硬编码 120），
                                        # 即「A > 该窗口内任一天成交额」视为放量命中
    "TQ_ACCOUNT": "",
    "TQ_PASSWORD": "",
    "FORWARD_ADJUST_ENABLED": True,   # 前复权开关
    "FULL_DATA_MODE": False,          # 全量数据模式
    # ── 后端加载K线「时间窗」配置 TIME_TRUNCATE_CONFIG ─────────────
    # 【键】必须是程序内部周期键名，不是中文：
    #     w=周K  d=日K  60m/30m/15m/5m/1m=分时  15s=15秒
    #     若键缺失，则该周期「不限制」，全量加载（当前 w/d 即走此路径）。
    # 【值】二元组 (days, label)：
    #     days —— 真正生效的字段：向前推多少天（整数，单位=自然日）。
    #             从「最新K线(A操作)」或「复盘结束时间(C操作)」往前推 days 天。
    #             days=0 或 days<0 表示「不限制」（此例补入 w/d 即用 0）；
    #             days>0 表示限制，如 30m 取近180天、5m 取近90天（改0即可放开）。
    #     label——简体中文说明，仅用于日志显示，【不参与截断计算】。
    #             请保持与 days 一致，避免日志误导（如 days=180 却写 label="4个月"）。
    # 【合法性】days 填任意整数天数：0=不限制，正数=向前推 N 天。建议用易于理解的天数。
    # 生效前提：FULL_DATA_MODE=False（全量数据模式跳过截断）。
    "TIME_TRUNCATE_CONFIG": {"w": (0, "不限制"), "d": (472, "近2年"), "30m": (180, "近6个月"), "5m": (90, "近3个月")},
    # "TIME_TRUNCATE_CONFIG": {"w": (0, "不限制"), "d": (0, "不限制"), "30m": (0, "不限制"), "5m": (0, "不限制")},
    # 前端视口默认显示的K线根数（所有周期相同，与后端加载根数解耦）：
    #   当后端加载的K线根数 > VIEW_COUNT，前端视口显示 VIEW_COUNT 根（右对齐）；
    #   当后端加载的K线根数 < VIEW_COUNT，前端视口降为「后端加载多少显示多少」（填满宽度）。
    #   经 /api/health 下发命令前端 app.js（取代原硬编码 377）。
    #   例外：双击选点（B操作）后端已按「选点→最新」过滤，前端全量显示，
    #   不受 VIEW_COUNT 限制（单窗上窗、双窗上窗、双窗下窗同此规则）。
    # 【与 DUAL_SUB_FALLBACK_MIN 的关系】两者相互独立，互不联动：
    #   双窗下窗「降全量」只看 DUAL_SUB_FALLBACK_MIN（默认100），与本值无关——
    #   它保护的是后端缠论分析所需最小K线数（下窗笔结构），不是视口填充数。
    #   故本值【不建议】配置为小于 DUAL_SUB_FALLBACK_MIN 的值：如本值配 50，
    #   下窗对齐后不足 100 仍会降全量（不会等到不足 50 才降），视口却只显示
    #   50 根——功能可用（可滚动查看），但容易误解为「不足视口数才降全量」。
    "VIEW_COUNT": 377,
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
    # 本缓存历史上无上限：双窗模式切 N 只票就攒 N 个 CChan 永不释放（泄漏）。
    # 现改为 LRU 限额，超限淘汰最旧；切回单窗不主动清（保留快速切回能力）。
    "MAX_STOCKS_SUB_CHAN": 5,    # 运行时下窗 CChan 条目上限
    "DEBUG_COLD_START_START_DATE": None,  # 冷启动起始日期（None=不开启）
    "DEBUG_COLD_START_END_DATE": None,    # 冷启动结束日期（None=不开启）
    "SSE_DEBUG": False,                   # SSE 推送详细调试日志开关
}

# 降级版 env 变量 → 类型转换表（仅覆盖可经 env/.env 注入的标量字段）
_FIELD_TYPES = {
    "HOST": str,
    "PORT": int,
    "CHAN_PATH": str,
    "TDX_INSTALL_DIR": str,
    "SCAN_POOL_WORKERS": int,
    "SCAN_MIN_FLOAT_MC": float,
    "TQ_ACCOUNT": str,
    "TQ_PASSWORD": str,
    "SSE_DEBUG": bool,
}


class _AppConfigBase:
    """共享底座（P2-1）：派生路径 + 方法。

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
        return _REPO_ROOT                           # 输出目录（= 仓库根，与原 OUTPUT_DIR 语义一致）

    @property
    def frontend_dir(self) -> str:
        return os.path.join(_REPO_ROOT, "Frontend")

    @property
    def last_code_freq_file(self) -> str:
        return os.path.join(self.vipdoc_dir, "last_code_freq.json")

    @property
    def stock_names_cache_file(self) -> str:
        """股票名称缓存文件"""
        return os.path.join(self.vipdoc_dir, "stock_names.json")

    @property
    def stock_pe_ttm_file(self) -> str:
        """PE-TTM / 指数归属缓存文件"""
        return os.path.join(self.vipdoc_dir, "stock_pettm_index.json")

    @property
    def float_mc_cache_file(self) -> str:
        """流通市值缓存文件"""
        return os.path.join(self.vipdoc_dir, "stock_float_mc.json")

    @property
    def saved_point_file(self) -> str:
        """手动选点持久化文件"""
        return os.path.join(self.vipdoc_dir, "double_click_dt.csv")

    @property
    def annotations_file(self) -> str:
        """文本标注持久化文件"""
        return os.path.join(self.vipdoc_dir, "text_annotation.json")

    # ── TQ 凭据（本地文件，不进缓存）────────────────────────────
    def load_tq_account(self) -> dict:
        """
        从 VIPDOC_DIR 下 tq_account.json 读取天勤账号凭据。
        文件格式: {"account": "手机号或用户名", "password": "密码"}
        凭据仅服务进程启动时读取，不落缓存、不写日志、不随任务序列化。
        """
        path = os.path.join(self.vipdoc_dir, "tq_account.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def as_dict(self, redact: bool = True) -> dict:
        """对外展示用摘要；redact=True 时凭据打码（7.3）。"""
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

        # ── 凭据（.env 或环境变量注入；7.3：不落缓存不写日志）──
        tq_account: str = _FIELD_DEFAULTS["TQ_ACCOUNT"]         # 天勤账号（空 = 回退 vipdoc/tq_account.json）
        tq_password: str = _FIELD_DEFAULTS["TQ_PASSWORD"]       # 天勤密码

        # ── 数据模式（引擎行为开关）────────────────────────────
        forward_adjust_enabled: bool = _FIELD_DEFAULTS["FORWARD_ADJUST_ENABLED"]  # 前复权开关
        full_data_mode: bool = _FIELD_DEFAULTS["FULL_DATA_MODE"]                   # 全量数据模式
        time_truncate_config: dict = _FIELD_DEFAULTS["TIME_TRUNCATE_CONFIG"]      # 时间截断配置
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
            # 赋值目标为小写属性名 —— 修复点：此前 setattr 用大写键，
            # 属性名不匹配导致降级模式下环境变量覆盖静默失效。
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


# 全局单例：一处定义、全局引用（方案 7.1）
app_config = AppConfig()

# -*- coding: utf-8 -*-
"""
App/AppConfig.py —— 基础设施配置（配置中心 · 双文件之一）
=========================================================================
职责（V10 方案 7.1/7.2）：与运行环境相关的配置集中于此；
算法参数在顶层 ChanConfig.py（双文件并行，职责分离）。

加载优先级（高 → 低）：
  1. 环境变量（PORT / HOST / CHAN_PATH / TDX_INSTALL_DIR /
     SCAN_CONCURRENCY / TQ_ACCOUNT / TQ_PASSWORD）
  2. 仓库根目录 .env 文件（格式见 .env.example，凭据永不进 git）
  3. 本文件内的默认值（与历史硬编码行为完全一致，保证零配置可跑）

实现：优先 pydantic-settings（类型安全、可校验）；未安装时降级为
内置解析器（.env + 环境变量，标量类型），保证引擎链路不被配置层卡死。

凭据安全（方案 7.3）：TQ 账号/密码仅进程启动时读取、内存使用，
不落缓存、不写日志、不随任务序列化；对外展示必须走 as_dict(redact=True)。

合并说明（阶段 2 双版本合并）：
  - 底座采用第三方版：绝对路径 .env（_REPO_ROOT/.env）+ pydantic-settings 降级
  - 吸收本版补充：数据模式字段（forward_adjust / full_data_mode / sse 调试）
    与派生路径（stock_names / pe_ttm / float_mc / saved_point / annotations）
  - symbol_code 归 ChanConfig.py 管理（方案 7.2），本文件不持有
"""
import os
import json

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


if _HAVE_PYDANTIC_SETTINGS:

    class AppConfig(BaseSettings):
        """基础设施配置（与算法参数 ChanConfig.py 并行）"""

        model_config = SettingsConfigDict(
            env_file=_ENV_FILE if os.path.exists(_ENV_FILE) else None,
            env_file_encoding="utf-8",
            extra="ignore",
        )

        # ── 服务 ──
        host: str = "127.0.0.1"                       # API 监听地址
        port: int = 18081                              # API 服务端口

        # ── 路径 ──
        chan_path: str = _REPO_ROOT                    # chan.py 仓库根目录（引擎与 DataAPI 所在）
        tdx_install_dir: str = r"C:\new_tdx_hd_test"   # 通达信安装目录（改路径只需改这一处/.env）

        # ── 并发 ──
        scan_concurrency: int = 1                      # 批量扫描并发上限（阶段 7 ProcessPool 接入生效）

        # ── 凭据（.env 或环境变量注入；7.3：不落缓存不写日志）──
        tq_account: str = ""                           # 天勤账号（空 = 回退 vipdoc/tq_account.json）
        tq_password: str = ""                          # 天勤密码

        # ── 数据模式（本版补充：引擎行为开关）────────────────────────
        forward_adjust_enabled: bool = True            # 前复权开关
        full_data_mode: bool = False                   # 全量数据模式
        time_truncate_config: dict = {
            "30m": (180, "6个月"),
            "5m": (90, "3个月"),
        }                                              # 时间截断配置（周期: (天数, 说明)）
        debug_cold_start_start_date: str | None = None # 冷启动起始日期（None=不开启）
        debug_cold_start_end_date: str | None = None   # 冷启动结束日期（None=不开启）

        # ── SSE 调试（本版补充）──────────────────────────────────────
        sse_debug: bool = False                        # SSE 推送详细调试日志
        sse_diag: bool = True                          # 双窗口进度条诊断日志

        # ── 派生路径（只读属性，均由 tdx_install_dir 推导）──
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
            """股票名称缓存文件（本版补充）"""
            return os.path.join(self.vipdoc_dir, "stock_names.json")

        @property
        def stock_pe_ttm_file(self) -> str:
            """PE-TTM / 指数归属缓存文件（本版补充）"""
            return os.path.join(self.vipdoc_dir, "stock_pettm_index.json")

        @property
        def float_mc_cache_file(self) -> str:
            """流通市值缓存文件（本版补充）"""
            return os.path.join(self.vipdoc_dir, "stock_float_mc.json")

        @property
        def saved_point_file(self) -> str:
            """手动选点持久化文件（本版补充）"""
            return os.path.join(self.vipdoc_dir, "double_click_dt.csv")

        @property
        def annotations_file(self) -> str:
            """文本标注持久化文件（本版补充）"""
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
                "scan_concurrency": self.scan_concurrency,
                "tq_account": (self.tq_account[:2] + "***") if (redact and self.tq_account) else self.tq_account,
                "tq_password": "***" if (redact and self.tq_password) else self.tq_password,
                "config_source": "pydantic-settings" if _HAVE_PYDANTIC_SETTINGS else "builtin-fallback",
            }

else:

    class AppConfig:
        """降级实现：字段与 pydantic 版一一对应，标量环境变量/.env 覆盖默认值。"""

        host = "127.0.0.1"
        port = 18081
        chan_path = _REPO_ROOT
        tdx_install_dir = r"C:\new_tdx_hd_test"
        scan_concurrency = 1
        tq_account = ""
        tq_password = ""
        forward_adjust_enabled = True
        full_data_mode = False
        time_truncate_config = {"30m": (180, "6个月"), "5m": (90, "3个月")}
        debug_cold_start_start_date = None
        debug_cold_start_end_date = None
        sse_debug = False
        sse_diag = True

        def __init__(self):
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
                    print(f"[AppConfig] 环境变量 {key}={raw!r} 类型非法，使用默认值")
            self._source = "builtin-fallback"

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
            return _REPO_ROOT

        @property
        def frontend_dir(self) -> str:
            return os.path.join(_REPO_ROOT, "Frontend")

        @property
        def last_code_freq_file(self) -> str:
            return os.path.join(self.vipdoc_dir, "last_code_freq.json")

        @property
        def stock_names_cache_file(self) -> str:
            return os.path.join(self.vipdoc_dir, "stock_names.json")

        @property
        def stock_pe_ttm_file(self) -> str:
            return os.path.join(self.vipdoc_dir, "stock_pettm_index.json")

        @property
        def float_mc_cache_file(self) -> str:
            return os.path.join(self.vipdoc_dir, "stock_float_mc.json")

        @property
        def saved_point_file(self) -> str:
            return os.path.join(self.vipdoc_dir, "double_click_dt.csv")

        @property
        def annotations_file(self) -> str:
            return os.path.join(self.vipdoc_dir, "text_annotation.json")

        def load_tq_account(self) -> dict:
            path = os.path.join(self.vipdoc_dir, "tq_account.json")
            if not os.path.exists(path):
                return {}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}

        def as_dict(self, redact: bool = True) -> dict:
            return {
                "host": self.host,
                "port": self.port,
                "chan_path": self.chan_path,
                "tdx_install_dir": self.tdx_install_dir,
                "scan_concurrency": self.scan_concurrency,
                "tq_account": (self.tq_account[:2] + "***") if (redact and self.tq_account) else self.tq_account,
                "tq_password": "***" if (redact and self.tq_password) else self.tq_password,
                "config_source": "builtin-fallback",
            }


_FIELD_TYPES = {
    "HOST": str,
    "PORT": int,
    "CHAN_PATH": str,
    "TDX_INSTALL_DIR": str,
    "SCAN_CONCURRENCY": int,
    "TQ_ACCOUNT": str,
    "TQ_PASSWORD": str,
}

# 全局单例：一处定义、全局引用（方案 7.1）
app_config = AppConfig()

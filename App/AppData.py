# -*- coding: utf-8 -*-
"""
App/AppData.py — 业务数据层
================================================
缓存 / 持久化 / 标注 / 自选股全部收敛于此，持有真实实现与真实状态：
  · 分析结果 LRU 缓存（股票 + 期货）
  · 名称 / 指数归属 / 流通市值 三类惰性缓存
  · PE-TTM **实时层**：K 线页面打开标的时按需取数，**不落盘**（PE 每日随
    行情变动，落盘缓存在不点刷新时必然陈旧）；取数实现由 AppRefresh 注入
    （AppData 不得 import DataAPI，依赖倒置）
  · 手动选点 CSV 持久化（double_click_dt.csv）
  · 上次查看代码/周期（last_code_freq.json）
  · 文字标注（text_annotation.json）
  · 自选股（zxg.blk 读写；存储格式知识自含，与 DataAPI 互不依赖）

依赖方向：
  FrontAPI.py → App/AppOrch.py → App/AppData.py → App/AppConfig.py（单向）

使用方式：
    from App.AppData import app_data
    app_data.cache_put("key", value)
    app_data.get_annotations_for("sh000001", "d")
"""

import collections
import contextlib
import gc
import io
import json
import os
import re
import tempfile
import threading
import time

# 跨进程文件锁：POSIX 用 fcntl.flock，Windows 用 msvcrt.locking（见 file_lock）
try:
    import fcntl
except ImportError:                      # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:                      # pragma: no cover - POSIX
    msvcrt = None

from App.AppConfig import app_config
from App.AppLog import get_logger
log = get_logger(__name__)



# ═══════════════════════════════════════════════════════════════════
# 选点表 schema（持久化格式定义；消费侧经别名共享）
# ═══════════════════════════════════════════════════════════════════
SAVED_POINT_COLUMNS = ["code", "name", "y", "q", "m", "w", "d",
                       "60m", "30m", "15m", "5m", "1m", "15s"]
FREQ_TO_COL = {"y": "y", "q": "q", "m": "m", "w": "w", "d": "d",
               "60m": "60m", "30m": "30m", "15m": "15m", "5m": "5m",
               "1m": "1m", "15s": "15s"}

# 指数映射，用于同花顺自选股 同步 通达信自选股
ZXG_HK_INDEX_MAP = {
    "HS2083": "HZ5017",  # HS2083 同花顺恒生科技指数  27#HZ5017 通达信恒生科技指数K线文件
    "HS2198": "HZ5489",  # HS2198 同花顺恒生创新药指数 27#HZ5489 通达信恒生创新药K线文件
}
ZXG_US_INDEX_MAP = {
    "NBI": "A_NBI",
}
# 同步时，保留不覆盖
# 标准指数在 TDX zxg.blk 中的行格式（SH=1前缀, SZ=0前缀）
TDX_STANDARD_INDICES = {"1000001", "0399001", "1000300", "1000905", "1000852", "0399006", "1000688"}


# 原子落盘的替换重试参数（见 _atomic_replace）
_REPLACE_RETRY = 6
_REPLACE_BACKOFF = 0.02   # 秒，逐次线性放大


def _atomic_replace(tmp_path, path):
    """用 os.replace 原子覆盖目标文件，Windows 冲突时退避重试。

    Windows 特有问题：目标文件正被另一线程以 open() 持句柄读取时，
    os.replace（MoveFileEx）会抛 PermissionError(WinError 5 拒绝访问)。
    这在「刷新线程写 stock_names.json / float_mc_cache.json，同时扫描或
    分析线程在读」的场景下真实发生——POSIX 上同名替换不受影响，故只在
    Windows 暴露。退避重试让读线程有机会释放句柄，而不是直接失败。
    """
    last_err = None
    for attempt in range(_REPLACE_RETRY):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError as exc:   # Windows：目标被持句柄
            last_err = exc
        except OSError as exc:           # 其余瞬时错误（杀软扫描等）
            last_err = exc
        time.sleep(_REPLACE_BACKOFF * (attempt + 1))
    raise last_err


@contextlib.contextmanager
def file_lock(lock_path, *, timeout=10.0, poll=0.02):
    """跨进程文件锁（审计 P1-4：threading.Lock 只在**进程内**有效）。

    zxg.blk 有两个写者跑在**不同进程**——生产 API 进程与独立的同步脚本。
    `_user_store_lock` 是 `threading.Lock`，对另一个进程毫无约束力，正是
    v5 §1.2 自己点名的「最危险的误用」，只是它出现在文件层而没被识别。
    故 .blk 这类跨进程共享文件的读-改-写，必须**额外**加 OS 级文件锁。

    - POSIX：`fcntl.flock`（劝告锁，要求所有写者都遵守同一约定）
    - Windows：`msvcrt.locking`（强制锁，LK_NBLCK 非阻塞 + 轮询退避）

    锁文件与数据文件分离（`*.lock`），锁操作不影响数据文件内容。
    拿不到锁时抛 TimeoutError，让调用方显式失败而非带着错觉继续写。
    """
    dir_name = os.path.dirname(lock_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        deadline = time.time() + timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:
                    # LK_NBLCK 锁 1 字节；区间任意，约定所有写者锁同一位置
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:                     # pragma: no cover - 理论不可达
                    raise RuntimeError("当前平台无可用文件锁实现")
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"[文件锁] 等待超时({timeout}s): {lock_path}")
                time.sleep(poll)
        try:
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        fh.close()


def safe_write_json_file(path, data, *, ensure_ascii=False, indent=None):
    """先写临时文件并校验 JSON 可读，再用 os.replace 覆盖正式文件；失败时保留旧文件。
    （持久化底座：原子写，防断电/中断产生半截文件）"""
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    # 临时文件名唯一化（审计 P2）：原用 path + ".tmp" 固定名，同一目录下
    # 一旦出现两个写者（如 worker 进程落盘），先完成的那个在 finally 里
    # os.remove 会删掉别人刚建的临时文件，导致后者 os.replace 失败或
    # 写出空文件。mkstemp 保证同目录唯一（同目录是 os.replace 原子的前提）。
    _fd, tmp_path = tempfile.mkstemp(dir=dir_name or ".", prefix=".tmp_", suffix=".json")
    os.close(_fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
        with open(tmp_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, type(data)):
            raise ValueError("临时 JSON 文件类型校验失败")
        _atomic_replace(tmp_path, path)
        return True
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _atomic_write_text(path, text, *, encoding="utf-8", newline=None):
    """原子写任意文本：先写同目录临时文件，再 os.replace 覆盖。

    与 safe_write_json_file 同一底座，用于 JSON 之外的文本（CSV / .blk）。
    防两类事故：① 写一半进程退出留下截断文件；② 并发读者读到半截内容。
    """
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    # 临时文件名唯一化（同上，审计 P2）：固定 ".tmp" 名在多写者场景下会被
    # 彼此的 finally: os.remove 误删。
    _fd, tmp_path = tempfile.mkstemp(dir=dir_name or ".", prefix=".tmp_", suffix=".wr")
    os.close(_fd)
    try:
        with open(tmp_path, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
        _atomic_replace(tmp_path, path)
        return True
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def get_annotation_key(code, freq):
    """生成标注缓存的键: {code}_{freq}"""
    return f"{code}_{freq}"


def _read_zxg_blk_file(blk_path):
    """读取通达信 .blk 板块文件，返回股票代码列表（自选股存储格式知识）。

    文件格式：GBK编码，每行一个代码：
      A股 7 位纯数字（前缀 + 6 位代码）/ 港股 31# / 港股指数 27# /
      美股 74# / 美股指数 12#A_
    说明：DataAPI/TdxAPI.py 另持一份同名解析（服务其指数成分读取），
    属「两者互不依赖」的边界代价——各自自含为正确边界，
    不引入顶层中立模块强并。
    """
    if not blk_path or not os.path.exists(blk_path):
        return []
    stocks = []
    try:
        with open(blk_path, "r", encoding="gbk") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if len(line) == 7 and line.isdigit():
                    stocks.append({"prefix": line[0], "code": line[1:7]})
                elif line.startswith("31#") and len(line) == 8:
                    code = line[3:].strip()
                    if code.isdigit():
                        stocks.append({"prefix": "hk", "code": code.zfill(5)})
                elif line.startswith("74#") and len(line) > 3:
                    code = line[3:].strip()
                    if code:
                        stocks.append({"prefix": "us", "code": code})
                elif line.startswith("27#") and len(line) > 3:
                    code = line[3:].strip()
                    if code:
                        stocks.append({"prefix": "hk", "code": code})
                elif line.startswith("12#") and len(line) > 3:
                    code = line[3:].strip()
                    if code:
                        stocks.append({"prefix": "us", "code": code})
    except Exception as e:
        log.error(f"[错误] 读取板块文件失败 {blk_path}: {e}")
    return stocks


def _code_to_zxg_line(code_str):
    """标准格式代码 → zxg.blk 行（自选股写盘格式知识，全量同步/追加写入共用）。

    兼容两个输入源（均为"读取"不同来源，不产生任何旧写法）：
      · 应用层标准写法 market(小写)+code：如 sh600519 / sz000001 / hk00700 / bj430047
      · 外部数据源点号写法：600519.SH / 000001.SZ / 00700.HK / NBI.US（同花顺云同步契约）

    各市场输出（与通达信 zxg.blk 约定一致）：
      A 股：     {前缀}{6位代码}    如 1600519（SH→1 / SZ→0 / BJ→2）
      港股个股：  31#{5位代码}       如 31#00700
      港股指数：  27#{HZ代码}       如 27#HZ5489   （ZXG_HK_INDEX_MAP 映射）
      美股个股：  74#{代码}         如 74#NBI
      美股指数：  12#A_{代码}       如 12#A_NBI    （ZXG_US_INDEX_MAP 映射）
    无法识别返回 None（调用方跳过）。
    """
    c = code_str.strip()
    if not c:
        return None
    # ① 应用层标准/别名经唯一事实源解析（market(小写)+数字代码 / 字母速记）
    try:
        from App import utils as _u
        mkt, bare = _u._get_stock_market_code(c)
    except Exception:
        mkt, bare = None, None
    if mkt and bare and bare.isdigit():
        if mkt in ("sh", "sz", "bj"):
            return {"sh": "1", "sz": "0", "bj": "2"}[mkt] + bare
        if mkt == "hk":
            return "31#" + bare.zfill(5)
        if mkt == "ds":
            return None  # 大数据指数不入自选股
    # ② 外部数据源点号写法（同花顺云同步契约，保留读取）
    cU = c.upper()
    m = re.match(r'^(\d+)\.(SH|SZ|BJ)$', cU)
    if m:
        return {"SH": "1", "SZ": "0", "BJ": "2"}[m.group(2)] + m.group(1)
    m2 = re.match(r'^(\w+)\.(HK|US)$', cU)
    if m2:
        code, market = m2.group(1), m2.group(2)
        if market == "HK":
            if code in ZXG_HK_INDEX_MAP:
                return "27#" + ZXG_HK_INDEX_MAP[code]
            return "31#" + code.zfill(5)
        if code in ZXG_US_INDEX_MAP:
            return "12#" + ZXG_US_INDEX_MAP[code]
        return "74#" + code
    return None


def _read_preserved_zxg_lines(path):
    """读取 zxg.blk 中已有的保留代码（标准指数 + 通达信私有指数 88/188xxxx）。

    全量同步（替换）时这些代码不被覆盖，并保持在自选股列表开头。
    注意：通达信 zxg.blk 中 88xxxx 私有指数带 SH 前缀，格式为 188xxxx。
    """
    if not path or not os.path.exists(path):
        return []
    preserved = []
    try:
        with open(path, "r", encoding="gbk") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line in TDX_STANDARD_INDICES or line.startswith("88") or line.startswith("188"):
                    preserved.append(line)
    except Exception:
        pass
    return preserved


# ════════════════════════════════════════════════════════════════
# 结构化缓存键（消除字符串拼接歧义与漂移）
# ════════════════════════════════════════════════════════════════
# 股票分析缓存（_stocks_analysis_cache）使用结构化元组键：
#   (kind, market, code, freq, date)
# 双窗口键（dual_main/dual_sub）追加第 6 维 impl（independent/legacy，
# 见 _dual_impl_tag），隔离 A/B 两种实现的缓存，防切换开关后串用。
# 天然 hashable、可比较、无分隔符歧义（代码/周期/日期含 "_" / ":" 也不会碰撞）。
# 期货子窗缓存（_futures_analysis_cache）使用字符串键，统一经
# make_futures_sub_key 规范化（symbol 大写入键），消除 AppSSE/AppChart/
# 语义化接口三处拼接的大小写漂移；第三方引擎（BSPointList）按同一
# 格式拼接，兼容不变。单一事实源：所有调用方经本组工厂函数生成键，
# 不手工拼接字符串。
def make_analysis_key(kind, market, code, freq, date):
    """构造结构化分析缓存键（kind: single/dual_main/dual_sub/live）"""
    return (kind, str(market), str(code), str(freq), str(date))


def _dual_impl_tag():
    """股票双窗 A/B 实现标签（dual 缓存 key 的第 6 维）。

    独立(independent)与联立(legacy)两种实现的红框边界语义不同
    （独立=数学换算，联立=KLU.sub_kl_list 真实边界），dual 缓存若
    不区分实现，切换 A/B 开关后会串用另一实现的缓存结果（例：
    legacy 先写缓存，independent 复跑直接命中返回联立输出）。
    语义事实源为 AppEngine._stock_dual_impl（读
    CHAN_STOCK_DUAL_IMPL，非法值回退 independent）；此处仅重复读同
    一环境变量做 key 归一，不 import 引擎，避免循环依赖。
    """
    impl = os.environ.get("CHAN_STOCK_DUAL_IMPL", "independent").strip().lower()
    return impl if impl in ("independent", "legacy") else "independent"


def make_single_key(market, code, freq, date):
    """单窗口分析缓存键（date: 复盘日期 或 "live"）"""
    return make_analysis_key("single", market, code, freq, date)


def make_dual_main_key(market, code, freq, date):
    """双窗口主级别缓存键（追加 A/B 实现维度，independent/legacy 隔离）"""
    return make_analysis_key("dual_main", market, code, freq, date) + (_dual_impl_tag(),)


def make_dual_sub_key(market, code, freq, date):
    """双窗口子级别缓存键（追加 A/B 实现维度，independent/legacy 隔离）"""
    return make_analysis_key("dual_sub", market, code, freq, date) + (_dual_impl_tag(),)


def make_live_key(market, code, freq):
    """实时（非复盘）单窗口缓存键"""
    return make_single_key(market, code, freq, "live")


def make_futures_sub_key(symbol, sub_freq):
    """期货子窗口缓存键（symbol 统一大写入键，消除大小写漂移）"""
    return f"{symbol.upper()}:{sub_freq}"


def make_futures_sub_key_from_code(chan_code, sub_freq):
    """由 CChan.code（形如 "SYMBOL:freq_sec"，含周期后缀）生成期货下窗缓存键。

    读侧（BSPointList 区间套）专用：CChan.code 带 ":freq_sec" 周期后缀，
    先 rsplit 还原纯 symbol，再委托 make_futures_sub_key 工厂，与写侧
    （AppSSE.set_futures_sub_chan）同源，避免手工拼接造成 key 漂移
    （P0-2 根因：原先直接以 chan_code 拼接得 "SYMBOL:freq_sec:sub_freq"，
    与写侧 "SYMBOL:sub_freq" 永不相等，区间套 100% 静默失效）。
    """
    symbol = chan_code.rsplit(":", 1)[0]
    return make_futures_sub_key(symbol, sub_freq)


# ═══════════════════════════════════════════════════════════════════
# 通达信研究行业 X代码 ↔ 881代码 映射 · **单一权威源（本文件不含数据）**
#
# 权威源 = 本机通达信客户端文件
#     <TDX_INSTALL_DIR>/T0002/hq_cache/tdxzs3.cfg
# GBK 文本，`|` 分隔：名称|881代码|12|1|0|X代码（第 6 列即 X 行业码）。
# 该文件由通达信「盘后数据下载」随 zhb.zip 自动落盘：服务端重构行业树时
# 内容才变、客户端随之更新，即「这台机器上的通达信当前认哪些行业指数」的
# 权威定义 —— 本仓库无需、也不应再冻结副本。
#
# 沿革：原内嵌 470 条快照自官方 PDF《板块指数和行业分类》3.6 节提取，
# 2026-09-11 实测已漂移 12 处（补 4 / 删 7 / 改 1），且漂移症状**全部不报错**
# （下拉少几项、失效码返回残留票、名称整体错位一格），故整体删除本块数据。
#
# 取不到权威源时**硬失败**（RuntimeError，含可执行指引），不静默降级 ——
# 与本仓库『禁止静默降级』约定一致：宁可让『板块指数2/3』报错，也不要拿
# 过期快照冒充权威数据。恢复方式：通达信客户端 → 盘后数据下载
# （7709 协议对 tdxzs3.cfg 返回 meta=0 取不到，但可由可下载的 zhb.zip 解出）。
# ═══════════════════════════════════════════════════════════════════
_TDXZS3_RELPATH = ("T0002", "hq_cache", "tdxzs3.cfg")
_TDXZS3_CODE_RE = re.compile(r"^881\d{3}$")
_TDXZS3_X_RE = re.compile(r"^X\d+$")
# 合法性下限：全网正常约 467 条（一级 30 / 二级 128 / 三级 309）。明显残缺
# （截断 / 损坏）一律弃用 —— 半张表静默生效导致少一半行业，比报错更糟。
_TDXZS3_MIN_ROWS = 300
_TDXZS3_MISSING_HINT = (
    "缺少通达信行业映射权威源文件：{path}"
    "（该文件由通达信『盘后数据下载』随 zhb.zip 落盘；本仓库已不再内嵌映射快照。"
    "处理：通达信 → 盘后数据下载后重启本程序）")
# 解析结果缓存：键含 (路径, mtime, size)，文件一更新即自动失效重读。
_TDXHY_MAP_LOCK = threading.Lock()
_TDXHY_MAP_CACHE = None
_TDXHY_MAP_STAMP = None


def _tdxzs3_path():
    """权威源绝对路径；未配置 TDX_INSTALL_DIR 时返回空串"""
    root = app_config.tdx_install_dir
    return os.path.join(root, *_TDXZS3_RELPATH) if root else ""


def _read_tdxzs3_cfg(path):
    """解析权威源 → (x_to_881, to_x)；缺失 / 残缺一律抛 RuntimeError（硬失败）

    x_to_881: {X代码:  (名称, 881代码)}
    to_x    : {881代码: (X代码, 名称)}
    非 881 行（880 板块、地区等）由代码正则自然跳过。
    """
    if not path or not os.path.exists(path):
        raise RuntimeError(_TDXZS3_MISSING_HINT.format(path=path or "<未配置 TDX_INSTALL_DIR>"))
    with open(path, "rb") as fh:
        raw = fh.read()
    x_to_881, to_x = {}, {}
    for line in raw.decode("gbk", errors="ignore").splitlines():
        parts = line.split("|")
        if len(parts) < 6:
            continue
        name = parts[0].strip()
        code = parts[1].strip()
        x_code = parts[5].strip()
        if not name or not _TDXZS3_CODE_RE.match(code) or not _TDXZS3_X_RE.match(x_code):
            continue
        if code in to_x:                      # 同码重复行：以首行为准
            continue
        x_to_881[x_code] = (name, code)
        to_x[code] = (x_code, name)
    if len(x_to_881) < _TDXZS3_MIN_ROWS:
        raise RuntimeError(
            f"行业映射权威源条目过少（{len(x_to_881)} < {_TDXZS3_MIN_ROWS}），"
            f"疑为残缺 / 截断文件：{path}\n"
            f"  处理：通达信 → 盘后数据下载（重新落盘该文件）后重启本程序。")
    return x_to_881, to_x


def _load_tdxhy_mapping():
    """唯一加载口：读本地权威源（mtime/size 缓存）。失败抛 RuntimeError

    不做任何兜底、不返回空表：调用方要么拿到真实数据，要么拿到带指引的异常。
    """
    global _TDXHY_MAP_CACHE, _TDXHY_MAP_STAMP
    path = _tdxzs3_path()
    stamp = None
    if path:
        try:
            st = os.stat(path)
            stamp = (path, st.st_mtime, st.st_size)
        except OSError:
            stamp = None
    with _TDXHY_MAP_LOCK:
        if _TDXHY_MAP_CACHE is not None and stamp == _TDXHY_MAP_STAMP:
            return _TDXHY_MAP_CACHE
        loaded = _read_tdxzs3_cfg(path)       # 失败不缓存：修好文件后即可生效
        _TDXHY_MAP_CACHE = loaded
        _TDXHY_MAP_STAMP = stamp
    lv = collections.Counter(len(x) - 1 for x in loaded[0])
    log.info(f"[行业映射] 权威源 {os.path.basename(path)}: {len(loaded[0])} 条"
             f"（一级{lv.get(2, 0)}/二级{lv.get(4, 0)}/三级{lv.get(6, 0)}）")
    return loaded


class AppData:
    """业务数据层：缓存 / 持久化 / 标注 / 自选股（真实实现）"""

    MAX_CACHE_SIZE = 50  # 最多缓存 50 个 (股票, 周期) 组合（共享 LRU 池总上限）
    # 双窗结构化缓存键（dual_main/dual_sub）在共享池内单独限额：
    # 双窗条目更重（两键各含 CChan + records）且不常用，超限优先淘汰
    # 最旧 dual 键，不挤占常用单窗口缓存（单一事实源 AppConfig）。
    MAX_DUAL_CACHE_KEYS = app_config.max_dual_cache_keys
    # 双窗运行时下窗 CChan 缓存上限（LRU）：超限淘汰最旧
    # （切回单窗不主动清，保留快速切回），防切换标的时旧 CChan 残留泄漏。
    MAX_STOCKS_SUB_CHAN = app_config.max_stocks_sub_chan

    def __init__(self):
        # ══ 锁集合（一把锁 = 一个资源，锁名即资源名）══════════════════
        # 设计依据见 Docs/chan_lock_design_v5.md。要点：
        #   _stocks_cache_lock    护「股票分析结果 + 股票下窗」内存缓存
        #                        （REST 线程，毫秒级访问）
        #   _futures_cache_lock  护期货内存缓存。独立成锁：访问者是 SSE
        #                        常驻线程（高频写、生命周期以分钟计），
        #                        不应与 REST 缓存操作抢同一把锁
        #   _user_store_lock     护全部「用户持久化数据」的读-改-写：
        #                        标注 / 选点 / 上次查看 / 流通市值 / 自选股。
        #                        合并为一把的理由：都是毫秒级 RMW，且消除
        #                        「标注→缓存」与「缓存→选点」跨锁顺序死锁
        #   _meta_cache_lock     护「名称 / PE-TTM / 指数归属」三张元数据表
        #                        （v5 漏登记，审计 P1-1~P1-3）。写者只有刷新
        #                        线程，读者是全部 REST / 扫描线程，故独立成
        #                        锁而非并入 _user_store_lock（后者要护文件
        #                        I/O，持有时间以毫秒计，不该被元数据拖住）。
        # 均为 RLock：读-改-写内部可能嵌套同类操作。
        #
        # ── 锁顺序（必须遵守，否则跨锁死锁）────────────────────────
        #   _user_store_lock → _meta_cache_lock → _stocks_cache_lock
        #                                       → _futures_cache_lock
        # _meta_cache_lock 是叶子锁：持有它的代码一律不得再取 _user_store_lock。
        self._stocks_cache_lock = threading.RLock()
        self._futures_cache_lock = threading.RLock()
        self._user_store_lock = threading.RLock()
        self._meta_cache_lock = threading.RLock()
        # 期货下窗 CChan「对象图锁」登记表：key → RLock。
        # 缓存 dict 由 _futures_cache_lock 保护，但 dict 里存的是**活着的
        # CChan**，SSE 每根K线 step_load → do_init 会就地清空重建 kl_datas。
        # 容器锁护不住对象图，故按 key 单列一把锁，让「SSE 重建」与
        # 「读取方遍历」互斥（审计 P0-2）。由 _futures_cache_lock 护本表。
        self._futures_chan_locks = {}

        # ── 分析结果缓存 ──
        self._stocks_analysis_cache = collections.OrderedDict()
        self._futures_analysis_cache = {}
        # 股票双窗独立化：下窗 CChan 运行时缓存（键见 stocks_sub_cache_key）
        self._stocks_sub_chan_cache = {}

        # ── 名称 / PE / 归属 / 流通市值（惰性加载标志 + 字典）──
        self._names = {}
        self._names_loaded = False
        # PE-TTM 表：**不再从落盘缓存加载**，由实时层（K 线页面按需取数）填充。
        # 仍保留本表作为「点查源」：写入一律走 update_pe_ttm（持 _meta_cache_lock），
        # 遍历一律走 pe_snapshot()——快照用例也直接注入本表以冻结基线。
        self._pe = {}
        # _pe_loaded 的语义是「PE 表已由外部提供（落盘缓存 / 用例注入）」，
        # 为真时**禁用实时层**（纯点查）。快照用例注入确定性参考表后置位。
        # 生产路径恒为 False（不再从缓存加载 PE），故每次 get_pe_ttm 都实时取数。
        self._pe_loaded = False
        self._belong = {}
        self._belong_loaded = False
        self._float_mc = {}
        self._float_mc_loaded = False
        self._float_mc_saved_at = None   # 本地缓存写入时刻（time.time()），用于"接口失败且缓存过期"告警

        # ── PE-TTM 实时层（K 线页面「打开即取」）──
        # _pe_live_lock：single-flight 门。**独立叶子锁**，不参与
        #   _user_store_lock → _meta_cache_lock → _stocks_cache_lock 的顺序——
        #   持有它期间不得再取任何其它锁（联网取数约 1.3s）；写入时由
        #   update_pe_ttm 去取 _meta_cache_lock，两者**不嵌套持有**，无死锁路径。
        # _pe_live_fetcher：取数实现，由 AppRefresh 在模块导入时注入
        #   （依赖倒置：AppData 不得 import DataAPI，见 phase5 守卫 ④b）。
        self._pe_live_lock = threading.Lock()
        self._pe_live_fetcher = None

        # ── 标注 ──
        self._annotations = {}   # { "code_freq": [ {date,text,y_offset}, ... ] }
        self._annotations_loaded = False

        # ── 启动时加载（实例化即加载选点表与标注）──
        self._saved_point_times = self.load_saved_point_times()
        self.load_annotations()

    # ════════════════════════════════════════════════════════════════
    # 路径（AppConfig 单一事实源的派生属性）
    # ════════════════════════════════════════════════════════════════
    @property
    def saved_point_file(self):
        return app_config.saved_point_file

    @property
    def annotations_file(self):
        return app_config.annotations_file

    @property
    def last_code_freq_file(self):
        return app_config.last_code_freq_file

    @property
    def stock_names_cache_file(self):
        return app_config.stock_names_cache_file

    @property
    def stock_index_belong_file(self):
        return app_config.stock_index_belong_file

    @property
    def legacy_stock_pe_ttm_file(self):
        """旧版「PE-TTM + 指数归属」合并缓存路径（仅供一次性迁移读取）。"""
        return app_config.legacy_stock_pe_ttm_file

    @property
    def stock_pettm_file(self):
        """A 股 PE-TTM 全量落盘镜像（见 AppConfig.stock_pettm_file）。"""
        return app_config.stock_pettm_file

    @property
    def float_mc_cache_file(self):
        return app_config.float_mc_cache_file

    # ════════════════════════════════════════════════════════════════
    # 状态只读出口（获取侧刷新函数经此共享同一对象）
    # ════════════════════════════════════════════════════════════════
    @property
    def stocks_cache_lock(self):
        return self._stocks_cache_lock

    @property
    def futures_cache_lock(self):
        return self._futures_cache_lock

    @property
    def user_store_lock(self):
        return self._user_store_lock

    @property
    def stocks_analysis_cache(self):
        return self._stocks_analysis_cache

    @property
    def futures_analysis_cache(self):
        return self._futures_analysis_cache

    @property
    def names_cache(self):
        return self._names

    @property
    def pe_cache(self):
        return self._pe

    @property
    def belong_cache(self):
        return self._belong

    @property
    def float_mc_cache(self):
        return self._float_mc

    @property
    def float_mc_loaded(self):
        return self._float_mc_loaded

    @property
    def saved_point_times(self):
        return self._saved_point_times

    @property
    def annotations_cache(self):
        return self._annotations

    def freq_to_col(self, freq):
        """周期 → CSV 列名（选点表；无映射返回 None）"""
        return FREQ_TO_COL.get(freq)

    # ── 行业映射单一加载 ──────────────────────────────────────
    #    权威源为本机通达信文件 T0002/hq_cache/tdxzs3.cfg（**无内嵌快照**）；
    #    本方法是唯一加载点，DataAPI 侧经 set_tdx_hy_mapping 注入（互不依赖）。

    def load_tdxhy_mapping(self):
        """加载通达信研究行业映射，返回 (x_to_881, to_x) 二元组

        单一权威源：`<TDX_INSTALL_DIR>/T0002/hq_cache/tdxzs3.cfg`，由通达信
        客户端「盘后数据下载」随 zhb.zip 自动落盘、服务端重构行业树时自更新。
        本仓库**不再内嵌任何映射快照**（原 470 条快照实测漂移 12 处，已删除）。

        取不到权威源时硬失败（RuntimeError，含可执行指引），不静默降级：
        宁可让『板块指数2/3』列表报错，也不拿过期快照冒充权威数据。
        结果按 (x_to_881, to_x) 次序返回，可直接 set_tdx_hy_mapping(*result)。
        """
        return _load_tdxhy_mapping()

    def tdxhy_l2_indices(self):
        """返回所有二级行业板块指数列表（X+4位代码对应的881yyy），共125个"""
        x_to_881, _ = self.load_tdxhy_mapping()
        result = []
        for x_code, (name, code_881) in x_to_881.items():
            digits = x_code[1:]  # 去掉X
            if len(digits) == 4:  # X+4位 = 二级行业
                result.append({"code": code_881, "prefix": "1", "name": name})
        return result

    def tdxhy_l3_indices(self):
        """返回所有三级行业板块指数列表（X+6位代码对应的881yyy），共315个"""
        x_to_881, _ = self.load_tdxhy_mapping()
        result = []
        for x_code, (name, code_881) in x_to_881.items():
            digits = x_code[1:]  # 去掉X
            if len(digits) == 6:  # X+6位 = 三级行业
                result.append({"code": code_881, "prefix": "1", "name": name})
        return result

    # ════════════════════════════════════════════════════════════════
    # 统一缓存（LRU 50 条；股票扫描与冷启动共用；双窗键单独限额）
    # ════════════════════════════════════════════════════════════════
    @staticmethod
    def _is_dual_key(key):
        """双窗结构化缓存键判定（第 1 维 kind ∈ {dual_main, dual_sub}）"""
        return isinstance(key, tuple) and bool(key) and key[0] in ("dual_main", "dual_sub")

    def _gc_outside_lock(self):
        """（内部）在**不持有任何锁**时执行全量 GC —— 维度 5.4「临界区过重」

        被淘汰的是 CChan 这种**重量级对象图**（跨代引用、含大量 K 线单元），
        因此淘汰路径需要一次 gc.collect() 才能真正回收。但 gc.collect()
        是 stop-the-world：**在临界区内执行，等于让所有等待本锁的线程一起
        多等一个 GC 周期**（阻塞放大）。淘汰越频繁，这个放大越明显。

        故本类所有淘汰路径统一改为：锁内只置「需要 GC」标记，出锁后再调用
        本方法。注意此时已不在临界区，可能已有别的线程再次淘汰——重复 GC
        只是多花一点时间，不影响正确性。
        """
        try:
            gc.collect()
        except Exception as _exc:  # noqa: BLE001 —— GC 失败不得阻断业务路径
            log.warning(f"[内存] GC 异常: {type(_exc).__name__}: {_exc}")

    def _evict_dual_overflow_locked(self):
        """（内部，须持 _stocks_cache_lock）双窗键单独限额淘汰。

        写入新 dual 键前调用：池内现存 dual 键数已达 MAX_DUAL_CACHE_KEYS
        时，按插入序（最旧优先）淘汰，直到腾出空位。dict 保序 + cache_get
        命中移尾，序即 LRU 新旧序。单窗键不受影响。

        返回：是否发生了淘汰 —— 调用方据此在**出锁后**补一次 GC
        （见 _gc_outside_lock 与审计 X4′）。
        """
        evicted = False
        dual_keys = [k for k in self._stocks_analysis_cache if self._is_dual_key(k)]
        while len(dual_keys) >= self.MAX_DUAL_CACHE_KEYS:
            oldest = dual_keys.pop(0)
            self._stocks_analysis_cache.pop(oldest, None)
            evicted = True
            log.info(f"[内存] 双窗缓存达上限({self.MAX_DUAL_CACHE_KEYS})，淘汰最旧双窗条目: {oldest}")
        return evicted

    def cache_put(self, key, value):
        """写入缓存，超出上限时淘汰最旧的条目（LRU语义）。
        内存由 LRU 50 条上限 + 双窗键单独限额 + 扫描时逐只释放非买点缓存
        共同控制：dual_* 键超 MAX_DUAL_CACHE_KEYS 时优先淘汰最旧 dual 键
        （双窗不常用且条目重，不挤占常用单窗口缓存）。"""
        with self._stocks_cache_lock:
            evicted = self._cache_put_locked(key, value)
        if evicted:
            # 维度 5.4：GC 必须出锁后做，避免 stop-the-world 阻塞放大
            self._gc_outside_lock()

    def _cache_put_locked(self, key, value):
        """（内部，须持 _stocks_cache_lock）写入并维护 LRU/容量

        返回：是否发生了淘汰 —— 调用方据此在**出锁后**补一次 GC
        （见 _gc_outside_lock 与审计 X4′：GC 不得在临界区内执行）。
        """
        evicted = False
        if key in self._stocks_analysis_cache:
            del self._stocks_analysis_cache[key]  # 移到末尾
        else:
            # 新键入池前的容量控制：dual 键先过单独限额，再过池总量限
            if self._is_dual_key(key):
                evicted = self._evict_dual_overflow_locked()
            if len(self._stocks_analysis_cache) >= self.MAX_CACHE_SIZE:
                oldest_key = next(iter(self._stocks_analysis_cache))
                self._stocks_analysis_cache.pop(oldest_key)
                evicted = True
                log.info(f"[内存] 缓存已满({self.MAX_CACHE_SIZE})，淘汰: {oldest_key}")
        self._stocks_analysis_cache[key] = value
        return evicted

    def cache_update(self, key, **fields):
        """在**同一把锁内**完成缓存条目的读-改-写（审计 P0-3）。

        原先调用方是 `_cache_get → 锁外改字段 → _cache_put`：每一步单独看
        都持了锁，但整段不是原子的。两个并发请求各取到同一个 dict、各自
        补字段、先后 put 回去，后写的会整体覆盖先写的——实测最终缓存只剩
        `['records']`，另一个线程写入的 `chan` 被静默吞掉。

        典型用法（替代 get→改→put）：
            app_data.cache_update(key, result=main_result, chan=chan)
        """
        with self._stocks_cache_lock:
            entry = self._stocks_analysis_cache.get(key)
            if entry is None:
                entry = {}
            entry.update(fields)
            evicted = self._cache_put_locked(key, entry)
        if evicted:
            self._gc_outside_lock()
        return entry

    def cache_get(self, key):
        """读取缓存，命中时移到末尾（LRU语义）"""
        with self._stocks_cache_lock:
            if key not in self._stocks_analysis_cache:
                return None
            value = self._stocks_analysis_cache.pop(key)
            self._stocks_analysis_cache[key] = value
        return value

    def cache_remove(self, key):
        """从缓存中删除指定条目（不触发 GC，由调用方在适当时机统一回收）"""
        with self._stocks_cache_lock:
            if key in self._stocks_analysis_cache:
                del self._stocks_analysis_cache[key]

    def stocks_cache_clear(self):
        """清空股票分析 LRU 缓存（持 _stocks_cache_lock，线程安全），返回清除条数

        P0-3 后调用方为：下载完成回调（AppDownload.stocks_cache_clear →
        on_finish，该回调已随「盘后下载」功能移除）；扫描面板关闭
        （Scanner.clear_cache）已不再清池（返回 cleared=0，缓存由 LRU
        自然淘汰）。与 futures_cache_clear 同模式。
        """
        with self._stocks_cache_lock:
            n = len(self._stocks_analysis_cache)
            self._stocks_analysis_cache.clear()
            return n

    # ── 期货缓存（独立锁 _futures_cache_lock）──────────────────────
    #   访问者是 SSE 常驻线程（写，生命周期以分钟计）与 REST 线程
    #   （读 /api/red_range_zs、清 /api/futures/cleanup）。原先三个入口
    #   三套规矩（写无锁 / 读靠外层 _ENGINE_LOCK / clear 用 _stocks_cache_lock），
    #   现统一由本锁覆盖，且不与 REST 的 stocks_cache_lock 争抢。
    def futures_cache_get(self, key):
        """期货分析缓存读（独立于股票 LRU，键形如 "KQ.m@SHFE.rb:1m"）"""
        with self._futures_cache_lock:
            return self._futures_analysis_cache.get(key)

    def futures_cache_put(self, key, value):
        """期货分析缓存写（SSE 双窗口下窗 chan 供 /api/red_range_zs 访问）"""
        with self._futures_cache_lock:
            self._futures_analysis_cache[key] = value

    def futures_cache_pop(self, key, default=None):
        """期货分析缓存失效（子级别切换时释放旧中间状态）

        审计 P2（生命周期）：连带回收该 key 的**对象图锁**。_futures_chan_locks
        原本只增不删——切换一次子级别就新建一把 RLock 留着，长跑下与「缓存
        条目已释放」形成不对称增长（锁对象本身很轻，但登记表无界增长说明
        生命周期没人管，且同名 key 复用时会拿到**新**锁而与旧持有者失去互斥）。
        故逐条弹出时一并摘除，锁的生命周期与缓存条目对齐。
        """
        with self._futures_cache_lock:
            value = self._futures_analysis_cache.pop(key, default)
            self._futures_chan_locks.pop(key, None)
            return value

    def futures_cache_clear(self):
        """清空全部期货分析缓存（持 _futures_cache_lock，线程安全）

        期货切股票时由 _cleanup_all_futures_data 调用：双窗下窗 chan
        若残留，后续区间套 check_nested_diver / compute_red_range_zs
        经 futures_cache_get 会读到过期中间状态，必须整池清空。

        注：清理只对「当前无 SSE 连接写入」成立；正在推送的连接在下一
        帧重新 set_futures_sub_chan 会自行补回，不会读到半清状态。

        审计 P2（生命周期）：整池清空时一并清空**对象图锁表**
        _futures_chan_locks，与缓存条目生命周期对齐（理由见
        futures_cache_pop）。注意此时若有并发读取方正持有某把锁，
        `dict.clear()` 只是摘除登记表的引用，不会销毁仍在使用的锁对象——
        持有者照常工作，重建时再取新锁，故不影响正确性。
        """
        with self._futures_cache_lock:
            self._futures_analysis_cache.clear()
            self._futures_chan_locks.clear()

    # ── 语义化子窗接口（key 规则内聚于数据层，调用方不手工拼接  ──
    #    "{SYMBOL}:{sub_freq}"，大小写规则单一事实源）
    def set_futures_sub_chan(self, symbol, sub_freq, chan):
        """写入期货子窗口 CChan（symbol 统一大写入键，供 /api/red_range_zs 访问）"""
        return self.futures_cache_put(make_futures_sub_key(symbol, sub_freq), chan)

    def get_futures_sub_chan(self, symbol, sub_freq):
        """读取期货子窗口 CChan（无则返回 None；symbol 大小写不敏感）"""
        return self.futures_cache_get(make_futures_sub_key(symbol, sub_freq))

    def pop_futures_sub_chan(self, symbol, sub_freq):
        """弹出并删除期货子窗口 CChan（SSE 连接关闭 / 子级别切换时释放）"""
        return self.futures_cache_pop(make_futures_sub_key(symbol, sub_freq), None)

    # ── 期货下窗 CChan 的「对象图锁」（审计 P0-2）────────────────
    #   v5 的三问在**容器**这一层是完备的，但缓存里存的不是容器，是**一张
    #   活着的对象图**：SSE 线程每根K线 _drain_chan → step_load → do_init
    #   会把 chan.kl_datas 整个替换成新的空 CKLine_List（Chan.py:90），
    #   再逐根回填。_futures_cache_lock 只护住了「取指针」这一瞬间，护不住
    #   读取方拿到指针后在锁外的遍历——正好撞在 do_init 之后、回填之前就
    #   读到空 bi_list，表现为「红框内无完整笔」「下窗缓存已过期」。
    #
    #   修法二选一：① 发布不可变快照（改动大）；② 让「SSE 重建」与
    #   「读取方遍历」互斥。这里取 ②，并把读取方的临界区压到最小——
    #   只在锁内做 list(bi_list) 浅拷贝（do_init 是整体替换，旧的 CBi
    #   对象不再被引用、不会被就地改写，故浅拷贝即等价于不可变快照）。
    def _futures_chan_lock_for_key(self, cache_key):
        """（内部）按缓存 key 取（或建）对象图锁；登记表由 _futures_cache_lock 护。

        写侧（`futures_sub_chan_lock(symbol, sub_freq)`）与读侧
        （`futures_sub_chan_guarded_by_key(cache_key)`）只要解析出**同一个
        key**，拿到的就是同一把锁，两侧自然互斥。
        """
        with self._futures_cache_lock:
            lk = self._futures_chan_locks.get(cache_key)
            if lk is None:
                lk = threading.RLock()
                self._futures_chan_locks[cache_key] = lk
            return lk

    def futures_sub_chan_lock(self, symbol, sub_freq):
        """取该下窗 key 对应的对象图锁（同一 key 恒定返回同一把 RLock）"""
        return self._futures_chan_lock_for_key(make_futures_sub_key(symbol, sub_freq))

    @contextlib.contextmanager
    def futures_sub_chan_guarded_by_key(self, cache_key):
        """按**缓存 key**取期货下窗 CChan，并在整个使用期内持对象图锁。

        读取方手里往往只有 key（如 BSPointList 经
        `make_futures_sub_key_from_code` 拼出），故单列此入口；它与
        `futures_sub_chan_guarded(symbol, sub_freq)` 共用同一把按 key 分配
        的锁，读写两侧自然互斥。
        """
        lk = self._futures_chan_lock_for_key(cache_key)
        with lk:
            with self._futures_cache_lock:
                yield self._futures_analysis_cache.get(cache_key)

    @contextlib.contextmanager
    def futures_sub_chan_guarded(self, symbol, sub_freq):
        """取出期货下窗 CChan，并在**整个使用期内**持有该对象图的锁。

        用法（临界区只包住取对象与浅拷贝，别在锁内做重计算）：
            with app_data.futures_sub_chan_guarded(sym, sub_freq) as sub_chan:
                if sub_chan is None:
                    ...
                bi_list = list(sub_chan[sub_kl_type].bi_list)   # 快照后出锁
        """
        with self.futures_sub_chan_lock(symbol, sub_freq):
            yield self.get_futures_sub_chan(symbol, sub_freq)

    @contextlib.contextmanager
    def stocks_sub_chan_guarded(self, chan_code, sub_freq):
        """股票下窗 CChan 的同类守卫（与期货侧同一套语义）。

        股票侧下窗发布后不再被持续改写（每次分析整条替换），风险低于期货，
        但「整条替换」本身仍可能与读取方的遍历重叠，故一并纳入保护。
        """
        with self._stocks_cache_lock:
            yield self.stocks_sub_cache_get(chan_code, sub_freq)

    # ── 股票双窗独立化 ──────────────────────────────────────────
    #    仿期货子窗缓存建「股票下窗 CChan 运行时缓存」：
    #      · 写入方：_analyze_stock_internal 独立双窗路径（先建下窗再建上窗，
    #        保证上窗 bsp 计算的区间套 check_nested_diver 能整读到完整下窗）；
    #      · 读取方：check_nested_diver（区间套）与 compute_red_range_zs
    #        （红框中枢，读独立下窗；miss 抛错，对齐期货语义）。
    #    键不带复盘日期后缀（运行时态，随每次双窗重建覆盖），
    #    与 dual_sub 结构化缓存（带 date_suffix，存 result/records）职责分离。
    def stocks_sub_cache_key(self, chan_code, sub_freq):
        """股票下窗运行时缓存键：标准 market(小写)+code，无连接符、不大写（运行时态，随双窗重建覆盖）"""
        return f"{chan_code}:{sub_freq}"

    def stocks_sub_cache_get(self, chan_code, sub_freq):
        """读取股票下窗 CChan（无则返回 None；命中移到末尾=LRU 语义）"""
        key = self.stocks_sub_cache_key(chan_code, sub_freq)
        with self._stocks_cache_lock:
            chan = self._stocks_sub_chan_cache.get(key)
            if chan is not None:
                # LRU：命中移到末尾（dict 保序，插入序即新旧序）
                del self._stocks_sub_chan_cache[key]
                self._stocks_sub_chan_cache[key] = chan
        return chan

    def stocks_sub_cache_put(self, chan_code, sub_freq, chan):
        """写入股票下窗 CChan（同名键覆盖 = 双窗重建即刷新运行时态；
        超 MAX_STOCKS_SUB_CHAN 淘汰最旧，切换标的残留的旧 CChan 不泄漏）"""
        key = self.stocks_sub_cache_key(chan_code, sub_freq)
        with self._stocks_cache_lock:
            evicted = False
            if key in self._stocks_sub_chan_cache:
                del self._stocks_sub_chan_cache[key]
            elif len(self._stocks_sub_chan_cache) >= self.MAX_STOCKS_SUB_CHAN:
                oldest = next(iter(self._stocks_sub_chan_cache))
                self._stocks_sub_chan_cache.pop(oldest)
                evicted = True
                log.info(f"[内存] 运行时下窗缓存达上限({self.MAX_STOCKS_SUB_CHAN})，淘汰: {oldest}")
            self._stocks_sub_chan_cache[key] = chan
        if evicted:
            # 维度 5.4：同上，GC 出锁后再做
            self._gc_outside_lock()

    def stocks_sub_cache_pop(self, chan_code, sub_freq):
        """弹出并删除股票下窗 CChan（切换标的/下窗周期时释放旧中间状态）"""
        with self._stocks_cache_lock:
            return self._stocks_sub_chan_cache.pop(self.stocks_sub_cache_key(chan_code, sub_freq), None)


    # ════════════════════════════════════════════════════════════════
    # 股票名称缓存
    # ════════════════════════════════════════════════════════════════
    def get_stock_name(self, market, code):
        """获取股票名称。从本地缓存文件读取，缓存不存在则返回None。
        港股5位代码（如00700）和A股6位代码（如000700）是不同证券，绝不互相回退。"""
        if market == "ds" and code == "932000":
            return "中证2000"
        if market == "hk" and code.upper() in ("HSTECH", "HSIDI"):
            return {"HSTECH": "恒生科技指数", "HSIDI": "恒生创新药指数"}[code.upper()]
        self.load_stock_names_from_cache_file()
        compound_key = market + code
        info = self._names.get(compound_key)
        if info and isinstance(info, dict):
            name = info.get("name", "")
            if name:
                return name
        if info and isinstance(info, str) and info:
            return info
        return None

    def load_stock_names_from_cache_file(self):
        """从 stock_names.json 缓存文件加载股票名称到内存。
        返回加载的记录数，文件不存在则返回0。
        自动将纯数字键（旧缓存格式）转换为 market+code 复合键。

        写入段持 _meta_cache_lock：并发首次调用只解析一次，且不与刷新
        线程的 replace_names 交错（审计 P1-2）。
        """
        if self._names_loaded:
            return len(self._names)
        if not os.path.exists(self.stock_names_cache_file):
            return 0
        try:
            with open(self.stock_names_cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 缓存文件兼容：将纯数字键（如 "000001"）转换为复合键（如 "sh000001"）
                migrated = {}
                for key, info in data.items():
                    if isinstance(info, dict) and "market" in info and info["market"]:
                        mkt = info["market"]
                        # 纯数字键（旧缓存格式）→ 复合键
                        if key.isdigit():
                            new_key = mkt + key
                        else:
                            new_key = key
                        migrated[new_key] = info
                    else:
                        migrated[key] = info
                with self._meta_cache_lock:
                    if self._names_loaded:      # 双检锁：并发只解析一次
                        return len(self._names)
                    self._names.update(migrated)
                    self._names_loaded = True
                log.info(f"[信息] 从缓存文件加载股票名称: {len(self._names)}只")
                return len(self._names)
        except Exception as e:
            log.warning(f"[警告] 读取股票名称缓存失败: {e}")
        return 0

    def names_snapshot(self):
        """返回名称表**快照**（审计 P1-2，遍历专用）。

        点查（.get / `in`）在 CPython 下是原子的，用别名直接查没问题；
        但**遍历**必须与写者互斥。刷新线程的 replace_names 是
        clear()+update()：读者遍历到一半会抛「dictionary changed size
        during iteration」；更隐蔽的是新表条数**恰好相同**时会绕过 CPython
        的 ma_used 检查，不抛异常而**静默串表**（旧表残条 + 新表新条），
        搜索结果无声错乱。故凡遍历一律经本快照。
        """
        with self._meta_cache_lock:
            return dict(self._names)

    def pe_snapshot(self):
        """PE-TTM 表快照（同 names_snapshot：遍历专用）"""
        with self._meta_cache_lock:
            return dict(self._pe)

    def belong_snapshot(self):
        """指数归属表快照（同 names_snapshot：遍历专用）"""
        with self._meta_cache_lock:
            return dict(self._belong)

    def update_pe_ttm(self, mapping):
        """批量写入 PE-TTM（审计 P1：写者收口到锁内，与遍历读者互斥）

        原先刷新线程经模块级别名 `_pe_ttm_cache[k] = v` 裸写共享 dict：
        写是「点查 + 下标赋值」两步，读者若在两次操作之间整表遍历
        （set(d.keys()) / for k in d），CPython 会抛「dictionary changed
        size during iteration」；条数恰好不变时更会**不抛异常而静默串表**。
        整段 RMW 收进本方法后，与 pe_snapshot() 共用 _meta_cache_lock，
        读者拿到的一定是自洽的表。

        返回：新增/更新的条数（值未变化的键不计数）。
        """
        changed = 0
        with self._meta_cache_lock:
            for k, v in mapping.items():
                if self._pe.get(k) != v:
                    self._pe[k] = v
                    changed += 1
        return changed

    def replace_names(self, all_names):
        """整体替换名称缓存（获取侧刷新完成时调用；
        同对象清空+灌入，保证所有共享别名同步可见）

        整段 RMW 持 _meta_cache_lock：clear()+update() 期间与快照读者
        及其它写者互斥，杜绝「遍历中途换表」（审计 P1-2）。
        """
        with self._meta_cache_lock:
            self._names.clear()
            self._names.update(all_names)
            self._names_loaded = True

    # ════════════════════════════════════════════════════════════════
    # 指数归属缓存（本地 stock_index_belong.json）
    # ════════════════════════════════════════════════════════════════
    # 本段原为「PE-TTM / 指数归属」双字段缓存（同文件 stock_pettm_index.json）。
    # PE-TTM 每日随行情变动、指数归属季度调仓才变，两者时间维度不匹配，且
    # 实际使用中不会每天点刷新 → 页面读到的是陈旧 PE。现 PE-TTM 改为
    # 「打开 K 线页面时实时取数」（见本类 PE-TTM 实时层），不再落盘；
    # 本文件只保存指数归属，故连文件名一并改掉。
    def load_index_belong_cache(self):
        """从本地 JSON 加载**指数归属**缓存到内存。文件不存在则返回空表。

        一次性迁移：新文件 stock_index_belong.json 不存在、而旧文件
        stock_pettm_index.json 存在时，读旧文件取其 index 字段、写成新文件；
        旧文件**保留不删**（可回退）。

        向后兼容旧格式（其中的 pe_ttm 字段一律忽略，不再消费）：
          {"sh600519": {"pe_ttm": 25.3, "index": "沪深300"}}   ← 旧合并格式
          {"sh600519": {"index": "沪深300"}}                   ← 现格式

        审计 P1-3，两处修复（沿用原实现）：
        ① 原实现「先置 loaded 旗，再逐条填充」——并发读者见 loaded 为真
           直接返回**半成品**，且 flag 已置真后这一轮再也不会重试，指数
           归属列静默显空。现改为先填本地字典、整体提交后才置位：读者
           要么看到「未加载」走完整路径，要么看到完整数据。
        ② 双检锁：并发首次调用只解析一次文件。
        """
        if self._belong_loaded:
            return self._belong
        belong_local = {}
        path = self.stock_index_belong_file
        migrated = False
        try:
            if not os.path.exists(path):
                legacy = self.legacy_stock_pe_ttm_file
                if os.path.exists(legacy):
                    path, migrated = legacy, True
                else:
                    path = None
            if path:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        idx_val = v.get("index") if isinstance(v, dict) else None
                        if isinstance(idx_val, str) and idx_val:
                            belong_local[k] = idx_val
                log.info(f"[信息] 从缓存文件加载指数归属：{len(belong_local)}只")
        except Exception as e:
            log.info(f"[指数归属] 加载缓存失败: {e}")
            belong_local = {}
        with self._meta_cache_lock:
            if not self._belong_loaded:      # 双检锁：并发只提交一次
                self._belong.update(belong_local)
                # ★ 填充完成后才置位
                self._belong_loaded = True
        if migrated:
            # 旧文件 → 新文件的落盘迁移（失败不影响已提交的内存态：
            # 下次启动会再走一遍本迁移路径）
            self.save_index_belong_cache()
        return self._belong

    def save_index_belong_cache(self):
        """把内存中的指数归属表落盘到 stock_index_belong.json（原子写）。返回是否成功。

        写入格式（**只含 index，不再写 pe_ttm**）：
            {"sh600519": {"index": "沪深300"}, ...}
        旧格式的纯数字 key（6 位）在此过滤，与改造前的落盘口径一致。
        """
        try:
            snap = self.belong_snapshot()
            combined = {}
            for k, v in snap.items():
                if k.isdigit() and len(k) == 6:
                    continue                     # 过滤旧格式纯数字 key
                if v:
                    combined[k] = {"index": v}
            os.makedirs(os.path.dirname(self.stock_index_belong_file), exist_ok=True)
            safe_write_json_file(self.stock_index_belong_file, combined,
                                 ensure_ascii=False)
            log.info(f"[指数归属] 已保存 {len(combined)} 条到 "
                     f"{self.stock_index_belong_file}")
            return True
        except Exception as e:
            log.info(f"[指数归属] 保存失败: {e}")
            return False

    def get_index_belong(self, market, code):
        """获取单只股票的指数归属（沪深300/中证500/中证1000），未缓存则返回 None。"""
        self.load_index_belong_cache()
        return self._belong.get(market + code)

    # ════════════════════════════════════════════════════════════════
    # PE-TTM 实时层（K 线页面「打开这个标的就取一次」）
    # ════════════════════════════════════════════════════════════════
    def set_pe_ttm_live_fetcher(self, fetcher):
        """注入 PE-TTM 取数实现（依赖倒置）。

        由 App/AppRefresh.py 在模块导入时注入，实现为 AppRefresh 的
        `_fetch_pe_ttm_live(market, code)`（A 股个股走 eltdx 全量进程缓存、
        指数 / 港股走腾讯；分流的单一实现在该函数内）。
        本类**不得**直接 import DataAPI（phase5 守卫 ④b：防影子双源），
        故取数实现只能由上层注入；未注入时实时层降级为空表。

        注入的实现**自带进程级全量缓存**：FastAPI 进程内首次取数时才联网拉
        一次全 A 股，之后命中内存（约 0 网络开销），因此这里可以每次调用都
        走一遍而不用担心打开每只股票都打一次网络。
        """
        self._pe_live_fetcher = fetcher

    def get_pe_ttm(self, market, code):
        """获取单只股票的 PE-TTM 值，无值返回 None。key 为 market+code 避免沪深同号冲突。

        两级来源：
          ① `_pe_loaded` 为真（PE 表已由外部提供——落盘缓存或快照用例注入）
             → 纯点查，不取数；
          ② 常态 → 每次调用经注入的取数实现取一次（该实现内部按进程缓存，
             非每次联网），取到即写入 _pe 供同批并发读者复用；**取数失败与
             「该票确实无 PE」必须可区分**，故失败以 log.error 显著上报并
             返回 None（页面不显示 PE），**不向上抛异常**（K 线接口不应因
             一个元数据取数失败而整体 500）。
        """
        key = market + code
        if self._pe_loaded:
            return self._pe.get(key)
        self._ensure_pe_ttm_live(market, code)
        return self._pe.get(key)

    def _ensure_pe_ttm_live(self, market, code):
        """取一只 PE-TTM 并写入 _pe（single-flight 门）。

        为什么每次调用都过一遍取数实现：PE-TTM 随行情每日变动，而落盘缓存
        只在点刷新时更新（实际使用中不会每天点）→ 页面读到的是陈旧值。注入的
        取数实现内部持有**进程级全量缓存**，故这里的"每次"不是"每次联网"：
        进程内首次取数触发一次 eltdx 全 A 股拉取（约 1.3s），之后纯内存命中。

        失败语义（与「该票无 PE」必须可区分）：
          · 取数实现抛异常 = **数据源不可用**（eltdx 连不上、且落盘镜像也没有）
            → log.error 显著上报（原为 log.info，与"正常无 PE"混在一起排障
            时看不出问题），保留旧值、不抛出。
          · 返回空 dict = 该票确实无 PE（次新股等），属正常，不记 error。

        并发语义：多标签页 / 多请求并发打开时，只有抢到 _pe_live_lock 的线程
        真去取数，其余线程**立即返回当前表**（可能暂无该票 → 页面不显示 PE），
        不排队等待——避免一个卡住的网络请求把整批请求一起拖住。
        """
        fetcher = self._pe_live_fetcher
        if fetcher is None:
            return          # 未注入取数实现（未走 AppRefresh 导入链）：降级为空
        if not self._pe_live_lock.acquire(blocking=False):
            return          # 已有线程在拉取，本次直接用当前表
        try:
            try:
                result = fetcher(market, code)
            except Exception as e:              # noqa: BLE001
                # 数据源不可用：显著上报。不抛出——K 线接口不应因一个元数据
                # 取数失败而整体 500，但必须留下 error 级痕迹。
                log.error(f"[PE-TTM] 取数失败({market}{code})，沿用旧值: "
                          f"{type(e).__name__}: {e}")
                return
            if result:
                self.update_pe_ttm(result)
        finally:
            self._pe_live_lock.release()

    def replace_index_belong(self, result):
        """整体替换指数归属缓存（获取侧 AKShare 刷新完成时调用）

        整段 RMW 持 _meta_cache_lock（审计 P1-2，与 replace_names 同形）：
        clear()+update() 期间与快照读者互斥。
        """
        with self._meta_cache_lock:
            self._belong.clear()
            self._belong.update(result)
            self._belong_loaded = True

    # ════════════════════════════════════════════════════════════════
    # 流通市值缓存（腾讯接口成功时的内存态 + 本地 JSON 兜底）
    # ════════════════════════════════════════════════════════════════
    # 流通市值本质上随价格每日变动；本地缓存作为腾讯接口失败时的兜底，
    # 但若缓存太久未刷新且当天接口又失败，继续用旧数据会误导判断。该阈值
    # 用于"接口失败且缓存过期"时的告警判定（秒）。
    _FLOAT_MC_STALE_SECONDS = 2 * 24 * 3600   # 2 天

    def load_float_mc_cache(self):
        """从本地JSON加载流通市值缓存（无日期限制，作为腾讯接口失败时的兜底）。"""
        if self._float_mc_loaded:
            return
        if not os.path.exists(self.float_mc_cache_file):
            return
        try:
            with open(self.float_mc_cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "data" in data:
                # 审计 P1-2/P1-3 同形修复：clear()+update() 的整段 RMW 持
                # _user_store_lock，与扫描预过滤的读、update_float_mc_cache
                # 的写互斥；并加双检锁避免并发重复解析。
                with self._user_store_lock:
                    if self._float_mc_loaded:
                        return
                    self._float_mc.clear()
                    self._float_mc.update(data["data"])
                    self._float_mc_loaded = True
                    # 旧版缓存无 saved_at 字段时保持 None（视为"未知"，不过期、不误报）
                    self._float_mc_saved_at = data.get("saved_at")
                log.info(f"[流通市值] 从本地缓存加载 {len(self._float_mc)} 只股票")
        except Exception as e:
            log.info(f"[流通市值] 读取缓存失败: {e}")

    def float_mc_cache_stale(self, max_age_seconds=_FLOAT_MC_STALE_SECONDS):
        """本地流通市值缓存是否已过期（用于接口失败回退时决定是否告警）。

        max_age_seconds：过期阈值（秒）。缓存无时间戳（旧文件）时返回 False，
        避免旧格式文件误报告警。"""
        if self._float_mc_saved_at is None:
            return False
        return (time.time() - self._float_mc_saved_at) > max_age_seconds

    def update_float_mc_cache(self, mv_dict):
        """将外部获取的流通市值字典合并到全局缓存，并保存到本地JSON。
        调用方应确保 load_float_mc_cache() 已先执行。

        持 _user_store_lock + 原子写：写线程是刷新线程，读线程是扫描
        预过滤，非原子写会让并发读者读到半截文件。
        """
        with self._user_store_lock:
            self._float_mc.update(mv_dict)
            self._float_mc_loaded = True
            self._float_mc_saved_at = time.time()
            # 保存到本地JSON（无日期限制，作为下次腾讯接口失败时的兜底；
            # 记录写入时刻供过期判定）
            try:
                safe_write_json_file(
                    self.float_mc_cache_file,
                    {"data": self._float_mc, "saved_at": self._float_mc_saved_at})
            except Exception as e:
                log.info(f"[流通市值] 保存缓存文件失败: {e}")

    def get_float_mc_from_cache(self, code):
        """从缓存获取流通市值（亿元），未命中返回None。"""
        return self._float_mc.get(code)

    def float_mc_count(self):
        """缓存条数（持 _user_store_lock 读取）

        给调用方一个**不需要碰共享容器本体**的取数口。原写法是
        `len(app_data.float_mc_cache)`——直接对共享 dict 取长度。虽然
        CPython 下 `len(dict)` 本身是原子的、不会抛异常，但它是「裸读共享
        容器」的口子：一旦有人照着改成 `for k in app_data.float_mc_cache`
        就会踩到真正的竞态。宁可多一个方法，也别留这个样板。
        """
        with self._user_store_lock:
            return len(self._float_mc)

    # ════════════════════════════════════════════════════════════════
    # 手选进入段选点持久化（CSV）
    # ════════════════════════════════════════════════════════════════
    def load_saved_point_times(self):
        """从CSV文件加载所有选点记录，返回 {code: {col: value}} 字典"""
        points = {}
        if not os.path.exists(self.saved_point_file):
            return points
        try:
            import csv
            with open(self.saved_point_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    code = row.get("code", "").strip()
                    if code:
                        points[code] = row
        except Exception as e:
            log.warning(f"[警告] 读取选点文件失败: {e}")
        return points

    def get_saved_point_time(self, code, col):
        """加锁读取某代码某周期的选点时间（无则返回 ""）。

        审计 P2：调用方原先是 `if code in _saved_point_times:` 再下标取值
        的 **check-then-act**，两步之间写者可能删掉该键 → KeyError。写者
        clear_saved_points_by_prefix 只删 `KQ.` 前缀，与股票代码不重叠，
        所以目前**撞不上是靠数据巧合，不是靠设计**。收敛到本方法后即与
        写者（save_point_time / clear_saved_point_time / clear_saved_
        points_by_prefix，均持 _user_store_lock）互斥。
        """
        with self._user_store_lock:
            entry = self._saved_point_times.get(code)
            if not entry:
                return ""
            return (entry.get(col) or "").strip()

    def save_point_time(self, code, name, freq, sdt):
        """保存或更新某只股票某个周期的选点（CSV 落盘 + 内存态，锁内原子）"""
        with self._user_store_lock:
            import csv
            col = FREQ_TO_COL.get(freq)
            if not col:
                return
            # 内存态同步更新（与落盘同锁，杜绝 CSV 与内存态不一致——
            # 修复前内存态由调用方在锁外直写，reader 可读到中间态）
            if code not in self._saved_point_times:
                self._saved_point_times[code] = {}
            self._saved_point_times[code]["name"] = name
            self._saved_point_times[code][col] = sdt
            # 读取现有数据
            rows = []
            if os.path.exists(self.saved_point_file):
                try:
                    with open(self.saved_point_file, "r", encoding="utf-8-sig") as f:
                        reader = csv.DictReader(f)
                        fieldnames = reader.fieldnames
                        for row in reader:
                            rows.append(row)
                except Exception:
                    fieldnames = SAVED_POINT_COLUMNS
            else:
                fieldnames = SAVED_POINT_COLUMNS

            # 查找是否已有该代码的记录
            found = False
            for row in rows:
                if row.get("code", "").strip() == code:
                    row["name"] = name
                    row[col] = sdt
                    found = True
                    break
            if not found:
                new_row = {"code": code, "name": name}
                for c in SAVED_POINT_COLUMNS[2:]:
                    new_row[c] = ""
                new_row[col] = sdt
                rows.append(new_row)

            # 写回文件（内存态已在锁内置位，与落盘一致；原子落盘防半截 CSV）
            try:
                _buf = io.StringIO()
                writer = csv.DictWriter(_buf, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
                _atomic_write_text(self.saved_point_file, _buf.getvalue(),
                                   encoding="utf-8-sig", newline="")
                log.info(f"[信息] 保存选点成功: {code} {freq} {col}={sdt}")
            except Exception as e:
                log.warning(f"[警告] 保存选点文件失败: {e}")

    def clear_saved_point_time(self, code, freq):
        """清除某只股票某个周期在CSV中的选点，同时更新内存缓存"""
        with self._user_store_lock:
            import csv
            col = FREQ_TO_COL.get(freq)
            if not col:
                return
            # 先清除内存缓存（无论CSV是否存在都要执行）
            if code in self._saved_point_times:
                if col in self._saved_point_times[code]:
                    self._saved_point_times[code][col] = ""
            if not os.path.exists(self.saved_point_file):
                return
            rows = []
            try:
                with open(self.saved_point_file, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames
                    for row in reader:
                        rows.append(row)
            except Exception:
                return
            # 清除该代码对应周期的选点
            for row in rows:
                if row.get("code", "").strip() == code:
                    row[col] = ""
                    break
            try:
                _buf = io.StringIO()
                writer = csv.DictWriter(_buf, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
                _atomic_write_text(self.saved_point_file, _buf.getvalue(),
                                   encoding="utf-8-sig", newline="")
                log.info(f"[信息] 清除选点成功: {code} {freq}")
            except Exception as e:
                log.warning(f"[警告] 清除选点失败: {e}")

    def clear_saved_points_by_prefix(self, prefix):
        """加锁批量删除内存态中指定前缀的选点（如 KQ. 期货条目）。

        修复：原 AppSSE 清理直接 del app_data.saved_point_times，绕开
        _saved_point_lock；改为加锁删内存态，与其它选点读-改-写串行。
        """
        removed = 0
        with self._user_store_lock:
            for k in [k for k in list(self._saved_point_times.keys()) if k.startswith(prefix)]:
                del self._saved_point_times[k]
                removed += 1
        return removed

    def clear_saved_point(self, code, freq="d"):
        """清除选点并同步清理分析缓存（对应 /api/clear_saved_point）"""
        from App import utils as _u
        market, normalized_code = _u._get_stock_market_code(code)
        if not market:
            # 统一解析拒掉旧写法/未知代码，不再在内部维护一套 SH/SZ 前后缀规则
            return {"ok": False, "error": f"无法识别代码: {code}"}
        # saved-point 内部标识与引擎一致：market(小写)+code 标准格式
        qualified_code = market + normalized_code
        self.clear_saved_point_time(qualified_code, freq)
        cache_key = make_live_key(market, normalized_code, freq)
        with self._stocks_cache_lock:
            if cache_key in self._stocks_analysis_cache:
                del self._stocks_analysis_cache[cache_key]
        gc.collect()
        return {"ok": True}

    # ════════════════════════════════════════════════════════════════
    # 上次查看代码/周期持久化
    # ════════════════════════════════════════════════════════════════
    def save_last_code_freq(self, code, freq="d"):
        """持久化上次查看的代码和周期到JSON文件（股票和期货通用）

        持 _user_store_lock + 原子写：/api/stocks/{code}/analyze 每次成功
        分析后都会落盘一次（FrontAPI 单独一次 run_in_threadpool，不在任何
        分析锁内），并发请求同时 open(w) 会互相截断成半截 JSON。
        """
        with self._user_store_lock:
            try:
                safe_write_json_file(self.last_code_freq_file,
                                     {"code": code, "freq": freq})
            except Exception:
                pass  # 静默失败，不影响主流程

    def load_last_code_freq(self):
        """从JSON文件加载上次查看的代码和周期，返回 (code, freq) 或 (None, None)"""
        try:
            if not os.path.exists(self.last_code_freq_file):
                return None, None
            with open(self.last_code_freq_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            code = data.get("code", "").strip()
            freq = data.get("freq", "d")
            if code:
                return code, freq
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
        return None, None

    # ════════════════════════════════════════════════════════════════
    # 文字标注持久化
    # ════════════════════════════════════════════════════════════════
    def load_annotations(self):
        """从 text_annotation.json 加载标注数据到内存

        自己持 _user_store_lock（RLock，调用方已持锁时可重入，无害）。
        原先依赖「所有调用方都已在外层持锁」——这个约定没有机器可验证的
        保障，任一个新调用方忘了持锁就会静默退化成无锁 clear+update
        （审计 P1-1 的根因之一）。收敛到方法内部后约定消失。
        """
        # 快速路径（无锁）：已加载直接返回，避免每次都进锁
        if self._annotations_loaded:
            return
        with self._user_store_lock:
            if self._annotations_loaded:      # 双检锁
                return
            if os.path.exists(self.annotations_file):
                try:
                    with open(self.annotations_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        self._annotations.clear()
                        self._annotations.update(data)
                except Exception as e:
                    log.warning(f"[警告] 加载标注数据失败: {e}")
                    self._annotations.clear()
            self._annotations_loaded = True

    def save_annotations(self):
        """保存标注数据到 text_annotation.json（原子落盘）

        调用方均已持 _user_store_lock；原子写额外防「写一半进程退出」留下
        截断 JSON —— 那会让下次 load_annotations 静默清空全部标注。
        """
        # 自己持锁（RLock 可重入）：原先注释写「调用方均已持锁」，但该约定
        # 无从校验；这里锁内取快照再落盘，保证写出的内容与某一时刻一致。
        with self._user_store_lock:
            _snapshot = dict(self._annotations)
        try:
            safe_write_json_file(self.annotations_file, _snapshot,
                                 ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"[警告] 保存标注数据失败: {e}")

    def get_annotations_for(self, code, freq):
        """获取某股票某周期的所有标注"""
        with self._user_store_lock:
            self.load_annotations()
            key = get_annotation_key(code, freq)
            return self._annotations.get(key, [])

    def add_annotation(self, code, freq, date_str, text, y_offset=0):
        """添加一条标注（自动去重：同日期同文字不重复添加）"""
        with self._user_store_lock:
            self.load_annotations()
            key = get_annotation_key(code, freq)
            if key not in self._annotations:
                self._annotations[key] = []
            # 去重：同日期同文字已存在则不添加
            for ann in self._annotations[key]:
                if ann.get("date") == date_str and ann.get("text") == text:
                    return False
            self._annotations[key].append({
                "date": date_str,
                "text": text,
                "y_offset": y_offset,
            })
            self.save_annotations()
            return True

    def delete_annotation(self, code, freq, date_str, text):
        """删除一条标注"""
        with self._user_store_lock:
            self.load_annotations()
            key = get_annotation_key(code, freq)
            if key not in self._annotations:
                return False
            before = len(self._annotations[key])
            self._annotations[key] = [
                ann for ann in self._annotations[key]
                if not (ann.get("date") == date_str and ann.get("text") == text)
            ]
            if len(self._annotations[key]) < before:
                if not self._annotations[key]:
                    del self._annotations[key]  # 清理空列表
                self.save_annotations()
                return True
            return False

    def delete_annotation_by_date(self, code, freq, date_str):
        """删除某日期下所有标注"""
        with self._user_store_lock:
            self.load_annotations()
            key = get_annotation_key(code, freq)
            if key not in self._annotations:
                return False
            before = len(self._annotations[key])
            self._annotations[key] = [
                ann for ann in self._annotations[key]
                if ann.get("date") != date_str
            ]
            if len(self._annotations[key]) < before:
                if not self._annotations[key]:
                    del self._annotations[key]
                self.save_annotations()
                return True
            return False

    def delete_all_annotations(self, code, freq):
        """删除某股票某周期下全部标注"""
        with self._user_store_lock:
            self.load_annotations()
            key = get_annotation_key(code, freq)
            if key not in self._annotations or not self._annotations[key]:
                return False
            del self._annotations[key]
            self.save_annotations()
            return True

    def get_annotated_codes(self, freq=""):
        """获取所有有标注的股票代码+周期列表，用于自选扫描
        返回 bare_code + market + name，方便前端与自选股列表交叉匹配。
        例如 key "sh000001_d" → {"code": "000001", "market": "sh", "name": "上证指数", "freq": "d", "count": N}
        期货 key "KQ.m@SHFE.rb_d" → {"code": "KQ.m@SHFE.rb", "market": "", "name": "", "freq": "d", "count": N}
        """
        # 审计 P1-1：原实现在**无锁**状态下直接遍历 self._annotations，
        # 而写者 add_annotation 持 _user_store_lock 修改同一张表 → 并发
        # 即抛「dictionary changed size during iteration」，路由 500。
        # 改为「锁内取快照、锁外遍历」，遍历的是锁内构造的副本：
        #   · dict 层：写者会 del / 新增键 → 须拷 dict；
        #   · list 层：add_annotation 是 `self._annotations[key].append(...)`
        #     的**就地追加** → 只拷 dict 不够，须逐键拷 list。
        # 删除类操作是整体替换列表（`_annotations[key] = [...]`），不会
        # 就地改写，故浅拷贝两层即等价于不可变快照。
        with self._user_store_lock:
            self.load_annotations()
            self.load_stock_names_from_cache_file()
            annotations_snapshot = {k: list(v) for k, v in self._annotations.items()}
        result = []
        for key, anns in annotations_snapshot.items():
            if not anns:
                continue
            parts = key.rsplit("_", 1)
            if len(parts) != 2:
                continue
            code_with_suffix, key_freq = parts
            if freq and key_freq != freq:
                continue

            # 解析代码：键内代码已是标准 market(小写)+code，或期货等非股票键原样
            from App import utils as _u
            mkt, bcode = _u._get_stock_market_code(code_with_suffix)
            if mkt:
                market = mkt
                bare_code = bcode
            else:
                market = ""
                bare_code = code_with_suffix  # 期货等非股票键原样保留（非点号旧写法）

            # 查询股票名称
            name = ""
            if market and bare_code:
                lookup_key = market.lower() + bare_code
                info = self._names.get(lookup_key, {})
                if isinstance(info, dict):
                    name = info.get("name", "")
                elif info:
                    name = str(info)

            result.append({
                "code": bare_code,
                "market": market,
                "name": name,
                "freq": key_freq,
                "count": len(anns),
                "annotations": [{"date": a.get("date", ""), "text": a.get("text", "")} for a in anns if a.get("text")]
            })
        return result

    # ════════════════════════════════════════════════════════════════
    # 自选股（zxg.blk；自含存储格式知识，与 DataAPI 互不依赖）
    # ════════════════════════════════════════════════════════════════
    @property
    def zxg_blk_path(self):
        """自选股文件路径：<tdx_install_dir>/T0002/blocknew/zxg.blk。

        与 DataAPI get_blk_path("zxg") 等价（dirname(vipdoc_dir) =
        tdx_install_dir），但路径推导自含于数据层，不产生 App → DataAPI 依赖边。"""
        root = app_config.tdx_install_dir
        if not root:
            return ""
        return os.path.join(root, "T0002", "blocknew", "zxg.blk")

    def read_zxg_stocks(self):
        """读取通达信自选股文件 zxg.blk，返回股票代码列表。"""
        path = self.zxg_blk_path
        if not os.path.exists(path):
            log.warning(f"[警告] 自选股文件不存在: {path}")
            return []
        return _read_zxg_blk_file(path)

    def save_to_zxg_blk(self, codes):
        """将股票代码列表追加到通达信自选股文件 zxg.blk。
        codes: list of str，内部标准格式 market(小写)+code：
        如 "sh600519"、"sz000001"、"hk00700"（外部点号写法 600519.SH 仅作读取容忍）
        自动去重，已存在的不会重复添加；无法识别的代码跳过。
        格式转换单一源：本方法内部统一走 _code_to_zxg_line。
        """
        # 审计 P1-4：原实现是 `open(path, "a")` 的**无锁非原子追加**——
        # ① 并发两次 POST 会让两批代码交错写入；② 写一半进程退出会留下
        # 截断行。现与 sync_zxg_blk 统一为「读-改-写 + 原子落盘」，并同时
        # 持进程内锁与**跨进程文件锁**（另一个写者跑在独立脚本进程里，
        # threading.Lock 对它无效）。
        path = self.zxg_blk_path
        if not path:
            return 0
        with self._user_store_lock:
            dir_name = os.path.dirname(path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with file_lock(path + ".lock"):
                existing = []
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="gbk") as f:
                            existing = [ln.strip() for ln in f if ln.strip()]
                    except Exception:
                        existing = []
                seen = set(existing)

                added = 0
                new_lines = []
                for code_str in codes:
                    line = _code_to_zxg_line(code_str)
                    if line is None:
                        log.info(f"[自选保存] 跳过无法识别的代码: {code_str.strip()}")
                        continue
                    if line not in seen:
                        new_lines.append(line)
                        seen.add(line)
                        added += 1
                        log.info(f"[自选保存] 已添加到自选股: {code_str.strip()} -> {line}")

                # 无新增则不落盘（避免把已存在的文件无谓重写一遍，
                # 也不在文件本不存在时创建空文件）
                if new_lines:
                    _atomic_write_text(path, "\n".join(existing + new_lines) + "\n",
                                       encoding="gbk")

        log.info(f"[自选保存] 共添加 {added} 只股票到自选股")
        return added

    def read_preserved_zxg_lines(self, path=None):
        """读取 zxg.blk 中应保留的代码（标准指数 + 通达信私有指数 88/188xxxx）。

        path 缺省取单一事实源 zxg_blk_path；'--tdx-path' 覆盖时传显式路径。
        返回保留行列表（模块级 _read_preserved_zxg_lines 的实例化入口）。
        """
        return _read_preserved_zxg_lines(path if path else self.zxg_blk_path)

    def sync_zxg_blk(self, codes, path=None, append=False):
        """全量同步自选股到 zxg.blk（同花顺→通达信 单一写入源）。

        replace（默认，append=False）：清空重写，仅保留标准指数与通达信私有指数
          （88/188xxxx），不在目标列表的旧代码被移除——由 `codes` 全量决定。
        append=True：在现有基础上追加（保留码依然保持开头，新增码去重）。

        :param codes: 内部标准格式代码列表 ["sh600519","sz000001","hk00700",...]
                      （点号写法 600519.SH / 00700.HK 仅作外部同花顺源读取容忍）
        :param path: zxg.blk 路径，缺省取 self.zxg_blk_path；--tdx-path 覆盖时传入
        :param append: True=追加模式，False=替换模式（默认）
        :return: {"written": 实际写入行数, "preserved": 保留的行数}
        """
        blk_path = path if path else self.zxg_blk_path
        if not blk_path:
            return {"written": 0, "preserved": 0}
        # 整段读-改-写持 _user_store_lock + 原子落盘：.blk 是自选股用户数据，
        # 并发 POST /api/stocks/scan/save/zxg 会互相截断。
        with self._user_store_lock:
            _dir = os.path.dirname(blk_path)
            if _dir:
                os.makedirs(_dir, exist_ok=True)
            # 跨进程文件锁（审计 P1-4）：本方法也被**独立脚本进程**调用，
            # 它与 API 进程各自的 _user_store_lock 互不可见，必须靠 OS 级
            # 文件锁才能真正串行化两进程的读-改-写。
            with file_lock(blk_path + ".lock"):
                preserved = _read_preserved_zxg_lines(blk_path)
                new_lines = []
                for c in codes:
                    line = _code_to_zxg_line(c)
                    if line is not None:
                        new_lines.append(line)

                if append:
                    existing = []
                    if os.path.exists(blk_path):
                        with open(blk_path, "r", encoding="gbk") as f:
                            existing = [ln.strip() for ln in f if ln.strip()]
                    final = list(dict.fromkeys(existing + preserved + new_lines))
                else:
                    final = list(dict.fromkeys(preserved + new_lines))

                _atomic_write_text(blk_path, "\n".join(final) + "\n", encoding="gbk")

        return {"written": len(new_lines), "preserved": len(preserved)}


# 全局单例（实例化即完成选点/标注启动加载）
app_data = AppData()

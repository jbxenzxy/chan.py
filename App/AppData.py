# -*- coding: utf-8 -*-
"""
App/AppData.py — 业务数据层（阶段 4：数据层收敛）
================================================
设计文档 6.1 / 8.7：缓存 / 持久化 / 标注 / 自选股全部收敛于此，
产出「数据层职责单一」。

本文件自阶段 4 起持有**真实实现与真实状态**（此前为薄委托壳）：
  · 分析结果 LRU 缓存（股票 + 期货）
  · 名称 / PE-TTM / 指数归属 / 流通市值 四类惰性缓存
  · 手动选点 CSV 持久化（double_click_dt.csv）
  · 上次查看代码/周期（last_code_freq.json）
  · 文字标注（text_annotation.json）
  · 自选股（zxg.blk 读写；存储格式知识自含，设计 4.4 与 DataAPI 互不依赖）

依赖方向（设计 6.2 / 4.4）：
  FrontAPI.py → App/AppOrch.py → App/AppData.py → App/AppConfig.py（单向）
  my_chan_main.py 中的同名函数/状态自阶段 4 起降级为兼容壳/别名，
  指向本模块单例 app_data，行为零漂移。

使用方式：
    from App.AppData import app_data
    app_data.cache_put("key", value)
    app_data.get_annotations_for("000001.SH", "d")
"""

import collections
import gc
import json
import os
import re
import threading

from App.AppConfig import app_config
from App.AppLog import get_logger
log = get_logger(__name__)



# ═══════════════════════════════════════════════════════════════════
# 选点表 schema（持久化格式定义，随数据层同迁；消费侧经别名共享）
# ═══════════════════════════════════════════════════════════════════
SAVED_POINT_COLUMNS = ["code", "name", "y", "q", "m", "w", "d",
                       "60m", "30m", "15m", "5m", "1m", "15s"]
FREQ_TO_COL = {"y": "y", "q": "q", "m": "m", "w": "w", "d": "d",
               "60m": "60m", "30m": "30m", "15m": "15m", "5m": "5m",
               "1m": "1m", "15s": "15s"}

# 自选股写盘用指数映射（阶段 4 自 DataAPI/TdxAPI.py 迁入；
# 通达信内部代码格式，与 Script/ths_sync_to_tdx.py 中的同名映射保持一致）
ZXG_HK_INDEX_MAP = {
    "HS2198": "HZ5489",  # 恒生港股通可投资指数（显示名 HSIDI）
    "HS2083": "HZ5017",  # 恒生科技指数（显示名 HSTECH）
}
ZXG_US_INDEX_MAP = {
    "NBI": "A_NBI",
}


def safe_write_json_file(path, data, *, ensure_ascii=False, indent=None):
    """先写临时文件并校验 JSON 可读，再用 os.replace 覆盖正式文件；失败时保留旧文件。
    （持久化底座：原子写，防断电/中断产生半截文件）"""
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
        with open(tmp_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, type(data)):
            raise ValueError("临时 JSON 文件类型校验失败")
        os.replace(tmp_path, path)
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
    属设计 4.4「两者互不依赖」的边界代价（AppData-vs-DataAPI-职责边界.html §6
    确认各自自含为正确边界，不引入顶层中立模块强并）。
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
        print(f"[错误] 读取板块文件失败 {blk_path}: {e}")
    return stocks


# ════════════════════════════════════════════════════════════════
# P1-5 缓存键规范化：结构化键（消除字符串拼接歧义与漂移）
# ════════════════════════════════════════════════════════════════
# 股票分析缓存（_stocks_analysis_cache）改用结构化元组键：
#   (kind, market, code, freq, date)
# 双窗口键（dual_main/dual_sub）追加第 6 维 impl（independent/legacy，
# 见 _dual_impl_tag），隔离 A/B 两种实现的缓存，防切换开关后串用。
# 天然 hashable、可比较、无分隔符歧义（代码/周期/日期含 "_" / ":" 也不会碰撞）。
# 期货子窗缓存（_futures_analysis_cache）保留字符串键，但统一经
# make_futures_sub_key 规范化（symbol 大写入键），消除 AppSSE/AppChart/
# 语义化接口三处拼接的大小写漂移；第三方引擎（BSPointList）按同一
# 格式拼接，兼容不变。单一事实源：所有调用方经本组工厂函数生成键，
# 不再手工拼接字符串。
def make_analysis_key(kind, market, code, freq, date):
    """构造结构化分析缓存键（kind: single/dual_main/dual_sub/live）"""
    return (kind, str(market), str(code), str(freq), str(date))


def _dual_impl_tag():
    """股票双窗 A/B 实现标签（dual 缓存 key 的第 6 维）。

    独立(independent)与联立(legacy)两种实现的红框边界语义不同
    （独立=数学换算，联立=KLU.sub_kl_list 真实边界），dual 缓存若
    不区分实现，切换 A/B 开关后会串用另一实现的缓存结果（快照全量
    连跑已复现：legacy 先写缓存，independent 复跑直接命中返回联立
    输出）。语义事实源为 AppEngine._stock_dual_impl（读
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


class AppData:
    """业务数据层：缓存 / 持久化 / 标注 / 自选股（真实实现 · 阶段 4）"""

    MAX_CACHE_SIZE = 50  # 最多缓存 50 个 (股票, 周期) 组合（共享 LRU 池总上限）
    # 双窗结构化缓存键（dual_main/dual_sub）在共享池内单独限额：
    # 双窗条目更重（两键各含 CChan + records）且不常用，超限优先淘汰
    # 最旧 dual 键，不挤占常用单窗口缓存（单一事实源 AppConfig）。
    MAX_DUAL_CACHE_KEYS = app_config.max_dual_cache_keys
    # 双窗运行时下窗 CChan 缓存上限（LRU）：历史上无上限，切换标的时
    # 旧 CChan 残留泄漏；现超限淘汰最旧（切回单窗不主动清，保留快速切回）。
    MAX_STOCKS_SUB_CHAN = app_config.max_stocks_sub_chan

    def __init__(self):
        # ── 分析结果缓存 ──
        self._cache_lock = threading.RLock()
        self._stocks_analysis_cache = collections.OrderedDict()
        self._futures_analysis_cache = {}
        # 股票双窗独立化：下窗 CChan 运行时缓存（P1；键见 stocks_sub_cache_key）
        self._stocks_sub_chan_cache = {}

        # ── 名称 / PE / 归属 / 流通市值（惰性加载标志 + 字典）──
        self._names = {}
        self._names_loaded = False
        self._pe = {}
        self._pe_loaded = False
        self._belong = {}
        self._belong_loaded = False
        self._float_mc = {}
        self._float_mc_loaded = False

        # ── 标注 ──
        self._annotations = {}   # { "code_freq": [ {date,text,y_offset}, ... ] }
        self._annotations_loaded = False

        # ── 启动时加载（与原 my_chan_main import 期行为一致）──
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
    def stock_pe_ttm_file(self):
        return app_config.stock_pe_ttm_file

    @property
    def float_mc_cache_file(self):
        return app_config.float_mc_cache_file

    # ════════════════════════════════════════════════════════════════
    # 状态只读出口（my_chan_main 兼容别名 / 获取侧刷新函数经此共享同一对象）
    # ════════════════════════════════════════════════════════════════
    @property
    def cache_lock(self):
        return self._cache_lock

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

    # ── 行业映射单一加载（阶段 5，设计 8.8/4.1）──────────────────
    #    tdxhy_mapping_data.py 已整体迁入 App/（独立数据文件，不并入本类）；
    #    原 DataAPI/TdxAPI.py 与 my_chan_main.py 两处硬编码寻址收敛为
    #    本方法一个加载点，DataAPI 侧经 set_tdx_hy_mapping 注入（互不依赖）。
    #    P2-2：加载方式由 exec(open(...).read()) 改为直接 import 数据模块
    #    （数据文件自带统一加载函数 load_tdxhy_mapping，消除硬编码路径）。

    def load_tdxhy_mapping(self):
        """加载通达信研究行业映射，返回 (x_to_881, to_x) 二元组

        单一加载点：直接 import 数据模块（P2-2 由 exec() 改 import），
        文件缺失/损坏时硬失败（ValueError），不再静默降级空表
        （设计 8.4 验收要求：迁移全过程中映射表不发生静默降级）。
        结果按 (x_to_881, to_x) 次序返回，可直接 set_tdx_hy_mapping(*result)。
        """
        try:
            from App.tdxhy_mapping_data import load_tdxhy_mapping as _load
            x_to_881, to_x = _load()
        except ImportError as e:
            raise ValueError(f"行业映射数据文件缺失: {e}") from e
        if not x_to_881 or not to_x:
            raise ValueError("行业映射数据为空（加载静默降级已禁止）")
        return x_to_881, to_x

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

    def _evict_dual_overflow_locked(self):
        """（内部，须持 _cache_lock）双窗键单独限额淘汰。

        写入新 dual 键前调用：池内现存 dual 键数已达 MAX_DUAL_CACHE_KEYS
        时，按插入序（最旧优先）淘汰，直到腾出空位。dict 保序 + cache_get
        命中移尾，序即 LRU 新旧序。单窗键不受影响。
        """
        dual_keys = [k for k in self._stocks_analysis_cache if self._is_dual_key(k)]
        while len(dual_keys) >= self.MAX_DUAL_CACHE_KEYS:
            oldest = dual_keys.pop(0)
            self._stocks_analysis_cache.pop(oldest, None)
            gc.collect()
            log.info(f"[内存] 双窗缓存达上限({self.MAX_DUAL_CACHE_KEYS})，淘汰最旧双窗条目: {oldest}")

    def cache_put(self, key, value):
        """写入缓存，超出上限时淘汰最旧的条目（LRU语义）。
        内存由 LRU 50 条上限 + 双窗键单独限额 + 扫描时逐只释放非买点缓存
        共同控制：dual_* 键超 MAX_DUAL_CACHE_KEYS 时优先淘汰最旧 dual 键
        （双窗不常用且条目重，不挤占常用单窗口缓存）。"""
        with self._cache_lock:
            if key in self._stocks_analysis_cache:
                del self._stocks_analysis_cache[key]  # 移到末尾
            else:
                # 新键入池前的容量控制：dual 键先过单独限额，再过池总量限
                if self._is_dual_key(key):
                    self._evict_dual_overflow_locked()
                if len(self._stocks_analysis_cache) >= self.MAX_CACHE_SIZE:
                    oldest_key = next(iter(self._stocks_analysis_cache))
                    self._stocks_analysis_cache.pop(oldest_key)
                    gc.collect()
                    log.info(f"[内存] 缓存已满({self.MAX_CACHE_SIZE})，淘汰: {oldest_key}")
            self._stocks_analysis_cache[key] = value

    def cache_get(self, key):
        """读取缓存，命中时移到末尾（LRU语义）"""
        with self._cache_lock:
            if key not in self._stocks_analysis_cache:
                return None
            value = self._stocks_analysis_cache.pop(key)
            self._stocks_analysis_cache[key] = value
        return value

    def cache_remove(self, key):
        """从缓存中删除指定条目（不触发 GC，由调用方在适当时机统一回收）"""
        with self._cache_lock:
            if key in self._stocks_analysis_cache:
                del self._stocks_analysis_cache[key]

    def futures_cache_get(self, key):
        """期货分析缓存读（独立于股票 LRU，键形如 "KQ.m@SHFE.rb:1m"）"""
        return self._futures_analysis_cache.get(key)

    def futures_cache_put(self, key, value):
        """期货分析缓存写（SSE 双窗口下窗 chan 供 /api/red_range_zs 访问）"""
        self._futures_analysis_cache[key] = value

    def futures_cache_pop(self, key, default=None):
        """期货分析缓存失效（子级别切换时释放旧中间状态）"""
        return self._futures_analysis_cache.pop(key, default)

    # ── 语义化子窗接口（吸收外部评审：key 规则内聚于数据层，   ──
    #    调用方不再手工拼接 "{SYMBOL}:{sub_freq}"，大小写规则单一事实源）
    def set_futures_sub_chan(self, symbol, sub_freq, chan):
        """写入期货子窗口 CChan（symbol 统一大写入键，供 /api/red_range_zs 访问）"""
        return self.futures_cache_put(make_futures_sub_key(symbol, sub_freq), chan)

    def get_futures_sub_chan(self, symbol, sub_freq):
        """读取期货子窗口 CChan（无则返回 None；symbol 大小写不敏感）"""
        return self.futures_cache_get(make_futures_sub_key(symbol, sub_freq))

    def pop_futures_sub_chan(self, symbol, sub_freq):
        """弹出并删除期货子窗口 CChan（SSE 连接关闭 / 子级别切换时释放）"""
        return self.futures_cache_pop(make_futures_sub_key(symbol, sub_freq), None)

    # ── 股票双窗独立化（P1，D1=A 改造）────────────────────────
    #    仿期货子窗缓存建「股票下窗 CChan 运行时缓存」：
    #      · 写入方：_analyze_stock_internal 独立双窗路径（先建下窗再建上窗，
    #        保证上窗 bsp 计算的区间套 check_nested_diver 能整读到完整下窗）；
    #      · 读取方：check_nested_diver（区间套）与 compute_red_range_zs
    #        （红框中枢，P3 起改读独立下窗；miss 抛错，D6=B 对齐期货语义）。
    #    键不带复盘日期后缀（运行时态，随每次双窗重建覆盖），
    #    与 dual_sub 结构化缓存（带 date_suffix，存 result/records）职责分离。
    def stocks_sub_cache_key(self, chan_code, sub_freq):
        """股票下窗运行时缓存键："{market}.{code}:{sub_freq}"（统一大写）"""
        return f"{str(chan_code).upper()}:{sub_freq}"

    def stocks_sub_cache_get(self, chan_code, sub_freq):
        """读取股票下窗 CChan（无则返回 None；命中移到末尾=LRU 语义）"""
        key = self.stocks_sub_cache_key(chan_code, sub_freq)
        chan = self._stocks_sub_chan_cache.get(key)
        if chan is not None:
            # LRU：命中移到末尾（dict 保序，插入序即新旧序）
            del self._stocks_sub_chan_cache[key]
            self._stocks_sub_chan_cache[key] = chan
        return chan

    def stocks_sub_cache_put(self, chan_code, sub_freq, chan):
        """写入股票下窗 CChan（同名键覆盖 = 双窗重建即刷新运行时态；
        超 MAX_STOCKS_SUB_CHAN 淘汰最旧——切换标的残留的旧 CChan 不再泄漏）"""
        key = self.stocks_sub_cache_key(chan_code, sub_freq)
        if key in self._stocks_sub_chan_cache:
            del self._stocks_sub_chan_cache[key]
        elif len(self._stocks_sub_chan_cache) >= self.MAX_STOCKS_SUB_CHAN:
            oldest = next(iter(self._stocks_sub_chan_cache))
            self._stocks_sub_chan_cache.pop(oldest)
            gc.collect()
            log.info(f"[内存] 运行时下窗缓存达上限({self.MAX_STOCKS_SUB_CHAN})，淘汰: {oldest}")
        self._stocks_sub_chan_cache[key] = chan

    def stocks_sub_cache_pop(self, chan_code, sub_freq):
        """弹出并删除股票下窗 CChan（切换标的/下窗周期时释放旧中间状态）"""
        return self._stocks_sub_chan_cache.pop(self.stocks_sub_cache_key(chan_code, sub_freq), None)


    # ════════════════════════════════════════════════════════════════
    # 股票名称缓存
    # ════════════════════════════════════════════════════════════════
    def get_stock_name(self, market, code):
        """获取股票名称。从本地缓存文件读取，缓存不存在则返回None。
        港股5位代码（如00700）和A股6位代码（如000700）是不同证券，绝不互相回退。"""
        if market == "ds" and code == "932000":
            return "中证2000"
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
        版本迁移：自动将旧版纯数字键转换为 market+code 复合键。"""
        if self._names_loaded:
            return len(self._names)
        if not os.path.exists(self.stock_names_cache_file):
            return 0
        try:
            with open(self.stock_names_cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 迁移：将旧版纯数字键（如 "000001"）转换为复合键（如 "sh000001"）
                migrated = {}
                for key, info in data.items():
                    if isinstance(info, dict) and "market" in info and info["market"]:
                        mkt = info["market"]
                        # 纯数字键（旧格式）→ 复合键
                        if key.isdigit():
                            new_key = mkt + key
                        else:
                            new_key = key
                        migrated[new_key] = info
                    else:
                        migrated[key] = info
                self._names.update(migrated)
                self._names_loaded = True
                log.info(f"[信息] 从缓存文件加载股票名称: {len(self._names)}只")
                return len(self._names)
        except Exception as e:
            log.warning(f"[警告] 读取股票名称缓存失败: {e}")
        return 0

    def replace_names(self, all_names):
        """整体替换名称缓存（获取侧刷新完成时调用；原实现为模块级重绑定，
        此处改为同对象清空+灌入，保证所有共享别名同步可见）"""
        self._names.clear()
        self._names.update(all_names)
        self._names_loaded = True

    # ════════════════════════════════════════════════════════════════
    # PE-TTM / 指数归属缓存（同文件 stock_pettm_index.json）
    # ════════════════════════════════════════════════════════════════
    def load_pe_ttm_cache(self):
        """从 stock_pettm_index.json 加载 PE-TTM 和指数归属缓存到内存。文件不存在则返回空。
        向后兼容旧格式 {"sh600519": 25.3}，新格式为 {"sh600519": {"pe_ttm": 25.3, "index": "沪深300"}}"""
        if self._pe_loaded:
            return self._pe
        self._pe_loaded = True
        self._belong_loaded = True
        if not os.path.exists(self.stock_pe_ttm_file):
            return self._pe
        try:
            with open(self.stock_pe_ttm_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            pe_count = 0
            idx_count = 0
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, dict):
                        # 新格式：{"pe_ttm": float, "index": str}
                        pe_val = v.get("pe_ttm")
                        idx_val = v.get("index")
                        if isinstance(pe_val, (int, float)) and pe_val != 0:
                            self._pe[k] = pe_val
                            pe_count += 1
                        if isinstance(idx_val, str) and idx_val:
                            self._belong[k] = idx_val
                            idx_count += 1
                    elif isinstance(v, (int, float)) and v != 0:
                        # 旧格式：直接是数字
                        self._pe[k] = v
                        pe_count += 1
            log.info(f"[信息] 从缓存文件加载PE-TTM：{pe_count}只；加载指数归属：{idx_count}只")
        except Exception as e:
            log.info(f"[PE-TTM] 加载缓存失败: {e}")
        return self._pe

    def get_pe_ttm(self, market, code):
        """获取单只股票的 PE-TTM 值，未缓存则返回 None。key 为 market+code 避免沪市深市同号冲突。"""
        self.load_pe_ttm_cache()
        return self._pe.get(market + code)

    def get_index_belong(self, market, code):
        """获取单只股票的指数归属（沪深300/中证500/中证1000），未缓存则返回 None。"""
        self.load_pe_ttm_cache()
        return self._belong.get(market + code)

    def replace_index_belong(self, result):
        """整体替换指数归属缓存（获取侧 AKShare 刷新完成时调用）"""
        self._belong.clear()
        self._belong.update(result)
        self._belong_loaded = True

    # ════════════════════════════════════════════════════════════════
    # 流通市值缓存（腾讯接口成功时的内存态 + 本地 JSON 兜底）
    # ════════════════════════════════════════════════════════════════
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
                self._float_mc.clear()
                self._float_mc.update(data["data"])
                self._float_mc_loaded = True
                log.info(f"[流通市值] 从本地缓存加载 {len(self._float_mc)} 只股票")
        except Exception as e:
            log.info(f"[流通市值] 读取缓存失败: {e}")

    def update_float_mc_cache(self, mv_dict):
        """将外部获取的流通市值字典合并到全局缓存，并保存到本地JSON。
        调用方应确保 load_float_mc_cache() 已先执行。"""
        self._float_mc.update(mv_dict)
        self._float_mc_loaded = True
        # 保存到本地JSON（无日期限制，作为下次腾讯接口失败时的兜底）
        try:
            with open(self.float_mc_cache_file, "w", encoding="utf-8") as f:
                json.dump({"data": self._float_mc}, f, ensure_ascii=False)
        except Exception as e:
            log.info(f"[流通市值] 保存缓存文件失败: {e}")

    def get_float_mc_from_cache(self, code):
        """从缓存获取流通市值（亿元），未命中返回None。"""
        return self._float_mc.get(code)

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

    def save_point_time(self, code, name, freq, sdt):
        """保存或更新某只股票某个周期的选点"""
        import csv
        col = FREQ_TO_COL.get(freq)
        if not col:
            return
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

        # 写回文件（内存态由调用方维护——分析路径保存后自行更新 _saved_point_times）
        try:
            with open(self.saved_point_file, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            log.info(f"[信息] 保存选点成功: {code} {freq} {col}={sdt}")
        except Exception as e:
            log.warning(f"[警告] 保存选点文件失败: {e}")

    def clear_saved_point_time(self, code, freq):
        """清除某只股票某个周期在CSV中的选点，同时更新内存缓存"""
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
            with open(self.saved_point_file, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            log.info(f"[信息] 清除选点成功: {code} {freq}")
        except Exception as e:
            log.warning(f"[警告] 清除选点失败: {e}")

    def clear_saved_point(self, code, freq="d"):
        """清除选点并同步清理分析缓存（对应 /api/clear_saved_point）"""
        normalized_code = code.strip().upper()
        market = None
        prefix_match = re.match(r'^(SH|SZ|HK|DS)(\d+)$', normalized_code)
        suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK|DS)$', normalized_code)
        if prefix_match:
            market = prefix_match.group(1).lower()
            normalized_code = prefix_match.group(2)
        elif suffix_match:
            normalized_code = suffix_match.group(1)
            market = suffix_match.group(2).lower()

        qualified_code = f"{normalized_code}.{market.upper()}" if market else normalized_code
        self.clear_saved_point_time(qualified_code, freq)
        cache_key = make_live_key(market, normalized_code, freq)
        with self._cache_lock:
            if cache_key in self._stocks_analysis_cache:
                del self._stocks_analysis_cache[cache_key]
        gc.collect()
        return {"ok": True}

    # ════════════════════════════════════════════════════════════════
    # 上次查看代码/周期持久化
    # ════════════════════════════════════════════════════════════════
    def save_last_code_freq(self, code, freq="d"):
        """持久化上次查看的代码和周期到JSON文件（股票和期货通用）"""
        try:
            data = {"code": code, "freq": freq}
            with open(self.last_code_freq_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
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
        """从 text_annotation.json 加载标注数据到内存"""
        if self._annotations_loaded:
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
        """保存标注数据到 text_annotation.json"""
        try:
            with open(self.annotations_file, "w", encoding="utf-8") as f:
                json.dump(self._annotations, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"[警告] 保存标注数据失败: {e}")

    def get_annotations_for(self, code, freq):
        """获取某股票某周期的所有标注"""
        self.load_annotations()
        key = get_annotation_key(code, freq)
        return self._annotations.get(key, [])

    def add_annotation(self, code, freq, date_str, text, y_offset=0):
        """添加一条标注（自动去重：同日期同文字不重复添加）"""
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
        例如 key "000001.SH_d" → {"code": "000001", "market": "SH", "name": "上证指数", "freq": "d", "count": N}
        期货 key "KQ.m@SHFE.rb_d" → {"code": "KQ.m@SHFE.rb", "market": "", "name": "", "freq": "d", "count": N}
        """
        self.load_annotations()
        self.load_stock_names_from_cache_file()
        result = []
        for key, anns in self._annotations.items():
            if not anns:
                continue
            parts = key.rsplit("_", 1)
            if len(parts) != 2:
                continue
            code_with_suffix, key_freq = parts
            if freq and key_freq != freq:
                continue

            # 解析市场后缀: 000001.SH → bare_code=000001, market=SH
            # 期货代码（如 KQ.m@SHFE.rb）没有市场后缀，保持不变
            market = ""
            bare_code = code_with_suffix
            for suffix in [".SH", ".SZ", ".HK", ".BJ", ".DS"]:
                if code_with_suffix.upper().endswith(suffix):
                    market = suffix[1:]  # 去掉点号
                    bare_code = code_with_suffix[:-len(suffix)]
                    break

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
    # 自选股（zxg.blk；阶段 4 自含存储格式知识，设计 4.4 与 DataAPI 互不依赖）
    # ════════════════════════════════════════════════════════════════
    @property
    def zxg_blk_path(self):
        """自选股文件路径：<tdx_install_dir>/T0002/blocknew/zxg.blk。

        与 DataAPI get_blk_path("zxg") 等价（其 vipdoc_dir 由
        my_chan_main 从 app_config 注入，dirname(vipdoc) = tdx_install_dir），
        但路径推导自含于数据层，不产生 App → DataAPI 依赖边。"""
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
        codes: list of str，格式如 "000852.SH"、"600519.SH"、"00700.HK"、"NBI.US"
        自动去重，已存在的不会重复添加。

        各市场输出格式：
          A 股：     {前缀}{6位代码}    如 1600519
          港股个股：  31#{5位代码}       如 31#00700
          港股指数：  27#{HZ代码}       如 27#HZ5489
          美股个股：  74#{代码}         如 74#XBI
          美股指数：  12#A_{代码}       如 12#A_NBI
        """
        path = self.zxg_blk_path
        if not path:
            return 0
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            existing = set()
        else:
            existing = set()
            try:
                with open(path, "r", encoding="gbk") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            existing.add(line)
            except Exception:
                pass

        added = 0
        with open(path, "a", encoding="gbk") as f:
            for code_str in codes:
                code_str = code_str.strip().upper()
                # A 股：纯数字 + SH/SZ/BJ 后缀
                m = re.match(r'^(\d+)\.(SH|SZ|BJ)$', code_str)
                if m:
                    code = m.group(1)
                    market = m.group(2)
                    if market == "SH":
                        line = "1" + code
                    elif market == "SZ":
                        line = "0" + code
                    else:  # BJ
                        line = "2" + code
                else:
                    # 港股 / 美股：代码可能含字母（如 NBI.US、HSIDI.HK）
                    m2 = re.match(r'^(\w+)\.(HK|US)$', code_str)
                    if m2:
                        code = m2.group(1)
                        market = m2.group(2)
                        if market == "HK":
                            if code in ZXG_HK_INDEX_MAP:
                                # 港股指数：27# + 通达信内部代码
                                line = "27#" + ZXG_HK_INDEX_MAP[code]
                            else:
                                # 港股个股：31# + 5位代码
                                line = "31#" + code.zfill(5)
                        else:  # US
                            if code in ZXG_US_INDEX_MAP:
                                # 美股指数：12# + 通达信内部代码
                                line = "12#" + ZXG_US_INDEX_MAP[code]
                            else:
                                # 美股个股：74# + 原始代码
                                line = "74#" + code
                    else:
                        # 无法识别的格式，跳过
                        log.info(f"[自选保存] 跳过无法识别的代码: {code_str}")
                        continue
                if line not in existing:
                    f.write(line + "\n")
                    existing.add(line)
                    added += 1
                    log.info(f"[自选保存] 已添加到自选股: {code_str} -> {line}")

        log.info(f"[自选保存] 共添加 {added} 只股票到自选股")
        return added


# 全局单例（实例化即完成选点/标注启动加载，与原 import 期行为一致）
app_data = AppData()

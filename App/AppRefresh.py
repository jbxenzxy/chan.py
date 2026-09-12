# -*- coding: utf-8 -*-
"""
App/AppRefresh.py —— 刷新功能域
=========================================================================
点击页面右上角「刷新」按钮后的操作，刷新股票名、指数归属、板块文件等。
（PE-TTM **不在此列**：它每日随行情变动，落盘缓存在用户不点刷新时必然陈旧，
已在 2026-09 改为「打开 K 线页面实时取数」——见 AppData 的 PE-TTM 实时层，
本模块只负责把取数实现注入进去。）

本模块收纳：
  - 股票名称刷新（refresh_stock_names / refresh_stock_names_async / refresh_status）
  - 名称 / 指数归属 缓存读写（AppData 直连）
  - PE-TTM 实时取数实现的注入点（依赖倒置，见下方 app_data.set_pe_ttm_live_fetcher）
  - 刷新实现（_refresh_stock_names / _refresh_index_belong /
      _fetch_index_belong_from_akshare / _collect_codes_from_vipdoc /
      _fetch_names_from_sina_once 等）

依赖方向：AppRefresh.py → AppConfig / AppData / DataAPI（单向）
"""
import json
import os
import threading
import time
import traceback

from App.AppConfig import app_config
from App.AppData import app_data
from App.AppLog import get_logger
from App.utils import is_index
from DataAPI.TdxAPI import collect_codes_from_vipdoc, refresh_block_files
from DataAPI.AkshareAPI import AKSHARE_EXCHANGE_MAP, AKSHARE_INDEX_MAP, fetch_index_cons
from DataAPI.ElTdxAPI import fetch_pe_ttm_all as _eltdx_fetch_pe_ttm_all
from DataAPI.TxAPI import fetch_pe_ttm as _tx_fetch_pe_ttm, fetch_hk_names
from DataAPI.SinaAPI import fetch_a_names

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# PE-TTM 取数（A 股个股 → eltdx 全量进程缓存；指数 / 港股 → 腾讯）
# ═══════════════════════════════════════════════════════════════════════
# 「按市场 / 按标的类型选数据源」是**业务编排规则**，不是某个数据源的能力，
# 故收口在本模块（App 层），不另立 DataAPI 门面模块 —— 与本模块既有写法一致
# （_fetch_names_from_sina_once 里 A 股走新浪、港股走腾讯，同样是 App 层选源）。
#
# 为什么指数不能走 eltdx：eltdx 的 0x06B9 统计文件 tdxstat.cfg 只覆盖 A 股
# 个股，指数不在其中，取回来必然是空表（表现为「输入指数不显示 PE-TTM」）。
# 指数与港股沿用腾讯 qt.gtimg.cn 字段 [39]（改造前就是这个方案，实测可用）。
#
# A 股个股：eltdx 的 read_stats() 是**单次请求覆盖全市场**，取 1 只与取 5000
# 只耗时相同（约 1.3s）。故做成「进程级全量缓存」：FastAPI 进程内首次取数时
# 拉全 A 股 → 内存 + 落盘 stock_pettm.json；只要进程不重启，后续任何股票都
# 直接读内存，不再调 eltdx（本软件无实时行情，无需盘中获取最新 PE）。
# eltdx 失败 → 退回落盘的 stock_pettm.json；落盘也没有 → 报错（抛异常，由
# AppData 以 log.error 显著上报），不静默返回空表。
_PE_TTM_ALL = {}                 # {mkt+code: float} 进程级全量 PE-TTM
_PE_TTM_PRIMED = False           # 全量缓存是否已就绪（成功=True；彻底失败=False 并允许重试）
_PE_TTM_PRIMED_AT = 0.0          # 就绪时刻
_PE_TTM_LOCK = threading.Lock()  # 守卫全量缓存的构建（单飞）
_PE_TTM_RETRY_TTL = 60           # 全量拉取失败后的退避秒数（抑制每次开股票都重试 1.3s 网络）
_PE_TTM_LAST_FAIL = 0.0
# 指数 / 港股走腾讯，属「打开即取」，不做进程级缓存（腾讯是单只批量接口，
# 一次请求只查一只，成本与全量缓存策略无关）。


def _eltdx_markets():
    """eltdx 统计口径覆盖的市场（A 股）。"""
    return ("sh", "sz", "bj")


def _load_pe_ttm_snapshot():
    """读落盘的 stock_pettm.json，返回 {mkt+code: float}；无文件 / 损坏返回 {}。"""
    path = app_data.stock_pettm_file
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in data.items():
            if isinstance(v, (int, float)) and v != 0:
                out[k] = float(v)
        log.info(f"[PE-TTM] 已加载本地镜像 {len(out)} 条: {path}")
        return out
    except Exception as e:
        log.warning(f"[PE-TTM] 本地镜像读取失败({path}): {e}")
        return {}


def _save_pe_ttm_snapshot(mapping):
    """把全量 PE-TTM 落盘到 stock_pettm.json（原子写）。失败只告警，不影响内存态。"""
    path = app_data.stock_pettm_file
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # tmp 名含 pid+线程：批量扫描用 ProcessPool，spawn 出的 worker 各自会走到
        # 本函数（各自拉 PE 全量并落盘）。共用固定 tmp 名会互相截断，且 Windows 上
        # os.replace 撞到其它进程持有的句柄会报 WinError 32（实测 2026-09-12）。
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        log.info(f"[PE-TTM] 全量 {len(mapping)} 条已落盘: {path}")
        return True
    except Exception as e:
        log.warning(f"[PE-TTM] 落盘失败({path}): {e}")
        return False


def _prime_pe_ttm_all():
    """构建全 A 股 PE-TTM 进程缓存（single-flight 内调用）。

    优先级：① eltdx 全量拉取（成功即落盘）；② 落盘镜像 stock_pettm.json；
    ③ 两者都没有 → 抛 RuntimeError（不静默降级为空表）。
    """
    global _PE_TTM_ALL, _PE_TTM_PRIMED, _PE_TTM_PRIMED_AT, _PE_TTM_LAST_FAIL

    # ① eltdx 全量：read_stats() 单次请求即覆盖全市场（约 1.3s），
    #    与逐只取数成本相同，故一次拉全表。
    result = None
    try:
        result = _eltdx_fetch_pe_ttm_all()
    except Exception as e:
        log.error(f"[PE-TTM] eltdx 全量取数失败: {type(e).__name__}: {e}")
        result = None

    if result:
        _PE_TTM_ALL = result
        _PE_TTM_PRIMED = True
        _PE_TTM_PRIMED_AT = time.time()
        _save_pe_ttm_snapshot(result)
        return

    # ② 落盘镜像
    snap = _load_pe_ttm_snapshot()
    if snap:
        _PE_TTM_ALL = snap
        _PE_TTM_PRIMED = True
        _PE_TTM_PRIMED_AT = time.time()
        log.warning("[PE-TTM] eltdx 不可用，本次使用本地镜像（数据可能陈旧）")
        return

    # ③ 都无法获得
    _PE_TTM_LAST_FAIL = time.time()
    _PE_TTM_PRIMED = False
    raise RuntimeError(
        "[PE-TTM] 无法获取全 A 股 PE-TTM：eltdx 取数失败，且本地镜像 "
        f"{app_data.stock_pettm_file} 不存在或不可用（请检查 eltdx/网络，或先在能联网时"
        "打开过一次股票以生成镜像）")


def _fetch_pe_ttm_live(market, code):
    """PE-TTM 取数单一入口（注入 AppData，供 K 线页面打开标的时调用）。

    分流（唯一实现，替代原先的 DataAPI/MarketStatsAPI.py）：
      · A 股**指数**（sh000001 上证指数 / sh000300 沪深300 / sz399001 等）
        → 腾讯（eltdx 统计文件不含指数，走 eltdx 必然取空）
      · 港股 → 腾讯
      · A 股个股（sh/sz/bj）→ eltdx 全量进程缓存
    返回 {mkt+code: float}；该票确实无 PE 时返回 {}。
    数据源整体不可用时**抛异常**（交由 AppData 记 error），不静默返回 {} ——
    否则「取数失败」与「该票无 PE」不可区分。
    """
    market = (market or "").lower()
    if market not in _eltdx_markets() or is_index(market, code):
        # 指数 / 港股：腾讯单只实时（改造前方案）
        return _tx_fetch_pe_ttm([(market, code)])

    return _pe_ttm_from_all_cache(market, code)


def _pe_ttm_from_all_cache(market, code):
    """从 A 股全量进程缓存取一只；未就绪则先构建（single-flight + 失败退避）。"""
    global _PE_TTM_PRIMED, _PE_TTM_LAST_FAIL
    if _PE_TTM_PRIMED:
        v = _PE_TTM_ALL.get(market + code)
        return {market + code: v} if v is not None else {}

    # 退避窗口内不再重试，避免"每次开股票都等一次 1.3s 网络超时"
    if _PE_TTM_LAST_FAIL and time.time() - _PE_TTM_LAST_FAIL < _PE_TTM_RETRY_TTL:
        raise RuntimeError("[PE-TTM] 全量缓存暂不可用（上次取数失败，退避中）")

    if not _PE_TTM_LOCK.acquire(blocking=False):
        # 已有线程在构建全量缓存：本次直接返回当前表（可能暂无该票）
        v = _PE_TTM_ALL.get(market + code)
        return {market + code: v} if v is not None else {}
    try:
        if not _PE_TTM_PRIMED:
            _prime_pe_ttm_all()
    finally:
        _PE_TTM_LOCK.release()
    v = _PE_TTM_ALL.get(market + code)
    return {market + code: v} if v is not None else {}


# 注入取数实现（依赖倒置：AppData 不得 import DataAPI，phase5 守卫 ④b）。
# 导入本模块即成注入——真实运行链 FrontAPI → AppOrch → AppRefresh 必然经过。
app_data.set_pe_ttm_live_fetcher(_fetch_pe_ttm_live)


# 股票名称缓存别名 = app_data 实例字段（共享同一对象）
# key: 股票代码(6位), value: {"name": "股票名称", "pinyin": "拼音首字母"}
# 只用于**判空**（:360 的 `if _stock_names_cache:`）——真值测试在 CPython
# 下是原子的；遍历一律走 app_data.names_snapshot()。
_stock_names_cache = app_data.names_cache

# 【已删除】_pe_ttm_cache = app_data.pe_cache
#          _index_belong_cache = app_data.belong_cache
# 审计 P3：两条都是死别名（本模块零引用）。P1-2 把 PE/归属表的读写全部收进
# app_data.update_pe_ttm() / pe_snapshot() / belong_snapshot() 之后，这两
# 个别名就没用了。留着等于给"绕开锁直接全表遍历"留一个现成入口。

# 「残缺表不得覆盖完好表」门槛（刷新落盘前的最后一道闸）
# 补全链路整体故障时（拼参错误 / 接口被封），合并表里绝大多数条目没有名称，
# 会被步骤5 的过滤器剔除；若照旧落盘并 replace_names，**一次坏刷新就把完好的
# 名称表永久换成了残表** —— 表现为页面名称退化成代码、拼音搜不到，且重启也
# 不恢复（残表已落盘）。故新表条目数不足旧表该比例时视为刷新异常：保留旧表。
# 放本模块而非 AppConfig：这是刷新流程的内部安全阈值、不是可配置业务参数，
# 放配置层会牵动 phase2 的「AppConfig↔AppData property 配对」守卫，扩大波及面。
_REFRESH_NAMES_MIN_KEEP_RATIO = 0.5

# 刷新状态（股票名称刷新用；获取侧状态）
# 访问者：刷新工作线程（写）+ /api/stocks/refresh/read|POST 的 REST 线程
# （读）。running 的「检查后置位」必须原子，否则两个并发 POST 会同时通过
# 检查起两条刷新线程 —— 故配 _refresh_state_lock 做 CAS。
_refresh_status = {"running": False, "progress": 0, "total": 0, "loaded": 0, "error": None, "step": ""}
_refresh_state_lock = threading.Lock()


# 「起线程排队门」：与 _refresh_status 共用 _refresh_state_lock。
# 与 running 旗分离——running 由装饰器在子线程内 CAS，本旗只用于让
# refresh_stock_names_async 如实回答「到底是不是我启动的」（审计 P2）。
_refresh_starting = False


def _set_refresh_status(**fields):
    """加锁写入刷新状态（审计 P2）。

    原实现只有**读者**（refresh_status）持 _refresh_state_lock，写者
    （step / error / total / loaded / running）全部裸写。dict 的键集合固定
    所以不会崩，但这是「看起来有锁」的典型：读快照与写之间没有真正的
    互斥，并发下可能读到 step/error/loaded 互相错配的组合（例如 step 已是
    新一轮的值、error 还是上一轮的）。CAS 那部分本来就是对的，其余是装饰。

    ⚠ 本锁是**非重入**的 threading.Lock：已在 `with _refresh_state_lock:`
    内部的直接赋值（_reset_refresh_running 的 running 检查-置位）不要改
    用本函数，否则自死锁。
    """
    with _refresh_state_lock:
        _refresh_status.update(fields)

# AKShare 指数代码 → 市场前缀映射
# ═══════════════════════════════════════════════════════════════════════
# 名称 / 指数归属 缓存
# ═══════════════════════════════════════════════════════════════════════
# AKShare 交易所映射 / 指数归属映射常量已在 DataAPI/AkshareAPI.py 统一收纳
# （AKSHARE_EXCHANGE_MAP / AKSHARE_INDEX_MAP），此处经顶部 import 复用，不再本地重复定义。

def load_stock_names_from_cache_file():
    """加载股票名称缓存（AppData 直连）"""
    return app_data.load_stock_names_from_cache_file()


def load_index_belong_cache():
    """加载指数归属缓存（AppData 直连；新旧文件名自动迁移，见 AppData）"""
    return app_data.load_index_belong_cache()


def get_pe_ttm(market, code):
    """获取 PE-TTM（AppData 直连）"""
    return app_data.get_pe_ttm(market, code)


def get_index_belong(market, code):
    """获取指数归属（AppData 直连）"""
    return app_data.get_index_belong(market, code)


# ═══════════════════════════════════════════════════════════════════════
# 刷新实现
# ═══════════════════════════════════════════════════════════════════════

def _safe_write_json_file(path, data, *, ensure_ascii=False, indent=None):
    """先写临时文件并校验 JSON 可读，再原子覆盖（委托 app_data）"""
    from App.AppData import safe_write_json_file
    return safe_write_json_file(path, data, ensure_ascii=ensure_ascii, indent=indent)


def _collect_codes_from_vipdoc(vipdoc_dir):
    """委托 DataAPI/TdxAPI（vipdoc_dir 由调用方注入）"""
    return collect_codes_from_vipdoc(vipdoc_dir)


def _fetch_names_from_sina_once(codes_dict):
    """
    一次性从新浪财经API获取股票名称，用于首次建立缓存。
    参数 codes_dict: {code: {"name": "", ...}} —— 只获取 name 为空的条目。
    返回补充了多少条名称。
    注意：新浪API不支持A股和港股混合请求，必须分开调用。
    """
    # 只获取没有名称的代码
    codes_missing = [compound_key for compound_key, info in codes_dict.items() if not info.get("name")]
    if not codes_missing:
        return 0

    # 按市场分组：A股和港股必须分开请求
    a_stock_codes = []
    hk_codes = []
    # ★ 键必须是 (market, bare_code) 复合键 —— 不能只用 bare_code。
    # 沪深存在大量同号代码（sh000001 上证指数 vs sz000001 平安银行、
    # sh000002 vs sz000002 …）：若按 bare_code 建索引，后遍历到的一只会把前
    # 一只覆盖掉，于是两只里必然有一只拿不到名字 → 被步骤5 的「无名称」过滤
    # 删除 → 搜索联想里 000001 只剩一条（实测丢的正是上证指数：vipdoc 先扫
    # sh 再扫 sz，sz 后写入覆盖了映射）。
    compound_key_map = {}   # (market, bare_code) -> compound_key
    for compound_key in codes_missing:
        market = codes_dict[compound_key].get("market", "")
        # 从复合键提取纯代码：去掉前缀 sh/sz/hk
        if market and compound_key.startswith(market):
            bare_code = compound_key[len(market):]
        else:
            bare_code = compound_key
        compound_key_map[(market, bare_code)] = compound_key
        if market == "hk":
            hk_codes.append(bare_code)
        else:
            a_stock_codes.append((bare_code, market))

    filled = 0

    # === 第一轮：A股（新浪财经，经 SinaAPI 收口）===
    if a_stock_codes:
        name_map = fetch_a_names(a_stock_codes)
        for bare_code, _market in a_stock_codes:
            name = name_map.get(_market + bare_code)
            if name:
                compound_key = compound_key_map.get((_market, bare_code))
                if compound_key in codes_dict:
                    codes_dict[compound_key]["name"] = name
                    filled += 1

    # === 第二轮：港股（用腾讯财经API，新浪港股接口已失效；经 TxAPI 收口）===
    if hk_codes:
        name_map = fetch_hk_names(hk_codes)
        for bare_code in hk_codes:
            name = name_map.get("hk" + bare_code)
            if name:
                compound_key = compound_key_map.get(("hk", bare_code))
                if compound_key in codes_dict:
                    codes_dict[compound_key]["name"] = name
                    filled += 1

    return filled


def _fetch_index_belong_from_akshare(timeout=30):
    """
    通过 AKShare index_stock_cons_csindex 接口在线获取沪深300/中证500/中证1000 最新成分股，
    构建 stock→指数归属 反向映射。返回 {market+code: "沪深300"|"中证500"|"中证1000"}。
    如果 AKShare 不可用或网络异常，返回空字典。每个指数单独设置超时。
    （归属缓存由 app_data 持有，经 replace_index_belong 同对象替换）
    """
    def _fetch_one(_idx_code, _idx_name):
        try:
            _set_refresh_status(step=f"刷新指数归属: {_idx_name}...")
            log.info(f"[指数归属] 开始获取 {_idx_name}({_idx_code})...")
            items = fetch_index_cons(_idx_code)
            count = 0
            for item in items:
                mkt = item["market"]
                if mkt:
                    result[mkt + item["code"]] = _idx_name
                    count += 1
            log.info(f"[指数归属] ✅ {_idx_name}({_idx_code}): 成功获取 {count}只 成分股")
        except Exception as e:
            log.info(f"[指数归属] {_idx_name}({_idx_code}) 获取失败: {e}")

    import concurrent.futures
    result = {}
    for index_code, index_name in AKSHARE_INDEX_MAP.items():
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_fetch_one, index_code, index_name)
            try:
                future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                log.info(f"[指数归属] {index_name}({index_code}) 获取超时({timeout}s)，跳过")
        finally:
            executor.shutdown(wait=False)  # 不等待卡住的线程，直接进入下一个指数

    if result:
        app_data.replace_index_belong(result)
    else:
        # 空结果不替换：replace_index_belong 是 clear()+update() **整体替换**，
        # 网络全挂时用它会把内存里已有的指数归属一并清空（改造前即如此，随后
        # 还会把空表落盘）。指数归属季度才变，保留旧值远优于清空。
        log.info("[指数归属] 本次未获取到任何指数归属，保留内存中的既有数据")
    return result


def _refresh_index_belong():
    """
    刷新指数归属（AKShare 在线获取），落盘 stock_index_belong.json。

    历史说明：本函数原为 _refresh_pe_ttm，同时刷新 PE-TTM 与指数归属并**合并**
    写入 stock_pettm_index.json。PE-TTM 每日随行情变动、指数归属季度调仓才变，
    两者时间维度不匹配，且实际使用中不会每天点刷新 → 那份缓存的 PE 必然陈旧。
    故 PE-TTM 改为「打开 K 线页面实时取数」（见 AppData PE-TTM 实时层），不再
    参与本刷新、不再落盘；本函数只保留指数归属部分——它适合缓存。

    落盘走 AppData.save_index_belong_cache()（原子写 + 过滤旧格式纯数字 key +
    空表保护），不再由本模块手工拼装合并字典。
    """
    _set_refresh_status(step="刷新指数归属...")
    load_index_belong_cache()   # 先加载已有缓存（含新旧文件名一次性迁移）
    log.info("[指数归属] ========== 开始刷新指数归属 ==========")
    try:
        _fetch_index_belong_from_akshare()
    except Exception as e:
        log.info(f"[指数归属] 获取失败: {e}")
        _set_refresh_status(error=f"指数归属获取失败: {e}")
        return

    snap = app_data.belong_snapshot()
    if not snap:
        log.info("[指数归属] 本次未获取到任何指数归属，保留既有文件不落盘")
        return
    if app_data.save_index_belong_cache():
        log.info(f"[指数归属] ✅ 刷新完成: 共 {len(snap)} 条，已保存到 "
                 f"{app_data.stock_index_belong_file}")
    else:
        _set_refresh_status(error="保存指数归属失败")


def _reset_refresh_running(fn):
    """装饰器：统一管理 running 旗（守卫 + 置位 + try/finally 复位）。

    原实现 running=True 只在函数末尾（617）复位，中途异常会卡死运行旗，
    后续所有刷新请求直接返回 already_running；后改为无条件 finally 复位，
    又破坏了重入守卫——撞守卫的调用（running 为 True 时的早退）也会把旗标
    误抹掉、允许第二个刷新并发进入。现收敛为：守卫只判不写（running 时
    直接返回，不进 finally 复位）；真正进入才置位，无论正常完成还是异常
    逃逸都复位。三种路径（守卫早退/正常/异常）互不串扰。
    """
    import functools

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        # 检查 + 置位在同一把锁内（CAS）：并发调用只有一个能进入
        with _refresh_state_lock:
            if _refresh_status["running"]:
                return                  # 撞守卫：直接返回，不触碰 flag
            _refresh_status["running"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            with _refresh_state_lock:
                _refresh_status["running"] = False
    return _wrapper


@_reset_refresh_running
def _refresh_stock_names():
    """
    从本地文件批量获取全市场股票名称，保存到 stock_names.json。
    数据来源优先级：
      1. vipdoc/*.day 文件名（收集所有已下载过数据的股票代码）
      2. 新浪财经API（为无名称的代码批量查询名称）
    （名称缓存由 app_data 持有；本函数只做获取与合并，
     最终经 replace_names 同对象替换，_stock_names_cache 别名全程可见）
    """
    # running 守卫与置位由装饰器 _reset_refresh_running 统一管理，此处不再重复
    _set_refresh_status(step="刷新股票名...")
    _set_refresh_status(error=None)
    log.info("[股名刷新] ========== 开始刷新股票名称 ==========")

    # === 先加载已有缓存，新数据合并进去，不覆盖 ===
    raw_names = {}
    load_stock_names_from_cache_file()
    if _stock_names_cache:
        # 审计 P1-2：遍历共享表须走快照。本线程虽是 names 的主要写者，但
        # REST 线程的 load_stock_names_from_cache_file 也会 `self._names
        # .update(...)`（惰性加载），与此处遍历可并发 → 同样会触发
        # 「dictionary changed size during iteration」或静默串表。
        for code, info in app_data.names_snapshot().items():
            if isinstance(info, dict):
                raw_names[code] = info
            else:
                raw_names[code] = {"name": info, "pinyin": ""}
        log.info(f"[股名刷新] 步骤1/5 加载缓存: 已加载 {len(raw_names)} 只")
    else:
        log.info("[股名刷新] 步骤1/5 加载缓存: 无缓存，全新读取")

    # === 方案1: vipdoc .day文件名收集代码 ===
    # .day 文件覆盖所有已下载过K线数据的股票
    vipdoc_codes = _collect_codes_from_vipdoc(app_config.vipdoc_dir)
    # 统计扫描结果
    v_sh = sum(1 for v in vipdoc_codes.values() if v.get("market") == "sh")
    v_sz = sum(1 for v in vipdoc_codes.values() if v.get("market") == "sz")
    v_hk = sum(1 for v in vipdoc_codes.values() if v.get("market") == "hk")
    v_total = v_sh + v_sz + v_hk
    cache_before = len(raw_names)
    vipdoc_new = 0   # 缓存中没有的新代码
    vipdoc_filled = 0  # 缓存中有但无名称，从vipdoc补全
    for code, info in vipdoc_codes.items():
        if code not in raw_names:
            raw_names[code] = info
            vipdoc_new += 1
        elif not raw_names[code].get("name"):
            raw_names[code]["name"] = info.get("name", "")
            vipdoc_filled += 1
    log.info(f"[股名刷新] 步骤2/5 合并扫描: vipdoc共{v_total}只 (sh{v_sh}+sz{v_sz}+ds{v_hk}), 缓存{cache_before}只, 合并后{len(raw_names)}只 (新增{vipdoc_new}只)")

    # === 方案2: 新浪API补全缺失的名称 ===
    # 即使已有缓存，如果有新发现的代码（如港股）没有名称，也要补全
    codes_without_name = [c for c, info in raw_names.items() if not info.get("name")]
    if codes_without_name:
        a_no = sum(1 for c in codes_without_name if raw_names[c].get("market") != "hk")
        hk_no = sum(1 for c in codes_without_name if raw_names[c].get("market") == "hk")
        log.info(f"[股名刷新] 步骤3/5 补全名称: {len(codes_without_name)} 只无名称 (A股{a_no}, 港股{hk_no})")
        temp_dict = {c: raw_names[c] for c in codes_without_name}
        filled = _fetch_names_from_sina_once(temp_dict)
        for code, info in temp_dict.items():
            if info.get("name"):
                raw_names[code] = info
        failed = len(codes_without_name) - filled
        if failed > 0:
            # 失败率过半说明「不是个别票取不到」而是链路级故障（如拼参错误 /
            # 接口被封），用 warning 让它跳出 INFO 噪音——此前的静默 INFO
            # 让 5000+ 只名称整体丢失只表现为一行和平的日志。
            _rate = failed * 100.0 / max(1, len(codes_without_name))
            _msg = (f"[股名刷新]   └─ 补全结果: 成功 {filled} 只, "
                    f"失败 {failed} 只 ({_rate:.0f}%)")
            if _rate >= 50:
                log.warning(_msg)
            else:
                log.info(_msg)
        else:
            log.info(f"[股名刷新]   └─ 补全结果: 全部成功 {filled} 只")
    else:
        log.info("[股名刷新] 步骤3/5 补全名称: 无需补全")

    # === 补充通达信板块指数名称（88xxxx系列，如880491半导体、881319半导体）===
    # 88xxxx代码不以标准A股格式开头，_is_a_stock_code() 会过滤掉，所以不在 raw_names 中。
    # 来源: tdxzs.cfg（通达信配置文件）和 AppData 内嵌映射表（本地映射）
    tdxzs_filled = 0
    tdxzs_file = os.path.join(app_config.tdx_hq_cache, "tdxzs.cfg")
    if os.path.exists(tdxzs_file):
        try:
            with open(tdxzs_file, "r", encoding="gbk", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("|")
                    if len(parts) >= 2:
                        name = parts[0].strip()
                        code = parts[1].strip()
                        if "." in code:
                            code = code.split(".")[0]
                        if not name or not code:
                            continue
                        # 跳过 8803xx-8804xx（旧版行业），由 881 研究行业替代
                        if code.startswith("8803") or code.startswith("8804"):
                            continue
                        compound_key = "sh" + code
                        if compound_key not in raw_names:
                            raw_names[compound_key] = {"name": name, "pinyin": "", "market": "sh"}
                            tdxzs_filled += 1
                        elif not raw_names[compound_key].get("name"):
                            raw_names[compound_key]["name"] = name
                            tdxzs_filled += 1
        except Exception as e:
            log.info(f"[股名刷新]   读取tdxzs.cfg失败: {e}")

    # 研究行业(881xxx)从本机权威源 tdxzs3.cfg 读取（单一加载函数
    # app_data.load_tdxhy_mapping；该源缺失时抛 RuntimeError，下表 try 兜住 ——
    # 补名失败不影响其余代码名）
    tdxhy_filled = 0
    try:
        _TDXHY_881_TO_X = app_data.load_tdxhy_mapping()[1]
        for code_881, (x_code, name) in _TDXHY_881_TO_X.items():
            compound_key = "sh" + code_881
            if compound_key not in raw_names:
                raw_names[compound_key] = {"name": name, "pinyin": "", "market": "sh"}
                tdxhy_filled += 1
            elif not raw_names[compound_key].get("name"):
                raw_names[compound_key]["name"] = name
                tdxhy_filled += 1
    except Exception as e:
        log.info(f"[股名刷新]   加载行业映射失败: {e}")

    block_filled = tdxzs_filled + tdxhy_filled
    log.info(f"[股名刷新] 步骤4/5 补充板块: tdxzs.cfg +{tdxzs_filled}条, tdxhy +{tdxhy_filled}条, 共补全 {block_filled} 条板块")

    # === 统一用pypinyin生成拼音首字母（忽略tnf文件中的拼音，确保格式一致） ===
    try:
        from pypinyin import lazy_pinyin
        all_names = {}
        for code, info in raw_names.items():
            if isinstance(info, dict):
                name = info.get("name", "")
                market = info.get("market", "")  # 保留市场字段
            else:
                name = str(info)
                market = ""
            # 始终用pypinyin生成拼音首字母，确保搜索的一致性
            # 通达信/新浪API中名称可能含空格（如"五 粮 液"）或全角字母（如"鲁泰Ａ"）→ 统一清理
            name_clean = name.replace(" ", "")
            # 全角ASCII → 半角: U+FF01-U+FF5E → U+0021-U+007E
            name_clean = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in name_clean)
            pinyin = ""
            if name_clean:
                try:
                    py_list = lazy_pinyin(name_clean)
                    pinyin = "".join([p[0].upper() for p in py_list if p])
                except Exception:
                    pinyin = ""
            all_names[code] = {"name": name_clean, "pinyin": pinyin, "market": market}
    except ImportError:
        all_names = {}
        for code, info in raw_names.items():
            if isinstance(info, dict):
                name = info.get("name", "")
                market = info.get("market", "")
            else:
                name = str(info)
                market = ""
            all_names[code] = {"name": name, "pinyin": "", "market": market}

    # === 过滤 ST、*ST、退市股票，不写入缓存 ===
    filtered_count = 0
    filtered_empty = 0
    filtered_st = 0
    filtered_delist = 0
    for _code in list(all_names.keys()):
        name = all_names[_code].get("name", "")
        if not name:
            del all_names[_code]
            filtered_count += 1
            filtered_empty += 1
        elif name.startswith("*ST") or name.startswith("ST"):
            del all_names[_code]
            filtered_count += 1
            filtered_st += 1
        elif "退" in name:
            del all_names[_code]
            filtered_count += 1
            filtered_delist += 1
    # === 步骤5 落盘前的质量门槛（见 _REFRESH_NAMES_MIN_KEEP_RATIO 注释）===
    _old_count = len(app_data.names_snapshot())
    _new_count = len(all_names)
    _keep = bool(all_names) and (
        _old_count == 0 or _new_count >= _old_count * _REFRESH_NAMES_MIN_KEEP_RATIO)
    if all_names and not _keep:
        log.warning(
            f"[股名刷新] 步骤5/5 拒绝覆盖: 新表 {_new_count} 只不足旧表 {_old_count} 只的 "
            f"{_REFRESH_NAMES_MIN_KEEP_RATIO:.0%}，判定为名称补全链路异常 "
            f"→ 保留旧名称表（不覆盖内存、不落盘）。请检查补全数据源。")
    elif all_names:
        os.makedirs(os.path.dirname(app_config.stock_names_cache_file), exist_ok=True)
        _safe_write_json_file(app_config.stock_names_cache_file, all_names, ensure_ascii=False)
        app_data.replace_names(all_names)  # 同对象替换：别名 _stock_names_cache 即时可见
        sh_count = sum(1 for c in all_names if all_names[c].get("market") == "sh")
        sz_count = sum(1 for c in all_names if all_names[c].get("market") == "sz")
        hk_count = sum(1 for c in all_names if all_names[c].get("market") == "hk")
        if filtered_count > 0:
            parts = []
            if filtered_st: parts.append(f"ST/*ST {filtered_st}只")
            if filtered_delist: parts.append(f"退市 {filtered_delist}只")
            if filtered_empty: parts.append(f"无名 {filtered_empty}只")
            log.info(f"[股名刷新] 步骤5/5 过滤保存: 过滤 {filtered_count} 只 ({', '.join(parts)}), 最终 {len(all_names)} 只 (上海{sh_count}, 深圳{sz_count}, 港股{hk_count})")
        else:
            log.info(f"[股名刷新] 步骤5/5 过滤保存: 最终 {len(all_names)} 只 (上海{sh_count}, 深圳{sz_count}, 港股{hk_count})")
        log.info(f"[股名刷新] ✅ 刷新完成: 共 {len(all_names)} 只股票名称, 已保存到 {app_config.stock_names_cache_file}")
    else:
        log.info("[股名刷新] 步骤5/5 过滤保存: 失败，未获取到任何数据")

    # 刷新板块文件（block_zs.dat / block_gn.dat / block_fg.dat / block.dat）
    log.info("[板块刷新] ========== 开始刷新板块文件 ==========")
    _set_refresh_status(step="刷新成分股...")
    try:
        def _set_step(msg):
            _set_refresh_status(step=msg)
        refresh_block_files(progress_callback=_set_step)
    except Exception as e:
        log.info(f"[板块刷新] 板块文件刷新失败: {e}")

    # 刷新指数归属（PE-TTM 已改为「打开 K 线页面实时取数」，不再随刷新按钮更新）
    try:
        _refresh_index_belong()
    except Exception as e:
        log.info(f"[指数归属] 指数归属刷新失败: {e}")
        _set_refresh_status(error=f"指数归属刷新失败: {e}")

    # 全部刷新完成，标记状态
    _set_refresh_status(running=False)
    _set_refresh_status(step="")


# ═══════════════════════════════════════════════════════════════════════
# 股票名称刷新（异步）
# ═══════════════════════════════════════════════════════════════════════

def refresh_status():
    """股票名称刷新状态（快照副本，避免调用方拿到可变的内部字典）"""
    with _refresh_state_lock:
        return dict(_refresh_status)


def refresh_stock_names():
    """刷新股票名称（阻塞）"""
    return _refresh_stock_names()


def refresh_stock_names_async():
    """异步启动股票名称刷新（不阻塞请求线程）

    仅做「快速预检查」：running 已为 True 时直接返回 already_running，避免再起一条
    注定被守卫拦截的线程。真正的 CAS（检查 + 置位）由装饰器 _reset_refresh_running
    包裹的 _refresh_stock_names 在子线程内完成，finally 中无条件复位，保证 running
    不会卡死。注意：此处【不得】提前把 running 旗置为 True —— 否则子线程进入被装饰函数
    时守卫已见 running=True 而早退，刷新正文永不执行且 running 永久卡死。
    """
    global _refresh_starting
    with _refresh_state_lock:
        if _refresh_status["running"]:
            return {"status": "already_running", **_refresh_status}
        # 审计 P2：原实现「预检查 → 放锁 → 起线程」，两个并发 POST 会在
        # 窗口期双双通过预检查、双双返回 "started"；但真正的 CAS 在装饰器
        # 里，只有一条线程能进正文，另一条**静默退出**——调用方（前端）
        # 却以为自己启动成功了。
        # 这里在同一把锁内加一道「起线程排队门 _refresh_starting」：谁先
        # 置位谁负责起线程，落后者如实返回 already_running。
        # 注意：这只是**排队去重**，不代替装饰器里的 CAS——running 旗仍
        # 由装饰器在子线程内检查-置位（本函数若提前置 running 会让正文
        # 永不执行且 running 卡死，见上方注释）。
        if _refresh_starting:
            return {"status": "already_running", **_refresh_status}
        _refresh_starting = True

    def _do_refresh():
        global _refresh_starting
        try:
            _refresh_stock_names()
        except Exception as e:
            traceback.print_exc()
            log.error(f"[错误] refresh_stock_names异常: {e}")
        finally:
            with _refresh_state_lock:
                _refresh_starting = False

    try:
        t = threading.Thread(target=_do_refresh, daemon=True)
        t.start()
    except Exception as e:
        # 起线程失败必须归还排队门，否则后续刷新永远被判 already_running
        with _refresh_state_lock:
            _refresh_starting = False
        log.error(f"[错误] 刷新线程启动失败: {e}")
        return {"status": "error", "msg": f"刷新线程启动失败: {e}"}
    return {"status": "started", "msg": "股票名称刷新已启动"}


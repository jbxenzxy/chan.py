# -*- coding: utf-8 -*-
"""
App/AppScan.py —— 股票扫描功能域
=========================================================================
点击页面右上角「股票扫描」按钮后的操作。

本模块收纳：
  - Scanner 类（批量扫描服务：遍历代码列表、逐票调用 analyze_stock、
    汇总结果、追踪进度）+ 全局单例 scanner
  - 自选股读写（read_zxg_stocks / zxg_save / get_annotated_codes）
  - 同花顺云端自选股（save_scan_to_ths_cloud）
  - 扫描预过滤 + 行业索引读取 + 扫描态（_quick_prefilter_pass /
      read_tdxhy_l2_indices / read_tdxhy_l3_indices 等）
  - 流通市值批量获取 + 缓存壳（_fetch_float_mc_from_tencent /
      fetch_float_mc_from_tencent / load_float_mc_cache / update_float_mc_cache）
  - Windows 扫描完成通知（_send_windows_notification）

依赖方向：AppScan.py → AppEngine / AppData / AppScanPool（单向）
批量扫描提交/状态/中止委托 AppScanPool（入口适配器），共享结果经 SQLite
AppScanStore 跨进程；本模块保持纯业务、零并发框架依赖。
"""
import itertools
import os
import threading
import time
import traceback

# 分析引擎层（App/AppEngine.py）
from App import AppEngine as _m

# 结构化缓存键工厂（消除字符串拼接歧义与漂移）
from App.AppData import app_data, make_live_key

# 配置中心（扫描预过滤阈值等）
from App.AppConfig import app_config

# 板块成分读取（扫描来源 page_index）
from DataAPI.TdxAPI import get_index_stocks

# 流通市值批量获取（腾讯行情接口，经 TxAPI 收口）
from DataAPI.TxAPI import fetch_float_mc

from App.AppLog import get_logger
log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 扫描态 + 预过滤 / 行业索引读取
# ═══════════════════════════════════════════════════════════════════════

# 股票名称缓存：别名 = app_data 实例字段（共享同一对象）
_stock_names_cache = app_data.names_cache

# 扫描跳过记录（收集后统一打印）
#
# 演进说明（P1-5 → X3）：
#   P1-5 阶段：列表是**模块级全局**，且整个逃出 AppOrch.SHARED_RESOURCE_
#   REGISTRY，既没登记也没加锁。两个执行体在碰它（收割线程追加、REST 线程
#   清空/遍历/计数），`.clear()` 与 `len()`/遍历不是原子的组合操作，清空
#   发生在遍历中途会让汇总明细整批丢失或串批次——不抛异常，一直没人发现。
#   当时的修法是加 _scan_skip_log_lock 并收敛出四个加锁访问器。
#   X3 阶段：加锁解决了崩溃与丢失，但没解决**跨页串批**——它仍是进程级
#   单例，两个页面同时扫描时，A 页的开始会清空 B 页的记录，B 页结束时会
#   打印 A 页的明细。故本列表进一步下沉为**每次扫描私有**
#   （_ScanSession.skip_log，见下方会话区），此处不再保留全局副本。
# 访问器 append_scan_skip / clear_scan_skip / scan_skip_snapshot /
# scan_skip_count 保留（签名新增可选 scan_token），按 token 定位会话。


def append_scan_skip(msg, scan_token=None):
    """加锁追加一条跳过记录（扫描线程 / 收割线程调用）

    审计 X3：按 scan_token 归属到**发起它的那一次扫描**，不同页面的扫描
    各写各的，不会互相串批。未提供 token（旧调用方）时回退到最近一次会话。
    """
    sess = get_scan_session(scan_token)
    if sess is None:
        return
    with sess.lock:
        sess.skip_log.append(msg)


def clear_scan_skip(scan_token=None):
    """加锁清空跳过记录（新一轮扫描开始时调用）"""
    sess = get_scan_session(scan_token)
    if sess is None:
        return
    with sess.lock:
        sess.skip_log.clear()


def scan_skip_snapshot(scan_token=None):
    """加锁取跳过记录的**快照**（遍历一律经此，勿直接碰列表本体）"""
    sess = get_scan_session(scan_token)
    if sess is None:
        return []
    with sess.lock:
        return list(sess.skip_log)


def scan_skip_count(scan_token=None):
    """加锁取跳过记录条数"""
    sess = get_scan_session(scan_token)
    if sess is None:
        return 0
    with sess.lock:
        return len(sess.skip_log)

# 【已删除】_scan_lock
# 它从未真正生效：① API 进程没有任何路由调用 scan_one（FrontAPI 只有
# submit / status / cancel 三个批量入口）；② 批量路径的 scan_one 跑在
# ProcessPool worker 内，每个 worker 串行取任务、且每个进程各持一份
# _scan_lock，永不竞争。保留它只会让人误以为「扫描靠这把锁保护引擎缓存」。
# 真正的隔离来自进程边界（spawn，每 worker 独立缓存）+ CChan 构建的
# 每请求数据注入。

# ═══════════════════════════════════════════════════════════════════════
# 扫描会话上下文（审计 X3：任务级状态不得放在进程级）
# ═══════════════════════════════════════════════════════════════════════
# 原实现用模块级全局承载「当前这一次扫描」的上下文：
#     _scan_start_time（开始时间）、_page_index_code（板块指数代码）
# 它们是**任务级**状态（属于某一次扫描），却放在**进程级**、无锁、无分片键。
#
# 多网页下后发起的扫描会覆盖先发起的参数（维度 0.2「模式 A」）。最危险的
# 是 _page_index_code：A 页选沪深300成分股、B 页选中证500 → 两页都按最后
# 设置的那个扫，**静默扫错一整批股票**，且没有任何报错。
#
# 注意：**给它补一把锁并不能解决问题**——加锁只是让两页「串行地互相覆盖」，
# 锁的类型选对了、层级错了，结果仍然错（指导书维度 0.3）。真正的修法是
# 让它不再共享：每次扫描一个私有会话对象，按 scan_token 索引。
#
# 会话内承载：开始时间（耗时统计）、板块指数代码、跳过记录。
# start() 建会话并返回 token；end() / append_scan_skip() / stock_list()
# 一律按 token 定位自己那一份，不同页面互不干扰。


class _ScanSession:
    """一次扫描的私有上下文（每页一份，进程内按 token 隔离）"""

    __slots__ = ("token", "start_time", "page_index_code", "skip_log", "lock")

    def __init__(self, token, page_index_code=None):
        self.token = token
        self.start_time = time.time()
        self.page_index_code = page_index_code
        self.skip_log = []                    # 本会话的跳过记录
        self.lock = threading.Lock()          # 只护本会话的 skip_log

    def elapsed(self):
        return time.time() - self.start_time


_scan_sessions = {}                    # scan_token -> _ScanSession
_scan_sessions_lock = threading.Lock()  # 只护注册表本身（dict 级别）
_scan_token_seq = itertools.count(1)
# 旧客户端（不传 scan_token）回退到最近一次会话，保持向后兼容
_legacy_scan_session = None
# task_id -> scan_token：让 ProcessPool 收割线程能把跳过记录写回**发起它的
# 那一次扫描**（收割线程只拿得到 task_id，见 AppScanPool._monitor_task）
_task_scan_token = {}


def _reap_stale_scan_sessions_locked():
    """清扫超时弃扫会话（审计 N3）。**调用方必须已持 _scan_sessions_lock。**

    TTL 可经环境变量 CHAN_SCAN_SESSION_TTL_SEC 覆盖（秒；非法值回落默认 24h）。
    每次调用现场读取，不把 env 值持久化进全局（便于测试内动态切换）。
    被清扫会话的 legacy 兜底引用一并置空；随后回收 _task_scan_token 中
    指向已不存在会话的死键，避免该表无界增长。
    """
    global _legacy_scan_session
    ttl = _SCAN_SESSION_TTL
    env_val = os.environ.get("CHAN_SCAN_SESSION_TTL_SEC")
    if env_val:
        try:
            ttl = float(env_val)
        except (TypeError, ValueError):
            pass
    now = time.time()
    stale = [t for t, s in _scan_sessions.items()
             if now - s.start_time > ttl]
    for t in stale:
        dropped = _scan_sessions.pop(t, None)
        if dropped is not None and _legacy_scan_session is dropped:
            _legacy_scan_session = None
    # 死键清扫：无论本轮是否有 stale，只要表非空就顺手扫一遍
    #（task_id 反查到已注销会话即为死键；表通常很小，扫描开销可忽略）
    if _task_scan_token:
        for tid, t in list(_task_scan_token.items()):
            if t not in _scan_sessions:
                _task_scan_token.pop(tid, None)


# 会话最长驻留时间（秒）。正常路径 end() 会注销会话；页面扫描中途被关闭/
# 刷新时 end() 永远不会被调（审计 N3），会话会驻留 _scan_sessions。
# 不引入后台线程：new_scan_session 每次建新会话前惰性清扫超时旧会话。
# 可用环境变量 CHAN_SCAN_SESSION_TTL_SEC 覆盖（测试/复现脚本用）；
# 默认 24h 远超一次正常扫描时长，误伤在跑会话的概率可忽略。
_SCAN_SESSION_TTL = 24 * 3600


def new_scan_session(page_index_code=None):
    """建一次扫描的私有会话，返回 (scan_token, session)

    审计 N3：建会话前惰性清扫超时旧会话（弃扫残留），同时回收
    _task_scan_token 中指向已不存在会话的死键（该表同样只增不减）。
    """
    global _legacy_scan_session
    with _scan_sessions_lock:
        _reap_stale_scan_sessions_locked()
        token = f"sc{next(_scan_token_seq)}"
        sess = _ScanSession(token, page_index_code)
        _scan_sessions[token] = sess
        _legacy_scan_session = sess
    return token, sess


def get_scan_session(scan_token=None):
    """取会话；无 token 时回退到最近一次会话（旧客户端兼容）"""
    if scan_token:
        with _scan_sessions_lock:
            return _scan_sessions.get(scan_token)
    return _legacy_scan_session


def drop_scan_session(scan_token=None):
    """结算并注销会话（注销后同名 token 再取返回 None，end() 幂等）"""
    global _legacy_scan_session
    sess = None
    with _scan_sessions_lock:
        if scan_token:
            sess = _scan_sessions.pop(scan_token, None)
        elif _legacy_scan_session is not None:
            sess = _legacy_scan_session
            _scan_sessions.pop(sess.token, None)
        if _legacy_scan_session is sess:
            _legacy_scan_session = None
    return sess


def bind_task_scan_token(task_id, scan_token):
    """把批量任务 task_id 绑定到发起它的扫描会话（供收割线程回写）"""
    if not task_id or not scan_token:
        return
    with _scan_sessions_lock:
        _task_scan_token[task_id] = scan_token


def scan_token_for_task(task_id):
    """按 task_id 反查扫描会话 token（收割线程用）"""
    if not task_id:
        return None
    with _scan_sessions_lock:
        return _task_scan_token.get(task_id)


def drop_task_scan_token(task_id):
    """任务终态后解绑，避免 _task_scan_token 只增不减"""
    if not task_id:
        return
    with _scan_sessions_lock:
        _task_scan_token.pop(task_id, None)


# 页面指数代码【已废弃·仅供兼容】
# 新路径：由前端在 /api/stocks/scan/read/candidates 上以 page_index_code
# 参数显式传入，随请求走，不落全局（见 Scanner.stock_list）。
# 保留本全局只为兼容仍在调用 PUT /scan/set/index 的旧客户端。
_page_index_code = None


def _send_windows_notification(title, message):
    """发送 Windows 10/11 Toast 通知（右下角弹出，扫描完成提示）。
    winotify 零依赖，仅需 PowerShell（Windows 内置），无需额外安装任何包。
    只需在 venv 中执行一次: pip install winotify
    在新线程中执行，不阻塞主流程。
    """
    def _notify():
        try:
            from winotify import Notification
            toast = Notification(app_id="缠论扫描", title=title, msg=message, duration="short")
            toast.show()
        except ImportError:
            log.info("[通知] 未安装 winotify，请在 venv 中执行: pip install winotify")
        except Exception as e:
            log.info(f"[通知] 发送失败: {type(e).__name__}: {e}")

    t = threading.Thread(target=_notify, daemon=True)
    t.start()


def _quick_prefilter_pass(market, code):
    """
    快速预过滤：检查ST/*ST/退市、流通市值条件。
    用于中证1000等大范围扫描时提前跳过不满足条件的股票。
    返回 (pass_filter, float_mc, skip_reason)：
      - pass_filter=True 表示通过过滤，可以继续分析
      - pass_filter=False 表示应跳过
      - skip_reason 为跳过原因字符串（如 "ST" / "流通市值<50亿"）
    """
    try:
        # 1. 过滤 ST/*ST/退市股票（通过名称缓存判断）
        try:
            compound_key = ("sh" if market == "1" else "sz" if market == "0" else "bj") + code
            info = _stock_names_cache.get(compound_key, {})
            name = info.get("name", "") if isinstance(info, dict) else str(info) if info else ""
            if name and (name.startswith("*ST") or name.startswith("ST") or "退" in name):
                return (False, None, "ST")
        except Exception:
            pass  # 名称查找失败不跳过

        # 2. 流通市值过滤：从缓存获取（扫描前已确保缓存有数据）
        # 阈值来自配置中心 app_config.scan_min_float_mc
        _min_float_mc = app_config.scan_min_float_mc
        float_mc = app_data.get_float_mc_from_cache(code)
        if float_mc is not None:
            if float_mc < _min_float_mc:
                return (False, float_mc, f"流通市值<{_min_float_mc:g}亿")
        else:
            log.info(f"[预过滤] {code} 流通市值未知")

        return (True, float_mc, None)
    except Exception as e:
        import traceback as _tb
        log.info(f"[预过滤] {code} 异常: {type(e).__name__}: {e}")
        _tb.print_exc()
        return (True, None, None)


def _fetch_float_mc_from_tencent(stock_list):
    """通过腾讯行情接口批量获取流通市值（经 TxAPI.fetch_float_mc 收口）。

    stock_list: [{"code": "600519", "prefix": "1"}, ...]
    返回: {code: float_mc(亿元)}，失败返回空字典。
    """
    return fetch_float_mc(stock_list)


def fetch_float_mc_from_tencent(stock_list):
    """从腾讯接口获取流通市值（获取侧）"""
    return _fetch_float_mc_from_tencent(stock_list)


def load_float_mc_cache():
    """加载流通市值缓存（转发 AppData，供 AppOrch 再导出 facade）"""
    return app_data.load_float_mc_cache()


def update_float_mc_cache(mv_dict):
    """合并并落盘流通市值缓存（转发 AppData，供 AppOrch 再导出 facade）"""
    return app_data.update_float_mc_cache(mv_dict)


def _debug_read_page_index_stocks(sector_code):
    """获取当前页面指数的成分股（网络抓取限时保护）

    P1-5：删除扫描中断标志（abort_check）传递——遗留中止链路
    （Scanner.abort / _scan_aborted）已随前端 task cancel 语义删除，
    成分抓取限时由 TdxAPI._run_with_timeout 兜底。
    """
    if not sector_code:
        return []
    return get_index_stocks(sector_code)


def read_tdxhy_l2_indices():
    """返回所有二级行业板块指数列表（X+4位代码对应的881yyy），共125个
    （委托 app_data.tdxhy_l2_indices）"""
    return app_data.tdxhy_l2_indices()


def read_tdxhy_l3_indices():
    """返回所有三级行业板块指数列表（X+6位代码对应的881yyy），共315个
    （委托 app_data.tdxhy_l3_indices）"""
    return app_data.tdxhy_l3_indices()



# ═══════════════════════════════════════════════════════════════════════
# 自选股 / 标注扫描
# ═══════════════════════════════════════════════════════════════════════

def get_annotated_codes(freq=""):
    """自选扫描：返回有标注的股票列表（/api/annotations_scan）"""
    from App.AppData import app_data
    return app_data.get_annotated_codes(freq)


def read_zxg_stocks():
    """读取自选股列表（/api/zxg_list）"""
    from App.AppData import app_data
    return app_data.read_zxg_stocks()


def zxg_save(codes):
    """保存勾选股票到通达信 + 同花顺自选股（/api/zxg_save）

    codes: 逗号分隔字符串
    返回 (result_dict, status_code)。
    """
    from App.AppData import app_data

    codes_list = codes.split(",") if codes else []
    if not codes_list:
        return {"error": "codes为空"}, 400

    try:
        codes_raw = [c.strip() for c in codes_list]
        codes_ths = list(dict.fromkeys(codes_raw))

        # 通达信
        log.info(f"[保存] 通达信: 输入 {len(codes_raw)} 只, 代码={codes_raw}")
        tdx_added = app_data.save_to_zxg_blk(codes_raw)
        log.info(f"[保存] 通达信: 实际写入 {tdx_added} 只")

        # 同花顺
        ths_added = 0
        ths_msg = ""
        log.info(f"[保存] 同花顺: 输入 {len(codes_ths)} 只, 代码={codes_ths}")
        if _m._THS_CLOUD_AVAILABLE:
            try:
                cloud_result = _m.save_scan_to_ths_cloud(codes_ths)
                if "error" in cloud_result:
                    raise Exception(cloud_result["error"])
                ths_added = len(cloud_result.get("added", []))
                ths_msg = "ok"
                log.info(f"[保存] 同花顺: 新增{ths_added}, "
                      f"跳过{len(cloud_result.get('skipped',[]))}, "
                      f"失败{len(cloud_result.get('failed',[]))}")
            except Exception as e:
                err_str = str(e)
                if "登录状态失效" in err_str or "Cookie" in err_str:
                    ths_msg = "Cookie过期，请运行 ths_capture_cookie.py 重新获取"
                else:
                    ths_msg = f"云端同步失败: {err_str}"
                log.info(f"[保存] 同花顺: {ths_msg}")
        else:
            ths_msg = "DataAPI/ThsCloudZxgAPI.py 未找到，请确保 DataAPI/ 目录完整"
            log.info(f"[保存] 同花顺: {ths_msg}")

        log.info(f"[保存] 汇总: 通达信={tdx_added}, 同花顺={ths_added}, msg={ths_msg}")
        return {
            "ok": True,
            "tdx_saved": tdx_added,
            "ths_saved": ths_added,
            "ths_msg": ths_msg,
        }, 200
    except Exception as exc:
        traceback.print_exc()
        return {"error": str(exc)}, 500


# ═══════════════════════════════════════════════════════════════════════
# 同花顺云端自选股
# ═══════════════════════════════════════════════════════════════════════

def save_scan_to_ths_cloud(codes):
    """保存扫描结果到同花顺云端自选股"""
    if not _m._THS_CLOUD_AVAILABLE or _m.save_scan_to_ths_cloud is None:
        return {"error": "DataAPI/ThsCloudZxgAPI.py 未找到，请确保该文件在 DataAPI/ 目录"}
    return _m.save_scan_to_ths_cloud(codes)


# ═══════════════════════════════════════════════════════════════════════
# 扫描：批量扫描服务（状态收敛到类内部）
# ═══════════════════════════════════════════════════════════════════════

class Scanner:
    """批量扫描服务：遍历代码列表、逐票调用 analyze_stock、汇总结果、追踪进度。

    对外通过本类方法访问，避免路由层直接操作模块级状态。
    """

    # ── 状态访问器（收敛到类内部）────────────────
    @property
    def skip_log(self):
        """跳过记录**快照**（调用方常在锁外遍历它，故返回副本）

        无 scan_token 时回退到最近一次会话（旧客户端兼容）。
        """
        return scan_skip_snapshot()

    @property
    def start_time(self):
        """最近一次扫描的开始时间（旧接口；多页场景请按 scan_token 取会话）"""
        sess = get_scan_session()
        return sess.start_time if sess is not None else None

    @property
    def page_index_code(self):
        """【已废弃·仅兼容】全局兜底值——新路径由请求参数传入，见 stock_list"""
        return _page_index_code

    @page_index_code.setter
    def page_index_code(self, value):
        global _page_index_code
        _page_index_code = value

    # ── 股票列表 ─────────────────────────────────────────────────────
    def stock_list(self, source="zxg", page_index_code=None, scan_token=None):
        """返回股票列表（支持逗号分隔多来源）

        审计 X3：page_index 来源的板块指数代码**由请求参数传入**，不再读
        进程级全局。原实现读全局 _page_index_code，多网页下 A 页选沪深300、
        B 页选中证500 时，两页都会按最后设置的那个扫——静默扫错一整批
        成分股，且不报错。

        取值优先级：显式参数 > 本扫描会话 > 全局兜底（旧客户端）。
        """
        sources = [s.strip() for s in source.split(",") if s.strip()]

        # 解析本次要用的板块指数代码（每页一份，不落全局）
        if page_index_code:
            _idx_code = page_index_code
        else:
            _sess = get_scan_session(scan_token)
            _idx_code = (_sess.page_index_code if _sess is not None else None) or _page_index_code

        # 归一化板块指数代码（与 set_page_index_code 共用 utils._get_stock_market_code
        # 单一事实源）：前端按标准契约传 market+裸码（sh000852），而成分取数契约
        # get_index_stocks 只认无前缀裸板块代码（881xxx/880xxx/399xxx/000xxx）。
        # 若这一层漏归一化，带前缀代码会被透传下去，命中不了 CSI_INDICES 精确匹配，
        # 落入中证 csindex 兜底拼出不存在的 "sh000852cons.xls"，返回非 Excel 内容，
        # 抛 "Excel file format cannot be determined"，成分为空、扫描池为 0。
        if _idx_code:
            from App import utils as _u
            _mkt, _bare = _u._get_stock_market_code(_idx_code)
            if _mkt:
                _idx_code = _bare

        _SOURCE_READERS = {
            "zxg": (read_zxg_stocks, "自选股"),
            "page_index": (lambda: _debug_read_page_index_stocks(_idx_code), "成分股"),
            "tdxhy2": (read_tdxhy_l2_indices, "板块指数2"),
            "tdxhy3": (read_tdxhy_l3_indices, "板块指数3"),
        }

        src_stocks = {}
        errors = []
        for src in sources:
            reader = _SOURCE_READERS.get(src)
            if reader is None:
                errors.append(f"未知来源: {src}")
                continue
            read_fn, _ = reader
            src_stocks[src] = read_fn()

        # 合并去重
        merged = []
        seen = {}
        for src in sources:
            stocks = src_stocks.get(src)
            if not stocks:
                continue
            for stk in stocks:
                key = (stk["code"], stk["prefix"])
                if key not in seen:
                    stk["_source"] = src
                    seen[key] = len(merged)
                    merged.append(stk)
                else:
                    exist_idx = seen[key]
                    exist_src = merged[exist_idx].get("_source", "")
                    if exist_src == "zxg" and src != "zxg":
                        merged[exist_idx]["_source"] = src

        # 批量获取流通市值
        _need_float_mc = any(s not in ("tdxhy2", "tdxhy3") for s in sources)
        if _need_float_mc:
            app_data.load_float_mc_cache()
            if app_data.float_mc_loaded:
                # 审计 P2：原为 `len(app_data.float_mc_cache)` —— 裸读共享容器。
                # 改走 app_data 加锁取数口，与写者（update_float_mc_cache）
                # 共用 _user_store_lock，也给后来人留一个正确的样板。
                log.info(f"[流通市值] 本地缓存已加载 {app_data.float_mc_count()} 只")
            try:
                t_mc = time.time()
                mv_dict = fetch_float_mc_from_tencent(merged)
                if mv_dict:
                    total_stocks = len(merged)
                    got_count = len(mv_dict)
                    miss_count = total_stocks - got_count
                    app_data.update_float_mc_cache(mv_dict)
                    if miss_count == 0:
                        log.info(f"[流通市值] 腾讯接口 获取全部 {got_count} 只 (耗时{time.time()-t_mc:.1f}s)")
                    else:
                        log.info(f"[流通市值] 腾讯接口 获取 {got_count}/{total_stocks} 只，{miss_count} 只未获取到 (耗时{time.time()-t_mc:.1f}s)")
                else:
                    log.info("[流通市值] 腾讯接口未返回数据，使用本地缓存")
                    if app_data.float_mc_cache_stale():
                        log.warning("[流通市值] 警告：腾讯接口未返回数据，且本地缓存已过期，"
                                    "本次流通市值判定可能使用旧数据，建议稍后重新刷新。")
            except Exception as e:
                log.info(f"[流通市值] 腾讯接口异常: {type(e).__name__}: {e}，使用本地缓存")
                if app_data.float_mc_cache_stale():
                    log.warning("[流通市值] 警告：腾讯接口异常，且本地缓存已过期，"
                                "本次流通市值判定可能使用旧数据，建议稍后重新刷新。")

        # 后端预过滤
        pre_filtered = merged
        pre_skip_count = 0
        pre_skip_log = []
        try:
            t_pre_all = time.time()
            _PFX_MAP = {"0": "sz", "1": "sh", "2": "bj"}
            filtered = []
            for stk in merged:
                src = stk.get("_source", "zxg")
                if src in ("zxg", "tdxhy2", "tdxhy3"):
                    filtered.append(stk)
                    continue
                code = stk.get("code", "")
                prefix = stk.get("prefix", "")
                market = _PFX_MAP.get(prefix, "")
                if not market or not code:
                    filtered.append(stk)
                    continue
                pass_ok, pre_mc, skip_reason = _quick_prefilter_pass(market, code)
                if not pass_ok:
                    pre_skip_count += 1
                    pre_skip_log.append(f"[预过滤] {code} 跳过 ({skip_reason})")
                else:
                    filtered.append(stk)
            pre_filtered = filtered
            elapsed = time.time() - t_pre_all
            if pre_skip_count > 0:
                log.info(f"[预过滤] 批量预过滤完成: 跳过 {pre_skip_count} 只，剩余 {len(pre_filtered)} 只 (耗时 {elapsed:.1f}s)")
                for line in pre_skip_log:
                    log.info(line)
            else:
                log.info(f"[预过滤] 批量预过滤完成: 全部通过 {len(pre_filtered)} 只 (耗时 {elapsed:.1f}s)")
        except Exception as e:
            log.info(f"[预过滤] 批量预过滤异常: {type(e).__name__}: {e}")

        return {
            "stocks": pre_filtered,
            "sources": sources,
            "total": len(pre_filtered),
            "pre_skipped": pre_skip_count,
            "errors": errors if errors else None,
        }

    # ── 单只扫描 ─────────────────────────────────────────────────────
    def scan_one(self, code, freq="d", prefix="", recent="1", source="zxg", mode=""):
        """扫描单只股票（唯一调用方：AppScanPool._worker_scan_one，worker 进程内）

        并发安全不依赖锁，而是三层隔离：
          · 进程边界  —— spawn worker，每进程独立 app_data 与分析缓存；
          · 数据注入  —— CChan 构建经 tdx_data_context 每请求线程局部注入；
          · 缓存操作  —— app_data.cache_* 各自持 stocks_cache_lock。
        （本模块顶部注释记录了为何不需要该锁）
        """
        t_scan_start = time.time()
        try:
            recent_days = max(1, int(recent))
        except ValueError:
            recent_days = 1

        if not code:
            return {"error": "缺少code参数"}

        try:
            t0 = time.time()
            _PREFIX_MAP = {"0": "sz", "1": "sh", "2": "bj", "hk": "hk"}
            market_prefix = _PREFIX_MAP.get(prefix, "")
            qualified_code = (market_prefix + code) if market_prefix else code
            market = market_prefix.lower() if market_prefix else ""

            # cache_chan=False：扫描模式不缓存 CChan 对象与 K 线 records
            # （内存大头），只留轻量 result；配合扫描完成即销毁进程池
            # （AppScanPool.destroy_pool），实现「即用即弃」、状态可恢复。
            result = _m.analyze_stock(qualified_code, freq=freq, cache_chan=False)

            t_analyze = time.time() - t0
            if "error" in result:
                append_scan_skip(f"{code} - {result['error']}")
                log.info(f"[耗时-扫描] {code} 分析失败: {result['error']}, 耗时{t_analyze:.3f}s")
                return {"error": result["error"]}

            t0 = time.time()
            bsps = result.get("bsps", [])
            stock_name = result.get("meta", {}).get("name", f"{code}")
            klines = result.get("klines", [])
            t_filter = 0

            # ── 底分型扫描模式 ──
            if mode == "fx_d":
                bis = result.get("bis", [])
                is_fx_d = False
                fx_strength = 0
                if bis:
                    last_bi = bis[-1]
                    if last_bi.get("is_sure", True) and last_bi.get("direction") == "down":
                        is_fx_d = True
                        fx_strength = last_bi.get("fx_strength", 0)
                t_filter = time.time() - t0
                if is_fx_d:
                    t_total = time.time() - t_scan_start
                    log.info(f"[耗时-扫描-底分型] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 是底分型")
                    return {
                        "code": market + code, "name": stock_name,
                        "is_fx_d": True,
                        "last_close": klines[-1]["close"] if klines else 0,
                        "freq": freq,
                        "fx_strength": fx_strength,
                    }
                else:
                    mkt, cd = _m._get_market_code(qualified_code)
                    if mkt and cd:
                        app_data.cache_remove(make_live_key(mkt, cd, freq))
                    t_total = time.time() - t_scan_start
                    log.info(f"[耗时-扫描-底分型] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 不是底分型")
                    return {"code": code, "is_fx_d": False}

            # ── 均线分类扫描模式 ──
            if mode == "ma":
                ma_periods = [5, 13, 21, 34, 55, 89, 144, 233]
                closes = [k.get("close", 0) for k in klines]
                last_close = closes[-1] if closes else 0
                ma_category = -1
                if last_close > 0 and len(closes) >= max(ma_periods):
                    conquered = 0
                    for p in ma_periods:
                        ma_val = sum(closes[-p:]) / p
                        if last_close >= ma_val:
                            conquered += 1
                    ma_category = 8 - conquered
                t_filter = time.time() - t0
                t_total = time.time() - t_scan_start
                log.info(f"[耗时-扫描-均线] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 分类:{ma_category}")
                resp_data = {
                    "code": market + code,
                    "name": stock_name,
                    "ma_category": ma_category,
                    "last_close": round(last_close, 2),
                    "freq": freq,
                }
                if ma_category > 3:
                    mkt, cd = _m._get_market_code(qualified_code)
                    if mkt and cd:
                        app_data.cache_remove(make_live_key(mkt, cd, freq))
                return resp_data

            # ── 放量扫描模式 ──
            if mode == "fangliang":
                # 逻辑（以日K为例）：最近 recent_days 根记为 N+1..N+N，
                # 取其中成交额最大者 A；再从 N 起往前数 120 根作比较窗口
                # （窗口根数可配：app_config.scan_fangliang_window_bars）。
                # 若 A 大于该窗口内任意一天的成交额（即 A > 窗口内最大
                # 成交额），则该标的放量命中。
                #
                # 边界处理：K线数据不足（不足 recent_days+窗口根数）或近期
                # 最大成交额 A 为 0 时，视为无效标的，直接静默跳过（不打印日志）。
                window_bars = int(app_config.scan_fangliang_window_bars or 120)
                if window_bars < 1:
                    window_bars = 120
                need_bars = recent_days + window_bars
                is_fangliang = False
                amount_a = 0
                peak_prev = 0
                a_is_rise = False
                if len(klines) >= need_bars:
                    recent_kl = klines[-recent_days:]
                    amount_a = max((k.get("amount", 0) for k in recent_kl), default=0)
                    if amount_a > 0:
                        prev_window = klines[-(need_bars):-recent_days]
                        peak_prev = max((k.get("amount", 0) for k in prev_window), default=0)
                        is_fangliang = amount_a > peak_prev
                        # 找出 A 所在的那根K线，记录其涨跌（收阳/收阴），
                        # 用以对齐扫描结果标签与K线图中该根"成交额柱"的颜色。
                        a_kline = next(
                            (k for k in recent_kl if (k.get("amount", 0) or 0) == amount_a),
                            None)
                        if a_kline is not None:
                            a_is_rise = a_kline.get("close", 0) > a_kline.get("open", 0)

                if not is_fangliang:
                    # 数据不足 / A为0 / 未放量：统一静默返回未命中，不打印日志
                    mkt, cd = _m._get_market_code(qualified_code)
                    if mkt and cd:
                        app_data.cache_remove(make_live_key(mkt, cd, freq))
                    return {"code": code, "is_fangliang": False}

                last_close = klines[-1]["close"] if klines else 0
                t_filter = time.time() - t0
                t_total = time.time() - t_scan_start
                log.info(f"[耗时-扫描-放量] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) "
                         f"A张={amount_a:g} 前窗峰值={peak_prev:g} 放量=是")
                return {
                    "code": market + code, "name": stock_name,
                    "is_fangliang": True,
                    "last_close": last_close,
                    "freq": freq,
                    "amount_a": amount_a,
                    "peak_prev": peak_prev,
                    "a_is_rise": a_is_rise,
                }

            # ── 买卖点扫描模式 ──
            recent_dates = set()
            for k in klines[-recent_days:]:
                recent_dates.add(k.get("date", ""))
            buy_points = []
            sell_points = []
            for bsp in bsps:
                if bsp.get("date", "") in recent_dates:
                    point = {
                        "type": bsp.get("type", ""),
                        "price": bsp.get("price", 0),
                        "date": bsp.get("date", ""),
                    }
                    if bsp.get("is_buy", False):
                        buy_points.append(point)
                    else:
                        sell_points.append(point)
            has_points = buy_points or sell_points

            below_ma120 = False
            ma120_val = 0
            closes = [k.get("close", 0) for k in klines]
            last_close = klines[-1]["close"] if klines else 0
            if last_close > 0 and len(closes) >= 120:
                ma120_val = round(sum(closes[-120:]) / 120, 2)
                below_ma120 = last_close < ma120_val

            t_filter = time.time() - t0

            if not buy_points:
                mkt, cd = _m._get_market_code(qualified_code)
                if mkt and cd:
                    app_data.cache_remove(make_live_key(mkt, cd, freq))

            if has_points:
                t_total = time.time() - t_scan_start
                log.info(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 有买卖点")
                return {
                    "code": market + code, "name": stock_name,
                    "buy_points": buy_points,
                    "sell_points": sell_points,
                    "last_close": klines[-1]["close"] if klines else 0,
                    "freq": freq,
                    "below_ma120": below_ma120,
                    "ma120_val": ma120_val,
                }
            else:
                t_total = time.time() - t_scan_start
                log.info(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 无买卖点")
                return {"code": code, "buy_points": [], "sell_points": []}

        except Exception as exc:
            append_scan_skip(f"{code} - 异常: {exc}")
            t_total = time.time() - t_scan_start
            log.info(f"[耗时-扫描] {code} 异常: {exc}, 总耗时{t_total:.3f}s")
            return {"error": str(exc)}

    # ── 扫描生命周期 ─────────────────────────────────────────────────
    def start(self, page_index_code=None):
        """新一轮扫描开始 → {"ok": True, "scan_token": ...}

        审计 X3：为本次扫描建一个**私有会话**，返回 scan_token。后续
        end(scan_token) / 扫描期间的跳过记录都归属到这个 token，
        两个页面同时扫描各用各的上下文，不会互相覆盖。
        """
        token, _sess = new_scan_session(page_index_code=page_index_code)
        try:
            _m._load_stock_names_from_cache_file()
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
        return {"ok": True, "scan_token": token}

    def end(self, scan_token=None):
        """扫描结束（按 scan_token 结算那一次扫描；无 token 回退最近一次）

        幂等：会话注销后重复 end() 不会重复弹通知（原实现靠把
        _scan_start_time 置 None 来防重，现由会话生命周期承担）。
        """
        # 先取会话**快照**，再注销——注销后 scan_skip_snapshot() 就取不到了
        sess = get_scan_session(scan_token)
        if sess is None:
            # 已结算过（重复 end）/ token 失效 —— 幂等返回，不再弹通知
            return {"count": 0, "already_ended": True}
        token = sess.token
        elapsed = sess.elapsed()
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        time_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"

        # 审计 P1-5 + X3：整段基于**同一次快照**（且只取本会话的），
        # 避免「判空 → 遍历 → 计数」之间被收割线程的追加 / 别页的清空插队。
        _skip_snapshot = scan_skip_snapshot(token)
        if _skip_snapshot:
            log.info("========== 扫描异常/失败股票明细 ==========")
            log.info(f"共 {len(_skip_snapshot)} 只:")
            for i, item in enumerate(_skip_snapshot, 1):
                log.info(f"  {i}. {item}")
            log.info("============================================\n")
        else:
            log.info(f"[扫描明细] 全部扫描成功（耗时 {time_str}），无异常股票")

        skip_count = len(_skip_snapshot)
        msg = f"耗时 {time_str}"
        if skip_count > 0:
            msg += f"，跳过 {skip_count} 只"
        _send_windows_notification("扫描完成", msg)

        drop_scan_session(token)
        return {"count": skip_count}

    def clear_cache(self):
        """关闭扫描面板：不再清空共享股票分析缓存。
        P0-3 收敛：批量扫描与单票分析共享同一分析缓存（LRU 上限 50），
        关闭面板时清空会误伤用户正在查看的图表缓存，故关闭不再清空
        （原下载完成回调已随「盘后下载」功能移除）。
        返回 cleared=0 保持前端兼容（前端仅 POST 不读响应）。"""
        return {"cleared": 0}

    # ── 批量扫描异步化（ProcessPool 先行）────────────────────
    # 薄封装：AppScan 保持纯业务、零并发框架依赖（模块级不 import
    # concurrent.futures），批量入口委托 App/AppScanPool（入口适配器）。
    # 双路径：交互单票仍走 scan_one（线程池），批量走 ProcessPool；
    # 共享结果经 SQLite AppScanStore 跨进程。

    def submit_batch_scan(self, stocks, freq="d", mode="", recent="1", source="zxg",
                          scan_token=None):
        """提交批量扫描 → {task_id, total}（薄封装，委托 AppScanPool）

        stocks: [{code, prefix, _source}, ...]（scan_stock_list 合并列表）。
        任务在 ProcessPool 异步执行，进度经 get_batch_scan_status 轮询。

        scan_token：本次扫描的会话标识（由 start() 返回）。池的收割线程
        只拿得到 task_id，故在此绑定 task_id → scan_token，让它能把跳过
        记录写回**发起它的那一次扫描**而非全局（审计 X3）。
        """
        from App.AppScanPool import submit_batch_scan as _submit
        result = _submit(stocks, freq=freq, mode=mode, recent=recent, source=source)
        task_id = result.get("task_id") if isinstance(result, dict) else None
        if task_id and scan_token:
            # 未传 token 的旧客户端无需绑定：append_scan_skip 会回退到
            # _legacy_scan_session，行为与改动前一致。
            bind_task_scan_token(task_id, scan_token)
        return result

    def get_batch_scan_status(self, task_id, since=0):
        """批量扫描状态轮询视图（薄封装，委托 AppScanStore，增量读取）

        返回 {task_id, status, total, completed, results, error}；
        results 为 seq >= since 的增量行；任务不存在返回 None。
        """
        from App.AppScanPool import get_status as _status
        return _status(task_id, since=since)

    def abort_batch_scan(self, task_id):
        """中止批量扫描（薄封装，委托 AppScanPool）"""
        from App.AppScanPool import abort as _abort
        return _abort(task_id)

    def set_page_index_code(self, code):
        """设置当前板块指数代码（前端传标准 market+code，归一为裸板块代码）

        前端提交 chartData.meta.symbol（标准 market(小写)+code，如 sh880491 /
        sz399001 / ds932000 / sh000300）。get_index_stocks 只认裸板块代码
        （881xxx/880xxx/399xxx/000xxx），故先剥离 market 前缀归一，避免带前缀
        代码失配 `.startswith("88"/"399")` 判定。
        """
        global _page_index_code
        from App import utils as _u
        code = code.strip()
        if code:
            mkt, bare = _u._get_stock_market_code(code)
            normalized = bare if mkt else code
            _page_index_code = normalized
            log.info(f"[成分股] 已设置板块指数代码: {normalized}")
            return {"ok": True, "code": normalized}
        else:
            return {"error": "缺少code参数"}


# 全局单例
scanner = Scanner()

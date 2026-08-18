# -*- coding: utf-8 -*-
"""
App/AppChart.py —— 代码加载 / 图表功能域
=========================================================================
按业务能力拆分（阶段 8 重设计）：页面左上角输入股票或期货代码、切换
周期、点击双窗口按钮、选择复盘日期等触发的一系列动作。

本模块收纳：
  - 分析漏斗（call_analysis / run_analysis / analyze_stock 等，持 _ENGINE_LOCK）
  - 手动选点 / 红框中枢（call_* 持锁漏斗 + RAW 原始入口）
  - 数据拉取与注入（fetch_and_inject / _get_data_source）
  - 搜索（search_stocks）
  - 选点 / 上次查看 / 期货子窗缓存漏斗
  - 期货元数据（futures_cleanup / get_futures_aliases / futures_config 等）
  - 代码解析（get_stock_market_code / get_market_code / get_stock_name）

依赖方向：AppChart.py → AppEngine / AppData / DataAPI（单向）
锁定义：_ENGINE_LOCK 为引擎调用全局串行锁，本模块 call_* 漏斗持锁；
LOCK_POLICY 登记表在 AppOrch.py（聚合入口）统一维护。
"""
import os
import json
import time
import threading
import traceback

# 分析引擎层（阶段 10.1：my_chan_main.py 职责被各层完全吸收，引擎迁入 App/AppEngine.py）
from App import AppEngine as _m


# 引擎调用全局串行锁（锁分类 SERIAL 共用；LOCK_POLICY 登记见 AppOrch.py）
_ENGINE_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════
# 消费侧：分析引擎
# ═══════════════════════════════════════════════════════════════════════

def call_analysis(code, freq="d", end_date=None, dual=False, step=None, sub_freq=None):
    """单标的缠论分析（同步入口，REST 路由当前直接调用）

    - 引擎全局缓存非线程安全 → 全局串行锁保护
    - 开始/完成即时打印：uvicorn 仅在请求完成后记日志，挂起时控制台零输出，
      此日志是排障第一现场（阶段 1 Hotfix 教训）。
    """
    print(f"[api] /api/stock 开始分析: code={code!r} freq={freq!r} "
          f"end_date={end_date!r} dual={dual}", flush=True)
    t0 = time.time()
    with _ENGINE_LOCK:
        result = _m.analyze_stock(code, freq=freq, end_date=end_date,
                                  dual=dual, step=step, sub_freq=sub_freq)
    print(f"[api] /api/stock 完成: code={code!r} 耗时 {time.time() - t0:.2f}s", flush=True)
    return result


async def run_analysis(code, freq="d", end_date=None, dual=False, step=None, sub_freq=None):
    """单标的缠论分析（异步入口，阶段 3a SSE/REST 统一走此通道）

    线程池执行 + 串行锁：不阻塞事件循环（静态资源/健康检查保持可响应），
    同时保证同一时刻只有一个线程进入引擎。
    """
    import asyncio
    loop = asyncio.get_event_loop()
    print(f"[api] run_analysis 开始: code={code!r} freq={freq!r} "
          f"end_date={end_date!r} dual={dual}", flush=True)
    t0 = time.time()

    def _job():
        with _ENGINE_LOCK:
            return _m.analyze_stock(code, freq=freq, end_date=end_date,
                                    dual=dual, step=step, sub_freq=sub_freq)

    try:
        return await loop.run_in_executor(None, _job)
    finally:
        print(f"[api] run_analysis 完成: code={code!r} 耗时 {time.time() - t0:.2f}s", flush=True)


def analyze_stock(code, freq="d", end_date=None, cache_chan=True, dual=False, step=None, sub_freq=None):
    """统一的缠论分析入口 · 锁分类 RAW（无锁）

    ⚠ 本函数是引擎原始入口的薄封装，**并非无状态**：引擎内部维护模块级
    LRU 缓存（_stocks_analysis_cache）与名称/PE/市值等共享缓存，均非线程
    安全。此前的「无状态，可在线程池 / ProcessPool 复用」表述有误导。

    调用约定（LOCK_POLICY，见 AppOrch.py 文件头）：
      - 串行分析路径（REST 交互式）→ 必须走 call_analysis / run_analysis
        （持 _ENGINE_LOCK），不得直调本函数；
      - 扫描路径（SCAN）→ AppScan.Scanner.scan_one 内部调用（全局
        _scan_lock 内串行引擎调用，锁外保留并发）；
      - SSE 期货路径（SELF_CONTAINED）→ 独立 CChan 会话，不触共享缓存。
    """
    return _m.analyze_stock(code, freq=freq, end_date=end_date,
                            cache_chan=cache_chan, dual=dual, step=step, sub_freq=sub_freq)


def call_manual_select_point(code, freq="d", bi_idx=-1):
    """股票手动选点 · SERIAL（持 _ENGINE_LOCK）

    原路由直连 m.stock_manual_select_point 绕锁（阶段 2 遗留问题 L185），
    阶段 3a 起统一走本漏斗：内部链路复用 analyze_stock 引擎与共享缓存。
    """
    with _ENGINE_LOCK:
        return _m.stock_manual_select_point(code, freq=freq, bi_idx=bi_idx)


def call_futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """期货手动选点 · SERIAL（持 _ENGINE_LOCK）

    内部走期货分析链路（_analyze_futures_internal，含期货缓存读写），
    归入串行分类，与股票侧共用引擎锁。
    """
    with _ENGINE_LOCK:
        return _m.futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)


def call_compute_red_range_zs(code, sub_freq="d", left_date="", right_date="", end_date=None):
    """红框中枢计算 · SERIAL（持 _ENGINE_LOCK）

    原路由直连 m.compute_red_range_zs 绕锁（阶段 2 遗留问题 L206），
    阶段 3a 起统一走本漏斗：内部复用 analyze_stock 引擎与共享缓存。
    """
    with _ENGINE_LOCK:
        return _m.compute_red_range_zs(code, sub_freq=sub_freq,
                                       left_date=left_date, right_date=right_date,
                                       end_date=end_date)


def stock_manual_select_point(code, freq="d", bi_idx=-1):
    """股票手动选点 · RAW（无锁原始入口）

    ⚠ 与 analyze_stock 同理并非无状态：内部走 analyze_stock 引擎链路与
    共享缓存。REST 调用方必须走 call_manual_select_point（持锁漏斗）；
    本签名保留供已按 SELF_CONTAINED 分类并自带会话隔离的路径使用。
    """
    return _m.stock_manual_select_point(code, freq=freq, bi_idx=bi_idx)


def futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """期货手动选点 · RAW（无锁原始入口）

    ⚠ 内部读写期货共享缓存，非线程安全。REST 调用方必须走
    call_futures_manual_select_point（持锁漏斗）。
    """
    return _m.futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)


def compute_red_range_zs(code, sub_freq="d", left_date="", right_date="", end_date=None):
    """红框中枢计算 · RAW（无锁原始入口）

    ⚠ 内部复用 analyze_stock 引擎与共享缓存。REST 调用方必须走
    call_compute_red_range_zs（持锁漏斗）。
    """
    return _m.compute_red_range_zs(code, sub_freq=sub_freq,
                                   left_date=left_date, right_date=right_date,
                                   end_date=end_date)


def extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None):
    """从实时行情快照中提取分析所需字段（供 SSE 路径使用）"""
    return _m._extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                         saved_selection_date, lightweight, klines)


# ═══════════════════════════════════════════════════════════════════════
# 获取侧：数据拉取与注入（阶段 5：统一走 DataAPI 抽象层）
# ═══════════════════════════════════════════════════════════════════════

def _get_data_source(code, source="tdx"):
    """阶段 5：根据 code 类型和 source 参数选择数据源，返回 DataAPI 类引用。

    数据源选择规则：
      - tdx: 通达信本地数据（股票/指数/板块），使用 DataAPI.TdxAPI
      - tqsdk: 天勤期货数据（期货/期指），使用 DataAPI.TqSdkAPI
      - 自动检测: code 包含期货特征时自动选择 tqsdk

    返回 (api_module, is_futures) 元组。
    """
    from DataAPI.TqSdkAPI import _get_futures_code

    if source == "tqsdk" or _get_futures_code(code):
        from DataAPI import TqSdkAPI
        return TqSdkAPI, True

    from DataAPI import TdxAPI
    return TdxAPI, False


def fetch_and_inject(code, freq="d", source="tdx", end_date=None, dual=False, step=None, sub_freq=None):
    """
    阶段 5：判断股票 / 期货 → 拉取 K 线 → 注入分析引擎。

    fetch 统一走 DataAPI 抽象层（替换阶段 4 前的直连模式）：
      - 数据源选择经 _get_data_source() 路由到对应 DataAPI 实现
      - 实际拉取仍委托 analyze_stock（其内部已通过 DataAPI 读取数据）
      - source 参数显式选择数据源（tdx / tqsdk），缺省自动检测

    锁分类 RAW（无锁）：委托 analyze_stock，共享引擎缓存，非线程安全。
    串行调用方须走 call_analysis / run_analysis。
    """
    api_module, is_futures = _get_data_source(code, source)
    return _m.analyze_stock(code, freq=freq, end_date=end_date,
                            dual=dual, step=step, sub_freq=sub_freq)


# ═══════════════════════════════════════════════════════════════════════
# 搜索
# ═══════════════════════════════════════════════════════════════════════

def search_stocks(q):
    """股票代码 / 名称 / 拼音搜索（委托 my_chan_main 的缓存与别名）"""
    from App.AppData import app_data
    _m._load_stock_names_from_cache_file()
    if not os.path.exists(app_data.stock_names_cache_file):
        return {"need_refresh": True, "msg": "请先刷新股票名缓存"}

    keyword_upper = q.upper()
    exact_results = []
    exact_pinyin_results = []
    prefix_results = []
    other_results = []

    # 手工补充扩展市场指数
    manual_items = [
        {"code": "932000", "name": "中证2000", "pinyin": "ZZ2", "market": "ds", "type": "指数"},
    ]
    for item in manual_items:
        bare_code = item["code"]
        name = item["name"]
        pinyin = item.get("pinyin", "")
        if not (keyword_upper in bare_code or keyword_upper in name.upper() or keyword_upper in pinyin):
            continue
        if bare_code == keyword_upper:
            exact_results.append(item)
        elif pinyin == keyword_upper or name.upper() == keyword_upper:
            exact_pinyin_results.append(item)
        elif bare_code.startswith(keyword_upper):
            prefix_results.append(item)
        else:
            other_results.append(item)

    for compound_key, info in _m._stock_names_cache.items():
        if isinstance(info, dict):
            name = info.get("name", "")
            pinyin = info.get("pinyin", "")
            market = info.get("market", "")
        else:
            name = info
            pinyin = ""
            market = ""
        name = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in name)
        pinyin = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in pinyin)
        if not name:
            continue

        if market and compound_key.startswith(market):
            bare_code = compound_key[len(market):]
        else:
            bare_code = compound_key

        if not (keyword_upper in bare_code or keyword_upper in name.upper() or keyword_upper in pinyin):
            continue

        if not market:
            if len(bare_code) == 5 and bare_code.isdigit():
                market = "hk"
            elif bare_code.startswith("6") or bare_code.startswith("5") or bare_code.startswith("9"):
                market = "sh"
            elif bare_code.startswith("0") or bare_code.startswith("3") or bare_code.startswith("2"):
                market = "sz"
            elif bare_code.startswith("88") or bare_code.startswith("99"):
                market = "sh"
            else:
                market = "sz"

        item = {"code": bare_code, "name": name, "pinyin": pinyin, "market": market, "type": ""}
        if bare_code == keyword_upper:
            exact_results.append(item)
        elif pinyin == keyword_upper or name.upper() == keyword_upper:
            exact_pinyin_results.append(item)
        elif bare_code.startswith(keyword_upper):
            prefix_results.append(item)
        else:
            other_results.append(item)

    results = exact_results[:10] + exact_pinyin_results[:10] + prefix_results[:10] + other_results[:10]
    results = results[:10]

    # 期货/期指别名搜索（阶段 5：经 CTqSdkAPI 元数据接口）
    from DataAPI.TqSdkAPI import CTqSdkAPI
    for alias, full_code in CTqSdkAPI.FUTURES_ALIASES.items():
        if keyword_upper in alias.upper():
            name = _m._get_futures_name(full_code) if _m._get_futures_name else alias
            if not any(r["code"] == full_code for r in results):
                results.append({
                    "code": full_code, "name": name, "pinyin": alias,
                    "market": "futures", "type": "",
                })

    return {"results": results}


# ═══════════════════════════════════════════════════════════════════════
# 选点 / 上次查看 / 期货子窗缓存漏斗
# ═══════════════════════════════════════════════════════════════════════

def clear_saved_point(code, freq="d"):
    """清除选点并同步清缓存（/api/clear_saved_point）"""
    from App.AppData import app_data
    return app_data.clear_saved_point(code, freq)


def futures_clear_saved_point(symbol, freq="15s"):
    """期货清除选点（/api/futures_clear_saved_point）：别名解析 + 清 CSV"""
    from App.AppData import app_data

    symbol_upper = symbol.upper()
    from DataAPI.TqSdkAPI import CTqSdkAPI
    if symbol_upper in CTqSdkAPI.FUTURES_ALIASES:
        symbol = CTqSdkAPI.FUTURES_ALIASES[symbol_upper]
    app_data.clear_saved_point_time(symbol, freq)
    return {"ok": True}


def save_last_code_freq(code, freq="d"):
    """持久化上次查看代码/周期（/api/stock 成功后的副作用）"""
    from App.AppData import app_data
    return app_data.save_last_code_freq(code, freq)


def load_last_code_freq():
    """加载上次查看代码/周期（启动恢复）"""
    from App.AppData import app_data
    return app_data.load_last_code_freq()


def get_saved_point_times():
    """选点内存表（阶段 4：FrontAPI 经此只读访问，禁直连 my_chan_main 状态）"""
    from App.AppData import app_data
    return app_data.saved_point_times


def futures_cache_get(key):
    """期货分析缓存读（阶段 4 漏斗）"""
    from App.AppData import app_data
    return app_data.futures_cache_get(key)


def futures_cache_put(key, value):
    """期货分析缓存写（阶段 4 漏斗；SSE 双窗口下窗 chan 入缓存）"""
    from App.AppData import app_data
    return app_data.futures_cache_put(key, value)


def futures_cache_pop(key, default=None):
    """期货分析缓存失效（阶段 4 漏斗；连接关闭时释放）"""
    from App.AppData import app_data
    return app_data.futures_cache_pop(key, default)


def futures_set_sub_chan(symbol, sub_freq, chan):
    """写期货子窗 CChan（阶段 4 吸收评审：语义化漏斗，key 规则内聚数据层）"""
    from App.AppData import app_data
    return app_data.set_futures_sub_chan(symbol, sub_freq, chan)


def futures_get_sub_chan(symbol, sub_freq):
    """读期货子窗 CChan（语义化漏斗；symbol 大小写不敏感）"""
    from App.AppData import app_data
    return app_data.get_futures_sub_chan(symbol, sub_freq)


def futures_pop_sub_chan(symbol, sub_freq):
    """失效期货子窗 CChan（语义化漏斗；连接关闭时释放）"""
    from App.AppData import app_data
    return app_data.pop_futures_sub_chan(symbol, sub_freq)


def get_saved_point(code, freq):
    """查询单个选点：返回该 (code, freq) 已保存的选点时间或空串（阶段 4）"""
    from App.AppData import app_data
    col = app_data.freq_to_col(freq)
    if not col:
        return ""
    return app_data.saved_point_times.get(code, {}).get(col, "").strip()


# ═══════════════════════════════════════════════════════════════════════
# 期货
# ═══════════════════════════════════════════════════════════════════════

def futures_cleanup():
    """清理所有期货数据"""
    return _m._cleanup_all_futures_data()


def get_futures_aliases():
    """期货别名映射（阶段 5：经 CTqSdkAPI 元数据接口）"""
    from DataAPI.TqSdkAPI import CTqSdkAPI
    return CTqSdkAPI.FUTURES_ALIASES


def get_futures_name(full_code):
    """期货名称"""
    if _m._get_futures_name:
        return _m._get_futures_name(full_code)
    return full_code


def tq_available():
    """天勤数据源是否可用"""
    return _m.TQ_AVAILABLE


def futures_config():
    """期货可用周期列表（阶段 5：经 CTqSdkAPI 元数据接口）"""
    try:
        from DataAPI.TqSdkAPI import CTqSdkAPI
        return {"supported_freqs": CTqSdkAPI.SUPPORTED_FREQS,
                "disabled_freqs": CTqSdkAPI.DISABLED_FREQS}
    except ImportError:
        return {"supported_freqs": [], "disabled_freqs": []}


def get_stock_names_cache_file():
    """股票名称缓存文件路径"""
    from App.AppData import app_data
    return app_data.stock_names_cache_file


# ═══════════════════════════════════════════════════════════════════════
# 辅助：代码解析
# ═══════════════════════════════════════════════════════════════════════

def get_stock_market_code(code):
    """解析股票代码 → (market, bare_code)"""
    return _m._get_stock_market_code(code)


def get_market_code(code):
    """解析市场代码"""
    return _m._get_market_code(code)


def get_stock_name(market, code):
    """获取股票名称"""
    return _m._get_stock_name(market, code)

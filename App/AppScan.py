# -*- coding: utf-8 -*-
"""
App/AppScan.py —— 股票扫描功能域
=========================================================================
点击页面右上角「股票扫描」按钮后的操作。

本模块收纳：
  - Scanner 类（批量扫描服务：遍历代码列表、逐票调用 analyze_stock、
    汇总结果、追踪进度）+ 全局单例 scanner
  - 自选股读写（read_zxg_stocks / zxg_save / get_annotated_codes）
  - 同花顺云端自选股（save_scan_to_ths_cloud / ths_cloud_available）
  - 扫描预过滤 + 行业索引读取 + 扫描态（_quick_prefilter_pass /
      read_tdxhy_l2_indices / read_tdxhy_l3_indices / _scan_lock 等）
  - Windows 扫描完成通知（_send_windows_notification）

依赖方向：AppScan.py → AppEngine / AppData / AppRefresh / AppScanPool（单向）
批量扫描提交/状态/中止委托 AppScanPool（入口适配器），共享结果经 SQLite
AppScanStore 跨进程；本模块保持纯业务、零并发框架依赖。
"""
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

# 刷新功能域（Scanner.stock_list 批量获取流通市值复用其漏斗）
from App.AppRefresh import load_float_mc_cache, fetch_float_mc_from_tencent, update_float_mc_cache
from App.AppLog import get_logger
log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 扫描态 + 预过滤 / 行业索引读取
# ═══════════════════════════════════════════════════════════════════════

# 股票名称缓存：别名 = app_data 实例字段（共享同一对象）
_stock_names_cache = app_data.names_cache

# 扫描跳过记录（收集后统一打印）
_scan_skip_log = []

# 扫描锁（防止并发扫描导致内存峰值翻倍）
_scan_lock = threading.Lock()

# 扫描终止标志：前端点击中断时设True，后端检查后跳过后续请求
_scan_aborted = False

# 扫描开始时间：用于计算扫描耗时，在通知中显示
_scan_start_time = None

# 页面指数代码：当前页面正在查看的通达信板块指数代码（如 880491），用于"成分股"扫描来源
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


def _debug_read_page_index_stocks(sector_code):
    """获取当前页面指数的成分股"""
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

def ths_cloud_available():
    """同花顺云端 API 是否可用"""
    return _m._THS_CLOUD_AVAILABLE


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
    def aborted(self):
        return _scan_aborted

    @aborted.setter
    def aborted(self, value):
        global _scan_aborted
        _scan_aborted = value

    @property
    def skip_log(self):
        return _scan_skip_log

    @property
    def start_time(self):
        return _scan_start_time

    @start_time.setter
    def start_time(self, value):
        global _scan_start_time
        _scan_start_time = value

    @property
    def page_index_code(self):
        return _page_index_code

    @page_index_code.setter
    def page_index_code(self, value):
        global _page_index_code
        _page_index_code = value

    @property
    def lock(self):
        return _scan_lock

    # ── 股票列表 ─────────────────────────────────────────────────────
    def stock_list(self, source="zxg"):
        """返回股票列表（支持逗号分隔多来源）"""
        sources = [s.strip() for s in source.split(",") if s.strip()]

        _SOURCE_READERS = {
            "zxg": (read_zxg_stocks, "自选股"),
            "page_index": (lambda: _debug_read_page_index_stocks(_page_index_code), "成分股"),
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
            from App.AppData import app_data
            load_float_mc_cache()
            if app_data.float_mc_loaded:
                log.info(f"[流通市值] 本地缓存已加载 {len(app_data.float_mc_cache)} 只")
            try:
                t_mc = time.time()
                mv_dict = fetch_float_mc_from_tencent(merged)
                if mv_dict:
                    total_stocks = len(merged)
                    got_count = len(mv_dict)
                    miss_count = total_stocks - got_count
                    update_float_mc_cache(mv_dict)
                    if miss_count == 0:
                        log.info(f"[流通市值] 腾讯接口 获取全部 {got_count} 只 (耗时{time.time()-t_mc:.1f}s)")
                    else:
                        log.info(f"[流通市值] 腾讯接口 获取 {got_count}/{total_stocks} 只，{miss_count} 只未获取到 (耗时{time.time()-t_mc:.1f}s)")
                else:
                    log.info("[流通市值] 腾讯接口未返回数据，使用本地缓存")
            except Exception as e:
                log.info(f"[流通市值] 腾讯接口异常: {type(e).__name__}: {e}，使用本地缓存")

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
        """扫描单只股票 · 锁分类 SCAN

        锁语义：引擎调用 analyze_stock 在全局 _scan_lock（单实例、非按票）
        内串行执行——保护非线程安全的引擎缓存不被并发写；锁外的预处理/
        结果过滤保留并发，故同步路径扫描吞吐主要依赖非引擎阶段的并行。
        前端批量扫描走 ProcessPool（SCAN_ASYNC，worker 数自动适配 CPU），
        本径为兼容保留。
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
            _PREFIX_MAP = {"0": "SZ", "1": "SH", "2": "BJ", "hk": "HK"}
            market_prefix = _PREFIX_MAP.get(prefix, "")
            qualified_code = (market_prefix + code) if market_prefix else code
            market = market_prefix.lower() if market_prefix else ""

            if _scan_aborted:
                return {"error": "扫描已终止", "aborted": True}

            with _scan_lock:
                if _scan_aborted:
                    return {"error": "扫描已终止", "aborted": True}
                # cache_chan=False：扫描模式不缓存 CChan 对象与 K 线 records
                # （内存大头），只留轻量 result；配合扫描完成即销毁进程池
                # （AppScanPool.destroy_pool），实现「即用即弃」、状态可恢复。
                result = _m.analyze_stock(qualified_code, freq=freq, cache_chan=False)

            t_analyze = time.time() - t0
            if "error" in result:
                _scan_skip_log.append(f"{code} - {result['error']}")
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
                        "code": code + "." + market.upper(), "name": stock_name,
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
                    "code": code + "." + market.upper(),
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
                    "code": code + "." + market.upper(), "name": stock_name,
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
                    "code": code + "." + market.upper(), "name": stock_name,
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
            _scan_skip_log.append(f"{code} - 异常: {exc}")
            t_total = time.time() - t_scan_start
            log.info(f"[耗时-扫描] {code} 异常: {exc}, 总耗时{t_total:.3f}s")
            return {"error": str(exc)}

    # ── 扫描生命周期 ─────────────────────────────────────────────────
    def start(self):
        """新一轮扫描开始"""
        global _scan_aborted, _scan_start_time
        _scan_aborted = False
        _scan_skip_log.clear()
        _scan_start_time = time.time()
        try:
            _m._load_stock_names_from_cache_file()
        except Exception as e:
            log.warning(f"[警告] 异常: {type(e).__name__}: {e}")
        return {"ok": True}

    def end(self):
        """扫描结束"""
        global _scan_start_time
        # 耗时统一计算：控制台明细 + 右下角弹窗共用同一口径
        elapsed = 0.0
        if _scan_start_time is not None:
            elapsed = time.time() - _scan_start_time
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        time_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"

        if _scan_aborted:
            # 用户点击中止后结束：不打印"全部扫描成功"误导日志
            log.info(f"\n[扫描明细] 扫描已中断（耗时 {time_str}）\n")
        elif _scan_skip_log:
            log.info("\n========== 扫描异常/失败股票明细 ==========")
            log.info(f"共 {len(_scan_skip_log)} 只:")
            for i, item in enumerate(_scan_skip_log, 1):
                log.info(f"  {i}. {item}")
            log.info("============================================\n")
        else:
            log.info(f"\n[扫描明细] 全部扫描成功（耗时 {time_str}），无异常股票\n")

        if _scan_start_time is not None:
            skip_count = len(_scan_skip_log)
            msg = f"耗时 {time_str}"
            if skip_count > 0:
                msg += f"，跳过 {skip_count} 只"
            _send_windows_notification("扫描完成", msg)
            _scan_start_time = None
        return {"count": len(_scan_skip_log)}

    def abort(self):
        """中断扫描（同步中止所有进行中的批量任务）

        ProcessPool worker 是独立进程，看不到主进程 _scan_aborted 标志；
        必须同步把 AppScanStore 中所有 pending/running 任务置为 aborted，
        worker 每票前检查 is_aborted 才会真正停止。
        """
        global _scan_aborted
        _scan_aborted = True
        log.info("[扫描] 收到中断请求，设置终止标志")
        try:
            from App.AppScanStore import get_scan_store
            aborted = get_scan_store().abort_all_running()
            if aborted:
                log.info(f"[扫描] 已中止 {aborted} 个进行中的批量任务")
        except Exception as exc:  # noqa: BLE001 —— 兜底不阻断中止
            log.info(f"[扫描] 中止批量任务异常: {type(exc).__name__}: {exc}")
        return {"ok": True}

    def clear_cache(self):
        """关闭扫描面板"""
        log.info("[扫描缓存] 面板关闭，缓存由 LRU 自然淘汰")
        return {"cleared": 0}

    # ── 批量扫描异步化（ProcessPool 先行）────────────────────
    # 薄封装：AppScan 保持纯业务、零并发框架依赖（模块级不 import
    # concurrent.futures），批量入口委托 App/AppScanPool（入口适配器）。
    # 双路径：交互单票仍走 scan_one（线程池），批量走 ProcessPool；
    # 共享结果经 SQLite AppScanStore 跨进程。

    def submit_batch_scan(self, stocks, freq="d", mode="", recent="1", source="zxg"):
        """提交批量扫描 → {task_id, total}（薄封装，委托 AppScanPool）

        stocks: [{code, prefix, _source}, ...]（scan_stock_list 合并列表）。
        任务在 ProcessPool 异步执行，进度经 get_batch_scan_status 轮询。
        """
        from App.AppScanPool import submit_batch_scan as _submit
        return _submit(stocks, freq=freq, mode=mode, recent=recent, source=source)

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
        """设置当前板块指数代码"""
        global _page_index_code
        code = code.strip()
        if code:
            if "." in code:
                code = code.split(".")[0]
            _page_index_code = code
            log.info(f"[成分股] 已设置板块指数代码: {code}")
            return {"ok": True, "code": code}
        else:
            return {"error": "缺少code参数"}


# 全局单例
scanner = Scanner()

"""
缠论分析 FastAPI 后端 — 替代 my_chan_main.py 中的 http.server 部分
=========================================================================
启动方式:  uvicorn api_server:app --host 127.0.0.1 --port 18081
API 文档:  http://127.0.0.1:18081/docs

与现有代码的关系：
  - 零侵入：my_chan_main.py 不做任何修改，仅作为模块被 import
  - 所有分析函数（analyze_stock / stock_manual_select_point / …）直接复用
  - 全局变量通过 my_chan_main.xxx 访问
  - SSE 实时推送通过 mock adapter 桥接原有 handler 逻辑

阶段 2（V10 方案 8.3）：
  - 路由收敛为 APIRouter，由 FrontAPI.py（新入口）聚合挂载；本文件仍可独立运行
  - 端口 / CHAN_PATH 等基础设施配置统一走 App/AppConfig.py（环境变量 / .env）
  - /api/stock 经 App/AppOrch.py 业务编排层调用（引擎串行锁 + 计时日志）
"""

import sys, os, json, time, threading, queue, traceback, re, gc

# ── 确保仓库根目录可导入（App/ 包与 my_chan_main.py 所在目录）────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Python 3.10 兼容垫片（原项目使用 typing.Self，需 3.11+；3.11+ 下自动跳过）
import typing
if not hasattr(typing, "Self"):
    try:
        from typing_extensions import Self
        typing.Self = Self
    except ImportError:
        pass

# ── 基础设施配置（阶段 2：环境变量 / .env 优先，见 App/AppConfig.py）──
from App.AppConfig import app_config

CHAN_PATH = app_config.chan_path
if CHAN_PATH not in sys.path:
    sys.path.insert(0, CHAN_PATH)

# ── 导入 my_chan_main（这会触发其模块级初始化，包括 TdxAPI 配置等）──
try:
    import my_chan_main as m
except ImportError as e:
    print(f"[错误] 无法导入 my_chan_main.py: {e}")
    print(f"[提示] 请确保 my_chan_main.py 在 CHAN_PATH ({CHAN_PATH}) 下")
    sys.exit(1)

# ── FastAPI ───────────────────────────────────────────────────────────
from fastapi import FastAPI, Query, HTTPException, Body, APIRouter, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from App import AppOrch as orch   # 阶段 2：业务编排层（引擎串行锁 + 统一计时日志）
from App.AppOrch import AppError  # 领域异常基类（评审 A：路由层不再吞掉，统一走异常处理器）

app = FastAPI(title="缠论分析 API", version="1.0.0", docs_url="/docs")


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """领域异常统一处理（与 FrontAPI 一致；api_server 独立入口同样可用）"""
    print(f"[api_server] 领域异常 {exc.__class__.__name__}: {exc} ({request.url.path})", flush=True)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.__class__.__name__, "detail": str(exc)},
    )

# 阶段 2：路由收敛为 APIRouter —— 由 FrontAPI.py（新入口）聚合挂载；
# 本文件保留 app 兼容组装，`python api_server.py` / `uvicorn api_server:app` 仍可独立运行。
# 阶段 3a 将把 REST 端点逐步迁入 FrontAPI.py，届时本文件退役为纯路由模块。
router = APIRouter()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════
# SSE Mock 适配器 —— 将 ChartHandler 的 SSE 方法桥接到 StreamingResponse
# ═══════════════════════════════════════════════════════════════════════

class _SSEMockWfile:
    """模拟 socket.SocketIO，将 write() 转为 enqueue"""
    def __init__(self):
        self.q = queue.Queue()

    def write(self, data: bytes):
        self.q.put(data)

    def flush(self):
        pass


class _SSEMockHandler:
    """模拟 ChartHandler，提供 SSE handler 所需的最小接口"""
    def __init__(self):
        self.wfile = _SSEMockWfile()

    def send_response(self, code: int):
        pass

    def send_header(self, keyword: str, value: str):
        pass

    def end_headers(self):
        pass

    def send_json_response(self, data, status_code: int):
        err = json.dumps(data, ensure_ascii=False, allow_nan=False)
        self.wfile.write(f"event: error\ndata: {err}\n\n".encode("utf-8"))


def _sse_generator(handler_method, *args):
    """在独立线程中运行 ChartHandler 的 SSE 方法，yield 其输出"""
    mock = _SSEMockHandler()

    def _run():
        try:
            handler_method(mock, *args)
        except Exception as exc:
            traceback.print_exc()
            err = json.dumps({"error": str(exc)}, ensure_ascii=False)
            mock.wfile.q.put(f"event: error\ndata: {err}\n\n".encode("utf-8"))
        finally:
            mock.wfile.q.put(None)          # 哨兵

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    while True:
        data = mock.wfile.q.get()
        if data is None:
            break
        yield data


# ═══════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════

def _json_response(data, status_code: int = 200):
    """统一 JSON 返回（兼容原有 send_json_response 行为）"""
    return JSONResponse(
        content=data,
        status_code=status_code,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# 路由 — 核心数据
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/stock")
async def api_stock(
    code: str = Query(...),
    freq: str = Query("d"),
    end_date: str = Query(None),
    step: str = Query(None),
    dual: bool = Query(False),
    sub_freq: str = Query(None),
):
    """获取股票缠论分析数据"""
    if not code:
        raise HTTPException(status_code=400, detail="请输入股票代码")
    try:
        # 阶段 2：经业务编排层调用（引擎串行锁 + 开始/完成计时日志）
        result = orch.call_analysis(code, freq=freq, end_date=end_date,
                                    dual=dual, step=step, sub_freq=sub_freq)
    except AppError:
        raise  # 领域异常：交给 FastAPI 统一异常处理器（评审 A，FrontAPI/api_server 均已注册）
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")

    if "error" in result:
        return _json_response(result, 400)

    # 持久化（与原有逻辑一致：非复盘、非双窗口下窗、非期货）
    if not end_date and result.get("meta", {}).get("market") != "futures":
        m._save_last_code_freq(code, freq)

    return _json_response(result)


@router.get("/api/stocks_manual_select_point")
async def api_stocks_manual_select_point(
    code: str = Query(...),
    freq: str = Query("d"),
    bi_idx: str = Query("-1"),
):
    """股票手动选点"""
    if not code or bi_idx == "-1":
        raise HTTPException(status_code=400, detail="缺少必要参数 code 或 bi_idx")
    try:
        result = m.stock_manual_select_point(code, freq=freq, bi_idx=bi_idx)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


@router.get("/api/red_range_zs")
async def api_red_range_zs(
    code: str = Query(...),
    freq: str = Query("d"),
    left_date: str = Query(...),
    right_date: str = Query(...),
    end_date: str = Query(None),
):
    """红框中枢计算"""
    if not code or not left_date or not right_date:
        raise HTTPException(status_code=400, detail="参数错误: code/left_date/right_date 不能为空")
    try:
        result = m.compute_red_range_zs(code, sub_freq=freq,
                                         left_date=left_date, right_date=right_date,
                                         end_date=end_date)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


# ═══════════════════════════════════════════════════════════════════════
# 路由 — 搜索
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/search")
async def api_search(q: str = Query(...)):
    """股票代码/名称/拼音搜索"""
    if not q:
        raise HTTPException(status_code=400, detail="请输入搜索关键词")

    m._load_stock_names_from_cache_file()
    if not os.path.exists(m._STOCK_NAMES_CACHE_FILE):
        return _json_response({"need_refresh": True, "msg": "请先刷新股票名缓存"})

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

    for compound_key, info in m._stock_names_cache.items():
        if isinstance(info, dict):
            name = info.get("name", "")
            pinyin = info.get("pinyin", "")
            market = info.get("market", "")
        else:
            name = info
            pinyin = ""
            market = ""
        # 全角 → 半角
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

    # 期货/期指别名搜索
    for alias, full_code in m.FUTURES_ALIASES.items():
        if keyword_upper in alias.upper():
            name = m._get_futures_name(full_code) if m._get_futures_name else alias
            if not any(r["code"] == full_code for r in results):
                results.append({
                    "code": full_code, "name": name, "pinyin": alias,
                    "market": "futures", "type": "",
                })

    return _json_response({"results": results})


# ═══════════════════════════════════════════════════════════════════════
# 路由 — 扫描
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/zxg_list")
async def api_zxg_list():
    """返回自选股列表"""
    try:
        stocks = m.read_zxg_stocks()
        return _json_response({"stocks": stocks})
    except Exception as exc:
        return _json_response({"error": str(exc)}, 500)


@router.get("/api/scan_stock_list")
async def api_scan_stock_list(source: str = Query("zxg")):
    """返回股票列表（支持逗号分隔多来源）"""
    sources = [s.strip() for s in source.split(",") if s.strip()]

    _SOURCE_READERS = {
        "zxg": (m.read_zxg_stocks, "自选股"),
        "page_index": (lambda: m._debug_read_page_index_stocks(m._page_index_code), "成分股"),
        "tdxhy2": (m.read_tdxhy_l2_indices, "板块指数2"),
        "tdxhy3": (m.read_tdxhy_l3_indices, "板块指数3"),
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
        m._load_float_mc_cache()
        if m._float_mc_loaded:
            print(f"[流通市值] 本地缓存已加载 {len(m._float_mc_cache)} 只")
        try:
            t_mc = time.time()
            mv_dict = m._fetch_float_mc_from_tencent(merged)
            total_stocks = len(merged)
            got_count = len(mv_dict)
            miss_count = total_stocks - got_count
            if mv_dict:
                m._update_float_mc_cache(mv_dict)
                if miss_count == 0:
                    print(f"[流通市值] 腾讯接口 获取全部 {got_count} 只 (耗时{time.time()-t_mc:.1f}s)")
                else:
                    print(f"[流通市值] 腾讯接口 获取 {got_count}/{total_stocks} 只，{miss_count} 只未获取到 (耗时{time.time()-t_mc:.1f}s)")
            else:
                print("[流通市值] 腾讯接口未返回数据，使用本地缓存")
        except Exception as e:
            print(f"[流通市值] 腾讯接口异常: {type(e).__name__}: {e}，使用本地缓存")

    # 后端预过滤
    pre_filtered = merged
    pre_skip_count = 0
    pre_skip_log = []
    try:
        t_pre_all = time.time()
        filtered = []
        _PFX_MAP = {"0": "sz", "1": "sh", "2": "bj"}
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
            pass_ok, pre_mc, skip_reason = m._quick_prefilter_pass(market, code)
            if not pass_ok:
                pre_skip_count += 1
                pre_skip_log.append(f"[预过滤] {code} 跳过 ({skip_reason})")
            else:
                filtered.append(stk)
        pre_filtered = filtered
        elapsed = time.time() - t_pre_all
        if pre_skip_count > 0:
            print(f"[预过滤] 批量预过滤完成: 跳过 {pre_skip_count} 只，剩余 {len(pre_filtered)} 只 (耗时 {elapsed:.1f}s)")
            for line in pre_skip_log:
                print(line)
        else:
            print(f"[预过滤] 批量预过滤完成: 全部通过 {len(pre_filtered)} 只 (耗时 {elapsed:.1f}s)")
    except Exception as e:
        print(f"[预过滤] 批量预过滤异常: {type(e).__name__}: {e}")

    return _json_response({
        "stocks": pre_filtered,
        "sources": sources,
        "total": len(pre_filtered),
        "pre_skipped": pre_skip_count,
        "errors": errors if errors else None,
    })


@router.get("/api/scan_one")
async def api_scan_one(
    code: str = Query(...),
    freq: str = Query("d"),
    prefix: str = Query(""),
    recent: str = Query("1"),
    source: str = Query("zxg"),
    mode: str = Query(""),
):
    """扫描单只股票"""
    t_scan_start = time.time()
    try:
        recent_days = max(1, int(recent))
    except ValueError:
        recent_days = 1

    if not code:
        return _json_response({"error": "缺少code参数"}, 400)

    try:
        t0 = time.time()
        _PREFIX_MAP = {"0": "SZ", "1": "SH", "2": "BJ", "hk": "HK"}
        market_prefix = _PREFIX_MAP.get(prefix, "")
        qualified_code = (market_prefix + code) if market_prefix else code
        market = market_prefix.lower() if market_prefix else ""

        if m._scan_aborted:
            return _json_response({"error": "扫描已终止", "aborted": True})

        with m._scan_lock:
            if m._scan_aborted:
                return _json_response({"error": "扫描已终止", "aborted": True})
            result = m.analyze_stock(qualified_code, freq=freq, cache_chan=True)

        t_analyze = time.time() - t0
        if "error" in result:
            m._scan_skip_log.append(f"{code} - {result['error']}")
            print(f"[耗时-扫描] {code} 分析失败: {result['error']}, 耗时{t_analyze:.3f}s")
            return _json_response({"error": result["error"]})

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
                print(f"[耗时-扫描-底分型] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 是底分型")
                return _json_response({
                    "code": code + "." + market.upper(), "name": stock_name,
                    "is_fx_d": True,
                    "last_close": klines[-1]["close"] if klines else 0,
                    "freq": freq,
                    "fx_strength": fx_strength,
                })
            else:
                mkt, cd = m._get_market_code(qualified_code)
                if mkt and cd:
                    cache_key = f"single_{mkt}_{cd}_{freq}_live"
                    m._cache_remove(cache_key)
                t_total = time.time() - t_scan_start
                print(f"[耗时-扫描-底分型] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 不是底分型")
                return _json_response({"code": code, "is_fx_d": False})

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
            print(f"[耗时-扫描-均线] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 分类:{ma_category}")
            resp_data = {
                "code": code + "." + market.upper(),
                "name": stock_name,
                "ma_category": ma_category,
                "last_close": round(last_close, 2),
                "freq": freq,
            }
            if ma_category > 3:
                mkt, cd = m._get_market_code(qualified_code)
                if mkt and cd:
                    cache_key = f"single_{mkt}_{cd}_{freq}_live"
                    m._cache_remove(cache_key)
            return _json_response(resp_data)

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
            mkt, cd = m._get_market_code(qualified_code)
            if mkt and cd:
                cache_key = f"single_{mkt}_{cd}_{freq}_live"
                m._cache_remove(cache_key)

        if has_points:
            t_total = time.time() - t_scan_start
            print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 有买卖点")
            return _json_response({
                "code": code + "." + market.upper(), "name": stock_name,
                "buy_points": buy_points,
                "sell_points": sell_points,
                "last_close": klines[-1]["close"] if klines else 0,
                "freq": freq,
                "below_ma120": below_ma120,
                "ma120_val": ma120_val,
            })
        else:
            t_total = time.time() - t_scan_start
            print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 无买卖点")
            return _json_response({"code": code, "buy_points": [], "sell_points": []})

    except Exception as exc:
        m._scan_skip_log.append(f"{code} - 异常: {exc}")
        t_total = time.time() - t_scan_start
        print(f"[耗时-扫描] {code} 异常: {exc}, 总耗时{t_total:.3f}s")
        return _json_response({"error": str(exc)})


@router.get("/api/scan_page_index_code")
async def api_scan_page_index_code(code: str = Query("")):
    """设置当前板块指数代码"""
    code = code.strip()
    if code:
        if "." in code:
            code = code.split(".")[0]
        m._page_index_code = code
        print(f"[成分股] 已设置板块指数代码: {code}")
        return _json_response({"ok": True, "code": code})
    else:
        return _json_response({"error": "缺少code参数"}, 400)


@router.get("/api/scan_start")
async def api_scan_start():
    """新一轮扫描开始"""
    m._scan_aborted = False
    m._scan_skip_log.clear()
    m._scan_start_time = time.time()
    try:
        m._load_stock_names_from_cache_file()
    except Exception as e:
        print(f"[警告] 异常: {type(e).__name__}: {e}")
    return _json_response({"ok": True})


@router.get("/api/scan_end")
async def api_scan_end():
    """扫描结束"""
    if m._scan_skip_log:
        print(f"\n========== 扫描异常/失败股票明细 ==========")
        print(f"共 {len(m._scan_skip_log)} 只:")
        for i, item in enumerate(m._scan_skip_log, 1):
            print(f"  {i}. {item}")
        print("============================================\n")
    else:
        print("\n[扫描明细] 全部扫描成功，无异常股票\n")

    if m._scan_start_time is not None:
        elapsed = time.time() - m._scan_start_time
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        time_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"
        skip_count = len(m._scan_skip_log)
        msg = f"耗时 {time_str}"
        if skip_count > 0:
            msg += f"，跳过 {skip_count} 只"
        m._send_windows_notification("扫描完成", msg)
        m._scan_start_time = None
    return _json_response({"count": len(m._scan_skip_log)})


@router.get("/api/scan_clear_cache")
async def api_scan_clear_cache():
    """关闭扫描面板"""
    print("[扫描缓存] 面板关闭，缓存由 LRU 自然淘汰")
    return _json_response({"cleared": 0})


@router.get("/api/scan_abort")
async def api_scan_abort():
    """中断扫描"""
    m._scan_aborted = True
    print("[扫描] 收到中断请求，设置终止标志")
    return _json_response({"ok": True})


# ═══════════════════════════════════════════════════════════════════════
# 路由 — 自选股保存
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/zxg_save")
async def api_zxg_save(codes: str = Query("")):
    """保存勾选的股票到通达信+同花顺自选股"""
    codes_list = codes.split(",") if codes else []
    if not codes_list:
        return _json_response({"error": "codes为空"}, 400)

    try:
        codes_raw = [c.strip() for c in codes_list]
        codes_ths = list(dict.fromkeys(codes_raw))

        # 通达信
        print(f"[保存] 通达信: 输入 {len(codes_raw)} 只, 代码={codes_raw}")
        tdx_added = m.save_to_zxg_blk(codes_raw)
        print(f"[保存] 通达信: 实际写入 {tdx_added} 只")

        # 同花顺
        ths_added = 0
        ths_msg = ""
        print(f"[保存] 同花顺: 输入 {len(codes_ths)} 只, 代码={codes_ths}")
        if m._THS_CLOUD_AVAILABLE:
            try:
                cloud_result = m.save_scan_to_ths_cloud(codes_ths)
                if "error" in cloud_result:
                    raise Exception(cloud_result["error"])
                ths_added = len(cloud_result.get("added", []))
                ths_msg = "ok"
                print(f"[保存] 同花顺: 新增{ths_added}, "
                      f"跳过{len(cloud_result.get('skipped',[]))}, "
                      f"失败{len(cloud_result.get('failed',[]))}")
            except Exception as e:
                err_str = str(e)
                if "登录状态失效" in err_str or "Cookie" in err_str:
                    ths_msg = "Cookie过期，请运行 ths_capture_cookie.py 重新获取"
                else:
                    ths_msg = f"云端同步失败: {err_str}"
                print(f"[保存] 同花顺: {ths_msg}")
        else:
            ths_msg = "App/ths_cloud_api.py 未找到，请确保 App/ 目录完整（阶段 2 已迁入 App/）"
            print(f"[保存] 同花顺: {ths_msg}")

        print(f"[保存] 汇总: 通达信={tdx_added}, 同花顺={ths_added}, msg={ths_msg}")
        return _json_response({
            "ok": True,
            "tdx_saved": tdx_added,
            "ths_saved": ths_added,
            "ths_msg": ths_msg,
        })
    except Exception as exc:
        traceback.print_exc()
        return _json_response({"error": str(exc)}, 500)


# ═══════════════════════════════════════════════════════════════════════
# 路由 — 选点管理
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/clear_saved_point")
async def api_clear_saved_point(code: str = Query(...), freq: str = Query("d")):
    """清除选点"""
    if not code:
        return _json_response({"error": "缺少code参数"}, 400)

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
    m._clear_saved_point_time(qualified_code, freq)
    cache_key = f"single_{market}_{normalized_code}_{freq}_live"
    with m._cache_lock:
        if cache_key in m._stocks_analysis_cache:
            del m._stocks_analysis_cache[cache_key]
    gc.collect()
    return _json_response({"ok": True})


# ═══════════════════════════════════════════════════════════════════════
# 路由 — 期货/期指
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/futures_manual_select_point")
async def api_futures_manual_select_point(
    symbol: str = Query(...),
    freq: str = Query("15s"),
    bi_idx: str = Query("-1"),
):
    """期货手动选点"""
    if not symbol or bi_idx == "-1":
        return _json_response({"error": "缺少必要参数 symbol 或 bi_idx"}, 400)
    try:
        result = m.futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {exc}")
    if "error" in result:
        return _json_response(result, 400)
    return _json_response(result)


@router.get("/api/futures_clear_saved_point")
async def api_futures_clear_saved_point(symbol: str = Query(...), freq: str = Query("15s")):
    """期货清除选点"""
    if not symbol:
        return _json_response({"error": "缺少symbol参数"}, 400)
    symbol_upper = symbol.upper()
    if symbol_upper in m.FUTURES_ALIASES:
        symbol = m.FUTURES_ALIASES[symbol_upper]
    m._clear_saved_point_time(symbol, freq)
    return _json_response({"ok": True})


@router.get("/api/futures_cleanup")
async def api_futures_cleanup():
    """清理所有期货数据"""
    m._cleanup_all_futures_data()
    return _json_response({"ok": True})


@router.get("/api/futures_status")
async def api_futures_status():
    """期货状态"""
    return _json_response({"ok": True, "architecture": "self-contained"})


@router.get("/api/futures_config")
async def api_futures_config():
    """期货可用周期列表"""
    from DataAPI.TqSdkAPI import SUPPORTED_FREQS, DISABLED_FREQS
    return _json_response({
        "supported_freqs": SUPPORTED_FREQS,
        "disabled_freqs": DISABLED_FREQS,
    })


# ═══════════════════════════════════════════════════════════════════════
# 路由 — SSE 实时推送（期货）
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/futures_stream")
async def api_futures_stream(
    symbol: str = Query(...),
    freq: str = Query("15s"),
    start_time: str = Query(None),
    dual: bool = Query(False),
    sub_freq: str = Query(None),
):
    """SSE 实时推送（期货单/双窗口）"""
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少symbol参数")

    if not m.TQ_AVAILABLE:
        raise HTTPException(status_code=503, detail="天勤数据源不可用")

    if dual:
        handler = m.ChartHandler._handle_sse_stream_dual
        gen = _sse_generator(handler, symbol, freq, sub_freq, start_time)
    else:
        handler = m.ChartHandler._handle_sse_stream_single
        gen = _sse_generator(handler, symbol, freq, start_time)

    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# 路由 — 股票名称刷新
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/refresh_stock_names")
async def api_refresh_stock_names():
    """启动股票名称刷新"""
    if m._refresh_status["running"]:
        return _json_response({"status": "already_running", **m._refresh_status})
    else:
        def _do_refresh():
            try:
                m._refresh_stock_names()
            except Exception as e:
                traceback.print_exc()
                print(f"[错误] refresh_stock_names异常: {e}")
        t = threading.Thread(target=_do_refresh, daemon=True)
        t.start()
        return _json_response({"status": "started", "msg": "股票名称刷新已启动"})


@router.get("/api/refresh_status")
async def api_refresh_status():
    """查询刷新状态"""
    return _json_response(m._refresh_status)


# ═══════════════════════════════════════════════════════════════════════
# 路由 — 标注
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/annotations")
async def api_annotations_get(code: str = Query(...), freq: str = Query("d")):
    """获取标注数据"""
    if not code:
        return _json_response({"error": "缺少code参数"}, 400)
    anns = m._get_annotations_for(code, freq)
    return _json_response({"annotations": anns, "code": code, "freq": freq})


@router.post("/api/annotations")
async def api_annotations_post(body: dict = Body(...)):
    """标注增删改"""
    action = body.get("action", "")
    code = body.get("code", "")
    freq = body.get("freq", "d")
    date_str = body.get("date", "")
    text = body.get("text", "")
    y_offset = body.get("y_offset", 0)

    if not code:
        return _json_response({"error": "缺少code参数"}, 400)

    if action == "add":
        if not date_str or not text:
            return _json_response({"error": "缺少date或text参数"}, 400)
        success = m._add_annotation(code, freq, date_str, text, y_offset)
        return _json_response({"ok": success, "duplicate": not success})
    elif action == "delete":
        if not date_str or not text:
            return _json_response({"error": "缺少date或text参数"}, 400)
        success = m._delete_annotation(code, freq, date_str, text)
        return _json_response({"ok": success})
    elif action == "delete_by_date":
        if not date_str:
            return _json_response({"error": "缺少date参数"}, 400)
        success = m._delete_annotation_by_date(code, freq, date_str)
        return _json_response({"ok": success})
    elif action == "delete_all":
        success = m._delete_all_annotations(code, freq)
        return _json_response({"ok": success})
    elif action == "update":
        old_text = body.get("old_text", "")
        new_text = body.get("text", "")
        if not date_str or not old_text or not new_text:
            return _json_response({"error": "缺少date/old_text/text参数"}, 400)
        m._delete_annotation(code, freq, date_str, old_text)
        success = m._add_annotation(code, freq, date_str, new_text, y_offset)
        return _json_response({"ok": success})
    else:
        return _json_response({"error": f"未知action: {action}"}, 400)


@router.get("/api/annotations_scan")
async def api_annotations_scan(freq: str = Query("")):
    """自选扫描：返回有标注的股票列表"""
    codes = m._get_annotated_codes(freq)
    return _json_response({"codes": codes, "total": len(codes)})


# ═══════════════════════════════════════════════════════════════════════
# 路由 — 盘后下载
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/tdx_download_start")
async def api_tdx_download_start_get(
    categories: str = Query("[]"),
    day_start: str = Query(""),
    min_start: str = Query(""),
):
    """盘后下载启动 (GET)"""
    if not m._ELTDX_AVAILABLE:
        return _json_response({"error": "eltdx 未安装，请先 pip install eltdx"}, 400)
    try:
        cats = json.loads(categories) if isinstance(categories, str) else categories
    except Exception:
        return _json_response({"error": "categories 参数格式错误"}, 400)
    if not cats:
        return _json_response({"error": "请选择要下载的数据类型"}, 400)
    ok, msg = m._start_download(m.DOWNLOAD_DIR, cats,
                                day_start=day_start or None,
                                min_start=min_start or None)
    return _json_response({"ok": ok, "message": msg}, 200 if ok else 409)


@router.post("/api/tdx_download_start")
async def api_tdx_download_start_post(body: dict = Body(...)):
    """盘后下载启动 (POST)"""
    if not m._ELTDX_AVAILABLE:
        return _json_response({"error": "eltdx 未安装，请先 pip install eltdx"}, 400)
    categories = body.get("categories") or []
    if not categories:
        return _json_response({"error": "请选择要下载的数据类型"}, 400)
    day_start = body.get("day_start") or ""
    min_start = body.get("min_start") or ""
    ok, msg = m._start_download(m.DOWNLOAD_DIR, categories,
                                day_start=day_start or None,
                                min_start=min_start or None)
    return _json_response({"ok": ok, "message": msg}, 200 if ok else 409)


@router.get("/api/tdx_download_status")
async def api_tdx_download_status():
    """盘后下载进度"""
    status = m._get_download_status()
    return _json_response(status)


@router.get("/api/tdx_download_stop")
async def api_tdx_download_stop():
    """盘后下载停止"""
    ok, msg = m._stop_download()
    return _json_response({"ok": ok, "message": msg})


# ═══════════════════════════════════════════════════════════════════════
# 静态文件服务（替代 ChartHandler 的 fallback 逻辑）
# FastAPI 会优先匹配显式路由，匹配不到才 fallback 到静态文件，/docs 不受影响
# ═══════════════════════════════════════════════════════════════════════

# 兼容重定向：旧版页面路径 chan_chart.html → 新首页 index.html
from fastapi.responses import RedirectResponse

@router.get("/chan_chart.html", include_in_schema=False)
async def chan_chart_redirect():
    return RedirectResponse(url="/")

# 第一阶段：挂载 Frontend/ 为前端页面目录（优先）
# 阶段 2：路由聚合 —— 本文件全部路由挂到 router 上，由下方 include_router 挂入 app；
# FrontAPI.py（新入口）同样 include 本 router，实现单一路由源、双入口可用。
app.include_router(router)
frontend_dir = app_config.frontend_dir
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
else:
    print(f"[警告] Frontend/ 目录不存在 ({frontend_dir})，回退到 OUTPUT_DIR 静态挂载")
    app.mount("/", StaticFiles(directory=m.OUTPUT_DIR, html=True), name="static")


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import socket
    import uvicorn

    # ── 端口占用检测：若端口已被旧进程占用，先给出明确提示，避免浏览器打到旧服务 ──
    PORT = app_config.port            # 阶段 2：端口来自 AppConfig（PORT 环境变量 / .env 可覆盖）
    HOST = app_config.host
    try:
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _probe.bind((HOST, PORT))
        _probe.close()
    except OSError:
        print(f"[错误] 端口 {PORT} 已被占用！")
        print(f"[错误] 可能原因：上一次启动的服务器进程仍在运行（旧版 my_chan_main.py 或 api_server.py）。")
        print(f"[解决] 请先关闭占用 {PORT} 端口的旧进程，再重新启动本服务。")
        print(f"[解决] Windows 可在命令行执行: netstat -ano | findstr {PORT}  查看占用进程 PID，")
        print(f"[解决] 然后执行: taskkill /PID <PID> /F  结束旧进程。")
        sys.exit(1)

    last_code, last_freq = m._load_last_code_freq()
    if last_code:
        print(f"[信息] 恢复上次: {last_code} (周期: {last_freq})")
    else:
        print(f"[信息] 使用默认股票: {m.SYMBOL_CODE}")

    print(f"[信息] FastAPI 服务器启动: http://{HOST}:{PORT}")
    print(f"[信息] API 文档:   http://{HOST}:{PORT}/docs")
    print(f"[信息] K线图表页:  http://{HOST}:{PORT}/")
    print(f"[信息] 新版入口为 FrontAPI.py（统一异常中间件 + 健康检查），本入口保留兼容")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
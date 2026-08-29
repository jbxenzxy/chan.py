# -*- coding: utf-8 -*-
"""
App/AppAMO.py —— 市场量能功能域
=========================================================================
点击页面右上角「市场量能」按钮后的操作：读取 TDX 本地指数日线成交额，
按 K 线页面「视口」日期区间绘制全市场成交额曲线。

数据源（仅 TDX 本地，无任何兜底）：
  - 沪市成交额：{vipdoc_dir}/sh/lday/sh000001.day（上证指数日线）
  - 深市成交额：{vipdoc_dir}/sz/lday/sz399106.day（深证综指日线）
  两者 amount 字段（成交额，单位：元）逐日相加 = 全市场成交额。

通达信 .day 文件每条记录 32 字节（A 股格式，与 DataAPI/TdxAPI.py
read_tdx_day_file 同一格式）：
  日期(I4) 开盘(I4) 最高(I4) 最低(I4) 收盘(I4) 成交额(f4) 成交量(I4) 保留(I4)
  第 6 个字段 amount（f4）即当日成交额（元）。
  → sh000001.day / sz399106.day 的 amount 字段就是沪市/深市成交额，
    无需换算，逐日相加即为全市场成交额（单位：元）。

视口边界映射：前端把 K 线页面「视口」最左/最右 K 线日期作为
start_date / end_date 传入，本模块按日期区间过滤日线成交额序列。
（按钮仅在上证指数日K图可用，故视口即日线日期区间；若传入日内
周期视口，仍按「覆盖日期内的每日成交额」返回，天然满足。）

指标计算（区间内）：
  - 峰值成交额（最大）
  - 当前成交额（最右）
  - 缩至峰值占比 = 当前/峰值 * 100（口径同市场量能文章，如 3.45万亿→0.97万亿=28%）

无持久化：面板关闭即释放数据，本模块不存任何状态。

锁分类：call_amo 登记 SERIAL（AppOrch.LOCK_POLICY），持 _ENGINE_LOCK
与其它引擎调用串行（读盘极快，锁开销可忽略；与下载写盘天然互斥）。
"""
import os
import struct
import time

# 可执行锁策略（P1-2：按 LOCK_POLICY 分类执行，SERIAL → _ENGINE_LOCK；
# 定义在 App/AppChart.py，AppOrch 聚合 re-export）
from App.AppChart import engine_section
# 配置中心（vipdoc_dir 单一事实源）
from App.AppConfig import app_config
# 领域异常（数据源缺失 → DataFetchError）
from App.AppErrors import DataFetchError
from App.AppLog import get_logger
log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════════════

# 沪市成交额来源：上证指数日线（amount 字段 = 沪市成交额）
_SH_INDEX = ("sh", "000001")
# 深市成交额来源：深证综指日线（amount 字段 = 深市成交额）
_SZ_INDEX = ("sz", "399106")

# 通达信 .day 记录大小（字节）
_RECORD_SIZE = 32


def _index_day_file(market: str, code: str) -> str:
    """指数日线文件路径：{vipdoc_dir}/{market}/lday/{market}{code}.day"""
    return os.path.join(app_config.vipdoc_dir, market, "lday",
                        f"{market}{code}.day")


def _read_index_amounts(filepath: str):
    """读取指数日线 .day 文件，返回 [(date_str, amount_元), ...]（按日期升序）。

    仅取日期 + 成交额（amount，f4），价格字段与本功能无关。
    与 DataAPI/TdxAPI.read_tdx_day_file 的 A 股格式一致：
      日期(I4) 开盘(I4) 最高(I4) 最低(I4) 收盘(I4) 成交额(f4) 成交量(I4) 保留(I4)
    """
    if not os.path.exists(filepath):
        raise DataFetchError(f"TDX 本地指数日线缺失: {filepath}")
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise DataFetchError(f"读取 TDX 本地指数日线失败: {filepath} ({exc})") from exc

    out = []
    for i in range(0, len(data) - _RECORD_SIZE + 1, _RECORD_SIZE):
        row = data[i:i + _RECORD_SIZE]
        try:
            date_int, _o, _h, _l, _c, amount, _vol, _resv = \
                struct.unpack('<IIIIIfII', row)
        except struct.error:
            continue
        year = date_int // 10000
        month = (date_int % 10000) // 100
        day = date_int % 100
        if year < 1990 or year > 2030 or month < 1 or month > 12 or day < 1 or day > 31:
            continue
        out.append((f"{year:04d}-{month:02d}-{day:02d}", float(amount)))
    return out


def _merge_market_amounts(sh_rows, sz_rows):
    """沪市 + 深市成交额逐日相加 → {date: 全市场成交额(元)}。

    两指数交易日历一致，直接按日期对齐相加；个别缺失日以另一市为准。
    """
    merged = {}
    for date_str, amount in sh_rows:
        merged[date_str] = merged.get(date_str, 0.0) + amount
    for date_str, amount in sz_rows:
        merged[date_str] = merged.get(date_str, 0.0) + amount
    return merged


def _fmt_yi(amount_yuan: float) -> float:
    """元 → 亿元（保留 2 位小数，前端展示用）"""
    return round(amount_yuan / 1e8, 2)


def _norm_date(s: str) -> str:
    """把前端传入的日期统一为连字符格式（YYYY/MM/DD → YYYY-MM-DD），
    以便与 .day 读取出的内部连字符日期做字符串区间比较。"""
    return (s or "").replace("/", "-")


def call_amo(start_date: str, end_date: str):
    """市场量能数据（同步入口，REST 路由经 run_in_threadpool 调用）

    - 仅读 TDX 本地指数日线（sh000001 + sz399106），无任何兜底
    - 持 _ENGINE_LOCK 串行（SERIAL 分类，见 AppOrch.LOCK_POLICY）
    - 入参前端日期为斜杠格式（%Y/%m/%d，全站 K 线契约）；内部转连字符比较，
      返回的 dates/peak_date/current_date 也统一为斜杠格式以便前端横轴对齐
    - 返回 {dates, amounts(亿元), stats:{peak, peak_date, current,
      current_date, peak_ratio}}；数据源缺失抛 DataFetchError
    """
    start_date = _norm_date(start_date)   # 前端斜杠 → 内部连字符，参与比较
    end_date = _norm_date(end_date)
    # DEBUG 级：正常只读接口，避免刷屏；定位问题时可临时调 DEBUG 级看到
    log.debug(f"[api] /api/amo/read 开始: start_date={start_date!r} end_date={end_date!r}")
    t0 = time.time()
    # P1-2：锁策略经 engine_section 按 LOCK_POLICY 执行（SERIAL → _ENGINE_LOCK）
    with engine_section("call_amo"):
        sh_path = _index_day_file(*_SH_INDEX)
        sz_path = _index_day_file(*_SZ_INDEX)
        sh_rows = _read_index_amounts(sh_path)
        sz_rows = _read_index_amounts(sz_path)
        merged = _merge_market_amounts(sh_rows, sz_rows)

        # 按视口日期区间过滤（含两端；区间外数据不参与指标计算）
        # 内部比较统一用连字符格式；输出再转斜杠（全站横轴约定 %Y/%m/%d）
        dates = sorted(d for d in merged if start_date <= d <= end_date)
        amounts = [merged[d] for d in dates]

        if not dates:
            log.debug(f"[api] /api/amo/read 完成: 区间无数据 "
                      f"({start_date}~{end_date}) 耗时 {time.time() - t0:.2f}s")
            return {
                "dates": [], "amounts": [],
                "stats": {"peak": None, "peak_date": None,
                          "current": None, "current_date": None,
                          "peak_ratio": None},
            }

        # 指标：峰值 / 当前（最右）/ 缩至峰值占比
        # peak_ratio = current/peak*100，口径同市场量能文章："成交额缩至峰值的百分之几"
        # （例子：3.45万亿→0.97万亿 为 28%）。与"缩水幅度(1-占比)"互为 100- 互补。
        peak_idx = max(range(len(amounts)), key=lambda i: amounts[i])
        peak = amounts[peak_idx]
        current = amounts[-1]
        peak_ratio = round(current / peak * 100.0, 2) if peak > 0 else None

        # 对外统一斜杠格式（%Y/%m/%d），与全站 K 线日期契约一致
        out_dates = [d.replace("-", "/") for d in dates]

        result = {
            "dates": out_dates,
            "amounts": [_fmt_yi(a) for a in amounts],
            "stats": {
                "peak": _fmt_yi(peak),
                "peak_date": out_dates[peak_idx],
                "current": _fmt_yi(current),
                "current_date": out_dates[-1],
                "peak_ratio": peak_ratio,
            },
        }
    log.debug(f"[api] /api/amo/read 完成: {len(dates)} 天 "
              f"耗时 {time.time() - t0:.2f}s")
    return result

# -*- coding: utf-8 -*-
"""
DataAPI/ElTdxAPI.py —— 盘后下载（日/分钟线下载、二进制解析）
=====================================================================
实现页面右上角「盘后下载」按钮功能：批量把 TDX 的日 / 分钟线数据
下载到本地 vipdoc 目录，职责内聚在数据源抽象层。

对外函数面：
  _tdx_day_record / _tdx_min_record / _date_to_int / _date_to_min_packed /
  _ensure_dir / _download_day_kline / _download_min_kline / _download_task /
  _start_download / _stop_download / _get_download_status /
  _collect_codes_from_vipdoc

下载状态：
  _download_state / _download_lock 为本模块状态（单一事实源）。
"""
import os
import struct
import threading
import time
from datetime import datetime, timedelta

from chinese_calendar import is_holiday

import logging
log = logging.getLogger(__name__)

# 依赖方向：本模块属 DataAPI 数据源抽象层，不 import App 层——
# 目录路径一律由调用方注入（vipdoc_dir 参数），配置读取留在 App 层。

# ============================================================
# eltdx 盘后下载引擎（可选依赖）
# ============================================================
try:
    from eltdx import TdxClient
    _ELTDX_AVAILABLE = True
except ImportError:
    _ELTDX_AVAILABLE = False
    TdxClient = None
    log.warning("[警告] eltdx 未安装，盘后下载功能不可用。pip install eltdx")

# 下载状态管理（单一事实源）
_download_state = {
    "running": False,
    "aborted": False,
    "progress": 0,        # 0-100
    "total_stocks": 0,
    "completed_stocks": 0,
    "current_stock": "",
    "current_category": "",
    "errors": [],
    "start_time": None,
    "end_time": None,
    "bytes_written": 0,
    "files_written": 0,
}
_download_lock = threading.Lock()


def _tdx_day_record(date_int, open_milli, high_milli, low_milli, close_milli, amount, volume, last_close_milli=None, is_ext_market=False):
    """
    将 K 线数据打包为 TDX .day 格式的 32 字节记录
    参数 open_milli/high_milli/low_milli/close_milli 为千分价格单位（price * 1000），来自 KlineBar.*_price_milli
    volume 为实际手数，来自 KlineBar.volume_wire_value
    last_close_milli 为前一根K线收盘价千分价，来自 KlineBar.last_close_price_milli
    A股(sh/sz/bj): IIIIIfII - 日期(I) 开(I) 高(I) 低(I) 收(I) 成交额(f) 成交量(I) 上日收盘(I)
                    价格是 int，单位是厘（(千分价+5) // 10 四舍五入）
    扩展市场(ds/hk): IfffffII - 日期(I) 开(f) 高(f) 低(f) 收(f) 成交额(f) 成交量(I) 结算价(I)
                    价格是 float，千分价 / 1000 还原为实际价格
    """
    # 四舍五入: (x + 5) // 10 替代 x // 10，避免整除向下截断
    last_close_cent = (last_close_milli + 5) // 10 if last_close_milli is not None else 0
    if is_ext_market:
        return struct.pack(
            "<IfffffII",
            date_int,                      # 日期 YYYYMMDD
            open_milli / 1000.0,               # 开盘价
            high_milli / 1000.0,               # 最高价
            low_milli / 1000.0,                # 最低价
            close_milli / 1000.0,              # 收盘价
            float(amount),                     # 成交额（元）
            round(volume),                     # 成交量（手）四舍五入取整
            0,                                 # 结算价
        )
    else:
        return struct.pack(
            "<IIIIIfII",
            date_int,                      # 日期 YYYYMMDD
            (open_milli + 5) // 10,            # 开盘价（厘）四舍五入
            (high_milli + 5) // 10,            # 最高价（厘）
            (low_milli + 5) // 10,             # 最低价（厘）
            (close_milli + 5) // 10,           # 收盘价（厘）
            float(amount),                     # 成交额（元）
            round(volume),                     # 成交量（手）四舍五入取整
            last_close_cent,                   # 上日收盘（厘）
        )


def _tdx_min_record(date_int, minute_int, open_price, high, low, close, amount, volume):
    """
    将分钟线数据打包为 TDX .lc1/.lc5 格式的 32 字节记录
    格式: HHfffffII - 日期(H) + 时间(H) + 开(f) + 高(f) + 低(f) + 收(f) + 成交额(f) + 成交量(I) + 保留(I)
    日期编码: year = num // 2048 + 2004, month = (num % 2048) // 100, day = (num % 2048) % 100
    时间编码: 从0点开始的分钟数 (HH*60+MM)
    价格字段是 float 类型，直接使用
    成交量字段是 unsigned int 类型（与 TdxAPI.py read_tdx_min_file 的 numpy dtype 一致）
    """
    return struct.pack(
        "<HHfffffII",
        date_int & 0xFFFF,             # 压缩日期: (year-2004)*2048+month*100+day
        minute_int & 0xFFFF,               # 分钟: HH*60+MM
        float(open_price),                 # 开盘价
        float(high),                       # 最高价
        float(low),                        # 最低价
        float(close),                      # 收盘价
        float(amount),                     # 成交额
        int(volume),                       # 成交量 (unsigned int)
        0,                                 # 保留
    )


def _date_to_int(dt):
    """将 datetime 对象转为 YYYYMMDD 整数"""
    return dt.year * 10000 + dt.month * 100 + dt.day


def _date_to_min_packed(dt):
    """
    将 datetime 对象转为 TDX 分钟线压缩日期格式
    year = num // 2048 + 2004  →  num = (year - 2004) * 2048 + month * 100 + day
    """
    return (dt.year - 2004) * 2048 + dt.month * 100 + dt.day


def _ensure_dir(path):
    """确保目录存在"""
    os.makedirs(path, exist_ok=True)


def _download_day_kline(client, code, market, vipdoc_dir, code_prefix=None, start_date=None, _progress_callback=None):
    """
    下载单只股票的日 K 线数据并写入 .day 文件
    market: 'sh' | 'sz' | 'bj' | 'ds' | 'hk'
    code_prefix: 扩展市场代码前缀（'31#' 或 '62#'），仅 ds/hk 市场使用
    start_date: str 如 "2021-07-28"，只下载此日期及之后的K线；None 表示只拉最新
    """
    try:
        is_ext = (market in ('ds', 'hk'))
        # 解析起始日期
        min_date_int = None
        if start_date:
            try:
                parts = start_date.split("-")
                min_date_int = int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])
            except Exception:
                pass

        # 确定文件路径
        if is_ext:
            prefix = code_prefix or "31#"
            mkt_dir = os.path.join(vipdoc_dir, "ds", "lday")
            filename = f"{prefix}{code}.day"
            full_code = f"{prefix}{code}"
        else:
            mkt_dir = os.path.join(vipdoc_dir, market, "lday")
            filename = f"{market}{code}.day"
            full_code = f"{market}{code}"

        _ensure_dir(mkt_dir)
        file_path = os.path.join(mkt_dir, filename)

        if start_date is None:
            # === 最新模式：每天执行，只拉服务器最新1页，去重后追加 ===
            existing_dates = set()
            if os.path.exists(file_path):
                try:
                    with open(file_path, "rb") as f:
                        while True:
                            rec = f.read(32)
                            if len(rec) < 32:
                                break
                            d = struct.unpack_from("<I", rec, 0)[0]
                            existing_dates.add(d)
                except Exception:
                    pass

            try:
                series = client.bars.all(full_code, period="day", max_pages=1)
            except Exception as _e:
                log.warning(f"[下载警告] {full_code} 日线拉取失败: {_e}")
                return 0, 0, None

            if not series or not series.bars:
                return 0, 0, None

            new_records = 0
            max_date_int = 0
            with open(file_path, "ab") as f:
                for bar in series.bars:
                    d = bar.time
                    date_int = _date_to_int(d)
                    if date_int in existing_dates:
                        continue
                    record = _tdx_day_record(
                        date_int,
                        bar.open_price_milli, bar.high_price_milli,
                        bar.low_price_milli, bar.close_price_milli,
                        bar.amount,
                        bar.volume_wire_value,
                        last_close_milli=bar.last_close_price_milli,
                        is_ext_market=is_ext,
                    )
                    f.write(record)
                    new_records += 1
                    if date_int > max_date_int:
                        max_date_int = date_int

            return new_records, 1, max_date_int if max_date_int > 0 else None

        else:
            # === 指定日期模式：分页拉取，当某页最老数据早于目标日期时提前终止 ===
            bars = []
            start = 0
            page_size = 800
            while True:
                try:
                    page = client.bars.get(full_code, period="day", start=start, count=page_size)
                except Exception as _e:
                    log.warning(f"[下载警告] {full_code} 日线分页拉取失败: {_e}")
                    break
                if not hasattr(page, "bars") or not page.bars:
                    break
                bars.extend(page.bars)
                # 检查该页最老的那条是否已早于目标日期：后续页只会更老，终止
                oldest_date_int = _date_to_int(page.bars[-1].time)
                if min_date_int is not None and oldest_date_int < min_date_int:
                    break
                if page.count < page_size:
                    break
                start += page_size

            if not bars:
                return 0, 0, None

            records = []
            max_date_int = 0
            for bar in bars:
                d = bar.time
                date_int = _date_to_int(d)
                if min_date_int is not None and date_int < min_date_int:
                    continue
                record = _tdx_day_record(
                    date_int,
                    bar.open_price_milli, bar.high_price_milli,
                    bar.low_price_milli, bar.close_price_milli,
                    bar.amount,
                    bar.volume_wire_value,
                    last_close_milli=bar.last_close_price_milli,
                    is_ext_market=is_ext,
                )
                records.append(record)
                if date_int > max_date_int:
                    max_date_int = date_int

            if records:
                with open(file_path, "wb") as f:
                    for rec in records:
                        f.write(rec)

            return len(records), 1, max_date_int if max_date_int > 0 else None

    except Exception:
        raise


def _download_min_kline(client, code, market, period, vipdoc_dir, code_prefix=None, start_date=None, _progress_callback=None):
    """
    下载单只股票的分钟 K 线数据
    period: '1m' | '5m'
    code_prefix: 扩展市场代码前缀（'31#' 或 '62#'），仅 ds/hk 市场使用
    start_date: str 如 "2025-07-28"，只下载此日期及之后的分钟线；None 表示只拉最新
    """
    try:
        is_ext = (market in ('ds', 'hk'))
        # 解析起始日期
        min_date_int = None
        if start_date:
            try:
                parts = start_date.split("-")
                min_date_int = int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])
            except Exception:
                pass

        # 确定文件路径和扩展名
        if period == "5m":
            ext = "lc5"
            sub_dir = "fzline"
        else:
            ext = "lc1"
            sub_dir = "minline"

        if is_ext:
            prefix = code_prefix or "31#"
            mkt_dir = os.path.join(vipdoc_dir, "ds", sub_dir)
            filename = f"{prefix}{code}.{ext}"
            full_code = f"{prefix}{code}"
        else:
            mkt_dir = os.path.join(vipdoc_dir, market, sub_dir)
            filename = f"{market}{code}.{ext}"
            full_code = f"{market}{code}"

        _ensure_dir(mkt_dir)
        file_path = os.path.join(mkt_dir, filename)

        if start_date is None:
            # === 最新模式：每天执行，只拉服务器最新1页，去重后追加 ===
            existing_keys = set()
            if os.path.exists(file_path):
                try:
                    with open(file_path, "rb") as f:
                        while True:
                            rec = f.read(32)
                            if len(rec) < 32:
                                break
                            date_val = struct.unpack_from("<H", rec, 0)[0]
                            min_val = struct.unpack_from("<H", rec, 2)[0]
                            existing_keys.add((date_val, min_val))
                except Exception:
                    pass

            try:
                series = client.bars.all(full_code, period=period, max_pages=1)
            except Exception as _e:
                log.warning(f"[下载警告] {full_code} {period} 拉取失败: {_e}")
                return 0, 0, None

            if not series or not series.bars:
                return 0, 0, None

            new_records = 0
            max_date_int = 0
            with open(file_path, "ab") as f:
                for bar in series.bars:
                    d = bar.time
                    date_int = _date_to_int(d)
                    date_packed = _date_to_min_packed(d)
                    minute_int = d.hour * 60 + d.minute
                    key = (date_packed, minute_int)
                    if key in existing_keys:
                        continue
                    record = _tdx_min_record(
                        date_packed, minute_int,
                        bar.open, bar.high, bar.low, bar.close,
                        bar.amount,
                        bar.volume_wire_value,
                    )
                    f.write(record)
                    new_records += 1
                    if date_int > max_date_int:
                        max_date_int = date_int

            return new_records, 1, max_date_int if max_date_int > 0 else None

        else:
            # === 指定日期模式：分页拉取，当某页最老数据早于目标日期时提前终止 ===
            bars = []
            start = 0
            page_size = 800
            while True:
                try:
                    page = client.bars.get(full_code, period=period, start=start, count=page_size)
                except Exception as _e:
                    log.warning(f"[下载警告] {full_code} {period} 分页拉取失败: {_e}")
                    break
                if not hasattr(page, "bars") or not page.bars:
                    break
                bars.extend(page.bars)
                # 检查该页最老的那条是否已早于目标日期：后续页只会更老，终止
                oldest_date_int = _date_to_int(page.bars[-1].time)
                if min_date_int is not None and oldest_date_int < min_date_int:
                    break
                if page.count < page_size:
                    break
                start += page_size

            if not bars:
                return 0, 0, None

            records = []
            max_date_int = 0
            for bar in bars:
                d = bar.time
                date_int = _date_to_int(d)
                if min_date_int is not None and date_int < min_date_int:
                    continue
                date_packed = _date_to_min_packed(d)
                minute_int = d.hour * 60 + d.minute
                record = _tdx_min_record(
                    date_packed, minute_int,
                    bar.open, bar.high, bar.low, bar.close,
                    bar.amount,
                    bar.volume_wire_value,
                )
                records.append(record)
                if date_int > max_date_int:
                    max_date_int = date_int

            if records:
                with open(file_path, "wb") as f:
                    for rec in records:
                        f.write(rec)

            return len(records), 1, max_date_int if max_date_int > 0 else None

    except Exception:
        raise


def _download_task(vipdoc_dir, categories, day_start_str=None, min_start_str=None, progress_callback=None):
    """
    后台下载任务主函数
    categories: list of dict, e.g. [{"type": "day", "market": "sh"}, ...]
    day_start_str: str 如 "2021-07-28"，日线起始日期；None 表示下载全部
    min_start_str: str 如 "2025-07-28"，分钟线起始日期；None 表示下载全部
    """
    global _download_state

    with _download_lock:
        _download_state["running"] = True
        _download_state["aborted"] = False
        _download_state["progress"] = 0
        _download_state["total_stocks"] = 0
        _download_state["completed_stocks"] = 0
        _download_state["current_stock"] = ""
        _download_state["current_category"] = ""
        _download_state["errors"] = []
        _download_state["start_time"] = time.time()
        _download_state["bytes_written"] = 0
        _download_state["files_written"] = 0
        _download_state["latest_data_date"] = None

    try:
        # 确保下载目录存在
        os.makedirs(vipdoc_dir, exist_ok=True)

        # 收集所有需要下载的股票
        all_tasks = []
        log.info(f"[下载] 开始收集代码列表, 共 {len(categories)} 个分类: {categories}")
        log.info(f"[下载] DOWNLOAD_DIR={vipdoc_dir}, day_start={day_start_str}, min_start={min_start_str}")

        # 构建 (market, type) 快速查找集合
        wanted_market_types = set()
        for cat in categories:
            mkt = cat["market"]
            if mkt in ("ds", "hk"):
                log.warning(f"[下载] {mkt}: eltdx 不支持港股市场，跳过")
                _download_state["errors"].append(f"港股/扩展市场({mkt})暂不支持，请使用其他方式获取港股数据")
                continue
            wanted_market_types.add((mkt, cat["type"]))

        if not wanted_market_types:
            log.info("[下载] 没有需要下载的分类")
            _download_state["running"] = False
            return

        # probe_hosts=False：关闭启动服务器探测（探测会写排名缓存
        # tdx_server_ranking.json，Windows 下文件占用会抛 OSError →
        # RuntimeWarning，与扫描侧 eltdx 单例修复同根因；连接失败时
        # eltdx 内部仍会按 hosts 顺序重连，非必需）
        with TdxClient(timeout=10, probe_hosts=False) as client:
            with _download_lock:
                _download_state["current_category"] = "获取A股代码列表"

            # 使用 eltdx 内置的 A 股过滤（基于服务端返回的 category 字段）
            a_share_codes = client.get_a_share_codes_all()
            log.info(f"[下载] 从服务器获取到 {len(a_share_codes)} 只A股代码")

            # 按市场和类型分配到任务列表
            market_counts = {}
            for full_code in a_share_codes:
                exchange = full_code[:2]  # "sh", "sz", "bj"
                code = full_code[2:]      # 6位数字代码

                for (mkt, cat_type) in wanted_market_types:
                    if exchange == mkt:
                        all_tasks.append({
                            "code": code,
                            "market": exchange,
                            "type": cat_type,
                            "code_prefix": None,
                        })
                        market_counts[mkt] = market_counts.get(mkt, 0) + 1

            for mkt, cnt in sorted(market_counts.items()):
                # 每个市场的任务数 = A股数 × 该市场在wanted中的类型数
                type_count = sum(1 for (mm, ct) in wanted_market_types if mm == mkt)
                stock_count = cnt // type_count if type_count > 0 else cnt
                log.info(f"[下载] {mkt}: {stock_count} 只A股 × {type_count} 种类型 = {cnt} 个任务")

            # 开始逐只下载
            total = len(all_tasks)
            log.info(f"[下载] 代码收集完成, 共 {total} 只股票, {len(_download_state['errors'])} 个错误")
            with _download_lock:
                _download_state["total_stocks"] = total

            completed = 0

            log.info(f"[下载] 开始逐只下载...")
            for i, task in enumerate(all_tasks):
                with _download_lock:
                    if _download_state["aborted"]:
                        break
                    prefix_display = (task.get("code_prefix") or "") + task["code"]
                    _download_state["current_stock"] = f"{task['market']}:{prefix_display}"
                    if total > 0:
                        _download_state["progress"] = (completed * 100) // total

                try:
                    code_prefix = task.get("code_prefix")
                    if task["type"] == "day":
                        records, files, max_date = _download_day_kline(
                            client, task["code"], task["market"],
                            vipdoc_dir, code_prefix=code_prefix,
                            start_date=day_start_str,
                            _progress_callback=progress_callback
                        )
                    else:  # 5m
                        records, files, max_date = _download_min_kline(
                            client, task["code"], task["market"],
                            task["type"], vipdoc_dir,
                            code_prefix=code_prefix,
                            start_date=min_start_str,
                            _progress_callback=progress_callback
                        )
                    completed += 1
                    with _download_lock:
                        _download_state["completed_stocks"] = completed
                        _download_state["files_written"] += files
                        if max_date and (not _download_state["latest_data_date"] or max_date > _download_state["latest_data_date"]):
                            _download_state["latest_data_date"] = max_date
                except Exception as _e:
                    err_msg = f"{task['market']}{(task.get('code_prefix') or '')}{task['code']}: {str(_e)[:80]}"
                    if completed < 5:  # 只打印前5个错误详情
                        log.error(f"[下载] 错误: {err_msg}")
                    with _download_lock:
                        _download_state["errors"].append(err_msg)
                        _download_state["completed_stocks"] = completed + 1
                    completed += 1

                # 每 10 只股票更新一次进度
                if i % 10 == 0:
                    with _download_lock:
                        if total > 0:
                            _download_state["progress"] = (completed * 100) // total

    except Exception as e:
        with _download_lock:
            _download_state["errors"].append(f"下载引擎异常: {e}")
        log.info(f"[下载] 引擎异常: {e}")
    finally:
        with _download_lock:
            _download_state["running"] = False
            _download_state["progress"] = 100
            _download_state["end_time"] = time.time()
        latest = _download_state.get("latest_data_date")
        log.error(f"[下载] 下载任务结束, 完成 {_download_state['completed_stocks']}/{_download_state['total_stocks']} 只, 错误 {len(_download_state['errors'])} 个, 最新数据日期 {latest}")
        if _download_state["errors"]:
            for err in _download_state["errors"][:5]:
                log.error(f"  [下载错误] {err}")
        # 检查最新数据日期，提示用户
        today_int = int(datetime.now().strftime("%Y%m%d"))
        if latest and latest < today_int:
            from chinese_calendar import is_workday
            if is_workday(datetime.now().date()):
                log.info(f"[下载] 提示: 服务器最新数据日期为 {latest}，今日({today_int})数据尚未更新，请稍后再试")
            else:
                log.info(f"[下载] 提示: 今日为非交易日，最新数据日期为 {latest}")


def _start_download(vipdoc_dir, categories, day_start=None, min_start=None):
    """启动后台下载线程"""
    global _download_state
    with _download_lock:
        if _download_state["running"]:
            return False, "下载任务已在运行中"

    thread = threading.Thread(
        target=_download_task,
        args=(vipdoc_dir, categories, day_start, min_start),
        daemon=True,
    )
    thread.start()
    return True, "下载已启动"


def _stop_download():
    """停止下载"""
    global _download_state
    with _download_lock:
        if not _download_state["running"]:
            return False, "没有正在运行的下载任务"
        _download_state["aborted"] = True
    return True, "正在停止下载..."


def _get_download_status():
    """获取下载状态"""
    global _download_state
    with _download_lock:
        status = dict(_download_state)
        # 将 latest_data_date (int 如 20260728) 转为可读字符串，并判断是否已到最新交易日
        latest_date_int = status.get("latest_data_date")
        if latest_date_int:
            y = latest_date_int // 10000
            m = (latest_date_int % 10000) // 100
            d = latest_date_int % 100
            status["latest_data_date_str"] = f"{y}-{m:02d}-{d:02d}"
            # 查找最新交易日
            today = datetime.now()
            check_date = today
            max_days_back = 10
            for _ in range(max_days_back):
                if check_date.weekday() < 5 and not is_holiday(check_date):
                    break
                check_date = check_date - timedelta(days=1)
            latest_td_int = check_date.year * 10000 + check_date.month * 100 + check_date.day
            status["latest_trading_day_str"] = check_date.strftime("%Y-%m-%d")
            status["data_is_latest"] = (latest_date_int >= latest_td_int)
        else:
            status["latest_data_date_str"] = None
            status["latest_trading_day_str"] = None
            status["data_is_latest"] = None
        return status


def collect_codes_from_vipdoc(vipdoc_dir):
    """
    从 vipdoc 目录下的 .day 文件名收集所有股票代码。
    适用于没有 shm.tnf/szm.tnf 的通达信普通版。
    返回 {code: {"name": "", "pinyin": "", "market": "sh/sz/hk"}} 字典。

    包含范围：
      A股: 主板(60xxxx/00xxxx)、创业板(30xxxx)、科创板(68xxxx)、北交所(8xxxxx/4xxxxx)
           指数(000001上证/399001深成指/399006创业板指 等 399xxx/000xxx指数)
           ETF(51xxxx沪市ETF/15xxxx深市ETF/159xxx)
      港股: ds/lday 目录下的 31#XXXXX.day 文件

    排除范围：
      债券(11xxxx/12xxxx/13xxxx沪市债券, 10xxxx/11xxxx/12xxxx深市债券)
      基金(50xxxx沪市封闭基金, 16xxxx/18xxxx深市基金)
      其他(1xxxxx北交所债券, 20xxxx/90xxxx B股, 395xxx通达信内部板块)
    """
    result = {}

    # === A股代码过滤规则 ===
    # 上海市场(sh)：包含
    sh_include_prefixes = ("60", "68")  # 主板60, 科创板68
    # 上海市场(sh)：排除（债券、基金、ETF等）
    sh_exclude_prefixes = ("11", "12", "13", "50", "51", "52", "53", "54", "55", "56", "57", "58", "59", "588", "90", "91", "92", "93", "94", "95", "96", "97", "98", "10", "00", "09")
    # 上海指数：000xxx 和 9xxxxx 是上证系列指数
    sh_index_prefixes = ("000", "9")

    # 深圳市场(sz)：包含
    sz_include_prefixes = ("00", "30", "39")  # 主板00, 创业板30, 指数39
    # 深圳市场(sz)：排除（债券、基金、ETF等）
    sz_exclude_prefixes = ("10", "11", "12", "13", "14", "15", "16", "17", "18", "20", "395")

    # 深圳指数：399xxx 是深市指数（如399001深成指、399006创业板指）
    sz_index_prefixes = ("399",)

    def _is_a_stock_code(code, mkt_dir):
        """判断是否为需要包含的A股代码"""
        if not code.isdigit() or len(code) != 6:
            return False

        if mkt_dir == "sh":
            # 上海指数：000xxx（上证系列指数）、9xxxxx
            if code.startswith(sh_index_prefixes):
                return True
            # 上海包含：主板60、科创板68、ETF 51/56/58/59/588
            if code.startswith(sh_include_prefixes):
                return True
            # 上海排除：债券、基金等
            if code.startswith(sh_exclude_prefixes):
                return False
            # 其他上海代码默认排除
            return False

        elif mkt_dir == "sz":
            # 深圳指数：399xxx
            if code.startswith(sz_index_prefixes):
                return True
            # 深圳包含：主板00、创业板30、ETF 15/16/18
            if code.startswith(sz_include_prefixes):
                return True
            # 深圳排除：债券、基金、通达信内部板块等
            if code.startswith(sz_exclude_prefixes):
                return False
            # 其他深圳代码默认排除
            return False

        return False

    # === 收集A股代码 ===
    sh_count = 0
    sz_count = 0
    for mkt_dir, prefix in [("sh", "sh"), ("sz", "sz")]:
        lday_dir = os.path.join(vipdoc_dir, mkt_dir, "lday")
        if not os.path.isdir(lday_dir):
            continue
        for fname in os.listdir(lday_dir):
            if fname.startswith(prefix) and fname.endswith(".day"):
                code = fname[len(prefix):-4]
                if _is_a_stock_code(code, mkt_dir):
                    compound_key = mkt_dir + code
                    if compound_key not in result:
                        result[compound_key] = {"name": "", "pinyin": "", "market": mkt_dir}
                        if mkt_dir == "sh":
                            sh_count += 1
                        else:
                            sz_count += 1

    # === 收集港股代码（ds目录）===
    hk_count = 0
    ds_lday_dir = os.path.join(vipdoc_dir, "ds", "lday")
    if os.path.isdir(ds_lday_dir):
        for fname in os.listdir(ds_lday_dir):
            if fname.startswith("31#") and fname.endswith(".day"):
                # 港股格式：31#00700.day
                code = fname[3:-4]  # 提取 00700
                if code.isdigit():
                    # 港股代码统一补前导零到5位
                    hk_code = code.zfill(5)
                    compound_key = "hk" + hk_code
                    if compound_key not in result:
                        result[compound_key] = {"name": "", "pinyin": "", "market": "hk"}
                        hk_count += 1

    return result

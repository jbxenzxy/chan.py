# -*- coding: utf-8 -*-
"""
15 分钟周期：合成正确性 + 红框链路 + 双副本一致性守卫
=====================================================================
针对 custom-dev 新增的「股票 15m 周期」写最小回归测试，锁定三类内容：

  1. 15m 合成（_resample_5m_to_15m）：A股 / 港股交易日的桶边界、OHLC/量/额聚合
     与一份「不依赖实现、只用规范」的参考再实现逐字段一致（防算法被改坏）。
  2. 红框链路（_stocks_red_range_algo + _get_date_fmt）：15m 主窗红框边界数学
     正确，且 _get_date_fmt("15m") 返回带时分格式（不带时分 = P1 红框错位根因）。
  3. 双副本一致性守卫（P1 直接防护）：App/utils.py 与 BuySellPoint/BSPointList.py
     两份 INTRADAY_FREQS 必须集合相等且都含 "15m"。缺任一副本的 "15m" 立即失败。
  4. 15m 双窗上窗一致性（P2）：后端配对表 / 前端配对表 / 前端入口按钮三者不得矛盾。
  5. 30m 分桶（顺带修掉的既有缺陷，v4.0 即存在、非 15m 引入）：港股 11:35~11:55 与
     15:35~15:55 曾被错并进上一个桶；断言修复后为 11 桶且成员正确，同时断言 A股
     30m 行为零回归，并守卫「30m 分桶逻辑只有一份实现」。

运行：python Test/test_15m_period.py
"""
import os
import sys
from datetime import datetime, timedelta

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

from BuySellPoint.BSPointList import (
    _stocks_red_range_algo,
    _get_date_fmt,
    INTRADAY_FREQS as BSL_INTRADAY_FREQS,
)
from App import utils as APP_UTILS


# ═══════════════════════════════════════════════════════════════════
# 工具：构造合成 5m 数据 + 规范参考合成
# ═══════════════════════════════════════════════════════════════════
def _make_5m_records(times):
    """按时间列表生成 5m 记录，第 i 根 bar 的 OHLC 全设为 i+1（互异，便于断言），
    vol=i+1，amount=(i+1)*10。"""
    recs = []
    for i, t in enumerate(times):
        v = i + 1
        recs.append({
            "dt": t,
            "open": float(v), "high": float(v), "low": float(v), "close": float(v),
            "vol": v, "amount": v * 10.0,
        })
    return recs


def _trading_minutes_ashare(day):
    """A股一个交易日 5m 结束时刻：09:35..11:30 与 13:05..15:00。"""
    out = []
    t = datetime(day.year, day.month, day.day, 9, 35)
    end = datetime(day.year, day.month, day.day, 11, 30)
    while t <= end:
        out.append(t); t += timedelta(minutes=5)
    t = datetime(day.year, day.month, day.day, 13, 5)
    end = datetime(day.year, day.month, day.day, 15, 0)
    while t <= end:
        out.append(t); t += timedelta(minutes=5)
    return out


def _trading_minutes_hk(day):
    """港股一个交易日 5m 结束时刻：09:35..12:00 与 13:05..16:00。"""
    out = []
    t = datetime(day.year, day.month, day.day, 9, 35)
    end = datetime(day.year, day.month, day.day, 12, 0)
    while t <= end:
        out.append(t); t += timedelta(minutes=5)
    t = datetime(day.year, day.month, day.day, 13, 5)
    end = datetime(day.year, day.month, day.day, 16, 0)
    while t <= end:
        out.append(t); t += timedelta(minutes=5)
    return out


def _expected_15m_buckets(day, session_ranges, period_min=15):
    """规范桶列表：每个 (start_h,start_m,end_h,end_m) 区间内从 09:30 锚点起的 period_min 边界。"""
    buckets = []
    for sh, sm, eh, em in session_ranges:
        t = datetime(day.year, day.month, day.day, sh, sm)
        end = datetime(day.year, day.month, day.day, eh, em)
        while t <= end:
            buckets.append(t); t += timedelta(minutes=period_min)
    return buckets


def _ref_resample(records, bucket_dts, period_min=15):
    """规范参考合成：桶 bdt 收集满足 (bdt-period_min, bdt] 的 5m bar，
    不使用被测试的实现，仅依据「桶=结束时刻对齐 09:30 锚点的 period_min 区间」规范。"""
    out = []
    for bdt in bucket_dts:
        lo = bdt - timedelta(minutes=period_min)
        members = [r for r in records if lo < r["dt"] <= bdt]
        if not members:
            continue
        out.append({
            "dt": bdt,
            "open": members[0]["open"],
            "close": members[-1]["close"],
            "high": max(m["high"] for m in members),
            "low": min(m["low"] for m in members),
            "vol": sum(m["vol"] for m in members),
            "amount": sum(m["amount"] for m in members),
        })
    return out


# ═══════════════════════════════════════════════════════════════════
# 1. 15m 合成正确性（A股 / 港股）
# ═══════════════════════════════════════════════════════════════════
def test_resample_15m_ashare(failures):
    """A股一个交易日 48 根 5m → 16 根 15m；桶边界与聚合与规范一致。"""
    from DataAPI.TdxAPI import _resample_5m_to_15m
    day = datetime(2024, 1, 2)
    times = _trading_minutes_ashare(day)
    recs = _make_5m_records(times)
    expected_buckets = _expected_15m_buckets(
        day, [(9, 45, 11, 30), (13, 15, 15, 0)])

    result = _resample_5m_to_15m([dict(r) for r in recs], market="sh")
    ref = _ref_resample(recs, expected_buckets)

    if len(result) != len(ref):
        failures.append(f"A股桶数: 期望 {len(ref)}，实际 {len(result)}")
        print(f"[FAIL] A股桶数: 期望 {len(ref)}，实际 {len(result)}")
        return
    ok = True
    for r, e in zip(result, ref):
        if r["dt"] != e["dt"]:
            failures.append(f"A股桶序: 期望 {e['dt']}，实际 {r['dt']}"); ok = False; break
        for k in ("open", "close", "high", "low", "vol", "amount"):
            if abs(r[k] - e[k]) > 1e-6:
                failures.append(f"A股桶 {e['dt']} {k}: 期望 {e[k]}，实际 {r[k]}")
                ok = False
    if ok:
        print(f"[PASS] 15m 合成(A股): {len(result)} 桶，OHLC/量/额逐字段一致")


def test_resample_15m_hk(failures):
    """港股一个交易日 66 根 5m → 22 根 15m；桶边界与聚合与规范一致。"""
    from DataAPI.TdxAPI import _resample_5m_to_15m
    day = datetime(2024, 1, 2)
    times = _trading_minutes_hk(day)
    recs = _make_5m_records(times)
    expected_buckets = _expected_15m_buckets(
        day, [(9, 45, 12, 0), (13, 15, 16, 0)])

    result = _resample_5m_to_15m([dict(r) for r in recs], market="hk")
    ref = _ref_resample(recs, expected_buckets)

    if len(result) != len(ref):
        failures.append(f"港股桶数: 期望 {len(ref)}，实际 {len(result)}")
        print(f"[FAIL] 港股桶数: 期望 {len(ref)}，实际 {len(result)}")
        return
    ok = True
    for r, e in zip(result, ref):
        if r["dt"] != e["dt"]:
            failures.append(f"港股桶序: 期望 {e['dt']}，实际 {r['dt']}"); ok = False; break
        for k in ("open", "close", "high", "low", "vol", "amount"):
            if abs(r[k] - e[k]) > 1e-6:
                failures.append(f"港股桶 {e['dt']} {k}: 期望 {e[k]}，实际 {r[k]}")
                ok = False
    if ok:
        print(f"[PASS] 15m 合成(港股): {len(result)} 桶，OHLC/量/额逐字段一致")


def test_resample_15m_oos_boundary(failures):
    """边界(P3 文档化): 交易时段外 09:25 的 5m bar 会被并入 09:30 桶。
    锁定当前行为，若未来调整合成锚点会立即告警。"""
    from DataAPI.TdxAPI import _resample_5m_to_15m
    day = datetime(2024, 1, 2)
    times = [datetime(day.year, day.month, day.day, 9, 25),
             datetime(day.year, day.month, day.day, 9, 35)]
    recs = _make_5m_records(times)
    result = _resample_5m_to_15m([dict(r) for r in recs], market="sh")
    first_dt = result[0]["dt"]
    if first_dt != datetime(day.year, day.month, day.day, 9, 30):
        failures.append(f"时段外边界: 期望首个 15m 桶 09:30，实际 {first_dt}")
        print(f"[FAIL] 时段外边界: 期望 09:30，实际 {first_dt}")
    else:
        print(f"[PASS] 15m 时段外边界: 09:25 bar 并入 09:30 桶（已文档化）")


# ═══════════════════════════════════════════════════════════════════
# 2. 红框链路 15m
# ═══════════════════════════════════════════════════════════════════
def test_red_range_15m_intraday(failures):
    """15m 主窗 + 5m 子窗：左界 = A - (15m-5m) = A-600s，右界 = B。"""
    c, d = _stocks_red_range_algo("2024/01/02 11:15", "2024/01/02 11:30", "15m", "5m")
    if c != "2024/01/02 11:05":
        failures.append(f"15m 红框左界: 期望 2024/01/02 11:05，实际 {c}")
        print(f"[FAIL] 15m 红框左界: 期望 2024/01/02 11:05，实际 {c}")
    elif d != "2024/01/02 11:30":
        failures.append(f"15m 红框右界: 期望 2024/01/02 11:30，实际 {d}")
        print(f"[FAIL] 15m 红框右界: 期望 2024/01/02 11:30，实际 {d}")
    else:
        print(f"[PASS] 15m 红框(11:15→11:30): C={c}, D={d}")


def test_get_date_fmt_15m(failures):
    """_get_date_fmt("15m") 必须带时分（%Y/%m/%d %H:%M）。
    若退化为纯日期 %Y/%m/%d，则红框字符串会把主级别时间截断为整日 → P1 错位。"""
    fmt = _get_date_fmt("15m")
    if fmt != "%Y/%m/%d %H:%M":
        failures.append(f"_get_date_fmt(15m): 期望 %Y/%m/%d %H:%M，实际 {fmt!r}")
        print(f"[FAIL] _get_date_fmt(15m): 期望 '%Y/%m/%d %H:%M'，实际 {fmt!r}")
    else:
        print(f"[PASS] _get_date_fmt(15m) = {fmt!r}（带时分，红框不会错位）")


# ═══════════════════════════════════════════════════════════════════
# 3. 双副本一致性守卫（P1 直接防护）
# ═══════════════════════════════════════════════════════════════════
def test_intraday_freqs_dual_copy(failures):
    """App/utils.py 与 BuySellPoint/BSPointList.py 两份 INTRADAY_FREQS 必须集合相等，
    且都含 "15m"。缺任一副本的 "15m" → 15m 主窗红框错位（P1 根因）。"""
    a = APP_UTILS.INTRADAY_FREQS
    b = BSL_INTRADAY_FREQS
    if a != b:
        failures.append(f"INTRADAY_FREQS 双副本不一致: App={a} vs BSPointList={b}")
        print(f"[FAIL] INTRADAY_FREQS 双副本不一致: App={a} vs BSPointList={b}")
        return
    if "15m" not in a:
        failures.append("INTRADAY_FREQS 两份副本均缺 '15m'（P1 根因）")
        print("[FAIL] INTRADAY_FREQS 两份副本均缺 '15m'（P1 根因）")
        return
    print(f"[PASS] INTRADAY_FREQS 双副本一致且含 15m: {sorted(a)}")


# ═══════════════════════════════════════════════════════════════════
# 4. 15m 双窗上窗一致性（P2 矛盾点守卫）
# ═══════════════════════════════════════════════════════════════════
def test_dual_window_15m_consistency(failures):
    """15m 作双窗上窗：后端配对、前端配对表/入口必须一致。
    后端 _STOCKS_DUAL_PAIRS['15m']=={'5m'} 且前端 STOCK_DUAL_PAIRS_JS 含 '15m':['5m']；
    前端 updateDualBtn 不得再禁用 15m（否则与后端/配对表/设计文档手测项 4 矛盾）。"""
    from App import AppEngine as m
    if m._STOCKS_DUAL_PAIRS.get("15m") != {"5m"}:
        failures.append(f"后端 _STOCKS_DUAL_PAIRS['15m'] 应为 {{'5m'}}，实际 {m._STOCKS_DUAL_PAIRS.get('15m')}")
        print(f"[FAIL] 后端双窗配对 15m: 实际 {m._STOCKS_DUAL_PAIRS.get('15m')}")
        return
    js_path = os.path.join(REPO_ROOT, "Frontend", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()
    # 后端支持 15m 上窗 → 前端入口不得再禁用它（P2 矛盾点）
    if "disabled = (currentFreq === '15m'" in js or "disabled = (currentFreq === '15m' ||" in js:
        failures.append("前端 updateDualBtn 仍禁用 15m 双窗（与后端/配对表/设计文档矛盾）")
        print("[FAIL] 前端 updateDualBtn 仍禁用 15m 双窗")
        return
    if "'15m': ['5m']" not in js:
        failures.append("前端 STOCK_DUAL_PAIRS_JS 未含 '15m': ['5m']")
        print("[FAIL] 前端 STOCK_DUAL_PAIRS_JS 未含 15m 配对")
        return
    print("[PASS] 15m 双窗上窗: 后端配对 + 前端配对表/入口 一致（未被禁用）")


# ═══════════════════════════════════════════════════════════════════
# 5. 30m 分桶（既有缺陷修复 + A股零回归 + 单一事实源守卫）
# ═══════════════════════════════════════════════════════════════════
def _old_bucket_30min_hk(dt_obj):
    """v4.0 / custom-dev 的旧港股 30m 分桶实现，原样保留作为「行为基线」。
    仅供测试对比用：证明修复只改动了确实出错的时段。"""
    h, m = dt_obj.hour, dt_obj.minute
    if h == 9:
        return dt_obj.replace(minute=0, hour=10)
    elif h == 10:
        return dt_obj.replace(minute=0, hour=10) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=11)
    elif h == 11:
        return dt_obj.replace(minute=0, hour=11) if m == 0 else dt_obj.replace(minute=30)
    elif h == 12:
        return dt_obj.replace(minute=0)
    elif h == 13:
        return dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=14)
    elif h == 14:
        return dt_obj.replace(minute=0, hour=14) if m == 0 else dt_obj.replace(minute=30) if m < 35 else dt_obj.replace(minute=0, hour=15)
    elif h == 15:
        return dt_obj.replace(minute=0, hour=15) if m == 0 else dt_obj.replace(minute=30)
    elif h == 16:
        return dt_obj.replace(minute=0)
    return dt_obj


def test_resample_30m_ashare_no_regression(failures):
    """A股 30m：新锚点法结果必须与规范参考完全一致，
    且桶列表仍是 10:00..15:00 共 8 桶（证明修港股 bug 未动 A股行为）。"""
    from DataAPI.TdxAPI import _resample_5m_to_30m
    day = datetime(2024, 1, 2)
    recs = _make_5m_records(_trading_minutes_ashare(day))
    expected_buckets = _expected_15m_buckets(
        day, [(10, 0, 11, 30), (13, 30, 15, 0)], period_min=30)

    result = _resample_5m_to_30m([dict(r) for r in recs], market="sh")
    ref = _ref_resample(recs, expected_buckets, period_min=30)

    labels = [f"{r['dt']:%H:%M}" for r in result]
    want = ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
    if labels != want:
        failures.append(f"A股 30m 桶列表: 期望 {want}，实际 {labels}")
        print(f"[FAIL] A股 30m 桶列表: 实际 {labels}")
        return
    ok = True
    for r, e in zip(result, ref):
        for k in ("open", "close", "high", "low", "vol", "amount"):
            if abs(r[k] - e[k]) > 1e-6:
                failures.append(f"A股 30m 桶 {e['dt']} {k}: 期望 {e[k]}，实际 {r[k]}")
                ok = False
    if ok:
        print(f"[PASS] 30m 合成(A股): {len(result)} 桶 10:00..15:00，与规范逐字段一致（零回归）")


def test_resample_30m_hk_bucket_fix(failures):
    """港股 30m：旧实现把 11:35~11:55 错并进 11:30 桶、15:35~15:55 错并进 15:30 桶
    （v4.0 即已存在，非 15m 引入）。修复后必须为 11 桶、每桶 6 根，
    12:00 桶 = 11:35..12:00，16:00 桶 = 15:35..16:00。"""
    from DataAPI.TdxAPI import _resample_5m_to_30m
    day = datetime(2024, 1, 2)
    times = _trading_minutes_hk(day)
    recs = _make_5m_records(times)
    result = _resample_5m_to_30m([dict(r) for r in recs], market="hk")

    labels = [f"{r['dt']:%H:%M}" for r in result]
    want = ["10:00", "10:30", "11:00", "11:30", "12:00",
            "13:30", "14:00", "14:30", "15:00", "15:30", "16:00"]
    if labels != want:
        failures.append(f"港股 30m 桶列表: 期望 {want}，实际 {labels}")
        print(f"[FAIL] 港股 30m 桶列表: 实际 {labels}")
        return

    # 用规范参考校验每桶聚合，并断言修复时段成员正确
    expected_buckets = _expected_15m_buckets(
        day, [(10, 0, 12, 0), (13, 30, 16, 0)], period_min=30)
    ref = _ref_resample(recs, expected_buckets, period_min=30)
    ok = True
    for r, e in zip(result, ref):
        if r["dt"] != e["dt"]:
            failures.append(f"港股 30m 桶序: 期望 {e['dt']}，实际 {r['dt']}"); ok = False; break
        for k in ("open", "close", "high", "low", "vol", "amount"):
            if abs(r[k] - e[k]) > 1e-6:
                failures.append(f"港股 30m 桶 {e['dt']} {k}: 期望 {e[k]}，实际 {r[k]}")
                ok = False
    if not ok:
        return

    # 新实现每桶必须恰好 6 根 5m
    new_size = {}
    for r in result:
        lo = r["dt"] - timedelta(minutes=30)
        new_size[f"{r['dt']:%H:%M}"] = sum(1 for t in times if lo < t <= r["dt"])
    bad = {k: v for k, v in new_size.items() if v != 6}
    if bad:
        failures.append(f"港股 30m 每桶应含 6 根 5m，异常桶={bad}")
        print(f"[FAIL] 港股 30m 每桶根数异常: {bad}")
        return

    # 基线自检：旧实现并非漏桶，而是「错并」——11:30 桶吞掉 11 根、12:00 桶只剩 1 根。
    # 若基线不再复现该畸形分布，说明基线抄错了，本测试将失去意义。
    old_size = {}
    for t in times:
        k = f"{_old_bucket_30min_hk(t):%H:%M}"
        old_size[k] = old_size.get(k, 0) + 1
    if old_size.get("11:30") != 11 or old_size.get("12:00") != 1 \
            or old_size.get("15:30") != 11 or old_size.get("16:00") != 1:
        failures.append(f"旧港股分桶基线未复现出缺陷分布，测试基线可能已失效: {old_size}")
        print(f"[FAIL] 旧港股分桶基线未复现缺陷分布: {old_size}")
        return
    print(f"[PASS] 30m 合成(港股): 11 桶且每桶 6 根 "
          f"（旧实现畸形分布 11:30={old_size['11:30']}根/12:00={old_size['12:00']}根、"
          f"15:30={old_size['15:30']}根/16:00={old_size['16:00']}根 已修复）")


def test_30m_bucket_single_source(failures):
    """单一事实源守卫：30m 分桶逻辑只能存在于 _resample_5m_to_30m 一处。
    旧代码在 read_tdx_5min_file 内联了一份同名 _bucket_30min，导致同一 bug 两处并存、
    修一处另一处仍错。此处断言源码中不再出现 _bucket_30min。"""
    src_path = os.path.join(REPO_ROOT, "DataAPI", "TdxAPI.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    n = src.count("_bucket_30min")
    # 允许注释中提及（说明历史），但不得有 def / 调用形式
    if "def _bucket_30min" in src:
        failures.append(f"TdxAPI.py 仍存在 _bucket_30min 定义（重复副本风险），出现 {n} 次")
        print(f"[FAIL] TdxAPI.py 仍定义 _bucket_30min（出现 {n} 次）")
        return
    if "_bucket_30min(" in src:
        failures.append("TdxAPI.py 仍调用 _bucket_30min（重复副本风险）")
        print("[FAIL] TdxAPI.py 仍调用 _bucket_30min")
        return
    if "_resample_5m_to_30m(records, market)" not in src:
        failures.append("read_tdx_5min_file 未委托到 _resample_5m_to_30m（30m 逻辑可能又被内联）")
        print("[FAIL] read_tdx_5min_file 未委托到 _resample_5m_to_30m")
        return
    print("[PASS] 30m 分桶单一事实源: read_tdx_5min_file 委托 _resample_5m_to_30m，无内联副本")


def test_resample_single_implementation(failures):
    """重采样单一实现守卫：15m/30m 合成必须共用同一个 _resample_5m(period_min)。
    判据：① 09:30 锚点分桶算术在源码中只出现 1 次；
          ② _resample_5m_to_15m / _resample_5m_to_30m 均为委托薄封装。
    历史：这两个函数曾是逐行相同的两份拷贝（仅 ceil 常数与临时键名不同），
    加上 read_tdx_5min_file 内联的第三份，正是港股 30m 分桶 bug 修不干净的原因。"""
    src_path = os.path.join(REPO_ROOT, "DataAPI", "TdxAPI.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    n_anchor = src.count("minutes_since = int((dt - ref).total_seconds()) // 60")
    if n_anchor != 1:
        failures.append(f"锚点分桶算术应只出现 1 次（单一实现），实际 {n_anchor} 次")
        print(f"[FAIL] 锚点分桶算术出现 {n_anchor} 次（应为 1，重复实现风险）")
        return
    for name, period in (("_resample_5m_to_15m", 15), ("_resample_5m_to_30m", 30)):
        if f"return _resample_5m(records, {period})" not in src:
            failures.append(f"{name} 未委托 _resample_5m(records, {period})")
            print(f"[FAIL] {name} 未委托到 _resample_5m")
            return
    print(f"[PASS] 重采样单一实现: 锚点算术仅 1 处，15m/30m 均委托 _resample_5m(period_min)")


def test_resample_period_parity(failures):
    """参数化等价性：合并后 _resample_5m 对 (15m,30m)×(A股,港股) 四种组合，
    每个桶都必须恰含 period_min/5 根 5m，且桶数符合各市场交易时长。"""
    from DataAPI.TdxAPI import _resample_5m
    day = datetime(2024, 1, 2)
    cases = [
        ("A股", _trading_minutes_ashare(day), 15, 16),
        ("A股", _trading_minutes_ashare(day), 30, 8),
        ("港股", _trading_minutes_hk(day), 15, 22),
        ("港股", _trading_minutes_hk(day), 30, 11),
    ]
    ok = True
    for mkt, times, period, want_buckets in cases:
        recs = _make_5m_records(times)
        res = _resample_5m([dict(r) for r in recs], period)
        if len(res) != want_buckets:
            failures.append(f"{mkt} {period}m 桶数: 期望 {want_buckets}，实际 {len(res)}")
            print(f"[FAIL] {mkt} {period}m 桶数: 期望 {want_buckets}，实际 {len(res)}")
            ok = False
            continue
        want_per = period // 5
        for r in res:
            lo = r["dt"] - timedelta(minutes=period)
            cnt = sum(1 for t in times if lo < t <= r["dt"])
            if cnt != want_per:
                failures.append(f"{mkt} {period}m 桶 {r['dt']:%H:%M} 含 {cnt} 根（期望 {want_per}）")
                print(f"[FAIL] {mkt} {period}m 桶 {r['dt']:%H:%M} 含 {cnt} 根（期望 {want_per}）")
                ok = False
    if ok:
        print("[PASS] 参数化等价性: (15m,30m)×(A股,港股) 桶数正确、每桶根数恰为 period/5")


# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════
def main():
    failures = []
    test_resample_15m_ashare(failures)
    test_resample_15m_hk(failures)
    test_resample_15m_oos_boundary(failures)
    test_red_range_15m_intraday(failures)
    test_get_date_fmt_15m(failures)
    test_intraday_freqs_dual_copy(failures)
    test_dual_window_15m_consistency(failures)
    test_resample_30m_ashare_no_regression(failures)
    test_resample_30m_hk_bucket_fix(failures)
    test_30m_bucket_single_source(failures)
    test_resample_single_implementation(failures)
    test_resample_period_parity(failures)

    print()
    if failures:
        print(f"===== 15m 周期回归: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== 15m 周期回归: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

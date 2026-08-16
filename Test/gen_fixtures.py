# -*- coding: utf-8 -*-
"""
阶段 2.5：回归测试基线 —— fixtures 生成器
=====================================================================
生成确定性的合成 K 线 fixtures（冻结输入），供快照回归测试使用。

设计要点：
  1. 确定性：固定 seed，任何时候重跑生成完全一致的数据（基线可复现）
  2. 结构性：注入清晰的「趋势 → 盘整 → 趋势 → 跳空」段落，
     使引擎产生非平凡的 笔 / 线段 / 中枢 / 买卖点
  3. 真实性贴近：交易日历跳过周末、开高低收的次序约束、量额非负
  4. record 格式与 my_chan_main.read_main_level_records 输出一致：
     {"dt": datetime, "open": float, "high": float, "low": float,
      "close": float, "vol": float, "amount": float}

用法：
    python Test/gen_fixtures.py           # 生成/覆盖全部 fixtures
    python Test/gen_fixtures.py --check   # 校验现有 fixtures 与生成器一致（防手改漂移）

在用户真实环境（有 TDX vipdoc 数据）可扩展：从真实数据导出代表性样本
替换合成数据（见 README.md「真实数据冻结」一节），快照机制不变。
"""
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# 所有 fixtures 的生成参数（单一事实源，写入 manifest）
MANIFEST = {
    "version": 1,
    "generator_seed": 20250816,
    "note": "确定性合成K线；重跑 gen_fixtures.py 结果必须与此 manifest 的 sha256 一致",
}


# ─────────────────────────────────────────────────────────────
# 交易日历工具
# ─────────────────────────────────────────────────────────────
def trading_days(start: datetime, n: int, skip_days=()):
    """从 start 开始生成 n 个交易日（跳过周末与 skip_days），方向向后"""
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5 and d.date() not in skip_days:
            days.append(d)
        d += timedelta(days=1)
    return days


def trading_minutes_60(start: datetime, n: int):
    """生成 n 根 60 分钟 K 线的时间序列（交易日 10:30 / 11:30 / 14:00 / 15:00，贴近 A 股节奏）"""
    times, d = [], start
    hours = [10, 11, 14, 15]
    minutes = {10: 30, 11: 30, 14: 0, 15: 0}
    while len(times) < n:
        if d.weekday() < 5:
            for h in hours:
                if len(times) >= n:
                    break
                times.append(d.replace(hour=h, minute=minutes[h], second=0, microsecond=0))
        d += timedelta(days=1)
    return times


# ─────────────────────────────────────────────────────────────
# 结构化行情合成
# ─────────────────────────────────────────────────────────────
def _make_bar(dt, open_, high, low, close, vol):
    return {
        "dt": dt,
        "open": round(open_, 2),
        "high": round(high, 2),
        "low": round(low, 2),
        "close": round(close, 2),
        "vol": round(vol, 2),
        "amount": round(vol * close, 2),
    }


def synth_daily(seed: int, n_days: int):
    """合成日线：四个明确段落
       A 上升趋势 35% → B 宽幅盘整 25% → C 下降趋势 25% → D 缓升+跳空 15%
       盘整段落保证中枢产生；趋势段落保证笔/段/一二三类买卖点产生。
    """
    rng = random.Random(seed)
    bars = []
    price = 10.0
    n_a, n_b, n_c = int(n_days * 0.35), int(n_days * 0.25), int(n_days * 0.25)
    days = trading_days(datetime(2023, 1, 3), n_days)

    for i, dt in enumerate(days):
        if i < n_a:                      # A：上升
            drift, sigma, vol = +0.012, 0.020, 1.0 + rng.random()
        elif i < n_a + n_b:              # B：宽幅盘整（中枢）
            drift, sigma, vol = 0.0, 0.030, 0.8 + rng.random() * 0.5
        elif i < n_a + n_b + n_c:        # C：下降
            drift, sigma, vol = -0.011, 0.018, 0.9 + rng.random() * 0.6
        else:                            # D：缓升 + 一次向上跳空
            drift, sigma, vol = +0.006, 0.015, 0.7 + rng.random() * 0.4

        gap = 1.04 if (i == n_a + n_b + n_c + max(2, (n_days - n_a - n_b - n_c) // 2)) else 1.0
        open_ = price * gap
        close = open_ * math.exp(drift + rng.gauss(0, sigma))
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.006)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.006)))
        bars.append(_make_bar(dt, open_, high, low, close, vol * 10000))
        price = close
    return bars


def synth_60m(seed: int, n_bars: int):
    """合成 60 分钟线（多级别联立用）：日线节奏的子级别噪声版"""
    rng = random.Random(seed)
    bars = []
    price = 10.0
    times = trading_minutes_60(datetime(2023, 1, 3), n_bars)
    trend = +0.002
    for i, dt in enumerate(times):
        if i % 240 == 0:                 # 每 240 根切换趋势（约 60 个交易日）
            trend = [+0.0025, 0.0, -0.0022, +0.0015][rng.randrange(4)]
        open_ = price
        close = open_ * math.exp(trend + rng.gauss(0, 0.006))
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.002)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.002)))
        bars.append(_make_bar(dt, open_, high, low, close, 3000 + rng.random() * 2000))
        price = close
    return bars


def synth_futures_15s(seed: int, n_bars: int):
    """合成期货 15 秒 K 线（螺纹钢主力量级，价格 ~3500，价格 3 位小数）：
       贴近 rb 交易时段（白盘 9:00-10:15/10:30-11:30/13:30-15:00 + 夜盘 21:00-23:00），
       结构同日线：上升 → 盘整 → 下降 → 缓升，保证笔/中枢/买卖点非平凡。
       天勤期货 record 无成交额：amount=0 占位（与 DataAPI/TqSdkAPI 口径一致）。"""
    rng = random.Random(seed)
    bars = []
    price = 3500.0
    # 预生成交易时刻（15s 一根），跨 n_bars 个 bar
    times, d = [], datetime(2024, 6, 3)
    sessions = [((9, 0), (10, 15)), ((10, 30), (11, 30)),
                ((13, 30), (15, 0)), ((21, 0), (23, 0))]
    while len(times) < n_bars:
        if d.weekday() < 5:                      # 跳过周末（夜盘归属当日）
            for (sh, sm), (eh, em) in sessions:
                t = d.replace(hour=sh, minute=sm, second=0, microsecond=0)
                end = d.replace(hour=eh, minute=em, second=0, microsecond=0)
                while t < end and len(times) < n_bars:
                    times.append(t)
                    t += timedelta(seconds=15)
        d += timedelta(days=1)

    n_a, n_b = int(n_bars * 0.35), int(n_bars * 0.25)
    for i, dt in enumerate(times):
        if i < n_a:                              # A：上升
            drift, sigma, vol = +0.00035, 0.0009, 800 + int(rng.random() * 600)
        elif i < n_a + n_b:                      # B：宽幅盘整（中枢）
            drift, sigma, vol = 0.0, 0.0013, 600 + int(rng.random() * 500)
        elif i < n_a + n_b + int(n_bars * 0.25): # C：下降
            drift, sigma, vol = -0.00032, 0.0008, 700 + int(rng.random() * 500)
        else:                                    # D：缓升
            drift, sigma, vol = +0.00018, 0.0007, 500 + int(rng.random() * 400)
        open_ = price
        close = open_ * math.exp(drift + rng.gauss(0, sigma))
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.0003)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.0003)))
        bars.append({
            "dt": dt,
            "open": round(open_, 3),
            "high": round(high, 3),
            "low": round(low, 3),
            "close": round(close, 3),
            "vol": vol,
            "amount": 0,
        })
        price = close
    return bars


def inject_gap(bars, idx_from_end: int):
    """模拟停牌：从倒数第 idx_from_end 根起删除 5 根（数据缺失边界用）"""
    cut = len(bars) - idx_from_end
    return bars[:cut] + bars[cut + 5:]


def inject_zero_vol(bars, idx_from_end: int):
    """模拟停牌日零成交：单根 vol=0、ohlc 收敛为前收盘"""
    b = [dict(x) for x in bars]
    i = len(b) - idx_from_end
    pc = b[i - 1]["close"]
    b[i].update(open=pc, high=pc, low=pc, close=pc, vol=0.0, amount=0.0)
    return b


# ─────────────────────────────────────────────────────────────
# 序列化（datetime → ISO 字符串，冻结文件为纯 JSON）
# ─────────────────────────────────────────────────────────────
def dump_records(bars):
    out = []
    for b in bars:
        r = dict(b)
        r["dt"] = b["dt"].strftime("%Y-%m-%d %H:%M:%S")
        out.append(r)
    return out


def load_records(path):
    """读回 fixtures：dt 还原为 datetime（与 read_main_level_records 输出同构）"""
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    for r in rows:
        r["dt"] = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S")
    return rows


def _write(name, obj):
    path = os.path.join(FIXTURE_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    return path


def generate_all():
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    seed = MANIFEST["generator_seed"]
    daily = synth_daily(seed, 500)
    m60 = synth_60m(seed + 1, 2000)   # 500 交易日 × 4 根/日：与日线同时间窗（跨级别对齐前提）
    f15 = synth_futures_15s(seed + 2, 2400)
    files = {
        "stock_day.json": dump_records(daily),
        "stock_60m.json": dump_records(m60),
        "futures_15s.json": dump_records(f15),
        "stock_day_gap.json": dump_records(inject_gap([dict(x) for x in daily], 60)),
        "stock_day_zero_vol.json": dump_records(inject_zero_vol([dict(x) for x in daily], 30)),
    }
    for name, obj in files.items():
        p = _write(name, obj)
        print(f"[gen] {name}: {len(obj)} bars")
    _write("fixtures_manifest.json", MANIFEST)
    print("[gen] fixtures_manifest.json")


if __name__ == "__main__":
    if "--check" in sys.argv:
        # 校验现有 fixtures 未被手改（重生成到临时目录并比对）
        import tempfile
        import filecmp
        with tempfile.TemporaryDirectory() as td:
            FIXTURE_DIR = td
            generate_all()
            real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
            ok = True
            for f in os.listdir(td):
                if not filecmp.cmp(os.path.join(td, f), os.path.join(real, f), shallow=False):
                    print(f"[CHECK-FAIL] {f} 与生成器输出不一致")
                    ok = False
        print("[CHECK] 全部一致" if ok else "[CHECK] 存在漂移，请重新生成并重冻结快照")
        sys.exit(0 if ok else 1)
    generate_all()

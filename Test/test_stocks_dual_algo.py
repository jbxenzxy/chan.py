# -*- coding: utf-8 -*-
"""
P0 纯函数 4 向单测：股票双窗独立化「方向 × 边界」公式锁定
=====================================================================
改造报告 §8 P0 要求：抽出 _stocks_sub_dt_algo（下窗截断边界）与
_stocks_red_range_algo（红框边界）纯函数后，针对「方向」与「边界」
两个错误维度写 4 个最小单测（开始/结束 × 左/右），锁定公式方向，
防止后续人改坏（尤其防止照搬期货「开始时间」公式导致整体偏移）。

覆盖对象：
  1. _stocks_sub_dt_algo   （AppEngine.py:1612）下窗截断边界
  2. _stocks_red_range_algo（BSPointList.py:1845）红框边界

设计要点（与期货公式镜像对称）：
  · 股票 K 线 dt = 结束时间；期货 dt = 开始时间（两者相反）
  · 日内主级别（30m）：左界 = A - (main_sec - sub_sec)，右界 = B
  · 日期型主级别（w/d）：左界 = 覆盖期首日 00:00，右界 = 右肩当日 23:59:59
  · 下窗截断：right = 上窗末根结束时刻；left = 上窗首根开始时刻

运行：python Test/test_stocks_dual_algo.py
"""
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

from datetime import datetime, timedelta


# ═══════════════════════════════════════════════════════════════════
# 1. _stocks_sub_dt_algo —— 下窗截断边界（P0）
# ═══════════════════════════════════════════════════════════════════
def test_sub_dt_algo_intraday(failures):
    """日内主级别（30m + 5m）：左界 = 首根开始时刻，右界 = 末根结束时刻。

    例：上窗 30m 首根 dt=11:30（结束时间，覆盖 (11:00,11:30]），
        末根 dt=14:30（覆盖 (14:00,14:30]）。
        期望：left = 11:00（首根开始），right = 14:30（末根结束）。
    """
    from App import AppEngine as m
    first = datetime(2024, 1, 2, 11, 30)
    last = datetime(2024, 1, 2, 14, 30)
    left, right = m._stocks_sub_dt_algo(first, last, "30m", "5m")
    if left != datetime(2024, 1, 2, 11, 0):
        failures.append(f"日内左界: 期望 11:00，实际 {left}")
        print(f"[FAIL] 日内左界: 期望 11:00，实际 {left}")
    elif right != datetime(2024, 1, 2, 14, 30):
        failures.append(f"日内右界: 期望 14:30，实际 {right}")
        print(f"[FAIL] 日内右界: 期望 14:30，实际 {right}")
    else:
        print(f"[PASS] 日内截断(30m+5m): left={left} right={right}")


def test_sub_dt_algo_daily(failures):
    """日期型主级别（d + 30m）：dt(00:00) 按当日结束时刻补齐。

    例：上窗 d 首根 dt=2024/01/02 00:00，末根 dt=2024/01/05 00:00。
        期望：left = 2024/01/01 23:59:59.999999（首根开始=前一日收盘后），
              right = 2024/01/05 23:59:59.999999（末根当日收盘）。
    """
    from App import AppEngine as m
    first = datetime(2024, 1, 2)
    last = datetime(2024, 1, 5)
    left, right = m._stocks_sub_dt_algo(first, last, "d", "30m")
    exp_left = datetime(2024, 1, 1, 23, 59, 59, 999999)
    exp_right = datetime(2024, 1, 5, 23, 59, 59, 999999)
    if left != exp_left:
        failures.append(f"日线左界: 期望 {exp_left}，实际 {left}")
        print(f"[FAIL] 日线左界: 期望 {exp_left}，实际 {left}")
    elif right != exp_right:
        failures.append(f"日线右界: 期望 {exp_right}，实际 {right}")
        print(f"[FAIL] 日线右界: 期望 {exp_right}，实际 {right}")
    else:
        print(f"[PASS] 日线截断(d+30m): left={left} right={right}")


def test_sub_dt_algo_unknown_freq(failures):
    """未知主级别周期 → (None, None)，调用方退化为不过滤。"""
    from App import AppEngine as m
    left, right = m._stocks_sub_dt_algo(
        datetime(2024, 1, 2), datetime(2024, 1, 5), "1m", "5m")
    if left is not None or right is not None:
        failures.append(f"未知周期: 期望 (None,None)，实际 ({left},{right})")
        print(f"[FAIL] 未知周期: 期望 (None,None)，实际 ({left},{right})")
    else:
        print("[PASS] 未知周期: 返回 (None, None) 退化不过滤")


# ═══════════════════════════════════════════════════════════════════
# 2. _stocks_red_range_algo —— 红框边界（P3，与 P0 同口径锁定）
# ═══════════════════════════════════════════════════════════════════
def test_red_range_algo_intraday(failures):
    """日内主级别（30m + 5m）：左界 = A - offset，右界 = B（镜像期货）。

    例：A=11:00（左肩 30m 线结束时刻，覆盖 (10:30,11:00]），B=11:30。
        offset = 30m - 5m = 25m。
        期望：C = 10:35（10:31~11:00 内首根 5m 线结束时刻），D = 11:30。
    """
    from BuySellPoint.BSPointList import _stocks_red_range_algo
    c, d = _stocks_red_range_algo("2024/01/02 11:00", "2024/01/02 11:30", "30m", "5m")
    if c != "2024/01/02 10:35":
        failures.append(f"日内红框左界: 期望 10:35，实际 {c}")
        print(f"[FAIL] 日内红框左界: 期望 10:35，实际 {c}")
    elif d != "2024/01/02 11:30":
        failures.append(f"日内红框右界: 期望 11:30，实际 {d}")
        print(f"[FAIL] 日内红框右界: 期望 11:30，实际 {d}")
    else:
        print(f"[PASS] 日内红框(30m+5m): C={c} D={d}")


def test_red_range_algo_daily(failures):
    """日期型主级别（d + 30m）：左界 = A 当日 00:00，右界 = B 当日 23:59。

    例：A=2024/01/02（左肩日线），B=2024/01/05（右肩日线）。
        下窗 30m 输出格式为 %Y/%m/%d %H:%M（分钟精度）。
        期望：C = 2024/01/02 00:00，D = 2024/01/05 23:59。
    """
    from BuySellPoint.BSPointList import _stocks_red_range_algo
    c, d = _stocks_red_range_algo("2024/01/02", "2024/01/05", "d", "30m")
    if c != "2024/01/02 00:00":
        failures.append(f"日线红框左界: 期望 2024/01/02 00:00，实际 {c}")
        print(f"[FAIL] 日线红框左界: 期望 2024/01/02 00:00，实际 {c}")
    elif d != "2024/01/05 23:59":
        failures.append(f"日线红框右界: 期望 2024/01/05 23:59，实际 {d}")
        print(f"[FAIL] 日线红框右界: 期望 2024/01/05 23:59，实际 {d}")
    else:
        print(f"[PASS] 日线红框(d+30m): C={c} D={d}")


def test_red_range_algo_weekly(failures):
    """日期型主级别（w + d）：左界 = A 所在周周一，右界 = B 当日（日期粒度）。

    例：A=2024/01/04（周四），B=2024/01/11（周四）。
        下窗 d 输出格式为 %Y/%m/%d（仅日期）。
        期望：C = 2024/01/01（周一），D = 2024/01/11。
    """
    from BuySellPoint.BSPointList import _stocks_red_range_algo
    c, d = _stocks_red_range_algo("2024/01/04", "2024/01/11", "w", "d")
    if c != "2024/01/01":
        failures.append(f"周线红框左界: 期望 2024/01/01，实际 {c}")
        print(f"[FAIL] 周线红框左界: 期望 2024/01/01，实际 {c}")
    elif d != "2024/01/11":
        failures.append(f"周线红框右界: 期望 2024/01/11，实际 {d}")
        print(f"[FAIL] 周线红框右界: 期望 2024/01/11，实际 {d}")
    else:
        print(f"[PASS] 周线红框(w+d): C={c} D={d}")


def test_red_range_algo_invalid(failures):
    """非法输入 → ("", "")：解析失败或周期未知时静默返回空。"""
    from BuySellPoint.BSPointList import _stocks_red_range_algo
    # 周期未知
    c, d = _stocks_red_range_algo("2024/01/02 11:00", "2024/01/02 11:30", "1m", "5m")
    if c != "" or d != "":
        failures.append(f"未知周期红框: 期望 ('','')，实际 ({c},{d})")
        print(f"[FAIL] 未知周期红框: 期望 ('','')，实际 ({c},{d})")
    else:
        print("[PASS] 未知周期红框: 返回 ('', '')")
    # 日期解析失败
    c, d = _stocks_red_range_algo("not-a-date", "2024/01/02 11:30", "30m", "5m")
    if c != "" or d != "":
        failures.append(f"非法日期红框: 期望 ('','')，实际 ({c},{d})")
        print(f"[FAIL] 非法日期红框: 期望 ('','')，实际 ({c},{d})")
    else:
        print("[PASS] 非法日期红框: 返回 ('', '')")


# ═══════════════════════════════════════════════════════════════════
# 3. 方向守卫：股票公式与期货公式必须「镜像对称」而非「同向」
# ═══════════════════════════════════════════════════════════════════
def test_direction_guard(failures):
    """方向守卫：股票 offset 减在左端（结束时间语义），与期货加在右端相反。

    若有人把股票公式误写成期货式（左界=A、右界=B+offset），
    本用例立即失败——这是全方案最易写错、偏移一个 sub_freq 的地方。
    """
    from BuySellPoint.BSPointList import _stocks_red_range_algo
    c, d = _stocks_red_range_algo("2024/01/02 11:00", "2024/01/02 11:30", "30m", "5m")
    # 股票正确方向：左界前移 offset（10:35），右界不动（11:30）
    if c == "2024/01/02 11:00" or d == "2024/01/02 11:55":
        failures.append("方向守卫: 公式疑似被误改为期货式（offset 加在右端）")
        print("[FAIL] 方向守卫: 公式疑似被误改为期货式（offset 加在右端）")
    else:
        print("[PASS] 方向守卫: 股票公式保持「左界减 offset、右界不动」")


# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════
def main():
    failures = []
    test_sub_dt_algo_intraday(failures)
    test_sub_dt_algo_daily(failures)
    test_sub_dt_algo_unknown_freq(failures)
    test_red_range_algo_intraday(failures)
    test_red_range_algo_daily(failures)
    test_red_range_algo_weekly(failures)
    test_red_range_algo_invalid(failures)
    test_direction_guard(failures)

    print()
    if failures:
        print(f"===== P0 纯函数 4 向单测: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== P0 纯函数 4 向单测: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

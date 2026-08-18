# -*- coding: utf-8 -*-
"""
App/utils.py —— 通用工具函数（自 my_chan_main.py 迁入）
=========================================================================
设计文档 10.1：完成最后一步拆分，my_chan_main.py 职责被各层完全吸收。
本模块收纳纯算法/纯工具函数，无业务依赖。
"""
import time
import json
import os
from typing import List, Dict, Any, Optional


def ema(data: List[float], period: int) -> List[float]:
    """EMA 计算"""
    if not data:
        return []
    alpha = 2.0 / (period + 1)
    ema_values = [data[0]]
    for i in range(1, len(data)):
        ema_values.append(alpha * data[i] + (1 - alpha) * ema_values[-1])
    return ema_values


def calculate_macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """MACD 计算，返回 (dif, dea, macd)"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    dea = ema(dif, signal)
    macd = [2 * (dif[i] - dea[i]) for i in range(len(closes))]
    return dif, dea, macd


def _inherit_macd_for_preview_bar(klines_list):
    """
    为预览K线继承上一根 bar 的 MACD 累计值（预览 bar 只有 open/high/low/close，
    没有成交量，MACD 累计值需要继承）。

    算法：先用当前 bar 的 close 计算新 DIF，DIF 减新 bar 得到新 DEA，
    MACD = 2*(DIF - DEA)。这样预览 bar 加入后指标不会漂移。
    """
    # klines_list 是列表，每个元素是 dict，至少有 close, dif, dea, macd
    if len(klines_list) < 2:
        return klines_list

    # 取出之前计算好的MACD
    prev = klines_list[-2]
    curr = klines_list[-1]

    # 收集所有 close
    closes = [k["close"] for k in klines_list[:-1]] + [curr["close"]]
    difs, deas, macds = calculate_macd(closes)

    # 替换当前 bar 的 MACD
    curr["dif"] = difs[-1]
    curr["dea"] = deas[-1]
    curr["macd"] = macds[-1]
    return klines_list


def _get_date_fmt(freq):
    """根据周期得到 strftime 日期格式"""
    if freq in ("y", "w", "d"):
        return "%Y-%m-%d"
    elif freq in ("60m", "30m", "15m", "5m"):
        return "%Y-%m-%d %H:%M"
    elif freq in ("1m", "15s"):
        return "%Y-%m-%d %H:%M:%S"
    return "%Y-%m-%d %H:%M"


def _get_kl_type(freq):
    """K线类型映射：周期 → Klines 类"""
    if freq in ("y", "q", "m", "w", "d", "M", "W"):
        return "KlineManagerDay"
    elif freq in ("60m", "30m", "15m", "5m", "1m", "15s", "5s"):
        return "KlineManagerMinute"
    return "KlineManagerDay"


def _get_freq_label(freq):
    """周期标签 → 中文标签"""
    label = {
        "y": "年", "q": "季", "m": "月", "w": "周", "d": "日",
        "60m": "60分", "30m": "30分", "15m": "15分", "5m": "5分", "1m": "1分",
        "15s": "15秒", "5s": "5秒",
    }
    return label.get(freq, freq)


def _find_left_shoulder_time(kl_list, bi_list, bi_idx, freq):
    """找顶分型中枢左肩膀时间（红框区间计算用）"""
    if bi_idx <= 0:
        return kl_list[0]["date"]
    prev_bi = bi_list[bi_idx - 1]
    return prev_bi["sdt"]


def _bi_overlap_range(bi, zg, zd):
    """计算笔与中枢区间重叠比例（中枢延伸判断用）"""
    bi_high = max(bi["high"], bi["low"])
    bi_low = min(bi["high"], bi["low"])
    # 重叠区间长度 / 笔高度，>0 表示有重叠
    overlap_high = min(bi_high, zg)
    overlap_low = max(bi_low, zd)
    if overlap_high <= overlap_low:
        return 0.0
    return (overlap_high - overlap_low) / (bi_high - bi_low)


def _calc_zs_confirm_edt_from_bis(zs_obj, all_bi_list, date_fmt):
    """从笔列表计算中枢确认时间（红框中枢输出用）"""
    if not zs_obj.get("bis"):
        return ""
    last_bi_idx = zs_obj["bis"][-1]
    if last_bi_idx >= len(all_bi_list):
        return ""
    last_bi = all_bi_list[last_bi_idx]
    return last_bi["edt"] if "edt" in last_bi else last_bi["sdt"] if "sdt" in last_bi else ""


def safe_write_json_file(path: str, data: Any, *, ensure_ascii: bool = False, indent: Optional[int] = None) -> bool:
    """原子写 JSON 文件：先写 tmp，成功再覆盖，失败保留旧文件。

    持久化底座，防断电/中断产生半截文件。
    """
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
        # 写后读校验（类型一致才覆盖）
        with open(tmp_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, type(data)):
            raise ValueError(f"临时 JSON 文件类型校验失败: 期望 {type(data).__name__}")
        os.replace(tmp_path, path)
        return True
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    return False


def _send_windows_notification(title: str, message: str):
    """发送Windows通知（扫描完成提示），跨平台兼容不抛错"""
    try:
        from win10toast import ToastNotifier
        toaster = ToastNotifier()
        toaster.show_toast(title, message, duration=5)
    except ImportError:
        # 依赖未安装，静默跳过
        pass
    except Exception:
        # 其他错误也静默跳过，不影响主流程
        pass

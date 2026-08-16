# -*- coding: utf-8 -*-
"""
阶段 3b 灰度验证：新旧 SSE 生成器并行比对
=====================================================================
设计文档 8.6「SSE 灰度比对口径」：
  ① 事件类型序列逐个比对 type 字段顺序；
  ② 数据内容剥离时间戳 / 实时价字段后比对结构与笔 / 段 / 中枢数据；
  ③ 间隔仅做统计性核对（事件总数一致即可，不做逐事件比对）。
  灰度输入固定为历史区间（固定 end_date）；实时快照路径的灰度改为
  单次冒烟 + 人工抽查，不纳入自动比对。

本用例被测对象：
  - 旧路径：FrontAPI._sse_generator（线程 + queue.Queue 桥接）
  - 新路径：FrontAPI._sse_async_generator（asyncio.Queue 原生异步）
两者对相同确定性输入并行运行，比对事件序列。真实实时数据源（天勤）
不可离线复现，故用确定性 mock handler 驱动（模拟单/双窗口 SSE 输出
节奏，含时间戳 / 实时价字段以验证剥离逻辑）。

运行：python Test/test_sse_gray_scale.py
"""
import asyncio
import json
import os
import sys
import time

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    import typing_extensions
    typing.Self = typing_extensions.Self

import FrontAPI
from FrontAPI import _sse_generator, _sse_async_generator

SNAPSHOT = os.path.join(TEST_DIR, "snapshots", "sse_gray_scale.json")

# ── 确定性 mock handler：模拟单/双窗口 SSE 典型输出节奏 ──
SSE_FRAME = lambda event, payload: (
    f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))


def _klines(n=3, base_ts=1748826000000):
    """确定性 K 线（含 timestamp 时间戳字段）"""
    out = []
    for i in range(n):
        out.append({
            "date": f"2025-06-02 09:{i:02d}:00",
            "timestamp": base_ts + i * 60000,
            "open": 3200.0 + i, "high": 3210.0 + i, "low": 3195.0 + i,
            "close": 3205.0 + i, "vol": 1000 + i, "amount": 3.9e8 + i,
            "dif": 1.2 + i * 0.1, "dea": 0.8 + i * 0.1, "macd": 0.4 + i * 0.1,
        })
    return out


def _bi_list(n=2):
    """确定性笔列表"""
    return [{
        "direction": "up" if i % 2 == 0 else "down",
        "sdt": f"2025-06-02 09:{i:02d}:00", "edt": f"2025-06-02 10:{i:02d}:00",
        "high": 3210.0 + i, "low": 3195.0 + i,
    } for i in range(n)]


def _zs_list(n=1):
    """确定性中枢列表"""
    return [{
        "start": "2025-06-02 09:00:00", "end": "2025-06-02 11:00:00",
        "zg": 3208.0, "zd": 3198.0,
    } for _ in range(n)]


def mock_single_stream(handler, symbol, freq, start_time):
    """单窗口：init（首事件，含时间戳/笔/中枢）→ 3×tick（实时价）→ done"""
    init_data = {
        "symbol": symbol, "freq": freq, "name": "螺纹钢",
        "meta": {"kline_count": 3, "bi_count": 2, "zs_count": 1},
        "klines": _klines(),
        "bi_list": _bi_list(),
        "zs_list": _zs_list(),
        "segment_list": [],
        "white_hline": 3200.0,
    }
    handler.wfile.write(SSE_FRAME("init", init_data))
    for i in range(3):
        handler.wfile.write(SSE_FRAME("tick", {
            "i": i, "last": 3205.0 + i, "price": 3205.0 + i,
            "timestamp": 1748826000000 + i * 1000,
        }))
    handler.wfile.write(SSE_FRAME("done", {"reason": "normal"}))


def mock_dual_stream(handler, symbol, freq, sub_freq, start_time):
    """双窗口：dual_snapshot（首事件）→ 2×dual_tick → done"""
    snap = {
        "symbol": symbol, "freq": freq, "sub_freq": sub_freq,
        "main": {"meta": {"kline_count": 3, "bi_count": 2, "zs_count": 1},
                 "klines": _klines(), "bi_list": _bi_list(), "zs_list": _zs_list()},
        "sub": {"meta": {"kline_count": 3, "bi_count": 1, "zs_count": 0},
                "klines": _klines(), "bi_list": _bi_list(1), "zs_list": []},
    }
    handler.wfile.write(SSE_FRAME("dual_snapshot", snap))
    for i in range(2):
        handler.wfile.write(SSE_FRAME("dual_tick", {
            "i": i, "main": {"last": 3205.0 + i}, "sub": {"last": 1602.0 + i},
            "timestamp": 1748826000000 + i * 1000,
        }))
    handler.wfile.write(SSE_FRAME("done", {"reason": "normal"}))


def mock_error_stream(handler, *args):
    """异常路径：先出 1 帧再抛异常 → 应转 event:error 并正常关闭"""
    handler.wfile.write(SSE_FRAME("init", {"symbol": "KQ.m@SHFE.rb", "n": 1}))
    raise RuntimeError("数据源中断（测试注入）")


# ── 采集：新旧路径并行 ──
def collect_sync(gen):
    """旧路径：同步采集全部帧"""
    frames = []
    for data in gen:
        frames.append(data)
    return frames


async def _collect_async(gen):
    frames = []
    async for data in gen:
        frames.append(data)
    return frames


def collect_async(gen):
    """新路径：异步采集全部帧"""
    return asyncio.run(_collect_async(gen))


def parse_frames(frames):
    """解析帧 → [{event, data}]"""
    out = []
    for raw in frames:
        text = raw.decode("utf-8")
        event, data = None, None
        for line in text.strip().split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        out.append({"event": event, "data": json.loads(data) if data else None})
    return out


# ── 剥离时间戳 / 实时价字段（灰度口径 ②）──────────────
REALTIME_KEYS = {"timestamp", "ts", "last", "price"}


def strip_realtime(obj):
    """递归剥离时间戳 / 实时价字段，保留笔/段/中枢结构与历史K线"""
    if isinstance(obj, dict):
        return {k: strip_realtime(v) for k, v in obj.items() if k not in REALTIME_KEYS}
    if isinstance(obj, list):
        return [strip_realtime(v) for v in obj]
    return obj


def sequence_summary(events):
    """灰度口径：事件类型序列 + 事件总数（剥离时间戳/实时价后）"""
    return {
        "event_sequence": [e["event"] for e in events],
        "event_count": len(events),
        "payloads": [strip_realtime(e["data"]) for e in events],
    }


def main():
    failures = []
    summary = {}
    force_update = "--update" in sys.argv

    cases = [
        ("single", mock_single_stream, ("KQ.m@SHFE.rb", "1m", None)),
        ("dual", mock_dual_stream, ("KQ.m@SHFE.rb", "1m", "15s", None)),
        ("error", mock_error_stream, ("KQ.m@SHFE.rb", "1m", None)),
    ]

    for name, handler, args in cases:
        # 旧路径（同步）
        t0 = time.time()
        old_frames = collect_sync(_sse_generator(handler, *args))
        t_old = time.time() - t0
        old_events = parse_frames(old_frames)
        old_sum = sequence_summary(old_events)

        # 新路径（异步）
        t0 = time.time()
        new_frames = collect_async(_sse_async_generator(handler, *args))
        t_new = time.time() - t0
        new_events = parse_frames(new_frames)
        new_sum = sequence_summary(new_events)

        # ① 事件类型序列逐个比对
        if old_sum["event_sequence"] != new_sum["event_sequence"]:
            failures.append(f"{name}: 事件类型序列不一致\n  旧: {old_sum['event_sequence']}\n  新: {new_sum['event_sequence']}")
            print(f"[FAIL] {name} 事件类型序列: 不一致")
        else:
            print(f"[PASS] {name} 事件类型序列: {old_sum['event_sequence']}")

        # ③ 事件总数（间隔统计性核对）
        if old_sum["event_count"] != new_sum["event_count"]:
            failures.append(f"{name}: 事件总数不一致 旧={old_sum['event_count']} 新={new_sum['event_count']}")
            print(f"[FAIL] {name} 事件总数: 旧={old_sum['event_count']} 新={new_sum['event_count']}")
        else:
            print(f"[PASS] {name} 事件总数: {old_sum['event_count']}（耗时 旧={t_old:.2f}s 新={t_new:.2f}s）")

        # ② 数据内容剥离时间戳/实时价后比对
        if old_sum["payloads"] != new_sum["payloads"]:
            failures.append(f"{name}: 剥离后数据内容不一致")
            print(f"[FAIL] {name} 剥离后数据内容: 不一致")
        else:
            print(f"[PASS] {name} 剥离后数据内容: 结构/笔/段/中枢一致")

        # 正常关闭行为（哨兵生效）
        if not old_frames or not new_frames:
            failures.append(f"{name}: 帧为空（哨兵未生效）")
            print(f"[FAIL] {name} 正常关闭: 帧为空")
        else:
            print(f"[PASS] {name} 正常关闭: 新旧均正常耗尽")

        # 异常路径专属：末事件必须是 error
        if name == "error":
            if old_sum["event_sequence"][-1] != "error" or new_sum["event_sequence"][-1] != "error":
                failures.append(f"error 用例末事件不是 error: 旧={old_sum['event_sequence'][-1]} 新={new_sum['event_sequence'][-1]}")
                print(f"[FAIL] error 路径: 末事件非 error")
            else:
                print("[PASS] error 路径: 新旧均转 event:error 事件")

        summary[name] = {"old": old_sum, "new": new_sum}

    # ── 冻结 / 比对灰度基线 ──
    if force_update or not os.path.exists(SNAPSHOT):
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"\n[{'UPDATED' if force_update else 'FROZEN'}] SSE 灰度基线: "
              f"{json.dumps({k: v['new']['event_sequence'] for k, v in summary.items()}, ensure_ascii=False)}")
    else:
        with open(SNAPSHOT, encoding="utf-8") as f:
            expected = json.load(f)
        ok = True
        for name in summary:
            if expected[name]["new"] != summary[name]["new"]:
                ok = False
                failures.append(f"{name}: 灰度基线漂移")
                print(f"[FAIL] {name} 灰度基线漂移")
        if ok:
            print("[PASS] SSE 灰度基线: 新旧一致且与冻结基线一致")

    print()
    if failures:
        print(f"===== SSE 灰度验证: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== SSE 灰度验证: 全部通过（新旧路径事件序列一致）=====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

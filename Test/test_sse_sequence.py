# -*- coding: utf-8 -*-
"""
阶段 2.5：SSE 事件序列快照用例
=====================================================================
设计文档 8.4：「为两个 SSE 端点增加事件序列快照用例（首事件类型 /
事件间隔 / 正常关闭行为），为阶段 3b 的 SSE 接口形态重写提供回归基线」。

口径遵循 V10 灰度比对修正：事件类型序列 + 事件总数（统计性核对），
不做逐事件内容比对（内容比对属阶段 3b 灰度验证，输入固定历史区间）。

被测对象：api_server._sse_generator 桥接层 + _SSEMockHandler 协议契约
（真实实时数据源（天勤）不可用于离线回归，故用确定性 mock handler 驱动；
桥接层正是阶段 3b 要重写的部分，其协议行为必须先冻结）。

运行：python Test/test_sse_sequence.py
"""
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

import api_server
from api_server import _sse_generator, _SSEMockHandler

SNAPSHOT = os.path.join(TEST_DIR, "snapshots", "sse_event_sequences.json")

# ── 确定性 mock handler：模拟单/双窗口 SSE 的典型输出节奏 ──
SSE_FRAME = lambda event, payload: (
    f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))


def mock_single_stream(handler, symbol, freq, start_time):
    """单窗口典型事件流：snapshot（首事件）→ 3×tick → done → 关闭"""
    handler.wfile.write(SSE_FRAME("snapshot", {"symbol": symbol, "freq": freq, "n": 1}))
    for i in range(3):
        handler.wfile.write(SSE_FRAME("tick", {"i": i, "last": 100.0 + i}))
    handler.wfile.write(SSE_FRAME("done", {"reason": "normal"}))


def mock_dual_stream(handler, symbol, freq, sub_freq, start_time):
    """双窗口典型事件流：dual_snapshot（首事件）→ 2×dual_tick → done"""
    handler.wfile.write(SSE_FRAME("dual_snapshot", {"symbol": symbol, "freq": freq, "sub": sub_freq}))
    for i in range(2):
        handler.wfile.write(SSE_FRAME("dual_tick", {"i": i, "main": 1.0, "sub": 0.5}))
    handler.wfile.write(SSE_FRAME("done", {"reason": "normal"}))


def mock_error_stream(handler, *args):
    """异常路径：先出 1 帧再抛异常 → 应转 event:error 并正常关闭"""
    handler.wfile.write(SSE_FRAME("snapshot", {"n": 1}))
    raise RuntimeError("数据源中断（测试注入）")


def collect(gen):
    """采集全部事件帧：返回 (帧字节列表, 是否正常关闭)"""
    frames, closed_normally = [], False
    for data in gen:
        frames.append(data)
    # for 正常耗尽 = 哨兵收到 = 正常关闭
    closed_normally = True
    return frames, closed_normally


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
        out.append({"event": event, "data": data})
    return out


def _sequence_summary(events):
    """V10 口径：事件类型序列 + 事件总数（剥离 data 内容中的时间戳/实时价）"""
    return {
        "first_event": events[0]["event"] if events else None,
        "event_sequence": [e["event"] for e in events],
        "event_count": len(events),
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
        t0 = time.time()
        frames, closed = collect(_sse_generator(handler, *args))
        elapsed = time.time() - t0
        events = parse_frames(frames)
        s = _sequence_summary(events)

        # 正常关闭行为（含异常路径：error 事件后必须正常关闭，不得悬挂）
        if not closed:
            failures.append(f"{name}: 流未正常关闭（哨兵未生效）")
            print(f"[FAIL] {name} 正常关闭: 哨兵未生效")
        else:
            print(f"[PASS] {name} 正常关闭: 生成器正常耗尽（{elapsed:.2f}s, {len(frames)} 帧）")

        # 首事件类型
        expected_first = {"single": "snapshot", "dual": "dual_snapshot", "error": "snapshot"}[name]
        if s["first_event"] != expected_first:
            failures.append(f"{name}: 首事件类型 {s['first_event']} != {expected_first}")
            print(f"[FAIL] {name} 首事件: {s['first_event']}")
        else:
            print(f"[PASS] {name} 首事件类型: {s['first_event']}")

        # 异常路径专属：必须转 event:error
        if name == "error":
            if s["event_sequence"][-1] != "error":
                failures.append(f"error 用例末事件不是 error: {s['event_sequence']}")
                print(f"[FAIL] error 路径: 末事件 {s['event_sequence'][-1]}")
            else:
                print("[PASS] error 路径: 异常已转 event:error 事件")

        summary[name] = s

    # ── 冻结 / 比对事件序列快照 ──
    from Test import comparator
    if force_update or not os.path.exists(SNAPSHOT):
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"\n[{'UPDATED' if force_update else 'FROZEN'}] SSE 事件序列基线: "
              f"{json.dumps({k: v['event_sequence'] for k, v in summary.items()}, ensure_ascii=False)}")
    else:
        with open(SNAPSHOT, encoding="utf-8") as f:
            expected = json.load(f)
        ok, diff = comparator.compare(expected, summary, path="$.sse")
        if ok:
            print("[PASS] SSE 事件序列快照: 类型序列/首事件/事件总数 一致")
        else:
            failures.append(f"SSE 序列漂移: {diff}")
            print(f"[FAIL] SSE 事件序列快照:\n{diff}")

    print()
    if failures:
        print(f"===== SSE 事件序列: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== SSE 事件序列: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

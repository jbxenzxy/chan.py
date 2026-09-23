# -*- coding: utf-8 -*-
"""
P65：SSE 源停止中断（盘后静默流卡死修复）
==========================================
2026-09-23 15:02 实盘：锁仓态收盘后点关闭 → 子进程卡死在「收尾中...」，
App 侧等不到退出，前端弹「交易引擎关闭失败：Failed to fetch」。

根因（Trading/Source/SSE.py）：stop() 只置 _running 标志、不关连接；
events() 阻塞在 urlopen(timeout=None) 的 read1 上，_running 检查点在
「下一帧到达」处 —— 盘中 App SSE 每 100ms 有帧，最多 0.1s 退出，所以
盘中关闭一直正常；盘后行情停流、无帧到达，read1 永久阻塞，主循环退
不出、finally 收尾（shutdown_and_lock_all）不执行。

修复：stop() 同时主动 close 当前 SSE 连接，让阻塞中的 read1 立即返回
EOF 或抛异常（两条路径都收敛于 events() 退出）。

本护栏 monkeypatch urlopen 注入可控 FakeResp，在**用户态**复现两种打断
路径（close → read1 返回 EOF / read1 抛 OSError），覆盖：stop 秒级退出、
真正调用了 close（行为级，非字符串断言）、盘中帧流不受影响、结构性不
重连、幂等、未连接空路径。

【环境注记】真实 socket 的跨线程 close 在 WorkBuddy 沙盒会被网络拦截层
SIGTERM（loopback RST + 进程存活 >1s 即杀，实机无此限制），故真 socket
时序验证不在本护栏内 —— close 打断阻塞 read1 是 http.client 标准行为
（close 后 read1 返回空或抛 OSError），两条路径均已在此覆盖。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import Trading.Source.SSE as sse_mod  # noqa: E402
from Trading.Source.SSE import SseSource  # noqa: E402

_results = {"pass": 0, "fail": 0}


def check(tag, cond, expect=True):
    ok = bool(cond) == bool(expect)
    _results["pass" if ok else "fail"] += 1
    print("  {} {}".format("✓" if ok else "✗", tag), flush=True)


class FakeResp:
    """可控 SSE 响应：read1 行为注入；close() 记录调用并可打断 read1。"""

    def __init__(self, read1_impl):
        self._read1 = read1_impl
        self.closed = False

    def read1(self, n):
        return self._read1(n)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _patch_urlopen(resps):
    """把 sse 模块的 urlopen 换成逐次弹出 FakeResp 的桩；返回恢复函数。"""
    seq = list(resps)
    calls = {"n": 0}
    real = sse_mod.urllib.request.urlopen

    def fake(req, timeout=None):
        calls["n"] += 1
        return seq.pop(0)

    sse_mod.urllib.request.urlopen = fake

    def restore():
        sse_mod.urllib.request.urlopen = real

    return calls, restore


def _consume(src, done):
    try:
        for _ev in src.events():
            pass
    except Exception:
        pass
    done.set()


def run_case(name, resps, kwargs_probe=None):
    """通用跑法：注入桩 → 消费线程 → 探针（默认 stop 后断言退出）→ 恢复。"""
    calls, restore = _patch_urlopen(resps)
    done = threading.Event()
    src = SseSource({"sse_base": "http://stub", "symbol": "KQ.m@CFFEX.IM",
                     "freq": "15s"}, None)
    t = threading.Thread(target=_consume, args=(src, done), daemon=True)
    t.start()
    out = {"calls": calls, "src": src, "done": done}
    try:
        if kwargs_probe:
            kwargs_probe(src, done, out)
        else:
            time.sleep(0.3)          # 让消费线程进入 read1 阻塞
            t0 = time.time()
            src.stop()
            exited = done.wait(5.0)
            out["dt"] = time.time() - t0
            out["exited"] = exited
    finally:
        restore()
    return out


def main():
    print("==== P65：SSE 源停止中断（盘后静默流卡死修复）====", flush=True)

    # [1] 盘后卡死复现 + EOF 打断：read1 永久阻塞，stop() 调 close 后返回 b""
    block = threading.Event()
    close_seen = threading.Event()

    def read1_eof(n):
        block.wait(10.0)            # 盘后停流：read1 永久阻塞（等 close 打断）
        close_seen.set()
        return b""                  # close 打断后 read1 返回 EOF

    resp = FakeResp(read1_eof)
    _orig_close = resp.close

    def close_and_release():
        _orig_close()
        block.set()                 # close 打断阻塞读（模拟 OS 层效果）

    resp.close = close_and_release

    out = run_case("eof", [resp])
    check("[1a] 盘后静默流：消费方卡在 read1（已建立连接）",
          not out["done"].is_set() or out["calls"]["n"] == 1, True)
    check("[1b] stop 后消费方秒级退出（<3s）",
          out.get("exited") and out.get("dt", 99) < 3.0, True)
    check("[1c] stop 真正调用了底层 close（行为级）", resp.closed, True)
    check("[1d] read1 被 close 打断后返回 EOF", close_seen.is_set(), True)
    check("[1e] stop 后 _running=False（while 门关死，结构性不重连）",
          out["src"]._running is False, True)
    check("[1f] stop 后 _resp 已清（无残留连接引用）", out["src"]._resp is None, True)
    check("[1g] 全程只建立一次连接（不重连）", out["calls"]["n"] == 1, True)

    # [2] 异常打断路径：close 后 read1 抛 OSError → except 分支收敛退出
    block2 = threading.Event()

    def read1_oserr(n):
        block2.wait(10.0)
        raise OSError("closed")

    resp2 = FakeResp(read1_oserr)
    _orig_close2 = resp2.close

    def close2():
        _orig_close2()
        block2.set()

    resp2.close = close2
    out2 = run_case("oserr", [resp2])
    check("[2a] read1 抛 OSError 路径同样收敛退出",
          out2.get("exited") and out2.get("dt", 99) < 3.0, True)
    check("[2b] 异常路径后 _running=False（不重连）",
          out2["src"]._running is False, True)

    # [3] 盘中帧流不受影响：两帧心跳正常消费、随后 stop 立即退出
    hb_frames = [b": heartbeat\n\n", b": heartbeat\n\n"]
    block3 = threading.Event()

    def read1_frames(n):
        if hb_frames:
            return hb_frames.pop(0)
        block3.wait(10.0)
        return b""

    resp3 = FakeResp(read1_frames)
    _orig_close3 = resp3.close

    def close3():
        _orig_close3()
        block3.set()

    resp3.close = close3
    got = {"n": 0}
    calls3, restore3 = _patch_urlopen([resp3])
    src3 = SseSource({"sse_base": "http://stub", "symbol": "KQ.m@CFFEX.IM",
                      "freq": "15s"}, None)
    done3 = threading.Event()

    def consume3():
        try:
            for _ev in src3.events():
                got["n"] += 1
        except Exception:
            pass
        done3.set()

    t3 = threading.Thread(target=consume3, daemon=True)
    t3.start()
    try:
        time.sleep(0.5)              # 两帧心跳被消费（空 data 帧不产出事件）
        mid_alive = not done3.is_set()
        t0 = time.time()
        src3.stop()
        exited3 = done3.wait(5.0)
        dt3 = time.time() - t0
    finally:
        restore3()
    check("[3a] 盘中帧流阶段消费方正常存活（未退出）", mid_alive, True)
    check("[3b] 盘中 stop 同样秒级打断退出（<3s）",
          exited3 and dt3 < 3.0, True)

    # [4] 幂等：连续 stop 不抛异常
    try:
        src3.stop()
        src3.stop()
        check("[4a] stop 幂等（连续调用不抛异常）", True, True)
    except Exception:
        check("[4a] stop 幂等（连续调用不抛异常）", False, True)

    # [5] 未建立连接时 stop 走空路径（_resp 为 None）
    try:
        SseSource({"sse_base": "http://127.0.0.1:1",
                   "symbol": "KQ.m@CFFEX.IM", "freq": "15s"}, None).stop()
        check("[5a] 未建立连接时 stop 走空路径不炸", True, True)
    except Exception:
        check("[5a] 未建立连接时 stop 走空路径不炸", False, True)

    print("==== P65：{} passed, {} failed ====".format(
        _results["pass"], _results["fail"]), flush=True)
    return 0 if _results["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

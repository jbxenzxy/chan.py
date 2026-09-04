#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M0 信号录制器（零侵入）—— 缠论买卖点生命周期录制
=================================================================
作用
    订阅 chan.py (custom-dev) 的期货 SSE 端点，录制：
      ① 买卖点信号的**完整生命周期**（首次出现 / 价格位移 / 消失重绘）
      ② K 线序列（供离线回放与止盈止损参数统计）
    只通过 HTTP/SSE 消费，**不导入、不修改 chan.py 任何代码**。

为什么要先做这一步
    缠论 bsp 是"结构信号"，随新 K 线可能位移或消失（重绘）。
    在接真金白银的执行器之前，必须先用真实数据量化：
      - 信号出现频率
      - 重绘率（出现后消失 / 价格位移的比例）
      - 信号 K 线振幅分布（决定止损距离）
      - 给定止盈/止损参数下的胜率与期望（用 analyze.py）

输出文件（--out 目录）
    signals.json     全量信号状态（原子覆盖写，含 revisions 位移轨迹）
    events.jsonl     每帧事件流水（新增/消失/位移）
    klines.json      K 线序列（全量，按时间戳去重）

用法
    python signal_recorder.py --symbol "KQ.m@CFFEX.IF" --freq 5m
    python signal_recorder.py --symbol "KQ.m@CFFEX.IF" --freq 5m --base http://127.0.0.1:18081 --out ./out

    需要先启动 chan.py 的 API 服务（python FrontAPI.py，默认端口见 App/AppConfig.py）。

依赖：仅 Python 标准库（urllib / json / argparse）。
"""
import argparse
import json
import os
import sys
import time
import signal as _signal
import urllib.request
from datetime import datetime, timezone, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CN_TZ = timezone(timedelta(hours=8))
_RUNNING = True


def _stop(signum, frame):
    global _RUNNING
    _RUNNING = False
    print("\n[recorder] 收到退出信号，正在收尾...")


_signal.signal(_signal.SIGINT, _stop)
try:
    _signal.signal(_signal.SIGTERM, _stop)
except Exception:
    pass


def now_cn():
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def iter_sse(resp):
    """从 HTTPResponse 逐帧解析 SSE（"event: x\\ndata: {json}\\n\\n"，心跳为 ": x\\n\\n"）。"""
    buf = b""
    while True:
        try:
            chunk = resp.read1(8192)
        except AttributeError:
            chunk = resp.read(8192)
        if not chunk:
            return
        buf += chunk
        while b"\n\n" in buf:
            raw, buf = buf.split(b"\n\n", 1)
            event = None
            data_parts = []
            for line in raw.split(b"\n"):
                if line.startswith(b"event: "):
                    event = line[7:].decode("utf-8", "replace").strip()
                elif line.startswith(b"data: "):
                    data_parts.append(line[6:])
                elif line.startswith(b"data:"):
                    data_parts.append(line[5:])
            yield event, b"".join(data_parts).decode("utf-8", "replace")


def bsp_key(date, btype, is_buy):
    """幂等键。刻意不含价格：笔端点位移会让 date 变化，此时视为新信号、旧信号作废。"""
    return "{}|{}|{}".format(date, btype, "B" if is_buy else "S")


def bsp_fingerprint(b):
    return (round(float(b.get("price", 0) or 0), 6),
            round(float(b.get("high", 0) or 0), 6),
            round(float(b.get("low", 0) or 0), 6))


class Recorder:
    def __init__(self, out_dir, symbol, freq):
        self.out_dir = out_dir
        self.symbol = symbol
        self.freq = freq
        self.signals = {}
        self.klines = {}
        self.prev_keys = set()
        self.frame_no = 0
        self.events_fh = None
        os.makedirs(out_dir, exist_ok=True)
        self._load_existing()

    def _load_existing(self):
        p_sig = os.path.join(self.out_dir, "signals.json")
        if os.path.isfile(p_sig):
            try:
                with open(p_sig, "r", encoding="utf-8") as f:
                    self.signals = json.load(f).get("signals", {})
                print("[recorder] 已加载历史信号 {} 条".format(len(self.signals)))
            except Exception as e:
                print("[recorder] 历史信号加载失败（重新开始）: {}".format(e))
        p_k = os.path.join(self.out_dir, "klines.json")
        if os.path.isfile(p_k):
            try:
                with open(p_k, "r", encoding="utf-8") as f:
                    for k in json.load(f).get("klines", []):
                        self.klines[k["timestamp"]] = k
                print("[recorder] 已加载历史K线 {} 根".format(len(self.klines)))
            except Exception as e:
                print("[recorder] 历史K线加载失败（重新开始）: {}".format(e))
        if self.signals:
            self.prev_keys = {k for k, s in self.signals.items()
                              if s.get("status") == "alive"}

    def open_events(self):
        self.events_fh = open(os.path.join(self.out_dir, "events.jsonl"),
                              "a", encoding="utf-8")

    def close(self):
        if self.events_fh:
            self.events_fh.close()
            self.events_fh = None
        self.flush()

    def on_frame(self, event, payload):
        self.frame_no += 1
        at = now_cn()

        for k in payload.get("klines", []) or []:
            ts = k.get("timestamp")
            if ts is None:
                continue
            self.klines[ts] = {
                "timestamp": ts, "date": k.get("date", ""),
                "open": k.get("open"), "high": k.get("high"),
                "low": k.get("low"), "close": k.get("close"),
                "vol": k.get("vol"),
            }

        cur_keys = set()
        new_keys, gone_keys, changed_keys = [], [], []
        for b in payload.get("bsps", []) or []:
            date = b.get("date", "")
            btype = b.get("type", "")
            key = bsp_key(date, btype, b.get("is_buy"))
            cur_keys.add(key)
            fp = bsp_fingerprint(b)

            if key not in self.signals:
                self.signals[key] = {
                    "key": key, "symbol": self.symbol, "freq": self.freq,
                    "date": date, "timestamp": b.get("timestamp"),
                    "type": btype, "is_buy": b.get("is_buy"),
                    "first_seen_at": at, "last_seen_at": at,
                    "seen_frames": 1, "disappear_count": 0,
                    "status": "alive",
                    "revisions": [{"at": at, "price": b.get("price"),
                                   "high": b.get("high"), "low": b.get("low")}],
                    "final": {"price": b.get("price"), "high": b.get("high"),
                              "low": b.get("low")},
                }
                new_keys.append(key)
            else:
                rec = self.signals[key]
                if rec.get("status") == "gone":
                    rec["disappear_count"] = rec.get("disappear_count", 0) + 1
                    rec["status"] = "alive"
                rec["last_seen_at"] = at
                rec["seen_frames"] = rec.get("seen_frames", 0) + 1
                if bsp_fingerprint(rec["final"]) != fp:
                    rec["revisions"].append({"at": at, "price": b.get("price"),
                                             "high": b.get("high"), "low": b.get("low")})
                    rec["final"] = {"price": b.get("price"), "high": b.get("high"),
                                    "low": b.get("low")}
                    changed_keys.append(key)

        for key in (self.prev_keys - cur_keys):
            rec = self.signals.get(key)
            if rec and rec.get("status") == "alive":
                rec["status"] = "gone"
                rec["gone_at"] = at
                gone_keys.append(key)

        self.prev_keys = cur_keys

        if self.events_fh and (new_keys or gone_keys or changed_keys):
            self.events_fh.write(json.dumps({
                "at": at, "frame": self.frame_no, "event": event,
                "new": new_keys, "gone": gone_keys, "changed": changed_keys,
                "alive_count": len(cur_keys),
            }, ensure_ascii=False) + "\n")
            self.events_fh.flush()

        return new_keys, gone_keys, changed_keys

    def flush(self):
        self._atomic_write("signals.json", {
            "meta": {"symbol": self.symbol, "freq": self.freq,
                     "updated_at": now_cn(), "frames": self.frame_no,
                     "count": len(self.signals)},
            "signals": self.signals,
        })
        ks = sorted(self.klines.values(), key=lambda x: x["timestamp"])
        self._atomic_write("klines.json", {
            "meta": {"symbol": self.symbol, "freq": self.freq,
                     "updated_at": now_cn(), "count": len(ks)},
            "klines": ks,
        })

    def _atomic_write(self, name, obj):
        tmp = os.path.join(self.out_dir, name + ".tmp")
        dst = os.path.join(self.out_dir, name)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, dst)


def run(args):
    url = "{}/api/futures/read/stream?symbol={}&freq={}".format(
        args.base.rstrip("/"),
        urllib.request.quote(args.symbol, safe="@."),
        urllib.request.quote(args.freq),
    )
    rec = Recorder(args.out, args.symbol, args.freq)
    rec.open_events()
    print("[recorder] 目标: {}".format(url))
    print("[recorder] 输出目录: {}".format(os.path.abspath(args.out)))
    print("[recorder] 开始录制，Ctrl+C 停止\n")

    fail_streak = 0
    while _RUNNING:
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=None) as resp:
                print("[recorder] 已连接 (HTTP {})".format(resp.status))
                fail_streak = 0
                for event, data in iter_sse(resp):
                    if not _RUNNING:
                        break
                    if not data:
                        continue
                    try:
                        payload = json.loads(data)
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    new_k, gone_k, chg_k = rec.on_frame(event, payload)
                    if new_k or gone_k or chg_k:
                        print("[{}] {} 新={} 消失={} 位移={} 存活={}".format(
                            now_cn()[11:19], event or "-", len(new_k),
                            len(gone_k), len(chg_k), len(rec.prev_keys)))
                    if rec.frame_no % args.flush_every == 0:
                        rec.flush()
        except Exception as e:
            rec.flush()
            fail_streak += 1
            if not _RUNNING:
                break
            wait = min(args.reconnect * fail_streak, args.reconnect_max)
            print("[recorder] 连接中断: {} | {}s 后重连 (第{}次)".format(
                type(e).__name__, wait, fail_streak))
            time.sleep(wait)

    rec.close()
    print("\n[recorder] 已停止。信号 {} 条，K线 {} 根".format(
        len(rec.signals), len(rec.klines)))
    print("[recorder] 下一步: python analyze.py --out {}".format(args.out))


def main():
    ap = argparse.ArgumentParser(description="M0 缠论买卖点信号录制器（零侵入）")
    ap.add_argument("--base", default="http://127.0.0.1:18081",
                    help="chan.py API 服务地址（默认 http://127.0.0.1:18081）")
    ap.add_argument("--symbol", default="KQ.m@CFFEX.IF", help="合约代码")
    ap.add_argument("--freq", default="5m", help="周期")
    ap.add_argument("--out", default="./out", help="输出目录")
    ap.add_argument("--reconnect", type=int, default=5, help="重连基础间隔秒")
    ap.add_argument("--reconnect-max", type=int, default=60, help="重连最大间隔秒")
    ap.add_argument("--flush-every", type=int, default=10, help="每 N 帧落盘")
    run(ap.parse_args())


if __name__ == "__main__":
    main()

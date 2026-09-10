# -*- coding: utf-8 -*-
"""
实时 SSE 源
===========
订阅 chan.py 现有的端点 `GET /api/futures/read/stream?symbol=&freq=`，
零侵入——不导入也不修改项目代码。

K 线闭合判定（bar_mode）
    "confirmed"（默认）：只有当最后一根 K 线的 timestamp 发生变化时，
    才认为上一根已闭合并发出。这样绝不会用未闭合的 K 线去判定止盈止损
    （那会产生大量虚假触发）。代价是出场判定滞后一根 K 线。
    "last"：每帧都发最后一根，由引擎按 timestamp 去重。延迟低，但快照里
    最后一根可能仍在形成中。

信号的重发语义
    只有"上一帧没有、这一帧出现"的 key 才发。信号消失后再次出现会重新发一次，
    引擎侧的 store 会判定为重复并记录 signal_dup 事件——这正是重绘率的观测点。

信号新鲜度过滤（2026-09-08 改为 K 线位置口径，取代原 signal_max_age_minutes）
    chan.py 的 SSE 是「累计推」语义：每次新连接都会把当前已存在的所有 bsp 一起推过来。
    网关首次启动会收到一大批历史信号（几天前的）。每个买卖点信号的 timestamp = 它
    所在分型右肩 K 的时间戳；快照最后一根 K 即「当前最新 K」。这里按
    「信号归属K 距最新K 的根数」判新旧：距最新 K > N 根（signal_k_tol_bars，默认 1）
    → 视为历史残留丢弃。N 是「距最终K的相对根数」，**不随周期改变**（非周期敏感）。
    0 = 必须正好是最右一根 K 才处理。首连重放的历史 bsp 归属K远，天然被滤。
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Dict, Iterator, Optional, Set

from ..Config import SourceConfig
from ..Infra.PeriodProfile import bar_secs_for
from ..Infra.Types import Bar, Signal
from .Base import Event, Source, register_source


def iter_sse(resp) -> Iterator[tuple]:
    """逐帧解析 SSE（"event: x\\ndata: {json}\\n\\n"，心跳为 ": x\\n\\n"）。"""
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
            parts = []
            for line in raw.split(b"\n"):
                if line.startswith(b"event: "):
                    event = line[7:].decode("utf-8", "replace").strip()
                elif line.startswith(b"data: "):
                    parts.append(line[6:])
                elif line.startswith(b"data:"):
                    parts.append(line[5:])
            yield event, b"".join(parts).decode("utf-8", "replace")


@register_source
class SseSource(Source):
    name = "sse"

    def __init__(self, params: Dict[str, Any], spec):
        super().__init__(params, spec)
        self.base = str(self.params.get("sse_base") or "http://127.0.0.1:18081").rstrip("/")
        self.symbol = str(self.params.get("symbol") or "KQ.m@CFFEX.IF")
        self.freq = str(self.params.get("freq") or "5m")
        self.bar_mode = str(self.params.get("bar_mode") or "confirmed")
        # 信号新鲜度过滤：按「信号归属K 距最新K 的根数」判新旧（K 线位置口径，非周期敏感）。
        # 默认值单一事实源 = Config.SourceConfig（不在本文件写第二套值）。
        self.signal_k_tol_bars = int(
            self.params.get("signal_k_tol_bars", SourceConfig().signal_k_tol_bars))
        # 一根K的毫秒数（周期换算）：未知周期（无档案）为 None → 退化为不过滤，
        # 仅靠引擎幂等去重兜底，避免非标周期把 SSE 源直接打崩。
        _bar_secs = bar_secs_for(self.freq, default=None)
        self._bar_ms = (_bar_secs * 1000) if _bar_secs else None
        # Step 2.5：重连三参数默认值唯一事实源 = Config.SourceConfig（reconnect_* 三字段）。
        # 删除旧 `params.get(key, d) or d` 双默认源写法（且 SourceConfig extra=forbid
        # 下旧键名根本传不进来，是死旋钮）。非法值（<=0 的等待 / 负数重试）构造期 fail-fast。
        _sc = SourceConfig()
        self.reconnect = float(
            self.params.get("reconnect_wait", _sc.reconnect_wait) or 0)
        self.reconnect_max = float(
            self.params.get("reconnect_wait_max", _sc.reconnect_wait_max) or 0)
        self.max_retry = int(
            self.params.get("reconnect_max_retry", _sc.reconnect_max_retry) or 0)   # 0=无限
        if self.reconnect <= 0:
            raise ValueError(
                "source.reconnect_wait 必须 > 0（当前={}），否则中断后会 0 间隔打爆服务"
                .format(self.reconnect))
        if self.reconnect_max <= 0:
            raise ValueError(
                "source.reconnect_wait_max 必须 > 0（当前={}）".format(self.reconnect_max))
        if self.max_retry < 0:
            raise ValueError(
                "source.reconnect_max_retry 不能为负（当前={}，0=无限）"
                .format(self.max_retry))
        self._running = True
        self._prev_bar: Optional[Bar] = None
        self._last_ts: Optional[int] = None
        self._frame_keys: Set[str] = set()

    def url(self) -> str:
        return "{}/api/futures/read/stream?symbol={}&freq={}".format(
            self.base,
            urllib.request.quote(self.symbol, safe="@."),
            urllib.request.quote(self.freq))

    def stop(self) -> None:
        self._running = False

    def _on_frame(self, payload: Dict[str, Any]) -> Iterator[Event]:
        klines = payload.get("klines") or []
        if klines:
            last = klines[-1]
            ts = int(last.get("timestamp") or 0)
            bar = Bar.from_dict(last)
            if self.bar_mode == "last":
                yield ("bar", bar)
            else:
                if self._last_ts is None:
                    self._prev_bar, self._last_ts = bar, ts
                elif ts != self._last_ts:
                    if self._prev_bar is not None:
                        yield ("bar", self._prev_bar)
                    self._prev_bar, self._last_ts = bar, ts
                else:
                    self._prev_bar = bar      # 未闭合期间持续更新为最新快照

        cur: Set[str] = set()
        kl = payload.get("klines") or []
        latest_bar_ts = int(kl[-1].get("timestamp") or 0) if kl else 0  # 快照最新K(ms)
        for b in payload.get("bsps") or []:
            # F3（2026-09-10）：Signal.from_bsp 对缺 date/timestamp 的 bsp 抛 ValueError
            #   （否则幂等键退化成 '|1|B'，同类信号互相去重丢弃）。这里逐条捕获：
            #   丢弃坏条目 + 告警，**不向上冒泡** —— 冒泡会被 events() 的重连兜底
            #   当成连接故障，导致反复断线重连（一个坏 bsp 拖垮整条流）。
            try:
                s = Signal.from_bsp(b, self.symbol, self.freq)
            except ValueError as e:
                print("[sse] 丢弃非法 bsp：{}".format(e))
                continue
            # 新鲜度过滤：信号归属K 距最新K > N（signal_k_tol_bars）根 → 历史残留丢弃。
            #   周期无关（N 是相对根数）；首连 init 重放的历史 bsp 归属K远，天然被滤。
            #   无 klines / 未知周期（_bar_ms 为空）→ 跳过本过滤，由引擎幂等兜底。
            if self._bar_ms and latest_bar_ts:
                dist_bars = (latest_bar_ts - s.timestamp) / self._bar_ms
                if dist_bars > self.signal_k_tol_bars:
                    cur.add(s.key)  # 并入集合，避免后续重发
                    continue
            cur.add(s.key)
            if s.key not in self._frame_keys:
                yield ("signal", s)
        self._frame_keys = cur

    def events(self) -> Iterator[Event]:
        url = self.url()
        fail = 0
        while self._running:
            try:
                req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=None) as resp:
                    fail = 0
                    for _event, data in iter_sse(resp):
                        if not self._running:
                            return
                        if not data:
                            continue
                        try:
                            payload = json.loads(data)
                        except Exception:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        for ev in self._on_frame(payload):
                            yield ev
            except GeneratorExit:
                return
            except Exception as e:
                if not self._running:
                    return
                fail += 1
                if self.max_retry and fail > self.max_retry:
                    raise
                wait = min(self.reconnect * fail, self.reconnect_max)
                print("[sse] 连接中断: {} | {:.0f}s 后重连 (第{}次)".format(
                    type(e).__name__, wait, fail))
                time.sleep(wait)

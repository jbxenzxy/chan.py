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

信号新鲜度过滤（P1 修复）
    chan.py 的 SSE 是「累计推」语义：每次新连接都会把当前已存在的所有 bsp 一起推过来。
    网关首次启动会收到一大批历史信号（几天前的），这些其实早就该被处理过。
    用 `signal_max_age_minutes`（默认 60 分钟）按 first_seen_at 过滤：
        收到信号的当下 - first_seen_at > max_age → 跳过（视为历史残留）
    显式传 0 表示不过滤。
"""
from __future__ import annotations

import datetime as _dt
import json
import time
import urllib.request
from typing import Any, Dict, Iterator, Optional, Set

from ..types import Bar, Signal
from .base import Event, Source, register_source


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
        # P1: 信号新鲜度过滤，单位分钟。0=不过滤。
        self.signal_max_age_min = float(self.params.get("signal_max_age_minutes", 60) or 0)
        self.reconnect = float(self.params.get("reconnect", 5) or 5)
        self.reconnect_max = float(self.params.get("reconnect_max", 60) or 60)
        self.max_retry = int(self.params.get("max_retry", 0) or 0)   # 0=无限
        self._running = True
        self._prev_bar: Optional[Bar] = None
        self._last_ts: Optional[int] = None
        self._frame_keys: Set[str] = set()
        # first_seen_at 的解析格式
        self._ts_formats = (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S.%f",
        )

    def url(self) -> str:
        return "{}/api/futures/read/stream?symbol={}&freq={}".format(
            self.base,
            urllib.request.quote(self.symbol, safe="@."),
            urllib.request.quote(self.freq))

    def _parse_ts(self, s: str) -> Optional[float]:
        """把 first_seen_at 这种字符串解析为 unix 时间戳（秒）。失败返回 None。"""
        if not s:
            return None
        s = s.strip()
        # 尝试多种格式
        for fmt in self._ts_formats:
            try:
                return _dt.datetime.strptime(s, fmt).timestamp()
            except ValueError:
                continue
        # 最后试一下 ISO 8601（"2026-09-01T10:20:00"）
        try:
            return _dt.datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None

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
        now = time.time()
        for b in payload.get("bsps") or []:
            s = Signal.from_bsp(b, self.symbol, self.freq)
            # P1: 信号新鲜度过滤。chan.py SSE 首次连接会 replay 一批历史信号
            # （first_seen_at 几天前），按"信号首次出现时间距今 > max_age"判定为陈旧。
            if self.signal_max_age_min > 0:
                first_seen = (s.extra or {}).get("first_seen_at")
                if first_seen:
                    ts = self._parse_ts(str(first_seen))
                    if ts and (now - ts) > self.signal_max_age_min * 60.0:
                        cur.add(s.key)  # 仍在 _frame_keys 集合里，避免后续又重新发
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

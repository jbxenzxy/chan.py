# -*- coding: utf-8 -*-
"""
离线回放源
==========
直接消费 M0 录制器的产物（`signals.json` + `klines.json`），无需任何转换。
价值：调出场策略时用几秒跑完一天的信号，不用等行情、不受交易时段限制。

事件顺序与实时一致：先 bar（结算旧持仓）→ 再 signal（开新仓）。

关于"已消失"的信号（重要）
    M0 会把重绘后消失的信号标记为 status="gone"。默认**仍然回放**它们——
    因为真实交易时你是在信号*首次出现*的那一刻下的单，事后它消失了，
    亏损也是真实发生的。只回放最终存活的信号等于开了未来函数，会系统性高估策略。
    想对比两者差异时用 --only-alive。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..types import Bar, Signal
from .base import Event, Source, register_source


@register_source
class ReplaySource(Source):
    name = "replay"

    def __init__(self, params: Dict[str, Any], spec):
        super().__init__(params, spec)
        self.dir = str(self.params.get("replay_dir") or "./replay_data")
        self.only_alive = bool(self.params.get("only_alive", False))
        self.speed = float(self.params.get("speed", 0.0) or 0.0)
        self.klines: List[Bar] = []
        self.sig_by_ts: Dict[int, List[Signal]] = {}
        self.sig_by_date: Dict[str, List[Signal]] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        p_sig = os.path.join(self.dir, "signals.json")
        p_k = os.path.join(self.dir, "klines.json")
        if not os.path.isfile(p_k):
            raise FileNotFoundError("回放目录缺少 klines.json: {}".format(p_k))

        with open(p_k, "r", encoding="utf-8") as f:
            for d in json.load(f).get("klines") or []:
                self.klines.append(Bar.from_dict(d))
        self.klines.sort(key=lambda b: b.timestamp)

        if os.path.isfile(p_sig):
            with open(p_sig, "r", encoding="utf-8") as f:
                recs = (json.load(f).get("signals") or {}).values()
            for rec in recs:
                if self.only_alive and rec.get("status") == "gone":
                    continue
                if rec.get("final", {}).get("price") in (None, 0, 0.0):
                    continue
                s = Signal.from_m0_record(rec)
                if s.timestamp:
                    self.sig_by_ts.setdefault(s.timestamp, []).append(s)
                self.sig_by_date.setdefault(s.date, []).append(s)
        self._loaded = True

    def info(self) -> Dict[str, Any]:
        self.load()
        return {"dir": os.path.abspath(self.dir), "bars": len(self.klines),
                "signals": sum(len(v) for v in self.sig_by_ts.values()),
                "only_alive": self.only_alive}

    def events(self) -> Iterator[Event]:
        import time
        self.load()
        used = set()
        for bar in self.klines:
            if self.speed > 0:
                time.sleep(self.speed)
            yield ("bar", bar)
            sigs = self.sig_by_ts.get(bar.timestamp)
            if not sigs:
                sigs = self.sig_by_date.get(bar.date) or []
            for s in sigs:
                if s.key in used:
                    continue
                used.add(s.key)
                yield ("signal", s)

# -*- coding: utf-8 -*-
"""
事件日志（jsonl 追加写）
========================
每一条都是一行 JSON，含时间戳与 kind。用途：
  ① 事后复盘——"当时为什么开/没开"
  ② 参数敏感性分析——出场策略名与参数快照随事件落盘，
     换一套参数重放同一批信号即可对比
  ③ 崩溃取证——进程被杀也不丢（每秒/每 64 条 flush 一次，最坏丢最后 1 秒）
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict

from .types import now_cn

_KIND_LABEL = {
    "start": "启动", "stop": "停止", "bar": "K线", "signal": "信号",
    "signal_dup": "重复信号", "signal_skip": "信号跳过", "order": "委托",
    "fill": "成交", "open": "开仓", "close": "平仓", "risk_block": "风控拦截",
    "order_rejected": "委托被拒", "exit_plan_update": "更新出场计划",
    "error": "错误", "day_roll": "换日",
}


class EventLog:
    def __init__(self, path: str, echo: bool = True, echo_kinds: Any = None,
                 flush_interval: float = 1.0):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.echo = echo
        self.echo_kinds = set(echo_kinds) if echo_kinds else None
        # 每根 K 线都 flush 在 Windows 上会拖慢回放一个数量级（实测 144 根要 7s）。
        # 改成"每秒或每 64 条 flush 一次"，最坏只丢最后 1 秒的事件，可接受。
        self.flush_interval = flush_interval
        self._buf: list = []
        self._last_flush = 0.0
        self.fh = open(path, "a", encoding="utf-8")

    def write(self, kind: str, **payload: Any) -> Dict[str, Any]:
        rec: Dict[str, Any] = {"at": now_cn(), "kind": kind}
        rec.update(payload)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        self._buf.append(line)
        if len(self._buf) >= 64 or (time.time() - self._last_flush) >= self.flush_interval:
            self.flush()
        if self.echo and (self.echo_kinds is None or kind in self.echo_kinds):
            print("[{}] {:<6} {}".format(rec["at"][11:19],
                                         _KIND_LABEL.get(kind, kind),
                                         _brief(payload)),
                  file=sys.stdout, flush=True)
        return rec

    def flush(self) -> None:
        if not self._buf:
            return
        self.fh.write("\n".join(self._buf) + "\n")
        self.fh.flush()
        self._buf.clear()
        self._last_flush = time.time()

    def close(self) -> None:
        try:
            self.flush()
            self.fh.close()
        except Exception:
            pass


def _brief(payload: Dict[str, Any], max_len: int = 150) -> str:
    """把 payload 压成一行可读摘要（不逐字段展开）。"""
    parts = []
    for k, v in payload.items():
        if isinstance(v, dict):
            v = "{" + ", ".join("{}={}".format(a, b) for a, b in list(v.items())[:3]) + "}"
        s = "{}={}".format(k, v)
        parts.append(s)
        if sum(len(p) + 2 for p in parts) > max_len:
            break
    text = "  ".join(parts)
    return text if len(text) <= max_len else text[:max_len - 3] + "..."

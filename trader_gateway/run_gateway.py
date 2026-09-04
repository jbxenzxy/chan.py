#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M1 交易网关 · CLI 入口
======================
    # 用 M0 录制数据离线回放（推荐先跑这个，几秒出结果）
    python run_gateway.py --source replay --replay-dir ./replay_data --out ./run1

    # 实时接入 chan.py 的 SSE
    python run_gateway.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m --out ./run_live

    # 生成一份可编辑的配置
    python run_gateway.py --init-config ./config.json
    python run_gateway.py --config ./config.json

换止盈止损：改 config.json 的 exit_policy.params，或换一个策略类名。
    引擎 / 信号源 / broker 都不需要动。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tg import brokers, sources, strategy          # noqa: E402  导入触发注册
from tg.config import DEFAULT_CONFIG, GatewayConfig  # noqa: E402
from tg.engine import GatewayEngine                  # noqa: E402
from tg.events import EventLog                       # noqa: E402
from tg.store import Store                           # noqa: E402
from tg.types import now_cn                          # noqa: E402

ECHO_DEFAULT = {"start", "signal", "signal_dup", "signal_skip", "open", "close",
                "risk_block", "error", "day_roll", "stop"}


def build_runtime(args):
    if args.config and os.path.isfile(args.config):
        cfg = GatewayConfig.load(args.config)
    else:
        cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
        if args.config:
            print("[cfg] 配置文件不存在，使用内置默认: {}".format(args.config))

    src = dict(cfg.source)
    if args.source:
        src["type"] = args.source
    if args.replay_dir:
        src["replay_dir"] = args.replay_dir
    if args.symbol:
        src["symbol"] = args.symbol
    if args.freq:
        src["freq"] = args.freq
    if args.sse_base:
        src["sse_base"] = args.sse_base
    if args.speed is not None:
        src["speed"] = args.speed
    if args.only_alive:
        src["only_alive"] = True
    if args.bar_mode:
        src["bar_mode"] = args.bar_mode

    out = args.out or cfg.state_dir
    os.makedirs(out, exist_ok=True)
    spec = cfg.instrument

    broker = brokers.build_broker(args.broker or cfg.broker, spec, cfg.broker_params)
    entry = strategy.build_entry_policy(
        cfg.entry_policy.get("name", "DefaultEntryPolicy"),
        cfg.entry_policy.get("params") or {})
    exitp = strategy.build_exit_policy(
        cfg.exit_policy.get("name", "DefaultExitPolicy"),
        cfg.exit_policy.get("params") or {})
    store = Store(os.path.join(out, "state.db"))
    ev = EventLog(os.path.join(out, "events.jsonl"), echo=not args.quiet,
                  echo_kinds=None if args.echo_all else ECHO_DEFAULT)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    source = sources.build_source(src.get("type", "replay"), src, spec)
    return cfg, engine, source, store, ev, out, src


def print_summary(engine: GatewayEngine, out: str, src: Dict[str, Any],
                  cfg: GatewayConfig, elapsed: float) -> Dict[str, Any]:
    s = engine.summary()
    spec = cfg.instrument
    line = "-" * 60
    print("\n" + "=" * 60)
    print("运行摘要  {}".format(now_cn()))
    print("=" * 60)
    print("信号源    : {}   {}".format(src.get("type"),
                                       src.get("replay_dir") or src.get("sse_base", "")))
    print("Broker    : {}".format(engine.broker.name))
    print("合约      : {} -> {}  (tick={}, 乘数={})".format(
        spec.signal_symbol, spec.trade_symbol, spec.price_tick, spec.multiplier))
    print("入场策略  : {}".format(s["entry_policy"]))
    print("出场策略  : {}".format(s["exit_policy"]))
    print(line)
    if s["trades"] == 0:
        print("本轮没有产生成交。检查：回放目录是否有 signals.json、")
        print("风控时段/尾盘限制是否把开仓全拦了（见 events.jsonl 的 risk_block）。")
    else:
        print("成交笔数  : {}   (胜 {} / 负 {})   胜率 {:.1%}".format(
            s["trades"], s["wins"], s["losses"], s["win_rate"]))
        print("平均盈利  : {:+.2f} 点    平均亏损: {:+.2f} 点".format(
            s["avg_win"], s["avg_loss"]))
        print("净盈亏    : {:+.2f} 点   ({:+.2f} 元)".format(
            s["net_points"], s["net_cash"]))
        print("单笔期望  : {:+.3f} 点".format(s["expectancy_points"]))
        if s["by_reason"]:
            seg = "  ".join("{}: n={} net={:+.2f}".format(k, v["n"], v["net"])
                            for k, v in s["by_reason"].items())
            print("按出场    : {}".format(seg))
    print("当前持仓  : {}".format(
        "无" if not s["open_position"] else
        "{side} {volume}手 @{price}".format(
            side=s["open_position"]["side"], volume=s["open_position"]["volume"],
            price=s["open_position"]["entry_price"])))
    print(line)
    print("耗时 {:.2f}s   状态目录: {}".format(elapsed, os.path.abspath(out)))
    print("事件日志: {}".format(os.path.join(os.path.abspath(out), "events.jsonl")))
    print("=" * 60 + "\n")
    return s


def run(args) -> int:
    if args.init_config:
        GatewayConfig.from_dict(DEFAULT_CONFIG).save_example(args.init_config)
        print("[cfg] 已生成配置模板: {}".format(os.path.abspath(args.init_config)))
        print("      改完用 python run_gateway.py --config <路径> 启动")
        return 0

    cfg, engine, source, store, ev, out, src = build_runtime(args)

    if hasattr(source, "info"):
        try:
            print("[source] {}".format(source.info()))
        except Exception as e:
            print("[source] 加载失败: {}".format(e))
            return 2

    ev.write("start", source=src.get("type"), broker=engine.broker.name,
             entry=engine.entry_policy.describe(), exit=engine.exit_policy.describe(),
             instrument={"signal": cfg.instrument.signal_symbol,
                         "trade": cfg.instrument.trade_symbol,
                         "tick": cfg.instrument.price_tick,
                         "multiplier": cfg.instrument.multiplier})

    engine.risk.roll_day("")     # 初始化当日统计
    t0 = time.time()
    counted = 0

    def _stop(signum, frame):
        print("\n[gw] 收到退出信号，收尾中...")
        if hasattr(source, "stop"):
            source.stop()

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except Exception:
        pass

    try:
        for kind, obj in source.events():
            if kind == "bar":
                engine.on_bar(obj)
                counted += 1
            elif kind == "signal":
                engine.on_signal(obj)
            if args.max_bars and counted >= args.max_bars:
                print("[gw] 已达 --max-bars {}，提前停止".format(args.max_bars))
                break
            if not getattr(source, "_running", True):
                break
    except KeyboardInterrupt:
        pass
    except Exception as e:
        ev.write("error", where="main_loop", err="{}: {}".format(type(e).__name__, e))
        raise
    finally:
        elapsed = time.time() - t0
        engine._persist()
        summary = print_summary(engine, out, src, cfg, elapsed)
        if args.summary_json:
            with open(args.summary_json, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        ev.write("stop", bars=counted, elapsed=round(elapsed, 2),
                 trades=summary["trades"], net_points=summary["net_points"])
        engine.broker.close()
        source.close()
        store.close()
        ev.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="缠论信号 → 交易执行网关（M1 dry-run 骨架）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="配置文件路径（JSON）")
    ap.add_argument("--init-config", metavar="PATH",
                    help="生成一份默认配置模板并退出")
    ap.add_argument("--source", choices=["replay", "sse"], help="信号源类型")
    ap.add_argument("--broker", choices=sorted(brokers.BROKERS), help="执行通道")
    ap.add_argument("--replay-dir", help="回放目录（含 signals.json / klines.json）")
    ap.add_argument("--sse-base", help="chan.py API 地址，默认 http://127.0.0.1:18081")
    ap.add_argument("--symbol", help="合约代码，如 KQ.m@CFFEX.IF")
    ap.add_argument("--freq", help="周期，如 5m")
    ap.add_argument("--bar-mode", choices=["confirmed", "last"],
                    help="SSE 源的 K 线闭合判定方式，默认 confirmed")
    ap.add_argument("--speed", type=float, help="回放每根 K 线间隔秒（默认 0）")
    ap.add_argument("--only-alive", action="store_true",
                    help="回放时跳过最终消失的信号（会高估策略，仅供对比）")
    ap.add_argument("--out", help="输出目录（state.db / events.jsonl）")
    ap.add_argument("--summary-json", help="把运行摘要写成 JSON")
    ap.add_argument("--max-bars", type=int, help="最多处理多少根 K 线后停止")
    ap.add_argument("--quiet", action="store_true", help="不打印事件流水")
    ap.add_argument("--echo-all", action="store_true", help="连同 bar/order 一起打印")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()

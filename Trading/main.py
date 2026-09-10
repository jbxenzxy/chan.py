#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M1 交易网关 · CLI 入口
======================
    # 用 M0 录制数据离线回放（推荐先跑这个，几秒出结果）
    python main.py --source replay --replay-dir ./replay_data --out ./run1

    # 实时接入 chan.py 的 SSE
    python main.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m --out ./run_live

配置（2026-09-07 归一）：**只有一处** —— Trading/Config.py
    · 改默认值        → 改 Trading/Config.py 里对应模型字段
    · 临时改（不入库）→ 环境变量或仓库根 .env（TRADING_ 前缀，如 TRADING_SOURCE__FREQ=15s）
    · 单次覆盖        → 命令行 --symbol / --freq / --broker / --source ...
    · 账户密码        → 只走环境变量（SN_ACCOUNT / LIVE_ACCOUNT / TQ_ACCOUNT ...），不落盘

换止盈止损：改 Trading/Config.py 的 ExitConfig（L1-L3 分层出场的唯一参数模型），
    引擎 / 信号源 / broker 都不需要动。出场策略固定为 LayeredExitPolicy，不再有策略选择。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根（import Trading）

from Trading import Broker, Source                        # noqa: E402  导入触发注册
from Trading.Strategy import (DefaultEntryPolicy,          # noqa: E402
                              LayeredExitPolicy)
from Trading.Config import TradingConfig                         # noqa: E402
from Trading.Engine.Engine import TradingEngine                  # noqa: E402
from Trading.Infra.EventLog import EventLog                       # noqa: E402
from Trading.Infra.PeriodProfile import (SUPPORTED_FREQS, SESSION_SECS,  # noqa: E402
                                        bar_secs_for, bars_per_day)
from Trading.Infra.Store import Store                           # noqa: E402
from Trading.Infra.Types import now_cn                          # noqa: E402

ECHO_DEFAULT = {"start", "signal", "signal_dup", "signal_skip", "open", "close",
                "error", "stop"}

# 跨平台停止协议：父进程（AppTrader.stop）在 out_dir 写该文件 → 本模块
# 看护线程观测到 → 请求优雅收尾（lock_all + 持久化 + 退出 0）。
# 绕开 Windows SIGTERM=TerminateProcess（signal handler 不执行）与
# venv shim pid 两处平台陷阱（P1-1 / P1-2）。与 App/AppTrader._STOP_REQUEST 保持一致。
_STOP_REQUEST = ".stop_request"


def build_runtime(args):
    # 配置来源（2026-09-07 归一）：Trading/Config.py 模型默认值
    #   ← 环境变量/仓库根 .env（TRADING_ 前缀，pydantic-settings 自动读取）
    #   ← 命令行参数（最高优先级，下面逐个套用）
    cfg = TradingConfig()

    # 命令行覆盖：只覆盖显式传了的项（None 表示没传）
    if args.source:
        cfg.source.type = args.source
    if args.replay_dir:
        cfg.source.replay_dir = args.replay_dir
    if args.symbol:
        cfg.source.symbol = args.symbol
    if args.freq:
        cfg.source.freq = args.freq
    if args.sse_base:
        cfg.source.sse_base = args.sse_base
    if args.speed is not None:
        cfg.source.speed = args.speed
    if args.only_alive:
        cfg.source.only_alive = True
    if args.bar_mode:
        cfg.source.bar_mode = args.bar_mode
    if args.broker:
        cfg.broker = args.broker

    # Step 1（2026-09-08）：启动期周期校验 —— fail-fast。
    #   周期是所有时间语义（时间止损 / 收盘强平 / 追价窗口）的地基。旧设计里
    #   周期错了不会报错，只会让策略在运行时"推断失败 → 静默降级"，实盘上表现
    #   为"兜底逻辑从没生效过但没人知道"。这里直接拦下，比事后查日志便宜得多。
    _bs = bar_secs_for(cfg.source.freq, default=None)
    if _bs is None:
        raise SystemExit(
            "[gw] 不支持的 K 线周期: {!r}（当前支持: {}）。"
            "新增周期请同步 Trading/Infra/PeriodProfile.py 的 FREQ_SEC "
            "并补 test_period_consistency 对账。".format(
                cfg.source.freq, ", ".join(SUPPORTED_FREQS)))
    # Step 2.2：4.5h 交易日近似收口到 PeriodProfile.SESSION_SECS（原硬编码在此处）
    _bpd = bars_per_day(_bs, SESSION_SECS)
    print("[gw] 周期档案 freq={} bar_secs={}s（约 {} 根/交易日，按 {}h 近似）".format(
        cfg.source.freq, _bs,
        _bpd if _bpd is not None else "?", SESSION_SECS / 3600))

    # 下游（Source.build_source）仍按 dict 消费
    src: Dict[str, Any] = cfg.source.model_dump()

    out = args.out or cfg.state_dir
    if not os.path.isabs(out):
        # 相对路径（默认 "./State"）以 main.py 所在目录
        # （Trading/）为基准，避免 CLI 直跑把 state 建到 CWD 下、
        # 找不到 Trading/State。
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), out)
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    # 引擎全部 stdout/stderr 统一落盘 {out}/gateway.log：
    #   · CLI 直跑（python main.py ...）也会产生 gateway.log；
    #   · AppTrader 子进程模式（stdout 已是该文件）重新赋值无害——后续
    #     print/事件回声只走新句柄，不会重复写。
    # buffering=1（行缓冲）：每行实时落盘，进程崩溃/退出后日志可即查。
    _log_fh = open(os.path.join(out, "gateway.log"), "a",
                   encoding="utf-8", buffering=1)
    sys.stdout = _log_fh
    sys.stderr = _log_fh
    spec = cfg.instrument

    broker = Broker.build_broker(args.broker or cfg.broker, spec,
                                 cfg.broker_params.model_dump())
    entry = DefaultEntryPolicy(cfg.entry_params.model_dump())
    exitp = LayeredExitPolicy(cfg.exit_params.model_dump())
    store_path = os.path.join(out, "state.db")
    store = Store(store_path)

    # ===== --fresh：回放重跑前清空派生状态 =====
    # 历史 bug（v6 实测 0 笔成交）：上一轮回放把 7 个 signal_key 标成了
    # opened/rejected 写进 processed_signals，下一轮回放读同一份 state.db 时
    # try_mark_signal 全部返回 False → 7 笔信号全被判 signal_dup → trades=0。
    # 回放是"重跑同一份数据"，默认就该从干净状态开始；用 --no-fresh 显式保留。
    if getattr(args, "fresh", None) is None:
        fresh = (src.get("type") == "replay")     # 回放默认清，实盘默认留
    else:
        fresh = bool(args.fresh)
    if fresh:
        removed = store.wipe_runtime_state()
        if not getattr(args, "quiet", False):
            print("[fresh] 已清空派生状态: processed_signals={}  trades={}  "
                  "(events.jsonl 与 orders 表保留作审计)"
                  .format(removed.get("processed_signals", 0),
                          removed.get("trades", 0)))

    ev = EventLog(os.path.join(out, "events.jsonl"), echo=not args.quiet,
                  echo_kinds=None if args.echo_all else ECHO_DEFAULT)
    engine = TradingEngine(cfg, broker, entry, exitp, store, ev)
    source = Source.build_source(src.get("type", "replay"), src, spec)
    return cfg, engine, source, store, ev, out, src


def print_summary(engine: TradingEngine, out: str, src: Dict[str, Any],
                  cfg: TradingConfig, elapsed: float) -> Dict[str, Any]:
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
        print("仓位笔数上限是否把开仓静默填满了，或开仓报单是否被拒"
              "（见 events.jsonl 的 open_silenced / order_rejected）。")
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
    # 2026-09-10 修复：Engine.summary()["open_position"] 是**持仓 dict 的列表**
    #   （Engine.py 明确 `[p.to_dict() ...] or None`），原代码当成单个 dict 取下标
    #   → 只要收盘时簿内还有持仓，收尾必抛 TypeError、进程 rc=1，并连带跳过
    #   --summary-json 写出 / stop 事件 / handle.close()。此处取第一笔展示。
    _open_list = s.get("open_position") or []
    _pos0 = _open_list[0] if _open_list else None
    print("当前持仓  : {}".format(
        "无" if _pos0 is None else
        "{side} {volume}手 @{price}".format(
            side=_pos0["side"], volume=_pos0["volume"],
            price=_pos0["entry_price"])))
    print(line)
    print("耗时 {:.2f}s   状态目录: {}".format(elapsed, os.path.abspath(out)))
    print("事件日志: {}".format(os.path.join(os.path.abspath(out), "events.jsonl")))
    print("=" * 60 + "\n")
    return s


def run(args) -> int:
    cfg, engine, source, store, ev, out, src = build_runtime(args)

    # P1-3 防御：启动即清掉上次停止可能遗留的 .stop_request。否则任何"不走
    # AppTrader.start() 的直启/重启路径"（进程崩溃后手动重启、CI 复跑、直接
    # CLI 拉起）一启动就会被残留 flag 看护线程立刻关停。AppTrader.start() 也会
    # 清，这里双保险，让 CLI 直启同样健壮。幂等：文件不存在即跳过。
    _leftover_flag = os.path.join(out, _STOP_REQUEST)
    if os.path.exists(_leftover_flag):
        try:
            os.remove(_leftover_flag)
        except OSError:
            pass

    if hasattr(source, "info"):
        try:
            print("[source] {}".format(source.info()))
        except Exception as e:
            print("[source] 加载失败: {}".format(e))
            return 2

    # 启动摘要：第一屏就给出完整上下文（AppTrader 子进程模式下写入 gateway.log，
    # 进程意外退出时后端 status 会把这段日志尾部带回前端定位）
    print("[gw] 启动 pid={}  source={}  symbol={}  freq={}  sse_base={}  "
          "broker={}  out={}  state_dir={}".format(
              os.getpid(), src.get("type"), src.get("symbol"),
              src.get("freq"), src.get("sse_base", ""), engine.broker.name,
              os.path.abspath(out), cfg.state_dir))

    ev.write("start", source=src.get("type"), broker=engine.broker.name,
             entry=engine.entry_policy.describe(), exit=engine.exit_policy.describe(),
             instrument={"signal": cfg.instrument.signal_symbol,
                         "trade": cfg.instrument.trade_symbol,
                         "tick": cfg.instrument.price_tick,
                         "multiplier": cfg.instrument.multiplier})
    # （2026-09-08：原 engine.risk.roll_day("") 当日统计初始化已随 RiskGate 删除。）

    t0 = time.time()
    counted = 0

    # ── 停止请求（跨平台 flag / 信号 / Ctrl-C）· P1-1/P1-2/P2-4 ──
    # 任一触发源命中 → stop_event 置位。真正的收尾（shutdown_and_lock_all，
    # 内含阻塞式 CTP 下单）不放在 signal handler 里做（P2-4：handler 内阻塞
    # 下单有重入风险，且锁仓最坏 ~100s > _STOP_TIMEOUT 会被 SIGKILL 半途而废），
    # 而是放回主循环：handler / 看护线程只负责"请求停止 + 让主循环退出"，
    # 主循环退出前统一执行收尾。
    stop_event = threading.Event()

    def _request_stop(reason: str) -> None:
        """置停止标志 + 停行情源让主循环退出（不做阻塞收尾）。幂等。"""
        if stop_event.is_set():
            return
        print("[gw] 收到停止请求（{}），收尾中...".format(reason))
        stop_event.set()
        if hasattr(source, "stop"):
            try:
                source.stop()
            except Exception:
                pass

    def _stop(signum, frame):
        _request_stop("signal {}".format(signum))

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except Exception:
        pass

    # 看护线程：监控 AppTrader 写的 .stop_request 文件（Windows 上 SIGTERM
    # 是 TerminateProcess，handler 不执行——flag 文件是唯一可靠跨平台触发）。
    stop_flag = os.path.join(out, _STOP_REQUEST)

    def _monitor_stop_flag() -> None:
        while not stop_event.is_set():
            try:
                if os.path.exists(stop_flag):
                    _request_stop("flag " + _STOP_REQUEST)
                    return
            except OSError:
                pass
            time.sleep(0.2)

    threading.Thread(target=_monitor_stop_flag, name="gw-stop-monitor",
                     daemon=True).start()

    try:
        for kind, obj in source.events():
            if stop_event.is_set():
                break
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
        _request_stop("ctrl-c")
    except Exception as e:
        ev.write("error", where="main_loop", err="{}: {}".format(type(e).__name__, e))
        raise
    finally:
        # 主循环已退出 → 若确有停止请求，执行真正的收尾（停信号门 + 锁仓 +
        # 持久化）。仅在停止请求时锁仓；正常数据流跑完（max_bars / 源自然
        # 结束）不锁。收尾放 finally 而非 signal handler（P2-4）。
        if stop_event.is_set():
            try:
                engine.shutdown_and_lock_all()
            except Exception as e:
                ev.write("error", where="shutdown_lock_all",
                         err="{}: {}".format(type(e).__name__, e))
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
    # 配置只在 Trading/Config.py（+ 环境变量/根 .env 覆盖），不再有 --config JSON。
    ap.add_argument("--source", choices=["replay", "sse"], help="信号源类型")
    ap.add_argument("--broker", choices=sorted(Broker.BROKERS), help="执行通道")
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
    ap.add_argument("--fresh", dest="fresh", action="store_true", default=None,
                    help="启动前清空派生状态（信号幂等键/成交/持仓），"
                         "回放模式默认开启")
    ap.add_argument("--no-fresh", dest="fresh", action="store_false",
                    help="保留上轮状态继续跑（实盘模式默认，SSE 重连用）")
    ap.add_argument("--summary-json", help="把运行摘要写成 JSON")
    ap.add_argument("--max-bars", type=int, help="最多处理多少根 K 线后停止")
    ap.add_argument("--quiet", action="store_true", help="不打印事件流水")
    ap.add_argument("--echo-all", action="store_true", help="连同 bar/order 一起打印")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()

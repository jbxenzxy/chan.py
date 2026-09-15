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

换止盈止损：品种无关参数（ATR / trailing / 触发倍数 / breakeven_buffer_r）改 Trading/Config.py 的
    ExitConfig；品种相关参数（r_multiple_tp，2026-09-14 Fix A 单源化）改
    Trading/Infra/Product.py 的品种档案。
    引擎 / 信号源 / broker 都不需要动。出场策略固定为 LayeredExitPolicy，不再有策略选择。

合约规格（P-B · 2026-09-15 合并）：部署配置收口在 Trading/Config.py 的
    `instrument`（InstrumentConfig，frozen=True）；运行时的有效 tick/乘数、
    涨跌停区间、A′ verified、trade_symbol/last_trade_date 回填收口在**唯一一份**
    `Instrument`（Infra/InstrumentSpec.py），由本文件构造并交给 Engine 与 Broker
    —— 静态身份与运行时状态合并为同一对象（播种桥 for_product/_seed_instrument 已消亡，
    tick/乘数初值直接取品种档案）。
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
from Trading.Strategy import (EntryPolicy,          # noqa: E402
                              LayeredExitPolicy)
from Trading.Config import TradingConfig, resolved_exit_params     # noqa: E402
from Trading.Engine.Engine import TradingEngine                  # noqa: E402
from Trading.Infra.EventLog import EventLog                       # noqa: E402
from Trading.Infra.InstrumentSpec import (Instrument,              # noqa: E402
                                          InstrumentConfig,
                                          derive_exchange)
from Trading.Infra.Period import SUPPORTED_FREQS, bar_secs_for, bars_per_day
from Trading.Infra.TradingClock import SESSION_SECS
from Trading.Infra.StateDB import Store  # noqa: E402

from Trading.Infra.TradingClock import now_cn  # noqa: E402


ECHO_DEFAULT = {"start", "signal", "signal_dup", "signal_skip", "open", "close",
                "error", "stop"}

# 跨平台停止协议：父进程（AppTrader.stop）在 out_dir 写该文件 → 本模块
# 看护线程观测到 → 请求优雅收尾（lock_all + 持久化 + 退出 0）。
# 绕开 Windows SIGTERM=TerminateProcess（signal handler 不执行）与
# venv shim pid 两处平台陷阱（P1-1 / P1-2）。与 App/AppTrader._STOP_REQUEST 保持一致。
_STOP_REQUEST = ".stop_request"


# 2026-09-15 P-B 删除 `_seed_instrument`（Phase 3.2 · Fix B 的播种桥）：
#   `InstrumentSpec.for_product()` + `cfg.instrument.model_dump(exclude={...})` 的
#   躲闪随双类合并一并消亡 —— tick/乘数/exchange/费率真值源 = 品种档案
#   Product，运行时对象 Instrument 构造时直接取档案初值，不再有
#   "先构造 spec 再播种"的两段式。换品种 = 构造期重建 InstrumentConfig
#   （frozen=True 下不能就地改 signal_symbol，见 build_runtime）。


def _fee_banner(cfg: TradingConfig) -> None:
    """启动费率横幅（P-A · 2026-09-15，交接文档 §6.3-d）。

    取代已删除的运行期"平今经济性告警"（closetoday_suggestion）——
    费率是静态档案，启动第一屏就给出两档费率与派生结论，
    比"等成交后才告警"早得多、也不用状态机。只打一次，不刷屏。
    """
    p = cfg.product_profile
    if p is None:
        return
    open_fee, ct_fee = p.fee_pair()
    if not p.supports_closetoday:
        concl = "交易所无平今指令 → 今仓离场走反向锁仓（平今指令已禁用）"
    elif p.prefer_closetoday(1.0):
        concl = "平今更省（平今费 < 3× 开仓费）→ 今仓离场直接平今"
    else:
        concl = "平今更贵（平今费 ≥ 3× 开仓费）→ 今仓离场走反向锁仓"
    print("[gw] 品种 {}（{}）：开仓 {} ｜ 平昨 {} ｜ 平今 {}".format(
        p.product, p.exchange or "?", open_fee.describe(),
        open_fee.describe(), ct_fee.describe()))
    print("[gw]   → " + concl)


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
        # Phase 8（§5.9.1 隐患根治 · §5.9.4 项 7）：--symbol 必须同步打通到
        # instrument.signal_symbol。旧代码只写 source.symbol，"两者一致"只是
        # Config.py 的一句注释约定 —— 前端切品种后 tick/乘数/费率全不变，
        # SimNow 仍按旧 signal_symbol 解析主力合约（实际还在交易 IF），即便
        # 走到下单也会因 tick 不是最小变动价位整数倍被 CTP 拒单。
        #
        # Phase 3.2：换品种的重新播种已随 P-B 消亡 —— InstrumentConfig
        #   frozen=True 下不能就地改 signal_symbol，改为**构造期重建**配置对象
        #   （其余字段原样带过；旧配置若带 price_tick 等已归位键，重建时
        #   InstrumentConfig._check_removed_keys 会显式报错，交接文档 §7.2）。
        if cfg.instrument.signal_symbol != args.symbol:
            _d = cfg.instrument.model_dump()
            _d["signal_symbol"] = args.symbol
            cfg.instrument = InstrumentConfig(**_d)
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

    # 2026-09-15 P-B：品种播种调用已删除（播种桥消亡，见 _seed_instrument 处注释）；
    #   tick/乘数等有效值初值由下面 Instrument 构造直接取品种档案。

    # P-A（2026-09-15）：费率横幅 —— 两档费率 + 平今派生结论，启动第一屏可见。
    _fee_banner(cfg)

    # Step 1（2026-09-08）：启动期周期校验 —— fail-fast。
    #   周期是所有时间语义（时间止损 / 收盘强平 / 追价窗口）的地基。旧设计里
    #   周期错了不会报错，只会让策略在运行时"推断失败 → 静默降级"，实盘上表现
    #   为"兜底逻辑从没生效过但没人知道"。这里直接拦下，比事后查日志便宜得多。
    _bs = bar_secs_for(cfg.source.freq, default=None)
    if _bs is None:
        raise SystemExit(
            "[gw] 不支持的 K 线周期: {!r}（当前支持: {}）。"
            "新增周期请同步 Trading/Infra/Period.py 的 FREQ_SEC "
            "并补 test_period_consistency 对账。".format(
                cfg.source.freq, ", ".join(SUPPORTED_FREQS)))
    # Step 2.2：4.5h 交易日近似收口到 Period.SESSION_SECS（原硬编码在此处）
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
    # P-B（2026-09-15）：唯一一份**运行时对象** —— 静态身份（config 转发）+
    #   运行时身份（trade_symbol/last_trade_date 回填）+ 有效值（初值直接取
    #   品种档案，行情原子覆盖）+ 定价成本，合并于同一个 Instrument。
    #   由本进程持有并同时交给 Broker（写）与 Engine（读）—— 必须同源，
    #   否则 SimNow 置的 verified 引擎看不见，A′ 闸门会恒拒单且看不出原因。
    instr = Instrument(cfg.instrument, cfg.product_profile)

    broker = Broker.build_broker(args.broker or cfg.broker, instr,
                                 cfg.broker_params.model_dump())

    # Phase 8.1（O-4 收窄）：off 只对离线生效 —— 在线通道配 off 时**启动期**
    # 就明确告知（原版静默，运维要等第一笔报单被闸门拒了才从告警反推原因）。
    # 不在此处阻断：闸门（Engine._pre_trade_check）本身会拒单 + 严重告警兜底，
    # 这里只负责"让原因在启动日志里第一屏可见"。只打一次，不会刷屏。
    if (str(cfg.broker_params.instrument_fetch_policy).strip().lower() == "off"
            and not getattr(broker, "is_offline", False)):
        print("[gw] ⚠ instrument_fetch_policy=off 在在线通道（{}）下不生效："
              "合约参数仍必须从行情获取（A′ fail-closed），"
              "未验证前所有报单将被拒单".format(broker.name))
    # 2026-09-14 评审 P1-3：quote_partial 是给"不走 tqsdk 的自研/第三方在线通道"
    # 的逃生舱（只强制 tick/乘数，涨跌停缺失时护栏降级）。降级必须在**启动横幅**
    # 里声明一次 —— broker 侧的告警要等取值失败才发，运维在第一屏就该看到
    # "本轮没有涨跌停保护"，而不是等事后从 events.jsonl 里翻。
    if (str(cfg.broker_params.instrument_fetch_policy).strip().lower()
            == "quote_partial" and not getattr(broker, "is_offline", False)):
        print("[gw] ⚠ instrument_fetch_policy=quote_partial：只强制从行情取 "
              "price_tick / 乘数（缺一即拒单）；涨跌停区间取不到时**护栏降级为"
              "不校验**（来源会标 QUOTE_PARTIAL 并发 instrument_band_degraded 告警），"
              "本通道不提供停板保护")

    # Phase 8（§5.9.3 规则 2 · 来源标记）：离线模式（dry_run，含 replay 数据源）
    # 没有行情连接，合约参数用品种档案值 —— 但必须显式标记来源，让"回测口径"能自证。
    # 在线通道（simnow/live）不走这里：来源由 SimNow 取到行情后标 QUOTE；
    # 取不到则 verified=False → Engine 闸门拒单（fail-closed）。
    # P-B（2026-09-15）：exchange 真值源 = 品种档案 —— 离线不再写对象
    #   （原 spec.exchange = derive_exchange(...) 就地写入随双类合并删除），
    #   改为对账提示：symbol 推导与档案不一致时显式说出来。
    if getattr(broker, "is_offline", False):
        instr.mark_config_offline()
        _ex = derive_exchange(instr.signal_symbol)
        if _ex and _ex != instr.exchange:
            print("[gw] ⚠ 离线对账：symbol 推导交易所 {!r} ≠ 品种档案 exchange {!r}"
                  "（以档案为准；若档案填错请改 PRODUCT_PROFILES）".format(
                      _ex, instr.exchange))
        print("[gw] ⚠ 离线模式：tick/乘数取自品种档案（source=CONFIG_OFFLINE），"
              "可能与交易所口径不符，回测结果不可直接外推实盘")

    entry = EntryPolicy(cfg.entry_params.model_dump())
    # Fix A（2026-09-14）：品种相关出场参数（r_multiple_tp）的唯一来源是 Product
    #   档案 —— 经 resolved_exit_params 合并成完整参数；直接用 exit_params.model_dump()
    #   会缺品种参数（LayeredExitPolicy 构造期 AttributeError）。
    exitp = LayeredExitPolicy(resolved_exit_params(cfg))
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
    # P-B：state 与 broker 共用同一份 Instrument（引擎侧以 state=instr 显式注入；
    # 不给的话引擎会沿用 broker.state，两者本就是同一个对象 —— 显式传只为可读性）。
    engine = TradingEngine(cfg, broker, entry, exitp, store, ev, state=instr)
    source = Source.build_source(src.get("type", "replay"), src, instr)
    return cfg, engine, source, store, ev, out, src


def print_summary(engine: TradingEngine, out: str, src: Dict[str, Any],
                  cfg: TradingConfig, elapsed: float) -> Dict[str, Any]:
    s = engine.summary()
    instr = engine.state          # P-B：唯一运行时对象（部署配置在 instr.config）
    line = "-" * 60
    print("\n" + "=" * 60)
    print("运行摘要  {}".format(now_cn()))
    print("=" * 60)
    print("信号源    : {}   {}".format(src.get("type"),
                                       src.get("replay_dir") or src.get("sse_base", "")))
    print("Broker    : {}".format(engine.broker.name))
    print("合约      : {} -> {}  (tick={}, 乘数={})".format(
        instr.signal_symbol, instr.trade_symbol, instr.price_tick, instr.multiplier))

    print("入场策略  : {}".format(s["entry_policy"]))
    print("出场策略  : {}".format(s["exit_policy"]))
    print(line)
    if s["trades"] == 0:
        print("本轮没有产生成交。检查：回放目录是否有 signals.json、")
        print("信号是否被入场策略/风控过滤（signal_skip / signal_dup），"
              "或报单是否被拒（见 events.jsonl 的 order_rejected）。")
    else:
        print("成交笔数  : {}   (胜 {} / 负 {})   胜率 {:.1%}".format(
            s["trades"], s["wins"], s["losses"], s["win_rate"]))
        # P-A（2026-09-15）：净统计口径改元（net_cash）—— per_lot 费率档
        # 无法在点数口径无损表达，净盈亏只有元是自洽的。
        print("平均盈利  : {:+.2f} 元    平均亏损: {:+.2f} 元".format(
            s["avg_win"], s["avg_loss"]))
        print("净盈亏    : {:+.2f} 元".format(s["net_cash"]))
        print("单笔期望  : {:+.2f} 元".format(s["expectancy_cash"]))
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

    # Phase 3：verified / source 现在读 **engine.state**（唯一运行时状态对象），
    #   不再从配置树的 instrument 上读 —— 启动横幅因此反映的是真实运行口径。
    _st = engine.state
    ev.write("start", source=src.get("type"), broker=engine.broker.name,
             entry=engine.entry_policy.describe(), exit=engine.exit_policy.describe(),
             instrument={"signal": cfg.instrument.signal_symbol,
                         "trade": cfg.instrument.trade_symbol,
                         "tick": _st.price_tick,
                         "multiplier": _st.multiplier,
                         "verified": _st.verified,
                         "source": _st.source,
                         "fetch_policy": cfg.broker_params.instrument_fetch_policy})
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
                 trades=summary["trades"], net_cash=summary["net_cash"])
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

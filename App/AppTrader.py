# -*- coding: utf-8 -*-
"""
App/AppTrader.py —— 自动下单进程托管（交易网关子进程生命周期管理）
=========================================================================
前端期货 K 线页「自动下单」开关 → 本模块启动/停止
trader_gateway/run_gateway.py 子进程（--source sse 实时接入本服务的
SSE 行情流）。

为什么用子进程而不是线程/协程：
  · 交易自动下单子进程持有自己的 tqsdk 长连接（CTP），与主服务（FastAPI + SSE
    行情源）的 TqApi 各自独立，互不掐断；
  · 自动下单子进程崩溃 / 卡单 / 死循环不影响行情页面；
  · 自动下单子进程内部全部是同步阻塞代码（wait_update 循环），塞进线程池会
    与 REST/SSE 抢线程，且无法优雅终止。

开关语义（用户拍板）：
  · 开启（on）→ 拉起子进程，自动下单子进程正常接收买卖点信号并交易；
  · 关闭（off）→ 跨平台「flag 文件」停止协议：
      stop() 写 {out_dir}/.stop_request → 子进程 run_gateway 主循环/看护线程
      观测到该文件 → engine.shutdown_and_lock_all()：
        ① auto_order_enabled=False（停止接收买卖点信号）
        ② 簿内所有「未锁定」持仓全部 LOCK（锁仓，落簿 LOCKED 反向仓，
           次日对向信号自动走解锁入场管线）
      → 子进程退出 0。Windows/venv 无需 SIGTERM（那在 Windows 上是
      TerminateProcess，signal handler 不执行，会静默跳过锁仓），也无需
      依赖 pid 去 kill（venv 下 pid 是 shim 不是真进程）——flag 文件协议
      完全绕开 pid 与信号语义的平台差异。
    graceful 按结果校验：子进程退出的同时，events.jsonl 出现 auto_order_off
    （或 state.db auto_order_enabled==False）才算真正收尾，而不是"退出来即优雅"。
    auto_order_enabled 持久化在 state.db，重启服务/自动下单子进程仍保持
    关闭语义（防悄悄重新开跑）。

实盘安全闸门：启动前预检 config.json —— broker=live 或
broker_params.tq_market≠simnow 时必须显式
confirm_live_trading=true，否则抛 AppError（前端提示，不拉起进程）。

状态持久化：App/auto_trader_state.json 记录最后一次启动参数
（pid/out_dir/cfg_path/started_at/broker），服务重启后可查可停。
"""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

from App.AppErrors import AppError
from App.AppLog import get_logger

log = get_logger("AppTrader")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TG_ROOT = os.path.join(_REPO_ROOT, "trader_gateway")
_RUN_GATEWAY = os.path.join(_TG_ROOT, "run_gateway.py")

# 让 AppTrader 能直接读 state.db（Store 是纯 sqlite，只读安全）。
# P3-6：用 append 而非 insert(0)，避免把 trader_gateway 顶到 sys.path 前面、
# 造成顶层命名污染（不以 trader_gateway 下的同名 package 遮蔽项目其它路径）。
if _TG_ROOT not in sys.path:
    sys.path.append(_TG_ROOT)

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "auto_trader_state.json")

_DEFAULT_CFG = os.path.join(_TG_ROOT, "config.json")
_DEFAULT_OUT = os.path.join(_TG_ROOT, "state")

# 跨平台停止协议用的 flag 文件名（写在 {out_dir} 下）。子进程 run_gateway 主循环
# /看护线程观测到该文件 → engine.shutdown_and_lock_all() → 退出 0。绕开 Windows
# SIGTERM=TerminateProcess（handler 不跑）与 venv pid 是 shim 两类平台陷阱。
_STOP_REQUEST = ".stop_request"

# 后端（AppTrader）日志 tee 进 gateway.log 的 handler，路径随每次启停更新
_log_file_handler: Optional[logging.Handler] = None

# 关闭时给子进程的优雅退出宽限（秒）。
# P2-4：锁仓每笔 submit 是同步阻塞的（平仓每轮 5s × 最多 20 轮追价 → 单笔最坏
# ~100s），stop() 需等子进程主循环完成收尾，故宽限必须覆盖最坏锁仓耗时，
# 超时再强杀兜底。20s 会被 SIGKILL 锁仓半途而废。
_STOP_TIMEOUT = 150.0


class _TraderProc:
    """子进程 + 启动参数的内存态（AppTrader 单例持有）。"""

    __slots__ = ("proc", "pid", "cfg_path", "out_dir", "started_at", "broker",
                 "symbol", "freq", "sse_base")

    def __init__(self, proc: subprocess.Popen, cfg_path: str, out_dir: str,
                 started_at: str, broker: str, symbol: Optional[str] = None,
                 freq: Optional[str] = None, sse_base: Optional[str] = None):
        self.proc = proc
        self.pid = proc.pid if proc is not None else 0
        self.cfg_path = cfg_path
        self.out_dir = out_dir
        self.started_at = started_at
        self.broker = broker
        self.symbol = symbol or ""
        self.freq = freq or ""
        self.sse_base = sse_base or ""

    @property
    def running(self) -> bool:
        if self.proc is not None and self.proc.poll() is not None:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "running": self.running,
            "cfg_path": self.cfg_path,
            "out_dir": self.out_dir,
            "started_at": self.started_at,
            "broker": self.broker,
            "symbol": self.symbol,
            "freq": self.freq,
            "sse_base": self.sse_base,
        }


def _read_state_file() -> Dict[str, Any]:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state_file(data: Dict[str, Any]) -> None:
    # 原子写（P3-1）：先写临时文件再 os.replace，避免中途崩溃留下半截 JSON，
    # 下次 _read_state_file 解析失败 → 静默 {} → 进程在跑但托管失联。
    tmp = _STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATE_FILE)
    except OSError as e:
        log.info("[AppTrader] 写状态文件失败: %s: %s", type(e).__name__, e)
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _engine_store(out_dir: str):
    """按 out_dir 打开自动下单子进程 state.db 的 Store（只读场景为主）。"""
    from tg.store import Store
    return Store(os.path.join(out_dir, "state.db"))


def _events_have_off(out_dir: str) -> bool:
    """events.jsonl 出现过 auto_order_off 事件（shutdown_and_lock_all 成功收尾）。

    这是判定"优雅关闭"的最强证据：shutdown_and_lock_all 单点在锁仓并持久化后
    写此事件，且只在真正执行时才写。比起"进程退出来就当作优雅"要可靠得多
    （修复 P1-1 的谎报成功：Windows TerminateProcess 下进程也退了，但无此事件）。
    """
    try:
        with open(os.path.join(out_dir, "events.jsonl"),
                  "r", encoding="utf-8") as f:
            for line in f:
                if '"kind": "auto_order_off"' in line:
                    return True
    except OSError:
        pass
    return False


def _graceful_by_result(out_dir: str) -> bool:
    """按结果判定优雅关闭：子进程确实执行了收尾（锁仓 + 落盘关闭态）。

    主要看 auto_order_off 事件；个别情况下事件文件未刷盘（无回写）时，
    回援 state.db 的 auto_order_enabled==False（shutdown 持久化过）。
    """
    if _events_have_off(out_dir):
        return True
    try:
        s = _engine_store(out_dir)
        enabled = bool(s.get_json("auto_order_enabled", True))
        s.close()
        return enabled is False
    except Exception:
        return False


class AppTrader:
    """自动下单子进程托管单例。所有操作持进程内锁，串行化启停。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._handle: Optional[_TraderProc] = None
        # 已上报过「自动下单子进程退出」的 pid 集合：status() 前台轮询频繁，必须只
        # 上报一次，否则每次轮询都把日志尾部再写回 gateway.log，造成
        # 雪崩式无限自嵌套、日志指数级膨胀（掩盖真实退出原因）。
        self._exit_logged: set = set()
        # pid -> 子进程 stdin 输出读取线程：用 PIPE 接管子进程 stdout
        # （Windows 下把 text-mode 文件对象直接塞给 Popen 作 stdout 是已知
        # 脆弱点，句柄继承不牢，导入期崩溃的 traceback 会整段丢失 → 零输出
        # + 同秒静默退出）。改用 PIPE + 读取线程 tee 落盘，任何阶段输出不丢。
        self._readers: Dict[int, threading.Thread] = {}
        self._restore_from_file()

    # ---------------- 内部 ----------------
    def _restore_from_file(self) -> None:
        """服务重启后恢复上一次的启动参数（pid 已失效则视为未运行）。"""
        data = _read_state_file()
        pid = int(data.get("pid") or 0)
        if pid <= 0:
            return
        if not _pid_alive(pid):
            # 上次进程已不在（服务重启/自动下单子进程退出）：清掉残留状态文件
            log.info("[AppTrader] 上次自动下单子进程已退出，清理状态（pid=%s）", pid)
            _write_state_file({})
            return
        # pid 活着但这不是我们 spawn 的句柄 —— 只能记录参数，stop 时按 pid 发信号
        self._handle = _TraderProc(
            proc=None, cfg_path=str(data.get("cfg_path") or ""),
            out_dir=str(data.get("out_dir") or ""),
            started_at=str(data.get("started_at") or ""),
            broker=str(data.get("broker") or ""),
            symbol=str(data.get("symbol") or ""),
            freq=str(data.get("freq") or ""),
            sse_base=str(data.get("sse_base") or ""),
        )
        self._handle.pid = pid

    # ---------------- 对外操作 ----------------
    def start(self, cfg_path: Optional[str] = None,
              out_dir: Optional[str] = None,
              symbol: Optional[str] = None,
              freq: Optional[str] = None,
              sse_base: Optional[str] = None) -> Dict[str, Any]:
        """启动自动下单自动下单子进程（SSE 实时源，订阅 chan.py 行情流）。

        cfg_path：trader_gateway config.json（缺省 trader_gateway/config.json）；
        out_dir： 自动下单子进程状态目录（缺省 cfg.state_dir 或 trader_gateway/state）；
        symbol / freq：订阅的合约与周期（前端开关传当前页面品种；缺省读
            cfg.source，再缺省 KQ.m@CFFEX.IF / 5m）；
        sse_base：行情流地址（前端传 location.origin；缺省读 cfg.source，
            再缺省 http://127.0.0.1:18081）。
        启动前预检：config 存在 + 实盘安全闸门（live 必须 confirm_live_trading）。
        自动下单子进程 stdout/stderr 落盘 {out_dir}/gateway.log（异常可查，不再吞掉）。
        """
        with self._lock:
            if self._handle is not None and self._handle.running:
                return self._handle.to_dict()

            cfg_path = os.path.abspath(cfg_path or _DEFAULT_CFG)

            # ── 先定状态目录并创建日志文件：无论后续校验是否通过，都留下
            #    可查的 gateway.log（曾有"执行后什么都没有"——根因是校验
            #    失败在 makedirs 之前就 raise，目录/日志从未创建）。
            pre_cfg: Dict[str, Any] = {}
            if os.path.isfile(cfg_path):
                try:
                    pre_cfg = self._load_cfg(cfg_path)
                except AppError:
                    pre_cfg = {}
            if out_dir:
                out_dir = os.path.abspath(out_dir)
            else:
                raw = str(pre_cfg.get("state_dir") or "") or _DEFAULT_OUT
                # 相对 state_dir（默认 "./state"）以配置文件所在目录
                # （trader_gateway/）为基准，避免落到后端进程 CWD 下，
                # 造成"找不到 trader_gateway/state"。
                out_dir = os.path.abspath(
                    os.path.join(os.path.dirname(cfg_path), raw))
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as e:
                raise AppError(
                    "无法创建自动下单子进程状态目录 {}: {}: {}".format(
                        out_dir, type(e).__name__, e))
            log_file = os.path.join(out_dir, "gateway.log")
            # 后端（本模块）日志也 tee 进 gateway.log，与自动下单子进程日志同一文件
            self._set_engine_log_handler(log_file)
            self._engine_log(
                log_file,
                "收到开启请求: cfg={} out={} symbol={} freq={} sse_base={}".format(
                    cfg_path, out_dir, symbol, freq, sse_base))

            if not os.path.isfile(cfg_path):
                msg = ("交易网关配置文件不存在: {}。请先用 "
                       "python trader_gateway/run_gateway.py --init-config <路径> "
                       "生成并配置（含 broker/账户选择）。".format(cfg_path))
                self._engine_log(log_file, "启动失败: " + msg)
                raise AppError(msg)

            try:
                cfg_data = self._load_cfg(cfg_path)
            except AppError as e:
                self._engine_log(log_file, "读取配置失败: {}".format(e))
                raise
            broker = str(cfg_data.get("broker") or "dry_run")
            try:
                self._check_live_gate(cfg_data, broker)
            except AppError as e:
                self._engine_log(log_file, "实盘安全闸门拦截: {}".format(e))
                raise

            # 开启 = 显式恢复自动下单开关（上次关闭已把 False 持久化）
            self._reset_engine_switch(out_dir)

            # 信号源参数：前端开关优先（当前页面品种/周期），其次 cfg.source，最后内置默认
            src_cfg = cfg_data.get("source") or {}
            use_symbol = symbol or str(src_cfg.get("symbol") or "KQ.m@CFFEX.IF")
            use_freq = freq or str(src_cfg.get("freq") or "5m")
            use_base = sse_base or str(src_cfg.get("sse_base")
                                       or "http://127.0.0.1:18081")

            cmd = [sys.executable, _RUN_GATEWAY,
                   "--config", cfg_path,
                   "--out", out_dir,
                   "--no-fresh",     # 保留持仓/信号幂等键，不 wipe
                   "--quiet",        # 不打印事件流水（日志在 out/events.jsonl）
                   "--source", "sse",
                   "--symbol", use_symbol,
                   "--freq", use_freq,
                   "--sse-base", use_base]
            log.info("[AppTrader] 启动自动下单子进程: %s", " ".join(cmd))
            log.info("[AppTrader] 信号源: source=sse symbol=%s freq=%s "
                     "sse_base=%s broker=%s out=%s",
                     use_symbol, use_freq, use_base, broker, out_dir)
            log.info("[AppTrader] 自动下单子进程日志: %s", log_file)
            self._engine_log(
                log_file,
                "启动子进程: source=sse symbol={} freq={} sse_base={} "
                "broker={} cmd={}".format(
                    use_symbol, use_freq, use_base, broker, " ".join(cmd)))
            try:
                env = dict(os.environ)
                env["PYTHONUNBUFFERED"] = "1"   # 自动下单子进程 stdout 逐行落盘，异常/退出可即查
                # 用 PIPE + 读取线程接管子进程 stdout，而不用把 text-mode 文件
                # 对象塞给 Popen（Windows 句柄继承脆弱，导入期 traceback 会丢）。
                # run_gateway.py 内部随后会把 sys.stdout/stderr 重定向到
                # gateway.log，本 PIPE 主要兜住「重定向前」的启动/导入期输出。
                #
                # Windows 关键修复：本 start() 被 FastAPI worker 线程调用，
                # 从非主线程 Popen 一个控制台子进程 + 继承父进程控制台 stdin，
                # 会造成子进程 DllMain 初始化失败 → 退出码 0xC0000142
                # (STATUS_DLL_INIT_FAILED)，Python 还没执行就静默退出、零输出。
                # 对策：stdin 用 DEVNULL（不继承控制台输入句柄）+ 子进程不附加
                # 控制台 (CREATE_NO_WINDOW)，彻底绕开控制台句柄继承链路。
                creationflags = 0
                if os.name == "nt":
                    creationflags = int(
                        getattr(subprocess, "CREATE_NO_WINDOW", 0))
                proc = subprocess.Popen(
                    cmd, cwd=_TG_ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=env, creationflags=creationflags)
            except OSError as e:
                self._engine_log(log_file, "启动子进程失败: {}: {}".format(
                    type(e).__name__, e))
                raise AppError(
                    "启动交易自动下单子进程失败: {}: {}".format(type(e).__name__, e))

            self._spawn_reader(proc, log_file)

            handle = _TraderProc(proc, cfg_path, out_dir,
                                 time.strftime("%Y-%m-%d %H:%M:%S"), broker,
                                 symbol=use_symbol, freq=use_freq,
                                 sse_base=use_base)
            self._handle = handle
            # 新一轮自动下单子进程：清零退出上报集合（避免历史 pid 干扰本次退出上报）
            self._exit_logged.discard(handle.pid)
            _write_state_file(handle.to_dict())
            log.info("[AppTrader] 自动下单子进程已启动 pid=%s", handle.pid)
            self._engine_log(log_file, "子进程已启动 pid={}".format(handle.pid))
            return handle.to_dict()

    def stop(self, timeout: float = _STOP_TIMEOUT) -> Dict[str, Any]:
        """关闭自动下单：跨平台 flag 文件停止协议 → 子进程收尾锁仓 → 退出。

        ① 写 {out_dir}/.stop_request —— 子进程 run_gateway 主循环/看护线程观测到
           即 shutdown_and_lock_all 并退出（跨平台，不依赖 pid 与信号语义）；
        ② 非 Windows 再补发 SIGTERM 促活（Linux/macOS handler 会转置停止事件），
           Windows **不发** —— SIGTERM 在 Windows 上是 TerminateProcess，会抢在
           flag 被消费前强杀，反而破坏优雅；
        ③ 等子进程退出，超时再强杀兜底（Windows 追加 taskkill /T 防 venv shim
           pid 留下真进程孤儿，P1-2）；
        ④ graceful 按**结果**判定：EXIT 且出现 auto_order_off 事件（或 state.db
           auto_order_enabled==False），而不是"退出来即优雅"（修 P1-1 谎报成功）。
        """
        with self._lock:
            handle = self._handle
            if handle is None or not handle.running:
                # 没有在跑的子进程：仍确保开关落盘为关闭态（防状态漂移）
                if handle is not None and os.path.isdir(handle.out_dir):
                    lf = os.path.join(handle.out_dir, "gateway.log")
                    self._set_engine_log_handler(lf)
                    try:
                        s = _engine_store(handle.out_dir)
                        s.set_json("auto_order_enabled", False)
                        s.close()
                    except Exception:
                        pass
                    self._engine_log(lf, "收到关闭请求（未在运行）")
                _write_state_file({})
                self._handle = None
                return {"running": False, "pid": None, "note": "未在运行"}

            pid = handle.pid
            out_dir = handle.out_dir
            log_file = os.path.join(out_dir, "gateway.log")
            self._set_engine_log_handler(log_file)
            self._engine_log(log_file, "收到关闭请求 pid={}".format(pid))

            # ① 写停止 flag —— 子进程唯一的跨平台停止触发
            stop_flag = os.path.join(out_dir, _STOP_REQUEST)
            try:
                with open(stop_flag, "w", encoding="utf-8") as f:
                    f.write("requested_by=apptrader ts={}\n".format(
                        time.strftime("%Y-%m-%d %H:%M:%S")))
            except OSError as e:
                self._engine_log(log_file, "写停止flag失败: {}: {}".format(
                    type(e).__name__, e))

            # ② 非 Windows 补发 SIGTERM 促活；Windows 不发（见 docstring）
            if os.name != "nt":
                _send_signal_best_effort(handle, signal.SIGTERM)

            # ③ 等退出
            deadline = time.time() + timeout
            exited = False
            while time.time() < deadline:
                if not handle.running:
                    exited = True
                    break
                time.sleep(0.3)

            # 兜底强杀
            if not exited:
                log.warning(
                    "[AppTrader] 自动下单子进程 pid=%s 未在 %.0fs 内退出，强杀兜底",
                    pid, timeout)
                _send_signal_best_effort(handle, signal.SIGKILL)
                try:
                    handle.proc.wait(timeout=5)
                except Exception:
                    pass
                if os.name == "nt":
                    _taskkill(pid)

            # 拿退出码（定位"自动退出"问题：非 0 说明自动下单子进程主循环抛异常）
            rc = None
            try:
                rc = handle.proc.wait(timeout=0)
            except Exception:
                pass

            # ④ graceful 按结果校验（P1-1：不再信任"退出来即优雅"）
            graceful = exited and _graceful_by_result(out_dir)
            if graceful:
                log.info("[AppTrader] 自动下单已优雅关闭 pid=%s（已锁仓并持久化关闭态）",
                         pid)
            else:
                log.warning(
                    "[AppTrader] 自动下单 pid=%s 已退出但未检测到收尾结果"
                    "(graceful=False, rc=%s) —— 需核查是否真的锁仓", pid, rc)

            self._handle = None
            _write_state_file({})
            log.info("[AppTrader] 自动下单已关闭（pid=%s，graceful=%s，rc=%s）",
                     pid, graceful, rc)
            self._engine_log(
                log_file,
                "已关闭 pid={} graceful={} rc={}".format(pid, graceful, rc))
            return {"running": False, "pid": pid, "graceful": graceful, "rc": rc}

    def status(self) -> Dict[str, Any]:
        """自动下单状态（进程 + 自动下单子进程开关 + 持仓快照）。"""
        with self._lock:
            handle = self._handle
            running = bool(handle is not None and handle.running)
            base: Dict[str, Any] = {
                "running": running,
                "pid": handle.pid if handle else None,
                "started_at": handle.started_at if handle else None,
                "cfg_path": handle.cfg_path if handle else None,
                "out_dir": handle.out_dir if handle else None,
                "broker": handle.broker if handle else None,
                "symbol": handle.symbol if handle else None,
                "freq": handle.freq if handle else None,
                "sse_base": handle.sse_base if handle else None,
                "log_file": (os.path.join(handle.out_dir, "gateway.log")
                             if handle is not None else None),
            }
            if handle is None:
                # 尝试从上次状态文件恢复基本信息（仅展示用）
                data = _read_state_file()
                if data.get("pid"):
                    base["pid"] = int(data["pid"])
                    base["cfg_path"] = data.get("cfg_path")
                    base["out_dir"] = data.get("out_dir")
                    base["started_at"] = data.get("started_at")
                    base["broker"] = data.get("broker")
                    base["symbol"] = data.get("symbol")
                    base["freq"] = data.get("freq")
                    base["sse_base"] = data.get("sse_base")
                    if data.get("out_dir"):
                        base["log_file"] = os.path.join(
                            str(data["out_dir"]), "gateway.log")
            log_file = base.get("log_file")
            if log_file:
                self._set_engine_log_handler(str(log_file))
            # 进程曾启动但已退出：附带 gateway.log 尾部，直接回答"为什么关掉了"。
            # 退出只上报一次（_exit_logged 记 pid），且不把尾部 Echo 回
            # gateway.log —— 否则前台每次轮询 status() 都把上一轮写入的尾部
            # 再写一遍，日志自嵌套无限膨胀（雪崩），掩盖真实退出原因。
            if handle is not None and not running and log_file:
                tail = self._read_log_tail(str(log_file))
                base["log_tail"] = tail or None
                # 取子进程退出码（判别"零输出同秒退出"的机器级原因）：
                #   0           干净返回（我们的 run() 总会 print_summary，不可能静默 0）
                #   0xC0000005  访问违例（硬崩溃，Traceback 都没机会写）
                #   0xC0000135  DLL 加载失败（Python/依赖启动即挂）
                rc = None
                try:
                    proc = handle.proc
                    if proc is not None:
                        rc = getattr(proc, "returncode", None)
                        if rc is None:
                            try:
                                rc = proc.poll()
                            except Exception:
                                rc = None
                except Exception:
                    rc = None
                base["exit_rc"] = rc
                if handle.pid not in self._exit_logged:
                    self._exit_logged.add(handle.pid)
                    log.warning(
                        "[AppTrader] 自动下单子进程 pid=%s 已退出（rc=%s，一次性上报，"
                        "原因见 %s 尾部）", handle.pid, rc, log_file)
                    self._engine_log(
                        str(log_file),
                        "检测到自动下单子进程 pid={} 已退出 rc={}（详情见日志上文）".format(
                            handle.pid, rc))
            # 自动下单子进程内部状态：尽力读 state.db（自动下单子进程写 WAL，并发只读安全）
            base["auto_order"] = self._read_engine_switch(handle)
            return base

    # ---------------- 内部工具 ----------------
    @classmethod
    def _set_engine_log_handler(cls, log_file: str) -> None:
        """把后端（AppTrader logger）的输出 tee 进 gateway.log（路径随启停更新）。

        根 logger 仍按 AppLog 配置打后端终端；本 handler 只让 trader 相关的
        后端日志同时落盘 gateway.log，与自动下单子进程日志同一文件定位。
        """
        global _log_file_handler
        try:
            if _log_file_handler is not None:
                log.removeHandler(_log_file_handler)
                try:
                    _log_file_handler.close()
                except Exception:
                    pass
                _log_file_handler = None
            h = logging.FileHandler(log_file, encoding="utf-8", delay=True)
            h.setFormatter(logging.Formatter(
                "%(asctime)s [AppTrader] %(levelname)-5s %(message)s",
                datefmt="%H:%M:%S"))
            log.addHandler(h)
            _log_file_handler = h
        except OSError:
            pass

    @staticmethod
    def _engine_log(log_file: str, line: str) -> None:
        """追加写自动下单子进程日志文件（与自动下单子进程 stdout 同一文件，定位一体化）。

        带 [AppTrader] 前缀以示来自进程托管层；自动下单子进程自身的输出为裸行。
        文件不存在（如目录被删）静默跳过，不影响主流程。
        """
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("[{}] [AppTrader] {}\n".format(
                    time.strftime("%H:%M:%S"), line))
        except OSError:
            pass

    @staticmethod
    def _read_log_tail(log_file: str, n: int = 12) -> str:
        """读自动下单子进程日志尾部若干行（进程意外退出时带回前端，帮助定位）。

        从文件末尾反向读，避免日志较大时每次 status() 轮询都整读一遍。
        """
        try:
            with open(log_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                chunk = b""
                # 每次回退 8KB，最多拼满 ~256KB；尾行以 \n 界定取最后 n 行
                pos = f.tell()
                while pos > 0 and len(chunk) < 256 * 1024:
                    pos = max(0, pos - 8192)
                    f.seek(pos)
                    chunk = f.read() + chunk
                    if pos == 0:
                        break
                text = chunk.decode("utf-8", errors="replace")
                lines = text.splitlines()
                return "\n".join(lines[-n:])
        except OSError:
            return ""

    def _spawn_reader(self, proc: subprocess.Popen, log_file: str) -> None:
        """后台线程流式把子进程 stdout (PIPE) tee 进 gateway.log。

        跨平台最稳的子进程输出接管方式（Windows 下把 text-mode 文件对象
        塞给 Popen 作 stdout，句柄继承不牢，导入期 traceback 会整段丢失）。
        子进程自己随后把 sys.stdout 重定向到同一文件时，本线程只会读到
        「重定向前」的启动/导入期输出，与子进程落盘内容不重复。
        """
        pid = proc.pid
        stdout = getattr(proc, "stdout", None)
        if stdout is None:
            # 无 stdout 句柄（如测试桩/恢复态）：无可 tee，直接返回
            return

        def _drain():
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    try:
                        text = line.decode("utf-8", errors="replace")
                    except Exception:
                        text = "<undecodable>"
                    self._engine_log(log_file, "engine> " + text.rstrip("\r\n"))
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                self._readers.pop(pid, None)

        t = threading.Thread(target=_drain, name="apptrader-engine-tee",
                             daemon=True)
        self._readers[pid] = t
        t.start()

    @staticmethod
    def _load_cfg(cfg_path: str) -> Dict[str, Any]:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as e:
            raise AppError("读取交易网关配置失败 {}: {}".format(
                cfg_path, e))

    @staticmethod
    def _check_live_gate(cfg_data: Dict[str, Any], broker: str) -> None:
        """实盘安全闸门预检（与 tg/brokers/simnow.py 内部判定同口径）。

        broker=live 或 broker_params.tq_market≠simnow → 实盘意图，
        必须显式 confirm_live_trading=true 才允许拉起自动下单子进程。
        非 simnow/live（如 dry_run 离线模拟）不经任何实盘路由，直接放行
        ——此前无此短路，用户设 broker=dry_run 但 tq_market 填了期货公司名时
        dry_run 会被实盘闸门误拦（P2-1）。
        """
        if broker not in ("simnow", "live"):
            return
        bp = cfg_data.get("broker_params") or {}
        market = str(bp.get("tq_market") or "simnow").strip().lower()
        is_live = broker == "live" or market != "simnow"
        if not is_live:
            return
        if market == "simnow":
            raise AppError(
                "实盘安全闸门：broker='{}' 但 broker_params.tq_market 仍为 "
                "'simnow'，实盘请填期货公司名（如 '创元期货'）".format(broker))
        if not bool(bp.get("confirm_live_trading")):
            raise AppError(
                "实盘安全闸门未开启：tq_market='{}' 非仿真市场，必须显式设置 "
                "broker_params.confirm_live_trading=true 才能启动实盘自动下单。"
                .format(bp.get("tq_market")))

    @staticmethod
    def _reset_engine_switch(out_dir: str) -> None:
        """开启自动下单：把自动下单子进程 state.db 的 auto_order_enabled 置 True。

        上次关闭把 False 持久化了，直接重启自动下单子进程会保持关闭语义 ——
        这里在拉起前显式恢复为开启，子进程 _restore 读到 True 才正常收信号。
        """
        try:
            s = _engine_store(out_dir)
            s.set_json("auto_order_enabled", True)
            s.close()
        except Exception as e:
            log.info("[AppTrader] 重置自动下单子进程开关失败（新状态目录可忽略）: %s: %s",
                     type(e).__name__, e)

    @staticmethod
    def _read_engine_switch(handle: Optional[_TraderProc]) -> Optional[Dict[str, Any]]:
        if handle is None or not os.path.isdir(handle.out_dir):
            return None
        try:
            s = _engine_store(handle.out_dir)
            enabled = bool(s.get_json("auto_order_enabled", True))
            positions = s.get_json("positions") or []
            s.close()
            return {
                "enabled": enabled,
                "positions_n": len(positions),
                "positions": positions,
            }
        except Exception:
            return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _send_signal_best_effort(handle, sig) -> bool:
    """对子进程发信号，尽力而为（Popen.send_signal 优先，失败退回 os.kill）。"""
    proc = getattr(handle, "proc", None)
    if proc is not None:
        try:
            proc.send_signal(sig)
            return True
        except Exception:
            pass
    pid = getattr(handle, "pid", 0)
    if pid > 0:
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False
    return False


def _taskkill(pid: int) -> None:
    """Windows 兜底：taskkill /F /T 保进程树必死。

    venv 下 Popen.pid 是 shim（python.exe 壳）而非真解释器进程（P1-2）；
    仅 kill shim 可能留下背后真进程成为孤儿（继续连行情/下单）。用 /T 连根杀。
    仅作强杀兜底，正常优雅路径（flag 文件）不走这里。
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, creationflags=0)
    except OSError:
        pass


# 全局单例：一处定义、全局引用（FrontAPI → orch.trader 调用）
trader = AppTrader()

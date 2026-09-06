# -*- coding: utf-8 -*-
"""
App/AppTrader.py —— 自动下单进程托管（交易网关子进程生命周期管理）
=========================================================================
前端期货 K 线页「自动下单」开关 → 本模块启动/停止
trader_gateway/run_gateway.py 子进程（--source sse 实时接入本服务的
SSE 行情流）。

为什么用子进程而不是线程/协程：
  · 交易引擎持有自己的 tqsdk 长连接（CTP），与主服务（FastAPI + SSE
    行情源）的 TqApi 各自独立，互不掐断；
  · 引擎崩溃 / 卡单 / 死循环不影响行情页面；
  · 引擎内部全部是同步阻塞代码（wait_update 循环），塞进线程池会
    与 REST/SSE 抢线程，且无法优雅终止。

开关语义（用户拍板）：
  · 开启（on）→ 拉起子进程，引擎正常接收买卖点信号并交易；
  · 关闭（off）→ SIGTERM 子进程 → run_gateway._stop 回调
    engine.shutdown_and_lock_all()：
      ① auto_order_enabled=False（停止接收买卖点信号）
      ② 簿内所有「未锁定」持仓全部 LOCK（锁仓，落簿 LOCKED 反向仓，
         次日对向信号自动走解锁入场管线）
    → 子进程优雅退出。auto_order_enabled 持久化在 state.db，
    重启服务/引擎仍保持关闭语义（防悄悄重新开跑）。

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

# 让 AppTrader 能直接读 state.db（Store 是纯 sqlite，只读安全）
if _TG_ROOT not in sys.path:
    sys.path.insert(0, _TG_ROOT)

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "auto_trader_state.json")

_DEFAULT_CFG = os.path.join(_TG_ROOT, "config.json")
_DEFAULT_OUT = os.path.join(_TG_ROOT, "state")

# 后端（AppTrader）日志 tee 进 gateway.log 的 handler，路径随每次启停更新
_log_file_handler: Optional[logging.Handler] = None

# 关闭时给子进程的优雅退出宽限（秒）：SIGTERM → shutdown_and_lock_all
# （锁仓每笔 submit 是同步阻塞的，多仓时给足时间）→ 超时再 SIGKILL。
_STOP_TIMEOUT = 20.0


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
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.info("[AppTrader] 写状态文件失败: %s: %s", type(e).__name__, e)


def _engine_store(out_dir: str):
    """按 out_dir 打开引擎 state.db 的 Store（只读场景为主）。"""
    from tg.store import Store
    return Store(os.path.join(out_dir, "state.db"))


class AppTrader:
    """自动下单子进程托管单例。所有操作持进程内锁，串行化启停。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._handle: Optional[_TraderProc] = None
        self._restore_from_file()

    # ---------------- 内部 ----------------
    def _restore_from_file(self) -> None:
        """服务重启后恢复上一次的启动参数（pid 已失效则视为未运行）。"""
        data = _read_state_file()
        pid = int(data.get("pid") or 0)
        if pid <= 0:
            return
        if not _pid_alive(pid):
            # 上次进程已不在（服务重启/引擎退出）：清掉残留状态文件
            log.info("[AppTrader] 上次引擎进程已退出，清理状态（pid=%s）", pid)
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
        """启动自动下单引擎子进程（SSE 实时源，订阅 chan.py 行情流）。

        cfg_path：trader_gateway config.json（缺省 trader_gateway/config.json）；
        out_dir： 引擎状态目录（缺省 cfg.state_dir 或 trader_gateway/state）；
        symbol / freq：订阅的合约与周期（前端开关传当前页面品种；缺省读
            cfg.source，再缺省 KQ.m@CFFEX.IF / 5m）；
        sse_base：行情流地址（前端传 location.origin；缺省读 cfg.source，
            再缺省 http://127.0.0.1:18081）。
        启动前预检：config 存在 + 实盘安全闸门（live 必须 confirm_live_trading）。
        引擎 stdout/stderr 落盘 {out_dir}/gateway.log（异常可查，不再吞掉）。
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
                    "无法创建引擎状态目录 {}: {}: {}".format(
                        out_dir, type(e).__name__, e))
            log_file = os.path.join(out_dir, "gateway.log")
            # 后端（本模块）日志也 tee 进 gateway.log，与引擎日志同一文件
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
            log.info("[AppTrader] 引擎日志: %s", log_file)
            self._engine_log(
                log_file,
                "启动子进程: source=sse symbol={} freq={} sse_base={} "
                "broker={} cmd={}".format(
                    use_symbol, use_freq, use_base, broker, " ".join(cmd)))
            try:
                env = dict(os.environ)
                env["PYTHONUNBUFFERED"] = "1"   # 引擎 stdout 逐行落盘，异常/退出可即查
                with open(log_file, "a", encoding="utf-8") as lf:
                    proc = subprocess.Popen(
                        cmd, cwd=_TG_ROOT,
                        stdout=lf, stderr=subprocess.STDOUT, env=env)
            except OSError as e:
                self._engine_log(log_file, "启动子进程失败: {}: {}".format(
                    type(e).__name__, e))
                raise AppError(
                    "启动交易引擎子进程失败: {}: {}".format(type(e).__name__, e))

            handle = _TraderProc(proc, cfg_path, out_dir,
                                 time.strftime("%Y-%m-%d %H:%M:%S"), broker,
                                 symbol=use_symbol, freq=use_freq,
                                 sse_base=use_base)
            self._handle = handle
            _write_state_file(handle.to_dict())
            log.info("[AppTrader] 引擎子进程已启动 pid=%s", handle.pid)
            self._engine_log(log_file, "子进程已启动 pid={}".format(handle.pid))
            return handle.to_dict()

    def stop(self, timeout: float = _STOP_TIMEOUT) -> Dict[str, Any]:
        """关闭自动下单：SIGTERM → 子进程 shutdown_and_lock_all → 退出。

        超时未退则 SIGKILL（兜底；引擎锁仓是同步阻塞，正常都能在宽限内收尾）。
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
            log_file = os.path.join(handle.out_dir, "gateway.log")
            self._set_engine_log_handler(log_file)
            self._engine_log(log_file, "收到关闭请求 pid={}".format(pid))
            try:
                handle.proc.send_signal(signal.SIGTERM)
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass

            deadline = time.time() + timeout
            exited = False
            while time.time() < deadline:
                if not handle.running:
                    exited = True
                    break
                time.sleep(0.3)

            if not exited:
                log.warning("[AppTrader] 引擎 pid=%s 未在 %.0fs 内退出，SIGKILL 兜底",
                            pid, timeout)
                try:
                    handle.proc.kill()
                except Exception:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                try:
                    handle.proc.wait(timeout=5)
                except Exception:
                    pass

            # 拿退出码（定位"自动退出"问题：非 0 说明引擎主循环抛异常）
            rc = None
            try:
                rc = handle.proc.wait(timeout=0)
            except Exception:
                pass

            self._handle = None
            _write_state_file({})
            log.info("[AppTrader] 自动下单已关闭（pid=%s，graceful=%s，rc=%s）",
                     pid, exited, rc)
            self._engine_log(
                log_file,
                "已关闭 pid={} graceful={} rc={}".format(pid, exited, rc))
            return {"running": False, "pid": pid, "graceful": exited, "rc": rc}

    def status(self) -> Dict[str, Any]:
        """自动下单状态（进程 + 引擎开关 + 持仓快照）。"""
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
            # 进程曾启动但已退出：附带 gateway.log 尾部，直接回答"为什么关掉了"
            if handle is not None and not running and log_file:
                tail = self._read_log_tail(str(log_file))
                if tail:
                    base["log_tail"] = tail
                    log.warning(
                        "[AppTrader] 引擎 pid=%s 已退出，日志尾部: %s",
                        handle.pid, tail.replace("\n", " | "))
                    self._engine_log(
                        str(log_file),
                        "检测到引擎 pid={} 已退出".format(handle.pid))
            # 引擎内部状态：尽力读 state.db（引擎写 WAL，并发只读安全）
            base["auto_order"] = self._read_engine_switch(handle)
            return base

    # ---------------- 内部工具 ----------------
    @classmethod
    def _set_engine_log_handler(cls, log_file: str) -> None:
        """把后端（AppTrader logger）的输出 tee 进 gateway.log（路径随启停更新）。

        根 logger 仍按 AppLog 配置打后端终端；本 handler 只让 trader 相关的
        后端日志同时落盘 gateway.log，与引擎日志同一文件定位。
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
        """追加写引擎日志文件（与引擎子进程 stdout 同一文件，定位一体化）。

        带 [AppTrader] 前缀以示来自进程托管层；引擎自身的输出为裸行。
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
        """读引擎日志尾部若干行（进程意外退出时带回前端，帮助定位）。"""
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""

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
        必须显式 confirm_live_trading=true 才允许拉起引擎。
        """
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
        """开启自动下单：把引擎 state.db 的 auto_order_enabled 置 True。

        上次关闭把 False 持久化了，直接重启引擎会保持关闭语义 ——
        这里在拉起前显式恢复为开启，子进程 _restore 读到 True 才正常收信号。
        """
        try:
            s = _engine_store(out_dir)
            s.set_json("auto_order_enabled", True)
            s.close()
        except Exception as e:
            log.info("[AppTrader] 重置引擎开关失败（新状态目录可忽略）: %s: %s",
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


# 全局单例：一处定义、全局引用（FrontAPI → orch.trader 调用）
trader = AppTrader()

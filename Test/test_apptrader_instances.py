# -*- coding: utf-8 -*-
"""
多实例托管契约测试（2026-09-29 设计定稿
Docs/多实例自动下单_设计兼交接文档_20260929.md §3.1/§3.2/§3.3/§3.10.2/§3.10.3）
=========================================================================
覆盖（全部走内存伪造子进程，不真拉起引擎、不连柜台）：
  · 实例键 = 品种键：同品种同登录方式重复 start → 幂等绑定（idempotent_bind）；
  · 品种互斥：同品种另一登录方式的 start 被拒，文案带在跑账户与周期；
  · ⑴ 多品种并跑：注册表各持有实例，目录按 State/<登录方式>/<品种键> 两级；
  · stop(symbol) 只停指定品种（目录保留 = 已停止实例），stop() 无参停全部；
  · 顶层 running = 任一在跑 + 停"最后加入的那个"时镜像保留（兼容投影不塌）；
  · 托管记录列表：trader_launch_record.json = {"instances": [...]}；
  · ack 水位线广播写全部实例目录；
  · 买卖点类型过滤广播写 + 新实例启动继承；
  · status() 带 instances[] 逐一投影；
  · ledger() 按 登录方式 → 品种 分组、已停止实例 alerts/toasts 置空。

脚本式测试：python Test/test_apptrader_instances.py（sys.exit(1) = 失败）。
"""
import logging as _logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from App import AppTrader as AT  # noqa: E402
# 先于任何 subprocess.Popen 补丁导入：asyncio.windows_utils 在首次导入期
# 执行 class Popen(subprocess.Popen)，若此时 Popen 已被测试换成函数会炸。
from Trading.Config import TradingConfig  # noqa: E402,F401

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = (got == want)
    if ok:
        _PASS += 1
        print("  [PASS] {}".format(name))
    else:
        _FAIL += 1
        print("  [FAIL] {} -> got {!r}, want {!r}".format(name, got, want))


class _FakeProc:
    """伪造子进程：pid = 本进程（存活），poll 永不退出。"""

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.args = cmd
        self.pid = os.getpid()
        self.returncode = None

    def poll(self):
        return None

    def send_signal(self, sig):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.wait()
        return False

    def communicate(self, input=None, timeout=None):
        return b"", b""


class _ExitOnFlagProc:
    """见 {out_dir}/.stop_request 出现即退出的伪进程（graceful 路径覆盖）。"""

    def __init__(self, cmd, out_dir):
        self.cmd = cmd
        self.pid = os.getpid()
        self.returncode = None
        self._flag = os.path.join(out_dir, ".stop_request")

    def poll(self):
        return 0 if os.path.exists(self._flag) else None

    def send_signal(self, sig):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def communicate(self, input=None, timeout=None):
        return b"", b""


class _Store:
    """_engine_store 的桩：内存 kv，按目录隔离（替代真实 sqlite）。"""

    data = {}  # out_dir -> {key: value}

    def __init__(self, out_dir):
        self.out_dir = out_dir
        _Store.data.setdefault(out_dir, {})
        # 触碰真实文件：生产逻辑的 isfile 门依赖目录里有 state.db
        try:
            os.makedirs(out_dir, exist_ok=True)
            open(os.path.join(out_dir, "state.db"), "ab").close()
        except OSError:
            pass

    def get_json(self, key, default=None):
        return _Store.data[self.out_dir].get(key, default)

    def set_json(self, key, value):
        _Store.data[self.out_dir][key] = value

    def delete_key(self, key):
        _Store.data[self.out_dir].pop(key, None)

    def trades(self):
        return []

    def close(self):
        pass


def main():
    from App.AppTrader import AppTrader, _TraderProc
    at = AppTrader

    def new_trader():
        return AppTrader()

    with _tmpdir() as tmp:
        root = os.path.join(tmp, "State")
        state_file = os.path.join(tmp, "trader_launch_record.json")

        orig = (AT._STATE_FILE, AT.AppTrader._load_cfg,
                AT.subprocess.Popen, AT._engine_store,
                AT._scan_state_dirs)
        try:
            AT._STATE_FILE = state_file
            AT.AppTrader._load_cfg = staticmethod(lambda: _cfg())
            def _popen(cmd, **kw):
                _Store.data.setdefault("__cmd__", []).append(cmd)
                out_dir = cmd[cmd.index("--out") + 1]                     if "--out" in cmd else tmp
                return _ExitOnFlagProc(cmd, out_dir)
            AT.subprocess.Popen = _popen
            AT._engine_store = _Store
            AT._scan_state_dirs = staticmethod(
                lambda: sorted(
                    os.path.join(root, link, pk)
                    for link in ("SimNow", "Live")
                    for pk in ("IF", "AU", "TA")
                    if os.path.isdir(os.path.join(root, link, pk))))

            t = new_trader()

            # ── [1] 首次启动：实例建立 + 两级目录布局 ──
            out_if = os.path.join(root, "SimNow", "IF")
            res_if = t.start(out_dir=out_if, symbol="KQ.m@CFFEX.IF",
                             freq="5m", sse_base="http://x", link="simnow")
            check("[1a] start 成功且 instance_key = IF",
                  res_if.get("instance_key"), "IF")
            check("[1b] 注册表含 (simnow, IF)",
                  "IF" in t._instances, True)
            check("[1c] out_dir = State/SimNow/IF（两级布局）",
                  os.path.normpath(res_if.get("out_dir")),
                  os.path.normpath(out_if))

            # ── [2] 同品种同登录方式重复 start → 幂等绑定 ──
            res2 = t.start(out_dir=out_if, symbol="KQ.m@CFFEX.IF",
                           freq="5m", sse_base="http://x", link="simnow")
            check("[2a] 幂等绑定返回同一 pid",
                  (res2.get("pid"), res2.get("instance_key")),
                  (res_if.get("pid"), "IF"))
            check("[2b] 幂等绑定带标记", res2.get("idempotent_bind"), True)
            check("[2c] 注册表仍只有该品种一个实例",
                  len(t._instances), 1)

            # ── [3] 品种互斥：同品种另一登录方式 → 拒绝 ──
            raised = None
            try:
                t.start(out_dir=os.path.join(root, "Live", "IF"),
                        symbol="KQ.m@CFFEX.IF", freq="1m",
                        sse_base="http://x", link="live")
            except AT.BadRequestError as e:
                raised = str(e)
            check("[3a] 品种互斥拒绝", raised is not None, True)
            check("[3b] 文案带在跑账户与周期",
                  (raised or "").find("仿真") >= 0 and (raised or "").find("5m") >= 0,
                  True)

            # ── [4] ⑴ 多品种并跑：AU 实例建立，注册表 2 个 ──
            out_au = os.path.join(root, "SimNow", "AU")
            t.start(out_dir=out_au, symbol="KQ.m@SHFE.AU",
                    freq="15m", sse_base="http://x", link="simnow")
            check("[4a] 注册表 2 个实例（IF、AU）",
                  sorted(t._instances), ["AU", "IF"])
            check("[4b] AU 目录两级布局",
                  os.path.isdir(os.path.join(root, "SimNow", "AU")), True)

            # ── [5] status() 带 instances[] 逐一投影 ──
            st = t.status()
            check("[5a] status.instances 长度 = 2",
                  len(st.get("instances") or []), 2)
            check("[5b] instances 各含 instance_key/auto_order",
                  all("instance_key" in i and "auto_order" in i
                      for i in st.get("instances") or []), True)
            check("[5c] 顶层 running = 任一在跑", st.get("running"), True)

            # ── [6] 托管记录列表形态 ──
            rec = _read_json(state_file)
            check("[6a] 记录 = instances 列表",
                  isinstance(rec.get("instances"), list), True)
            check("[6b] 列表长度 = 2", len(rec.get("instances") or []), 2)

            # ── [7] 过滤广播：写全部实例目录 ──
            r = t.set_bsp_filter(types={"0": True, "1": True,
                                        "2": True, "3": False})
            check("[7a] 主写入目录 = 主实例", r.get("out_dir"), out_au)
            check("[7b] 广播目录数 = 2", r.get("written_dirs"), 2)
            check("[7c] IF 库也收到过滤值",
                  _Store.data[out_if].get("bsp_type_filter"),
                  {"0": True, "1": True, "2": True, "3": False})

            # ── [8] ack 广播：两目录各有 1 条告警 → 全部确认 ──
            for d in (out_if, out_au):
                _Store.data[d]["alerts"] = [{"ts": 100.0, "code": "x"}]
            r = t.ack_alerts(ts=200.0)
            check("[8a] ack 广播确认 2 条", r.get("acked"), 2)

            # ── [9] 停 IF（目录保留 = 已停止实例），AU 不受影响 ──
            r = t.stop(symbol="KQ.m@CFFEX.IF", timeout=0.1)
            check("[9a] 返回 running=False", r.get("running"), False)
            check("[9b] 注册表只剩 AU", sorted(t._instances), ["AU"])
            check("[9c] IF 目录保留（已停止实例）",
                  os.path.isdir(out_if), True)

            # ── [10] ledger()：全量分区显示（运行中 + 已停止）──
            _Store.data[out_if]["positions"] = [
                {"side": "LONG", "volume": 2, "entry_price": 3856.2}]
            led = t.ledger()
            simnow = next((g for g in led.get("groups") or []
                           if g.get("label") == "SimNow"), None)
            check("[10a] ledger 有 SimNow 分区", simnow is not None, True)
            by_pk = {i.get("product_key"): i
                     for i in (simnow or {}).get("items") or []}
            check("[10b] SimNow 区含 IF 与 AU",
                  sorted(by_pk), ["AU", "IF"])
            check("[10c] IF 为已停止实例（目录在、进程停）",
                  by_pk.get("IF", {}).get("running"), False)
            check("[10d] AU 为运行中实例",
                  by_pk.get("AU", {}).get("running"), True)
            check("[10e] 已停止实例的持仓投影保留（账本停机可见）",
                  by_pk.get("IF", {}).get("positions"),
                  [{"side": "LONG", "volume": 2, "entry_price": 3856.2}])

            # ── [11] 已停止实例重新以另一登录方式启动 → 换账户成功 ──
            out_if_live = os.path.join(root, "Live", "IF")
            res3 = t.start(out_dir=out_if_live, symbol="KQ.m@CFFEX.IF",
                           freq="1m", sse_base="http://x", link="live")
            check("[11a] 换账户启动成功", res3.get("instance_key"), "IF")
            check("[11b] 落 State/Live/IF",
                  os.path.normpath(res3.get("out_dir")),
                  os.path.normpath(out_if_live))
            check("[11c] 注册表 2 个实例（IF@live、AU；IF@simnow 已出表）",
                  sorted(t._instances), ["AU", "IF"])

            # ── [12] 过滤启动继承：Live/IF 无过滤值 → 从既有库继承 ──
            check("[12] 新实例继承过滤值",
                  _Store.data[out_if_live].get("bsp_type_filter"),
                  {"0": True, "1": True, "2": True, "3": False})

            # ── [13] stop() 无参 = 停全部 ──
            r = t.stop(timeout=0.1)
            check("[13a] 停全部返回 results", "results" in r, True)
            check("[13b] 注册表清空", t._instances, {})
            check("[13c] _handle 兼容镜像清空", t._handle is None, True)

            # ── [15] 优雅退出路径覆盖（评审 P3-1）：伪进程观测到停止旗标
            #    即退出（poll 返回 0），store 预置 auto_order_enabled=False →
            #    graceful 按结果判定为 True（不再走强杀分支）。──
            out_im = os.path.join(root, "SimNow", "IM")
            t.start(out_dir=out_im, symbol="KQ.m@CFFEX.IM",
                    freq="5m", sse_base="http://x", link="simnow")
            _Store.data[out_im]["auto_order_enabled"] = False
            r15 = t.stop(symbol="KQ.m@CFFEX.IM", timeout=2)
            check("[15a] 观测到停止旗标后退出 → graceful=True",
                  r15.get("graceful"), True)
            check("[15b] pid 文件已摘除",
                  os.path.exists(os.path.join(out_im, "gateway.pid")), False)

            # ── [16] 子进程已不在（崩溃 / 被外力杀）时点关闭：仍摘掉 pid 文件 ──
            #    走的是 stop() 的「未在运行」分支。残留 gateway.pid 会被下
            #    一次 start() 的"多 worker 可见性"扫描读到，pid 一旦被系统
            #    复用给无关进程，就会误报「疑似多个后端 worker 同时调度」。
            out_ic = os.path.join(root, "SimNow", "IC")
            t.start(out_dir=out_ic, symbol="KQ.m@CFFEX.IC",
                    freq="5m", sse_base="http://x", link="simnow")
            check("[16a] 启动时写了 pid 文件",
                  os.path.exists(os.path.join(out_ic, "gateway.pid")), True)
            # 伪进程 pid 就是本进程（必存活）；换成几乎不存在的 pid，制造
            # "子进程已经不在了"，让 stop() 落到「未在运行」分支
            t._instances["IC"].pid = 999997
            r16 = t.stop(symbol="KQ.m@CFFEX.IC", timeout=2)
            check("[16b] 走「未在运行」分支", r16.get("note"), "未在运行")
            check("[16c] 「未在运行」分支也摘除 pid 文件",
                  os.path.exists(os.path.join(out_ic, "gateway.pid")), False)

            # ── [17] 停"最后加入的那个实例"：顶层 running 仍须 = 任一在跑，
            #    镜像保留（评审 P1-1 的可复现反例）。此前 stop() 只判镜像自己
            #    的 running 就清 _handle → 顶层 running 假 false，且 pid/out_dir/
            #    symbol/log_tail/exit_rc 全变 null，旧消费方（异常退出弹窗、
            #    登录方式默认项）随之误判。旧用例里"最后加入的实例恰好就是在跑
            #    的那个"（先 IF 后 AU，再停 IF），本分支从未被覆盖 → 假绿。
            out_if17 = os.path.join(root, "SimNow", "IF")
            out_au17 = os.path.join(root, "SimNow", "AU")
            t.start(out_dir=out_if17, symbol="KQ.m@CFFEX.IF",
                    freq="5m", sse_base="http://x", link="simnow")
            t.start(out_dir=out_au17, symbol="KQ.m@SHFE.AU",
                    freq="15m", sse_base="http://x", link="simnow")
            check("[17a] 镜像 = 最后加入的 AU（IF 同时在跑）",
                  (t._handle.product_key, sorted(t._instances)),
                  ("AU", ["AU", "IF"]))
            # 伪进程 pid 恒 = 本进程（见 _ExitOnFlagProc），前面几组 stop 已把该
            # pid 记进"已上报"集合 —— 不清空就测不出"这一次 stop 有没有登记"，
            # 会让下面 [17h] 变成假绿。
            t._exit_logged.clear()
            t.stop(symbol="KQ.m@SHFE.AU", timeout=0.1)
            check("[17b] 停 AU 后注册表只剩 IF", sorted(t._instances), ["IF"])
            check("[17c] 停的是镜像自己 → 镜像**保留**（顶层投影要答'刚停的是谁'）",
                  t._handle is not None and t._handle.product_key, "AU")
            # 停 AU 后的**第一次**轮询才是"本该上报退出"的那一次，必须整段捕获；
            # 只捕获第二拍会假绿（第一拍已把 pid 记进 _exit_logged）。
            _recs = []
            _lh = _logging.Handler()
            _lh.emit = lambda r: _recs.append(r.getMessage())
            AT.log.addHandler(_lh)
            try:
                st17 = t.status()
                st17b = t.status()
            finally:
                AT.log.removeHandler(_lh)
            check("[17d] 顶层 running = 任一在跑（IF 还在跑）",
                  st17.get("running"), True)
            check("[17e] 顶层 symbol = 刚停的 AU（不再退化成 null）",
                  st17.get("symbol"), "KQ.m@SHFE.AU")
            check("[17f] 顶层带出退出码（刚停实例的机器级退出原因）",
                  st17.get("exit_rc"), 0)
            # 主动关闭 ≠ 异常退出：镜像被停后仍保留 → 会命中 status() 的"退出上报"
            # 分支，不登记 _exit_logged 就会 warning 一次"已退出"（谎报 + 噪声）。
            check("[17h] 主动关闭后 status() 不误报'子进程已退出'（含首拍）",
                  [m for m in _recs if "已退出" in m], [])
            check("[17i] 二次轮询仍带回退出信息（观测字段不受一次性上报影响）",
                  st17b.get("exit_rc"), 0)
            t.stop(symbol="KQ.m@CFFEX.IF", timeout=0.1)
            check("[17g] 注册表清空 → 镜像才清",
                  (t._handle, t._instances), (None, {}))

            # ── [14] status()：无实例时 running=False ──
            st2 = new_trader_and_status(new_trader)
            check("[14] 无实例 status.running = False",
                  st2.get("running"), False)
        finally:
            (AT._STATE_FILE,
             AT.AppTrader._load_cfg,
             AT.subprocess.Popen,
             AT.AppTrader._engine_store,
             AT._scan_state_dirs) = orig
            _Store.data.clear()

    print("\n============================================================")
    print("多实例托管契约: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("============================================================")
    sys.exit(1 if _FAIL else 0)


def new_trader_and_status(new_trader):
    t = new_trader()
    return t.status()


def _cfg():
    from Trading.Config import TradingConfig
    return TradingConfig(broker="dry_run", state_dir="State",
                         broker_params={"tq_market": "测试期货",
                                        "confirm_live_trading": True})


def _read_json(path):
    import json
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class _tmpdir:
    """临时目录上下文（退出时清理）。"""

    def __init__(self):
        import tempfile
        self.path = tempfile.mkdtemp(prefix="at_inst_")

    def __enter__(self):
        return self.path

    def __exit__(self, *a):
        import shutil
        shutil.rmtree(self.path, ignore_errors=True)
        return False


if __name__ == "__main__":
    main()

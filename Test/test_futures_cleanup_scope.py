# -*- coding: utf-8 -*-
"""G11 守护：期货退出清理的**作用域纪律**（审计 X1 回归拦截）

## 为什么要这条守护

X1 是本项目审计出的唯一一个 **P0 级、确定性故障**（不是概率性竞争）：
原实现里 `/api/futures/cleanup` 无条件做**全局清扫**并调用 `close_all()`。
后果是——用户在 A 页把期货切回股票，会连带掐断 B、C、D 页正在跑的期货
SSE 流、清空它们的 K 线记录缓存、抹掉下窗 chan 与选点。**每次必错**。

修复（提交 a334284）的思路不是加锁——锁解决不了作用域错配（见指导书
维度 0.2 模式 B），而是**收窄作用域**：
  · 会话自有资源（TqApi 连接 / 本连接 K 线记录 / 本连接下窗 chan）
    由该会话自己的生成器 finally 回收；
  · 全局清扫只在 `active_session_count() == 0`（无人持有）时执行。

## 这条守护拦的是什么

修复落地后**没有任何一条测试拦着它回潮**。也就是说：谁要是把守卫删了、
把 `futures_cleanup` 改回无条件清扫、或重新在里面调 `close_all()`，
CI 会全程绿灯，故障直接进生产。本用例把这几条都钉死。

## 核心安全不变式

    swept == True  ⟹  active_sessions == 0
    futures_cleanup() 永远不得触碰在册会话（不关流、不注销）

运行：把本文件放在仓库 Test/ 下，在仓库根目录执行
    python Test/test_futures_cleanup_scope.py
退出码：0 = 全部通过；1 = 有守护失败
"""
import ast
import contextlib
import inspect
import logging
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # Test/ 同目录
import _stub_env                                                  # noqa: E402
_stub_env.install()      # 缺三方依赖时兜底；环境齐全则零介入

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from App import AppSSE                                   # noqa: E402
from App.AppData import app_data                         # noqa: E402
from DataAPI import TqSdkCSSESource as Src               # noqa: E402

# ── 结果记录（对齐仓库既有守护用例的 rec 风格）──────────────────────
_RESULTS = []
_MISSING = object()


def rec(tag, name, ok, detail=""):
    _RESULTS.append((tag, name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {tag} {name}")
    if detail:
        print(f"         {detail}")


# ── 会话桩 ────────────────────────────────────────────────────────────
class _FakeSession:
    """最小会话桩：只需能进 _ACTIVE_SOURCES 并支持 close() 探spy。"""

    def __init__(self, name):
        self.name = name
        self.closed = False
        self._api_closed = threading.Event()

    def close(self):
        self.closed = True


def _register(n):
    """向全局注册表塞 n 个会话桩，返回列表（调用方负责 _unregister）。"""
    srcs = [_FakeSession(f"page-{i}") for i in range(n)]
    with Src._ACTIVE_SOURCES_LOCK:
        Src._ACTIVE_SOURCES.update(srcs)
    return srcs


def _unregister(srcs):
    with Src._ACTIVE_SOURCES_LOCK:
        for s in srcs:
            Src._ACTIVE_SOURCES.discard(s)


@contextlib.contextmanager
def _empty_registry():
    """借出并清空全局会话注册表，退出时**原样归还**。

    不这样做的话，别的用例/真实服务器残留的会话会让本用例的
    「无活跃会话」断言产生假阴性。
    """
    with Src._ACTIVE_SOURCES_LOCK:
        saved = set(Src._ACTIVE_SOURCES)
        Src._ACTIVE_SOURCES.clear()
    try:
        yield
    finally:
        with Src._ACTIVE_SOURCES_LOCK:
            Src._ACTIVE_SOURCES.clear()
            Src._ACTIVE_SOURCES.update(saved)


@contextlib.contextmanager
def _sweep_spies():
    """监听三类清扫副作用，并顶替 CTqSdkAPI 以便计数。

    退出时**精确还原**：实例属性原本不存在就删掉，存在就还原原值——
    避免把桩永久留在 app_data 单例上污染后续用例。
    """
    calls = {"clear_all_cache": 0, "futures_cache_clear": 0, "clear_points": 0}

    class _FakeCTqSdkAPI:
        FREQ_SEC_MAP = {}
        FREQ_LABEL_CN = {}
        FUTURES_ALIASES = {}

        @staticmethod
        def clear_all_cache():
            calls["clear_all_cache"] += 1

    patched = ("futures_cache_clear", "clear_saved_points_by_prefix")
    saved = {n: getattr(app_data, n, _MISSING) for n in patched}
    saved_inst = {n: app_data.__dict__.get(n, _MISSING) for n in patched}
    real_ctq = AppSSE.CTqSdkAPI

    AppSSE.CTqSdkAPI = _FakeCTqSdkAPI
    app_data.futures_cache_clear = lambda: calls.__setitem__(
        "futures_cache_clear", calls["futures_cache_clear"] + 1)

    def _fake_clear_points(prefix):
        calls["clear_points"] += 1
        return 0

    app_data.clear_saved_points_by_prefix = _fake_clear_points
    try:
        yield calls
    finally:
        AppSSE.CTqSdkAPI = real_ctq
        for n in patched:
            if saved_inst[n] is _MISSING:
                app_data.__dict__.pop(n, None)
            else:
                app_data.__dict__[n] = saved_inst[n]
            del saved[n]      # 防止误用


# ── ① 有活跃会话 → 必须跳过清扫 ───────────────────────────────────────
def test_skip_when_sessions_active():
    srcs = _register(2)
    try:
        with _sweep_spies() as calls:
            r = AppSSE.futures_cleanup()
        ok_flag = (r.get("swept") is False) and (r.get("active_sessions") == 2)
        rec("①", "有活跃会话时跳过全局清扫", ok_flag,
            f"返回={r}（期望 swept=False, active_sessions=2）")
        no_side_effect = sum(calls.values()) == 0
        rec("①", "跳过时未执行任何清扫动作", no_side_effect,
            f"清扫副作用计数={calls}（期望全 0：K线缓存/选点/期货分析缓存都没被动）")
    finally:
        _unregister(srcs)


# ── ② 无活跃会话 → 必须执行清扫 ───────────────────────────────────────
def test_sweep_when_no_sessions():
    with _empty_registry():
        with _sweep_spies() as calls:
            r = AppSSE.futures_cleanup()
        ok_flag = (r.get("swept") is True) and (r.get("active_sessions") == 0)
        rec("②", "无活跃会话时执行孤儿清扫", ok_flag,
            f"返回={r}（期望 swept=True, active_sessions=0）")
        swept_all = (calls["clear_all_cache"] >= 1
                     and calls["futures_cache_clear"] >= 1
                     and calls["clear_points"] >= 1)
        rec("②", "清扫覆盖 K线缓存/选点/期货分析缓存", swept_all,
            f"清扫副作用计数={calls}（期望三项各 ≥1）")


# ── ③ X1 核心：cleanup 绝不可触碰他页会话 ─────────────────────────────
def test_cleanup_never_closes_other_pages():
    """「跨页核弹」回归点：页面级 REST 端点不得关掉别的页面的 SSE 流。

    有人把 futures_cleanup 改回调用 close_all() 的那一刻，本用例变红。
    """
    srcs = _register(3)
    try:
        with _sweep_spies():
            AppSSE.futures_cleanup()
            AppSSE.futures_cleanup()      # 幂等：连点两次同样不得误伤
        not_closed = all(not s.closed for s in srcs)
        rec("③", "cleanup 未调用任何会话的 close()", not_closed,
            f"被关闭的会话数={sum(1 for s in srcs if s.closed)}/3（期望 0）")
        with Src._ACTIVE_SOURCES_LOCK:
            still = [s for s in srcs if s in Src._ACTIVE_SOURCES]
        rec("③", "cleanup 未注销任何在册会话", len(still) == 3,
            f"仍在册={len(still)}/3（期望 3：会话回收归各生成器 finally）")
    finally:
        _unregister(srcs)


# ── ④ 安全不变式：swept=True ⟹ active_sessions==0（并发下自洽）────────
def test_concurrent_cleanup_invariant():
    """并发连点 cleanup（含与会话注册交错），返回值必须始终自洽。"""
    srcs = _register(4)
    results = []
    errors = []
    lock = threading.Lock()
    barrier = threading.Barrier(9)

    def caller():
        try:
            barrier.wait(timeout=10)
            for _ in range(40):
                r = AppSSE.futures_cleanup()
                with lock:
                    results.append((r.get("swept"), r.get("active_sessions")))
        except Exception as exc:                      # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    def flipper():
        """反复注销/重注册，制造注册表的动态交错（自持列表，不留残桩）。"""
        mine = []
        try:
            barrier.wait(timeout=10)
            for _ in range(20):
                _unregister(mine)
                mine = _register(4)
        except Exception as exc:                      # noqa: BLE001
            with lock:
                errors.append(f"flipper {type(exc).__name__}: {exc}")
        finally:
            _unregister(mine)

    try:
        with _sweep_spies():
            threads = [threading.Thread(target=caller) for _ in range(8)]
            threads.append(threading.Thread(target=flipper))
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)

        rec("④", "并发 cleanup 无异常", not errors,
            f"错误={errors[:3] if errors else '无'}")
        violations = [(s, a) for (s, a) in results if s and (a or 0) > 0]
        rec("④", "不变式 swept=True ⟹ active_sessions==0", not violations,
            f"样本={len(results)} 条，违反={len(violations)} 条"
            + (f" 例：{violations[:2]}" if violations else "（期望 0）"))
    finally:
        _unregister(srcs)
        with Src._ACTIVE_SOURCES_LOCK:
            for s in list(Src._ACTIVE_SOURCES):
                if isinstance(s, _FakeSession):
                    Src._ACTIVE_SOURCES.discard(s)


def _find_calls(src, func_name):
    """在源码里找**真实调用** `func_name(...)` 的位置（AST 精确匹配）。

    不能用子串匹配：docstring/注释里写「原实现被 close_all() 掐断」会被
    误判成回归。只认 AST 的 Call 节点（Name 或 obj.attr 形态）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == func_name:
            hits.append(node.lineno)
        elif isinstance(fn, ast.Attribute) and fn.attr == func_name:
            hits.append(node.lineno)
    return hits


def _find_calls_in_def(py_path, def_name, func_name):
    """在指定文件的指定函数定义内找 func_name 调用（**不 import** 该文件）。

    FrontAPI 依赖 fastapi，纯净环境 import 不了；改为直接读源码解析，
    这样本守护在无第三方依赖的 CI 上依然有效。
    返回 None 表示无法判定（文件缺失 / 找不到函数）。
    """
    try:
        with open(py_path, "r", encoding="utf-8") as f:
            raw = f.read()
        tree = ast.parse(raw)
    except Exception:                                 # noqa: BLE001
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == def_name:
            seg = ast.get_source_segment(raw, node)
            if seg:
                return _find_calls(seg, func_name)
    return None


# ── ⑤ 源码守卫：防回潮（AST/源码级，改动实现即变红）───────────────────
def test_source_guard_present():
    """行为测试挡不住所有回归形式，补一层源码断言：
    守卫函数必须真的以 active_session_count() 为判据，且清理调用链
    不得出现 close_all（页面级端点调用进程级回收 = X1 本尊）。
    """
    try:
        src = inspect.getsource(AppSSE._cleanup_all_futures_data)
    except Exception as exc:                          # noqa: BLE001
        rec("⑤", "_cleanup_all_futures_data 保有活跃会话守卫", False,
            f"取源码失败: {exc}")
        return
    has_guard = bool(_find_calls(src, "active_session_count"))
    rec("⑤", "_cleanup_all_futures_data 保有活跃会话守卫", has_guard,
        "源码中调用了 active_session_count()"
        if has_guard else "源码中**未调用** active_session_count()，守卫已被移除！")

    # 清理调用链不得出现 close_all 调用（页面级端点调进程级回收 = X1 本尊）
    chain_hits = []
    for fn in (AppSSE.futures_cleanup, AppSSE._cleanup_all_futures_data):
        try:
            chain_hits += _find_calls(inspect.getsource(fn), "close_all")
        except Exception:                             # noqa: BLE001
            pass
    rec("⑤", "清理调用链不含 close_all（页面级不调进程级回收）", not chain_hits,
        "futures_cleanup → _cleanup_all_futures_data 调用链中无 close_all 调用"
        if not chain_hits
        else f"调用链中出现了 close_all 调用，行号={chain_hits}！X1 跨页核弹已回潮")

    # 端点层同样不得直连 close_all（读源码解析，不 import FrontAPI）
    fpath = os.path.join(REPO_ROOT, "FrontAPI.py")
    endpoint_hits = _find_calls_in_def(fpath, "api_futures_cleanup", "close_all")
    if endpoint_hits is None:
        rec("⑤", "REST 端点 /api/futures/cleanup 不直连 close_all", False,
            f"无法解析端点源码：{fpath} 缺失或未找到 api_futures_cleanup")
    else:
        rec("⑤", "REST 端点 /api/futures/cleanup 不直连 close_all",
            not endpoint_hits,
            "端点实现未调用 close_all"
            if not endpoint_hits
            else f"端点实现调用了 close_all，行号={endpoint_hits}！")


def main():
    # 清扫日志在并发用例里会刷出几百行 INFO，淹没断言输出；只看断言。
    logging.getLogger("App.AppSSE").setLevel(logging.WARNING)
    print("=" * 66)
    print("G11 守护 · 期货退出清理作用域（审计 X1 回归拦截）")
    print("=" * 66)
    for fn in (test_skip_when_sessions_active,
               test_sweep_when_no_sessions,
               test_cleanup_never_closes_other_pages,
               test_concurrent_cleanup_invariant,
               test_source_guard_present):
        print(f"\n── {fn.__name__} ──")
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            import traceback
            rec("!!", f"{fn.__name__} 抛异常", False,
                f"{type(exc).__name__}: {exc}")
            traceback.print_exc()

    total = len(_RESULTS)
    failed = [n for (_t, n, ok) in _RESULTS if not ok]
    print("\n" + "=" * 66)
    print(f"合计 {total} 项，通过 {total - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for n in failed:
            print(f"  ✗ {n}")
        print("=" * 66)
        return 1
    print("全部通过：X1 作用域纪律未回潮")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())

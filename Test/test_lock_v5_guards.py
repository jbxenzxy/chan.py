# -*- coding: utf-8 -*-
"""锁改造验证（无第三方依赖，stub 环境下运行）

覆盖本轮改造的四个断言：
  ① tdx_data_context 线程局部注入：并发线程各自拿到自己的数据，互不串
  ② set_replay_mode 线程局部：并发线程复盘调试标志互不干扰
  ③ AppData 锁覆盖：期货缓存三个入口 + 股票下窗缓存三个入口均持锁
  ④ 已删除符号确实不存在（防回潮）
"""
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))            # .../src/Test
_REPO_ROOT = os.path.dirname(_HERE)                            # .../src
sys.path.insert(0, _REPO_ROOT)

# 无第三方依赖的环境（CI 沙箱）下，用最小 stub 顶替 pandas / numpy /
# chinese_calendar，使本用例在没有安装生产依赖时也能跑结构性验证。
_STUBS = os.path.join(os.path.dirname(_REPO_ROOT), "stubs")
if os.path.isdir(_STUBS):
    try:
        import pandas  # noqa: F401
    except ImportError:
        sys.path.insert(0, _STUBS)

os.environ.setdefault("SCAN_TASK_DB", os.path.join(_HERE, "_verify_scan_tasks.db"))

from Common.CEnum import KL_TYPE  # noqa: E402
from DataAPI.TdxAPI import CTdxAPI, tdx_data_context  # noqa: E402
from BuySellPoint.BSPointList import (  # noqa: E402
    set_replay_mode, in_replay_mode, CMyBSPointList,
)

results = []


class _CountingLock:
    """计数包装锁：委托给真实锁，统计 acquire 次数。

    _thread.RLock 是 C 扩展类型、属性只读，无法直接 patch acquire，
    故整体替换实例属性为包装对象（生产代码只经 app_data._xxx_lock 取锁）。
    """

    def __init__(self, real):
        self._real = real
        self.count = 0

    def acquire(self, *a, **k):
        self.count += 1
        return self._real.acquire(*a, **k)

    def release(self, *a, **k):
        return self._real.release(*a, **k)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))


# ══ ① 线程局部数据注入 ═══════════════════════════════════════════════
def _worker(idx, barrier, errors, observed):
    mine = [{"dt": f"2026-01-0{idx+1}", "open": idx}]
    try:
        with tdx_data_context(mine):
            barrier.wait()
            # 让出 GIL，制造与其它线程的交叠
            for _ in range(200):
                time.sleep(0)
                api = CTdxAPI(code=f"sh60000{idx}", k_type=KL_TYPE.K_DAY)
                recs = api._get_records()
                if recs != mine:
                    errors.append(f"thread{idx} 读到 {recs}, 期望 {mine}")
                    return
            observed.append(idx)
    except Exception as e:  # noqa: BLE001
        errors.append(f"thread{idx} 异常 {type(e).__name__}: {e}")


def test_tdx_thread_local():
    print("\n① tdx_data_context 线程局部注入")
    # 类变量必须已消失
    check("CTdxAPI 无 _tdx_data 类变量",
          not hasattr(CTdxAPI, "_tdx_data"),
          f"hasattr={hasattr(CTdxAPI, '_tdx_data')}")
    check("CTdxAPI 无 set_data classmethod",
          not hasattr(CTdxAPI, "set_data"))

    n = 8
    barrier = threading.Barrier(n)
    errors, observed = [], []
    threads = [threading.Thread(target=_worker, args=(i, barrier, errors, observed))
               for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    check(f"{n} 线程并发注入无串数据", not errors and len(observed) == n,
          "; ".join(errors[:3]))

    # with 结束后必须还原（线程池复用安全）
    with tdx_data_context([{"dt": "x"}]):
        pass
    api = CTdxAPI(code="sh600000", k_type=KL_TYPE.K_DAY)
    check("离开 with 后数据已还原", api._get_records() == [])

    # 多级别 dict 形态
    with tdx_data_context({KL_TYPE.K_DAY: [{"dt": "d"}],
                           KL_TYPE.K_30M: [{"dt": "m"}]}):
        a = CTdxAPI(code="sh600000", k_type=KL_TYPE.K_DAY)._get_records()
        b = CTdxAPI(code="sh600000", k_type=KL_TYPE.K_30M)._get_records()
        c = CTdxAPI(code="sh600000", k_type=KL_TYPE.K_5M)._get_records()
    check("多级别 dict 按 k_type 取数",
          a == [{"dt": "d"}] and b == [{"dt": "m"}] and c == [])

    # 嵌套：内层退出后必须回到外层值
    with tdx_data_context([{"dt": "outer"}]):
        with tdx_data_context([{"dt": "inner"}]):
            inner = CTdxAPI(code="c", k_type=KL_TYPE.K_DAY)._get_records()
        outer = CTdxAPI(code="c", k_type=KL_TYPE.K_DAY)._get_records()
    check("嵌套注入退出后回到外层",
          inner == [{"dt": "inner"}] and outer == [{"dt": "outer"}])


# ══ ② 复盘调试标志线程局部 ═══════════════════════════════════════════
def test_replay_flag():
    print("\n② set_replay_mode 线程局部")
    check("CMyBSPointList 无 REPLAY_MODE 类变量",
          not hasattr(CMyBSPointList, "REPLAY_MODE"))

    errors = []
    barrier = threading.Barrier(6)

    def w(on):
        try:
            prev = set_replay_mode(on)
            barrier.wait()
            for _ in range(300):
                time.sleep(0)
                if in_replay_mode() != on:
                    errors.append(f"期望 {on} 实到 {in_replay_mode()}")
                    break
            set_replay_mode(prev)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=w, args=(i % 2 == 0,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    check("6 线程并发复盘标志互不干扰", not errors, "; ".join(errors[:3]))

    prev = set_replay_mode(True)
    ok_true = in_replay_mode()
    set_replay_mode(prev)
    check("set_replay_mode 返回原值且可恢复", ok_true is True and in_replay_mode() == prev)

    # 调试输出函数确实读的是线程局部（而非已删除的类变量）
    src = open(os.path.join(_REPO_ROOT, "BuySellPoint", "BSPointList.py"),
               encoding="utf-8").read()
    check("_dbg_bs* 改读 in_replay_mode()",
          "in_replay_mode()" in src and "self.REPLAY_MODE" not in src)


# ══ ③ AppData 锁覆盖 ═════════════════════════════════════════════════
def test_appdata_locks():
    print("\n③ AppData 锁覆盖")
    from App.AppData import app_data  # noqa: E402

    for attr in ("_cache_lock", "_futures_cache_lock", "_user_store_lock"):
        check(f"AppData 持有 {attr}", hasattr(app_data, attr))
    for attr in ("_annotations_lock", "_saved_point_lock"):
        check(f"AppData 已移除 {attr}", not hasattr(app_data, attr))

    # 期货缓存：四个入口都必须进入 _futures_cache_lock
    # （_thread.RLock 的 acquire 不可改写，改为整体替换成计数包装锁）
    real_f = app_data._futures_cache_lock
    app_data._futures_cache_lock = _CountingLock(real_f)
    try:
        app_data.futures_cache_put("k", "v")
        got = app_data.futures_cache_get("k")
        app_data.futures_cache_pop("k")
        app_data.futures_cache_clear()
        check("期货缓存 get/put/pop/clear 全部持锁",
              app_data._futures_cache_lock.count == 4,
              f"实际持锁 {app_data._futures_cache_lock.count} 次")
    finally:
        app_data._futures_cache_lock = real_f
    check("期货缓存读写正确", got == "v" and app_data.futures_cache_get("k") is None)

    # 股票下窗缓存：三个入口都进入 _cache_lock
    real_c = app_data._cache_lock
    app_data._cache_lock = _CountingLock(real_c)
    try:
        app_data.stocks_sub_cache_put("sh600000", "30m", object())
        app_data.stocks_sub_cache_get("sh600000", "30m")
        app_data.stocks_sub_cache_pop("sh600000", "30m")
        check("股票下窗缓存 get/put/pop 全部持锁",
              app_data._cache_lock.count == 3,
              f"实际持锁 {app_data._cache_lock.count} 次")
    finally:
        app_data._cache_lock = real_c

    # 用户数据：标注 / 选点 / last_code_freq 都进入 _user_store_lock
    real_u = app_data._user_store_lock
    app_data._user_store_lock = _CountingLock(real_u)
    try:
        app_data.save_last_code_freq("sh600519", "d")
        n1 = app_data._user_store_lock.count
        app_data._user_store_lock.count = 0
        app_data.save_point_time("sh600519", "贵州茅台", "d", "2024-01-01")
        n2 = app_data._user_store_lock.count
        app_data.add_annotation("sh600519", "d", "2024-01-01", "测试标注")
        n3 = app_data._user_store_lock.count
        check("last_code_freq / 选点 / 标注 均持 user_store_lock",
              n1 >= 1 and n2 >= 1 and n3 >= 1, f"{n1}/{n2}/{n3}")
    finally:
        app_data._user_store_lock = real_u

    # 原子写底座存在
    from App.AppData import _atomic_write_text, safe_write_json_file  # noqa: E402
    check("存在 _atomic_write_text 原子写底座", callable(_atomic_write_text))

    # 原子写：并发读者不应读到半截文件
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "probe.json")
    bad = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                safe_write_json_file(p, {"code": "sh600519", "freq": "d"})
            except Exception as e:  # noqa: BLE001
                bad.append(f"write {type(e).__name__}: {e}")
            try:
                with open(p, encoding="utf-8") as f:
                    import json
                    json.load(f)
            except FileNotFoundError:
                pass
            except Exception as e:  # noqa: BLE001
                bad.append(f"read {type(e).__name__}: {e}")

    tr = threading.Thread(target=reader, daemon=True)
    tr.start()
    time.sleep(1.0)
    stop.set()
    tr.join(5)
    check("并发读写 JSON 无半截文件", not bad, "; ".join(bad[:3]))


# ══ ④ 已删除符号防回潮 ═══════════════════════════════════════════════
def test_removed_symbols():
    print("\n④ 已删除符号防回潮")
    import App.AppEngine as m  # noqa: E402
    import App.AppChart as c  # noqa: E402
    import App.AppScan as s  # noqa: E402
    import App.AppOrch as orch  # noqa: E402

    check("AppEngine 无 _stock_analysis_lock", not hasattr(m, "_stock_analysis_lock"))
    check("AppChart 无 _ENGINE_LOCK", not hasattr(c, "_ENGINE_LOCK"))
    check("AppChart 无 engine_section", not hasattr(c, "engine_section"))
    check("AppScan 无 _scan_lock", not hasattr(s, "_scan_lock"))
    check("AppOrch 无 LOCK_POLICY", not hasattr(orch, "LOCK_POLICY"))
    check("AppOrch 有 SHARED_RESOURCE_REGISTRY",
          hasattr(orch, "SHARED_RESOURCE_REGISTRY"))

    reg = orch.SHARED_RESOURCE_REGISTRY
    check("登记表按资源索引（含保护手段列）",
          all(isinstance(v, tuple) and len(v) == 4 for v in reg.values()),
          f"{len(reg)} 项")
    for key in ("stocks_analysis_cache", "futures_analysis_cache",
                "user_store_files", "scan_pool_singleton", "scan_tasks.db",
                "refresh_status", "CTdxAPI._tdx_data"):
        check(f"登记表含 {key}", key in reg)

    # call_* 漏斗仍在（路由不得直连引擎的约束）
    for name in ("call_analysis", "call_manual_select_point",
                 "call_futures_manual_select_point", "call_compute_red_range_zs"):
        check(f"AppOrch 仍导出 {name}", hasattr(orch, name))

    # 刷新状态 CAS
    import App.AppRefresh as r  # noqa: E402
    check("AppRefresh 有 _refresh_state_lock", hasattr(r, "_refresh_state_lock"))
    st = r.refresh_status()
    check("refresh_status 返回副本（非内部字典）",
          st is not r._refresh_status and isinstance(st, dict))


if __name__ == "__main__":
    test_tdx_thread_local()
    test_replay_flag()
    test_appdata_locks()
    test_removed_symbols()
    failed = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 60)
    print(f"共 {len(results)} 项断言，失败 {len(failed)} 项")
    for n in failed:
        print("  FAIL:", n)
    sys.exit(1 if failed else 0)

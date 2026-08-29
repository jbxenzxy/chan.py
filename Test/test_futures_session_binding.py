# -*- coding: utf-8 -*-
"""
阶段 7.5：P0-1 会话绑定回归用例 —— 期货 SSE 连接自包含
=====================================================================
回归目标（P0-1 修复后必须保持的行为，评审 §5 指出此前无任何覆盖）：

  ① 会话绑定生效：session_context(session) 内实例化的 CTqSdkAPI，
     其 _records_by_symbol / _lock 即 session 的同一对象（is 判定）。
  ② 跨会话隔离：两个独立会话（如两个 SSE 连接）的记录缓存互不可见
     —— 这是 P0-1 的根因场景（修复前类级共享缓存 → 连接间互踩）。
  ③ 同会话共享：同一会话内多次实例化的数据源读写同一份记录缓存。
  ④ 上下文还原：session_context 退出后线程局部还原为 None，
     新实例化回退自有缓存（工具脚本/离线场景不串连）。
  ⑤ 线程局部隔离：不同线程各自绑定自己的会话（threading.local），
     并行连接互不污染；同一会话内多线程经共享 _lock 串行写不丢数据。

不依赖 tqsdk（TqSdkAPI 模块级不 import tqsdk），可离线跑。

运行：python Test/test_futures_session_binding.py
"""
import os
import sys
import threading

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

import typing
if not hasattr(typing, "Self"):
    import typing_extensions
    typing.Self = typing_extensions.Self

from DataAPI.TqSdkAPI import (
    CTqSdkAPI, session_context, session_set, session_clear,
)


class FakeSession:
    """仿真 CTqSdkSession 的记录缓存面（仅需 _records_by_symbol/_lock）"""
    def __init__(self):
        self._records_by_symbol = {}
        self._lock = threading.Lock()


def make_api(session=None):
    """在 session 上下文内（或脱离会话）实例化一个 CTqSdkAPI"""
    if session is not None:
        with session_context(session):
            return CTqSdkAPI("rb", "1m", None, None, None)
    return CTqSdkAPI("rb", "1m", None, None, None)


# ═══════════════════════════════════════════════════════════════════
# ① 绑定生效
# ═══════════════════════════════════════════════════════════════════
def test_session_context_binds_cache():
    s = FakeSession()
    api = make_api(s)
    assert api._records_by_symbol is s._records_by_symbol, "记录缓存未绑定到会话"
    assert api._lock is s._lock, "锁未绑定到会话"


# ═══════════════════════════════════════════════════════════════════
# ② 跨会话隔离（P0-1 根因场景）
# ═══════════════════════════════════════════════════════════════════
def test_cross_session_isolation():
    s1, s2 = FakeSession(), FakeSession()
    api1 = make_api(s1)
    api1.set_data([{"dt": "2026-01-01 09:00:00", "open": 1}], symbol="rb")
    api2 = make_api(s2)
    assert api2.get_data(symbol="rb") == [], \
        "会话2 不应看到 会话1 注入的记录（P0-1 回归：修复前类级共享缓存互踩）"
    api2.set_data([{"dt": "2026-01-01 09:01:00", "open": 2}], symbol="rb")
    assert api1.get_data(symbol="rb")[0]["open"] == 1, \
        "会话1 的记录不应被会话2 覆盖"


# ═══════════════════════════════════════════════════════════════════
# ③ 同会话共享
# ═══════════════════════════════════════════════════════════════════
def test_same_session_shares_cache():
    s = FakeSession()
    a = make_api(s)
    b = make_api(s)
    a.set_data([{"dt": "2026-01-01 09:00:00", "open": 1}], symbol="rb")
    assert b.get_data(symbol="rb")[0]["open"] == 1, "同会话内多次实例化应共享记录缓存"


# ═══════════════════════════════════════════════════════════════════
# ④ 上下文还原 / 脱离会话回退
# ═══════════════════════════════════════════════════════════════════
def test_context_restore_and_fallback():
    s = FakeSession()
    make_api(s)          # 进入并退出 session_context
    api = CTqSdkAPI("rb", "1m", None, None, None)   # 脱离会话
    assert api._records_by_symbol is not s._records_by_symbol, \
        "session_context 退出后应回退自有缓存（防止线程池复用串连）"
    assert api._lock is not s._lock, "退出后锁也应独立"
    # 自有缓存可正常读写（离线工具场景）
    api.set_data([{"dt": "2026-01-01 09:00:00", "open": 1}], symbol="rb")
    assert api.get_data(symbol="rb")[0]["open"] == 1


# ═══════════════════════════════════════════════════════════════════
# ⑤ 线程局部隔离 + 同会话锁串行
# ═══════════════════════════════════════════════════════════════════
def test_thread_local_isolation():
    s1, s2 = FakeSession(), FakeSession()
    seen = {}

    def worker(name, session, val, out):
        # 模拟两条 SSE 连接各自线程：session_set 覆盖线程局部（生成器语义）
        session_set(session)
        api = CTqSdkAPI("rb", "1m", None, None, None)
        api.set_data([{"dt": "2026-01-01 09:00:00", "open": val}], symbol=name)
        out[name] = api.get_data(symbol=name)[0]["open"]
        session_clear()

    t1 = threading.Thread(target=worker, args=("a", s1, 1, seen))
    t2 = threading.Thread(target=worker, args=("b", s2, 2, seen))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert seen == {"a": 1, "b": 2}, "两条线程（连接）各自绑定会话，互不污染"
    # 主线程未绑定 → 回退自有缓存
    main_api = CTqSdkAPI("rb", "1m", None, None, None)
    assert main_api._records_by_symbol is not s1._records_by_symbol
    assert main_api._records_by_symbol is not s2._records_by_symbol


def test_concurrent_writes_share_lock():
    """同一会话内多线程经共享 _lock 写，不丢数据（append_bar 并发）"""
    s = FakeSession()
    api = make_api(s)
    n = 200
    threads = [
        threading.Thread(target=api.append_bar, args=({"dt": f"2026-01-01 09:{i:02d}:00"}, "rb"))
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(api.get_data(symbol="rb")) == n, f"并发写入丢失：{len(api.get_data(symbol='rb'))}/{n}"


# ═══════════════════════════════════════════════════════════════════
# ⑥ 实时流：set_data + append_bar + get_last_n（SSE 生成器路径）
# ═══════════════════════════════════════════════════════════════════
def test_realtime_stream_through_session():
    s = FakeSession()
    api = make_api(s)
    api.set_data([{"dt": f"2026-01-01 09:{i:02d}:00"} for i in range(5)], symbol="rb")
    api.append_bar({"dt": "2026-01-01 09:05:00"}, symbol="rb")
    assert len(api.get_last_n(3, symbol="rb")) == 3
    assert api.get_last_n(1, symbol="rb")[0]["dt"] == "2026-01-01 09:05:00"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)

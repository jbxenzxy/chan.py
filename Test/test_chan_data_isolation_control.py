# -*- coding: utf-8 -*-
"""确定性对照：证明「类变量注入」没有隔离，而「线程局部注入」有

不用调度运气（GIL 切换间隔）制造竞态，而是用事件把两个线程**精确交错**：

    线程 A：set_data(A数据) → 发信号 → 等 B 改完 → 建 CChan → 读数
    线程 B：等 A 的信号 → set_data(B数据) → 通知 A 继续

改造前（类变量）  → A 读到 B 的数据 ❌
改造后（线程局部）→ A 仍读到自己的数据 ✅

这个交错在生产中真实存在：REST 线程池里一个请求在 set_data 之后被抢占，
另一个请求紧接着改写同一个类变量。
"""
import os
import sys
import threading
from datetime import datetime, timedelta

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

from Common.CEnum import AUTYPE, KL_TYPE  # noqa: E402
from Chan import CChan  # noqa: E402
from ChanConfig import CChanConfig  # noqa: E402
from DataAPI import TdxAPI  # noqa: E402

BARS = 90


def make_records(tag, bars=BARS):
    """tag 决定价格水平：A=100 区段，B=200 区段，一眼能分辨读到了谁的数据"""
    base_price = 100.0 if tag == "A" else 200.0
    recs = []
    base = datetime(2025, 1, 1)
    for i in range(bars):
        o = base_price + (i % 7)
        c = base_price + ((i + 1) % 7)
        recs.append({"dt": base + timedelta(days=i), "open": o, "close": c,
                     "high": max(o, c) + 0.5, "low": min(o, c) - 0.5,
                     "vol": 1000 + i, "amount": (o + c) * 1000})
    return recs


# ── 改造前的实现（类变量 + set_data classmethod）────────────────────
class _OldCTdxAPI(TdxAPI.CTdxAPI):
    _tdx_data = None

    @classmethod
    def set_data(cls, data):
        cls._tdx_data = data

    def _get_records(self):
        d = type(self)._tdx_data
        if not d:
            return []
        return d.get(self.k_type, []) if isinstance(d, dict) else d


def interleave(build_chan, inject, label):
    """交错执行，返回线程 A 建链后读到的末根收盘价"""
    ev_a_injected = threading.Event()
    ev_b_done = threading.Event()
    a_close = {}

    def thread_a():
        data = make_records("A")
        inject(data)
        ev_a_injected.set()
        ev_b_done.wait(10)
        chan = build_chan("A_CODE")
        kl = chan[KL_TYPE.K_DAY]
        a_close["close"] = round(kl.lst[-1].lst[-1].close, 4)

    def thread_b():
        ev_a_injected.wait(10)
        inject(make_records("B"))
        ev_b_done.set()

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join(60)
    tb.join(60)
    got = a_close.get("close")
    ok = got is not None and abs(got - 100.0) < 5
    print(f"  {'[隔离]' if ok else '[串数据]'} {label}: A 读到末收 {got} "
          f"（A 数据≈100，B 数据≈200）")
    return ok


def in_band_a(v):
    """A 数据末收落在 [100,107)，B 数据落在 [200,207)"""
    return v is not None and 100.0 <= v < 108.0


def in_band_b(v):
    return v is not None and 200.0 <= v < 208.0


def main():
    print("确定性交错对照（A 注入 → B 覆盖 → A 建链）")
    print("A 数据末收=106（价格带 100~107），B 数据末收=206（价格带 200~207）\n")

    # ① 改造后：线程局部注入 + 自定义数据源子类
    def inject_new(data):
        return TdxAPI.tdx_data_context(data)

    def build_new_ctx(code):
        cfg = CChanConfig()
        cfg.kl_data_check = False
        with TdxAPI.tdx_data_context(make_records("A")):
            chan = CChan(code=code, begin_time=None, end_time=None,
                         data_src="custom:TdxAPI.CTdxAPI",
                         lv_list=[KL_TYPE.K_DAY], config=cfg,
                         autype=AUTYPE.NONE, market_type="stock")
            for _s in chan.step_load():
                pass
        return chan

    # 用包一层的方式让 A 的注入跨越 B 的注入
    ctx_holder = {}

    def a_body_new():
        data = make_records("A")
        ctx = TdxAPI.tdx_data_context(data)
        ctx.__enter__()
        ctx_holder["ctx"] = ctx
        return ctx

    ev_a, ev_b = threading.Event(), threading.Event()
    res_new = {}

    def thread_a_new():
        ctx = a_body_new()
        ev_a.set()
        ev_b.wait(10)
        cfg = CChanConfig()
        cfg.kl_data_check = False
        chan = CChan(code="A_CODE", begin_time=None, end_time=None,
                     data_src="custom:TdxAPI.CTdxAPI",
                     lv_list=[KL_TYPE.K_DAY], config=cfg,
                     autype=AUTYPE.NONE, market_type="stock")
        for _s in chan.step_load():
            pass
        kl = chan[KL_TYPE.K_DAY]
        res_new["close"] = round(kl.lst[-1].lst[-1].close, 4)
        ctx.__exit__(None, None, None)

    def thread_b_new():
        ev_a.wait(10)
        with TdxAPI.tdx_data_context(make_records("B")):
            pass
        ev_b.set()

    ta = threading.Thread(target=thread_a_new)
    tb = threading.Thread(target=thread_b_new)
    ta.start(); tb.start(); ta.join(60); tb.join(60)
    got_new = res_new.get("close")
    ok_new = in_band_a(got_new)
    print(f"  {'[隔离 ✅]' if ok_new else '[串数据 ❌]'} 改造后（线程局部注入）: "
          f"A 读到末收 {got_new}")

    # ② 改造前：类变量注入
    #    用独立子类 + 自己的 data_src，避免污染真实 CTdxAPI
    import DataAPI.TdxAPI as _mod
    _mod._OldCTdxAPI = _OldCTdxAPI
    res_old = {}
    ev_a2, ev_b2 = threading.Event(), threading.Event()

    def thread_a_old():
        _OldCTdxAPI.set_data(make_records("A"))
        ev_a2.set()
        ev_b2.wait(10)
        cfg = CChanConfig()
        cfg.kl_data_check = False
        chan = CChan(code="A_CODE", begin_time=None, end_time=None,
                     data_src="custom:TdxAPI._OldCTdxAPI",
                     lv_list=[KL_TYPE.K_DAY], config=cfg,
                     autype=AUTYPE.NONE, market_type="stock")
        for _s in chan.step_load():
            pass
        kl = chan[KL_TYPE.K_DAY]
        res_old["close"] = round(kl.lst[-1].lst[-1].close, 4)

    def thread_b_old():
        ev_a2.wait(10)
        _OldCTdxAPI.set_data(make_records("B"))
        ev_b2.set()

    ta = threading.Thread(target=thread_a_old)
    tb = threading.Thread(target=thread_b_old)
    ta.start(); tb.start(); ta.join(60); tb.join(60)
    got_old = res_old.get("close")
    ok_old = in_band_a(got_old)
    leaked_to_b = in_band_b(got_old)
    print(f"  {'[隔离 ✅]' if ok_old else '[串数据 ❌]'} 改造前（类变量注入）: "
          f"A 读到末收 {got_old}"
          + ("（读到了 B 的数据）" if leaked_to_b else ""))

    print()
    if ok_new and not ok_old:
        print("结论：对照成立 —— 类变量注入确实串数据，线程局部注入确实隔离。")
        print("      verify_chan_concurrency.py 的判据对这个差异敏感（非空测试）。")
        return 0
    print("结论：对照未达预期 ——", f"new_ok={ok_new} old_ok={ok_old}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
增量喂入 —— 行为回归守护用例
=====================================================================
背景：SSE 每根新完成K线原先走 `_drain_chan(chan)` →
`CChan.step_load()`（首行 `do_init()` 清空 + 从第一根全量重跑）。
10000 根窗口下单根 3.8s，且同步阻塞 SSE 生成器，是「新K线延迟约 20 秒」
的根因（详见《期货1分钟新K线延迟定位报告》）。

改法：新K线改走 `CChan.trigger_load({kl_type: [klu]})` 增量喂入，
同窗口实测 0.3ms/根。本用例锁定四件事：

  ① 等价性：逐根增量 ≡ 全量重建（合并K线/笔/线段/中枢/末笔端点 全维度）
  ② 未重建：增量调用不替换 `chan.kl_datas[kl_type]` 对象
     （全量 `step_load` 会 `do_init()` 重建，对象身份必变——这是
      「是否悄悄退回全量」最直接的判据，不依赖耗时、不受机器影响）
  ③ 回退：未传 new_bar / kl_type，或增量抛异常 → 仍走 step_load，不崩
  ④ 单一事实源：`CTqSdkAPI.get_kl_data` 必须调用 `build_kline_unit`
     （防止两处各写一份 K 线构造口径而漂移）

运行：python Test/test_chan_incremental_feed.py
"""
import ast
import os
import random
import sys
import threading
from datetime import datetime, timedelta

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    try:
        import typing_extensions
        typing.Self = typing_extensions.Self
    except ImportError:
        pass

from App.AppSSE import _drain_chan, _build_futures_chan
from Common.CEnum import KL_TYPE
from DataAPI.TqSdkAPI import build_kline_unit, session_context


class FakeSrc:
    """最小会话数据源（与生产 CTqSdkSession 的记录缓存接口一致）。"""

    def __init__(self):
        self._records_by_symbol = {}
        self._lock = threading.Lock()

    def set_data(self, records, symbol=None):
        self._records_by_symbol[symbol or "__default__"] = list(records)

    def get_data(self, symbol=None, **kw):
        return list(self._records_by_symbol.get(symbol or "__default__", []))

    def last_records(self, code_key):
        return self._records_by_symbol.get(code_key, [])[-1:]

    def append_bar(self, bar, code_key):
        self._records_by_symbol.setdefault(code_key, []).append(bar)


def _make_records(n, start, px, seed):
    random.seed(seed)
    t, p = start, px
    out = []
    for _ in range(n):
        p += random.uniform(-3, 3)
        o = p
        h = p + abs(random.uniform(0, 4))
        l = p - abs(random.uniform(0, 4))
        c = p + random.uniform(-2, 2)
        out.append({"dt": t, "open": round(o, 1), "high": round(h, 1),
                    "low": round(l, 1), "close": round(c, 1),
                    "vol": random.randint(1, 100), "amount": 0})
        t += timedelta(minutes=1)
    return out


def _sig(chan, kl_type):
    """结构签名：任何一处增量漏算都会让某个维度对不上。"""
    klc = chan.kl_datas[kl_type]
    bi, seg, zs = klc.bi_list, klc.seg_list, klc.zs_list
    last_bi_end = ""
    if len(bi) > 0 and bi[-1].get_end_klu() is not None:
        last_bi_end = bi[-1].get_end_klu().time.toFmtStr("%Y/%m/%d %H:%M")
    return {"klc": len(klc.lst), "bi": len(bi), "seg": len(seg),
            "zs": len(zs), "last_bi_end": last_bi_end}


class _StubChan:
    """只为判定「走了哪条路径」，不做真实计算。"""

    def __init__(self, fail_trigger=False):
        self.trigger_calls = 0
        self.step_calls = 0
        self.fail_trigger = fail_trigger

    def trigger_load(self, inp):
        self.trigger_calls += 1
        if self.fail_trigger:
            raise ValueError("simulated trigger failure")

    def step_load(self):
        self.step_calls += 1
        return iter(())


def case_dispatch():
    """③ 路径选择：增量优先，缺失/异常回退全量。"""
    bar = {"dt": datetime(2026, 5, 6, 9, 31), "open": 1.0, "high": 2.0,
           "low": 0.5, "close": 1.5, "vol": 1, "amount": 0}

    c = _StubChan()
    _drain_chan(c, KL_TYPE.K_1M, bar)
    assert (c.trigger_calls, c.step_calls) == (1, 0), \
        f"有 new_bar 时应只走增量，实际 trigger={c.trigger_calls} step={c.step_calls}"

    c = _StubChan()
    _drain_chan(c)
    assert (c.trigger_calls, c.step_calls) == (0, 1), "无 new_bar 时应回退全量"

    c = _StubChan()
    _drain_chan(c, None, bar)
    assert (c.trigger_calls, c.step_calls) == (0, 1), "无 kl_type 时应回退全量"

    c = _StubChan(fail_trigger=True)
    _drain_chan(c, KL_TYPE.K_1M, bar)
    assert (c.trigger_calls, c.step_calls) == (1, 1), "增量抛异常后应回退全量"

    c = _StubChan()
    _drain_chan(c, KL_TYPE.K_1M, {"dt": None, "open": 1})
    assert (c.trigger_calls, c.step_calls) == (0, 1), "bar 不可用时应回退全量"


def case_equivalence(n=1200, m=60):
    """① 等价性 + ② 未重建。"""
    symbol, freq_sec = "KQ.m@CFFEX.IF", 60
    code_key = f"{symbol}:{freq_sec}"
    start = datetime(2026, 3, 2, 9, 30)

    base = _make_records(n, start, 4000.0, seed=7)
    news = _make_records(m, base[-1]["dt"] + timedelta(minutes=1),
                         base[-1]["close"], seed=99)

    src = FakeSrc()
    chan, kl_type = _build_futures_chan(base, symbol, freq_sec, src=src)
    klc_id_before = id(chan.kl_datas[kl_type])
    sig_before = _sig(chan, kl_type)

    for bar in news:
        src.append_bar(bar, code_key)
        with session_context(src):
            _drain_chan(chan, kl_type, bar)

    # ② 增量不应触发 do_init 重建
    assert id(chan.kl_datas[kl_type]) == klc_id_before, \
        "kl_datas[kl_type] 对象被替换 ⇒ 实际走了 do_init 全量重建，增量路径失效"

    sig_inc = _sig(chan, kl_type)
    assert sig_inc["klc"] > sig_before["klc"], "增量后合并K线数未增长"

    # ① 与全量重建逐维度对齐
    src_f = FakeSrc()
    chan_f, _ = _build_futures_chan(base + news, symbol, freq_sec, src=src_f)
    sig_full = _sig(chan_f, kl_type)
    assert sig_inc == sig_full, f"增量 ≠ 全量：\n  incr={sig_inc}\n  full={sig_full}"


def case_single_source():
    """④ get_kl_data 必须复用 build_kline_unit（AST 判据，不依赖注释）。"""
    path = os.path.join(REPO_ROOT, "DataAPI", "TqSdkAPI.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CTqSdkAPI":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "get_kl_data":
                    target = sub
                    break
    assert target is not None, "未找到 CTqSdkAPI.get_kl_data"

    called = {n.func.id for n in ast.walk(target)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "build_kline_unit" in called, \
        f"get_kl_data 未复用 build_kline_unit（实际调用={sorted(called)}）⇒ K线构造出现第二份实现"

    # 构造口径本身：同一 record 的字段必须落到 klu 上
    bar = {"dt": datetime(2026, 5, 6, 9, 31), "open": 1.5, "high": 2.5,
           "low": 1.0, "close": 2.0, "vol": 7, "amount": 9}
    klu = build_kline_unit(bar)
    assert klu is not None
    assert (klu.open, klu.high, klu.low, klu.close) == (1.5, 2.5, 1.0, 2.0)
    assert klu.time.toFmtStr("%Y/%m/%d %H:%M") == "2026/05/06 09:31"


def main():
    case_dispatch()
    print("[PASS] ③ 路径选择：增量优先 / 缺失与异常回退全量")
    case_single_source()
    print("[PASS] ④ 单一事实源：get_kl_data 复用 build_kline_unit")
    case_equivalence()
    print("[PASS] ① 增量 ≡ 全量  ② 未触发 do_init 重建")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

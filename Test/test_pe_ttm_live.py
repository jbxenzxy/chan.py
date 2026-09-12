# -*- coding: utf-8 -*-
"""PE-TTM 实时层验证（2026-09 改造）

背景：K 线图左上角 PE-TTM 原读 App/stock_pettm_index.json，而该文件只在点
「刷新」按钮时更新；实际使用中不会每天点刷新 → 页面显示的是陈旧 PE。现改为
「打开 K 线页面（这个标的）时实时取一次」：

  · AppData.get_pe_ttm 常态下经**注入的**取数实现实时取一次——single-flight
    合并并发请求、失败静默降级（保留旧值，不让 K 线接口整体 500）;
  · PE-TTM 不再从落盘缓存加载；该 json 只保存指数归属，并已改名
    stock_index_belong.json（旧文件名自动迁移）;
  · 快照用例注入 _pe 表后置 _pe_loaded=True **冻结**实时层，冻结基线保持确定;
  · 依赖倒置：AppData 不得 import DataAPI（phase5 守卫 ④b），取数实现由
    AppRefresh 在导入时注入。

本用例**完全不联网**：所有取数实现均为打桩。

运行：python Test/test_pe_ttm_live.py        # 校验（非 0 退出即失败）
"""
import json
import os
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

from App.AppData import app_data                            # noqa: E402
from DataAPI import ElTdxAPI, MarketStatsAPI, TxAPI         # noqa: E402

results = []


def rec(tag, name, ok, detail=""):
    results.append((ok, tag, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tag} {name}: {detail}")


class _State:
    """保存 / 恢复 app_data 的 PE / 归属状态与取数实现，避免用例间互相污染。"""

    def __enter__(self):
        self.pe = app_data._pe
        self.pe_loaded = app_data._pe_loaded
        self.belong = app_data._belong
        self.belong_loaded = app_data._belong_loaded
        self.fetcher = app_data._pe_live_fetcher
        return self

    def __exit__(self, *exc):
        app_data._pe = self.pe
        app_data._pe_loaded = self.pe_loaded
        app_data._belong = self.belong
        app_data._belong_loaded = self.belong_loaded
        app_data._pe_live_fetcher = self.fetcher
        return False


def test_live_fetch():
    """① 常态：每次 get_pe_ttm 都实时取一次并写入 _pe"""
    with _State():
        app_data._pe = {}
        app_data._pe_loaded = False
        calls = []

        def fake(market, code):
            calls.append((market, code))
            return {market + code: 19.57}

        app_data.set_pe_ttm_live_fetcher(fake)
        v1 = app_data.get_pe_ttm("sh", "600519")
        v2 = app_data.get_pe_ttm("sh", "600519")
        ok = (v1 == 19.57 and v2 == 19.57 and len(calls) == 2)
        rec("①", "常态每次调用都实时取数", ok,
            f"两次取值={v1}/{v2}，取数实现被调用 {len(calls)} 次（期望 2）")


def test_no_fetcher():
    """② 未注入取数实现：降级为 None，不抛异常"""
    with _State():
        app_data._pe = {}
        app_data._pe_loaded = False
        app_data.set_pe_ttm_live_fetcher(None)
        try:
            v = app_data.get_pe_ttm("sh", "600519")
            rec("②", "未注入取数实现时静默降级", v is None, f"取值={v}（期望 None）")
        except Exception as e:                              # noqa: BLE001
            rec("②", "未注入取数实现时静默降级", False,
                f"抛出 {type(e).__name__}: {e}")


def test_failure_keeps_old():
    """③ 取数失败：保留旧值、不向上抛（K 线接口不应因元数据失败而整体 500）"""
    with _State():
        app_data._pe = {"sh600519": 18.0}
        app_data._pe_loaded = False

        def boom(market, code):
            raise RuntimeError("7709 不可达")

        app_data.set_pe_ttm_live_fetcher(boom)
        try:
            v = app_data.get_pe_ttm("sh", "600519")
            rec("③", "取数失败保留旧值且不抛出", v == 18.0,
                f"取值={v}（期望旧值 18.0）")
        except Exception as e:                              # noqa: BLE001
            rec("③", "取数失败保留旧值且不抛出", False,
                f"抛出 {type(e).__name__}: {e}")


def test_frozen_skips_fetch():
    """④ 冻结态（_pe_loaded=True）：纯点查、不联网（快照用例依赖此语义）"""
    with _State():
        app_data._pe = {"sh600519": 20.0}
        app_data._pe_loaded = True
        called = []

        def fake(market, code):
            called.append(1)
            return {market + code: 99.9}

        app_data.set_pe_ttm_live_fetcher(fake)
        v = app_data.get_pe_ttm("sh", "600519")
        ok = (v == 20.0 and not called)
        rec("④", "冻结态纯点查不联网", ok,
            f"取值={v}（期望注入值 20.0），取数被调用 {len(called)} 次（期望 0）")


def test_single_flight():
    """⑤ single-flight：取数进行中，并发请求不重复联网、且立即返回不排队"""
    with _State():
        app_data._pe = {}
        app_data._pe_loaded = False
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def slow(market, code):
            calls.append(1)
            entered.set()
            release.wait(5)                 # 卡住取数，模拟 ~1.3s 联网
            return {market + code: 7.7}

        app_data.set_pe_ttm_live_fetcher(slow)

        t1 = threading.Thread(target=lambda: app_data.get_pe_ttm("sh", "600519"))
        t1.start()
        entered.wait(5)                     # 确保第一个线程已在取数中

        t0 = time.time()
        v2 = app_data.get_pe_ttm("sh", "600519")       # 并发第二个请求
        blocked_s = time.time() - t0

        release.set()
        t1.join(10)
        v1 = app_data._pe.get("sh600519")
        ok = (len(calls) == 1 and v2 is None and blocked_s < 0.5 and v1 == 7.7)
        rec("⑤", "single-flight：并发不重复取数、不阻塞", ok,
            f"取数次数={len(calls)}（期望 1）；并发请求耗时={blocked_s:.3f}s"
            f"（期望 <0.5，不排队）；并发请求取值={v2}（期望 None）；"
            f"首个请求最终落表={v1}（期望 7.7）")


def test_save_writes_index_only():
    """⑥ 落盘格式：只写 index，键集与值形态精确（不再出现 pe_ttm）"""
    with _State():
        tmpdir = tempfile.mkdtemp()
        p = os.path.join(tmpdir, "stock_index_belong.json")
        _prop = type(app_data).stock_index_belong_file
        type(app_data).stock_index_belong_file = property(lambda self: p)
        try:
            app_data._belong = {"sh600519": "沪深300", "sz000001": "中证500"}
            app_data._belong_loaded = True
            ok_write = app_data.save_index_belong_cache()
            data = json.load(open(p, encoding="utf-8"))
            has_pe = any("pe_ttm" in v for v in data.values()
                         if isinstance(v, dict))
            expect = {"sh600519": {"index": "沪深300"},
                      "sz000001": {"index": "中证500"}}
            ok = (ok_write and not has_pe and data == expect)
            rec("⑥", "落盘只含 index（无 pe_ttm 键）", ok,
                f"写入={ok_write}，含 pe_ttm 键={has_pe}，内容={data}")
        finally:
            type(app_data).stock_index_belong_file = _prop


def test_legacy_migration():
    """⑦ 旧文件迁移：新文件不存在而旧文件存在时，读取并只取 index 字段"""
    with _State():
        tmpdir = tempfile.mkdtemp()
        new_p = os.path.join(tmpdir, "stock_index_belong.json")
        old_p = os.path.join(tmpdir, "stock_pettm_index.json")
        with open(old_p, "w", encoding="utf-8") as f:
            json.dump({"sh600519": {"pe_ttm": 25.3, "index": "沪深300"}}, f)

        _pn = type(app_data).stock_index_belong_file
        _po = type(app_data).legacy_stock_pe_ttm_file
        type(app_data).stock_index_belong_file = property(lambda self: new_p)
        type(app_data).legacy_stock_pe_ttm_file = property(lambda self: old_p)
        try:
            app_data._belong = {}
            app_data._belong_loaded = False
            v = app_data.get_index_belong("sh", "600519")
            migrated = os.path.exists(new_p)
            new_data = (json.load(open(new_p, encoding="utf-8"))
                        if migrated else {})
            ok = (v == "沪深300" and migrated
                  and new_data == {"sh600519": {"index": "沪深300"}})
            rec("⑦", "旧文件→新文件迁移（只取 index）", ok,
                f"读到={v}（期望 沪深300）；新文件已生成={migrated}；"
                f"新文件内容={new_data}")
        finally:
            type(app_data).stock_index_belong_file = _pn
            type(app_data).legacy_stock_pe_ttm_file = _po


def test_source_routing():
    """⑧ 分流单一源：A 股→eltdx、港股→腾讯（打桩验证，不联网）"""
    seen = {}

    def fake_eltdx(pairs):
        seen["eltdx"] = list(pairs)
        return {m + c: 10.0 for m, c in pairs}

    def fake_tx(pairs):
        seen["tx"] = list(pairs)
        return {m + c: 11.0 for m, c in pairs}

    _e, _t = ElTdxAPI.fetch_pe_ttm, TxAPI.fetch_pe_ttm
    ElTdxAPI.fetch_pe_ttm, TxAPI.fetch_pe_ttm = fake_eltdx, fake_tx
    try:
        r_a = MarketStatsAPI.fetch_pe_ttm_live("sh", "600519")
        r_hk = MarketStatsAPI.fetch_pe_ttm_live("hk", "00700")
        ok = (seen.get("eltdx") == [("sh", "600519")]
              and seen.get("tx") == [("hk", "00700")]
              and r_a == {"sh600519": 10.0} and r_hk == {"hk00700": 11.0})
        rec("⑧", "分流：A股→eltdx / 港股→腾讯", ok,
            f"eltdx 收到={seen.get('eltdx')}；腾讯收到={seen.get('tx')}；"
            f"返回={r_a}/{r_hk}")
    finally:
        ElTdxAPI.fetch_pe_ttm, TxAPI.fetch_pe_ttm = _e, _t


def main():
    print("=" * 64)
    print("PE-TTM 实时层验证（2026-09 改造：打开 K 线页面即取数）")
    print("=" * 64)
    for fn in (test_live_fetch, test_no_fetcher, test_failure_keeps_old,
               test_frozen_skips_fetch, test_single_flight,
               test_save_writes_index_only, test_legacy_migration,
               test_source_routing):
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            rec(fn.__name__, "执行异常", False, f"{type(e).__name__}: {e}")
    failed = [r for r in results if not r[0]]
    print("=" * 64)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)}，"
          f"失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

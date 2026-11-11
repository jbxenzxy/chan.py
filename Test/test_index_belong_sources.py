# -*- coding: utf-8 -*-
"""指数归属取数路径验证（2026-11-11 改造：eltdx 主源 / AKShare 兜底）

背景（用户实测日志）：点「刷新」时中证500 反复超时，最终落盘的
stock_index_belong.json 里 500 只中证500 成分股一条不剩。根因不是单一网络
故障，而是四件事叠在一起：

  ① 数据源：AKShare index_stock_cons_csindex 从 csindex 的 OSS 拉一个 xls，
     单请求拿全但无中间粒度；实测同一 URL 连打 8 次耗时 1.75~>20s；
  ② 外层 30s < 内层 akshare 默认 (10, 60) —— 超过 30s 的响应必被丢弃，
     请求线程却继续跑到 60s 才失败（日志里同指数出现两条 ✅ 即由此而来）；
  ③ count=0 也打「✅ 成功获取 0只」，失败与成功不可区分；
  ④ replace_index_belong 是整体替换 → 一个指数失败就把它名下全部归属抹掉。

本用例锁住改造后的四条性质（**全程打桩，不联网**）：
  ① 主源 eltdx 成功时不碰 AKShare（调用计数为 0）；
  ② 主源失败 / 返回空 → AKShare 兜底；
  ③ 条数≠期望（含 0 只）→ 该指数判失败、不进 accepted（旧值可保留）；
  ④ 超整体预算 → 该指数判失败，总耗时不超预算；
  ⑤ 合并语义：成功指数整体换新、失败指数旧值保留；
  ⑥ 静态契约：刷新侧走 merge_index_belong，不再调用 replace_index_belong；
  ⑦ eltdx 侧自检：服务端 total 与实收行数不一致要抛异常（防分页静默截断）。

运行：python Test/test_index_belong_sources.py     # 校验（非 0 退出即失败）
"""
import inspect
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

from App import AppRefresh as R                            # noqa: E402
from App.AppData import app_data                           # noqa: E402
from DataAPI import ElTdxAPI                               # noqa: E402

results = []

# 期望条数由被测模块自己的映射推出，避免本用例另存一份真值
EXPECTED = dict(R._INDEX_BELONG_EXPECTED)
NAMES = dict(R.AKSHARE_INDEX_MAP)          # code -> name


def rec(tag, name, ok, detail=""):
    results.append((ok, tag, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tag} {name}: {detail}")


def _mk_items(n, market="sh", start=600000):
    """造 n 只成分股（代码取 6 位、market 单值，够走通映射与条数校验）。"""
    return [{"code": f"{(start + i) % 1000000:06d}", "market": market}
            for i in range(n)]


class _Patched:
    """替换取数实现 + 隔离 app_data 归属表（退出时原样恢复）。"""

    def __init__(self, eltdx_fn, ak_fn):
        self.eltdx_fn, self.ak_fn = eltdx_fn, ak_fn

    def __enter__(self):
        self._e, self._a = R.fetch_index_members, R.fetch_index_cons
        self._belong, self._loaded = app_data._belong, app_data._belong_loaded
        R.fetch_index_members, R.fetch_index_cons = self.eltdx_fn, self.ak_fn
        return self

    def __exit__(self, *exc):
        R.fetch_index_members, R.fetch_index_cons = self._e, self._a
        app_data._belong, app_data._belong_loaded = self._belong, self._loaded
        return False


class _Counter:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def __call__(self, code):
        self.calls.append(code)
        return self.fn(code)


def test_eltdx_primary():
    """① 主源全成功：4 个指数都从 eltdx 取到，AKShare 一次都不调用。"""
    ak = _Counter(lambda c: (_ for _ in ()).throw(AssertionError("不该走兜底")))
    with _Patched(lambda c: _mk_items(EXPECTED[c]), ak):
        acc, failed = R._fetch_index_belong_from_sources()
    ok = (set(acc) == set(NAMES.values()) and not failed and not ak.calls
          and all(len(acc[NAMES[c]]) == EXPECTED[c] for c in EXPECTED))
    rec("①", "eltdx 主源成功且不碰 AKShare", ok,
        f"成功 {len(acc)} 个指数，兜底调用 {len(ak.calls)} 次，失败 {failed}")


def test_akshare_fallback():
    """② 主源抛异常 → AKShare 兜底，4 个指数仍全部取到。"""
    def down(_code):
        raise RuntimeError("TransportError: 403")

    ak = _Counter(lambda c: _mk_items(EXPECTED[c]))
    with _Patched(down, ak):
        acc, failed = R._fetch_index_belong_from_sources()
    ok = set(acc) == set(NAMES.values()) and not failed and len(ak.calls) == 4
    rec("②", "主源失败走 AKShare 兜底", ok,
        f"成功 {len(acc)} 个，兜底调用 {len(ak.calls)} 次，失败 {failed}")


def test_empty_falls_back():
    """②b 主源返回空（不是抛异常）也走兜底 —— 4 个固定指数返回空必是故障。"""
    ak = _Counter(lambda c: _mk_items(EXPECTED[c]))
    with _Patched(lambda c: [], ak):
        acc, failed = R._fetch_index_belong_from_sources()
    ok = set(acc) == set(NAMES.values()) and not failed and len(ak.calls) == 4
    rec("②b", "主源返回空 → 兜底", ok, f"兜底调用 {len(ak.calls)} 次，失败 {failed}")


def test_count_guard():
    """③ 条数≠期望（含 0 只）判失败 —— 这是「✅ 成功获取 0只」的回潮守卫。"""
    cases = []
    # 0 只：两源都空 → 必须判失败，不能算成功
    with _Patched(lambda c: [], lambda c: []):
        acc, failed = R._fetch_index_belong_from_sources()
    cases.append(("两源皆空", set(acc) == set() and set(failed) == set(NAMES.values())))
    # 截断：中证1000 只回来 800 只 → 判失败，其余指数不受影响
    with _Patched(lambda c: _mk_items(EXPECTED[c] - 200 if c == "000852" else EXPECTED[c]),
                  lambda c: _mk_items(EXPECTED[c])):
        acc, failed = R._fetch_index_belong_from_sources()
    cases.append(("中证1000 截断", "中证1000" in failed and "中证1000" not in acc
                  and len(acc) == 3))
    # 多出来也算异常（例如把别的表当成分股返回）
    with _Patched(lambda c: _mk_items(EXPECTED[c] + 1), lambda c: _mk_items(EXPECTED[c])):
        acc, failed = R._fetch_index_belong_from_sources()
    cases.append(("条数多出", set(acc) == set() and len(failed) == 4))
    ok = all(v for _, v in cases)
    rec("③", "条数校验（0 只/截断/多出）", ok, str(cases))


def test_budget():
    """④ 超出整体预算的指数判失败，总耗时不超预算（预算不是每指数一份）。"""
    def slow(code):
        if code == "000905":
            time.sleep(2.0)
        return _mk_items(EXPECTED[code])

    with _Patched(slow, lambda c: _mk_items(EXPECTED[c])):
        t0 = time.monotonic()
        acc, failed = R._fetch_index_belong_from_sources(timeout=0.6)
        cost = time.monotonic() - t0
    ok = ("中证500" in failed and "中证500" not in acc and cost < 1.5
          and len(acc) == 3)
    rec("④", "整体预算生效", ok, f"耗时 {cost:.2f}s（预算 0.6s），失败 {sorted(failed)}")


def test_merge_semantics():
    """⑤ 成功指数整体换新（调仓可剔除旧票）、失败指数旧值保留。"""
    app_data._belong.clear()
    app_data._belong.update({"sh600000": "中证500", "sh600001": "中证500",
                             "sz000001": "沪深300"})
    app_data.merge_index_belong({"沪深300": {"sh600002": "沪深300"}})
    snap = dict(app_data.belong_snapshot())
    ok_keep = snap.get("sh600000") == "中证500" and snap.get("sh600001") == "中证500"
    ok_new = snap.get("sh600002") == "沪深300"
    ok_drop = "sz000001" not in snap          # 出现的指数整体换新
    app_data.merge_index_belong({"科创50": {"sh688001": "科创50"}})
    snap2 = dict(app_data.belong_snapshot())
    ok_iso = snap2.get("sh688001") == "科创50" and snap2.get("sh600000") == "中证500"
    rec("⑤", "按指数合并不抹别的指数", ok_keep and ok_new and ok_drop and ok_iso,
        f"保留失败指数={ok_keep} 换新={ok_new} 剔除旧成员={ok_drop} 互不影响={ok_iso}")


def test_no_whole_replace():
    """⑥ 静态契约：刷新侧走 merge_index_belong，不再整体替换、不再「✅ 0只」。"""
    src = inspect.getsource(R._refresh_index_belong)
    body = inspect.getsource(R)
    ok = ("merge_index_belong" in src
          and "replace_index_belong" not in body
          and "成功获取 {count}只" not in body)
    rec("⑥", "刷新侧只用合并、不再有「✅ 0只」", ok,
        f"merge 在 _refresh_index_belong 内={'merge_index_belong' in src}，"
        f"残留 replace={'replace_index_belong' in body}")


def test_eltdx_total_mismatch():
    """⑦ eltdx 侧自检：total 与实收行数不一致要抛（防分页静默截断）。"""
    class _Row:
        def __init__(self, code):
            self.code, self.market = code, "sh"

    class _Table:
        def __init__(self, total, rows):
            self.total, self.rows = total, tuple(rows)

    orig = ElTdxAPI._fetch_index_member_table
    try:
        ElTdxAPI._fetch_index_member_table = lambda c: _Table(300, [_Row("600000")] * 298)
        raised = False
        try:
            ElTdxAPI.fetch_index_members("000300")
        except RuntimeError:
            raised = True
        ElTdxAPI._fetch_index_member_table = lambda c: _Table(2, [_Row("600000"), _Row("600001")])
        items = ElTdxAPI.fetch_index_members("000300")
        ok = raised and len(items) == 2 and items[0] == {"code": "600000", "market": "sh"}
    finally:
        ElTdxAPI._fetch_index_member_table = orig
    rec("⑦", "eltdx total 自检 + 返回结构", ok, f"截断抛异常={raised}，正常返回 {len(items)} 条")


def main():
    print("=" * 64)
    print("指数归属取数路径验证（eltdx 主源 / AKShare 兜底）")
    print("=" * 64)
    for fn in (test_eltdx_primary, test_akshare_fallback, test_empty_falls_back,
               test_count_guard, test_budget, test_merge_semantics,
               test_no_whole_replace, test_eltdx_total_mismatch):
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

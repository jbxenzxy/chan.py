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

本用例锁住改造后的下列性质（**全程打桩，不联网**）：
  ① 主源 eltdx 成功时不碰 AKShare（调用计数为 0）；
  ② 主源失败 / 返回空 → AKShare 兜底；
  ③ 条数越界（0 只 / 多出 / 低于期望且超出下界容差）→ 该指数判失败、不进
     accepted（旧值可保留）；
  ③b 合法过滤：`dropped` 随主源返回值透出并与映射后条数一起对齐期望值，
      不误判「成分不全」、不额外联网（回滚自证：换回旧判据本用例必须红）；
  ③c 上下界分开管：少 1~2 只（样本股退市到下次调仓之间）**放行**，少 3 只判
      截断；行数齐但一行都没映射成时**单列原因**，不许落到「超过总预算」；
  ④ 超整体预算 → 该指数判失败，总耗时不超预算；
  ⑤ 合并语义：成功指数整体换新、失败指数旧值保留；
  ⑤b 跨指数重叠票按 per_index 插入顺序后写覆盖（把现状钉住，改语义必须改断言）；
  ⑥ 静态契约：刷新侧走 merge_index_belong，不再调用 replace_index_belong；
  ⑦ eltdx 侧自检：服务端 total 与**实收行数**不一致要抛异常（防分页静默截断）；
     「行被过滤/去重」不算截断（否则主源会被误判失败、静默退回兜底源），
     且过滤行数随返回值透出 `dropped`。
  ⑧ 期望条数与**独立真值**一致（实现改错时本用例必须红）；
  ⑨ 市场判不出的 920 段（北交所新段）**丢票但不写 `sh920736` 错 key** ——
     首位 9→sh 的规则会把 920 段误判成沪市，而落盘只过滤「6 位纯数字」key，
     脏 key 会永久留在 json 里；带 `market="bj"` 的票必须原样保留。
  ⑩ 部分失败上报：`failed` 非空、`accepted` 也非空时，刷新状态里必须留下
     「部分失败」原文（旧实现只 log.warning，UI 照旧显示「刷新完成」）。

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

# 期望条数的**独立真值**（刻意不从被测模块取）：
# 2026-11-11 实测 eltdx index_members 与 AKShare index_stock_cons_csindex 对
# 这 4 个指数返回条数完全一致（300/500/1000/50）。此处另存一份，用于发现
# `_INDEX_BELONG_EXPECTED` 被改错 —— 若从实现推出，改错也没人报警。
EXPECTED_TRUE = {"000300": 300, "000905": 500, "000852": 1000, "000688": 50}
EXPECTED = EXPECTED_TRUE
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
        # 存**内容快照**而不是 dict 引用：用例 ⑤ 用 app_data._belong.clear()
        # 原地清空，按引用恢复等于把被测试数据污染过的同一个对象放回去（假恢复）。
        self._belong_snapshot = dict(app_data._belong)
        self._loaded = app_data._belong_loaded
        R.fetch_index_members, R.fetch_index_cons = self.eltdx_fn, self.ak_fn
        return self

    def __exit__(self, *exc):
        R.fetch_index_members, R.fetch_index_cons = self._e, self._a
        app_data._belong.clear()
        app_data._belong.update(self._belong_snapshot)
        app_data._belong_loaded = self._loaded
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


def test_dropped_accounting():
    """③b 合法过滤不算「成分不全」：dropped 随主源返回值透出，零额外联网。

    主源 300 行里 2 行被过滤 → 映射后 298 只 + dropped=2，合计仍 300，
    必须 accepted 且 AKShare 一次都不调。上界仍是「不许比期望多」（③ 的
    「条数多出」用例仍须全失败），下界留给 `_INDEX_BELONG_TOLERANCE` 只容差，
    因为样本股退市到下次定期调整之间会合法少几只。
    回滚自证：把 `_fetch_one_index_belong` 换回「只看 len(mapping)」的旧
    判据，本用例必须红（沪深300 会被误判失败）。
    """
    def with_dropped(code):
        items = _mk_items(EXPECTED[code])
        if code != "000300":
            return items
        return ElTdxAPI.IndexMemberItems(items[:-2], 2)

    ak = _Counter(lambda c: (_ for _ in ()).throw(AssertionError("不该走兜底")))
    with _Patched(with_dropped, ak):
        acc, failed = R._fetch_index_belong_from_sources()
    ok = (set(acc) == set(NAMES.values()) and not failed and not ak.calls
          and len(acc.get("沪深300", {})) == 298)
    rec("③b", "主源丢弃行计入条数：不误判、不兜底", ok,
        f"沪深300 {len(acc.get('沪深300', {}))} 只（应 298），"
        f"兜底调用 {len(ak.calls)} 次，失败 {failed}")


def test_bj_920_no_bad_key():
    """⑨ 交易所字段缺失的 920 段：丢票，但不写 `sh920736` 这种错 key。

    `_infer_bare_code_market` 按首位 9→sh（那是沪 B 900-909 的规则），北交所
    920 段会被误判成沪市；而 `save_index_belong_cache` 只过滤「6 位纯数字」这一种
    旧格式 key ⇒ 脏 key 会**永久落盘**，K 线页标签取到的是一个不存在的标的。
    4 个固定指数都不含北交所票，故正确处置是丢弃并计入 dropped（条数判据仍是
    「有效 + 丢弃 == 期望」，因此不会误判成成分不全）。
    带正确 `market="bj"` 的票必须原样保留 —— 丢弃条件只针对「市场判不出来」。
    回滚自证：去掉 `bare.startswith("920")` 分支后本用例必须红（出现 sh920736）。
    """
    def dirty(code):
        items = _mk_items(EXPECTED[code])
        if code != "000300":
            return items
        return items[:-1] + [{"code": "920736", "market": ""}]

    def clean_bj(code):
        items = _mk_items(EXPECTED[code])
        if code != "000300":
            return items
        return items[:-1] + [{"code": "920736", "market": "bj"}]

    ak = _Counter(lambda c: (_ for _ in ()).throw(AssertionError("不该走兜底")))
    with _Patched(dirty, ak):
        acc, failed = R._fetch_index_belong_from_sources()
    bad = acc.get("沪深300", {})
    ok_drop = ("sh920736" not in bad and "bj920736" not in bad
               and len(bad) == EXPECTED["000300"] - 1 and not failed)

    with _Patched(clean_bj, ak):
        acc2, failed2 = R._fetch_index_belong_from_sources()
    good = acc2.get("沪深300", {})
    ok_keep = good.get("bj920736") == "沪深300" and len(good) == EXPECTED["000300"]

    rec("⑨", "920 段只丢「市场判不出」的票，不写错 key",
        ok_drop and ok_keep,
        f"脏行：{len(bad)} 只（应 299），sh920736 在表内={'sh920736' in bad}，"
        f"失败={failed}；带 market=bj：{len(good)} 只（应 300），"
        f"bj920736={good.get('bj920736')}，失败={failed2}")


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


def test_merge_overlap():
    """⑤b 跨指数重叠票：单值存储上限，按 per_index 插入顺序后写覆盖。

    把现状钉住：`AppData.merge_index_belong` 的 docstring 已写明这条例外，
    谁改语义（例如「失败指数重叠票不覆盖」）都必须显式改本断言。
    回滚自证：注入「保留失败指数归属」的修正版 merge 后本用例必须红。
    """
    names = list(NAMES.values())
    app_data._belong.clear()
    app_data._belong.update({"sh688001": "科创50", "sz000001": "中证500"})
    app_data.merge_index_belong(
        {"沪深300": {"sh688001": "沪深300", "sh600001": "沪深300"}})
    one = dict(app_data.belong_snapshot())
    ok_one = one.get("sh688001") == "沪深300"         # 成功指数后写覆盖
    ok_keep = one.get("sz000001") == "中证500"         # 失败指数独占票保留

    app_data._belong.clear()
    app_data._belong.update({"sh688001": "科创50"})
    app_data.merge_index_belong({n: {"sh688001": n} for n in names})
    all_ok = dict(app_data.belong_snapshot())
    ok_all = all_ok.get("sh688001") == names[-1]       # 全成功稳态 = 最后一个
    rec("⑤b", "重叠票后写覆盖（单值存储上限）", ok_one and ok_keep and ok_all,
        f"仅沪深300成功={one.get('sh688001')} 全成功={all_ok.get('sh688001')} "
        f"失败指数独占票={one.get('sz000001')}")


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
    """⑦ eltdx 侧自检：真截断抛异常；行被过滤/去重**不算**截断。"""
    class _Row:
        def __init__(self, code, market="sh"):
            self.code, self.market = code, market

    class _Table:
        def __init__(self, total, rows):
            self.total, self.rows = total, tuple(rows)

    orig = ElTdxAPI._fetch_index_member_table
    try:
        # ⑦a 真截断：服务端说 300，只回来 298 行 → 必须抛
        ElTdxAPI._fetch_index_member_table = lambda c: _Table(
            300, [_Row(f"{600000 + i}") for i in range(298)])
        truncated = False
        try:
            ElTdxAPI.fetch_index_members("000300")
        except RuntimeError:
            truncated = True
        # ⑦b 过滤：300 行齐全，其中 1 行 market 表外(hk) + 1 行重复 → **不抛**
        #     （旧实现拿过滤后的 298 比对 total=300，会误报「分页不完整」）
        rows = ([_Row(f"{600000 + i}") for i in range(298)]
                + [_Row("600298", "hk"), _Row("600000", "sh")])
        ElTdxAPI._fetch_index_member_table = lambda c: _Table(300, rows)
        items = ElTdxAPI.fetch_index_members("000300")
        # ⑦b2 丢弃行数必须随返回值透出，App 层才能把「合法过滤」与
        #      「成分不全」分开（见 ③b）；普通 list 路径按 0 处理。
        dropped_ok = getattr(items, "dropped", None) == 2
        filtered_ok = (len(items) == 298
                       and not any(i["code"] == "600298" for i in items)
                       and dropped_ok)
        # ⑦c 正常：market 原样透传
        ElTdxAPI._fetch_index_member_table = lambda c: _Table(
            2, [_Row("600000"), _Row("000001", "sz")])
        norm = ElTdxAPI.fetch_index_members("000300")
        shape_ok = (norm == [{"code": "600000", "market": "sh"},
                             {"code": "000001", "market": "sz"}]
                    and getattr(norm, "dropped", None) == 0)
    finally:
        ElTdxAPI._fetch_index_member_table = orig
    rec("⑦", "eltdx 自检：真截断抛异常 / 过滤不算截断 / dropped 透出 / 返回结构",
        truncated and filtered_ok and shape_ok,
        f"截断抛异常={truncated} 过滤后 {len(items)} 只（应 298）={filtered_ok} "
        f"dropped={getattr(items, 'dropped', None)}(应 2)={dropped_ok} 结构={shape_ok}")


def test_expected_truth():
    """⑧ 实现里的期望条数与独立真值一致（防「改实现顺手改测试」）。"""
    impl = dict(R._INDEX_BELONG_EXPECTED)
    same = impl == EXPECTED_TRUE
    covered = set(NAMES) == set(EXPECTED_TRUE)
    rec("⑧", "期望条数与独立真值一致", same and covered,
        f"实现={impl} 真值={EXPECTED_TRUE} 覆盖全部指数={covered}")


def test_partial_failure_reports():
    """⑩ 部分失败挂到刷新状态（改造前只在「全部失败」时报错）。

    `_refresh_index_belong` 的「全部失败」分支早已上报；缺口在**部分失败**：
    4 个指数挂了 1 个时只 log.warning，UI 照旧「刷新完成」，用户以为归属已
    是最新。本用例把取数、落盘、快照全部换成假件（零副作用），只断言状态。
    """
    orig_fetch = R._fetch_index_belong_from_sources
    orig_load = R.load_index_belong_cache
    fakes = {
        "merge_index_belong": lambda accepted: None,
        "belong_snapshot": lambda: {"sh600000": "沪深300"},
        "save_index_belong_cache": lambda: True,
    }
    had = {name: (name in app_data.__dict__,
                  app_data.__dict__.get(name)) for name in fakes}
    status_bak = dict(R._refresh_status)
    try:
        R._fetch_index_belong_from_sources = lambda: (
            {"沪深300": {"sh600000": "沪深300"}},
            {"中证500": "超过总预算 12s"})
        R.load_index_belong_cache = lambda: None
        for name, value in fakes.items():
            setattr(app_data, name, value)
        R._set_refresh_status(error=None)
        R._refresh_index_belong()
        err = R.refresh_status().get("error") or ""
    finally:
        R._fetch_index_belong_from_sources = orig_fetch
        R.load_index_belong_cache = orig_load
        for name, (was_own, value) in had.items():
            if was_own:
                app_data.__dict__[name] = value
            else:
                app_data.__dict__.pop(name, None)
        R._refresh_status.clear()
        R._refresh_status.update(status_bak)
    ok = "部分失败" in err and "中证500" in err and "本次更新 1/" in err
    rec("⑩", "部分失败挂到刷新状态（不再只 log）", ok, f"error={err!r}")


def test_count_tolerance_and_empty_mapping():
    """③c 下界容差放行「合法少几只」；映射全空必须单列原因（不许落到超预算）。

    两个后果同源于旧判据「原始行数 == 期望」：
      · 严格相等 → 样本股退市少 1~2 只时整指数判失败，旧值（含已退市票）永久保留；
      · 行数齐但映射为空 → box 里没有 error，外层把「映射为 0」报成「超过总预算」。
    回滚自证：判据改回 `accounted != expected`，前半必红；
              删掉 `if not mapping` 分支，后半必红（原因变成「超过总预算」）。
    """
    def ak_never(code):
        raise AssertionError("不该走兜底")

    # 少 2 只（= 容差）→ 放行，映射 298 只
    with _Patched(lambda c: _mk_items(EXPECTED[c] - 2 if c == "000300" else EXPECTED[c]),
                  _Counter(ak_never)):
        acc, failed = R._fetch_index_belong_from_sources()
    ok_loose = ("沪深300" in acc and len(acc["沪深300"]) == EXPECTED["000300"] - 2
                and not failed)

    # 少 3 只（超出容差）→ 判截断
    with _Patched(lambda c: _mk_items(EXPECTED[c] - 3 if c == "000300" else EXPECTED[c]),
                  _Counter(ak_never)):
        acc2, failed2 = R._fetch_index_belong_from_sources()
    ok_strict = ("沪深300" in failed2 and "沪深300" not in acc2
                 and "截断" in failed2.get("沪深300", ""))

    # 行数齐但映射全空：code 首位 7 推不出市场 + market 字段缺失
    def unmappable(code):
        return _mk_items(EXPECTED[code], market="", start=700000)

    with _Patched(unmappable, _Counter(ak_never)):
        acc3, failed3 = R._fetch_index_belong_from_sources()
    reasons = " | ".join(failed3.values())
    ok_empty = (not acc3 and len(failed3) == len(NAMES)
                and "判不出市场" in reasons and "超过总预算" not in reasons)

    rec("③c", "下界容差 2 只 + 空映射单列原因",
        ok_loose and ok_strict and ok_empty,
        f"少2只映射 {len(acc.get('沪深300', {}))}(应 298)/失败{bool(failed)}；"
        f"少3只失败={failed2.get('沪深300')}；空映射原因={reasons[:60]}")


def main():
    print("=" * 64)
    print("指数归属取数路径验证（eltdx 主源 / AKShare 兜底）")
    print("=" * 64)
    for fn in (test_expected_truth, test_eltdx_primary, test_akshare_fallback,
               test_empty_falls_back, test_count_guard, test_dropped_accounting,
               test_count_tolerance_and_empty_mapping,
               test_bj_920_no_bad_key, test_budget, test_merge_semantics, test_merge_overlap,
               test_no_whole_replace, test_eltdx_total_mismatch,
               test_partial_failure_reports):
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

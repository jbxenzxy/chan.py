# -*- coding: utf-8 -*-
"""
阶段 2.5：快照采集器（snapshot runner）
=====================================================================
拦截数据加载层（read_main_level_records / read_sub_level_records），
把冻结的 fixtures 注入 `_analyze_stock_internal` 全路径，取得完整输出，
经「规范化」（剥离环境相关字段）后冻结为 JSON 快照 / 与既有快照比对。

这是阶段 3 起一切结构拆分的安全网：任何改动导致笔/段/中枢/买卖点
输出漂移，`--update` 之外的重跑会立即失败并给出精确 diff 路径。

用法（独立运行）：
    python Test/snapshot_runner.py --case stock_d_full           # 采集并比对
    python Test/snapshot_runner.py --case stock_d_full --update  # 重新冻结该快照
    python Test/snapshot_runner.py --all --update                # 重冻结全部快照
"""
import copy
import json
import os
import sys
from datetime import datetime, date

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
SNAPSHOT_DIR = os.path.join(TEST_DIR, "snapshots")
FIXTURE_DIR = os.path.join(TEST_DIR, "fixtures")

# Python 3.10 垫片（沙箱环境；用户 Windows 环境为 3.11+ 无需）
import typing
if not hasattr(typing, "Self"):
    import typing_extensions
    typing.Self = typing_extensions.Self

sys.path.insert(0, REPO_ROOT)


# ─────────────────────────────────────────────────────────────
# 1. 数据加载层拦截
# ─────────────────────────────────────────────────────────────
def install_data_source(main_fixture, sub_fixture=None, truncate_to=None):
    """
    monkeypatch 数据加载函数，返回 (restore_fn, records_ref)。
    main_fixture/sub_fixture: fixtures 文件名；sub 为 None 时保持原函数。
    truncate_to: 若非 None，返回 dt <= truncate_to 的子集（全量截断对照用）。
    """
    from App import AppEngine as m
    from Test.gen_fixtures import load_records

    main_records = load_records(os.path.join(FIXTURE_DIR, main_fixture))
    if truncate_to is not None:
        main_records = [r for r in main_records if r["dt"] <= truncate_to]
    sub_records = load_records(os.path.join(FIXTURE_DIR, sub_fixture)) if sub_fixture else None
    if sub_records is not None and truncate_to is not None:
        sub_records = [r for r in sub_records if r["dt"] <= truncate_to]

    orig_main = m.read_main_level_records
    orig_sub = m.read_sub_level_records

    def fake_main(market, code, freq, return_raw=False, end_date=None, **kw):
        if return_raw:
            # 双窗口共用读取优化路径：(main, sub, forward_adjust_done)
            return list(main_records), (list(sub_records) if sub_records is not None else []), True
        return list(main_records), True

    def fake_sub(market, code, freq, sub_freq, full_records, end_date=None, **kw):
        return list(sub_records) if sub_records is not None else []

    m.read_main_level_records = fake_main
    m.read_sub_level_records = fake_sub

    def restore():
        m.read_main_level_records = orig_main
        m.read_sub_level_records = orig_sub

    return restore, main_records


# ─────────────────────────────────────────────────────────────
# 1b. 期货数据加载层拦截（天勤路径离线化）
# ─────────────────────────────────────────────────────────────
def install_futures_data_source(fixture):
    """
    拦截期货路径的两处真实网络点，返回 restore_fn：
      ① TqApi 连接      → sys.modules 注入假 tqsdk（TqApi/TqAuth stub）
      ② fetch_futures_kline → 替换为返回冻结 fixtures
    并确保模块级 TQ_AVAILABLE/CTqSdkAPI 检查通过；
    结束时清空 CTqSdkAPI 类级 K 线缓存（副作用隔离）。
    引擎(CChan→CTqSdkAPI.get_kl_data)全程走内存，无需 mock。
    """
    import sys
    import types
    from App import AppEngine as m
    from Test.gen_fixtures import load_records

    records = load_records(os.path.join(FIXTURE_DIR, fixture))

    class _FakeTqApi:
        def __init__(self, *a, **kw):
            pass
        def close(self):
            pass

    fake_mod = types.ModuleType("tqsdk")
    fake_mod.TqApi = _FakeTqApi
    fake_mod.TqAuth = lambda *a, **kw: None

    saved_modules = {k: sys.modules.get(k) for k in ("tqsdk",)}
    sys.modules["tqsdk"] = fake_mod

    # 阶段 5：fetch 收归 get_kline 家族基类方法 CTqSdkAPI.fetch_kline
    # （my_chan_main 不再持有模块级 fetch_futures_kline 符号），打桩点
    # 改到抽象层类方法：api_cls.fetch_kline = classmethod(_fake_fetch_kline)。
    m.TQ_AVAILABLE = True
    # CTqSdkAPI 本体可用（内存适配器）；仅当环境缺失时用真实类顶替检查
    if m.CTqSdkAPI is None:
        from DataAPI.TqSdkAPI import CTqSdkAPI as _RealCTqSdkAPI
        m.CTqSdkAPI = _RealCTqSdkAPI
    api_cls = m.CTqSdkAPI

    # 打桩点：抽象层类方法 CTqSdkAPI.fetch_kline（classmethod，
    # 调用形如 CTqSdkAPI.fetch_kline(api, symbol, ...) → cls 自动绑定）
    orig_fetch = api_cls.__dict__.get("fetch_kline", None)
    orig_tq_available = getattr(m, "TQ_AVAILABLE", None)
    orig_ctqsdkapi = getattr(m, "CTqSdkAPI", None)

    def _fake_fetch_kline(cls, api, symbol, freq_sec=15, num_bars=None,
                          display_key=None, start_time=None):
        return [dict(r) for r in records]

    api_cls.fetch_kline = classmethod(_fake_fetch_kline)

    def restore():
        for k, v in saved_modules.items():
            if v is not None:
                sys.modules[k] = v
            else:
                sys.modules.pop(k, None)
        # 还原 fetch_kline：有原始定义则还原，无则删除（回落到基类声明）
        if orig_fetch is not None:
            api_cls.fetch_kline = orig_fetch
        elif "fetch_kline" in api_cls.__dict__:
            del api_cls.fetch_kline
        if orig_tq_available is not None:
            m.TQ_AVAILABLE = orig_tq_available
        if orig_ctqsdkapi is not None:
            m.CTqSdkAPI = orig_ctqsdkapi
        # 清空类级 K 线缓存（set_data 的副作用）
        try:
            m.CTqSdkAPI.clear_all_cache()
        except Exception:
            pass

    return restore


# ─────────────────────────────────────────────────────────────
# 2. 副作用隔离
# ─────────────────────────────────────────────────────────────
def isolate_side_effects():
    """隔离引擎进程内缓存与写文件副作用，保证快照可重复采集"""
    from App import AppEngine as m
    saved = {}
    # CChan 对象缓存：清空，测试统一用 cache_chan=False 亦不依赖该缓存
    for attr in ("_STOCK_CACHE", "_stock_cache", "_CHAN_CACHE"):
        if hasattr(m, attr):
            saved[attr] = getattr(m, attr)
            setattr(m, attr, {})
    # 上次代码/周期持久化：已随阶段 8 瘦身迁移删除（AppChart 委托 app_data），
    # 快照路径不再写该文件，无需打桩

    def restore():
        for k, v in saved.items():
            setattr(m, k, v)
    return restore


# ─────────────────────────────────────────────────────────────
# 3. 输出规范化
# ─────────────────────────────────────────────────────────────
# 需剥离的环境相关/不稳定字段（耗时统计、进程路径、实时快照值）
_STRIP_KEYS = {
    "elapsed", "elapsed_s", "cost", "cost_s", "time_cost", "duration",
    "generated_at", "timestamp", "ts", "server_time",
    "last_price", "realtime_price", "current_price",
    "data_dir", "file_path", "vipdoc",
}
_FLOAT_KEEP = 10  # 浮点保留 10 位有效小数（远高于 1e-6 相对容差，但消除跨平台表示噪声）


def _norm(obj, path="$"):
    """递归规范化：剥离不稳定键、时间转 ISO、浮点定精度"""
    if isinstance(obj, dict):
        return {k: _norm(v, f"{path}.{k}") for k, v in sorted(obj.items()) if k not in _STRIP_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_norm(v, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, float):
        return round(obj, _FLOAT_KEEP)
    if isinstance(obj, (datetime, date)):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    return obj


def normalize(result):
    return _norm(copy.deepcopy(result))


# ─────────────────────────────────────────────────────────────
# 4. 用例定义（case → 采集参数）
# ─────────────────────────────────────────────────────────────
def _tail_dt(fixture_name, offset=0):
    """fixture 尾部往前第 offset 根的 dt（end_date / step 用例锚点）"""
    from Test.gen_fixtures import load_records
    rows = load_records(os.path.join(FIXTURE_DIR, fixture_name))
    return rows[-1 - offset]["dt"]


CASES = {}


def case(name):
    def deco(fn):
        CASES[name] = fn
        return fn
    return deco


@case("stock_d_full")
def _c_stock_d_full():
    """日线全量分析：笔/段/中枢/买卖点基线（核心快照）"""
    restore, _ = install_data_source("stock_day.json")
    try:
        from App import AppEngine as m
        r = m._analyze_stock_internal("600519", freq="d", end_date=None, cache_chan=False)
        return r
    finally:
        restore()


@case("stock_d_end_date")
def _c_stock_d_end_date():
    """复盘模式：end_date 固定为倒数第 30 根（REPLAY_MODE 生效路径）。
    注意 end_date 仅支持斜杠格式（%Y/%m/%d 等），连字符会解析失败。"""
    anchor = _tail_dt("stock_day.json", 30)
    restore, _ = install_data_source("stock_day.json")
    try:
        from App import AppEngine as m
        return m._analyze_stock_internal("600519", freq="d", end_date=anchor.strftime("%Y/%m/%d"), cache_chan=False)
    finally:
        restore()


@case("stock_d_step_m5")
def _c_stock_d_step_m5():
    """trigger_step 逐步回放：end_date 锚定 + step=-5（截断点前移 5 根）"""
    anchor = _tail_dt("stock_day.json", 60)
    restore, _ = install_data_source("stock_day.json")
    try:
        from App import AppEngine as m
        return m._analyze_stock_internal("600519", freq="d", end_date=anchor.strftime("%Y/%m/%d"), cache_chan=False, step=-5)
    finally:
        restore()


@case("multilevel_d_30m")
def _c_multilevel():
    """多级别联立：日线主级别 + 子级别（dual=True 时子级别由 _SUB_FREQ_MAP
    自动映射，不走外部参数；子级别数据经 read_sub_level_records 通道注入）"""
    from App import AppEngine as m
    restore, _ = install_data_source("stock_day.json", sub_fixture="stock_60m.json")
    try:
        return m._analyze_stock_internal("600519", freq="d", cache_chan=False, dual=True)
    finally:
        restore()


@case("edge_gap")
def _c_edge_gap():
    """边界：数据缺失（停牌 5 日缺口）"""
    restore, _ = install_data_source("stock_day_gap.json")
    try:
        from App import AppEngine as m
        return m._analyze_stock_internal("600519", freq="d", cache_chan=False)
    finally:
        restore()


@case("edge_zero_vol")
def _c_edge_zero_vol():
    """边界：零成交停牌日（ohlc 收敛为一点）"""
    restore, _ = install_data_source("stock_day_zero_vol.json")
    try:
        from App import AppEngine as m
        return m._analyze_stock_internal("600519", freq="d", cache_chan=False)
    finally:
        restore()


@case("futures_15s_full")
def _c_futures_full():
    """期货路径全量（螺纹钢 15s）：天勤拉取→截断→注入→CChan(market_type=futures)。
    覆盖 _analyze_futures_internal 全链路（此前快照仅覆盖股票路径）。"""
    restore = install_futures_data_source("futures_15s.json")
    try:
        from App import AppEngine as m
        return m._analyze_futures_internal("KQ.m@SHFE.rb", freq="15s")
    finally:
        restore()


@case("futures_15s_end_date")
def _c_futures_end_date():
    """期货复盘模式：end_date 锚定倒数第 400 根（REPLAY_MODE 生效）。
    注意 end_date 仅支持斜杠格式（%Y/%m/%d 系），连字符契约见 test_phase2_guards。"""
    from Test.gen_fixtures import load_records
    rows = load_records(os.path.join(FIXTURE_DIR, "futures_15s.json"))
    anchor = rows[-401]["dt"]
    restore = install_futures_data_source("futures_15s.json")
    try:
        from App import AppEngine as m
        return m._analyze_futures_internal(
            "KQ.m@SHFE.rb", freq="15s",
            end_date=anchor.strftime("%Y/%m/%d %H:%M:%S"))
    finally:
        restore()


# ─────────────────────────────────────────────────────────────
# 5. 快照冻结 / 比对
# ─────────────────────────────────────────────────────────────
def snapshot_path(name):
    return os.path.join(SNAPSHOT_DIR, f"{name}.json")


def acquire(name, update=False):
    """采集一个用例 → 契约校验 → 规范化 → 冻结或比对。返回 (ok, detail)"""
    from Test import comparator
    from Test.contracts import validate_result_structure, format_diffs

    restore_iso = isolate_side_effects()
    try:
        raw = CASES[name]()
    finally:
        restore_iso()
    norm = normalize(raw)

    # 契约校验（规范化后仍适用：必需键均为非易变键）
    # 违反契约 → 硬失败，不冻结坏快照（避免把结构破坏固化成基线）
    expect_sub = isinstance(norm, dict) and isinstance(norm.get("sub"), dict)
    ok_c, diffs_c = validate_result_structure(norm, expect_sub=expect_sub)
    if not ok_c:
        return False, f"契约校验失败 {name}:\n{format_diffs(diffs_c)}"

    path = snapshot_path(name)
    if update or not os.path.exists(path):
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(norm, f, ensure_ascii=False, indent=1, sort_keys=True)
        return True, "frozen" if not update else "updated"

    with open(path, "r", encoding="utf-8") as f:
        expected = json.load(f)
    return comparator.compare(expected, norm, path=f"$.{name}")


def run(cases, update=False):
    results = {}
    for name in cases:
        ok, detail = acquire(name, update=update)
        results[name] = (ok, detail)
        flag = "PASS" if ok else "FAIL"
        extra = "" if ok else f"\n{detail}"
        print(f"[{flag}] {name}{extra if not ok else ''}")
    n_fail = sum(1 for ok, _ in results.values() if not ok)
    print(f"\n===== 快照回归: {len(results) - n_fail}/{len(results)} 通过 =====")
    return n_fail == 0


if __name__ == "__main__":
    args = sys.argv[1:]
    update = "--update" in args
    args = [a for a in args if not a.startswith("--")]
    names = CASES.keys() if not args or "all" in args else args
    ok = run(list(names), update=update)
    sys.exit(0 if ok else 1)

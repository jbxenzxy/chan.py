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

    P1-1 数据源抽象单轨化：打桩点随引擎层迁移，改打 CTdxAPI.fetch_main_level /
    fetch_sub_level（CommonStockAPI 适配器类方法），不再打 AppEngine 模块级
    read_main_level_records / read_sub_level_records 符号。
    """
    from App import AppEngine as m
    from DataAPI.TdxAPI import CTdxAPI
    from Test.gen_fixtures import load_records

    main_records = load_records(os.path.join(FIXTURE_DIR, main_fixture))
    if truncate_to is not None:
        main_records = [r for r in main_records if r["dt"] <= truncate_to]
    sub_records = load_records(os.path.join(FIXTURE_DIR, sub_fixture)) if sub_fixture else None
    if sub_records is not None and truncate_to is not None:
        sub_records = [r for r in sub_records if r["dt"] <= truncate_to]

    orig_main = CTdxAPI.__dict__.get("fetch_main_level")
    orig_sub = CTdxAPI.__dict__.get("fetch_sub_level")

    def fake_main(cls, market, code, freq, return_raw=False, end_date=None, **kw):
        if return_raw:
            # 双窗口共用读取优化路径：(main, sub, forward_adjust_done)
            return list(main_records), (list(sub_records) if sub_records is not None else []), True
        return list(main_records), True

    def fake_sub(cls, market, code, freq, sub_freq, full_records, end_date=None, **kw):
        return list(sub_records) if sub_records is not None else []

    CTdxAPI.fetch_main_level = classmethod(fake_main)
    CTdxAPI.fetch_sub_level = classmethod(fake_sub)

    def restore():
        if orig_main is not None:
            CTdxAPI.fetch_main_level = orig_main
        elif "fetch_main_level" in CTdxAPI.__dict__:
            del CTdxAPI.fetch_main_level
        if orig_sub is not None:
            CTdxAPI.fetch_sub_level = orig_sub
        elif "fetch_sub_level" in CTdxAPI.__dict__:
            del CTdxAPI.fetch_sub_level

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
        def wait_update(self, *a, **kw):
            # connect() 接口守护要求 api 具备 wait_update（静态快照用例
            # 不消费 wait_update，仅需通过 hasattr 检查）
            return None
        def get_kline_serial(self, symbol, freq_sec, *a, **kw):
            # D7 迁移：init_chan_symbol 会取实时序列引用（静态快照用例
            # 不消费 wait_update，返回占位 dict 即可）
            return {"symbol": symbol, "freq_sec": freq_sec}

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
# 2b. 参考数据源注入（与 test_trigger_step_replay 同源）
# ─────────────────────────────────────────────────────────────
# 股票名称 / PE-TTM / 指数归属 由 app_data 从 *gitignored* 的本地缓存文件
# （App/stock_names.json、App/stock_pettm_index.json）加载；干净检出下这些文件
# 不存在，get_stock_name 退化为 market+code、get_pe_ttm 退化为 None、get_index_belong
# 退化为 None，导致冻结基线（期望 贵州茅台 / 20.0 / 沪深300）对比失败。
# 为使快照在任意干净检出下可复现，这里注入与冻结基线一致的确定性参考表
# （属元数据，非笔/段/中枢/买卖点算法被测对象）。期货用例不消费该表，注入无副作用。
_REF_MARKET = "sh"
_REF_CODE = "600519"
_REF_COMPOUND = f"{_REF_MARKET}{_REF_CODE}"


def _seed_reference():
    """注入确定性 name / pe_ttm / index_belong 参考表，返回 restore_fn（隔离全局副作用）。"""
    from App.AppData import app_data
    saved = {
        "_names": app_data._names,
        "_pe": app_data._pe,
        "_belong": app_data._belong,
        "_names_loaded": app_data._names_loaded,
        "_pe_loaded": app_data._pe_loaded,
        "_belong_loaded": app_data._belong_loaded,
    }
    app_data._names = {_REF_COMPOUND: {"name": "贵州茅台", "market": _REF_MARKET}}
    app_data._pe = {_REF_COMPOUND: 20.0}
    app_data._belong = {_REF_COMPOUND: "沪深300"}
    app_data._names_loaded = True
    app_data._pe_loaded = True
    app_data._belong_loaded = True

    def restore():
        app_data._names = saved["_names"]
        app_data._pe = saved["_pe"]
        app_data._belong = saved["_belong"]
        app_data._names_loaded = saved["_names_loaded"]
        app_data._pe_loaded = saved["_pe_loaded"]
        app_data._belong_loaded = saved["_belong_loaded"]
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


def _force_dual_impl(value):
    """强制股票双窗 A/B 开关（CHAN_STOCK_DUAL_IMPL），返回 restore_fn。

    用途：multilevel_d_30m（legacy 基线，冻结不改）与
    multilevel_d_30m_indep（独立实现新基线）分别锁定两种实现，
    保证 A/B 两条路径的快照互不漂移、同时受回归守护。
    """
    key = "CHAN_STOCK_DUAL_IMPL"
    orig = os.environ.get(key)
    os.environ[key] = value

    def restore():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    return restore


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
    """多级别联立（legacy 基线）：A/B 开关强制 legacy，走原联立路径
    （dual=True 时子级别由 _SUB_FREQ_MAP 自动映射，子级别数据经
    read_sub_level_records 通道注入，红框边界取主笔边界（峰/谷）KLU 联立
    sub_kl_list 真实子级边界）。快照冻结于联立实现，不随独立改造漂移。"""
    from App import AppEngine as m
    restore_env = _force_dual_impl("legacy")
    restore, _ = install_data_source("stock_day.json", sub_fixture="stock_60m.json")
    try:
        return m._analyze_stock_internal("600519", freq="d", cache_chan=False, dual=True)
    finally:
        restore()
        restore_env()


@case("multilevel_d_30m_indep")
def _c_multilevel_indep():
    """多级别双窗（independent 新基线 · P0-P3）：A/B 开关强制
    independent，下窗独立拉取独立建 CChan（先下后上）：
      · 下窗按「结束时间语义」精确截断（P0）；
      · 灰框 sub_kl_times 后端时间分桶合成（P3 · D2=A）；
      · 红框边界改数学换算 _stocks_red_range_algo（日期型主级别
        d/w 取当日 00:00~23:59:59，语义=覆盖当日全部下窗K线，
        与 legacy 联立真实首末根边界不同属预期差异）；
      · 区间套 check_nested_diver 改读独立下窗缓存（P1）。
    """
    from App import AppEngine as m
    restore_env = _force_dual_impl("independent")
    restore, _ = install_data_source("stock_day.json", sub_fixture="stock_60m.json")
    try:
        return m._analyze_stock_internal("600519", freq="d", cache_chan=False, dual=True)
    finally:
        restore()
        restore_env()


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


def _run_futures_snapshot_case(freq="15s", freq_sec=15, end_time=None):
    """期货静态快照公共链路（D7 迁移后统一走 SSE 生产路径）：
    CTqSdkSession → init_chan_symbol（拉取+截断+建 chan）→
    _extract_realtime_snapshot（与 SSE init 事件同构的快照格式）。
    原 _analyze_futures_internal 已按 D7 决策删除（生产零调用方）。"""
    from App import AppSSE
    symbol = "KQ.m@SHFE.rb"
    src = AppSSE.CTqSdkSession()
    src.connect()
    try:
        r = AppSSE.init_chan_symbol(src, symbol, AppSSE._get_futures_name(symbol),
                                    freq_sec, freq, end_time=end_time)
        if r is None:
            raise RuntimeError("init_chan_symbol 返回 None（数据不足或截断后为空）")
        chan, _klines, kl_type, _records = r
        result = AppSSE._extract_realtime_snapshot(
            chan, kl_type, symbol, AppSSE._get_futures_name(symbol), freq)
        # 契约桥接：SSE init 快照 meta 不含静态分析三键（chan_version /
        # date_range / forward_adjust），测试侧补齐以满足统一契约
        # （Test/contracts.py REQUIRED_META_KEYS，生产 SSE 载荷不受影响）
        meta = result["meta"]
        meta.setdefault("chan_version", "chan.py")
        meta["forward_adjust"] = False
        if result.get("klines"):
            meta["date_range"] = f"{result['klines'][0]['date']} ~ {result['klines'][-1]['date']}"
        else:
            meta["date_range"] = ""
        return result
    finally:
        try:
            src.close()
            src.close_api()
        except Exception:
            pass


@case("futures_15s_full")
def _c_futures_full():
    """期货路径全量（螺纹钢 15s）：SSE 生产链路 init_chan_symbol →
    _extract_realtime_snapshot。D7 迁移：原 _analyze_futures_internal
    快照口径更换为 SSE init 同构格式，基线需 --update 重采集。"""
    restore = install_futures_data_source("futures_15s.json")
    try:
        return _run_futures_snapshot_case(freq="15s", freq_sec=15)
    finally:
        restore()


@case("futures_15s_end_date")
def _c_futures_end_date():
    """期货复盘模式：end_time 锚定倒数第 400 根（SSE 软断开数据边界）。
    D7 迁移：截断由 _truncate_records_by_end 承担（仅支持 %Y/%m/%d 系
    斜杠格式，连字符契约见 test_phase2_guards）。"""
    from Test.gen_fixtures import load_records
    rows = load_records(os.path.join(FIXTURE_DIR, "futures_15s.json"))
    anchor = rows[-401]["dt"]
    restore = install_futures_data_source("futures_15s.json")
    try:
        return _run_futures_snapshot_case(
            freq="15s", freq_sec=15,
            end_time=anchor.strftime("%Y/%m/%d %H:%M:%S"))
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
    restore_ref = _seed_reference()   # 注入确定性参考表（见 _seed_reference）
    try:
        raw = CASES[name]()
    finally:
        restore_ref()
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

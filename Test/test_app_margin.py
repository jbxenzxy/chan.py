# -*- coding: utf-8 -*-
"""
两融余额（App/AppMargin.py）行为护栏
=====================================================================
覆盖 call_margin 的核心行为（数据源 = eltdx F10「大盘两融日序列汇总」，
按 N001 日期把沪深北三市 N005 分行求和）：

  ① 全量区间：三市合计（元）→ 日期/余额(亿元)/指标
  ② 视口区间过滤：start_date/end_date 只取区间内（含两端）
  ③ 区间无数据：返回空序列 + stats 全 None
  ④ 斜杠入参：前端契约 %Y/%m/%d，内部转连字符比较后仍能命中数据
  ⑤ 日期契约守护：输出日期必须全为斜杠 %Y/%m/%d
  ⑥ 口径钉：取 N005（融资 + 融券两融余额），不取 N003（仅融资余额）；
     三市标记 012001/012002/012046 齐全，东方财富旧口径零残留
  ⑦ 单位换算：接口返回元，对外为亿元（1e8）
  ⑧ 坏值只丢该行：None / 字符串 / 负数 / NaN / 未知市场 / 非字典
     不污染整段序列
  ⑨ 数据源失败：抛 DataFetchError，无兜底（含 eltdx 底层异常转译）
  ⑩ 无持久化：重复调用结果一致，模块不落盘
  ⑪ 三市分行（含北交所）按日期求和 + 同市场重复行去重 + 升序 + 单次调用
  ⑫ 口径边界：total 汇总行不重复计入、北交所缺失日期按「有则加」、
     区间外日期被过滤

测试数据全部由内存打桩提供（不联网、不写任何生产数据文件）：
交易日 = 2024-01-02 / 01-03 / 01-04 / 01-05 / 01-08
  两融余额（亿元） = [16500, 16800, 16000, 17200, 15500]
  全量：峰值=17200(01-05) 当前=15500(01-08) 缩至峰值=90.12%
  视口 01-03~01-04：峰值=16800(01-03) 当前=16000 缩至峰值=95.24%

运行：python Test/test_app_margin.py
"""
import math
import os
import re
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

from App import AppMargin
from App.AppErrors import DataFetchError

SLASH_DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")

_TRADING_DATES = [
    "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08",
]
_BALANCE_YI = [16500, 16800, 16000, 17200, 15500]
_ROWS = [(d, float(v) * 1e8) for d, v in zip(_TRADING_DATES, _BALANCE_YI)]
_EXPECTED_DATES = [d.replace("-", "/") for d in _TRADING_DATES]

# 分市场原始行：故意含重复行 / total / 坏值 / 未知市场 / 北交所缺失日
_MARKET_ROWS = [
    {"N001": "2024-01-03 00:00:00", "N010": "012001",
     "N003": 90 * 1e8, "N004": 10 * 1e8, "N005": 100 * 1e8},
    {"N001": "2024-01-03", "N010": "012002", "N005": 60 * 1e8},
    {"N001": "2024-01-03", "N010": "012001", "N005": 100 * 1e8},  # 重复
    {"N001": "2024-01-02", "N010": "012001", "N005": 90 * 1e8},
    {"N001": "2024-01-02", "N010": "012002", "N005": 60 * 1e8},
    {"N001": "2024-01-02", "N010": "012046", "N005": 5 * 1e8},
    {"N001": "2024-01-02", "N010": "total", "N005": 155 * 1e8},   # 不计入
    {"N001": "2024-01-04", "N010": "012001", "N005": None},
    {"N001": "2024-01-04", "N010": "012002", "N005": -1.0},
    {"N001": "2024-01-04", "N010": "999999", "N005": 999 * 1e8},
    {"N001": "2025-01-02", "N010": "012001", "N005": 200 * 1e8},  # 区间外
]

_FAILURES = []


def check(cond, desc, extra=""):
    if cond:
        print(f"  [OK] {desc}")
    else:
        print(f"  [FAIL] {desc} {extra}")
        _FAILURES.append(desc)


def _stub_rows(rows):
    """把 fetch_margin_rows 换成内存假数据，返回还原函数"""
    orig = AppMargin.fetch_margin_rows
    AppMargin.fetch_margin_rows = lambda s, e: list(rows)
    return lambda: setattr(AppMargin, "fetch_margin_rows", orig)


def test_full_range():
    """① 全量区间：日期/余额/指标正确"""
    restore = _stub_rows(_ROWS)
    try:
        got = AppMargin.call_margin("2024/01/01", "2024/12/31")
    finally:
        restore()
    check(got["dates"] == _EXPECTED_DATES, "① 日期序列为全站斜杠格式", str(got["dates"]))
    check(got["balances"] == _BALANCE_YI, "① 余额(亿元)=接口元值/1e8", str(got["balances"]))
    st = got["stats"]
    check(st["peak"] == 17200 and st["peak_date"] == "2024/01/05",
          "① 峰值余额=17200亿(01-05)", str(st))
    check(st["current"] == 15500 and st["current_date"] == "2024/01/08",
          "① 当前余额=15500亿(01-08)", str(st))
    check(st["peak_ratio"] == 90.12, "① 缩至峰值=当前/峰值×100=90.12%", str(st))


def test_range_filter():
    """② 视口区间过滤：只取区间内（含两端）"""
    restore = _stub_rows(_ROWS)
    try:
        got = AppMargin.call_margin("2024/01/03", "2024/01/04")
    finally:
        restore()
    check(got["dates"] == ["2024/01/03", "2024/01/04"],
          "② 视口过滤含两端", str(got["dates"]))
    check(got["balances"] == [16800, 16000], "② 视口内余额", str(got["balances"]))
    check(got["stats"]["peak"] == 16800 and got["stats"]["current"] == 16000,
          "② 视口内峰值/当前", str(got["stats"]))
    check(got["stats"]["peak_ratio"] == 95.24,
          "② 视口内缩至峰值=95.24%", str(got["stats"]))


def test_empty_range():
    """③ 区间无数据：空序列 + stats 全 None"""
    restore = _stub_rows(_ROWS)
    try:
        got = AppMargin.call_margin("2025/01/01", "2025/12/31")
    finally:
        restore()
    check(got["dates"] == [] and got["balances"] == [], "③ 空序列", str(got))
    check(all(v is None for v in got["stats"].values()),
          "③ stats 全 None", str(got["stats"]))


def test_slash_input_and_output_contract():
    """④⑤ 斜杠入参 + 输出日期契约"""
    restore = _stub_rows(_ROWS)
    try:
        got = AppMargin.call_margin("2024/01/02", "2024/01/08")
    finally:
        restore()
    check(len(got["dates"]) == 5, "④ 斜杠入参仍能命中数据", str(got["dates"]))
    check(all(SLASH_DATE_RE.match(d) for d in got["dates"]),
          "⑤ 输出日期全为 %Y/%m/%d", str(got["dates"]))
    check(SLASH_DATE_RE.match(got["stats"]["peak_date"])
          and SLASH_DATE_RE.match(got["stats"]["current_date"]),
          "⑤ 统计里的日期同为斜杠格式", str(got["stats"]))


def test_balance_field_is_three_market_total():
    """⑥ 口径钉：取 N005（融资+融券），不是 N003（仅融资）；三市齐全"""
    check(AppMargin._BALANCE_FIELD == "N005",
          "⑥ 常量 _BALANCE_FIELD == N005", AppMargin._BALANCE_FIELD)
    check(AppMargin._MARGIN_SECTION == "market_margin_summary"
          and AppMargin._MARGIN_CODE == "000001",
          "⑥ section 为大盘两融日序列汇总", AppMargin._MARGIN_SECTION)
    check(AppMargin._FINANCING_ONLY_FIELD == "N003",
          "⑥ 明确标出「仅融资余额」字段 N003", AppMargin._FINANCING_ONLY_FIELD)
    check(AppMargin._MARKET_TAGS == ("012001", "012002", "012046"),
          "⑥ 三市标记沪/深/北齐全", str(AppMargin._MARKET_TAGS))
    # 真行为：一行里 N003 与 N005 故意不同值，看解析取了哪个
    row = {"N001": "2024-05-06 00:00:00", "N010": "012001",
           "N003": 100 * 1e8, "N004": 5 * 1e8, "N005": 105 * 1e8}
    parsed = AppMargin._parse_rows([row])
    check(parsed == [("2024-05-06", 105 * 1e8)],
          "⑥ 解析取 N005=105亿（含融券），不是融资余额100亿", str(parsed))
    # 静态负向：源码里不得把 N003 当取值字段，也不得残留东方财富口径
    src = open(AppMargin.__file__, "r", encoding="utf-8").read()
    check("row.get(_FINANCING_ONLY_FIELD)" not in src
          and 'row.get("N003")' not in src
          and '_BALANCE_FIELD = "N003"' not in src,
          "⑥ 源码无「取融资余额当两融余额」写法")
    check("RZRQYE" not in src and "datacenter-web.eastmoney.com" not in src,
          "⑥ 东方财富 RZRQYE 旧口径零残留")
    check("from eltdx import F10Client" in src
          and "market_report(_MARGIN_CODE, _MARGIN_SECTION)" in src,
          "⑥ 取数走 eltdx F10 market_report")


def test_unit_conversion():
    """⑦ 单位换算：元 → 亿元"""
    check(AppMargin._fmt_yi(2295784141868) == 22957.84,
          "⑦ 22,957.84 亿元（2026-11-09 实测值）",
          str(AppMargin._fmt_yi(2295784141868)))
    check(AppMargin._fmt_yi(1e8) == 1.0, "⑦ 1 亿元 = 1e8 元")


def test_bad_rows_dropped():
    """⑧ 坏值只丢该行，不污染整段"""
    rows = [
        {"N001": "2024-01-02", "N010": "012001", "N005": 16500 * 1e8},
        {"N001": "2024-01-03", "N010": "012001", "N005": None},
        {"N001": "2024-01-03", "N010": "012002", "N005": "bad"},
        {"N001": "2024-01-04", "N010": "012001", "N005": -1.0},
        {"N001": "2024-01-05", "N010": "012001", "N005": float("nan")},
        {"N001": "2024-01-05", "N010": "012002", "N005": float("inf")},
        {"N001": "2024-01-08", "N010": "012001", "N005": 17000 * 1e8},
        {"N001": "2024-01-09", "N010": "999999", "N005": 99999 * 1e8},
        "not-a-dict",
    ]
    parsed = AppMargin._parse_rows(rows)
    check(parsed == [("2024-01-02", 16500 * 1e8), ("2024-01-08", 17000 * 1e8)],
          "⑧ 非有限/负值/缺值/未知市场/非字典只丢该行", str(parsed))
    check(math.isfinite(parsed[-1][1]), "⑧ 结果里无 NaN/inf 污染")


def test_fetch_failure_propagates():
    """⑨ 数据源失败 → DataFetchError，无兜底"""
    def boom(_s, _e):
        raise DataFetchError("mock upstream down")

    orig = AppMargin.fetch_margin_rows
    AppMargin.fetch_margin_rows = boom
    try:
        try:
            AppMargin.call_margin("2024/01/01", "2024/12/31")
            hit = False
        except DataFetchError:
            hit = True
    finally:
        AppMargin.fetch_margin_rows = orig
    check(hit, "⑨ 数据源异常原样抛 DataFetchError")

    # eltdx 网关异常：伪造 F10Client 让 market_report 抛 RuntimeError，
    # _fetch_f10_rows 必须把它转译成领域异常（不能漏出 RuntimeError）
    import types

    class _BrokenClient:
        def __init__(self, **_kwargs):
            pass

        def market_report(self, *_args, **_kwargs):
            raise RuntimeError("7615 gateway down")

    fake_eltdx = types.ModuleType("eltdx")
    fake_eltdx.F10Client = _BrokenClient
    orig_module = sys.modules.get("eltdx")
    sys.modules["eltdx"] = fake_eltdx
    try:
        try:
            AppMargin._fetch_f10_rows()
            wrapped = False
        except DataFetchError:
            wrapped = True
    finally:
        if orig_module is None:
            del sys.modules["eltdx"]
        else:
            sys.modules["eltdx"] = orig_module
    check(wrapped, "⑨ eltdx 网关异常转译为 DataFetchError")

    # 取数链路异常不被吞掉：fetch_margin_rows 原样冒泡，不做任何兜底
    orig_f10 = AppMargin._fetch_f10_rows

    def down():
        raise DataFetchError("mock 7615 down")

    AppMargin._fetch_f10_rows = down
    try:
        try:
            AppMargin.fetch_margin_rows("2024-01-01", "2024-12-31")
            bubbled = False
        except DataFetchError:
            bubbled = True
    finally:
        AppMargin._fetch_f10_rows = orig_f10
    check(bubbled, "⑨ 取数链路异常原样冒泡，无兜底")


def test_no_persistence():
    """⑩ 无持久化：重复调用结果一致"""
    restore = _stub_rows(_ROWS)
    try:
        a = AppMargin.call_margin("2024/01/01", "2024/12/31")
        b = AppMargin.call_margin("2024/01/01", "2024/12/31")
    finally:
        restore()
    check(a == b, "⑩ 重复调用结果一致")
    check(not hasattr(AppMargin, "CACHE") and not hasattr(AppMargin, "_CACHE"),
          "⑩ 模块无缓存容器")
    check(not hasattr(AppMargin, "_CLIENT") and not hasattr(AppMargin, "_f10_client"),
          "⑩ 模块不复用 F10Client 实例（无共享连接状态）")


def test_market_rows_merge():
    """⑪ 三市分行（含北交所）求和 + 去重 + 升序 + 单次调用"""
    calls = []
    orig = AppMargin._fetch_f10_rows

    def stub():
        calls.append(1)
        return list(_MARKET_ROWS)

    AppMargin._fetch_f10_rows = stub
    try:
        rows = AppMargin.fetch_margin_rows("2024-01-02", "2024-01-03")
    finally:
        AppMargin._fetch_f10_rows = orig
    check(rows == [("2024-01-02", 155 * 1e8), ("2024-01-03", 160 * 1e8)],
          "⑪ 三市分行（含北交所）按日期求和并去重升序", str(rows))
    check(len(calls) == 1, "⑪ 单次 F10 调用拿全历史，不分页", str(calls))


def test_three_market_scope():
    """⑫ total 不计入 / 北交所缺失按有则加 / 区间外过滤"""
    parsed = AppMargin._parse_rows(_MARKET_ROWS)
    by_date = dict(parsed)
    check(by_date.get("2024-01-02") == 155 * 1e8,
          "⑫ total 汇总行未被重复计入（否则会变成 310 亿）",
          str(by_date.get("2024-01-02")))
    check(by_date.get("2024-01-03") == 160 * 1e8,
          "⑫ 北交所缺失的日期按「有则加」仍有值",
          str(by_date.get("2024-01-03")))
    check("2024-01-04" not in by_date,
          "⑫ 全坏值/未知市场的交易日不产出半截合计", str(by_date))

    orig = AppMargin._fetch_f10_rows
    AppMargin._fetch_f10_rows = lambda: list(_MARKET_ROWS)
    try:
        rows = AppMargin.fetch_margin_rows("2024-01-02", "2024-01-03")
    finally:
        AppMargin._fetch_f10_rows = orig
    check(all("2024-01-02" <= d <= "2024-01-03" for d, _v in rows),
          "⑫ 全历史内存过滤，区间外日期不进结果", str(rows))


def main():
    print("===== 两融余额行为护栏（eltdx F10 口径）=====")
    for fn in (test_full_range, test_range_filter, test_empty_range,
               test_slash_input_and_output_contract,
               test_balance_field_is_three_market_total,
               test_unit_conversion, test_bad_rows_dropped,
               test_fetch_failure_propagates, test_no_persistence,
               test_market_rows_merge, test_three_market_scope):
        fn()
    print(f"===== 合计 {len(_FAILURES)} 项失败 =====")
    if _FAILURES:
        for f in _FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("===== 全部通过 =====")
    sys.exit(0)


if __name__ == "__main__":
    main()

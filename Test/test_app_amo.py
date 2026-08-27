# -*- coding: utf-8 -*-
"""
市场量能（App/AppAMO.py）单元测试
=====================================================================
覆盖 call_amo 的核心行为（数据源仅 TDX 本地指数日线，无兜底）：

  ① 全量区间：sh000001 + sz399106 成交额逐日相加 → 日期/成交额/指标
  ② 视口区间过滤：start_date/end_date 只取区间内（含两端）
  ③ 区间无数据：返回空序列 + stats 全 None
  ④ 数据源缺失：抛 DataFetchError（无任何兜底）
  ⑤ 无持久化：模块不存状态，重复调用结果一致
  ⑥ 斜杠入参：前端契约 %Y/%m/%d，内部转连字符比较后仍能命中数据
  ⑦ 日期契约守护：输出日期必须全为斜杠 %Y/%m/%d（行为层正则 + 结构层
     源码归一化/输出转换必须存在），防连字符日期回归

测试数据（运行时生成合成 .day 文件，32 字节/条，A 股格式，5 个交易日）：
  日期(I4) 开(I4) 高(I4) 低(I4) 收(I4) 成交额(f4) 量(I4) 保留(I4)
  第 6 字段 amount（f4）即当日成交额（元），沪+深逐日相加 = 全市场成交额。

交易日 = 2024-01-02(二) 01-03 01-04 01-05 01-08(一)
  沪市(sh000001)：[1000,1200, 900,1500, 800] 亿元
  深市(sz399106)：[ 800, 900, 700,1000, 600] 亿元
  相加（亿）       ：[1800,2100,1600,2500,1400]
  全量：峰值=2500亿(2024-01-05) 当前=1400亿(2024-01-08) 缩量=44.0%
  视口 2024-01-03~2024-01-04：金额=[2100,1600] 峰值=2100(01-03)
      当前=1600(01-04) 缩量=(1-1600/2100)*100=23.81%

运行：python Test/test_app_amo.py
"""
import os
import re
import struct
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

from App.AppConfig import app_config
from App.AppErrors import DataFetchError
from App import AppAMO


# 全站 K 线日期契约：严格斜杠 %Y/%m/%d（长度 10），非连字符
SLASH_DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")


# ── 合成 .day 夹具生成 ────────────────────────────────────────────
# 交易日历（2024-01-02 起连续 5 个交易日，跳过周末：01-06/07）
_TRADING_DATES = [
    "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08",
]
_SH_AMT_YI = [1000, 1200, 900, 1500, 800]   # 沪市每日成交额（亿元）
_SZ_AMT_YI = [800, 900, 700, 1000, 600]      # 深市每日成交额（亿元）

_AMD_DIR = os.path.join(TEST_DIR, "fixtures", "_amo_gen")


def _date_to_int(date_str: str) -> int:
    return int(date_str.replace("-", ""))


def _write_day_file(market, code, amounts_yi):
    """写一个 5 条记录的 .day 文件（A 股格式，成交额单位：元）。
    AppAMO._index_day_file 采用 {vipdoc_dir}/{market}/lday/{market}{code}.day，
    其中 vipdoc_dir = {tdx_install_dir}/vipdoc，故写入 tdx_install_dir/vipdoc 下。
    """
    path = os.path.join(_AMD_DIR, "vipdoc", market, "lday",
                        f"{market}{code}.day")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        for ds, amt_yi in zip(_TRADING_DATES, amounts_yi):
            date_int = _date_to_int(ds)
            price = 1000 * 100            # 开=高=低=收=1000.00（元*100）
            amount = float(amt_yi) * 1e8  # 亿元 → 元（f4）
            rec = struct.pack('<IIIIIfII', date_int, price, price, price,
                              price, amount, 1_000_000, 0)
            f.write(rec)
    return path


def _build_fixture_tdx_dir():
    """生成合成 TDX 目录（含 sh000001 + sz399106 日线），返回 tdx_install_dir"""
    if os.path.isdir(_AMD_DIR):
        import shutil
        shutil.rmtree(_AMD_DIR, ignore_errors=True)
    _write_day_file("sh", "000001", _SH_AMT_YI)
    _write_day_file("sz", "399106", _SZ_AMT_YI)
    # vipdoc_dir = f"{tdx_install_dir}/vipdoc" → 需要 _AMD_DIR 下有 vipdoc 子目录
    # 且 AppAMO._index_day_file 拼 {vipdoc_dir}/{market}/lday/...
    # 故直接把 market/lday 放在 _AMD_DIR/vipdoc 下
    return _AMD_DIR


def _sum_yi():
    """沪+深逐日相加（亿元），返回列表"""
    return [round(s + z, 2) for s, z in zip(_SH_AMT_YI, _SZ_AMT_YI)]


# 预期的合并成交额（亿元），对应 _TRADING_DATES
EXPECTED_AMOUNTS = _sum_yi()
# 输出日期统一为全站斜杠格式（%Y/%m/%d，与 K 线契约一致）
EXPECTED_DATES = [d.replace("-", "/") for d in _TRADING_DATES]


def _restore_config(orig):
    app_config.tdx_install_dir = orig


def _with_fixture():
    """设置 app_config 指向合成夹具目录，返回原值"""
    orig = app_config.tdx_install_dir
    app_config.tdx_install_dir = _build_fixture_tdx_dir()
    return orig


def test_full_range(failures):
    """① 全量区间：日期/成交额/指标正确（沪+深逐日相加）"""
    orig = _with_fixture()
    try:
        r = AppAMO.call_amo("2024-01-01", "2024-01-31")
    finally:
        _restore_config(orig)

    if r["dates"] != EXPECTED_DATES:
        failures.append(f"① 日期序列错误: {r['dates']}")
        print(f"[FAIL] ① 日期序列: {r['dates']}")
        return
    if r["amounts"] != EXPECTED_AMOUNTS:
        failures.append(f"① 成交额(亿)错误: {r['amounts']} != {EXPECTED_AMOUNTS}")
        print(f"[FAIL] ① 成交额: {r['amounts']}")
        return
    # 峰值=2500(2024/01/05)，当前=1400(2024/01/08)，缩至峰值占比=(1400/2500)*100=56.0
    st = r["stats"]
    if st["peak"] != 2500.0 or st["peak_date"] != "2024/01/05":
        failures.append(f"① 峰值错误: {st}")
        print(f"[FAIL] ① 峰值: {st}")
        return
    if st["current"] != 1400.0 or st["current_date"] != "2024/01/08":
        failures.append(f"① 当前值错误: {st}")
        print(f"[FAIL] ① 当前值: {st}")
        return
    if st["peak_ratio"] != 56.0:
        failures.append(f"① 缩至峰值占比错误: {st['peak_ratio']}")
        print(f"[FAIL] ① 缩至峰值占比: {st['peak_ratio']}")
        return
    print("[PASS] ① 全量区间: 日期/成交额/峰值/当前/缩至峰值占比 均正确")


def test_viewport_filter(failures):
    """② 视口区间过滤：只取 [start_date, end_date]（含两端）"""
    orig = _with_fixture()
    try:
        r = AppAMO.call_amo("2024-01-03", "2024-01-04")
    finally:
        _restore_config(orig)

    if r["dates"] != ["2024/01/03", "2024/01/04"]:
        failures.append(f"② 区间过滤日期错误: {r['dates']}")
        print(f"[FAIL] ② 区间过滤: {r['dates']}")
        return
    if r["amounts"] != [2100.0, 1600.0]:
        failures.append(f"② 区间过滤成交额错误: {r['amounts']}")
        print(f"[FAIL] ② 区间过滤成交额: {r['amounts']}")
        return
    # 区间内峰值=2100(01-03)，当前=1600(01-04)，缩至峰值占比=(1600/2100)*100=76.19
    st = r["stats"]
    if st["peak"] != 2100.0 or st["peak_date"] != "2024/01/03":
        failures.append(f"② 区间内峰值错误: {st}")
        print(f"[FAIL] ② 区间内峰值: {st}")
        return
    if st["current"] != 1600.0 or st["current_date"] != "2024/01/04":
        failures.append(f"② 区间内当前值错误: {st}")
        print(f"[FAIL] ② 区间内当前值: {st}")
        return
    if st["peak_ratio"] != 76.19:
        failures.append(f"② 区间内缩至峰值占比错误: {st['peak_ratio']}")
        print(f"[FAIL] ② 区间内缩至峰值占比: {st['peak_ratio']}")
        return
    print("[PASS] ② 视口区间过滤: 只含区间内日期，指标按区间内数据计算")


def test_empty_range(failures):
    """③ 区间无数据：空序列 + stats 全 None"""
    orig = _with_fixture()
    try:
        r = AppAMO.call_amo("2025-01-01", "2025-01-31")
    finally:
        _restore_config(orig)

    if r["dates"] != [] or r["amounts"] != []:
        failures.append(f"③ 空区间应返回空序列: {r}")
        print(f"[FAIL] ③ 空区间: {r}")
        return
    st = r["stats"]
    if st != {"peak": None, "peak_date": None, "current": None,
              "current_date": None, "peak_ratio": None}:
        failures.append(f"③ 空区间 stats 应为全 None: {st}")
        print(f"[FAIL] ③ 空区间 stats: {st}")
        return
    print("[PASS] ③ 区间无数据: 空序列 + stats 全 None")


def test_missing_file(failures):
    """④ 数据源缺失：抛 DataFetchError（无兜底）"""
    empty_dir = tempfile.mkdtemp(prefix="amo_empty_")
    orig = app_config.tdx_install_dir
    app_config.tdx_install_dir = empty_dir
    try:
        try:
            AppAMO.call_amo("2024-01-01", "2024-01-31")
            failures.append("④ 缺失数据源应抛 DataFetchError")
            print("[FAIL] ④ 缺失数据源未抛异常")
            return
        except DataFetchError:
            pass
    finally:
        _restore_config(orig)
    print("[PASS] ④ 数据源缺失: 抛 DataFetchError（无兜底）")


def test_no_persistence(failures):
    """⑤ 无持久化：模块不存状态，重复调用结果一致"""
    orig = _with_fixture()
    try:
        r1 = AppAMO.call_amo("2024-01-01", "2024-01-31")
        r2 = AppAMO.call_amo("2024-01-01", "2024-01-31")
    finally:
        _restore_config(orig)

    if r1 != r2:
        failures.append("⑤ 重复调用结果应一致（无持久化状态）")
        print("[FAIL] ⑤ 重复调用结果不一致")
        return
    # 模块级不应有缓存类全局变量（__cached__ 为解释器自动生成，排除）
    cached = [k for k in vars(AppAMO)
              if ("cache" in k.lower() or "store" in k.lower())
              and not k.startswith("__")]
    if cached:
        failures.append(f"⑤ 模块存在疑似缓存状态: {cached}")
        print(f"[FAIL] ⑤ 模块疑似缓存: {cached}")
        return
    print("[PASS] ⑤ 无持久化: 重复调用一致，无模块级缓存")


def test_slash_input(failures):
    """⑥ 斜杠入参：前端契约 %Y/%m/%d（与全站 K 线日期一致），
    内部转连字符比较后仍能命中区间数据"""
    orig = _with_fixture()
    try:
        # 前端实际发送的斜杠日期（loadAmoData 直接 slice kline date）
        r = AppAMO.call_amo("2024/01/03", "2024/01/04")
    finally:
        _restore_config(orig)

    if r["dates"] != ["2024/01/03", "2024/01/04"]:
        failures.append(f"⑥ 斜杠入参应命中区间: {r['dates']}")
        print(f"[FAIL] ⑥ 斜杠入参: {r['dates']}")
        return
    if r["amounts"] != [2100.0, 1600.0]:
        failures.append(f"⑥ 斜杠入参成交额错误: {r['amounts']}")
        print(f"[FAIL] ⑥ 斜杠入参成交额: {r['amounts']}")
        return
    print("[PASS] ⑥ 斜杠入参: %Y/%m/%d 可正确命中区间（修复无数据显示）")


def test_date_format_guard(failures):
    """⑦ 日期契约守护：输出日期必须全为斜杠 %Y/%m/%d，非其它。

    AppAMO 与全站 K 线日期契约（%Y/%m/%d）对齐；历史上多次因误用连字符
    （%Y-%m-%d）导致「区间比对失败 → 无数据显示」。本守护分两层：

      a. 行为层：真实查询后，dates / peak_date / current_date 每个日期
         都必须命中 ^\d{4}/\d{2}/\d{2}$（严格斜杠，长度 10），一旦有人把
         call_amo 的输出日期改回连字符格式即失败。
      b. 结构层：AppAMO.py 源码必须同时存在「斜杠→连字符」入参归一化
         （_norm_date 内 .replace("/", "-")）与「连字符→斜杠」输出转换
         （.replace("-", "/")），防止未来重构悄悄丢掉该契约。
    """
    import inspect

    # a. 行为层：输出日期严格斜杠
    orig = _with_fixture()
    try:
        r = AppAMO.call_amo("2024-01-01", "2024-01-31")
    finally:
        _restore_config(orig)
    bad = [d for d in r["dates"] if not SLASH_DATE_RE.match(d)]
    st = r["stats"]
    for name, val in (("peak_date", st["peak_date"]),
                      ("current_date", st["current_date"])):
        if val is not None and not SLASH_DATE_RE.match(val):
            bad.append(f"{name}={val}")
    if bad:
        failures.append(f"⑦ 日期契约(行为): 存在非 %Y/%m/%d 的日期: {bad}")
        print(f"[FAIL] ⑦ 日期契约(行为): 非斜杠日期: {bad}")
        return

    # b. 结构层：源码必须含「斜杠→连字符」入参归一化 「连字符→斜杠」输出转换
    src = inspect.getsource(AppAMO).replace(" ", "")
    if ".replace('/','-')" not in src and '.replace("/","-")' not in src:
        failures.append("⑦ 日期契约(结构): AppAMO 缺失斜杠→连字符归一化"
                        "（_norm_date 的 .replace('/','-') 被删）")
        print("[FAIL] ⑦ 日期契约(结构): 缺失斜杠→连字符归一化")
        return
    if ".replace('-','/')" not in src and '.replace("-","/")' not in src:
        failures.append("⑦ 日期契约(结构): AppAMO 缺失连字符→斜杠输出转换"
                        "（.replace('-','/') 被删）")
        print("[FAIL] ⑦ 日期契约(结构): 缺失连字符→斜杠输出转换")
        return
    print("[PASS] ⑦ 日期契约守护: 输出全为 %Y/%m/%d（行为+结构两层）")


def main():
    failures = []
    test_full_range(failures)
    test_viewport_filter(failures)
    test_empty_range(failures)
    test_missing_file(failures)
    test_no_persistence(failures)
    test_slash_input(failures)
    test_date_format_guard(failures)

    print()
    if failures:
        print(f"===== 市场量能单元测试: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== 市场量能单元测试: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
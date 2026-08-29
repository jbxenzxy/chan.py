# -*- coding: utf-8 -*-
"""
代码输入处理链路守护测试（沪深重名消歧）
=====================================================================
背景：左上角代码输入 → search 候选 → 引擎代码解析 是「沪市/深市重名」的
关键链路。例如 000001，沪市是上证指数(sh000001)，深市是平安银行(sz000001)，
当前能正确处理。但历史上每次新增功能（港股/扩展指数/前缀后缀新写法等）
都容易把这条路改坏——由于市场判定规则、缓存键、大小写约定散落在多处，
常规功能变更极易在无感知处引入回归。

本用例把这些易碎点冻结为显式契约，离线可跑、零行情依赖：

  ① 旧写法一律拒绝：大写 market、点号连接、code 在前、.SH/.HK 后缀
     必须返回 (None, 原样)，绝不静默兼容（杜绝新功能又照着旧做法写）
  ② 标准写法唯一：market(小写)+code(数字)，market 在前、无连接符
     （sh600519 / sz000001 / hk00700 / ds932000 / bj430047）
  ③ 纯数字默认规则：000001(6位首0)→sz 平安银行；600xxx→sh；30xxxx→sz
  ④ 港股数字规范化：5位/4位补零（00700 / 9926→09926）
  ⑤ 别名速记：中证2000→ds932000；HSI/HSTECH/HKHSTECH→hk；HSTECH.HK 拒绝
  ⑥ search(000001) 必须同时返回 sh000001(上证指数) 与 sz000001(平安银行)，
     防止按纯数字去重/排序丢弃其中一个市场
  ⑦ 名称解析不得坍缩：_get_stock_name('sh','000001') 与 ('sz','000001')
     必须得到不同名称

运行：python Test/test_code_resolution_guards.py
      python Test/test_code_resolution_guards.py --update   # 兼容 run_all --update
"""
import argparse
import os
import re
import sys

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


def _get_common(TC, patch, tmp, cases, label):
    """跑一组 (输入, 期望(market,code)) 断言，逐项冻结；任一失败即收集。"""
    from App import utils as u
    from App import AppEngine as m
    n_fail = 0
    for inp, expected in cases:
        got = m._get_stock_market_code(inp)
        if got != expected:
            TC.failures.append(f"[{label}] {inp!r} -> {got} != {expected}")
            print(f"[FAIL] [{label}] {inp!r} -> {got} != {expected}")
            n_fail += 1
    if not n_fail:
        print(f"[PASS] {label}: {len(cases)} 组全部命中")
    return n_fail == 0


# ═══════════════════════════════════════════════════════════════════
# ① 旧写法一律拒绝（大写/点号/code在前/.HK·.SH后缀）
# ═══════════════════════════════════════════════════════════════════
def test_resolve_old_forms_rejected(TC):
    """① 一切历史写法必须返回 (None, 原样)，绝不静默归一。

    标准写法唯一 = market(小写)+code(数字)，market 在前、无连接符。
    以下旧写法（大写 market、点号连接、code 在前、.SH/.HK 后缀）若被新功能
    沿用、或前端传入，都必须被严格拒绝，防止新旧格式并存、永改不完。
    """
    cases = [
        # 大写 market（前）
        ("SH000001", None), ("SZ000001", None), ("SH600519", None),
        ("SZ300750", None), ("DS932000", None), ("HK00700", None),
        # 点号连接（market.前 / code.后）
        ("000001.SH", None), ("000001.SZ", None), ("600519.SH", None),
        ("sz.000001", None), ("sh.600519", None), ("SZ.600519", None),
        ("00700.hk", None), ("9926.hk", None), ("932000.DS", None),
        # code 在前（无点）
        ("654321sz", None), ("600519sh", None),
        # 非法复合
        ("SH000001.SH", None),
    ]
    from App import utils as u
    n = 0
    for inp, _ in cases:
        got = u._get_stock_market_code(inp)
        if got not in (None, (None, inp)):
            TC.failures.append(f"[①拒绝旧写法] {inp!r} -> {got}，未按标准拒绝")
            print(f"[FAIL] [①拒绝旧写法] {inp!r} -> {got}")
            n += 1
    # 显式验证：必须返回 (None, 原样)，保持原字符串原样回传
    for inp in ["SH000001", "000001.SH", "SZ.600519", "654321.sz", "00700.hk"]:
        got = u._get_stock_market_code(inp)
        if got != (None, inp):
            TC.failures.append(f"[①拒绝旧写法] {inp!r} -> {got} != (None, {inp!r})")
            print(f"[FAIL] [①拒绝旧写法] {inp!r} -> {got}")
            n += 1
    if not n:
        print(f"[PASS] ①旧写法拒绝: {len(cases)}+5 组大写/点号/后缀写法全部返回 (None, 原样)")


# ═══════════════════════════════════════════════════════════════════
# ② 纯数字默认市场规则（沪深同号的关键消歧地面规则）
# ═══════════════════════════════════════════════════════════════════
def test_resolve_bare_a_share(TC):
    """② 裸 6 位纯数字按首位落到默认市场；000001 默认=深市平安银行。"""
    cases = [
        ("000001", ("sz", "000001")),   # 首0→深市
        ("000300", ("sz", "000300")),   # 沪深300 裸输入默认深市（选点后前端会带前缀）
        ("600519", ("sh", "600519")),   # 首6→沪市
        ("600000", ("sh", "600000")),
        ("300750", ("sz", "300750")),   # 首3→深市
        ("510300", ("sh", "510300")),   # 首5→沪市（ETF）
        ("159915", ("sz", "159915")),   # 首1→深市（ETF）
    ]
    _get_common(TC, None, None, cases, "②裸A股")


# ═══════════════════════════════════════════════════════════════════
# ③ 港股数字规范化
# ═══════════════════════════════════════════════════════════════════
def test_resolve_bare_hk(TC):
    """③ 5位纯数字→港股；4位补前导零（9926→09926 / 00700）。"""
    cases = [
        ("00700", ("hk", "00700")),
        ("09926", ("hk", "09926")),
        ("9926", ("hk", "09926")),   # 4位补零
        ("09618", ("hk", "09618")),
    ]
    _get_common(TC, None, None, cases, "③港股裸代码")


# ═══════════════════════════════════════════════════════════════════
# ④ 别名速记（扩展指数 / 港股指数 / HK 前缀字母）
# ═══════════════════════════════════════════════════════════════════
def test_resolve_aliases(TC):
    """④ 别名与字母代码速记不得回归；带点/后缀字母缩写一律拒绝。"""
    from App import utils as u
    n = 0
    def check(inp, expected, label="④别名"):
        got = u._get_stock_market_code(inp)
        if got != expected:
            TC.failures.append(f"[{label}] {inp!r} -> {got} != {expected}")
            print(f"[FAIL] [{label}] {inp!r} -> {got} != {expected}")
            return 1
        return 0
    n += check("ZZ2000", ("ds", "932000"))
    n += check("ZZ2", ("ds", "932000"))
    n += check("中证2000", ("ds", "932000"))   # 中文别名，不经 upper 即可命中
    n += check("932000", ("ds", "932000"))
    n += check("HSI", ("hk", "HSI"))
    n += check("HSTECH", ("hk", "HSTECH"))
    n += check("HSCEI", ("hk", "HSCEI"))
    # 恒生科技 / 恒生创新药 的英文代码与拼音首字母速记
    n += check("HSIDI", ("hk", "HSIDI"))       # 恒生创新药指数
    n += check("HSKJ", ("hk", "HSTECH"))       # 恒生科技（拼音首字母）
    n += check("HSCXY", ("hk", "HSIDI"))       # 恒生创新药（拼音首字母）
    # hk+指数名 便捷写法（标准"hk 前缀+字母"，无点、无后缀）
    n += check("HKHSTECH", ("hk", "HSTECH"))
    n += check("HkHSTECH", ("hk", "HSTECH"))
    # hk+HZ 通达信港股指数文件代码（反向查表归一为字母代码）
    n += check("hkHZ5489", ("hk", "HSIDI"))   # 恒生创新药
    n += check("hkHZ5017", ("hk", "HSTECH"))  # 恒生科技
    n += check("HKHZ5489", ("hk", "HSIDI"))   # 前缀大小写不敏感
    # 带点/后缀的缩写：非标准，一律拒绝
    n += check("HSTECH.HK", (None, "HSTECH.HK"))
    n += check("HSTECHHK", (None, "HSTECHHK"))
    n += check("hstech.HK", (None, "hstech.HK"))
    # 中文别名：_get_stock_market_code 不 upper，中文不受影响，单独验证其经公开入口可命中
    got = u._get_market_code("中证2000")
    if got != ("ds", "932000"):
        TC.failures.append(f"[④别名] 公开入口 '中证2000' -> {got} != ('ds','932000')")
        print(f"[FAIL] [④别名] 公开入口 '中证2000' -> {got}")
        n += 1
    if not n:
        print(f"[PASS] ④别名速记全部命中；HSTECH.HK / HSTECHHK 带点后缀缩写已拒绝")


# ═══════════════════════════════════════════════════════════════════
# ②b 标准写法唯一：market(小写)+code(数字) 通过，其它一律拒绝
# ═══════════════════════════════════════════════════════════════════
def test_only_standard_writes(TC):
    """②b 唯一标准 market(小写)+code 必须解析成功；其余写法一律 (None, 原样)。"""
    from App import utils as u
    pass_cases = [
        ("sh000001", ("sh", "000001")),
        ("sz000001", ("sz", "000001")),
        ("sh600519", ("sh", "600519")),
        ("sz300750", ("sz", "300750")),
        ("bj430047", ("bj", "430047")),
        ("hk00700", ("hk", "00700")),
        ("hk09926", ("hk", "09926")),
        ("hk9926", ("hk", "09926")),   # 4位港股带前缀补零
        ("ds932000", ("ds", "932000")),
    ]
    reject_cases = [
        "654321sz", "654321.sz", "654321.SZ", "600519.SH",
        "sz.654321", "SZ654321", "SH.600519", "600519sh", "SZ.600519",
        "932000.DS", "DS932000", "hk.09926", "00700.hk", "9926.hk", "hk00700fake",
    ]
    n = 0
    for inp, expected in pass_cases:
        got = u._get_stock_market_code(inp)
        if got != expected:
            TC.failures.append(f"[②b标准] {inp!r} -> {got} != {expected}")
            print(f"[FAIL] [②b标准] {inp!r} -> {got} != {expected}")
            n += 1
    for inp in reject_cases:
        got = u._get_stock_market_code(inp)
        if got[0] is not None:
            TC.failures.append(f"[②b标准] 旧写法 {inp!r} -> {got}，未拒绝")
            print(f"[FAIL] [②b标准] 旧写法 {inp!r} -> {got}")
            n += 1
    if not n:
        print(f"[PASS] ②b标准: {len(pass_cases)} 组标准写法通过，{len(reject_cases)} 组旧写法被拒绝")


# ═══════════════════════════════════════════════════════════════════
# ⑤ 公开入口契约（前端/引擎传标准格式也必须解析成功）
# ═══════════════════════════════════════════════════════════════════
def test_public_entry_lowercase(TC):
    """⑤ 公开入口 _get_market_code / _get_stock_market_code 标准格式必须成功。

    标准写法唯一 = market(小写)+code：全小写、无点、market 在前。
    """
    from App import utils as u

    def check(label, fn, cases):
        n = 0
        for inp, expected in cases:
            got = fn(inp)
            if got != expected:
                TC.failures.append(f"[⑤{label}] {inp!r} -> {got} != {expected}")
                print(f"[FAIL] [⑤{label}] {inp!r} -> {got} != {expected}")
                n += 1
        return n == 0

    ok1 = check("公开入口", u._get_market_code, [
        ("sh000001", ("sh", "000001")),
        ("sz000001", ("sz", "000001")),
        ("sh600519", ("sh", "600519")),
        ("000001", ("sz", "000001")),
        ("hk000700", ("hk", "000700")),
    ])
    # 直呼 _get_stock_market_code：标准格式必须成功；旧写法（混用大小写/带点）拒绝
    ok2 = check("直呼内部", u._get_stock_market_code, [
        ("sh000001", ("sh", "000001")),
        ("sz000001", ("sz", "000001")),
        ("hkHSI", ("hk", "HSI")),
        ("zz2000", ("ds", "932000")),
        ("Sh000001", (None, "Sh000001")),   # 混用大写拒绝
        ("hstech.HK", (None, "hstech.HK")),  # 带点后缀拒绝
    ])
    if ok1 and ok2:
        print(f"[PASS] ⑤公开入口: 标准格式全过，混用大小写/带点写法均按契约拒绝")


# ═══════════════════════════════════════════════════════════════════
# ⑥ search(000001) 必须同时返回沪/深两个市场候选
# ═══════════════════════════════════════════════════════════════════
def _inject_names(TC, patch, tmp, mapping):
    """向缓存注入基本面数据并标记已加载（离线、零文件依赖）。"""
    from App.AppData import app_data
    app_data._names.clear()
    app_data._names.update(mapping)
    app_data._names_loaded = True


def test_search_dup_both_markets(TC, patch, tmp):
    """⑥ 纯数字 000001 搜索必须同时返回 sh 上证指数 与 sz 平安银行。

    防止新功能在 search_stocks 里按「纯数字去重」「bucket 后截断」或
    排序变更时把其中一个市场丢弃，导致重名证券再不可选。
    """
    from App import AppChart as chart
    mapping = {
        "sh000001": {"name": "上证指数", "pinyin": "SZZS", "market": "sh"},
        "sz000001": {"name": "平安银行", "pinyin": "PAYH", "market": "sz"},
        "sh600519": {"name": "贵州茅台", "pinyin": "GZMT", "market": "sh"},
    }
    _inject_names(TC, patch, tmp, mapping)
    # search_stocks 开头有「缓存文件不存在→need_refresh」门，需 mock exists 放行
    with patch("os.path.exists") as p:
        p.return_value = True
        r = chart.search_stocks("000001")
    items = [(it["market"], it["code"], it["name"]) for it in r.get("results", [])]
    if ("sh", "000001", "上证指数") not in items:
        TC.failures.append(f"[⑥search] 缺少 sh 上证指数: {items}")
        print(f"[FAIL] [⑥search] 000001 未返回 sh 上证指数: {items}")
        return
    if ("sz", "000001", "平安银行") not in items:
        TC.failures.append(f"[⑥search] 缺少 sz 平安银行: {items}")
        print(f"[FAIL] [⑥search] 000001 未返回 sz 平安银行: {items}")
        return
    print(f"[PASS] ⑥search(000001): 同时命中 sh上证指数 + sz平安银行")


# ═══════════════════════════════════════════════════════════════════
# ⑦ 名称解析不得坍缩：sh000001 与 sz000001 必须不同名
# ═══════════════════════════════════════════════════════════════════
def test_names_not_collapsed(TC, patch, tmp):
    """⑦ get_stock_name 按 market+code 区分，同一纯数字不得坍缩为同一名称。"""
    from App.AppData import app_data
    mapping = {
        "sh000001": {"name": "上证指数", "pinyin": "SZZS", "market": "sh"},
        "sz000001": {"name": "平安银行", "pinyin": "PAYH", "market": "sz"},
    }
    _inject_names(TC, patch, tmp, mapping)
    n_sh = app_data.get_stock_name("sh", "000001")
    n_sz = app_data.get_stock_name("sz", "000001")
    if n_sh != "上证指数" or n_sz != "平安银行" or n_sh == n_sz:
        TC.failures.append(f"[⑦名称] 沪深同名坍缩: sh={n_sh!r} sz={n_sz!r}")
        print(f"[FAIL] [⑦名称] 沪深同名坍缩: sh000001={n_sh!r} sz000001={n_sz!r}")
        return
    print(f"[PASS] ⑦名称: sh000001=上证指数 / sz000001=平安银行")


# ═══════════════════════════════════════════════════════════════════
# ⑧ 前端输入框消歧路径守卫（源码 canary）
# ═══════════════════════════════════════════════════════════════════
def test_frontend_input_guard(TC, patch, tmp):
    """⑧ 前端左上角输入框：带市场限定代码跳过搜索、纯数字进入重名搜索。

    两条源码守卫链：
     · 输入→消歧：显式市场代码（含新旧写法）不作为纯数字搜索词交后端严格校验；
     · 前端绝不再存在旧写法归一化（dotMatch/prefixMatch 把 600519.SH 拼成 sh600519），
       否则新功能会继续沿用"前端帮你转格式"的老做法，永改不完。
    """
    path = os.path.join(REPO_ROOT, "Frontend", "app.js")
    if not os.path.exists(path):
        TC.failures.append(f"[⑧前端] 未找到 Frontend/app.js")
        print(f"[FAIL] [⑧前端] 缺失 {path}")
        return
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    problems = []
    if "stock-code-input" not in src:
        problems.append("缺少 stock-code-input 输入框绑定")
    # 候选点击传 fullCode 必须保留市场前缀（market+code），否则同号证券无法区分
    if "selectHistory" not in src or ("safeMarket" not in src and "item.market" not in src):
        problems.append("搜索候选选择丢失市场信息（market+code 拼接被改动）")
    # 显式市场（前/后、可带点）输入须整体被识别为「非纯数字搜索」，交后端严格校验
    if "/^(sh|sz|bj|hk)[.]?\\d+$/i" not in src and "^\\d+[.]?(sh|sz|bj|hk)$" not in src:
        problems.append("前端缺少 market 前后缀(可带点)的显式识别正则")
    if "loadStock" not in src:
        problems.append("缺少 loadStock 加载逻辑")
    # 前端不得再有旧写法归一化（把点号/大写转成市场前缀）——否则旧做法继续留存在前端
    for forbidden in ["dotMatch", "prefixMatch", ".toLowerCase() + dotMatch", "match(/^(SH|SZ|HK|BJ|DS)"]:
        if forbidden in src:
            problems.append(f"前端仍残留旧写法归一化代码: {forbidden}")
    # 硬编码在 analyze 请求里的默认代码字面量必须能被严格解析器识别为标准格式
    # （大小写/点号旧写法会 404/400 → 首屏 initDefault"默认加载失败"崩屏）。
    # 历史血泪：initDefault 曾遗漏大写 "SH000001"，后端拒绝后直接抛"默认加载失败"。
    from App import utils as _u
    default_codes = re.findall(r'encodeURIComponent\("([^"]+)"\)\s*\+\s*"/analyze', src)
    for code in default_codes:
        mkt, _bare = _u._get_stock_market_code(code)
        if mkt is None:
            problems.append(f"前端默认/链接代码字面量非标准格式(严格解析失败): {code!r}")
    if problems:
        TC.failures.append("[⑧前端] " + "；".join(problems))
        print("[FAIL] [⑧前端] " + "；".join(problems))
        return
    print("[PASS] ⑧前端: 输入→search→选候→loadStock 链路完整，旧写法归一化已清除")


# ═══════════════════════════════════════════════════════════════════
# ⑨ 后端 analyze 输出 meta.symbol 必须为标准格式（代码 canary）
# ═══════════════════════════════════════════════════════════════════
def test_backend_symbol_output(TC):
    """⑨ 后端把 meta.symbol 喂给前端并作为本地缓存/实时回传的 key。

    若 symbol 退回旧点号格式 "code.market"（如 000001.SH），前端会把它原样
    回传后端 → 被严格解析器拒绝 → 实时/刷新/区间选择全断；且本地 lastCodeFreq
    会存旧写法，下次启动恢复必然失败。故后端输出必须为 market + code。
    """
    path = os.path.join(REPO_ROOT, "App", "AppEngine.py")
    if not os.path.exists(path):
        TC.failures.append("[⑨symbol] 未找到 App/AppEngine.py")
        print(f"[FAIL] [⑨symbol] 缺失 {path}")
        return
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    problems = []
    # 标准输出：symbol = market + code（这两处是单/双窗股票 meta）
    if src.count('"symbol": market + code,') < 1:
        problems.append("meta.symbol 缺失标准格式 'market + code'（单/双窗至少一处）")
    # 禁止退回旧点号写法
    if '"symbol": f"{code}.{market.upper()}"' in src or \
       '"symbol": f"{normalized_code}.{market.upper()}"' in src:
        problems.append("meta.symbol 仍输出旧点号写法 code.market（须改市场前缀在前）")
    if problems:
        TC.failures.append("[⑨symbol] " + "；".join(problems))
        print("[FAIL] [⑨symbol] " + "；".join(problems))
        return
    print("[PASS] ⑨symbol: 后端 meta.symbol 为标准格式 market+code，无旧点号输出")


# ═══════════════════════════════════════════════════════════════════
# ⑩ 内部缓存/选点 key 必须全走标准格式（无旧点号·大写痕迹）
# ═══════════════════════════════════════════════════════════════════
def test_internal_key_standard(TC):
    """⑩ 内部键（选点 saved_point_times / 标注 {code}_{freq} / 双窗下窗缓存
    stocks_sub_cache_key）一律为标准 market(小写)+code，无连接符、无大写上转。

    决策：存量旧格式数据「作废就作废」，不兼容、不迁移、不回流；代码里不得
    保留任何旧点号/大写的生成痕迹，否则新功能照抄又分叉。质检三文件出现
    下述任一旧写法即判失败。
    """
    targets = [
        os.path.join(REPO_ROOT, "App", "AppEngine.py"),
        os.path.join(REPO_ROOT, "App", "AppChart.py"),
        os.path.join(REPO_ROOT, "App", "AppData.py"),
    ]
    # 旧格式生成痕迹：点号拼接（code.market / market.code）与大写上转
    forbidden = [
        'f"{market}.{code}"',
        'f"{code}.{market}"',
        'f"{market.upper()}{code}"',
        'f"{market}.{normalized_code}"',
        'f"{code}.{market.upper()}"',
        'f"{normalized_code}.{market.upper()}"',
        'str(chan_code).upper()}',
        'str(chan_code).upper():',
    ]
    problems = []
    for path in targets:
        if not os.path.exists(path):
            problems.append(f"缺失 {path}")
            continue
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        for bad in forbidden:
            if bad in src:
                problems.append(f"{os.path.basename(path)} 仍含旧格式痕迹: {bad}")
    # 正向：qualified_code 生成必须是无连接符的 market + code
    for rel, needle in [
        ("AppEngine.py", "qualified_code = market + code"),
        ("AppChart.py", "qualified_code = market + normalized_code"),
        ("AppData.py", "qualified_code = market + normalized_code"),
    ]:
        p = os.path.join(REPO_ROOT, "App", rel)
        if not os.path.exists(p):
            continue
        with open(p, "r", encoding="utf-8") as f:
            src = f.read()
        if needle not in src:
            problems.append(f"{rel} 缺失标准 qualified_code 生成: {needle}")
    if problems:
        TC.failures.append("[⑩内部键] " + "；".join(problems))
        print("[FAIL] [⑩内部键] " + "；".join(problems))
        return
    print("[PASS] ⑩内部键: 选点/标注/双窗下窗缓存键全部为标准 market+code，无旧点号·大写痕迹")


# ═══════════════════════════════════════════════════════════════════
# ⑪ 股票扫描来源：指数成分股识别（前端置灰+后端归一）
# ═══════════════════════════════════════════════════════════════════
def test_scan_index_source(TC):
    """⑪ 打开指数，「扫描来源→成分股」置灰与否 = 当前打开的是不是指数。

    判定本质是「当前打开的是不是指数」，由后端权威给出 meta.is_index（与引擎
    同源，见 App/utils.is_index），前端只读它，不再靠 code 正则自行推断。
    本用例守护：① is_index 对沪深同号(000001)按 market 区分（sh000001 指数可用 /
sz000001 平安银行股票灰化）；② meta 注入该字段；③ 前端只读 meta.is_index，无任何正则兜底。
    """
    # ── ① 单一事实源 is_index(market, code) 的判定表 ──
    from App.utils import is_index
    index_cases = {
        ("sh", "000001"): True, ("sz", "000001"): False,   # 沪深同号：上证指数≠平安银行
        ("sh", "000300"): True, ("sh", "880491"): True, ("sh", "881319"): True,
        ("sh", "990001"): True, ("sh", "600519"): False,
        ("sz", "399001"): True, ("sz", "399006"): True, ("sz", "000063"): False,
        ("sz", "300750"): False, ("ds", "932000"): True,
        ("hk", "HSTECH"): True, ("hk", "00700"): False, ("bj", "430047"): False,
    }
    problems = []
    for (mkt, code), expect in index_cases.items():
        got = is_index(mkt, code)
        if got != expect:
            problems.append(f"[⑪is_index] {mkt}{code} -> {got} != {expect}")

    # ── ② 后端 meta 注入 is_index（AppEngine 主 meta）──
    engine_path = os.path.join(REPO_ROOT, "App", "AppEngine.py")
    if not os.path.exists(engine_path):
        problems.append("[⑪meta] 未找到 App/AppEngine.py")
    else:
        with open(engine_path, "r", encoding="utf-8") as f:
            esrc = f.read()
        if '"symbol": market + code' in esrc and '"is_index": is_index(market, code)' not in esrc:
            problems.append("[⑪meta] 主 meta 未注入 meta.is_index 权威字段")

    # ── ③ 后端：set_page_index_code 把标准 market+code 归一为裸板块代码 ──
    from App import AppScan as _scan
    backend_cases = {
        "sh880491": "880491", "sh881001": "881001", "sz399001": "399001",
        "ds932000": "932000", "sh000300": "000300",
        "sh000001": "000001", "sz000001": "000001",  # 归一为裸码；指数与否靠 is_index 区分
    }
    for inp, expect in backend_cases.items():
        try:
            r = _scan.scanner.set_page_index_code(inp)
        except Exception as e:
            problems.append(f"[⑪后端] set_page_index_code({inp!r}) 抛异常: {e}")
            continue
        got = r.get("code") if isinstance(r, dict) else None
        if got != expect:
            problems.append(f"[⑪后端] {inp!r} -> {got} != {expect}")

    # ── ④ 前端：置灰只读 meta.is_index 权威字段，零正则兜底（canary）──
    frontend_path = os.path.join(REPO_ROOT, "Frontend", "app.js")
    if not os.path.exists(frontend_path):
        problems.append("[⑪前端] 未找到 Frontend/app.js")
    else:
        with open(frontend_path, "r", encoding="utf-8") as f:
            src = f.read()
        if 'meta.is_index)' not in src:
            problems.append("前端置灰判定未从 meta.is_index 权威字段赋值得出")
        # 唯一事实源原则：前端不得再用 code 正则自行推断是否指数（任何兜底都是事实源分裂）
        if "_mkt" in src or "(sh|sz|bj|hk|ds)" in src:
            problems.append("前端仍残留按 code 正则推断指数的兜底逻辑（应仅依赖 meta.is_index）")
        # 不得再出现 blanket 排除 000001（否则上证指数 sh000001 也被灰化，与需求相悖）
        if "!/^000001" in src:
            problems.append("前端仍残留对 000001 的全量排除（上证指数成分股会被灰化）")
        # 旧写法：基于点号后缀排除深市股票（reform 后无点号，不应保留）
        if r"\.[Ss][Zz]$" in src:
            problems.append("前端仍残留点号后缀排除（.SZ）旧写法")
    if problems:
        TC.failures.append("；".join(problems))
        print("[FAIL] [⑪扫描来源] " + "；".join(problems))
        return
    print("[PASS] ⑪扫描来源: 置灰判定=后端 meta.is_index 权威（沪深同号按 market 区分）")


# ═══════════════════════════════════════════════════════════════════
# ⑫ 成分股抓取健壮性：限时 + 可中断 + 上证指数上交所直连
# ═══════════════════════════════════════════════════════════════════
def test_constituent_fetch_robustness(TC):
    """⑫ 成分股抓取不得卡死扫描、中断必须立即生效；上证指数走上交所官网直连。

    历史缺陷：沪深300/中证1000 走 akshare csindex 网络抓取，无超时、原生调用
    不可打断 → 扫描卡死、「中断扫描」也结束不了；上证指数(000001) 曾走通达信
    本地 vipdoc/sh/lday 枚举（该目录混有其它指数，不能作指数成分来源），后被
    先引入「盲 return []」，再又"卡网络"。本用例守护四点：
      ① _run_with_timeout：阻塞调用限时截断、正常返回透出；
      ② 000001 走 _read_sh_index_stocks_exchange（上交所 query.sse.com.cn 直连），
         与 399xxx 深交所直连风格一致；不得走本地枚举、不得盲 return []；
      ③ 上交所/深交所/中证 网络源都被 _run_with_timeout 限时包裹；
      ④ P1-5：AppScan 遗留中止链路（_scan_aborted / Scanner.abort）已删除，
         TdxAPI 通用限时机制 _run_with_timeout 保留。
    """
    problems = []
    # ── ① _run_with_timeout 行为 ──
    from DataAPI import TdxAPI as _T
    import time
    t0 = time.time()
    r = _T._run_with_timeout(lambda: time.sleep(99), timeout=1)
    if r is not None or time.time() - t0 >= 3:
        problems.append(f"[⑫超时] 阻塞调用未限时截断: r={r} elapsed={time.time()-t0:.1f}")
    if _T._run_with_timeout(lambda: 42, timeout=2) != 42:
        problems.append("[⑫正常] 正常返回值未被透出")

    # ── ② 000001 上交所直连（不走本地枚举 / 不盲空） ──
    tdx_path = os.path.join(REPO_ROOT, "DataAPI", "TdxAPI.py")
    with open(tdx_path, "r", encoding="utf-8") as f:
        tsrc = f.read()
    # 000001 分支必须路由到 exchange 直连，且不得残留本地枚举函数
    if "sector_code == \"000001\"" in tsrc and "_read_sh_index_stocks_exchange(" not in tsrc:
        problems.append("[⑫000001] 000001 分支未走上交所 exchange 直连") 
    if "_read_sh_index_stocks_local" in tsrc:
        problems.append("[⑫000001] 000001 仍残留本地 vipdoc/sh/lday 枚举（该目录混有其它指数）")
    if 'if sector_code == "000001":\n        return []' in tsrc:
        problems.append("[⑫000001] 000001 分支仍是盲 return []")
    # exchange 直连必须用上交所官方查询接口（同 399xxx 深交所直连风格）
    if "query.sse.com.cn/sseQuery/commonQuery.do" not in tsrc:
        problems.append("[⑫000001] 000001 未使用上交所官网 query.sse.com.cn 直连接口")

    # ── ③ 所有网络源（上交所/深交所/中证）都被 _run_with_timeout 限时包裹 ──
    n_to = tsrc.count("_run_with_timeout(")
    need_sources = ["query.sse.com.cn/sseQuery", "szse.cn/api/report/ShowReport", "index_stock_cons_csindex"]
    for pat in need_sources:
        if pat in tsrc and n_to < 3:
            problems.append(f"[⑫超时] 网络源 {pat} 未被 _run_with_timeout 限时包裹(共{n_to}处)")

    # ── ④ P1-5：遗留中止链路已删除，通用限时机制保留 ──
    scan_path = os.path.join(REPO_ROOT, "App", "AppScan.py")
    with open(scan_path, "r", encoding="utf-8") as f:
        ssrc = f.read()
    # 仅匹配实际遗留代码（def abort(self) 无参中止 / 模块级 _scan_aborted 旗），
    # 不误伤注释/文档说明与新的 abort_batch_scan(task_id) 任务级取消入口
    legacy_abort = (
        re.search(r"def\s+abort\s*\(\s*self\s*\)\s*:", ssrc)
        or re.search(r"global\s+_scan_aborted", ssrc)
        or re.search(r"_scan_aborted\s*=\s*(False|True)", ssrc)
        or re.search(r"if\s+_scan_aborted\s*:", ssrc)
    )
    if legacy_abort:
        problems.append("[⑫P1-5] AppScan 仍残留遗留中止链路（Scanner.abort / _scan_aborted，"
                        "前端已有 task cancel 语义，应删除）")
    # TdxAPI 通用限时机制（_run_with_timeout）必须保留（成分抓取防卡死兜底）
    if "_run_with_timeout" not in tsrc:
        problems.append("[⑫P1-5] TdxAPI 限时机制 _run_with_timeout 缺失（成分抓取无超时保护）")

    if problems:
        TC.failures.append("；".join(problems))
        print("[FAIL] [⑫成分健壮] " + "；".join(problems))
        return
    print("[PASS] ⑫成分健壮: 网络抓取限时可中断；000001=上交所官网直连（同399xxx）")


def main():
    ap = argparse.ArgumentParser(description="代码输入处理链路守护测试（沪深重名消歧）")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    args = ap.parse_args()

    from unittest import mock
    import tempfile
    from types import SimpleNamespace

    TC = SimpleNamespace(failures=[])
    patch = mock.patch
    tmp = tempfile.gettempdir()

    # 先注入基础的沪深同名数据，供 ⑥⑦ 复用（⑥⑦ 各自再次注入保证独立性）
    test_resolve_old_forms_rejected(TC)
    test_only_standard_writes(TC)
    test_resolve_bare_a_share(TC)
    test_resolve_bare_hk(TC)
    test_resolve_aliases(TC)
    test_public_entry_lowercase(TC)
    test_search_dup_both_markets(TC, patch, tmp)
    test_names_not_collapsed(TC, patch, tmp)
    test_frontend_input_guard(TC, patch, tmp)
    test_backend_symbol_output(TC)
    test_internal_key_standard(TC)
    test_scan_index_source(TC)
    test_constituent_fetch_robustness(TC)

    print()
    if TC.failures:
        print(f"===== 代码输入链路守护: 失败 {len(TC.failures)} 项 =====")
        for x in TC.failures:
            print(" -", x)
        return False
    print("===== 代码输入链路守护: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
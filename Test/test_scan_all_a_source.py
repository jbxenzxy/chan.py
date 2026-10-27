# -*- coding: utf-8 -*-
"""
股票扫描「全A股」来源 + 流通市值过滤阈值随请求传入 守护
=====================================================================
背景（2026-10-02）：
  ① 扫描来源新增 all_a（全A股 = 沪市+深市个股，不含北交所），数据源 =
     本地 vipdoc（与 K 线主源同源），按 TdxAPI 前缀常量单一事实源过滤个股段；
  ② 流通市值过滤从「仅成分股、阈值写死配置」改为「自选股/成分股/全A股
     任一来源都过滤、阈值随请求传入（前端设置抽屉配置，localStorage 持久化）；
     阈值 <=0 = 关闭过滤且不做市值批量取数」；
  ③ ST/退市过滤：此前名义上「仅成分股来源启用、check_st 即该开关」，实际两处
     漏洞使其全来源失效——market 只认数字编码（1/0/2）而调用方传 sh/sz/bj，
     名称键恒拼成 "bj"+code 查不到；且名称刷新在落盘前就删掉 ST 股。
     修复后判定谓词单点化（App.AppUtils.is_st_like_name）+ 名称表保留 ST 名，
     过滤对自选股/成分股/全A股统一生效（check_st 形参保留为开关，当前无调用方
     传 False；板块指数 2/3 来源不走预过滤）。
  ④ 阈值事实源收敛到后端：前端不再写死 50，未配置时不带参，由后端
     app_config.SCAN_MIN_FLOAT_MC 兜底（此前前端每次都带参，后端配置被架空）。

锁定契约：
  [1] read_all_a_stocks：vipdoc 合成目录下只收个股段（60/68/00/30），
      剔除指数（sh000/399）、B股、ETF/债券；bj 目录不收。
  [2] 阈值 0：不调 fetch_float_mc_all（省一次 eltdx 全表）、候选全保留。
  [3] 阈值 >0：自选股来源也按阈值过滤（低于跳过、高于保留）。
  [4] _SOURCE_READERS 登记 all_a →「全A股」。
  [5] ST/退市过滤对自选股同样生效（与成分股口径一致，不再是成分股独有）。
  [6] is_st_like_name 为唯一判定谓词；名称刷新不再删 ST；搜索端已接线。
  [7] 阈值单一事实源在后端：前端 _scanMinFloatMc 初值为 null（不写死数字），
      且仅在非 null 时才拼 &scan_min_float_mc=。
  [8] 后端经 /api/health 下发 config.scan_min_float_mc（as_dict 含该键）。
  [9] 新鲜度闸门（2026-10-26）：候选票最后一根 K 线早于「本批最新日期 -
      STALE_KLINE_DAYS 天」→ 剔除。判据：① 停牌/退市票被剔除；② 整机数据
      都旧时不误杀（基准是相对最新、不是今天）；③ 读不出日期（None）保留；
      ④ STALE_KLINE_DAYS<=0 关闭闸门；⑤ 分钟周期（2026-10-27）判据叠加
      .day 日期取较旧者——停牌票的 .lc5 是零量占位 bar（恒新鲜），
      单看分钟文件闸门形同虚设。

全程打桩，不联网、不触碰 App/ 生产数据（app_data 以 stub 替换）。
运行：python Test/test_scan_all_a_source.py
"""
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest.mock as mock
from datetime import datetime, timedelta

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import App.AppScan as _scan_mod


def _make_vipdoc(root, files):
    """合成 vipdoc 目录：files = [(相对路径, 文件名), ...]，内容为空字节。"""
    for rel_dir, fname in files:
        d = os.path.join(root, rel_dir)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fname), "wb") as f:
            f.write(b"\x00" * 16)


def test_all_a_prefix_filter(failures):
    """[1] vipdoc 合成目录：只收个股段，指数/B股/ETF/债券/北交所全部排除。"""
    tmp = tempfile.mkdtemp(prefix="scan_all_a_")
    try:
        _make_vipdoc(tmp, [
            ("sh/lday", "sh600519.day"),   # 沪主板 ✓
            ("sh/lday", "sh688001.day"),   # 科创板 ✓
            ("sh/lday", "sh000001.day"),   # 上证指数：collect 收录，个股过滤剔除
            ("sh/lday", "sh510300.day"),   # ETF：collect 已拒（51 段）
            ("sh/lday", "sh900901.day"),   # 沪B：collect 已拒（90 段）
            ("sh/lday", "sh113050.day"),   # 可转债：collect 已拒（11 段）
            ("sz/lday", "sz000001.day"),   # 深主板 ✓
            ("sz/lday", "sz300750.day"),   # 创业板 ✓
            ("sz/lday", "sz399001.day"),   # 深成指：collect 收录，个股过滤剔除
            ("sz/lday", "sz200002.day"),   # 深B：collect 已拒（20 段）
            ("sz/lday", "sz159915.day"),   # ETF：collect 已拒（15 段）
            ("bj/lday", "bj830799.day"),   # 北交所：collect 不扫 bj 目录
        ])
        stub_cfg = types.SimpleNamespace(vipdoc_dir=tmp)
        with mock.patch.object(_scan_mod, "app_config", stub_cfg):
            got = _scan_mod.read_all_a_stocks("d")
        expected = {("1", "600519"), ("1", "688001"),
                    ("0", "000001"), ("0", "300750")}
        actual = {(s["prefix"], s["code"]) for s in got}
        if actual != expected:
            failures.append(f"[1] 全A股过滤结果不符: {sorted(actual)} != {sorted(expected)}")
            print(f"[FAIL] [1] 全A股过滤: {sorted(actual)}")
        else:
            print(f"[PASS] [1] 全A股过滤: 只收个股段 {len(got)} 只（指数/B股/ETF/北交所全排除）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_stock_list(source="zxg", min_float_mc=None, mc_value=None):
    """打桩跑 stock_list，返回 (结果, fetch 调用次数)。"""
    stub_app_data = types.SimpleNamespace(
        load_float_mc_cache=lambda: None,
        float_mc_loaded=False,
        float_mc_count=lambda: 0,
        update_float_mc_cache=lambda d: None,
        float_mc_cache_stale=lambda: False,
        get_float_mc_from_cache=lambda code: mc_value,
        read_zxg_stocks=lambda: [{"prefix": "1", "code": "600519"}],
    )
    calls = {"n": 0}

    def _spy_fetch(stock_list):
        calls["n"] += 1
        return {}

    with mock.patch.object(_scan_mod, "app_data", stub_app_data), \
         mock.patch.object(_scan_mod, "read_zxg_stocks",
                           lambda: [{"prefix": "1", "code": "600519"}]), \
         mock.patch.object(_scan_mod, "fetch_float_mc_all", _spy_fetch):
        result = _scan_mod.scanner.stock_list("zxg", min_float_mc=min_float_mc)
    return result, calls["n"]


def test_minute_period_uses_fzline(failures):
    """[1b] 5m/15m/30m 读 fzline/*.lc5：lday 独有的票不进分钟候选。"""
    tmp = tempfile.mkdtemp(prefix="scan_all_a_m_")
    try:
        _make_vipdoc(tmp, [
            ("sh/lday", "sh600519.day"),    # 仅日线存在
            ("sh/fzline", "sh688001.lc5"),  # 仅5分钟线存在
            ("sz/fzline", "sz000001.lc5"),  # 深市分钟 ✓
            ("sz/fzline", "sz399001.lc5"),  # 深成指分钟：指数段剔除
            ("bj/fzline", "bj830799.lc5"),  # 北交所：不收
        ])
        stub_cfg = types.SimpleNamespace(vipdoc_dir=tmp)
        with mock.patch.object(_scan_mod, "app_config", stub_cfg):
            got_5m = _scan_mod.read_all_a_stocks("5m")
            got_30m = _scan_mod.read_all_a_stocks("30m")
        expected = {("1", "688001"), ("0", "000001")}
        actual = {(s["prefix"], s["code"]) for s in got_5m}
        if actual != expected or got_30m != got_5m:
            failures.append(f"[1b] 分钟候选不符: 5m={sorted(actual)} 30m==5m:{got_30m == got_5m}")
            print(f"[FAIL] [1b] 分钟周期: {sorted(actual)}")
        else:
            print("[PASS] [1b] 分钟周期: fzline/*.lc5 个股段（lday 独有票不进、指数剔除、30m 同 5m）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write_day_record(path, ymd):
    """写一条 32 字节日线记录，日期 = uint32 YYYYMMDD（与真实 .day 同格式）。"""
    with open(path, "wb") as f:
        f.write(struct.pack("<IIIIIfII", ymd, 1000, 1000, 1000, 1000, 0.0, 0, 0))


def _write_lc5_bars(path, bars):
    """写若干条 32 字节 .lc5 记录（与真实分钟线同格式），bars = [(y, m, d, vol)]。

    vol=0 即复现停牌票的「零成交量占位 bar」：日期新、价格冻结、无成交——
    [9d] 的关键夹具（此前的 16 字节空夹具 n_records=0 → None → 放行，
    分钟日期解码分支零覆盖）。
    """
    with open(path, "wb") as f:
        for (y, m, d, vol) in bars:
            num = (y - 2004) * 2048 + m * 100 + d
            f.write(struct.pack("<HHfffffII", num, 900, 10.0, 11.0, 9.0, 10.0, 0.0, vol, 0))


def test_minute_gate_overlays_day(failures):
    """[9d] 分钟周期闸门叠加 .day 判据：占位 .lc5 + 旧 .day ⇒ 剔除；
    真实 .lc5 + 新 .day ⇒ 保留；仅分钟线（无 .day）⇒ 保留。"""
    tmp = tempfile.mkdtemp(prefix="scan_all_a_stale3_")
    try:
        fz = os.path.join(tmp, "sz", "fzline")
        ld = os.path.join(tmp, "sz", "lday")
        os.makedirs(fz, exist_ok=True)
        os.makedirs(ld, exist_ok=True)
        # 本批最新 = 2026-10-23，阈值 20 天 ⇒ 早于 2026-10-03 的剔除
        # 停牌票（复现本机 sz000016 形态）：.lc5 续写占位 bar 到 10-23（恒新鲜），
        # .day 停在 09-14 ⇒ 叠加后按日线日期剔除
        _write_lc5_bars(os.path.join(fz, "sz000016.lc5"),
                        [(2026, 9, 14, 100), (2026, 10, 23, 0)])
        _write_day_record(os.path.join(ld, "sz000016.day"), 20260914)
        # 正常票：.lc5 有量到 10-23，.day 同步 ⇒ 留
        _write_lc5_bars(os.path.join(fz, "sz000002.lc5"),
                        [(2026, 9, 14, 100), (2026, 10, 23, 100)])
        _write_day_record(os.path.join(ld, "sz000002.day"), 20261023)
        # 仅分钟线（.day 缺失）：None 不参与比较 ⇒ 留（不因「判不出」误杀）
        _write_lc5_bars(os.path.join(fz, "sz000003.lc5"),
                        [(2026, 10, 23, 100)])
        with mock.patch.object(_scan_mod, "app_config",
                               types.SimpleNamespace(vipdoc_dir=tmp)):
            got = _scan_mod.read_all_a_stocks("5m")
        actual = {s["code"] for s in got}
        expected = {"000002", "000003"}
        if actual != expected:
            failures.append(f"[9d] 分钟闸门叠加 .day 结果不符: {sorted(actual)} != {sorted(expected)}")
            print(f"[FAIL] [9d] 分钟闸门叠加 .day: {sorted(actual)}")
        else:
            print("[PASS] [9d] 分钟周期叠加 .day：占位 .lc5 + 旧 .day 被剔除，"
                  "正常票与仅分钟票保留")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_gate(failures):
    """[9] 新鲜度闸门：停牌/退市票剔除，整机数据都旧时不误杀，读不出日期保留。"""
    tmp = tempfile.mkdtemp(prefix="scan_all_a_stale_")
    try:
        sh = os.path.join(tmp, "sh", "lday")
        os.makedirs(sh, exist_ok=True)
        # 本批最新 = 20261023，闸门阈值 20 天 ⇒ 早于 20261003 的剔除
        _write_day_record(os.path.join(sh, "sh600519.day"), 20261023)  # 当天 → 留
        _write_day_record(os.path.join(sh, "sh601066.day"), 20261022)  # 1 天前 → 留
        _write_day_record(os.path.join(sh, "sh601059.day"), 20260914)  # 停牌 39 天 → 剔
        _write_day_record(os.path.join(sh, "sh600000.day"), 20260901)  # 52 天前 → 剔
        with open(os.path.join(sh, "sh600036.day"), "wb") as f:
            f.write(b"\x00" * 16)                                      # 判不出日期 → 留

        stub_cfg = types.SimpleNamespace(vipdoc_dir=tmp)
        with mock.patch.object(_scan_mod, "app_config", stub_cfg):
            got = _scan_mod.read_all_a_stocks("d")
        actual = {s["code"] for s in got}
        expected = {"600519", "601066", "600036"}
        if actual != expected:
            failures.append(f"[9] 闸门结果不符: {sorted(actual)} != {sorted(expected)}")
            print(f"[FAIL] [9a] 新鲜度闸门: {sorted(actual)}")
        else:
            print("[PASS] [9a] 停牌/退市票被剔除，读不出日期的票保留")

        tmp2 = tempfile.mkdtemp(prefix="scan_all_a_stale2_")
        try:
            sh2 = os.path.join(tmp2, "sh", "lday")
            os.makedirs(sh2, exist_ok=True)
            _write_day_record(os.path.join(sh2, "sh600519.day"), 20260901)
            _write_day_record(os.path.join(sh2, "sh601066.day"), 20260820)
            with mock.patch.object(_scan_mod, "app_config",
                                   types.SimpleNamespace(vipdoc_dir=tmp2)):
                got2 = _scan_mod.read_all_a_stocks("d")
            if len(got2) != 2:
                failures.append(f"[9] 整机数据都旧时误杀: 期望 2 只，实得 {len(got2)}")
                print(f"[FAIL] [9b] 整机数据都旧: {len(got2)} 只")
            else:
                print("[PASS] [9b] 整机数据都旧时不误杀（基准取本批最新，不是今天）")
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        with mock.patch.object(_scan_mod, "app_config", stub_cfg), \
             mock.patch.object(_scan_mod, "STALE_KLINE_DAYS", 0):
            got3 = _scan_mod.read_all_a_stocks("d")
        if len(got3) != 5:
            failures.append(f"[9] STALE_KLINE_DAYS=0 未关闭闸门: 期望 5 只，实得 {len(got3)}")
            print(f"[FAIL] [9c] 闸门关闭: {len(got3)} 只")
        else:
            print("[PASS] [9c] STALE_KLINE_DAYS<=0 关闭闸门（退回纯文件名枚举）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_gate_bulk_warning(failures):
    """[9e] 闸门剔除告警分级（2026-10-27 ddb0883 D5）：大面积/整段缺失升级 warning。

    判据（与实现同口径）：
      - 占比 ≥20% ⇒ warning（正常剔除本机 4/5227≈0.08%，不会误报）；
      - 集中单一市场 ≥90% 且 ≥10 只 **且占比 ≥2%** ⇒ warning（疑似只下载了
        部分市场；2% 下限是为了把「20 天内同市场零星停牌」压回 INFO）；
      - 零星剔除（正常停牌/退市）⇒ 保持 INFO。

    文案也要验：`log.warning` 被 mock 掉时 `%` 格式化不会执行，占位符与实参
    不匹配的 bug 会潜伏到线上 —— 这里把 (fmt, *args) 真渲染一遍再断言措辞
    （2026-10-27 51c2fef N1/N2：占比分支曾误写「集中在首位数字 X（50/100）」）。
    """
    base = datetime(2026, 10, 23, 15, 0, 0)
    stale = base - timedelta(days=30)

    def _render(m):
        """把 mock 到的 (fmt, *args) 真渲染 —— 占位符/实参不匹配在这里就会暴露。"""
        if not m.called:
            return ""
        fmt, args = m.call_args[0][0], m.call_args[0][1:]
        try:
            return fmt % args
        except TypeError as exc:
            failures.append(f"[9e] 日志文案占位符与实参不匹配：{exc}（fmt={fmt!r}）")
            return ""

    def run(stocks, last_dates):
        with mock.patch.object(_scan_mod.log, "warning") as mw, \
             mock.patch.object(_scan_mod.log, "info") as mi:
            _scan_mod._drop_stale_candidates(stocks, last_dates)
        return mw.called, mi.called, _render(mw), _render(mi)

    # A. 大面积（50%）：200 只剔 100，被剔代码首位 0/6 混布（集中度 50%）
    #    ⇒ 只能由占比分支触发（隔离验证占比判据本身）
    stocks = [{"prefix": "1", "code": f"{600100 + i:06d}" if i % 2 else f"{i:06d}"}
              for i in range(200)]
    dates = [stale if i % 4 in (0, 1) else base for i in range(200)]
    warned, infoed, wmsg, _ = run(stocks, dates)
    if not warned or infoed:
        failures.append(f"[9e] 50% 剔除应 warning（实际 warning={warned} info={infoed}）")
        print(f"[FAIL] [9e-A] 50% 剔除: warning={warned} info={infoed}")
    elif "集中在首位数字" in wmsg:
        failures.append(f"[9e] 占比触发的文案不该写「集中在」（市场混布 50/50）：{wmsg}")
        print("[FAIL] [9e-A] 占比触发却写了「集中在首位数字」")
    else:
        print("[PASS] [9e-A] 大面积剔除（50%，市场混布）升级 warning，且文案不称「集中」")

    # B. 零星剔除：1000 只剔 4，混布 ⇒ 保持 INFO
    stocks = [{"prefix": "1", "code": f"{600000 + i:06d}"} for i in range(1000)]
    dates = [base] * 1000
    for i, first in enumerate(("6", "0", "3", "6")):
        stocks[i]["code"] = f"{first}00000{i}"
        dates[i] = stale
    warned, infoed, _, _ = run(stocks, dates)
    if warned or not infoed:
        failures.append(f"[9e] 零星剔除应保持 INFO（实际 warning={warned} info={infoed}）")
        print(f"[FAIL] [9e-B] 零星剔除: warning={warned} info={infoed}")
    else:
        print("[PASS] [9e-B] 零星剔除（4/1000 混布）保持 INFO")

    # C. 集中单一市场但占比低：100 只剔 10、全是 6 开头 ⇒ warning（≥90%、≥10 只、≥2%）
    stocks = [{"prefix": "1", "code": f"{600000 + i:06d}"} for i in range(100)]
    dates = [base] * 100
    for i in range(10):
        dates[i] = stale
    warned, infoed, wmsg, _ = run(stocks, dates)
    if not warned:
        failures.append("[9e] 单市场整段缺失（10 只全 6 开头）应 warning")
        print("[FAIL] [9e-C] 集中剔除未升级 warning")
    elif "集中在首位数字 6（10/10）" not in wmsg:
        failures.append(f"[9e] 集中触发的文案应写明集中度：{wmsg}")
        print("[FAIL] [9e-C] 文案缺「集中在首位数字 6（10/10）」")
    else:
        print("[PASS] [9e-C] 单一市场整段缺失（10/100）升级 warning，文案含集中度")

    # D. 集中但不足 10 只：100 只剔 9、全 6 开头 ⇒ INFO（单只剔除本就 100% 集中）
    dates = [base] * 100
    for i in range(9):
        dates[i] = stale
    warned, infoed, _, _ = run(stocks, dates)
    if warned or not infoed:
        failures.append(f"[9e] 9 只集中剔除应保持 INFO（实际 warning={warned} info={infoed}）")
        print(f"[FAIL] [9e-D] 9 只集中剔除: warning={warned} info={infoed}")
    else:
        print("[PASS] [9e-D] 集中但 <10 只不误报（防单只剔除 100% 集中误触发）")

    # E. 集中但占比 <2%：1000 只剔 11、全 6 开头 ⇒ INFO
    #    20 天窗口内同一市场零星停牌累计到 11 只是常态，不该报「K 线未更新」
    stocks = [{"prefix": "1", "code": f"{600000 + i:06d}"} for i in range(1000)]
    dates = [base] * 1000
    for i in range(11):
        dates[i] = stale
    warned, infoed, _, _ = run(stocks, dates)
    if warned or not infoed:
        failures.append(f"[9e] 集中但占比 1.1% 应保持 INFO（实际 warning={warned} info={infoed}）")
        print(f"[FAIL] [9e-E] 11/1000 集中: warning={warned} info={infoed}")
    else:
        print("[PASS] [9e-E] 集中但占比 <2%（11/1000）保持 INFO（防零星同市场停牌误报）")


def test_threshold_zero_disables(failures):
    """[2] 阈值 0：不取市值、候选全保留。"""
    result, n = _run_stock_list(min_float_mc=0)
    if n != 0:
        failures.append(f"[2] 阈值 0 仍调用了 {n} 次市值取数（期望 0）")
        print(f"[FAIL] [2] 阈值 0 市值取数次数: {n}")
    elif result.get("total") != 1 or result.get("pre_skipped") != 0:
        failures.append(f"[2] 阈值 0 候选应全保留，实际 total={result.get('total')} pre_skipped={result.get('pre_skipped')}")
        print(f"[FAIL] [2] 阈值 0 结果: {result.get('total')}/{result.get('pre_skipped')}")
    else:
        print("[PASS] [2] 阈值 0：市值取数 0 次，候选全保留")


def test_zxg_filtered_by_threshold(failures):
    """[3] 阈值 >0：自选股也按市值过滤（10亿跳过 / 80亿保留）。"""
    result, _ = _run_stock_list(min_float_mc=50, mc_value=10.0)
    if result.get("pre_skipped") != 1 or result.get("total") != 0:
        failures.append(f"[3] 10亿<50亿 应跳过: total={result.get('total')} pre_skipped={result.get('pre_skipped')}")
        print(f"[FAIL] [3] 低于阈值: {result.get('total')}/{result.get('pre_skipped')}")
    else:
        print("[PASS] [3a] 低于阈值: 自选股 10亿 < 50亿 被跳过")
    result, _ = _run_stock_list(min_float_mc=50, mc_value=80.0)
    if result.get("total") != 1 or result.get("pre_skipped") != 0:
        failures.append(f"[3] 80亿>=50亿 应保留: total={result.get('total')} pre_skipped={result.get('pre_skipped')}")
        print(f"[FAIL] [3] 高于阈值: {result.get('total')}/{result.get('pre_skipped')}")
    else:
        print("[PASS] [3b] 高于阈值: 自选股 80亿 >= 50亿 保留")


def test_st_filter_applies_to_zxg(failures):
    """[5] ST/退市过滤扩展到自选股（名称缓存命中 ST 名 → 跳过）。"""
    cache = _scan_mod._stock_names_cache
    cache["sh600519"] = {"name": "ST测试"}
    try:
        result, _ = _run_stock_list(min_float_mc=50, mc_value=80.0)
        total = result.get("total")
        skipped = result.get("pre_skipped")
        if total != 0 or skipped != 1:
            failures.append(f"[5] ST 票应被跳过: total={total} pre_skipped={skipped}")
            print("[FAIL] [5] ST 过滤未作用于自选股")
        else:
            print("[PASS] [5] ST/退市过滤已扩展到自选股（非成分股来源同样生效）")
    finally:
        cache.pop("sh600519", None)


def test_st_predicate_wiring(failures):
    """[6] ST 判定单点化：谓词正确 + 刷新不再删 ST + 搜索端接线。"""
    from App.AppUtils import is_st_like_name
    for n in ("*ST西旅", "ST沈化", "顺利退"):
        if not is_st_like_name(n):
            failures.append(f"[6] 谓词漏判 {n!r}")
            print(f"[FAIL] [6] 谓词漏判: {n!r}")
            return
    for n in ("平安银行", "贵州茅台", ""):
        if is_st_like_name(n):
            failures.append(f"[6] 谓词误判 {n!r}")
            print(f"[FAIL] [6] 谓词误判: {n!r}")
            return
    rf = open(os.path.join(REPO_ROOT, "App", "AppRefresh.py"), encoding="utf-8").read()
    if "filtered_st" in rf:
        failures.append("[6] 名称刷新仍在删除 ST 股（filtered_st 残留）")
        print("[FAIL] [6] AppRefresh 仍删 ST")
        return
    ch = open(os.path.join(REPO_ROOT, "App", "AppChart.py"), encoding="utf-8").read()
    if "is_st_like_name" not in ch:
        failures.append("[6] search_stocks 未接 ST 过滤")
        print("[FAIL] [6] 搜索端未接线")
        return
    print("[PASS] [6] ST 判定单点化：谓词 6 例 + 刷新保留 + 搜索端过滤在位")


def test_threshold_ssot(failures):
    """[7] 阈值单一事实源在后端：前端不写死默认值，未配置时不带参。"""
    js = open(os.path.join(REPO_ROOT, "Frontend", "app.js"), encoding="utf-8").read()
    if "_scanMinFloatMc = 50" in js or "_scanMinFloatMc=50" in js:
        failures.append("[7] 前端仍写死阈值默认值 50（后端 SCAN_MIN_FLOAT_MC 被架空）")
        print("[FAIL] [7] 前端写死阈值 50")
        return
    import re
    # 行首即拼参（前面没有 if 守卫）= 无条件带参，未配置时仍覆盖后端配置
    if re.search(r'(?m)^\s*url \+= "&scan_min_float_mc=" \+ _scanMinFloatMc;', js):
        failures.append("[7] 阈值参数无条件拼参：未配置时仍以 0/旧值覆盖后端配置")
        print("[FAIL] [7] 阈值无条件拼参")
        return
    if not re.search(r'if \(_scanMinFloatMc !== null\) url \+= "&scan_min_float_mc=" \+ _scanMinFloatMc;', js):
        failures.append("[7] 缺少「非 null 才带参」守卫（后端默认无法生效）")
        print("[FAIL] [7] 缺少 null 守卫")
        return
    print("[PASS] [7] 阈值 SSOT：前端初值 null + 非 null 才带参")


def test_threshold_published_by_backend(failures):
    """[8] 后端经 /api/health 下发 config.scan_min_float_mc，前端接该默认。"""
    api = open(os.path.join(REPO_ROOT, "FrontAPI.py"), encoding="utf-8").read()
    if '"config": app_config.as_dict(redact=True)' not in api:
        failures.append("[8] /api/health 未下发 config 摘要")
        print("[FAIL] [8] /api/health 未下发 config")
        return
    from App.AppConfig import app_config
    if "scan_min_float_mc" not in app_config.as_dict(redact=True):
        failures.append("[8] as_dict 未包含 scan_min_float_mc")
        print("[FAIL] [8] as_dict 缺 scan_min_float_mc")
        return
    js = open(os.path.join(REPO_ROOT, "Frontend", "app.js"), encoding="utf-8").read()
    if "data.config.scan_min_float_mc" not in js:
        failures.append("[8] 前端未消费后端下发的阈值默认")
        print("[FAIL] [8] 前端未接后端默认")
        return
    print("[PASS] [8] 阈值默认经 /api/health 下发且前端已消费")


def test_all_a_registered(failures):
    """[4] _SOURCE_READERS 登记 all_a →「全A股」。"""
    code = open(os.path.join(REPO_ROOT, "App", "AppScan.py"), encoding="utf-8").read()
    if '"all_a": (lambda: read_all_a_stocks(_freq), "全A股")' not in code:
        failures.append("[4] _SOURCE_READERS 未登记 all_a 来源（或未随周期传参）")
        print("[FAIL] [4] all_a 未登记")
    else:
        print("[PASS] [4] _SOURCE_READERS 已登记 all_a → 全A股")


def main():
    failures = []
    test_all_a_prefix_filter(failures)
    test_minute_period_uses_fzline(failures)
    test_stale_gate(failures)
    test_stale_gate_bulk_warning(failures)
    test_minute_gate_overlays_day(failures)
    test_threshold_zero_disables(failures)
    test_zxg_filtered_by_threshold(failures)
    test_st_filter_applies_to_zxg(failures)
    test_st_predicate_wiring(failures)
    test_threshold_ssot(failures)
    test_threshold_published_by_backend(failures)
    test_all_a_registered(failures)
    print("=" * 60)
    if failures:
        print(f"结果: {len(failures)} 处 FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("结果: 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
增量快照 —— 行为回归守护用例
=====================================================================
背景：将 SSE 每根 K 线完成时的全量 O(n) 快照重建改为增量
（复用缓存 klines 仅追加新确认K线 + EMA 状态续算 MACD）。本守护用例
锁定增量路径与全量路径输出一致，防止「增量优化引入 MACD/klines 漂移」：

  ① 增量 klines：新确认K线追加、预览bar剥离、合并场景尾部 OHLC 修正
  ② 增量 MACD ≡ 全量 MACD：对同一序列，逐根增量续算与全量重算
     逐位一致（EMA 状态续算正确性）
  ③ 快照同构：_extract_realtime_snapshot 增量路径（prev_klines/
     prev_ema_state）与全量路径 klines/meta.kline_count 一致
  ④ 状态缺失回退：prev_ema_state=None 时增量路径回退全量重算，仍正确
  ⑤ tick 路径指标（2026-10-10 新加，该分支此前**零覆盖**）：
     a. MacdStream 增量 ≡ 全量 MACD（逐位 ==）
     b. tick 路径末根 ≡ 快照全量末根（dea 取整口径已统一）
     c. 上/下窗状态按 key 隔离，互不覆盖
     d. 结构契约（棘轮，**全 AST 判据 + 变异自证**）：门槛单源、tick 无 O(n) 全量重算、
        调用点恰好 2 处。判据本身带「判别力自证」（注入选定坏写法必须报错）与
        「不误报自证」（变量改名 / 换行书写 / 全文件重排不许报错）
     e. MACD_MIN_BARS 门槛语义：整窗不足 26 根 ⇒ 全量与 tick 都置 0；
        窗内前 25 根全量照样有值（曾把两者门槛混为一谈，见 ⑤e）
     f. tick 后**整窗** ≡ `_apply_macd_full` 整窗（含 25/26 边界，逐位 ==）：
        曾经只断言末根，于是「快照把前 n-1 根置 0 而末根已有值」的整窗不一致
        被漏掉（窗口恰好 26 根时表现为一条孤立点）

运行：python Test/test_sse_incremental.py            # 校验（run_all 组件）
      python Test/test_sse_incremental.py --update   # 兼容 run_all --update
"""
import argparse
import os
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

from App.AppSSE import (
    _incremental_klines,
    _apply_macd_full,
    _apply_macd_incremental,
    _apply_rsi_full,
    _tick_update_metrics,
    _extract_realtime_snapshot,
)
from App.AppUtils import MacdStream, MACD_MIN_BARS, _inherit_metrics_for_preview_bar


# ── 轻量 Mock：仅承载 _incremental_klines 需要的 kl_list 结构 ────────
class MockTime:
    def __init__(self, ts):
        self.ts = ts

    def toFmtStr(self, fmt):
        from datetime import datetime
        return datetime.fromtimestamp(self.ts).strftime(fmt)


class MockMetric:
    def __init__(self, vol=0, turnover=0.0):
        self.metric = {"volume": vol, "turnover": turnover}


class MockKlu:
    def __init__(self, ts, o=100.0, h=101.0, l=99.0, c=100.5, vol=10):
        self.time = MockTime(ts)
        self.open = o; self.high = h; self.low = l; self.close = c
        self.trade_info = MockMetric(vol=vol)


class MockKlc:
    def __init__(self, klus):
        self.lst = klus
        self.fx = None


class MockKlList:
    def __init__(self, klus):
        self.lst = [MockKlc(klus)]
        self.bi_list = []
        self.zs_list = []
        self.seg_list = []


def _klu(ts, c=100.5, vol=10):
    return MockKlu(ts, c=c, vol=vol)


def _preview_bar(dt_str):
    return {"date": dt_str, "timestamp": 0, "open": 0, "high": 0,
            "low": 0, "close": 0, "vol": 0, "amount": 0, "dif": 0, "dea": 0, "macd": 0}


def _fmt_ts(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M:%S")


def test_incremental_klines(failures):
    """① 增量 klines：新确认K线追加 + 预览bar剥离 + 合并修正"""
    base = 1_700_000_000
    fmt = "%Y/%m/%d %H:%M:%S"
    # 缓存快照：2 根确认K线 + 1 根预览bar
    prev = [
        {"date": _fmt_ts(base), "open": 1, "high": 2, "low": 0, "close": 1.5, "vol": 10, "amount": 0,
         "dif": 0, "dea": 0, "macd": 0},
        {"date": _fmt_ts(base + 15), "open": 2, "high": 3, "low": 1, "close": 2.5, "vol": 10, "amount": 0,
         "dif": 0, "dea": 0, "macd": 0},
        _preview_bar(_fmt_ts(base + 30)),  # 预览bar（下一根形成K线）
    ]
    # chan 推进：新增第 3 根确认K线
    kl_list = MockKlList([_klu(base), _klu(base + 15), _klu(base + 30, c=3.5)])
    out, changed_idx = _incremental_klines(prev, kl_list, fmt)
    if len(out) != 3:
        failures.append(f"① 增量后数量 {len(out)} != 3")
        print(f"[FAIL] ① 增量 klines: 数量 {len(out)}")
        return
    if out[-1]["date"] != _fmt_ts(base + 30) or out[-1]["close"] != 3.5:
        failures.append(f"① 新确认K线未正确追加: {out[-1]}")
        print(f"[FAIL] ① 增量 klines: 尾部 {out[-1]}")
        return
    if changed_idx != 2:
        failures.append(f"① changed_idx {changed_idx} != 2")
        print(f"[FAIL] ① 增量 klines: changed_idx {changed_idx}")
        return
    print("[PASS] ① 增量 klines: 新确认K线追加 + 预览bar剥离")

    # 合并场景：chan 尾部K线 OHLC 修正（日期不变）
    kl_list2 = MockKlList([_klu(base), _klu(base + 15, c=9.9)])
    out2, changed_idx2 = _incremental_klines(prev, kl_list2, fmt)
    if len(out2) != 2:
        failures.append(f"① 合并场景数量 {len(out2)} != 2")
        print(f"[FAIL] ① 合并场景: 数量 {len(out2)}")
        return
    if out2[-1]["close"] != 9.9:
        failures.append(f"① 合并场景尾部 OHLC 未修正: {out2[-1]}")
        print(f"[FAIL] ① 合并场景: 尾部 {out2[-1]}")
        return
    if changed_idx2 != 1:
        failures.append(f"① 合并场景 changed_idx {changed_idx2} != 1")
        print(f"[FAIL] ① 合并场景: changed_idx {changed_idx2}")
        return
    print("[PASS] ① 增量 klines: 合并场景尾部 OHLC 修正")


def test_macd_incremental_equals_full(failures):
    """② 增量 MACD ≡ 全量 MACD：逐根续算与全量重算逐位一致"""
    import random
    random.seed(42)
    closes = [100.0 + i * 0.5 + random.uniform(-1, 1) for i in range(120)]
    klines = [{"close": c, "dif": 0, "dea": 0, "macd": 0} for c in closes]

    # 全量：一次性重算
    full = [dict(k) for k in klines]
    _apply_macd_full(full)

    # 增量：先算前 60 根，再逐根续算到 120
    inc = [dict(k) for k in klines[:60]]
    state = _apply_macd_full(inc)
    for i in range(60, len(klines)):
        inc.append(dict(klines[i]))
        state = _apply_macd_incremental(inc, len(inc) - 1, state)

    for i in range(len(full)):
        for key in ("dif", "dea", "macd"):
            if abs(full[i][key] - inc[i][key]) > 1e-9:
                failures.append(f"② 第{i}根 {key}: 全量={full[i][key]} 增量={inc[i][key]}")
                print(f"[FAIL] ② MACD 漂移: 第{i}根 {key} 全量={full[i][key]} 增量={inc[i][key]}")
                return
    print("[PASS] ② 增量 MACD ≡ 全量 MACD（120 根逐位一致）")


def test_macd_state_missing_fallback(failures):
    """④ 状态缺失回退：prev_ema_state=None 时增量路径回退全量重算"""
    closes = [100.0 + i for i in range(40)]
    klines = [{"close": c, "dif": 0, "dea": 0, "macd": 0} for c in closes]
    full = [dict(k) for k in klines]
    _apply_macd_full(full)

    inc = [dict(k) for k in klines]
    state = _apply_macd_incremental(inc, 0, None)  # 状态缺失 → 回退全量
    for i in range(len(full)):
        for key in ("dif", "dea", "macd"):
            if abs(full[i][key] - inc[i][key]) > 1e-9:
                failures.append(f"④ 第{i}根 {key} 回退不一致")
                print(f"[FAIL] ④ 状态缺失回退: 第{i}根 {key}")
                return
    if state is None:
        failures.append("④ 回退后状态仍为 None")
        print("[FAIL] ④ 状态缺失回退: 返回状态 None")
        return
    print("[PASS] ④ 状态缺失回退: 回退全量重算且返回 EMA 状态")


def test_snapshot_incremental_consistency(failures):
    """③ 快照同构：增量路径与全量路径 klines/meta.kline_count 一致"""
    from datetime import datetime
    base = datetime(2025, 6, 2, 9, 30, 0).timestamp()
    fmt = "%Y/%m/%d %H:%M:%S"

    # 构造 40 根K线的 chan
    klus = []
    for i in range(40):
        ts = base + 15 * i
        klus.append(_klu(ts, c=100.0 + i * 0.3, vol=10))
    kl_list = MockKlList(klus)

    class MockChan(dict):
        def __init__(self):
            super().__init__()
            self._kl = kl_list
        def __getitem__(self, key):
            return self._kl

    chan = MockChan()
    kl_type = ("kl",)

    # 全量路径
    full = _extract_realtime_snapshot(chan, kl_type, "SYM", "名称", "15s")
    # 增量路径：先取前 39 根，再增量到 40
    prev_klus = klus[:39]
    prev_kl_list = MockKlList(prev_klus)

    class MockChanPrev(dict):
        def __init__(self):
            super().__init__()
            self._kl = prev_kl_list
        def __getitem__(self, key):
            return self._kl

    chan_prev = MockChanPrev()
    prev_snap = _extract_realtime_snapshot(chan_prev, kl_type, "SYM", "名称", "15s")
    # 模拟调用方追加预览bar
    _ex = prev_snap["klines"]
    _ex.append(_preview_bar(_fmt_ts(base + 15 * 39)))

    inc = _extract_realtime_snapshot(
        chan, kl_type, "SYM", "名称", "15s",
        prev_klines=prev_snap["klines"],
        prev_ema_state=prev_snap["meta"].get("_ema_state"))

    if len(full["klines"]) != len(inc["klines"]):
        failures.append(f"③ klines 数量: 全量={len(full['klines'])} 增量={len(inc['klines'])}")
        print(f"[FAIL] ③ 快照同构: klines 数量 {len(full['klines'])} != {len(inc['klines'])}")
        return
    for i in range(len(full["klines"])):
        for key in ("date", "open", "high", "low", "close", "vol", "amount", "dif", "dea", "macd"):
            if full["klines"][i].get(key) != inc["klines"][i].get(key):
                failures.append(f"③ 第{i}根 {key}: 全量={full['klines'][i].get(key)} 增量={inc['klines'][i].get(key)}")
                print(f"[FAIL] ③ 快照同构: 第{i}根 {key} 不一致")
                return
    if full["meta"]["kline_count"] != inc["meta"]["kline_count"]:
        failures.append(f"③ kline_count: {full['meta']['kline_count']} != {inc['meta']['kline_count']}")
        print("[FAIL] ③ 快照同构: kline_count 不一致")
        return
    print("[PASS] ③ 快照同构: 增量路径与全量路径 klines/MACD/kline_count 逐位一致")


def _src(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _last_bar(kl):
    return (kl["dif"], kl["dea"], kl["macd"], kl["rsi"])


def test_macd_stream_equals_full(failures):
    """⑤a MacdStream 增量 ≡ 全量 MACD：逐位 ==（不是"误差范围内"）"""
    import random
    random.seed(7)
    closes = [100.0 + i * 0.5 + random.uniform(-2, 2) for i in range(200)]
    klines = [{"close": c, "dif": 0, "dea": 0, "macd": 0} for c in closes]
    _apply_macd_full(klines)

    s = MacdStream()
    for i, c in enumerate(closes):
        dif, dea, macd = s.feed(c)
        got = (round(dif, 4), round(dea, 4), round(macd, 4))
        want = (klines[i]["dif"], klines[i]["dea"], klines[i]["macd"])
        if got != want:
            failures.append(f"⑤a 第{i}根 增量={got} 全量={want}")
            print(f"[FAIL] ⑤a MacdStream 增量≠全量: 第{i}根 {got} != {want}")
            return
    print("[PASS] ⑤a MacdStream 增量 ≡ 全量 MACD（200 根逐位一致）")


def test_tick_metrics_equals_snapshot(failures):
    """⑤b tick 路径末根 ≡ 快照全量末根（dea 取整口径已统一：用未取整 dif 递推）"""
    import random
    random.seed(11)
    for n in (30, 120, 300):
        closes = [round(3000 + random.uniform(-50, 50), 2) for _ in range(n)]
        # 快照：n 根确认K线 + 一根预览bar（预览bar 先继承前一根的指标值）
        ex = [{"close": c, "dif": 0, "dea": 0, "macd": 0, "rsi": 0} for c in closes]
        _apply_macd_full(ex)
        _apply_rsi_full(ex)
        ex.append(dict(ex[-1]))
        ex[-1]["close"] = round(3000 + random.uniform(-50, 50), 2)  # tick 灌入真实 close

        ref = [dict(k) for k in ex]
        _apply_macd_full(ref)
        _apply_rsi_full(ref)

        ms, rs = {}, {}
        _tick_update_metrics(ex, ms, rs, "main")
        if _last_bar(ex[-1]) != _last_bar(ref[-1]):
            failures.append(f"⑤b n={n} 末根 tick={_last_bar(ex[-1])} 快照={_last_bar(ref[-1])}")
            print(f"[FAIL] ⑤b tick≠快照: n={n} tick={_last_bar(ex[-1])} 快照={_last_bar(ref[-1])}")
            return
        # tick 不该碰前 n-1 根
        for i in range(len(ex) - 1):
            if _last_bar(ex[i]) != _last_bar(ref[i]):
                failures.append(f"⑤b n={n} 第{i}根被 tick 改动")
                print(f"[FAIL] ⑤b tick 改动了非末根: n={n} 第{i}根")
                return
    print("[PASS] ⑤b tick 末根 ≡ 快照全量末根（30/120/300 根逐位一致，前 n-1 根不动）")


def test_tick_state_isolated_by_key(failures):
    """⑤c 上/下窗状态按 key 隔离：互不覆盖

    **必须含"根数相同"的一对**：根数不同的两个窗口即使共用状态也会被
    「n 不等于 len(ex)-1 ⇒ 重建」兜住（只是变慢、不会算错）；只有**根数相同**
    时状态才会被误复用 ⇒ 判别力全在"根数相同"这一对上。
    """
    import random
    random.seed(13)
    # A/B 根数相同、数据不同（危险组）；C 根数不同（顺带覆盖重建分支）
    a = [{"close": 3000.0 + i + random.uniform(-1, 1), "dif": 0, "dea": 0, "macd": 0, "rsi": 0}
         for i in range(120)]
    b = [{"close": 5000.0 - i * 0.8 + random.uniform(-1, 1), "dif": 0, "dea": 0, "macd": 0, "rsi": 0}
         for i in range(120)]
    c = [{"close": 4000.0 + i * 0.7 + random.uniform(-1, 1), "dif": 0, "dea": 0, "macd": 0, "rsi": 0}
         for i in range(80)]
    ms, rs = {}, {}
    for _ in range(3):
        _tick_update_metrics(a, ms, rs, "main")
        _tick_update_metrics(b, ms, rs, "sub")
        _tick_update_metrics(c, ms, rs, "sub5s")
    for tag, ex in (("A/main", a), ("B/sub", b), ("C/sub5s", c)):
        ref = [dict(k) for k in ex]
        _apply_macd_full(ref)
        _apply_rsi_full(ref)
        if _last_bar(ex[-1]) != _last_bar(ref[-1]):
            failures.append(f"⑤c 窗口{tag} tick={_last_bar(ex[-1])} 快照={_last_bar(ref[-1])}")
            print(f"[FAIL] ⑤c 状态未按 key 隔离: 窗口{tag} {_last_bar(ex[-1])} != {_last_bar(ref[-1])}")
            return
    print("[PASS] ⑤c 上/下窗状态按 key 隔离（含根数相同的 A/B 对，交替 3 轮仍 ≡ 快照）")


# ── ⑤d 结构契约判据：抽成**纯函数**（源码文本 → 违规清单），便于做变异自证 ──
def _ast_contract_failures(sse_src):
    """结构契约判据（源代码文本 → 违规清单，空清单 = 通过）。

    判据全部走 AST ⇒ 与空格、换行、变量名无关。上一版用字符串判据有两个毛病：
      · `sse.count(">= 26")` 挡不住 `> 25` / `== 26` / `26 <= n`（换个写法就绕过）；
      · `sse.count("_tick_update_metrics(ex, macd_tail_state")` 会因**变量改名**或
        调用改多行书写而误报（护栏拦住无关重构）。
    抽成纯函数的目的是能在测试里把它喂给**故意改坏的源码**，证明判据真有判别力
    （否则「判据恒真」与「代码正确」无法区分）。
    """
    import ast
    bad = []
    tree = ast.parse(sse_src)

    def _find_fn(name):
        return next((x for x in ast.walk(tree)
                     if isinstance(x, ast.FunctionDef) and x.name == name), None)

    # [1] 门槛单源：全文件「引用 MACD_MIN_BARS 的比较」恰好 2 处（全量 + tick）
    gate_cmps = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Compare) and "MACD_MIN_BARS" in ast.unparse(n)]
    if len(gate_cmps) != 2:
        bad.append(f"引用 MACD_MIN_BARS 的比较 {len(gate_cmps)} 处（期望 2：全量 + tick）")

    # [2] MACD 三个函数体内**不得**出现「与整数常量比较」—— 这三个函数里唯一的阈值
    #     就是整窗门槛，写成字面量即失去单源。`>= 26` / `> 25` / `== 26` / `26 <= n`
    #     一并覆盖；空白与换行不影响 AST。
    for fn_name in ("_apply_macd_full", "_apply_macd_incremental", "_tick_update_metrics"):
        fn = _find_fn(fn_name)
        if fn is None:
            bad.append(f"未找到 {fn_name} 定义")
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Compare):
                continue
            for side in [node.left] + list(node.comparators):
                if (isinstance(side, ast.Constant) and isinstance(side.value, int)
                        and not isinstance(side.value, bool)):
                    bad.append(f"{fn_name}:{node.lineno} 与整数常量 {side.value} 比较"
                               f"（门槛必须走 MACD_MIN_BARS）")

    # [3] tick 路径不得再有 O(n) 全量重算：函数体内不得调用 ema(。
    #     上一版为此手工"跳过 docstring 再取文本段"（怕把叙述当代码）；AST 下天然不需要。
    fn = _find_fn("_tick_update_metrics")
    if fn is None:
        bad.append("未找到 _tick_update_metrics 定义")
    else:
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "ema"):
                bad.append(f"_tick_update_metrics:{node.lineno} 仍有 ema( 全量调用")
    # 全量 EMA 只剩 _apply_macd_full 里的两处
    c = sum(1 for x in ast.walk(tree)
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == "ema"
            and x.args and isinstance(x.args[0], ast.Name) and x.args[0].id == "closes")
    if c != 2:
        bad.append(f"ema(closes…) 调用点 {c} 处（期望 2，只应在 _apply_macd_full）")

    # [4] tick 调用点恰好 2 处（单窗 "main" + 双窗 freq_label）——
    #     按 `Call.func.id` 数（不按字符串匹配）+ 校验第 4 个实参 ⇒ 改名/换行均不受影响
    calls = [x for x in ast.walk(tree)
             if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
             and x.func.id == "_tick_update_metrics"]
    if len(calls) != 2:
        bad.append(f"_tick_update_metrics 调用点 {len(calls)} 处（期望 2）")
    else:
        keys = sorted(ast.unparse(x.args[3]) if len(x.args) > 3 else "<缺第4参>"
                      for x in calls)
        if keys != ["'main'", "freq_label"]:
            bad.append(f"tick 调用点窗口 key = {keys}（期望 单窗 'main' + 双窗 freq_label）")
    return bad


def _utils_gate_failures(utils_src):
    """`App/AppUtils.py` 侧 MACD 门槛的结构契约（源代码文本 → 违规清单）。

    与 `_ast_contract_failures` 同形，盯的是**另一处**引用：`calculate_macd`
    是展示侧唯一的算值出口（HTTP 股票路径走它）。此前它**没有**整窗门槛 ⇒
    与 SSE 侧口径分裂（2026-10-11 补齐）。这里钉住「它必须引用 `MACD_MIN_BARS`
    且不得把门槛写成字面量」—— 否则改 SSE 侧时又会漏掉这一处。
    """
    import ast
    bad = []
    tree = ast.parse(utils_src)
    fn = next((x for x in ast.walk(tree)
               if isinstance(x, ast.FunctionDef) and x.name == "calculate_macd"), None)
    if fn is None:
        return ["未找到 calculate_macd 定义"]
    gate = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
            and "MACD_MIN_BARS" in ast.unparse(n)]
    if len(gate) != 1:
        bad.append(f"calculate_macd 引用 MACD_MIN_BARS 的比较 {len(gate)} 处（期望 1）")
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        for side in [node.left] + list(node.comparators):
            if (isinstance(side, ast.Constant) and isinstance(side.value, int)
                    and not isinstance(side.value, bool)):
                bad.append(f"calculate_macd:{node.lineno} 与整数常量 {side.value} 比较"
                           f"（门槛必须走 MACD_MIN_BARS）")
    return bad


def test_tick_no_full_recompute(failures):
    """⑤d 结构契约（棘轮）：门槛单源、tick 无 O(n) 全量重算、调用点恰好 2 处"""
    import ast
    sse = _src("App", "AppSSE.py")
    utils = _src("App", "AppUtils.py")

    bad = _ast_contract_failures(sse)
    if bad:
        failures.extend("⑤d " + x for x in bad)
        for x in bad:
            print(f"[FAIL] ⑤d 结构契约: {x}")
        return

    # ── AppUtils 侧：`calculate_macd` 的整窗门槛必须与 SSE 侧同源 ──────
    bad_u = _utils_gate_failures(utils)
    if bad_u:
        failures.extend("⑤d " + x for x in bad_u)
        for x in bad_u:
            print(f"[FAIL] ⑤d 门槛同源(AppUtils): {x}")
        return

    # 常量值本身（外围）仍按文本校验：单源于 AppUtils，且必须是 26
    import re
    m = re.search(r"^MACD_MIN_BARS = (\d+)", utils, re.M)
    if not m or m.group(1) != "26":
        failures.append("⑤d AppUtils.MACD_MIN_BARS 不是 26")
        print("[FAIL] ⑤d 门槛同源: MACD_MIN_BARS 缺失或被改")
        return

    tree = ast.parse(sse)
    lines = sse.split("\n")
    tick_fn = next(x for x in ast.walk(tree) if isinstance(x, ast.FunctionDef)
                   and x.name == "_tick_update_metrics")
    gates = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)
             and "MACD_MIN_BARS" in ast.unparse(n)]
    seg_t = ast.get_source_segment(sse, next(n for n in gates if n.lineno > tick_fn.lineno))
    seg_f = ast.get_source_segment(sse, next(n for n in gates if n.lineno < tick_fn.lineno))
    tick_call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "_tick_update_metrics"
                     and len(n.args) > 3 and ast.unparse(n.args[3]) == "'main'")
    seg_call = ast.get_source_segment(sse, tick_call)
    # ema 注入点：函数体第一条**可执行**语句（跳过 docstring），缩进按实际行推导
    first_stmt = tick_fn.body[1] if (tick_fn.body and isinstance(tick_fn.body[0], ast.Expr)
                                     and isinstance(tick_fn.body[0].value, ast.Constant)) else tick_fn.body[0]
    _l = lines[first_stmt.lineno - 1]
    indent = " " * (len(_l) - len(_l.lstrip()))

    # ── 判别力自证：这些坏写法**必须**被判据拦住 ─────────────────────
    # ① 文本替换型：门槛改成字面量 / 界外值 / 相等式
    text_mutations = [
        ("tick 门槛写字面值", seg_t, seg_t.replace("MACD_MIN_BARS", "26")),
        ("tick 门槛写界外值", seg_t, seg_t.replace(">=", ">").replace("MACD_MIN_BARS", "25")),
        ("tick 门槛改相等式", seg_t, seg_t.replace(">=", "==").replace("MACD_MIN_BARS", "26")),
        ("全量门槛写字面值", seg_f, seg_f.replace("MACD_MIN_BARS", "26")),
    ]
    for label, old, new in text_mutations:
        mutated = sse.replace(old, new, 1)
        if mutated == sse:
            failures.append(f"⑤d 变异自证: 注入「{label}」未产生变化（锚点 {old!r}）")
            print(f"[FAIL] ⑤d 变异自证: 「{label}」未产生变化")
            return
        if not _ast_contract_failures(mutated):
            failures.append(f"⑤d 变异自证: 注入「{label}」后判据未报错（判据无判别力）")
            print(f"[FAIL] ⑤d 变异自证: 「{label}」未被拦住")
            return

    # ② 行插入型：把 ema( 全量调用塞回 _tick_update_metrics 函数体（缩进按实际行推导）
    mutated = "\n".join(lines[:first_stmt.lineno - 1]
                        + [indent + "ema(closes, 12)"]
                        + lines[first_stmt.lineno - 1:])
    if not _ast_contract_failures(mutated):
        failures.append("⑤d 变异自证: 塞回 ema( 全量调用后判据未报错")
        print("[FAIL] ⑤d 变异自证: 塞回 ema( 全量调用未被拦住")
        return

    # ③ AppUtils 侧：门槛写字面值 / 删掉门槛 ⇒ 必须被 `_utils_gate_failures` 拦住
    u_gate = "len(closes) < MACD_MIN_BARS"
    if u_gate not in utils:
        failures.append("⑤d 变异自证: AppUtils 门槛锚点未找到")
        print("[FAIL] ⑤d 变异自证: AppUtils 门槛锚点未找到")
        return
    for label, new_u in (("AppUtils 门槛写字面值", u_gate.replace("MACD_MIN_BARS", "26")),
                         ("AppUtils 删掉整窗门槛", "False")):
        if not _utils_gate_failures(utils.replace(u_gate, new_u, 1)):
            failures.append(f"⑤d 变异自证: 注入「{label}」后判据未报错（判据无判别力）")
            print(f"[FAIL] ⑤d 变异自证: 「{label}」未被拦住")
            return

    # ④ 删调用点：必须被 [4] 拦住
    if not _ast_contract_failures(sse.replace(seg_call, "pass", 1)):
        failures.append("⑤d 变异自证: 删掉单窗 tick 调用点后判据未报错")
        print("[FAIL] ⑤d 变异自证: 删掉调用点未被拦住")
        return

    # ── 不误报自证：等价改写**必须**通过（否则护栏会拦无关重构）─────
    neutrals = [
        ("变量改名", sse.replace("macd_tail_state", "macd_state_renamed")),
        ("调用改多行", sse.replace(seg_call, seg_call.replace(", ", ",\n" + " " * 32))),
        ("全文件 AST 重排", ast.unparse(tree)),
    ]
    for label, mutated in neutrals:
        if mutated == sse:
            failures.append(f"⑤d 不误报自证: 「{label}」未产生变化")
            print(f"[FAIL] ⑤d 不误报自证: 「{label}」未产生变化")
            return
        got = _ast_contract_failures(mutated)
        if got:
            failures.append(f"⑤d 不误报自证: 「{label}」被误报 {got}")
            print(f"[FAIL] ⑤d 不误报自证: 「{label}」被误报: {got}")
            return

    print("[PASS] ⑤d 结构契约: 门槛单源（SSE 全量 / tick + AppUtils.calculate_macd）"
          " + tick 无 ema( 全量 + 调用点 2 处；"
          "8 种坏写法全被拦、3 种等价改写不误报")


def test_macd_min_bars_semantics(failures):
    """⑤e 门槛语义：整窗不足 26 根 ⇒ 全量与 tick 都置 0；窗内前 25 根全量照样有值"""
    closes = [3000.0 + i for i in range(20)]
    # [1] 整窗不足门槛：_apply_macd_full 整窗置 0
    short = [{"close": c, "dif": 9, "dea": 9, "macd": 9, "rsi": 0} for c in closes]
    _apply_macd_full(short)
    if any((k["dif"], k["dea"], k["macd"]) != (0, 0, 0) for k in short):
        failures.append("⑤e 整窗<26 时全量未置 0")
        print("[FAIL] ⑤e 门槛语义: 整窗<26 全量未置 0")
        return
    # [2] tick 同口径：末根也置 0
    _tick_update_metrics(short, {}, {}, "main")
    if _last_bar(short[-1])[:3] != (0, 0, 0):
        failures.append(f"⑤e 整窗<26 时 tick 未置 0: {_last_bar(short[-1])}")
        print(f"[FAIL] ⑤e 门槛语义: 整窗<26 tick 未置 0 {_last_bar(short[-1])}")
        return
    # [3] 窗内前 25 根**不是** 0（门槛是整窗判据，不是"前 25 根无效"）
    long_closes = [3000.0 + i for i in range(40)]
    longk = [{"close": c, "dif": 0, "dea": 0, "macd": 0, "rsi": 0} for c in long_closes]
    _apply_macd_full(longk)
    if all(k["dif"] == 0 for k in longk[:25]):
        failures.append("⑤e 窗内前 25 根被误置 0（门槛应是整窗判据）")
        print("[FAIL] ⑤e 门槛语义: 窗内前 25 根被误置 0")
        return
    s = MacdStream()
    for i, c in enumerate(long_closes[:25]):
        if s.feed(c) is None:
            failures.append(f"⑤e MacdStream 第{i}根返回 None（应恒返回三元组）")
            print(f"[FAIL] ⑤e 门槛语义: MacdStream 第{i}根返回 None")
            return
    print("[PASS] ⑤e 门槛语义: 整窗<26 置 0 / 窗内前 25 根仍有值（两处门槛不混）")


def test_tick_window_equals_full_window(failures):
    """⑤f tick 后**整窗** ≡ `_apply_macd_full` 整窗（含 25/26 门槛边界，逐位 ==）

    上一版只断言「末根 ≡ 全量末根」，于是「快照在整窗不足门槛时把前 n-1 根置 0、
    而此刻窗口已越过门槛 ⇒ 末根有值」这种**整窗不一致**被漏掉：窗口恰好 26 根时
    前端表现为「25 个 0 + 一条孤立点」。旧 tick 因循环重写全窗而不显，切增量后才暴露。

    可达性（这是本轮修复的前提，2026-10-10 复核更正）：窗口长度由「选点左边界 T」或
    数据自身根数决定 —— `AppSSE.py` 的 start_time 可来自前端选点 / CSV 恢复
    （见 `_sse_single_gen` / `_sse_dual_gen`），而 `_futures_window_fetch_bars` 的
    B 模式是「[T, 最新] 全量」、不设下界 ⇒ 短窗（含恰好 25 根）是**可达**的。
    """
    import random
    random.seed(23)
    for n_confirm in (10, 24, 25, 26, 27, 40, 120, 300):
        closes = [round(3000 + random.uniform(-50, 50), 2) for _ in range(n_confirm)]
        # 真实链路：快照只喂「已确认K线」→ 追加 1 根预览bar（继承前一根）→ 第一次 tick
        ex = [{"close": c, "dif": 0, "dea": 0, "macd": 0, "rsi": 0} for c in closes]
        _apply_macd_full(ex)
        _apply_rsi_full(ex)
        ex.append(dict(ex[-1]))
        _inherit_metrics_for_preview_bar(ex)
        ex[-1]["close"] = round(3000 + random.uniform(-50, 50), 2)
        before = [(k["dif"], k["dea"], k["macd"]) for k in ex[:-1]]

        ref = [dict(k) for k in ex]
        _apply_macd_full(ref)
        _apply_rsi_full(ref)

        ms, rs = {}, {}
        _tick_update_metrics(ex, ms, rs, "main")
        win = len(ex)

        # [1] 整窗逐位一致（含末根）——这是本用例的主断言
        for i in range(win):
            got = (ex[i]["dif"], ex[i]["dea"], ex[i]["macd"])
            want = (ref[i]["dif"], ref[i]["dea"], ref[i]["macd"])
            if got != want:
                failures.append(f"⑤f n={n_confirm} 窗={win} 第{i}根 tick={got} 全量={want}")
                print(f"[FAIL] ⑤f 整窗不一致: n={n_confirm} 窗={win} 第{i}根 {got} != {want}")
                return

        # [2] 情形分派：门槛以**快照当时的整窗长度**为准
        if n_confirm >= MACD_MIN_BARS:
            # 快照本就整窗填充 ⇒ 重建分支的重写必须幂等（不改动已填好的值）
            for i, t in enumerate(before):
                cur = (ex[i]["dif"], ex[i]["dea"], ex[i]["macd"])
                if t != cur:
                    failures.append(f"⑤f n={n_confirm} 第{i}根 重建重写非幂等 {t} → {cur}")
                    print(f"[FAIL] ⑤f 重建重写非幂等: n={n_confirm} 第{i}根 {t} → {cur}")
                    return
        elif win >= MACD_MIN_BARS:
            # 关键情形（n_confirm = 25 ⇒ 窗 = 26）：快照生成时整窗不足门槛、被整窗置 0；
            # 此刻窗口已越界 ⇒ 前 n-1 根必须被补齐，不许留「25 个 0 + 孤立点」
            zeros = sum(1 for k in ex[:-1] if (k["dif"], k["dea"], k["macd"]) == (0, 0, 0))
            if zeros > 1:      # 首根天然 0（dif[0] = ema12[0] − ema26[0] = 0）
                failures.append(f"⑤f n={n_confirm} 窗={win} 跨门槛后仍有 {zeros} 根为 0（孤立点）")
                print(f"[FAIL] ⑤f 跨门槛后仍有 {zeros} 根为 0: n={n_confirm} 窗={win}")
                return
        else:
            # 整窗仍不足门槛 ⇒ 整窗置 0（与 _apply_macd_full 同口径）
            if any((k["dif"], k["dea"], k["macd"]) != (0, 0, 0) for k in ex):
                failures.append(f"⑤f n={n_confirm} 窗={win} 整窗不足门槛却未整窗置 0")
                print(f"[FAIL] ⑤f 整窗<{MACD_MIN_BARS} 未整窗置 0: n={n_confirm} 窗={win}")
                return
    print("[PASS] ⑤f tick 后整窗 ≡ 全量整窗（8 个窗口含 25/26 边界逐位一致；"
          "跨门槛无孤立点、重建重写幂等）")


def main():
    ap = argparse.ArgumentParser(description="P2-4 增量快照守护用例")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    args = ap.parse_args()

    failures = []
    test_incremental_klines(failures)
    test_macd_incremental_equals_full(failures)
    test_macd_state_missing_fallback(failures)
    test_snapshot_incremental_consistency(failures)
    test_macd_stream_equals_full(failures)
    test_tick_metrics_equals_snapshot(failures)
    test_tick_state_isolated_by_key(failures)
    test_tick_no_full_recompute(failures)
    test_macd_min_bars_semantics(failures)
    test_tick_window_equals_full_window(failures)

    print()
    if failures:
        print(f"===== P2-4 增量快照: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== P2-4 增量快照: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

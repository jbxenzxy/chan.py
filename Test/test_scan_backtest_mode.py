# -*- coding: utf-8 -*-
"""
回测扫描（扫描模式 `"backtest"`）+ 买/卖点扫描的类型过滤 护栏
====================================================================
钉住「股票扫描」的两处新增行为：

  ① 后端 worker 层：`AppScan.Scanner.scan_one(mode="backtest")` 的输出结构，
     以及它与单页回测（`App.AppBacktest.compute_stock_backtest`）**同内核、
     同口径** —— 同一份 klines 喂两条路径，笔数 / 期望值必须逐位相等。
     这一条是本功能能否成立的前提：若 worker 里另写一套算法，扫描出来的数字
     就与「点开这只票手点回测」的数字对不上，面板立刻变成不可信的数表。
  ②b 买/卖点扫描（`mode=""`）也按同一份 `bsp_types` 做**事前过滤** ——
     过滤发生在收集候选之前，被拒类型连"最新买卖点是买还是卖"的判定都不参与。
     判据必须复用 `Backtest.Filter.bsp_type_allowed`（与 Trading 引擎
     `_bsp_type_allowed` 逐字同源、被 p59 护栏钉住），不许自造第二套 ——
     这样「图上画的 = 扫描扫的 = 自动下单用的」三者过同一条门。
  ② 前端真函数层（node 抽 app.js 真代码）：排序（期望值降序 / `null` 沉底）、
     列序（股票名 · 代码 · 笔数 · 期望值）、默认勾选（仅期望值 > 0）、涨红跌绿，
     以及**弹窗置灰契约**：回测模式下「最近N根」置灰，而「扫描来源」「扫描周期」
     保持可用 —— 这条是需求核心，反了就等于功能没做（并附标注模式作对照，
     证明断言有判别力：标注模式三个全灰）。
  ③ 透传契约层：`bsp_types` 从路由到 worker 的整条签名链。`None`（未启用过滤
     = 全放行）与 `""`（四类全不勾 = 零成交）语义**相反**，中途任何一处用
     `if x` 收口，都会把"全不勾"静默变成"全放行"（与单页回测同一条坑）。

样本：与 bt01 / test_stock_backtest.py 共用 `Test/fixtures_real/sz002190_d.json`
日线冻结切片 ⇒ 三个用例集说的是**同一批 K 线**，数字可交叉验证。

跑法：`python Test/test_scan_backtest_mode.py`（退出码 0/1 即判决）
"""
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from App.AppBacktest import compute_stock_backtest     # noqa: E402
from App import AppScan as _sc                         # noqa: E402
from App import AppScanPool as _pool                   # noqa: E402

FIXTURES = os.path.join(ROOT, "Test", "fixtures_real")
APPJS = os.path.join(ROOT, "Frontend", "app.js")
APPHTML = os.path.join(ROOT, "Frontend", "app.html")
APPCSS = os.path.join(ROOT, "Frontend", "app.css")
FRONTAPI = os.path.join(ROOT, "FrontAPI.py")

_OK = []


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  —— " + str(detail)[:260]))
    return cond


def _page_klines(fixture_name):
    """冻结切片（`dt` 连字符）→ 页面 `chartData.klines` 格式（斜杠日期）。

    与 `test_stock_backtest.py::_page_klines` 同一转换规则（页面格式的
    SSOT 是 `Common.func_util._get_date_fmt`），此处只覆盖日线。
    """
    recs = json.load(io.open(os.path.join(FIXTURES, fixture_name), encoding="utf-8"))
    out = []
    for r in recs:
        dt = r["dt"]
        out.append({
            "date": "%s/%s/%s" % (dt[:4], dt[5:7], dt[8:10]),
            "open": r["open"], "high": r["high"], "low": r["low"],
            "close": r["close"], "vol": r["vol"], "amount": r["amount"],
        })
    return out


def _run_scan_one(klines, name="测试股", mode="backtest", bsp_types=None,
                  freq="d", code="002190", prefix="0", bsps=None):
    """桩驱动 `scan_one`：把 `analyze_stock` 换成本地冻结切片。

    真实 `analyze_stock` 要读通达信本地 vipdoc，纯净环境没有 ⇒ 用桩喂同一份
    klines。桩法**不改被测逻辑**：`scan_one` 的各个模式消费的就是
    `result["klines"]` / `result["bsps"]`，与真路径逐字段同形。
    """
    def _stub(qualified_code, **kw):
        return {"klines": klines, "meta": {"name": name}, "bsps": bsps or [],
                "bis": []}

    orig = _sc._m.analyze_stock
    _sc._m.analyze_stock = _stub
    try:
        return _sc.scanner.scan_one(code, freq=freq, prefix=prefix, recent="1",
                                    source="zxg", mode=mode, bsp_types=bsp_types)
    finally:
        _sc._m.analyze_stock = orig


# ══════════════════════════════════════════════════════════════════════
# ① 后端：扫描回测 ≡ 单页回测（同内核同口径）
# ══════════════════════════════════════════════════════════════════════
def part1(kl):
    print("══ ① 后端 worker：扫描回测 ≡ 单页回测（同内核同口径）══")
    row = _run_scan_one(kl, name="测试股")
    direct = compute_stock_backtest("sz002190", {"freq": "d", "klines": kl})

    check("① 返回结构与前端契约字段齐全",
          isinstance(row, dict) and "error" not in row
          and all(k in row for k in ("code", "name", "freq", "bars", "date_from",
                                     "date_to", "filled", "closed", "still_open",
                                     "win_rate", "avg_net_return_pct")),
          "row=%r" % (sorted(row.keys()) if isinstance(row, dict) else row))
    check("① 代码补全市场前缀（裸 002190 → sz002190）",
          row.get("code") == "sz002190", row.get("code"))
    check("① 股票名透传自 analyze_stock 的 meta.name",
          row.get("name") == "测试股", row.get("name"))
    check("① 周期原样回显", row.get("freq") == "d", row.get("freq"))

    # —— 判据核心：两条路径的数字必须逐位相等 ——
    # 基线 5 笔：2026-10-08 最新内核恢复了 `_has_bsp_for_bi` 拦截，2 类买卖点
    #   不再进入信号流，本切片 6→5 笔（与 test_stock_backtest 同源同值）。
    check("① 笔数与单页回测**逐位相等**（filled=5 基线）",
          row.get("filled") == direct["run"]["filled"] == 5,
          "scan=%r direct=%r" % (row.get("filled"), direct["run"]["filled"]))
    check("① 期望值(%/笔) 与单页回测**逐位相等**（同一份 klines 同一内核）",
          row.get("avg_net_return_pct") == direct["summary"]["avg_net_return_pct"],
          "scan=%r direct=%r" % (row.get("avg_net_return_pct"),
                                 direct["summary"]["avg_net_return_pct"]))
    check("① 胜率 / 已平 / 未平与单页回测逐位相等",
          row.get("win_rate") == direct["summary"]["win_rate"]
          and row.get("closed") == direct["run"]["closed"]
          and row.get("still_open") == direct["run"]["still_open"],
          "scan=%r direct=%r" % ((row.get("win_rate"), row.get("closed"),
                                  row.get("still_open")),
                                 (direct["summary"]["win_rate"],
                                  direct["run"]["closed"],
                                  direct["run"]["still_open"])))
    check("① 区间 / 根数透传自内核 target（2021-01-04 ~ 2026-09-30 / 1393 根）",
          row.get("bars") == direct["target"]["bars"] == 1393
          and row.get("date_from") == direct["target"]["date_from"]
          and row.get("date_to") == direct["target"]["date_to"],
          "scan=%r direct=%r" % ((row.get("bars"), row.get("date_from"),
                                  row.get("date_to")), direct["target"]))
    return direct


# ══════════════════════════════════════════════════════════════════════
# ② bsp_types 三态（口径不合并）
# ══════════════════════════════════════════════════════════════════════
def part2(kl):
    print("══ ② bsp_types 三态：None / \"\" / 全选 ══")
    r_none = _run_scan_one(kl, bsp_types=None)
    r_all = _run_scan_one(kl, bsp_types="0,1,2,3")
    r_empty = _run_scan_one(kl, bsp_types="")
    r_only1 = _run_scan_one(kl, bsp_types="1")

    check("② None = 未启用过滤（全放行，5 笔）",
          r_none.get("filled") == 5, r_none.get("filled"))
    check("② \"0,1,2,3\" ≡ None（四类全放行同样 5 笔）",
          r_all.get("filled") == 5, r_all.get("filled"))
    check("② \"\" = 四类全不勾 ⇒ 0 笔（**不是**全放行）",
          r_empty.get("filled") == 0, r_empty.get("filled"))
    check("② 0 笔仍返回正常行、不报错（前端按 filled>=1 过滤，不是靠异常）",
          "error" not in r_empty and r_empty.get("avg_net_return_pct") is None,
          r_empty)
    check("② 单类过滤生效且 ≠ 全放行（判别力：与 None 不同即证明过滤真的进了内核）",
          r_only1.get("filled") != r_none.get("filled"),
          "only1=%r none=%r" % (r_only1.get("filled"), r_none.get("filled")))


# ══════════════════════════════════════════════════════════════════════
# ②b 买/卖点扫描（mode=""）同样按 bsp_types 事前过滤
# ══════════════════════════════════════════════════════════════════════
def _count_pts(row):
    return (len(row.get("buy_points") or []), len(row.get("sell_points") or []))


def part2b(kl):
    print("══ ②b 买/卖点扫描（mode=\"\"）同样按 bsp_types 事前过滤 ══")
    last = kl[-1]["date"]
    bsps = []
    for i, t in enumerate(("0", "1", "2", "3")):
        bsps.append({"type": t, "price": 10 + i, "date": last, "is_buy": True})
        bsps.append({"type": t, "price": 20 + i, "date": last, "is_buy": False})

    r_none = _run_scan_one(kl, mode="", bsp_types=None, bsps=bsps)
    r_all = _run_scan_one(kl, mode="", bsp_types="0,1,2,3", bsps=bsps)
    r_1 = _run_scan_one(kl, mode="", bsp_types="1", bsps=bsps)
    r_03 = _run_scan_one(kl, mode="", bsp_types="0,3", bsps=bsps)
    r_empty = _run_scan_one(kl, mode="", bsp_types="", bsps=bsps)

    check("②b 未启用过滤（None）⇒ 4 买 4 卖全收（**改动前的行为**，向后兼容）",
          _count_pts(r_none) == (4, 4), _count_pts(r_none))
    check("②b 四类全勾 ≡ 未启用过滤（同样 4/4）",
          _count_pts(r_all) == (4, 4), _count_pts(r_all))
    check("②b 只勾 1 类 ⇒ 只剩 1 类的买/卖点（过滤真的进了收集环节，不是只挡显示）",
          _count_pts(r_1) == (1, 1)
          and all(p["type"] == "1" for p in r_1["buy_points"]),
          r_1.get("buy_points"))
    check("②b 勾 0、3 两类 ⇒ 各 2 个（2 类型 × 买/卖各一）",
          _count_pts(r_03) == (2, 2), _count_pts(r_03))
    check("②b 四类全不勾 ⇒ 一个点都收不到（命中数天然为 0，且不报错）",
          _count_pts(r_empty) == (0, 0) and "error" not in r_empty, r_empty)

    # 逗号串（同一位置合并出的多类型，type2str → "1,3"）——与引擎
    # `_bsp_type_allowed` / 前端 drawBspMarkers 同一判据：任一段被勾选即放行。
    comma = [{"type": "1,3", "price": 1, "date": last, "is_buy": True}]
    r_c_hit = _run_scan_one(kl, mode="", bsp_types="3", bsps=comma)
    r_c_miss = _run_scan_one(kl, mode="", bsp_types="2", bsps=comma)
    check("②b 逗号串任一子类型被勾选即放行（\"1,3\" 勾 3 ⇒ 命中）",
          len(r_c_hit.get("buy_points") or []) == 1, r_c_hit.get("buy_points"))
    check("②b 逗号串子类型全未勾选 ⇒ 过滤掉（\"1,3\" 只勾 2 ⇒ 不命中）",
          len(r_c_miss.get("buy_points") or []) == 0, r_c_miss.get("buy_points"))

    # 判据同源（不许自造第二套）：直接 import Backtest.Filter 的那个函数，
    # 它与 Trading/Engine/Engine.py::_bsp_type_allowed 逐字同源（p59 护栏钉住）。
    src_scan = io.open(os.path.join(ROOT, "App", "AppScan.py"),
                       encoding="utf-8").read()
    check("②b 判据复用 Backtest.Filter.bsp_type_allowed（与引擎/图表同一条门）",
          "from Backtest.Filter import bsp_type_allowed" in src_scan, "未复用")
    check("②b 过滤发生在**收集候选之前**（事前过滤：被拒类型不参与命中判定）",
          src_scan.index("bsp_type_allowed(bsp.get(\"type\"")
          < src_scan.index("has_points = buy_points or sell_points"),
          "过滤位置不在收集前")

    # 前端：买/卖点扫描也带 bsp_types，且结果披露类型口径
    appjs = io.open(APPJS, encoding="utf-8").read()
    check("②b 前端买/卖点扫描也传 bsp_types（与回测扫描同一份取值）",
          bool(re.search(r'mode:\s*"",[\s\S]{0,900}?bsp_types:\s*_btBspTypes\(\)',
                         appjs)),
          "bsp spec 未传 bsp_types")
    check("②b 前端买/卖点扫描结果披露买卖点类型口径",
          "买卖点类型：" in appjs, "缺披露行")
    # 措辞必须点明「事前」过滤：被拒类型**不参与**「最新买卖点是买还是卖」的
    #   判定（§4.7.3 定案）。说成"结果里过滤掉"会误导用户以为放行类型的命中
    #   不受勾选影响 —— 这正是「图上有买卖点却扫不到」被当 bug 的成因。
    check("②b 买卖点披露写明**事前过滤**语义（未勾选类型不参与判定）",
          "未勾选的类型不参与判定（事前过滤，非事后过滤）" in appjs,
          "事前过滤语义未写进披露行")


# ══════════════════════════════════════════════════════════════════════
# ③ 单票数据问题收敛为「跳过」而非整批失败
# ══════════════════════════════════════════════════════════════════════
def part3(kl):
    print("══ ③ 空 klines / 坏代码：收敛为跳过，不炸整批 ══")
    r_empty = _run_scan_one([], name="新股")
    check("③ klines 为空 → 返回 error 行（**不抛异常**）",
          isinstance(r_empty, dict) and "error" in r_empty, r_empty)
    check("③ 错误文案是人话，不含 traceback / 类名堆栈",
          "Traceback" not in str(r_empty.get("error"))
          and "BadRequestError" not in str(r_empty.get("error")),
          r_empty.get("error"))
    r_ok = _run_scan_one(kl)
    check("③ 同批次内其它票不受影响（上一票失败不污染下一票）",
          r_ok.get("filled") == 5, r_ok.get("filled"))


# ══════════════════════════════════════════════════════════════════════
# ④ 透传契约：bsp_types 全链签名 + 派发位置
# ══════════════════════════════════════════════════════════════════════
def part4():
    print("══ ④ 透传契约：bsp_types 全链签名 / 派发 / 路由 ══")
    p_scan = inspect.signature(_sc.Scanner.scan_one).parameters
    check("④ Scanner.scan_one 收 bsp_types（默认 None）",
          "bsp_types" in p_scan and p_scan["bsp_types"].default is None,
          list(p_scan))
    p_sub = inspect.signature(_sc.Scanner.submit_batch_scan).parameters
    check("④ Scanner.submit_batch_scan 收 bsp_types",
          "bsp_types" in p_sub, list(p_sub))
    p_pool = inspect.signature(_pool.submit_batch_scan).parameters
    check("④ AppScanPool.submit_batch_scan 收 bsp_types",
          "bsp_types" in p_pool, list(p_pool))
    p_w = inspect.signature(_pool._worker_scan_one).parameters
    check("④ _worker_scan_one 参数序 = ...mode, bsp_types, seq（位置必须对得上派发）",
          list(p_w) == ["task_id", "code", "freq", "prefix", "recent", "source",
                        "mode", "bsp_types", "seq"], list(p_w))

    src_pool = io.open(os.path.join(ROOT, "App", "AppScanPool.py"),
                       encoding="utf-8").read()
    m = re.search(r"pool\.submit\(_worker_scan_one,([^)]*)\)", src_pool)
    n_args = len([x for x in (m.group(1).split(",") if m else []) if x.strip()])
    check("④ 派发实参 9 个（与 _worker_scan_one 形参一一对应）",
          n_args == 9, "n_args=%r src=%r" % (n_args, m.group(0) if m else None))
    check("④ 派发把 bsp_types 排在 mode 与 seq 之间",
          bool(m) and re.search(r"mode\s*,\s*bsp_types\s*,\s*seq", m.group(1)),
          m.group(1) if m else None)

    src_api = io.open(FRONTAPI, encoding="utf-8").read()
    check("④ 路由 /scan/submit 声明 bsp_types 且缺省 None（不传 = 全放行）",
          bool(re.search(r"bsp_types:\s*typing\.Optional\[str\]\s*=\s*Body\(None\)",
                         src_api)),
          "未找到 bsp_types 声明")
    check("④ 路由把 bsp_types 作为末位实参交给 submit_batch_scan",
          bool(re.search(r"scan_token\s+or\s+None,\s*bsp_types\)", src_api)),
          "未找到透传调用")

    src_pool_txt = src_pool
    check("④ 池层不吞 bsp_types（submit 调用带 bsp_types=）",
          "bsp_types=bsp_types" in src_pool_txt, "submit 未透传")
    check("④ worker 层不吞 bsp_types（scan_one 调用带 bsp_types=）",
          "bsp_types=bsp_types" in src_pool_txt, "worker 未透传")


# ══════════════════════════════════════════════════════════════════════
# ⑤ 前端真函数（node 抽段）
# ══════════════════════════════════════════════════════════════════════
def _extract_any_fn(appjs, fn_name):
    """按**花括号配平**抽一个函数（兼容单行 / 多行两种写法）。"""
    m = re.search(r"        function " + fn_name + r"\(", appjs)
    assert m, "app.js 抽取失败: " + fn_name
    i = appjs.index("{", m.end())
    depth = 0
    for j in range(i, len(appjs)):
        if appjs[j] == "{":
            depth += 1
        elif appjs[j] == "}":
            depth -= 1
            if depth == 0:
                return appjs[appjs.rfind("\n", 0, m.start()) + 1:j + 1]
    raise AssertionError("app.js 花括号不配平: " + fn_name)


_ROWS = [
    # 故意打乱输入顺序 + 混入 null，验证"排序"真的发生（不是顺序透传）
    {"code": "sz000003", "name": "C股", "filled": 3, "closed": 2, "still_open": 1,
     "avg_net_return_pct": -1.5, "bars": 500,
     "date_from": "2024-01-01", "date_to": "2026-10-01"},
    {"code": "sz000001", "name": "A股", "filled": 10, "closed": 10, "still_open": 0,
     "avg_net_return_pct": 3.25, "bars": 500,
     "date_from": "2024-01-01", "date_to": "2026-10-01"},
    {"code": "sz000002", "name": "B股", "filled": 5, "closed": 0, "still_open": 5,
     "avg_net_return_pct": None, "bars": 500,
     "date_from": "2024-01-01", "date_to": "2026-10-01"},
    {"code": "sz000004", "name": "D股", "filled": 8, "closed": 8, "still_open": 0,
     "avg_net_return_pct": 1.0, "bars": 500,
     "date_from": "2024-01-01", "date_to": "2026-10-01"},
]


def part5():
    print("══ ⑤ 前端真函数（node 抽段；node 不在位则 SKIP）══")
    node = shutil.which("node")
    if not node:
        check("node 不在位，前端契约层 SKIP", True)
        return

    appjs = io.open(APPJS, encoding="utf-8").read()

    prelude = (
        "var window = globalThis;\n"
        "var _scanSources = ['zxg'];\n"
        "var _scanFreq = 'd';\n"
        "var bspFilter = { '0': true, '1': true, '2': true, '3': true };\n"
        # 回测行渲染的外部依赖（只关心结构与文案，故用极简替身）
        "function statsEsc(x) { return String(x); }\n"
        "function chkBox(code, checked) { return '<chk:' + code + ':' + (checked ? '1' : '0') + '>'; }\n"
        "function _scanMarketSummaryHtml(rs) { return ''; }\n"
        "function updateScanSaveBtn() {}\n"
        "var SCAN_HTML = '';\n"
        "var DOM = { 'scan-body': { innerHTML: '' } };\n"
        # updateScanRecentDisabled 的三个探针元素 + 模式单选
        "var _els = {"
        " 'scan-recent-row': { style: {} },"
        " 'scan-recent-days': { disabled: null },"
        " 'scan-freq-row': { style: {} },"
        " 'scan-source-section': { style: {} } };\n"
        "var _modeChecked = null;\n"
        "var document = {\n"
        "  getElementById: function (id) { return _els[id] || DOM[id] || null; },\n"
        "  querySelector: function () { return _modeChecked; }\n"
        "};\n"
        "var out = [];\n"
    )

    driver = (
        # ── ⑤a 弹窗置灰契约：回测 = 最近N根灰、来源与周期可用 ──
        "function _probeMode(v) {\n"
        "  _modeChecked = { value: v };\n"
        "  _els['scan-recent-row'].style = {};\n"
        "  _els['scan-recent-days'].disabled = null;\n"
        "  _els['scan-freq-row'].style = {};\n"
        "  _els['scan-source-section'].style = {};\n"
        "  updateScanRecentDisabled();\n"
        "  return JSON.stringify({\n"
        "    recent: _els['scan-recent-row'].style.opacity,\n"
        "    recentDis: _els['scan-recent-days'].disabled,\n"
        "    freq: _els['scan-freq-row'].style.opacity,\n"
        "    src: _els['scan-source-section'].style.opacity });\n"
        "}\n"
        "out.push('M_bt=' + _probeMode('backtest'));\n"
        "out.push('M_ann=' + _probeMode('ann'));\n"
        "out.push('M_bsp=' + _probeMode('bsp'));\n"
        "out.push('M_fxd=' + _probeMode('fx_d'));\n"
        # ── ⑤b 排序 / 列序 / 勾选 / 配色 ──
        "var rows = " + json.dumps(_ROWS, ensure_ascii=False) + ";\n"
        "var html = _renderBacktestRows(rows);\n"
        "SCAN_HTML = html;\n"
        "function _idx(s) { return html.indexOf(s); }\n"
        "out.push('ORDER=' + JSON.stringify({\n"
        "  a: _idx('A股'), d: _idx('D股'), c: _idx('C股'), b: _idx('B股'),\n"
        "  ok: _idx('A股') < _idx('D股') && _idx('D股') < _idx('C股') && _idx('C股') < _idx('B股') }));\n"
        "function _rowHtml(code) { var i = html.indexOf('loadScanResult(\\'' + code); return html.slice(i, html.indexOf('</div>', i)); }\n"
        "var rA = _rowHtml('sz000001');\n"
        "out.push('COLS=' + JSON.stringify({\n"
        "  name: rA.indexOf('scan-col-name'),\n"
        "  code: rA.indexOf('scan-col-code'),\n"
        "  cnt: rA.indexOf('scan-col-btcount'),\n"
        "  exp: rA.indexOf('scan-col-expect'),\n"
        "  ok: rA.indexOf('scan-col-name') < rA.indexOf('scan-col-code')\n"
        "    && rA.indexOf('scan-col-code') < rA.indexOf('scan-col-btcount')\n"
        "    && rA.indexOf('scan-col-btcount') < rA.indexOf('scan-col-expect') }));\n"
        "out.push('CHK=' + JSON.stringify({\n"
        "  a: html.indexOf('<chk:sz000001:1>') >= 0,\n"
        "  d: html.indexOf('<chk:sz000004:1>') >= 0,\n"
        "  c: html.indexOf('<chk:sz000003:0>') >= 0,\n"
        "  b: html.indexOf('<chk:sz000002:0>') >= 0 }));\n"
        "out.push('COLOR=' + JSON.stringify({\n"
        "  posRed: _rowHtml('sz000001').indexOf('#FF3C3C') >= 0,\n"
        "  negGreen: _rowHtml('sz000003').indexOf('#00F0F0') >= 0,\n"
        "  naGrey: _rowHtml('sz000002').indexOf('#8b93a7') >= 0,\n"
        "  posTxt: _rowHtml('sz000001').indexOf('3.25%') >= 0,\n"
        "  negTxt: _rowHtml('sz000003').indexOf('-1.50%') >= 0,\n"
        "  naTxt: _rowHtml('sz000002').indexOf('—') >= 0,\n"
        "  filledTxt: _rowHtml('sz000001').indexOf('10 笔') >= 0 }));\n"
        # ── ⑤c 提交体：null 不带字段 / "" 如实带 ──
        "out.push('BODY_null=' + JSON.stringify(_scanSubmitBody([], {freq:'d', mode:'backtest', bsp_types: null})));\n"
        "out.push('BODY_empty=' + JSON.stringify(_scanSubmitBody([], {freq:'d', mode:'backtest', bsp_types: ''})));\n"
        "out.push('BODY_03=' + JSON.stringify(_scanSubmitBody([], {freq:'d', mode:'backtest', bsp_types: '0,3'})));\n"
        # ── ⑤d 买卖点类型披露文案 ──
        "out.push('LBL_null=' + _btBspTypesLabel(null));\n"
        "out.push('LBL_empty=' + _btBspTypesLabel(''));\n"
        "out.push('LBL_03=' + _btBspTypesLabel('0,3'));\n"
        # ── ⑤e 终态渲染：摘要 / 口径披露 / 空态 ──
        #   renderBacktestScanResults 的产物落在 #scan-body.innerHTML（它是
        #   "写面板"型函数，不像 _renderBacktestRows 那样 return html）⇒ 从 DOM 探针读回。
        "function _fin() { return document.getElementById('scan-body').innerHTML; }\n"
        "document.getElementById('scan-body').innerHTML = '';\n"
        "renderBacktestScanResults(rows.slice(), 100, 7, false);\n"
        "var F = _fin();\n"
        "out.push('FIN=' + JSON.stringify({\n"
        "  html: F,\n"
        "  cnt: F.indexOf('有成交 <b>4</b> 只') >= 0,\n"
        "  pos: F.indexOf('期望 > 0 <b>2</b> 只') >= 0,\n"
        "  skip: F.indexOf('跳过 <b>7</b> 只') >= 0,\n"
        "  caliber: F.indexOf('区间：最新 500 根（截至 2026-10-01）') >= 0,\n"
        "  bsp: F.indexOf('买卖点类型：全部（未启用过滤）') >= 0 }));\n"
        "document.getElementById('scan-body').innerHTML = '';\n"
        "renderBacktestScanResults([], 100, 100, false);\n"
        "var E = _fin();\n"
        "out.push('EMPTY=' + JSON.stringify({ html: E,\n"
        "  msg: E.indexOf('没有股票产生成交') >= 0 }));\n"
        "console.log(out.join('\\n'));\n"
    )

    harness = (prelude
               + _extract_any_fn(appjs, "updateScanRecentDisabled") + "\n"
               + _extract_any_fn(appjs, "_scanSourceLabel") + "\n"
               + _extract_any_fn(appjs, "_btBspTypes") + "\n"
               + _extract_any_fn(appjs, "_btBspTypesLabel") + "\n"
               + _extract_any_fn(appjs, "_btExpect") + "\n"
               + _extract_any_fn(appjs, "_btCol") + "\n"
               + _extract_any_fn(appjs, "_btPct") + "\n"
               + _extract_any_fn(appjs, "_scanSubmitBody") + "\n"
               + _extract_any_fn(appjs, "_renderBacktestRows") + "\n"
               + _extract_any_fn(appjs, "renderBacktestScanResults") + "\n"
               + driver)

    jf = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    jf.write(harness)
    jf.close()
    try:
        proc = subprocess.run([node, jf.name], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        lines = [x for x in proc.stdout.splitlines() if x.strip()]
    finally:
        os.unlink(jf.name)

    if proc.returncode != 0:
        check("node 执行成功", False, (proc.stderr or proc.stdout)[:500])
        return
    kv = dict(x.split("=", 1) for x in lines)

    # ⑤a 置灰契约
    m_bt = json.loads(kv["M_bt"])
    m_ann = json.loads(kv["M_ann"])
    m_bsp = json.loads(kv["M_bsp"])
    m_fxd = json.loads(kv["M_fxd"])
    check("⑤a 回测模式：「最近N根」置灰（opacity 0.35 + input 真 disabled）",
          m_bt["recent"] == "0.35" and m_bt["recentDis"] is True, m_bt)
    check("⑤a 回测模式：「扫描周期」**可用**（需求核心）",
          m_bt["freq"] == "1", m_bt)
    check("⑤a 回测模式：「扫描来源」**可用**（需求核心）",
          m_bt["src"] == "1", m_bt)
    check("⑤a 对照·标注模式：来源与周期**都**置灰（证明上两条不是恒真断言）",
          m_ann["freq"] == "0.35" and m_ann["src"] == "0.35", m_ann)
    check("⑤a 对照·买卖点模式：三个全可用",
          m_bsp["recent"] == "1" and m_bsp["freq"] == "1" and m_bsp["src"] == "1",
          m_bsp)
    check("⑤a 底分型模式：周期与来源可用（与回测同为「最近N根」灰）",
          m_fxd["recent"] == "0.35" and m_fxd["freq"] == "1" and m_fxd["src"] == "1",
          m_fxd)

    # ⑤b 排序 / 列序 / 勾选 / 配色
    order = json.loads(kv["ORDER"])
    check("⑤b 按期望值降序：3.25 → 1.0 → -1.5 → null（null 沉底）",
          order["ok"], order)
    cols = json.loads(kv["COLS"])
    check("⑤b 列序 = 股票名 → 代码 → 笔数 → 期望值",
          cols["ok"], cols)
    chk = json.loads(kv["CHK"])
    check("⑤b 默认勾选：仅期望值 > 0 的两只（null / 负期望不勾）",
          chk["a"] and chk["d"] and chk["c"] and chk["b"], chk)
    color = json.loads(kv["COLOR"])
    check("⑤b 涨红跌绿（正值 #FF3C3C / 负值 #00F0F0 / 无值灰 #8b93a7）",
          color["posRed"] and color["negGreen"] and color["naGrey"], color)
    check("⑤b 数值格式：正 3.25% / 负 -1.50% / 无值 — / 笔数「10 笔」",
          color["posTxt"] and color["negTxt"] and color["naTxt"]
          and color["filledTxt"], color)

    # ⑤c 提交体
    b_null = json.loads(kv["BODY_null"])
    b_empty = json.loads(kv["BODY_empty"])
    b_03 = json.loads(kv["BODY_03"])
    check("⑤c bsp_types=null（未启用过滤）⇒ 提交体**不带**该字段",
          "bsp_types" not in b_null, b_null)
    check("⑤c bsp_types=\"\"（全不勾）⇒ **如实带上**空串（不得被吞）",
          b_empty.get("bsp_types") == "", b_empty)
    check("⑤c bsp_types=\"0,3\" 原样透传",
          b_03.get("bsp_types") == "0,3", b_03)

    # ⑤d 披露文案
    check("⑤d 类型披露：null → 「全部（未启用过滤）」",
          kv["LBL_null"] == "全部（未启用过滤）", kv["LBL_null"])
    check("⑤d 类型披露：\"\" → 明说不会有任何成交（防「全不勾」被误读成无过滤）",
          "全不勾" in kv["LBL_empty"], kv["LBL_empty"])
    check("⑤d 类型披露：\"0,3\" → 「0类、3类」",
          kv["LBL_03"] == "0类、3类", kv["LBL_03"])

    # ⑤e 终态
    fin = json.loads(kv["FIN"])
    check("⑤e 终态摘要：总数 / 跳过 / 有成交 / 期望>0 四项齐全",
          fin["cnt"] and fin["pos"] and fin["skip"], fin)
    check("⑤e 终态披露区间与根数（口径差异必须写在脸上）",
          fin["caliber"], fin["html"][:600])
    check("⑤e 终态披露买卖点类型口径（与提交给后端的同一份取值）",
          fin["bsp"], fin["html"][:600])
    empty = json.loads(kv["EMPTY"])
    check("⑤e 空结果态给出可读提示（而非空白面板）",
          empty["msg"], empty["html"][:300])


# ══════════════════════════════════════════════════════════════════════
# ⑥ 静态结构契约（含静态资源版本号）
# ══════════════════════════════════════════════════════════════════════
def part6():
    print("══ ⑥ 静态结构契约（HTML / CSS / 白名单 / 版本号）══")
    html = io.open(APPHTML, encoding="utf-8").read()
    check("⑥ 弹窗有「回测」单选项（name=scan-mode / value=backtest）",
          bool(re.search(r'name="scan-mode"\s+value="backtest"', html)),
          "未找到 scan-mode=backtest")
    check("⑥ 该单选项带 onchange 联动置灰逻辑（否则切换后灰化状态不更新）",
          bool(re.search(r'value="backtest"[^>]*onchange="updateScanRecentDisabled\(\)"',
                         html)),
          "缺 onchange")
    check("⑥ 静态资源版本号已 bump（v=73，防浏览器吃旧缓存；10-08 放量口径披露同步）",
          'app.js?v=73' in html, "仍指向旧版本号")

    appjs = io.open(APPJS, encoding="utf-8").read()
    check("⑥ localStorage 白名单收 backtest（否则重开弹窗回落到标注模式）",
          bool(re.search(r'savedMode === "fx_d" \|\| savedMode === "fangliang" '
                         r'\|\| savedMode === "lianzhang" \|\| savedMode === "backtest"', appjs)),
          "白名单未含 backtest")
    check("⑥ 结果面板标题分支存在（日K 回测）",
          'freqLabel + " 回测"' in appjs, "缺标题分支")

    css = io.open(APPCSS, encoding="utf-8").read()
    check("⑥ CSS 有笔数与期望值两列（期望值右对齐 + 等宽数字）",
          ".scan-col-btcount" in css and ".scan-col-expect" in css
          and "tabular-nums" in css,
          "缺列样式")


# ══════════════════════════════════════════════════════════════════════
# ⑦ 真实 spawn 子进程：worker 内的惰性 import 可用（最容易被改坏的一环）
# ══════════════════════════════════════════════════════════════════════
def _spawn_worker(queue):
    """模块级函数（spawn 必须可 pickle）：复刻 ProcessPool worker 的执行环境。

    worker 里是**首次** import 回测内核 —— `App.AppBacktest` → `Backtest.Runner`
    → `Trading.Strategy.Exit` → `Trading/Config.py`（导入期构造 DEFAULT_CONFIG、
    读 `.env`）。这条链在 spawn 子进程里能不能立起来，只有真起一个子进程才知道；
    一旦有人把它提到模块顶层或改动导入期副作用，回测扫描会在点下去那一刻整批失败。
    """
    try:
        from App.AppBacktest import compute_stock_backtest
        kl = _page_klines("sz002190_d.json")
        out = compute_stock_backtest("sz002190", {"freq": "d", "klines": kl})
        queue.put({"ok": True, "filled": out["run"]["filled"],
                   "avg": out["summary"]["avg_net_return_pct"]})
    except Exception as exc:  # noqa: BLE001 —— 回传给父进程判定
        import traceback
        queue.put({"ok": False, "err": "%s: %s" % (type(exc).__name__, exc),
                   "tb": traceback.format_exc()[-600:]})


def part7(kl):
    print("══ ⑦ 真实 spawn 子进程：worker 内惰性 import 回测内核 ══")
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_spawn_worker, args=(q,))
    p.start()
    p.join(240)
    if p.is_alive():
        p.terminate()
        check("⑦ spawn 子进程在限时内结束", False, "超时未退出")
        return
    try:
        got = q.get(timeout=10)
    except Exception:  # noqa: BLE001
        got = None
    check("⑦ 子进程内 `from App.AppBacktest import ...` 成功"
          "（链式 Trading.Config 的导入期副作用不炸 worker）",
          bool(got) and got.get("ok"), got)
    if not (got and got.get("ok")):
        return
    direct = compute_stock_backtest("sz002190", {"freq": "d", "klines": kl})
    check("⑦ 子进程回测结果与主进程**逐位一致**（worker 不是另一套数）",
          got["filled"] == direct["run"]["filled"]
          and got["avg"] == direct["summary"]["avg_net_return_pct"],
          "child=%r main=%r" % (got, (direct["run"]["filled"],
                                      direct["summary"]["avg_net_return_pct"])))


def main():
    print("=" * 68)
    print("回测扫描（scan mode = backtest）护栏")
    print("=" * 68)
    kl = _page_klines("sz002190_d.json")
    part1(kl)
    part2(kl)
    part2b(kl)
    part3(kl)
    part4()
    part5()
    part6()
    part7(kl)
    print("-" * 68)
    total, passed = len(_OK), sum(_OK)
    print("合计 %d 项，通过 %d，失败 %d" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

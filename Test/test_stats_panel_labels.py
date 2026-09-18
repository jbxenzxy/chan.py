# -*- coding: utf-8 -*-
"""
统计面板（右上角「统计」弹窗）字段契约 —— 2026-09-19 用户拍板口径
=====================================================================

被守护的七条
---------------------------------------------------------------------
 ① 品种：只显示**品种键**（IF / IH / IC / IM / AU / AG / CU / TA…），
    不显示合约名（CFFEX.IF2612）。统计口径是「整个品种」—— 同品种的多个
    月份合约是合并统计的（见 Trading/Infra/TradeStats.py 的
    `WHERE product_key = ?`），把合约名列出来会让「品种合计」被读成
    「某一个合约的战绩」。
 ② 平均盈利 / 平均亏损 → 平均每笔盈利 / 平均每笔亏损。
    这两个数正是「盈亏比(赔率)」的两个分量：`pl_ratio = avg_win / |avg_loss|`
    （TradeStats.py:193）。不写「每笔」会被当成总量口径，与「盈利因子」
    （总盈 ÷ 总亏）混淆 —— 而这两个比率名字相近、数值可能差一倍。
 ③ 最大单笔盈利 / 最大单笔亏损：**不显示时间**（exit_at 不再落到面板上）。
 ④ 删除「按出场原因」与「曲线口径」两行。
 ⑤ 期望值/笔 → 期望值（没有「期望值/笔」这种表述；expectancy 本身
    = win_rate*avg_win + loss_rate*avg_loss，已经是每笔量纲）。
 ⑥ 明细行行序：品种 → 成交笔数 → 期望值 →
    平均每笔盈利 / 平均每笔亏损 → 最大单笔盈利 → 最大单笔亏损
 ⑦ 盈利因子提到核心区：一行四格 = 总净盈亏 / 实际胜率 / 盈亏比(赔率) /
    盈利因子；明细区不再重复这一行（同一字段上下各出现一次会让人以为
    是两个不同的指标）。

两层守护
---------------------------------------------------------------------
 [静态]  从 app.js 抽出 renderTradeStats 区块，验标签序列与禁用串；
         不需要浏览器，任何环境都跑。
 [真渲染] 无头 Chrome 打开真实页面 → 打桩 /api/trader/trades 喂一份
         带合约名 / 带 exit_at / 带 by_reason 的响应 → 点「统计」按钮 →
         读 #stats-content 的真实 innerHTML 复核上面七条。
         浏览器不在位时降级 SKIP（静态层已覆盖）。

为什么真渲染层要喂「脏数据」：静态层只能证明源码里没有那段拼接；真渲染层
证明的是**即使后端把合约名、exit_at、by_reason 全都发过来，面板也不会显示**
—— 这才是这七条真正想要的保证。

跑法：python Test/test_stats_panel_labels.py
"""
from __future__ import annotations

import functools
import glob
import http.server
import json
import os
import re
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
APP_JS = os.path.join(_REPO, "Frontend", "app.js")
FRONTEND_DIR = os.path.join(_REPO, "Frontend")
SNAP_FUTURES = os.path.join(_HERE, "snapshots", "futures_15s_full.json")

_FAIL: list[str] = []


def check(cond, name, extra=""):
    ok = bool(cond)
    print(("  [ok]   " if ok else "  [FAIL] ") + name + (("   << " + str(extra)) if not ok else ""))
    if not ok:
        _FAIL.append(name)
    return ok


# 明细行的目标顺序（实现与本测试共用的唯一定义）
EXPECT_ORDER = ["品种", "成交笔数", "期望值",
                "平均每笔盈利 / 平均每笔亏损", "最大单笔盈利", "最大单笔亏损"]

# 核心区（stats-hero）四格的目标顺序（⑦）
EXPECT_HERO = ["总净盈亏", "实际胜率", "盈亏比(赔率)", "盈利因子"]

# 打桩响应里的「脏数据」—— 真渲染层要证明它们一个都上不了面板
DIRTY_CONTRACT_A = "SHFE.rb2510"
DIRTY_CONTRACT_B = "SHFE.rb2601"
DIRTY_EXIT_AT_WIN = "2026-09-18 10:15:00"
DIRTY_EXIT_AT_LOSS = "2026-09-18 14:02:00"
DIRTY_REASON = "tp"


def stats_fixture():
    """一份「什么都带」的统计响应：合约名、exit_at、by_reason 全在。"""
    return {
        "count": 5, "wins": 3, "losses": 2, "flat": 0,
        "total_net": 3400.0,
        "win_rate": 0.6,
        "pl_ratio": 2.55,
        "profit_factor": 3.4,
        "avg_win": 1530.0,
        "avg_loss": -600.0,
        "max_win": {"trade_id": "T-001", "exit_at": DIRTY_EXIT_AT_WIN, "net_cash": 2100.0},
        "max_loss": {"trade_id": "T-005", "exit_at": DIRTY_EXIT_AT_LOSS, "net_cash": -880.0},
        "expectancy": 516.0,
        "by_reason": {DIRTY_REASON: 2100.0, "sl": -880.0},
        "equity_curve": [
            {"exit_at": "2026-09-18 10:15:00", "net_cash": 2100.0, "cumulative": 2100.0},
            {"exit_at": "2026-09-18 14:02:00", "net_cash": -880.0, "cumulative": 1220.0},
            {"exit_at": "2026-09-18 21:05:00", "net_cash": 2180.0, "cumulative": 3400.0},
        ],
        "symbol_raw": "KQ.m@SHFE.rb",
        "symbol_key": "RB",
        "by_symbol": {DIRTY_CONTRACT_A: 3, DIRTY_CONTRACT_B: 2},
        "read_errors": [],
        "dbs_scanned": 1,
    }


# ══════════════════════════════════════════════════════════════
print("[静态] app.js 里 renderTradeStats 的标签与禁用串")
# ══════════════════════════════════════════════════════════════
check(os.path.isfile(APP_JS), "app.js 存在", APP_JS)
js = open(APP_JS, encoding="utf-8").read()

m = re.search(r"function renderTradeStats\(d\) \{(.*?)if \(_writeStatsHtml\(html\)\)", js, re.S)
check(m is not None, "能抽出 renderTradeStats 区块（覆盖面自检）")
if m is None:
    print("\n结果: 失败（抽不到渲染函数，后续无法判定）")
    raise SystemExit(1)
blk = m.group(1)
check(len(blk) > 500, "区块规模合理（正则没只捞到半截）", len(blk))

# 只取明细行：stats-hero 四格单独校验（⑦）；空态分支里也有一个
# stats-rows，故按带分号的整行写法切。
detail = blk.split("'<div class=\"stats-rows\">';", 1)[1]
labels = re.findall(r'class="stats-label">([^<]+)</span>', detail)
check(labels == EXPECT_ORDER, "①⑥ 明细行标签序列 == 目标顺序", labels)
check(labels.index("品种") < labels.index("成交笔数"), "⑥ 品种 在 成交笔数 之前")
check(labels.index("期望值") == labels.index("成交笔数") + 1,
      "⑥ 期望值 紧随 成交笔数 之后")
check("盈利因子" not in labels, "⑦ 明细区不再有盈利因子行（核心区已提上去）", labels)

# ⑦ 核心区四格：从 stats-hero 起行切到它的收尾 '</div>'
hero = blk.split("'<div class=\"stats-hero\">';", 1)[1].split("'</div>';", 1)[0]
hero_labels = re.findall(r'class="stats-label">([^<]+)</span>', hero)
check(hero_labels == EXPECT_HERO, "⑦ 核心区四格序列 == 目标顺序", hero_labels)
check(hero.count("stats-cell") == 4, "⑦ 核心区正好四格（不多不少）",
      hero.count("stats-cell"))
check(hero.count("d.profit_factor") == 1,
      "⑦ 盈利因子格取的是 d.profit_factor（与明细区同一字段）")

check('class="stats-label">品种</span>' in blk, "① 有「品种」行")
check("statsEsc(d.symbol_key)" in blk, "① 品种行取的是 symbol_key（品种键）")
check("d.by_symbol" not in blk, "① 品种行不再读 by_symbol（合约名来源已断）")
check("+ sub +" not in blk, "① 品种行不再拼 sub 后缀（原「（合约 XXX）」）")

check("平均每笔盈利 / 平均每笔亏损" in blk, "② 平均每笔盈利 / 平均每笔亏损")
check("平均盈利 / 平均亏损" not in blk, "② 旧标签「平均盈利 / 平均亏损」无残留")

check("exit_at" not in blk, "③ 明细行区块内 exit_at 零命中（最大单笔不带时间）")
check(blk.count("最大单笔盈利") == 1 and blk.count("最大单笔亏损") == 1,
      "③ 最大单笔盈/亏各一行且只剩金额")

check("按出场原因" not in blk, "④ 「按出场原因」已删除")
check("d.by_reason" not in blk, "④ 不再读 by_reason")
check("曲线口径" not in blk and "stats-note" not in blk, "④ 「曲线口径」注释行已删除")

check('class="stats-label">期望值</span>' in blk, "⑤ 「期望值」标签在位")
check("期望值/笔" not in blk, "⑤ 旧标签「期望值/笔」无残留")

# 全仓防回潮：这些串不该在前端任何地方再冒出来
for bad in ("期望值/笔", "按出场原因", "曲线口径"):
    check(bad not in js, "全仓防回潮：前端无「%s」" % bad)


# ══════════════════════════════════════════════════════════════
print("\n[真渲染] 打桩「脏数据」→ 点「统计」→ 读真实 innerHTML")
# ══════════════════════════════════════════════════════════════
def find_playwright():
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        return sync_playwright, ""
    except Exception as e:
        return None, str(e).splitlines()[0][:120]


def launch_browser(pw):
    """本机 Chrome 优先：本机「捆绑 chromium」在 browser.close() 时会挂住
    Playwright driver，令整个用例收尾卡死（在干净基线上同样复现）。"""
    notes = []
    try:
        return pw.chromium.launch(channel="chrome"), "本机 Chrome", notes
    except Exception as e:
        notes.append("channel=chrome: " + str(e).splitlines()[0][:90])
    try:
        return pw.chromium.launch(), "捆绑 chromium", notes
    except Exception as e:
        notes.append("捆绑 chromium: " + str(e).splitlines()[0][:90])
    for pat in ("~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe",
                "~/AppData/Local/ms-playwright/chromium-*/chrome-win/chrome.exe"):
        for cand in glob.glob(os.path.expanduser(pat)):
            try:
                return pw.chromium.launch(executable_path=cand), os.path.basename(cand), notes
            except Exception as e:
                notes.append(os.path.basename(cand) + ": " + str(e).splitlines()[0][:90])
    return None, "", notes


sync_playwright, why = find_playwright()
if sync_playwright is None:
    print("  [SKIP] " + why + "（静态层已覆盖契约）")
elif not os.path.isfile(SNAP_FUTURES):
    print("  [SKIP] 缺快照 " + SNAP_FUTURES)
else:
    snap_text = open(SNAP_FUTURES, encoding="utf-8").read()
    fixture = json.dumps(stats_fixture())

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=FRONTEND_DIR))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    with sync_playwright() as pw:
        browser, which, notes = launch_browser(pw)
        if browser is None:
            print("  [SKIP] 无可用无头浏览器：" + " / ".join(notes[:3]))
            httpd.shutdown()
        else:
            print("  （无头浏览器：%s）" % which)
            page = browser.new_page(viewport={"width": 1600, "height": 900})

            def route_api(route):
                url = route.request.url
                if "/api/health" in url:
                    body = json.dumps({"config": {"view_count": 233}})
                elif "analyze" in url:
                    body = snap_text
                elif "trader/trades" in url:
                    body = fixture          # 什么都带的脏数据
                else:
                    body = "{}"
                route.fulfill(status=200, content_type="application/json", body=body)

            page.route("**/api/**", route_api)
            page.goto("http://127.0.0.1:%d/index.html" % port, wait_until="load")
            page.wait_for_function(
                "() => { const s = window.ChanApp && window.ChanApp.state;"
                " return !!(s && s.chartData && s.chartData.klines"
                " && s.chartData.klines.length > 0); }", timeout=20000)
            page.wait_for_timeout(400)

            page.click("#btn-stats")
            try:
                page.wait_for_function(
                    "() => { const b = document.getElementById('stats-content');"
                    " return !!(b && b.innerHTML.indexOf('成交笔数') >= 0); }",
                    timeout=15000)
                html = page.evaluate(
                    "() => document.getElementById('stats-content').innerHTML")
            except Exception as e:
                html = ""
                print("  （等统计内容超时：%s）" % str(e).splitlines()[0][:100])

            check("成交笔数" in html, "面板真的渲染出统计明细（不是空态/失败态）",
                  html[:200])
            if html:
                # 只取明细行（stats-hero 四格在上面，单独校验）
                detail_html = html.split('class="stats-rows"', 1)[1] \
                    if 'class="stats-rows"' in html else html
                got = re.findall(r'class="stats-label">([^<]+)</span>', detail_html)
                check(got == EXPECT_ORDER, "①⑥ 真渲染行序 == 目标顺序", got)

                # ⑦ 真渲染：核心四格在位且顺序正确，明细区不再重复盈利因子
                hero_html = ""
                if 'class="stats-hero"' in html and 'class="stats-rows"' in html:
                    hero_html = html.split('class="stats-hero"', 1)[1] \
                                    .split('class="stats-rows"', 1)[0]
                hero_got = re.findall(r'class="stats-label">([^<]+)</span>',
                                      hero_html)
                check(hero_got == EXPECT_HERO, "⑦ 真渲染核心四格 == 目标顺序",
                      hero_got)
                check(hero_html.count("stats-cell") == 4,
                      "⑦ 真渲染核心区正好四格", hero_html.count("stats-cell"))
                check("3.40" in hero_html,
                      "⑦ 真渲染核心区有盈利因子数值（3.40）", hero_html[-200:])
                check("盈利因子" not in detail_html,
                      "⑦ 真渲染明细区无盈利因子行（不重复）",
                      re.findall(r'class="stats-label">([^<]+)</span>',
                                 detail_html))

                # ① 品种格子里只有品种键
                mrow = re.search(r'class="stats-label">品种</span>.*?'
                                 r'class="stats-value"[^>]*>(.*?)</span>', html, re.S)
                check(mrow is not None, "① 真渲染有「品种」行")
                if mrow:
                    val = re.sub(r"<[^>]+>", "", mrow.group(1)).strip()
                    check(val == "RB", "① 品种格 == RB（合约名一个都没带上）", val)
                check(DIRTY_CONTRACT_A not in html and DIRTY_CONTRACT_B not in html,
                      "① 整个面板无合约名（%s / %s）" % (DIRTY_CONTRACT_A, DIRTY_CONTRACT_B))

                check("平均每笔盈利 / 平均每笔亏损" in html, "② 真渲染标签为「平均每笔盈利 / 平均每笔亏损」")

                check(DIRTY_EXIT_AT_WIN not in html and DIRTY_EXIT_AT_LOSS not in html,
                      "③ 最大单笔两行不带时间（exit_at 未渲染）")
                check("+2100.00 元" in html, "③ 最大单笔盈利仍给金额", html[:300])
                check("-880.00 元" in html, "③ 最大单笔亏损仍给金额")

                check("按出场原因" not in html, "④ 真渲染无「按出场原因」")
                check("曲线口径" not in html, "④ 真渲染无「曲线口径」")

                check("期望值" in html and "期望值/笔" not in html, "⑤ 真渲染为「期望值」")

            page.unroute_all(behavior="ignore")
            browser.close()
            httpd.shutdown()


print("\n" + "=" * 62)
print("统计面板字段契约 结果: %s" % ("全部通过" if not _FAIL else "%d 项失败 -> %s" % (len(_FAIL), _FAIL)))
print("=" * 62)
sys.exit(1 if _FAIL else 0)

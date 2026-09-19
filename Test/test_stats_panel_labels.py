# -*- coding: utf-8 -*-
"""
统计面板（右上角「统计」弹窗）字段契约 —— 2026-09-19 用户拍板口径
=====================================================================

被守护的九条
---------------------------------------------------------------------
 ① 期货品种：只显示**品种键**（IF / IH / IC / IM / AU / AG / CU / TA…），
    不显示合约名（CFFEX.IF2612）。统计口径是「整个品种」—— 同品种的多个
    月份合约是合并统计的（见 Trading/Infra/TradeStats.py 的
    `WHERE product_key = ?`），把合约名列出来会让「品种合计」被读成
    「某一个合约的战绩」。标签写全「期货品种」（2026-09-19 用户拍板：
    数据源就是期货自动下单库，写「品种」看不出是股票还是期货）。
 ② 平均盈利 / 平均亏损 → 「平均每笔盈利/亏损」（2026-09-19 用户拍板：
    同一个「每笔」不重复第二遍，标签太长会把右侧的值挤到折行）。
    这两个数正是「盈亏比(赔率)」的两个分量：`pl_ratio = avg_win / |avg_loss|`
    （TradeStats.py:193）。不写「每笔」会被当成总量口径，与「盈利因子」
    （总盈 ÷ 总亏）混淆 —— 而这两个比率名字相近、数值可能差一倍。
 ③ 最大单笔盈利/亏损：**合成一行**、只给金额、**不显示时间**。
    这两个数本来就是一对（最好的单笔 / 最坏的单笔），合成一行后与
    「平均每笔盈利/亏损」同构。
 ④ 删除「按出场原因」与「曲线口径」两行。
 ⑤ 期望值那一行的**标签**是「期望值」，**数值带「 元/笔」**（2026-09-19 用户
    拍板：= 总净盈亏 ÷ 总交易笔数，除完剩下的单位就是元/笔）。「期望值/笔」
    这种把量纲缀进标签的写法仍然禁用。
 ⑥ 明细行行序：期货品种 → 成交笔数 → 期望值 →
    平均每笔盈利/亏损 → 最大单笔盈利/亏损
 ⑦ 盈利因子提到核心区：一行四格 = 总净盈亏 / 实际胜率 / 盈亏比(赔率) /
    盈利因子；明细区不再重复这一行（同一字段上下各出现一次会让人以为
    是两个不同的指标）。
 ⑧ 所有金额**不带正号**：0 与正数都直接写数字（"+1530.00 元" → "1530.00 元"）。
    只保留负数的 "-"。
 ⑨ 总净盈亏**过万写「万元」、过百万写「百万元」**（2026-09-19 用户拍板）：
    核心区一行四格、每格仅 ~100px 宽，"3400000.00 元" 会把格子撑破。
    边界：|x| < 1 万 → 元；1 万 ≤ |x| < 100 万 → 万元；|x| ≥ 100 万 → 百万元。
    负号保留（-34000 → "-3.40 万元"）。
    其余金额字段（期望值 / 平均每笔 / 最大单笔）**维持「元」**——它们都是
    「每笔」量级，把 5000 元写成 "0.50 万元" 反而读不出数。

两层守护
---------------------------------------------------------------------
 [静态]  从 app.js 抽出 renderTradeStats 区块，验标签序列与禁用串；
         不需要浏览器，任何环境都跑。
 [真渲染] 无头 Chrome 打开真实页面 → 打桩 /api/trader/trades 喂一份
         带合约名 / 带 exit_at / 带 by_reason 的响应 → 点「统计」按钮 →
         读 #stats-content 的真实 innerHTML 复核上面九条；⑨ 另外换四档
         量级（未过万 / 过万 / 过百万 / 负过万）重渲染，逐档核对字面值。
         浏览器不在位时降级 SKIP（静态层已覆盖）。

为什么真渲染层要喂「脏数据」：静态层只能证明源码里没有那段拼接；真渲染层
证明的是**即使后端把合约名、exit_at、by_reason 全都发过来，面板也不会显示**
—— 这才是这九条真正想要的保证。

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
EXPECT_ORDER = ["期货品种", "成交笔数", "期望值",
                "平均每笔盈利/亏损", "最大单笔盈利/亏损"]

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
check(labels.index("期货品种") < labels.index("成交笔数"), "⑥ 期货品种 在 成交笔数 之前")
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

check('class="stats-label">期货品种</span>' in blk, "① 有「期货品种」行")
check(re.search(r'class="stats-label">品种</span>', blk) is None,
      "① 旧标签「品种」无残留（已改「期货品种」）")
check("statsEsc(d.symbol_key)" in blk, "① 品种行取的是 symbol_key（品种键）")
check("d.by_symbol" not in blk, "① 品种行不再读 by_symbol（合约名来源已断）")
check("+ sub +" not in blk, "① 品种行不再拼 sub 后缀（原「（合约 XXX）」）")

check('class="stats-label">平均每笔盈利/亏损</span>' in blk,
      "② 标签为「平均每笔盈利/亏损」（后半截不重复「平均每笔」）")
check("平均盈利 / 平均亏损" not in blk, "② 旧标签「平均盈利 / 平均亏损」无残留")
for stale in ("平均每笔盈利 / 平均每笔亏损", "平均每笔盈利/平均每笔亏损"):
    check(stale not in blk, "② 旧长标签「%s」无残留（注释里也不留）" % stale)

check("exit_at" not in blk, "③ 明细行区块内 exit_at 零命中（最大单笔不带时间）")
check(blk.count("最大单笔盈利/亏损") == 1,
      "③ 最大单笔盈/亏合成一行（不是两行）")
for stale in ("最大单笔盈利 / 最大单笔亏损", "最大单笔盈利/最大单笔亏损"):
    check(stale not in blk, "③ 旧长标签「%s」无残留" % stale)
# 「单独一行」判定要看整条标签，不能拿子串比 —— 合并后的标签里就含有
# 「最大单笔亏损」这几个字（2026-09-19 起后半截缩成「亏损」，这两条
# 正则留作「旧两行写法回潮」的兜底）。
check(re.search(r'class="stats-label">最大单笔盈利(?!\s*/)', blk) is None
      and re.search(r'class="stats-label">最大单笔亏损', blk) is None,
      "③ 旧的两行标签无残留")
check("按出场原因" not in blk, "④ 「按出场原因」已删除")
check("d.by_reason" not in blk, "④ 不再读 by_reason")
check("曲线口径" not in blk and "stats-note" not in blk, "④ 「曲线口径」注释行已删除")

check('class="stats-label">期望值</span>' in blk, "⑤ 「期望值」标签在位")
check("期望值/笔" not in blk, "⑤ 旧标签「期望值/笔」无残留")
check("元/笔" in blk, "⑤ 期望值数值带「 元/笔」（量纲 = 总净盈亏 ÷ 总笔数）")
check("yuanPer(d.expectancy)" in blk, "⑤ 期望值走 yuanPer()（元/笔 专用格式）")
check("yuan(d.expectancy)" not in blk, "⑤ 期望值不再走 yuan()（那样量纲就丢了）")

# ⑨ 总净盈亏缩位：过万→万元、过百万→百万元；其余金额字段维持「元」。
#    缩位实现已从面板里提出来、单独成一节（面板「总净盈亏」与盈亏曲线**纵轴
#    刻度**共用同一份），所以这里从**全仓**抽它，而不是从面板那一段里抽。
check("money(d.total_net)" in blk, "⑨ 核心区「总净盈亏」走 money() 缩位")
mfn = re.search(r"^        function moneyScale\(v\) \{(.*?)\n        \}", js, re.S | re.M)
check(mfn is not None, "⑨ 抽得到 moneyScale() 缩位实现（覆盖面自检）")
if mfn:
    mbody = mfn.group(1)
    check(mbody.index("1e6") < mbody.index("1e4"),
          "⑨ 先判「过百万」再判「过万」（顺序反了 3.4e6 会写成 340.00 万元）")
    for unit in ('"元"', '"万元"', '"百万元"'):
        check(unit in mbody, "⑨ 单位分支 %s 在位" % unit)
check(js.count("function moneyScale(") == 1 and js.count("function money(") == 1,
      "⑨ 缩位规则全仓只有一份（面板与纵轴同源，不是各抄一份）",
      (js.count("function moneyScale("), js.count("function money(")))
check("var money = function" not in js,
      "⑨ 旧的局部 money() 已删除（留着就是第二份实现）")
check("var sc = moneyScale(" in js,
      "⑨ 盈亏曲线纵轴的单位也取自 moneyScale（两处同一个档位函数）")
for keep in ("yuan(d.avg_win)", "yuan(d.avg_loss)",
             "yuan(d.max_win.net_cash)", "yuan(d.max_loss.net_cash)"):
    check(keep in blk, "⑨ 每笔量级字段仍写「元」不缩位：%s" % keep)

# ⑧ 金额一律不带正号：yuan() 里不许再出现任何形式的 '+"'
check('x > 0 ? "+"' not in blk and 'x >= 0 ? "+"' not in blk,
      "⑧ 金额拼接里已无「正数补 +」的分支")
check('Number(x).toFixed(2) + " 元"' in blk,
      "⑧ 金额格式就是「数字 + 空格 + 元」")

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
    # 用一层 list 兜住「当前 fixture」：⑨ 要换三档量级重渲染，路由闭包里得看得见
    cur_fix = [json.dumps(stats_fixture())]

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
            # 截图只服务交付说明，不是门禁的一部分 —— 默认不落盘（跑测试不该在
            # 仓库里留下产物），要图时用 STATS_SHOT_DIR 指定一个仓库外的目录。
            shot_dir = os.environ.get("STATS_SHOT_DIR", "")

            def route_api(route):
                url = route.request.url
                if "/api/health" in url:
                    body = json.dumps({"config": {"view_count": 233}})
                elif "analyze" in url:
                    body = snap_text
                elif "trader/trades" in url:
                    body = cur_fix[0]        # 什么都带的脏数据
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
                mrow = re.search(r'class="stats-label">期货品种</span>.*?'
                                 r'class="stats-value"[^>]*>(.*?)</span>', html, re.S)
                check(mrow is not None, "① 真渲染有「期货品种」行")
                if mrow:
                    val = re.sub(r"<[^>]+>", "", mrow.group(1)).strip()
                    check(val == "RB", "① 品种格 == RB（合约名一个都没带上）", val)
                check(DIRTY_CONTRACT_A not in html and DIRTY_CONTRACT_B not in html,
                      "① 整个面板无合约名（%s / %s）" % (DIRTY_CONTRACT_A, DIRTY_CONTRACT_B))

                check("平均每笔盈利/亏损" in html,
                      "② 真渲染标签为「平均每笔盈利/亏损」")
                check("平均每笔盈利 / 平均每笔亏损" not in html,
                      "② 真渲染：旧长标签没有以任何形式出现")

                check(DIRTY_EXIT_AT_WIN not in html and DIRTY_EXIT_AT_LOSS not in html,
                      "③ 最大单笔那一行不带时间（exit_at 未渲染）")
                # 两个金额各自套着自己的红/绿 span，故不能拿整串去比 ——
                # 剥掉标签看这一格的**文本**。
                mrow2 = re.search(
                    r'class="stats-label">最大单笔盈利/亏损</span>'
                    r'<span class="stats-value">(.*?)</span></div>', html, re.S)
                check(mrow2 is not None, "③ 真渲染有「最大单笔盈利/亏损」行")
                if mrow2:
                    vals = re.sub(r"<[^>]+>", "", mrow2.group(1)).strip()
                    check(vals == "2100.00 元 / -880.00 元",
                          "③ 这一行同时给出盈利与亏损两个金额（无 + 号）", vals)

                check("按出场原因" not in html, "④ 真渲染无「按出场原因」")
                check("曲线口径" not in html, "④ 真渲染无「曲线口径」")

                check("期望值" in html and "期望值/笔" not in html, "⑤ 真渲染为「期望值」")
                check("516.00 元/笔" in html,
                      "⑤ 真渲染：期望值数值带「 元/笔」",
                      re.findall(r"期望值</span>.{0,80}", html))

                # ⑧ 真渲染层面的「无正号」：面板内不应有以 + 开头的文本节点
                check(re.search(r">\+", html) is None,
                      "⑧ 真渲染：没有任何以 + 开头的数值",
                      re.findall(r">\+[^<]{0,12}", html))
                check("+3400.00" not in html and "+516.00" not in html,
                      "⑧ 真渲染：总净盈亏 / 期望值都不带 +")
                check("3400.00 元" in html and "516.00 元/笔" in html,
                      "⑧ 真渲染：金额本体还在（去掉的只是 +）")

                # ⑨ 总净盈亏三档缩位：关面板 → 换 fixture → 再点开（打开时 force 刷新）
                for tag, tot, want in (
                        ("未过万", 3400.0, "3400.00 元"),
                        ("过万", 34000.0, "3.40 万元"),
                        ("过百万", 3400000.0, "3.40 百万元"),
                        ("负过万", -34000.0, "-3.40 万元")):
                    f = stats_fixture()
                    f["total_net"] = tot
                    f["expectancy"] = round(tot / f["count"], 2)
                    cur_fix[0] = json.dumps(f)
                    page.evaluate(
                        "() => { const p = document.getElementById('stats-panel');"
                        " if (p) p.classList.remove('show'); }")
                    page.click("#btn-stats")
                    page.wait_for_timeout(300)
                    cell = page.evaluate(
                        "() => { const c = document.querySelector("
                        "'#stats-content .stats-hero .stats-cell');"
                        " return c ? c.textContent : ''; }") or ""
                    check(want in cell,
                          "⑨ 真渲染 总净盈亏「%s」→ %s" % (tag, want), cell)
                    if shot_dir:
                        os.makedirs(shot_dir, exist_ok=True)
                        page.locator("#stats-panel").screenshot(
                            path=os.path.join(shot_dir, "panel_%s.png" % tag))

            page.unroute_all(behavior="ignore")
            browser.close()
            httpd.shutdown()


print("\n" + "=" * 62)
print("统计面板字段契约 结果: %s" % ("全部通过" if not _FAIL else "%d 项失败 -> %s" % (len(_FAIL), _FAIL)))
print("=" * 62)
sys.exit(1 if _FAIL else 0)

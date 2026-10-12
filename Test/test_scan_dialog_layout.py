# -*- coding: utf-8 -*-
"""
「股票扫描」对话框两处布局契约：扫描来源两行 / 扫描模式两行
====================================================================
需求（2026-10-12，用户指定）：
  ⑴ 扫描来源：三行 → 两行
       第1行：自选股  成分股  全A股
       第2行：板块指数2  板块指数3
  ⑵ 扫描模式：单行 → 两行（2026-10-12 第二轮：第1行尾部追加「RSI」）
       第1行：标注  均线  放量  连涨  RSI
       第2行：底分型  买/卖点  回测

实现（Frontend/app.html）：
  · 来源网格 2 列 → 3 列；并把「全A股」块移到「板块指数2」之前 —— DOM 顺序
    zxg → page_index → all_a → tdxhy2 → tdxhy3，自动落位 = 3+2；
  · 模式容器 display:flex → 5 列网格 —— DOM 顺序不动，自动落位 = 5+3。
    追加 RSI 时实测：4 列会让 RSI 掉到第 2 行行首（第1行只剩 4 项），故必须 5 列。
  （两处各带一行「两行布局」说明注释，本用例把注释也纳入契约防静默回退。）

两层守护
--------------------------------------------------------------------
 [静态]  读 app.html：两组容器的列数、成员顺序与标签文本逐字比对，
         并扫旧写法零残留；不需要浏览器，任何环境都跑。
 [真渲染] 无头 Chrome 打开真实页面（/api/** 打桩）→ 显示对话框 →
         `getBoundingClientRect` 量测每个复选项 label，按 top 分行：
         必须**恰好 2 行**且成员逐字符合；另核单行不换行、不越出对话框
         盒子（防「折行 / 溢出」这类静态正则看不出来的缺陷）。
         浏览器不在位时降级 SKIP（静态层已覆盖）。

跑法：python Test/test_scan_dialog_layout.py（退出码 0/1 即判决）
"""
import functools
import glob
import http.server
import io
import json
import os
import re
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
APPHTML = os.path.join(ROOT, "Frontend", "app.html")
FRONTEND_DIR = os.path.join(ROOT, "Frontend")

_OK = []

# 契约：DOM 顺序即落位顺序（源 = 3 列 / 模式 = 5 列网格）
EXPECT_SOURCES = [("zxg", "自选股"), ("page_index", "成分股"), ("all_a", "全A股"),
                  ("tdxhy2", "板块指数2"), ("tdxhy3", "板块指数3")]
EXPECT_MODES = [("ann", "标注"), ("ma", "均线"), ("fangliang", "放量"),
                ("lianzhang", "连涨"), ("rsi", "RSI"), ("fx_d", "底分型"),
                ("bsp", "买/卖点"), ("backtest", "回测")]


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  —— " + str(detail)[:260]))
    return cond


# ══════════════════════════════════════════════════════════════════════
# ① 静态结构契约（不需要浏览器）
# ══════════════════════════════════════════════════════════════════════
def part_static():
    print("══ ① 静态结构契约（app.html：列数 / 顺序 / 标签 / 注释）══")
    html = io.open(APPHTML, encoding="utf-8").read()

    # 切片锚点自检：锚点漂移时下面全是空转
    check("① 切片锚点唯一且存在",
          html.count('id="scan-mode-dialog"') == 1
          and html.count('id="scan-source-section"') == 1
          and html.count('id="scan-freq-row"') == 1
          and html.count(">扫描模式</div>") == 1,
          "锚点命中数：%d/%d/%d/%d" % (
              html.count('id="scan-mode-dialog"'),
              html.count('id="scan-source-section"'),
              html.count('id="scan-freq-row"'),
              html.count(">扫描模式</div>")))

    i_dlg = html.index('id="scan-mode-dialog"')
    dialog = html[i_dlg:]
    i_btns = dialog.index('class="annotation-dialog-btns"')
    src_sec = dialog[dialog.index('id="scan-source-section"'):dialog.index('id="scan-freq-row"')]
    mode_sec = dialog[dialog.index(">扫描模式</div>"):i_btns]
    check("① 两个区块切片非空（防锚点漂移后护栏空转）",
          len(src_sec) > 800 and len(mode_sec) > 600,
          "len(src)=%d len(mode)=%d" % (len(src_sec), len(mode_sec)))

    def pairs(sec, name):
        """(value, 标签文本) 按 DOM 顺序；文本两侧空白归一。"""
        return [(v, lbl.strip())
                for v, lbl in re.findall(
                    r'name="%s" value="(\w+)"[^>]*>\s*([^<]+?)\s*</label>' % name, sec)]

    # ── ⑴ 扫描来源 ──
    sp = pairs(src_sec, "scan-source")
    check("① 来源 5 项、DOM 顺序 = 自选股→成分股→全A股→板块指数2→板块指数3"
          "（3 列网格 ⇒ 第1行 自选股 成分股 全A股；第2行 板块指数2 板块指数3）",
          sp == EXPECT_SOURCES, sp)
    check("① 来源网格为 3 列",
          re.search(r'display:grid;\s*grid-template-columns:\s*1fr 1fr 1fr;', src_sec)
          is not None, src_sec[:180])
    check("① 旧 2 列来源网格零残留（全文件）",
          'grid-template-columns:1fr 1fr;' not in html,
          "仍存在 2 列声明")
    check("① label-page-index 仍在「成分股」标签上（JS 置灰依赖该 id）",
          re.search(r'<label id="label-page-index"[^>]*>\s*<input[^>]*'
                    r'name="scan-source"\s+value="page_index"', src_sec) is not None,
          "id 与 value 的从属关系未找到")

    # ── ⑵ 扫描模式 ──
    mp = pairs(mode_sec, "scan-mode")
    check("② 模式 8 项、DOM 顺序不变（标注…连涨 RSI 底分型…回测）",
          mp == EXPECT_MODES, mp)
    check("② 模式容器为 5 列网格（自动落位 = 5+3）",
          re.search(r'display:grid;\s*grid-template-columns:\s*1fr 1fr 1fr 1fr 1fr;',
                    mode_sec) is not None, mode_sec[:180])
    check("② 旧 4 列模式网格零残留（全文件；4 列会让 RSI 掉到第 2 行）",
          'grid-template-columns:1fr 1fr 1fr 1fr;' not in html,
          "仍存在 4 列声明")
    check("② 旧 flex 单行写法零残留（全文件）",
          'display:flex;gap:16px;font-size:13px;color:#a8b2d1;margin-bottom:14px;'
          not in html, "仍存在旧 flex 容器")
    check("② 5 列落位 ⇒ 第1行 标注 均线 放量 连涨 RSI",
          [lbl for _, lbl in mp][:5] == ["标注", "均线", "放量", "连涨", "RSI"],
          [lbl for _, lbl in mp][:5])
    check("② 5 列落位 ⇒ 第2行 底分型 买/卖点 回测（共 3 项）",
          [lbl for _, lbl in mp][5:] == ["底分型", "买/卖点", "回测"],
          [lbl for _, lbl in mp][5:])

    # ── 需求留痕（两个布局注释）──
    check("①② 两处「两行布局」说明注释在位（需求留痕，防静默回退）",
          "扫描来源两行布局" in html and "扫描模式两行布局" in html,
          "缺布局说明注释")


# ══════════════════════════════════════════════════════════════════════
# ② 真渲染层（无头 Chrome；浏览器不在位降级 SKIP）
# ══════════════════════════════════════════════════════════════════════
MEASURE = r"""
() => {
  const one = (el) => {
    const r = el.getBoundingClientRect();
    return { text: (el.textContent || '').replace(/\s+/g, ''),
             top: Math.round(r.top), left: Math.round(r.left),
             right: Math.round(r.right), h: Math.round(r.height) };
  };
  const byInput = (name) => Array.from(
      document.querySelectorAll('input[name="' + name + '"]'))
      .map(ip => one(ip.closest('label')));
  const box = document.querySelector('#scan-mode-dialog .annotation-dialog-box');
  const br = box.getBoundingClientRect();
  return {
    sources: byInput('scan-source'),
    modes: byInput('scan-mode'),
    box: { left: Math.round(br.left), right: Math.round(br.right) },
    shown: document.getElementById('scan-mode-dialog').classList.contains('show')
  };
}
"""


def rows_of(items):
    items = sorted(items, key=lambda x: (x["top"], x["left"]))
    rows = []
    for it in items:
        if rows and abs(it["top"] - rows[-1][0]["top"]) <= 3:
            rows[-1].append(it)
        else:
            rows.append([it])
    return [[i["text"] for i in r] for r in rows]


def find_playwright():
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        return sync_playwright, ""
    except Exception as e:  # noqa: BLE001
        return None, str(e).splitlines()[0][:120]


def launch_browser(pw):
    """本机 Chrome 优先：本机「捆绑 chromium」在 browser.close() 时会挂住
    Playwright driver（与 test_stats_panel_labels.py 同一处置）。"""
    notes = []
    try:
        return pw.chromium.launch(channel="chrome"), "本机 Chrome", notes
    except Exception as e:  # noqa: BLE001
        notes.append("channel=chrome: " + str(e).splitlines()[0][:90])
    try:
        return pw.chromium.launch(), "捆绑 chromium", notes
    except Exception as e:  # noqa: BLE001
        notes.append("捆绑 chromium: " + str(e).splitlines()[0][:90])
    for pat in ("~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe",
                "~/AppData/Local/ms-playwright/chromium-*/chrome-win/chrome.exe"):
        for cand in glob.glob(os.path.expanduser(pat)):
            try:
                return (pw.chromium.launch(executable_path=cand),
                        os.path.basename(cand), notes)
            except Exception as e:  # noqa: BLE001
                notes.append(os.path.basename(cand) + ": " + str(e).splitlines()[0][:90])
    return None, "", notes


def part_render():
    print("══ ② 真渲染（无头 Chrome 量测 rect → 按 top 分行）══")
    sync_playwright, why = find_playwright()
    if sync_playwright is None:
        print("  [SKIP] " + why + "（静态层已覆盖契约）")
        return

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
            return
        print("  （无头浏览器：%s）" % which)
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:160]))

        def route_api(route):
            body = (json.dumps({"config": {"view_count": 233}})
                    if "/api/health" in route.request.url else "{}")
            route.fulfill(status=200, content_type="application/json", body=body)

        page.route("**/api/**", route_api)
        page.goto("http://127.0.0.1:%d/app.html" % port, wait_until="load")
        page.wait_for_timeout(800)
        page.evaluate(
            "() => document.getElementById('scan-mode-dialog').classList.add('show')")
        page.wait_for_timeout(300)
        data = page.evaluate(MEASURE)
        shot_dir = os.environ.get("SCAN_DIALOG_SHOT_DIR", "")
        if shot_dir:
            os.makedirs(shot_dir, exist_ok=True)
            page.locator("#scan-mode-dialog .annotation-dialog-box").screenshot(
                path=os.path.join(shot_dir, "scan_dialog.png"))
        page.unroute_all(behavior="ignore")
        browser.close()
    httpd.shutdown()

    if errs:
        print("  （页面非致命 JS 错误 %d 条，不判红：%s）" % (len(errs), errs[:2]))
    check("② 真渲染：对话框已显示（量测前提）", data.get("shown") is True,
          data.get("shown"))

    rs = rows_of(data["sources"])
    rm = rows_of(data["modes"])
    check("① 真渲染：扫描来源**恰好 2 行**（改前为 3 行）", len(rs) == 2, rs)
    check("① 真渲染：第1行 = 自选股 成分股 全A股", rs[:1] == [["自选股", "成分股", "全A股"]], rs)
    check("① 真渲染：第2行 = 板块指数2 板块指数3", rs[1:2] == [["板块指数2", "板块指数3"]], rs)
    check("② 真渲染：扫描模式**恰好 2 行**（改前为 1 行）", len(rm) == 2, rm)
    check("② 真渲染：第1行 = 标注 均线 放量 连涨 RSI（改前 4 项）",
          rm[:1] == [["标注", "均线", "放量", "连涨", "RSI"]], rm)
    check("② 真渲染：第2行 = 底分型 买/卖点 回测",
          rm[1:2] == [["底分型", "买/卖点", "回测"]], rm)

    all_items = data["sources"] + data["modes"]
    check("① 真渲染：5 个来源项全部单行（带 padding 的单行高 29px，折行会到 46+）",
          len(data["sources"]) == 5
          and all(it["h"] <= 34 for it in data["sources"]),
          [(it["text"], it["h"]) for it in data["sources"] if it["h"] > 34])
    check("② 真渲染：8 个模式项全部单行（无 padding 的单行高 17px，折行会到 34+）",
          len(data["modes"]) == 8
          and all(it["h"] <= 26 for it in data["modes"]),
          [(it["text"], it["h"]) for it in data["modes"] if it["h"] > 26])
    box = data["box"]
    check("② 真渲染：13 个复选项全部不越出对话框盒子",
          all(box["left"] <= it["left"] and it["right"] <= box["right"]
              for it in all_items),
          [(it["text"], it["left"], it["right"], box) for it in all_items
           if not (box["left"] <= it["left"] and it["right"] <= box["right"])])


def main():
    print("=" * 68)
    print("扫描对话框两行布局契约（来源 3+2 / 模式 5+3）")
    print("=" * 68)
    part_static()
    part_render()
    print("-" * 68)
    total, passed = len(_OK), sum(_OK)
    print("合计 %d 项，通过 %d，失败 %d" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

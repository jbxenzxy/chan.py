# -*- coding: utf-8 -*-
"""把「自动下单」控件的 3 种对齐方案**真实渲染**成一张对比图，供用户挑选。

不用手画 —— 直接复用项目真实 app.css + index.html 的真实 DOM 片段，
所以像素与线上一致。徽标尺寸在页面里实时量出来，再喂给各方案，保证"等宽等高"是精确的。

用法：
  python variants.py <前端目录> <输出目录>
"""
import json
import os
import re
import shutil
import subprocess
import sys

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

INJECT = r"""
<script>
(function () {
  var badge = document.getElementById('realtime-badge');
  var wrap = document.getElementById('auto-order-wrap');
  badge.classList.add('visible');
  wrap.classList.add('visible');

  // ★ 关键：把真实 .header 藏掉。否则窗口不够宽时 header-left/right 互相挤压，
  //   徽标会被压成折行的方块（实测 35.75x49 而非 47.69x19），量出来的"参照尺寸"就是错的。
  document.querySelector('.header').style.display = 'none';

  var badgeHTML = badge.outerHTML;
  var wrapHTML = wrap.outerHTML;

  // ★ 固定在视口顶部：页面里 #chart-container 高 = calc(100vh - 80px)，
  //   普通文档流里的行会被顶到首屏之外，截图就成了全黑。
  var VBOX = document.createElement('div');
  VBOX.style.cssText = 'position:fixed;top:0;left:0;width:100%;z-index:99999;' +
    'background:#0f1628;';
  document.body.appendChild(VBOX);

  function stripIds(root) {
    var all = root.querySelectorAll('[id]');
    for (var i = 0; i < all.length; i++) all[i].removeAttribute('id');
    return root;
  }

  function cloneBadge() {
    var d = document.createElement('span');
    d.innerHTML = badgeHTML;
    return stripIds(d.firstElementChild);
  }

  function mkRow(id, caption) {
    var row = document.createElement('div');
    row.className = 'vrow ' + id;
    // ★ 全部内联，不依赖 <style> 块（body 里的 style 曾整块不生效，导致行退化成块级、
    //   克隆体被拉满整行宽度，量出来的"参照尺寸"变成 1888 宽）
    row.style.cssText = 'display:flex;align-items:center;gap:18px;' +
      'padding:10px 20px;border-bottom:1px solid #1d2740;background:#0f1628;';
    var cap = document.createElement('span');
    cap.textContent = caption;
    cap.style.cssText = 'width:170px;flex:none;color:#8892b0;font-size:12px;' +
      'font-family:Consolas,monospace;';
    row.appendChild(cap);
    return row;
  }

  function ensureVisible(el) {
    el.style.display = 'flex';
    return el;
  }

  function makeRow(id, caption, withBadge, apply) {
    var row = mkRow(id, caption);
    if (withBadge) row.appendChild(ensureVisible(cloneBadge()));
    var holder = document.createElement('span');
    holder.innerHTML = wrapHTML;
    row.appendChild(ensureVisible(stripIds(holder.firstElementChild)));
    VBOX.appendChild(row);
    if (apply) apply(row);
    return row;
  }

  // 参照行：单独放一枚徽标（不受挤压），用它的尺寸当"目标尺寸"
  var refRow = mkRow('vR', '参照 · 实时徽标');
  var refBadge = ensureVisible(cloneBadge());
  refRow.appendChild(refBadge);
  VBOX.appendChild(refRow);
  var br = refBadge.getBoundingClientRect();
  var BW = +br.width.toFixed(2), BH = +br.height.toFixed(2);

  makeRow('v0', '现状（对照）', true, null);

  // A：只统一高度（控件 20 → 与徽标一致），宽度不动
  makeRow('vA', 'A 只统一高度', true, function (row) {
    row.querySelector('.auto-order-wrap').style.height = BH + 'px';
  });

  // B：开关本体改成与徽标同尺寸，其余不动
  makeRow('vB', 'B 开关本体等尺寸', true, function (row) {
    var sw = row.querySelector('.auto-order-switch');
    sw.style.width = BW + 'px';
    sw.style.height = BH + 'px';
    var sl = row.querySelector('.auto-order-slider');
    sl.style.width = BW + 'px';
    sl.style.height = BH + 'px';
    sl.style.borderRadius = (BH / 2) + 'px';
    var knob = BH - 6;
    var st = document.createElement('style');
    st.textContent =
      '.vB .auto-order-slider::before{width:' + knob + 'px;height:' + knob +
      'px;left:3px;top:3px;}' +
      '.vB .auto-order-switch input:checked + .auto-order-slider::before{transform:translateX(' +
      (BW - 6 - knob) + 'px);}';
    document.head.appendChild(st);
  });

  // C：整块做成与徽标等宽等高的胶囊（文字缩为「下单」，整块可点）
  makeRow('vC', 'C 整块等尺寸胶囊', true, function (row) {
    var w = row.querySelector('.auto-order-wrap');
    w.style.width = BW + 'px';
    w.style.height = BH + 'px';
    w.style.padding = '2px 8px';
    w.style.borderRadius = (BH / 2) + 'px';
    w.style.background = '#e94560';
    w.style.color = '#fff';
    w.style.fontWeight = 'bold';
    w.style.justifyContent = 'center';
    w.style.cursor = 'pointer';
    w.style.marginLeft = '0';
    row.querySelector('.auto-order-dot').style.display = 'none';
    var lab = row.querySelector('.auto-order-label');
    lab.textContent = '下单';                 // 4 字塞不进 47.69-16=31.69px，必须缩短
    lab.style.color = '#fff';
    row.querySelector('.auto-order-switch').style.display = 'none';
  });

  var out = {badge: {w: BW, h: BH}, rows: {}};
  ['v0', 'vA', 'vB', 'vC'].forEach(function (id) {
    var row = document.querySelector('.' + id);
    var w = row.querySelector('.auto-order-wrap');
    var sw = row.querySelector('.auto-order-switch');
    function m(el) { if (!el) return null; var r = el.getBoundingClientRect();
      return [+r.width.toFixed(2), +r.height.toFixed(2)]; }
    out.rows[id] = {wrap: m(w), sw: m(sw), swDisplay: getComputedStyle(sw).display,
                    rowDisplay: getComputedStyle(row).display};
  });
  var pre = document.createElement('pre');
  pre.id = '__PROBE__';
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>
"""

EXTRA_CSS = """
<style>
  body { margin: 0; background: #0f1628; }
  .vrow { display: flex; align-items: center; gap: 18px;
          padding: 10px 20px; border-bottom: 1px solid #1d2740; }
  .vcap { width: 150px; flex: none; color: #8892b0; font-size: 12px;
          font-family: Consolas, monospace; }
  .vrow .auto-order-wrap { display: flex !important; }
  .vrow .realtime-badge { display: flex !important; }
  .hdr-note { color: #5a6172; font-size: 11px; padding: 4px 20px 0;
              font-family: Consolas, monospace; }
</style>
"""


def main():
    src_dir, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    html = open(os.path.join(src_dir, "index.html"), encoding="utf-8").read()
    html2 = re.sub(r'<script src="app\.js[^"]*"></script>', INJECT + EXTRA_CSS, html)
    assert '__PROBE__' in html2, "注入失败"
    page = os.path.join(out_dir, "variants.html")
    open(page, "w", encoding="utf-8").write(html2)
    shutil.copy2(os.path.join(src_dir, "app.css"), os.path.join(out_dir, "app.css"))

    url = "file:///" + page.replace("\\", "/")
    prof = os.path.join(out_dir, "_edge_profile")
    common = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
              "--no-default-browser-check", "--hide-scrollbars",
              "--user-data-dir=" + prof, "--virtual-time-budget=5000",
              "--window-size=1920,600"]

    p = subprocess.run(common + ["--dump-dom", url], capture_output=True, timeout=180)
    dom = (p.stdout or b"").decode("utf-8", "replace")
    m = re.search(r'<pre id="__PROBE__">(.*?)</pre>', dom, re.S)
    data = json.loads(m.group(1)) if m and m.group(1).strip() else None

    png = os.path.join(out_dir, "variants.png")
    subprocess.run(common + ["--force-device-scale-factor=3",
                             "--screenshot=" + png, url],
                   capture_output=True, timeout=180)

    print("徽标 realtime-badge 实测 = {} x {}".format(
        data["badge"]["w"], data["badge"]["h"]) if data else "（未拿到量测）")
    if data:
        for k, v in data["rows"].items():
            print("  {:<4s} wrap={:<22} switch={:<20} display={} row={}".format(
                k, str(v["wrap"]), str(v["sw"]), v["swDisplay"], v["rowDisplay"]))
    print("-> {}".format(png))


if __name__ == "__main__":
    main()

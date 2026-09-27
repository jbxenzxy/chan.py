# -*- coding: utf-8 -*-
"""无头 Edge 探针：量出 index.html 里目标元素的真实盒模型 + 截头部区域图。

用法：
  python probe.py <页面源目录> <输出目录> [标签]
页面源目录里需有 index.html + app.css。
"""
import json
import os
import re
import shutil
import subprocess
import sys

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

MEASURE_JS = r"""
<script>
(function () {
  function run() {
    var rb = document.getElementById('realtime-badge');
    var aw = document.getElementById('auto-order-wrap');
    if (rb) rb.classList.add('visible');
    if (aw) aw.classList.add('visible');
    var out = {__diag: {}};
    out.__diag.dpr = window.devicePixelRatio;
    out.__diag.clientW = document.documentElement.clientWidth;
    out.__diag.clientH = document.documentElement.clientHeight;
    out.__diag.scrollY = window.scrollY;
    var bcs = getComputedStyle(document.body);
    out.__diag.bodyZoom = bcs.zoom;
    out.__diag.bodyTransform = bcs.transform;
    out.__diag.bodyFont = bcs.fontSize + ' / ' + bcs.lineHeight;
    out.__diag.bodyMargin = bcs.margin;
    function box(name, el) {
      if (!el) { out[name] = null; return; }
      var r = el.getBoundingClientRect();
      var cs = getComputedStyle(el);
      out[name] = {
        w: +r.width.toFixed(2), h: +r.height.toFixed(2),
        left: +r.left.toFixed(2), top: +r.top.toFixed(2),
        right: +r.right.toFixed(2), bottom: +r.bottom.toFixed(2),
        pad: cs.padding, fontSize: cs.fontSize, lineHeight: cs.lineHeight,
        fontFamily: cs.fontFamily, radius: cs.borderRadius,
        display: cs.display, gap: cs.gap, flex: cs.flex,
        zoom: cs.zoom, transform: cs.transform,
        minW: cs.minWidth, minH: cs.minHeight
      };
    }
    box('realtimeBadge', rb);
    box('autoOrderWrap', aw);
    box('autoOrderDot', document.getElementById('auto-order-dot'));
    box('autoOrderLabel', document.getElementById('auto-order-label'));
    box('autoOrderSwitch', document.querySelector('.auto-order-switch'));
    box('autoOrderSlider', document.querySelector('.auto-order-slider'));
    box('headerLeft', document.querySelector('.header-left'));
    box('headerRight', document.querySelector('.header-right'));
    box('header', document.querySelector('.header'));
    box('freqSelector', document.getElementById('freq-selector'));
    box('btnDual', document.getElementById('btn-dual'));
    box('stockInput', document.querySelector('.stock-input'));
    box('stockName', document.getElementById('stock-name'));
    box('stockCode', document.getElementById('stock-code'));
    // 开关旋钮（伪元素）：关 / 开 两种状态各量一次，验证左右边距是否对称。
    // ⚠️ 必须临时关掉 transition —— 否则同步读到的是过渡**起点**的值（永远 matrix(...,0,0)）。
    var st = document.createElement('style');
    st.textContent = '.auto-order-slider, .auto-order-slider::before ' +
                     '{ transition: none !important; }';
    document.head.appendChild(st);
    function knob(state) {
      var cb = document.getElementById('auto-order-checkbox');
      var sl = document.querySelector('.auto-order-slider');
      cb.checked = state;
      var cs = getComputedStyle(sl, '::before');
      var slr = sl.getBoundingClientRect();
      return {knob: cs.width + ' x ' + cs.height, left: cs.left, top: cs.top,
              translateX: cs.transform, sliderW: +slr.width.toFixed(2),
              sliderH: +slr.height.toFixed(2)};
    }
    out.__knobOff = knob(false);
    out.__knobOn = knob(true);
    document.getElementById('auto-order-checkbox').checked = false;
    // 逐个列出 header-left / header-right 的直接子元素（判断是否被挤压）
    out.__leftKids = [];
    var lk = document.querySelector('.header-left');
    if (lk) {
      for (var i = 0; i < lk.children.length; i++) {
        var c = lk.children[i], r = c.getBoundingClientRect();
        out.__leftKids.push([c.className || c.id || c.tagName,
                             +r.width.toFixed(2), +r.height.toFixed(2)]);
      }
    }
    out.__rightKids = [];
    var rk = document.querySelector('.header-right');
    if (rk) {
      for (var j = 0; j < rk.children.length; j++) {
        var c2 = rk.children[j], r2 = c2.getBoundingClientRect();
        out.__rightKids.push([c2.className || c2.id || c2.tagName,
                              +r2.width.toFixed(2), +r2.height.toFixed(2)]);
      }
    }
    var pre = document.createElement('pre');
    pre.id = '__PROBE__';
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  run();   // ★ 同步跑：脚本在 body 末尾，元素已就绪；getBoundingClientRect 会强制布局
})();
</script>
"""


def build(src_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    html = open(os.path.join(src_dir, "index.html"), encoding="utf-8").read()
    # 去掉真实 app.js（会去连后端并报错），换成量测脚本
    html2 = re.sub(r'<script src="app\.js[^"]*"></script>', MEASURE_JS, html)
    assert '__PROBE__' in html2, "注入失败：没找到 app.js 的 script 标签"
    open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8").write(html2)
    shutil.copy2(os.path.join(src_dir, "app.css"), os.path.join(out_dir, "app.css"))
    return os.path.join(out_dir, "index.html")


def run_edge(page, out_dir, tag, width=1920, height=140):
    url = "file:///" + page.replace("\\", "/")
    prof = os.path.join(out_dir, "_edge_profile_" + tag)
    common = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
              "--no-default-browser-check", "--hide-scrollbars",
              "--user-data-dir=" + prof, "--virtual-time-budget=4000",
              "--window-size={},{}".format(width, height)]

    # ① 量测：dump-dom
    p = subprocess.run(common + ["--dump-dom", url], capture_output=True,
                       timeout=180)
    dom = (p.stdout or b"").decode("utf-8", "replace")
    # ⚠️ 不能直接找 PROBE_JSON_BEGIN —— 注入的 <script> 源码里也有这个字面量，
    #    会先匹配到脚本本身。必须锚在 <pre id="__PROBE__"> 上。
    m = re.search(r'<pre id="__PROBE__">(.*?)</pre>', dom, re.S)
    if not m:
        print("  ⚠️ dump-dom 未拿到量测（rc={})".format(p.returncode))
        err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-6:]
        for l in err:
            print("      ! " + l)
        return None
    data = json.loads(m.group(1))

    # ② 截图：4 倍缩放，便于看清像素
    png = os.path.join(out_dir, "header_{}.png".format(tag))
    p2 = subprocess.run(common + ["--force-device-scale-factor=4",
                                  "--screenshot=" + png, url],
                        capture_output=True, timeout=180)
    if not os.path.exists(png):
        print("  ⚠️ 截图失败（rc={}）".format(p2.returncode))
    return data, png


def main():
    src_dir, out_dir = sys.argv[1], sys.argv[2]
    tag = sys.argv[3] if len(sys.argv) > 3 else "x"
    width = int(os.environ.get("WINW", "1920"))
    page = build(src_dir, out_dir)
    res = run_edge(page, out_dir, tag, width=width)
    if not res:
        sys.exit(1)
    data, png = res
    d = data.get("__diag", {})
    print("=== 诊断（窗口宽 {}）===".format(width))
    print("  dpr={} clientW={} clientH={} scrollY={} bodyZoom={} bodyTransform={}".format(
        d.get("dpr"), d.get("clientW"), d.get("clientH"), d.get("scrollY"),
        d.get("bodyZoom"), d.get("bodyTransform")))
    print("=== 量测（CSS px）===")
    print("  {:<16s} {:>8s} {:>8s} {:>8s} {:>7s}  {:<12s} {:<6s}".format(
        "元素", "宽", "高", "left", "top", "padding", "font"))
    for k in ("realtimeBadge", "autoOrderWrap", "autoOrderDot", "autoOrderLabel",
              "autoOrderSwitch", "autoOrderSlider", "headerLeft", "headerRight",
              "header", "freqSelector", "btnDual", "stockInput", "stockName",
              "stockCode"):
        v = data.get(k)
        if not v:
            print("  {:<16s} (不存在)".format(k))
            continue
        print("  {:<16s} {:>8.2f} {:>8.2f} {:>8.2f} {:>7.2f}  {:<12s} {:<6s}".format(
            k, v["w"], v["h"], v["left"], v["top"], v["pad"], v["fontSize"]))
    print("=== .header-left 子元素 ===")
    for name, w, h in data.get("__leftKids", []):
        print("  {:<20s} {:>8.2f} x {:.2f}".format(name, w, h))
    ko, kn = data.get("__knobOff") or {}, data.get("__knobOn") or {}
    print("=== 开关旋钮（伪元素）===")
    for state, k in (("关", ko), ("开", kn)):
        print("  {} 旋钮 {}  left={} top={} transform={}  滑块={}x{}".format(
            state, k.get("knob"), k.get("left"), k.get("top"),
            k.get("translateX"), k.get("sliderW"), k.get("sliderH")))
    rb, aw = data.get("realtimeBadge") or {}, data.get("autoOrderWrap") or {}
    if rb and aw:
        print("=== 对齐判定 ===")
        print("  徽标高 {}  控件高 {}  →  {} ".format(
            rb["h"], aw["h"], "✓ 同高" if rb["h"] == aw["h"] else "✗ 不等高"))
        sw = data.get("autoOrderSwitch") or {}
        if sw:
            print("  开关本体 {} x {}  （徽标 {} x {}）".format(
                sw["w"], sw["h"], rb["w"], rb["h"]))
    print("=== .header-right 子元素 ===")
    for name, w, h in data.get("__rightKids", []):
        print("  {:<20s} {:>8.2f} x {:.2f}".format(name, w, h))
    out_json = os.path.join(out_dir, "measure_{}.json".format(tag))
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("  -> {} / {}".format(os.path.basename(out_json),
                                os.path.basename(png) if png else "(无图)"))


if __name__ == "__main__":
    main()

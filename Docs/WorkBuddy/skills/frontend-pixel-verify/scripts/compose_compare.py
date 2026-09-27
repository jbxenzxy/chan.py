# -*- coding: utf-8 -*-
"""把 probe.py 产出的 before/after 头部截图拼成一张对比图（带中文标注）。

用法： python compose_compare.py <probe_fe 目录> <输出 png>
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

S = sys.argv[1] if len(sys.argv) > 1 else "."
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(S, "compare.png")

K = 4                      # probe.py 里用的 --force-device-scale-factor
X0, X1 = 725, 945          # 关注的 CSS x 区间（徽标 + 自动下单整块）
Y0, Y1 = 4, 44             # 关注的 CSS y 区间（头部条内）

# 参考线用的 CSS y（来自 probe 量测）：徽标 top=14 / bottom=33；
# 自动下单整块 bottom：改前 33.5、改后 33.0
BADGE_TOP, BADGE_BOT = 14.0, 33.0
WRAP_BOT = {"before": 33.5, "after": 33.0}
C_REF = (90, 220, 255)     # 青：徽标上下沿（= 目标高度）
C_WRAP = (255, 170, 70)    # 橙：自动下单整块下沿

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def crop(tag, name):
    src = os.path.join(S, tag, name)
    im = Image.open(src)
    return im.crop((X0 * K, Y0 * K, X1 * K, Y1 * K)).convert("RGB")


def marked(tag, name):
    """裁出关注区，并叠参考线：青 = 徽标上下沿（目标高度），橙 = 整块下沿。"""
    im = crop(tag, name)
    d = ImageDraw.Draw(im)
    w = im.size[0]
    for cy in (BADGE_TOP, BADGE_BOT):
        y = int((cy - Y0) * K)
        for x in range(0, w, 12):          # 虚线：青
            d.line([(x, y), (min(x + 7, w - 1), y)], fill=C_REF, width=1)
    wy = int((WRAP_BOT[tag] - Y0) * K)
    for x in range(0, w, 12):              # 虚线：橙
        d.line([(x, wy), (min(x + 7, w - 1), wy)], fill=C_WRAP, width=2)
    return im


rows = [
    ("改前（你的项目现状）", marked("before", "header_before.png"),
     ["徽标 47.69 x 19   自动下单整块 106 x 20",
      "未对齐：差 1px（橙线在青线下方）"], (255, 140, 140)),
    ("改后（A 只统一高度）", marked("after", "header_after.png"),
     ["徽标 47.69 x 19   自动下单整块 106 x 19",
      "已对齐：同高（橙线与青线重合）"], (140, 230, 160)),
]

cw = (X1 - X0) * K
ch = (Y1 - Y0) * K
F = load_font(28)
F2 = load_font(21)

pad = 16
label_w = 400
canvas_w = label_w + cw + pad * 2
row_h = ch + 62
canvas_h = pad + len(rows) * row_h + 44
canvas = Image.new("RGB", (canvas_w, canvas_h), (15, 22, 40))
d = ImageDraw.Draw(canvas)

y = pad
for title, img, notes, color in rows:
    d.text((pad, y + 4), title, font=F, fill=(226, 226, 226))
    for i, n in enumerate(notes):
        d.text((pad, y + 40 + i * 26), n, font=F2, fill=color)
    canvas.paste(img, (label_w + pad, y))
    d.rectangle([label_w + pad - 1, y - 1, label_w + pad + cw, y + ch],
                outline=(40, 55, 90))
    y += row_h

d.text((pad, canvas_h - 36),
       "青线 = 实时徽标上下沿（目标高度 19px）    橙线 = 自动下单整块下沿",
       font=F2, fill=(150, 165, 200))

canvas.save(OUT)
print("-> {}".format(OUT))
print("   {} x {}".format(*canvas.size))

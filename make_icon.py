"""Génère icon.icns : un feutre qui trace une ligne cursive à boucles sur une feuille quadrillée."""
from PIL import Image, ImageDraw, ImageFilter
import math, subprocess, os, shutil

S = 1024            # taille finale
K = 2               # sur-échantillonnage (anti-crénelage)
W = S * K


def px(v):
    return int(round(v * K))


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


# ── fond : dégradé bleu nuit → bleu roi, dans un carré arrondi ──
bg = Image.new("RGBA", (W, W))
bp = bg.load()
c0, c1 = (46, 98, 226), (14, 28, 88)
for y in range(W):
    for x in range(W):
        t = (0.55 * x + 0.9 * y) / (1.45 * W)
        bp[x, y] = lerp(c0, c1, min(1.0, t)) + (255,)
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle((px(64), px(64), px(S - 64), px(S - 64)), radius=px(205), fill=255)
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
img.paste(bg, (0, 0), mask)

# ── feuille : blanc, légèrement inclinée, quadrillée, avec ombre ──
pw, ph = px(610), px(750)
paper = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
pd = ImageDraw.Draw(paper)
pd.rounded_rectangle((0, 0, pw - 1, ph - 1), radius=px(26), fill=(253, 252, 249, 255))
grid = (206, 220, 242, 255)
step = px(38)
for gx in range(step, pw, step):
    pd.line((gx, px(14), gx, ph - px(14)), fill=grid, width=max(1, K))
for gy in range(step, ph, step):
    pd.line((px(14), gy, pw - px(14), gy), fill=grid, width=max(1, K))
# repère de zéro (coin haut-gauche), comme sur la machine
zr = px(15)
pd.ellipse((px(34) - zr, px(34) - zr, px(34) + zr, px(34) + zr), fill=(238, 120, 40, 255))

# ── trait à boucles (trochoïde) ──
ink = (22, 62, 205, 255)
pts = []
a, b = 24.0, 60.0
t_max = 2.5 * 2 * math.pi
n = 520
x_off, y_off = 70, 500
for i in range(n + 1):
    t = t_max * i / n
    x = a * t - b * math.sin(t)
    y = -b * math.cos(t) * 0.95
    pts.append((px(x_off + x), px(y_off + y)))
width = px(17)
for p0, p1 in zip(pts, pts[1:]):
    pd.line((p0, p1), fill=ink, width=width)
for p in pts[::2]:
    pd.ellipse((p[0] - width // 2, p[1] - width // 2, p[0] + width // 2, p[1] + width // 2), fill=ink)
end = pts[-1]

angle_paper = -6
rot = paper.rotate(angle_paper, resample=Image.BICUBIC, expand=True)
ox, oy = (W - rot.width) // 2 - px(34), (W - rot.height) // 2 + px(18)

shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
sh = Image.new("RGBA", rot.size, (0, 0, 0, 0))
sh.putalpha(rot.split()[3].point(lambda v: int(v * 0.45)))
shadow.paste(sh, (ox + px(10), oy + px(22)), sh)
shadow = shadow.filter(ImageFilter.GaussianBlur(px(22)))
img = Image.alpha_composite(img, Image.composite(shadow, Image.new("RGBA", (W, W), (0, 0, 0, 0)), mask))
layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
layer.paste(rot, (ox, oy), rot)
img = Image.alpha_composite(img, Image.composite(layer, Image.new("RGBA", (W, W), (0, 0, 0, 0)), mask))

# position de la pointe : fin du trait, dans le repère de l'icône
cx0, cy0 = paper.width / 2, paper.height / 2
dx, dy = end[0] - cx0, end[1] - cy0
ca, sa = math.cos(math.radians(-angle_paper)), math.sin(math.radians(-angle_paper))
tip = (ox + rot.width / 2 + dx * ca - dy * sa, oy + rot.height / 2 + dx * sa + dy * ca)

# ── feutre : pointe à gauche, corps à droite, puis rotation ──
L, Wd = px(420), px(88)
pen = Image.new("RGBA", (W, W), (0, 0, 0, 0))
pp = ImageDraw.Draw(pen)
ox0, oy0 = W // 2, W // 2                       # la pointe est au centre du calque
tipl = px(120)
# pointe (cône sombre) + embout encré
pp.polygon([(ox0, oy0), (ox0 + tipl, oy0 - Wd * 0.30), (ox0 + tipl, oy0 + Wd * 0.30)], fill=(38, 40, 52, 255))
pp.polygon([(ox0, oy0), (ox0 + px(46), oy0 - Wd * 0.12), (ox0 + px(46), oy0 + Wd * 0.12)], fill=ink)
# collet
pp.rounded_rectangle((ox0 + tipl - px(8), oy0 - Wd * 0.36, ox0 + tipl + px(58), oy0 + Wd * 0.36), radius=px(12), fill=(226, 228, 236, 255))
# corps
bx0 = ox0 + tipl + px(40)
pp.rounded_rectangle((bx0, oy0 - Wd / 2, ox0 + L, oy0 + Wd / 2), radius=px(40), fill=(255, 122, 69, 255))
# reflet et ombre du corps
pp.rounded_rectangle((bx0 + px(14), oy0 - Wd / 2 + px(10), ox0 + L - px(18), oy0 - Wd / 2 + px(26)), radius=px(8), fill=(255, 176, 138, 255))
pp.rounded_rectangle((bx0 + px(14), oy0 + Wd / 2 - px(24), ox0 + L - px(18), oy0 + Wd / 2 - px(10)), radius=px(8), fill=(222, 92, 44, 255))
# bague blanche et capuchon
pp.rectangle((bx0 + px(120), oy0 - Wd / 2, bx0 + px(150), oy0 + Wd / 2), fill=(255, 250, 244, 255))
pp.rounded_rectangle((ox0 + L - px(130), oy0 - Wd / 2 - px(6), ox0 + L + px(6), oy0 + Wd / 2 + px(6)), radius=px(34), fill=(30, 34, 52, 255))
pp.rounded_rectangle((ox0 + L - px(118), oy0 - Wd / 2 + px(6), ox0 + L - px(20), oy0 - Wd / 2 + px(20)), radius=px(7), fill=(88, 96, 130, 255))
pen = pen.rotate(46, resample=Image.BICUBIC, center=(ox0, oy0))
poff = (int(tip[0] - ox0), int(tip[1] - oy0))
pshadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ps = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ps.putalpha(pen.split()[3].point(lambda v: int(v * 0.42)))
pshadow.paste(ps, (poff[0] + px(10), poff[1] + px(16)), ps)
pshadow = pshadow.filter(ImageFilter.GaussianBlur(px(12)))
img = Image.alpha_composite(img, Image.composite(pshadow, Image.new("RGBA", (W, W), (0, 0, 0, 0)), mask))
pl = Image.new("RGBA", (W, W), (0, 0, 0, 0))
pl.paste(pen, poff, pen)
img = Image.alpha_composite(img, Image.composite(pl, Image.new("RGBA", (W, W), (0, 0, 0, 0)), mask))

img = img.resize((S, S), Image.LANCZOS)

iset = "icon.iconset"
shutil.rmtree(iset, ignore_errors=True)
os.mkdir(iset)
for sz in (16, 32, 128, 256, 512):
    img.resize((sz, sz), Image.LANCZOS).save(f"{iset}/icon_{sz}x{sz}.png")
    img.resize((sz * 2, sz * 2), Image.LANCZOS).save(f"{iset}/icon_{sz}x{sz}@2x.png")
subprocess.run(["iconutil", "-c", "icns", iset, "-o", "icon.icns"], check=True)
shutil.rmtree(iset)
img.save("icon_preview.png")

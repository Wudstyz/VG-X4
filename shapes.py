"""VG-X4 : bibliothèque de formes et de dessins prédéfinis (générateurs paramétriques).

Chaque générateur retourne une liste de polylignes en mm (y vers le bas), qui sont ensuite centrées.
"""
import math

import vec


# ───────────────────────── géométrie de base ─────────────────────────

def ell(cx, cy, rx, ry, a0=0.0, a1=360.0, n=None, rot=0.0):
    """Points d'un arc d'ellipse (angles en degrés, 0 = droite, sens horaire à l'écran)."""
    n = n or max(8, int(abs(a1 - a0) / 5))
    cr, sr = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        x, y = rx * math.cos(a), ry * math.sin(a)
        pts.append((cx + x * cr - y * sr, cy + x * sr + y * cr))
    return pts


def rrect(w, h, r, cx=0.0, cy=0.0):
    """Rectangle à coins arrondis (polyligne fermée)."""
    r = max(0.0, min(r, w / 2, h / 2))
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    if r < 0.05:
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    pts = []
    for (ccx, ccy, a0) in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        pts += ell(ccx, ccy, r, r, a0, a0 + 90, 10)
    pts.append(pts[0])
    return pts


def clip_line(p0, p1, x0, y0, x1, y1):
    """Liang-Barsky : segment p0-p1 découpé dans le rectangle ; None si extérieur."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, p0[0] - x0), (dx, x1 - p0[0]), (-dy, p0[1] - y0), (dy, y1 - p0[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                if t > t1: return None
                t0 = max(t0, t)
            else:
                if t < t0: return None
                t1 = min(t1, t)
    return [(p0[0] + t0 * dx, p0[1] + t0 * dy), (p0[0] + t1 * dx, p0[1] + t1 * dy)]


def union_outline(circles, y_max=None, n=90):
    """Contour extérieur d'une réunion de cercles (cx, cy, r). y_max : on coupe tout ce qui est plus bas."""
    runs = []
    for i, (cx, cy, r) in enumerate(circles):
        cur = []
        for k in range(n + 1):
            a = 2 * math.pi * k / n
            x, y = cx + r * math.cos(a), cy + r * math.sin(a)
            inside = any(j != i and math.hypot(x - c2[0], y - c2[1]) < c2[2] - 1e-6 for j, c2 in enumerate(circles))
            if inside or (y_max is not None and y > y_max + 1e-6):
                if len(cur) > 1: runs.append(cur)
                cur = []
            else:
                cur.append((x, y))
        if len(cur) > 1: runs.append(cur)
    return runs


def poly(n, d, rot=-90.0):
    r = d / 2
    pts = [(r * math.cos(math.radians(rot + 360.0 * i / n)), r * math.sin(math.radians(rot + 360.0 * i / n))) for i in range(n)]
    return pts + [pts[0]]


# ───────────────────────── générateurs ─────────────────────────

def g_line(length=60):
    return [[(-length / 2, 0), (length / 2, 0)]]


def g_rect(w=60, h=40, r=0):
    return [rrect(w, h, r)]


def g_ellipse(w=50, h=50):
    return [ell(0, 0, w / 2, h / 2, 0, 360, 96)]


def g_polygon(n=6, d=50):
    return [poly(int(max(3, n)), d)]


def g_star(n=5, d=50, inner=0.4):
    n = int(max(3, n)); r = d / 2; pts = []
    for i in range(2 * n):
        rr = r if i % 2 == 0 else r * inner
        a = math.radians(-90 + 180.0 * i / n)
        pts.append((rr * math.cos(a), rr * math.sin(a)))
    return [pts + [pts[0]]]


def g_diamond(w=40, h=60):
    return [[(0, -h / 2), (w / 2, 0), (0, h / 2), (-w / 2, 0), (0, -h / 2)]]


def g_heart(w=50):
    pts = []
    for i in range(121):
        t = 2 * math.pi * i / 120
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((x * w / 32.0, y * w / 32.0))
    return [pts]


def g_cross(size=30):
    return [[(-size / 2, 0), (size / 2, 0)], [(0, -size / 2), (0, size / 2)]]


def g_arrow(length=70, head=16):
    return [[(-length / 2, 0), (length / 2, 0)],
            [(length / 2 - head, -head * 0.6), (length / 2, 0), (length / 2 - head, head * 0.6)]]


def g_spiral(turns=4, d=60):
    turns = max(0.5, turns); n = int(turns * 48); pts = []
    for i in range(n + 1):
        a = 2 * math.pi * turns * i / n
        r = (d / 2) * i / n
        pts.append((r * math.cos(a), r * math.sin(a)))
    return [pts]


def g_wave(length=100, amp=8, periods=5):
    n = int(max(8, periods * 24))
    return [[(-length / 2 + length * i / n, amp * math.sin(2 * math.pi * periods * i / n)) for i in range(n + 1)]]


def g_zigzag(length=100, amp=8, n=8):
    n = int(max(1, n)); pts = []
    for i in range(2 * n + 1):
        pts.append((-length / 2 + length * i / (2 * n), (-amp if i % 2 == 0 else amp)))
    return [pts]


def g_grid(w=80, h=60, cols=4, rows=3):
    cols, rows = int(max(1, cols)), int(max(1, rows)); out = []
    for i in range(cols + 1):
        x = -w / 2 + w * i / cols
        out.append([(x, -h / 2), (x, h / 2)] if i % 2 == 0 else [(x, h / 2), (x, -h / 2)])
    for j in range(rows + 1):
        y = -h / 2 + h * j / rows
        out.append([(-w / 2, y), (w / 2, y)] if j % 2 == 0 else [(w / 2, y), (-w / 2, y)])
    return out


def g_lines(w=60, h=40, spacing=2.0, angle=45):
    """Hachures dans un rectangle."""
    spacing = max(0.3, spacing); a = math.radians(angle); c, s = math.cos(a), math.sin(a)
    L = math.hypot(w, h); out = []; k = 0; off = -L / 2
    while off <= L / 2:
        p0 = (-c * L + off * -s, -s * L + off * c); p1 = (c * L + off * -s, s * L + off * c)
        seg = clip_line(p0, p1, -w / 2, -h / 2, w / 2, h / 2)
        if seg:
            out.append(seg if k % 2 == 0 else seg[::-1]); k += 1
        off += spacing
    return out + [rrect(w, h, 0)]


def g_rosette(d=60, petals=12):
    n = int(max(3, petals)); r = d / 4
    return [ell(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), r, r, 0, 360, 48) for i in range(n)]


def g_frame(w=100, h=70, gap=3.0, r=4):
    return [rrect(w, h, r), rrect(w - 2 * gap, h - 2 * gap, max(0, r - gap))]


def g_divider(length=80):
    """Séparateur : trait avec un losange au centre."""
    hl = length / 2; dm = min(5.0, length / 8)
    return [[(-hl, 0), (-dm * 1.6, 0)], [(dm * 1.6, 0), (hl, 0)],
            [(-dm, 0), (0, -dm * 0.8), (dm, 0), (0, dm * 0.8), (-dm, 0)]]


def g_sun(d=40, rays=12):
    n = int(max(4, rays)); r = d / 2; out = [ell(0, 0, r, r, 0, 360, 64)]
    for i in range(n):
        a = 2 * math.pi * i / n
        out.append([((r * 1.25) * math.cos(a), (r * 1.25) * math.sin(a)), ((r * (1.7 if i % 2 == 0 else 1.5)) * math.cos(a), (r * (1.7 if i % 2 == 0 else 1.5)) * math.sin(a))])
    return out


def g_cloud(w=70):
    u = w / 100.0
    circles = [(-28 * u, -10 * u, 17 * u), (-6 * u, -22 * u, 22 * u), (20 * u, -14 * u, 18 * u), (36 * u, -6 * u, 13 * u), (-40 * u, -2 * u, 10 * u)]
    runs = [r for r in union_outline(circles, y_max=0.0) if vec.path_len(r) > 3.0 * u]
    xs = [p[0] for r in runs for p in r]
    return runs + [[(min(xs), 0), (max(xs), 0)]]


def g_flower(d=60, petals=6):
    n = int(max(3, petals)); r = d / 2; pr = r * 0.36
    circles = [((r - pr) * math.cos(2 * math.pi * i / n), (r - pr) * math.sin(2 * math.pi * i / n), pr) for i in range(n)]
    return union_outline(circles) + [ell(0, 0, r * 0.16, r * 0.16, 0, 360, 24)]


def g_house(w=60):
    h = w * 0.55; roof = w * 0.35
    return [[(-w / 2, 0), (w / 2, 0), (w / 2, -h), (-w / 2, -h), (-w / 2, 0)],
            [(-w / 2 - w * 0.06, -h), (0, -h - roof), (w / 2 + w * 0.06, -h)],
            [(-w * 0.12, 0), (-w * 0.12, -h * 0.55), (w * 0.12, -h * 0.55), (w * 0.12, 0)],
            [(w * 0.22, -h * 0.5), (w * 0.38, -h * 0.5), (w * 0.38, -h * 0.75), (w * 0.22, -h * 0.75), (w * 0.22, -h * 0.5)],
            [(-w * 0.38, -h * 0.5), (-w * 0.22, -h * 0.5), (-w * 0.22, -h * 0.75), (-w * 0.38, -h * 0.75), (-w * 0.38, -h * 0.5)],
            [(w * 0.26, -h - roof * 0.62), (w * 0.26, -h - roof * 1.0), (w * 0.34, -h - roof * 1.0), (w * 0.34, -h - roof * 0.45)]]


def g_tree(h=80):
    w = h * 0.55; th = h * 0.40; out = []
    for k in range(3):
        top = -h + k * h * 0.22
        bw = w * (0.55 + 0.22 * k)
        out.append([(0, top), (bw / 2, top + th), (-bw / 2, top + th), (0, top)])
    out.append([(-h * 0.05, -h * 0.16), (-h * 0.05, 0), (h * 0.05, 0), (h * 0.05, -h * 0.16)])
    return out


def g_speech(w=70, h=40):
    r = min(h * 0.3, 8); x0, y0, x1, y1 = -w / 2, -h / 2, w / 2, h / 2
    pts = [(x0 + r, y0), (x1 - r, y0)] + ell(x1 - r, y0 + r, r, r, -90, 0, 8) + [(x1, y1 - r)] + ell(x1 - r, y1 - r, r, r, 0, 90, 8)
    pts += [(x0 + w * 0.55, y1), (x0 + w * 0.22, y1 + h * 0.32), (x0 + w * 0.30, y1)]
    pts += [(x0 + r, y1)] + ell(x0 + r, y1 - r, r, r, 90, 180, 8) + [(x0, y0 + r)] + ell(x0 + r, y0 + r, r, r, 180, 270, 8)
    return [pts]


def g_banner(w=100, h=24):
    a = w * 0.12
    return [[(-w / 2 + a, -h / 2), (w / 2 - a, -h / 2), (w / 2 - a, h / 2), (-w / 2 + a, h / 2), (-w / 2 + a, -h / 2)],
            [(-w / 2 + a, -h * 0.25), (-w / 2, -h * 0.25), (-w / 2 + a * 0.45, h * 0.25), (-w / 2, h * 0.75), (-w / 2 + a * 1.4, h * 0.75), (-w / 2 + a, h / 2)],
            [(w / 2 - a, -h * 0.25), (w / 2, -h * 0.25), (w / 2 - a * 0.45, h * 0.25), (w / 2, h * 0.75), (w / 2 - a * 1.4, h * 0.75), (w / 2 - a, h / 2)]]


def g_leaf(length=60):
    n = 40; top = []; bot = []
    for i in range(n + 1):
        t = i / n
        x = length * t; y = length * 0.22 * math.sin(math.pi * t) * (1 - 0.35 * t)
        top.append((x - length / 2, -y)); bot.append((x - length / 2, y))
    return [top + bot[::-1][1:], [(-length / 2 - length * 0.12, 0), (length * 0.42, 0)]]


def g_butterfly(w=70):
    u = w / 100.0
    out = []
    for sgn in (-1, 1):
        out.append(ell(sgn * 26 * u, -14 * u, 25 * u, 18 * u, 0, 360, 48, rot=sgn * 30))
        out.append(ell(sgn * 19 * u, 16 * u, 16 * u, 12 * u, 0, 360, 40, rot=-sgn * 25))
        out.append([(sgn * 2 * u, -22 * u), (sgn * 12 * u, -40 * u), (sgn * 20 * u, -42 * u)])
    out.append(ell(0, 0, 3.2 * u, 24 * u, 0, 360, 24))
    return out


def g_moon(d=50):
    r = d / 2; R2 = r * 0.82; off = r * 0.55
    x = (r * r - R2 * R2 + off * off) / (2 * off); y = math.sqrt(max(0.0, r * r - x * x))
    phi = math.degrees(math.atan2(y, x)); psi = math.degrees(math.atan2(y, x - off))
    outer = ell(0, 0, r, r, phi, 360 - phi, 96)
    inner = ell(off, 0, R2, R2, 360 - psi, psi, 96)
    return [outer + inner + [outer[0]]]


def g_smiley(d=50):
    r = d / 2
    return [ell(0, 0, r, r, 0, 360, 72), ell(-r * 0.36, -r * 0.25, r * 0.07, r * 0.12, 0, 360, 16),
            ell(r * 0.36, -r * 0.25, r * 0.07, r * 0.12, 0, 360, 16), ell(0, r * 0.05, r * 0.55, r * 0.5, 25, 155, 24)]


def g_mountains(w=100):
    h = w * 0.35
    return [[(-w / 2, h / 2), (-w * 0.22, -h / 2), (-w * 0.05, h * 0.05), (w * 0.12, -h * 0.35), (w / 2, h / 2)]]


def g_check(size=30):
    return [[(-size * 0.5, 0), (-size * 0.15, size * 0.35), (size * 0.5, -size * 0.4)]]


GENS = {
    # id: (nom, catégorie, fonction, paramètres)
    "line": ("Ligne", "Lignes et motifs", g_line, [("length", "Longueur", 60, 5, 300, 5, "mm")]),
    "wave": ("Vague", "Lignes et motifs", g_wave, [("length", "Longueur", 100, 10, 300, 5, "mm"), ("amp", "Amplitude", 8, 1, 40, 1, "mm"), ("periods", "Périodes", 5, 1, 30, 1, "")]),
    "zigzag": ("Zigzag", "Lignes et motifs", g_zigzag, [("length", "Longueur", 100, 10, 300, 5, "mm"), ("amp", "Amplitude", 8, 1, 40, 1, "mm"), ("n", "Dents", 8, 1, 40, 1, "")]),
    "grid": ("Quadrillage", "Lignes et motifs", g_grid, [("w", "Largeur", 80, 10, 300, 5, "mm"), ("h", "Hauteur", 60, 10, 250, 5, "mm"), ("cols", "Colonnes", 4, 1, 40, 1, ""), ("rows", "Lignes", 3, 1, 40, 1, "")]),
    "lines": ("Hachures", "Lignes et motifs", g_lines, [("w", "Largeur", 60, 5, 300, 5, "mm"), ("h", "Hauteur", 40, 5, 250, 5, "mm"), ("spacing", "Écart", 2.0, 0.3, 10, 0.1, "mm"), ("angle", "Angle", 45, 0, 179, 5, "°")]),
    "spiral": ("Spirale", "Lignes et motifs", g_spiral, [("turns", "Tours", 4, 1, 20, 0.5, ""), ("d", "Diamètre", 60, 10, 250, 5, "mm")]),
    "rosette": ("Rosace", "Lignes et motifs", g_rosette, [("d", "Diamètre", 60, 10, 250, 5, "mm"), ("petals", "Cercles", 12, 3, 36, 1, "")]),
    "rect": ("Rectangle", "Formes", g_rect, [("w", "Largeur", 60, 2, 300, 1, "mm"), ("h", "Hauteur", 40, 2, 250, 1, "mm"), ("r", "Arrondi", 0, 0, 60, 1, "mm")]),
    "ellipse": ("Cercle / ellipse", "Formes", g_ellipse, [("w", "Largeur", 50, 2, 300, 1, "mm"), ("h", "Hauteur", 50, 2, 250, 1, "mm")]),
    "polygon": ("Polygone régulier", "Formes", g_polygon, [("n", "Côtés", 6, 3, 24, 1, ""), ("d", "Diamètre", 50, 5, 250, 1, "mm")]),
    "star": ("Étoile", "Formes", g_star, [("n", "Branches", 5, 3, 24, 1, ""), ("d", "Diamètre", 50, 5, 250, 1, "mm"), ("inner", "Creux", 0.4, 0.1, 0.9, 0.05, "")]),
    "diamond": ("Losange", "Formes", g_diamond, [("w", "Largeur", 40, 2, 250, 1, "mm"), ("h", "Hauteur", 60, 2, 250, 1, "mm")]),
    "heart": ("Cœur", "Formes", g_heart, [("w", "Largeur", 50, 5, 250, 1, "mm")]),
    "cross": ("Croix", "Formes", g_cross, [("size", "Taille", 30, 3, 250, 1, "mm")]),
    "arrow": ("Flèche", "Formes", g_arrow, [("length", "Longueur", 70, 10, 300, 5, "mm"), ("head", "Pointe", 16, 3, 60, 1, "mm")]),
    "check": ("Coche", "Formes", g_check, [("size", "Taille", 30, 5, 150, 1, "mm")]),
    "frame": ("Cadre double", "Cadres et ornements", g_frame, [("w", "Largeur", 100, 10, 300, 5, "mm"), ("h", "Hauteur", 70, 10, 250, 5, "mm"), ("gap", "Écart", 3, 1, 20, 0.5, "mm"), ("r", "Arrondi", 4, 0, 40, 1, "mm")]),
    "divider": ("Séparateur", "Cadres et ornements", g_divider, [("length", "Longueur", 80, 20, 300, 5, "mm")]),
    "banner": ("Bandeau", "Cadres et ornements", g_banner, [("w", "Largeur", 100, 30, 300, 5, "mm"), ("h", "Hauteur", 24, 8, 100, 1, "mm")]),
    "speech": ("Bulle de dialogue", "Cadres et ornements", g_speech, [("w", "Largeur", 70, 20, 250, 5, "mm"), ("h", "Hauteur", 40, 12, 150, 2, "mm")]),
    "sun": ("Soleil", "Dessins", g_sun, [("d", "Diamètre", 40, 8, 150, 2, "mm"), ("rays", "Rayons", 12, 4, 36, 1, "")]),
    "cloud": ("Nuage", "Dessins", g_cloud, [("w", "Largeur", 70, 15, 250, 5, "mm")]),
    "flower": ("Fleur", "Dessins", g_flower, [("d", "Diamètre", 60, 10, 250, 2, "mm"), ("petals", "Pétales", 6, 3, 16, 1, "")]),
    "house": ("Maison", "Dessins", g_house, [("w", "Largeur", 60, 15, 250, 5, "mm")]),
    "tree": ("Sapin", "Dessins", g_tree, [("h", "Hauteur", 80, 15, 250, 5, "mm")]),
    "leaf": ("Feuille", "Dessins", g_leaf, [("length", "Longueur", 60, 10, 250, 5, "mm")]),
    "butterfly": ("Papillon", "Dessins", g_butterfly, [("w", "Largeur", 70, 15, 250, 5, "mm")]),
    "moon": ("Croissant de lune", "Dessins", g_moon, [("d", "Diamètre", 50, 8, 200, 2, "mm")]),
    "smiley": ("Smiley", "Dessins", g_smiley, [("d", "Diamètre", 50, 8, 200, 2, "mm")]),
    "mountains": ("Montagnes", "Dessins", g_mountains, [("w", "Largeur", 100, 20, 300, 5, "mm")]),
}


def gens_schema():
    return {k: {"name": v[0], "cat": v[1],
                "params": [{"k": p[0], "label": p[1], "def": p[2], "min": p[3], "max": p[4], "step": p[5], "unit": p[6]} for p in v[3]]}
            for k, v in GENS.items()}


def defaults(gen):
    return {p[0]: p[2] for p in GENS[gen][3]}


def generate(gen, params=None):
    """(paths centrés, largeur, hauteur) pour un générateur et ses paramètres (bornés)."""
    if gen not in GENS:
        raise ValueError("Forme inconnue.")
    name, cat, fn, spec = GENS[gen]
    vals = {}
    for k, label, dflt, lo, hi, st, unit in spec:
        try:
            v = float((params or {}).get(k, dflt))
        except (TypeError, ValueError):
            v = dflt
        vals[k] = min(hi, max(lo, v))
    paths = fn(**vals)
    paths = [[(float(x), float(y)) for x, y in p] for p in paths if len(p) >= 2]
    if not paths:
        return [], 0.0, 0.0
    return vec.center_paths(paths)


def simplify_preview(paths, max_pts=260):
    """Version légère pour les vignettes."""
    tot = sum(len(p) for p in paths)
    eps = 0.05
    out = paths
    while tot > max_pts and eps < 8:
        out = [vec.rdp(p, eps) if len(p) > 2 else p for p in paths]
        tot = sum(len(p) for p in out); eps *= 1.8
    return [[[round(x, 2), round(y, 2)] for x, y in p] for p in out]

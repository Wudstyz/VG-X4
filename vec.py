"""VG-X4 : vectorisation (texte, SVG, image) et génération / analyse de G-code.

Tous les tracés sont des polylignes [(x, y), ...] en millimètres,
x vers la droite, y vers le BAS (origine = coin haut-gauche du papier).
"""
import io, math, os, random, re, time, unicodedata

import numpy as np
from PIL import Image, ImageFilter, ImageFont, ImageOps

# ───────────────────────── géométrie ─────────────────────────


def bbox(paths):
    pts = [p for path in paths for p in path]
    if not pts:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def path_len(path):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))


def rdp(pts, eps):
    """Ramer–Douglas–Peucker itératif (pts : liste de (x, y))."""
    n = len(pts)
    if n < 3 or eps <= 0:
        return list(pts)
    a = np.asarray(pts, dtype=float)
    keep = np.zeros(n, bool); keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = a[j] - a[i]
        d = a[i + 1:j] - a[i]
        L = math.hypot(*seg)
        if L < 1e-12:
            dist = np.hypot(d[:, 0], d[:, 1])
        else:
            dist = np.abs(seg[0] * d[:, 1] - seg[1] * d[:, 0]) / L
        k = int(np.argmax(dist))
        if dist[k] > eps:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m)); stack.append((m, j))
    return [tuple(p) for p in a[keep]]


def clean(paths, eps=0.03, min_len=0.05):
    out = []
    for p in paths:
        if len(p) < 2:
            continue
        q = [p[0]]
        for pt in p[1:]:
            if math.hypot(pt[0] - q[-1][0], pt[1] - q[-1][1]) > 1e-4:
                q.append(pt)
        if len(q) < 2:
            continue
        q = rdp(q, eps)
        if path_len(q) >= min_len:
            out.append([(float(x), float(y)) for x, y in q])
    return out


def join_paths(paths, tol):
    """Fusionne les tracés dont les extrémités sont à moins de `tol` mm
    (évite des levées de stylo inutiles)."""
    if tol <= 0 or len(paths) < 2:
        return paths
    paths = [list(p) for p in paths]
    ends = {}
    cell = max(tol, 1e-3)

    def key(pt):
        return (int(pt[0] // cell), int(pt[1] // cell))

    alive = [True] * len(paths)
    grid = {}
    for i, p in enumerate(paths):
        for e in (0, 1):
            grid.setdefault(key(p[0] if e == 0 else p[-1]), []).append((i, e))

    def near(pt, skip):
        kx, ky = key(pt)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i, e in grid.get((kx + dx, ky + dy), ()):
                    if i == skip or not alive[i]:
                        continue
                    q = paths[i][0] if e == 0 else paths[i][-1]
                    d = math.hypot(q[0] - pt[0], q[1] - pt[1])
                    if d <= tol and (best is None or d < best[0]):
                        best = (d, i, e)
        return best

    for i in range(len(paths)):
        if not alive[i]:
            continue
        while True:
            b = near(paths[i][-1], i)
            if not b:
                break
            _, j, e = b
            q = paths[j] if e == 0 else paths[j][::-1]
            paths[i] = paths[i] + q
            alive[j] = False
            for e2 in (0, 1):
                grid.setdefault(key(paths[i][-1]), []).append((i, 1))
    return [p for p, a in zip(paths, alive) if a]


def order_paths(paths, start=(0.0, 0.0)):
    """Ordre plus-proche-voisin avec inversion éventuelle des tracés."""
    n = len(paths)
    if n < 3:
        return paths
    starts = np.array([p[0] for p in paths]); ends = np.array([p[-1] for p in paths])
    used = np.zeros(n, bool); out = []
    cur = np.array(start, float)
    for _ in range(n):
        ds = np.hypot(*(starts - cur).T); de = np.hypot(*(ends - cur).T)
        ds[used] = np.inf; de[used] = np.inf
        i = int(np.argmin(ds)); j = int(np.argmin(de))
        if ds[i] <= de[j]:
            used[i] = True; out.append(paths[i]); cur = ends[i]
        else:
            used[j] = True; out.append(paths[j][::-1]); cur = starts[j]
    return out


def two_opt(paths, start=(0.0, 0.0), end=None, budget=1.5):
    """Améliore un ordre de tracés par échanges 2-opt (inverser un bloc de tracés consécutifs, en retournant chacun).
    Ne change jamais le dessin : seulement l'ordre et le sens de parcours. `end` = point où l'on revient à la fin
    (None = pas de retour). S'arrête quand plus rien ne s'améliore ou après `budget` secondes."""
    n = len(paths)
    if n < 3:
        return paths
    t0 = time.time()
    P = list(paths)
    S = np.array([p[0] for p in P], float); E = np.array([p[-1] for p in P], float)
    st = np.array(start, float)
    en = np.array(end, float) if end is not None else None
    improved = True
    while improved and time.time() - t0 < budget:
        improved = False
        for i in range(n):
            prev = E[i - 1] if i else st
            Ej, Si = E[i:], S[i]
            nxt = np.vstack([S[i + 1:], en if en is not None else E[-1:]])          # début du tracé qui suit chaque j
            d_old_next = np.hypot(*(Ej - nxt).T); d_new_next = np.hypot(*(nxt - Si).T)
            if en is None:                                                          # pas d'arête après le dernier tracé
                d_old_next[-1] = 0.0; d_new_next[-1] = 0.0
            delta = np.hypot(*(prev - Ej).T) + d_new_next - np.hypot(*(prev - Si)) - d_old_next
            k = int(np.argmin(delta))
            if delta[k] < -1e-6:
                j = i + k
                P[i:j + 1] = [q[::-1] for q in P[i:j + 1][::-1]]
                S[i:j + 1], E[i:j + 1] = E[i:j + 1][::-1].copy(), S[i:j + 1][::-1].copy()
                improved = True
            if time.time() - t0 > budget:
                break
    return P


_order_cache = {}


def order_paths_best(paths, start=(0.0, 0.0), end=None, budget=1.5):
    """Plus proche voisin puis 2-opt, avec mémoire (l'estimation et le tracé demandent le même calcul)."""
    key = (hash(tuple(tuple(map(tuple, p)) for p in paths)), round(start[0], 2), round(start[1], 2), end)
    hit = _order_cache.get(key)
    if hit is not None:
        return hit
    out = two_opt(order_paths(paths, start), start, end, budget)
    if len(_order_cache) > 24:
        _order_cache.pop(next(iter(_order_cache)))
    _order_cache[key] = out
    return out


def center_paths(paths):
    """Recentre sur le centre de la boîte englobante. Retourne (paths, w, h)."""
    x0, y0, x1, y1 = bbox(paths)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return [[(x - cx, y - cy) for x, y in p] for p in paths], x1 - x0, y1 - y0


# ───────────────────────── texte : polices Hershey ─────────────────────────

HERSHEY_NAMES = {
    "scripts": "Manuscrite fine (Hershey Script)",
    "scriptc": "Manuscrite liée (Hershey Script complexe)",
    "cursive": "Cursive (Hershey)",
    "rowmans": "Bâton simple (Roman)",
    "futural": "Bâton Futura",
    "futuram": "Bâton Futura moyen",
    "rowmand": "Roman double trait",
    "timesi": "Times italique",
    "timesr": "Times romain",
    "gothgbt": "Gothique (britannique)",
    "rowmant": "Bâton triple trait (Roman)",
    "timesrb": "Times gras",
    "timesib": "Times italique gras",
    "gothiceng": "Gothique anglais",
    "gothicger": "Gothique allemand",
    "gothicita": "Gothique italien",
    "markers": "Feutre (Hershey Markers)",
}

_hershey = {}
ACCENTS = {
    "́": "acute", "̀": "grave", "̂": "circ", "̈": "diaer",
    "̧": "cedil", "̃": "tilde",
}


def _hfont(name):
    from HersheyFonts import HersheyFonts
    if name not in _hershey:
        f = HersheyFonts(); f.load_default_font(name)
        _hershey[name] = f
    return _hershey[name]


def _accent_strokes(kind, cx, top, base, unit):
    """Accents dessinés (coordonnées Hershey : y vers le bas). cx = centre,
    top = ordonnée du haut de la lettre, base = ligne de base, unit ≈ hauteur de majuscule/21*3."""
    u = unit
    if kind == "acute":
        y = top - 1.2 * u
        return [[(cx - 0.4 * u, y + 1.1 * u), (cx + 1.0 * u, y - 0.2 * u)]]
    if kind == "grave":
        y = top - 1.2 * u
        return [[(cx + 0.4 * u, y + 1.1 * u), (cx - 1.0 * u, y - 0.2 * u)]]
    if kind == "circ":
        y = top - 1.2 * u
        return [[(cx - 1.0 * u, y + 0.6 * u), (cx, y - 0.6 * u), (cx + 1.0 * u, y + 0.6 * u)]]
    if kind == "diaer":
        y = top - 0.8 * u
        return [[(cx - 0.9 * u, y), (cx - 0.8 * u, y)], [(cx + 0.8 * u, y), (cx + 0.9 * u, y)]]
    if kind == "tilde":
        y = top - 1.0 * u
        return [[(cx - 1.2 * u, y + 0.3 * u), (cx - 0.6 * u, y - 0.4 * u), (cx, y),
                 (cx + 0.6 * u, y + 0.4 * u), (cx + 1.2 * u, y - 0.3 * u)]]
    if kind == "cedil":
        return [[(cx - 0.2 * u, base), (cx, base + 0.8 * u), (cx + 0.5 * u, base + 1.3 * u),
                 (cx + 0.2 * u, base + 2.0 * u), (cx - 0.6 * u, base + 2.0 * u)]]
    return []


def _glyph(font, ch, missing):
    """Retourne (strokes, width) en unités Hershey (x ≥ 0, y vers le bas)."""
    f = _hfont(font)
    ch = {"’": "'", "‘": "'", "«": "<", "»": ">", "–": "-", "—": "-", "…": "...", " ": " ",
          "“": '"', "”": '"'}.get(ch, ch)
    if len(ch) > 1:                             # ex. "..."
        out, x = [], 0
        for c in ch:
            s, w = _glyph(font, c, missing)
            out += [[(px + x, py) for px, py in st] for st in s]; x += w
        return out, x
    special = {"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE"}
    if ch in special:
        return _glyph(font, special[ch], missing)
    if ch == "€":
        s, w = _glyph(font, "C", missing)
        bars = [[(0.0, -3.0), (w * 0.75, -3.0)], [(0.0, 3.0), (w * 0.75, 3.0)]]
        return s + bars, w
    gl = f.all_glyphs.get(ch)
    if ch == " ":
        return [], (gl.char_width if gl else 12)
    if gl is not None and ch.isascii():
        lo = gl.left_offset
        return [[(x - lo, y) for x, y in st] for st in gl.strokes], gl.char_width
    d = unicodedata.normalize("NFD", ch)
    if len(d) > 1 and d[0].isascii():
        s, w = _glyph(font, d[0], missing)
        pts = [p for st in s for p in st]
        if not pts:
            return s, w
        cap = f.render_options.cap_line; base = f.render_options.base_line
        ytop = min(p[1] for p in pts)
        unit = (base - cap) / 21.0 * 3.0
        extra = []
        for m in d[1:]:
            k = ACCENTS.get(m)
            if k:
                extra += _accent_strokes(k, w / 2.0, ytop, base, unit)
        return s + extra, w
    missing.add(ch)
    return [], 12


def chaikin(pts, n=1):
    """Lissage par coupe des coins (garde les extrémités)."""
    for _ in range(n):
        if len(pts) < 3:
            return pts
        out = [pts[0]]
        for i in range(len(pts) - 1):
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            out.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            out.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        out.append(pts[-1])
        pts = out
    return pts


def wrap_lines(text, font="scripts", size=8.0, letter_spacing=0.0, max_width=0.0, missing=None):
    """Découpe le texte en lignes (même algorithme que le tracé). Retourne [(ligne, début_de_ligne_dure)]."""
    f = _hfont(font)
    k = size / float(f.render_options.base_line - f.render_options.cap_line)
    missing = set() if missing is None else missing
    lines = []
    for raw in text.replace("\r", "").split("\n"):
        if max_width and max_width > 0:
            words, cur, first = raw.split(" "), "", True
            for w in words:
                t = (cur + " " + w) if cur else w
                if cur and _line_width(font, t, k, letter_spacing, missing) > max_width:
                    lines.append((cur, first)); first = False; cur = w
                else:
                    cur = t
            lines.append((cur, first))
        else:
            lines.append((raw, True))
    return lines


def text_to_paths(text, font="scripts", size=8.0, line_spacing=1.9, letter_spacing=0.0,
                  align="left", slant=0.0, jitter=0.0, max_width=0.0, seed=1,
                  word_spacing=0.0, wobble=0.0, smooth=0.0, raw=False):
    """Retourne (paths centrés, w, h, manquants). `size` = hauteur de majuscule en mm.
    raw=True : retourne (paths non recentrés — ligne de base de la 1re ligne à y=0, x=0 au départ du texte —, manquants)."""
    f = _hfont(font)
    cap = f.render_options.cap_line; base = f.render_options.base_line
    k = size / float(base - cap)                       # mm par unité Hershey
    rnd = random.Random(seed)
    missing = set()
    lines = [l for l, _ in wrap_lines(text, font, size, letter_spacing, max_width, missing)]
    sh = math.tan(math.radians(slant))
    paths, widths = [], []
    pitch = size * line_spacing
    for li, line in enumerate(lines):
        x = 0.0
        row = []
        ph1, ph2 = rnd.uniform(0, 6.28), rnd.uniform(0, 6.28)
        for ch in unicodedata.normalize("NFC", line):
            strokes, w = _glyph(font, ch, missing)
            dy = rnd.uniform(-1, 1) * jitter * size * 0.05
            if wobble:                                              # la ligne de base ondule doucement
                dy += wobble * size * 0.10 * (0.6 * math.sin(x * 0.06 + ph1) + 0.4 * math.sin(x * 0.17 + ph2))
            ang = rnd.uniform(-1, 1) * jitter * math.radians(3)
            sc = 1 + rnd.uniform(-1, 1) * jitter * 0.04
            ca, sa = math.cos(ang), math.sin(ang)
            for st in strokes:
                pts = []
                for px, py in st:
                    yy = (py - base) * k * sc                   # 0 = ligne de base, négatif = haut
                    xx = (px - w / 2.0) * k * sc
                    xr = xx * ca - yy * sa; yr = xx * sa + yy * ca
                    pts.append((x + w / 2.0 * k + xr - sh * yr, li * pitch + yr + dy))
                row.append(pts)
            x += w * k + letter_spacing + (word_spacing if ch == " " else 0.0)
        widths.append(max(0.0, x - letter_spacing))
        paths.append(row)
    W = max(widths) if widths else 0
    out = []
    for row, wl in zip(paths, widths):
        off = {"left": 0.0, "center": (W - wl) / 2, "right": W - wl}.get(align, 0.0)
        for st in row:
            out.append([(px + off, py) for px, py in st])
    if smooth and smooth > 0:
        out = [chaikin(st, 1 if smooth < 1.5 else 2) for st in out]
    out = clean(out, 0.02, 0.05)
    if raw:
        return out, sorted(missing)
    if not out:
        return [], 0.0, 0.0, sorted(missing)
    c, w, h = center_paths(out)
    return c, w, h, sorted(missing)


def _line_width(font, line, k, ls, missing):
    return sum(_glyph(font, ch, missing)[1] * k + ls for ch in line) - ls


# ───────────────────────── texte : polices système ─────────────────────────

FONT_DIRS = ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
             os.path.expanduser("~/Library/Fonts")]
HAND_WORDS = ("hand", "script", "chancery", "snell", "zapfino", "noteworthy", "marker", "chalk",
              "savoye", "brush", "bradley", "party", "segoe", "kristi", "cursive", "signature")
_sys_fonts = None


def system_fonts():
    """Liste [{id, name, path, index, hand}] des polices du Mac (mise en cache)."""
    global _sys_fonts
    if _sys_fonts is not None:
        return _sys_fonts
    res = []
    for d in FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith((".ttf", ".otf", ".ttc")):
                continue
            path = os.path.join(d, fn)
            for idx in range(0, 12 if fn.lower().endswith(".ttc") else 1):
                try:
                    f = ImageFont.truetype(path, 20, index=idx)
                    fam, sty = f.getname()
                except Exception:
                    break
                name = f"{fam} {sty}".strip() if sty and sty.lower() != "regular" else fam
                res.append({"id": f"sys:{path}#{idx}", "name": name, "path": path, "index": idx,
                            "hand": any(w in name.lower() or w in fn.lower() for w in HAND_WORDS)})
    seen, uniq = set(), []
    for r in res:
        if r["name"] not in seen:
            seen.add(r["name"]); uniq.append(r)
    _sys_fonts = uniq
    return uniq


def system_text_mask(text, font_id, size, line_spacing, align, ppm):
    """Rastérise le texte (police système) à `ppm` pixels/mm. size = hauteur de majuscule en mm.
    Retourne un masque L (255 = encre)."""
    path, _, idx = font_id[4:].partition("#")
    probe = ImageFont.truetype(path, 100, index=int(idx or 0))
    cap = probe.getbbox("H")
    cap_h = max(1, cap[3] - cap[1])
    px = max(8, int(round(size * ppm * 100.0 / cap_h)))
    font = ImageFont.truetype(path, px, index=int(idx or 0))
    lines = text.replace("\r", "").split("\n")
    asc, desc = font.getmetrics()
    pitch = int(size * ppm * line_spacing)
    widths = [font.getlength(l) for l in lines]
    W = int(max(widths + [1])) + 2 * px; H = pitch * len(lines) + 2 * px
    img = Image.new("L", (W, H), 0)
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    for i, l in enumerate(lines):
        off = {"left": 0, "center": (max(widths) - widths[i]) / 2, "right": max(widths) - widths[i]}[align]
        d.text((px + off, px // 2 + i * pitch), l, font=font, fill=255)
    return img


# ───────────────────────── SVG ─────────────────────────


def _fill_hatch(fills, spacing, angle):
    """fills : liste d'éléments, chacun = liste de polygones (mm). Remplissage pair/impair par élément,
    puis hachures sur le masque obtenu. Retourne des segments (mm)."""
    from PIL import ImageChops, ImageDraw
    pts = [q for el in fills for poly in el for q in poly]
    if not pts:
        return []
    x0, y0 = min(q[0] for q in pts), min(q[1] for q in pts)
    x1, y1 = max(q[0] for q in pts), max(q[1] for q in pts)
    ppm = min(10.0, 3500.0 / max(x1 - x0, y1 - y0, 1.0))
    W, H = int((x1 - x0) * ppm) + 3, int((y1 - y0) * ppm) + 3
    total = Image.new("1", (W, H), 0)
    for el in fills:
        m = Image.new("1", (W, H), 0)
        for poly in el:
            t = Image.new("1", (W, H), 0)
            ImageDraw.Draw(t).polygon([((x - x0) * ppm + 1, (y - y0) * ppm + 1) for x, y in poly], fill=1)
            m = ImageChops.logical_xor(m, t)
        total = ImageChops.logical_or(total, m)
    mask = np.asarray(total, dtype=bool)
    segs = _hatch(mask, ppm, max(0.2, spacing), angle, 0.5)
    return [[(x - 1 / ppm + x0, y - 1 / ppm + y0) for x, y in sg] for sg in segs]


def _svg_color(el):
    """Couleur « de tracé » d'un élément : contour s'il existe, sinon remplissage, sinon noir."""
    def hx(c):
        try:
            if c is not None and c.value is not None and (c.alpha is None or c.alpha > 0):
                return c.hex[:7].lower()
        except Exception:
            pass
        return None
    return hx(getattr(el, "stroke", None)) or hx(getattr(el, "fill", None)) or "#000000"


def svg_to_paths(data, tol=0.05, fill="none", hatch=1.0, angle=45.0, color=None):
    """Lit un SVG (bytes). Retourne (paths centrés, w, h, notes, couleurs). Unité : mm
    (1 px SVG = 25,4/96 mm). fill="hatch" : les formes remplies sont hachurées.
    color : ne garder que les tracés de cette couleur (le centrage reste celui de tout le dessin,
    pour que les calques de couleurs différentes se superposent exactement)."""
    import svgelements as se
    notes = []
    fills = []                                   # (couleur, [polygones])
    items = []                                   # (couleur, polyligne)
    mm = 25.4 / 96.0
    svg = se.SVG.parse(io.BytesIO(data), reify=True, ppi=96.0)
    skipped = set()
    for el in svg.elements():
        if isinstance(el, (se.Path, se.Shape)) and not isinstance(el, (se.SVG, se.Group)):
            if getattr(el, "values", {}) and str(el.values.get("display", "")).lower() == "none":
                continue
            try:
                p = el if isinstance(el, se.Path) else se.Path(el)
                p = abs(p)
            except Exception:
                continue
            col = _svg_color(el)
            el_polys = []
            fcol = getattr(el, "fill", None)
            filled = fill == "hatch" and fcol is not None and fcol.value is not None and (fcol.alpha is None or fcol.alpha > 0)
            for sub in p.as_subpaths():
                sp = se.Path(sub)
                length = sp.length(error=1e-3) if len(sp) else 0
                if not length or not math.isfinite(length):
                    continue
                ts = np.linspace(0, 1, 2) if any(isinstance(s_, (se.CubicBezier, se.QuadraticBezier, se.Arc)) for s_ in sp) else None
                if ts is None:
                    pts = [(s_.start.x, s_.start.y) for s_ in sp if isinstance(s_, (se.Move, se.Line, se.Close))
                           and s_.start is not None]
                    if len(sp) and sp[-1].end is not None:
                        pts.append((sp[-1].end.x, sp[-1].end.y))
                    pts = [q for q in pts if q[0] is not None]
                else:
                    pts = []
                    for s_ in sp:
                        if isinstance(s_, se.Move) or s_.start is None:
                            continue
                        if isinstance(s_, (se.Line, se.Close)):
                            if not pts: pts.append((s_.start.x, s_.start.y))
                            pts.append((s_.end.x, s_.end.y))
                        else:
                            m = int(max(2, min(400, s_.length(error=1e-3) * mm / max(tol, 0.02))))
                            if not pts: pts.append((s_.start.x, s_.start.y))
                            for t in np.linspace(0, 1, m + 1)[1:]:
                                q = s_.point(t); pts.append((q.x, q.y))
                if len(pts) >= 2:
                    items.append((col, [(x * mm, y * mm) for x, y in pts]))
                if filled and len(pts) >= 3:
                    el_polys.append([(x * mm, y * mm) for x, y in pts])
            if el_polys:
                fills.append((col, el_polys))
        elif isinstance(el, se.Text):
            skipped.add("texte (convertir en tracés dans Inkscape)")
        elif isinstance(el, se.Image):
            skipped.add("image incorporée")
    if skipped:
        notes.append("Ignoré : " + ", ".join(sorted(skipped)))
    if fills:
        try:
            nf = 0
            for col in sorted({c for c, _ in fills}):
                segs = _fill_hatch([e for c, e in fills if c == col], float(hatch), float(angle))
                items += [(col, sg) for sg in segs]; nf += sum(1 for c, _ in fills if c == col)
            notes.append(f"{nf} forme(s) remplie(s) par hachures")
        except Exception as e:
            notes.append(f"Hachures impossibles : {e}")
    if not items:
        return [], 0.0, 0.0, notes + ["Aucun tracé exploitable dans ce SVG."], []
    allp = [p_ for _, p_ in items]
    allp = clean(allp, tol * 0.5, 0.05)          # même nettoyage pour tous (le centrage dépend de tout)
    x0, y0, x1, y1 = bbox(allp)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    counts = {}
    for c, p_ in items:
        counts[c] = counts.get(c, 0) + 1
    colors = [{"color": c, "n": n} for c, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    sel = [p_ for c, p_ in items if not color or c == color]
    sel = clean(sel, tol * 0.5, 0.05)
    return [[(x - cx, y - cy) for x, y in p_] for p_ in sel], x1 - x0, y1 - y0, notes, colors


# ───────────────────────── image → tracés ─────────────────────────


def load_gray(data):
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im); im = bg
    return im.convert("L")


def otsu(gray):
    h = np.bincount(np.asarray(gray).ravel(), minlength=256).astype(float)
    tot = h.sum(); sumt = (h * np.arange(256)).sum()
    wb = sb = 0.0; best, thr = -1, 128
    for t in range(256):
        wb += h[t]
        if wb == 0: continue
        wf = tot - wb
        if wf == 0: break
        sb += t * h[t]
        mb, mf = sb / wb, (sumt - sb) / wf
        v = wb * wf * (mb - mf) ** 2
        if v > best: best, thr = v, t
    return thr


def mask_to_paths(gray, ppm, mode, threshold=128, hatch=0.5, angle=45.0, min_blob=0.5,
                  smooth=0.6, simplify=0.05, spur=1.0, levels=4):
    """gray : PIL L (0 noir … 255 blanc) à ppm pixels/mm. Retourne des polylignes en mm
    (origine = coin haut-gauche de l'image)."""
    from skimage import measure
    out = []
    if smooth > 0:
        gray = gray.filter(ImageFilter.GaussianBlur(smooth))
    a = np.asarray(gray, dtype=np.float32)
    if mode in ("contour", "both"):
        pad = np.pad(a, 1, constant_values=255)
        for c in measure.find_contours(pad, level=float(threshold)):
            pts = [((x - 1) / ppm, (y - 1) / ppm) for y, x in c]
            if len(pts) < 4 or path_len(pts) < min_blob * 2:
                continue
            out.append(pts)
    if mode in ("hatch", "both"):
        out += _hatch(a < threshold, ppm, hatch, angle, min_blob)
    if mode == "skeleton":
        out += _skeleton(a < threshold, ppm, spur, min_blob)
    if mode == "tone":                       # hachures croisées : plus c'est sombre, plus il y a de passes
        L = int(max(2, min(5, levels)))
        lo = 25.0
        for k in range(L):
            thr = threshold - (threshold - lo) * k / (L - 1)
            ang = (angle + (0, 90, 45, 135, 22)[k]) % 180
            out += _hatch(a < thr, ppm, hatch, ang, min_blob)
    return out


def _hatch(mask, ppm, spacing, angle, min_len):
    H, W = mask.shape
    m = Image.fromarray((mask * 255).astype(np.uint8))
    ang = float(angle) % 180
    cx, cy = W / 2.0, H / 2.0
    if ang:
        m = m.rotate(ang, resample=Image.BILINEAR, expand=True, fillcolor=0)
    r = np.asarray(m) > 127
    RH, RW = r.shape
    step = max(1.0, spacing * ppm)
    segs = []
    ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))
    ncx, ncy = RW / 2.0, RH / 2.0
    y = step / 2
    row_i = 0
    while y < RH:
        row = r[int(y)]
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts = np.nonzero(d == 1)[0]; ends = np.nonzero(d == -1)[0]
        runs = [(s, e) for s, e in zip(starts, ends) if (e - s) / ppm >= min_len]
        if row_i % 2: runs = [(e, s) for s, e in runs[::-1]]
        for s, e in runs:
            seg = []
            for xx in (s, e):
                dx, dy = xx - ncx, y - ncy
                # rotation inverse (PIL tourne dans le sens anti-horaire)
                ox = dx * ca - dy * sa + cx
                oy = dx * sa + dy * ca + cy
                seg.append((ox / ppm, oy / ppm))
            segs.append(seg)
        y += step; row_i += 1
    return segs


def _skeleton(mask, ppm, spur_mm, min_len):
    from skimage.morphology import skeletonize
    sk = skeletonize(mask)
    ys, xs = np.nonzero(sk)
    if len(ys) == 0:
        return []
    pix = set(zip(ys.tolist(), xs.tolist()))
    nb = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
    adj = {p: [(p[0] + dy, p[1] + dx) for dy, dx in nb if (p[0] + dy, p[1] + dx) in pix] for p in pix}
    # supprime les voisins diagonaux redondants (coins en L)
    for p, ns in adj.items():
        if len(ns) >= 3:
            orth = [q for q in ns if q[0] == p[0] or q[1] == p[1]]
            keep = list(orth)
            for q in ns:
                if q in orth: continue
                if not any(abs(q[0] - o[0]) + abs(q[1] - o[1]) == 1 for o in orth):
                    keep.append(q)
            adj[p] = keep
    # rend la relation symétrique
    for p in list(adj):
        for q in adj[p]:
            if p not in adj[q]: adj[q].append(p)
    node = {p for p, ns in adj.items() if len(ns) != 2}
    seen_edges = set()
    paths = []

    def walk(start, nxt):
        path = [start, nxt]
        prev, cur = start, nxt
        while cur not in node:
            cand = [q for q in adj[cur] if q != prev]
            if not cand: break
            prev, cur = cur, cand[0]
            path.append(cur)
            if cur == start: break
        return path

    for n in node:
        for q in adj[n]:
            if (n, q) in seen_edges: continue
            p = walk(n, q)
            seen_edges.add((p[0], p[1])); seen_edges.add((p[-1], p[-2]))
            paths.append(p)
    done = {pt for p in paths for pt in p}
    for p0 in pix:                                    # boucles pures
        if p0 in done: continue
        p = [p0]; prev, cur = None, p0
        while True:
            cand = [q for q in adj[cur] if q != prev and q not in done]
            if not cand: break
            prev, cur = cur, cand[0]
            if cur == p0: p.append(cur); break
            p.append(cur); done.add(cur)
        done.update(p)
        if len(p) > 2: paths.append(p)
    # élagage des petites branches terminales
    deg = {p: len(ns) for p, ns in adj.items()}
    lim = spur_mm * ppm
    kept = []
    for p in paths:
        if len(p) < lim and (deg[p[0]] == 1) != (deg[p[-1]] == 1) and max(deg[p[0]], deg[p[-1]]) >= 3:
            continue
        kept.append(p)
    res = []
    for p in kept:
        pts = [((x + 0.5) / ppm, (y + 0.5) / ppm) for y, x in p]
        if path_len(pts) >= min_len: res.append(pts)
    return res


def image_to_paths(gray, width_mm, mode="contour", threshold=128, invert=False, hatch=0.5, angle=45.0,
                   min_blob=0.5, simplify=0.05, smooth=0.6, max_px=2600, levels=4):
    """gray : image PIL 'L'. Retourne (paths centrés, w, h, info)."""
    if invert:
        gray = ImageOps.invert(gray)
    ar = gray.height / gray.width
    ppm = min(8.0, max_px / max(width_mm, width_mm * ar))
    ppm = max(ppm, 1.0)
    if mode in ("hatch", "both", "tone"):
        ppm = max(ppm, 4.0 / max(hatch, 0.05) / 4.0)
        ppm = min(ppm, 12.0)
    w_px = max(8, int(round(width_mm * ppm))); h_px = max(8, int(round(width_mm * ar * ppm)))
    g = gray.resize((w_px, h_px), Image.LANCZOS)
    paths = mask_to_paths(g, w_px / width_mm, mode, threshold, hatch, angle, min_blob, smooth, simplify, levels=levels)
    paths = clean(paths, simplify, 0.05)
    if not paths:
        return [], 0.0, 0.0, {"ppm": round(ppm, 2)}
    w, h = width_mm, width_mm * ar
    paths = [[(x - w / 2, y - h / 2) for x, y in p] for p in paths]      # centré sur l'image
    return paths, w, h, {"ppm": round(ppm, 2), "paths": len(paths)}


def mask_to_item(mask_img, ppm, mode, threshold=128, hatch=0.5, angle=45.0, min_blob=0.3, simplify=0.04):
    """Tracés pour un masque (ex. texte système) ; blanc = encre → on inverse."""
    g = ImageOps.invert(mask_img)
    paths = mask_to_paths(g, ppm, mode, threshold, hatch, angle, min_blob, 0.5, simplify)
    paths = clean(paths, simplify, 0.05)
    if not paths:
        return [], 0.0, 0.0
    return center_paths(paths)


# ───────────────────────── G-code ─────────────────────────


def chain_time(segs, accel=400.0, jd=0.1):
    """Durée (s) d'une suite de segments exécutés sans arrêt : segs = [(dx, dy, vitesse mm/s)].
    Modèle du planificateur GRBL : accélération constante, vitesses de jonction (junction deviation)."""
    n = len(segs)
    if n == 0:
        return 0.0
    L = [math.hypot(dx, dy) for dx, dy, _ in segs]
    V = [max(1.0, v) for _, _, v in segs]
    keep = [i for i in range(n) if L[i] > 1e-9]
    if not keep:
        return 0.0
    L = [L[i] for i in keep]; V = [V[i] for i in keep]
    U = [(segs[i][0] / math.hypot(segs[i][0], segs[i][1]), segs[i][1] / math.hypot(segs[i][0], segs[i][1])) for i in keep]
    n = len(L)
    vj = [0.0] * (n + 1)
    for i in range(1, n):
        cos_t = -(U[i - 1][0] * U[i][0] + U[i - 1][1] * U[i][1])
        vmax = min(V[i - 1], V[i])
        if cos_t < -0.999999:
            vj[i] = vmax
        elif cos_t > 0.999999:
            vj[i] = 0.0
        else:
            sn = math.sqrt(0.5 * (1.0 - cos_t))
            vj[i] = min(vmax, math.sqrt(accel * jd * sn / (1.0 - sn)))
    for i in range(n - 1, -1, -1):                       # freinage possible jusqu'à la fin de la chaîne
        vj[i] = min(vj[i], math.sqrt(vj[i + 1] ** 2 + 2 * accel * L[i]))
    for i in range(n):                                    # accélération possible depuis le début
        vj[i + 1] = min(vj[i + 1], math.sqrt(vj[i] ** 2 + 2 * accel * L[i]))
    t = 0.0
    for i in range(n):
        v0, v1, vm, d = vj[i], vj[i + 1], V[i], L[i]
        da = (vm * vm - v0 * v0) / (2 * accel); dd = (vm * vm - v1 * v1) / (2 * accel)
        if da + dd <= d:
            t += (vm - v0) / accel + (vm - v1) / accel + (d - da - dd) / vm
        else:
            vp = math.sqrt(max(0.0, (2 * accel * d + v0 * v0 + v1 * v1) / 2))
            t += (vp - v0) / accel + (vp - v1) / accel
    return t


def apply_backlash(text, bx, by):
    """Compensation du jeu mécanique : à chaque inversion de sens sur un axe, la consigne est décalée du jeu
    (le décalage absorbe le jeu sans que le stylo ne bouge). Ne traite que le G-code absolu (G90)."""
    if not (bx or by):
        return text
    cur = {"X": 0.0, "Y": 0.0}; dr = {"X": 0, "Y": 0}; b = {"X": float(bx), "Y": float(by)}
    num = r"[-+]?(?:\d+\.?\d*|\.\d+)"
    out = []
    for raw in text.splitlines():
        code, sep, comment = raw.partition(";")
        u = re.sub(r"\(.*?\)", "", code).upper()
        if re.search(r"\bG\s*91\b", u):
            return text                                   # relatif : on ne touche à rien
        if not u.strip() or u.strip().startswith(("$", "%")) or re.search(r"\bG0*4(?!\d)", u):
            out.append(raw); continue
        vals = {k: float(v) for k, v in re.findall(r"([XY])\s*(" + num + ")", u)}
        if re.search(r"\bG\s*92\b", u):
            for k, v in vals.items(): cur[k] = v
            out.append(raw); continue
        if not vals:
            out.append(raw); continue
        new_code = code
        for k, v in vals.items():
            d = v - cur[k]
            if abs(d) > 1e-9:
                sg = 1 if d > 0 else -1
                if sg != dr[k]: dr[k] = sg
            cur[k] = v
            emitted = v + dr[k] * b[k] / 2.0
            new_code = re.sub(r"(" + k + r"\s*)" + num, lambda m, e=emitted: m.group(1) + fmt(e), new_code, count=1, flags=re.I)
        out.append(new_code + (sep + comment if sep else ""))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def fmt(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def dry_gcode(text):
    """Essai à blanc : retire descentes de stylo, pauses (G4) et changements de stylo (M0)."""
    out = []
    for raw in text.splitlines():
        l = re.sub(r"\(.*?\)", "", raw.split(";")[0]).strip().upper()
        if re.search(r"\bM0?[34]\b", l) or re.fullmatch(r"M0+", l.replace(" ", "")) or re.match(r"G0*4(?!\d)", l):
            continue
        out.append(raw)
    return "\n".join(out) + "\n"


def groups_to_gcode(groups, cfg, header_comment="", pens=None, pen_names=None, pen_s=None):
    """groups : liste de listes de polylignes (mm, y vers le bas, origine = zéro). Retourne (texte, stats).
    pens (optionnel) : numéro de stylo par groupe ; entre deux stylos différents on insère
    un `M0 ; Changer de stylo : …` (le logiciel s'arrête et attend la confirmation)."""
    sign = -1.0 if cfg.get("invert_y") else 1.0
    sx = -1.0 if cfg.get("invert_x") else 1.0
    s_down = int(cfg.get("pen_down_s", 905))
    dwell = max(0.0, cfg.get("pen_settle_ms", 300) / 1000.0)
    ff, tf = int(cfg.get("draw_feed", 1200)), int(cfg.get("travel_feed", 3000))
    lines = []
    if header_comment:
        lines.append(f"; {header_comment}")
    lines += ["G21", "G90", "G94", "M5", f"G4 P{dwell:.3f}"]
    n_paths = 0; draw = 0.0; travel = 0.0; changes = 0
    cur = (0.0, 0.0); last_pen = None
    merged = []                                  # des objets consécutifs au même stylo s'optimisent ensemble
    for gi, grp in enumerate(groups):
        grp = [g for g in grp if len(g) >= 2]
        if not grp:
            continue
        pen = pens[gi] if pens and gi < len(pens) else None
        if cfg.get("optimize", True) and merged and merged[-1][1] == pen and pen is not None:
            merged[-1][0].extend(grp)
        else:
            merged.append([list(grp), pen])
    for grp, pen in merged:
        if pen is not None and last_pen is not None and pen != last_pen:
            name = (pen_names or {}).get(pen) or (pen_names or {}).get(str(pen)) or f"stylo {pen + 1}"
            lines += ["M5", f"G4 P{dwell:.3f}", f"M0 ; Changer de stylo : {name}"]
            changes += 1
        if pen is not None:
            last_pen = pen
        grp = join_paths(grp, cfg.get("join_tol", 0.4))
        if cfg.get("optimize", True):
            grp = order_paths_best(grp, cur, (0.0, 0.0) if cfg.get("return_home", True) else None, float(cfg.get("optimize_s", 1.5)))
        for p in grp:
            lines.append(f"G1 X{fmt(sx * p[0][0])} Y{fmt(sign * p[0][1])} F{tf}")
            travel += math.hypot(p[0][0] - cur[0], p[0][1] - cur[1])
            s_now = int((pen_s or {}).get(pen, s_down)) if pen is not None else s_down
            lines.append(f"M3 S{s_now}"); lines.append(f"G4 P{dwell:.3f}")
            first = True
            for x, y in p[1:]:
                lines.append(f"G1 X{fmt(sx * x)} Y{fmt(sign * y)}" + (f" F{ff}" if first else ""))
                first = False
            draw += path_len(p)
            lines += ["M5", f"G4 P{dwell:.3f}"]
            cur = p[-1]; n_paths += 1
    if cfg.get("return_home", True):
        lines.append(f"G1 X0 Y0 F{tf}")
        travel += math.hypot(*cur)
    lines += ["G4 P0.300", "M5"]
    t = draw / max(ff, 1) * 60 + travel / max(tf, 1) * 60 + n_paths * 2 * dwell
    return "\n".join(lines) + "\n", {"paths": n_paths, "draw_mm": round(draw), "travel_mm": round(travel),
                                      "seconds": round(t), "lines": len(lines), "pen_changes": changes}


_word = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


def analyze_gcode(text, cfg):
    """Analyse un G-code : commandes à envoyer, tracés pour l'aperçu, boîte englobante,
    durée estimée et avertissements. Les commandes `$…` sont refusées."""
    warns, cmds = [], []
    abs_mode = True; x = y = 0.0; ox = oy = 0.0; f = 1000.0; pen = False
    inch = False
    minx = miny = 1e9; maxx = maxy = -1e9
    draw = []; cur = None; t = 0.0; refused = 0; motion = None
    chain = []; accel = float(cfg.get("accel", 400)); jd = float(cfg.get("junction", 0.1))
    sign = -1.0 if cfg.get("invert_y") else 1.0
    sx = -1.0 if cfg.get("invert_x") else 1.0
    pen_changes = 0
    for ln, raw in enumerate(text.splitlines(), 1):
        l = re.sub(r"\(.*?\)", "", raw.split(";")[0]).strip().upper()
        if not l:
            continue
        if l.startswith("$") or l.startswith("%"):
            if l.startswith("$"):
                refused += 1
                if len(warns) < 8: warns.append(f"ligne {ln} ignorée (commande système refusée) : {raw.strip()[:40]}")
            continue
        if re.fullmatch(r"M0+", l.replace(" ", "")):          # pause « changer de stylo » (traitée par le logiciel)
            label = raw.split(";", 1)[1].strip() if ";" in raw else "Changer de stylo"
            cmds.append("M0 ;" + label); pen_changes += 1; pen = False; cur = None
            continue
        cmds.append(l)
        gs = [float(v) for v in re.findall(r"G\s*(\d+\.?\d*)", l)]
        ms = [int(float(v)) for v in re.findall(r"M\s*(\d+)", l)]
        vals = {k: float(v) for k, v in re.findall(r"([XYSFP])\s*([-+]?(?:\d+\.?\d*|\.\d+))", l)}
        if 20 in gs: inch = True; warns.append(f"ligne {ln} : G20 (pouces) non pris en charge")
        if 90 in gs: abs_mode = True
        if 91 in gs: abs_mode = False
        if 3 in ms or 4 in ms: pen = True
        if 5 in ms or 2 in ms or 30 in ms:
            pen = False; cur = None
        if any(m in (6, 7, 8, 9) for m in ms) and len(warns) < 8: warns.append(f"ligne {ln} : M{ms[0]} sans effet ici")
        if "F" in vals: f = max(1.0, vals["F"])
        if 92 in gs:
            if "X" in vals: ox = x - vals["X"]
            if "Y" in vals: oy = y - vals["Y"]
            continue
        if 4 in gs or 3 in ms or 4 in ms or 5 in ms or 2 in ms or 30 in ms:
            t += chain_time(chain, accel, jd); chain = []      # G4 / M3 / M5 : le planificateur se vide
        if 4 in gs and "P" in vals:
            t += vals["P"]
        for g in gs:
            if g in (0, 1, 2, 3): motion = int(g)
        if ("X" in vals or "Y" in vals) and motion is not None and 4 not in gs:
            nx = (vals["X"] if abs_mode else x + vals["X"]) if "X" in vals else x
            ny = (vals["Y"] if abs_mode else y + vals["Y"]) if "Y" in vals else y
            if motion in (2, 3) and len(warns) < 8:
                warns.append(f"ligne {ln} : arc G2/G3 tracé en ligne droite dans l'aperçu")
            d = math.hypot(nx - x, ny - y)
            chain.append((nx - x, ny - y, (5000.0 if motion == 0 else f) / 60.0))
            if pen and d > 0:
                if cur is None:
                    cur = [(sx * x, sign * y)]; draw.append(cur)
                cur.append((sx * nx, sign * ny))
            else:
                cur = None
            x, y = nx, ny
            minx, maxx = min(minx, sx * x), max(maxx, sx * x)
            miny, maxy = min(miny, sign * y), max(maxy, sign * y)
    t += chain_time(chain, accel, jd)
    W, H = cfg.get("area_w", 310), cfg.get("area_h", 256)
    out = False
    if maxx >= minx:
        if minx < -0.01 or miny < -0.01 or maxx > W + 0.01 or maxy > H + 0.01:
            out = True
            warns.insert(0, f"Hors zone ({fmt(minx)}…{fmt(maxx)} × {fmt(miny)}…{fmt(maxy)} mm) — zone 0…{fmt(W)} × 0…{fmt(H)}.")
    return {"commands": cmds, "preview": [[(round(a, 2), round(b, 2)) for a, b in p] for p in draw],
            "bbox": None if maxx < minx else [minx, miny, maxx, maxy], "seconds": round(t),
            "warnings": warns, "out_of_bounds": out, "refused": refused, "pen_changes": pen_changes}

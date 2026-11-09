"""Import de documents Word (.docx) et PDF : extraction du texte et des images, puis mise en page
sur des feuilles (pagination) avec les polices Hershey de la traceuse."""
import hashlib
import io
import re
import unicodedata

from PIL import Image

import vec

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MIN_IMG = 24            # px : en dessous, c'est un filet ou une puce, pas une image
MAX_IMG = 2200          # px : on réduit les très grandes images
MAX_IMAGES = 300
HEAD_SCALE = {1: 1.6, 2: 1.35, 3: 1.15}


class DocError(ValueError):
    pass


# ───────────────────────── extraction ─────────────────────────

def _clean(t):
    t = t.replace(" ", " ").replace("​", "").replace("­", "").replace("\t", "  ")
    t = re.sub(r"«\s*", '"', t)
    t = re.sub(r"\s*»", '"', t)
    t = "".join(unicodedata.normalize("NFKC", c) if "ﬀ" <= c <= "ﬆ" else c for c in t)
    return "".join(c for c in t if c == "\n" or unicodedata.category(c) not in ("Cc", "Cf"))


class Extract:
    def __init__(self):
        self.blocks = []        # {"t": "h"|"p"|"img"|"pb", ...}
        self.images = []        # {"png": bytes, "w": px, "h": px}
        self.by_hash = {}
        self.skipped = 0
        self.hf = {}            # en-tête / pied de page : {"header": rows, "footer": rows, "first": bool, ...}

    def text(self, t, kind="p", level=0, keep=False, bullet=False):
        t = _clean(t).strip("\n ")
        if not t.strip():
            return
        if bullet:
            t = "- " + t
        self.blocks.append({"t": kind, "text": t, "level": level, "keep": keep})

    def table(self, cols, rows, frac=1.0):
        """cols : largeurs relatives des colonnes ; rows : [[{"text", "span"}]] ; frac : part de la largeur du texte."""
        rows = [[{"text": _clean(c["text"]).strip(), "span": max(1, int(c.get("span", 1)))} for c in r] for r in rows]
        if not rows or not any(c["text"] for r in rows for c in r):
            return
        self.blocks.append({"t": "tbl", "cols": cols, "rows": rows, "frac": max(0.3, min(1.0, frac))})

    def page_break(self):
        if self.blocks and self.blocks[-1]["t"] != "pb":
            self.blocks.append({"t": "pb"})

    def image(self, blob, mm=None):
        """Ajoute une image (octets d'un fichier image). `mm` = largeur réelle de l'image dans le
        document d'origine (millimètres). Retourne True si elle est exploitable."""
        if len(self.images) >= MAX_IMAGES:
            self.skipped += 1
            return False
        h = hashlib.sha1(blob).hexdigest()
        if h not in self.by_hash:
            try:
                im = Image.open(io.BytesIO(blob))
                im.load()
                if im.width < MIN_IMG or im.height < MIN_IMG:
                    raise ValueError("trop petite")
                if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                    im = im.convert("RGBA")
                    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                    bg.alpha_composite(im)
                    im = bg
                im = im.convert("RGB") if im.mode not in ("L", "RGB") else im
                if max(im.size) > MAX_IMG:
                    r = MAX_IMG / max(im.size)
                    im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "PNG")
            except Exception:
                self.skipped += 1           # EMF/WMF, format exotique, minuscule…
                self.by_hash[h] = None
                return False
            self.images.append({"png": buf.getvalue(), "w": im.width, "h": im.height})
            self.by_hash[h] = len(self.images) - 1
        i = self.by_hash[h]
        if i is None:
            return False
        self.blocks.append({"t": "img", "i": i, "mm": mm})
        return True


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _para_cells(p_el):
    """Texte d'un paragraphe d'en-tête / pied de page, découpé aux tabulations.
    Les champs PAGE et NUMPAGES deviennent {PAGE} et {PAGES} (remplacés à la mise en page)."""
    cells, cur = [], [""]
    fld = None                      # champ complexe en cours : {"instr", "phase"}
    skip_ids = set()

    def put(t):
        cur[0] += t

    for el in p_el.iter():
        if id(el) in skip_ids:
            continue
        t = el.tag.rsplit("}", 1)[-1]
        if t == "fldSimple":
            instr = (el.get(f"{{{W_NS}}}instr") or "").upper()
            for sub in el.iter():
                if sub is not el:
                    skip_ids.add(id(sub))
            if "NUMPAGES" in instr:
                put("{PAGES}")
            elif "PAGE" in instr:
                put("{PAGE}")
            else:
                put("".join(el.itertext()))
        elif t == "fldChar":
            typ = el.get(f"{{{W_NS}}}fldCharType")
            if typ == "begin":
                fld = {"instr": "", "phase": "instr"}
            elif typ == "separate" and fld:
                fld["phase"] = "result"
                ins = fld["instr"].upper()
                if "NUMPAGES" in ins:
                    put("{PAGES}"); fld["skip"] = True
                elif "PAGE" in ins:
                    put("{PAGE}"); fld["skip"] = True
            elif typ == "end":
                fld = None
        elif t == "instrText":
            if fld is not None:
                fld["instr"] += el.text or ""
        elif t == "t":
            if fld is not None and (fld["phase"] == "instr" or fld.get("skip")):
                continue
            put(el.text or "")
        elif t in ("tab", "ptab"):
            if fld is not None and fld.get("skip"):
                continue
            cells.append(cur[0]); cur[0] = ""
        elif t in ("br", "cr"):
            put("\n")
    cells.append(cur[0])
    cells = [_clean(c).strip() for c in cells]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _hf_rows(part):
    """Lignes d'un en-tête ou pied de page Word : [{"cells": [texte…], "align": "left|center|right"}]."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    rows = []
    try:
        if part.is_linked_to_previous:
            return rows
        for child in part._element.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                al = {0: "left", 1: "center", 2: "right"}.get(int(Paragraph(child, part).alignment or 0), "left")
                cells = _para_cells(child)
                if any(cells):
                    rows.append({"cells": cells, "align": al})
            elif tag == "tbl":
                tbl = Table(child, part)
                for tr in tbl._tbl.tr_lst:
                    cells = []
                    for tc in tr.tc_lst:
                        txt = " ".join(c for pp in tc.iterchildren() if pp.tag.rsplit("}", 1)[-1] == "p"
                                       for c in _para_cells(pp) if c)
                        cells.append(_clean(txt).strip())
                    while cells and not cells[-1]:
                        cells.pop()
                    if any(cells):
                        rows.append({"cells": cells, "align": "left", "table": True})
    except Exception:
        return []
    return rows


def extract_docx(data):
    try:
        from docx import Document
        from docx.table import Table, _Cell
        from docx.text.paragraph import Paragraph
    except ImportError:
        raise DocError("Le module python-docx est absent (pip install python-docx).")
    try:
        d = Document(io.BytesIO(data))
    except Exception as e:
        raise DocError(f"Document Word illisible : {e}")
    ex = Extract()
    rel = d.part.related_parts
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, d)
            style = ""
            try:
                style = (para.style.name or "").lower()
            except Exception:
                pass
            level = 0
            m = re.search(r"(?:heading|titre)\s*(\d)", style)
            if m:
                level = int(m.group(1))
            elif style in ("title", "titre"):
                level = 1
            elif style in ("subtitle", "sous-titre"):
                level = 2
            numpr = child.find(".//{*}numPr")
            bullet = numpr is not None or style.startswith(("list", "liste"))
            br_page = False
            rids = []
            ext = None
            for el in child.iter():
                t = el.tag.rsplit("}", 1)[-1]
                if t == "extent":                                  # taille de l'image dans Word (EMU)
                    try: ext = int(el.get("cx"))
                    except (TypeError, ValueError): ext = None
                elif t == "blip":
                    rids.append((el.get(f"{{{R_NS}}}embed"), ext)); ext = None
                elif t == "imagedata":
                    rids.append((el.get(f"{{{R_NS}}}id"), None))
                elif t == "br" and el.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type") == "page":
                    br_page = True
            if child.find(".//{*}pageBreakBefore") is not None:
                ex.page_break()
            ex.text(para.text, "h" if level else "p", level, bullet=bullet and not level)
            for rid, cx in rids:
                part = rel.get(rid) if rid else None
                if part is not None:
                    ex.image(part.blob, cx / 36000.0 if cx and cx > 0 else None)      # EMU → mm
            if br_page:
                ex.page_break()
        elif tag == "tbl":
            tbl = Table(child, d)
            try:
                grid = [int(g.w) for g in tbl._tbl.tblGrid.gridCol_lst if g.w]
            except Exception:
                grid = []
            rows = []
            for tr in tbl._tbl.tr_lst:
                cells = []
                for tc in tr.tc_lst:
                    cell = _Cell(tc, tbl)
                    txt = "\n".join(p.text for p in cell.paragraphs)
                    if tc.vMerge == "continue":
                        txt = ""
                    cells.append({"text": txt, "span": tc.grid_span})
                if cells:
                    rows.append(cells)
            if not rows:
                continue
            ncols = max(sum(c["span"] for c in r) for r in rows)
            if len(grid) != ncols:
                grid = [1] * ncols
            frac = 1.0
            try:
                sec = d.sections[0]
                textw = int(sec.page_width - sec.left_margin - sec.right_margin)
                frac = sum(grid) / textw if len(grid) == ncols and grid[0] > 1000 else 1.0
            except Exception:
                pass
            ex.table(grid, rows, frac)
    try:
        sec = d.sections[0]
        first = bool(sec.different_first_page_header_footer)
        ex.hf = {"header": _hf_rows(sec.header), "footer": _hf_rows(sec.footer), "first": first,
                 "first_header": _hf_rows(sec.first_page_header) if first else None,
                 "first_footer": _hf_rows(sec.first_page_footer) if first else None}
    except Exception:
        ex.hf = {}
    return ex


def _split_paragraphs(text):
    """Le texte d'une page PDF arrive ligne par ligne : on regroupe en paragraphes (ligne vide, ou fin de
    phrase sur une ligne nettement plus courte que les autres)."""
    lines = [l.rstrip() for l in text.replace("\r", "").split("\n")]
    longest = max((len(l) for l in lines), default=0)
    paras, cur = [], []
    for l in lines:
        if not l.strip():
            if cur:
                paras.append(cur); cur = []
            continue
        cur.append(l)
        end = l.rstrip()[-1:]
        if (end in '.!?:»"' and len(l) < 0.8 * longest) or (len(l) < 0.5 * longest and end not in ",;-"):
            paras.append(cur); cur = []
    if cur:
        paras.append(cur)
    return ["\n".join(p) for p in paras]


def _pdf_tables(pl_page):
    """Tableaux à traits d'un PDF (pdfplumber) : [(bbox, cols, rows)]."""
    out = []
    try:
        found = pl_page.find_tables()
    except Exception:
        return out
    for t in found:
        try:
            rows_c = [r.cells for r in t.rows]
            if len(rows_c) < 2 or max(len(r) for r in rows_c) < 2:
                continue
            xs = sorted({round(c[k], 1) for r in rows_c for c in r if c for k in (0, 2)})
            edges = []
            for x in xs:                                   # fusionne les bords à moins de 1,5 pt
                if not edges or x - edges[-1] > 1.5:
                    edges.append(x)
            near = lambda x: min(range(len(edges)), key=lambda i: abs(edges[i] - x))
            cols = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
            text = t.extract()
            rows = []
            for r, tr in zip(rows_c, text):
                cells = []
                for c, tx in zip(r, tr):
                    if not c:
                        continue
                    a, b = near(c[0]), near(c[2])
                    cells.append({"text": " ".join((tx or "").split()), "span": max(1, b - a), "at": a})
                # une cellule vide manquante laisse un trou : on le comble pour garder les colonnes alignées
                pos, fixed = 0, []
                for c in sorted(cells, key=lambda c: c["at"]):
                    if c["at"] > pos:
                        fixed.append({"text": "", "span": c["at"] - pos})
                    fixed.append(c); pos = c["at"] + c["span"]
                if pos < len(cols):
                    fixed.append({"text": "", "span": len(cols) - pos})
                rows.append(fixed)
            if cols and rows:
                out.append((t.bbox, cols, rows))
        except Exception:
            continue
    return out


def extract_pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DocError("Le module pypdf est absent (pip install pypdf).")
    try:
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted and not r.decrypt(""):
            raise DocError("Ce PDF est protégé par un mot de passe.")
        pages = list(r.pages)
    except DocError:
        raise
    except Exception as e:
        raise DocError(f"PDF illisible : {e}")
    ex = Extract()
    try:
        import pdfplumber
        pl = pdfplumber.open(io.BytesIO(data))
        pl_pages = list(pl.pages)
    except Exception:
        pl, pl_pages = None, []
    for n, page in enumerate(pages):
        if n:
            ex.page_break()
        tables = _pdf_tables(pl_pages[n]) if n < len(pl_pages) else []
        if tables:
            plp = pl_pages[n]
            y = 0.0
            for bbox, cols, rows in sorted(tables, key=lambda t: t[0][1]) + [((0, plp.height, 0, plp.height), None, None)]:
                top = bbox[1]
                if top - y > 3:
                    try:
                        band = plp.crop((0, max(0, y - 0.5), plp.width, min(plp.height, top + 0.5))).extract_text() or ""
                    except Exception:
                        band = ""
                    for para in _split_paragraphs(_clean(band)):
                        ex.text(para)
                if cols:
                    ex.table(cols, rows, sum(cols) / max(1.0, plp.width - 144))
                y = bbox[3]
        else:
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""
            for para in _split_paragraphs(_clean(txt)):
                ex.text(para)
        try:
            imgs = list(page.images)
        except Exception:
            imgs = []
        for im in imgs:
            try:
                mm = None
                try:
                    mm = (float(im["x1"]) - float(im["x0"])) * 25.4 / 72.0                # points → mm
                except Exception:
                    pass
                ex.image(im.data, mm if mm and mm > 1 else None)
            except Exception:
                ex.skipped += 1
    if pl:
        pl.close()
    ex.pdf_pages = len(pages)
    return ex


def extract(data, name):
    n = name.lower()
    if n.endswith(".docx"):
        ex, kind = extract_docx(data), "docx"
    elif n.endswith(".pdf") or data[:5] == b"%PDF-":
        ex, kind = extract_pdf(data), "pdf"
    elif n.endswith(".doc"):
        raise DocError("Les anciens fichiers .doc ne sont pas lus : enregistre-le au format .docx (Word → Fichier → Enregistrer sous) ou en PDF.")
    else:
        raise DocError("Format non pris en charge (Word .docx ou PDF).")
    while ex.blocks and ex.blocks[-1]["t"] == "pb":
        ex.blocks.pop()
    return ex, kind


# ───────────────────────── mise en page ─────────────────────────

def _prepare(block, reflow):
    t = block["text"]
    if reflow and not block.get("keep"):
        t = re.sub(r"-\n(?=\w)", "-", t)          # coupure de fin de ligne : on garde le trait d'union
        t = " ".join(t.split())
    else:
        t = "\n".join(l.rstrip() for l in t.split("\n"))
    return t


HF_SCALE = 0.7          # taille des lettres de l'en-tête / pied de page par rapport au texte


def _hf_cells(row, W):
    """Répartit les cellules d'une ligne : [(texte, alignement, largeur max)]."""
    c = [x for x in row["cells"]]
    if len(c) <= 1:
        return [(c[0] if c else "", row.get("align", "left"), W)]
    if len(c) == 2:
        return [(c[0], "left", W * 0.48), (c[1], "right", W * 0.48)]
    rest = " ".join(x for x in c[2:] if x)
    return [(c[0], "left", W * 0.32), (c[1], "center", W * 0.32), (rest, "right", W * 0.32)]


def _hf_layout(rows, o, hs):
    """Hauteur de chaque ligne d'en-tête / pied de page (mm)."""
    out = []
    for r in rows:
        n = 1
        for txt, al, mw in _hf_cells(r, o["W"]):
            t = txt.replace("{PAGES}", "00").replace("{PAGE}", "00")
            if t.strip():
                n = max(n, len(vec.wrap_lines(t, o["font"], hs, o["letter_spacing"], max(5.0, mw))))
        out.append({"row": r, "h": hs * 1.35 + (n - 1) * hs * 1.5})
    return out


def _hf_zone(ex, o, zone, idx):
    """Lignes à écrire pour la zone 'header' ou 'footer' sur la feuille idx (la première peut être différente)."""
    hf = getattr(ex, "hf", None) or {}
    if not o.get("hf", True) or not hf:
        return []
    if idx == 0 and hf.get("first"):
        return hf.get("first_" + zone) or []
    return hf.get(zone) or []


def _hf_metrics(ex, o):
    """Place réservée en haut (hh) et en bas (fh) de chaque feuille."""
    hs = o["size"] * HF_SCALE
    hf = getattr(ex, "hf", None) or {}
    if not o.get("hf", True) or not hf:
        return 0.0, 0.0, hs
    def need(zone):
        best = 0.0
        for rows in (hf.get(zone), hf.get("first_" + zone)):
            if rows:
                best = max(best, sum(x["h"] for x in _hf_layout(rows, o, hs)))
        return best + (hs * 1.8 if best else 0.0)
    hh, fh = need("header"), need("footer")
    if o["H"] - hh - fh < 20:
        return 0.0, 0.0, hs
    return hh, fh, hs


def paginate(ex, o):
    """Place les blocs sur des feuilles. Retourne une liste de feuilles ; chaque feuille est une liste d'éléments
    {"k": "text", "text", "size", "y" (haut de la ligne, mm depuis la marge), "n" lignes}
    ou {"k": "img", "i", "w", "h", "y"}."""
    hh, fh, _hs = _hf_metrics(ex, o)
    W, H = o["W"], o["H"] - hh - fh
    size, ls = o["size"], o["line_spacing"]
    gap = size * o["gap"]
    pages, cur = [], []
    state = {"y": 0.0}

    def flush(force=False):
        nonlocal cur
        if cur or force:
            pages.append(cur)
        cur = []
        state["y"] = 0.0

    blocks = []
    for b in ex.blocks:                                 # tableaux désactivés : une ligne de texte par rangée
        if b["t"] == "tbl" and not o.get("tables", True):
            for r in b["rows"]:
                t = "   ".join(c["text"].replace("\n", " ") for c in r if c["text"])
                if t:
                    blocks.append({"t": "p", "text": t, "level": 0, "keep": True})
        else:
            blocks.append(b)
    for b in blocks:
        if b["t"] == "tbl":
            tsize = size * 0.85
            tls = min(ls, 1.5)
            pad = tsize * 0.45
            tw = W * b["frac"]
            tot = float(sum(b["cols"])) or 1.0
            xs = [0.0]
            for c in b["cols"]:
                xs.append(xs[-1] + c / tot * tw)
            rows = []
            for r in b["rows"]:
                ci, cells, maxn = 0, [], 1
                for c in r:
                    c0 = min(ci, len(b["cols"]) - 1)
                    c1 = min(ci + c["span"], len(b["cols"]))
                    ci += c["span"]
                    if c1 <= c0:
                        continue
                    x0, x1 = xs[c0], xs[c1]
                    txt = "\n".join(" ".join(q.split()) for q in c["text"].split("\n")) if c["text"] else ""
                    n = len(vec.wrap_lines(txt, o["font"], tsize, o["letter_spacing"], max(5.0, x1 - x0 - 2 * pad))) if txt else 1
                    maxn = max(maxn, n)
                    cells.append({"x0": x0, "x1": x1, "text": txt})
                rows.append({"h": 2 * pad + tsize * 1.35 + (maxn - 1) * tsize * tls, "cells": cells})
            if cur and state["y"] + rows[0]["h"] > H:
                flush()
            chunk = None

            def open_chunk(first):
                nonlocal chunk
                chunk = {"k": "tbl", "y": state["y"], "xs": xs, "tsize": tsize, "tls": tls, "pad": pad, "rows": [], "used": 0.0}
                cur.append(chunk)
                if first is not None:
                    chunk["rows"].append({**first, "dy": 0.0}); chunk["used"] = first["h"]

            open_chunk(None)
            for ri, row in enumerate(rows):
                if chunk["rows"] and state["y"] + chunk["used"] + row["h"] > H:
                    flush()
                    open_chunk(rows[0] if len(rows) > 3 and ri > 0 else None)      # l'en-tête est répété
                chunk["rows"].append({**row, "dy": chunk["used"]})
                chunk["used"] += row["h"]
            state["y"] += chunk["used"] + gap
            continue
        if b["t"] == "pb":
            if o["breaks"] and cur:
                flush()
            continue
        if b["t"] == "img":
            if not o["images"]:
                continue
            iw, ih = ex.images[b["i"]]["w"], ex.images[b["i"]]["h"]
            real = b.get("mm")
            w = min(W, real) if (o.get("img_doc") and real) else min(W, o["img_w"])
            h = w * ih / iw
            if h > H:
                w, h = w * H / h, H
            if cur and state["y"] + h > H:
                flush()
            cur.append({"k": "img", "i": b["i"], "w": w, "h": h, "y": state["y"]})
            state["y"] += h + gap
            continue
        head = b["t"] == "h"
        sz = size * HEAD_SCALE.get(b.get("level", 1), 1.0) if head else size
        pitch = sz * ls
        text = _prepare(b, o["reflow"])
        if not text.strip():
            continue
        lines = vec.wrap_lines(text, o["font"], sz, o["letter_spacing"], W)
        if head and cur:
            state["y"] += sz * 0.6
        if head and cur and state["y"] + sz * 1.4 + 2 * pitch > H:    # un titre ne reste pas seul en bas de feuille
            flush()
        while lines:
            room = H - state["y"]
            fit = int((room - sz * 1.35) // pitch) + 1 if room >= sz * 1.35 else 0
            if fit < 1:
                if cur:
                    flush(); continue
                fit = 1
            chunk, lines = lines[:fit], lines[fit:]
            txt = ""
            for i, (l, hard) in enumerate(chunk):
                txt += ("" if i == 0 else ("\n" if hard else " ")) + l
            cur.append({"k": "text", "text": txt, "size": sz, "y": state["y"], "n": len(chunk), "head": head})
            state["y"] += sz * 1.35 + (len(chunk) - 1) * pitch
            if lines:
                flush()
        state["y"] += (sz * 0.35 if head else gap)
    flush()
    if not pages:
        pages = [[]]
    return pages


def _merge(segs, horizontal):
    """Fusionne les segments alignés qui se touchent (moins de levées de stylo)."""
    by = {}
    for fixed, a, b in segs:
        by.setdefault(round(fixed, 2), []).append((a, b))
    out = []
    for fixed, lst in by.items():
        lst.sort()
        a, b = lst[0]
        for a2, b2 in lst[1:]:
            if a2 <= b + 0.05:
                b = max(b, b2)
            else:
                out.append((fixed, a, b)); a, b = a2, b2
        out.append((fixed, a, b))
    return [[(a, f), (b, f)] if horizontal else [(f, a), (f, b)] for f, a, b in out]


def _render_table(e, o, missing):
    """Un tableau = un seul objet : quadrillage + texte des cellules (tracés en ligne)."""
    m, pad, tsize = o["margin"], e["pad"], e["tsize"]
    hs, vs, paths = set(), set(), []
    for r in e["rows"]:
        top, bot = e["y"] + r["dy"], e["y"] + r["dy"] + r["h"]
        for c in r["cells"]:
            hs.add((round(top, 3), round(c["x0"], 3), round(c["x1"], 3)))
            hs.add((round(bot, 3), round(c["x0"], 3), round(c["x1"], 3)))
            vs.add((round(c["x0"], 3), round(top, 3), round(bot, 3)))
            vs.add((round(c["x1"], 3), round(top, 3), round(bot, 3)))
            if not c["text"]:
                continue
            tp, miss = vec.text_to_paths(c["text"], o["font"], tsize, e["tls"], o["letter_spacing"], "left", 0, 0,
                                         max(5.0, c["x1"] - c["x0"] - 2 * pad), 1, 0, 0, 0, raw=True)
            missing.update(miss)
            ox, oy = c["x0"] + pad, top + pad + tsize
            paths += [[(x + ox, y + oy) for x, y in p] for p in tp]
    grid = _merge(hs, True) + _merge(vs, False)
    allp = grid + paths
    if not allp:
        return None
    x0, y0, x1, y1 = vec.bbox(allp)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    cen = [[[round(x - cx, 2), round(y - cy, 2)] for x, y in p] for p in allp]
    return {"kind": "shape", "name": "Tableau", "cx": round(m + cx, 2), "cy": round(m + cy, 2),
            "w": round(x1 - x0, 2), "h": round(y1 - y0, 2), "paths": cen}


def _render_hf(ex, o, idx, total, hh, fh, hs, missing):
    """Textes de l'en-tête et du pied de page de la feuille idx."""
    m, W, items = o["margin"], o["W"], []
    for zone in ("header", "footer"):
        lay = _hf_layout(_hf_zone(ex, o, zone, idx), o, hs)
        if not lay:
            continue
        y = 0.0 if zone == "header" else o["H"] - sum(x["h"] for x in lay)
        for x in lay:
            for txt, al, mw in _hf_cells(x["row"], W):
                txt = txt.replace("{PAGES}", str(total)).replace("{PAGE}", str(idx + 1))
                if not txt.strip():
                    continue
                paths, miss = vec.text_to_paths(txt, o["font"], hs, 1.5, o["letter_spacing"], "left", 0, 0, max(5.0, mw), 1, 0, 0, 0, raw=True)
                missing.update(miss)
                if not paths:
                    continue
                x0, y0, x1, y1 = vec.bbox(paths)
                w = x1 - x0
                cx_sheet = {"left": w / 2, "center": W / 2, "right": W - w / 2}[al]
                cx, cy = x0 / 2 + x1 / 2, y0 / 2 + y1 / 2
                cen = [[(px - cx, py - cy) for px, py in p] for p in paths]
                items.append({"kind": "text", "text": txt, "size": round(hs, 3), "head": False,
                              "cx": round(m + cx_sheet, 2), "cy": round(m + y + hs + cy, 2), "w": round(w, 2), "h": round(y1 - y0, 2),
                              "paths": [[[round(px, 2), round(py, 2)] for px, py in p] for p in cen]})
            y += x["h"]
    return items


def render_page(ex, pages, idx, o):
    """Géométrie réelle d'une feuille (positions en mm sur le papier, marge comprise)."""
    idx = max(0, min(idx, len(pages) - 1))
    hh, fh, hs = _hf_metrics(ex, o)
    m = o["margin"] + hh                      # le texte commence sous l'en-tête
    items, missing = [], set()
    for e in pages[idx]:
        if e["k"] == "img":
            items.append({"kind": "image", "i": e["i"], "w": round(e["w"], 2), "h": round(e["h"], 2),
                          "cx": round(o["margin"] + o["W"] / 2, 2), "cy": round(m + e["y"] + e["h"] / 2, 2)})
            continue
        if e["k"] == "tbl":
            it = _render_table(e, {**o, "margin": o["margin"]}, missing)
            if it:
                it["cy"] = round(it["cy"] + hh, 2)
                items.append(it)
            continue
        paths, miss = vec.text_to_paths(e["text"], o["font"], e["size"], o["line_spacing"], o["letter_spacing"],
                                        "left", 0, 0, o["W"], 1, 0, 0, 0, raw=True)
        missing.update(miss)
        if not paths:
            continue
        x0, y0, x1, y1 = vec.bbox(paths)
        base = e["y"] + e["size"]
        cx, cy = x0 / 2 + x1 / 2, y0 / 2 + y1 / 2
        cen = [[(x - cx, y - cy) for x, y in p] for p in paths]
        items.append({"kind": "text", "text": e["text"], "size": round(e["size"], 3), "head": e["head"],
                      "cx": round(o["margin"] + cx, 2), "cy": round(m + base + cy, 2), "w": round(x1 - x0, 2), "h": round(y1 - y0, 2),
                      "paths": [[[round(x, 2), round(y, 2)] for x, y in p] for p in cen]})
    if hh or fh:
        items += _render_hf(ex, o, idx, len(pages), hh, fh, hs, missing)
    rot = int(o.get("rotate", 0)) % 360
    if rot:                                    # mise en page faite sur un papier tourné, puis remise à plat
        pw, ph = o["pw"], o["ph"]
        for it in items:
            x, y = it["cx"], it["cy"]
            it["cx"], it["cy"] = {90: (pw - y, x), 180: (pw - x, ph - y), 270: (y, ph - x)}[rot]
            it["cx"], it["cy"] = round(it["cx"], 2), round(it["cy"], 2)
            it["rot"] = rot if rot <= 180 else rot - 360
    return {"page": idx, "pages": len(pages), "items": items, "missing": sorted(missing)}

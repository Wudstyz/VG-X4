"""Atelier Traceuse : pilotage de la traceuse VigoTec VG-X4 (GRBL) — fenêtre native + serveur HTTP local."""
import json, os, re, sys, threading, time, traceback, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import grbl, vec, shapes, docimport

SUPPORT = os.path.expanduser("~/Library/Application Support/VG-X4")
CONFIG = os.path.join(SUPPORT, "settings.json")
os.makedirs(SUPPORT, exist_ok=True)

DEFAULTS = {
    "port": "", "baud": 115200,
    "area_w": 310.0, "area_h": 256.0, "paper_w": 297.0, "paper_h": 210.0,
    "pen_down_s": 905, "pen_settle_ms": 300, "draw_feed": 1200, "travel_feed": 2500, "jog_feed": 2000,
    "invert_y": False, "invert_x": False, "auto_reconnect": True, "margin": 10.0,
    "keep_awake": True, "notify": True, "backlash_x": 0.0, "backlash_y": 0.0,
    "pens": [{"name": "Noir", "color": "#1d1d1f"}, {"name": "Rouge", "color": "#d70015"},
             {"name": "Bleu", "color": "#0071e3"}, {"name": "Vert", "color": "#1a9d4b"}],
    "return_home": True, "optimize": True, "join_tol": 0.4, "simplify": 0.05,
    "sim_speed": 10.0, "ui": {},
}
LIMITS = {"baud": (1200, 1000000), "area_w": (10, 2000), "area_h": (10, 2000), "paper_w": (10, 2000),
          "paper_h": (10, 2000), "pen_down_s": (0, 1000), "pen_settle_ms": (50, 3000),
          "draw_feed": (100, 5000), "travel_feed": (100, 5000), "jog_feed": (100, 5000),
          "join_tol": (0, 5), "simplify": (0.0, 1.0), "sim_speed": (0.5, 200), "margin": (0, 100),
          "backlash_x": (0, 3), "backlash_y": (0, 3)}

_cfg_lock = threading.Lock()
_cfg = dict(DEFAULTS)


def load_cfg():
    try:
        with open(CONFIG) as f:
            d = json.load(f)
        for k, v in d.items():
            if k in DEFAULTS:
                _cfg[k] = v
    except Exception:
        pass


def save_cfg():
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_cfg, f, indent=1, ensure_ascii=False)
    os.replace(tmp, CONFIG)


def cfg():
    return _cfg


def update_cfg(d):
    with _cfg_lock:
        for k, v in d.items():
            if k not in DEFAULTS:
                continue
            if k in LIMITS:
                v = float(v); lo, hi = LIMITS[k]
                v = min(hi, max(lo, v))
                if isinstance(DEFAULTS[k], int):
                    v = int(round(v))
            elif isinstance(DEFAULTS[k], bool):
                v = bool(v)
            elif k == "ui":
                v = dict(v) if isinstance(v, dict) else {}
            elif k == "pens":
                import re as _re
                src = v if isinstance(v, list) else []
                v = []
                for i, d in enumerate(src[:8]):
                    d = d if isinstance(d, dict) else {}
                    c = str(d.get("color", "#1d1d1f"))
                    try: sv = int(float(d.get("s") or 0))
                    except (TypeError, ValueError): sv = 0
                    v.append({"name": str(d.get("name") or f"Stylo {i + 1}")[:24],
                              "color": c if _re.fullmatch(r"#[0-9a-fA-F]{6}", c) else "#1d1d1f",
                              "s": min(1000, max(0, sv))})
                if not v:
                    v = DEFAULTS["pens"]
            else:
                v = str(v)
            _cfg[k] = v
        save_cfg()
    return _cfg


machine = grbl.Machine(cfg)
uploads = {}                       # id -> {"kind", "name", "data", "gray"}
docs = {}                          # id -> {"ex", "name", "kind", "cache"} (documents Word/PDF importés)
fonts_cache = None


class ApiError(Exception):
    pass


def font_list():
    global fonts_cache
    if fonts_cache is None:
        res = [{"id": k, "name": v, "group": "Trait simple (recommandé)"} for k, v in vec.HERSHEY_NAMES.items()]
        sysf = vec.system_fonts()
        res += [{"id": f["id"], "name": f["name"], "group": "Polices Mac manuscrites (ligne centrale)"}
                for f in sysf if f["hand"]]
        res += [{"id": f["id"], "name": f["name"], "group": "Autres polices du Mac"} for f in sysf if not f["hand"]]
        fonts_cache = res
    return fonts_cache


def parse_groups(raw):
    """Accepte [[polylignes…], …] ou [{"paths": […], "pen": n}, …] ; retourne (groupes, stylos|None)."""
    groups, pens = [], []
    for g in raw or []:
        if isinstance(g, dict):
            groups.append([[(float(x), float(y)) for x, y in p] for p in g.get("paths", [])])
            pens.append(int(g.get("pen", 0)))
        else:
            groups.append([[(float(x), float(y)) for x, y in p] for p in g]); pens.append(0)
    if groups and len(set(pens)) > 1:
        order = sorted(range(len(groups)), key=lambda i: (pens[i], i))      # regroupe par stylo
        groups = [groups[i] for i in order]; pens = [pens[i] for i in order]
        return groups, pens
    return groups, None


def eff_cfg():
    """Réglages + paramètres du planificateur lus sur la carte + zone réellement utilisable."""
    c = dict(cfg())
    g = machine.grbl_cfg or {}
    if g.get(120): c["accel"] = float(g[120])
    if g.get(11): c["junction"] = float(g[11])
    r = machine.reach()
    if r.get("w"): c["area_w"] = min(c["area_w"], r["w"])
    if r.get("h"): c["area_h"] = min(c["area_h"], r["h"])
    return c


def time_factor():
    """Rapport durée réelle / durée estimée, médiane des tracés passés sur la vraie machine (1.0 si pas assez de données)."""
    ratios = []
    for j in read_history(60):
        if j.get("state") == "done" and not j.get("sim") and j.get("est", 0) >= 20 and j.get("duration", 0) >= 20 \
                and not str(j.get("name", "")).startswith(("Essai", "Cadre", "Alignement")):
            ratios.append(j["duration"] / j["est"])
    if len(ratios) < 3:
        return 1.0, len(ratios)
    ratios.sort()
    m = ratios[len(ratios) // 2]
    return min(2.0, max(0.6, m)), len(ratios)


def pen_names():
    return {i: d.get("name", f"Stylo {i + 1}") for i, d in enumerate(cfg().get("pens", []))}


def pen_levels():
    """Valeur S du stylo baissé propre à chaque stylo (0 = valeur générale)."""
    return {i: int(d["s"]) for i, d in enumerate(cfg().get("pens", [])) if d.get("s")}


def build_gcode(groups, name="", pens=None):
    c = cfg()
    paths_n = sum(len(g) for g in groups)
    if not paths_n:
        raise ApiError("Rien à tracer.")
    text, st = vec.groups_to_gcode(groups, c, header_comment=f"Atelier Traceuse — {name or 'dessin'} — {time.strftime('%Y-%m-%d %H:%M')}",
                                   pens=pens, pen_names=pen_names(), pen_s=pen_levels())
    a = vec.analyze_gcode(text, eff_cfg())
    f, nf = time_factor()
    st["seconds_model"] = a["seconds"]; st["seconds"] = round(a["seconds"] * f); st["time_factor"] = round(f, 2)
    st["warnings"] = a["warnings"]; st["out_of_bounds"] = a["out_of_bounds"]; st["bbox"] = a["bbox"]
    return {"gcode": text, "stats": st}


AUTOSAVE = os.path.join(SUPPORT, "autosave.vg4")
LIBDIR = os.path.join(SUPPORT, "library")
BACKUPS = os.path.join(SUPPORT, "backups")
HISTORY = os.path.join(SUPPORT, "history.jsonl")


def backup_grbl(force=False):
    """Écrit les réglages $ de la carte dans un fichier ; réutilise la dernière sauvegarde si elle date de moins de 2 min."""
    os.makedirs(BACKUPS, exist_ok=True)
    files = sorted(f for f in os.listdir(BACKUPS) if f.startswith("grbl_") and f.endswith(".txt"))
    if files and not force:
        last = os.path.join(BACKUPS, files[-1])
        if time.time() - os.path.getmtime(last) < 120:
            return last
    st = machine.grbl_cfg or machine.read_grbl_settings()
    if not st:
        raise ApiError("Impossible de lire les réglages de la carte ($$).")
    path = os.path.join(BACKUPS, time.strftime("grbl_%Y%m%d_%H%M%S.txt"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"; Réglages GRBL sauvegardés par Atelier Traceuse le {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        for k in sorted(st):
            v = st[k]
            f.write(f"${k}={int(v) if float(v).is_integer() else v}\n")
    return path


def latest_backup():
    if not os.path.isdir(BACKUPS):
        return None
    files = sorted(f for f in os.listdir(BACKUPS) if f.startswith("grbl_") and f.endswith(".txt"))
    if not files:
        return None
    path = os.path.join(BACKUPS, files[-1])
    vals = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\$(\d+)=([-+]?[\d.]+)", line.strip())
            if m: vals[int(m.group(1))] = float(m.group(2))
    return {"file": path, "name": files[-1], "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
            "settings": vals}


_builtin_cache = None


def builtin_items():
    global _builtin_cache
    if _builtin_cache is None:
        res = []
        for gid, (name, cat, fn, spec) in shapes.GENS.items():
            paths, w, h = shapes.generate(gid)
            res.append({"id": "b:" + gid, "name": name, "cat": cat, "kind": "param", "builtin": True, "gen": gid,
                        "params": shapes.defaults(gid), "w": round(w, 1), "h": round(h, 1),
                        "preview": shapes.simplify_preview(paths)})
        _builtin_cache = res
    return _builtin_cache


def user_items(full=False):
    res = []
    if os.path.isdir(LIBDIR):
        for fn in sorted(os.listdir(LIBDIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(LIBDIR, fn), encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if not full:
                d = {k: v for k, v in d.items() if k != "paths"}
            res.append(d)
    return sorted(res, key=lambda d: d.get("created", 0), reverse=True)


def lib_get(lid):
    if str(lid).startswith("b:"):
        return next((d for d in builtin_items() if d["id"] == lid), None)
    if not re.fullmatch(r"[0-9a-f]{32}", str(lid)):
        return None
    try:
        with open(os.path.join(LIBDIR, lid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def lib_save(body):
    os.makedirs(LIBDIR, exist_ok=True)
    lid = str(body.get("id") or "")
    old = None
    if lid:
        if lid.startswith("b:") or not re.fullmatch(r"[0-9a-f]{32}", lid):
            raise ApiError("Les formes d'origine ne sont pas modifiables (duplique-les d'abord).")
        old = lib_get(lid)
        if not old:
            raise ApiError("Élément introuvable.")
    else:
        lid = uuid.uuid4().hex
    d = dict(old or {"id": lid, "created": time.time(), "kind": "paths"})
    d["name"] = (str(body.get("name") or d.get("name") or "Sans nom").strip() or "Sans nom")[:60]
    d["cat"] = (str(body.get("cat") or d.get("cat") or "Mes dessins").strip() or "Mes dessins")[:40]
    d["builtin"] = False
    if body.get("gen"):
        gid = str(body["gen"])
        if gid not in shapes.GENS:
            raise ApiError("Forme inconnue.")
        paths, w, h = shapes.generate(gid, body.get("params") or {})
        d.update(kind="param", gen=gid, params={k: float(v) for k, v in (body.get("params") or {}).items() if k in shapes.defaults(gid)})
        d.pop("paths", None)
    elif body.get("paths") is not None:
        raw = [[(float(x), float(y)) for x, y in p] for p in body["paths"] if len(p) >= 2]
        if not raw:
            raise ApiError("Dessin vide.")
        if sum(len(p) for p in raw) > 300000:
            raise ApiError("Dessin trop lourd pour la bibliothèque.")
        paths, w, h = vec.center_paths(raw)
        d.update(kind="paths", paths=[[[round(x, 3), round(y, 3)] for x, y in p] for p in paths])
        d.pop("gen", None); d.pop("params", None)
    else:
        if old is None:
            raise ApiError("Rien à enregistrer.")
        if old.get("kind") == "param":
            paths, w, h = shapes.generate(old["gen"], old.get("params"))
        else:
            paths = [[(x, y) for x, y in p] for p in old.get("paths", [])]
            xs = [q[0] for p in paths for q in p] or [0]; ys = [q[1] for p in paths for q in p] or [0]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
    d["w"], d["h"] = round(w, 1), round(h, 1)
    d["preview"] = shapes.simplify_preview(paths)
    tmp = os.path.join(LIBDIR, lid + ".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(LIBDIR, lid + ".json"))
    return {k: v for k, v in d.items() if k != "paths"}


def lib_delete(lid):
    if not re.fullmatch(r"[0-9a-f]{32}", str(lid)):
        raise ApiError("Les formes d'origine ne sont pas supprimables.")
    try:
        os.remove(os.path.join(LIBDIR, lid + ".json"))
    except OSError:
        raise ApiError("Élément introuvable.")


def read_history(n=300):
    out = []
    try:
        with open(HISTORY, encoding="utf-8") as f:
            for line in f:
                try: out.append(json.loads(line))
                except ValueError: pass
    except OSError:
        pass
    return out[-n:][::-1]


def write_project(path, text):
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not path.lower().endswith(".vg4"):
        raise ApiError("Le fichier doit se terminer par .vg4")
    if not path.startswith(os.path.expanduser("~") + os.sep):
        raise ApiError("Enregistrement refusé hors du dossier personnel.")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def run_job(gcode, name, force, dry=False, nocomp=False):
    c = eff_cfg()
    comp = False
    if dry:
        gcode = vec.dry_gcode(gcode)
        name = "Essai à blanc — " + (name or "tracé")
    elif not nocomp and (c.get("backlash_x") or c.get("backlash_y")):
        comp = True
    a = vec.analyze_gcode(gcode, c)                      # limites et avertissements : sur le programme d'origine
    if not a["commands"]:
        raise ApiError("G-code vide.")
    if a["out_of_bounds"] and not force:
        raise ApiError(a["warnings"][0] + " Impression refusée (le papier est-il bien placé ? Sinon coche « ignorer »).")
    if any("G20" in w for w in a["warnings"]):
        raise ApiError("G20 (pouces) non pris en charge.")
    if not machine.connected:
        raise ApiError("Connecte la machine (ou lance la simulation) d'abord.")
    cmds = a["commands"]
    if comp:                                             # compensation du jeu appliquée juste avant l'envoi
        cmds = vec.analyze_gcode(vec.apply_backlash(gcode, c.get("backlash_x", 0), c.get("backlash_y", 0)), c)["commands"]
    machine.start_job(cmds, name=name or "tracé", est=a["seconds"])
    return {"lines": len(cmds), "seconds": a["seconds"], "refused": a["refused"], "warnings": a["warnings"]}


def vec_response(paths, w, h, extra=None):
    d = {"paths": [[[round(x, 3), round(y, 3)] for x, y in p] for p in paths], "w": round(w, 3), "h": round(h, 3),
         "n": len(paths), "points": sum(len(p) for p in paths)}
    if extra: d.update(extra)
    return d


def route(method, path, q, body, headers):
    P = lambda k, d=None: body.get(k, d) if isinstance(body, dict) else d
    if method == "GET":
        if path == "/api/state":
            return machine.snapshot(float(q.get("after", ["0"])[0]))
        if path == "/api/ports":
            return {"ports": grbl.list_serial_ports()}
        if path == "/api/settings":
            return cfg()
        if path == "/api/fonts":
            return {"fonts": font_list()}
        if path == "/api/lib":
            return {"gens": shapes.gens_schema(), "items": builtin_items() + user_items()}
        if path == "/api/history":
            return {"jobs": read_history()}
        if path == "/api/grbl/backup":
            return latest_backup() or {}
        if path == "/api/autosave":
            try:
                with open(AUTOSAVE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        raise ApiError("inconnu")
    # POST
    if path == "/api/settings":
        return update_cfg(body)
    if path == "/api/connect":
        c = cfg()
        port = P("port") or c.get("port")
        sim = bool(P("sim"))
        machine.connect(port, int(P("baud", c.get("baud", 115200))), sim=sim)
        if not sim:
            update_cfg({"port": port})
        return {"ok": True}
    if path == "/api/disconnect":
        machine.disconnect(); return {"ok": True}
    if path == "/api/jog":
        machine.jog(float(P("dx", 0)), float(P("dy", 0)), P("feed")); return {"ok": True}
    if path == "/api/pen":
        machine.pen(bool(P("down"))); return {"ok": True}
    if path == "/api/pen_test":
        machine.pen_test(int(P("s", 905))); return {"ok": True}
    if path == "/api/zero":
        machine.set_zero(); return {"ok": True}
    if path == "/api/goto":
        machine.goto(float(P("x", 0)), float(P("y", 0))); return {"ok": True}
    if path == "/api/raw":
        machine.raw(str(P("cmd", ""))); return {"ok": True}
    if path == "/api/unlock":
        machine.unlock(); return {"ok": True}
    if path == "/api/reset":
        machine.reset(); return {"ok": True}
    if path == "/api/pause":
        machine.pause(); return {"ok": True}
    if path == "/api/resume":
        machine.resume(); return {"ok": True}
    if path == "/api/lib/save":
        return lib_save(body)
    if path == "/api/lib/delete":
        lib_delete(str(P("id", ""))); return {"ok": True}
    if path == "/api/vec/shape":
        inl = P("inline")
        if inl:
            raw = [[(float(x), float(y)) for x, y in p] for p in inl if len(p) >= 2]
            paths, w, h = vec.center_paths(raw) if raw else ([], 0.0, 0.0)
            return vec_response(paths, w, h)
        lid = P("lib")
        if lid:
            d = lib_get(lid)
            if not d:
                raise ApiError("Élément de bibliothèque introuvable.")
            if d.get("kind") == "param":
                paths, w, h = shapes.generate(d["gen"], d.get("params"))
            else:
                raw = [[(float(x), float(y)) for x, y in p] for p in d.get("paths", [])]
                paths, w, h = vec.center_paths(raw) if raw else ([], 0.0, 0.0)
            return vec_response(paths, w, h)
        paths, w, h = shapes.generate(str(P("gen", "")), body)
        return vec_response(paths, w, h)
    if path == "/api/limit":
        ax = str(P("axis", ""))
        if ax == "clear": machine.clear_limits()
        else: machine.mark_limit(ax)
        return {"reach": machine.reach()}
    if path == "/api/estimate":
        groups, pens = parse_groups(P("groups", []))
        if not sum(len(g) for g in groups):
            return {"seconds": 0}
        text, st = vec.groups_to_gcode(groups, cfg(), pens=pens, pen_names=pen_names(), pen_s=pen_levels())
        a = vec.analyze_gcode(text, eff_cfg()); f, nf = time_factor()
        return {"seconds": round(a["seconds"] * f), "model": a["seconds"], "factor": round(f, 2), "samples": nf,
                "out_of_bounds": a["out_of_bounds"]}
    if path == "/api/job/continue":
        machine.continue_job(); return {"ok": True}
    if path == "/api/job/resume":
        machine.resume_job(int(P("path", 1)), bool(P("force"))); return {"ok": True}
    if path == "/api/job/forget":
        machine.forget_job(); return {"ok": True}
    if path == "/api/grbl/read":
        return {"settings": machine.read_grbl_settings()}
    if path == "/api/grbl/steps":
        b = backup_grbl()
        r = machine.set_steps(str(P("axis", "")), float(P("value", 0)))
        r["backup"] = b; return r
    if path == "/api/grbl/backup":
        return {"file": backup_grbl(force=True)}
    if path == "/api/grbl/restore":
        lb = latest_backup()
        if not lb:
            raise ApiError("Aucune sauvegarde trouvée.")
        res = {}
        for axis, n in (("x", 100), ("y", 101)):
            if n in lb["settings"]:
                res[axis] = machine.set_steps(axis, lb["settings"][n])
        return {"restored": res, "backup": lb["name"]}
    if path == "/api/history/clear":
        try: os.remove(HISTORY)
        except OSError: pass
        return {"ok": True}
    if path == "/api/autosave":
        text = json.dumps(body, ensure_ascii=False)
        tmp = AUTOSAVE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, AUTOSAVE)
        return {"ok": True}
    if path == "/api/project/save":
        return {"path": write_project(P("path", ""), str(P("text", "")))}
    if path == "/api/upload":
        kind = q.get("kind", ["image"])[0]
        name = unquote(headers.get("X-Filename", "fichier"))
        data = body
        uid = uuid.uuid4().hex
        if kind == "image":
            try:
                gray = vec.load_gray(data)
            except Exception as e:
                raise ApiError(f"Image illisible : {e}")
            uploads[uid] = {"kind": "image", "name": name, "gray": gray}
            return {"id": uid, "px_w": gray.width, "px_h": gray.height, "otsu": int(vec.otsu(gray))}
        if kind == "doc":
            try:
                ex, dkind = docimport.extract(data, name)
            except docimport.DocError as e:
                raise ApiError(str(e))
            texts = [b for b in ex.blocks if b["t"] in ("p", "h")]
            ntab = sum(1 for b in ex.blocks if b["t"] == "tbl")
            nimg = sum(1 for b in ex.blocks if b["t"] == "img")
            if not texts and not nimg and not ntab:
                raise ApiError("Aucun texte ni image trouvés dans ce document" +
                               (" (PDF scanné ? le texte est une image sans texte à lire)." if dkind == "pdf" else "."))
            docs[uid] = {"ex": ex, "name": name, "kind": dkind, "cache": None}
            while len(docs) > 4:
                docs.pop(next(iter(docs)))
            return {"id": uid, "name": name, "kind": dkind, "paragraphs": len(texts), "images": nimg,
                    "unique_images": len(ex.images), "skipped": ex.skipped, "tables": ntab,
                    "chars": sum(len(b["text"]) for b in texts) + sum(len(c["text"]) for b in ex.blocks if b["t"] == "tbl" for r in b["rows"] for c in r),
                    "preview": "\n\n".join(b["text"] for b in texts)[:1200],
                    "src_pages": getattr(ex, "pdf_pages", 0)}
        uploads[uid] = {"kind": "svg", "name": name, "data": data}
        return {"id": uid}
    if path == "/api/vec/text":
        text = str(P("text", ""))
        if not text.strip():
            return vec_response([], 0, 0)
        font = P("font", "scripts")
        size = float(P("size", 8)); ls = float(P("line_spacing", 1.9))
        if font.startswith("sys:"):
            ppm = 10.0
            align = P("align", "left")
            mask = vec.system_text_mask(text, font, size, ls, align, ppm)
            paths, w, h = vec.mask_to_item(mask, ppm, P("trace", "skeleton"), 128, float(P("hatch", 0.5)), 45.0)
            return vec_response(paths, w, h)
        paths, w, h, miss = vec.text_to_paths(
            text, font, size, ls, float(P("letter_spacing", 0)), P("align", "left"), float(P("slant", 0)),
            float(P("jitter", 0)), float(P("max_width", 0)), int(P("seed", 1)),
            float(P("word_spacing", 0)), float(P("wobble", 0)), float(P("smooth", 0)))
        return vec_response(paths, w, h, {"missing": miss})
    if path == "/api/doc/layout":
        d = docs.get(P("id"))
        if not d: raise ApiError("Document inconnu (recharge le fichier).")
        margin = max(0.0, float(P("margin", 10)))
        o = {"font": str(P("font", "scripts")), "size": max(1.0, float(P("size", 4))),
             "line_spacing": max(1.0, float(P("line_spacing", 1.7))), "letter_spacing": float(P("letter_spacing", 0)),
             "gap": max(0.0, float(P("gap", 0.6))), "reflow": bool(P("reflow", True)), "breaks": bool(P("breaks", True)),
             "tables": bool(P("tables", True)), "images": bool(P("images", True)), "img_w": max(10.0, float(P("img_w", 80))), "margin": margin,
             "rotate": int(P("rotate", 0)) % 360 // 90 * 90}
        pw, ph = float(P("paper_w", 297)), float(P("paper_h", 210))
        vw, vh = (ph, pw) if o["rotate"] in (90, 270) else (pw, ph)     # papier « virtuel » sur lequel on met en page
        o.update({"pw": pw, "ph": ph, "W": vw - 2 * margin, "H": vh - 2 * margin})
        if o["font"].startswith("sys:"): o["font"] = "scripts"
        if o["W"] < 20 or o["H"] < 20: raise ApiError("La marge est trop grande pour ce papier.")
        key = json.dumps(o, sort_keys=True)
        if not d["cache"] or d["cache"][0] != key:
            d["cache"] = (key, docimport.paginate(d["ex"], o))
        r = docimport.render_page(d["ex"], d["cache"][1], int(P("page", 0)), o)
        for it in r["items"]:
            if it["kind"] == "text":
                it["font"] = o["font"]
        r["max_width"] = round(o["W"], 3)
        return r
    if path == "/api/vec/svg":
        u = uploads.get(P("id"))
        if not u: raise ApiError("SVG inconnu (recharge le fichier).")
        paths, w, h, notes, colors = vec.svg_to_paths(u["data"], max(0.02, float(cfg().get("simplify", 0.05))),
                                                      str(P("fill", "none")), float(P("hatch", 1.0)), float(P("angle", 45)),
                                                      P("color") or None)
        return vec_response(paths, w, h, {"notes": notes, "colors": colors})
    if path == "/api/vec/image":
        u = uploads.get(P("id"))
        if not u: raise ApiError("Image inconnue (recharge le fichier).")
        paths, w, h, info = vec.image_to_paths(
            u["gray"], max(2.0, float(P("width", 100))), P("mode", "contour"), int(P("threshold", 128)),
            bool(P("invert", False)), float(P("hatch", 0.5)), float(P("angle", 45)), float(P("min_blob", 0.5)),
            float(cfg().get("simplify", 0.05)), float(P("smooth", 0.6)), levels=int(P("levels", 4)))
        return vec_response(paths, w, h, {"info": info})
    if path == "/api/gcode/build":
        groups, pens = parse_groups(P("groups", []))
        return build_gcode(groups, P("name", ""), pens)
    if path == "/api/gcode/analyze":
        a = vec.analyze_gcode(str(P("text", "")), eff_cfg())
        a.pop("commands"); return a
    if path == "/api/run":
        if P("groups") is not None:
            groups, pens = parse_groups(P("groups", []))
            g = build_gcode(groups, P("name", ""), pens)["gcode"]
        else:
            g = str(P("gcode", ""))
        return run_job(g, P("name", "tracé"), bool(P("force")), bool(P("dry")), bool(P("nocomp")))
    raise ApiError("inconnu")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)

    def _handle(self, method):
        u = urlparse(self.path)
        if method == "GET" and u.path == "/api/doc/img":
            q = parse_qs(u.query)
            d = docs.get((q.get("id") or [""])[0])
            try:
                return self.send(200, d["ex"].images[int(q["i"][0])]["png"], "image/png")
            except (TypeError, KeyError, IndexError, ValueError):
                return self.send(404, {"error": "image inconnue"})
        if method == "GET" and u.path == "/":
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if method == "POST" and u.path == "/api/upload":
            body = raw
        else:
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                return self.send(400, {"error": "JSON invalide"})
        try:
            self.send(200, route(method, u.path, parse_qs(u.query), body, self.headers))
        except (ApiError, RuntimeError, ValueError) as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")


class Api:
    """Fonctions natives exposées à la page (boîtes de dialogue)."""

    def save_project(self, name, text):
        """Boîte « Enregistrer sous » : retourne le chemin choisi (ou None)."""
        import webview
        kind = getattr(webview, "FileDialog", None)
        kind = kind.SAVE if kind else webview.SAVE_DIALOG
        r = webview.windows[0].create_file_dialog(
            kind, directory=os.path.expanduser("~/Documents"), save_filename=name)
        if not r:
            return None
        path = r if isinstance(r, str) else r[0]
        if not path.lower().endswith(".vg4"):
            path += ".vg4"
        return write_project(path, text)

    def save_text(self, name, text):
        import webview
        kind = getattr(webview, "FileDialog", None)
        kind = kind.SAVE if kind else webview.SAVE_DIALOG
        r = webview.windows[0].create_file_dialog(
            kind, directory=os.path.expanduser("~/Documents"), save_filename=name)
        if not r:
            return None
        path = r if isinstance(r, str) else r[0]
        with open(path, "w") as f:
            f.write(text)
        return path


def set_mac_identity():
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Atelier Traceuse"
        icon = os.path.join(BASE, "icon.icns")
        if os.path.exists(icon):
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(icon))
    except Exception:
        pass


def main():
    load_cfg()
    machine.journal_path = HISTORY
    port = 0
    server_only = "--server" in sys.argv
    if server_only:
        i = sys.argv.index("--server")
        port = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 8765
    def watchdog():
        while True:
            time.sleep(1.5)
            try: machine.auto_tick()
            except Exception: pass
    threading.Thread(target=watchdog, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    if server_only:
        print("Atelier Traceuse — serveur :", url, flush=True)
        try:
            while True: time.sleep(3600)
        except KeyboardInterrupt:
            return
    import webview
    set_mac_identity()
    webview.create_window("Atelier Traceuse", url, js_api=Api(), width=1320, height=860, min_size=(1000, 680))
    webview.start(set_mac_identity)
    try:
        if machine.connected:
            if machine.job['running']: machine.reset()
            else: machine.command('M5', timeout=2, quiet=True)
            machine.disconnect()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()

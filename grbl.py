"""Atelier Traceuse : liaison GRBL (série réel ou simulation) et exécution des jobs.

Protocole (VigoWriter 1.1f / GRBL 1.1f, 115200 bauds) : lignes G-code terminées par \\n,
acquittement « ok » / « error:N », statut temps réel « ? », pause « ! », reprise « ~ »,
arrêt (soft reset) 0x18. Stylo : « M3 S<valeur> » = baissé, « M5 » = levé.
"""
import bisect, collections, json, math, os, queue, re, subprocess, threading, time

try:
    import serial
    from serial.tools import list_ports
except Exception:                                   # pyserial absent : simulation seule
    serial = None
    list_ports = None

STATUS_RE = re.compile(r"<([A-Za-z]+)(?::(\d+))?\|(.*)>")
SETTING_RE = re.compile(r"^\$(\d+)=([-+]?[\d.]+)")
GRBL_ERRORS = {
    1: "commande G-code inconnue", 2: "valeur manquante", 3: "commande $ invalide", 8: "pas autorisé (non Idle)",
    9: "G-code verrouillé (alarme)", 15: "dépassement de course", 20: "commande non supportée",
    21: "conflit d'axes", 22: "mot G-code manquant", 23: "valeur invalide", 24: "valeur manquante",
}


def list_serial_ports():
    """Ports série avec un indice « probable » pour les adaptateurs USB (CH340, etc.)."""
    res = []
    if list_ports is None:
        return res
    for p in list_ports.comports():
        dev = p.device
        if dev.startswith("/dev/tty.") and any(dev.replace("/dev/tty.", "/dev/cu.") == q.device for q in list_ports.comports()):
            continue                               # on ne garde que /dev/cu.*
        likely = any(k in dev for k in ("usbserial", "usbmodem", "wchusbserial", "SLAB")) or (p.vid in (0x1A86, 0x10C4, 0x0403, 0x2341))
        if "Bluetooth" in dev or "debug-console" in dev:
            continue
        res.append({"device": dev, "desc": p.description or "", "likely": bool(likely),
                    "vid": p.vid, "pid": p.pid})
    res.sort(key=lambda r: (not r["likely"], r["device"]))
    return res


def parse_job(lines):
    """Analyse un job : retourne (pts, is_motion, starts).
    pts[i] = position (x, y) après la ligne i ; starts = indices de début de chaque tracé
    (le dernier déplacement avant chaque descente de stylo M3/M4)."""
    absm = True; x = y = 0.0; motion = None
    pts, mot = [], []
    for l in lines:
        u = re.sub(r"\(.*?\)", "", l.split(";")[0]).upper()
        gs = [int(float(v)) for v in re.findall(r"G\s*(\d+\.?\d*)", u)]
        vals = {k: float(v) for k, v in re.findall(r"([XY])\s*([-+]?(?:\d+\.?\d*|\.\d+))", u)}
        if 90 in gs: absm = True
        if 91 in gs: absm = False
        for g in gs:
            if g in (0, 1, 2, 3): motion = g
        is_m = bool(vals) and motion is not None and 4 not in gs and 92 not in gs
        if is_m:
            x = (vals["X"] if absm else x + vals["X"]) if "X" in vals else x
            y = (vals["Y"] if absm else y + vals["Y"]) if "Y" in vals else y
        pts.append((x, y)); mot.append(is_m)
    starts = []
    for i, l in enumerate(lines):
        if re.search(r"\bM0?[34]\b", l.upper()):
            j = next((k for k in range(i - 1, -1, -1) if mot[k]), None)
            st = j if j is not None else i
            if not starts or starts[-1] != st:
                starts.append(st)
    return pts, mot, starts


def job_lengths(lines):
    """(longueur dessinée, longueur de déplacements à vide) en mm, d'après le programme."""
    pts, mot, _ = parse_job(lines)
    pen = False; draw = trav = 0.0; prev = (0.0, 0.0)
    for i, l in enumerate(lines):
        u = l.upper()
        if re.search(r"\bM0?[34]\b", u): pen = True
        elif re.search(r"\bM0?5\b", u): pen = False
        if mot[i]:
            d = math.hypot(pts[i][0] - prev[0], pts[i][1] - prev[1])
            if pen: draw += d
            else: trav += d
        prev = pts[i]
    return draw, trav


def mac_notify(title, msg):
    """Notification macOS (avec son) ; sans effet ailleurs."""
    try:
        t = str(title).replace('"', "'").replace("\\", "")
        m = str(msg).replace('"', "'").replace("\\", "")
        subprocess.Popen(["osascript", "-e", f'display notification "{m}" with title "{t}" sound name "Glass"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ───────────────────────── simulateur ─────────────────────────


class SimPort:
    """Imite une carte GRBL : mêmes lignes de réponse, mouvements chronométrés
    (vitesse accélérée par `speed`), planner de 16 blocs, feed hold et reset."""

    def __init__(self, speed=10.0, area=(310.0, 256.0)):
        self.speed = max(0.1, speed)
        self.area = area
        self.is_open = True
        self.out = queue.Queue()
        self._buf = b""
        self.cmds = collections.deque()           # blocs en attente d'exécution
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.pos = [0.0, 0.0]; self.wco = [0.0, 0.0]
        self.absolute = True; self.feed = 1000.0; self.spindle = 0
        self.hold = False; self.state = "Idle"
        self.alarm = False
        self.busy = False
        self.gen = 0                              # incrémenté à chaque reset : interrompt le mouvement en cours
        self.inq = queue.Queue()
        self.t = threading.Thread(target=self._run, daemon=True); self.t.start()
        threading.Thread(target=self._feeder, daemon=True).start()
        self.out.put(b"\r\nGrbl 1.1f ['$' for help]\r\n")

    # interface « serial »
    def write(self, data):
        for b in data:
            c = bytes([b])
            if c == b"?":
                self._status()
            elif c == b"!":
                with self.cv: self.hold = True
            elif c == b"~":
                with self.cv: self.hold = False; self.cv.notify_all()
            elif c == b"\x18":
                self._reset()
            elif c in (b"\n", b"\r"):
                line = self._buf.decode("ascii", "ignore").strip(); self._buf = b""
                if line: self._line(line)
            else:
                self._buf += c
        return len(data)

    def readline(self):
        try:
            return self.out.get(timeout=0.15)
        except queue.Empty:
            return b""

    def close(self):
        self.is_open = False
        with self.cv: self.cv.notify_all()

    def reset_input_buffer(self): pass

    # interne
    def _say(self, s): self.out.put((s + "\r\n").encode())

    def _status(self):
        with self.lock:
            st = "Hold:0" if self.hold and self.busy else ("Alarm" if self.alarm else ("Run" if self.busy else "Idle"))
            m = self.pos
            s = f"<{st}|MPos:{m[0]:.3f},{m[1]:.3f},0.000|FS:{int(self.feed)},{self.spindle}"
            s += f"|WCO:{self.wco[0]:.3f},{self.wco[1]:.3f},0.000>"
        self._say(s)

    def _reset(self):
        while not self.inq.empty():
            try: self.inq.get_nowait()
            except queue.Empty: break
        with self.cv:
            self.gen += 1
            self.cmds.clear(); self.hold = False; self.spindle = 0; self.busy = False
            self.alarm = False
            self.cv.notify_all()
        self._say("\r\nGrbl 1.1f ['$' for help]")

    def _line(self, line):
        self.inq.put(line)

    def _feeder(self):
        """Analyse les lignes reçues ; « ok » une fois le bloc accepté dans le planner (16 blocs)."""
        while self.is_open:
            try:
                line = self.inq.get(timeout=0.1)
            except queue.Empty:
                continue
            u = re.sub(r"\(.*?\)|;.*", "", line).upper().strip()
            if u == "$I":
                self._say("[VER:1.1f.20170131:VigoWriter 1.1f (simulation)]"); self._say("[OPT:V,15,128]"); self._say("ok"); continue
            if u == "$$":
                for k, v in (("$100", 80), ("$101", 80), ("$110", 5000), ("$111", 5000), ("$120", 400),
                             ("$121", 400), ("$130", 310), ("$131", 256)):
                    self._say(f"{k}={v}")
                self._say("ok"); continue
            if u == "$X":
                self.alarm = False; self._say("[MSG:Caution: Unlocked]"); self._say("ok"); continue
            if u.startswith("$"):
                self._say("ok"); continue
            with self.cv:
                while len(self.cmds) >= 16 and self.is_open:
                    self.cv.wait(0.05)
                self.cmds.append(u)
                self.cv.notify_all()
            self._say("ok")

    def _run(self):
        while self.is_open:
            with self.cv:
                while self.is_open and (not self.cmds or self.hold):
                    self.busy = bool(self.cmds)
                    self.cv.wait(0.05)
                if not self.is_open: return
                cmd = self.cmds[0]
                self.busy = True
            self._exec(cmd)
            with self.cv:
                if self.cmds and self.cmds[0] is cmd:
                    self.cmds.popleft()
                self.busy = bool(self.cmds)
                self.cv.notify_all()

    def _wait(self, secs, gen=None):
        end = time.monotonic() + secs / self.speed
        while time.monotonic() < end and self.is_open:
            if gen is not None and gen != self.gen:
                return
            if self.hold:
                end += 0.02
            time.sleep(0.01)

    def _exec(self, u):
        gen = self.gen
        gs = [float(v) for v in re.findall(r"G\s*(\d+\.?\d*)", u)]
        ms = [int(float(v)) for v in re.findall(r"M\s*(\d+)", u)]
        vals = {k: float(v) for k, v in re.findall(r"([XYSFP])\s*([-+]?(?:\d+\.?\d*|\.\d+))", u)}
        if 90 in gs: self.absolute = True
        if 91 in gs: self.absolute = False
        if "F" in vals: self.feed = max(1.0, vals["F"])
        if 3 in ms: self.spindle = int(vals.get("S", 0))
        if 5 in ms or 2 in ms: self.spindle = 0
        if 92 in gs:
            self.wco = [self.pos[0] - vals["X"] if "X" in vals else self.wco[0],
                        self.pos[1] - vals["Y"] if "Y" in vals else self.wco[1]]
            return
        if 4 in gs and "P" in vals:
            self._wait(vals["P"], gen); return
        if (0 in gs or 1 in gs or (("X" in vals or "Y" in vals) and not gs)) and ("X" in vals or "Y" in vals):
            tx, ty = self.pos
            if "X" in vals: tx = (vals["X"] + self.wco[0]) if self.absolute else tx + vals["X"]
            if "Y" in vals: ty = (vals["Y"] + self.wco[1]) if self.absolute else ty + vals["Y"]
            d = math.hypot(tx - self.pos[0], ty - self.pos[1])
            f = 5000.0 if 0 in gs else self.feed
            dur = d / max(f, 1.0) * 60.0
            n = max(1, int(dur * 60 / self.speed))
            x0, y0 = self.pos
            for i in range(1, n + 1):
                self._wait(dur / n, gen)
                if not self.is_open or gen != self.gen: return
                with self.lock: self.pos = [x0 + (tx - x0) * i / n, y0 + (ty - y0) * i / n]


# ───────────────────────── machine ─────────────────────────


class Machine:
    def __init__(self, get_cfg):
        self.get_cfg = get_cfg
        self.port = None
        self.sim = False
        self.connected = False
        self.firmware = ""
        self.state = "—"
        self.mpos = [0.0, 0.0]; self.wco = [0.0, 0.0]; self.has_wco = False
        self.feed = 0; self.spindle = 0
        self.alarm_msg = ""
        self.zero_set = False
        self.log = collections.deque(maxlen=400)
        self.ack = queue.Queue()
        self.wlock = threading.Lock()
        self.stop_flag = threading.Event()
        self.paused = False
        self.job = {"running": False, "paused": False, "total": 0, "sent": 0, "name": "", "state": "idle",
                    "msg": "", "t0": 0, "est": 0, "done_at": 0}
        self._reader = None; self._poller = None
        self._cmd_lock = threading.Lock()
        self.substate = ""
        self.auto_port = None; self.auto_baud = 115200; self.lost = False; self._watch = None
        self.cont_event = threading.Event()
        self.grbl_cfg = {}
        self.job_full = None; self.job_parsed = None; self.job_off = None; self.job_base_name = ""
        self.mem = None                                 # dernière position connue (zéro + stylo), gardée tant que l'app est ouverte
        self.trail = []; self.trail_li = []; self.trail_id = 0; self._tf = None   # tracé réellement dessiné (suivi du programme)
        self.stop_pos = None; self.stop_waiting = False; self._ri_key = None; self._ri = None
        self.awake_proc = None; self.journal_path = None
        self.limit_m = {"x": None, "y": None}          # positions machine (brutes) des butées enregistrées

    # ── journal ──
    def _log(self, kind, text):
        self.log.append({"t": time.time(), "k": kind, "s": text})

    # ── connexion ──
    def connect(self, port=None, baud=115200, sim=False):
        if self.connected:
            self.disconnect()
        elif self.port is not None:                    # port resté ouvert après une liaison perdue
            try: self.port.close()
            except Exception: pass
            self.port = None
        cfg = self.get_cfg()
        if sim:
            self.port = SimPort(cfg.get("sim_speed", 10), (cfg.get("area_w", 310), cfg.get("area_h", 256)))
            self.sim = True
        else:
            if serial is None:
                raise RuntimeError("pyserial n'est pas installé.")
            if not port:
                raise RuntimeError("Aucun port série choisi.")
            try:
                self.port = serial.Serial(port, baud, timeout=0.15, write_timeout=2)
            except Exception as e:
                self.port = None
                raise RuntimeError(f"Impossible d'ouvrir {port} : {getattr(e, 'strerror', None) or e}. "
                                   "(Port absent ou déjà utilisé par une autre application ?)")
            self.sim = False
            time.sleep(2.0)                           # démarrage de la carte après ouverture
            try: self.port.reset_input_buffer()
            except Exception: pass
        self.connected = True; self.state = "Connexion…"; self.firmware = ""
        self.lost = False; self.zero_set = False; self.has_wco = False; self.wco = [0.0, 0.0]
        self.limit_m = {"x": None, "y": None}
        self.auto_port = None if sim else port; self.auto_baud = baud
        self.stop_flag.clear(); self.paused = False; self.alarm_msg = ""
        self.ack = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True); self._reader.start()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True); self._poller.start()
        self._log("info", "Simulation démarrée" if sim else f"Connecté à {port} ({baud} bauds)")
        try:
            self.command("$I", timeout=6, quiet=True)
        except Exception as e:
            self._log("warn", f"Pas de réponse à $I : {e}")
        if not self.sim and "VigoWriter" not in self.firmware and "1.1" not in self.firmware:
            self._log("warn", "Firmware non reconnu (attendu : VigoWriter 1.1f). Vérifie le port.")
        if not self.sim and self.mem:
            try: self._restore_zero()
            except Exception as e: self._log("warn", f"Point zéro non restauré : {e}")

    def _restore_zero(self):
        """Après une reconnexion : la carte a redémarré (position 0,0, zéro perdu) mais le stylo n'a pas bougé.
        On redonne à la carte la dernière position connue, donc le point zéro reste celui du dessin."""
        mem = self.mem; lw, lm = mem["w"], mem["m"]
        t_end = time.monotonic() + 3.0
        while time.monotonic() < t_end and self.state in ("Connexion…", "—"):
            time.sleep(0.05)
        time.sleep(0.4)                                   # laisse arriver un statut à jour
        if self.state.startswith("Alarm"):
            self._log("warn", "Carte en alarme : point zéro précédent non restauré (débloque puis redéfinis le zéro).")
            self.mem = None
            return
        if math.hypot(self.mpos[0], self.mpos[1]) < 0.5:  # carte redémarrée : on lui redonne la position
            self.command("M5", timeout=4, quiet=True)
            self.command(f"G92 X{lw[0]:.3f} Y{lw[1]:.3f}", timeout=4, quiet=True)
            self.zero_set = True; self.has_wco = True
            self._log("info", f"Point zéro précédent conservé (dernière position connue : X{lw[0]:.2f} Y{lw[1]:.2f}). "
                              "Vérifie que le stylo n'a pas été déplacé à la main.")
        elif math.hypot(self.mpos[0] - lm[0], self.mpos[1] - lm[1]) < 1.0:      # la carte n'avait pas redémarré
            t2 = time.monotonic() + 2.0
            while not self.has_wco and time.monotonic() < t2:
                time.sleep(0.05)
            if self.has_wco:
                self.zero_set = True
                self._log("info", "Point zéro précédent conservé (la carte n'avait pas redémarré).")
        else:
            self._log("warn", "Position de la carte inattendue : point zéro précédent non restauré, redéfinis-le.")
            self.mem = None

    def disconnect(self, keep_auto=False):
        port = self.port
        if not keep_auto:
            self.auto_port = None; self.lost = False
        self.connected = False
        self.stop_flag.set()
        try:
            if port: port.close()
        except Exception:
            pass
        self.port = None; self.state = "—"
        self.job.update(running=False, paused=False)
        self._log("info", "Déconnecté")

    # ── E/S ──
    def _write(self, data):
        with self.wlock:
            self.port.write(data)

    def _read_loop(self):
        port = self.port
        buf = b""
        while self.connected and self.port is port:
            try:
                if self.sim:
                    chunk = port.readline()
                else:
                    chunk = port.readline()
            except Exception as e:
                if self.connected:
                    self._log("err", f"Liaison perdue : {e}")
                    self.state = "Déconnecté"; self.connected = False; self.lost = bool(self.auto_port)
                    if self.mem and self.job.get("running"): self.stop_pos = list(self.mem["w"])
                    self.job.update(running=False, state="error", msg="Liaison série perdue")
                    self.stop_flag.set()
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", "ignore").strip()
                if line:
                    self._on_line(line)

    def _on_line(self, line):
        m = STATUS_RE.match(line)
        if m:
            self.state = m.group(1); self.substate = m.group(2) or ""
            for part in m.group(3).split("|"):
                k, _, v = part.partition(":")
                try:
                    if k in ("MPos", "WPos"):
                        x, y = [float(a) for a in v.split(",")[:2]]
                        if k == "MPos": self.mpos = [x, y]
                        else: self.mpos = [x + self.wco[0], y + self.wco[1]]
                    elif k == "WCO":
                        self.wco = [float(a) for a in v.split(",")[:2]]; self.has_wco = True
                    elif k == "FS":
                        f, s = v.split(",")[:2]; self.feed = int(float(f)); self.spindle = int(float(s))
                except ValueError:
                    pass
            if self.zero_set and self.has_wco and not self.sim and self.connected:
                self.mem = {"w": self.wpos(), "m": list(self.mpos)}
            if self._tf is not None:
                try: self._trail_feed()
                except Exception: pass
            return
        ms = SETTING_RE.match(line)
        if ms:
            self.grbl_cfg[int(ms.group(1))] = float(ms.group(2))
        low = line.lower()
        if low == "ok":
            self.ack.put(("ok", ""))
        elif low.startswith("error:"):
            try: n = int(low[6:])
            except ValueError: n = -1
            msg = f"error:{n} — {GRBL_ERRORS.get(n, 'erreur GRBL')}"
            self._log("err", msg); self.ack.put(("error", msg))
        elif low.startswith("alarm"):
            self.alarm_msg = line; self.state = "Alarm"
            self._log("err", line); self.ack.put(("error", line))
        else:
            if line.startswith("[VER:"):
                self.firmware = line
            if line.lower().startswith("grbl"):
                self.ack = queue.Queue()
            self._log("rx", line)

    def _poll_loop(self):
        port = self.port
        while self.connected and self.port is port:
            try:
                self._write(b"?")
            except Exception:
                pass
            time.sleep(0.12 if self.job["running"] else 0.3)

    def command(self, line, timeout=30.0, quiet=False):
        """Envoie une ligne et attend « ok ». Lève RuntimeError si erreur/timeout/arrêt."""
        if not self.connected:
            raise RuntimeError("Non connecté.")
        with self._cmd_lock:
            if not quiet:
                self._log("tx", line)
            while not self.ack.empty():
                try: self.ack.get_nowait()
                except queue.Empty: break
            self._write((line.strip() + "\n").encode("ascii", "ignore"))
            self._after_send(line)
            end = time.monotonic() + timeout
            while True:
                if self.stop_flag.is_set():
                    raise RuntimeError("Arrêté.")
                try:
                    kind, msg = self.ack.get(timeout=0.1)
                except queue.Empty:
                    if self.paused or self.state.startswith("Hold"):
                        end = time.monotonic() + timeout
                    if time.monotonic() > end:
                        raise RuntimeError(f"Pas d'acquittement pour « {line} ».")
                    if not self.connected:
                        raise RuntimeError("Liaison perdue.")
                    continue
                if kind == "error":
                    raise RuntimeError(msg)
                return

    def _after_send(self, line):
        u = line.strip().upper()
        if u.startswith("G92"):
            # nouveau zéro : offset = position machine − valeur demandée (le statut suivant le confirmera)
            v = {k: float(a) for k, a in re.findall(r"([XY])\s*([-+]?(?:\d+\.?\d*|\.\d+))", u)}
            self.wco = [self.mpos[0] - v.get("X", self.mpos[0] - self.wco[0]),
                        self.mpos[1] - v.get("Y", self.mpos[1] - self.wco[1])]

    # ── commandes manuelles ──
    def wpos(self):
        return [self.mpos[0] - self.wco[0], self.mpos[1] - self.wco[1]]

    def _guard(self):
        if self.job["running"]:
            raise RuntimeError("Un tracé est en cours.")
        if not self.connected:
            raise RuntimeError("Non connecté.")

    def pen(self, down):
        self._guard()
        cfg = self.get_cfg(); d = max(0.0, cfg.get("pen_settle_ms", 300) / 1000.0)
        if down:
            self.command(f"M3 S{int(cfg.get('pen_down_s', 905))}")
        else:
            self.command("M5")
        self.command(f"G4 P{d:.3f}")

    def pen_test(self, s):
        self._guard()
        self.command(f"M3 S{int(max(0, min(1000, s)))}")

    def jog(self, dx, dy, feed=None):
        """dx, dy dans le sens de l'écran (dy > 0 = vers le bas du papier)."""
        self._guard()
        cfg = self.get_cfg()
        sign = -1.0 if cfg.get("invert_y") else 1.0
        sx = -1.0 if cfg.get("invert_x") else 1.0
        f = int(feed or cfg.get("jog_feed", 2000))
        self.command("G91")
        try:
            self.command(f"G1 X{sx * dx:.3f} Y{sign * dy:.3f} F{f}")
        finally:
            self.command("G90")

    def goto(self, x, y):
        self._guard()
        cfg = self.get_cfg(); sign = -1.0 if cfg.get("invert_y") else 1.0
        sx = -1.0 if cfg.get("invert_x") else 1.0
        self.command("M5")
        self.command("G90")
        self.command(f"G1 X{sx * x:.3f} Y{sign * y:.3f} F{int(cfg.get('travel_feed', 3000))}")

    def set_zero(self):
        self._guard()
        self.command("M5")
        self.command("G92 X0 Y0")
        self.zero_set = True
        self.has_wco = True
        self._log("info", "Point zéro défini à la position actuelle")

    def raw(self, line):
        if not self.connected:
            raise RuntimeError("Non connecté.")
        u = line.strip().upper()
        if u.startswith("$H"):
            raise RuntimeError("$H (homing) refusé : la machine n'a pas de fins de course.")
        if re.match(r"\$\d+\s*=", u) or u.startswith("$RST") or u.startswith("$N"):
            raise RuntimeError("Écriture de réglages GRBL refusée depuis la console.")
        if self.job["running"]:
            raise RuntimeError("Un tracé est en cours.")
        self.command(line)

    def unlock(self):
        if not self.connected:
            raise RuntimeError("Non connecté.")
        self.command("$X"); self.alarm_msg = ""

    def reset(self):
        """Arrêt : feed hold, puis soft reset (Ctrl-X). Si la machine s'est bien arrêtée avant le reset,
        sa position est conservée : on restaure le point zéro et le tracé peut être repris."""
        if not self.connected:
            return
        moving = bool(self.job["running"]) or self.state in ("Run", "Hold", "Jog")
        keep_zero = self.zero_set and self.has_wco
        saved_wco = list(self.wco)
        self.stop_waiting = bool(self.job.get("waiting"))
        self.stop_flag.set()
        held = not moving and self.state == "Idle"
        if moving:
            try: self._write(b"!")
            except Exception: pass
            t0 = time.monotonic()
            while time.monotonic() - t0 < 3.5:
                if (self.state == "Hold" and self.substate == "0") or self.state == "Idle":
                    held = True; break
                time.sleep(0.05)
        time.sleep(0.15)
        self.stop_pos = self.wpos()
        mpos_before = list(self.mpos)
        if self.job["running"]:
            self.job.update(running=False, paused=False, state="stopped", msg="Arrêt demandé")
        self.paused = False; self.cont_event.set()
        self._write(b"\x18")
        self._log("warn", "Arrêt demandé (feed hold puis soft reset)")
        time.sleep(0.9)                                   # redémarrage de la carte + statut à jour
        self.stop_flag.clear(); self.cont_event.clear()
        try: self._write(b"M5\n")
        except Exception: pass
        restored = False
        if held and keep_zero and not self.state.startswith("Alarm"):
            moved = math.hypot(self.mpos[0] - mpos_before[0], self.mpos[1] - mpos_before[1])
            if moved < 0.6:
                try:
                    vx, vy = self.mpos[0] - saved_wco[0], self.mpos[1] - saved_wco[1]
                    self.command(f"G92 X{vx:.3f} Y{vy:.3f}", timeout=4, quiet=True)
                    restored = True
                except Exception as e:
                    self._log("warn", f"Zéro non restauré : {e}")
        self.zero_set = restored
        if not restored: self.mem = None
        self._ri_key = None
        if restored:
            self._log("info", "Arrêt propre : position et point zéro conservés — tu peux reprendre le tracé.")
        else:
            self._log("warn", "Arrêt : la position n'est pas garantie — redéfinis le point zéro avant de relancer.")

    # ── zone réellement utilisable depuis le zéro ──
    def mark_limit(self, axis):
        """Enregistre la position actuelle comme butée (axe 'x' = droite, 'y' = bas, sens de l'écran)."""
        self._guard()
        if axis not in ("x", "y"):
            raise RuntimeError("Axe inconnu.")
        i = 0 if axis == "x" else 1
        self.limit_m[axis] = self.mpos[i]
        r = self.reach()
        self._log("info", f"Limite {axis.upper()} enregistrée : zone utilisable {r.get('w')} × {r.get('h')} mm")

    def clear_limits(self):
        self.limit_m = {"x": None, "y": None}

    def reach(self):
        """Course disponible depuis le zéro actuel, vers la droite (w) et vers le bas (h) de l'écran."""
        cfg = self.get_cfg()
        sx = -1.0 if cfg.get("invert_x") else 1.0
        sy = -1.0 if cfg.get("invert_y") else 1.0
        out = {}
        if self.limit_m["x"] is not None:
            v = sx * (self.limit_m["x"] - self.wco[0])
            if v > 1: out["w"] = round(v, 1)
        if self.limit_m["y"] is not None:
            v = sy * (self.limit_m["y"] - self.wco[1])
            if v > 1: out["h"] = round(v, 1)
        return out

    # ── réglages GRBL (lecture / étalonnage) ──
    def read_grbl_settings(self):
        self._guard()
        self.grbl_cfg = {}
        self.command("$$", timeout=8, quiet=True)
        time.sleep(0.2)
        return dict(self.grbl_cfg)

    def set_steps(self, axis, value):
        """Seuls $100 (X) et $101 (Y), pas/mm, peuvent être écrits (étalonnage)."""
        self._guard()
        n = {"x": 100, "y": 101}.get(str(axis).lower())
        if n is None:
            raise RuntimeError("Axe inconnu.")
        v = float(value)
        if not 20.0 <= v <= 800.0:
            raise RuntimeError("Valeur de pas/mm hors plage (20 à 800).")
        old = self.grbl_cfg.get(n)
        self.command(f"${n}={v:.3f}")
        self.grbl_cfg[n] = v
        self._log("info", f"${n} (pas/mm {axis.upper()}) : {old} → {v:.3f}")
        return {"axis": axis, "old": old, "new": v}

    # ── jobs ──
    def start_job(self, lines, name="tracé", est=0, resume_from=None):
        self._guard()
        if self.job["running"]:
            raise RuntimeError("Un tracé est déjà en cours.")
        self.stop_flag.clear(); self.paused = False; self.cont_event.clear()
        if resume_from is None:                          # tracé neuf : on garde le programme complet
            self.job_full = list(lines); self.job_off = None; self.job_base_name = name
            self.job_parsed = parse_job(self.job_full)
        else:                                            # reprise : (longueur de l'en-tête, début dans le programme)
            self.job_off = resume_from
        self.stop_pos = None; self.stop_waiting = False; self._ri_key = None
        npaths = len(self.job_parsed[2]) if self.job_parsed else 0
        try: draw_mm, _t = job_lengths(lines)
        except Exception: draw_mm = 0.0
        self.job = {"running": True, "paused": False, "total": len(lines), "sent": 0, "name": name,
                    "state": "running", "msg": "", "t0": time.time(), "est": est, "done_at": 0,
                    "npaths": npaths, "resumed": resume_from is not None, "waiting": "", "draw_mm": round(draw_mm)}
        self._trail_init(lines, resume_from is not None, name)
        self._awake(True)
        threading.Thread(target=self._run_job, args=(list(lines),), daemon=True).start()

    # ── anti-veille, notifications, journal ──
    def _awake(self, on):
        if on and self.get_cfg().get("keep_awake", True) and not self.sim and self.awake_proc is None:
            try: self.awake_proc = subprocess.Popen(["caffeinate", "-i", "-m", "-w", str(os.getpid())])
            except Exception: self.awake_proc = None
        elif not on and self.awake_proc is not None:
            try: self.awake_proc.terminate()
            except Exception: pass
            self.awake_proc = None

    def _notify(self, title, msg):
        if self.get_cfg().get("notify", True) and not self.sim:
            mac_notify(title, msg)

    def _journal(self, job):
        if not self.journal_path:
            return
        try:
            total = max(1, job.get("total", 1))
            rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "name": job.get("name", ""), "state": job.get("state", ""),
                   "duration": round(time.time() - job.get("t0", time.time())), "est": job.get("est", 0),
                   "draw_mm": job.get("draw_mm", 0), "frac": round(job.get("sent", 0) / total, 3),
                   "sim": bool(self.sim), "msg": job.get("msg", "")}
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _full_idx(self, i):
        """Indice local (ligne envoyée) → indice dans le programme complet."""
        if self.job_off is None:
            return i
        hl, st = self.job_off
        return st + (i - hl) if i >= hl else None

    def _wait_idle(self, timeout=90.0):
        t_end = time.monotonic() + timeout
        n = 0
        while time.monotonic() < t_end and not self.stop_flag.is_set() and self.connected:
            time.sleep(0.12)
            n = n + 1 if (self.state == "Idle" and not self.paused) else 0
            if n >= 3:
                return True
        return False

    def _run_job(self, lines):
        job = self.job
        try:
            for i, line in enumerate(lines):
                if self.stop_flag.is_set():
                    raise RuntimeError("Arrêté.")
                if re.match(r"M0+(\s|;|$)", line.strip().upper()):       # changement de stylo
                    self._wait_idle()
                    label = line.split(";", 1)[1].strip() if ";" in line else "Changer de stylo"
                    self.cont_event.clear()
                    job["waiting"] = label
                    self._log("warn", f"Pause : {label}")
                    self._notify("Atelier Traceuse", label)
                    while not self.cont_event.wait(0.1):
                        if self.stop_flag.is_set() or not self.connected:
                            raise RuntimeError("Arrêté.")
                    job["waiting"] = ""
                    self._log("info", "Reprise après changement de stylo")
                    job["sent"] = i + 1
                    continue
                self.command(line, quiet=True)
                job["sent"] = i + 1
            job["msg"] = "Fin de l'envoi, attente de la fin des mouvements…"
            idle = 0; t_end = time.monotonic() + 600
            while idle < 3 and not self.stop_flag.is_set() and self.connected and time.monotonic() < t_end:
                time.sleep(0.15)
                idle = idle + 1 if (self.state == "Idle" and not self.paused) else 0
            if self.stop_flag.is_set():
                raise RuntimeError("Arrêté.")
            job.update(state="done", msg="Terminé", done_at=time.time())
            self._log("info", f"Tracé terminé en {time.time() - job['t0']:.0f} s")
        except Exception as e:
            if job["state"] != "stopped":
                job.update(state="error" if "Arrêté" not in str(e) else "stopped", msg=str(e))
                self._log("err" if job["state"] == "error" else "warn", f"Tracé interrompu : {e}")
            try:
                if self.connected and job["state"] == "error":
                    self._write(b"!"); time.sleep(0.05)
            except Exception:
                pass
        finally:
            try: self._trail_feed(final=True)
            except Exception: pass
            self._tf = None
            job["running"] = False; job["paused"] = False; job["waiting"] = ""; self.paused = False
            self._ri_key = None
            self._awake(False)
            if job["state"] == "running": job["state"] = "error"
            self._journal(job)
            dur = time.time() - job["t0"]
            if job["state"] == "done" and dur >= 20:
                self._notify("Atelier Traceuse — tracé terminé", f"{job['name']} ({int(dur // 60)} min {int(dur % 60):02d} s)")
            elif job["state"] == "error":
                self._notify("Atelier Traceuse — tracé interrompu", job.get("msg", "") or "erreur")

    # ── tracé affiché : on suit le programme, pas seulement les relevés de position ──
    def _trail_init(self, lines, resumed, name):
        """Prépare le suivi : liste des déplacements (x0, y0, x1, y1, stylo, longueur) du programme,
        avec la ligne d'où chacun vient (la machine ne peut pas être au-delà de ce qui a été envoyé)."""
        if not resumed and not re.match(r"(Alignement|Cadre|Essai)", name or ""):
            self.trail = []; self.trail_li = []; self.trail_id += 1
        elif resumed and self.job_off:                   # reprise : on garde ce qui précède le point de reprise, rien de plus
            st = self.job_off[1]
            keep = [n for n, i in enumerate(self.trail_li) if i < st]
            if len(keep) != len(self.trail):
                self.trail = [self.trail[n] for n in keep]; self.trail_li = [self.trail_li[n] for n in keep]
                self.trail_id += 1
        pts, mot, _ = parse_job(lines)
        px, py = self.wpos(); pen = False; mv = []; li = []
        for i, l in enumerate(lines):
            u = re.sub(r"\(.*?\)", "", l.split(";")[0]).upper()
            if re.search(r"\bM0?[34]\b", u): pen = True
            elif re.search(r"\bM0?5\b", u): pen = False
            if mot[i]:
                x, y = pts[i]
                mv.append((px, py, x, y, pen, math.hypot(x - px, y - py)))
                li.append(i)
                px, py = x, y
        self._tf = {"mv": mv, "li": li, "k": 0, "t": 0.0, "miss": 0, "n": len(lines)}

    def _tpush(self, pen, x, y, line_i):
        f = self._full_idx(line_i)
        self.trail.append([1 if pen else 0, round(x, 2), round(y, 2)])
        self.trail_li.append(-1 if f is None else f)

    TRAIL_BEHIND = 80          # lignes : retard maximal de la machine sur ce qui a été envoyé (tampon de la carte)

    def _trail_feed(self, final=False):
        """Place la position de la machine sur le programme et ajoute au tracé les morceaux parcourus.
        La recherche reste dans la fenêtre « lignes envoyées moins le tampon de la carte » : le suivi ne peut
        donc ni sauter en avant (vers un trait voisin plus loin dans le fichier) ni rester loin derrière."""
        tf = self._tf
        if not tf:
            return
        mv = tf["mv"]; li = tf["li"]; k, t = tf["k"], tf["t"]
        if k >= len(mv):
            return
        sent = self.job.get("sent", 0)
        done = self.job.get("state") == "done"
        hi = len(mv) if (done or sent >= tf["n"]) else bisect.bisect_left(li, sent)       # déplacements déjà envoyés
        lo = 0 if (final and done) else bisect.bisect_left(li, sent - self.TRAIL_BEHIND)
        if final and done:                               # tracé terminé : tout le reste a été dessiné
            for j in range(k, len(mv)):
                x0, y0, x1, y1, pen, L = mv[j]
                self._tpush(pen, x1, y1, li[j])
            tf["k"] = len(mv); tf["t"] = 0.0
            return
        if lo > k:                                       # la machine ne peut plus être avant : on avance le curseur
            k, t = lo, 0.0
        if k >= hi:
            return
        wx, wy = self.wpos()
        best = None; acc = 0.0; i = k
        limit = None if (tf["miss"] >= 8 or final) else 60.0
        while i < hi and (limit is None or acc < limit):
            x0, y0, x1, y1, pen, L = mv[i]
            tt = 1.0 if L < 1e-9 else ((wx - x0) * (x1 - x0) + (wy - y0) * (y1 - y0)) / (L * L)
            tt = min(1.0, max(0.0, tt))
            if i == tf["k"]: tt = max(tt, tf["t"])
            d = math.hypot(wx - (x0 + (x1 - x0) * tt), wy - (y0 + (y1 - y0) * tt))
            if best is None or d < best[0] - 1e-6:
                best = (d, i, tt)
                if d < 1e-4 and limit is not None: break
            acc += L; i += 1
        if best is None or best[0] > 0.5:
            tf["miss"] += 1
            return
        tf["miss"] = 0
        _, bi, bt = best
        k0, t0 = tf["k"], tf["t"]
        def pt(j, f):
            x0, y0, x1, y1, pen, L = mv[j]
            self._tpush(pen, x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, li[j])
        if bi == k0:
            if bt > t0: pt(k0, bt)
        else:
            pt(k0, 1.0)
            for j in range(k0 + 1, bi): pt(j, 1.0)
            if bt > 0: pt(bi, bt)
        tf["k"], tf["t"] = bi, bt

    def continue_job(self):
        """Fin d'une pause « changement de stylo »."""
        if not (self.connected and self.job["running"] and self.job.get("waiting")):
            raise RuntimeError("Aucun changement de stylo en attente.")
        self.cont_event.set()

    def forget_job(self):
        self.job_full = None; self.job_parsed = None; self.job_off = None; self._ri_key = None
        if not self.job["running"]:
            self.job.update(state="idle", msg="")

    # ── reprise après arrêt ──
    def resume_info(self):
        j = self.job
        if j["running"] or not self.job_full or not self.job_parsed or j["state"] not in ("stopped", "error"):
            return None
        pts, mot, starts = self.job_parsed
        if not starts:
            return None
        key = (len(self.job_full), j["sent"], j["state"], tuple(self.stop_pos or ()), self.job_off, self.zero_set)
        if self._ri_key == key:
            return self._ri
        last = self._full_idx(max(0, j["sent"] - 1))
        if last is None:
            last = 0
        last = min(last, len(self.job_full) - 1)
        lo = max(0, last - 24)
        if self.stop_pos is not None:
            cands = [k for k in range(lo, last + 1) if mot[k]]
            if cands:
                sp = self.stop_pos
                idx = min(cands, key=lambda k: (math.hypot(pts[k][0] - sp[0], pts[k][1] - sp[1]), -k))
            else:
                idx = lo
        else:
            idx = max(0, last - 18)
        k = max(0, bisect.bisect_right(starts, idx) - 1)
        if self.stop_waiting:                           # arrêté pendant « changer de stylo » : on repart au tracé suivant
            k = min(len(starts) - 1, bisect.bisect_right(starts, last))
        self._ri = {"name": self.job_base_name, "path": k + 1, "npaths": len(starts), "zero_ok": bool(self.zero_set),
                    "state": j["state"], "msg": j["msg"]}
        self._ri_key = key
        return self._ri

    def resume_job(self, path_no, force=False):
        info = self.resume_info()
        if not info:
            raise RuntimeError("Aucun tracé à reprendre.")
        if not self.zero_set and not force:
            raise RuntimeError("Le point zéro n'est plus connu : replace le stylo à l'origine du papier, "
                               "redéfinis le zéro (Contrôle) puis reprends.")
        pts, mot, starts = self.job_parsed
        k = max(0, min(len(starts) - 1, int(path_no) - 1))
        st = starts[k]
        for q in range(st - 1, max(-1, st - 4), -1):      # un « changer de stylo » juste avant : on le rejoue
            if re.match(r"M0+(\s|;|$)", self.job_full[q].strip().upper()):
                st = q - 2 if q >= 2 and self.job_full[q - 2].strip().upper() == "M5" else q
                break
        header = [l for i, l in enumerate(self.job_full[:starts[0]]) if not mot[i]]
        lines = ["M5"] + header + self.job_full[st:]
        name = self.job_base_name
        self._log("info", f"Reprise au tracé {k + 1}/{len(starts)}")
        self.start_job(lines, name=f"{name} (reprise)", est=0, resume_from=(len(header) + 1, st))

    def auto_tick(self):
        """Appelé toutes les ~1,5 s : reconnecte tout seul si la liaison a été perdue (câble, mise hors tension)."""
        cfg = self.get_cfg()
        if self.connected or not self.lost or not self.auto_port or not cfg.get("auto_reconnect", True):
            return
        if self.auto_port not in {p["device"] for p in list_serial_ports()}:
            return
        try:
            time.sleep(1.0)
            self.connect(self.auto_port, self.auto_baud)
            self._log("info", "Reconnexion automatique réussie. La position n'est plus connue : redéfinis le point zéro.")
            self._auto_err = None
        except Exception as e:
            if getattr(self, "_auto_err", None) != str(e):
                self._auto_err = str(e)
                self._log("warn", f"Reconnexion automatique impossible : {e}")

    def pause(self):
        if self.connected and self.job["running"]:
            self._write(b"!"); self.paused = True; self.job["paused"] = True
            self._log("warn", "Pause (feed hold) — le stylo reste là où il est")

    def resume(self):
        if self.connected and self.job["running"]:
            self._write(b"~"); self.paused = False; self.job["paused"] = False
            self._log("info", "Reprise")

    # ── état pour l'interface ──
    def snapshot(self, log_after=0.0, trail_id=-1, trail_after=0):
        j = dict(self.job)
        j["elapsed"] = (time.time() - j["t0"]) if j["running"] else 0
        if j["running"] and self.job_parsed:
            fi = self._full_idx(max(0, j["sent"] - 1))
            j["path"] = (bisect.bisect_right(self.job_parsed[2], fi) if fi is not None else 0)
        wp = self.wpos()
        if self.get_cfg().get("invert_y"): wp[1] = -wp[1]
        if self.get_cfg().get("invert_x"): wp[0] = -wp[0]
        tr = self.trail; ta = trail_after if trail_id == self.trail_id and 0 <= trail_after <= len(tr) else 0
        ix = -1 if self.get_cfg().get("invert_x") else 1; iy = -1 if self.get_cfg().get("invert_y") else 1
        tp = [[p[0], p[1] * ix, p[2] * iy] for p in tr[ta:ta + 20000]]
        return {"trail": {"id": self.trail_id, "n": ta + len(tp), "pts": tp},
                "connected": self.connected, "sim": self.sim, "state": self.state, "firmware": self.firmware,
                "mpos": [round(v, 3) for v in self.mpos], "wpos": [round(v, 3) for v in wp],
                "spindle": self.spindle, "feed": self.feed, "alarm": self.alarm_msg, "zero_set": self.zero_set,
                "job": j, "resume": self.resume_info(), "lost": self.lost, "reach": self.reach(), "grbl": dict(self.grbl_cfg),
                "log": [e for e in list(self.log) if e["t"] > log_after][-80:]}

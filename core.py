"""ACE - Kern: Kill-Erkennung, Export, Updates, Video-Player (ohne Oberflaeche).

Erkennt den roten Kill-Hitmarker in der Bildmitte, legt pro Kill einen Clip
(Standard: 4 s davor, 2 s danach) an und exportiert mit FFmpeg.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import cv2
import numpy as np

# ----------------------------------------------------------------- Kern ----

APP_VERSION = "1.0"                 # wird beim Build automatisch gesetzt
UPDATE_REPO = "Gaby18x/kill-app"    # oeffentliches Repo mit den Releases
ACCOUNT_URL = "https://ace.sendreagaby.workers.dev"                    # Adresse des Konto-Servers (Cloudflare Worker); leer = ohne Konten


def parse_ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def check_update():
    """Gibt (version, download_url) zurueck, wenn es ein neueres Release gibt, sonst None."""
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": "ACE", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    if parse_ver(tag) > parse_ver(APP_VERSION):
        for a in data.get("assets", []):
            if a["name"].lower().endswith(".exe"):
                return tag.lstrip("v"), a["browser_download_url"]
    return None


def download_file(url, progress=None):
    dst = os.path.join(tempfile.gettempdir(), "ACE-Setup.exe")
    req = urllib.request.Request(url, headers={"User-Agent": "ACE"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dst, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(got / total)
    return dst



def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Eingebauter Server "Bruder": Kill-Meldung mit lila "Information"-Box und Text "Du hast ... getoetet".
BRUDER = {"template": ("profiles", "bruder_notify.png"),
          "header": (10, 32, 55, 145),
          # Woerter im Beispielbild (y0, y1, x0, x1)
          "words": {"duhast": (31, 46, 58, 106), "getotet": (58, 74, 78, 124)}}

DATA_DIR = os.path.join(os.path.expanduser("~"), ".killapp_data")

_HALF = 60
_YY, _XX = np.mgrid[-_HALF:_HALF, -_HALF:_HALF]
_D = np.hypot(_XX, _YY)
_INNER = (_D >= 4) & (_D <= 28)
_OUTER = _D > 28
_QUADS = [(_XX > 0) & (_YY > 0), (_XX < 0) & (_YY > 0), (_XX > 0) & (_YY < 0), (_XX < 0) & (_YY < 0)]


def is_hitmarker(roi, diff=40, qmin=4):
    """roi: 120x120 BGR. Rotes X: alle vier Quadranten um die Mitte rot, aussen herum kaum Rot."""
    a = roi.astype(np.int16)
    b, g, r = a[..., 0], a[..., 1], a[..., 2]
    m = (r > 140) & (r - np.maximum(g, b) > diff)
    ins = m & _INNER
    if min(int((ins & q).sum()) for q in _QUADS) < qmin:
        return False
    return int((m & _OUTER).sum()) <= 0.5 * int(ins.sum()) + 6


def purple_mask(im):
    h = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    return ((h[..., 0] >= 118) & (h[..., 0] <= 150) & (h[..., 1] >= 130) & (h[..., 2] >= 90)).astype(np.float32)


def read_image(path, flags=cv2.IMREAD_COLOR):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), flags)


def load_notify_assets(profile):
    """Header-Maske (lila 'Information'), Wort-Vorlagen und deren Lage relativ zum Header."""
    path = resource(*profile["template"])
    im = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    y0, y1, x0, x1 = profile["header"]
    m = purple_mask(im)[y0:y1, x0:x1]
    ys, xs = np.nonzero(m)
    anchor = (y0 + int(ys.min()), x0 + int(xs.min()))
    hdr = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    words = {n: gray[a:b, c:d] for n, (a, b, c, d) in profile["words"].items()}
    return hdr, words


# Suchbereich fuer den Text relativ zur Header-Ecke (1080p): y von +6 bis +80, x von -8 bis +200
TEXT_REGION = (6, 80, -8, 200)
# Breiter Bereich: lange Namen + Waffe ("... mit der Waffe Pistole .50 getoetet") brechen auf 2-3 Zeilen um
TEXT_REGION_WIDE = (0, 104, -24, 300)


def verify_text(frame, anchor_xy, ks, words, thr, region=TEXT_REGION):
    """Prueft im Originalbild, ob in der Box 'Du hast' und 'getoetet' stehen. Gibt (ok, scores) zurueck."""
    ax, ay = anchor_xy
    H, W = frame.shape[:2]
    y0, y1 = int(max(0, ay + region[0] * ks)), int(min(H, ay + region[1] * ks))
    x0, x1 = int(max(0, ax + region[2] * ks)), int(min(W, ax + region[3] * ks))
    if y1 - y0 < 4 or x1 - x0 < 4:                       # Bereich liegt (fast) ausserhalb des Bildes
        return False, {n: -1.0 for n in words}
    reg = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    scores = {}
    for name, t in words.items():
        if abs(ks - 1) > 0.02:
            t = cv2.resize(t, None, fx=ks, fy=ks, interpolation=cv2.INTER_AREA)
        if reg.shape[0] < t.shape[0] or reg.shape[1] < t.shape[1]:
            scores[name] = -1.0
        else:
            scores[name] = float(cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED).max())
    return all(v >= thr for v in scores.values()), scores


def frames_from_ffmpeg(ffmpeg, path, vf, w, h, cancel, gpu=True, ss=None, dur=None):
    """Liest vorverarbeitete Frames als Rohdaten (BGR) per Pipe. Nutzt GPU-Decoding, wenn moeglich.
    ss/dur: nur dieses Zeitfenster (in Sekunden) lesen."""
    n = w * h * 3
    seek = (["-ss", f"{ss:.3f}"] if ss is not None else []) + (["-t", f"{dur:.3f}"] if dur is not None else [])
    for hw in ((True, False) if gpu else (False,)):
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"] + (["-hwaccel", "auto"] if hw else []) + \
              ["-skip_loop_filter", "all"] + seek + \
              ["-i", path, "-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             creationflags=NOWIN, bufsize=n * 8)
        got = 0
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    return
                buf = p.stdout.read(n)
                if len(buf) < n:
                    break
                yield got, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                got += 1
        finally:
            p.kill()
            p.wait()
        if got:
            return


def keyframes_from_ffmpeg(ffmpeg, path, vf, w, h, cancel, ss=None, dur=None):
    """Liest NUR die Keyframes (I-Bilder) mit ihren Zeitpunkten - sehr schnell, da fast nichts dekodiert wird.
    Liefert (zeit_in_s, bild). ss/dur: nur dieses Zeitfenster."""
    n = w * h * 3
    off = ss or 0.0
    seek = (["-ss", f"{ss:.3f}"] if ss is not None else []) + (["-t", f"{dur:.3f}"] if dur is not None else [])
    for opt in (["-fps_mode", "passthrough"], ["-vsync", "0"]):
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats", "-skip_frame", "nokey"] + seek + ["-i", path,
               "-an", "-sn", "-vf", vf + ",showinfo"] + opt + ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN,
                             bufsize=n * 4)
        pts = []

        def reader(pr=p, lst=pts):
            for line in iter(pr.stderr.readline, b""):
                m = re.search(rb"pts_time:\s*(-?[0-9.]+)", line)
                if m:
                    lst.append(float(m.group(1)))
        th = threading.Thread(target=reader, daemon=True)
        th.start()
        got = 0
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    return
                buf = p.stdout.read(n)
                if len(buf) < n:
                    break
                t0 = time.time()
                while len(pts) <= got and time.time() - t0 < 2.0:
                    time.sleep(0.001)
                if len(pts) > got:
                    yield off + pts[got], np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                got += 1
        finally:
            p.kill()
            p.wait()
        if got:
            return


# Groesster Keyframe-Abstand, bei dem die Schnellsuche noch genutzt wird (OBS-Standard: ca. 4,2 s)
MAX_KEYGAP = 4.6


def group_hits(hits, gap):
    kills, last = [], None
    for t in hits:
        if last is None or t - last > gap:
            kills.append(t)
        last = t
    return kills


class SafeCap:
    """Wie cv2.VideoCapture, liest aber über FFmpeg weiter, wenn OpenCV das Video nicht dekodieren kann
    (z. B. AV1-Aufnahmen neuerer Grafikkarten, manche HEVC-/10-Bit-Dateien)."""

    def __init__(self, path):
        self.path = path
        self.cap = cv2.VideoCapture(path)
        self.pos = 0
        self.ff = False
        self.meta = {}
        self.proc = None
        self.proc_pos = -1
        ok = self.cap.isOpened() and (self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) > 0
        if ok:
            ok, fr = self.cap.read()                 # kann OpenCV wirklich Bilder liefern?
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        if not ok:
            self.ff = True
            self._probe()

    def _probe(self):
        ff = find_ffmpeg()
        m = {"fps": 60.0, "w": 0, "h": 0, "dur": 0.0}
        try:
            e = subprocess.run([ff, "-hide_banner", "-i", self.path], capture_output=True, text=True, errors="ignore",
                               creationflags=NOWIN, timeout=20).stderr
            d = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", e)
            if d:
                m["dur"] = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
            v = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", e)
            if v:
                m["w"], m["h"] = int(v.group(1)), int(v.group(2))
            f = re.search(r"([\d.]+) fps", e) or re.search(r"([\d.]+) tbr", e)
            if f:
                m["fps"] = float(f.group(1)) or 60.0
        except Exception:
            pass
        self.meta = m

    def isOpened(self):
        return (not self.ff and self.cap.isOpened()) or (self.ff and self.meta.get("w", 0) > 0)

    def get(self, prop):
        if prop == cv2.CAP_PROP_POS_FRAMES and self.ff:
            return float(self.pos)
        if prop == cv2.CAP_PROP_POS_MSEC and self.ff:
            return self.pos / (self.meta.get("fps", 60.0) or 60.0) * 1000.0
        if not self.ff or not self.meta:
            v = self.cap.get(prop)
            if v or not self.ff:
                return v
        m = self.meta or {}
        if prop == cv2.CAP_PROP_FPS:
            return m.get("fps", 60.0)
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return m.get("dur", 0.0) * m.get("fps", 60.0)
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return m.get("w", 0)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return m.get("h", 0)
        return 0.0

    def set(self, prop, v):
        if prop == cv2.CAP_PROP_POS_FRAMES:
            self.pos = int(v)
        elif prop == cv2.CAP_PROP_POS_MSEC:
            self.pos = int(round(v / 1000.0 * (self.get(cv2.CAP_PROP_FPS) or 60.0)))
        if not self.ff:
            return self.cap.set(prop, v)
        return True

    def read(self):
        if not self.ff:
            ok, fr = self.cap.read()
            if ok:
                self.pos += 1
                return ok, fr
            self.ff = True                           # OpenCV gibt auf: ab jetzt über FFmpeg
            if not self.meta:
                self._probe()
                if not self.meta.get("w"):
                    self.meta.update(w=int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                                     h=int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        m = self.meta
        w, h, fps = m.get("w", 0), m.get("h", 0), m.get("fps", 60.0) or 60.0
        if w <= 0 or h <= 0:
            return False, None
        n = w * h * 3
        for attempt in (0, 1):
            if self.proc is None or self.proc_pos != self.pos:   # nicht fortlaufend: Strom neu an der Stelle starten
                self._close_proc()
                t = self.pos / fps
                try:
                    self.proc = subprocess.Popen(
                        [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", self.path,
                         "-an", "-sn", "-vf", f"fps={fps:.4f}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NOWIN, bufsize=n * 2)
                except Exception:
                    return False, None
                self.proc_pos = self.pos
            try:
                b = self.proc.stdout.read(n)
            except Exception:
                b = b""
            if len(b) == n:
                self.pos += 1
                self.proc_pos = self.pos
                return True, np.frombuffer(b, np.uint8).reshape(h, w, 3).copy()
            self._close_proc()
            if attempt == 0 and self.pos > 0 and self.pos / fps < m.get("dur", 0) - 0.05:
                continue
            break
        return False, None

    def grab(self):
        if not self.ff:
            ok = self.cap.grab()
            if ok:
                self.pos += 1
                self._grabbed = None
                return ok
        ok, self._grabbed = self.read()
        return ok

    def retrieve(self):
        if getattr(self, "_grabbed", None) is not None:
            return True, self._grabbed
        if not self.ff:
            return self.cap.retrieve()
        return False, None

    def _close_proc(self):
        p_ = getattr(self, "proc", None)
        self.proc = None
        if p_ is not None:
            try:
                p_.kill()
                p_.wait(timeout=1)
            except Exception:
                pass

    def release(self):
        self._close_proc()
        try:
            self.cap.release()
        except Exception:
            pass


LAST_DEBUG = {}


def scan_video(path, pre, post, server, sens=6, progress=None, cancel=None, on_kill=None, gpu=True, thorough=False):
    """server: Dict mit "kind" = "bruder" | "notify" | "xhit". Gibt (clips, info) zurueck.
    clips: Liste von (start, ende, kill_zeit, thumbnail_bgr)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg nicht gefunden.")
    kind = server["kind"]
    dbg = {"path": path, "server": server.get("name", ""), "kind": kind, "sens": sens, "thorough": thorough,
           "top": [], "checks": [], "cal": [], "lock": threading.Lock()}
    LAST_DEBUG.clear()
    LAST_DEBUG.update(dbg)
    dbg = LAST_DEBUG
    cap = SafeCap(path)
    dbg["ff"] = cap.ff
    if not cap.isOpened():
        raise RuntimeError("Video konnte nicht geoeffnet werden.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = total / fps
    dbg.update(fps=fps, total=total, w=w, h=h, duration=duration)

    def prog(v):
        if progress:
            progress(max(0.0, min(1.0, v)))

    hits = []
    info = {}
    pre_verified = False
    t_start = time.time()
    if kind in ("bruder", "notify"):
        if kind == "bruder":
            ow = 960
            k = 0.5 * (16 / 9) * h / w            # Skalierung der Vorlage (1080p-Referenz -> aktuelles Bild)
            hdr, words = load_notify_assets(BRUDER)
            tpl = cv2.resize(hdr, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            need = max(20, int(tpl.sum() * 0.35))
            pad = max(1, int(8 * k))               # nicht-lila Rand: lila Flaechen sind keine Ueberschrift
            tpl = cv2.copyMakeBorder(tpl, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
            thr = min(0.85, max(0.5, 0.95 - 0.05 * sens))
            ks, region = h / 1080.0, TEXT_REGION_WIDE
        else:
            ow = 960
            d = os.path.join(DATA_DIR, "servers", server["id"])
            anchor = read_image(os.path.join(d, "anchor.png"))
            if anchor is None:
                raise RuntimeError("Für diesen Server fehlt die Vorlage. Bitte die Kill-Meldung neu markieren.")
            anchor = cv2.cvtColor(anchor, cv2.COLOR_BGR2GRAY)          # Graustufen: schneller und robuster
            words = {f: read_image(os.path.join(d, f), cv2.IMREAD_GRAYSCALE)        # alle Kill-Woerter muessen da sein
                     for f in sorted(os.listdir(d)) if f.startswith("word") and f.endswith(".png")}
            words = {n: w_ for n, w_ in words.items() if w_ is not None}
            if not words:
                raise RuntimeError("Für diesen Server fehlt das Kill-Wort. Bitte die Vorlage neu markieren.")
            ks = h / float(server.get("ref_h", 1080))
            k = ks * ow / w
            tpl = cv2.resize(anchor, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            if min(tpl.shape[:2]) < 5:
                raise RuntimeError("Der markierte feste Teil der Meldung ist zu klein.")
            thr = min(0.85, max(0.5, 0.95 - 0.04 * sens))
            rx0, ry0, rx1, ry1 = server["region"]
            region = (ry0, ry1, rx0, rx1)
        oh = int(round(ow * h / w / 2)) * 2
        scale_vf = f"scale={ow}:{oh}:flags=area"
        text_thr = min(0.85, max(0.70, 0.85 - 0.02 * sens))
        # --- automatische Groessen-Anpassung: Meldungen koennen je nach Aufloesung / HUD-Groesse
        #     groesser oder kleiner sein als bei der Vorlage. Dafuer werden moegliche Meldungen gesammelt.
        base_k, base_ks = k, ks
        samples, samp_lock = [], threading.Lock()
        scale_f = 1.0

        def make_tpl(f):
            nonlocal tpl, pad, need, k, ks
            k, ks = base_k * f, base_ks * f
            if kind == "bruder":
                t_ = cv2.resize(hdr, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
                need = max(20, int(t_.sum() * 0.35))
                pad = max(1, int(8 * k))
                tpl = cv2.copyMakeBorder(t_, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
            else:
                tpl = cv2.resize(anchor, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)

        def collect(x):
            if len(samples) < 24:
                with samp_lock:
                    samples.append(x)

        def calibrate():
            """Waehlt die Groesse, bei der die gesammelten Meldungen am besten zur Vorlage passen."""
            if len(samples) < 2:
                return 1.0
            best_f, best_s, s_one = 1.0, -1.0, -1.0
            for f in np.arange(0.70, 1.41, 0.05):
                if kind == "bruder":
                    t_ = cv2.resize(hdr, None, fx=base_k * f, fy=base_k * f, interpolation=cv2.INTER_AREA)
                    p_ = max(1, int(8 * base_k * f))
                    t_ = cv2.copyMakeBorder(t_, p_, p_, p_, p_, cv2.BORDER_CONSTANT, value=0)
                else:
                    t_ = cv2.resize(anchor, None, fx=base_k * f, fy=base_k * f, interpolation=cv2.INTER_AREA)
                sc = []
                for s_ in samples:
                    if s_.shape[0] > t_.shape[0] and s_.shape[1] > t_.shape[1] and min(t_.shape[:2]) >= 4:
                        sc.append(float(cv2.matchTemplate(s_, t_, cv2.TM_CCOEFF_NORMED).max()))
                if not sc:
                    continue
                # Bewertung: wie viele Beispiele werden bei dieser Groesse wirklich erkannt (kleine Vorlagen passen
                # sonst "zufaellig" besser) - bei Gleichstand zaehlt die mittlere Uebereinstimmung
                hit_n = sum(1 for x in sc if x >= thr)
                v = (hit_n, float(np.mean(sorted(sc, reverse=True)[:3])))
                dbg["cal"].append((round(float(f), 2), hit_n, round(v[1], 3)))
                if abs(f - 1.0) < 0.01:
                    s_one = v
                if best_s == -1.0 or v > best_s:
                    best_f, best_s = float(f), v
            # nur wechseln, wenn bei einer anderen Groesse klar mehr Meldungen erkannt werden
            if best_s != -1.0 and s_one != -1.0 and best_s[0] > s_one[0] and best_s[1] > 0.45:
                return best_f
            return 1.0

        good_scale = [1.0]
        x_half = max(20, int(60 * h / 1080))

        def x_near(cap_, t):
            """Zusatz-Sicherheit: war kurz vor der Meldung das rote Kill-X in der Bildmitte zu sehen?"""
            for dt in (-1.2, -0.9, -0.6, -0.3, 0.0):
                tt = t + float(dt)
                if tt < 0 or tt > duration - 0.05:
                    continue
                cap_.set(cv2.CAP_PROP_POS_FRAMES, int(round(tt * fps)))
                ok_, f_ = cap_.read()
                if not ok_:
                    continue
                cy, cx = f_.shape[0] // 2, f_.shape[1] // 2
                roi = cv2.resize(f_[cy - x_half:cy + x_half, cx - x_half:cx + x_half], (120, 120),
                                 interpolation=cv2.INTER_AREA)
                if is_hitmarker(roi, 40):
                    return True
            return False

        def verify_or_x(cap_, fr, loc, t):
            """Text muss passen. Ist der Text nur knapp zu unsicher, entscheidet das rote X mit."""
            ok = verify(fr, loc)
            if not ok:
                ok = best_text[0] >= text_thr - 0.08 and x_near(cap_, t)
            try:
                x, y = int(loc[0]), int(loc[1])
                crop = fr[max(0, y - int(30 * ks)):y + int(140 * ks), max(0, x - int(30 * ks)):x + int(520 * ks)].copy()
                with dbg["lock"]:
                    if len(dbg["checks"]) < 8:
                        dbg["checks"].append((float(t), round(best_text[0], 3), bool(ok), crop))
            except Exception:
                pass
            return ok

        best_text = [0.0]

        def verify(fr, loc):
            """Text-Pruefung ("Du hast" UND "getoetet" muessen da sein). Probiert mehrere Groessen, damit auch
            Aufnahmen mit anderer Aufloesung / HUD-Groesse sicher erkannt werden; die passende Groesse wird gemerkt."""
            order = [good_scale[0]] + [s for s in (1.0, 0.95, 1.05, 0.9, 1.1, 0.85, 1.15, 0.8, 1.2, 0.75, 1.27)
                                       if abs(s - good_scale[0]) > 1e-6]
            best_text[0] = 0.0
            for s_ in order:
                ok_, sc_ = verify_text(fr, loc, ks * s_, words, text_thr, region)
                if sc_:
                    best_text[0] = max(best_text[0], min(sc_.values()))
                if ok_:
                    good_scale[0] = s_
                    return True
            return False

        def _dbg_top(mx, fr, ml):
            """Diagnose: die besten Fast-Treffer merken (auch wenn sie unter der Schwelle liegen)."""
            top = dbg["top"]
            if len(top) >= 6 and mx <= top[-1][0]:
                return
            with dbg["lock"]:
                top.append((float(mx), fr.copy(), ml))
                top.sort(key=lambda x: -x[0])
                del top[6:]

        smask = [None]          # 0 = feste lila Flaechen (HUD), die immer zu sehen sind -> ignorieren

        def build_static():
            """Ueber das ganze Video verteilt Bilder ansehen: was fast immer lila ist, gehoert zum HUD
            (Geld, Waffe, Tacho ...) und ist keine Meldung - eine Kill-Meldung steht nur ein paar Sekunden."""
            if kind != "bruder" or duration < 20:
                return
            pts = [duration * (i + 0.5) / 24 for i in range(24)]

            def one(t):
                if cancel is not None and cancel.is_set():
                    return None
                for _, fr in frames_from_ffmpeg(ffmpeg, path, scale_vf, ow, oh, cancel, False, t, 0.1):
                    return purple_mask(fr)
                return None
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(4) as ex:
                ms = [m_ for m_ in ex.map(one, pts) if m_ is not None]
            if len(ms) < 8:
                return
            st = (np.mean(ms, axis=0) >= 0.45).astype(np.uint8)
            dbg["static"] = int(st.sum())
            if st.sum() < 30:
                return
            st = cv2.dilate(st, np.ones((7, 7), np.uint8))
            smask[0] = (1 - st).astype(np.float32)

        def drop_static(hs, gap, max_run=25.0):
            """Treffer, die minutenlang an derselben Stelle stehen, sind HUD und keine Meldung."""
            by = {}
            for h_ in hs:
                if h_[2] is not None:
                    by.setdefault((int(h_[2][0] // 40), int(h_[2][1] // 40)), []).append(h_)
            bad = set()
            for lst in by.values():
                lst.sort(key=lambda x: x[1])            # x[1] = Zeitpunkt des Bildes
                run = [lst[0]]
                for a_, b_ in zip(lst, lst[1:] + [None]):
                    if b_ is not None and b_[1] - a_[1] <= gap:
                        run.append(b_)
                        continue
                    if run[-1][1] - run[0][1] > max_run:
                        bad.update(id(x) for x in run)
                    if b_ is not None:
                        run = [b_]
            if bad:
                dbg["static_hits"] = dbg.get("static_hits", 0) + len(bad)
            return [h_ for h_ in hs if id(h_) not in bad]

        def peaks(res, mx, ml, off):
            """Bis zu 3 Stellen ueber der Schwelle (HUD-Feld und echte Meldung koennen gleichzeitig da sein)."""
            out_ = []
            th_, tw_ = tpl.shape[:2]
            while mx >= thr and len(out_) < 3:
                out_.append(((ml[0] + off) * w / ow, (ml[1] + off) * w / ow))
                x0_, y0_ = max(0, ml[0] - tw_ // 2), max(0, ml[1] - th_ // 2)
                res[y0_:ml[1] + th_ // 2 + 1, x0_:ml[0] + tw_ // 2 + 1] = -1.0
                _, mx, _, ml = cv2.minMaxLoc(res)
            return out_

        def detect_multi(fr):
            """Alle Stellen (Lage), an denen im verkleinerten Bild eine Meldung stehen koennte."""
            if kind == "bruder":
                m = purple_mask(fr)
                if smask[0] is not None and smask[0].shape == m.shape:
                    m *= smask[0]
                if m.sum() >= need * 0.5:
                    collect(m)
                if m.sum() >= need:
                    res = cv2.matchTemplate(m, tpl, cv2.TM_CCOEFF_NORMED)
                    _, mx, _, ml = cv2.minMaxLoc(res)
                    _dbg_top(mx, fr, ml)
                    return peaks(res, mx, ml, pad)
                return []
            g_ = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            if g_.shape[0] < tpl.shape[0] or g_.shape[1] < tpl.shape[1]:
                return []
            res = cv2.matchTemplate(g_, tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, ml = cv2.minMaxLoc(res)
            _dbg_top(mx, fr, ml)
            if mx >= 0.35:
                collect(g_)
            return peaks(res, mx, ml, 0)

        def detect(fr):
            """Gibt (Treffer, Lage der besten Stelle) fuer ein verkleinertes Bild zurueck."""
            locs_ = detect_multi(fr)
            return (True, locs_[0]) if locs_ else (False, None)

        # --- Schnellsuche: nur Keyframes ansehen (die Meldung steht mehrere Sekunden, also wird sie getroffen)
        fast = None
        times, khits, sparse = [], [], False
        # Probe: sind die Keyframes dicht genug? (sonst gruendliche Suche)
        probe = [t for t, _ in keyframes_from_ffmpeg(ffmpeg, path, "scale=16:16", 16, 16, cancel, None, 75)]
        if thorough or (len(probe) >= 4 and float(np.median(np.diff(probe))) > MAX_KEYGAP):
            sparse = True
        def fast_pass():
            times_, khits_ = [], []
            nseg = max(1, min(os.cpu_count() or 2, 6, int(duration // 90)))
            bounds = [duration * i / nseg for i in range(nseg + 1)]
            parts = [None] * nseg
            done = [0.0] * nseg

            def work(i):
                ts, hs = [], []
                for t, fr in keyframes_from_ffmpeg(ffmpeg, path, scale_vf, ow, oh, cancel, bounds[i],
                                                   bounds[i + 1] - bounds[i] + 0.05):
                    if t < bounds[i] - 0.05 or t >= bounds[i + 1]:
                        continue                    # Grenzbild gehoert zum Nachbarabschnitt
                    ts.append(t)
                    for loc in detect_multi(fr):
                        hs.append((t, t, loc))
                    done[i] = t - bounds[i]
                parts[i] = (ts, hs)
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(nseg) as ex:
                futs = [ex.submit(work, i) for i in range(nseg)]
                while not all(f.done() for f in futs):
                    prog(0.9 * sum(done) / max(1.0, duration))
                    time.sleep(0.1)
                for f in futs:
                    f.result()
            for ts, hs in parts:
                times_ += ts
                khits_ += hs
            times_.sort()
            khits_.sort(key=lambda x: x[0])
            return times_, khits_

        build_static()
        if not sparse and len(probe) >= 2 and not (cancel is not None and cancel.is_set()):
            times, khits = fast_pass()
            scale_f = calibrate()
            if scale_f != 1.0 and not (cancel is not None and cancel.is_set()):
                make_tpl(scale_f)                 # Meldungen sind anders gross: mit passender Groesse neu suchen
                times2, khits2 = fast_pass()
                # beide Durchgaenge behalten: was bei Originalgroesse gefunden wurde, geht nie verloren
                seen_t = {round(x[0], 3) for x in khits2}
                khits = sorted(khits2 + [x for x in khits if round(x[0], 3) not in seen_t], key=lambda x: x[0])
                times = times2 or times
        elif not (cancel is not None and cancel.is_set()):
            # gruendliche Suche: vorher auf Keyframes Beispiele sammeln, um die Groesse anzupassen
            t_cal = time.time()
            for t_, fr_ in keyframes_from_ffmpeg(ffmpeg, path, scale_vf, ow, oh, cancel):
                detect(fr_)
                if len(samples) >= 12 or time.time() - t_cal > 5.0:   # hoechstens ein paar Sekunden
                    break
            scale_f = calibrate()
            if scale_f != 1.0:
                make_tpl(scale_f)
        info["groesse"] = round(scale_f, 2)
        dbg.update(scale=scale_f, samples=len(samples), probe=len(probe), thr=thr, text_thr=text_thr,
                   probe_gap=(float(np.median(np.diff(probe))) if len(probe) >= 2 else None), sparse=sparse,
                   keyframes=len(times), khits=len(khits))
        if cancel is not None and cancel.is_set():
            cap.release()
            return [], {"notices": 0, "mode": "abgebrochen"}
        keygap = float(np.median(np.diff(times))) if len(times) >= 3 else None
        if keygap is not None and khits:
            khits = drop_static(khits, keygap * 1.5 + 0.3)
        if not sparse and keygap is not None and keygap <= MAX_KEYGAP:
            fast = (khits, keygap)

        if fast is not None:
            khits, keygap = fast
            gap = keygap * 1.05 + 0.3          # nur aufeinanderfolgende Keyframes gehoeren zu einer Meldung
            info['mode'] = f'schnell (Keyframe alle {keygap:.1f} s)'
            # Anzahl aller "Information"-Meldungen (nur fuer die Anzeige)
            info["notices"] = len(group_hits([x[0] for x in khits], gap))
            # Jeden Treffer einzeln auf den Kill-Text pruefen (Springen auf einen Keyframe ist billig)
            from concurrent.futures import ThreadPoolExecutor
            tl = threading.local()
            caps_open = []

            def check(item):
                t, _, loc = item
                if loc is None or (cancel is not None and cancel.is_set()):
                    return None
                c = getattr(tl, "cap", None)
                if c is None:                       # jeder Arbeiter bekommt sein eigenes Video-Handle
                    c = tl.cap = SafeCap(path)
                    caps_open.append(c)
                c.set(cv2.CAP_PROP_POS_FRAMES, int(round(min(t, duration - 0.1) * fps)))
                ok, fr = c.read()
                return item if ok and verify_or_x(c, fr, loc, t) else None
            with ThreadPoolExecutor(max(1, min(4, os.cpu_count() or 2))) as ex:
                hits = [r for r in ex.map(check, khits) if r is not None]
            for c in caps_open:
                c.release()
            prog(0.95)
            pre_verified = True
            info["mode"] = f"schnell (Keyframe alle {keygap:.1f} s)"
        else:
            info["mode"] = "gruendlich"
            ofps, run_at = 4, {}
            vf = f"fps={ofps},{scale_vf}"
            for i, fr in frames_from_ffmpeg(ffmpeg, path, vf, ow, oh, cancel, gpu):
                locs_ = detect_multi(fr)
                new_ = {}
                for loc in locs_:                       # jede Stelle hat ihre eigene Treffer-Reihe
                    key = (int(loc[0] // 40), int(loc[1] // 40))
                    st_, cnt_ = next((v for k_, v in run_at.items()
                                      if abs(k_[0] - key[0]) <= 1 and abs(k_[1] - key[1]) <= 1), (i, 0))
                    new_[key] = (st_, cnt_ + 1)
                    if cnt_ + 1 >= 2:
                        hits.append((st_ / ofps, i / ofps, loc))
                run_at = new_
                if i % 20 == 0:
                    prog(i / max(1, duration * ofps))
            gap = 1.5
            hits = drop_static(hits, 1.0)
        min_run = 1
    else:
        ofps = 15
        half = max(20, int(60 * h / 1080))
        vf = (f"fps={ofps},crop={2 * half}:{2 * half}:{(w - 2 * half) // 2}:{(h - 2 * half) // 2},"
              f"scale=120:120:flags=area")
        diff = int(70 - 5 * sens)
        run = 0
        for i, fr in frames_from_ffmpeg(ffmpeg, path, vf, 120, 120, cancel, gpu):
            ok_ = is_hitmarker(fr, diff)
            run = run + 1 if ok_ else 0
            if run >= 2:
                hits.append(((i - run + 1) / ofps, i / ofps, None))
            if i % 25 == 0:
                prog(i / max(1, duration * ofps))
        gap = 1.2

    if cancel is not None and cancel.is_set() and not hits:
        cap.release()
        return [], {"notices": 0, "mode": info.get("mode", "")}

    info["t_suche"] = round(time.time() - t_start, 1)
    groups = []
    for t0, t, loc in hits:
        if groups and t - groups[-1]["last"] <= gap:
            g = groups[-1]
            g["last"] = t
            g["locs"].append((t, loc))
        else:
            groups.append({"start": t0, "last": t, "locs": [(t, loc)]})
    info.setdefault("notices", len(groups))

    if kind in ("bruder", "notify"):
        fastmode = info["mode"].startswith("schnell")
        # Nur Meldungen mit dem richtigen Text sind Kills
        good = []
        for gi, g in enumerate(groups):
            if cancel is not None and cancel.is_set():
                break
            if pre_verified:
                good.append(g)
                if on_kill:
                    on_kill(len(good))
                continue
            if fastmode:
                # direkt auf den Keyframes pruefen (Springen auf einen Keyframe kostet fast nichts)
                kt = [x[0] for x in g["locs"]]
                cand = [kt[len(kt) // 2], kt[-1], kt[0]]
            else:
                cand = [g["start"] + d for d in (0.4, 0.9, 1.5, 2.2) if g["start"] + d <= g["last"] + 0.2]
                cand += [g["last"], (g["start"] + g["last"]) / 2]
            ok_any, seen = False, set()
            for t in cand:
                if round(t, 2) in seen:
                    continue
                seen.add(round(t, 2))
                dmin = min(abs(x[0] - t) for x in g["locs"])
                nears = []
                for x in g["locs"]:                 # alle Stellen zu diesem Zeitpunkt pruefen (nicht nur die erste)
                    if x[1] is not None and abs(x[0] - t) <= dmin + 0.6 and \
                            all(abs(x[1][0] - n_[0]) + abs(x[1][1] - n_[1]) > 20 for n_ in nears):
                        nears.append(x[1])
                if not nears:
                    continue
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(min(t, duration - 0.1) * fps)))
                ok, fr = cap.read()
                if ok and any(verify_or_x(cap, fr, n_, t) for n_ in nears[:3]):
                    ok_any = True
                    break
            if ok_any:
                good.append(g)
                if on_kill:
                    on_kill(len(good))
            if progress:
                prog(0.9 + 0.05 * (gi + 1) / max(1, len(groups)))
        groups = good
        info["t_pruefen"] = round(time.time() - t_start - info["t_suche"], 1)
        # Feinsuche nur fuer echte Kills: genauen Beginn der Meldung in einem kurzen Zeitfenster bestimmen
        if fastmode and groups:
            import bisect
            from concurrent.futures import ThreadPoolExecutor

            def refine(g):
                if cancel is not None and cancel.is_set():
                    return g["start"]
                k_i = bisect.bisect_left(times, g["start"] - 0.01) - 1
                s0 = times[k_i] if k_i >= 0 else max(0.0, g["start"] - keygap)
                dur = g["start"] - s0 + 0.2
                flags = [detect(fr)[0] for _, fr in
                         frames_from_ffmpeg(ffmpeg, path, f"fps=10,{scale_vf}", ow, oh, cancel, False, s0, dur)]
                # Beginn = erstes Bild der ununterbrochenen Treffer-Reihe, die bis zum Keyframe reicht
                if flags and flags[-1]:
                    j = len(flags) - 1
                    while j > 0 and flags[j - 1]:
                        j -= 1
                    return min(g["start"], s0 + j / 10.0)
                return g["start"]
            with ThreadPoolExecutor(max(1, min(4, os.cpu_count() or 2))) as ex:
                for g, st in zip(groups, ex.map(refine, groups)):
                    g["start"] = st
            prog(1.0)
    elif on_kill:
        on_kill(len(groups))

    out = []
    for g in groups:
        t = g["start"]
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(min(t + 0.3, duration - 0.2) * fps))
        ok, fr = cap.read()
        th = cv2.resize(fr, (256, int(256 * h / w)), interpolation=cv2.INTER_AREA) if ok else None
        out.append((max(0.0, t - pre), min(duration, t + post), t, th))
    cap.release()
    info["sekunden"] = round(time.time() - t_start, 1)
    dbg.update(info={k: v for k, v in info.items()}, hits=len(hits), kills=len(out))
    return out, info


def write_debug_image(dst):
    """Erstellt ein Diagnose-Bild zum letzten Scan (Messwerte + Ausschnitte), das man einfach weiterschicken kann."""
    d = LAST_DEBUG
    if not d:
        return None
    lines = []
    try:
        e = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", d.get("path", "")], capture_output=True, text=True,
                           errors="ignore", creationflags=NOWIN, timeout=20).stderr
        lines += [ln.strip()[:150] for ln in e.splitlines() if "Duration" in ln or "Stream #" in ln][:5]
    except Exception:
        pass
    f2 = lambda v: "-" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))
    lines += [
        f"ACE {APP_VERSION if 'APP_VERSION' in globals() else ''}  Server: {d.get('server')} ({d.get('kind')})  "
        f"Empf.: {d.get('sens')}  gruendlich: {d.get('thorough')}",
        f"OpenCV->FFmpeg: {d.get('ff')}  fps {f2(d.get('fps'))}  Bilder {d.get('total')}  {d.get('w')}x{d.get('h')}  "
        f"Dauer {f2(d.get('duration'))} s",
        f"Probe-Keyframes {d.get('probe')}  Abstand {f2(d.get('probe_gap'))}  sparse {d.get('sparse')}  "
        f"Keyframes {d.get('keyframes')}  Fast-Treffer {d.get('khits')}  Treffer {d.get('hits')}  Kills {d.get('kills')}",
        f"HUD-Pixel {d.get('static')}  HUD-Treffer entfernt {d.get('static_hits', 0)}  "
        f"Groesse {f2(d.get('scale'))}  Beispiele {d.get('samples')}  Schwelle {f2(d.get('thr'))}  "
        f"Text-Schwelle {f2(d.get('text_thr'))}",
        "Kalibrierung (Groesse:erkannt/Wert): " + "  ".join(f"{c[0]}:{c[1]}/{c[2]}" for c in d.get("cal", [])[:15]),
        f"Info: {d.get('info')}"[:160],
    ]
    W = 1600
    tiles = []
    for mx, fr, ml in d.get("top", [])[:6]:
        t_ = cv2.resize(fr, (480, int(480 * fr.shape[0] / fr.shape[1])))
        k = 480 / fr.shape[1]
        cv2.rectangle(t_, (int(ml[0] * k), int(ml[1] * k)), (int(ml[0] * k) + 60, int(ml[1] * k) + 30), (0, 255, 0), 2)
        cv2.putText(t_, f"Suche {mx:.2f}", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        tiles.append(t_)
    checks = []
    for t, sc, ok, crop in d.get("checks", [])[:8]:
        if crop is None or crop.size == 0:
            continue
        c_ = cv2.resize(crop, (520, max(10, int(520 * crop.shape[0] / max(1, crop.shape[1])))))
        bar = np.zeros((28, 520, 3), np.uint8)
        cv2.putText(bar, f"t={t:.1f}s Text {sc:.2f} {'OK' if ok else 'nein'}", (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 0) if ok else (0, 0, 255), 2)
        checks.append(np.vstack([bar, c_]))
    th = 26 * len(lines) + 20
    rows_h = (tiles[0].shape[0] + 10) * ((len(tiles) + 2) // 3) if tiles else 0
    ch_h = (max(c.shape[0] for c in checks) + 10) * ((len(checks) + 2) // 3) if checks else 0
    img = np.full((th + rows_h + ch_h + 20, W, 3), 24, np.uint8)
    for i, ln in enumerate(lines):
        cv2.putText(img, ln, (10, 24 + 26 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
    y = th
    for i, t_ in enumerate(tiles):
        r, c = divmod(i, 3)
        yy, xx = y + r * (t_.shape[0] + 10), 10 + c * 530
        img[yy:yy + t_.shape[0], xx:xx + t_.shape[1]] = t_
    y += rows_h
    if checks:
        hh = max(c.shape[0] for c in checks)
        for i, c_ in enumerate(checks):
            r, c = divmod(i, 3)
            yy, xx = y + r * (hh + 10), 10 + c * 530
            img[yy:yy + c_.shape[0], xx:xx + c_.shape[1]] = c_
    if os.path.dirname(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    ok, buf = cv2.imencode(".png", img)
    if ok:
        with open(dst, "wb") as f:
            f.write(buf.tobytes())
        return dst
    return None


_HW = {}


def _hw_encoder(ffmpeg, codec):
    """Sucht einmalig einen Hardware-Encoder der Grafikkarte (NVIDIA, AMD, Intel). None = keiner nutzbar."""
    if codec in _HW:
        return _HW[codec]
    names = {"h264": ["h264_nvenc", "h264_amf", "h264_qsv"], "hevc": ["hevc_nvenc", "hevc_amf", "hevc_qsv"]}[codec]
    found = None
    for enc in names:
        try:
            r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "color=c=black:s=256x144:d=0.2", "-c:v", enc, "-f", "null", "-"],
                               capture_output=True, timeout=15, creationflags=NOWIN)
            if r.returncode == 0:
                found = enc
                break
        except Exception:
            pass
    _HW[codec] = found
    return found


def _enc_args(enc, quality):
    """Gleichwertige, sehr hohe Qualitaet fuer die Hardware-Encoder."""
    if enc.endswith("_nvenc"):
        return ["-c:v", enc, "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
    if enc.endswith("_amf"):
        return ["-c:v", enc, "-quality", "quality", "-rc", "cqp", "-qp_i", str(quality - 1),
                "-qp_p", str(quality), "-qp_b", str(quality + 1)]
    return ["-c:v", enc, "-global_quality", str(quality), "-preset", "slower"]


def export_clip(ffmpeg, src, start, end, dst, mode):
    """Schneidet einen Clip. Nutzt die Grafikkarte, wenn moeglich (schnell, PC bleibt bedienbar);
    sonst den Prozessor mit niedriger Prioritaet und nicht allen Kernen."""
    base = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", src,
            "-t", f"{end - start:.3f}"]
    audio = ["-c:a", "aac", "-b:a", "320k"]
    threads = ["-threads", str(max(1, (os.cpu_count() or 4) - 2))]
    flags = NOWIN | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    attempts = []
    if mode == "copy":
        attempts.append(["-c", "copy", "-avoid_negative_ts", "make_zero"])
    elif mode == "h265":
        hw = _hw_encoder(ffmpeg, "hevc")
        if hw:
            attempts.append(_enc_args(hw, 22) + ["-tag:v", "hvc1"] + audio)
        attempts.append(["-c:v", "libx265", "-crf", "20", "-preset", "fast", "-tag:v", "hvc1",
                         "-x265-params", f"log-level=error:pools={max(1, (os.cpu_count() or 4) - 2)}"] + audio)
    else:  # exact: bildgenau, sehr hohe Qualitaet
        hw = _hw_encoder(ffmpeg, "h264")
        if hw:
            attempts.append(_enc_args(hw, 19) + audio)
        attempts.append(["-c:v", "libx264", "-crf", "17", "-preset", "faster"] + threads + audio)
    err = None
    for args in attempts:
        try:
            subprocess.run(base + args + [dst], check=True, creationflags=flags, capture_output=True)
            return
        except subprocess.CalledProcessError as e:
            err = e                       # Hardware-Encoder klappt nicht -> naechste Moeglichkeit
    raise err


class Player(threading.Thread):
    """Dekodiert Video in einem eigenen Thread, damit die Oberflaeche nie wartet (Abspielen und Springen)."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.cap = SafeCap(path)
        self.ok = self.cap.isOpened()
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 60.0
        self.frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.duration = self.frames / self.fps
        self.size = (640, 360)
        self.lock = threading.Lock()
        self.ev = threading.Event()
        self.seek_to = None
        self.playing = False
        self.speed = 1.0
        self.resync = False
        self.ended = False
        self.latest = None
        self.seek_id = 0              # zaehlt jede Sprung-Anfrage
        self.cur_id = 0               # Sprung-Nummer, zu der das zuletzt dekodierte Bild gehoert
        self.stop_flag = False
        self.cur = 0.0
        self.t0 = self.pos0 = 0.0

    def request_seek(self, t):
        with self.lock:
            self.seek_to = t
            self.seek_id += 1
        self.ev.set()

    def set_play(self, on, speed=1.0):
        with self.lock:
            self.playing, self.speed, self.resync, self.ended = on, speed, True, False
        self.ev.set()

    def take(self):
        with self.lock:
            f, self.latest = self.latest, None
        return f

    def stop(self):
        self.stop_flag = True
        self.ev.set()

    def _publish(self, f, kind):
        w, h = self.size
        fh, fw = f.shape[:2]
        sc = max(0.05, min(w / fw, h / fh))
        interp = cv2.INTER_LINEAR if kind == "play" else cv2.INTER_AREA      # beim Abspielen: schnell, im Stand: sauber
        f = cv2.resize(f, (max(2, int(fw * sc)), max(2, int(fh * sc))), interpolation=interp)
        rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
        with self.lock:
            self.latest = (kind, self.cur, rgb, self.cur_id)

    def run(self):
        cap, fps = self.cap, self.fps
        while not self.stop_flag:
            with self.lock:
                seek, self.seek_to = self.seek_to, None
                playing, speed, resync = self.playing, self.speed, self.resync
                self.resync = False
                if seek is not None:
                    self.cur_id = self.seek_id
            if seek is not None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(max(0.0, min(seek, self.duration - 0.1)) * fps))
                ok, f = cap.read()
                if ok:
                    self.cur = (int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1) / fps
                    self._publish(f, "seek")
                resync = True
            if playing:
                if resync:
                    self.t0, self.pos0 = time.perf_counter(), self.cur
                target = self.pos0 + (time.perf_counter() - self.t0) * speed
                lag = int((target - self.cur) * fps)
                for _ in range(min(lag, 10) - 1):
                    cap.grab()
                ok, f = cap.read()
                if not ok:
                    with self.lock:
                        self.playing, self.ended = False, True
                    continue
                self.cur = (int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1) / fps
                wait = (self.cur - target) / speed
                if wait > 0.002:
                    self.ev.wait(min(wait, 0.1))
                    self.ev.clear()
                self._publish(f, "play")
            elif seek is None:
                self.ev.wait(0.05)
                self.ev.clear()
        cap.release()

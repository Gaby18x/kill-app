"""Kill App - schneidet automatisch Kill-Clips aus GTA/FiveM-Aufnahmen.

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
import urllib.request
import cv2
import numpy as np
from PIL import Image, ImageTk

# ----------------------------------------------------------------- Kern ----

APP_VERSION = "1.0"                 # wird beim Build automatisch gesetzt
UPDATE_REPO = "Gaby18x/kill-app"    # oeffentliches Repo mit den Releases


def parse_ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def check_update():
    """Gibt (version, download_url) zurueck, wenn es ein neueres Release gibt, sonst None."""
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": "KillApp", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    if parse_ver(tag) > parse_ver(APP_VERSION):
        for a in data.get("assets", []):
            if a["name"].lower().endswith(".exe"):
                return tag.lstrip("v"), a["browser_download_url"]
    return None


def download_file(url, progress=None):
    dst = os.path.join(tempfile.gettempdir(), "KillApp-Setup.exe")
    req = urllib.request.Request(url, headers={"User-Agent": "KillApp"})
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


def verify_text(frame, anchor_xy, ks, words, thr, region=TEXT_REGION):
    """Prueft im Originalbild, ob in der Box 'Du hast' und 'getoetet' stehen. Gibt (ok, scores) zurueck."""
    ax, ay = anchor_xy
    H, W = frame.shape[:2]
    y0, y1 = int(max(0, ay + region[0] * ks)), int(min(H, ay + region[1] * ks))
    x0, x1 = int(max(0, ax + region[2] * ks)), int(min(W, ax + region[3] * ks))
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


def frames_from_ffmpeg(ffmpeg, path, vf, w, h, cancel, gpu=True):
    """Liest vorverarbeitete Frames als Rohdaten (BGR) per Pipe. Nutzt GPU-Decoding, wenn moeglich."""
    n = w * h * 3
    for hw in ((True, False) if gpu else (False,)):
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"] + (["-hwaccel", "auto"] if hw else []) + \
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


def group_hits(hits, gap):
    kills, last = [], None
    for t in hits:
        if last is None or t - last > gap:
            kills.append(t)
        last = t
    return kills


def scan_video(path, pre, post, server, sens=6, progress=None, cancel=None, on_kill=None, gpu=True):
    """server: Dict mit "kind" = "bruder" | "notify" | "xhit". Gibt (clips, info) zurueck.
    clips: Liste von (start, ende, kill_zeit, thumbnail_bgr)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg nicht gefunden.")
    kind = server["kind"]
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("Video konnte nicht geoeffnet werden.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = total / fps

    hits = []
    if kind in ("bruder", "notify"):
        ofps = 5
        if kind == "bruder":
            ow = 960
            k = 0.5 * (16 / 9) * h / w            # Skalierung der Vorlage (1080p-Referenz -> aktuelles Bild)
            hdr, words = load_notify_assets(BRUDER)
            tpl = cv2.resize(hdr, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            need = max(20, int(tpl.sum() * 0.35))
            pad = max(1, int(8 * k))               # nicht-lila Rand: lila Flaechen sind keine Ueberschrift
            tpl = cv2.copyMakeBorder(tpl, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
            thr = min(0.85, max(0.5, 0.95 - 0.05 * sens))
            ks, region = h / 1080.0, TEXT_REGION
        else:
            ow = 1280
            d = os.path.join(DATA_DIR, "servers", server["id"])
            anchor = read_image(os.path.join(d, "anchor.png"))
            words = {"word": read_image(os.path.join(d, "word.png"), cv2.IMREAD_GRAYSCALE)}
            ks = h / float(server.get("ref_h", 1080))
            k = ks * ow / w
            tpl = cv2.resize(anchor, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            if min(tpl.shape[:2]) < 5:
                raise RuntimeError("Der markierte feste Teil der Meldung ist zu klein.")
            thr = min(0.85, max(0.5, 0.95 - 0.04 * sens))
            rx0, ry0, rx1, ry1 = server["region"]
            region = (ry0, ry1, rx0, rx1)
        oh = int(round(ow * h / w / 2)) * 2
        vf = f"fps={ofps},scale={ow}:{oh}:flags=area"
        text_thr = min(0.85, max(0.55, 0.85 - 0.025 * sens))
        fw, fh = ow, oh
        min_run, gap = 2, 1.5
    else:
        ofps = 15
        half = max(20, int(60 * h / 1080))
        vf = (f"fps={ofps},crop={2 * half}:{2 * half}:{(w - 2 * half) // 2}:{(h - 2 * half) // 2},"
              f"scale=120:120:flags=area")
        diff = int(70 - 5 * sens)
        fw, fh = 120, 120
        min_run, gap = 2, 1.2

    run = 0
    for i, fr in frames_from_ffmpeg(ffmpeg, path, vf, fw, fh, cancel, gpu):
        loc = None
        if kind == "bruder":
            m = purple_mask(fr)
            hit = False
            if m.sum() >= need:
                _, mx, _, ml = cv2.minMaxLoc(cv2.matchTemplate(m, tpl, cv2.TM_CCOEFF_NORMED))
                if mx >= thr:
                    hit, loc = True, ((ml[0] + pad) * w / ow, (ml[1] + pad) * w / ow)
        elif kind == "notify":
            _, mx, _, ml = cv2.minMaxLoc(cv2.matchTemplate(fr, tpl, cv2.TM_CCOEFF_NORMED))
            hit = mx >= thr
            if hit:
                loc = (ml[0] * w / ow, ml[1] * w / ow)
        else:
            hit = is_hitmarker(fr, diff)
        run = run + 1 if hit else 0
        if run >= min_run:
            hits.append(((i - run + 1) / ofps, i / ofps, loc))     # (Beginn des Treffers, jetzt, Lage)
        if progress and i % 25 == 0:
            progress(min(1.0, i / max(1, duration * ofps)))

    groups = []
    for t0, t, loc in hits:
        if groups and t - groups[-1]["last"] <= gap:
            g = groups[-1]
            g["last"] = t
            g["locs"].append((t, loc))
        else:
            groups.append({"start": t0, "last": t, "locs": [(t, loc)]})
    info = {"notices": len(groups)}

    if kind in ("bruder", "notify"):
        # Nur Meldungen mit dem richtigen Text sind Kills
        good = []
        for g in groups:
            ok_any = False
            cand = [g["start"] + d for d in (0.4, 0.9, 1.5, 2.2) if g["start"] + d <= g["last"] + 0.2]
            for t in cand or [(g["start"] + g["last"]) / 2]:
                near = min(g["locs"], key=lambda x: abs(x[0] - t))[1]
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(min(t, duration - 0.1) * fps))
                ok, fr = cap.read()
                if ok and verify_text(fr, near, ks, words, text_thr, region)[0]:
                    ok_any = True
                    break
            if ok_any:
                good.append(g)
                if on_kill:
                    on_kill(len(good))
        groups = good
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
    return out, info


def export_clip(ffmpeg, src, start, end, dst, mode):
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", src, "-t", f"{end - start:.3f}"]
    if mode == "copy":
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    elif mode == "h265":
        cmd += ["-c:v", "libx265", "-crf", "18", "-preset", "medium",
                "-tag:v", "hvc1", "-x265-params", "log-level=error", "-c:a", "copy"]
    else:  # exact: bildgenau, nahezu verlustfrei
        cmd += ["-c:v", "libx264", "-crf", "12", "-preset", "slow", "-c:a", "copy"]
    cmd.append(dst)
    subprocess.run(cmd, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ------------------------------------------------------------------ GUI ----
import queue
import threading
import time
import tkinter as tk
import uuid

import customtkinter as ctk

# "Noir"-Farbwelt
BG, PANEL, CARD, CARD_HI, BORDER = "#08090a", "#0d0e0f", "#131415", "#1a1c1d", "#232527"
TEXT, MUTED, DIM = "#f3f4f4", "#8d9195", "#5c6063"
GREEN, GREEN_HI, GREEN_DK, GREEN_BD = "#1fcb8f", "#35e3a6", "#0f2a20", "#1d6b4f"
SEG_ON, SEG_HI = "#17805b", "#1b9a70"
BLUE, RED, AMBER = "#6cc6f2", "#ef4a4a", "#f5a524"
FONT = "Segoe UI"
SETTINGS = os.path.join(os.path.expanduser("~"), ".killapp.json")

MODES = {
    "Verlustfrei": ("copy", "1:1 Kopie ohne Neu-Kodieren. Blitzschnell, Schnitt am Keyframe."),
    "Bildgenau": ("exact", "Neu kodiert (CRF 12). Bildgenau, praktisch ohne Verlust."),
    "Klein": ("h265", "H.265 (CRF 18). Deutlich kleiner, optisch gleich."),
}

BUILTIN_SERVERS = [
    {"id": "bruder", "name": "Bruder", "kind": "bruder", "builtin": True},
    {"id": "standard", "name": "Standard", "kind": "xhit", "builtin": True},
]
KIND_TEXT = {"bruder": "Kill-Meldung mit Text-Prüfung", "notify": "Kill-Meldung (eigene Vorlage)",
             "xhit": "Roter Kill-X in der Bildmitte"}


def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def fmt_short(t):
    return f"{int(t // 60)}:{int(t % 60):02d}"


class Clip:
    def __init__(self, start, end, kill=None, thumb=None):
        self.start, self.end = start, end
        self.kill = start if kill is None else kill
        self.thumb = thumb          # PIL.Image
        self.card = None
        self.title = self.sub = self.img = None


class Player(threading.Thread):
    """Dekodiert Video in einem eigenen Thread, damit die Oberflaeche nie wartet (Abspielen und Springen)."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.cap = cv2.VideoCapture(path)
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
        self.stop_flag = False
        self.cur = 0.0
        self.t0 = self.pos0 = 0.0

    def request_seek(self, t):
        with self.lock:
            self.seek_to = t
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
            self.latest = (kind, self.cur, rgb)

    def run(self):
        cap, fps = self.cap, self.fps
        while not self.stop_flag:
            with self.lock:
                seek, self.seek_to = self.seek_to, None
                playing, speed, resync = self.playing, self.speed, self.resync
                self.resync = False
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


class Timeline(tk.Canvas):
    """Ueberblick ueber das ganze Video + zoombare Detailansicht mit ziehbaren Clip-Raendern."""
    PAD, H_OV = 14, 34

    def __init__(self, master, app):
        super().__init__(master, height=150, bg=CARD, highlightthickness=0, bd=0)
        self.app = app
        self.v0, self.v1 = 0.0, 60.0
        self.mode = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self.press)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<ButtonRelease-1>", self.release)
        self.bind("<Motion>", self.hover)
        self.bind("<MouseWheel>", lambda e: self.zoom(1 if e.delta > 0 else -1, e.x))
        self.bind("<Button-4>", lambda e: self.zoom(1, e.x))
        self.bind("<Button-5>", lambda e: self.zoom(-1, e.x))

    # --- Koordinaten
    @property
    def w(self):
        return max(100, self.winfo_width())

    def x_of(self, t):
        return self.PAD + (t - self.v0) / (self.v1 - self.v0) * (self.w - 2 * self.PAD)

    def t_of(self, x):
        return self.v0 + (x - self.PAD) / (self.w - 2 * self.PAD) * (self.v1 - self.v0)

    def ox(self, t):
        return self.PAD + t / max(0.1, self.app.duration) * (self.w - 2 * self.PAD)

    def ot(self, x):
        return (x - self.PAD) / (self.w - 2 * self.PAD) * self.app.duration

    def focus_on(self, t0, t1):
        span = max(24.0, (t1 - t0) * 3)
        c = (t0 + t1) / 2
        self.set_view(c - span / 2, c + span / 2)

    def set_view(self, a, b):
        d = max(1.0, self.app.duration)
        span = min(max(4.0, b - a), d)
        a = min(max(0.0, a), d - span)
        self.v0, self.v1 = a, a + span
        self.redraw()

    def zoom(self, direction, x):
        t = self.t_of(x)
        f = 0.75 if direction > 0 else 1.33
        self.set_view(t - (t - self.v0) * f, t + (self.v1 - t) * f)

    # --- Zeichnen
    def redraw(self):
        self.delete("all")
        app, w = self.app, self.w
        if not app.player:
            self.create_text(w / 2, 75, text="Öffne ein Video, um die Zeitleiste zu sehen",
                             fill=MUTED, font=(FONT, 11))
            return
        P, d = self.PAD, app.duration
        # Ueberblick
        self.create_rectangle(P, 8, w - P, 8 + 18, fill="#1b1d1e", outline="")
        for i, c in enumerate(app.clips):
            sel = i == app.sel
            self.create_rectangle(self.ox(c.start), 8, max(self.ox(c.end), self.ox(c.start) + 3), 26,
                                  fill=GREEN if sel else "#3a4a44", outline="")
            self.create_rectangle(self.ox(c.kill) - 1, 8, self.ox(c.kill) + 1, 26, fill=RED, outline="")
        self.create_rectangle(self.ox(self.v0), 6, self.ox(self.v1), 28, outline="#9aa0a4", width=2)
        self.create_line(self.ox(app.pos), 4, self.ox(app.pos), 30, fill="white", width=2)
        # Lineal
        top = self.H_OV + 8
        span = self.v1 - self.v0
        step = next((s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800)
                     if s / span * (w - 2 * P) >= 70), 3600)
        t = int(self.v0 // step) * step
        while t <= self.v1:
            if t >= self.v0:
                x = self.x_of(t)
                self.create_line(x, top + 14, x, top + 22, fill=BORDER)
                self.create_text(x, top + 6, text=fmt_short(t) if step >= 1 else f"{t:.1f}s",
                                 fill=MUTED, font=(FONT, 8))
            t += step
        # Spur
        y0, y1 = top + 28, top + 28 + 62
        self.create_rectangle(P, y0, w - P, y1, fill="#0a0b0c", outline=BORDER)
        for i, c in enumerate(app.clips):
            if c.end < self.v0 or c.start > self.v1:
                continue
            sel = i == app.sel
            xa, xb = max(P, self.x_of(c.start)), min(w - P, self.x_of(c.end))
            self.create_rectangle(xa, y0 + 4, xb, y1 - 4, fill="#12382c" if sel else "#1a1d1e",
                                  outline=GREEN if sel else "#2c2f31", width=2 if sel else 1)
            self.create_text((xa + xb) / 2, (y0 + y1) / 2, text=f"KILL {i + 1:02d}  ·  {c.end - c.start:.1f}s",
                             fill=TEXT if sel else MUTED, font=(FONT, 9, "bold"))
            if sel:
                for x in (self.x_of(c.start), self.x_of(c.end)):
                    if P <= x <= w - P:
                        self.create_rectangle(x - 4, y0 + 8, x + 4, y1 - 8, fill=GREEN, outline="")
                        self.create_line(x, y0 + 20, x, y1 - 20, fill="white")
            k = self.x_of(c.kill)
            if P <= k <= w - P:
                self.create_polygon(k - 6, y0 + 1, k + 6, y0 + 1, k, y0 + 12, fill=RED, outline="")
        px = self.x_of(app.pos)
        if P <= px <= w - P:
            self.create_line(px, top + 20, px, y1, fill="white", width=2)
            self.create_polygon(px - 6, top + 20, px + 6, top + 20, px, top + 30, fill="white", outline="")

    # --- Maus
    def near_handle(self, x):
        a = self.app
        if a.sel is None:
            return None
        c = a.clips[a.sel]
        if abs(x - self.x_of(c.start)) <= 8:
            return "start"
        if abs(x - self.x_of(c.end)) <= 8:
            return "end"
        return None

    def hover(self, e):
        self.config(cursor="sb_h_double_arrow" if e.y > self.H_OV and self.near_handle(e.x) else "")

    def press(self, e):
        a = self.app
        if not a.player:
            return
        if e.y < self.H_OV:
            self.mode = "ov"
            self.drag(e)
            return
        self.mode = self.near_handle(e.x)
        if self.mode:
            return
        t = self.t_of(e.x)
        for i, c in enumerate(a.clips):
            if c.start <= t <= c.end and i != a.sel:
                a.select(i, seek=False, refocus=False)
                break
        self.mode = "scrub"
        a.request_seek(t)

    def drag(self, e):
        a = self.app
        if self.mode == "ov":
            t = self.ot(e.x)
            span = self.v1 - self.v0
            self.set_view(t - span / 2, t + span / 2)
            a.request_seek(t)
        elif self.mode == "scrub":
            a.request_seek(self.t_of(e.x))
        elif self.mode in ("start", "end") and a.sel is not None:
            c = a.clips[a.sel]
            t = min(max(0.0, self.t_of(e.x)), a.duration)
            if self.mode == "start":
                c.start = min(t, c.end - 0.5)
            else:
                c.end = max(t, c.start + 0.5)
            a.clip_changed(a.sel)
            a.request_seek(c.start if self.mode == "start" else c.end)

    def release(self, e):
        self.mode = None




# ------------------------------------------------------------------ Dialoge
def style_entry(e):
    e.configure(fg_color="#1b1d1e", border_color=BORDER, text_color=TEXT, height=38, corner_radius=10)


class RegionPicker(ctk.CTkToplevel):
    """Screenshot laden und in drei Schritten Bereiche mit der Maus markieren."""
    STEPS = [
        ("1 / 3  Fester Teil der Meldung",
         "Ziehe einen Rahmen um etwas, das in JEDER Kill-Meldung gleich aussieht (z. B. Überschrift oder Symbol).",
         GREEN),
        ("2 / 3  Ganze Meldung",
         "Ziehe einen Rahmen um die komplette Meldungsbox mit dem Text.", BLUE),
        ("3 / 3  Kill-Wort",
         "Ziehe einen Rahmen um ein Wort, das NUR bei Kills vorkommt (z. B. „getötet“).", AMBER),
    ]

    def __init__(self, app, path, done):
        super().__init__(app, fg_color=PANEL)
        self.title("Kill-Meldung markieren")
        self.done = done
        self.im = Image.open(path).convert("RGB")
        W, H = self.im.size
        self.s = min(1.0, 1000 / W, 520 / H)
        if self.s < 1.0:
            disp = self.im.resize((int(W * self.s), int(H * self.s)), Image.LANCZOS)
        else:
            disp = self.im
        self.disp_w, self.disp_h = disp.size
        self.geometry(f"{max(760, self.disp_w + 40)}x{self.disp_h + 210}")
        self.transient(app)
        self.step = 0
        self.boxes = [None, None, None]
        self.cur = None
        self.title_lbl = ctk.CTkLabel(self, text="", font=ctk.CTkFont(FONT, 18, "bold"), text_color=TEXT)
        self.title_lbl.pack(anchor="w", padx=20, pady=(16, 2))
        self.help_lbl = ctk.CTkLabel(self, text="", font=ctk.CTkFont(FONT, 12), text_color=MUTED, wraplength=900,
                                     justify="left")
        self.help_lbl.pack(anchor="w", padx=20)
        self.canvas = tk.Canvas(self, width=self.disp_w, height=self.disp_h, bg="#000000", highlightthickness=1,
                                highlightbackground=BORDER, cursor="crosshair")
        self.canvas.pack(padx=20, pady=12)
        self._tk_img = ImageTk.PhotoImage(disp)
        self.canvas.create_image(0, 0, image=self._tk_img, anchor="nw")
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=20)
        self.err = ctk.CTkLabel(bar, text="", text_color=RED, font=ctk.CTkFont(FONT, 11))
        self.err.pack(side="left")
        self.next_btn = ctk.CTkButton(bar, text="Weiter", width=110, height=36, corner_radius=10, fg_color=GREEN,
                                      hover_color=GREEN_HI, text_color="#04120c", state="disabled",
                                      font=ctk.CTkFont(FONT, 12, "bold"), command=self._next)
        self.next_btn.pack(side="right")
        self.back_btn = ctk.CTkButton(bar, text="Zurück", width=90, height=36, corner_radius=10, fg_color="#1b1d1e",
                                      hover_color="#26292a", text_color=TEXT, font=ctk.CTkFont(FONT, 12, "bold"),
                                      command=self._back)
        self.back_btn.pack(side="right", padx=8)
        self._show_step()

    def _show_step(self):
        t, h, col = self.STEPS[self.step]
        self.title_lbl.configure(text=t, text_color=col)
        self.help_lbl.configure(text=h)
        self.next_btn.configure(text="Fertig" if self.step == 2 else "Weiter",
                                state="normal" if self.boxes[self.step] else "disabled")
        self.back_btn.configure(state="normal" if self.step else "disabled")
        self.err.configure(text="")
        self._redraw()

    def _redraw(self):
        self.canvas.delete("box")
        for i, b in enumerate(self.boxes):
            if b and i <= self.step:
                x0, y0, x1, y1 = [v * self.s for v in b]
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=self.STEPS[i][2], width=2, tags="box")

    def _clamp(self, x, y):
        return min(max(0, x), self.disp_w - 1), min(max(0, y), self.disp_h - 1)

    def _press(self, e):
        self.cur = self._clamp(e.x, e.y)

    def _drag(self, e):
        if not self.cur:
            return
        x, y = self._clamp(e.x, e.y)
        self.canvas.delete("live")
        self.canvas.create_rectangle(self.cur[0], self.cur[1], x, y, outline=self.STEPS[self.step][2], width=2,
                                     tags="live")

    def _release(self, e):
        if not self.cur:
            return
        x, y = self._clamp(e.x, e.y)
        x0, x1 = sorted((self.cur[0], x))
        y0, y1 = sorted((self.cur[1], y))
        self.cur = None
        self.canvas.delete("live")
        if x1 - x0 < 6 or y1 - y0 < 4:
            return
        self.boxes[self.step] = tuple(int(v / self.s) for v in (x0, y0, x1, y1))
        self.next_btn.configure(state="normal")
        self.err.configure(text="")
        self._redraw()

    def _back(self):
        if self.step:
            self.step -= 1
            self._show_step()

    def _next(self):
        if not self.boxes[self.step]:
            return
        if self.step < 2:
            self.step += 1
            self._show_step()
            return
        a, r, wd = self.boxes
        if not (r[0] <= wd[0] and r[1] <= wd[1] and wd[2] <= r[2] and wd[3] <= r[3]):
            self.err.configure(text="Das Kill-Wort muss innerhalb der ganzen Meldung liegen.")
            return
        H = self.im.size[1]
        self.done({"anchor": a, "region": r, "word": wd, "image": self.im, "ref_h": H if H >= 700 else 1080})
        self.destroy()


class AddServerDialog(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app, fg_color=PANEL)
        self.app = app
        self.title("Server hinzufügen")
        self.geometry("520x560")
        self.transient(app)
        self.result = None
        ctk.CTkLabel(self, text="Neuer Server", font=ctk.CTkFont(FONT, 22, "bold"), text_color=TEXT).pack(
            anchor="w", padx=26, pady=(22, 2))
        ctk.CTkLabel(self, text="Lege fest, woran Kill App auf diesem Server einen Kill erkennt.",
                     font=ctk.CTkFont(FONT, 12), text_color=MUTED).pack(anchor="w", padx=26)
        ctk.CTkLabel(self, text="NAME", font=ctk.CTkFont(FONT, 10, "bold"), text_color=DIM).pack(
            anchor="w", padx=26, pady=(20, 4))
        self.name = ctk.CTkEntry(self, placeholder_text="z. B. Mein Server")
        style_entry(self.name)
        self.name.pack(fill="x", padx=26)
        ctk.CTkLabel(self, text="ERKENNUNG", font=ctk.CTkFont(FONT, 10, "bold"), text_color=DIM).pack(
            anchor="w", padx=26, pady=(18, 4))
        self.kind = tk.StringVar(value="Roter Kill-X")
        ctk.CTkSegmentedButton(self, values=["Roter Kill-X", "Kill-Meldung"], variable=self.kind, height=34,
                               selected_color=SEG_ON, selected_hover_color=SEG_HI, unselected_color="#1b1d1e",
                               fg_color="#1b1d1e", unselected_hover_color="#26292a", text_color="white",
                               font=ctk.CTkFont(FONT, 12, "bold"), command=lambda v: self._kind_changed()).pack(
            fill="x", padx=26)
        self.info = ctk.CTkLabel(self, text="", font=ctk.CTkFont(FONT, 12), text_color=MUTED, wraplength=460,
                                 justify="left")
        self.info.pack(anchor="w", padx=26, pady=(12, 8))
        self.pick_btn = ctk.CTkButton(self, text="📷  Screenshot der Kill-Meldung wählen …", height=40,
                                      corner_radius=10, fg_color="#1b1d1e", hover_color="#26292a", text_color=TEXT,
                                      border_width=1, border_color="#2a2d2f", font=ctk.CTkFont(FONT, 12, "bold"),
                                      command=self._pick)
        self.pick_lbl = ctk.CTkLabel(self, text="", font=ctk.CTkFont(FONT, 12), text_color=GREEN)
        self.err = ctk.CTkLabel(self, text="", font=ctk.CTkFont(FONT, 11), text_color=RED, wraplength=460,
                                justify="left")
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=26, pady=20)
        ctk.CTkButton(bar, text="Speichern", width=120, height=38, corner_radius=10, fg_color=GREEN,
                      hover_color=GREEN_HI, text_color="#04120c", font=ctk.CTkFont(FONT, 12, "bold"),
                      command=self._save).pack(side="right")
        ctk.CTkButton(bar, text="Abbrechen", width=100, height=38, corner_radius=10, fg_color="#1b1d1e",
                      hover_color="#26292a", text_color=TEXT, font=ctk.CTkFont(FONT, 12, "bold"),
                      command=self.destroy).pack(side="right", padx=8)
        self.err.pack(side="bottom", anchor="w", padx=26)
        self._kind_changed()

    def _kind_changed(self):
        if self.kind.get() == "Roter Kill-X":
            self.info.configure(text="Der rote X-Treffer in der Bildmitte (Hitmarker) wird als Kill gezählt. "
                                     "Funktioniert ohne weitere Einrichtung.")
            self.pick_btn.pack_forget()
            self.pick_lbl.pack_forget()
        else:
            self.info.configure(text="Der Server zeigt bei einem Kill eine Meldung auf dem Bildschirm. Wähle einen "
                                     "Screenshot, auf dem sie zu sehen ist, und markiere drei Bereiche. "
                                     "Am besten ein Vollbild-Screenshot.")
            self.pick_btn.pack(fill="x", padx=26, pady=(0, 4), before=self.err)
            self.pick_lbl.pack(anchor="w", padx=26, before=self.err)

    def _pick(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(filetypes=[("Bilder", "*.png *.jpg *.jpeg *.webp *.bmp"), ("Alle", "*.*")])
        if p:
            try:
                RegionPicker(self.app, p, self._picked)
            except Exception as e:
                self.err.configure(text=f"Bild konnte nicht geladen werden: {e}")

    def _picked(self, res):
        self.result = res
        self.pick_lbl.configure(text="✓  Meldung markiert")
        self.err.configure(text="")

    def _save(self):
        name = self.name.get().strip()
        if not name:
            self.err.configure(text="Bitte einen Namen eingeben.")
            return
        if name.lower() in [s["name"].lower() for s in self.app.all_servers()]:
            self.err.configure(text="Diesen Namen gibt es schon.")
            return
        sid = uuid.uuid4().hex[:8]
        if self.kind.get() == "Roter Kill-X":
            srv = {"id": sid, "name": name, "kind": "xhit"}
        else:
            if not self.result:
                self.err.configure(text="Bitte zuerst die Kill-Meldung markieren.")
                return
            r = self.result
            d = os.path.join(DATA_DIR, "servers", sid)
            os.makedirs(d, exist_ok=True)
            im = cv2.cvtColor(np.array(r["image"]), cv2.COLOR_RGB2BGR)
            ax0, ay0, ax1, ay1 = r["anchor"]
            wx0, wy0, wx1, wy1 = r["word"]
            rx0, ry0, rx1, ry1 = r["region"]
            for fname, crop in (("anchor.png", im[ay0:ay1, ax0:ax1]), ("word.png", im[wy0:wy1, wx0:wx1])):
                cv2.imencode(".png", crop)[1].tofile(os.path.join(d, fname))
            srv = {"id": sid, "name": name, "kind": "notify", "ref_h": r["ref_h"],
                   "region": [rx0 - ax0, ry0 - ay0, rx1 - ax0, ry1 - ay0]}
        self.app.add_server(srv)
        self.destroy()


class ConfirmDialog(ctk.CTkToplevel):
    def __init__(self, app, text, yes_text, on_yes):
        super().__init__(app, fg_color=PANEL)
        self.title("Bestätigen")
        self.geometry("420x190")
        self.transient(app)
        ctk.CTkLabel(self, text=text, font=ctk.CTkFont(FONT, 14), text_color=TEXT, wraplength=370,
                     justify="left").pack(anchor="w", padx=26, pady=(28, 10))
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=26, pady=20)
        ctk.CTkButton(bar, text=yes_text, width=110, height=38, corner_radius=10, fg_color=RED, hover_color="#ff6b6b",
                      text_color="white", font=ctk.CTkFont(FONT, 12, "bold"),
                      command=lambda: (self.destroy(), on_yes())).pack(side="right")
        ctk.CTkButton(bar, text="Abbrechen", width=100, height=38, corner_radius=10, fg_color="#1b1d1e",
                      hover_color="#26292a", text_color=TEXT, font=ctk.CTkFont(FONT, 12, "bold"),
                      command=self.destroy).pack(side="right", padx=8)



class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        super().__init__(fg_color=BG)
        self.title("Kill App")
        # Kleine Bildschirme: Oberflaeche automatisch verkleinern
        sc = min(1.0, max(0.7, (self.winfo_screenheight() - 60) / 880))
        sc = min(sc, max(0.7, (self.winfo_screenwidth() - 20) / 1300))
        if sc < 0.98:
            ctk.set_widget_scaling(sc)
            ctk.set_window_scaling(sc)
        self.geometry("1440x880")
        self.minsize(1280, 820)
        if sys.platform.startswith("win"):
            self.after(60, lambda: self.state("zoomed"))
        self.ffmpeg = find_ffmpeg()
        self.st = self.load_settings()
        self.server = None
        self.path = None
        self.player = None
        self.fps, self.duration, self.pos = 60.0, 0.0, 0.0
        self.last_rgb = None
        self._last_tl = 0.0
        self._tick = 0
        self.clips, self.sel = [], None
        self.cancel = threading.Event()
        self.scanning = False
        self.playing = False
        self.play_clip = None
        self.speed = 1.0
        self._img = None
        self.update_url = None
        self.page = "picker"
        self.q = queue.Queue()
        self.mode_var = tk.StringVar(value=self.st["mode"] if self.st["mode"] in MODES else "Verlustfrei")
        self.var_loop = tk.BooleanVar(value=self.st["loop"])
        self.var_open = tk.BooleanVar(value=self.st["open"])
        self.var_autoupd = tk.BooleanVar(value=self.st["autoupd"])
        self.var_gpu = tk.BooleanVar(value=self.st["gpu"])
        self.upd_btns = []
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.build_picker()
        self.build_work()
        self.toast_lbl = ctk.CTkLabel(self, text="", fg_color=GREEN, text_color="#04120c", corner_radius=16,
                                      height=38, font=ctk.CTkFont(FONT, 13, "bold"))
        self.show_picker()
        self.bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(30, self.pump)
        self.after(15, self.poll_player)
        self.after(200, self.check_ffmpeg)
        self.after(300, self.set_icon)
        if self.var_autoupd.get():
            self.after(1500, lambda: self.start_update_check(False))

    # ------------------------------------------------------------ Infrastruktur
    def load_settings(self):
        d = {"pre": 4.0, "post": 2.0, "sens": 6, "mode": "Verlustfrei", "open": True, "loop": True,
             "autoupd": True, "gpu": True, "servers": [], "last_server": "bruder"}
        try:
            with open(SETTINGS) as f:
                d.update(json.load(f))
        except Exception:
            pass
        if not isinstance(d.get("servers"), list):
            d["servers"] = []
        return d

    def save_settings(self):
        self.st.update(pre=self.s_pre.get(), post=self.s_post.get(), sens=int(self.s_sens.get()),
                       mode=self.mode_var.get(), open=self.var_open.get(), loop=self.var_loop.get(),
                       autoupd=self.var_autoupd.get(), gpu=self.var_gpu.get())
        try:
            with open(SETTINGS, "w") as f:
                json.dump(self.st, f)
        except Exception:
            pass

    def on_close(self):
        self.save_settings()
        self.playing = False
        self.cancel.set()
        if self.player:
            self.player.stop()
        self.destroy()

    def ui(self, fn, *args):
        """Thread-sicher: fuehrt fn(*args) im Hauptthread aus."""
        self.q.put((fn, args))

    def pump(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(30, self.pump)

    def set_icon(self):
        try:
            if sys.platform.startswith("win"):
                self.iconbitmap(resource("assets", "icon.ico"))
            else:
                self.iconphoto(True, tk.PhotoImage(file=resource("assets", "logo.png")))
        except Exception:
            pass

    def check_ffmpeg(self):
        if not self.ffmpeg:
            self.toast("FFmpeg nicht gefunden – Export nicht möglich", RED)

    def toast(self, text, color=GREEN):
        self.toast_lbl.configure(text=f"   {text}   ", fg_color=color,
                                 text_color="#04120c" if color in (GREEN, GREEN_HI, AMBER) else "white")
        self._toast_id = getattr(self, "_toast_id", 0) + 1
        tid = self._toast_id
        self.toast_lbl.place(relx=0.5, rely=0.94, anchor="center")
        self.toast_lbl.lift()
        self.after(3200, lambda: tid == self._toast_id and self.toast_lbl.place_forget())

    # ------------------------------------------------------------ Bausteine
    def t(self, parent, text, size=12, color=TEXT, bold=False, **kw):
        return ctk.CTkLabel(parent, text=text, text_color=color,
                            font=ctk.CTkFont(FONT, size, "bold" if bold else "normal"), **kw)

    def button(self, parent, text, cmd, kind="dark", **kw):
        styles = {
            "green": dict(fg_color=GREEN, hover_color=GREEN_HI, text_color="#04120c"),
            "dark": dict(fg_color="#1b1d1e", hover_color="#26292a", text_color=TEXT, border_width=1,
                         border_color="#2a2d2f"),
            "ghost": dict(fg_color="#1b1d1e", hover_color="#26292a", text_color=TEXT),
            "danger": dict(fg_color=RED, hover_color="#ff6b6b", text_color="white"),
            "flat": dict(fg_color="transparent", hover_color="#1b1d1e", text_color=MUTED),
        }[kind]
        kw.setdefault("height", 36)
        kw.setdefault("corner_radius", 10)
        return ctk.CTkButton(parent, text=text, command=cmd, font=ctk.CTkFont(FONT, 12, "bold"), **styles, **kw)

    def seg(self, parent, values, **kw):
        return ctk.CTkSegmentedButton(parent, values=values, height=30, selected_color=SEG_ON,
                                      selected_hover_color=SEG_HI, text_color="white", unselected_color="#1b1d1e",
                                      fg_color="#1b1d1e", unselected_hover_color="#26292a",
                                      font=ctk.CTkFont(FONT, 11, "bold"), **kw)

    def section(self, parent, row, num, title, **grid):
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        f.grid(row=row, column=0, sticky="nsew", pady=(0, 10), **grid)
        head = ctk.CTkFrame(f, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 6))
        self.t(head, str(num), 11, GREEN, True, fg_color=GREEN_DK, corner_radius=10, width=22, height=22).pack(
            side="left")
        self.t(head, title, 15, TEXT, True).pack(side="left", padx=10)
        return f, head

    def slider_row(self, parent, title, lo, hi, steps, value, unit_fn):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=1)
        row.grid_columnconfigure(1, weight=1)
        self.t(row, title, 11, MUTED, anchor="w", width=100).grid(row=0, column=0, sticky="w")
        val = self.t(row, "", 11, TEXT, True, anchor="e", width=80)
        s = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=steps, height=14, progress_color=GREEN,
                          button_color="#f1f2f2", button_hover_color=GREEN_HI, fg_color="#2a2c2e",
                          command=lambda v: val.configure(text=unit_fn(v)))
        s.set(value)
        val.configure(text=unit_fn(value))
        s.grid(row=0, column=1, sticky="ew", padx=8)
        val.grid(row=0, column=2, sticky="e")
        return s

    def toggle(self, parent, text, var):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=6)
        self.t(row, text, 12, TEXT, anchor="w").pack(side="left")
        ctk.CTkSwitch(row, text="", variable=var, onvalue=True, offvalue=False, width=44, height=22,
                      progress_color=GREEN, button_color="#f1f2f2", button_hover_color="white",
                      fg_color="#3a3d3f").pack(side="right")

    def bind_tree(self, w, seq, fn, skip=()):
        if w in skip:
            return
        w.bind(seq, fn, add="+")
        for c in w.winfo_children():
            self.bind_tree(c, seq, fn, skip)

    # ------------------------------------------------------------ Server
    def all_servers(self):
        return BUILTIN_SERVERS + self.st["servers"]

    def add_server(self, srv):
        self.st["servers"].append(srv)
        self.save_settings()
        self.render_picker()
        self.toast(f"Server „{srv['name']}“ hinzugefügt")

    def ask_delete_server(self, srv):
        ConfirmDialog(self, f"Server „{srv['name']}“ wirklich löschen?", "Löschen",
                      lambda: self.delete_server(srv))

    def delete_server(self, srv):
        self.st["servers"] = [s for s in self.st["servers"] if s["id"] != srv["id"]]
        shutil.rmtree(os.path.join(DATA_DIR, "servers", srv["id"]), ignore_errors=True)
        if self.st.get("last_server") == srv["id"]:
            self.st["last_server"] = "bruder"
        self.save_settings()
        self.render_picker()
        self.toast("Server gelöscht")

    def kind_text(self, s):
        return KIND_TEXT.get(s["kind"], "")

    # ------------------------------------------------------------ Server-Auswahl
    def build_picker(self):
        p = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        p.grid(row=0, column=0, sticky="nsew")
        self.picker = p
        p.grid_rowconfigure(0, weight=1)
        p.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(p, fg_color="transparent")
        top.place(relx=1, x=-20, y=16, anchor="ne")
        self.upd_btn_picker = self.button(top, "⬆  Update verfügbar", self.do_update, "green", height=34, width=170)
        self.upd_btns.append(self.upd_btn_picker)
        self.button(top, "⚙", self.open_settings, "ghost", width=38, height=34).pack(side="right")
        center = ctk.CTkFrame(p, fg_color="transparent")
        center.grid(row=0, column=0)
        try:
            im = Image.open(resource("assets", "logo.png"))
            self.logo_img = ctk.CTkImage(light_image=im, dark_image=im, size=(72, 72))
            ctk.CTkLabel(center, image=self.logo_img, text="").pack(pady=(0, 14))
        except Exception:
            pass
        self.t(center, "Wähle deinen Server", 30, TEXT, True).pack()
        self.t(center, "Pro Server nutzt Kill App die passende Kill-Erkennung.", 13, MUTED).pack(pady=(4, 26))
        self.grid_frame = ctk.CTkFrame(center, fg_color="transparent")
        self.grid_frame.pack()
        self.t(p, f"Version {APP_VERSION}", 10, DIM).place(relx=0.5, rely=1, y=-14, anchor="s")
        self.render_picker()

    def render_picker(self):
        """Baut die Server-Karten neu (nur noetig, wenn Server hinzugefuegt/geloescht wurden)."""
        self.tags = {}
        for w in self.grid_frame.winfo_children():
            w.destroy()
        items = self.all_servers()
        cols = 3
        for i, s in enumerate(items):
            self.server_card(self.grid_frame, s, i // cols, i % cols)
        i = len(items)
        self.add_card(self.grid_frame, i // cols, i % cols)

    def hoverable(self, card, normal, hover, border_n, border_h, skip=()):
        def enter(e):
            card.configure(fg_color=hover, border_color=border_h)

        def leave(e):
            x, y = self.winfo_pointerxy()
            cx, cy = card.winfo_rootx(), card.winfo_rooty()
            if cx <= x < cx + card.winfo_width() and cy <= y < cy + card.winfo_height():
                return
            card.configure(fg_color=normal, border_color=border_n)
        self.bind_tree(card, "<Enter>", enter, skip)
        self.bind_tree(card, "<Leave>", lambda e: self.after(15, leave, e), skip)

    def server_card(self, parent, s, r, c):
        card = ctk.CTkFrame(parent, width=300, height=150, corner_radius=18, fg_color=CARD, border_width=1,
                            border_color=BORDER)
        card.grid(row=r, column=c, padx=10, pady=10)
        card.grid_propagate(False)
        card.pack_propagate(False)
        self.t(card, s["name"][:1].upper(), 20, GREEN, True, fg_color=GREEN_DK, corner_radius=14, width=46,
               height=46).place(x=20, y=20)
        self.t(card, s["name"], 18, TEXT, True, anchor="w").place(x=78, y=20)
        self.t(card, self.kind_text(s), 11, MUTED, anchor="w", wraplength=200, justify="left").place(x=78, y=48)
        self.t(card, "Auswählen  ›", 12, GREEN, True).place(x=20, y=108)
        tag = self.t(card, "Zuletzt genutzt", 10, MUTED, fg_color="#1b1d1e", corner_radius=8, width=96, height=22)
        self.tags[s["id"]] = tag
        if s["id"] == self.st.get("last_server"):
            tag.place(relx=1, x=-16, y=-16, rely=1, anchor="se")
        dl = None
        if not s.get("builtin"):
            dl = self.button(card, "✕", lambda s=s: self.ask_delete_server(s), "flat", width=30, height=30,
                             corner_radius=15)
            dl.place(relx=1, x=-10, y=10, anchor="ne")
        self.hoverable(card, CARD, CARD_HI, BORDER, GREEN_BD, skip=(dl,) if dl else ())
        self.bind_tree(card, "<Button-1>", lambda e, s=s: self.enter_server(s), skip=(dl,) if dl else ())

    def add_card(self, parent, r, c):
        card = ctk.CTkFrame(parent, width=300, height=150, corner_radius=18, fg_color="#0e1110", border_width=1,
                            border_color=GREEN_BD)
        card.grid(row=r, column=c, padx=10, pady=10)
        card.grid_propagate(False)
        card.pack_propagate(False)
        self.t(card, "+", 30, GREEN, True).place(relx=0.5, rely=0.38, anchor="center")
        self.t(card, "Server hinzufügen", 14, TEXT, True).place(relx=0.5, rely=0.66, anchor="center")
        self.hoverable(card, "#0e1110", "#122019", GREEN_BD, GREEN)
        self.bind_tree(card, "<Button-1>", lambda e: AddServerDialog(self))

    def show_picker(self):
        if self.playing:
            self.toggle_play()
        self.page = "picker"
        self.picker.tkraise()

    def enter_server(self, s):
        self.server = s
        self.st["last_server"] = s["id"]
        self.srv_avatar.configure(text=s["name"][:1].upper())
        self.srv_name.configure(text=s["name"])
        self.srv_kind.configure(text=self.kind_text(s))
        self.page = "work"
        self.work.tkraise()
        for sid, tag in self.tags.items():
            if sid == s["id"]:
                tag.place(relx=1, x=-16, y=-16, rely=1, anchor="se")
            else:
                tag.place_forget()
        self.save_settings()
        if self.player:
            self.after(30, lambda: self.player.request_seek(self.pos))

    # ------------------------------------------------------------ Arbeitsbereich
    def build_work(self):
        w = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        w.grid(row=0, column=0, sticky="nsew")
        self.work = w
        w.grid_columnconfigure(0, weight=1)
        w.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(w, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        self.button(top, "‹  Server wechseln", self.show_picker, "dark", width=160, height=40).pack(side="left")
        srv = ctk.CTkFrame(top, fg_color="#101112", corner_radius=14, border_width=1, border_color=BORDER)
        srv.pack(side="left", padx=12)
        self.srv_avatar = self.t(srv, "B", 15, GREEN, True, fg_color=GREEN_DK, corner_radius=10, width=32, height=32)
        self.srv_avatar.pack(side="left", padx=(8, 10), pady=6)
        col = ctk.CTkFrame(srv, fg_color="transparent")
        col.pack(side="left", padx=(0, 16))
        self.srv_name = self.t(col, "", 13, TEXT, True, anchor="w")
        self.srv_name.pack(anchor="w")
        self.srv_kind = self.t(col, "", 10, DIM, anchor="w")
        self.srv_kind.pack(anchor="w")
        self.button(top, "⚙", self.open_settings, "ghost", width=40, height=40).pack(side="right")
        self.upd_btn_work = self.button(top, "⬆  Update verfügbar", self.do_update, "green", height=40, width=170)
        self.upd_btns.append(self.upd_btn_work)

        body = ctk.CTkFrame(w, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 16))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # --- links: Vorschau, Steuerung, Zeitleiste
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)
        self.vid_box = ctk.CTkFrame(left, fg_color="#050607", corner_radius=18, border_width=1, border_color=BORDER)
        self.vid_box.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        self.vid = tk.Label(self.vid_box, bg="#050607", bd=0)
        self.vid.place(relx=0.5, rely=0.5, anchor="center")
        self.cta = ctk.CTkFrame(self.vid_box, fg_color="transparent")
        self.cta.place(relx=0.5, rely=0.5, anchor="center")
        self.t(self.cta, "🎬", 44, MUTED).pack()
        self.t(self.cta, "Los geht’s", 22, TEXT, True).pack(pady=(8, 2))
        self.t(self.cta, "Wähle deine Aufnahme aus – danach suchst du die Kills.", 12, MUTED).pack(pady=(0, 16))
        self.button(self.cta, "📂  Video öffnen", self.open_video, "green", width=200, height=46).pack()
        self.time_chip = tk.Label(self.vid_box, text="00:00.00", bg="#000000", fg="white", padx=10, pady=4,
                                  font=("Consolas", 12, "bold"))
        self.kill_chip = tk.Label(self.vid_box, text="●  KILL", bg=RED, fg="white", padx=10, pady=4,
                                  font=(FONT, 11, "bold"))
        self._chip_text = ""

        ctl = ctk.CTkFrame(left, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        ctl.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        inner = ctk.CTkFrame(ctl, fg_color="transparent")
        inner.pack(pady=10, padx=14, fill="x")
        self.button(inner, "⏮", lambda: self.step_clip(-1), "ghost", width=44).pack(side="left", padx=2)
        self.button(inner, "−1 s", lambda: self.request_seek(self.pos - 1), "ghost", width=54).pack(side="left", padx=2)
        self.play_btn = self.button(inner, "▶", self.toggle_play, "green", width=64, height=44, corner_radius=22)
        self.play_btn.pack(side="left", padx=8)
        self.button(inner, "+1 s", lambda: self.request_seek(self.pos + 1), "ghost", width=54).pack(side="left", padx=2)
        self.button(inner, "⏭", lambda: self.step_clip(1), "ghost", width=44).pack(side="left", padx=2)
        self.speed_seg = self.seg(inner, ["0.5×", "1×", "2×"], width=150, command=self.set_speed)
        self.speed_seg.set("1×")
        self.speed_seg.pack(side="right")

        tl = ctk.CTkFrame(left, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        tl.grid(row=2, column=0, sticky="ew")
        self.timeline = Timeline(tl, self)
        self.timeline.pack(fill="x", padx=8, pady=(8, 0))
        ed = ctk.CTkFrame(tl, fg_color="transparent")
        ed.pack(fill="x", padx=12, pady=10)
        self.button(ed, "[I]  Start hier", lambda: self.set_edge(0), "ghost", width=120).pack(side="left", padx=(0, 6))
        self.button(ed, "[O]  Ende hier", lambda: self.set_edge(1), "ghost", width=120).pack(side="left", padx=6)
        self.button(ed, "＋  Clip hier", self.add_clip, "ghost", width=120).pack(side="left", padx=6)
        self.t(ed, "Scrollen = Zoom · Ränder ziehen = Clip anpassen", 11, DIM).pack(side="right")

        # --- rechts: die vier Schritte
        rp = ctk.CTkFrame(body, width=390, fg_color="transparent")
        rp.grid(row=0, column=1, sticky="ns", padx=(14, 0))
        rp.grid_propagate(False)
        rp.grid_columnconfigure(0, weight=1)
        rp.grid_rowconfigure(2, weight=1)

        a, _ = self.section(rp, 0, 1, "Video")
        self.file_lbl = self.t(a, "Noch kein Video geladen", 12, TEXT, True, anchor="w", wraplength=350,
                               justify="left")
        self.file_lbl.pack(fill="x", padx=16)
        self.meta_lbl = self.t(a, "—", 11, MUTED, anchor="w")
        self.meta_lbl.pack(fill="x", padx=16, pady=(0, 8))
        self.open_btn = self.button(a, "📂  Video öffnen", self.open_video, "green")
        self.open_btn.pack(fill="x", padx=16, pady=(0, 14))

        b, _ = self.section(rp, 1, 2, "Kills suchen")
        self.s_pre = self.slider_row(b, "Vor dem Kill", 0, 10, 20, self.st["pre"], lambda v: f"{v:.1f} s")
        self.s_post = self.slider_row(b, "Nach dem Kill", 0, 10, 20, self.st["post"], lambda v: f"{v:.1f} s")
        self.s_sens = self.slider_row(b, "Empfindlichkeit", 1, 10, 9, self.st["sens"],
                                      lambda v: ["", "sehr niedrig", "niedrig", "niedrig", "mittel", "mittel",
                                                 "mittel+", "hoch", "hoch", "sehr hoch", "max"][int(v)])
        self.scan_btn = self.button(b, "🎯  Kills suchen", self.start_scan, "green", height=42)
        self.scan_btn.pack(fill="x", padx=16, pady=(10, 8))
        self.scan_bar = ctk.CTkProgressBar(b, height=5, fg_color="#2a2c2e", progress_color=GREEN)
        self.scan_bar.set(0)
        self.scan_bar.pack(fill="x", padx=16)
        sl = ctk.CTkFrame(b, fg_color="transparent")
        sl.pack(fill="x", padx=16, pady=(6, 12))
        self.dot = self.t(sl, "●", 11, DIM)
        self.dot.pack(side="left")
        self.status = self.t(sl, "Bereit", 11, MUTED, anchor="w", wraplength=320, justify="left")
        self.status.pack(side="left", padx=6)

        c, head = self.section(rp, 2, 3, "Clips")
        self.count_lbl = ctk.CTkLabel(head, text="0", fg_color=GREEN_DK, text_color=GREEN, corner_radius=12, width=34,
                                      height=24, font=ctk.CTkFont(FONT, 12, "bold"))
        self.count_lbl.pack(side="left")
        self.total_lbl = self.t(head, "", 11, DIM)
        self.total_lbl.pack(side="right")
        self.list = ctk.CTkScrollableFrame(c, fg_color="transparent", scrollbar_button_color="#26292a",
                                           scrollbar_button_hover_color=GREEN)
        self.list.pack(fill="both", expand=True, padx=(6, 2), pady=(0, 8))

        d, _ = self.section(rp, 3, 4, "Export")
        self.seg_ed = self.seg(d, list(MODES), variable=self.mode_var)
        self.seg_ed.pack(fill="x", padx=16)
        self.mode_desc = self.t(d, "", 10, DIM, anchor="w", wraplength=350, justify="left")
        self.mode_desc.pack(fill="x", padx=16, pady=(4, 6))
        self.mode_var.trace_add("write", lambda *a: self.mode_changed())
        self.mode_changed()
        row = ctk.CTkFrame(d, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        self.exp_all = self.button(row, "Alle exportieren", lambda: self.export(False), "green", height=40)
        self.exp_all.pack(side="left", fill="x", expand=True)
        self.button(row, "Auswahl", lambda: self.export(True), "dark", height=40, width=90).pack(side="left", padx=(8, 0))
        self.exp_bar = ctk.CTkProgressBar(d, height=5, progress_color=GREEN, fg_color="#2a2c2e")
        self.exp_bar.set(0)
        self.exp_bar.pack(fill="x", padx=16, pady=(0, 14))
        self.rebuild_cards()

    # ------------------------------------------------------------ Einstellungen
    def open_settings(self):
        top = ctk.CTkToplevel(self, fg_color=PANEL)
        top.title("Einstellungen")
        top.geometry("460x640")
        top.transient(self)
        self.t(top, "Einstellungen", 22, TEXT, True).pack(anchor="w", padx=24, pady=(22, 8))
        self.t(top, "ALLGEMEIN", 10, DIM, True).pack(anchor="w", padx=24, pady=(8, 0))
        self.toggle(top, "Clip in der Vorschau wiederholen", self.var_loop)
        self.toggle(top, "Ordner nach dem Export öffnen", self.var_open)
        self.toggle(top, "Beim Start nach Updates suchen", self.var_autoupd)
        self.t(top, "KILL-SUCHE", 10, DIM, True).pack(anchor="w", padx=24, pady=(14, 0))
        self.toggle(top, "GPU-Beschleunigung nutzen", self.var_gpu)
        self.t(top, "UPDATES", 10, DIM, True).pack(anchor="w", padx=24, pady=(14, 4))
        row = ctk.CTkFrame(top, fg_color="transparent")
        row.pack(fill="x", padx=24)
        self.t(row, f"Version {APP_VERSION}", 12, TEXT).pack(side="left")
        self.button(row, "Nach Updates suchen", lambda: self.start_update_check(True), "dark", width=170).pack(
            side="right")
        self.t(top, "TASTENKÜRZEL", 10, DIM, True).pack(anchor="w", padx=24, pady=(18, 4))
        for k, v in (("Leertaste", "Play / Pause"), ("← →", "±1 Sekunde (Shift: 1 Bild)"),
                     ("↑ ↓", "Vorheriger / nächster Clip"), ("I / O", "Start / Ende setzen"), ("Entf", "Clip löschen")):
            r = ctk.CTkFrame(top, fg_color="transparent")
            r.pack(fill="x", padx=24, pady=2)
            self.t(r, k, 12, GREEN, True, width=100, anchor="w").pack(side="left")
            self.t(r, v, 12, TEXT).pack(side="left")

    # ------------------------------------------------------------ Updates
    def start_update_check(self, manual):
        def work():
            try:
                res = check_update()
            except Exception:
                if manual:
                    self.ui(self.toast, "Update-Suche fehlgeschlagen (Internet?)", RED)
                return
            if res:
                self.ui(self.show_update, *res)
            elif manual:
                self.ui(self.toast, f"Du hast die neueste Version (v{APP_VERSION})")
        threading.Thread(target=work, daemon=True).start()

    def show_update(self, version, url):
        self.update_url = url
        for b in self.upd_btns:
            b.configure(text=f"⬆  Update v{version} installieren")
        self.upd_btn_picker.pack(side="right", padx=(0, 8))
        self.upd_btn_work.pack(side="right", padx=(0, 8))
        self.toast(f"🚀  Neue Version v{version} verfügbar")

    def do_update(self):
        for b in self.upd_btns:
            b.configure(state="disabled", text="Lade herunter …")

        def work():
            try:
                path = download_file(self.update_url, lambda p: self.ui(
                    lambda: [b.configure(text=f"Lade … {int(p * 100)} %") for b in self.upd_btns]))
                self.ui(self.run_installer, path)
            except Exception as e:
                self.ui(lambda: [b.configure(state="normal", text="Erneut versuchen") for b in self.upd_btns])
                self.ui(self.toast, f"Update fehlgeschlagen: {e}", RED)
        threading.Thread(target=work, daemon=True).start()

    def run_installer(self, path):
        subprocess.Popen([path, "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS"], creationflags=NOWIN)
        self.on_close()

    # ------------------------------------------------------------ Anzeige-Helfer
    def mode_changed(self):
        self.mode_desc.configure(text=MODES[self.mode_var.get()][1])

    def set_status(self, text, state="idle"):
        self.status.configure(text=text)
        self.dot.configure(text_color={"ok": GREEN, "bad": RED, "warn": AMBER, "idle": DIM, "busy": BLUE}[state])

    # ------------------------------------------------------------ Video
    def open_video(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.flv *.ts *.avi"), ("Alle", "*.*")])
        if p:
            self.load_video(p)

    def load_video(self, p):
        pl = Player(p)
        if not pl.ok:
            self.toast("Video konnte nicht geöffnet werden", RED)
            return
        self.playing = False
        self.play_btn.configure(text="▶")
        if self.player:
            self.player.stop()
        self.player, self.path = pl, p
        self.fps, self.duration = pl.fps, pl.duration
        pl.size = self.video_size()
        pl.start()
        self.file_lbl.configure(text=os.path.basename(p))
        self.meta_lbl.configure(text=f"{pl.w}×{pl.h}  ·  {pl.fps:.0f} fps  ·  {fmt_short(pl.duration)}")
        self.open_btn.configure(text="📂  Anderes Video öffnen", fg_color="#1b1d1e", hover_color="#26292a",
                                text_color=TEXT, border_width=1, border_color="#2a2d2f")
        self.cta.place_forget()
        self.time_chip.place(x=16, y=16)
        self.clips, self.sel = [], None
        self.rebuild_cards()
        self.scan_bar.set(0)
        self.set_status("Bereit – jetzt „Kills suchen“", "idle")
        self.timeline.set_view(0, 60)
        self.seek(0)

    def video_size(self):
        return max(64, self.vid_box.winfo_width() - 16), max(64, self.vid_box.winfo_height() - 16)

    def poll_player(self):
        """Holt fertige Bilder vom Decoder-Thread und zeigt sie an (laeuft sehr leicht)."""
        pl = self.player
        if pl and self.page == "work":
            self._tick += 1
            if self._tick % 40 == 0:
                sz = self.video_size()
                if sz != pl.size:
                    pl.size = sz
                    if not self.playing:
                        pl.request_seek(self.pos)
            fr = pl.take()
            if fr:
                self.draw_frame(*fr)
            if pl.ended and self.playing:
                self.playing = False
                self.play_btn.configure(text="▶")
        if self.winfo_exists():
            self.after(10, self.poll_player)

    def draw_frame(self, kind, pos, rgb):
        self.last_rgb = rgb
        self._img = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.vid.configure(image=self._img)
        if kind == "play":
            self.pos = pos
            c = self.play_clip
            if c and pos >= c.end:
                if self.var_loop.get():
                    self.player.request_seek(c.start)
                else:
                    self.toggle_play()
        txt = fmt(self.pos)
        if txt != self._chip_text:
            self._chip_text = txt
            self.time_chip.configure(text=txt)
        if any(abs(self.pos - c.kill) < 0.4 for c in self.clips):
            self.kill_chip.place(x=self.vid_box.winfo_width() - 100, y=16)
        else:
            self.kill_chip.place_forget()
        now = time.perf_counter()
        if now - self._last_tl > 0.07:                      # Zeitleiste hoechstens ~14x pro Sekunde
            self._last_tl = now
            if self.playing and self.pos > self.timeline.v1:
                self.timeline.set_view(self.pos - 2, self.pos - 2 + (self.timeline.v1 - self.timeline.v0))
            else:
                self.timeline.redraw()

    def seek(self, t):
        if not self.player:
            return
        t = min(max(0.0, t), max(0.0, self.duration - 0.1))
        self.pos = t
        self.player.request_seek(t)
        self.timeline.redraw()

    def request_seek(self, t):
        self.seek(t)

    def set_speed(self, v):
        self.speed = float(v.replace("×", ""))
        if self.player:
            self.player.set_play(self.playing, self.speed)

    def toggle_play(self):
        if not self.player:
            return
        self.playing = not self.playing
        self.play_btn.configure(text="❚❚" if self.playing else "▶")
        if self.playing:
            self.play_clip = next((c for c in self.clips if c.start <= self.pos <= c.end), None)
            if self.play_clip and self.pos >= self.play_clip.end - 0.05:
                self.seek(self.play_clip.start)
        self.player.set_play(self.playing, self.speed)

    # ------------------------------------------------------------ Scan
    def start_scan(self):
        if self.scanning:
            self.cancel.set()
            return
        if not self.path:
            self.toast("Bitte zuerst ein Video öffnen", RED)
            return
        if self.playing:
            self.toggle_play()
        self.scanning = True
        self.cancel.clear()
        self.scan_btn.configure(text="✕  Abbrechen", fg_color=RED, hover_color="#ff6b6b", text_color="white")
        self.scan_bar.set(0)
        self.set_status("Suche läuft …", "busy")
        args = (self.path, self.s_pre.get(), self.s_post.get(), self.server, int(self.s_sens.get()))
        gpu = self.var_gpu.get()

        def work():
            try:
                res = scan_video(*args, gpu=gpu,
                                 progress=lambda p: self.ui(self.scan_bar.set, p),
                                 on_kill=lambda n: self.ui(self.set_status, f"Suche läuft …  {n} "
                                                           f"{'Kill' if n == 1 else 'Kills'}", "busy"),
                                 cancel=self.cancel)
                self.ui(self.scan_done, *res)
            except Exception as e:
                self.ui(self.scan_failed, str(e) or repr(e))
        threading.Thread(target=work, daemon=True).start()

    def scan_reset(self):
        self.scanning = False
        self.scan_btn.configure(text="🎯  Kills suchen", fg_color=GREEN, hover_color=GREEN_HI, text_color="#04120c")

    def scan_failed(self, msg):
        self.scan_reset()
        self.set_status("Fehler: " + msg[:120], "bad")
        self.toast("Suche fehlgeschlagen", RED)

    def scan_done(self, res, info):
        cancelled = self.cancel.is_set()
        self.scan_reset()
        self.scan_bar.set(1)
        self.clips = [Clip(a, b, k, Image.fromarray(cv2.cvtColor(th, cv2.COLOR_BGR2RGB)) if th is not None else None)
                      for a, b, k, th in res]
        self.sel = None
        self.rebuild_cards()
        n = len(self.clips)
        word = "Kill" if n == 1 else "Kills"
        extra = f" · {info['notices']} Meldungen geprüft" if self.server["kind"] != "xhit" else ""
        self.set_status(("Abgebrochen · " if cancelled else "Fertig · ") + f"{n} {word}{extra}", "ok" if n else "warn")
        self.toast(f"🎯  {n} {word} gefunden" if n else "Keine Kills gefunden – Empfindlichkeit erhöhen?",
                   GREEN if n else AMBER)
        if n:
            self.select(0)

    # ------------------------------------------------------------ Clips
    def rebuild_cards(self):
        for w in self.list.winfo_children():
            w.destroy()
        if not self.clips:
            self.t(self.list, "Noch keine Clips.", 12, DIM, justify="center").pack(pady=24)
        for i, c in enumerate(self.clips):
            self.make_card(i, c)
        self.count_lbl.configure(text=str(len(self.clips)))
        total = sum(c.end - c.start for c in self.clips)
        self.total_lbl.configure(text=f"Gesamt {fmt_short(total)}" if self.clips else "")
        self.exp_all.configure(text=f"Alle exportieren ({len(self.clips)})" if self.clips else "Alle exportieren")
        self.timeline.redraw()

    def make_card(self, i, c):
        card = ctk.CTkFrame(self.list, fg_color="#141516", corner_radius=12, border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=4, padx=4)
        c.card = card
        th = None
        if c.thumb:
            c.img = ctk.CTkImage(light_image=c.thumb, dark_image=c.thumb, size=(96, 54))
            th = ctk.CTkLabel(card, image=c.img, text="", corner_radius=8)
            th.pack(side="left", padx=8, pady=8)
        col = ctk.CTkFrame(card, fg_color="transparent")
        col.pack(side="left", fill="both", expand=True, pady=8)
        c.title = self.t(col, "", 13, TEXT, True, anchor="w")
        c.title.pack(fill="x")
        c.sub = self.t(col, "", 11, MUTED, anchor="w")
        c.sub.pack(fill="x")
        dl = self.button(card, "✕", lambda i=i: self.delete(i), "flat", width=28, height=28, corner_radius=14)
        dl.pack(side="right", padx=6)
        self.update_card(i)
        for w in [card, col, c.title, c.sub] + ([th] if th else []):
            w.bind("<Button-1>", lambda e, i=i: self.select(i))

    def update_card(self, i):
        c = self.clips[i]
        c.title.configure(text=f"KILL {i + 1:02d}")
        c.sub.configure(text=f"{fmt_short(c.start)} – {fmt_short(c.end)}  ·  {c.end - c.start:.1f} s")

    def clip_changed(self, i):
        self.update_card(i)
        self.timeline.redraw()

    def select(self, i, seek=True, refocus=True):
        if not (0 <= i < len(self.clips)):
            return
        self.sel = i
        for j, c in enumerate(self.clips):
            if c.card:
                c.card.configure(border_color=GREEN if j == i else BORDER, fg_color=GREEN_DK if j == i else "#141516")
        c = self.clips[i]
        self.timeline.focus_on(c.start, c.end)
        if seek:
            if self.playing:
                self.toggle_play()
            self.seek(c.start)
        self.timeline.redraw()

    def step_clip(self, d):
        if self.clips:
            self.select(((self.sel if self.sel is not None else (-1 if d > 0 else 0)) + d) % len(self.clips))

    def delete(self, i=None):
        i = self.sel if i is None else i
        if i is None or not (0 <= i < len(self.clips)):
            return
        del self.clips[i]
        self.sel = None
        self.rebuild_cards()
        if self.clips:
            self.select(min(i, len(self.clips) - 1), seek=False)

    def add_clip(self):
        if not self.player:
            return
        c = Clip(max(0, self.pos - self.s_pre.get()), min(self.duration, self.pos + self.s_post.get()), self.pos)
        if self.last_rgb is not None:
            f = self.last_rgb
            c.thumb = Image.fromarray(cv2.resize(f, (256, int(256 * f.shape[0] / f.shape[1])),
                                                 interpolation=cv2.INTER_AREA))
        self.clips.append(c)
        self.clips.sort(key=lambda x: x.start)
        idx = self.clips.index(c)
        self.rebuild_cards()
        self.select(idx, seek=False)
        self.toast("Clip hinzugefügt")

    def set_edge(self, which):
        if self.sel is None:
            self.toast("Wähle zuerst einen Clip aus", RED)
            return
        c = self.clips[self.sel]
        if which == 0:
            c.start = min(self.pos, c.end - 0.5)
        else:
            c.end = max(self.pos, c.start + 0.5)
        self.clip_changed(self.sel)

    # ------------------------------------------------------------ Export
    def export(self, only_selected):
        if not self.clips:
            self.toast("Keine Clips zum Exportieren", RED)
            return
        if not self.ffmpeg:
            self.toast("FFmpeg nicht gefunden", RED)
            return
        if only_selected:
            if self.sel is None:
                self.toast("Wähle zuerst einen Clip aus", RED)
                return
            idx = [self.sel]
        else:
            idx = list(range(len(self.clips)))
        from tkinter import filedialog
        out = filedialog.askdirectory(title="Zielordner wählen", initialdir=self.st.get("outdir") or None)
        if not out:
            return
        self.st["outdir"] = out
        mode = MODES[self.mode_var.get()][0]
        base, ext = os.path.splitext(os.path.basename(self.path))
        jobs = [(self.clips[i].start, self.clips[i].end, os.path.join(out, f"{base}_kill{i + 1:02d}{ext}"))
                for i in idx]
        self.exp_bar.set(0)
        self.exp_all.configure(state="disabled")

        def work():
            try:
                for n, (a, b, dst) in enumerate(jobs, 1):
                    self.ui(self.set_status, f"Export {n}/{len(jobs)} …", "busy")
                    export_clip(self.ffmpeg, self.path, a, b, dst, mode)
                    self.ui(self.exp_bar.set, n / len(jobs))
                self.ui(self.export_done, out, len(jobs))
            except Exception as e:
                self.ui(self.export_failed, str(e))
        threading.Thread(target=work, daemon=True).start()

    def export_failed(self, msg):
        self.exp_all.configure(state="normal")
        self.set_status("Export fehlgeschlagen", "bad")
        self.toast("Export-Fehler: " + msg[:90], RED)

    def export_done(self, out, n):
        self.exp_all.configure(state="normal")
        self.set_status(f"{n} Clips exportiert", "ok")
        self.toast(f"✅  {n} Clips exportiert")
        if self.var_open.get():
            try:
                if sys.platform.startswith("win"):
                    os.startfile(out)
                else:
                    subprocess.Popen(["xdg-open", out])
            except Exception:
                pass

    # ------------------------------------------------------------ Tasten
    def bind_keys(self):
        def ed(fn):
            return lambda e: fn() if self.page == "work" else None
        self.bind("<space>", ed(self.toggle_play))
        self.bind("<Left>", ed(lambda: self.request_seek(self.pos - 1)))
        self.bind("<Right>", ed(lambda: self.request_seek(self.pos + 1)))
        self.bind("<Shift-Left>", ed(lambda: self.request_seek(self.pos - 1 / self.fps)))
        self.bind("<Shift-Right>", ed(lambda: self.request_seek(self.pos + 1 / self.fps)))
        self.bind("<Up>", ed(lambda: self.step_clip(-1)))
        self.bind("<Down>", ed(lambda: self.step_clip(1)))
        self.bind("i", ed(lambda: self.set_edge(0)))
        self.bind("o", ed(lambda: self.set_edge(1)))
        self.bind("<Delete>", ed(self.delete))


if __name__ == "__main__":
    App().mainloop()

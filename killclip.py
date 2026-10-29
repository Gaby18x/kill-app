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

# Server-Profile. kind "notify": Kill-Benachrichtigung des Servers (lila "Information"-Box),
# kind "xhit": nur roter Hitmarker (X) in der Bildmitte.
PROFILES = {
    "Bruder (Kill-Notify)": {"kind": "notify", "template": ("profiles", "bruder_notify.png"),
                             "header": (10, 32, 55, 145),
                             # Woerter im Beispielbild (y0, y1, x0, x1): Kill-Text = "Du hast ... getoetet"
                             "words": {"duhast": (31, 46, 58, 106), "getotet": (58, 74, 78, 124)}},
    "Standard (roter X)": {"kind": "xhit"},
}

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


def verify_text(frame, anchor_xy, ks, words, thr):
    """Prueft im Originalbild, ob in der Box 'Du hast' und 'getoetet' stehen. Gibt (ok, scores) zurueck."""
    ax, ay = anchor_xy
    H, W = frame.shape[:2]
    y0, y1 = int(max(0, ay + TEXT_REGION[0] * ks)), int(min(H, ay + TEXT_REGION[1] * ks))
    x0, x1 = int(max(0, ax + TEXT_REGION[2] * ks)), int(min(W, ax + TEXT_REGION[3] * ks))
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


def scan_video(path, pre, post, profile_name="Bruder (Kill-Notify)", sens=6, progress=None, cancel=None,
               on_kill=None, gpu=True):
    """Gibt (clips, info) zurueck. clips: Liste von (start, ende, kill_zeit, thumbnail_bgr)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg nicht gefunden.")
    prof = PROFILES[profile_name]
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("Video konnte nicht geoeffnet werden.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = total / fps

    hits = []                                   # Zeitpunkte (und beim Notify: Lage der Box)
    if prof["kind"] == "notify":
        ofps = 5
        ow = 960
        oh = int(round(ow * h / w / 2)) * 2
        vf = f"fps={ofps},scale={ow}:{oh}:flags=area"
        k = 0.5 * (16 / 9) * h / w            # Skalierung der Vorlage (1080p-Referenz -> aktuelles Bild)
        hdr, words = load_notify_assets(prof)
        tpl = cv2.resize(hdr, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        need = max(20, int(tpl.sum() * 0.35))
        pad = max(1, int(8 * k))               # nicht-lila Rand: lila Flaechen sind keine Ueberschrift
        tpl = cv2.copyMakeBorder(tpl, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
        thr = min(0.85, max(0.5, 0.95 - 0.05 * sens))
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
        if prof["kind"] == "notify":
            m = purple_mask(fr)
            hit = False
            if m.sum() >= need:
                res = cv2.matchTemplate(m, tpl, cv2.TM_CCOEFF_NORMED)
                _, mx, _, ml = cv2.minMaxLoc(res)
                if mx >= thr:
                    hit, loc = True, ((ml[0] + pad) * w / ow, (ml[1] + pad) * w / ow)
        else:
            hit = is_hitmarker(fr, diff)
        run = run + 1 if hit else 0
        if run >= min_run:
            hits.append(((i - run + 1) / ofps, i / ofps, loc))     # (Beginn des Treffers, jetzt, Lage)
        if progress and i % 25 == 0:
            progress(min(1.0, i / max(1, duration * ofps)))

    # Treffer zu Ereignissen zusammenfassen
    groups = []
    for t0, t, loc in hits:
        if groups and t - groups[-1]["last"] <= gap:
            g = groups[-1]
            g["last"] = t
            g["locs"].append((t, loc))
        else:
            groups.append({"start": t0, "last": t, "locs": [(t, loc)]})
    info = {"notices": len(groups)}

    if prof["kind"] == "notify":
        # Nur Meldungen, die wirklich "Du hast ... getoetet" lauten, sind Kills
        ks = h / 1080.0
        good = []
        for g in groups:
            ok_any = False
            cand = [g["start"] + d for d in (0.4, 0.9, 1.5, 2.2) if g["start"] + d <= g["last"] + 0.2]
            for t in cand or [(g["start"] + g["last"]) / 2]:
                near = min(g["locs"], key=lambda x: abs(x[0] - t))[1]
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(min(t, duration - 0.1) * fps))
                ok, fr = cap.read()
                if ok and verify_text(fr, near, ks, words, text_thr)[0]:
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
import math
import queue
import threading
import time
import tkinter as tk

import customtkinter as ctk

# "Noir"-Farbwelt
BG, PANEL, CARD, CARD_HI, BORDER = "#08090a", "#0d0e0f", "#131415", "#1a1c1d", "#232527"
TEXT, MUTED, DIM = "#f3f4f4", "#8d9195", "#5c6063"
GREEN, GREEN_HI, GREEN_DK, GREEN_BD = "#1fcb8f", "#35e3a6", "#0f2a20", "#1d6b4f"
BLUE, PURPLE, RED, AMBER = "#6cc6f2", "#a78bfa", "#ef4a4a", "#f5a524"
GLOW = "#2f9c78"
ACC, ACC_HI, CYAN = GREEN, GREEN_HI, BLUE
FONT = "Segoe UI"
SETTINGS = os.path.join(os.path.expanduser("~"), ".killapp.json")

MODES = {
    "Verlustfrei": ("copy", "1:1 Kopie ohne Neu-Kodieren. Blitzschnell, Schnitt am Keyframe."),
    "Bildgenau": ("exact", "Neu kodiert (CRF 12). Bildgenau, praktisch ohne Verlust."),
    "Klein": ("h265", "H.265 (CRF 18). Deutlich kleiner, optisch gleich."),
}


def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def fmt_short(t):
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def mix(a, b, t):
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(int(ra[i] + (rb[i] - ra[i]) * t) for i in range(3))


class Clip:
    def __init__(self, start, end, kill=None, thumb=None):
        self.start, self.end = start, end
        self.kill = start if kill is None else kill
        self.thumb = thumb          # PIL.Image
        self.card = None
        self.title = self.sub = self.img = None


# ----------------------------------------------------- Punkt-Grafiken (Kartenhintergruende)
def _points(kind):
    if kind == "sheet":
        u, v = np.meshgrid(np.linspace(-1.3, 1.3, 58), np.linspace(-1, 1, 34))
        return np.stack([u.ravel(), np.zeros(u.size), v.ravel()], 1)
    if kind == "torus":
        th, ph = np.meshgrid(np.linspace(0, 2 * np.pi, 64, endpoint=False),
                             np.linspace(0, 2 * np.pi, 22, endpoint=False))
        R, r = 1.0, 0.42
        return np.stack([((R + r * np.cos(ph)) * np.cos(th)).ravel(), (r * np.sin(ph)).ravel(),
                         ((R + r * np.cos(ph)) * np.sin(th)).ravel()], 1)
    t, s = np.meshgrid(np.linspace(0, 2 * np.pi, 170, endpoint=False), np.linspace(-1, 1, 9))
    tw = t * 1.5
    return np.stack([(np.cos(t) + 0.3 * s * np.cos(tw)).ravel(), (0.6 * np.sin(2 * t) + 0.3 * s * np.sin(tw)).ravel(),
                     np.sin(t).ravel()], 1)


def render_art(kind, w, h, bg, tint, frames=36):
    """Rendert eine sanft schwingende Punktwolke als Bildfolge (Schleife)."""
    P = _points(kind)
    xs = np.linspace(0, 1, w)[None, :]
    ys = np.linspace(0, 1, h)[:, None]
    fade = np.clip(xs * 2.4, 0, 1) * np.clip(ys * 3.0 + 0.3, 0, 1)
    bgc, tc = np.array(_rgb(bg), np.float32), np.array(_rgb(tint), np.float32)
    out = []
    for f in range(frames):
        ph = 2 * np.pi * f / frames
        pts = P.copy()
        if kind == "sheet":
            pts[:, 1] = 0.34 * np.sin(2.6 * pts[:, 0] + ph) * np.cos(1.8 * pts[:, 2] + ph)
        ang = 0.55 * np.sin(ph)
        ca, sa = np.cos(ang), np.sin(ang)
        x = pts[:, 0] * ca + pts[:, 2] * sa
        z = -pts[:, 0] * sa + pts[:, 2] * ca
        y = pts[:, 1]
        tl = 0.55 + (0.45 * np.sin(ph) if kind == "torus" else 0.0)
        y2, z2 = y * np.cos(tl) - z * np.sin(tl), y * np.sin(tl) + z * np.cos(tl)
        sc = 3.2 / (3.2 + z2)
        S = min(w, h) * 0.40
        ix = np.round(w * 0.56 + x * sc * S * 1.1).astype(int)
        iy = np.round(h * 0.52 - y2 * sc * S).astype(int)
        b = np.clip(0.30 + 0.70 * (1 - (z2 + 1.6) / 3.2), 0.15, 1).astype(np.float32)
        img = np.zeros((h, w), np.float32)
        for dx, dy, k in ((0, 0, 1.0), (1, 0, .55), (0, 1, .55), (1, 1, .35)):
            jx, jy = ix + dx, iy + dy
            m = (jx >= 0) & (jx < w) & (jy >= 0) & (jy < h)
            np.add.at(img, (jy[m], jx[m]), b[m] * k)
        img = np.clip(img, 0, 1) * fade
        out.append(Image.fromarray((bgc + (tc - bgc) * img[..., None]).astype(np.uint8)))
    return out


# ------------------------------------------------------------------ Widgets
class Gauge(tk.Canvas):
    """Kreisanzeige mit animiertem Bogen."""

    def __init__(self, master, size=112, bg=CARD, color=BLUE):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self.size, self.color = size, color
        self.value = self.target = 0.0
        self.text, self.sub = "0%", ""
        self.draw()

    def set(self, v, text=None, sub=None):
        self.target = min(1.0, max(0.0, v))
        if text is not None:
            self.text = text
        if sub is not None:
            self.sub = sub
        self._step()

    def _step(self):
        d = self.target - self.value
        self.value = self.target if abs(d) < 0.004 else self.value + d * 0.18
        self.draw()
        if self.value != self.target:
            self.after(16, self._step)

    def draw(self):
        self.delete("all")
        s, p = self.size, 9
        self.create_oval(p, p, s - p, s - p, outline="#1f2528", width=8)
        if self.value > 0.002:
            self.create_arc(p, p, s - p, s - p, start=90, extent=-max(1.0, self.value * 359.9),
                            style="arc", outline=self.color, width=8)
        self.create_text(s / 2, s / 2 - (7 if self.sub else 0), text=self.text, fill=TEXT, font=(FONT, 15, "bold"))
        if self.sub:
            self.create_text(s / 2, s / 2 + 13, text=self.sub, fill=MUTED, font=(FONT, 8))


class AreaChart(tk.Canvas):
    """Lila Flaechendiagramm mit Einblend-Animation."""

    def __init__(self, master, bg=CARD):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0)
        self.data, self.labels, self.reveal = [], ("", ""), 1.0
        self.bind("<Configure>", lambda e: self.redraw())

    def set_data(self, vals, labels=("", "")):
        self.data, self.labels, self.reveal = list(vals), labels, 0.0
        self._anim()

    def _anim(self):
        self.reveal = min(1.0, self.reveal + 0.05)
        self.redraw()
        if self.reveal < 1.0:
            self.after(16, self._anim)

    def redraw(self):
        self.delete("all")
        w, h = max(50, self.winfo_width()), max(50, self.winfo_height())
        L, R, T, B = 6, 6, 10, 20
        for i in range(4):
            y = T + i * (h - T - B) / 3
            self.create_line(L, y, w - R, y, fill="#1a1c1e")
        mx = max(self.data) if self.data else 0
        if mx <= 0:
            self.create_text(w / 2, h / 2 - 8, text="Noch keine Daten – starte die Kill-Suche", fill=DIM,
                             font=(FONT, 10))
            return
        e = 1 - (1 - self.reveal) ** 3
        n = len(self.data)
        base = h - B
        pts = []
        for i, v in enumerate(self.data):
            pts += [L + i / (n - 1) * (w - L - R), base - v / (mx * 1.15) * (base - T) * e]
        self.create_polygon([L, base] + pts + [w - R, base], fill="#241c3f", outline="", smooth=True)
        self.create_line(pts, fill=PURPLE, width=2, smooth=True)
        self.create_text(L, h - 8, text=self.labels[0], fill=DIM, font=(FONT, 8), anchor="w")
        self.create_text(w - R, h - 8, text=self.labels[1], fill=DIM, font=(FONT, 8), anchor="e")


class Card(ctk.CTkFrame):
    """Dunkle Karte; der Rand leuchtet auf, wenn die Maus darueber ist (gesteuert von App.hover_tick)."""

    def __init__(self, master, **kw):
        kw.setdefault("fg_color", CARD)
        kw.setdefault("corner_radius", 18)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", BORDER)
        super().__init__(master, **kw)
        self.glow = self.glow_target = 0.0

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
        if not app.cap:
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
        if not a.cap:
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




class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        super().__init__(fg_color=BG)
        self.title("Kill App")
        # Kleine Bildschirme: Oberflaeche automatisch verkleinern
        sc = min(1.0, max(0.7, (self.winfo_screenheight() - 60) / 920, 0.0))
        sc = min(sc, max(0.7, (self.winfo_screenwidth() - 20) / 1420))
        if sc < 0.98:
            ctk.set_widget_scaling(sc)
            ctk.set_window_scaling(sc)
        self.geometry("1500x920")
        self.minsize(1400, 900)
        if sys.platform.startswith("win"):
            self.after(60, lambda: self.state("zoomed"))
        self.ffmpeg = find_ffmpeg()
        self.st = self.load_settings()
        self.path = None
        self.cap = None
        self.fps, self.duration, self.pos = 60.0, 0.0, 0.0
        self.clips, self.sel = [], None
        self.cancel = threading.Event()
        self.scanning = False
        self.playing = False
        self.play_clip = None
        self.speed = 1.0
        self._seek_to = None
        self._frame_n = 0
        self._img = None
        self._count_shown = 0
        self.q = queue.Queue()
        self.page = "home"
        self.cards, self.arts, self.art_specs = [], [], []
        self.update_url = None
        self.num_anim = {}
        self.mode_var = tk.StringVar(value=self.st["mode"] if self.st["mode"] in MODES else "Verlustfrei")
        self.var_loop = tk.BooleanVar(value=self.st["loop"])
        self.var_open = tk.BooleanVar(value=self.st["open"])
        self.var_autoupd = tk.BooleanVar(value=self.st["autoupd"])
        self.var_gpu = tk.BooleanVar(value=self.st["gpu"])
        self.build()
        self.bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(40, self.pump)
        self.after(60, self.hover_tick)
        self.after(120, self.pulse_tick)
        self.after(200, self.check_ffmpeg)
        self.after(300, self.set_icon)
        self.after(400, self.start_art)
        if self.var_autoupd.get():
            self.after(1500, lambda: self.start_update_check(False))

    # ------------------------------------------------------------ Infrastruktur
    def load_settings(self):
        d = {"pre": 4.0, "post": 2.0, "sens": 6, "mode": "Verlustfrei", "open": True, "loop": True,
             "server": "Bruder (Kill-Notify)", "autoupd": True, "gpu": True}
        try:
            with open(SETTINGS) as f:
                d.update(json.load(f))
        except Exception:
            pass
        return d

    def on_close(self):
        self.st.update(pre=self.s_pre.get(), post=self.s_post.get(), sens=int(self.s_sens.get()),
                       mode=self.mode_var.get(), server=self.server_menu.get(), open=self.var_open.get(),
                       loop=self.var_loop.get(), autoupd=self.var_autoupd.get(), gpu=self.var_gpu.get())
        try:
            with open(SETTINGS, "w") as f:
                json.dump(self.st, f)
        except Exception:
            pass
        self.playing = False
        self.cancel.set()
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
            self.after(40, self.pump)

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

    # ------------------------------------------------------------ Effekte
    def hover_tick(self):
        """Karten-Rand leuchtet sanft auf, wenn die Maus darueber ist."""
        try:
            px, py = self.winfo_pointerxy()
            for c in self.cards:
                if not c.winfo_viewable():
                    continue
                x, y = c.winfo_rootx(), c.winfo_rooty()
                inside = x <= px < x + c.winfo_width() and y <= py < y + c.winfo_height()
                c.glow_target = 1.0 if inside else 0.0
                if abs(c.glow - c.glow_target) > 0.01:
                    c.glow += (c.glow_target - c.glow) * 0.35
                    c.configure(border_color=mix(BORDER, GLOW, c.glow))
                elif c.glow != c.glow_target:
                    c.glow = c.glow_target
                    c.configure(border_color=mix(BORDER, GLOW, c.glow))
        except Exception:
            pass
        if self.winfo_exists():
            self.after(40, self.hover_tick)

    def pulse_tick(self):
        t = (math.sin(time.time() * 4) + 1) / 2
        if self.scanning:
            col = mix("#0d6b4b", GREEN_HI, t)
            for d in self.scan_dots:
                d.configure(text_color=col)
        if self.winfo_exists():
            self.after(60, self.pulse_tick)

    def anim_number(self, key, label, target, fmt_fn=lambda v: str(int(round(v)))):
        """Zahl zaehlt sanft hoch/runter."""
        self.num_anim[key] = (label, target, fmt_fn, self.num_anim.get(key, (0, 0.0))[-1] if False else
                              getattr(label, "_cur", 0.0))

        def step():
            lbl, tgt, ff, _ = self.num_anim[key]
            cur = getattr(lbl, "_cur", 0.0)
            cur = tgt if abs(tgt - cur) < 0.02 * max(1, abs(tgt)) else cur + (tgt - cur) * 0.2
            lbl._cur = cur
            lbl.configure(text=ff(cur))
            if cur != tgt:
                self.after(30, step)
        step()

    def start_art(self):
        if not self.art_specs:
            return

        def work():
            res = [(lbl, render_art(kind, w, h, CARD, tint)) for lbl, kind, w, h, tint in self.art_specs]
            self.ui(self.art_ready, res)
        threading.Thread(target=work, daemon=True).start()

    def art_ready(self, res):
        self.arts = [(lbl, [ImageTk.PhotoImage(f) for f in frames]) for lbl, frames in res]
        for lbl, imgs in self.arts:
            lbl.configure(image=imgs[0])
        self.art_i = 0
        self.art_tick()

    def art_tick(self):
        if self.page == "home" and self.state() != "iconic":
            self.art_i += 1
            for lbl, imgs in self.arts:
                lbl.configure(image=imgs[self.art_i % len(imgs)])
        if self.winfo_exists():
            self.after(90, self.art_tick)

    def toast(self, text, color=GREEN):
        self.toast_lbl.configure(text=f"   {text}   ", fg_color=color,
                                 text_color="#04120c" if color in (GREEN, GREEN_HI) else "white")
        self._toast_id = getattr(self, "_toast_id", 0) + 1
        tid = self._toast_id

        def slide(y):
            if tid != self._toast_id:
                return
            self.toast_lbl.place(relx=0.5, rely=y, anchor="center")
            self.toast_lbl.lift()
            if y > 0.93:
                self.after(12, slide, y - 0.012)
        slide(1.05)
        self.after(3400, lambda: tid == self._toast_id and self.toast_lbl.place_forget())

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
            "danger": dict(fg_color="#2a1517", hover_color=RED, text_color="#ff9d9d"),
            "flat": dict(fg_color="transparent", hover_color="#1b1d1e", text_color=MUTED),
        }[kind]
        kw.setdefault("height", 36)
        kw.setdefault("corner_radius", 10)
        return ctk.CTkButton(parent, text=text, command=cmd, font=ctk.CTkFont(FONT, 12, "bold"), **styles, **kw)

    def card(self, parent, r, c, rs=1, cs=1, art=None):
        card = Card(parent)
        card.grid(row=r, column=c, rowspan=rs, columnspan=cs, sticky="nsew", padx=6, pady=6)
        self.cards.append(card)
        if art:                                  # vor allen anderen Kindern anlegen (liegt dann dahinter)
            kind, w, h, tint = art
            lbl = tk.Label(card, bg=CARD, bd=0, width=w, height=h)
            lbl.place(relx=1, rely=1, x=-8, y=-8, anchor="se")
            self.art_specs.append((lbl, kind, w, h, tint))
        return card

    def card_title(self, card, text, sub="", pad=(18, 0)):
        self.t(card, text, 18, TEXT, True).pack(anchor="w", padx=20, pady=(pad[0], 0))
        if sub:
            self.t(card, sub, 10, DIM).pack(anchor="w", padx=20)

    def dot_line(self, parent, text, state="ok", size=11, **pack):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(**pack)
        col = {"ok": GREEN, "bad": RED, "warn": AMBER, "idle": DIM}[state]
        d = self.t(f, "●", 11, col)
        d.pack(side="left")
        lbl = self.t(f, text, size, TEXT)
        lbl.pack(side="left", padx=(6, 0))
        return d, lbl

    def set_dot(self, dot, state):
        dot.configure(text_color={"ok": GREEN, "bad": RED, "warn": AMBER, "idle": DIM}[state])

    def bar(self, parent, **kw):
        b = ctk.CTkProgressBar(parent, height=4, fg_color="#2a2c2e", progress_color="#f1f2f2", corner_radius=3, **kw)
        b.set(0)
        return b

    def slider_row(self, parent, title, lo, hi, steps, value, unit_fn):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=1)
        row.grid_columnconfigure(1, weight=1)
        self.t(row, title, 11, MUTED, anchor="w", width=104).grid(row=0, column=0, sticky="w")
        val = self.t(row, "", 11, TEXT, True, anchor="e", width=70)
        s = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=steps, height=14, progress_color=GREEN,
                          button_color="#f1f2f2", button_hover_color=GREEN_HI, fg_color="#2a2c2e",
                          command=lambda v: val.configure(text=unit_fn(v)))
        s.set(value)
        val.configure(text=unit_fn(value))
        s.grid(row=0, column=1, sticky="ew", padx=8)
        val.grid(row=0, column=2, sticky="e")
        return s

    def toggle(self, parent, text, var, cmd=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=5)
        self.t(row, text, 11, TEXT, anchor="w").pack(side="left")
        sw = ctk.CTkSwitch(row, text="", variable=var, onvalue=True, offvalue=False, width=44, height=22,
                           progress_color=GREEN, button_color="#f1f2f2", button_hover_color="white",
                           fg_color="#3a3d3f", command=cmd)
        sw.pack(side="right")
        return sw

    # ------------------------------------------------------------ Aufbau
    def build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.scan_dots = []
        self.build_sidebar()
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=(0, 18), pady=14)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)
        self.build_header(main)
        self.pages = {}
        for name in ("home", "editor"):
            f = ctk.CTkFrame(main, fg_color="transparent")
            self.pages[name] = f
        self.build_home(self.pages["home"])
        self.build_editor(self.pages["editor"])
        self.toast_lbl = ctk.CTkLabel(self, text="", fg_color=GREEN, text_color="#04120c", corner_radius=16,
                                      height=38, font=ctk.CTkFont(FONT, 13, "bold"))
        self.show_page("home")

    def build_sidebar(self):
        sb = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color=PANEL)
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        logo = ctk.CTkFrame(sb, fg_color="transparent")
        logo.pack(anchor="w", padx=22, pady=(26, 22))
        try:
            im = Image.open(resource("assets", "logo.png"))
            self.logo_img = ctk.CTkImage(light_image=im, dark_image=im, size=(34, 34))
            ctk.CTkLabel(logo, image=self.logo_img, text="").pack(side="left", padx=(0, 10))
        except Exception:
            pass
        self.t(logo, "KILL APP", 19, TEXT, True).pack(side="left")
        self.t(logo, "beta", 9, DIM, True, fg_color="#1b1d1e", corner_radius=6, width=34, height=18).pack(
            side="left", padx=8)

        self.nav = {}
        self.t(sb, "MENÜ", 9, DIM, True).pack(anchor="w", padx=24, pady=(0, 6))
        for key, text in (("home", "⌂   Home"), ("editor", "✂   Editor")):
            b = ctk.CTkButton(sb, text=text, anchor="w", height=42, corner_radius=12, fg_color="transparent",
                              hover_color="#151718", text_color=MUTED, border_width=1, border_color=PANEL,
                              font=ctk.CTkFont(FONT, 13, "bold"), command=lambda k=key: self.show_page(k))
            b.pack(fill="x", padx=16, pady=3)
            self.nav[key] = b

        help_ = Card(sb, fg_color="#0f1311", border_color="#1d2a24", corner_radius=16)
        help_.pack(side="bottom", fill="x", padx=16, pady=18)
        self.t(help_, "Tipps & Tasten", 14, TEXT, True).pack(anchor="w", padx=16, pady=(14, 0))
        self.t(help_, "Alle Tastenkürzel auf einen Blick", 10, MUTED, wraplength=190, justify="left").pack(
            anchor="w", padx=16)
        self.button(help_, "Tastenkürzel zeigen", self.show_keys, "green", height=38).pack(
            fill="x", padx=14, pady=(10, 14))
        self.ver_lbl = self.t(sb, f"Version {APP_VERSION}", 10, DIM)
        self.ver_lbl.pack(side="bottom", pady=(0, 2))

    def build_header(self, main):
        h = ctk.CTkFrame(main, fg_color="transparent")
        h.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 4))
        left = ctk.CTkFrame(h, fg_color="transparent")
        left.pack(side="left")
        self.t(left, "Kill App", 11, MUTED).pack(anchor="w")
        self.page_title = self.t(left, "Home", 28, TEXT, True)
        self.page_title.pack(anchor="w")
        right = ctk.CTkFrame(h, fg_color="#101112", corner_radius=14, border_width=1, border_color=BORDER)
        right.pack(side="right", pady=4)
        self.chip_dot = self.t(right, "●", 11, GREEN)
        self.chip_dot.pack(side="left", padx=(14, 4), pady=10)
        self.chip_lbl = self.t(right, f"v{APP_VERSION} · aktuell", 11, TEXT)
        self.chip_lbl.pack(side="left", padx=(0, 14))

    def show_page(self, name):
        if name != "editor" and self.playing:
            self.toggle_play()
        self.page = name
        for k, f in self.pages.items():
            f.grid_forget()
        self.pages[name].grid(row=1, column=0, sticky="nsew")
        self.page_title.configure(text="Home" if name == "home" else "Editor")
        for k, b in self.nav.items():
            on = k == name
            b.configure(fg_color=GREEN_DK if on else "transparent", border_color=GREEN_BD if on else PANEL,
                        text_color=TEXT if on else MUTED)
        if name == "editor":
            self.after(60, self.refresh_editor)

    def refresh_editor(self):
        if self.cap:
            self.seek(self.pos)          # Vorschau auf die jetzt sichtbare Groesse neu zeichnen
        self.timeline.redraw()

    # ------------------------------------------------------------ Home
    def build_home(self, pg):
        for c in range(6):
            pg.grid_columnconfigure(c, weight=1, uniform="c")
        for r, wt in enumerate((30, 30, 27, 23)):
            pg.grid_rowconfigure(r, weight=wt, uniform="r")

        # Willkommen
        c = self.card(pg, 0, 0, 2, 2, art=("ribbon", 340, 300, "#b79cff"))
        self.t(c, "Willkommen zurück", 11, MUTED).pack(anchor="w", padx=24, pady=(22, 0))
        self.t(c, "Kill App", 36, TEXT, True).pack(anchor="w", padx=24)
        lst = ctk.CTkFrame(c, fg_color="transparent")
        lst.pack(anchor="w", padx=24, pady=(26, 0))
        self.wl = {}
        for key, title in (("ffmpeg", "FFmpeg"), ("gpu", "GPU-Decoding"), ("server", "Server-Profil"),
                           ("upd", "Updates")):
            row = ctk.CTkFrame(lst, fg_color="transparent")
            row.pack(anchor="w", pady=6)
            d = self.t(row, "●", 12, GREEN)
            d.grid(row=0, column=0, rowspan=2, padx=(0, 10))
            self.t(row, title, 12, TEXT, True).grid(row=0, column=1, sticky="w")
            sub = self.t(row, "", 10, DIM)
            sub.grid(row=1, column=1, sticky="w")
            self.wl[key] = (d, sub)
        self.button(c, "Zum Editor  ›", lambda: self.show_page("editor"), "dark", width=130).place(
            relx=1, rely=1, x=-22, y=-20, anchor="se")

        # Kill-Suche
        c = self.card(pg, 0, 2, 1, 2)
        self.card_title(c, "Kill-Suche", "")
        self.search_sub = self.t(c, "Kein Video geladen", 10, DIM)
        self.search_sub.pack(anchor="w", padx=20)
        d, self.search_status = self.dot_line(c, "Bereit", "idle", anchor="w", padx=20, pady=(8, 0))
        self.search_dot = d
        self.scan_dots.append(d)
        self.gauge = Gauge(c, 90, CARD, BLUE)
        self.gauge.set(0, "0%", "Scan")
        self.gauge.place(relx=1, x=-20, y=14, anchor="ne")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(side="bottom", fill="x", padx=20, pady=(0, 16))
        self.scan_btn_home = self.button(row, "🎯  Kills suchen", self.start_scan, "green", width=150)
        self.scan_btn_home.pack(side="right")
        pb = ctk.CTkFrame(c, fg_color="transparent")
        pb.pack(side="bottom", fill="x", padx=20, pady=(0, 10))
        self.t(pb, "Fortschritt", 10, MUTED).pack(side="left")
        self.pct_lbl = self.t(pb, "0%", 10, TEXT, True)
        self.pct_lbl.pack(side="right")
        self.home_bar = self.bar(c)
        self.home_bar.pack(side="bottom", fill="x", padx=20, pady=(0, 4))
        # Kills
        c = self.card(pg, 0, 4)
        self.card_title(c, "Kills", "Gefunden")
        self.dot_line(c, "Erkannt", "ok", anchor="w", padx=20, pady=(6, 0))
        self.kills_num = self.t(c, "0", 34, TEXT, True)
        self.kills_num._cur = 0.0
        self.kills_num.pack(anchor="w", padx=20, pady=(2, 0))
        b = ctk.CTkFrame(c, fg_color="transparent")
        b.pack(side="bottom", fill="x", padx=20, pady=(0, 14))
        self.t(b, "Abdeckung", 10, MUTED).pack(side="left")
        self.cov_lbl = self.t(b, "0%", 10, TEXT, True)
        self.cov_lbl.pack(side="right")
        self.cov_bar = self.bar(c)
        self.cov_bar.pack(side="bottom", fill="x", padx=20, pady=(0, 2))
        # Clips
        c = self.card(pg, 0, 5)
        self.card_title(c, "Clips", "Gesamtlänge")
        self.dot_line(c, "Bereit", "ok", anchor="w", padx=20, pady=(6, 0))
        self.len_lbl = self.t(c, "0:00", 34, TEXT, True)
        self.len_lbl.pack(anchor="w", padx=20, pady=(2, 0))
        b = ctk.CTkFrame(c, fg_color="transparent")
        b.pack(side="bottom", fill="x", padx=20, pady=(0, 14))
        self.t(b, "Ø Länge", 10, MUTED).pack(side="left")
        self.avg_lbl = self.t(b, "–", 10, TEXT, True)
        self.avg_lbl.pack(side="right")
        self.avg_bar = self.bar(c)
        self.avg_bar.pack(side="bottom", fill="x", padx=20, pady=(0, 2))

        # Video
        c = self.card(pg, 1, 2, 1, 2, art=("torus", 230, 120, "#ff9a5c"))
        self.card_title(c, "Video", "")
        self.video_sub = self.t(c, "Noch kein Video geladen", 10, DIM, wraplength=260, justify="left")
        self.video_sub.pack(anchor="w", padx=20)
        self.video_dot, self.video_status = self.dot_line(c, "Kein Video", "idle", anchor="w", padx=20, pady=(8, 0))
        self.meta_lbl = self.t(c, "—", 11, MUTED)
        self.meta_lbl.pack(anchor="w", padx=20, pady=(6, 0))
        self.thumb_lbl = ctk.CTkLabel(c, text="", fg_color="#0a0b0c", corner_radius=10, width=168, height=94)
        self.thumb_lbl.place(relx=1, x=-20, y=20, anchor="ne")
        self.button(c, "📂  Video öffnen  ›", self.open_video, "dark", width=170).pack(
            side="bottom", anchor="w", padx=20, pady=(0, 18))

        # Kills-Verlauf
        c = self.card(pg, 1, 4, 1, 2)
        top = ctk.CTkFrame(c, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(18, 0))
        self.t(top, "Kills-Verlauf", 18, TEXT, True).pack(side="left")
        self.chart_stat = self.t(top, "", 11, MUTED)
        self.chart_stat.pack(side="right")
        self.t(c, "Verteilung der Kills über das Video", 10, DIM).pack(anchor="w", padx=20)
        self.chart = AreaChart(c)
        self.chart.pack(fill="both", expand=True, padx=14, pady=(6, 12))

        # Server-Profil
        c = self.card(pg, 2, 0, 1, 2, art=("sheet", 250, 120, "#9d8cff"))
        self.card_title(c, "Server-Profil", "Welche Kill-Erkennung genutzt wird", pad=(14, 0))
        self.dot_line(c, "Aktiv", "ok", anchor="w", padx=20, pady=(4, 0))
        self.server_menu = ctk.CTkOptionMenu(c, values=list(PROFILES), height=34, corner_radius=10, width=210,
                                             fg_color="#1b1d1e", button_color="#26292a", button_hover_color="#2f3335",
                                             dropdown_fg_color="#161718", dropdown_hover_color="#26292a",
                                             font=ctk.CTkFont(FONT, 12, "bold"), dropdown_font=ctk.CTkFont(FONT, 12),
                                             command=lambda v: self.refresh_welcome())
        self.server_menu.set(self.st["server"] if self.st["server"] in PROFILES else list(PROFILES)[0])
        self.server_menu.pack(side="bottom", anchor="w", padx=20, pady=(0, 16))

        # Erkennung
        c = self.card(pg, 2, 2, 1, 2)
        self.card_title(c, "Erkennung", "Zeitfenster und Empfindlichkeit", pad=(14, 2))
        self.s_pre = self.slider_row(c, "Vor dem Kill", 0, 10, 20, self.st["pre"], lambda v: f"{v:.1f} s")
        self.s_post = self.slider_row(c, "Nach dem Kill", 0, 10, 20, self.st["post"], lambda v: f"{v:.1f} s")
        self.s_sens = self.slider_row(c, "Empfindlichkeit", 1, 10, 9, self.st["sens"],
                                      lambda v: ["", "sehr niedrig", "niedrig", "niedrig", "mittel", "mittel",
                                                 "mittel+", "hoch", "hoch", "sehr hoch", "max"][int(v)])

        # Export
        c = self.card(pg, 3, 0, 1, 2)
        self.card_title(c, "Export", "", pad=(12, 0))
        self.mode_desc = self.t(c, "", 10, DIM, anchor="w", wraplength=330, justify="left")
        self.mode_desc.pack(anchor="w", padx=20)
        self.seg_home = ctk.CTkSegmentedButton(c, values=list(MODES), variable=self.mode_var, height=30,
                                               selected_color="#17805b", selected_hover_color="#1b9a70",
                                               text_color="white", unselected_color="#1b1d1e",
                                               unselected_hover_color="#26292a", fg_color="#1b1d1e",
                                               font=ctk.CTkFont(FONT, 11, "bold"))
        self.seg_home.pack(side="bottom", fill="x", padx=20, pady=(0, 14))
        self.mode_var.trace_add("write", lambda *a: self.mode_changed())
        self.mode_changed()
        self.exp_home = self.button(c, "Alle Clips exportieren  ›", lambda: self.export(False), "green")
        self.exp_home.pack(side="bottom", fill="x", padx=20, pady=(0, 8))

        # Updates
        c = self.card(pg, 3, 2, 1, 2, art=("sheet", 230, 100, "#6cc6f2"))
        self.card_title(c, "Updates", f"Installiert: Version {APP_VERSION}", pad=(12, 0))
        self.upd_dot, self.upd_lbl = self.dot_line(c, "Aktuell", "ok", anchor="w", padx=20, pady=(4, 0))
        self.upd_btn = self.button(c, "Nach Updates suchen  ›", lambda: self.start_update_check(True), "dark",
                                   width=190)
        self.upd_btn.pack(side="bottom", anchor="w", padx=20, pady=(0, 14))

        # Plattform-Einstellungen
        c = self.card(pg, 2, 4, 2, 2)
        self.card_title(c, "Einstellungen", "", pad=(18, 6))
        self.t(c, "ALLGEMEIN", 9, DIM, True).pack(anchor="w", padx=20, pady=(2, 2))
        self.toggle(c, "Clip in der Vorschau wiederholen", self.var_loop)
        self.toggle(c, "Ordner nach dem Export öffnen", self.var_open)
        self.toggle(c, "Beim Start nach Updates suchen", self.var_autoupd)
        self.t(c, "ERKENNUNG", 9, DIM, True).pack(anchor="w", padx=20, pady=(12, 2))
        self.toggle(c, "GPU-Beschleunigung beim Scan", self.var_gpu)
        self.button(c, "Tastenkürzel  ›", self.show_keys, "dark").pack(side="bottom", fill="x", padx=20, pady=(0, 18))
        self.refresh_welcome()

    # ------------------------------------------------------------ Editor
    def build_editor(self, pg):
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_rowconfigure(0, weight=1)
        left = ctk.CTkFrame(pg, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)

        tb = Card(left, corner_radius=16)
        tb.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 6))
        self.cards.append(tb)
        self.ed_file = self.t(tb, "Noch kein Video geladen", 13, TEXT, True, anchor="w")
        self.ed_file.pack(side="left", padx=(18, 10), pady=12)
        d, self.ed_status = self.dot_line(tb, "Bereit", "idle", side="left", padx=6)
        self.scan_dots.append(d)
        self.scan_btn_ed = self.button(tb, "🎯  Kills suchen", self.start_scan, "green", width=150)
        self.scan_btn_ed.pack(side="right", padx=14, pady=10)
        self.ed_bar = ctk.CTkProgressBar(tb, width=170, height=5, fg_color="#2a2c2e", progress_color=BLUE)
        self.ed_bar.set(0)
        self.ed_bar.pack(side="right", padx=8)

        self.vid_box = ctk.CTkFrame(left, fg_color="#050607", corner_radius=18, border_width=1, border_color=BORDER)
        self.vid_box.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self.vid = tk.Label(self.vid_box, bg="#050607", bd=0, fg=MUTED, font=(FONT, 14),
                            text="🎬\n\nÖffne ein Video auf der Home-Seite")
        self.vid.place(relx=0.5, rely=0.5, anchor="center")
        self.time_chip = ctk.CTkLabel(self.vid_box, text="00:00.00", fg_color="#000000", text_color="white",
                                      corner_radius=10, width=86, height=28, font=ctk.CTkFont("Consolas", 13, "bold"))
        self.time_chip.place(x=16, y=16)
        self.kill_chip = ctk.CTkLabel(self.vid_box, text="●  KILL", fg_color=RED, text_color="white",
                                      corner_radius=10, width=84, height=28, font=ctk.CTkFont(FONT, 12, "bold"))

        ctl = Card(left, corner_radius=16)
        ctl.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        self.cards.append(ctl)
        inner = ctk.CTkFrame(ctl, fg_color="transparent")
        inner.pack(pady=10, padx=14, fill="x")
        self.button(inner, "⏮", lambda: self.step_clip(-1), "ghost", width=44).pack(side="left", padx=2)
        self.button(inner, "−1 s", lambda: self.request_seek(self.pos - 1), "ghost", width=54).pack(side="left", padx=2)
        self.play_btn = self.button(inner, "▶", self.toggle_play, "green", width=64, height=44, corner_radius=22)
        self.play_btn.pack(side="left", padx=8)
        self.button(inner, "+1 s", lambda: self.request_seek(self.pos + 1), "ghost", width=54).pack(side="left", padx=2)
        self.button(inner, "⏭", lambda: self.step_clip(1), "ghost", width=44).pack(side="left", padx=2)
        self.speed_seg = ctk.CTkSegmentedButton(inner, values=["0.5×", "1×", "2×"], width=150, selected_color="#17805b",
                                                selected_hover_color="#1b9a70", text_color="white",
                                                unselected_color="#1b1d1e", fg_color="#1b1d1e",
                                                unselected_hover_color="#26292a", command=self.set_speed)
        self.speed_seg.set("1×")
        self.speed_seg.pack(side="right")

        tl = Card(left, corner_radius=16)
        tl.grid(row=3, column=0, sticky="ew", padx=6, pady=6)
        self.cards.append(tl)
        self.timeline = Timeline(tl, self)
        self.timeline.pack(fill="x", padx=8, pady=(8, 0))
        ed = ctk.CTkFrame(tl, fg_color="transparent")
        ed.pack(fill="x", padx=12, pady=10)
        self.button(ed, "[I]  Start hier", lambda: self.set_edge(0), "ghost", width=120).pack(side="left", padx=(0, 6))
        self.button(ed, "[O]  Ende hier", lambda: self.set_edge(1), "ghost", width=120).pack(side="left", padx=6)
        self.button(ed, "＋  Clip hier", self.add_clip, "ghost", width=120).pack(side="left", padx=6)
        self.t(ed, "Scrollen = Zoom · Ränder ziehen = Clip anpassen", 11, DIM).pack(side="right")

        rt = Card(pg, width=350, corner_radius=18)
        rt.grid(row=0, column=1, sticky="ns", padx=(6, 6), pady=6)
        rt.grid_propagate(False)
        rt.grid_rowconfigure(1, weight=1)
        rt.grid_columnconfigure(0, weight=1)
        self.cards.append(rt)
        head = ctk.CTkFrame(rt, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 6))
        self.t(head, "Clips", 18, TEXT, True).pack(side="left")
        self.count_lbl = ctk.CTkLabel(head, text="0", fg_color=GREEN_DK, text_color=GREEN, corner_radius=12,
                                      width=34, height=24, font=ctk.CTkFont(FONT, 12, "bold"))
        self.count_lbl.pack(side="left", padx=10)
        self.list = ctk.CTkScrollableFrame(rt, fg_color="transparent", scrollbar_button_color="#26292a",
                                           scrollbar_button_hover_color=GREEN)
        self.list.grid(row=1, column=0, sticky="nsew", padx=(8, 2))
        ex = ctk.CTkFrame(rt, fg_color="#0f1011", corner_radius=14)
        ex.grid(row=2, column=0, sticky="ew", padx=12, pady=12)
        self.t(ex, "EXPORT", 9, DIM, True).pack(anchor="w", padx=14, pady=(10, 4))
        self.seg_ed = ctk.CTkSegmentedButton(ex, values=list(MODES), variable=self.mode_var, height=28,
                                             selected_color="#17805b", selected_hover_color="#1b9a70",
                                             text_color="white", unselected_color="#1b1d1e", fg_color="#1b1d1e",
                                             unselected_hover_color="#26292a", font=ctk.CTkFont(FONT, 11, "bold"))
        self.seg_ed.pack(fill="x", padx=14)
        self.exp_bar = ctk.CTkProgressBar(ex, height=5, progress_color=GREEN, fg_color="#2a2c2e")
        self.exp_bar.set(0)
        self.exp_bar.pack(fill="x", padx=14, pady=(10, 8))
        self.exp_all = self.button(ex, "ALLE EXPORTIEREN", lambda: self.export(False), "green", height=42)
        self.exp_all.pack(fill="x", padx=14)
        self.button(ex, "Nur ausgewählten Clip", lambda: self.export(True), "dark").pack(
            fill="x", padx=14, pady=(8, 12))
        self.rebuild_cards()

    # ------------------------------------------------------------ Anzeige-Helfer
    def mode_changed(self):
        self.mode_desc.configure(text=MODES[self.mode_var.get()][1])

    def refresh_welcome(self):
        ff = bool(self.ffmpeg)
        self.wl["ffmpeg"][0].configure(text_color=GREEN if ff else RED)
        self.wl["ffmpeg"][1].configure(text="Bereit" if ff else "Nicht gefunden")
        self.wl["gpu"][1].configure(text="Automatisch")
        self.wl["server"][1].configure(text=self.server_menu.get() if hasattr(self, "server_menu") else "")
        if self.update_url:
            self.wl["upd"][0].configure(text_color=RED)
        else:
            self.wl["upd"][0].configure(text_color=GREEN)
        self.wl["upd"][1].configure(text="Neue Version verfügbar" if self.update_url else "Aktuell")

    def set_status(self, text, state="idle"):
        self.search_status.configure(text=text)
        self.ed_status.configure(text=text)
        if state != "busy":
            for d in self.scan_dots:
                self.set_dot(d, state)

    def set_progress(self, p):
        self.gauge.set(p, f"{int(p * 100)}%", "Scan")
        self.home_bar.set(p)
        self.ed_bar.set(p)
        self.pct_lbl.configure(text=f"{int(p * 100)}%")

    def refresh_stats(self):
        n = len(self.clips)
        total = sum(c.end - c.start for c in self.clips)
        self.anim_number("kills", self.kills_num, n)
        self.anim_number("len", self.len_lbl, total, lambda v: fmt_short(v))
        cov = total / self.duration if self.duration else 0
        self.cov_bar.set(min(1.0, cov * 5))
        self.cov_lbl.configure(text=f"{cov * 100:.1f}%")
        avg = total / n if n else 0
        self.avg_bar.set(min(1.0, avg / 10))
        self.avg_lbl.configure(text=f"{avg:.1f} s" if n else "–")
        bins = 48
        if self.duration and n:
            h = np.zeros(bins)
            for c in self.clips:
                h[min(bins - 1, int(c.kill / self.duration * bins))] += 1
            k = np.exp(-0.5 * (np.arange(-4, 5) / 1.3) ** 2)
            h = np.convolve(h, k / k.sum(), mode="same")
            self.chart.set_data(h, ("0:00", fmt_short(self.duration)))
            self.chart_stat.configure(text=f"●  {n} {'Kill' if n == 1 else 'Kills'}   ·   {fmt_short(self.duration)}", text_color=MUTED)
        else:
            self.chart.set_data([], ("", ""))
            self.chart_stat.configure(text="")
        self.exp_home.configure(text=f"Alle Clips exportieren ({n})  ›")
        self.exp_all.configure(text=f"ALLE EXPORTIEREN  ({n})")

    def show_keys(self):
        top = ctk.CTkToplevel(self, fg_color=PANEL)
        top.title("Tastenkürzel")
        top.geometry("420x330")
        top.transient(self)
        self.t(top, "Tastenkürzel", 20, TEXT, True).pack(anchor="w", padx=24, pady=(22, 10))
        for k, v in (("Leertaste", "Play / Pause"), ("← →", "±1 Sekunde (Shift: 1 Bild)"),
                     ("↑ ↓", "Vorheriger / nächster Clip"), ("I / O", "Start / Ende setzen"),
                     ("Entf", "Clip löschen"), ("Scrollen", "Zeitleiste zoomen")):
            r = ctk.CTkFrame(top, fg_color="transparent")
            r.pack(fill="x", padx=24, pady=4)
            self.t(r, k, 12, GREEN, True, width=100, anchor="w").pack(side="left")
            self.t(r, v, 12, TEXT).pack(side="left")
        self.after(200, top.lift)

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
                self.ui(self.toast, f"Du hast die neueste Version (v{APP_VERSION})", GREEN)
        threading.Thread(target=work, daemon=True).start()

    def show_update(self, version, url):
        self.update_url = url
        self.set_dot(self.upd_dot, "bad")
        self.upd_lbl.configure(text=f"Neue Version v{version} verfügbar")
        self.upd_btn.configure(text="Jetzt aktualisieren  ›", fg_color=GREEN, hover_color=GREEN_HI,
                               text_color="#04120c", border_width=0, command=self.do_update)
        self.chip_dot.configure(text_color=RED)
        self.chip_lbl.configure(text=f"Update v{version} verfügbar")
        self.refresh_welcome()
        self.toast(f"🚀  Neue Version v{version} verfügbar – siehe Home › Updates")

    def do_update(self):
        self.upd_btn.configure(state="disabled", text="Lade herunter …")

        def work():
            try:
                path = download_file(self.update_url,
                                     lambda p: self.ui(self.upd_btn.configure, {"text": f"Lade … {int(p * 100)} %"}))
                self.ui(self.run_installer, path)
            except Exception as e:
                self.ui(self.upd_btn.configure, {"state": "normal", "text": "Erneut versuchen"})
                self.ui(self.toast, f"Update fehlgeschlagen: {e}", RED)
        threading.Thread(target=work, daemon=True).start()

    def run_installer(self, path):
        subprocess.Popen([path, "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS"], creationflags=NOWIN)
        self.on_close()

    # ------------------------------------------------------------ Video
    def open_video(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.flv *.ts *.avi"), ("Alle", "*.*")])
        if p:
            self.load_video(p)

    def load_video(self, p):
        cap = cv2.VideoCapture(p)
        if not cap.isOpened():
            self.toast("Video konnte nicht geöffnet werden", RED)
            return
        self.playing = False
        self.play_btn.configure(text="▶")
        if self.cap:
            self.cap.release()
        self.cap, self.path = cap, p
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
        self.duration = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1) / self.fps
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        name = os.path.basename(p)
        self.video_sub.configure(text=name)
        self.search_sub.configure(text=name)
        self.ed_file.configure(text=name)
        self.meta_lbl.configure(text=f"{w}×{h}  ·  {self.fps:.0f} fps  ·  {fmt_short(self.duration)}")
        self.set_dot(self.video_dot, "ok")
        self.video_status.configure(text="Geladen")
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(min(self.duration * 0.1, self.duration - 1) * self.fps))
        ok, fr = cap.read()
        if ok:
            im = Image.fromarray(cv2.cvtColor(cv2.resize(fr, (336, 188), interpolation=cv2.INTER_AREA),
                                              cv2.COLOR_BGR2RGB))
            self.thumb_img = ctk.CTkImage(light_image=im, dark_image=im, size=(168, 94))
            self.thumb_lbl.configure(image=self.thumb_img)
        self.clips, self.sel = [], None
        self.rebuild_cards()
        self.set_progress(0)
        self.set_status("Bereit", "idle")
        self.vid.configure(text="")
        self.timeline.set_view(0, 60)
        self.seek(0)
        self.toast(f"Video geladen: {name}")

    def show_frame(self):
        ok, f = self.cap.read()
        if not ok:
            return False
        self._frame_n += 1
        cw, ch = self.vid_box.winfo_width() - 16, self.vid_box.winfo_height() - 16
        fh, fw = f.shape[:2]
        s = max(0.05, min(cw / fw, ch / fh))
        f = cv2.resize(f, (max(2, int(fw * s)), max(2, int(fh * s))), interpolation=cv2.INTER_AREA)
        self._img = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)))
        self.vid.configure(image=self._img)
        self.pos = (int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1) / self.fps
        self.time_chip.configure(text=fmt(self.pos))
        near = any(abs(self.pos - c.kill) < 0.4 for c in self.clips)
        if near:
            self.kill_chip.place(x=self.vid_box.winfo_width() - 100, y=16)
        else:
            self.kill_chip.place_forget()
        if self.playing and self.pos > self.timeline.v1:
            self.timeline.set_view(self.pos - 2, self.pos - 2 + (self.timeline.v1 - self.timeline.v0))
        if not self.playing or self._frame_n % 2 == 0:
            self.timeline.redraw()
        return True

    def seek(self, t):
        if not self.cap:
            return
        t = min(max(0.0, t), max(0.0, self.duration - 0.1))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * self.fps))
        self.show_frame()
        if self.playing:
            self.play_t0, self.play_pos0 = time.perf_counter(), self.pos

    def request_seek(self, t):
        self._seek_to = t
        if not getattr(self, "_seek_job", None):
            self._seek_job = self.after(20, self._do_seek)

    def _do_seek(self):
        self._seek_job = None
        t, self._seek_to = self._seek_to, None
        if t is not None:
            self.seek(t)

    def set_speed(self, v):
        self.speed = float(v.replace("×", ""))
        if self.playing:
            self.play_t0, self.play_pos0 = time.perf_counter(), self.pos

    def toggle_play(self):
        if not self.cap:
            return
        self.playing = not self.playing
        self.play_btn.configure(text="❚❚" if self.playing else "▶")
        if self.playing:
            self.play_clip = next((c for c in self.clips if c.start <= self.pos <= c.end), None)
            if self.play_clip and self.pos >= self.play_clip.end - 0.05:
                self.seek(self.play_clip.start)
            self.play_t0, self.play_pos0 = time.perf_counter(), self.pos
            self.play_loop()

    def play_loop(self):
        if not self.playing:
            return
        target = self.play_pos0 + (time.perf_counter() - self.play_t0) * self.speed
        n = int((target - self.pos) * self.fps)
        if n >= 1:
            for _ in range(min(n, 8) - 1):
                self.cap.grab()
            if not self.show_frame():
                self.playing = False
                self.play_btn.configure(text="▶")
                return
            c = self.play_clip
            if c and self.pos >= c.end:
                if self.var_loop.get():
                    self.seek(c.start)
                    self.play_t0, self.play_pos0 = time.perf_counter(), self.pos
                else:
                    self.toggle_play()
                    return
        self.after(5, self.play_loop)

    # ------------------------------------------------------------ Scan
    def set_scan_buttons(self, scanning):
        for b in (self.scan_btn_home, self.scan_btn_ed):
            if scanning:
                b.configure(text="✕  Abbrechen", fg_color=RED, hover_color="#ff6b6b", text_color="white")
            else:
                b.configure(text="🎯  Kills suchen", fg_color=GREEN, hover_color=GREEN_HI, text_color="#04120c")

    def start_scan(self):
        if self.scanning:
            self.cancel.set()
            return
        if not self.path:
            self.toast("Bitte zuerst ein Video öffnen", RED)
            return
        self.playing = False
        self.scanning = True
        self.cancel.clear()
        self.set_scan_buttons(True)
        self.set_progress(0)
        self.set_status("Suche läuft …", "busy")
        args = (self.path, self.s_pre.get(), self.s_post.get(), self.server_menu.get(), int(self.s_sens.get()))
        gpu = self.var_gpu.get()

        def work():
            try:
                res = scan_video(*args, gpu=gpu,
                                 progress=lambda p: self.ui(self.set_progress, p),
                                 on_kill=lambda n: self.ui(self.set_status, f"Suche läuft …  {n} {'Kill' if n == 1 else 'Kills'}", "busy"),
                                 cancel=self.cancel)
                self.ui(self.scan_done, *res)
            except Exception as e:
                self.ui(self.scan_failed, repr(e))
        threading.Thread(target=work, daemon=True).start()

    def scan_failed(self, msg):
        self.scan_reset()
        self.set_status("Fehler", "bad")
        self.toast(f"Fehler: {msg}", RED)

    def scan_reset(self):
        self.scanning = False
        self.set_scan_buttons(False)

    def scan_done(self, res, info):
        cancelled = self.cancel.is_set()
        self.scan_reset()
        self.set_progress(1.0)
        self.clips = [Clip(a, b, k, Image.fromarray(cv2.cvtColor(th, cv2.COLOR_BGR2RGB)) if th is not None else None)
                      for a, b, k, th in res]
        self.sel = None
        self.rebuild_cards()
        n = len(self.clips)
        extra = f" · {info['notices']} Meldungen geprüft" if self.server_menu.get().startswith("Bruder") else ""
        self.set_status(("Abgebrochen · " if cancelled else "Fertig · ") + f"{n} {'Kill' if n == 1 else 'Kills'}{extra}", "ok" if n else "warn")
        self.toast(f"🎯  {n} {'Kill' if n == 1 else 'Kills'} gefunden" if n else "Keine Kills gefunden – Empfindlichkeit erhöhen?",
                   GREEN if n else AMBER)
        if n:
            self.select(0)
            self.after(900, lambda: self.show_page("editor"))

    # ------------------------------------------------------------ Clips
    def rebuild_cards(self):
        for w in self.list.winfo_children():
            w.destroy()
        if not self.clips:
            self.t(self.list, "Noch keine Clips.\n\nÖffne ein Video und starte\ndie Kill-Suche.", 12, DIM,
                   justify="center").pack(pady=50)
        for i, c in enumerate(self.clips):
            self.make_card(i, c)
        self.count_lbl.configure(text=str(len(self.clips)))
        self.refresh_stats()
        self.timeline.redraw()

    def make_card(self, i, c):
        card = ctk.CTkFrame(self.list, fg_color="#141516", corner_radius=14, border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=5, padx=4)
        c.card = card
        th = None
        if c.thumb:
            c.img = ctk.CTkImage(light_image=c.thumb, dark_image=c.thumb, size=(112, 63))
            th = ctk.CTkLabel(card, image=c.img, text="", corner_radius=8)
            th.pack(side="left", padx=10, pady=10)
        col = ctk.CTkFrame(card, fg_color="transparent")
        col.pack(side="left", fill="both", expand=True, pady=10)
        c.title = self.t(col, "", 13, TEXT, True, anchor="w")
        c.title.pack(fill="x")
        c.sub = self.t(col, "", 11, MUTED, anchor="w")
        c.sub.pack(fill="x")
        dl = self.button(card, "✕", lambda i=i: self.delete(i), "flat", width=30, height=30, corner_radius=15)
        dl.pack(side="right", padx=8)
        self.update_card(i)
        for w in [card, col, c.title, c.sub] + ([th] if th else []):
            w.bind("<Button-1>", lambda e, i=i: self.select(i))
            w.bind("<Enter>", lambda e, i=i, card=card: card.configure(fg_color="#1a1c1d") if i != self.sel else None)
            w.bind("<Leave>", lambda e, i=i, card=card: card.configure(fg_color="#141516" if i != self.sel else GREEN_DK))

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
            self.playing = False
            self.play_btn.configure(text="▶")
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
        if not self.cap:
            return
        c = Clip(max(0, self.pos - self.s_pre.get()), min(self.duration, self.pos + self.s_post.get()), self.pos)
        ok, f = self.cap.read()
        if ok:
            f = cv2.resize(f, (256, int(256 * f.shape[0] / f.shape[1])), interpolation=cv2.INTER_AREA)
            c.thumb = Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(self.pos * self.fps))
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
        self.exp_home.configure(state="disabled")

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
        self.exp_home.configure(state="normal")
        self.set_status("Export fehlgeschlagen", "bad")
        self.toast("Export-Fehler: " + msg[:90], RED)

    def export_done(self, out, n):
        self.exp_all.configure(state="normal")
        self.exp_home.configure(state="normal")
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
            return lambda e: fn() if self.page == "editor" else None
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

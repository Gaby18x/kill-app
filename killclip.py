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


def frames_from_ffmpeg(ffmpeg, path, vf, w, h, cancel):
    """Liest vorverarbeitete Frames als Rohdaten (BGR) per Pipe. Nutzt GPU-Decoding, wenn moeglich."""
    n = w * h * 3
    for hw in (True, False):
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
               on_kill=None):
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
    for i, fr in frames_from_ffmpeg(ffmpeg, path, vf, fw, fh, cancel):
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
import queue
import threading
import time
import tkinter as tk

import customtkinter as ctk

BG, PANEL, CARD, CARD_HI = "#0a0c12", "#10131c", "#171b29", "#202640"
BORDER, TEXT, MUTED = "#262c44", "#eceef7", "#8a91ad"
ACC, ACC_HI, CYAN, RED, GREEN = "#7c5cff", "#977bff", "#22d3ee", "#ff3d63", "#34d399"
FONT = "Segoe UI"
SETTINGS = os.path.join(os.path.expanduser("~"), ".killapp.json")

MODES = {
    "Verlustfrei": ("copy", "1:1 Kopie, ohne Neu-Kodieren. Blitzschnell. Schnitt am Keyframe."),
    "Bildgenau": ("exact", "Neu kodiert (CRF 12). Bildgenauer Schnitt, praktisch ohne Verlust."),
    "Klein": ("h265", "H.265 (CRF 18). Deutlich kleinere Dateien, optisch gleich."),
}


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
        self.create_rectangle(P, 8, w - P, 8 + 18, fill=CARD_HI, outline="")
        for i, c in enumerate(app.clips):
            sel = i == app.sel
            self.create_rectangle(self.ox(c.start), 8, max(self.ox(c.end), self.ox(c.start) + 3), 26,
                                  fill=CYAN if sel else "#3b4270", outline="")
            self.create_rectangle(self.ox(c.kill) - 1, 8, self.ox(c.kill) + 1, 26, fill=RED, outline="")
        self.create_rectangle(self.ox(self.v0), 6, self.ox(self.v1), 28, outline=ACC_HI, width=2)
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
        self.create_rectangle(P, y0, w - P, y1, fill=BG, outline=BORDER)
        for i, c in enumerate(app.clips):
            if c.end < self.v0 or c.start > self.v1:
                continue
            sel = i == app.sel
            xa, xb = max(P, self.x_of(c.start)), min(w - P, self.x_of(c.end))
            self.create_rectangle(xa, y0 + 4, xb, y1 - 4, fill="#2b2368" if sel else "#202748",
                                  outline=ACC_HI if sel else "#343c66", width=2 if sel else 1)
            self.create_text((xa + xb) / 2, (y0 + y1) / 2, text=f"KILL {i + 1:02d}  ·  {c.end - c.start:.1f}s",
                             fill=TEXT if sel else MUTED, font=(FONT, 9, "bold"))
            if sel:
                for x in (self.x_of(c.start), self.x_of(c.end)):
                    if P <= x <= w - P:
                        self.create_rectangle(x - 4, y0 + 8, x + 4, y1 - 8, fill=ACC_HI, outline="")
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
        self.geometry("1440x860")
        self.minsize(1180, 720)
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
        self.build()
        self.bind_keys()
        self.after(40, self.pump)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.check_ffmpeg)
        self.after(300, self.set_icon)
        self.after(1500, self.start_update_check)

    def set_icon(self):
        try:
            if sys.platform.startswith("win"):
                self.iconbitmap(resource("assets", "icon.ico"))
            else:
                self.iconphoto(True, tk.PhotoImage(file=resource("assets", "logo.png")))
        except Exception:
            pass

    # ------------------------------------------------------------ Updates
    def start_update_check(self):
        def work():
            try:
                res = check_update()
            except Exception:
                return
            if res:
                self.ui(self.show_update, *res)
        threading.Thread(target=work, daemon=True).start()

    def show_update(self, version, url):
        self.update_url = url
        self.update_lbl.configure(text=f"Update verfügbar: v{version}")
        self.keys_box.pack_forget()
        self.update_box.pack(side="bottom", fill="x", padx=20, pady=(0, 14))
        self.toast(f"🚀  Neue Version v{version} verfügbar", ACC)

    def do_update(self):
        self.update_btn.configure(state="disabled", text="Lade herunter …")

        def work():
            try:
                path = download_file(self.update_url,
                                     lambda p: self.ui(self.update_btn.configure, {"text": f"Lade … {int(p * 100)} %"}))
                self.ui(self.run_installer, path)
            except Exception as e:
                self.ui(self.update_btn.configure, {"state": "normal", "text": "Erneut versuchen"})
                self.ui(self.toast, f"Update fehlgeschlagen: {e}", RED)
        threading.Thread(target=work, daemon=True).start()

    def run_installer(self, path):
        subprocess.Popen([path, "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS"], creationflags=NOWIN)
        self.on_close()

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

    # ------------------------------------------------------------ Aufbau
    def load_settings(self):
        d = {"pre": 4.0, "post": 2.0, "sens": 6, "mode": "Verlustfrei", "open": True, "loop": True,
             "server": "Bruder (Kill-Notify)"}
        try:
            with open(SETTINGS) as f:
                d.update(json.load(f))
        except Exception:
            pass
        return d

    def on_close(self):
        self.st.update(pre=self.s_pre.get(), post=self.s_post.get(), sens=int(self.s_sens.get()),
                       mode=self.seg.get(), server=self.server_menu.get(), open=bool(self.sw_open.get()), loop=bool(self.sw_loop.get()))
        try:
            with open(SETTINGS, "w") as f:
                json.dump(self.st, f)
        except Exception:
            pass
        self.playing = False
        self.cancel.set()
        self.destroy()

    def label(self, parent, text, size=12, color=TEXT, bold=False, **kw):
        return ctk.CTkLabel(parent, text=text, text_color=color,
                            font=ctk.CTkFont(FONT, size, "bold" if bold else "normal"), **kw)

    def button(self, parent, text, cmd, kind="ghost", **kw):
        styles = {
            "accent": dict(fg_color=ACC, hover_color=ACC_HI, text_color="white"),
            "ghost": dict(fg_color=CARD_HI, hover_color="#2b3354", text_color=TEXT),
            "danger": dict(fg_color="#3a1b27", hover_color=RED, text_color="#ff9db1"),
            "flat": dict(fg_color="transparent", hover_color=CARD_HI, text_color=MUTED),
        }[kind]
        kw.setdefault("height", 36)
        kw.setdefault("corner_radius", 10)
        return ctk.CTkButton(parent, text=text, command=cmd, font=ctk.CTkFont(FONT, 12, "bold"),
                             **styles, **kw)

    def slider(self, parent, title, lo, hi, steps, value, unit_fn):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self.label(row, title, 11, MUTED).pack(side="left")
        val = self.label(row, "", 11, TEXT, bold=True)
        val.pack(side="right")
        s = ctk.CTkSlider(parent, from_=lo, to=hi, number_of_steps=steps, height=16,
                          progress_color=ACC, button_color=ACC_HI, button_hover_color="white",
                          fg_color=CARD_HI,
                          command=lambda v: val.configure(text=unit_fn(v)))
        s.set(value)
        val.configure(text=unit_fn(value))
        s.pack(fill="x", pady=(4, 0))
        return s

    def build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.build_sidebar()
        self.build_center()
        self.build_clips()
        self.toast_lbl = ctk.CTkLabel(self, text="", fg_color=ACC, text_color="white", corner_radius=16,
                                      height=38, font=ctk.CTkFont(FONT, 13, "bold"))

    def build_sidebar(self):
        sb = ctk.CTkFrame(self, width=280, corner_radius=0, fg_color=PANEL)
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        pad = dict(padx=20)
        logo = ctk.CTkFrame(sb, fg_color="transparent")
        logo.pack(anchor="w", pady=(26, 0), **pad)
        try:
            im = Image.open(resource("assets", "logo.png"))
            self.logo_img = ctk.CTkImage(light_image=im, dark_image=im, size=(46, 46))
            ctk.CTkLabel(logo, image=self.logo_img, text="").pack(side="left", padx=(0, 10))
        except Exception:
            pass
        self.label(logo, "KILL", 30, TEXT, True).pack(side="left")
        self.label(logo, " APP", 30, ACC_HI, True).pack(side="left")
        self.label(sb, "Automatischer Kill-Clip Cutter", 11, MUTED).pack(anchor="w", **pad)

        self.button(sb, "📂  Video öffnen", self.open_video, "accent", height=44).pack(fill="x", pady=(24, 10), **pad)
        info = ctk.CTkFrame(sb, fg_color=CARD, corner_radius=12)
        info.pack(fill="x", **pad)
        self.file_lbl = self.label(info, "Noch kein Video geladen", 12, TEXT, True, anchor="w", wraplength=210,
                                   justify="left")
        self.file_lbl.pack(fill="x", padx=14, pady=(12, 2))
        self.meta_lbl = self.label(info, "—", 11, MUTED, anchor="w")
        self.meta_lbl.pack(fill="x", padx=14, pady=(0, 12))

        self.label(sb, "SERVER", 11, ACC_HI, True).pack(anchor="w", pady=(24, 4), **pad)
        self.server_menu = ctk.CTkOptionMenu(sb, values=list(PROFILES), height=36, corner_radius=10,
                                             fg_color=CARD_HI, button_color=ACC, button_hover_color=ACC_HI,
                                             dropdown_fg_color=CARD, dropdown_hover_color=CARD_HI,
                                             font=ctk.CTkFont(FONT, 12, "bold"), dropdown_font=ctk.CTkFont(FONT, 12))
        self.server_menu.set(self.st["server"] if self.st["server"] in PROFILES else list(PROFILES)[0])
        self.server_menu.pack(fill="x", **pad)
        self.label(sb, "ERKENNUNG", 11, ACC_HI, True).pack(anchor="w", pady=(18, 0), **pad)
        box = ctk.CTkFrame(sb, fg_color="transparent")
        box.pack(fill="x", **pad)
        self.s_pre = self.slider(box, "Vor dem Kill", 0, 10, 20, self.st["pre"], lambda v: f"{v:.1f} s")
        self.s_post = self.slider(box, "Nach dem Kill", 0, 10, 20, self.st["post"], lambda v: f"{v:.1f} s")
        self.s_sens = self.slider(box, "Empfindlichkeit", 1, 10, 9, self.st["sens"],
                                  lambda v: ["", "sehr niedrig", "niedrig", "niedrig", "mittel", "mittel",
                                             "mittel+", "hoch", "hoch", "sehr hoch", "max"][int(v)])

        self.scan_btn = self.button(sb, "🎯  KILLS SUCHEN", self.start_scan, "accent", height=48)
        self.scan_btn.pack(fill="x", pady=(18, 8), **pad)
        self.scan_bar = ctk.CTkProgressBar(sb, height=8, progress_color=CYAN, fg_color=CARD_HI)
        self.scan_bar.set(0)
        self.scan_bar.pack(fill="x", **pad)
        self.status = self.label(sb, "Bereit", 11, MUTED, anchor="w", wraplength=240, justify="left")
        self.status.pack(fill="x", pady=(6, 0), **pad)

        keys = ctk.CTkFrame(sb, fg_color=CARD, corner_radius=12)
        keys.pack(side="bottom", fill="x", pady=(0, 16), **pad)
        self.keys_box = keys
        self.label(sb, f"Version {APP_VERSION}", 10, MUTED).pack(side="bottom", pady=(0, 6))
        self.update_box = ctk.CTkFrame(sb, fg_color="#241f4d", corner_radius=12, border_width=1, border_color=ACC)
        self.update_lbl = self.label(self.update_box, "", 12, TEXT, True)
        self.update_lbl.pack(padx=14, pady=(10, 4))
        self.update_btn = self.button(self.update_box, "Jetzt aktualisieren", self.do_update, "accent", height=34)
        self.update_btn.pack(fill="x", padx=14, pady=(0, 12))
        self.label(keys, "TASTEN", 10, ACC_HI, True).pack(anchor="w", padx=14, pady=(10, 2))
        self.label(keys, "Leertaste  Play / Pause\n← →  ±1 s   (Shift: 1 Bild)\n↑ ↓  Vorheriger / Nächster Clip\n"
                         "I / O  Start / Ende setzen\nEntf  Clip löschen",
                   10, MUTED, anchor="w", justify="left").pack(anchor="w", padx=14, pady=(0, 12))

    def build_center(self):
        ct = ctk.CTkFrame(self, fg_color="transparent")
        ct.grid(row=0, column=1, sticky="nsew", padx=18, pady=18)
        ct.grid_columnconfigure(0, weight=1)
        ct.grid_rowconfigure(0, weight=1)

        self.vid_box = ctk.CTkFrame(ct, fg_color="#05060a", corner_radius=18, border_width=1, border_color=BORDER)
        self.vid_box.grid(row=0, column=0, sticky="nsew")
        self.vid = tk.Label(self.vid_box, bg="#05060a", bd=0, fg=MUTED, font=(FONT, 14),
                            text="🎬\n\nVideo öffnen und „Kills suchen“ klicken")
        self.vid.place(relx=0.5, rely=0.5, anchor="center")
        self.time_chip = ctk.CTkLabel(self.vid_box, text="00:00.00", fg_color="#000000", text_color="white",
                                      corner_radius=10, width=86, height=28, font=ctk.CTkFont("Consolas", 13, "bold"))
        self.time_chip.place(x=16, y=16)
        self.kill_chip = ctk.CTkLabel(self.vid_box, text="●  KILL", fg_color=RED, text_color="white",
                                      corner_radius=10, width=84, height=28, font=ctk.CTkFont(FONT, 12, "bold"))

        ctl = ctk.CTkFrame(ct, fg_color=CARD, corner_radius=14)
        ctl.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        inner = ctk.CTkFrame(ctl, fg_color="transparent")
        inner.pack(pady=10, padx=14, fill="x")
        self.button(inner, "⏮", lambda: self.step_clip(-1), "ghost", width=44).pack(side="left", padx=2)
        self.button(inner, "−1 s", lambda: self.request_seek(self.pos - 1), "ghost", width=54).pack(side="left", padx=2)
        self.play_btn = self.button(inner, "▶", self.toggle_play, "accent", width=64, height=44, corner_radius=22)
        self.play_btn.pack(side="left", padx=8)
        self.button(inner, "+1 s", lambda: self.request_seek(self.pos + 1), "ghost", width=54).pack(side="left", padx=2)
        self.button(inner, "⏭", lambda: self.step_clip(1), "ghost", width=44).pack(side="left", padx=2)
        self.sw_loop = ctk.CTkSwitch(inner, text="Clip wiederholen", progress_color=ACC, text_color=MUTED,
                                     font=ctk.CTkFont(FONT, 11))
        if self.st["loop"]:
            self.sw_loop.select()
        self.sw_loop.pack(side="right", padx=(10, 0))
        self.speed_seg = ctk.CTkSegmentedButton(inner, values=["0.5×", "1×", "2×"], width=150,
                                                selected_color=ACC, selected_hover_color=ACC_HI,
                                                unselected_color=CARD_HI, command=self.set_speed)
        self.speed_seg.set("1×")
        self.speed_seg.pack(side="right")

        tl = ctk.CTkFrame(ct, fg_color=CARD, corner_radius=14)
        tl.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.timeline = Timeline(tl, self)
        self.timeline.pack(fill="x", padx=8, pady=(8, 0))
        ed = ctk.CTkFrame(tl, fg_color="transparent")
        ed.pack(fill="x", padx=12, pady=10)
        self.button(ed, "[I]  Start hier", lambda: self.set_edge(0), "ghost", width=120).pack(side="left", padx=(0, 6))
        self.button(ed, "[O]  Ende hier", lambda: self.set_edge(1), "ghost", width=120).pack(side="left", padx=6)
        self.button(ed, "＋  Clip an dieser Stelle", self.add_clip, "ghost", width=170).pack(side="left", padx=6)
        self.edit_lbl = self.label(ed, "Scrollen = Zoom · Ränder ziehen = Clip anpassen", 11, MUTED)
        self.edit_lbl.pack(side="right")

    def build_clips(self):
        rt = ctk.CTkFrame(self, width=340, corner_radius=0, fg_color=PANEL)
        rt.grid(row=0, column=2, sticky="nse")
        rt.grid_propagate(False)
        rt.grid_rowconfigure(1, weight=1)
        rt.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(rt, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(24, 8))
        self.label(head, "CLIPS", 20, TEXT, True).pack(side="left")
        self.count_lbl = ctk.CTkLabel(head, text="0", fg_color=ACC, text_color="white", corner_radius=12,
                                      width=34, height=24, font=ctk.CTkFont(FONT, 12, "bold"))
        self.count_lbl.pack(side="left", padx=10)
        self.list = ctk.CTkScrollableFrame(rt, fg_color="transparent", scrollbar_button_color=CARD_HI,
                                           scrollbar_button_hover_color=ACC)
        self.list.grid(row=1, column=0, sticky="nsew", padx=(10, 4))
        self.empty_lbl = self.label(self.list, "Noch keine Clips.\n\nÖffne ein Video und starte\ndie Kill-Suche.",
                                    12, MUTED, justify="center")
        self.empty_lbl.pack(pady=50)

        ex = ctk.CTkFrame(rt, fg_color=CARD, corner_radius=16)
        ex.grid(row=2, column=0, sticky="ew", padx=14, pady=14)
        self.label(ex, "EXPORT", 11, ACC_HI, True).pack(anchor="w", padx=14, pady=(12, 6))
        self.seg = ctk.CTkSegmentedButton(ex, values=list(MODES), selected_color=ACC, selected_hover_color=ACC_HI,
                                          unselected_color=CARD_HI, command=self.mode_changed)
        self.seg.set(self.st["mode"] if self.st["mode"] in MODES else "Verlustfrei")
        self.seg.pack(fill="x", padx=14)
        self.mode_lbl = self.label(ex, "", 10, MUTED, anchor="w", wraplength=290, justify="left")
        self.mode_lbl.pack(fill="x", padx=14, pady=(6, 4))
        self.sw_open = ctk.CTkSwitch(ex, text="Ordner danach öffnen", progress_color=ACC, text_color=MUTED,
                                     font=ctk.CTkFont(FONT, 11))
        if self.st["open"]:
            self.sw_open.select()
        self.sw_open.pack(anchor="w", padx=14, pady=4)
        self.exp_bar = ctk.CTkProgressBar(ex, height=8, progress_color=GREEN, fg_color=CARD_HI)
        self.exp_bar.set(0)
        self.exp_bar.pack(fill="x", padx=14, pady=(6, 8))
        self.exp_all = self.button(ex, "ALLE EXPORTIEREN", lambda: self.export(False), "accent", height=44)
        self.exp_all.pack(fill="x", padx=14)
        self.button(ex, "Nur ausgewählten Clip", lambda: self.export(True), "ghost").pack(fill="x", padx=14, pady=(8, 14))
        self.mode_changed(self.seg.get())

    # ------------------------------------------------------------ Helfer
    def toast(self, text, color=ACC):
        self.toast_lbl.configure(text=f"   {text}   ", fg_color=color)
        self._toast_id = getattr(self, "_toast_id", 0) + 1
        tid = self._toast_id

        def slide(y):
            if tid != self._toast_id:
                return
            self.toast_lbl.place(relx=0.5, rely=y, anchor="center")
            self.toast_lbl.lift()
            if y > 0.92:
                self.after(12, slide, y - 0.012)
        slide(1.05)
        self.after(3200, lambda: tid == self._toast_id and self.toast_lbl.place_forget())

    def check_ffmpeg(self):
        if not self.ffmpeg:
            self.toast("FFmpeg nicht gefunden – Export nicht möglich", RED)

    def mode_changed(self, v):
        self.mode_lbl.configure(text=MODES[v][1])

    def set_speed(self, v):
        self.speed = float(v.replace("×", ""))
        if self.playing:
            self.play_t0, self.play_pos0 = time.perf_counter(), self.pos

    def animate_count(self, target):
        cur = self._count_shown
        if cur == target:
            return
        cur += 1 if target > cur else -1
        self._count_shown = cur
        self.count_lbl.configure(text=str(cur))
        self.after(40, self.animate_count, target)

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
        self.file_lbl.configure(text=os.path.basename(p))
        self.meta_lbl.configure(text=f"{w}×{h} · {self.fps:.0f} fps · {fmt_short(self.duration)}")
        self.clips, self.sel = [], None
        self.rebuild_cards()
        self.scan_bar.set(0)
        self.status.configure(text="Bereit – klicke auf „Kills suchen“")
        self.vid.configure(text="")
        self.timeline.set_view(0, 60)
        self.seek(0)

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
                if self.sw_loop.get():
                    self.seek(c.start)
                    self.play_t0, self.play_pos0 = time.perf_counter(), self.pos
                else:
                    self.toggle_play()
                    return
        self.after(5, self.play_loop)

    # ------------------------------------------------------------ Scan
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
        self.scan_btn.configure(text="✕  ABBRECHEN", fg_color=RED, hover_color="#ff6483")
        self.scan_bar.set(0)
        self.status.configure(text="Suche läuft…  0 Kills")
        args = (self.path, self.s_pre.get(), self.s_post.get(), self.server_menu.get(), int(self.s_sens.get()))

        def work():
            try:
                res = scan_video(*args,
                                 progress=lambda p: self.ui(self.scan_bar.set, p),
                                 on_kill=lambda n: self.ui(self.status.configure,
                                                           {"text": f"Suche läuft…  {n} Kills"}),
                                 cancel=self.cancel)
                self.ui(self.scan_done, *res)
            except Exception as e:
                self.ui(self.scan_failed, repr(e))
        threading.Thread(target=work, daemon=True).start()

    def scan_failed(self, msg):
        self.scan_reset()
        self.toast(f"Fehler: {msg}", RED)

    def scan_reset(self):
        self.scanning = False
        self.scan_btn.configure(text="🎯  KILLS SUCHEN", fg_color=ACC, hover_color=ACC_HI)

    def scan_done(self, res, info):
        cancelled = self.cancel.is_set()
        self.scan_reset()
        self.scan_bar.set(1)
        self.clips = [Clip(a, b, k, Image.fromarray(cv2.cvtColor(th, cv2.COLOR_BGR2RGB))) for a, b, k, th in res]
        self.sel = None
        self.rebuild_cards()
        n = len(self.clips)
        extra = ""
        if self.server_menu.get().startswith("Bruder"):
            extra = f"\n{info['notices']} Info-Meldungen geprüft, {n} davon Kills"
        self.status.configure(text=("Abgebrochen – " if cancelled else "Fertig – ") + f"{n} Kills gefunden" + extra)
        self.toast(f"🎯  {n} Kills gefunden" if n else "Keine Kills gefunden – Empfindlichkeit erhöhen?",
                   GREEN if n else RED)
        if n:
            self.select(0)

    # ------------------------------------------------------------ Clips
    def rebuild_cards(self):
        for w in self.list.winfo_children():
            w.destroy()
        if not self.clips:
            self.empty_lbl = self.label(self.list, "Noch keine Clips.\n\nÖffne ein Video und starte\ndie Kill-Suche.",
                                        12, MUTED, justify="center")
            self.empty_lbl.pack(pady=50)
        for i, c in enumerate(self.clips):
            self.make_card(i, c)
        self.animate_count(len(self.clips))
        self.exp_all.configure(text=f"ALLE EXPORTIEREN  ({len(self.clips)})")
        self.timeline.redraw()

    def make_card(self, i, c):
        card = ctk.CTkFrame(self.list, fg_color=CARD, corner_radius=14, border_width=2, border_color=BORDER)
        card.pack(fill="x", pady=5, padx=4)
        c.card = card
        if c.thumb:
            c.img = ctk.CTkImage(light_image=c.thumb, dark_image=c.thumb, size=(112, 63))
            th = ctk.CTkLabel(card, image=c.img, text="", corner_radius=8)
            th.pack(side="left", padx=10, pady=10)
        col = ctk.CTkFrame(card, fg_color="transparent")
        col.pack(side="left", fill="both", expand=True, pady=10)
        c.title = self.label(col, "", 13, TEXT, True, anchor="w")
        c.title.pack(fill="x")
        c.sub = self.label(col, "", 11, MUTED, anchor="w")
        c.sub.pack(fill="x")
        dl = self.button(card, "✕", lambda i=i: self.delete(i), "flat", width=30, height=30, corner_radius=15)
        dl.pack(side="right", padx=8)
        self.update_card(i)
        widgets = [card, col, c.title, c.sub] + ([th] if c.thumb else [])
        for w in widgets:
            w.bind("<Button-1>", lambda e, i=i: self.select(i))
            w.bind("<Enter>", lambda e, c=c, i=i: card.configure(fg_color=CARD_HI) if i != self.sel else None)
            w.bind("<Leave>", lambda e, c=c, i=i: card.configure(fg_color=CARD if i != self.sel else "#241f4d"))

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
                c.card.configure(border_color=ACC if j == i else BORDER,
                                 fg_color="#241f4d" if j == i else CARD)
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
        self.toast("Clip hinzugefügt", GREEN)

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
        mode = MODES[self.seg.get()][0]
        base, ext = os.path.splitext(os.path.basename(self.path))
        jobs = [(self.clips[i].start, self.clips[i].end, os.path.join(out, f"{base}_kill{i + 1:02d}{ext}"))
                for i in idx]
        self.exp_bar.set(0)
        self.exp_all.configure(state="disabled")

        def work():
            try:
                for n, (a, b, dst) in enumerate(jobs, 1):
                    self.ui(self.status.configure, {"text": f"Export {n}/{len(jobs)} …"})
                    export_clip(self.ffmpeg, self.path, a, b, dst, mode)
                    self.ui(self.exp_bar.set, n / len(jobs))
                self.ui(self.export_done, out, len(jobs))
            except Exception as e:
                self.ui(self.export_failed, str(e))
        threading.Thread(target=work, daemon=True).start()

    def export_failed(self, msg):
        self.exp_all.configure(state="normal")
        self.toast("Export-Fehler – siehe Statuszeile", RED)
        self.status.configure(text=msg[:300])

    def export_done(self, out, n):
        self.exp_all.configure(state="normal")
        self.status.configure(text=f"{n} Clips exportiert")
        self.toast(f"✅  {n} Clips exportiert", GREEN)
        if self.sw_open.get():
            try:
                if sys.platform.startswith("win"):
                    os.startfile(out)
                else:
                    subprocess.Popen(["xdg-open", out])
            except Exception:
                pass

    # ------------------------------------------------------------ Tasten
    def bind_keys(self):
        def on(fn):
            return lambda e: (fn(), "break")[1]
        self.bind("<space>", on(self.toggle_play))
        self.bind("<Left>", lambda e: self.request_seek(self.pos - 1))
        self.bind("<Right>", lambda e: self.request_seek(self.pos + 1))
        self.bind("<Shift-Left>", lambda e: self.request_seek(self.pos - 1 / self.fps))
        self.bind("<Shift-Right>", lambda e: self.request_seek(self.pos + 1 / self.fps))
        self.bind("<Up>", lambda e: self.step_clip(-1))
        self.bind("<Down>", lambda e: self.step_clip(1))
        self.bind("i", lambda e: self.set_edge(0))
        self.bind("o", lambda e: self.set_edge(1))
        self.bind("<Delete>", lambda e: self.delete())


if __name__ == "__main__":
    App().mainloop()

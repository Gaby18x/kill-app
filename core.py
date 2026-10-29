"""Kill App - Kern: Kill-Erkennung, Export, Updates, Video-Player (ohne Oberflaeche).

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

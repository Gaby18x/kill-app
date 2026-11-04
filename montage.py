"""ACE - Montage: Clips aneinanderreihen, Musik/Sounds darunter, Übergänge, Export ohne Qualitätsverlust.

Aufbau wie in bekannten Schnittprogrammen: Medien links, Vorschau in der Mitte, Eigenschaften rechts,
mehrspurige Zeitleiste unten (Video-Spur + zwei Ton-Spuren)."""
import copy
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid

import cv2
import numpy as np
from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

try:
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
except Exception:
    QAudioFormat = QAudioSink = QMediaDevices = None

RATE = 48000


class PcmAudio:
    """Spielt den fertig gemischten Montage-Ton direkt über die Soundkarte ab (ohne Medien-Plugin, sehr zuverlässig).
    Gleiche Bedienung wie ein Player: play / pause / stop / setPosition / position (Millisekunden)."""

    def __init__(self):
        self.data = None          # bytes, s16le stereo 48 kHz
        self.source = None        # oder Funktion (start_sample, anzahl) -> bytes: live gemischt
        self.sink = None
        self.io = None
        self.idx = 0
        self.base_ms = 0
        self.timer = QTimer()
        self.timer.setInterval(15)
        self.timer.timeout.connect(self._feed)
        self.playing = False
        fmt = QAudioFormat()
        fmt.setSampleRate(RATE)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.Int16)
        self.fmt = fmt

    def load(self, data):
        pos = self.position()
        was = self.playing
        self.stop()
        self.data = data
        if was:
            self.setPosition(pos)
            self.play()

    def _open(self):
        if self.sink is None:
            dev = QMediaDevices.defaultAudioOutput()
            self.sink = QAudioSink(dev, self.fmt)
            self.sink.setBufferSize(RATE * 4 // 10)         # 0,1 s Puffer: kaum Verzögerung
        self.io = self.sink.start()

    def play(self):
        if self.data is None and self.source is None:
            return
        self._open()
        self.playing = True
        self._feed()
        self.timer.start()

    def _feed(self):
        if not self.playing or self.io is None or (self.data is None and self.source is None):
            return
        n = self.sink.bytesFree()
        n -= n % 4
        if n <= 0:
            return
        n = min(n, RATE * 4 // 20)                    # kleine Stücke: Änderungen sind sofort zu hören
        if self.source is not None:
            chunk = self.source(self.idx // 4, n // 4)
        else:
            chunk = self.data[self.idx:self.idx + n]
        if not chunk:
            return
        self.io.write(chunk)
        self.idx += len(chunk)

    def pause(self):
        if self.playing:
            self.base_ms = self.position()
        self.playing = False
        self.timer.stop()
        if self.sink is not None:
            self.sink.stop()
            self.sink = None
        self.io = None

    def stop(self):
        self.pause()

    def setPosition(self, ms):
        was = self.playing
        if was:
            self.pause()
        self.base_ms = max(0, int(ms))
        self.idx = int(self.base_ms / 1000 * RATE) * 4
        if was:
            self.play()

    def position(self):
        if self.playing and self.sink is not None:
            return self.base_ms + self.sink.processedUSecs() / 1000.0
        return self.base_ms

    def clock_ok(self):
        """Läuft der Ton wirklich (Soundkarte vorhanden und spielt)? Nur dann gibt er den Takt vor."""
        if not self.playing or self.sink is None:
            return False
        try:
            from PySide6.QtMultimedia import QAudio
            if self.sink.error() != QAudio.Error.NoError:
                return False
        except Exception:
            pass
        return self.sink.processedUSecs() > 0

import core
from theme import RED, T, ink, with_alpha
from ui import (ElidedLabel, Glass, PopButton, Segmented, SliderRow, app_font, display_font, paint_icon, tween)

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".flv", ".ts", ".avi", ".webm")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".wma")

# Übergänge: (Anzeigename, FFmpeg-xfade-Name)
TRANSITIONS = [("Keiner", "none"), ("Überblenden", "fade"), ("Schwarzblende", "fadeblack"), ("Blitz", "fadewhite"),
               ("Wischen", "wipeleft"), ("Schieben", "slideleft"), ("Zoom", "zoomin"), ("Kreis", "circleopen"),
               ("Auflösen", "dissolve"), ("Pixel", "pixelize"), ("Radial", "radial"), ("Unschärfe", "hblur")]
TRANS_NAME = {v: n for n, v in TRANSITIONS}
FONT_FILE = core.resource("assets", "fonts", "Montserrat_700Bold.ttf")
BOX_COLORS = ["#ffffff", "#111111", "#ff4d5e", "#ffb020", "#3ddc84", "#3ea6ff", "#a26bff", "#ff6bd5"]


def lbl(text="", size=10.0, bold=False, role=None, wrap=False):
    q = QLabel(text)
    q.setFont(app_font(size, bold))
    if role:
        q.setProperty("role", role)
    q.setWordWrap(wrap)
    return q


def fmt(t):
    t = max(0.0, t)
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:05.2f}"


def fmt_s(t):
    t = max(0.0, t)
    m, s = divmod(int(round(t)), 60)
    return f"{m}:{s:02d}"


def box_font_px(b, vw, vh):
    """Schriftgröße (Pixel) für den Text im Viereck: so groß wie gewählt, aber nie breiter als das Viereck."""
    want = max(6.0, vh * b.bh * b.tsize)
    f = display_font(10, QFont.Bold)
    f.setPixelSize(100)
    w100 = QFontMetrics(f).horizontalAdvance(b.text) or 1
    fit = (vw * b.bw * 0.92) / w100 * 100
    return int(max(6, min(want, fit)))


# ------------------------------------------------------------------ Medien-Infos
_INFO = {}


def clip_len(path, track=0):
    """Nutzbare Länge: auf Video-Spuren nur so lang wie es Bild gibt."""
    info = media_info(path)
    if track <= 0 and info.get("video"):
        return info.get("vdur") or info["dur"]
    return info["dur"]


def media_info(path):
    """Dauer, Größe, fps und ob Ton vorhanden ist (über FFmpeg gelesen, zwischengespeichert)."""
    if path in _INFO:
        return _INFO[path]
    ff = core.find_ffmpeg()
    info = {"dur": 0.0, "w": 0, "h": 0, "fps": 0.0, "audio": False, "video": False}
    try:
        r = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True, errors="ignore",
                           creationflags=core.NOWIN, timeout=20)
        e = r.stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", e)
        if m:
            info["dur"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        mv = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", e)
        if mv:
            info["video"], info["w"], info["h"] = True, int(mv.group(1)), int(mv.group(2))
        mf = re.search(r"([\d.]+) fps", e)
        if mf:
            info["fps"] = float(mf.group(1))
        info["audio"] = "Audio:" in e
        info["vdur"] = info["dur"]
        if info["video"] and info["fps"] > 1:
            # Bild kann kürzer sein als der Ton (z. B. Aufnahmen mit längerem Ton): echte Bild-Länge merken
            cap = cv2.VideoCapture(path)
            n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            fps = cap.get(cv2.CAP_PROP_FPS) or info["fps"]
            cap.release()
            if n > 2 and fps > 1:
                vd = n / fps
                if 0.5 < vd < info["dur"] - 0.03:
                    info["vdur"] = vd
    except Exception:
        pass
    _INFO[path] = info
    return info


import queue as _queue

_JOBS = _queue.Queue()
_PENDING = set()
_NOTIFY = [None]              # Funktion, die nach dem Laden neu zeichnen lässt (im Hauptthread)
_TIMG = {}                    # path -> QImage (wird im Hintergrund erzeugt)
_TPIX = {}                    # path -> QPixmap (im Hauptthread aus dem QImage)


def _worker():
    while True:
        kind, path = _JOBS.get()
        try:
            media_info(path)
            if kind == "thumb":
                cap = cv2.VideoCapture(path)
                if cap.isOpened():
                    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, media_info(path)["dur"] / 2) * 1000)
                    ok, fr = cap.read()
                    if ok:
                        h, w = fr.shape[:2]
                        fr = cv2.resize(fr, (160, max(2, int(160 * h / w))), interpolation=cv2.INTER_AREA)
                        rgb = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
                        _TIMG[path] = QImage(rgb.data, rgb.shape[1], rgb.shape[0], 3 * rgb.shape[1],
                                             QImage.Format_RGB888).copy()
                cap.release()
            elif kind == "wave":
                waveform(path)
        except Exception:
            pass
        _PENDING.discard((kind, path))
        if _NOTIFY[0] and _JOBS.empty():
            try:
                _NOTIFY[0]()
            except Exception:
                pass


threading.Thread(target=_worker, daemon=True).start()


def want(kind, path):
    if (kind, path) not in _PENDING:
        _PENDING.add((kind, path))
        _JOBS.put((kind, path))


def thumb_fast(path):
    """Vorschaubild ohne Warten: gibt None zurück und lädt im Hintergrund, falls noch nicht da."""
    if path in _TPIX:
        return _TPIX[path]
    img = _TIMG.get(path)
    if img is None:
        want("thumb", path)
        return None
    _TPIX[path] = QPixmap.fromImage(img)
    return _TPIX[path]


def wave_fast(path):
    if path in _WAVE:
        return _WAVE[path]
    want("wave", path)
    return None


def info_fast(path):
    if path in _INFO:
        return _INFO[path]
    want("info", path)
    return None


_THUMB = {}


def thumb(path, t=None):
    key = (path, round(t or -1, 1))
    if key in _THUMB:
        return _THUMB[key]
    pm = None
    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        if t is None:
            t = media_info(path)["dur"] / 2
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
        ok, fr = cap.read()
        if ok:
            h, w = fr.shape[:2]
            fr = cv2.resize(fr, (160, max(2, int(160 * h / w))), interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
            pm = QPixmap.fromImage(QImage(rgb.data, rgb.shape[1], rgb.shape[0], 3 * rgb.shape[1],
                                          QImage.Format_RGB888).copy())
    cap.release()
    _THUMB[key] = pm
    return pm


_WAVE = {}


def waveform(path, n=600):
    """Lautstärke-Verlauf für die Ton-Spur (n Werte 0..1)."""
    if path in _WAVE:
        return _WAVE[path]
    ff = core.find_ffmpeg()
    try:
        p = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", path, "-ac", "1", "-ar", "2000", "-f", "s16le",
                            "-"], capture_output=True, creationflags=core.NOWIN, timeout=60)
        a = np.abs(np.frombuffer(p.stdout, np.int16).astype(np.float32))
        if len(a) < n:
            w = np.zeros(n)
        else:
            w = a[: len(a) // n * n].reshape(n, -1).max(axis=1)
            w = w / max(1.0, w.max())
    except Exception:
        w = np.zeros(n)
    _WAVE[path] = w
    return w


# ------------------------------------------------------------------ Datenmodell
class MItem:
    """Ein Stück auf der Zeitleiste. track 0 = Haupt-Video, -1/-2/... = weitere Video-Spuren (liegen darüber),
    1/2/... = Ton-Spuren."""

    BOX_KEYS = ("kind", "bx", "by", "bw", "bh", "color", "alpha", "text", "tsize")

    def __init__(self, path, track=0, start=0.0, src_in=0.0, src_out=None, kind="media"):
        self.id = uuid.uuid4().hex[:8]
        self.path = path
        self.track = track
        self.start = start
        self.kind = kind
        self.bx, self.by, self.bw, self.bh = 0.35, 0.38, 0.30, 0.20
        self.color, self.alpha, self.text, self.tsize = "#ffffff", 0.85, "", 0.5
        dur = 3.0 if kind == "box" else clip_len(path, track)
        self.src_in = src_in
        self.src_out = src_out if src_out is not None else max(0.2, dur)
        self.speed = 1.0
        self.volume = 1.0
        self.trans = "none"          # Übergang zum nächsten Clip
        self.tdur = 0.5
        self.fade_in = 0.0
        self.fade_out = 0.0
        self.muted = False

    @property
    def vol(self):
        """Tatsächliche Lautstärke (0 wenn stumm)."""
        return 0.0 if self.muted else self.volume

    @property
    def dur(self):
        return max(0.05, (self.src_out - self.src_in) / self.speed)

    @property
    def end(self):
        return self.start + self.dur

    def to_dict(self):
        return {k: getattr(self, k) for k in ("id", "path", "track", "start", "src_in", "src_out", "speed", "volume",
                                               "trans", "tdur", "fade_in", "fade_out", "muted") + MItem.BOX_KEYS}

    @staticmethod
    def from_dict(d):
        it = MItem.__new__(MItem)
        it.id = d.get("id") or uuid.uuid4().hex[:8]
        for k in ("path", "track", "start", "src_in", "src_out", "speed", "volume", "trans", "tdur", "fade_in", "fade_out"):
            setattr(it, k, d.get(k, {"speed": 1.0, "volume": 1.0, "trans": "none", "tdur": 0.5, "fade_in": 0.0,
                                     "fade_out": 0.0, "track": 0, "start": 0.0, "src_in": 0.0}.get(k)))
        for k, dv in zip(MItem.BOX_KEYS, ("media", 0.35, 0.38, 0.30, 0.20, "#ffffff", 0.85, "", 0.5)):
            setattr(it, k, d.get(k, dv))
        it.muted = bool(d.get("muted", False))
        return it


class Project:
    def __init__(self):
        self.items = []
        self.magnet = True
        self.res = "Original"        # Original / 1080p / 1440p
        self.fps = "Original"        # Original / 60 / 30
        self.vlanes = 1              # Anzahl Video-Spuren
        self.alanes = 1              # Anzahl Ton-Spuren

    def videos(self):
        """Haupt-Video-Spur (hier rasten Clips ein und sitzen die Übergänge)."""
        return sorted([i for i in self.items if i.track == 0], key=lambda x: x.start)

    def overlays(self):
        """Clips auf den weiteren Video-Spuren (überdecken die Spuren darunter)."""
        return sorted([i for i in self.items if i.track < 0 and i.kind != "box"], key=lambda x: (-x.track, x.start))

    def boxes(self):
        """Vierecke (mit optionalem Text) - liegen immer ganz oben."""
        return sorted([i for i in self.items if i.kind == "box"], key=lambda x: (-x.track, x.start))

    def audios(self):
        return [i for i in self.items if i.track > 0]

    def lanes(self):
        """Spuren in Anzeige-Reihenfolge: oberste Video-Spur zuerst, dann Haupt-Video, dann Ton."""
        return list(range(-(self.vlanes - 1), 1)) + list(range(1, self.alanes + 1))

    def pack(self):
        """Einrasten: Clips kleben aneinander (Übergänge überlappen um ihre Dauer)."""
        if not self.magnet:
            return
        t = 0.0
        vs = self.videos()
        for k, it in enumerate(vs):
            it.start = t
            ov = 0.0
            if it.trans != "none" and k + 1 < len(vs):
                ov = min(it.tdur, it.dur * 0.45, vs[k + 1].dur * 0.45)
            t = it.end - ov

    def length(self):
        ends = [i.end for i in self.items]
        return max(ends) if ends else 0.0

    def to_dict(self):
        return {"items": [i.to_dict() for i in self.items], "magnet": self.magnet, "res": self.res, "fps": self.fps,
                "vlanes": self.vlanes, "alanes": self.alanes}

    def load(self, d):
        self.items = [MItem.from_dict(x) for x in (d or {}).get("items", [])
                      if x.get("kind") != "box" and os.path.exists(x.get("path", ""))]
        for it in self.items:                          # alte Projekte: nicht über das Bild-Ende hinaus
            if it.track <= 0:
                L = clip_len(it.path, it.track)
                if L > 0.3 and it.src_out > L + 0.02:
                    it.src_out = max(it.src_in + 0.2, L)
        self.magnet = (d or {}).get("magnet", True)
        self.res = (d or {}).get("res", "Original")
        self.fps = (d or {}).get("fps", "Original")
        tr = [i.track for i in self.items]
        self.vlanes = max((d or {}).get("vlanes", 1), 1 - min(tr + [0]))
        self.alanes = max((d or {}).get("alanes", 1), max(tr + [1]))


# ------------------------------------------------------------------ Export (FFmpeg)
def _atempo(speed):
    out, s = [], speed
    while s > 2.0:
        out.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        out.append("atempo=0.5")
        s /= 0.5
    out.append(f"atempo={s:.4f}")
    return ",".join(out)


def build_graph(proj, audio_only=False):
    """Baut den FFmpeg-Befehl (Eingänge + filter_complex). Gibt (inputs, filter, vlabel, alabel, W, H, fps, length)."""
    vs = proj.videos()
    aus = sorted(proj.audios(), key=lambda x: x.start)
    first = media_info(vs[0].path) if vs else {"w": 1920, "h": 1080, "fps": 60}
    W, H = first.get("w") or 1920, first.get("h") or 1080
    if proj.res == "1080p":
        W, H = int(round(1080 * W / H / 2)) * 2, 1080
    elif proj.res == "1440p":
        W, H = int(round(1440 * W / H / 2)) * 2, 1440
    W, H = W // 2 * 2, H // 2 * 2
    fps = first.get("fps") or 60
    if proj.fps in ("60", "30"):
        fps = float(proj.fps)
    fps = min(144.0, max(24.0, fps))
    inputs, f = [], []
    vlab, alab = None, None
    pos = 0.0                            # Länge der bisherigen Kette
    cursor = 0.0                         # Zeitleisten-Position am Ende der Kette
    n = 0

    def add_input(args):
        nonlocal n
        inputs.extend(args)
        n += 1
        return n - 1

    prev = None
    for k, it in enumerate(vs):
        info = media_info(it.path)
        i = add_input(["-ss", f"{it.src_in:.3f}", "-t", f"{(it.src_out - it.src_in):.3f}", "-i", it.path])
        d = it.dur
        vl, al = f"v{k}", f"a{k}"
        if not audio_only:
            vf_fade = ""
            if it.fade_in > 0.01:
                vf_fade += f",fade=t=in:st=0:d={min(it.fade_in, d):.3f}"
            if it.fade_out > 0.01:
                vf_fade += f",fade=t=out:st={max(0.0, d - it.fade_out):.3f}:d={min(it.fade_out, d):.3f}"
            f.append(f"[{i}:v]setpts=(PTS-STARTPTS)/{it.speed:.4f},fps={fps:.3f},"
                     f"tpad=stop_mode=clone:stop_duration={d:.3f},trim=duration={d:.3f},scale={W}:{H}:force_original_aspect_ratio="
                     f"decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p{vf_fade},"
                     f"settb=AVTB[{vl}]")
        if info["audio"]:
            af_fade = ""
            if it.fade_in > 0.01:
                af_fade += f",afade=t=in:st=0:d={min(it.fade_in, d):.3f}"
            if it.fade_out > 0.01:
                af_fade += f",afade=t=out:st={max(0.0, d - it.fade_out):.3f}:d={min(it.fade_out, d):.3f}"
            f.append(f"[{i}:a]asetpts=PTS-STARTPTS,{_atempo(it.speed)},volume={it.vol:.3f},aresample=48000,"
                     f"aformat=channel_layouts=stereo:sample_rates=48000,apad=whole_dur={d:.3f},atrim=0:{d:.3f}"
                     f"{af_fade}[{al}]")
        else:
            f.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{d:.3f}[{al}]")
        if vlab is None:
            gap = it.start
            if gap > 0.04:                                    # schwarzer Anfang
                if not audio_only:
                    f.append(f"color=c=black:s={W}x{H}:r={fps:.3f}:d={gap:.3f},format=yuv420p,settb=AVTB[g{k}]")
                    f.append(f"[g{k}][{vl}]concat=n=2:v=1:a=0[c{k}]")
                    vl = f"c{k}"
                f.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{gap:.3f}[ga{k}]")
                f.append(f"[ga{k}][{al}]concat=n=2:v=0:a=1[ca{k}]")
                al = f"ca{k}"
                pos = gap
            vlab, alab, pos, cursor = vl, al, pos + d, it.start + d
            prev = it
            continue
        gap = it.start - cursor
        ov = cursor - it.start
        if prev.trans != "none" and ov > 0.05:
            ov = min(ov, prev.dur * 0.45, d * 0.45)
            off = pos - ov
            if not audio_only:
                f.append(f"[{vlab}][{vl}]xfade=transition={prev.trans}:duration={ov:.3f}:offset={off:.3f}[x{k}]")
                vlab = f"x{k}"
            f.append(f"[{alab}][{al}]acrossfade=d={ov:.3f}[xa{k}]")
            alab = f"xa{k}"
            pos = pos - ov + d
        else:
            if gap > 0.04:
                if not audio_only:
                    f.append(f"color=c=black:s={W}x{H}:r={fps:.3f}:d={gap:.3f},format=yuv420p,settb=AVTB[g{k}]")
                    f.append(f"[{vlab}][g{k}]concat=n=2:v=1:a=0[gv{k}]")
                    vlab = f"gv{k}"
                f.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{gap:.3f}[ga{k}]")
                f.append(f"[{alab}][ga{k}]concat=n=2:v=0:a=1[gav{k}]")
                alab = f"gav{k}"
                pos += gap
            if not audio_only:
                f.append(f"[{vlab}][{vl}]concat=n=2:v=1:a=0[j{k}]")
                vlab = f"j{k}"
            f.append(f"[{alab}][{al}]concat=n=2:v=0:a=1[ja{k}]")
            alab = f"ja{k}"
            pos += d
        cursor = it.start + d
        prev = it
    ovs = proj.overlays()
    length = max(pos, max((a.end for a in aus), default=0.0), max((o.end for o in ovs), default=0.0),
                 max((b.end for b in proj.boxes()), default=0.0))
    # weitere Video-Spuren: liegen über dem Haupt-Video (Ton wird dazugemischt)
    ov_mix = []
    ov_list = []
    for k, o in enumerate(ovs):
        info = media_info(o.path)
        i = add_input(["-ss", f"{o.src_in:.3f}", "-t", f"{(o.src_out - o.src_in):.3f}", "-i", o.path])
        if not audio_only:
            f.append(f"[{i}:v]setpts=(PTS-STARTPTS)/{o.speed:.4f},fps={fps:.3f},"
                     f"tpad=stop_mode=clone:stop_duration={o.dur:.3f},trim=duration={o.dur:.3f},scale={W}:{H}:force_original_aspect_ratio="
                     f"decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                     f"setpts=PTS+{o.start:.3f}/TB[ov{k}]")
            ov_list.append((f"ov{k}", o))
        if info["audio"]:
            ms = int(round(o.start * 1000))
            f.append(f"[{i}:a]asetpts=PTS-STARTPTS,{_atempo(o.speed)},volume={o.vol:.3f},aresample=48000,"
                     f"aformat=channel_layouts=stereo:sample_rates=48000,adelay={ms}|{ms}[oa{k}]")
            ov_mix.append(f"oa{k}")
    # Musik / Sounds
    mix = ([alab] if alab else []) + ov_mix
    for k, a in enumerate(aus):
        i = add_input(["-ss", f"{a.src_in:.3f}", "-t", f"{(a.src_out - a.src_in):.3f}", "-i", a.path])
        d = a.dur
        fx = [f"asetpts=PTS-STARTPTS", _atempo(a.speed), f"volume={a.vol:.3f}", "aresample=48000",
              "aformat=channel_layouts=stereo:sample_rates=48000"]
        if a.fade_in > 0.01:
            fx.append(f"afade=t=in:st=0:d={min(a.fade_in, d):.3f}")
        if a.fade_out > 0.01:
            fx.append(f"afade=t=out:st={max(0.0, d - a.fade_out):.3f}:d={min(a.fade_out, d):.3f}")
        ms = int(round(a.start * 1000))
        fx.append(f"adelay={ms}|{ms}")
        f.append(f"[{i}:a]{','.join(fx)}[m{k}]")
        mix.append(f"m{k}")
    if not mix:
        f.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{max(0.1, length):.3f}[aout]")
    elif len(mix) == 1:
        f.append(f"[{mix[0]}]apad=whole_dur={length:.3f},atrim=0:{length:.3f}[aout]")
    else:
        f.append("".join(f"[{m}]" for m in mix) + f"amix=inputs={len(mix)}:duration=longest:normalize=0,"
                                                   f"atrim=0:{length:.3f}[aout]")
    if not audio_only:
        if vlab is None:
            f.append(f"color=c=black:s={W}x{H}:r={fps:.3f}:d={max(0.1, length):.3f},format=yuv420p[vb0]")
        elif length > pos + 0.04:                             # länger als die Haupt-Spur: schwarz auffüllen
            f.append(f"[{vlab}]tpad=stop_mode=add:stop_duration={length - pos:.3f}:color=black[vb0]")
        else:
            f.append(f"[{vlab}]null[vb0]")
        cur = "vb0"
        for n_, (lab, o) in enumerate(ov_list):
            f.append(f"[{cur}][{lab}]overlay=eof_action=pass:enable='between(t,{o.start:.3f},{o.end:.3f})'[vb{n_ + 1}]")
            cur = f"vb{n_ + 1}"
        fx = []
        for b in proj.boxes():
            en = f"enable='between(t,{b.start:.3f},{b.end:.3f})'"
            col = QColor(b.color).name()[1:]
            if b.alpha > 0.01:
                fx.append(f"drawbox=x=iw*{b.bx:.4f}:y=ih*{b.by:.4f}:w=iw*{b.bw:.4f}:h=ih*{b.bh:.4f}:"
                          f"color=0x{col}@{b.alpha:.3f}:t=fill:{en}")
            if b.text.strip():
                fsz = max(8, box_font_px(b, W, H))
                txt = b.text.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%")
                font = FONT_FILE.replace("\\", "/").replace(":", "\\:")
                tc = "000000" if QColor(b.color).lightness() > 150 and b.alpha > 0.3 else "ffffff"
                fx.append(f"drawtext=fontfile='{font}':text='{txt}':fontcolor=0x{tc}:fontsize={fsz}:"
                          f"x=w*{b.bx:.4f}+(w*{b.bw:.4f}-text_w)/2:y=h*{b.by:.4f}+(h*{b.bh:.4f}-text_h)/2:{en}")
        f.append(f"[{cur}]" + (",".join(fx) if fx else "null") + "[vout]")
    return inputs, ";".join(f), "vout", "aout", W, H, fps, length


def graph_args(graph, gfile):
    """Filtergraph an FFmpeg übergeben. Kurz: direkt. Sehr lang (viele Clips): als Datei – je nach FFmpeg-Version
    mit der neuen (-/filter_complex) oder alten Schreibweise (-filter_complex_script)."""
    if len(graph) < 24000:
        return ["-filter_complex", graph]
    ff = core.find_ffmpeg()
    try:
        v = subprocess.run([ff, "-hide_banner", "-version"], capture_output=True, text=True, creationflags=core.NOWIN).stdout
        major = int(re.search(r"version\D*(\d+)", v).group(1))
    except Exception:
        major = 7
    return ["-/filter_complex", gfile] if major >= 7 else ["-filter_complex_script", gfile]


def export_montage(proj, dst, progress=None, cancel=None):
    ff = core.find_ffmpeg()
    inputs, graph, vl, al, W, H, fps, length = build_graph(proj)
    gfile = os.path.join(tempfile.gettempdir(), f"ace_montage_{uuid.uuid4().hex[:6]}.txt")
    with open(gfile, "w", encoding="utf-8") as fh:
        fh.write(graph)
    base = [ff, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"] + inputs + \
           graph_args(graph, gfile) + ["-map", f"[{vl}]", "-map", f"[{al}]"]
    audio = ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"]
    attempts = []
    hw = core._hw_encoder(ff, "h264")
    if hw:
        attempts.append(core._enc_args(hw, 18) + ["-pix_fmt", "yuv420p"] + audio)
    attempts.append(["-c:v", "libx264", "-crf", "16", "-preset", "fast", "-pix_fmt", "yuv420p",
                     "-threads", str(max(1, (os.cpu_count() or 4) - 2))] + audio)
    flags = core.NOWIN | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    err = ""
    try:
        for args in attempts:
            p = subprocess.Popen(base + args + [dst], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 errors="ignore", creationflags=flags)
            for line in p.stdout:
                if cancel is not None and cancel.is_set():
                    p.kill()
                    p.wait()
                    raise RuntimeError("Abgebrochen")
                m = re.match(r"out_time_us=(\d+)", line.strip())
                if m and progress and length > 0:
                    progress(min(1.0, int(m.group(1)) / 1e6 / length))
            p.wait()
            err = p.stderr.read()
            if p.returncode == 0:
                return dst
        raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else "Export fehlgeschlagen")
    finally:
        try:
            os.remove(gfile)
        except OSError:
            pass


# ------------------------------------------------------------------ Vorschau (Bilder berechnen)
class FrameSource:
    """Liefert Bilder aus den Quellvideos. Jeder Clip hat seinen eigenen Leser (auch wenn zwei Clips aus derselben
    Datei stammen); der nächste Clip wird kurz vor dem Übergang im Hintergrund vorbereitet -> kein Ruckeln."""

    def __init__(self):
        self.caps = {}
        self.glock = threading.Lock()

    def _entry(self, key, path):
        with self.glock:
            c = self.caps.get(key)
            if c is None or c["path"] != path:
                cap = cv2.VideoCapture(path)
                c = self.caps[key] = {"cap": cap, "fps": cap.get(cv2.CAP_PROP_FPS) or 60.0, "last": -1.0,
                                      "lastf": None, "lock": threading.Lock(), "path": path}
            return c

    def get(self, key, path, t):
        c = self._entry(key, path)
        with c["lock"]:
            cap, fps, last, lastf = c["cap"], c["fps"], c["last"], c["lastf"]
            if lastf is not None and abs(t - last) < 0.5 / fps:
                return lastf
            if last < t < last + 0.6:
                n = int(round((t - last) * fps)) - 1
                for _ in range(max(0, n)):
                    cap.grab()
            else:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
            ok, fr = cap.read()
            if not ok:
                if lastf is None:                     # hinter dem Bild-Ende: letztes Bild zeigen
                    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                    if n > 1:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, n - 2)
                        ok, fr = cap.read()
                        if ok:
                            c["lastf"] = fr
                            return fr
                return lastf
            c["last"] = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 - 1.0 / fps
            c["lastf"] = fr
            return fr

    def warm(self, key, path, t):
        """Im Hintergrund schon an die Stelle springen, an der der Clip gleich beginnt."""
        c = self._entry(key, path)
        if c.get("warm") == round(t, 2):
            return
        c["warm"] = round(t, 2)
        threading.Thread(target=lambda: self.get(key, path, t), daemon=True).start()

    def close(self):
        with self.glock:
            for c in self.caps.values():
                c["cap"].release()
            self.caps = {}


def _fit(fr, W, H):
    h, w = fr.shape[:2]
    s = min(W / w, H / h)
    nw, nh = max(2, int(w * s)), max(2, int(h * s))
    out = np.zeros((H, W, 3), np.uint8)
    r = cv2.resize(fr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    y, x = (H - nh) // 2, (W - nw) // 2
    out[y:y + nh, x:x + nw] = r
    return out


def blend(a, b, p, kind):
    """Vorschau eines Übergangs (Annäherung an die FFmpeg-Übergänge beim Export)."""
    H, W = a.shape[:2]
    p = max(0.0, min(1.0, p))
    if kind in ("fadeblack", "fadewhite"):
        c = np.zeros_like(a) if kind == "fadeblack" else np.full_like(a, 255)
        return cv2.addWeighted(a, 1 - p * 2, c, p * 2, 0) if p < 0.5 else cv2.addWeighted(c, 2 - p * 2, b, p * 2 - 1, 0)
    if kind == "wipeleft":
        x = int(W * (1 - p))
        out = a.copy()
        out[:, x:] = b[:, x:]
        return out
    if kind == "slideleft":
        x = int(W * p)
        out = np.empty_like(a)
        out[:, :W - x] = a[:, x:]
        out[:, W - x:] = b[:, :x]
        return out
    if kind == "zoomin":
        z = 1 + p
        za = cv2.resize(a, None, fx=z, fy=z, interpolation=cv2.INTER_LINEAR)
        y0, x0 = (za.shape[0] - H) // 2, (za.shape[1] - W) // 2
        za = za[y0:y0 + H, x0:x0 + W]
        return cv2.addWeighted(za, 1 - p, b, p, 0)
    if kind == "circleopen":
        m = np.zeros((H, W), np.uint8)
        cv2.circle(m, (W // 2, H // 2), int(math.hypot(W, H) / 2 * p), 255, -1)
        out = a.copy()
        out[m > 0] = b[m > 0]
        return out
    if kind == "pixelize":
        k = max(1, int(40 * (1 - abs(p - 0.5) * 2)))
        src = a if p < 0.5 else b
        sm = cv2.resize(src, (max(1, W // k), max(1, H // k)), interpolation=cv2.INTER_AREA)
        return cv2.resize(sm, (W, H), interpolation=cv2.INTER_NEAREST)
    if kind == "hblur":
        k = int(1 + 60 * (1 - abs(p - 0.5) * 2)) | 1
        mix = cv2.addWeighted(a, 1 - p, b, p, 0)
        return cv2.blur(mix, (k, 1))
    return cv2.addWeighted(a, 1 - p, b, p, 0)


class ClipStream(threading.Thread):
    """Liest einen Clip fortlaufend über FFmpeg (mehrere Kerne, ggf. Grafikkarte): genau 60 Bilder pro Sekunde in
    Vorschau-Größe, Geschwindigkeit bereits eingerechnet. Läuft voraus und puffert -> gleichmäßige Wiedergabe."""
    FPS = 60.0

    def __init__(self, it, t_tl, W, H):
        super().__init__(daemon=True)
        import queue as q_
        off = max(0.0, t_tl - it.start)
        self.sig = (it.id, it.path, round(it.src_in, 3), round(it.src_out, 3), round(it.speed, 3), round(it.start, 3), W, H)
        self.t0 = it.start + off
        self.end = it.end
        self.W, self.H = W, H
        self.q = q_.Queue(maxsize=40)
        self.idx = -1
        self.cur = None
        self.stop_flag = False
        src = it.src_in + off * it.speed
        dur = max(0.05, (it.src_out - src))
        vf = (f"setpts=(PTS-STARTPTS)/{it.speed:.4f},fps={self.FPS:g},scale={W}:{H}:force_original_aspect_ratio=decrease:"
              f"flags=bilinear,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2")
        self.cmd = [core.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{src:.3f}", "-t", f"{dur:.3f}",
                    "-i", it.path, "-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        self.proc = None
        self.start()

    def run(self):
        n = self.W * self.H * 3
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         creationflags=core.NOWIN, bufsize=n * 2)
            k = 0
            while not self.stop_flag:
                buf = self.proc.stdout.read(n)
                if len(buf) < n:
                    break
                fr = np.frombuffer(buf, np.uint8).reshape(self.H, self.W, 3)
                while not self.stop_flag:
                    try:
                        self.q.put((k, fr), timeout=0.1)
                        break
                    except Exception:
                        pass
                k += 1
        except Exception:
            pass
        finally:
            try:
                self.q.put_nowait(None)                 # Ende markieren
            except Exception:
                pass
            self.close()

    def frame(self, t, wait=0.012):
        """Bild zum Zeitpunkt t (Zeitleiste). Überspringt alte Bilder, wartet kurz auf neue."""
        want = int(round((t - self.t0) * self.FPS))
        if want <= self.idx and self.cur is not None:
            return self.cur
        deadline = time.perf_counter() + wait
        while self.idx < want:
            rem = deadline - time.perf_counter()
            if rem <= 0:
                break
            try:
                item = self.q.get(timeout=rem)
            except Exception:
                break
            if item is None:
                try:
                    self.q.put_nowait(None)
                except Exception:
                    pass
                break
            self.idx, self.cur = item
        return self.cur

    def close(self):
        self.stop_flag = True
        p = self.proc
        if p is not None:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass


class LiveMixer:
    """Mischt den Ton der Montage live (Clips, Musik, Lautstärke, Stumm, Ein-/Ausblenden, Übergänge).
    Jede Änderung ist beim Abspielen sofort zu hören - es wird nichts neu berechnet."""

    def __init__(self, page):
        self.page = page
        self.stems = {}
        self.pending = set()

    @staticmethod
    def key(it):
        return (it.path, round(it.src_in, 3), round(it.src_out, 3), round(it.speed, 3))

    def ensure(self):
        keep = set()
        for it in self.page.proj.items:
            if it.kind == "box":
                continue
            inf = media_info(it.path)
            if not inf.get("audio"):
                continue
            k = self.key(it)
            keep.add(k)
            if k not in self.stems and k not in self.pending:
                self.pending.add(k)
                threading.Thread(target=self._decode, args=(k,), daemon=True).start()
        for k in list(self.stems):                    # alte Fassungen (z. B. nach dem Kürzen) freigeben
            if k not in keep:
                del self.stems[k]

    def _decode(self, k):
        path, a, b, sp = k
        try:
            r = subprocess.run([core.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{a:.3f}", "-t",
                                f"{max(0.05, b - a):.3f}", "-i", path, "-vn", "-af", _atempo(sp), "-f", "s16le",
                                "-ac", "2", "-ar", str(RATE), "-"], capture_output=True, creationflags=core.NOWIN,
                               timeout=300)
            self.stems[k] = np.frombuffer(r.stdout, np.int16).reshape(-1, 2)
        except Exception:
            self.stems[k] = np.zeros((0, 2), np.int16)
        self.pending.discard(k)

    def mix(self, i0, n):
        proj = self.page.proj
        out = np.zeros((n, 2), np.float32)
        t0, t1 = i0 / RATE, (i0 + n) / RATE
        xin, xout = {}, {}
        vs = proj.videos()
        for k_, a in enumerate(vs[:-1]):                       # Übergänge: weich überblenden
            b = vs[k_ + 1]
            ov = a.end - b.start
            if a.trans != "none" and ov > 0.05:
                xout[a.id], xin[b.id] = ov, ov
        for it in proj.items:
            if it.kind == "box" or it.end <= t0 or it.start >= t1:
                continue
            g = it.vol
            if g <= 0.0001:
                continue
            st = self.stems.get(self.key(it))
            if st is None or not len(st):
                continue
            a = max(t0, it.start)
            b = min(t1, it.end)
            ia = int(round((a - it.start) * RATE))
            ib = min(len(st), ia + int(round((b - a) * RATE)))
            if ib <= ia:
                continue
            seg = st[ia:ib].astype(np.float32)
            tl_ = (ia + np.arange(len(seg), dtype=np.float32)) / RATE        # Zeit im Clip
            env = np.full(len(seg), g, np.float32)
            d = it.dur
            for fi in (it.fade_in, xin.get(it.id, 0.0)):
                if fi > 0.01:
                    env *= np.clip(tl_ / fi, 0.0, 1.0)
            for fo in (it.fade_out, xout.get(it.id, 0.0)):
                if fo > 0.01:
                    env *= np.clip((d - tl_) / fo, 0.0, 1.0)
            oa = int(round((a - t0) * RATE))
            out[oa:oa + len(seg)] += seg * env[:, None]
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


class MontagePlayer(threading.Thread):
    def __init__(self, page):
        super().__init__(daemon=True)
        self.page = page
        self.src = FrameSource()
        self.lock = threading.Lock()
        self.ev = threading.Event()
        self.want = None
        self.playing = False
        self.t = 0.0
        self.t0 = self.p0 = 0.0
        self.size = (960, 540)
        self.latest = None
        self.want_src = None
        self.tcache = {}
        self.streams = {}
        self.stop_flag = False

    def seek(self, t):
        with self.lock:
            self.want = max(0.0, t)
        self.ev.set()

    def show_src(self, path, t):
        """Beim Kürzen: genau das Bild an der Schnittkante zeigen (aus dem Vorrat sofort, sonst gesucht)."""
        fr = self.cached(path, t)
        if fr is not None:
            with self.lock:
                self.latest = (None, cv2.cvtColor(_fit(fr, *self.size), cv2.COLOR_BGR2RGB))
            return
        with self.lock:
            self.want_src = (path, t)
        self.ev.set()
        if abs(t - getattr(self, "pf_center", -99)) > 4.0:      # weit gezogen: neuen Vorrat um die Kante laden
            self.prefetch(path, t)

    def cached(self, path, t):
        c = self.tcache.get(path)
        if not c:
            return None
        ts, frs = c
        if not ts:
            return None
        i = int(np.searchsorted(ts, t))
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(ts) and (best is None or abs(ts[j] - t) < abs(ts[best] - t)):
                best = j
        if best is not None and abs(ts[best] - t) < 0.08:
            return frs[best]
        return None

    def prefetch(self, path, center, span=7.0):
        """Beim Start des Kürzens: Bilder rund um die Schnittkante im Hintergrund vorladen (dann ist Ziehen live)."""
        gen = self.pf_gen = getattr(self, "pf_gen", 0) + 1
        self.pf_center = center
        ts, frs = [], []
        self.tcache = {path: (ts, frs)}
        W, H = self.size

        def work():
            cap = cv2.VideoCapture(path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
            t0 = max(0.0, center - span)
            cap.set(cv2.CAP_PROP_POS_MSEC, t0 * 1000)
            step = max(1, int(round(fps / 30)))
            n = 0
            while gen == self.pf_gen:
                ok = cap.grab()
                if not ok:
                    break
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                if t > center + span:
                    break
                if n % step == 0:
                    ok, fr = cap.retrieve()
                    if ok:
                        h, w = fr.shape[:2]
                        s = min(W / w, H / h, 1.0)
                        fr = cv2.resize(fr, (max(2, int(w * s)), max(2, int(h * s))), interpolation=cv2.INTER_AREA)
                        k = int(np.searchsorted(ts, t))
                        ts.insert(k, t)
                        frs.insert(k, fr)
                n += 1
            cap.release()
        threading.Thread(target=work, daemon=True).start()

    def play(self, on):
        self.started = not on
        if on:
            try:
                self._streams_for(self.t)            # Streams schon vor dem ersten Bild starten
            except Exception:
                pass
            self._warm_until = time.perf_counter() + 0.6
        with self.lock:
            self.playing = on
            self.t0, self.p0 = time.perf_counter(), self.t
        self.ev.set()

    def sync_clock(self, t_audio):
        """Der Ton gibt den Takt vor: Bild läuft exakt zum Ton (nur sanft nachstellen)."""
        with self.lock:
            if not self.playing:
                return
            now = time.perf_counter()
            cur = self.p0 + (now - self.t0)
            if abs(cur - t_audio) > 0.03:
                self.t0, self.p0 = now, t_audio

    def _streams_for(self, t):
        """Sorgt dafür, dass für alle gerade (und gleich) sichtbaren Clips ein Stream läuft."""
        proj = self.page.proj
        W, H = self.size
        need = {}
        for it in proj.videos() + proj.overlays():
            if it.start - 1.5 <= t < it.end:                # sichtbar oder beginnt in den nächsten 1,5 s
                need[it.id] = it
        for sid in list(self.streams):
            s = self.streams[sid]
            it = need.get(sid)
            sig = None if it is None else (it.id, it.path, round(it.src_in, 3), round(it.src_out, 3),
                                           round(it.speed, 3), round(it.start, 3), W, H)
            # neu starten, wenn der Clip geändert wurde oder wir vor den Anfang des Streams zurückgesprungen sind
            if it is None or s.sig != sig or max(t, it.start) < s.t0 - 0.02 or \
                    (s.stop_flag and s.q.empty() and s.idx < 0 and t >= it.start):
                s.close()
                del self.streams[sid]
        for sid, it in need.items():
            if sid not in self.streams:
                self.streams[sid] = ClipStream(it, max(t, it.start), W, H)

    def stop_streams(self):
        for s in self.streams.values():
            s.close()
        self.streams = {}

    def take(self):
        with self.lock:
            f, self.latest = self.latest, None
        return f

    def frame_at(self, t, live=False):
        proj = self.page.proj
        W, H = self.size

        def raw(it):
            if live and it.id in self.streams:
                fr = self.streams[it.id].frame(t)
                if fr is not None and fr.shape[1] == W and fr.shape[0] == H:
                    return fr
            fr = self.src.get(it.id, it.path, it.src_in + (t - it.start) * it.speed)
            return None if fr is None else _fit(fr, W, H)
        top = [o for o in sorted(proj.overlays(), key=lambda x: x.track) if o.start - 1e-6 <= t < o.end]
        if top:                                      # weitere Video-Spur deckt alles darunter ab
            fr = raw(top[0])
            if fr is not None:
                return fr
        vs = proj.videos()
        act = [v for v in vs if v.start - 1e-6 <= t < v.end]
        if not act:
            return np.zeros((H, W, 3), np.uint8)

        def img(it):
            fr = raw(it)
            if fr is None:
                return np.zeros((H, W, 3), np.uint8)
            k = 1.0                                      # Ein-/Ausblenden (aus/zu Schwarz)
            if it.fade_in > 0.01 and t - it.start < it.fade_in:
                k = min(k, (t - it.start) / it.fade_in)
            if it.fade_out > 0.01 and it.end - t < it.fade_out:
                k = min(k, (it.end - t) / it.fade_out)
            if k < 0.999:
                fr = cv2.convertScaleAbs(fr, alpha=max(0.0, k))
            return fr
        if len(act) >= 2:
            a, b = act[0], act[1]
            if a.trans != "none":
                ov = a.end - b.start
                p = (t - b.start) / max(0.05, ov)
                return blend(img(a), img(b), p, a.trans)
            return img(b)
        return img(act[0])

    def run(self):
        while not self.stop_flag:
            with self.lock:
                want, self.want = self.want, None
                wsrc, self.want_src = self.want_src, None
                playing = self.playing
            if wsrc is not None:
                try:
                    fr = self.src.get("trim:" + wsrc[0], wsrc[0], wsrc[1])
                    if fr is not None:
                        with self.lock:
                            self.latest = (None, cv2.cvtColor(_fit(fr, *self.size), cv2.COLOR_BGR2RGB))
                except Exception:
                    pass
            if want is not None:
                self.t = want
                with self.lock:
                    self.t0, self.p0 = time.perf_counter(), self.t
                try:
                    fr = self.frame_at(self.t)
                    with self.lock:
                        self.latest = (self.t, cv2.cvtColor(fr, cv2.COLOR_BGR2RGB) if fr is not None else None)
                except Exception:
                    pass
            if playing:
                t = self.p0 + (time.perf_counter() - self.t0)
                L = self.page.proj.length()
                if t >= L:
                    with self.lock:
                        self.playing = False
                    self.t = L
                    with self.lock:
                        self.latest = (L, None)
                    continue
                self.t = t
                tick = time.perf_counter()
                # Start: warten, bis die Streams das erste Bild haben (max. 0,6 s) - Bild & Ton starten gemeinsam
                if getattr(self, "_warm_until", 0) > tick:
                    self._streams_for(t)
                    act = [it for it in self.page.proj.videos() + self.page.proj.overlays() if it.start <= t < it.end]
                    if any(it.id in self.streams and self.streams[it.id].q.empty() and self.streams[it.id].idx < 0
                           for it in act):
                        with self.lock:
                            self.t0, self.p0 = time.perf_counter(), self.t if False else self.p0
                        time.sleep(0.005)
                        continue
                    self._warm_until = 0
                    with self.lock:
                        self.t0 = time.perf_counter()
                    self.started = True
                    t = self.p0
                try:
                    self._streams_for(t)
                    fr = self.frame_at(t, live=True)
                    fr = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB) if fr is not None else None
                except Exception:
                    fr = None
                with self.lock:
                    self.latest = (t, fr)
                time.sleep(max(0.0, 1 / 60 - (time.perf_counter() - tick)))   # gleichmäßig 60 Bilder/s
            else:
                if self.streams:
                    self.stop_streams()
                if want is None:
                    self.ev.wait(0.05)
                    self.ev.clear()
        self.stop_streams()
        self.src.close()


# ------------------------------------------------------------------ Vorschau-Fläche
class ScrubBar(QWidget):
    """Normale Spul-Leiste: Punkt anklicken/ziehen oder irgendwo auf die Linie klicken."""
    begin = Signal()
    moved = Signal(float)
    end = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.val = 0.0
        self.length = 0.0
        self.drag = False
        self.hov = False
        self.setFixedHeight(20)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_pos(self, t, length):
        if self.drag:
            return
        if abs(t - self.val) > 1e-3 or abs(length - self.length) > 1e-3:
            self.val, self.length = t, length
            self.update()

    def _x0x1(self):
        return 8.0, self.width() - 8.0

    def _t_at(self, x):
        x0, x1 = self._x0x1()
        k = max(0.0, min(1.0, (x - x0) / max(1.0, x1 - x0)))
        return k * self.length

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self.length <= 0:
            return
        self.drag = True
        self.begin.emit()
        self.val = self._t_at(e.position().x())
        self.moved.emit(self.val)
        self.update()

    def mouseMoveEvent(self, e):
        if self.drag:
            self.val = self._t_at(e.position().x())
            self.moved.emit(self.val)
            self.update()

    def mouseReleaseEvent(self, e):
        if self.drag:
            self.drag = False
            self.end.emit()
            self.update()

    def enterEvent(self, e):
        self.hov = True
        self.update()

    def leaveEvent(self, e):
        self.hov = False
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        x0, x1 = self._x0x1()
        cy = self.height() / 2.0
        th = 5.0 if (self.hov or self.drag) else 4.0
        k = (self.val / self.length) if self.length > 0 else 0.0
        k = max(0.0, min(1.0, k))
        xk = x0 + k * (x1 - x0)
        p.setPen(Qt.NoPen)
        p.setBrush(T.ink(40))
        p.drawRoundedRect(QRectF(x0, cy - th / 2, x1 - x0, th), th / 2, th / 2)
        fill, _ = T.pill()
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(x0, cy - th / 2, xk - x0, th), th / 2, th / 2)
        r = 7.5 if (self.hov or self.drag) else 6.5
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawEllipse(QPointF(xk, cy + 1), r, r)
        p.setBrush(fill)
        p.drawEllipse(QPointF(xk, cy), r, r)
        p.end()


class Preview(QWidget):
    box_picked = Signal(object)
    box_changed = Signal()
    box_begin = Signal()
    dbl = Signal()

    def mouseDoubleClickEvent(self, e):
        self.dbl.emit()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.img = None
        self.boxes = []          # sichtbare Vierecke (MItem)
        self.sel = None
        self.drag = None
        self.aspect = 16 / 9
        self.setMinimumSize(320, 180)
        self.setMouseTracking(True)

    def vrect(self):
        """Bereich, in dem das Video gezeigt wird (für die Lage der Vierecke)."""
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        a = (self.img.width() / max(1, self.img.height())) if self.img is not None else self.aspect
        w = min(r.width(), r.height() * a)
        h = w / a
        return QRectF(r.center().x() - w / 2, r.center().y() - h / 2, w, h)

    def box_rect(self, b):
        v = self.vrect()
        return QRectF(v.left() + b.bx * v.width(), v.top() + b.by * v.height(), b.bw * v.width(), b.bh * v.height())

    def _hit(self, pos):
        for b in reversed(self.boxes):
            r = self.box_rect(b)
            if QRectF(r.right() - 14, r.bottom() - 14, 20, 20).contains(pos):
                return b, "size"
            if r.contains(pos):
                return b, "move"
        return None, None

    def mousePressEvent(self, e):
        b, mode = self._hit(e.position())
        if b is None:
            return
        self.sel = b
        self.box_picked.emit(b)
        self.box_begin.emit()
        self.drag = (mode, b, e.position(), b.bx, b.by, b.bw, b.bh)
        self.update()

    def mouseMoveEvent(self, e):
        if not self.drag:
            b, mode = self._hit(e.position())
            self.setCursor(Qt.SizeFDiagCursor if mode == "size" else Qt.SizeAllCursor if mode else Qt.ArrowCursor)
            return
        mode, b, p0, bx, by, bw, bh = self.drag
        v = self.vrect()
        dx = (e.position().x() - p0.x()) / max(1.0, v.width())
        dy = (e.position().y() - p0.y()) / max(1.0, v.height())
        if mode == "move":
            b.bx = min(max(-bw + 0.02, bx + dx), 0.98)
            b.by = min(max(-bh + 0.02, by + dy), 0.98)
        else:
            b.bw = min(max(0.03, bw + dx), 1.2)
            b.bh = min(max(0.03, bh + dy), 1.2)
        self.update()
        self.box_changed.emit()

    def mouseReleaseEvent(self, e):
        self.drag = None

    def set_frame(self, rgb):
        h, w = rgb.shape[:2]
        self._buf = rgb
        self.img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 18, 18)
        p.setClipPath(path)
        p.fillRect(r, QColor(0, 0, 0))
        if self.img is not None:
            iw, ih = self.img.width(), self.img.height()
            s = min(r.width() / iw, r.height() / ih)
            dw, dh = iw * s, ih * s
            p.drawImage(QRectF(r.center().x() - dw / 2, r.center().y() - dh / 2, dw, dh), self.img)
        else:
            p.setPen(QColor(255, 255, 255, 150))
            p.setFont(app_font(10.5))
            p.drawText(r, Qt.AlignCenter, "Füge links Clips hinzu – sie erscheinen unten in der Zeitleiste.")
        # Vierecke darüber (so wie sie im Export aussehen)
        v = self.vrect()
        p.setClipRect(v)
        for b in self.boxes:
            br = self.box_rect(b)
            c = QColor(b.color)
            c.setAlphaF(max(0.0, min(1.0, b.alpha)))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRect(br)
            if b.text.strip():
                tc = QColor(0, 0, 0) if QColor(b.color).lightness() > 150 and b.alpha > 0.3 else QColor(255, 255, 255)
                f = display_font(10, QFont.Bold)
                f.setPixelSize(box_font_px(b, v.width(), v.height()))
                p.setFont(f)
                p.setPen(tc)
                p.drawText(br, Qt.AlignCenter, b.text)
            if b is self.sel:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(255, 255, 255), 1.5, Qt.DashLine))
                p.drawRect(br)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255))
                p.drawEllipse(QPointF(br.right(), br.bottom()), 6, 6)
        p.setClipping(False)
        p.setPen(QPen(T.line(1.6), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 18, 18)


# ------------------------------------------------------------------ Zeitleiste
class MTimeline(QWidget):
    seek = Signal(float)
    selected = Signal(object)        # MItem, ("trans", MItem) oder None
    changed = Signal()
    begin = Signal()                 # vor einer Änderung (Rückgängig)
    trimming = Signal(str, float)    # beim Kürzen: Datei + Zeit der Schnittkante (für die Vorschau)
    trim_begin = Signal(str, float)  # Kürzen beginnt: Bilder rund um die Kante vorladen
    add_lane = Signal(str)           # "video" / "audio"
    del_lane = Signal(int)
    dropped = Signal(list, float, object)     # Pfade, Zeit, Spur (oder None)
    magnet_pause = Signal(bool)               # Gruppe wird verschoben: Einrasten kurz aus

    RULER, VH, AH, GAP, LEFT, ADD = 30, 58, 42, 10, 96, 30

    def __init__(self, proj, parent=None):
        super().__init__(parent)
        self.proj = proj
        self.pps = 40.0              # Pixel pro Sekunde
        self.x0 = 0.0                # Scroll (Sekunden)
        self.ph = 0.0                # Abspielposition
        self.sel = None
        self.lane_hover = None
        self._lane_x = {}
        self.multi = []              # per Rahmen ausgewählte Stücke
        self.band = None             # Auswahl-Rahmen (QRectF) beim Ziehen
        self.drag = None
        self.hover = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setAcceptDrops(True)
        self.drop_at = None
        self._last = None
        self._as = QTimer(self)                  # Ansicht läuft mit, wenn man beim Ziehen an den Rand kommt
        self._as.setInterval(16)
        self._as.timeout.connect(self._autoscroll)
        self.relayout()

    # ---- Ansicht verschieben (nur Ansicht, nichts am Projekt)
    def _edge_speed(self, x):
        edge = 60.0
        lo, hi = self.LEFT + edge, self.width() - 8 - edge
        if x < lo:
            v = -(lo - x)
        elif x > hi:
            v = x - hi
        else:
            return 0.0
        return max(-60.0, min(60.0, v * 0.35))          # Pixel pro Takt, je näher am Rand desto schneller

    def _autoscroll(self):
        if self._last is None or (not self.drag and self.drop_at is None) or (self.drag and self.drag[0] == "sbar"):
            self._as.stop()
            return
        pos = self._last
        v = self._edge_speed(pos.x())
        if v == 0.0:
            return
        nx = max(0.0, self.x0 + v / self.pps)
        if abs(nx - self.x0) < 1e-6:
            return
        self.x0 = nx
        if self.drag:
            class _Ev:
                def __init__(s_, p_):
                    s_.p_ = p_

                def position(s_):
                    return s_.p_
            self.mouseMoveEvent(_Ev(pos))
        else:
            self.drop_at = (max(0.0, self.xt(pos.x())), self.drop_at[1])
        self.update()

    def scroll_total(self):
        vis = (self.width() - self.LEFT - 8) / self.pps
        return max(self.proj.length() + vis * 0.3, self.x0 + vis, vis), vis

    def sbar_rect(self):
        y = self.height() - self.ADD - 4
        return QRectF(190, y + self.ADD / 2 - 6, max(40, self.width() - 198), 12)

    def sbar_thumb(self):
        r = self.sbar_rect()
        total, vis = self.scroll_total()
        w = max(36.0, r.width() * min(1.0, vis / total))
        x = r.left() + (r.width() - w) * (self.x0 / max(1e-6, total - vis) if total > vis else 0.0)
        return QRectF(x, r.top(), w, r.height())

    @staticmethod
    def _paths(md):
        if md.hasUrls():
            ps = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        elif md.hasText() and md.text().startswith("acemedia:"):
            ps = [md.text()[9:]]
        else:
            ps = []
        return [p for p in ps if p.lower().endswith(VIDEO_EXT + AUDIO_EXT)]

    def dragEnterEvent(self, e):
        if self._paths(e.mimeData()):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if self._paths(e.mimeData()):
            e.acceptProposedAction()
            self._last = e.position()
            if not self._as.isActive():
                self._as.start()
            self.drop_at = (max(0.0, self.xt(e.position().x())), self.track_at(e.position().y()))
            self.update()

    def dragLeaveEvent(self, e):
        self.drop_at = None
        self.update()

    def dropEvent(self, e):
        ps = self._paths(e.mimeData())
        self.drop_at = None
        if ps:
            e.acceptProposedAction()
            self.dropped.emit(ps, max(0.0, self.xt(e.position().x())), self.track_at(e.position().y()))
        self.update()

    def has_audio(self, it):
        if it.track > 0:
            return True
        inf = _INFO.get(it.path)
        return bool(inf and inf.get("audio"))

    def mute_rect(self, it):
        r = self.item_rect(it)
        if r.width() < 64:
            return None
        return QRectF(r.right() - 40, r.top() + 4, 20, 20)       # Abstand zum Griff zum Kürzen

    def relayout(self):
        h = self.RULER + self.GAP
        for tr in self.proj.lanes():
            h += (self.VH if tr <= 0 else self.AH) + self.GAP
        self.setFixedHeight(int(h + self.ADD + 8))
        self.update()

    # Geometrie
    def tx(self, t):
        return self.LEFT + (t - self.x0) * self.pps

    def xt(self, x):
        return self.x0 + (x - self.LEFT) / self.pps

    def track_rect(self, tr):
        y = self.RULER + self.GAP
        for t_ in self.proj.lanes():
            hh = self.VH if t_ <= 0 else self.AH
            if t_ == tr:
                return QRectF(self.LEFT, y, self.width() - self.LEFT - 8, hh)
            y += hh + self.GAP
        return QRectF(self.LEFT, y, self.width() - self.LEFT - 8, self.AH)

    def add_rects(self):
        y = self.height() - self.ADD - 4
        return (QRectF(8, y, 84, self.ADD), QRectF(100, y, 74, self.ADD))

    def item_rect(self, it):
        tr = self.track_rect(it.track)
        return QRectF(self.tx(it.start), tr.top() + 3, max(6.0, it.dur * self.pps), tr.height() - 6)

    def track_at(self, y):
        for tr in self.proj.lanes():
            r = self.track_rect(tr)
            if r.top() - self.GAP / 2 <= y <= r.bottom() + self.GAP / 2:
                return tr
        return None

    def trans_markers(self):
        """Rauten an den Stellen, wo zwei Clips aneinanderstoßen (hier sitzt der Übergang)."""
        out = []
        vs = self.proj.videos()
        r0 = self.track_rect(0)
        cy = r0.top() - 1                                  # Raute sitzt oben auf der Kante (stört das Kürzen nicht)
        for a, b in zip(vs, vs[1:]):
            if b.start <= a.end + 0.06:
                t = (b.start + a.end) / 2 if b.start < a.end else b.start
                x = self.tx(t)
                out.append((QRectF(x - 10, cy - 10, 20, 20), a))
        return out

    def fit_all(self):
        L = max(10.0, self.proj.length() + 2)
        self.pps = max(4.0, (self.width() - self.LEFT - 30) / L)
        self.x0 = 0.0
        self.update()

    # Zeichnen
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        W = self.width()
        # Spur-Namen
        names = []
        for tr in self.proj.lanes():
            if tr == 0:
                names.append((tr, "Video", "film"))
            elif tr < 0:
                names.append((tr, f"Video {1 - tr}", "film"))
            else:
                names.append((tr, f"Ton {tr}", "music"))
        for rr, txt in zip(self.add_rects(), ("Video", "Ton")):
            p.setPen(QPen(T.glass_edge(1.0), 1))
            p.setBrush(T.card_fill(120))
            p.drawRoundedRect(rr, rr.height() / 2, rr.height() / 2)
            paint_icon(p, "plus", QRectF(rr.left() + 8, rr.center().y() - 6, 12, 12), ink(200), 1.6)
            p.setPen(ink(200))
            p.setFont(app_font(8.5, True))
            p.drawText(rr.adjusted(24, 0, -4, 0), Qt.AlignVCenter, txt)
        sb = self.sbar_rect()                                   # Scroll-Leiste (Ansicht verschieben)
        total, vis = self.scroll_total()
        if total > vis + 0.01:
            p.setPen(Qt.NoPen)
            p.setBrush(ink(16))
            p.drawRoundedRect(sb, sb.height() / 2, sb.height() / 2)
            th = self.sbar_thumb()
            act = bool(self.drag and self.drag[0] == "sbar")
            p.setBrush(ink(120 if act else 70))
            p.drawRoundedRect(th.adjusted(1, 2, -1, -2), (th.height() - 4) / 2, (th.height() - 4) / 2)
        self._lane_x = {}
        for tr, name, ic in names:
            r = self.track_rect(tr)
            if (tr < 0 and True) or (tr > 0 and self.proj.alanes > 1):
                xr = QRectF(self.LEFT - 26, r.top() + 4, 16, 16)
                self._lane_x[tr] = xr
            p.setPen(Qt.NoPen)
            p.setBrush(T.card_fill(130))
            p.drawRoundedRect(QRectF(8, r.top(), self.LEFT - 16, r.height()), 10, 10)
            paint_icon(p, ic, QRectF(16, r.center().y() - 8, 16, 16), ink(170), 1.5)
            p.setPen(ink(170))
            p.setFont(app_font(8.5, True))
            p.drawText(QRectF(36, r.top(), self.LEFT - 44, r.height()), Qt.AlignVCenter, name)
            p.setPen(Qt.NoPen)
            p.setBrush(ink(10))
            p.drawRoundedRect(r, 10, 10)
            if tr in self._lane_x:                                 # Spur löschen
                xr = self._lane_x[tr]
                p.setBrush(QColor(230, 60, 75, 200) if self.lane_hover == tr else ink(30))
                p.drawEllipse(xr)
                paint_icon(p, "close", xr.adjusted(3, 3, -3, -3), QColor(255, 255, 255), 1.5)
        p.save()
        p.setClipRect(QRectF(self.LEFT, 0, W - self.LEFT, self.height()))
        # Lineal
        step = next((s for s in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if s * self.pps >= 70), 600)
        t = math.floor(self.x0 / step) * step
        p.setFont(app_font(8))
        while self.tx(t) < W:
            x = self.tx(t)
            p.setPen(ink(60))
            p.drawLine(QPointF(x, self.RULER - 7), QPointF(x, self.RULER))
            p.setPen(ink(140))
            lab = fmt_s(t) if step >= 1 else f"{int(t + 1e-6) // 60}:{(t + 1e-6) % 60:04.1f}"
            p.drawText(QRectF(x + 3, 2, 60, self.RULER - 8), Qt.AlignLeft | Qt.AlignVCenter, lab)
            t += step
        # Stücke
        for it in sorted(self.proj.items, key=lambda x: (x is self.sel or x in self.multi, x.track, x.start)):
            r = self.item_rect(it)
            if r.right() < self.LEFT or r.left() > W:
                continue
            sel = it is self.sel
            path = QPainterPath()
            path.addRoundedRect(r, 8, 8)
            if it.kind == "box":
                c = QColor(it.color)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(44, 44, 52))
                p.drawPath(path)
                sw = QRectF(r.left() + 8, r.center().y() - 9, 18, 18)
                p.setBrush(c)
                p.setPen(QPen(QColor(255, 255, 255, 120), 1))
                p.drawRoundedRect(sw, 4, 4)
                p.setPen(QColor(255, 255, 255, 235))
                p.setFont(app_font(8.5, True))
                t_ = f"Viereck  ·  {it.text}" if it.text.strip() else "Viereck"
                p.drawText(r.adjusted(32, 0, -8, 0), Qt.AlignVCenter,
                           QFontMetrics(p.font()).elidedText(t_, Qt.ElideRight, int(max(10, r.width() - 40))))
            elif it.track <= 0:
                p.save()
                p.setClipPath(path, Qt.IntersectClip)        # nie über die Spur-Namen links malen
                p.fillRect(r, QColor(30, 30, 34))
                pm = thumb_fast(it.path)
                if pm is not None:
                    tw = max(4.0, r.height() * pm.width() / max(1, pm.height()))
                    x = r.left()
                    if x < self.LEFT - tw:                   # nur den sichtbaren Teil kacheln
                        x += math.floor((self.LEFT - tw - x) / tw) * tw
                    xend = min(r.right(), W)
                    while x < xend:
                        p.drawPixmap(QRectF(x, r.top(), tw, r.height()), pm, QRectF(0, 0, pm.width(), pm.height()))
                        x += tw
                g = QLinearGradient(r.topLeft(), r.bottomLeft())
                g.setColorAt(0, QColor(0, 0, 0, 30))
                g.setColorAt(1, QColor(0, 0, 0, 160))
                p.fillRect(r, g)
                p.setPen(QColor(255, 255, 255, 235))
                p.setFont(app_font(8.5, True))
                name = os.path.splitext(os.path.basename(it.path))[0]
                sp = f"  ·  {it.speed:g}×" if abs(it.speed - 1) > 0.01 else ""
                p.drawText(r.adjusted(8, 0, -8, -4), Qt.AlignLeft | Qt.AlignBottom,
                           QFontMetrics(p.font()).elidedText(f"{name}  ·  {it.dur:.1f}s{sp}", Qt.ElideRight,
                                                            int(r.width() - 16)))
                p.restore()
            else:
                col = [T.pill()[0], QColor("#5fb3ff"), QColor("#f2b36f"), QColor("#5fcfae")][(it.track - 1) % 4]
                p.setPen(Qt.NoPen)
                p.setBrush(with_alpha(col, 70))
                p.drawPath(path)
                w = wave_fast(it.path)
                info = media_info(it.path)
                dur = max(0.1, info["dur"])
                if w is None:
                    w = np.zeros(10)
                p.setPen(QPen(with_alpha(col, 220), 1))
                cy, hh = r.center().y(), r.height() / 2 - 4
                # nur den sichtbaren Ausschnitt berechnen (bei starkem Zoom sonst riesig)
                vx0 = max(0, int((self.LEFT - r.left()) // 2 * 2))
                vx1 = int(min(r.width(), W - r.left() + 2))
                key = (it.id, round(self.pps, 3), round(it.src_in, 3), round(it.src_out, 3), round(it.speed, 3),
                       round(it.vol, 2), int(r.height()), len(w), vx0, vx1)
                cache = self.__dict__.setdefault("_wcache", {})
                lines = cache.get(key)
                if lines is None:                            # nur bei Änderung neu berechnen
                    from PySide6.QtCore import QLineF
                    xs = np.arange(vx0, max(vx0, vx1), 2, dtype=np.float64)
                    st = it.src_in + xs / self.pps * it.speed
                    idx = np.clip((st / dur * (len(w) - 1)).astype(int), 0, len(w) - 1)
                    amp = w[idx] * hh * min(1.5, it.vol)
                    lines = [QLineF(float(x), -float(a_), float(x), float(a_)) for x, a_ in zip(xs, amp) if a_ > 0.3]
                    if len(cache) > 200:
                        cache.clear()
                    cache[key] = lines
                p.save()
                p.setClipPath(path, Qt.IntersectClip)
                p.translate(r.left(), cy)
                p.drawLines(lines)
                p.restore()
                p.setPen(QColor(255, 255, 255, 230))
                p.setFont(app_font(8, True))
                p.drawText(r.adjusted(8, 2, -8, 0), Qt.AlignLeft | Qt.AlignTop,
                           QFontMetrics(p.font()).elidedText(os.path.basename(it.path), Qt.ElideRight,
                                                            int(r.width() - 16)))
                for fdur, left in ((it.fade_in, True), (it.fade_out, False)):
                    if fdur > 0.01:
                        fw = min(r.width() / 2, fdur * self.pps)
                        tri = QPainterPath()
                        if left:
                            tri.moveTo(r.bottomLeft())
                            tri.lineTo(r.topLeft())
                            tri.lineTo(QPointF(r.left() + fw, r.top()))
                        else:
                            tri.moveTo(r.bottomRight())
                            tri.lineTo(r.topRight())
                            tri.lineTo(QPointF(r.right() - fw, r.top()))
                        tri.closeSubpath()
                        p.setPen(Qt.NoPen)
                        p.setBrush(QColor(0, 0, 0, 90))
                        p.drawPath(tri)
            ms = self.mute_rect(it)
            if ms is not None and self.has_audio(it):          # Lautsprecher: Klick = stumm / Ton an
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 150) if not it.muted else QColor(230, 60, 75, 220))
                p.drawEllipse(ms)
                paint_icon(p, "mute" if it.muted else "speaker", ms.adjusted(3, 3, -3, -3), QColor(255, 255, 255), 1.5)
            insel = it in self.multi
            p.setBrush(with_alpha(QColor(255, 255, 255), 40) if insel else Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 240) if (sel or insel) else with_alpha(T.glass_edge(1.0), 255),
                          2 if (sel or insel) else 1))
            p.drawRoundedRect(r, 8, 8)
            if sel or it is self.hover:                          # breite Griffe zum Kürzen
                for left in (True, False):
                    hr = QRectF(r.left(), r.top(), 12, r.height()) if left else QRectF(r.right() - 12, r.top(), 12, r.height())
                    hp = QPainterPath()
                    hp.addRoundedRect(hr, 6, 6)
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(255, 255, 255, 235 if sel else 140))
                    p.drawPath(hp)
                    p.setPen(QPen(QColor(20, 20, 24), 1.4))
                    cx = hr.center().x()
                    p.drawLine(QPointF(cx, hr.center().y() - 6), QPointF(cx, hr.center().y() + 6))
        if self.drag and self.drag[0] in ("left", "right"):     # Länge beim Kürzen anzeigen
            it = self.drag[1]
            r = self.item_rect(it)
            txt = f"{it.dur:.2f} s"
            p.setFont(app_font(8.5, True))
            tw = QFontMetrics(p.font()).horizontalAdvance(txt) + 16
            x = (r.left() if self.drag[0] == "left" else r.right()) - tw / 2
            tr_ = QRectF(x, r.top() - 24, tw, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255))
            p.drawRoundedRect(tr_, 10, 10)
            p.setPen(QColor(17, 17, 17))
            p.drawText(tr_, Qt.AlignCenter, txt)
        # Übergänge (Rauten zwischen den Clips)
        for rr, a in self.trans_markers():
            on = a.trans != "none"
            selt = isinstance(self.sel, tuple) and self.sel[1] is a
            p.save()
            p.translate(rr.center())
            p.rotate(45)
            q = QRectF(-8, -8, 16, 16)
            p.setPen(QPen(QColor(255, 255, 255, 230 if selt else 160), 1.5))
            p.setBrush(T.pill()[0] if on else QColor(20, 20, 24))
            p.drawRoundedRect(q, 3, 3)
            p.restore()
            if not on:
                p.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
                p.drawLine(QPointF(rr.center().x() - 4, rr.center().y()), QPointF(rr.center().x() + 4, rr.center().y()))
                p.drawLine(QPointF(rr.center().x(), rr.center().y() - 4), QPointF(rr.center().x(), rr.center().y() + 4))
        if self.drop_at is not None:                           # Ablage-Stelle beim Hineinziehen
            x = self.tx(self.drop_at[0])
            tr_ = self.drop_at[1]
            rr = self.track_rect(tr_) if tr_ is not None else QRectF(self.LEFT, self.RULER, 0, self.height() - self.RULER)
            p.setPen(QPen(T.pill()[0], 3))
            p.drawLine(QPointF(x, rr.top()), QPointF(x, rr.bottom()))
        if self.band is not None:                              # Auswahl-Rahmen
            p.setPen(QPen(QColor(255, 255, 255, 220), 1.2, Qt.DashLine))
            p.setBrush(QColor(255, 255, 255, 28))
            p.drawRoundedRect(self.band, 6, 6)
        # Abspielkopf
        x = self.tx(self.ph)
        p.setPen(QPen(QColor(255, 255, 255), 1.6))
        p.drawLine(QPointF(x, 6), QPointF(x, self.track_rect(self.proj.lanes()[-1]).bottom() + 2))
        tri = QPainterPath()
        tri.moveTo(x - 6, 4)
        tri.lineTo(x + 6, 4)
        tri.lineTo(x, 12)
        tri.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255))
        p.drawPath(tri)
        p.restore()

    # Maus
    def hit(self, pos):
        # ausgewähltes Stück zuerst prüfen (liegt oben, auch wenn es sich mit einem anderen überschneidet)
        sel = self.sel if isinstance(self.sel, MItem) else None
        if sel is not None:
            r = self.item_rect(sel)
            if r.adjusted(-6, 0, 6, 0).contains(pos):
                edge = min(14.0, r.width() / 3)
                if pos.x() - r.left() < edge:
                    return ("left", sel, None)
                if r.right() - pos.x() < edge:
                    return ("right", sel, None)
        for it in self.proj.items:
            ms = self.mute_rect(it)
            if ms is not None and self.has_audio(it) and ms.adjusted(-2, -2, 2, 2).contains(pos):
                return ("mute", it, None)
        for rr, a in self.trans_markers():
            if rr.adjusted(-3, -3, 3, 3).contains(pos):
                return ("trans", a, None)
        for it in sorted(self.proj.items, key=lambda x: -x.start):
            r = self.item_rect(it)
            if r.adjusted(-6, 0, 6, 0).contains(pos):
                edge = min(14.0, r.width() / 3)
                if pos.x() - r.left() < edge:
                    return ("left", it, None)
                if r.right() - pos.x() < edge:
                    return ("right", it, None)
                if r.contains(pos):
                    return ("move", it, None)
        return None

    def snap_points(self, skip):
        pts = [0.0, self.ph]
        for it in self.proj.items:
            if it is not skip:
                pts += [it.start, it.end]
        return pts

    def snap(self, t, skip):
        if not self.proj.magnet:
            return t
        best, bd = t, 10 / self.pps
        for s in self.snap_points(skip):
            if abs(s - t) < bd:
                best, bd = s, abs(s - t)
        return best

    def mousePressEvent(self, e):
        pos = e.position()
        self.setFocus()
        for rr, kind in zip(self.add_rects(), ("video", "audio")):
            if rr.contains(pos):
                self.add_lane.emit(kind)
                return
        for tr, xr in self._lane_x.items():
            if xr.adjusted(-3, -3, 3, 3).contains(pos):
                self.del_lane.emit(tr)
                return
        sb = self.sbar_rect()
        if sb.adjusted(0, -6, 0, 6).contains(pos):            # Scroll-Leiste
            th = self.sbar_thumb()
            if not th.contains(pos):                          # neben den Griff geklickt: dorthin springen
                total, vis = self.scroll_total()
                k = (pos.x() - sb.left() - th.width() / 2) / max(1.0, sb.width() - th.width())
                self.x0 = max(0.0, min(1.0, k)) * max(0.0, total - vis)
                th = self.sbar_thumb()
            self.drag = ("sbar", pos.x(), self.x0, self.scroll_total())
            self.update()
            return
        if pos.y() < self.RULER or pos.x() < self.LEFT:
            if pos.x() >= self.LEFT:
                self.drag = ("seek",)
                self.seek.emit(max(0.0, self.xt(pos.x())))
            return
        h = self.hit(pos)
        if h is None:
            # leere Fläche: Klick = Abspielposition, Ziehen = Rahmen über mehrere Clips
            self.drag = ("band", pos, self.xt(pos.x()))
            self.update()
            return
        kind, it, _ = h
        if kind == "mute":
            self.begin.emit()
            group = self.multi if it in self.multi else [it]
            new = not it.muted
            for g in group:
                g.muted = new
            self.changed.emit()
            self.selected.emit(self.sel if self.sel is not None else (("multi", self.multi) if self.multi else None))
            self.update()
            return
        if it not in self.multi:
            self.multi = []
        if kind == "trans":
            self.sel = ("trans", it)
            self.selected.emit(self.sel)
            self.update()
            return
        self.sel = it
        self.selected.emit(it)
        self.begin.emit()
        self.drag = (kind, it, self.xt(pos.x()), it.start, it.src_in, it.src_out, it.track)
        if kind in ("left", "right") and it.kind != "box":
            self.trim_begin.emit(it.path, it.src_in if kind == "left" else it.src_out)
        self.update()

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self.drag:
            lh = next((tr for tr, xr in self._lane_x.items() if xr.adjusted(-3, -3, 3, 3).contains(pos)), None)
            if lh != self.lane_hover:
                self.lane_hover = lh
                self.update()
            h = self.hit(pos)
            self.setCursor(Qt.SizeHorCursor if h and h[0] in ("left", "right") else
                           Qt.PointingHandCursor if h or any(r.contains(pos) for r in self.add_rects()) else
                           Qt.ArrowCursor)
            hv = h[1] if h and h[0] != "trans" else None
            if hv is not self.hover:
                self.hover = hv
                self.update()
            return
        if self.drag[0] == "sbar":
            _, px, x00, (total, vis) = self.drag
            sb = self.sbar_rect()
            th = self.sbar_thumb()
            self.x0 = max(0.0, x00 + (pos.x() - px) / max(1.0, sb.width() - th.width()) * max(0.0, total - vis))
            self.update()
            return
        if not isinstance(e, QWidget) and hasattr(e, "buttons"):
            self._last = pos
            if not self._as.isActive():
                self._as.start()
        if self.drag[0] == "seek":
            self.seek.emit(max(0.0, self.xt(pos.x())))
            return
        if self.drag[0] == "band":
            p0 = QPointF(self.tx(self.drag[2]), self.drag[1].y())      # Startpunkt wandert mit der Ansicht
            if (pos - p0).manhattanLength() > 6 or self.band is not None:
                self.band = QRectF(p0, pos).normalized()
                self.multi = [i for i in self.proj.items if self.item_rect(i).intersects(self.band)]
                self.update()
            return
        kind, it, t_press, st0, in0, out0, tr0 = self.drag
        dt = self.xt(pos.x()) - t_press
        info = {"dur": 3600.0} if it.kind == "box" else media_info(it.path)
        if kind == "move" and len(self.multi) > 1 and it in self.multi:
            # mehrere ausgewählte Stücke zusammen verschieben (Einrasten ist dabei kurz aus)
            if not hasattr(self, "_grp0"):
                self._grp0 = {g.id: g.start for g in self.multi}
                if self.proj.magnet and any(g.track == 0 for g in self.multi):
                    self._mag_paused = True
                    self.magnet_pause.emit(True)
            mn = min(self._grp0.values())
            d = max(-mn, dt)
            for g in self.multi:
                g.start = self._grp0[g.id] + d
            self.changed.emit()
            self.update()
            return
        if kind == "move":
            tr = self.track_at(pos.y())
            if tr is not None and (tr <= 0) == (it.track <= 0) and tr != it.track and not (it.kind == "box" and tr == 0):
                it.track = tr                          # zwischen Video-Spuren bzw. Ton-Spuren wechseln
                self.proj.pack()
            if it.track == 0 and self.proj.magnet:
                # Einrasten: Reihenfolge per Ziehen ändern
                vs = self.proj.videos()
                t = st0 + dt + it.dur / 2
                others = [v for v in vs if v is not it]
                idx = sum(1 for v in others if v.start + v.dur / 2 < t)
                order = others[:idx] + [it] + others[idx:]
                tmp = 0.0
                for v in order:                       # neue Reihenfolge übernehmen
                    v.start = tmp
                    tmp += v.dur
                self.proj.pack()
            else:
                ns = max(0.0, st0 + dt)
                ns = self.snap(ns, it)
                ne = self.snap(ns + it.dur, it)
                if ne != ns + it.dur:
                    ns = ne - it.dur
                it.start = max(0.0, ns)
        elif kind == "left":
            # von vorne kürzen: die linke Kante folgt der Maus, Einrasten erst beim Loslassen
            ni = min(out0 - 0.2, max(0.0, in0 + dt * it.speed))
            it.start = max(0.0, st0 + (ni - in0) / it.speed)
            it.src_in = ni
            if it.kind != "box":
                self.trimming.emit(it.path, ni)
        elif kind == "right":
            no = max(in0 + 0.2, min(clip_len(it.path, it.track) or out0, out0 + dt * it.speed))
            it.src_out = no
            self.proj.pack()
            if it.kind != "box":
                self.trimming.emit(it.path, max(0.0, no - 0.02))
        self.changed.emit()
        self.update()

    def resolve_overlap(self, it):
        """Freier Modus: nicht auf einen anderen Clip derselben Spur legen - an die nächste freie Stelle rücken."""
        if it.track == 0 and self.proj.magnet:
            return
        others = sorted([o for o in self.proj.items if o is not it and o.track == it.track], key=lambda o: o.start)
        for _ in range(len(others) + 1):
            hit_ = [o for o in others if o.start < it.end - 0.01 and it.start < o.end - 0.01]
            if not hit_:
                return
            o = hit_[0]
            if it.start + it.dur / 2 < o.start + o.dur / 2:
                it.start = max(0.0, o.start - it.dur)
                if any(x.start < it.end - 0.01 and it.start < x.end - 0.01 for x in others):
                    it.start = o.end
            else:
                it.start = o.end

    def mouseReleaseEvent(self, e):
        if self.drag and self.drag[0] in ("move", "left", "right") and isinstance(self.drag[1], MItem):
            for g in (self.multi if self.drag[1] in self.multi else [self.drag[1]]):
                self.resolve_overlap(g)
        if hasattr(self, "_grp0"):
            del self._grp0
        if getattr(self, "_mag_paused", False):           # Einrasten wieder an: Gruppe als Block einsortieren
            self._mag_paused = False
            grp = sorted([g for g in self.multi if g.track == 0], key=lambda g: g.start)
            others = [v for v in self.proj.videos() if v not in grp]
            if grp:
                glen = sum(g.dur for g in grp)
                tc = grp[0].start + glen / 2
                idx = sum(1 for v in others if v.start + v.dur / 2 < tc)
                tmp = 0.0
                for v in others[:idx] + grp + others[idx:]:
                    v.start = tmp
                    tmp += v.dur
            self.proj.pack()
            self.magnet_pause.emit(False)
        if self.drag and self.drag[0] == "band":
            if self.band is None:                         # nur geklickt: Abspielposition setzen
                self.sel = None
                self.multi = []
                self.selected.emit(None)
                self.seek.emit(max(0.0, self.xt(e.position().x())))
            else:
                self.band = None
                self.sel = None
                if len(self.multi) == 1:
                    self.sel = self.multi[0]
                    self.multi = []
                    self.selected.emit(self.sel)
                else:
                    self.selected.emit(("multi", list(self.multi)) if self.multi else None)
            self.drag = None
            self.update()
            return
        self._as.stop()
        if self.drag and self.drag[0] not in ("seek", "sbar"):
            self.proj.pack()
            self.changed.emit()
        self.drag = None
        self.update()

    def wheelEvent(self, e):
        ad = e.angleDelta()
        d = ad.y() or ad.x()
        if abs(ad.x()) > abs(ad.y()):                    # Touchpad seitlich wischen: Ansicht verschieben
            self.x0 = max(0.0, self.x0 - ad.x() / 120 * 80 / self.pps)
        elif e.modifiers() & Qt.ShiftModifier:          # Shift + Mausrad: seitlich scrollen
            self.x0 = max(0.0, self.x0 - d / 120 * 80 / self.pps)
        else:                                             # Mausrad: zoomen
            t = self.xt(e.position().x())
            f = 1.15 if d > 0 else 1 / 1.15
            self.pps = max(2.0, min(400.0, self.pps * f))
            self.x0 = max(0.0, t - (e.position().x() - self.LEFT) / self.pps)
        self.update()

    def scroll_to(self, t):
        vis = (self.width() - self.LEFT - 20) / self.pps
        if t < self.x0 or t > self.x0 + vis:
            self.x0 = max(0.0, t - vis * 0.2)


class ColorDot(QWidget):
    clicked = Signal()

    def __init__(self, color, on, parent=None):
        super().__init__(parent)
        self.c, self.on = QColor(color), on
        self.setFixedSize(28, 28)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        p.setPen(QPen(QColor(255, 255, 255, 80), 1))
        p.setBrush(self.c)
        p.drawEllipse(r)
        if self.on:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(ink(230), 2))
            p.drawEllipse(r.adjusted(-3, -3, 3, 3))


# ------------------------------------------------------------------ Medien-Liste
class MediaRow(QWidget):
    add = Signal()

    def __init__(self, path, is_audio, parent=None):
        super().__init__(parent)
        self.path, self.is_audio = path, is_audio
        self.hov = 0.0
        self.setFixedHeight(58)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Klicken = hinten anfügen · Ziehen = an eine Stelle in der Zeitleiste legen")

    def enterEvent(self, e):
        tween(self, "hov", 1.0, 120)

    def leaveEvent(self, e):
        tween(self, "hov", 0.0, 160)

    def mousePressEvent(self, e):
        self._p0 = e.position()

    def mouseMoveEvent(self, e):
        p0 = getattr(self, "_p0", None)
        if p0 is None or (e.position() - p0).manhattanLength() < 8:
            return
        self._p0 = None
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QDrag
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(self.path)])
        md.setText("acemedia:" + self.path)
        dr = QDrag(self)
        dr.setMimeData(md)
        pm = thumb_fast(self.path) if not self.is_audio else None
        if pm is not None:
            dr.setPixmap(pm.scaled(96, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        dr.exec(Qt.CopyAction)

    def mouseReleaseEvent(self, e):
        if getattr(self, "_p0", None) is not None:
            self._p0 = None
            self.add.emit()                       # einfacher Klick: hinten anfügen

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(2, 3, -2, -3)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, int(14 * self.hov)) if T.dark else QColor(0, 0, 0, int(10 * self.hov)))
        p.drawRoundedRect(r, 12, 12)
        tr = QRectF(r.left() + 6, r.top() + 5, 64, r.height() - 10)
        path = QPainterPath()
        path.addRoundedRect(tr, 7, 7)
        p.save()
        p.setClipPath(path)
        p.fillRect(tr, ink(25))
        if not self.is_audio:
            pm = thumb_fast(self.path)
            if pm is not None:
                p.drawPixmap(tr, pm, QRectF(0, 0, pm.width(), pm.height()))
        else:
            paint_icon(p, "music", QRectF(tr.center().x() - 9, tr.center().y() - 9, 18, 18), ink(180), 1.6)
        p.restore()
        info = info_fast(self.path) or {"dur": 0.0}
        p.setPen(ink(235))
        p.setFont(app_font(9, True))
        avail = int(r.width() - 120)
        p.drawText(QRectF(tr.right() + 10, r.top() + 6, avail, 18), Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(os.path.basename(self.path), Qt.ElideMiddle, avail))
        p.setPen(ink(130))
        p.setFont(app_font(8))
        p.drawText(QRectF(tr.right() + 10, r.top() + 24, avail, 16), Qt.AlignVCenter, fmt_s(info["dur"]))
        c = QRectF(r.right() - 34, r.center().y() - 13, 26, 26)
        p.setPen(Qt.NoPen)
        p.setBrush(with_alpha(T.pill()[0], int(120 + 135 * self.hov)))
        p.drawEllipse(c)
        paint_icon(p, "plus", c.adjusted(7, 7, -7, -7), T.pill()[1], 1.8)


# ------------------------------------------------------------------ Seite
class MontagePage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.proj = Project()
        self.proj.load(win.st.get("montage"))
        self.media = [p for p in win.st.get("montage_media", []) if os.path.exists(p)]
        self.undo_st, self.redo_st = [], []
        self.player = MontagePlayer(self)
        self.player.start()
        self.playing = False
        self.exporting = False
        self.cancel = threading.Event()
        self.audio = None
        self._aud_gen = 0
        self._aud_file = None
        self.mixer = LiveMixer(self)
        if QAudioSink is not None:
            try:
                self.audio = PcmAudio()
                self.audio.source = self.mixer.mix
            except Exception:
                self.audio = None
        _NOTIFY[0] = lambda: self.win.ui(self._loaded)
        self._media_key = None
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self.save_t = QTimer(self)
        self.save_t.setSingleShot(True)
        self.save_t.setInterval(700)
        self.save_t.timeout.connect(self._after_change)
        QTimer.singleShot(300, self._after_change)

    # ---- Aufbau
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(12)
        root.addLayout(top, 1)

        # links: Medien
        left = Glass(radius=30, alpha=120)
        self.left_panel = left
        left.setFixedWidth(300)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(16, 16, 10, 12)
        lv.setSpacing(8)
        t = QLabel("Medien")
        t.setFont(display_font(13))
        lv.addWidget(t)
        self.media_seg = Segmented(["Clips", "Musik & Sounds"], height=34)
        self.media_seg.changed.connect(lambda _: self.refresh_media())
        lv.addWidget(self.media_seg)
        self.media_scroll = QScrollArea()
        self.media_scroll.setWidgetResizable(True)
        self.media_scroll.setFrameShape(QScrollArea.NoFrame)
        self.media_scroll.viewport().setAutoFillBackground(False)
        lv.addWidget(self.media_scroll, 1)
        b = PopButton("Dateien hinzufügen …", "folder", "glass", 36)
        b.clicked.connect(self.import_files)
        lv.addWidget(b)
        top.addWidget(left)

        # Mitte: Vorschau
        mid = Glass(radius=30, alpha=120)
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(14, 14, 14, 12)
        mv.setSpacing(10)
        self.preview = Preview()
        self.preview.dbl.connect(self.toggle_fs)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview.box_picked.connect(self._pick_box)
        self.preview.box_begin.connect(self.snapshot)
        self.preview.box_changed.connect(lambda: self.on_changed(light=True))
        mv.addWidget(self.preview, 1)
        self.scrub = ScrubBar()
        self.scrub.setToolTip("Vor- und zurückspulen")
        self.scrub.begin.connect(self._scrub_begin)
        self.scrub.moved.connect(self.seek)
        self.scrub.end.connect(self._scrub_end)
        mv.addWidget(self.scrub)
        ctl = QHBoxLayout()
        ctl.setSpacing(8)
        self.time_lbl = lbl("0:00.00 / 0:00.00", 10, True)
        self.time_lbl.setFixedWidth(190)
        ctl.addWidget(self.time_lbl)
        ctl.addStretch(1)
        b = PopButton("", "prev", "flat", 38, icon_only=True)
        b.clicked.connect(lambda: self.seek(0))
        ctl.addWidget(b)
        self.play_btn = PopButton("", "play", "accent", 42, icon_only=True)
        self.play_btn.clicked.connect(self.toggle_play)
        ctl.addWidget(self.play_btn)
        b = PopButton("", "next", "flat", 38, icon_only=True)
        b.clicked.connect(lambda: self.seek(self.proj.length()))
        ctl.addWidget(b)
        ctl.addStretch(1)
        self.fs_btn = PopButton("", "expand", "glass", 38, icon_only=True)
        self.fs_btn.setToolTip("Vollbild-Vorschau (F) – so sieht das fertige Video aus")
        self.fs_btn.clicked.connect(self.toggle_fs)
        ctl.addWidget(self.fs_btn)
        self.exp_btn = PopButton("Montage exportieren", "upload", "accent", 40)
        self.exp_btn.clicked.connect(self.export)
        ctl.addWidget(self.exp_btn)
        mv.addLayout(ctl)
        from ui import Bar
        self.exp_bar = Bar(height=4)
        mv.addWidget(self.exp_bar)
        top.addWidget(mid, 1)

        # rechts: Eigenschaften
        right = Glass(radius=30, alpha=120)
        self.right_panel = right
        right.setFixedWidth(340)
        self.insp_outer = QVBoxLayout(right)
        self.insp_outer.setContentsMargins(18, 18, 18, 14)
        self.insp_outer.setSpacing(12)
        self.insp_tabs = Segmented(["Clip", "Übergang"], height=34)
        self.insp_tabs.changed.connect(self._tab_changed)
        self.insp_outer.addWidget(self.insp_tabs)
        self.insp_box = None
        top.addWidget(right)

        # unten: Werkzeuge + Zeitleiste
        bottom = Glass(radius=30, alpha=120)
        self.bottom_panel = bottom
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(12, 10, 12, 10)
        bv.setSpacing(6)
        tools = QHBoxLayout()
        tools.setSpacing(6)

        def tb(text, icon, fn, tip=""):
            x = PopButton(text, icon, "glass", 32, icon_only=not text)
            x.setToolTip(tip)
            x.clicked.connect(fn)
            tools.addWidget(x)
            return x
        tb("", "back", self.undo, "Rückgängig (Strg+Z)")
        tb("", "refresh", self.redo, "Wiederholen (Strg+Y)")
        tb("Teilen", "cut", self.split, "Clip an der Abspielposition teilen (S)")
        tb("Löschen", "trash", self.delete_sel, "Ausgewähltes löschen (Entf)")
        tools.addSpacing(8)
        self.mag_btn = PopButton("Einrasten", "magnet", "accent" if self.proj.magnet else "glass", 32)
        self.mag_btn.setToolTip("An: Clips kleben lückenlos aneinander. Aus: frei verschieben.")
        self.mag_btn.clicked.connect(self.toggle_magnet)
        tools.addWidget(self.mag_btn)
        tools.addStretch(1)
        self.len_lbl = lbl("", 9, True, "muted")
        tools.addWidget(self.len_lbl)
        tb("", "minus", lambda: self.zoom(1 / 1.4), "Herauszoomen")
        tb("", "plus", lambda: self.zoom(1.4), "Hineinzoomen")
        tb("Alles", "expand", lambda: self.tl.fit_all(), "Ganze Montage zeigen")
        bv.addLayout(tools)
        self.tl = MTimeline(self.proj)
        self.tl.seek.connect(self.seek)
        self.tl.magnet_pause.connect(lambda on: self.mag_btn.set_kind("glass" if on else
                                                                     ("accent" if self.proj.magnet else "glass")))
        self.tl.selected.connect(lambda s: (self.show_inspector(s), self._sync_boxes()))
        self.tl.changed.connect(self.on_changed)
        self.tl.begin.connect(self.snapshot)
        self.tl.trimming.connect(lambda path, t: self.player.show_src(path, t))
        self.tl.trim_begin.connect(lambda path, t: self.player.prefetch(path, t))
        self.tl.add_lane.connect(self.add_lane)
        self.tl.del_lane.connect(self.del_lane)
        self.tl.dropped.connect(self.drop_files)
        bv.addWidget(self.tl)
        root.addWidget(bottom)
        self.refresh_media()
        self.show_inspector(None)

    # ---- Medien
    def media_files(self, audio):
        ext = AUDIO_EXT if audio else VIDEO_EXT
        out = []
        if not audio:
            d = self.win.export_dir() if hasattr(self.win, "export_dir") else ""
            if d and os.path.isdir(d):
                fs = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(VIDEO_EXT)]
                out += sorted(fs, key=lambda x: os.path.getmtime(x), reverse=True)
        out += [m for m in self.media if m.lower().endswith(ext) and m not in out]
        return out

    def _loaded(self):
        self.tl.update()
        w = self.media_scroll.widget()
        if w is not None:
            w.update()
            for r in w.findChildren(MediaRow):
                r.update()

    def refresh_media(self):
        audio = self.media_seg.current() != "Clips"
        files_ = self.media_files(audio)
        key = (audio, tuple(files_))
        if key == self._media_key:
            return                                   # nichts geändert: Liste nicht neu bauen
        self._media_key = key
        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(0)
        files = files_
        if not files:
            v.addSpacing(20)
            h = lbl("Noch keine Musik oder Sounds. Füge MP3/WAV-Dateien über den Knopf unten hinzu." if audio else
                    "Noch keine Clips. Exportierte Clips aus dem Editor erscheinen hier automatisch – oder füge "
                    "Dateien über den Knopf unten hinzu.", 9, False, "muted", True)
            h.setAlignment(Qt.AlignCenter)
            v.addWidget(h)
        for f in files:
            row = MediaRow(f, audio)
            row.add.connect(lambda f=f, a=audio: self.add_media(f, a))
            v.addWidget(row)
        v.addStretch(1)
        self.media_scroll.setWidget(host)

    def import_files(self):
        audio = self.media_seg.current() != "Clips"
        flt = ("Ton (*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.opus *.wma)" if audio else
               "Video (*.mp4 *.mkv *.mov *.flv *.ts *.avi *.webm)")
        files, _ = QFileDialog.getOpenFileNames(self, "Dateien hinzufügen", "", flt + ";;Alle (*)")
        if files:
            for f in files:
                if f not in self.media:
                    self.media.append(f)
            self.win.st["montage_media"] = self.media
            self.win.save_settings()
            self._media_key = None
            self.refresh_media()

    def add_media(self, path, audio):
        self.snapshot()
        if audio:
            lane = None
            for tr in range(1, self.proj.alanes + 1):      # erste freie Ton-Spur an der Abspielposition
                if all(not (a.start < self.tl.ph + 1 and a.end > self.tl.ph) for a in self.proj.audios() if a.track == tr):
                    lane = tr
                    break
            if lane is None:
                self.proj.alanes += 1
                lane = self.proj.alanes
                self.tl.relayout()
            it = MItem(path, lane, start=self.tl.ph)
        else:
            vs = self.proj.videos()
            it = MItem(path, 0, start=vs[-1].end if vs else 0.0)
        if it.dur < 0.2:
            self.win.say("Datei konnte nicht gelesen werden", RED)
            return
        self.proj.items.append(it)
        self.proj.pack()
        self.tl.sel = it
        self.show_inspector(it)
        if self.proj.length() * self.tl.pps > self.tl.width() - 120:
            self.tl.fit_all()
        self.on_changed()

    # ---- Eigenschaften (rechts)
    def _clear(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._clear(it.layout())

    def _tab_changed(self, name):
        sel = self.tl.sel
        if name == "Übergang":
            if isinstance(sel, MItem) and sel.track == 0:
                vs = self.proj.videos()
                if sel in vs and vs.index(sel) < len(vs) - 1:
                    self.tl.sel = ("trans", sel)
                    self.tl.update()
                    self.show_inspector(self.tl.sel)
                    return
            if isinstance(sel, tuple) and sel[0] == "trans":
                self.show_inspector(sel)
                return
            self.show_inspector(("all_trans", None))
        else:
            if isinstance(sel, tuple) and sel[0] == "trans":
                self.tl.sel = sel[1]
                self.tl.update()
                self.show_inspector(sel[1])
                return
            self.show_inspector(sel if not (isinstance(sel, tuple) and sel[0] == "all_trans") else None)

    def show_inspector(self, sel):
        self.stop_trans_preview()
        want = "Übergang" if isinstance(sel, tuple) and sel[0] in ("trans", "all_trans") else "Clip"
        if self.insp_tabs.current() != want:
            self.insp_tabs.set_current(want)
        # jedes Mal ein frischer Bereich: das alte wird sofort ausgeblendet (keine Überlagerung)
        if self.insp_box is not None:
            self.insp_box.hide()
            self.insp_outer.removeWidget(self.insp_box)
            self.insp_box.deleteLater()
        self.insp_box = QWidget()
        self.insp_box.setAttribute(Qt.WA_TranslucentBackground)
        L = QVBoxLayout(self.insp_box)
        L.setContentsMargins(0, 0, 0, 0)
        L.setSpacing(10)
        self.insp_outer.addWidget(self.insp_box)

        def title(text, sub=""):
            q = QLabel(text)
            q.setFont(display_font(13))
            L.addWidget(q)
            if sub:
                L.addWidget(lbl(sub, 8.5, False, "muted", True))
        if sel is None:
            title("Projekt", "Wähle unten einen Clip, eine Tonspur oder eine Raute zwischen zwei Clips (Übergang).")
            L.addWidget(lbl("Auflösung", 9, True, "muted"))
            s = Segmented(["Original", "1080p", "1440p"], height=34)
            s.set_current(self.proj.res)
            s.changed.connect(lambda v: (setattr(self.proj, "res", v), self.on_changed()))
            L.addWidget(s)
            L.addWidget(lbl("Bilder pro Sekunde", 9, True, "muted"))
            s2 = Segmented(["Original", "60", "30"], height=34)
            s2.set_current(self.proj.fps)
            s2.changed.connect(lambda v: (setattr(self.proj, "fps", v), self.on_changed()))
            L.addWidget(s2)
            L.addWidget(lbl("„Original“ übernimmt Auflösung und fps des ersten Clips – ohne Qualitätsverlust.",
                            8.5, False, "muted", True))
            L.addSpacing(8)
            b = PopButton("Alles leeren", "trash", "danger", 34)
            b.clicked.connect(self.clear_all)
            L.addWidget(b)
            L.addStretch(1)
            return
        if isinstance(sel, tuple) and sel[0] == "multi":
            items = sel[1]
            title(f"{len(items)} ausgewählt", "Mit gedrückter Maus über mehrere Clips ziehen. Ziehen an einem der Clips "
                                              "verschiebt alle zusammen (bei Einrasten nur Ton und obere Spuren).")
            anyon = any(not i.muted for i in items)
            b = PopButton("Alle stumm" if anyon else "Ton für alle an", "mute" if anyon else "speaker", "glass", 34)
            b.clicked.connect(lambda: self.mute_items(items, anyon))
            L.addWidget(b)
            vids = [i for i in items if i.track <= 0]
            vol0 = sum(i.volume for i in items) / max(1, len(items))
            vsr = SliderRow("Lautstärke (alle ausgewählten)", 0, 2, 200, vol0, lambda v: f"{int(round(v * 100))} %")
            vst = {"snap": False}

            def vol_all(_):
                if not vst["snap"]:
                    self.snapshot()
                    vst["snap"] = True
                for i in items:
                    i.volume = round(vsr.value(), 2)
                self.on_changed(light=True)
            vsr.s.valueChanged.connect(vol_all)
            L.addWidget(vsr)
            if vids:
                L.addWidget(self.speed_slider(vids, "Geschwindigkeit (alle ausgewählten)"))
            L.addSpacing(6)
            b = PopButton(f"{len(items)} löschen", "trash", "danger", 34)
            b.clicked.connect(self.delete_sel)
            L.addWidget(b)
            L.addStretch(1)
            return
        if isinstance(sel, tuple) and sel[0] == "all_trans":
            vs = self.proj.videos()
            title("Übergänge", "Wähle einen Clip oder eine Raute – oder setze hier einen Übergang für alle Clips. "
                               "Maus auf einen Übergang halten = Vorschau.")
            if len(vs) < 2:
                L.addWidget(lbl("Füge mindestens zwei Clips hinzu.", 9, False, "muted", True))
                L.addStretch(1)
                return
            from PySide6.QtWidgets import QGridLayout
            grid = QGridLayout()
            grid.setHorizontalSpacing(4)
            grid.setVerticalSpacing(2)
            for k, (nm, key) in enumerate(TRANSITIONS):
                b = PopButton(nm, None, "glass", 30)
                b.setFont(app_font(9, True))
                b.clicked.connect(lambda _=False, key=key: self.trans_all(key, vs[0].tdur or 0.5))
                self._hover_preview(b, vs[0], key)
                grid.addWidget(b, k // 2, k % 2)
            L.addLayout(grid)
            L.addStretch(1)
            return
        if isinstance(sel, tuple):
            a = sel[1]
            title("Übergang", "Wie der Clip in den nächsten übergeht. Maus auf einen Übergang halten = Vorschau.")
            from PySide6.QtWidgets import QGridLayout
            grid = QGridLayout()
            grid.setHorizontalSpacing(4)
            grid.setVerticalSpacing(2)
            for k, (name, key) in enumerate(TRANSITIONS):
                b = PopButton(name, None, "accent" if a.trans == key else "glass", 30)
                b.setFont(app_font(9, True))
                b.clicked.connect(lambda _=False, key=key: self.set_trans(a, key))
                self._hover_preview(b, a, key)
                grid.addWidget(b, k // 2, k % 2)
            L.addLayout(grid)
            sr = SliderRow("Dauer", 0.2, 1.5, 13, a.tdur, lambda v: f"{v:.1f} s")
            sr.s.valueChanged.connect(lambda v: (setattr(a, "tdur", round(sr.value(), 2)), self.proj.pack(),
                                                 self.on_changed(light=True)))
            L.addWidget(sr)
            b = PopButton("Für alle Übergänge übernehmen", None, "glass", 32)
            b.clicked.connect(lambda: self.trans_all(a.trans, a.tdur))
            L.addWidget(b)
            L.addStretch(1)
            return
        it = sel
        name = os.path.basename(it.path)
        if it.kind == "box":
            title("Viereck", "In der Vorschau verschieben · unten rechts ziehen für die Größe.")
            L.addWidget(lbl("Farbe", 9, True, "muted"))
            row = QHBoxLayout()
            row.setSpacing(6)
            for c in BOX_COLORS:
                b = ColorDot(c, QColor(c).name() == QColor(it.color).name())
                b.clicked.connect(lambda c=c: (self.snapshot(), setattr(it, "color", c), self.show_inspector(it),
                                               self.on_changed(light=True)))
                row.addWidget(b)
            row.addStretch(1)
            L.addLayout(row)
            sr = SliderRow("Deckkraft", 0, 1, 20, it.alpha, lambda v: f"{int(round(v * 100))} %")
            sr.s.valueChanged.connect(lambda _: (setattr(it, "alpha", round(sr.value(), 2)), self.on_changed(light=True)))
            L.addWidget(sr)
            L.addWidget(lbl("Text (optional)", 9, True, "muted"))
            from PySide6.QtWidgets import QLineEdit
            te = QLineEdit(it.text)
            te.setPlaceholderText("z. B. ACE · 7 Kills")
            te.setMinimumHeight(38)
            te.textChanged.connect(lambda s: (setattr(it, "text", s), self.on_changed(light=True)))
            L.addWidget(te)
            s2 = SliderRow("Textgröße", 0.2, 0.9, 14, it.tsize, lambda v: f"{int(round(v * 100))} %")
            s2.s.valueChanged.connect(lambda _: (setattr(it, "tsize", round(s2.value(), 2)), self.on_changed(light=True)))
            L.addWidget(s2)
            b = PopButton("Ganzes Bild füllen", "expand", "glass", 32)
            b.clicked.connect(lambda: (self.snapshot(), [setattr(it, k, v) for k, v in
                                                         (("bx", 0.0), ("by", 0.0), ("bw", 1.0), ("bh", 1.0))],
                                       self.on_changed(light=True)))
            L.addWidget(b)
            L.addSpacing(6)
            b = PopButton("Löschen", "trash", "danger", 34)
            b.clicked.connect(self.delete_sel)
            L.addWidget(b)
            L.addStretch(1)
            return
        if it.track <= 0:
            title("Clip", name)
            L.addWidget(self.speed_slider([it], "Geschwindigkeit"))
            if self.tl.has_audio(it):
                b = PopButton("Ton an" if it.muted else "Stumm schalten", "speaker" if it.muted else "mute", "glass", 32)
                b.clicked.connect(lambda: self.mute_items([it], not it.muted))
                L.addWidget(b)
            else:
                L.addWidget(lbl("Dieser Clip hat keinen Ton.", 8.5, False, "muted"))
            sr = SliderRow("Clip-Ton", 0, 2, 200, it.volume, lambda v: f"{int(round(v * 100))} %")
            sr.s.valueChanged.connect(lambda _: (setattr(it, "volume", round(sr.value(), 2)), self.on_changed(light=True)))
            L.addWidget(sr)
            fi = SliderRow("Einblenden (aus Schwarz)", 0, 3, 30, it.fade_in, lambda v: f"{v:.1f} s")
            fi.s.valueChanged.connect(lambda _: (setattr(it, "fade_in", round(fi.value(), 2)), self.on_changed(light=True)))
            L.addWidget(fi)
            fo = SliderRow("Ausblenden (zu Schwarz)", 0, 3, 30, it.fade_out, lambda v: f"{v:.1f} s")
            fo.s.valueChanged.connect(lambda _: (setattr(it, "fade_out", round(fo.value(), 2)), self.on_changed(light=True)))
            L.addWidget(fo)
            L.addWidget(lbl(f"Ausschnitt: {fmt(it.src_in)} – {fmt(it.src_out)}  ·  Ränder unten ziehen zum Kürzen",
                            8.5, False, "muted", True))
            if it.track < 0:
                L.addWidget(lbl("Liegt auf einer oberen Video-Spur und überdeckt dort das Haupt-Video.", 8.5,
                                False, "muted", True))
            vs_ = self.proj.videos()
            if it.track == 0 and it in vs_ and vs_.index(it) < len(vs_) - 1:
                b = PopButton("Übergang zum nächsten Clip …", "layers", "glass", 34)
                b.clicked.connect(lambda: (setattr(self.tl, "sel", ("trans", it)), self.show_inspector(("trans", it)),
                                           self.tl.update()))
                L.addWidget(b)
        else:
            title("Musik / Sound", name)
            b = PopButton("Ton an" if it.muted else "Stumm schalten", "speaker" if it.muted else "mute", "glass", 32)
            b.clicked.connect(lambda: self.mute_items([it], not it.muted))
            L.addWidget(b)
            sr = SliderRow("Lautstärke", 0, 2, 200, it.volume, lambda v: f"{int(round(v * 100))} %")
            sr.s.valueChanged.connect(lambda _: (setattr(it, "volume", round(sr.value(), 2)), self.on_changed(light=True)))
            L.addWidget(sr)
            s1 = SliderRow("Einblenden", 0, 5, 50, it.fade_in, lambda v: f"{v:.1f} s")
            s1.s.valueChanged.connect(lambda _: (setattr(it, "fade_in", round(s1.value(), 2)), self.on_changed(light=True)))
            L.addWidget(s1)
            s2 = SliderRow("Ausblenden", 0, 5, 50, it.fade_out, lambda v: f"{v:.1f} s")
            s2.s.valueChanged.connect(lambda _: (setattr(it, "fade_out", round(s2.value(), 2)), self.on_changed(light=True)))
            L.addWidget(s2)
            b = PopButton("An Montage-Länge anpassen", None, "glass", 34)
            b.clicked.connect(lambda: self.fit_audio(it))
            L.addWidget(b)
            L.addWidget(lbl("Tipp: Mit „Clip-Ton“ beim Clip kannst du den Spielsound leiser machen, damit die Musik "
                            "besser zu hören ist.", 8.5, False, "muted", True))
        L.addSpacing(6)
        b = PopButton("Löschen", "trash", "danger", 34)
        b.clicked.connect(self.delete_sel)
        L.addWidget(b)
        L.addStretch(1)

    # ---- Bearbeiten
    def snapshot(self):
        self.undo_st.append(json.dumps(self.proj.to_dict()))
        del self.undo_st[:-80]
        self.redo_st = []

    def _restore(self, s):
        self.proj.load(json.loads(s))
        self.tl.proj = self.proj
        self.tl.relayout()
        self.tl.sel = None
        self.show_inspector(None)
        self.on_changed()

    def undo(self):
        if self.undo_st:
            self.redo_st.append(json.dumps(self.proj.to_dict()))
            self._restore(self.undo_st.pop())

    def redo(self):
        if self.redo_st:
            self.undo_st.append(json.dumps(self.proj.to_dict()))
            self._restore(self.redo_st.pop())

    def mute_items(self, items, on):
        self.snapshot()
        for i in items:
            i.muted = on
        self.show_inspector(self.tl.sel if self.tl.sel is not None else (("multi", items) if len(items) > 1 else None))
        self.on_changed()
        self.win.say("Stumm geschaltet" if on else "Ton wieder an")

    def speed_slider(self, items, title):
        """Stufenlos 0,1× bis 3,0× in 0,1er-Schritten (gilt für alle übergebenen Clips)."""
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        sr = SliderRow(title, 0.1, 3.0, 29, items[0].speed, lambda x: f"{round(x, 1):.1f}×")
        v.addWidget(sr)
        row = QHBoxLayout()
        row.setSpacing(4)
        for val in (0.5, 1.0, 1.5, 2.0):
            b = PopButton(f"{val:g}×", None, "glass", 28)
            b.setFont(app_font(8.5, True))
            b.clicked.connect(lambda _=False, val=val: sr.s._set(val))
            row.addWidget(b, 1)
        v.addLayout(row)
        state = {"snap": False}

        def changed(_):
            sp = round(sr.value(), 1)
            if all(abs(i.speed - sp) < 1e-6 for i in items):
                return
            if not state["snap"]:
                self.snapshot()
                state["snap"] = True
            for i in items:
                i.speed = sp
            self.proj.pack()
            self.on_changed(light=True)
        sr.s.valueChanged.connect(changed)
        return box

    def speed_items(self, items, sp):
        self.snapshot()
        for i in items:
            i.speed = sp
        self.proj.pack()
        self.on_changed()

    def select_all(self):
        self.tl.multi = list(self.proj.items)
        self.tl.sel = None
        self.show_inspector(("multi", self.tl.multi) if self.tl.multi else None)
        self.tl.update()

    # ---- Übergangs-Vorschau beim Darüberfahren
    def _hover_preview(self, btn, a, key):
        ent, lev = btn.enterEvent, btn.leaveEvent
        btn.enterEvent = lambda e: (ent(e), self.start_trans_preview(a, key))
        btn.leaveEvent = lambda e: (lev(e), self.stop_trans_preview())

    def start_trans_preview(self, a, key):
        vs = self.proj.videos()
        if a not in vs or vs.index(a) >= len(vs) - 1 or self.playing:
            return
        b = vs[vs.index(a) + 1]
        W, H = self.player.size
        fa = self.player.src.get("pv_a", a.path, max(a.src_in, a.src_out - 0.05 * a.speed))
        fb = self.player.src.get("pv_b", b.path, b.src_in + 0.05)
        if fa is None or fb is None:
            return
        self._tp = (_fit(fa, W, H), _fit(fb, W, H), key if key != "none" else "none", max(0.5, a.tdur), time.perf_counter())
        if not hasattr(self, "_tp_timer"):
            self._tp_timer = QTimer(self)
            self._tp_timer.setInterval(16)
            self._tp_timer.timeout.connect(self._tp_tick)
        self._tp_timer.start()

    def _tp_tick(self):
        if not getattr(self, "_tp", None):
            return
        fa, fb, key, d, t0 = self._tp
        cyc = (time.perf_counter() - t0) % (d + 0.6)          # Übergang abspielen, kurz halten, wiederholen
        p = min(1.0, max(0.0, (cyc - 0.15) / d))
        fr = fb if key == "none" and p > 0.5 else (fa if key == "none" else blend(fa, fb, p, key))
        self.preview.set_frame(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))

    def stop_trans_preview(self):
        if getattr(self, "_tp", None):
            self._tp = None
            if hasattr(self, "_tp_timer"):
                self._tp_timer.stop()
            self.player.seek(self.tl.ph)                         # wieder das normale Bild

    def set_trans(self, a, key):
        self.snapshot()
        a.trans = key
        self.proj.pack()
        self.show_inspector(("trans", a))
        self.on_changed()

    def trans_all(self, key, d):
        self.snapshot()
        for v in self.proj.videos()[:-1]:
            v.trans, v.tdur = key, d
        self.proj.pack()
        self.on_changed()
        self.win.say("Übergang für alle Clips übernommen")

    def set_speed(self, it, sp):
        self.snapshot()
        it.speed = sp
        self.proj.pack()
        self.on_changed()

    def fit_audio(self, it):
        self.snapshot()
        L = max((v.end for v in self.proj.videos()), default=0.0)
        if L > it.start + 0.2:
            it.src_out = min(media_info(it.path)["dur"], it.src_in + (L - it.start) * it.speed)
            it.fade_out = max(it.fade_out, 2.0)
        self.show_inspector(it)
        self.on_changed()

    def add_box(self):
        self.snapshot()
        if self.proj.vlanes < 2:
            self.proj.vlanes = 2
            self.tl.relayout()
        top = -(self.proj.vlanes - 1)
        b = MItem("", top, start=self.tl.ph, kind="box")
        self.proj.items.append(b)
        self.tl.sel = b
        self.preview.sel = b
        self.show_inspector(b)
        self.on_changed()
        self.win.say("Viereck hinzugefügt – in der Vorschau verschieben, unten rechts ziehen für die Größe")

    def _pick_box(self, b):
        self.tl.sel = b
        self.show_inspector(b)
        self.tl.update()

    def _sync_boxes(self):
        self.preview.boxes = []
        sel = self.tl.sel
        self.preview.sel = sel if isinstance(sel, MItem) and sel.kind == "box" else None
        self.preview.update()

    def add_lane(self, kind):
        self.snapshot()
        if kind == "video":
            if self.proj.vlanes >= 4:
                self.win.say("Höchstens 4 Video-Spuren")
                return
            self.proj.vlanes += 1
        else:
            if self.proj.alanes >= 6:
                self.win.say("Höchstens 6 Ton-Spuren")
                return
            self.proj.alanes += 1
        self.tl.relayout()
        self.on_changed()

    def del_lane(self, tr):
        """Spur löschen (mit allem, was darauf liegt). Haupt-Video und letzte Ton-Spur bleiben."""
        if tr == 0 or (tr > 0 and self.proj.alanes <= 1):
            return
        self.snapshot()
        n = sum(1 for i in self.proj.items if i.track == tr)
        self.proj.items = [i for i in self.proj.items if i.track != tr]
        for i in self.proj.items:
            if tr < 0 and i.track < tr:
                i.track += 1
            elif tr > 0 and i.track > tr:
                i.track -= 1
        if tr < 0:
            self.proj.vlanes -= 1
        else:
            self.proj.alanes -= 1
        self.tl.sel = None
        self.tl.multi = []
        self.tl.relayout()
        self.show_inspector(None)
        self.on_changed()
        self.win.say("Spur gelöscht" + (f" ({n} Stücke)" if n else "") + " – Strg+Z holt sie zurück")

    def import_paths(self, paths):
        """Dateien (z. B. aus dem Explorer) in die Medien aufnehmen."""
        ok = [p for p in paths if p.lower().endswith(VIDEO_EXT + AUDIO_EXT)]
        if not ok:
            self.win.say("Keine Video- oder Tondatei", RED)
            return
        for p in ok:
            if p not in self.media:
                self.media.append(p)
        self.win.st["montage_media"] = self.media
        self.win.save_settings()
        self._media_key = None
        if all(p.lower().endswith(AUDIO_EXT) for p in ok):
            self.media_seg.set_current("Musik & Sounds", True)
        elif all(p.lower().endswith(VIDEO_EXT) for p in ok):
            self.media_seg.set_current("Clips", True)
        self.refresh_media()
        self.win.say(f"{len(ok)} {'Datei' if len(ok) == 1 else 'Dateien'} hinzugefügt")

    def drop_files(self, paths, t, lane):
        """Dateien an eine Stelle der Zeitleiste ziehen."""
        self.snapshot()
        new = [p for p in paths if p not in self.media and not p.startswith(self.win.export_dir() or "\0")]
        if new:
            self.media += new
            self.win.st["montage_media"] = self.media
            self._media_key = None
        added = None
        for p in paths:
            audio = p.lower().endswith(AUDIO_EXT)
            if audio:
                tr = lane if (lane is not None and lane > 0) else 1
                it = MItem(p, tr, start=t)
            else:
                tr = lane if (lane is not None and lane <= 0) else 0
                it = MItem(p, tr, start=t)
                if tr == 0 and self.proj.magnet:
                    # an der Stelle einfügen: Reihenfolge nach der Ablege-Position
                    vs = self.proj.videos()
                    idx = sum(1 for v in vs if v.start + v.dur / 2 < t)
                    for k, v in enumerate(vs):
                        v.start = k if k < idx else k + 1
                    it.start = idx + 0.5
            if it.dur < 0.2:
                continue
            self.proj.items.append(it)
            if not (tr == 0 and self.proj.magnet):
                self.tl.resolve_overlap(it)
            self.proj.pack()
            t = it.end
            added = it
        if added is not None:
            self.tl.sel = added
            self.show_inspector(added)
            if self.proj.length() * self.tl.pps > self.tl.width() - 120:
                self.tl.fit_all()
        self.refresh_media()
        self.on_changed()

    def remove_empty_lanes(self):
        used = {i.track for i in self.proj.items}
        while self.proj.vlanes > 1 and -(self.proj.vlanes - 1) not in used:
            self.proj.vlanes -= 1
        while self.proj.alanes > 1 and self.proj.alanes not in used:
            self.proj.alanes -= 1
        self.tl.relayout()

    def toggle_magnet(self):
        self.snapshot()
        self.proj.magnet = not self.proj.magnet
        self.mag_btn.set_kind("accent" if self.proj.magnet else "glass")
        self.proj.pack()
        self.on_changed()
        self.win.say("Einrasten an – Clips kleben aneinander" if self.proj.magnet else "Einrasten aus – frei verschieben")

    def split(self):
        t = self.tl.ph
        sel = self.tl.sel if isinstance(self.tl.sel, MItem) else None
        cands = [sel] if sel and sel.start < t < sel.end else \
            [i for i in self.proj.items if i.start + 0.05 < t < i.end - 0.05]
        if not cands:
            self.win.say("Setze die Abspielposition auf einen Clip zum Teilen")
            return
        self.snapshot()
        it = cands[0]
        cut = it.src_in + (t - it.start) * it.speed
        b = MItem.from_dict(it.to_dict())
        b.id = uuid.uuid4().hex[:8]
        b.src_in, b.start = cut, t
        b.fade_in = 0.0
        it.src_out = cut
        it.trans, it.fade_out = "none", 0.0
        self.proj.items.append(b)
        self.proj.pack()
        self.on_changed()

    def delete_sel(self):
        s = self.tl.sel
        if self.tl.multi:
            self.snapshot()
            ids = {i.id for i in self.tl.multi}
            self.proj.items = [i for i in self.proj.items if i.id not in ids]
            n = len(ids)
            self.tl.multi = []
            self.tl.sel = None
            self.proj.pack()
            self.remove_empty_lanes()
            self.show_inspector(None)
            self.on_changed()
            self.win.say(f"{n} gelöscht")
            return
        if isinstance(s, tuple):
            self.set_trans(s[1], "none")
            return
        if s is None:
            return
        self.snapshot()
        self.proj.items = [i for i in self.proj.items if i is not s]
        self.tl.sel = None
        self.proj.pack()
        self.show_inspector(None)
        self.on_changed()

    def clear_all(self):
        if not self.proj.items:
            return
        self.snapshot()
        self.proj.items = []
        self.tl.sel = None
        self.on_changed()
        self.win.say("Montage geleert – Strg+Z holt sie zurück")

    def zoom(self, f):
        tl = self.tl
        vis = (tl.width() - tl.LEFT - 8) / tl.pps
        anchor = tl.ph if tl.x0 <= tl.ph <= tl.x0 + vis else tl.x0 + vis / 2   # um die Abspiel-Linie zoomen
        ax = tl.tx(anchor)
        tl.pps = max(2.0, min(400.0, tl.pps * f))
        tl.x0 = max(0.0, anchor - (ax - tl.LEFT) / tl.pps)
        tl.update()

    def on_changed(self, light=False):
        L = self.proj.length()
        self.len_lbl.setText(f"Länge {fmt_s(L)}  ·  {len(self.proj.videos())} Clips")
        self._time()
        self.tl.update()
        if not (self.tl.drag and self.tl.drag[0] in ("left", "right")):
            self.player.seek(self.tl.ph)              # beim Kürzen bleibt das Bild an der Schnittkante
        self._sync_boxes()
        self.mixer.ensure()
        self._aud_file = bool(self.proj.items)
        self.save_t.start()

    def _after_change(self):
        self.win.st["montage"] = self.proj.to_dict()
        self.win.save_settings()
        self.render_audio()

    # ---- Ton-Vorschau: der komplette Ton wird im Hintergrund kurz vorberechnet
    def render_audio(self):
        """Ton-Stücke der Clips im Hintergrund laden; gemischt wird live beim Abspielen."""
        self.mixer.ensure()
        self._aud_file = bool(self.proj.items)

    def _audio_ready(self, data):
        self._aud_file = True
        self.audio.load(data)

    # ---- Abspielen
    def seek(self, t):
        t = max(0.0, min(t, max(0.0, self.proj.length())))
        self.tl.ph = t
        self.player.seek(t)
        if self.audio and self.playing:
            self.audio.setPosition(int(t * 1000))
        self.tl.update()
        self._sync_boxes()
        self._time()

    def toggle_play(self):
        if not self.proj.items:
            return
        self.playing = not self.playing
        if self.playing and self.tl.ph >= self.proj.length() - 0.05:
            self.seek(0)
        self.player.t = self.tl.ph
        self.player.play(self.playing)
        self.play_btn.icon_name = "pause" if self.playing else "play"
        self.play_btn.update()
        if self.audio and self._aud_file:
            if self.playing:
                self._audio_pending = True                    # startet mit dem ersten Bild (siehe _poll)
            else:
                self._audio_pending = False
                self.audio.pause()

    def stop(self):
        if self.playing:
            self.toggle_play()
        if getattr(self, "fs", False):
            self.toggle_fs()

    def _time(self):
        L = self.proj.length()
        self.time_lbl.setText(f"{fmt(self.tl.ph)} / {fmt(L)}")
        self.scrub.set_pos(self.tl.ph, L)

    def _scrub_begin(self):
        self._scrub_resume = self.playing          # beim Ziehen kurz anhalten, danach weiter
        if self.playing:
            self.toggle_play()

    def _scrub_end(self):
        if getattr(self, "_scrub_resume", False):
            self._scrub_resume = False
            self.toggle_play()

    def _poll(self):
        if not self.isVisible():
            return
        w = max(64, self.preview.width())
        h = max(64, self.preview.height())
        dpr = self.devicePixelRatioF()
        self.player.size = (int(w * dpr) // 2 * 2, int(h * dpr) // 2 * 2)
        f = self.player.take()
        if f is None:
            return
        t, fr = f
        if getattr(self, "_tp", None):                     # Übergangs-Vorschau läuft gerade
            return
        if fr is not None:
            self.preview.set_frame(fr)
        if self.playing and getattr(self, "_audio_pending", False) and getattr(self.player, "started", False) \
                and t is not None and self.audio is not None:
            self._audio_pending = False
            self.audio.setPosition(int(t * 1000))
            self.audio.play()
        if self.playing and self.audio is not None and self._aud_file and self.audio.clock_ok():
            self.player.sync_clock(self.audio.position() / 1000.0)
        if self.playing:
            self.tl.ph = t
            self.tl.scroll_to(t)
            self.tl.update()
            self._sync_boxes()
            self._time()
            if not self.player.playing:
                self.playing = False
                self.play_btn.icon_name = "play"
                self.play_btn.update()
                if self.audio:
                    self.audio.pause()
            elif self.audio and self._aud_file and not self.audio.playing:
                pass

    # ---- Export
    def export(self):
        if self.exporting:
            self.cancel.set()
            return
        if not self.proj.videos() and not self.proj.audios():
            self.win.say("Füge zuerst Clips hinzu", RED)
            return
        d = self.win.export_dir() if hasattr(self.win, "export_dir") else ""
        start = os.path.join(d or os.path.expanduser("~"), f"Montage {time.strftime('%Y-%m-%d %H-%M')}.mp4")
        dst, _ = QFileDialog.getSaveFileName(self, "Montage speichern", start, "Video (*.mp4)")
        if not dst:
            return
        if not dst.lower().endswith(".mp4"):
            dst += ".mp4"
        self.stop()
        self.exporting = True
        self.cancel.clear()
        self.exp_btn.setText("Abbrechen")
        self.exp_btn.icon_name = "close"
        self.exp_btn.set_kind("danger")
        self.exp_bar.set_value(0, False)
        proj = copy.deepcopy(self.proj)

        def work():
            try:
                export_montage(proj, dst, lambda p: self.win.ui(self.exp_bar.set_value, p), self.cancel)
                self.win.ui(self._exp_done, dst, None)
            except Exception as e:
                self.win.ui(self._exp_done, dst, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _exp_done(self, dst, err):
        self.exporting = False
        self.exp_btn.setText("Montage exportieren")
        self.exp_btn.icon_name = "upload"
        self.exp_btn.set_kind("accent")
        if err:
            self.exp_bar.set_value(0, False)
            self.win.say("Abgebrochen" if "Abgebrochen" in err else "Export fehlgeschlagen: " + err[:80], RED)
            return
        self.exp_bar.set_value(1.0)
        self.win.say(f"Montage exportiert: {os.path.basename(dst)}")
        if getattr(self.win, "tg_open", None) is not None and self.win.tg_open.isChecked():
            try:
                from ace import open_path
                open_path(os.path.dirname(dst))
            except Exception:
                pass

    # ---- Vollbild-Vorschau
    def toggle_fs(self):
        w = self.win
        on = not getattr(self, "fs", False)
        self.fs = on
        if on:
            self._was_max = w.isMaximized()
            w.showFullScreen()
        else:
            w.showMaximized() if self._was_max else w.showNormal()
        for x in (self.left_panel, self.right_panel, self.bottom_panel, w.sidebar):
            x.setVisible(not on)
        w.dock.setVisible(not on)
        w.bg.layout().setContentsMargins(*((10, 10, 10, 10) if on else (20, 12, 20, 20)))
        w.place_dock()
        self.fs_btn.icon_name = "shrink" if on else "expand"
        self.fs_btn.setToolTip("Vollbild beenden (Esc)" if on else "Vollbild-Vorschau (F)")
        self.fs_btn.update()
        QTimer.singleShot(120, lambda: self.player.seek(self.tl.ph))

    # ---- Tasten
    def key(self, e):
        k = e.key()
        if k == Qt.Key_F:
            self.toggle_fs()
            return True
        if k == Qt.Key_Escape and getattr(self, "fs", False):
            self.toggle_fs()
            return True
        ctrl = bool(e.modifiers() & Qt.ControlModifier)
        if k == Qt.Key_Space:
            self.toggle_play()
        elif ctrl and k == Qt.Key_Z:
            self.redo() if e.modifiers() & Qt.ShiftModifier else self.undo()
        elif ctrl and k == Qt.Key_Y:
            self.redo()
        elif ctrl and k == Qt.Key_A:
            self.select_all()
        elif k == Qt.Key_M:
            sel = self.tl.multi or ([self.tl.sel] if isinstance(self.tl.sel, MItem) else [])
            if sel:
                self.mute_items(sel, not all(i.muted for i in sel))
        elif k == Qt.Key_Escape:
            self.tl.multi = []
            self.tl.sel = None
            self.show_inspector(None)
            self.tl.update()
        elif k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_sel()
        elif k == Qt.Key_S:
            self.split()
        elif k == Qt.Key_Left:
            self.seek(self.tl.ph - (1 / 30 if e.modifiers() & Qt.ShiftModifier else 1))
        elif k == Qt.Key_Right:
            self.seek(self.tl.ph + (1 / 30 if e.modifiers() & Qt.ShiftModifier else 1))
        elif k == Qt.Key_Home:
            self.seek(0)
        elif k == Qt.Key_End:
            self.seek(self.proj.length())
        else:
            return False
        return True

    def shutdown(self):
        self.player.stop_flag = True
        if self.audio:
            self.audio.stop()

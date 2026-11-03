"""Kill App - schneidet automatisch Kill-Clips aus GTA/FiveM-Aufnahmen (helle Glas-Oberflaeche, Qt)."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

import cv2
import numpy as np
from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QColorDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except Exception:          # Ton ist optional - ohne Multimedia laeuft die App trotzdem
    QAudioOutput = QMediaPlayer = None

import auth
import core
from core import Player, check_update, download_file, export_clip, find_ffmpeg, resource, scan_video
from theme import AMBER, DEFAULT, PRESETS, RED, T, ink, white, with_alpha
from ui import (Background, Bar, ElidedLabel, FadeStack, Glass, Overlay, PopButton, SeekBar, Segmented, SliderRow,
                Timeline, Toast, Toggle, app_font, paint_icon, tween)

# Textrollen (Farben kommen aus dem Farbsystem, damit der Modus umschaltbar ist)
TXT, MUT, DIM, ACC_TEXT = "text", "muted", "dim", "acc"

SETTINGS = os.path.join(os.path.expanduser("~"), ".killapp.json")

MODES = {
    "Genau": ("exact", "Bildgenau, neu kodiert in sehr hoher Qualität."),
    "Klein": ("h265", "Bildgenau, deutlich kleinere Dateien (H.265)."),
    "Schnell": ("copy", "Sofort fertig, aber Schnitt am Keyframe – Clip kann länger sein."),
}
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".flv", ".ts", ".avi", ".webm", ".m4v", ".wmv")


def sanitize_name(name):
    """Entfernt Zeichen, die in Dateinamen nicht erlaubt sind."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip().strip(".")[:80]


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_videos(folder):
    try:
        files = [f for f in os.listdir(folder) if f.lower().endswith(VIDEO_EXT)]
    except OSError:
        return []
    return sorted(files, key=natural_key)


def open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass
BUILTIN_SERVERS = [
    {"id": "bruder", "name": "Bruder", "kind": "bruder", "builtin": True},
    {"id": "standard", "name": "Standard", "kind": "xhit", "builtin": True},
]
KIND_TEXT = {"bruder": "Kill-Meldung mit Text-Prüfung", "notify": "Kill-Meldung (eigene Vorlage)",
             "xhit": "Roter Kill-X in der Bildmitte"}
AVATARS = [("#8c79ee", "#c9a6f8"), ("#f08fb8", "#f7b6a0"), ("#6fb7f2", "#8c9cf2"), ("#5fcfae", "#7fd0e8"),
           ("#f2b36f", "#f08f8f")]


def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def fmt_short(t):
    return f"{int(t // 60)}:{int(t % 60):02d}"


def bgr_to_pixmap(bgr):
    h, w = bgr.shape[:2]
    img = QImage(np.ascontiguousarray(bgr).data, w, h, 3 * w, QImage.Format_BGR888)
    return QPixmap.fromImage(img.copy())


def avatar_colors(name):
    return AVATARS[abs(hash(name)) % len(AVATARS)]


class Clip:
    def __init__(self, start, end, kill=None, thumb=None):
        self.start, self.end = start, end
        self.kill = start if kill is None else kill
        self.thumb = thumb          # QPixmap
        self.name = None            # eigener Dateiname (None = automatische Nummer)
        self.last_name = None       # Name, unter dem der Clip zuletzt exportiert wurde


def _quiet(fn):
    try:
        fn()
    except Exception:
        pass


class Bridge(QObject):
    """Fuehrt Funktionen aus Hintergrund-Threads sicher im Hauptthread aus."""
    call = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn, args: fn(*args))


def label(text="", size=10.5, bold=False, color=None, wrap=False):
    """color ist eine Textrolle (z. B. "muted", "dim", "acc", "err"), keine feste Farbe."""
    lb = QLabel(text)
    lb.setFont(app_font(size, bold))
    if color:
        lb.setProperty("role", color)
    lb.setWordWrap(wrap)
    return lb


_AVATAR_CACHE = {}


def avatar_path(sid):
    return os.path.join(core.DATA_DIR, "avatars", f"{sid}.png")


def avatar_pix(sid):
    """Profilbild eines Servers (QPixmap, quadratisch) oder None."""
    if not sid:
        return None
    if sid not in _AVATAR_CACHE:
        pm = QPixmap(avatar_path(sid))
        _AVATAR_CACHE[sid] = None if pm.isNull() else pm
    return _AVATAR_CACHE[sid]


def save_avatar(sid, src_path):
    """Schneidet das Bild mittig quadratisch zu und speichert es (256x256)."""
    im = QImage(src_path)
    if im.isNull():
        return False
    side = min(im.width(), im.height())
    im = im.copy((im.width() - side) // 2, (im.height() - side) // 2, side, side).scaled(
        256, 256, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    os.makedirs(os.path.dirname(avatar_path(sid)), exist_ok=True)
    ok = im.save(avatar_path(sid), "PNG")
    _AVATAR_CACHE.pop(sid, None)
    return ok


def remove_avatar(sid):
    try:
        os.remove(avatar_path(sid))
    except OSError:
        pass
    _AVATAR_CACHE.pop(sid, None)


def paint_avatar(p, rect, name, radius=None, letter_size=14, sid=None):
    pm = avatar_pix(sid)
    if pm is not None:
        rad = radius if radius is not None else rect.width() / 2
        path = QPainterPath()
        path.addRoundedRect(rect, rad, rad)
        p.save()
        p.setClipPath(path)
        p.drawPixmap(rect, pm, QRectF(0, 0, pm.width(), pm.height()))
        p.restore()
        return
    c0, c1 = avatar_colors(name)
    g = QLinearGradient(rect.topLeft(), rect.bottomRight())
    g.setColorAt(0, QColor(c0))
    g.setColorAt(1, QColor(c1))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    rad = radius if radius is not None else rect.width() / 2
    p.drawRoundedRect(rect, rad, rad)
    p.setPen(QColor("white"))
    p.setFont(app_font(letter_size, True))
    p.drawText(rect, Qt.AlignCenter, (name[:1] or "?").upper())


# ------------------------------------------------------------------ Bausteine
class ServerCard(QWidget):
    clicked = Signal()
    edit = Signal()
    delete = Signal()
    M, W, H = 10, 300, 168

    def __init__(self, name="", sub="", tag="", add=False, sid=None, parent=None):
        super().__init__(parent)
        self.name, self.sub, self.tag, self.add, self.sid = name, sub, tag, add, sid
        self.pop = 0.0
        self.ehov = 0.0
        self.dhov = 0.0
        self.locked = False
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.W + 2 * self.M, self.H + 2 * self.M)

    def _edit_rect(self):
        return QRectF(self.M + self.W - 46, self.M + 14, 32, 32)

    def _del_rect(self):
        return QRectF(self.M + self.W - 84, self.M + 14, 32, 32)

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 160)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 180)
        tween(self, "ehov", 0.0, 120)
        tween(self, "dhov", 0.0, 120)

    def mouseMoveEvent(self, e):
        if not self.add and not self.locked:
            tween(self, "ehov", 1.0 if self._edit_rect().contains(e.position()) else 0.0, 110)
            tween(self, "dhov", 1.0 if self._del_rect().contains(e.position()) else 0.0, 110)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if not self.add and not self.locked and self._edit_rect().contains(e.position()):
            self.edit.emit()
        elif not self.add and not self.locked and self._del_rect().contains(e.position()):
            self.delete.emit()
        else:
            self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.M, self.M, self.W, self.H)
        hv = max(0.0, min(1.0, self.pop))
        c = r.center()
        p.translate(c)
        p.scale(1 + 0.018 * self.pop, 1 + 0.018 * self.pop)
        p.translate(-c)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, white(220))
        g.setColorAt(1, white(150))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(r, 24, 24)
        bp = QPen(white(240), 1.2)
        if self.add:
            bp = QPen(with_alpha(T.acc, 150 + 90 * hv), 1.4)
            bp.setStyle(Qt.DashLine)
        p.setBrush(Qt.NoBrush)
        p.setPen(bp)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 24, 24)
        if self.add:
            d = 48
            cr = QRectF(c.x() - d / 2, c.y() - 36, d, d)
            p.setPen(Qt.NoPen)
            p.setBrush(with_alpha(T.acc, 40))
            p.drawEllipse(cr)
            paint_icon(p, "plus", cr.adjusted(12, 12, -12, -12), T.acc, 2.2)
            p.setPen(ink(235))
            p.setFont(app_font(11.5, True))
            p.drawText(QRectF(r.left(), c.y() + 18, r.width(), 28), Qt.AlignCenter, "Neuer Server")
            p.setPen(ink(130))
            p.setFont(app_font(9.5))
            p.drawText(QRectF(r.left(), c.y() + 42, r.width(), 22), Qt.AlignCenter, "Eigene Kill-Erkennung anlegen")
            return
        paint_avatar(p, QRectF(r.left() + 22, r.top() + 22, 54, 54), self.name, 18, 20, self.sid)
        p.setPen(ink(240))
        p.setFont(app_font(15, True))
        fm = QFontMetrics(p.font())
        p.drawText(QRectF(r.left() + 90, r.top() + 24, r.width() - 180, 26), Qt.AlignVCenter,
                   fm.elidedText(self.name, Qt.ElideRight, int(r.width() - 180)))
        p.setPen(ink(130))
        p.setFont(app_font(9.5))
        p.drawText(QRectF(r.left() + 90, r.top() + 50, r.width() - 110, 40),
                   int(Qt.AlignTop.value) | int(Qt.TextWordWrap.value), self.sub)
        if not self.locked:
            dr = self._del_rect()
            p.setPen(Qt.NoPen)
            p.setBrush(with_alpha(RED, 40 + 50 * self.dhov) if self.dhov > 0.05 else white(150))
            p.drawEllipse(dr)
            paint_icon(p, "trash", dr.adjusted(8 - 2 * self.dhov, 8 - 2 * self.dhov, -8 + 2 * self.dhov, -8 + 2 * self.dhov),
                       QColor(T.err) if self.dhov > 0.3 else T.icon)
            er = self._edit_rect()
            p.setPen(Qt.NoPen)
            p.setBrush(white(150))
            p.drawEllipse(er)
            paint_icon(p, "edit", er.adjusted(8 - 2 * self.ehov, 8 - 2 * self.ehov, -8 + 2 * self.ehov, -8 + 2 * self.ehov), T.icon)
        p.setPen(T.acc_text)
        p.setFont(app_font(10.5, True))
        p.drawText(QRectF(r.left() + 22, r.bottom() - 46, 120, 26), Qt.AlignVCenter, "Auswählen")
        paint_icon(p, "chevron", QRectF(r.left() + 100, r.bottom() - 43, 18, 18), T.acc_text)
        if self.tag:
            fm = QFontMetrics(app_font(9, True))
            tw = fm.horizontalAdvance(self.tag) + 20
            tr = QRectF(r.right() - 18 - tw, r.bottom() - 46, tw, 24)
            p.setPen(Qt.NoPen)
            p.setBrush(with_alpha(T.acc, 40))
            p.drawRoundedRect(tr, 12, 12)
            p.setPen(T.acc_text)
            p.setFont(app_font(9, True))
            p.drawText(tr, Qt.AlignCenter, self.tag)


class NavItem(QWidget):
    clicked = Signal()

    def __init__(self, icon, text, parent=None):
        super().__init__(parent)
        self.icon, self.text = icon, text
        self.sel = 0.0
        self.pop = 0.0
        self.enabled_nav = True
        self.setFixedHeight(48)
        self.setCursor(Qt.PointingHandCursor)

    def set_selected(self, on):
        tween(self, "sel", 1.0 if on else 0.0, 260)

    def set_nav_enabled(self, on):
        self.enabled_nav = on
        self.update()

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 160)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 160)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setOpacity(1.0 if self.enabled_nav else 0.45)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        s = 1 + 0.012 * self.pop
        c = r.center()
        p.translate(c)
        p.scale(s, s)
        p.translate(-c)
        a = self.sel
        p.setPen(Qt.NoPen)
        p.setBrush(T.sel_fill(a))
        p.drawRoundedRect(r, 16, 16)
        if a > 0.02:
            p.setPen(QPen(with_alpha(T.acc, 55 * a), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 16, 16)
        paint_icon(p, self.icon, QRectF(r.left() + 16, r.center().y() - 10, 20, 20), T.acc if a > 0.5 else T.icon, 1.8)
        p.setPen(ink(235 if a > 0.5 else 175))
        p.setFont(app_font(10.5, a > 0.5))
        p.drawText(QRectF(r.left() + 50, r.top(), r.width() - 56, r.height()), Qt.AlignVCenter, self.text)


class SideServer(QWidget):
    """Server-Eintrag in der Seitenleiste (wie eine Playlist)."""
    clicked = Signal()

    def __init__(self, name, active=False, sid=None, parent=None):
        super().__init__(parent)
        self.name = name
        self.sid = sid
        self.active = active
        self.pop = 0.0
        self.setFixedHeight(44)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 150)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 150)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(4, 3, -4, -3)
        c = r.center()
        p.translate(c)
        p.scale(1 + 0.012 * self.pop, 1 + 0.012 * self.pop)
        p.translate(-c)
        p.setPen(Qt.NoPen)
        p.setBrush(T.sel_fill() if self.active else Qt.NoBrush)
        p.drawRoundedRect(r, 14, 14)
        paint_avatar(p, QRectF(r.left() + 10, r.center().y() - 11, 22, 22), self.name, 8, 9, self.sid)
        p.setPen(ink(235 if self.active else 175))
        p.setFont(app_font(10, self.active))
        fm = QFontMetrics(p.font())
        p.drawText(QRectF(r.left() + 44, r.top(), r.width() - 50, r.height()), Qt.AlignVCenter,
                   fm.elidedText(self.name, Qt.ElideRight, int(r.width() - 54)))


class ClipRow(QWidget):
    """Eintrag in der Clip-Liste (wie ein Titel in einer Playlist)."""
    clicked = Signal()
    delete = Signal()
    rename = Signal()

    def __init__(self, index, clip, parent=None):
        super().__init__(parent)
        self.i, self.c = index, clip
        self.selected = False
        self.hov = 0.0
        self.xhov = 0.0
        self.ehov = 0.0
        self.title_text = None
        self.appear = 1.0                 # 0 -> 1: Einblenden (Deckkraft + leichtes Aufsteigen)
        self.setFixedHeight(66)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def _xr(self):
        return QRectF(self.width() - 40, (self.height() - 28) / 2, 28, 28)

    def _er(self):
        return QRectF(self.width() - 72, (self.height() - 28) / 2, 28, 28)

    def enterEvent(self, e):
        tween(self, "hov", 1.0, 130)

    def leaveEvent(self, e):
        tween(self, "hov", 0.0, 130)
        tween(self, "xhov", 0.0, 100)
        tween(self, "ehov", 0.0, 100)

    def mouseMoveEvent(self, e):
        tween(self, "xhov", 1.0 if self._xr().contains(e.position()) else 0.0, 100)
        tween(self, "ehov", 1.0 if self._er().contains(e.position()) else 0.0, 100)

    def mousePressEvent(self, e):
        if self._xr().contains(e.position()):
            self.delete.emit()
        elif self._er().contains(e.position()):
            self.rename.emit()
        else:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, e):
        if not self._xr().contains(e.position()) and not self._er().contains(e.position()):
            self.rename.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(2, 3, -2, -3)
        if self.appear < 1.0:
            p.setOpacity(max(0.0, self.appear))
            p.translate(0, (1 - self.appear) * 8)
        c0 = r.center()
        p.translate(c0)
        p.scale(1 + 0.008 * self.hov, 1 + 0.008 * self.hov)
        p.translate(-c0)
        p.setPen(QPen(with_alpha(T.acc, 90), 1.2) if self.selected else Qt.NoPen)
        p.setBrush(T.sel_fill() if self.selected else Qt.NoBrush)
        p.drawRoundedRect(r, 18, 18)
        p.setPen(ink(110))
        p.setFont(app_font(10))
        p.drawText(QRectF(r.left() + 6, r.top(), 28, r.height()), Qt.AlignCenter, str(self.i + 1))
        t = QRectF(r.left() + 40, r.top() + 8, 70, r.height() - 16)
        if self.c.thumb:
            path = QPainterPath()
            path.addRoundedRect(t, 9, 9)
            p.save()
            p.setClipPath(path)
            pw, ph = self.c.thumb.width(), self.c.thumb.height()
            ta = t.width() / t.height()
            sw, sh = (ph * ta, ph) if pw / ph > ta else (pw, pw / ta)
            p.drawPixmap(t, self.c.thumb, QRectF((pw - sw) / 2, (ph - sh) / 2, sw, sh))
            p.restore()
        tx = t.right() + 14
        avail = max(40, self.width() - 160 - tx)
        p.setPen(ink(240))
        p.setFont(app_font(10.5, True))
        name = self.title_text or f"Clip {self.i + 1}"
        p.drawText(QRectF(tx, r.top() + 11, avail, 20), Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(name, Qt.ElideRight, int(avail)))
        c = self.c
        p.setPen(ink(150))
        p.setFont(app_font(9))
        sub = f"{fmt_short(c.start)} – {fmt_short(c.end)}" + ("  ·  exportiert" if c.last_name else "")
        p.drawText(QRectF(tx, r.top() + 32, avail, 18), Qt.AlignVCenter, sub)
        p.setPen(ink(150))
        p.setFont(app_font(9.5))
        p.drawText(QRectF(r.right() - 150, r.top(), 66, r.height()), Qt.AlignVCenter | Qt.AlignRight,
                   f"{c.end - c.start:.1f} s")
        er = self._er()
        p.setPen(Qt.NoPen)
        p.setBrush(white(160 * self.ehov))
        p.drawEllipse(er)
        paint_icon(p, "edit", er.adjusted(7, 7, -7, -7), with_alpha(T.icon, int(120 + 110 * self.ehov)), 1.6)
        xr = self._xr()
        p.setBrush(with_alpha(RED, 55 * self.xhov))
        p.drawEllipse(xr)
        paint_icon(p, "close", xr.adjusted(9, 9, -9, -9), with_alpha(T.icon, int(120 + 110 * self.xhov)), 1.7)


class LibRow(QWidget):
    """Datei im Export-Ordner."""
    rename = Signal()
    open_file = Signal()

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.thumb = None
        self.detail = ""
        self.hov = 0.0
        self.ehov = 0.0
        self.phov = 0.0
        self.appear = 1.0
        self.setFixedHeight(70)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def _er(self):
        return QRectF(self.width() - 80, (self.height() - 32) / 2, 32, 32)

    def _pr(self):
        return QRectF(self.width() - 44, (self.height() - 32) / 2, 32, 32)

    def enterEvent(self, e):
        tween(self, "hov", 1.0, 120)

    def leaveEvent(self, e):
        tween(self, "hov", 0.0, 120)
        tween(self, "ehov", 0.0, 100)
        tween(self, "phov", 0.0, 100)

    def mouseMoveEvent(self, e):
        tween(self, "ehov", 1.0 if self._er().contains(e.position()) else 0.0, 100)
        tween(self, "phov", 1.0 if self._pr().contains(e.position()) else 0.0, 100)

    def mousePressEvent(self, e):
        if self._er().contains(e.position()):
            self.rename.emit()
        else:
            self.open_file.emit()

    def mouseDoubleClickEvent(self, e):
        self.open_file.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(2, 3, -2, -3)
        if self.appear < 1.0:
            p.setOpacity(max(0.0, self.appear))
            p.translate(0, (1 - self.appear) * 8)
        p.setPen(Qt.NoPen)
        p.setBrush(white(120 * self.hov))
        p.drawRoundedRect(r, 18, 18)
        t = QRectF(r.left() + 10, r.top() + 8, 84, r.height() - 16)
        p.setBrush(ink(18))
        p.drawRoundedRect(t, 10, 10)
        if self.thumb:
            path = QPainterPath()
            path.addRoundedRect(t, 10, 10)
            p.save()
            p.setClipPath(path)
            pw, ph = self.thumb.width(), self.thumb.height()
            ta = t.width() / t.height()
            sw, sh = (ph * ta, ph) if pw / ph > ta else (pw, pw / ta)
            p.drawPixmap(t, self.thumb, QRectF((pw - sw) / 2, (ph - sh) / 2, sw, sh))
            p.restore()
        tx = t.right() + 14
        avail = max(40, self.width() - 100 - tx)
        p.setPen(ink(240))
        p.setFont(app_font(10.5, True))
        stem = os.path.splitext(os.path.basename(self.path))[0]
        p.drawText(QRectF(tx, r.top() + 12, avail, 20), Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(stem, Qt.ElideRight, int(avail)))
        p.setPen(ink(150))
        p.setFont(app_font(9))
        p.drawText(QRectF(tx, r.top() + 34, avail, 18), Qt.AlignVCenter, self.detail)
        er = self._er()
        p.setPen(Qt.NoPen)
        p.setBrush(white(160 * self.ehov))
        p.drawEllipse(er)
        paint_icon(p, "edit", er.adjusted(8, 8, -8, -8), with_alpha(T.icon, int(130 + 100 * self.ehov)), 1.6)
        pr = self._pr()
        p.setBrush(with_alpha(T.acc, 70 * self.phov))
        p.drawEllipse(pr)
        paint_icon(p, "play", pr.adjusted(9, 9, -9, -9), with_alpha(T.icon, int(130 + 100 * self.phov)), 1.6)


class SkeletonRow(QWidget):
    """Platzhalter in der Clip-Liste, solange die Suche laeuft (statt eines Drehkreises)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(66)
        self.ph = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._step)
        self.timer.start(33)

    def _step(self):
        if self.isVisible():
            self.ph = (self.ph + 0.022) % 1.0
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 3, -2, -3)
        p.setPen(Qt.NoPen)
        base, hi = ink(18), ink(34)
        t = QRectF(r.left() + 40, r.top() + 8, 70, r.height() - 16)
        blocks = [(t, 9), (QRectF(t.right() + 14, r.top() + 14, 120, 12), 6),
                  (QRectF(t.right() + 14, r.top() + 34, 80, 10), 5), (QRectF(r.left() + 8, r.center().y() - 6, 18, 12), 5)]
        x = (self.ph * 1.6 - 0.3) * self.width()
        for rect, rad in blocks:
            g = QLinearGradient(x - 90, 0, x + 90, 0)
            g.setColorAt(0, base)
            g.setColorAt(0.5, hi)
            g.setColorAt(1, base)
            p.setBrush(g)
            p.drawRoundedRect(rect, rad, rad)


class VideoWidget(QWidget):
    """Vorschau-Flaeche mit Zeit- und Kill-Anzeige."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.img = None
        self.arr = None
        self.time_text = "00:00.00"
        self.kill = False
        self.has_video = False
        self.cover = None

    def resizeEvent(self, e):
        if self.cover is not None:
            self.cover.setGeometry(self.rect())

    def set_frame(self, rgb):
        h, w = rgb.shape[:2]
        self.arr = rgb
        self.img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        self.img.setDevicePixelRatio(self.devicePixelRatioF())
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 24, 24)
        p.setClipPath(path)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        c0, c1 = T.video_stops()
        g.setColorAt(0, c0)
        g.setColorAt(1, c1)
        p.fillRect(self.rect(), g)
        if self.img is not None and self.has_video:
            dpr = self.devicePixelRatioF()
            iw, ih = self.img.width() / dpr, self.img.height() / dpr
            p.drawImage(QPointF((self.width() - iw) / 2, (self.height() - ih) / 2), self.img)
        p.setClipping(False)
        p.setPen(QPen(white(200), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 24, 24)
        if self.has_video:
            p.setFont(app_font(10, True))
            tw = QFontMetrics(p.font()).horizontalAdvance(self.time_text) + 24
            tr = QRectF(16, 16, tw, 28)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 16, 44, 150))
            p.drawRoundedRect(tr, 14, 14)
            p.setPen(QColor("white"))
            p.drawText(tr, Qt.AlignCenter, self.time_text)
            if self.kill:
                kr = QRectF(self.width() - 16 - 92, 16, 92, 28)
                p.setPen(Qt.NoPen)
                p.setBrush(RED)
                p.drawRoundedRect(kr, 14, 14)
                p.setPen(QColor("white"))
                p.drawText(kr, Qt.AlignCenter, "●  KILL")


def panel(title, sub="", radius=26, alpha=120):
    """Glas-Karte mit Titel; gibt (Karte, Layout) zurueck."""
    g = Glass(None, radius=radius, alpha=alpha)
    lay = QVBoxLayout(g)
    lay.setContentsMargins(22, 20, 22, 20)
    lay.setSpacing(10)
    lay.addWidget(label(title, 14.5, True))
    if sub:
        lay.addWidget(ElidedLabel(sub, 9.5, False, MUT))
    return g, lay


def icon_pixmap(name, color, size=20, lw=1.8):
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(2)
    pp = QPainter(pm)
    pp.setRenderHint(QPainter.Antialiasing)
    paint_icon(pp, name, QRectF(0, 0, size, size), color, lw)
    pp.end()
    return pm


class DialogCard(Glass):
    """Karte fuer Overlays: Titel, Untertitel, Inhalt, Knopfleiste."""

    def __init__(self, title, subtitle="", width=500):
        super().__init__(None, radius=30, alpha=215, dialog=True)
        self.setFixedWidth(width)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(32, 28, 32, 24)
        self.lay.setSpacing(10)
        self.lay.addWidget(label(title, 18, True))
        if subtitle:
            self.lay.addWidget(label(subtitle, 10.5, color=MUT, wrap=True))
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        self.lay.addLayout(self.body)
        self.err = label("", 10, color="err", wrap=True)
        self.lay.addWidget(self.err)
        self.btns = QHBoxLayout()
        self.btns.setSpacing(8)
        self.btns.addStretch(1)
        self.lay.addSpacing(6)
        self.lay.addLayout(self.btns)

    def fit(self):
        self.lay.activate()
        self.setFixedHeight(max(self.lay.totalHeightForWidth(self.width()), self.lay.sizeHint().height()))


class PickCanvas(QWidget):
    """Zeigt einen Screenshot; man zieht Rahmen mit der Maus."""
    released = Signal(tuple)

    def __init__(self, pix, parent=None):
        super().__init__(parent)
        self.pix = pix
        W, H = pix.width(), pix.height()
        self.s = min(1.0, 960 / W, 470 / H)
        self.dw, self.dh = int(W * self.s), int(H * self.s)
        self.setFixedSize(self.dw, self.dh)
        self.setCursor(Qt.CrossCursor)
        self.boxes = [None, None, None]
        self.step = 0
        self.start = None
        self.live = None
        self.colors = [T.acc, QColor("#2f8fe8"), AMBER]

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPixmap(self.rect(), self.pix)
        for i, b in enumerate(self.boxes):
            if b and i <= self.step:
                x0, y0, x1, y1 = [v * self.s for v in b]
                p.setPen(QPen(self.colors[i], 2))
                p.setBrush(with_alpha(self.colors[i], 40))
                p.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))
        if self.live:
            x0, y0, x1, y1 = self.live
            p.setPen(QPen(self.colors[self.step], 2))
            p.setBrush(with_alpha(self.colors[self.step], 40))
            p.drawRect(QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)))

    def _c(self, pos):
        return min(max(0, pos.x()), self.dw - 1), min(max(0, pos.y()), self.dh - 1)

    def mousePressEvent(self, e):
        self.start = self._c(e.position())
        self.live = None

    def mouseMoveEvent(self, e):
        if self.start:
            x, y = self._c(e.position())
            self.live = (self.start[0], self.start[1], x, y)
            self.update()

    def mouseReleaseEvent(self, e):
        if not self.start:
            return
        x, y = self._c(e.position())
        x0, x1 = sorted((self.start[0], x))
        y0, y1 = sorted((self.start[1], y))
        self.start = self.live = None
        if x1 - x0 >= 6 and y1 - y0 >= 4:
            self.boxes[self.step] = tuple(int(v / self.s) for v in (x0, y0, x1, y1))
            self.released.emit(self.boxes[self.step])
        self.update()


class ServerChip(QWidget):
    """Kleine Server-Anzeige oben rechts im Editor (klickbar -> Server-Seite)."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.name = ""
        self.pop = 0.0
        self.setFixedHeight(48)
        self.setFixedWidth(200)
        self.setCursor(Qt.PointingHandCursor)

    def set_server(self, name, sid=None):
        self.name = name
        self.sid = sid
        self.update()

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 150)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 150)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(3, 2, -3, -2)
        c = r.center()
        p.translate(c)
        p.scale(1 + 0.012 * self.pop, 1 + 0.012 * self.pop)
        p.translate(-c)
        p.setPen(QPen(white(240), 1.1))
        p.setBrush(white(190))
        p.drawRoundedRect(r, 22, 22)
        if self.name:
            paint_avatar(p, QRectF(r.left() + 8, r.center().y() - 15, 30, 30), self.name, 15, 12, getattr(self, 'sid', None))
            p.setPen(ink(235))
            p.setFont(app_font(10.5, True))
            p.drawText(QRectF(r.left() + 48, r.top(), r.width() - 56, r.height()), Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(self.name, Qt.ElideRight, int(r.width() - 60)))
        else:
            p.setPen(ink(130))
            p.setFont(app_font(10))
            p.drawText(r, Qt.AlignCenter, "Kein Server")


class ColorDot(QWidget):
    """Farbfeld zum Anklicken (zeigt die aktuelle Farbe)."""
    clicked = Signal()

    def __init__(self, getter, parent=None):
        super().__init__(parent)
        self.getter = getter
        self.pop = 0.0
        self.setFixedSize(44, 44)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 150)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 150)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QRectF(self.rect()).center()
        r = 15 + 3 * self.pop
        p.setPen(QPen(white(240), 2))
        p.setBrush(self.getter())
        p.drawEllipse(c, r, r)



class PresetCard(QWidget):
    """Farbthema zum Anklicken: zwei Akzentfarben und der Hintergrundton."""
    clicked = Signal()

    def __init__(self, name, preset, parent=None):
        super().__init__(parent)
        self.name, self.preset = name, preset
        self.pop = 0.0
        self.setFixedSize(92, 84)
        self.setCursor(Qt.PointingHandCursor)

    def active(self):
        return T.acc.name().lower() == self.preset["acc"].lower() and T.acc2.name().lower() == self.preset["acc2"].lower()

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 150)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 150)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        c = r.center()
        p.translate(c)
        p.scale(1 + 0.025 * self.pop, 1 + 0.025 * self.pop)
        p.translate(-c)
        on = self.active()
        p.setPen(QPen(QColor(T.acc) if on else white(220), 2 if on else 1.1))
        p.setBrush(white(170))
        p.drawRoundedRect(r, 18, 18)
        cx, cy = r.center().x(), r.top() + 28
        for dx, col in ((-9, self.preset["acc"]), (9, self.preset["acc2"])):
            p.setPen(QPen(white(240), 1.6))
            p.setBrush(QColor(col))
            p.drawEllipse(QPointF(cx + dx, cy), 13, 13)
        p.setPen(ink(210))
        p.setFont(app_font(9, True))
        p.drawText(QRectF(r.left(), r.top() + 46, r.width(), 22), Qt.AlignCenter, self.name)


class WinControls(QWidget):
    """Eigene Fenster-Knoepfe (Minimieren, Maximieren, Schliessen), da das Fenster keine Titelleiste hat."""
    minimize = Signal()
    toggle = Signal()
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(3 * 42, 30)
        self.setMouseTracking(True)
        self.hov = [0.0, 0.0, 0.0]
        self.maximized = False
        self._h = -1

    def _idx(self, x):
        return int(x // 42) if 0 <= x < 126 else -1

    def mouseMoveEvent(self, e):
        i = self._idx(e.position().x())
        if i != self._h:
            for k in range(3):
                tween_list(self, k, 1.0 if k == i else 0.0)
            self._h = i

    def leaveEvent(self, e):
        for k in range(3):
            tween_list(self, k, 0.0)
        self._h = -1

    def mousePressEvent(self, e):
        i = self._idx(e.position().x())
        (self.minimize, self.toggle, self.closed)[i].emit() if i >= 0 else None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        for i, name in enumerate(("min", "restore" if self.maximized else "square", "close")):
            r = QRectF(i * 42 + 5, 2, 32, 26)
            h = self.hov[i]
            p.setPen(Qt.NoPen)
            p.setBrush(with_alpha(RED, 230 * h) if i == 2 else white(150 * h))
            p.drawRoundedRect(r, 13, 13)
            col = QColor("white") if (i == 2 and h > 0.5) else ink(190)
            paint_icon(p, name, QRectF(r.center().x() - 8, r.center().y() - 8, 16, 16), col, 1.7)


def tween_list(w, k, end):
    """Animiert Eintrag k der Liste w.hov (fuer Widgets mit mehreren Hover-Zustaenden)."""
    from PySide6.QtCore import QVariantAnimation
    d = w.__dict__.setdefault("_tl", {})
    a = d.get(k)
    if a is None:
        a = QVariantAnimation(w)
        a.valueChanged.connect(lambda v, kk=k: (w.hov.__setitem__(kk, float(v)), w.update()))
        d[k] = a
    a.stop()
    a.setDuration(140)
    a.setStartValue(float(w.hov[k]))
    a.setEndValue(float(end))
    a.start()


class DropHint(QWidget):
    """Zeigt beim Reinziehen einer Datei einen Hinweis ("Video hier loslassen")."""

    def __init__(self, parent):
        super().__init__(parent)
        self.k = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()

    def show_hint(self):
        self.setGeometry(self.parent().rect())
        self.show()
        self.raise_()
        tween(self, "k", 1.0, 160)

    def hide_hint(self):
        tween(self, "k", 0.0, 140)
        QTimer.singleShot(170, lambda: self.k < 0.05 and self.hide())

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        c = QColor(T.scrim)
        c.setAlpha(int(c.alpha() * 0.85 * self.k))
        p.setClipPath(self.parent().clip_path())
        p.fillRect(self.rect(), c)
        p.setClipping(False)
        w, h = 520, 250
        r = QRectF((self.width() - w) / 2, (self.height() - h) / 2 + (1 - self.k) * 14, w, h)
        p.setOpacity(self.k)
        p.setPen(Qt.NoPen)
        p.setBrush(T.dialog_fill)
        p.drawRoundedRect(r, 32, 32)
        pen = QPen(T.acc, 2.2)
        pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(14, 14, -14, -14), 24, 24)
        ic = QRectF(r.center().x() - 24, r.top() + 56, 48, 48)
        paint_icon(p, "upload", ic, T.acc, 2.0)
        p.setPen(ink(240))
        p.setFont(app_font(17, True))
        p.drawText(QRectF(r.left(), r.top() + 118, r.width(), 36), Qt.AlignCenter, "Video hier loslassen")
        p.setPen(ink(140))
        p.setFont(app_font(10))
        p.drawText(QRectF(r.left(), r.top() + 154, r.width(), 28), Qt.AlignCenter, "mp4, mkv, mov, avi …")


# ------------------------------------------------------------------ Hauptfenster
class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kill App")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)        # keine Windows-Titelleiste
        self.setAttribute(Qt.WA_TranslucentBackground)                # das Fenster selbst ist der abgerundete Kasten
        self.setMinimumSize(1280, 820)
        self.resize(1440, 900)
        self.bridge = Bridge()
        self.st = self.load_settings()
        T.configure(self.st["dark"], self.st["acc"], self.st["acc2"], self.st["bg"])
        QApplication.instance().setStyleSheet(T.qss())
        self.ffmpeg = find_ffmpeg()
        self.server = None
        self.path = None
        self.role = None                       # None = ohne Konto-Server (alles erlaubt), "admin" oder "user"
        self.login_open = False
        self.pending_invite = ""
        self.player = None
        self.audio = self.aout = None
        if QMediaPlayer is not None:
            try:
                self.audio = QMediaPlayer()
                self.aout = QAudioOutput()
                self.aout.setVolume(0.8)
                self.audio.setAudioOutput(self.aout)
            except Exception:
                self.audio = None
        self._shown_id = 0
        self.fps, self.duration, self.pos = 60.0, 0.0, 0.0
        self.has_video = False
        self.clips, self.sel = [], None
        self.cancel = threading.Event()
        self.scanning = False
        self.playing = False
        self.play_clip = None
        self.speed = 1.0
        self.update_url = None
        self.mark_in = self.mark_out = None
        self.dir_widgets = []
        self.lib_gen = 0
        self.lib_rows = {}
        self.lib_cache = {}
        self._tick = 0
        self._last_tl = 0.0
        self._thumb_pending = False
        self.tags = {}
        self.rows = []

        self.bg = Background()
        self.setCentralWidget(self.bg)
        root = QHBoxLayout(self.bg)
        root.setContentsMargins(34, 46, 34, 34)
        root.setSpacing(14)
        fl = root
        self.sidebar = self.build_sidebar()
        fl.addWidget(self.sidebar)
        self.stack = FadeStack()
        fl.addWidget(self.stack, 1)
        self.page_server = self.build_server_page()
        self.page_editor = self.build_editor_page()
        self.page_library = self.build_library_page()
        self.page_settings = self.build_settings_page()
        for pg in (self.page_server, self.page_editor, self.page_library, self.page_settings):
            self.stack.add(pg)
        self.overlay = Overlay(self.bg)
        self.toast = Toast(self.bg)
        self.drop_hint = DropHint(self.bg)
        self.pending_video = None
        self.setAcceptDrops(True)
        self.controls = WinControls(self.bg)
        self.controls.minimize.connect(self.showMinimized)
        self.controls.toggle.connect(self.toggle_max)
        self.controls.closed.connect(self.close)
        self.controls.raise_()
        self.bg.hooks = {"press": self._bg_press, "move": self._bg_move, "double": self._bg_double}
        self.go_page(0, animate=False)
        self.render_servers()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_player)
        self.timer.start(8)
        QTimer.singleShot(250, self.check_ffmpeg)
        QTimer.singleShot(150, self.start_account)
        QTimer.singleShot(500, self.stack.warm)
        if self.st["autoupd"]:
            QTimer.singleShot(1500, lambda: self.start_update_check(False))

    # ------------------------------------------------------------ Infrastruktur
    def ui(self, fn, *args):
        self.bridge.call.emit(fn, args)

    def load_settings(self):
        d = {"pre": 4.0, "post": 2.0, "sens": 6, "mode": "Genau", "open": True, "loop": True,
             "autoupd": True, "gpu": True, "servers": [], "names": {}, "last_server": "bruder",
             "hidden": [], "export_dir": "", "thorough": False, "dark": False, "acc": DEFAULT["acc"], "acc2": DEFAULT["acc2"], "bg": DEFAULT["bg"]}
        try:
            with open(SETTINGS) as f:
                d.update(json.load(f))
        except Exception:
            pass
        if d.get("mode") in ("Verlustfrei", "Bildgenau") or d.get("mode") not in MODES:
            d["mode"] = "Genau"                 # alte Namen -> bildgenauer Modus (Schnitt stimmt auf das Bild)
        if not isinstance(d.get("servers"), list):
            d["servers"] = []
        if not isinstance(d.get("names"), dict):
            d["names"] = {}
        return d

    def save_settings(self):
        self.st.update(pre=self.s_pre.value(), post=self.s_post.value(), sens=int(round(self.s_sens.value())),
                       mode=self.seg_mode.current(), open=self.tg_open.isChecked(), loop=self.tg_loop.isChecked(),
                       autoupd=self.tg_upd.isChecked(), gpu=self.tg_gpu.isChecked(),
                       thorough=self.tg_thorough.isChecked())
        try:
            with open(SETTINGS, "w") as f:
                json.dump(self.st, f)
        except Exception:
            pass

    def closeEvent(self, e):
        self.save_settings()
        self.cancel.set()
        if self.player:
            self.player.stop()
        if self.audio:
            self.audio.stop()
        super().closeEvent(e)

    def check_ffmpeg(self):
        if not self.ffmpeg:
            self.toast.show_text("FFmpeg nicht gefunden – Export nicht möglich", RED)

    def say(self, text, color=None):
        self.toast.show_text(text, color)

    # ---- rahmenloses Fenster: Verschieben, Groesse aendern, Maximieren
    def toggle_max(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self.controls.maximized = self.isMaximized()
        self.controls.update()

    def _edges(self, pos):
        if self.isMaximized():
            return Qt.Edges()
        m, w, h = 8, self.bg.width(), self.bg.height()
        ed = Qt.Edges()
        if pos.x() < m:
            ed |= Qt.LeftEdge
        if pos.x() > w - m:
            ed |= Qt.RightEdge
        if pos.y() < m:
            ed |= Qt.TopEdge
        if pos.y() > h - m:
            ed |= Qt.BottomEdge
        return ed

    def _bg_press(self, e):
        if e.button() != Qt.LeftButton:
            return
        h = self.windowHandle()
        if h is None:
            return
        ed = self._edges(e.position())
        if ed:
            h.startSystemResize(ed)
        elif e.position().y() < 46:
            h.startSystemMove()

    def _bg_move(self, e):
        ed = self._edges(e.position())
        if ed in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            self.bg.setCursor(Qt.SizeFDiagCursor)
        elif ed in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            self.bg.setCursor(Qt.SizeBDiagCursor)
        elif ed in (Qt.LeftEdge, Qt.RightEdge):
            self.bg.setCursor(Qt.SizeHorCursor)
        elif ed in (Qt.TopEdge, Qt.BottomEdge):
            self.bg.setCursor(Qt.SizeVerCursor)
        else:
            self.bg.setCursor(Qt.ArrowCursor)

    def _bg_double(self, e):
        if e.position().y() < 46:
            self.toggle_max()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "bg"):
            rad = 0.0 if (self.isMaximized() or self.isFullScreen()) else 34.0
            if rad != self.bg.radius:
                self.bg.radius = rad
                self.bg.update()
        if hasattr(self, "controls"):
            self.controls.move(self.bg.width() - self.controls.width() - 26, 9)
            self.controls.maximized = self.isMaximized()
            self.controls.raise_()
        if hasattr(self, "overlay"):
            self.overlay.setGeometry(self.bg.rect())
            self.drop_hint.setGeometry(self.bg.rect())
            QTimer.singleShot(150, self._maybe_reflow)

    def _maybe_reflow(self):
        if self.srv_cols() != getattr(self, "_cols", None):
            self._cols = self.srv_cols()
            self.render_servers()

    # ------------------------------------------------------------ Drag & Drop
    @staticmethod
    def _dropped_video(e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p and p.lower().endswith(VIDEO_EXT):
                return p
        return None

    def dragEnterEvent(self, e):
        if self.overlay.isVisible():
            return
        if self._dropped_video(e):
            e.acceptProposedAction()
            self.drop_hint.show_hint()

    def dragMoveEvent(self, e):
        if self._dropped_video(e):
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.drop_hint.hide_hint()

    def dropEvent(self, e):
        self.drop_hint.hide_hint()
        p = self._dropped_video(e)
        if not p:
            self.say("Das ist keine Videodatei", RED)
            return
        e.acceptProposedAction()
        self.open_dropped(p)

    def open_dropped(self, p):
        if self.server is None:
            self.pending_video = p
            self.say("Wähle jetzt einen Server – dein Video wird danach geladen", AMBER)
            self.go_page(0)
            return
        if self.stack.cur != 1:
            self.go_page(1)
        QTimer.singleShot(60, lambda: self.load_video(p))

    # ------------------------------------------------------------ Seitenleiste
    def build_sidebar(self):
        sb = QWidget()
        sb.setFixedWidth(224)
        lay = QVBoxLayout(sb)
        lay.setContentsMargins(6, 8, 6, 4)
        lay.setSpacing(2)
        logo = QHBoxLayout()
        logo.setContentsMargins(6, 0, 0, 0)
        logo.setSpacing(10)
        lg = QLabel()
        lg.setPixmap(QPixmap(resource("assets", "logo.png")).scaled(36, 36, Qt.KeepAspectRatio,
                                                                      Qt.SmoothTransformation))
        logo.addWidget(lg)
        logo.addWidget(label("Kill App", 15, True))
        logo.addStretch(1)
        lay.addLayout(logo)
        lay.addSpacing(26)
        lay.addWidget(self.caption("Menü"))
        self.nav = []
        for i, (ic, tx) in enumerate((("server", "Server"), ("film", "Editor"), ("folder", "Bibliothek"),
                                      ("gear", "Einstellungen"))):
            n = NavItem(ic, tx)
            n.clicked.connect(lambda i=i: self.nav_clicked(i))
            lay.addWidget(n)
            self.nav.append(n)
        self.nav[1].set_nav_enabled(False)
        lay.addSpacing(18)
        lay.addWidget(self.caption("Meine Server"))
        self.side_scroll = QScrollArea()
        self.side_scroll.setWidgetResizable(True)
        self.side_scroll.setFrameShape(QScrollArea.NoFrame)
        self.side_scroll.viewport().setAutoFillBackground(False)
        lay.addWidget(self.side_scroll, 1)
        self.btn_update = PopButton("Update installieren", "upload", "glass", 40)
        self.btn_update.clicked.connect(self.do_update)
        self.btn_update.hide()
        lay.addWidget(self.btn_update)
        nb = PopButton("Neuer Server", "plus", "accent", 44)
        nb.clicked.connect(lambda: self.open_add_server())
        lay.addWidget(nb)
        self.ver = label(f"Version {core.APP_VERSION}", 9, color=DIM)
        self.ver.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.ver)
        return sb

    def caption(self, text):
        lb = label(text, 10, True, MUT)
        lb.setContentsMargins(10, 4, 0, 4)
        return lb

    def render_side_servers(self):
        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(0)
        for s in self.all_servers():
            it = SideServer(s["name"], bool(self.server and self.server["id"] == s["id"]), s["id"])
            it.clicked.connect(lambda s=s: self.enter_server(s))
            v.addWidget(it)
        v.addStretch(1)
        self.side_scroll.setWidget(host)

    def nav_clicked(self, i):
        if i == 1 and self.server is None:
            self.say("Wähle zuerst einen Server", AMBER)
            self.go_page(0)
            return
        self.go_page(i)

    def go_page(self, i, animate=True):
        cur = self.stack.cur
        if cur == 1 and i != 1 and self.playing:
            self.toggle_play()
        for k, n in enumerate(self.nav):
            n.set_selected(k == i)
        if animate:
            self.stack.go(i, 1 if i >= max(cur, 0) else -1)
        else:
            self.stack.go(i)
        if i == 1 and self.player:
            QTimer.singleShot(60, lambda: self.player.request_seek(self.pos))
        if i == 2:
            self.refresh_library()

    # ------------------------------------------------------------ Seite: Server
    def page_header(self, title, sub):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        v.addWidget(label(title, 24, True))
        v.addWidget(label(sub, 10.5, color=MUT))
        return w

    def build_server_page(self):
        main = Glass(radius=32, alpha=125)
        lay = QVBoxLayout(main)
        lay.setContentsMargins(30, 26, 22, 22)
        lay.addWidget(self.page_header("Server", "Wähle den Server, auf dem du aufgenommen hast – "
                                       "Kill App nutzt dann die passende Kill-Erkennung."))
        lay.addSpacing(14)
        self.srv_scroll = QScrollArea()
        self.srv_scroll.setWidgetResizable(True)
        self.srv_scroll.setFrameShape(QScrollArea.NoFrame)
        self.srv_scroll.viewport().setAutoFillBackground(False)
        lay.addWidget(self.srv_scroll, 1)
        return main

    def all_servers(self):
        out = []
        for s in BUILTIN_SERVERS:
            if s["id"] in self.st.get("hidden", []):
                continue
            d = dict(s)
            d["name"] = self.st["names"].get(s["id"], s["name"])
            out.append(d)
        return out + self.st["servers"]

    def srv_cols(self):
        avail = self.srv_scroll.viewport().width() or 900
        return max(1, min(4, (avail - 10) // (ServerCard.W + 2 * ServerCard.M + 4)))

    def render_servers(self):
        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        self.tags = {}
        items = self.all_servers()
        cols = self._cols = self.srv_cols()
        for i, s in enumerate(items):
            tag = "Aktiv" if self.server and self.server["id"] == s["id"] else (
                "Zuletzt genutzt" if s["id"] == self.st.get("last_server") else "")
            card = ServerCard(s["name"], KIND_TEXT.get(s["kind"], ""), tag, sid=s["id"])
            card.locked = not self.is_admin
            card.clicked.connect(lambda s=s: self.enter_server(s))
            card.edit.connect(lambda s=s: self.open_edit_server(s))
            card.delete.connect(lambda s=s: self.open_confirm_delete(s))
            grid.addWidget(card, i // cols, i % cols)
            self.tags[s["id"]] = card
        if self.is_admin:
            add = ServerCard(add=True)
            add.clicked.connect(lambda: self.open_add_server())
            grid.addWidget(add, len(items) // cols, len(items) % cols)
        grid.setColumnStretch(cols, 1)
        hidden = self.st.get("hidden", [])
        if hidden and self.is_admin:
            rb = PopButton(f"Gelöschte Standard-Server wiederherstellen ({len(hidden)})", "refresh", "glass", 38)
            rb.clicked.connect(self.restore_builtin)
            grid.addWidget(rb, len(items) // cols + 1, 0, 1, cols, Qt.AlignLeft)
        grid.setRowStretch(len(items) // cols + 2, 1)
        self.srv_scroll.setWidget(host)
        self.srv_host = host
        self.render_side_servers()

    def enter_server(self, s):
        self.server = s
        self.st["last_server"] = s["id"]
        self.ed_chip.set_server(s["name"], s["id"])
        self.nav[1].set_nav_enabled(True)
        self.save_settings()
        self.render_servers()
        self.go_page(1)
        if self.pending_video:
            pv, self.pending_video = self.pending_video, None
            QTimer.singleShot(260, lambda: self.load_video(pv))

    # ------------------------------------------------------------ Profilbild-Auswahl
    def avatar_picker(self, st, name_fn, sid=None):
        """Zeile mit Vorschau + Knoepfen zum Waehlen/Entfernen eines Profilbilds. Zustand in st."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        prev = QLabel()
        prev.setFixedSize(64, 64)
        row.addWidget(prev)
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(label("Profilbild (optional)", 10.5, True, MUT))
        btns = QHBoxLayout()
        btns.setSpacing(8)
        pick = PopButton("Bild wählen …", "folder", "glass", 36)
        rm = PopButton("Entfernen", None, "flat", 36)
        btns.addWidget(pick)
        btns.addWidget(rm)
        btns.addStretch(1)
        col.addLayout(btns)
        row.addLayout(col, 1)

        def refresh():
            pm = QPixmap(128, 128)
            pm.fill(Qt.transparent)
            pm.setDevicePixelRatio(2)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.Antialiasing)
            rect = QRectF(0, 0, 64, 64)
            if st.get("avatar"):
                im = QImage(st["avatar"])
                if not im.isNull():
                    side = min(im.width(), im.height())
                    im = im.copy((im.width() - side) // 2, (im.height() - side) // 2, side, side)
                    path = QPainterPath()
                    path.addRoundedRect(rect, 20, 20)
                    pp.setClipPath(path)
                    pp.drawImage(rect, im)
                    pp.end()
                    prev.setPixmap(pm)
                    return
            sid_eff = None if st.get("removed") else sid
            paint_avatar(pp, rect, name_fn() or "?", 20, 24, sid_eff)
            pp.end()
            prev.setPixmap(pm)

        def do_pick():
            p, _ = QFileDialog.getOpenFileName(self, "Profilbild wählen", "", "Bilder (*.png *.jpg *.jpeg *.webp *.bmp)")
            if p and not QImage(p).isNull():
                st["avatar"], st["removed"] = p, False
                refresh()
            elif p:
                self.say("Bild konnte nicht geladen werden", RED)

        def do_remove():
            st["avatar"], st["removed"] = None, True
            refresh()
        pick.clicked.connect(do_pick)
        rm.clicked.connect(do_remove)
        refresh()
        box.refresh = refresh
        return box

    # ------------------------------------------------------------ Server hinzufuegen / bearbeiten
    def style_edit(self, le):
        le.setFont(app_font(11))
        le.setMinimumHeight(44)

    def open_add_server(self, state=None):
        st = state or {"name": "", "kind": "Roter Kill-X", "result": None, "avatar": None}
        card = DialogCard("Neuer Server", "Lege fest, woran Kill App auf diesem Server einen Kill erkennt.")
        card.body.addWidget(label("Name", 10.5, True, MUT))
        name = QLineEdit(st["name"])
        name.setPlaceholderText("z. B. Mein Server")
        self.style_edit(name)
        card.body.addWidget(name)
        ap = self.avatar_picker(st, name.text)
        name.textChanged.connect(lambda _: ap.refresh())
        card.body.addWidget(ap)
        card.body.addWidget(label("Erkennung", 10.5, True, MUT))
        seg = Segmented(["Roter Kill-X", "Kill-Meldung"])
        seg.set_current(st["kind"])
        card.body.addWidget(seg)
        info = label("", 10, color=MUT, wrap=True)
        card.body.addWidget(info)
        pick = PopButton("Screenshot der Kill-Meldung wählen …", "folder", "glass", 42)
        card.body.addWidget(pick)
        ok = label("✓  Meldung markiert" if st["result"] else "", 10.5, True, ACC_TEXT)
        card.body.addWidget(ok)

        def kind_changed(_=None):
            st["kind"] = seg.current()
            x = seg.current() == "Roter Kill-X"
            pick.setVisible(not x)
            ok.setVisible(not x)
            info.setText("Der rote X-Treffer in der Bildmitte (Hitmarker) zählt als Kill. Keine weitere Einrichtung "
                         "nötig." if x else
                         "Der Server zeigt bei einem Kill eine Meldung auf dem Bildschirm. Wähle einen Screenshot, auf "
                         "dem sie zu sehen ist (am besten ein Vollbild), und markiere drei Bereiche.")
            card.fit()
            self.overlay._center()
        seg.changed.connect(kind_changed)
        kind_changed()

        def do_pick():
            st["name"] = name.text()
            p, _ = QFileDialog.getOpenFileName(self, "Screenshot wählen", "", "Bilder (*.png *.jpg *.jpeg *.webp *.bmp)")
            if p:
                self.open_region_picker(p, lambda res: self.open_add_server({**st, "result": res}),
                                        lambda: self.open_add_server(st))
        pick.clicked.connect(do_pick)

        def save():
            nm = name.text().strip()
            st["name"] = nm
            if not nm:
                card.err.setText("Bitte einen Namen eingeben.")
                return
            if nm.lower() in [s["name"].lower() for s in self.all_servers()]:
                card.err.setText("Diesen Namen gibt es schon.")
                return
            sid = uuid.uuid4().hex[:8]
            if seg.current() == "Roter Kill-X":
                srv = {"id": sid, "name": nm, "kind": "xhit"}
            else:
                if not st["result"]:
                    card.err.setText("Bitte zuerst die Kill-Meldung markieren.")
                    return
                srv = self.write_notify_server(sid, nm, st["result"])
            if st.get("avatar"):
                save_avatar(sid, st["avatar"])
            self.st["servers"].append(srv)
            self.save_settings()
            self.sync_catalog()
            self.render_servers()
            self.overlay.dismiss()
            self.say(f"Server „{nm}“ hinzugefügt")
        cancel = PopButton("Abbrechen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        sv = PopButton("Speichern", "check", "accent", 40)
        sv.clicked.connect(save)
        card.btns.addWidget(cancel)
        card.btns.addWidget(sv)
        self.overlay.present(card)

    def write_notify_server(self, sid, name, r):
        d = os.path.join(core.DATA_DIR, "servers", sid)
        os.makedirs(d, exist_ok=True)
        im = r["bgr"]
        ax0, ay0, ax1, ay1 = r["anchor"]
        wx0, wy0, wx1, wy1 = r["word"]
        rx0, ry0, rx1, ry1 = r["region"]
        for fname, crop in (("anchor.png", im[ay0:ay1, ax0:ax1]), ("word.png", im[wy0:wy1, wx0:wx1])):
            cv2.imencode(".png", crop)[1].tofile(os.path.join(d, fname))
        return {"id": sid, "name": name, "kind": "notify", "ref_h": r["ref_h"],
                "region": [rx0 - ax0, ry0 - ay0, rx1 - ax0, ry1 - ay0]}

    def open_edit_server(self, s):
        builtin = s.get("builtin")
        card = DialogCard("Server bearbeiten", KIND_TEXT.get(s["kind"], ""))
        card.body.addWidget(label("Name", 10.5, True, MUT))
        name = QLineEdit(s["name"])
        self.style_edit(name)
        card.body.addWidget(name)
        ast = {"avatar": None, "removed": False}
        ap = self.avatar_picker(ast, name.text, s["id"])
        name.textChanged.connect(lambda _: ap.refresh())
        card.body.addWidget(ap)
        if s["kind"] == "notify":
            pick = PopButton("Kill-Meldung neu markieren …", "crosshair", "glass", 42)

            def do_pick():
                p, _ = QFileDialog.getOpenFileName(self, "Screenshot wählen", "",
                                                   "Bilder (*.png *.jpg *.jpeg *.webp *.bmp)")
                if p:
                    self.open_region_picker(p, lambda res: self._edit_after_pick(s, name.text(), res),
                                            lambda: self.open_edit_server(s))
            pick.clicked.connect(do_pick)
            card.body.addWidget(pick)

        def save():
            nm = name.text().strip()
            if not nm:
                card.err.setText("Bitte einen Namen eingeben.")
                return
            others = [x["name"].lower() for x in self.all_servers() if x["id"] != s["id"]]
            if nm.lower() in others:
                card.err.setText("Diesen Namen gibt es schon.")
                return
            if ast["avatar"]:
                save_avatar(s["id"], ast["avatar"])
            elif ast["removed"]:
                remove_avatar(s["id"])
            self.rename_server(s, nm)
            self.overlay.dismiss()
        dl = PopButton("Löschen", "trash", "danger", 40)
        dl.clicked.connect(lambda: self.open_confirm_delete(s))
        card.btns.insertWidget(0, dl)
        cancel = PopButton("Abbrechen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        sv = PopButton("Speichern", "check", "accent", 40)
        sv.clicked.connect(save)
        card.btns.addWidget(cancel)
        card.btns.addWidget(sv)
        self.overlay.present(card)

    def _edit_after_pick(self, s, name, res):
        srv = self.write_notify_server(s["id"], name or s["name"], res)
        for i, x in enumerate(self.st["servers"]):
            if x["id"] == s["id"]:
                self.st["servers"][i] = srv
        self.save_settings()
        self.sync_catalog()
        if self.server and self.server["id"] == s["id"]:
            self.server = srv
        self.render_servers()
        self.overlay.dismiss()
        self.say("Kill-Meldung aktualisiert")

    def rename_server(self, s, nm):
        if s.get("builtin"):
            self.st["names"][s["id"]] = nm
        else:
            for x in self.st["servers"]:
                if x["id"] == s["id"]:
                    x["name"] = nm
        if self.server and self.server["id"] == s["id"]:
            self.server["name"] = nm
            self.ed_chip.set_server(nm, s["id"])
        self.save_settings()
        self.sync_catalog()
        self.render_servers()
        self.say("Gespeichert")

    def open_confirm_delete(self, s):
        card = DialogCard("Server löschen?", f"„{s['name']}“ wird entfernt." + (" Du kannst ihn später wiederherstellen." if s.get("builtin") else " Das kann nicht rückgängig gemacht werden."), 460)
        cancel = PopButton("Abbrechen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        yes = PopButton("Löschen", "trash", "danger", 40)
        yes.clicked.connect(lambda: self.delete_server(s))
        card.btns.addWidget(cancel)
        card.btns.addWidget(yes)
        self.overlay.present(card)

    def delete_server(self, s):
        if s.get("builtin"):
            if s["id"] not in self.st["hidden"]:
                self.st["hidden"].append(s["id"])
        else:
            self.st["servers"] = [x for x in self.st["servers"] if x["id"] != s["id"]]
            shutil.rmtree(os.path.join(core.DATA_DIR, "servers", s["id"]), ignore_errors=True)
        remove_avatar(s["id"])
        if self.st.get("last_server") == s["id"]:
            self.st["last_server"] = ""
        if self.server and self.server["id"] == s["id"]:
            self.server = None
            self.nav[1].set_nav_enabled(False)
            self.ed_chip.set_server("")
            if self.stack.cur == 1:
                self.go_page(0)
        self.save_settings()
        self.sync_catalog()
        self.render_servers()
        self.overlay.dismiss()
        self.say("Server gelöscht")

    def restore_builtin(self):
        self.st["hidden"] = []
        self.save_settings()
        self.sync_catalog()
        self.render_servers()
        self.say("Standard-Server wiederhergestellt")

    def open_region_picker(self, path, done, back):
        pm = QPixmap(path)
        if pm.isNull():
            self.say("Bild konnte nicht geladen werden", RED)
            return
        steps = [("1 / 3  Fester Teil der Meldung",
                  "Ziehe einen Rahmen um etwas, das in JEDER Kill-Meldung gleich aussieht (z. B. Überschrift "
                  "oder Symbol)."),
                 ("2 / 3  Ganze Meldung", "Ziehe einen Rahmen um die komplette Meldungsbox mit dem Text."),
                 ("3 / 3  Kill-Wort", "Ziehe einen Rahmen um ein Wort, das NUR bei Kills vorkommt "
                                      "(z. B. „getötet“).")]
        canvas = PickCanvas(pm)
        card = DialogCard(steps[0][0], steps[0][1], max(600, canvas.dw + 64))
        title = card.lay.itemAt(0).widget()
        helpl = card.lay.itemAt(1).widget()
        card.body.addWidget(canvas, 0, Qt.AlignHCenter)
        back_b = PopButton("Zurück", None, "glass", 40)
        next_b = PopButton("Weiter", None, "accent", 40)
        next_b.setEnabled(False)
        cancel = PopButton("Abbrechen", None, "flat", 40)
        cancel.clicked.connect(back)
        card.btns.insertWidget(0, cancel)
        card.btns.addWidget(back_b)
        card.btns.addWidget(next_b)

        def show():
            title.setText(steps[canvas.step][0])
            helpl.setText(steps[canvas.step][1])
            next_b.setText("Fertig" if canvas.step == 2 else "Weiter")
            next_b.setEnabled(canvas.boxes[canvas.step] is not None)
            back_b.setEnabled(canvas.step > 0)
            card.err.setText("")
            canvas.update()
        canvas.released.connect(lambda b: (next_b.setEnabled(True), card.err.setText("")))

        def nxt():
            if canvas.step < 2:
                canvas.step += 1
                show()
                return
            a, rg, wd = canvas.boxes
            if not (rg[0] <= wd[0] and rg[1] <= wd[1] and wd[2] <= rg[2] and wd[3] <= rg[3]):
                card.err.setText("Das Kill-Wort muss innerhalb der ganzen Meldung liegen.")
                return
            img = pm.toImage().convertToFormat(QImage.Format_RGB888)
            arr = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width() * 3]
            bgr = cv2.cvtColor(arr.reshape(img.height(), img.width(), 3).copy(), cv2.COLOR_RGB2BGR)
            H = img.height()
            done({"anchor": a, "region": rg, "word": wd, "bgr": bgr, "ref_h": H if H >= 700 else 1080})

        def prv():
            if canvas.step:
                canvas.step -= 1
                show()
        next_b.clicked.connect(nxt)
        back_b.clicked.connect(prv)
        show()
        self.overlay.present(card)

    # ------------------------------------------------------------ Seite: Editor
    def build_editor_page(self):
        pg = QWidget()
        outer = QVBoxLayout(pg)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(14)
        outer.addLayout(top, 1)

        # ---- Hauptfeld
        main = Glass(radius=32, alpha=125)
        top.addWidget(main, 1)
        ml = QVBoxLayout(main)
        ml.setContentsMargins(26, 22, 26, 20)
        ml.setSpacing(12)
        hdr = QHBoxLayout()
        hdr.setSpacing(12)
        pill = Glass(radius=24, alpha=170)
        pill.setFixedHeight(48)
        pl = QHBoxLayout(pill)
        pl.setContentsMargins(16, 0, 8, 0)
        pl.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(icon_pixmap("film", T.icon, 20))
        self.pill_icon = ic
        pl.addWidget(ic)
        self.file_lbl = ElidedLabel("Noch kein Video geladen", 10.5, True)
        self.meta_lbl = ElidedLabel("", 9.5, False, MUT)
        self.meta_lbl.hide()
        pl.addWidget(self.file_lbl, 1)
        self.open_btn = PopButton("Video öffnen", "folder", "accent", 34)
        self.open_btn.clicked.connect(self.open_video)
        pl.addWidget(self.open_btn)
        hdr.addWidget(pill, 1)
        self.ed_chip = ServerChip()
        self.ed_chip.clicked.connect(lambda: self.go_page(0))
        hdr.addWidget(self.ed_chip)
        ml.addLayout(hdr)

        tr = QHBoxLayout()
        tr.addWidget(label("Deine Clips", 22, True))
        self.count_lbl = label("0", 9.5, True, "badge")
        tr.addWidget(self.count_lbl)
        tr.addStretch(1)
        self.total_lbl = label("", 10, color=MUT)
        tr.addWidget(self.total_lbl)
        ml.addLayout(tr)

        mid = QHBoxLayout()
        mid.setSpacing(18)
        ml.addLayout(mid, 1)
        self.video = VideoWidget()
        self.video.setMinimumSize(320, 220)
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        mid.addWidget(self.video, 1)
        cta = QWidget(self.video)
        cl = QVBoxLayout(cta)
        cl.setAlignment(Qt.AlignCenter)
        ib = QLabel()
        ib.setPixmap(icon_pixmap("film", QColor(255, 255, 255, 235), 42, 1.6))
        ib.setFixedSize(84, 84)
        ib.setAlignment(Qt.AlignCenter)
        ib.setProperty("role", "ctaicon")
        cl.addWidget(ib, 0, Qt.AlignHCenter)
        cl.addSpacing(10)
        t1 = label("Los geht’s", 18, True, "onvideo")
        t2 = label("Wähle deine Aufnahme aus.", 10.5, False, "onvideo2")
        cl.addWidget(t1, 0, Qt.AlignHCenter)
        cl.addWidget(t2, 0, Qt.AlignHCenter)
        cl.addSpacing(10)
        ob = PopButton("Video öffnen", "folder", "accent", 44)
        ob.clicked.connect(self.open_video)
        cl.addWidget(ob, 0, Qt.AlignHCenter)
        self.cta = cta
        self.video.cover = cta

        self.clip_scroll = QScrollArea()
        self.clip_scroll.setWidgetResizable(True)
        self.clip_scroll.setFrameShape(QScrollArea.NoFrame)
        self.clip_scroll.viewport().setAutoFillBackground(False)
        self.clip_scroll.setFixedWidth(290)
        mid.addWidget(self.clip_scroll)

        tlc = QWidget()
        tv = QVBoxLayout(tlc)
        tv.setContentsMargins(0, 2, 0, 0)
        tv.setSpacing(4)
        self.timeline = Timeline(self)
        self.timeline.seek.connect(self.seek)
        self.timeline.pick.connect(lambda i: self.select(i, seek=False))
        self.timeline.edited.connect(self.clip_changed)
        tv.addWidget(self.timeline)
        er = QHBoxLayout()
        er.setContentsMargins(6, 0, 0, 0)
        er.setSpacing(6)
        for tx, ic_, fn in (("Start hier  [I]", None, lambda: self.set_edge(0)),
                            ("Ende hier  [O]", None, lambda: self.set_edge(1)),
                            ("Clip hier", "plus", self.add_clip)):
            b = PopButton(tx, ic_, "glass", 36)
            b.clicked.connect(fn)
            er.addWidget(b)
        er.addWidget(ElidedLabel("Scrollen = Zoom · Ränder ziehen = Clip anpassen", 9, False, DIM), 1)
        tv.addLayout(er)
        ml.addWidget(tlc)

        # ---- rechte Spalte
        rw = QWidget()
        rw.setFixedWidth(304)
        rl = QVBoxLayout(rw)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(14)
        top.addWidget(rw)

        g, lay = panel("Kills suchen")
        self.s_pre = SliderRow("Vor dem Kill", 0, 10, 100, self.st["pre"], lambda v: f"{v:.1f} s")
        self.s_post = SliderRow("Nach dem Kill", 0, 10, 100, self.st["post"], lambda v: f"{v:.1f} s")
        names = ["", "sehr niedrig", "niedrig", "niedrig", "mittel", "mittel", "mittel+", "hoch", "hoch", "sehr hoch",
                 "max"]
        self.s_sens = SliderRow("Empfindlichkeit", 1, 10, 0, self.st["sens"], lambda v: names[int(round(v))])
        for s_ in (self.s_pre, self.s_post, self.s_sens):
            lay.addWidget(s_)
        self.scan_btn = PopButton("Kills suchen", "crosshair", "accent", 46)
        self.scan_btn.clicked.connect(self.start_scan)
        lay.addWidget(self.scan_btn)
        self.scan_bar = Bar()
        lay.addWidget(self.scan_bar)
        self.status = ElidedLabel("Bereit", 9.5, False, MUT)
        lay.addWidget(self.status)
        rl.addWidget(g)

        g, lay = panel("Export")
        self.seg_mode = Segmented(list(MODES), height=36)
        self.seg_mode.set_current(self.st["mode"] if self.st["mode"] in MODES else "Genau")
        self.mode_desc = QLabel()
        self.mode_desc.setWordWrap(True)
        self.mode_desc.setFont(app_font(9))
        self.mode_desc.setProperty("role", "muted")
        self.mode_desc.setMinimumHeight(30)
        self.seg_mode.changed.connect(lambda v: self.mode_desc.setText(MODES[v][1]))
        self.mode_desc.setText(MODES[self.seg_mode.current()][1])
        lay.addWidget(self.seg_mode)
        lay.addWidget(self.mode_desc)
        lay.addWidget(self.make_dir_row())
        self.exp_all = PopButton("Alle exportieren", "upload", "accent", 44)
        self.exp_all.clicked.connect(lambda: self.export(False))
        lay.addWidget(self.exp_all)
        b = PopButton("Nur ausgewählten Clip", None, "glass", 40)
        b.clicked.connect(lambda: self.export(True))
        lay.addWidget(b)
        self.exp_bar = Bar()
        lay.addWidget(self.exp_bar)
        rl.addWidget(g)
        rl.addStretch(1)

        # ---- Player-Leiste
        bar = Glass(radius=28, alpha=130)
        bar.setFixedHeight(104)
        bv = QVBoxLayout(bar)
        bv.setContentsMargins(22, 8, 22, 10)
        bv.setSpacing(2)
        self.seekbar = SeekBar(self)
        self.seekbar.seek.connect(self.seek)
        bv.addWidget(self.seekbar)
        row = QHBoxLayout()
        row.setSpacing(10)
        bv.addLayout(row, 1)
        left = QHBoxLayout()
        left.setSpacing(12)
        self.bar_thumb = QLabel()
        self.bar_thumb.setFixedSize(58, 46)
        self.bar_thumb.setProperty("role", "thumbbg")
        left.addWidget(self.bar_thumb)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        self.bar_title = ElidedLabel("Kein Video", 10.5, True)
        self.bar_sub = ElidedLabel("", 9.5, False, MUT)
        tcol.addWidget(self.bar_title)
        tcol.addWidget(self.bar_sub)
        left.addLayout(tcol)
        lw = QWidget()
        lw.setLayout(left)
        lw.setFixedWidth(300)
        row.addWidget(lw)
        row.addStretch(1)
        b = PopButton("", "prev", "flat", 42, icon_only=True)
        b.clicked.connect(lambda: self.step_clip(-1))
        row.addWidget(b)
        b = PopButton("−1 s", None, "flat", 42)
        b.clicked.connect(lambda: self.seek(self.pos - 1))
        row.addWidget(b)
        self.play_btn = PopButton("", "play", "accent", 50, icon_only=True)
        self.play_btn.clicked.connect(self.toggle_play)
        row.addWidget(self.play_btn)
        b = PopButton("+1 s", None, "flat", 42)
        b.clicked.connect(lambda: self.seek(self.pos + 1))
        row.addWidget(b)
        b = PopButton("", "next", "flat", 42, icon_only=True)
        b.clicked.connect(lambda: self.step_clip(1))
        row.addWidget(b)
        row.addStretch(1)
        rr = QHBoxLayout()
        rr.setSpacing(10)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.addStretch(1)
        self.loop_btn = PopButton("", "refresh", "glass", 38, icon_only=True)
        self.loop_btn.setToolTip("Clip in der Vorschau wiederholen")
        rr.addWidget(self.loop_btn)
        self.speed_seg = Segmented(["0.5×", "1×", "2×"], height=36)
        self.speed_seg.setFixedWidth(170)
        self.speed_seg.set_current("1×")
        self.speed_seg.changed.connect(self.set_speed)
        rr.addWidget(self.speed_seg)
        rw2 = QWidget()
        rw2.setLayout(rr)
        rw2.setFixedWidth(300)
        row.addWidget(rw2)
        outer.addWidget(bar)
        self.rebuild_cards()
        return pg

    # ------------------------------------------------------------ Seite: Einstellungen
    def build_settings_page(self):
        main = Glass(radius=32, alpha=125)
        lay = QVBoxLayout(main)
        lay.setContentsMargins(30, 26, 22, 22)
        lay.addWidget(self.page_header("Einstellungen", "Verhalten der App und Updates."))
        lay.addSpacing(10)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QScrollArea.NoFrame)
        sc.viewport().setAutoFillBackground(False)
        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        v = QVBoxLayout(host)
        v.setSpacing(14)
        v.setContentsMargins(0, 0, 10, 0)
        v.addWidget(self.build_appearance_panel())
        if core.ACCOUNT_URL:
            v.addWidget(self.build_account_panel())

        def toggle_row(parent_lay, title, sub, checked):
            r = QHBoxLayout()
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(label(title, 11, True))
            col.addWidget(label(sub, 9.5, color=DIM))
            r.addLayout(col, 1)
            t = Toggle(checked)
            r.addWidget(t)
            parent_lay.addLayout(r)
            return t

        g, gl = panel("Allgemein", alpha=150)
        self.tg_loop = toggle_row(gl, "Clip in der Vorschau wiederholen", "Spielt den gewählten Clip in Schleife.",
                                  self.st["loop"])
        self.tg_open = toggle_row(gl, "Ordner nach dem Export öffnen", "Zeigt die fertigen Clips im Explorer.",
                                  self.st["open"])
        v.addWidget(g)
        g, gl = panel("Kill-Suche", alpha=150)
        self.tg_gpu = toggle_row(gl, "GPU-Beschleunigung nutzen", "Beschleunigt das Dekodieren beim Suchen, wenn "
                                 "möglich.", self.st["gpu"])
        self.tg_thorough = toggle_row(gl, "Gründliche Suche (langsamer)", "Sucht Bild für Bild. Nur nötig, wenn "
                                      "eine sehr kurze Meldung übersehen wurde.", self.st["thorough"])
        v.addWidget(g)
        g, gl = panel("Export", "Wohin die Clips gespeichert werden", alpha=150)
        gl.addWidget(self.make_dir_row())
        v.addWidget(g)
        g, gl = panel("Updates", alpha=150)
        self.tg_upd = toggle_row(gl, "Beim Start nach Updates suchen", "Neue Versionen werden automatisch gefunden.",
                                 self.st["autoupd"])
        r = QHBoxLayout()
        r.addWidget(label(f"Installierte Version: {core.APP_VERSION}", 10.5, color=MUT), 1)
        b = PopButton("Nach Updates suchen", "refresh", "glass", 40)
        b.clicked.connect(lambda: self.start_update_check(True))
        r.addWidget(b)
        gl.addLayout(r)
        v.addWidget(g)
        g, gl = panel("Tastenkürzel", alpha=150)
        for k, t in (("Leertaste", "Play / Pause"), ("← →", "±1 Sekunde (Shift: 1 Bild)"),
                     ("↑ ↓", "Vorheriger / nächster Clip"), ("I / O", "Start / Ende setzen"), ("Entf", "Clip löschen")):
            rr = QHBoxLayout()
            kl = label(k, 10.5, True, ACC_TEXT)
            kl.setFixedWidth(110)
            rr.addWidget(kl)
            rr.addWidget(label(t, 10.5))
            rr.addStretch(1)
            gl.addLayout(rr)
        v.addWidget(g)
        v.addStretch(1)
        sc.setWidget(host)
        lay.addWidget(sc, 1)
        # Schleifen-Schalter in der Player-Leiste mit dem Einstellungs-Schalter koppeln
        self.tg_loop.toggled.connect(self._sync_loop)
        self.loop_btn.clicked.connect(lambda: self.tg_loop.setChecked(not self.tg_loop.isChecked()))
        self._sync_loop(self.tg_loop.isChecked())
        return main

    def _sync_loop(self, on):
        self.loop_btn.set_kind("accent" if on else "glass")

    # ------------------------------------------------------------ Darstellung
    def apply_theme(self, **kw):
        """Aendert Modus/Farben sofort in der ganzen App und merkt sie sich."""
        self.st.update(kw)
        T.configure(self.st["dark"], self.st["acc"], self.st["acc2"], self.st["bg"])
        QApplication.instance().setStyleSheet(T.qss())
        self.bg.refresh()
        self.pill_icon.setPixmap(icon_pixmap("film", T.icon, 20))
        for w in self.findChildren(QWidget):
            w.update()
        self.save_settings()

    def pick_color(self, key, title):
        c = QColorDialog.getColor(QColor(self.st[key]), self, title)
        if c.isValid():
            self.apply_theme(**{key: c.name()})

    def build_appearance_panel(self):
        g, lay = panel("Darstellung", "Hell oder dunkel und das Farbthema der ganzen App", alpha=150)
        r = QHBoxLayout()
        r.addWidget(label("Design", 11, True), 1)
        self.seg_dark = Segmented(["Hell", "Dunkel"], height=36)
        self.seg_dark.setFixedWidth(200)
        self.seg_dark.set_current("Dunkel" if self.st["dark"] else "Hell")
        self.seg_dark.changed.connect(lambda v: self.apply_theme(dark=(v == "Dunkel")))
        r.addWidget(self.seg_dark)
        lay.addLayout(r)
        lay.addSpacing(4)
        lay.addWidget(label("Farbthema", 10.5, True, MUT))
        pr = QHBoxLayout()
        pr.setSpacing(8)
        for name, pre in PRESETS.items():
            pc = PresetCard(name, pre)
            pc.clicked.connect(lambda pre=pre: self.apply_theme(**pre))
            pr.addWidget(pc)
        pr.addStretch(1)
        lay.addLayout(pr)
        return g

    # ------------------------------------------------------------ Konten
    @property
    def is_admin(self):
        return self.role in (None, "admin")

    def builtin_ids(self):
        return [s["id"] for s in BUILTIN_SERVERS]

    def start_account(self):
        if not core.ACCOUNT_URL:
            return
        tok = self.st.get("acct_token")
        if not tok:
            self.show_login(self.pending_invite)
            return

        def work():
            try:
                me = auth.call("GET", "/me", token=tok)
                self.ui(self.on_login_ok, me["name"], me["role"], tok, False)
            except auth.Offline:
                if self.st.get("acct_role"):
                    self.ui(self.on_login_ok, self.st.get("acct_name", ""), self.st["acct_role"], tok, True)
                else:
                    self.ui(self.show_login, self.pending_invite, "Keine Verbindung zum Konto-Server.")
            except auth.ApiError:
                self.ui(self.show_login, self.pending_invite)
        threading.Thread(target=work, daemon=True).start()

    def show_login(self, invite="", note=""):
        self.login_open = True
        card = DialogCard("Willkommen bei Kill App", "Melde dich an, um die App zu nutzen.", 470)
        seg = Segmented(["Anmelden", "Neues Konto"])
        card.body.addWidget(seg)
        l_inv = label("Einladungscode (steht im Link, den du bekommen hast)", 10.5, True, MUT)
        inv = QLineEdit(invite)
        l_user = label("Benutzername", 10.5, True, MUT)
        user = QLineEdit()
        l_pw = label("Passwort", 10.5, True, MUT)
        pw = QLineEdit()
        pw.setEchoMode(QLineEdit.Password)
        for w_ in (inv, user, pw):
            self.style_edit(w_)
        for w_ in (l_inv, inv, l_user, user, l_pw, pw):
            card.body.addWidget(w_)
        if note:
            card.err.setText(note)
        go = PopButton("Anmelden", "check", "accent", 44)
        card.btns.addWidget(go)
        mode = {"new": bool(invite)}

        def apply_mode(first=False):
            new = mode["new"]
            for w_ in (l_inv, inv):
                w_.setVisible(new)
            go.setText("Konto erstellen" if new else "Anmelden")
            card.err.setText("" if first and not note else card.err.text() if first else "")
            card.fit()
            self.overlay._center()

        def on_seg(name):
            mode["new"] = name == "Neues Konto"
            apply_mode()
        seg.changed.connect(on_seg)
        if invite:
            seg.set_current("Neues Konto")
        apply_mode(True)

        def submit():
            u, p = user.text().strip(), pw.text()
            if not u or not p:
                card.err.setText("Bitte Benutzername und Passwort eingeben.")
                return
            go.setEnabled(False)
            card.err.setText("")
            new = mode["new"]

            def work():
                try:
                    if new:
                        r = auth.call("POST", "/redeem", {"invite": inv.text().strip(), "username": u, "password": p})
                    else:
                        r = auth.call("POST", "/login", {"username": u, "password": p})
                    self.ui(self.on_login_ok, r["name"], r["role"], r["token"], False, True)
                except auth.ApiError as e:
                    self.ui(lambda m: (card.err.setText(m), go.setEnabled(True)), str(e))
            threading.Thread(target=work, daemon=True).start()
        go.clicked.connect(submit)
        for w_ in (inv, user, pw):
            w_.returnPressed.connect(submit)
        self.overlay.present(card)

    def on_login_ok(self, name, role, token, offline, dismiss=False):
        self.st.update(acct_token=token, acct_role=role, acct_name=name)
        self.role = role
        self.pending_invite = ""
        if self.login_open or dismiss:
            self.login_open = False
            self.overlay.dismiss()
        self.save_settings()
        self.refresh_account_panel()
        self.render_servers()
        if offline:
            self.say("Offline – gespeicherte Server werden benutzt")
            return
        self.say(f"Angemeldet als {name}")

        def work():
            try:
                c = auth.call("GET", "/catalog", token=token)
                self.ui(self.apply_remote_catalog, c)
            except auth.ApiError as e:
                if e.status == 401:
                    self.ui(self.force_logout)
        threading.Thread(target=work, daemon=True).start()

    def apply_remote_catalog(self, c):
        if not c.get("data"):
            if self.is_admin and self.role:
                self.sync_catalog()
            return
        if self.role == "admin" and (self.st.get("cat_dirty") or c["ver"] == self.st.get("cat_ver")):
            if self.st.get("cat_dirty"):
                self.sync_catalog()
            return
        if c["ver"] == self.st.get("cat_ver") and self.role != "user":
            return
        auth.unpack_catalog(c["data"], self.st, avatar_path, self.builtin_ids())
        self.st["cat_ver"] = c["ver"]
        _AVATAR_CACHE.clear()
        self.save_settings()
        if self.server and self.server["id"] not in [s["id"] for s in self.all_servers()]:
            self.server = None
            self.nav[1].set_nav_enabled(False)
            self.ed_chip.set_server("")
        self.render_servers()

    def sync_catalog(self):
        """Admin: aktuelle Server-Liste zum Konto-Server schicken (alle anderen bekommen sie beim Start)."""
        if not core.ACCOUNT_URL or self.role != "admin":
            return
        data = auth.pack_catalog(self.st, avatar_path, self.builtin_ids())
        tok = self.st.get("acct_token")
        self.st["cat_dirty"] = True

        def work():
            try:
                r = auth.call("PUT", "/catalog", {"data": data}, token=tok, timeout=30)
                self.ui(self._synced, r.get("ver", 0))
            except auth.ApiError:
                self.ui(self.say, "Server-Liste konnte nicht hochgeladen werden (wird später erneut versucht)", RED)
        threading.Thread(target=work, daemon=True).start()

    def _synced(self, ver):
        self.st["cat_ver"], self.st["cat_dirty"] = ver, False
        self.save_settings()

    def force_logout(self):
        tok = self.st.pop("acct_token", None)
        if tok:
            threading.Thread(target=lambda: _quiet(lambda: auth.call("POST", "/logout", token=tok)), daemon=True).start()
        self.st.pop("acct_role", None)
        self.st.pop("acct_name", None)
        self.save_settings()
        self.role = "user"
        self.render_servers()
        self.show_login()

    def build_account_panel(self):
        self.acct_g, self.acct_lay = panel("Konto", alpha=150)
        self.refresh_account_panel()
        return self.acct_g

    def refresh_account_panel(self):
        if not hasattr(self, "acct_lay"):
            return
        lay = self.acct_lay
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._clear_layout(it.layout())
        if not self.role:
            lay.addWidget(label("Nicht angemeldet.", 10.5, color=MUT))
            return
        who = self.st.get("acct_name", "")
        r = QHBoxLayout()
        r.addWidget(label(f"Angemeldet als {who} ({'Admin' if self.role == 'admin' else 'Benutzer'})", 11, True), 1)
        out = PopButton("Abmelden", None, "glass", 40)
        out.clicked.connect(self.force_logout)
        r.addWidget(out)
        lay.addLayout(r)
        if self.role == "admin":
            inv = PopButton("Einladung für eine Person erstellen", "plus", "accent", 42)
            inv.clicked.connect(self.open_invite)
            lay.addWidget(inv)
            ub = PopButton("Konten anzeigen", "refresh", "glass", 40)
            ub.clicked.connect(self.load_users)
            lay.addWidget(ub)
            self.users_box = QVBoxLayout()
            self.users_box.setSpacing(6)
            lay.addLayout(self.users_box)

    def _clear_layout(self, l):
        while l.count():
            it = l.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._clear_layout(it.layout())

    def open_invite(self):
        card = DialogCard("Einladung erstellen", "Für wen ist der Zugang? Du bekommst einen Download-Link, den du verschicken kannst.", 520)
        card.body.addWidget(label("Name der Person (nur für dich zur Übersicht)", 10.5, True, MUT))
        nm = QLineEdit()
        self.style_edit(nm)
        card.body.addWidget(nm)
        cancel = PopButton("Schließen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        go = PopButton("Link erstellen", "check", "accent", 40)
        card.btns.addWidget(cancel)
        card.btns.addWidget(go)
        tok = self.st.get("acct_token")

        def make():
            go.setEnabled(False)

            def work():
                try:
                    r = auth.call("POST", "/invite", {"label": nm.text().strip()}, token=tok)
                    self.ui(shown, r)
                except auth.ApiError as e:
                    self.ui(lambda m: (card.err.setText(m), go.setEnabled(True)), str(e))
            threading.Thread(target=work, daemon=True).start()

        def shown(r):
            nm.setReadOnly(True)
            link = QLineEdit(r["link"])
            self.style_edit(link)
            link.setReadOnly(True)
            card.body.addWidget(label("Diesen Link schickst du weiter (14 Tage gültig, einmal benutzbar):", 10.5, True, MUT))
            card.body.addWidget(link)
            go.setText("Link kopieren")
            go.setEnabled(True)
            go.clicked.disconnect()
            go.clicked.connect(lambda: (QApplication.clipboard().setText(r["link"]), self.say("Link kopiert")))
            QApplication.clipboard().setText(r["link"])
            card.fit()
            self.overlay._center()
        go.clicked.connect(make)
        nm.returnPressed.connect(make)
        self.overlay.present(card)

    def load_users(self):
        tok = self.st.get("acct_token")

        def work():
            try:
                r = auth.call("GET", "/users", token=tok)
                self.ui(self.show_users, r["users"])
            except auth.ApiError as e:
                self.ui(self.say, str(e), RED)
        threading.Thread(target=work, daemon=True).start()

    def show_users(self, users):
        self._clear_layout(self.users_box)
        for u in users:
            row = QHBoxLayout()
            txt = u["name"] + (f"  ·  {u['label']}" if u.get("label") else "") + ("  ·  gesperrt" if u["disabled"] else "")
            row.addWidget(label(txt + ("  (Admin)" if u["role"] == "admin" else ""), 10.5, color=MUT), 1)
            if u["role"] != "admin":
                b1 = PopButton("Entsperren" if u["disabled"] else "Sperren", None, "glass", 36)
                b1.clicked.connect(lambda _=False, n=u["name"], d=not u["disabled"]: self.user_action("/users/disable", {"name": n, "disabled": d}))
                b2 = PopButton("Löschen", "trash", "danger", 36)
                b2.clicked.connect(lambda _=False, n=u["name"]: self.user_action("/users/delete", {"name": n}))
                row.addWidget(b1)
                row.addWidget(b2)
            self.users_box.addLayout(row)

    def user_action(self, path, data):
        tok = self.st.get("acct_token")

        def work():
            try:
                auth.call("POST", path, data, token=tok)
                self.ui(self.load_users)
            except auth.ApiError as e:
                self.ui(self.say, str(e), RED)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ Updates
    def start_update_check(self, manual):
        def work():
            try:
                res = check_update()
            except Exception:
                if manual:
                    self.ui(self.say, "Update-Suche fehlgeschlagen (Internet?)", RED)
                return
            if res:
                self.ui(self.show_update, *res)
            elif manual:
                self.ui(self.say, f"Du hast die neueste Version (v{core.APP_VERSION})")
        threading.Thread(target=work, daemon=True).start()

    def show_update(self, version, url):
        self.update_url = url
        self.btn_update.setText(f"Update v{version} installieren")
        self.btn_update.show()
        self.say(f"Neue Version v{version} verfügbar")

    def do_update(self):
        self.btn_update.setEnabled(False)
        self.btn_update.setText("Lade herunter …")

        def work():
            try:
                path = download_file(self.update_url, lambda p: self.ui(self.btn_update.setText,
                                                                         f"Lade … {int(p * 100)} %"))
                self.ui(self.run_installer, path)
            except Exception as e:
                self.ui(self.btn_update.setEnabled, True)
                self.ui(self.btn_update.setText, "Erneut versuchen")
                self.ui(self.say, f"Update fehlgeschlagen: {e}", RED)
        threading.Thread(target=work, daemon=True).start()

    def run_installer(self, path):
        subprocess.Popen([path, "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS"], creationflags=core.NOWIN)
        self.close()

    # ------------------------------------------------------------ Video
    def open_video(self):
        p, _ = QFileDialog.getOpenFileName(self, "Video wählen", "", "Video (*.mp4 *.mkv *.mov *.flv *.ts *.avi);;Alle (*)")
        if p:
            self.load_video(p)

    def video_size(self):
        dpr = self.video.devicePixelRatioF()
        return max(64, int((self.video.width() - 4) * dpr)), max(64, int((self.video.height() - 4) * dpr))

    def load_video(self, p):
        pl = Player(p)
        if not pl.ok:
            self.say("Video konnte nicht geöffnet werden", RED)
            return
        if self.playing:
            self.toggle_play()
        if self.player:
            self.player.stop()
        self.player, self.path = pl, p
        self._shown_id = 0
        if self.audio:
            self.audio.stop()
            self.audio.setSource(QUrl.fromLocalFile(p))
        self.fps, self.duration = pl.fps, pl.duration
        self.has_video = True
        self.video.has_video = True
        self.cta.hide()
        pl.size = self.video_size()
        pl.start()
        name = os.path.basename(p)
        self.file_lbl.setText(name)
        self.meta_lbl.setText(f"{pl.w}×{pl.h} · {pl.fps:.0f} fps · {fmt_short(pl.duration)}")
        self.bar_title.setText(name)
        self.bar_sub.setText(f"{pl.w}×{pl.h} · {pl.fps:.0f} fps")
        self._thumb_pending = True
        self.open_btn.setText("Wechseln")
        self.open_btn.set_kind("glass")
        self.clips, self.sel = [], None
        self.mark_in = self.mark_out = None
        self.rebuild_cards()
        self.scan_bar.set_value(0, False)
        self.set_status("Bereit – jetzt „Kills suchen“")
        self.timeline.set_view(0, 60)
        self.seek(0)

    def poll_player(self):
        pl = self.player
        if not pl or self.stack.cur != 1:
            return
        self._tick += 1
        if self._tick % 60 == 0:
            sz = self.video_size()
            if sz != pl.size:
                pl.size = sz
                if not self.playing:
                    pl.request_seek(self.pos)
        fr = pl.take()
        if fr:
            self.draw_frame(*fr)
        if self.playing and self._tick % 90 == 0:
            self.audio_sync(self.pos)
        if pl.ended and self.playing:
            self.playing = False
            if self.audio:
                self.audio.pause()
            self.play_btn.icon_name = "play"
            self.play_btn.update()

    def draw_frame(self, kind, pos, rgb, fid=None):
        if fid is not None and self.player is not None:
            if kind == "play":
                if fid < self.player.seek_id:
                    return              # veraltetes Abspiel-Bild (vor dem letzten Sprung) verwerfen
            elif fid < self._shown_id:
                return                  # Sprung-Bilder: ruhig anzeigen, solange sie nicht aelter als das letzte sind
            self._shown_id = max(self._shown_id, fid)
        self.video.set_frame(rgb)
        if self._thumb_pending:
            self._thumb_pending = False
            h, w = rgb.shape[:2]
            small = cv2.resize(rgb, (116, max(2, int(116 * h / w))), interpolation=cv2.INTER_AREA)
            sh, sw = small.shape[:2]
            pm = QPixmap.fromImage(QImage(small.data, sw, sh, 3 * sw, QImage.Format_RGB888).copy())
            self.bar_thumb.setPixmap(self._rounded(pm.scaled(58, 46, Qt.KeepAspectRatioByExpanding,
                                                              Qt.SmoothTransformation), 12))
        if kind == "play":
            self.pos = pos
            c = self.play_clip
            if c and pos >= c.end:
                if self.tg_loop.isChecked():
                    self.player.request_seek(c.start)
                    self.audio_sync(c.start, True)
                else:
                    self.toggle_play()
        self.video.time_text = fmt(self.pos)
        self.video.kill = any(abs(self.pos - c.kill) < 0.4 for c in self.clips)
        now = time.perf_counter()
        if now - self._last_tl > 0.07:
            self._last_tl = now
            if self.playing and self.pos > self.timeline.v1:
                self.timeline.set_view(self.pos - 2, self.pos - 2 + (self.timeline.v1 - self.timeline.v0))
            self.timeline.update()
            self.seekbar.update()
            self.bar_sub.setText(f"{fmt_short(self.pos)} / {fmt_short(self.duration)}")

    @staticmethod
    def _rounded(pm, r):
        out = QPixmap(pm.size())
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, pm.width(), pm.height()), r, r)
        p.setClipPath(path)
        p.drawPixmap(0, 0, pm)
        p.end()
        return out

    def seek(self, t):
        if not self.player:
            return
        t = min(max(0.0, t), max(0.0, self.duration - 0.1))
        self.pos = t
        self.player.request_seek(t)
        if self.playing:
            self.audio_sync(t, True)
        self.timeline.update()
        self.seekbar.update()

    def audio_sync(self, t, force=False):
        a = self.audio
        if not a:
            return
        try:
            if force or abs(a.position() / 1000.0 - t) > 0.25:
                a.setPosition(int(max(0.0, t) * 1000))
        except Exception:
            pass

    def audio_state(self):
        a = self.audio
        if not a:
            return
        try:
            if self.playing:
                a.setPlaybackRate(self.speed)
                a.setPosition(int(max(0.0, self.pos) * 1000))
                a.play()
            else:
                a.pause()
        except Exception:
            pass

    def set_speed(self, v):
        self.speed = float(v.replace("×", ""))
        if self.player:
            self.player.set_play(self.playing, self.speed)
        if self.audio:
            self.audio.setPlaybackRate(self.speed)

    def toggle_play(self):
        if not self.player:
            return
        self.playing = not self.playing
        self.play_btn.icon_name = "pause" if self.playing else "play"
        self.play_btn.update()
        if self.playing:
            self.play_clip = self.clip_for_play()
            if self.play_clip and self.pos >= self.play_clip.end - 0.05:
                self.seek(self.play_clip.start)
        self.player.set_play(self.playing, self.speed)
        self.audio_state()

    def clip_for_play(self):
        """Welcher Clip wird in Schleife abgespielt? Bevorzugt den ausgewaehlten (auch bei ueberlappenden Clips)."""
        if self.sel is not None and 0 <= self.sel < len(self.clips):
            c = self.clips[self.sel]
            if c.start - 0.05 <= self.pos <= c.end:
                return c
        inside = [c for c in self.clips if c.start <= self.pos <= c.end]
        return min(inside, key=lambda c: abs(c.kill - self.pos)) if inside else None

    # ------------------------------------------------------------ Kill-Suche
    def set_status(self, text, state="idle"):
        self.status.set_color({"ok": "ok", "bad": "bad", "warn": "warn", "busy": "busy", "idle": MUT}[state])
        self.status.setText(text)

    def start_scan(self):
        if self.scanning:
            self.cancel.set()
            return
        if not self.path:
            self.say("Bitte zuerst ein Video öffnen", RED)
            return
        if self.server is None:
            self.say("Wähle zuerst einen Server", AMBER)
            return
        if self.playing:
            self.toggle_play()
        self.scanning = True
        self.cancel.clear()
        self.scan_btn.setText("Abbrechen")
        self.scan_btn.icon_name = "close"
        self.scan_btn.set_kind("danger")
        self.scan_bar.set_value(0, False)
        self.set_status("Suche läuft …", "busy")
        if not self.clips:
            self.rebuild_cards()                          # Platzhalter statt leerer Liste
        args = (self.path, self.s_pre.value(), self.s_post.value(), self.server, int(round(self.s_sens.value())))
        gpu = self.tg_gpu.isChecked()
        thorough = self.tg_thorough.isChecked()

        def work():
            try:
                res = scan_video(*args, gpu=gpu, thorough=thorough, progress=lambda p: self.ui(self.scan_bar.set_value, p),
                                 on_kill=lambda n: self.ui(self.set_status, f"Suche läuft …  {n} "
                                                           f"{'Kill' if n == 1 else 'Kills'}", "busy"),
                                 cancel=self.cancel)
                self.ui(self.scan_done, *res)
            except Exception as e:
                self.ui(self.scan_failed, str(e) or repr(e))
        threading.Thread(target=work, daemon=True).start()

    def scan_reset(self):
        self.scanning = False
        self.scan_btn.setText("Kills suchen")
        self.scan_btn.icon_name = "crosshair"
        self.scan_btn.set_kind("accent")

    def scan_failed(self, msg):
        self.scan_reset()
        if not self.clips:
            self.rebuild_cards()
        self.set_status("Fehler: " + msg[:140], "bad")
        self.say("Suche fehlgeschlagen", RED)

    def scan_done(self, res, info):
        cancelled = self.cancel.is_set()
        self.scan_reset()
        self.scan_bar.set_value(1.0)
        self.clips = [Clip(a, b, k, bgr_to_pixmap(th) if th is not None else None) for a, b, k, th in res]
        self.sel = None
        self.rebuild_cards(animate=True)
        n = len(self.clips)
        word = "Kill" if n == 1 else "Kills"
        extra = f" · {info['notices']} Meldungen geprüft" if self.server["kind"] != "xhit" else ""
        if info.get("sekunden") is not None:
            extra += f" · {info['sekunden']:.0f} s"
        self.set_status(("Abgebrochen · " if cancelled else "Fertig · ") + f"{n} {word}{extra}", "ok" if n else "warn")
        self.say(f"{n} {word} gefunden" if n else "Keine Kills gefunden – Empfindlichkeit erhöhen?",
                 T.acc if n else AMBER)
        if n:
            self.select(0)

    # ------------------------------------------------------------ Clips
    def empty_state(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setAlignment(Qt.AlignCenter)
        v.setSpacing(6)
        ic = QLabel()
        ic.setPixmap(icon_pixmap("crosshair" if self.player else "film", T.icon, 40, 1.5))
        ic.setAlignment(Qt.AlignCenter)
        v.addSpacing(34)
        v.addWidget(ic)
        v.addSpacing(6)
        t = label("Noch keine Clips" if self.player else "Noch kein Video", 12, True)
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        h = label("Starte die Kill-Suche – gefundene Kills erscheinen hier." if self.player else
                  "Öffne eine Aufnahme, dann kannst du die Kills suchen.", 10, False, MUT, True)
        h.setAlignment(Qt.AlignCenter)
        v.addWidget(h)
        v.addSpacing(8)
        b = PopButton("Kills suchen" if self.player else "Video öffnen", "crosshair" if self.player else "folder",
                      "glass", 38)
        b.clicked.connect(self.start_scan if self.player else self.open_video)
        v.addWidget(b, 0, Qt.AlignHCenter)
        return w

    def rebuild_cards(self, animate=False):
        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(2)
        self.rows = []
        if not self.clips and self.scanning:
            for _ in range(3):
                v.addWidget(SkeletonRow())
        elif not self.clips:
            v.addWidget(self.empty_state())
        for i, c in enumerate(self.clips):
            row = ClipRow(i, c)
            row.selected = i == self.sel
            row.clicked.connect(lambda i=i: self.select(i))
            row.delete.connect(lambda i=i: self.delete(i))
            row.rename.connect(lambda i=i: self.open_rename_clip(i))
            if animate:                                   # Liste erscheint nacheinander (kurze Staffelung)
                row.appear = 0.0
                QTimer.singleShot(min(i, 8) * 45, lambda r=row: self._appear(r))
            v.addWidget(row)
            self.rows.append(row)
        v.addStretch(1)
        self.clip_scroll.setWidget(host)
        self.refresh_names()
        self.count_lbl.setText(str(len(self.clips)))
        total = sum(c.end - c.start for c in self.clips)
        self.total_lbl.setText(f"Gesamt {fmt_short(total)}" if self.clips else "")
        self.exp_all.setText(f"Alle exportieren ({len(self.clips)})" if self.clips else "Alle exportieren")
        self.timeline.update()

    @staticmethod
    def _appear(row):
        try:
            tween(row, "appear", 1.0, 240)
        except RuntimeError:                              # Zeile wurde inzwischen ersetzt
            pass

    def clip_changed(self, i):
        if 0 <= i < len(self.rows):
            self.rows[i].update()
        self.timeline.update()

    def select(self, i, seek=True):
        if not (0 <= i < len(self.clips)):
            return
        self.sel = i
        for j, r in enumerate(self.rows):
            r.selected = j == i
            r.update()
        c = self.clips[i]
        self.play_clip = c
        self.timeline.focus_on(c.start, c.end)
        if seek:
            if self.playing:
                self.toggle_play()
            self.seek(c.start)
        self.timeline.update()

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

    def make_thumb(self):
        arr = self.video.arr
        if arr is None:
            return None
        small = cv2.resize(arr, (256, int(256 * arr.shape[0] / arr.shape[1])), interpolation=cv2.INTER_AREA)
        h, w = small.shape[:2]
        return QPixmap.fromImage(QImage(small.data, w, h, 3 * w, QImage.Format_RGB888).copy())

    def create_clip(self, start, end, kill=None):
        c = Clip(max(0.0, start), min(self.duration, end), kill if kill is not None else start)
        c.thumb = self.make_thumb()
        self.clips.append(c)
        self.clips.sort(key=lambda x: x.start)
        idx = self.clips.index(c)
        self.mark_in = self.mark_out = None
        self.rebuild_cards()
        self.select(idx, seek=False)
        return c

    def add_clip(self):
        if not self.player:
            return
        self.create_clip(self.pos - self.s_pre.value(), self.pos + self.s_post.value(), self.pos)
        self.say("Clip hinzugefügt")

    def set_edge(self, which):
        """Start/Ende setzen: aendert den gewaehlten Clip, wenn die Position darin liegt - sonst entsteht ein neuer
        Clip aus Start- und Ende-Markierung."""
        if not self.player:
            return
        c = self.clips[self.sel] if self.sel is not None and 0 <= self.sel < len(self.clips) else None
        if c is not None and c.start - 1.0 <= self.pos <= c.end + 1.0:
            if which == 0:
                c.start = min(self.pos, c.end - 0.5)
            else:
                c.end = max(self.pos, c.start + 0.5)
            self.clip_changed(self.sel)
            self.refresh_names()
            return
        if which == 0:
            if self.mark_out is not None and self.pos < self.mark_out - 0.4:
                n = self.mark_out - self.pos
                self.create_clip(self.pos, self.mark_out)
                self.say(f"Clip erstellt ({n:.1f} s)")
            else:
                self.mark_in, self.mark_out = self.pos, None
                self.say(f"Start gesetzt bei {fmt_short(self.pos)} – jetzt „Ende hier“ drücken")
        else:
            if self.mark_in is not None and self.pos > self.mark_in + 0.4:
                n = self.pos - self.mark_in
                self.create_clip(self.mark_in, self.pos)
                self.say(f"Clip erstellt ({n:.1f} s)")
            else:
                self.mark_out, self.mark_in = self.pos, None
                self.say(f"Ende gesetzt bei {fmt_short(self.pos)} – jetzt „Start hier“ drücken")
        self.timeline.update()

    # ------------------------------------------------------------ Export-Ordner und Namen
    def export_dir(self):
        d = self.st.get("export_dir") or ""
        return d if d and os.path.isdir(d) else ""

    def make_dir_row(self):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(label("Export-Ordner", 10.5, True))
        pl = ElidedLabel("", 9, False, MUT)
        col.addWidget(pl)
        row.addLayout(col, 1)
        b = PopButton("Wählen", "folder", "glass", 34)
        b.clicked.connect(lambda: self.choose_export_dir())
        row.addWidget(b)
        self.dir_widgets.append((pl, b))
        self.refresh_dir_labels()
        return box

    def refresh_dir_labels(self):
        d = self.export_dir()
        for lbl, btn in self.dir_widgets:
            lbl.setText(d if d else "Noch nicht festgelegt – wird beim ersten Export gefragt")
            lbl.setToolTip(d)
            btn.setText("Ändern" if d else "Wählen")

    def choose_export_dir(self, then=None):
        start = self.export_dir() or self.st.get("outdir") or ""
        d = QFileDialog.getExistingDirectory(self, "Export-Ordner wählen", start)
        if not d:
            return
        self.st["export_dir"] = d
        self.st["outdir"] = d
        self.save_settings()
        self.refresh_names()
        self.refresh_library()
        self.say("Export-Ordner gespeichert")
        if then:
            then()

    def plan_names(self, clips):
        """Dateinamen fuer die Clips: eigener Name, sonst die naechste freie Nummer im Export-Ordner."""
        folder = self.export_dir()
        used, nums = set(), []
        if folder:
            try:
                for f in os.listdir(folder):
                    stem = os.path.splitext(f)[0]
                    used.add(stem.lower())
                    if stem.isdigit():
                        nums.append(int(stem))
            except OSError:
                pass
        nxt = (max(nums) + 1) if nums else 1
        out = []
        for c in clips:
            base = sanitize_name(c.name) if c.name else ""
            if base:
                n, k = base, 2
                while n.lower() in used:
                    n = f"{base} ({k})"
                    k += 1
            else:
                while str(nxt) in used:
                    nxt += 1
                n = str(nxt)
                nxt += 1
            used.add(n.lower())
            out.append(n)
        return out

    def refresh_names(self):
        if not hasattr(self, "rows"):
            return
        plan = self.plan_names(self.clips)
        for i, row in enumerate(self.rows):
            if i < len(self.clips):
                nm = self.clips[i].last_name or plan[i]
                row.title_text = f"Clip {nm}" if nm.isdigit() else nm
                row.update()
        self.refresh_dir_labels()

    def open_rename_clip(self, i):
        if not (0 <= i < len(self.clips)):
            return
        c = self.clips[i]
        plan = self.plan_names(self.clips)
        card = DialogCard("Clip benennen", "Der Name wird beim Export als Dateiname verwendet. "
                                           "Leer lassen für die automatische Nummer.")
        card.body.addWidget(label("Name", 10.5, True, MUT))
        le = QLineEdit(c.name or "")
        le.setPlaceholderText(plan[i])
        self.style_edit(le)
        card.body.addWidget(le)

        def save():
            c.name = sanitize_name(le.text()) or None
            c.last_name = None
            self.refresh_names()
            self.overlay.dismiss()
        le.returnPressed.connect(save)
        cancel = PopButton("Abbrechen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        sv = PopButton("Speichern", "check", "accent", 40)
        sv.clicked.connect(save)
        card.btns.addWidget(cancel)
        card.btns.addWidget(sv)
        self.overlay.present(card)
        QTimer.singleShot(120, le.setFocus)

    # ------------------------------------------------------------ Bibliothek (Export-Ordner)
    def build_library_page(self):
        main = Glass(radius=32, alpha=125)
        lay = QVBoxLayout(main)
        lay.setContentsMargins(30, 26, 22, 22)
        top = QHBoxLayout()
        hdr = QVBoxLayout()
        hdr.setSpacing(2)
        hdr.addWidget(label("Bibliothek", 24, True))
        self.lib_sub = ElidedLabel("", 10.5, False, MUT)
        hdr.addWidget(self.lib_sub)
        top.addLayout(hdr, 1)
        b1 = PopButton("Ordner wählen", "folder", "glass", 40)
        b1.clicked.connect(lambda: self.choose_export_dir())
        b2 = PopButton("Im Explorer öffnen", None, "glass", 40)
        b2.clicked.connect(lambda: self.export_dir() and open_path(self.export_dir()))
        b3 = PopButton("", "refresh", "glass", 40, icon_only=True)
        b3.setToolTip("Aktualisieren")
        b3.clicked.connect(self.refresh_library)
        for b in (b1, b2, b3):
            top.addWidget(b)
        lay.addLayout(top)
        lay.addSpacing(10)
        self.lib_scroll = QScrollArea()
        self.lib_scroll.setWidgetResizable(True)
        self.lib_scroll.setFrameShape(QScrollArea.NoFrame)
        self.lib_scroll.viewport().setAutoFillBackground(False)
        lay.addWidget(self.lib_scroll, 1)
        return main

    def refresh_library(self):
        self.lib_gen += 1
        gen = self.lib_gen
        d = self.export_dir()
        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 8, 0)
        v.setSpacing(2)
        self.lib_rows = {}
        if not d:
            self.lib_sub.setText("Noch kein Export-Ordner gewählt")
            e = QLabel()
            e.setPixmap(icon_pixmap("folder", T.icon, 40, 1.5))
            e.setAlignment(Qt.AlignCenter)
            v.addSpacing(60)
            v.addWidget(e)
            t = label("Hier erscheinen deine exportierten Clips", 12, True)
            t.setAlignment(Qt.AlignCenter)
            v.addWidget(t)
            h = label("Wähle einen Ordner. Dort werden alle Clips gespeichert, und du siehst sie hier.", 10, False,
                      MUT, True)
            h.setAlignment(Qt.AlignCenter)
            v.addWidget(h)
            v.addSpacing(8)
            b = PopButton("Ordner wählen", "folder", "accent", 42)
            b.clicked.connect(lambda: self.choose_export_dir())
            v.addWidget(b, 0, Qt.AlignHCenter)
            v.addStretch(1)
        else:
            files = list_videos(d)
            self.lib_sub.setText(f"{d}  ·  {len(files)} {'Clip' if len(files) == 1 else 'Clips'}")
            if not files:
                t = label("Noch keine Clips in diesem Ordner", 12, True)
                t.setAlignment(Qt.AlignCenter)
                v.addSpacing(70)
                v.addWidget(t)
                h = label("Exportiere Clips aus dem Editor – sie erscheinen dann hier.", 10, False, MUT, True)
                h.setAlignment(Qt.AlignCenter)
                v.addWidget(h)
            paths = []
            for f in files:
                p = os.path.join(d, f)
                row = LibRow(p)
                try:
                    row.detail = f"{os.path.splitext(f)[1][1:].upper()}  ·  {os.path.getsize(p) / 1e6:.0f} MB"
                except OSError:
                    pass
                row.rename.connect(lambda p=p: self.open_rename_file(p))
                row.open_file.connect(lambda p=p: open_path(p))
                v.addWidget(row)
                self.lib_rows[p] = row
                paths.append(p)
            v.addStretch(1)
            if paths:
                threading.Thread(target=self._load_lib_info, args=(paths, gen), daemon=True).start()
        self.lib_scroll.setWidget(host)

    def _load_lib_info(self, paths, gen):
        for p in paths:
            if gen != self.lib_gen:
                return
            try:
                key = (p, os.path.getmtime(p), os.path.getsize(p))
            except OSError:
                continue
            info = self.lib_cache.get(key)
            if info is None:
                thumb, dur = None, 0.0
                cap = cv2.VideoCapture(p)
                if cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
                    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                    dur = n / fps
                    cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * 0.15))
                    ok, fr = cap.read()
                    if ok:
                        h, w = fr.shape[:2]
                        thumb = cv2.resize(fr, (192, max(2, int(192 * h / w))), interpolation=cv2.INTER_AREA)
                cap.release()
                info = (thumb, dur, key[2])
                self.lib_cache[key] = info
            self.ui(self._apply_lib, gen, p, *info)

    def _apply_lib(self, gen, path, thumb, dur, size):
        row = self.lib_rows.get(path)
        if gen != self.lib_gen or row is None:
            return
        if thumb is not None:
            row.thumb = bgr_to_pixmap(thumb)
        ext = os.path.splitext(path)[1][1:].upper()
        row.detail = f"{ext}  ·  {fmt_short(dur)}  ·  {size / 1e6:.0f} MB"
        row.update()

    def open_rename_file(self, path):
        stem, ext = os.path.splitext(os.path.basename(path))
        card = DialogCard("Clip umbenennen", f"Dateiendung bleibt {ext}.")
        card.body.addWidget(label("Name", 10.5, True, MUT))
        le = QLineEdit(stem)
        self.style_edit(le)
        card.body.addWidget(le)

        def save():
            nm = sanitize_name(le.text())
            if not nm:
                card.err.setText("Bitte einen Namen eingeben.")
                return
            new = os.path.join(os.path.dirname(path), nm + ext)
            if new.lower() != path.lower() and os.path.exists(new):
                card.err.setText("Diesen Namen gibt es in dem Ordner schon.")
                return
            try:
                os.rename(path, new)
            except OSError as e:
                card.err.setText(f"Umbenennen nicht möglich: {e}")
                return
            self.overlay.dismiss()
            self.refresh_names()
            self.refresh_library()
            self.say("Umbenannt")
        le.returnPressed.connect(save)
        cancel = PopButton("Abbrechen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        sv = PopButton("Speichern", "check", "accent", 40)
        sv.clicked.connect(save)
        card.btns.addWidget(cancel)
        card.btns.addWidget(sv)
        self.overlay.present(card)
        QTimer.singleShot(120, le.setFocus)
        QTimer.singleShot(150, le.selectAll)

    # ------------------------------------------------------------ Export
    def export(self, only_selected):
        if not self.clips:
            self.say("Keine Clips zum Exportieren", RED)
            return
        if not self.ffmpeg:
            self.say("FFmpeg nicht gefunden", RED)
            return
        if only_selected:
            if self.sel is None:
                self.say("Wähle zuerst einen Clip aus", RED)
                return
            idx = [self.sel]
        else:
            idx = list(range(len(self.clips)))
        if not self.export_dir():
            self.choose_export_dir(then=lambda: self.export(only_selected))
            return
        out = self.export_dir()
        mode = MODES[self.seg_mode.current()][0]
        ext = os.path.splitext(self.path)[1] or ".mp4"
        sel = [self.clips[i] for i in idx]
        names = self.plan_names(sel)
        jobs = [(c.start, c.end, os.path.join(out, n + ext), c, n) for c, n in zip(sel, names)]
        self.exp_bar.set_value(0, False)
        self.exp_all.setEnabled(False)

        def work():
            try:
                for n, (a, b, dst, c, nm) in enumerate(jobs, 1):
                    self.ui(self.set_status, f"Export {n}/{len(jobs)} …", "busy")
                    export_clip(self.ffmpeg, self.path, a, b, dst, mode)
                    self.ui(self.exp_bar.set_value, n / len(jobs))
                self.ui(self.export_done, out, [(c, nm) for _, _, _, c, nm in jobs])
            except Exception as e:
                self.ui(self.export_failed, str(e))
        threading.Thread(target=work, daemon=True).start()

    def export_failed(self, msg):
        self.exp_all.setEnabled(True)
        self.set_status("Export fehlgeschlagen", "bad")
        self.say("Export-Fehler: " + msg[:90], RED)

    def export_done(self, out, items):
        self.exp_all.setEnabled(True)
        n = len(items)
        for c, nm in items:
            c.last_name = nm
        self.set_status(f"{n} Clips exportiert", "ok")
        self.say(f"{n} {'Clip' if n == 1 else 'Clips'} exportiert")
        self.refresh_names()
        self.refresh_library()
        if self.tg_open.isChecked():
            open_path(out)

    # ------------------------------------------------------------ Tasten
    def keyPressEvent(self, e):
        if self.overlay.isVisible():
            if e.key() == Qt.Key_Escape and not self.login_open:
                self.overlay.dismiss()
            return
        if self.stack.cur != 1:
            return super().keyPressEvent(e)
        k = e.key()
        shift = bool(e.modifiers() & Qt.ShiftModifier)
        if k == Qt.Key_Space:
            self.toggle_play()
        elif k == Qt.Key_Left:
            self.seek(self.pos - (1 / self.fps if shift else 1))
        elif k == Qt.Key_Right:
            self.seek(self.pos + (1 / self.fps if shift else 1))
        elif k == Qt.Key_Up:
            self.step_clip(-1)
        elif k == Qt.Key_Down:
            self.step_clip(1)
        elif k == Qt.Key_I:
            self.set_edge(0)
        elif k == Qt.Key_O:
            self.set_edge(1)
        elif k == Qt.Key_Delete:
            self.delete()
        else:
            super().keyPressEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setFont(app_font(10))
    try:
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(resource("assets", "icon.ico")))
    except Exception:
        pass
    w = Window()
    for a in sys.argv[1:]:
        m = re.match(r"killapp:(?://)?invite/([A-Za-z0-9]+)", a)
        if m:
            w.pending_invite = m.group(1)
    if sys.platform.startswith("win"):
        w.showMaximized()
    else:
        w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

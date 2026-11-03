"""ACE - schneidet automatisch Kill-Clips aus GTA/FiveM-Aufnahmen (helle Glas-Oberflaeche, Qt)."""
import json
import math
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
from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, Qt, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QPolygonF)
from PySide6.QtWidgets import (QApplication, QFrame, QColorDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
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
                Timeline, Toast, Toggle, app_font, display_font, load_fonts, paint_glass, paint_icon, tween)

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


def hero_font(size):
    """Grosse Ueberschrift / grosse Zahl (Sora)."""
    return display_font(size, QFont.DemiBold)


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
        return QRectF(self.M + self.W - 48, self.M + 18, 32, 32)

    def _del_rect(self):
        return QRectF(self.M + self.W - 86, self.M + 18, 32, 32)

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 160)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 180)
        tween(self, "ehov", 0.0, 120)
        tween(self, "dhov", 0.0, 120)

    def mouseMoveEvent(self, e):
        self.mx, self.my = e.position().x(), e.position().y()
        self.update()
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
        """Karte wie im Vorbild: Profilbild, graue Zeile, fetter Name, gruener Status, kleine Pillen."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.M, self.M, self.W, self.H)
        hv = max(0.0, min(1.0, self.pop))
        c = r.center()
        p.translate(c)
        p.scale(1 + 0.012 * self.pop, 1 + 0.012 * self.pop)
        p.translate(-c)
        rad = 22
        paint_glass(p, self, r.adjusted(0.5, 0.5, -0.5, -0.5), rad, 140 + 50 * hv, border=not self.add,
                    sheen=(getattr(self, "mx", c.x()), getattr(self, "my", c.y()), hv))
        if self.add:
            pen = QPen(T.glass_edge(0.9 + 0.5 * hv), 1.2)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        if self.add:
            d = 44
            cr = QRectF(c.x() - d / 2, c.y() - 34, d, d)
            p.setPen(QPen(T.line(1.6), 1))
            p.setBrush(T.card_fill(170))
            p.drawEllipse(cr)
            paint_icon(p, "plus", cr.adjusted(13, 13, -13, -13), ink(220), 1.8)
            p.setPen(ink(235))
            p.setFont(app_font(10.5, True))
            req = getattr(self, "request", False)
            p.drawText(QRectF(r.left(), c.y() + 16, r.width(), 24), Qt.AlignCenter,
                       "Server anfragen" if req else "Neuer Server")
            p.setPen(ink(130))
            p.setFont(app_font(9))
            p.drawText(QRectF(r.left(), c.y() + 38, r.width(), 20), Qt.AlignCenter,
                       "Dein Server fehlt? Frag ihn beim Admin an" if req else "Eigene Kill-Erkennung anlegen")
            return
        paint_avatar(p, QRectF(r.left() + 18, r.top() + 18, 42, 42), self.name, 21, 16, self.sid)
        tx = r.left() + 72
        avail = r.width() - 72 - (96 if not self.locked else 18)
        p.setPen(ink(130))
        p.setFont(app_font(8.5))
        p.drawText(QRectF(tx, r.top() + 19, avail, 16), Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(self.sub, Qt.ElideRight, int(avail)))
        p.setPen(ink(240))
        p.setFont(display_font(12.5))
        p.drawText(QRectF(tx, r.top() + 36, avail, 24), Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(self.name, Qt.ElideRight, int(avail)))
        if not self.locked:
            for rr, icon, h, col in ((self._del_rect(), "trash", self.dhov, T.err), (self._edit_rect(), "edit", self.ehov, T.icon)):
                p.setPen(QPen(T.line(1.2 + h), 1))
                p.setBrush(T.card_fill(120 + 60 * h))
                p.drawEllipse(rr)
                paint_icon(p, icon, rr.adjusted(9, 9, -9, -9), col if h > 0.4 else ink(170), 1.5)
        if self.tag:
            p.setPen(T.okc)
            p.setFont(app_font(9))
            p.drawText(QRectF(r.left() + 18, r.top() + 74, r.width() - 36, 18), Qt.AlignVCenter, self.tag)
        # Pillen unten
        p.setFont(app_font(9))
        fm = QFontMetrics(p.font())
        sel_txt = "Auswählen"
        w = fm.horizontalAdvance(sel_txt) + 46
        pr = QRectF(r.left() + 18, r.bottom() - 46, w, 30)
        pf, pfg = T.pill()
        p.setPen(Qt.NoPen)
        p.setBrush(with_alpha(pf, 255 * hv) if hv > 0.02 else Qt.NoBrush)
        if hv <= 0.02:
            p.setPen(QPen(T.line(1.5), 1))
        p.drawRoundedRect(pr, 15, 15)
        fgc = pfg if hv > 0.5 else ink(215)
        p.setPen(fgc)
        p.drawText(QRectF(pr.left() + 14, pr.top(), w - 30, pr.height()), Qt.AlignVCenter, sel_txt)
        ax, ay = pr.right() - 18, pr.center().y()
        p.setPen(QPen(fgc, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(QPointF(ax - 4, ay + 4), QPointF(ax + 4, ay - 4))
        p.drawPolyline(QPolygonF([QPointF(ax - 2, ay - 4), QPointF(ax + 4, ay - 4), QPointF(ax + 4, ay + 2)]))


class NavItem(QWidget):
    """Reiter oben: nur Text, der aktive Reiter liegt auf einer dezenten dunklen Pille."""
    clicked = Signal()

    def __init__(self, icon, text, parent=None):
        super().__init__(parent)
        self.icon, self.text = icon, text
        self.sel = 0.0
        self.pop = 0.0
        self.enabled_nav = True
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(QFontMetrics(app_font(9.5, False)).horizontalAdvance(text) + 38)

    def set_selected(self, on):
        tween(self, "sel", 1.0 if on else 0.0, 220)

    def set_nav_enabled(self, on):
        self.enabled_nav = on
        self.update()

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 140)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 140)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setOpacity(1.0 if self.enabled_nav else 0.35)
        r = QRectF(self.rect()).adjusted(1, 3, -1, -3)
        a = max(0.0, min(1.0, self.sel))
        base = ink(int(150 + 60 * self.pop))
        hi = T.pill()[1]
        p.setPen(QColor(int(base.red() + (hi.red() - base.red()) * a), int(base.green() + (hi.green() - base.green()) * a),
                        int(base.blue() + (hi.blue() - base.blue()) * a), int(base.alpha() + (255 - base.alpha()) * a)))
        p.setFont(app_font(9.5, a > 0.5))
        p.drawText(r, Qt.AlignCenter, self.text)


class NavBar(Glass):
    """Glas-Leiste mit den Reitern; die helle Markierung gleitet weich zum gewaehlten Reiter."""

    def __init__(self, parent=None):
        super().__init__(parent, radius=40, alpha=120)
        self.items = []
        self.cur = 0
        self.ix, self.iw = 0.0, 0.0
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(5, 0, 5, 0)
        self.lay.setSpacing(0)
        self.setFixedHeight(48)

    def add(self, item):
        self.items.append(item)
        self.lay.addWidget(item)

    def select(self, i, animate=True):
        self.cur = i
        g = self.items[i].geometry()
        if animate and self.iw > 0 and g.width() > 0:
            tween(self, "ix", g.x(), 320)
            tween(self, "iw", g.width(), 320)
        else:
            self.ix, self.iw = float(g.x()), float(g.width())
            self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, lambda: self.select(self.cur, False))

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.iw <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.ix + 1, 6, self.iw - 2, self.height() - 12)
        pf, _ = T.pill()
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, pf.lighter(118))
        g.setColorAt(1, pf.darker(104))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        hl = QLinearGradient(r.left(), 0, r.right(), 0)
        hl.setColorAt(0, QColor(255, 255, 255, 0))
        hl.setColorAt(0.5, QColor(255, 255, 255, 160))
        hl.setColorAt(1, QColor(255, 255, 255, 0))
        p.setPen(QPen(hl, 1))
        p.drawLine(QPointF(r.left() + 12, r.top() + 1), QPointF(r.right() - 12, r.top() + 1))


class Dock(QWidget):
    """Navigation unten in der Mitte wie ein Apple-Dock: Symbole vergroessern sich weich zur Maus hin,
    beim Darueberfahren erscheint der Name darueber, der aktive Bereich hat einen Punkt darunter."""
    BASE, MAG, DIST, PANEL, HEAD = 40.0, 64.0, 150.0, 60, 58

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.items = items                    # Liste von dicts: icon, label, fn, sep(optional)
        self.mx = None
        self.active = 0
        self.disabled = set()
        self.dots = set()
        self.w = [self.BASE] * len(items)
        self._exp = False
        self.lab = [0.0] * len(items)
        self.hk = 0.0
        self.hover_i = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.t = QTimer(self)
        self.t.setInterval(14)
        self.t.timeout.connect(self._step)
        self.t.start()

    # ---- Geometrie
    def _target(self, i, cx):
        if self.mx is None:
            return self.BASE
        d = abs(self.mx - cx)
        if d >= self.DIST:
            return self.BASE
        k = 1 - d / self.DIST
        return self.BASE + (self.MAG - self.BASE) * (0.5 - 0.5 * math.cos(math.pi * k))

    def _layout(self):
        xs, x = [], 16.0
        for i, it in enumerate(self.items):
            if it.get("sep"):
                x += 10
            xs.append(x)
            x += self.w[i] + 14
        return xs, x - 14 + 16

    def total_width(self):
        return int(16 + sum(self.BASE + 14 for _ in self.items) - 14 + 16 + 10 * sum(1 for it in self.items if it.get("sep")))

    def _step(self):
        xs, tw = self._layout()
        moved = False
        for i in range(len(self.items)):
            cx = xs[i] + self.w[i] / 2 + (self.width() - tw) / 2
            tgt = self._target(i, cx)
            nw = self.w[i] + (tgt - self.w[i]) * 0.22            # weiche Feder
            if abs(nw - self.w[i]) > 0.05:
                moved = True
            self.w[i] = nw
            lt = 1.0 if i == self.hover_i else 0.0
            nl = self.lab[i] + (lt - self.lab[i]) * 0.25
            if abs(nl - self.lab[i]) > 0.003:
                moved = True
            self.lab[i] = nl
        ht = 1.0 if self.mx is not None else 0.0
        nh = self.hk + (ht - self.hk) * 0.25
        if abs(nh - self.hk) > 0.003:
            moved = True
        self.hk = nh
        if moved:
            self.update()

    def _hit(self, x, y):
        xs, tw = self._layout()
        off = (self.width() - tw) / 2
        for i in range(len(self.items)):
            r = QRectF(off + xs[i], 10, self.w[i], self.w[i])
            if r.adjusted(-6, -6, 6, 6).contains(QPointF(x, y)):
                return i
        return -1

    hovered = Signal(int)

    def enterEvent(self, e):
        self.expand(True)

    def mouseMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        i = self._hit(x, y)
        if y <= self.PANEL and not self._exp:     # zurueck aufs Dock: wieder Platz fuer Groesse und Namen schaffen
            self.expand(True)
        if y > self.PANEL + 6 and i < 0:          # unter dem Dock: wie Verlassen (Inhalt darunter bleibt klickbar)
            self.leaveEvent(None)
            return
        self.mx = x
        if i != self.hover_i:
            self.hover_i = i
            self.hovered.emit(i)

    def leaveEvent(self, e):
        self.mx = None
        if self.hover_i != -1:
            self.hover_i = -1
            self.hovered.emit(-1)
        self.expand(False)

    def expand(self, on):
        self._exp = on
        if on:
            self.on_geom(True)
        else:
            QTimer.singleShot(260, lambda: (not self._exp) and self.on_geom(False))

    def on_geom(self, big):
        pass

    def mousePressEvent(self, e):
        i = self._hit(e.position().x(), e.position().y())
        if i >= 0 and i not in self.disabled:
            self.items[i]["fn"]()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        xs, tw = self._layout()
        off = (self.width() - tw) / 2
        panel = QRectF(off, 0.5, tw, self.PANEL)
        p.setPen(QPen(T.glass_edge(1.0), 1))
        p.setBrush(T.card_fill(200))
        p.drawRoundedRect(panel, 18, 18)
        for i, it in enumerate(self.items):
            if it.get("sep"):
                p.setPen(QPen(T.glass_edge(1.0), 1))
                sx = off + xs[i] - 12
                p.drawLine(QPointF(sx, 14), QPointF(sx, self.PANEL - 14))
            w = self.w[i]
            r = QRectF(off + xs[i], 10, w, w)
            p.setOpacity(0.35 if i in self.disabled else 1.0)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(38, 38, 38) if T.dark else QColor(229, 231, 235))
            p.drawEllipse(r)
            s = w / 2
            col = QColor(212, 212, 212) if T.dark else QColor(82, 82, 82)
            if i == self.active:
                col = QColor(255, 255, 255) if T.dark else QColor(17, 17, 17)
            paint_icon(p, it["icon"], QRectF(r.center().x() - s / 2, r.center().y() - s / 2, s, s), col,
                       max(1.4, w / 26))
            if i in self.dots:
                p.setBrush(RED)
                p.drawEllipse(QPointF(r.right() - w * 0.16, r.top() + w * 0.16), 4, 4)
            p.setOpacity(1.0)
            if i == self.active:
                p.setBrush(ink(220))
                p.drawEllipse(QPointF(r.center().x(), r.bottom() + 5), 2, 2)
            a = self.lab[i]
            if a > 0.02 and not it.get("nolabel"):
                p.setFont(app_font(8.5))
                fm = QFontMetrics(p.font())
                lw = fm.horizontalAdvance(it["label"]) + 16
                lr = QRectF(r.center().x() - lw / 2, r.bottom() + 6 + 8 * a, lw, 22)
                p.setOpacity(a)
                p.setPen(QPen(QColor(23, 23, 23) if T.dark else QColor(229, 231, 235), 1))
                p.setBrush(QColor(38, 38, 38) if T.dark else QColor(243, 244, 246))
                p.drawRoundedRect(lr, 6, 6)
                p.setPen(QColor(255, 255, 255) if T.dark else QColor(64, 64, 64))
                p.drawText(lr, Qt.AlignCenter, it["label"])
                p.setOpacity(1.0)


class ThemePopover(QWidget):
    """Kleines Fenster unter dem Dock-Symbol: Hell oder Dunkel waehlen (mit dem Origin-Effekt der Knoepfe)."""

    def __init__(self, parent, on_pick):
        super().__init__(parent)
        self.on_pick = on_pick
        self.k = 0.0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self.b_light = PopButton("Hell", "sun", "glass", 36)
        self.b_dark = PopButton("Dunkel", "moon", "glass", 36)
        self.b_light.clicked.connect(lambda: self.on_pick(False))
        self.b_dark.clicked.connect(lambda: self.on_pick(True))
        lay.addWidget(self.b_light)
        lay.addWidget(self.b_dark)
        self.setFixedSize(236, 64)
        self.watch = QTimer(self)
        self.watch.setInterval(120)
        self.watch.timeout.connect(self._check)

    def sync(self):
        self.b_dark.set_kind("accent" if T.dark else "glass")
        self.b_light.set_kind("glass" if T.dark else "accent")

    def open_at(self, cx, y):
        self.sync()
        self.move(cx - self.width() // 2, y)
        if not self.isVisible():
            self.k = 0.0
            self.show()
            tween(self, "k", 1.0, 220)
        self.raise_()
        self.watch.start()

    def _check(self):
        from PySide6.QtGui import QCursor
        gp = QCursor.pos()
        par = self.parent()
        dock = getattr(par.window(), "dock", None)
        inside = self.rect().adjusted(-10, -24, 10, 10).contains(self.mapFromGlobal(gp))
        on_item = dock is not None and dock.hover_i == len(dock.items) - 1
        if not inside and not on_item:
            self.watch.stop()
            self.hide()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(max(0.0, min(1.0, self.k)))
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(T.glass_edge(1.0), 1))
        p.setBrush(T.card_fill(230))
        p.drawRoundedRect(r, 16, 16)


class ServerMenuRow(QWidget):
    clicked = Signal()

    def __init__(self, srv, active, parent=None):
        super().__init__(parent)
        self.s, self.active = srv, active
        self.hov = 0.0
        self.appear = 0.0
        self.setFixedHeight(56)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, e):
        tween(self, "hov", 1.0, 120)

    def leaveEvent(self, e):
        tween(self, "hov", 0.0, 160)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setOpacity(max(0.0, min(1.0, self.appear)))
        p.translate(0, (1 - self.appear) * 6)
        r = QRectF(self.rect()).adjusted(4, 2, -4, -2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, int(16 * self.hov)) if T.dark else QColor(0, 0, 0, int(10 * self.hov)))
        p.drawRoundedRect(r, 12, 12)
        paint_avatar(p, QRectF(r.left() + 10, r.center().y() - 16, 32, 32), self.s["name"], 16, 12, self.s["id"])
        p.setPen(ink(240))
        p.setFont(app_font(10, True))
        p.drawText(QRectF(r.left() + 52, r.top() + 8, r.width() - 90, 20), Qt.AlignVCenter, self.s["name"])
        p.setPen(ink(130))
        p.setFont(app_font(8.5))
        p.drawText(QRectF(r.left() + 52, r.top() + 27, r.width() - 90, 16), Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(KIND_TEXT.get(self.s["kind"], ""), Qt.ElideRight, int(r.width() - 92)))
        if self.active:
            paint_icon(p, "check", QRectF(r.right() - 30, r.center().y() - 9, 18, 18), T.okc, 2.0)


class ServerMenu(QWidget):
    """Klappt unter dem Server oben rechts auf: alle Server zum direkten Wechseln (mit weichem Einblenden)."""

    def __init__(self, parent, on_pick, on_all):
        super().__init__(parent)
        self.on_pick, self.on_all = on_pick, on_all
        self.k = 0.0
        self.panel = QWidget(self)
        self.panel.paintEvent = self._paint_panel
        self.v = QVBoxLayout(self.panel)
        self.v.setContentsMargins(8, 10, 8, 8)
        self.v.setSpacing(0)
        self.hide()

    def mousePressEvent(self, e):            # Klick daneben schliesst
        self.close_menu()

    def _paint_panel(self, e):
        p = QPainter(self.panel)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.panel.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(T.glass_edge(1.0), 1))
        p.setBrush(T.card_fill(240))
        p.drawRoundedRect(r, 16, 16)

    def open_menu(self, servers, active_id, anchor):
        while self.v.count():
            it = self.v.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        cap = label("Server wechseln", 8.5, True, MUT)
        cap.setContentsMargins(12, 2, 0, 6)
        self.v.addWidget(cap)
        rows = []
        for s in servers:
            row = ServerMenuRow(s, s["id"] == active_id)
            row.clicked.connect(lambda s=s: (self.close_menu(), self.on_pick(s)))
            self.v.addWidget(row)
            rows.append(row)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {T.glass_edge(1.0).name()}; margin: 0 10px;")
        self.v.addSpacing(6)
        self.v.addWidget(line)
        self.v.addSpacing(6)
        allb = PopButton("Alle Server ansehen", "server", "glass", 36)
        allb.clicked.connect(lambda: (self.close_menu(), self.on_all()))
        self.v.addWidget(allb)
        self.setGeometry(self.parent().rect())
        w = 300
        self.panel.setFixedWidth(w)
        self.panel.adjustSize()
        self.ax, self.ay = anchor.x() + anchor.width() - w, anchor.y() + anchor.height() + 6
        self.panel.move(self.ax, self.ay)
        self.show()
        self.raise_()
        self.k = 0.0
        tween(self, "k", 1.0, 260)
        for i, r in enumerate(rows):
            QTimer.singleShot(30 + i * 40, lambda r=r: tween(r, "appear", 1.0, 220))

    def __setattr__(self, n, v):
        super().__setattr__(n, v)
        if n == "k" and hasattr(self, "panel"):
            self.panel.move(self.ax if hasattr(self, "ax") else 0, int(getattr(self, "ay", 0) - 8 * (1 - v)))
            eff = self.panel.graphicsEffect()
            if eff is None:
                from PySide6.QtWidgets import QGraphicsOpacityEffect
                eff = QGraphicsOpacityEffect(self.panel)
                self.panel.setGraphicsEffect(eff)
            eff.setOpacity(max(0.0, min(1.0, v)))
            if v >= 0.999:
                self.panel.setGraphicsEffect(None)

    def close_menu(self):
        tween(self, "k", 0.0, 140)
        QTimer.singleShot(150, self.hide)


class _NavProxy:
    """Damit der alte Code (self.nav[i].set_selected / set_nav_enabled) weiter funktioniert."""

    def __init__(self, dock, i):
        self.dock, self.i = dock, i

    def set_selected(self, on):
        if on:
            self.dock.active = self.i
            self.dock.update()

    def set_nav_enabled(self, on):
        (self.dock.disabled.discard if on else self.dock.disabled.add)(self.i)
        self.dock.update()


class UnsavedBar(QWidget):
    """Hinweis-Leiste: erscheint mit Feder-Effekt, sobald es Clips gibt, die noch nicht exportiert sind."""

    def __init__(self, parent, on_export):
        super().__init__(parent)
        self.k = 0.0
        self.shake = 0.0
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(18, 0, 8, 0)
        self.lay.setSpacing(8)
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("background:#f5a524;border-radius:4px;")
        self.lay.addWidget(dot)
        self.lbl = label("", 9.5, True)
        self.lay.addWidget(self.lbl, 1)
        self.later = PopButton("Später", None, "glass", 32)
        self.n = 0
        self.later.clicked.connect(lambda: (setattr(self, "dismissed_for", self.n), self.set_visible(False)))
        self.lay.addWidget(self.later)
        self.go = PopButton("Exportieren", "upload", "accent", 32)
        self.go.clicked.connect(on_export)
        self.lay.addWidget(self.go)
        self.setFixedHeight(52)
        self.hide()
        self.dismissed_for = None

    def set_count(self, n):
        if n <= 0:
            self.dismissed_for = None
            self.set_visible(False)
            return
        self.n = n
        self.lbl.setText(f"{n} {'Clip' if n == 1 else 'Clips'} noch nicht exportiert")
        if self.dismissed_for != n:
            self.set_visible(True)

    def __setattr__(self, n, v):
        super().__setattr__(n, v)
        if n == "k":
            self._place()

    def set_visible(self, on):
        if on:
            self.show()
            self.raise_()
        tween(self, "k", 1.0 if on else 0.0, 420 if on else 220,
              QEasingCurve(QEasingCurve.OutBack) if on else None)
        if not on:
            QTimer.singleShot(240, lambda: self.k < 0.05 and self.hide())

    def nudge(self):
        """Kurzes Wackeln, wenn man trotz ungespeicherter Clips etwas verlassen will."""
        self.set_visible(True)
        self.shake = 0.0
        a = QVariantAnimation(self)
        a.setDuration(420)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.valueChanged.connect(lambda v: (setattr(self, "shake", math.sin(float(v) * math.pi * 6) * (1 - float(v)) * 8),
                                           self._place()))
        a.start()
        self._shake_anim = a

    def _place(self):
        par = self.parent()
        if hasattr(par, "place"):
            par.place()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(max(0.0, min(1.0, self.k)))
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(T.glass_edge(1.0), 1))
        p.setBrush(T.card_fill(220))
        p.drawRoundedRect(r, 16, 16)


def serif_font(size=15.0, weight=QFont.DemiBold):
    """Serifenschrift fuer die Bento-Titel (wie im Vorbild)."""
    f = QFont()
    f.setFamilies(["Montserrat", "Segoe UI", "Arial"])
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


class BentoVisual(QWidget):
    """Bild-Motiv oben in einer Bento-Karte (gezeichnet, teils leicht animiert)."""

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.ph = 0.0
        self.setMinimumHeight(64 if kind in ("pills", "load", "folders", "keys", "users") else 96)
        if kind in ("rings", "load"):
            self.t = QTimer(self)
            self.t.setInterval(33)
            self.t.timeout.connect(self._tick)
            self.t.start()

    def _tick(self):
        if not self.isVisible():
            return
        self.ph = (self.ph + 0.012) % 1.0
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        W, H = self.width(), self.height()
        c = QPointF(W / 2, H / 2)
        soft = ink(70)
        mid = ink(120)
        strong = ink(235)
        k = self.kind
        if k == "aa":
            p.setPen(strong)
            p.setFont(serif_font(min(64, H * 0.55), QFont.DemiBold))
            p.drawText(QRectF(0, 0, W, H), Qt.AlignCenter, "Aa")
        elif k == "pills":
            p.setPen(Qt.NoPen)
            p.setBrush(ink(55))
            y = c.y() - 18
            p.drawRoundedRect(QRectF(c.x() - 52, y, 50, 14), 7, 7)
            p.drawRoundedRect(QRectF(c.x() + 4, y, 50, 14), 7, 7)
            p.drawRoundedRect(QRectF(c.x() - 52, y + 22, 50, 14), 7, 7)
        elif k == "load":
            p.setPen(mid)
            p.setFont(app_font(9))
            p.drawText(QRectF(0, c.y() - 26, W, 18), Qt.AlignCenter, "Suchzeit")
            bw = min(160, W * 0.5)
            t = 0.5 - 0.5 * math.cos(self.ph * 2 * math.pi)
            p.setPen(Qt.NoPen)
            p.setBrush(ink(40))
            p.drawRoundedRect(QRectF(c.x() - bw / 2, c.y(), bw, 4), 2, 2)
            p.setBrush(strong)
            p.drawRoundedRect(QRectF(c.x() - bw / 2, c.y(), bw * (0.25 + 0.75 * t), 4), 2, 2)
        elif k == "rings":
            for i in range(3):
                ph = (self.ph + i / 3) % 1.0
                rr = 34 + ph * 52
                p.setPen(QPen(ink(int(110 * (1 - ph))), 1.4))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(c, rr, rr)
            p.setPen(QPen(ink(60), 1))
            p.setBrush(ink(22))
            p.drawEllipse(c, 30, 30)
            paint_icon(p, "refresh", QRectF(c.x() - 15, c.y() - 15, 30, 30), strong, 2.0)
        elif k == "folders":
            for i, a in enumerate((255, 255, 90)):
                r = QRectF(c.x() - 66 + i * 46, c.y() - 20, 40, 40)
                p.setPen(Qt.NoPen)
                p.setBrush(ink(int(70 * a / 255)))
                p.drawRoundedRect(r, 9, 9)
                paint_icon(p, "folder", r.adjusted(11, 11, -11, -11), ink(int(230 * a / 255)), 1.6)
        elif k == "keys":
            p.setFont(app_font(10, True))
            for i, t in enumerate(("I", "O", "F")):
                r = QRectF(c.x() - 68 + i * 46, c.y() - 20, 40, 40)
                p.setPen(QPen(ink(120 if i < 2 else 60), 1.4))
                p.setBrush(ink(20))
                p.drawRoundedRect(r, 9, 9)
                p.setPen(strong if i < 2 else mid)
                p.drawText(r, Qt.AlignCenter, t)
        elif k == "users":
            for i in range(3):
                r = QRectF(c.x() - 50 + i * 30, c.y() - 20, 40, 40)
                p.setPen(QPen(T.card_fill(255), 2))
                p.setBrush(ink(60 + 40 * i))
                p.drawEllipse(r)
                paint_icon(p, "user", r.adjusted(10, 10, -10, -10), QColor(17, 17, 17) if T.dark else QColor(255, 255, 255), 1.6)


class BentoCard(Glass):
    """Karte im Bento-Raster: Motiv oben, darunter Symbol + Serifen-Titel, graue Beschreibung, Bedienelemente."""

    def __init__(self, visual, icon, title, desc, parent=None):
        super().__init__(parent, radius=22, alpha=150)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(22, 14, 22, 18)
        self.lay.setSpacing(6)
        if visual:
            self.vis = BentoVisual(visual)
            self.lay.addWidget(self.vis, 1)
        tr = QHBoxLayout()
        tr.setSpacing(8)
        if icon:
            ic = QLabel()
            ic.setPixmap(icon_pixmap(icon, T.text, 16, 1.6))
            tr.addWidget(ic)
        t = QLabel(title)
        t.setFont(serif_font(15.5 if visual else 19))
        if not visual:
            self.lay.addSpacing(6)
        tr.addWidget(t)
        tr.addStretch(1)
        self.head = tr
        self.lay.addLayout(tr)
        if desc:
            d = label(desc, 9, color=MUT, wrap=True)
            self.lay.addWidget(d)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.lay.addSpacing(4)
        self.lay.addLayout(self.body)


class ThemePreview(QWidget):
    """Kleine Vorschau der App im aktuellen Farbthema (aendert sich sofort mit)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        base = QColor(0, 0, 0) if T.dark else QColor(255, 255, 255)
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, base)
        g.setColorAt(0.5, base)
        g.setColorAt(1, T.glow())
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        p.setClipPath(path)
        p.fillRect(r, g)
        p.setClipping(False)
        p.setPen(QPen(T.glass_edge(1.0), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 14, 14)
        W, H = r.width(), r.height()
        # Dock oben
        dk = QRectF(r.center().x() - 54, r.top() + 10, 108, 20)
        p.setBrush(T.card_fill(200))
        p.drawRoundedRect(dk, 8, 8)
        p.setPen(Qt.NoPen)
        for i in range(5):
            p.setBrush(ink(60 if i else 200))
            p.drawEllipse(QPointF(dk.left() + 14 + i * 20, dk.center().y()), 5, 5)
        # Karten
        cw = (W - 50) / 3
        for i in range(3):
            c = QRectF(r.left() + 14 + i * (cw + 11), r.top() + 42, cw, H - 56)
            p.setPen(QPen(T.glass_edge(1.0), 1))
            p.setBrush(T.glass_veil(0.8))
            p.drawRoundedRect(c, 9, 9)
            p.setPen(Qt.NoPen)
            p.setBrush(ink(70))
            p.drawRoundedRect(QRectF(c.left() + 10, c.top() + 12, c.width() * 0.55, 6), 3, 3)
            p.setBrush(ink(35))
            p.drawRoundedRect(QRectF(c.left() + 10, c.top() + 24, c.width() * 0.75, 5), 2.5, 2.5)
            if i == 1:
                pf, _ = T.pill()
                p.setBrush(pf)
                p.drawRoundedRect(QRectF(c.left() + 10, c.bottom() - 26, c.width() - 20, 16), 6, 6)
            if i == 2:
                p.setBrush(T.pill()[0])
                tg = QRectF(c.right() - 36, c.bottom() - 24, 26, 14)
                p.drawRoundedRect(tg, 7, 7)
                p.setBrush(QColor(255, 255, 255))
                p.drawEllipse(QPointF(tg.right() - 7, tg.center().y()), 5, 5)


class ThemeDots(QWidget):
    """Farbthemen als runde Punkte mit Namen darunter (aktuelles Thema mit Ring)."""
    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.names = list(PRESETS)
        self.hov = -1
        self.setFixedHeight(66)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def _r(self, i):
        n = len(self.names)
        step = (self.width() - 8) / n
        d = min(38.0, step - 10)
        return QRectF(4 + i * step + (step - d) / 2, 6, d, d)

    def mouseMoveEvent(self, e):
        h = -1
        for i in range(len(self.names)):
            if self._r(i).adjusted(-4, -4, 4, 4).contains(e.position()):
                h = i
        if h != self.hov:
            self.hov = h
            self.update()

    def leaveEvent(self, e):
        self.hov = -1
        self.update()

    def mousePressEvent(self, e):
        for i, n in enumerate(self.names):
            if self._r(i).adjusted(-4, -4, 4, 4).contains(e.position()):
                self.picked.emit(n)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        st = getattr(self.window(), "st", {})
        cur = next((n for n, pr in PRESETS.items() if pr["acc"].lower() == str(st.get("acc", "")).lower()), None)
        for i, n in enumerate(self.names):
            pr = PRESETS[n]
            r = self._r(i)
            s = 1.08 if i == self.hov else 1.0
            d = r.width() * s
            rr = QRectF(r.center().x() - d / 2, r.center().y() - d / 2, d, d)
            g = QLinearGradient(rr.topLeft(), rr.bottomRight())
            g.setColorAt(0, QColor(pr["acc2"]))
            g.setColorAt(1, QColor(pr["acc"]))
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(rr)
            if n == cur:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(ink(230), 2))
                p.drawEllipse(rr.adjusted(-4, -4, 4, 4))
            p.setPen(ink(235 if (n == cur or i == self.hov) else 140))
            p.setFont(app_font(8.5, n == cur))
            p.drawText(QRectF(r.center().x() - 34, r.bottom() + 6, 68, 16), Qt.AlignCenter, n)


class LoginCard(QWidget):
    """Anmeldekarte wie im Vorbild: halbdurchsichtige Glas-Karte ueber blauen Schwaden."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(500)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(44, 42, 44, 34)
        self.lay.setSpacing(10)

    def fit(self):
        self.lay.activate()
        self.setFixedHeight(self.lay.sizeHint().height())

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, QColor(255, 255, 255, 40))
        g.setColorAt(0.5, QColor(255, 255, 255, 18))
        g.setColorAt(1, QColor(255, 255, 255, 28))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 14, 30, 70))
        p.drawRoundedRect(r, 22, 22)
        p.setBrush(g)
        p.drawRoundedRect(r, 22, 22)
        bg = QLinearGradient(r.topLeft(), r.bottomRight())
        bg.setColorAt(0, QColor(255, 255, 255, 90))
        bg.setColorAt(1, QColor(255, 255, 255, 35))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(bg, 1))
        p.drawRoundedRect(r, 22, 22)


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
        self.setFixedHeight(126)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def _xr(self):
        return QRectF(self.width() - 15 - 30, 17, 30, 30)

    def _er(self):
        return QRectF(self.width() - 15 - 64, 17, 30, 30)

    def enterEvent(self, e):
        tween(self, "hov", 1.0, 130)

    def leaveEvent(self, e):
        tween(self, "hov", 0.0, 130)
        tween(self, "xhov", 0.0, 100)
        tween(self, "ehov", 0.0, 100)

    def mouseMoveEvent(self, e):
        self.mx, self.my = e.position().x(), e.position().y()
        self.update()
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
        r = QRectF(self.rect()).adjusted(1, 4, -1, -4)
        if self.appear < 1.0:
            p.setOpacity(max(0.0, self.appear))
            p.translate(0, (1 - self.appear) * 8)
        sel = 1.0 if self.selected else 0.0
        paint_glass(p, self, r, 18, 150 + 30 * self.hov + 60 * sel, tint=self.selected,
                    sheen=(getattr(self, "mx", 0.0), getattr(self, "my", 0.0), self.hov))
        c = self.c
        # Vorschaubild rund wie ein Profilbild
        t = QRectF(r.left() + 14, r.top() + 14, 34, 34)
        path = QPainterPath()
        path.addEllipse(t)
        p.save()
        p.setClipPath(path)
        if c.thumb:
            pw, ph = c.thumb.width(), c.thumb.height()
            s = min(pw, ph)
            p.drawPixmap(t, c.thumb, QRectF((pw - s) / 2, (ph - s) / 2, s, s))
        else:
            p.fillRect(t, ink(30))
        p.restore()
        tx = t.right() + 12
        avail = max(40, r.right() - 84 - tx)
        p.setPen(ink(135))
        p.setFont(app_font(8.5))
        cap = (self.title_text or f"Clip {self.i + 1}") + ("  ·  exportiert" if c.last_name else "")
        p.drawText(QRectF(tx, r.top() + 12, avail, 16), Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(cap, Qt.ElideRight, int(avail)))
        p.setPen(ink(240))
        p.setFont(app_font(10.5, True))
        p.setFont(display_font(11))
        name = f"{fmt_short(c.start)} – {fmt_short(c.end)}"
        p.drawText(QRectF(tx, r.top() + 29, avail, 20), Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(name, Qt.ElideRight, int(avail)))
        er, xr = self._er(), self._xr()
        for rr, icon, hv, col in ((er, "edit", self.ehov, T.icon), (xr, "close", self.xhov, T.err)):
            p.setPen(QPen(T.line(1.2 + hv), 1))
            p.setBrush(T.card_fill(120 + 60 * hv))
            p.drawEllipse(rr)
            paint_icon(p, icon, rr.adjusted(8, 8, -8, -8), col if hv > 0.4 else ink(170), 1.5)
        # Kill-Zeitpunkt (gruen, wie "Good Match")
        p.setPen(T.okc)
        p.setFont(app_font(9))
        p.drawText(QRectF(r.left() + 14, r.top() + 56, r.width() - 28, 18), Qt.AlignVCenter,
                   f"Kill bei {fmt_short(c.kill)}")
        # kleine Pillen unten
        x = r.left() + 14
        p.setFont(app_font(8.5))
        fm = QFontMetrics(p.font())
        for txt in (f"{c.end - c.start:.1f} s", f"Start {fmt_short(c.start)}"):
            w = fm.horizontalAdvance(txt) + 22
            pr = QRectF(x, r.bottom() - 34, w, 24)
            p.setPen(QPen(T.line(1.4), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(pr, 12, 12)
            p.setPen(ink(190))
            p.drawText(pr, Qt.AlignCenter, txt)
            x += w + 6
        p.setPen(ink(110))
        p.drawText(QRectF(r.right() - 90, r.bottom() - 34, 76, 24), Qt.AlignVCenter | Qt.AlignRight,
                   f"#{self.i + 1:02d}")


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
    double = Signal()

    def mouseDoubleClickEvent(self, e):
        if self.has_video:
            self.double.emit()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.img = None
        self.arr = None
        self.time_text = "00:00.00"
        self.kill = False
        self.has_video = False
        self.cover = None

    view = None                  # Bereich, in dem das Bild gezeigt wird (zwischen den schwebenden Karten)

    def area(self):
        return QRectF(self.view) if self.view is not None else QRectF(self.rect())

    def resizeEvent(self, e):
        self.sync_cover()

    def sync_cover(self):
        if self.cover is not None:
            a = self.area()
            self.cover.setGeometry(int(a.x()), int(a.y()), int(a.width()), int(a.height()))

    def set_frame(self, rgb):
        h, w = rgb.shape[:2]
        self.arr = rgb
        self.img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        self.img.setDevicePixelRatio(self.devicePixelRatioF())
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        a = self.area()
        r = a.adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 28, 28)
        p.setClipPath(path)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        c0, c1 = T.video_stops()
        g.setColorAt(0, c0)
        g.setColorAt(1, c1)
        p.fillRect(a, g)
        if self.img is not None and self.has_video:
            dpr = self.devicePixelRatioF()
            iw, ih = self.img.width() / dpr, self.img.height() / dpr
            p.drawImage(QPointF(a.x() + (a.width() - iw) / 2, a.y() + (a.height() - ih) / 2), self.img)
        # weiche Abdunklung oben (Titel) und unten (Bedienung), damit alles auf jedem Bild lesbar bleibt
        sh = QLinearGradient(0, r.top(), 0, r.bottom())
        sh.setColorAt(0, QColor(0, 0, 0, 150))
        sh.setColorAt(0.28, QColor(0, 0, 0, 0))
        sh.setColorAt(0.72, QColor(0, 0, 0, 0))
        sh.setColorAt(1, QColor(0, 0, 0, 160))
        p.fillRect(r, sh)
        p.setClipping(False)
        p.setPen(QPen(T.line(1.6), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 28, 28)
        if self.has_video:
            p.setFont(app_font(10, True))
            tw = QFontMetrics(p.font()).horizontalAdvance(self.time_text) + 24
            tr = QRectF(a.right() - 22 - tw, a.y() + 24, tw, 28)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(10, 10, 14, 170))
            p.drawRoundedRect(tr, 14, 14)
            p.setPen(QColor("white"))
            p.drawText(tr, Qt.AlignCenter, self.time_text)
            if self.kill:
                kr = QRectF(tr.left() - 8 - 92, a.y() + 24, 92, 28)
                p.setPen(Qt.NoPen)
                p.setBrush(RED)
                p.drawRoundedRect(kr, 14, 14)
                p.setPen(QColor("white"))
                p.drawText(kr, Qt.AlignCenter, "\u25cf  KILL")


def panel(title, sub="", radius=30, alpha=130):
    """Karte mit Titel und grauer Unterzeile; gibt (Karte, Layout) zurueck."""
    g = Glass(None, radius=radius, alpha=alpha)
    lay = QVBoxLayout(g)
    lay.setContentsMargins(20, 18, 20, 18)
    lay.setSpacing(10)
    head = QVBoxLayout()
    head.setSpacing(2)
    tl_ = QLabel(title)
    tl_.setFont(display_font(12.5, QFont.DemiBold))
    head.addWidget(tl_)
    if sub:
        head.addWidget(ElidedLabel(sub, 9, False, MUT))
    lay.addLayout(head)
    lay.addSpacing(2)
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


class ColorChoice(QWidget):
    """Reihe runder Farben zum Anklicken (leer = Standard). Nur der Admin sieht sie im Server-Dialog."""
    changed = Signal(str)

    def __init__(self, current="", parent=None):
        super().__init__(parent)
        self.cur = current if current in PRESETS else ""
        self.names = [""] + list(PRESETS)
        self.setFixedHeight(66)
        self.setCursor(Qt.PointingHandCursor)

    def _rect(self, i):
        return QRectF(2 + i * 40, 4, 30, 30)

    def mousePressEvent(self, e):
        for i, n in enumerate(self.names):
            if self._rect(i).adjusted(-5, -5, 5, 5).contains(e.position()):
                self.cur = n
                self.changed.emit(n)
                self.update()
                return

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        for i, n in enumerate(self.names):
            r = self._rect(i)
            if n:
                pr = PRESETS[n]
                g = QLinearGradient(r.topLeft(), r.bottomRight())
                g.setColorAt(0, QColor(pr["acc2"]))
                g.setColorAt(1, QColor(pr["acc"]))
                p.setPen(Qt.NoPen)
                p.setBrush(g)
                p.drawEllipse(r)
            else:
                p.setPen(QPen(ink(110), 1.6))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(r.adjusted(1, 1, -1, -1))
                p.drawLine(r.bottomLeft() + QPointF(7, -7), r.topRight() + QPointF(-7, 7))
            if n == self.cur:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(ink(230), 2))
                p.drawEllipse(r.adjusted(-4, -4, 4, 4))
        p.setPen(ink(150))
        p.setFont(app_font(9.5))
        p.drawText(QRectF(0, 40, self.width(), 22), Qt.AlignLeft | Qt.AlignVCenter,
                   self.cur or "Standard – wie in den Einstellungen")


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


class EditorCanvas(QWidget):
    """Editor-Seite: links Clip-Spalte, Mitte grosse Video-Buehne mit Titel und Bedienung darauf,
    unten die Zeitleiste, rechts Suche und Export (Aufbau wie im Vorbild)."""
    G = 12
    LW, RW = 304, 312
    TL = 236

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parts = {}

    def resizeEvent(self, e):
        self.place()

    def place(self):
        p = self.parts
        if not p:
            return
        W, H, g = self.width(), self.height(), self.G
        vis = lambda n: not p[n].isHidden()
        x0 = self.LW + g if vis("left") else 0
        x1 = W - (self.RW + g if vis("right") else 0)
        cw = max(240, x1 - x0)
        if vis("left"):
            p["left"].setGeometry(0, 0, self.LW, H)
        if vis("right"):
            p["right"].setGeometry(W - self.RW, 0, self.RW, H)
        p["tl"].setGeometry(x0, H - self.TL, cw, self.TL)
        hero = QRectF(x0, 0, cw, H - self.TL - g)
        v = p["video"]
        v.setGeometry(0, 0, W, H)
        v.view = hero
        v.sync_cover()
        v.update()
        hx, hy, hw, hh = int(hero.x()), int(hero.y()), int(hero.width()), int(hero.height())
        if vis("title"):
            p["title"].setGeometry(hx + 26, hy + 22, min(620, hw - 200), 112)
        cl, cr = p["ctrl_l"], p["ctrl_r"]
        cl.adjustSize()
        cr.adjustSize()
        by = hy + hh - 20 - 52
        cl.setGeometry(hx + 20, by, cl.sizeHint().width(), 52)
        cr.setGeometry(hx + hw - 20 - cr.sizeHint().width(), by, cr.sizeHint().width(), 52)
        p["seek"].setGeometry(hx + 26, by - 26, hw - 52, 16)
        ub = p.get("unsaved")
        if ub is not None:
            uw = min(520, hw - 40)
            k_ = max(0.0, min(1.2, ub.k))
            ub.setGeometry(int(hx + (hw - uw) / 2 + ub.shake), int(by - 26 - 14 - 52 + 18 * (1 - k_)), uw, 52)
        for n in ("left", "right", "tl", "title", "ctrl_l", "ctrl_r", "seek", "unsaved"):
            if n in p and vis(n):
                p[n].raise_()


class HeroCluster(QWidget):
    """Dunkle Pille auf dem Video, in der Bedienknoepfe sitzen (bleibt auf jedem Bild lesbar)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(6, 0, 6, 0)
        self.lay.setSpacing(2)

    def childEvent(self, e):
        super().childEvent(e)
        w = e.child()
        if isinstance(w, (PopButton, Segmented)):
            w.on_dark = True

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 2.5, -0.5, -2.5)
        p.setPen(QPen(QColor(255, 255, 255, 34), 1))
        p.setBrush(QColor(8, 9, 10, 175))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)


class IconDot(QWidget):
    """Runder Icon-Knopf der oberen Leiste; dot=True zeigt einen kleinen roten Punkt (z. B. Update da)."""
    clicked = Signal()

    def __init__(self, icon, tip="", parent=None):
        super().__init__(parent)
        self.icon, self.dot, self.pop = icon, False, 0.0
        self.setFixedSize(40, 40)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tip)

    def set_dot(self, on):
        self.dot = on
        self.update()

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 140)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 140)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        paint_glass(p, self, r, r.height() / 2, 130 + 60 * self.pop)
        paint_icon(p, self.icon, QRectF(r.center().x() - 8, r.center().y() - 8, 16, 16), ink(int(170 + 60 * self.pop)), 1.6)
        if self.dot:
            p.setPen(QPen(T.bg_stops()[0], 2))
            p.setBrush(RED)
            p.drawEllipse(QPointF(r.right() - 7, r.top() + 7), 4.5, 4.5)


class ServerChip(QWidget):
    """Kleine Server-Anzeige oben rechts im Editor (klickbar -> Server-Seite)."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.name = ""
        self.pop = 0.0
        self.setFixedHeight(44)
        self.setFixedWidth(230)
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
        r = QRectF(self.rect()).adjusted(1, 4, -1, -4)
        paint_glass(p, self, r, r.height() / 2, 130 + 60 * self.pop)
        x = r.left() + 6
        if self.name:
            paint_avatar(p, QRectF(x, r.center().y() - 12, 24, 24), self.name, 12, 10, getattr(self, "sid", None))
            p.setPen(ink(225))
            p.setFont(app_font(9.5))
            p.drawText(QRectF(x + 32, r.top(), r.width() - 70, r.height()), Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(self.name, Qt.ElideRight, int(r.width() - 72)))
        else:
            paint_icon(p, "server", QRectF(x + 6, r.center().y() - 8, 16, 16), ink(120), 1.6)
            p.setPen(ink(120))
            p.setFont(app_font(9.5))
            p.drawText(QRectF(x + 32, r.top(), r.width() - 70, r.height()), Qt.AlignVCenter, "Server wählen")
        cx, cy = r.right() - 18, r.center().y()
        p.setPen(QPen(ink(150), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline(QPolygonF([QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2)]))


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
        self.setWindowTitle("ACE")
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
        self.theme_override = None
        self.fs = False
        self._was_max = True
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
        root = QVBoxLayout(self.bg)
        root.setContentsMargins(20, 12, 20, 20)
        root.setSpacing(10)
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
        self.dock = Dock([
            {"icon": "server", "label": "Server", "fn": lambda: self.nav_clicked(0)},
            {"icon": "film", "label": "Editor", "fn": lambda: self.nav_clicked(1)},
            {"icon": "folder", "label": "Bibliothek", "fn": lambda: self.nav_clicked(2)},
            {"icon": "gear", "label": "Einstellungen", "fn": lambda: self.nav_clicked(3)},
            {"icon": "upload", "label": "Video öffnen", "fn": self._top_open, "sep": True},
            {"icon": "moon", "label": "Hell / Dunkel", "fn": self._toggle_dark, "nolabel": True},
        ], self.bg)
        self.dock.on_geom = lambda big: self.place_dock(big)
        self.dock.hovered.connect(self._dock_hover)
        self.theme_pop = ThemePopover(self.bg, self.set_mode)
        self.theme_pop.hide()
        self.nav = [_NavProxy(self.dock, i) for i in range(4)]
        self.nav[1].set_nav_enabled(False)
        self.navbar = self.dock
        self.dock.select = lambda i, animate=True: self.nav[i].set_selected(True)
        self.server_menu = ServerMenu(self.bg, self.switch_server, lambda: self.go_page(0))
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
        QTimer.singleShot(150, self.startup)
        QTimer.singleShot(500, self.stack.warm)

    # ------------------------------------------------------------ Infrastruktur
    def ui(self, fn, *args):
        self.bridge.call.emit(fn, args)

    def load_settings(self):
        d = {"pre": 4.0, "post": 2.0, "sens": 6, "mode": "Genau", "open": True, "loop": True,
             "autoupd": True, "gpu": True, "servers": [], "names": {}, "last_server": "bruder",
             "hidden": [], "export_dir": "", "thorough": False, "dark": True, "acc": DEFAULT["acc"], "acc2": DEFAULT["acc2"], "bg": DEFAULT["bg"]}
        try:
            with open(SETTINGS) as f:
                d.update(json.load(f))
        except Exception:
            pass
        if d.get("mode") in ("Verlustfrei", "Bildgenau") or d.get("mode") not in MODES:
            d["mode"] = "Genau"                 # alte Namen -> bildgenauer Modus (Schnitt stimmt auf das Bild)
        if d.get("theme_ver", 0) < 2:           # neues Standard-Grau: einmalig auf das Standardthema zuruecksetzen
            d.update(acc=DEFAULT["acc"], acc2=DEFAULT["acc2"], bg=DEFAULT["bg"], theme_ver=2)
        if not isinstance(d.get("servers"), list):
            d["servers"] = []
        if not isinstance(d.get("names"), dict):
            d["names"] = {}
        return d

    def save_settings(self):
        self.st.update(pre=self.s_pre.value(), post=self.s_post.value(), sens=int(round(self.s_sens.value())),
                       mode=self.seg_mode.current(), open=self.tg_open.isChecked(), loop=self.tg_loop.isChecked(),
                       gpu=self.tg_gpu.isChecked(),
                       thorough=self.tg_thorough.isChecked())
        try:
            with open(SETTINGS, "w") as f:
                json.dump(self.st, f)
        except Exception:
            pass

    def closeEvent(self, e):
        n = sum(1 for c in self.clips if not c.last_name)
        if n and not getattr(self, "_close_ok", False):
            e.ignore()
            if self.stack.cur != 1:
                self.go_page(1)
            self.unsaved.nudge()
            card = DialogCard("Ungespeicherte Änderungen", f"{n} {'Clip ist' if n == 1 else 'Clips sind'} noch nicht "
                              "exportiert. Wenn du ACE jetzt schließt, gehen sie verloren.", 480)
            no = PopButton("Abbrechen", None, "glass", 40)
            no.clicked.connect(self.overlay.dismiss)
            ex = PopButton("Exportieren", "upload", "glass", 40)
            ex.clicked.connect(lambda: (self.overlay.dismiss(), self.export(False)))
            yes = PopButton("Trotzdem schließen", None, "danger", 40)
            yes.clicked.connect(lambda: (setattr(self, "_close_ok", True), self.close()))
            card.btns.addWidget(no)
            card.btns.addWidget(ex)
            card.btns.addWidget(yes)
            self.overlay.present(card)
            return
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
    def place_dock(self, big=None):
        """Dock oben in der Mitte; nur waehrend die Maus darauf ist, reicht es nach unten (fuer Groesse und Namen)."""
        d = self.dock
        if big is None:
            big = d._exp
        w = d.total_width() + 160
        h = Dock.PANEL + (Dock.HEAD + 14 if big else 2)
        d.setGeometry(int((self.bg.width() - w) / 2), 12, w, h)
        d.raise_()
        if hasattr(self, "theme_pop") and self.theme_pop.isVisible():
            self.theme_pop.raise_()
        if hasattr(self, "overlay") and self.overlay.isVisible():
            self.overlay.raise_()

    def toggle_fullscreen(self):
        """Vollbild-Bearbeitung: nur Video, Clips, Zeitleiste und Player-Leiste."""
        if not self.has_video or self.stack.cur != 1:
            return
        on = not self.fs
        self.fs = on
        if on:
            self._was_max = self.isMaximized()
            self.showFullScreen()
        else:
            self.showMaximized() if self._was_max else self.showNormal()
        for w in (self.sidebar, self.right_col, self.pill, self.left_panel):
            w.setVisible(not on)
        self.bg.layout().setContentsMargins(*((12, 12, 12, 12) if on else (20, 12, 20, 20)))
        self.dock.setVisible(not on)
        self.place_dock()
        self.page_editor.place()
        self.fs_btn.icon_name = "shrink" if on else "expand"
        self.fs_btn.setToolTip("Vollbild beenden (Esc)" if on else "Vollbild (F)")
        self.fs_btn.update()
        QTimer.singleShot(60, lambda: self.player and self.player.request_seek(self.pos))

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
            rad = 0.0 if (self.isMaximized() or self.isFullScreen()) else 26.0
            if rad != self.bg.radius:
                self.bg.radius = rad
                self.bg.update()
        if hasattr(self, "dock"):
            self.place_dock()
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
        """Obere Leiste wie im Vorbild: Logo, Text-Reiter, rechts Server-Auswahl und runde Icon-Knoepfe."""
        sb = QWidget()
        sb.setFixedHeight(64)
        lay = QHBoxLayout(sb)
        lay.setContentsMargins(4, 0, 150, 0)
        lay.setSpacing(6)
        logo = Glass(radius=40, alpha=130)
        logo.setFixedSize(48, 48)
        ll = QHBoxLayout(logo)
        ll.setContentsMargins(0, 0, 0, 0)
        lg = QLabel()
        lg.setPixmap(QPixmap(resource("assets", "logo.png")).scaled(26, 26, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lg.setAlignment(Qt.AlignCenter)
        ll.addWidget(lg)
        lay.addWidget(logo)
        lay.addSpacing(8)
        nm = QLabel("ACE")
        nm.setFont(display_font(15, QFont.Bold))
        lay.addWidget(nm)
        lay.addSpacing(10)
        self.page_lbl = label("", 10, color=MUT)
        lay.addWidget(self.page_lbl)
        lay.addStretch(1)
        self.top_chip = ServerChip()
        self.top_chip.clicked.connect(self.open_server_menu)
        lay.addWidget(self.top_chip)
        lay.addSpacing(4)
        self.upd_dot = IconDot("download", "Update")
        self.upd_dot.clicked.connect(self._top_update)
        lay.addSpacing(6)
        lay.addWidget(self.upd_dot)
        self.btn_update = PopButton("Update installieren", "upload", "glass", 36)
        self.btn_update.clicked.connect(self.do_update)
        self.btn_update.hide()
        lay.addWidget(self.btn_update)
        self.ver = label(f"v{core.APP_VERSION}", 9, color=DIM)
        self.ver.hide()
        self.side_scroll = QScrollArea(sb)
        self.side_scroll.hide()
        return sb

    def open_server_menu(self):
        """Ohne gewaehlten Server: grosse Server-Seite. Sonst: Auswahl klappt direkt unter dem Server auf."""
        if self.server is None or not hasattr(self, "server_menu"):
            self.go_page(0)
            return
        a = self.top_chip
        pos = a.mapTo(self.bg, a.rect().topLeft())
        from PySide6.QtCore import QRect
        self.server_menu.open_menu(self.all_servers(), self.server["id"], QRect(pos, a.size()))

    def _top_open(self):
        if self.server is None:
            self.say("Wähle zuerst einen Server", AMBER)
            self.go_page(0)
            return
        self.go_page(1)
        self.open_video()

    def set_mode(self, dark):
        self.apply_theme(dark=dark)
        if hasattr(self, "seg_dark"):
            self.seg_dark.set_current("Dunkel" if dark else "Hell")
        self.theme_pop.sync()

    def _dock_hover(self, i):
        """Ueber Hell/Dunkel im Dock: kleines Fenster mit beiden Moeglichkeiten aufklappen."""
        ti = len(self.dock.items) - 1
        if i == ti:
            xs, tw = self.dock._layout()
            off = (self.dock.width() - tw) / 2
            cx = self.dock.x() + off + xs[ti] + self.dock.w[ti] / 2
            self.theme_pop.open_at(int(cx), self.dock.y() + Dock.PANEL + 20)

    def _toggle_dark(self):
        self.apply_theme(dark=not self.st["dark"])
        if hasattr(self, "seg_dark"):
            self.seg_dark.set_current("Dunkel" if self.st["dark"] else "Hell")

    def _top_update(self):
        if self.update_url:
            self.do_update()
        else:
            self.start_update_check(True)

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
        if self.fs and i != 1:
            self.toggle_fullscreen()
        want = self.st.get("themes", {}).get(self.server["id"]) if (self.server and i in (1, 2)) else None
        if want != self.theme_override:
            self.set_override(want)
        if cur == 1 and i != 1 and self.playing:
            self.toggle_play()
        for k, n in enumerate(self.nav):
            n.set_selected(k == i)
        if hasattr(self, "page_lbl"):
            self.page_lbl.setText("/  " + ("Server", "Editor", "Bibliothek", "Einstellungen")[i])
        self.navbar.select(i, animate)
        if animate:
            self.stack.go(i, 1 if i >= max(cur, 0) else -1)
        else:
            self.stack.go(i)
        if i == 1 and self.player:
            QTimer.singleShot(60, lambda: self.player.request_seek(self.pos))
        if i == 2:
            self.refresh_library()
        if i == 3 and self.role == "admin" and hasattr(self, "users_box"):
            self.load_users()

    # ------------------------------------------------------------ Seite: Server
    def page_header(self, title, sub):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 0, 0)
        v.setSpacing(4)
        t = label(title, 22, False)
        t.setFont(hero_font(22))
        v.addWidget(t)
        v.addWidget(label(sub, 9.5, color=MUT))
        return w

    def build_server_page(self):
        main = Glass(radius=32, alpha=0)
        main.ghost = True
        lay = QVBoxLayout(main)
        lay.setContentsMargins(30, 26, 22, 22)
        lay.addWidget(self.page_header("Server", "Wähle den Server, auf dem du aufgenommen hast – "
                                       "ACE nutzt dann die passende Kill-Erkennung."))
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

    def _sync_left_srv(self):
        if hasattr(self, "left_srv"):
            self.left_srv.setText(self.server["name"] if self.server else "–")

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
        add = ServerCard(add=True)
        if self.is_admin:
            add.clicked.connect(lambda: self.open_add_server())
        else:
            add.request = True                    # Spieler: Server beim Admin anfragen
            add.clicked.connect(self.open_request_server)
        grid.addWidget(add, len(items) // cols, len(items) % cols)
        grid.setColumnStretch(cols, 1)
        grid.setRowStretch(len(items) // cols + 1, 1)
        self.srv_scroll.setWidget(host)
        self.srv_host = host
        self._sync_left_srv()
        self.render_side_servers()

    def switch_server(self, s):
        """Server wechseln, ohne die aktuelle Seite zu verlassen."""
        if self.server and self.server["id"] == s["id"]:
            return
        stay = self.stack.cur if self.stack.cur in (1, 2, 3) else 1
        self.enter_server(s)
        if self.stack.cur != stay:
            self.go_page(stay)
        self.say(f"Server: {s['name']}")

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
        card = DialogCard("Neuer Server", "Lege fest, woran ACE auf diesem Server einen Kill erkennt.")
        card.body.addWidget(label("Name", 10.5, True, MUT))
        name = QLineEdit(st["name"])
        name.setPlaceholderText("z. B. Mein Server")
        self.style_edit(name)
        card.body.addWidget(name)
        ap = self.avatar_picker(st, name.text)
        name.textChanged.connect(lambda _: ap.refresh())
        card.body.addWidget(ap)
        card.body.addWidget(label("Farbe dieses Servers", 10.5, True, MUT))
        cc = ColorChoice(st.get("theme", ""))
        cc.changed.connect(lambda n: st.__setitem__("theme", n))
        card.body.addWidget(cc)
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
            self.set_server_theme(sid, st.get("theme", ""))
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
        ast = {"avatar": None, "removed": False, "theme": self.st.get("themes", {}).get(s["id"], "")}
        ap = self.avatar_picker(ast, name.text, s["id"])
        name.textChanged.connect(lambda _: ap.refresh())
        card.body.addWidget(ap)
        card.body.addWidget(label("Farbe dieses Servers", 10.5, True, MUT))
        cc = ColorChoice(self.st.get("themes", {}).get(s["id"], ""))
        cc.changed.connect(lambda n: ast.__setitem__("theme", n))
        card.body.addWidget(cc)
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
            self.set_server_theme(s["id"], ast["theme"])
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
        card = DialogCard("Server löschen?", f"„{s['name']}“ wird entfernt. Das kann nicht rückgängig gemacht werden.", 460)
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
        self.st.get("themes", {}).pop(s["id"], None)
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
        cv = EditorCanvas()
        self.ed_chip = self.top_chip
        self.pill_icon = QLabel()            # (nur noch intern genutzt)

        # ---- Video-Buehne
        self.video = VideoWidget(cv)
        self.video.double.connect(self.toggle_fullscreen)
        cta = QWidget(self.video)
        cl = QVBoxLayout(cta)
        cl.setAlignment(Qt.AlignCenter)
        ib = QLabel()
        ib.setPixmap(icon_pixmap("film", QColor(255, 255, 255, 220), 36, 1.5))
        ib.setFixedSize(76, 76)
        ib.setAlignment(Qt.AlignCenter)
        ib.setProperty("role", "ctaicon")
        cl.addWidget(ib, 0, Qt.AlignHCenter)
        cl.addSpacing(8)
        cl.addWidget(label("Ziehe eine Aufnahme hierher", 12, True, "onvideo"), 0, Qt.AlignHCenter)
        cl.addWidget(label("oder öffne sie über den Knopf.", 9.5, False, "onvideo2"), 0, Qt.AlignHCenter)
        cl.addSpacing(10)
        ob = PopButton("Video öffnen", "folder", "accent", 40)
        ob.clicked.connect(self.open_video)
        cl.addWidget(ob, 0, Qt.AlignHCenter)
        self.cta = cta
        self.video.cover = cta

        # Titel oben links auf der Buehne
        tb = QWidget(cv)
        self.pill = tb
        tv = QVBoxLayout(tb)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(4)
        self.file_lbl = ElidedLabel("Noch kein Video", 22, False, "onvideo")
        self.file_lbl.setFont(hero_font(23))
        tv.addWidget(self.file_lbl)
        self.meta_lbl = ElidedLabel("Wähle eine Aufnahme aus · OBS oder NVIDIA", 9.5, False, "onvideo2")
        tv.addWidget(self.meta_lbl)
        tv.addSpacing(6)
        pr = QHBoxLayout()
        pr.setSpacing(6)
        self.open_btn = PopButton("Video öffnen", "folder", "dark", 32)
        self.open_btn.clicked.connect(self.open_video)
        pr.addWidget(self.open_btn)
        self.close_btn = PopButton("", "close", "dark", 32, icon_only=True)
        self.close_btn.setToolTip("Video entfernen")
        self.close_btn.clicked.connect(self.ask_unload)
        self.close_btn.hide()
        pr.addWidget(self.close_btn)
        pr.addStretch(1)
        tv.addLayout(pr)
        tv.addStretch(1)

        # Bedienung unten auf der Buehne
        self.seekbar = SeekBar(self)
        self.seekbar.setParent(cv)
        self.seekbar.seek.connect(self.seek)
        ctl = HeroCluster(cv)
        b = PopButton("", "prev", "flat", 40, icon_only=True)
        b.clicked.connect(lambda: self.step_clip(-1))
        ctl.lay.addWidget(b)
        b = PopButton("\u22121 s", None, "flat", 40)
        b.clicked.connect(lambda: self.seek(self.pos - 1))
        ctl.lay.addWidget(b)
        self.play_btn = PopButton("", "play", "accent", 40, icon_only=True)
        self.play_btn.clicked.connect(self.toggle_play)
        ctl.lay.addWidget(self.play_btn)
        b = PopButton("+1 s", None, "flat", 40)
        b.clicked.connect(lambda: self.seek(self.pos + 1))
        ctl.lay.addWidget(b)
        b = PopButton("", "next", "flat", 40, icon_only=True)
        b.clicked.connect(lambda: self.step_clip(1))
        ctl.lay.addWidget(b)
        ctr = HeroCluster(cv)
        self.loop_btn = PopButton("", "refresh", "flat", 40, icon_only=True)
        self.loop_btn.setToolTip("Clip in der Vorschau wiederholen")
        ctr.lay.addWidget(self.loop_btn)
        self.fs_btn = PopButton("", "expand", "flat", 40, icon_only=True)
        self.fs_btn.setToolTip("Vollbild (F)")
        self.fs_btn.clicked.connect(self.toggle_fullscreen)
        ctr.lay.addWidget(self.fs_btn)
        self.speed_seg = Segmented(["0.5\u00d7", "1\u00d7", "2\u00d7"], height=34)
        self.speed_seg.setFixedWidth(138)
        self.speed_seg.set_current("1\u00d7")
        self.speed_seg.changed.connect(self.set_speed)
        ctr.lay.addWidget(self.speed_seg)
        # Datei-Infos fuer die Leiste (nur intern)
        self.bar_thumb = QLabel(cv)
        self.bar_title = ElidedLabel("", 10, True)
        self.bar_sub = ElidedLabel("", 9, False, MUT)
        for w_ in (self.bar_thumb, self.bar_title, self.bar_sub):
            w_.hide()

        # ---- linke Spalte: Kennzahlen + Clips
        left = Glass(cv, radius=30, alpha=120)
        self.left_panel = left
        ml = QVBoxLayout(left)
        ml.setContentsMargins(14, 14, 10, 12)
        ml.setSpacing(10)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)

        def tile(caption, value_lbl):
            t = Glass(radius=24, alpha=150)
            t.setFixedHeight(84)
            tl = QVBoxLayout(t)
            tl.setContentsMargins(14, 12, 14, 12)
            tl.setSpacing(2)
            tl.addWidget(label(caption, 8.5, color=MUT))
            value_lbl.setFont(hero_font(18))
            tl.addWidget(value_lbl)
            return t
        self.count_lbl = label("0")
        self.total_lbl = label("0:00")
        tiles.addWidget(tile("Clips", self.count_lbl))
        tiles.addWidget(tile("Gesamtlänge", self.total_lbl))
        ml.addLayout(tiles)
        st = Glass(radius=24, alpha=150)
        st.setFixedHeight(64)
        sl = QHBoxLayout(st)
        sl.setContentsMargins(14, 8, 8, 8)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(label("Aktueller Server", 8.5, color=MUT))
        self.left_srv = ElidedLabel("–", 10.5, True)
        col.addWidget(self.left_srv)
        sl.addLayout(col, 1)
        sb_ = PopButton("Wechseln", "chevron", "glass", 30)
        sb_.clicked.connect(lambda: self.go_page(0))
        sl.addWidget(sb_)
        st.hide()                                    # (Server wechselt man oben rechts)
        self._srv_tile = st
        hr = QHBoxLayout()
        hr.setContentsMargins(4, 6, 4, 0)
        self.ed_title = label("Deine Clips", 11.5, True)
        hr.addWidget(self.ed_title)
        hr.addStretch(1)
        hr.addWidget(label("Doppelklick = umbenennen", 8.5, color=DIM))
        ml.addLayout(hr)
        self.clip_scroll = QScrollArea()
        self.clip_scroll.setWidgetResizable(True)
        self.clip_scroll.setFrameShape(QScrollArea.NoFrame)
        self.clip_scroll.viewport().setAutoFillBackground(False)
        ml.addWidget(self.clip_scroll, 1)

        # ---- unten Mitte: Zeitleiste
        tlc = Glass(cv, radius=30, alpha=120)
        self.timeline_panel = tlc
        tlv = QVBoxLayout(tlc)
        tlv.setContentsMargins(18, 14, 18, 12)
        tlv.setSpacing(8)
        th = QHBoxLayout()
        th.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(icon_pixmap("crosshair", T.icon, 18))
        ic.setFixedSize(34, 34)
        ic.setAlignment(Qt.AlignCenter)
        ic.setProperty("role", "thumbbg")
        th.addWidget(ic)
        hc = QVBoxLayout()
        hc.setSpacing(0)
        hc.addWidget(label("Zeitleiste", 11.5, True))
        hc.addWidget(ElidedLabel("Ränder ziehen = Clip anpassen · Scrollen = Zoom", 9, False, MUT))
        th.addLayout(hc, 1)
        for tx, ic_, fn in (("Start  I", None, lambda: self.set_edge(0)),
                            ("Ende  O", None, lambda: self.set_edge(1)),
                            ("Clip hier", "plus", self.add_clip)):
            b = PopButton(tx, ic_, "glass", 30)
            b.clicked.connect(fn)
            th.addWidget(b)
        tlv.addLayout(th)
        self.timeline = Timeline(self)
        self.timeline.seek.connect(self.seek)
        self.timeline.pick.connect(lambda i: self.select(i, seek=False))
        self.timeline.edited.connect(self.clip_changed)
        tlv.addWidget(self.timeline, 1)

        # ---- rechte Spalte: Suche + Export
        rw = QWidget(cv)
        self.right_col = rw
        rl = QVBoxLayout(rw)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(12)
        g, lay = panel("Kills suchen", "Findet die Kill-Meldungen automatisch")
        self.s_pre = SliderRow("Vor dem Kill", 0, 10, 100, self.st["pre"], lambda v: f"{v:.1f} s")
        self.s_post = SliderRow("Nach dem Kill", 0, 10, 100, self.st["post"], lambda v: f"{v:.1f} s")
        names = ["", "sehr niedrig", "niedrig", "niedrig", "mittel", "mittel", "mittel+", "hoch", "hoch", "sehr hoch",
                 "max"]
        self.s_sens = SliderRow("Empfindlichkeit", 1, 10, 0, self.st["sens"], lambda v: names[int(round(v))])
        for s_ in (self.s_pre, self.s_post, self.s_sens):
            lay.addWidget(s_)
        self.scan_btn = PopButton("Kills suchen", "crosshair", "accent", 42)
        self.scan_btn.clicked.connect(self.start_scan)
        lay.addWidget(self.scan_btn)
        self.scan_bar = Bar(height=4)
        lay.addWidget(self.scan_bar)
        self.status = ElidedLabel("Bereit", 9, False, MUT)
        lay.addWidget(self.status)
        rl.addWidget(g)
        g, lay = panel("Export", "Speichert die Clips in deinen Ordner")
        self.seg_mode = Segmented(list(MODES), height=34)
        self.seg_mode.set_current(self.st["mode"] if self.st["mode"] in MODES else "Genau")
        self.mode_desc = QLabel()
        self.mode_desc.setWordWrap(True)
        self.mode_desc.setFont(app_font(8.5))
        self.mode_desc.setProperty("role", "muted")
        self.mode_desc.setMinimumHeight(28)
        self.seg_mode.changed.connect(lambda v: self.mode_desc.setText(MODES[v][1]))
        self.mode_desc.setText(MODES[self.seg_mode.current()][1])
        lay.addWidget(self.seg_mode)
        lay.addWidget(self.mode_desc)
        lay.addWidget(self.make_dir_row())
        self.exp_all = PopButton("Alle exportieren", "upload", "accent", 42)
        self.exp_all.clicked.connect(lambda: self.export(False))
        lay.addWidget(self.exp_all)
        b = PopButton("Nur ausgewählten Clip", None, "glass", 36)
        b.clicked.connect(lambda: self.export(True))
        lay.addWidget(b)
        self.exp_bar = Bar(height=4)
        lay.addWidget(self.exp_bar)
        rl.addWidget(g)
        rl.addStretch(1)

        self.unsaved = UnsavedBar(cv, lambda: self.export(False))
        cv.parts = {"video": self.video, "title": tb, "left": left, "right": rw, "tl": tlc,
                    "ctrl_l": ctl, "ctrl_r": ctr, "seek": self.seekbar, "unsaved": self.unsaved}
        self.rebuild_cards()
        return cv

    # ------------------------------------------------------------ Seite: Einstellungen
    def build_settings_page(self):
        """Einstellungen als Bento-Raster (Karten mit Motiv, Serifen-Titel und Bedienung)."""
        main = Glass(radius=32, alpha=0)
        main.ghost = True
        lay = QVBoxLayout(main)
        lay.setContentsMargins(30, 18, 22, 22)
        cap = label("EINSTELLUNGEN", 8.5, True, MUT)
        lay.addWidget(cap)
        lay.addSpacing(8)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QScrollArea.NoFrame)
        sc.viewport().setAutoFillBackground(False)
        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 10, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)

        def toggle_row(parent_lay, title, sub, checked):
            r = QHBoxLayout()
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(label(title, 10, True))
            col.addWidget(ElidedLabel(sub, 8.5, False, MUT))
            r.addLayout(col, 1)
            t = Toggle(checked)
            r.addWidget(t)
            parent_lay.addLayout(r)
            return t

        # Darstellung (gross, links)
        look = BentoCard(None, "palette", "Darstellung", "Hell oder dunkel und die Farbe der ganzen App. "
                         "Server mit eigener Farbe nutzen ihre Farbe.")
        self.theme_preview = ThemePreview()
        look.lay.insertWidget(0, self.theme_preview, 1)
        look.body.addWidget(label("Modus", 9.5, True, MUT))
        self.seg_dark = Segmented(["Hell", "Dunkel"], height=42)
        self.seg_dark.set_current("Dunkel" if self.st["dark"] else "Hell")
        self.seg_dark.changed.connect(lambda v: self.apply_theme(dark=(v == "Dunkel")))
        look.body.addWidget(self.seg_dark)
        look.body.addSpacing(4)
        look.body.addWidget(label("Farbe", 9.5, True, MUT))
        self.theme_dots = ThemeDots()
        self.theme_dots.picked.connect(lambda n: self.apply_theme(**PRESETS[n]))
        look.body.addWidget(self.theme_dots)

        allg = BentoCard("pills", None, "Allgemein", "Vorschau und Export-Ordner.")
        self.tg_loop = toggle_row(allg.body, "Clip wiederholen", "Spielt den Clip in Schleife.", self.st["loop"])
        self.tg_open = toggle_row(allg.body, "Ordner öffnen", "Nach dem Export im Explorer.", self.st["open"])
        kill = BentoCard("load", None, "Kill-Suche", "Schnell oder gründlich suchen.")
        self.tg_gpu = toggle_row(kill.body, "GPU nutzen", "Beschleunigt das Dekodieren.", self.st["gpu"])
        self.tg_thorough = toggle_row(kill.body, "Gründlich", "Bild für Bild, langsamer.", self.st["thorough"])

        upd = BentoCard("rings", None, "Updates", f"Installierte Version {core.APP_VERSION}. Neue Versionen "
                        "kommen automatisch beim Start.")
        b = PopButton("Nach Updates suchen", "refresh", "glass", 38)
        b.clicked.connect(lambda: self.start_update_check(True))
        upd.body.addWidget(b)
        upd.body.addWidget(self.btn_update)

        exp = BentoCard("folders", "folder", "Export", "Wohin die fertigen Clips gespeichert werden.")
        exp.body.addWidget(self.make_dir_row())
        keys = BentoCard(None, None, "Tastenkürzel", "Schneller schneiden mit der Tastatur.")
        kg = QGridLayout()
        kg.setHorizontalSpacing(14)
        kg.setVerticalSpacing(4)
        for i, (kk, tt) in enumerate((("Leertaste", "Play / Pause"), ("\u2190 \u2192", "\u00b11 s (Shift: 1 Bild)"),
                                      ("\u2191 \u2193", "Clip wechseln"), ("I / O", "Start / Ende"),
                                      ("F", "Vollbild"), ("Entf", "Clip löschen"))):
            kl = label(kk, 9, True)
            kg.addWidget(kl, i // 2, (i % 2) * 2)
            kg.addWidget(label(tt, 9, color=MUT), i // 2, (i % 2) * 2 + 1)
        keys.body.addLayout(kg)

        grid.addWidget(look, 0, 0, 2, 2)
        grid.addWidget(allg, 0, 2, 1, 2)
        grid.addWidget(kill, 1, 2, 1, 2)
        grid.addWidget(upd, 0, 4, 2, 2)
        grid.addWidget(exp, 2, 0, 1, 3)
        grid.addWidget(keys, 2, 3, 1, 3)
        row = 3
        if core.ACCOUNT_URL:
            acct = BentoCard("users", "user", "Konto", "Anmeldung, Passwort und – für den Admin – Einladungen.")
            self.acct_g = acct
            self.acct_lay = QVBoxLayout()
            self.acct_lay.setSpacing(8)
            acct.body.addLayout(self.acct_lay)
            acct.vis.setMinimumHeight(80)
            self.refresh_account_panel()
            grid.addWidget(acct, 3, 0, 1, 6)
            row = 4
        grid.setRowStretch(row, 1)
        for c_ in range(6):
            grid.setColumnStretch(c_, 1)
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
        if hasattr(self, "theme_dots"):
            self.theme_dots.update()
            self.theme_preview.update()
        T.configure(*self.theme_colors())
        self._repaint_theme()
        self.save_settings()

    def set_server_theme(self, sid, name):
        d = self.st.setdefault("themes", {})
        if name in PRESETS:
            d[sid] = name
        else:
            d.pop(sid, None)
        if self.server and self.server["id"] == sid and self.stack.cur in (1, 2):
            self.set_override(d.get(sid))

    def theme_colors(self):
        if self.theme_override in PRESETS:
            p = PRESETS[self.theme_override]
            return self.st["dark"], p["acc"], p["acc2"], p["bg"]
        return self.st["dark"], self.st["acc"], self.st["acc2"], self.st["bg"]

    def _repaint_theme(self, qss=True, bg=True):
        if qss:
            QApplication.instance().setStyleSheet(T.qss())
        if bg:
            self.bg.refresh()
        self.pill_icon.setPixmap(icon_pixmap("film", T.icon, 20))
        for w in self.findChildren(QWidget):
            w.update()

    def set_override(self, name):
        """Serverfarbe weich einblenden (name leer = Farbe aus den Einstellungen)."""
        name = name if name in PRESETS else None
        if name == self.theme_override:
            return
        self.theme_override = name
        dark, acc, acc2, bg = self.theme_colors()
        a0 = (QColor(T.acc), QColor(T.acc2), QColor(T.bg))
        a1 = (QColor(acc), QColor(acc2), QColor(bg))
        if getattr(self, "_theme_anim", None):
            self._theme_anim.stop()
        from PySide6.QtCore import QVariantAnimation
        an = self._theme_anim = QVariantAnimation(self)
        an.setStartValue(0.0)
        an.setEndValue(1.0)
        an.setDuration(480)
        an.setEasingCurve(QEasingCurve(QEasingCurve.OutCubic))

        def mix(c0, c1, t):
            return QColor(int(c0.red() + (c1.red() - c0.red()) * t), int(c0.green() + (c1.green() - c0.green()) * t),
                          int(c0.blue() + (c1.blue() - c0.blue()) * t))

        def step(v):
            t = float(v)
            T.configure(dark, *(mix(x, y, t).name() for x, y in zip(a0, a1)))
            self._repaint_theme(qss=False, bg=False)      # Hintergrund erst am Ende neu berechnen (spart Rechenzeit)

        def done():
            T.configure(dark, acc, acc2, bg)
            self._repaint_theme()
        an.valueChanged.connect(step)
        an.finished.connect(done)
        an.start()

    def gate_theme(self, on):
        """Anmelde- und Update-Fenster immer im Standard-Grau zeigen; danach wieder das gewaehlte Thema."""
        if on:
            T.configure(False, DEFAULT["acc"], DEFAULT["acc2"], DEFAULT["bg"])
        else:
            T.configure(*self.theme_colors())
        self._repaint_theme()

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
        lay.addWidget(label("Server mit eigener Farbe (vom Admin gesetzt) nutzen stattdessen ihre Farbe.", 9.5, color=DIM))
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
        """Anmeldung wie im Vorbild: Glas-Karte ueber blauen Schwaden, Unterstrich-Felder, blauer Knopf."""
        self.login_open = True
        self.gate_theme(True)
        card = LoginCard()
        L = card.lay
        title = QLabel("Willkommen zurück")
        title.setFont(display_font(25, QFont.Bold))
        title.setStyleSheet("color: white;")
        title.setAlignment(Qt.AlignCenter)
        L.addWidget(title)
        sub = QLabel("Melde dich an, um weiterzumachen")
        sub.setFont(app_font(11))
        sub.setStyleSheet("color: rgba(255,255,255,215);")
        sub.setAlignment(Qt.AlignCenter)
        L.addWidget(sub)
        L.addSpacing(18)

        def field(icon, hint, pwd=False):
            e = QLineEdit()
            e.setPlaceholderText(hint)
            e.setProperty("role", "underline")
            e.setMinimumHeight(44)
            if pwd:
                e.setEchoMode(QLineEdit.Password)
            from PySide6.QtGui import QIcon
            e.addAction(QIcon(icon_pixmap(icon, QColor(255, 255, 255, 220), 18, 1.6)), QLineEdit.LeadingPosition)
            pal = e.palette()
            from PySide6.QtGui import QPalette
            pal.setColor(QPalette.PlaceholderText, QColor(255, 255, 255, 200))
            e.setPalette(pal)
            return e
        inv = field("ticket", "Einladungscode")
        inv.setText(invite)
        user = field("user", "Benutzername")
        pw = field("lock", "Passwort", True)
        for w_ in (inv, user, pw):
            L.addWidget(w_)
            L.addSpacing(8)
        forgot = QLabel("Passwort vergessen?")
        forgot.setFont(app_font(9.5, True))
        forgot.setStyleSheet("color: rgba(255,255,255,225);")
        forgot.setCursor(Qt.PointingHandCursor)
        L.addWidget(forgot)
        err = QLabel(note)
        err.setWordWrap(True)
        err.setFont(app_font(9.5, True))
        err.setStyleSheet("color: #ff9aa8;")
        L.addWidget(err)
        L.addSpacing(6)
        go = PopButton("Anmelden", "arrow", "blue", 50)
        go.icon_right = True
        L.addWidget(go)
        L.addSpacing(10)
        orrow = QHBoxLayout()
        for _ in range(2):
            pass
        ln1, ln2 = QFrame(), QFrame()
        for ln in (ln1, ln2):
            ln.setFixedHeight(1)
            ln.setStyleSheet("background: rgba(255,255,255,60);")
        orl = QLabel("ODER")
        orl.setFont(app_font(9))
        orl.setStyleSheet("color: rgba(255,255,255,170); letter-spacing: 1px;")
        orrow.addWidget(ln1, 1)
        orrow.addSpacing(12)
        orrow.addWidget(orl)
        orrow.addSpacing(12)
        orrow.addWidget(ln2, 1)
        L.addLayout(orrow)
        L.addSpacing(10)
        alt = PopButton("Mit Einladungscode registrieren", "ticket", "white", 48)
        L.addWidget(alt)
        L.addSpacing(12)
        bottom = QLabel()
        bottom.setAlignment(Qt.AlignCenter)
        bottom.setFont(app_font(9.5))
        bottom.setCursor(Qt.PointingHandCursor)
        L.addWidget(bottom)
        mode = {"new": bool(invite)}

        def apply_mode():
            new = mode["new"]
            inv.setVisible(new)
            title.setText("Konto erstellen" if new else "Willkommen zurück")
            sub.setText("Mit dem Code aus deinem Einladungslink" if new else "Melde dich an, um weiterzumachen")
            go.setText("Konto erstellen" if new else "Anmelden")
            alt.setText("Zurück zur Anmeldung" if new else "Mit Einladungscode registrieren")
            alt.icon_name = "back" if new else "ticket"
            forgot.setVisible(not new)
            bottom.setText(('<span style="color:rgba(255,255,255,190)">Schon ein Konto? </span>'
                            '<b style="color:#5b8cff">Anmelden</b>') if new else
                           ('<span style="color:rgba(255,255,255,190)">Noch kein Konto? </span>'
                            '<b style="color:#5b8cff">Einladung einlösen</b>'))
            card.fit()
            self.overlay._center()

        def toggle():
            mode["new"] = not mode["new"]
            err.setText("")
            apply_mode()
        alt.clicked.connect(toggle)
        bottom.mousePressEvent = lambda e: toggle()
        forgot.mousePressEvent = lambda e: err.setText("Frag den Admin – er kann dir einen neuen Zugang schicken.")
        apply_mode()

        def submit():
            u, p = user.text().strip(), pw.text()
            if not u or not p:
                err.setText("Bitte Benutzername und Passwort eingeben.")
                card.fit()
                return
            go.setEnabled(False)
            err.setText("")
            new = mode["new"]

            def work():
                try:
                    if new:
                        r = auth.call("POST", "/redeem", {"invite": inv.text().strip(), "username": u, "password": p})
                    else:
                        r = auth.call("POST", "/login", {"username": u, "password": p})
                    self.ui(self.on_login_ok, r["name"], r["role"], r["token"], False, True)
                except auth.ApiError as e:
                    self.ui(lambda m: (err.setText(m), go.setEnabled(True), card.fit(), self.overlay._center()), str(e))
            threading.Thread(target=work, daemon=True).start()
        go.clicked.connect(submit)
        for w_ in (inv, user, pw):
            w_.returnPressed.connect(submit)
        self.overlay.present(card, opaque=True)
        self.controls.raise_()

    def on_login_ok(self, name, role, token, offline, dismiss=False):
        self.st.update(acct_token=token, acct_role=role, acct_name=name)
        self.role = role
        self.pending_invite = ""
        if self.login_open or dismiss:
            self.login_open = False
            self.overlay.dismiss()
            self.gate_theme(False)
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
        self.acct_g, lay = panel("Konto", alpha=150)
        self.acct_lay = QVBoxLayout()
        self.acct_lay.setSpacing(8)
        lay.addLayout(self.acct_lay)
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
        pwb = PopButton("Passwort ändern", None, "glass", 40)
        pwb.clicked.connect(self.open_password)
        r.addWidget(pwb)
        out = PopButton("Abmelden", None, "glass", 40)
        out.clicked.connect(self.force_logout)
        r.addWidget(out)
        lay.addLayout(r)
        if self.role == "admin":
            inv = PopButton("Einladung für eine Person erstellen", "plus", "accent", 42)
            inv.clicked.connect(self.open_invite)
            lay.addWidget(inv)
            self.users_box = QVBoxLayout()
            self.users_box.setSpacing(6)
            lay.addLayout(self.users_box)
            self.load_users()

    def _clear_layout(self, l):
        while l.count():
            it = l.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._clear_layout(it.layout())

    def open_request_server(self):
        """Spieler: einen noch nicht eingerichteten Server beim Admin anfragen (mit optionalem Screenshot)."""
        if not core.ACCOUNT_URL or not self.st.get("acct_token"):
            self.say("Dafür musst du angemeldet sein", AMBER)
            return
        card = DialogCard("Server anfragen", "Der Admin bekommt deine Anfrage und richtet den Server ein.", 520)
        card.body.addWidget(label("Name des Servers", 10.5, True, MUT))
        nm = QLineEdit()
        self.style_edit(nm)
        card.body.addWidget(nm)
        card.body.addWidget(label("Wie sieht die Kill-Meldung aus? (optional)", 10.5, True, MUT))
        note = QLineEdit()
        note.setPlaceholderText("z. B. oben rechts: „Du hast … getötet“")
        self.style_edit(note)
        card.body.addWidget(note)
        st = {"img": None}
        pick = PopButton("Screenshot der Kill-Meldung anhängen …", "folder", "glass", 38)
        info = label("", 9, color=MUT)

        def do_pick():
            p, _ = QFileDialog.getOpenFileName(self, "Screenshot wählen", "", "Bilder (*.png *.jpg *.jpeg *.webp *.bmp)")
            if p:
                im = QImage(p)
                if not im.isNull():
                    im = im.scaled(1600, 900, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    from PySide6.QtCore import QBuffer, QByteArray
                    ba = QByteArray()
                    buf = QBuffer(ba)
                    buf.open(QBuffer.WriteOnly)
                    im.save(buf, "JPG", 80)
                    import base64
                    st["img"] = base64.b64encode(bytes(ba)).decode()
                    info.setText(f"Angehängt: {os.path.basename(p)}")
        pick.clicked.connect(do_pick)
        card.body.addWidget(pick)
        card.body.addWidget(info)
        cancel = PopButton("Abbrechen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        go = PopButton("Anfrage senden", "check", "accent", 40)
        card.btns.addWidget(cancel)
        card.btns.addWidget(go)
        tok = self.st.get("acct_token")

        def send():
            if not nm.text().strip():
                card.err.setText("Bitte den Namen des Servers eingeben.")
                return
            go.setEnabled(False)

            def work():
                try:
                    auth.call("POST", "/requests", {"name": nm.text().strip(), "note": note.text().strip(),
                                                    "image": st["img"]}, token=tok, timeout=40)
                    self.ui(lambda: (self.overlay.dismiss(), self.say("Anfrage gesendet – der Admin kümmert sich darum")))
                except auth.ApiError as e:
                    self.ui(lambda m: (card.err.setText(m), go.setEnabled(True)), str(e))
            threading.Thread(target=work, daemon=True).start()
        go.clicked.connect(send)
        self.overlay.present(card)

    def open_request(self, rq):
        """Admin: eine Server-Anfrage ansehen."""
        card = DialogCard(f"Anfrage: {rq.get('name', '?')}", f"von {rq.get('by', '?')}", 640)
        if rq.get("note"):
            card.body.addWidget(label(rq["note"], 10.5, wrap=True))
        tok = self.st.get("acct_token")
        img_lbl = QLabel("Screenshot wird geladen …" if rq.get("has_image") else "Kein Screenshot angehängt.")
        img_lbl.setProperty("role", "muted")
        card.body.addWidget(img_lbl)
        if rq.get("has_image"):
            def work():
                try:
                    r = auth.call("GET", f"/requests/image?id={rq.get('id')}", token=tok, timeout=40)
                    self.ui(show_img, r.get("image"))
                except auth.ApiError as e:
                    self.ui(img_lbl.setText, str(e))

            def show_img(b64):
                import base64
                pm = QPixmap()
                pm.loadFromData(base64.b64decode(b64 or ""))
                if pm.isNull():
                    img_lbl.setText("Screenshot konnte nicht geladen werden.")
                    return
                img_lbl.setPixmap(pm.scaled(576, 330, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                card.fit()
                self.overlay._center()
            threading.Thread(target=work, daemon=True).start()
        ok = PopButton("Schließen", None, "glass", 40)
        ok.clicked.connect(self.overlay.dismiss)
        card.btns.addWidget(ok)
        self.overlay.present(card)

    def open_password(self):
        card = DialogCard("Passwort ändern", "Gib dein altes und ein neues Passwort ein (mindestens 6 Zeichen).", 480)
        fields = []
        for t in ("Altes Passwort", "Neues Passwort", "Neues Passwort wiederholen"):
            card.body.addWidget(label(t, 10.5, True, MUT))
            e = QLineEdit()
            e.setEchoMode(QLineEdit.Password)
            self.style_edit(e)
            card.body.addWidget(e)
            fields.append(e)
        cancel = PopButton("Abbrechen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        go = PopButton("Speichern", "check", "accent", 40)
        card.btns.addWidget(cancel)
        card.btns.addWidget(go)
        tok = self.st.get("acct_token")

        def submit():
            old, new, rep = (f.text() for f in fields)
            if len(new) < 6:
                card.err.setText("Das neue Passwort braucht mindestens 6 Zeichen.")
                return
            if new != rep:
                card.err.setText("Die beiden neuen Passwörter sind nicht gleich.")
                return
            go.setEnabled(False)
            card.err.setText("")

            def work():
                try:
                    auth.call("POST", "/password", {"old": old, "password": new}, token=tok)
                    self.ui(lambda: (self.overlay.dismiss(), self.say("Passwort geändert")))
                except auth.ApiError as e:
                    self.ui(lambda m: (card.err.setText(m), go.setEnabled(True)), str(e))
            threading.Thread(target=work, daemon=True).start()
        go.clicked.connect(submit)
        fields[2].returnPressed.connect(submit)
        self.overlay.present(card)

    def open_invite(self):
        card = DialogCard("Einladung erstellen", "Für wen ist der Zugang? Du bekommst einen Download-Link, den du verschicken kannst.", 520)
        card.body.addWidget(label("Name der Person (nur für dich zur Übersicht)", 10.5, True, MUT))
        nm = QLineEdit()
        self.style_edit(nm)
        card.body.addWidget(nm)
        cancel = PopButton("Abbrechen", None, "glass", 40)
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
            QApplication.clipboard().setText(r["link"])
            c2 = DialogCard("Einladung erstellt", "Der Link ist kopiert. Schick ihn weiter, er ist 14 Tage gültig und "
                            "nur einmal benutzbar.", 520)
            link = QLineEdit(r["link"])
            self.style_edit(link)
            link.setReadOnly(True)
            c2.body.addWidget(link)
            cp = PopButton("Link kopieren", "check", "glass", 40)
            cp.clicked.connect(lambda: (QApplication.clipboard().setText(r["link"]), self.say("Link kopiert")))
            ok = PopButton("Fertig", None, "accent", 40)
            ok.clicked.connect(self.overlay.dismiss)
            c2.btns.addWidget(cp)
            c2.btns.addWidget(ok)
            self.overlay.present(c2)
            self.load_users()
        go.clicked.connect(make)
        nm.returnPressed.connect(make)
        self.overlay.present(card)

    def load_users(self):
        tok = self.st.get("acct_token")

        def work():
            try:
                u = auth.call("GET", "/users", token=tok)["users"]
            except auth.ApiError as e:
                self.ui(self.say, f"Konten konnten nicht geladen werden: {e}", RED)
                return
            res = {}
            for key, path in (("inv", "/invites"), ("req", "/requests")):
                try:
                    res[key] = auth.call("GET", path, token=tok)[{"inv": "invites", "req": "requests"}[key]]
                except auth.ApiError:
                    res[key] = None            # aelterer Konto-Server ohne diese Funktion
            self.ui(self.show_users, u, res["inv"] or [], res["req"])
        threading.Thread(target=work, daemon=True).start()

    def show_users(self, users, invites=(), requests=None):
        if not hasattr(self, "users_box") or self.users_box is None:
            return
        try:
            self._clear_layout(self.users_box)
        except RuntimeError:
            return
        hr = QHBoxLayout()
        hr.addStretch(1)
        rb = PopButton("Aktualisieren", "refresh", "glass", 30)
        rb.clicked.connect(self.load_users)
        hr.addWidget(rb)
        self.users_box.addLayout(hr)
        if requests is None:
            self.users_box.addWidget(label("Server-Anfragen: Bitte den Konto-Server (worker.js) aktualisieren.", 9, color=MUT))
        elif requests:
            self.users_box.addWidget(label(f"Server-Anfragen ({len(requests)})", 11, True))
            for rq in requests:
                row = QHBoxLayout()
                row.addWidget(label(f"{rq.get('name', '?')}  ·  von {rq.get('by', '?')}", 10, color=MUT), 1)
                b1 = PopButton("Ansehen", None, "glass", 32)
                b1.clicked.connect(lambda _=False, r=rq: self.open_request(r))
                b2 = PopButton("Erledigt", "check", "glass", 32)
                b2.clicked.connect(lambda _=False, c=rq.get("id"): self.user_action("/requests/delete", {"id": c}))
                row.addWidget(b1)
                row.addWidget(b2)
                self.users_box.addLayout(row)
        if invites:
            self.users_box.addWidget(label("Einladungen", 11, True))
            for i in invites:
                row = QHBoxLayout()
                who = i["label"] or "ohne Namen"
                state = f"angemeldet als {i['by']}" if i["used"] else "noch nicht angemeldet"
                row.addWidget(label(f"{who}  ·  {state}", 10.5, color=("ok" if i["used"] else MUT)), 1)
                if not i["used"]:
                    link = f"{core.ACCOUNT_URL.rstrip('/')}/d/{i['code']}"
                    b1 = PopButton("Link kopieren", None, "glass", 36)
                    b1.clicked.connect(lambda _=False, l=link: (QApplication.clipboard().setText(l), self.say("Link kopiert")))
                    row.addWidget(b1)
                b2 = PopButton("", "close", "flat", 36, icon_only=True)
                b2.clicked.connect(lambda _=False, c=i["code"]: self.user_action("/invites/delete", {"code": c}))
                row.addWidget(b2)
                self.users_box.addLayout(row)
        self.users_box.addWidget(label("Konten", 11, True))
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
    def startup(self):
        """Beim Start zuerst nach einem Update suchen. Gibt es eines, ist die App gesperrt, bis es installiert ist."""
        def work():
            try:
                res = check_update()
            except Exception:
                res = None
            if res:
                self.ui(self.show_forced_update, *res)
            else:
                self.ui(self.start_account)
        threading.Thread(target=work, daemon=True).start()

    def show_forced_update(self, version, url):
        self.update_url = url
        self.gate_theme(True)
        self.login_open = True                       # sperrt Esc und Schliessen des Fensters per Klick daneben
        card = DialogCard("Neues Update verfügbar", f"ACE v{version} ist da. Du kannst die App erst wieder "
                          "benutzen, wenn du das Update installiert hast. Es dauert nur einen Moment, danach "
                          "startet ACE von selbst neu.", 560)
        btn = PopButton("Jetzt aktualisieren", "upload", "accent", 48)
        card.btns.addWidget(btn)

        def go():
            btn.setEnabled(False)
            btn.setText("Lade herunter …")
            card.err.setText("")

            def work():
                try:
                    path = download_file(url, lambda p: self.ui(btn.setText, f"Lade … {int(p * 100)} %"))
                    self.ui(self.run_installer, path)
                except Exception as e:
                    self.ui(lambda m: (btn.setEnabled(True), btn.setText("Erneut versuchen"),
                                       card.err.setText(f"Download fehlgeschlagen: {m}")), str(e))
            threading.Thread(target=work, daemon=True).start()
        btn.clicked.connect(go)
        self.overlay.present(card, opaque=True)
        self.controls.raise_()                       # Schliessen/Minimieren bleibt erreichbar

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
        self.upd_dot.set_dot(True)
        self.say(f"Update v{version} verfügbar – unter Einstellungen installieren")
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
        a = self.video.area()
        return max(64, int((a.width() - 4) * dpr)), max(64, int((a.height() - 4) * dpr))

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
        self.close_btn.show()
        self.open_btn.set_kind("dark")
        self.clips, self.sel = [], None
        self.mark_in = self.mark_out = None
        self.rebuild_cards()
        self.scan_bar.set_value(0, False)
        self.set_status("Bereit – jetzt „Kills suchen“")
        self.timeline.set_view(0, 60)
        self.seek(0)

    def ask_unload(self):
        if not self.has_video:
            return
        if self.scanning:
            self.say("Die Suche läuft noch – bitte erst abbrechen", RED)
            return
        if not self.clips:
            self.unload_video()
            return
        n = len(self.clips)
        card = DialogCard("Video entfernen?", f"Die {n} {'Clip' if n == 1 else 'Clips'} in der Liste "
                          "gehen dabei verloren, wenn du sie noch nicht exportiert hast.", 480)
        cancel = PopButton("Abbrechen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        yes = PopButton("Entfernen", "trash", "danger", 40)
        yes.clicked.connect(lambda: (self.overlay.dismiss(), self.unload_video()))
        card.btns.addWidget(cancel)
        card.btns.addWidget(yes)
        self.overlay.present(card)

    def unload_video(self):
        """Video wieder entfernen: zurueck zum leeren Zustand, dann kann ein neues geoeffnet werden."""
        if self.fs:
            self.toggle_fullscreen()
        if self.playing:
            self.toggle_play()
        if self.player:
            self.player.stop()
        if self.audio:
            self.audio.stop()
            self.audio.setSource(QUrl())
        self.player = self.path = None
        self.has_video = False
        self.video.has_video = False
        self.video.img = None
        self.video.update()
        self.cta.show()
        self.fps, self.duration, self.pos = 60.0, 0.0, 0.0
        self.clips, self.sel, self.play_clip = [], None, None
        self.mark_in = self.mark_out = None
        self.file_lbl.setText("Noch kein Video")
        self.meta_lbl.setText("Wähle eine Aufnahme aus · OBS oder NVIDIA")
        self.open_btn.setText("Video öffnen")
        self.open_btn.set_kind("dark")
        self.close_btn.hide()
        self.bar_title.setText("Kein Video")
        self.bar_sub.setText("")
        self.bar_thumb.clear()
        self._thumb_pending = False
        self.scan_bar.set_value(0, False)
        self.set_status("Bereit")
        self.rebuild_cards()
        self.timeline.set_view(0, 60)
        self.timeline.update()
        self.seekbar.update()

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
        self.total_lbl.setText(fmt_short(total) if self.clips else "0:00")
        self.exp_all.setText(f"Alle exportieren ({len(self.clips)})" if self.clips else "Alle exportieren")
        self.update_unsaved()
        self.timeline.update()

    @staticmethod
    def _appear(row):
        try:
            tween(row, "appear", 1.0, 240)
        except RuntimeError:                              # Zeile wurde inzwischen ersetzt
            pass

    def update_unsaved(self):
        if hasattr(self, "unsaved"):
            self.unsaved.set_count(sum(1 for c in self.clips if not c.last_name))

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
        main = Glass(radius=32, alpha=0)
        main.ghost = True
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
        if self.fs and e.key() == Qt.Key_Escape and not self.overlay.isVisible():
            self.toggle_fullscreen()
            return
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
        elif k == Qt.Key_F:
            self.toggle_fullscreen()
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
    load_fonts(resource("assets", "fonts"))
    app.setFont(app_font(10))
    try:
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(resource("assets", "icon.ico")))
    except Exception:
        pass
    w = Window()
    for a in sys.argv[1:]:
        m = re.match(r"ace:(?://)?invite/([A-Za-z0-9]+)", a)
        if m:
            w.pending_invite = m.group(1)
    if sys.platform.startswith("win"):
        w.showMaximized()
    else:
        w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

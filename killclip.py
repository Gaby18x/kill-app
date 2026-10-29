"""Kill App - schneidet automatisch Kill-Clips aus GTA/FiveM-Aufnahmen (helle Glas-Oberflaeche, Qt)."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

import cv2
import numpy as np
from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

import core
from core import Player, check_update, download_file, export_clip, find_ffmpeg, resource, scan_video
from ui import (ACC, ACC2, ACC_TEXT, AMBER, DIM, MUT, RED, TXT, Background, Bar, ElidedLabel, FadeStack, Glass,
                Overlay, PopButton, SeekBar, Segmented, SliderRow, Timeline, Toast, Toggle, app_font, ink,
                paint_icon, tween, white, with_alpha)

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
AVATARS = [("#8c79ee", "#c9a6f8"), ("#f08fb8", "#f7b6a0"), ("#6fb7f2", "#8c9cf2"), ("#5fcfae", "#7fd0e8"),
           ("#f2b36f", "#f08f8f")]
ICON = QColor(110, 98, 176, 225)            # Symbolfarbe auf hellem Glas

LABEL_QSS = f"""
QLabel {{ color: {TXT}; background: transparent; }}
QLineEdit {{ background: rgba(255,255,255,170); border: 1px solid rgba(140,121,238,70); border-radius: 13px;
            padding: 9px 14px; color: {TXT}; selection-background-color: #8c79ee; selection-color: white; }}
QLineEdit:focus {{ border: 1px solid #8c79ee; background: rgba(255,255,255,225); }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollArea > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(42,40,72,50); border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(42,40,72,95); }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QSlider::groove:horizontal {{ height: 6px; background: rgba(42,40,72,36); border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: #8c79ee; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: white; border: 2px solid #8c79ee; width: 14px; height: 14px; margin: -7px 0;
                              border-radius: 9px; }}
QSlider::handle:horizontal:hover {{ width: 18px; height: 18px; margin: -9px 0; border-radius: 11px; }}
QToolTip {{ background: white; color: {TXT}; border: 1px solid rgba(140,121,238,90); padding: 4px 8px; }}
"""


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


class Bridge(QObject):
    """Fuehrt Funktionen aus Hintergrund-Threads sicher im Hauptthread aus."""
    call = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn, args: fn(*args))


def label(text="", size=10.5, bold=False, color=None, wrap=False):
    lb = QLabel(text)
    lb.setFont(app_font(size, bold))
    if color:
        lb.setStyleSheet(f"color: {color};")
    lb.setWordWrap(wrap)
    return lb


def paint_avatar(p, rect, name, radius=None, letter_size=14):
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
    M, W, H = 10, 300, 168

    def __init__(self, name="", sub="", tag="", add=False, parent=None):
        super().__init__(parent)
        self.name, self.sub, self.tag, self.add = name, sub, tag, add
        self.pop = 0.0
        self.ehov = 0.0
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.W + 2 * self.M, self.H + 2 * self.M)

    def _edit_rect(self):
        return QRectF(self.M + self.W - 46, self.M + 14, 32, 32)

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 200, QEasingCurve.OutBack)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 180)
        tween(self, "ehov", 0.0, 120)

    def mouseMoveEvent(self, e):
        if not self.add:
            tween(self, "ehov", 1.0 if self._edit_rect().contains(e.position()) else 0.0, 110)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if not self.add and self._edit_rect().contains(e.position()):
            self.edit.emit()
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
        p.scale(1 + 0.04 * self.pop, 1 + 0.04 * self.pop)
        p.translate(-c)
        if hv > 0:
            for grow, a in ((14, 16), (8, 26)):
                p.setPen(Qt.NoPen)
                p.setBrush(with_alpha(ACC, a * hv))
                p.drawRoundedRect(r.adjusted(-grow * hv, -grow * hv, grow * hv, grow * hv), 24 + grow * hv,
                                  24 + grow * hv)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, white(210 + 30 * hv))
        g.setColorAt(1, white(130 + 60 * hv))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(r, 24, 24)
        bp = QPen(white(240), 1.2)
        if self.add:
            bp = QPen(with_alpha(ACC, 150 + 90 * hv), 1.4)
            bp.setStyle(Qt.DashLine)
        p.setBrush(Qt.NoBrush)
        p.setPen(bp)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 24, 24)
        if self.add:
            d = 48
            cr = QRectF(c.x() - d / 2, c.y() - 36, d, d)
            p.setPen(Qt.NoPen)
            p.setBrush(with_alpha(ACC, 40))
            p.drawEllipse(cr)
            paint_icon(p, "plus", cr.adjusted(12, 12, -12, -12), ACC, 2.2)
            p.setPen(ink(235))
            p.setFont(app_font(11.5, True))
            p.drawText(QRectF(r.left(), c.y() + 18, r.width(), 28), Qt.AlignCenter, "Neuer Server")
            p.setPen(ink(130))
            p.setFont(app_font(9.5))
            p.drawText(QRectF(r.left(), c.y() + 42, r.width(), 22), Qt.AlignCenter, "Eigene Kill-Erkennung anlegen")
            return
        paint_avatar(p, QRectF(r.left() + 22, r.top() + 22, 54, 54), self.name, 18, 20)
        p.setPen(ink(240))
        p.setFont(app_font(15, True))
        fm = QFontMetrics(p.font())
        p.drawText(QRectF(r.left() + 90, r.top() + 24, r.width() - 150, 26), Qt.AlignVCenter,
                   fm.elidedText(self.name, Qt.ElideRight, int(r.width() - 150)))
        p.setPen(ink(130))
        p.setFont(app_font(9.5))
        p.drawText(QRectF(r.left() + 90, r.top() + 50, r.width() - 110, 40),
                   int(Qt.AlignTop.value) | int(Qt.TextWordWrap.value), self.sub)
        er = self._edit_rect()
        p.setPen(Qt.NoPen)
        p.setBrush(white(140 + 90 * self.ehov))
        p.drawEllipse(er)
        paint_icon(p, "edit", er.adjusted(8, 8, -8, -8), ICON)
        p.setPen(QColor(ACC_TEXT))
        p.setFont(app_font(10.5, True))
        p.drawText(QRectF(r.left() + 22, r.bottom() - 46, 120, 26), Qt.AlignVCenter, "Auswählen")
        paint_icon(p, "chevron", QRectF(r.left() + 100, r.bottom() - 43, 18, 18), QColor(ACC_TEXT))
        if self.tag:
            fm = QFontMetrics(app_font(9, True))
            tw = fm.horizontalAdvance(self.tag) + 20
            tr = QRectF(r.right() - 18 - tw, r.bottom() - 46, tw, 24)
            p.setPen(Qt.NoPen)
            p.setBrush(with_alpha(ACC, 40))
            p.drawRoundedRect(tr, 12, 12)
            p.setPen(QColor(ACC_TEXT))
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
        s = 1 + 0.03 * self.pop
        c = r.center()
        p.translate(c)
        p.scale(s, s)
        p.translate(-c)
        a = self.sel
        p.setPen(Qt.NoPen)
        p.setBrush(white(120 * self.pop * (1 - a) + 235 * a))
        p.drawRoundedRect(r, 16, 16)
        if a > 0.02:
            p.setPen(QPen(with_alpha(ACC, 55 * a), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 16, 16)
        paint_icon(p, self.icon, QRectF(r.left() + 16, r.center().y() - 10, 20, 20), ACC if a > 0.5 else ICON, 1.8)
        p.setPen(ink(235 if a > 0.5 else 175))
        p.setFont(app_font(10.5, a > 0.5))
        p.drawText(QRectF(r.left() + 50, r.top(), r.width() - 56, r.height()), Qt.AlignVCenter, self.text)


class SideServer(QWidget):
    """Server-Eintrag in der Seitenleiste (wie eine Playlist)."""
    clicked = Signal()

    def __init__(self, name, active=False, parent=None):
        super().__init__(parent)
        self.name = name
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
        p.scale(1 + 0.025 * self.pop, 1 + 0.025 * self.pop)
        p.translate(-c)
        p.setPen(Qt.NoPen)
        p.setBrush(white(235 if self.active else 120 * self.pop))
        p.drawRoundedRect(r, 14, 14)
        paint_avatar(p, QRectF(r.left() + 10, r.center().y() - 11, 22, 22), self.name, 8, 9)
        p.setPen(ink(235 if self.active else 175))
        p.setFont(app_font(10, self.active))
        fm = QFontMetrics(p.font())
        p.drawText(QRectF(r.left() + 44, r.top(), r.width() - 50, r.height()), Qt.AlignVCenter,
                   fm.elidedText(self.name, Qt.ElideRight, int(r.width() - 54)))


class ClipRow(QWidget):
    """Eintrag in der Clip-Liste (wie ein Titel in einer Playlist)."""
    clicked = Signal()
    delete = Signal()

    def __init__(self, index, clip, parent=None):
        super().__init__(parent)
        self.i, self.c = index, clip
        self.selected = False
        self.hov = 0.0
        self.xhov = 0.0
        self.setFixedHeight(66)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def _xr(self):
        return QRectF(self.width() - 40, (self.height() - 28) / 2, 28, 28)

    def enterEvent(self, e):
        tween(self, "hov", 1.0, 130)

    def leaveEvent(self, e):
        tween(self, "hov", 0.0, 130)
        tween(self, "xhov", 0.0, 100)

    def mouseMoveEvent(self, e):
        tween(self, "xhov", 1.0 if self._xr().contains(e.position()) else 0.0, 100)

    def mousePressEvent(self, e):
        if self._xr().contains(e.position()):
            self.delete.emit()
        else:
            self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(2, 3, -2, -3)
        p.setPen(QPen(with_alpha(ACC, 90), 1.2) if self.selected else Qt.NoPen)
        p.setBrush(white(240) if self.selected else white(130 * self.hov))
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
        p.setPen(ink(240))
        p.setFont(app_font(10.5, True))
        p.drawText(QRectF(t.right() + 14, r.top() + 11, 150, 20), Qt.AlignVCenter, f"KILL {self.i + 1:02d}")
        c = self.c
        p.setPen(ink(125))
        p.setFont(app_font(9))
        p.drawText(QRectF(t.right() + 14, r.top() + 32, 160, 18), Qt.AlignVCenter,
                   f"{fmt_short(c.start)} – {fmt_short(c.end)}")
        p.setPen(ink(125))
        p.setFont(app_font(9.5))
        p.drawText(QRectF(r.right() - 104, r.top(), 56, r.height()), Qt.AlignVCenter | Qt.AlignRight,
                   f"{c.end - c.start:.1f} s")
        xr = self._xr()
        p.setPen(Qt.NoPen)
        p.setBrush(with_alpha(RED, 55 * self.xhov))
        p.drawEllipse(xr)
        paint_icon(p, "close", xr.adjusted(9, 9, -9, -9), QColor(110, 98, 176, int(120 + 110 * self.xhov)), 1.7)


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
        g.setColorAt(0, QColor(58, 50, 104, 235))
        g.setColorAt(1, QColor(30, 26, 64, 240))
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
        super().__init__(None, radius=30, alpha=215)
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
        self.err = label("", 10, color="#d6334f", wrap=True)
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
        self.colors = [ACC, QColor("#2f8fe8"), AMBER]

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

    def set_server(self, name):
        self.name = name
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
        p.scale(1 + 0.03 * self.pop, 1 + 0.03 * self.pop)
        p.translate(-c)
        p.setPen(QPen(white(240), 1.1))
        p.setBrush(white(170 + 50 * self.pop))
        p.drawRoundedRect(r, 22, 22)
        if self.name:
            paint_avatar(p, QRectF(r.left() + 8, r.center().y() - 15, 30, 30), self.name, 15, 12)
            p.setPen(ink(235))
            p.setFont(app_font(10.5, True))
            p.drawText(QRectF(r.left() + 48, r.top(), r.width() - 56, r.height()), Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(self.name, Qt.ElideRight, int(r.width() - 60)))
        else:
            p.setPen(ink(130))
            p.setFont(app_font(10))
            p.drawText(r, Qt.AlignCenter, "Kein Server")


# ------------------------------------------------------------------ Hauptfenster
class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kill App")
        self.setMinimumSize(1280, 800)
        self.resize(1440, 880)
        self.bridge = Bridge()
        self.st = self.load_settings()
        self.ffmpeg = find_ffmpeg()
        self.server = None
        self.path = None
        self.player = None
        self.fps, self.duration, self.pos = 60.0, 0.0, 0.0
        self.has_video = False
        self.clips, self.sel = [], None
        self.cancel = threading.Event()
        self.scanning = False
        self.playing = False
        self.play_clip = None
        self.speed = 1.0
        self.update_url = None
        self._tick = 0
        self._last_tl = 0.0
        self._thumb_pending = False
        self.tags = {}
        self.rows = []

        self.bg = Background()
        self.setCentralWidget(self.bg)
        self.bg.setStyleSheet(LABEL_QSS)
        root = QHBoxLayout(self.bg)
        root.setContentsMargins(22, 22, 22, 22)
        self.frame = Glass(radius=38, alpha=36)
        root.addWidget(self.frame)
        fl = QHBoxLayout(self.frame)
        fl.setContentsMargins(16, 16, 16, 16)
        fl.setSpacing(14)
        self.sidebar = self.build_sidebar()
        fl.addWidget(self.sidebar)
        self.stack = FadeStack()
        fl.addWidget(self.stack, 1)
        self.page_server = self.build_server_page()
        self.page_editor = self.build_editor_page()
        self.page_settings = self.build_settings_page()
        for pg in (self.page_server, self.page_editor, self.page_settings):
            self.stack.add(pg)
        self.overlay = Overlay(self.bg)
        self.toast = Toast(self.bg)
        self.go_page(0, animate=False)
        self.render_servers()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_player)
        self.timer.start(8)
        QTimer.singleShot(250, self.check_ffmpeg)
        QTimer.singleShot(500, self.stack.warm)
        if self.st["autoupd"]:
            QTimer.singleShot(1500, lambda: self.start_update_check(False))

    # ------------------------------------------------------------ Infrastruktur
    def ui(self, fn, *args):
        self.bridge.call.emit(fn, args)

    def load_settings(self):
        d = {"pre": 4.0, "post": 2.0, "sens": 6, "mode": "Verlustfrei", "open": True, "loop": True,
             "autoupd": True, "gpu": True, "servers": [], "names": {}, "last_server": "bruder"}
        try:
            with open(SETTINGS) as f:
                d.update(json.load(f))
        except Exception:
            pass
        if not isinstance(d.get("servers"), list):
            d["servers"] = []
        if not isinstance(d.get("names"), dict):
            d["names"] = {}
        return d

    def save_settings(self):
        self.st.update(pre=self.s_pre.value(), post=self.s_post.value(), sens=int(round(self.s_sens.value())),
                       mode=self.seg_mode.current(), open=self.tg_open.isChecked(), loop=self.tg_loop.isChecked(),
                       autoupd=self.tg_upd.isChecked(), gpu=self.tg_gpu.isChecked())
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
        super().closeEvent(e)

    def check_ffmpeg(self):
        if not self.ffmpeg:
            self.toast.show_text("FFmpeg nicht gefunden – Export nicht möglich", RED)

    def say(self, text, color=ACC):
        self.toast.show_text(text, color)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "overlay"):
            self.overlay.setGeometry(self.bg.rect())
            QTimer.singleShot(150, self._maybe_reflow)

    def _maybe_reflow(self):
        if self.srv_cols() != getattr(self, "_cols", None):
            self._cols = self.srv_cols()
            self.render_servers()

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
        lay.addWidget(self.caption("MENÜ"))
        self.nav = []
        for i, (ic, tx) in enumerate((("server", "Server"), ("film", "Editor"), ("gear", "Einstellungen"))):
            n = NavItem(ic, tx)
            n.clicked.connect(lambda i=i: self.nav_clicked(i))
            lay.addWidget(n)
            self.nav.append(n)
        self.nav[1].set_nav_enabled(False)
        lay.addSpacing(18)
        lay.addWidget(self.caption("MEINE SERVER"))
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
        lb = label(text, 8.5, True, DIM)
        lb.setContentsMargins(10, 4, 0, 4)
        return lb

    def render_side_servers(self):
        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(0)
        for s in self.all_servers():
            it = SideServer(s["name"], bool(self.server and self.server["id"] == s["id"]))
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
            card = ServerCard(s["name"], KIND_TEXT.get(s["kind"], ""), tag)
            card.clicked.connect(lambda s=s: self.enter_server(s))
            card.edit.connect(lambda s=s: self.open_edit_server(s))
            grid.addWidget(card, i // cols, i % cols)
            self.tags[s["id"]] = card
        add = ServerCard(add=True)
        add.clicked.connect(lambda: self.open_add_server())
        grid.addWidget(add, len(items) // cols, len(items) % cols)
        grid.setColumnStretch(cols, 1)
        grid.setRowStretch(len(items) // cols + 1, 1)
        self.srv_scroll.setWidget(host)
        self.srv_host = host
        self.render_side_servers()

    def enter_server(self, s):
        self.server = s
        self.st["last_server"] = s["id"]
        self.ed_chip.set_server(s["name"])
        self.nav[1].set_nav_enabled(True)
        self.save_settings()
        self.render_servers()
        self.go_page(1)

    # ------------------------------------------------------------ Server hinzufuegen / bearbeiten
    def style_edit(self, le):
        le.setFont(app_font(11))
        le.setMinimumHeight(44)

    def open_add_server(self, state=None):
        st = state or {"name": "", "kind": "Roter Kill-X", "result": None}
        card = DialogCard("Neuer Server", "Lege fest, woran Kill App auf diesem Server einen Kill erkennt.")
        card.body.addWidget(label("NAME", 8.5, True, DIM))
        name = QLineEdit(st["name"])
        name.setPlaceholderText("z. B. Mein Server")
        self.style_edit(name)
        card.body.addWidget(name)
        card.body.addWidget(label("ERKENNUNG", 8.5, True, DIM))
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
            self.st["servers"].append(srv)
            self.save_settings()
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
        card.body.addWidget(label("NAME", 8.5, True, DIM))
        name = QLineEdit(s["name"])
        self.style_edit(name)
        card.body.addWidget(name)
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
        elif builtin:
            card.body.addWidget(label("Dieser Server ist fest eingebaut. Du kannst ihn umbenennen, aber nicht löschen.",
                                      10, color=MUT, wrap=True))

        def save():
            nm = name.text().strip()
            if not nm:
                card.err.setText("Bitte einen Namen eingeben.")
                return
            others = [x["name"].lower() for x in self.all_servers() if x["id"] != s["id"]]
            if nm.lower() in others:
                card.err.setText("Diesen Namen gibt es schon.")
                return
            self.rename_server(s, nm)
            self.overlay.dismiss()
        if not builtin:
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
            self.ed_chip.set_server(nm)
        self.save_settings()
        self.render_servers()
        self.say("Gespeichert")

    def open_confirm_delete(self, s):
        card = DialogCard("Server löschen?", f"„{s['name']}“ und seine Einstellungen werden entfernt.", 460)
        cancel = PopButton("Abbrechen", None, "glass", 40)
        cancel.clicked.connect(self.overlay.dismiss)
        yes = PopButton("Löschen", "trash", "danger", 40)
        yes.clicked.connect(lambda: self.delete_server(s))
        card.btns.addWidget(cancel)
        card.btns.addWidget(yes)
        self.overlay.present(card)

    def delete_server(self, s):
        self.st["servers"] = [x for x in self.st["servers"] if x["id"] != s["id"]]
        shutil.rmtree(os.path.join(core.DATA_DIR, "servers", s["id"]), ignore_errors=True)
        if self.st.get("last_server") == s["id"]:
            self.st["last_server"] = "bruder"
        if self.server and self.server["id"] == s["id"]:
            self.server = None
            self.nav[1].set_nav_enabled(False)
            self.ed_chip.set_server("")
        self.save_settings()
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
        ic.setPixmap(icon_pixmap("film", ICON, 20))
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
        self.count_lbl = label("0", 9.5, True, ACC_TEXT)
        self.count_lbl.setStyleSheet(f"background: rgba(140,121,238,50); color: {ACC_TEXT}; border-radius: 10px; "
                                     "padding: 2px 10px;")
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
        ib.setStyleSheet("background: rgba(255,255,255,40); border-radius: 42px;")
        cl.addWidget(ib, 0, Qt.AlignHCenter)
        cl.addSpacing(10)
        t1 = label("Los geht’s", 18, True)
        t1.setStyleSheet("color: white;")
        t2 = label("Wähle deine Aufnahme aus.", 10.5)
        t2.setStyleSheet("color: rgba(255,255,255,190);")
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

        tlc = Glass(radius=22, alpha=95)
        tv = QVBoxLayout(tlc)
        tv.setContentsMargins(10, 8, 14, 8)
        tv.setSpacing(2)
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

        g, lay = panel("Kills suchen", "Findet automatisch jeden Kill im Video")
        self.s_pre = SliderRow("Vor dem Kill", 0, 10, 20, self.st["pre"], lambda v: f"{v:.1f} s")
        self.s_post = SliderRow("Nach dem Kill", 0, 10, 20, self.st["post"], lambda v: f"{v:.1f} s")
        names = ["", "sehr niedrig", "niedrig", "niedrig", "mittel", "mittel", "mittel+", "hoch", "hoch", "sehr hoch",
                 "max"]
        self.s_sens = SliderRow("Empfindlichkeit", 1, 10, 9, self.st["sens"], lambda v: names[int(round(v))])
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

        g, lay = panel("Export", "Clips als einzelne Videodateien speichern")
        self.seg_mode = Segmented(list(MODES), height=36)
        self.seg_mode.set_current(self.st["mode"] if self.st["mode"] in MODES else "Verlustfrei")
        self.mode_desc = ElidedLabel("", 9.5, False, DIM)
        self.seg_mode.changed.connect(lambda v: self.mode_desc.setText(MODES[v][1]))
        self.mode_desc.setText(MODES[self.seg_mode.current()][1])
        lay.addWidget(self.seg_mode)
        lay.addWidget(self.mode_desc)
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
        self.bar_thumb.setStyleSheet("background: rgba(42,40,72,40); border-radius: 12px;")
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
        if pl.ended and self.playing:
            self.playing = False
            self.play_btn.icon_name = "play"
            self.play_btn.update()

    def draw_frame(self, kind, pos, rgb):
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
        self.timeline.update()
        self.seekbar.update()

    def set_speed(self, v):
        self.speed = float(v.replace("×", ""))
        if self.player:
            self.player.set_play(self.playing, self.speed)

    def toggle_play(self):
        if not self.player:
            return
        self.playing = not self.playing
        self.play_btn.icon_name = "pause" if self.playing else "play"
        self.play_btn.update()
        if self.playing:
            self.play_clip = next((c for c in self.clips if c.start <= self.pos <= c.end), None)
            if self.play_clip and self.pos >= self.play_clip.end - 0.05:
                self.seek(self.play_clip.start)
        self.player.set_play(self.playing, self.speed)

    # ------------------------------------------------------------ Kill-Suche
    def set_status(self, text, state="idle"):
        col = {"ok": "#1f9d73", "bad": "#d6334f", "warn": "#c4801f", "busy": "#4a7be0", "idle": MUT}[state]
        self.status.set_color(col)
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
        args = (self.path, self.s_pre.value(), self.s_post.value(), self.server, int(round(self.s_sens.value())))
        gpu = self.tg_gpu.isChecked()

        def work():
            try:
                res = scan_video(*args, gpu=gpu, progress=lambda p: self.ui(self.scan_bar.set_value, p),
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
        self.set_status("Fehler: " + msg[:140], "bad")
        self.say("Suche fehlgeschlagen", RED)

    def scan_done(self, res, info):
        cancelled = self.cancel.is_set()
        self.scan_reset()
        self.scan_bar.set_value(1.0)
        self.clips = [Clip(a, b, k, bgr_to_pixmap(th) if th is not None else None) for a, b, k, th in res]
        self.sel = None
        self.rebuild_cards()
        n = len(self.clips)
        word = "Kill" if n == 1 else "Kills"
        extra = f" · {info['notices']} Meldungen geprüft" if self.server["kind"] != "xhit" else ""
        self.set_status(("Abgebrochen · " if cancelled else "Fertig · ") + f"{n} {word}{extra}", "ok" if n else "warn")
        self.say(f"{n} {word} gefunden" if n else "Keine Kills gefunden – Empfindlichkeit erhöhen?",
                 ACC if n else AMBER)
        if n:
            self.select(0)

    # ------------------------------------------------------------ Clips
    def rebuild_cards(self):
        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(2)
        self.rows = []
        if not self.clips:
            l = label("Noch keine Clips.\nStarte rechts die Kill-Suche.", 10.5, color=DIM)
            l.setAlignment(Qt.AlignCenter)
            v.addSpacing(40)
            v.addWidget(l)
        for i, c in enumerate(self.clips):
            row = ClipRow(i, c)
            row.selected = i == self.sel
            row.clicked.connect(lambda i=i: self.select(i))
            row.delete.connect(lambda i=i: self.delete(i))
            v.addWidget(row)
            self.rows.append(row)
        v.addStretch(1)
        self.clip_scroll.setWidget(host)
        self.count_lbl.setText(str(len(self.clips)))
        total = sum(c.end - c.start for c in self.clips)
        self.total_lbl.setText(f"Gesamt {fmt_short(total)}" if self.clips else "")
        self.exp_all.setText(f"Alle exportieren ({len(self.clips)})" if self.clips else "Alle exportieren")
        self.timeline.update()

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

    def add_clip(self):
        if not self.player:
            return
        c = Clip(max(0, self.pos - self.s_pre.value()), min(self.duration, self.pos + self.s_post.value()), self.pos)
        arr = self.video.arr
        if arr is not None:
            small = cv2.resize(arr, (256, int(256 * arr.shape[0] / arr.shape[1])), interpolation=cv2.INTER_AREA)
            h, w = small.shape[:2]
            c.thumb = QPixmap.fromImage(QImage(small.data, w, h, 3 * w, QImage.Format_RGB888).copy())
        self.clips.append(c)
        self.clips.sort(key=lambda x: x.start)
        idx = self.clips.index(c)
        self.rebuild_cards()
        self.select(idx, seek=False)
        self.say("Clip hinzugefügt")

    def set_edge(self, which):
        if self.sel is None:
            self.say("Wähle zuerst einen Clip aus", RED)
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
        out = QFileDialog.getExistingDirectory(self, "Zielordner wählen", self.st.get("outdir") or "")
        if not out:
            return
        self.st["outdir"] = out
        mode = MODES[self.seg_mode.current()][0]
        base, ext = os.path.splitext(os.path.basename(self.path))
        jobs = [(self.clips[i].start, self.clips[i].end, os.path.join(out, f"{base}_kill{i + 1:02d}{ext}"))
                for i in idx]
        self.exp_bar.set_value(0, False)
        self.exp_all.setEnabled(False)

        def work():
            try:
                for n, (a, b, dst) in enumerate(jobs, 1):
                    self.ui(self.set_status, f"Export {n}/{len(jobs)} …", "busy")
                    export_clip(self.ffmpeg, self.path, a, b, dst, mode)
                    self.ui(self.exp_bar.set_value, n / len(jobs))
                self.ui(self.export_done, out, len(jobs))
            except Exception as e:
                self.ui(self.export_failed, str(e))
        threading.Thread(target=work, daemon=True).start()

    def export_failed(self, msg):
        self.exp_all.setEnabled(True)
        self.set_status("Export fehlgeschlagen", "bad")
        self.say("Export-Fehler: " + msg[:90], RED)

    def export_done(self, out, n):
        self.exp_all.setEnabled(True)
        self.set_status(f"{n} Clips exportiert", "ok")
        self.say(f"{n} Clips exportiert")
        if self.tg_open.isChecked():
            try:
                if sys.platform.startswith("win"):
                    os.startfile(out)
                else:
                    subprocess.Popen(["xdg-open", out])
            except Exception:
                pass

    # ------------------------------------------------------------ Tasten
    def keyPressEvent(self, e):
        if self.overlay.isVisible():
            if e.key() == Qt.Key_Escape:
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
    if sys.platform.startswith("win"):
        w.showMaximized()
    else:
        w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

"""Kill App - Oberflaechen-Bausteine im Glas-Design (Qt / PySide6)."""
import math

from PySide6.QtCore import (QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QPolygonF, QRadialGradient)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QSizePolicy, QSlider, QWidget)

FONT_FAMILIES = ["Segoe UI Variable Display", "Segoe UI", "Inter", "Helvetica Neue", "Arial", "DejaVu Sans"]
ACC = QColor("#3ee0b4")
ACC2 = QColor("#42b8f0")
RED = QColor("#ff5d73")
AMBER = QColor("#ffc15a")
DARK_ON_ACC = QColor("#04231b")


def white(a):
    return QColor(255, 255, 255, int(max(0, min(255, a))))


def with_alpha(c, a):
    c = QColor(c)
    c.setAlpha(int(max(0, min(255, a))))
    return c


def app_font(size=10.0, bold=False):
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


# ------------------------------------------------------------------ Animation
def tween(w, name, end, ms=160, curve=QEasingCurve.OutCubic):
    """Animiert das Float-Attribut `name` von Widget `w` weich auf `end` (und zeichnet dabei neu)."""
    d = w.__dict__.setdefault("_tw", {})
    a = d.get(name)
    if a is None:
        a = QVariantAnimation(w)
        a.valueChanged.connect(lambda v, n=name, ww=w: (setattr(ww, n, float(v)), ww.update()))
        d[name] = a
    a.stop()
    a.setDuration(ms)
    a.setEasingCurve(curve)
    a.setStartValue(float(getattr(w, name)))
    a.setEndValue(float(end))
    a.start()


# ------------------------------------------------------------------ Symbole
def paint_icon(p, name, rect, color, lw=1.9):
    """Zeichnet ein einfaches Linien-Symbol (24x24-Raster) in `rect`."""
    p.save()
    p.translate(rect.topLeft())
    p.scale(rect.width() / 24.0, rect.height() / 24.0)
    pen = QPen(color, lw)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    P = QPointF

    def poly(pts, fill=False, close=False):
        pg = QPolygonF([P(x, y) for x, y in pts])
        if fill:
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawPolygon(pg)
            p.setBrush(Qt.NoBrush)
            p.setPen(pen)
        elif close:
            p.drawPolygon(pg)
        else:
            p.drawPolyline(pg)

    if name == "play":
        poly([(8, 5), (19, 12), (8, 19)], fill=True)
    elif name == "pause":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(6.5, 5, 3.6, 14), 1.4, 1.4)
        p.drawRoundedRect(QRectF(13.9, 5, 3.6, 14), 1.4, 1.4)
    elif name == "prev":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(5.5, 5, 2.6, 14), 1.2, 1.2)
        poly([(19, 5.5), (9.5, 12), (19, 18.5)], fill=True)
    elif name == "next":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(15.9, 5, 2.6, 14), 1.2, 1.2)
        poly([(5, 5.5), (14.5, 12), (5, 18.5)], fill=True)
    elif name == "plus":
        p.drawLine(P(12, 5), P(12, 19))
        p.drawLine(P(5, 12), P(19, 12))
    elif name == "close":
        p.drawLine(P(6, 6), P(18, 18))
        p.drawLine(P(18, 6), P(6, 18))
    elif name == "check":
        poly([(5, 12.5), (10, 17.5), (19, 7)])
    elif name == "edit":
        poly([(4, 20), (4, 16), (15, 5), (19, 9), (8, 20)], close=True)
        p.drawLine(P(12.5, 7.5), P(16.5, 11.5))
    elif name == "trash":
        p.drawLine(P(5, 7), P(19, 7))
        poly([(9, 7), (9, 4.5), (15, 4.5), (15, 7)])
        poly([(7, 7), (8, 20), (16, 20), (17, 7)])
    elif name == "gear":
        p.drawEllipse(P(12, 12), 3.2, 3.2)
        for i in range(8):
            a = math.radians(i * 45)
            p.drawLine(P(12 + 6.6 * math.cos(a), 12 + 6.6 * math.sin(a)),
                       P(12 + 9 * math.cos(a), 12 + 9 * math.sin(a)))
        p.drawEllipse(P(12, 12), 6.6, 6.6)
    elif name == "server":
        for y in (5, 10, 15):
            p.drawRoundedRect(QRectF(4, y, 16, 4.2), 2, 2)
    elif name == "film":
        p.drawRoundedRect(QRectF(3.5, 5.5, 17, 13), 3, 3)
        poly([(10.5, 9), (15, 12), (10.5, 15)], fill=True)
    elif name == "crosshair":
        p.drawEllipse(P(12, 12), 6.5, 6.5)
        for a, b in (((12, 2.5), (12, 6)), ((12, 18), (12, 21.5)), ((2.5, 12), (6, 12)), ((18, 12), (21.5, 12))):
            p.drawLine(P(*a), P(*b))
        p.setBrush(color)
        p.drawEllipse(P(12, 12), 1.3, 1.3)
    elif name == "folder":
        poly([(3.5, 7), (9, 7), (11, 9.5), (20.5, 9.5), (20.5, 18.5), (3.5, 18.5)], close=True)
    elif name == "upload":
        p.drawLine(P(12, 16), P(12, 4.5))
        poly([(7.5, 9), (12, 4.5), (16.5, 9)])
        poly([(4.5, 14), (4.5, 19.5), (19.5, 19.5), (19.5, 14)])
    elif name == "chevron":
        poly([(9.5, 5.5), (16, 12), (9.5, 18.5)])
    elif name == "back":
        poly([(14.5, 5.5), (8, 12), (14.5, 18.5)])
    elif name == "more":
        p.setBrush(color)
        p.setPen(Qt.NoPen)
        for x in (6, 12, 18):
            p.drawEllipse(P(x, 12), 1.7, 1.7)
    elif name == "refresh":
        p.drawArc(QRectF(5, 5, 14, 14), 40 * 16, 270 * 16)
        poly([(17.5, 3.5), (18.5, 8.5), (13.5, 8)])
    p.restore()


class ElidedLabel(QLabel):
    """Einzeiliges Label, das zu langen Text mit … abkuerzt (statt das Layout aufzublaehen)."""

    def __init__(self, text="", size=10.5, bold=False, color="rgba(255,255,255,235)", parent=None):
        super().__init__(parent)
        self._text = text
        self._color = color
        self.setFont(app_font(size, bold))
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, t):
        self._text = t
        self.updateGeometry()
        self.update()

    def text(self):
        return self._text

    def set_color(self, c):
        self._color = c
        self.update()

    def sizeHint(self):
        return QSize(0, QFontMetrics(self.font()).height() + 2)

    def minimumSizeHint(self):
        return QSize(0, QFontMetrics(self.font()).height() + 2)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        c = self._color
        if isinstance(c, str):
            if c.startswith("rgba"):
                r, g, b, a = [float(x) for x in c[5:-1].split(",")]
                c = QColor(int(r), int(g), int(b), int(a))
            else:
                c = QColor(c)
        p.setPen(c)
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(self.font()).elidedText(self._text, Qt.ElideRight, self.width()))


# ------------------------------------------------------------------ Hintergrund
class Background(QWidget):
    """Dunkler Farbverlauf mit weichen Farbflecken (wird einmal gerendert und zwischengespeichert)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm = None
        self._size = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._rebuild)

    @staticmethod
    def render(w, h):
        pm = QPixmap(max(1, w), max(1, h))
        p = QPainter(pm)
        lg = QLinearGradient(0, 0, w, h)
        lg.setColorAt(0.0, QColor("#090e2b"))
        lg.setColorAt(0.55, QColor("#170f45"))
        lg.setColorAt(1.0, QColor("#08233b"))
        p.fillRect(0, 0, w, h, lg)
        m = max(w, h)
        for cx, cy, r, c, a in ((0.08, 0.95, 0.55, "#17e0c0", 95), (0.92, 0.05, 0.60, "#8a5cff", 115),
                                (0.78, 0.98, 0.42, "#ff5fa2", 55), (0.30, 0.22, 0.38, "#3a7bff", 60)):
            rg = QRadialGradient(cx * w, cy * h, r * m)
            rg.setColorAt(0.0, with_alpha(QColor(c), a))
            rg.setColorAt(1.0, with_alpha(QColor(c), 0))
            p.fillRect(0, 0, w, h, rg)
        p.end()
        return pm

    def _rebuild(self):
        dpr = self.devicePixelRatioF()
        self._pm = self.render(int(self.width() * dpr), int(self.height() * dpr))
        self._pm.setDevicePixelRatio(dpr)
        self._size = self.size()
        self.update()

    def resizeEvent(self, e):
        if self._pm is None:
            self._rebuild()
        else:
            self._timer.start(90)            # erst nach dem Ziehen neu rendern (bis dahin wird skaliert)

    def paintEvent(self, e):
        if self._pm is None:
            self._rebuild()
        p = QPainter(self)
        if self._size == self.size():
            p.drawPixmap(0, 0, self._pm)
        else:
            p.drawPixmap(self.rect(), self._pm)


# ------------------------------------------------------------------ Glas
class Glass(QFrame):
    """Milchglas-Flaeche: halbtransparentes Weiss, feiner heller Rand."""

    def __init__(self, parent=None, radius=20, alpha=20, border=True):
        super().__init__(parent)
        self.radius, self.alpha, self.border = radius, alpha, border
        self.setAttribute(Qt.WA_StyledBackground, False)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, white(self.alpha + 12))
        g.setColorAt(1, white(self.alpha - 8))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(r, self.radius, self.radius)
        if self.border:
            bg = QLinearGradient(r.topLeft(), r.bottomRight())
            bg.setColorAt(0, white(80))
            bg.setColorAt(0.5, white(22))
            bg.setColorAt(1, white(40))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(bg, 1))
            p.drawRoundedRect(r, self.radius, self.radius)


# ------------------------------------------------------------------ Buttons
class PopButton(QAbstractButton):
    """Button mit Pop-Effekt: waechst weich, solange die Maus darauf ist."""
    M = 6

    def __init__(self, text="", icon=None, kind="glass", height=40, parent=None, icon_only=False):
        super().__init__(parent)
        self.setText(text)
        self.icon_name, self.kind, self.h, self.icon_only = icon, kind, height, icon_only
        self.pop = 0.0
        self.press = 0.0
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFont(app_font(10, True))
        self.setFixedHeight(height + 2 * self.M)
        if icon_only:
            self.setFixedWidth(height + 2 * self.M)

    def set_kind(self, kind):
        self.kind = kind
        self.update()

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(self.text()) + 2 * 20 + 2 * self.M
        if self.icon_name and self.text():
            w += 26
        return QSize(max(w, self.h + 2 * self.M), self.h + 2 * self.M)

    def enterEvent(self, e):
        tween(self, "pop", 1.0, 170, QEasingCurve.OutBack)

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 170)
        tween(self, "press", 0.0, 90)

    def mousePressEvent(self, e):
        tween(self, "press", 1.0, 70)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        tween(self, "press", 0.0, 110)
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        M = self.M
        r = QRectF(self.rect()).adjusted(M, M, -M, -M)
        s = 1 + 0.06 * self.pop - 0.035 * self.press
        c = r.center()
        p.translate(c)
        p.scale(s, s)
        p.translate(-c)
        p.setOpacity(1.0 if self.isEnabled() else 0.4)
        rad = min(r.height() / 2, 16)
        hv = max(0.0, min(1.0, self.pop))
        k = self.kind
        if k == "accent":
            for grow, a in ((9, 26), (5, 40)):
                p.setPen(Qt.NoPen)
                p.setBrush(with_alpha(ACC, a * hv))
                p.drawRoundedRect(r.adjusted(-grow * hv, -grow * hv, grow * hv, grow * hv), rad + grow * hv,
                                  rad + grow * hv)
            g = QLinearGradient(r.topLeft(), r.topRight())
            g.setColorAt(0, ACC.lighter(100 + int(10 * hv)))
            g.setColorAt(1, ACC2.lighter(100 + int(10 * hv)))
            p.setBrush(g)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, rad, rad)
            fg = DARK_ON_ACC
        elif k == "danger":
            p.setBrush(with_alpha(RED, 55 + 55 * hv))
            p.setPen(QPen(with_alpha(RED, 150), 1))
            p.drawRoundedRect(r, rad, rad)
            fg = QColor("#ffc2cb")
        elif k == "flat":
            p.setBrush(white(20 * hv))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, rad, rad)
            fg = white(170 + 70 * hv)
        else:                                            # glass
            p.setBrush(white(24 + 26 * hv))
            p.setPen(QPen(white(50 + 40 * hv), 1))
            p.drawRoundedRect(r, rad, rad)
            fg = white(240)
        p.setPen(fg)
        fm = QFontMetrics(self.font())
        tw = 0 if self.icon_only else fm.horizontalAdvance(self.text())
        iw = 18 if self.icon_name else 0
        gap = 8 if (iw and tw) else 0
        x = r.center().x() - (iw + gap + tw) / 2
        if self.icon_name:
            paint_icon(p, self.icon_name, QRectF(x, r.center().y() - iw / 2, iw, iw), fg)
            x += iw + gap
        if tw:
            p.setFont(self.font())
            p.drawText(QRectF(x, r.top(), tw + 2, r.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())


class Segmented(QWidget):
    """Auswahl-Schalter mit gleitender Markierung."""
    changed = Signal(str)

    def __init__(self, items, parent=None, height=38):
        super().__init__(parent)
        self.items = list(items)
        self.idx = 0
        self.pos = 0.0
        self.setFixedHeight(height)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(app_font(9.5, True))

    def current(self):
        return self.items[self.idx]

    def set_current(self, name, emit=False):
        if name in self.items:
            self.idx = self.items.index(name)
            tween(self, "pos", self.idx, 200)
            if emit:
                self.changed.emit(name)

    def mousePressEvent(self, e):
        w = (self.width() - 8) / len(self.items)
        i = int(max(0, min(len(self.items) - 1, (e.position().x() - 4) // w)))
        if i != self.idx:
            self.set_current(self.items[i], True)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(white(46), 1))
        p.setBrush(white(18))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        n = len(self.items)
        w = (self.width() - 8) / n
        hr = QRectF(4 + self.pos * w, 4, w, self.height() - 8)
        g = QLinearGradient(hr.topLeft(), hr.topRight())
        g.setColorAt(0, ACC)
        g.setColorAt(1, ACC2)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(hr, hr.height() / 2, hr.height() / 2)
        p.setFont(self.font())
        for i, t in enumerate(self.items):
            sel = max(0.0, 1 - abs(self.pos - i))
            c = QColor(int(255 + (4 - 255) * sel), int(255 + (35 - 255) * sel), int(255 + (27 - 255) * sel), 230)
            p.setPen(c)
            p.drawText(QRectF(4 + i * w, 0, w, self.height()), Qt.AlignCenter, t)


class Toggle(QAbstractButton):
    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.k = 1.0 if checked else 0.0
        self.setFixedSize(50, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.toggled.connect(lambda c: tween(self, "k", 1.0 if c else 0.0, 180, QEasingCurve.OutBack))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 4, -2, -4)
        k = max(0.0, min(1.0, self.k))
        g = QLinearGradient(r.topLeft(), r.topRight())
        g.setColorAt(0, QColor(int(255 + (62 - 255) * k), int(255 + (224 - 255) * k), int(255 + (180 - 255) * k),
                               int(50 + 180 * k)))
        g.setColorAt(1, QColor(int(255 + (66 - 255) * k), int(255 + (184 - 255) * k), int(255 + (240 - 255) * k),
                               int(50 + 180 * k)))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - d - 6) * self.k
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(QRectF(x, r.top() + 4, d, d))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))


class Bar(QWidget):
    """Duenner Fortschrittsbalken mit sanfter Animation."""

    def __init__(self, parent=None, height=6):
        super().__init__(parent)
        self.v = 0.0
        self.setFixedHeight(height)

    def set_value(self, v, animate=True):
        v = max(0.0, min(1.0, v))
        if animate:
            tween(self, "v", v, 200)
        else:
            self.v = v
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(white(30))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if self.v > 0.003:
            fr = QRectF(r.left(), r.top(), max(r.height(), r.width() * self.v), r.height())
            g = QLinearGradient(fr.topLeft(), fr.topRight())
            g.setColorAt(0, ACC)
            g.setColorAt(1, ACC2)
            p.setBrush(g)
            p.drawRoundedRect(fr, r.height() / 2, r.height() / 2)


class SliderRow(QWidget):
    def __init__(self, title, lo, hi, steps, value, fmt, parent=None):
        super().__init__(parent)
        self.fmt = fmt
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.title = QLabel(title)
        self.title.setFixedWidth(112)
        self.title.setStyleSheet("color: rgba(255,255,255,150);")
        self.s = QSlider(Qt.Horizontal)
        self.s.setRange(0, steps)
        self.lo, self.hi, self.steps = lo, hi, steps
        self.s.setValue(round((value - lo) / (hi - lo) * steps))
        self.s.setCursor(Qt.PointingHandCursor)
        self.val = QLabel()
        self.val.setFixedWidth(92)
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.val.setFont(app_font(10, True))
        lay.addWidget(self.title)
        lay.addWidget(self.s, 1)
        lay.addWidget(self.val)
        self.s.valueChanged.connect(lambda _: self.val.setText(self.fmt(self.value())))
        self.val.setText(self.fmt(self.value()))

    def value(self):
        return self.lo + self.s.value() / self.steps * (self.hi - self.lo)


# ------------------------------------------------------------------ Uebergaenge
class FadeStack(QWidget):
    """Seitenwechsel mit weichem Ein-/Ausblenden und leichter Verschiebung."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pages = []
        self.cur = -1
        self.t = 1.0
        self.anim_on = False
        self.old_px = self.new_px = None
        self.dir = 1
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(300)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.valueChanged.connect(self._tick)
        self.anim.finished.connect(self._done)

    def add(self, w):
        w.setParent(self)
        w.hide()
        self.pages.append(w)
        return len(self.pages) - 1

    def resizeEvent(self, e):
        for w in self.pages:
            w.setGeometry(self.rect())

    def go(self, i, direction=1):
        if i == self.cur:
            return
        new = self.pages[i]
        if self.cur < 0 or not self.isVisible() or self.anim_on:
            for k, w in enumerate(self.pages):
                w.setVisible(k == i)
            new.setGeometry(self.rect())
            self.cur = i
            self.anim_on = False
            return
        old = self.pages[self.cur]
        self.old_px = old.grab()
        new.setGeometry(self.rect())
        new.show()
        self.new_px = new.grab()
        new.hide()
        old.hide()
        self.cur, self.dir, self.t, self.anim_on = i, direction, 0.0, True
        self.anim.start()

    def _tick(self, v):
        self.t = float(v)
        self.update()

    def _done(self):
        self.anim_on = False
        self.old_px = self.new_px = None
        self.pages[self.cur].show()
        self.update()

    def paintEvent(self, e):
        if not self.anim_on:
            return
        p = QPainter(self)
        t = self.t
        shift = 28 * self.dir
        if self.old_px:
            p.setOpacity(max(0.0, 1 - t * 1.6))
            p.drawPixmap(int(-shift * t), 0, self.old_px)
        if self.new_px:
            p.setOpacity(min(1.0, t * 1.3))
            p.drawPixmap(int(shift * (1 - t)), 0, self.new_px)


class Overlay(QWidget):
    """Abgedunkelter Hintergrund mit einer Glas-Karte in der Mitte (Dialoge)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.card = None
        self.k = 0.0
        self.hide()
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(240)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._tick)
        self.anim.finished.connect(self._finished)
        self.closing = False
        self.eff = None

    def present(self, card):
        if self.card is not None:
            self.card.hide()
            self.card.deleteLater()
        self.card = card
        card.setParent(self)
        self.setGeometry(self.parent().rect())
        self.eff = QGraphicsOpacityEffect(card)
        card.setGraphicsEffect(self.eff)
        if hasattr(card, "fit"):
            card.fit()
        else:
            card.adjustSize()
        self._center()
        card.show()
        self.show()
        self.raise_()
        self.closing = False
        self.anim.stop()
        self.anim.setStartValue(self.k)
        self.anim.setEndValue(1.0)
        self.anim.start()

    def dismiss(self):
        if self.card is None or self.closing:
            return
        self.closing = True
        self.anim.stop()
        self.anim.setStartValue(self.k)
        self.anim.setEndValue(0.0)
        self.anim.start()

    def _center(self):
        if self.card:
            c = self.card
            off = int((1 - self.k) * 22)
            c.move((self.width() - c.width()) // 2, (self.height() - c.height()) // 2 + off)

    def _tick(self, v):
        self.k = float(v)
        if self.eff:
            self.eff.setOpacity(self.k)
        self._center()
        self.update()

    def _finished(self):
        if self.closing:
            self.hide()
            if self.card:
                self.card.deleteLater()
                self.card = None
            self.closing = False
        elif self.card:
            self.card.setGraphicsEffect(None)          # Effekt nach dem Einblenden entfernen (spart Leistung)
            self.eff = None

    def resizeEvent(self, e):
        self._center()

    def mousePressEvent(self, e):
        e.accept()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(4, 6, 18, int(165 * self.k)))


class Toast(Glass):
    def __init__(self, parent):
        super().__init__(parent, radius=18, alpha=34)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 11, 20, 11)
        self.lbl = QLabel()
        self.lbl.setFont(app_font(10.5, True))
        lay.addWidget(self.lbl)
        self.dot = QColor(ACC)
        self.eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.eff)
        self.k = 0.0
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(260)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._tick)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._hide)
        self.hide()

    def show_text(self, text, color=ACC):
        self.lbl.setText(text)
        self.dot = QColor(color)
        self.adjustSize()
        self.k = max(self.k, 0.0)
        self.show()
        self.raise_()
        self.anim.stop()
        self.anim.setStartValue(self.k)
        self.anim.setEndValue(1.0)
        self.anim.start()
        self.timer.start(3200)

    def _hide(self):
        self.anim.stop()
        self.anim.setStartValue(self.k)
        self.anim.setEndValue(0.0)
        self.anim.start()

    def _tick(self, v):
        self.k = float(v)
        self.eff.setOpacity(self.k)
        self.reposition()
        if self.k <= 0.001 and self.anim.endValue() == 0.0:
            self.hide()

    def reposition(self):
        pw, ph = self.parent().width(), self.parent().height()
        self.move((pw - self.width()) // 2, int(-self.height() + (self.height() + 26) * self.k))

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.dot)
        p.drawRoundedRect(QRectF(0, 12, 4, self.height() - 24), 2, 2)


# ------------------------------------------------------------------ Zeitleiste
class Timeline(QWidget):
    """Ueberblick ueber das ganze Video + zoombare Detailansicht mit ziehbaren Clip-Raendern."""
    seek = Signal(float)
    pick = Signal(int)
    edited = Signal(int)
    PAD, H_OV = 14, 34

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.m = model
        self.v0, self.v1 = 0.0, 60.0
        self.mode = None
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self.setFont(app_font(8.5))

    def x_of(self, t):
        return self.PAD + (t - self.v0) / (self.v1 - self.v0) * (self.width() - 2 * self.PAD)

    def t_of(self, x):
        return self.v0 + (x - self.PAD) / (self.width() - 2 * self.PAD) * (self.v1 - self.v0)

    def ox(self, t):
        return self.PAD + t / max(0.1, self.m.duration) * (self.width() - 2 * self.PAD)

    def ot(self, x):
        return (x - self.PAD) / (self.width() - 2 * self.PAD) * self.m.duration

    def focus_on(self, t0, t1):
        span = max(24.0, (t1 - t0) * 3)
        c = (t0 + t1) / 2
        self.set_view(c - span / 2, c + span / 2)

    def set_view(self, a, b):
        d = max(1.0, self.m.duration)
        span = min(max(4.0, b - a), d)
        a = min(max(0.0, a), d - span)
        self.v0, self.v1 = a, a + span
        self.update()

    def zoom(self, direction, x):
        t = self.t_of(x)
        f = 0.75 if direction > 0 else 1.33
        self.set_view(t - (t - self.v0) * f, t + (self.v1 - t) * f)

    def wheelEvent(self, e):
        self.zoom(1 if e.angleDelta().y() > 0 else -1, e.position().x())

    # --- Zeichnen
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m, w, P = self.m, self.width(), self.PAD
        if not m.has_video:
            p.setPen(white(110))
            p.setFont(app_font(10))
            p.drawText(self.rect(), Qt.AlignCenter, "Öffne ein Video, um die Zeitleiste zu sehen")
            return
        # Ueberblick
        p.setPen(Qt.NoPen)
        p.setBrush(white(22))
        p.drawRoundedRect(QRectF(P, 8, w - 2 * P, 18), 9, 9)
        for i, c in enumerate(m.clips):
            sel = i == m.sel
            x0 = self.ox(c.start)
            p.setBrush(with_alpha(ACC, 230) if sel else white(75))
            p.drawRoundedRect(QRectF(x0, 8, max(self.ox(c.end) - x0, 4), 18), 4, 4)
            p.setBrush(RED)
            p.drawRect(QRectF(self.ox(c.kill) - 1, 8, 2, 18))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(white(200), 1.6))
        p.drawRoundedRect(QRectF(self.ox(self.v0), 6, max(6, self.ox(self.v1) - self.ox(self.v0)), 22), 6, 6)
        p.setPen(QPen(QColor("white"), 2))
        p.drawLine(QPointF(self.ox(m.pos), 4), QPointF(self.ox(m.pos), 30))
        # Lineal
        top = self.H_OV + 8
        span = self.v1 - self.v0
        step = next((s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800)
                     if s / span * (w - 2 * P) >= 70), 3600)
        t = int(self.v0 // step) * step
        p.setFont(self.font())
        while t <= self.v1:
            if t >= self.v0:
                x = self.x_of(t)
                p.setPen(white(50))
                p.drawLine(QPointF(x, top + 13), QPointF(x, top + 21))
                p.setPen(white(120))
                txt = f"{int(t // 60)}:{int(t % 60):02d}" if step >= 1 else f"{t:.1f}s"
                p.drawText(QRectF(x - 30, top - 6, 60, 14), Qt.AlignCenter, txt)
            t += step
        # Spur
        y0, y1 = top + 28, top + 28 + 62
        p.setPen(QPen(white(30), 1))
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(QRectF(P, y0, w - 2 * P, y1 - y0), 12, 12)
        p.setClipRect(QRectF(P, y0, w - 2 * P, y1 - y0))
        for i, c in enumerate(m.clips):
            if c.end < self.v0 or c.start > self.v1:
                continue
            sel = i == m.sel
            xa, xb = self.x_of(c.start), self.x_of(c.end)
            rr = QRectF(xa, y0 + 6, xb - xa, y1 - y0 - 12)
            p.setBrush(with_alpha(ACC, 70) if sel else white(26))
            p.setPen(QPen(ACC if sel else white(60), 1.6 if sel else 1))
            p.drawRoundedRect(rr, 10, 10)
            p.setPen(QColor("white") if sel else white(160))
            p.setFont(app_font(9, True))
            p.drawText(rr, Qt.AlignCenter, f"KILL {i + 1:02d}  ·  {c.end - c.start:.1f}s")
            if sel:
                p.setPen(Qt.NoPen)
                p.setBrush(ACC)
                for x in (xa, xb):
                    p.drawRoundedRect(QRectF(x - 4, y0 + 14, 8, y1 - y0 - 28), 4, 4)
            k = self.x_of(c.kill)
            p.setPen(Qt.NoPen)
            p.setBrush(RED)
            p.drawPolygon(QPolygonF([QPointF(k - 6, y0 + 1), QPointF(k + 6, y0 + 1), QPointF(k, y0 + 12)]))
        p.setClipping(False)
        px = self.x_of(m.pos)
        if P <= px <= w - P:
            p.setPen(QPen(QColor("white"), 2))
            p.drawLine(QPointF(px, top + 20), QPointF(px, y1))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("white"))
            p.drawPolygon(QPolygonF([QPointF(px - 6, top + 20), QPointF(px + 6, top + 20), QPointF(px, top + 30)]))

    # --- Maus
    def near_handle(self, x):
        m = self.m
        if m.sel is None:
            return None
        c = m.clips[m.sel]
        if abs(x - self.x_of(c.start)) <= 9:
            return "start"
        if abs(x - self.x_of(c.end)) <= 9:
            return "end"
        return None

    def mouseMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        if e.buttons() & Qt.LeftButton:
            self._drag(x)
        else:
            self.setCursor(Qt.SizeHorCursor if (y > self.H_OV and self.m.has_video and self.near_handle(x))
                           else Qt.ArrowCursor)

    def mousePressEvent(self, e):
        m = self.m
        if not m.has_video or e.button() != Qt.LeftButton:
            return
        x, y = e.position().x(), e.position().y()
        if y < self.H_OV:
            self.mode = "ov"
            self._drag(x)
            return
        self.mode = self.near_handle(x)
        if self.mode:
            return
        t = self.t_of(x)
        for i, c in enumerate(m.clips):
            if c.start <= t <= c.end and i != m.sel:
                self.pick.emit(i)
                break
        self.mode = "scrub"
        self.seek.emit(t)

    def _drag(self, x):
        m = self.m
        if self.mode == "ov":
            t = self.ot(x)
            span = self.v1 - self.v0
            self.set_view(t - span / 2, t + span / 2)
            self.seek.emit(t)
        elif self.mode == "scrub":
            self.seek.emit(self.t_of(x))
        elif self.mode in ("start", "end") and m.sel is not None:
            c = m.clips[m.sel]
            t = min(max(0.0, self.t_of(x)), m.duration)
            if self.mode == "start":
                c.start = min(t, c.end - 0.5)
            else:
                c.end = max(t, c.start + 0.5)
            self.edited.emit(m.sel)
            self.seek.emit(c.start if self.mode == "start" else c.end)
            self.update()

    def mouseReleaseEvent(self, e):
        self.mode = None

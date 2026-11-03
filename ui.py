"""ACE - Oberflaechen-Bausteine im Glas-Design (Qt / PySide6)."""
import math

from PySide6.QtCore import (QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QPolygonF, QRadialGradient)
from theme import AMBER, RED, T, ink, white, with_alpha
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QSizePolicy, QSlider, QWidget)

FONT_FAMILIES = ["Montserrat", "Segoe UI", "Arial", "DejaVu Sans"]
DISPLAY_FAMILIES = ["Montserrat", "Segoe UI", "Arial"]


def load_fonts(folder):
    """Eigene Schriften (Manrope fuer Text, Sora fuer Ueberschriften und Zahlen) einmal beim Start laden."""
    import os
    from PySide6.QtGui import QFontDatabase
    try:
        for n in sorted(os.listdir(folder)):
            if n.lower().endswith(".ttf"):
                QFontDatabase.addApplicationFont(os.path.join(folder, n))
    except OSError:
        pass


def display_font(size=14.0, weight=QFont.DemiBold):
    """Ueberschriften, grosse Zahlen und Titel."""
    f = QFont()
    f.setFamilies(DISPLAY_FAMILIES)
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


def app_font(size=10.0, bold=False):
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSizeF(size)
    f.setWeight(QFont.Bold if bold else QFont.Medium)
    return f


# ------------------------------------------------------------------ Animation
def _bezier(x1, y1, x2, y2):
    c = QEasingCurve(QEasingCurve.BezierSpline)
    c.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1, 1))
    return c


def ease_out():
    """Kraeftiges Ease-Out: startet sofort schnell (fuehlt sich reaktionsschnell an), bremst weich ab. Kein Ueberschwingen."""
    return _bezier(0.23, 1, 0.32, 1)


def tween(w, name, end, ms=160, curve=None):
    """Animiert das Float-Attribut `name` von Widget `w` weich auf `end` (und zeichnet dabei neu).
    Unterbrechbar: eine neue Animation startet vom aktuellen Wert."""
    if curve is None:
        curve = ease_out()
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
    elif name == "min":
        p.drawLine(P(6, 12), P(18, 12))
    elif name == "square":
        p.drawRoundedRect(QRectF(6.5, 6.5, 11, 11), 2.4, 2.4)
    elif name == "user":
        p.drawEllipse(P(12, 8.5), 3.6, 3.6)
        p.drawArc(QRectF(5, 13.5, 14, 12), 20 * 16, 140 * 16)
    elif name == "lock":
        p.drawRoundedRect(QRectF(6, 11, 12, 9), 2.2, 2.2)
        p.drawArc(QRectF(8.5, 5, 7, 10), 0, 180 * 16)
        p.drawLine(P(8.5, 10), P(8.5, 11))
        p.drawLine(P(15.5, 10), P(15.5, 11))
    elif name == "ticket":
        p.drawRoundedRect(QRectF(4.5, 7, 15, 10), 2, 2)
        p.drawLine(P(14, 7.5), P(14, 16.5))
    elif name == "bell":
        path = QPainterPath()
        path.moveTo(P(6, 16.5))
        path.cubicTo(P(7.5, 14.5), P(7, 12.5), P(7, 10.5))
        path.cubicTo(P(7, 7.2), P(9.2, 5), P(12, 5))
        path.cubicTo(P(14.8, 5), P(17, 7.2), P(17, 10.5))
        path.cubicTo(P(17, 12.5), P(16.5, 14.5), P(18, 16.5))
        path.closeSubpath()
        p.drawPath(path)
        p.drawArc(QRectF(10, 16.5, 4, 3.4), 180 * 16, 180 * 16)
    elif name == "download":
        p.drawLine(P(12, 4.5), P(12, 14.5))
        poly([(7.5, 10.5), (12, 15), (16.5, 10.5)])
        p.drawPolyline(QPolygonF([P(5, 15.5), P(5, 19), P(19, 19), P(19, 15.5)]))
    elif name == "sun":
        p.drawEllipse(P(12, 12), 3.8, 3.8)
        for i in range(8):
            ang = i * math.pi / 4
            p.drawLine(P(12 + 6.3 * math.cos(ang), 12 + 6.3 * math.sin(ang)), P(12 + 8.6 * math.cos(ang), 12 + 8.6 * math.sin(ang)))
    elif name == "moon":
        path = QPainterPath()
        path.addEllipse(QRectF(5, 5, 14, 14))
        cut = QPainterPath()
        cut.addEllipse(QRectF(9.5, 2.5, 13, 13))
        p.drawPath(path.subtracted(cut))
    elif name == "arrow":
        p.drawLine(P(6, 12), P(18, 12))
        poly([(13, 7), (18, 12), (13, 17)])
    elif name == "expand":                       # typisches Vollbild-Zeichen: vier Ecken mit Abstand
        for (x, y, dx, dy) in ((4, 4, 1, 1), (20, 4, -1, 1), (4, 20, 1, -1), (20, 20, -1, -1)):
            p.drawPolyline(QPolygonF([P(x + 5 * dx, y), P(x, y), P(x, y + 5 * dy)]))
    elif name == "shrink":
        for (x, y, dx, dy) in ((9, 9, -1, -1), (15, 9, 1, -1), (9, 15, -1, 1), (15, 15, 1, 1)):
            p.drawPolyline(QPolygonF([P(x + 5 * dx, y), P(x, y), P(x, y + 5 * dy)]))
    elif name == "restore":
        p.drawRoundedRect(QRectF(5.5, 8.5, 10, 10), 2.2, 2.2)
        p.drawPolyline(QPolygonF([P(9, 8.5), P(9, 5.5), P(18.5, 5.5), P(18.5, 15), P(15.5, 15)]))
    elif name == "palette":
        p.drawEllipse(P(12, 12), 8, 8)
        for x, y in ((8.5, 10), (11, 7.5), (15, 9)):
            p.setBrush(color)
            p.drawEllipse(P(x, y), 1.1, 1.1)
        p.setBrush(Qt.NoBrush)
    elif name == "refresh":
        p.drawArc(QRectF(5, 5, 14, 14), 40 * 16, 270 * 16)
        poly([(17.5, 3.5), (18.5, 8.5), (13.5, 8)])
    p.restore()


class ElidedLabel(QLabel):
    """Einzeiliges Label, das zu langen Text mit … abkuerzt (statt das Layout aufzublaehen)."""

    def __init__(self, text="", size=10.5, bold=False, role="text", parent=None):
        super().__init__(parent)
        self._text = text
        self._role = role
        self.setFont(app_font(size, bold))
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, t):
        self._text = t
        self.updateGeometry()
        self.update()

    def text(self):
        return self._text

    def set_color(self, role):
        self._role = role
        self.update()

    def sizeHint(self):
        return QSize(0, QFontMetrics(self.font()).height() + 2)

    def minimumSizeHint(self):
        return QSize(0, QFontMetrics(self.font()).height() + 2)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setPen(T.role(self._role))
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
        self.hooks = {}
        self.blur = None
        self.setMouseTracking(True)

    def mousePressEvent(self, e):
        h = self.hooks.get("press")
        if h:
            h(e)

    def mouseMoveEvent(self, e):
        h = self.hooks.get("move")
        if h:
            h(e)

    def mouseDoubleClickEvent(self, e):
        h = self.hooks.get("double")
        if h:
            h(e)

    instance = None             # das Fenster-Hintergrundbild (fuer das Milchglas der Karten)

    @staticmethod
    def render(w, h):
        """Schwarz oben, von unten steigt weiches Licht in der Themenfarbe auf.
        Wird pixelgenau mit Kommazahlen berechnet und fein gestreut - so entstehen keine sichtbaren Ringe."""
        import numpy as np
        from PySide6.QtGui import QImage
        W, H = max(1, w), max(1, h)
        f = 3 if W * H > 1_500_000 else 2                # erst kleiner rechnen (weicher Verlauf, viel schneller)
        w, h = (W + f - 1) // f, (H + f - 1) // f
        gc = T.glow()
        g = np.array([gc.redF(), gc.greenF(), gc.blueF()], dtype=np.float32)
        gl = np.array([QColor(gc).lighter(112).redF(), QColor(gc).lighter(112).greenF(), QColor(gc).lighter(112).blueF()],
                      dtype=np.float32)
        base = np.zeros(3, np.float32) if T.dark else np.array([0.88, 0.885, 0.90], np.float32)   # Hell: sanftes Grau statt Weiss
        ys = (np.arange(h, dtype=np.float32) + 0.5) / h
        xs = (np.arange(w, dtype=np.float32) + 0.5) / w

        def smooth(e0, e1, x):
            t = np.clip((x - e0) / (e1 - e0), 0, 1)
            return t * t * (3 - 2 * t)
        v = smooth(0.40, 1.0, ys)[:, None]                                     # Licht von unten
        dx = (xs[None, :] - 0.5) * w / (0.78 * w)
        dy = (ys[:, None] - 0.18) * h / (0.80 * h)
        s = smooth(0.42, 1.05, np.sqrt(dx * dx + dy * dy))                     # dunkle Ellipse oben in der Mitte
        a = v * s
        m = float(max(w, h))
        corner = np.zeros((h, w), np.float32)
        for cx in (0.0, 1.0):                                                  # weiche hellere Ecken unten
            d = np.sqrt(((xs[None, :] - cx) * w) ** 2 + ((ys[:, None] - 1.02) * h) ** 2) / (0.45 * m)
            c = np.clip(1 - d, 0, 1)
            corner += c * c * 0.32
        col = base[None, None, :] + (g - base)[None, None, :] * a[:, :, None]
        col += (gl - col) * np.clip(corner, 0, 1)[:, :, None] * (1.0 if T.dark else 0.6)
        col = np.repeat(np.repeat(col * 255.0, f, axis=0), f, axis=1)[:H, :W]
        tile = np.random.default_rng(5).uniform(-0.75, 0.75, size=(128, 128, 1)).astype(np.float32)
        col += np.tile(tile, ((H + 127) // 128, (W + 127) // 128, 1))[:H, :W]
        rgb = np.clip(np.rint(col), 0, 255).astype(np.uint8)
        h, w = H, W
        buf = np.empty((h, w, 4), np.uint8)
        buf[:, :, 0] = rgb[:, :, 2]
        buf[:, :, 1] = rgb[:, :, 1]
        buf[:, :, 2] = rgb[:, :, 0]
        buf[:, :, 3] = 255
        img = QImage(buf.tobytes(), w, h, w * 4, QImage.Format_RGB32).copy()
        return QPixmap.fromImage(img)

    @staticmethod
    def _dither(pm):
        """Feines Rauschen (+-1,5 Helligkeitsstufen): Farbverlaeufe zeigen dann keine Ringe/Stufen mehr."""
        try:
            import numpy as np
            from PySide6.QtGui import QImage
            img = pm.toImage().convertToFormat(QImage.Format_RGB32)
            w, h = img.width(), img.height()
            ptr = img.bits()
            arr = np.frombuffer(ptr, dtype=np.uint8, count=img.sizeInBytes()).reshape(h, img.bytesPerLine())[:, :w * 4]
            arr = arr.reshape(h, w, 4).astype(np.int16)
            noise = np.random.default_rng(3).integers(-2, 3, size=(h, w, 1), dtype=np.int16)
            arr[:, :, :3] = np.clip(arr[:, :, :3] + noise, 0, 255)
            out = QImage(arr.astype(np.uint8).tobytes(), w, h, w * 4, QImage.Format_RGB32).copy()
            return QPixmap.fromImage(out)
        except Exception:
            return pm

    @staticmethod
    def login_backdrop(w, h):
        """Hintergrund des Anmeldefensters: schwarze Flaeche mit weichen blauen Schwaden (wie Bild 2)."""
        from PySide6.QtGui import QTransform
        pm = QPixmap(max(1, w // 4), max(1, h // 4))
        sw, sh = pm.width(), pm.height()
        pm.fill(QColor(0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        blue = QColor(30, 64, 205)
        for (x, y, rx, ry, rot, a) in ((0.18, 0.25, 0.30, 0.10, 35, 230), (0.70, 0.12, 0.38, 0.11, -20, 220),
                                        (0.85, 0.55, 0.30, 0.13, 60, 200), (0.30, 0.78, 0.36, 0.10, -35, 230),
                                        (0.55, 0.45, 0.22, 0.07, 15, 170), (0.05, 0.60, 0.18, 0.12, 80, 190),
                                        (0.62, 0.92, 0.28, 0.09, 10, 200), (0.95, 0.15, 0.15, 0.10, 45, 180)):
            p.save()
            p.setTransform(QTransform().translate(x * sw, y * sh).rotate(rot).scale(rx * sw, ry * sh))
            rg = QRadialGradient(0, 0, 1.0)
            rg.setColorAt(0.0, with_alpha(blue, a))
            rg.setColorAt(0.6, with_alpha(blue, a * 0.55))
            rg.setColorAt(1.0, with_alpha(blue, 0))
            p.fillRect(QRectF(-1.2, -1.2, 2.4, 2.4), rg)
            p.restore()
        p.end()
        return Background.blurred(pm).scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    _grain_pm = None

    @staticmethod
    def _grain():
        if Background._grain_pm is None:
            import random
            from PySide6.QtGui import QImage
            img = QImage(160, 160, QImage.Format_ARGB32)
            rnd = random.Random(7)
            for y in range(160):
                for x in range(160):
                    v = rnd.randint(0, 255)
                    img.setPixel(x, y, (40 << 24) | (v << 16) | (v << 8) | v)
            Background._grain_pm = QPixmap.fromImage(img)
        return Background._grain_pm

    @staticmethod
    def blurred(pm):
        """Stark weichgezeichnete Kopie (verkleinern und wieder vergroessern) - Grundlage des Milchglases."""
        w, h = pm.width(), pm.height()
        img = pm.toImage()
        for f in (6, 3):
            small = img.scaled(max(1, w // f), max(1, h // f), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            img = small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return QPixmap.fromImage(img)

    fade_k = 0.0

    def prepare_next(self):
        """Hintergrund fuer die gerade eingestellten Farben vorab berechnen (fuer den Uebergang)."""
        dpr = self.devicePixelRatioF()
        pm = self.render(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        return pm

    def begin_fade(self, pm):
        self._next = pm
        self.fade_k = 0.0

    def set_fade(self, v):
        self.fade_k = float(v)
        self.update()

    def end_fade(self):
        if getattr(self, "_next", None) is not None:
            self._pm, self._next = self._next, None
            self._size = self.size()
        self.update()

    def refresh(self):
        self._rebuild()

    def _rebuild(self):
        dpr = self.devicePixelRatioF()
        self._pm = self.render(int(self.width() * dpr), int(self.height() * dpr))
        self.blur = None                         # kein Glas mehr: Karten sind ruhige, deckende Flaechen
        self._pm.setDevicePixelRatio(dpr)
        Background.instance = self
        self._size = self.size()
        self.update()

    def resizeEvent(self, e):
        if self._pm is None:
            self._rebuild()
        else:
            self._timer.start(90)            # erst nach dem Ziehen neu rendern (bis dahin wird skaliert)

    radius = 26.0               # Eckenradius des Fensters (0 = eckig, z. B. maximiert)

    def clip_path(self):
        path = QPainterPath()
        if self.radius > 0:
            path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
        else:
            path.addRect(QRectF(self.rect()))
        return path

    def paintEvent(self, e):
        if self._pm is None:
            self._rebuild()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setClipPath(self.clip_path())
        if self._size == self.size():
            p.drawPixmap(0, 0, self._pm)
        else:
            p.drawPixmap(self.rect(), self._pm)
        nx = getattr(self, "_next", None)
        if nx is not None:                           # neuer Hintergrund blendet weich ueber den alten
            p.setOpacity(max(0.0, min(1.0, self.fade_k)))
            p.drawPixmap(self.rect(), nx)
            p.setOpacity(1.0)
        # feiner, klarer Rahmen ums Fenster (flaches Kartendesign)
        if self.radius > 0:
            r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            p.setClipping(False)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(T.line(1.3), 1.0))
            p.drawRoundedRect(r, self.radius, self.radius)


# ------------------------------------------------------------------ Glas
class Glass(QFrame):
    """Milchglas-Flaeche: halbtransparent mit feinem Rand (hell oder dunkel je nach Thema)."""

    def __init__(self, parent=None, radius=22, alpha=110, border=True, dialog=False):
        super().__init__(parent)
        self.radius, self.alpha, self.border, self.dialog = radius, alpha, border, dialog
        self.tint = False
        self.setAttribute(Qt.WA_StyledBackground, False)

    def paintEvent(self, e):
        """Milchglas: weichgezeichneter Hintergrund, heller Schleier, Lichtkante."""
        if getattr(self, "ghost", False):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        paint_glass(p, self, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), min(self.radius, 30) * 0.8,
                    self.alpha, dialog=self.dialog, tint=self.tint, border=self.border)


def paint_glass(p, w, r, rad, alpha=130, dialog=False, tint=False, border=True, sheen=None):
    """Karte (ohne Glas): deckende ruhige Flaeche, feiner warmer Rand, optional Lichtfleck unter der Maus."""
    path = QPainterPath()
    path.addRoundedRect(r, rad, rad)
    p.save()
    p.setClipPath(path)
    p.setPen(Qt.NoPen)
    k = max(0.0, min(1.0, (alpha - 90) / 120.0))
    p.fillRect(r, T.dialog_fill if dialog else (T.card_fill(alpha) if alpha >= 175 else T.glass_veil(k)))
    if tint:
        tg = QLinearGradient(r.topLeft(), r.bottomRight())
        tg.setColorAt(0, with_alpha(T.glow(), 70 if T.dark else 45))
        tg.setColorAt(0.8, with_alpha(T.glow(), 0))
        p.fillRect(r, tg)
    if sheen is not None and sheen[2] > 0.01:
        sx, sy, sk = sheen
        rg = QRadialGradient(sx, sy, max(110.0, r.width() * 0.5))
        rg.setColorAt(0, QColor(255, 255, 255, int((14 if T.dark else 0) * sk)) if T.dark else QColor(0, 0, 0, int(8 * sk)))
        rg.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillRect(r, rg)
    p.restore()
    if border:
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(T.glass_edge(1.0), 1.0))
        p.drawRoundedRect(r, rad, rad)


# ------------------------------------------------------------------ Buttons
class PopButton(QAbstractButton):
    """Button mit Pop-Effekt: waechst weich, solange die Maus darauf ist."""
    M = 6

    def __init__(self, text="", icon=None, kind="glass", height=40, parent=None, icon_only=False):
        super().__init__(parent)
        self.setText(text)
        self.icon_name, self.kind, self.h, self.icon_only = icon, kind, height, icon_only
        self.on_dark = False                          # True: sitzt auf dem Video (Schrift immer hell)
        self.pop = 0.0
        self.press = 0.0
        self.fill = 0.0
        self.ox = self.oy = 0.0
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)             # nur per Tab erreichbar (Klick nimmt keinen Fokus)
        self._kfocus = False
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
        tween(self, "pop", 1.0, 150)
        pos = e.position() if hasattr(e, "position") else None
        if pos is not None:
            self.ox, self.oy = pos.x(), pos.y()
        tween(self, "fill", 1.0, 500, _bezier(0.16, 1, 0.3, 1))      # Fuellung waechst vom Eintrittspunkt aus

    def leaveEvent(self, e):
        tween(self, "pop", 0.0, 120)                 # Ausblenden etwas schneller als Einblenden
        tween(self, "press", 0.0, 120)
        tween(self, "fill", 0.0, 500, _bezier(0.16, 1, 0.3, 1))

    def mousePressEvent(self, e):
        self.ox, self.oy = e.position().x(), e.position().y()
        tween(self, "press", 1.0, 100)
        tween(self, "fill", 1.0, 500, _bezier(0.16, 1, 0.3, 1))
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        tween(self, "press", 0.0, 160)
        super().mouseReleaseEvent(e)

    def focusInEvent(self, e):
        self._kfocus = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        self._kfocus = False
        super().focusOutEvent(e)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        M = self.M
        r = QRectF(self.rect()).adjusted(M, M, -M, -M)
        s = 1 + 0.012 * self.pop - 0.045 * self.press       # Hover: hebt sich leicht, Druck: 0,97
        c = r.center()
        p.translate(0, -1.2 * self.pop * (1 - self.press))
        p.translate(c)
        p.scale(s, s)
        p.translate(-c)
        p.setOpacity(1.0 if self.isEnabled() else 0.4)
        hv = max(0.0, min(1.0, self.pop))
        k = self.kind
        # Form: Text-Knoepfe abgerundet (12 px), reine Symbol-Knoepfe rund
        rad = r.height() / 2 if self.icon_only else min(12.0, r.height() / 2)
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        dark = T.dark
        # je Art: Grundflaeche, Rand, Schrift, Farbe des wachsenden Kreises, Schrift wenn gefuellt
        if k == "accent":
            base, fg = T.pill()
            edge = None
            fillc = QColor(T.acc_text) if dark else QColor(T.acc)
            if abs(fillc.lightness() - base.lightness()) < 40:
                fillc = base.darker(150)
            fg2 = QColor("#ffffff") if fillc.lightness() < 150 else QColor("#111111")
        elif k == "blue":
            base, fg, edge = QColor(20, 99, 255), QColor("#ffffff"), None
            fillc, fg2 = QColor("#ffffff"), QColor(20, 99, 255)
        elif k == "white":
            base, fg, edge = QColor(240, 241, 246), QColor("#1f2430"), None
            fillc, fg2 = QColor("#111111"), QColor("#ffffff")
        elif k == "danger":
            base, fg, edge = with_alpha(RED, 46), QColor(T.err), with_alpha(RED, 130)
            fillc, fg2 = QColor(RED), QColor("#ffffff")
        elif k == "flat":
            base, edge = QColor(0, 0, 0, 0), None
            fg = QColor(255, 255, 255, int(190 + 65 * hv)) if self.on_dark else ink(170 + 70 * hv)
            fillc = QColor(255, 255, 255, 235) if (self.on_dark or dark) else QColor(17, 17, 17)
            fg2 = QColor("#111111") if (self.on_dark or dark) else QColor("#ffffff")
        elif k == "dark":
            base, fg, edge = QColor(8, 9, 10, 215), QColor(255, 255, 255, 235), QColor(255, 255, 255, 50)
            fillc, fg2 = QColor(255, 255, 255, 240), QColor("#111111")
        else:                                              # glass / Standard
            base, fg, edge = T.card_fill(150), ink(235), T.glass_edge(1.0)
            fillc = ink(255)
            fg2 = QColor("#111111") if dark else QColor("#ffffff")
        p.setPen(Qt.NoPen)
        p.setBrush(base)
        p.drawPath(path)
        # Origin-Effekt: Kreis waechst vom Punkt, an dem die Maus hereinkommt
        f = max(0.0, min(1.0, self.fill)) if self.isEnabled() else 0.0
        if f > 0.002:
            ox, oy = self.ox, self.oy
            cover = 2 * max(math.hypot(ox - r.left(), oy - r.top()), math.hypot(r.right() - ox, oy - r.top()),
                            math.hypot(ox - r.left(), r.bottom() - oy), math.hypot(r.right() - ox, r.bottom() - oy))
            p.save()
            p.setClipPath(path)
            p.setBrush(fillc)
            rr = cover / 2 * f
            p.drawEllipse(QPointF(ox, oy), rr, rr)
            p.restore()
        if edge is not None:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(edge, 0.8))
            p.drawPath(path)
        if f > 0.45:
            fg = fg2
        if self._kfocus and self.hasFocus():            # sichtbarer Tastatur-Fokus
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(T.acc, 2))
            p.drawRoundedRect(r.adjusted(-3, -3, 3, 3), rad + 3, rad + 3)
        p.setPen(fg)
        fm = QFontMetrics(self.font())
        tw = 0 if self.icon_only else fm.horizontalAdvance(self.text())
        iw = 18 if self.icon_name else 0
        gap = 8 if (iw and tw) else 0
        x = r.center().x() - (iw + gap + tw) / 2
        if getattr(self, "icon_right", False):
            if tw:
                p.setFont(self.font())
                p.drawText(QRectF(x, r.top(), tw + 2, r.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())
            if self.icon_name:
                ix = x + tw + gap + 2 * self.pop
                paint_icon(p, self.icon_name, QRectF(ix, r.center().y() - iw / 2, iw, iw), fg)
            return
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
        if not getattr(self, "on_dark", False):
            p.setPen(QPen(T.line(1.4), 1))
            p.setBrush(ink(10))
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        n = len(self.items)
        w = (self.width() - 8) / n
        hr = QRectF(4 + self.pos * w, 4, w, self.height() - 8)
        pf, pfg = T.pill()
        base = ink(150)
        if getattr(self, "on_dark", False):              # auf dem Video: immer helle Darstellung
            pf, pfg, base = QColor(255, 255, 255, 230), QColor("#14161a"), QColor(255, 255, 255, 170)
        p.setPen(Qt.NoPen)
        p.setBrush(pf)
        p.drawRoundedRect(hr, hr.height() / 2, hr.height() / 2)
        p.setFont(self.font())
        hi = pfg
        for i, t in enumerate(self.items):
            sel = max(0.0, 1 - abs(self.pos - i))
            c = QColor(int(base.red() + (hi.red() - base.red()) * sel), int(base.green() + (hi.green() - base.green()) * sel),
                       int(base.blue() + (hi.blue() - base.blue()) * sel), int(150 + 105 * sel))
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
        self.setFocusPolicy(Qt.TabFocus)
        self._kfocus = False
        self.toggled.connect(lambda c: tween(self, "k", 1.0 if c else 0.0, 170))

    def focusInEvent(self, e):
        self._kfocus = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        self._kfocus = False
        super().focusOutEvent(e)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 4, -2, -4)
        k = max(0.0, min(1.0, self.k))
        if self._kfocus and self.hasFocus():
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(T.acc, 2))
            p.drawRoundedRect(r.adjusted(-2.5, -2.5, 2.5, 2.5), r.height() / 2 + 2.5, r.height() / 2 + 2.5)
            p.setPen(Qt.NoPen)
        off = T.toggle_off()

        def mixc(c):
            return QColor(int(off.red() + (c.red() - off.red()) * k), int(off.green() + (c.green() - off.green()) * k),
                          int(off.blue() + (c.blue() - off.blue()) * k), int(off.alpha() + (255 - off.alpha()) * k))
        p.setPen(Qt.NoPen)
        p.setBrush(mixc(T.pill()[0]))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - d - 6) * self.k
        p.setBrush(QColor(0, 0, 0, 50))
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
        p.setBrush(ink(26))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if self.v > 0.003:
            fr = QRectF(r.left(), r.top(), max(r.height(), r.width() * self.v), r.height())
            p.setBrush(T.acc_bar())
            p.drawRoundedRect(fr, r.height() / 2, r.height() / 2)


class SmoothSlider(QWidget):
    """Schlichter, stufenlos gleitender Regler (kein Aufploppen, kein Einrasten beim Ziehen)."""
    valueChanged = Signal(float)
    PAD = 11

    def __init__(self, lo, hi, value, step=0.0, parent=None):
        super().__init__(parent)
        self.lo, self.hi, self.step = float(lo), float(hi), float(step)
        self._v = float(value)
        self.drag = False
        self.setFixedHeight(26)
        self.setCursor(Qt.PointingHandCursor)

    def value(self):
        return self._v

    def _set(self, v):
        v = max(self.lo, min(self.hi, v))
        if self.step > 0:
            v = round((v - self.lo) / self.step) * self.step + self.lo
        if v != self._v:
            self._v = v
            self.valueChanged.emit(v)
            self.update()

    def _from_x(self, x):
        t = (x - self.PAD) / max(1.0, self.width() - 2 * self.PAD)
        return self.lo + max(0.0, min(1.0, t)) * (self.hi - self.lo)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.drag = True
            self._set(self._from_x(e.position().x()))

    def mouseMoveEvent(self, e):
        if self.drag:
            self._set(self._from_x(e.position().x()))

    def mouseReleaseEvent(self, e):
        self.drag = False

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cy = self.height() / 2
        left, right = self.PAD, self.width() - self.PAD
        t = (self._v - self.lo) / max(1e-9, self.hi - self.lo)
        x = left + (right - left) * t
        p.setPen(Qt.NoPen)
        p.setBrush(ink(38))
        p.drawRoundedRect(QRectF(left, cy - 3, right - left, 6), 3, 3)
        g = QLinearGradient(left, 0, right, 0)
        g.setColorAt(0, with_alpha(T.pill()[0], 140))
        g.setColorAt(1, T.pill()[0])
        p.setBrush(g)
        p.drawRoundedRect(QRectF(left, cy - 3, max(6.0, x - left), 6), 3, 3)
        # Griff: weisser Kreis mit weichem Schatten, immer gleich gross
        p.setBrush(QColor(30, 25, 70, 26))
        p.drawEllipse(QPointF(x, cy + 1.5), 10.5, 10.5)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(x, cy), 9, 9)
        p.setBrush(T.pill()[1] if T.dark else T.acc)
        p.drawEllipse(QPointF(x, cy), 3.0, 3.0)


class InfoDot(QWidget):
    """Kleines "i" im Kreis; der Hilfetext erscheint beim Daruberfahren."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.setToolTip(text)
        self.setCursor(Qt.WhatsThisCursor)
        self.hov = 0.0

    def enterEvent(self, e):
        tween(self, "hov", 1.0, 120)

    def leaveEvent(self, e):
        tween(self, "hov", 0.0, 120)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QRectF(self.rect()).center()
        col = ink(120 + 90 * self.hov)
        p.setPen(QPen(col, 1.3))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, 7.2, 7.2)
        p.setPen(QPen(col, 1.6))
        p.setBrush(col)
        p.drawEllipse(QPointF(c.x(), c.y() - 3.0), 0.9, 0.9)
        p.drawLine(QPointF(c.x(), c.y() - 0.8), QPointF(c.x(), c.y() + 3.4))


class SliderRow(QWidget):
    """Titel links, Wert rechts, darunter der Regler ueber die volle Breite."""

    def __init__(self, title, lo, hi, steps, value, fmt, parent=None, hint=None):
        super().__init__(parent)
        self.fmt = fmt
        from PySide6.QtWidgets import QVBoxLayout
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel(title)
        self.title.setProperty("role", "muted")
        self.val = QLabel()
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.val.setFont(app_font(10, True))
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.val)
        # steps > 0: feines Raster (nur zur Anzeige), steps = 0: voellig stufenlos
        self.s = SmoothSlider(lo, hi, value, (hi - lo) / steps if steps > 0 else 0.0)
        lay.addLayout(top)
        lay.addWidget(self.s)
        if hint:
            h = QLabel(hint)
            h.setProperty("role", "dim")
            h.setWordWrap(True)
            h.setFont(app_font(9))
            lay.addWidget(h)
        self.s.valueChanged.connect(lambda _: self.val.setText(self.fmt(self.value())))
        self.val.setText(self.fmt(self.value()))

    def value(self):
        return self.s.value()


# ------------------------------------------------------------------ Uebergaenge
class FadeStack(QWidget):
    """Seitenwechsel mit weichem Einblenden. Die neue Seite wird live eingeblendet (kein Zwischenbild),
    dadurch gibt es kein Ruckeln und keine schwarzen Flaechen."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pages = []
        self.cur = -1
        self.eff = None
        self.incoming = None
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(260)
        self.anim.setEasingCurve(ease_out())
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.valueChanged.connect(self._tick)
        self.anim.finished.connect(self._finish)

    def add(self, w):
        w.setParent(self)
        w.hide()
        self.pages.append(w)
        return len(self.pages) - 1

    def resizeEvent(self, e):
        for w in self.pages:
            w.setGeometry(self.rect())

    def warm(self):
        """Zeigt jede Seite einmal unsichtbar, damit der erste echte Wechsel nicht ruckelt."""
        for i, w in enumerate(self.pages):
            if i == self.cur:
                continue
            eff = QGraphicsOpacityEffect(w)
            eff.setOpacity(0.0)
            w.setGraphicsEffect(eff)
            w.setGeometry(self.rect())
            w.show()
            QApplication.processEvents()
            w.hide()
            w.setGraphicsEffect(None)

    def go(self, i, direction=1):
        if i == self.cur:
            return
        self._finish()
        new = self.pages[i]
        new.setGeometry(self.rect())
        if self.cur < 0 or not self.isVisible():
            for k, w in enumerate(self.pages):
                w.setVisible(k == i)
            self.cur = i
            return
        self.pages[self.cur].hide()
        self.eff = QGraphicsOpacityEffect(new)
        self.eff.setOpacity(0.0)
        new.setGraphicsEffect(self.eff)
        self.incoming = new
        new.show()
        self.cur = i
        self.anim.stop()
        self.anim.start()

    def _tick(self, v):
        if self.eff is not None:
            self.eff.setOpacity(min(1.0, float(v) * 1.25))
        if self.incoming is not None:
            self.incoming.move(0, int((1 - float(v)) * 16))

    def _finish(self):
        self.anim.stop()
        if self.incoming is not None:
            self.incoming.setGraphicsEffect(None)       # Effekt entfernen: danach wird normal (schnell) gezeichnet
            self.incoming.move(0, 0)
        self.incoming = None
        self.eff = None


class Overlay(QWidget):
    """Abgedunkelter Hintergrund mit einer Glas-Karte in der Mitte (Dialoge)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.card = None
        self.k = 0.0
        self.hide()
        self.anim = QVariantAnimation(self)
        self.anim.setEasingCurve(ease_out())
        self.anim.valueChanged.connect(self._tick)
        self.anim.finished.connect(self._finished)
        self.closing = False
        self.opaque = False
        self.eff = None

    def present(self, card, opaque=False):
        self.opaque = opaque
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
        self.anim.setDuration(210)
        self.anim.setStartValue(self.k)
        self.anim.setEndValue(1.0)
        self.anim.start()

    def dismiss(self):
        if self.card is None or self.closing:
            return
        self.closing = True
        self.anim.stop()
        self.anim.setDuration(130)                    # Abgang schneller als Auftritt
        self.anim.setStartValue(self.k)
        self.anim.setEndValue(0.0)
        self.anim.start()

    def _center(self):
        if self.card:
            c = self.card
            off = int((1 - self.k) * 10)
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
        c = QColor(T.scrim)
        c.setAlpha(int(c.alpha() * self.k * 1.0))
        if self.opaque:                                   # ganzes Fenster zudecken (Anmeldung, Pflicht-Update)
            p.setRenderHint(QPainter.Antialiasing)
            p.setClipPath(self.parent().clip_path())
            bg = Background.instance
            p.setOpacity(min(1.0, self.k * 1.4))
            if bg is not None and bg._pm is not None:     # der normale App-Hintergrund mit seinem Verlauf
                p.drawPixmap(self.rect(), bg._pm)
            else:
                p.fillRect(self.rect(), QColor(0, 0, 0) if T.dark else QColor(225, 226, 230))
            return
        p.setRenderHint(QPainter.Antialiasing)
        p.setClipPath(self.parent().clip_path())
        p.fillRect(self.rect(), c)


class Toast(Glass):
    def __init__(self, parent):
        super().__init__(parent, radius=20, alpha=190, dialog=True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 11, 22, 11)
        self.lbl = QLabel()
        self.lbl.setFont(app_font(10.5, True))
        lay.addWidget(self.lbl)
        self.dot = QColor(T.acc)
        self.eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.eff)
        self.k = 0.0
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(230)
        self.anim.setEasingCurve(ease_out())
        self.anim.valueChanged.connect(self._tick)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._hide)
        self.hide()

    def show_text(self, text, color=None):
        self.lbl.setText(text)
        self.dot = QColor(color if color is not None else T.acc)
        self.adjustSize()
        self.k = max(self.k, 0.0)
        self.show()
        self.raise_()
        self.anim.stop()
        self.anim.setDuration(230)
        self.anim.setStartValue(self.k)
        self.anim.setEndValue(1.0)
        self.anim.start()
        self.timer.start(3200)

    def _hide(self):
        self.anim.stop()
        self.anim.setDuration(140)
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
        self.move((pw - self.width()) // 2, int(ph - self.height() - 28 + (1 - self.k) * 16))

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.dot)
        p.drawRoundedRect(QRectF(0, 12, 4, self.height() - 24), 2, 2)


# ------------------------------------------------------------------ Zeitleiste
class SeekBar(QWidget):
    """Duenne, anklickbare Fortschrittslinie ueber die ganze Videolaenge."""
    seek = Signal(float)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.m = model
        self.hov = 0.0
        self.setFixedHeight(16)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def enterEvent(self, e):
        tween(self, "hov", 1.0, 140)

    def leaveEvent(self, e):
        tween(self, "hov", 0.0, 140)

    def _t(self, x):
        return max(0.0, min(1.0, x / max(1, self.width()))) * self.m.duration

    def mousePressEvent(self, e):
        if self.m.has_video:
            self.seek.emit(self._t(e.position().x()))

    def mouseMoveEvent(self, e):
        if self.m.has_video and e.buttons() & Qt.LeftButton:
            self.seek.emit(self._t(e.position().x()))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = 4 + 2 * self.hov
        r = QRectF(0, (self.height() - h) / 2, self.width(), h)
        p.setPen(Qt.NoPen)
        p.setBrush(ink(24))
        p.drawRoundedRect(r, h / 2, h / 2)
        d = max(1.0, self.m.duration)
        x = self.width() * max(0.0, min(1.0, self.m.pos / d)) if self.m.has_video else 0.0
        if x > 0:
            g = QLinearGradient(r.topLeft(), r.topRight())
            g.setColorAt(0, T.acc)
            g.setColorAt(1, T.acc2)
            p.setBrush(g)
            p.drawRoundedRect(QRectF(r.left(), r.top(), max(h, x), h), h / 2, h / 2)
            if self.hov > 0.05:
                p.setBrush(QColor("#ffffff"))
                p.setPen(QPen(T.acc, 1.5))
                p.drawEllipse(QPointF(x, self.height() / 2), 5.5 * self.hov, 5.5 * self.hov)


class Timeline(QWidget):
    """Ueberblick ueber das ganze Video + zoombare Detailansicht mit ziehbaren Clip-Raendern."""
    seek = Signal(float)
    pick = Signal(int)
    edited = Signal(int)
    begin_edit = Signal()
    PAD, H_OV = 14, 34

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.m = model
        self.v0, self.v1 = 0.0, 60.0
        self.mode = None
        self.setMinimumHeight(128)
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
            p.setPen(ink(120))
            p.setFont(app_font(10))
            p.drawText(self.rect(), Qt.AlignCenter, "Öffne ein Video, um die Zeitleiste zu sehen")
            return
        # Ueberblick
        p.setPen(Qt.NoPen)
        p.setBrush(ink(22))
        p.drawRoundedRect(QRectF(P, 8, w - 2 * P, 18), 9, 9)
        for i, c in enumerate(m.clips):
            sel = i == m.sel
            x0 = self.ox(c.start)
            p.setBrush(with_alpha(T.acc, 235) if sel else with_alpha(T.acc, 90))
            p.drawRoundedRect(QRectF(x0, 8, max(self.ox(c.end) - x0, 4), 18), 4, 4)
            p.setBrush(RED)
            p.drawRect(QRectF(self.ox(c.kill) - 1, 8, 2, 18))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(ink(150), 1.6))
        p.drawRoundedRect(QRectF(self.ox(self.v0), 6, max(6, self.ox(self.v1) - self.ox(self.v0)), 22), 6, 6)
        p.setPen(QPen(ink(235), 2))
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
                p.setPen(ink(45))
                p.drawLine(QPointF(x, top + 13), QPointF(x, top + 21))
                p.setPen(ink(135))
                txt = f"{int(t // 60)}:{int(t % 60):02d}" if step >= 1 else f"{t:.1f}s"
                p.drawText(QRectF(x - 30, top - 6, 60, 14), Qt.AlignCenter, txt)
            t += step
        # Spur
        y0, y1 = top + 28, top + 28 + 62
        p.setPen(QPen(ink(34), 1))
        p.setBrush(ink(14))
        p.drawRoundedRect(QRectF(P, y0, w - 2 * P, y1 - y0), 12, 12)
        p.setClipRect(QRectF(P, y0, w - 2 * P, y1 - y0))
        for i, c in enumerate(m.clips):
            if c.end < self.v0 or c.start > self.v1:
                continue
            sel = i == m.sel
            xa, xb = self.x_of(c.start), self.x_of(c.end)
            rr = QRectF(xa, y0 + 6, xb - xa, y1 - y0 - 12)
            p.setBrush(with_alpha(T.acc, 60) if sel else ink(12))
            p.setPen(QPen(T.acc if sel else ink(55), 1.6 if sel else 1))
            p.drawRoundedRect(rr, 10, 10)
            p.setPen(ink(235) if sel else ink(150))
            p.setFont(app_font(9, True))
            p.drawText(rr, Qt.AlignCenter, f"KILL {i + 1:02d}  ·  {c.end - c.start:.1f}s")
            if sel:
                p.setPen(Qt.NoPen)
                p.setBrush(T.acc)
                for x in (xa, xb):
                    p.drawRoundedRect(QRectF(x - 4, y0 + 14, 8, y1 - y0 - 28), 4, 4)
            k = self.x_of(c.kill)
            p.setPen(Qt.NoPen)
            p.setBrush(RED)
            p.drawPolygon(QPolygonF([QPointF(k - 6, y0 + 1), QPointF(k + 6, y0 + 1), QPointF(k, y0 + 12)]))
        for attr, txt in (("mark_in", "Start"), ("mark_out", "Ende")):
            tm = getattr(m, attr, None)
            if tm is not None and self.v0 <= tm <= self.v1:
                mx = self.x_of(tm)
                p.setPen(QPen(T.acc, 2))
                p.drawLine(QPointF(mx, y0 + 2), QPointF(mx, y1 - 2))
                p.setPen(Qt.NoPen)
                p.setBrush(T.acc)
                fr = QRectF(mx - 24, y1 - 22, 48, 16)
                p.drawRoundedRect(fr, 8, 8)
                p.setPen(T.on_acc)
                p.setFont(app_font(8, True))
                p.drawText(fr, Qt.AlignCenter, txt)
        p.setClipping(False)
        px = self.x_of(m.pos)
        if P <= px <= w - P:
            p.setPen(QPen(ink(235), 2))
            p.drawLine(QPointF(px, top + 20), QPointF(px, y1))
            p.setPen(Qt.NoPen)
            p.setBrush(ink(235))
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
            self.begin_edit.emit()                   # fuer Strg+Z: Zustand vor dem Ziehen merken
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

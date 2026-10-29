"""Kill App - Farbsystem: heller/dunkler Modus und frei waehlbare Akzent-/Hintergrundfarben."""
from PySide6.QtGui import QColor

RED = QColor("#f0506e")
AMBER = QColor("#e8a23a")

PRESETS = {
    "Flieder": {"acc": "#8c79ee", "acc2": "#b49cf8", "bg": "#8c79ee"},
    "Ozean": {"acc": "#3b8df0", "acc2": "#6fd0f5", "bg": "#3b8df0"},
    "Mint": {"acc": "#1fb98c", "acc2": "#6fe3c0", "bg": "#2fb98a"},
    "Sunset": {"acc": "#f0703c", "acc2": "#f7b267", "bg": "#f06a8c"},
    "Rosé": {"acc": "#e8589a", "acc2": "#f7a1c8", "bg": "#e8589a"},
    "Gold": {"acc": "#d99a1e", "acc2": "#f5d06f", "bg": "#e0a03a"},
}
DEFAULT = PRESETS["Flieder"]


def _hsl(c):
    c = QColor(c).toHsl()
    h = c.hslHueF()
    return (0.0 if h < 0 else h), c.hslSaturationF(), c.lightnessF()


def hsl(h, s, l, a=1.0):
    return QColor.fromHslF(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, l)), a)


def lum(c):
    c = QColor(c)
    return 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()


def rgba(c):
    c = QColor(c)
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


class Theme:
    def __init__(self):
        self.configure(False, DEFAULT["acc"], DEFAULT["acc2"], DEFAULT["bg"])

    def configure(self, dark, acc, acc2, bg):
        self.dark = bool(dark)
        self.acc, self.acc2, self.bg = QColor(acc), QColor(acc2), QColor(bg)
        h, s, l = _hsl(self.acc)
        # Akzentfarbe fuer Text: auf hellem Glas dunkler, auf dunklem heller (immer gut lesbar)
        self.acc_text = hsl(h, max(s, 0.45), 0.42) if not self.dark else hsl(h, max(s, 0.55), 0.76)
        # Schrift auf Akzent-Flaechen: weiss, bei sehr hellen Akzenten dunkel
        self.on_acc = QColor("#1c1838") if (lum(self.acc) + lum(self.acc2)) / 2 > 0.66 else QColor("#ffffff")
        self.icon = hsl(h, 0.28, 0.46, 0.9) if not self.dark else hsl(h, 0.35, 0.80, 0.9)
        # Textfarben
        self.text = QColor(236, 237, 252) if self.dark else QColor(42, 40, 72)
        self.k_white = 0.2 if self.dark else 1.0           # Glas-Weiss ist im Dunkeln viel schwaecher
        self.err = QColor("#ff8a9b") if self.dark else QColor("#d6334f")
        self.okc = QColor("#5fe3b5") if self.dark else QColor("#1f9d73")
        self.warn = QColor("#ffc15a") if self.dark else QColor("#c4801f")
        self.busy = QColor("#8fb0ff") if self.dark else QColor("#4a7be0")
        self.dialog_fill = QColor(30, 27, 62, 248) if self.dark else QColor(255, 255, 255, 238)
        self.scrim = QColor(0, 0, 0, 150) if self.dark else hsl(h, 0.35, 0.28, 0.37)
        self._bg_cache = None

    # ---- Farben mit Alpha
    def ink(self, a=255):
        c = QColor(self.text)
        c.setAlpha(int(max(0, min(255, a))))
        return c

    def white(self, a):
        return QColor(255, 255, 255, int(max(0, min(255, a * self.k_white))))

    def sel_fill(self, k=1.0):
        """Hervorgehobene Flaeche (ausgewaehlter Eintrag)."""
        if self.dark:
            c = QColor(self.acc)
            c.setAlpha(int(90 * k))
            return c
        return QColor(255, 255, 255, int(235 * k))

    def toggle_off(self):
        return QColor(255, 255, 255, 70) if self.dark else QColor(196, 194, 216)

    def role(self, name):
        """Farbe fuer eine Textrolle (wird auch fuer das QSS benutzt)."""
        t = {
            "text": self.text,
            "muted": self.ink(150),
            "dim": self.ink(105),
            "acc": self.acc_text,
            "err": self.err,
            "ok": self.okc,
            "bad": self.err,
            "warn": self.warn,
            "busy": self.busy,
            "onvideo": QColor(255, 255, 255, 240),
            "onvideo2": QColor(255, 255, 255, 190),
        }
        return t.get(name, self.text)

    # ---- Hintergrund
    def bg_stops(self):
        h, s, l = _hsl(self.bg)
        if self.dark:
            return (hsl(h, 0.55, 0.075), hsl(h + 0.04, 0.55, 0.115), hsl(h - 0.05, 0.50, 0.085))
        return (hsl(h - 0.03, 0.55, 0.86), hsl(h + 0.05, 0.60, 0.90), hsl(h - 0.08, 0.55, 0.87))

    def bg_blobs(self):
        """(x, y, radius, Farbe, Alpha) - weiche Farbflecken."""
        h, s, l = _hsl(self.bg)
        a2 = _hsl(self.acc2)
        a1 = _hsl(self.acc)
        if self.dark:
            return [(0.06, 0.98, 0.55, hsl(a2[0], 0.75, 0.50), 80), (0.95, 0.04, 0.60, hsl(a1[0], 0.8, 0.52), 95),
                    (0.80, 0.98, 0.45, hsl(h + 0.08, 0.7, 0.45), 70), (0.28, 0.12, 0.40, hsl(h - 0.06, 0.7, 0.50), 55)]
        return [(0.05, 0.98, 0.55, hsl(a2[0] + 0.02, 0.85, 0.80), 150), (0.95, 0.04, 0.60, hsl(a1[0], 0.85, 0.74), 150),
                (0.80, 0.98, 0.45, hsl(h + 0.10, 0.85, 0.80), 140), (0.28, 0.12, 0.40, hsl(h - 0.06, 0.80, 0.78), 110)]

    # ---- Globales Stylesheet
    def qss(self):
        T = self
        r = {n: rgba(T.role(n)) for n in ("text", "muted", "dim", "acc", "err", "ok", "bad", "warn", "busy",
                                          "onvideo", "onvideo2")}
        field = "rgba(255,255,255,24)" if T.dark else "rgba(255,255,255,170)"
        field_f = "rgba(255,255,255,38)" if T.dark else "rgba(255,255,255,225)"
        acc = T.acc.name()
        acc_soft = rgba(self.alpha_of(T.acc, 90 if T.dark else 50))
        return f"""
QLabel {{ color: {r['text']}; background: transparent; }}
QLabel[role="muted"] {{ color: {r['muted']}; }}
QLabel[role="dim"] {{ color: {r['dim']}; }}
QLabel[role="acc"] {{ color: {r['acc']}; }}
QLabel[role="err"] {{ color: {r['err']}; }}
QLabel[role="onvideo"] {{ color: {r['onvideo']}; }}
QLabel[role="onvideo2"] {{ color: {r['onvideo2']}; }}
QLabel[role="badge"] {{ background: {acc_soft}; color: {r['acc']}; border-radius: 10px; padding: 2px 10px; }}
QLabel[role="thumbbg"] {{ background: {rgba(T.ink(40))}; border-radius: 12px; }}
QLabel[role="ctaicon"] {{ background: rgba(255,255,255,40); border-radius: 42px; }}
QLineEdit {{ background: {field}; border: 1px solid {rgba(self.alpha_of(T.acc, 90))}; border-radius: 13px;
            padding: 9px 14px; color: {r['text']}; selection-background-color: {acc}; selection-color: white; }}
QLineEdit:focus {{ border: 1px solid {acc}; background: {field_f}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollArea > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {rgba(T.ink(55))}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {rgba(T.ink(100))}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QSlider::groove:horizontal {{ height: 6px; background: {rgba(T.ink(40))}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {acc}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: white; border: 2px solid {acc}; width: 14px; height: 14px; margin: -7px 0;
                              border-radius: 9px; }}
QSlider::handle:horizontal:hover {{ width: 18px; height: 18px; margin: -9px 0; border-radius: 11px; }}
QToolTip {{ background: {'#262244' if T.dark else 'white'}; color: {r['text']}; border: 1px solid {rgba(self.alpha_of(T.acc, 120))};
           padding: 4px 8px; }}
"""

    @staticmethod
    def alpha_of(c, a):
        c = QColor(c)
        c.setAlpha(int(a))
        return c


T = Theme()


def white(a):
    return T.white(a)


def ink(a=255):
    return T.ink(a)


def with_alpha(c, a):
    c = QColor(c)
    c.setAlpha(int(max(0, min(255, a))))
    return c

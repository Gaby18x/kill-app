"""ACE - Farbsystem: heller/dunkler Modus und frei waehlbare Akzent-/Hintergrundfarben."""
from PySide6.QtGui import QColor

RED = QColor("#f0506e")
AMBER = QColor("#e8a23a")

PRESETS = {
    "Grau": {"acc": "#5f6575", "acc2": "#8b91a3", "bg": "#8b909c"},
    "Lime": {"acc": "#c5e15a", "acc2": "#e6f7a0", "bg": "#8a9a4a"},
    "Flieder": {"acc": "#8c79ee", "acc2": "#b49cf8", "bg": "#8c79ee"},
    "Ozean": {"acc": "#3b8df0", "acc2": "#6fd0f5", "bg": "#3b8df0"},
    "Mint": {"acc": "#1fb98c", "acc2": "#6fe3c0", "bg": "#2fb98a"},
    "Sunset": {"acc": "#f0703c", "acc2": "#f7b267", "bg": "#f06a8c"},
    "Rosé": {"acc": "#e8589a", "acc2": "#f7a1c8", "bg": "#e8589a"},
    "Gold": {"acc": "#d99a1e", "acc2": "#f5d06f", "bg": "#e0a03a"},
}
DEFAULT = PRESETS["Grau"]


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
        # wie bunt ist das Farbthema? (Grau-Thema: kaum Saettigung -> alles bleibt neutral)
        self.sk_acc = min(1.0, s / 0.5)
        self.sk_bg = min(1.0, _hsl(self.bg)[1] / 0.45)
        # Akzentfarbe fuer Text: auf hellem Glas dunkler, auf dunklem heller (immer gut lesbar)
        self.acc_text = (hsl(h, max(s, 0.45), 0.42) if not self.dark else hsl(h, max(s, 0.55), 0.76)) if s > 0.25 else \
            (hsl(h, s, 0.36) if not self.dark else hsl(h, s, 0.80))
        # Schrift auf Akzent-Flaechen: weiss, bei sehr hellen Akzenten dunkel
        self.on_acc = QColor("#1c1838") if (lum(self.acc) + lum(self.acc2)) / 2 > 0.66 else QColor("#ffffff")
        self.icon = hsl(h, 0.28 * self.sk_acc, 0.46, 0.9) if not self.dark else hsl(h, 0.35 * self.sk_acc, 0.80, 0.9)
        # Textfarben
        self.text = QColor(246, 243, 236) if self.dark else QColor(17, 17, 17)
        self.k_white = 0.2 if self.dark else 1.0           # Glas-Weiss ist im Dunkeln viel schwaecher
        self.err = QColor("#ff8a9b") if self.dark else QColor("#d6334f")
        self.okc = QColor("#5fe3b5") if self.dark else QColor("#1f9d73")
        self.warn = QColor("#ffc15a") if self.dark else QColor("#c4801f")
        self.busy = QColor("#8fb0ff") if self.dark else QColor("#4a7be0")
        self.dialog_fill = hsl(h, 0.32 * self.sk_acc, 0.14, 0.97) if self.dark else QColor(255, 255, 255, 238)
        self.scrim = QColor(0, 0, 0, 150) if self.dark else hsl(h, 0.35 * self.sk_acc, 0.28, 0.37)
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

    # ---- Kartenstil (flach, ruhig, 1-px-Rand)
    def card_fill(self, a=120):
        """Fuellfarbe einer Karte; a ist die alte Glas-Staerke (110..170) und steuert nur die Helligkeit leicht."""
        k = max(0.0, min(1.0, (a - 90) / 120.0))
        if self.dark:
            return QColor(23, 23, 22, int(215 + 35 * k))          # #171716 - leicht abgesetzte Flaeche
        return QColor(245, 247, 250, int(220 + 35 * k))

    def line(self, k=1.0):
        """Feine Randfarbe (heller Strich im Dunkeln, dunkler im Hellen)."""
        if self.dark:
            return QColor(255, 248, 230, int(min(255, 20 * k)))
        return QColor(17, 24, 39, int(min(255, 20 * k)))

    def tab_fill(self, a=1.0):
        """Hinterlegung des aktiven Reiters (dezent, nicht bunt)."""
        if self.dark:
            return QColor(255, 255, 255, int(20 * a))
        return QColor(30, 30, 50, int(16 * a))

    def pill(self):
        """(Fuellung, Schriftfarbe) fuer die hervorgehobene Pille (aktiver Reiter, Haupt-Knopf)."""
        h, s, l = _hsl(self.acc)
        if self.dark:
            return hsl(h, min(s, 0.75) * 0.95, 0.86 if s < 0.25 else 0.77), QColor("#14161a")
        return hsl(h, min(s, 0.45), 0.20), QColor("#ffffff")

    def acc_bar(self):
        return QColor(self.acc_text) if self.dark else QColor(self.acc)

    def role(self, name):
        """Farbe fuer eine Textrolle (wird auch fuer das QSS benutzt)."""
        t = {
            "text": self.text,
            "muted": self.ink(178 if not self.dark else 170),     # >= 4,5:1 Kontrast (gut lesbar)
            "dim": self.ink(150 if not self.dark else 128),
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

    def video_stops(self):
        """Hintergrund der Vorschau-Flaeche (leer/Letterbox): dunkle Variante der Akzentfarbe."""
        h, s, l = _hsl(self.acc)
        return hsl(h, 0.40 * self.sk_acc, 0.26, 0.94), hsl(h + 0.03, 0.46 * self.sk_acc, 0.12, 0.96)

    # ---- Hintergrund
    def _aurora_sat(self):
        return 0.32 + 0.45 * self.sk_acc

    def bg_stops(self):
        h, s, l = _hsl(self.bg)
        k = 0.06 + 0.22 * self.sk_bg
        if self.dark:
            return (hsl(h + 0.02, k, 0.065), hsl(h, k, 0.045), hsl(h - 0.03, k, 0.07))
        return (hsl(h + 0.02, k * 0.9, 0.93), hsl(h, k * 0.8, 0.955), hsl(h - 0.03, k * 0.9, 0.925))

    def bg_blobs(self):
        """(x, y, radius, Farbe, Alpha) - weiche Aurora-Lichter hinter dem Glas."""
        ha = _hsl(self.acc)[0]
        h2 = _hsl(self.acc2)[0]
        hb = _hsl(self.bg)[0]
        s = self._aurora_sat()
        if self.dark:
            return [(0.10, 0.08, 0.55, hsl(ha, s, 0.46), 150), (0.90, 0.22, 0.48, hsl(h2 + 0.05, s, 0.50), 120),
                    (0.58, 1.00, 0.62, hsl(hb - 0.08, s * 0.9, 0.40), 140), (0.36, 0.55, 0.30, hsl(ha + 0.04, s, 0.55), 55)]
        return [(0.10, 0.08, 0.55, hsl(ha, s, 0.78), 170), (0.90, 0.22, 0.48, hsl(h2 + 0.05, s, 0.80), 150),
                (0.58, 1.00, 0.62, hsl(hb - 0.08, s * 0.9, 0.76), 160), (0.36, 0.55, 0.30, hsl(ha + 0.04, s, 0.82), 80)]

    def arc_color(self):
        return QColor(255, 255, 255) if self.dark else hsl(_hsl(self.acc)[0], 0.3, 0.35)

    def grain_alpha(self):
        return 0.45 if self.dark else 0.30

    def glass_veil(self, k=1.0):
        """Kartenflaeche (kein Glas mehr): ruhiges, fast deckendes Dunkel bzw. Weiss."""
        if self.dark:
            return QColor(17, 17, 17, int(max(0, min(255, 200 + 30 * k))))
        return QColor(255, 255, 255, int(max(0, min(255, 215 + 25 * k))))

    def glass_edge(self, k=1.0):
        """Kartenrand (#2b2a25 im Dunkeln, #e3e7ec im Hellen)."""
        if self.dark:
            return QColor(43, 42, 37, int(max(0, min(255, 255 * min(1.0, 0.6 + 0.4 * k)))))
        return QColor(227, 231, 236, int(max(0, min(255, 255 * min(1.0, 0.6 + 0.4 * k)))))

    def glow(self):
        """Farbe des Lichts am unteren Rand: folgt dem Farbthema, bewusst gedaempft (angenehm fuer die Augen)."""
        h, s, l = _hsl(self.acc)
        if s < 0.25:                                    # Grau: neutrales, ruhiges Licht
            return hsl(h, 0.10, 0.20) if self.dark else hsl(h, 0.10, 0.86)
        return hsl(h, min(s, 0.6) * 0.62, 0.24) if self.dark else hsl(h, min(s, 0.6) * 0.55, 0.88)

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
QLineEdit {{ background: {rgba(T.card_fill(150))}; border: 1px solid {rgba(T.glass_edge(1.0))}; border-radius: 12px;
            padding: 9px 14px; color: {r['text']}; selection-background-color: {acc}; selection-color: white; }}
QLineEdit:focus {{ border: 1px solid {rgba(T.ink(150))}; }}
QLineEdit[role="underline"] {{ background: transparent; border: none; border-bottom: 2px solid rgba(255,255,255,150);
            border-radius: 0px; padding: 8px 4px 8px 4px; color: white; font-size: 11pt; }}
QLineEdit[role="underline"]:focus {{ border-bottom: 2px solid rgba(255,255,255,240); }}
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
QToolTip {{ background: {T.dialog_fill.name() if T.dark else 'white'}; color: {r['text']}; border: 1px solid {rgba(self.alpha_of(T.acc, 120))};
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

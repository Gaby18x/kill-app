"""KillClip - schneidet automatisch Kill-Clips aus GTA/FiveM-Aufnahmen.

Erkennt den roten Kill-Hitmarker in der Bildmitte, legt pro Kill einen Clip
(Standard: 4 s davor, 2 s danach) an und exportiert mit FFmpeg.
"""
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

# ----------------------------------------------------------------- Kern ----


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def red_pixels(roi, diff):
    """Anzahl stark roter Pixel (R deutlich ueber G und B)."""
    a = roi.astype(np.int16)
    b, g, r = a[..., 0], a[..., 1], a[..., 2]
    return int(((r > 140) & (r - np.maximum(g, b) > diff)).sum())


def roi_of(frame, h):
    half = max(20, int(60 * h / 1080))
    hh, ww = frame.shape[:2]
    cy, cx = hh // 2, ww // 2
    return frame[cy - half:cy + half, cx - half:cx + half], half


def scan_video(path, pre, post, diff=60, min_px=15, max_px=700, step=2,
               merge_gap=2.0, progress=None, cancel=None):
    """Gibt Liste von (start, ende) in Sekunden zurueck."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("Video konnte nicht geoeffnet werden.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    scale = (h / 1080.0) ** 2
    lo, hi = max(3, int(min_px * scale)), int(max_px * scale)
    hits, i = [], 0
    while True:
        if cancel is not None and cancel.is_set():
            break
        if not cap.grab():
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                roi, _ = roi_of(frame, h)
                n = red_pixels(roi, diff)
                if lo <= n <= hi:
                    hits.append(i / fps)
        i += 1
        if progress and i % 300 == 0:
            progress(min(1.0, i / total))
    cap.release()
    # Treffer zu Kills zusammenfassen
    kills, last = [], None
    for t in hits:
        if last is None or t - last > merge_gap:
            kills.append(t)
        last = t
    duration = total / fps
    clips = []
    for t in kills:
        clips.append((max(0.0, t - pre), min(duration, t + post)))
    return clips


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

MODES = {
    "Verlustfrei (1:1, schnell, Schnitt am Keyframe)": "copy",
    "Bildgenau (neu kodiert, CRF 12, ~verlustfrei)": "exact",
    "Kleiner (H.265, CRF 18, optisch gleich)": "h265",
}


def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KillClip")
        self.geometry("1200x760")
        self.ffmpeg = find_ffmpeg()
        self.path = None
        self.cap = None
        self.fps = 60.0
        self.duration = 0.0
        self.clips = []          # Liste [start, end]
        self.cancel = threading.Event()
        self.playing = False
        self.pos = 0.0
        self._img = None
        self._build()

    # ---- UI aufbauen
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Button(top, text="Video öffnen…", command=self.open_video).pack(side="left")
        ttk.Label(top, text="  Davor (s)").pack(side="left")
        self.pre = tk.DoubleVar(value=4.0)
        ttk.Spinbox(top, from_=0, to=30, increment=0.5, width=5, textvariable=self.pre).pack(side="left")
        ttk.Label(top, text=" Danach (s)").pack(side="left")
        self.post = tk.DoubleVar(value=2.0)
        ttk.Spinbox(top, from_=0, to=30, increment=0.5, width=5, textvariable=self.post).pack(side="left")
        ttk.Label(top, text=" Empfindlichkeit").pack(side="left")
        self.diff = tk.IntVar(value=60)
        ttk.Spinbox(top, from_=20, to=150, increment=5, width=5, textvariable=self.diff).pack(side="left")
        self.scan_btn = ttk.Button(top, text="Kills suchen", command=self.start_scan)
        self.scan_btn.pack(side="left", padx=8)
        self.prog = ttk.Progressbar(top, length=200)
        self.prog.pack(side="left")
        self.status = ttk.Label(top, text="")
        self.status.pack(side="left", padx=8)

        main = ttk.Frame(self)
        main.pack(fill="both", expand=True, padx=8)
        left = ttk.Frame(main)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Clips").pack(anchor="w")
        self.lb = tk.Listbox(left, width=28, exportselection=False)
        self.lb.pack(fill="y", expand=True)
        self.lb.bind("<<ListboxSelect>>", self.on_select)
        ttk.Button(left, text="Clip an aktueller Position", command=self.add_clip).pack(fill="x", pady=2)
        ttk.Button(left, text="Clip löschen", command=self.del_clip).pack(fill="x")

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=8)
        self.canvas = tk.Label(right, bg="black")
        self.canvas.pack(fill="both", expand=True)
        self.slider = ttk.Scale(right, from_=0, to=1, command=self.on_slider)
        self.slider.pack(fill="x", pady=4)
        ctl = ttk.Frame(right)
        ctl.pack(fill="x")
        ttk.Button(ctl, text="▶ / ❚❚", command=self.toggle_play).pack(side="left")
        ttk.Button(ctl, text="◀ 1s", command=lambda: self.seek(self.pos - 1)).pack(side="left", padx=2)
        ttk.Button(ctl, text="1s ▶", command=lambda: self.seek(self.pos + 1)).pack(side="left")
        self.timelbl = ttk.Label(ctl, text="00:00.00")
        self.timelbl.pack(side="left", padx=10)
        ttk.Button(ctl, text="Start hier setzen", command=lambda: self.set_edge(0)).pack(side="left", padx=2)
        ttk.Button(ctl, text="Ende hier setzen", command=lambda: self.set_edge(1)).pack(side="left")
        self.cliplbl = ttk.Label(ctl, text="")
        self.cliplbl.pack(side="left", padx=10)

        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=8)
        ttk.Label(bot, text="Export:").pack(side="left")
        self.mode = ttk.Combobox(bot, values=list(MODES), state="readonly", width=48)
        self.mode.current(0)
        self.mode.pack(side="left", padx=4)
        ttk.Button(bot, text="Alle exportieren", command=lambda: self.export(False)).pack(side="left", padx=4)
        ttk.Button(bot, text="Ausgewählten exportieren", command=lambda: self.export(True)).pack(side="left")

        if not self.ffmpeg:
            messagebox.showwarning("FFmpeg fehlt", "FFmpeg nicht gefunden. Installiere es mit:\n"
                                   "pip install imageio-ffmpeg\noder lege ffmpeg.exe in den PATH.")

    # ---- Video
    def open_video(self):
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.flv *.ts"), ("Alle", "*.*")])
        if not p:
            return
        self.playing = False
        if self.cap:
            self.cap.release()
        self.cap = cv2.VideoCapture(p)
        if not self.cap.isOpened():
            messagebox.showerror("Fehler", "Video konnte nicht geöffnet werden.")
            return
        self.path = p
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 60.0
        self.duration = (self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1) / self.fps
        self.clips.clear()
        self.refresh_list()
        self.slider.configure(to=self.duration)
        self.seek(0)
        self.status.config(text=os.path.basename(p))

    def show_frame(self):
        ok, f = self.cap.read()
        if not ok:
            return False
        w = max(200, self.canvas.winfo_width())
        h = max(120, self.canvas.winfo_height())
        fh, fw = f.shape[:2]
        s = min(w / fw, h / fh)
        f = cv2.resize(f, (int(fw * s), int(fh * s)))
        self._img = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)))
        self.canvas.configure(image=self._img)
        self.pos = self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        self.timelbl.config(text=fmt(self.pos))
        return True

    def seek(self, t):
        if not self.cap:
            return
        t = min(max(0, t), max(0, self.duration - 0.1))
        self.cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        self.show_frame()
        self.slider.set(self.pos)

    def on_slider(self, v):
        if self.cap and not self.playing and abs(float(v) - self.pos) > 0.05:
            self.seek(float(v))

    def toggle_play(self):
        if not self.cap:
            return
        self.playing = not self.playing
        if self.playing:
            self.play_loop()

    def play_loop(self):
        if not self.playing:
            return
        if not self.show_frame():
            self.playing = False
            return
        self.slider.set(self.pos)
        sel = self.selected()
        if sel is not None and self.pos >= self.clips[sel][1]:
            self.playing = False
            return
        self.after(int(1000 / self.fps), self.play_loop)

    # ---- Scan
    def start_scan(self):
        if not self.path:
            return
        self.scan_btn.config(state="disabled")
        self.cancel.clear()
        args = (self.path, self.pre.get(), self.post.get(), self.diff.get())

        def work():
            try:
                clips = scan_video(*args, progress=lambda p: self.after(0, self.prog.config, {"value": p * 100}),
                                   cancel=self.cancel)
                self.after(0, self.scan_done, clips)
            except Exception as e:
                self.after(0, messagebox.showerror, "Fehler", str(e))
                self.after(0, self.scan_btn.config, {"state": "normal"})

        threading.Thread(target=work, daemon=True).start()
        self.status.config(text="Suche läuft…")

    def scan_done(self, clips):
        self.clips = [list(c) for c in clips]
        self.refresh_list()
        self.prog.config(value=100)
        self.scan_btn.config(state="normal")
        self.status.config(text=f"{len(clips)} Kills gefunden")
        if self.clips:
            self.lb.selection_set(0)
            self.on_select()

    # ---- Clipverwaltung
    def refresh_list(self):
        self.lb.delete(0, "end")
        for n, (a, b) in enumerate(self.clips, 1):
            self.lb.insert("end", f"{n:02d}  {fmt(a)} – {fmt(b)}  ({b - a:.1f}s)")

    def selected(self):
        s = self.lb.curselection()
        return s[0] if s else None

    def on_select(self, *_):
        i = self.selected()
        if i is None:
            return
        self.seek(self.clips[i][0])
        self.cliplbl.config(text=f"Clip {i + 1}: {fmt(self.clips[i][0])} – {fmt(self.clips[i][1])}")

    def add_clip(self):
        if not self.cap:
            return
        a = max(0, self.pos - self.pre.get())
        b = min(self.duration, self.pos + self.post.get())
        self.clips.append([a, b])
        self.clips.sort()
        self.refresh_list()

    def del_clip(self):
        i = self.selected()
        if i is not None:
            del self.clips[i]
            self.refresh_list()

    def set_edge(self, which):
        i = self.selected()
        if i is None:
            return
        self.clips[i][which] = self.pos
        if self.clips[i][0] >= self.clips[i][1]:
            self.clips[i][1 - which] = self.pos + (1 if which == 0 else -1)
        self.refresh_list()
        self.lb.selection_set(i)
        self.on_select()

    # ---- Export
    def export(self, only_selected):
        if not self.path or not self.clips:
            return
        if not self.ffmpeg:
            messagebox.showerror("FFmpeg fehlt", "Kein FFmpeg gefunden.")
            return
        idx = [self.selected()] if only_selected else range(len(self.clips))
        if idx == [None]:
            return
        out = filedialog.askdirectory(title="Zielordner")
        if not out:
            return
        mode = MODES[self.mode.get()]
        base = os.path.splitext(os.path.basename(self.path))[0]
        ext = os.path.splitext(self.path)[1]
        jobs = [(self.clips[i][0], self.clips[i][1], os.path.join(out, f"{base}_kill{i + 1:02d}{ext}")) for i in idx]

        def work():
            try:
                for n, (a, b, dst) in enumerate(jobs, 1):
                    self.after(0, self.status.config, {"text": f"Export {n}/{len(jobs)}…"})
                    export_clip(self.ffmpeg, self.path, a, b, dst, mode)
                self.after(0, self.status.config, {"text": f"Fertig: {len(jobs)} Clips in {out}"})
            except Exception as e:
                self.after(0, messagebox.showerror, "Export-Fehler", str(e))

        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    App().mainloop()

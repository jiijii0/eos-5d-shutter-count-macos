"""Window for eos5d_shutter_count.py (macOS) - 8-bit style.

macOS port of shutter_count_app.py by Blianahui
(https://github.com/Blianahui/EOS_5D_Shutter_Count), GPL-3.0.

The button runs the reader's own main() unchanged; everything it prints goes to
the log window and to ~/Library/Logs/EOS5D_ShutterCount/shutter_count_log.txt.

Fonts: Press Start 2P and VT323 (SIL Open Font License, see fonts/).
"""

from __future__ import annotations

import ctypes
import datetime
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
import tkinter as tk
import tkinter.font as tkfont

import eos5d_shutter_count as reader

LOG_DIR = os.path.expanduser("~/Library/Logs/EOS5D_ShutterCount")
LOG_PATH = os.path.join(LOG_DIR, "shutter_count_log.txt")
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(BASE_DIR, "fonts")

# muted 8-bit palette
BG = "#1a1c2c"
PANEL = "#252840"
EDGE = "#3e4563"
SHADOW = "#10111c"
TEXT = "#e4e6ee"
DIM = "#8a93ab"
FAINT = "#4f5672"
GREEN = "#8fc27a"
YELLOW = "#e8c47f"
ORANGE = "#d98a6a"
BLUE = "#6c9bd2"

COUNT_DIGITS = 7

messages: "queue.Queue[tuple[str, object]]" = queue.Queue()


def register_fonts():
    """Make the bundled TTFs visible to Tk (process-local, via CoreText)."""
    try:
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ct = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreText.framework/CoreText")
        cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        ct.CTFontManagerRegisterFontsForURL.restype = ctypes.c_bool
        ct.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        for name in ("PressStart2P-Regular.ttf", "VT323-Regular.ttf"):
            path = os.path.join(FONT_DIR, name).encode()
            if not os.path.exists(path):
                continue
            url = cf.CFURLCreateFromFileSystemRepresentation(None, path, len(path), False)
            if url:
                ct.CTFontManagerRegisterFontsForURL(url, 1, None)  # 1 = process scope
                cf.CFRelease(url)
    except OSError:
        pass


def pick_font(root, wanted, fallback):
    return wanted if wanted in set(tkfont.families(root)) else fallback


class LogWriter:
    """Stands in for stdout/stderr: hands text to the window and the log file."""

    def __init__(self):
        self._lock = threading.Lock()
        self._line_start = True
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except OSError:
            pass

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            stamped = []
            for part in text.splitlines(keepends=True):
                if self._line_start:
                    stamped.append(datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] "))
                stamped.append(part)
                self._line_start = part.endswith("\n")
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write("".join(stamped))
            except OSError:
                pass
        messages.put(("log", text))
        return len(text)

    def flush(self):
        pass


def run_check():
    """Same as the reader's _run(), with output captured."""
    print("=" * 60, flush=True)
    try:
        reader.main()
        ok = True
    except Exception as exc:
        print(f"\nFATAL: {exc}", flush=True)
        if reader.VERBOSE:
            traceback.print_exc()
        ok = False
    messages.put(("done", "ok" if ok else "fail"))


class PixelPanel(tk.Frame):
    """Flat panel with a hard border, offset shadow and a small title."""

    def __init__(self, master, title, font, accent=DIM):
        self.outer = tk.Frame(master, bg=SHADOW)
        super().__init__(self.outer, bg=PANEL, highlightthickness=2,
                         highlightbackground=EDGE, highlightcolor=EDGE)
        self.place(x=0, y=0, relwidth=1, relheight=1, width=-4, height=-4)
        tk.Label(self, text=title, font=font, bg=PANEL, fg=accent).pack(anchor="w", padx=12, pady=(10, 2))

    def pack(self, **kw):
        self.outer.pack(**kw)

    def size(self, height):
        self.outer.configure(height=height)


class PixelButton(tk.Label):
    """Blocky button. tk.Button ignores colours on macOS, so draw our own."""

    def __init__(self, master, text, command, font):
        super().__init__(master, text=text, font=font, bg=GREEN, fg=BG, padx=16, pady=12,
                         cursor="hand2", highlightthickness=2, highlightbackground=SHADOW)
        self.command = command
        self.enabled = True
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=TEXT))
        self.bind("<Leave>", lambda e: self.enabled and self.configure(bg=GREEN))
        self.bind("<ButtonRelease-1>", self._release)

    def _release(self, e):
        if self.enabled and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()

    def set_enabled(self, on):
        self.enabled = on
        self.configure(bg=GREEN if on else EDGE, fg=BG if on else DIM,
                       cursor="hand2" if on else "arrow")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("EOS 5D Shutter Count")
        self.geometry("740x600")
        self.minsize(620, 520)
        self.configure(bg=BG)
        self.busy = False
        self.output = ""
        self.tick = 0

        pixel = pick_font(self, "Press Start 2P", "Monaco")
        term = pick_font(self, "VT323", "Monaco")
        f_small = (pixel, 8)
        f_ui = (pixel, 10)
        f_title = (pixel, 13)
        f_count = (pixel, 36)
        f_log = (term, 17 if term == "VT323" else 11)

        # ---- header ----
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=18, pady=(18, 8))
        tk.Label(header, text="EOS 5D", font=f_title, bg=BG, fg=YELLOW).pack(side="left")
        tk.Label(header, text=" SHUTTER COUNT", font=f_title, bg=BG, fg=TEXT).pack(side="left")

        # ---- button + camera status ----
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=18, pady=8)
        self.button = PixelButton(top, "CHECK", self.start, f_ui)
        self.button.pack(side="left", padx=(0, 16))

        cam = PixelPanel(top, "CAMERA", f_small)
        cam.size(80)
        cam.pack(side="left", fill="x", expand=True)
        row = tk.Frame(cam, bg=PANEL)
        row.pack(fill="x", padx=12)
        self.dot = tk.Canvas(row, width=12, height=12, bg=PANEL, highlightthickness=0)
        self.dot_id = self.dot.create_rectangle(1, 1, 11, 11, fill=FAINT, outline="")
        self.dot.pack(side="left")
        self.status = tk.Label(row, text="SCANNING", font=f_ui, bg=PANEL, fg=DIM)
        self.status.pack(side="left", padx=8)
        self.detail = tk.Label(cam, text="", font=f_small, bg=PANEL, fg=FAINT)
        self.detail.pack(anchor="w", padx=12, pady=(6, 0))

        # ---- count ----
        score = PixelPanel(self, "SHUTTER COUNT", f_small)
        score.size(150)
        score.pack(fill="x", padx=18, pady=8)
        self.count = tk.Label(score, text="-" * COUNT_DIGITS, font=f_count, bg=PANEL, fg=FAINT)
        self.count.pack(pady=(12, 8))
        # loading bar drawn as pixel blocks (the pixel font has no block glyphs)
        self.bar = tk.Canvas(score, width=COUNT_DIGITS * 40, height=40, bg=PANEL, highlightthickness=0)
        self.cells = [self.bar.create_rectangle(i * 40 + 6, 8, i * 40 + 34, 36, fill=SHADOW, outline="")
                      for i in range(COUNT_DIGITS)]
        self.note = tk.Label(score, text="", font=f_small, bg=PANEL, fg=DIM)
        self.note.pack()

        # ---- log ----
        log_box = PixelPanel(self, "LOG", f_small)
        log_box.pack(fill="both", expand=True, padx=18, pady=8)
        self.log = tk.Text(log_box, font=f_log, bg=SHADOW, fg=DIM, relief="flat", borderwidth=0,
                           highlightthickness=0, padx=10, pady=8, state="disabled", wrap="word",
                           height=8, selectbackground=EDGE, selectforeground=TEXT)
        self.log.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log.tag_configure("error", foreground=ORANGE)
        self.log.tag_configure("result", foreground=YELLOW)

        # ---- footer ----
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=18, pady=(2, 14))
        link = tk.Label(footer, text="OPEN LOG FILE", font=f_small, bg=BG, fg=FAINT, cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda e: self.reveal_log())
        link.bind("<Enter>", lambda e: link.configure(fg=TEXT))
        link.bind("<Leave>", lambda e: link.configure(fg=FAINT))

        self.after(100, self.pump)
        self.after(200, self.animate)
        self.poll_status()

    # ---- actions ----

    def reveal_log(self):
        target = LOG_PATH if os.path.exists(LOG_PATH) else LOG_DIR
        subprocess.run(["open", "-R", target])

    def start(self):
        if self.busy:
            return
        self.busy = True
        self.output = ""
        self.button.set_enabled(False)
        self.count.pack_forget()
        self.bar.pack(before=self.note, pady=(14, 10))
        self.note.configure(text="READING", fg=DIM)
        self.show_status("busy", "")
        threading.Thread(target=run_check, daemon=True).start()

    def append(self, text):
        self.output += text
        self.log.configure(state="normal")
        for line in text.splitlines(keepends=True):
            if re.match(r"\s*(ERROR|FATAL|WARNING)", line):
                tag = "error"
            elif "SHUTTER COUNT" in line:
                tag = "result"
            else:
                tag = None
            self.log.insert("end", line, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def finish(self, result):
        self.busy = False
        self.button.set_enabled(True)
        self.bar.pack_forget()
        self.count.pack(before=self.note, pady=(12, 8))
        m = re.search(r"SHUTTER COUNT: (\d+)", self.output)
        if result == "ok" and m:
            self.count.configure(text=m.group(1).zfill(COUNT_DIGITS), fg=YELLOW)
            approx = "live byte is 0xFF" in self.output
            self.note.configure(text="+/- 255" if approx else "", fg=DIM)
        else:
            self.count.configure(text="-" * COUNT_DIGITS, fg=FAINT)
            self.note.configure(text="FAILED - SEE LOG", fg=ORANGE)
        self.poll_status(once=True)

    # ---- loops ----

    def animate(self):
        self.tick += 1
        if self.busy:
            pos = self.tick % (COUNT_DIGITS + 3)
            for i, cell in enumerate(self.cells):
                self.bar.itemconfigure(cell, fill=BLUE if 0 <= pos - i < 3 else SHADOW)
            self.note.configure(text="READING" + "." * (self.tick % 4))
        self.after(200, self.animate)

    def pump(self):
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == "log":
                    self.append(value)
                elif kind == "status":
                    if not self.busy:
                        self.show_status(*value)
                else:
                    self.finish(value)
        except queue.Empty:
            pass
        self.after(100, self.pump)

    def show_status(self, mode, detail):
        text, colour = {
            "normal": ("READY", GREEN),
            "service": ("SERVICE MODE", ORANGE),
            "none": ("NO CAMERA", FAINT),
            "busy": ("READING", BLUE),
            "error": ("USB ERROR", ORANGE),
        }[mode]
        self.status.configure(text=text, fg=colour if mode != "none" else DIM)
        self.dot.itemconfigure(self.dot_id, fill=colour)
        if mode == "normal":
            self.detail.configure(text=f"USB 04A9:3102  {detail.upper()}")
        elif mode == "service":
            self.detail.configure(text="USB 04A9:3086")
        elif mode == "error":
            self.detail.configure(text=detail.upper()[:44])
        else:
            self.detail.configure(text="")

    def poll_status(self, once=False):
        # Don't enumerate the bus while a check is talking to the camera.
        if not self.busy:
            threading.Thread(target=lambda: messages.put(("status", reader.camera_state())),
                             daemon=True).start()
        if not once:
            self.after(1500, self.poll_status)


def main():
    register_fonts()
    writer = LogWriter()
    sys.stdout = writer
    sys.stderr = writer
    App().mainloop()


if __name__ == "__main__":
    main()

"""
Mirror Bloom - kaleidoscope drawing toy
Python/Tkinter port of mini-apps/mirror-bloom/index.html.

Drawing is mirrored onto a Pillow Image alongside the on-screen Canvas so
"Save image" can write a real PNG directly, without needing Ghostscript
(which canvas.postscript() would otherwise require to rasterize).
"""
import math
import os
import tkinter as tk
from tkinter import filedialog

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

WIDTH, HEIGHT = 800, 800

JEWELS = [
    ("ruby", "#e0435f"),
    ("amber", "#e2963a"),
    ("topaz", "#e8c766"),
    ("emerald", "#3ea373"),
    ("sapphire", "#3f7fd6"),
    ("amethyst", "#9a63d1"),
    ("pearl", "#f2ede0"),
]


class MirrorBloomApp:
    def __init__(self, root):
        self.root = root
        root.title("Mirror Bloom")
        root.configure(bg="#0a0a0d")
        root.resizable(False, False)

        self.fold = 8
        self.brush_size = 3
        self.color = JEWELS[0][1]
        self.drawing = False
        self.last = None
        self.cx, self.cy = WIDTH / 2, HEIGHT * 0.46

        self.image = Image.new("RGB", (WIDTH, HEIGHT), "black") if HAS_PIL else None
        self.draw_ctx = ImageDraw.Draw(self.image) if HAS_PIL else None

        self._build_ui()

    def _build_ui(self):
        self.canvas = tk.Canvas(self.root, width=WIDTH, height=HEIGHT, bg="black", highlightthickness=0)
        self.canvas.pack(side="top")
        self.canvas.bind("<ButtonPress-1>", self.on_start)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_stop)

        rail = tk.Frame(self.root, bg="#141319")
        rail.pack(side="top", fill="x", pady=6)

        tk.Label(rail, text="Fold", bg="#141319", fg="#8e8a78").pack(side="left", padx=(10, 2))
        self.fold_scale = tk.Scale(rail, from_=3, to=16, orient="horizontal", showvalue=True,
                                    bg="#141319", fg="#efe9d8", troughcolor="#2b2933",
                                    highlightthickness=0, command=self.on_fold_change)
        self.fold_scale.set(self.fold)
        self.fold_scale.pack(side="left", padx=4)

        tk.Label(rail, text="Glass", bg="#141319", fg="#8e8a78").pack(side="left", padx=(16, 2))
        swatch_frame = tk.Frame(rail, bg="#141319")
        swatch_frame.pack(side="left")
        self.swatch_buttons = []
        for name, hex_color in JEWELS:
            b = tk.Button(swatch_frame, bg=hex_color, activebackground=hex_color, width=2, height=1,
                          relief="flat", command=lambda c=hex_color: self.select_color(c))
            b.pack(side="left", padx=2)
            self.swatch_buttons.append((b, hex_color))
        self._mark_selected_swatch()

        tk.Label(rail, text="Brush", bg="#141319", fg="#8e8a78").pack(side="left", padx=(16, 2))
        self.size_scale = tk.Scale(rail, from_=1, to=10, orient="horizontal", showvalue=True,
                                    bg="#141319", fg="#efe9d8", troughcolor="#2b2933",
                                    highlightthickness=0, command=self.on_size_change)
        self.size_scale.set(self.brush_size)
        self.size_scale.pack(side="left", padx=4)

        tk.Button(rail, text="Clear chamber", command=self.clear_chamber).pack(side="left", padx=(16, 4))
        self.save_btn = tk.Button(rail, text="Save image", command=self.save_image)
        self.save_btn.pack(side="left", padx=4)
        if not HAS_PIL:
            self.save_btn.config(state="disabled")
            tk.Label(rail, text="(pip install pillow to enable Save)",
                     bg="#141319", fg="#8e8a78").pack(side="left")

    def _mark_selected_swatch(self):
        for b, hex_color in self.swatch_buttons:
            b.config(relief="sunken" if hex_color == self.color else "flat")

    def select_color(self, hex_color):
        self.color = hex_color
        self._mark_selected_swatch()

    def on_fold_change(self, value):
        self.fold = int(float(value))

    def on_size_change(self, value):
        self.brush_size = int(float(value))

    def to_local(self, event_x, event_y):
        return event_x - self.cx, event_y - self.cy

    def draw_segment(self, p0, p1):
        step = (2 * math.pi) / self.fold
        for k in range(self.fold):
            ang = step * k
            s, c = math.sin(ang), math.cos(ang)
            for mirror in (1, -1):
                a0x = p0[0] * c - (p0[1] * mirror) * s
                a0y = p0[0] * s + (p0[1] * mirror) * c
                a1x = p1[0] * c - (p1[1] * mirror) * s
                a1y = p1[0] * s + (p1[1] * mirror) * c
                x0, y0 = self.cx + a0x, self.cy + a0y
                x1, y1 = self.cx + a1x, self.cy + a1y
                self.canvas.create_line(x0, y0, x1, y1, fill=self.color, width=self.brush_size,
                                          capstyle="round")
                if self.draw_ctx:
                    self.draw_ctx.line([(x0, y0), (x1, y1)], fill=self.color, width=self.brush_size)

    def on_start(self, event):
        self.drawing = True
        self.last = self.to_local(event.x, event.y)
        self.draw_segment(self.last, (self.last[0] + 0.01, self.last[1] + 0.01))

    def on_move(self, event):
        if not self.drawing:
            return
        cur = self.to_local(event.x, event.y)
        self.draw_segment(self.last, cur)
        self.last = cur

    def on_stop(self, event):
        self.drawing = False
        self.last = None

    def clear_chamber(self):
        self.canvas.delete("all")
        if self.draw_ctx:
            self.draw_ctx.rectangle([0, 0, WIDTH, HEIGHT], fill="black")

    def save_image(self):
        if not self.image:
            return
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                              filetypes=[("PNG image", "*.png")],
                                              initialfile="mirror-bloom.png")
        if path:
            self.image.save(path)


def main():
    root = tk.Tk()
    MirrorBloomApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

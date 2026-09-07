"""
Nova Fold - 2048-style tile-merging game
Python/Tkinter port of mini-apps/nova-fold/index.html.

Carries forward the overlay fix made on the HTML version: `won` is
compared with `is True`, so continuing past 2048 ("Keep playing") does
not permanently block the "Field is full" game-over overlay from ever
showing again.
"""
import json
import os
import random
import tkinter as tk

SIZE = 4
CELL = 90
GAP = 10
BOARD_PAD = 14
BOARD_PX = BOARD_PAD * 2 + SIZE * CELL + (SIZE - 1) * GAP

HERE = os.path.dirname(os.path.abspath(__file__))
BEST_FILE = os.path.join(HERE, "nf_best.json")

CLASS_NAMES = {
    2: "red dwarf", 4: "orange dwarf", 8: "yellow dwarf", 16: "white star",
    32: "blue star", 64: "subgiant", 128: "giant", 256: "bright giant",
    512: "supergiant", 1024: "hypergiant", 2048: "supernova",
}
TILE_COLORS = {
    2: ("#e7dccb", "#4a3c22"), 4: ("#eec9a6", "#4a2f10"), 8: ("#f0ac72", "#3c220a"),
    16: ("#eb9a58", "#331c07"), 32: ("#e8c23f", "#332902"), 64: ("#cdd85e", "#2a2e0a"),
    128: ("#9fe07a", "#173311"), 256: ("#6fe0c0", "#0d2e26"), 512: ("#6fc7e0", "#0b2933"),
    1024: ("#8f9dff", "#141a3a"), 2048: ("#ffd166", "#332000"),
}


def load_best():
    try:
        with open(BEST_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("best", 0))
    except Exception:
        return 0


def save_best(v):
    try:
        with open(BEST_FILE, "w", encoding="utf-8") as f:
            json.dump({"best": v}, f)
    except Exception:
        pass


class Tile:
    def __init__(self, tile_id, value, r, c):
        self.id = tile_id
        self.value = value
        self.r = r
        self.c = c


class NovaFoldApp:
    def __init__(self, root):
        self.root = root
        root.title("Nova Fold")
        root.configure(bg="#0a0e1a")
        root.resizable(False, False)

        header = tk.Frame(root, bg="#0a0e1a")
        header.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(header, text="Nova Fold", bg="#0a0e1a", fg="#eef2ff",
                 font=("Segoe UI Semibold", 18)).pack(side="left")

        scores = tk.Frame(header, bg="#0a0e1a")
        scores.pack(side="right")
        self.score_var = tk.StringVar(value="0")
        self.best_var = tk.StringVar(value="0")
        self._score_tile(scores, "Score", self.score_var)
        self._score_tile(scores, "Best", self.best_var)

        toolbar = tk.Frame(root, bg="#0a0e1a")
        toolbar.pack(fill="x", padx=14)
        tk.Label(toolbar, text="Arrow keys to shift the field.", bg="#0a0e1a", fg="#8791b8").pack(side="left")
        tk.Button(toolbar, text="New game", command=self.start_game).pack(side="right")

        board_frame = tk.Frame(root, bg="#0a0e1a")
        board_frame.pack(padx=14, pady=14)
        self.canvas = tk.Canvas(board_frame, width=BOARD_PX, height=BOARD_PX,
                                 bg="#131a2c", highlightthickness=0)
        self.canvas.pack()

        self.overlay = None
        self.grid = None
        self.score = 0
        self.best = load_best()
        self.tile_id = 0
        self.over = False
        self.won = False

        root.bind("<Key>", self.on_key)
        root.focus_set()

        self.start_game()

    def _score_tile(self, parent, label, var):
        box = tk.Frame(parent, bg="#151b2e", bd=1, relief="solid")
        box.pack(side="left", padx=4)
        tk.Label(box, text=label.upper(), bg="#151b2e", fg="#8791b8", font=("Segoe UI", 7)).pack(
            padx=10, pady=(4, 0))
        tk.Label(box, textvariable=var, bg="#151b2e", fg="#eef2ff", font=("Consolas", 13, "bold")).pack(
            padx=10, pady=(0, 4))

    # ---------- grid mechanics ----------
    def empty_grid(self):
        return [[None] * SIZE for _ in range(SIZE)]

    def random_empty_cell(self):
        empties = [(r, c) for r in range(SIZE) for c in range(SIZE) if self.grid[r][c] is None]
        return random.choice(empties) if empties else None

    def spawn(self):
        cell = self.random_empty_cell()
        if not cell:
            return
        r, c = cell
        value = 2 if random.random() < 0.9 else 4
        self.tile_id += 1
        self.grid[r][c] = Tile(f"t{self.tile_id}", value, r, c)

    def start_game(self):
        self.grid = self.empty_grid()
        self.score = 0
        self.tile_id = 0
        self.over = False
        self.won = False
        self.best = load_best()
        self.spawn()
        self.spawn()
        self.render()

    def get_lines(self, direction):
        lines = []
        if direction in ("left", "right"):
            for r in range(SIZE):
                line = [(r, c) for c in range(SIZE)]
                if direction == "right":
                    line.reverse()
                lines.append(line)
        else:
            for c in range(SIZE):
                line = [(r, c) for r in range(SIZE)]
                if direction == "down":
                    line.reverse()
                lines.append(line)
        return lines

    def move(self, direction):
        if self.over:
            return
        lines = self.get_lines(direction)
        moved = False
        for line in lines:
            tiles = [self.grid[r][c] for (r, c) in line if self.grid[r][c] is not None]
            result = []
            i = 0
            while i < len(tiles):
                if i + 1 < len(tiles) and tiles[i].value == tiles[i + 1].value:
                    self.tile_id += 1
                    merged_value = tiles[i].value * 2
                    merged = Tile(f"t{self.tile_id}", merged_value, 0, 0)
                    self.score += merged_value
                    if merged_value == 2048 and not self.won:
                        self.won = True
                    result.append(merged)
                    i += 2
                else:
                    result.append(tiles[i])
                    i += 1
            for k, (r, c) in enumerate(line):
                new_tile = result[k] if k < len(result) else None
                if new_tile is not None:
                    new_tile.r, new_tile.c = r, c
                if self.grid[r][c] is not new_tile:
                    moved = True
                self.grid[r][c] = new_tile
        if moved:
            self.spawn()
            if not self.has_moves():
                self.over = True
            self.render()

    def has_moves(self):
        for r in range(SIZE):
            for c in range(SIZE):
                if self.grid[r][c] is None:
                    return True
                v = self.grid[r][c].value
                if c < SIZE - 1 and self.grid[r][c + 1] and self.grid[r][c + 1].value == v:
                    return True
                if r < SIZE - 1 and self.grid[r + 1][c] and self.grid[r + 1][c].value == v:
                    return True
        return False

    # ---------- rendering ----------
    def tile_style(self, value):
        key = value if value <= 2048 else 2048
        return TILE_COLORS.get(key, TILE_COLORS[2048])

    def render(self):
        self.canvas.delete("all")
        for r in range(SIZE):
            for c in range(SIZE):
                x0 = BOARD_PAD + c * (CELL + GAP)
                y0 = BOARD_PAD + r * (CELL + GAP)
                self.canvas.create_rectangle(x0, y0, x0 + CELL, y0 + CELL, fill="#1c2540", outline="")
        for r in range(SIZE):
            for c in range(SIZE):
                t = self.grid[r][c]
                if not t:
                    continue
                x0 = BOARD_PAD + c * (CELL + GAP)
                y0 = BOARD_PAD + r * (CELL + GAP)
                bg, fg = self.tile_style(t.value)
                self.canvas.create_rectangle(x0, y0, x0 + CELL, y0 + CELL, fill=bg, outline="")
                self.canvas.create_text(x0 + CELL / 2, y0 + CELL / 2 - 6, text=str(t.value),
                                          fill=fg, font=("Consolas", 20, "bold"))
                cls = CLASS_NAMES.get(min(t.value, 2048), "")
                self.canvas.create_text(x0 + CELL / 2, y0 + CELL - 12, text=cls,
                                          fill=fg, font=("Segoe UI", 7))
        self.score_var.set(str(self.score))
        if self.score > self.best:
            self.best = self.score
            save_best(self.best)
        self.best_var.set(str(self.best))

        self.remove_overlay()
        if self.won is True:
            self.show_overlay("Supernova!",
                               "You folded a star all the way to 2048. Keep going, or start fresh.",
                               "Keep playing", self.keep_playing)
        elif self.over:
            self.show_overlay("Field is full", "No more folds available. Start a new field?",
                               "New game", self.start_game)

    def keep_playing(self):
        self.won = "continue"
        self.remove_overlay()

    def show_overlay(self, title, msg, btn_text, on_click):
        self.remove_overlay()
        self.overlay = tk.Frame(self.canvas, bg="#0a0c18")
        self.overlay.place(relx=0.5, rely=0.5, anchor="center", width=BOARD_PX - 40, height=BOARD_PX - 40)
        tk.Label(self.overlay, text=title, bg="#0a0c18", fg="white",
                 font=("Segoe UI Semibold", 16)).pack(pady=(24, 8))
        tk.Label(self.overlay, text=msg, bg="#0a0c18", fg="#c7cbe6", wraplength=BOARD_PX - 80,
                 justify="center").pack(pady=4, padx=10)

        def handle():
            on_click()
            if title != "Supernova!":
                self.render()

        tk.Button(self.overlay, text=btn_text, command=handle).pack(pady=16)

    def remove_overlay(self):
        if self.overlay:
            self.overlay.destroy()
            self.overlay = None

    def on_key(self, event):
        mapping = {"Left": "left", "Right": "right", "Up": "up", "Down": "down"}
        d = mapping.get(event.keysym)
        if d:
            self.move(d)


def main():
    root = tk.Tk()
    NovaFoldApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

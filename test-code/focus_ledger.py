"""
Focus Ledger - Pomodoro timer + task ledger
Python/Tkinter port of mini-apps/focus-ledger/index.html.
State persists to fl_state.json / fl_tasks.json next to this script,
mirroring the original's localStorage keys (fl_state / fl_tasks).
"""
import json
import os
import time
import tkinter as tk

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "fl_state.json")
TASKS_FILE = os.path.join(HERE, "fl_tasks.json")

DURATIONS = {"work": 25 * 60, "short": 5 * 60, "long": 15 * 60}
LABELS = {"work": "working on", "short": "short break", "long": "long break"}
MODE_TITLES = {"work": "Focus 25", "short": "Short 5", "long": "Long 15"}


def today_key():
    d = time.localtime()
    return f"{d.tm_year}-{d.tm_mon}-{d.tm_mday}"


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def chime():
    if not HAS_WINSOUND:
        return
    try:
        winsound.Beep(880, 450)
    except Exception:
        pass


class Task:
    _next_id = 1

    def __init__(self, title, done=False, sessions=0, task_id=None):
        self.id = task_id or Task._alloc_id()
        self.title = title
        self.done = done
        self.sessions = sessions

    @classmethod
    def _alloc_id(cls):
        tid = f"t{cls._next_id}"
        cls._next_id += 1
        return tid

    def to_dict(self):
        return {"id": self.id, "title": self.title, "done": self.done, "sessions": self.sessions}

    @classmethod
    def from_dict(cls, d):
        return cls(d["title"], d.get("done", False), d.get("sessions", 0), d.get("id"))


class FocusLedgerApp:
    def __init__(self, root):
        self.root = root
        root.title("Focus Ledger")
        root.configure(bg="#15171a")
        root.geometry("760x560")

        state = load_json(STATE_FILE, {
            "mode": "work", "remaining": DURATIONS["work"], "running": False,
            "active_task_id": None, "sessions_today": 0, "focus_seconds": 0,
            "last_day": today_key(),
        })
        if state.get("last_day") != today_key():
            state["sessions_today"] = 0
            state["focus_seconds"] = 0
            state["last_day"] = today_key()
        self.state = state

        raw_tasks = load_json(TASKS_FILE, [
            {"title": "Draft Q3 proposal", "done": False, "sessions": 0},
            {"title": "Review open PRs", "done": False, "sessions": 0},
            {"title": "Read chapter 4", "done": True, "sessions": 2},
        ])
        self.tasks = [Task.from_dict(t) for t in raw_tasks]

        self.tick_job = None
        self._build_ui()
        self.render_timer()
        self.render_tasks()
        if self.state["running"]:
            self.start_tick()

    # ---------- persistence ----------
    def save(self):
        save_json(STATE_FILE, self.state)
        save_json(TASKS_FILE, [t.to_dict() for t in self.tasks])

    # ---------- UI ----------
    def _build_ui(self):
        wrap = tk.Frame(self.root, bg="#15171a")
        wrap.pack(fill="both", expand=True, padx=20, pady=20)

        board = tk.Frame(wrap, bg="#15171a")
        board.pack(fill="both", expand=True)

        timer_panel = tk.Frame(board, bg="#1d2023", bd=1, relief="solid")
        timer_panel.pack(side="left", fill="both", expand=True, padx=(0, 10))

        mode_row = tk.Frame(timer_panel, bg="#1d2023")
        mode_row.pack(pady=14)
        self.mode_buttons = {}
        for mode in ("work", "short", "long"):
            b = tk.Button(mode_row, text=MODE_TITLES[mode], relief="flat",
                          command=lambda m=mode: self.set_mode(m, user=True))
            b.pack(side="left", padx=4)
            self.mode_buttons[mode] = b

        self.canvas = tk.Canvas(timer_panel, width=220, height=220, bg="#1d2023", highlightthickness=0)
        self.canvas.pack(pady=10)
        self.canvas.create_oval(10, 10, 210, 210, outline="#2b2e32", width=10)
        self.ring_progress = self.canvas.create_arc(10, 10, 210, 210, start=90, extent=0,
                                                      outline="#ff6a45", width=10, style="arc")
        self.time_text = self.canvas.create_text(110, 100, text="25:00", fill="#eeece6",
                                                    font=("Consolas", 28, "bold"))
        self.session_text = self.canvas.create_text(110, 130, text="no task selected", fill="#9a9d98",
                                                       font=("Segoe UI", 9))

        controls = tk.Frame(timer_panel, bg="#1d2023")
        controls.pack(pady=8)
        self.start_btn = tk.Button(controls, text="Start", width=10, command=self.toggle_start)
        self.start_btn.pack(side="left", padx=4)
        tk.Button(controls, text="Reset", width=10, command=self.reset_timer).pack(side="left", padx=4)

        tally = tk.Frame(timer_panel, bg="#1d2023")
        tally.pack(pady=10)
        self.sessions_label = tk.Label(tally, text="Sessions today 0", bg="#1d2023", fg="#9a9d98")
        self.sessions_label.pack(side="left", padx=10)
        self.minutes_label = tk.Label(tally, text="Focus minutes 0", bg="#1d2023", fg="#9a9d98")
        self.minutes_label.pack(side="left", padx=10)

        ledger_panel = tk.Frame(board, bg="#1d2023", bd=1, relief="solid")
        ledger_panel.pack(side="left", fill="both", expand=True)

        head = tk.Frame(ledger_panel, bg="#1d2023")
        head.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(head, text="Ledger", bg="#1d2023", fg="#eeece6", font=("Segoe UI", 12, "bold")).pack(side="left")
        self.done_count_label = tk.Label(head, text="0 / 0 done", bg="#1d2023", fg="#9a9d98")
        self.done_count_label.pack(side="right")

        add_row = tk.Frame(ledger_panel, bg="#1d2023")
        add_row.pack(fill="x", padx=14, pady=6)
        self.task_entry = tk.Entry(add_row)
        self.task_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.task_entry.bind("<Return>", lambda e: self.add_task())
        tk.Button(add_row, text="Add", command=self.add_task).pack(side="left")

        self.task_list_frame = tk.Frame(ledger_panel, bg="#1d2023")
        self.task_list_frame.pack(fill="both", expand=True, padx=14, pady=6)

    # ---------- rendering ----------
    def fmt(self, sec):
        m, s = divmod(max(0, sec), 60)
        return f"{m:02d}:{s:02d}"

    def active_task(self):
        for t in self.tasks:
            if t.id == self.state.get("active_task_id"):
                return t
        return None

    def render_timer(self):
        self.canvas.itemconfig(self.time_text, text=self.fmt(self.state["remaining"]))
        t = self.active_task()
        if self.state["mode"] == "work":
            label = f"{LABELS['work']} {t.title}" if t else "no task selected"
        else:
            label = LABELS[self.state["mode"]]
        self.canvas.itemconfig(self.session_text, text=label)

        total = DURATIONS[self.state["mode"]]
        frac = 1 - (self.state["remaining"] / total) if total else 0
        self.canvas.itemconfig(self.ring_progress, extent=-360 * frac)

        self.start_btn.config(text="Pause" if self.state["running"] else "Start")
        for mode, b in self.mode_buttons.items():
            b.config(relief="sunken" if mode == self.state["mode"] else "flat")

        self.sessions_label.config(text=f"Sessions today {self.state['sessions_today']}")
        self.minutes_label.config(text=f"Focus minutes {round(self.state['focus_seconds']/60)}")

    def render_tasks(self):
        for w in self.task_list_frame.winfo_children():
            w.destroy()
        done_n = sum(1 for t in self.tasks if t.done)
        self.done_count_label.config(text=f"{done_n} / {len(self.tasks)} done")
        if not self.tasks:
            tk.Label(self.task_list_frame, text="No tasks yet — add one above.",
                     bg="#1d2023", fg="#9a9d98").pack(anchor="w")
            return
        for t in self.tasks:
            row = tk.Frame(self.task_list_frame, bg="#23262a", bd=1, relief="solid")
            row.pack(fill="x", pady=3)
            check_text = "✓" if t.done else " "
            tk.Button(row, text=check_text, width=2,
                      command=lambda t=t: self.toggle_done(t)).pack(side="left", padx=4, pady=4)
            title_fg = "#6b706a" if t.done else "#eeece6"
            title_font = ("Segoe UI", 10, "overstrike") if t.done else ("Segoe UI", 10)
            tk.Label(row, text=t.title, bg="#23262a", fg=title_fg, font=title_font).pack(
                side="left", fill="x", expand=True, padx=4)
            tk.Label(row, text=f"{t.sessions}x", bg="#23262a", fg="#9a9d98").pack(side="left", padx=4)
            focus_text = "focused" if t.id == self.state.get("active_task_id") else "focus"
            tk.Button(row, text=focus_text, relief="flat", fg="#9a9d98", bg="#23262a",
                      command=lambda t=t: self.pick_task(t)).pack(side="left", padx=4)
            tk.Button(row, text="×", relief="flat", fg="#9a9d98", bg="#23262a",
                      command=lambda t=t: self.delete_task(t)).pack(side="left", padx=4)

    # ---------- actions ----------
    def set_mode(self, mode, reset_remaining=True, user=False):
        self.state["mode"] = mode
        if reset_remaining:
            self.state["remaining"] = DURATIONS[mode]
        if user:
            self.state["running"] = False
            self.stop_tick()
        self.save()
        self.render_timer()

    def toggle_start(self):
        self.state["running"] = not self.state["running"]
        if self.state["running"]:
            self.start_tick()
        else:
            self.stop_tick()
        self.save()
        self.render_timer()

    def reset_timer(self):
        self.state["running"] = False
        self.stop_tick()
        self.state["remaining"] = DURATIONS[self.state["mode"]]
        self.save()
        self.render_timer()

    def complete_session(self):
        if self.state["mode"] == "work":
            self.state["sessions_today"] += 1
            self.state["focus_seconds"] += DURATIONS["work"]
            t = self.active_task()
            if t:
                t.sessions += 1
            next_mode = "long" if self.state["sessions_today"] % 4 == 0 else "short"
            self.set_mode(next_mode)
        else:
            self.set_mode("work")
        self.state["running"] = False
        self.stop_tick()
        chime()
        self.save()
        self.render_timer()
        self.render_tasks()

    def tick(self):
        self.state["remaining"] -= 1
        if self.state["remaining"] <= 0:
            self.complete_session()
            return
        self.save()
        self.render_timer()
        self.tick_job = self.root.after(1000, self.tick)

    def start_tick(self):
        if self.tick_job:
            return
        self.tick_job = self.root.after(1000, self.tick)

    def stop_tick(self):
        if self.tick_job:
            self.root.after_cancel(self.tick_job)
            self.tick_job = None

    def add_task(self):
        title = self.task_entry.get().strip()
        if not title:
            return
        self.tasks.append(Task(title))
        self.task_entry.delete(0, "end")
        self.save()
        self.render_tasks()

    def toggle_done(self, t):
        t.done = not t.done
        self.save()
        self.render_tasks()

    def pick_task(self, t):
        self.state["active_task_id"] = t.id
        self.save()
        self.render_tasks()
        self.render_timer()

    def delete_task(self, t):
        self.tasks = [x for x in self.tasks if x.id != t.id]
        if self.state.get("active_task_id") == t.id:
            self.state["active_task_id"] = None
        self.save()
        self.render_tasks()
        self.render_timer()


def main():
    root = tk.Tk()
    FocusLedgerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

"""
Garden_GUI.py - a simple desktop window for the productivity tracker (tkinter, no extra installs).

  python Garden_GUI.py            run the real tracker (needs: pip install pygetwindow, Windows)
  python Garden_GUI.py --demo     fake windows + sample history in data/demo.db, to look around

Closing the window stops tracking (use Tracking > Pause, or just minimise, to keep it running).
Needs Garden_ASCII.py and db.py alongside. Insights use coach.py (standard library only), or the older ml_coach.py.
"""
import argparse
import csv
import math
import queue
import random
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, simpledialog, ttk

import Garden_ASCII as ga
import db

try:
    import coach as ml_coach            # lightweight, standard library only (preferred)
except Exception:
    try:
        import ml_coach                 # the older pandas / scikit-learn version
    except Exception:
        ml_coach = None

LABELS = {1: ("PRODUCTIVE", "#2e7d32"), 0: ("DISTRACTION", "#c62828"), 2: ("NEUTRAL", "#6b6b6b")}
DEMO_TITLES = ["main.py - garden - Visual Studio Code", "Lecture notes - Notion",
               "Cat compilation - YouTube - Google Chrome", "general - Slack", "Inbox - Outlook"]


def nice_max(v):
    step = 0.5 if v < 8 else 1 if v < 20 else 5
    return max(step, math.ceil(v / step) * step)


def hm(secs):
    secs = int(secs or 0)
    h, m = secs // 3600, secs % 3600 // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ───────────────────────── background tracker thread ─────────────────────────
class Worker(threading.Thread):
    """Runs the tracker + reminder rules off the UI thread. The UI only reads `state` and `events`."""

    def __init__(self, db_path, get_title):
        super().__init__(daemon=True, name="tracker")
        self.db_path, self.get_title = db_path, get_title
        self.stop_flag, self.paused = threading.Event(), threading.Event()
        self.events = queue.Queue()                        # ("toast", title, msg) | ("error", text)
        self.state = {"ready": False}
        self.cfg = dict(db.DEFAULT_SETTINGS)
        self.day, self.week_prod, self.weekly_goal = {0: 0, 1: 0, 2: 0}, 0, 0
        self.run_start, self.suggested, self.pending_break = None, False, None
        self.tracker = None

    def run(self):
        conn = db.connect(self.db_path)
        prod, neu = db.get_keywords(conn)
        self.tracker = ga.Tracker(conn, prod, neu, on_warn=lambda _m: self._warn(conn))
        last_slow = last_rebuild = 0.0
        while not self.stop_flag.is_set():
            now = time.time()
            try:
                if now - last_slow >= 3:
                    self._slow(conn)
                    last_slow = now
                if now - last_rebuild >= ga.REBUILD_SECS:
                    db.rebuild_daily_summaries(conn)
                    db.ensure_week_goal(conn)
                    last_rebuild = now
                title = None if self.paused.is_set() else self.get_title()
                self.tracker.tick(title, now)
                self._run_logic(conn, now, title)
                self._publish(now, title)
            except Exception as e:                           # never let the tracker die silently
                self.events.put(("error", repr(e)))
                self.stop_flag.wait(5)
            self.stop_flag.wait(ga.POLL_SECS)
        self.tracker.stop(time.time())
        db.rebuild_all(conn)
        conn.close()

    # -- periodic refresh of things edited in the UI (keywords, settings) and of day/week totals
    def _slow(self, conn):
        self.tracker.productive, self.tracker.neutral = db.get_keywords(conn)
        self.cfg = {k: db.setting(conn, k) for k in db.DEFAULT_SETTINGS}
        ga.WARN_AFTER_SECS = int(self.cfg["warn_after_secs"])
        today, monday = datetime.now().strftime("%Y-%m-%d"), db.monday_of()
        self.day = {0: 0, 1: 0, 2: 0}
        for lab, s in conn.execute("SELECT label, SUM(duration) FROM sessions WHERE date=? GROUP BY label", (today,)):
            self.day[lab] = s
        self.week_prod = conn.execute(
            "SELECT COALESCE(SUM(duration),0) FROM sessions WHERE label=1 AND date>=?", (monday,)).fetchone()[0]
        row = conn.execute("SELECT target_secs FROM goals WHERE week=?", (monday,)).fetchone()
        self.weekly_goal = row[0] if row else self.cfg["weekly_goal_secs"]

    def _run_min(self, now):
        return (now - self.run_start) / 60 if self.run_start else 0.0

    def _log_nudge(self, conn, now, arm):
        dt = datetime.fromtimestamp(now)
        db.SqlNudgeBandit(conn).log(now, db.context_key(now), arm, "rule", dt.hour, dt.weekday(),
                                    run_min=round(self._run_min(now), 1))

    def _warn(self, conn):                                   # called by the tracker after a long distraction
        now = time.time()
        if db.can_nudge(conn, now):
            self._log_nudge(conn, now, "rule_warn")
            self.events.put(("toast", "Time to refocus",
                             f"You've been off-task for {ga.fmt_secs(ga.WARN_AFTER_SECS)}. Ready for a fresh focus block?"))

    def _run_logic(self, conn, now, title):
        """Track the current focus run, suggest a break at the focus limit, and record what happened."""
        cur = self.tracker.cur
        on_task = bool(title) and cur is not None and cur["label"] != 0
        if on_task:
            if self.run_start is None:
                self.run_start, self.suggested = now, False
        elif self.run_start is not None:                     # the run just ended
            if self.pending_break and now - self.pending_break[1] <= 600:
                db.resolve_break(conn, self.pending_break[0], True, started_at=int(now))   # proxy: stepped away
                self.pending_break = None
            self.run_start = None
        if self.pending_break and now - self.pending_break[1] > 600:
            db.resolve_break(conn, self.pending_break[0], False)                           # kept going: skipped
            self.pending_break = None

        run_min = self._run_min(now)
        if (on_task and not self.suggested and run_min >= self.cfg["focus_minutes"]
                and self.cfg["break_suggestions_enabled"] and db.can_nudge(conn, now)):
            self.suggested = True
            mins = int(self.cfg["break_minutes"])
            bid = db.suggest_break(conn, now, "fade_point", mins * 60, round(run_min, 1))
            self.pending_break = (bid, now)
            self._log_nudge(conn, now, "rule_break")
            self.events.put(("toast", "Time for a break?",
                             f"You've been on task for {int(run_min)} minutes. A {mins}-minute break now will help you stay sharp."))

    def _publish(self, now, title):
        cur = self.tracker.cur
        self.state = {
            "ready": True, "paused": self.paused.is_set(), "title": title,
            "app": cur["app"] if cur else None, "label": cur["label"] if cur else None,
            "unflushed": int(now - cur["flushed_at"]) if cur else 0,
            "run_min": self._run_min(now), "day": dict(self.day), "week_prod": self.week_prod,
            "daily_goal": self.cfg["daily_goal_secs"], "weekly_goal": self.weekly_goal,
            "focus_minutes": self.cfg["focus_minutes"],
        }


# ───────────────────────── small drawing helper (no matplotlib needed) ─────────────────────────
def draw_bars(cv, labels, values, color, ymax, unit="", hline=None):
    cv.delete("all")
    W, H = int(cv["width"]), int(cv["height"])
    left, right, top, bottom = 38, 8, 14, 22
    pw, ph = W - left - right, H - top - bottom
    ymax = max(ymax, 1e-9)
    cv.create_line(left, top, left, top + ph, fill="#999")
    cv.create_line(left, top + ph, left + pw, top + ph, fill="#999")
    cv.create_text(left - 4, top, text=f"{ymax:g}{unit}", anchor="e", font=("TkDefaultFont", 7))
    cv.create_text(left - 4, top + ph, text="0", anchor="e", font=("TkDefaultFont", 7))
    slot = pw / len(values)
    for i, v in enumerate(values):
        x0, x1 = left + i * slot + slot * 0.15, left + (i + 1) * slot - slot * 0.15
        y = top + ph - ph * min(v, ymax) / ymax
        cv.create_rectangle(x0, y, x1, top + ph, fill=color, outline="")
        if labels[i]:
            cv.create_text((x0 + x1) / 2, top + ph + 10, text=labels[i], font=("TkDefaultFont", 7))
    if hline:
        y = top + ph - ph * min(hline, ymax) / ymax
        cv.create_line(left, y, left + pw, y, fill="#c62828", dash=(4, 3))
        cv.create_text(left + pw, y - 6, text=f"goal {hline:g}{unit}", anchor="e", fill="#c62828",
                       font=("TkDefaultFont", 7))


SETTING_FIELDS = [   # key, label, scale, from, to, step
    ("daily_goal_secs", "Daily productive goal (hours)", 3600, 0, 16, 0.5),
    ("weekly_goal_secs", "Weekly productive goal (hours)", 3600, 0, 80, 1),
    ("focus_minutes", "Focus block before a break reminder (minutes)", 1, 10, 120, 5),
    ("break_minutes", "Suggested break length (minutes)", 1, 1, 60, 1),
    ("warn_after_secs", "Warn after being off-task for (minutes)", 60, 1, 60, 1),
    ("cooldown_min", "Minimum gap between reminders (minutes)", 1, 1, 120, 1),
    ("max_nudges_per_hour", "Maximum reminders per hour", 1, 0, 12, 1),
    ("quiet_start_hour", "Quiet hours start (0-23)", 1, 0, 23, 1),
    ("quiet_end_hour", "Quiet hours end (0-23)", 1, 0, 23, 1),
]


# ───────────────────────── the window ─────────────────────────
class App:
    def __init__(self, root, db_path, get_title, demo=False):
        self.root, self.db_path, self.get_title, self.demo = root, db_path, get_title, demo
        self.conn = db.connect(db_path)                      # UI-thread connection (created before the worker)
        if not any(db.get_keywords(self.conn)) and ga.KEYWORDS_FILE.exists():
            try:
                db.import_keywords_json(self.conn, ga.KEYWORDS_FILE)
            except Exception:
                pass
        self.ui_q, self.insights = queue.Queue(), {}
        self.worker = Worker(db_path, get_title)

        root.title("Garden - productivity tracker" + (" (demo)" if demo else ""))
        root.geometry("880x640")
        root.minsize(780, 560)
        ttk.Style().theme_use("clam")
        self._build_menu()
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tabs = {}
        for name, builder in [("Dashboard", self._tab_dashboard), ("Keywords", self._tab_keywords),
                              ("Insights", self._tab_insights), ("History", self._tab_history),
                              ("Settings", self._tab_settings)]:
            frame = ttk.Frame(self.nb)
            self.nb.add(frame, text=name)
            self.tabs[name] = frame
            builder(frame)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.worker.start()
        root.after(500, self._tick)

    # ---------- menu bar ----------
    def _build_menu(self):
        bar = tk.Menu(self.root)
        f = tk.Menu(bar, tearoff=0)
        f.add_command(label="Export sessions to CSV...", command=self.export_csv)
        f.add_command(label="Rebuild statistics", command=self.rebuild)
        f.add_separator()
        f.add_command(label="Quit", command=self.quit)
        bar.add_cascade(label="File", menu=f)
        self.m_track = tk.Menu(bar, tearoff=0)
        self.m_track.add_command(label="Pause tracking", command=self.toggle_pause)
        bar.add_cascade(label="Tracking", menu=self.m_track)
        v = tk.Menu(bar, tearoff=0)
        for name in ["Dashboard", "Keywords", "Insights", "History", "Settings"]:
            v.add_command(label=name, command=lambda n=name: self.nb.select(self.tabs[n]))
        bar.add_cascade(label="View", menu=v)
        h = tk.Menu(bar, tearoff=0)
        h.add_command(label="Where is my data?", command=lambda: messagebox.showinfo(
            "Your data", f"Everything is stored locally in:\n{self.db_path}\n\nNothing is sent anywhere."))
        bar.add_cascade(label="Help", menu=h)
        self.root.config(menu=bar)

    # ---------- dashboard ----------
    def _tab_dashboard(self, p):
        self.status = tk.Label(p, text="Starting...", font=("TkDefaultFont", 26, "bold"), fg="white", bg="#6b6b6b", pady=14)
        self.status.pack(fill="x", padx=12, pady=(12, 4))
        self.now_lbl = ttk.Label(p, text="", font=("TkDefaultFont", 11))
        self.now_lbl.pack(anchor="w", padx=14)
        self.run_lbl = ttk.Label(p, text="", foreground="#555")
        self.run_lbl.pack(anchor="w", padx=14, pady=(0, 8))
        self.hint = ttk.Label(p, text="", foreground="#c62828", wraplength=780)
        self.hint.pack(anchor="w", padx=14)

        box = ttk.LabelFrame(p, text="Today")
        box.pack(fill="x", padx=12, pady=6)
        self.today_lbl = ttk.Label(box, text="")
        self.today_lbl.pack(anchor="w", padx=10, pady=(8, 2))
        self.day_bar = ttk.Progressbar(box, maximum=100)
        self.day_bar.pack(fill="x", padx=10)
        self.day_txt = ttk.Label(box, text="")
        self.day_txt.pack(anchor="w", padx=10, pady=(2, 8))

        box = ttk.LabelFrame(p, text="This week")
        box.pack(fill="x", padx=12, pady=6)
        self.week_bar = ttk.Progressbar(box, maximum=100)
        self.week_bar.pack(fill="x", padx=10, pady=(10, 0))
        self.week_txt = ttk.Label(box, text="")
        self.week_txt.pack(anchor="w", padx=10, pady=(2, 8))

        row = ttk.Frame(p)
        row.pack(fill="x", padx=12, pady=8)
        self.pause_btn = ttk.Button(row, text="Pause tracking", command=self.toggle_pause)
        self.pause_btn.pack(side="left")
        ttk.Label(row, text="   Wrong label? Mark the current window as:").pack(side="left")
        for lab, txt in [(1, "Productive"), (2, "Neutral"), (0, "Distraction")]:
            ttk.Button(row, text=txt, command=lambda l=lab: self.mark_current(l)).pack(side="left", padx=3)

    def _update_dashboard(self):
        st = self.worker.state
        if not st.get("ready"):
            return
        if st["paused"]:
            text, color = "PAUSED", "#6b6b6b"
        elif st["label"] is None:
            text, color = "NO ACTIVE WINDOW", "#6b6b6b"
        else:
            text, color = LABELS[st["label"]]
        self.status.config(text=text, bg=color)
        self.now_lbl.config(text=f"{st['app']}   |   {(st['title'] or '')[:70]}" if st["app"] else "")
        self.run_lbl.config(text=f"Focus run: {int(st['run_min'])} min  (break suggested at {int(st['focus_minutes'])} min)"
                            if st["run_min"] > 0 else "")
        day = dict(st["day"])
        if st["label"] is not None:
            day[st["label"]] = day.get(st["label"], 0) + st["unflushed"]
        week = st["week_prod"] + (st["unflushed"] if st["label"] == 1 else 0)
        self.today_lbl.config(text=f"Productive {hm(day[1])}     Neutral {hm(day[2])}     Distraction {hm(day[0])}")
        dg, wg = max(st["daily_goal"], 1), max(st["weekly_goal"], 1)
        self.day_bar["value"] = min(100, 100 * day[1] / dg)
        self.day_txt.config(text=f"{hm(day[1])} of {hm(dg)} daily goal  ({min(100, 100 * day[1] / dg):.0f}%)")
        self.week_bar["value"] = min(100, 100 * week / wg)
        self.week_txt.config(text=f"{hm(week)} of {hm(wg)} weekly goal  ({min(100, 100 * week / wg):.0f}%)")
        self.hint.config(text="" if self.worker.tracker.productive else
                         "No productive keywords yet, so everything counts as a distraction. Add some in the Keywords tab.")
        self.m_track.entryconfig(0, label="Resume tracking" if st["paused"] else "Pause tracking")
        self.pause_btn.config(text="Resume tracking" if st["paused"] else "Pause tracking")

    def toggle_pause(self):
        (self.worker.paused.clear if self.worker.paused.is_set() else self.worker.paused.set)()

    def mark_current(self, label):
        st = self.worker.state
        if not st.get("app"):
            messagebox.showinfo("Nothing to mark", "There is no active window being tracked right now.")
            return
        title = (st["title"] or "").lower()
        if label in (1, 2):
            word = "productive" if label == 1 else "neutral"
            kw = simpledialog.askstring(f"Mark as {word}", f"Any window whose title contains this text will count as {word}.\n"
                                        "Tip: for a browser, use a word from the page title (e.g. 'wikipedia')\n"
                                        "instead of 'chrome', or every tab will count:",
                                        initialvalue=st["app"].lower(), parent=self.root)
            if kw and kw.strip():
                db.add_keyword(self.conn, kw, label)
        else:
            prod, neu = db.get_keywords(self.conn)
            hits = [k for k in prod + neu if k in title]
            if not hits:
                messagebox.showinfo("Already a distraction", "No keyword matches this window, so it already counts as a distraction.")
                return
            if messagebox.askyesno("Mark as distraction", "Remove these keywords?\n\n" + "\n".join(hits)):
                for k in hits:
                    db.remove_keyword(self.conn, k)
        self._kw_reload()

    # ---------- keywords ----------
    def _tab_keywords(self, p):
        ttk.Label(p, text="A window counts as productive/neutral if its title contains one of these words. Anything else is a distraction.",
                  wraplength=800).pack(anchor="w", padx=12, pady=(10, 0))
        grid = ttk.Frame(p)
        grid.pack(fill="both", expand=True)
        for c in range(3):
            grid.columnconfigure(c, weight=1)
        grid.rowconfigure(0, weight=1)
        self.lb_prod = self._kw_column(grid, "Productive", 1, 0)
        self.lb_neu = self._kw_column(grid, "Neutral (email, chat...)", 2, 1)

        f = ttk.LabelFrame(grid, text="Open windows right now")
        f.grid(row=0, column=2, sticky="nsew", padx=6, pady=6)
        self.lb_open = tk.Listbox(f, height=12, exportselection=False)
        self.lb_open.pack(fill="both", expand=True, padx=6, pady=6)
        r = ttk.Frame(f)
        r.pack(pady=(0, 6))
        ttk.Button(r, text="-> Productive", command=lambda: self._kw_from_open(1)).pack(side="left", padx=2)
        ttk.Button(r, text="-> Neutral", command=lambda: self._kw_from_open(2)).pack(side="left", padx=2)
        ttk.Button(f, text="Refresh list", command=self._kw_reload).pack(pady=(0, 6))
        self._kw_reload()

    def _kw_column(self, parent, title, label, col):
        f = ttk.LabelFrame(parent, text=title)
        f.grid(row=0, column=col, sticky="nsew", padx=6, pady=6)
        lb = tk.Listbox(f, height=12, exportselection=False)
        lb.pack(fill="both", expand=True, padx=6, pady=6)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(0, 6))
        e = ttk.Entry(row)
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Add", command=lambda: self._kw_add(e, label)).pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self._kw_add(e, label))
        ttk.Button(f, text="Remove selected", command=lambda: self._kw_remove(lb)).pack(pady=(0, 6))
        return lb

    def _kw_add(self, entry, label):
        if entry.get().strip():
            db.add_keyword(self.conn, entry.get(), label)
            entry.delete(0, "end")
            self._kw_reload()

    def _kw_remove(self, lb):
        for i in reversed(lb.curselection()):
            db.remove_keyword(self.conn, lb.get(i))
        self._kw_reload()

    def _kw_from_open(self, label):
        for i in self.lb_open.curselection():
            db.add_keyword(self.conn, self.lb_open.get(i).lower(), label)
        self._kw_reload()

    def _kw_reload(self):
        prod, neu = db.get_keywords(self.conn)
        for lb, items in ((self.lb_prod, prod), (self.lb_neu, neu)):
            lb.delete(0, "end")
            for k in items:
                lb.insert("end", k)
        titles = DEMO_TITLES if self.demo else ga.get_all_window_titles()
        self.lb_open.delete(0, "end")
        for a in sorted({ga.app_name(t) for t in titles if t and t.strip()}):
            self.lb_open.insert("end", a)

    # ---------- insights ----------
    def _tab_insights(self, p):
        charts = ttk.Frame(p)
        charts.pack(fill="x", padx=10, pady=(10, 4))
        for attr, title in (("cv_days", "Productive hours, last 14 days"), ("cv_hours", "Productive share by hour of day")):
            f = ttk.LabelFrame(charts, text=title)
            f.pack(side="left", padx=6)
            setattr(self, attr, tk.Canvas(f, width=360, height=170, bg="white", highlightthickness=0))
            getattr(self, attr).pack(padx=6, pady=6)
        row = ttk.Frame(p)
        row.pack(fill="x", padx=12, pady=4)
        ttk.Button(row, text="Refresh insights", command=self.refresh_insights).pack(side="left")
        self.btn_focus = ttk.Button(row, text="Use recommended focus length", command=self.apply_focus, state="disabled")
        self.btn_focus.pack(side="left", padx=6)
        self.btn_goal = ttk.Button(row, text="Use suggested weekly goal", command=self.apply_goal, state="disabled")
        self.btn_goal.pack(side="left")
        self.txt = tk.Text(p, height=12, wrap="word", state="disabled", relief="flat", bg="#f4f4f4")
        self.txt.pack(fill="both", expand=True, padx=12, pady=8)
        self._set_text("Press 'Refresh insights'. The learning features need a few days of data." if ml_coach else
                       "Insights need coach.py (or the older ml_coach.py) next to this file.\n"
                       "The charts above work without it.")

    def _set_text(self, s):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("end", s)
        self.txt.config(state="disabled")

    def draw_charts(self):
        today = datetime.now().date()
        days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
        got = dict(self.conn.execute("SELECT date, productive_secs FROM daily_summaries WHERE date>=?", (days[0],)))
        vals = [got.get(d, 0) / 3600 for d in days]
        goal = db.setting(self.conn, "daily_goal_secs") / 3600
        draw_bars(self.cv_days, [d[8:] if i % 2 == 0 else "" for i, d in enumerate(days)], vals, "#4caf50",
                  nice_max(max(max(vals), goal)), unit="h", hline=goal)
        by_hour = {h: (p, t) for h, p, t in self.conn.execute(
            "SELECT hour, SUM(productive_secs), SUM(total_secs) FROM v_slot_productivity GROUP BY hour")}
        ratios = [100 * by_hour[h][0] / by_hour[h][1] if h in by_hour and by_hour[h][1] else 0 for h in range(24)]
        draw_bars(self.cv_hours, [str(h) if h % 3 == 0 else "" for h in range(24)], ratios, "#1e88e5", 100, unit="%")

    def refresh_insights(self):
        self.draw_charts()
        if ml_coach is None:
            return
        self._set_text("Analysing your data...")
        wk = self.worker.weekly_goal or db.setting(self.conn, "weekly_goal_secs")
        dg = db.setting(self.conn, "daily_goal_secs")

        def job():
            try:
                self.ui_q.put(("insights", ml_coach.Coach(self.db_path, weekly_target_secs=wk, daily_goal_secs=dg).refresh()))
            except Exception as e:
                self.ui_q.put(("insights_error", repr(e)))
        threading.Thread(target=job, daemon=True).start()

    def show_insights(self, r):
        self.insights = r
        L = []
        ph = r["peak_hours"]
        if ph.get("status") == "ok":
            L.append("Best focus hours:  " + ",  ".join(f"{h['hour']}:00 ({h['score'] * 100:.0f}%)" for h in ph["top_hours"]))
            if ph["best_window"]:
                L.append(f"Best 2-hour window:  {ph['best_window']['start']}:00 - {ph['best_window']['end']}:00")
        else:
            L.append("Best focus hours:  not enough data yet (needs a few days of tracking).")
        sl = r["session_length"]
        if sl.get("status") == "ok":
            L.append(f"\nFocus stamina:  your focus usually fades after about {sl['typical_fade_min']:.0f} minutes. "
                     f"Suggested: {sl['recommended_focus_min']}-minute blocks with {sl['recommended_break_min']}-minute breaks.")
        else:
            L.append("\nFocus stamina:  not enough completed focus runs yet.")
        fc = r["forecast"]
        if fc.get("status") == "ok":
            L.append(f"\nThis week:  on pace for {hm(fc['forecast_secs'])} of your {hm(fc['target_secs'])} goal "
                     f"({fc['prob_hit_goal'] * 100:.0f}% chance of hitting it).")
        else:
            L.append("\nWeekly forecast:  needs about two weeks of history.")
        ng = r["next_goal"]
        if ng.get("status") == "ok":
            L.append(f"Suggested weekly goal:  {hm(ng['suggested_target_secs'])}  (you hit your goal in {ng['hit_rate'] * 100:.0f}% of recent weeks).")
        st = r["streak"]
        L.append(f"\nStreak:  {st['current_streak_days']} day(s) meeting your daily goal"
                 + (f";  consistency {st['consistency']:.2f} (1.0 = very steady)." if st["consistency"] is not None else "."))
        rm = r["risk_model"]
        if rm.get("status") == "ok":
            auc = rm.get("holdout_auc")
            L.append(f"\nDistraction model:  learned from {rm['rows']} moments" + (f", holdout AUC {auc}." if auc else "."))
            if r.get("risk_drivers"):
                L.append("Strongest signals:  " + ",  ".join(f"{n} ({c:+.1f})" for n, c in r["risk_drivers"][:3]))
            db.log_model_run(self.conn, "distraction_risk", rm["rows"], "auc", auc)
        else:
            L.append("\nDistraction model:  not enough data yet (needs a couple of weeks).")
        self._set_text("\n".join(L))
        self.btn_focus.config(state="normal" if sl.get("status") == "ok" else "disabled")
        self.btn_goal.config(state="normal" if ng.get("status") == "ok" else "disabled")

    def put_setting(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))
        self.conn.commit()

    def apply_focus(self):
        sl = self.insights["session_length"]
        self.put_setting("focus_minutes", sl["recommended_focus_min"])
        self.put_setting("break_minutes", sl["recommended_break_min"])
        self._load_settings()
        messagebox.showinfo("Saved", f"Focus blocks set to {sl['recommended_focus_min']} min, breaks to {sl['recommended_break_min']} min.")

    def apply_goal(self):
        secs = self.insights["next_goal"]["suggested_target_secs"]
        self.put_setting("weekly_goal_secs", secs)
        db.set_week_target(self.conn, db.monday_of(), secs)
        self._load_settings()
        messagebox.showinfo("Saved", f"Weekly goal set to {hm(secs)}.")

    # ---------- history ----------
    def _tab_history(self, p):
        cols = ("time", "app", "title", "length", "label")
        self.tree = ttk.Treeview(p, columns=cols, show="headings", selectmode="extended")
        for c, w in zip(cols, (130, 160, 330, 70, 100)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(p, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        row = ttk.Frame(p)
        row.pack(side="bottom", fill="x", padx=8, pady=6)
        ttk.Label(row, text="Mark selected sessions as:").pack(side="left")
        for lab, txt in [(1, "Productive"), (2, "Neutral"), (0, "Distraction")]:
            ttk.Button(row, text=txt, command=lambda l=lab: self.correct_selected(l)).pack(side="left", padx=3)
        ttk.Button(row, text="Reload", command=self.load_history).pack(side="right")
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="left", fill="y", pady=8)

    def load_history(self):
        self.tree.delete(*self.tree.get_children())
        for sid, ts, app, title, dur, lab in self.conn.execute(
                "SELECT id, timestamp, app, title, duration, label FROM sessions ORDER BY timestamp DESC LIMIT 300"):
            self.tree.insert("", "end", iid=str(sid), values=(
                datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M"), app, title or "", f"{dur // 60}m {dur % 60:02d}s", LABELS[lab][0].title()))

    def correct_selected(self, label):
        sel = self.tree.selection()
        if not sel:
            return
        n = sum(db.correct_session(self.conn, int(i), label, "fixed in history tab") for i in sel)
        if n:
            db.rebuild_all(self.conn)
        self.load_history()

    # ---------- settings ----------
    def _tab_settings(self, p):
        self.set_vars = {}
        f = ttk.Frame(p)
        f.pack(anchor="nw", padx=20, pady=16)
        for r, (key, label, scale, lo, hi, step) in enumerate(SETTING_FIELDS):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
            v = tk.StringVar()
            ttk.Spinbox(f, from_=lo, to=hi, increment=step, textvariable=v, width=8).grid(row=r, column=1, padx=12)
            self.set_vars[key] = v
        r = len(SETTING_FIELDS)
        for i, (key, label) in enumerate([("nudges_enabled", "Show reminders"), ("break_suggestions_enabled", "Suggest breaks")]):
            v = tk.IntVar()
            ttk.Checkbutton(f, text=label, variable=v).grid(row=r + i, column=0, sticky="w", pady=4)
            self.set_vars[key] = v
        ttk.Button(f, text="Save settings", command=self.save_settings).grid(row=r + 3, column=0, sticky="w", pady=14)
        self._load_settings()

    def _load_settings(self):
        for key, label, scale, *_ in SETTING_FIELDS:
            self.set_vars[key].set(f"{db.setting(self.conn, key) / scale:g}")
        for key in ("nudges_enabled", "break_suggestions_enabled"):
            self.set_vars[key].set(int(db.setting(self.conn, key)))

    def save_settings(self):
        try:
            new = {k: float(self.set_vars[k].get()) * s for k, _l, s, *_ in SETTING_FIELDS}
        except ValueError:
            messagebox.showerror("Invalid value", "Please enter numbers only.")
            return
        for k, v in new.items():
            self.put_setting(k, int(round(v)))
        for k in ("nudges_enabled", "break_suggestions_enabled"):
            self.put_setting(k, self.set_vars[k].get())
        db.set_week_target(self.conn, db.monday_of(), int(new["weekly_goal_secs"]))
        db.rebuild_daily_summaries(self.conn)
        messagebox.showinfo("Saved", "Settings saved. They take effect within a few seconds.")

    # ---------- misc ----------
    def export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile="sessions.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["start", "app", "title", "seconds", "label"])
            for ts, app, title, dur, lab in self.conn.execute("SELECT timestamp, app, title, duration, label FROM sessions ORDER BY timestamp"):
                w.writerow([datetime.fromtimestamp(ts).isoformat(" ", "seconds"), app, title, dur, LABELS[lab][0].lower()])
        messagebox.showinfo("Exported", f"Saved to {path}")

    def rebuild(self):
        db.rebuild_all(self.conn)
        self.draw_charts()
        messagebox.showinfo("Done", "Statistics rebuilt from your sessions.")

    def _on_tab(self, _e=None):
        name = self.nb.tab(self.nb.select(), "text")
        if name == "History":
            self.load_history()
        elif name == "Insights":
            self.draw_charts()
        elif name == "Keywords":
            self._kw_reload()
        elif name == "Settings":
            self._load_settings()

    def toast(self, title, msg):
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        f = tk.Frame(t, bg="#263238", padx=14, pady=10)
        f.pack()
        tk.Label(f, text=title, fg="white", bg="#263238", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        tk.Label(f, text=msg, fg="#cfd8dc", bg="#263238", wraplength=300, justify="left").pack(anchor="w")
        t.update_idletasks()
        t.geometry(f"+{self.root.winfo_screenwidth() - t.winfo_width() - 24}+{self.root.winfo_screenheight() - t.winfo_height() - 70}")
        for w in (t, f) + tuple(f.winfo_children()):
            w.bind("<Button-1>", lambda _e: t.destroy())
        t.after(9000, lambda: t.winfo_exists() and t.destroy())

    def _tick(self):
        try:
            while True:
                kind, *data = self.worker.events.get_nowait()
                if kind == "toast":
                    self.toast(*data)
                elif kind == "error":
                    self.hint.config(text=f"Tracker problem (it will keep retrying): {data[0]}")
        except queue.Empty:
            pass
        try:
            while True:
                kind, data = self.ui_q.get_nowait()
                if kind == "insights":
                    self.show_insights(data)
                else:
                    self._set_text(f"Could not compute insights: {data}")
        except queue.Empty:
            pass
        self._update_dashboard()
        self.root.after(1000, self._tick)

    def quit(self, ask=True):
        if ask and not messagebox.askyesno("Quit", "Quitting stops tracking. Quit now?"):
            return
        self.worker.stop_flag.set()
        self.worker.join(timeout=5)
        self.root.destroy()


# ───────────────────────── demo helpers ─────────────────────────
class DemoSource:
    def __init__(self):
        self.cur, self.next = random.choice(DEMO_TITLES), 0

    def __call__(self):
        if time.time() >= self.next:
            self.cur, self.next = random.choice(DEMO_TITLES), time.time() + random.uniform(6, 18)
        return self.cur


def seed_demo(conn, days=28):
    """Four weeks of believable history: mornings focused, afternoons drift."""
    if conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]:
        return
    random.seed(7)
    db.set_keywords(conn, ["visual studio code", "notion"], ["slack", "outlook"])
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    for d in range(days):
        day = start + timedelta(days=d)
        if day.weekday() >= 5 and random.random() < 0.6:
            continue
        for hour, focus in [(9, 40), (10, 38), (14, 22), (15, 20), (20, 15)]:
            t = day.replace(hour=hour, minute=random.randint(0, 10)).timestamp()
            end = t + 3000
            while t < end:
                fl = max(300, random.gauss(focus, 5) * 60)
                db.insert_session(conn, "Visual Studio Code", "main.py - garden", t, fl, 1)
                t += fl
                dl = max(90, random.gauss(6 if hour < 12 else 10, 2) * 60)
                db.insert_session(conn, "Google Chrome", "Cat compilation - YouTube", t, dl, 0)
                t += dl
    db.rebuild_all(conn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--db")
    a = ap.parse_args()
    root = tk.Tk()
    db_path = a.db or ("./data/demo.db" if a.demo else ga.DB_PATH)
    if a.demo:
        seed_demo(db.connect(db_path))
        source = DemoSource()
    elif ga.gw is None:
        root.withdraw()
        messagebox.showerror("Missing package", "Window tracking needs pygetwindow (Windows).\n\n"
                             "Install it with:  pip install pygetwindow\n\nOr run with --demo to look around.")
        sys.exit(1)
    else:
        source = ga.get_active_window_title
    App(root, db_path, source, demo=a.demo)
    root.mainloop()


if __name__ == "__main__":
    main()

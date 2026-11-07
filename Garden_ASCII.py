"""
Garden_ASCII.py - active-window productivity tracker (SQLite edition).

Same flow as before: list windows -> enter keywords -> track until Ctrl+C.
What changed: instead of two running totals, every contiguous stretch in one
app with one label is stored as a SESSION row, which is what the ML needs.

Labels: 1 = productive, 2 = neutral, 0 = distraction (anything unmatched)
"""
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

try:
    import pygetwindow as gw
except ImportError:
    gw = None

import db

DB_PATH = os.getenv("TRACKER_DB", "./data/tracker.db")
DATA_DIR = Path(DB_PATH).parent
KEYWORDS_FILE = DATA_DIR / "keywords.json"
TOKEN_FILE = DATA_DIR / "token.txt"
API_URL = os.getenv("TRACKER_API", "http://127.0.0.1:8765")

POLL_SECS = 1
WARN_AFTER_SECS = 300     # your old code warned at 30 ticks but printed "5 minutes"; use 30 to test
MIN_SESSION_SECS = 2      # ignore alt-tab flicker
CHECKPOINT_SECS = 60      # save long sessions as they go, so a crash loses at most 60s
MAX_GAP_SECS = 10         # a longer gap between polls means sleep/lock: don't count it
REBUILD_SECS = 300        # refresh daily summaries while running


# ───────────────────────── window helpers ─────────────────────────
def get_active_window_title():
    try:
        return gw.getActiveWindow().title
    except Exception:
        return None


def get_all_window_titles():
    try:
        return gw.getAllTitles()
    except Exception:
        return []


def app_name(title):
    """'main.py - proj - Visual Studio Code' -> 'Visual Studio Code'. Falls back to the title."""
    for sep in (" - ", " \u2014 ", " \u2013 "):
        if sep in title:
            return title.rsplit(sep, 1)[-1].strip()
    return title.strip()


def fmt_secs(s):
    return f"{int(s) // 60} minutes" if s >= 60 else f"{int(s)} seconds"


# ───────────────────────── dashboard notification ─────────────────────────
def notify_api(app, label):
    """Tell the dashboard backend about a switch. Best effort: tracking never depends on it."""
    def _post():
        try:
            req = urllib.request.Request(
                API_URL + "/switch",
                data=json.dumps({"app": app, "label": label}).encode(),
                headers={"Content-Type": "application/json", "X-Token": TOKEN_FILE.read_text().strip()},
                method="POST")
            urllib.request.urlopen(req, timeout=1).read()
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()


# ───────────────────────── the tracker ─────────────────────────
class Tracker:
    def __init__(self, conn, productive, neutral=(), on_warn=print, on_switch=None):
        self.conn, self.productive, self.neutral = conn, list(productive), list(neutral)
        self.on_warn, self.on_switch = on_warn, on_switch
        self.cur = None                       # session in progress
        self.last_tick = None
        self.totals = {1: 0.0, 2: 0.0, 0: 0.0}
        self.nonprod_run, self.warned = 0.0, False

    def label_for(self, title):
        t = title.lower()
        if any(k in t for k in self.productive):
            return 1
        if any(k in t for k in self.neutral):
            return 2
        return 0

    def tick(self, title, now):
        gap = None if self.last_tick is None else now - self.last_tick
        if self.cur and gap is not None and gap > MAX_GAP_SECS:
            self._close(self.last_tick + POLL_SECS)       # slept/locked: end where we last saw it
        dt = 0.0 if gap is None else min(gap, 2.0)
        self.last_tick = now

        if not title:                                     # no active window (desktop, lock screen)
            self._close(now)
            return

        app, label = app_name(title), self.label_for(title)
        self.totals[label] += dt
        self._warn_check(label, dt)

        if self.cur and self.cur["app"] == app and self.cur["label"] == label:
            if now - self.cur["flushed_at"] >= CHECKPOINT_SECS:
                self._flush(now)
            return
        self._close(now)
        self.cur = {"app": app, "title": title, "label": label, "start": now,
                    "row_id": None, "flushed_at": now}
        if self.on_switch:
            self.on_switch(app, label)

    def _warn_check(self, label, dt):
        if label == 1:
            self.nonprod_run, self.warned = 0.0, False
        elif label == 0:
            self.nonprod_run += dt
            if self.nonprod_run >= WARN_AFTER_SECS and not self.warned:
                self.warned = True
                self.on_warn(f"WARNING YOU HAVE BEEN UNPRODUCTIVE FOR {fmt_secs(WARN_AFTER_SECS).upper()}")

    def _flush(self, end):
        c = self.cur
        dur = int(end - c["start"])
        if dur < MIN_SESSION_SECS:
            return
        if c["row_id"] is None:
            c["row_id"] = db.insert_session(self.conn, c["app"], c["title"], c["start"], dur, c["label"])
        else:
            db.update_session_duration(self.conn, c["row_id"], dur)
        c["flushed_at"] = end

    def _close(self, end):
        if self.cur:
            self._flush(end)
            self.cur = None

    def stop(self, now):
        self._close(now)


def rebuild_all(conn):
    db.rebuild_all(conn)          # focus runs, daily summaries, streaks, weekly goal progress


# ───────────────────────── keyword entry ─────────────────────────
def ask_keywords(label):
    print(f"\nEnter keywords for {label} windows (type 0 to finish):")
    out = []
    while True:
        kw = input("> ").strip().lower()
        if kw == "0":
            return out
        if kw:
            out.append(kw)


def load_keywords():
    try:
        d = json.loads(KEYWORDS_FILE.read_text())
        return d["productive"], d.get("neutral", [])
    except Exception:
        return None


def main():
    if gw is None:
        sys.exit("pygetwindow is required:  pip install pygetwindow  (full support is Windows-only)")

    print("\nAll current window titles:\n")
    seen = set()
    for title in get_all_window_titles():
        if title and title.strip() and title not in seen:
            print(f"- {title}")
            seen.add(title)

    saved = load_keywords()
    choice = "r"
    if saved:
        choice = input(f"\nSaved keywords:\n  productive: {saved[0]}\n  neutral:    {saved[1]}\n"
                       "[U]se them / [A]dd more / [R]e-enter all? [U/a/r] ").strip().lower()[:1] or "u"
    if saved and choice not in ("a", "r"):
        productive, neutral = saved
    else:
        old_p, old_n = saved if (saved and choice == "a") else ([], [])
        new_p = ask_keywords("PRODUCTIVE" + (" to add" if old_p or old_n else ""))
        new_n = ask_keywords("NEUTRAL, optional, e.g. slack, email")
        productive = list(dict.fromkeys([k for k in old_p if k not in new_n] + new_p))
        neutral = list(dict.fromkeys([k for k in old_n if k not in new_p] + new_n))
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        KEYWORDS_FILE.write_text(json.dumps({"productive": productive, "neutral": neutral}))

    print("\nProductive keywords:", productive)
    print("Neutral keywords:", neutral)

    conn = db.connect(DB_PATH)
    tracker = Tracker(conn, productive, neutral, on_switch=notify_api)
    print("\nTracking productivity... Press Ctrl+C to stop.\n")

    last_rebuild = time.time()
    try:
        while True:
            now = time.time()
            tracker.tick(get_active_window_title(), now)
            if now - last_rebuild >= REBUILD_SECS:
                db.rebuild_daily_summaries(conn)
                last_rebuild = now
            time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop(time.time())
        rebuild_all(conn)
        t = tracker.totals
        print(f"you were productive for {t[1]:.2f} seconds")
        if neutral:
            print(f"you were neutral for {t[2]:.2f} seconds")
        print(f"you were not productive for {t[0]:.2f} seconds")
        print(f"total time elapsed this session: {sum(t.values()):.2f} seconds")
        print("Stopped tracking.")


if __name__ == "__main__":
    main()

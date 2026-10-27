"""
inspect_db.py - see what's stored in tracker.db (read-only, safe while the tracker is running).

  python inspect_db.py                      summary report
  python inspect_db.py --table sessions     last 20 rows of any table
  python inspect_db.py --table nudges -n 50
  python inspect_db.py --db path/to/tracker.db
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime


def fmt(secs):
    secs = int(secs or 0)
    return f"{secs // 3600}h {secs % 3600 // 60:02d}m" if secs >= 3600 else f"{secs // 60}m {secs % 60:02d}s"


def when(ts):
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")


def q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return None                                   # table/column not in this schema version


def section(title):
    print(f"\n{'─' * 4} {title} {'─' * max(2, 60 - len(title))}")


def show(rows, fmt_row, empty="(nothing yet)"):
    if rows is None:
        print("(not in this schema version)")
    elif not rows:
        print(empty)
    else:
        for r in rows:
            print("  " + fmt_row(r))


def dump_table(conn, name, n):
    if not q(conn, "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)):
        sys.exit(f"no table or view called '{name}'")
    cur = conn.execute(f"SELECT * FROM {name} ORDER BY rowid DESC LIMIT ?", (n,))
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()[::-1]
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join("" if v is None else str(v) for v in r))
    print(f"\n({len(rows)} rows shown, newest last)")


def report(conn, path):
    section("file")
    print(f"  {os.path.abspath(path)}  ({os.path.getsize(path) / 1024:.0f} KB)")
    print(f"  schema version: {conn.execute('PRAGMA user_version').fetchone()[0]}")

    section("row counts")
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        print(f"  {t:<18}{conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>8}")

    section("data range")
    r = q(conn, "SELECT MIN(timestamp), MAX(timestamp), COUNT(DISTINCT date) FROM sessions")
    if r and r[0][0]:
        print(f"  {when(r[0][0])}  ->  {when(r[0][1])}   ({r[0][2]} days with data)")
    else:
        print("  no sessions recorded yet")

    section("latest 10 sessions")
    label = {1: "productive", 0: "distraction", 2: "neutral"}
    show(q(conn, "SELECT timestamp, app, title, duration, label FROM sessions ORDER BY timestamp DESC LIMIT 10"),
         lambda r: f"{when(r[0])}  {fmt(r[3]):>8}  {label.get(r[4], '?'):<11} {r[1]}  [{(r[2] or '')[:30]}]")

    section("top apps (all time)")
    show(q(conn, "SELECT app, label, SUM(duration) s FROM sessions GROUP BY app, label ORDER BY s DESC LIMIT 8"),
         lambda r: f"{fmt(r[2]):>9}  {label.get(r[1], '?'):<11} {r[0]}")

    section("last 7 days")
    show(q(conn, "SELECT date, productive_secs, distraction_secs, neutral_secs, goal_secs, goal_met "
                 "FROM daily_summaries ORDER BY date DESC LIMIT 7"),
         lambda r: f"{r[0]}  productive {fmt(r[1]):>8}  distraction {fmt(r[2]):>8}  neutral {fmt(r[3]):>8}"
                   f"  goal {fmt(r[4])} {'MET' if r[5] else '-'}")
    if not q(conn, "SELECT 1 FROM daily_summaries LIMIT 1"):
        print("  (empty: run db.rebuild_all(conn) to build daily summaries from sessions)")

    section("streaks + weekly goal")
    show(q(conn, "SELECT start_date, end_date, length_days, is_current FROM streaks ORDER BY start_date DESC LIMIT 3"),
         lambda r: f"{r[0]} -> {r[1]}  {r[2]} days {'(current)' if r[3] else ''}")
    show(q(conn, "SELECT week, target_secs, achieved_secs, met, forecast_secs, prob_hit FROM goals "
                 "ORDER BY week DESC LIMIT 3"),
         lambda r: f"week of {r[0]}  target {fmt(r[1])}  achieved {fmt(r[2])}  "
                   f"forecast {fmt(r[4]) if r[4] else 'n/a'}  P(hit) {r[5] if r[5] is not None else 'n/a'}")

    section("coaching: nudges and breaks")
    show(q(conn, "SELECT arm, COUNT(*), ROUND(AVG(reward), 2) FROM nudges WHERE reward IS NOT NULL GROUP BY arm"),
         lambda r: f"{r[0]:<14} sent {r[1]:>4}   response rate {r[2]}", empty="(no resolved nudges yet)")
    b = q(conn, "SELECT COUNT(*), SUM(taken) FROM breaks WHERE taken IS NOT NULL")
    print(f"  breaks resolved: {b[0][0]}   taken: {b[0][1] or 0}" if b and b[0][0] else "  (no breaks resolved yet)")

    section("settings")
    show(q(conn, "SELECT key, value FROM settings ORDER BY key"), lambda r: f"{r[0]:<28}{r[1]}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.getenv("TRACKER_DB", "./data/tracker.db"))
    p.add_argument("--table")
    p.add_argument("-n", type=int, default=20)
    a = p.parse_args()
    if not os.path.exists(a.db):
        sys.exit(f"database not found: {os.path.abspath(a.db)}\n"
                 "Run from the folder the tracker ran in, or pass --db / set TRACKER_DB.")
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)      # read-only: can't change anything
    if a.table:
        dump_table(conn, a.table, a.n)
    else:
        report(conn, a.db)


if __name__ == "__main__":
    main()

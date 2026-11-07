"""
db.py - complete SQLite storage for the productivity tracker (stdlib only).

TRACKERS (tables)
  Raw signal      sessions          every contiguous stretch in one app with one label
                  idle_periods      time the user was away (keyboard/mouse idle)
  Derived         focus_runs        consecutive on-task sessions        -> session length
                  daily_summaries   one row per day                     -> streaks, forecast
                  streaks           streak history
                  goals             weekly target + progress + forecast
  Coaching        breaks            suggested breaks and whether they were taken
                  nudges            every reminder sent + outcome       -> timing bandit
                  bandit_arms       learned nudge policy (Beta posteriors)
  User input      keywords          what counts as productive / neutral
                  corrections       "that wasn't a distraction" feedback
  Housekeeping    settings          goals, quiet hours, idle threshold, ...
                  insights_cache    model output for the dashboard
                  model_runs        training history / model health
  Views           v_slot_productivity, v_weekly_totals

Labels: 1 = productive, 0 = distraction, 2 = neutral
Schema changes are applied by numbered, re-runnable migrations (PRAGMA user_version).
"""
import json
import os
import random
import sqlite3
from datetime import date, datetime, timedelta

# ───────────────────────── migration 1: original trackers ─────────────────────────
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sessions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    app       TEXT    NOT NULL,
    title     TEXT,                                   -- truncated window title
    timestamp INTEGER NOT NULL,                       -- unix epoch, session start
    duration  INTEGER NOT NULL,                       -- seconds
    label     INTEGER NOT NULL CHECK (label IN (0,1,2)),
    date      TEXT    NOT NULL,                       -- LOCAL 'YYYY-MM-DD'
    hour      INTEGER NOT NULL,                       -- local 0-23
    weekday   INTEGER NOT NULL                        -- 0=Mon .. 6=Sun
);
CREATE INDEX IF NOT EXISTS idx_sessions_ts     ON sessions(timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_date   ON sessions(date);
CREATE INDEX IF NOT EXISTS idx_sessions_hr_lbl ON sessions(hour, label);

CREATE TABLE IF NOT EXISTS focus_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts             INTEGER NOT NULL UNIQUE,
    secs                 INTEGER NOT NULL,
    ended_by_distraction INTEGER NOT NULL,
    date                 TEXT NOT NULL,
    hour                 INTEGER NOT NULL,
    weekday              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_hour ON focus_runs(hour);

CREATE TABLE IF NOT EXISTS daily_summaries (
    date               TEXT PRIMARY KEY,
    productive_secs    INTEGER NOT NULL DEFAULT 0,
    distraction_secs   INTEGER NOT NULL DEFAULT 0,
    neutral_secs       INTEGER NOT NULL DEFAULT 0,
    total_secs         INTEGER NOT NULL DEFAULT 0,
    session_count      INTEGER NOT NULL DEFAULT 0,
    avg_session_secs   INTEGER NOT NULL DEFAULT 0,
    context_switches   INTEGER NOT NULL DEFAULT 0,
    goal_secs          INTEGER NOT NULL DEFAULT 0,
    goal_met           INTEGER NOT NULL DEFAULT 0,
    productivity_score REAL    NOT NULL DEFAULT 0      -- written by the ML layer
);

CREATE TABLE IF NOT EXISTS streaks (
    start_date  TEXT PRIMARY KEY,
    end_date    TEXT NOT NULL,
    length_days INTEGER NOT NULL,
    is_current  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS goals (
    week          TEXT PRIMARY KEY,                   -- Monday 'YYYY-MM-DD'
    target_secs   INTEGER NOT NULL,
    achieved_secs INTEGER NOT NULL DEFAULT 0,
    met           INTEGER NOT NULL DEFAULT 0,
    forecast_secs INTEGER,
    prob_hit      REAL,
    updated_at    INTEGER
);

CREATE TABLE IF NOT EXISTS nudges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at       INTEGER NOT NULL,
    context_key   TEXT    NOT NULL,
    arm           TEXT    NOT NULL,
    policy        TEXT    NOT NULL,                   -- 'thompson' | 'random'
    hour          INTEGER NOT NULL,
    weekday       INTEGER NOT NULL,
    run_min       REAL,
    risk          REAL,
    switches_10m  INTEGER,
    responded     INTEGER,                            -- NULL until resolved
    response_secs INTEGER,
    reward        REAL,
    resolved_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_nudges_sent ON nudges(sent_at);

CREATE TABLE IF NOT EXISTS bandit_arms (
    context_key TEXT NOT NULL,
    arm         TEXT NOT NULL,
    alpha       REAL NOT NULL DEFAULT 1,
    beta        REAL NOT NULL DEFAULT 1,
    pulls       INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER,
    PRIMARY KEY (context_key, arm)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS insights_cache (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL
);
"""

# ───────────────────────── migration 2: idle, breaks, keywords, feedback, models ─────
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS idle_periods (             -- away from keyboard/mouse
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts INTEGER NOT NULL UNIQUE,
    secs     INTEGER NOT NULL,
    date     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idle_date ON idle_periods(date);

CREATE TABLE IF NOT EXISTS breaks (                   -- suggested breaks and what the user did
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    suggested_at      INTEGER NOT NULL,
    reason            TEXT    NOT NULL,               -- 'fade_point' | 'high_risk' | 'scheduled'
    planned_secs      INTEGER NOT NULL,
    run_min           REAL,                           -- focus-run length when suggested
    taken             INTEGER,                        -- NULL until resolved; 1 taken, 0 skipped
    started_at        INTEGER,
    actual_secs       INTEGER,
    returned_on_task  INTEGER                         -- 1 if the session after the break was productive
);
CREATE INDEX IF NOT EXISTS idx_breaks_ts ON breaks(suggested_at);

CREATE TABLE IF NOT EXISTS keywords (                 -- replaces keywords.json
    keyword TEXT PRIMARY KEY,                         -- stored lowercase
    label   INTEGER NOT NULL CHECK (label IN (1,2))   -- 1 productive, 2 neutral; no match = 0
);

CREATE TABLE IF NOT EXISTS corrections (              -- user fixes the app's labelling
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    old_label  INTEGER NOT NULL,
    new_label  INTEGER NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS model_runs (               -- is each model healthy / improving?
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,                    -- 'distraction_risk', 'peak_hours', ...
    trained_at   INTEGER NOT NULL,
    rows_used    INTEGER,
    metric_name  TEXT,
    metric_value REAL,
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_runs ON model_runs(name, trained_at);

CREATE VIEW IF NOT EXISTS v_slot_productivity AS      -- feeds peak-hour prediction
SELECT weekday, hour,
       SUM(CASE WHEN label=1 THEN duration ELSE 0 END) AS productive_secs,
       SUM(duration)                                   AS total_secs,
       ROUND(1.0 * SUM(CASE WHEN label=1 THEN duration ELSE 0 END) / SUM(duration), 3) AS productive_ratio
FROM sessions GROUP BY weekday, hour;

CREATE VIEW IF NOT EXISTS v_weekly_totals AS          -- feeds goal forecasting
SELECT date(date, '-6 days', 'weekday 1') AS week,
       SUM(productive_secs)  AS productive_secs,
       SUM(distraction_secs) AS distraction_secs,
       COUNT(*)              AS days_tracked
FROM daily_summaries GROUP BY week;
"""


def _m1(conn):
    conn.executescript(SCHEMA_V1)


def _m2(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_summaries)")}
    if "idle_secs" not in cols:
        conn.execute("ALTER TABLE daily_summaries ADD COLUMN idle_secs INTEGER NOT NULL DEFAULT 0")
    conn.executescript(SCHEMA_V2)


MIGRATIONS = [_m1, _m2]          # append _m3, _m4... later; never edit an old one


def migrate(conn):
    """Bring any database (new, or created by an older version) up to date. Safe to re-run."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, step in enumerate(MIGRATIONS[version:], start=version + 1):
        step(conn)
        conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()


DEFAULT_SETTINGS = {
    "nudges_enabled": 1, "max_nudges_per_hour": 3, "cooldown_min": 10,
    "quiet_start_hour": 22, "quiet_end_hour": 7,
    "daily_goal_secs": 10800, "weekly_goal_secs": 72000,
    "idle_threshold_secs": 300,            # no input this long = idle, stop counting
    "break_suggestions_enabled": 1,
    "focus_minutes": 45,                   # on-task time before a break is suggested
    "break_minutes": 5,                    # suggested break length
    "warn_after_secs": 300,                # off-task time before a refocus reminder
}


def connect(path="./data/tracker.db"):
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")          # tracker writes while ML reads
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    for k, v in DEFAULT_SETTINGS.items():              # new defaults appear on upgrade
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, str(v)))
    conn.commit()
    return conn


def setting(conn, key):
    """A setting as a number. A missing row falls back to its default, so a database (or a db.py)
    from an older version never crashes a newer one."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(f"unknown setting: {key}")
        return float(DEFAULT_SETTINGS[key])
    return float(row[0])


def _day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


# ───────────────────────── sessions + idle ─────────────────────────
def insert_session(conn, app, title, ts, duration, label):
    dt = datetime.fromtimestamp(ts)
    cur = conn.execute(
        "INSERT INTO sessions (app,title,timestamp,duration,label,date,hour,weekday) VALUES (?,?,?,?,?,?,?,?)",
        (app, (title or "")[:40], int(ts), int(duration), label, dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday()),
    )
    conn.commit()
    return cur.lastrowid


def update_session_duration(conn, session_id, duration):
    """Checkpoint a session that is still in progress."""
    conn.execute("UPDATE sessions SET duration=? WHERE id=?", (int(duration), session_id))
    conn.commit()


def log_idle(conn, start_ts, secs):
    """Record an away-from-keyboard period. Re-logging the same start just updates its length."""
    conn.execute(
        "INSERT INTO idle_periods (start_ts, secs, date) VALUES (?,?,?) "
        "ON CONFLICT(start_ts) DO UPDATE SET secs=excluded.secs",
        (int(start_ts), int(secs), _day(start_ts)))
    conn.commit()


# ───────────────────────── keywords (productive / neutral rules) ─────────────────────────
def set_keywords(conn, productive, neutral=()):
    conn.execute("DELETE FROM keywords")
    rows = [(k.strip().lower(), 1) for k in productive if k.strip()]
    rows += [(k.strip().lower(), 2) for k in neutral if k.strip()]
    conn.executemany("INSERT OR REPLACE INTO keywords VALUES (?,?)", rows)
    conn.commit()


def add_keyword(conn, keyword, label):
    conn.execute("INSERT OR REPLACE INTO keywords VALUES (?,?)", (keyword.strip().lower(), label))
    conn.commit()


def remove_keyword(conn, keyword):
    conn.execute("DELETE FROM keywords WHERE keyword=?", (keyword.strip().lower(),))
    conn.commit()


def get_keywords(conn):
    """Returns (productive_list, neutral_list)."""
    rows = conn.execute("SELECT keyword, label FROM keywords ORDER BY keyword").fetchall()
    return [r[0] for r in rows if r[1] == 1], [r[0] for r in rows if r[1] == 2]


def import_keywords_json(conn, path):
    """One-time import of the old keywords.json."""
    with open(path) as f:
        d = json.load(f)
    set_keywords(conn, d.get("productive", []), d.get("neutral", []))


# ───────────────────────── user corrections ─────────────────────────
def correct_session(conn, session_id, new_label, note=None):
    """User says 'that was actually productive/neutral'. Keeps an audit trail; rebuild afterwards."""
    row = conn.execute("SELECT label FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None or row[0] == new_label:
        return False
    conn.execute("UPDATE sessions SET label=? WHERE id=?", (new_label, session_id))
    conn.execute("INSERT INTO corrections (ts, session_id, old_label, new_label, note) VALUES (?,?,?,?,?)",
                 (int(datetime.now().timestamp()), session_id, row[0], new_label, note))
    conn.commit()
    return True


# ───────────────────────── breaks ─────────────────────────
def suggest_break(conn, ts, reason, planned_secs, run_min=None):
    cur = conn.execute("INSERT INTO breaks (suggested_at, reason, planned_secs, run_min) VALUES (?,?,?,?)",
                       (int(ts), reason, int(planned_secs), run_min))
    conn.commit()
    return cur.lastrowid


def resolve_break(conn, break_id, taken, started_at=None, actual_secs=None, returned_on_task=None):
    conn.execute("UPDATE breaks SET taken=?, started_at=?, actual_secs=?, returned_on_task=? WHERE id=?",
                 (int(taken), started_at, actual_secs,
                  None if returned_on_task is None else int(returned_on_task), break_id))
    conn.commit()


def break_stats(conn, days=30):
    """Do suggested breaks get taken, and do they help? Feeds break-length tuning."""
    since = int((datetime.now() - timedelta(days=days)).timestamp())
    r = conn.execute(
        "SELECT COUNT(*), SUM(taken), SUM(CASE WHEN taken=1 AND returned_on_task=1 THEN 1 ELSE 0 END), "
        "AVG(CASE WHEN taken=1 THEN actual_secs END) FROM breaks WHERE suggested_at >= ? AND taken IS NOT NULL",
        (since,)).fetchone()
    n, taken, good, avg = r[0], r[1] or 0, r[2] or 0, r[3]
    return {"resolved": n, "taken_rate": round(taken / n, 2) if n else None,
            "return_on_task_rate": round(good / taken, 2) if taken else None,
            "avg_break_secs": None if avg is None else int(avg)}


# ───────────────────────── model health ─────────────────────────
def log_model_run(conn, name, rows_used=None, metric_name=None, metric_value=None, notes=None):
    conn.execute("INSERT INTO model_runs (name, trained_at, rows_used, metric_name, metric_value, notes) "
                 "VALUES (?,?,?,?,?,?)",
                 (name, int(datetime.now().timestamp()), rows_used, metric_name, metric_value, notes))
    conn.commit()


def latest_model_runs(conn):
    return [dict(r) for r in conn.execute(
        "SELECT m.* FROM model_runs m JOIN (SELECT name, MAX(trained_at) t FROM model_runs GROUP BY name) x "
        "ON m.name = x.name AND m.trained_at = x.t ORDER BY m.name")]


# ───────────────────────── weekly goals ─────────────────────────
def monday_of(d=None):
    d = d or date.today()
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def ensure_week_goal(conn, week=None, target=None):
    week = week or monday_of()
    conn.execute("INSERT OR IGNORE INTO goals (week, target_secs) VALUES (?,?)",
                 (week, int(target or setting(conn, "weekly_goal_secs"))))
    conn.commit()
    return week


def set_week_target(conn, week, target_secs):
    """Apply the adaptive-goal suggestion (or a manual change) to a week."""
    ensure_week_goal(conn, week, target_secs)
    conn.execute("UPDATE goals SET target_secs=? WHERE week=?", (int(target_secs), week))
    conn.commit()


def save_forecast(conn, week, forecast_secs, prob_hit):
    conn.execute("UPDATE goals SET forecast_secs=?, prob_hit=?, updated_at=? WHERE week=?",
                 (int(forecast_secs), prob_hit, int(datetime.now().timestamp()), week))
    conn.commit()


def update_weekly_progress(conn):
    """Recompute achieved_secs / met for every goal row from daily_summaries."""
    conn.execute("""
        UPDATE goals SET achieved_secs = (
            SELECT COALESCE(SUM(d.productive_secs), 0) FROM daily_summaries d
            WHERE d.date >= goals.week AND d.date < date(goals.week, '+7 days'))
    """)
    conn.execute("UPDATE goals SET met = (achieved_secs >= target_secs)")
    conn.commit()


# ───────────────────────── nightly rebuilds ─────────────────────────
def rebuild_focus_runs(conn, days=90, max_gap=120):
    since = int((datetime.now() - timedelta(days=days)).timestamp())
    rows = conn.execute(
        "SELECT timestamp, duration, label FROM sessions WHERE timestamp >= ? ORDER BY timestamp", (since,)
    ).fetchall()
    runs, cur, last_end = [], None, None

    def close(by_distraction):
        nonlocal cur
        if cur and cur["prod"]:
            runs.append((cur["start"], cur["secs"], int(by_distraction)))
        cur = None

    for ts, dur, lab in rows:
        if lab == 0:
            close(True)
            last_end = ts + dur
            continue
        if cur and ts - last_end > max_gap:
            close(False)
        if cur is None:
            cur = {"start": ts, "secs": 0, "prod": False}
        cur["secs"] += dur
        cur["prod"] = cur["prod"] or lab == 1
        last_end = ts + dur
    close(False)

    conn.execute("DELETE FROM focus_runs WHERE start_ts >= ?", (since,))
    for start, secs, by in runs:
        dt = datetime.fromtimestamp(start)
        conn.execute(
            "INSERT OR REPLACE INTO focus_runs (start_ts,secs,ended_by_distraction,date,hour,weekday) VALUES (?,?,?,?,?,?)",
            (start, secs, by, dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday()))
    conn.commit()
    return len(runs)


def rebuild_daily_summaries(conn, days=90, restamp_goals=False):
    """
    Goals are stamped per day the first time a day is summarised, so changing your goal later
    does not rewrite history (past streaks stay honest). restamp_goals=True overrides that.
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn.execute("""
        INSERT INTO daily_summaries
            (date, productive_secs, distraction_secs, neutral_secs, total_secs,
             session_count, avg_session_secs, context_switches)
        SELECT date,
               COALESCE(SUM(CASE WHEN label=1 THEN duration END),0),
               COALESCE(SUM(CASE WHEN label=0 THEN duration END),0),
               COALESCE(SUM(CASE WHEN label=2 THEN duration END),0),
               SUM(duration), COUNT(*), CAST(AVG(duration) AS INT), COUNT(*) - 1
        FROM sessions WHERE date >= ? GROUP BY date
        ON CONFLICT(date) DO UPDATE SET
            productive_secs=excluded.productive_secs, distraction_secs=excluded.distraction_secs,
            neutral_secs=excluded.neutral_secs, total_secs=excluded.total_secs,
            session_count=excluded.session_count, avg_session_secs=excluded.avg_session_secs,
            context_switches=excluded.context_switches
    """, (since,))
    conn.execute("""
        UPDATE daily_summaries SET idle_secs = COALESCE(
            (SELECT SUM(secs) FROM idle_periods i WHERE i.date = daily_summaries.date), 0)
    """)
    goal = int(setting(conn, "daily_goal_secs"))
    today = datetime.now().strftime("%Y-%m-%d")
    if restamp_goals:
        conn.execute("UPDATE daily_summaries SET goal_secs=?", (goal,))
    else:
        conn.execute("UPDATE daily_summaries SET goal_secs=? WHERE goal_secs=0 OR date=?", (goal, today))
    conn.execute("UPDATE daily_summaries SET goal_met = (goal_secs > 0 AND productive_secs >= goal_secs)")
    conn.commit()


def rebuild_streaks(conn):
    days = [r[0] for r in conn.execute("SELECT date FROM daily_summaries WHERE goal_met=1 ORDER BY date")]
    conn.execute("DELETE FROM streaks")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    prev, spans = None, []
    for d in days:
        dd = datetime.strptime(d, "%Y-%m-%d")
        if prev and (dd - prev).days == 1:
            spans[-1][1] = d
        else:
            spans.append([d, d])
        prev = dd
    for s, e in spans:
        n = (datetime.strptime(e, "%Y-%m-%d") - datetime.strptime(s, "%Y-%m-%d")).days + 1
        conn.execute("INSERT INTO streaks VALUES (?,?,?,?)", (s, e, n, int(e >= yesterday)))
    conn.commit()


def rebuild_all(conn):
    """Run nightly (and on exit): runs -> daily -> streaks -> weekly goal progress."""
    rebuild_focus_runs(conn)
    rebuild_daily_summaries(conn)
    rebuild_streaks(conn)
    ensure_week_goal(conn)
    update_weekly_progress(conn)


# ───────────────────────── nudge timing bandit ─────────────────────────
ARMS = ["break@run20", "break@run35", "goal@risk", "block@risk", "none"]


def context_key(ts):
    dt = datetime.fromtimestamp(ts)
    h = dt.hour
    part = ("morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17
            else "evening" if 17 <= h < 23 else "night")
    return f"{'wk' if dt.weekday() < 5 else 'we'}_{part}"


def can_nudge(conn, ts):
    """Hard guardrails outside the learner: quiet hours, cooldown, hourly cap, master switch."""
    if not setting(conn, "nudges_enabled"):
        return False
    h, qs, qe = datetime.fromtimestamp(ts).hour, setting(conn, "quiet_start_hour"), setting(conn, "quiet_end_hour")
    if (h >= qs or h < qe) if qs > qe else (qs <= h < qe):
        return False
    last = conn.execute("SELECT MAX(sent_at) FROM nudges WHERE arm != 'none'").fetchone()[0]
    if last and ts - last < setting(conn, "cooldown_min") * 60:
        return False
    n = conn.execute("SELECT COUNT(*) FROM nudges WHERE arm != 'none' AND sent_at > ?", (ts - 3600,)).fetchone()[0]
    return n < setting(conn, "max_nudges_per_hour")


def log_nudge(conn, ts, arm, policy="rule", run_min=None, risk=None, switches_10m=None):
    """Record a reminder that was shown. Also feeds the can_nudge() guardrails (cooldown, hourly cap)."""
    dt = datetime.fromtimestamp(ts)
    cur = conn.execute(
        "INSERT INTO nudges (sent_at,context_key,arm,policy,hour,weekday,run_min,risk,switches_10m) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (int(ts), context_key(ts), arm, policy, dt.hour, dt.weekday(), run_min, risk, switches_10m))
    conn.commit()
    return cur.lastrowid


class SqlNudgeBandit:
    """Discounted Thompson sampling per context; state lives in bandit_arms."""

    def __init__(self, conn, epsilon=0.1, gamma=0.98):
        self.conn, self.epsilon, self.gamma = conn, epsilon, gamma

    def _ensure(self, ctx):
        for a in ARMS:
            self.conn.execute("INSERT OR IGNORE INTO bandit_arms (context_key, arm) VALUES (?,?)", (ctx, a))

    def choose(self, ts):
        ctx = context_key(ts)
        self._ensure(ctx)
        rows = self.conn.execute("SELECT arm, alpha, beta FROM bandit_arms WHERE context_key=?", (ctx,)).fetchall()
        if random.random() < self.epsilon:
            return ctx, random.choice(ARMS), "random"
        best = max(rows, key=lambda r: random.betavariate(r["alpha"], r["beta"]))
        return ctx, best["arm"], "thompson"

    def log(self, ts, ctx, arm, policy, hour, weekday, run_min=None, risk=None, switches_10m=None):
        cur = self.conn.execute(
            "INSERT INTO nudges (sent_at,context_key,arm,policy,hour,weekday,run_min,risk,switches_10m) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (int(ts), ctx, arm, policy, hour, weekday, run_min, risk, switches_10m))
        self.conn.execute("UPDATE bandit_arms SET pulls=pulls+1 WHERE context_key=? AND arm=?", (ctx, arm))
        self.conn.commit()
        return cur.lastrowid

    def resolve(self, nudge_id, responded, response_secs=None, now=None):
        now = int(now or datetime.now().timestamp())
        ctx, arm = self.conn.execute("SELECT context_key, arm FROM nudges WHERE id=?", (nudge_id,)).fetchone()
        r = 1.0 if responded else 0.0
        self.conn.execute("UPDATE nudges SET responded=?, response_secs=?, reward=?, resolved_at=? WHERE id=?",
                          (int(responded), response_secs, r, now, nudge_id))
        self.conn.execute(
            "UPDATE bandit_arms SET alpha=MAX(1.0, alpha*?)+?, beta=MAX(1.0, beta*?)+?, updated_at=? "
            "WHERE context_key=? AND arm=?", (self.gamma, r, self.gamma, 1 - r, now, ctx, arm))
        self.conn.commit()

    def policy_table(self):
        return [dict(r) | {"mean": round(r["alpha"] / (r["alpha"] + r["beta"]), 3)} for r in self.conn.execute(
            "SELECT context_key, arm, alpha, beta, pulls FROM bandit_arms "
            "ORDER BY context_key, alpha/(alpha+beta) DESC")]


# ───────────────────────── output cache for the dashboard ─────────────────────────
def set_cache(conn, key, obj):
    conn.execute("INSERT OR REPLACE INTO insights_cache VALUES (?,?,?)",
                 (key, json.dumps(obj, default=float), int(datetime.now().timestamp())))
    conn.commit()


def get_cache(conn, key):
    r = conn.execute("SELECT value FROM insights_cache WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None

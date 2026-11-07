"""
coach.py - lightweight ML coaching for the productivity tracker. Standard library only.

A drop-in replacement for ml_coach.Coach (same refresh() output, same risk_model.risk() call), built for
a desktop app you intend to ship:

  * No pandas / numpy / scipy / scikit-learn  -> small installer, instant start-up
  * SQL does the aggregation (focus_runs, sessions grouped by hour/day), Python only does the maths
  * The one real model (distraction risk) is a regularised logistic regression solved with Newton's
    method; it matches scikit-learn's solution and is saved in the database, so scoring works at once on
    the next start with no retraining
  * NudgeEngine ties the risk model, the timing bandit (db.SqlNudgeBandit) and the guardrails
    (db.can_nudge) together, and resolves each nudge's outcome itself, so reminders keep learning even
    when no window is open

  python coach.py            print the current insights for ./data/tracker.db as JSON
  python coach.py --db path
"""
import argparse
import json
import math
import statistics
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta

import db

DB_PATH = "./data/tracker.db"


def _since(days):
    return int((datetime.now() - timedelta(days=days)).timestamp())


def _percentile(sorted_vals, q):
    """Linear interpolation, same as numpy.percentile's default."""
    k = (len(sorted_vals) - 1) * q / 100
    f, c = math.floor(k), math.ceil(k)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


# ───────────────── 1. peak focus hours ─────────────────
def peak_hours(conn, days=90, half_life_days=14, shrink_secs=1800, min_secs=1800):
    """
    Score each hour by its productive ratio with two safeguards:
      recency weighting: last two weeks count more than last month
      shrinkage: hours with little data are pulled toward your average, so one lucky 3am session
                 does not become a 'peak'
    """
    rows = conn.execute(
        "SELECT date, hour, SUM(CASE WHEN label=1 THEN duration ELSE 0 END), SUM(duration) "
        "FROM sessions WHERE timestamp >= ? GROUP BY date, hour", (_since(days),)).fetchall()
    if not rows:
        return {"status": "insufficient_data"}
    latest = max(date.fromisoformat(r[0]) for r in rows)
    prod, tot, raw = {}, {}, {}
    for d, hour, p, t in rows:
        w = 0.5 ** ((latest - date.fromisoformat(d)).days / half_life_days)
        prod[hour] = prod.get(hour, 0) + p * w
        tot[hour] = tot.get(hour, 0) + t * w
        raw[hour] = raw.get(hour, 0) + t
    prior = sum(prod.values()) / max(sum(tot.values()), 1)
    score = {h: (prod[h] + shrink_secs * prior) / (tot[h] + shrink_secs) for h in prod if raw[h] >= min_secs}
    if not score:
        return {"status": "insufficient_data"}
    top = sorted(score.items(), key=lambda kv: -kv[1])[:3]
    best, best_start = -1, None
    for h in sorted(score):
        if h + 1 in score and (score[h] + score[h + 1]) / 2 > best:
            best, best_start = (score[h] + score[h + 1]) / 2, h
    return {"status": "ok",
            "top_hours": [{"hour": int(h), "score": round(s, 3)} for h, s in top],
            "best_window": None if best_start is None else {"start": best_start, "end": best_start + 2}}


# ───────────────── 2. session length recommender ─────────────────
def session_length(conn, days=90):
    """Reads the focus_runs table (db.rebuild_focus_runs): how long until a distraction ends a run."""
    runs = conn.execute("SELECT secs, ended_by_distraction FROM focus_runs WHERE start_ts >= ?",
                        (_since(days),)).fetchall()
    faded = [s for s, by in runs if by and s >= 300]
    sample = faded if len(faded) >= 8 else [s for s, _ in runs if s >= 300]
    if len(sample) < 8:
        return {"status": "insufficient_data", "runs_seen": len(runs)}
    fade_min = statistics.median(sample) / 60
    focus = int(min(50, max(15, round(0.85 * fade_min / 5) * 5)))      # stop a little before the fade point
    return {"status": "ok", "typical_fade_min": round(fade_min, 1),
            "p75_min": round(_percentile(sorted(sample), 75) / 60, 1),
            "recommended_focus_min": focus, "recommended_break_min": 5 if focus < 30 else 10,
            "runs_used": len(sample)}


# ───────────────── 3. distraction risk (logistic regression) ─────────────────
FEATURES = ["hour_sin", "hour_cos", "weekday", "run_min", "switches_10m", "since_distraction_min"]


def _row(hour, weekday, run_min, switches_10m, since_distraction_min):
    return [math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
            weekday, run_min, switches_10m, min(since_distraction_min, 180)]


def load_rows(conn, days=90):
    return [tuple(r) for r in conn.execute(
        "SELECT timestamp, duration, label, hour, weekday FROM sessions WHERE timestamp >= ? ORDER BY timestamp",
        (_since(days),))]


def build_risk_dataset(rows, horizon=900):
    """At the start of every on-task session: will a distraction begin within `horizon` seconds?"""
    ts = [r[0] for r in rows]
    distraction_ts = [r[0] for r in rows if r[2] == 0]
    X, y = [], []
    run_start = last_end = last_distr_end = None
    for i, (t, dur, lab, hr, wd) in enumerate(rows):
        if lab == 0:
            run_start, last_distr_end, last_end = None, t + dur, t + dur
            continue
        if run_start is None or t - last_end > 120:
            run_start = t
        last_end = t + dur
        sw = i - bisect_left(ts, t - 600)
        since = (t - last_distr_end) / 60 if last_distr_end else 180
        X.append(_row(hr, wd, (t - run_start) / 60, sw, since))
        j = bisect_right(distraction_ts, t)
        y.append(int(j < len(distraction_ts) and distraction_ts[j] <= t + horizon))
    return X, y


def _sigmoid(z):
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _solve(A, b):
    """Gaussian elimination with partial pivoting (matrices here are 7x7)."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        piv = M[c][c] or 1e-12
        for r in range(c + 1, n):
            f = M[r][c] / piv
            for k in range(c, n + 1):
                M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / (M[r][r] or 1e-12)
    return x


def _fit_logistic(X, y, l2=1.0, iters=30, tol=1e-6):
    """
    L2-regularised logistic regression with 'balanced' class weights, solved by Newton's method.
    Minimises  sum_i w_i * logloss_i + (l2/2) * |beta|^2  (intercept unpenalised) -- the same objective
    as scikit-learn's LogisticRegression(C=1/l2, class_weight='balanced'). Returns [intercept, *coefs].
    """
    n, d = len(y), len(X[0]) + 1
    n1 = sum(y)
    wts = {1: n / (2 * n1), 0: n / (2 * (n - n1))}
    beta = [0.0] * d
    for _ in range(iters):
        g, H = [0.0] * d, [[0.0] * d for _ in range(d)]
        for xi, yi in zip(X, y):
            row = (1.0, *xi)
            p = _sigmoid(sum(b * v for b, v in zip(beta, row)))
            w = wts[yi]
            r, s = w * (p - yi), w * p * (1 - p)
            for a in range(d):
                ra = row[a]
                g[a] += r * ra
                sa, Ha = s * ra, H[a]
                for b in range(a, d):
                    Ha[b] += sa * row[b]
        for a in range(1, d):
            g[a] += l2 * beta[a]
            H[a][a] += l2
        H[0][0] += 1e-9
        for a in range(d):
            for b in range(a):
                H[a][b] = H[b][a]
        step = _solve(H, g)
        beta = [b - s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < tol:
            break
    return beta


def _auc(y, scores):
    """Area under the ROC curve (rank method, ties averaged). None if only one class is present."""
    n1 = sum(y)
    n0 = len(y) - n1
    if n0 == 0 or n1 == 0:
        return None
    pairs = sorted(zip(scores, y))
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        rank_sum += (i + 1 + j) / 2 * sum(1 for k in range(i, j) if pairs[k][1] == 1)
        i = j
    return (rank_sum - n1 * (n1 + 1) / 2) / (n1 * n0)


def _standardizer(X):
    cols = list(zip(*X))
    mean = [sum(c) / len(c) for c in cols]
    std = [math.sqrt(sum((v - m) ** 2 for v in c) / len(c)) or 1.0 for c, m in zip(cols, mean)]
    return mean, std


def _scale(X, mean, std):
    return [[(v - m) / s for v, m, s in zip(row, mean, std)] for row in X]


class RiskModel:
    MIN_ROWS = 200
    MAX_ROWS = 6000                     # keeps training under about a second; newest data wins

    def __init__(self):
        self.ready, self.auc, self.rows = False, None, 0
        self.beta = self.mean = self.std = None

    def fit(self, rows):
        X, y = build_risk_dataset(rows)
        X, y = X[-self.MAX_ROWS:], y[-self.MAX_ROWS:]
        if len(y) < self.MIN_ROWS or len(set(y)) < 2:
            return {"status": "insufficient_data", "rows": len(y)}
        cut = int(len(y) * 0.8)                                   # time-ordered split, never shuffled
        self.auc = None
        mean, std = _standardizer(X[:cut])
        beta = _fit_logistic(_scale(X[:cut], mean, std), y[:cut])
        if len(set(y[cut:])) == 2:
            scores = [_sigmoid(beta[0] + sum(b * v for b, v in zip(beta[1:], r)))
                      for r in _scale(X[cut:], mean, std)]
            self.auc = _auc(y[cut:], scores)
        self.mean, self.std = _standardizer(X)                    # final fit on everything
        self.beta = _fit_logistic(_scale(X, self.mean, self.std), y)
        self.ready, self.rows = True, len(y)
        return {"status": "ok", "rows": len(y), "holdout_auc": None if self.auc is None else round(self.auc, 3)}

    def risk(self, hour, weekday, run_min, switches_10m, since_distraction_min):
        if not self.ready:
            return None
        x = [(v - m) / s for v, m, s in zip(_row(hour, weekday, run_min, switches_10m, since_distraction_min),
                                           self.mean, self.std)]
        return _sigmoid(self.beta[0] + sum(b * v for b, v in zip(self.beta[1:], x)))

    def explain(self):
        """Which signals push distraction risk up or down (standardised coefficients)."""
        if not self.ready:
            return []
        return sorted(zip(FEATURES, [round(c, 2) for c in self.beta[1:]]), key=lambda kv: -abs(kv[1]))

    def save(self, conn):
        if self.ready:
            db.set_cache(conn, "risk_model", {"beta": self.beta, "mean": self.mean, "std": self.std,
                                               "auc": self.auc, "rows": self.rows})

    def load(self, conn):
        d = db.get_cache(conn, "risk_model")
        if d:
            self.beta, self.mean, self.std = d["beta"], d["mean"], d["std"]
            self.auc, self.rows, self.ready = d["auc"], d["rows"], True
        return self.ready


# ───────────────── 4-6. weekly forecast, adaptive goal, streaks ─────────────────
def daily_productive(conn, days=90):
    """{date: productive seconds}, zero-filled across the observed range."""
    rows = conn.execute("SELECT date, SUM(CASE WHEN label=1 THEN duration ELSE 0 END) FROM sessions "
                        "WHERE timestamp >= ? GROUP BY date ORDER BY date", (_since(days),)).fetchall()
    if not rows:
        return {}
    got = {date.fromisoformat(d): s for d, s in rows}
    first, last = min(got), max(got)
    return {first + timedelta(days=i): got.get(first + timedelta(days=i), 0) for i in range((last - first).days + 1)}


def _week_start(d):
    return d - timedelta(days=d.weekday())


def weekly_totals(daily):
    out = {}
    for d, secs in daily.items():
        out[_week_start(d)] = out.get(_week_start(d), 0) + secs
    return out


def weekly_forecast(daily, target_secs, today=None):
    today = today or date.today()
    if len(daily) < 14:
        return {"status": "insufficient_data", "days_seen": len(daily)}
    mon = _week_start(today)
    hist = {d: v for d, v in daily.items() if mon - timedelta(weeks=8) <= d < mon}
    profile = []
    for wd in range(7):
        vals = [v for d, v in hist.items() if d.weekday() == wd]
        profile.append(sum(vals) / len(vals) if vals else 0.0)

    wd = today.weekday()
    done = sum(daily.get(mon + timedelta(days=i), 0) for i in range(wd))
    k = 3600                                                      # shrink pace toward 1.0 early in the week
    pace = min(1.4, max(0.6, (done + k) / (sum(profile[:wd]) + k)))
    today_actual = daily.get(today, 0)
    remaining = max(profile[wd] * pace - today_actual, 0) + sum(profile[wd + 1:]) * pace
    forecast = done + today_actual + remaining

    past = list(weekly_totals(hist).values())
    sigma = statistics.pstdev(past) if len(past) >= 2 else forecast * 0.25
    sigma = max(sigma * math.sqrt(remaining / max(sum(profile), 1)), 600)
    p_hit = 0.5 * (1 + math.erf((forecast - target_secs) / (sigma * math.sqrt(2))))
    return {"status": "ok", "achieved_secs": int(done + today_actual), "forecast_secs": int(forecast),
            "target_secs": int(target_secs), "prob_hit_goal": round(p_hit, 3), "pace": round(pace, 2),
            "daily_needed_secs": int(max(target_secs - done - today_actual, 0) / max(7 - wd, 1))}


def adaptive_goal(daily, current_target):
    """Aim just above the comfort zone; back off if the user keeps missing."""
    totals = list(weekly_totals(daily).values())[:-1][-4:]        # last 4 completed weeks
    if len(totals) < 2:
        return {"status": "insufficient_data"}
    hit_rate = sum(t >= current_target for t in totals) / len(totals)
    factor = 1.10 if hit_rate >= 0.75 else 0.90 if hit_rate <= 0.25 else 1.0
    return {"status": "ok", "hit_rate": round(hit_rate, 2),
            "suggested_target_secs": int(round(statistics.median(totals) * factor / 900) * 900)}


def streak_stats(daily, daily_goal_secs, today=None):
    today = today or date.today()
    hit = {d: s >= daily_goal_secs for d, s in daily.items()}
    streak, d = 0, today - timedelta(days=1)                      # today is still in progress
    if hit.get(today):
        streak = 1
    while hit.get(d):
        streak += 1
        d -= timedelta(days=1)
    last14 = [v for dd, v in daily.items() if dd > today - timedelta(days=14)]
    mean = statistics.fmean(last14) if last14 else 0
    cv = statistics.stdev(last14) / mean if len(last14) >= 7 and mean > 0 else None
    return {"current_streak_days": streak, "consistency": None if cv is None else round(max(0, 1 - cv), 2)}


# ───────────────── the coach (same interface as ml_coach.Coach) ─────────────────
class Coach:
    def __init__(self, db_path=DB_PATH, weekly_target_secs=20 * 3600, daily_goal_secs=3 * 3600):
        self.db_path, self.weekly_target, self.daily_goal = db_path, weekly_target_secs, daily_goal_secs
        self.risk_model, self.cache = RiskModel(), {}
        conn = db.connect(db_path)
        try:
            self.risk_model.load(conn)                 # saved weights: risk() works before the first refresh
        finally:
            conn.close()

    def refresh(self):
        """Run on a schedule (every 5-15 min) in a background thread. Opens its own connection."""
        conn = db.connect(self.db_path)
        try:
            db.rebuild_focus_runs(conn)                # keep the derived table the recommender reads fresh
            daily = daily_productive(conn)
            fit = self.risk_model.fit(load_rows(conn))
            if fit["status"] == "ok":
                self.risk_model.save(conn)
            self.cache = {
                "peak_hours": peak_hours(conn),
                "session_length": session_length(conn),
                "risk_model": fit,
                "forecast": weekly_forecast(daily, self.weekly_target),
                "next_goal": adaptive_goal(daily, self.weekly_target),
                "streak": streak_stats(daily, self.daily_goal),
                "risk_drivers": self.risk_model.explain(),
            }
        finally:
            conn.close()
        return self.cache


# ───────────────── nudge timing: WHEN to remind, and learn what works ─────────────────
class NudgeEngine:
    """
    Call step() about once a second with the live state. It decides when a reminder should appear.

    One decision per 'episode' (a stretch of on-task work):
      1. the bandit picks an arm for this context, e.g. 'break@run35' or 'goal@risk' or 'none'
      2. the engine waits until that arm's trigger is met (35 more minutes of focus, or risk above the
         threshold; 'none' uses the risk trigger but stays silent, so silence is measured fairly)
      3. guardrails (db.can_nudge) must allow it, otherwise it keeps waiting
      4. the nudge is logged, then judged 10 minutes later, by the engine itself:
         success = no distraction began in that window. Stepping away without a distraction also counts.
    Runs that end before the trigger are not recorded, so an arm is never punished for a moment that
    never came.
    """
    OUTCOME_SECS = 600
    RISK_THRESHOLD = 0.6

    def __init__(self, conn, epsilon=0.1):
        self.conn, self.bandit = conn, db.SqlNudgeBandit(conn, epsilon=epsilon)
        self.episode = self.pending = None

    def _triggered(self, arm, run_since, risk):
        if arm.startswith("break@run"):
            return run_since >= int(arm.split("run")[1])
        if risk is None:                                  # risk model not trained yet
            return run_since >= 30
        return risk >= self.RISK_THRESHOLD

    @staticmethod
    def _text(kind, run_min, goal_pct):
        if kind == "break":
            return f"You've been focused for {int(run_min)} minutes. A short break now will help you stay sharp."
        if kind == "goal":
            return f"You're {goal_pct:.0f}% of the way to today's goal. One more focused block gets you closer."
        return "Want to silence distracting apps for the next 25 minutes?"

    def step(self, now, on_task, distracted, run_min, risk=None, switches_10m=0, goal_pct=0.0):
        """Returns {'id','arm','kind','text'} when a reminder should be shown now, otherwise None."""
        if self.pending:
            p = self.pending
            if now - p["deadline"] > 120:                 # we were not watching (app closed, asleep):
                self.pending = None                       # drop it; never learn from unobserved time
            elif distracted:
                self.bandit.resolve(p["id"], False, response_secs=int(now - p["at"]), now=now)
                self.pending = None
            elif now >= p["deadline"]:
                self.bandit.resolve(p["id"], True, now=now)
                self.pending = None
            if self.pending:
                return None                               # still measuring the last one
        if not on_task:
            self.episode = None
            return None
        if self.episode is None:
            ctx, arm, policy = self.bandit.choose(now)
            self.episode = {"ctx": ctx, "arm": arm, "policy": policy, "run_at_start": run_min}
        ep = self.episode
        if not self._triggered(ep["arm"], run_min - ep["run_at_start"], risk):
            return None
        if ep["arm"] != "none" and not db.can_nudge(self.conn, now):
            return None                                   # guardrails say not now: keep waiting
        dt = datetime.fromtimestamp(now)
        nid = self.bandit.log(now, ep["ctx"], ep["arm"], ep["policy"], dt.hour, dt.weekday(),
                              run_min=round(run_min, 1), risk=None if risk is None else round(risk, 3),
                              switches_10m=switches_10m)
        self.pending = {"id": nid, "at": now, "deadline": now + self.OUTCOME_SECS}
        self.episode = None                               # next decision comes after this outcome is known
        if ep["arm"] == "none":
            return None
        kind = ep["arm"].split("@")[0]
        return {"id": nid, "arm": ep["arm"], "kind": kind, "text": self._text(kind, run_min, goal_pct)}


def main():
    ap = argparse.ArgumentParser(description="Print current coaching insights as JSON")
    ap.add_argument("--db", default=DB_PATH)
    a = ap.parse_args()
    print(json.dumps(Coach(a.db).refresh(), indent=2, default=str))


if __name__ == "__main__":
    main()

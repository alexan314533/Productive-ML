import db
c = db.connect("./data/demo.db")
for k, v in {"focus_minutes": 0.5, "warn_after_secs": 10, "cooldown_min": 0,
             "quiet_start_hour": 0, "quiet_end_hour": 0, "max_nudges_per_hour": 20}.items():
    c.execute("UPDATE settings SET value=? WHERE key=?", (str(v), k))
c.commit()
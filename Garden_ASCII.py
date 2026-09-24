import time
import pygetwindow as gw 
#import json depreciated 
import sqlite3
from datetime import datetime

prod_time = 0
nonprod_time = 0
WarnNonProd_time = 0
productive_keywords = []

'''convert json saving process to SQLite 
    Storage container: 4 tables
    SESSIONS
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    app           TEXT    NOT NULL,
    title         TEXT,
    timestamp     INTEGER NOT NULL,   -- unix epoch (session start)
    duration      INTEGER NOT NULL,   -- seconds
    label         INTEGER NOT NULL,   -- 1=productive, 0=distraction, 2=neutral
    hour          INTEGER,            -- 0-23, derived on insert
    weekday       INTEGER,            -- 0=Mon 6=Sun, derived on insert
    followed_by   INTEGER            -- label of NEXT session (for recommender)

    SESSION SUMMARY
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT    NOT NULL UNIQUE,  -- "YYYY-MM-DD"
    productive_secs     INTEGER DEFAULT 0,
    distraction_secs    INTEGER DEFAULT 0,
    neutral_secs        INTEGER DEFAULT 0,
    total_secs          INTEGER DEFAULT 0,
    session_count       INTEGER DEFAULT 0,
    avg_session_secs    INTEGER DEFAULT 0,
    context_switches    INTEGER DEFAULT 0,        -- app changes per day
    goal_secs           INTEGER DEFAULT 0,        -- target for that day
    goal_met            INTEGER DEFAULT 0,        -- 1=yes 0=no
    productivity_score  REAL    DEFAULT 0.0       -- 0.0-100.0 computed score

    STREAKS
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date      TEXT NOT NULL,   -- "YYYY-MM-DD"
    end_date        TEXT NOT NULL,   -- "YYYY-MM-DD"
    length_days     INTEGER NOT NULL,
    avg_score       REAL,
    best_score      REAL,
    is_current      INTEGER DEFAULT 0  -- 1 = still active streak
    
    WEEKLY GOAL
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    week            TEXT NOT NULL UNIQUE,  -- "YYYY-WNN" e.g. "2025-W03"
    target_secs     INTEGER NOT NULL,      -- weekly productive time target
    achieved_secs   INTEGER DEFAULT 0,     -- running total
    met             INTEGER DEFAULT 0,     -- 1=hit goal 0=missed
    forecast_secs   INTEGER,               -- ML predicted final total
    confidence      REAL                   -- model confidence 0.0-1.0
'''
def get_active_window_title(): #get current window title on the active page
    try:
        return gw.getActiveWindow().title
    except:
        return None

def get_all_window_titles():
    try:
        return gw.getAllTitles()
    except:
        return []

print("\nAll current window titles:\n")

seen = set()
for title in get_all_window_titles():
    if title and title.strip() and title not in seen:
        print(f"- {title}")
        seen.add(title)

# --- USER ENTERS KEYWORDS ---
print("\nEnter keywords for PRODUCTIVE windows (type 0 to finish):")

while True:
    keyword = input("> ").strip().lower()
    if keyword == "0":
        break
    if keyword:
        productive_keywords.append(keyword)

print("\nProductive keywords:")
print(productive_keywords)

def Write_data():

    data = {
        "prod_time": prod_time,
        "nonprod_time": nonprod_time
    }

    with open('live_data.json', 'w') as f:
        json.dump(data, f)

    #print("1 second has passed")


def Save_data():

    data = {
        "prod_time": 0,
        "nonprod_time": 0
    }

    try:
        with open('total_data.json', 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
         pass

    data['prod_time'] += prod_time
    data['nonprod_time'] += nonprod_time


    with open('total_data.json', 'w') as f:
        json.dump(data, f)

    print("Data Sucessfully saved")

print("\nTracking productivity per second... Press Ctrl+C to stop.\n")

try:
    while True:
        current_window = get_active_window_title()
        #print("current active window: ", current_window)

        if current_window:
            title_lower = current_window.lower()

            if any(keyword in title_lower for keyword in productive_keywords):
                prod_time += 1
                WarnNonProd_time = 0
                #print(f"[+1s] PRODUCTIVE → {current_window}")
            else:
                nonprod_time += 1
                WarnNonProd_time += 1
                if (WarnNonProd_time == 30):
                    print("WARNING YOU HAVE BEEN UNPRODUCTIVE FOR 5 MINUTES")
                #print(f"[+1s] NON-PRODUCTIVE → {current_window}")             
        Write_data()
        time.sleep(1)

except KeyboardInterrupt:
        print(f"you were productive for {prod_time:.2f} seconds")
        print(f"you were not productive for {nonprod_time:.2f} seconds")
        print(f"total time elasped this session: {prod_time + nonprod_time:.2f} seconds")
        print("Stopped tracking.")
        Save_data()








    
    



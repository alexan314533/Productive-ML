import time
import pygetwindow as gw 
import json

prod_time = 0
nonprod_time = 0
productive_keywords = []


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

    print("1 second has passed")


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

        if current_window:
            title_lower = current_window.lower()

            if any(keyword in title_lower for keyword in productive_keywords):
                prod_time += 1
                #print(f"[+1s] PRODUCTIVE → {current_window}")
            else:
                nonprod_time += 1
                #print(f"[+1s] NON-PRODUCTIVE → {current_window}")             
        Write_data()
        time.sleep(1)

except KeyboardInterrupt:
        print(f"you were productive for {prod_time:.2f} seconds")
        print(f"you were not productive for {nonprod_time:.2f} seconds")
        print("Stopped tracking.")
        Save_data()








    
    



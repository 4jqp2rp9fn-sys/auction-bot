import time
from scripts.python import yahoo_auctions_line_alert

import json
import os

SEEN_FILE = "seen.json"

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

while True:
    yahoo_auctions_line_alert.main()
    time.sleep(300)

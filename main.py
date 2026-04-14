import time
from scripts.python import yahoo_auctions_line_alert

while True:
    yahoo_auctions_line_alert.main()
    time.sleep(300)

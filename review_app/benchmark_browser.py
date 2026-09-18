"""Optional browser timing probe (requires Playwright and Chromium).

Run from the repository root: python3 -m review_app.benchmark_browser
"""

import json
import random
import statistics
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright
from review_app import server as service

ROOT = Path(__file__).resolve().parents[1]
ReviewHTTPServer = getattr(service, 'ReviewHTTPServer', ThreadingHTTPServer)
initialize = service.initialize
make_handler = service.make_handler


def pct(values, q):
    values = sorted(values)
    return round(values[int((len(values)-1)*q + .5)]*1000, 2)


snapshot = json.loads((ROOT / '.review/snapshot.json').read_text())
with tempfile.TemporaryDirectory() as directory:
    db = Path(directory) / 'judgments.sqlite3'
    initialize(db)
    handler = make_handler(snapshot, db, ROOT / 'review_app/static')
    handler.log_message = lambda *args: None
    server = ReviewHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            start = time.perf_counter()
            page.goto(f'http://127.0.0.1:{server.server_port}/', wait_until='domcontentloaded')
            page.locator('#review-card:not([hidden])').wait_for(timeout=15000)
            first_ms = round((time.perf_counter()-start)*1000, 2)
            count = page.locator('.list-row').count()
            indices = random.Random(42).sample(range(count), 40)
            values = []
            for index in indices:
                start = time.perf_counter()
                page.locator('.list-row').nth(index).evaluate('(el) => el.click()')
                page.locator('#review-card:not([hidden])').wait_for(timeout=15000)
                values.append(time.perf_counter() - start)
            print(json.dumps({'first_card_ms': first_ms, 'click_count':len(values),
                              'click_p50_ms':pct(values,.5), 'click_p95_ms':pct(values,.95),
                              'click_p99_ms':pct(values,.99), 'click_max_ms':pct(values,1)}))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

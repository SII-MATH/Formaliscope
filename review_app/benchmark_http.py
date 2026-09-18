"""Repeatable local HTTP workload for KIP126 review card loads.

Run from the repository root: python3 -m review_app.benchmark_http
The benchmark uses a temporary database and an ephemeral loopback port.
"""
import argparse
import json
import statistics
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from review_app import server as service

ROOT = Path(__file__).resolve().parents[1]
ReviewHTTPServer = getattr(service, "ReviewHTTPServer", ThreadingHTTPServer)
initialize = service.initialize
make_handler = service.make_handler


def percentile(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, max(0, int((len(values) - 1) * q + .5)))] * 1000, 2)


def request(base, path, *, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", "Origin": base} if data else {}
    req = Request(base + path, data=data, headers=headers)
    with urlopen(req, timeout=15) as response:
        body = response.read()
        if response.status not in (200, 201):
            raise RuntimeError(f"{response.status}: {body[:100]}")
    return json.loads(body)


def run(snapshot, clients, cards_per_client):
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "judgments.sqlite3"
        initialize(db)
        handler = make_handler(snapshot, db, ROOT / "review_app/static")
        handler.log_message = lambda *args: None
        server = ReviewHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        barrier = threading.Barrier(clients)
        card_ids = [row["id"] for row in snapshot["cards"] if row["source_status"] == "local"]

        def client(number):
            local_load = []
            local_submit = []
            errors = []
            try:
                catalog = request(base, "/api/catalog")
                if len(catalog["cards"]) != len(snapshot["cards"]):
                    raise RuntimeError("incomplete catalog")
                barrier.wait(timeout=15)
                with ThreadPoolExecutor(max_workers=2) as pair:
                    for iteration in range(cards_per_client):
                        card_id = card_ids[(number * 7 + iteration) % len(card_ids)]
                        encoded = quote(card_id, safe="")
                        start = time.perf_counter()
                        evidence = pair.submit(request, base, f"/api/evidence?id={encoded}")
                        history = pair.submit(request, base, f"/api/history?id={encoded}")
                        card = evidence.result(timeout=15)
                        history.result(timeout=15)
                        local_load.append(time.perf_counter() - start)
                        if iteration % 8 == 0:
                            payload = {"request_id": str(uuid.uuid4()), "card_id": card_id,
                                       "fingerprint": card["fingerprint"],
                                       "reviewer": f"load-{number}", "verdict": "aligned", "rationale": ""}
                            start = time.perf_counter()
                            request(base, "/api/judgments", payload=payload)
                            local_submit.append(time.perf_counter() - start)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            return local_load, local_submit, errors

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=clients) as pool:
            results = list(pool.map(client, range(clients)))
        elapsed = time.perf_counter() - start
        server.shutdown()
        server.server_close()
        thread.join()
        loads = [value for local, _, _ in results for value in local]
        submits = [value for _, local, _ in results for value in local]
        client_p95s = [percentile(local, .95) for local, _, _ in results if local]
        errors = [err for _, _, local in results for err in local]
        return {"clients": clients, "cards_per_client": cards_per_client,
                "card_count": len(loads), "submit_count": len(submits), "errors": errors,
                "card_p50_ms": percentile(loads, .50), "card_p95_ms": percentile(loads, .95),
                "card_p99_ms": percentile(loads, .99), "card_max_ms": percentile(loads, 1),
                "client_p95_min_ms": min(client_p95s), "client_p95_max_ms": max(client_p95s),
                "client_p95_gap_ms": round(max(client_p95s) - min(client_p95s), 2),
                "client_p95_spread": round(max(client_p95s) / min(client_p95s), 2),
                "submit_p95_ms": percentile(submits, .95),
                "card_throughput_per_s": round(len(loads) / elapsed, 2)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--cards", type=int, default=24)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    snapshot = json.loads((ROOT / ".review/snapshot.json").read_text())
    trials = [run(snapshot, args.clients, args.cards) for _ in range(args.repeat)]
    print(json.dumps({"trials": trials, "median_card_p95_ms": statistics.median(t["card_p95_ms"] for t in trials),
                      "median_client_p95_gap_ms": statistics.median(t["client_p95_gap_ms"] for t in trials),
                      "median_client_p95_spread": statistics.median(t["client_p95_spread"] for t in trials),
                      "median_submit_p95_ms": statistics.median(t["submit_p95_ms"] for t in trials)}, indent=2))


if __name__ == "__main__":
    main()

"""Measure how fast spans are actually WRITTEN to DuckDB, and that none are lost.

bench_ingest.py can only see HTTP responses, and a 200 means "queued", not "on
disk". This runs the Heimdall backend unchanged and records the span writer's
own counters every 100 ms, then summarizes them.

Usage (from the repo root, in two terminals):
    # 1. start Heimdall and record counters
    uv run --project backend python bench/writer_stats.py serve writer_stats.csv
    # 2. generate load, e.g.
    uv run bench/bench_ingest.py --batches 500 --spans-per-batch 500 --concurrency 20
    # 3. stop the server (Ctrl+C) and summarize
    uv run --project backend python bench/writer_stats.py summary writer_stats.csv

Server settings come from the usual env vars (HEIMDALL_BATCH_SIZE, HEIMDALL_MAX_PENDING, ...).
"""

import csv
import sys
import threading
import time
from pathlib import Path

COLUMNS = ["t", "received", "written", "rejected", "flushes", "last_flush_ms"]


def serve(csv_path: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    import uvicorn

    from heimdall.api.main import app

    out = open(csv_path, "w", buffering=1)
    out.write(",".join(COLUMNS) + "\n")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=4318, log_level="warning"))

    def record() -> None:
        while not server.started:
            time.sleep(0.05)
        while True:
            s = app.state.span_writer.stats
            out.write(f"{time.perf_counter():.3f},{s.received},{s.written},{s.rejected},"
                      f"{s.flushes},{s.last_flush_ms:.1f}\n")
            time.sleep(0.1)

    threading.Thread(target=record, daemon=True).start()
    print(f"Heimdall on http://127.0.0.1:4318, recording span writer counters to {csv_path}")
    server.run()


def summary(csv_path: str) -> None:
    rows = [{k: float(v) for k, v in r.items()} for r in csv.DictReader(open(csv_path))]
    started = [r for r in rows if r["received"] > 0]
    if not started:
        print("No spans were received.")
        return
    last = rows[-1]
    first_in = started[0]
    all_written = next(r for r in rows if r["written"] >= last["written"])
    elapsed = all_written["t"] - first_in["t"]

    best = 0.0  # best sustained 1-second write rate
    for i, r in enumerate(rows):
        later = next((x for x in rows[i:] if x["t"] - r["t"] >= 1.0), None)
        if later:
            best = max(best, (later["written"] - r["written"]) / (later["t"] - r["t"]))

    lost = last["received"] - last["written"]
    print(f"Spans accepted (queued):   {last['received']:,.0f}")
    print(f"Spans written to DuckDB:   {last['written']:,.0f}   ({'none lost' if lost == 0 else f'{lost:,.0f} NOT written'})")
    print(f"Spans rejected with 429:   {last['rejected']:,.0f}")
    print(f"Flushes:                   {last['flushes']:,.0f}  (avg {last['written'] / max(last['flushes'], 1):,.0f} spans each)")
    print(f"Write throughput:          {last['written'] / max(elapsed, 1e-9):,.0f} spans/s end to end "
          f"(first span accepted -> all on disk in {elapsed:.2f}s), best 1-s window {best:,.0f} spans/s")
    print(f"Largest queue backlog:     {max(r['received'] - r['written'] for r in rows):,.0f} spans")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"serve", "summary"}:
        print(__doc__)
        sys.exit(1)
    serve(sys.argv[2]) if sys.argv[1] == "serve" else summary(sys.argv[2])

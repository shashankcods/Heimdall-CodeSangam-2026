# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx",
# ]
# ///
"""Continuous load generator sending requests to the Heimdall demo gateway."""

import argparse
import sys
import threading
import time
from typing import Optional

import httpx

GATEWAY_URL = "http://localhost:8001"


class LoadGenerator:
    def __init__(self, target_url: str = f"{GATEWAY_URL}/order", rate_per_sec: float = 2.0):
        self.target_url = target_url
        self.interval = 1.0 / max(rate_per_sec, 0.1)
        self.running = False
        self.thread: Optional[threading.Thread] = None

        self.total_requests = 0
        self.success_count = 0
        self.error_count = 0
        self.latencies = []

    def _worker(self):
        with httpx.Client(timeout=15.0) as client:
            while self.running:
                start = time.time()
                try:
                    resp = client.post(
                        self.target_url,
                        json={"customer_id": f"cust_{self.total_requests}", "total": 49.99},
                    )
                    elapsed_ms = (time.time() - start) * 1000.0
                    self.total_requests += 1
                    self.latencies.append(elapsed_ms)
                    if len(self.latencies) > 200:
                        self.latencies.pop(0)

                    if resp.status_code == 200:
                        self.success_count += 1
                    else:
                        self.error_count += 1
                except Exception:
                    self.total_requests += 1
                    self.error_count += 1

                # Sleep to maintain rate
                elapsed_request = time.time() - start
                sleep_time = max(0.0, self.interval - elapsed_request)
                time.sleep(sleep_time)

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)

    def stats_summary(self) -> str:
        avg_ms = sum(self.latencies) / len(self.latencies) if self.latencies else 0.0
        return (
            f"reqs: {self.total_requests} | "
            f"ok: {self.success_count} | "
            f"errors: {self.error_count} | "
            f"avg latency: {avg_ms:.1f}ms"
        )


def main():
    parser = argparse.ArgumentParser(description="Heimdall Demo Load Generator")
    parser.add_argument("--url", default=f"{GATEWAY_URL}/order", help="Target endpoint")
    parser.add_argument("--rate", type=float, default=2.0, help="Requests per second")
    args = parser.parse_args()

    print(f"Starting load generator at {args.rate} req/s -> {args.url}")
    print("Press Ctrl+C to stop.\n")

    gen = LoadGenerator(target_url=args.url, rate_per_sec=args.rate)
    gen.start()

    try:
        while True:
            time.sleep(2.0)
            print(f"[load-gen] {gen.stats_summary()}")
    except KeyboardInterrupt:
        print("\nStopping load generator...")
        gen.stop()
        print(f"Final stats: {gen.stats_summary()}")


if __name__ == "__main__":
    main()

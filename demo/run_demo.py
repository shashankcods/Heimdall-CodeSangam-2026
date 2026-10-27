# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn",
#     "httpx",
#     "pydantic",
#     "opentelemetry-sdk",
#     "opentelemetry-exporter-otlp-proto-http",
#     "opentelemetry-instrumentation-fastapi",
# ]
# ///
"""Single-command runner for Heimdall demo microservices + load generator.

Usage:
    uv run demo/run_demo.py
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Ensure repo root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx

from demo.load_generator import LoadGenerator

HEIMDALL_URL = os.environ.get("HEIMDALL_URL", "http://localhost:4318")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8001"))
ORDER_PORT = int(os.environ.get("ORDER_PORT", "8002"))
PAYMENT_PORT = int(os.environ.get("PAYMENT_PORT", "8003"))


def check_backend_health() -> bool:
    try:
        r = httpx.get(f"{HEIMDALL_URL}/api/health", timeout=2.0)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        return False


def wait_for_service(url: str, name: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def main():
    print("=" * 60)
    print("           HEIMDALL DEMO CLUSTER RUNNER")
    print("=" * 60)

    # 1. Verify Heimdall Backend is reachable
    print(f"\nChecking Heimdall backend at {HEIMDALL_URL}...")
    if not check_backend_health():
        print(f"[-] Heimdall is NOT running at {HEIMDALL_URL}.")
        print("    Please start Heimdall first in another terminal:")
        print("      cd backend && uv run uvicorn heimdall.api.main:app --port 4318\n")
        sys.exit(1)
    print("[+] Heimdall backend is healthy!")

    # 2. Start demo microservices as separate processes for isolated TracerProviders
    services = [
        ("payment-service", ["uv", "run", str(REPO_ROOT / "demo" / "services" / "payment.py")], PAYMENT_PORT),
        ("order-service", ["uv", "run", str(REPO_ROOT / "demo" / "services" / "order.py")], ORDER_PORT),
        ("gateway", ["uv", "run", str(REPO_ROOT / "demo" / "services" / "gateway.py")], GATEWAY_PORT),
    ]

    running_procs = []
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)

    for name, cmd, port in services:
        print(f"[*] Starting {name} on port {port}...")
        proc = subprocess.Popen(cmd, env=env)
        running_procs.append((name, proc))

    # 3. Wait for all 3 services to pass health checks
    for _, _, port in services:
        name = next(n for n, _, p in services if p == port)
        if not wait_for_service(f"http://127.0.0.1:{port}", name):
            print(f"[-] Failed to start {name} on port {port}!")
            for _, p in running_procs:
                p.terminate()
            sys.exit(1)
    print("\n[+] All demo services are live and healthy!")

    # 4. Start load generator
    load_gen = LoadGenerator(target_url=f"http://127.0.0.1:{GATEWAY_PORT}/order", rate_per_sec=2.0)
    load_gen.start()
    print(f"[+] Load generator started: sending 2 req/s to http://localhost:{GATEWAY_PORT}/order")

    print("\n" + "-" * 60)
    print("DEMO CONTROLS & ENDPOINTS:")
    print(f"  Gateway:        http://localhost:{GATEWAY_PORT}/order")
    print(f"  Order service:  http://localhost:{ORDER_PORT}/orders")
    print(f"  Payment service:http://localhost:{PAYMENT_PORT}/charge")
    print(f"  Heimdall UI:    http://localhost:5173")
    print(f"  Heimdall API:   {HEIMDALL_URL}/api/traces")
    print("-" * 60)
    print("Interactive Commands (type and press Enter):")
    print("  'f' or 'fault'  -> Inject payment failure (1.9s latency + 504 Gateway Timeout)")
    print("  'r' or 'reset'  -> Reset payment service back to normal")
    print("  's' or 'status' -> Print current load stats and fault status")
    print("  'q' or 'quit'   -> Stop all demo services and exit")
    print("-" * 60 + "\n")

    def shutdown():
        print("\nStopping load generator...")
        load_gen.stop()
        print("Stopping demo service processes...")
        for name, proc in running_procs:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except Exception:
                proc.kill()
        print("All demo services stopped. Goodbye!")

    def set_payment_fault(latency_ms: float, error_rate: float, status: int):
        try:
            httpx.post(
                f"http://127.0.0.1:{PAYMENT_PORT}/fault",
                json={
                    "latency_ms": latency_ms,
                    "error_rate": error_rate,
                    "error_status": status,
                    "error_message": "Stripe gateway timeout after 1.9s, retry 0",
                },
                timeout=2.0,
            )
        except Exception as e:
            print(f"Failed to set fault: {e}")

    try:
        while True:
            # Check user input or sleep
            if sys.stdin.isatty():
                cmd = input().strip().lower()
                if cmd in ("q", "quit", "exit"):
                    break
                elif cmd in ("f", "fault"):
                    set_payment_fault(1900.0, 1.0, 504)
                    print("\n>> FAULT INJECTED on payment-service! (1.9s latency + 504 Gateway Timeout)")
                    print(">> Check your Heimdall Traces Waterfall and Service Map!\n")
                elif cmd in ("r", "reset"):
                    set_payment_fault(0.0, 0.0, 504)
                    print("\n>> FAULT CLEARED! payment-service restored to healthy.\n")
                elif cmd in ("s", "status"):
                    try:
                        fault_info = httpx.get(f"http://127.0.0.1:{PAYMENT_PORT}/fault").json()
                        fault_str = "ACTIVE (504 / 1.9s)" if fault_info.get("fault_active") else "HEALTHY"
                    except Exception:
                        fault_str = "unknown"
                    print(f"\n>> Status: [{load_gen.stats_summary()}] | Payment Fault: {fault_str}\n")
            else:
                time.sleep(2.0)
                print(f"[demo] {load_gen.stats_summary()}")
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()


if __name__ == "__main__":
    main()

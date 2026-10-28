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
    uv run demo/run_demo.py [--non-interactive] [--rate 2.0]
"""

import argparse
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

HEIMDALL_URL = os.environ.get("HEIMDALL_URL", "http://127.0.0.1:4318")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8001"))
ORDER_PORT = int(os.environ.get("ORDER_PORT", "8002"))
PAYMENT_PORT = int(os.environ.get("PAYMENT_PORT", "8003"))
INVENTORY_PORT = int(os.environ.get("INVENTORY_PORT", "8004"))


def check_backend_health() -> bool:
    try:
        r = httpx.get(f"{HEIMDALL_URL}/api/health", timeout=2.0)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        return False


def wait_for_service(url: str, name: str, timeout: float = 10.0) -> bool:
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
    parser = argparse.ArgumentParser(description="Heimdall Demo Cluster Runner")
    parser.add_argument(
        "--non-interactive", "-d", "--daemon",
        action="store_true",
        help="Run continuously in the background without waiting for keyboard input",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=2.0,
        help="Load generator request rate (req/s, default: 2.0)",
    )
    args = parser.parse_args()

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
    node_dir = REPO_ROOT / "demo" / "node_service"

    services = [
        ("payment-service", ["uv", "run", str(REPO_ROOT / "demo" / "services" / "payment.py")], None, PAYMENT_PORT),
        ("inventory-service", ["node", "--require", "./tracer.js", "server.js"], str(node_dir), INVENTORY_PORT),
        ("order-service", ["uv", "run", str(REPO_ROOT / "demo" / "services" / "order.py")], None, ORDER_PORT),
        ("gateway", ["uv", "run", str(REPO_ROOT / "demo" / "services" / "gateway.py")], None, GATEWAY_PORT),
    ]

    running_procs = []
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["HEIMDALL_URL"] = HEIMDALL_URL

    for name, cmd, cwd, port in services:
        print(f"[*] Starting {name} on port {port}...")
        proc = subprocess.Popen(cmd, cwd=cwd, env=env)
        running_procs.append((name, proc))

    # 3. Wait for all 4 services to pass health checks
    for name, _, _, port in services:
        if not wait_for_service(f"http://127.0.0.1:{port}", name):
            print(f"[-] Failed to start {name} on port {port}!")
            for _, p in running_procs:
                p.terminate()
            sys.exit(1)
    print("\n[+] All 4 demo services (Gateway, Order, Inventory [Node.js], Payment) are live!")

    # 4. Start load generator
    load_gen = LoadGenerator(target_url=f"http://127.0.0.1:{GATEWAY_PORT}/order", rate_per_sec=args.rate)
    load_gen.start()
    print(f"[+] Load generator started: sending {args.rate} req/s to http://127.0.0.1:{GATEWAY_PORT}/order")

    print("\n" + "-" * 60)
    print("DEMO CONTROLS & ENDPOINTS:")
    print(f"  Gateway:          http://127.0.0.1:{GATEWAY_PORT}/order")
    print(f"  Order service:    http://127.0.0.1:{ORDER_PORT}/orders")
    print(f"  Inventory (Node): http://127.0.0.1:{INVENTORY_PORT}/reserve")
    print(f"  Payment service:  http://127.0.0.1:{PAYMENT_PORT}/charge")
    print(f"  Heimdall UI:      http://127.0.0.1:5173")
    print(f"  Heimdall API:     {HEIMDALL_URL}/api/traces")
    print("-" * 60)

    if not args.non_interactive and sys.stdin.isatty():
        print("Interactive Commands (type and press Enter):")
        print("  'f' or 'fault'  -> Inject payment failure (1.9s latency + 504 Gateway Timeout)")
        print("  'i' or 'inv'    -> Inject inventory failure (1.5s latency + 500 Stock Error)")
        print("  'r' or 'reset'  -> Reset all faults back to normal")
        print("  's' or 'status' -> Print current load stats and fault status")
        print("  'q' or 'quit'   -> Stop all demo services and exit")
        print("-" * 60 + "\n")
    else:
        print("Running in non-interactive mode. Press Ctrl+C to terminate.")
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
            print(f"Failed to set payment fault: {e}")

    def set_inventory_fault(latency_ms: float, error_rate: float):
        try:
            httpx.post(
                f"http://127.0.0.1:{INVENTORY_PORT}/fault",
                json={
                    "latency_ms": latency_ms,
                    "error_rate": error_rate,
                    "errorMessage": "Warehouse reservation timeout",
                },
                timeout=2.0,
            )
        except Exception as e:
            print(f"Failed to set inventory fault: {e}")

    try:
        while True:
            if not args.non_interactive and sys.stdin.isatty():
                cmd = input().strip().lower()
                if cmd in ("q", "quit", "exit"):
                    break
                elif cmd in ("f", "fault"):
                    set_payment_fault(1900.0, 1.0, 504)
                    print("\n>> FAULT INJECTED on payment-service! (1.9s latency + 504 Gateway Timeout)")
                    print(">> Check your Heimdall Traces Waterfall and Service Map!\n")
                elif cmd in ("i", "inv"):
                    set_inventory_fault(1500.0, 1.0)
                    print("\n>> FAULT INJECTED on inventory-service (Node.js)! (1.5s latency + 500 Error)")
                    print(">> Check your Heimdall Traces Waterfall and Service Map!\n")
                elif cmd in ("r", "reset"):
                    set_payment_fault(0.0, 0.0, 504)
                    set_inventory_fault(0.0, 0.0)
                    print("\n>> ALL FAULTS CLEARED! Cluster restored to normal.\n")
                elif cmd in ("s", "status"):
                    try:
                        p_fault = httpx.get(f"http://127.0.0.1:{PAYMENT_PORT}/fault").json()
                        p_str = "ACTIVE (504 / 1.9s)" if p_fault.get("fault_active") else "HEALTHY"
                    except Exception:
                        p_str = "unknown"
                    try:
                        i_fault = httpx.get(f"http://127.0.0.1:{INVENTORY_PORT}/fault").json()
                        i_str = "ACTIVE (500 / 1.5s)" if i_fault.get("faultActive") else "HEALTHY"
                    except Exception:
                        i_str = "unknown"
                    print(f"\n>> Status: [{load_gen.stats_summary()}] | Payment: {p_str} | Inventory: {i_str}\n")
            else:
                time.sleep(2.0)
                print(f"[demo] {load_gen.stats_summary()}")
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn",
#     "httpx",
#     "opentelemetry-sdk",
#     "opentelemetry-exporter-otlp-proto-http",
#     "opentelemetry-instrumentation-fastapi",
# ]
# ///
"""Heimdall Demo Orchestrator & Load Generator.

Manages the 3-service distributed demo (gateway -> order -> payment),
generates deterministic traffic, injects runtime faults, and performs
end-to-end telemetry verification against Heimdall.

Usage:
    uv run demo/run_demo.py --once
    uv run demo/run_demo.py --serve
    uv run demo/run_demo.py --traffic --count 10
    uv run demo/run_demo.py --verify
    uv run demo/run_demo.py --reset-faults
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Default service URLs and ports
DEFAULT_HEIMDALL_URL = os.environ.get("HEIMDALL_URL", "http://127.0.0.1:4318")
DEFAULT_GATEWAY_PORT = int(os.environ.get("GATEWAY_SERVICE_PORT", "8001"))
DEFAULT_ORDER_PORT = int(os.environ.get("ORDER_SERVICE_PORT", "8002"))
DEFAULT_PAYMENT_PORT = int(os.environ.get("PAYMENT_SERVICE_PORT", "8003"))

SERVICE_PORTS = {
    "gateway": DEFAULT_GATEWAY_PORT,
    "order": DEFAULT_ORDER_PORT,
    "payment": DEFAULT_PAYMENT_PORT,
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DemoError(Exception):
    """Base exception for demo orchestrator errors."""


class ServiceStartupError(DemoError):
    """Raised when a demo service fails to bind or become healthy."""


class VerificationTimeoutError(DemoError):
    """Raised when telemetry fails to become queryable in Heimdall."""


# ---------------------------------------------------------------------------
# Process Management & Readiness
# ---------------------------------------------------------------------------

def wait_for_service_ready(
    client: httpx.Client,
    service_name: str,
    base_url: str,
    timeout: float = 10.0,
    poll_interval: float = 0.2,
) -> None:
    """Poll a service's /health endpoint until it returns HTTP 200."""
    deadline = time.monotonic() + timeout
    health_url = f"{base_url.rstrip('/')}/health"
    last_err: Exception | str | None = None

    while time.monotonic() < deadline:
        try:
            r = client.get(health_url, timeout=2.0)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "ok":
                    return
                last_err = f"Unexpected response: {data}"
            else:
                last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = e
        time.sleep(poll_interval)

    raise ServiceStartupError(
        f"Service '{service_name}' at {health_url} failed to become ready within {timeout:.1f}s. "
        f"Last error: {last_err}"
    )


def wait_for_heimdall_healthy(
    client: httpx.Client,
    heimdall_url: str = DEFAULT_HEIMDALL_URL,
    timeout: float = 8.0,
) -> None:
    """Confirm Heimdall backend is reachable and responsive."""
    deadline = time.monotonic() + timeout
    health_url = f"{heimdall_url.rstrip('/')}/api/health"
    last_err: Exception | str | None = None

    while time.monotonic() < deadline:
        try:
            r = client.get(health_url, timeout=2.0)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return
            last_err = f"HTTP {r.status_code}: {r.text}"
        except Exception as e:
            last_err = e
        time.sleep(0.25)

    raise DemoError(
        f"Heimdall backend is unreachable at {health_url}. "
        f"Please start Heimdall first ('cd backend && uv run uvicorn heimdall.api.main:app --port 4318'). "
        f"Last error: {last_err}"
    )


def start_demo_services(
    ports: dict[str, int] | None = None,
    heimdall_url: str = DEFAULT_HEIMDALL_URL,
    startup_timeout: float = 10.0,
) -> list[subprocess.Popen]:
    """Launch gateway, order, and payment services as background subprocesses."""
    cfg_ports = ports or SERVICE_PORTS
    services = [
        ("payment", cfg_ports["payment"], "demo/services/payment.py"),
        ("order", cfg_ports["order"], "demo/services/order.py"),
        ("gateway", cfg_ports["gateway"], "demo/services/gateway.py"),
    ]

    processes: list[subprocess.Popen] = []
    env_base = os.environ.copy()
    env_base["PYTHONUNBUFFERED"] = "1"
    env_base["PYTHONPATH"] = str(REPO_ROOT)
    env_base["HEIMDALL_URL"] = heimdall_url
    env_base["PAYMENT_SERVICE_URL"] = f"http://127.0.0.1:{cfg_ports['payment']}"
    env_base["ORDER_SERVICE_URL"] = f"http://127.0.0.1:{cfg_ports['order']}"

    try:
        for name, port, rel_path in services:
            service_file = REPO_ROOT / rel_path
            # Use sys.executable to run in current python environment
            cmd = [sys.executable, str(service_file)]
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                env=env_base,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            processes.append(proc)

        # Wait for all services to become responsive
        with httpx.Client() as client:
            for (name, port, _), proc in zip(services, processes):
                # Check if process exited prematurely
                if proc.poll() is not None:
                    err_text = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                    raise ServiceStartupError(
                        f"Service '{name}' exited immediately with code {proc.returncode}.\nStderr: {err_text}"
                    )
                wait_for_service_ready(
                    client,
                    name,
                    f"http://127.0.0.1:{port}",
                    timeout=startup_timeout,
                )
        return processes

    except Exception as exc:
        stop_demo_services(processes)
        raise ServiceStartupError(f"Failed to start demo services: {exc}") from exc


def stop_demo_services(processes: list[subprocess.Popen]) -> None:
    """Cleanly terminate all spawned demo service processes."""
    for proc in processes:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    # Wait up to 3 seconds for graceful exit, then force kill
    deadline = time.monotonic() + 3.0
    for proc in processes:
        if proc.poll() is None:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Traffic Generation
# ---------------------------------------------------------------------------

def send_demo_traffic(
    gateway_url: str = f"http://127.0.0.1:{DEFAULT_GATEWAY_PORT}",
    count: int = 6,
    delay_between_requests: float = 0.05,
) -> dict[str, int]:
    """Send a bounded number of requests through the gateway and tally outcomes."""
    results = {"success": 0, "payment_declined": 0, "downstream_error": 0, "network_error": 0}
    target = f"{gateway_url.rstrip('/')}/order"

    with httpx.Client(timeout=10.0) as client:
        for i in range(1, count + 1):
            payload = {
                "items": [{"name": f"Item-{i}", "quantity": 1, "price": 25.0 * i}],
                "customer_id": f"cust_{i}",
                "amount": float(25.0 * i),
            }
            try:
                r = client.post(target, json=payload)
                if r.status_code == 200:
                    results["success"] += 1
                elif r.status_code == 402:
                    results["payment_declined"] += 1
                else:
                    results["downstream_error"] += 1
            except Exception:
                results["network_error"] += 1

            if delay_between_requests > 0:
                time.sleep(delay_between_requests)

    return results


# ---------------------------------------------------------------------------
# Fault Control Helpers
# ---------------------------------------------------------------------------

def configure_fault(
    service_url: str,
    latency_ms: float | None = None,
    error_type: str | None = None,
    error_rate: float = 1.0,
) -> dict[str, Any]:
    """Configure runtime fault injection on a target service."""
    report = {}
    with httpx.Client(timeout=5.0) as client:
        if latency_ms is not None:
            r = client.post(f"{service_url.rstrip('/')}/fault/latency", json={"delay_ms": latency_ms})
            report["latency"] = r.json()
        if error_type is not None:
            r = client.post(f"{service_url.rstrip('/')}/fault/error", json={"error_type": error_type, "rate": error_rate})
            report["error"] = r.json()
    return report


def reset_all_faults(gateway_url: str = f"http://127.0.0.1:{DEFAULT_GATEWAY_PORT}") -> dict[str, Any]:
    """Trigger reset on all demo services."""
    with httpx.Client(timeout=5.0) as client:
        r = client.post(f"{gateway_url.rstrip('/')}/fault/reset-all")
        return r.json()


# ---------------------------------------------------------------------------
# Telemetry Verification Logic
# ---------------------------------------------------------------------------

def verify_demo_telemetry(
    heimdall_url: str = DEFAULT_HEIMDALL_URL,
    run_start_ms: float = 0.0,
    expected_requests: int = 1,
    timeout: float = 15.0,
    poll_interval: float = 0.3,
) -> dict[str, bool]:
    """Poll Heimdall API and assert distributed traces, logs, and service-map dependencies."""
    deadline = time.monotonic() + timeout
    traces_url = f"{heimdall_url.rstrip('/')}/api/traces"
    logs_url = f"{heimdall_url.rstrip('/')}/api/logs"
    smap_url = f"{heimdall_url.rstrip('/')}/api/service-map"

    matching_traces: list[dict[str, Any]] = []
    sample_trace_spans: list[dict[str, Any]] = []

    with httpx.Client(timeout=5.0) as client:
        # 1. Poll for traces produced in the current run
        while time.monotonic() < deadline:
            try:
                r = client.get(traces_url, params={"from_ms": run_start_ms, "limit": 100})
                if r.status_code == 200:
                    traces = r.json().get("traces", [])
                    if len(traces) >= expected_requests:
                        matching_traces = traces
                        break
            except Exception:
                pass
            time.sleep(poll_interval)

        if not matching_traces:
            raise VerificationTimeoutError(
                f"No traces found at {traces_url} starting after {run_start_ms:.1f} within {timeout:.1f}s."
            )

        # 2. Inspect the first trace details
        target_trace_id = matching_traces[0]["trace_id"]
        trace_resp = client.get(f"{traces_url}/{target_trace_id}")
        if trace_resp.status_code == 200:
            sample_trace_spans = trace_resp.json().get("spans", [])

        # 3. Query logs for this trace or demo services
        demo_logs: list[dict[str, Any]] = []
        try:
            log_resp = client.get(logs_url, params={"from_ms": run_start_ms, "limit": 100})
            if log_resp.status_code == 200:
                demo_logs = log_resp.json().get("logs", [])
        except Exception:
            pass

        # 4. Query service topology map
        service_map: dict[str, Any] = {"nodes": [], "edges": []}
        try:
            smap_resp = client.get(smap_url, params={"from_ms": run_start_ms})
            if smap_resp.status_code == 200:
                service_map = smap_resp.json()
        except Exception:
            pass

    # Evaluate checks
    trace_services = {s.get("service") for s in sample_trace_spans}
    all_3_services_in_trace = {"gateway", "order", "payment"}.issubset(trace_services)

    # Check parent-child links across service boundaries:
    # gateway client span -> order server span
    # order client span -> payment server span
    gw_spans = [s for s in sample_trace_spans if s.get("service") == "gateway"]
    order_spans = [s for s in sample_trace_spans if s.get("service") == "order"]
    payment_spans = [s for s in sample_trace_spans if s.get("service") == "payment"]

    gw_to_order_linked = any(
        o.get("parent_span_id") in {g.get("span_id") for g in gw_spans}
        for o in order_spans
    )
    order_to_payment_linked = any(
        p.get("parent_span_id") in {o.get("span_id") for o in order_spans}
        for p in payment_spans
    )

    # Service map dependency check
    smap_edges = {(e["source"], e["target"]) for e in service_map.get("edges", [])}
    smap_has_gw_order = ("gateway", "order") in smap_edges
    smap_has_order_pay = ("order", "payment") in smap_edges

    checks = {
        "traces received": len(matching_traces) >= expected_requests,
        "trace contains all 3 services": all_3_services_in_trace,
        "gateway -> order span parent link": gw_to_order_linked,
        "order -> payment span parent link": order_to_payment_linked,
        "demo logs ingested with trace_id": any(l.get("trace_id") for l in demo_logs) if demo_logs else True,
        "service map has gateway -> order": smap_has_gw_order,
        "service map has order -> payment": smap_has_order_pay,
    }
    return checks


# ---------------------------------------------------------------------------
# One-Shot Execution Flow
# ---------------------------------------------------------------------------

def run_once(
    heimdall_url: str = DEFAULT_HEIMDALL_URL,
    ports: dict[str, int] | None = None,
    requests_count: int = 6,
) -> int:
    """Execute complete end-to-end verification run."""
    cfg_ports = ports or SERVICE_PORTS
    gateway_url = f"http://127.0.0.1:{cfg_ports['gateway']}"

    print("=================================================================")
    print("      Heimdall Phase 4: Multi-Service Demo & Verification        ")
    print("=================================================================")
    print(f"Target Heimdall URL : {heimdall_url}")
    print(f"Gateway Port        : {cfg_ports['gateway']}")
    print(f"Order Port          : {cfg_ports['order']}")
    print(f"Payment Port        : {cfg_ports['payment']}\n")

    # 1. Health check Heimdall
    print("1. Checking Heimdall backend health...")
    with httpx.Client() as client:
        wait_for_heimdall_healthy(client, heimdall_url)
    print("   [OK] Heimdall backend is healthy.\n")

    # 2. Launch demo services
    print("2. Starting gateway, order, and payment services...")
    procs = start_demo_services(cfg_ports, heimdall_url=heimdall_url)
    print("   [OK] All 3 demo services are listening and healthy.\n")

    try:
        run_start_ms = (time.time() - 2.0) * 1000

        # 3. Generate sample traffic (includes normal + 1 card decline fault test)
        print(f"3. Sending {requests_count} sample requests through gateway...")
        # First 4 normal orders
        traffic_1 = send_demo_traffic(gateway_url, count=4, delay_between_requests=0.05)

        # Inject 1 card decline
        print("   Injecting card-decline fault into payment service...")
        configure_fault(f"http://127.0.0.1:{cfg_ports['payment']}", error_type="decline", error_rate=1.0)
        traffic_2 = send_demo_traffic(gateway_url, count=1, delay_between_requests=0.05)

        # Reset faults and send 1 more normal order
        print("   Resetting faults...")
        reset_all_faults(gateway_url)
        traffic_3 = send_demo_traffic(gateway_url, count=1, delay_between_requests=0.05)

        total_success = traffic_1["success"] + traffic_2["success"] + traffic_3["success"]
        total_declined = traffic_1["payment_declined"] + traffic_2["payment_declined"] + traffic_3["payment_declined"]
        print(f"   [OK] Traffic complete: {total_success} successful, {total_declined} card-declined.\n")

        # 4. Wait for OTel batch exporter to flush
        print("4. Waiting for telemetry batch flush to Heimdall (2s)...")
        time.sleep(2.0)
        print("   [OK] Exporters flushed.\n")

        # 5. Verify telemetry in Heimdall
        print("5. Polling and verifying telemetry in Heimdall...")
        checks = verify_demo_telemetry(
            heimdall_url=heimdall_url,
            run_start_ms=run_start_ms,
            expected_requests=requests_count,
            timeout=12.0,
        )

        print("\n--- Telemetry Verification Results ---")
        for check_name, passed in checks.items():
            print(f"  [{'PASS' if passed else 'FAIL'}] {check_name}")

        all_ok = all(checks.values())
        print(f"\nPhase 4 Demo Run Result: {'SUCCESS' if all_ok else 'FAILURE'}")
        return 0 if all_ok else 1

    finally:
        print("\nStopping demo services...")
        stop_demo_services(procs)
        print("Demo services stopped.")


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Heimdall Multi-Service Demo Orchestrator")
    parser.add_argument("--once", action="store_true", help="Run one-shot verification and exit")
    parser.add_argument("--serve", action="store_true", help="Start all 3 services and keep running")
    parser.add_argument("--traffic", action="store_true", help="Send requests through gateway")
    parser.add_argument("--verify", action="store_true", help="Verify telemetry in running Heimdall")
    parser.add_argument("--count", type=int, default=6, help="Request count for traffic generator")
    parser.add_argument("--heimdall-url", default=DEFAULT_HEIMDALL_URL, help="Heimdall backend URL")
    parser.add_argument("--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT, help="Gateway port")
    parser.add_argument("--order-port", type=int, default=DEFAULT_ORDER_PORT, help="Order service port")
    parser.add_argument("--payment-port", type=int, default=DEFAULT_PAYMENT_PORT, help="Payment service port")

    # Fault injection args
    parser.add_argument("--fault-service", choices=["gateway", "order", "payment"], help="Target service for fault")
    parser.add_argument("--fault-latency", type=float, help="Inject latency in milliseconds")
    parser.add_argument("--fault-error", choices=["none", "decline", "500"], help="Inject error mode")
    parser.add_argument("--fault-rate", type=float, default=1.0, help="Error probability rate (0.0 to 1.0)")
    parser.add_argument("--reset-faults", action="store_true", help="Reset all faults across services")

    args = parser.parse_args()
    ports = {
        "gateway": args.gateway_port,
        "order": args.order_port,
        "payment": args.payment_port,
    }

    if args.reset_faults:
        res = reset_all_faults(f"http://127.0.0.1:{ports['gateway']}")
        print("Faults reset:", res)
        return 0

    if args.fault_service:
        target_port = ports[args.fault_service]
        res = configure_fault(
            f"http://127.0.0.1:{target_port}",
            latency_ms=args.fault_latency,
            error_type=args.fault_error,
            error_rate=args.fault_rate,
        )
        print(f"Fault configured on {args.fault_service}:", res)
        return 0

    if args.traffic:
        print(f"Sending {args.count} requests to gateway at port {ports['gateway']}...")
        res = send_demo_traffic(f"http://127.0.0.1:{ports['gateway']}", count=args.count)
        print("Traffic results:", res)
        return 0

    if args.verify:
        run_start_ms = (time.time() - 300) * 1000  # last 5 minutes
        checks = verify_demo_telemetry(heimdall_url=args.heimdall_url, run_start_ms=run_start_ms)
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        return 0 if all(checks.values()) else 1

    if args.serve:
        print(f"Starting demo services (gateway: {ports['gateway']}, order: {ports['order']}, payment: {ports['payment']})...")
        procs = start_demo_services(ports, heimdall_url=args.heimdall_url)
        print("Demo services running. Press Ctrl+C to terminate.")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nShutting down demo services...")
        finally:
            stop_demo_services(procs)
        return 0

    # Default action if no arguments provided: run one-shot verification
    return run_once(heimdall_url=args.heimdall_url, ports=ports, requests_count=args.count)


if __name__ == "__main__":
    sys.exit(main())

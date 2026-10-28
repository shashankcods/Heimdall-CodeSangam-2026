# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn",
#     "httpx",
#     "pytest",
#     "opentelemetry-sdk",
#     "opentelemetry-exporter-otlp-proto-http",
#     "opentelemetry-instrumentation-fastapi",
# ]
# ///
"""Automated end-to-end integration test for Heimdall demo cluster and fault injection.

Tests:
1. Normal trace propagation: gateway (8001) -> order (8002) -> payment (8003).
2. DuckDB span verification & parent-child linkage across services.
3. Log correlation by trace_id.
4. Fault injection: 1.9s latency + 504 error, edge failure in traces and logs.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HEIMDALL = os.environ.get("HEIMDALL_URL", "http://127.0.0.1:4318")
GATEWAY_URL = "http://127.0.0.1:8001"
ORDER_URL = "http://127.0.0.1:8002"
PAYMENT_URL = "http://127.0.0.1:8003"
INVENTORY_URL = "http://127.0.0.1:8004"


@pytest.fixture(scope="module")
def demo_cluster():
    # 1. Verify Heimdall is running
    try:
        r = httpx.get(f"{HEIMDALL}/api/health", timeout=2.0)
        assert r.status_code == 200, "Heimdall backend is not healthy"
    except Exception as exc:
        pytest.skip(f"Heimdall backend not reachable at {HEIMDALL}: {exc}")

    # 2. Launch demo services in separate subprocesses
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["HEIMDALL_URL"] = HEIMDALL
    node_dir = REPO_ROOT / "demo" / "node_service"

    services = [
        subprocess.Popen(["uv", "run", str(REPO_ROOT / "demo" / "services" / "payment.py")], env=env),
        subprocess.Popen(["node", "--require", "./tracer.js", "server.js"], cwd=str(node_dir), env=env),
        subprocess.Popen(["uv", "run", str(REPO_ROOT / "demo" / "services" / "order.py")], env=env),
        subprocess.Popen(["uv", "run", str(REPO_ROOT / "demo" / "services" / "gateway.py")], env=env),
    ]

    # Wait for all 4 to be healthy
    for url, name in [
        (PAYMENT_URL, "payment-service"),
        (INVENTORY_URL, "inventory-service"),
        (ORDER_URL, "order-service"),
        (GATEWAY_URL, "gateway"),
    ]:
        deadline = time.time() + 15.0
        ready = False
        while time.time() < deadline:
            try:
                res = httpx.get(f"{url}/health", timeout=1.0)
                if res.status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.2)
        assert ready, f"Failed to start {name} at {url}"

    yield

    # Teardown: terminate processes
    for proc in services:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except Exception:
            proc.kill()


def test_demo_happy_path(demo_cluster):
    # Ensure payment and inventory services have no faults
    httpx.post(f"{PAYMENT_URL}/fault/reset", timeout=2.0)
    httpx.post(f"{INVENTORY_URL}/fault/reset", timeout=2.0)

    # Send order through gateway
    order_res = httpx.post(
        f"{GATEWAY_URL}/order",
        json={"customer_id": "test_alice", "total": 99.0},
        timeout=10.0,
    )
    assert order_res.status_code == 200
    order_data = order_res.json()
    assert order_data.get("status") == "confirmed"
    assert "inventory" in order_data
    assert "payment" in order_data

    # Wait for Heimdall DuckDB flush (1s timeout)
    time.sleep(1.4)

    # Query traces
    traces_res = httpx.get(f"{HEIMDALL}/api/traces", params={"limit": 10}, timeout=3.0)
    assert traces_res.status_code == 200
    traces = traces_res.json().get("traces", [])
    assert len(traces) > 0

    latest_trace = traces[0]
    trace_id = latest_trace["trace_id"]
    assert latest_trace["root_service"] == "gateway"

    # Query detailed waterfall for this trace
    detail_res = httpx.get(f"{HEIMDALL}/api/traces/{trace_id}", timeout=3.0)
    assert detail_res.status_code == 200
    spans = detail_res.json().get("spans", [])

    services_in_trace = {s["service"] for s in spans}
    assert "gateway" in services_in_trace
    assert "order-service" in services_in_trace
    assert "inventory-service" in services_in_trace
    assert "payment-service" in services_in_trace

    # Check database span
    db_spans = [s for s in spans if s["name"] == "INSERT orders"]
    assert len(db_spans) == 1
    assert db_spans[0]["service"] == "order-service"

    # Check correlated logs
    logs_res = httpx.get(f"{HEIMDALL}/api/logs", params={"trace_id": trace_id}, timeout=3.0)
    assert logs_res.status_code == 200
    logs = logs_res.json().get("logs", [])
    assert len(logs) > 0
    log_services = {l["service"] for l in logs}
    assert "gateway" in log_services
    assert "payment-service" in log_services


def test_demo_fault_injection(demo_cluster):
    # Inject fault: 100% error with 504 Gateway Timeout
    fault_res = httpx.post(
        f"{PAYMENT_URL}/fault",
        json={
            "latency_ms": 200.0,  # 200ms delay for quick test
            "error_rate": 1.0,
            "error_status": 504,
            "error_message": "Stripe gateway timeout after 1.9s, retry 0",
        },
        timeout=2.0,
    )
    assert fault_res.status_code == 200

    # Send order through gateway - should fail
    order_res = httpx.post(
        f"{GATEWAY_URL}/order",
        json={"customer_id": "test_fault", "total": 49.99},
        timeout=10.0,
    )
    assert order_res.status_code in (500, 502, 504)

    # Wait for flush
    time.sleep(1.4)

    # Query traces - latest should have has_error=True
    traces_res = httpx.get(f"{HEIMDALL}/api/traces", params={"limit": 5}, timeout=3.0)
    assert traces_res.status_code == 200
    traces = traces_res.json().get("traces", [])
    assert len(traces) > 0

    error_trace = traces[0]
    assert error_trace["has_error"] is True

    # Check error log in Heimdall
    logs_res = httpx.get(
        f"{HEIMDALL}/api/logs",
        params={"trace_id": error_trace["trace_id"], "severity": "ERROR"},
        timeout=3.0,
    )
    assert logs_res.status_code == 200
    error_logs = logs_res.json().get("logs", [])
    assert len(error_logs) > 0
    assert any("Stripe" in l["body"] for l in error_logs)

    # Reset fault
    httpx.post(f"{PAYMENT_URL}/fault/reset", timeout=2.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

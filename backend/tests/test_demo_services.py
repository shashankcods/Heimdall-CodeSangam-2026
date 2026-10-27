"""Automated tests for Heimdall multi-service demo and fault injection."""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

# Ensure repo root is on sys.path
repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from demo.run_demo import (
    DemoError,
    ServiceStartupError,
    send_demo_traffic,
    stop_demo_services,
    verify_demo_telemetry,
    wait_for_heimdall_healthy,
    wait_for_service_ready,
)
from demo.services.common import (
    FaultState,
    inject_trace_context,
    register_fault_routes,
)
from demo.services.gateway import create_app as create_gateway_app
from demo.services.order import create_app as create_order_app
from demo.services.payment import create_app as create_payment_app, fault_state as payment_fault_state


# ---------------------------------------------------------------------------
# 1. Fault State & Fault Route Validation Tests
# ---------------------------------------------------------------------------

def test_fault_state_bounds_and_validation():
    state = FaultState()
    assert state.latency_ms == 0.0
    assert state.error_type == "none"
    assert state.error_rate == 1.0

    # Valid latency
    state.set_latency(250.0)
    assert state.latency_ms == 250.0

    # Invalid latency (negative)
    with pytest.raises(ValueError, match="between 0 and 30000"):
        state.set_latency(-10.0)

    # Invalid latency (exceeds max)
    with pytest.raises(ValueError, match="between 0 and 30000"):
        state.set_latency(35000.0)

    # Valid error settings
    state.set_error("decline", 0.5)
    assert state.error_type == "decline"
    assert state.error_rate == 0.5

    # Invalid error type
    with pytest.raises(ValueError, match="Invalid error_type"):
        state.set_error("explode", 1.0)

    # Invalid error rate
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        state.set_error("500", 1.5)

    # Reset
    state.reset()
    assert state.latency_ms == 0.0
    assert state.error_type == "none"
    assert state.error_rate == 1.0


def test_fault_routes_api():
    from fastapi import FastAPI
    app = FastAPI()
    state = FaultState()
    register_fault_routes(app, state, "test-service")
    client = TestClient(app)

    # Set latency
    r = client.post("/fault/latency", json={"delay_ms": 150})
    assert r.status_code == 200
    assert r.json()["fault"]["latency_ms"] == 150.0

    # Invalid latency returns 422
    r = client.post("/fault/latency", json={"delay_ms": -5})
    assert r.status_code == 422

    # Set error
    r = client.post("/fault/error", json={"error_type": "decline", "rate": 0.8})
    assert r.status_code == 200
    assert r.json()["fault"]["error_type"] == "decline"

    # Invalid error returns 422
    r = client.post("/fault/error", json={"error_type": "invalid_mode"})
    assert r.status_code == 422

    # Status route
    r = client.get("/fault/status")
    assert r.status_code == 200
    assert r.json()["fault"]["error_type"] == "decline"

    # Reset
    r = client.post("/fault/reset")
    assert r.status_code == 200
    assert r.json()["fault"]["latency_ms"] == 0.0
    assert r.json()["fault"]["error_type"] == "none"


# ---------------------------------------------------------------------------
# 2. Payment Service Tests
# ---------------------------------------------------------------------------

def test_payment_service_success():
    app, _, _ = create_payment_app(service_name="payment")
    payment_fault_state.reset()
    client = TestClient(app)

    # Health check
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "payment"

    # Successful payment
    r = client.post("/pay", json={"order_id": "ord_101", "amount": 75.50})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "authorized"
    assert data["order_id"] == "ord_101"
    assert "transaction_id" in data


def test_payment_service_card_decline_business_outcome():
    app, _, _ = create_payment_app(service_name="payment")
    payment_fault_state.set_error("decline", rate=1.0)
    try:
        client = TestClient(app)
        r = client.post("/pay", json={"order_id": "ord_102", "amount": 99.00})
        # Card decline returns HTTP 402 with structured reason
        assert r.status_code == 402
        data = r.json()
        assert data["status"] == "declined"
        assert data["reason"] == "insufficient_funds"
    finally:
        payment_fault_state.reset()


def test_payment_service_500_internal_failure():
    app, _, _ = create_payment_app(service_name="payment")
    payment_fault_state.set_error("500", rate=1.0)
    try:
        client = TestClient(app)
        r = client.post("/pay", json={"order_id": "ord_103", "amount": 120.00})
        assert r.status_code == 500
        assert "Downstream payment gateway error" in r.text
    finally:
        payment_fault_state.reset()


# ---------------------------------------------------------------------------
# 3. Order Service Tests & Context Propagation
# ---------------------------------------------------------------------------

def test_order_service_success_and_context_propagation():
    captured_headers: dict[str, str] = {}

    def mock_payment_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pay"
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json={"status": "authorized", "transaction_id": "tx_mock_123"},
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_payment_handler))
    app, _, _ = create_order_app(
        service_name="order",
        payment_url="http://mock-payment",
        http_client=mock_client,
    )
    client = TestClient(app)

    payload = {
        "order_id": "ord_201",
        "items": [{"name": "Widget A", "quantity": 2, "price": 10.0}],
        "customer_id": "cust_42",
        "amount": 20.0,
    }
    r = client.post("/order", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "created"
    assert data["order_id"] == "ord_201"
    assert data["payment"]["transaction_id"] == "tx_mock_123"


def test_order_service_handles_payment_decline():
    def mock_payment_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={"status": "declined", "reason": "insufficient_funds"},
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_payment_handler))
    app, _, _ = create_order_app(
        service_name="order",
        payment_url="http://mock-payment",
        http_client=mock_client,
    )
    client = TestClient(app)

    r = client.post("/order", json={"order_id": "ord_202", "amount": 50.0})
    assert r.status_code == 402
    assert r.json()["status"] == "payment_declined"


def test_order_service_handles_payment_500():
    def mock_payment_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Gateway Error")

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_payment_handler))
    app, _, _ = create_order_app(
        service_name="order",
        payment_url="http://mock-payment",
        http_client=mock_client,
    )
    client = TestClient(app)

    r = client.post("/order", json={"order_id": "ord_203", "amount": 50.0})
    assert r.status_code == 502
    assert r.json()["status"] == "downstream_error"


# ---------------------------------------------------------------------------
# 4. Gateway Service Tests
# ---------------------------------------------------------------------------

def test_gateway_service_order_success():
    def mock_order_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/order"
        return httpx.Response(
            200,
            json={"status": "created", "order_id": "ord_301", "payment": {"status": "authorized"}},
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_order_handler))
    app, _, _ = create_gateway_app(
        service_name="gateway",
        order_url="http://mock-order",
        payment_url="http://mock-payment",
        http_client=mock_client,
    )
    client = TestClient(app)

    r = client.post("/order", json={"amount": 49.99, "customer_id": "cust_301"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "success"
    assert "order_id" in data


def test_gateway_service_handles_order_decline():
    def mock_order_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={"status": "payment_declined", "detail": {"reason": "card_declined"}},
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_order_handler))
    app, _, _ = create_gateway_app(
        service_name="gateway",
        order_url="http://mock-order",
        payment_url="http://mock-payment",
        http_client=mock_client,
    )
    client = TestClient(app)

    r = client.post("/order", json={"amount": 49.99})
    assert r.status_code == 402
    assert r.json()["status"] == "payment_declined"


def test_gateway_service_handles_order_unreachable():
    def mock_order_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_order_handler))
    app, _, _ = create_gateway_app(
        service_name="gateway",
        order_url="http://mock-order",
        payment_url="http://mock-payment",
        http_client=mock_client,
    )
    client = TestClient(app)

    r = client.post("/order", json={"amount": 49.99})
    assert r.status_code == 502
    assert r.json()["status"] == "downstream_error"


# ---------------------------------------------------------------------------
# 5. Traffic Generator & Orchestration Readiness Tests
# ---------------------------------------------------------------------------

def test_traffic_generator_accounting():
    responses = [
        httpx.Response(200, json={"status": "success"}),
        httpx.Response(200, json={"status": "success"}),
        httpx.Response(402, json={"status": "payment_declined"}),
        httpx.Response(502, json={"status": "downstream_error"}),
    ]
    call_index = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_index
        resp = responses[call_index % len(responses)]
        call_index += 1
        return resp

    transport = httpx.MockTransport(mock_handler)
    with patch("httpx.Client", return_value=httpx.Client(transport=transport)):
        results = send_demo_traffic("http://mock-gateway:8001", count=4, delay_between_requests=0.0)

    assert results["success"] == 2
    assert results["payment_declined"] == 1
    assert results["downstream_error"] == 1
    assert results["network_error"] == 0


def test_wait_for_service_ready_success_and_timeout():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "ready" in str(request.url):
            return httpx.Response(200, json={"status": "ok"})
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(mock_handler)
    with httpx.Client(transport=transport) as client:
        # Success
        wait_for_service_ready(client, "test-svc", "http://ready:8000", timeout=0.5, poll_interval=0.01)

        # Timeout
        with pytest.raises(ServiceStartupError, match="failed to become ready"):
            wait_for_service_ready(client, "test-svc", "http://unreachable:8000", timeout=0.05, poll_interval=0.01)


def test_wait_for_heimdall_healthy_success_and_timeout():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "healthy" in str(request.url):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(500, text="Internal error")

    transport = httpx.MockTransport(mock_handler)
    with httpx.Client(transport=transport) as client:
        # Success
        wait_for_heimdall_healthy(client, "http://healthy:4318", timeout=0.5)

        # Timeout
        with pytest.raises(DemoError, match="unreachable"):
            wait_for_heimdall_healthy(client, "http://broken:4318", timeout=0.05)


def test_stop_demo_services_graceful_and_force():
    mock_proc_1 = MagicMock()
    mock_proc_1.poll.return_value = None

    mock_proc_2 = MagicMock()
    mock_proc_2.poll.return_value = 0  # Already terminated

    stop_demo_services([mock_proc_1, mock_proc_2])
    mock_proc_1.terminate.assert_called_once()
    mock_proc_2.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Telemetry Verification Logic Analysis
# ---------------------------------------------------------------------------

def test_verify_demo_telemetry_passes_all_checks():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/traces":
            return httpx.Response(200, json={"traces": [{"trace_id": "abc12345"}]})
        elif path == "/api/traces/abc12345":
            spans = [
                {"service": "gateway", "span_id": "s_gw", "parent_span_id": None},
                {"service": "gateway", "span_id": "s_gw_client", "parent_span_id": "s_gw"},
                {"service": "order", "span_id": "s_ord", "parent_span_id": "s_gw_client"},
                {"service": "order", "span_id": "s_ord_client", "parent_span_id": "s_ord"},
                {"service": "payment", "span_id": "s_pay", "parent_span_id": "s_ord_client"},
            ]
            return httpx.Response(200, json={"spans": spans})
        elif path == "/api/logs":
            return httpx.Response(200, json={"logs": [{"trace_id": "abc12345", "severity": "INFO"}]})
        elif path == "/api/service-map":
            return httpx.Response(
                200,
                json={
                    "nodes": [{"name": "gateway"}, {"name": "order"}, {"name": "payment"}],
                    "edges": [
                        {"source": "gateway", "target": "order", "call_count": 5},
                        {"source": "order", "target": "payment", "call_count": 5},
                    ],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    with patch("httpx.Client", return_value=httpx.Client(transport=transport)):
        checks = verify_demo_telemetry(heimdall_url="http://mock-heimdall:4318", run_start_ms=1000.0, timeout=1.0)

    assert checks["traces received"] is True
    assert checks["trace contains all 3 services"] is True
    assert checks["gateway -> order span parent link"] is True
    assert checks["order -> payment span parent link"] is True
    assert checks["service map has gateway -> order"] is True
    assert checks["service map has order -> payment"] is True

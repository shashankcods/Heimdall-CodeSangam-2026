import pytest
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient

import heimdall.api.main as main
from heimdall.api.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "api.duckdb"))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def schema(client):
    return client.get("/openapi.json").json()


def response_ref(schema, path, status="200"):
    content = schema["paths"][path]["get"]["responses"][status]["content"]["application/json"]["schema"]
    return content["$ref"].rsplit("/", 1)[-1]


@pytest.mark.parametrize("path, model", [
    ("/api/health", "Health"),
    ("/api/services", "ServiceList"),
    ("/api/traces", "TraceList"),
    ("/api/traces/{trace_id}", "TraceDetail"),
    ("/api/logs", "LogList"),
    ("/api/service-map", "ServiceMap"),
    ("/api/anomalies", "AnomalyReport"),
])
def test_every_json_endpoint_documents_its_response(schema, path, model):
    assert response_ref(schema, path) == model


def test_schema_lists_real_fields_and_types(schema):
    trace = schema["components"]["schemas"]["TraceSummary"]
    assert set(trace["required"]) >= {"trace_id", "duration_ms", "has_error", "http_status"}
    assert trace["properties"]["has_error"]["type"] == "boolean"
    span = schema["components"]["schemas"]["SpanDetail"]
    status = next(o for o in span["properties"]["status_code"]["anyOf"] if "enum" in o)
    assert status["enum"] == ["UNSET", "OK", "ERROR"]


def test_error_responses_are_documented(schema):
    assert response_ref(schema, "/api/traces/{trace_id}", "404") == "ErrorDetail"
    assert response_ref(schema, "/api/logs", "400") == "ErrorDetail"


def test_response_that_drifts_from_its_model_is_rejected(client, monkeypatch):
    # A query returning a renamed field must fail loudly, not silently break the frontend.
    monkeypatch.setattr(main, "list_traces", lambda cur, **kw: [{"traceId": "abc"}])
    with pytest.raises(ResponseValidationError):
        client.get("/api/traces")

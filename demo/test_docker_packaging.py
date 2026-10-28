"""Automated packaging and integration tests for Heimdall multi-stage Dockerfile

and complete docker-compose.yml orchestration stack.
"""

import sys
from pathlib import Path
from starlette.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))



def test_dockerfile_multi_stage_structure():
    """Verify that root Dockerfile implements a robust multi-stage build."""
    dockerfile_path = REPO_ROOT / "Dockerfile"
    assert dockerfile_path.is_file(), "Root Dockerfile must exist"

    content = dockerfile_path.read_text(encoding="utf-8")

    # Stage 1: Frontend build
    assert "FROM node:" in content, "Stage 1 must use a Node base image"
    assert "AS frontend-builder" in content, "Stage 1 must be named frontend-builder"
    assert "npm run build" in content, "Stage 1 must run npm run build"

    # Stage 2: Runtime engine
    assert "FROM python:" in content, "Stage 2 must use a Python base image"
    assert "AS heimdall-engine" in content, "Stage 2 must be named heimdall-engine"
    assert "COPY --from=frontend-builder" in content, "Stage 2 must copy built static assets from Stage 1"
    assert "/app/static" in content, "Built static assets must be copied to /app/static"
    assert "EXPOSE 4318" in content, "Dockerfile must expose port 4318"
    assert "HEALTHCHECK" in content, "Dockerfile must define a container healthcheck"
    assert "demo.serve_heimdall:app" in content, "Dockerfile must launch demo.serve_heimdall:app"


def test_docker_compose_orchestration_spec():
    """Verify that root docker-compose.yml declares all services, healthchecks, and networking."""
    compose_path = REPO_ROOT / "docker-compose.yml"
    assert compose_path.is_file(), "Root docker-compose.yml must exist"

    content = compose_path.read_text(encoding="utf-8")

    # Core engine service
    assert "heimdall:" in content, "docker-compose must define the heimdall engine service"
    assert '"4318:4318"' in content or "'4318:4318'" in content, "heimdall must map port 4318:4318"
    assert "heimdall-data:/data" in content, "heimdall must mount persistent volume /data"

    # Microservice cluster services
    assert "payment-service:" in content, "docker-compose must define payment-service"
    assert "inventory-service:" in content, "docker-compose must define inventory-service"
    assert "order-service:" in content, "docker-compose must define order-service"
    assert "gateway:" in content, "docker-compose must define gateway"
    assert "load-generator:" in content, "docker-compose must define load-generator"

    # Inter-service dependencies and networking
    assert "HEIMDALL_URL=http://heimdall:4318" in content, "Demo services must target the internal heimdall container"
    assert "condition: service_healthy" in content, "Services must wait for healthy upstream dependencies"
    assert "heimdall-net" in content, "docker-compose must define a unified network"
    assert "heimdall-data" in content, "docker-compose must define a persistent named volume"


def test_serve_heimdall_spa_and_api_integration():
    """Verify that demo.serve_heimdall serves UI at root, supports SPA routing, and preserves APIs."""
    from demo.serve_heimdall import app

    with TestClient(app) as client:
        # 1. Root GET / serves index.html
        res_root = client.get("/")
        assert res_root.status_code == 200
        assert "<!doctype html>" in res_root.text.lower()

        # 2. SPA client-side routing fallback serves index.html
        res_spa = client.get("/traces")
        assert res_spa.status_code == 200
        assert "<!doctype html>" in res_spa.text.lower()

        res_logs_page = client.get("/logs")
        assert res_logs_page.status_code == 200
        assert "<!doctype html>" in res_logs_page.text.lower()

        # 3. Core REST API endpoints remain fully functional and unshadowed
        res_traces = client.get("/api/traces")
        assert res_traces.status_code == 200
        assert "traces" in res_traces.json()

        res_services = client.get("/api/services")
        assert res_services.status_code == 200
        assert "services" in res_services.json()

        res_map = client.get("/api/service-map")
        assert res_map.status_code == 200
        assert "nodes" in res_map.json()
        assert "edges" in res_map.json()

        # 4. OTLP protobuf ingestion endpoint remains unshadowed
        res_otlp = client.post(
            "/v1/traces",
            content=ExportTraceServiceRequest().SerializeToString(),
            headers={"Content-Type": "application/x-protobuf"},
        )
        assert res_otlp.status_code == 200

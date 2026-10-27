# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn",
# ]
# ///
"""OTLP Catcher Server: saves raw binary request bodies from /v1/traces and /v1/logs.

Used to capture wire fixtures from OpenTelemetry SDKs (Python, Node, etc.).

Usage:
    uv run fixtures/catcher.py [--port 4319]
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, Request, Response

FIXTURES_DIR = Path(__file__).resolve().parent / "otlp"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="OTLP Wire Catcher")


def save_payload(kind: str, data: bytes, is_gzip: bool, custom_name: str | None = None) -> Path:
    if custom_name:
        filename = custom_name if custom_name.endswith((".pb", ".bin")) else f"{custom_name}.pb"
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        gz = "_gzip" if is_gzip else ""
        filename = f"{kind}_{ts}{gz}.pb"

    filepath = FIXTURES_DIR / filename
    filepath.write_bytes(data)
    print(f"[CATCHER] Saved {len(data)} bytes ({'gzip' if is_gzip else 'raw'}) -> {filepath.name}")
    return filepath


@app.post("/v1/traces")
async def catch_traces(
    request: Request,
    content_encoding: str | None = Header(None),
    x_fixture_name: str | None = Header(None),
):
    body = await request.body()
    is_gzip = content_encoding == "gzip" or (len(body) >= 2 and body[:2] == b"\x1f\x8b")
    save_payload("traces", body, is_gzip, x_fixture_name or request.query_params.get("name"))
    return Response(content=b"", media_type="application/x-protobuf", status_code=200)


@app.post("/v1/logs")
async def catch_logs(
    request: Request,
    content_encoding: str | None = Header(None),
    x_fixture_name: str | None = Header(None),
):
    body = await request.body()
    is_gzip = content_encoding == "gzip" or (len(body) >= 2 and body[:2] == b"\x1f\x8b")
    save_payload("logs", body, is_gzip, x_fixture_name or request.query_params.get("name"))
    return Response(content=b"", media_type="application/x-protobuf", status_code=200)


def main():
    parser = argparse.ArgumentParser(description="OTLP Wire Catcher Server")
    parser.add_argument("--port", type=int, default=4319, help="Listening port (default 4319)")
    args = parser.parse_args()

    print(f"Starting OTLP Catcher on port {args.port}...")
    print(f"Saving incoming payloads to: {FIXTURES_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

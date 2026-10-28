"""Heimdall Production ASGI entrypoint.

Serves the Heimdall OTLP ingest gateway and REST query APIs alongside the
pre-built React + TypeScript frontend at `/` with single-page application (SPA)
client-side routing fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

# Import core backend application
from heimdall.api.main import app

# Determine static frontend directory:
# 1. HEIMDALL_STATIC_DIR environment variable (set in Docker image)
# 2. Local frontend/dist (during local packaging / dev testing)
_env_static = os.environ.get("HEIMDALL_STATIC_DIR")
if _env_static:
    STATIC_DIR = Path(_env_static).resolve()
else:
    _repo_root = Path(__file__).resolve().parent.parent
    STATIC_DIR = (_repo_root / "frontend" / "dist").resolve()


def mount_static_frontend(application, dist_path: Path) -> bool:
    """Mounts assets and SPA fallback onto the FastAPI application if dist exists."""
    if not dist_path.is_dir() or not (dist_path / "index.html").is_file():
        return False

    assets_path = dist_path / "assets"
    if assets_path.is_dir():
        application.mount(
            "/assets",
            StaticFiles(directory=str(assets_path)),
            name="static_assets",
        )

    index_html = dist_path / "index.html"

    @application.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(request: Request, full_path: str):
        # Specific static file match inside dist (e.g. favicon.ico, vite.svg)
        candidate = dist_path / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        # SPA client-side routing fallback: serve index.html
        return FileResponse(index_html)

    return True


# Mount static files on load
_mounted = mount_static_frontend(app, STATIC_DIR)

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HEIMDALL_HOST", "0.0.0.0")
    port = int(os.environ.get("HEIMDALL_PORT", "4318"))
    print(f"Starting Heimdall on http://{host}:{port} (Static frontend mounted: {_mounted})")
    uvicorn.run("demo.serve_heimdall:app", host=host, port=port)

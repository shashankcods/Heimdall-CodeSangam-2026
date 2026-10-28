# ==============================================================================
# Stage 1: Build the React + TypeScript frontend UI
# ==============================================================================
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend

# Install frontend dependencies with layer caching
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

# Copy frontend source and build static distribution
COPY frontend/ ./
RUN npm run build

# ==============================================================================
# Stage 2: Final Heimdall Ingestion Engine & API Server
# ==============================================================================
FROM python:3.12-slim AS heimdall-engine

# Install uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install backend dependencies with layer caching
COPY backend/pyproject.toml /app/backend/pyproject.toml
RUN cd /app/backend && uv pip install --system -e .

# Copy application code
COPY backend /app/backend
COPY demo /app/demo

# Copy built frontend static assets from stage 1
COPY --from=frontend-builder /app/frontend/dist /app/static

# Persistent data directory for DuckDB store
RUN mkdir -p /data
VOLUME ["/data"]

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/backend:/app
ENV HEIMDALL_STATIC_DIR=/app/static
ENV HEIMDALL_DB_PATH=/data/heimdall.duckdb

EXPOSE 4318

HEALTHCHECK --interval=5s --timeout=3s --retries=5 --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4318/api/services')"

CMD ["uvicorn", "demo.serve_heimdall:app", "--host", "0.0.0.0", "--port", "4318"]

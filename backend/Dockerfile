FROM python:3.13-slim

# Install uv from official Astral binary image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Ensure /data volume directory exists for persistent DuckDB storage
RUN mkdir -p /data

# Default environment configuration
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HEIMDALL_DB_PATH=/data/heimdall.duckdb \
    HEIMDALL_MAX_PENDING=100000 \
    HEIMDALL_BATCH_SIZE=500 \
    HEIMDALL_FLUSH_INTERVAL=1.0 \
    HEIMDALL_CORS_ORIGINS="http://localhost:5173,http://127.0.0.1:5173,http://localhost:80,http://frontend,http://frontend:80"

# Copy dependency specifications first for Docker layer caching
COPY pyproject.toml uv.lock ./

# Install locked dependencies without development packages
RUN uv sync --frozen --no-dev

# Copy application source code
COPY heimdall/ ./heimdall/

# Expose OTLP ingest & REST API port
EXPOSE 4318

# Health check probing backend /api/health endpoint
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4318/api/health')" || exit 1

# Start Uvicorn listening on all interfaces (0.0.0.0)
CMD ["uv", "run", "uvicorn", "heimdall.api.main:app", "--host", "0.0.0.0", "--port", "4318"]

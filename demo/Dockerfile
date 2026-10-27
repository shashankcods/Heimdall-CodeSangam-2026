FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DEMO_SERVICE_HOST=0.0.0.0

# Install dependencies using pinned requirements
COPY demo/requirements.txt ./demo/requirements.txt
RUN pip install --no-cache-dir -r demo/requirements.txt

# Copy demo code and structure
COPY demo/ ./demo/

# Default command: runs the orchestrator serve mode if invoked without arguments
CMD ["python", "demo/run_demo.py", "--serve"]

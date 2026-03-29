FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python package and dependencies from pyproject.toml
COPY pyproject.toml .
# Copy source so pip install -e . can find packages
COPY apps/ apps/
COPY libs/ libs/

RUN pip install --no-cache-dir -e .

# Copy remaining project files
COPY . .

# Environment variables with defaults
ENV LOG_LEVEL=INFO \
    PORT=8000 \
    HOST=0.0.0.0

EXPOSE 8000

# Health check — the /health endpoint is defined in apps/api/main.py
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run the FastAPI app via uvicorn
CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

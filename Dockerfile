# ── Stage 1: Python deps ──────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime image ────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Install ffmpeg (required by yt-dlp for audio conversion)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY server.py .
COPY static/ ./static/

# Cloud Run sets PORT env var (default 8080)
ENV PORT=8080

# Use gunicorn for production
CMD exec gunicorn \
    --bind "0.0.0.0:${PORT}" \
    --workers 2 \
    --threads 8 \
    --timeout 300 \
    --keep-alive 5 \
    server:app

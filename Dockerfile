# SmartGymWorkoutManager — homelab image (flipch fork)
FROM python:3.11-slim

WORKDIR /app

# system deps (tzdata for scheduling/calendar; curl for healthcheck)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# config.json + media cache live on a mounted volume (see compose)
ENV PYTHONUNBUFFERED=1

# Flask web UI on 5001, MCP server on 5002
EXPOSE 5001 5002

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5001/healthz || exit 1

# default: web app (the mcp service overrides CMD in compose)
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5001", "--timeout", "120", "app:app"]

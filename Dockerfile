# ── Build stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Keeps Python from buffering stdout/stderr (shows logs immediately in Cloud Run)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first — separate layer so it's cached on code-only changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code, core modules, catalogs, and bundled skill data
COPY app/ ./app/
COPY core/ ./core/
COPY .agents/ ./.agents/
COPY player_ids.json board.json news_depth_overrides.json main.py ./

# Cloud Run injects $PORT at runtime; default to 8080 for local docker run
ENV PORT=8080
EXPOSE 8080

# NOTE: For production, deploy with --min-instances=1 to avoid cold starts:
#   gcloud run deploy fantasy-ai-service ... --min-instances=1
# This keeps one warm instance and eliminates the 5MB player cache cold-start penalty.

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

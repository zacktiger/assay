# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

# Bytecode is written at build time, not per container start; unbuffered so
# logs reach the orchestrator immediately rather than sitting in a pipe.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

# Dependencies first so a source change does not invalidate the install layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "psycopg[binary]>=3.1"

COPY app ./app
COPY qa ./qa
COPY scripts ./scripts

# Run as a non-root user. Nothing in the image needs write access at runtime.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv/app
USER appuser

ENV ENVIRONMENT=production \
    PORT=8000

EXPOSE 8000

# The readiness probe checks the database; liveness deliberately does not.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('PORT','8000')}/health\").read()"

# --proxy-headers so client IPs and scheme survive a load balancer.
# Worker count comes from the environment; see docs/DEPLOY.md on why the
# in-process login throttle affects that choice.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]

#!/bin/sh
# Container entrypoint for the Render deploy.
#
# The schema is created here rather than in a pre-deploy hook because Render's
# pre-deploy command is a paid-plan feature. It is still an ordered release
# step - it runs once, before the server binds, and a failure exits non-zero
# and fails the deploy rather than surfacing as a 500 on the first request.
# It is not the application's startup hook, which would run per worker; with
# WEB_CONCURRENCY=1 there is no CREATE TABLE race either way.
#
# This lives in a file, not in render.yaml, because a start command written
# inline there is re-parsed by the platform and its quotes do not survive.
set -e

python -m scripts.init_db

# exec so uvicorn is PID 1 and receives SIGTERM directly on shutdown.
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --proxy-headers \
    --forwarded-allow-ips='*'

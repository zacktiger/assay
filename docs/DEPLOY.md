# Deployment

## Quick start

```bash
docker compose up --build        # API + Postgres, schema created by the migrate step
curl localhost:8000/health       # {"status":"ok"}
curl localhost:8000/health/ready # {"status":"ready","database":"up"}
```

Without Docker:

```bash
export ENVIRONMENT=production
export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db
pip install -r requirements.txt "psycopg[binary]"

python -m scripts.init_db        # release step - run once, before the API starts
uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
```

## Free-tier deploy (Render)

`render.yaml` is a blueprint for a free Docker web service plus a free Postgres
instance. From the repo root:

1. Push the branch to GitHub.
2. Render dashboard → **New → Blueprint** → pick this repo → **Apply**.
3. First build takes a few minutes. Then:

```bash
curl https://<service>.onrender.com/health        # {"status":"ok"}
curl https://<service>.onrender.com/health/ready  # {"status":"ready","database":"up"}
```

`/docs` is on for this deploy — see the comment in `render.yaml`.

What the free tier costs you, stated plainly:

- **The service sleeps after 15 minutes idle.** The next request pays a cold
  start of roughly 50 seconds. A `/health` probe is enough to wake it.
- **The free database expires 30 days after creation.** Render will not migrate
  it. Recreate the blueprint, or move to a paid instance, before then.
- **One worker, no gateway rate limiting.** Same caveat as everywhere else in
  this document: the login throttle is in-process.
- **No backups.** The free plan has none. Nothing in this deploy is worth
  restoring — it holds demo data.

Render supplies `DATABASE_URL` as a driverless `postgres://` URL, which
SQLAlchemy reads as psycopg2 — a driver this image does not install. The
rewrite to `postgresql+psycopg://` happens in `_database_url` in
`app/config.py`; nothing but the driver is touched. Set `DATABASE_URL`
explicitly and it is honoured as written.

## What `ENVIRONMENT=production` changes

Two defaults flip, and both can be overridden explicitly:

| Setting | development | production | Why |
|---|---|---|---|
| `AUTO_CREATE_TABLES` | true | **false** | `create_all` from N workers at once is a race. Run `scripts.init_db` instead. |
| `ENABLE_DOCS` | true | **false** | `/docs`, `/redoc` and `/openapi.json` publish every field and bound. |

Nothing else is environment-dependent — the same code path runs in both.

## Configuration

Every setting is an environment variable, read once at import. A `.env` file is
loaded if present, and real environment variables always win. Full list with
defaults in `.env.example`.

The ones that matter for a deploy:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./finance.db` | **Must be changed.** SQLite is single-writer and dies with the container. |
| `ENVIRONMENT` | `development` | Set to `production`. |
| `CORS_ORIGINS` | *(empty)* | Empty disables CORS entirely — correct unless a browser calls this directly. Comma-separated allowlist otherwise. |
| `TOKEN_TTL_SECONDS` | `3600` | Tokens are opaque and revocable via `POST /auth/logout`. |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` | `10` / `900` | See the worker-count caveat below. |
| `MAX_PAGE_SIZE` | `100` | Hard cap on any list endpoint. |
| `LOG_LEVEL` | `INFO` | Application logs go to stdout. |
| `LLM_PROVIDER` | `stub` | `anthropic` for live Claude; needs `ANTHROPIC_API_KEY`. |
| `WEB_CONCURRENCY` | `1` | Uvicorn worker count. Read the next section before raising it. |

## Worker count — read this before scaling

**The login throttle is in-process.** With N workers an attacker gets N times the
configured allowance, and a restart clears the counters. The Dockerfile defaults
`WEB_CONCURRENCY` to 1 for that reason.

Before running more than one worker, move rate limiting to the layer in front —
an ingress rule, an API gateway, or a shared Redis counter. The in-process
throttle is a real improvement over nothing, and it is not a substitute for
gateway-level protection.

Nothing else in the app holds process-local state; the throttle is the only
thing blocking horizontal scale.

## Probes

| Path | Checks | Use for |
|---|---|---|
| `GET /health` | Process is up. Deliberately does **not** touch the database. | Liveness. A load balancer polling this must not open a connection per probe. |
| `GET /health/ready` | Opens a connection, runs `SELECT 1`. Returns 503 if the database is down. | Readiness, and the gate before sending traffic. |

Startup also runs a connectivity check, so a bad `DATABASE_URL` fails the boot
rather than surfacing as a 500 on the first real request.

## Schema changes

`scripts/init_db.py` is **create-only**. It adds tables that do not exist; it
will not alter a table whose columns changed. It is idempotent and safe to run
on every deploy.

Any change to an existing table against a database holding real data needs
Alembic, which is not set up here. That is the main thing standing between this
and a long-lived production deployment — see [Known gaps](#known-gaps).

## Logging

Application logs go to stdout as:

```
2026-09-08 16:57:58,987 INFO app GET /health -> 200 in 1.0ms [log-check-77]
```

Every response carries an `X-Request-ID` header matching the bracketed id, so a
user-reported failure maps to a log line without guessing at timestamps. A
client-supplied `X-Request-ID` is honoured, which lets an upstream proxy's trace
id flow through.

Unhandled exceptions are logged with a traceback and returned as a bare
`{"detail": "Internal Server Error"}` — no internals in the response body,
verified by `test_error_responses_do_not_leak_internals`.

## Security posture

Set by the app:

- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` on every response
- Passwords hashed with PBKDF2-HMAC-SHA256, 200k rounds, per-user salt
- Bearer tokens stored only as SHA-256 digests — a database dump yields no live
  sessions
- Login failures throttled per account; the lockout also blocks the correct
  password, so an attacker who guesses right on attempt 11 still gets 429
- Ownership checked before existence on every read path — a cross-tenant request
  gets a 404 identical to a genuinely missing row

Left to the platform, deliberately:

- **TLS.** Terminate at the load balancer and run with `--proxy-headers`.
- **Rate limiting beyond login.** No global limit; use the gateway.
- **Secret storage.** `ANTHROPIC_API_KEY` comes from the environment; use the
  platform's secret manager, not a baked `.env`.

## Container

- `python:3.13-slim`, non-root user (uid 10001), no build toolchain in the final
  image
- Dependencies installed before source is copied, so a code change does not
  invalidate the install layer
- `HEALTHCHECK` hits `/health`
- `--proxy-headers --forwarded-allow-ips='*'` so client IPs and scheme survive
  the load balancer. Narrow `--forwarded-allow-ips` to the proxy's range if you
  know it.

## Known gaps

Stated plainly rather than discovered later:

1. **No migration tool.** Create-only bootstrap. A column change against live
   data needs Alembic.
2. **Login throttle is per-process.** Caps useful worker count at 1 until rate
   limiting moves to the gateway.
3. **Concurrency is guarded but untested.** The balance is re-read inside the
   transaction with `SELECT … FOR UPDATE` on Postgres, but the test suite runs
   on SQLite, where writes serialise and a lost update cannot be reproduced.
   Untested code is not proven code.
4. **The live LLM path has never been exercised.** Every test runs the
   deterministic stub. `AnthropicProvider` is written against the current SDK
   but has not made a real call — validate it in staging before enabling
   `LLM_PROVIDER=anthropic`.
5. **No backups, no metrics.** Both belong to the platform, neither is
   configured here.

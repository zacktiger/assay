# Defect log

Every entry below was found by running the suite against the application, and
every one has a regression test that fails without the fix. Nothing here is
illustrative — if a defect is listed, it was real, and the test that catches it
is named.

| ID | Severity | Area | Status |
|----|----------|------|--------|
| [BUG-001](#bug-001) | Critical | Input validation | Fixed |
| [BUG-002](#bug-002) | Low | HTTP headers | Fixed |
| [BUG-003](#bug-003) | Low | Observability | Fixed |
| [KNOWN-001](#known-001) | Low | LLM guardrail | Accepted |
| [TRIAGE-001](#triage-001) | — | Test defect | Test corrected |
| [TRIAGE-002](#triage-002) | — | Test defect | Test corrected |

---

## BUG-001

**Every out-of-range money value returned HTTP 500 instead of 422.**

- **Severity:** Critical
- **Found by:** `tests/test_boundaries.py::test_investment_amount_boundaries`
- **Regression test:** the same test — 8 of its 12 cases failed
- **Expected:** `422` with field-level detail
- **Actual:** `500 Internal Server Error`, connection-level failure

### What happened

The `RequestValidationError` handler returned `exc.errors()` directly into a
`JSONResponse`. For a `Decimal` bound, Pydantic includes the constraint in the
error context:

```python
{'type': 'greater_than_equal', 'loc': ('body', 'amount'),
 'ctx': {'ge': Decimal('1000')}}
```

`json.dumps` cannot encode a `Decimal`, so the handler itself raised while
building the error response. Validation was working correctly; reporting the
failure is what broke.

### Why it mattered

It affected every amount outside 1,000–100,000 — the entire rejection path for
the primary money field. A client sending a negative amount got a 500, which
reads as "the server is broken, retry" rather than "your input is invalid". A
retry loop against a 500 is a plausible route to duplicate submissions.

It is also the failure mode least likely to be caught by hand: the happy path
and the in-range boundaries all pass, so the endpoint looks healthy until
someone tests *outside* the range.

### Fix

`app/main.py` — pass the errors through `jsonable_encoder` before serialising.

---

## BUG-002

**JSON responses carried no `X-Content-Type-Options: nosniff` header.**

- **Severity:** Low
- **Found by:** investigating a failed assertion in
  `tests/test_security.py::test_script_payloads_are_returned_as_data_not_html`
- **Regression test:** `tests/test_security.py::test_responses_forbid_mime_sniffing`
- **Expected:** `nosniff` on every response
- **Actual:** responses carried only `content-length` and `content-type`

### What happened

The API accepts and stores arbitrary strings in text fields such as a portfolio
name — correctly, since it is a JSON API and the payload is inert data. What
keeps a stored `<script>` payload inert is the `application/json` content type.
Without `nosniff`, a browser is permitted to disregard that content type and
re-interpret the body as HTML, at which point the stored payload executes.

Low severity because exploiting it needs a browser that content-sniffs and a
route that serves the JSON directly to one. It is a one-line defence.

### Fix

`app/main.py` — middleware setting `X-Content-Type-Options: nosniff` and
`X-Frame-Options: DENY` on every response.

---

## BUG-003

**Application log lines were never emitted.**

- **Severity:** Low (Medium in production — it removes the ability to debug)
- **Found by:** reading the server log during a production-mode smoke test
- **Regression tests:** `tests/test_deployment.py::test_app_logger_is_configured_to_emit`,
  `::test_request_logging_records_method_status_and_request_id`
- **Expected:** one `app` log line per request, carrying the request id
- **Actual:** only uvicorn's own access log; every `logger.info` discarded

### What happened

The request middleware attached an `X-Request-ID` header to every response and
logged the method, path, status, duration and that id. The header appeared. The
log line did not.

`logging.getLogger("app")` had no handler, and the root logger's default level
is `WARNING`, so every `logger.info(...)` was dropped on the floor. uvicorn
configures its own loggers, not the application's.

The failure is quiet in the worst way: the feature looks like it works, because
the half that is visible in a response header does work. It only shows up when
someone tries to trace a reported failure and finds no line to trace.

### Fix

`app/main.py` — `_configure_logging()` attaches a stdout handler to the `app`
logger at a level from `LOG_LEVEL` (default `INFO`).

### Note on the regression test

The first version of this test used `capsys` and failed while the log line was
plainly visible in pytest's captured output. The handler binds `sys.stdout` at
import time, before `capsys` swaps it, so the write goes to the original stream.
The test now attaches its own handler to the `app` logger, which is what the
defect was actually about.

---

## KNOWN-001

**The certainty guardrail flags correctly-negated denials.**

- **Severity:** Low
- **Status:** Accepted, not fixed
- **Documented by:** `tests/test_llm_eval.py::test_guardrail_does_not_flag_negated_certainty_claims`
  (`xfail(strict=True)`)

`app/llm/guardrails.py` matches certainty language by keyword and has no
negation handling, so a *correct* answer phrased as "no index fund is
risk-free" or "this is not a guaranteed return" trips the
`guaranteed_return_claim` violation.

Accepted rather than fixed, deliberately. The failure direction is safe: a
false positive causes the endpoint to substitute the deterministic fallback
text, which still carries the right figures. A false *negative* — letting an
actual guarantee through — is the outcome that harms a user, and widening the
matcher to understand negation would trade a safe failure for a chance at an
unsafe one. Revisit if the fallback rate on live traffic becomes material.

The `strict=True` marker means that if this is ever fixed, the xfail turns into
an `XPASS` failure and forces this entry to be updated rather than left stale.

---

## TRIAGE-001

**Not a defect: `"5e4"` accepted as an investment amount.**

`qa/test_data.py` originally listed scientific notation among the malformed
amounts. The application accepts it and stores 50000.00. On review the *test*
was wrong: `5e4` is an exact, unambiguous representation of 50,000, and
rejecting it would be a formatting preference rather than a correctness
requirement. Moved to `AMOUNT_VALID_STRING_FORMS`.

## TRIAGE-002

**Not a defect: `<` not escaped in JSON response bodies.**

A security assertion required `"<script>"` to be absent from the raw response
body. JSON does not escape `<` and is not required to; the payload round-trips
as an opaque string with a JSON content type, which is correct behaviour. The
assertion was replaced with a content-type check — and chasing it down is what
surfaced [BUG-002](#bug-002), which was the real issue in that area.

---

## Note on the fix rate

Three defects across ~270 tests is a low yield, and the reason is visible in the
architecture rather than flattering to the tests: the application was built
alongside the suite, in Decimal end to end, with bounds defined once in
`app/config.py` and consumed by both the validator and the boundary data. The
classes of bug this suite is designed to catch — float drift in money, an
off-by-one at a bound, a double-charge on retry, an ownership check missing on
a read path — were largely designed out before they could be written in.

The three that got through share a blind spot, and it is a consistent one: the
*happy path and the rejection decision* were tested by construction, while
everything one layer outside the business logic was not. BUG-001 is validation
working correctly and failing to say so. BUG-002 is correct data handling
undermined by a missing header. BUG-003 is a feature whose visible half worked
and whose useful half did nothing.

All three were invisible to the endpoint tests, and all three were found by
looking at what the application *emits* rather than what it returns — the error
body, the response headers, the log stream. That is the gap worth naming.

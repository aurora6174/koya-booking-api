# Clinic booking API

A booking API plus a small web page for a clinic: patients see open appointment slots, book one, and get a confirmation. Built for Task 1 of the Koya Cohort 4 assessment against the [spec](https://koyaacademy.com/assessment-api). **All five levels are implemented**, and there's an end-to-end test for each one.

- **API base URL:** `https://<your-app>.vercel.app`
- **Web page:** `https://<your-app>.vercel.app/`
- **Stack:** Python 3.12 · FastAPI · Postgres (Neon) · Vercel serverless

## How each level is enforced

The main design rule: **any guarantee that has to hold across requests is enforced by Postgres, not by Python.** On serverless hosting every request can run in a different process, so in-memory locks, counters or caches would silently stop working.

| Level | Requirement | How |
|---|---|---|
| 1 | List slots, book, look up | Plain SQL. Slots keep the exact `start`/`end` strings you sent (returned unchanged) plus parsed `timestamptz` copies for sorting and the "already started" check. |
| 2 | API key, validation | A middleware checks `X-API-Key` with `hmac.compare_digest` (constant time) before any route runs. Bodies are validated by hand rather than with Pydantic, so every error is exactly `{"error": "..."}` with the status the spec asks for (FastAPI's default would be a 422 with a different shape). |
| 3 | No double booking under concurrency | A **partial unique index**: `UNIQUE (slot_id) WHERE status = 'confirmed'`. The API doesn't check and then insert (a check-then-act race). It just inserts, and the database rejects the loser, which becomes a 409. Cancelling is one conditional `UPDATE … WHERE status = 'confirmed'`, so two simultaneous cancels can't both succeed, and the cancelled row no longer blocks the slot. |
| 4 | Idempotency, pagination | The key is claimed with `INSERT … ON CONFLICT DO NOTHING` in the same transaction as the booking. A concurrent retry with the same key **blocks on that row until the first request commits**, then replays the saved status code and body. The body is fingerprinted with SHA-256; a different body under the same key gives 422. Pagination uses an opaque cursor over an identity column (keyset pagination), so pages stay stable when new bookings arrive, unlike `OFFSET`. |
| 5 | 5 attempts / email / minute | Each attempt is a row in `booking_attempts`. A transaction-scoped **advisory lock on the email** serialises that email's requests, so 10 simultaneous requests can't all read "4 so far". Different emails never wait on each other. The attempt is recorded *before* validation, so failed attempts count. `Retry-After` is when the oldest attempt in the window expires. |

### Smaller decisions worth knowing

- **Only respond after commit.** The response is built inside the transaction but sent after it commits, so a client can never see a booking ID that a crash then loses.
- **429s aren't stored as idempotent responses**, and the rejected attempt isn't counted. That way a rate-limited request with an Idempotency-Key can simply be retried later.
- **Idempotent replays don't count against the rate limit.** They're answered before the limiter runs, so a client retrying a flaky request isn't punished.
- **`/reset` runs in one transaction** and also clears idempotency keys and rate-limit history, so a test run starts from a clean state.
- **Email matching:** emails are trimmed and stored lower-cased, so `?email=` lookups ignore case.

### Known limitations / what I'd do next

- The rate limit is per email, as specified. A request with no email isn't limited (it's rejected with a 400 anyway). In production I'd add a per-IP limit at the edge too.
- Idempotency keys never expire. In production they'd have a TTL (e.g. 24 h) and be scoped per API key or client.
- One DB connection per request (through Neon's pooler). Simple and correct for serverless, but a long-running server would use a connection pool.
- The web page asks for the API key and keeps it in `localStorage` rather than baking a secret into public JavaScript. A real patient-facing page would call a small backend that holds the key server-side.

## The web page

`GET /` serves a single HTML file (`web/index.html`), vanilla JS with no build step:

- Slots grouped by day with a provider filter (uses `?provider=`). Booked and past slots are shown but disabled.
- Book, then see a confirmation with the booking reference; cancel from there.
- "My bookings" pages through `GET /bookings?email=` with the cursor ("Load more").
- Each booking submit sends an `Idempotency-Key`. The same key is reused only when retrying the identical request after a network error, so a flaky connection can't double-book.
- Handles 409 ("someone just booked this"), 429 (countdown from `Retry-After`) and 401.
- User text is HTML-escaped before rendering. Works on mobile and in dark mode.

## Run it locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt uvicorn
export DATABASE_URL=postgresql://...  API_KEY=dev-key
python scripts/migrate.py               # creates tables (idempotent)
uvicorn app.main:app --reload --port 8000
```

## Tests

`tests/check_api.py` is an end-to-end check of all five levels against any running deployment. It needs only the standard library, and it fires genuinely concurrent requests (released together by a `threading.Barrier`) for the race-condition levels:

```bash
BASE_URL=http://localhost:8000 API_KEY=dev-key python tests/check_api.py
```

It covers, among other things: 20 simultaneous bookings of one slot (exactly 1×201 and 19×409), 10 simultaneous cancels (exactly 1×200), 8 simultaneous retries with one Idempotency-Key (one booking), and 10 simultaneous attempts from one email (exactly 5 allowed). **It calls `/reset`**, then re-seeds demo slots at the end.

## Layout

```
app/main.py         all endpoints, grouped by level
app/db.py           per-request Postgres connection
schema.sql          tables + the indexes that enforce levels 3-5
api/index.py        Vercel entry point (vercel.json rewrites every path here)
web/index.html      the booking page
tests/check_api.py  end-to-end checks
scripts/migrate.py  applies schema.sql
```

"""Clinic booking API - Koya Cohort 4, Task 1.

Every guarantee that has to hold across many requests (no double booking,
idempotency, rate limiting) is enforced by Postgres, not by Python memory,
because on serverless hosting each request may run in a different process.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg import errors
from psycopg.types.json import Jsonb
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import connect

RATE_LIMIT = 5             # booking attempts...
RATE_WINDOW_SECONDS = 60   # ...per email per this many seconds
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WEB_PAGE = Path(__file__).resolve().parent.parent / "web" / "index.html"
PUBLIC_PATHS = {"/", "/favicon.ico"}  # the web page itself; it sends the key on its API calls

app = FastAPI(title="Clinic booking API", docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- errors

class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None):
        self.status, self.message, self.headers = status, message, headers


def error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    """Every error in this API has the shape {"error": "<message>"}."""
    return JSONResponse({"error": message}, status_code=status, headers=headers)


@app.exception_handler(ApiError)
async def handle_api_error(_: Request, exc: ApiError):
    return error(exc.status, exc.message, exc.headers)


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(_: Request, exc: StarletteHTTPException):
    defaults = {404: "not found", 405: "method not allowed"}
    message = exc.detail if isinstance(exc.detail, str) else defaults.get(exc.status_code, "error")
    if exc.status_code in defaults and message in ("Not Found", "Method Not Allowed"):
        message = defaults[exc.status_code]
    return error(exc.status_code, message)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError):
    return error(400, "invalid request")


@app.exception_handler(Exception)
async def handle_unexpected(_: Request, exc: Exception):
    return error(500, "internal server error")


# ---------------------------------------------------------------- level 2: API key

@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    expected = os.environ.get("API_KEY", "").strip()  # tolerate a stray space/newline in the env var
    given = request.headers.get("x-api-key", "").strip()
    # compare_digest takes the same time whether the first or last character differs
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        return error(401, "missing or invalid API key")
    return await call_next(request)


# Added after the auth middleware, so it wraps it: browser preflights get answered,
# and even 401s carry CORS headers so a browser can read the error.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["X-API-Key", "Idempotency-Key", "Content-Type"],
    expose_headers=["Retry-After", "Idempotent-Replayed"],
)


# ---------------------------------------------------------------- helpers

async def read_json(request: Request) -> tuple[object, bytes]:
    raw = await request.body()
    try:
        return json.loads(raw), raw
    except ValueError:
        return None, raw


def parse_datetime(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def booking_json(row: dict) -> dict:
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "created_at": iso(row["created_at"]),
    }


def clean(value) -> str:
    """Trimmed string, or "" for anything that isn't a string."""
    return value.strip() if isinstance(value, str) else ""


def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{seq}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        version, seq = base64.urlsafe_b64decode(padded).decode().split(":")
        if version == "v1" and seq.isdigit():
            return int(seq)
    except (ValueError, UnicodeDecodeError):
        pass
    raise ApiError(400, "invalid cursor")


# ---------------------------------------------------------------- web page

@app.get("/", include_in_schema=False)
async def web_page():
    return HTMLResponse(WEB_PAGE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- level 1: reset + slots

@app.post("/reset")
async def reset(request: Request):
    body, _ = await read_json(request)
    slots = body.get("slots") if isinstance(body, dict) else None
    if not isinstance(slots, list):
        raise ApiError(400, "body must be {\"slots\": [...]}")

    rows, seen = [], set()
    for i, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise ApiError(400, f"slots[{i}] must be an object")
        sid, provider, start, end = (slot.get(k) for k in ("id", "provider", "start", "end"))
        if not all(isinstance(v, str) and v.strip() for v in (sid, provider, start, end)):
            raise ApiError(400, f"slots[{i}] needs id, provider, start and end")
        start_at, end_at = parse_datetime(start), parse_datetime(end)
        if not start_at or not end_at:
            raise ApiError(400, f"slots[{i}] has an invalid start or end")
        if sid in seen:
            raise ApiError(400, f"duplicate slot id {sid!r}")
        seen.add(sid)
        rows.append((sid, provider, start, end, start_at, end_at))

    async with await connect() as conn:
        # One transaction: nobody ever sees a half-reset database.
        await conn.execute("TRUNCATE bookings, slots, idempotency_keys, booking_attempts")
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO slots (id, provider, start_raw, end_raw, start_at, end_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                rows,
            )
    return {"status": "ok", "slots": len(rows)}


@app.get("/slots")
async def list_slots(provider: str | None = None):
    async with await connect() as conn:
        cur = await conn.execute(
            """
            SELECT s.id, s.provider, s.start_raw, s.end_raw,
                   NOT EXISTS (SELECT 1 FROM bookings b
                               WHERE b.slot_id = s.id AND b.status = 'confirmed') AS available
            FROM slots s
            WHERE %(provider)s::text IS NULL OR s.provider = %(provider)s
            ORDER BY s.start_at, s.id
            """,
            {"provider": provider},
        )
        rows = await cur.fetchall()
    return {
        "slots": [
            {"id": r["id"], "provider": r["provider"], "start": r["start_raw"],
             "end": r["end_raw"], "available": r["available"]}
            for r in rows
        ]
    }


# ---------------------------------------------------------------- levels 1-5: create a booking

@app.post("/bookings")
async def create_booking(request: Request):
    body, raw = await read_json(request)
    idem_key = request.headers.get("idempotency-key", "").strip()
    if len(idem_key) > 255:
        return error(400, "Idempotency-Key is too long")
    # Fingerprint the body so a reused key with a *different* body can be caught (level 4).
    canonical = json.dumps(body, sort_keys=True).encode() if body is not None else raw
    fingerprint = hashlib.sha256(canonical).hexdigest()

    async with await connect() as conn:
        if idem_key:
            # Claim the key. If another request holds it (even one still running),
            # Postgres makes this INSERT wait until that request commits.
            cur = await conn.execute(
                "INSERT INTO idempotency_keys (key, fingerprint) VALUES (%s, %s)"
                " ON CONFLICT (key) DO NOTHING RETURNING key",
                (idem_key, fingerprint),
            )
            if await cur.fetchone() is None:
                cur = await conn.execute(
                    "SELECT fingerprint, status_code, response FROM idempotency_keys WHERE key = %s",
                    (idem_key,),
                )
                saved = await cur.fetchone()
                if saved["fingerprint"] != fingerprint:
                    return error(422, "Idempotency-Key was already used with a different request body")
                return JSONResponse(saved["response"], status_code=saved["status_code"],
                                    headers={"Idempotent-Replayed": "true"})

        status, payload, headers = await book_slot(conn, body)

        if status == 429:
            # Throw away the key claim: a rate-limited request should be retryable later.
            await conn.rollback()
        elif idem_key:
            await conn.execute(
                "UPDATE idempotency_keys SET status_code = %s, response = %s WHERE key = %s",
                (status, Jsonb(payload), idem_key),
            )
    # Leaving the `async with` commits, so we only answer once the booking is durable.
    return JSONResponse(payload, status_code=status, headers=headers)


async def book_slot(conn, body) -> tuple[int, dict, dict | None]:
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object"}, None

    slot_id, name = clean(body.get("slot_id")), clean(body.get("name"))
    email = clean(body.get("email")).lower()

    # Level 5: count the attempt *before* validating, so failed attempts count too.
    if email:
        # Serialise requests for the same email so 10 simultaneous requests can't
        # all read "4 attempts so far". Different emails don't block each other.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (email,))
        cur = await conn.execute(
            """
            SELECT count(*) AS attempts,
                   GREATEST(1, ceil(extract(epoch FROM
                       min(attempted_at) + make_interval(secs => %(window)s) - clock_timestamp())))::int
                       AS retry_after
            FROM booking_attempts
            WHERE email = %(email)s
              AND attempted_at > clock_timestamp() - make_interval(secs => %(window)s)
            """,
            {"email": email, "window": RATE_WINDOW_SECONDS},
        )
        window = await cur.fetchone()
        if window["attempts"] >= RATE_LIMIT:
            seconds = window["retry_after"]
            return 429, {"error": f"too many booking attempts for this email, retry in {seconds}s"}, \
                {"Retry-After": str(seconds)}
        await conn.execute("INSERT INTO booking_attempts (email) VALUES (%s)", (email,))
        await conn.execute(  # housekeeping: forget attempts older than an hour
            "DELETE FROM booking_attempts WHERE email = %s AND attempted_at < clock_timestamp() - interval '1 hour'",
            (email,),
        )

    # Level 2: validation.
    missing = [field for field, value in (("slot_id", slot_id), ("name", name), ("email", email)) if not value]
    if missing:
        return 400, {"error": f"{', '.join(missing)} is required"}, None
    if len(email) > 254 or not EMAIL_RE.match(email):
        return 400, {"error": "email is not valid"}, None
    if len(name) > 200:
        return 400, {"error": "name is too long"}, None

    cur = await conn.execute("SELECT start_at FROM slots WHERE id = %s", (slot_id,))
    slot = await cur.fetchone()
    if slot is None:
        return 404, {"error": "slot not found"}, None
    if slot["start_at"] <= datetime.now(timezone.utc):
        return 400, {"error": "slot has already started"}, None

    # Level 3: just try the insert. The partial unique index rejects a second
    # confirmed booking for this slot, however many requests arrive at once.
    try:
        async with conn.transaction():  # savepoint: a 409 keeps the attempt row above
            cur = await conn.execute(
                "INSERT INTO bookings (id, slot_id, name, email) VALUES (%s, %s, %s, %s) RETURNING *",
                (str(uuid.uuid4()), slot_id, name, email),
            )
            booking = await cur.fetchone()
    except errors.UniqueViolation:
        return 409, {"error": "slot is already booked"}, None
    return 201, booking_json(booking), None


# ---------------------------------------------------------------- level 4: list a person's bookings

@app.get("/bookings")
async def list_bookings(request: Request):
    params = request.query_params
    email = params.get("email", "").strip().lower()
    if not email:
        raise ApiError(400, "email is required")

    raw_limit = params.get("limit")
    if raw_limit is None:
        limit = DEFAULT_PAGE_SIZE
    else:
        if not raw_limit.strip().isdigit() or not 1 <= int(raw_limit) <= MAX_PAGE_SIZE:
            raise ApiError(400, f"limit must be an integer from 1 to {MAX_PAGE_SIZE}")
        limit = int(raw_limit)

    cursor = params.get("cursor")
    after = decode_cursor(cursor) if cursor else 0

    async with await connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM bookings WHERE email = %s AND seq > %s ORDER BY seq LIMIT %s",
            (email, after, limit + 1),  # one extra row tells us whether another page exists
        )
        rows = await cur.fetchall()

    page = rows[:limit]
    next_cursor = encode_cursor(page[-1]["seq"]) if len(rows) > limit else None
    return {"bookings": [booking_json(r) for r in page], "next_cursor": next_cursor}


# ---------------------------------------------------------------- level 1 + 3: get / cancel one booking

@app.get("/bookings/{booking_id}")
async def get_booking(booking_id: str):
    async with await connect() as conn:
        cur = await conn.execute("SELECT * FROM bookings WHERE id = %s", (booking_id,))
        row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "booking not found")
    return booking_json(row)


@app.delete("/bookings/{booking_id}")
async def cancel_booking(booking_id: str):
    async with await connect() as conn:
        # A single conditional UPDATE: two simultaneous cancels can't both succeed.
        cur = await conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = %s AND status = 'confirmed' RETURNING *",
            (booking_id,),
        )
        row = await cur.fetchone()
        if row is None:
            cur = await conn.execute("SELECT 1 FROM bookings WHERE id = %s", (booking_id,))
            exists = await cur.fetchone()
            raise ApiError(409 if exists else 404, "booking is already cancelled" if exists else "booking not found")
    return booking_json(row)

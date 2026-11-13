#!/usr/bin/env python3
"""End-to-end checks for all five levels, run against a live API.

    BASE_URL=https://your-app.vercel.app API_KEY=... python tests/check_api.py
    BASE_URL=... API_KEY=... python tests/check_api.py --seed-only   # just load demo slots

WARNING: this calls POST /reset, which wipes every booking. It re-seeds demo
slots at the end. Do not run it against the deployment once it's submitted.
Standard library only - no install needed.
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
results: list[tuple[bool, str]] = []


def call(method, path, body=None, headers=None, key=API_KEY, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    if key is not None:
        h["X-API-Key"] = key
    req = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, text, resp_headers = r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        status, text, resp_headers = e.code, e.read().decode(), e.headers
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        payload = text
    return status, payload, resp_headers


def check(ok, label, detail=""):
    results.append((bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   <- {detail}" if not ok and detail else ""))


def is_error(payload):
    return isinstance(payload, dict) and set(payload) == {"error"} and isinstance(payload["error"], str)


def at_once(n, fn):
    """Run fn(i) n times, released at the same instant by a barrier."""
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(run, range(n)))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_slots():
    base = (datetime.now(timezone.utc) + timedelta(days=30)).replace(hour=9, minute=0, second=0, microsecond=0)
    slots = [{"id": f"t{i}", "provider": "Dr Ade" if i % 2 else "Dr Bello",
              "start": iso(base + timedelta(minutes=30 * i)),
              "end": iso(base + timedelta(minutes=30 * i + 30))} for i in range(40)]
    slots.reverse()  # send them out of order to prove the API sorts
    past = datetime.now(timezone.utc) - timedelta(days=1)
    slots.append({"id": "past", "provider": "Dr Ade", "start": iso(past), "end": iso(past + timedelta(minutes=30))})
    return slots


def book(slot_id, email, name="Test Person", headers=None):
    return call("POST", "/bookings", {"slot_id": slot_id, "name": name, "email": email}, headers)


def level1():
    print("\nLevel 1 - list slots, book one, look it up")
    status, _, _ = call("POST", "/reset", {"slots": test_slots()})
    check(status == 200, "POST /reset -> 200", status)

    status, body, _ = call("GET", "/slots")
    slots = body.get("slots", []) if isinstance(body, dict) else []
    check(status == 200 and len(slots) == 41, "GET /slots -> 200 with every slot", f"{status} {len(slots)}")
    check([s["id"] for s in slots][:2] == ["past", "t0"], "slots are sorted earliest start first")
    check(all({"id", "provider", "start", "end", "available"} <= set(s) for s in slots), "each slot has all fields")
    t0 = next(s for s in slots if s["id"] == "t0")
    check(t0["start"] == next(s["start"] for s in test_slots() if s["id"] == "t0"), "start is returned exactly as sent")

    status, body, _ = call("GET", "/slots?provider=Dr%20Ade")
    check(status == 200 and body["slots"] and all(s["provider"] == "Dr Ade" for s in body["slots"]),
          "GET /slots?provider= filters by exact provider")

    status, booking, _ = book("t0", "ada@example.com", "Ada Obi")
    check(status == 201, "POST /bookings -> 201", f"{status} {booking}")
    check(isinstance(booking, dict) and booking.get("status") == "confirmed"
          and {"id", "slot_id", "name", "email", "status", "created_at"} <= set(booking), "booking has all fields")

    status, fetched, _ = call("GET", f"/bookings/{booking['id']}")
    check(status == 200 and fetched == booking, "GET /bookings/{id} returns the same booking")
    _, body, _ = call("GET", "/slots")
    check(next(s for s in body["slots"] if s["id"] == "t0")["available"] is False, "booked slot shows available=false")

    status, body, _ = call("GET", "/bookings/does-not-exist")
    check(status == 404 and is_error(body), "unknown booking -> 404 {error}")


def level2():
    print("\nLevel 2 - API key and validation")
    for path in ("/slots", "/bookings/x"):
        status, body, _ = call("GET", path, key=None)
        check(status == 401 and is_error(body), f"no key on {path} -> 401")
        status, _, _ = call("GET", path, key="wrong")
        check(status == 401, f"wrong key on {path} -> 401")
    status, _, _ = call("POST", "/bookings", {"slot_id": "t1", "name": "A", "email": "a@b.co"}, key=None)
    check(status == 401, "no key on POST /bookings -> 401")

    cases = [
        ({"name": "A", "email": "v1@example.com"}, 400, "missing slot_id"),
        ({"slot_id": "t1", "email": "v2@example.com"}, 400, "missing name"),
        ({"slot_id": "t1", "name": "A"}, 400, "missing email"),
        ({"slot_id": "t1", "name": "   ", "email": "v3@example.com"}, 400, "blank name"),
        ({"slot_id": "t1", "name": "A", "email": ""}, 400, "empty email"),
        ({"slot_id": "t1", "name": "A", "email": "not-an-email"}, 400, "invalid email"),
        ({"slot_id": "t1", "name": "A", "email": "a@b"}, 400, "email without a domain"),
        ({"slot_id": "nope", "name": "A", "email": "v4@example.com"}, 404, "unknown slot"),
        ({"slot_id": "past", "name": "A", "email": "v5@example.com"}, 400, "slot already started"),
    ]
    for payload, expected, label in cases:
        status, body, _ = call("POST", "/bookings", payload)
        check(status == expected and is_error(body), f"{label} -> {expected}", f"{status} {body}")
    status, body, _ = call("POST", "/bookings", raw=b"{not json")
    check(status == 400 and is_error(body), "malformed JSON -> 400")

    status, body, _ = book("t1", "  MiXeD@Example.COM  ", "  Ada  Lovelace  ")
    check(status == 201 and body["email"] == "mixed@example.com" and body["name"] == "Ada  Lovelace",
          "name/email trimmed, email lower-cased", body)


def level3():
    print("\nLevel 3 - no double booking, cancellation")
    statuses = Counter(s for s, _, _ in at_once(20, lambda i: book("t2", f"race{i}@example.com")))
    check(statuses == Counter({201: 1, 409: 19}), "20 simultaneous bookings of one slot -> exactly 1x201 + 19x409",
          dict(statuses))

    _, slots, _ = call("GET", "/slots")
    check(next(s for s in slots["slots"] if s["id"] == "t2")["available"] is False, "raced slot is unavailable")

    status, booking, _ = book("t3", "cancel@example.com")
    status, body, _ = call("DELETE", f"/bookings/{booking['id']}")
    check(status == 200 and body["status"] == "cancelled" and body["id"] == booking["id"], "DELETE -> 200 cancelled")
    status, body, _ = call("DELETE", f"/bookings/{booking['id']}")
    check(status == 409 and is_error(body), "cancel twice -> 409")
    status, _, _ = call("DELETE", "/bookings/does-not-exist")
    check(status == 404, "cancel unknown -> 404")
    _, slots, _ = call("GET", "/slots")
    check(next(s for s in slots["slots"] if s["id"] == "t3")["available"] is True, "cancelling frees the slot")
    status, _, _ = book("t3", "rebook@example.com")
    check(status == 201, "freed slot can be booked again")

    status, booking, _ = book("t4", "cancelrace@example.com")
    statuses = Counter(s for s, _, _ in at_once(10, lambda i: call("DELETE", f"/bookings/{booking['id']}")))
    check(statuses == Counter({200: 1, 409: 9}), "10 simultaneous cancels -> exactly 1x200", dict(statuses))


def level4():
    print("\nLevel 4 - idempotency and pagination")
    key = str(uuid.uuid4())
    payload = {"slot_id": "t5", "name": "Idem", "email": "idem@example.com"}
    s1, b1, _ = call("POST", "/bookings", payload, {"Idempotency-Key": key})
    s2, b2, _ = call("POST", "/bookings", payload, {"Idempotency-Key": key})
    check(s1 == s2 == 201 and b1["id"] == b2["id"], "same key + same body -> same status and booking id", f"{s1} {s2}")
    s3, b3, _ = call("POST", "/bookings", {**payload, "slot_id": "t6"}, {"Idempotency-Key": key})
    check(s3 == 422 and is_error(b3), "same key + different body -> 422", s3)

    key = str(uuid.uuid4())
    payload = {"slot_id": "t7", "name": "Retry", "email": "retry@example.com"}
    responses = at_once(8, lambda i: call("POST", "/bookings", payload, {"Idempotency-Key": key}))
    ids = {b.get("id") for _, b, _ in responses}
    check(all(s == 201 for s, _, _ in responses) and len(ids) == 1,
          "8 simultaneous retries with one key -> all 201, one booking id", [s for s, _, _ in responses])
    _, page, _ = call("GET", "/bookings?email=retry@example.com")
    check(len(page["bookings"]) == 1, "...and only one booking was created")

    created = [book(f"t{8 + i}", "pager@example.com")[1]["id"] for i in range(4)]
    seen, cursor, pages = [], None, 0
    while True:
        path = "/bookings?email=PAGER@example.com&limit=3" + (f"&cursor={cursor}" if cursor else "")
        status, page, _ = call("GET", path)
        if status != 200:
            break
        pages += 1
        seen += [b["id"] for b in page["bookings"]]
        cursor = page["next_cursor"]
        if cursor is None or pages > 5:
            break
    check(seen == created and pages == 2, "4 bookings, limit=3 -> 2 pages, oldest first, case-insensitive email",
          f"pages={pages}")
    check(page["next_cursor"] is None, "last page has next_cursor: null")
    status, page, _ = call("GET", "/bookings?email=pager@example.com")
    check(status == 200 and len(page["bookings"]) == 4 and page["next_cursor"] is None, "default limit works")
    for bad in ("0", "51", "abc", "-1", "2.5"):
        status, body, _ = call("GET", f"/bookings?email=pager@example.com&limit={bad}")
        check(status == 400 and is_error(body), f"limit={bad} -> 400")
    status, _, _ = call("GET", "/bookings?email=pager@example.com&cursor=garbage!!")
    check(status == 400, "invalid cursor -> 400")


def level5():
    print("\nLevel 5 - rate limiting")
    statuses = [book("nope", "spammer@example.com")[0] for _ in range(5)]
    check(statuses == [404] * 5, "5 failed attempts are allowed (404s)", statuses)
    status, body, headers = book("t20", "spammer@example.com")
    retry = headers.get("Retry-After")
    check(status == 429 and is_error(body), "6th attempt -> 429", status)
    check(retry is not None and retry.isdigit() and 1 <= int(retry) <= 60, "Retry-After is whole seconds", retry)
    status, _, _ = book("t20", "innocent@example.com")
    check(status == 201, "other emails are not affected")

    statuses = Counter(s for s, _, _ in at_once(10, lambda i: book(f"t{21 + i}", "burst@example.com")))
    check(statuses[429] == 5 and statuses[201] == 5, "10 simultaneous attempts -> exactly 5 allowed", dict(statuses))


def seed_demo():
    """Friendly slots for the web page: next 4 weeks, three providers."""
    providers = ["Dr Ade", "Dr Bello", "Dr Okafor"]
    today = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    slots = []
    for day in range(1, 29):
        date = today + timedelta(days=day)
        for p, provider in enumerate(providers):
            for h in (8, 9, 10, 13, 14):
                start = date.replace(hour=h, minute=30 * (p % 2))
                slots.append({"id": f"{start:%m%d}-{provider.split()[1].lower()}-{start:%H%M}",
                              "provider": provider, "start": iso(start), "end": iso(start + timedelta(minutes=30))})
    status, body, _ = call("POST", "/reset", {"slots": slots})
    print(f"\nSeeded {len(slots)} demo slots -> {status} {body}")


if __name__ == "__main__":
    if not API_KEY:
        sys.exit("Set API_KEY (and BASE_URL) first.")
    print(f"Checking {BASE_URL}")
    if "--seed-only" not in sys.argv:
        for level in (level1, level2, level3, level4, level5):
            try:
                level()
            except Exception as exc:  # keep going so one crash doesn't hide other results
                check(False, f"{level.__name__} crashed", repr(exc))
        passed = sum(ok for ok, _ in results)
        print(f"\n{passed}/{len(results)} checks passed")
    seed_demo()
    sys.exit(0 if all(ok for ok, _ in results) else 1)

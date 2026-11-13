-- Clinic booking API schema. Safe to run more than once.

CREATE TABLE IF NOT EXISTS slots (
    id        text PRIMARY KEY,
    provider  text NOT NULL,
    start_raw text NOT NULL,          -- exactly what the client sent, returned unchanged
    end_raw   text NOT NULL,
    start_at  timestamptz NOT NULL,   -- parsed, used for sorting and "already started"
    end_at    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS slots_start_idx ON slots (start_at, id);

CREATE TABLE IF NOT EXISTS bookings (
    seq        bigint GENERATED ALWAYS AS IDENTITY UNIQUE,  -- insertion order, used as the pagination cursor
    id         text PRIMARY KEY,
    slot_id    text NOT NULL REFERENCES slots (id) ON DELETE CASCADE,
    name       text NOT NULL,
    email      text NOT NULL,                               -- stored lower-cased
    status     text NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Level 3: the database itself refuses a second *confirmed* booking for the same slot.
-- Cancelled bookings don't count, so cancelling frees the slot.
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_booking_per_slot
    ON bookings (slot_id) WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS bookings_email_seq_idx ON bookings (email, seq);

-- Level 4: one row per Idempotency-Key, holding the response we gave the first time.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         text PRIMARY KEY,
    fingerprint text NOT NULL,        -- sha256 of the request body
    status_code int,
    response    jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Level 5: one row per booking attempt, for the per-email sliding window.
CREATE TABLE IF NOT EXISTS booking_attempts (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email        text NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS booking_attempts_email_time_idx ON booking_attempts (email, attempted_at);

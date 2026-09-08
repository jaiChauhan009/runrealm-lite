-- ============================================================
--  RunRealm Lite — schema (PostGIS)
--  Applied automatically by docker-compose (initdb), or:
--    psql "$DATABASE_URL" -f sql/schema.sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ── users ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,               -- stored lower-cased by the app
    password_hash TEXT NOT NULL,
    name          TEXT NOT NULL,
    avatar_url    TEXT NOT NULL,                      -- initials SVG data-URI at register; real URL on upload
    contact       TEXT,                              -- optional
    visibility    TEXT NOT NULL DEFAULT 'private'
                  CHECK (visibility IN ('public','private')),
    timezone      TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── connections (lets a private user grant profile/contact access) ──
CREATE TABLE IF NOT EXISTS connections (
    requester_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    addressee_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','accepted','blocked')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (requester_id, addressee_id)
);
CREATE INDEX IF NOT EXISTS connections_addressee_idx ON connections (addressee_id, status);

-- ── run_sessions ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS run_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    duration_s  INTEGER,
    distance_m  DOUBLE PRECISION NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','completed','discarded')),
    synced      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_sessions_user_idx ON run_sessions (user_id, started_at DESC);

-- ── route_points (high volume -> bigserial pk) ───────────────
CREATE TABLE IF NOT EXISTS route_points (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES run_sessions(id) ON DELETE CASCADE,
    lat         DOUBLE PRECISION NOT NULL,
    lng         DOUBLE PRECISION NOT NULL,
    speed_kmh   REAL,
    bearing     REAL,
    seq         INTEGER NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    UNIQUE (session_id, seq)
);

-- ── territories (PostGIS polygons) ──────────────────────────
CREATE TABLE IF NOT EXISTS territories (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id UUID REFERENCES run_sessions(id) ON DELETE SET NULL,
    geom       geometry(Polygon, 4326) NOT NULL,
    area_m2    DOUBLE PRECISION NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'active'
               CHECK (status IN ('active','superseded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- partial GiST: the map only ever queries active polygons
CREATE INDEX IF NOT EXISTS territories_geom_active_gix
    ON territories USING gist (geom) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS territories_owner_idx
    ON territories (owner_id, status);

-- ── todos (due date = the day it was created, in the user's tz) ──
CREATE TABLE IF NOT EXISTS todos (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    description TEXT,
    rating      SMALLINT CHECK (rating BETWEEN 1 AND 5),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','complete','failed')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS todos_user_created_idx ON todos (user_id, created_at);

-- ── stats rollups ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_stats (
    user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day       DATE NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    total     INTEGER NOT NULL DEFAULT 0,
    pct       NUMERIC(5,1) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS weekly_stats (
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    week_start DATE NOT NULL,          -- Monday
    pct        NUMERIC(5,1) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, week_start)
);
CREATE TABLE IF NOT EXISTS monthly_stats (
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month_start DATE NOT NULL,         -- first of month
    pct         NUMERIC(5,1) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, month_start)
);

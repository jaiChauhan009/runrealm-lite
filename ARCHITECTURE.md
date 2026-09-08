# RunRealm Lite — Architecture

## 1. Goal & scope

RunRealm Lite is a deliberately small, fast backend for a RunRealm-style
territory game. It does exactly six things:

1. **Auth** — email + password, JWT bearer tokens.
2. **Users** — one profile per user, `public` or `private`, with peer-to-peer
   **connections** that unlock a private user's profile page and contact field.
3. **Run sessions** — start a run, stream **batched GPS points**, finish it.
4. **Territories** — on finish, the run's loop becomes a **PostGIS polygon** on a
   shared map; overlapping rival polygons are superseded (last claim wins).
5. **Map serving** — the map is served as **cached vector tiles**, so tens of
   thousands of concurrent viewers cost the database almost nothing.
6. **Todos + stats** — daily checklist items with a derived due date, rolled up
   into **daily / weekly / monthly completion %**.

It replaces an older backend (`../runrealm-fastapi`) that was built on the
Supabase Python SDK with ~17 tables (teams, leagues, XP, levels, achievements,
notifications, activity feed, sync queue, corridor capture…). Lite keeps the
game loop and the anti-cheat logic and drops everything else. It is a single
FastAPI process, one Postgres database, one Redis, and a nightly scheduler.

---

## 2. Stack & why

| Piece | Choice | One-line rationale |
|---|---|---|
| Web framework | **FastAPI** (async) | Async I/O glue with free OpenAPI docs; the process is never CPU-bound. |
| Database | **PostgreSQL 15 + PostGIS** | Real spatial types, indexes, and `ST_AsMVT` — the DB does the geometry, not Python. |
| DB driver | **raw asyncpg**, no ORM | Hand-written SQL, one round-trip per call, prepared statements, no ORM overhead or lazy-load surprises. |
| Cache | **Redis 7** | Per-tile vector-tile cache and short-lived read caches; collapses N identical map requests into one DB query. |
| Scheduler | **APScheduler** | In-process cron for the nightly todo auto-fail + stats rollup; no extra service for local dev. |
| Auth | **JWT (PyJWT, HS256) + bcrypt** | Stateless tokens, no session store; bcrypt for password hashing. |
| Serialisation | **Pydantic v2** | Request validation + camelCase aliasing for a JS/Kotlin client. |

Runtime: Python 3.11, `uvicorn` with 2 workers. See `requirements.txt` — ten
packages, no Shapely, no GeoAlchemy, no Supabase.

---

## 3. Data model

Full schema in [`sql/schema.sql`](sql/schema.sql). Extensions: `postgis`,
`pgcrypto` (for `gen_random_uuid()`).

### `users`

| Column | Type | Purpose |
|---|---|---|
| `id` | `uuid` PK, `gen_random_uuid()` | User identity. |
| `email` | `text` unique | Login; stored lower-cased by the app. |
| `password_hash` | `text` | bcrypt hash. |
| `name` | `text` | Display name; also shown on the map. |
| `avatar_url` | `text` | Initials-SVG data-URI at register (`app/avatar.py`); a real URL after photo upload. |
| `contact` | `text` null | Optional phone/handle; hidden unless public or connected. |
| `visibility` | `text` `public`/`private`, default **`private`** | Gates the profile page + `contact`. |
| `timezone` | `text`, default `Asia/Kolkata` | IANA tz; the reference clock for todo due dates and stat buckets. |
| `created_at` | `timestamptz` | Registration time. |

**One profile == one user.** The old backend split `users` (Supabase auth) from
`user_profiles` (game data) because auth was outsourced. Here auth is ours, so
there is a single row. No separate "profile" concept, no join.

**`visibility` defaults to `private`.** A private user is still a full
participant in the game: their **territories, their name, and their avatar still
render on the map for everyone**. What `private` hides is the *social* surface —
`GET /users/{id}` returns only `id`, `name`, `avatarUrl`, `visibility`, and the
`contact` field and profile detail are withheld — until the two users have an
`accepted` row in `connections`.

### `connections`

| Column | Type | Purpose |
|---|---|---|
| `requester_id` | `uuid` FK users | Who sent the request. |
| `addressee_id` | `uuid` FK users | Who received it. |
| `status` | `text` `pending`/`accepted`/`blocked`, default `pending` | Request state. |
| `created_at` | `timestamptz` | — |
| PK | `(requester_id, addressee_id)` | One request per ordered pair. |

Index `connections_addressee_idx (addressee_id, status)` — "show me my pending
requests". An `accepted` row in either direction unlocks the private profile and
contact for both users.

### `run_sessions`

| Column | Type | Purpose |
|---|---|---|
| `id` | `uuid` PK | Session identity. |
| `user_id` | `uuid` FK users | Owner. |
| `started_at` | `timestamptz` | Client-supplied start. |
| `ended_at` | `timestamptz` null | Set on finish. |
| `duration_s` | `integer` null | Set on finish. |
| `distance_m` | `double precision`, default 0 | Client-reported distance, stored on finish. |
| `status` | `text` `active`/`completed`/`discarded`, default `active` | Lifecycle. |
| `synced` | `boolean`, default true | Reserved for offline clients. |
| `created_at` | `timestamptz` | — |

Index `run_sessions_user_idx (user_id, started_at DESC)` — session history list.

### `route_points`

| Column | Type | Purpose |
|---|---|---|
| `id` | **`bigserial`** PK | High-volume monotonic key. |
| `session_id` | `uuid` FK run_sessions | Parent session. |
| `lat`, `lng` | `double precision` | GPS position. |
| `speed_kmh`, `bearing` | `real` null | Optional per-point telemetry. |
| `seq` | `integer` | Client-assigned order within the session. |
| `recorded_at` | `timestamptz` | Fix timestamp. |
| unique | `(session_id, seq)` | Idempotent batched upload — re-sending a batch is a no-op. |

**`bigserial`, not uuid.** A single run can be thousands of points and they are
never referenced by id from outside — only inserted in bulk and read back in
`seq` order. An 8-byte sequential integer keeps the index tight and the
multi-row insert cheap; a random uuid per point would bloat the table and
fragment the index for no benefit.

### `territories`

| Column | Type | Purpose |
|---|---|---|
| `id` | `uuid` PK | Territory identity. |
| `owner_id` | `uuid` FK users | Who claimed it. |
| `session_id` | `uuid` FK run_sessions, `ON DELETE SET NULL` | The run that produced it. |
| `geom` | **`geometry(Polygon, 4326)`** | The claimed area, WGS-84 lon/lat. |
| `area_m2` | `double precision` | `ST_Area(geom::geography)`, cached for sorting/stats. |
| `status` | `text` `active`/`superseded`, default `active` | Only `active` is on the map. |
| `created_at` | `timestamptz` | Claim time. |

Indexes:

```sql
-- the map only ever queries active polygons, so the spatial index only covers them
CREATE INDEX territories_geom_active_gix
    ON territories USING gist (geom) WHERE status = 'active';
CREATE INDEX territories_owner_idx ON territories (owner_id, status);
```

**`geometry(Polygon, 4326)`** is a typed, SRID-constrained column: PostGIS
rejects non-polygon or wrong-projection geometry at write time, and every
spatial function (`&&`, `ST_Intersects`, `ST_AsMVTGeom`) can use the GiST index.
The old backend stored a GeoJSON string plus four `min/max_lat/lon` float columns
and did bbox math in Python with Shapely — that is replaced entirely.

**The partial GiST index** (`WHERE status = 'active'`) matters because superseded
polygons accumulate forever (history) but are never drawn or intersected. Keeping
them out of the index makes it smaller, keeps it in cache, and makes every tile
query and every supersede scan touch only live data.

### `todos`

| Column | Type | Purpose |
|---|---|---|
| `id` | `uuid` PK | — |
| `user_id` | `uuid` FK users | Owner. |
| `title` | `text` | — |
| `description` | `text` null | — |
| `rating` | `smallint` 1–5 null | Optional self-score on completion. |
| `status` | `text` `pending`/`complete`/`failed`, default `pending` | — |
| `created_at` | `timestamptz` | **Also the due date.** |

Index `todos_user_created_idx (user_id, created_at)` — day/week/month range scans.

**There is no `due_date` column.** A todo is due on **the day it was created, in
the user's timezone**. `due_date` would just be `created_at` re-encoded, and a
stored copy would drift if the user changes timezone. Instead the nightly job (see
§5 of `app/jobs.py`) runs per user at their local midnight: every todo still
`pending` whose `created_at`, converted to `users.timezone`, is now a past local
day is flipped to `failed`. Completing a todo is only possible on its own local
day; after local midnight an untouched todo auto-fails and counts against that
day's percentage.

### Stats rollups

| Table | Key | Columns | Purpose |
|---|---|---|---|
| `daily_stats` | `(user_id, day)` | `completed`, `total`, `pct NUMERIC(5,1)` | One row per user per day. |
| `weekly_stats` | `(user_id, week_start)` | `pct` | `week_start` = Monday. |
| `monthly_stats` | `(user_id, month_start)` | `pct` | `month_start` = 1st of month. |

Written by the nightly rollup after the auto-fail pass. `pct` is
`100 * completed / total` (0 when `total = 0`). These are pure denormalised
caches — the `todos` table remains the source of truth and the rollups can be
rebuilt from it at any time.

---

## 4. The map / territory-serving strategy

This is the central design decision. A territory map is *read by everyone and
written by almost no one*: at any moment maybe 10,000 people are panning around
the map and a few dozen are finishing a run. So the read path is a cache
problem, not a database problem.

### Tier 1 — Redis-cached vector tiles

`GET /tiles/territories/{z}/{x}/{y}.mvt` (`app/routers/tiles.py`, **no auth** —
it is the base map layer). One SQL statement builds a Mapbox Vector Tile
entirely inside PostGIS:

```sql
SELECT ST_AsMVT(q, 'territories', 4096, 'geom') FROM (
    SELECT id::text, owner_id::text, area_m2,
           ST_AsMVTGeom(
             ST_Transform(ST_SimplifyPreserveTopology(geom, $4), 3857),
             ST_TileEnvelope($1,$2,$3), 4096, 64, true) AS geom
    FROM territories
    WHERE status = 'active'
      AND geom && ST_Transform(ST_TileEnvelope($1,$2,$3), 4326)
) q WHERE q.geom IS NOT NULL;
```

- `ST_TileEnvelope(z,x,y)` turns the tile coordinate into a Web-Mercator bbox.
- `geom && …` uses the **partial GiST index** to grab only the polygons in view.
- `ST_SimplifyPreserveTopology(geom, tol)` drops vertices at low zoom
  (`tol = max(0, 0.5 / 2^z * 0.01)` degrees — coarse when zoomed out, ~0 past
  ~z15) so a city-wide tile is not shipping street-level detail.
- `ST_AsMVTGeom` + `ST_AsMVT` encode the protobuf tile in the database.

The result bytes are cached in Redis at `tile:territories:{z}:{x}:{y}` with TTL
`settings.tile_cache_ttl` (default **20 s**, `TILE_CACHE_TTL`). **Empty tiles are
cached too** — most tiles of a sparse map are empty and this stops pans over
empty space from ever reaching PostGIS. The response carries
`Cache-Control: public, max-age=<ttl>` so a CDN or the browser can serve it
without touching us at all. The endpoint never returns 500 — on any error it
logs and returns an empty tile, so a DB hiccup degrades to "no polygons drawn"
rather than a broken map.

**Why this scales.** Within one TTL window, every one of the *N* users looking
at tile `14/12043/6194` gets the same bytes; only the **first** request per tile
per TTL runs a query. If 10,000 users are viewing a metro area that spans ~40
visible tiles, that is ~40 DB queries every 20 s ≈ **2 queries/sec**, regardless
of how many users. Add a CDN and the API sees a small fraction of even that.

### Tier 2 — batched writes & lazy loading

- **`POST /sessions/{id}/points`** takes an **array** of points and does one
  multi-row `INSERT`. A 30-minute run at 1 Hz is ~1800 points sent as ~18
  batches of 100, i.e. 18 inserts instead of 1800.
- History (`GET /sessions`, `GET /sessions/{id}/points`) and anything not
  currently on screen is fetched on demand, paginated, never pushed.
- `GET /territories/mine` is a cheap owner-indexed query, separate from the tile
  path, so a user's own list never competes with map rendering.

### Tier 3 — targeted invalidation (future)

Today, any territory change calls `bust_territory_tiles()` which does
`SCAN` + `DEL` on `tile:territories:*` — simple and correct, and with a 20 s TTL
the blast radius is tiny anyway. Planned:

- Compute the set of `{z}/{x}/{y}` keys a changed polygon actually covers and
  delete only those.
- Optional SSE / WebSocket channel that pushes changed tile keys to connected
  clients so they refetch immediately instead of waiting out the TTL.
- Materialise popular low-zoom tiles to object storage behind the CDN.

### "Can Python handle it?"

Yes, because Python does almost nothing on the hot path. FastAPI here is async
I/O glue: parse the URL, check Redis, on a miss `await` one PostGIS query, write
Redis, return bytes. There is no CPU-bound work in the process — the geometry is
in PostGIS, the fan-out absorption is in Redis, TLS/compression is in uvicorn/the
CDN.

Rough 10k-concurrent-map-users math:

- ~40 distinct visible tiles in a busy area, TTL 20 s → **~2 tile queries/sec** on
  the DB from the map.
- Finishes: even 1000 runs/hour is **~0.3 writes/sec**, each a handful of indexed
  statements.
- Everything else (login, todos, session history) is per-user-action traffic,
  not per-frame — thousands/min is fine on 2 uvicorn workers and a 10-connection
  asyncpg pool.

The bottleneck you would hit first is Redis network throughput or the CDN, not
Python and not Postgres.

---

## 5. Territory generation

On **`POST /sessions/{id}/finish`** (`claimTerritory` defaults to `true`):

1. **Load** the session's `route_points` ordered by `seq`; set the session
   `completed` with `ended_at`, `duration_s`, `distance_m`.
2. **Pure-Python validation** (`app/geo.py`, ported from the old
   `utils/geo_utils.py`):
   - **Loop closure** — last point within `territory_loop_close_m` (50 m) of the
     first, and the gap is a small fraction of total route length.
   - **Anti-cheat speed tiers** on each consecutive segment:
     `CLEAN` ≤ 20 km/h · `SUSPICIOUS` 20–35 km/h · `VEHICLE` > 35 km/h ·
     `TELEPORT` = > 200 m jump in < 3 s.
   - **Score** starts at 1.0, minus `0.80 × vehicle_ratio`, minus
     `0.20 × suspicious_ratio`, minus `0.10 per teleport`. Reject if
     `vehicle_ratio > 0.30` or **score < 0.40**.
   - **Minimums:** ≥ 20 points (`territory_min_points`), ≥ 200 m route
     (`territory_min_distance_m`), ≥ 500 m² polygon (`territory_min_area_m2`).
   - Failures raise `HTTPException(422, <human reason>)`.
3. **Build the polygon in PostGIS** from the ordered points — the geometry is
   never constructed in Python:

   ```sql
   WITH ring AS (
     SELECT ST_MakeValid(
       ST_MakePolygon(
         ST_AddPoint(
           ST_MakeLine(ST_MakePoint(lng, lat) ORDER BY seq),
           ST_StartPoint(ST_MakeLine(ST_MakePoint(lng, lat) ORDER BY seq))
         )
       )
     )::geometry(Polygon,4326) AS geom
   FROM route_points WHERE session_id = $1)
   INSERT INTO territories (owner_id, session_id, geom, area_m2, status)
   SELECT $2, $1, geom, ST_Area(geom::geography), 'active' FROM ring
   RETURNING id;
   ```

4. **Supersede overlaps** — last claim wins the contested ground:

   ```sql
   UPDATE territories
      SET status = 'superseded'
    WHERE status = 'active'
      AND owner_id <> $me
      AND id <> $new_id
      AND ST_Intersects(geom, (SELECT geom FROM territories WHERE id = $new_id));
   ```

5. **Bust the tile cache** — `await bust_territory_tiles()` (`tile:territories:*`).

The new polygon is inserted whole; rivals it overlaps are flipped to
`superseded` in one indexed statement. No carving, no remainder geometry, no
partial areas — that complexity lived in the old corridor-capture path and is
gone.

---

## 6. Borrowed from RunRealm vs dropped

**Borrowed**

- The `ok()` response envelope (`{success, message, data}`).
- Loop-closure + speed-tier anti-cheat validation (CLEAN / SUSPICIOUS / VEHICLE /
  TELEPORT, score ≥ 0.40, 20 pts / 200 m / 500 m² minimums).
- The `start session → batch points → finish` flow.
- Supersede-on-overlap ("last claim wins").
- The streak / completion-percentage idea, reworked as daily/weekly/monthly `pct`.

**Dropped**

- The Supabase Python SDK and PostgREST — replaced by raw asyncpg.
- `territory_captures` table (was written but never read).
- Corridor / path-through capture (`buffer_route_m`, `difference`, MultiPolygon
  remainder handling).
- `teams`, `team_members`, `leagues`, `league_members`, `league_join_requests`,
  `league_delete_votes`.
- `xp_transactions`, XP, levels, `achievements`, `user_achievements`.
- `notifications`, `activity_feed`.
- `sync_queue` and the offline-sync machinery.
- Separate `user_profiles` / `streaks` tables.
- The `min_lat/max_lat/min_lon/max_lon` float columns + Shapely-in-Python bbox
  and area math — replaced by real `geometry(Polygon,4326)` and PostGIS
  functions.
- Chaikin smoothing / Douglas-Peucker in Python — simplification is now
  `ST_SimplifyPreserveTopology` at tile time.

---

## 7. Request / response conventions

- **Envelope.** Success bodies are `ok(data, message?)` →
  `{"success": true, "message": null | "...", "data": <payload>}`.
  The `.mvt` tile endpoint is the one exception — it returns raw protobuf bytes.
- **Keys are camelCase** in and out (`accessToken`, `avatarUrl`, `startedAt`,
  `claimTerritory`). Pydantic models (`app/schemas.py`) use `CamelModel` with
  field aliases; DB rows are mapped to camelCase by hand in each router.
- **Auth.** `Authorization: Bearer <jwt>`; HS256, `sub` = user id, `exp` =
  `JWT_EXPIRE_HOURS` (default 720 h / 30 days). `HTTPBearer(auto_error=True)`
  dependency; a bad or expired token → `401 "Invalid or expired token"`.
- **Errors** are plain `HTTPException(status_code, detail)`:
  `400` bad request / business rule · `401` missing/expired JWT ·
  `403` private profile · `404` not found / bad uuid · `409` duplicate email ·
  `422` body schema error or anti-cheat rejection.
- **CORS** is open (`allow_origins=["*"]`); `GZipMiddleware` compresses
  responses ≥ 500 bytes.

---

## 8. Endpoints

Base URL `http://localhost:8000`. All except `auth/*`, `tiles/*`, and `health`
require a bearer token.

### auth — `/auth`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/register` | – | Create a user, return a token + profile. |
| POST | `/auth/login` | – | Verify credentials, return a token + profile. |

### users — `/users`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/users/me` | ✔ | Full own profile. |
| PATCH | `/users/me` | ✔ | Update `name` / `contact` / `avatarUrl` / `visibility` / `timezone`. |
| GET | `/users/{id}` | ✔ | Another user's profile — trimmed unless self, public, or connected. |
| POST | `/users/{id}/connect` | ✔ | Send (or upsert) a connection request. |
| POST | `/users/{id}/accept` | ✔ | Accept a pending request from `{id}`. |
| GET | `/users/me/connections` | ✔ | List accepted connections. |

### sessions — `/sessions`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/sessions` | ✔ | Start a run (`startedAt`), status `active`. |
| POST | `/sessions/{id}/points` | ✔ | Batch-append GPS points (array → one multi-row insert). |
| POST | `/sessions/{id}/finish` | ✔ | Complete the run; validate + generate a territory. |
| GET | `/sessions` | ✔ | Own session history, paginated. |
| GET | `/sessions/{id}` | ✔ | One session. |
| GET | `/sessions/{id}/points` | ✔ | That session's points in `seq` order. |

### territories — `/territories`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/territories/mine` | ✔ | Own active territories. |
| GET | `/territories/{id}` | ✔ | One territory (GeoJSON geometry + metadata). |
| DELETE | `/territories/{id}` | ✔ | Retire own territory; busts the tile cache. |

### tiles — `/tiles`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/tiles/territories/{z}/{x}/{y}.mvt` | – | Redis-cached, CDN-frontable vector tile of active territories. |

### todos — `/todos`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/todos` | ✔ | Create a todo (due = today in the user's tz). |
| GET | `/todos` | ✔ | List todos, filterable by day/range. |
| PATCH | `/todos/{id}` | ✔ | Update fields / mark `complete` (with optional `rating`). |
| DELETE | `/todos/{id}` | ✔ | Delete a todo. |

### stats — `/stats`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/stats/daily` | ✔ | Per-day completion % (range). |
| GET | `/stats/weekly` | ✔ | Per-week (Monday-anchored) %. |
| GET | `/stats/monthly` | ✔ | Per-month %. |
| GET | `/stats/summary` | ✔ | Current day/week/month % + streak in one call. |

### health

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | – | `{"status": "ok"}` liveness probe. |

Interactive docs at `/docs` (Swagger) and `/redoc`.

---

## 9. Deployment & scaling

### Local

`docker compose up` starts three services (`docker-compose.yml`):

| Service | Image | Notes |
|---|---|---|
| `db` | `postgis/postgis:15-3.4` | `sql/schema.sql` is mounted into `/docker-entrypoint-initdb.d/` and applied on first boot. Healthcheck gates the API. |
| `redis` | `redis:7-alpine` | Persistence off (`--save "" --appendonly no`) — it is a pure cache. |
| `api` | built from `Dockerfile` | `uvicorn app.main:app --workers 2`, waits for `db` healthy. |

### Production notes

- **Connection pooling.** asyncpg pool is `min_size=2, max_size=10` per worker.
  Put **PgBouncer** (transaction pooling) in front of Postgres and size
  `workers × max_size` to stay under `max_connections`.
- **CDN in front of `/tiles/*`.** The tiles are `public, max-age=<ttl>` and carry
  no auth — a CDN (or even nginx `proxy_cache`) should absorb the vast majority
  of map traffic. Origin then only sees one request per tile per TTL per PoP.
- **Read replica (optional).** Route `GET /tiles/*` and other read-only endpoints
  to a replica; keep writes and `finish` on the primary.
- **Nightly job.** APScheduler runs in-process and is fine for one node. For
  multiple API nodes, either pin the scheduler to one node or move the job to
  **`pg_cron`** so it runs once in the database regardless of app topology.
- **Index maintenance.** `superseded` territories grow forever; `autovacuum`
  keeps the partial index healthy. Periodically `REINDEX` or archive very old
  superseded rows if the table gets large.
- **Secrets.** Set a real `JWT_SECRET`; never ship `dev-secret-change-me`.

### When to graduate

- Map read load outgrows a single Postgres + CDN → run a dedicated tile server
  (**pg_tileserv** or **Martin**) against the same table.
- Even that is hot → pre-render low/mid-zoom tiles to **object storage** on
  territory change and serve them statically; keep dynamic rendering only for
  high zoom.
- Write path grows → move `finish` territory generation to a queue/worker and
  return `202`.

---

## 10. Repo layout

```
runrealm-lite/
├── ARCHITECTURE.md          this file
├── README.md                quick start
├── docker-compose.yml       db (postgis) + redis + api
├── Dockerfile               python:3.11-slim, uvicorn, 2 workers
├── requirements.txt         10 packages, no ORM, no Shapely
├── .env.example             copy to .env for non-docker runs
├── sql/
│   └── schema.sql           full schema; auto-applied by initdb
└── app/
    ├── main.py              FastAPI app, lifespan (db + scheduler), router wiring
    ├── config.py            pydantic-settings; env-driven knobs & thresholds
    ├── db.py                asyncpg pool (connect/disconnect/pool())
    ├── cache.py             Redis wrapper: cache_get/set, cache_delete_pattern (SCAN)
    ├── security.py          bcrypt hashing + JWT encode/decode
    ├── deps.py              get_db(), get_current_user_id() dependencies
    ├── schemas.py           Pydantic CamelModel request bodies + ok() envelope
    ├── avatar.py            deterministic initials-SVG data-URI generator
    ├── geo.py               pure-Python loop closure + anti-cheat  (feature layer)
    ├── jobs.py              APScheduler: nightly todo auto-fail + stats rollup  (feature layer)
    └── routers/
        ├── auth.py          register, login
        ├── users.py         me, {id}, connect/accept, connections
        ├── sessions.py      start, points, finish, history  (feature layer)
        ├── territories.py   mine, {id}, delete  (feature layer)
        ├── tiles.py         /tiles/territories/{z}/{x}/{y}.mvt — cached MVT
        ├── todos.py         CRUD + complete  (feature layer)
        └── stats.py         daily / weekly / monthly / summary  (feature layer)
```

*Feature layer* = `geo.py`, `jobs.py`, and the `sessions` / `territories` /
`todos` / `stats` routers are being filled in against the contracts in §5 and §8;
`config.py`, `db.py`, `cache.py`, `security.py`, `deps.py`, `schemas.py`,
`avatar.py`, `main.py`, `routers/auth.py`, `routers/users.py`, `routers/tiles.py`
are complete.

---

## 11. Roadmap / TODO

- **Timezone-correct midnight.** Run the auto-fail/rollup per distinct
  `users.timezone` at that zone's local midnight, not one global 00:00.
- **Connection request notifications.** A lightweight `notifications` table or a
  push hook when a `connect`/`accept` happens.
- **Avatar upload to object storage.** `PATCH /users/me` currently accepts an
  `avatarUrl` string; add a real multipart upload → S3/GCS → store the URL.
- **Rate limiting.** Per-IP on `auth/*`, per-user on `sessions/{id}/points`.
- **Targeted tile invalidation + push.** Delete only the covered tile keys; add
  an SSE/WebSocket channel of changed keys (Tier 3, §4).
- **Tests.** Contract tests for the endpoint list in §8 and a PostGIS fixture for
  polygon generation + supersede.
- **Idempotency keys** on `POST /sessions` and `finish` for flaky mobile
  networks.

# RunRealm Lite

A small, fast backend for a RunRealm-style territory game: email/password auth,
public/private user profiles with peer connections, run sessions with batched GPS
points, run loops turned into **PostGIS territory polygons** on a shared map, and
daily/weekly/monthly todo completion stats. The map is served as **Redis-cached
vector tiles** so many thousands of concurrent viewers cost the database almost
nothing. FastAPI + PostgreSQL 15/PostGIS + Redis + raw asyncpg (no ORM), JWT
auth. It replaces a much larger Supabase-SDK backend — see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Quick start (Docker)

```bash
cp .env.example .env          # optional for docker; compose sets its own env
docker compose up
```

- API: <http://localhost:8000>
- Interactive docs: <http://localhost:8000/docs>
- The schema in `sql/schema.sql` is applied automatically on the database's first
  boot (Postgres `initdb`).

## Local dev without Docker

```bash
# 1. start Postgres+PostGIS and Redis however you like, e.g.
docker run -d -p 5432:5432 -e POSTGRES_USER=runrealm -e POSTGRES_PASSWORD=runrealm \
  -e POSTGRES_DB=runrealm postgis/postgis:15-3.4
docker run -d -p 6379:6379 redis:7-alpine

# 2. apply the schema
cp .env.example .env
psql "postgresql://runrealm:runrealm@localhost:5432/runrealm" -f sql/schema.sql

# 3. run the API
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Config is read from `.env` (see `app/config.py`): `DATABASE_URL`, `REDIS_URL`,
`JWT_SECRET`, `JWT_EXPIRE_HOURS`, `DEFAULT_TIMEZONE`, `TILE_CACHE_TTL`.

## End-to-end curl example

```bash
BASE=http://localhost:8000

# register -> token
TOKEN=$(curl -s -X POST $BASE/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"jai@example.com","password":"secret123","name":"Jai"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["accessToken"])')

# (or later: POST $BASE/auth/login with the same body minus name)
AUTH="Authorization: Bearer $TOKEN"

# start a session
SID=$(curl -s -X POST $BASE/sessions -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"startedAt":"2026-09-08T06:00:00Z"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["id"])')

# upload a batch of GPS points (a small square loop)
curl -s -X POST $BASE/sessions/$SID/points -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "points":[
    {"lat":28.6139,"lng":77.2090,"seq":1,"recordedAt":"2026-09-08T06:00:01Z"},
    {"lat":28.6149,"lng":77.2090,"seq":2,"recordedAt":"2026-09-08T06:03:01Z"},
    {"lat":28.6149,"lng":77.2100,"seq":3,"recordedAt":"2026-09-08T06:06:01Z"},
    {"lat":28.6139,"lng":77.2100,"seq":4,"recordedAt":"2026-09-08T06:09:01Z"},
    {"lat":28.6139,"lng":77.2090,"seq":5,"recordedAt":"2026-09-08T06:12:01Z"}
  ]
}'
# (a real run sends ~20+ points per the anti-cheat minimums)

# finish -> validates the loop and claims a territory
curl -s -X POST $BASE/sessions/$SID/finish -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"endedAt":"2026-09-08T06:12:30Z","distanceM":900,"claimTerritory":true}'

# fetch a vector tile covering the area (no auth needed)
curl -s "$BASE/tiles/territories/14/11599/6660.mvt" -o tile.mvt
```

## More

Design, data model, scaling notes, and the full endpoint list:
[ARCHITECTURE.md](ARCHITECTURE.md).

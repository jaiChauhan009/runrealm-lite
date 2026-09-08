"""Run sessions — start, stream GPS points, finish (and optionally claim a
territory). Mounted at ``/sessions``.

Raw asyncpg. UUID path/args are passed as text and cast with ``$n::uuid`` in
SQL. The territory polygon is built entirely in PostGIS from the ordered point
list; loop-closure + anti-cheat are pure-Python (see ``app.geo``).
"""
from __future__ import annotations

from datetime import timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app import cache
from app.config import settings
from app.deps import get_current_user_id, get_db
from app.geo import anticheat, validate_loop
from app.schemas import (
    PointsBatchRequest,
    SessionFinishRequest,
    SessionStartRequest,
    ok,
)

router = APIRouter()


# ── start ────────────────────────────────────────────────────
@router.post("")
async def start_session(
    body: SessionStartRequest,
    db=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    row = await db.fetchrow(
        """
        INSERT INTO run_sessions (user_id, started_at, status)
        VALUES ($1::uuid, $2, 'active')
        RETURNING id, started_at, status
        """,
        user_id,
        body.started_at,
    )
    return ok({
        "id": str(row["id"]),
        "startedAt": row["started_at"],
        "status": row["status"],
    })


# ── append GPS points ────────────────────────────────────────
@router.post("/{session_id}/points")
async def add_points(
    session_id: str,
    body: PointsBatchRequest,
    db=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    owns = await db.fetchval(
        "SELECT 1 FROM run_sessions WHERE id = $1::uuid AND user_id = $2::uuid",
        session_id,
        user_id,
    )
    if not owns:
        raise HTTPException(status_code=404, detail="Session not found")

    rows = [
        (session_id, p.lat, p.lng, p.speed_kmh, p.bearing, p.seq, p.recorded_at)
        for p in body.points
    ]
    if rows:
        await db.executemany(
            """
            INSERT INTO route_points
                (session_id, lat, lng, speed_kmh, bearing, seq, recorded_at)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (session_id, seq) DO NOTHING
            """,
            rows,
        )
    return ok({"saved": len(rows)})


# ── finish (+ optional territory claim) ──────────────────────
@router.post("/{session_id}/finish")
async def finish_session(
    session_id: str,
    body: SessionFinishRequest,
    db=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    sess = await db.fetchrow(
        """
        SELECT id, user_id, started_at, status
        FROM run_sessions
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        session_id,
        user_id,
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    if sess["status"] != "active":
        raise HTTPException(status_code=409, detail="Session is not active")

    ended_at = body.ended_at
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)
    duration_s = max(0, int((ended_at - sess["started_at"]).total_seconds()))

    await db.execute(
        """
        UPDATE run_sessions
        SET ended_at = $2, duration_s = $3, distance_m = $4, status = 'completed'
        WHERE id = $1::uuid
        """,
        session_id,
        ended_at,
        duration_s,
        body.distance_m,
    )

    if not body.claim_territory:
        return ok({"claimed": False, "reason": "territory claim not requested"})

    # ── load ordered points and run pure-Python validation ──
    pt_rows = await db.fetch(
        """
        SELECT lat, lng, recorded_at
        FROM route_points
        WHERE session_id = $1::uuid
        ORDER BY seq
        """,
        session_id,
    )
    points = [
        {"lat": r["lat"], "lng": r["lng"], "recorded_at": r["recorded_at"]}
        for r in pt_rows
    ]

    loop_ok, loop_reason = validate_loop(points)
    if not loop_ok:
        return ok({"claimed": False, "reason": loop_reason})

    ac_ok, ac_reason, score = anticheat(points)
    if not ac_ok:
        return ok({"claimed": False, "reason": ac_reason})

    # ── build the polygon in PostGIS ──
    # Explicitly close the ring (the loop gap can be up to territory_loop_close_m).
    lngs = [p["lng"] for p in points]
    lats = [p["lat"] for p in points]
    if lngs[0] != lngs[-1] or lats[0] != lats[-1]:
        lngs.append(lngs[0])
        lats.append(lats[0])

    try:
        terr = await db.fetchrow(
            """
            WITH ring AS (
                SELECT ST_MakeValid(
                    ST_MakePolygon(
                        ST_MakeLine(
                            ARRAY(
                                SELECT ST_SetSRID(ST_MakePoint(p.lng, p.lat), 4326)
                                FROM unnest($3::float8[], $4::float8[])
                                     AS p(lng, lat)
                            )
                        )
                    )
                ) AS geom
            )
            INSERT INTO territories (owner_id, session_id, geom, area_m2, status)
            SELECT $1::uuid, $2::uuid, ring.geom,
                   ST_Area(ring.geom::geography), 'active'
            FROM ring
            RETURNING id, area_m2
            """,
            user_id,
            session_id,
            lngs,
            lats,
        )
    except asyncpg.PostgresError:
        # self-intersecting / degenerate route -> ST_MakeValid yielded a
        # non-Polygon that won't fit geometry(Polygon,4326).
        return ok({"claimed": False, "reason": "could not build a valid territory polygon"})

    territory_id = str(terr["id"])
    area_m2 = float(terr["area_m2"] or 0.0)

    if area_m2 < settings.territory_min_area_m2:
        await db.execute("DELETE FROM territories WHERE id = $1::uuid", territory_id)
        return ok({"claimed": False, "reason": "polygon too small"})

    superseded = await db.fetch(
        """
        UPDATE territories t
        SET status = 'superseded'
        FROM territories n
        WHERE n.id = $1::uuid
          AND t.id <> n.id
          AND t.status = 'active'
          AND t.owner_id <> $2::uuid
          AND ST_Intersects(t.geom, n.geom)
        RETURNING t.id
        """,
        territory_id,
        user_id,
    )

    await cache.cache_delete_pattern("tile:territories:*")

    return ok({
        "claimed": True,
        "territoryId": territory_id,
        "areaM2": round(area_m2, 2),
        "supersededCount": len(superseded),
    })


# ── reads ────────────────────────────────────────────────────
@router.get("")
async def list_sessions(
    limit: int = 20,
    db=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    rows = await db.fetch(
        """
        SELECT id, started_at, ended_at, duration_s, distance_m, status
        FROM run_sessions
        WHERE user_id = $1::uuid
        ORDER BY started_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return ok([
        {
            "id": str(r["id"]),
            "startedAt": r["started_at"],
            "endedAt": r["ended_at"],
            "durationS": r["duration_s"],
            "distanceM": r["distance_m"],
            "status": r["status"],
        }
        for r in rows
    ])


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    db=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    row = await db.fetchrow(
        """
        SELECT s.id, s.user_id, s.started_at, s.ended_at, s.duration_s,
               s.distance_m, s.status,
               (SELECT count(*) FROM route_points p WHERE p.session_id = s.id)
                   AS point_count
        FROM run_sessions s
        WHERE s.id = $1::uuid
        """,
        session_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    if str(row["user_id"]) != str(user_id):
        raise HTTPException(status_code=403, detail="Not your session")
    return ok({
        "id": str(row["id"]),
        "startedAt": row["started_at"],
        "endedAt": row["ended_at"],
        "durationS": row["duration_s"],
        "distanceM": row["distance_m"],
        "status": row["status"],
        "pointCount": row["point_count"],
    })


@router.get("/{session_id}/points")
async def get_session_points(
    session_id: str,
    db=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    owner = await db.fetchval(
        "SELECT user_id FROM run_sessions WHERE id = $1::uuid", session_id
    )
    if owner is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if str(owner) != str(user_id):
        raise HTTPException(status_code=403, detail="Not your session")

    rows = await db.fetch(
        """
        SELECT lat, lng, speed_kmh, bearing, seq, recorded_at
        FROM route_points
        WHERE session_id = $1::uuid
        ORDER BY seq
        """,
        session_id,
    )
    return ok([
        {
            "lat": r["lat"],
            "lng": r["lng"],
            "speedKmh": r["speed_kmh"],
            "bearing": r["bearing"],
            "seq": r["seq"],
            "recordedAt": r["recorded_at"],
        }
        for r in rows
    ])

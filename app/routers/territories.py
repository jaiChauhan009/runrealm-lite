"""Territories — read + soft-delete. Mounted at ``/territories``.

Claiming happens in the sessions router (POST /sessions/{id}/finish). Here we
only expose the caller's polygons, a single territory with owner info, and a
soft-delete that supersedes a territory the caller owns.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from app import cache
from app.deps import get_current_user_id, get_db
from app.schemas import ok

router = APIRouter()


def _as_json(value):
    """asyncpg hands back a ``::json`` column as text — parse it once."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


# NOTE: /mine must be declared before /{territory_id} so it isn't captured
# as a territory id.
@router.get("/mine")
async def my_territories(
    db=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    rows = await db.fetch(
        """
        SELECT id, area_m2, created_at, ST_AsGeoJSON(geom)::json AS geojson
        FROM territories
        WHERE owner_id = $1::uuid AND status = 'active'
        ORDER BY created_at DESC
        """,
        user_id,
    )
    return ok([
        {
            "id": str(r["id"]),
            "areaM2": r["area_m2"],
            "createdAt": r["created_at"],
            "geojson": _as_json(r["geojson"]),
        }
        for r in rows
    ])


@router.get("/{territory_id}")
async def get_territory(
    territory_id: str,
    db=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    row = await db.fetchrow(
        """
        SELECT t.id, t.session_id, t.area_m2, t.status, t.created_at,
               ST_AsGeoJSON(t.geom)::json AS geojson,
               u.id AS owner_id, u.name AS owner_name, u.avatar_url AS owner_avatar
        FROM territories t
        JOIN users u ON u.id = t.owner_id
        WHERE t.id = $1::uuid
        """,
        territory_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Territory not found")
    return ok({
        "id": str(row["id"]),
        "sessionId": str(row["session_id"]) if row["session_id"] else None,
        "areaM2": row["area_m2"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "geojson": _as_json(row["geojson"]),
        "owner": {
            "id": str(row["owner_id"]),
            "name": row["owner_name"],
            "avatarUrl": row["owner_avatar"],
        },
    })


@router.delete("/{territory_id}")
async def delete_territory(
    territory_id: str,
    db=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    row = await db.fetchrow(
        "SELECT owner_id FROM territories WHERE id = $1::uuid", territory_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Territory not found")
    if str(row["owner_id"]) != str(user_id):
        raise HTTPException(status_code=403, detail="Not your territory")

    await db.execute(
        "UPDATE territories SET status = 'superseded' WHERE id = $1::uuid",
        territory_id,
    )
    await cache.cache_delete_pattern("tile:territories:*")
    return ok({"id": territory_id, "deleted": True})

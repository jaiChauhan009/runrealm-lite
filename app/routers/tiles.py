"""Public territory vector-tile layer (MVT), Redis-cached.

No auth: this is the base map layer the client renders for everyone, so it
must stay cacheable both in Redis and by any HTTP cache in front of us.
"""
import logging

from fastapi import APIRouter, Response

from app.cache import cache_delete_pattern, cache_get, cache_set
from app.config import settings
from app.db import pool

log = logging.getLogger("runrealm.tiles")

router = APIRouter()

_MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"

_TILE_SQL = """
SELECT ST_AsMVT(q, 'territories', 4096, 'geom') FROM (
    SELECT id::text, owner_id::text, area_m2,
           ST_AsMVTGeom(
             ST_Transform(ST_SimplifyPreserveTopology(geom, $4), 3857),
             ST_TileEnvelope($1,$2,$3), 4096, 64, true) AS geom
    FROM territories
    WHERE status='active'
      AND geom && ST_Transform(ST_TileEnvelope($1,$2,$3), 4326)
) q WHERE q.geom IS NOT NULL;
"""


def _simplify_tolerance(z: int) -> float:
    """Simplify tolerance in degrees: coarse when zoomed out, ~0 past ~z15."""
    return max(0.0, 0.5 / (2 ** z) * 0.01)


async def bust_territory_tiles() -> int:
    """Drop every cached territory tile. Named alias other modules can import."""
    return await cache_delete_pattern("tile:territories:*")


@router.get("/territories/{z}/{x}/{y}.mvt")
async def territory_tile(z: int, x: int, y: int) -> Response:
    z = max(0, min(22, z))
    key = f"tile:territories:{z}:{x}:{y}"

    mvt: bytes = b""
    try:
        cached = await cache_get(key)
        if cached is not None:
            mvt = cached
        else:
            async with pool().acquire() as conn:
                row = await conn.fetchval(
                    _TILE_SQL, z, x, y, _simplify_tolerance(z)
                )
            mvt = bytes(row) if row is not None else b""
            # Cache empty tiles too: most tiles in a sparse map are empty and
            # this keeps them from hitting PostGIS on every pan.
            await cache_set(key, mvt, settings.tile_cache_ttl)
    except Exception:  # never 500 the base map layer
        log.exception("tile render failed z=%s x=%s y=%s", z, x, y)
        mvt = b""

    return Response(
        content=mvt or b"",
        media_type=_MVT_MEDIA_TYPE,
        headers={"Cache-Control": f"public, max-age={settings.tile_cache_ttl}"},
    )

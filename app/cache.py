"""Thin Redis wrapper for the tile cache and any short-lived read caches."""
import redis.asyncio as aioredis

from app.config import settings

_r: aioredis.Redis | None = None


def r() -> aioredis.Redis:
    global _r
    if _r is None:
        _r = aioredis.from_url(settings.redis_url, decode_responses=False)
    return _r


async def disconnect() -> None:
    global _r
    if _r is not None:
        await _r.aclose()
        _r = None


async def cache_get(key: str) -> bytes | None:
    return await r().get(key)


async def cache_set(key: str, value: bytes, ttl: int) -> None:
    await r().set(key, value, ex=ttl)


async def cache_delete_pattern(pattern: str) -> int:
    """Delete every key matching a glob pattern (SCAN — safe on big keyspaces)."""
    client = r()
    removed = 0
    async for key in client.scan_iter(match=pattern, count=500):
        removed += await client.delete(key)
    return removed

"""Raw asyncpg connection pool — no ORM. Fast and small.

Usage in a router:

    from app.deps import get_db

    @router.get("/thing")
    async def thing(db=Depends(get_db)):
        row = await db.fetchrow("SELECT 1 AS n")
        return row["n"]
"""
import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def connect() -> None:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=2,
            max_size=10,
            command_timeout=15,
        )


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    assert _pool is not None, "db pool not initialised — call app.db.connect() first"
    return _pool

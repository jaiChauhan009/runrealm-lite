from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app import cache, db, jobs
from app.routers import auth, sessions, stats, territories, tiles, todos, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    jobs.start_scheduler()
    try:
        yield
    finally:
        jobs.stop_scheduler()
        await cache.disconnect()
        await db.disconnect()


app = FastAPI(title="RunRealm Lite", version="0.1.0", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
app.include_router(territories.router, prefix="/territories", tags=["territories"])
app.include_router(tiles.router, prefix="/tiles", tags=["tiles"])
app.include_router(todos.router, prefix="/todos", tags=["todos"])
app.include_router(stats.router, prefix="/stats", tags=["stats"])


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}

"""APScheduler jobs — nightly todo auto-fail + stats rollups.

Cadence: one job, ``_tick``, runs once an hour. Each run walks the distinct
``users.timezone`` values and only does work for a timezone that has *just*
rolled past local midnight (local hour == 0). That way a single hourly job
gives every timezone its own "nightly" pass without per-tz scheduling.

Everything is asyncpg — no psycopg anywhere. The recompute helpers are async
and take a live connection so ``app.routers.todos`` can call them inside a
request, while the scheduler acquires its own connection from the pool.
"""
from __future__ import annotations

import asyncio
import logging

from app.db import pool

log = logging.getLogger("runrealm.jobs")

_scheduler = None  # module global so stop_scheduler() can reach it


# ── stats recompute helpers (reused by todos.py) ─────────────────────────────

async def _refresh_week_month(db, user_id: str, day) -> None:
    """Recompute weekly_stats (Monday week) + monthly_stats for the week/month
    that contains ``day``, aggregating the user's daily_stats rows."""
    await db.execute(
        """
        INSERT INTO weekly_stats (user_id, week_start, pct)
        SELECT $1,
               date_trunc('week', $2::date)::date,
               COALESCE(ROUND(SUM(completed) * 100.0 / NULLIF(SUM(total), 0), 1), 0)
        FROM daily_stats
        WHERE user_id = $1
          AND day >= date_trunc('week', $2::date)::date
          AND day <  date_trunc('week', $2::date)::date + INTERVAL '7 days'
        ON CONFLICT (user_id, week_start) DO UPDATE SET pct = EXCLUDED.pct
        """,
        user_id,
        day,
    )
    await db.execute(
        """
        INSERT INTO monthly_stats (user_id, month_start, pct)
        SELECT $1,
               date_trunc('month', $2::date)::date,
               COALESCE(ROUND(SUM(completed) * 100.0 / NULLIF(SUM(total), 0), 1), 0)
        FROM daily_stats
        WHERE user_id = $1
          AND day >= date_trunc('month', $2::date)::date
          AND day <  date_trunc('month', $2::date)::date + INTERVAL '1 month'
        ON CONFLICT (user_id, month_start) DO UPDATE SET pct = EXCLUDED.pct
        """,
        user_id,
        day,
    )


async def _recompute_daily(db, user_id: str, day) -> None:
    """Upsert daily_stats for ``user_id`` on the local calendar date ``day``
    (todos are bucketed by created_at in the user's own timezone), then refresh
    the containing week and month."""
    await db.execute(
        """
        INSERT INTO daily_stats (user_id, day, completed, total, pct)
        SELECT $1,
               $2::date,
               COUNT(*) FILTER (WHERE t.status = 'complete'),
               COUNT(*),
               CASE WHEN COUNT(*) = 0 THEN 0
                    ELSE ROUND(COUNT(*) FILTER (WHERE t.status = 'complete') * 100.0
                               / COUNT(*), 1)
               END
        FROM todos t
        JOIN users u ON u.id = t.user_id
        WHERE t.user_id = $1
          AND (t.created_at AT TIME ZONE u.timezone)::date = $2::date
        ON CONFLICT (user_id, day) DO UPDATE
          SET completed = EXCLUDED.completed,
              total     = EXCLUDED.total,
              pct       = EXCLUDED.pct
        """,
        user_id,
        day,
    )
    await _refresh_week_month(db, user_id, day)


# ── the hourly tick ─────────────────────────────────────────────────────────

async def _tick() -> None:
    try:
        async with pool().acquire() as db:
            tzs = [
                r["timezone"]
                for r in await db.fetch("SELECT DISTINCT timezone FROM users")
            ]
            for tz in tzs:
                local_hour = await db.fetchval(
                    "SELECT EXTRACT(HOUR FROM (now() AT TIME ZONE $1))::int", tz
                )
                if local_hour != 0:
                    continue
                await _run_nightly_for_tz(db, tz)
    except Exception:
        log.exception("scheduler tick failed")


async def _run_nightly_for_tz(db, tz: str) -> None:
    # 1. anything still pending whose local due-day is already in the past fails
    await db.execute(
        """
        UPDATE todos
        SET status = 'failed'
        WHERE status = 'pending'
          AND (created_at AT TIME ZONE $1)::date < (now() AT TIME ZONE $1)::date
          AND user_id IN (SELECT id FROM users WHERE timezone = $1)
        """,
        tz,
    )

    # 2. roll up yesterday (local) for every user in this timezone
    yesterday = await db.fetchval(
        "SELECT ((now() AT TIME ZONE $1)::date - 1)", tz
    )
    user_ids = [
        str(r["id"])
        for r in await db.fetch(
            "SELECT id FROM users WHERE timezone = $1", tz
        )
    ]
    for uid in user_ids:
        try:
            await _recompute_daily(db, uid, yesterday)
        except Exception:
            log.exception("nightly recompute failed user=%s tz=%s", uid, tz)


# ── lifecycle (names imported by app/main.py) ───────────────────────────────

def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        sched = AsyncIOScheduler()
        sched.add_job(
            _tick,
            "interval",
            hours=1,
            id="runrealm_hourly_tick",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        sched.start()
        _scheduler = sched
        log.info("scheduler started (hourly tick)")
    except Exception:
        # A broken scheduler must never take down app startup.
        log.exception("scheduler failed to start — continuing without it")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        log.exception("scheduler shutdown failed")
    finally:
        _scheduler = None

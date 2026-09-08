"""Read-only stats rollups for the caller, straight out of the *_stats tables
that app/jobs.py maintains."""
from fastapi import APIRouter, Depends, Query

from app.deps import get_current_user_id, get_db
from app.schemas import ok

router = APIRouter()


@router.get("/daily")
async def daily(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None, alias="to"),
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    rows = await db.fetch(
        """
        SELECT day, completed, total, pct
        FROM daily_stats
        WHERE user_id = $1
          AND day >= COALESCE($2::date, CURRENT_DATE - 13)
          AND day <= COALESCE($3::date, CURRENT_DATE)
        ORDER BY day
        """,
        user_id,
        from_,
        to,
    )
    return ok([
        {
            "day": r["day"].isoformat(),
            "completed": r["completed"],
            "total": r["total"],
            "pct": float(r["pct"]),
        }
        for r in rows
    ])


@router.get("/weekly")
async def weekly(
    weeks: int = Query(8, ge=1, le=104),
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    rows = await db.fetch(
        """
        SELECT week_start, pct FROM weekly_stats
        WHERE user_id = $1
        ORDER BY week_start DESC
        LIMIT $2
        """,
        user_id,
        weeks,
    )
    return ok([
        {"weekStart": r["week_start"].isoformat(), "pct": float(r["pct"])}
        for r in reversed(rows)
    ])


@router.get("/monthly")
async def monthly(
    months: int = Query(6, ge=1, le=60),
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    rows = await db.fetch(
        """
        SELECT month_start, pct FROM monthly_stats
        WHERE user_id = $1
        ORDER BY month_start DESC
        LIMIT $2
        """,
        user_id,
        months,
    )
    return ok([
        {"monthStart": r["month_start"].isoformat(), "pct": float(r["pct"])}
        for r in reversed(rows)
    ])


@router.get("/summary")
async def summary(
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    row = await db.fetchrow(
        """
        WITH me AS (
            SELECT (now() AT TIME ZONE timezone)::date AS today
            FROM users WHERE id = $1
        )
        SELECT
          me.today,
          COALESCE((SELECT pct FROM daily_stats
                    WHERE user_id = $1 AND day = me.today), 0)                      AS daily_pct,
          COALESCE((SELECT pct FROM weekly_stats
                    WHERE user_id = $1
                      AND week_start = date_trunc('week', me.today)::date), 0)      AS weekly_pct,
          COALESCE((SELECT pct FROM monthly_stats
                    WHERE user_id = $1
                      AND month_start = date_trunc('month', me.today)::date), 0)    AS monthly_pct
        FROM me
        """,
        user_id,
    )
    if row is None:  # unknown user id in a valid token
        return ok({"day": None, "dailyPct": 0.0, "weeklyPct": 0.0, "monthlyPct": 0.0})
    return ok({
        "day": row["today"].isoformat(),
        "dailyPct": float(row["daily_pct"]),
        "weeklyPct": float(row["weekly_pct"]),
        "monthlyPct": float(row["monthly_pct"]),
    })

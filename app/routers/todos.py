"""Daily todos. "Due date" == the calendar day the todo was created, read in
the owner's timezone. There is no due_date column; everything derives from
``created_at AT TIME ZONE users.timezone``.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.deps import get_current_user_id, get_db
from app.jobs import _recompute_daily
from app.schemas import TodoCreateRequest, TodoUpdateRequest, ok

router = APIRouter()

_ROW = "id, user_id, title, description, rating, status, created_at"


def _serialize(row, *, status: str | None = None) -> dict:
    return {
        "id": str(row["id"]),
        "userId": str(row["user_id"]),
        "title": row["title"],
        "description": row["description"],
        "rating": row["rating"],
        "status": status or row["status"],
        "createdAt": row["created_at"].isoformat(),
    }


async def _todo_context(db, todo_id: str, user_id: str):
    """Return (row, local_day) for a todo or raise 404/403."""
    row = await db.fetchrow(
        f"""
        SELECT {_ROW},
               (created_at AT TIME ZONE u.timezone)::date AS local_day
        FROM todos t
        JOIN users u ON u.id = t.user_id
        WHERE t.id = $1
        """,
        todo_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Todo not found")
    if str(row["user_id"]) != str(user_id):
        raise HTTPException(status_code=403, detail="Not your todo")
    return row, row["local_day"]


@router.post("/")
async def create_todo(
    body: TodoCreateRequest,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    row = await db.fetchrow(
        f"""
        INSERT INTO todos (user_id, title, description, rating, status)
        VALUES ($1, $2, $3, $4, 'pending')
        RETURNING {_ROW}
        """,
        user_id,
        body.title,
        body.description,
        body.rating,
    )
    return ok(_serialize(row))


@router.get("/")
async def list_todos(
    day: str | None = None,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    rows = await db.fetch(
        f"""
        SELECT {_ROW},
               (t.created_at AT TIME ZONE u.timezone)::date AS local_day,
               (now() AT TIME ZONE u.timezone)::date       AS today
        FROM todos t
        JOIN users u ON u.id = t.user_id
        WHERE t.user_id = $1
          AND (t.created_at AT TIME ZONE u.timezone)::date
              = COALESCE($2::date, (now() AT TIME ZONE u.timezone)::date)
        ORDER BY t.created_at
        """,
        user_id,
        day,
    )

    items = []
    counts = {"complete": 0, "failed": 0, "pending": 0}
    for row in rows:
        eff = row["status"]
        # lazy: a still-pending todo whose local day is already past reads as
        # "failed" here without touching the DB (the nightly job persists it).
        if eff == "pending" and row["local_day"] < row["today"]:
            eff = "failed"
        counts[eff] = counts.get(eff, 0) + 1
        items.append(_serialize(row, status=eff))

    total = len(items)
    complete = counts["complete"]
    summary = {
        "total": total,
        "complete": complete,
        "failed": counts["failed"],
        "pending": counts["pending"],
        "pct": round(complete * 100 / total, 1) if total else 0.0,
    }
    return ok({"items": items, "summary": summary})


@router.patch("/{todo_id}")
async def update_todo(
    todo_id: str,
    body: TodoUpdateRequest,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    _, local_day = await _todo_context(db, todo_id, user_id)

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        row = await db.fetchrow(f"SELECT {_ROW} FROM todos WHERE id = $1", todo_id)
        return ok(_serialize(row))

    sets, params = [], []
    for i, (col, val) in enumerate(fields.items(), start=1):
        sets.append(f"{col} = ${i}")
        params.append(val)
    params.append(todo_id)
    row = await db.fetchrow(
        f"UPDATE todos SET {', '.join(sets)} WHERE id = ${len(params)} RETURNING {_ROW}",
        *params,
    )

    if fields.keys() & {"status", "rating"}:
        await _recompute_daily(db, user_id, local_day)

    return ok(_serialize(row))


@router.delete("/{todo_id}")
async def delete_todo(
    todo_id: str,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    _, local_day = await _todo_context(db, todo_id, user_id)
    await db.execute("DELETE FROM todos WHERE id = $1", todo_id)
    await _recompute_daily(db, user_id, local_day)
    return ok(None, "Todo deleted")

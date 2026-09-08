"""Users router — own profile, other profiles (visibility-gated), connections."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.deps import get_current_user_id, get_db
from app.schemas import UserUpdateRequest, ok

router = APIRouter()

_UPDATABLE = ("name", "contact", "avatar_url", "visibility", "timezone")


def _valid_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="User not found")


def _full_user(row) -> dict:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "avatarUrl": row["avatar_url"],
        "contact": row["contact"],
        "visibility": row["visibility"],
        "timezone": row["timezone"],
        "createdAt": row["created_at"],
    }


async def _accepted_connection(db, a: str, b: str) -> bool:
    found = await db.fetchval(
        """
        SELECT 1 FROM connections
        WHERE status = 'accepted'
          AND ((requester_id = $1::uuid AND addressee_id = $2::uuid)
            OR (requester_id = $2::uuid AND addressee_id = $1::uuid))
        """,
        a,
        b,
    )
    return bool(found)


@router.get("/me")
async def get_me(user_id: str = Depends(get_current_user_id), db=Depends(get_db)):
    row = await db.fetchrow(
        """
        SELECT id, email, name, avatar_url, contact, visibility, timezone, created_at
        FROM users WHERE id = $1::uuid
        """,
        user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return ok(_full_user(row))


@router.patch("/me")
async def patch_me(
    body: UserUpdateRequest,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    changes = {
        k: v for k, v in body.model_dump(exclude_unset=True).items() if k in _UPDATABLE
    }

    if not changes:
        row = await db.fetchrow(
            """
            SELECT id, email, name, avatar_url, contact, visibility, timezone, created_at
            FROM users WHERE id = $1::uuid
            """,
            user_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return ok(_full_user(row))

    cols = list(changes.keys())
    set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(cols))
    values = [changes[col] for col in cols]

    row = await db.fetchrow(
        f"""
        UPDATE users SET {set_clause}
        WHERE id = $1::uuid
        RETURNING id, email, name, avatar_url, contact, visibility, timezone, created_at
        """,
        user_id,
        *values,
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return ok(_full_user(row))


@router.get("/me/connections")
async def list_connections(
    user_id: str = Depends(get_current_user_id), db=Depends(get_db)
):
    rows = await db.fetch(
        """
        SELECT u.id, u.name, u.avatar_url
        FROM connections c
        JOIN users u ON u.id = CASE
            WHEN c.requester_id = $1::uuid THEN c.addressee_id
            ELSE c.requester_id
        END
        WHERE c.status = 'accepted'
          AND (c.requester_id = $1::uuid OR c.addressee_id = $1::uuid)
        ORDER BY u.name
        """,
        user_id,
    )
    return ok(
        [
            {"id": str(r["id"]), "name": r["name"], "avatarUrl": r["avatar_url"]}
            for r in rows
        ]
    )


@router.get("/{user_id}")
async def get_user(
    user_id: str,
    caller_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    target_id = _valid_uuid(user_id)
    row = await db.fetchrow(
        """
        SELECT id, name, avatar_url, visibility, contact, created_at
        FROM users WHERE id = $1::uuid
        """,
        target_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    is_self = target_id == str(caller_id)
    connected = False if is_self else await _accepted_connection(db, caller_id, target_id)

    if not (is_self or row["visibility"] == "public" or connected):
        raise HTTPException(status_code=403, detail="This profile is private")

    data = {
        "id": str(row["id"]),
        "name": row["name"],
        "avatarUrl": row["avatar_url"],
        "visibility": row["visibility"],
    }
    if is_self or connected:
        data["contact"] = row["contact"]
        data["createdAt"] = row["created_at"]
    return ok(data)


@router.post("/{user_id}/connect")
async def connect(
    user_id: str,
    caller_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    target_id = _valid_uuid(user_id)
    if target_id == str(caller_id):
        raise HTTPException(status_code=400, detail="Cannot connect to yourself")

    target = await db.fetchval(
        "SELECT 1 FROM users WHERE id = $1::uuid", target_id
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    await db.execute(
        """
        INSERT INTO connections (requester_id, addressee_id, status)
        VALUES ($1::uuid, $2::uuid, 'pending')
        ON CONFLICT (requester_id, addressee_id) DO NOTHING
        """,
        caller_id,
        target_id,
    )
    status_val = await db.fetchval(
        """
        SELECT status FROM connections
        WHERE requester_id = $1::uuid AND addressee_id = $2::uuid
        """,
        caller_id,
        target_id,
    )
    return ok({"requesterId": str(caller_id), "addresseeId": target_id, "status": status_val})


@router.post("/{user_id}/accept")
async def accept(
    user_id: str,
    caller_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    requester_id = _valid_uuid(user_id)
    row = await db.fetchrow(
        """
        UPDATE connections SET status = 'accepted'
        WHERE requester_id = $1::uuid AND addressee_id = $2::uuid AND status = 'pending'
        RETURNING requester_id, addressee_id, status
        """,
        requester_id,
        caller_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="No pending request")
    return ok(
        {
            "requesterId": str(row["requester_id"]),
            "addresseeId": str(row["addressee_id"]),
            "status": row["status"],
        }
    )

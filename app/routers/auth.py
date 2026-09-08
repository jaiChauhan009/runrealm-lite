"""Auth router — register + login. Raw asyncpg, JWT bearer tokens."""
from fastapi import APIRouter, Depends, HTTPException

from app.avatar import initials_avatar_data_uri
from app.deps import get_db
from app.schemas import LoginRequest, RegisterRequest, ok
from app.security import hash_password, make_token, verify_password

router = APIRouter()


def _token_payload(user_id: str, name: str, email: str, avatar_url: str) -> dict:
    return {
        "accessToken": make_token(str(user_id)),
        "tokenType": "Bearer",
        "userId": str(user_id),
        "name": name,
        "email": email,
        "avatarUrl": avatar_url,
    }


@router.post("/register")
async def register(body: RegisterRequest, db=Depends(get_db)):
    email = body.email.lower()

    exists = await db.fetchval("SELECT 1 FROM users WHERE email = $1", email)
    if exists:
        raise HTTPException(status_code=409, detail="Email already registered")

    avatar_url = initials_avatar_data_uri(body.name)
    row = await db.fetchrow(
        """
        INSERT INTO users (email, password_hash, name, avatar_url, contact)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, name, email, avatar_url
        """,
        email,
        hash_password(body.password),
        body.name,
        avatar_url,
        body.contact,
    )

    return ok(
        _token_payload(row["id"], row["name"], row["email"], row["avatar_url"])
    )


@router.post("/login")
async def login(body: LoginRequest, db=Depends(get_db)):
    email = body.email.lower()
    row = await db.fetchrow(
        "SELECT id, name, email, avatar_url, password_hash FROM users WHERE email = $1",
        email,
    )
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return ok(
        _token_payload(row["id"], row["name"], row["email"], row["avatar_url"])
    )

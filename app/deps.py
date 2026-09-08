"""FastAPI dependencies: a pooled DB connection + the current user id."""
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import pool
from app.security import decode_token

_bearer = HTTPBearer(auto_error=True)


async def get_db():
    async with pool().acquire() as conn:
        yield conn


async def get_current_user_id(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    try:
        return decode_token(creds.credentials)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

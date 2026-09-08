"""Shared request/response models. camelCase in/out to match a JS/Kotlin client."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


def ok(data: Any = None, message: str | None = None) -> dict:
    return {"success": True, "message": message, "data": data}


class CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


# ── auth / users ─────────────────────────────────────────────
class RegisterRequest(CamelModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    contact: Optional[str] = None


class LoginRequest(CamelModel):
    email: EmailStr
    password: str


class TokenResponse(CamelModel):
    access_token: str = Field(alias="accessToken")
    token_type: str = Field(default="Bearer", alias="tokenType")
    user_id: str = Field(alias="userId")


class UserUpdateRequest(CamelModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    contact: Optional[str] = None
    avatar_url: Optional[str] = Field(default=None, alias="avatarUrl")
    visibility: Optional[Literal["public", "private"]] = None
    timezone: Optional[str] = None


# ── run sessions / points ───────────────────────────────────
class SessionStartRequest(CamelModel):
    started_at: datetime = Field(alias="startedAt")


class PointIn(CamelModel):
    lat: float
    lng: float
    speed_kmh: Optional[float] = Field(default=None, alias="speedKmh")
    bearing: Optional[float] = None
    seq: int
    recorded_at: datetime = Field(alias="recordedAt")


class PointsBatchRequest(CamelModel):
    points: list[PointIn]


class SessionFinishRequest(CamelModel):
    ended_at: datetime = Field(alias="endedAt")
    distance_m: float = Field(default=0.0, alias="distanceM")
    claim_territory: bool = Field(default=True, alias="claimTerritory")


# ── todos ───────────────────────────────────────────────────
class TodoCreateRequest(CamelModel):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)


class TodoUpdateRequest(CamelModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    status: Optional[Literal["pending", "complete", "failed"]] = None

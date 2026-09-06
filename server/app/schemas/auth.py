from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import Optional


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserResponse"


class UserResponse(BaseModel):
    id: UUID
    username: str
    email: str
    full_name: Optional[str]
    role: str
    auth_source: str = "local"
    permissions: list[str] = []
    # Visibility scope (empty = unrestricted); the UI uses this to show a
    # "filtered view" indicator, never to enforce — enforcement is server-side.
    scope_tags: list[str] = []
    last_login: Optional[datetime]

    model_config = {"from_attributes": True}

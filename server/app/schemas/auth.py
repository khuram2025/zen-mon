from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import Optional


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., min_length=1)


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
    last_login: Optional[datetime]

    model_config = {"from_attributes": True}

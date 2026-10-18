from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import Optional


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., min_length=1, max_length=1024)
    # One-step MFA for API clients; the browser uses the two-step challenge flow.
    otp: Optional[str] = Field(None, max_length=32)


class MfaLoginRequest(BaseModel):
    challenge: str = Field(..., max_length=2048)
    code: str = Field(..., min_length=1, max_length=32)


class TokenResponse(BaseModel):
    access_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: int = 0
    user: Optional["UserResponse"] = None
    # Set instead of a token when the account has multi-factor enabled and the
    # request carried no code: complete sign-in with POST /auth/login/mfa.
    mfa_required: bool = False
    challenge: Optional[str] = None


class UserResponse(BaseModel):
    id: UUID
    username: str
    email: str
    full_name: Optional[str]
    role: str
    auth_source: str = "local"
    permissions: list[str] = []
    # Short digest of role + permissions: changes whenever the account's
    # effective access changes, so an open browser can notice and refresh.
    permissions_revision: str = ""
    # Visibility scope (empty = unrestricted); the UI uses this to show a
    # "filtered view" indicator, never to enforce — enforcement is server-side.
    scope_tags: list[str] = []
    # Scope inherited from the role (roles.scope_tags); the effective scope is
    # scope_tags plus these, unless the role carries system.admin.
    role_scope_tags: list[str] = []
    last_login: Optional[datetime]
    # Personal IANA time zone (None = the appliance zone).
    timezone: Optional[str] = None
    must_change_password: bool = False
    mfa_enabled: bool = False
    mfa_required: bool = False
    session_id: Optional[str] = None

    model_config = {"from_attributes": True}


class PasswordPolicyResponse(BaseModel):
    min_length: int
    max_length: int
    rules: list[str]


class MfaEnableRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=32)


class MfaSetupRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)


class MfaReauthRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)
    code: str = Field(..., min_length=6, max_length=32)

from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field


class AlertRuleCreate(BaseModel):
    name: str = Field(..., max_length=255)
    description: Optional[str] = None
    metric: str = Field(..., pattern="^(ping_status|rtt|packet_loss|jitter|service_status)$")
    operator: str = Field(..., pattern="^(eq|neq|gt|lt|gte|lte)$")
    threshold: float
    duration: int = 0
    device_id: Optional[UUID] = None
    group_id: Optional[UUID] = None
    service_check_id: Optional[UUID] = None
    service_check_group_id: Optional[UUID] = None
    severity: str = Field(default="warning", pattern="^(info|warning|critical)$")
    cooldown: int = Field(default=300, ge=60)


class AlertRuleResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str]
    enabled: bool
    metric: str
    operator: str
    threshold: float
    duration: int
    device_id: Optional[UUID]
    group_id: Optional[UUID]
    service_check_id: Optional[UUID] = None
    service_check_group_id: Optional[UUID] = None
    severity: str
    cooldown: int
    created_at: datetime

    model_config = {"from_attributes": True}


class AlertResponse(BaseModel):
    id: UUID
    rule_id: Optional[UUID]
    device_id: Optional[UUID] = None
    device_hostname: Optional[str] = None
    device_ip: Optional[str] = None
    service_check_id: Optional[UUID] = None
    service_check_name: Optional[str] = None
    sensor_id: Optional[UUID] = None
    sensor_name: Optional[str] = None
    status: str
    severity: str
    message: str
    triggered_at: datetime
    acknowledged_at: Optional[datetime]
    resolved_at: Optional[datetime]

    model_config = {"from_attributes": True}


class AlertStats(BaseModel):
    active: int
    acknowledged: int
    resolved_today: int
    critical: int
    warning: int
    info: int

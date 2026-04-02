from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel


class MetricPoint(BaseModel):
    timestamp: datetime
    rtt_ms: Optional[float] = None
    packet_loss: Optional[float] = None
    jitter_ms: Optional[float] = None
    min_rtt_ms: Optional[float] = None
    max_rtt_ms: Optional[float] = None
    is_up: Optional[bool] = None


class MetricResponse(BaseModel):
    device_id: UUID
    granularity: str
    from_time: datetime
    to_time: datetime
    points: list[MetricPoint]


class StatusChangeEvent(BaseModel):
    device_id: UUID
    old_status: str
    new_status: str
    reason: str
    timestamp: datetime
    duration_sec: Optional[int] = None

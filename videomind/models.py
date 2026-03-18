from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


Status = Literal["pending", "done", "error"]


class CollectRequest(BaseModel):
    url: HttpUrl = Field(..., description="视频链接（B站/YouTube 等）")


class CollectResponse(BaseModel):
    id: str
    status: Status


class SummaryResponse(BaseModel):
    id: str
    original_url: str
    title: Optional[str] = None
    category: Optional[str] = None
    key_points: Optional[List[str]] = None
    summary: Optional[str] = None
    reminder_copy: Optional[str] = None
    remind_at: Optional[str] = None
    status: Status
    error_message: Optional[str] = None


class DailyDigestItem(BaseModel):
    id: str
    title: Optional[str] = None
    category: Optional[str] = None
    summary: Optional[str] = None
    reminder_copy: Optional[str] = None
    created_at: datetime


class DailyDigestResponse(BaseModel):
    date: str
    total: int
    items: List[DailyDigestItem]


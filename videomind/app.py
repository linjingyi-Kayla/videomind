from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .models import CollectRequest, CollectResponse, DailyDigestItem, DailyDigestResponse, SummaryResponse
from .storage import get_item, init_db, insert_pending, list_items_by_date_utc
from .worker import JobQueue


app = FastAPI(title="VideoMind", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

queue = JobQueue()


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    await queue.start()


@app.post("/collect", response_model=CollectResponse)
async def collect(req: CollectRequest) -> CollectResponse:
    id_ = uuid.uuid4().hex
    insert_pending({"id": id_, "original_url": str(req.url)})
    await queue.enqueue(id_)
    return CollectResponse(id=id_, status="pending")


def _loads_json(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


@app.get("/summary/{id}", response_model=SummaryResponse)
async def summary(id: str) -> SummaryResponse:
    item = get_item(id)
    if not item:
        raise HTTPException(status_code=404, detail="id 不存在")

    key_points = _loads_json(item.get("key_points_json"))
    if not isinstance(key_points, list):
        key_points = None

    return SummaryResponse(
        id=item["id"],
        original_url=item["original_url"],
        title=item.get("title"),
        category=item.get("category"),
        key_points=key_points,
        summary=item.get("summary"),
        reminder_copy=item.get("reminder_copy"),
        remind_at=item.get("remind_at"),
        status=item.get("status", "pending"),
        error_message=item.get("error_message"),
    )


@app.get("/daily-digest", response_model=DailyDigestResponse)
async def daily_digest() -> DailyDigestResponse:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = list_items_by_date_utc(today)

    items: List[DailyDigestItem] = []
    for r in rows:
        created_at = r.get("created_at") or datetime.now(timezone.utc).isoformat()
        try:
            dt = datetime.fromisoformat(created_at)
        except Exception:
            dt = datetime.now(timezone.utc)
        items.append(
            DailyDigestItem(
                id=r["id"],
                title=r.get("title"),
                category=r.get("category"),
                summary=r.get("summary"),
                reminder_copy=r.get("reminder_copy"),
                created_at=dt,
            )
        )

    return DailyDigestResponse(date=today, total=len(items), items=items)


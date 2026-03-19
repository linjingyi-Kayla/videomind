from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from sqlalchemy import desc, select

from videomind.ai_service import analyze_video
from videomind.db import init_db, new_session
from videomind.db_models import Subscription, Task
from videomind.extractor import extract_video_text
from videomind.webpush import send_web_push

load_dotenv(override=False)

app = FastAPI(title="VideoMind PWA Backend", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ROOT_MANIFEST = BASE_DIR / "manifest.json"
ROOT_SERVICE_WORKER = BASE_DIR / "service-worker.js"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class PushSubscriptionIn(BaseModel):
    endpoint: str
    expirationTime: Optional[int] = None
    keys: Dict[str, str]


class SubscribeResponse(BaseModel):
    subscription_id: str
    status: str = "ok"


class SummarizeRequest(BaseModel):
    url: HttpUrl
    # 可选：让任务绑定到某个订阅；如果不传，则绑定“最新订阅”（MVP 单用户假设）
    subscription_id: Optional[str] = None


class SummarizeResponse(BaseModel):
    task_id: str
    status: Literal["pending", "done", "error"] = "pending"


class HistoryItem(BaseModel):
    id: str
    video_url: str
    title: Optional[str] = None
    summary: Optional[str] = None
    key_points: Optional[List[str]] = None
    remind_at_hhmm: Optional[str] = None
    is_notified: bool
    status: str
    created_at: datetime


class HistoryResponse(BaseModel):
    subscription_id: Optional[str]
    items: List[HistoryItem]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time_hhmm(hhmm: str) -> time:
    hh, mm = hhmm.split(":")
    return time(hour=int(hh), minute=int(mm))


def _calc_next_remind_datetime(hhmm: str) -> datetime:
    """
    将 DeepSeek 返回的 `HH:MM` 计算为“下一次提醒时间”（UTC）。
    若已过则顺延一天。
    """
    t = _parse_time_hhmm(hhmm)
    now = _now_utc()
    dt = datetime.combine(now.date(), t, tzinfo=timezone.utc)
    if dt <= now:
        dt = dt + timedelta(days=1)
    return dt


def _get_latest_subscription_id(session) -> Optional[str]:
    sub = session.execute(select(Subscription).order_by(desc(Subscription.created_at)).limit(1)).scalars().first()
    return sub.id if sub else None


async def _process_task(task_id: str) -> None:
    """
    后台处理：抽取字幕 -> DeepSeek 总结 -> 写入 tasks 表
    """
    session = new_session()
    try:
        task = session.get(Task, task_id)
        if not task:
            return

        extracted: Dict[str, Any] = await asyncio.to_thread(extract_video_text, task.video_url)
        ai = await asyncio.to_thread(analyze_video, extracted)

        key_points_json = json.dumps(ai.key_points, ensure_ascii=False)
        summary = ai.summary

        remind_at_dt = _calc_next_remind_datetime(ai.remind_at)
        task.title = extracted.get("title")
        task.summary = summary
        task.key_points_json = key_points_json
        task.status = "done"
        task.remind_at = remind_at_dt.replace(tzinfo=None)
        task.is_notified = False
        task.error_message = None
        session.commit()
    except Exception as e:
        task = session.get(Task, task_id)
        if task:
            task.status = "error"
            task.error_message = str(e)
            session.commit()
    finally:
        session.close()


async def _send_due_tasks_loop() -> None:
    """
    每 60 秒轮询 tasks 表，到期未通知的任务发 Web Push，然后将 is_notified=true。
    """
    while True:
        await asyncio.sleep(60)

        session = new_session()
        try:
            now = _now_utc().replace(tzinfo=None)
            due = (
                session.execute(
                    select(Task)
                    .where(Task.status == "done")
                    .where(Task.is_notified == False)  # noqa: E712
                    .where(Task.remind_at != None)  # noqa: E711
                    .where(Task.remind_at <= now)
                )
                .scalars()
                .all()
            )

            if not due:
                continue

            for t in due:
                if not t.subscription_id:
                    continue
                sub = session.get(Subscription, t.subscription_id)
                if not sub:
                    continue

                subscription_obj = {
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                }

                payload = {
                    "title": "VideoMind 提醒",
                    "body": (t.summary or "")[:120] or "你的总结已就绪",
                    "task_id": t.id,
                    "url": t.video_url,
                }
                try:
                    await asyncio.to_thread(send_web_push, subscription_obj, payload)
                    t.is_notified = True
                    session.commit()
                except Exception:
                    # 发送失败不标记 is_notified，避免丢通知
                    session.rollback()
        finally:
            session.close()


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse({"ok": False, "error": "index.html not found"}, status_code=404)


@app.get("/manifest.json", include_in_schema=False)
async def manifest_json() -> FileResponse:
    # Railway 运行时的工作目录可能与本地不同；这里做兜底，确保 manifest 一定可返回
    if ROOT_MANIFEST.exists():
        return FileResponse(str(ROOT_MANIFEST), media_type="application/manifest+json")
    static_manifest = STATIC_DIR / "manifest.json"
    if static_manifest.exists():
        return FileResponse(str(static_manifest), media_type="application/manifest+json")
    # 最小兜底：避免 500
    return JSONResponse(
        {
            "name": "VideoRemind",
            "short_name": "VideoRemind",
            "start_url": "/",
            "display": "standalone",
            "icons": [],
            "share_target": {
                "action": "/api/share-target",
                "method": "POST",
                "enctype": "multipart/form-data",
                "params": {"url": "url"},
            },
        }
    )


@app.get("/service-worker.js", include_in_schema=False)
async def service_worker_js() -> FileResponse:
    if ROOT_SERVICE_WORKER.exists():
        return FileResponse(str(ROOT_SERVICE_WORKER), media_type="application/javascript")
    static_sw = STATIC_DIR.parent / "service-worker.js"
    if static_sw.exists():
        return FileResponse(str(static_sw), media_type="application/javascript")
    return JSONResponse({"ok": True})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> JSONResponse:
    # 提供一个空的 200，避免浏览器反复报 404（不影响 PWA 安装）
    return JSONResponse({"ok": True})


@app.get("/api/vapid-public-key")
async def vapid_public_key() -> JSONResponse:
    key = os.getenv("VAPID_PUBLIC_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="VAPID_PUBLIC_KEY 未配置")
    return JSONResponse({"publicKey": key})


@app.post("/api/subscribe", response_model=SubscribeResponse)
async def subscribe(req: PushSubscriptionIn) -> SubscribeResponse:
    """
    保存前端 PushSubscription（endpoint + keys）
    """
    session = new_session()
    try:
        # Endpoint 唯一：存在则更新
        existing = session.execute(select(Subscription).where(Subscription.endpoint == req.endpoint)).scalars().first()
        if existing:
            existing.p256dh = req.keys["p256dh"]
            existing.auth = req.keys["auth"]
            existing.expiration_time = (
                datetime.fromtimestamp(req.expirationTime, tz=timezone.utc).replace(tzinfo=None)
                if req.expirationTime
                else None
            )
            session.commit()
            return SubscribeResponse(subscription_id=existing.id)

        subscription_id = uuid.uuid4().hex
        sub = Subscription(
            id=subscription_id,
            endpoint=req.endpoint,
            p256dh=req.keys["p256dh"],
            auth=req.keys["auth"],
            expiration_time=(
                datetime.fromtimestamp(req.expirationTime, tz=timezone.utc).replace(tzinfo=None)
                if req.expirationTime
                else None
            ),
            created_at=_now_utc().replace(tzinfo=None),
        )
        session.add(sub)
        session.commit()
        return SubscribeResponse(subscription_id=subscription_id)
    finally:
        session.close()


@app.post("/api/summarize", response_model=SummarizeResponse)
async def summarize(req: SummarizeRequest, background: BackgroundTasks) -> SummarizeResponse:
    """
    后台处理模式：
    - 创建 tasks 记录（pending）
    - 立即返回 task_id
    - BackgroundTasks 执行抽取+DeepSeek，写入任务结果并计算 remind_at
    """
    session = new_session()
    try:
        sub_id = req.subscription_id
        if not sub_id:
            sub_id = _get_latest_subscription_id(session)

        task_id = uuid.uuid4().hex
        t = Task(
            id=task_id,
            video_url=str(req.url),
            title=None,
            summary=None,
            key_points_json=None,
            remind_at=None,
            is_notified=False,
            status="pending",
            error_message=None,
            subscription_id=sub_id,
            created_at=_now_utc().replace(tzinfo=None),
            updated_at=_now_utc().replace(tzinfo=None),
        )
        session.add(t)
        session.commit()
    finally:
        session.close()

    background.add_task(_process_task, task_id)
    return SummarizeResponse(task_id=task_id, status="pending")


@app.get("/api/history", response_model=HistoryResponse)
async def history(subscription_id: Optional[str] = None) -> HistoryResponse:
    """
    返回已总结/处理中任务列表（供 PWA 看板展示）
    """
    session = new_session()
    try:
        if not subscription_id:
            subscription_id = _get_latest_subscription_id(session)

        q = select(Task).where(Task.subscription_id == subscription_id).order_by(desc(Task.created_at)).limit(50)
        items = session.execute(q).scalars().all()

        out: List[HistoryItem] = []
        for t in items:
            key_points = None
            if t.key_points_json:
                try:
                    key_points = json.loads(t.key_points_json)
                except Exception:
                    key_points = None

            out.append(
                HistoryItem(
                    id=t.id,
                    video_url=t.video_url,
                    title=t.title,
                    summary=t.summary,
                    key_points=key_points,
                    remind_at_hhmm=t.remind_at.strftime("%H:%M") if t.remind_at else None,
                    is_notified=bool(t.is_notified),
                    status=t.status,
                    created_at=t.created_at,
                )
            )
        return HistoryResponse(subscription_id=subscription_id, items=out)
    finally:
        session.close()


class UpdateRemindAtRequest(BaseModel):
    remind_at: str  # "HH:MM"


@app.post("/api/tasks/{task_id}/remind-at", response_model=HistoryItem)
async def update_task_remind_at(task_id: str, req: UpdateRemindAtRequest) -> HistoryItem:
    """
    更新某个任务的提醒时间（HH:MM），并将 is_notified=false 以便 scheduler 再次推送。
    """
    session = new_session()
    try:
        task = session.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task_id 不存在")
        if task.subscription_id is None:
            # per_subscription 模式下，MVP 保守限制：必须先绑定订阅
            # 这里不强制，但至少确保字段存在以便前端按历史归类
            pass

        task.remind_at = _calc_next_remind_datetime(req.remind_at).replace(tzinfo=None)
        task.is_notified = False
        task.status = task.status if task.status in {"done", "error"} else "done"
        session.commit()

        key_points = None
        if task.key_points_json:
            try:
                key_points = json.loads(task.key_points_json)
            except Exception:
                key_points = None

        return HistoryItem(
            id=task.id,
            video_url=task.video_url,
            title=task.title,
            summary=task.summary,
            key_points=key_points,
            remind_at_hhmm=task.remind_at.strftime("%H:%M") if task.remind_at else None,
            is_notified=bool(task.is_notified),
            status=task.status,
            created_at=task.created_at,
        )
    finally:
        session.close()


@app.post("/api/share-target")
async def share_target(request: Request, background: BackgroundTasks) -> JSONResponse:
    """
    Web Share Target 入口：接收来自 iOS 的分享 URL，然后创建任务并在后台处理。
    """
    content_type = request.headers.get("content-type", "")
    url: Optional[str] = None
    try:
        if "application/json" in content_type:
            body = await request.json()
            url = body.get("url")
        else:
            form = await request.form()
            url = form.get("url")
    except Exception:
        url = None

    if not url:
        raise HTTPException(status_code=400, detail="缺少 url 参数")

    req = SummarizeRequest(url=url)  # type: ignore[arg-type]
    resp = await summarize(req, background=background)  # reuse logic
    return JSONResponse(resp.model_dump())


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    # 后台定时推送循环
    asyncio.create_task(_send_due_tasks_loop())


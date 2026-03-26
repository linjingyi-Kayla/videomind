from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import uuid
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, EmailStr, Field, HttpUrl
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from videomind.ai_service import analyze_video
from videomind.auth import create_access_token, get_user_by_token, hash_password, verify_password
from videomind.db import init_db, new_session
from videomind.db_models import Subscription, Task, User
from videomind.extractor import _extract_youtube_video_id, extract_video_text
from videomind.webpush import send_web_push

load_dotenv(override=False)

logger = logging.getLogger("videomind")

app = FastAPI(title="VideoMind PWA Backend", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class APINoCacheMiddleware(BaseHTTPMiddleware):
    """避免浏览器/CDN 缓存 JSON API，导致列表/详情仍显示旧任务内容。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(APINoCacheMiddleware)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ROOT_MANIFEST = BASE_DIR / "manifest.json"
ROOT_SERVICE_WORKER = BASE_DIR / "service-worker.js"

# 避免 CDN/浏览器长期缓存 HTML，导致部署后仍看到旧版主页样式与脚本
_HTML_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


def _html_response(path: Path) -> FileResponse:
    return FileResponse(str(path), headers=_HTML_NO_CACHE_HEADERS)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    u = m.group(0).strip().rstrip(").,;\"'")
    return u or None


def _resolve_shared_video_url(url: Optional[str], text: Optional[str]) -> Optional[str]:
    u = (url or "").strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    t = (text or "").strip()
    if t:
        cand = _extract_first_url(t)
        if cand and (cand.startswith("http://") or cand.startswith("https://")):
            return cand
    return None


def _youtube_id_from_url_safe(video_url: Optional[str]) -> Optional[str]:
    if not video_url:
        return None
    try:
        return _extract_youtube_video_id(video_url)
    except Exception:
        return None


def _dedupe_task_for_youtube(user_id: int, video_url: str) -> Optional[Task]:
    """
    兜底去重：避免同一用户在短时间内因前端重复触发而产生两条任务。
    只要 YouTube video_id 相同，就复用最近的 pending/done 任务。
    """
    session = new_session()
    try:
        target_id = _youtube_id_from_url_safe(video_url)
        if not target_id:
            return None

        cutoff = (datetime.utcnow() - timedelta(minutes=30)).replace(tzinfo=None)
        candidates = (
            session.execute(
                select(Task)
                .where(
                    Task.user_id == user_id,
                    Task.status.in_(["pending", "done"]),
                    Task.created_at >= cutoff,
                )
                .order_by(desc(Task.created_at))
                .limit(30)
            )
            .scalars()
            .all()
        )
        for t in candidates:
            tid = _youtube_id_from_url_safe(getattr(t, "video_url", None))
            if tid == target_id:
                return t
        return None
    finally:
        session.close()


def _try_get_user_from_request(req: Request, db: Session) -> Optional[User]:
    auth_h = req.headers.get("authorization") or ""
    token: Optional[str] = None
    if auth_h.lower().startswith("bearer "):
        token = auth_h.split(" ", 1)[1].strip()
    if not token:
        token = (req.cookies.get("vm_access_token") or "").strip() or None
    if not token:
        return None
    return get_user_by_token(db, token)


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
    category: Optional[str] = None
    summary: Optional[str] = None
    error_message: Optional[str] = None
    key_points: Optional[List[str]] = None
    remind_at_hhmm: Optional[str] = None
    # 存库为 UTC（naive），序列化为 ISO，供前端按用户本地时区显示
    remind_at_iso: Optional[str] = None
    # 已到提醒时间但仍未推送（用于前端站内 Modal 兜底）
    remind_due_pending: bool = False
    is_favorite: bool = False
    annotation: Optional[str] = None
    is_notified: bool
    status: str
    created_at: datetime


class TaskPatchRequest(BaseModel):
    is_favorite: Optional[bool] = None
    annotation: Optional[str] = None


class HistoryResponse(BaseModel):
    subscription_id: Optional[str]
    items: List[HistoryItem]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    id: int
    email: str


def get_db():
    db = new_session()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    authorization: Optional[str] = Header(None),
    vm_access_token: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    token: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token and vm_access_token:
        token = vm_access_token.strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录：缺少 Token")
    user = get_user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已过期或 Token 无效")
    return user


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _task_remind_at_iso(t: Task) -> Optional[str]:
    if not t.remind_at:
        return None
    dt = t.remind_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _history_item_from_task(t: Task) -> HistoryItem:
    key_points = None
    if t.key_points_json:
        try:
            key_points = json.loads(t.key_points_json)
        except Exception:
            key_points = None
    now_naive = _now_utc().replace(tzinfo=None)
    due_pending = bool(
        t.remind_at and t.status == "done" and not t.is_notified and t.remind_at <= now_naive
    )
    return HistoryItem(
        id=t.task_uuid,
        video_url=t.video_url,
        title=t.title,
        category=t.category,
        summary=t.summary,
        error_message=getattr(t, "error_message", None),
        key_points=key_points,
        remind_at_hhmm=t.remind_at.strftime("%H:%M") if t.remind_at else None,
        remind_at_iso=_task_remind_at_iso(t),
        remind_due_pending=due_pending,
        is_favorite=bool(getattr(t, "is_favorite", False)),
        annotation=getattr(t, "annotation", None),
        is_notified=bool(t.is_notified),
        status=t.status,
        created_at=t.created_at,
    )


def _parse_time_hhmm(hhmm: str) -> time:
    hh, mm = hhmm.split(":")
    return time(hour=int(hh), minute=int(mm))


def _calc_next_remind_datetime(hhmm: str, tz_offset_minutes: Optional[int] = None) -> datetime:
    """
    将 `HH:MM` 计算为“下一次提醒时间”的 UTC 存库值（naive UTC）。

    - tz_offset_minutes：与浏览器 `Date.getTimezoneOffset()` 一致（东八区通常为 -480）。
      传入时按用户本地墙钟解释 HH:MM。
    - 为 None 时按 UTC 墙钟解释（兼容旧行为 / 服务端无用户时区时）。
    """
    t = _parse_time_hhmm(hhmm)
    now = _now_utc()
    now_naive = now.replace(tzinfo=None)

    if tz_offset_minutes is None:
        dt = datetime.combine(now.date(), t)
        if dt <= now_naive:
            dt = dt + timedelta(days=1)
        return dt

    local_wall = now_naive - timedelta(minutes=tz_offset_minutes)
    local_date = local_wall.date()
    candidate = datetime.combine(local_date, t)
    if candidate <= local_wall:
        candidate = candidate + timedelta(days=1)
    utc_candidate = candidate + timedelta(minutes=tz_offset_minutes)
    return utc_candidate


def _fallback_title_from_summary(summary: Optional[str]) -> Optional[str]:
    """DeepSeek / RapidAPI 都无标题时，用总结首行作展示标题。"""
    if not summary or not str(summary).strip():
        return None
    line = str(summary).strip().split("\n")[0].strip()
    if len(line) > 72:
        line = line[:72].rstrip() + "…"
    return line or None


def _is_youtube_url(url: str) -> bool:
    u = (url or "").lower()
    return "youtube.com" in u or "youtu.be" in u or "youtube-nocookie.com" in u


def _extract_youtube_title_with_ytdlp(url: str) -> Optional[str]:
    """
    仅提取元信息 title，不下载媒体文件，避免生成临时文件。
    若在 Railway 被封锁/失败则返回 None。
    """
    try:
        import yt_dlp  # 按需导入，避免不需要时增加启动开销

        ydl_opts = {
            "skip_download": True,
            "quiet": True,
            "nocheckcertificate": True,
            "noplaylist": True,
            "ignoreerrors": True,
            "no_warnings": True,
            # 限制抓 title 的网络等待，避免拖慢后台任务
            "socket_timeout": 20,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if isinstance(info, dict):
            t = info.get("title")
            if t:
                return str(t).strip() or None
    except Exception:
        return None
    return None


def _friendly_push_body(t: Task) -> str:
    """温和有趣的到期提醒文案（与视频标题绑定，避免千篇一律）。"""
    title = (t.title or "这条视频").strip()
    if len(title) > 40:
        title = title[:40] + "…"
    idx = int(hashlib.md5(t.task_uuid.encode("utf-8")).hexdigest(), 16) % 4
    lines = [
        f"「{title}」在书架等你啦～点开看几分钟，给大脑加个餐 ✨",
        f"小提醒：{title} 可以复习了，趁排队/通勤刷一眼就好 📚",
        f"学习卡送达：「{title}」总结已就绪，轻松看完再滑走～",
        f"嘿，「{title}」喊你回来补课啦，点一下就能续上进度 🎧",
    ]
    return lines[idx]


def _get_latest_subscription_id(session, user_id: int) -> Optional[str]:
    """当前用户最近一次 Web Push 订阅。"""
    sub = (
        session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(desc(Subscription.created_at))
            .limit(1)
        )
        .scalars()
        .first()
    )
    return sub.id if sub else None


async def _process_task(task_uuid: str) -> None:
    """
    后台处理：抽取字幕 -> DeepSeek 总结 -> 写入 tasks 表
    """
    session = new_session()
    try:
        task = session.execute(select(Task).where(Task.task_uuid == task_uuid)).scalars().first()
        if not task:
            return

        extracted: Dict[str, Any] = await asyncio.to_thread(extract_video_text, task.video_url)
        # 与 DB 一致，避免下游只读到旧 webpage_url
        extracted["webpage_url"] = task.video_url

        # YouTube：始终并行拉 yt-dlp 标题（RapidAPI 有时无 title），再与抽取结果合并
        title_future = None
        if _is_youtube_url(task.video_url):
            title_future = asyncio.to_thread(_extract_youtube_title_with_ytdlp, task.video_url)

        extracted_title = extracted.get("title")
        if isinstance(extracted_title, str):
            extracted_title = extracted_title.strip() or None
        elif extracted_title is not None:
            extracted_title = str(extracted_title).strip() or None

        ai = await asyncio.to_thread(analyze_video, extracted)

        key_points_json = json.dumps(ai.key_points, ensure_ascii=False)
        summary = ai.summary

        _tz_env = os.getenv("VIDEOMIND_DEFAULT_TZ_OFFSET")
        _tz_ai: Optional[int] = None
        if _tz_env is not None and str(_tz_env).strip() != "":
            try:
                _tz_ai = int(str(_tz_env).strip())
            except ValueError:
                _tz_ai = None
        remind_at_dt = _calc_next_remind_datetime(ai.remind_at, _tz_ai)
        yt_title: Optional[str] = None
        if title_future:
            yt_title = await title_future
        merged = (extracted_title or yt_title or "").strip()
        if not merged:
            merged = (_fallback_title_from_summary(summary) or "").strip()
        task.title = merged if merged else "未命名视频"
        task.category = ai.category
        task.summary = summary
        task.key_points_json = key_points_json
        task.status = "done"
        task.remind_at = remind_at_dt
        task.is_notified = False
        task.error_message = None
        session.commit()
    except Exception as e:
        task = session.execute(select(Task).where(Task.task_uuid == task_uuid)).scalars().first()
        if task:
            task.status = "error"
            task.error_message = str(e)
            session.commit()
    finally:
        session.close()


async def _send_due_tasks_loop() -> None:
    """
    每 30 秒轮询 tasks 表，到期未通知的任务发 Web Push，然后将 is_notified=true。
    """
    while True:
        await asyncio.sleep(30)

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
                uid = t.user_id
                sub_id = t.subscription_id
                if sub_id:
                    sub = session.get(Subscription, sub_id)
                    if not sub:
                        logger.warning("skip push: subscription %s missing for task %s", sub_id, t.task_uuid)
                        continue
                    if uid is not None and sub.user_id is not None and sub.user_id != uid:
                        logger.warning(
                            "skip push: subscription %s user mismatch for task %s", sub_id, t.task_uuid
                        )
                        continue
                else:
                    if uid is None:
                        logger.warning("skip push: task %s has no user_id", t.task_uuid)
                        continue
                    sub_id = _get_latest_subscription_id(session, uid)
                    if not sub_id:
                        logger.warning("skip push: task %s has no subscription for user", t.task_uuid)
                        continue
                    sub = session.get(Subscription, sub_id)
                    if not sub:
                        logger.warning("skip push: subscription %s missing for task %s", sub_id, t.task_uuid)
                        continue

                subscription_obj = {
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                }

                payload = {
                    "title": "VideoMind · 到点啦",
                    "body": _friendly_push_body(t),
                    "task_id": t.task_uuid,
                    "url": t.video_url,
                }
                try:
                    await asyncio.to_thread(send_web_push, subscription_obj, payload)
                    t.is_notified = True
                    session.commit()
                except Exception as e:
                    logger.exception("web push failed for task %s: %s", t.task_uuid, e)
                    # 发送失败不标记 is_notified，避免丢通知
                    session.rollback()
        finally:
            session.close()


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return _html_response(index_path)
    return JSONResponse({"ok": False, "error": "index.html not found"}, status_code=404)


@app.get("/detail", include_in_schema=False)
async def page_detail() -> FileResponse:
    p = STATIC_DIR / "detail.html"
    if p.exists():
        return _html_response(p)
    raise HTTPException(status_code=404, detail="detail.html not found")


@app.get("/favorites", include_in_schema=False)
async def page_favorites() -> FileResponse:
    p = STATIC_DIR / "favorites.html"
    if p.exists():
        return _html_response(p)
    raise HTTPException(status_code=404, detail="favorites.html not found")


@app.get("/profile", include_in_schema=False)
async def page_profile() -> FileResponse:
    p = STATIC_DIR / "profile.html"
    if p.exists():
        return _html_response(p)
    raise HTTPException(status_code=404, detail="profile.html not found")


@app.get("/login.html", include_in_schema=False)
async def page_login() -> FileResponse:
    p = STATIC_DIR / "login.html"
    if p.exists():
        return _html_response(p)
    raise HTTPException(status_code=404, detail="login.html not found")


@app.get("/register.html", include_in_schema=False)
async def page_register() -> FileResponse:
    p = STATIC_DIR / "register.html"
    if p.exists():
        return _html_response(p)
    raise HTTPException(status_code=404, detail="register.html not found")


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
                "method": "GET",
                "params": {"title": "title", "text": "text", "url": "url"},
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


@app.post("/api/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> JSONResponse:
    email = str(body.email).lower().strip()
    exists = db.execute(select(User).where(User.email == email)).scalars().first()
    if exists:
        raise HTTPException(status_code=400, detail="该邮箱已注册")
    u = User(
        email=email,
        hashed_password=hash_password(body.password),
        share_token=secrets.token_hex(16),
    )
    db.add(u)
    db.commit()
    return JSONResponse({"ok": True, "message": "注册成功"})


@app.post("/api/login", response_model=TokenResponse)
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)) -> TokenResponse:
    email = str(body.email).lower().strip()
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    token = create_access_token(user.id)
    # 用 HttpOnly Cookie 承载登录态，便于 iOS 系统分享（GET share_target）自动携带
    response.set_cookie(
        key="vm_access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=bool(os.getenv("COOKIE_SECURE", "").strip() == "1"),
        path="/",
        max_age=60 * 60 * 24 * 14,
    )
    return TokenResponse(access_token=token)


@app.get("/api/me", response_model=MeResponse)
def me(current_user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(id=current_user.id, email=current_user.email)


@app.post("/api/logout")
def logout(response: Response) -> JSONResponse:
    response.delete_cookie("vm_access_token", path="/")
    return JSONResponse({"ok": True})


@app.post("/api/subscribe", response_model=SubscribeResponse)
async def subscribe(
    req: PushSubscriptionIn,
    current_user: User = Depends(get_current_user),
) -> SubscribeResponse:
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
            existing.user_id = current_user.id
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
            user_id=current_user.id,
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


def _summarize_for_user(user_id: int, req: SummarizeRequest, background: BackgroundTasks) -> SummarizeResponse:
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
            sub_id = _get_latest_subscription_id(session, user_id)

        task_uuid = uuid.uuid4().hex
        t = Task(
            task_uuid=task_uuid,
            video_url=str(req.url),
            title=None,
            summary=None,
            key_points_json=None,
            remind_at=None,
            is_notified=False,
            status="pending",
            error_message=None,
            subscription_id=sub_id,
            user_id=user_id,
            created_at=_now_utc().replace(tzinfo=None),
            updated_at=_now_utc().replace(tzinfo=None),
        )
        session.add(t)
        session.commit()
    finally:
        session.close()

    background.add_task(_process_task, task_uuid)
    return SummarizeResponse(task_id=task_uuid, status="pending")


@app.post("/api/summarize", response_model=SummarizeResponse)
async def summarize(
    req: SummarizeRequest,
    background: BackgroundTasks,
    current_user: User = Depends(get_current_user),
) -> SummarizeResponse:
    return _summarize_for_user(current_user.id, req, background)


@app.get("/api/history", response_model=HistoryResponse)
async def history(
    subscription_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
) -> HistoryResponse:
    """
    返回当前登录用户的最近任务（最多 50 条）。
    """
    session = new_session()
    try:
        target_sid = subscription_id
        if not target_sid:
            target_sid = _get_latest_subscription_id(session, current_user.id)

        items = (
            session.execute(
                select(Task)
                .where(Task.user_id == current_user.id)
                .order_by(desc(Task.created_at))
                .limit(50)
            )
            .scalars()
            .all()
        )

        out: List[HistoryItem] = []
        for t in items:
            out.append(_history_item_from_task(t))
        return HistoryResponse(subscription_id=target_sid, items=out)
    finally:
        session.close()


class UpdateRemindAtRequest(BaseModel):
    remind_at: str  # "HH:MM"
    # 与 JS Date.getTimezoneOffset() 一致；不传则按 UTC 墙钟解释（旧行为）
    tz_offset_minutes: Optional[int] = None


@app.post("/api/tasks/{task_id}/remind-at", response_model=HistoryItem)
async def update_task_remind_at(
    task_id: str,
    req: UpdateRemindAtRequest,
    current_user: User = Depends(get_current_user),
) -> HistoryItem:
    """
    更新某个任务的提醒时间（HH:MM），并将 is_notified=false 以便 scheduler 再次推送。
    """
    session = new_session()
    try:
        task = (
            session.execute(
                select(Task).where(Task.task_uuid == task_id, Task.user_id == current_user.id)
            )
            .scalars()
            .first()
        )
        if not task:
            raise HTTPException(status_code=404, detail="task_id 不存在")
        if task.subscription_id is None:
            # per_subscription 模式下，MVP 保守限制：必须先绑定订阅
            # 这里不强制，但至少确保字段存在以便前端按历史归类
            pass

        task.remind_at = _calc_next_remind_datetime(req.remind_at, req.tz_offset_minutes)
        task.is_notified = False
        task.status = task.status if task.status in {"done", "error"} else "done"
        session.commit()

        session.refresh(task)
        return _history_item_from_task(task)
    finally:
        session.close()


@app.get("/api/tasks/{task_id}", response_model=HistoryItem)
async def get_task(task_id: str, current_user: User = Depends(get_current_user)) -> HistoryItem:
    session = new_session()
    try:
        task = (
            session.execute(
                select(Task).where(Task.task_uuid == task_id, Task.user_id == current_user.id)
            )
            .scalars()
            .first()
        )
        if not task:
            raise HTTPException(status_code=404, detail="task_id 不存在")
        return _history_item_from_task(task)
    finally:
        session.close()


@app.patch("/api/tasks/{task_id}", response_model=HistoryItem)
async def patch_task(
    task_id: str,
    req: TaskPatchRequest,
    current_user: User = Depends(get_current_user),
) -> HistoryItem:
    """更新收藏、批注等字段。"""
    session = new_session()
    try:
        task = (
            session.execute(
                select(Task).where(Task.task_uuid == task_id, Task.user_id == current_user.id)
            )
            .scalars()
            .first()
        )
        if not task:
            raise HTTPException(status_code=404, detail="task_id 不存在")
        if req.is_favorite is not None:
            task.is_favorite = bool(req.is_favorite)
        if req.annotation is not None:
            task.annotation = req.annotation
        task.updated_at = _now_utc().replace(tzinfo=None)
        session.commit()
        session.refresh(task)
        return _history_item_from_task(task)
    finally:
        session.close()


@app.post("/api/share-target")
async def share_target(request: Request, background: BackgroundTasks) -> JSONResponse:
    """
    Web Share Target / 快捷指令：接收分享的 YouTube URL。

    鉴权（任选其一）：
    - Header: Authorization: Bearer <JWT>
    - 查询参数或表单: access_token=<JWT>
    - 查询参数或表单: share_token=<我的页展示的分享密钥>（快捷指令无法带 JWT 时用此方式）
    """
    qp = request.query_params
    content_type = request.headers.get("content-type", "")
    url: Optional[str] = None
    jwt_token: Optional[str] = None
    share_key: Optional[str] = None

    auth_h = request.headers.get("authorization")
    if auth_h and auth_h.lower().startswith("bearer "):
        jwt_token = auth_h.split(" ", 1)[1].strip()
    jwt_token = jwt_token or qp.get("access_token")
    share_key = qp.get("share_token")

    try:
        if "application/json" in content_type:
            body = await request.json()
            url = body.get("url")
            jwt_token = jwt_token or body.get("access_token")
            share_key = share_key or body.get("share_token")
        else:
            form = await request.form()
            url = form.get("url")
            jwt_token = jwt_token or form.get("access_token")
            share_key = share_key or form.get("share_token")
    except Exception:
        url = None

    if not url:
        raise HTTPException(status_code=400, detail="缺少 url 参数")

    session = new_session()
    try:
        user: Optional[User] = None
        if jwt_token:
            user = get_user_by_token(session, jwt_token)
        if not user and share_key:
            user = session.execute(select(User).where(User.share_token == share_key)).scalars().first()
        if not user:
            raise HTTPException(
                status_code=401,
                detail="需要鉴权：请使用「我的」中的分享密钥 share_token，或传入 access_token / Authorization",
            )
    finally:
        session.close()

    existing = _dedupe_task_for_youtube(user.id, url)  # type: ignore[arg-type]
    if existing:
        return JSONResponse({"task_id": existing.task_uuid, "status": existing.status})

    req = SummarizeRequest(url=url)  # type: ignore[arg-type]
    resp = _summarize_for_user(user.id, req, background)
    return JSONResponse(resp.model_dump())


@app.get("/api/share-target", include_in_schema=False)
async def share_target_get(
    request: Request,
    background: BackgroundTasks,
    title: Optional[str] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
) -> RedirectResponse:
    """
    Web Share Target GET 入口（iOS 系统分享菜单会以 query params 传入 title/text/url）。

    - 若已登录（Cookie 或 Authorization）：后台创建任务并 Redirect 回主页 `/?share_task_id=...`
    - 若未登录：Redirect 到 `/login.html?next=<当前分享 URL>`，登录成功后再回到此接口继续创建任务
    """
    video_url = _resolve_shared_video_url(url, text)
    if not video_url:
        return RedirectResponse(url="/?share_error=missing_url", status_code=302)

    db = new_session()
    try:
        user = _try_get_user_from_request(request, db)
    finally:
        db.close()

    if not user:
        next_url = str(request.url)
        # next 作为 query 参数值只需要编码一次；过度编码会导致登录页无法解析完整 URL
        return RedirectResponse(
            url="/login.html?next=" + quote(next_url, safe="/:?&=%#[]@!$'()*+,;="),
            status_code=302,
        )

    # 去重：避免同一用户同一 YouTube 视频在短时间内触发两条任务
    existing = _dedupe_task_for_youtube(user.id, video_url)
    if existing:
        return RedirectResponse(url="/?share_task_id=" + quote(existing.task_uuid), status_code=302)

    req = SummarizeRequest(url=video_url)  # type: ignore[arg-type]
    resp = _summarize_for_user(user.id, req, background)
    return RedirectResponse(url="/?share_task_id=" + quote(resp.task_id), status_code=302)


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    # 后台定时推送循环
    asyncio.create_task(_send_due_tasks_loop())


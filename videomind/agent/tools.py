from __future__ import annotations

import json
import logging
import time
from datetime import timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from videomind.agent.state import ToolContext
from videomind.agent.transcript import search_cues
from videomind.agent.web_search import web_search as run_web_search
from videomind.db import new_session
from videomind.db_models import Task
from videomind.remind import parse_remind_at

logger = logging.getLogger("videomind.agent")

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_video_summary",
            "description": (
                "获取当前视频已生成的标题、分类、总结、要点与链接。"
                "适合宏观问题：讲了什么、核心观点、值不值得看、快速回顾。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "当前视频任务 id（可省略，后端会绑定当前详情页任务）",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_transcript",
            "description": (
                "在本视频带时间戳字幕中检索相关片段，返回带前后文与 [mm:ss-mm:ss] 的证据。"
                "问视频怎么说、作者观点、某段含义、时间点、为什么视频里提到某事时必须使用。"
                "不要用它获取最新外部新闻。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "query": {
                        "type": "string",
                        "description": "检索关键词或短句，如 OpenAI Stargate、AMD 权证",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回条数，默认 5，最大 8",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "检索公开网页的标题、摘要与链接。仅用于事实核查、用户要最新情况、"
                "或明确需要视频之外的信息。不要把结果说成视频内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索词，建议具体，如 OpenAI Stargate project status 2026",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "把内容追加写入当前视频的用户批注/笔记。仅在用户要求记下来时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "content": {"type": "string", "description": "要保存的笔记正文"},
                    "source_timestamp": {
                        "type": "string",
                        "description": "可选，字幕时间如 14:00-14:44",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "为当前视频设置复习提醒，复用现有 Web Push / 站内提醒调度。"
                "仅在用户要求提醒时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "remind_at": {
                        "type": "string",
                        "description": "HH:MM、今晚、明晚、明天 20:00 或 ISO 时间",
                    },
                    "content": {
                        "type": "string",
                        "description": "可选，提醒事由，会写入笔记一行",
                    },
                    "source_timestamp": {
                        "type": "string",
                        "description": "可选，对应视频时间戳",
                    },
                },
                "required": ["remind_at"],
            },
        },
    },
]


def _ok(**payload: Any) -> Dict[str, Any]:
    out = {"success": True}
    out.update(payload)
    return out


def _err(code: str, **payload: Any) -> Dict[str, Any]:
    out = {"success": False, "error": code}
    out.update(payload)
    return out


def _bind_task_id(ctx: ToolContext, args: Dict[str, Any]) -> Optional[str]:
    raw = str(args.get("task_id") or "").strip()
    if raw and raw != ctx.task_id:
        return None
    return ctx.task_id


def _load_owned_task(session: Session, ctx: ToolContext, task_id: str) -> Optional[Task]:
    return (
        session.execute(select(Task).where(Task.task_uuid == task_id, Task.user_id == ctx.user_id))
        .scalars()
        .first()
    )


def _with_session(ctx: ToolContext):
    if ctx.session is not None:
        return ctx.session, False
    return new_session(), True


def get_video_summary(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = _bind_task_id(ctx, args)
    if not task_id:
        return _err("task_id_mismatch")
    session, own = _with_session(ctx)
    try:
        task = _load_owned_task(session, ctx, task_id)
        if not task:
            return _err("task_not_found")
        key_points = []
        if task.key_points_json:
            try:
                parsed = json.loads(task.key_points_json)
                if isinstance(parsed, list):
                    key_points = [str(x) for x in parsed]
            except Exception:
                key_points = []
        title = (task.title or "").strip() or "未命名视频"
        reminder_copy = f"复习「{title[:32]}」" if title else ""
        return _ok(
            task_id=task.task_uuid,
            title=title,
            category=task.category,
            summary=task.summary,
            key_points=key_points,
            reminder_copy=reminder_copy,
            video_url=task.video_url,
            remind_at_hhmm=task.remind_at.strftime("%H:%M") if task.remind_at else None,
            annotation=task.annotation,
            has_subtitles=bool((task.subtitles_text or "").strip())
            and "暂无可用字幕" not in (task.subtitles_text or "")[:40],
        )
    finally:
        if own:
            session.close()


def search_transcript(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = _bind_task_id(ctx, args)
    if not task_id:
        return _err("task_id_mismatch")
    query = str(args.get("query") or "").strip()
    if not query:
        return _err("empty_query", results=[])
    top_k = args.get("top_k") or 5
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 5

    session, own = _with_session(ctx)
    try:
        task = _load_owned_task(session, ctx, task_id)
        if not task:
            return _err("task_not_found", results=[])
        payload = search_cues(task.subtitles_text, query, top_k=top_k)
        payload["task_id"] = task_id
        return payload
    finally:
        if own:
            session.close()


def web_search(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip()
    return run_web_search(query, limit=5)


def save_note(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = _bind_task_id(ctx, args)
    if not task_id:
        return _err("task_id_mismatch")
    content = str(args.get("content") or "").strip()
    if not content:
        return _err("empty_content")
    ts = str(args.get("source_timestamp") or "").strip()
    line = f"[{ts}] {content}" if ts else content

    session, own = _with_session(ctx)
    try:
        task = _load_owned_task(session, ctx, task_id)
        if not task:
            return _err("task_not_found")
        existing = (task.annotation or "").strip()
        if line in existing:
            return _ok(task_id=task_id, annotation=existing, appended=False, note=line)
        task.annotation = f"{existing}\n{line}".strip() if existing else line
        session.commit()
        session.refresh(task)
        return _ok(task_id=task_id, annotation=task.annotation, appended=True, note=line)
    except Exception as e:
        session.rollback()
        return _err("save_note_failed", detail=str(e))
    finally:
        if own:
            session.close()


def set_reminder(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = _bind_task_id(ctx, args)
    if not task_id:
        return _err("task_id_mismatch")
    raw_when = str(args.get("remind_at") or "").strip()
    if not raw_when:
        return _err("empty_remind_at")
    try:
        remind_dt = parse_remind_at(raw_when, ctx.tz_offset_minutes)
    except Exception:
        return _err("invalid_remind_at", remind_at=raw_when)

    content = str(args.get("content") or "").strip()
    ts = str(args.get("source_timestamp") or "").strip()

    session, own = _with_session(ctx)
    try:
        task = _load_owned_task(session, ctx, task_id)
        if not task:
            return _err("task_not_found")
        task.remind_at = remind_dt
        task.is_notified = False
        if content:
            stamp = remind_dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            extra = f"[提醒 {stamp}] {content}"
            if ts:
                extra += f" [{ts}]"
            existing = (task.annotation or "").strip()
            if extra not in existing:
                task.annotation = f"{existing}\n{extra}".strip() if existing else extra
        session.commit()
        session.refresh(task)
        iso = remind_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return _ok(
            task_id=task_id,
            remind_at=iso,
            remind_at_hhmm=remind_dt.strftime("%H:%M"),
            content=content or None,
            source_timestamp=ts or None,
        )
    except Exception as e:
        session.rollback()
        return _err("set_reminder_failed", detail=str(e))
    finally:
        if own:
            session.close()


TOOL_HANDLERS: Dict[str, Callable[[ToolContext, Dict[str, Any]], Dict[str, Any]]] = {
    "get_video_summary": get_video_summary,
    "search_transcript": search_transcript,
    "web_search": web_search,
    "save_note": save_note,
    "set_reminder": set_reminder,
}


def execute_tool(ctx: ToolContext, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return _err("unknown_tool", tool=name)
    t0 = time.perf_counter()
    try:
        result = handler(ctx, arguments or {})
    except Exception as e:
        result = _err("tool_exception", tool=name, detail=str(e))
    latency_ms = int((time.perf_counter() - t0) * 1000)
    safe_args = {k: v for k, v in (arguments or {}).items() if k not in {"api_key", "token", "password"}}
    logger.info(
        "agent_trace request_id=%s user_id=%s task_id=%s tool=%s success=%s latency_ms=%s args=%s error=%s",
        ctx.request_id,
        ctx.user_id,
        ctx.task_id,
        name,
        bool(result.get("success")),
        latency_ms,
        json.dumps(safe_args, ensure_ascii=False)[:500],
        result.get("error"),
    )
    result = dict(result)
    result["_meta"] = {"tool": name, "latency_ms": latency_ms}
    return result

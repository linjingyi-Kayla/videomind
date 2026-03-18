from __future__ import annotations

import asyncio
import traceback
from typing import Optional

from .ai_service import analyze_video
from .extractor import dumps_json, extract_video_text
from .storage import get_item, update_item


class JobQueue:
    """
    MVP：进程内队列 + 单 worker。
    优点：简单可用；缺点：重启丢队列（但 DB 里仍是 pending，可二次触发处理）。
    """

    def __init__(self) -> None:
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._worker_task and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(self._run())

    async def enqueue(self, id_: str) -> None:
        await self._q.put(id_)

    async def _run(self) -> None:
        while True:
            id_ = await self._q.get()
            try:
                await asyncio.to_thread(self._process_one, id_)
            finally:
                self._q.task_done()

    def _process_one(self, id_: str) -> None:
        item = get_item(id_)
        if not item:
            return
        try:
            extracted = extract_video_text(item["original_url"])
            update_item(
                id_,
                {
                    "title": extracted.get("title"),
                    "description": extracted.get("description"),
                    "tags_json": dumps_json(extracted.get("tags") or []),
                    "subtitles_json": dumps_json(
                        {
                            "meta": extracted.get("subtitles_meta") or {},
                            "text": extracted.get("subtitles_text") or "",
                        }
                    ),
                },
            )

            ai = analyze_video(extracted)
            update_item(
                id_,
                {
                    "category": ai.category,
                    "key_points_json": dumps_json(ai.key_points),
                    "summary": ai.summary,
                    "reminder_copy": ai.reminder_copy,
                    "remind_at": ai.remind_at,
                    "status": "done",
                    "error_message": None,
                },
            )
        except Exception:
            update_item(
                id_,
                {
                    "status": "error",
                    "error_message": traceback.format_exc(limit=6),
                },
            )


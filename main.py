from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

from videomind.ai_service import analyze_video
from videomind.extractor import extract_video_text

# 启动时加载 .env（包含 DEEPSEEK_API_KEY）
load_dotenv(override=False)

app = FastAPI(title="VideoMind API", version="0.1.0")


class SummarizeRequest(BaseModel):
    url: HttpUrl


class SummarizeResponse(BaseModel):
    url: str
    title: Optional[str] = None
    category: str
    key_points: List[str]
    summary: str
    reminder_copy: str
    remind_at: str


@app.post("/api/summarize", response_model=SummarizeResponse)
async def summarize(req: SummarizeRequest) -> SummarizeResponse:
    """
    输入：{"url": "..."}
    输出：DeepSeek 总结后的 JSON（分类/要点/总结/提醒文案/提醒时间）
    """
    url = str(req.url)
    try:
        extracted: Dict[str, Any] = await asyncio.to_thread(extract_video_text, url)
        ai = await asyncio.to_thread(analyze_video, extracted)
        return SummarizeResponse(
            url=url,
            title=extracted.get("title"),
            category=ai.category,
            key_points=ai.key_points,
            summary=ai.summary,
            reminder_copy=ai.reminder_copy,
            remind_at=ai.remind_at,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# 运行方式：
# 1) 安装依赖：python -m pip install -r requirements.txt
# 2) 准备 .env：把 DEEPSEEK_API_KEY 写进去
# 3) 启动：uvicorn main:app --reload --port 8000


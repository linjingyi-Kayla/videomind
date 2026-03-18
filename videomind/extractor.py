from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import YouTubeTranscriptApi

def _extract_youtube_video_id(url: str) -> str:
    u = urlparse(url)
    host = (u.netloc or "").lower()
    path = (u.path or "").strip("/")

    # youtu.be/<id>
    if "youtu.be" in host:
        if not path:
            raise ValueError("无法从 youtu.be URL 解析出 video id")
        return path.split("/")[0]

    # youtube.com/watch?v=<id>
    if "youtube.com" in host or "youtube-nocookie.com" in host or "googlevideo.com" in host:
        qs = parse_qs(u.query or "")
        if qs.get("v"):
            return qs["v"][0]

        # youtube.com/shorts/<id> or youtube.com/live/<id>
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in {"shorts", "live"}:
            return parts[1]

    raise ValueError("无法从 URL 解析出 YouTube video id")


def _format_ts(seconds: float) -> str:
    # 输出 mm:ss，便于总结时快速“抓住段落位置”
    s = int(seconds)
    m = s // 60
    sec = s % 60
    return f"{m:02d}:{sec:02d}"


def _extract_youtube_transcript_text(url: str) -> Dict[str, Any]:
    video_id = _extract_youtube_video_id(url)

    # 优先中文，再到英文
    languages = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "en", "en-US"]
    last_err: Optional[Exception] = None
    transcript = None
    used_lang = languages[0]

    api = YouTubeTranscriptApi()
    for lang in languages:
        try:
            # 返回可迭代的 FetchedTranscript（snippet: text/start/duration）
            transcript = api.fetch(video_id, languages=[lang], preserve_formatting=False)
            used_lang = lang
            break
        except Exception as e:
            last_err = e

    if transcript is None or len(transcript) == 0:
        raise RuntimeError(f"YouTube 无可用字幕：{last_err}")

    lines: List[str] = []
    for seg in transcript:
        start = float(getattr(seg, "start", 0.0) or 0.0)
        text = str(getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        lines.append(f"[{_format_ts(start)}] {text}")

    # 不获取 title/description/tags：youtube-transcript-api 专注字幕抽取
    return {
        "title": None,
        "description": None,
        "tags": [],
        "subtitles_text": "\n".join(lines).strip(),
        "subtitles_meta": {
            "lang": used_lang,
            "source": "youtube_transcript_api",
            "has_timestamps": True,
        },
        "extractor_key": "YoutubeTranscriptAPI",
        "webpage_url": url,
    }


def extract_video_text(url: str) -> Dict[str, Any]:
    """
    仅提取：title / description / tags / subtitles(优先自动字幕)。
    为提高 B 站成功率，显式设置 UA、打开自动字幕提取等开关。
    """
    # YouTube：优先用 youtube-transcript-api 直接拉“字幕文本”，避免 yt-dlp 的 format 选择/反爬封锁
    host = (urlparse(url).netloc or "").lower()
    if "youtube.com" in host or "youtu.be" in host or "youtube-nocookie.com" in host:
        return _extract_youtube_transcript_text(url)
    raise ValueError("当前仅支持 YouTube 链接（字幕优先模式）。")


def dumps_json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

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


def _to_seconds(v: Any) -> Optional[float]:
    """
    尽量把字幕 segment 里的时间字段规范为“秒（float）”。
    兼容：秒 / 毫秒 / "00:12" / "00:00:12"。
    """
    if v is None:
        return None

    # 数值：通常是秒或毫秒
    if isinstance(v, (int, float)):
        fv = float(v)
        if fv > 1000:
            # 很可能是毫秒
            return fv / 1000.0
        return fv

    # 字符串：尝试解析 "mm:ss" 或 "hh:mm:ss"
    s = str(v).strip()
    if not s:
        return None

    # "12.3" 这种
    try:
        fv = float(s)
        return fv / 1000.0 if fv > 1000 else fv
    except Exception:
        pass

    # "00:12" 或 "00:00:12"
    m = re.match(r"^(\\d{1,2}):(\\d{2})(?::(\\d{2}))?$", s)
    if m:
        a = int(m.group(1))
        b = int(m.group(2))
        c = m.group(3)
        if c is None:
            # mm:ss
            return a * 60 + b
        # hh:mm:ss
        hh = a
        return hh * 3600 + b * 60 + int(c)

    return None


def _extract_youtube_transcript_text(url: str) -> Dict[str, Any]:
    """
    通过 RapidAPI 的 youtube-transcript 获取字幕（带时间戳），
    完全绕开本地下载，避免 Railway IP 被封。
    """
    video_id = _extract_youtube_video_id(url)

    rapid_api_key = os.getenv("RAPIDAPI_KEY")
    if not rapid_api_key:
        raise RuntimeError("缺少 RAPIDAPI_KEY（请在环境变量中配置）")

    endpoint = os.getenv(
        "RAPIDAPI_ENDPOINT",
        "https://youtube-transcript3.p.rapidapi.com/api/transcript",
    )
    host = os.getenv("RAPIDAPI_HOST", "youtube-transcript3.p.rapidapi.com")
    headers = {
        "X-RapidAPI-Key": rapid_api_key,
        "X-RapidAPI-Host": host,
        # 降低代理/CDN 返回「上一次请求」字幕的概率
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    # 先用 video_id 拉取；部分服务端字段名可能略有不同，这里做轻量兜底
    params_list = [
        {"video_id": video_id},
        {"videoId": video_id},
        {"id": video_id},
        {"url": url},
    ]
    last_resp_text: str = ""
    had_success_200 = False
    had_empty_transcript = False
    last_title: Optional[str] = None
    last_description: Optional[str] = None
    last_tags: List[str] = []

    for params in params_list:
        try:
            resp = requests.get(endpoint, headers=headers, params=params, timeout=60)
            last_resp_text = resp.text[:2000]
        except requests.Timeout as e:
            raise RuntimeError("字幕服务超时，请稍后重试。") from e
        except Exception as e:
            raise RuntimeError(f"字幕服务请求失败：{e}") from e

        if resp.status_code == 429:
            raise RuntimeError("字幕服务超额限制（429），请稍后再试。")
        if resp.status_code >= 500:
            raise RuntimeError(f"字幕服务服务器错误（HTTP {resp.status_code}），请稍后再试。")
        if resp.status_code < 200 or resp.status_code >= 300:
            # 其他 4xx：继续尝试下一个 params（例如换 url）
            continue
        had_success_200 = True

        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"字幕服务返回非 JSON：{last_resp_text}") from e

        # 根据你的描述：返回 JSON 中的 transcript 字段是一个数组
        # 同时尽量解析 title/description/tags（不同 RapidAPI 服务字段名可能略有不同）
        title = None
        description = None
        tags: List[str] = []
        if isinstance(data, dict):
            title = data.get("title") or data.get("video_title") or data.get("videoTitle")
            description = (
                data.get("description") or data.get("video_description") or data.get("videoDescription")
            )

            raw_tags = data.get("tags") or data.get("keywords") or data.get("tag")
            if isinstance(raw_tags, list):
                tags = [str(x).strip() for x in raw_tags if str(x).strip()][:20]
            elif isinstance(raw_tags, str):
                tags = [t.strip() for t in re.split(r"[,\n]", raw_tags) if t.strip()][:20]

            # 嵌套字段兜底
            if not title:
                meta = data.get("meta") or data.get("metadata") or {}
                if isinstance(meta, dict):
                    title = meta.get("title") or meta.get("video_title")

        if title is not None:
            title = str(title).strip() or None
        if description is not None:
            description = str(description).strip() or None

        last_title = title
        last_description = description
        last_tags = tags

        transcript: Any = None
        if isinstance(data, dict):
            transcript = data.get("transcript")
            if transcript is None and isinstance(data.get("data"), dict):
                transcript = data["data"].get("transcript")
        else:
            transcript = data

        # transcript 为空：别立刻返回，继续尝试下一个参数名
        if not transcript:
            had_empty_transcript = True
            continue

        # transcript 通常是 list[dict]，每个 dict 里至少包含 text 和 offset
        lines: List[str] = []
        if isinstance(transcript, list):
            for item in transcript:
                if isinstance(item, str):
                    t = item.strip()
                    if t:
                        lines.append(t)
                    continue
                if not isinstance(item, dict):
                    continue

                text = (
                    item.get("text")
                    or item.get("sentence")
                    or item.get("caption")
                    or item.get("value")
                    or item.get("transcript")
                    or ""
                )
                text = str(text).strip()
                if not text:
                    continue

                # 你的要求：offset -> [mm:ss]
                offset = item.get("offset")
                if offset is None:
                    offset = (
                        item.get("start")
                        or item.get("timestamp")
                        or item.get("time")
                        or item.get("startMs")
                        or item.get("start_ms")
                    )

                sec = _to_seconds(offset)
                if sec is None:
                    lines.append(text)
                else:
                    lines.append(f"[{_format_ts(sec)}] {text}")
        else:
            # 非 list 兜底：如果 data 里有纯文本字段
            maybe_text = None
            if isinstance(data, dict):
                for k in ("text", "transcript", "caption"):
                    if k in data:
                        maybe_text = data[k]
                        break
            if maybe_text:
                mt = str(maybe_text).strip()
                return {
                    "title": title,
                    "description": description,
                    "tags": tags,
                    "subtitles_text": mt,
                    "subtitles_meta": {"source": "rapidapi", "has_timestamps": False},
                    "extractor_key": "RapidAPI:youtube-transcript3",
                    "webpage_url": url,
                }

        if not lines:
            had_empty_transcript = True
            continue

        return {
            "title": title,
            "description": description,
            "tags": tags,
            "subtitles_text": "\n".join(lines).strip(),
            "subtitles_meta": {
                "source": "rapidapi",
                "has_timestamps": any(line.startswith("[") for line in lines),
            },
            "extractor_key": "RapidAPI:youtube-transcript3",
            "webpage_url": url,
        }

    # 全部尝试都拿不到字幕：返回“暂无可用字幕”
    if had_success_200 and had_empty_transcript:
        return {
            "title": last_title,
            "description": last_description,
            "tags": last_tags,
            "subtitles_text": "该视频暂无可用字幕",
            "subtitles_meta": {"source": "rapidapi", "has_timestamps": False},
            "extractor_key": "RapidAPI:youtube-transcript3",
            "webpage_url": url,
        }

    # 完全失败：返回错误
    raise RuntimeError(f"字幕服务请求失败（RapidAPI）。响应：{last_resp_text}")


def extract_video_text(url: str) -> Dict[str, Any]:
    """
    仅提取：title / description / tags / subtitles(优先自动字幕)。
    生产模式：优先走 RapidAPI 字幕服务（无本地下载、无 yt-dlp / ffmpeg）。
    """
    # YouTube：优先走 RapidAPI 获取“带时间戳”的字幕文本
    host = (urlparse(url).netloc or "").lower()
    if "youtube.com" in host or "youtu.be" in host or "youtube-nocookie.com" in host:
        return _extract_youtube_transcript_text(url)
    raise ValueError("当前仅支持 YouTube 链接（字幕优先模式）。")


def dumps_json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


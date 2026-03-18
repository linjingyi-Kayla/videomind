from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from youtube_transcript_api import YouTubeTranscriptApi


def _pick_lang_track(subs_dict: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    从 subtitles / automatic_captions 里挑一个最可能可用的字幕轨道。
    优先中文，其次英文，最后任意可用。
    """
    if not isinstance(subs_dict, dict) or not subs_dict:
        return None

    preferred = [
        "zh-Hans",
        "zh-CN",
        "zh",
        "zh-Hant",
        "zh-TW",
        "en",
        "en-US",
    ]
    for lang in preferred:
        if lang in subs_dict and subs_dict[lang]:
            return lang, subs_dict[lang]

    # B站常见的 danmaku（弹幕）不是“字幕”，对总结反而是噪声，优先跳过
    if "danmaku" in subs_dict:
        subs_dict = {k: v for k, v in subs_dict.items() if k != "danmaku"}

    # 兜底：挑第一个
    for lang, tracks in subs_dict.items():
        if tracks:
            return str(lang), tracks
    return None


def _download_subtitle_text(url: str, fmt_url: str) -> str:
    # 用 yt-dlp 自带 downloader 能更稳，这里走最朴素的 HTTP 也可以，
    # 但为减少依赖，直接让 yt-dlp 来下载字幕文件到内存不太方便。
    # 这里利用 yt-dlp 的 extract_info 已经给到 "url"，我们用 requests 会更直接。
    import requests

    # B 站等站点经常对字幕/弹幕接口做 412 校验：必须带 Referer / UA
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": url,
    }
    resp = requests.get(fmt_url, headers=headers, timeout=30)
    resp.raise_for_status()
    text = resp.text

    # 常见格式：vtt / srt / json3。这里不做重度解析，先尽量提取纯文本。
    # - vtt：去掉时间戳与空行
    # - srt：去掉序号与时间戳
    if "WEBVTT" in text[:20]:
        lines = []
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ln.startswith("WEBVTT"):
                continue
            if "-->" in ln:
                continue
            if ln.startswith("NOTE"):
                continue
            lines.append(ln)
        return "\n".join(lines).strip()

    # srt 简单过滤
    if "-->" in text:
        lines = []
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ln.isdigit():
                continue
            if "-->" in ln:
                continue
            lines.append(ln)
        return "\n".join(lines).strip()

    return text.strip()


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

    cookiefile = os.getenv("YTDLP_COOKIEFILE", "youtube_cookies.txt")
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # yt-dlp 配置：尽量贴近你给的示例，同时保留字幕提取相关参数
    # 注意：如果你提供了 cookiefile，但它无效/过期，可能会导致抽取失败。
    # 因此这里做一次失败重试：去掉 cookiefile 再试一次。
    base_opts = {
        "format": "best",
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt/srt/best",
        "subtitleslangs": ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "en", "en-US"],
        "user_agent": user_agent,
        "http_headers": {
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "User-Agent": user_agent,
        },
    }

    if cookiefile and os.path.exists(cookiefile):
        base_opts["cookiefile"] = cookiefile

    def _extract(opts: Dict[str, Any]) -> Dict[str, Any]:
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _is_format_unavailable(err: Exception) -> bool:
        msg = str(err)
        return "Requested format is not available" in msg

    info: Dict[str, Any]
    try:
        info = _extract(base_opts)
    except Exception as e:
        last_err: Exception = e

        # 1) 如果启用了 cookiefile 且失败，再退回到不使用 cookiefile。
        opts_wo_cookie: Optional[Dict[str, Any]] = None
        if "cookiefile" in base_opts:
            opts_wo_cookie = dict(base_opts)
            opts_wo_cookie.pop("cookiefile", None)
            try:
                info = _extract(opts_wo_cookie)
                opts_wo_cookie = None
            except Exception as e2:
                last_err = e2

        # 2) 如果仍然是 format 不可用，再换更宽松的 format 选择器重试。
        if _is_format_unavailable(last_err):
            alt_formats = [
                "bestvideo*+bestaudio/best",
                "bestaudio/best",
            ]
            candidates = [opts_wo_cookie] if opts_wo_cookie else []
            # 如果前面没成功且没进入 opts_wo_cookie，仍然可以在 base_opts 上换格式
            if not candidates:
                candidates = [dict(base_opts)]
            for opts in candidates:
                for fmt in alt_formats:
                    retry_opts = dict(opts)
                    retry_opts["format"] = fmt
                    try:
                        info = _extract(retry_opts)
                        # 成功就直接返回
                        break
                    except Exception as e3:
                        last_err = e3
                else:
                    continue
                break
        else:
            # 非 format 问题，直接抛出最后一次错误
            raise last_err

        # 最后兜底：如果 info 仍未被赋值则抛出 last_err
        if "info" not in locals():
            raise last_err

    title = (info.get("title") or "").strip() or None
    description = (info.get("description") or "").strip() or None
    tags = info.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    subtitles_text = None
    subtitles_meta: Dict[str, Any] = {}

    # 优先“手动字幕”，没有则用“自动字幕”
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    picked = _pick_lang_track(subs) or _pick_lang_track(auto)
    if picked:
        lang, tracks = picked
        # tracks 通常是 list[dict]，每个 dict 里有 url/ext
        track = tracks[0] if isinstance(tracks, list) and tracks else None
        if isinstance(track, dict) and track.get("url"):
            subtitles_meta = {"lang": lang, "ext": track.get("ext"), "source": "subtitles" if lang in subs else "automatic_captions"}
            subtitles_text = _download_subtitle_text(url, track["url"]) or None

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "subtitles_text": subtitles_text,
        "subtitles_meta": subtitles_meta,
        # 保存一点原始信息，便于排错（不建议存太多）
        "extractor_key": info.get("extractor_key"),
        "webpage_url": info.get("webpage_url") or url,
    }


def dumps_json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


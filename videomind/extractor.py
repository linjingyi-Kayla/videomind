from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from yt_dlp import YoutubeDL


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


def extract_video_text(url: str) -> Dict[str, Any]:
    """
    仅提取：title / description / tags / subtitles(优先自动字幕)。
    为提高 B 站成功率，显式设置 UA、打开自动字幕提取等开关。
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt/srt/best",
        "subtitleslangs": ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "en", "en-US"],
        "http_headers": {
            # B 站对 UA/Referer 更敏感一些
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

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


from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Cue:
    start_seconds: int
    text: str


_TS_LINE = re.compile(r"^\[(\d{1,4}):(\d{2})(?::(\d{2}))?\]\s*(.*)$")


def format_ts(seconds: int) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def parse_cues(subtitles_text: Optional[str]) -> List[Cue]:
    """复用现有落库格式：每行 `[mm:ss] text`。"""
    raw = (subtitles_text or "").strip()
    if not raw or raw in ("该视频暂无可用字幕",) or ("暂无可用字幕" in raw and len(raw) < 40):
        return []

    cues: List[Cue] = []
    last_sec = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _TS_LINE.match(line)
        if m:
            a, b, c, text = m.group(1), m.group(2), m.group(3), (m.group(4) or "").strip()
            if c is not None:
                sec = int(a) * 3600 + int(b) * 60 + int(c)
            else:
                sec = int(a) * 60 + int(b)
            last_sec = sec
            cues.append(Cue(start_seconds=sec, text=text or line))
        else:
            cues.append(Cue(start_seconds=last_sec, text=line))
    return cues


def _query_tokens(query: str) -> List[str]:
    q = (query or "").strip().lower()
    tokens: List[str] = []
    tokens.extend(re.findall(r"[a-z0-9]{2,}", q))
    for run in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
            if 2 <= len(run) <= 12:
                tokens.append(run)
    # 去重保序
    seen = set()
    out: List[str] = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _score(text: str, tokens: List[str]) -> int:
    hay = (text or "").lower()
    if not tokens:
        return 0
    return sum(hay.count(t) for t in tokens if t)


def search_cues(
    subtitles_text: Optional[str],
    query: str,
    top_k: int = 5,
    neighbor: int = 2,
) -> Dict[str, Any]:
    """
    在已落库的带时间戳字幕上做轻量关键词检索，并带前后文。
    不引入向量库；命中行向两侧扩展 neighbor 行作为上下文。
    """
    cues = parse_cues(subtitles_text)
    if not cues:
        return {"success": False, "error": "no_subtitles", "results": []}

    k = max(1, min(int(top_k or 5), 8))
    tokens = _query_tokens(query)
    scored = [(_score(c.text, tokens), i) for i, c in enumerate(cues)]
    scored.sort(key=lambda x: (-x[0], x[1]))

    results: List[Dict[str, Any]] = []
    used: set[int] = set()
    low_confidence = bool(tokens) and all(sc <= 0 for sc, _ in scored)

    pool = scored
    if low_confidence:
        # 无命中时返回开头若干窗，让 Agent 换 query 再搜
        pool = [(1, i) for i in range(0, len(cues), max(1, neighbor * 2 + 1))]

    for sc, i in pool:
        if sc <= 0:
            continue
        if i in used:
            continue
        lo = max(0, i - neighbor)
        hi = min(len(cues) - 1, i + neighbor)
        for j in range(lo, hi + 1):
            used.add(j)
        start = cues[i].start_seconds
        if hi + 1 < len(cues):
            end = max(cues[hi].start_seconds, cues[hi + 1].start_seconds)
        else:
            end = cues[hi].start_seconds + 15
        if end < start:
            end = start + 15
        ctx_start = cues[lo].start_seconds
        text = " ".join(cues[j].text for j in range(lo, hi + 1) if cues[j].text).strip()
        results.append(
            {
                "start_seconds": start,
                "end_seconds": end,
                "timestamp": f"{format_ts(ctx_start)}-{format_ts(end)}",
                "text": text[:1200],
            }
        )
        if len(results) >= k:
            break

    return {
        "success": True,
        "low_confidence": low_confidence,
        "query": (query or "").strip(),
        "results": results,
    }

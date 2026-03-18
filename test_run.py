from __future__ import annotations

import json
import sys
from typing import Any, Dict

from dotenv import load_dotenv

from videomind.ai_service import analyze_video
from videomind.extractor import extract_video_text


def _pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def main() -> int:
    load_dotenv(override=False)

    if len(sys.argv) < 2:
        print("用法：python test_run.py <B站或YouTube链接>")
        return 2

    url = sys.argv[1].strip()
    print(f"[1/3] 开始解析：{url}")
    extracted: Dict[str, Any] = extract_video_text(url)

    print("\n[2/3] 抽取结果（节选）")
    print(_pretty(
        {
            "title": extracted.get("title"),
            "extractor_key": extracted.get("extractor_key"),
            "tags_top10": (extracted.get("tags") or [])[:10],
            "has_subtitles": bool((extracted.get("subtitles_text") or "").strip()),
            "subtitles_meta": extracted.get("subtitles_meta"),
            "description_len": len(extracted.get("description") or ""),
            "subtitles_len": len(extracted.get("subtitles_text") or ""),
        }
    ))

    print("\n[3/3] 调用 DeepSeek 总结…")
    ai = analyze_video(extracted)
    print(_pretty(
        {
            "category": ai.category,
            "key_points": ai.key_points,
            "summary": ai.summary,
            "reminder_copy": ai.reminder_copy,
            "remind_at": ai.remind_at,
        }
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


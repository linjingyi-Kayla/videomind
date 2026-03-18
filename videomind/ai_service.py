from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI


@dataclass(frozen=True)
class AIResult:
    category: str
    key_points: List[str]
    summary: str
    reminder_copy: str
    remind_at: str


def _client() -> OpenAI:
    # 支持从 .env 读取
    load_dotenv(override=False)
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY（请在 .env 里配置）")
    return OpenAI(base_url="https://api.deepseek.com", api_key=api_key)


def _model_name() -> str:
    # DeepSeek-V3 在不同账号/时期可能暴露为不同 model 名。
    # 这里用环境变量可覆盖；默认 deepseek-chat（最通用）。
    return os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def _build_prompt(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    title = payload.get("title") or ""
    desc = payload.get("description") or ""
    tags = payload.get("tags") or []
    subtitles = payload.get("subtitles_text") or ""

    # 关键：字幕优先；没有字幕时用 description+tags 兜底
    text = subtitles.strip()
    if not text:
        text = (desc + "\n\n" + " ".join([f"#{t}" for t in tags if t])).strip()
    text = text[:15000]  # MVP：限制输入长度，避免超长/超费

    system = (
        "你是一个中文内容助理，擅长把视频内容快速总结给通勤/排队人群。"
        "你必须严格按我要求输出 JSON，不能输出多余文字。"
    )
    user = f"""
请基于以下视频信息，完成：分类、3个核心要点、总结、以及一条轻量推送提醒文案（适合通勤阅读）。

要求：
- 输出必须是严格 JSON
- 字段：category（字符串）、key_points（长度=3 的字符串数组）、summary（<=220字）、reminder_copy（<=40字）、remind_at（建议提醒时间，格式 HH:MM，24小时制）
- 分类示例：AI 技术 / 穿搭 / 美食 / 职业 / 生活方式 / 学习 / 投资理财 / 影视娱乐 / 亲子
- 语言：简体中文

视频标题：{title}

视频简介/描述：
{desc}

标签：{", ".join(tags) if tags else "(无)"}

字幕或文本（可能为空）：
{text if text else "(无字幕/无文本)"}

补充说明：
- 下面这段字幕是“原始字幕文本”，请先梳理其内部逻辑（按出现顺序/因果/结论），再产出总结；
- 若字幕带时间戳，请把时间戳当作段落线索，不要原样堆叠到输出里；
- key_points（3条）建议按逻辑推进顺序输出。
""".strip()

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def analyze_video(payload: Dict[str, Any]) -> AIResult:
    cli = _client()
    resp = cli.chat.completions.create(
        model=_model_name(),
        temperature=0.2,
        max_tokens=600,
        messages=_build_prompt(payload),
    )
    text = (resp.choices[0].message.content or "").strip()

    # DeepSeek 偶尔会把 JSON 包在 ```json ... ``` 代码块里；这里做一次稳健提取
    def _extract_json(s: str) -> str:
        s = s.strip()
        if s.startswith("```"):
            # 去掉首尾 fence
            s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
            s = s.strip()
        # 兜底：截取第一对大括号
        i = s.find("{")
        j = s.rfind("}")
        if i != -1 and j != -1 and j > i:
            return s[i : j + 1]
        return s

    try:
        data = json.loads(_extract_json(text))
    except Exception as e:
        raise RuntimeError(f"DeepSeek 返回无法解析为 JSON：{text}") from e

    category = str(data.get("category") or "").strip() or "未分类"
    key_points = data.get("key_points") or []
    if not isinstance(key_points, list):
        key_points = []
    key_points = [str(x).strip() for x in key_points if str(x).strip()][:3]
    while len(key_points) < 3:
        key_points.append("（要点待补充）")

    summary = str(data.get("summary") or "").strip()
    reminder_copy = str(data.get("reminder_copy") or "").strip()
    remind_at = str(data.get("remind_at") or "").strip() or "18:30"

    # 轻度兜底裁剪
    if len(summary) > 260:
        summary = summary[:260].rstrip() + "…"
    if len(reminder_copy) > 60:
        reminder_copy = reminder_copy[:60].rstrip() + "…"

    return AIResult(
        category=category,
        key_points=key_points,
        summary=summary,
        reminder_copy=reminder_copy,
        remind_at=remind_at,
    )


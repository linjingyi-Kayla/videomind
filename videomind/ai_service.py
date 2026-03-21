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
    raw_sub = subtitles.strip()
    placeholder_no_sub = raw_sub in (
        "",
        "该视频暂无可用字幕",
    ) or ("暂无可用字幕" in raw_sub and len(raw_sub) < 40)

    text = raw_sub
    if not text or placeholder_no_sub:
        text = (desc + "\n\n" + " ".join([f"#{t}" for t in tags if t])).strip()
    # 无真实字幕轨时，向模型说明仅能用标题/简介归纳
    no_sub_for_model = placeholder_no_sub
    text = text[:15000]  # MVP：限制输入长度，避免超长/超费

    system = (
        "你是一个中文内容助理，擅长把视频内容快速总结给通勤/排队人群。"
        "你必须严格按我要求输出 JSON，不能输出多余文字。"
        "JSON 里所有字符串必须用英文半角双引号 \" 作为字段边界；"
        "句末中文弯引号可以出现在字符串内容里，但字段结尾必须是 ASCII 的 \" 再跟逗号或括号。"
    )
    page_url = str(payload.get("webpage_url") or "").strip()
    user = f"""
【重要】当前请求只针对下面这一条视频页面（请严格对应该链接的字幕与标题，不要混入其他视频、历史会话或想象内容）：
视频链接：{page_url or "(未提供)"}

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
{f"- 【无字幕或仅有简介】本视频没有可用字幕文本：请仅依据上面的标题、简介与标签归纳，不要编造具体对话或细节；要点与总结需诚实标注信息来源有限（可委婉表达）。" if no_sub_for_model else "- 下面这段字幕是“原始字幕文本”，请先梳理其内部逻辑（按出现顺序/因果/结论），再产出总结；"}
{f"" if no_sub_for_model else "- 若字幕带时间戳，请把时间戳当作段落线索，不要原样堆叠到输出里；"}
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
        # 长字幕时 summary/key_points 较长，450 易截断导致 JSON 不完整
        max_tokens=1200,
        messages=_build_prompt(payload),
    )
    text = (resp.choices[0].message.content or "").strip()

    # DeepSeek 常把 JSON 包在 ```json ... ``` 里，或前后带说明文字；不能用简单 rfind("}")（串内/截断会错）
    def _strip_code_fences(s: str) -> str:
        s = s.strip()
        if s.startswith("\ufeff"):
            s = s.lstrip("\ufeff").strip()
        # 可能为「说明文字」+ ```json ... ```；从首个 fence 起剥，闭合取最后一个 ```（避免非贪婪正则截断）
        idx = s.find("```")
        if idx != -1:
            s = s[idx:]
            s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
            ri = s.rfind("```")
            if ri != -1:
                s = s[:ri].rstrip()
        return s.strip()

    def _first_json_object(s: str) -> str:
        """从首个 { 起按括号深度截取完整 JSON 对象（尊重字符串内的引号转义）。"""
        i = s.find("{")
        if i == -1:
            return s
        depth = 0
        in_string = False
        escape = False
        for j in range(i, len(s)):
            c = s[j]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[i : j + 1]
        # 未闭合（截断）：仍交给 json.loads 报错，便于日志
        return s[i:]

    def _extract_json(s: str) -> str:
        s = _strip_code_fences(s)
        # 前面可能有「好的，如下」等废话，先找第一个 {
        i = s.find("{")
        if i > 0:
            s = s[i:]
        return _first_json_object(s)

    def _repair_json_text(s: str) -> str:
        """修复模型漏写 ASCII 闭合引号：中文右引号 ” 后紧跟 , ] } 的情况。"""
        s = re.sub(r"”(\s*,)", r'”"\1', s)
        s = re.sub(r"”(\s*])", r'”"\1', s)
        s = re.sub(r"”(\s*})", r'”"\1', s)
        return s

    raw_json = _extract_json(text)
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        try:
            data = json.loads(_repair_json_text(raw_json))
        except Exception as e2:
            raise RuntimeError(f"DeepSeek 返回无法解析为 JSON：{text}") from e2
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


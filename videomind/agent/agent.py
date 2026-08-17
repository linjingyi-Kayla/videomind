from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from videomind.agent.prompts import AGENT_SYSTEM_PROMPT
from videomind.agent.state import HISTORY_LIMIT, MAX_STEPS, MAX_TOOL_CALLS, AgentResult, ToolContext
from videomind.agent.tools import TOOL_DEFINITIONS, execute_tool
from videomind.ai_service import _client, _model_name

logger = logging.getLogger("videomind.agent")


def _trim(s: str, n: int) -> str:
    t = (s or "").strip()
    return t[:n] if len(t) > n else t


def build_agent_messages(
    *,
    task_id: str,
    user_message: str,
    chat_history: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"当前视频 task_id={task_id}。之后的问答与工具都针对这一条视频。\n"
                "请按工具规则决定是否调用工具。"
            ),
        },
        {
            "role": "assistant",
            "content": "好的，我会针对当前视频使用工具检索字幕或在必要时核对公开信息，再给出回答。",
        },
    ]
    trimmed = (chat_history or [])[-HISTORY_LIMIT:]
    for m in trimmed:
        role = (m.get("role") or "").strip()
        content = _trim(m.get("content") or "", 4000)
        if role not in ("user", "assistant") or not content:
            continue
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": _trim(user_message, 2000)})
    return messages


def _assistant_message_dict(message: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "role": "assistant",
        "content": message.content if message.content else None,
    }
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        payload["tool_calls"] = []
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            payload["tool_calls"].append(
                {
                    "id": getattr(tc, "id", "") or "",
                    "type": getattr(tc, "type", None) or "function",
                    "function": {
                        "name": getattr(fn, "name", "") if fn else "",
                        "arguments": getattr(fn, "arguments", "") if fn else "{}",
                    },
                }
            )
    return payload


def _parse_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not s:
        return {}
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _tool_result_public(result: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in result.items() if k != "_meta"}


def generate_safe_final_answer(cli: Any, model: str, messages: List[Dict[str, Any]]) -> str:
    wrap = list(messages) + [
        {
            "role": "user",
            "content": (
                "请根据以上对话与工具结果，给出最终中文回答。"
                "不要再调用工具。区分「根据视频」与「外部公开信息」。"
                "没有证据时不要编造时间戳。"
            ),
        }
    ]
    try:
        resp = cli.chat.completions.create(
            model=model,
            temperature=0.3,
            max_tokens=900,
            messages=wrap,
        )
        out = (resp.choices[0].message.content or "").strip()
        if out:
            return out
    except Exception as e:
        logger.warning("safe_final_answer failed: %s", e)
    return "我已经查过可用材料，但这一步没能整理出完整回答。请换个问法再试一次。"


def run_agent(
    *,
    user_id: int,
    task_id: str,
    user_message: str,
    chat_history: List[Dict[str, str]],
    tz_offset_minutes: Optional[int] = None,
    client: Any = None,
) -> AgentResult:
    request_id = uuid.uuid4().hex[:12]
    ctx = ToolContext(
        user_id=user_id,
        task_id=task_id,
        tz_offset_minutes=tz_offset_minutes,
        request_id=request_id,
    )
    messages = build_agent_messages(
        task_id=task_id,
        user_message=user_message,
        chat_history=chat_history,
    )
    cli = client or _client()
    model = _model_name()
    tool_call_count = 0
    traces: List[Dict[str, Any]] = []

    logger.info(
        "agent_start request_id=%s user_id=%s task_id=%s",
        request_id,
        user_id,
        task_id,
    )

    for step in range(MAX_STEPS):
        resp = cli.chat.completions.create(
            model=model,
            temperature=0.3,
            max_tokens=900,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
        )
        message = resp.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []

        if not tool_calls:
            content = (message.content or "").strip()
            if not content:
                content = generate_safe_final_answer(cli, model, messages)
            logger.info(
                "agent_done request_id=%s steps=%s tool_calls=%s",
                request_id,
                step + 1,
                tool_call_count,
            )
            return AgentResult(
                content=content,
                tool_call_count=tool_call_count,
                steps=step + 1,
                traces=traces,
                request_id=request_id,
            )

        messages.append(_assistant_message_dict(message))

        for tc in tool_calls:
            tool_call_count += 1
            if tool_call_count > MAX_TOOL_CALLS:
                content = generate_safe_final_answer(cli, model, messages)
                return AgentResult(
                    content=content,
                    tool_call_count=tool_call_count,
                    steps=step + 1,
                    traces=traces,
                    request_id=request_id,
                )
            fn = getattr(tc, "function", None)
            name = (getattr(fn, "name", None) if fn else None) or "unknown"
            args = _parse_arguments(getattr(fn, "arguments", None) if fn else None)
            result = execute_tool(ctx, name, args)
            traces.append(
                {
                    "step": step + 1,
                    "tool": name,
                    "success": bool(result.get("success")),
                    "error": result.get("error"),
                    "latency_ms": (result.get("_meta") or {}).get("latency_ms"),
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(tc, "id", "") or "",
                    "name": name,
                    "content": json.dumps(_tool_result_public(result), ensure_ascii=False)[:8000],
                }
            )

    content = generate_safe_final_answer(cli, model, messages)
    logger.info(
        "agent_max_steps request_id=%s tool_calls=%s",
        request_id,
        tool_call_count,
    )
    return AgentResult(
        content=content,
        tool_call_count=tool_call_count,
        steps=MAX_STEPS,
        traces=traces,
        request_id=request_id,
    )

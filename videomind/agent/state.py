from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

MAX_STEPS = 5
MAX_TOOL_CALLS = 8
HISTORY_LIMIT = 12


@dataclass
class ToolContext:
    user_id: int
    task_id: str
    tz_offset_minutes: Optional[int] = None
    request_id: str = ""
    session: Optional[Session] = None


@dataclass
class AgentResult:
    content: str
    tool_call_count: int = 0
    steps: int = 0
    traces: List[Dict[str, Any]] = field(default_factory=list)
    request_id: str = ""

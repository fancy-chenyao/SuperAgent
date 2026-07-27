from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ContextReference(BaseModel):
    """当前请求实际使用的会话上下文引用。"""

    mention: str
    kind: Literal["entity", "artifact", "clarification"]
    key: str
    value: Any
    source: str = "conversation"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ResolvedRequest(BaseModel):
    """送入 TaskProfiler 的本轮请求，不包含无关历史任务。"""

    raw_message: str
    resolved_message: str
    turn_type: Literal["request", "clarification_answer"] = "request"
    entity_overrides: dict[str, Any] = Field(default_factory=dict)
    artifact_inputs: list[dict[str, Any]] = Field(default_factory=list)
    context_references: list[ContextReference] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)

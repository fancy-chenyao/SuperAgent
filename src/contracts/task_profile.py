from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TaskProfile(BaseModel):
    """主 Agent 对自然语言任务形成的稳定、可审计结构化画像。"""

    task_id: str
    intent: str = "general_assistance"
    task_type: str = "GENERAL"
    business_goal: str = ""
    action: str = "read"
    entities: dict[str, Any] = Field(default_factory=dict)
    data_scope: list[str] = Field(default_factory=lambda: ["general"])
    scenario_tags: list[str] = Field(default_factory=lambda: ["general"])
    expected_capabilities: list[str] = Field(default_factory=lambda: ["General"])
    risk_level: str = "LOW"
    irreversible: bool = False
    constraints: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = ""
    sub_intents: list[str] = Field(default_factory=list)
    subtasks: list[dict[str, Any]] = Field(default_factory=list)
    is_composite: bool = False
    segments: list[dict[str, Any]] = Field(default_factory=list)
    intent_nodes: list[dict[str, Any]] = Field(default_factory=list)
    confidence_factors: list[str] = Field(default_factory=list)
    # 新版识别结果。旧字段继续保留，供现有 Router、Planner 和前端使用。
    primary_goal_intent: str = "general_assistance"
    ambiguities: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    clarification_reasons: list[dict[str, Any]] = Field(default_factory=list)
    recognition_mode: str = "rule"
    recognition_degraded: bool = False
    recognition: dict[str, Any] = Field(default_factory=dict)
    # 当前对话轮次解析结果。历史任务只通过显式引用进入，不参与任务边界。
    raw_request: str = ""
    resolved_request: str = ""
    context_references: list[dict[str, Any]] = Field(default_factory=list)
    context_artifacts: list[dict[str, Any]] = Field(default_factory=list)

    def to_legacy_scenario(self) -> dict[str, Any]:
        """兼容现有 S-ABAC 和 Planner 使用的字段命名。"""
        data = self.model_dump()
        data.update(
            {
                "operation_mode": self.action,
                "risk_profile": self.risk_level,
                "data_scope": ",".join(self.data_scope),
            }
        )
        return data

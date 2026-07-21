from __future__ import annotations

from pydantic import BaseModel, Field


class AgentCard(BaseModel):
    """部门 Agent 的标准能力、安全边界和治理元数据。"""

    agent_id: str
    name: str
    department: str = "General"
    capabilities: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)
    supported_actions: list[str] = Field(default_factory=list)
    accepted_data_scopes: list[str] = Field(default_factory=list)
    scenario_tags: list[str] = Field(default_factory=list)
    risk_ceiling: str = "LOW"
    required_grants: list[str] = Field(default_factory=list)
    tool_scopes: list[str] = Field(default_factory=list)
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    version: str = "1.0.0"
    status: str = "ONLINE"
    description: str = ""
    source: str = "local"


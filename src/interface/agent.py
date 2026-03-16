from enum import Enum, unique
from typing import Any, Dict, List, Optional

try:
    from langgraph.graph import MessagesState
except Exception:  # pragma: no cover - optional dependency in lightweight test env
    class MessagesState(dict):  # type: ignore
        pass
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

from .mcp import Tool


@unique
class Lang(str, Enum):
    EN = "en"
    ZH = "zh"
    JP = "jp"
    SP = "sp"
    DE = "de"


@unique
class AgentSource(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class LLMType(str, Enum):
    BASIC = "basic"
    REASONING = "reasoning"
    VISION = "vision"
    CODE = "code"


class AgentMCPConfig(BaseModel):
    mcp_servers: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Component(BaseModel):
    component_type: str
    label: str
    name: str
    description: str
    config: dict


COORDINATOR = Component(
    component_type="agent",
    label="coordinator",
    name="coordinator",
    description="Coordinator node that communicate with customers.",
    config={"type": "system_agent", "name": "coordinator"},
)

PLANNER = Component(
    component_type="agent",
    label="planner",
    name="planner",
    description="Planner node that plan the task.",
    config={"type": "system_agent", "name": "planner"},
)

PUBLISHER = Component(
    component_type="condtion",
    label="publisher_condition",
    name="publisher",
    description="Publisher node that publish the task.",
    config={"type": "system_agent", "name": "publisher"},
)

class WorkMode(str, Enum):
    LAUNCH = "launch"
    POLISH = "polish"
    PRODUCTION = "production"
    AUTO = "auto"


class Agent(BaseModel):
    user_id: str
    agent_name: str
    nick_name: str
    description: str
    llm_type: LLMType
    selected_tools: List[Tool]
    prompt: str

    source: AgentSource = AgentSource.LOCAL
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    mcp_config: Optional[AgentMCPConfig] = None
    mcp_servers: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _validate_and_normalize(self):
        if self.source == AgentSource.REMOTE and not self.endpoint:
            raise ValueError("Remote agent requires endpoint")

        if self.mcp_config is None and self.mcp_servers:
            self.mcp_config = AgentMCPConfig(mcp_servers=self.mcp_servers, enabled=True)

        if self.mcp_config is not None and not self.mcp_servers:
            self.mcp_servers = self.mcp_config.mcp_servers

        return self


class AgentMessage(BaseModel):
    content: str
    role: str


class AgentRequest(BaseModel):
    user_id: str
    lang: Lang
    messages: List[AgentMessage]
    debug: bool
    deep_thinking_mode: bool
    search_before_planning: bool
    coor_agents: Optional[list[str]]
    workmode: WorkMode
    workflow_id: Optional[str] = None
    stop_after_planner: bool = False
    instruction: Optional[str] = None
    instruction_history: Optional[list[str]] = None


class listAgentRequest(BaseModel):
    user_id: Optional[str]
    match: Optional[str]


class EditStepsRequest(BaseModel):
    workflow_id: str
    planning_steps: dict


class Router(TypedDict):
    next: str


class PromptBuilder(TypedDict):
    prompt: str
    agent_description: str


class State(MessagesState):
    TEAM_MEMBERS: list[str]
    TEAM_MEMBERS_DESCRIPTION: str
    RESOURCE_CATALOG: str
    user_id: str
    next: str
    full_plan: str
    deep_thinking_mode: bool
    search_before_planning: bool
    workflow_id: str
    workflow_mode: WorkMode
    initialized: bool
    stop_after_planner: bool
    instruction_history: list[str]


class RemoveAgentRequest(BaseModel):
    user_id: str
    agent_name: str

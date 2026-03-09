"""Set of serialize object."""

from typing_extensions import TypedDict
from typing import Annotated


class AgentTool(TypedDict):
    """
    A typed dictionary representing a tool configuration for an agent.

    This class defines the structure for a tool used by an agent, including its name
    and description. It uses `TypedDict` to enforce type hints for the dictionary keys.
    """

    name: str
    description: str


class AgentBuilder(TypedDict):
    """
    For building an agent with specified properties and capabilities
    """

    agent_name: str
    agent_description: str
    thought: str
    llm_type: str
    selected_tools: list[AgentTool]
    prompt: str


class NewAgent(TypedDict):
    """
    A typed dictionary representing a new agent.

    """

    name: str
    role: str
    capabilities: Annotated[list[str], "List of capabilities"]
    contribution: str


class Step(TypedDict):
    """
    A typed dictionary representing a step in a plan.
    """

    agent_name: str
    title: str
    description: str
    note: str


class PlanWithAgents(TypedDict):
    """
    A typed dictionary representing a plan with agents.

    This class defines the structure for a plan that includes a list of agents. It uses
    `TypedDict` to enforce type hints for the dictionary keys.
    """

    thought: str
    title: str
    new_agents_needed: list[NewAgent]
    steps: list[Step]

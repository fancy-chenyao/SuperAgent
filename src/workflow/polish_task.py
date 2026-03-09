import logging
from src.llm.llm import get_llm_by_type
from src.llm.agents import AGENT_LLM_MAP
from src.prompts.template import apply_polish_template
from src.interface.agent import PromptBuilder
from src.interface.agent import Agent

logger = logging.getLogger(__name__)


async def polish_agent(_agent: Agent, part_to_edit: str, instruction=None, tools=None):
    if part_to_edit in ["prompt", "tool"]:
        if part_to_edit == "prompt":
            messages = apply_polish_template(_agent, instruction)
        else:
            TOOLS_DESCRIPTION_TEMPLATE = """
            - **`{tool_name}`**: {tool_description}
            """
            TOOLS_DESCRIPTION = """
            """
            for tool in tools:
                TOOLS_DESCRIPTION += "\n" + TOOLS_DESCRIPTION_TEMPLATE.format(
                    tool_name=tool["name"], tool_description=tool["description"]
                )
            instruction = f"I have selected a new set of tools:{TOOLS_DESCRIPTION}. Please rewrite the prompt according to the new tool list, and it must include all tools"
            messages = apply_polish_template(_agent, instruction)
        response = (
            get_llm_by_type(AGENT_LLM_MAP["polisher"])
            .with_structured_output(PromptBuilder)
            .invoke(messages)
        )
        return response
    else:
        raise ValueError(
            f"The expectation for part_to_edit is prompt or tool,but get {part_to_edit}"
        )

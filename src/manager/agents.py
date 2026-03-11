import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.interface.agent import Agent
from src.interface.mcp import Tool
from src.manager.mcp import get_mcp_hot_reload_manager
from src.manager.registry import AgentRegistry, ToolRegistry
from src.manager.registry import sync_local_resources, sync_remote_agents
from src.manager.resource import get_resource_registry, refresh_remote_resources, start_remote_registry_watch
from src.service.env import USR_AGENT, USE_BROWSER, USE_MCP_TOOLS
from src.skills import SkillsManager
from src.utils.path_utils import get_project_root

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


class NotFoundAgentError(Exception):
    """Raised when agent is not found."""


class AgentManager:
    def __init__(self, tools_dir: Path, agents_dir: Path, prompt_dir: Path, skills_dir: Path):
        for path in [tools_dir, agents_dir, prompt_dir, skills_dir]:
            Path(path).mkdir(parents=True, exist_ok=True)

        self.tools_dir = Path(tools_dir)
        self.agents_dir = Path(agents_dir)
        self.prompt_dir = Path(prompt_dir)
        self.skills_dir = Path(skills_dir)

        self.available_agents: Dict[str, Agent] = {}
        self.available_tools: Dict[str, Any] = {}

        self.skills_manager = SkillsManager(self.skills_dir)
        self.agent_registry = AgentRegistry(self.agents_dir, self.prompt_dir)

        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def ensure_initialized(self, user_agent_flag: bool = USR_AGENT) -> None:
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            await self._load_default_agents()
            await self.agent_registry.load_from_disk(user_agent_flag=user_agent_flag)
            await self._sync_agent_cache()

            await self.load_tools()
            await self.skills_manager.initialize()

            # Resource registry integration (Stage 3)
            resource_registry = await get_resource_registry()
            await sync_local_resources(
                agent_registry=self.agent_registry,
                tool_registry=await ToolRegistry.get_instance(),
                skills_manager=self.skills_manager,
                resource_registry=resource_registry,
            )
            await refresh_remote_resources()
            await start_remote_registry_watch()
            await sync_remote_agents(resource_registry, self.agent_registry)
            await self._sync_agent_cache()

            self._initialized = True
            logger.info(
                "AgentManager initialized: %s agents, %s tools, %s skills",
                len(self.available_agents),
                len(self.available_tools),
                len(self.skills_manager.list_skills()),
            )

    async def initialize(self, user_agent_flag: bool = USR_AGENT):
        await self.ensure_initialized(user_agent_flag=user_agent_flag)

    async def load_tools(self):
        from src.manager.registry import ToolLoader

        registry = await ToolRegistry.get_instance()
        loader = ToolLoader(registry)

        await loader.load_builtin_tools()
        if USE_MCP_TOOLS:
            manager = await get_mcp_hot_reload_manager()
            try:
                await manager.reload(force=True)
            except Exception as e:
                logger.error("MCP reload failed during tool load: %s", e)

        global_tools = await registry.list_global_tools()
        self.available_tools = {
            meta.tool.name: meta.tool
            for meta in global_tools
            if hasattr(meta.tool, "name")
        }

    async def load_mcp_tools(self):
        await self.ensure_initialized()
        registry = await ToolRegistry.get_instance()
        manager = await get_mcp_hot_reload_manager()
        try:
            await manager.reload(force=True)
        except Exception as e:
            logger.error("MCP reload failed during MCP tool load: %s", e)
        global_tools = await registry.list_global_tools()
        self.available_tools = {
            meta.tool.name: meta.tool
            for meta in global_tools
            if hasattr(meta.tool, "name")
        }

    async def _sync_agent_cache(self):
        self.available_agents = await self.agent_registry.snapshot()
        if not USE_BROWSER and "browser" in self.available_agents:
            del self.available_agents["browser"]

    async def _save_agent(self, agent: Agent, flush: bool = False):
        await self.ensure_initialized()
        await self.agent_registry.register(agent, persist=flush)
        await self._sync_agent_cache()

    async def _save_agents(self, agents: list[Agent], flush: bool = False):
        await self.ensure_initialized()
        for agent in agents:
            await self.agent_registry.register(agent, persist=flush)
        await self._sync_agent_cache()

    async def _remove_agent(self, agent_name: str):
        await self.ensure_initialized()
        await self.agent_registry.delete(agent_name, remove_files=True)
        await self._sync_agent_cache()

        manager = await get_mcp_hot_reload_manager()
        manager.unregister_agent_mcp_config(agent_name)

    async def _load_agent(self, agent_name: str, user_agent_flag: bool = False):
        await self.ensure_initialized(user_agent_flag=user_agent_flag)
        agent = await self.agent_registry.load_agent(agent_name, user_agent_flag=user_agent_flag)
        if agent is None:
            raise FileNotFoundError(f"agent {agent_name} not found.")

        if agent.mcp_config and agent.mcp_config.enabled and agent.mcp_config.mcp_servers:
            manager = await get_mcp_hot_reload_manager()
            manager.register_agent_mcp_config(agent.agent_name, agent.mcp_config.mcp_servers)

        await self._sync_agent_cache()

    async def _list_agents(self, user_id: str = None, match: str = None):
        await self.ensure_initialized()
        return await self.agent_registry.list(user_id=user_id, match=match)

    async def _list_user_all_agents(self, user_id: str):
        await self.ensure_initialized()
        return await self.agent_registry.list(user_id=user_id)

    async def _edit_agent(self, agent: Agent):
        await self.ensure_initialized()

        existing = await self.agent_registry.get(agent.agent_name)
        if existing is None:
            raise NotFoundAgentError(f"agent {agent.agent_name} not found.")

        await self.agent_registry.update(agent, persist=True)

        manager = await get_mcp_hot_reload_manager()
        manager.unregister_agent_mcp_config(agent.agent_name)
        if agent.mcp_config and agent.mcp_config.enabled and agent.mcp_config.mcp_servers:
            manager.register_agent_mcp_config(agent.agent_name, agent.mcp_config.mcp_servers)

        await self._sync_agent_cache()
        return "success"

    async def _create_agent_by_prebuilt(
        self,
        user_id: str,
        name: str,
        nick_name: str,
        llm_type: str,
        tools: list[Any],
        prompt: str,
        description: str,
    ):
        await self.ensure_initialized()
        selected_tools = [
            Tool(name=getattr(t, "name", ""), description=getattr(t, "description", ""))
            for t in tools
            if getattr(t, "name", None)
        ]

        agent = Agent(
            agent_name=name,
            nick_name=nick_name,
            description=description,
            user_id=user_id,
            llm_type=llm_type,
            selected_tools=selected_tools,
            prompt=str(prompt),
        )
        await self.agent_registry.register(agent, persist=True)
        await self._sync_agent_cache()
        return agent

    async def _load_default_agents(self):
        from src.llm.agents import AGENT_LLM_MAP
        from src.prompts import get_prompt_template
        from src.tools import bash_tool, browser_tool, crawl_tool, python_repl_tool, tavily_tool

        default_specs = [
            {
                "name": "researcher",
                "llm_type": AGENT_LLM_MAP["researcher"],
                "tools": [tavily_tool, crawl_tool],
                "description": "This agent specializes in research tasks by utilizing search engines and web crawling. It can search for information using keywords, crawl specific URLs to extract content, and synthesize findings into comprehensive reports. The agent excels at gathering information from multiple sources, verifying relevance and credibility, and presenting structured conclusions based on collected data.",
            },
            {
                "name": "coder",
                "llm_type": AGENT_LLM_MAP["coder"],
                "tools": [python_repl_tool, bash_tool],
                "description": "This agent specializes in software engineering tasks using Python and bash scripting. It can analyze requirements, implement efficient solutions, and provide clear documentation. The agent excels at data analysis, algorithm implementation, system resource management, and environment queries. It follows best practices, handles edge cases, and integrates Python with bash when needed for comprehensive problem-solving.",
            },
            {
                "name": "browser",
                "llm_type": AGENT_LLM_MAP["browser"],
                "tools": [browser_tool],
                "description": "This agent specializes in interacting with web browsers. It can navigate to websites, perform actions like clicking, typing, and scrolling, and extract information from web pages. The agent is adept at handling tasks such as searching specific websites, interacting with web elements, and gathering online data. It is capable of operations like logging in, form filling, clicking buttons, and scraping content.",
            },
            {
                "name": "reporter",
                "llm_type": AGENT_LLM_MAP["reporter"],
                "tools": [],
                "description": "This agent specializes in creating clear, comprehensive reports based solely on provided information and verifiable facts. It presents data objectively, organizes information logically, and highlights key findings using professional language. The agent structures reports with executive summaries, detailed analysis, and actionable conclusions while maintaining strict data integrity and never fabricating information.",
            },
        ]

        for spec in default_specs:
            existing = await self.agent_registry.get(spec["name"])
            if existing:
                continue
            agent = Agent(
                user_id="share",
                agent_name=spec["name"],
                nick_name=spec["name"],
                description=spec["description"],
                llm_type=spec["llm_type"],
                selected_tools=[
                    Tool(name=t.name, description=getattr(t, "description", ""))
                    for t in spec["tools"]
                ],
                prompt=get_prompt_template(spec["name"]),
            )
            await self.agent_registry.register(agent, persist=True)

    async def _list_default_tools(self):
        await self.ensure_initialized()
        return [
            Tool(name=tool_name, description=getattr(agent_tool, "description", ""))
            for tool_name, agent_tool in self.available_tools.items()
        ]

    async def _list_default_agents(self):
        await self.ensure_initialized()
        return [agent for agent in self.available_agents.values() if agent.user_id == "share"]

    async def execute_skill(self, skill_name: str, **kwargs):
        await self.ensure_initialized()
        return await self.skills_manager.execute_skill(skill_name, **kwargs)

    def get_skill(self, skill_name: str):
        return self.skills_manager.get_skill(skill_name)

    def list_skills(self):
        return self.skills_manager.list_skills()

    def list_skills_by_category(self, category: str):
        return self.skills_manager.list_skills_by_category(category)


def create_agent_manager() -> AgentManager:
    tools_dir = get_project_root() / "store" / "tools"
    agents_dir = get_project_root() / "store" / "agents"
    prompts_dir = get_project_root() / "store" / "prompts"
    skills_dir = get_project_root() / "store" / "skills"
    return AgentManager(tools_dir, agents_dir, prompts_dir, skills_dir)

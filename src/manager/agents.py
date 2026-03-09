import re
import logging
import asyncio
from pathlib import Path

import aiofiles
import aiofiles.os

from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient

from src.tools import (
    bash_tool,
    browser_tool,
    crawl_tool,
    python_repl_tool,
    tavily_tool,
)

from src.llm.agents import AGENT_LLM_MAP
from src.interface.mcp import Tool
from src.prompts import get_prompt_template
from src.interface.agent import Agent
from src.service.env import USR_AGENT, USE_BROWSER,USE_MCP_TOOLS
from src.manager.mcp import mcp_client_config

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

class NotFoundAgentError(Exception):
    """when agent not found"""
    pass

class NotFoundToolError(Exception):
    """when tool not found"""
    pass

class AgentManager:
    def __init__(self, tools_dir, agents_dir, prompt_dir):
        for path in [tools_dir, agents_dir, prompt_dir]:
            if not path.exists():
                logger.info(f"path {path} does not exist when agent manager initializing, gona to create...")
                path.mkdir(parents=True, exist_ok=True)
                
        self.tools_dir = Path(tools_dir)
        self.agents_dir = Path(agents_dir)
        self.prompt_dir = Path(prompt_dir)

        if not self.tools_dir.exists() or not self.agents_dir.exists() or not self.prompt_dir.exists():
            raise FileNotFoundError("One or more provided directories do not exist.")
        self.available_agents = {}
        self.available_tools = {}

    async def initialize(self, user_agent_flag=USR_AGENT):
        """Asynchronously initializes the AgentManager by loading agents and tools."""
        await self._load_agents(user_agent_flag)
        await self.load_tools()
        logger.info(f"AgentManager initialized. {len(self.available_agents)} agents and {len(self.available_tools)} tools available.")

    async def _create_agent_by_prebuilt(self, user_id: str, name: str, nick_name: str, llm_type: str, tools: list[tool], prompt: str, description: str):
        async def _create(user_id: str, name: str, nick_name: str, llm_type: str, tools: list[tool], prompt: str, description: str):
            _tools = []
            for tool in tools:
                _tools.append(Tool(
                    name=tool.name,
                    description=tool.description,
                ))
            
            _agent = Agent(
                agent_name=name,
                nick_name=nick_name,
                description=description,
                user_id=user_id,
                llm_type=llm_type,
                selected_tools=_tools,
                prompt=str(prompt)
            )
        
            await self._save_agent(_agent, flush=True)
            return _agent
        
        _agent = await _create(user_id, name, nick_name, llm_type, tools, prompt, description)
        self.available_agents[name] = _agent

    async def load_mcp_tools(self):
        mcp_client = MultiServerMCPClient(mcp_client_config())
        mcp_tools = await mcp_client.get_tools()
        for _tool in mcp_tools:
            self.available_tools[_tool.name] = _tool
                    
    async def load_tools(self):        
        self.available_tools.update({
            bash_tool.name: bash_tool,
            browser_tool.name: browser_tool,
            crawl_tool.name: crawl_tool,
            python_repl_tool.name: python_repl_tool,
            tavily_tool.name: tavily_tool,
        })
        if not USE_BROWSER:
            del self.available_tools[browser_tool.name]    
        if USE_MCP_TOOLS:
            await self.load_mcp_tools()
        
    async def _write_file(self, path: Path, content: str):
        async with aiofiles.open(path, "w") as f:
            await f.write(content)

    async def _save_agent(self, agent: Agent, flush=False):
        agent_path = self.agents_dir / f"{agent.agent_name}.json"
        agent_prompt_path = self.prompt_dir / f"{agent.agent_name}.md"
        agents = []

        if flush and not agent_path.exists():
            agents.append((agent_path, agent.model_dump_json(indent=4)))

        if flush and not agent_prompt_path.exists():
            agents.append((agent_prompt_path, agent.prompt))

        if not agents:
            logger.debug(f"skip saving agent")
            return

        agent_tasks = [self._write_file(path, content) for path, content in agents]
        await asyncio.gather(*agent_tasks)

        logger.info(f"agent {agent.agent_name} saved.")
        
    async def _remove_agent(self, agent_name: str):
        agent_path = self.agents_dir / f"{agent_name}.json"
        agent_prompt_path = self.prompt_dir / f"{agent_name}.md"

        if agent_path.exists():
            await aiofiles.os.remove(agent_path)
            logger.info(f"Removed agent definition file: {agent_path}")
        if agent_prompt_path.exists():
            await aiofiles.os.remove(agent_prompt_path)
            logger.info(f"Removed agent prompt file: {agent_prompt_path}")
        if agent_name in self.available_agents:
            del self.available_agents[agent_name] 
            logger.info(f"Removed agent '{agent_name}' from available agents.")
    
    async def _load_agent(self, agent_name: str, user_agent_flag: bool=False):
        agent_path = self.agents_dir / f"{agent_name}.json"

        if not agent_path.exists():
            raise FileNotFoundError(f"agent {agent_name} not found.")

        async with aiofiles.open(agent_path, "r") as f:
            json_str = await f.read()
            _agent = Agent.model_validate_json(json_str)
            if _agent.user_id == 'share':
                self.available_agents[_agent.agent_name] = _agent
            elif user_agent_flag:
                self.available_agents[_agent.agent_name] = _agent
        
    async def _list_agents(self, user_id: str = None, match: str = None):
        agents = [agent for agent in self.available_agents.values()]

        if user_id:
            agents = [agent for agent in agents if agent.user_id == user_id]
        if match:
            agents = [agent for agent in agents if re.match(match, agent.agent_name)]

        return agents

    async def _edit_agent(self, agent: Agent):

        if agent.agent_name not in self.available_agents:
            raise NotFoundAgentError(f"agent {agent.agent_name} not found.")

        _agent = self.available_agents[agent.agent_name]
        _agent.nick_name = agent.nick_name
        _agent.description = agent.description
        _agent.selected_tools = agent.selected_tools
        _agent.prompt = agent.prompt
        _agent.llm_type = agent.llm_type
        await self._save_agent(_agent, flush=True)

        return "success"
    
    async def _save_agents(self, agents: list[Agent], flush=False):
        agent_tasks = [self._save_agent(agent, flush) for agent in agents]
        await asyncio.gather(*agent_tasks)
    
    async def _load_default_agents(self):
        await self._create_agent_by_prebuilt(
            user_id="share",
            name="researcher",
            nick_name="researcher",
            llm_type=AGENT_LLM_MAP["researcher"],
            tools=[tavily_tool, crawl_tool],
            prompt=get_prompt_template("researcher"),
            description="This agent specializes in research tasks by utilizing search engines and web crawling. It can search for information using keywords, crawl specific URLs to extract content, and synthesize findings into comprehensive reports. The agent excels at gathering information from multiple sources, verifying relevance and credibility, and presenting structured conclusions based on collected data.")
        
        await self._create_agent_by_prebuilt(
            user_id="share",
            name="coder",
            nick_name="coder",
            llm_type=AGENT_LLM_MAP["coder"],
            tools=[python_repl_tool, bash_tool],
            prompt=get_prompt_template("coder"),
            description="This agent specializes in software engineering tasks using Python and bash scripting. It can analyze requirements, implement efficient solutions, and provide clear documentation. The agent excels at data analysis, algorithm implementation, system resource management, and environment queries. It follows best practices, handles edge cases, and integrates Python with bash when needed for comprehensive problem-solving.")
        
        await self._create_agent_by_prebuilt(
            user_id="share",
            name="browser",
            nick_name="browser",
            llm_type=AGENT_LLM_MAP["browser"],
            tools=[browser_tool],
            prompt=get_prompt_template("browser"),
            description="This agent specializes in interacting with web browsers. It can navigate to websites, perform actions like clicking, typing, and scrolling, and extract information from web pages. The agent is adept at handling tasks such as searching specific websites, interacting with web elements, and gathering online data. It is capable of operations like logging in, form filling, clicking buttons, and scraping content.")
    
        await self._create_agent_by_prebuilt(
            user_id="share",
            name="reporter",
            nick_name="reporter",
            llm_type=AGENT_LLM_MAP["reporter"],
            tools=[],
            prompt=get_prompt_template("reporter"),
            description="This agent specializes in creating clear, comprehensive reports based solely on provided information and verifiable facts. It presents data objectively, organizes information logically, and highlights key findings using professional language. The agent structures reports with executive summaries, detailed analysis, and actionable conclusions while maintaining strict data integrity and never fabricating information.")

    async def _load_agents(self, user_agent_flag):
        await self._load_default_agents()
        load_tasks = []
        for agent_path in self.agents_dir.glob("*.json"):
            agent_name = agent_path.stem
            if agent_name not in self.available_agents:
                load_tasks.append(self._load_agent(agent_name, user_agent_flag))
        if not USE_BROWSER and "browser" in self.available_agents:
            del self.available_agents["browser"]
        if load_tasks:
            results = await asyncio.gather(*load_tasks, return_exceptions=True)
            for i, result in enumerate(results):
                 if isinstance(result, FileNotFoundError):
                      logger.warning(f"File not found during bulk load for agent: {load_tasks[i]}. Error: {result}")
                 elif isinstance(result, Exception):
                      logger.error(f"Error during bulk load for agent: {load_tasks[i]}. Error: {result}")
    
    async def _list_default_tools(self):
        tools = []
        for tool_name, agent_tool in self.available_tools.items():
            tools.append(Tool(
                name=tool_name,
                description=agent_tool.description,
            ))
        return tools

    async def _list_default_agents(self):
        agents = [agent for agent in self.available_agents.values() if agent.user_id == "share"]
        return agents
    
from src.utils.path_utils import get_project_root

tools_dir = get_project_root() / "store" / "tools"
agents_dir = get_project_root() / "store" / "agents"
prompts_dir = get_project_root() / "store" / "prompts"

agent_manager = AgentManager(tools_dir, agents_dir, prompts_dir)
asyncio.run(agent_manager.initialize())
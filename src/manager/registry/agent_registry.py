import asyncio
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from src.interface.agent import Agent


class AgentRegistry:
    """Unified agent registry backed by in-memory cache + filesystem."""

    def __init__(self, agents_dir: Path, prompt_dir: Path):
        self.agents_dir = Path(agents_dir)
        self.prompt_dir = Path(prompt_dir)
        self._lock = asyncio.Lock()
        self._agents: Dict[str, Agent] = {}

        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_dir.mkdir(parents=True, exist_ok=True)

    async def register(self, agent: Agent, persist: bool = True) -> Agent:
        async with self._lock:
            self._agents[agent.agent_name] = agent
            if persist:
                await self._save_agent_files(agent)
            return agent

    async def get(self, agent_name: str) -> Optional[Agent]:
        async with self._lock:
            return self._agents.get(agent_name)

    async def list(self, user_id: Optional[str] = None, match: Optional[str] = None) -> List[Agent]:
        async with self._lock:
            agents = list(self._agents.values())

        if user_id:
            agents = [a for a in agents if a.user_id == user_id]
        if match:
            agents = [a for a in agents if re.match(match, a.agent_name)]
        return agents

    async def update(self, agent: Agent, persist: bool = True) -> Agent:
        async with self._lock:
            if agent.agent_name not in self._agents:
                raise KeyError(f"Agent not found: {agent.agent_name}")
            self._agents[agent.agent_name] = agent
            if persist:
                await self._save_agent_files(agent)
            return agent

    async def delete(self, agent_name: str, remove_files: bool = True) -> bool:
        async with self._lock:
            existed = agent_name in self._agents
            if existed:
                del self._agents[agent_name]

        if remove_files:
            agent_path = self.agents_dir / f"{agent_name}.json"
            prompt_path = self.prompt_dir / f"{agent_name}.md"
            if agent_path.exists():
                agent_path.unlink()
            if prompt_path.exists():
                prompt_path.unlink()
        return existed

    async def load_agent(self, agent_name: str, user_agent_flag: bool = False) -> Optional[Agent]:
        agent_path = self.agents_dir / f"{agent_name}.json"
        if not agent_path.exists():
            return None

        payload = agent_path.read_text(encoding="utf-8")
        agent = Agent.model_validate_json(payload)
        if agent.user_id == "share" or user_agent_flag:
            async with self._lock:
                self._agents[agent.agent_name] = agent
            return agent
        return None

    async def load_from_disk(self, user_agent_flag: bool = False) -> int:
        loaded = 0
        for agent_path in self.agents_dir.glob("*.json"):
            agent = await self.load_agent(agent_path.stem, user_agent_flag=user_agent_flag)
            if agent is not None:
                loaded += 1
        return loaded

    async def snapshot(self) -> Dict[str, Agent]:
        async with self._lock:
            return dict(self._agents)

    async def _save_agent_files(self, agent: Agent) -> None:
        agent_path = self.agents_dir / f"{agent.agent_name}.json"
        prompt_path = self.prompt_dir / f"{agent.agent_name}.md"

        agent_path.write_text(agent.model_dump_json(indent=4), encoding="utf-8")
        prompt_path.write_text(agent.prompt, encoding="utf-8")

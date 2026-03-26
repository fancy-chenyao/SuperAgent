#!/usr/bin/env python
"""Agent factory for creating remote agent instances."""

from typing import Dict
from .base_agent import BaseRemoteAgent
from .hr_assistant_agent import RemoteHRAssistantAgent
from .knowledge_agent import RemoteKnowledgeAgent
from .document_generator_agent import RemoteDocumentGeneratorAgent
from .report_agent import RemoteReportAgent


class AgentFactory:
    """Factory for creating remote agent instances."""

    _agents: Dict[str, BaseRemoteAgent] = {}

    @classmethod
    def register_agent(cls, agent: BaseRemoteAgent):
        """Register an agent instance."""
        cls._agents[agent.name] = agent

    @classmethod
    def get_agent(cls, agent_name: str) -> BaseRemoteAgent:
        """Get an agent instance by name."""
        if agent_name not in cls._agents:
            raise ValueError(f"Unknown agent: {agent_name}")
        return cls._agents[agent_name]

    @classmethod
    def initialize_all(cls):
        """Initialize and register all agents."""
        cls.register_agent(RemoteHRAssistantAgent())
        cls.register_agent(RemoteKnowledgeAgent())
        cls.register_agent(RemoteDocumentGeneratorAgent())
        cls.register_agent(RemoteReportAgent())


# Initialize all agents on module import
AgentFactory.initialize_all()

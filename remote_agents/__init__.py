"""Remote agents package."""

from .base_agent import BaseRemoteAgent
from .hr_assistant_agent import RemoteHRAssistantAgent
from .knowledge_agent import RemoteKnowledgeAgent
from .document_generator_agent import RemoteDocumentGeneratorAgent
from .report_agent import RemoteReportAgent
from .factory import AgentFactory

__all__ = [
    "BaseRemoteAgent",
    "RemoteHRAssistantAgent",
    "RemoteKnowledgeAgent",
    "RemoteDocumentGeneratorAgent",
    "RemoteReportAgent",
    "AgentFactory",
]

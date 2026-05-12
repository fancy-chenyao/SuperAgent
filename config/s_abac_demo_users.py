"""S-ABAC demo user profiles."""

from __future__ import annotations

from typing import Any, Dict, List


DEMO_USERS: Dict[str, Dict[str, Any]] = {
    "admin": {
        "display_name": "Admin (System Admin)",
        "role": "UniversalAssistant",
        "department": "System",
        "job_role": "system_orchestrator",
        "clearance_level": 5,
        "trust_level": "HIGH",
        "grants": ["all"],
        "description": "Full system access. Can dispatch any agent and use any tool.",
        "available_agents": "*",
        "icon": "🛡",
    },
    "hr_manager": {
        "display_name": "HR Manager (Zhang Wei)",
        "role": "HRAgent",
        "department": "HR",
        "job_role": "hr_manager",
        "clearance_level": 3,
        "trust_level": "HIGH",
        "grants": ["employee_profile_read", "salary_read", "document_generate"],
        "description": "HR manager with access to personnel and salary workflows.",
        "available_agents": [
            "RemoteHRAssistantAgent",
            "RemoteDocumentGeneratorAgent",
            "RemoteKnowledgeAgent",
            "reporter",
            "researcher",
        ],
        "icon": "👔",
    },
    "engineer": {
        "display_name": "Engineer (Li Ming)",
        "role": "CodeAgent",
        "department": "Engineering",
        "job_role": "engineer",
        "clearance_level": 3,
        "trust_level": "HIGH",
        "grants": ["code_execute", "file_write", "research_read"],
        "description": "Software engineer. Can use code execution, search, and browser tools.",
        "available_agents": [
            "coder",
            "researcher",
            "browser",
            "reporter",
        ],
        "icon": "💻",
    },
    "researcher_user": {
        "display_name": "Researcher (Wang Fang)",
        "role": "ResearchAgent",
        "department": "Research",
        "job_role": "research_analyst",
        "clearance_level": 2,
        "trust_level": "MEDIUM",
        "grants": ["research_read"],
        "description": "Research analyst. Can use search and crawler tools.",
        "available_agents": [
            "researcher",
            "browser",
            "reporter",
        ],
        "icon": "🔎",
    },
    "guest": {
        "display_name": "Guest (Limited Access)",
        "role": "UniversalAssistant",
        "department": "General",
        "job_role": "guest",
        "clearance_level": 1,
        "trust_level": "LOW",
        "grants": [],
        "description": "Guest user with minimal access. Can only use basic search.",
        "available_agents": [
            "researcher",
        ],
        "icon": "👤",
    },
    "communication_officer": {
        "display_name": "Comm Officer (Zhao Min)",
        "role": "CommunicationAgent",
        "department": "Office",
        "job_role": "communication_officer",
        "clearance_level": 3,
        "trust_level": "HIGH",
        "grants": ["document_generate", "external_send", "notification_send"],
        "description": "Communication officer. Can send emails and generate documents in matching scenarios.",
        "available_agents": [
            "RemoteCommunicationAgent",
            "RemoteEmailDispatchAgent",
            "RemoteDocumentGeneratorAgent",
            "RemoteOfficeAssistantAgent",
            "researcher",
            "reporter",
        ],
        "icon": "📣",
    },
}


def get_demo_user(user_id: str) -> Dict[str, Any] | None:
    """Return the demo user profile for the given user_id, or None."""
    return DEMO_USERS.get(user_id)


def list_demo_users() -> List[Dict[str, Any]]:
    """Return all demo user profiles."""
    return [
        {
            "user_id": uid,
            "display_name": profile["display_name"],
            "role": profile["role"],
            "department": profile["department"],
            "job_role": profile["job_role"],
            "clearance_level": profile["clearance_level"],
            "trust_level": profile["trust_level"],
            "grants": profile["grants"],
            "description": profile["description"],
            "icon": profile["icon"],
        }
        for uid, profile in DEMO_USERS.items()
    ]


def get_user_available_agents(user_id: str) -> List[str]:
    """Return the list of agent names available to a given demo user."""
    profile = DEMO_USERS.get(user_id)
    if not profile:
        return []
    agents = profile.get("available_agents", [])
    if agents == "*":
        return ["*"]
    return agents

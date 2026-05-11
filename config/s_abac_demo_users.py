"""S-ABAC Demo User Profiles.

Maps user_id to simulated user profiles for the web demo.
Each profile defines the user's role, clearance level, and which
agents/tools they can access.
"""

from typing import Any, Dict, List

# Demo user profiles keyed by user_id.
# In production these would come from an identity provider / directory.
DEMO_USERS: Dict[str, Dict[str, Any]] = {
    "admin": {
        "display_name": "Admin (System Admin)",
        "role": "UniversalAssistant",
        "department": "System",
        "clearance_level": 5,
        "trust_level": "HIGH",
        "description": "Full system access. Can dispatch any agent and use any tool.",
        "available_agents": "*",  # wildcard = all agents
        "icon": "👑",
    },
    "hr_manager": {
        "display_name": "HR Manager (Zhang Wei)",
        "role": "HRAgent",
        "department": "HR",
        "clearance_level": 3,
        "trust_level": "HIGH",
        "description": "HR department manager. Can access HR tools, personnel data, and document generation.",
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
        "clearance_level": 3,
        "trust_level": "HIGH",
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
        "clearance_level": 2,
        "trust_level": "MEDIUM",
        "description": "Research analyst. Can use search and crawler tools. Cannot execute code or send emails.",
        "available_agents": [
            "researcher",
            "browser",
            "reporter",
        ],
        "icon": "🔍",
    },
    "guest": {
        "display_name": "Guest (Limited Access)",
        "role": "UniversalAssistant",
        "department": "General",
        "clearance_level": 1,
        "trust_level": "LOW",
        "description": "Guest user with minimal access. Can only use basic search. Most tools require approval.",
        "available_agents": [
            "researcher",
        ],
        "icon": "👤",
    },
    "communication_officer": {
        "display_name": "Comm Officer (Zhao Min)",
        "role": "CommunicationAgent",
        "department": "Office",
        "clearance_level": 3,
        "trust_level": "HIGH",
        "description": "Communication officer. Can send emails and generate documents. Email sending requires approval.",
        "available_agents": [
            "RemoteCommunicationAgent",
            "RemoteEmailDispatchAgent",
            "RemoteDocumentGeneratorAgent",
            "RemoteOfficeAssistantAgent",
            "researcher",
            "reporter",
        ],
        "icon": "📧",
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
            "clearance_level": profile["clearance_level"],
            "trust_level": profile["trust_level"],
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
        return ["*"]  # special marker for all agents
    return agents

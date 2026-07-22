from src.utils.path_utils import get_project_root
from src.service import env as _env

workflow_dir = get_project_root() / "store" / "workflows"

tools_dir = get_project_root() / "store" / "tools"
agents_dir = get_project_root() / "store" / "agents"
prompts_dir = get_project_root() / "store" / "prompts"
workflows_dir = get_project_root() / "store" / "workflows"
checkpoints_dir = get_project_root() / "store" / "checkpoints"
task_logs_dir = get_project_root() / "store" / "task_logs"
memory_dir = get_project_root() / "store" / "memory"

context_variables = {
    "has_lauched": False
}

# Toggle Mermaid workflow visualization generation.
# Set to True to enable, False to disable.
mermaid_enabled = True

# Execution-engine feature flags. Values are sourced from .env via
# src.service.env (single source of truth); default OFF keeps the legacy
# publisher/while behavior (B1) unchanged. Consumers keep importing these
# lowercase names from config.global_variables.
# Phase 2: agent_proxy captures each executed step's output as a typed Artifact.
artifact_capture_enabled = _env.ARTIFACT_CAPTURE_ENABLED

# Phase 3: route the workflow through the TaskGraph scheduler when the state
# carries an explicit task graph (falls back to the legacy loop otherwise).
orchestration_scheduler_enabled = _env.ORCHESTRATION_SCHEDULER_ENABLED

system_agents = {
        "coordinator": {
            "type": "system_agent",
            "name": "coordinator",
            "description": "Coordinator node that communicate with customers."
        },
        "planner": {
            "type": "system_agent",
            "name": "planner",
            "description": "Planner node that plan the task."
        },
        "publisher": {
            "type": "system_agent",
            "name": "publisher",
            "description": "Publisher node that publish the task."
        },
}

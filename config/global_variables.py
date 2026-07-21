from src.utils.path_utils import get_project_root

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

# Execution-engine feature flags (default OFF: main behavior unchanged, B1 preserved).
# Phase 2: agent_proxy captures each executed step's output as a typed Artifact.
artifact_capture_enabled = False

# Phase 3: route the workflow through the TaskGraph scheduler when the state
# carries an explicit task graph (falls back to the legacy loop otherwise).
orchestration_scheduler_enabled = False

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

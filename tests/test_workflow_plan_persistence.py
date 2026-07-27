import json

import src.workflow.cache as cache_module
from src.workflow.cache import WorkflowCache


def _isolated_cache(workflow_dir):
    cache = object.__new__(WorkflowCache)
    cache.workflow_dir = workflow_dir
    cache.queue = {}
    cache.cache = {}
    cache.latest_polish_id = {}
    cache._lock_pool = {}
    cache.initialized = True
    return cache


def test_restore_planning_steps_persists_for_next_execution_request(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cache_module, "mermaid_enabled", False)
    workflow_dir = tmp_path / "workflows"
    (workflow_dir / "admin").mkdir(parents=True)
    workflow_id = "admin:workflow-1"
    steps = [
        {
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询员工信息",
        },
        {
            "agent_name": "RemoteDocumentGeneratorAgent",
            "title": "生成收入证明",
        },
    ]

    planning_cache = _isolated_cache(workflow_dir)
    planning_cache.cache[workflow_id] = {
        "workflow_id": workflow_id,
        "planning_steps": [],
        "nodes": {},
        "graph": [],
    }

    planning_cache.restore_planning_steps(workflow_id, steps, "admin")

    workflow_path = workflow_dir / "admin" / "workflow-1.json"
    persisted = json.loads(workflow_path.read_text(encoding="utf-8"))
    assert persisted["planning_steps"] == steps

    execution_cache = _isolated_cache(workflow_dir)
    execution_cache._load_workflow("admin")
    assert execution_cache.get_planning_steps(workflow_id) == steps


def test_new_chat_workflow_adds_workflow_and_message_timestamps(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cache_module, "mermaid_enabled", False)
    workflow_dir = tmp_path / "workflows"
    cache = _isolated_cache(workflow_dir)

    cache.init_cache(
        user_id="admin",
        lap=1,
        mode="launch",
        workflow_id="admin:today",
        version=1,
        user_input_messages=[
            {"role": "user", "content": "查询客户授信风险"}
        ],
        deep_thinking_mode=True,
        search_before_planning=False,
        coor_agents=[],
    )

    workflow = cache.cache["admin:today"]
    assert workflow["created_at"]
    assert workflow["updated_at"]
    assert workflow["user_input_messages"][0]["timestamp"]

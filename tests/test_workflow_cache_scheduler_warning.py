import json
import logging

import pytest

import src.workflow.cache as cache_mod
from src.workflow.cache import WorkflowCache


@pytest.mark.parametrize(
    ("scheduler_enabled", "expects_warning"),
    [(True, False), (False, True)],
)
def test_legacy_empty_queue_warning_respects_scheduler_mode(
    tmp_path,
    monkeypatch,
    caplog,
    scheduler_enabled,
    expects_warning,
):
    workflow_id = "u1:empty"
    user_dir = tmp_path / "u1"
    user_dir.mkdir()
    (user_dir / "empty.json").write_text(
        json.dumps({"workflow_id": workflow_id, "graph": []}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cache_mod,
        "orchestration_scheduler_enabled",
        scheduler_enabled,
    )
    monkeypatch.setattr(WorkflowCache, "_instance", None)
    cache = WorkflowCache(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=cache_mod.logger.name):
        cache.init_cache(
            user_id="u1",
            lap=1,
            mode="production",
            workflow_id=workflow_id,
            version=1,
            user_input_messages=[],
            deep_thinking_mode=False,
            search_before_planning=False,
            coor_agents=[],
        )

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "Execution queue is empty" in record.getMessage()
    ]
    assert bool(warnings) is expects_warning


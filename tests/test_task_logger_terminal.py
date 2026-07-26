import asyncio

import pytest

import src.orchestration.runtime as runtime_mod
import src.robust.task_logger as task_logger_mod
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep, WorkflowStatus
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.scheduler import WorkflowResult
from src.orchestration.failure_mapper import make_failure
from src.robust.task_logger import TaskLogger
from src.service.web_app import _finalize_disconnected_task


@pytest.fixture(autouse=True)
def _isolate_runtime_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        task_logger_mod,
        "checkpoints_dir",
        tmp_path / "checkpoints",
    )
    monkeypatch.setenv(
        "ARTIFACT_PAYLOAD_STORE_DIR",
        str(tmp_path / "artifacts"),
    )
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    monkeypatch.setattr(
        "src.service.env.S_ABAC_ENABLED",
        False,
        raising=False,
    )


@pytest.mark.parametrize("status", list(WorkflowStatus))
def test_task_logger_persists_scheduler_terminal_status_and_finished_at(status):
    logger = TaskLogger(
        task_id=f"task-{status.value.lower()}",
        workflow_id="wf-terminal",
    )
    logger.log_workflow_terminal(status, error=None)
    finished_at = logger.finished_at
    history_size = len(logger.history)

    assert logger.status == status.value
    assert finished_at

    logger.log_workflow_terminal(status, error="must not overwrite")
    assert logger.finished_at == finished_at
    assert len(logger.history) == history_size

    loaded = TaskLogger.load(logger.task_id)
    assert loaded is not None
    assert loaded.status == status.value
    assert loaded.finished_at == finished_at


def test_task_logger_persists_structured_failure_without_finalizing_early():
    logger = TaskLogger(task_id="task-structured-failure", workflow_id="wf-terminal")
    failure = make_failure(
        "UPSTREAM_STEP_FAILED",
        step_id="report_step",
        blocked_by=["hr_step"],
    )

    logger.log_failure(failure.model_dump(mode="json"), step=2)

    assert logger.status == "running"
    assert logger.failures == [failure.model_dump(mode="json")]
    assert logger.history[-1]["event"] == "step_failure"
    assert logger.history[-1]["failure_code"] == "UPSTREAM_STEP_FAILED"

    loaded = TaskLogger.load(logger.task_id)
    assert loaded is not None
    assert loaded.status == "running"
    assert loaded.failures[0]["blocked_by"] == ["hr_step"]


@pytest.mark.parametrize("status", list(WorkflowStatus))
def test_runtime_end_status_matches_task_logger(status, monkeypatch):
    class _TerminalScheduler:
        def __init__(self, **_kwargs):
            pass

        async def run(self, *_args, **_kwargs):
            return WorkflowResult({}, terminal_status=status)

    monkeypatch.setattr(runtime_mod, "TaskScheduler", _TerminalScheduler)
    task_id = f"runtime-{status.value.lower()}"
    logger = TaskLogger(task_id=task_id, workflow_id="wf-terminal")
    state = {
        "workflow_id": "wf-terminal",
        "user_id": "u1",
        "task_graph": TaskGraph(
            spec=TaskSpec(task_id=task_id),
            steps=[],
        ),
        "messages": [],
    }

    async def _run():
        return [
            event
            async for event in runtime_mod.run_scheduler_workflow(
                state,
                task_id=task_id,
                task_logger=logger,
                execute_step=lambda **_kwargs: None,
                routing_provider=StubRoutingProvider(),
            )
        ]

    events = asyncio.run(_run())
    assert events[-1]["event"] == "end_of_workflow"
    assert events[-1]["data"]["status"] == status.value

    loaded = TaskLogger.load(task_id)
    assert loaded is not None
    assert loaded.status == status.value
    assert loaded.finished_at


def test_scheduler_generator_close_marks_running_task_failed():
    logger = TaskLogger(task_id="task-cancel", workflow_id="wf-cancel")
    wait_forever = asyncio.Event()
    graph = TaskGraph(
        spec=TaskSpec(task_id="task-cancel"),
        steps=[
            TaskStep(
                step_id="step_1",
                agent_name="A",
                preferred_resource_id="A",
                expected_outputs=["result"],
            )
        ],
    )
    state = {
        "workflow_id": "wf-cancel",
        "user_id": "u1",
        "task_graph": graph,
        "messages": [],
    }

    async def _wait_execute(**_kwargs):
        await wait_forever.wait()

    async def _run_and_close():
        stream = runtime_mod.run_scheduler_workflow(
            state,
            task_id="task-cancel",
            task_logger=logger,
            execute_step=_wait_execute,
            routing_provider=StubRoutingProvider(),
        )
        assert (await stream.__anext__())["event"] == "start_of_workflow"
        assert (await stream.__anext__())["event"] == "start_of_agent"
        await stream.aclose()

    asyncio.run(_run_and_close())

    loaded = TaskLogger.load("task-cancel")
    assert loaded is not None
    assert loaded.status == "FAILED"
    assert loaded.finished_at
    assert "cancelled" in (loaded.error or "")


def test_client_disconnect_finalizer_closes_running_task():
    logger = TaskLogger(task_id="task-disconnect", workflow_id="wf-disconnect")
    logger.log_workflow_start("test")

    _finalize_disconnected_task(
        "task-disconnect",
        "client disconnected before workflow completion",
    )

    loaded = TaskLogger.load("task-disconnect")
    assert loaded is not None
    assert loaded.status == "FAILED"
    assert loaded.finished_at
    assert "client disconnected" in (loaded.error or "")

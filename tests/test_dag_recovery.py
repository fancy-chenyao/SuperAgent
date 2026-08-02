import asyncio

from src.interface.artifact import StepResult, StepStatus
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.recovery import (
    apply_dag_recovery_state,
    build_dag_recovery_plan,
    classify_failure,
    retry_delay_seconds,
)
from src.orchestration.runtime import run_scheduler_workflow
from src.robust.hooks.base import HookResult


def _graph() -> TaskGraph:
    return TaskGraph(
        spec=TaskSpec(task_id="dag-recovery"),
        steps=[
            TaskStep(step_id="keep", preferred_resource_id="A"),
            TaskStep(step_id="failed", preferred_resource_id="B"),
            TaskStep(
                step_id="downstream",
                depends_on=["failed"],
                preferred_resource_id="C",
            ),
        ],
    )


def test_dag_recovery_keeps_independent_success_and_retries_downstream():
    graph = _graph()
    results = {
        "keep": StepResult(step_id="keep", status=StepStatus.SUCCEEDED),
        "failed": StepResult(
            step_id="failed",
            status=StepStatus.FAILED,
            error="temporary network unavailable",
        ),
    }

    plan = build_dag_recovery_plan(
        graph, results, completed_steps={"keep"}
    )

    assert plan.automatic is True
    assert plan.keep_steps == ["keep"]
    assert plan.retry_steps == ["failed", "downstream"]
    state = {
        "completed_steps": ["keep", "failed", "downstream"],
        "step_results": {sid: {"status": "SUCCEEDED"} for sid in (
            "keep", "failed", "downstream"
        )},
        "skill_step_evidence": {sid: {} for sid in (
            "keep", "failed", "downstream"
        )},
    }
    apply_dag_recovery_state(state, plan, attempt=1)
    assert state["completed_steps"] == ["keep"]
    assert set(state["step_results"]) == {"keep"}
    assert set(state["skill_step_evidence"]) == {"keep"}


def test_skipped_side_effect_descendant_does_not_block_read_failure_recovery():
    graph = TaskGraph(
        spec=TaskSpec(task_id="read-then-write"),
        steps=[
            TaskStep(
                step_id="read",
                preferred_resource_id="Reader",
                operation_mode="read",
            ),
            TaskStep(
                step_id="write",
                depends_on=["read"],
                preferred_resource_id="Writer",
                operation_mode="write",
            ),
        ],
    )
    results = {
        "read": StepResult(
            step_id="read",
            status=StepStatus.FAILED,
            error="temporary network unavailable",
        ),
        "write": StepResult(
            step_id="write",
            status=StepStatus.SKIPPED,
            error="dependency failed: read",
        ),
    }

    plan = build_dag_recovery_plan(graph, results, completed_steps=set())

    assert plan.automatic is True
    assert plan.reason_code == "DAG_BRANCH_SAFE_TO_RETRY"
    assert plan.failed_steps == ["read"]
    assert plan.retry_steps == ["read", "write"]
    assert set(plan.classifications) == {"read"}


def test_reconciliation_and_permission_failures_never_auto_recover():
    for metrics in (
        {"needs_reconciliation": True},
        {"permission_denied": True},
        {"approval_required": True},
        {"persistence_failed": True},
    ):
        classification = classify_failure(
            "failure", metrics, read_only=False
        )
        assert classification.retryable is False


def test_completed_side_effect_in_invalidated_branch_requires_compensation():
    graph = TaskGraph(
        spec=TaskSpec(task_id="compensation"),
        steps=[
            TaskStep(
                step_id="write",
                operation_mode="write",
                compensation_action={"tool": "delete_generated_document"},
            ),
            TaskStep(step_id="verify", depends_on=["write"]),
        ],
    )
    results = {
        "write": StepResult(
            step_id="write",
            status=StepStatus.FAILED,
            error="validation failed",
            metrics={"safe_to_retry": True},
        )
    }
    plan = build_dag_recovery_plan(
        graph, results, completed_steps={"write"}
    )
    assert plan.automatic is False
    assert plan.reason_code == "COMPENSATION_CONFIRMATION_REQUIRED"
    assert plan.compensation_actions[0]["step_id"] == "write"


def test_retry_delay_is_exponential_bounded_and_deterministic():
    assert retry_delay_seconds(
        1,
        base_seconds=1,
        max_seconds=3,
        jitter_ratio=0,
    ) == 1
    assert retry_delay_seconds(
        2,
        base_seconds=1,
        max_seconds=3,
        jitter_ratio=0,
    ) == 2
    assert retry_delay_seconds(
        3,
        base_seconds=1,
        max_seconds=3,
        jitter_ratio=0,
    ) == 3


def test_runtime_auto_recovers_only_failed_dag_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_RECOVERY_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_AUTO_RECOVERY_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("SCHEDULER_RETRY_BASE_SECONDS", "0")
    monkeypatch.setenv("ARTIFACT_PAYLOAD_STORE_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    calls = {"keep": 0, "failed": 0, "downstream": 0}

    async def execute_step(*, step, **_kwargs):
        calls[step.step_id] += 1
        if step.step_id == "failed" and calls["failed"] == 1:
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error="temporary network unavailable",
            )
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={"ok": step.step_id},
        )

    class HookSpy:
        def __init__(self):
            self.points = []

        async def process(self, ctx):
            self.points.append(ctx.hook_point.value)
            return HookResult()

    hook_spy = HookSpy()
    state = {
        "workflow_id": "wf-recovery",
        "user_id": "u1",
        "task_graph": _graph(),
        "messages": [],
    }

    async def collect():
        return [
            event
            async for event in run_scheduler_workflow(
                state,
                task_id="dag-recovery",
                execute_step=execute_step,
                routing_provider=StubRoutingProvider(),
                hook_engine=hook_spy,
            )
        ]

    events = asyncio.run(collect())
    assert calls == {"keep": 1, "failed": 2, "downstream": 1}
    assert events[-1]["event"] == "end_of_workflow"
    assert events[-1]["data"]["status"] == "SUCCEEDED"
    assert any(event["event"] == "recovery_plan" for event in events)
    assert any(event["event"] == "recovery_started" for event in events)
    assert "step_start" in hook_spy.points
    assert "step_failed" in hook_spy.points
    assert "step_end" in hook_spy.points
    assert "workflow_end" in hook_spy.points


def test_runtime_emits_retry_schedule_with_reason_and_delay(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AUTO_RECOVERY_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_RETRY_BASE_SECONDS", "0")
    monkeypatch.setenv("SCHEDULER_RETRY_JITTER_RATIO", "0")
    monkeypatch.setenv("ARTIFACT_PAYLOAD_STORE_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    calls = 0

    async def execute_step(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error="service temporarily unavailable",
            )
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={"ok": True},
        )

    graph = TaskGraph(
        spec=TaskSpec(task_id="retry-events"),
        steps=[
            TaskStep(
                step_id="read",
                retry=1,
                preferred_resource_id="A",
            )
        ],
    )
    state = {
        "workflow_id": "wf-retry",
        "user_id": "u1",
        "task_graph": graph,
        "messages": [],
    }

    async def collect():
        return [
            event
            async for event in run_scheduler_workflow(
                state,
                task_id="retry-events",
                execute_step=execute_step,
                routing_provider=StubRoutingProvider(),
            )
        ]

    events = asyncio.run(collect())
    retry = next(
        event["data"]
        for event in events
        if event["event"] == "retry_scheduled"
    )
    assert retry["reason_code"] == "TRANSIENT_EXTERNAL_FAILURE"
    assert retry["attempt"] == 1
    assert retry["next_attempt"] == 2
    assert events[-1]["data"]["status"] == "SUCCEEDED"

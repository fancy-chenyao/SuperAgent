"""Acceptance tests for the P0 execution-engine fixes.

Covers the review's acceptance matrix:

- T1  main agent REJECT   -> preferred agent is NOT dispatched, step fails
- T2  main agent CLARIFY  -> clarification surfaced, nothing executed
- T3  no capable agent    -> preferred agent cannot be used as a bypass
- T5  s1 ok, s2 fail then resume -> s2 receives the full upstream artifact
- T6  artifact missing on resume -> fail closed (no empty-input execution)
"""

import asyncio

import pytest

from src.interface.artifact import StepStatus
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.providers import RoutingResult
from src.orchestration.runtime import run_scheduler_workflow
from src.orchestration.scheduler import TaskScheduler


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path, monkeypatch):
    """Redirect the protected payload + receipt stores to a per-test tmp dir so
    the two resume runs share state without polluting the repo ``store/``."""
    monkeypatch.setenv("ARTIFACT_PAYLOAD_STORE_DIR",
                       str(tmp_path / "artifacts"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    # These acceptance cases use the synthetic subject ``u1``. Keep them
    # independent of a developer's local S-ABAC opt-in setting.
    monkeypatch.setattr(
        "src.service.env.S_ABAC_ENABLED", False, raising=False
    )


class _RecordingExecutor:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, *, step, selected_agent, inputs, context):
        self.calls.append(step.step_id)
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"ok": step.step_id})


class _FixedRouting:
    """Routing provider returning a fixed verdict for every step."""

    def __init__(self, decision, *, clarification=None, reason_codes=None):
        self._decision = decision
        self._clarification = clarification
        self._reason_codes = reason_codes or []

    async def decide(self, step, **kwargs):
        return RoutingResult(
            selected_agent=None,
            decision=self._decision,
            clarification=self._clarification,
            reason_codes=self._reason_codes,
        )


def _one_step_graph():
    return TaskGraph(
        spec=TaskSpec(task_id="t"),
        steps=[TaskStep(
            step_id="s1", preferred_resource_id="PreferredAgent", agent_name="PreferredAgent")],
    )


def _run(execute, graph, routing):
    sched = TaskScheduler(execute_step=execute, routing_provider=routing)
    return asyncio.run(sched.run(graph, context={"task_id": "t"}))


def test_t1_reject_does_not_dispatch_preferred_agent():
    execute = _RecordingExecutor()
    results = _run(execute, _one_step_graph(), _FixedRouting(
        "REJECT", reason_codes=["NO_CAPABLE_AGENT"]))
    assert execute.calls == []  # preferred agent NEVER executed
    assert results["s1"].status == StepStatus.FAILED
    assert results["s1"].metrics.get("routing_decision") == "REJECT"


def test_t2_clarify_surfaces_question_and_executes_nothing():
    execute = _RecordingExecutor()
    results = _run(
        execute,
        _one_step_graph(),
        _FixedRouting("CLARIFY", clarification="请提供收件人邮箱"),
    )
    assert execute.calls == []
    assert results["s1"].status == StepStatus.FAILED
    assert results["s1"].metrics.get("clarify") is True
    assert results["s1"].metrics.get("clarification") == "请提供收件人邮箱"


def test_t3_no_capable_agent_cannot_bypass_via_preferred():
    execute = _RecordingExecutor()
    results = _run(execute, _one_step_graph(),
                   _FixedRouting("NO_CAPABLE_AGENT"))
    assert execute.calls == []
    assert results["s1"].status == StepStatus.FAILED
    assert results["s1"].metrics.get("routing_decision") == "NO_CAPABLE_AGENT"


# --------------------------------------------------------------------------- #
# Resume across two run_scheduler_workflow invocations (T5 / T6)
# --------------------------------------------------------------------------- #
class _ResumeExecutor:
    """s1 always succeeds; s2 fails on its first attempt, succeeds on resume."""

    def __init__(self):
        self.s2_attempts = 0
        self.received: dict[str, dict] = {}

    async def __call__(self, *, step, selected_agent, inputs, context):
        self.received[step.step_id] = dict(inputs)
        if step.step_id == "s1":
            return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"data_a": "hello-from-s1"})
        # s2
        self.s2_attempts += 1
        if self.s2_attempts == 1:
            return ExecuteResult(status=ExecutionStatus.FAILED, error="boom")
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"done": True})


def _resume_state():
    graph = TaskGraph(
        spec=TaskSpec(task_id="task-resume"),
        steps=[
            TaskStep(
                step_id="s1", preferred_resource_id="A", agent_name="A",
                expected_outputs=["data_a"], operation_mode="read",
            ),
            TaskStep(
                step_id="s2", depends_on=["s1"], preferred_resource_id="B", agent_name="B",
                operation_mode="read",
                input_bindings=[
                    {"parameter_name": "upstream",
                        "source_step": "A", "source_output": "data_a"}
                ],
            ),
        ],
    )
    return {"workflow_id": "wf-resume", "user_id": "u1", "task_graph": graph, "messages": []}


def _drain(state, execute, task_id="task-resume"):
    from src.orchestration.providers import StubRoutingProvider

    async def _go():
        events = []
        async for ev in run_scheduler_workflow(
            state, task_id=task_id, execute_step=execute, routing_provider=StubRoutingProvider()
        ):
            events.append(ev)
        return events

    return asyncio.run(_go())


def test_t5_resume_downstream_receives_full_upstream_artifact():
    state = _resume_state()
    execute = _ResumeExecutor()

    # First run: s1 succeeds, s2 fails.
    events1 = _drain(state, execute)
    assert events1[-1]["data"]["status"] == "PARTIAL_FAILED"
    assert state["completed_steps"] == ["s1"]
    assert state.get("artifacts")  # payload persisted for resume

    # Second run: s1 skipped, s2 resumes and MUST see s1's real output.
    events2 = _drain(state, execute)
    assert events2[-1]["data"]["status"] == "SUCCEEDED"
    assert execute.received["s2"]["upstream"] == {"data_a": "hello-from-s1"}
    evidence = events2[-1]["data"]["skill_execution_evidence"]
    assert evidence["technical_success"] is True
    assert evidence["step_coverage"] == 1.0
    assert {step["step_id"] for step in evidence["steps"]} == {"s1", "s2"}


def test_t6_resume_with_missing_artifact_fails_closed():
    state = _resume_state()
    execute = _ResumeExecutor()

    _drain(state, execute)
    assert state["completed_steps"] == ["s1"]

    # Simulate lost artifact payloads (e.g. corrupted checkpoint): the resumed
    # downstream step must fail closed, NOT run with an empty input.
    state["artifacts"] = {}
    s2_calls_before = execute.s2_attempts
    events2 = _drain(state, execute)

    # s2 never executed with empty input
    assert execute.s2_attempts == s2_calls_before
    assert events2[-1]["data"]["status"] == "FAILED"
    s2_result = state["step_results"]["s2"]
    assert s2_result["metrics"].get("input_error") == "artifact_not_found"

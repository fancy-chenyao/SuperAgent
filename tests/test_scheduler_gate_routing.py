"""C1 tests: pre-flight routing, global clarification, and illegal-routing gates.

Exercises the scheduler with a per-step routing provider so the workflow-level
verdicts (CLARIFY_REQUIRED / REJECTED / DISPATCH-without-agent) are asserted
without the real agent/LLM stack.
"""

import asyncio

from src.interface.artifact import StepStatus
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep, WorkflowStatus
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.providers import RoutingResult
from src.orchestration.scheduler import TaskScheduler


class _RecordingExecutor:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, *, step, selected_agent, inputs, context):
        self.calls.append(step.step_id)
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"ok": step.step_id})


class _MapRouting:
    """Return a fixed verdict per step id."""

    def __init__(self, verdicts: dict[str, RoutingResult]):
        self._verdicts = verdicts

    async def decide(self, step, **kwargs):
        return self._verdicts[step.step_id]


def _step(step_id, deps=None, mode="read", **extra):
    return TaskStep(step_id=step_id, depends_on=deps or [], operation_mode=mode, **extra)


def _graph(*steps):
    return TaskGraph(spec=TaskSpec(task_id="t"), steps=list(steps))


def _run(execute, graph, routing, **run_kwargs):
    sched = TaskScheduler(execute_step=execute, routing_provider=routing)
    return asyncio.run(sched.run(graph, context={"task_id": "t"}, **run_kwargs))


def test_global_clarify_blocks_a_ready_email_step():
    """A CLARIFY on any step halts the whole workflow before a READY send step
    (email) can run: the email executor must be called 0 times."""
    verdicts = {
        "ask": RoutingResult(selected_agent=None, decision="CLARIFY", clarification="收件人邮箱？"),
        "email": RoutingResult(selected_agent="EmailAgent", decision="DISPATCH"),
    }
    execute = _RecordingExecutor()
    # Both steps are independent -> both READY in the first frontier.
    result = _run(execute, _graph(_step("ask"), _step(
        "email", mode="send")), _MapRouting(verdicts))

    assert execute.calls == []  # NOTHING executed, email never sent
    assert result.terminal_status == WorkflowStatus.CLARIFY_REQUIRED
    assert result["ask"].status == StepStatus.FAILED
    assert result["ask"].metrics.get("clarify") is True
    assert "收件人邮箱？" in result.clarifications


def test_dispatch_without_agent_does_not_start_hook_or_execute():
    verdicts = {"s": RoutingResult(selected_agent=None, decision="DISPATCH")}
    execute = _RecordingExecutor()
    started: list[str] = []

    async def _on_start(*, step, selected_agent, inputs):
        started.append(step.step_id)

    result = _run(execute, _graph(_step("s")), _MapRouting(
        verdicts), on_step_start=_on_start)

    assert execute.calls == []  # executor never invoked
    assert started == []  # start hook never invoked
    assert result["s"].status == StepStatus.FAILED
    assert result["s"].metrics.get("routing_decision") == "DISPATCH_NO_AGENT"


def test_reject_isolates_branch_but_independent_readonly_survives():
    verdicts = {
        "reject": RoutingResult(selected_agent=None, decision="REJECT", reason_codes=["NO_CAPABLE_AGENT"]),
        "down": RoutingResult(selected_agent="X", decision="DISPATCH"),
        "read1": RoutingResult(selected_agent="R", decision="DISPATCH"),
        "read2": RoutingResult(selected_agent="R", decision="DISPATCH"),
    }
    execute = _RecordingExecutor()
    g = _graph(
        _step("reject"),
        _step("down", deps=["reject"]),
        _step("read1"),
        _step("read2", deps=["read1"]),
    )
    result = _run(execute, g, _MapRouting(verdicts))

    assert result["reject"].status == StepStatus.FAILED
    assert "down" not in result  # downstream of the rejected step is blocked
    assert result["read1"].is_success
    # independent read-only branch keeps running
    assert result["read2"].is_success
    assert result.terminal_status == WorkflowStatus.PARTIAL_FAILED

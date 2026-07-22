"""Unit tests for the TaskGraph scheduler (Plan Phase 3).

Uses a fake ``execute_step`` so the scheduler is exercised without the real
agent runtime. Coroutines are driven with ``asyncio.run`` to avoid a
pytest-asyncio dependency. Concurrency is asserted via a peak-in-flight counter.
"""

import asyncio

from src.interface.artifact import StepStatus
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.scheduler import TaskScheduler


class FakeExecutor:
    def __init__(self, sleep: float = 0.02):
        self.calls: list[str] = []
        self.received: dict[str, dict] = {}
        self.concurrent = 0
        self.peak = 0
        self.sleep = sleep
        self.fail_ids: set[str] = set()
        self.fail_once: dict[str, int] = {}
        self.timeout_ids: set[str] = set()

    async def __call__(self, *, step, selected_agent, inputs, context):
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        self.calls.append(step.step_id)
        self.received[step.step_id] = {"agent": selected_agent, "inputs": dict(inputs)}
        try:
            if step.step_id in self.timeout_ids:
                await asyncio.sleep(1.0)  # exceed step.timeout
            await asyncio.sleep(self.sleep)
            if step.step_id in self.fail_ids:
                return ExecuteResult(status=ExecutionStatus.FAILED, error="boom")
            if self.fail_once.get(step.step_id, 0) > 0:
                self.fail_once[step.step_id] -= 1
                return ExecuteResult(status=ExecutionStatus.FAILED, error="transient")
            return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"ok": step.step_id})
        finally:
            self.concurrent -= 1


def _step(step_id, deps=None, mode="read", **extra):
    return TaskStep(step_id=step_id, depends_on=deps or [], operation_mode=mode, **extra)


def _graph(*steps, task_id="t"):
    return TaskGraph(spec=TaskSpec(task_id=task_id), steps=list(steps))


def _run(execute_step, graph, routing=None, **run_kwargs):
    sched = TaskScheduler(execute_step=execute_step, routing_provider=routing)
    return asyncio.run(sched.run(graph, **run_kwargs))


def test_serial_chain_runs_in_order_no_overlap():
    fake = FakeExecutor()
    g = _graph(_step("a"), _step("b", ["a"]), _step("c", ["b"]))
    results = _run(fake, g)
    assert fake.calls == ["a", "b", "c"]
    assert fake.peak == 1
    assert all(r.is_success for r in results.values())


def test_independent_reads_run_in_parallel():
    fake = FakeExecutor()
    g = _graph(_step("a"), _step("b"), _step("c"))  # no deps -> all ready
    _run(fake, g)
    assert fake.peak >= 2  # ran concurrently


def test_writes_sharing_lock_are_serialized():
    fake = FakeExecutor()
    g = _graph(
        _step("w1", mode="write", resource_locks=["mailbox"]),
        _step("w2", mode="write", resource_locks=["mailbox"]),
    )
    _run(fake, g)
    assert fake.peak == 1


def test_writes_with_distinct_locks_can_parallelize():
    fake = FakeExecutor()
    g = _graph(
        _step("w1", mode="write", resource_locks=["mailbox"]),
        _step("w2", mode="write", resource_locks=["calendar"]),
    )
    _run(fake, g)
    assert fake.peak >= 2


def test_untagged_writes_are_serialized_by_default():
    fake = FakeExecutor()
    g = _graph(_step("w1", mode="write"), _step("w2", mode="write"))
    _run(fake, g)
    assert fake.peak == 1


def test_failure_only_blocks_downstream_branch():
    fake = FakeExecutor()
    fake.fail_ids = {"b"}
    # a -> b(fails) -> d ; a -> c(ok)
    g = _graph(_step("a"), _step("b", ["a"]), _step("c", ["a"]), _step("d", ["b"]))
    results = _run(fake, g)
    assert results["a"].status == StepStatus.SUCCEEDED
    assert results["b"].status == StepStatus.FAILED
    assert results["c"].status == StepStatus.SUCCEEDED
    assert "d" not in results  # blocked because its dependency failed


def test_retry_then_succeed():
    fake = FakeExecutor()
    fake.fail_once = {"a": 1}
    g = _graph(_step("a", retry=1))
    results = _run(fake, g)
    assert results["a"].status == StepStatus.SUCCEEDED
    assert results["a"].metrics["attempts"] == 2


def test_timeout_marks_failed():
    fake = FakeExecutor()
    fake.timeout_ids = {"a"}
    g = _graph(_step("a", timeout=0.05))
    results = _run(fake, g)
    assert results["a"].status == StepStatus.FAILED
    assert "timeout" in (results["a"].error or "")


def test_routing_provider_selects_preferred_agent():
    fake = FakeExecutor()
    g = _graph(_step("a", preferred_resource_id="RemoteHRAssistantAgent"))
    _run(fake, g, routing=StubRoutingProvider())
    assert fake.received["a"]["agent"] == "RemoteHRAssistantAgent"


def test_initial_completed_skips_done_steps():
    fake = FakeExecutor()
    g = _graph(_step("a"), _step("b", ["a"]))
    results = _run(fake, g, initial_completed={"a"})
    assert fake.calls == ["b"]  # a skipped
    assert "a" not in results


def test_downstream_receives_resolved_inputs_from_upstream_artifact():
    fake = FakeExecutor()
    a = _step("step_1", agent_name="A", expected_outputs=["person_info"])
    b = _step(
        "step_2",
        deps=["step_1"],
        agent_name="B",
        input_bindings=[
            {"parameter_name": "employee", "source_step": "A", "source_output": "person_info"}
        ],
    )
    results = _run(fake, _graph(a, b))
    assert results["step_2"].is_success
    # b's executor received the resolved upstream payload
    assert "employee" in fake.received["step_2"]["inputs"]
    assert fake.received["step_2"]["inputs"]["employee"] == {"ok": "step_1"}


def test_binding_resolves_to_most_recent_completed_step_of_same_agent():
    """When one agent drives multiple steps, a binding must point at the
    already-completed upstream, not the latest declared step."""
    fake = FakeExecutor()
    # Agent A drives step_1 (upstream) and step_3 (downstream, not yet run when
    # step_2 resolves its inputs). step_2 binds source_step="A".
    s1 = _step("step_1", agent_name="A", expected_outputs=["person_info"])
    s2 = _step(
        "step_2",
        deps=["step_1"],
        agent_name="B",
        input_bindings=[
            {"parameter_name": "employee", "source_step": "A", "source_output": "person_info"}
        ],
    )
    s3 = _step("step_3", deps=["step_2"], agent_name="A")
    results = _run(fake, _graph(s1, s2, s3))
    assert results["step_2"].is_success
    # step_2 must have received step_1's output, not an empty/未运行 step_3.
    assert fake.received["step_2"]["inputs"]["employee"] == {"ok": "step_1"}


def test_routing_crash_degrades_to_failed_and_isolates_branch():
    """A routing failure must fail only that step, not crash the whole DAG."""

    class ExplodingRouting:
        async def decide(self, step, **kwargs):
            if step.step_id == "b":
                raise RuntimeError("routing boom")

            class _R:
                selected_agent = None

            return _R()

    fake = FakeExecutor()
    # a -> b(routing crashes) -> d ; a -> c(ok)
    g = _graph(_step("a"), _step("b", ["a"]), _step("c", ["a"]), _step("d", ["b"]))
    results = _run(fake, g, routing=ExplodingRouting())
    assert results["a"].status == StepStatus.SUCCEEDED
    assert results["b"].status == StepStatus.FAILED
    assert "step crashed" in (results["b"].error or "")
    assert results["c"].status == StepStatus.SUCCEEDED  # independent branch survives
    assert "d" not in results  # blocked by failed dependency

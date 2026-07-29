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
        self.received[step.step_id] = {
            "agent": selected_agent, "inputs": dict(inputs)}
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


def test_read_sharing_lock_with_write_is_not_run_concurrently():
    """A read declaring a lock also held by a write must not run in parallel
    with that write (read/write conflict on the same resource)."""
    fake = FakeExecutor()
    g = _graph(
        _step("w", mode="write", resource_locks=["mailbox"]),
        _step("r", mode="read", resource_locks=["mailbox"]),
    )
    results = _run(fake, g)
    assert fake.peak == 1
    assert all(r.is_success for r in results.values())


def test_untagged_read_still_parallelizes_with_a_locked_write():
    """An untagged read has no resource contention and keeps running freely."""
    fake = FakeExecutor()
    g = _graph(
        _step("w", mode="write", resource_locks=["mailbox"]),
        _step("r", mode="read"),  # no lock -> no conflict
    )
    _run(fake, g)
    assert fake.peak >= 2


def test_failure_only_blocks_downstream_branch():
    fake = FakeExecutor()
    fake.fail_ids = {"b"}
    # a -> b(fails) -> d ; a -> c(ok)
    g = _graph(_step("a"), _step("b", ["a"]),
               _step("c", ["a"]), _step("d", ["b"]))
    results = _run(fake, g)
    assert results["a"].status == StepStatus.SUCCEEDED
    assert results["b"].status == StepStatus.FAILED
    assert results["c"].status == StepStatus.SUCCEEDED
    assert results["d"].status == StepStatus.SKIPPED
    assert results["d"].failure.code == "UPSTREAM_STEP_FAILED"
    assert results["d"].failure.blocked_by == ["b"]


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
            {"parameter_name": "employee", "source_step": "A",
                "source_output": "person_info"}
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
            {"parameter_name": "employee", "source_step": "A",
                "source_output": "person_info"}
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
                # A DISPATCH must carry a concrete agent; only step "b" crashes.
                selected_agent = "agent_x"
                decision = "DISPATCH"

            return _R()

    fake = FakeExecutor()
    # a -> b(routing crashes) -> d ; a -> c(ok)
    g = _graph(_step("a"), _step("b", ["a"]),
               _step("c", ["a"]), _step("d", ["b"]))
    results = _run(fake, g, routing=ExplodingRouting())
    assert results["a"].status == StepStatus.SUCCEEDED
    assert results["b"].status == StepStatus.FAILED
    assert "step crashed" in (results["b"].error or "")
    # independent branch survives
    assert results["c"].status == StepStatus.SUCCEEDED
    assert results["d"].status == StepStatus.SKIPPED
    assert results["d"].failure.code == "UPSTREAM_STEP_FAILED"
    assert results["d"].failure.blocked_by == ["b"]


# --------------------------------------------------------------------------- #
# C3: strict input resolution (no fallback; fail closed)
# --------------------------------------------------------------------------- #
def test_wrong_source_output_name_fails_closed():
    """A binding naming an output the producer never emitted must fail closed,
    never fall back to an arbitrary output."""
    fake = FakeExecutor()
    a = _step("step_1", agent_name="A", expected_outputs=["person_info"])
    b = _step(
        "step_2",
        deps=["step_1"],
        agent_name="B",
        input_bindings=[
            {"parameter_name": "x", "source_step": "A",
                "source_output": "WRONG_NAME"}
        ],
    )
    results = _run(fake, _graph(a, b))
    assert results["step_1"].is_success
    assert results["step_2"].status == StepStatus.FAILED
    assert results["step_2"].metrics.get(
        "input_error") == "artifact_not_produced"
    assert "step_2" not in fake.calls  # never executed with a wrong input


def test_consuming_schema_invalid_write_output_fails_closed():
    """An untyped write output (schema_valid=False) must not be consumed."""
    fake = FakeExecutor()
    a = _step("step_1", agent_name="A", mode="write", expected_outputs=["doc"])
    b = _step(
        "step_2",
        deps=["step_1"],
        agent_name="B",
        input_bindings=[
            {"parameter_name": "x", "source_step": "A", "source_output": "doc"}
        ],
    )
    results = _run(fake, _graph(a, b))
    assert results["step_1"].is_success
    assert results["step_2"].status == StepStatus.FAILED
    assert results["step_2"].metrics.get("input_error") == "schema_invalid"
    assert "step_2" not in fake.calls


# --------------------------------------------------------------------------- #
# P0-2: author-declared required_inputs (concrete ArtifactRef) resolution
# --------------------------------------------------------------------------- #
from src.interface.artifact import Artifact, ArtifactRef, Sensitivity  # noqa: E402
from src.orchestration.resolver import ArtifactResolver  # noqa: E402
from src.orchestration.store import ArtifactStore  # noqa: E402


class _DenyGuard:
    def can_read(self, *, subject, artifact, scenario=None, action="read"):
        return False


def test_required_inputs_resolved_and_passed_to_executor():
    store = ArtifactStore()
    ref = store.put(Artifact(logical_name="employee", payload={
                    "name": "王强", "id": "E001"}, sensitivity=Sensitivity.INTERNAL))
    fake = FakeExecutor()
    step = _step("s1", agent_name="A", required_inputs={"employee": ref})
    sched = TaskScheduler(execute_step=fake, store=store)
    results = asyncio.run(sched.run(_graph(step), context={"task_id": "t"}))
    assert results["s1"].is_success
    assert fake.received["s1"]["inputs"]["employee"] == {
        "name": "王强", "id": "E001"}


def test_required_input_missing_artifact_fails_closed():
    store = ArtifactStore()
    missing = ArtifactRef(artifact_id="does-not-exist", version=1)
    fake = FakeExecutor()
    step = _step("s1", agent_name="A", required_inputs={"employee": missing})
    sched = TaskScheduler(execute_step=fake, store=store)
    results = asyncio.run(sched.run(_graph(step), context={"task_id": "t"}))
    assert results["s1"].status == StepStatus.FAILED
    assert results["s1"].metrics.get("input_error") == "artifact_not_found"
    assert fake.calls == []  # executor never invoked with a missing input


def test_required_input_access_denied_fails_closed():
    store = ArtifactStore()
    ref = store.put(Artifact(logical_name="employee", payload={
                    "x": 1}, sensitivity=Sensitivity.INTERNAL))
    fake = FakeExecutor()
    step = _step("s1", agent_name="A", required_inputs={"employee": ref})
    sched = TaskScheduler(
        execute_step=fake, store=store,
        resolver=ArtifactResolver(store, guard=_DenyGuard()),
    )
    results = asyncio.run(sched.run(_graph(step), context={"task_id": "t"}))
    assert results["s1"].status == StepStatus.FAILED
    assert results["s1"].metrics.get("input_error") == "access_denied"
    assert fake.calls == []


def test_required_input_schema_invalid_fails_closed():
    store = ArtifactStore()
    ref = store.put(Artifact(logical_name="doc", payload={
                    "x": 1}, schema_valid=False, sensitivity=Sensitivity.INTERNAL))
    fake = FakeExecutor()
    step = _step("s1", agent_name="A", required_inputs={"doc": ref})
    sched = TaskScheduler(execute_step=fake, store=store)
    results = asyncio.run(sched.run(_graph(step), context={"task_id": "t"}))
    assert results["s1"].status == StepStatus.FAILED
    assert results["s1"].metrics.get("input_error") == "schema_invalid"
    assert fake.calls == []


def test_required_input_selector_error_fails_closed():
    store = ArtifactStore()
    stored = store.put(Artifact(logical_name="employee", payload={
                       "name": "王强"}, sensitivity=Sensitivity.INTERNAL))
    bad = ArtifactRef(artifact_id=stored.artifact_id,
                      version=stored.version, selector="missing.path")
    fake = FakeExecutor()
    step = _step("s1", agent_name="A", required_inputs={"employee": bad})
    sched = TaskScheduler(execute_step=fake, store=store)
    results = asyncio.run(sched.run(_graph(step), context={"task_id": "t"}))
    assert results["s1"].status == StepStatus.FAILED
    assert results["s1"].metrics.get("input_error") == "selector_error"
    assert fake.calls == []


def test_required_inputs_conflicting_with_binding_is_rejected():
    """A param declared by BOTH required_inputs and input_bindings is an illegal
    graph: fail closed, never a silent override, executor not called."""
    store = ArtifactStore()
    ref = store.put(Artifact(logical_name="employee", payload={
                    "x": 1}, sensitivity=Sensitivity.INTERNAL))
    fake = FakeExecutor()
    step = _step(
        "s1",
        agent_name="B",
        required_inputs={"employee": ref},
        input_bindings=[{"parameter_name": "employee",
                         "source_step": "A", "source_output": "person_info"}],
    )
    sched = TaskScheduler(execute_step=fake, store=store)
    results = asyncio.run(sched.run(_graph(step), context={"task_id": "t"}))
    assert results["s1"].status == StepStatus.FAILED
    assert results["s1"].metrics.get("input_error") == "duplicate_param"
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# P1-3: critical commit_step_result persistence must change the terminal status
# --------------------------------------------------------------------------- #
def test_commit_success_keeps_step_succeeded():
    fake = FakeExecutor()
    committed: list[str] = []

    async def commit(*, step, result):
        committed.append(step.step_id)

    sched = TaskScheduler(execute_step=fake)
    results = asyncio.run(
        sched.run(_graph(_step("a")), commit_step_result=commit))
    assert results["a"].is_success
    assert committed == ["a"]


def test_commit_failure_marks_read_step_failed_not_succeeded():
    """A critical persistence failure escalates a would-be SUCCEEDED read step to
    FAILED (persistence_failed); a read carries no reconciliation flag."""
    fake = FakeExecutor()

    async def commit(*, step, result):
        raise IOError("disk full")

    sched = TaskScheduler(execute_step=fake)
    results = asyncio.run(
        sched.run(_graph(_step("a")), commit_step_result=commit))
    assert results["a"].status == StepStatus.FAILED
    assert results["a"].metrics.get("persistence_failed") is True
    assert results["a"].metrics.get("needs_reconciliation") is not True


def test_commit_failure_on_side_effect_needs_reconciliation():
    """A side effect that ran once but whose durable commit failed must NOT be
    SUCCEEDED and must request reconciliation (no silent success/re-send)."""
    from src.orchestration.completion import ReceiptStore

    calls = {"n": 0}

    async def exec_step(*, step, selected_agent, inputs, context):
        calls["n"] += 1
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS, result={"sent": True},
            metadata={"external_op_id": "op-1"},
        )

    async def commit(*, step, result):
        raise IOError("checkpoint failed")

    step = _step("email", mode="send", preferred_resource_id="EmailAgent")
    sched = TaskScheduler(
        execute_step=exec_step,
        routing_provider=StubRoutingProvider(),
        receipt_store=ReceiptStore(),
    )
    results = asyncio.run(
        sched.run(_graph(step), context={"task_id": "t"}, commit_step_result=commit))
    assert results["email"].status == StepStatus.FAILED
    assert results["email"].metrics.get("persistence_failed") is True
    assert results["email"].metrics.get("needs_reconciliation") is True
    assert calls["n"] == 1  # the side effect happened exactly once


def test_side_effect_receipt_extracts_provider_id_from_structured_result():
    from src.orchestration.completion import ReceiptStore, validate_receipt

    async def exec_step(*, step, selected_agent, inputs, context):
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={"status": "sent", "message_id": "msg-42"},
        )

    receipts = ReceiptStore()
    step = _step("email", mode="send", preferred_resource_id="EmailAgent")
    scheduler = TaskScheduler(
        execute_step=exec_step,
        routing_provider=StubRoutingProvider(),
        receipt_store=receipts,
    )
    results = asyncio.run(
        scheduler.run(_graph(step), context={"task_id": "receipt-task"})
    )
    result = results["email"]
    receipt = receipts.get(result.metrics["idempotency_key"])

    assert result.is_success
    assert result.metrics["external_op_id"] == "msg-42"
    assert receipt["external_op_id"] == "msg-42"
    assert validate_receipt(receipt, key=result.metrics["idempotency_key"])


def test_dispatch_permission_denial_is_not_retried_or_misclassified():
    from src.security.enforcement import PermissionDeniedError

    calls = {"n": 0}

    async def denied(**_kwargs):
        calls["n"] += 1
        raise PermissionDeniedError("private policy reason", {"secret": "value"})

    step = _step("read", retry=3, preferred_resource_id="DeniedAgent")
    results = _run(denied, _graph(step), StubRoutingProvider())

    assert calls["n"] == 1
    assert results["read"].failure.code == "AGENT_DISPATCH_DENIED"
    assert results["read"].failure.category == "permission"
    assert results["read"].failure.retryable is False
    assert "private policy reason" not in results["read"].failure.message


def test_side_effect_dispatch_permission_denial_does_not_require_reconciliation():
    from src.orchestration.completion import ReceiptStore
    from src.security.enforcement import PermissionDeniedError

    async def denied(**_kwargs):
        raise PermissionDeniedError("private policy reason", {"secret": "value"})

    step = _step("email", mode="send", preferred_resource_id="DeniedAgent")
    scheduler = TaskScheduler(
        execute_step=denied,
        routing_provider=StubRoutingProvider(),
        receipt_store=ReceiptStore(),
    )
    results = asyncio.run(
        scheduler.run(_graph(step), context={"task_id": "permission-task"})
    )

    assert results["email"].failure.code == "AGENT_DISPATCH_DENIED"
    assert results["email"].failure.category == "permission"
    assert results["email"].metrics.get("needs_reconciliation") is not True


# --------------------------------------------------------------------------- #
# Artifact governance closed loop: produce (owner-tag) -> guard -> consume
# --------------------------------------------------------------------------- #
def _governed_graph(producer_extra=None):
    a = _step("step_1", agent_name="A",
              expected_outputs=["person_info"], **(producer_extra or {}))
    b = _step(
        "step_2",
        deps=["step_1"],
        agent_name="B",
        input_bindings=[
            {"parameter_name": "employee", "source_step": "A",
             "source_output": "person_info"}
        ],
    )
    return _graph(a, b)


def _alice_factory(step, selected_agent):
    from src.manager.executor.base import ExecutionContext
    # The producing step runs as 'alice' -> its captured artifact is owned by
    # alice, regardless of the acting subject that later consumes it.
    return ExecutionContext(user_id="alice", metadata={"operation_mode": step.operation_mode})


def test_governed_cross_user_artifact_read_fails_closed(tmp_path, monkeypatch):
    """End-to-end: a producer tags its output owner=alice; a downstream consumer
    acting as bob is denied by the guard -> step FAILED, executor not called."""
    import src.service.env as env
    from src.orchestration.artifact_guard import PolicyEngineArtifactGuard

    monkeypatch.setattr(env, "S_ABAC_ENABLED", False)
    monkeypatch.setenv("ARTIFACT_AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    store = ArtifactStore()
    resolver = ArtifactResolver(store, guard=PolicyEngineArtifactGuard())
    fake = FakeExecutor()
    sched = TaskScheduler(execute_step=fake, store=store, resolver=resolver)
    results = asyncio.run(sched.run(
        _governed_graph(),
        context={"task_id": "t", "subject": "bob",
                 "context_factory": _alice_factory},
    ))
    assert results["step_1"].is_success
    assert results["step_2"].status == StepStatus.FAILED
    assert results["step_2"].metrics.get("input_error") == "access_denied"
    assert "step_2" not in fake.calls  # never ran on a denied cross-user input


def test_governed_cross_user_read_allowed_for_listed_reader(tmp_path, monkeypatch):
    """With the producer declaring bob in ``allowed_reader_ids``, the same
    cross-user read is GOVERNED-allowed and the value flows to the consumer."""
    import src.service.env as env
    from src.orchestration.artifact_guard import PolicyEngineArtifactGuard

    monkeypatch.setattr(env, "S_ABAC_ENABLED", False)
    monkeypatch.setenv("ARTIFACT_AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    store = ArtifactStore()
    resolver = ArtifactResolver(store, guard=PolicyEngineArtifactGuard())
    fake = FakeExecutor()
    sched = TaskScheduler(execute_step=fake, store=store, resolver=resolver)
    results = asyncio.run(sched.run(
        _governed_graph(producer_extra={"allowed_reader_ids": ["bob"]}),
        context={"task_id": "t", "subject": "bob",
                 "context_factory": _alice_factory},
    ))
    assert results["step_1"].is_success
    assert results["step_2"].is_success
    assert fake.received["step_2"]["inputs"]["employee"] == {"ok": "step_1"}

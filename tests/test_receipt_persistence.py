"""Persistent receipt store tests (P1, T9 / T10).

Verifies idempotency survives a simulated process restart: a side-effect step
(e.g. sending an email) is executed at most once even when the scheduler is
recreated with a fresh in-memory state but the same on-disk receipt store.
"""

import asyncio

from src.interface.artifact import StepStatus
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep, WorkflowStatus
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.completion import (
    PersistentReceiptStore,
    ReceiptStore,
    idempotency_key,
    normalize_input,
)
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.scheduler import TaskScheduler


def _send_graph(task_id="task-email", **step_kwargs):
    return TaskGraph(
        spec=TaskSpec(task_id=task_id),
        steps=[TaskStep(step_id="send", operation_mode="send",
                        preferred_resource_id="RemoteEmailDispatchAgent", **step_kwargs)],
    )


class _SendCounter:
    def __init__(self, delay: float = 0.0):
        self.sends = 0
        self._delay = delay

    async def __call__(self, *, step, selected_agent, inputs, context):
        self.sends += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={"message_id": f"msg-{self.sends}"},
            metadata={"external_op_id": f"msg-{self.sends}"},
        )


def _email_graph():
    return TaskGraph(
        spec=TaskSpec(task_id="task-email"),
        steps=[TaskStep(step_id="send", operation_mode="send",
                        preferred_resource_id="RemoteEmailDispatchAgent")],
    )


def _run(execute, receipts):
    sched = TaskScheduler(
        execute_step=execute, routing_provider=StubRoutingProvider(), receipt_store=receipts
    )
    return asyncio.run(sched.run(_email_graph(), context={"task_id": "task-email"}))


def test_t10_first_run_executes_and_records_receipt(tmp_path):
    execute = _SendCounter()
    receipts = PersistentReceiptStore("task-email", base_dir=tmp_path)
    results = _run(execute, receipts)
    assert execute.sends == 1
    assert results["send"].is_success
    # A receipt file was persisted.
    assert (tmp_path / "task-email.json").exists()


def test_t9_repeated_run_after_restart_does_not_resend(tmp_path):
    execute = _SendCounter()

    # First run in "process A".
    _run(execute, PersistentReceiptStore("task-email", base_dir=tmp_path))
    assert execute.sends == 1

    # "Process restart": a brand new receipt store instance reading the same dir.
    reloaded = PersistentReceiptStore("task-email", base_dir=tmp_path)
    results = _run(execute, reloaded)

    assert execute.sends == 1  # NOT re-sent
    assert results["send"].is_success
    assert results["send"].metrics.get("idempotent_reuse") is True


def test_pre_side_effect_validation_failure_releases_receipt_for_safe_retry(
    tmp_path,
):
    class _ValidationFailure:
        calls = 0

        async def __call__(self, **_kwargs):
            self.calls += 1
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error="could not convert string to float: '待补充'",
                metadata={
                    "side_effect_started": False,
                    "failure_phase": "validation",
                    "safe_to_retry": True,
                },
            )

    execute = _ValidationFailure()
    receipts = PersistentReceiptStore("task-email", base_dir=tmp_path)
    results = _run(execute, receipts)

    assert execute.calls == 1
    assert results.terminal_status == WorkflowStatus.FAILED
    assert results["send"].metrics["safe_to_retry"] is True
    assert results["send"].metrics["receipt_released"] is True
    key = idempotency_key("task-email", "send", {})
    assert PersistentReceiptStore(
        "task-email", base_dir=tmp_path
    ).get(key) is None


# --------------------------------------------------------------------------- #
# C4: crash window + receipt-write failure -> NEEDS_RECONCILIATION (no re-send)
# --------------------------------------------------------------------------- #
def test_crash_before_success_receipt_needs_reconciliation(tmp_path):
    """External send succeeded and a STARTED intent was written, but the process
    crashed before the SUCCEEDED receipt. On resume the step must NOT re-send;
    it needs reconciliation."""
    key = idempotency_key("task-email", "send", {})
    seed = PersistentReceiptStore("task-email", base_dir=tmp_path)
    seed.put(
        key,
        {
            "idempotency_key": key,
            "task_id": "task-email",
            "step_id": "send",
            "agent": "RemoteEmailDispatchAgent",
            "status": "STARTED",
            "normalized_input": normalize_input({}),
            "external_op_id": None,
            "timestamp": 1.0,
        },
    )

    execute = _SendCounter()
    sched = TaskScheduler(
        execute_step=execute,
        routing_provider=StubRoutingProvider(),
        receipt_store=PersistentReceiptStore("task-email", base_dir=tmp_path),
    )
    results = asyncio.run(
        sched.run(_email_graph(), context={"task_id": "task-email"}))

    assert execute.sends == 0  # never re-sent
    assert results["send"].status == StepStatus.FAILED
    assert results["send"].metrics.get("needs_reconciliation") is True
    assert results.terminal_status == WorkflowStatus.NEEDS_RECONCILIATION


class _FailOnSuccessReceipts(ReceiptStore):
    """Allows the STARTED intent write but fails the SUCCEEDED write."""

    def put(self, key, receipt):
        if receipt.get("status") == "SUCCEEDED":
            raise IOError("simulated disk failure")
        super().put(key, receipt)


def test_receipt_write_failure_marks_reconciliation():
    execute = _SendCounter()
    sched = TaskScheduler(
        execute_step=execute,
        routing_provider=StubRoutingProvider(),
        receipt_store=_FailOnSuccessReceipts(),
    )
    results = asyncio.run(
        sched.run(_email_graph(), context={"task_id": "task-email"}))

    assert execute.sends == 1  # the side effect happened exactly once
    assert results["send"].status == StepStatus.FAILED
    assert results["send"].metrics.get("needs_reconciliation") is True
    assert results.terminal_status == WorkflowStatus.NEEDS_RECONCILIATION


# --------------------------------------------------------------------------- #
# Part 1: atomic receipt claiming
# --------------------------------------------------------------------------- #
def test_concurrent_instances_execute_side_effect_exactly_once(tmp_path):
    """Two pre-created store instances race on the same email step. The atomic
    claim guarantees the executor runs EXACTLY once; the loser gets a trusted
    SUCCEEDED reuse or an IN_PROGRESS/reconciliation verdict -- never a re-send."""
    execute = _SendCounter(delay=0.05)

    async def _go():
        # Both stores are created up-front (before either run) and share a dir.
        store_a = PersistentReceiptStore("task-email", base_dir=tmp_path)
        store_b = PersistentReceiptStore("task-email", base_dir=tmp_path)

        async def _run_with(store):
            sched = TaskScheduler(
                execute_step=execute,
                routing_provider=StubRoutingProvider(),
                receipt_store=store,
            )
            return await sched.run(_send_graph(), context={"task_id": "task-email"})

        return await asyncio.gather(_run_with(store_a), _run_with(store_b))

    r_a, r_b = asyncio.run(_go())

    assert execute.sends == 1  # executed exactly once across both instances
    # At least one run reports success; any FAILED run must be a reconciliation
    # verdict (the loser NEVER re-executes the side effect).
    assert any(r["send"].status == StepStatus.SUCCEEDED for r in (r_a, r_b))
    for r in (r_a, r_b):
        res = r["send"]
        if res.status == StepStatus.FAILED:
            assert res.metrics.get("needs_reconciliation") is True


def test_first_write_creates_missing_parent_dir(tmp_path):
    """The receipt parent directory is created on first claim (before the lock),
    so the very first write into a fresh store succeeds."""
    nested = tmp_path / "does" / "not" / "exist"
    execute = _SendCounter()
    receipts = PersistentReceiptStore("task-email", base_dir=nested)
    results = _run(execute, receipts)
    assert execute.sends == 1
    assert results["send"].is_success
    assert (nested / "task-email.json").exists()


def test_corrupt_receipt_store_fails_closed_and_does_not_execute(tmp_path):
    """A corrupt receipt JSON must fail closed: the executor is NEVER called and
    the run ends in a storage-corruption terminal. The file is not cleared."""
    path = tmp_path / "task-email.json"
    path.write_text("{ this is : not valid json", encoding="utf-8")

    execute = _SendCounter()
    sched = TaskScheduler(
        execute_step=execute,
        routing_provider=StubRoutingProvider(),
        receipt_store=PersistentReceiptStore("task-email", base_dir=tmp_path),
    )
    results = asyncio.run(
        sched.run(_send_graph(), context={"task_id": "task-email"}))

    assert execute.sends == 0  # never executed against an unknown prior state
    assert results["send"].status == StepStatus.FAILED
    assert results["send"].metrics.get("receipt_store_corrupt") is True
    assert results.terminal_status == WorkflowStatus.FAILED
    # Corrupt file is left untouched (never cleared/overwritten).
    assert path.read_text(encoding="utf-8").startswith("{ this is")


def test_started_receipt_present_is_not_resent(tmp_path):
    """A pre-existing STARTED receipt (unconfirmed) blocks execution and needs
    reconciliation; the executor is never called."""
    key = idempotency_key("task-email", "send", {})
    seed = PersistentReceiptStore("task-email", base_dir=tmp_path)
    seed.put(
        key,
        {
            "idempotency_key": key,
            "task_id": "task-email",
            "step_id": "send",
            "agent": "RemoteEmailDispatchAgent",
            "status": "STARTED",
            "normalized_input": normalize_input({}),
            "external_op_id": None,
            "claim_id": "someone-else",
            "timestamp": 1.0,
        },
    )
    execute = _SendCounter()
    sched = TaskScheduler(
        execute_step=execute,
        routing_provider=StubRoutingProvider(),
        receipt_store=PersistentReceiptStore("task-email", base_dir=tmp_path),
    )
    results = asyncio.run(
        sched.run(_send_graph(), context={"task_id": "task-email"}))

    assert execute.sends == 0
    assert results["send"].metrics.get("needs_reconciliation") is True
    assert results.terminal_status == WorkflowStatus.NEEDS_RECONCILIATION


# --------------------------------------------------------------------------- #
# Part 2: side-effect steps never auto-retry
# --------------------------------------------------------------------------- #
class _StatusExecutor:
    """Returns a fixed non-success status; counts invocations."""

    def __init__(self, status):
        self.calls = 0
        self._status = status

    async def __call__(self, *, step, selected_agent, inputs, context):
        self.calls += 1
        return ExecuteResult(status=self._status, error="unavailable")


class _RaisingExecutor:
    def __init__(self, exc):
        self.calls = 0
        self._exc = exc

    async def __call__(self, *, step, selected_agent, inputs, context):
        self.calls += 1
        raise self._exc


def test_send_step_timeout_does_not_auto_retry(tmp_path):
    """A send step with retry=3 whose attempt returns TIMEOUT must invoke the
    executor exactly once and require reconciliation (no auto re-send)."""
    execute = _StatusExecutor(ExecutionStatus.TIMEOUT)
    sched = TaskScheduler(
        execute_step=execute,
        routing_provider=StubRoutingProvider(),
        receipt_store=PersistentReceiptStore("task-email", base_dir=tmp_path),
    )
    results = asyncio.run(
        sched.run(_send_graph(retry=3), context={"task_id": "task-email"}))

    assert execute.calls == 1  # NOT retried despite retry=3
    assert results["send"].status == StepStatus.FAILED
    assert results["send"].metrics.get("needs_reconciliation") is True
    assert results.terminal_status == WorkflowStatus.NEEDS_RECONCILIATION


def test_write_step_network_error_does_not_auto_retry(tmp_path):
    """A write step raising a network error must not be auto-called a second
    time; the unconfirmed outcome needs reconciliation."""
    execute = _RaisingExecutor(ConnectionError("connection reset by peer"))
    graph = TaskGraph(
        spec=TaskSpec(task_id="task-write"),
        steps=[TaskStep(step_id="w", operation_mode="write", retry=2,
                        preferred_resource_id="RemoteDocumentGeneratorAgent")],
    )
    sched = TaskScheduler(
        execute_step=execute,
        routing_provider=StubRoutingProvider(),
        receipt_store=PersistentReceiptStore("task-write", base_dir=tmp_path),
    )
    results = asyncio.run(
        sched.run(graph, context={"task_id": "task-write"}))

    assert execute.calls == 1  # NOT retried
    assert results["w"].status == StepStatus.FAILED
    assert results["w"].metrics.get("needs_reconciliation") is True


def test_read_step_retry_behavior_is_preserved():
    """A read step keeps the original retry semantics even with a receipt store
    bound (reads carry no idempotency receipt / side effect)."""
    calls = {"n": 0}

    async def execute(*, step, selected_agent, inputs, context):
        calls["n"] += 1
        if calls["n"] == 1:
            return ExecuteResult(status=ExecutionStatus.TIMEOUT, error="transient")
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"ok": True})

    graph = TaskGraph(
        spec=TaskSpec(task_id="t"),
        steps=[TaskStep(step_id="r", operation_mode="read", retry=1)],
    )
    sched = TaskScheduler(execute_step=execute, receipt_store=ReceiptStore())
    results = asyncio.run(sched.run(graph, context={"task_id": "t"}))

    assert calls["n"] == 2  # retried once, then succeeded
    assert results["r"].is_success
    assert results["r"].metrics.get("attempts") == 2

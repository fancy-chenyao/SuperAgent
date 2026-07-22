"""Tests for closed-loop governance (Plan Phase 4): completion DSL, idempotency,
receipts, and their scheduler integration."""

import asyncio

import pytest

from src.interface.artifact import StepStatus
from src.interface.task_graph import CompletionCondition, TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.completion import (
    ReceiptStore,
    evaluate_completion,
    evaluate_condition,
    idempotency_key,
    normalize_input,
    validate_receipt,
)
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.scheduler import TaskScheduler


# --------------------------------------------------------------------------- #
# Restricted DSL
# --------------------------------------------------------------------------- #
def test_dsl_exists_and_null():
    ctx = {"outputs": {"a": {"x": 1}}, "metrics": {}, "status": "SUCCEEDED"}
    assert evaluate_condition("exists(outputs.a)", ctx) is True
    assert evaluate_condition("exists(outputs.b)", ctx) is False
    assert evaluate_condition("outputs.a != null", ctx) is True
    assert evaluate_condition("outputs.b != null", ctx) is False
    assert evaluate_condition("outputs.b == null", ctx) is True


def test_dsl_bool_and_compare():
    ctx = {"outputs": {}, "metrics": {"attempts": 2}, "status": "SUCCEEDED"}
    assert evaluate_condition(
        "status == 'SUCCEEDED' and metrics.attempts <= 3", ctx) is True
    assert evaluate_condition(
        "metrics.attempts > 5 or status == 'SUCCEEDED'", ctx) is True
    assert evaluate_condition("not (status == 'FAILED')", ctx) is True
    assert evaluate_condition(
        "metrics.attempts >= 2 and metrics.attempts < 3", ctx) is True


def test_dsl_len_and_nested_path():
    ctx = {"outputs": {"rows": [1, 2, 3], "doc": {
        "pages": 5}}, "metrics": {}, "status": "S"}
    assert evaluate_condition("len(outputs.rows) == 3", ctx) is True
    assert evaluate_condition("outputs.doc.pages >= 5", ctx) is True


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os')",
        "().__class__",
        "open('x')",
        "lambda: 1",
        "a.__class__",
        "outputs.__dict__",
    ],
)
def test_dsl_rejects_unsafe_constructs(expr):
    with pytest.raises(ValueError):
        evaluate_condition(expr, {"a": 1, "outputs": {}})


def test_evaluate_completion_reports_first_failure():
    conds = [
        CompletionCondition(expression="status == 'SUCCEEDED'"),
        CompletionCondition(expression="exists(outputs.missing)"),
    ]
    ok, failed = evaluate_completion(
        conds, outputs={}, metrics={}, status="SUCCEEDED")
    assert ok is False
    assert failed == "exists(outputs.missing)"


def test_evaluate_completion_all_pass():
    conds = [CompletionCondition(expression="exists(outputs.doc)")]
    ok, failed = evaluate_completion(
        conds, outputs={"doc": object()}, metrics={}, status="SUCCEEDED")
    assert ok is True and failed is None


# --------------------------------------------------------------------------- #
# Idempotency + receipts
# --------------------------------------------------------------------------- #
def test_idempotency_key_stable_and_order_independent():
    k1 = idempotency_key("T", "s", {"a": 1, "b": 2})
    k2 = idempotency_key("T", "s", {"b": 2, "a": 1})
    assert k1 == k2
    assert idempotency_key("T", "s2", {"a": 1}) != k1
    assert normalize_input(
        {"a": 1, "b": 2}) == normalize_input({"b": 2, "a": 1})


def test_receipt_store_and_validation():
    rs = ReceiptStore()
    assert rs.has("k") is False
    full = {
        "task_id": "T",
        "step_id": "s",
        "normalized_input": "{}",
        "agent": "EmailAgent",
        "status": "SUCCEEDED",
        "timestamp": 1.0,
        "external_op_id": "op-1",
    }
    rs.put("k", full)
    assert rs.has("k") is True
    assert validate_receipt(rs.get("k")) is True
    # Missing required provenance fields -> not trusted (fail closed).
    assert validate_receipt({"step_id": "s", "status": "SUCCEEDED"}) is False
    assert validate_receipt({"status": "FAILED", "step_id": "s"}) is False
    assert validate_receipt(None) is False


def test_receipt_external_op_id_must_be_nonempty_string():
    base = {
        "task_id": "T",
        "step_id": "s",
        "normalized_input": "{}",
        "agent": "EmailAgent",
        "status": "SUCCEEDED",
        "timestamp": 1.0,
    }
    assert validate_receipt(dict(base, external_op_id="op-1")) is True
    # Empty / whitespace / non-string external ids are not verifiable.
    assert validate_receipt(dict(base, external_op_id="")) is False
    assert validate_receipt(dict(base, external_op_id="   ")) is False
    assert validate_receipt(dict(base, external_op_id=123)) is False
    assert validate_receipt(dict(base, external_op_id=None)) is False


def test_receipt_key_consistency_blocks_idempotent_skip():
    """A receipt whose identity fields do not derive the lookup key must not be
    trusted for an idempotent skip."""
    key = idempotency_key("T", "s", {"a": 1})
    good = {
        "idempotency_key": key,
        "task_id": "T",
        "step_id": "s",
        "normalized_input": normalize_input({"a": 1}),
        "agent": "EmailAgent",
        "status": "SUCCEEDED",
        "timestamp": 1.0,
        "external_op_id": "op-1",
    }
    assert validate_receipt(good, key=key) is True
    # Tampered normalized_input: recorded key no longer derives from the fields.
    tampered = dict(good, normalized_input=normalize_input({"a": 999}))
    assert validate_receipt(tampered) is False
    assert validate_receipt(tampered, key=key) is False
    # A receipt stored under a different key must not satisfy this lookup.
    other = dict(good)
    assert validate_receipt(other, key="some-other-key") is False


# --------------------------------------------------------------------------- #
# Scheduler integration
# --------------------------------------------------------------------------- #
def _graph(*steps):
    return TaskGraph(spec=TaskSpec(task_id="T"), steps=list(steps))


def test_completion_condition_failure_marks_step_failed():
    async def exec_step(*, step, selected_agent, inputs, context):
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"doc": "x"})

    step = TaskStep(
        step_id="s",
        expected_outputs=["doc"],
        completion_conditions=[CompletionCondition(
            expression="exists(outputs.other)")],
    )
    results = asyncio.run(TaskScheduler(
        execute_step=exec_step).run(_graph(step)))
    assert results["s"].status == StepStatus.FAILED
    assert "completion condition failed" in (results["s"].error or "")


def test_completion_condition_pass_keeps_success():
    async def exec_step(*, step, selected_agent, inputs, context):
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"doc": "x"})

    step = TaskStep(
        step_id="s",
        expected_outputs=["doc"],
        completion_conditions=[CompletionCondition(
            expression="exists(outputs.doc)")],
    )
    results = asyncio.run(TaskScheduler(
        execute_step=exec_step).run(_graph(step)))
    assert results["s"].is_success


def test_side_effect_step_not_re_executed_across_resume():
    receipts = ReceiptStore()
    calls = {"n": 0}

    async def exec_step(*, step, selected_agent, inputs, context):
        calls["n"] += 1
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={"sent": True},
            metadata={"external_op_id": f"op-{calls['n']}"},
        )

    email = TaskStep(
        step_id="email",
        operation_mode="write",
        resource_locks=["mailbox"],
        preferred_resource_id="EmailAgent",
    )
    ctx = {"task_id": "T"}

    # First run: email is sent, receipt recorded.
    r1 = asyncio.run(
        TaskScheduler(
            execute_step=exec_step,
            routing_provider=StubRoutingProvider(),
            receipt_store=receipts,
        ).run(_graph(email), context=ctx)
    )
    # Simulated retry/resume with the SAME receipt store.
    r2 = asyncio.run(
        TaskScheduler(
            execute_step=exec_step,
            routing_provider=StubRoutingProvider(),
            receipt_store=receipts,
        ).run(_graph(email), context=ctx)
    )

    assert r1["email"].is_success and r2["email"].is_success
    assert calls["n"] == 1  # executed once; the resume reused the receipt
    assert r2["email"].metrics.get("idempotent_reuse") is True


def test_read_only_step_has_no_idempotency_receipt():
    receipts = ReceiptStore()

    async def exec_step(*, step, selected_agent, inputs, context):
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"data": 1})

    read = TaskStep(step_id="q", operation_mode="read")
    asyncio.run(
        TaskScheduler(execute_step=exec_step, receipt_store=receipts).run(
            _graph(read), context={"task_id": "T"}
        )
    )
    # No receipt is written for a read-only step.
    assert idempotency_key("T", "q", {}) not in receipts._receipts

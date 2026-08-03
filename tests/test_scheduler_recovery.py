"""Bounded read-only recovery: semantic retry, then one trusted redispatch."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.providers import RoutingResult
from src.orchestration.scheduler import TaskScheduler


def _contract(
    name: str = "policy.info",
    schema_ref: str = "policy.info@v1",
    *,
    requires: list[DataContractRef] | None = None,
    produces: list[DataContractRef] | None = None,
    contract_version: str = "1.0",
) -> AgentContract:
    return AgentContract(
        contract_version=contract_version,
        requires=list(requires or []),
        produces=(
            list(produces)
            if produces is not None
            else [DataContractRef(name=name, schema_ref=schema_ref)]
        ),
    )


def _step(*, retry: int = 1, mode: str = "read") -> TaskStep:
    return TaskStep(
        step_id="lookup",
        operation_mode=mode,
        retry=retry,
        agent_name="PrimaryAgent",
        preferred_resource_id="PrimaryAgent",
        expected_outputs=["policy.info"],
        agent_contract=_contract(),
    )


def _graph(step: TaskStep | None = None) -> TaskGraph:
    return TaskGraph(
        spec=TaskSpec(task_id="recovery"),
        steps=[step or _step()],
    )


def _envelope(
    agent: str,
    *,
    status: str = "success",
    retryable: bool = False,
) -> dict:
    outputs = {
        "policy.info": {
            "query": "年假",
            "answer": "满一年五天",
            "knowledge_items_count": 1,
            "policy_scope": "company",
        }
    }
    return {
        "contract_version": "1.0",
        "status": status,
        "outputs": outputs if status == "success" else {},
        "error": (
            {
                "code": "UPSTREAM_TIMEOUT",
                "message": "remote failure",
                "retryable": retryable,
                "details": {},
            }
            if status != "success"
            else None
        ),
        "metadata": {
            "producer_agent": agent,
            "schema_version": "1.0",
        },
    }


def _ok(payload: dict) -> ExecuteResult:
    return ExecuteResult(status=ExecutionStatus.SUCCESS, result=payload)


class SequenceRoutingProvider:
    def __init__(self, *verdicts: RoutingResult) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[set[str]] = []

    async def decide(self, step, *, authorized_agent_ids, **kwargs):
        self.calls.append(set(authorized_agent_ids or set()))
        if self.verdicts:
            return self.verdicts.pop(0)
        return RoutingResult(
            selected_agent=None,
            decision="NO_CAPABLE_AGENT",
        )


class FailingRecoveryRoutingProvider(SequenceRoutingProvider):
    async def decide(self, step, *, authorized_agent_ids, **kwargs):
        self.calls.append(set(authorized_agent_ids or set()))
        if len(self.calls) == 1:
            return RoutingResult(
                selected_agent="PrimaryAgent",
                decision="DISPATCH",
            )
        raise RuntimeError("router unavailable")


def _context(*, backup_contract: AgentContract | None = None) -> dict:
    agents = [
        SimpleNamespace(
            agent_name="PrimaryAgent",
            agent_contract=_contract(),
        )
    ]
    if backup_contract is not None:
        agents.append(
            SimpleNamespace(
                agent_name="BackupAgent",
                agent_contract=backup_contract,
            )
        )
    return {
        "task_id": "recovery",
        "workflow_id": "wf-recovery",
        "user_query": "查询年假政策",
        "authorized_agent_ids": {"PrimaryAgent", "BackupAgent"},
        "agents": agents,
    }


def test_non_retryable_business_failure_stops_after_first_attempt():
    calls = {"n": 0}

    async def execute(**kwargs):
        calls["n"] += 1
        return _ok(_envelope("PrimaryAgent", status="error", retryable=False))

    scheduler = TaskScheduler(execute_step=execute)
    result = asyncio.run(scheduler.run(_graph(_step(retry=5))))["lookup"]

    assert calls["n"] == 1
    assert result.is_success is False
    assert result.failure.retryable is False
    assert result.metrics["attempts"] == 1
    assert len(result.metrics["attempt_failures"]) == 1


def test_schema_failure_stops_after_first_attempt():
    calls = {"n": 0}

    async def execute(**kwargs):
        calls["n"] += 1
        payload = _envelope("PrimaryAgent")
        payload["outputs"]["policy.info"] = {"query": "missing required fields"}
        return _ok(payload)

    result = asyncio.run(
        TaskScheduler(execute_step=execute).run(_graph(_step(retry=5)))
    )["lookup"]

    assert calls["n"] == 1
    assert result.is_success is False
    assert result.failure.code == "SCHEMA_VALIDATION_FAILED"
    assert result.failure.retryable is False


def test_retryable_failure_retries_once_and_emits_one_step_start():
    calls = {"n": 0}
    starts = {"n": 0}

    async def execute(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok(_envelope("PrimaryAgent", status="error", retryable=True))
        return _ok(_envelope("PrimaryAgent"))

    async def on_step_start(**kwargs):
        starts["n"] += 1

    scheduler = TaskScheduler(execute_step=execute, retry_delay_seconds=-1)
    result = asyncio.run(
        scheduler.run(_graph(), on_step_start=on_step_start)
    )["lookup"]

    assert result.is_success is True
    assert calls["n"] == 2
    assert starts["n"] == 1
    assert result.metrics["attempts"] == 2
    assert result.metrics["retry_count"] == 1
    assert result.metrics["recovery_path"] == ["primary", "same_agent_retry"]


def test_unknown_executor_exception_is_not_retried():
    calls = {"n": 0}

    async def execute(**kwargs):
        calls["n"] += 1
        raise RuntimeError("programming defect")

    result = asyncio.run(
        TaskScheduler(execute_step=execute).run(_graph(_step(retry=5)))
    )["lookup"]

    assert calls["n"] == 1
    assert result.failure.code == "INTERNAL_STEP_ERROR"
    assert result.failure.retryable is False


def test_raw_executor_failure_without_trusted_signal_is_not_retried():
    calls: list[str] = []
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        RoutingResult(selected_agent="BackupAgent", decision="DISPATCH"),
    )

    async def execute(*, selected_agent, **kwargs):
        calls.append(selected_agent)
        return ExecuteResult(status=ExecutionStatus.FAILED, error="unavailable")

    result = asyncio.run(
        TaskScheduler(
            execute_step=execute,
            routing_provider=routing,
            redispatch_enabled=True,
        ).run(
            _graph(_step(retry=5)),
            context=_context(backup_contract=_contract()),
        )
    )["lookup"]

    assert calls == ["PrimaryAgent"]
    assert routing.calls == [{"PrimaryAgent", "BackupAgent"}]
    assert result.failure.code == "AGENT_EXECUTION_FAILED"
    assert result.failure.retryable is False
    assert result.metrics["attempts"] == 1
    assert len(result.metrics["attempt_failures"]) == 1


def test_retry_exhaustion_redispatches_once_to_trusted_equivalent_agent():
    calls: list[str] = []
    starts = {"n": 0}
    contexts: list[str] = []
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        RoutingResult(selected_agent="BackupAgent", decision="DISPATCH"),
    )

    async def execute(*, selected_agent, **kwargs):
        calls.append(selected_agent)
        if selected_agent == "PrimaryAgent":
            return _ok(_envelope(selected_agent, status="error", retryable=True))
        return _ok(_envelope(selected_agent))

    async def on_step_start(**kwargs):
        starts["n"] += 1

    context = _context(backup_contract=_contract())
    context["context_factory"] = lambda step, agent: contexts.append(agent)
    scheduler = TaskScheduler(
        execute_step=execute,
        routing_provider=routing,
        redispatch_enabled=True,
    )
    result = asyncio.run(
        scheduler.run(
            _graph(),
            context=context,
            on_step_start=on_step_start,
        )
    )["lookup"]

    assert result.is_success is True
    assert calls == ["PrimaryAgent", "PrimaryAgent", "BackupAgent"]
    assert starts["n"] == 1
    assert contexts == ["PrimaryAgent", "BackupAgent"]
    assert routing.calls == [
        {"PrimaryAgent", "BackupAgent"},
        {"BackupAgent"},
    ]
    assert result.metrics["attempts"] == 3
    assert result.metrics["retry_count"] == 1
    assert result.metrics["redispatch_count"] == 1
    assert result.metrics["selected_agent"] == "BackupAgent"
    assert result.metrics["redispatch_outcome"] == "SUCCEEDED"
    assert result.metrics["recovery_path"] == [
        "primary",
        "same_agent_retry",
        "redispatch",
    ]


def test_redispatch_disabled_by_default():
    calls: list[str] = []
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        RoutingResult(selected_agent="BackupAgent", decision="DISPATCH"),
    )

    async def execute(*, selected_agent, **kwargs):
        calls.append(selected_agent)
        return _ok(_envelope(selected_agent, status="error", retryable=True))

    result = asyncio.run(
        TaskScheduler(execute_step=execute, routing_provider=routing).run(
            _graph(),
            context=_context(backup_contract=_contract()),
        )
    )["lookup"]

    assert result.is_success is False
    assert calls == ["PrimaryAgent", "PrimaryAgent"]
    assert len(routing.calls) == 1


def test_redispatch_without_trusted_contract_never_invokes_candidate():
    calls: list[str] = []
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        RoutingResult(selected_agent="BackupAgent", decision="DISPATCH"),
    )

    async def execute(*, selected_agent, **kwargs):
        calls.append(selected_agent)
        return _ok(_envelope(selected_agent, status="error", retryable=True))

    result = asyncio.run(
        TaskScheduler(
            execute_step=execute,
            routing_provider=routing,
            redispatch_enabled=True,
        ).run(_graph(), context=_context())
    )["lookup"]

    assert calls == ["PrimaryAgent", "PrimaryAgent"]
    assert result.is_success is False
    assert result.failure.retryable is True
    assert (
        result.metrics["redispatch_outcome"]
        == "REROUTED_AGENT_CONTRACT_MISSING"
    )


@pytest.mark.parametrize(
    ("verdict", "outcome"),
    [
        (
            RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
            "REDISPATCH_RESELECTED_EXCLUDED_AGENT",
        ),
        (
            RoutingResult(selected_agent=None, decision="CLARIFY"),
            "CLARIFY",
        ),
        (
            RoutingResult(selected_agent=None, decision="NO_CAPABLE_AGENT"),
            "NO_CAPABLE_AGENT",
        ),
    ],
)
def test_invalid_recovery_route_is_terminal_without_loop(verdict, outcome):
    calls = {"n": 0}
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        verdict,
    )

    async def execute(**kwargs):
        calls["n"] += 1
        return _ok(_envelope("PrimaryAgent", status="error", retryable=True))

    result = asyncio.run(
        TaskScheduler(
            execute_step=execute,
            routing_provider=routing,
            redispatch_enabled=True,
        ).run(_graph(), context=_context(backup_contract=_contract()))
    )["lookup"]

    assert calls["n"] == 2
    assert len(routing.calls) == 2
    assert result.metrics["redispatch_outcome"] == outcome


@pytest.mark.parametrize(
    ("candidate_outputs", "contract_version", "outcome"),
    [
        (
            [DataContractRef(name="policy.info", schema_ref="policy.info@v2")],
            "1.0",
            "REDISPATCH_OUTPUT_SCHEMA_MISMATCH",
        ),
        (
            [
                DataContractRef(
                    name="policy.info",
                    schema_ref="policy.info@v1",
                    required=False,
                )
            ],
            "1.0",
            "REDISPATCH_OUTPUT_REQUIRED_MISMATCH",
        ),
        (
            [
                DataContractRef(
                    name="policy.info",
                    schema_ref="policy.info@v1",
                    cardinality="many",
                )
            ],
            "1.0",
            "REDISPATCH_OUTPUT_CARDINALITY_MISMATCH",
        ),
        (
            [
                DataContractRef(
                    name="policy.info",
                    schema_ref="policy.info@v1",
                ),
                DataContractRef(
                    name="candidate.extra",
                    schema_ref="candidate.extra@v1",
                    required=False,
                ),
            ],
            "1.0",
            "REDISPATCH_OUTPUT_EXTRA",
        ),
        (
            [],
            "1.0",
            "REDISPATCH_OUTPUT_MISSING",
        ),
        (
            [DataContractRef(name="policy.info", schema_ref="policy.info@v1")],
            "2.0",
            "REDISPATCH_CONTRACT_VERSION_MISMATCH",
        ),
    ],
    ids=[
        "schema",
        "required-downgrade",
        "cardinality",
        "extra-output",
        "missing-output",
        "contract-version",
    ],
)
def test_incompatible_candidate_output_contract_is_rejected_before_execution(
    candidate_outputs,
    contract_version,
    outcome,
):
    calls: list[str] = []
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        RoutingResult(selected_agent="BackupAgent", decision="DISPATCH"),
    )

    async def execute(*, selected_agent, **kwargs):
        calls.append(selected_agent)
        return _ok(_envelope(selected_agent, status="error", retryable=True))

    result = asyncio.run(
        TaskScheduler(
            execute_step=execute,
            routing_provider=routing,
            redispatch_enabled=True,
        ).run(
            _graph(),
            context=_context(
                backup_contract=_contract(
                    produces=candidate_outputs,
                    contract_version=contract_version,
                )
            ),
        )
    )["lookup"]

    assert calls == ["PrimaryAgent", "PrimaryAgent"]
    assert result.metrics["redispatch_outcome"] == outcome


@pytest.mark.parametrize(
    ("candidate_input", "outcome"),
    [
        (None, "REDISPATCH_INPUT_MISSING"),
        (
            DataContractRef(
                name="policy.query",
                schema_ref="policy.query@v2",
                required=False,
            ),
            "REDISPATCH_INPUT_SCHEMA_MISMATCH",
        ),
        (
            DataContractRef(
                name="policy.query",
                schema_ref="policy.query@v1",
                cardinality="many",
                required=False,
            ),
            "REDISPATCH_INPUT_CARDINALITY_MISMATCH",
        ),
        (
            DataContractRef(
                name="policy.query",
                schema_ref="policy.query@v1",
                required=True,
            ),
            "REDISPATCH_INPUT_REQUIRED_MISMATCH",
        ),
    ],
)
def test_incompatible_candidate_input_contract_is_rejected_before_execution(
    candidate_input,
    outcome,
):
    calls: list[str] = []
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        RoutingResult(selected_agent="BackupAgent", decision="DISPATCH"),
    )
    planned_input = DataContractRef(
        name="policy.query",
        schema_ref="policy.query@v1",
        required=False,
    )
    step = _step()
    step.agent_contract = _contract(requires=[planned_input])
    backup_requires = [candidate_input] if candidate_input is not None else []

    async def execute(*, selected_agent, **kwargs):
        calls.append(selected_agent)
        return _ok(_envelope(selected_agent, status="error", retryable=True))

    result = asyncio.run(
        TaskScheduler(
            execute_step=execute,
            routing_provider=routing,
            redispatch_enabled=True,
        ).run(
            _graph(step),
            context=_context(
                backup_contract=_contract(requires=backup_requires)
            ),
        )
    )["lookup"]

    assert calls == ["PrimaryAgent", "PrimaryAgent"]
    assert result.metrics["redispatch_outcome"] == outcome


def test_router_cannot_redispatch_to_unauthorized_agent():
    calls: list[str] = []
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        RoutingResult(selected_agent="UnauthorizedAgent", decision="DISPATCH"),
    )

    async def execute(*, selected_agent, **kwargs):
        calls.append(selected_agent)
        return _ok(_envelope(selected_agent, status="error", retryable=True))

    context = _context(backup_contract=_contract())
    context["agents"].append(
        SimpleNamespace(
            agent_name="UnauthorizedAgent",
            agent_contract=_contract(),
        )
    )
    result = asyncio.run(
        TaskScheduler(
            execute_step=execute,
            routing_provider=routing,
            redispatch_enabled=True,
        ).run(_graph(), context=context)
    )["lookup"]

    assert calls == ["PrimaryAgent", "PrimaryAgent"]
    assert (
        result.metrics["redispatch_outcome"]
        == "REDISPATCH_AGENT_UNAUTHORIZED"
    )


def test_candidate_with_unresolved_required_input_is_not_invoked():
    calls: list[str] = []
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        RoutingResult(selected_agent="BackupAgent", decision="DISPATCH"),
    )
    backup_contract = AgentContract(
        requires=[
            DataContractRef(
                name="policy.query",
                schema_ref="policy.query@v1",
            )
        ],
        produces=[
            DataContractRef(
                name="policy.info",
                schema_ref="policy.info@v1",
            )
        ],
    )

    async def execute(*, selected_agent, **kwargs):
        calls.append(selected_agent)
        return _ok(_envelope(selected_agent, status="error", retryable=True))

    result = asyncio.run(
        TaskScheduler(
            execute_step=execute,
            routing_provider=routing,
            redispatch_enabled=True,
        ).run(
            _graph(),
            context=_context(backup_contract=backup_contract),
        )
    )["lookup"]

    assert calls == ["PrimaryAgent", "PrimaryAgent"]
    assert (
        result.metrics["redispatch_outcome"]
        == "REDISPATCH_INPUT_MISSING"
    )


def test_recovery_routing_exception_is_terminal():
    calls = {"n": 0}
    routing = FailingRecoveryRoutingProvider()

    async def execute(**kwargs):
        calls["n"] += 1
        return _ok(_envelope("PrimaryAgent", status="error", retryable=True))

    result = asyncio.run(
        TaskScheduler(
            execute_step=execute,
            routing_provider=routing,
            redispatch_enabled=True,
        ).run(
            _graph(),
            context=_context(backup_contract=_contract()),
        )
    )["lookup"]

    assert calls["n"] == 2
    assert len(routing.calls) == 2
    assert result.metrics["redispatch_outcome"] == "ROUTING_FAILED"


def test_side_effect_failure_never_retries_or_redispatches():
    calls = {"n": 0}
    routing = SequenceRoutingProvider(
        RoutingResult(selected_agent="PrimaryAgent", decision="DISPATCH"),
        RoutingResult(selected_agent="BackupAgent", decision="DISPATCH"),
    )

    async def execute(**kwargs):
        calls["n"] += 1
        return ExecuteResult(status=ExecutionStatus.FAILED, error="uncertain")

    result = asyncio.run(
        TaskScheduler(
            execute_step=execute,
            routing_provider=routing,
            redispatch_enabled=True,
        ).run(
            _graph(_step(retry=5, mode="write")),
            context=_context(backup_contract=_contract()),
        )
    )["lookup"]

    assert calls["n"] == 1
    assert len(routing.calls) == 1
    assert "redispatch_count" not in result.metrics

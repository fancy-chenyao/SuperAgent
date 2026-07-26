import asyncio

import pytest

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.manager.executor.agent_result_adapter import (
    AgentResultNormalizationError,
    _register_missing_agent_schemas,
    normalize_agent_result,
)
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.interface.artifact import Artifact
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.orchestration.completion import ReceiptStore
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.schema_registry import SchemaRegistry, get_schema_registry
from src.orchestration.scheduler import InputResolutionError, TaskScheduler


def _ok(result):
    return ExecuteResult(status=ExecutionStatus.SUCCESS, result=result)


def _contract(name: str, schema_ref: str) -> AgentContract:
    return AgentContract(produces=[DataContractRef(name=name, schema_ref=schema_ref)])


def _envelope(agent: str, outputs: dict, *, status: str = "success", error=None):
    return {
        "contract_version": "1.0",
        "status": status,
        "outputs": outputs,
        "error": error,
        "metadata": {
            "producer_agent": agent,
            "schema_version": "1.0",
        },
    }


def test_contract_envelope_is_normalized_and_schema_checked():
    normalized = normalize_agent_result(
        _ok(
            _envelope(
                "RemoteKnowledgeAgent",
                {
                    "policy.info": {
                        "query": "年假",
                        "answer": "按司龄享受年假",
                        "knowledge_items_count": 1,
                        "policy_scope": "company",
                    }
                },
            )
        ),
        agent_contract=_contract("policy.info", "policy.info@v1"),
    )
    assert normalized.outputs["policy.info"]["policy_scope"] == "company"
    assert normalized.schema_refs == {"policy.info": "policy.info@v1"}
    assert normalized.legacy is False


def test_contract_envelope_rejects_mismatched_producer_agent():
    with pytest.raises(AgentResultNormalizationError) as exc:
        normalize_agent_result(
            _ok(
                _envelope(
                    "DifferentAgent",
                    {
                        "policy.info": {
                            "query": "年假",
                            "answer": "五天",
                            "knowledge_items_count": 1,
                            "policy_scope": "company",
                        }
                    },
                )
            ),
            agent_contract=_contract("policy.info", "policy.info@v1"),
            producer_agent="RemoteKnowledgeAgent",
        )

    assert exc.value.code == "PRODUCER_AGENT_MISMATCH"


def test_contract_envelope_rejects_mismatched_schema_version():
    envelope = _envelope(
        "RemoteKnowledgeAgent",
        {
            "policy.info": {
                "query": "年假",
                "answer": "五天",
                "knowledge_items_count": 1,
                "policy_scope": "company",
            }
        },
    )
    envelope["metadata"]["schema_version"] = "2.0"

    with pytest.raises(AgentResultNormalizationError) as exc:
        normalize_agent_result(
            _ok(envelope),
            agent_contract=_contract("policy.info", "policy.info@v1"),
            producer_agent="RemoteKnowledgeAgent",
        )

    assert exc.value.code == "RESULT_SCHEMA_VERSION_MISMATCH"


def test_legacy_contract_result_is_adapted_only_when_unambiguous():
    normalized = normalize_agent_result(
        _ok(
            {
                "query": "年假",
                "answer": "五天",
                "knowledge_items_count": 1,
                "policy_scope": "company",
            }
        ),
        agent_contract=_contract("policy.info", "policy.info@v1"),
        producer_agent="RemoteKnowledgeAgent",
    )
    assert set(normalized.outputs) == {"policy.info"}
    assert normalized.legacy is True


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"error": "没有权限"}, "BUSINESS_RESULT_ERROR"),
        (
            _envelope(
                "RemoteKnowledgeAgent",
                {
                    "policy.info": {
                        "query": "年假",
                        "answer": "部分结果",
                        "knowledge_items_count": 1,
                        "policy_scope": "company",
                    }
                },
                status="partial",
                error={
                    "code": "UPSTREAM_PARTIAL",
                    "message": "仅获得部分制度",
                    "retryable": False,
                    "details": {},
                },
            ),
            "BUSINESS_RESULT_INCOMPLETE",
        ),
    ],
)
def test_business_error_and_partial_fail_closed(payload, code):
    with pytest.raises(AgentResultNormalizationError) as exc:
        normalize_agent_result(
            _ok(payload),
            agent_contract=_contract("policy.info", "policy.info@v1"),
        )
    assert exc.value.code == code


def test_uncontracted_legacy_result_preserves_declared_output_aliases():
    normalized = normalize_agent_result(
        _ok({"value": 1}),
        expected_outputs=["legacy.a", "legacy.b"],
    )
    assert normalized.outputs == {
        "legacy.a": {"value": 1},
        "legacy.b": {"value": 1},
    }


def test_builtin_schema_registration_does_not_replace_existing_schema():
    registry = SchemaRegistry()
    strict_schema = {
        "required": ["sentinel"],
        "properties": {"sentinel": {"type": "string"}},
    }
    registry.register("employee.info@v1", strict_schema)

    _register_missing_agent_schemas(registry)

    assert registry.get("employee.info@v1") == strict_schema
    assert registry.has("policy.info@v1")


def test_side_effect_normalization_failure_does_not_complete_success_receipt():
    schema_ref = "test.side-effect-result@v1"
    get_schema_registry().register(
        schema_ref,
        {
            "required": ["accepted"],
            "properties": {"accepted": {"type": "boolean"}},
        },
    )
    contract = _contract("side-effect.result", schema_ref)
    step = TaskStep(
        step_id="send",
        operation_mode="send",
        preferred_resource_id="RemoteWriteAgent",
        expected_outputs=["side-effect.result"],
        agent_contract=contract,
    )

    async def execute(**kwargs):
        return _ok(
            _envelope(
                "RemoteWriteAgent",
                {"side-effect.result": {"unexpected": True}},
            )
        )

    receipts = ReceiptStore()
    scheduler = TaskScheduler(
        execute_step=execute,
        routing_provider=StubRoutingProvider(),
        receipt_store=receipts,
    )
    result = asyncio.run(
        scheduler.run(
            TaskGraph(spec=TaskSpec(task_id="side-effect"), steps=[step]),
            context={"task_id": "side-effect"},
        )
    )["send"]
    receipt = receipts.get(result.metrics["idempotency_key"])

    assert result.is_success is False
    assert result.metrics["needs_reconciliation"] is True
    assert result.metrics["result_error"] == "SCHEMA_VALIDATION_FAILED"
    assert receipt["status"] == "STARTED"


def test_required_contract_fan_in_cannot_be_downgraded_to_optional():
    report_contract = AgentContract(
        requires=[
            DataContractRef(name="report.sources", schema_ref="report.sources@v1")
        ],
        produces=[
            DataContractRef(name="report.markdown", schema_ref="report.markdown@v1")
        ],
    )
    step = TaskStep(
        step_id="report",
        operation_mode="read",
        agent_contract=report_contract,
        input_bindings=[
            {
                "parameter_name": "report.sources",
                "optional": True,
                "source_artifacts": [
                    {
                        "source_step": "hr",
                        "source_output": "employee.info",
                    },
                    {
                        "source_step": "knowledge",
                        "source_output": "policy.info",
                    },
                ],
            }
        ],
    )
    scheduler = TaskScheduler(execute_step=lambda **kwargs: None)
    employee_ref = scheduler.store.put(
        Artifact(
            logical_name="employee.info",
            schema_ref="employee.info@v1",
            payload={"records": []},
            schema_valid=True,
        )
    )
    scheduler._outputs = {
        "hr": {"employee.info": employee_ref},
        "knowledge": {},
    }

    with pytest.raises(InputResolutionError) as exc:
        scheduler._resolve_inputs(step, {})

    assert exc.value.reason == "artifact_not_produced"
    assert exc.value.source == "knowledge"


def test_required_contract_fan_in_rejects_empty_source_list():
    report_contract = AgentContract(
        requires=[
            DataContractRef(name="report.sources", schema_ref="report.sources@v1")
        ],
        produces=[
            DataContractRef(name="report.markdown", schema_ref="report.markdown@v1")
        ],
    )
    step = TaskStep(
        step_id="report",
        operation_mode="read",
        agent_contract=report_contract,
        input_bindings=[
            {
                "parameter_name": "report.sources",
                "optional": True,
                "source_artifacts": [],
            }
        ],
    )
    scheduler = TaskScheduler(execute_step=lambda **kwargs: None)

    with pytest.raises(InputResolutionError) as exc:
        scheduler._resolve_inputs(step, {})

    assert exc.value.reason == "invalid_fan_in"


def test_three_agent_contract_fan_in_creates_named_artifacts_and_lineage():
    hr_contract = AgentContract(
        produces=[
            DataContractRef(name="employee.info", schema_ref="employee.info@v1"),
            DataContractRef(
                name="employee.salary",
                schema_ref="employee.salary@v1",
                required=False,
            ),
        ]
    )
    knowledge_contract = _contract("policy.info", "policy.info@v1")
    report_contract = AgentContract(
        requires=[
            DataContractRef(name="report.sources", schema_ref="report.sources@v1")
        ],
        produces=[
            DataContractRef(name="report.markdown", schema_ref="report.markdown@v1")
        ],
    )
    graph = TaskGraph(
        spec=TaskSpec(task_id="contract-fan-in", subject="u1"),
        steps=[
            TaskStep(
                step_id="hr",
                agent_name="RemoteHRAssistantAgent",
                preferred_resource_id="RemoteHRAssistantAgent",
                operation_mode="read",
                expected_outputs=["employee.info", "employee.salary"],
                agent_contract=hr_contract.model_dump(mode="json"),
            ),
            TaskStep(
                step_id="knowledge",
                agent_name="RemoteKnowledgeAgent",
                preferred_resource_id="RemoteKnowledgeAgent",
                operation_mode="read",
                expected_outputs=["policy.info"],
                agent_contract=knowledge_contract.model_dump(mode="json"),
            ),
            TaskStep(
                step_id="report",
                agent_name="RemoteReportAgent",
                preferred_resource_id="RemoteReportAgent",
                operation_mode="read",
                depends_on=["hr", "knowledge"],
                expected_outputs=["report.markdown"],
                agent_contract=report_contract.model_dump(mode="json"),
                title="王强员工档案与年假制度",
                description="使用两个上游结果生成 Markdown 综合汇总",
                input_bindings=[
                    {
                        "parameter_name": "report.sources",
                        "source_artifacts": [
                            {
                                "source_step": "hr",
                                "source_output": "employee.info",
                            },
                            {
                                "source_step": "knowledge",
                                "source_output": "policy.info",
                            },
                        ],
                        "assembly": {"schema_ref": "report.sources@v1"},
                    }
                ],
            ),
        ],
    )
    started = set()
    parallel = asyncio.Event()
    report_inputs = {}

    async def execute(*, step, selected_agent, inputs, context):
        if step.step_id in {"hr", "knowledge"}:
            started.add(step.step_id)
            if started == {"hr", "knowledge"}:
                parallel.set()
            await asyncio.wait_for(parallel.wait(), timeout=1)
        if step.step_id == "hr":
            return _ok(
                _envelope(
                    selected_agent,
                    {
                        "employee.info": {
                            "records": [
                                {
                                    "employee_id": "E001",
                                    "name": "王强",
                                    "department": "研发部",
                                    "position": "工程师",
                                }
                            ],
                            "matched_count": 1,
                        },
                        "employee.salary": {
                            "records": [{"employee_id": "E001", "amount": 100}],
                            "matched_count": 1,
                        },
                    },
                )
            )
        if step.step_id == "knowledge":
            return _ok(
                _envelope(
                    selected_agent,
                    {
                        "policy.info": {
                            "query": "公司现行年假制度",
                            "answer": "满一年享受五天年假",
                            "knowledge_items_count": 1,
                            "policy_scope": "company",
                        }
                    },
                )
            )
        report_inputs.update(inputs)
        return _ok(
            _envelope(
                selected_agent,
                {
                    "report.markdown": {
                        "title": "综合汇总",
                        "markdown": "# 综合汇总",
                        "source_count": len(inputs["report.sources"]["sources"]),
                    }
                },
            )
        )

    scheduler = TaskScheduler(
        execute_step=execute,
        routing_provider=StubRoutingProvider(),
    )
    results = asyncio.run(scheduler.run(graph, context={"subject": "u1"}))

    assert all(result.is_success for result in results.values())
    assert len(report_inputs["report.sources"]["sources"]) == 2
    assert {
        source["logical_name"] for source in report_inputs["report.sources"]["sources"]
    } == {"employee.info", "policy.info"}

    hr_info_ref = results["hr"].outputs["employee.info"]
    hr_salary_ref = results["hr"].outputs["employee.salary"]
    assert hr_info_ref.artifact_id != hr_salary_ref.artifact_id

    report_ref = results["report"].outputs["report.markdown"]
    report_artifact = scheduler.store.get(report_ref)
    assert report_artifact.schema_ref == "report.markdown@v1"
    assert report_artifact.schema_valid is True
    assert {ref.artifact_id for ref in report_artifact.derived_from} == {
        hr_info_ref.artifact_id,
        results["knowledge"].outputs["policy.info"].artifact_id,
    }

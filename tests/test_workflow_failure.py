"""Tests for the structured workflow failure compatibility boundary."""

import asyncio

import pytest
from pydantic import ValidationError

from src.contracts import FailureCategory, FailureCode, FailureDescriptor
from src.interface.artifact import StepResult, StepStatus
from src.orchestration.failure_mapper import (
    failure_from_exception,
    failure_from_step_result,
    make_failure,
)


def test_failure_descriptor_serializes_stable_protocol_values():
    failure = FailureDescriptor(
        code="schema_validation_failed",
        category=FailureCategory.SCHEMA,
        message="Schema validation failed",
        step_id="hr_step",
        details_safe={"schema_ref": "employee.info@v1"},
    )

    assert failure.code == "SCHEMA_VALIDATION_FAILED"
    assert failure.model_dump(mode="json")["category"] == "schema"


def test_failure_descriptor_filters_unsafe_details_and_sorts_blockers():
    failure = FailureDescriptor(
        code="UPSTREAM_STEP_FAILED",
        category="artifact",
        message="blocked",
        blocked_by=["policy_step", "hr_step", "hr_step"],
        details_safe={
            "schema_ref": "report.sources@v1",
            "reason_codes": ["NO_CAPABLE_AGENT", "routing_error: password=secret"],
            "missing_outputs": [{"payload": "secret"}],
            "payload": {"salary": 123},
            "traceback": "secret stack",
            "result_error_details": [{"input": "secret"}],
        },
    )

    assert failure.blocked_by == ["hr_step", "policy_step"]
    assert failure.details_safe == {
        "schema_ref": "report.sources@v1",
        "reason_codes": ["NO_CAPABLE_AGENT"],
        "missing_outputs": ["[redacted]"],
    }
    assert "secret" not in failure.model_dump_json()


def test_failure_descriptor_rejects_non_machine_code():
    with pytest.raises(ValidationError):
        FailureDescriptor(
            code="bad-code",
            category="internal",
            message="bad",
        )


def test_make_failure_uses_catalog_defaults_and_explicit_context():
    failure = make_failure(
        FailureCode.UPSTREAM_STEP_FAILED,
        step_id="report_step",
        blocked_by=["knowledge_step", "hr_step"],
        param="report.sources",
        source_output="employee.info",
    )

    assert failure.category == "artifact"
    assert failure.retryable is False
    assert failure.parameter_name == "report.sources"
    assert failure.source_output == "employee.info"
    assert failure.blocked_by == ["hr_step", "knowledge_step"]


def test_make_failure_safely_degrades_unknown_code():
    failure = make_failure(
        "FUTURE_OR_UNTRUSTED_CODE",
        category="schema",
        step_id="step_1",
    )

    assert failure.code == "INTERNAL_STEP_ERROR"
    assert failure.category == "internal"


@pytest.mark.parametrize(
    ("code", "category", "retryable"),
    [
        ("INTERNAL_SCHEDULER_ERROR", "internal", True),
        ("ARTIFACT_STORE_CORRUPTION", "persistence", False),
    ],
)
def test_runtime_failure_codes_are_catalogued(code, category, retryable):
    failure = make_failure(code)

    assert failure.code == code
    assert failure.category == category
    assert failure.retryable is retryable


@pytest.mark.parametrize(
    ("metrics", "expected_code", "expected_category"),
    [
        (
            {"result_error": "INVALID_ENVELOPE"},
            "AGENT_RESULT_INVALID",
            "contract",
        ),
        (
            {"result_error": "SCHEMA_VALIDATION_FAILED"},
            "SCHEMA_VALIDATION_FAILED",
            "schema",
        ),
        (
            {"result_error": "REROUTED_AGENT_CONTRACT_MISSING"},
            "REROUTED_AGENT_CONTRACT_MISSING",
            "contract",
        ),
        (
            {"input_error": "artifact_not_produced"},
            "UPSTREAM_OUTPUT_MISSING",
            "artifact",
        ),
        (
            {"input_error": "required_contract_input_missing"},
            "UPSTREAM_OUTPUT_MISSING",
            "artifact",
        ),
        (
            {"input_error": "access_denied"},
            "ARTIFACT_ACCESS_DENIED",
            "permission",
        ),
        (
            {"routing_decision": "NO_CAPABLE_AGENT"},
            "NO_CAPABLE_AGENT",
            "routing",
        ),
        (
            {"persistence_failed": True},
            "PERSISTENCE_FAILED",
            "persistence",
        ),
        (
            {"needs_reconciliation": True, "persistence_failed": True},
            "SIDE_EFFECT_UNCONFIRMED",
            "reconciliation",
        ),
    ],
)
def test_failure_from_step_result_maps_legacy_metrics(
    metrics, expected_code, expected_category
):
    failure = failure_from_step_result("step_1", "raw internal error", metrics)

    assert failure.code == expected_code
    assert failure.category == expected_category
    assert failure.step_id == "step_1"
    assert failure.message != "raw internal error"


def test_failure_from_step_result_does_not_publish_remote_details():
    failure = failure_from_step_result(
        "hr_step",
        "token=secret",
        {
            "result_error": "REMOTE_PRIVATE_CODE",
            "result_error_details": {
                "payload": {"employee_salary": 1000},
                "traceback": "private",
            },
            "selected_agent": "hr-agent",
        },
    )

    dumped = failure.model_dump_json()
    assert failure.code == "AGENT_BUSINESS_ERROR"
    assert "secret" not in dumped
    assert "employee_salary" not in dumped
    assert "private" not in dumped
    assert failure.agent_id == "hr-agent"


def test_adapter_retryable_verdict_upgrades_business_failure():
    failure = failure_from_step_result(
        "hr_step",
        "upstream timeout",
        {
            "result_error": "BUSINESS_RESULT_ERROR",
            "result_retryable": True,
            "selected_agent": "hr-agent",
        },
    )

    assert failure.code == "AGENT_BUSINESS_ERROR"
    assert failure.retryable is True
    # Presentation stays platform-owned even when the verdict is trusted.
    assert failure.message != "upstream timeout"


def test_adapter_retryable_verdict_never_leaks_outside_result_errors():
    failure = failure_from_step_result(
        "hr_step",
        None,
        {
            "input_error": "artifact_not_found",
            "result_retryable": True,
        },
    )

    assert failure.code == "ARTIFACT_NOT_FOUND"
    # The verdict only applies to business-result failures; other codes keep
    # their catalog default.
    assert failure.retryable is False


def test_failure_from_exception_never_exposes_exception_text():
    failure = failure_from_exception(
        RuntimeError("password=private"),
        step_id="step_1",
    )
    timeout = failure_from_exception(asyncio.TimeoutError(), step_id="step_2")

    assert failure.code == "INTERNAL_STEP_ERROR"
    assert "private" not in failure.model_dump_json()
    assert timeout.code == "AGENT_TIMEOUT"


def test_step_result_failure_populates_legacy_error():
    result = StepResult(
        step_id="hr_step",
        status=StepStatus.FAILED,
        failure=make_failure(
            "SCHEMA_VALIDATION_FAILED",
            step_id="hr_step",
        ),
    )

    assert result.error == result.failure.message


def test_old_step_result_without_failure_remains_readable():
    result = StepResult.model_validate(
        {
            "step_id": "legacy_step",
            "status": "FAILED",
            "error": "legacy failure",
            "metrics": {"attempts": 1},
        }
    )

    assert result.error == "legacy failure"
    assert result.failure is None


def test_successful_step_result_cannot_carry_failure():
    with pytest.raises(ValidationError):
        StepResult(
            step_id="step_1",
            status=StepStatus.SUCCEEDED,
            failure=make_failure("INTERNAL_STEP_ERROR"),
        )

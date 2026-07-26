from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts import (
    AgentContract,
    AgentResultEnvelope,
    AgentResultError,
    AgentResultMetadata,
    DataContractRef,
    validate_agent_result,
)
from src.orchestration.schema_registry import SchemaRegistry


def _metadata() -> AgentResultMetadata:
    return AgentResultMetadata(producer_agent="pilot", schema_version="1.0")


@pytest.mark.parametrize(
    ("status", "outputs", "error"),
    [
        ("success", {}, None),
        (
            "success",
            {"policy.info": {}},
            AgentResultError(code="FAILED", message="failed"),
        ),
        ("error", {}, None),
        (
            "error",
            {"policy.info": {}},
            AgentResultError(code="FAILED", message="failed"),
        ),
        ("partial", {"policy.info": {}}, None),
        ("partial", {}, AgentResultError(code="FAILED", message="failed")),
    ],
)
def test_invalid_status_combinations_are_rejected(status, outputs, error) -> None:
    with pytest.raises(ValidationError):
        AgentResultEnvelope(
            status=status,
            outputs=outputs,
            error=error,
            metadata=_metadata(),
        )


def test_validation_rejects_undeclared_output() -> None:
    contract = AgentContract(
        produces=[DataContractRef(name="policy.info", schema_ref="policy.info@v1")]
    )
    envelope = AgentResultEnvelope(
        status="success",
        outputs={"other.output": {}},
        metadata=_metadata(),
    )

    result = validate_agent_result(envelope, contract, SchemaRegistry())

    assert not result.valid
    assert result.errors[0].code == "UNDECLARED_OUTPUT"


def test_validation_rejects_unknown_schema_and_version_mismatch() -> None:
    contract = AgentContract(
        produces=[DataContractRef(name="policy.info", schema_ref="policy.info@v2")]
    )
    envelope = AgentResultEnvelope(
        contract_version="2.0",
        status="success",
        outputs={"policy.info": {}},
        metadata=_metadata(),
    )

    result = validate_agent_result(envelope, contract, SchemaRegistry())

    assert {error.code for error in result.errors} == {
        "CONTRACT_VERSION_MISMATCH",
        "UNREGISTERED_SCHEMA",
    }

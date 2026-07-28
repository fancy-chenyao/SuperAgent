from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.orchestration.schema_registry import SchemaRegistry

from .agent_contract import AgentContract


class AgentResultStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"


class AgentResultError(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class AgentResultMetadata(BaseModel):
    producer_agent: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


class AgentResultEnvelope(BaseModel):
    contract_version: str = "1.0"
    status: AgentResultStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: AgentResultError | None = None
    metadata: AgentResultMetadata

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_status_combination(self) -> "AgentResultEnvelope":
        if self.status == AgentResultStatus.SUCCESS:
            if not self.outputs:
                raise ValueError("success must contain at least one output")
            if self.error is not None:
                raise ValueError("success must not contain an error")
        elif self.status == AgentResultStatus.ERROR:
            if self.error is None:
                raise ValueError("error status must contain an error object")
            if self.outputs:
                raise ValueError("error status must not contain outputs")
        elif not self.outputs or self.error is None:
            raise ValueError("partial must contain both outputs and an error")
        return self


class AgentContractValidationError(BaseModel):
    code: str
    message: str
    logical_name: str | None = None
    schema_ref: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AgentContractValidationResult(BaseModel):
    valid: bool
    errors: list[AgentContractValidationError] = Field(default_factory=list)


def validate_agent_result(
    envelope: AgentResultEnvelope | dict[str, Any],
    agent_contract: AgentContract,
    schema_registry: SchemaRegistry,
) -> AgentContractValidationResult:
    """Validate an already-normalized Agent result; never adapt legacy output."""

    try:
        parsed = (
            envelope
            if isinstance(envelope, AgentResultEnvelope)
            else AgentResultEnvelope.model_validate(envelope)
        )
    except ValidationError as exc:
        return AgentContractValidationResult(
            valid=False,
            errors=[
                AgentContractValidationError(
                    code="INVALID_ENVELOPE",
                    message="Agent result envelope is invalid",
                    details={"validation_errors": exc.errors(include_url=False)},
                )
            ],
        )

    errors: list[AgentContractValidationError] = []
    if parsed.contract_version != agent_contract.contract_version:
        errors.append(
            AgentContractValidationError(
                code="CONTRACT_VERSION_MISMATCH",
                message=(
                    f"result contract_version {parsed.contract_version!r} does not "
                    f"match Agent contract {agent_contract.contract_version!r}"
                ),
            )
        )

    produced = {ref.name: ref for ref in agent_contract.produces}
    for logical_name, payload in parsed.outputs.items():
        ref = produced.get(logical_name)
        if ref is None:
            errors.append(
                AgentContractValidationError(
                    code="UNDECLARED_OUTPUT",
                    message=f"output {logical_name!r} is not declared by the Agent",
                    logical_name=logical_name,
                )
            )
            continue
        if not schema_registry.has(ref.schema_ref):
            errors.append(
                AgentContractValidationError(
                    code="UNREGISTERED_SCHEMA",
                    message=f"schema {ref.schema_ref!r} is not registered",
                    logical_name=logical_name,
                    schema_ref=ref.schema_ref,
                )
            )
            continue
        valid, schema_errors = schema_registry.validate(payload, ref.schema_ref)
        if not valid:
            errors.append(
                AgentContractValidationError(
                    code="SCHEMA_VALIDATION_FAILED",
                    message=f"output {logical_name!r} failed schema validation",
                    logical_name=logical_name,
                    schema_ref=ref.schema_ref,
                    details={"errors": schema_errors},
                )
            )

    if parsed.status == AgentResultStatus.SUCCESS:
        for ref in agent_contract.produces:
            if ref.required and ref.name not in parsed.outputs:
                errors.append(
                    AgentContractValidationError(
                        code="MISSING_REQUIRED_OUTPUT",
                        message=f"required output {ref.name!r} is missing",
                        logical_name=ref.name,
                        schema_ref=ref.schema_ref,
                    )
                )

    return AgentContractValidationResult(valid=not errors, errors=errors)

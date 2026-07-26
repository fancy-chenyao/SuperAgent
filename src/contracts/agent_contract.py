from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DataContractRef(BaseModel):
    """A logical business datum and the schema used to validate its payload."""

    name: str = Field(min_length=1)
    schema_ref: str = Field(min_length=1)
    required: bool = True
    cardinality: Literal["one", "many"] = "one"

    model_config = ConfigDict(extra="forbid")


class AgentContract(BaseModel):
    """Versioned business input/output contract for one Agent."""

    contract_version: str = "1.0"
    requires: list[DataContractRef] = Field(default_factory=list)
    produces: list[DataContractRef] = Field(default_factory=list)
    input_schema_refs: dict[str, str] = Field(default_factory=dict)
    output_schema_refs: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_references(self) -> "AgentContract":
        for field_name, refs in (
            ("requires", self.requires),
            ("produces", self.produces),
        ):
            names = [ref.name for ref in refs]
            if len(names) != len(set(names)):
                raise ValueError(f"{field_name} contains duplicate logical names")

        required_refs = {ref.name: ref.schema_ref for ref in self.requires}
        produced_refs = {ref.name: ref.schema_ref for ref in self.produces}
        if self.input_schema_refs and self.input_schema_refs != required_refs:
            raise ValueError("input_schema_refs must match requires")
        if self.output_schema_refs and self.output_schema_refs != produced_refs:
            raise ValueError("output_schema_refs must match produces")

        self.input_schema_refs = required_refs
        self.output_schema_refs = produced_refs
        return self

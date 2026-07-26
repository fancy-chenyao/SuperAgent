from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts import AgentCard, AgentContract, DataContractRef


def test_agent_contract_materializes_schema_reference_maps() -> None:
    contract = AgentContract(
        requires=[
            DataContractRef(
                name="report.sources",
                schema_ref="report.sources@v1",
            )
        ],
        produces=[
            DataContractRef(
                name="report.markdown",
                schema_ref="report.markdown@v1",
            )
        ],
    )

    assert contract.input_schema_refs == {"report.sources": "report.sources@v1"}
    assert contract.output_schema_refs == {"report.markdown": "report.markdown@v1"}


def test_agent_contract_rejects_duplicate_logical_names() -> None:
    with pytest.raises(ValidationError, match="duplicate logical names"):
        AgentContract(
            produces=[
                DataContractRef(name="policy.info", schema_ref="policy.info@v1"),
                DataContractRef(name="policy.info", schema_ref="policy.info@v2"),
            ]
        )


def test_agent_card_retains_legacy_and_explicit_contract_fields() -> None:
    contract = AgentContract(
        produces=[DataContractRef(name="policy.info", schema_ref="policy.info@v1")]
    )
    card = AgentCard(
        agent_id="knowledge",
        name="knowledge",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        contract_version=contract.contract_version,
        produces=contract.produces,
        output_schema_refs=contract.output_schema_refs,
        agent_contract=contract,
    )

    assert card.input_schema == {"type": "object"}
    assert card.output_schema == {"type": "object"}
    assert card.produces[0].name == "policy.info"

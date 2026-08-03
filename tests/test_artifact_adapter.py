"""Unit tests for the executor-result -> Artifact adapter (Plan Phase 2)."""

from src.interface.artifact import ArtifactRef
from src.interface.task_graph import TaskStep
from src.manager.executor.artifact_adapter import to_artifact
from src.manager.executor.base import ExecuteResult, ExecutionContext, ExecutionStatus
from src.orchestration.schema_registry import SchemaRegistry


def _ok(result) -> ExecuteResult:
    return ExecuteResult(status=ExecutionStatus.SUCCESS, result=result)


def test_typed_result_validates_against_schema():
    reg = SchemaRegistry()
    reg.register(
        "person@v1",
        {"required": ["name"], "properties": {"name": {"type": "string"}}},
    )
    step = TaskStep(step_id="s1", expected_outputs=["person_info"])
    art = to_artifact(
        _ok({"name": "王强", "id_number": "86000103"}),
        step=step,
        schema_ref="person@v1",
        schema_registry=reg,
    )
    assert art.logical_name == "person_info"
    assert art.schema_ref == "person@v1"
    assert art.schema_valid is True
    assert art.checksum  # computed
    assert art.payload["name"] == "王强"


def test_schema_mismatch_flags_invalid_with_errors():
    reg = SchemaRegistry()
    reg.register(
        "person@v1", {"required": ["name"], "properties": {"name": {"type": "string"}}})
    art = to_artifact(
        _ok({"id": 1}), schema_ref="person@v1", schema_registry=reg)
    assert art.schema_valid is False
    assert art.metadata.get("schema_errors")


def test_read_only_untyped_result_degraded_low_confidence():
    ctx = ExecutionContext(user_id="u", metadata={"operation_mode": "read"})
    art = to_artifact(_ok("some free-form summary text"), context=ctx)
    assert art.schema_valid is None
    assert art.metadata["typed"] is False
    assert art.metadata["confidence"] == "low"


def test_write_untyped_result_is_flagged_invalid_and_warned():
    ctx = ExecutionContext(user_id="u", metadata={"operation_mode": "write"})
    art = to_artifact(_ok({"sent": True}), context=ctx)
    # Untyped write output must not be consumed downstream as typed.
    assert art.schema_valid is False
    assert "warning" in art.metadata


def test_known_document_contract_validates_with_default_registry():
    step = TaskStep(
        step_id="document",
        operation_mode="write",
        expected_schema_ref="document_generation_result@v1",
    )
    art = to_artifact(
        _ok(
            {
                "status": "success",
                "file_path": r"E:\Program\SuperAgent\output\income_proof.docx",
                "file_name": "income_proof.docx",
                "template_used": "income_proof",
                "message": "document generated",
            }
        ),
        step=step,
    )

    assert art.schema_ref == "document_generation_result@v1"
    assert art.schema_valid is True


def test_known_email_contract_validates_with_default_registry():
    step = TaskStep(
        step_id="email",
        operation_mode="send",
        expected_schema_ref="email_dispatch_result@v1",
    )
    art = to_artifact(
        _ok(
            {
                "status": "success",
                "sent": {
                    "id": "email-1",
                    "to": "manager@example.test",
                    "subject": "Income proof",
                },
            }
        ),
        step=step,
    )

    assert art.schema_ref == "email_dispatch_result@v1"
    assert art.schema_valid is True


def test_known_employee_array_contract_validates_with_default_registry():
    step = TaskStep(
        step_id="employee",
        operation_mode="read",
        expected_schema_ref="employee_query_result@v1",
    )
    art = to_artifact(
        _ok([{"adtEmpeNm": "李娜", "monthly_salary": 22000.0}]),
        step=step,
    )

    assert art.schema_ref == "employee_query_result@v1"
    assert art.schema_valid is True


def test_known_markdown_text_contract_validates_with_default_registry():
    step = TaskStep(
        step_id="report",
        operation_mode="write",
        expected_schema_ref="markdown_text_result@v1",
    )
    art = to_artifact(_ok("# Trusted report\n\nAvailable information only."), step=step)

    assert art.schema_ref == "markdown_text_result@v1"
    assert art.schema_valid is True


def test_json_string_result_coerced_to_dict_payload():
    art = to_artifact(_ok('{"template_name": "income_proof"}'))
    assert isinstance(art.payload, dict)
    assert art.payload["template_name"] == "income_proof"


def test_lineage_carried_from_step_required_inputs():
    upstream = ArtifactRef(artifact_id="up-1", version=1)
    step = TaskStep(
        step_id="s2",
        required_inputs={"employee": upstream},
        expected_outputs=["doc"],
    )
    art = to_artifact(_ok({"doc": "x"}), step=step)
    assert len(art.derived_from) == 1
    assert art.derived_from[0].artifact_id == "up-1"


def test_none_result_still_valid_artifact():
    art = to_artifact(ExecuteResult(
        status=ExecutionStatus.SUCCESS, result=None))
    # payload must never be None (Artifact model requires payload or uri)
    assert art.payload is not None


# --------------------------------------------------------------------------- #
# C3: producer/owner provenance + upstream-aware sensitivity
# --------------------------------------------------------------------------- #
def test_producer_owner_and_provenance_metadata():
    ctx = ExecutionContext(
        user_id="alice",
        metadata={
            "operation_mode": "read",
            "producer_agent_id": "RemoteHRAssistantAgent",
            "risk_profile": "LOW",
            "scenario_tags": ["hr_service"],
            "expected_capabilities": ["HR"],
        },
    )
    art = to_artifact(_ok({"name": "王强"}), context=ctx)
    assert art.metadata["owner_user_id"] == "alice"
    assert art.metadata["producer_subject"] == "alice"
    assert art.metadata["producer_agent_id"] == "RemoteHRAssistantAgent"
    assert art.metadata["data_source"] == "RemoteHRAssistantAgent"
    assert art.metadata["risk_level"] == "LOW"
    assert art.metadata["scenario_tags"] == ["hr_service"]


def test_sensitivity_raised_by_high_risk():
    ctx = ExecutionContext(user_id="u", metadata={"risk_profile": "HIGH"})
    art = to_artifact(_ok({"x": 1}), context=ctx)
    assert art.sensitivity == "confidential"


def test_sensitivity_raised_by_upstream_lineage():
    """A low-risk step consuming CONFIDENTIAL upstream data stays CONFIDENTIAL."""
    ctx = ExecutionContext(user_id="u", metadata={"risk_profile": "LOW"})
    art = to_artifact(
        _ok({"x": 1}), context=ctx, upstream_sensitivities=["confidential"]
    )
    assert art.sensitivity == "confidential"


def test_untrusted_allowed_reader_ids_from_step_are_not_propagated():
    """Planner-shaped step extras must not grant cross-user Artifact access."""
    ctx = ExecutionContext(user_id="alice", metadata={
                           "operation_mode": "read"})
    step = TaskStep(step_id="s1", expected_outputs=[
                    "person_info"], allowed_reader_ids=["bob", "carol"])
    art = to_artifact(_ok({"name": "王强"}), step=step, context=ctx)
    assert art.metadata["owner_user_id"] == "alice"
    assert "allowed_reader_ids" not in art.metadata


def test_trusted_allowed_reader_ids_from_server_context_are_propagated():
    ctx = ExecutionContext(
        user_id="alice",
        metadata={
            "operation_mode": "read",
            "trusted_allowed_reader_ids": ["bob"],
        },
    )
    art = to_artifact(_ok({"x": 1}), context=ctx)
    assert art.metadata["allowed_reader_ids"] == ["bob"]
    assert art.metadata["reader_grants_source"] == "trusted_server"

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
    reg.register("person@v1", {"required": ["name"], "properties": {"name": {"type": "string"}}})
    art = to_artifact(_ok({"id": 1}), schema_ref="person@v1", schema_registry=reg)
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
    art = to_artifact(ExecuteResult(status=ExecutionStatus.SUCCESS, result=None))
    # payload must never be None (Artifact model requires payload or uri)
    assert art.payload is not None

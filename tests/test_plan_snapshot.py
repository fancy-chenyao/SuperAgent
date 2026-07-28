"""T11: PlanSnapshot hashing, persistence and consistency validation (C5)."""

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.orchestration.plan_snapshot import (
    CONVERTER_VERSION,
    SCHEMA_VERSION,
    load_plan_snapshot,
    plan_hash,
    save_plan_snapshot,
    snapshot_hash,
    validate_snapshot,
    verify_snapshot_for_execution,
)
from src.orchestration.plan_to_task_graph import plan_to_task_graph

_STEPS = [
    {"agent_name": "RemoteHRAssistantAgent", "title": "查询"},
    {
        "agent_name": "RemoteDocumentGeneratorAgent",
        "title": "生成",
        "inputs": [
            {"parameter_name": "employee", "source_step": "RemoteHRAssistantAgent",
                "source_output": "person_info"}
        ],
    },
]


def test_plan_hash_stable_and_order_sensitive():
    assert plan_hash(_STEPS) == plan_hash(_STEPS)
    reordered = list(reversed(_STEPS))
    assert plan_hash(reordered) != plan_hash(_STEPS)


def test_save_load_roundtrip(tmp_path):
    tg = plan_to_task_graph(_STEPS, task_id="wf-1", subject="u1").model_dump()
    saved = save_plan_snapshot(
        workflow_id="wf-1", user_id="u1", planning_steps=_STEPS,
        task_graph=tg, base_dir=tmp_path,
    )
    assert saved["schema_version"] == SCHEMA_VERSION
    loaded = load_plan_snapshot("wf-1", base_dir=tmp_path)
    assert loaded is not None
    assert loaded["plan_hash"] == plan_hash(_STEPS)
    assert loaded["task_graph"]["spec"]["task_id"] == "wf-1"


def test_load_missing_returns_none(tmp_path):
    assert load_plan_snapshot("does-not-exist", base_dir=tmp_path) is None


def test_validate_ok():
    tg = plan_to_task_graph(_STEPS, task_id="wf-1", subject="u1").model_dump()
    snap = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": "wf-1",
        "user_id": "u1",
        "planning_steps": _STEPS,
        "task_graph": tg,
        "plan_hash": plan_hash(_STEPS),
    }
    ok, reason = validate_snapshot(
        snap, workflow_id="wf-1", user_id="u1", planning_steps=_STEPS)
    assert ok and reason == "ok"


def test_validate_detects_mismatches():
    tg = plan_to_task_graph(_STEPS, task_id="wf-1", subject="u1").model_dump()
    base = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": "wf-1",
        "user_id": "u1",
        "planning_steps": _STEPS,
        "task_graph": tg,
        "plan_hash": plan_hash(_STEPS),
    }
    # workflow mismatch
    ok, _ = validate_snapshot(
        base, workflow_id="other", user_id="u1", planning_steps=_STEPS)
    assert not ok
    # user mismatch
    ok, _ = validate_snapshot(base, workflow_id="wf-1",
                              user_id="bob", planning_steps=_STEPS)
    assert not ok
    # plan hash mismatch (plan changed)
    ok, _ = validate_snapshot(
        base, workflow_id="wf-1", user_id="u1", planning_steps=[{"agent_name": "X"}]
    )
    assert not ok
    # schema version mismatch
    stale = dict(base, schema_version=SCHEMA_VERSION + 1)
    ok, _ = validate_snapshot(
        stale, workflow_id="wf-1", user_id="u1", planning_steps=_STEPS)
    assert not ok
    # missing snapshot
    ok, _ = validate_snapshot(None, workflow_id="wf-1",
                              user_id="u1", planning_steps=_STEPS)
    assert not ok


# --------------------------------------------------------------------------- #
# Part 3: snapshot bound to the DERIVED task graph (rebuild + deep compare)
# --------------------------------------------------------------------------- #
def _saved_snapshot(tmp_path, steps=_STEPS):
    tg = plan_to_task_graph(steps, task_id="wf-1", subject="u1").model_dump()
    return save_plan_snapshot(
        workflow_id="wf-1", user_id="u1", planning_steps=steps,
        task_graph=tg, base_dir=tmp_path,
    )


def _reseal(snapshot):
    """Recompute snapshot_hash so a tamper passes the content-integrity check,
    isolating the rebuild-and-compare guarantee (the primary defense)."""
    snapshot["snapshot_hash"] = snapshot_hash(
        workflow_id=snapshot["workflow_id"],
        user_id=snapshot["user_id"],
        planning_steps=snapshot["planning_steps"],
        task_graph=snapshot["task_graph"],
    )
    return snapshot


def _verify(snap, planning_steps=_STEPS):
    return verify_snapshot_for_execution(
        snap, workflow_id="wf-1", user_id="u1", planning_steps=planning_steps)


def test_verify_unmodified_snapshot_enters_scheduler(tmp_path):
    tg, reason = _verify(_saved_snapshot(tmp_path))
    assert reason == "ok"
    assert tg is not None and tg["spec"]["task_id"] == "wf-1"


def test_verify_rejects_resealed_task_graph_spec_tampering(tmp_path):
    snap = _saved_snapshot(tmp_path)
    snap["task_graph"]["spec"]["subject"] = "other-user"
    snap["task_graph"]["spec"]["task_id"] = "other-workflow"
    _reseal(snap)

    tg, reason = _verify(snap)

    assert tg is None and "task_graph mismatch" in reason


def test_verify_compares_goal_from_trusted_execution_state(tmp_path):
    tg = plan_to_task_graph(
        _STEPS, task_id="wf-1", subject="u1", goal="approved goal"
    ).model_dump()
    snap = save_plan_snapshot(
        workflow_id="wf-1",
        user_id="u1",
        planning_steps=_STEPS,
        task_graph=tg,
        base_dir=tmp_path,
    )

    accepted, accepted_reason = verify_snapshot_for_execution(
        snap,
        workflow_id="wf-1",
        user_id="u1",
        planning_steps=_STEPS,
        goal="approved goal",
    )
    rejected, rejected_reason = verify_snapshot_for_execution(
        snap,
        workflow_id="wf-1",
        user_id="u1",
        planning_steps=_STEPS,
        goal="different goal",
    )

    assert accepted is not None and accepted_reason == "ok"
    assert rejected is None and "task_graph mismatch" in rejected_reason


def test_verify_rejects_modified_operation_mode(tmp_path):
    snap = _saved_snapshot(tmp_path)
    snap["task_graph"]["steps"][0]["operation_mode"] = "send"
    _reseal(snap)  # even with a recomputed hash, rebuild-compare must reject
    tg, reason = _verify(snap)
    assert tg is None and "task_graph mismatch" in reason


def test_verify_rejects_modified_preferred_resource_id(tmp_path):
    snap = _saved_snapshot(tmp_path)
    snap["task_graph"]["steps"][0]["preferred_resource_id"] = "EvilAgent"
    _reseal(snap)
    tg, reason = _verify(snap)
    assert tg is None and "task_graph mismatch" in reason


def test_verify_rejects_modified_dependency_or_binding(tmp_path):
    snap = _saved_snapshot(tmp_path)
    # Drop the downstream step's declared dependency + output binding.
    snap["task_graph"]["steps"][1]["depends_on"] = []
    snap["task_graph"]["steps"][1]["input_bindings"] = []
    _reseal(snap)
    tg, reason = _verify(snap)
    assert tg is None and "task_graph mismatch" in reason


def test_verify_rejects_swapped_task_graph_with_same_planning_steps(tmp_path):
    snap = _saved_snapshot(tmp_path)
    # Same (unchanged) planning steps, but a different legal task graph swapped in.
    other = plan_to_task_graph(
        [{"agent_name": "RemoteHRAssistantAgent", "title": "仅一步"}],
        task_id="wf-1", subject="u1",
    ).model_dump()
    snap["task_graph"] = other
    _reseal(snap)
    tg, reason = _verify(snap)
    assert tg is None and "task_graph mismatch" in reason


def test_verify_rejects_modified_planning_steps(tmp_path):
    snap = _saved_snapshot(tmp_path)
    modified = _STEPS + \
        [{"agent_name": "RemoteEmailDispatchAgent", "title": "额外一步"}]
    tg, reason = _verify(snap, planning_steps=modified)
    assert tg is None and "task_graph mismatch" in reason


def test_verify_rejects_schema_version_mismatch(tmp_path):
    snap = _saved_snapshot(tmp_path)
    snap["schema_version"] = SCHEMA_VERSION + 1
    tg, reason = _verify(snap)
    assert tg is None and "schema_version mismatch" in reason


def test_verify_rejects_converter_version_mismatch(tmp_path):
    snap = _saved_snapshot(tmp_path)
    snap["converter_version"] = CONVERTER_VERSION + 1
    tg, reason = _verify(snap)
    assert tg is None and "converter_version mismatch" in reason


def test_verify_rejects_corrupt_snapshot_hash(tmp_path):
    snap = _saved_snapshot(tmp_path)
    # Tamper the stored graph WITHOUT recomputing the hash (file corruption).
    snap["task_graph"]["steps"][0]["operation_mode"] = "send"
    tg, reason = _verify(snap)
    assert tg is None and "snapshot_hash mismatch" in reason


def test_saved_snapshot_carries_converter_version_and_hash(tmp_path):
    snap = _saved_snapshot(tmp_path)
    assert snap["converter_version"] == CONVERTER_VERSION
    assert snap["snapshot_hash"]
    loaded = load_plan_snapshot("wf-1", base_dir=tmp_path)
    assert loaded["snapshot_hash"] == snap["snapshot_hash"]


def test_verify_uses_current_trusted_agent_contract(tmp_path):
    contract = AgentContract(
        produces=[
            DataContractRef(name="employee.info", schema_ref="employee.info@v1")
        ]
    )
    graph = plan_to_task_graph(
        [_STEPS[0]],
        task_id="wf-1",
        subject="u1",
        agent_contracts={"RemoteHRAssistantAgent": contract},
    ).model_dump()
    snap = save_plan_snapshot(
        workflow_id="wf-1",
        user_id="u1",
        planning_steps=[_STEPS[0]],
        task_graph=graph,
        base_dir=tmp_path,
    )

    accepted, accepted_reason = verify_snapshot_for_execution(
        snap,
        workflow_id="wf-1",
        user_id="u1",
        planning_steps=[_STEPS[0]],
        current_agent_contracts={
            "RemoteHRAssistantAgent": contract.model_dump(mode="json")
        },
    )
    missing, missing_reason = verify_snapshot_for_execution(
        snap,
        workflow_id="wf-1",
        user_id="u1",
        planning_steps=[_STEPS[0]],
        current_agent_contracts={},
    )
    changed_contract = AgentContract(
        produces=[
            DataContractRef(name="employee.info", schema_ref="employee.info@v2")
        ]
    )
    changed, changed_reason = verify_snapshot_for_execution(
        snap,
        workflow_id="wf-1",
        user_id="u1",
        planning_steps=[_STEPS[0]],
        current_agent_contracts={
            "RemoteHRAssistantAgent": changed_contract.model_dump(mode="json")
        },
    )

    assert accepted is not None and accepted_reason == "ok"
    assert missing is None and "current Agent contract missing" in missing_reason
    assert changed is None and "task_graph mismatch" in changed_reason

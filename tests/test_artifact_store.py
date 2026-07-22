"""Unit tests for the Artifact data plane: store, resolver, schema registry.

Isolated: imports only pure interface types + orchestration data-plane modules
(pydantic only), no workflow/manager/LLM stack.
"""

import pytest
from pydantic import ValidationError

from src.interface.artifact import (
    Artifact,
    ArtifactRef,
    StepResult,
    StepStatus,
    compute_checksum,
)
from src.orchestration.resolver import (
    AllowAllGuard,
    ArtifactAccessDenied,
    ArtifactResolver,
    ArtifactSchemaIncompatible,
    ArtifactSchemaInvalid,
)
from src.orchestration.schema_registry import SchemaRegistry
from src.orchestration.store import (
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactStoreCorruption,
)


# --------------------------------------------------------------------------- #
# Artifact model
# --------------------------------------------------------------------------- #
def test_artifact_requires_payload_or_uri():
    with pytest.raises(ValidationError):
        Artifact(logical_name="empty")


def test_artifact_ref_and_checksum():
    art = Artifact(logical_name="person", payload={
                   "name": "王强"}, schema_ref="person@v1")
    ref = art.ref("name")
    assert ref.artifact_id == art.artifact_id
    assert ref.version == art.version
    assert ref.selector == "name"
    assert ref.expected_schema_ref == "person@v1"

    assert compute_checksum(
        {"a": 1, "b": 2}) == compute_checksum({"b": 2, "a": 1})
    filled = art.with_checksum()
    assert filled.checksum == compute_checksum({"name": "王强"})


def test_step_result_success_flag():
    ok = StepResult(step_id="s1", status=StepStatus.SUCCEEDED)
    bad = StepResult(step_id="s2", status=StepStatus.FAILED, error="boom")
    assert ok.is_success is True
    assert bad.is_success is False
    assert bad.error == "boom"


# --------------------------------------------------------------------------- #
# ArtifactStore: put / get / exists / versioning / immutability
# --------------------------------------------------------------------------- #
def test_put_returns_v1_and_get_roundtrip():
    store = ArtifactStore()
    ref = store.put(Artifact(logical_name="p", payload={"name": "王强"}))
    assert ref.version == 1
    assert store.exists(ref) is True
    assert store.get(ref).payload == {"name": "王强"}


def test_put_creates_new_version_not_in_place():
    store = ArtifactStore()
    ref1 = store.put(Artifact(logical_name="p", payload={"name": "王强"}))

    updated = store.get(ref1)
    updated.payload = {"name": "王强", "id": "86000103"}
    ref2 = store.put(updated)

    assert ref2.artifact_id == ref1.artifact_id
    assert ref2.version == 2
    # old version preserved
    assert store.get(ref1).payload == {"name": "王强"}
    assert store.get(ref2).payload["id"] == "86000103"
    # version-less ref resolves to latest
    latest = store.get(ArtifactRef(artifact_id=ref1.artifact_id))
    assert latest.version == 2
    assert store.latest_version(ref1.artifact_id) == 2


def test_store_returns_copies_immutable():
    store = ArtifactStore()
    ref = store.put(Artifact(logical_name="p", payload={"k": "v"}))
    got = store.get(ref)
    got.payload["k"] = "MUTATED"
    assert store.get(ref).payload["k"] == "v"


def test_get_unknown_raises():
    store = ArtifactStore()
    with pytest.raises(ArtifactNotFoundError):
        store.get(ArtifactRef(artifact_id="does-not-exist"))
    assert store.exists(ArtifactRef(artifact_id="nope")) is False


# --------------------------------------------------------------------------- #
# ArtifactResolver: selector + guard allow/deny
# --------------------------------------------------------------------------- #
def _seed_resolver(guard=None):
    store = ArtifactStore()
    ref = store.put(
        Artifact(
            logical_name="record",
            payload={"data": {"name": "王强", "rows": [
                {"id": "1"}, {"id": "2"}]}},
        )
    )
    return ArtifactResolver(store, guard=guard), ref


def test_resolver_no_selector_returns_full_payload():
    resolver, ref = _seed_resolver()
    assert resolver.resolve(ref) == {
        "data": {"name": "王强", "rows": [{"id": "1"}, {"id": "2"}]}
    }


def test_resolver_dict_and_list_selector():
    resolver, ref = _seed_resolver()
    name_ref = ArtifactRef(artifact_id=ref.artifact_id, selector="data.name")
    row_ref = ArtifactRef(artifact_id=ref.artifact_id,
                          selector="data.rows.1.id")
    assert resolver.resolve(name_ref) == "王强"
    assert resolver.resolve(row_ref) == "2"


def test_resolver_bad_selector_raises():
    resolver, ref = _seed_resolver()
    bad = ArtifactRef(artifact_id=ref.artifact_id, selector="data.missing")
    with pytest.raises(KeyError):
        resolver.resolve(bad)


def test_resolver_allow_all_guard_allows():
    resolver, ref = _seed_resolver(guard=AllowAllGuard())
    assert resolver.resolve(ref)["data"]["name"] == "王强"


def test_resolver_deny_guard_blocks():
    class DenyGuard:
        def can_read(self, *, subject, artifact, scenario=None, action="read"):
            return False

    resolver, ref = _seed_resolver(guard=DenyGuard())
    with pytest.raises(ArtifactAccessDenied):
        resolver.resolve(ref, subject="alice")


def test_resolver_guard_receives_subject_and_action():
    seen = {}

    class RecordingGuard:
        def can_read(self, *, subject, artifact, scenario=None, action="read"):
            seen["subject"] = subject
            seen["action"] = action
            seen["artifact_name"] = artifact.logical_name
            return True

    resolver, ref = _seed_resolver(guard=RecordingGuard())
    resolver.resolve(ref, subject="bob", action="read")
    assert seen == {"subject": "bob",
                    "action": "read", "artifact_name": "record"}


# --------------------------------------------------------------------------- #
# C3: resolver rejects invalid / incompatible schemas (fail closed)
# --------------------------------------------------------------------------- #
def test_resolver_rejects_schema_invalid_artifact():
    store = ArtifactStore()
    ref = store.put(
        Artifact(logical_name="sent", payload={"ok": True}, schema_valid=False)
    )
    resolver = ArtifactResolver(store, guard=AllowAllGuard())
    with pytest.raises(ArtifactSchemaInvalid):
        resolver.resolve(ref)


def test_resolver_rejects_incompatible_expected_schema():
    store = ArtifactStore()
    stored = store.put(
        Artifact(logical_name="doc", payload={"a": 1}, schema_ref="doc@v1")
    )
    # Ask for a different schema than the artifact actually has.
    bad = ArtifactRef(artifact_id=stored.artifact_id,
                      expected_schema_ref="doc@v2")
    resolver = ArtifactResolver(store, guard=AllowAllGuard())
    with pytest.raises(ArtifactSchemaIncompatible):
        resolver.resolve(bad)


# --------------------------------------------------------------------------- #
# C3: load_state fails closed on corruption (never silently skips)
# --------------------------------------------------------------------------- #
def _dump_one() -> dict:
    store = ArtifactStore()
    store.put(Artifact(logical_name="p", payload={
              "name": "王强"}).with_checksum())
    return store.dump_state()


def test_load_state_roundtrip_ok():
    data = _dump_one()
    restored = ArtifactStore()
    restored.load_state(data)  # valid -> no raise
    assert len(restored.dump_state()) == 1


def test_load_state_rejects_checksum_mismatch():
    data = _dump_one()
    aid = next(iter(data))
    # checksum no longer matches
    data[aid]["1"]["payload"] = {"name": "TAMPERED"}
    with pytest.raises(ArtifactStoreCorruption):
        ArtifactStore().load_state(data)


def test_load_state_rejects_id_mismatch():
    data = _dump_one()
    aid = next(iter(data))
    data[aid]["1"]["artifact_id"] = "different-id"
    with pytest.raises(ArtifactStoreCorruption):
        ArtifactStore().load_state(data)


def test_load_state_rejects_version_mismatch():
    data = _dump_one()
    aid = next(iter(data))
    data[aid]["1"]["version"] = 99  # key says 1, payload says 99
    with pytest.raises(ArtifactStoreCorruption):
        ArtifactStore().load_state(data)


# --------------------------------------------------------------------------- #
# SchemaRegistry
# --------------------------------------------------------------------------- #
def test_schema_validate_ok_and_missing_required():
    reg = SchemaRegistry()
    reg.register(
        "person@v1",
        {"required": ["name"], "properties": {
            "name": {"type": "string"}, "age": {"type": "integer"}}},
    )
    ok, errs = reg.validate({"name": "王强", "age": 30}, "person@v1")
    assert ok and errs == []

    ok, errs = reg.validate({"age": 30}, "person@v1")
    assert not ok
    assert any("name" in e for e in errs)


def test_schema_type_mismatch_and_unknown_schema():
    reg = SchemaRegistry()
    reg.register("person@v1", {"properties": {"name": {"type": "string"}}})
    ok, errs = reg.validate({"name": 123}, "person@v1")
    assert not ok
    assert any("expected string" in e for e in errs)

    ok, errs = reg.validate({"name": "x"}, "unknown@v9")
    assert not ok
    assert any("unknown schema_ref" in e for e in errs)


def test_schema_bool_not_accepted_as_integer():
    reg = SchemaRegistry()
    reg.register("f@v1", {"properties": {"n": {"type": "integer"}}})
    ok, errs = reg.validate({"n": True}, "f@v1")
    assert not ok
    assert any("bool" in e for e in errs)


def test_schema_additional_properties_false():
    reg = SchemaRegistry()
    reg.register(
        "strict@v1",
        {"properties": {"a": {"type": "string"}}, "additional_properties": False},
    )
    ok, errs = reg.validate({"a": "x", "b": "y"}, "strict@v1")
    assert not ok
    assert any("unexpected field" in e for e in errs)


def test_schema_number_accepts_int_and_float():
    reg = SchemaRegistry()
    reg.register("m@v1", {"properties": {"salary": {"type": "number"}}})
    assert reg.validate({"salary": 28000}, "m@v1")[0] is True
    assert reg.validate({"salary": 28000.5}, "m@v1")[0] is True
    assert reg.validate({"salary": "28000"}, "m@v1")[0] is False

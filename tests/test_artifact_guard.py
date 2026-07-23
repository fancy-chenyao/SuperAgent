"""Tests for the PolicyEngine-backed artifact read guard (P0-2, T4)."""

import pytest

import src.service.env as env
from src.interface.artifact import Artifact, Sensitivity
from src.orchestration.artifact_guard import PolicyEngineArtifactGuard
from src.orchestration.resolver import ArtifactAccessDenied, ArtifactResolver
from src.orchestration.store import ArtifactStore


def _artifact(sensitivity, name="data", **metadata):
    return Artifact(
        logical_name=name,
        payload={"value": 1},
        sensitivity=sensitivity,
        metadata=metadata or {},
    )


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path, monkeypatch):
    """Redirect the artifact-access audit log to a per-test file so guard tests
    never append to the repo's default audit log."""
    monkeypatch.setenv("ARTIFACT_AUDIT_LOG", str(tmp_path / "audit.jsonl"))


def test_guard_denies_missing_subject(monkeypatch):
    """A missing subject fails closed -- never promoted to a system subject."""
    monkeypatch.setattr(env, "S_ABAC_ENABLED", False)
    guard = PolicyEngineArtifactGuard()
    assert guard.can_read(subject=None, artifact=_artifact(
        Sensitivity.INTERNAL)) is False
    assert guard.can_read(subject="", artifact=_artifact(
        Sensitivity.PUBLIC)) is False


def test_guard_denies_cross_user_owner_when_sabac_disabled(monkeypatch):
    monkeypatch.setattr(env, "S_ABAC_ENABLED", False)
    guard = PolicyEngineArtifactGuard()
    owned = _artifact(Sensitivity.INTERNAL, owner_user_id="alice")
    # Same owner is allowed; a different user is denied.
    assert guard.can_read(subject="alice", artifact=owned) is True
    assert guard.can_read(subject="bob", artifact=owned) is False


def test_guard_allows_non_sensitive_when_sabac_disabled(monkeypatch):
    monkeypatch.setattr(env, "S_ABAC_ENABLED", False)
    guard = PolicyEngineArtifactGuard()
    assert guard.can_read(subject="u1", artifact=_artifact(
        Sensitivity.INTERNAL)) is True
    assert guard.can_read(
        subject="u1", artifact=_artifact(Sensitivity.PUBLIC)) is True


def test_guard_fails_closed_on_sensitive_when_sabac_disabled(monkeypatch):
    monkeypatch.setattr(env, "S_ABAC_ENABLED", False)
    guard = PolicyEngineArtifactGuard()
    assert guard.can_read(subject="u1", artifact=_artifact(
        Sensitivity.CONFIDENTIAL)) is False
    assert guard.can_read(subject="u1", artifact=_artifact(
        Sensitivity.RESTRICTED)) is False


def test_guard_denies_unknown_user_when_sabac_enabled(monkeypatch):
    monkeypatch.setattr(env, "S_ABAC_ENABLED", True)
    guard = PolicyEngineArtifactGuard()
    # An unknown demo user cannot be resolved to a subject -> fail closed.
    assert guard.can_read(subject="definitely-not-a-real-user",
                          artifact=_artifact(Sensitivity.CONFIDENTIAL)) is False


def test_t4_resolver_denies_reading_salary_artifact(monkeypatch):
    """T4: an unauthorized read of a sensitive (salary) artifact is rejected."""
    monkeypatch.setattr(env, "S_ABAC_ENABLED", False)
    store = ArtifactStore()
    ref = store.put(
        Artifact(
            logical_name="salary_record",
            payload={"employee": "王强", "salary": 42000},
            sensitivity=Sensitivity.CONFIDENTIAL,
        )
    )
    resolver = ArtifactResolver(store, guard=PolicyEngineArtifactGuard())
    with pytest.raises(ArtifactAccessDenied):
        resolver.resolve(ref, subject="ordinary_user")


def test_resolver_allows_non_sensitive_read(monkeypatch):
    monkeypatch.setattr(env, "S_ABAC_ENABLED", False)
    store = ArtifactStore()
    ref = store.put(Artifact(logical_name="weather", payload={
                    "temp": 20}, sensitivity=Sensitivity.INTERNAL))
    resolver = ArtifactResolver(store, guard=PolicyEngineArtifactGuard())
    assert resolver.resolve(ref, subject="ordinary_user") == {"temp": 20}


# --------------------------------------------------------------------------- #
# P0-1: cross-user ownership gate (enforced before the PolicyEngine)
# --------------------------------------------------------------------------- #
def test_guard_denies_cross_user_salary_when_sabac_enabled(monkeypatch):
    """With S-ABAC on, a non-owner (engineer) is denied hr_manager's salary
    artifact at the ownership gate -- before the PolicyEngine is ever consulted
    (which cannot express subject.id == object.owner_user_id)."""
    monkeypatch.setattr(env, "S_ABAC_ENABLED", True)
    guard = PolicyEngineArtifactGuard()

    def _boom(*_a, **_k):  # the policy path must not even be reached
        raise AssertionError(
            "policy engine must not be consulted for a denied owner")

    monkeypatch.setattr(guard, "_evaluate", _boom)
    salary = _artifact(Sensitivity.CONFIDENTIAL, name="salary",
                       owner_user_id="hr_manager")
    assert guard.can_read(subject="engineer", artifact=salary) is False


def test_guard_allows_owner_to_read_own_artifact_when_sabac_enabled(monkeypatch):
    monkeypatch.setattr(env, "S_ABAC_ENABLED", True)
    guard = PolicyEngineArtifactGuard()
    # Isolate the ownership gate: the owner must not be blocked by it, then the
    # (separately tested) policy evaluation decides -- stubbed allow here.
    monkeypatch.setattr(guard, "_evaluate", lambda *a, **k: True)
    salary = _artifact(Sensitivity.CONFIDENTIAL, name="salary",
                       owner_user_id="hr_manager")
    assert guard.can_read(subject="hr_manager", artifact=salary) is True


def test_guard_allows_only_listed_reader_cross_user(monkeypatch):
    monkeypatch.setattr(env, "S_ABAC_ENABLED", True)
    guard = PolicyEngineArtifactGuard()
    monkeypatch.setattr(guard, "_evaluate", lambda *a, **k: True)
    # engineer is explicitly authorized to read hr_manager's artifact.
    listed = _artifact(Sensitivity.CONFIDENTIAL, name="salary",
                       owner_user_id="hr_manager", allowed_reader_ids=["engineer"])
    assert guard.can_read(subject="engineer", artifact=listed) is True
    # A user NOT on the list is denied at the ownership gate (policy stub allow
    # is irrelevant).
    other = _artifact(Sensitivity.CONFIDENTIAL, name="salary",
                      owner_user_id="hr_manager", allowed_reader_ids=["someone_else"])
    assert guard.can_read(subject="engineer", artifact=other) is False


def test_guard_denies_on_policy_exception_for_owner(monkeypatch):
    """Ownership passes but the policy evaluation raises -> fail closed."""
    monkeypatch.setattr(env, "S_ABAC_ENABLED", True)
    guard = PolicyEngineArtifactGuard()

    def _raise(*_a, **_k):
        raise RuntimeError("policy boom")

    monkeypatch.setattr(guard, "_evaluate", _raise)
    art = _artifact(Sensitivity.CONFIDENTIAL, name="salary",
                    owner_user_id="hr_manager")
    assert guard.can_read(subject="hr_manager", artifact=art) is False


def test_resolver_denies_cross_user_salary_end_to_end(monkeypatch):
    """Through the resolver: a non-owner read of an owned salary artifact raises
    ArtifactAccessDenied and never returns the payload."""
    monkeypatch.setattr(env, "S_ABAC_ENABLED", True)
    store = ArtifactStore()
    ref = store.put(
        Artifact(
            logical_name="salary_record",
            payload={"employee": "王强", "salary": 42000},
            sensitivity=Sensitivity.CONFIDENTIAL,
            metadata={"owner_user_id": "hr_manager"},
        )
    )
    resolver = ArtifactResolver(store, guard=PolicyEngineArtifactGuard())
    with pytest.raises(ArtifactAccessDenied):
        resolver.resolve(ref, subject="engineer")


def test_artifact_access_is_audited_without_payload(tmp_path, monkeypatch):
    """Both an allow and a deny decision are written to the persistent audit log
    as METADATA ONLY -- never the artifact payload."""
    from src.orchestration.audit import read_audit_records

    audit_path = tmp_path / "records.jsonl"
    monkeypatch.setenv("ARTIFACT_AUDIT_LOG", str(audit_path))
    monkeypatch.setattr(env, "S_ABAC_ENABLED", True)
    guard = PolicyEngineArtifactGuard()
    monkeypatch.setattr(guard, "_evaluate", lambda *a, **k: True)

    salary = _artifact(Sensitivity.CONFIDENTIAL, name="salary",
                       owner_user_id="hr_manager")
    # A cross-user deny (ownership gate) and a same-owner allow (policy stub).
    assert guard.can_read(subject="engineer", artifact=salary) is False
    assert guard.can_read(subject="hr_manager", artifact=salary) is True

    records = read_audit_records(audit_path)
    assert len(records) == 2
    decisions = {(r["subject"], r["decision"]) for r in records}
    assert ("engineer", "deny") in decisions
    assert ("hr_manager", "allow") in decisions
    for r in records:
        assert r["logical_name"] == "salary"
        assert r["sensitivity"] == Sensitivity.CONFIDENTIAL.value
        # No payload / uri ever recorded.
        assert "payload" not in r and "uri" not in r

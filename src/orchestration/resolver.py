"""Artifact resolver + access guard (Plan §7, Phase 1).

Resolves an :class:`ArtifactRef` to a concrete value (applying its ``selector``)
after an access check via an :class:`ArtifactAccessGuard`.

The guard protocol deliberately reserves ``scenario`` / ``action`` parameters so
Phase 4 can bind a real implementation onto
``src.security.policy.PolicyEngine.evaluate(subject, object, scenario, action)``
without changing this module's call sites.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from src.interface.artifact import Artifact, ArtifactRef
from src.orchestration.store import ArtifactStore


class ArtifactAccessDenied(PermissionError):
    """Raised when the guard denies read access to an artifact."""


class ArtifactSchemaInvalid(ValueError):
    """Raised when an artifact is flagged ``schema_valid=False`` (untyped write/send
    output or a failed schema validation): it must not be consumed downstream."""


class ArtifactSchemaIncompatible(ValueError):
    """Raised when a ref's ``expected_schema_ref`` does not match the artifact's
    actual ``schema_ref``."""


@runtime_checkable
class ArtifactAccessGuard(Protocol):
    """Access-control seam for reading artifacts.

    Implementations must be side-effect free w.r.t. the returned decision.
    ``scenario``/``action`` are optional now and consumed by the Phase 4
    PolicyEngine-backed guard.
    """

    def can_read(
        self,
        *,
        subject: Any,
        artifact: Artifact,
        scenario: Optional[Any] = None,
        action: str = "read",
    ) -> bool: ...


class AllowAllGuard:
    """Permissive guard (default / unit tests). Always allows reads."""

    def can_read(
        self,
        *,
        subject: Any,
        artifact: Artifact,
        scenario: Optional[Any] = None,
        action: str = "read",
    ) -> bool:
        return True


def _apply_selector(value: Any, selector: Optional[str]) -> Any:
    """Navigate ``value`` following a dotted/indexed ``selector``.

    Supports mapping keys and list indices, e.g. ``data.rows.0.id``. Raises
    ``KeyError``/``IndexError`` for a path that does not exist.
    """
    if not selector:
        return value
    current = value
    for token in selector.split("."):
        if isinstance(current, dict):
            if token not in current:
                raise KeyError(f"selector segment {token!r} not found")
            current = current[token]
        elif isinstance(current, (list, tuple)):
            try:
                index = int(token)
            except ValueError as exc:
                raise KeyError(
                    f"selector segment {token!r} is not a valid list index"
                ) from exc
            current = current[index]
        else:
            raise KeyError(
                f"cannot descend into {type(current).__name__} with {token!r}"
            )
    return current


class ArtifactResolver:
    """Resolve refs to values, enforcing read access via a guard."""

    def __init__(
        self,
        store: ArtifactStore,
        guard: Optional[ArtifactAccessGuard] = None,
    ) -> None:
        self.store = store
        self.guard: ArtifactAccessGuard = guard or AllowAllGuard()

    def resolve(
        self,
        ref: ArtifactRef,
        subject: Any = None,
        *,
        scenario: Optional[Any] = None,
        action: str = "read",
        consumer_agent: Optional[str] = None,
    ) -> Any:
        """Return the value referenced by ``ref`` (after access + schema checks).

        Raises :class:`ArtifactAccessDenied` if the guard refuses,
        :class:`ArtifactSchemaInvalid` if the artifact is flagged invalid,
        :class:`ArtifactSchemaIncompatible` on an expected-schema mismatch, or
        ``KeyError``/``IndexError`` if the selector path is invalid.

        ``consumer_agent`` (the agent that will consume the value) is surfaced to
        the guard via the scenario so a downstream consumer can be authorized.
        """
        artifact = self.store.get(
            ref)  # raises ArtifactNotFoundError if missing

        # Pass the consumer agent to the guard without changing its signature:
        # merge it into the scenario mapping (guards accept ``scenario``).
        guard_scenario = scenario
        if consumer_agent is not None:
            base = dict(scenario) if isinstance(scenario, dict) else {}
            base["consumer_agent_id"] = consumer_agent
            guard_scenario = base

        allowed = self.guard.can_read(
            subject=subject, artifact=artifact, scenario=guard_scenario, action=action
        )
        if not allowed:
            raise ArtifactAccessDenied(
                f"subject {subject!r} denied read on artifact "
                f"{artifact.artifact_id!r} ({artifact.logical_name})"
            )

        # An artifact explicitly flagged invalid (untyped write/send output or a
        # failed schema validation) must never be consumed as typed data.
        if artifact.schema_valid is False:
            raise ArtifactSchemaInvalid(
                f"artifact {artifact.artifact_id!r} ({artifact.logical_name}) is "
                f"flagged schema_valid=False and cannot be consumed downstream"
            )

        # A ref declaring an expected schema must match the artifact's schema.
        expected = getattr(ref, "expected_schema_ref", None)
        if expected and artifact.schema_ref and expected != artifact.schema_ref:
            raise ArtifactSchemaIncompatible(
                f"expected schema {expected!r} but artifact "
                f"{artifact.artifact_id!r} has schema {artifact.schema_ref!r}"
            )

        if artifact.payload is None and artifact.uri is not None:
            # Phase 1 store is in-memory/inline only; external URIs are resolved
            # by later phases. Surface the URI so callers can decide.
            return {"uri": artifact.uri}
        return _apply_selector(artifact.payload, ref.selector)

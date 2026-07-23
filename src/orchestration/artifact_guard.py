"""PolicyEngine-backed artifact read guard (Plan §7 / Phase 4 fix).

Binds :class:`~src.orchestration.resolver.ArtifactResolver` reads to the real
S-ABAC :class:`~src.security.policy.PolicyEngine`, so passing HR/salary or other
sensitive data between scheduled steps is subject to the same authorization as
agent dispatch and tool calls -- instead of the permissive ``AllowAllGuard``.

Fail-closed contract:

- Unknown acting user -> deny.
- ``CONFIDENTIAL`` / ``RESTRICTED`` artifacts are denied when S-ABAC is globally
  disabled (no policy engine to consult).
- Any evaluation error -> deny.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.interface.artifact import Artifact, Sensitivity
from src.orchestration.audit import record_artifact_access

logger = logging.getLogger(__name__)

# Artifact sensitivity -> S-ABAC sensitivity bucket (see config SENSITIVITY_LEVELS).
_SENSITIVITY_TO_POLICY = {
    Sensitivity.PUBLIC.value: "LOW",
    Sensitivity.INTERNAL.value: "MEDIUM",
    Sensitivity.CONFIDENTIAL.value: "HIGH",
    Sensitivity.RESTRICTED.value: "CRITICAL",
}
# Buckets that must fail closed when S-ABAC is globally disabled.
_FAIL_CLOSED_WHEN_DISABLED = {"HIGH", "CRITICAL"}


class PolicyEngineArtifactGuard:
    """Artifact read guard backed by the S-ABAC PolicyEngine."""

    def __init__(self, *, scenario: Optional[dict] = None) -> None:
        # Default scenario (e.g. {"scenario_tags": [...], "task_profile": {...}})
        # used when a per-read scenario is not supplied.
        self._scenario = scenario or {}

    @staticmethod
    def _policy_sensitivity(artifact: Artifact) -> str:
        raw = getattr(artifact, "sensitivity", None)
        # Artifact uses ``use_enum_values=True`` so this is already a str, but
        # tolerate an Enum too.
        key = getattr(raw, "value", raw)
        return _SENSITIVITY_TO_POLICY.get(str(key).lower(), "HIGH")

    def can_read(
        self,
        *,
        subject: Any,
        artifact: Artifact,
        scenario: Optional[Any] = None,
        action: str = "read",
    ) -> bool:
        allowed, reason = self._decide(
            subject=subject, artifact=artifact, scenario=scenario, action=action
        )
        # Persist a metadata-only audit record of the allow/deny decision (never
        # the payload). Best-effort: auditing must never change/crash a read.
        record_artifact_access(
            subject=subject, artifact=artifact, allowed=allowed, reason=reason, action=action
        )
        return allowed

    def _decide(
        self,
        *,
        subject: Any,
        artifact: Artifact,
        scenario: Optional[Any],
        action: str,
    ) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a read decision (no side effects)."""
        sensitivity = self._policy_sensitivity(artifact)

        # A missing acting subject fails closed -- never silently promoted to a
        # system/privileged subject.
        if not subject:
            logger.warning(
                "artifact-guard: deny read (no subject) name=%s",
                getattr(artifact, "logical_name", ""),
            )
            return False, "no_subject"

        # Cross-user ownership gate. The current PolicyEngine cannot express a
        # ``subject.id == object.owner_user_id`` field comparison, so ownership
        # is enforced HERE (never merely passed to the engine as an attribute):
        # an artifact owned by another user is denied unless the acting subject
        # is explicitly listed in ``allowed_reader_ids``. This applies whether
        # or not S-ABAC is enabled (fail closed on cross-user reads).
        meta = getattr(artifact, "metadata", None) or {}
        owner = meta.get("owner_user_id")
        allowed_readers = {str(r)
                           for r in (meta.get("allowed_reader_ids") or [])}
        cross_user = bool(owner) and str(owner) != str(subject)
        if cross_user and str(subject) not in allowed_readers:
            logger.warning(
                "artifact-guard: deny cross-user read owner=%s subject=%s name=%s",
                owner,
                subject,
                getattr(artifact, "logical_name", ""),
            )
            return False, "cross_user_denied"

        try:
            from src.service.env import S_ABAC_ENABLED
        except Exception:  # pragma: no cover - env always importable in-repo
            S_ABAC_ENABLED = False

        if not S_ABAC_ENABLED:
            # No policy engine in force: allow only non-sensitive data (the
            # cross-user case is already handled by the ownership gate above).
            if sensitivity in _FAIL_CLOSED_WHEN_DISABLED:
                logger.warning(
                    "artifact-guard: deny read (S-ABAC off, sensitivity=%s) name=%s",
                    sensitivity,
                    getattr(artifact, "logical_name", ""),
                )
                return False, f"sabac_off_sensitive:{sensitivity}"
            return True, "sabac_off_allowed"

        try:
            allowed = self._evaluate(
                subject, artifact, sensitivity, scenario, action)
            return bool(allowed), "policy_allow" if allowed else "policy_deny"
        except Exception as exc:  # noqa: BLE001 - any failure fails closed
            logger.warning(
                "artifact-guard: deny read on evaluation error: %s", exc)
            return False, "policy_error"

    def _evaluate(
        self,
        subject_id: Any,
        artifact: Artifact,
        sensitivity: str,
        scenario: Optional[Any],
        action: str,
    ) -> bool:
        from src.security.context import (
            SecurityContextBuilder,
            UnknownSecurityUserError,
        )
        from src.security.policy import Action, Object, PolicyEngine, Scenario
        from src.security.enforcement import get_policy_engine

        # Subject: an unknown user fails closed.
        if subject_id:
            try:
                subject = SecurityContextBuilder.subject_for_user(
                    str(subject_id))
            except UnknownSecurityUserError:
                logger.warning(
                    "artifact-guard: deny read for unknown user %r", subject_id)
                return False
        else:
            subject = SecurityContextBuilder.system_subject()

        meta = getattr(artifact, "metadata", None) or {}
        obj = Object(
            object_type="artifact",
            id=str(getattr(artifact, "logical_name", "artifact")),
            attributes={
                "type": "artifact",
                "sensitivity": sensitivity,
                "owner_user_id": meta.get("owner_user_id"),
                "scenario_tags": list(meta.get("scenario_tags", []) or []),
                "expected_capabilities": list(meta.get("expected_capabilities", []) or []),
            },
        )

        scenario_src = scenario if isinstance(
            scenario, dict) else self._scenario
        scenario_obj = Scenario(
            task_scenario={
                "operation_mode": action,
                "scenario_tags": scenario_src.get("scenario_tags", []),
                "expected_capabilities": scenario_src.get("expected_capabilities", []),
                "task_type": scenario_src.get("task_type", "GENERAL"),
                "risk_profile": scenario_src.get("risk_profile", "LOW"),
                "scenario_fit_result": scenario_src.get("scenario_fit_result", {}),
                "consumer_agent_id": scenario_src.get("consumer_agent_id"),
            },
        )
        act = Action(
            verb=action,
            attributes={
                "operation_mode": action,
                "action_type": action,
                "consumer_agent_id": scenario_src.get("consumer_agent_id"),
            },
        )

        engine: PolicyEngine = get_policy_engine()
        result = engine.evaluate(subject, obj, scenario_obj, act)
        allowed = bool(result.get("allowed"))
        if not allowed:
            logger.info(
                "artifact-guard: deny read name=%s sensitivity=%s reason=%s",
                obj.id,
                sensitivity,
                result.get("reason"),
            )
        return allowed

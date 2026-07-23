"""Persistent artifact-access audit log (Plan §Batch 2, item 4).

Records every artifact read *decision* (allow/deny) made by the
:class:`~src.orchestration.artifact_guard.PolicyEngineArtifactGuard` to a durable
JSONL file so cross-user access attempts on sensitive data leave an audit trail.

Hard rule: the audit record carries ONLY metadata (acting subject, the artifact's
logical name / sensitivity / owner, the decision and reason). It MUST NEVER
contain the artifact payload (e.g. salary figures) -- the whole point is to audit
*access to* sensitive data without duplicating that data into a second store.

Writes are best-effort and never raise: an audit failure must not block or fail a
read decision (the decision itself is the security control; the log is evidence).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_AUDIT_PATH = "store/audit/artifact_access.jsonl"

# Only these artifact fields are ever recorded (never ``payload``/``uri``).
_ALLOWED_ARTIFACT_FIELDS = ("logical_name", "sensitivity")


def _audit_path() -> Path:
    return Path(os.getenv("ARTIFACT_AUDIT_LOG", _DEFAULT_AUDIT_PATH))


def record_artifact_access(
    *,
    subject: Any,
    artifact: Any,
    allowed: bool,
    reason: str,
    action: str = "read",
) -> None:
    """Append a metadata-only allow/deny record for an artifact read decision.

    Best-effort: any failure is swallowed (logged at debug) so auditing can never
    change the outcome of, or crash, a read decision.
    """
    try:
        meta = getattr(artifact, "metadata", None) or {}
        record = {
            "ts": time.time(),
            "action": str(action),
            "decision": "allow" if allowed else "deny",
            "reason": str(reason),
            "subject": None if subject is None else str(subject),
            "logical_name": getattr(artifact, "logical_name", None),
            "sensitivity": getattr(artifact, "sensitivity", None),
            "owner_user_id": meta.get("owner_user_id"),
            # A boolean flag is enough to spot cross-user attempts without
            # recording the allowed-reader roster or any payload.
            "cross_user": bool(meta.get("owner_user_id"))
            and str(meta.get("owner_user_id")) != str(subject),
        }
        # Defensive: guarantee no payload/uri ever leaks into the audit record.
        record.pop("payload", None)
        record.pop("uri", None)

        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - auditing must never break a read
        logger.debug("artifact-audit: could not record access: %s", exc)


def read_audit_records(path: Optional[Path] = None) -> list[dict]:
    """Read back the audit records (test/inspection helper)."""
    path = path or _audit_path()
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:  # pragma: no cover - skip a corrupt line
                continue
    return out

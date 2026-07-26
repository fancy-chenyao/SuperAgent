"""Artifact data-plane types (Plan §7).

Pure pydantic v2 models with no dependency on the workflow/manager/LLM stack, so
they can be imported and unit-tested in isolation. ``src/interface/__init__.py``
is intentionally empty, so importing this module only pulls in pydantic.

Concepts
--------
- :class:`Artifact`      immutable, versioned unit of data exchanged between steps.
- :class:`ArtifactRef`   a typed pointer to (a selection within) an Artifact.
- :class:`StepResult`    the outcome of executing a single TaskStep.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.contracts.workflow_failure import FailureDescriptor


def new_artifact_id() -> str:
    """Generate a fresh artifact id."""
    return uuid.uuid4().hex


def compute_checksum(value: Any) -> str:
    """Deterministic SHA-256 checksum over an arbitrary JSON-able value.

    Falls back to ``str(value)`` for anything that is not JSON serialisable so a
    checksum can always be produced (used by the Phase 2 artifact adapter).
    """
    try:
        canonical = json.dumps(value, ensure_ascii=False,
                               sort_keys=True, default=str)
    except Exception:  # pragma: no cover - defensive, str() is total
        canonical = str(value)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Sensitivity(str, Enum):
    """Coarse data-sensitivity classification carried by an Artifact."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ArtifactRef(BaseModel):
    """A pointer to an Artifact (optionally a selection within it).

    ``selector`` is an optional dotted/indexed path (e.g. ``data.name`` or
    ``rows.0.id``) resolved by :class:`~src.orchestration.resolver.ArtifactResolver`.
    """

    artifact_id: str
    version: Optional[int] = None  # None => latest version in the store
    selector: Optional[str] = None
    expected_schema_ref: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class Artifact(BaseModel):
    """An immutable, versioned unit of data produced by a step.

    The store assigns ``artifact_id``/``version`` on ``put()``; any change must
    produce a new version rather than mutating an existing one.
    """

    artifact_id: str = Field(default_factory=new_artifact_id)
    version: int = 1
    logical_name: str
    schema_ref: Optional[str] = None

    # Data is carried either inline (``payload``) or by reference (``uri``).
    payload: Optional[Any] = None
    uri: Optional[str] = None

    checksum: Optional[str] = None
    derived_from: List[ArtifactRef] = Field(default_factory=list)  # lineage
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    schema_valid: Optional[bool] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True, extra="allow")

    @model_validator(mode="after")
    def _require_payload_or_uri(self) -> "Artifact":
        if self.payload is None and self.uri is None:
            raise ValueError("Artifact must carry data via 'payload' or 'uri'")
        return self

    def ref(self, selector: Optional[str] = None) -> ArtifactRef:
        """Build an :class:`ArtifactRef` pointing at this artifact (or a part)."""
        return ArtifactRef(
            artifact_id=self.artifact_id,
            version=self.version,
            selector=selector,
            expected_schema_ref=self.schema_ref,
        )

    def with_checksum(self) -> "Artifact":
        """Return a copy with ``checksum`` filled from the inline payload."""
        if self.payload is None:
            return self
        updated = self.model_copy(deep=True)
        updated.checksum = compute_checksum(self.payload)
        return updated


class StepStatus(str, Enum):
    """Lifecycle status of a task step (shared by StepResult and the scheduler)."""

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class StepResult(BaseModel):
    """Outcome of executing a single :class:`~src.interface.task_graph.TaskStep`."""

    step_id: str
    status: StepStatus = StepStatus.PENDING
    outputs: Dict[str, ArtifactRef] = Field(default_factory=dict)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    failure: Optional[FailureDescriptor] = None

    model_config = ConfigDict(use_enum_values=True, extra="allow")

    @model_validator(mode="after")
    def _validate_failure_state(self) -> "StepResult":
        if self.status == StepStatus.SUCCEEDED and self.failure is not None:
            raise ValueError("a successful StepResult cannot carry a failure")
        # Keep the legacy text field populated during the compatibility window.
        if self.failure is not None and self.error is None:
            self.error = self.failure.message
        return self

    @property
    def is_success(self) -> bool:
        return self.status == StepStatus.SUCCEEDED

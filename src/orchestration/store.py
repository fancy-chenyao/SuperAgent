"""In-memory Artifact store (Plan §7, Phase 1).

Versioned and copy-on-read/write: every ``put`` produces a new version and
stored artifacts are never mutated in place. Depends only on the pure interface
types, so it stays unit-testable in isolation.
"""

from __future__ import annotations

from typing import Dict

from src.interface.artifact import Artifact, ArtifactRef, new_artifact_id


class ArtifactNotFoundError(KeyError):
    """Raised by :meth:`ArtifactStore.get` when a ref cannot be resolved."""


class ArtifactStore:
    """A simple versioned artifact store.

    Layout: ``{artifact_id: {version: Artifact}}`` plus a latest-version index.
    """

    def __init__(self) -> None:
        self._store: Dict[str, Dict[int, Artifact]] = {}
        self._latest: Dict[str, int] = {}

    def put(self, artifact: Artifact) -> ArtifactRef:
        """Store ``artifact`` as a new version and return a ref to it.

        - If the artifact has no ``artifact_id`` it is assigned a fresh one at
          version 1.
        - If its ``artifact_id`` already exists, a new (incremented) version is
          created; existing versions are left untouched.
        A deep copy is stored so later mutation of the caller's object cannot
        change what the store holds.
        """
        artifact_id = artifact.artifact_id or new_artifact_id()
        next_version = self._latest.get(artifact_id, 0) + 1

        stored = artifact.model_copy(deep=True)
        stored.artifact_id = artifact_id
        stored.version = next_version

        self._store.setdefault(artifact_id, {})[next_version] = stored
        self._latest[artifact_id] = next_version
        return stored.ref()

    def _resolve_version(self, ref: ArtifactRef) -> int:
        if ref.version is not None:
            return ref.version
        latest = self._latest.get(ref.artifact_id)
        if latest is None:
            raise ArtifactNotFoundError(f"unknown artifact_id: {ref.artifact_id!r}")
        return latest

    def get(self, ref: ArtifactRef) -> Artifact:
        """Return a copy of the referenced artifact (latest if no version)."""
        versions = self._store.get(ref.artifact_id)
        if not versions:
            raise ArtifactNotFoundError(f"unknown artifact_id: {ref.artifact_id!r}")
        version = self._resolve_version(ref)
        artifact = versions.get(version)
        if artifact is None:
            raise ArtifactNotFoundError(
                f"artifact {ref.artifact_id!r} has no version {version}"
            )
        return artifact.model_copy(deep=True)

    def exists(self, ref: ArtifactRef) -> bool:
        """True if the referenced artifact (and version, if given) exists."""
        versions = self._store.get(ref.artifact_id)
        if not versions:
            return False
        if ref.version is None:
            return True
        return ref.version in versions

    def latest_version(self, artifact_id: str) -> int | None:
        """Return the latest stored version for ``artifact_id`` (or ``None``)."""
        return self._latest.get(artifact_id)

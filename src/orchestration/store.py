"""In-memory Artifact store (Plan §7, Phase 1).

Versioned and copy-on-read/write: every ``put`` produces a new version and
stored artifacts are never mutated in place. Depends only on the pure interface
types, so it stays unit-testable in isolation.
"""

from __future__ import annotations

from typing import Any, Dict

from src.interface.artifact import Artifact, ArtifactRef, compute_checksum, new_artifact_id


class ArtifactNotFoundError(KeyError):
    """Raised by :meth:`ArtifactStore.get` when a ref cannot be resolved."""


class ArtifactStoreCorruption(ValueError):
    """Raised by :meth:`ArtifactStore.load_state` for a corrupt/inconsistent record.

    Restoring from a checkpoint must fail closed on any integrity problem
    (bad artifact_id/version, checksum mismatch, or a duplicate) instead of
    silently skipping the record and letting a downstream step read partial or
    wrong upstream data.
    """


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
            raise ArtifactNotFoundError(
                f"unknown artifact_id: {ref.artifact_id!r}")
        return latest

    def get(self, ref: ArtifactRef) -> Artifact:
        """Return a copy of the referenced artifact (latest if no version)."""
        versions = self._store.get(ref.artifact_id)
        if not versions:
            raise ArtifactNotFoundError(
                f"unknown artifact_id: {ref.artifact_id!r}")
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

    def load(self, artifact: Artifact) -> ArtifactRef:
        """Insert ``artifact`` preserving its own ``artifact_id``/``version``.

        Unlike :meth:`put` (which always mints a new version), this restores a
        previously produced artifact -- used to rebuild the store from a
        checkpoint so resumed downstream steps can read upstream outputs.
        """
        stored = artifact.model_copy(deep=True)
        artifact_id = stored.artifact_id or new_artifact_id()
        version = stored.version or 1
        stored.artifact_id = artifact_id
        stored.version = version
        self._store.setdefault(artifact_id, {})[version] = stored
        self._latest[artifact_id] = max(
            self._latest.get(artifact_id, 0), version)
        return stored.ref()

    def dump_state(self) -> Dict[str, Any]:
        """Serialize the whole store to a JSON-able dict (for checkpoints)."""
        return {
            aid: {str(ver): art.model_dump() for ver, art in versions.items()}
            for aid, versions in self._store.items()
        }

    def load_state(self, data: Dict[str, Any]) -> None:
        """Rebuild the store from :meth:`dump_state` output, validating integrity.

        Fails closed (:class:`ArtifactStoreCorruption`) on any inconsistency --
        malformed record, ``artifact_id``/``version`` mismatch, checksum
        mismatch, or a duplicate ``(artifact_id, version)`` -- rather than
        silently skipping it. A skipped record would let a resumed downstream
        step run against missing/partial upstream data.
        """
        if not isinstance(data, dict):
            raise ArtifactStoreCorruption("artifact state must be a dict")
        seen: set[tuple[str, int]] = set()
        for aid, versions in data.items():
            if not isinstance(versions, dict):
                raise ArtifactStoreCorruption(
                    f"versions for artifact {aid!r} must be a dict"
                )
            for ver_str, payload in versions.items():
                try:
                    artifact = Artifact(**payload)
                except Exception as exc:  # noqa: BLE001 - malformed record
                    raise ArtifactStoreCorruption(
                        f"invalid artifact {aid!r} v{ver_str}: {exc}"
                    ) from exc
                if artifact.artifact_id != aid:
                    raise ArtifactStoreCorruption(
                        f"artifact_id mismatch: key={aid!r} payload={artifact.artifact_id!r}"
                    )
                try:
                    ver_int = int(ver_str)
                except (TypeError, ValueError) as exc:
                    raise ArtifactStoreCorruption(
                        f"invalid version key {ver_str!r} for artifact {aid!r}"
                    ) from exc
                if artifact.version != ver_int:
                    raise ArtifactStoreCorruption(
                        f"version mismatch for {aid!r}: key={ver_int} payload={artifact.version}"
                    )
                key = (aid, ver_int)
                if key in seen:
                    raise ArtifactStoreCorruption(
                        f"duplicate artifact record {key}")
                seen.add(key)
                if artifact.payload is not None and artifact.checksum:
                    expected = compute_checksum(artifact.payload)
                    if artifact.checksum != expected:
                        raise ArtifactStoreCorruption(
                            f"checksum mismatch for {aid!r} v{ver_int}"
                        )
                self.load(artifact)

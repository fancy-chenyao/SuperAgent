"""Dedicated Artifact payload persistence layer (Plan §9, C4).

The generic workflow checkpoint must NOT carry full (possibly sensitive)
artifact payloads. Instead, payloads are written here -- a protected, per-task
store on disk -- and the checkpoint keeps only a de-sensitized index of
:class:`~src.interface.artifact.ArtifactRef` + integrity metadata.

Guarantees:

- **Integrity (accidental-corruption detection)**: each record stores a SHA-256
  checksum; :meth:`load_index` recomputes it and fails closed on mismatch. This
  detects accidental corruption, truncation and casual edits -- it is NOT a
  cryptographic anti-tamper control (an attacker who can rewrite the payload can
  also recompute the checksum). Authenticated encryption is a deferred item.
- **Atomic writes**: every file is written to a temp file and ``os.replace``-d
  into place so a crash never leaves a half-written payload.
- **Best-effort directory permissions**: the per-task directory is created
  ``0700`` on POSIX. On Windows ``chmod`` is a near no-op -- this is NOT a
  Windows ACL implementation; a real ACL/DACL is a deferred production item.
- **Lifecycle / cleanup**: :meth:`clear` removes a task's payloads;
  :meth:`cleanup_expired` prunes stores older than a TTL.

Productionization enhancement (DEFERRED for this prototype)
----------------------------------------------------------
Encryption at rest (AES-GCM) is intentionally NOT implemented here yet. This is a
prototype; payloads live under a gitignored ``store/`` dir (or a dedicated dir via
``ARTIFACT_PAYLOAD_STORE_DIR``) and access is governed by the artifact read guard
+ audit log. Before production, add authenticated encryption at rest:

- AES-GCM (via ``cryptography``) with a key from the environment / a key service
  (never committed), and ``task_id | artifact_id | version | owner_user_id`` as
  the additional authenticated data (AAD) so a record cannot be replayed under a
  different identity;
- OR explicit Windows ACLs on the data directory as an alternative.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.interface.artifact import compute_checksum

logger = logging.getLogger(__name__)

_DEFAULT_DIR = "store/artifacts"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(name or "task"))


class ArtifactPayloadCorruption(ValueError):
    """Raised by :meth:`ArtifactPayloadStore.load_index` on an integrity failure."""


class ArtifactPayloadStore:
    """File-backed store for artifact payloads, namespaced per ``task_id``."""

    def __init__(self, task_id: str, *, base_dir: Optional[Path] = None) -> None:
        root = base_dir or Path(
            os.getenv("ARTIFACT_PAYLOAD_STORE_DIR", _DEFAULT_DIR))
        self._root = Path(root)
        self._dir = self._root / _safe(task_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Best-effort restrictive permissions (POSIX only). On Windows this is a
        # near no-op and is NOT a Windows ACL -- do not treat it as one.
        try:
            os.chmod(self._dir, 0o700)
        except OSError:  # pragma: no cover - platform without chmod semantics
            pass

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #
    def _path(self, artifact_id: str, version: int) -> Path:
        return self._dir / f"{_safe(artifact_id)}_v{int(version)}.json"

    def _atomic_write(self, path: Path, data: Dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:  # pragma: no cover - best effort
                    pass

    def save_store_state(self, store_state: Dict[str, Any]) -> Dict[str, Any]:
        """Persist all payloads from an :meth:`ArtifactStore.dump_state` mapping.

        Returns a de-sensitized index ``{artifact_id: {version: {checksum,
        logical_name, sensitivity}}}`` suitable for a checkpoint (no payload).
        """
        index: Dict[str, Any] = {}
        for aid, versions in (store_state or {}).items():
            if not isinstance(versions, dict):
                continue
            for ver_str, payload in versions.items():
                if not isinstance(payload, dict):
                    continue
                version = int(payload.get("version", ver_str) or 1)
                checksum = payload.get("checksum")
                if not checksum and payload.get("payload") is not None:
                    checksum = compute_checksum(payload.get("payload"))
                self._atomic_write(self._path(aid, version), payload)
                index.setdefault(aid, {})[str(version)] = {
                    "version": version,
                    "checksum": checksum,
                    "logical_name": payload.get("logical_name"),
                    "sensitivity": payload.get("sensitivity"),
                }
        return index

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def load_index(self, index: Dict[str, Any]) -> Dict[str, Any]:
        """Load the payloads referenced by ``index`` into ``dump_state`` format.

        Fails closed (:class:`ArtifactPayloadCorruption`) when a referenced
        payload file is missing, unreadable, or its checksum does not match the
        index / recomputed value -- so a resumed step never reads tampered or
        partial upstream data.
        """
        out: Dict[str, Any] = {}
        for aid, versions in (index or {}).items():
            if not isinstance(versions, dict):
                raise ArtifactPayloadCorruption(f"bad index entry for {aid!r}")
            for ver_str, meta in versions.items():
                version = int((meta or {}).get(
                    "version", ver_str) or 0) or int(ver_str)
                path = self._path(aid, version)
                if not path.exists():
                    raise ArtifactPayloadCorruption(
                        f"missing payload for {aid!r} v{version}"
                    )
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        payload = json.load(fh)
                except Exception as exc:  # noqa: BLE001 - unreadable -> corrupt
                    raise ArtifactPayloadCorruption(
                        f"unreadable payload for {aid!r} v{version}: {exc}"
                    ) from exc
                # Cross-check the stored checksum against the payload + index.
                actual = payload.get("checksum")
                if payload.get("payload") is not None:
                    recomputed = compute_checksum(payload.get("payload"))
                    if actual and actual != recomputed:
                        raise ArtifactPayloadCorruption(
                            f"checksum mismatch for {aid!r} v{version}"
                        )
                    actual = actual or recomputed
                expected = (meta or {}).get("checksum")
                if expected and actual and expected != actual:
                    raise ArtifactPayloadCorruption(
                        f"index/payload checksum mismatch for {aid!r} v{version}"
                    )
                out.setdefault(aid, {})[str(version)] = payload
        return out

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """Remove this task's payload directory (idempotent)."""
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except OSError:  # pragma: no cover - best effort
            pass

    def cleanup_expired(self, *, ttl_seconds: float) -> int:
        """Delete sibling task stores older than ``ttl_seconds``. Returns count."""
        removed = 0
        cutoff = time.time() - ttl_seconds
        if not self._root.exists():
            return 0
        for child in self._root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:  # pragma: no cover - best effort
                continue
        return removed

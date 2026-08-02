"""Least-privilege bearer capabilities for browser-owned runtime cleanup."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from src.utils.file_lock import FileLock
from src.utils.path_utils import get_project_root


class CleanupCapabilityError(PermissionError):
    """The supplied capability is missing, malformed, or not the owner."""


class CleanupCapabilityStore:
    """Persist only hashes of opaque, browser-generated cleanup credentials."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or _configured_store_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._file_lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")

    @staticmethod
    def _token_hash(token: str) -> str:
        value = str(token or "")
        if len(value) < 32:
            raise CleanupCapabilityError("task owner capability is invalid")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def bind(
        self,
        *,
        token: str,
        user_id: str,
        workflow_id: str,
        task_id: str = "",
    ) -> None:
        """Bind a capability to a workflow and optionally one concrete task."""

        token_hash = self._token_hash(token)
        workflow = str(workflow_id or "").strip()
        task = str(task_id or "").strip()
        if not workflow:
            raise ValueError("workflow_id is required")
        with self._lock, FileLock(self._file_lock_path):
            data = self._read()
            self._bind_key(data["workflows"], workflow, token_hash, user_id)
            if task:
                self._bind_key(data["tasks"], task, token_hash, user_id, workflow)
            self._write(data)

    def authorize_task(self, task_id: str, token: str) -> bool:
        return self._authorize("tasks", task_id, token)

    def authorize_workflow(self, workflow_id: str, token: str) -> bool:
        return self._authorize("workflows", workflow_id, token)

    def delete_task_binding(self, task_id: str) -> None:
        self._delete_binding("tasks", task_id)

    def delete_workflow_binding(self, workflow_id: str) -> None:
        self._delete_binding("workflows", workflow_id)

    @staticmethod
    def _bind_key(
        records: dict[str, Any],
        key: str,
        token_hash: str,
        user_id: str,
        workflow_id: str = "",
    ) -> None:
        existing = records.get(key)
        if isinstance(existing, dict) and not hmac.compare_digest(
            str(existing.get("token_hash") or ""), token_hash
        ):
            raise CleanupCapabilityError(
                "runtime records are already owned by another cleanup capability"
            )
        records[key] = {
            "token_hash": token_hash,
            "user_id": str(user_id or ""),
            **({"workflow_id": workflow_id} if workflow_id else {}),
        }

    def _authorize(self, namespace: str, key: str, token: str) -> bool:
        try:
            token_hash = self._token_hash(token)
        except CleanupCapabilityError:
            return False
        with self._lock, FileLock(self._file_lock_path):
            record = self._read()[namespace].get(str(key or "").strip())
        return bool(
            isinstance(record, dict)
            and hmac.compare_digest(
                str(record.get("token_hash") or ""), token_hash
            )
        )

    def _delete_binding(self, namespace: str, key: str) -> None:
        with self._lock, FileLock(self._file_lock_path):
            data = self._read()
            if data[namespace].pop(str(key or "").strip(), None) is not None:
                self._write(data)

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {"workflows": {}, "tasks": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CleanupCapabilityError(
                "cleanup capability store is unreadable"
            ) from exc
        if not isinstance(data, dict):
            raise CleanupCapabilityError("cleanup capability store is invalid")
        workflows = data.get("workflows")
        tasks = data.get("tasks")
        if not isinstance(workflows, dict) or not isinstance(tasks, dict):
            raise CleanupCapabilityError("cleanup capability store is invalid")
        return {"workflows": workflows, "tasks": tasks}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f"{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(temporary_path, self.path)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise


def _configured_store_path() -> Path:
    return Path(
        os.getenv(
            "CLEANUP_CAPABILITY_STORE_PATH",
            str(get_project_root() / "store" / "cleanup_capabilities.json"),
        )
    )


_store: Optional[CleanupCapabilityStore] = None


def get_cleanup_capability_store() -> CleanupCapabilityStore:
    global _store
    configured = _configured_store_path()
    if _store is None or _store.path != configured:
        _store = CleanupCapabilityStore(configured)
    return _store

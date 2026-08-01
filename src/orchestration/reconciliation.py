"""Persistent manual-reconciliation queue for uncertain side effects."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.utils.path_utils import get_project_root


@dataclass
class ReconciliationRequest:
    reconciliation_id: str
    status: str
    created_at: str
    updated_at: str
    user_id: str
    workflow_id: str
    task_id: str
    step_id: str
    resume_step: int
    agent_name: str
    error: str
    idempotency_key: str = ""
    claim_id: str = ""
    external_operation_id: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    resolution: dict[str, Any] = field(default_factory=dict)


class ReconciliationStore:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir or _configured_store_dir())
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        user_id: str,
        workflow_id: str,
        task_id: str,
        step_id: str,
        resume_step: int,
        agent_name: str,
        error: str,
        idempotency_key: str = "",
        claim_id: str = "",
        external_operation_id: str = "",
        receipt: Optional[dict[str, Any]] = None,
    ) -> ReconciliationRequest:
        existing = self.find_active(task_id=task_id, step_id=step_id)
        if existing is not None:
            return existing
        now = datetime.now().isoformat()
        identity = f"{task_id}_{step_id}_{int(datetime.now().timestamp() * 1000)}"
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in identity)
        request = ReconciliationRequest(
            reconciliation_id=f"recon_{safe}",
            status="pending",
            created_at=now,
            updated_at=now,
            user_id=user_id,
            workflow_id=workflow_id,
            task_id=task_id,
            step_id=step_id,
            resume_step=resume_step,
            agent_name=agent_name,
            error=error,
            idempotency_key=idempotency_key,
            claim_id=claim_id,
            external_operation_id=external_operation_id,
            receipt=dict(receipt or {}),
        )
        self._save(request)
        return request

    def list(
        self,
        *,
        status: Optional[str] = None,
        task_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self.base_dir.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if status and item.get("status") != status:
                continue
            if task_id and item.get("task_id") != task_id:
                continue
            if user_id and item.get("user_id") != user_id:
                continue
            items.append(item)
        return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, reconciliation_id: str) -> Optional[ReconciliationRequest]:
        path = self._path(reconciliation_id)
        if not path.exists():
            return None
        return ReconciliationRequest(
            **json.loads(path.read_text(encoding="utf-8"))
        )

    def find_active(
        self, *, task_id: str, step_id: str
    ) -> Optional[ReconciliationRequest]:
        # Only unresolved records suppress duplicates. A safe retry can produce
        # a genuinely new uncertain attempt, which must get a new queue item.
        for status in ("pending", "frozen"):
            for item in self.list(status=status, task_id=task_id):
                if item.get("step_id") == step_id:
                    return ReconciliationRequest(**item)
        return None

    def resolve(
        self,
        reconciliation_id: str,
        *,
        status: str,
        operator: str,
        comment: str = "",
        external_operation_id: str = "",
        outputs: Optional[dict[str, Any]] = None,
    ) -> ReconciliationRequest:
        request = self._require(reconciliation_id)
        if request.status not in {"pending", "frozen"}:
            raise ValueError(
                f"reconciliation is not resolvable in status={request.status}"
            )
        request.status = status
        request.updated_at = datetime.now().isoformat()
        if external_operation_id:
            request.external_operation_id = external_operation_id
        request.resolution = {
            "operator": operator,
            "comment": comment,
            "resolved_at": request.updated_at,
            "external_operation_id": external_operation_id,
            "outputs": dict(outputs or {}),
        }
        self._save(request)
        return request

    def delete(
        self,
        *,
        task_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> int:
        """Delete queue records in an explicitly scoped task/workflow boundary."""

        if not any((task_id, workflow_id, user_id)):
            raise ValueError("task_id, workflow_id or user_id is required")
        removed = 0
        for item in self.list():
            if task_id and item.get("task_id") != task_id:
                continue
            if workflow_id and item.get("workflow_id") != workflow_id:
                continue
            if user_id and item.get("user_id") != user_id:
                continue
            reconciliation_id = str(item.get("reconciliation_id") or "")
            if not reconciliation_id:
                continue
            path = self._path(reconciliation_id)
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
        return removed

    def freeze(
        self, reconciliation_id: str, *, operator: str, comment: str = ""
    ) -> ReconciliationRequest:
        request = self._require(reconciliation_id)
        if request.status not in {"pending", "frozen"}:
            raise ValueError(
                f"reconciliation is not freezable in status={request.status}"
            )
        request.status = "frozen"
        request.updated_at = datetime.now().isoformat()
        request.resolution = {
            "operator": operator,
            "comment": comment,
            "updated_at": request.updated_at,
        }
        self._save(request)
        return request

    def _require(self, reconciliation_id: str) -> ReconciliationRequest:
        request = self.get(reconciliation_id)
        if request is None:
            raise FileNotFoundError(
                f"reconciliation not found: {reconciliation_id}"
            )
        return request

    def _save(self, request: ReconciliationRequest) -> None:
        path = self._path(request.reconciliation_id)
        fd, temporary_path = tempfile.mkstemp(
            dir=str(self.base_dir),
            prefix=f"{path.stem}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    asdict(request),
                    handle,
                    indent=2,
                    ensure_ascii=False,
                )
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise

    def _path(self, reconciliation_id: str) -> Path:
        safe = "".join(
            c if c.isalnum() or c in "-_." else "_" for c in reconciliation_id
        )
        return self.base_dir / f"{safe}.json"


def _configured_store_dir() -> Path:
    return Path(
        os.getenv(
            "RECONCILIATION_STORE_DIR",
            str(get_project_root() / "store" / "reconciliations"),
        )
    )


_store: Optional[ReconciliationStore] = None


def get_reconciliation_store() -> ReconciliationStore:
    global _store
    configured = _configured_store_dir()
    if _store is None or _store.base_dir != configured:
        _store = ReconciliationStore(configured)
    return _store

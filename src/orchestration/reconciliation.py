"""Persistent manual-reconciliation queue for uncertain side effects."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.utils.path_utils import get_project_root
from src.utils.file_lock import FileLock
from src.orchestration.completion import ReceiptClaimMismatch, missing_receipt_outputs
from src.orchestration.schema_registry import get_schema_registry


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
    expected_outputs: list[str] = field(default_factory=list)
    expected_schema_refs: dict[str, str] = field(default_factory=dict)
    resolution: dict[str, Any] = field(default_factory=dict)


class ReconciliationStore:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir or _configured_store_dir())
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._file_lock_path = self.base_dir / ".reconciliation-store"
        self._transaction_path = self.base_dir / ".receipt-resolution.txn"
        self._recover_if_needed()

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
        expected_outputs: Optional[list[str]] = None,
        expected_schema_refs: Optional[dict[str, str]] = None,
    ) -> ReconciliationRequest:
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            existing = self.find_active(task_id=task_id, step_id=step_id)
            if existing is not None:
                return existing
            now = datetime.now().isoformat()
            identity = f"{task_id}_{step_id}_{int(datetime.now().timestamp() * 1000)}"
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in identity)
            request = ReconciliationRequest(
                reconciliation_id=f"recon_{safe}", status="pending",
                created_at=now, updated_at=now, user_id=user_id,
                workflow_id=workflow_id, task_id=task_id, step_id=step_id,
                resume_step=resume_step, agent_name=agent_name, error=error,
                idempotency_key=idempotency_key, claim_id=claim_id,
                external_operation_id=external_operation_id,
                receipt=dict(receipt or {}),
                expected_outputs=list(expected_outputs or []),
                expected_schema_refs=dict(expected_schema_refs or {}),
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
        self._recover_if_needed()
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
        self._recover_if_needed()
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
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            request = self._require(reconciliation_id)
            if request.status not in {"pending", "frozen"}:
                raise ValueError(
                    f"reconciliation is not resolvable in status={request.status}"
                )
            self._apply_resolution(
                request, status=status, operator=operator, comment=comment,
                external_operation_id=external_operation_id, outputs=outputs,
            )
            self._save(request)
            return request

    def resolve_with_receipt(
        self,
        reconciliation_id: str,
        *,
        receipt_store: Any,
        decision: str,
        operator: str,
        comment: str = "",
        external_operation_id: str = "",
        outputs: Optional[dict[str, Any]] = None,
    ) -> ReconciliationRequest:
        """Commit the reconciliation decision and receipt change as one transaction.

        A write-ahead rollback journal is persisted before either file changes.
        Ordinary exceptions roll both files back immediately; after a process
        crash the next store instance/read restores the pre-transaction state.
        """

        if decision not in {"retry", "succeeded"}:
            raise ValueError(f"unsupported receipt decision: {decision}")
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            receipt_store._path.parent.mkdir(parents=True, exist_ok=True)
            with receipt_store._lock, FileLock(receipt_store._path):
                request = self._require(reconciliation_id)
                if request.status not in {"pending", "frozen"}:
                    raise ValueError(
                        f"reconciliation is not resolvable in status={request.status}"
                    )
                key = request.idempotency_key
                if not key:
                    raise ValueError("missing idempotency key")
                receipt_data = receipt_store._read_strict()
                existing = receipt_data.get(key)
                if not isinstance(existing, dict):
                    raise KeyError(f"receipt not found: {key}")
                if existing.get("status") != "STARTED":
                    raise ValueError(
                        f"receipt is not resolvable in status={existing.get('status')}"
                    )
                owner = existing.get("claim_id")
                if request.claim_id and owner and owner != request.claim_id:
                    raise ReceiptClaimMismatch(
                        f"claim id mismatch for {key}: {owner!r} != {request.claim_id!r}"
                    )

                before_request = asdict(request)
                before_receipts = dict(receipt_data)
                if decision == "retry":
                    del receipt_data[key]
                    target_status = "retry_ready"
                else:
                    if not str(external_operation_id).strip():
                        raise ValueError("external_operation_id is required")
                    trusted_expected = existing.get("expected_outputs")
                    if trusted_expected is None:
                        trusted_expected = request.expected_outputs
                    elif request.expected_outputs and set(trusted_expected) != set(
                        request.expected_outputs
                    ):
                        raise ValueError(
                            "receipt output contract does not match reconciliation request"
                        )
                    missing = missing_receipt_outputs(
                        outputs, trusted_expected or []
                    )
                    if missing:
                        raise ValueError(
                            "confirmed outputs violate the trusted output contract; "
                            f"missing: {', '.join(missing)}"
                        )
                    trusted_schema_refs = existing.get("expected_schema_refs")
                    if trusted_schema_refs is None:
                        trusted_schema_refs = request.expected_schema_refs
                    elif request.expected_schema_refs and trusted_schema_refs != (
                        request.expected_schema_refs
                    ):
                        raise ValueError(
                            "receipt output schemas do not match reconciliation request"
                        )
                    registry = get_schema_registry()
                    for logical_name, schema_ref in dict(
                        trusted_schema_refs or {}
                    ).items():
                        if logical_name not in (outputs or {}):
                            continue
                        valid, errors = registry.validate(outputs[logical_name], schema_ref)
                        if not valid:
                            raise ValueError(
                                f"confirmed output {logical_name!r} failed schema "
                                f"{schema_ref!r}: {'; '.join(errors)}"
                            )
                    receipt_data[key] = {
                        **existing,
                        "status": "SUCCEEDED",
                        "external_op_id": str(external_operation_id).strip(),
                        "outputs": dict(outputs or {}),
                        "outputs_kind": "confirmed_payloads",
                        "confirmed_by": operator,
                        "timestamp": time.time(),
                    }
                    target_status = "confirmed_succeeded"
                self._apply_resolution(
                    request, status=target_status, operator=operator,
                    comment=comment, external_operation_id=external_operation_id,
                    outputs=outputs,
                )
                journal = {
                    "reconciliation_path": str(self._path(reconciliation_id).resolve()),
                    "reconciliation_before": before_request,
                    "receipt_path": str(receipt_store._path.resolve()),
                    "receipts_before": before_receipts,
                }
                self._write_json_atomic(self._transaction_path, journal)
                try:
                    receipt_store._flush_data(receipt_data)
                    receipt_store._receipts = receipt_data
                    self._save(request)
                except Exception:
                    self._recover_transaction_unlocked()
                    receipt_store._receipts = receipt_store._read_strict()
                    raise
                self._transaction_path.unlink(missing_ok=True)
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
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
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
                try:
                    self._path(reconciliation_id).unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
            return removed

    def freeze(
        self, reconciliation_id: str, *, operator: str, comment: str = ""
    ) -> ReconciliationRequest:
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            request = self._require(reconciliation_id)
            if request.status not in {"pending", "frozen"}:
                raise ValueError(
                    f"reconciliation is not freezable in status={request.status}"
                )
            request.status = "frozen"
            request.updated_at = datetime.now().isoformat()
            request.resolution = {
                "operator": operator, "comment": comment,
                "updated_at": request.updated_at,
            }
            self._save(request)
            return request

    @staticmethod
    def _apply_resolution(
        request: ReconciliationRequest, *, status: str, operator: str,
        comment: str, external_operation_id: str,
        outputs: Optional[dict[str, Any]],
    ) -> None:
        request.status = status
        request.updated_at = datetime.now().isoformat()
        if external_operation_id:
            request.external_operation_id = external_operation_id
        request.resolution = {
            "operator": operator, "comment": comment,
            "resolved_at": request.updated_at,
            "external_operation_id": external_operation_id,
            "outputs": dict(outputs or {}),
        }

    def _recover_if_needed(self) -> None:
        if not self._transaction_path.exists():
            return
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()

    def _recover_transaction_unlocked(self) -> None:
        if not self._transaction_path.exists():
            return
        journal = json.loads(self._transaction_path.read_text(encoding="utf-8"))
        reconciliation_path = Path(journal["reconciliation_path"])
        receipt_path = Path(journal["receipt_path"])
        if reconciliation_path.parent.resolve() != self.base_dir.resolve():
            raise ValueError("invalid reconciliation transaction path")
        self._write_json_atomic(
            reconciliation_path, journal["reconciliation_before"]
        )
        self._write_json_atomic(receipt_path, journal["receipts_before"])
        self._transaction_path.unlink(missing_ok=True)

    @staticmethod
    def _write_json_atomic(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise

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

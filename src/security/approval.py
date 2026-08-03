from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.path_utils import get_project_root
from src.utils.file_lock import FileLock


@dataclass
class ApprovalRequest:
    approval_id: str
    status: str
    created_at: str
    updated_at: str
    user_id: str
    workflow_id: str
    task_id: str
    resume_step: int
    node_name: str
    signature: str
    subject: Dict[str, Any]
    object: Dict[str, Any]
    scenario: Dict[str, Any]
    action: Dict[str, Any]
    policy_result: Dict[str, Any]
    step_id: str = ""
    decision: Dict[str, Any] = field(default_factory=dict)


def _stable_approval_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Return stable policy facts for an approval fingerprint.

    Scenario-fit prose may be model-assisted and vary on checkpoint resume.
    Bind the fit verdict and stable domains, never generated wording or its
    nondeterministic confidence score.
    """

    stable = json.loads(json.dumps(scenario, ensure_ascii=False, default=str))
    task_scenario = stable.get("task_scenario")
    if isinstance(task_scenario, dict):
        stage = str(task_scenario.get("stage") or "")
        if stage:
            task_scenario["stage"] = stage.rsplit(".", 1)[-1].upper()
        fit_result = task_scenario.get("scenario_fit_result")
        if isinstance(fit_result, dict):
            task_scenario["scenario_fit_result"] = {
                "fit": str(fit_result.get("fit") or "").lower(),
                "suggested_agent_domains": sorted(
                    str(item) for item in fit_result.get("suggested_agent_domains", [])
                ),
                "suggested_tool_domains": sorted(
                    str(item) for item in fit_result.get("suggested_tool_domains", [])
                ),
            }
    return stable


class ApprovalStore:
    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir or _configured_approval_store_dir())
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # The web process may resume the same task from concurrent requests.
        # Keep every read-decide-write transition atomic within the process so
        # one human approval can never be consumed twice.
        self._lock = threading.RLock()
        self._file_lock_path = self.base_dir / ".approval-store"

    @staticmethod
    def signature(
        subject: Dict[str, Any],
        object: Dict[str, Any],
        action: Dict[str, Any],
        scenario: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = {
            # Bind the complete policy inputs, not only ids. If a user's role,
            # clearance/grants, the resource sensitivity/constraints, the task
            # scenario or invocation arguments change after approval, the old
            # decision must not authorize the new context.
            "subject": subject or {},
            "object": object or {},
            "scenario": _stable_approval_scenario(scenario or {}),
            "action": action or {},
        }
        raw = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    def create(
        self,
        *,
        user_id: str,
        workflow_id: str,
        task_id: str,
        resume_step: int,
        node_name: str,
        subject: Dict[str, Any],
        object: Dict[str, Any],
        scenario: Dict[str, Any],
        action: Dict[str, Any],
        policy_result: Dict[str, Any],
        step_id: str = "",
    ) -> ApprovalRequest:
        signature = self.signature(subject, object, action, scenario)
        with self._lock, FileLock(self._file_lock_path):
            existing = self.find_active(task_id=task_id, signature=signature)
            if existing is not None:
                return existing

            now = datetime.now().isoformat()
            # Approval ids are global filenames, so neither a millisecond
            # timestamp nor a policy-signature prefix is a safe discriminator
            # across tasks.  Generation happens under the cross-process store
            # lock and retries even the vanishingly unlikely UUID collision.
            while True:
                approval_id = f"approval_{uuid.uuid4().hex}"
                if not self._path(approval_id).exists():
                    break
            request = ApprovalRequest(
                approval_id=approval_id,
                status="pending",
                created_at=now,
                updated_at=now,
                user_id=user_id,
                workflow_id=workflow_id,
                task_id=task_id,
                resume_step=resume_step,
                node_name=node_name,
                signature=signature,
                subject=subject,
                object=object,
                scenario=scenario,
                action=action,
                policy_result=policy_result,
                step_id=step_id,
            )
            self._save(request)
            return request

    def list(
        self,
        *,
        status: Optional[str] = None,
        workflow_id: Optional[str] = None,
        task_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        items = []
        for path in self.base_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if status and data.get("status") != status:
                continue
            if workflow_id and data.get("workflow_id") != workflow_id:
                continue
            if task_id and data.get("task_id") != task_id:
                continue
            if user_id and data.get("user_id") != user_id:
                continue
            items.append(data)
        return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, approval_id: str) -> Optional[ApprovalRequest]:
        path = self._path(approval_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return ApprovalRequest(**data)

    def approve(self, approval_id: str, approver: str = "user", comment: str = "") -> ApprovalRequest:
        with self._lock, FileLock(self._file_lock_path):
            request = self._require(approval_id)
            if request.status not in {"pending", "approved"}:
                raise ValueError(f"approval is not approvable in status={request.status}")
            request.status = "approved"
            request.updated_at = datetime.now().isoformat()
            request.decision = {"approver": approver, "comment": comment, "decided_at": request.updated_at}
            self._save(request)
            return request

    def reject(self, approval_id: str, approver: str = "user", comment: str = "") -> ApprovalRequest:
        with self._lock, FileLock(self._file_lock_path):
            request = self._require(approval_id)
            if request.status not in {"pending", "rejected"}:
                raise ValueError(f"approval is not rejectable in status={request.status}")
            request.status = "rejected"
            request.updated_at = datetime.now().isoformat()
            request.decision = {"approver": approver, "comment": comment, "decided_at": request.updated_at}
            self._save(request)
            return request

    def delete(
        self,
        *,
        task_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> int:
        """Delete approval records within an explicit task/workflow scope."""

        if not any((task_id, workflow_id, user_id)):
            raise ValueError("task_id, workflow_id or user_id is required")
        with self._lock, FileLock(self._file_lock_path):
            removed = 0
            for item in self.list():
                if task_id and item.get("task_id") != task_id:
                    continue
                if workflow_id and item.get("workflow_id") != workflow_id:
                    continue
                if user_id and item.get("user_id") != user_id:
                    continue
                approval_id = str(item.get("approval_id") or "")
                if not approval_id:
                    continue
                path = self._path(approval_id)
                try:
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
            return removed

    def consume_if_approved(self, *, task_id: str, signature: str) -> Optional[ApprovalRequest]:
        with self._lock, FileLock(self._file_lock_path):
            approved = [
                ApprovalRequest(**item)
                for item in self.list(status="approved", task_id=task_id)
                if item.get("signature") == signature
            ]
            if not approved:
                return None
            request = sorted(approved, key=lambda item: item.updated_at, reverse=True)[0]
            request.status = "consumed"
            request.updated_at = datetime.now().isoformat()
            self._save(request)
            return request

    def find_active(self, *, task_id: str, signature: str) -> Optional[ApprovalRequest]:
        for status in ("pending", "approved"):
            for item in self.list(status=status, task_id=task_id):
                if item.get("signature") == signature:
                    return ApprovalRequest(**item)
        return None

    def find_latest(
        self,
        *,
        task_id: str,
        signature: str,
        statuses: Optional[List[str]] = None,
    ) -> Optional[ApprovalRequest]:
        matches = [
            ApprovalRequest(**item)
            for item in self.list(task_id=task_id)
            if item.get("signature") == signature and (statuses is None or item.get("status") in statuses)
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def _require(self, approval_id: str) -> ApprovalRequest:
        request = self.get(approval_id)
        if request is None:
            raise FileNotFoundError(f"approval not found: {approval_id}")
        return request

    def _save(self, request: ApprovalRequest) -> None:
        path = self._path(request.approval_id)
        fd, temporary_path = tempfile.mkstemp(
            dir=str(self.base_dir), prefix=f"{path.stem}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(request), handle, indent=2, ensure_ascii=False)
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise

    def _path(self, approval_id: str) -> Path:
        safe = "".join(
            c if c.isalnum() or c in "-_." else "_" for c in str(approval_id)
        )
        return self.base_dir / f"{safe}.json"


_store: Optional[ApprovalStore] = None


def _configured_approval_store_dir() -> Path:
    return Path(
        os.getenv(
            "APPROVAL_STORE_DIR",
            str(get_project_root() / "store" / "approvals"),
        )
    )


def get_approval_store() -> ApprovalStore:
    global _store
    configured = _configured_approval_store_dir()
    if _store is None or _store.base_dir != configured:
        _store = ApprovalStore(configured)
    return _store

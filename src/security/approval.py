from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.path_utils import get_project_root


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
    decision: Dict[str, Any] = field(default_factory=dict)


class ApprovalStore:
    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir or (get_project_root() / "store" / "approvals"))
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def signature(subject: Dict[str, Any], object: Dict[str, Any], action: Dict[str, Any]) -> str:
        payload = {
            "subject_id": subject.get("id"),
            "object_id": object.get("id"),
            "object_type": object.get("object_type"),
            "action_verb": action.get("verb"),
            "action_type": (action.get("attributes") or {}).get("action_type"),
            "tool_id": (action.get("attributes") or {}).get("tool_id"),
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
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
    ) -> ApprovalRequest:
        signature = self.signature(subject, object, action)
        existing = self.find_active(task_id=task_id, signature=signature)
        if existing is not None:
            return existing

        now = datetime.now().isoformat()
        approval_id = f"approval_{int(datetime.now().timestamp() * 1000)}_{signature[:10]}"
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
        request = self._require(approval_id)
        if request.status not in {"pending", "approved"}:
            raise ValueError(f"approval is not approvable in status={request.status}")
        request.status = "approved"
        request.updated_at = datetime.now().isoformat()
        request.decision = {"approver": approver, "comment": comment, "decided_at": request.updated_at}
        self._save(request)
        return request

    def reject(self, approval_id: str, approver: str = "user", comment: str = "") -> ApprovalRequest:
        request = self._require(approval_id)
        if request.status not in {"pending", "rejected"}:
            raise ValueError(f"approval is not rejectable in status={request.status}")
        request.status = "rejected"
        request.updated_at = datetime.now().isoformat()
        request.decision = {"approver": approver, "comment": comment, "decided_at": request.updated_at}
        self._save(request)
        return request

    def consume_if_approved(self, *, task_id: str, signature: str) -> Optional[ApprovalRequest]:
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
        self._path(request.approval_id).write_text(
            json.dumps(asdict(request), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _path(self, approval_id: str) -> Path:
        return self.base_dir / f"{approval_id}.json"


_store: Optional[ApprovalStore] = None


def get_approval_store() -> ApprovalStore:
    global _store
    if _store is None:
        _store = ApprovalStore()
    return _store

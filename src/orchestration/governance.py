"""Persistent governance-event timeline for task execution.

Governance events are observability records. A write failure must never change
the business result of a workflow, so callers should use
``record_governance_event`` instead of writing through ``GovernanceEventStore``
directly unless they intentionally want persistence errors to propagate.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.utils.path_utils import get_project_root

logger = logging.getLogger(__name__)


class GovernanceEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"gov_{uuid.uuid4().hex}")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    event_type: str
    task_id: str
    workflow_id: str = ""
    step_id: Optional[str] = None
    subject: Optional[str] = None
    agent: Optional[str] = None
    tool: Optional[str] = None
    operation_mode: Optional[str] = None
    risk_level: Optional[str] = None
    decision: Optional[str] = None
    reason_code: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


class GovernanceEventStore:
    """Append-only JSONL store, partitioned by task id."""

    _write_lock = threading.Lock()

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir or _configured_store_dir())
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def append(self, event: GovernanceEvent) -> GovernanceEvent:
        path = self._path(event.task_id)
        line = json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        with self._write_lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.write("\n")
                handle.flush()
        return event

    def list(
        self,
        task_id: str,
        *,
        event_type: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        path = self._path(task_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed governance event in %s", path)
                continue
            if event_type and item.get("event_type") != event_type:
                continue
            if step_id and item.get("step_id") != step_id:
                continue
            events.append(item)
        return events

    def delete(self, task_id: str) -> bool:
        """Delete one task's observability timeline."""

        path = self._path(task_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def _path(self, task_id: str) -> Path:
        safe = "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_"
            for char in str(task_id)
        )
        if not safe:
            raise ValueError("task_id is required")
        return self.base_dir / f"{safe}.jsonl"


_store: Optional[GovernanceEventStore] = None


def _configured_store_dir() -> Path:
    return Path(
        os.getenv(
            "GOVERNANCE_EVENT_STORE_DIR",
            str(get_project_root() / "store" / "governance"),
        )
    )


def get_governance_event_store() -> GovernanceEventStore:
    global _store
    configured = _configured_store_dir()
    if _store is None or _store.base_dir != configured:
        _store = GovernanceEventStore(configured)
    return _store


def record_governance_event(
    event_type: str,
    *,
    task_id: str,
    workflow_id: str = "",
    step_id: Optional[str] = None,
    subject: Optional[str] = None,
    agent: Optional[str] = None,
    tool: Optional[str] = None,
    operation_mode: Optional[str] = None,
    risk_level: Optional[str] = None,
    decision: Optional[str] = None,
    reason_code: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> GovernanceEvent:
    event = GovernanceEvent(
        event_type=str(event_type).upper(),
        task_id=str(task_id),
        workflow_id=str(workflow_id or ""),
        step_id=step_id,
        subject=subject,
        agent=agent,
        tool=tool,
        operation_mode=operation_mode,
        risk_level=risk_level,
        decision=decision,
        reason_code=reason_code,
        details=dict(details or {}),
    )
    try:
        return get_governance_event_store().append(event)
    except Exception as exc:  # noqa: BLE001 - governance logging is best effort
        logger.warning("Could not persist governance event %s: %s", event_type, exc)
        return event

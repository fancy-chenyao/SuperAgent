"""Plan snapshot persistence (Plan §Phase 6, C5).

When the Planner validates a plan, the derived :class:`TaskGraph` is persisted
as a ``PlanSnapshot`` keyed by ``workflow_id``. A later production execution
request loads the snapshot and validates that the plan it is about to run is the
same plan that was approved (matching ``workflow_id`` / ``user_id`` /
``plan_hash`` / ``schema_version``) before entering the scheduler -- otherwise
execution is refused and a re-plan is required.

The snapshot is stored on disk (atomic write) under ``PLAN_SNAPSHOT_DIR`` (or
``store/plan_snapshots`` by default), separate from the generic checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Bumped whenever ``plan_to_task_graph`` changes its derivation so a snapshot
# built by an older converter is refused (re-plan) instead of executed against a
# graph the current converter would no longer produce.
CONVERTER_VERSION = 2
_DEFAULT_DIR = "store/plan_snapshots"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ":") else "_" for c in str(name or "wf"))


def _canonical(obj: Any) -> str:
    """Order-independent canonical JSON for hashing / deep comparison."""
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover - defensive
        return str(obj)


def _normalize_task_graph(task_graph: Any) -> Any:
    """Return a normalized, comparable representation of a TaskGraph.

    Accepts a ``TaskGraph`` model or an already-dumped dict; returns a plain
    dict so two graphs can be compared by their canonical JSON regardless of
    key order or model/dict form.
    """
    dump = getattr(task_graph, "model_dump", None)
    if callable(dump):
        return dump()
    return task_graph


def plan_hash(planning_steps: List[Dict[str, Any]]) -> str:
    """Stable hash over the planning steps (order-sensitive, key-canonical).

    Retained for backward compatibility; the authoritative integrity digest is
    :func:`snapshot_hash`, which also covers the derived task graph.
    """
    return hashlib.sha256(_canonical(planning_steps or []).encode("utf-8")).hexdigest()


def snapshot_hash(
    *,
    workflow_id: Optional[str],
    user_id: Optional[str],
    planning_steps: List[Dict[str, Any]],
    task_graph: Any,
) -> str:
    """Digest over the FULL normalized snapshot content (not just plan steps).

    Covers schema/converter versions, identity, planning steps AND the derived
    task graph so any inconsistent edit to the persisted file is detectable.
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "converter_version": CONVERTER_VERSION,
        "workflow_id": workflow_id,
        "user_id": user_id,
        "planning_steps": planning_steps or [],
        "task_graph": _normalize_task_graph(task_graph) or {},
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _path(workflow_id: str, base_dir: Optional[Path]) -> Path:
    root = base_dir or Path(os.getenv("PLAN_SNAPSHOT_DIR", _DEFAULT_DIR))
    return Path(root) / f"{_safe(workflow_id)}.json"


def save_plan_snapshot(
    *,
    workflow_id: str,
    user_id: Optional[str],
    planning_steps: List[Dict[str, Any]],
    task_graph: Dict[str, Any],
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist a validated plan snapshot atomically and return it."""
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "converter_version": CONVERTER_VERSION,
        "workflow_id": workflow_id,
        "user_id": user_id,
        "planning_steps": planning_steps,
        "task_graph": task_graph,
        # ``plan_hash`` kept for backward compatibility; ``snapshot_hash`` is the
        # authoritative integrity digest (covers the derived task graph too).
        "plan_hash": plan_hash(planning_steps),
        "snapshot_hash": snapshot_hash(
            workflow_id=workflow_id,
            user_id=user_id,
            planning_steps=planning_steps,
            task_graph=task_graph,
        ),
    }
    path = _path(workflow_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:  # pragma: no cover - best effort
                pass
    return snapshot


def load_plan_snapshot(
    workflow_id: str, *, base_dir: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """Load a plan snapshot, or ``None`` when absent/unreadable."""
    path = _path(workflow_id, base_dir)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 - unreadable -> treat as missing
        logger.warning("plan snapshot: could not load %s: %s", path, exc)
        return None


def validate_snapshot(
    snapshot: Optional[Dict[str, Any]],
    *,
    workflow_id: str,
    user_id: Optional[str],
    planning_steps: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Validate a snapshot is consistent with the current execution request.

    Returns ``(ok, reason)``. Fails closed on any mismatch so a stale or
    tampered plan is never executed.
    """
    if not isinstance(snapshot, dict):
        return False, "no_snapshot"
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        return False, f"schema_version mismatch: {snapshot.get('schema_version')}"
    if snapshot.get("workflow_id") != workflow_id:
        return False, "workflow_id mismatch"
    if snapshot.get("user_id") != user_id:
        return False, "user_id mismatch"
    if not snapshot.get("task_graph"):
        return False, "snapshot missing task_graph"
    if snapshot.get("plan_hash") != plan_hash(planning_steps):
        return False, "plan_hash mismatch (plan changed since planning)"
    return True, "ok"


def verify_snapshot_for_execution(
    snapshot: Optional[Dict[str, Any]],
    *,
    workflow_id: str,
    user_id: Optional[str],
    planning_steps: List[Dict[str, Any]],
    goal: str = "",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Authoritative production gate: return ``(task_graph, reason)`` or ``(None, reason)``.

    Fails closed. The PRIMARY safety guarantee is re-deriving the TaskGraph from
    the CURRENT planning steps with the same :func:`plan_to_task_graph` and
    deep-comparing it (normalized) against the snapshot's stored task graph:

    1. schema/converter versions must match (else a re-plan is required);
    2. workflow id / user id must match the request;
    3. ``snapshot_hash`` must match the snapshot's own content (detects file
       corruption / inconsistent hand edits);
    4. the full graph rebuilt from trusted request identity, goal and current
       planning steps must be byte-identical (after normalization) to the
       stored graph -- any drift in the spec, operation modes, preferred
       resources, dependencies or output bindings is rejected.

    On success the stored (approved) task graph dict is returned for injection.
    On ANY mismatch ``None`` is returned so the caller refuses execution and
    never falls back to a legacy path.
    """
    if not isinstance(snapshot, dict):
        return None, "no_snapshot"
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        return None, f"schema_version mismatch: {snapshot.get('schema_version')} (replan required)"
    if snapshot.get("converter_version") != CONVERTER_VERSION:
        return None, f"converter_version mismatch: {snapshot.get('converter_version')} (replan required)"
    if snapshot.get("workflow_id") != workflow_id:
        return None, "workflow_id mismatch"
    if snapshot.get("user_id") != user_id:
        return None, "user_id mismatch"
    snap_graph = snapshot.get("task_graph")
    if not snap_graph:
        return None, "snapshot missing task_graph"

    # (3) Content integrity: recompute the digest over the snapshot's OWN stored
    # content. A mismatch means the file was corrupted or edited inconsistently.
    expected = snapshot.get("snapshot_hash")
    recomputed = snapshot_hash(
        workflow_id=snapshot.get("workflow_id"),
        user_id=snapshot.get("user_id"),
        planning_steps=snapshot.get("planning_steps") or [],
        task_graph=snap_graph,
    )
    if not expected or expected != recomputed:
        return None, "snapshot_hash mismatch (corrupt or tampered snapshot)"

    # (4) Re-derive from the CURRENT planning steps and deep-compare. This is the
    # main guarantee: it catches a modified plan, a swapped-in task graph, or
    # any field drift even if the stored hash was recomputed by a tamperer.
    try:
        from src.orchestration.plan_to_task_graph import plan_to_task_graph

        rebuilt = plan_to_task_graph(
            planning_steps or [],
            task_id=workflow_id,
            subject=user_id,
            goal=goal or "",
        ).model_dump()
    except Exception as exc:  # noqa: BLE001 - cannot rebuild -> refuse
        return None, f"rebuild failed (replan required): {exc}"

    if _canonical(_normalize_task_graph(rebuilt)) != _canonical(
        _normalize_task_graph(snap_graph)
    ):
        return None, "task_graph mismatch vs current plan (replan required)"
    return snap_graph, "ok"

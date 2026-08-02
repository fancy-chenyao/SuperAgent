"""Closed-loop governance: completion conditions, idempotency, receipts (Plan §9, Phase 4).

Three concerns:

1. **Completion conditions** — a *restricted* mini-DSL evaluated via a whitelisted
   AST walk. ``eval``/``exec`` are NEVER used; only a small set of node types
   (comparisons, boolean ops, attribute/subscript paths, ``exists``/``len``) are
   interpreted against a context of ``{outputs, metrics, status}``.
2. **Idempotency** — a stable key ``sha256(task_id | step_id | normalized_input)``
   so a side-effect step is executed at most once across retries/resumes.
3. **Receipts** — a record that a side-effect completed; checked before re-running
   so e.g. an email is never sent twice.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from src.utils.file_lock import FileLock

logger = logging.getLogger(__name__)


class _Missing:
    """Sentinel for an absent path segment (distinct from an explicit None)."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<MISSING>"


MISSING = _Missing()

_ALLOWED_FUNCS = {"exists", "len"}
_COMPARE_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
}


# --------------------------------------------------------------------------- #
# Restricted completion-condition DSL (no eval/exec)
# --------------------------------------------------------------------------- #
def evaluate_condition(expression: str, context: Dict[str, Any]) -> bool:
    """Evaluate a whitelisted boolean ``expression`` against ``context``.

    Raises ``ValueError`` for any construct outside the whitelist.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid completion expression: {exc}") from exc
    return bool(_eval(tree.body, context))


def _coerce(value: Any) -> Any:
    return None if value is MISSING else value


def _eval(node: ast.AST, ctx: Dict[str, Any]) -> Any:
    if isinstance(node, ast.BoolOp):
        values = [_eval(v, ctx) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, ctx)

    if isinstance(node, ast.Compare):
        left = _coerce(_eval(node.left, ctx))
        for op, comparator in zip(node.ops, node.comparators):
            right = _coerce(_eval(comparator, ctx))
            func = _COMPARE_OPS.get(type(op))
            if func is None:
                raise ValueError(f"operator not allowed: {type(op).__name__}")
            try:
                outcome = func(left, right)
            except TypeError:
                return False
            if not outcome:
                return False
            left = right
        return True

    if isinstance(node, ast.Name):
        low = node.id.lower()
        if low in ("null", "none"):
            return None
        if low == "true":
            return True
        if low == "false":
            return False
        return ctx.get(node.id, MISSING)

    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise ValueError("dunder/private attribute access is not allowed")
        base = _eval(node.value, ctx)
        if base is MISSING or base is None:
            return MISSING
        if isinstance(base, dict):
            return base.get(node.attr, MISSING)
        return getattr(base, node.attr, MISSING)

    if isinstance(node, ast.Subscript):
        base = _eval(node.value, ctx)
        key = _eval(node.slice, ctx)
        if isinstance(base, dict):
            return base.get(key, MISSING)
        if isinstance(base, (list, tuple)) and isinstance(key, int):
            return base[key] if -len(base) <= key < len(base) else MISSING
        return MISSING

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise ValueError("only exists()/len() calls are allowed")
        args = [_eval(a, ctx) for a in node.args]
        if node.func.id == "exists":
            val = args[0] if args else MISSING
            return val is not MISSING and val is not None
        if node.func.id == "len":
            val = args[0] if args else None
            try:
                return len(val)
            except TypeError:
                return 0

    raise ValueError(
        f"expression construct not allowed: {type(node).__name__}")


def evaluate_completion(
    conditions: Optional[Iterable[Any]],
    outputs: Optional[Dict[str, Any]],
    metrics: Optional[Dict[str, Any]],
    status: str = "SUCCEEDED",
) -> Tuple[bool, Optional[str]]:
    """Evaluate all ``conditions``; return ``(all_passed, first_failing_expr)``."""
    ctx = {
        "outputs": dict(outputs or {}),
        "metrics": dict(metrics or {}),
        "status": str(status),
    }
    for cond in conditions or []:
        expr = getattr(cond, "expression", None)
        if expr is None and isinstance(cond, dict):
            expr = cond.get("expression")
        if not expr:
            continue
        try:
            if not evaluate_condition(expr, ctx):
                return False, expr
        except Exception as exc:  # noqa: BLE001 - a bad condition fails closed
            return False, f"{expr} (eval error: {exc})"
    return True, None


# --------------------------------------------------------------------------- #
# Idempotency + receipts
# --------------------------------------------------------------------------- #
def normalize_input(inputs: Any) -> str:
    """Canonical, order-independent string for hashing step inputs."""
    try:
        return json.dumps(inputs, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover - defensive
        return str(inputs)


def idempotency_key(task_id: str, step_id: str, inputs: Any) -> str:
    """Stable key: ``sha256(task_id | step_id | normalized_input)``."""
    return _key_from_normalized(task_id, step_id, normalize_input(inputs))


def _key_from_normalized(task_id: Any, step_id: Any, normalized_input: Any) -> str:
    """Derive the idempotency key from an ALREADY-normalized input string.

    Used both by :func:`idempotency_key` and by :func:`validate_receipt` to
    re-derive a receipt's key from its own stored identity fields (so a receipt
    whose fields do not match its key cannot trigger an idempotent skip).
    """
    payload = f"{task_id}|{step_id}|{normalized_input}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Fields a SUCCEEDED receipt must carry to be trusted for idempotent skip.
_REQUIRED_RECEIPT_FIELDS = (
    "task_id",
    "step_id",
    "normalized_input",
    "agent",
    "status",
    "timestamp",
    "external_op_id",
)


def validate_receipt(receipt: Any, *, key: Optional[str] = None) -> bool:
    """A usable receipt records a verifiable, completed side-effect.

    All identity + provenance fields must be present so a resumed run can trust
    it enough to SKIP re-executing a side effect: ``task_id``, ``step_id``,
    ``normalized_input``, ``agent``, ``status == SUCCEEDED``, ``timestamp`` and
    an ``external_op_id`` (the external system's operation id). A receipt
    missing any of these is NOT trusted (fail closed -> the step is treated as
    not-yet-confirmed rather than silently skipped).

    Additionally hardened so a malformed/mismatched receipt can NEVER trigger an
    idempotent skip:

    - ``external_op_id`` must be a NON-EMPTY string (not ``None``/empty/non-str);
    - the receipt's identity fields must re-derive its own recorded
      ``idempotency_key`` (``task_id``/``step_id``/``normalized_input``);
    - when a lookup ``key`` is supplied, the receipt must belong to it.
    """
    if not isinstance(receipt, dict):
        return False
    if receipt.get("status") != "SUCCEEDED":
        return False
    for field in _REQUIRED_RECEIPT_FIELDS:
        if receipt.get(field) is None:
            return False
    if not str(receipt.get("step_id")):
        return False
    # A verifiable external operation id must be a non-empty string.
    op = receipt.get("external_op_id")
    if not isinstance(op, str) or not op.strip():
        return False
    # Internal consistency: the recorded key must be derivable from the
    # receipt's own identity fields (guards against tampered normalized_input).
    recorded = receipt.get("idempotency_key")
    if recorded is not None:
        derived = _key_from_normalized(
            receipt.get("task_id"), receipt.get(
                "step_id"), receipt.get("normalized_input")
        )
        if str(recorded) != derived:
            return False
    # When looking up by key, the receipt must belong to exactly that key.
    if key is not None:
        if str(receipt.get("idempotency_key") or "") != str(key):
            return False
        derived = _key_from_normalized(
            receipt.get("task_id"), receipt.get(
                "step_id"), receipt.get("normalized_input")
        )
        if derived != str(key):
            return False
    return True


class ReceiptStoreCorruption(ValueError):
    """Raised when the on-disk receipt store cannot be parsed.

    The corrupt file is NEVER cleared/overwritten -- the caller must fail closed
    (refuse to execute the side effect) rather than risk a duplicate side effect
    against an unknown prior state.
    """


class ReceiptClaimMismatch(RuntimeError):
    """Raised when ``complete()`` is called with a ``claim_id`` that does not own
    the STARTED receipt (another instance holds the execution right)."""


class ClaimStatus(str, Enum):
    """Outcome of an atomic :meth:`ReceiptStore.claim_if_absent`."""

    CLAIMED = "CLAIMED"          # this instance won the right to execute
    SUCCEEDED = "SUCCEEDED"      # a trusted SUCCEEDED receipt already exists
    IN_PROGRESS = "IN_PROGRESS"  # another instance has already claimed/started
    CORRUPT = "CORRUPT"          # the store is unparseable -> fail closed


@dataclass
class ClaimResult:
    """Result of an atomic claim attempt.

    ``claim_id`` is only set on :attr:`ClaimStatus.CLAIMED` (the token the
    winning instance must present to :meth:`ReceiptStore.complete`). ``receipt``
    carries the existing on-disk receipt for SUCCEEDED (reuse) / IN_PROGRESS
    (external_op_id for reconciliation).
    """

    status: ClaimStatus
    claim_id: Optional[str] = None
    receipt: Optional[Dict[str, Any]] = None


class ReceiptStore:
    """In-memory registry of side-effect receipts keyed by idempotency key."""

    def __init__(self) -> None:
        self._receipts: Dict[str, Dict[str, Any]] = {}

    def put(self, key: str, receipt: Dict[str, Any]) -> None:
        self._receipts[key] = receipt

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return self._receipts.get(key)

    def has(self, key: str) -> bool:
        return key in self._receipts

    def claim_if_absent(self, key: str, started_receipt: Dict[str, Any]) -> ClaimResult:
        """Atomically claim the execution right for ``key`` (in-memory variant).

        Returns SUCCEEDED when a trusted receipt already exists, IN_PROGRESS
        when a prior (unconfirmed) intent exists, or CLAIMED after recording a
        STARTED receipt with a unique ``claim_id``.
        """
        existing = self._receipts.get(key)
        if isinstance(existing, dict):
            if validate_receipt(existing, key=key):
                return ClaimResult(ClaimStatus.SUCCEEDED, receipt=existing)
            return ClaimResult(ClaimStatus.IN_PROGRESS, receipt=existing)
        claim_id = str(started_receipt.get("claim_id") or uuid.uuid4().hex)
        receipt = dict(started_receipt)
        receipt["claim_id"] = claim_id
        self.put(key, receipt)
        return ClaimResult(ClaimStatus.CLAIMED, claim_id=claim_id, receipt=receipt)

    def complete(self, key: str, claim_id: Optional[str], succeeded_receipt: Dict[str, Any]) -> None:
        """Record a SUCCEEDED receipt; only the owning ``claim_id`` may complete.

        A completion for a key already marked SUCCEEDED is a no-op (idempotent).
        A ``claim_id`` that does not own the STARTED receipt raises
        :class:`ReceiptClaimMismatch`.
        """
        existing = self._receipts.get(key)
        if isinstance(existing, dict):
            if existing.get("status") == "SUCCEEDED":
                return
            owner = existing.get("claim_id")
            if owner is not None and claim_id is not None and owner != claim_id:
                raise ReceiptClaimMismatch(
                    f"claim id mismatch for {key}: {owner!r} != {claim_id!r}")
        receipt = dict(succeeded_receipt)
        receipt.setdefault("claim_id", claim_id)
        self.put(key, receipt)

    def release_for_retry(self, key: str, claim_id: Optional[str] = None) -> None:
        """Release an unconfirmed STARTED claim after a human confirms no effect."""
        existing = self._receipts.get(key)
        if not isinstance(existing, dict):
            raise KeyError(f"receipt not found: {key}")
        if existing.get("status") != "STARTED":
            raise ValueError(
                f"receipt is not releasable in status={existing.get('status')}"
            )
        owner = existing.get("claim_id")
        if claim_id and owner and owner != claim_id:
            raise ReceiptClaimMismatch(
                f"claim id mismatch for {key}: {owner!r} != {claim_id!r}"
            )
        del self._receipts[key]

    def confirm_succeeded(
        self,
        key: str,
        *,
        claim_id: Optional[str],
        external_operation_id: str,
        outputs: Optional[Dict[str, Any]] = None,
        operator: str = "",
    ) -> None:
        """Turn an uncertain STARTED receipt into a human-confirmed success."""
        if not str(external_operation_id or "").strip():
            raise ValueError("external_operation_id is required")
        existing = self._receipts.get(key)
        if not isinstance(existing, dict):
            raise KeyError(f"receipt not found: {key}")
        self.complete(
            key,
            claim_id,
            {
                **existing,
                "status": "SUCCEEDED",
                "external_op_id": str(external_operation_id),
                "outputs": dict(outputs or {}),
                "confirmed_by": operator,
                "timestamp": time.time(),
            },
        )


def _json_default(obj: Any) -> Any:
    """Serialize pydantic models (e.g. ArtifactRef) inside a receipt."""
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    return str(obj)


class PersistentReceiptStore(ReceiptStore):
    """File-backed receipt store that survives process restarts (Plan §9, C4).

    Receipts are namespaced per ``task_id`` under ``base_dir`` and flushed to
    disk on every ``put`` (atomic replace) under a cross-process file lock. This
    is what lets the scheduler refuse to re-send an email/notification after a
    crash+resume -- not just within a single Python process -- and prevents two
    instances from clobbering each other's receipts.

    ``put`` re-reads the on-disk state before writing (merge) so a concurrent
    writer's receipts are never lost, and a flush failure PROPAGATES to the
    caller (the scheduler must not mark a step SUCCEEDED if the receipt for its
    side effect could not be persisted).
    """

    def __init__(self, task_id: str, *, base_dir: Optional[Path] = None) -> None:
        super().__init__()
        safe_task = "".join(c if c.isalnum() or c in (
            "-", "_") else "_" for c in str(task_id or "task"))
        root = base_dir or Path(
            os.getenv("RECEIPT_STORE_DIR", "store/receipts"))
        self._path = Path(root) / f"{safe_task}.json"
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                with self._path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._receipts = data
        except Exception as exc:  # noqa: BLE001 - corrupt file -> start empty
            logger.warning(
                "receipt store: could not load %s: %s", self._path, exc)
            self._receipts = {}

    def _flush(self) -> None:
        """Atomically persist the in-memory receipts; raises on failure."""
        self._flush_data(self._receipts)

    def _read_strict(self) -> Dict[str, Any]:
        """Read the on-disk receipts, raising on any parse/format problem.

        Unlike :meth:`_load` (which degrades to empty), this NEVER masks
        corruption: the caller must fail closed rather than execute a side
        effect against an unknown prior state. The corrupt file is left intact.
        """
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (ValueError, OSError) as exc:
            raise ReceiptStoreCorruption(
                f"receipt store unreadable: {self._path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ReceiptStoreCorruption(
                f"receipt store is not a JSON object: {self._path}")
        return data

    def _flush_data(self, data: Dict[str, Any]) -> None:
        """Atomically persist ``data`` (temp file + ``os.replace``); raises on failure."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, default=_json_default)
            os.replace(tmp, self._path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:  # pragma: no cover - best effort
                    pass
            raise

    def claim_if_absent(self, key: str, started_receipt: Dict[str, Any]) -> ClaimResult:
        """Atomically claim the execution right for ``key`` under a file lock.

        The whole read-decide-write sequence happens inside one cross-process
        lock over a STRICT on-disk read (never the stale instance cache):

        - a trusted SUCCEEDED receipt -> :attr:`ClaimStatus.SUCCEEDED` (reuse);
        - any other existing receipt   -> :attr:`ClaimStatus.IN_PROGRESS`
          (another instance owns it -> reconcile, never re-execute);
        - absent                       -> write a STARTED receipt carrying a
          unique ``claim_id`` and return :attr:`ClaimStatus.CLAIMED`;
        - unparseable store            -> :attr:`ClaimStatus.CORRUPT`
          (fail closed; the corrupt file is left untouched).

        The parent directory is created BEFORE the lock so the very first write
        into a fresh store succeeds.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock:
                with FileLock(self._path):
                    data = self._read_strict()
                    existing = data.get(key)
                    if isinstance(existing, dict):
                        self._receipts = data
                        if validate_receipt(existing, key=key):
                            return ClaimResult(ClaimStatus.SUCCEEDED, receipt=existing)
                        return ClaimResult(ClaimStatus.IN_PROGRESS, receipt=existing)
                    claim_id = str(started_receipt.get(
                        "claim_id") or uuid.uuid4().hex)
                    receipt = dict(started_receipt)
                    receipt["claim_id"] = claim_id
                    data[key] = receipt
                    self._flush_data(data)
                    self._receipts = data
                    return ClaimResult(ClaimStatus.CLAIMED, claim_id=claim_id, receipt=receipt)
        except ReceiptStoreCorruption as exc:
            # Fail closed: never clear the file, never execute the side effect.
            logger.error("receipt store corrupt (fail closed): %s", exc)
            return ClaimResult(ClaimStatus.CORRUPT)

    def complete(self, key: str, claim_id: Optional[str], succeeded_receipt: Dict[str, Any]) -> None:
        """Persist a SUCCEEDED receipt under the file lock (owning claim only).

        Re-reads strictly, refuses a mismatched ``claim_id``, treats an
        already-SUCCEEDED key as a no-op, and writes atomically. Propagates
        :class:`ReceiptStoreCorruption` / IO errors so the scheduler can require
        reconciliation instead of silently marking success.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with FileLock(self._path):
                data = self._read_strict()
                existing = data.get(key)
                if isinstance(existing, dict):
                    if existing.get("status") == "SUCCEEDED":
                        self._receipts = data
                        return
                    owner = existing.get("claim_id")
                    if owner is not None and claim_id is not None and owner != claim_id:
                        raise ReceiptClaimMismatch(
                            f"claim id mismatch for {key}: {owner!r} != {claim_id!r}")
                receipt = dict(succeeded_receipt)
                receipt.setdefault("claim_id", claim_id)
                data[key] = receipt
                self._flush_data(data)
                self._receipts = data

    def put(self, key: str, receipt: Dict[str, Any]) -> None:
        with self._lock:
            with FileLock(self._path):
                # Merge the latest on-disk state so a concurrent writer's
                # receipts are not clobbered by this write.
                self._load()
                self._receipts[key] = receipt
                self._flush()

    def release_for_retry(self, key: str, claim_id: Optional[str] = None) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with FileLock(self._path):
                data = self._read_strict()
                existing = data.get(key)
                if not isinstance(existing, dict):
                    raise KeyError(f"receipt not found: {key}")
                if existing.get("status") != "STARTED":
                    raise ValueError(
                        f"receipt is not releasable in status={existing.get('status')}"
                    )
                owner = existing.get("claim_id")
                if claim_id and owner and owner != claim_id:
                    raise ReceiptClaimMismatch(
                        f"claim id mismatch for {key}: {owner!r} != {claim_id!r}"
                    )
                del data[key]
                self._flush_data(data)
                self._receipts = data

    def confirm_succeeded(
        self,
        key: str,
        *,
        claim_id: Optional[str],
        external_operation_id: str,
        outputs: Optional[Dict[str, Any]] = None,
        operator: str = "",
    ) -> None:
        if not str(external_operation_id or "").strip():
            raise ValueError("external_operation_id is required")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with FileLock(self._path):
                data = self._read_strict()
                existing = data.get(key)
                if not isinstance(existing, dict):
                    raise KeyError(f"receipt not found: {key}")
                owner = existing.get("claim_id")
                if claim_id and owner and owner != claim_id:
                    raise ReceiptClaimMismatch(
                        f"claim id mismatch for {key}: {owner!r} != {claim_id!r}"
                    )
                succeeded = {
                    **existing,
                    "status": "SUCCEEDED",
                    "external_op_id": str(external_operation_id),
                    "outputs": dict(outputs or {}),
                    "confirmed_by": operator,
                    "timestamp": time.time(),
                }
                data[key] = succeeded
                self._flush_data(data)
                self._receipts = data

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
from typing import Any, Dict, Iterable, Optional, Tuple


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

    raise ValueError(f"expression construct not allowed: {type(node).__name__}")


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
    payload = f"{task_id}|{step_id}|{normalize_input(inputs)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_receipt(receipt: Any) -> bool:
    """A usable receipt records a SUCCEEDED side-effect for a known step."""
    if not isinstance(receipt, dict):
        return False
    return receipt.get("status") == "SUCCEEDED" and bool(receipt.get("step_id"))


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

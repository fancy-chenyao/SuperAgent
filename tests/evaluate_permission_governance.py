"""Run the HTTP portion of the permission-governance evaluation set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx


DEFAULT_DATASET = Path(__file__).parent / "evaluations" / "permission_governance_eval.json"


def _value_at(payload: Any, dotted_path: str) -> Any:
    current = payload
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted_path)
        current = current[part]
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--case", action="append", dest="case_ids")
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    base_url = (args.base_url or dataset.get("base_url") or "").rstrip("/")
    selected = set(args.case_ids or [])
    cases = [
        case
        for case in dataset.get("cases", [])
        if case.get("automated") is True
        and (not selected or case.get("id") in selected)
    ]
    if not cases:
        print("No automated HTTP cases selected.")
        return 2

    failures = 0
    with httpx.Client(timeout=20.0) as client:
        for case in cases:
            request = case["request"]
            expected = case["expected"]
            try:
                response = client.request(
                    request.get("method", "GET"),
                    base_url + request["path"],
                    params=request.get("params"),
                    json=request.get("json"),
                )
                payload = response.json()
                errors: list[str] = []
                if response.status_code != expected.get("http_status", 200):
                    errors.append(
                        f"HTTP {response.status_code} != {expected.get('http_status', 200)}"
                    )
                for path, wanted in (expected.get("json") or {}).items():
                    try:
                        actual = _value_at(payload, path)
                    except KeyError:
                        errors.append(f"missing JSON field {path}")
                        continue
                    if actual != wanted:
                        errors.append(f"{path}={actual!r} != {wanted!r}")
                if errors:
                    failures += 1
                    print(f"[FAIL] {case['id']} {case['title']}: {'; '.join(errors)}")
                else:
                    print(f"[PASS] {case['id']} {case['title']}")
            except Exception as exc:  # noqa: BLE001 - report every evaluation failure
                failures += 1
                print(f"[ERROR] {case['id']} {case['title']}: {exc}")

    print(f"\nResult: {len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

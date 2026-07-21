from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tests" / "intent_eval_cases.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator.task_profiler import profile_task


def _load_suite() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {}, raw
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise ValueError("intent_eval_cases.json 必须是用例数组，或包含 cases 数组的对象")
    return {key: value for key, value in raw.items() if key != "cases"}, raw["cases"]


def _validate_cases(cases: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        missing = {"id", "query"} - set(case)
        if missing:
            raise ValueError(f"第 {index} 条用例缺少字段：{sorted(missing)}")
        case_id = str(case["id"])
        if case_id in seen:
            raise ValueError(f"评测用例 id 重复：{case_id}")
        seen.add(case_id)


def _set_score(expected: list[str], actual: list[str]) -> tuple[float, float, bool]:
    expected_set, actual_set = set(expected), set(actual)
    if not expected_set and not actual_set:
        return 1.0, 1.0, True
    precision = len(expected_set & actual_set) / len(actual_set) if actual_set else 0.0
    recall = len(expected_set & actual_set) / len(expected_set) if expected_set else 0.0
    return precision, recall, expected_set == actual_set


def _values_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, list) and isinstance(actual, list):
        return {str(item) for item in expected} == {str(item) for item in actual}
    return str(expected) == str(actual)


def _entity_scores(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[float, bool]:
    if not expected:
        return (1.0, not actual)
    matched = sum(
        1 for key, value in expected.items() if key in actual and _values_equal(value, actual[key])
    )
    return matched / len(expected), matched == len(expected) and len(actual) == len(expected)


def _dependency_edges(subtasks: list[dict[str, Any]]) -> set[tuple[str, str]]:
    id_to_intent = {
        str(item.get("id")): str(item.get("intent"))
        for item in subtasks if item.get("id") and item.get("intent")
    }
    return {
        (id_to_intent.get(str(dependency), f"UNKNOWN:{dependency}"), str(item.get("intent")))
        for item in subtasks
        for dependency in item.get("depends_on") or []
    }


def _segment_ok(expected: Any, actual: int) -> bool:
    if expected is None:
        return True
    if isinstance(expected, int):
        return actual == expected
    return int(expected[0]) <= actual <= int(expected[1])


def _confidence_ok(expected: Any, actual: float) -> bool:
    if expected is None:
        return True
    return float(expected[0]) <= actual <= float(expected[1])


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _actual_intents(profile, provenance: str | None = None, negated: bool = False) -> list[str]:
    return [
        str(node.get("name"))
        for node in profile.intent_nodes
        if bool(node.get("negated")) == negated
        and (provenance is None or node.get("provenance") == provenance)
    ]


def _intent_node_integrity(profile) -> bool:
    executable_nodes = [node for node in profile.intent_nodes if not node.get("negated")]
    if [node.get("name") for node in executable_nodes] != profile.sub_intents:
        return False
    for node in profile.intent_nodes:
        if not node.get("name") or not node.get("source") or not node.get("provenance"):
            return False
        if not isinstance(node.get("evidence"), list) or not node.get("evidence"):
            return False
        if node.get("provenance") == "explicit" and not str(node.get("text_span") or "").strip():
            return False
    return True


async def _evaluate_mode(mode: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    metrics: dict[str, list[float]] = defaultdict(list)
    categories: dict[str, list[float]] = defaultdict(list)
    tiers: dict[str, list[float]] = defaultdict(list)
    sources: Counter[str] = Counter()
    negated_total = 0
    negated_executed = 0
    degraded_total = 0
    degraded_success = 0

    for case in cases:
        try:
            profile = await profile_task(
                case["query"], task_id=str(case["id"]), recognition_mode=mode
            )
        except Exception as exc:
            rows.append({"case": case, "error": f"{type(exc).__name__}: {exc}", "passed": False})
            categories[str(case.get("category", "uncategorized"))].append(0.0)
            tiers[str(case.get("tier", "unspecified"))].append(0.0)
            continue

        for node in profile.intent_nodes:
            sources[str(node.get("source") or "unknown")] += 1
        checks: dict[str, bool] = {}
        expected_sub = list(case.get("expected_sub_intents", profile.sub_intents))
        sub_precision, sub_recall, sub_exact = _set_score(expected_sub, profile.sub_intents)
        metrics["sub_intent_precision"].append(sub_precision)
        metrics["sub_intent_recall"].append(sub_recall)
        checks["sub_intents"] = sub_exact

        if "expected_primary_intent" in case:
            checks["primary_intent"] = profile.intent == case["expected_primary_intent"]
            metrics["primary_intent_accuracy"].append(float(checks["primary_intent"]))
        if "expected_primary_goal_intent" in case:
            checks["primary_goal"] = profile.primary_goal_intent == case["expected_primary_goal_intent"]
            metrics["primary_goal_accuracy"].append(float(checks["primary_goal"]))

        entity_score, entity_exact = _entity_scores(case.get("expected_entities", {}), profile.entities)
        metrics["entity_field_accuracy"].append(entity_score)
        metrics["entity_exact_accuracy"].append(float(entity_exact))
        checks["entities"] = entity_score == 1.0

        if "expected_subtask_count" in case:
            checks["subtask_count"] = len(profile.subtasks) == int(case["expected_subtask_count"])
            metrics["subtask_count_accuracy"].append(float(checks["subtask_count"]))
        if "expected_subtask_actions" in case:
            actual = [[str(item.get("intent")), str(item.get("action"))] for item in profile.subtasks]
            checks["subtask_actions"] = actual == case["expected_subtask_actions"]
            metrics["subtask_action_accuracy"].append(float(checks["subtask_actions"]))
        if "expected_dependency_edges" in case:
            expected_edges = {tuple(item) for item in case["expected_dependency_edges"]}
            actual_edges = _dependency_edges(profile.subtasks)
            edge_p, edge_r, edge_exact = _set_score(
                ["->".join(item) for item in expected_edges],
                ["->".join(item) for item in actual_edges],
            )
            checks["dependencies"] = edge_exact
            metrics["dependency_precision"].append(edge_p)
            metrics["dependency_recall"].append(edge_r)
            metrics["dependency_exact_accuracy"].append(float(edge_exact))

        scalar_checks = {
            "missing_fields": ("expected_missing_fields", profile.missing_fields),
            "task_type": ("expected_task_type", profile.task_type),
            "action": ("expected_action", profile.action),
            "risk": ("expected_risk_level", profile.risk_level),
            "irreversible": ("expected_irreversible", profile.irreversible),
            "composite": ("expected_is_composite", profile.is_composite),
            "clarification": ("expected_needs_clarification", profile.needs_clarification),
        }
        for name, (expected_key, actual) in scalar_checks.items():
            if expected_key not in case:
                continue
            expected = case[expected_key]
            ok = _values_equal(expected, actual)
            checks[name] = ok
            metrics[f"{name}_accuracy"].append(float(ok))

        if "expected_explicit_intents" in case:
            _, _, ok = _set_score(case["expected_explicit_intents"], _actual_intents(profile, "explicit"))
            checks["explicit_intents"] = ok
            metrics["explicit_intent_accuracy"].append(float(ok))
        if "expected_inferred_intents" in case:
            _, _, ok = _set_score(case["expected_inferred_intents"], _actual_intents(profile, "inferred"))
            checks["inferred_intents"] = ok
            metrics["inferred_intent_accuracy"].append(float(ok))
        if "expected_explicit_intents" in case or "expected_inferred_intents" in case:
            provenance_ok = checks.get("explicit_intents", True) and checks.get("inferred_intents", True)
            checks["provenance"] = provenance_ok
            metrics["provenance_accuracy"].append(float(provenance_ok))

        expected_negated = list(case.get("expected_negated_intents", []))
        if "expected_negated_intents" in case:
            _, _, negated_ok = _set_score(expected_negated, _actual_intents(profile, negated=True))
            checks["negated_recognition"] = negated_ok
            metrics["negated_recognition_accuracy"].append(float(negated_ok))
            executable_names = {str(item.get("intent")) for item in profile.subtasks}
            negated_total += len(expected_negated)
            negated_executed += len(set(expected_negated) & executable_names)

        forbidden = set(case.get("expected_forbidden_executable_intents", []))
        if forbidden:
            actual_executable = {str(item.get("intent")) for item in profile.subtasks}
            ok = not (forbidden & actual_executable)
            checks["unknown_rejection"] = ok
            metrics["unknown_rejection_accuracy"].append(float(ok))

        expected_conditional = set(case.get("expected_conditional_intents", []))
        if "expected_conditional_intents" in case:
            actual_conditional = {
                str(item.get("intent")) for item in profile.subtasks
                if item.get("execution_policy") == "conditional" and item.get("condition")
            }
            ok = actual_conditional == expected_conditional
            checks["conditional"] = ok
            metrics["conditional_dependency_accuracy"].append(float(ok))

        if "expected_clarification_contains" in case:
            expected_text = str(case["expected_clarification_contains"])
            ok = any(expected_text in question for question in profile.clarification_questions)
            checks["clarification_question"] = ok
            metrics["clarification_question_accuracy"].append(float(ok))

        checks["segments"] = _segment_ok(case.get("expected_segment_count"), len(profile.segments))
        checks["confidence"] = _confidence_ok(case.get("expected_confidence_range"), profile.confidence)
        checks["intent_nodes"] = _intent_node_integrity(profile)
        metrics["segment_accuracy"].append(float(checks["segments"]))
        metrics["confidence_range_accuracy"].append(float(checks["confidence"]))
        metrics["intent_node_integrity"].append(float(checks["intent_nodes"]))

        passed = all(checks.values())
        metrics["overall_case_pass_rate"].append(float(passed))
        category = str(case.get("category", "uncategorized"))
        tier = str(case.get("tier", "unspecified"))
        categories[category].append(float(passed))
        tiers[tier].append(float(passed))
        if profile.recognition_degraded:
            degraded_total += 1
            degraded_success += int(checks.get("primary_intent", True) and sub_recall == 1.0)
        rows.append({
            "case": case,
            "profile": profile,
            "checks": checks,
            "passed": passed,
            "sub_precision": sub_precision,
            "sub_recall": sub_recall,
            "entity_score": entity_score,
        })

    summary = {name: _avg(values) for name, values in metrics.items()}
    summary["negated_action_misexecution_rate"] = (
        round(negated_executed / negated_total, 4) if negated_total else 0.0
    )
    summary["degradation_success_rate"] = (
        round(degraded_success / degraded_total, 4) if degraded_total else None
    )
    return {
        "mode": mode,
        "summary": summary,
        "rows": rows,
        "categories": {name: _avg(values) for name, values in categories.items()},
        "tiers": {name: _avg(values) for name, values in tiers.items()},
        "sources": dict(sources),
        "degraded_cases": degraded_total,
    }


def _render_result(result: dict[str, Any]) -> list[str]:
    summary = result["summary"]
    lines = [f"## {result['mode']} 模式", ""]
    lines.append(f"- Cases: {len(result['rows'])}")
    lines.append(f"- Degraded cases: {result['degraded_cases']}")
    for name in (
        "overall_case_pass_rate", "primary_intent_accuracy", "primary_goal_accuracy",
        "sub_intent_precision", "sub_intent_recall", "explicit_intent_accuracy",
        "inferred_intent_accuracy", "provenance_accuracy", "negated_recognition_accuracy",
        "negated_action_misexecution_rate", "unknown_rejection_accuracy",
        "clarification_accuracy", "clarification_question_accuracy",
        "conditional_dependency_accuracy", "entity_field_accuracy",
        "dependency_exact_accuracy", "degradation_success_rate",
    ):
        if name in summary and summary[name] is not None:
            lines.append(f"- {name}: {summary[name]}")
    lines.extend(["", "### 来源统计", ""])
    lines.append("| Source | Count |")
    lines.append("|---|---:|")
    for source in ("rule", "semantic", "rule+semantic", "unknown"):
        if source in result["sources"]:
            lines.append(f"| {source} | {result['sources'][source]} |")
    lines.extend(["", "### 分层结果", "", "| Tier | Pass rate |", "|---|---:|"])
    for name, value in sorted(result["tiers"].items()):
        lines.append(f"| {name} | {value:.4f} |")
    lines.extend(["", "### 分类结果", "", "| Category | Pass rate |", "|---|---:|"])
    for name, value in sorted(result["categories"].items()):
        lines.append(f"| {name} | {value:.4f} |")
    lines.extend([
        "", "### 用例明细", "",
        "| Case | Tier | Category | Status | Primary | Goal | Explicit | Inferred | Negation | Clarify | Source mode |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ])
    for row in result["rows"]:
        case = row["case"]
        if row.get("error"):
            lines.append(
                f"| {case['id']} | {case.get('tier', '-')} | {case.get('category', '-')} | ERROR | - | - | - | - | - | - | {row['error']} |"
            )
            continue
        checks = row["checks"]
        profile = row["profile"]
        mark = lambda key: "PASS" if checks.get(key, True) else "FAIL"
        lines.append(
            f"| {case['id']} | {case.get('tier', '-')} | {case.get('category', '-')} | "
            f"{'PASS' if row['passed'] else 'FAIL'} | {mark('primary_intent')} | {mark('primary_goal')} | "
            f"{mark('explicit_intents')} | {mark('inferred_intents')} | {mark('negated_recognition')} | "
            f"{mark('clarification')} | {profile.recognition_mode}{' (degraded)' if profile.recognition_degraded else ''} |"
        )
    failures = [row for row in result["rows"] if not row["passed"]]
    if failures:
        lines.extend(["", "### 失败摘要", ""])
        for row in failures:
            case = row["case"]
            if row.get("error"):
                lines.append(f"- `{case['id']}`：{row['error']}")
                continue
            failed = [name for name, ok in row["checks"].items() if not ok]
            profile = row["profile"]
            lines.append(
                f"- `{case['id']}`：{', '.join(failed)}；实际 intent={profile.intent}，"
                f"goal={profile.primary_goal_intent}，sub_intents={profile.sub_intents}"
            )
    return lines


async def _main(args: argparse.Namespace) -> str:
    meta, cases = _load_suite()
    _validate_cases(cases)
    if args.semantic_only:
        cases = [case for case in cases if case.get("tier") == "semantic"]
    modes = ["rule", "hybrid"] if args.mode == "compare" else [args.mode]
    results = [await _evaluate_mode(mode, cases) for mode in modes]
    lines = ["# Intent Profile Evaluation Report", ""]
    lines.append(f"- Suite version: {meta.get('version', '-')}")
    lines.append(f"- Selected cases: {len(cases)}")
    lines.append(f"- Evaluation: {args.mode}")
    if len(results) == 2:
        lines.extend([
            "", "## 规则与混合模式对比", "",
            "| Metric | rule | hybrid |",
            "|---|---:|---:|",
        ])
        names = sorted(set(results[0]["summary"]) | set(results[1]["summary"]))
        for name in names:
            left, right = results[0]["summary"].get(name), results[1]["summary"].get(name)
            lines.append(f"| {name} | {left if left is not None else 'N/A'} | {right if right is not None else 'N/A'} |")
    for result in results:
        lines.extend(["", *_render_result(result)])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评测规则/语义混合任务画像")
    parser.add_argument("--mode", choices=("rule", "hybrid", "semantic", "compare"), default="rule")
    parser.add_argument("--semantic-only", action="store_true", help="只运行 tier=semantic 的用例")
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    report = asyncio.run(_main(arguments))
    output = arguments.output or ROOT / "reports" / f"intent_eval_report_{arguments.mode}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(report)
    print(f"Report written to: {output}")

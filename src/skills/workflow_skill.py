"""Evidence-based distillation and reuse of declarative workflow skills.

The distiller stores parameterized procedure structure, never historical tool
arguments, outputs, messages, credentials, or checkpoint state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.memory.utils import contains_secret, lexical_terms


_PLACEHOLDER = "{{user_request}}"
_STOP_WORDS = {
    "please", "help", "create", "make", "the", "and", "with", "from",
    "request", "task", "current", "user", "this", "that", "for", "to",
}
_DATA_PATH_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_INSTANCE_IDENTIFIER_RE = re.compile(
    r"(?:@|\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|^[A-Z]{1,4}\d{2,}$)"
)
_RISK_LEVEL = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
_BUSINESS_OUTCOME_ACTIONS = {
    "approve",
    "create",
    "delete",
    "execute",
    "export",
    "send",
    "submit",
    "update",
    "write",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _verified_business_result(summary: dict[str, Any]) -> Optional[bool]:
    """Return a business result only when backed by structured step evidence."""

    result = summary.get("business_success")
    if result is False:
        return False
    if result is not True or not summary.get("evidence_schema_version"):
        return None
    steps = summary.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    side_effects = [
        step
        for step in steps
        if isinstance(step, dict)
        and str(step.get("operation_mode") or "read").lower()
        in _BUSINESS_OUTCOME_ACTIONS
    ]
    if not side_effects:
        return True
    if all(
        step.get("business_success") is True
        and str(step.get("verification_status") or "").lower() == "verified"
        for step in side_effects
    ):
        return True
    return None


def _normalize_token(value: Any, default: str = "") -> str:
    text = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return re.sub(r"_+", "_", text).strip("_") or default


def _normalize_values(value: Any) -> list[str]:
    if isinstance(value, str):
        source = value.split(",") if "," in value else [value]
    elif isinstance(value, Iterable) and not isinstance(value, (dict, bytes)):
        source = list(value)
    else:
        source = []
    return sorted({item for raw in source if (item := _normalize_token(raw))})


def _safe_terms(text: str) -> set[str]:
    normalized = text.replace("_", " ").replace("-", " ")
    return {
        term for term in lexical_terms(normalized)
        if term not in _STOP_WORDS and len(term) > 1
    }


def _profile_intent(profile: dict[str, Any]) -> str:
    direct = (
        profile.get("primary_goal_intent")
        or profile.get("intent")
        or profile.get("sub_intent")
    )
    if direct:
        return _normalize_token(direct, "general_assistance")
    tags = _normalize_values(profile.get("scenario_tags"))
    if tags and tags != ["general"]:
        return tags[0]
    return _normalize_token(profile.get("task_type"), "general_assistance")


def _profile_action(profile: dict[str, Any]) -> str:
    return _normalize_token(
        profile.get("action") or profile.get("operation_mode"),
        "read",
    )


def _profile_risk(profile: dict[str, Any]) -> str:
    risk = str(profile.get("risk_level") or profile.get("risk_profile") or "LOW").upper()
    return risk if risk in _RISK_LEVEL else "LOW"


def _replace_request(value: Any, request: str) -> Any:
    if isinstance(value, str):
        return value.replace(_PLACEHOLDER, request)
    if isinstance(value, list):
        return [_replace_request(item, request) for item in value]
    if isinstance(value, dict):
        return {key: _replace_request(item, request) for key, item in value.items()}
    return value


def _bind_skill_plan(
    card: "WorkflowSkillCard",
    request: str,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    bound = _replace_request(card.planning_steps, request)
    raw_entities = (
        profile.get("entities") if isinstance(profile.get("entities"), dict) else {}
    )
    entities = {_normalize_token(key): value for key, value in raw_entities.items()}
    slot_bindings = {
        slot.name: entities[slot.name]
        for slot in card.slots
        if slot.name in entities
    }
    for step in bound:
        if isinstance(step, dict) and slot_bindings:
            step["slot_bindings"] = dict(slot_bindings)
    return bound


class WorkflowSkillStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DISABLED = "disabled"


class WorkflowSkillSettings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    enabled: bool = True
    reuse_enabled: bool = True
    auto_distill_enabled: bool = True
    match_threshold: float = 0.62
    match_margin: float = 0.08
    promotion_success_threshold: int = 2
    failure_disable_threshold: int = 2
    minimum_structure_consistency: float = 1.0
    allow_legacy_reuse: bool = True
    store_path: Path = Path("store/skills/workflow_skills.sqlite3")

    @classmethod
    def from_env(cls) -> "WorkflowSkillSettings":
        def boolean(name: str, default: bool) -> bool:
            value = os.getenv(name)
            return default if value is None else value.strip().lower() in {
                "1", "true", "yes", "on",
            }

        def integer(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        def decimal(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=boolean("WORKFLOW_SKILL_ENABLED", True),
            reuse_enabled=boolean("WORKFLOW_SKILL_REUSE_ENABLED", True),
            auto_distill_enabled=boolean("WORKFLOW_SKILL_AUTO_DISTILL_ENABLED", True),
            match_threshold=decimal("WORKFLOW_SKILL_MATCH_THRESHOLD", 0.62),
            match_margin=decimal("WORKFLOW_SKILL_MATCH_MARGIN", 0.08),
            promotion_success_threshold=max(
                1, integer("WORKFLOW_SKILL_PROMOTION_THRESHOLD", 2)
            ),
            failure_disable_threshold=max(
                1, integer("WORKFLOW_SKILL_FAILURE_THRESHOLD", 2)
            ),
            minimum_structure_consistency=decimal(
                "WORKFLOW_SKILL_STRUCTURE_CONSISTENCY", 1.0
            ),
            allow_legacy_reuse=boolean("WORKFLOW_SKILL_ALLOW_LEGACY_REUSE", True),
            store_path=Path(
                os.getenv(
                    "WORKFLOW_SKILL_DB_PATH",
                    "store/skills/workflow_skills.sqlite3",
                )
            ),
        )


class WorkflowSkillProvenance(BaseModel):
    source_task_ids: list[str] = Field(default_factory=list)
    source_count: int = 1
    distilled_at: str = Field(default_factory=_now)


class WorkflowSkillSlot(BaseModel):
    name: str
    value_type: str = "string"
    required: bool = True
    source: str = "task_profile.entities"
    description: str = "Value bound from the current task"


class WorkflowSkillApplicability(BaseModel):
    intent: str = "general_assistance"
    action: str = "read"
    task_type: str = "GENERAL"
    expected_capabilities: list[str] = Field(default_factory=list)
    data_scopes: list[str] = Field(default_factory=lambda: ["general"])
    scenario_tags: list[str] = Field(default_factory=list)
    max_risk: str = "LOW"
    irreversible: bool = False


class WorkflowSkillGraphNode(BaseModel):
    node_id: str
    capability: str
    agent_binding: str
    inputs: list[dict[str, str]] = Field(default_factory=list)
    request_slots: list[str] = Field(default_factory=list)
    success_condition: str = "agent_execution_succeeded"
    operation_mode: str = "read"
    risk_level: str = "LOW"
    expected_outputs: list[str] = Field(default_factory=list)
    expected_schema_ref: Optional[str] = None
    verification_contract: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(
        default_factory=lambda: {"max_attempts": 1, "fallback": "normal_planning"}
    )


class WorkflowSkillGraphEdge(BaseModel):
    source: str
    target: str
    kind: str = "sequence"
    condition: str = ""
    data_mapping: dict[str, str] = Field(default_factory=dict)


class WorkflowSkillGraph(BaseModel):
    graph_type: str = "capability_dag"
    nodes: list[WorkflowSkillGraphNode] = Field(default_factory=list)
    edges: list[WorkflowSkillGraphEdge] = Field(default_factory=list)
    entry_nodes: list[str] = Field(default_factory=list)
    exit_nodes: list[str] = Field(default_factory=list)
    complete: bool = False


class WorkflowSkillQuality(BaseModel):
    support_count: int = 0
    structure_consistency: float = 0.0
    slot_coverage: float = 0.0
    execution_success_rate: float = 1.0
    business_success_rate: Optional[float] = None
    business_outcome_coverage: float = 0.0
    contract_stability: float = 1.0


class WorkflowSkillValidation(BaseModel):
    status: str = "pending"
    method: str = "multi_trace_consistency"
    operator_override: bool = False
    validated_at: Optional[str] = None


class WorkflowSkillEvidence(BaseModel):
    evidence_id: str
    user_id: str
    task_id: str
    bucket_signature: str
    control_flow_signature: str
    task_profile: dict[str, Any]
    slots: list[WorkflowSkillSlot] = Field(default_factory=list)
    graph: WorkflowSkillGraph
    planning_steps: list[dict[str, Any]] = Field(default_factory=list)
    contract_fingerprints: dict[str, str] = Field(default_factory=dict)
    outcome_summary: dict[str, Any] = Field(
        default_factory=lambda: {"technical_success": True}
    )
    created_at: str = Field(default_factory=_now)


class WorkflowSkillCard(BaseModel):
    model_config = ConfigDict(extra="allow")

    skill_id: str
    user_id: str
    name: str
    description: str
    schema_version: int = 1
    status: WorkflowSkillStatus = WorkflowSkillStatus.CANDIDATE
    version: int = 1
    family_signature: str = ""
    signature: str
    task_type: str = "GENERAL"
    intent_examples: list[str] = Field(default_factory=list)
    scenario_tags: list[str] = Field(default_factory=list)
    expected_capabilities: list[str] = Field(default_factory=list)
    risk_profile: str = "LOW"
    planning_steps: list[dict[str, Any]] = Field(default_factory=list)
    required_agents: list[str] = Field(default_factory=list)
    applicability: WorkflowSkillApplicability = Field(
        default_factory=WorkflowSkillApplicability
    )
    slots: list[WorkflowSkillSlot] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    graph: WorkflowSkillGraph = Field(default_factory=WorkflowSkillGraph)
    postconditions: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    contract_fingerprints: dict[str, str] = Field(default_factory=dict)
    quality: WorkflowSkillQuality = Field(default_factory=WorkflowSkillQuality)
    validation: WorkflowSkillValidation = Field(default_factory=WorkflowSkillValidation)
    confidence: float = 0.5
    evidence_count: int = 1
    success_count: int = 1
    failure_count: int = 0
    consecutive_failures: int = 0
    provenance: WorkflowSkillProvenance = Field(default_factory=WorkflowSkillProvenance)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    last_used_at: Optional[str] = None


class WorkflowSkillMatch(BaseModel):
    skill: WorkflowSkillCard
    score: float
    lexical_score: float
    reason: str
    bound_planning_steps: list[dict[str, Any]]
    applicability_checks: dict[str, bool] = Field(default_factory=dict)


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _slots_from_profile(profile: dict[str, Any]) -> list[WorkflowSkillSlot]:
    raw_entities = (
        profile.get("entities") if isinstance(profile.get("entities"), dict) else {}
    )
    entities = {_normalize_token(key): value for key, value in raw_entities.items()}
    missing = {
        _normalize_token(item) for item in profile.get("missing_fields", []) if item
    }
    names = sorted(set(_normalize_token(key) for key in entities) | missing)
    return [
        WorkflowSkillSlot(
            name=name,
            value_type=_value_type(entities.get(name)),
            required=True,
            source="task_profile.entities",
        )
        for name in names
        if name
    ]


def _parameterize_input_mapping(
    value: Any,
    prior_agents: set[str],
) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    parameter_name = str(value.get("parameter_name") or "").strip()
    source_step = str(value.get("source_step") or "").strip()
    source_output = str(value.get("source_output") or "").strip()
    if not parameter_name or not source_step or not source_output:
        return None
    if source_step not in prior_agents:
        raise ValueError(
            f"workflow skill input references unknown prior Agent: {source_step}"
        )
    for field_name, identifier in (
        ("parameter_name", parameter_name),
        ("source_output", source_output),
    ):
        if (
            not _DATA_PATH_RE.fullmatch(identifier)
            or _INSTANCE_IDENTIFIER_RE.search(identifier)
        ):
            raise ValueError(f"workflow skill input has invalid {field_name}")
    return {
        "parameter_name": parameter_name,
        "source_step": source_step,
        "source_output": source_output,
        "description": f"Map {source_step}.{source_output} to {parameter_name}",
    }


def _parameterize_steps(
    planning_steps: list[dict[str, Any]],
    *,
    default_action: str = "read",
    default_risk: str = "LOW",
) -> list[dict[str, Any]]:
    parameterized: list[dict[str, Any]] = []
    prior_agents: set[str] = set()
    total = len([step for step in planning_steps if isinstance(step, dict)])
    for index, step in enumerate(planning_steps, start=1):
        if not isinstance(step, dict):
            continue
        agent_name = str(step.get("agent_name") or "").strip()
        if not agent_name:
            continue
        inputs = []
        for mapping in step.get("inputs") or []:
            normalized = _parameterize_input_mapping(mapping, prior_agents)
            if normalized is not None:
                inputs.append(normalized)
        declared_mode = _normalize_token(step.get("operation_mode"))
        if not declared_mode:
            declared_mode = _normalize_token(default_action) if index == total else "read"
        operation_mode = (
            "send"
            if declared_mode == "send"
            else "write"
            if declared_mode in _BUSINESS_OUTCOME_ACTIONS or declared_mode == "generate"
            else "read"
        )
        expected_outputs = [
            str(item)
            for item in (step.get("expected_outputs") or step.get("produces") or [])
            if isinstance(item, str) and _DATA_PATH_RE.fullmatch(item)
        ]
        schema_ref = step.get("expected_schema_ref") or step.get("output_schema_ref")
        risk_level = str(step.get("risk_level") or default_risk).upper()
        trusted_verifier_required = (
            risk_level in {"HIGH", "CRITICAL"}
            or declared_mode in {"approve", "delete"}
        )
        parameterized.append(
            {
                "agent_name": agent_name,
                "title": f"Reusable step {index}: {agent_name}",
                "description": (
                    f"Use {agent_name} for the current task. Current request: {_PLACEHOLDER}"
                ),
                "note": (
                    "Use only the current request and explicitly mapped outputs "
                    "from prior steps."
                ),
                "inputs": inputs,
                "request_context": _PLACEHOLDER,
                "operation_mode": operation_mode,
                "risk_level": risk_level,
                "expected_outputs": expected_outputs,
                "expected_schema_ref": str(schema_ref) if schema_ref else None,
                "retry": max(0, int(step.get("retry") or 0)) if operation_mode == "read" else 0,
                "verification_contract": (
                    {
                        "required": True,
                        "method": (
                            "trusted_business_verifier"
                            if trusted_verifier_required
                            else "platform_receipt_and_business_identifier"
                        ),
                        "trusted_verifier_required": trusted_verifier_required,
                        "evidence_fields": [
                            "receipt_status",
                            "external_operation_id",
                        ],
                    }
                    if operation_mode in {"write", "send"}
                    else {"required": False, "method": "technical_result"}
                ),
            }
        )
        prior_agents.add(agent_name)
    return parameterized


def _capability_for_step(
    source_step: dict[str, Any],
    profile_capabilities: list[str],
    agent_capabilities: dict[str, list[str]],
    index: int,
    total: int,
) -> str:
    explicit = (
        source_step.get("capability")
        or source_step.get("expected_capability")
        or source_step.get("operation")
    )
    if explicit:
        return _normalize_token(explicit, "general")
    agent_name = str(source_step.get("agent_name") or "")
    declared = _normalize_values(agent_capabilities.get(agent_name, []))
    profile_matches = [
        capability for capability in profile_capabilities if capability in declared
    ]
    if profile_matches:
        return profile_matches[0]
    if declared and declared != ["general"]:
        return declared[0]
    if total == 1 and len(profile_capabilities) == 1:
        return profile_capabilities[0]
    if index == total - 1 and profile_capabilities:
        return profile_capabilities[0]
    return _normalize_token(source_step.get("agent_name"), "general")


def _compile_graph(
    source_steps: list[dict[str, Any]],
    parameterized_steps: list[dict[str, Any]],
    profile_capabilities: list[str],
    slots: list[WorkflowSkillSlot],
    agent_capabilities: dict[str, list[str]],
) -> WorkflowSkillGraph:
    nodes: list[WorkflowSkillGraphNode] = []
    edges: list[WorkflowSkillGraphEdge] = []
    agent_nodes: dict[str, str] = {}
    slot_names = [slot.name for slot in slots]
    total = len(parameterized_steps)
    valid_source = [step for step in source_steps if isinstance(step, dict) and step.get("agent_name")]
    for index, step in enumerate(parameterized_steps):
        node_id = f"step_{index + 1}"
        source = valid_source[index] if index < len(valid_source) else step
        node = WorkflowSkillGraphNode(
            node_id=node_id,
            capability=_capability_for_step(
                source,
                profile_capabilities,
                agent_capabilities,
                index,
                total,
            ),
            agent_binding=str(step["agent_name"]),
            inputs=list(step.get("inputs") or []),
            request_slots=slot_names,
            success_condition=(
                "business_outcome_verified"
                if step.get("operation_mode") in {"write", "send"}
                else "agent_execution_succeeded"
            ),
            operation_mode=str(step.get("operation_mode") or "read"),
            risk_level=str(step.get("risk_level") or "LOW"),
            expected_outputs=list(step.get("expected_outputs") or []),
            expected_schema_ref=step.get("expected_schema_ref"),
            verification_contract=dict(step.get("verification_contract") or {}),
            retry_policy={
                "max_attempts": int(step.get("retry") or 0) + 1,
                "fallback": (
                    "normal_planning"
                    if step.get("operation_mode") == "read"
                    else "reconciliation"
                ),
            },
        )
        nodes.append(node)
        agent_nodes[node.agent_binding] = node_id
        if index:
            edges.append(
                WorkflowSkillGraphEdge(
                    source=f"step_{index}",
                    target=node_id,
                    kind="sequence",
                )
            )
    for node in nodes:
        for mapping in node.inputs:
            source_node = agent_nodes.get(mapping.get("source_step", ""))
            if source_node:
                edges.append(
                    WorkflowSkillGraphEdge(
                        source=source_node,
                        target=node.node_id,
                        kind="data",
                        data_mapping={
                            "source_output": mapping["source_output"],
                            "parameter_name": mapping["parameter_name"],
                        },
                    )
                )
    complete = bool(nodes) and all(node.agent_binding and node.capability for node in nodes)
    return WorkflowSkillGraph(
        nodes=nodes,
        edges=edges,
        entry_nodes=[nodes[0].node_id] if nodes else [],
        exit_nodes=[nodes[-1].node_id] if nodes else [],
        complete=complete,
    )


def _applicability(profile: dict[str, Any]) -> WorkflowSkillApplicability:
    task_type = str(profile.get("task_type") or "GENERAL").upper()
    return WorkflowSkillApplicability(
        intent=_profile_intent(profile),
        action=_profile_action(profile),
        task_type=task_type,
        expected_capabilities=_normalize_values(profile.get("expected_capabilities")),
        data_scopes=_normalize_values(profile.get("data_scope")) or ["general"],
        scenario_tags=_normalize_values(profile.get("scenario_tags")),
        max_risk=_profile_risk(profile),
        irreversible=bool(profile.get("irreversible", False)),
    )


def _sanitized_profile(
    applicability: WorkflowSkillApplicability,
    slots: list[WorkflowSkillSlot],
) -> dict[str, Any]:
    return {
        **applicability.model_dump(mode="json"),
        "slot_names": [slot.name for slot in slots],
    }


def _family_signature(applicability: WorkflowSkillApplicability) -> str:
    return _hash(
        {
            "intent": applicability.intent,
            "action": applicability.action,
            "task_type": applicability.task_type,
            "expected_capabilities": applicability.expected_capabilities,
            "data_scopes": applicability.data_scopes,
        }
    )


def _control_flow_signature(
    graph: WorkflowSkillGraph,
    contracts: dict[str, str],
) -> str:
    return _hash(
        {
            "nodes": [
                {
                    "capability": node.capability,
                    "agent_binding": node.agent_binding,
                    "inputs": [
                        {
                            "parameter_name": item.get("parameter_name"),
                            "source_step": item.get("source_step"),
                            "source_output": item.get("source_output"),
                        }
                        for item in node.inputs
                    ],
                    "operation_mode": node.operation_mode,
                    "risk_level": node.risk_level,
                    "expected_outputs": node.expected_outputs,
                    "expected_schema_ref": node.expected_schema_ref,
                    "success_condition": node.success_condition,
                    "verification_contract": node.verification_contract,
                    "retry_policy": node.retry_policy,
                }
                for node in graph.nodes
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "kind": edge.kind,
                    "data_mapping": edge.data_mapping,
                }
                for edge in graph.edges
            ],
            "contracts": dict(sorted(contracts.items())),
        }
    )


def _intent_examples(
    applicability: WorkflowSkillApplicability,
    explicit_examples: Iterable[str] | None,
) -> list[str]:
    examples = [
        str(item).strip() for item in explicit_examples or [] if str(item).strip()
    ]
    examples.append(applicability.intent.replace("_", " "))
    examples.extend(tag.replace("_", " ") for tag in applicability.scenario_tags)
    return list(dict.fromkeys(examples))


class WorkflowSkillStore:
    """SQLite persistence for cards and their independently auditable evidence."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=30, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workflow_skills (
                    skill_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, signature)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_skills_user "
                "ON workflow_skills(user_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_skills_active_user "
                "ON workflow_skills(user_id, json_extract(payload, '$.status'), "
                "json_extract(payload, '$.task_type'))"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workflow_skill_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    bucket_signature TEXT NOT NULL,
                    control_flow_signature TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, task_id)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_evidence_bucket "
                "ON workflow_skill_evidence(user_id, bucket_signature, "
                "control_flow_signature)"
            )

    @staticmethod
    def _card_from_row(row: sqlite3.Row) -> WorkflowSkillCard:
        return WorkflowSkillCard.model_validate(json.loads(row["payload"]))

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> WorkflowSkillEvidence:
        return WorkflowSkillEvidence.model_validate(json.loads(row["payload"]))

    def get(self, user_id: str, skill_id: str) -> Optional[WorkflowSkillCard]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND skill_id = ?",
                (user_id, skill_id),
            ).fetchone()
            return self._card_from_row(row) if row else None

    def get_by_signature(
        self, user_id: str, signature: str
    ) -> Optional[WorkflowSkillCard]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND signature = ?",
                (user_id, signature),
            ).fetchone()
            return self._card_from_row(row) if row else None

    def list(
        self, user_id: str, include_shared: bool = True
    ) -> list[WorkflowSkillCard]:
        users = [user_id, "share"] if include_shared and user_id != "share" else [user_id]
        placeholders = ",".join("?" for _ in users)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM workflow_skills WHERE user_id IN ({placeholders}) "
                "ORDER BY updated_at DESC",
                users,
            ).fetchall()
            return [self._card_from_row(row) for row in rows]

    def list_active(self, user_id: str, task_type: str) -> list[WorkflowSkillCard]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? "
                "AND json_extract(payload, '$.status') = ? "
                "AND json_extract(payload, '$.task_type') IN (?, 'GENERAL') "
                "ORDER BY updated_at DESC",
                (user_id, WorkflowSkillStatus.ACTIVE.value, task_type),
            ).fetchall()
            return [self._card_from_row(row) for row in rows]

    def save_evidence(self, evidence: WorkflowSkillEvidence) -> WorkflowSkillEvidence:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM workflow_skill_evidence "
                "WHERE user_id = ? AND task_id = ?",
                (evidence.user_id, evidence.task_id),
            ).fetchone()
            if existing:
                connection.commit()
                return self._evidence_from_row(existing)
            connection.execute(
                "INSERT INTO workflow_skill_evidence"
                "(evidence_id,user_id,task_id,bucket_signature,"
                "control_flow_signature,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    evidence.evidence_id,
                    evidence.user_id,
                    evidence.task_id,
                    evidence.bucket_signature,
                    evidence.control_flow_signature,
                    _json(evidence.model_dump(mode="json")),
                    evidence.created_at,
                ),
            )
            connection.commit()
            return evidence

    def list_evidence(
        self,
        user_id: str,
        *,
        bucket_signature: str | None = None,
        control_flow_signature: str | None = None,
    ) -> list[WorkflowSkillEvidence]:
        clauses = ["user_id = ?"]
        values: list[str] = [user_id]
        if bucket_signature:
            clauses.append("bucket_signature = ?")
            values.append(bucket_signature)
        if control_flow_signature:
            clauses.append("control_flow_signature = ?")
            values.append(control_flow_signature)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_skill_evidence WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at",
                values,
            ).fetchall()
            return [self._evidence_from_row(row) for row in rows]

    def save_candidate(
        self,
        card: WorkflowSkillCard,
        promotion_threshold: int = 2,
        minimum_structure_consistency: float = 1.0,
    ) -> WorkflowSkillCard:
        promotion_threshold = max(2, promotion_threshold)
        minimum_structure_consistency = min(
            1.0, max(0.0, minimum_structure_consistency)
        )

        def promotion_ready(candidate: WorkflowSkillCard) -> bool:
            requires_business_outcome = (
                candidate.applicability.irreversible
                or candidate.applicability.action in _BUSINESS_OUTCOME_ACTIONS
            )
            business_ready = (
                not requires_business_outcome
                or (
                    candidate.quality.business_outcome_coverage >= 1.0
                    and candidate.quality.business_success_rate == 1.0
                )
            )
            return (
                candidate.schema_version >= 2
                and candidate.evidence_count >= promotion_threshold
                and candidate.graph.complete
                and candidate.quality.structure_consistency
                >= minimum_structure_consistency
                and business_ready
            )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND signature = ?",
                (card.user_id, card.signature),
            ).fetchone()
            if existing_row:
                existing = self._card_from_row(existing_row)
                new_task_ids = [
                    task_id
                    for task_id in card.provenance.source_task_ids
                    if task_id and task_id not in existing.provenance.source_task_ids
                ]
                if not new_task_ids:
                    connection.commit()
                    return existing
                existing.provenance.source_task_ids.extend(new_task_ids)
                existing.evidence_count = len(existing.provenance.source_task_ids)
                existing.provenance.source_count = existing.evidence_count
                existing.success_count += len(new_task_ids)
                existing.consecutive_failures = 0
                existing.confidence = min(
                    1.0, max(existing.confidence, card.confidence) + 0.05
                )
                existing.quality = card.quality
                existing.quality.support_count = existing.evidence_count
                for example in card.intent_examples:
                    if example not in existing.intent_examples:
                        existing.intent_examples.append(example)
                existing.updated_at = _now()
                if (
                    existing.status != WorkflowSkillStatus.DISABLED
                    and promotion_ready(existing)
                ):
                    existing.status = WorkflowSkillStatus.ACTIVE
                    existing.validation.status = "validated"
                    existing.validation.validated_at = _now()
                card = existing
            else:
                card.evidence_count = len(card.provenance.source_task_ids)
                card.provenance.source_count = card.evidence_count
                card.quality.support_count = card.evidence_count
                if promotion_ready(card):
                    card.status = WorkflowSkillStatus.ACTIVE
                    card.validation.status = "validated"
                    card.validation.validated_at = _now()
                rows = connection.execute(
                    "SELECT * FROM workflow_skills WHERE user_id = ?",
                    (card.user_id,),
                ).fetchall()
                family_cards = [
                    self._card_from_row(row)
                    for row in rows
                    if self._card_from_row(row).family_signature == card.family_signature
                ]
                card.version = max((item.version for item in family_cards), default=0) + 1
            if card.status == WorkflowSkillStatus.ACTIVE and card.family_signature:
                self._disable_active_siblings(connection, card)
            connection.execute(
                """INSERT INTO workflow_skills
                (skill_id,user_id,signature,payload,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(user_id,signature) DO UPDATE SET
                  skill_id=excluded.skill_id, payload=excluded.payload,
                  updated_at=excluded.updated_at""",
                (
                    card.skill_id,
                    card.user_id,
                    card.signature,
                    _json(card.model_dump(mode="json")),
                    card.created_at,
                    card.updated_at,
                ),
            )
            connection.commit()
            return card

    def _disable_active_siblings(
        self, connection: sqlite3.Connection, card: WorkflowSkillCard
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM workflow_skills WHERE user_id = ?", (card.user_id,)
        ).fetchall()
        for row in rows:
            sibling = self._card_from_row(row)
            if (
                sibling.skill_id != card.skill_id
                and sibling.family_signature == card.family_signature
                and sibling.status == WorkflowSkillStatus.ACTIVE
            ):
                sibling.status = WorkflowSkillStatus.DISABLED
                sibling.updated_at = _now()
                connection.execute(
                    "UPDATE workflow_skills SET payload = ?, updated_at = ? "
                    "WHERE user_id = ? AND skill_id = ?",
                    (
                        _json(sibling.model_dump(mode="json")),
                        sibling.updated_at,
                        sibling.user_id,
                        sibling.skill_id,
                    ),
                )

    def update(self, card: WorkflowSkillCard) -> WorkflowSkillCard:
        card.updated_at = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? "
                "WHERE user_id = ? AND skill_id = ?",
                (
                    _json(card.model_dump(mode="json")),
                    card.updated_at,
                    card.user_id,
                    card.skill_id,
                ),
            )
        return card

    def activate(self, user_id: str, skill_id: str) -> WorkflowSkillCard:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND skill_id = ?",
                (user_id, skill_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"workflow skill not found: {skill_id}")
            card = self._card_from_row(row)
            card.status = WorkflowSkillStatus.ACTIVE
            card.confidence = max(card.confidence, 0.8)
            card.validation.status = "operator_approved"
            card.validation.operator_override = True
            card.validation.validated_at = _now()
            card.updated_at = _now()
            if card.family_signature:
                self._disable_active_siblings(connection, card)
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? "
                "WHERE user_id = ? AND skill_id = ?",
                (
                    _json(card.model_dump(mode="json")),
                    card.updated_at,
                    user_id,
                    skill_id,
                ),
            )
            connection.commit()
            return card

    def disable(self, user_id: str, skill_id: str) -> WorkflowSkillCard:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND skill_id = ?",
                (user_id, skill_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"workflow skill not found: {skill_id}")
            card = self._card_from_row(row)
            card.status = WorkflowSkillStatus.DISABLED
            card.updated_at = _now()
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? "
                "WHERE user_id = ? AND skill_id = ?",
                (
                    _json(card.model_dump(mode="json")),
                    card.updated_at,
                    user_id,
                    skill_id,
                ),
            )
            connection.commit()
            return card

    def record_outcome(
        self,
        user_id: str,
        skill_id: str,
        success: bool,
        failure_threshold: int,
    ) -> Optional[WorkflowSkillCard]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND skill_id = ?",
                (user_id, skill_id),
            ).fetchone()
            if row is None:
                return None
            card = self._card_from_row(row)
            card.last_used_at = _now()
            if success:
                card.success_count += 1
                card.consecutive_failures = 0
            else:
                card.failure_count += 1
                card.consecutive_failures += 1
                if card.consecutive_failures >= failure_threshold:
                    card.status = WorkflowSkillStatus.DISABLED
            total = card.success_count + card.failure_count
            card.quality.execution_success_rate = (
                card.success_count / total if total else 0.0
            )
            card.updated_at = _now()
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? "
                "WHERE user_id = ? AND skill_id = ?",
                (
                    _json(card.model_dump(mode="json")),
                    card.updated_at,
                    user_id,
                    skill_id,
                ),
            )
            connection.commit()
            return card

    def mark_successful_reuse(
        self, user_id: str, skill_id: str
    ) -> Optional[WorkflowSkillCard]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND skill_id = ?",
                (user_id, skill_id),
            ).fetchone()
            if row is None:
                return None
            card = self._card_from_row(row)
            card.last_used_at = _now()
            card.consecutive_failures = 0
            card.updated_at = _now()
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? "
                "WHERE user_id = ? AND skill_id = ?",
                (
                    _json(card.model_dump(mode="json")),
                    card.updated_at,
                    user_id,
                    skill_id,
                ),
            )
            connection.commit()
            return card


class WorkflowSkillManager:
    def __init__(
        self,
        settings: WorkflowSkillSettings | None = None,
        store: WorkflowSkillStore | None = None,
    ):
        self.settings = settings or WorkflowSkillSettings.from_env()
        self.store = store or WorkflowSkillStore(self.settings.store_path)

    @staticmethod
    def _agents_from_steps(steps: list[dict[str, Any]]) -> list[str]:
        return list(
            dict.fromkeys(
                str(step.get("agent_name"))
                for step in steps
                if isinstance(step, dict) and step.get("agent_name")
            )
        )

    def distill(
        self,
        *,
        user_id: str,
        task_id: str,
        user_query: str,
        planning_steps: list[dict[str, Any]],
        task_profile: dict[str, Any] | None = None,
        intent_examples: Iterable[str] | None = None,
        agent_contracts: dict[str, str] | None = None,
        agent_capabilities: dict[str, list[str]] | None = None,
        outcome_summary: dict[str, Any] | None = None,
    ) -> WorkflowSkillCard:
        if not planning_steps:
            raise ValueError("workflow skill requires planning steps")
        normalized_outcome = dict(outcome_summary or {"technical_success": True})
        if normalized_outcome.get("technical_success") is not True:
            raise ValueError(
                "workflow skill requires a technically successful execution"
            )
        if normalized_outcome.get("business_success") is False:
            raise ValueError(
                "workflow skill cannot learn from a failed business outcome"
            )
        profile = dict(task_profile or {})
        if (
            contains_secret(_json(planning_steps))
            or contains_secret(user_query)
            or contains_secret(_json(profile))
        ):
            raise ValueError("workflow skill source contains secret-looking content")

        applicability = _applicability(profile)
        slots = _slots_from_profile(profile)
        parameterized = _parameterize_steps(
            planning_steps,
            default_action=applicability.action,
            default_risk=applicability.max_risk,
        )
        agents = self._agents_from_steps(parameterized)
        if not parameterized or not agents:
            raise ValueError("workflow skill requires valid Agent planning steps")
        contracts = {
            str(name): str(fingerprint)
            for name, fingerprint in (agent_contracts or {}).items()
            if str(name) in agents and str(fingerprint)
        }
        graph = _compile_graph(
            planning_steps,
            parameterized,
            applicability.expected_capabilities,
            slots,
            dict(agent_capabilities or {}),
        )
        if not graph.complete:
            raise ValueError("workflow skill graph is incomplete")
        family_signature = _family_signature(applicability)
        signature = _control_flow_signature(graph, contracts)
        evidence = WorkflowSkillEvidence(
            evidence_id=f"wevidence_{uuid.uuid4().hex}",
            user_id=user_id,
            task_id=task_id,
            bucket_signature=family_signature,
            control_flow_signature=signature,
            task_profile=_sanitized_profile(applicability, slots),
            slots=slots,
            graph=graph,
            planning_steps=parameterized,
            contract_fingerprints=contracts,
            outcome_summary=normalized_outcome,
        )
        saved_evidence = self.store.save_evidence(evidence)
        if saved_evidence.control_flow_signature != signature:
            existing = self.store.get_by_signature(
                user_id, saved_evidence.control_flow_signature
            )
            if existing is not None:
                return existing
            raise ValueError("task already contributed to another workflow skill")

        supporting = self.store.list_evidence(
            user_id,
            bucket_signature=family_signature,
            control_flow_signature=signature,
        )
        bucket_evidence = self.store.list_evidence(
            user_id,
            bucket_signature=family_signature,
        )
        source_task_ids = list(dict.fromkeys(item.task_id for item in supporting))
        business_results = [
            result
            for item in supporting
            if (result := _verified_business_result(item.outcome_summary))
            is not None
        ]
        quality = WorkflowSkillQuality(
            support_count=len(source_task_ids),
            structure_consistency=(
                len(supporting) / len(bucket_evidence)
                if bucket_evidence
                else 0.0
            ),
            slot_coverage=1.0 if all(slot.name for slot in slots) else 0.0,
            execution_success_rate=1.0,
            business_success_rate=(
                sum(bool(item) for item in business_results) / len(business_results)
                if business_results
                else None
            ),
            business_outcome_coverage=(
                len(business_results) / len(supporting) if supporting else 0.0
            ),
            contract_stability=1.0,
        )
        examples = _intent_examples(applicability, intent_examples)
        if contains_secret(_json(examples)):
            raise ValueError("workflow skill source contains secret-looking content")
        card = WorkflowSkillCard(
            skill_id=f"wskill_{uuid.uuid4().hex}",
            user_id=user_id,
            name=f"workflow_{applicability.intent}",
            description=(
                f"Reusable {applicability.intent.replace('_', ' ')} procedure "
                "distilled from successful executions"
            ),
            schema_version=2,
            family_signature=family_signature,
            signature=signature,
            task_type=applicability.task_type,
            intent_examples=examples,
            scenario_tags=applicability.scenario_tags,
            expected_capabilities=applicability.expected_capabilities,
            risk_profile=applicability.max_risk,
            planning_steps=parameterized,
            required_agents=agents,
            applicability=applicability,
            slots=slots,
            preconditions=[f"slot:{slot.name}" for slot in slots if slot.required],
            graph=graph,
            postconditions=[
                f"node_completed:{node_id}" for node_id in graph.exit_nodes
            ],
            outputs=[f"{node_id}.result" for node_id in graph.exit_nodes],
            failure_modes=[
                "missing_required_slot",
                "agent_unavailable",
                "contract_mismatch",
                "authorization_denied",
                "execution_failed",
            ],
            contract_fingerprints=contracts,
            quality=quality,
            confidence=min(0.95, 0.55 + 0.1 * len(source_task_ids)),
            evidence_count=len(source_task_ids),
            success_count=len(source_task_ids),
            provenance=WorkflowSkillProvenance(
                source_task_ids=source_task_ids,
                source_count=len(source_task_ids),
            ),
        )
        return self.store.save_candidate(
            card,
            max(2, self.settings.promotion_success_threshold),
            self.settings.minimum_structure_consistency,
        )

    def _applicability_checks(
        self,
        card: WorkflowSkillCard,
        profile: dict[str, Any],
        available_agents: set[str],
        agent_contracts: dict[str, str],
    ) -> dict[str, bool]:
        current = _applicability(profile)
        skill = card.applicability
        current_caps = set(current.expected_capabilities)
        skill_caps = set(skill.expected_capabilities)
        current_scopes = set(current.data_scopes)
        skill_scopes = set(skill.data_scopes)
        entities = {
            _normalize_token(key)
            for key in (profile.get("entities") or {})
            if key
        } if isinstance(profile.get("entities") or {}, dict) else set()
        missing = {
            _normalize_token(item)
            for item in profile.get("missing_fields", [])
            if item
        }
        required_slots = {slot.name for slot in card.slots if slot.required}
        contracts_ok = (
            not card.contract_fingerprints
            or all(
                agent_contracts.get(name) == fingerprint
                for name, fingerprint in card.contract_fingerprints.items()
            )
        )
        return {
            "schema": card.schema_version >= 2 or self.settings.allow_legacy_reuse,
            "task_ready": not missing and not bool(profile.get("needs_clarification")),
            "intent": (
                card.schema_version < 2
                or current.intent == skill.intent
                or skill.intent == "general_assistance"
            ),
            "action": card.schema_version < 2 or current.action == skill.action,
            "capabilities": (
                card.schema_version < 2
                or not current_caps
                or current_caps.issubset(skill_caps)
            ),
            "data_scope": (
                card.schema_version < 2
                or not current_scopes
                or current_scopes.issubset(skill_scopes)
            ),
            "risk": (
                card.schema_version < 2
                or _RISK_LEVEL[current.max_risk] <= _RISK_LEVEL[skill.max_risk]
            ),
            "slots": not required_slots or (
                required_slots.issubset(entities) and not (required_slots & missing)
            ),
            "agents": set(card.required_agents).issubset(available_agents),
            "contracts": contracts_ok,
            "graph": card.schema_version < 2 or card.graph.complete,
        }

    def _score(
        self,
        card: WorkflowSkillCard,
        query: str,
        profile: dict[str, Any],
    ) -> tuple[float, float, str]:
        current = _applicability(profile)
        query_terms = _safe_terms(query)
        example_scores = []
        for example in card.intent_examples:
            intent_terms = _safe_terms(example)
            if intent_terms:
                example_scores.append(
                    len(query_terms & intent_terms) / max(1, len(intent_terms))
                )
        lexical_score = max(example_scores, default=0.0)
        intent_score = 1.0 if current.intent == card.applicability.intent else 0.0
        action_score = 1.0 if current.action == card.applicability.action else 0.0
        current_caps = set(current.expected_capabilities)
        skill_caps = set(card.applicability.expected_capabilities)
        capability_score = len(current_caps & skill_caps) / max(
            1, len(current_caps | skill_caps)
        )
        current_scopes = set(current.data_scopes)
        skill_scopes = set(card.applicability.data_scopes)
        scope_score = len(current_scopes & skill_scopes) / max(
            1, len(current_scopes | skill_scopes)
        )
        tags = set(current.scenario_tags)
        card_tags = set(card.applicability.scenario_tags)
        tag_score = len(tags & card_tags) / max(1, len(tags | card_tags))
        if card.schema_version < 2:
            type_score = 1.0 if current.task_type in {card.task_type, "GENERAL"} else 0.0
            score = 0.55 * lexical_score + 0.3 * type_score + 0.15 * tag_score
        else:
            score = (
                0.25 * lexical_score
                + 0.25 * intent_score
                + 0.15 * action_score
                + 0.15 * capability_score
                + 0.1 * scope_score
                + 0.1 * tag_score
            )
        reason = (
            f"lexical={lexical_score:.2f}, intent={intent_score:.2f}, "
            f"action={action_score:.2f}, capabilities={capability_score:.2f}, "
            f"data_scope={scope_score:.2f}, tags={tag_score:.2f}"
        )
        return score, lexical_score, reason

    def match(
        self,
        *,
        user_id: str,
        query: str,
        task_profile: dict[str, Any],
        available_agents: Iterable[str],
        agent_contracts: dict[str, str] | None = None,
    ) -> Optional[WorkflowSkillMatch]:
        if (
            not self.settings.enabled
            or not self.settings.reuse_enabled
            or not query.strip()
        ):
            return None
        available = set(available_agents)
        contracts = dict(agent_contracts or {})
        current_task_type = str(
            task_profile.get("task_type") or "GENERAL"
        ).upper()
        candidates: list[WorkflowSkillMatch] = []
        for card in self.store.list_active(user_id, current_task_type):
            checks = self._applicability_checks(
                card, task_profile, available, contracts
            )
            if not all(checks.values()):
                continue
            score, lexical_score, reason = self._score(card, query, task_profile)
            if score < self.settings.match_threshold:
                continue
            candidates.append(
                WorkflowSkillMatch(
                    skill=card,
                    score=score,
                    lexical_score=lexical_score,
                    reason=reason,
                    bound_planning_steps=_bind_skill_plan(
                        card, query, task_profile
                    ),
                    applicability_checks=checks,
                )
            )
        candidates.sort(key=lambda item: item.score, reverse=True)
        if not candidates:
            return None
        if (
            len(candidates) > 1
            and candidates[0].score - candidates[1].score
            < self.settings.match_margin
        ):
            return None
        return candidates[0]


_manager: WorkflowSkillManager | None = None


def get_workflow_skill_manager() -> WorkflowSkillManager:
    global _manager
    if _manager is None:
        _manager = WorkflowSkillManager()
    return _manager


def set_workflow_skill_manager(manager: WorkflowSkillManager | None) -> None:
    global _manager
    _manager = manager


__all__ = [
    "WorkflowSkillStatus",
    "WorkflowSkillSettings",
    "WorkflowSkillProvenance",
    "WorkflowSkillSlot",
    "WorkflowSkillApplicability",
    "WorkflowSkillGraphNode",
    "WorkflowSkillGraphEdge",
    "WorkflowSkillGraph",
    "WorkflowSkillQuality",
    "WorkflowSkillValidation",
    "WorkflowSkillEvidence",
    "WorkflowSkillCard",
    "WorkflowSkillMatch",
    "WorkflowSkillStore",
    "WorkflowSkillManager",
    "get_workflow_skill_manager",
    "set_workflow_skill_manager",
]

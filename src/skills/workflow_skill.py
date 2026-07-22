"""Declarative, reusable workflow skills distilled from successful runs.

This module deliberately stores plans rather than executable Python or replay
state. It is safe to use as a backend capability before the Web UI exposes it.
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
    "request", "task", "current", "user", "请", "帮", "我", "一下",
}
_SCENARIO_INTENT_ALIASES = {
    "leave_request": [
        "请假", "休假", "年假", "病假", "事假", "调休", "产假", "陪产假",
        "leave", "time off", "sick leave", "annual leave",
    ],
    "travel_request": ["出差", "差旅", "business travel"],
    "salary_query": ["工资", "薪资", "salary"],
    "employee_info": ["员工信息", "人员信息", "employee information"],
    "employee_proof": ["员工证明", "在职证明", "employment certificate"],
    "notification_send": ["发送通知", "发送邮件", "send notification", "send email"],
    "mass_notification": ["批量通知", "群发", "batch notification"],
    "risk_analysis": ["风险分析", "risk analysis"],
    "market_research": ["市场调研", "market research"],
}
_ALLOWED_TASK_TYPES = {
    "GENERAL", "HR", "COMMUNICATION", "RISK", "DOCUMENT", "RESEARCH",
}
_ALLOWED_CAPABILITIES = {
    "general", "hr", "hr_data_access", "salary_information_retrieval",
    "leave management", "communication", "research", "document",
}
_DATA_PATH_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_INSTANCE_IDENTIFIER_RE = re.compile(
    r"(?:@|\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|^[A-Z]{1,4}\d{2,}$)"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
    store_path: Path = Path("store/skills/workflow_skills.sqlite3")

    @classmethod
    def from_env(cls) -> "WorkflowSkillSettings":
        def boolean(name: str, default: bool) -> bool:
            value = os.getenv(name)
            return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}

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
            promotion_success_threshold=max(1, integer("WORKFLOW_SKILL_PROMOTION_THRESHOLD", 2)),
            failure_disable_threshold=max(1, integer("WORKFLOW_SKILL_FAILURE_THRESHOLD", 2)),
            store_path=Path(os.getenv("WORKFLOW_SKILL_DB_PATH", "store/skills/workflow_skills.sqlite3")),
        )


class WorkflowSkillProvenance(BaseModel):
    source_task_ids: list[str] = Field(default_factory=list)
    source_count: int = 1
    distilled_at: str = Field(default_factory=_now)


class WorkflowSkillCard(BaseModel):
    model_config = ConfigDict(extra="allow")

    skill_id: str
    user_id: str
    name: str
    description: str
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


def _safe_terms(text: str) -> set[str]:
    normalized = text.replace("_", " " ).replace("-", " ")
    return {term for term in lexical_terms(normalized) if term not in _STOP_WORDS and len(term) > 1}


def _replace_request(value: Any, request: str) -> Any:
    if isinstance(value, str):
        return value.replace(_PLACEHOLDER, request)
    if isinstance(value, list):
        return [_replace_request(item, request) for item in value]
    if isinstance(value, dict):
        return {key: _replace_request(item, request) for key, item in value.items()}
    return value


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
        raise ValueError(f"workflow skill input references unknown prior Agent: {source_step}")
    for field_name, identifier in (
        ("parameter_name", parameter_name),
        ("source_output", source_output),
    ):
        if not _DATA_PATH_RE.fullmatch(identifier) or _INSTANCE_IDENTIFIER_RE.search(identifier):
            raise ValueError(f"workflow skill input has invalid {field_name}")
    return {
        "parameter_name": parameter_name,
        "source_step": source_step,
        "source_output": source_output,
        "description": f"将 {source_step}.{source_output} 映射到 {parameter_name}",
    }


def _parameterize_steps(planning_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain procedure structure while removing task-instance values."""

    parameterized: list[dict[str, Any]] = []
    prior_agents: set[str] = set()
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
        parameterized.append(
            {
                "agent_name": agent_name,
                "title": f"可复用步骤 {index}：{agent_name}",
                "description": (
                    f"由 {agent_name} 处理本次请求。只能使用本次请求和前序步骤映射输出中的数据。"
                    f"本次请求：{_PLACEHOLDER}"
                ),
                "note": "禁止复用源任务中的员工、日期、收件人、数量、原因或其他实例值。",
                "inputs": inputs,
                "request_context": _PLACEHOLDER,
            }
        )
        prior_agents.add(agent_name)
    return parameterized


def _intent_examples(
    profile: dict[str, Any],
    explicit_examples: Iterable[str] | None,
    user_query: str,
) -> list[str]:
    examples = [str(item).strip() for item in explicit_examples or [] if str(item).strip()]
    tags = [str(item).strip() for item in profile.get("scenario_tags", []) if str(item).strip()]
    for tag in tags:
        aliases = _SCENARIO_INTENT_ALIASES.get(tag.casefold())
        if aliases:
            examples.extend(aliases)
        elif not tag.casefold().endswith(("_service", "_operation")):
            examples.append(tag)
    lowered_query = user_query.casefold()
    for aliases in _SCENARIO_INTENT_ALIASES.values():
        if any(alias.casefold() in lowered_query for alias in aliases):
            examples.extend(aliases)
    return list(dict.fromkeys(examples))


def _inferred_scenario_tags(user_query: str) -> list[str]:
    lowered_query = user_query.casefold()
    return [
        tag
        for tag, aliases in _SCENARIO_INTENT_ALIASES.items()
        if any(alias.casefold() in lowered_query for alias in aliases)
    ]


def _family_signature(
    task_type: str,
    tags: Iterable[str],
) -> str:
    payload = {
        "task_type": str(task_type or "GENERAL").upper(),
        "tags": sorted({str(item).casefold() for item in tags if item}),
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _signature(family_signature: str, planning_steps: list[dict[str, Any]]) -> str:
    structure = []
    for step in planning_steps:
        structure.append(
            {
                "agent_name": step.get("agent_name"),
                "inputs": [
                    {
                        "parameter_name": mapping.get("parameter_name"),
                        "source_step": mapping.get("source_step"),
                        "source_output": mapping.get("source_output"),
                    }
                    for mapping in step.get("inputs", [])
                    if isinstance(mapping, dict)
                ],
            }
        )
    return hashlib.sha256(
        _json({"family_signature": family_signature, "structure": structure}).encode("utf-8")
    ).hexdigest()


class WorkflowSkillStore:
    """SQLite persistence with user-scoped reads and transactional updates."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
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

    @staticmethod
    def _from_row(row: sqlite3.Row) -> WorkflowSkillCard:
        return WorkflowSkillCard.model_validate(json.loads(row["payload"]))

    def get(self, user_id: str, skill_id: str) -> Optional[WorkflowSkillCard]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND skill_id = ?",
                (user_id, skill_id),
            ).fetchone()
            return self._from_row(row) if row else None

    def list(self, user_id: str, include_shared: bool = True) -> list[WorkflowSkillCard]:
        users = [user_id, "share"] if include_shared and user_id != "share" else [user_id]
        placeholders = ",".join("?" for _ in users)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM workflow_skills WHERE user_id IN ({placeholders}) "
                "ORDER BY updated_at DESC",
                users,
            ).fetchall()
            return [self._from_row(row) for row in rows]

    def list_active(self, user_id: str, task_type: str) -> list[WorkflowSkillCard]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? "
                "AND json_extract(payload, '$.status') = ? "
                "AND json_extract(payload, '$.task_type') IN (?, 'GENERAL') "
                "ORDER BY updated_at DESC",
                (user_id, WorkflowSkillStatus.ACTIVE.value, task_type),
            ).fetchall()
            return [self._from_row(row) for row in rows]

    def save_candidate(self, card: WorkflowSkillCard, promotion_threshold: int = 2) -> WorkflowSkillCard:
        with self._lock, self._connect() as connection:
            # Serialize the read-modify-write cycle across Store instances and
            # processes, not only threads sharing this Python object.
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND signature = ?",
                (card.user_id, card.signature),
            ).fetchone()
            if existing_row:
                existing = self._from_row(existing_row)
                new_task_ids = [
                    task_id
                    for task_id in card.provenance.source_task_ids
                    if task_id and task_id not in existing.provenance.source_task_ids
                ]
                if not new_task_ids:
                    connection.commit()
                    return existing
                existing.evidence_count += len(new_task_ids)
                existing.success_count += len(new_task_ids)
                existing.consecutive_failures = 0
                existing.confidence = min(1.0, max(existing.confidence, card.confidence) + 0.05)
                existing.provenance.source_count = existing.evidence_count
                existing.provenance.source_task_ids.extend(new_task_ids)
                for example in card.intent_examples:
                    if example not in existing.intent_examples:
                        existing.intent_examples.append(example)
                existing.updated_at = _now()
                if existing.status != WorkflowSkillStatus.DISABLED and promotion_threshold <= existing.evidence_count:
                    existing.status = WorkflowSkillStatus.ACTIVE
                card = existing
            else:
                if (
                    card.status != WorkflowSkillStatus.DISABLED
                    and promotion_threshold <= card.evidence_count
                ):
                    card.status = WorkflowSkillStatus.ACTIVE
                rows = connection.execute(
                    "SELECT * FROM workflow_skills WHERE user_id = ?",
                    (card.user_id,),
                ).fetchall()
                loaded_cards = [self._from_row(row) for row in rows]
                family_cards = [
                    item for item in loaded_cards if item.family_signature == card.family_signature
                ]
                card.version = max((item.version for item in family_cards), default=0) + 1
            payload = card.model_dump(mode="json")
            if card.status == WorkflowSkillStatus.ACTIVE and card.family_signature:
                for sibling in connection.execute(
                    "SELECT * FROM workflow_skills WHERE user_id = ?",
                    (card.user_id,),
                ).fetchall():
                    sibling_card = self._from_row(sibling)
                    if (
                        sibling_card.skill_id != card.skill_id
                        and sibling_card.family_signature == card.family_signature
                        and sibling_card.status == WorkflowSkillStatus.ACTIVE
                    ):
                        sibling_card.status = WorkflowSkillStatus.DISABLED
                        sibling_card.updated_at = _now()
                        connection.execute(
                            "UPDATE workflow_skills SET payload = ?, updated_at = ? "
                            "WHERE user_id = ? AND skill_id = ?",
                            (
                                _json(sibling_card.model_dump(mode="json")),
                                sibling_card.updated_at,
                                sibling_card.user_id,
                                sibling_card.skill_id,
                            ),
                        )
            connection.execute(
                """INSERT INTO workflow_skills(skill_id,user_id,signature,payload,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(user_id,signature) DO UPDATE SET
                  skill_id=excluded.skill_id, payload=excluded.payload,
                  updated_at=excluded.updated_at""",
                (card.skill_id, card.user_id, card.signature, _json(payload), card.created_at, card.updated_at),
            )
            connection.commit()
            return card

    def update(self, card: WorkflowSkillCard) -> WorkflowSkillCard:
        card.updated_at = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? WHERE user_id = ? AND skill_id = ?",
                (_json(card.model_dump(mode="json")), card.updated_at, card.user_id, card.skill_id),
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
            card = self._from_row(row)
            card.status = WorkflowSkillStatus.ACTIVE
            card.confidence = max(card.confidence, 0.8)
            card.updated_at = _now()
            if card.family_signature:
                rows = connection.execute(
                    "SELECT * FROM workflow_skills WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                for row in rows:
                    sibling = self._from_row(row)
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
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? WHERE user_id = ? AND skill_id = ?",
                (_json(card.model_dump(mode="json")), card.updated_at, user_id, skill_id),
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
            card = self._from_row(row)
            card.status = WorkflowSkillStatus.DISABLED
            card.updated_at = _now()
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? WHERE user_id = ? AND skill_id = ?",
                (_json(card.model_dump(mode="json")), card.updated_at, user_id, skill_id),
            )
            connection.commit()
            return card

    def record_outcome(self, user_id: str, skill_id: str, success: bool, failure_threshold: int) -> Optional[WorkflowSkillCard]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND skill_id = ?",
                (user_id, skill_id),
            ).fetchone()
            if row is None:
                return None
            card = self._from_row(row)
            card.last_used_at = _now()
            if success:
                card.success_count += 1
                card.consecutive_failures = 0
            else:
                card.failure_count += 1
                card.consecutive_failures += 1
                if card.consecutive_failures >= failure_threshold:
                    card.status = WorkflowSkillStatus.DISABLED
            card.updated_at = _now()
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? WHERE user_id = ? AND skill_id = ?",
                (_json(card.model_dump(mode="json")), card.updated_at, user_id, skill_id),
            )
            connection.commit()
            return card

    def mark_successful_reuse(self, user_id: str, skill_id: str) -> Optional[WorkflowSkillCard]:
        """Update reuse health when distillation already counted this success."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_skills WHERE user_id = ? AND skill_id = ?",
                (user_id, skill_id),
            ).fetchone()
            if row is None:
                return None
            card = self._from_row(row)
            card.last_used_at = _now()
            card.consecutive_failures = 0
            card.updated_at = _now()
            connection.execute(
                "UPDATE workflow_skills SET payload = ?, updated_at = ? WHERE user_id = ? AND skill_id = ?",
                (_json(card.model_dump(mode="json")), card.updated_at, user_id, skill_id),
            )
            connection.commit()
            return card


class WorkflowSkillManager:
    def __init__(self, settings: WorkflowSkillSettings | None = None, store: WorkflowSkillStore | None = None):
        self.settings = settings or WorkflowSkillSettings.from_env()
        self.store = store or WorkflowSkillStore(self.settings.store_path)

    @staticmethod
    def _agents_from_steps(steps: list[dict[str, Any]]) -> list[str]:
        return list(dict.fromkeys(str(step.get("agent_name")) for step in steps if isinstance(step, dict) and step.get("agent_name")))

    def distill(
        self,
        *,
        user_id: str,
        task_id: str,
        user_query: str,
        planning_steps: list[dict[str, Any]],
        task_profile: dict[str, Any] | None = None,
        intent_examples: Iterable[str] | None = None,
    ) -> WorkflowSkillCard:
        if not planning_steps:
            raise ValueError("workflow skill requires planning steps")
        serialized = _json(planning_steps)
        source_profile = dict(task_profile or {})
        if (
            contains_secret(serialized)
            or contains_secret(user_query)
            or contains_secret(_json(source_profile))
        ):
            raise ValueError("workflow skill source contains secret-looking content")
        profile = dict(source_profile)
        task_type = str(profile.get("task_type") or "GENERAL").upper()
        profile["task_type"] = task_type if task_type in _ALLOWED_TASK_TYPES else "GENERAL"
        scenario_tags = [
            str(item).casefold()
            for item in profile.get("scenario_tags", [])
            if str(item).casefold() in _SCENARIO_INTENT_ALIASES
        ]
        inferred_tags = _inferred_scenario_tags(user_query)
        for inferred_tag in inferred_tags:
            if inferred_tag not in scenario_tags:
                scenario_tags.append(inferred_tag)
        profile["scenario_tags"] = scenario_tags
        profile["expected_capabilities"] = [
            str(item)
            for item in profile.get("expected_capabilities", [])
            if str(item).casefold() in _ALLOWED_CAPABILITIES
        ]
        risk_profile = str(profile.get("risk_profile") or "LOW").upper()
        profile["risk_profile"] = risk_profile if risk_profile in {"LOW", "MEDIUM", "HIGH", "CRITICAL"} else "LOW"
        examples = _intent_examples(profile, intent_examples, user_query)
        if contains_secret(_json(examples)):
            raise ValueError("workflow skill source contains secret-looking content")
        agents = self._agents_from_steps(planning_steps)
        parameterized = _parameterize_steps(planning_steps)
        if not parameterized or not agents:
            raise ValueError("workflow skill requires valid Agent planning steps")
        family_signature = _family_signature(
            profile.get("task_type", "GENERAL"),
            inferred_tags or scenario_tags or [f"agent:{agent}" for agent in agents],
        )
        signature = _signature(family_signature, parameterized)
        card = WorkflowSkillCard(
            skill_id=f"wskill_{uuid.uuid4().hex}",
            user_id=user_id,
            name=f"workflow_{str(profile.get('task_type', 'general')).lower()}_{agents[0] if agents else 'task'}",
            description=f"由成功的 {profile.get('task_type', 'GENERAL')} 执行轨迹蒸馏得到的可复用工作流",
            family_signature=family_signature,
            signature=signature,
            task_type=str(profile.get("task_type", "GENERAL")).upper(),
            intent_examples=examples,
            scenario_tags=scenario_tags,
            expected_capabilities=[str(item) for item in profile.get("expected_capabilities", [])],
            risk_profile=str(profile.get("risk_profile", "LOW")).upper(),
            planning_steps=parameterized,
            required_agents=agents,
            confidence=0.65,
            provenance=WorkflowSkillProvenance(source_task_ids=[task_id]),
        )
        return self.store.save_candidate(card, self.settings.promotion_success_threshold)

    def bootstrap_leave_request(
        self,
        user_id: str,
        lookup_agent_name: str = "RemoteHRAssistantAgent",
        action_agent_name: str = "RemoteOfficeAssistantAgent",
    ) -> WorkflowSkillCard:
        query = "\u8bf7\u5047 leave request"
        steps = [
            {
                "agent_name": lookup_agent_name,
                "title": "Resolve the employee identity",
                "description": "Find the employee ID and name for the current leave request.",
                "inputs": [],
            },
            {
                "agent_name": action_agent_name,
                "title": "Save the leave request",
                "description": "Save the current leave type, dates, and reason for the resolved employee.",
                "inputs": [
                    {
                        "parameter_name": "employee.id",
                        "source_step": lookup_agent_name,
                        "source_output": "employee.id",
                    },
                    {
                        "parameter_name": "employee.name",
                        "source_step": lookup_agent_name,
                        "source_output": "employee.name",
                    },
                ],
            },
        ]
        return self.distill(
            user_id=user_id,
            task_id="bootstrap-leave-request",
            user_query=query,
            planning_steps=steps,
            task_profile={
                "task_type": "HR",
                "scenario_tags": ["leave_request", "hr_service"],
                "expected_capabilities": ["leave management"],
                "risk_profile": "MEDIUM",
            },
            intent_examples=[
                "请假",
                "申请休假",
                "申请年假",
                "leave request",
                "request time off",
            ],
        )

    def _score(self, card: WorkflowSkillCard, query: str, profile: dict[str, Any]) -> tuple[float, float, str]:
        query_terms = _safe_terms(query)
        example_scores = []
        for example in card.intent_examples:
            intent_terms = _safe_terms(example)
            if intent_terms:
                example_scores.append(len(query_terms & intent_terms) / len(intent_terms))
        lexical_score = max(example_scores, default=0.0)
        task_type = str(profile.get("task_type", "GENERAL")).upper()
        type_score = 1.0 if task_type == card.task_type or card.task_type == "GENERAL" else 0.0
        tags = {str(item).casefold() for item in profile.get("scenario_tags", [])}
        tags.update(_inferred_scenario_tags(query))
        card_tags = {str(item).casefold() for item in card.scenario_tags}
        tag_score = len(tags & card_tags) / max(1, len(tags | card_tags))
        caps = {str(item).casefold() for item in profile.get("expected_capabilities", [])}
        card_caps = {str(item).casefold() for item in card.expected_capabilities}
        capability_score = len(caps & card_caps) / max(1, len(caps | card_caps))
        score = 0.55 * lexical_score + 0.2 * type_score + 0.15 * tag_score + 0.1 * capability_score
        return score, lexical_score, f"lexical={lexical_score:.2f}, type={type_score:.2f}, tags={tag_score:.2f}, capabilities={capability_score:.2f}"

    def match(self, *, user_id: str, query: str, task_profile: dict[str, Any], available_agents: Iterable[str]) -> Optional[WorkflowSkillMatch]:
        if not self.settings.enabled or not self.settings.reuse_enabled or not query.strip():
            return None
        available = set(available_agents)
        candidates: list[WorkflowSkillMatch] = []
        current_task_type = str(task_profile.get("task_type") or "GENERAL").upper()
        for card in self.store.list_active(user_id, current_task_type):
            if current_task_type not in {"GENERAL", card.task_type} and card.task_type != "GENERAL":
                continue
            if not set(card.required_agents).issubset(available):
                continue
            score, lexical_score, reason = self._score(card, query, task_profile)
            if score < self.settings.match_threshold:
                continue
            bound = _replace_request(card.planning_steps, query)
            candidates.append(WorkflowSkillMatch(skill=card, score=score, lexical_score=lexical_score, reason=reason, bound_planning_steps=bound))
        candidates.sort(key=lambda item: item.score, reverse=True)
        if not candidates:
            return None
        if len(candidates) > 1 and candidates[0].score - candidates[1].score < self.settings.match_margin:
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
    "WorkflowSkillCard",
    "WorkflowSkillMatch",
    "WorkflowSkillStore",
    "WorkflowSkillManager",
    "get_workflow_skill_manager",
    "set_workflow_skill_manager",
]

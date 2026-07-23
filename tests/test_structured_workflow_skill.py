from __future__ import annotations

import json

from src.skills.workflow_skill import (
    WorkflowSkillCard,
    WorkflowSkillManager,
    WorkflowSkillSettings,
    WorkflowSkillStatus,
    WorkflowSkillStore,
)


def _manager(tmp_path, **overrides):
    values = {
        "enabled": True,
        "reuse_enabled": True,
        "auto_distill_enabled": True,
        "match_threshold": 0.35,
        "match_margin": 0.05,
        "promotion_success_threshold": 2,
        "failure_disable_threshold": 2,
        "store_path": tmp_path / "workflow-skills.sqlite3",
    }
    values.update(overrides)
    settings = WorkflowSkillSettings(**values)
    return WorkflowSkillManager(
        settings=settings,
        store=WorkflowSkillStore(settings.store_path),
    )


def _profile(
    *,
    intent="generate_monthly_report",
    action="generate",
    capabilities=None,
    data_scope=None,
    risk="MEDIUM",
    entities=None,
    missing_fields=None,
):
    return {
        "intent": intent,
        "primary_goal_intent": intent,
        "task_type": "DOCUMENT",
        "action": action,
        "operation_mode": action,
        "expected_capabilities": capabilities or ["report_generation"],
        "data_scope": data_scope or ["department_metrics"],
        "scenario_tags": ["monthly_operations"],
        "risk_level": risk,
        "risk_profile": risk,
        "entities": entities or {"month": "2026-06", "department": "sales"},
        "missing_fields": missing_fields or [],
    }


def _report_steps():
    return [
        {
            "agent_name": "MetricsReaderAgent",
            "capability": "metrics_retrieval",
            "description": "Read the current reporting period",
            "inputs": [],
        },
        {
            "agent_name": "ReportWriterAgent",
            "capability": "report_generation",
            "description": "Generate the current report",
            "inputs": [
                {
                    "parameter_name": "metrics",
                    "source_step": "MetricsReaderAgent",
                    "source_output": "metrics",
                }
            ],
        },
    ]


def _distill_report(manager, task_id, **kwargs):
    return manager.distill(
        user_id="alice",
        task_id=task_id,
        user_query=kwargs.pop("query", "Generate the June sales report"),
        planning_steps=kwargs.pop("planning_steps", _report_steps()),
        task_profile=kwargs.pop("task_profile", _profile()),
        intent_examples=["generate monthly report"],
        **kwargs,
    )


def test_distillation_emits_auditable_schema_v2_skill_and_evidence(tmp_path):
    manager = _manager(tmp_path)

    card = _distill_report(
        manager,
        "report-task-1",
        agent_contracts={"MetricsReaderAgent": "1.0", "ReportWriterAgent": "2.1"},
    )

    assert card.schema_version == 2
    assert card.status == WorkflowSkillStatus.CANDIDATE
    assert card.applicability.intent == "generate_monthly_report"
    assert card.applicability.action == "generate"
    assert card.applicability.expected_capabilities == ["report_generation"]
    assert {slot.name for slot in card.slots} == {"department", "month"}
    assert [node.capability for node in card.graph.nodes] == [
        "metrics_retrieval",
        "report_generation",
    ]
    assert {(edge.source, edge.target, edge.kind) for edge in card.graph.edges} >= {
        ("step_1", "step_2", "sequence"),
        ("step_1", "step_2", "data"),
    }
    assert card.contract_fingerprints == {
        "MetricsReaderAgent": "1.0",
        "ReportWriterAgent": "2.1",
    }
    assert card.quality.support_count == 1
    assert card.quality.structure_consistency == 1.0

    evidence = manager.store.list_evidence(
        "alice",
        bucket_signature=card.family_signature,
        control_flow_signature=card.signature,
    )
    assert len(evidence) == 1
    assert evidence[0].task_id == "report-task-1"
    serialized = json.dumps(evidence[0].model_dump(mode="json"), ensure_ascii=False)
    assert "2026-06" not in serialized
    assert "sales" not in serialized


def test_distiller_is_domain_independent_and_requires_two_traces(tmp_path):
    manager = _manager(tmp_path, promotion_success_threshold=1)

    first = _distill_report(manager, "report-task-1")
    second = _distill_report(
        manager,
        "report-task-2",
        query="Generate the July support report",
        task_profile=_profile(entities={"month": "2026-07", "department": "support"}),
    )
    notification_profile = {
        "intent": "send_incident_notification",
        "task_type": "COMMUNICATION",
        "action": "send",
        "expected_capabilities": ["notification_delivery"],
        "data_scope": ["incident_subscribers"],
        "scenario_tags": ["incident_update"],
        "risk_level": "HIGH",
        "entities": {"channel": "email", "incident_id": "INC-42"},
    }
    notification = manager.distill(
        user_id="alice",
        task_id="notification-task-1",
        user_query="Notify subscribers about incident 42",
        planning_steps=[
            {
                "agent_name": "NotificationAgent",
                "description": "Send the current incident update",
            }
        ],
        task_profile=notification_profile,
        intent_examples=["send incident notification"],
        agent_capabilities={
            "NotificationAgent": ["notification_delivery", "general"]
        },
    )

    assert first.status == WorkflowSkillStatus.CANDIDATE
    assert second.status == WorkflowSkillStatus.ACTIVE
    assert second.evidence_count == 2
    assert second.quality.support_count == 2
    assert notification.status == WorkflowSkillStatus.CANDIDATE
    assert notification.applicability.intent == "send_incident_notification"
    assert notification.applicability.action == "send"
    assert notification.graph.nodes[0].capability == "notification_delivery"

    second_notification = manager.distill(
        user_id="alice",
        task_id="notification-task-2",
        user_query="Notify subscribers about incident 43",
        planning_steps=[
            {
                "agent_name": "NotificationAgent",
                "description": "Send the current incident update",
            }
        ],
        task_profile={
            **notification_profile,
            "entities": {"channel": "email", "incident_id": "INC-43"},
        },
        intent_examples=["send incident notification"],
        agent_capabilities={
            "NotificationAgent": ["notification_delivery", "general"]
        },
    )
    assert second_notification.evidence_count == 2
    assert second_notification.status == WorkflowSkillStatus.CANDIDATE
    assert second_notification.quality.business_outcome_coverage == 0.0


def test_side_effect_skill_promotes_only_with_verified_business_outcomes(tmp_path):
    manager = _manager(tmp_path)
    profile = {
        "intent": "send_incident_notification",
        "task_type": "COMMUNICATION",
        "action": "send",
        "expected_capabilities": ["notification_delivery"],
        "data_scope": ["incident_subscribers"],
        "risk_level": "HIGH",
        "entities": {"incident_id": "INC-42"},
    }
    kwargs = {
        "user_id": "alice",
        "user_query": "Send the incident notification",
        "planning_steps": [
            {
                "agent_name": "NotificationAgent",
                "capability": "notification_delivery",
                "description": "Send the current incident update",
            }
        ],
        "task_profile": profile,
        "intent_examples": ["send incident notification"],
        "outcome_summary": {
            "evidence_schema_version": 1,
            "technical_success": True,
            "business_success": True,
            "business_outcome_coverage": 1.0,
            "steps": [
                {
                    "step_id": "send",
                    "operation_mode": "send",
                    "technical_success": True,
                    "business_success": True,
                    "verification_status": "verified",
                }
            ],
        },
    }

    first = manager.distill(task_id="notification-task-1", **kwargs)
    second = manager.distill(task_id="notification-task-2", **kwargs)

    assert first.status == WorkflowSkillStatus.CANDIDATE
    assert second.status == WorkflowSkillStatus.ACTIVE
    assert second.quality.business_outcome_coverage == 1.0
    assert second.quality.business_success_rate == 1.0


def test_duplicate_evidence_is_idempotent(tmp_path):
    manager = _manager(tmp_path)
    first = _distill_report(manager, "report-task-1")
    duplicate = _distill_report(manager, "report-task-1")

    assert duplicate.skill_id == first.skill_id
    assert duplicate.evidence_count == 1
    assert duplicate.quality.support_count == 1
    assert len(manager.store.list_evidence("alice")) == 1


def test_slot_names_are_normalized_and_bound_from_current_profile(tmp_path):
    manager = _manager(tmp_path)
    source_profile = _profile(
        entities={"Report Month": "2026-06", "Department": "sales"}
    )
    _distill_report(manager, "report-task-1", task_profile=source_profile)
    _distill_report(manager, "report-task-2", task_profile=source_profile)

    match = manager.match(
        user_id="alice",
        query="generate monthly report",
        task_profile=_profile(
            entities={"Report Month": "2026-08", "Department": "support"}
        ),
        available_agents=["MetricsReaderAgent", "ReportWriterAgent"],
    )

    assert match is not None
    assert match.bound_planning_steps[0]["slot_bindings"] == {
        "department": "support",
        "report_month": "2026-08",
    }


def test_structured_matching_rejects_incompatible_task_signals(tmp_path):
    manager = _manager(tmp_path)
    _distill_report(
        manager,
        "report-task-1",
        agent_contracts={"MetricsReaderAgent": "1.0", "ReportWriterAgent": "2.1"},
    )
    card = _distill_report(
        manager,
        "report-task-2",
        agent_contracts={"MetricsReaderAgent": "1.0", "ReportWriterAgent": "2.1"},
    )
    agents = ["MetricsReaderAgent", "ReportWriterAgent"]
    contracts = {"MetricsReaderAgent": "1.0", "ReportWriterAgent": "2.1"}

    matched = manager.match(
        user_id="alice",
        query="generate monthly report",
        task_profile=_profile(entities={"month": "2026-08", "department": "sales"}),
        available_agents=agents,
        agent_contracts=contracts,
    )
    assert matched is not None
    assert matched.skill.skill_id == card.skill_id
    assert matched.applicability_checks["action"] is True
    assert matched.bound_planning_steps[0]["slot_bindings"] == {
        "department": "sales",
        "month": "2026-08",
    }

    incompatible_profiles = [
        _profile(action="send"),
        _profile(capabilities=["spreadsheet_export"]),
        _profile(data_scope=["enterprise_finance"]),
        _profile(risk="HIGH"),
        _profile(missing_fields=["month"], entities={"department": "sales"}),
    ]
    for incompatible in incompatible_profiles:
        assert manager.match(
            user_id="alice",
            query="generate monthly report",
            task_profile=incompatible,
            available_agents=agents,
            agent_contracts=contracts,
        ) is None

    assert manager.match(
        user_id="alice",
        query="generate monthly report",
        task_profile=_profile(),
        available_agents=agents,
        agent_contracts={"MetricsReaderAgent": "1.0", "ReportWriterAgent": "3.0"},
    ) is None
    assert manager.match(
        user_id="alice",
        query="generate monthly report",
        task_profile=_profile(),
        available_agents=agents,
        agent_contracts={"MetricsReaderAgent": "1.0"},
    ) is None


def test_legacy_card_payload_remains_readable():
    legacy = WorkflowSkillCard.model_validate(
        {
            "skill_id": "legacy-1",
            "user_id": "alice",
            "name": "legacy",
            "description": "legacy card",
            "signature": "sig",
            "planning_steps": [{"agent_name": "LegacyAgent"}],
        }
    )

    assert legacy.schema_version == 1
    assert legacy.graph.nodes == []
    assert legacy.applicability.intent == "general_assistance"

"""Unit tests for planning_steps -> TaskGraph conversion (Plan Phase 3, R4).

Also includes a converter+scheduler integration run of the 3-step "王强"
scenario (query -> generate proof -> send email) with a fake executor.
"""

import asyncio

import pytest

from remote_agents.hr_assistant_agent import RemoteHRAssistantAgent
from remote_agents.knowledge_agent import RemoteKnowledgeAgent
from remote_agents.report_agent import RemoteReportAgent
from src.interface.task_graph import TaskGraphValidationError
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.plan_to_task_graph import (
    derive_step_dependencies,
    plan_to_task_graph,
)
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.scheduler import TaskScheduler

WANGQIANG_PLAN = [
    {"agent_name": "RemoteHRAssistantAgent", "title": "查询王强信息", "inputs": []},
    {
        "agent_name": "DocumentGeneratorAgent",
        "title": "生成收入证明",
        "inputs": [
            {
                "parameter_name": "employee_data",
                "source_step": "RemoteHRAssistantAgent",
                "source_output": "person_info",
            }
        ],
    },
    {
        "agent_name": "EmailDispatchAgent",
        "title": "发送邮件",
        "inputs": [
            {
                "parameter_name": "attachment",
                "source_step": "DocumentGeneratorAgent",
                "source_output": "document",
            }
        ],
    },
]

PRODUCES = {
    "RemoteHRAssistantAgent": ["person_info"],
    "DocumentGeneratorAgent": ["document"],
    "EmailDispatchAgent": ["receipt"],
}


def test_converter_derives_dependencies_and_order():
    g = plan_to_task_graph(
        WANGQIANG_PLAN,
        task_id="wangqiang",
        write_agents={"EmailDispatchAgent"},
        agent_produces=PRODUCES,
    )
    smap = g.step_map()
    assert list(smap.keys()) == ["step_1", "step_2", "step_3"]
    assert smap["step_1"].depends_on == []
    assert smap["step_2"].depends_on == ["step_1"]
    assert smap["step_3"].depends_on == ["step_2"]
    assert g.topological_order() == ["step_1", "step_2", "step_3"]


def test_converter_sets_mode_outputs_and_preferred_agent():
    g = plan_to_task_graph(
        WANGQIANG_PLAN,
        task_id="wangqiang",
        write_agents={"EmailDispatchAgent"},
        agent_produces=PRODUCES,
    )
    smap = g.step_map()
    assert smap["step_1"].is_read_only is True
    assert smap["step_3"].is_read_only is False  # email = write
    assert smap["step_1"].expected_outputs == ["person_info"]
    assert smap["step_1"].preferred_resource_id == "RemoteHRAssistantAgent"


def test_converter_explicit_step_id_and_independent_steps():
    plan = [
        {"agent_name": "A", "step_id": "alpha"},
        {"agent_name": "B"},  # no inputs -> independent
    ]
    g = plan_to_task_graph(plan, task_id="t")
    smap = g.step_map()
    assert "alpha" in smap
    assert smap["alpha"].depends_on == []
    assert g.step_map()["step_2"].depends_on == []


def test_converter_preserves_structured_execution_contract():
    graph = plan_to_task_graph(
        [
            {
                "step_id": "send",
                "agent_name": "RemoteEmailDispatchAgent",
                "operation_mode": "send",
                "expected_outputs": ["receipt"],
                "expected_schema_ref": "send_receipt@v1",
                "retry": 3,
                "completion_conditions": ["status == 'SUCCEEDED'"],
                "verification_contract": {
                    "required": True,
                    "method": "provider_receipt",
                },
            }
        ],
        task_id="structured",
    )
    step = graph.step_map()["send"]
    assert step.expected_schema_ref == "send_receipt@v1"
    assert step.verification_contract["required"] is True
    assert step.retry == 3
    assert step.completion_conditions[0].expression == "status == 'SUCCEEDED'"


def test_converter_skips_non_dict_steps():
    g = plan_to_task_graph([None, {"agent_name": "A"}, 42], task_id="t")
    assert list(g.step_map().keys()) == ["step_2"]


# --- Subtask dependency fallback (王强/年假 report scenario regression) ---------

# The Planner leaves `inputs` empty for the autonomous report agent, so the
# report step's dependency on the two upstream queries is lost -> all three run
# in parallel and the report fails (NEEDS_RECONCILIATION). The task profile's
# subtasks already know the correct DAG.
WANGQIANG_LEAVE_PLAN = [
    {"agent_name": "RemoteHRAssistantAgent", "title": "查询王强员工基础信息"},
    {"agent_name": "RemoteKnowledgeAgent", "title": "查询公司年假制度"},
    {"agent_name": "RemoteReportAgent", "title": "生成 Markdown 综合汇总报告"},
]

WANGQIANG_LEAVE_SUBTASKS = [
    {"id": "subtask_1", "depends_on": []},
    {"id": "subtask_2", "depends_on": []},
    {"id": "subtask_3", "depends_on": ["subtask_1", "subtask_2"]},
]


def test_derive_recovers_report_dependency_from_subtasks():
    augmented = derive_step_dependencies(
        WANGQIANG_LEAVE_PLAN, WANGQIANG_LEAVE_SUBTASKS
    )
    assert augmented[0].get("depends_on") in (None, [])
    assert augmented[1].get("depends_on") in (None, [])
    assert augmented[2]["depends_on"] == [
        "RemoteHRAssistantAgent",
        "RemoteKnowledgeAgent",
    ]


def test_converter_uses_subtasks_to_serialize_report_after_queries():
    g = plan_to_task_graph(
        WANGQIANG_LEAVE_PLAN,
        task_id="wq-leave",
        subtasks=WANGQIANG_LEAVE_SUBTASKS,
    )
    smap = g.step_map()
    assert smap["step_1"].depends_on == []
    assert smap["step_2"].depends_on == []
    # step_3 (report) now waits for both upstream queries.
    assert sorted(smap["step_3"].depends_on) == ["step_1", "step_2"]
    order = g.topological_order()
    assert order.index("step_3") > order.index("step_1")
    assert order.index("step_3") > order.index("step_2")


def test_converter_normalizes_single_value_depends_on():
    """A legal single-value ``"depends_on": "step"`` (accepted by upstream
    ``_string_list`` validation) must resolve as ONE edge, never be iterated
    character-by-character and silently dropped."""
    plan = [
        {"agent_name": "A", "step_id": "alpha"},
        {"agent_name": "B", "step_id": "beta", "depends_on": "alpha"},
    ]
    g = plan_to_task_graph(plan, task_id="t")
    assert g.step_map()["beta"].depends_on == ["alpha"]
    assert g.topological_order() == ["alpha", "beta"]


def test_derive_accepts_single_value_subtask_depends_on():
    subtasks = [
        {"id": "subtask_1", "depends_on": []},
        {"id": "subtask_2", "depends_on": []},
        {"id": "subtask_3", "depends_on": "subtask_1"},
    ]
    augmented = derive_step_dependencies(WANGQIANG_LEAVE_PLAN, subtasks)
    assert augmented[2]["depends_on"] == ["RemoteHRAssistantAgent"]


def test_converter_builds_contract_fan_in_dependencies():
    contracts = {
        "RemoteHRAssistantAgent": RemoteHRAssistantAgent().contract,
        "RemoteKnowledgeAgent": RemoteKnowledgeAgent().contract,
        "RemoteReportAgent": RemoteReportAgent().contract,
    }
    plan = [
        {"step_id": "hr", "agent_name": "RemoteHRAssistantAgent"},
        {"step_id": "knowledge", "agent_name": "RemoteKnowledgeAgent"},
        {
            "step_id": "report",
            "agent_name": "RemoteReportAgent",
            "inputs": [
                {
                    "parameter_name": "report.sources",
                    "source_artifacts": [
                        {
                            "source_step": "RemoteHRAssistantAgent",
                            "source_output": "employee.info",
                        },
                        {
                            "source_step": "RemoteKnowledgeAgent",
                            "source_output": "policy.info",
                        },
                    ],
                    "assembly": {"schema_ref": "report.sources@v1"},
                }
            ],
        },
    ]
    graph = plan_to_task_graph(
        plan,
        task_id="contract-fan-in",
        agent_contracts=contracts,
    )
    report = graph.step_map()["report"]
    assert report.depends_on == ["hr", "knowledge"]
    assert report.expected_outputs == ["report.markdown"]
    assert report.expected_schema_refs == {
        "report.markdown": "report.markdown@v1"
    }
    assert report.agent_contract.contract_version == "1.0"


def test_converter_prefers_trusted_registry_contract_over_planner_contract():
    trusted = RemoteKnowledgeAgent().contract
    untrusted = RemoteReportAgent().contract
    graph = plan_to_task_graph(
        [
            {
                "agent_name": "RemoteKnowledgeAgent",
                "agent_contract": untrusted.model_dump(mode="json"),
            }
        ],
        task_id="trusted-contract",
        agent_contracts={"RemoteKnowledgeAgent": trusted},
    )
    step = graph.steps[0]

    assert [ref.name for ref in step.agent_contract.produces] == ["policy.info"]
    assert step.expected_outputs == ["policy.info"]


def test_converter_ignores_planner_only_contract():
    """Planner output is untrusted: a step-level agent_contract with no
    matching trusted registry contract must be dropped entirely, never
    injected into the TaskStep."""
    untrusted = RemoteReportAgent().contract
    graph = plan_to_task_graph(
        [
            {
                "agent_name": "SomeUnregisteredAgent",
                "agent_contract": untrusted.model_dump(mode="json"),
            }
        ],
        task_id="planner-injected-contract",
    )
    step = graph.steps[0]

    assert step.agent_contract is None
    assert step.expected_schema_refs == {}


def test_converter_rejects_outputs_outside_trusted_contract():
    with pytest.raises(
        TaskGraphValidationError,
        match="outputs not present in trusted Agent contract",
    ):
        plan_to_task_graph(
            [
                {
                    "agent_name": "RemoteKnowledgeAgent",
                    "expected_outputs": ["fake.output"],
                }
            ],
            task_id="trusted-contract-outputs",
            agent_contracts={
                "RemoteKnowledgeAgent": RemoteKnowledgeAgent().contract
            },
        )


def test_derive_does_not_override_explicit_planner_edges():
    plan = [
        {"agent_name": "A"},
        {
            "agent_name": "B",
            "inputs": [{"parameter_name": "x", "source_step": "A"}],
        },
    ]
    # subtasks would suggest B depends on A too, but the Planner already said so;
    # the plan must be returned untouched (identity).
    subtasks = [
        {"id": "s1", "depends_on": []},
        {"id": "s2", "depends_on": ["s1"]},
    ]
    assert derive_step_dependencies(plan, subtasks) is plan


def test_derive_skips_when_counts_misaligned():
    plan = [{"agent_name": "A"}, {"agent_name": "B"}]
    subtasks = [{"id": "s1", "depends_on": []}]  # only one subtask
    assert derive_step_dependencies(plan, subtasks) is plan


def test_derive_skips_forward_and_unknown_edges():
    plan = [{"agent_name": "A"}, {"agent_name": "B"}]
    # s1 depends on a later (s2) and an unknown (s9) subtask -> both skipped,
    # yielding no valid backward edge, so the plan is returned unchanged.
    subtasks = [
        {"id": "s1", "depends_on": ["s2", "s9"]},
        {"id": "s2", "depends_on": []},
    ]
    assert derive_step_dependencies(plan, subtasks) is plan


def test_derive_noop_without_subtasks():
    assert derive_step_dependencies(WANGQIANG_LEAVE_PLAN, None) is WANGQIANG_LEAVE_PLAN
    assert derive_step_dependencies(WANGQIANG_LEAVE_PLAN, []) is WANGQIANG_LEAVE_PLAN


class _Fake:
    def __init__(self):
        self.calls: list[str] = []
        self.received: dict[str, dict] = {}

    async def __call__(self, *, step, selected_agent, inputs, context):
        self.calls.append(step.step_id)
        self.received[step.step_id] = {
            "agent": selected_agent, "inputs": dict(inputs)}
        # produce a payload keyed by this step's primary expected output
        name = step.expected_outputs[0] if step.expected_outputs else "out"
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={name: f"{step.step_id}-data"})


def test_converted_graph_runs_end_to_end_serially():
    g = plan_to_task_graph(
        WANGQIANG_PLAN,
        task_id="wangqiang",
        subject="user_123",
        write_agents={"EmailDispatchAgent"},
        agent_produces=PRODUCES,
    )
    fake = _Fake()
    sched = TaskScheduler(
        execute_step=fake, routing_provider=StubRoutingProvider())
    results = asyncio.run(sched.run(g, context={"subject": "user_123"}))

    assert fake.calls == ["step_1", "step_2", "step_3"]
    assert all(r.is_success for r in results.values())
    # routing selected the preferred agent per step
    assert fake.received["step_1"]["agent"] == "RemoteHRAssistantAgent"
    assert fake.received["step_3"]["agent"] == "EmailDispatchAgent"
    # email step received the document produced upstream
    assert fake.received["step_3"]["inputs"]["attachment"] == {
        "document": "step_2-data"}


def test_converter_output_validates_as_dag():
    g = plan_to_task_graph(WANGQIANG_PLAN, task_id="t")
    # Should not raise
    assert g.validate_dag() is g


def test_converter_empty_plan_is_valid_empty_graph():
    g = plan_to_task_graph([], task_id="t")
    assert g.steps == []
    try:
        g.validate_dag()
    except TaskGraphValidationError:  # pragma: no cover
        raise AssertionError("empty graph should be valid")


# --------------------------------------------------------------------------- #
# Operation-mode classification from S-ABAC config (P0-4, T7 / T8)
# --------------------------------------------------------------------------- #
def test_t7_email_step_is_classified_as_send_not_read():
    """An email dispatch step must never be classified read-only."""
    g = plan_to_task_graph(
        [{"agent_name": "RemoteEmailDispatchAgent", "title": "send mail"}],
        task_id="t",
    )
    step = g.step_map()["step_1"]
    assert step.operation_mode == "send"
    assert step.is_read_only is False


def test_pure_query_agent_stays_read_only():
    g = plan_to_task_graph(
        [{"agent_name": "RemoteHRAssistantAgent", "title": "query"}], task_id="t"
    )
    assert g.step_map()["step_1"].is_read_only is True


def test_unregistered_agent_is_unknown_not_read():
    """An unregistered agent must be 'unknown' (never defaulted to read)."""
    g = plan_to_task_graph([{"agent_name": "MysteryAgent"}], task_id="t")
    assert g.step_map()["step_1"].operation_mode == "unknown"


def test_t8_two_write_steps_are_not_read_only():
    """Two side-effect steps must both be non-read so the scheduler serializes
    them instead of running them as parallel read-only work."""
    g = plan_to_task_graph(
        [
            {"agent_name": "RemoteEmailDispatchAgent"},
            {"agent_name": "RemoteMeetingManagerAgent"},
        ],
        task_id="t",
    )
    smap = g.step_map()
    assert smap["step_1"].is_read_only is False
    assert smap["step_2"].is_read_only is False


# --------------------------------------------------------------------------- #
# Planner output is untrusted: an explicit mode may only RAISE risk (C2)
# --------------------------------------------------------------------------- #
def test_planner_explicit_read_cannot_downgrade_send():
    """A faked ``operation_mode: read`` on an email agent must stay ``send``."""
    g = plan_to_task_graph(
        [{"agent_name": "RemoteEmailDispatchAgent", "operation_mode": "read"}],
        task_id="t",
    )
    step = g.step_map()["step_1"]
    assert step.operation_mode == "send"
    assert step.is_read_only is False
    assert step.operation_mode_source == "agent_config"
    assert "not lowered" in step.operation_mode_reason


def test_planner_explicit_read_cannot_rescue_unregistered_to_read():
    """An unregistered agent stays ``unknown`` even if the plan claims read."""
    g = plan_to_task_graph(
        [{"agent_name": "MysteryAgent", "operation_mode": "read"}], task_id="t"
    )
    step = g.step_map()["step_1"]
    assert step.operation_mode == "unknown"


def test_planner_can_escalate_read_to_write():
    """The plan MAY raise a read-only agent to a higher-risk write."""
    g = plan_to_task_graph(
        [{"agent_name": "RemoteHRAssistantAgent", "operation_mode": "write"}],
        task_id="t",
    )
    step = g.step_map()["step_1"]
    assert step.operation_mode == "write"
    assert step.operation_mode_source == "planner_upgrade"


def test_business_risk_agent_with_export_is_write():
    """A multi-mode agent that includes a write mode (export) is classified write."""
    g = plan_to_task_graph(
        [{"agent_name": "RemoteBusinessRiskAgent"}], task_id="t"
    )
    assert g.step_map()["step_1"].operation_mode == "write"

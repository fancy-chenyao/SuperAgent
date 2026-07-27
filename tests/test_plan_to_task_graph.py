"""Unit tests for planning_steps -> TaskGraph conversion (Plan Phase 3, R4).

Also includes a converter+scheduler integration run of the 3-step "王强"
scenario (query -> generate proof -> send email) with a fake executor.
"""

import asyncio

import pytest

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


def test_converter_resolves_step_and_subtask_references_before_building_edges():
    graph = plan_to_task_graph(
        [
            {
                "step_id": "consumer",
                "subtask_ids": ["subtask_consumer"],
                "agent_name": "ConsumerAgent",
                "depends_on": ["subtask_source"],
            },
            {
                "step_id": "source",
                "subtask_ids": ["subtask_source"],
                "agent_name": "SourceAgent",
            },
        ],
        task_id="forward-reference",
    )

    assert graph.step_map()["consumer"].depends_on == ["source"]
    assert graph.topological_order() == ["source", "consumer"]


def test_converter_rejects_unknown_dependency_instead_of_dropping_it():
    with pytest.raises(
        TaskGraphValidationError,
        match="depends on unknown step 'missing_step'",
    ):
        plan_to_task_graph(
            [
                {
                    "step_id": "consumer",
                    "agent_name": "ConsumerAgent",
                    "depends_on": ["missing_step"],
                }
            ],
            task_id="unknown-dependency",
        )


def test_converter_rejects_unknown_input_source_instead_of_running_early():
    with pytest.raises(
        TaskGraphValidationError,
        match="depends on unknown step 'missing_source'",
    ):
        plan_to_task_graph(
            [
                {
                    "step_id": "consumer",
                    "agent_name": "ConsumerAgent",
                    "inputs": [
                        {
                            "parameter_name": "payload",
                            "source_step": "missing_source",
                        }
                    ],
                }
            ],
            task_id="unknown-input-source",
        )


def test_converter_keeps_legacy_agent_reference_to_most_recent_prior_step():
    graph = plan_to_task_graph(
        [
            {"step_id": "query_1", "agent_name": "SharedAgent"},
            {"step_id": "query_2", "agent_name": "SharedAgent"},
            {
                "step_id": "report",
                "agent_name": "ReportAgent",
                "depends_on": ["SharedAgent"],
            },
        ],
        task_id="legacy-agent-reference",
    )

    assert graph.step_map()["report"].depends_on == ["query_2"]


def test_converter_normalizes_single_subtask_and_intent_values():
    graph = plan_to_task_graph(
        [
            {
                "step_id": "query",
                "agent_name": "RemoteHRAssistantAgent",
                "subtask_ids": "subtask_1",
                "intents": "employee_information_query",
            }
        ],
        task_id="normalized-list-fields",
    )

    step = graph.step_map()["query"]
    assert step.subtask_ids == ["subtask_1"]
    assert step.intents == ["employee_information_query"]


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

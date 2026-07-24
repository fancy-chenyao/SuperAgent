"""Unit tests for planning_steps -> TaskGraph conversion (Plan Phase 3, R4).

Also includes a converter+scheduler integration run of the 3-step "王强"
scenario (query -> generate proof -> send email) with a fake executor.
"""

import asyncio

from src.interface.task_graph import TaskGraphValidationError
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.plan_to_task_graph import plan_to_task_graph
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

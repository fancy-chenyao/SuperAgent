"""Unit tests for TaskGraph structural validation (Plan Phase 1).

Isolated: imports only pure interface types (pydantic), no workflow stack.
"""

import pytest

from src.interface.task_graph import (
    CompletionCondition,
    TaskGraph,
    TaskGraphValidationError,
    TaskSpec,
    TaskStep,
)


def _step(step_id: str, deps=None, mode: str = "read") -> TaskStep:
    return TaskStep(step_id=step_id, depends_on=deps or [], operation_mode=mode)


def _graph(*steps: TaskStep, task_id: str = "t1") -> TaskGraph:
    return TaskGraph(spec=TaskSpec(task_id=task_id), steps=list(steps))


def test_valid_linear_graph_passes():
    g = _graph(_step("a"), _step("b", ["a"]), _step("c", ["b"]))
    assert g.validate_dag() is g
    assert set(g.step_map().keys()) == {"a", "b", "c"}
    assert g.topological_order() == ["a", "b", "c"]


def test_valid_diamond_graph_topo_order():
    # a -> {b, c} -> d
    g = _graph(
        _step("a"),
        _step("b", ["a"]),
        _step("c", ["a"]),
        _step("d", ["b", "c"]),
    )
    order = g.topological_order()
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_duplicate_step_id_rejected():
    g = _graph(_step("a"), _step("a"))
    with pytest.raises(TaskGraphValidationError, match="duplicate"):
        g.validate_dag()


def test_missing_dependency_rejected():
    g = _graph(_step("a", ["ghost"]))
    with pytest.raises(TaskGraphValidationError, match="unknown step"):
        g.validate_dag()


def test_self_dependency_rejected():
    g = _graph(_step("a", ["a"]))
    with pytest.raises(TaskGraphValidationError, match="itself"):
        g.validate_dag()


def test_cycle_rejected():
    g = _graph(_step("a", ["b"]), _step("b", ["a"]))
    with pytest.raises(TaskGraphValidationError, match="cycle"):
        g.validate_dag()


def test_three_node_cycle_rejected():
    g = _graph(_step("a", ["c"]), _step("b", ["a"]), _step("c", ["b"]))
    with pytest.raises(TaskGraphValidationError, match="cycle"):
        g.validate_dag()


def test_ready_steps_frontier():
    g = _graph(_step("a"), _step("b", ["a"]), _step("c", ["a"]))
    assert set(g.ready_steps(set())) == {"a"}
    assert set(g.ready_steps({"a"})) == {"b", "c"}
    assert g.ready_steps({"a", "b", "c"}) == []


def test_step_read_only_flag():
    assert _step("r").is_read_only is True
    assert _step("w", mode="write").is_read_only is False


def test_completion_condition_and_step_extra_fields():
    step = TaskStep(
        step_id="s1",
        required_capabilities=["hr.query"],
        expected_outputs=["person_info"],
        completion_conditions=[CompletionCondition(expression="outputs.person_info != null")],
        resource_locks=["mailbox"],
        operation_mode="write",
        risk_level="HIGH",
        timeout=30.0,
        retry=2,
        preferred_resource_id="RemoteHRAssistantAgent",
    )
    assert step.completion_conditions[0].expression.startswith("outputs")
    assert step.resource_locks == ["mailbox"]
    assert step.retry == 2
    assert step.is_read_only is False

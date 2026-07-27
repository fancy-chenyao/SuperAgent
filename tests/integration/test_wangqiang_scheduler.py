r"""End-to-end integration test: 王强 scenario via the TaskGraph scheduler (Plan Phase 3/4).

This drives the REAL execution path — ``run_scheduler_workflow`` with the real
``execute_step`` (agent_manager + execute_agent) against the remote demo agents —
using a hand-fed 王强 TaskGraph and stub routing (so routing is deterministic and
doesn't require an extra LLM round-trip).

It is DEFAULT-SKIPPED. It only runs when explicitly opted in AND all prerequisites
are met; otherwise it skips with an actionable message (never a false failure).

How to run (in the `superagent` conda env)
------------------------------------------
1. Configure `.env` (at least `REMOTE_API_KEY`, `BASIC_API_KEY`).
2. Start the remote demo services in separate terminals:
       python mock_remote_registry.py     # registry on :8010/:8011 resources
       python mock_remote_agent.py         # remote agent server (:8010) + tools (:8011)
3. Run only this test with the opt-in flag:
       set RUN_INTEGRATION=1
       D:\develop\condaenvs\superagent\python.exe -m pytest tests/integration/test_wangqiang_scheduler.py -o "addopts=-v" -p no:cacheprovider
   The 3-step variant that actually dispatches an email additionally requires:
       set RUN_INTEGRATION_EMAIL=1
"""

import asyncio
import os
import urllib.request

import pytest

pytestmark = pytest.mark.integration

REMOTE_HEALTH_URL = "http://127.0.0.1:8010/health"
HR_AGENT = "RemoteHRAssistantAgent"
DOC_AGENT = "RemoteDocumentGeneratorAgent"
EMAIL_AGENT = "RemoteEmailDispatchAgent"


# --------------------------------------------------------------------------- #
# Prerequisite gating (skip, never fail, when the environment isn't ready)
# --------------------------------------------------------------------------- #
def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


def _deps_available() -> bool:
    import importlib.util as u

    return all(u.find_spec(m) is not None for m in ("langchain_core", "langgraph"))


def _remote_server_up(url: str = REMOTE_HEALTH_URL, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost demo
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def _llm_key_present() -> bool:
    return bool(os.getenv("REMOTE_API_KEY") or os.getenv("BASIC_API_KEY"))


def _require_prerequisites(email: bool = False) -> None:
    """Skip unless opted in and every prerequisite is satisfied."""
    _load_env()

    if not os.getenv("RUN_INTEGRATION"):
        pytest.skip("集成测试默认跳过：设 RUN_INTEGRATION=1 并满足前提后运行")
    if email and not os.getenv("RUN_INTEGRATION_EMAIL"):
        pytest.skip("发邮件步骤默认跳过（避免真实发送）：额外设 RUN_INTEGRATION_EMAIL=1 后运行")

    reasons = []
    if not _deps_available():
        reasons.append("缺少 langchain_core/langgraph（请用 superagent 环境）")
    if not _llm_key_present():
        reasons.append("未配置 REMOTE_API_KEY/BASIC_API_KEY（远程 Agent 调 LLM 需要）")
    if not _remote_server_up():
        reasons.append(
            f"远程 Agent 服务不可达（{REMOTE_HEALTH_URL}）：先运行 "
            "mock_remote_registry.py 与 mock_remote_agent.py"
        )
    if reasons:
        pytest.skip("集成前提未满足：" + "；".join(reasons))


# --------------------------------------------------------------------------- #
# 王强 TaskGraph (hand-fed, per Plan Phase-3 verification)
# --------------------------------------------------------------------------- #
def _wangqiang_graph(include_email: bool):
    from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep

    steps = [
        TaskStep(
            step_id="step_1",
            agent_name=HR_AGENT,
            preferred_resource_id=HR_AGENT,
            expected_outputs=["employee.info"],
            operation_mode="read",
        ),
        TaskStep(
            step_id="step_2",
            depends_on=["step_1"],
            agent_name=DOC_AGENT,
            preferred_resource_id=DOC_AGENT,
            expected_outputs=["document"],
            operation_mode="read",
            input_bindings=[
                {
                    "parameter_name": "employee_data",
                    "source_step": HR_AGENT,
                    "source_output": "employee.info",
                }
            ],
        ),
    ]
    if include_email:
        steps.append(
            TaskStep(
                step_id="step_3",
                depends_on=["step_2"],
                agent_name=EMAIL_AGENT,
                preferred_resource_id=EMAIL_AGENT,
                expected_outputs=["receipt"],
                operation_mode="write",
                resource_locks=["mailbox"],
                input_bindings=[
                    {
                        "parameter_name": "attachment",
                        "source_step": DOC_AGENT,
                        "source_output": "document",
                    }
                ],
            )
        )
    return TaskGraph(
        spec=TaskSpec(task_id="wangqiang-integration",
                      subject="integration_user"),
        steps=steps,
    )


def _wangqiang_state(include_email: bool) -> dict:
    query = "帮我查询王强的收入信息，并生成一份收入证明" + ("，然后发邮件" if include_email else "")
    return {
        "workflow_id": "wf-wangqiang-int",
        "user_id": "integration_user",
        "USER_QUERY": query,
        "original_user_query": query,
        "messages": [{"role": "user", "content": query}],
        "workflow_mode": "production",
        "task_graph": _wangqiang_graph(include_email),
    }


def _disable_s_abac(monkeypatch) -> None:
    """Turn S-ABAC off for the duration of the test (auto-restored on teardown).

    This test targets the Scheduler / remote-Agent / Artifact data flow, NOT the
    security policy. Its synthetic subject (``integration_user``) is deliberately
    not a configured demo user, so the S-ABAC layers would otherwise fail the run
    regardless of policy config. We neutralize BOTH enforcement points so the test
    never breaks on demo-user config changes:

    1. ``enforce_agent_dispatch`` -- its ungated ``subject_for_user`` pre-check
       raises ``UnknownSecurityUserError`` before the ``S_ABAC_ENABLED`` gate is
       reached, so flipping the flag alone is insufficient.
    2. ``PolicyEngineArtifactGuard.can_read`` -- the fan-in read guard denies both
       an unknown user (S-ABAC on) AND HIGH/CRITICAL artifacts (S-ABAC off,
       fail-closed), so it must be neutralized to the permissive ``AllowAllGuard``
       behavior for this data-flow test.

    ``monkeypatch`` restores every patch automatically after the test.
    """
    import src.security.enforcement as enforcement
    import src.orchestration.artifact_guard as artifact_guard

    monkeypatch.setattr(enforcement, "S_ABAC_ENABLED", False, raising=False)

    async def _noop_enforce(agent, context):  # noqa: ANN001 - mirrors real signature
        return {"allowed": True, "reason": "S-ABAC disabled for integration test"}

    monkeypatch.setattr(enforcement, "enforce_agent_dispatch", _noop_enforce)

    monkeypatch.setattr(
        artifact_guard.PolicyEngineArtifactGuard,
        "can_read",
        lambda self, **kwargs: True,
    )


def _run_scenario(monkeypatch, include_email: bool) -> tuple[list, dict]:
    import config.global_variables as g
    from src.manager import agent_manager
    from src.orchestration.providers import StubRoutingProvider
    from src.orchestration.runtime import run_scheduler_workflow

    # Enable the Phase-3 flag for fidelity (run_scheduler_workflow is called directly).
    monkeypatch.setattr(
        g, "orchestration_scheduler_enabled", True, raising=False)
    # Keep the test focused on data flow, not on demo-user security config.
    _disable_s_abac(monkeypatch)

    state = _wangqiang_state(include_email)
    required = [HR_AGENT, DOC_AGENT] + ([EMAIL_AGENT] if include_email else [])

    async def _run():
        try:
            await agent_manager.ensure_initialized()
            for name in required:
                if await agent_manager.agent_registry.get(name) is None:
                    pytest.skip(
                        f"注册表缺少 Agent：{name}（需先同步 mock_remote_registry.json）")
        except pytest.skip.Exception:
            raise
        except Exception as exc:  # noqa: BLE001 - init failure -> skip, not fail
            pytest.skip(f"agent_manager 初始化失败，跳过：{exc}")

        events = []
        async for ev in run_scheduler_workflow(
            state,
            task_id="task-wangqiang-int",
            routing_provider=StubRoutingProvider(),  # 固定按 preferred_resource_id 选 Agent
        ):
            events.append(ev)
        return events

    events = asyncio.run(_run())
    return events, state


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_wangqiang_read_only_chain_end_to_end(monkeypatch):
    """查询 -> 生成收入证明（只读链路，无外部副作用）。"""
    _require_prerequisites(email=False)
    events, state = _run_scenario(monkeypatch, include_email=False)

    assert events[0]["event"] == "start_of_workflow"
    assert events[-1]["event"] == "end_of_workflow"
    assert events[-1]["data"]["status"] == "SUCCEEDED", (
        f"存在失败步骤：{events[-1]['data'].get('failed_steps')}"
    )
    assert state["completed_steps"] == ["step_1", "step_2"]
    assert set(state["step_results"].keys()) == {"step_1", "step_2"}


def test_wangqiang_full_chain_with_email(monkeypatch):
    """查询 -> 生成收入证明 -> 发邮件（含写/副作用步；额外 opt-in）。"""
    _require_prerequisites(email=True)
    events, state = _run_scenario(monkeypatch, include_email=True)

    assert events[-1]["event"] == "end_of_workflow"
    assert events[-1]["data"]["status"] == "SUCCEEDED", (
        f"存在失败步骤：{events[-1]['data'].get('failed_steps')}"
    )
    assert state["completed_steps"] == ["step_1", "step_2", "step_3"]
    # 邮件步为写操作，产出回执类结果
    assert "step_3" in state["step_results"]

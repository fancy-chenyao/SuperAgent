"""Offline, deterministic end-to-end demo: the single strong governance scenario.

Scenario (all fictional / mock data; no network, no LLM, no real identity system):

    hr_manager 发起工资汇总任务
    -> TaskGraph: s_query (HR agent, read) -> s_report (reporter, read)
    -> s_query produces an owned artifact (mock employee 王强 / mock salary)
    -> s_report reads it via ArtifactRef and produces a report (no salary body)
    -> engineer tries to read the same artifact -> ownership guard denies
    -> audit log records the deny (metadata only, never the payload)

This drives the REAL scheduler runtime bridge (``run_scheduler_workflow``) with a
Fake Executor + stub routing, so it is fully offline and repeatable. It proves the
acceptance points from the prototype closeout plan (phase 4):

- clear TaskGraph dependency; upstream output flows via ArtifactRef;
- the checkpoint/state carries NO salary plaintext (payload lives in the
  protected payload store, not the checkpoint);
- owner read succeeds; non-owner read fails closed;
- the audit log has no sensitive body;
- a completed step is not re-executed on resume;
- a tampered payload fails recovery;
- the final status is an explicit terminal verdict (not merely "stream ended").
"""

import asyncio
import json

import pytest

import src.service.env as env
from src.interface.artifact import ArtifactRef
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.artifact_guard import PolicyEngineArtifactGuard
from src.orchestration.artifact_payload_store import ArtifactPayloadStore
from src.orchestration.audit import read_audit_records
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.resolver import ArtifactAccessDenied, ArtifactResolver
from src.orchestration.runtime import run_scheduler_workflow
from src.orchestration.store import ArtifactStore

_HR = "RemoteHRAssistantAgent"
_REPORTER = "reporter"
_TASK_ID = "task-demo"

# Fictional data only (mock employee + simulated salary).
_MOCK_SALARY = {"employee": "王强", "salary": 42000, "currency": "CNY"}
_SECRETS = ("42000", "王强")  # must never appear in checkpoint/state or audit


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_PAYLOAD_STORE_DIR",
                       str(tmp_path / "artifacts"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    monkeypatch.setenv("ARTIFACT_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    # Prototype default: S-ABAC off -> the ownership gate is the deciding control
    # (it denies cross-user reads regardless of the policy engine).
    monkeypatch.setattr(env, "S_ABAC_ENABLED", False)


def _salary_graph():
    return TaskGraph(
        spec=TaskSpec(task_id=_TASK_ID, subject="hr_manager"),
        steps=[
            TaskStep(
                step_id="s_query", agent_name=_HR, preferred_resource_id=_HR,
                operation_mode="read", expected_outputs=["salary_summary"],
            ),
            TaskStep(
                step_id="s_report", agent_name=_REPORTER, preferred_resource_id=_REPORTER,
                operation_mode="read", depends_on=["s_query"],
                input_bindings=[
                    {"parameter_name": "summary", "source_step": _HR,
                     "source_output": "salary_summary"}
                ],
            ),
        ],
    )


class _DemoExecutor:
    """s_query returns the mock salary; s_report returns a body-free report.

    ``fail_report_times`` makes the reporter fail its first N invocations so the
    resume path (step already done is not re-run) can be exercised.
    """

    def __init__(self, *, fail_report_times: int = 0):
        self.calls: list[str] = []
        self.received: dict[str, dict] = {}
        self._fail_report = fail_report_times

    async def __call__(self, *, step, selected_agent, inputs, context):
        self.calls.append(step.step_id)
        self.received[step.step_id] = dict(inputs)
        if step.step_id == "s_query":
            return ExecuteResult(status=ExecutionStatus.SUCCESS, result=dict(_MOCK_SALARY))
        # s_report
        if self._fail_report > 0:
            self._fail_report -= 1
            return ExecuteResult(status=ExecutionStatus.FAILED, error="reporter transient")
        # The report references only a count -- never the salary body.
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"report_ok": True, "n": 1})


def _state(user_id="hr_manager"):
    return {
        "workflow_id": "wf-demo",
        "user_id": user_id,
        "task_graph": _salary_graph(),
        "USER_QUERY": "汇总员工工资信息",
        # -> INTERNAL artifacts (owner reads succeed offline)
        "risk_profile": "LOW",
        "messages": [],
    }


def _drive(state, execute, task_id=_TASK_ID, checkpoint_manager=None):
    async def _go():
        events = []
        async for ev in run_scheduler_workflow(
            state, task_id=task_id, execute_step=execute,
            routing_provider=StubRoutingProvider(),
            checkpoint_manager=checkpoint_manager,
        ):
            events.append(ev)
        return events

    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Data collaboration + explicit terminal verdict
# --------------------------------------------------------------------------- #
def test_demo_dependency_and_artifact_ref_passing():
    graph = _salary_graph()
    # (1) clear dependency in the TaskGraph.
    assert graph.step_map()["s_report"].depends_on == ["s_query"]

    execute = _DemoExecutor()
    state = _state()
    events = _drive(state, execute)

    # (2) upstream output flowed to the consumer via ArtifactRef resolution.
    assert execute.received["s_report"]["summary"] == _MOCK_SALARY
    # explicit terminal verdict (not merely "SSE ended").
    end = events[-1]
    assert end["event"] == "end_of_workflow"
    assert end["data"]["status"] == "SUCCEEDED"
    assert state["completed_steps"] == ["s_query", "s_report"]


def test_demo_checkpoint_state_has_no_salary_plaintext():
    execute = _DemoExecutor()
    state = _state()
    _drive(state, execute)

    # The checkpoint payload is exactly what goes onto ``state`` (refs + a
    # de-sensitized artifact index) -- never the salary body.
    serialized = json.dumps(
        {"step_results": state.get("step_results"),
         "artifacts": state.get("artifacts"),
         "completed_steps": state.get("completed_steps")},
        ensure_ascii=False, default=str,
    )
    for secret in _SECRETS:
        assert secret not in serialized


def test_demo_audit_records_have_no_payload_or_uri():
    execute = _DemoExecutor()
    state = _state()
    _drive(state, execute)

    records = read_audit_records()
    assert records  # the owner read of s_query's output was audited
    blob = json.dumps(records, ensure_ascii=False, default=str)
    for secret in _SECRETS:
        assert secret not in blob
    for r in records:
        assert "payload" not in r and "uri" not in r


# --------------------------------------------------------------------------- #
# Permission governance: owner allowed, non-owner denied + audited
# --------------------------------------------------------------------------- #
def _rebuild_store_and_query_ref(state):
    """Rebuild the ArtifactStore from the persisted payload index and return the
    (store, ref) for s_query's produced artifact."""
    payload_store = ArtifactPayloadStore(_TASK_ID)
    payloads = payload_store.load_index(state["artifacts"])
    store = ArtifactStore()
    store.load_state(payloads)
    ref_dict = state["step_results"]["s_query"]["outputs"]["salary_summary"]
    return store, ArtifactRef(**ref_dict)


def test_demo_owner_reads_but_engineer_is_denied_and_audited():
    execute = _DemoExecutor()
    state = _state()
    _drive(state, execute)  # owner (hr_manager) read already succeeded in-run

    store, ref = _rebuild_store_and_query_ref(state)
    resolver = ArtifactResolver(store, guard=PolicyEngineArtifactGuard())

    # Owner can read the artifact it produced.
    assert resolver.resolve(ref, subject="hr_manager") == _MOCK_SALARY
    # A non-owner (engineer) is denied by the ownership gate.
    with pytest.raises(ArtifactAccessDenied):
        resolver.resolve(ref, subject="engineer")

    # The deny is recorded in the audit log (metadata only).
    denies = [r for r in read_audit_records()
              if r["decision"] == "deny" and r["subject"] == "engineer"]
    assert denies
    assert all("payload" not in r for r in denies)


# --------------------------------------------------------------------------- #
# Persistence + recovery
# --------------------------------------------------------------------------- #
def test_demo_resume_from_checkpoint_does_not_reexecute_completed_step(tmp_path):
    """Proves crash recovery from the DURABLE checkpoint (not a reused in-memory
    state): after s_query completes and the process 'crashes', a fresh state
    rebuilt from the checkpoint must already record s_query as completed, so the
    resumed run never re-executes it."""
    from src.robust.checkpoint import CheckpointManager

    cm = CheckpointManager(base_dir=tmp_path / "checkpoints")

    # Run 1: reporter fails once -> s_query completes and is checkpointed;
    # workflow ends PARTIAL_FAILED. We then DISCARD this run's in-memory state
    # to simulate a process crash.
    exec1 = _DemoExecutor(fail_report_times=1)
    state1 = _state()
    events1 = _drive(state1, exec1, checkpoint_manager=cm)
    assert events1[-1]["data"]["status"] == "PARTIAL_FAILED"
    assert exec1.calls.count("s_query") == 1

    # Recover from the DURABLE checkpoint: it must already record the completion
    # (this is exactly what the save-order bug used to get wrong).
    ckpt = cm.load_checkpoint(task_id=_TASK_ID)
    assert "s_query" in (ckpt.state.get("completed_steps") or [])  # (#1)
    recovered_state = dict(ckpt.state)  # a brand-new state, as after a restart

    # Run 2 with a FRESH executor and the recovered state: s_query must NOT run
    # again; s_report resumes against the restored upstream artifact.
    exec2 = _DemoExecutor()
    events2 = _drive(recovered_state, exec2, checkpoint_manager=cm)
    assert events2[-1]["data"]["status"] == "SUCCEEDED"
    assert "s_query" not in exec2.calls  # never re-executed after recovery
    # (#2) total s_query executions across the crash+recovery stays exactly 1.
    assert exec1.calls.count("s_query") + exec2.calls.count("s_query") == 1
    assert exec2.received["s_report"]["summary"] == _MOCK_SALARY


def test_demo_tampered_payload_fails_recovery():
    execute = _DemoExecutor()
    state = _state()
    _drive(state, execute)
    assert state["completed_steps"] == ["s_query", "s_report"]

    # Tamper the persisted payload on disk (keep the stored checksum so the
    # integrity check detects the modification).
    payload_dir = ArtifactPayloadStore(_TASK_ID)._dir
    files = list(payload_dir.glob("*.json"))
    assert files
    tampered = False
    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        payload = data.get("payload")
        if isinstance(payload, dict) and "salary" in payload:
            payload["salary"] = 999999  # mutate; leave data["checksum"] stale
            fp.write_text(json.dumps(data, ensure_ascii=False),
                          encoding="utf-8")
            tampered = True
    assert tampered

    # Resume: restoring the tampered artifact must fail closed, never continue
    # with wrong upstream data.
    events = _drive(state, execute)
    end = events[-1]
    assert end["event"] == "end_of_workflow"
    assert end["data"]["status"] == "FAILED"
    assert end["data"].get("reason") == "artifact_store_corruption"


class _FailSecondCheckpoint:
    """Checkpoint manager that succeeds for s_query but FAILS on the second save
    (s_report), to exercise the crash-safe commit ordering."""

    def __init__(self):
        self.calls = 0

    def save_checkpoint(self, **kwargs):
        self.calls += 1
        if self.calls >= 2:
            raise IOError("checkpoint disk full")
        return "ckpt-0"


def test_demo_checkpoint_write_failure_keeps_state_and_not_succeeded():
    """(#3) When a step's checkpoint write fails, the live state's three fields
    (step_results / artifacts / completed_steps) must stay exactly as they were
    after the previous durable commit, and the terminal status must NOT be
    SUCCEEDED."""
    cm = _FailSecondCheckpoint()
    execute = _DemoExecutor()  # both steps succeed at the executor level
    state = _state()
    events = _drive(state, execute, checkpoint_manager=cm)

    end = events[-1]
    assert end["event"] == "end_of_workflow"
    # s_query committed durably; s_report ran but its checkpoint write failed.
    assert end["data"]["status"] != "SUCCEEDED"
    assert end["data"]["status"] == "PARTIAL_FAILED"
    assert "s_report" in end["data"]["failed_steps"]

    # The failed s_report commit left the live state exactly at the post-s_query
    # snapshot -- none of the three fields advanced to include s_report.
    assert state["completed_steps"] == ["s_query"]
    assert set(state["step_results"]) == {"s_query"}
    # The artifact index (keyed by artifact_id) holds ONLY s_query's output --
    # s_report's payload was written to disk but never promoted into the index.
    query_ref = state["step_results"]["s_query"]["outputs"]["salary_summary"]
    assert list(state["artifacts"]) == [query_ref["artifact_id"]]

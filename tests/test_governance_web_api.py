from fastapi.testclient import TestClient
import pytest

import src.service.web_app as web_app

from src.orchestration.completion import (
    PersistentReceiptStore,
    idempotency_key,
    normalize_input,
    validate_receipt,
)
from src.orchestration.reconciliation import get_reconciliation_store
from src.orchestration.governance import record_governance_event
from src.robust.task_logger import TaskLogger
from src.security.approval import get_approval_store
from src.service.web_app import create_app


@pytest.fixture(autouse=True)
def _configured_governance_identity(monkeypatch):
    monkeypatch.setattr(web_app, "GOVERNANCE_ADMIN_API_KEY", "test-governance-key")
    monkeypatch.setattr(web_app, "GOVERNANCE_ADMIN_ACTOR_ID", "admin")


def _client() -> TestClient:
    client = TestClient(create_app())
    client.headers["Authorization"] = "Bearer test-governance-key"
    return client


def _approval():
    return get_approval_store().create(
        user_id="u1",
        workflow_id="wf-1",
        task_id="task-1",
        resume_step=2,
        node_name="A",
        step_id="s1",
        subject={"subject_type": "user", "id": "u1", "attributes": {}},
        object={"object_type": "agent", "id": "A", "attributes": {}},
        scenario={},
        action={"verb": "dispatch", "attributes": {"action_type": "write"}},
        policy_result={
            "decision": "REVIEW_REQUIRED",
            "reason": "approval needed",
        },
    )


def test_approval_api_lists_approves_and_exposes_resume_contract(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    approval = _approval()
    client = _client()

    listed = client.get(
        "/api/security/approvals",
        params={"requester_id": "admin", "status": "pending"},
    )
    assert listed.status_code == 200
    assert [item["approval_id"] for item in listed.json()] == [
        approval.approval_id
    ]

    approved = client.post(
        f"/api/security/approvals/{approval.approval_id}/approve",
        json={"approver": "admin", "comment": "ok"},
    )
    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == "approved"
    assert body["resume_endpoint"] == "/api/tasks/resume"
    assert body["resume_request"] == {
        "task_id": "task-1",
        "resume_step": 2,
        "user_id": "u1",
        "workmode": "production",
    }

    timeline = client.get("/api/tasks/task-1/governance")
    assert timeline.status_code == 200
    assert timeline.json()[-1]["event_type"] == "APPROVAL_GRANTED"


def test_approval_api_rejects_request(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    approval = _approval()
    client = _client()

    rejected = client.post(
        f"/api/security/approvals/{approval.approval_id}/reject",
        json={"approver": "admin", "comment": "not allowed"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    again = client.post(
        f"/api/security/approvals/{approval.approval_id}/approve",
        json={"approver": "admin", "comment": ""},
    )
    assert again.status_code == 409


def _reconciliation(tmp_path, monkeypatch, *, task_id="task-recon"):
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    inputs = {"employee": "李娜"}
    key = idempotency_key(task_id, "write-1", inputs)
    receipts = PersistentReceiptStore(task_id)
    claim = receipts.claim_if_absent(
        key,
        {
            "idempotency_key": key,
            "task_id": task_id,
            "step_id": "write-1",
            "agent": "DocumentAgent",
            "status": "STARTED",
            "normalized_input": normalize_input(inputs),
            "external_op_id": None,
            "timestamp": 1.0,
        },
    )
    reconciliation = get_reconciliation_store().create(
        user_id="u1",
        workflow_id="wf-recon",
        task_id=task_id,
        step_id="write-1",
        resume_step=2,
        agent_name="DocumentAgent",
        error="external result unknown",
        idempotency_key=key,
        claim_id=claim.claim_id or "",
        receipt=claim.receipt,
    )
    return reconciliation, key


def test_reconciliation_api_releases_receipt_then_exposes_resume(
    tmp_path, monkeypatch
):
    reconciliation, key = _reconciliation(tmp_path, monkeypatch)
    client = _client()

    listed = client.get(
        "/api/security/reconciliations",
        params={"requester_id": "admin", "status": "pending"},
    )
    assert listed.status_code == 200
    assert [item["reconciliation_id"] for item in listed.json()] == [
        reconciliation.reconciliation_id
    ]

    retried = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/retry"
        ),
        json={"operator": "admin", "comment": "外部目录中没有新文件"},
    )
    assert retried.status_code == 200
    body = retried.json()
    assert body["status"] == "retry_ready"
    assert body["resume_request"]["resume_step"] == 2
    assert PersistentReceiptStore("task-recon").get(key) is None

    timeline = client.get("/api/tasks/task-recon/governance").json()
    assert timeline[-1]["decision"] == "SAFE_TO_RETRY"


def test_reconciliation_api_confirms_success_with_external_operation_id(
    tmp_path, monkeypatch
):
    reconciliation, key = _reconciliation(tmp_path, monkeypatch)
    client = _client()

    missing_id = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/succeeded"
        ),
        json={"operator": "admin"},
    )
    assert missing_id.status_code == 422

    succeeded = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/succeeded"
        ),
        json={
            "operator": "admin",
            "comment": "已在文档平台找到文件",
            "external_operation_id": "doc-20260729-001",
            "outputs": {"document_id": "doc-20260729-001"},
        },
    )
    assert succeeded.status_code == 200
    assert succeeded.json()["status"] == "confirmed_succeeded"
    receipt = PersistentReceiptStore("task-recon").get(key)
    assert validate_receipt(receipt, key=key)
    assert receipt["confirmed_by"] == "admin"


def test_reconciliation_api_can_freeze_and_terminate(tmp_path, monkeypatch):
    reconciliation, key = _reconciliation(tmp_path, monkeypatch)
    client = _client()

    frozen = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/freeze"
        ),
        json={"operator": "admin", "comment": "等待供应商回执"},
    )
    assert frozen.status_code == 200
    assert frozen.json()["status"] == "frozen"

    terminated = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/terminate"
        ),
        json={"operator": "admin", "comment": "业务取消"},
    )
    assert terminated.status_code == 200
    assert terminated.json()["status"] == "terminated"
    assert PersistentReceiptStore("task-recon").get(key)["status"] == "STARTED"


def test_deleting_conversation_cascades_runtime_and_security_records(
    tmp_path, monkeypatch
):
    import src.robust.checkpoint as checkpoint_module
    import src.robust.task_logger as task_logger_module

    checkpoint_root = tmp_path / "checkpoints"
    monkeypatch.setattr(task_logger_module, "checkpoints_dir", checkpoint_root)
    monkeypatch.setattr(checkpoint_module, "checkpoints_dir", checkpoint_root)
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    monkeypatch.setenv(
        "ARTIFACT_PAYLOAD_STORE_DIR", str(tmp_path / "artifacts")
    )
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )

    task_id = "u1_demo__20260729_120000"
    workflow_id = "u1:demo"
    task_log = TaskLogger(task_id, workflow_id, "test")
    task_log.log_workflow_start("test")
    checkpoint_dir = checkpoint_root / task_id
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    (checkpoint_dir / "0_scheduler.json").write_text("{}", encoding="utf-8")
    (tmp_path / "receipts").mkdir(parents=True, exist_ok=True)
    receipt = PersistentReceiptStore(task_id)
    receipt.put("key", {"status": "STARTED"})
    record_governance_event(
        "WORKFLOW_STARTED",
        task_id=task_id,
        workflow_id=workflow_id,
    )
    get_reconciliation_store().create(
        user_id="u1",
        workflow_id=workflow_id,
        task_id=task_id,
        step_id="send",
        resume_step=1,
        agent_name="RemoteEmailDispatchAgent",
        error="outcome unknown",
    )
    get_approval_store().create(
        user_id="u1",
        workflow_id=workflow_id,
        task_id=task_id,
        resume_step=1,
        node_name="RemoteEmailDispatchAgent",
        subject={"id": "u1"},
        object={"id": "RemoteEmailDispatchAgent"},
        scenario={},
        action={"verb": "dispatch"},
        policy_result={"decision": "REVIEW_REQUIRED"},
    )

    client = _client()
    response = client.delete(
        f"/api/tasks/{task_id}",
        params={"user_id": "admin"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["business_outputs_preserved"] is True
    assert TaskLogger.load(task_id) is None
    assert not checkpoint_dir.exists()
    assert get_reconciliation_store().list(task_id=task_id) == []
    assert get_approval_store().list(task_id=task_id) == []


def test_deleting_legacy_conversation_removes_orphan_security_records_only(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    get_reconciliation_store().create(
        user_id="u1",
        workflow_id="u1:demo",
        task_id="legacy-task",
        step_id="send",
        resume_step=1,
        agent_name="RemoteEmailDispatchAgent",
        error="outcome unknown",
    )

    response = _client().delete(
        "/api/conversation-history",
        params={"workflow_id": "u1:demo", "user_id": "u1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["deleted_tasks"] == 0
    assert body["deleted"]["reconciliations"] == 1
    assert body["business_outputs_preserved"] is True
    assert get_reconciliation_store().list(task_id="legacy-task") == []


def test_deleting_conversation_rejects_unauthenticated_spoofed_admin(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    client = TestClient(create_app())

    response = client.delete(
        "/api/conversation-history",
        params={"workflow_id": "alice:demo", "user_id": "admin"},
    )

    assert response.status_code == 401


def test_governance_mutations_require_authenticated_server_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    approval = _approval()
    reconciliation, _ = _reconciliation(tmp_path, monkeypatch)
    client = TestClient(create_app())

    denied_approval = client.post(
        f"/api/security/approvals/{approval.approval_id}/approve",
        json={"approver": "admin", "comment": "spoofed admin"},
    )
    denied_reconciliation = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/freeze"
        ),
        json={"operator": "admin", "comment": "spoofed admin"},
    )
    unknown_listing = client.get(
        "/api/security/approvals",
        params={"requester_id": "not-a-demo-user"},
    )

    assert denied_approval.status_code == 401
    assert denied_reconciliation.status_code == 401
    assert unknown_listing.status_code == 401
    assert get_approval_store().get(approval.approval_id).status == "pending"
    assert (
        get_reconciliation_store()
        .get(reconciliation.reconciliation_id)
        .status
        == "pending"
    )


def test_governance_mutations_ignore_body_actor_and_record_server_actor(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    approval = _approval()
    reconciliation, _ = _reconciliation(tmp_path, monkeypatch)
    client = _client()

    approved = client.post(
        f"/api/security/approvals/{approval.approval_id}/approve",
        json={"approver": "guest", "comment": "body actor is ignored"},
    )
    frozen = client.post(
        f"/api/security/reconciliations/{reconciliation.reconciliation_id}/freeze",
        json={"operator": "guest", "comment": "body actor is ignored"},
    )

    assert approved.status_code == 200
    assert approved.json()["decision"]["approver"] == "admin"
    assert frozen.status_code == 200
    assert frozen.json()["resolution"]["operator"] == "admin"


def test_task_cleanup_uses_authenticated_reviewer_not_query_identity(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    record = get_reconciliation_store().create(
        user_id="hr_manager",
        workflow_id="hr_manager:cleanup-test",
        task_id="task-owned-by-hr",
        step_id="send",
        resume_step=1,
        agent_name="RemoteEmailDispatchAgent",
        error="unknown outcome",
    )
    unauthenticated = TestClient(create_app())

    denied = unauthenticated.delete(
        "/api/tasks/task-owned-by-hr",
        params={"user_id": "admin"},
    )
    assert denied.status_code == 401
    assert get_reconciliation_store().get(record.reconciliation_id) is not None

    deleted = _client().delete(
        "/api/tasks/task-owned-by-hr",
        params={"user_id": "engineer"},
    )
    assert deleted.status_code == 200
    assert get_reconciliation_store().get(record.reconciliation_id) is None


def test_governance_reads_reject_query_parameter_identity_spoofing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    _approval()
    _reconciliation(tmp_path, monkeypatch)
    record_governance_event("APPROVAL_REQUIRED", task_id="task-1")
    client = TestClient(create_app())

    assert client.get(
        "/api/security/approvals", params={"requester_id": "admin"}
    ).status_code == 401
    assert client.get(
        "/api/security/reconciliations", params={"requester_id": "admin"}
    ).status_code == 401
    assert client.get(
        "/api/tasks/task-1/governance", params={"requester_id": "admin"}
    ).status_code == 401


def test_reconciliation_receipt_transaction_rolls_back_on_record_write_failure(
    tmp_path, monkeypatch
):
    reconciliation, key = _reconciliation(tmp_path, monkeypatch)
    store = get_reconciliation_store()
    original_save = store._save

    def fail_resolution_save(request):
        if request.status == "retry_ready":
            raise OSError("injected reconciliation write failure")
        return original_save(request)

    monkeypatch.setattr(store, "_save", fail_resolution_save)
    response = _client().post(
        f"/api/security/reconciliations/{reconciliation.reconciliation_id}/retry",
        json={"comment": "confirmed absent"},
    )

    assert response.status_code == 409
    assert PersistentReceiptStore(reconciliation.task_id).get(key)["status"] == "STARTED"
    assert store.get(reconciliation.reconciliation_id).status == "pending"


def test_security_precheck_matches_static_policy_constraints(monkeypatch):
    import src.service.web_app as web_app

    monkeypatch.setattr(web_app, "S_ABAC_ENABLED", True)
    client = _client()

    salary_review = client.get(
        "/api/security/tool-check",
        params={"user_id": "hr_manager", "tool_name": "remote_salary_info_tool"},
    )
    salary_denied = client.get(
        "/api/security/tool-check",
        params={"user_id": "engineer", "tool_name": "remote_salary_info_tool"},
    )
    unknown_tool = client.get(
        "/api/security/tool-check",
        params={"user_id": "admin", "tool_name": "not_registered_tool"},
    )
    precheck = client.get("/api/security/users/hr_manager/precheck")

    assert salary_review.status_code == 200
    assert salary_review.json()["decision"] == "REVIEW_REQUIRED"
    assert salary_review.json()["allowed"] is False
    assert salary_review.json()["eligible"] is True

    assert salary_denied.json()["decision"] == "DENY"
    assert salary_denied.json()["grants_match"] is False
    assert unknown_tool.json()["decision"] == "DENY"
    assert unknown_tool.json()["resource_registered"] is False

    salary_summary = precheck.json()["tool_access"]["remote_salary_info_tool"]
    document_summary = precheck.json()["tool_access"]["remote_docx_generator_tool"]
    assert salary_summary["decision"] == "REVIEW_REQUIRED"
    assert document_summary["decision"] == "ALLOW"


def test_agent_precheck_enforces_user_agent_roster(monkeypatch):
    import src.service.web_app as web_app

    monkeypatch.setattr(web_app, "S_ABAC_ENABLED", True)
    client = _client()

    denied = client.get(
        "/api/security/check",
        params={
            "user_id": "hr_manager",
            "agent_name": "RemoteEmailDispatchAgent",
            "action": "send",
        },
    )
    allowed = client.get(
        "/api/security/check",
        params={
            "user_id": "admin",
            "agent_name": "RemoteBusinessRiskAgent",
            "action": "query",
        },
    )

    assert denied.json()["decision"] == "DENY"
    assert denied.json()["available_to_user"] is False
    assert allowed.json()["decision"] == "ALLOW"
    assert allowed.json()["available_to_user"] is True

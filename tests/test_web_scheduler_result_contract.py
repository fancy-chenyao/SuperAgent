from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_web_recognizes_scheduler_agents_and_result_events():
    source = _source()

    assert 'normalized.startsWith("scheduler")' in source
    assert 'eventName === "step_result"' in source
    assert 'eventName === "final_result"' in source
    assert "renderFinalResult(payload.data || {})" in source


def test_web_keys_parallel_step_cards_by_scheduler_event_identity():
    source = _source()

    assert "executionStepCardsByKey = new Map()" in source
    assert "const findStepCard = (data = {})" in source
    assert "data.agent_id, data.step_id" in source
    assert 'if (existing.status === "running")' in source
    assert "executionStepCardsByKey.delete(normalizedKey)" in source
    assert "finalizeStepCard(findStepCard(data) || currentStepCard)" in source


def test_legacy_workflow_emits_unique_identity_for_each_node_execution():
    process_source = (
        Path(__file__).resolve().parents[1] / "src" / "workflow" / "process.py"
    ).read_text(encoding="utf-8")

    assert 'node_event_id = f"{workflow_id}_{original_node_name}_{step_count}"' in process_source
    assert process_source.count('"agent_id": node_event_id') == 2


def test_web_handles_all_scheduler_terminal_statuses():
    source = _source()

    for status in (
        "SUCCEEDED",
        "FAILED",
        "PARTIAL_FAILED",
        "CLARIFY_REQUIRED",
        "APPROVAL_REQUIRED",
        "REJECTED",
        "NEEDS_RECONCILIATION",
    ):
        assert f'case "{status}"' in source


def test_web_prefers_structured_failure_and_keeps_legacy_error_fallback():
    source = _source()

    assert "normalizeFailure = (failure, legacyError" in source
    assert "failure.message || legacyError" in source
    assert "data.failure || (status && status !== \"SUCCEEDED\")" in source
    assert "errorStepCard(content, card, data.failure)" in source
    assert 'data?.error || "该步骤未返回可展示的结果。"' in source


def test_web_failure_display_covers_actionable_categories_and_escapes_fields():
    source = _source()

    for value in (
        "UPSTREAM_STEP_FAILED",
        'category === "permission"',
        'category === "schema"',
        'category === "contract"',
        'category === "reconciliation"',
        "SIDE_EFFECT_UNCONFIRMED",
        "UNKNOWN_WORKFLOW_FAILURE",
    ):
        assert value in source

    for field in (
        "failure.message",
        "failure.code",
        "action",
        "retryText",
        'failure.blockedBy.join("、")',
    ):
        assert f"escapeHtml({field})" in source

    assert "parameterName: failure.parameter_name" in source
    assert "pre.textContent = JSON.stringify(data, null, 2)" in source
    assert "${escapeHtml(log.error)}" in source


def test_web_renders_terminal_failure_and_blocked_step_summary():
    source = _source()

    assert "workflowFailureSummary = {" in source
    assert "workflowData.failures" in source
    assert "workflowData.blocked_steps" in source
    assert "renderWorkflowFailureSummaryInto(workflowFailureSummary, frag)" in source
    assert 'section.setAttribute("aria-label", "工作流失败摘要")' in source
def test_web_routes_reconciliation_to_security_queue():
    source = _source()

    assert 'eventName === "reconciliation_required"' in source
    assert "Security → 人工核对队列" in source
    security_source = (
        Path(__file__).resolve().parents[1] / "web" / "security.js"
    ).read_text(encoding="utf-8")
    assert "/api/security/reconciliations?requester_id=" in security_source
    for decision in ("retry", "succeeded", "freeze", "terminate"):
        assert f'data-decision="{decision}"' in security_source


def test_clear_conversation_cascades_backend_history_before_local_storage():
    source = _source()

    assert 'fetch(`/api/tasks/${encodeURIComponent(taskId)}?${query}`' in source
    assert 'fetch(`/api/conversation-history?${query}`' in source
    fetch_index = source.index('fetch(`/api/conversation-history?${query}`')
    local_delete_index = source.index(
        "localStorage.removeItem(getChatHistoryKey(userId))"
    )
    assert fetch_index < local_delete_index


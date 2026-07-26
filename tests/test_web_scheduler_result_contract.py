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
    assert "finalizeStepCard(findStepCard(data) || currentStepCard)" in source


def test_web_handles_all_scheduler_terminal_statuses():
    source = _source()

    for status in (
        "SUCCEEDED",
        "FAILED",
        "PARTIAL_FAILED",
        "CLARIFY_REQUIRED",
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


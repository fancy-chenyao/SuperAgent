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


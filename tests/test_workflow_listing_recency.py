from datetime import timezone

from src.service.web_app import _workflow_last_used


def test_workflow_recency_falls_back_to_updated_at():
    timestamp = _workflow_last_used(
        {
            "user_input_messages": [
                {"role": "user", "content": "没有消息时间"}
            ],
            "updated_at": "2026-07-24T08:24:25+08:00",
        }
    )

    assert timestamp is not None
    assert timestamp.tzinfo == timezone.utc
    assert timestamp.isoformat() == "2026-07-24T00:24:25+00:00"


def test_workflow_recency_compares_legacy_naive_and_aware_timestamps():
    timestamp = _workflow_last_used(
        {
            "user_input_messages": [
                {"timestamp": "2026-07-21T15:17:07.810936"}
            ],
            "updated_at": "2026-07-24T00:24:25+00:00",
        }
    )

    assert timestamp is not None
    assert timestamp.isoformat() == "2026-07-24T00:24:25+00:00"

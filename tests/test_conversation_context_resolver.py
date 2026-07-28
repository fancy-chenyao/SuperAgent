from src.orchestrator.context_resolver import resolve_conversation_request


def test_new_request_does_not_inherit_unreferenced_previous_entity():
    resolved = resolve_conversation_request(
        current_message="查询北京明天天气，结合出差行程给出提醒",
        context_entities={"employee_name": "李娜", "people": ["李娜"]},
    )

    assert resolved.resolved_message == "查询北京明天天气，结合出差行程给出提醒"
    assert resolved.entity_overrides == {}
    assert resolved.context_references == []


def test_pronoun_is_left_for_semantic_context_resolution():
    resolved = resolve_conversation_request(
        current_message="帮她写一份请假书",
        context_entities={"employee_name": "李娜", "people": ["李娜"]},
    )

    assert resolved.resolved_message == "帮她写一份请假书"
    assert resolved.entity_overrides == {}
    assert resolved.context_references == []


def test_clarification_answer_fills_field_without_becoming_task_boundary():
    resolved = resolve_conversation_request(
        current_message="李娜",
        turn_type="clarification_answer",
        clarification_context={
            "base_query": "查询北京明天天气，结合出差行程给出提醒",
            "missing_fields": ["employee_or_criteria"],
            "entities": {"location": "北京", "time": "明天"},
        },
    )

    assert resolved.resolved_message == "查询北京明天天气，结合出差行程给出提醒"
    assert resolved.entity_overrides["employee_name"] == "李娜"
    assert resolved.entity_overrides["location"] == "北京"
    assert all("李娜" not in item for item in [resolved.resolved_message])


def test_recent_artifacts_are_only_candidates_for_semantic_resolution():
    artifacts = [{
        "artifact_id": "report-1",
        "type": "report",
        "title": "李娜公开信息简报",
        "source_agent": "reporter",
        "summary": "简报内容",
    }]

    unrelated = resolve_conversation_request(
        current_message="查询北京明天天气",
        context_artifacts=artifacts,
    )
    referenced = resolve_conversation_request(
        current_message="把刚才的报告发给王经理",
        context_artifacts=artifacts,
    )

    assert unrelated.artifact_inputs == artifacts
    assert referenced.artifact_inputs == artifacts
    assert referenced.context_references == []


def test_task_goal_clarification_uses_the_concrete_follow_up_request():
    resolved = resolve_conversation_request(
        current_message="查询李娜本月工资",
        turn_type="clarification_answer",
        clarification_context={
            "base_query": "帮我处理一下",
            "missing_fields": ["task_goal"],
        },
    )

    assert resolved.resolved_message == "查询李娜本月工资"

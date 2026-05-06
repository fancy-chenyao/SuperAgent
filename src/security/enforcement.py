from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional

from src.service.env import S_ABAC_ENABLED
from src.security.approval import get_approval_store
from src.security.context import SecurityContextBuilder
from src.security.policy import Action, Object, PolicyEngine, Scenario, Subject


class PermissionDeniedError(Exception):
    def __init__(self, message: str, payload: Dict[str, Any]):
        super().__init__(message)
        self.payload = payload


class ApprovalRequiredError(Exception):
    def __init__(self, message: str, payload: Dict[str, Any]):
        super().__init__(message)
        self.payload = payload


_engine: Optional[PolicyEngine] = None


def get_policy_engine() -> PolicyEngine:
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine


def _serialize(subject: Subject, object: Object, scenario: Scenario, action: Action, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "subject": asdict(subject),
        "object": asdict(object),
        "scenario": asdict(scenario),
        "action": asdict(action),
        "policy_result": result,
    }


def _metadata(context: Any = None) -> Dict[str, Any]:
    return dict(getattr(context, "metadata", {}) or {})


def _approval_signature(payload: Dict[str, Any]) -> str:
    return get_approval_store().signature(
        payload["subject"],
        payload["object"],
        payload["action"],
    )


def _approval_task_id(context: Any = None) -> Optional[str]:
    metadata = _metadata(context)
    return metadata.get("task_id")


def _check_rejected(payload: Dict[str, Any], context: Any = None) -> bool:
    task_id = _approval_task_id(context)
    if not task_id:
        return False
    signature = _approval_signature(payload)
    return get_approval_store().find_latest(
        task_id=task_id,
        signature=signature,
        statuses=["rejected"],
    ) is not None


def _check_grant(payload: Dict[str, Any], context: Any = None) -> bool:
    task_id = _approval_task_id(context)
    if not task_id:
        return False
    signature = _approval_signature(payload)
    return get_approval_store().consume_if_approved(task_id=task_id, signature=signature) is not None


def _enforce(
    subject: Subject,
    object: Object,
    scenario: Scenario,
    action: Action,
    *,
    context: Any = None,
) -> Dict[str, Any]:
    if not S_ABAC_ENABLED:
        return {"allowed": True, "reason": "S-ABAC disabled", "human_review_required": False}

    result = get_policy_engine().evaluate(subject, object, scenario, action)
    payload = _serialize(subject, object, scenario, action, result)
    if result.get("allowed"):
        return result

    if result.get("human_review_required"):
        if _check_rejected(payload, context=context):
            raise PermissionDeniedError("S-ABAC approval was rejected", payload)
        if _check_grant(payload, context=context):
            result = dict(result)
            result.update({"allowed": True, "human_review_required": False, "reason": "Approved one-time grant consumed"})
            return result
        raise ApprovalRequiredError("S-ABAC approval required", payload)

    raise PermissionDeniedError(result.get("reason", "S-ABAC permission denied"), payload)


async def enforce_agent_dispatch(agent: Any, context: Any) -> Dict[str, Any]:
    subject = SecurityContextBuilder.system_subject()
    object = SecurityContextBuilder.object_for_agent(agent)
    scenario = SecurityContextBuilder.scenario_from_context(context)
    action = SecurityContextBuilder.action_for_agent_dispatch(getattr(agent, "agent_name", "unknown"))
    return _enforce(subject, object, scenario, action, context=context)


async def enforce_tool_call(
    *,
    agent: Any,
    tool_name: str,
    arguments: Optional[Dict[str, Any]],
    context: Any,
    tool: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
    resource_spec: Any = None,
) -> Dict[str, Any]:
    subject = SecurityContextBuilder.subject_for_agent(agent)
    if resource_spec is not None:
        object = SecurityContextBuilder.object_for_resource_spec(resource_spec)
    else:
        object = SecurityContextBuilder.object_for_tool(tool_name, tool=tool, metadata=metadata)
    scenario = SecurityContextBuilder.scenario_from_context(context)
    action = SecurityContextBuilder.action_for_tool_call(tool_name, arguments)
    return _enforce(subject, object, scenario, action, context=context)

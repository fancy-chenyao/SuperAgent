from __future__ import annotations
from dataclasses import asdict
from typing import Any, Dict, Optional

from src.service.env import S_ABAC_ENABLED
from src.security.context import SecurityContextBuilder, UnknownSecurityUserError
from src.security.policy import Action, Object, PolicyEngine, Scenario, Subject
from src.security.scenario_analyzer import analyze_object_fit
from config.s_abac_demo_users import get_user_available_agents


class PermissionDeniedError(Exception):
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


async def _enrich_context_with_scenario_fit(
    *,
    context: Any,
    object: Object,
) -> None:
    metadata = getattr(context, "metadata", None)
    if not isinstance(metadata, dict):
        return

    task_profile = metadata.get("task_profile", {})
    user_query = (
        task_profile.get("business_goal")
        or metadata.get("business_goal")
        or metadata.get("USER_QUERY", metadata.get("user_query", ""))
    )
    cache = metadata.setdefault("scenario_fit_cache", {})
    cache_key = f"{object.object_type}:{object.id}"

    if cache_key in cache:
        metadata["scenario_fit_result"] = cache[cache_key]
        return

    fit_result = await analyze_object_fit(
        user_query,
        object_id=object.id,
        object_type=object.object_type,
        object_attrs=object.attributes,
        task_profile=task_profile,
    )
    cache[cache_key] = fit_result
    metadata["scenario_fit_result"] = fit_result


def _enforce(
    subject: Subject,
    object: Object,
    scenario: Scenario,
    action: Action,
    *,
    context: Any = None,
) -> Dict[str, Any]:
    if not S_ABAC_ENABLED:
        return {"allowed": True, "reason": "S-ABAC disabled"}

    result = get_policy_engine().evaluate(subject, object, scenario, action)
    if result.get("allowed"):
        return result

    payload = _serialize(subject, object, scenario, action, result)
    raise PermissionDeniedError(result.get("reason", "S-ABAC permission denied"), payload)


async def enforce_agent_dispatch(agent: Any, context: Any) -> Dict[str, Any]:
    agent_name = getattr(agent, "agent_name", "unknown")
    user_id = getattr(context, "user_id", None)
    if user_id:
        try:
            subject = SecurityContextBuilder.subject_for_user(user_id)
        except UnknownSecurityUserError as exc:
            payload = {
                "subject": {
                    "subject_type": "user",
                    "id": user_id,
                    "attributes": {},
                },
                "object": asdict(SecurityContextBuilder.object_for_agent(agent)),
                "action": asdict(SecurityContextBuilder.action_for_agent_dispatch(agent_name)),
                "policy_result": {
                    "allowed": False,
                    "reason": str(exc),
                },
            }
            raise PermissionDeniedError(str(exc), payload) from exc
        available = get_user_available_agents(user_id)
        if available and available != ["*"] and agent_name not in available:
            payload = {
                "subject": asdict(subject),
                "object": asdict(SecurityContextBuilder.object_for_agent(agent)),
                "action": asdict(SecurityContextBuilder.action_for_agent_dispatch(agent_name)),
                "policy_result": {
                    "allowed": False,
                    "reason": f"Agent '{agent_name}' is not in user '{user_id}' available agents: {available}",
                },
            }
            raise PermissionDeniedError(
                f"User '{user_id}' is not authorized to dispatch agent '{agent_name}'",
                payload,
            )
    else:
        subject = SecurityContextBuilder.system_subject()
    object = SecurityContextBuilder.object_for_agent(agent)
    await _enrich_context_with_scenario_fit(context=context, object=object)
    scenario = SecurityContextBuilder.scenario_from_context(context)
    action = SecurityContextBuilder.action_for_agent_dispatch(agent_name)
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
    user_id = getattr(context, "user_id", None)
    if user_id:
        try:
            subject = SecurityContextBuilder.subject_for_user(user_id)
        except UnknownSecurityUserError as exc:
            if resource_spec is not None:
                object = SecurityContextBuilder.object_for_resource_spec(resource_spec)
            else:
                object = SecurityContextBuilder.object_for_tool(tool_name, tool=tool, metadata=metadata)
            payload = {
                "subject": {
                    "subject_type": "user",
                    "id": user_id,
                    "attributes": {},
                },
                "object": asdict(object),
                "action": asdict(SecurityContextBuilder.action_for_tool_call(tool_name, arguments)),
                "policy_result": {
                    "allowed": False,
                    "reason": str(exc),
                },
            }
            raise PermissionDeniedError(str(exc), payload) from exc
    else:
        subject = SecurityContextBuilder.subject_for_agent(agent)
    if resource_spec is not None:
        object = SecurityContextBuilder.object_for_resource_spec(resource_spec)
    else:
        object = SecurityContextBuilder.object_for_tool(tool_name, tool=tool, metadata=metadata)
    await _enrich_context_with_scenario_fit(context=context, object=object)
    scenario = SecurityContextBuilder.scenario_from_context(context)
    action = SecurityContextBuilder.action_for_tool_call(tool_name, arguments)
    return _enforce(subject, object, scenario, action, context=context)

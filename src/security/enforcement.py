from __future__ import annotations
from dataclasses import asdict
from typing import Any, Dict, Optional

from src.service.env import S_ABAC_ENABLED
from src.security.context import SecurityContextBuilder, UnknownSecurityUserError
from src.security.policy import Action, Object, PolicyEngine, Scenario, Subject
from src.security.scenario_analyzer import analyze_object_fit
from src.security.approval import ApprovalStore, get_approval_store
from config.s_abac_demo_users import get_user_available_agents


class PermissionDeniedError(Exception):
    def __init__(self, message: str, payload: Dict[str, Any]):
        super().__init__(message)
        self.payload = payload


class ApprovalRequiredError(PermissionDeniedError):
    """The policy denied execution until a matching approval is granted."""


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
    if str(result.get("decision", "")).upper() == "REVIEW_REQUIRED":
        signature = ApprovalStore.signature(
            payload["subject"],
            payload["object"],
            payload["action"],
            payload["scenario"],
        )
        payload["approval_signature"] = signature

        metadata = getattr(context, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        task_id = str(metadata.get("task_id") or "")
        if task_id:
            approval_store = get_approval_store()
            approved = approval_store.consume_if_approved(
                task_id=task_id, signature=signature
            )
            if approved is not None:
                return {
                    **result,
                    "allowed": True,
                    "decision": "ALLOW_APPROVED",
                    "reason": "Matching human approval consumed",
                    "approval_id": approved.approval_id,
                    "approval_signature": signature,
                }

            active = approval_store.find_active(
                task_id=task_id, signature=signature
            )
            if active is None:
                rejected = approval_store.find_latest(
                    task_id=task_id,
                    signature=signature,
                    statuses=["rejected"],
                )
                if rejected is not None:
                    rejected_result = dict(result)
                    rejected_result.update(
                        {
                            "decision": "DENY",
                            "human_review_required": False,
                            "reason": "Human approval was rejected",
                            "approval_id": rejected.approval_id,
                        }
                    )
                    payload["policy_result"] = rejected_result
                    raise PermissionDeniedError(
                        rejected_result["reason"], payload
                    )

        raise ApprovalRequiredError(
            result.get("reason", "Human approval required"), payload
        )

    raise PermissionDeniedError(result.get("reason", "S-ABAC permission denied"), payload)


async def enforce_agent_dispatch(agent: Any, context: Any) -> Dict[str, Any]:
    if isinstance(context, dict) and context.get("execution_context") is not None:
        context = context["execution_context"]
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
    if isinstance(context, dict) and context.get("execution_context") is not None:
        context = context["execution_context"]
    # Tool implementations normally expose only their business arguments.  The
    # operation mode is owned by the trusted TaskGraph and must not silently
    # collapse to the generic ``call`` action, otherwise a legitimate read can
    # be misclassified and an approval signature will not bind read/write/send.
    action_arguments = dict(arguments or {})
    context_metadata = getattr(context, "metadata", None)
    if isinstance(context_metadata, dict):
        operation_mode = context_metadata.get("operation_mode")
        if operation_mode:
            action_arguments.setdefault("operation_mode", str(operation_mode))

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
                "action": asdict(SecurityContextBuilder.action_for_tool_call(tool_name, action_arguments)),
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
    action = SecurityContextBuilder.action_for_tool_call(tool_name, action_arguments)
    return _enforce(subject, object, scenario, action, context=context)

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.s_abac_config import S_ABAC_POLICIES, SENSITIVITY_LEVELS


@dataclass
class Subject:
    subject_type: str
    id: str
    attributes: Dict[str, Any] = field(default_factory=dict)

    def get_roles(self) -> List[str]:
        roles = self.attributes.get("role", [])
        if isinstance(roles, str):
            return [roles]
        return list(roles or [])

    def get_clearance_level(self) -> int:
        return int(self.attributes.get("clearance_level", 0) or 0)


@dataclass
class Object:
    object_type: str
    id: str
    attributes: Dict[str, Any] = field(default_factory=dict)

    def get_sensitivity(self) -> str:
        return str(self.attributes.get("sensitivity", "LOW")).upper()

    def get_allowed_roles(self) -> List[str]:
        roles = self.attributes.get("allowed_roles", [])
        if isinstance(roles, str):
            return [roles]
        return list(roles or [])

    def requires_human_approval(self) -> bool:
        return False


@dataclass
class Scenario:
    task_scenario: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    business_context: Dict[str, Any] = field(default_factory=dict)

    def get_stage(self) -> str:
        return str(self.task_scenario.get("stage", "EXECUTION"))

    def get_risk_profile(self) -> str:
        return str(self.task_scenario.get("risk_profile", "LOW")).upper()

    def is_working_hours(self) -> bool:
        explicit = self.environment.get("time")
        if explicit:
            return explicit == "working_hours"
        now = datetime.now().time()
        return datetime.strptime("09:00", "%H:%M").time() <= now <= datetime.strptime("18:00", "%H:%M").time()

    def is_internal_network(self) -> bool:
        return self.environment.get("network_zone", "internal") == "internal"


@dataclass
class Action:
    verb: str
    attributes: Dict[str, Any] = field(default_factory=dict)

    def get_amount(self) -> float:
        try:
            return float(self.attributes.get("amount", 0.0) or 0.0)
        except Exception:
            return 0.0

    def is_irreversible(self) -> bool:
        return False


@dataclass
class Policy:
    policy_id: str
    description: str
    rules: List[Dict[str, Any]]


class PolicyEngine:
    def __init__(self, policies: Optional[List[Dict[str, Any]]] = None):
        self.policies = [
            Policy(
                policy_id=item["policy_id"],
                description=item.get("description", ""),
                rules=item.get("rules", []),
            )
            for item in (policies if policies is not None else S_ABAC_POLICIES)
        ]
        self.audit_logs: List[Dict[str, Any]] = []

    def evaluate(
        self,
        subject: Subject,
        object: Object,
        scenario: Scenario | Dict[str, Any],
        action: Action,
    ) -> Dict[str, Any]:
        if isinstance(scenario, dict):
            scenario = Scenario(
                task_scenario=scenario.get("task_scenario", {}),
                environment=scenario.get("environment", {}),
                business_context=scenario.get("business_context", {}),
            )

        result = {
            "allowed": False,
            "reason": "No matching policy found",
            "audit_id": f"audit_{int(time.time() * 1000)}",
            "timestamp": datetime.now().isoformat(),
        }

        matched_policy: Optional[Policy] = None
        matched_rule: Optional[Dict[str, Any]] = None
        for policy in self.policies:
            for rule in policy.rules:
                if self._check_condition(subject, object, scenario, action, rule.get("condition", {})):
                    matched_policy = policy
                    matched_rule = rule
                    result["allowed"] = rule.get("effect", "DENY") == "ALLOW"
                    result["reason"] = rule.get("description", policy.description)
                    constraints = rule.get("constraints", {})
                    if constraints:
                        self._apply_constraints(result, constraints, subject, object, scenario, action)
                    self._log_audit(subject, object, scenario, action, result, matched_policy, matched_rule)
                    return result

        result.update(self._check_default_rules(subject, object, scenario, action))
        self._log_audit(subject, object, scenario, action, result, matched_policy, matched_rule)
        return result

    def _check_condition(
        self,
        subject: Subject,
        object: Object,
        scenario: Scenario,
        action: Action,
        condition: Dict[str, Any],
    ) -> bool:
        all_conditions = condition.get("all", [])
        any_conditions = condition.get("any", [])
        if all_conditions and not all(
            self._evaluate_condition(subject, object, scenario, action, cond)
            for cond in all_conditions
        ):
            return False
        if any_conditions and not any(
            self._evaluate_condition(subject, object, scenario, action, cond)
            for cond in any_conditions
        ):
            return False
        return True

    def _evaluate_condition(
        self,
        subject: Subject,
        object: Object,
        scenario: Scenario,
        action: Action,
        cond: Dict[str, Any],
    ) -> bool:
        for key, expected in cond.items():
            if key.startswith("subject.attributes."):
                value = self._nested(subject.attributes, key.replace("subject.attributes.", "").split("."))
            elif key == "subject.subject_type":
                value = subject.subject_type
            elif key == "subject.id":
                value = subject.id
            elif key.startswith("object.attributes."):
                value = self._nested(object.attributes, key.replace("object.attributes.", "").split("."))
            elif key == "object.id":
                value = object.id
            elif key == "object.type":
                value = object.object_type
            elif key == "scenario.stage":
                value = scenario.get_stage()
            elif key in {"scenario.risk_profile", "scenario.task_scenario.risk_profile"}:
                value = scenario.get_risk_profile()
            elif key == "action.verb":
                value = action.verb
            elif key.startswith("action.attributes."):
                value = action.attributes.get(key.replace("action.attributes.", ""))
            else:
                return False
            if not self._compare(value, expected):
                return False
        return True

    @staticmethod
    def _nested(data: Dict[str, Any], path: List[str]) -> Any:
        value: Any = data
        for key in path:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return None
        return value

    @staticmethod
    def _compare(value: Any, expected: Any) -> bool:
        if isinstance(expected, list):
            if isinstance(value, list):
                return bool(set(value).intersection(expected))
            return value in expected
        if isinstance(value, list):
            return expected in value
        return value == expected

    def _apply_constraints(
        self,
        result: Dict[str, Any],
        constraints: Dict[str, Any],
        subject: Subject,
        object: Object,
        scenario: Scenario,
        action: Action,
    ) -> None:
        allowed_actions = constraints.get("allowed_actions")
        if allowed_actions:
            action_type = action.attributes.get("action_type", action.verb)
            if action_type not in allowed_actions and action.verb not in allowed_actions:
                result["allowed"] = False
                result["reason"] = f"Action {action_type} not allowed"

        max_amount = constraints.get("max_amount")
        if max_amount is not None and action.get_amount() > float(max_amount):
            result["allowed"] = False
            result["reason"] = f"Amount exceeds threshold: {max_amount}"
            self._mark_for_review(result, subject, object, scenario, action)

        if constraints.get("require_working_hours") and not scenario.is_working_hours():
            result["allowed"] = False
            result["reason"] = "Operation not allowed outside working hours"

        if constraints.get("require_internal_network") and not scenario.is_internal_network():
            result["allowed"] = False
            result["reason"] = "Operation not allowed from external network"

    def _check_default_rules(
        self,
        subject: Subject,
        object: Object,
        scenario: Scenario,
        action: Action,
    ) -> Dict[str, Any]:
        result = {
            "allowed": False,
            "reason": "Default rule denied",
        }

        allowed_roles = object.get_allowed_roles()
        subject_roles = subject.get_roles()
        if allowed_roles and not set(subject_roles).intersection(allowed_roles):
            result["reason"] = f"Subject roles {subject_roles} not in allowed roles {allowed_roles}"
            return result

        sensitivity = object.get_sensitivity()
        if subject.get_clearance_level() < SENSITIVITY_LEVELS.get(sensitivity, 1):
            result["reason"] = f"Subject clearance insufficient for sensitivity {sensitivity}"
            return result

        result["allowed"] = True
        result["reason"] = "Default rule allowed"
        return result

    def _log_audit(
        self,
        subject: Subject,
        object: Object,
        scenario: Scenario,
        action: Action,
        result: Dict[str, Any],
        policy: Optional[Policy],
        rule: Optional[Dict[str, Any]],
    ) -> None:
        self.audit_logs.append(
            {
                "audit_id": result["audit_id"],
                "timestamp": result["timestamp"],
                "subject": subject.__dict__,
                "object": object.__dict__,
                "scenario": scenario.__dict__,
                "action": action.__dict__,
                "result": result,
                "policy": {"id": policy.policy_id if policy else "default", "rule": rule},
            }
        )

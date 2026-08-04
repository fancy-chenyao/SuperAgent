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

    def get_job_roles(self) -> List[str]:
        job_role = self.attributes.get("job_role", [])
        if isinstance(job_role, str):
            return [job_role]
        return list(job_role or [])

    def get_grants(self) -> List[str]:
        grants = self.attributes.get("grants", [])
        if isinstance(grants, str):
            return [grants]
        return list(grants or [])

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

    def get_allowed_job_roles(self) -> List[str]:
        roles = self.attributes.get("allowed_job_roles", [])
        if isinstance(roles, str):
            return [roles]
        return list(roles or [])

    def get_allowed_operation_modes(self) -> List[str]:
        modes = self.attributes.get("allowed_operation_modes", [])
        if isinstance(modes, str):
            return [modes]
        return list(modes or [])

    def get_expected_capabilities(self) -> List[str]:
        capabilities = self.attributes.get("expected_capabilities", [])
        if isinstance(capabilities, str):
            return [capabilities]
        return list(capabilities or [])

    def get_scenario_tags(self) -> List[str]:
        tags = self.attributes.get("scenario_tags", [])
        if isinstance(tags, str):
            return [tags]
        return list(tags or [])

    def get_required_grants(self) -> List[str]:
        grants = self.attributes.get("grants_required", [])
        if isinstance(grants, str):
            return [grants]
        return list(grants or [])

    def requires_human_approval(self) -> bool:
        return bool(self.attributes.get("requires_approval", False))


@dataclass
class Scenario:
    task_scenario: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    business_context: Dict[str, Any] = field(default_factory=dict)

    def get_stage(self) -> str:
        return str(self.task_scenario.get("stage", "EXECUTION"))

    def get_risk_profile(self) -> str:
        return str(self.task_scenario.get("risk_profile", "LOW")).upper()

    def get_task_type(self) -> str:
        return str(self.task_scenario.get("task_type", "GENERAL")).upper()

    def get_operation_mode(self) -> str:
        return str(self.task_scenario.get("operation_mode", "")).lower()

    def get_expected_capabilities(self) -> List[str]:
        capabilities = self.task_scenario.get("expected_capabilities", [])
        if isinstance(capabilities, str):
            return [capabilities]
        return list(capabilities or [])

    def get_scenario_tags(self) -> List[str]:
        tags = self.task_scenario.get("scenario_tags", [])
        if isinstance(tags, str):
            return [tags]
        return list(tags or [])

    def get_fit_result(self) -> Dict[str, Any]:
        fit_result = self.task_scenario.get("scenario_fit_result", {})
        return fit_result if isinstance(fit_result, dict) else {}

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

    def get_batch_size(self) -> int:
        try:
            return int(self.attributes.get("batch_size", 0) or 0)
        except Exception:
            return 0

    def get_operation_mode(self) -> str:
        return str(
            self.attributes.get("operation_mode")
            or self.attributes.get("action_type")
            or self.verb
        ).lower()

    def is_irreversible(self) -> bool:
        return bool(self.attributes.get("irreversible", False))


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
            "human_review_required": False,
            "approval_level": None,
            "decision": "DENY",
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
                    result["decision"] = "ALLOW" if result["allowed"] else "DENY"
                    constraints = rule.get("constraints", {})
                    if constraints:
                        self._apply_constraints(result, constraints, subject, object, scenario, action)
                    # Resource-level environment restrictions are intrinsic to
                    # the target and must still apply when a policy rule
                    # matches.  Previously a matching ALLOW rule returned
                    # before ``_check_default_rules`` and silently skipped
                    # constraints such as ``require_internal_network``.
                    self._apply_resource_environment_constraints(
                        result, subject, object, scenario
                    )
                    # An explicit ALLOW policy narrows who may use a resource;
                    # it must never bypass the resource's intrinsic roles,
                    # grants, clearance, scenario-fit or mandatory-review
                    # attributes. Evaluate the same fail-closed baseline used
                    # when no named policy matches and let any stricter outcome
                    # override the policy ALLOW.
                    if result.get("allowed"):
                        intrinsic = self._check_default_rules(
                            subject, object, scenario, action
                        )
                        if not intrinsic.get("allowed"):
                            for key in (
                                "allowed",
                                "reason",
                                "human_review_required",
                                "approval_level",
                                "decision",
                            ):
                                result[key] = intrinsic.get(key)
                    self._finalize_result(result)
                    self._log_audit(subject, object, scenario, action, result, matched_policy, matched_rule)
                    return result

        result.update(self._check_default_rules(subject, object, scenario, action))
        self._finalize_result(result)
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
            elif key == "scenario.task_scenario.task_type":
                value = scenario.get_task_type()
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
            operation_mode = action.get_operation_mode()
            allowed_normalized = {str(item).lower() for item in allowed_actions}
            if action_type.lower() not in allowed_normalized and action.verb.lower() not in allowed_normalized and operation_mode not in allowed_normalized:
                result["allowed"] = False
                result["reason"] = f"Action {action_type} not allowed"

        max_amount = constraints.get("max_amount")
        if max_amount is not None and action.get_amount() > float(max_amount):
            result["allowed"] = False
            result["reason"] = f"Amount exceeds threshold: {max_amount}"
            self._mark_for_review(result, level="HIGH")

        bypass_environment = self._bypasses_environment_constraints(subject)
        if (
            constraints.get("require_working_hours")
            and not bypass_environment
            and not scenario.is_working_hours()
        ):
            result["allowed"] = False
            result["reason"] = "Operation not allowed outside working hours"

        if (
            constraints.get("require_internal_network")
            and not bypass_environment
            and not scenario.is_internal_network()
        ):
            result["allowed"] = False
            result["reason"] = "Operation not allowed from external network"

    @staticmethod
    def _is_governance_administrator(subject: Subject) -> bool:
        """Return whether trusted attributes identify a governance admin.

        This is intentionally *not* a policy-engine bypass.  It is used only
        for the demo's approval and environment exceptions; roles, operation
        modes, scenario fit, grants and clearance are still evaluated by the
        ordinary authorization rules.
        """

        grants = {str(item).lower() for item in subject.get_grants()}
        job_roles = {str(item).lower() for item in subject.get_job_roles()}
        return "all" in grants or "system_orchestrator" in job_roles

    @staticmethod
    def _bypasses_environment_constraints(subject: Subject) -> bool:
        """Allow the system administrator to operate the demo at any time.

        The bypass is deliberately narrow: ordinary communication and HR
        roles are still subject to working-hour and network-zone controls.
        """

        return PolicyEngine._is_governance_administrator(subject)

    @staticmethod
    def _bypasses_mandatory_review(subject: Subject) -> bool:
        """Governance administrators may run the local demo without pausing.

        This bypass is deliberately limited to mandatory human review. It does
        not turn a policy DENY, scenario mismatch or unsupported operation mode
        into an ALLOW.
        """

        return PolicyEngine._is_governance_administrator(subject)

    def _apply_resource_environment_constraints(
        self,
        result: Dict[str, Any],
        subject: Subject,
        object: Object,
        scenario: Scenario,
    ) -> None:
        if self._bypasses_environment_constraints(subject):
            return
        if (
            object.attributes.get("require_working_hours")
            and not scenario.is_working_hours()
        ):
            result["allowed"] = False
            result["reason"] = "Operation not allowed outside working hours"
            result["decision"] = "DENY"
        if (
            object.attributes.get("require_internal_network")
            and not scenario.is_internal_network()
        ):
            result["allowed"] = False
            result["reason"] = "Operation not allowed from external network"
            result["decision"] = "DENY"

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
            "human_review_required": False,
            "approval_level": None,
            "decision": "DENY",
        }

        fit = self._check_scenario_fit(object, scenario)
        if fit["fit"] == "mismatch":
            result["reason"] = fit["reason"]
            return result
        if fit["fit"] == "uncertain" and self._is_high_sensitivity_or_irreversible(object, action):
            result["reason"] = fit["reason"]
            self._mark_for_review(result, level="MEDIUM")
            result["decision"] = "REVIEW_REQUIRED"
            return result

        allowed_roles = object.get_allowed_roles()
        subject_roles = subject.get_roles()
        if allowed_roles and not set(subject_roles).intersection(allowed_roles):
            result["reason"] = f"Subject roles {subject_roles} not in allowed roles {allowed_roles}"
            return result

        allowed_job_roles = object.get_allowed_job_roles()
        subject_job_roles = subject.get_job_roles()
        if allowed_job_roles and not set(subject_job_roles).intersection(allowed_job_roles):
            result["reason"] = f"Subject job roles {subject_job_roles} not in allowed job roles {allowed_job_roles}"
            return result

        required_grants = object.get_required_grants()
        subject_grants = set(subject.get_grants())
        if "all" not in subject_grants and required_grants and not set(required_grants).issubset(subject_grants):
            result["reason"] = f"Subject grants {sorted(subject_grants)} missing required grants {required_grants}"
            return result

        allowed_modes = {item.lower() for item in object.get_allowed_operation_modes()}
        operation_mode = action.get_operation_mode()
        if allowed_modes and operation_mode not in allowed_modes:
            result["reason"] = f"Operation mode {operation_mode} not in allowed modes {sorted(allowed_modes)}"
            return result

        sensitivity = object.get_sensitivity()
        if subject.get_clearance_level() < SENSITIVITY_LEVELS.get(sensitivity, 1):
            result["reason"] = f"Subject clearance insufficient for sensitivity {sensitivity}"
            if self._is_high_sensitivity_or_irreversible(object, action):
                self._mark_for_review(result, level="HIGH")
                result["decision"] = "REVIEW_REQUIRED"
            return result

        bypass_environment = self._bypasses_environment_constraints(subject)
        if (
            object.attributes.get("require_working_hours")
            and not bypass_environment
            and not scenario.is_working_hours()
        ):
            result["reason"] = "Operation not allowed outside working hours"
            return result

        if (
            object.attributes.get("require_internal_network")
            and not bypass_environment
            and not scenario.is_internal_network()
        ):
            result["reason"] = "Operation not allowed from external network"
            return result

        max_amount = object.attributes.get("max_amount")
        if max_amount is not None and action.get_amount() > float(max_amount):
            result["reason"] = f"Amount exceeds threshold: {max_amount}"
            self._mark_for_review(result, level="HIGH")
            result["decision"] = "REVIEW_REQUIRED"
            return result

        bypass_review = self._bypasses_mandatory_review(subject)
        if (
            action.is_irreversible()
            and self._is_high_sensitivity_or_irreversible(object, action)
            and not bypass_review
        ):
            result["reason"] = "Irreversible high-risk operation requires review"
            self._mark_for_review(result, level="HIGH")
            result["decision"] = "REVIEW_REQUIRED"
            return result

        if object.requires_human_approval() and not bypass_review:
            result["reason"] = "Operation requires human approval"
            self._mark_for_review(result, level="MEDIUM")
            result["decision"] = "REVIEW_REQUIRED"
            return result

        result["allowed"] = True
        result["reason"] = "Default rule allowed"
        result["decision"] = "ALLOW"
        return result

    def _check_scenario_fit(self, object: Object, scenario: Scenario) -> Dict[str, str]:
        fit_result = scenario.get_fit_result()
        if fit_result:
            fit = str(fit_result.get("fit", "uncertain")).lower()
            if fit == "mismatch":
                return {"fit": "mismatch", "reason": fit_result.get("reason", "Scenario fit mismatch")}
            if fit == "match":
                return {"fit": "match", "reason": fit_result.get("reason", "Scenario fit matched")}
            return {"fit": "uncertain", "reason": fit_result.get("reason", "Scenario fit uncertain")}

        expected_capabilities = {item.lower() for item in scenario.get_expected_capabilities()}
        object_capabilities = {item.lower() for item in object.get_expected_capabilities()}
        if expected_capabilities and object_capabilities and expected_capabilities.isdisjoint(object_capabilities):
            return {
                "fit": "mismatch",
                "reason": (
                    f"Task scenario expects capabilities {sorted(expected_capabilities)}, "
                    f"but object provides {sorted(object_capabilities)}"
                ),
            }

        scenario_tags = {item.lower() for item in scenario.get_scenario_tags()}
        object_tags = {item.lower() for item in object.get_scenario_tags()}
        if scenario_tags and object_tags and scenario_tags.isdisjoint(object_tags):
            return {
                "fit": "mismatch",
                "reason": (
                    f"Task scenario tags {sorted(scenario_tags)} do not align with object tags "
                    f"{sorted(object_tags)}"
                ),
            }

        if expected_capabilities or scenario_tags:
            return {"fit": "match", "reason": "Scenario heuristics matched object domain"}
        return {"fit": "uncertain", "reason": "Scenario information is incomplete"}

    @staticmethod
    def _is_high_sensitivity_or_irreversible(object: Object, action: Action) -> bool:
        return object.get_sensitivity() in {"HIGH", "CRITICAL"} or action.is_irreversible()

    @staticmethod
    def _mark_for_review(result: Dict[str, Any], level: str = "MEDIUM") -> None:
        result["human_review_required"] = True
        result["approval_level"] = level

    @staticmethod
    def _finalize_result(result: Dict[str, Any]) -> None:
        if result.get("allowed"):
            result["decision"] = "ALLOW"
        elif result.get("human_review_required"):
            result["decision"] = "REVIEW_REQUIRED"
        else:
            result["decision"] = "DENY"

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

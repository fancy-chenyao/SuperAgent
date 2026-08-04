#!/usr/bin/env python
"""HR Assistant Agent - handles employee info and salary queries."""

from typing import Any, Dict, List
import logging
import json

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.contracts.agent_result import AgentResultError

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)

_SALARY_INTENT_TERMS = (
    "薪资",
    "工资",
    "薪酬",
    "薪水",
    "收入",
    "报酬",
    "salary",
    "payroll",
    "compensation",
    "income",
)
_EXECUTION_CONTEXT_PREFIX = "EXECUTION_CONTEXT"


def _salary_requested(messages: List[Dict[str, Any]]) -> bool:
    """Detect salary intent from the current execution brief or latest user turn.

    The remote Agent is commonly configured with both person and salary tools.
    Tool availability is not user intent, so never query salary merely because
    the salary tool is present.
    """
    for message in reversed(messages or []):
        content = message.get("content", "") if isinstance(message, dict) else ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        stripped = content.strip()
        if stripped.startswith(_EXECUTION_CONTEXT_PREFIX):
            _, _, raw = stripped.partition("\n")
            try:
                brief = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                brief = {}
            step = brief.get("step") if isinstance(brief, dict) else {}
            current_text = " ".join(
                str(value)
                for value in (
                    brief.get("original_user_query") if isinstance(brief, dict) else "",
                    step.get("title") if isinstance(step, dict) else "",
                    step.get("description") if isinstance(step, dict) else "",
                )
                if value
            ).lower()
            return any(term in current_text for term in _SALARY_INTENT_TERMS)

    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or message.get("type") or "").lower() not in {
            "user",
            "human",
        }:
            continue
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        lowered = content.lower()
        if lowered.strip() in {"execute the confirmed plan.", "execute the confirmed plan"}:
            continue
        return any(term in lowered for term in _SALARY_INTENT_TERMS)
    return False


class RemoteHRAssistantAgent(BaseRemoteAgent):
    """
    HR Assistant Agent that can query both person info and salary info,
    then merge the results intelligently.
    """

    def __init__(self):
        super().__init__(
            name="RemoteHRAssistantAgent",
            prompt="You are an HR assistant that helps query employee information and salary data.",
            contract=AgentContract(
                contract_version="1.0",
                produces=[
                    DataContractRef(
                        name="employee.info",
                        schema_ref="employee.info@v1",
                    ),
                    DataContractRef(
                        name="employee.salary",
                        schema_ref="employee.salary@v1",
                        required=False,
                    ),
                ],
            ),
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """
        Execute HR assistant logic:
        1. Extract parameters for each tool
        2. Call all tools in parallel or sequence
        3. Merge results intelligently
        """
        logger.info(f"[{self.name}] Starting execution with {len(tools)} tools")

        # Separate tools by type
        person_tool = None
        salary_tool = None

        for tool in tools:
            tool_name = tool.get("name", "")
            logger.info(f"[{self.name}] Found tool: {tool_name}")
            if tool_name == "remote_person_info_tool":
                person_tool = tool
            elif tool_name == "remote_salary_info_tool":
                salary_tool = tool

        logger.info(f"[{self.name}] person_tool: {person_tool is not None}, salary_tool: {salary_tool is not None}")

        results = {}
        errors: list[AgentResultError] = []
        person_query = None
        salary_requested = _salary_requested(messages)

        # Step 1: Query person info if requested
        if person_tool:
            try:
                logger.info(f"[{self.name}] Extracting parameters for person info tool")
                person_params = await parameter_extractor.extract(
                    agent_name=self.name,
                    agent_prompt=self.prompt,
                    tool=person_tool,
                    messages=messages
                )
                person_query = person_params.get("keyword")
                logger.info(f"[{self.name}] Person params: {json.dumps(person_params, ensure_ascii=False)}")

                logger.info(f"[{self.name}] Calling person info tool")
                person_result = await self.call_tool(
                    tool_name="remote_person_info_tool",
                    arguments=person_params,
                )

                # Extract person list from the result
                # remote_person_info_tool returns: {"status": "success", "detail": {"personInfoList": [...]}}
                if isinstance(person_result, dict):
                    if "detail" in person_result and "personInfoList" in person_result["detail"]:
                        person_list = person_result["detail"]["personInfoList"]
                    elif "personInfoList" in person_result:
                        person_list = person_result["personInfoList"]
                    else:
                        person_list = [person_result]  # Single record
                elif isinstance(person_result, list):
                    person_list = person_result
                else:
                    person_list = []

                results["person_info"] = person_list
                results["person_info_raw"] = person_result  # Keep raw result for reference
                logger.info(f"[{self.name}] Person info retrieved: {len(person_list)} records")

            except Exception as e:
                logger.error(f"[{self.name}] Person info query failed: {e}")
                errors.append(
                    self.execution_error(e, tool_name="remote_person_info_tool")
                )

        # Step 2: Query salary info if requested
        if salary_tool and salary_requested:
            logger.info(f"[{self.name}] ===== SALARY TOOL SECTION STARTED =====")
            try:
                logger.info(f"[{self.name}] Extracting parameters for salary info tool")
                salary_params = await parameter_extractor.extract(
                    agent_name=self.name,
                    agent_prompt=self.prompt,
                    tool=salary_tool,
                    messages=messages
                )
                logger.info(f"[{self.name}] Salary params: {json.dumps(salary_params, ensure_ascii=False)}")

                # Keep the salary lookup on the scheduler-approved employee
                # binding. IDs learned inside the remote Agent must not replace
                # the employee_name/employee_id that was reviewed upstream.

                logger.info(f"[{self.name}] Calling salary info tool")
                salary_result = await self.call_tool(
                    tool_name="remote_salary_info_tool",
                    arguments=salary_params,
                )

                # Extract salary list from the result
                if isinstance(salary_result, dict):
                    if "salary_records" in salary_result:
                        salary_list = salary_result["salary_records"]
                    else:
                        salary_list = [salary_result]  # Single record
                elif isinstance(salary_result, list):
                    salary_list = salary_result
                else:
                    salary_list = []

                results["salary_info"] = salary_list
                results["salary_info_raw"] = salary_result  # Keep raw result
                logger.info(f"[{self.name}] Salary info retrieved: {len(salary_list)} records")

            except Exception as e:
                logger.error(f"[{self.name}] ===== SALARY QUERY FAILED =====")
                logger.error(f"[{self.name}] Salary info query failed: {e}")
                import traceback
                logger.error(f"[{self.name}] Traceback: {traceback.format_exc()}")
                errors.append(
                    self.execution_error(e, tool_name="remote_salary_info_tool")
                )
        elif salary_tool:
            logger.info(
                f"[{self.name}] Salary tool available but skipped: no salary intent"
            )
        else:
            logger.warning(f"[{self.name}] ===== NO SALARY TOOL FOUND =====")

        outputs: dict[str, Any] = {}
        if "person_info" in results:
            employee_info: dict[str, Any] = {
                "records": results["person_info"],
                "matched_count": len(results["person_info"]),
            }
            if isinstance(person_query, str):
                employee_info["query"] = person_query
            outputs["employee.info"] = employee_info
        if "salary_info" in results:
            outputs["employee.salary"] = {
                "records": results["salary_info"],
                "matched_count": len(results["salary_info"]),
            }

        error = None
        if errors:
            error = errors[0]
            if len(errors) > 1:
                error = AgentResultError(
                    code="MULTIPLE_REMOTE_TOOL_ERRORS",
                    message="Multiple HR tools failed",
                    retryable=any(item.retryable for item in errors),
                    details={
                        "errors": [item.model_dump(mode="json") for item in errors]
                    },
                )
        elif not outputs:
            error = AgentResultError(
                code="NO_DATA_RETRIEVED",
                message="No HR data was retrieved",
                retryable=False,
            )
        return self.result_envelope(outputs=outputs, error=error)

    def _merge_person_and_salary(
        self,
        person_data: Any,
        salary_data: Any
    ) -> List[Dict[str, Any]]:
        """
        Merge person info and salary info by employee_id.

        Args:
            person_data: Person query result (list or dict)
            salary_data: Salary query result (list or dict)

        Returns:
            List of merged records
        """
        # Normalize to lists
        persons = person_data if isinstance(person_data, list) else [person_data]
        salaries = salary_data if isinstance(salary_data, list) else [salary_data]

        # Build salary lookup by employee_id (try multiple field names)
        salary_map = {}
        for sal in salaries:
            if not isinstance(sal, dict):
                continue
            # Try multiple possible field names
            emp_id = sal.get("employee_id") or sal.get("idvId") or sal.get("empeInfBtlmprBtnc")
            if emp_id:
                salary_map[str(emp_id)] = sal

        # Merge
        merged = []
        for person in persons:
            if not isinstance(person, dict):
                continue

            # Try multiple possible field names for employee_id
            employee_id = person.get("employee_id") or person.get("idvId") or person.get("empeInfBtlmprBtnc")
            if employee_id:
                employee_id = str(employee_id)

            merged_record = {**person}  # Start with person data

            # Add salary data if available
            if employee_id and employee_id in salary_map:
                salary = salary_map[employee_id]
                merged_record.update({
                    "monthly_salary": salary.get("monthly_salary"),
                    "annual_salary": salary.get("annual_salary"),
                    "salary_breakdown": salary.get("salary_breakdown"),
                    "currency": salary.get("currency"),
                    "salary_last_updated": salary.get("last_updated"),
                })

            merged.append(merged_record)

        return merged

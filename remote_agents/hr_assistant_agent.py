#!/usr/bin/env python
"""HR Assistant Agent - handles employee info and salary queries."""

from typing import Any, Dict, List
import logging
import json

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


class RemoteHRAssistantAgent(BaseRemoteAgent):
    """
    HR Assistant Agent that can query both person info and salary info,
    then merge the results intelligently.
    """

    def __init__(self):
        super().__init__(
            name="RemoteHRAssistantAgent",
            prompt="You are an HR assistant that helps query employee information and salary data."
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
                logger.info(f"[{self.name}] Person params: {json.dumps(person_params, ensure_ascii=False)}")

                logger.info(f"[{self.name}] Calling person info tool")
                person_result = await self.call_tool(
                    tool_name="remote_person_info_tool",
                    arguments=person_params,
                    timeout=10
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
                results["person_info_error"] = str(e)

        # Step 2: Query salary info if requested
        if salary_tool:
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

                # If we have person results, use employee IDs from them
                if "person_info" in results and isinstance(results["person_info"], list):
                    # Try multiple possible field names for employee_id
                    employee_ids = []
                    for p in results["person_info"]:
                        if not isinstance(p, dict):
                            continue
                        emp_id = p.get("employee_id") or p.get("idvId") or p.get("empeInfBtlmprBtnc")
                        if emp_id:
                            employee_ids.append(str(emp_id))

                    if employee_ids:
                        salary_params["employee_id_list"] = employee_ids
                        logger.info(f"[{self.name}] Using employee IDs from person query: {employee_ids}")

                logger.info(f"[{self.name}] Calling salary info tool")
                salary_result = await self.call_tool(
                    tool_name="remote_salary_info_tool",
                    arguments=salary_params,
                    timeout=10
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
                results["salary_info_error"] = str(e)
        else:
            logger.warning(f"[{self.name}] ===== NO SALARY TOOL FOUND =====")

        # Step 3: Merge results if both are available
        if "person_info" in results and "salary_info" in results:
            merged = self._merge_person_and_salary(
                results["person_info"],
                results["salary_info"]
            )
            logger.info(f"[{self.name}] Merged {len(merged) if isinstance(merged, list) else 1} complete records")
            return merged

        # Return whatever we have
        if "person_info" in results:
            return results["person_info"]
        if "salary_info" in results:
            return results["salary_info"]

        # No results
        return {
            "error": "No data retrieved",
            "details": results
        }

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

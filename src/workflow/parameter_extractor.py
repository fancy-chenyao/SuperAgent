"""
Parameter extraction logic for Publisher node.

This module handles extracting parameters for agents based on their requirements
and the data flow defined in the plan.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def find_step_result(messages: List[Dict[str, Any]], step_name: str) -> Optional[Dict[str, Any]]:
    """
    Find the execution result of a specific step from message history.

    Args:
        messages: List of message dictionaries
        step_name: The agent_name of the step to find

    Returns:
        The result dictionary if found, None otherwise
    """
    # Special virtual source: user_instruction
    if step_name == "user_instruction":
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, dict) and content.get("role") == "user":
                query_text = content.get("content")
                if query_text:
                    return {"query_text": query_text}
            if isinstance(content, str) and content.strip():
                # Prefer the first human/user string message as user instruction
                msg_type = msg.get("type")
                if msg_type == "human":
                    return {"query_text": content}
        return None

    # Search backwards for the most recent result from this step
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue

        # Check if this message is from the target step
        tool = msg.get("tool")
        if tool != step_name:
            continue

        # Try to extract the result
        content = msg.get("content")
        if not content:
            continue

        # If content is already a dict, return it
        if isinstance(content, dict):
            return content

        # If content is a string, try to parse it as JSON
        if isinstance(content, str):
            try:
                result = json.loads(content)
                if isinstance(result, dict):
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

    # Also check for STRUCTURED_RESULT_JSON markers
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue

        content = msg.get("content")
        if not isinstance(content, str):
            continue

        # Look for structured result marker
        marker = "STRUCTURED_RESULT_JSON:"
        if marker in content:
            try:
                # Extract JSON after marker
                json_start = content.index(marker) + len(marker)
                json_str = content[json_start:].strip()

                # Parse the JSON
                structured = json.loads(json_str)
                if isinstance(structured, dict) and structured.get("tool") == step_name:
                    return structured.get("result")
            except (ValueError, json.JSONDecodeError):
                pass

    return None


def extract_value_from_result(result: Dict[str, Any], output_name: str) -> Optional[Any]:
    """
    Extract a specific output value from a step's result.

    This function uses heuristics to find the requested output in the result structure.

    Args:
        result: The result dictionary from a step
        output_name: The output name to extract (e.g., "person.email", "report.markdown")

    Returns:
        The extracted value if found, None otherwise
    """
    if not isinstance(result, dict):
        return None

    # Strategy 1: Direct key match
    if output_name in result:
        return result[output_name]

    # Strategy 2: Dot notation path (e.g., "person.email" -> result["person"]["email"])
    if "." in output_name:
        parts = output_name.split(".")
        current = result
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                current = None
                break
        if current is not None:
            return current

    # Strategy 3: Semantic matching based on output name
    # For "person.email" or "email.to", look for email-like fields
    if "email" in output_name.lower():
        # Search for email fields in common locations
        candidates = [
            result.get("email"),
            result.get("to"),
            result.get("recipient"),
        ]

        # Also check nested structures
        if "detail" in result and isinstance(result["detail"], dict):
            detail = result["detail"]
            if "personInfoList" in detail and isinstance(detail["personInfoList"], list):
                if len(detail["personInfoList"]) > 0:
                    person = detail["personInfoList"][0]
                    if isinstance(person, dict):
                        candidates.append(person.get("internalMaiBox"))
                        candidates.append(person.get("email"))

        # Return first non-None candidate
        for candidate in candidates:
            if candidate is not None:
                return candidate

    # For "report.markdown" or "markdown", look for markdown/report fields
    if "markdown" in output_name.lower() or "report" in output_name.lower():
        candidates = [
            result.get("markdown"),
            result.get("report"),
            result.get("content"),
        ]

        for candidate in candidates:
            if candidate is not None:
                return candidate

    # For "unicorn.list" or "list", look for list/records fields
    if "list" in output_name.lower() or "records" in output_name.lower():
        candidates = [
            result.get("records"),
            result.get("list"),
            result.get("data"),
        ]

        for candidate in candidates:
            if candidate is not None:
                return candidate

    # For "risk.metrics" or "metrics", look for records/data/metrics fields
    if "metrics" in output_name.lower() or "risk" in output_name.lower():
        candidates = [
            result.get("records"),
            result.get("metrics"),
            result.get("data"),
            result.get("list"),
        ]

        for candidate in candidates:
            if candidate is not None:
                return candidate

    # Strategy 4: If result has a single top-level key, try that
    if len(result) == 1:
        return list(result.values())[0]

    logger.warning(f"Could not extract '{output_name}' from result: {result}")
    return None


def extract_parameters_for_step(
    current_step: Dict[str, Any],
    agent_requires: List[str],
    messages: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Extract parameters for a step based on its input mappings.

    Args:
        current_step: The step dictionary from the plan (contains inputs field)
        agent_requires: List of parameter names the agent requires
        messages: Message history containing previous steps' results

    Returns:
        Dictionary mapping parameter names to extracted values

    Raises:
        ValueError: If a required parameter cannot be extracted
    """
    parameters = {}
    inputs = current_step.get("inputs", [])

    # If no inputs specified but agent requires parameters, log warning
    if not inputs and agent_requires:
        logger.warning(
            f"Step '{current_step.get('agent_name')}' requires {agent_requires} "
            f"but no input mappings specified in plan"
        )
        return parameters

    # Process each input mapping
    for input_mapping in inputs:
        if not isinstance(input_mapping, dict):
            continue

        param_name = input_mapping.get("parameter_name")
        source_step = input_mapping.get("source_step")
        source_output = input_mapping.get("source_output")
        description = input_mapping.get("description", "")

        if not param_name or not source_step or not source_output:
            logger.warning(f"Incomplete input mapping: {input_mapping}")
            continue

        # Find the source step's result
        source_result = find_step_result(messages, source_step)
        if source_result is None:
            logger.error(
                f"Cannot find result from source step '{source_step}' "
                f"for parameter '{param_name}'"
            )
            continue

        # Extract the specific output value
        value = extract_value_from_result(source_result, source_output)
        if value is None:
            logger.error(
                f"Cannot extract '{source_output}' from step '{source_step}' "
                f"for parameter '{param_name}'"
            )
            continue

        parameters[param_name] = value
        logger.info(
            f"Extracted parameter '{param_name}' = {value} "
            f"from {source_step}.{source_output}"
        )

    # Check if all required parameters were extracted
    missing = set(agent_requires) - set(parameters.keys())
    if missing:
        logger.error(
            f"Missing required parameters: {missing}. "
            f"Extracted: {list(parameters.keys())}"
        )

    return parameters

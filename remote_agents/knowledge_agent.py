#!/usr/bin/env python
"""Knowledge Agent - queries HR policies and labor laws."""

from typing import Any, Dict, List
import logging

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.contracts.agent_result import AgentResultError

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


class RemoteKnowledgeAgent(BaseRemoteAgent):
    """Knowledge Agent for querying HR policies and labor laws."""

    def __init__(self):
        super().__init__(
            name="RemoteKnowledgeAgent",
            prompt="You are a knowledge assistant that helps query HR policies and labor laws.",
            contract=AgentContract(
                contract_version="1.0",
                produces=[
                    DataContractRef(
                        name="policy.info",
                        schema_ref="policy.info@v1",
                    )
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
        """Execute knowledge query - single tool agent."""
        if not tools or len(tools) == 0:
            return self.result_envelope(
                error=AgentResultError(
                    code="NO_TOOL_PROVIDED",
                    message="No knowledge tool was provided",
                    retryable=False,
                )
            )

        tool = tools[0]
        tool_name = tool.get("name", "unknown")

        try:
            logger.info(f"[{self.name}] Extracting parameters for {tool_name}")
            arguments = await parameter_extractor.extract(
                agent_name=self.name,
                agent_prompt=self.prompt,
                tool=tool,
                messages=messages
            )

            logger.info(f"[{self.name}] Calling {tool_name}")
            result = await self.call_tool(
                tool_name=tool_name,
                arguments=arguments,
                timeout=60  # Knowledge queries may take longer
            )
            policy_scope = str(result.get("policy_scope") or "statutory")
            if policy_scope not in {"company", "statutory", "mixed", "unknown"}:
                policy_scope = "unknown"
            payload = {
                "query": str(result.get("query") or arguments.get("query") or ""),
                "answer": str(result.get("answer") or ""),
                "knowledge_items_count": int(
                    result.get("knowledge_items_count") or 0
                ),
                "policy_scope": policy_scope,
            }
            sources = result.get("sources")
            if isinstance(sources, list):
                payload["sources"] = [
                    source for source in sources if isinstance(source, dict)
                ]
            matched_items = result.get("matched_items")
            if isinstance(matched_items, list):
                payload["matched_items"] = [
                    str(item) for item in matched_items if item is not None
                ]
            if "not_found" in result:
                not_found = result.get("not_found")
                if not isinstance(not_found, bool):
                    raise TypeError(
                        "knowledge_search_tool returned contract-invalid "
                        f"not_found: expected bool, got {type(not_found).__name__}"
                    )
                payload["not_found"] = not_found
            return self.result_envelope(outputs={"policy.info": payload})
        except Exception as exc:
            return self.result_envelope(
                error=self.execution_error(exc, tool_name=tool_name)
            )

#!/usr/bin/env python
"""Mock remote agent server for testing RemoteExecutor."""

from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Header
from pydantic import BaseModel
import httpx
import json
import re
import datetime
import os
import logging
from openai import AsyncOpenAI
from dotenv import load_dotenv

# 加载.env文件
load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI()


class RemoteRequest(BaseModel):
    agent_name: str
    messages: List[Dict[str, Any]]
    context: Dict[str, Any]
    prompt: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None


class RemoteAgentConfig(BaseModel):
    """远程Agent配置"""
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_temperature: float = 0.1
    llm_max_tokens: int = 2000
    extraction_timeout: int = 30


def load_config() -> RemoteAgentConfig:
    """从环境变量加载配置"""
    api_key = os.getenv("REMOTE_API_KEY", "")
    base_url = os.getenv("REMOTE_BASE_URL", "")
    model = os.getenv("REMOTE_MODEL", "deepseek-v3.2")

    # 验证必需配置
    if not api_key:
        raise ValueError("REMOTE_API_KEY is not set in environment variables")
    if not base_url:
        raise ValueError("REMOTE_BASE_URL is not set in environment variables")

    # 确保base_url有协议前缀
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"REMOTE_BASE_URL must start with http:// or https://, got: {base_url}")

    logger.info(f"Loaded remote agent config: model={model}, base_url={base_url}")

    return RemoteAgentConfig(
        llm_api_key=api_key,
        llm_base_url=base_url,
        llm_model=model,
        llm_temperature=float(os.getenv("REMOTE_LLM_TEMPERATURE", "0.1")),
        llm_max_tokens=int(os.getenv("REMOTE_LLM_MAX_TOKENS", "2000")),
        extraction_timeout=int(os.getenv("REMOTE_EXTRACTION_TIMEOUT", "30")),
    )


class LLMParameterExtractor:
    """使用LLM从消息历史中提取工具参数"""

    def __init__(self, config: RemoteAgentConfig):
        self.config = config
        self.llm_client = AsyncOpenAI(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
        )

    async def extract(
        self,
        agent_name: str,
        agent_prompt: str,
        tool: Dict[str, Any],
        messages: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        从消息历史中提取工具参数

        Args:
            agent_name: Agent名称
            agent_prompt: Agent的系统提示词
            tool: 工具定义 {"name": "...", "description": "..."}
            messages: 消息历史

        Returns:
            提取的参数字典
        """
        try:
            # 1. 构建提取prompt
            extraction_prompt = self._build_extraction_prompt(
                agent_name, agent_prompt, tool, messages
            )

            # 2. 调用LLM
            llm_response = await self._call_llm(extraction_prompt)

            # 3. 解析响应
            parameters = self._parse_llm_response(llm_response)

            # 4. 验证参数
            validated_params = self._validate_parameters(parameters)

            return validated_params

        except Exception as e:
            logger.error(f"Parameter extraction failed: {e}")
            raise

    def _build_extraction_prompt(
        self,
        agent_name: str,
        agent_prompt: str,
        tool: Dict[str, Any],
        messages: List[Dict[str, Any]]
    ) -> str:
        """构建参数提取的prompt"""
        # 格式化消息历史
        formatted_messages = self._format_messages(messages)

        # 工具信息
        tool_name = tool.get("name", "unknown")
        tool_desc = tool.get("description", "No description")
        tool_params = tool.get("parameters", {})

        # 格式化参数schema
        schema_str = ""
        if tool_params:
            schema_str = f"\n\nTool Parameters Schema:\n```json\n{json.dumps(tool_params, indent=2, ensure_ascii=False)}\n```"

        prompt = f"""You are {agent_name}, a specialized AI agent.

Your role: {agent_prompt}

Task: Extract the parameters needed to call the following tool based on the conversation history.

Tool Information:
- Name: {tool_name}
- Description: {tool_desc}{schema_str}

Conversation History:
{formatted_messages}

Instructions:
1. Analyze the conversation history carefully
2. Extract all necessary parameters for the tool according to the schema above
3. If a parameter is mentioned in multiple messages, use the most recent value
4. If a parameter comes from a previous agent's result, extract it from the message content (look for JSON objects in the messages)
5. Follow the parameter types and requirements defined in the schema
6. For array parameters, extract all relevant items from the conversation
7. Output ONLY a valid JSON object with the extracted parameters

Output Format:
{{
    "parameter_name": "extracted_value",
    ...
}}

CRITICAL Requirements:
- Output ONLY the JSON object, no additional text or explanation
- Match the parameter names EXACTLY as defined in the schema
- Use the correct data types (string, number, array, object) as specified in the schema
- Include all REQUIRED parameters from the schema
- Omit optional parameters if they cannot be found in the conversation
- Ensure all values are properly formatted (strings, numbers, arrays, objects, etc.)
"""

        return prompt

    def _format_messages(self, messages: List[Dict[str, Any]]) -> str:
        """
        格式化消息历史

        输出格式:
        [Message 1] User: 查询雄安新区的天气
        [Message 2] Agent(RemotePersonInfoAgent): {"name": "张三", "email": "zhang@example.com"}
        """
        formatted = []

        for idx, msg in enumerate(messages, 1):
            msg_type = msg.get("type", "unknown")
            content = msg.get("content", "")
            tool = msg.get("tool", "")

            # 格式化content
            if isinstance(content, dict):
                content_str = json.dumps(content, ensure_ascii=False, indent=2)
            else:
                content_str = str(content)

            # 构建消息标签
            if msg_type == "human" or msg_type == "user":
                label = "User"
            elif tool:
                label = f"Agent({tool})"
            else:
                label = "System"

            formatted.append(f"[Message {idx}] {label}: {content_str}")

        return "\n\n".join(formatted)

    async def _call_llm(self, prompt: str) -> str:
        """调用LLM API"""
        try:
            logger.info(f"Calling LLM with model: {self.config.llm_model}")

            response = await self.llm_client.chat.completions.create(
                model=self.config.llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a parameter extraction assistant. Extract parameters from conversation history and output valid JSON."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=self.config.llm_temperature,
                max_tokens=self.config.llm_max_tokens,
            )

            llm_response = response.choices[0].message.content
            return llm_response

        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise

    def _parse_llm_response(self, response: str) -> Dict[str, Any]:
        """解析LLM返回的JSON"""
        if not response:
            return {}

        # 移除可能的markdown代码块
        response = response.strip()

        # 尝试提取JSON
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        elif response.startswith('{') and response.endswith('}'):
            json_str = response
        else:
            # 尝试找到第一个{和最后一个}
            start = response.find('{')
            end = response.rfind('}')
            if start != -1 and end != -1:
                json_str = response[start:end+1]
            else:
                logger.warning(f"Cannot find valid JSON in response: {response}")
                return {}

        # 解析JSON
        try:
            parameters = json.loads(json_str)
            if not isinstance(parameters, dict):
                logger.warning(f"Expected dict, got {type(parameters)}")
                return {}
            return parameters
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}\nResponse: {response}")
            return {}

    def _validate_parameters(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """验证参数 - 移除null值"""
        validated = {k: v for k, v in parameters.items() if v is not None}
        return validated


# 全局初始化
config = load_config()
parameter_extractor = LLMParameterExtractor(config)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/agent")
async def agent(req: RemoteRequest, authorization: Optional[str] = Header(default=None)):
    """
    远程Agent执行endpoint - 使用LLM提取参数

    流程:
    1. 接收请求
    2. 使用LLM从消息历史中提取参数
    3. 调用工具
    4. 返回结果
    """
    logger.info(f"Received request for agent: {req.agent_name}, message count: {len(req.messages)}")

    # 检查是否有工具需要调用
    if not req.tools or len(req.tools) == 0:
        logger.warning("No tools specified")
        return {
            "status": "failed",
            "error": "No tools specified for agent",
            "metadata": {
                "agent_name": req.agent_name,
                "message_count": len(req.messages),
            }
        }

    tool_def = req.tools[0]
    tool_name = tool_def.get("name", "unknown")
    logger.info(f"Tool to call: {tool_name}")

    try:
        # 使用LLM提取参数
        logger.info("Extracting parameters using LLM...")

        arguments = await parameter_extractor.extract(
            agent_name=req.agent_name,
            agent_prompt=req.prompt or "",
            tool=tool_def,
            messages=req.messages
        )

        logger.info(f"Extracted parameters: {list(arguments.keys()) if isinstance(arguments, dict) else 'N/A'}")

        # 检查是否为空
        if not arguments or (isinstance(arguments, dict) and len(arguments) == 0):
            logger.warning("Extracted parameters is empty")

        # 调用工具
        logger.info(f"Calling tool: {tool_name}")
        timeout_seconds = 60 if req.agent_name in {"RemoteReportAgent", "RemoteKnowledgeAgent"} else 10

        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds)) as client:
            resp = await client.post(
                "http://127.0.0.1:8011/tool",
                json={"tool": tool_name, "arguments": arguments},
                headers={"Content-Type": "application/json"},
            )
            tool_result = resp.json().get("result")
            logger.info("Tool call succeeded")

            # 完整展示返回结果
            if isinstance(tool_result, dict):
                logger.info(f"Tool result: {json.dumps(tool_result, ensure_ascii=False, indent=2)}")
            else:
                logger.info(f"Tool result: {tool_result}")

        # 返回结果
        return {
            "status": "success",
            "result": tool_result,
            "metadata": {
                "agent_name": req.agent_name,
                "tool_called": tool_name,
                "has_auth": bool(authorization),
                "message_count": len(req.messages),
                "extracted_params": list(arguments.keys()) if isinstance(arguments, dict) else [],
            },
        }

    except Exception as e:
        import traceback
        error_msg = str(e) or f"{type(e).__name__}: (empty error message)"
        logger.error(f"Error [{type(e).__name__}]: {error_msg}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")

        return {
            "status": "failed",
            "error": error_msg,
            "metadata": {
                "agent_name": req.agent_name,
                "tool": tool_name,
                "has_auth": bool(authorization),
                "message_count": len(req.messages),
            },
            "traceback": traceback.format_exc()
        }



if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)

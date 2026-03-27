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
import traceback
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Import agent factory
from remote_agents.factory import AgentFactory

# 加载.env文件
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Set log level for remote_agents module
logging.getLogger('remote_agents').setLevel(logging.INFO)
logging.getLogger('remote_agents.hr_assistant_agent').setLevel(logging.INFO)
logging.getLogger('remote_agents.base_agent').setLevel(logging.INFO)

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

    async def select_tool_and_extract(
        self,
        agent_name: str,
        agent_prompt: str,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """当Agent配置了多个工具时，先选择工具，再提取参数。"""
        selection_prompt = self._build_tool_selection_prompt(
            agent_name=agent_name,
            agent_prompt=agent_prompt,
            tools=tools,
            messages=messages,
        )
        llm_response = await self._call_llm(selection_prompt)
        selection = self._parse_llm_response(llm_response)
        selected_tool_name = selection.get("tool_name")
        arguments = selection.get("arguments", {})

        if not isinstance(arguments, dict):
            arguments = {}

        for tool in tools:
            if tool.get("name") == selected_tool_name:
                return tool, self._validate_parameters(arguments)

        available_tools = [tool.get("name", "") for tool in tools]
        raise ValueError(
            f"Selected tool '{selected_tool_name}' is invalid. Available tools: {available_tools}"
        )

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

Special Extraction Rules for {tool_name}:
- For age/generation expressions like "80后", "90后", "70后", convert to birth_year_range:
  * "80后" -> "birth_year_range": [1980, 1989]
  * "90后" -> "birth_year_range": [1990, 1999]
  * "70后" -> "birth_year_range": [1970, 1979]
- For job title queries:
  * IMPORTANT: Use "keyword" for complete job titles like "行长秘书", "副行长", "支行行长"
  * ONLY use "job_keywords" array when user explicitly wants OR matching (e.g., "行长或秘书")
  * Complete job titles should NOT be split into separate keywords
- For compound organization names, split into key components:
  * "二级分支行" -> "org_keywords": ["二级", "分行"]
  * "一级支行" -> "org_keywords": ["一级", "支行"]
- For document generation tools (remote_docx_generator_tool):
  * Extract employee data from previous agent results in the conversation history
  * Look for employee information in JSON format from RemoteHRAssistantAgent
  * The "data" parameter should contain: name, id_number, position, join_date, monthly_salary, annual_salary
  * Extract these fields from the employee record in previous messages
  * If generating income_proof, use template_name="income_proof"
  * If generating employment_certificate, use template_name="employment_certificate"

Examples:
User: "查询二级分支行80后行长"
Output: {{"keyword": "行长", "org_keywords": ["二级", "分行"], "birth_year_range": [1980, 1989]}}

User: "找一下90后的女性经理"
Output: {{"keyword": "经理", "gender": "女", "birth_year_range": [1990, 1999]}}

User: "查询行长秘书"
Output: {{"keyword": "行长秘书"}}

User: "查询行长或秘书"
Output: {{"job_keywords": ["行长", "秘书"]}}

User: "帮王强开买房用的个人收入证明"
Previous Agent Result: {{"adtEmpeNm": "王强", "idvId": "86000103", "tcoPostNm": "支行行长", "jnUnitDt": "2005-07-01", "monthly_salary": 28000.0, "annual_salary": 336000.0}}
Output: {{"template_name": "income_proof", "data": {{"name": "王强", "id_number": "86000103", "position": "支行行长", "join_date": "2005年7月1日", "monthly_salary": "28000.00", "annual_salary": "336000.00"}}, "output_filename": "income_proof_王强_20260326"}}

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

    def _build_tool_selection_prompt(
        self,
        agent_name: str,
        agent_prompt: str,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
    ) -> str:
        """构建多工具选择与参数提取 prompt。"""
        formatted_messages = self._format_messages(messages)

        tool_blocks = []
        for tool in tools:
            tool_name = tool.get("name", "unknown")
            tool_desc = tool.get("description", "No description")
            tool_params = tool.get("parameters", {})
            schema_str = json.dumps(tool_params, indent=2, ensure_ascii=False) if tool_params else "{}"
            tool_blocks.append(
                f"- Tool Name: {tool_name}\n"
                f"  Description: {tool_desc}\n"
                f"  Parameters Schema:\n```json\n{schema_str}\n```"
            )

        tools_text = "\n\n".join(tool_blocks)

        prompt = f"""You are {agent_name}, a specialized AI agent.

Your role: {agent_prompt}

Task: Based on the conversation history, choose the single best tool to use and extract the parameters needed for that tool.

Available Tools:
{tools_text}

Conversation History:
{formatted_messages}

Instructions:
1. Analyze the user's latest intent carefully.
2. Choose exactly ONE best matching tool.
3. Extract the parameters needed by the chosen tool according to its schema.
4. If a parameter is mentioned in multiple messages, use the most recent value.
5. If a parameter is required by the schema, you must provide it whenever it can be reasonably inferred from the conversation.
6. For date calculations, follow the agent instructions strictly.
7. Output ONLY a valid JSON object in the following format.

Output Format:
{{
  "tool_name": "chosen_tool_name",
  "arguments": {{
    "parameter_name": "value"
  }}
}}

CRITICAL Requirements:
- Output ONLY the JSON object, no additional text or explanation.
- tool_name must exactly match one of the available tools.
- arguments must be an object.
- Match parameter names EXACTLY as defined in the chosen tool schema.
- Use the correct data types for every parameter.
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

        # 尝试提取JSON - 使用更健壮的方法
        # 1. 先尝试移除markdown代码块标记
        if response.startswith('```json'):
            response = response[7:]  # 移除 ```json
        if response.startswith('```'):
            response = response[3:]  # 移除 ```
        if response.endswith('```'):
            response = response[:-3]  # 移除结尾的 ```

        response = response.strip()

        # 2. 现在尝试解析JSON
        if response.startswith('{') and response.endswith('}'):
            json_str = response
        else:
            # 尝试找到第一个{和最后一个}
            start = response.find('{')
            end = response.rfind('}')
            if start != -1 and end != -1:
                json_str = response[start:end+1]
            else:
                logger.warning(f"Cannot find valid JSON in response: {response[:200]}...")
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


def _render_agent_prompt(agent_prompt: str) -> str:
    """替换提示词中的时间占位符。"""
    now = datetime.datetime.now()
    current_time = now.strftime("%Y-%m-%d %H:%M:%S")
    current_date = now.strftime("%Y-%m-%d")
    return (
        agent_prompt
        .replace("<<CURRENT_TIME>>", current_time)
        .replace("<<CURRENT_DATE>>", current_date)
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/agent")
async def agent(req: RemoteRequest, authorization: Optional[str] = Header(default=None)):
    """
    远程Agent执行endpoint - 使用模块化Agent架构

    流程:
    1. 接收请求
    2. 根据agent_name获取对应的Agent实例
    3. Agent内部处理多工具调用和结果合并
    4. 返回结果
    """
    logger.info(f"Received request for agent: {req.agent_name}, message count: {len(req.messages)}, tools: {len(req.tools or [])}")
    if req.tools:
        logger.info(f"Tools list: {[t.get('name') for t in req.tools]}")

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

    try:
        # 获取Agent实例
        agent = AgentFactory.get_agent(req.agent_name)
        logger.info(f"Using agent: {agent.__class__.__name__}")

        # 执行Agent
        result = await agent.execute(
            tools=req.tools,
            messages=req.messages,
            context=req.context,
            parameter_extractor=parameter_extractor
        )

        # 返回结果
        return {
            "status": "success",
            "result": result,
            "metadata": {
                "agent_name": req.agent_name,
                "agent_class": agent.__class__.__name__,
                "tools_count": len(req.tools),
                "has_auth": bool(authorization),
                "message_count": len(req.messages),
            },
        }

    except ValueError as e:
        # Agent not found
        logger.error(f"Agent not found: {e}")
        return {
            "status": "failed",
            "error": str(e),
            "metadata": {
                "agent_name": req.agent_name,
                "has_auth": bool(authorization),
                "message_count": len(req.messages),
            },
        }

    except Exception as e:
        error_msg = str(e) or f"{type(e).__name__}: (empty error message)"
        logger.error(f"Error [{type(e).__name__}]: {error_msg}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")

        return {
            "status": "failed",
            "error": error_msg,
            "metadata": {
                "agent_name": req.agent_name,
                "has_auth": bool(authorization),
                "message_count": len(req.messages),
            },
            "traceback": traceback.format_exc()
        }



if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)

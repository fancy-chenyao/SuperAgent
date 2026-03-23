#!/usr/bin/env python
"""Mock remote agent server for testing RemoteExecutor."""

from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Header
from pydantic import BaseModel
import httpx
import json
import re
import datetime

app = FastAPI()


class RemoteRequest(BaseModel):
    agent_name: str
    messages: List[Dict[str, Any]]
    context: Dict[str, Any]
    prompt: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/agent")
async def agent(req: RemoteRequest, authorization: Optional[str] = Header(default=None)):
    # 检查是否收到了干净的参数（新架构）
    clean_params = None
    if req.messages and len(req.messages) > 0:
        first_msg = req.messages[0]
        content = first_msg.get("content", "")

        # 尝试解析为 JSON（新架构发送的干净参数）
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    # 检查是否是参数字典（包含 agent requires 的字段）
                    clean_params = parsed
            except json.JSONDecodeError:
                pass
        elif isinstance(content, dict):
            clean_params = content

    # 如果有干净的参数，直接使用；否则使用旧的提取逻辑
    if clean_params:
        query_message = json.dumps(clean_params, ensure_ascii=False)
        # Don't set arguments here, will be set in tool handling section
    else:
        # 原来的复杂提取逻辑
        query_message = ""
        candidates: List[str] = []

        def _is_publisher_text(text: str) -> bool:
            return ("publisher" in text and "delegating to" in text) or "Next step is delegating to" in text

        # 首先尝试找到 role='user' 或 type='human' 的消息
        for msg in req.messages:
            content = msg.get("content", "")
            msg_type = msg.get("type")

            if msg_type == "human":
                if isinstance(content, dict):
                    query_message = content.get("content", "") or json.dumps(content, ensure_ascii=False)
                else:
                    query_message = str(content)
                break

            if isinstance(content, dict) and content.get("role") == "user":
                # 提取实际的查询内容
                inner_content = content.get("content", "")
                if inner_content:
                    query_message = inner_content
                else:
                    query_message = json.dumps(content, ensure_ascii=False)
                break

            # 记录候选文本用于后续回退
            if isinstance(content, dict) and "content" in content:
                cand = content.get("content", "")
                if isinstance(cand, str) and cand:
                    candidates.append(cand)
            elif isinstance(content, str) and content:
                candidates.append(content)

        # 如果没有找到，尝试找包含业务关键词的候选消息
        if not query_message:
            keywords = ["行长秘书", "秘书", "邮箱", "查询", "选择", "分析"]
            for cand in candidates:
                if _is_publisher_text(cand):
                    continue
                if any(k in cand for k in keywords):
                    query_message = cand
                    break

        # 如果还是没有，使用第一条非 publisher 的候选消息
        if not query_message:
            for cand in candidates:
                if _is_publisher_text(cand):
                    continue
                query_message = cand
                break

        # 如果仍为空，使用第一条消息兜底
        if not query_message:
            first_msg = req.messages[0] if req.messages else {}
            content = first_msg.get("content", "")
            if isinstance(content, dict):
                query_message = content.get("content", "") or json.dumps(content, ensure_ascii=False)
            else:
                query_message = str(content)

        print(f"[REMOTE AGENT] Final query message: {query_message[:200]}...")
        arguments = None  # Will be extracted below

    tool_result = None

    if req.tools:
        tool = req.tools[0].get("name")
        if tool:
            print(f"[REMOTE AGENT] Tool to call: {tool}")
            print(f"[REMOTE AGENT] Has clean_params: {bool(clean_params)}")

            # 如果已经有 clean_params，直接使用；否则提取
            if not clean_params:
                print(f"[REMOTE AGENT] Extracting arguments for tool: {tool}")
                arguments: Dict[str, Any] = {"location": "东太行"}
                if req.agent_name == "RemotePersonInfoAgent" or tool == "remote_person_info_tool":
                    arguments = _extract_person_info_args(query_message)
                    print(f"[REMOTE AGENT] Extracted arguments: {json.dumps(arguments, ensure_ascii=False)}")
                if req.agent_name == "RemoteTodoAgent" or tool == "remote_todo_query_tool":
                    arguments = _extract_todo_args(query_message)
                if req.agent_name == "RemoteUnicornSelectorAgent" or tool == "remote_unicorn_db_tool":
                    arguments = _extract_unicorn_args(query_message)
                if req.agent_name == "RemoteBusinessRiskAgent" or tool == "remote_credit_risk_db_tool":
                    arguments = _extract_risk_args(query_message)
                if req.agent_name == "RemoteReportAgent" or tool == "remote_report_builder_tool":
                    report_source = _build_report_source(req.messages, _is_publisher_text)
                    arguments = _extract_report_args(report_source or query_message)
                if req.agent_name == "RemoteEmailDispatchAgent" or tool == "remote_email_tool":
                    email_source = _build_email_source(req.messages, _is_publisher_text)
                    arguments = _extract_email_args(email_source or query_message)
                if req.agent_name == "RemoteScheduleAgent" or tool == "remote_schedule_tool":
                    arguments = _extract_schedule_args(query_message)
            else:
                print(f"[REMOTE AGENT] ✓ Using clean parameters directly (skipping extraction)")
                arguments = clean_params
                print(f"[REMOTE AGENT] Clean params content: {json.dumps(clean_params, ensure_ascii=False, indent=2)}")

            print(f"[REMOTE AGENT] Final arguments for tool call:")
            print(f"[REMOTE AGENT]   {json.dumps(arguments, ensure_ascii=False, indent=2)}")
            print(f"[REMOTE AGENT] Arguments type: {type(arguments)}")
            print(f"[REMOTE AGENT] Arguments keys: {list(arguments.keys()) if isinstance(arguments, dict) else 'N/A'}")

            try:
                # 增加超时时间，特别是对于需要 LLM 的工具
                timeout = 60 if req.agent_name in {"RemoteReportAgent"} else 10
                print(f"[REMOTE AGENT] Calling tool '{tool}' with timeout={timeout}s")
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        "http://127.0.0.1:8011/tool",
                        json={"tool": tool, "arguments": arguments},
                        headers={"Content-Type": "application/json"},
                    )
                    tool_result = resp.json().get("result")
                    print(f"[REMOTE AGENT] ✓ Tool call succeeded")
            except Exception as exc:
                import traceback
                error_msg = str(exc) if str(exc) else f"{type(exc).__name__}: {repr(exc)}"
                print(f"[REMOTE AGENT] ✗ Tool call failed: {error_msg}")
                tool_result = {
                    "status": "error",
                    "error": error_msg,
                    "tool": tool,
                    "traceback": traceback.format_exc()
                }

    if req.agent_name == "RemotePersonInfoAgent" and tool_result is not None:
        result = tool_result
    elif req.agent_name == "RemoteTodoAgent" and tool_result is not None:
        result = tool_result
    elif req.agent_name in {
        "RemoteUnicornSelectorAgent",
        "RemoteBusinessRiskAgent",
        "RemoteReportAgent",
        "RemoteEmailDispatchAgent",
        "RemoteScheduleAgent",
    } and tool_result is not None:
        result = tool_result
    else:
        result = f"[remote:{req.agent_name}] {query_message if not clean_params else json.dumps(clean_params)}"
        if tool_result:
            result += f" | tool:{tool_result}"

    print(f"[REMOTE AGENT] Returning result (preview): {str(result)[:200]}...")
    print("=" * 80)

    return {
        "status": "success",
        "result": result,
        "metadata": {
            "has_auth": bool(authorization),
            "message_count": len(req.messages),
            "tool_called": bool(tool_result),
        },
    }


def _collect_message_texts(messages: List[Dict[str, Any]], is_publisher_text) -> List[str]:
    texts: List[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, dict):
            inner = content.get("content", "")
            if isinstance(inner, str) and inner:
                texts.append(inner)
            texts.append(json.dumps(content, ensure_ascii=False))
        else:
            if content is not None:
                texts.append(str(content))
        # Also include full message dump if it has useful text
        texts.append(json.dumps(msg, ensure_ascii=False))

    # Filter publisher-style messages
    filtered = []
    for text in texts:
        if not text:
            continue
        if is_publisher_text(text):
            continue
        filtered.append(text)
    return filtered


def _extract_json_objects_from_text(text: str) -> List[Dict[str, Any]]:
    objs: List[Dict[str, Any]] = []
    if not text:
        return objs
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        if text[idx] != "{":
            idx += 1
            continue
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except Exception:
            idx += 1
            continue
        if isinstance(obj, dict):
            objs.append(obj)
        idx += end
    return objs


def _build_report_source(messages: List[Dict[str, Any]], is_publisher_text) -> str:
    payloads: List[str] = []
    texts = _collect_message_texts(messages, is_publisher_text)
    for text in texts:
        for obj in _extract_json_objects_from_text(text):
            if isinstance(obj, dict):
                if "records" in obj or "markdown" in obj or "matched_count" in obj:
                    payloads.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(payloads)


def _build_email_source(messages: List[Dict[str, Any]], is_publisher_text) -> str:
    texts = _collect_message_texts(messages, is_publisher_text)
    joined = "\n".join(texts)
    email = _extract_first_email_from_text(joined)
    if email and email not in joined:
        joined += f"\n{email}"
    return joined


def _extract_first_email_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}", text)
    return match.group(0) if match else None


def _extract_person_info_args(message: str) -> Dict[str, Any]:
    """
    Parse arguments from message content.
    Supports raw JSON or ```json fenced block with keys:
    conditions, keyword, limit, return_detail, return_xml
    """
    if not message:
        return {"conditions": [], "limit": 5}

    json_text = None
    fenced = re.search(r"```json\\s*(\\{[\\s\\S]*?\\})\\s*```", message, re.IGNORECASE)
    if fenced:
        json_text = fenced.group(1)
    else:
        stripped = message.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            json_text = stripped

    if json_text:
        try:
            data = json.loads(json_text)
            if isinstance(data, dict):
                if "conditions" not in data and "conditionList" in data:
                    data["conditions"] = data.get("conditionList") or []
                # Check if JSON has explicit query parameters
                has_query_params = any(
                    key in data
                    for key in [
                        "job_keywords",
                        "org_keywords",
                        "keywords",
                        "keyword",
                        "gender",
                        "education_keywords",
                        "education_any_keywords",
                        "age_min",
                        "age_max",
                        "birth_year_range",
                    ]
                )
                if has_query_params:
                    return data
                # If JSON only has empty conditions, continue to extract from text
                json_data = data
        except json.JSONDecodeError:
            json_data = None
    else:
        json_data = None

    text = message.strip()
    args: Dict[str, Any] = json_data if json_data else {"conditions": [], "limit": 5}

    gender = None
    if "女" in text or "女性" in text:
        gender = "女"
    elif "男" in text or "男性" in text:
        gender = "男"
    if gender:
        args["gender"] = gender

    # Education parsing
    edu_term = None
    if "博士" in text:
        edu_term = "博士"
    elif "硕士" in text or "研究生" in text:
        edu_term = "研究生"
    elif "本科" in text or "学士" in text:
        edu_term = "本科"
    elif "大专" in text or "专科" in text:
        edu_term = "大专"
    elif "中专" in text:
        edu_term = "中专"
    elif "高中" in text:
        edu_term = "高中"
    elif "初中" in text:
        edu_term = "初中"

    if edu_term:
        if "及以上" in text or "以上" in text:
            edu_any = _education_any_keywords(edu_term)
            if edu_any:
                args["education_any_keywords"] = edu_any
        else:
            args["education_keywords"] = [edu_term]

    cohort = re.search(r"(\\d{2})后", text)
    if cohort:
        yy = int(cohort.group(1))
        start_year = 2000 + yy if yy <= 30 else 1900 + yy
        end_year = start_year + (9 if yy % 10 == 0 else 4)
        args["birth_year_range"] = [start_year, end_year]

    year_range = re.search(r"(\\d{4})\\s*[至\\-]\\s*(\\d{4})\\s*年?", text)
    if year_range:
        args["birth_year_range"] = [int(year_range.group(1)), int(year_range.group(2))]

    age_range = re.search(r"(\\d{1,2})\\s*[-到至]\\s*(\\d{1,2})\\s*岁", text)
    if age_range:
        args["age_min"] = int(age_range.group(1))
        args["age_max"] = int(age_range.group(2))
    else:
        age_single = re.search(r"(\\d{1,2})\\s*岁", text)
        if age_single:
            age_value = int(age_single.group(1))
            if "以上" in text or "及以上" in text:
                args["age_min"] = age_value
            elif "以下" in text or "及以下" in text:
                args["age_max"] = age_value
            else:
                args["age_min"] = age_value
                args["age_max"] = age_value

    # Years / tenure parsing
    years_min = None
    years_max = None
    years_range = re.search(r"(\\d+(?:\\.\\d+)?)\\s*[-到至]\\s*(\\d+(?:\\.\\d+)?)\\s*年", text)
    if years_range:
        years_min = float(years_range.group(1))
        years_max = float(years_range.group(2))
    else:
        years_single = re.search(r"(\\d+(?:\\.\\d+)?)\\s*年", text)
        if years_single:
            value = float(years_single.group(1))
            if any(k in text for k in ["以上", "及以上", "不少于", "至少", "起码", ">="]):
                years_min = value
            elif any(k in text for k in ["以下", "及以下", "不超过", "至多", "以内", "<="]):
                years_max = value
            else:
                years_min = value
                years_max = value

    if years_min is not None or years_max is not None:
        if "工龄" in text:
            args["work_years_min"] = years_min
            args["work_years_max"] = years_max
        elif "行龄" in text:
            args["bank_years_min"] = years_min
            args["bank_years_max"] = years_max
        elif "基层" in text:
            args["base_years_min"] = years_min
            args["base_years_max"] = years_max
        elif any(k in text for k in ["条线", "分管", "任职", "工作经历", "年限", "经验"]):
            args["experience_min"] = years_min
            args["experience_max"] = years_max

    if "在职" in text:
        args["conditions"].append({"cndName": "在职状态", "cndValList": ["在职"]})
    if "在岗" in text:
        args["conditions"].append({"cndName": "在职状态", "cndValList": ["在岗"]})

    job_keywords: List[str] = []
    # 按照从长到短的顺序匹配，避免短词覆盖长词
    for kw in [
        "分行行长", "支行行长", "行长秘书", "副行长",
        "客户经理", "风险经理", "业务经理", "部门经理", "技术经理",
        "部门领导", "总经理", "行长", "秘书", "主任", "经理"
    ]:
        if kw in text:
            # 如果已经匹配到更长的词，跳过短词
            if kw == "行长" and any(x in job_keywords for x in ["分行行长", "支行行长", "副行长", "行长秘书"]):
                continue
            if kw == "秘书" and "行长秘书" in job_keywords:
                continue
            if kw == "经理" and any(x.endswith("经理") for x in job_keywords):
                continue
            job_keywords.append(kw)
    if job_keywords:
        args["job_keywords"] = job_keywords

    org_keywords: List[str] = []
    for kw in ["二级支行", "一级支行", "支行", "分行", "总行"]:
        if kw in text:
            if kw == "支行" and ("二级支行" in org_keywords or "一级支行" in org_keywords):
                continue
            org_keywords.append(kw)
    if org_keywords:
        args["org_keywords"] = org_keywords

    keywords: List[str] = []
    for k in (job_keywords + org_keywords):
        if k not in keywords:
            keywords.append(k)
    if keywords:
        args["keywords"] = keywords

    if not args.get("keywords") and text:
        args["keyword"] = text

    return args


def _education_any_keywords(edu_term: str) -> List[str]:
    order = ["初中", "高中", "中专", "大专", "本科", "研究生", "博士"]
    if edu_term not in order:
        return [edu_term]
    start = order.index(edu_term)
    return order[start:]


def _extract_todo_args(message: str) -> Dict[str, Any]:
    """
    Parse todo query parameters from natural language or JSON.
    Supports keys: start_date, end_date, status
    """
    if not message:
        return {}

    json_text = None
    fenced = re.search(r"```json\\s*(\\{[\\s\\S]*?\\})\\s*```", message, re.IGNORECASE)
    if fenced:
        json_text = fenced.group(1)
    else:
        stripped = message.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            json_text = stripped

    if json_text:
        try:
            data = json.loads(json_text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    text = message.strip()
    args: Dict[str, Any] = {}

    if "下周" in text:
        # Use server-local relative dates; tool expects concrete dates
        today = datetime.date.today()
        next_monday = today + datetime.timedelta(days=(7 - today.weekday()))
        next_sunday = next_monday + datetime.timedelta(days=6)
        args["start_date"] = next_monday.isoformat()
        args["end_date"] = next_sunday.isoformat()

    if "已完成" in text:
        args["status"] = "已完成"
    elif "进行中" in text:
        args["status"] = "进行中"
    elif "待办" in text:
        args["status"] = "待办"

    # CRUD intent
    if any(k in text for k in ["新增", "添加", "创建"]):
        args["action"] = "create"
    elif any(k in text for k in ["删除", "移除"]):
        args["action"] = "delete"
    elif any(k in text for k in ["更新", "修改", "变更"]):
        args["action"] = "update"

    # user_id / employee_id parsing
    uid = re.search(r"user[_-]?id[:：\\s]*([a-zA-Z0-9_-]+)", text)
    if uid:
        args["user_id"] = uid.group(1)

    emp = re.search(r"(员工编号|工号|employee[_-]?id)[:：\\s]*([a-zA-Z0-9_-]+)", text)
    if emp:
        args["employee_id"] = emp.group(2)

    return args


def _extract_unicorn_args(message: str) -> Dict[str, Any]:
    if not message:
        return {"limit": 5}
    # JSON passthrough
    json_text = None
    fenced = re.search(r"```json\\s*(\\{[\\s\\S]*?\\})\\s*```", message, re.IGNORECASE)
    if fenced:
        json_text = fenced.group(1)
    else:
        stripped = message.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            json_text = stripped
    if json_text:
        try:
            data = json.loads(json_text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    args: Dict[str, Any] = {"limit": 5}
    count = re.search(r"(\\d+)家", message)
    if count:
        args["limit"] = int(count.group(1))
    if "独角兽" in message:
        args["status"] = "独角兽"
    return args


def _extract_risk_args(message: str) -> Dict[str, Any]:
    if not message:
        return {}
    json_text = None
    fenced = re.search(r"```json\\s*(\\{[\\s\\S]*?\\})\\s*```", message, re.IGNORECASE)
    if fenced:
        json_text = fenced.group(1)
    else:
        stripped = message.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            json_text = stripped
    if json_text:
        try:
            data = json.loads(json_text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    ids = re.findall(r"(uc-\\d{3})", message)
    return {"company_ids": ids} if ids else {}


def _extract_report_args(message: str) -> Dict[str, Any]:
    if not message:
        return {}
    json_text = None
    fenced = re.search(r"```json\\s*(\\{[\\s\\S]*?\\})\\s*```", message, re.IGNORECASE)
    if fenced:
        json_text = fenced.group(1)
    else:
        stripped = message.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            json_text = stripped
    if json_text:
        try:
            data = json.loads(json_text)
            if isinstance(data, dict):
                # If already structured with sections, return directly.
                return data
        except json.JSONDecodeError:
            pass

    # Heuristic assembly from raw message text (best-effort).
    text = message.strip()
    sections = []
    title = "分析报告"

    # Try to extract embedded tool results if present in text.
    unicorn_records = []
    risk_records = []
    report_text = None

    # Extract JSON blocks that look like tool outputs
    for m in re.finditer(r"(\\{[\\s\\S]*?\\})", text):
        block = m.group(1)
        try:
            data = json.loads(block)
        except Exception:
            continue
        if isinstance(data, dict):
            if "records" in data and "company_id" in json.dumps(data, ensure_ascii=False):
                records = data.get("records") or []
                if records and "credit_rating" in json.dumps(records, ensure_ascii=False):
                    risk_records.extend(records)
                else:
                    unicorn_records.extend(records)
            if "markdown" in data:
                report_text = data.get("markdown")

    if unicorn_records:
        overview_lines = []
        for item in unicorn_records:
            name = item.get("name") or item.get("company_name") or item.get("company_id")
            industry = item.get("industry")
            valuation = item.get("valuation")
            founding = item.get("founded_year")
            line = f"- {name}"
            if industry:
                line += f" | 行业: {industry}"
            if valuation:
                line += f" | 估值: {valuation}"
            if founding:
                line += f" | 成立: {founding}"
            overview_lines.append(line)
        sections.append({"heading": "独角兽企业概览", "content": "\n".join(overview_lines)})

    if risk_records:
        risk_lines = []
        for item in risk_records:
            cid = item.get("company_id")
            rating = item.get("credit_rating")
            pd = item.get("default_probability")
            leverage = item.get("leverage_ratio")
            liquidity = item.get("liquidity_risk")
            line = f"- {cid}"
            if rating:
                line += f" | 评级: {rating}"
            if pd is not None:
                line += f" | 违约概率: {pd}"
            if leverage is not None:
                line += f" | 负债率: {leverage}"
            if liquidity:
                line += f" | 流动性风险: {liquidity}"
            risk_lines.append(line)
        sections.append({"heading": "风险指标摘要", "content": "\n".join(risk_lines)})

    if report_text:
        sections.append({"heading": "补充分析", "content": report_text})

    if not sections:
        sections = [{"heading": "说明", "content": text}]

    return {"title": title, "sections": sections, "use_llm": True, "instruction": "生成清晰、结构化的Markdown分析报告，包含摘要、逐家分析与整体结论。"}


def _extract_email_args(message: str) -> Dict[str, Any]:
    if not message:
        return {}
    json_text = None
    fenced = re.search(r"```json\\s*(\\{[\\s\\S]*?\\})\\s*```", message, re.IGNORECASE)
    if fenced:
        json_text = fenced.group(1)
    else:
        stripped = message.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            json_text = stripped
    if json_text:
        try:
            data = json.loads(json_text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}", message)
    return {
        "to": email_match.group(0) if email_match else None,
        "subject": "邮件发送",
        "body": message.strip(),
    }


def _extract_schedule_args(message: str) -> Dict[str, Any]:
    if not message:
        return {}
    json_text = None
    fenced = re.search(r"```json\\s*(\\{[\\s\\S]*?\\})\\s*```", message, re.IGNORECASE)
    if fenced:
        json_text = fenced.group(1)
    else:
        stripped = message.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            json_text = stripped
    if json_text:
        try:
            data = json.loads(json_text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"action": "create", "schedule": {"title": message.strip()}}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from .decorators import create_logged_tool

logger = logging.getLogger(__name__)

_SAMPLE_CACHE: Optional[Dict[str, Any]] = None


def _sample_path() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "person_info_sample.json"


def _load_sample() -> Dict[str, Any]:
    global _SAMPLE_CACHE
    if _SAMPLE_CACHE is not None:
        return _SAMPLE_CACHE

    path = _sample_path()
    if not path.exists():
        raise FileNotFoundError(f"Sample data not found: {path}")

    _SAMPLE_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _SAMPLE_CACHE


def _flatten_text(person: Dict[str, Any]) -> str:
    parts: List[str] = []
    for value in person.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (int, float)):
            parts.append(str(value))
    return " ".join(parts)


def _extract_year_numbers(*values: Optional[str]) -> List[float]:
    numbers: List[float] = []
    pattern = re.compile(r"(\\d+(?:\\.\\d+)?)\\s*年")
    for value in values:
        if not value:
            continue
        for match in pattern.findall(value):
            try:
                numbers.append(float(match))
            except ValueError:
                continue
    return numbers


def _normalize_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _matches_condition(person: Dict[str, Any], condition: Dict[str, Any]) -> bool:
    name = str(condition.get("cndName") or condition.get("name") or "").strip()
    values = _normalize_list(condition.get("cndValList") or condition.get("values"))
    range_values = condition.get("rangeValueList") or condition.get("range_values") or []

    text = _flatten_text(person)

    value_ok = True
    if values:
        value_ok = any(v in text for v in values)

    range_ok = True
    if range_values:
        range_ok = False
        years = _extract_year_numbers(
            person.get("attr1DefDsc"),
            person.get("attr2DefDsc"),
            person.get("attr3DefDsc"),
            person.get("attr4DefDsc"),
        )
        for item in range_values:
            start_val = item.get("startVal")
            if start_val is None:
                continue
            try:
                start_num = float(start_val)
            except ValueError:
                continue
            if any(year >= start_num for year in years):
                range_ok = True
                break

    if name in {"在职状态"}:
        status = person.get("empeStdsc")
        value_ok = not values or any(v in str(status) for v in values)

    if name in {"机构名称"}:
        inst = f"{person.get('holdposInstNm') or ''} {person.get('instFullNm') or ''}"
        value_ok = not values or any(v in inst for v in values)

    if name in {"姓名", "人员姓名"}:
        name_value = person.get("adtEmpeNm") or ""
        value_ok = not values or any(v in name_value for v in values)

    return value_ok and range_ok


def _filter_people(
    people: List[Dict[str, Any]],
    conditions: List[Dict[str, Any]],
    keyword: Optional[str],
) -> List[Dict[str, Any]]:
    if keyword:
        conditions = list(conditions)
        conditions.append({"cndName": "关键词", "cndValList": [keyword]})

    if not conditions:
        return list(people)

    results: List[Dict[str, Any]] = []
    for person in people:
        if all(_matches_condition(person, cond) for cond in conditions):
            results.append(person)
    return results


def _build_request_xml(condition_list: List[Dict[str, Any]]) -> str:
    payload = {
        "tx26IdxVal": "04",
        "sessionId": str(int(time.time() * 1000)),
        "source": "web",
        "conditionList": condition_list,
    }
    data = json.dumps(payload, ensure_ascii=False)
    return (
        "<ENTITY>\\n"
        "  <Mnplt_TpCd><![CDATA[]]></Mnplt_TpCd>\\n"
        "  <Tx_26_Idx_Val><![CDATA[]]></Tx_26_Idx_Val>\\n"
        f"  <Data_Stc_Dsc><![CDATA[{data}]]></Data_Stc_Dsc>\\n"
        "</ENTITY>"
    )


def _build_response_xml(matched_count: int) -> str:
    summary = {"matched_count": matched_count}
    message_content = "```json\\n" + json.dumps(summary, ensure_ascii=False) + "\\n```"
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {"content": message_content, "role": "assistant", "tool_calls": []},
            }
        ],
        "created": int(time.time() * 1000),
        "notes": "提示：接口传入列表！",
        "traceId": f"trace_{int(time.time() * 1000)}",
        "usage": {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0},
    }
    data = json.dumps(payload, ensure_ascii=False)
    return (
        "<ENTITY>\\n"
        f"<Data_Enqr_Rslt><![CDATA[{data}]]></Data_Enqr_Rslt>\\n"
        "<codeid><![CDATA[20000]]></codeid>\\n"
        "</ENTITY>"
    )


class PersonInfoQueryInput(BaseModel):
    """Input for person info query tool."""

    conditions: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="条件列表，结构与 InterfaceExample.md 的 conditionList 一致",
    )
    keyword: Optional[str] = Field(
        default=None,
        description="关键词模糊匹配（会追加到条件列表中）",
    )
    limit: Optional[int] = Field(
        default=None,
        description="最多返回多少条人员记录",
    )
    return_detail: bool = Field(
        default=True,
        description="是否返回完整人员信息列表",
    )
    return_xml: bool = Field(
        default=True,
        description="是否返回接口请求/响应 XML 示例",
    )


class PersonInfoQueryTool(BaseTool):
    name: ClassVar[str] = "person_info_query_tool"
    args_schema: Type[BaseModel] = PersonInfoQueryInput
    description: ClassVar[str] = (
        "模拟人员信息查询接口，按照 InterfaceExample.md 的示例返回请求/响应 XML 与人员详情数据。"
    )

    def _run(
        self,
        conditions: Optional[List[Dict[str, Any]]] = None,
        keyword: Optional[str] = None,
        limit: Optional[int] = None,
        return_detail: bool = True,
        return_xml: bool = True,
    ) -> Dict[str, Any]:
        try:
            sample = _load_sample()
        except Exception as exc:
            logger.error("Failed to load sample data: %s", exc)
            return {"status": "error", "error": str(exc)}

        condition_list = conditions or []
        people = sample.get("personInfoList", [])
        filtered = _filter_people(people, condition_list, keyword)
        if isinstance(limit, int) and limit > 0:
            filtered = filtered[:limit]

        result: Dict[str, Any] = {
            "status": "success",
            "matched_count": len(filtered),
            "condition_list": condition_list,
        }

        if return_xml:
            result["request_xml"] = _build_request_xml(condition_list)
            result["response_xml"] = _build_response_xml(len(filtered))

        if return_detail:
            result["detail"] = {
                "authPersonPropertyMap": sample.get("authPersonPropertyMap", {}),
                "personInfoList": filtered,
            }

        return result

    async def _arun(self, **kwargs: Any) -> Dict[str, Any]:
        return self._run(**kwargs)


PersonInfoQueryTool = create_logged_tool(PersonInfoQueryTool)
person_info_query_tool = PersonInfoQueryTool()


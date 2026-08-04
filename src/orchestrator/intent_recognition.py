from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator

from src.orchestrator.intent_catalog import (
    INTENT_CATALOG,
    SUPPORTED_INTENTS,
    intent_prompt_catalog,
)


logger = logging.getLogger(__name__)

IntentSource = Literal["rule", "semantic", "rule+semantic"]
IntentProvenance = Literal["explicit", "inferred", "policy_generated"]


class IntentCandidate(BaseModel):
    name: str = Field(json_schema_extra={"enum": sorted(SUPPORTED_INTENTS)})
    confidence: float = Field(ge=0.0, le=1.0)
    source: IntentSource
    provenance: IntentProvenance = "explicit"
    text_span: str | None = None
    evidence: list[str] = Field(default_factory=list)
    negated: bool = False
    condition: str | None = None
    condition_on: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def supported_name(cls, value: str) -> str:
        if value not in SUPPORTED_INTENTS:
            raise ValueError(f"unsupported intent name: {value}")
        return value


class IntentRecognitionResult(BaseModel):
    primary_intent: str = "general_assistance"
    intents: list[IntentCandidate] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    ambiguities: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    mode: Literal["rule", "semantic", "hybrid"] = "rule"
    degraded: bool = False
    degradation_reason: str | None = None
    resolved_request: str | None = None
    context_references: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def executable_intents(self) -> list[IntentCandidate]:
        return [item for item in self.intents if not item.negated]


class SemanticEntities(BaseModel):
    """语义模型可输出的统一实体槽位；未知字段不会进入执行契约。"""

    people: list[str] = Field(default_factory=list)
    employee_name: str | None = None
    employee_id: str | None = None
    recipient: str | None = None
    location: str | None = None
    time: str | None = None
    count: str | int | None = None
    document_type: str | None = None
    business_object: str | None = None


class SemanticContextReference(BaseModel):
    """语义模型确认当前请求实际使用的上下文项。"""

    kind: Literal["entity", "artifact"]
    key: str
    value: Any
    mention: str
    source: Literal["conversation_context"] = "conversation_context"


class SemanticIntentPayload(BaseModel):
    primary_intent: str = Field(
        default="general_assistance",
        json_schema_extra={"enum": sorted(SUPPORTED_INTENTS | {"general_assistance"})},
    )
    intents: list[IntentCandidate] = Field(default_factory=list)
    entities: SemanticEntities = Field(default_factory=SemanticEntities)
    ambiguities: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    resolved_request: str | None = None
    context_references: list[SemanticContextReference] = Field(default_factory=list)

    @field_validator("primary_intent")
    @classmethod
    def supported_primary_name(cls, value: str) -> str:
        if value not in SUPPORTED_INTENTS | {"general_assistance"}:
            raise ValueError(f"unsupported primary intent: {value}")
        return value

    @field_validator("intents")
    @classmethod
    def semantic_sources_only(cls, value: list[IntentCandidate]) -> list[IntentCandidate]:
        for item in value:
            item.source = "semantic"
        return value


class SemanticIntentProvider(Protocol):
    async def recognize(self, user_query: str) -> SemanticIntentPayload | dict[str, Any]: ...


class SemanticProviderError(RuntimeError):
    """语义 Provider 不可用、超时或返回无效结构。"""


def _query_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class LLMSemanticIntentProvider:
    """复用项目 Basic LLM，并强制 Pydantic 结构化输出。"""

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        conversation_context: dict[str, Any] | None = None,
    ) -> None:
        from src.service.env import (
            INTENT_CONTEXT_SEMANTIC_TIMEOUT_SECONDS,
            INTENT_SEMANTIC_TIMEOUT_SECONDS,
        )

        self.conversation_context = (
            dict(conversation_context)
            if isinstance(conversation_context, dict)
            else {}
        )
        configured_timeout = timeout_seconds or INTENT_SEMANTIC_TIMEOUT_SECONDS
        has_context_candidates = any(
            value not in (None, "", [], {})
            for value in self.conversation_context.values()
        )
        self.timeout_seconds = (
            max(configured_timeout, INTENT_CONTEXT_SEMANTIC_TIMEOUT_SECONDS)
            if has_context_candidates
            else configured_timeout
        )

    @staticmethod
    def is_configured() -> bool:
        from src.llm.llm import get_llm_configuration_status

        return bool(get_llm_configuration_status()["details"]["basic"]["configured"])

    async def recognize(self, user_query: str) -> SemanticIntentPayload:
        if not self.is_configured():
            raise SemanticProviderError("semantic_provider_not_configured")

        from src.llm.llm import get_llm_by_type

        schema_text = json.dumps(
            SemanticIntentPayload.model_json_schema(),
            ensure_ascii=False,
        )
        context_text = json.dumps(
            self.conversation_context,
            ensure_ascii=False,
            default=str,
        )
        system_prompt = f"""你是任务理解组件，不负责执行任务。请从用户输入中识别用户最终目标、显式要求的动作、隐含前置动作、实体、否定关系、条件关系和缺失信息。

只能从系统提供的意图标签集合中选择 intent name，不得自行创造标签。

必须区分：
1. explicit：用户明确要求执行的动作；
2. inferred：为了实现用户目标可能需要的前置动作；
3. policy_generated：由权限、安全或审批策略产生的动作。

出现“不要、无需、禁止、别、仅了解如何、只是想了解”等表达时，不得将对应动作标记为可执行任务，应保留该意图并设置 negated=true，或使用 information_consultation。

条件任务必须在 condition 中保留条件表达，并通过 condition_on 指向作为条件依据的意图名。若存在多种合理理解，不要擅自选择，输出 ambiguities、needs_clarification 和 clarification_questions。

用户在原文中明确说出的条件动作仍属于 explicit；只有原文没有要求、纯粹为了完成目标补出的前置动作才属于 inferred。

每个意图必须包含独立 confidence、source=semantic、provenance、evidence 和尽可能准确的 text_span。隐含前置动作的 text_span 可以为空。输出必须符合给定 JSON Schema，不要输出解释性文本。

实体判断规则：
1. 由你根据完整语境判断实体类型，不得仅因词语出现在“查询”后面就把它当人员；
2. 地名写入 location，员工或参会人写入 people/employee_name，两者不得混用；
3. 用户没有说明员工时，不得猜测 employee_name 或 employee_id；
4. “给出提醒/建议/提示”表示期望的回答形式，不等于创建或查询日程提醒；
5. 只有“设置提醒、创建提醒、查询日程、查看待办”等明确表达才使用 schedule_management。

对话上下文处理规则：
1. 下方“上下文候选”不是当前任务，禁止把其中的旧意图、旧动作或旧任务边界直接加入本轮；
2. 由你根据完整语义判断当前输入是否包含指代、省略或对既有产物的引用，不得依赖固定代词表；
3. 只有当前输入确实引用某个上下文实体或产物时，才可使用该值，并在 context_references 中记录 kind、key、value、mention，source 固定为 conversation_context；
4. 如果引用关系唯一且明确，将指代消解后的完整本轮请求写入 resolved_request；
5. 如果可能指向多个对象，不要猜测，保留原请求并生成 clarification_questions；
6. 用户提出无关的新问题时，context_references 必须为空，resolved_request 必须只表达当前新问题。

上下文候选（仅供指代消解，不是待执行任务）：
{context_text or "{}"}

允许的 intent name（必须逐字使用其中之一）：
{', '.join(sorted(SUPPORTED_INTENTS))}

标签说明：
{intent_prompt_catalog()}

输出 JSON Schema：
{schema_text}"""
        llm = get_llm_by_type("basic")
        messages = [("system", system_prompt), ("human", user_query)]
        try:
            response = await asyncio.wait_for(
                llm.ainvoke(messages), timeout=self.timeout_seconds
            )
            content = getattr(response, "content", response)
            if isinstance(content, list):
                text_parts = [
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict)
                ]
                content = "".join(text_parts)
            if isinstance(content, str):
                normalized = content.strip()
                normalized = re.sub(r"^```(?:json)?\s*", "", normalized, flags=re.IGNORECASE)
                normalized = re.sub(r"\s*```$", "", normalized)
                content = json.loads(normalized)
            # 部分兼容 OpenAI 的服务会错误地在根节点外包一层单元素数组；
            # 这里只做传输结构归一化，随后仍由 Pydantic 严格校验业务 Schema。
            if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
                content = content[0]
            return SemanticIntentPayload.model_validate(content)
        except asyncio.TimeoutError as exc:
            raise SemanticProviderError("semantic_provider_timeout") from exc
        except (ValidationError, ValueError, TypeError) as exc:
            raise SemanticProviderError("semantic_provider_invalid_schema") from exc
        except Exception as exc:
            raise SemanticProviderError("semantic_provider_error") from exc


def _left_edge_intents(text: str) -> set[str]:
    normalized = text.lower().rstrip("的 ")
    return {
        name
        for name, definition in INTENT_CATALOG.items()
        if any(normalized.endswith(str(keyword).lower()) for keyword in definition.get("keywords") or ())
    }


def _right_edge_intents(text: str) -> set[str]:
    normalized = re.sub(
        r"^(?:再|并)?(?:查询|查一下|查看|看看|获取|读取|生成|整理)",
        "",
        text.lower().lstrip(),
    ).lstrip("员工的 ")
    return {
        name
        for name, definition in INTENT_CATALOG.items()
        if any(normalized.startswith(str(keyword).lower()) for keyword in definition.get("keywords") or ())
    }


def _split_coordinated_intents(part: str) -> list[str]:
    """只在连词两侧命中不同意图时拆分，避免把普通并列实体拆成任务。"""
    for match in re.finditer(r"(?:以及|和|及)", part):
        left = part[:match.start()].strip()
        right = part[match.end():].strip()
        if not left or not right:
            continue
        # 连词必须紧邻两个业务概念。这样可拆“基本信息和请假记录”，
        # 但不会把“王强和张三开会”这种并列实体误拆成两个任务。
        left_intents = _left_edge_intents(left)
        right_intents = _right_edge_intents(right)
        if (
            left_intents
            and right_intents
            and (left_intents - right_intents)
            and (right_intents - left_intents)
        ):
            return [left, right]
    return [part]


def segment_query(text: str) -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    coarse_parts = [
        part.strip()
        for part in re.split(
            r"(?:，|,|。|；|;|\bthen\b|\band\b|然后|之后|并且|同时|最后|否则)",
            raw,
            flags=re.IGNORECASE,
        )
        if part and part.strip()
    ] or [raw]
    parts = [
        fragment
        for part in coarse_parts
        for fragment in _split_coordinated_intents(part)
    ]
    result: list[dict[str, Any]] = []
    cursor = 0
    for index, part in enumerate(parts, start=1):
        start = raw.find(part, cursor)
        if start < 0:
            start = raw.find(part)
        end = start + len(part) if start >= 0 else -1
        cursor = max(cursor, end)
        result.append({"id": f"segment_{index}", "text": part, "start": start, "end": end})
    return result


_PERSON_STOP_WORDS = {
    "员工", "人员", "人事", "基本", "个人", "相关", "这个", "那个", "公司", "部门",
    "收入", "在职", "请假", "分析", "明天", "今天", "后天", "本周", "下周", "本月", "下月",
    "最近", "一次", "一场", "一个", "会议", "参会人", "收件人",
}
_GENERIC_PERSON_PREFIXES = (
    "我们",
    "咱们",
    "大家",
    "全体",
    "所有",
    "员工",
    "人员",
    "部门",
    "公司",
)
_ORGANIZATION_SUBJECT_SUFFIXES = (
    "部门",
    "公司",
    "团队",
    "小组",
    "中心",
    "办公室",
    "事业部",
    "分部",
    "处室",
    "科室",
    "部",
)

_LEAVE_QUERY_SUBJECT_PATTERN = (
    r"(?:^|[，,。；;\s])"
    r"(?:(?:请问(?:一下)?|请(?:帮我|帮忙)?|麻烦(?:帮我|帮忙)?|帮我|帮忙)\s*)?"
    r"(?:(?:查(?:询|一下|下)?|查看|看看|看(?:一下)?|确认(?:一下)?)\s*)?"
    r"(?:员工)?([\u4e00-\u9fff]{2,4}?)"
    r"(?=(?:最近|本月|本周|这段时间)?(?:有没有|是否|有无)"
    r"(?!.{0,8}(?:打算|计划|准备|需要|想|要不要|应该|可以|能否))"
    r".{0,8}(?:请假|休假))"
)


def is_person_candidate(value: Any) -> bool:
    candidate = str(value or "").strip()
    if not 2 <= len(candidate) <= 8 or candidate in _PERSON_STOP_WORDS:
        return False
    if candidate.startswith(_GENERIC_PERSON_PREFIXES):
        return False
    return not candidate.endswith(_ORGANIZATION_SUBJECT_SUFFIXES)


def extract_entities(text: str) -> dict[str, Any]:
    raw = str(text or "")
    entities: dict[str, Any] = {}

    # 规则实体只处理可由句法稳定定位的槽位，作为语义模型不可用时的降级结果。
    # 地点不依赖城市词表：提取“查询 + 地点 + 时间 + 天气”的结构。
    location_match = re.search(
        r"(?:查询|查一下|查看|看看)\s*"
        r"([\u4e00-\u9fff]{2,8}?)"
        r"(?=(?:今天|明天|后天|本周|下周|未来\d+天)(?:的)?天气)",
        raw,
    )
    if location_match:
        entities["location"] = location_match.group(1)

    recipient_match = re.search(
        r"(?:发给|发送给|寄给|转给|抄送给?|交给|通知)\s*([\w.@\-\u4e00-\u9fff]{2,30}?)(?=$|[，,。；;]|然后|并且|并发|再)",
        raw,
    )
    if not recipient_match:
        recipient_match = re.search(
            r"(?:给|向)([\w.@\-\u4e00-\u9fff]{2,30}?)(?=(?:发送|发一封|发邮件|寄送|通知))",
            raw,
        )
    if recipient_match:
        recipient = recipient_match.group(1).strip("，。；;,. ")
        if recipient:
            entities["recipient"] = recipient

    people: list[str] = []
    # 从动作与属格上下文中抽取姓名，不依赖固定人名表。
    patterns = (
        _LEAVE_QUERY_SUBJECT_PATTERN,
        r"(?:查询|查一下|查看|看看|帮|为|取消|生成)(?:员工)?([\u4e00-\u9fff]{2,4}?)(?=的|生成|写|开|明天|后天|本周|下周|本月|下月|日程|在职|收入|请假|休假)",
        r"(?:安排|预约)([\u4e00-\u9fff]{2,3})(?:和|与)([\u4e00-\u9fff]{2,3})(?=明天|后天|开会|的?会议)",
        r"(?:安排)?与([\u4e00-\u9fff]{2,3})(?=的?会议)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, raw):
            for group in match.groups():
                if not group:
                    continue
                candidate = group.removeprefix("员工").removeprefix("与").removeprefix("和").removesuffix("的")
                if (
                    is_person_candidate(candidate)
                    and candidate != entities.get("location")
                    and candidate not in people
                ):
                    people.append(candidate)
    if entities.get("recipient") and str(entities["recipient"]).endswith(("经理", "秘书")):
        recipient_person = str(entities["recipient"])
        if recipient_person not in people:
            people.append(recipient_person)
    leave_subject_match = re.search(
        _LEAVE_QUERY_SUBJECT_PATTERN,
        raw,
    )
    if leave_subject_match:
        leave_subject = leave_subject_match.group(1)
        if is_person_candidate(leave_subject):
            entities["employee_name"] = leave_subject
            if leave_subject not in people:
                people.append(leave_subject)

    if people:
        entities["people"] = people
        recipient = str(entities.get("recipient") or "")
        employee_candidates = [
            item for item in people if item != recipient
        ]
        if employee_candidates:
            entities["employee_name"] = employee_candidates[0]

    if (
        re.search(r"请假(?:申请书?|书|条|单|材料)?|休假(?:申请|材料)", raw)
        and re.search(r"生成|写|起草|准备|办理|申请书|请假书|请假条|请假单|材料", raw)
        and not re.search(r"请假(?:制度|规定|政策)", raw)
    ):
        entities["document_type"] = "leave_application"
    elif "收入证明" in raw:
        entities["document_type"] = "income_proof"
    elif "在职证明" in raw:
        entities["document_type"] = "employment_certificate"
    elif re.search(r"说明(?:文档|文件)|Word\s*文档", raw, flags=re.IGNORECASE):
        entities["document_type"] = "explanation_document"
    elif "分析报告" in raw:
        entities["document_type"] = "analysis_report"
    elif "报告" in raw:
        entities["document_type"] = "report"

    for word in ("今天", "明天", "后天", "本周", "下周", "本月", "下月"):
        if word in raw:
            entities["time"] = word
            break
    count_match = re.search(r"(\d+|[一二三四五六七八九十]+)\s*(?:家|个|份|名)", raw)
    if count_match:
        entities["count"] = count_match.group(1)
    if "独角兽" in raw:
        entities["business_object"] = "unicorn_company"
    return entities


def _find_keyword_spans(text: str, keywords: tuple[str, ...]) -> list[tuple[int, str]]:
    normalized = text.lower()
    matches: list[tuple[int, str]] = []
    for keyword in keywords:
        start = normalized.find(keyword.lower())
        if start >= 0:
            matches.append((start, keyword))
    return sorted(matches)


def _clause_for_match(text: str, start: int, match_text: str) -> str:
    """返回一次意图命中所在的完整分句，用于判断业务语境。"""
    clause_start = 0
    clause_end = len(text)
    match_end = start + len(match_text)
    boundaries = re.finditer(
        r"[，,。；;]|\bthen\b|\band\b|然后|之后|并且|同时|最后|否则",
        text,
        flags=re.IGNORECASE,
    )
    for boundary in boundaries:
        if boundary.end() <= start:
            clause_start = boundary.end()
            continue
        if boundary.start() >= match_end:
            clause_end = boundary.start()
            break
    return text[clause_start:clause_end].strip()


def _exclude_context_matches(
    text: str,
    matches: list[tuple[int, str]],
    exclusions: tuple[str, ...],
    preserve_patterns: tuple[str, ...] = (),
) -> list[tuple[int, str]]:
    if not exclusions:
        return matches
    filtered: list[tuple[int, str]] = []
    for position, matched_text in matches:
        clause = _clause_for_match(text, position, matched_text)
        excluded = any(
            re.search(exclusion, clause, flags=re.IGNORECASE)
            for exclusion in exclusions
        )
        preserved = any(
            re.search(pattern, clause, flags=re.IGNORECASE)
            for pattern in preserve_patterns
        )
        if not excluded or preserved:
            filtered.append((position, matched_text))
    return filtered


def _is_negated(text: str, start: int, keyword: str) -> bool:
    before = text[max(0, start - 12):start]
    negators = ("不要", "不需要", "无需", "禁止", "别", "不涉及", "不必", "不可", "不允许")
    # 逗号/分号切断前一个分句的否定作用域。
    scope = re.split(r"[，,。；;但]", before)[-1] + keyword
    return any(word in scope for word in negators)


def _is_consultation(text: str) -> bool:
    return bool(
        re.search(r"(?:只|仅)?(?:想)?了解|需要哪些权限|需要什么权限|如何(?:发送|生成|办理)|怎么(?:发送|生成|办理)", text)
    )


def _explicit_document_span(text: str) -> tuple[int, str] | None:
    """识别“生成一份说明文档”这类动词和文档名之间存在修饰语的表达。"""
    match = re.search(
        r"(?:生成|制作|输出|编写|撰写|写|起草)"
        r"(?:一|两|二|三|\d+)?\s*份?"
        r"[^，,。；;]{0,16}?"
        r"(?:说明文档|说明文件|Word\s*文档|文档|文件)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.start(), match.group(0)


class RuleIntentRecognizer:
    """把原关键词识别封装为可独立评测的候选生成器。"""

    def __init__(self, *, strong_threshold: float | None = None) -> None:
        from src.service.env import INTENT_RULE_STRONG_THRESHOLD

        self.strong_threshold = strong_threshold or INTENT_RULE_STRONG_THRESHOLD

    async def recognize(self, user_query: str) -> IntentRecognitionResult:
        text = str(user_query or "").strip()
        candidates: list[tuple[int, IntentCandidate]] = []
        consultation = _is_consultation(text)
        for name, definition in INTENT_CATALOG.items():
            if consultation and name in {"knowledge_lookup", "salary_query"}:
                # information_consultation 已表达“只咨询不执行”，避免重复生成同义读取任务。
                continue
            keywords = tuple(definition.get("keywords") or ())
            matches = _find_keyword_spans(text, keywords)
            pattern_matches: list[tuple[int, str]] = []
            for pattern in definition.get("patterns") or ():
                pattern_matches.extend(
                    (match.start(), match.group(0))
                    for match in re.finditer(str(pattern), text, flags=re.IGNORECASE)
                )
            matches.extend(pattern_matches)
            matches = _exclude_context_matches(
                text,
                matches,
                tuple(
                    str(item)
                    for item in definition.get("context_exclusions") or ()
                ),
                tuple(
                    str(item)
                    for item in definition.get("context_preserve_patterns") or ()
                ),
            )
            matches = sorted(set(matches))
            if not matches:
                continue
            if name == "employee_information_query" and re.search(
                r"(?:员工|人员|人事).{0,8}(?:制度|规定|政策)", text
            ):
                continue
            first_position, first_keyword = matches[0]
            evidence = [keyword for _, keyword in matches]
            negated = all(_is_negated(text, position, keyword) for position, keyword in matches)
            if consultation and name in {
                "message_or_email_send", "document_generation", "salary_query"
            }:
                negated = True
            provenance: IntentProvenance = "explicit"
            if name == "salary_query" and set(evidence) <= {"收入证明"}:
                provenance = "inferred"
            candidates.append(
                (
                    first_position,
                    IntentCandidate(
                        name=name,
                        confidence=min(
                            0.94,
                            self.strong_threshold + 0.04 * min(2, len(matches) - 1),
                        ),
                        source="rule",
                        provenance=provenance,
                        text_span=first_keyword,
                        evidence=evidence,
                        negated=negated,
                    ),
                )
            )
        # 目录关键词只能匹配连续短语，无法覆盖“生成一份说明文档”这种中间带
        # 数量词和文档类型修饰语的显式动作。这里补充结构模式，但仍输出标准
        # document_generation 候选，后续继续走统一融合、校验和子任务构建。
        document_span = _explicit_document_span(text)
        has_document_candidate = any(
            item.name == "document_generation" for _, item in candidates
        )
        if document_span and not has_document_candidate and not consultation:
            position, span = document_span
            candidates.append(
                (
                    position,
                    IntentCandidate(
                        name="document_generation",
                        confidence=self.strong_threshold,
                        source="rule",
                        provenance="explicit",
                        text_span=span,
                        evidence=[span, "显式文档生成结构"],
                        negated=_is_negated(text, position, span),
                    ),
                )
            )
        if consultation:
            candidates.append(
                (
                    0,
                    IntentCandidate(
                        name="information_consultation",
                        confidence=0.9,
                        source="rule",
                        provenance="explicit",
                        text_span=text,
                        evidence=["咨询/权限表达"],
                    ),
                )
            )
        ordered = [item for _, item in sorted(candidates, key=lambda pair: pair[0])]
        executable = [item for item in ordered if not item.negated]
        explicit = [item for item in executable if item.provenance == "explicit"]
        return IntentRecognitionResult(
            primary_intent=(explicit[0].name if explicit else executable[0].name) if executable else "general_assistance",
            intents=ordered,
            entities=extract_entities(text),
            mode="rule",
        )


class SemanticIntentRecognizer:
    def __init__(self, provider: SemanticIntentProvider) -> None:
        self.provider = provider

    async def recognize(self, user_query: str) -> IntentRecognitionResult:
        try:
            raw = await self.provider.recognize(user_query)
            payload = SemanticIntentPayload.model_validate(raw)
        except SemanticProviderError:
            raise
        except Exception as exc:
            raise SemanticProviderError("semantic_provider_invalid_schema") from exc

        invalid_names = [item.name for item in payload.intents if item.name not in SUPPORTED_INTENTS]
        if payload.primary_intent not in SUPPORTED_INTENTS | {"general_assistance"}:
            invalid_names.append(payload.primary_intent)
        if invalid_names:
            raise SemanticProviderError("semantic_provider_unknown_intent")

        executable = [item for item in payload.intents if not item.negated]
        primary = payload.primary_intent
        if primary != "general_assistance" and primary not in {item.name for item in executable}:
            primary = executable[0].name if executable else "general_assistance"
        return IntentRecognitionResult(
            primary_intent=primary,
            intents=payload.intents,
            entities=payload.entities.model_dump(exclude_none=True, exclude_defaults=True),
            ambiguities=payload.ambiguities,
            needs_clarification=payload.needs_clarification,
            clarification_questions=payload.clarification_questions,
            mode="semantic",
            resolved_request=payload.resolved_request,
            context_references=[
                item.model_dump()
                for item in payload.context_references
            ],
        )


class IntentFusion:
    def __init__(
        self,
        *,
        semantic_accept_threshold: float,
        semantic_high_risk_threshold: float,
        agreement_bonus: float,
        conflict_threshold: float,
    ) -> None:
        self.semantic_accept_threshold = semantic_accept_threshold
        self.semantic_high_risk_threshold = semantic_high_risk_threshold
        self.agreement_bonus = agreement_bonus
        self.conflict_threshold = conflict_threshold

    @staticmethod
    def _fuse_entities(
        rule_entities: dict[str, Any],
        semantic_entities: dict[str, Any],
    ) -> dict[str, Any]:
        """语义实体为主，规则只补确定性较高的格式化槽位。"""
        safe_rule_keys = {"recipient", "time", "count", "document_type"}
        result = {
            key: value
            for key, value in rule_entities.items()
            if key in safe_rule_keys and value not in (None, "", [])
        }
        result.update(
            {
                key: value
                for key, value in semantic_entities.items()
                if value not in (None, "", [])
            }
        )

        # 这是实体 Schema 的互斥约束，不依赖城市或人名词表。
        location = str(result.get("location") or "").strip()
        if location:
            if str(result.get("employee_name") or "").strip() == location:
                result.pop("employee_name", None)
            people = [
                str(item)
                for item in result.get("people") or []
                if str(item).strip() and str(item).strip() != location
            ]
            if people:
                result["people"] = people
            else:
                result.pop("people", None)
        return result

    def fuse(
        self,
        rule: IntentRecognitionResult,
        semantic: IntentRecognitionResult,
    ) -> IntentRecognitionResult:
        semantic_by_name = {item.name: item for item in semantic.intents}
        combined: list[IntentCandidate] = []
        consumed: set[str] = set()
        rule_executable = [item for item in rule.intents if not item.negated]
        concrete_entity_keys = {
            "employee_name",
            "people",
            "recipient",
            "business_object",
            "time",
        }
        # 语义模型经常会把“可补充的偏好”（报告格式、篇幅、公开人物的
        # 唯一身份等）也标记成 needs_clarification。若规则侧已经识别出
        # 多步骤结构，或单步骤同时具备明确业务实体，则这些问题只能作为
        # 非阻塞歧义保留，不能阻止一个本身可执行的任务进入 Planner。
        rule_has_actionable_structure = len(rule_executable) > 1 or bool(
            concrete_entity_keys & set(rule.entities)
        )
        semantic_clarification_is_blocking = (
            semantic.needs_clarification and not rule_has_actionable_structure
        )
        ambiguities = list(
            dict.fromkeys(
                rule.ambiguities
                + (semantic.ambiguities if semantic_clarification_is_blocking else [])
            )
        )
        questions = list(
            dict.fromkeys(
                rule.clarification_questions
                + (
                    semantic.clarification_questions
                    if semantic_clarification_is_blocking
                    else []
                )
            )
        )
        needs_clarification = (
            rule.needs_clarification or semantic_clarification_is_blocking
        )

        for rule_item in rule.intents:
            semantic_item = semantic_by_name.get(rule_item.name)
            if semantic_item:
                consumed.add(rule_item.name)
                negated = semantic_item.negated or rule_item.negated
                combined.append(
                    IntentCandidate(
                        name=rule_item.name,
                        confidence=min(
                            0.99,
                            max(rule_item.confidence, semantic_item.confidence)
                            + self.agreement_bonus,
                        ),
                        source="rule+semantic",
                        provenance=(
                            "explicit"
                            if rule_item.provenance == "explicit" or semantic_item.provenance == "explicit"
                            else semantic_item.provenance
                        ),
                        text_span=semantic_item.text_span or rule_item.text_span,
                        evidence=list(dict.fromkeys(rule_item.evidence + semantic_item.evidence)),
                        negated=negated,
                        condition=semantic_item.condition,
                        condition_on=semantic_item.condition_on,
                    )
                )
            else:
                combined.append(rule_item)

        for semantic_item in semantic.intents:
            if semantic_item.name in consumed:
                continue
            definition = INTENT_CATALOG[semantic_item.name]
            threshold = (
                self.semantic_high_risk_threshold
                if definition.get("high_risk")
                else self.semantic_accept_threshold
            )
            if semantic_item.negated or semantic_item.confidence >= threshold:
                combined.append(semantic_item)
            else:
                ambiguities.append(
                    f"语义候选 {semantic_item.name} 置信度 {semantic_item.confidence:.2f} 低于阈值 {threshold:.2f}"
                )
                needs_clarification = True

        # information_consultation 表示“只咨询、不执行”。如果同一输入已经明确要求
        # 生成报告、文档或发送结果，它就不能作为额外可执行意图加入任务链。
        combined_names = {item.name for item in combined if not item.negated}
        has_explicit_output = bool(
            combined_names
            & {
                "document_generation",
                "report_generation",
                "message_or_email_send",
                "meeting_arrangement",
            }
        )
        if (
            has_explicit_output
            and "knowledge_lookup" in combined_names
            and "information_consultation" in combined_names
        ):
            combined = [
                item for item in combined if item.name != "information_consultation"
            ]
            combined_names.discard("information_consultation")

        rule_intent_names = {item.name for item in rule.intents if not item.negated}
        semantic_intent_names = {
            item.name for item in semantic.intents if not item.negated
        }
        # 复合任务中，两路识别可能只是对“哪个意图最主要”的排序不同。
        # 只要双方都识别到了这两个主意图，就属于排序差异，不是任务理解冲突。
        shared_composite_ranking = (
            rule.primary_intent in semantic_intent_names
            and semantic.primary_intent in rule_intent_names
        )
        if (
            rule.primary_intent != "general_assistance"
            and semantic.primary_intent != "general_assistance"
            and rule.primary_intent != semantic.primary_intent
            and not shared_composite_ranking
            and rule.primary_intent in combined_names
            and semantic.primary_intent in combined_names
        ):
            rule_primary = next((x for x in rule.intents if x.name == rule.primary_intent), None)
            semantic_primary = next((x for x in semantic.intents if x.name == semantic.primary_intent), None)
            if (
                rule_primary
                and semantic_primary
                and min(rule_primary.confidence, semantic_primary.confidence) >= self.conflict_threshold
            ):
                ambiguities.append(
                    f"规则主意图 {rule.primary_intent} 与语义主意图 {semantic.primary_intent} 冲突"
                )
                needs_clarification = True

        if needs_clarification and not questions:
            questions.append("请确认希望执行的具体任务和目标对象。")
        executable = [item for item in combined if not item.negated]
        accepted_names = {item.name for item in executable}
        if semantic.primary_intent in accepted_names:
            primary = semantic.primary_intent
        elif rule.primary_intent in accepted_names:
            primary = rule.primary_intent
        else:
            primary = executable[0].name if executable else "general_assistance"
        return IntentRecognitionResult(
            primary_intent=primary,
            intents=combined,
            entities=self._fuse_entities(rule.entities, semantic.entities),
            ambiguities=list(dict.fromkeys(ambiguities)),
            needs_clarification=needs_clarification,
            clarification_questions=questions,
            mode="hybrid",
            resolved_request=semantic.resolved_request,
            context_references=semantic.context_references,
        )


class HybridIntentRecognizer:
    def __init__(
        self,
        *,
        mode: str | None = None,
        semantic_provider: SemanticIntentProvider | None = None,
        conversation_context: dict[str, Any] | None = None,
    ) -> None:
        from src.service.env import (
            INTENT_AGREEMENT_BONUS,
            INTENT_CONFLICT_THRESHOLD,
            INTENT_RECOGNITION_MODE,
            INTENT_SEMANTIC_ACCEPT_THRESHOLD,
            INTENT_SEMANTIC_HIGH_RISK_THRESHOLD,
        )

        normalized_mode = str(mode or INTENT_RECOGNITION_MODE).strip().lower()
        if normalized_mode not in {"rule", "hybrid", "semantic"}:
            raise ValueError(f"Unsupported intent recognition mode: {normalized_mode}")
        self.mode = normalized_mode
        self.rule = RuleIntentRecognizer()
        self.semantic_provider = semantic_provider
        self.conversation_context = dict(conversation_context or {})
        self.fusion = IntentFusion(
            semantic_accept_threshold=INTENT_SEMANTIC_ACCEPT_THRESHOLD,
            semantic_high_risk_threshold=INTENT_SEMANTIC_HIGH_RISK_THRESHOLD,
            agreement_bonus=INTENT_AGREEMENT_BONUS,
            conflict_threshold=INTENT_CONFLICT_THRESHOLD,
        )

    async def recognize(self, user_query: str) -> IntentRecognitionResult:
        if self.mode == "rule":
            return await self.rule.recognize(user_query)

        provider = self.semantic_provider
        if provider is None:
            provider = LLMSemanticIntentProvider(
                conversation_context=self.conversation_context,
            )
        # 规则和语义模块相互独立；混合/语义模式下并行运行，融合前互不覆盖。
        rule_task = asyncio.create_task(self.rule.recognize(user_query))
        semantic_task = asyncio.create_task(
            SemanticIntentRecognizer(provider).recognize(user_query)
        )
        rule_result = await rule_task
        try:
            semantic_result = await semantic_task
        except SemanticProviderError as exc:
            logger.warning(
                "Semantic intent recognition degraded: reason=%s query_len=%s query_hash=%s",
                str(exc),
                len(user_query),
                _query_fingerprint(user_query),
            )
            rule_result.mode = self.mode  # type: ignore[assignment]
            rule_result.degraded = True
            rule_result.degradation_reason = str(exc)
            return rule_result

        if self.mode == "semantic":
            semantic_only = self.fusion.fuse(
                IntentRecognitionResult(mode="rule"), semantic_result
            )
            semantic_only.mode = "semantic"
            return semantic_only
        return self.fusion.fuse(rule_result, semantic_result)

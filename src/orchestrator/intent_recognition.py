from __future__ import annotations

import asyncio
import hashlib
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

    @property
    def executable_intents(self) -> list[IntentCandidate]:
        return [item for item in self.intents if not item.negated]


class SemanticIntentPayload(BaseModel):
    primary_intent: str = Field(
        default="general_assistance",
        json_schema_extra={"enum": sorted(SUPPORTED_INTENTS | {"general_assistance"})},
    )
    intents: list[IntentCandidate] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    ambiguities: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)

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

    def __init__(self, *, timeout_seconds: float | None = None) -> None:
        from src.service.env import INTENT_SEMANTIC_TIMEOUT_SECONDS

        self.timeout_seconds = timeout_seconds or INTENT_SEMANTIC_TIMEOUT_SECONDS

    @staticmethod
    def is_configured() -> bool:
        from src.llm.llm import get_llm_configuration_status

        return bool(get_llm_configuration_status()["details"]["basic"]["configured"])

    async def recognize(self, user_query: str) -> SemanticIntentPayload:
        if not self.is_configured():
            raise SemanticProviderError("semantic_provider_not_configured")

        from src.llm.llm import get_llm_by_type

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

允许的 intent name（必须逐字使用其中之一）：
{', '.join(sorted(SUPPORTED_INTENTS))}

标签说明：
{intent_prompt_catalog()}"""
        llm = get_llm_by_type("basic").with_structured_output(SemanticIntentPayload)
        messages = [("system", system_prompt), ("human", user_query)]
        try:
            response = await asyncio.wait_for(
                llm.ainvoke(messages), timeout=self.timeout_seconds
            )
            return SemanticIntentPayload.model_validate(response)
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
    "收入", "在职", "请假", "分析", "明天", "今天", "后天", "本月", "下月",
}


def extract_entities(text: str) -> dict[str, Any]:
    raw = str(text or "")
    entities: dict[str, Any] = {}

    recipient_match = re.search(
        r"(?:发给|发送给|寄给|转给|抄送给?|交给|通知)([\w.@\-\u4e00-\u9fff]{2,30}?)(?=$|[，,。；;]|然后|并且|并发|再)",
        raw,
    )
    if recipient_match:
        recipient = recipient_match.group(1).strip("，。；;,. ")
        if recipient:
            entities["recipient"] = recipient

    people: list[str] = []
    # 从动作与属格上下文中抽取姓名，不依赖固定人名表。
    patterns = (
        r"(?:查询|查一下|查看|看看|帮|为|把|取消|生成)(?:员工)?([\u4e00-\u9fff]{2,4}?)(?=的|生成|写|开|明天|后天|本月|下月|在职|收入|请假|休假)",
        r"(?:安排|预约)([\u4e00-\u9fff]{2,3})(?:和|与)([\u4e00-\u9fff]{2,3})(?=明天|后天|开会|的?会议)",
        r"(?:安排)?与([\u4e00-\u9fff]{2,3})(?=的?会议)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, raw):
            for group in match.groups():
                if not group:
                    continue
                candidate = group.removeprefix("员工").removeprefix("与").removeprefix("和").removesuffix("的")
                if candidate not in _PERSON_STOP_WORDS and candidate not in people:
                    people.append(candidate)
    if entities.get("recipient") and str(entities["recipient"]).endswith(("经理", "秘书")):
        recipient_person = str(entities["recipient"])
        if recipient_person not in people:
            people.append(recipient_person)
    if people:
        entities["people"] = people
        employee_candidates = [
            item for item in people if not item.endswith(("经理", "主管", "秘书", "负责人"))
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
            entities=payload.entities,
            ambiguities=payload.ambiguities,
            needs_clarification=payload.needs_clarification,
            clarification_questions=payload.clarification_questions,
            mode="semantic",
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

    def fuse(
        self,
        rule: IntentRecognitionResult,
        semantic: IntentRecognitionResult,
    ) -> IntentRecognitionResult:
        semantic_by_name = {item.name: item for item in semantic.intents}
        combined: list[IntentCandidate] = []
        consumed: set[str] = set()
        ambiguities = list(dict.fromkeys(rule.ambiguities + semantic.ambiguities))
        questions = list(dict.fromkeys(rule.clarification_questions + semantic.clarification_questions))
        needs_clarification = rule.needs_clarification or semantic.needs_clarification

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

        if (
            rule.primary_intent != "general_assistance"
            and semantic.primary_intent != "general_assistance"
            and rule.primary_intent != semantic.primary_intent
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
            entities={**rule.entities, **semantic.entities},
            ambiguities=list(dict.fromkeys(ambiguities)),
            needs_clarification=needs_clarification,
            clarification_questions=questions,
            mode="hybrid",
        )


class HybridIntentRecognizer:
    def __init__(
        self,
        *,
        mode: str | None = None,
        semantic_provider: SemanticIntentProvider | None = None,
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
            provider = LLMSemanticIntentProvider()
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

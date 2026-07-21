"""High-level orchestration for short-term and long-term Agent Memory."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid4, uuid5

from config.global_variables import memory_dir
from src.service import env

from .compaction import (
    CompactionEngine,
    CompactionToolCallError,
    build_bounded_emergency_context,
    render_compaction_segments,
)
from .models import (
    CompactionRecord,
    MemoryContextMetadata,
    MemoryMessage,
    PreparedMemoryContext,
    RecoveryAttachments,
)
from .retrieval import (
    LexicalMemoryRetriever,
    MemoryRetriever,
    format_untrusted_memories,
)
from .store import MemoryStore, SecretDetectedError
from .utils import (
    build_provenance,
    contains_secret,
    derive_session_id,
    estimate_tokens,
    redact_secrets,
)


logger = logging.getLogger(__name__)


_REMEMBER_PATTERNS = (
    re.compile(r"^\s*(?:请)?记住(?:一下)?[：:,，\s]*(?P<content>.+)$", re.DOTALL),
    re.compile(r"^\s*remember(?:\s+that)?[：:,\s]+(?P<content>.+)$", re.I | re.DOTALL),
)
_SIMPLE_GREETING_PATTERN = re.compile(
    r"^\s*(?:hi|hello|hey|thanks|thank you|你好|您好|嗨|谢谢|你是谁|who are you)[!.。！?？\s]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MemorySettings:
    enabled: bool = True
    long_term_enabled: bool = True
    auto_compact_enabled: bool = True
    llm_compaction_enabled: bool = False
    max_context_tokens: int = 32768
    reserved_output_tokens: int = 4096
    trigger_tokens: int = 21504
    target_tokens: int = 10752
    long_term_top_k: int = 5
    max_record_chars: int = 8000
    store_path: Path = memory_dir

    @property
    def input_budget(self) -> int:
        return max(1, self.max_context_tokens - self.reserved_output_tokens)

    @classmethod
    def from_env(cls) -> "MemorySettings":
        configured = env.MEMORY_STORE_DIR or env.MEMORY_DB_PATH
        path = Path(configured) if configured else memory_dir
        return cls(
            enabled=env.MEMORY_ENABLED,
            long_term_enabled=env.MEMORY_LONG_TERM_ENABLED,
            auto_compact_enabled=env.MEMORY_AUTO_COMPACT_ENABLED,
            llm_compaction_enabled=env.MEMORY_COMPACTION_LLM_ENABLED,
            max_context_tokens=env.MEMORY_MAX_CONTEXT_TOKENS,
            reserved_output_tokens=env.MEMORY_RESERVED_OUTPUT_TOKENS,
            trigger_tokens=env.MEMORY_COMPACTION_TRIGGER_TOKENS,
            target_tokens=env.MEMORY_COMPACTION_TARGET_TOKENS,
            long_term_top_k=env.MEMORY_LONG_TERM_TOP_K,
            max_record_chars=env.MEMORY_MAX_RECORD_CHARS,
            store_path=path,
        )


class MemoryManager:
    def __init__(
        self,
        *,
        settings: MemorySettings | None = None,
        store: MemoryStore | None = None,
        compactor: CompactionEngine | None = None,
        retriever: MemoryRetriever | None = None,
    ) -> None:
        self.settings = settings or MemorySettings.from_env()
        self.store = store or MemoryStore(self.settings.store_path)
        self.retriever = retriever or LexicalMemoryRetriever()
        self.compactor = compactor or CompactionEngine(
            summarizer=self._build_summarizer(),
            trigger_tokens=self.settings.trigger_tokens,
            target_tokens=self.settings.target_tokens,
        )

    def _build_summarizer(self) -> Any | None:
        if not self.settings.llm_compaction_enabled:
            return None
        try:
            from src.llm.llm import get_llm_by_type

            # The raw chat model is deliberately not bound to tools.
            return get_llm_by_type("basic")
        except Exception as exc:
            logger.warning("Memory compaction LLM unavailable: %s", type(exc).__name__)
            return None

    def resolve_session_id(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
    ) -> str:
        # Do not derive the conversation from workflow_id: Launch generates that
        # ID from message content, while Production reuses it later.
        return derive_session_id(user_id, session_id=session_id)

    async def prepare_context(
        self,
        *,
        user_id: str,
        incoming_messages: Sequence[Mapping[str, Any]],
        session_id: str | None = None,
        workflow_id: str | None = None,
        request_enabled: bool | None = None,
        retrieval_query: str | None = None,
        attachments: RecoveryAttachments | Mapping[str, Any] | None = None,
        hook_results: Sequence[Mapping[str, Any]] | None = None,
    ) -> PreparedMemoryContext:
        fallback_messages = tuple(self._sanitize_message(message) for message in incoming_messages)
        resolved = self.resolve_session_id(user_id, session_id=session_id)
        if not self.settings.enabled or request_enabled is False:
            return PreparedMemoryContext(
                messages=fallback_messages,
                metadata=MemoryContextMetadata(
                    session_id=resolved,
                    token_estimate=estimate_tokens(fallback_messages),
                    warning="memory_disabled",
                ),
            )

        normalized_attachments = (
            attachments
            if isinstance(attachments, RecoveryAttachments)
            else RecoveryAttachments.from_dict(attachments)
        )
        extra = dict(normalized_attachments.extra)
        extra.setdefault("rebuild_runtime_capabilities", True)
        normalized_attachments = RecoveryAttachments(
            recent_files=normalized_attachments.recent_files,
            current_plan=normalized_attachments.current_plan,
            active_skills=normalized_attachments.active_skills,
            async_tasks=normalized_attachments.async_tasks,
            extra=extra,
        )

        try:
            stored = await asyncio.to_thread(
                self._append_incoming,
                user_id,
                resolved,
                incoming_messages,
                workflow_id,
            )
            if self.settings.long_term_enabled:
                await self._promote_explicit_requests(stored, workflow_id)

            latest, tail = await asyncio.to_thread(
                self.store.messages_after_compaction, user_id, resolved
            )
            all_messages = await asyncio.to_thread(
                self.store.list_messages, user_id, resolved
            )
            projection = self._project(latest, tail if latest else all_messages)

            if (
                self.settings.auto_compact_enabled
                and estimate_tokens(projection) >= self.settings.trigger_tokens
            ):
                latest = await self._compact_for_request(
                    user_id=user_id,
                    session_id=resolved,
                    latest=latest,
                    all_messages=all_messages,
                    tail=tail,
                    attachments=normalized_attachments,
                    hook_results=hook_results,
                )
                if latest is not None:
                    _, tail = await asyncio.to_thread(
                        self.store.messages_after_compaction, user_id, resolved
                    )
                    projection = self._project(latest, tail)

            query = (
                redact_secrets(retrieval_query).strip()
                if retrieval_query
                else self._latest_user_content(stored)
                or self._latest_user_content(all_messages)
            )
            retrieved = []
            if (
                query
                and self.settings.long_term_enabled
                and not _SIMPLE_GREETING_PATTERN.fullmatch(query)
            ):
                long_term = await asyncio.to_thread(self.store.list_long_term, user_id)
                retrieved = self.retriever.retrieve(
                    query,
                    long_term,
                    user_id=user_id,
                    top_k=self.settings.long_term_top_k,
                )
                reference = format_untrusted_memories(retrieved)
                if reference:
                    projection.insert(
                        0,
                        {
                            "role": "assistant",
                            "content": reference,
                            "metadata": {"memory_type": "long_term_reference"},
                        },
                    )

            token_estimate = estimate_tokens(projection)
            warning = None
            if token_estimate > self.settings.input_budget:
                projection = build_bounded_emergency_context(
                    all_messages, self.settings.input_budget
                )
                token_estimate = estimate_tokens(projection)
                warning = "context_budget_emergency_projection"
            compactions = await asyncio.to_thread(
                self.store.list_compactions, user_id, resolved
            )
            metadata = MemoryContextMetadata(
                session_id=resolved,
                token_estimate=token_estimate,
                compaction_id=latest.compaction_id if latest else None,
                compaction_generation=len(compactions),
                retrieved_memory_ids=tuple(item.memory.memory_id for item in retrieved),
                attachment_references=normalized_attachments.recent_files,
                warning=warning,
            )
            return PreparedMemoryContext(
                messages=tuple(projection),
                metadata=metadata,
            )
        except Exception as exc:
            correlation = uuid4().hex[:12]
            logger.warning(
                "Memory soft failure correlation=%s type=%s",
                correlation,
                type(exc).__name__,
            )
            return PreparedMemoryContext(
                messages=fallback_messages,
                metadata=MemoryContextMetadata(
                    session_id=resolved,
                    token_estimate=estimate_tokens(fallback_messages),
                    warning=f"memory_soft_failure:{correlation}",
                ),
            )

    def _append_incoming(
        self,
        user_id: str,
        session_id: str,
        incoming_messages: Sequence[Mapping[str, Any]],
        workflow_id: str | None,
    ) -> list[MemoryMessage]:
        stored = []
        for message in incoming_messages:
            metadata = dict(message.get("metadata") or {})
            if contains_secret(str(message.get("content", ""))):
                metadata["secret_redacted"] = True
            identifier = message.get("message_id") or uuid4().hex
            stored.append(
                self.store.append_message(
                    user_id=user_id,
                    session_id=session_id,
                    role=str(message.get("role", "user")),
                    content=str(message.get("content", "")),
                    message_id=str(identifier),
                    workflow_id=workflow_id,
                    metadata=metadata,
                )
            )
        return stored

    async def _promote_explicit_requests(
        self, messages: Sequence[MemoryMessage], workflow_id: str | None
    ) -> None:
        for message in messages:
            if message.role != "user":
                continue
            if message.metadata.get("secret_redacted"):
                continue
            candidate = self._extract_explicit_memory(message.content)
            if candidate is None:
                continue
            try:
                await self.remember(
                    user_id=message.user_id,
                    content=candidate,
                    kind=("preference" if self._looks_like_preference(candidate) else "fact"),
                    confidence=1.0,
                    workflow_id=workflow_id,
                    session_id=message.session_id,
                    provenance=build_provenance(
                        "explicit_user_request",
                        message_id=message.message_id,
                        workflow_id=workflow_id,
                        session_id=message.session_id,
                        actor="user",
                    ),
                )
            except SecretDetectedError:
                logger.warning("Rejected secret-looking explicit memory request")

    @staticmethod
    def _extract_explicit_memory(content: str) -> str | None:
        for pattern in _REMEMBER_PATTERNS:
            match = pattern.match(content)
            if match:
                candidate = match.group("content").strip()
                return candidate or None
        return None

    @staticmethod
    def _looks_like_preference(content: str) -> bool:
        normalized = content.casefold()
        return any(token in normalized for token in ("prefer", "preference", "偏好", "喜欢"))

    async def _compact_for_request(
        self,
        *,
        user_id: str,
        session_id: str,
        latest: CompactionRecord | None,
        all_messages: Sequence[MemoryMessage],
        tail: Sequence[MemoryMessage],
        attachments: RecoveryAttachments,
        hook_results: Sequence[Mapping[str, Any]] | None,
    ) -> CompactionRecord | None:
        source = self._compaction_source(latest, all_messages, tail)
        # Preserve the active request verbatim as the post-boundary tail.
        if len(source) > 1 and source[-1].role == "user":
            source = source[:-1]
        if not source:
            return latest
        if _is_synthetic_summary(source[-1]):
            return latest
        try:
            record = await self.compactor.compact(
                source,
                trigger="auto",
                attachments=attachments,
                hook_results=hook_results,
            )
        except CompactionToolCallError:
            logger.warning("Memory compaction discarded because summarizer called a tool")
            return latest
        return await asyncio.to_thread(self.store.save_compaction, record)

    @staticmethod
    def _compaction_source(
        latest: CompactionRecord | None,
        all_messages: Sequence[MemoryMessage],
        tail: Sequence[MemoryMessage],
    ) -> list[MemoryMessage]:
        if latest is None:
            return list(all_messages)
        synthetic = MemoryMessage(
            message_id=f"summary:{latest.compaction_id}",
            user_id=latest.user_id,
            session_id=latest.session_id,
            sequence=latest.boundary.last_sequence,
            role="assistant",
            content=latest.summary,
            metadata={"memory_type": "prior_summary"},
        )
        return [synthetic, *tail]

    @staticmethod
    def _project(
        latest: CompactionRecord | None, messages: Sequence[MemoryMessage]
    ) -> list[dict[str, Any]]:
        projection = render_compaction_segments(latest) if latest else []
        projection.extend(
            {
                "role": message.role,
                "content": message.content,
                "metadata": {
                    **message.metadata,
                    "message_id": message.message_id,
                    "sequence": message.sequence,
                },
            }
            for message in messages
        )
        return projection

    @staticmethod
    def _latest_user_content(messages: Sequence[MemoryMessage]) -> str:
        for message in reversed(messages):
            if message.role == "user":
                return message.content
        return ""

    @staticmethod
    def _sanitize_message(message: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "role": str(message.get("role", "user")),
            "content": redact_secrets(str(message.get("content", ""))),
            **(
                {"metadata": dict(message.get("metadata") or {})}
                if message.get("metadata")
                else {}
            ),
        }

    async def record_assistant_outputs(
        self,
        *,
        user_id: str,
        session_id: str,
        outputs: Sequence[Mapping[str, Any]],
        workflow_id: str | None = None,
    ) -> list[MemoryMessage]:
        messages = []
        for output in outputs:
            content = str(output.get("content", "")).strip()
            if not content:
                continue
            agent_name = str(output.get("agent_name", "assistant"))
            identifier = output.get("message_id") or uuid5(
                NAMESPACE_URL,
                f"superagent-output:{workflow_id}:{session_id}:{agent_name}:{content}",
            ).hex
            messages.append(
                MemoryMessage(
                    message_id=str(identifier),
                    user_id=user_id,
                    session_id=session_id,
                    sequence=0,
                    role="assistant",
                    content=content,
                    workflow_id=workflow_id,
                    metadata={"agent_name": agent_name},
                )
            )
        if not messages or not self.settings.enabled:
            return []
        return await asyncio.to_thread(self.store.append_messages, messages)

    async def compact_session(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        attachments: RecoveryAttachments | Mapping[str, Any] | None = None,
        hook_results: Sequence[Mapping[str, Any]] | None = None,
    ) -> CompactionRecord:
        resolved = self.resolve_session_id(user_id, session_id=session_id)
        latest, tail = await asyncio.to_thread(
            self.store.messages_after_compaction, user_id, resolved
        )
        all_messages = await asyncio.to_thread(
            self.store.list_messages, user_id, resolved
        )
        source = self._compaction_source(latest, all_messages, tail)
        if not source:
            raise ValueError("session has no messages to compact")
        if latest is not None and not tail:
            return latest
        record = await self.compactor.compact(
            source,
            trigger="manual",
            attachments=attachments,
            hook_results=hook_results,
        )
        return await asyncio.to_thread(self.store.save_compaction, record)

    async def remember(self, **kwargs: Any):
        if not self.settings.enabled or not self.settings.long_term_enabled:
            raise RuntimeError("long-term memory is disabled")
        content = str(kwargs.get("content", ""))
        if len(content) > self.settings.max_record_chars:
            raise ValueError("memory content exceeds configured size limit")
        return await asyncio.to_thread(self.store.remember, **kwargs)

    async def list_long_term(self, user_id: str, *, query: str | None = None):
        records = await asyncio.to_thread(self.store.list_long_term, user_id)
        if not query:
            return records
        return self.retriever.retrieve(
            query,
            records,
            user_id=user_id,
            top_k=self.settings.long_term_top_k,
        )

    async def forget(self, user_id: str, memory_id: str) -> bool:
        return await asyncio.to_thread(self.store.delete_long_term, user_id, memory_id)

    async def list_session_messages(
        self, user_id: str, session_id: str | None = None
    ) -> list[MemoryMessage]:
        resolved = self.resolve_session_id(user_id, session_id=session_id)
        return await asyncio.to_thread(self.store.list_messages, user_id, resolved)


_manager: MemoryManager | None = None


def _is_synthetic_summary(message: MemoryMessage) -> bool:
    return message.message_id.startswith("summary:")


def get_memory_manager() -> MemoryManager:
    global _manager
    if _manager is None:
        _manager = MemoryManager()
    return _manager


def set_memory_manager(manager: MemoryManager | None) -> None:
    global _manager
    _manager = manager


__all__ = [
    "MemoryManager",
    "MemorySettings",
    "get_memory_manager",
    "set_memory_manager",
]
